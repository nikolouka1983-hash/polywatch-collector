#!/usr/bin/env python3
"""
POLYWATCH DATA COLLECTOR
========================
Polls Polymarket every hour, logs to SQLite.
Tracks market snapshots, price changes, volume spikes,
and resolution outcomes for strategy backtesting.

Run manually:   python3 collector.py
Run via cron:   0 * * * * cd /path/to/polywatch-collector && python3 collector.py
GitHub Actions: see .github/workflows/collect.yml
"""

import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import os
import sys
import traceback

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
DB_PATH        = os.environ.get("POLYWATCH_DB", "polywatch.db")
MARKETS_LIMIT  = int(os.environ.get("POLYWATCH_LIMIT", "200"))
LOG_FILE       = os.environ.get("POLYWATCH_LOG", "collector.log")
POLY_BASE      = "https://gamma-api.polymarket.com"
MANIFOLD_BASE  = "https://api.manifold.markets/v0"
HEADERS        = {"User-Agent": "Mozilla/5.0 (PolyWatch/1.0)", "Accept": "application/json"}


# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
def log(msg, level="INFO"):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ─────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────
def fetch(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise
            import time; time.sleep(2 ** attempt)


# ─────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
    -- One row per market per hourly snapshot
    CREATE TABLE IF NOT EXISTS snapshots (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at     TEXT NOT NULL,          -- ISO UTC timestamp
        market_id       TEXT NOT NULL,
        question        TEXT NOT NULL,
        category        TEXT,
        end_date        TEXT,
        yes_prob        REAL,                   -- 0.0–1.0
        yes_bid         REAL,
        yes_ask         REAL,
        last_trade      REAL,
        spread          REAL,
        volume_24h      REAL,
        volume_total    REAL,
        liquidity       REAL,
        chg_1h          REAL,                   -- price change last hour
        chg_24h         REAL,
        spike_ratio     REAL,                   -- volume_24h / volume_total
        hours_to_resolve REAL,                  -- NULL if no end_date
        is_featured     INTEGER DEFAULT 0,
        is_new          INTEGER DEFAULT 0
    );

    -- Tracks when a market resolves and what the final answer was
    CREATE TABLE IF NOT EXISTS resolutions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        resolved_at     TEXT NOT NULL,
        market_id       TEXT NOT NULL,
        question        TEXT NOT NULL,
        resolved_yes    INTEGER,                -- 1=YES, 0=NO, NULL=unknown
        final_price     REAL,
        total_volume    REAL
    );

    -- Stores manual "paper trade" hypotheses for backtesting
    CREATE TABLE IF NOT EXISTS paper_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT NOT NULL,
        market_id       TEXT NOT NULL,
        question        TEXT NOT NULL,
        direction       TEXT NOT NULL,          -- YES or NO
        entry_prob      REAL NOT NULL,          -- probability at entry
        entry_vol24h    REAL,
        signal_type     TEXT,                   -- what triggered this (spike, high_conf, etc.)
        resolved        INTEGER DEFAULT 0,
        outcome         TEXT,                   -- WIN, LOSS, PUSH
        exit_prob       REAL,
        pnl_pct         REAL                    -- estimated P&L %
    );

    -- Cross-platform divergence log (Poly vs Manifold)
    CREATE TABLE IF NOT EXISTS divergences (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at     TEXT NOT NULL,
        poly_market_id  TEXT NOT NULL,
        poly_question   TEXT NOT NULL,
        manifold_question TEXT,
        poly_prob       REAL,
        manifold_prob   REAL,
        spread          REAL,
        keywords        TEXT                    -- matched keywords (comma-sep)
    );

    -- Indexes for common queries
    CREATE INDEX IF NOT EXISTS idx_snap_market   ON snapshots (market_id, captured_at);
    CREATE INDEX IF NOT EXISTS idx_snap_time     ON snapshots (captured_at);
    CREATE INDEX IF NOT EXISTS idx_snap_prob     ON snapshots (yes_prob);
    CREATE INDEX IF NOT EXISTS idx_snap_spike    ON snapshots (spike_ratio DESC);
    CREATE INDEX IF NOT EXISTS idx_res_market    ON resolutions (market_id);
    """)
    conn.commit()
    log("Database schema initialised")


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def parse_price(prices, idx=0):
    if not prices:
        return None
    try:
        arr = json.loads(prices) if isinstance(prices, str) else prices
        return float(arr[idx])
    except Exception:
        return None


def get_category(market):
    events = market.get("events") or []
    if not events:
        return "Other"
    series = events[0].get("series") or []
    if series:
        return series[0].get("title", "Other")[:60]
    return (events[0].get("title") or "Other")[:60]


def hours_until(date_str):
    if not date_str:
        return None
    try:
        end = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (end - now).total_seconds() / 3600)
    except Exception:
        return None


def spike_ratio(m):
    vol_total = m.get("volumeNum") or 0
    vol_24h   = m.get("volume24hr") or 0
    if vol_total <= 0:
        return 0.0
    return round(vol_24h / vol_total, 4)


# ─────────────────────────────────────────
# POLYMARKET COLLECTION
# ─────────────────────────────────────────
def collect_polymarket(conn, captured_at):
    log(f"Fetching top {MARKETS_LIMIT} Polymarket markets...")
    url = (f"{POLY_BASE}/markets"
           f"?limit={MARKETS_LIMIT}&active=true&closed=false"
           f"&order=volume24hr&ascending=false")
    markets = fetch(url)
    log(f"  Got {len(markets)} markets")

    rows = []
    for m in markets:
        yes_prob  = parse_price(m.get("outcomePrices"), 0)
        rows.append((
            captured_at,
            str(m.get("id", "")),
            m.get("question", "")[:300],
            get_category(m),
            m.get("endDate"),
            yes_prob,
            m.get("bestBid"),
            m.get("bestAsk"),
            m.get("lastTradePrice"),
            m.get("spread"),
            m.get("volume24hr"),
            m.get("volumeNum"),
            m.get("liquidityNum"),
            m.get("oneHourPriceChange"),
            m.get("oneDayPriceChange"),
            spike_ratio(m),
            hours_until(m.get("endDate")),
            int(bool(m.get("featured"))),
            int(bool(m.get("new"))),
        ))

    conn.executemany("""
        INSERT INTO snapshots (
            captured_at, market_id, question, category, end_date,
            yes_prob, yes_bid, yes_ask, last_trade, spread,
            volume_24h, volume_total, liquidity,
            chg_1h, chg_24h, spike_ratio, hours_to_resolve,
            is_featured, is_new
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    log(f"  Saved {len(rows)} snapshots to DB")
    return markets


# ─────────────────────────────────────────
# CHECK FOR RESOLUTIONS
# ─────────────────────────────────────────
def check_resolutions(conn, markets, captured_at):
    """
    A market is resolved when its price is near 0 or 1 AND it was
    previously tracked at an intermediate probability.
    """
    resolved_count = 0
    # Get previously tracked market IDs that haven't resolved yet
    cur = conn.execute("""
        SELECT DISTINCT market_id FROM snapshots
        WHERE market_id NOT IN (SELECT market_id FROM resolutions)
    """)
    tracked_ids = {row[0] for row in cur.fetchall()}

    for m in markets:
        mid = str(m.get("id", ""))
        if mid not in tracked_ids:
            continue
        yes_prob = parse_price(m.get("outcomePrices"), 0)
        if yes_prob is None:
            continue
        # Consider resolved if price is at or very near 0 or 1
        if yes_prob >= 0.98 or yes_prob <= 0.02:
            resolved_yes = 1 if yes_prob >= 0.98 else 0
            conn.execute("""
                INSERT OR IGNORE INTO resolutions
                (resolved_at, market_id, question, resolved_yes, final_price, total_volume)
                VALUES (?,?,?,?,?,?)
            """, (
                captured_at,
                mid,
                m.get("question", "")[:300],
                resolved_yes,
                yes_prob,
                m.get("volumeNum"),
            ))
            resolved_count += 1

    conn.commit()
    if resolved_count:
        log(f"  Detected {resolved_count} market resolutions")
    return resolved_count


# ─────────────────────────────────────────
# MANIFOLD DIVERGENCE SCAN
# ─────────────────────────────────────────
STOP_WORDS = {
    "will","the","a","an","by","in","of","to","be","is","are","for","on","at",
    "or","and","not","no","yes","win","2025","2026","2027","2028","before",
    "after","this","that","than","more","over","next","last","its","their"
}

def collect_divergences(conn, poly_markets, captured_at):
    log("Scanning Manifold for cross-platform divergences...")
    try:
        mfd = fetch(f"{MANIFOLD_BASE}/markets?limit=100&sort=last-bet-time")
    except Exception as e:
        log(f"  Manifold fetch failed: {e}", "WARN")
        return

    binary = [m for m in mfd
              if m.get("probability") and m.get("outcomeType") == "BINARY"
              and m.get("volume", 0) > 100]

    rows = []
    for pm in poly_markets:
        yes_prob = parse_price(pm.get("outcomePrices"), 0)
        if yes_prob is None or yes_prob >= 0.97 or yes_prob <= 0.03:
            continue  # skip near-resolved
        if (pm.get("volume24hr") or 0) < 10000:
            continue  # skip illiquid

        p_words = {w for w in pm["question"].lower().replace("?","").split()
                   if w not in STOP_WORDS and len(w) > 4}

        for mm in binary:
            m_words = {w for w in mm["question"].lower().replace("?","").split()
                       if w not in STOP_WORDS and len(w) > 4}
            overlap = [w for w in (p_words & m_words) if len(w) > 4]
            if len(overlap) >= 3:
                spread = abs(yes_prob - mm["probability"])
                if spread >= 0.04:
                    rows.append((
                        captured_at,
                        str(pm.get("id", "")),
                        pm["question"][:200],
                        mm["question"][:200],
                        yes_prob,
                        mm["probability"],
                        round(spread, 4),
                        ",".join(overlap[:5]),
                    ))
                break

    if rows:
        conn.executemany("""
            INSERT INTO divergences
            (captured_at, poly_market_id, poly_question, manifold_question,
             poly_prob, manifold_prob, spread, keywords)
            VALUES (?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
        log(f"  Saved {len(rows)} cross-platform divergences")
    else:
        log("  No significant divergences found this run")


# ─────────────────────────────────────────
# WEEKLY SUMMARY (for pasting into Claude)
# ─────────────────────────────────────────
def generate_summary(conn):
    now = datetime.datetime.utcnow()
    week_ago = (now - datetime.timedelta(days=7)).isoformat()

    summary = []
    summary.append("=" * 60)
    summary.append(f"POLYWATCH WEEKLY SUMMARY — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    summary.append("=" * 60)

    # Total snapshots
    cur = conn.execute("SELECT COUNT(*), COUNT(DISTINCT market_id) FROM snapshots")
    total_snaps, unique_markets = cur.fetchone()
    summary.append(f"\nTotal snapshots collected: {total_snaps:,}")
    summary.append(f"Unique markets tracked:    {unique_markets:,}")

    # Hours collected
    cur = conn.execute("SELECT MIN(captured_at), MAX(captured_at) FROM snapshots")
    first, last = cur.fetchone()
    summary.append(f"Data range: {first} → {last}")

    # Resolutions
    cur = conn.execute("SELECT COUNT(*), SUM(resolved_yes) FROM resolutions")
    total_res, yes_res = cur.fetchone()
    if total_res:
        summary.append(f"\nResolutions detected: {total_res}")
        summary.append(f"  Resolved YES: {int(yes_res or 0)} | Resolved NO: {total_res - int(yes_res or 0)}")

    # Top volume spikes this week
    summary.append("\nTop 10 Volume Spikes (spike_ratio = 24h_vol / all_time_vol):")
    cur = conn.execute("""
        SELECT question, MAX(spike_ratio), MAX(volume_24h), AVG(yes_prob)
        FROM snapshots
        WHERE captured_at > ?
        GROUP BY market_id
        ORDER BY MAX(spike_ratio) DESC
        LIMIT 10
    """, (week_ago,))
    for q, ratio, vol, prob in cur.fetchall():
        summary.append(f"  [{ratio*100:.0f}% spike | ${vol/1000:.0f}K | {prob*100:.0f}% YES] {q[:65]}")

    # High confidence markets (>80%)
    summary.append("\nHigh Confidence Markets (avg YES >80%) this week:")
    cur = conn.execute("""
        SELECT question, AVG(yes_prob), MAX(volume_24h)
        FROM snapshots
        WHERE captured_at > ? AND yes_prob >= 0.80
        GROUP BY market_id
        ORDER BY AVG(yes_prob) DESC
        LIMIT 10
    """, (week_ago,))
    for q, prob, vol in cur.fetchall():
        summary.append(f"  [{prob*100:.0f}% YES | ${vol/1000:.0f}K 24h] {q[:65]}")

    # Biggest price movers
    summary.append("\nBiggest 1h Price Movers (abs change) this week:")
    cur = conn.execute("""
        SELECT question, MAX(ABS(chg_1h)), AVG(yes_prob)
        FROM snapshots
        WHERE captured_at > ? AND chg_1h IS NOT NULL
        GROUP BY market_id
        ORDER BY MAX(ABS(chg_1h)) DESC
        LIMIT 10
    """, (week_ago,))
    for q, chg, prob in cur.fetchall():
        summary.append(f"  [{chg*100:.1f}% move | {prob*100:.0f}% YES] {q[:65]}")

    # Cross-platform divergences
    cur = conn.execute("""
        SELECT poly_question, poly_prob, manifold_prob, spread, keywords
        FROM divergences
        WHERE captured_at > ?
        ORDER BY spread DESC
        LIMIT 10
    """, (week_ago,))
    divs = cur.fetchall()
    if divs:
        summary.append("\nLargest Cross-Platform Divergences (Poly vs Manifold):")
        for q, pp, mp, sp, kw in divs:
            summary.append(f"  [{sp*100:.0f}% spread | Poly {pp*100:.0f}% vs Mfd {mp*100:.0f}%] {q[:55]}")
            summary.append(f"     Keywords: {kw}")

    # Paper trade performance (if any)
    cur = conn.execute("""
        SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),
               AVG(pnl_pct)
        FROM paper_trades WHERE resolved=1
    """)
    pt_total, pt_wins, pt_avg_pnl = cur.fetchone()
    if pt_total:
        win_rate = (pt_wins or 0) / pt_total * 100
        summary.append(f"\nPaper Trade Performance ({pt_total} resolved):")
        summary.append(f"  Win rate: {win_rate:.1f}%")
        summary.append(f"  Avg P&L:  {(pt_avg_pnl or 0)*100:.1f}%")

    summary.append("\n" + "=" * 60)
    summary.append("Paste this into Claude for strategy analysis.")
    summary.append("=" * 60)

    text = "\n".join(summary)
    fname = f"summary_{now.strftime('%Y%m%d_%H%M')}.txt"
    with open(fname, "w") as f:
        f.write(text)
    log(f"Weekly summary saved to {fname}")
    print(text)
    return text


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    args = sys.argv[1:]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    init_db(conn)

    if "summary" in args:
        generate_summary(conn)
        conn.close()
        return

    captured_at = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"Starting collection run at {captured_at}")

    try:
        markets = collect_polymarket(conn, captured_at)
        check_resolutions(conn, markets, captured_at)
        collect_divergences(conn, markets, captured_at)
        log("Collection run complete ✓")

        # Print quick stats
        cur = conn.execute("SELECT COUNT(*), COUNT(DISTINCT market_id) FROM snapshots")
        total, unique = cur.fetchone()
        log(f"DB totals: {total:,} snapshots | {unique:,} unique markets")

    except Exception as e:
        log(f"Collection run FAILED: {e}", "ERROR")
        log(traceback.format_exc(), "ERROR")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
