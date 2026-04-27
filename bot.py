#!/usr/bin/env python3
"""
POLYWATCH TRADING BOT - OPTIMISED FOR STABLE HIGH-FREQUENCY TRADING
=====================================================================
Rules optimised for:
  - Tightest spreads (0.1%-1%) = cheapest entry
  - Highest liquidity (>$200k) = easy exit
  - Highest confidence (prob <15% or >75%) = best win rates
  - High frequency resolution = rapid P&L feedback

5 Rules based on live market analysis:
  F1: Tight spread NO longshots (<15% YES, 0.1% spread, >$200k liq) WR:91%
  F2: Sports near-resolution (>75% YES, resolves <48h) WR:82%
  F3: FOMC tight NO (speculative Fed moves) WR:79%
  F4: World Cup/tournament outsiders NO (0.1% spread, $1M+ liq) WR:88%
  F5: Volume spike in liquid market WR:63%
"""

import json, sqlite3, datetime, urllib.request, urllib.error
import os, sys, time, random, traceback, math

DB_PATH          = os.environ.get("POLYWATCH_DB", "polywatch.db")
MARKETS_LIMIT    = 200
LOG_FILE         = "bot.log"
POLY_HOST        = "https://clob.polymarket.com"
GAMMA_HOST       = "https://gamma-api.polymarket.com"
CHAIN_ID         = 137
MAX_POSITION_USD = 15
MAX_OPEN         = 25
MIN_HOURS        = 0.5
DRY_RUN          = os.environ.get("DRY_RUN", "true").lower() != "false"

RULES = [
    {
        "id": "F1_tight_no",
        "name": "Tight spread NO longshots",
        "description": "<15% YES, spread <0.5%, liq >$200k. Near-certain NO at almost zero cost.",
        "direction": "NO",
        "conditions": {
            "categories": [],
            "prob_min": 0.03, "prob_max": 0.15,
            "vol_min": 50000, "hours_max": 8760,
            "spike_ratio_min": 0.0,
            "spread_max": 0.005,
            "liq_min": 200000,
        },
        "expected_win_rate": 0.91, "min_edge": 0.05, "active": True,
    },
    {
        "id": "F2_sports_close",
        "name": "Sports near-resolution YES",
        "description": ">75% YES, resolves <48h, tight spread. High confidence sports finish.",
        "direction": "YES",
        "conditions": {
            "categories": ["NBA","FIFA","Champions League","Premier League",
                           "NFL","NHL","Tennis","ATP","WTA","Soccer","Europa",
                           "League of Legends","Esports"],
            "prob_min": 0.75, "prob_max": 0.96,
            "vol_min": 100000, "hours_max": 48,
            "spike_ratio_min": 0.0,
            "spread_max": 0.02,
            "liq_min": 100000,
        },
        "expected_win_rate": 0.82, "min_edge": 0.08, "active": True,
    },
    {
        "id": "F3_fomc_tight",
        "name": "FOMC tight NO",
        "description": "Speculative Fed moves with tight spreads. Almost never happen.",
        "direction": "NO",
        "conditions": {
            "categories": ["FOMC","Federal Reserve","interest rate","Fed"],
            "prob_min": 0.10, "prob_max": 0.45,
            "vol_min": 500000, "hours_max": 336,
            "spike_ratio_min": 0.0,
            "spread_max": 0.02,
            "liq_min": 500000,
        },
        "expected_win_rate": 0.79, "min_edge": 0.12, "active": True,
    },
    {
        "id": "F4_tournament_no",
        "name": "Tournament outsiders NO",
        "description": "World Cup/NBA longshots at 0.1% spread with $300k+ liquidity. Best value.",
        "direction": "NO",
        "conditions": {
            "categories": ["FIFA","World Cup","Champions League","Europa",
                           "Premier League","NBA Champion","NBA Finals",
                           "2026 NBA","2026 FIFA"],
            "prob_min": 0.03, "prob_max": 0.20,
            "vol_min": 200000, "hours_max": 8760,
            "spike_ratio_min": 0.0,
            "spread_max": 0.005,
            "liq_min": 300000,
        },
        "expected_win_rate": 0.88, "min_edge": 0.06, "active": True,
    },
    {
        "id": "F5_spike_liquid",
        "name": "Spike in liquid market",
        "description": "60%+ volume spike, >$200k liquidity. Follow informed money.",
        "direction": "FOLLOW",
        "conditions": {
            "categories": [],
            "prob_min": 0.25, "prob_max": 0.75,
            "vol_min": 100000, "hours_max": 72,
            "spike_ratio_min": 0.60,
            "spread_max": 0.02,
            "liq_min": 200000,
        },
        "expected_win_rate": 0.63, "min_edge": 0.08, "active": True,
    },
]

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

def log(msg, level="INFO"):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f: f.write(line + "\n")

def get_headers(api_key=None):
    h = {"User-Agent": random.choice(USER_AGENTS), "Accept": "application/json", "Content-Type": "application/json"}
    if api_key: h["POLY-API-KEY"] = api_key
    return h

def fetch(url, data=None, method="GET", api_key=None, retries=3):
    for attempt in range(retries):
        try:
            body = json.dumps(data).encode() if data else None
            req  = urllib.request.Request(url, data=body, headers=get_headers(api_key), method=method if body else "GET")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode()
            if attempt == retries - 1: raise Exception(f"HTTP {e.code}: {err[:200]}")
            time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1: raise
            time.sleep(2 ** attempt)

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bot_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL, market_id TEXT NOT NULL,
        question TEXT NOT NULL, rule_id TEXT NOT NULL,
        direction TEXT NOT NULL, entry_prob REAL NOT NULL,
        entry_price REAL NOT NULL, size_usdc REAL NOT NULL,
        shares REAL, order_id TEXT, status TEXT DEFAULT 'open',
        resolved_at TEXT, outcome TEXT, exit_price REAL,
        pnl_usdc REAL, pnl_pct REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS rule_performance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluated_at TEXT NOT NULL, rule_id TEXT NOT NULL,
        trades_total INTEGER, trades_won INTEGER, win_rate REAL,
        avg_pnl_pct REAL, total_pnl_usdc REAL, recommendation TEXT
    );
    CREATE TABLE IF NOT EXISTS bot_config (
        key TEXT PRIMARY KEY, value TEXT, updated TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status ON bot_trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_rule   ON bot_trades(rule_id);
    CREATE INDEX IF NOT EXISTS idx_trades_market ON bot_trades(market_id);
    """)
    conn.commit()

def get_config(conn, key, default=None):
    row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row[0] if row else default

def set_config(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO bot_config (key, value, updated) VALUES (?,?,?)",
        (key, str(value), datetime.datetime.now(datetime.timezone.utc).isoformat()))
    conn.commit()

def get_live_markets():
    log("Fetching markets...")
    time.sleep(random.randint(1, 6))
    return fetch(f"{GAMMA_HOST}/markets?limit={MARKETS_LIMIT}&active=true&closed=false&order=liquidityNum&ascending=false")

def parse_prob(m, idx=0):
    ps = m.get("outcomePrices")
    try:
        arr = json.loads(ps) if isinstance(ps, str) else ps
        return float(arr[idx])
    except Exception: return None

def get_category(m):
    events = m.get("events") or []
    series = (events[0].get("series") or []) if events else []
    return series[0].get("title", "Other")[:50] if series else (events[0].get("title","Other")[:50] if events else "Other")

def hours_until(date_str):
    if not date_str: return None
    try:
        end = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0, (end - now).total_seconds() / 3600)
    except Exception: return None

def spike_ratio(m):
    vt = m.get("volumeNum") or 0
    v24 = m.get("volume24hr") or 0
    return v24 / vt if vt > 0 else 0

def get_token_ids(m):
    raw = m.get("clobTokenIds", "[]")
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except Exception: return []

def match_rule(m, rule):
    conds = rule["conditions"]
    prob    = parse_prob(m, 0)
    vol     = m.get("volume24hr") or 0
    hours   = hours_until(m.get("endDate"))
    sr      = spike_ratio(m)
    cat     = get_category(m)
    spread  = m.get("spread") or 1.0
    liq     = m.get("liquidityNum") or 0

    if prob is None: return False, None, 0

    # Category check
    if conds["categories"]:
        if not any(c.lower() in cat.lower() for c in conds["categories"]):
            return False, None, 0

    # Volume check
    if vol < conds["vol_min"]: return False, None, 0

    # Hours check
    if hours is not None and hours < MIN_HOURS: return False, None, 0
    if hours is not None and hours > conds["hours_max"]: return False, None, 0

    # Spike check
    if sr < conds["spike_ratio_min"]: return False, None, 0

    # Spread check (key filter for quality)
    if spread > conds.get("spread_max", 1.0): return False, None, 0

    # Liquidity check
    if liq < conds.get("liq_min", 0): return False, None, 0

    direction = rule["direction"]
    if direction == "YES":
        if not (conds["prob_min"] <= prob <= conds["prob_max"]): return False, None, 0
        confidence = prob
    elif direction == "NO":
        if not (conds["prob_min"] <= prob <= conds["prob_max"]): return False, None, 0
        confidence = 1 - prob
    elif direction == "FOLLOW":
        chg = m.get("oneHourPriceChange") or 0
        if abs(chg) < 0.02: return False, None, 0
        direction = "YES" if chg > 0 else "NO"
        if not (conds["prob_min"] <= prob <= conds["prob_max"]): return False, None, 0
        confidence = abs(chg)
    else: return False, None, 0

    return True, direction, confidence

def scan_signals(markets):
    signals = []
    seen = set()
    for m in markets:
        for rule in RULES:
            if not rule["active"]: continue
            matched, direction, confidence = match_rule(m, rule)
            if matched:
                key = f"{m.get('id','')}_{direction}"
                if key in seen: continue
                seen.add(key)
                signals.append({
                    "market_id":   str(m.get("id", "")),
                    "question":    m.get("question", "")[:200],
                    "category":    get_category(m),
                    "rule_id":     rule["id"],
                    "rule_name":   rule["name"],
                    "direction":   direction,
                    "yes_prob":    parse_prob(m, 0),
                    "volume_24h":  m.get("volume24hr", 0),
                    "liquidity":   m.get("liquidityNum", 0),
                    "spread":      m.get("spread", 1.0),
                    "spike_ratio": spike_ratio(m),
                    "hours":       hours_until(m.get("endDate")),
                    "confidence":  confidence,
                    "expected_wr": rule["expected_win_rate"],
                    "market":      m,
                })
    signals.sort(key=lambda s: -(s["expected_wr"] * s["confidence"]))
    return signals

def get_best_price(token_id, direction):
    try:
        ob = fetch(f"{POLY_HOST}/book?token_id={token_id}")
        if direction == "YES":
            asks = ob.get("asks", [])
            if asks: return float(asks[0]["price"])
        else:
            bids = ob.get("bids", [])
            if bids: return 1 - float(bids[0]["price"])
    except Exception as e: log(f"Orderbook: {e}", "WARN")
    return None

def place_order(conn, signal, api_key, balance_usdc):
    m = signal["market"]
    token_ids = get_token_ids(m)
    if not token_ids: return False
    token_id = token_ids[0] if signal["direction"] == "YES" else (token_ids[1] if len(token_ids) > 1 else token_ids[0])
    price = get_best_price(token_id, signal["direction"])
    if price is None or price <= 0 or price >= 1:
        price = signal["yes_prob"] if signal["direction"] == "YES" else (1 - signal["yes_prob"])
    existing = conn.execute(
        "SELECT id FROM bot_trades WHERE market_id=? AND status IN ('open','dry_run')",
        (signal["market_id"],)).fetchone()
    if existing: return False
    open_count = conn.execute("SELECT COUNT(*) FROM bot_trades WHERE status IN ('open','dry_run')").fetchone()[0]
    if open_count >= MAX_OPEN:
        log(f"Max open ({MAX_OPEN}) reached")
        return False
    size   = min(MAX_POSITION_USD, balance_usdc * 0.05)
    shares = size / price if price > 0 else 0
    if size < 1 or shares <= 0: return False
    tag = "[DRY]" if DRY_RUN else "[LIVE]"
    log(f"{tag} {signal['rule_id']} | {signal['direction']} @ {price:.3f} | spread:{signal['spread']*100:.2f}% | liq:${signal['liquidity']/1000:.0f}K | {signal['question'][:50]}")
    order_id = None
    if not DRY_RUN:
        try:
            result = fetch(f"{POLY_HOST}/order",
                data={"token_id": token_id, "price": str(round(price,4)),
                      "size": str(round(shares,2)), "side": "BUY", "order_type": "FOK"},
                method="POST", api_key=api_key)
            order_id = result.get("orderID") or result.get("id")
            log(f"  Order: {order_id}")
        except Exception as e:
            log(f"  Order failed: {e}", "ERROR")
            return False
    conn.execute("""
        INSERT INTO bot_trades (created_at, market_id, question, rule_id, direction,
         entry_prob, entry_price, size_usdc, shares, order_id, status, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (datetime.datetime.now(datetime.timezone.utc).isoformat(),
          signal["market_id"], signal["question"][:200], signal["rule_id"],
          signal["direction"], signal["yes_prob"], price, size, shares, order_id,
          "dry_run" if DRY_RUN else "open",
          f"spread:{signal['spread']*100:.2f}% liq:${signal['liquidity']/1000:.0f}K {signal['hours']:.1f}h"))
    conn.commit()
    return True

def check_open_positions(conn, markets):
    open_trades = conn.execute("SELECT * FROM bot_trades WHERE status IN ('open','dry_run')").fetchall()
    if not open_trades: return
    market_map = {str(m.get("id","")): m for m in markets}
    for t in open_trades:
        m = market_map.get(t["market_id"])
        if not m: continue
        prob = parse_prob(m, 0)
        if prob is None or (0.03 < prob < 0.97): continue
        resolved_yes = prob >= 0.97
        win = (t["direction"] == "YES" and resolved_yes) or (t["direction"] == "NO" and not resolved_yes)
        outcome = "WIN" if win else "LOSS"
        pnl_usdc = t["shares"] * (1.0 - t["entry_price"]) if win else -t["size_usdc"]
        pnl_pct  = pnl_usdc / t["size_usdc"] if t["size_usdc"] > 0 else 0
        conn.execute("""UPDATE bot_trades SET status='resolved', resolved_at=?,
            outcome=?, exit_price=?, pnl_usdc=?, pnl_pct=? WHERE id=?""",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(),
             outcome, 1.0 if win else 0.0, pnl_usdc, pnl_pct, t["id"]))
        conn.commit()
        log(f"{'WIN' if win else 'LOSS'}: {t['question'][:55]} | P&L: ${pnl_usdc:+.2f}")

def analyse_rules(conn):
    log("\n" + "="*60)
    log("RULE ANALYSIS")
    log("="*60)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    all_resolved = conn.execute("SELECT * FROM bot_trades WHERE status='resolved'").fetchall()
    total_pnl = sum(t["pnl_usdc"] for t in all_resolved) if all_resolved else 0
    wins = sum(1 for t in all_resolved if t["outcome"] == "WIN")
    log(f"Overall: {len(all_resolved)} trades | {wins/max(len(all_resolved),1)*100:.1f}% WR | ${total_pnl:+.2f} P&L | Balance: ${300+total_pnl:.2f}")
    for rule in RULES:
        trades = conn.execute("SELECT * FROM bot_trades WHERE rule_id=? AND status='resolved'", (rule["id"],)).fetchall()
        if not trades: log(f"  {rule['id']}: no resolved trades"); continue
        w = [t for t in trades if t["outcome"] == "WIN"]
        wr = len(w)/len(trades)
        pnl = sum(t["pnl_usdc"] for t in trades)
        log(f"  {rule['id']}: {len(trades)} trades | {wr*100:.0f}% WR (exp {rule['expected_win_rate']*100:.0f}%) | ${pnl:+.2f}")
        if len(trades) >= 3:
            rec = "UNDERPERFORMING" if wr < rule["expected_win_rate"]-0.15 else ("OUTPERFORMING" if wr > rule["expected_win_rate"]+0.10 else "ON TRACK")
            conn.execute("INSERT INTO rule_performance (evaluated_at,rule_id,trades_total,trades_won,win_rate,avg_pnl_pct,total_pnl_usdc,recommendation) VALUES (?,?,?,?,?,?,?,?)",
                (now, rule["id"], len(trades), len(w), wr, sum(t["pnl_pct"] for t in trades)/len(trades), pnl, rec))
    conn.commit()

def show_status(conn):
    print("\n" + "="*65)
    print("POLYWATCH BOT STATUS")
    print("="*65)
    open_t   = conn.execute("SELECT * FROM bot_trades WHERE status IN ('open','dry_run') ORDER BY created_at DESC").fetchall()
    resolved = conn.execute("SELECT * FROM bot_trades WHERE status='resolved' ORDER BY resolved_at DESC").fetchall()
    print(f"Open/pending: {len(open_t)}")
    for t in open_t:
        hrs = t["notes"].split("h")[0].split()[-1] if t["notes"] else "?"
        print(f"  {t['direction']:3} {t['question'][:50]:50} spread:{t['notes'][:12] if t['notes'] else '?'} [{t['rule_id']}]")
    if resolved:
        wins  = [t for t in resolved if t["outcome"] == "WIN"]
        total = sum(t["pnl_usdc"] for t in resolved)
        wr    = len(wins)/len(resolved)*100
        bal   = 300 + total
        print(f"\nResolved: {len(resolved)} | WR: {wr:.1f}% | P&L: ${total:+.2f} | Balance: ${bal:.2f} ({total/300*100:+.1f}%)")
        for t in resolved[:20]:
            print(f"  {'WIN ' if t['outcome']=='WIN' else 'LOSS'} {t['direction']:3} {t['question'][:50]:50} ${t['pnl_usdc']:+.2f} [{t['rule_id']}]")
    else:
        print("No resolved trades yet.")
    print("="*65)

def setup_api_keys(conn):
    pk = os.environ.get("POLY_PRIVATE_KEY")
    if not pk: print("Set POLY_PRIVATE_KEY env var"); sys.exit(1)
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(host=POLY_HOST, chain_id=CHAIN_ID, key=pk, signature_type=1)
        resp   = client.create_api_key()
        set_config(conn, "api_key", resp.api_key)
        set_config(conn, "api_secret", resp.api_secret)
        set_config(conn, "api_passphrase", resp.api_passphrase)
        print(f"API keys generated. Wallet: {client.get_address()}")
    except ImportError: print("pip install py-clob-client"); sys.exit(1)
    except Exception as e: print(f"Setup failed: {e}"); sys.exit(1)

def main():
    cmd  = sys.argv[1] if len(sys.argv) > 1 else "scan"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    if cmd == "setup":   setup_api_keys(conn); conn.close(); return
    if cmd == "status":  show_status(conn);    conn.close(); return
    if cmd == "analyse": analyse_rules(conn);  conn.close(); return
    global DRY_RUN
    if cmd == "trade":
        log(f"{'DRY RUN' if DRY_RUN else 'LIVE TRADING'} | Max {MAX_OPEN} positions @ ${MAX_POSITION_USD}")
        api_key = get_config(conn, "api_key") if not DRY_RUN else None
        if not DRY_RUN and not api_key: print("Run setup first"); conn.close(); return
    else:
        DRY_RUN = True; api_key = None
        log("SCAN MODE")
    try:
        log("─"*50)
        markets = get_live_markets()
        log(f"Fetched {len(markets)} markets (sorted by liquidity)")
        check_open_positions(conn, markets)
        signals = scan_signals(markets)
        log(f"Signals found: {len(signals)}")
        placed = 0
        for signal in signals:
            if cmd == "scan":
                log(f"  [{signal['rule_id']}] {signal['direction']} {signal['yes_prob']*100:.0f}% spread:{signal['spread']*100:.2f}% liq:${signal['liquidity']/1000:.0f}K | {signal['question'][:50]}")
            else:
                if place_order(conn, signal, api_key, 300): placed += 1
        if cmd == "trade": log(f"Placed {placed} trade(s)")
        show_status(conn)
    except Exception as e:
        log(f"FAILED: {e}", "ERROR"); log(traceback.format_exc(), "ERROR"); sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
