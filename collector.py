#!/usr/bin/env python3
"""
POLYWATCH DATA COLLECTOR
========================
Polls Polymarket every hour, logs to SQLite.
"""

import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import os
import sys
import traceback
import time
import random

DB_PATH        = os.environ.get("POLYWATCH_DB", "polywatch.db")
MARKETS_LIMIT  = int(os.environ.get("POLYWATCH_LIMIT", "200"))
LOG_FILE       = os.environ.get("POLYWATCH_LOG", "collector.log")
POLY_BASE      = "https://gamma-api.polymarket.com"
MANIFOLD_BASE  = "https://api.manifold.markets/v0"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://polymarket.com/",
        "Origin": "https://polymarket.com",
    }


def log(msg, level="INFO"):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch(url, retries=5):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=get_headers())
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 403:
                wait = 15 + (attempt * 20) + random.randint(1, 10)
                log(f"403 on attempt {attempt+1}/{retries}, retrying in {wait}s...", "WARN")
                time.sleep(wait)
                if attempt == retries - 1:
                    raise
            elif e.code == 429:
                wait = 30 + (attempt * 20)
                log(f"429 rate limited, waiting {wait}s...", "WARN")
                time.sleep(wait)
                if attempt == retries - 1:
                    raise
            elif attempt == retries - 1:
                raise
            else:
                time.sleep(3 * (attempt + 1))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(3 * (attempt + 1))


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL, market_id TEXT NOT NULL,
        question TEXT NOT NULL, category TEXT, end_date TEXT,
        yes_prob REAL, yes_bid REAL, yes_ask REAL, last_trade REAL,
        spread REAL, volume_24h REAL, volume_total REAL, liquidity REAL,
        chg_1h REAL, chg_24h REAL, spike_ratio REAL,
        hours_to_resolve REAL, is_featured INTEGER DEFAULT 0, is_new INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        resolved_at TEXT NOT NULL, market_id TEXT NOT NULL,
        question TEXT NOT NULL, resolved_yes INTEGER,
        final_price REAL, total_volume REAL
    );
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL, market_id TEXT NOT NULL,
        question TEXT NOT NULL, direction TEXT NOT NULL,
        entry_prob REAL NOT NULL, entry_vol24h REAL, signal_type TEXT,
        resolved INTEGER DEFAULT 0, outcome TEXT, exit_prob REAL, pnl_pct REAL
    );
    CREATE TABLE IF NOT EXISTS divergences (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL, poly_market_id TEXT NOT NULL,
        poly_question TEXT NOT NULL, manifold_question TEXT,
        poly_prob REAL, manifold_prob REAL, spread REAL, keywords TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_snap_market ON snapshots (market_id, captured_at);
    CREATE INDEX IF NOT EXISTS idx_snap_time   ON snapshots (captured_at);
    CREATE INDEX IF NOT EXISTS idx_snap_prob   ON snapshots (yes_prob);
    CREATE INDEX IF NOT EXISTS idx_snap_spike  ON snapshots (spike_ratio DESC);
    CREATE INDEX IF NOT EXISTS idx_res_market  ON resolutions (market_id);
    """)
    conn.commit()
    log("Database schema initialised")


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


def collect_polymarket(conn, captured_at):
    log(f"Fetching top {MARKETS_LIMIT} Polymarket markets...")
    # Random delay so all runners dont hit API at same second
    time.sleep(random.randint(2, 12))
    url = (f"{POLY_BASE}/markets"
           f"?limit={MARKETS_LIMIT}&active=true&closed=false"
           f"&order=volume24hr&ascending=false")
    markets = fetch(url)
    log(f"  Got {len(markets)} markets")
    rows = []
    for m in markets:
        yes_prob = parse_price(m.get("outcomePrices"), 0)
        rows.append((
            captured_at, str(m.get("id", "")),
            m.get("question", "")[:300], get_category(m),
            m.get("endDate"), yes_prob,
            m.get("bestBid"), m.get("bestAsk"), m.get("lastTradePrice"),
            m.get("spread"), m.get("volume24hr"), m.get("volumeNum"),
            m.get("liquidityNum"), m.get("oneHourPriceChange"),
            m.get("oneDayPriceChange"), spike_ratio(m),
            hours_until(m.get("endDate")),
            int(bool(m.get("featured"))), int(bool(m.get("new"))),
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


def check_resolutions(conn, markets, captured_at):
    cur = conn.execute("""
        SELECT DISTINCT market_id FROM snapshots
        WHERE market_id NOT IN (SELECT market_id FROM resolutions)
    """)
    tracked_ids = {row[0] for row in cur.fetchall()}
    resolved_count = 0
    for m in markets:
        mid = str(m.get("id", ""))
        if mid not in tracked_ids:
            continue
        yes_prob = parse_price(m.get("outcomePrices"), 0)
        if yes_prob is None:
            continue
        if yes_prob >= 0.98 or yes_prob <= 0.02:
            resolved_yes = 1 if yes_prob >= 0.98 else 0
            conn.execute("""
                INSERT OR IGNORE INTO resolutions
                (resolved_at, market_id, question, resolved_yes, final_price, total_volume)
                VALUES (?,?,?,?,?,?)
            """, (captured_at, mid, m.get("question", "")[:300],
                  resolved_yes, yes_prob, m.get("volumeNum")))
            resolved_count += 1
    conn.commit()
    if resolved_count:
        log(f"  Detected {resolved_count} market resolutions")
    return resolved_count


STOP_WORDS = {
    "will","the","a","an","by","in","of","to","be","is","are","for","on","at",
    "or","and","not","no","yes","win","2025","2026","2027","2028","before",
    "after","this","that","than","more","over","next","last","its","their"
}


def collect_divergences(conn, poly_markets, captured_at):
    log("Scanning Manifold for cross-platform divergences...")
    try:
        time.sleep(2)
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
            continue
        if (pm.get("volume24hr") or 0) < 10000:
            continue
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
                        captured_at, str(pm.get("id", "")),
                        pm["question"][:200], mm["question"][:200],
                        yes_prob, mm["probability"],
                        round(spread, 4), ",".join(overlap[:5]),
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


def generate_summary(conn):
    now = datetime.datetime.now(datetime.timezone.utc)
    week_ago = (now - datetime.timedelta(days=7)).isoformat()
    summary = []
    summary.append("=" * 60)
    summary.append(f"POLYWATCH WEEKLY SUMMARY — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    summary.append("=" * 60)
    cur = conn.execute("SELECT COUNT(*), COUNT(DISTINCT market_id) FROM snapshots")
    total_snaps, unique_markets = cur.fetchone()
    summary.append(f"Total snapshots: {total_snaps:,}")
    summary.append(f"Unique markets:  {unique_markets:,}")
    cur = conn.execute("SELECT MIN(captured_at), MAX(captured_at) FROM snapshots")
    first, last = cur.fetchone()
    summary.append(f"Date range: {first} to {last}")
    cur = conn.execute("SELECT COUNT(*), SUM(resolved_yes) FROM resolutions")
    total_res, yes_res = cur.fetchone()
    if total_res:
        summary.append(f"Resolutions: {total_res} (YES: {int(yes_res or 0)} | NO: {total_res - int(yes_res or 0)})")
    summary.append("\nTop 10 Volume Spikes:")
    cur = conn.execute("""
        SELECT question, MAX(spike_ratio), MAX(volume_24h), AVG(yes_prob)
        FROM snapshots WHERE captured_at > ?
        GROUP BY market_id ORDER BY MAX(spike_ratio) DESC LIMIT 10
    """, (week_ago,))
    for q, ratio, vol, prob in cur.fetchall():
        summary.append(f"  [{ratio*100:.0f}% | ${vol/1000:.0f}K | {prob*100:.0f}% YES] {q[:60]}")
    text = "\n".join(summary)
    fname = f"summary_{now.strftime('%Y%m%d_%H%M')}.txt"
    with open(fname, "w") as f:
        f.write(text)
    log(f"Summary saved to {fname}")
    print(text)
    return text


def main():
    args = sys.argv[1:]
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    if "summary" in args:
        generate_summary(conn)
        conn.close()
        return
    captured_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log(f"Starting collection run at {captured_at}")
    try:
        markets = collect_polymarket(conn, captured_at)
        check_resolutions(conn, markets, captured_at)
        collect_divergences(conn, markets, captured_at)
        log("Collection run complete")
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
