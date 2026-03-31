#!/usr/bin/env python3
"""
POLYWATCH HISTORICAL BACKFILL
==============================
Fetches all resolved Polymarket markets and loads them into the same
SQLite database used by the live collector. Gives you years of resolved
market data immediately — without waiting weeks.

Run once:   python3 backfill.py
With flags: python3 backfill.py --min-vol 50000 --resume --with-history
Summary:    python3 backfill.py --summary
"""

import sqlite3, json, datetime, urllib.request, urllib.error, time, sys, os, argparse

DB_PATH   = os.environ.get("POLYWATCH_DB", "polywatch.db")
POLY_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
HEADERS   = {"User-Agent": "Mozilla/5.0 (PolyWatch/1.0)", "Accept": "application/json"}
LOG_FILE  = "backfill.log"

def log(msg, level="INFO"):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f: f.write(line + "\n")

def fetch(url, retries=3, delay=1.0):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10 * (attempt + 1))
            elif attempt == retries - 1: raise
            else: time.sleep(delay * (attempt + 1))
        except Exception as e:
            if attempt == retries - 1: raise
            time.sleep(delay * (attempt + 1))

def parse_price(prices, idx=0):
    if not prices: return None
    try:
        arr = json.loads(prices) if isinstance(prices, str) else prices
        return float(arr[idx])
    except: return None

def get_category(market):
    events = market.get("events") or []
    if not events: return "Other"
    series = events[0].get("series") or []
    if series: return series[0].get("title", "Other")[:60]
    return (events[0].get("title") or "Other")[:60]

def get_token_ids(market):
    raw = market.get("clobTokenIds", "[]")
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except: return []

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
        market_id TEXT NOT NULL, question TEXT NOT NULL, category TEXT, end_date TEXT,
        yes_prob REAL, yes_bid REAL, yes_ask REAL, last_trade REAL, spread REAL,
        volume_24h REAL, volume_total REAL, liquidity REAL, chg_1h REAL, chg_24h REAL,
        spike_ratio REAL, hours_to_resolve REAL, is_featured INTEGER DEFAULT 0, is_new INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, resolved_at TEXT NOT NULL,
        market_id TEXT NOT NULL, question TEXT NOT NULL,
        resolved_yes INTEGER, final_price REAL, total_volume REAL
    );
    CREATE TABLE IF NOT EXISTS historical_markets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT UNIQUE NOT NULL,
        question TEXT NOT NULL, category TEXT, start_date TEXT, end_date TEXT,
        resolved_yes INTEGER, final_price REAL, total_volume REAL,
        liquidity REAL, spread REAL, created_at TEXT, fetched_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL,
        token_id TEXT NOT NULL, ts INTEGER NOT NULL, price REAL NOT NULL,
        UNIQUE(market_id, ts)
    );
    CREATE TABLE IF NOT EXISTS divergences (
        id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT NOT NULL,
        poly_market_id TEXT NOT NULL, poly_question TEXT NOT NULL,
        manifold_question TEXT, poly_prob REAL, manifold_prob REAL,
        spread REAL, keywords TEXT
    );
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        market_id TEXT NOT NULL, question TEXT NOT NULL, direction TEXT NOT NULL,
        entry_prob REAL NOT NULL, entry_vol24h REAL, signal_type TEXT,
        resolved INTEGER DEFAULT 0, outcome TEXT, exit_prob REAL, pnl_pct REAL
    );
    CREATE INDEX IF NOT EXISTS idx_hist_market   ON historical_markets(market_id);
    CREATE INDEX IF NOT EXISTS idx_hist_category ON historical_markets(category);
    CREATE INDEX IF NOT EXISTS idx_hist_vol      ON historical_markets(total_volume DESC);
    CREATE INDEX IF NOT EXISTS idx_hist_resolved ON historical_markets(resolved_yes);
    CREATE INDEX IF NOT EXISTS idx_price_market  ON price_history(market_id, ts);
    CREATE INDEX IF NOT EXISTS idx_res_market    ON resolutions(market_id);
    CREATE INDEX IF NOT EXISTS idx_snap_market   ON snapshots(market_id, captured_at);
    """)
    conn.commit()
    log("Database schema ready")

def fetch_resolved_markets(conn, min_vol=10000, limit=None, resume=False):
    existing = set()
    if resume:
        cur = conn.execute("SELECT market_id FROM historical_markets")
        existing = {r[0] for r in cur.fetchall()}
        log(f"Resume mode: {len(existing)} already in DB")
    offset = 0
    total_inserted = 0
    skipped = 0
    log(f"Backfilling resolved markets (min_vol=${min_vol:,})")
    while True:
        if limit and total_inserted >= limit:
            log(f"Hit limit of {limit}"); break
        url = f"{POLY_BASE}/markets?limit=100&closed=true&order=volumeNum&ascending=false&offset={offset}"
        try:
            batch = fetch(url)
        except Exception as e:
            log(f"Fetch error at offset {offset}: {e}", "WARN"); time.sleep(5); continue
        if not batch:
            log("Empty batch — reached end of data"); break
        if all((m.get("volumeNum") or 0) < min_vol for m in batch):
            log(f"Batch below ${min_vol:,} at offset {offset}, stopping"); break
        rows, res_rows = [], []
        for m in batch:
            vol = m.get("volumeNum") or 0
            if vol < min_vol: continue
            mid = str(m.get("id", ""))
            if resume and mid in existing: skipped += 1; continue
            yes_price = parse_price(m.get("outcomePrices"), 0)
            if yes_price is None: continue
            resolved_yes = 1 if yes_price >= 0.98 else (0 if yes_price <= 0.02 else None)
            rows.append((mid, m.get("question","")[:300], get_category(m),
                m.get("startDate") or m.get("createdAt"), m.get("endDate"),
                resolved_yes, yes_price, vol, m.get("liquidityNum"), m.get("spread"),
                m.get("createdAt"), datetime.datetime.utcnow().isoformat()))
            if resolved_yes is not None:
                res_rows.append((m.get("endDate") or datetime.datetime.utcnow().isoformat(),
                    mid, m.get("question","")[:300], resolved_yes, yes_price, vol))
        if rows:
            conn.executemany("INSERT OR IGNORE INTO historical_markets (market_id, question, category, start_date, end_date, resolved_yes, final_price, total_volume, liquidity, spread, created_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            if res_rows:
                conn.executemany("INSERT OR IGNORE INTO resolutions (resolved_at, market_id, question, resolved_yes, final_price, total_volume) VALUES (?,?,?,?,?,?)", res_rows)
            conn.commit()
            total_inserted += len(rows)
        offset += 100
        if offset % 1000 == 0:
            log(f"  offset={offset:,} | inserted={total_inserted:,} | skipped={skipped:,}")
        time.sleep(0.25)
    log(f"Done: {total_inserted:,} markets inserted, {skipped:,} skipped")
    return total_inserted

def fetch_price_histories(conn, min_vol=50000, max_markets=500):
    log(f"Fetching price histories (min_vol=${min_vol:,}, max={max_markets})")
    cur = conn.execute("""
        SELECT h.market_id, h.question, h.total_volume FROM historical_markets h
        LEFT JOIN price_history ph ON h.market_id = ph.market_id
        WHERE ph.market_id IS NULL AND h.total_volume >= ?
        ORDER BY h.total_volume DESC LIMIT ?""", (min_vol, max_markets))
    markets = cur.fetchall()
    if not markets: log("No markets need price history"); return 0
    log(f"  {len(markets)} markets to process")
    fetched = failed = 0
    for i, (mid, question, vol) in enumerate(markets):
        try:
            m_data = fetch(f"{POLY_BASE}/markets/{mid}")
            token_ids = get_token_ids(m_data)
            if not token_ids: failed += 1; continue
            hist = fetch(f"{CLOB_BASE}/prices-history?market={token_ids[0]}&interval=max&fidelity=60")
            h = hist.get("history", [])
            if not h: failed += 1; continue
            rows = [(mid, token_ids[0], p["t"], p["p"]) for p in h if "t" in p and "p" in p]
            conn.executemany("INSERT OR IGNORE INTO price_history (market_id, token_id, ts, price) VALUES (?,?,?,?)", rows)
            conn.commit(); fetched += 1
            if i % 50 == 0: log(f"  [{i}/{len(markets)}] {fetched} fetched — {question[:50]}")
        except: failed += 1
        time.sleep(0.5)
    log(f"Price histories: {fetched} fetched, {failed} failed/empty")
    return fetched

def print_summary(conn):
    total = conn.execute("SELECT COUNT(*) FROM historical_markets").fetchone()[0]
    if not total: print("No historical data yet."); return
    yes_c = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE resolved_yes=1").fetchone()[0]
    no_c  = conn.execute("SELECT COUNT(*) FROM historical_markets WHERE resolved_yes=0").fetchone()[0]
    vol   = conn.execute("SELECT SUM(total_volume) FROM historical_markets").fetchone()[0] or 0
    ph    = conn.execute("SELECT COUNT(DISTINCT market_id) FROM price_history").fetchone()[0]
    print(f"\n{'='*60}\nHISTORICAL DATABASE SUMMARY\n{'='*60}")
    print(f"Total markets:   {total:,}")
    print(f"  YES: {yes_c:,} ({yes_c/max(total,1)*100:.1f}%) | NO: {no_c:,} ({no_c/max(total,1)*100:.1f}%)")
    print(f"Total volume:    ${vol/1e9:.2f}B USDC")
    print(f"Price histories: {ph:,} markets")
    rows = conn.execute("""SELECT category, COUNT(*) as cnt,
        SUM(CASE WHEN resolved_yes=1 THEN 1 ELSE 0 END) as yes_n,
        AVG(final_price) as avg_p, SUM(total_volume)/1e6 as vol_m
        FROM historical_markets WHERE resolved_yes IS NOT NULL
        GROUP BY category ORDER BY cnt DESC LIMIT 15""").fetchall()
    print(f"\n  {'Category':30} {'Mkts':>6} {'YES%':>6} {'AvgP':>6} {'Vol$M':>8}")
    print("  " + "-"*60)
    for cat, cnt, yes_n, avg_p, vol_m in rows:
        print(f"  {(cat or 'Other')[:30]:30} {cnt:>6,} {yes_n/cnt*100:>5.0f}% {(avg_p or 0)*100:>5.0f}% {vol_m:>8.1f}M")
    row = conn.execute("SELECT MIN(start_date), MAX(end_date) FROM historical_markets WHERE start_date IS NOT NULL").fetchone()
    if row[0]: print(f"\nDate range: {row[0][:10]} to {(row[1] or '')[:10]}")
    print(f"{'='*60}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-vol",      type=int,  default=10000)
    parser.add_argument("--limit",        type=int,  default=None)
    parser.add_argument("--with-history", action="store_true")
    parser.add_argument("--history-only", action="store_true")
    parser.add_argument("--resume",       action="store_true")
    parser.add_argument("--summary",      action="store_true")
    args = parser.parse_args()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    if args.summary: print_summary(conn); conn.close(); return
    start = datetime.datetime.utcnow()
    log(f"Backfill started — DB: {DB_PATH}")
    if not args.history_only:
        fetch_resolved_markets(conn, min_vol=args.min_vol, limit=args.limit, resume=args.resume)
    if args.with_history or args.history_only:
        fetch_price_histories(conn, min_vol=max(args.min_vol, 50000), max_markets=500)
    log(f"Total time: {(datetime.datetime.utcnow()-start).total_seconds():.0f}s")
    print_summary(conn)
    conn.close()

if __name__ == "__main__":
    main()
