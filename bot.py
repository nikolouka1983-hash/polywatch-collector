#!/usr/bin/env python3
"""
POLYWATCH TRADING BOT
=====================
Scans live Polymarket markets for signals matching backtested rules,
places trades via the CLOB API, tracks performance, and automatically
re-evaluates + adjusts rules weekly.

Setup:
  1. Set POLY_PRIVATE_KEY in your environment (your wallet private key)
  2. Run:  python3 bot.py setup        -- generate API keys from wallet
  3. Run:  python3 bot.py scan         -- scan for signals (dry run)
  4. Run:  python3 bot.py trade        -- live trading mode
  5. Run:  python3 bot.py analyse      -- re-evaluate rules from DB
  6. Run:  python3 bot.py status       -- show open positions + P&L

NEVER share your private key. Store it as an env variable only.
"""

import json
import sqlite3
import datetime
import urllib.request
import urllib.error
import os
import sys
import time
import random
import traceback

DB_PATH          = os.environ.get("POLYWATCH_DB", "polywatch.db")
POLY_HOST        = "https://clob.polymarket.com"
GAMMA_HOST       = "https://gamma-api.polymarket.com"
CHAIN_ID         = 137
MAX_POSITION_USD = 100
MAX_OPEN         = 5
RESERVE_PCT      = 0.50
MIN_LIQUIDITY    = 50000
MIN_HOURS        = 2
DRY_RUN          = True

LOG_FILE = "bot.log"

RULES = [
    {
        "id":          "A_sports_yes",
        "name":        "Sports YES bias",
        "description": "Sports markets (NFL/UCL/LoL/PL) resolve YES 60-86% historically",
        "direction":   "YES",
        "conditions": {
            "categories":     ["NFL", "UEFA Champions League", "League of Legends",
                               "Premier League", "NBA", "MLB", "CFB"],
            "prob_min":       0.55,
            "prob_max":       0.80,
            "vol_min":        50000,
            "hours_max":      72,
            "spike_ratio_min": 0.0,
        },
        "expected_win_rate": 0.68,
        "min_edge":          0.10,
        "active":            True,
    },
    {
        "id":          "B_fomc_no",
        "name":        "FOMC/macro NO bias",
        "description": "Speculative Fed rate moves resolve NO 79% historically",
        "direction":   "NO",
        "conditions": {
            "categories":     ["FOMC", "Bitcoin Hit Price Monthly",
                               "When will Bitcoin hit", "US strikes"],
            "prob_min":       0.20,
            "prob_max":       0.45,
            "vol_min":        500000,
            "hours_max":      168,
            "spike_ratio_min": 0.0,
        },
        "expected_win_rate": 0.79,
        "min_edge":          0.15,
        "active":            True,
    },
    {
        "id":          "C_spike_follow",
        "name":        "Volume spike follow",
        "description": "80%+ spike ratio = informed traders entering -- follow direction",
        "direction":   "FOLLOW",
        "conditions": {
            "categories":     [],
            "prob_min":       0.30,
            "prob_max":       0.70,
            "vol_min":        80000,
            "hours_max":      48,
            "spike_ratio_min": 0.80,
        },
        "expected_win_rate": 0.60,
        "min_edge":          0.08,
        "active":            True,
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
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def get_headers(api_key=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        h["POLY-API-KEY"] = api_key
    return h


def fetch(url, data=None, method="GET", api_key=None, retries=3):
    for attempt in range(retries):
        try:
            body = json.dumps(data).encode() if data else None
            req = urllib.request.Request(
                url, data=body,
                headers=get_headers(api_key),
                method=method if body else "GET"
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode()
            if attempt == retries - 1:
                raise Exception(f"HTTP {e.code}: {err[:200]}")
            time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS bot_trades (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      TEXT NOT NULL,
        market_id       TEXT NOT NULL,
        question        TEXT NOT NULL,
        rule_id         TEXT NOT NULL,
        direction       TEXT NOT NULL,
        entry_prob      REAL NOT NULL,
        entry_price     REAL NOT NULL,
        size_usdc       REAL NOT NULL,
        shares          REAL,
        order_id        TEXT,
        status          TEXT DEFAULT 'open',
        resolved_at     TEXT,
        outcome         TEXT,
        exit_price      REAL,
        pnl_usdc        REAL,
        pnl_pct         REAL,
        notes           TEXT
    );
    CREATE TABLE IF NOT EXISTS rule_performance (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        evaluated_at    TEXT NOT NULL,
        rule_id         TEXT NOT NULL,
        trades_total    INTEGER,
        trades_won      INTEGER,
        win_rate        REAL,
        avg_pnl_pct     REAL,
        total_pnl_usdc  REAL,
        recommendation  TEXT
    );
    CREATE TABLE IF NOT EXISTS bot_config (
        key     TEXT PRIMARY KEY,
        value   TEXT,
        updated TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_trades_status ON bot_trades(status);
    CREATE INDEX IF NOT EXISTS idx_trades_rule   ON bot_trades(rule_id);
    """)
    conn.commit()


def get_config(conn, key, default=None):
    row = conn.execute("SELECT value FROM bot_config WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_config(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO bot_config (key, value, updated) VALUES (?,?,?)",
        (key, str(value), datetime.datetime.now(datetime.timezone.utc).isoformat())
    )
    conn.commit()

def get_live_markets():
    log("Fetching live markets from Gamma API...")
    time.sleep(random.randint(1, 5))
    url = f"{GAMMA_HOST}/markets?limit=200&active=true&closed=false&order=volume24hr&ascending=false"
    return fetch(url)


def parse_prob(m, idx=0):
    ps = m.get("outcomePrices")
    try:
        arr = json.loads(ps) if isinstance(ps, str) else ps
        return float(arr[idx])
    except Exception:
        return None


def get_category(m):
    events = m.get("events") or []
    series = (events[0].get("series") or []) if events else []
    return series[0].get("title", "Other")[:50] if series else (
        events[0].get("title", "Other")[:50] if events else "Other"
    )


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
    vt = m.get("volumeNum") or 0
    v24 = m.get("volume24hr") or 0
    return v24 / vt if vt > 0 else 0


def get_orderbook(token_id):
    return fetch(f"{POLY_HOST}/book?token_id={token_id}")


def get_token_ids(m):
    raw = m.get("clobTokenIds", "[]")
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []


def match_rule(m, rule):
    conds = rule["conditions"]
    prob = parse_prob(m, 0)
    vol = m.get("volume24hr") or 0
    hours = hours_until(m.get("endDate"))
    sr = spike_ratio(m)
    cat = get_category(m)
    if prob is None:
        return False, None, 0
    if conds["categories"]:
        cat_match = any(c.lower() in cat.lower() for c in conds["categories"])
        if not cat_match:
            return False, None, 0
    if vol < conds["vol_min"]:
        return False, None, 0
    if hours is None or hours < MIN_HOURS or hours > conds["hours_max"]:
        return False, None, 0
    if sr < conds["spike_ratio_min"]:
        return False, None, 0
    direction = rule["direction"]
    if direction == "YES":
        if not (conds["prob_min"] <= prob <= conds["prob_max"]):
            return False, None, 0
        confidence = prob
    elif direction == "NO":
        if not (conds["prob_min"] <= prob <= conds["prob_max"]):
            return False, None, 0
        confidence = 1 - prob
    elif direction == "FOLLOW":
        chg = m.get("oneHourPriceChange") or 0
        if abs(chg) < 0.03:
            return False, None, 0
        direction = "YES" if chg > 0 else "NO"
        if not (conds["prob_min"] <= prob <= conds["prob_max"]):
            return False, None, 0
        confidence = abs(chg)
    else:
        return False, None, 0
    return True, direction, confidence


def scan_signals(markets):
    signals = []
    for m in markets:
        for rule in RULES:
            if not rule["active"]:
                continue
            matched, direction, confidence = match_rule(m, rule)
            if matched:
                prob = parse_prob(m, 0)
                signals.append({
                    "market_id":   str(m.get("id", "")),
                    "question":    m.get("question", "")[:200],
                    "category":    get_category(m),
                    "rule_id":     rule["id"],
                    "rule_name":   rule["name"],
                    "direction":   direction,
                    "yes_prob":    prob,
                    "volume_24h":  m.get("volume24hr", 0),
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
        ob = get_orderbook(token_id)
        if direction == "YES":
            asks = ob.get("asks", [])
            if asks:
                return float(asks[0]["price"])
        else:
            bids = ob.get("bids", [])
            if bids:
                return 1 - float(bids[0]["price"])
    except Exception as e:
        log(f"Orderbook error: {e}", "WARN")
    return None


def calculate_position_size(prob, rule, available_balance):
    p = rule["expected_win_rate"]
    q = 1 - p
    price = prob if rule["direction"] in ["YES", "FOLLOW"] else (1 - prob)
    if price <= 0 or price >= 1:
        return 0
    b = (1 / price) - 1
    if b <= 0:
        return 0
    kelly_fraction = (p * b - q) / b
    half_kelly = kelly_fraction * 0.5
    size = min(
        MAX_POSITION_USD,
        available_balance * 0.10,
        available_balance * half_kelly,
    )
    return max(0, size)


def place_order(conn, signal, api_key, balance_usdc):
    m = signal["market"]
    token_ids = get_token_ids(m)
    if not token_ids:
        log(f"No token IDs for {signal['question'][:50]}", "WARN")
        return False
    token_id = token_ids[0] if signal["direction"] == "YES" else token_ids[1]
    price = get_best_price(token_id, signal["direction"])
    if price is None:
        log(f"Could not get price for {signal['question'][:50]}", "WARN")
        return False
    existing = conn.execute(
        "SELECT id FROM bot_trades WHERE market_id=? AND status='open'",
        (signal["market_id"],)
    ).fetchone()
    if existing:
        log(f"Already have open position in {signal['question'][:40]}")
        return False
    open_count = conn.execute(
        "SELECT COUNT(*) FROM bot_trades WHERE status='open'"
    ).fetchone()[0]
    if open_count >= MAX_OPEN:
        log(f"Max open positions ({MAX_OPEN}) reached, skipping")
        return False
    size = calculate_position_size(signal["yes_prob"],
                                   next(r for r in RULES if r["id"] == signal["rule_id"]),
                                   balance_usdc)
    if size < 5:
        log(f"Position too small (${size:.2f}), skipping")
        return False
    shares = size / price
    log(f"{'[DRY RUN] ' if DRY_RUN else ''}SIGNAL: {signal['rule_name']}")
    log(f"  Market:    {signal['question'][:60]}")
    log(f"  Direction: {signal['direction']} @ {price:.3f}")
    log(f"  Size:      ${size:.2f} = {shares:.1f} shares")
    log(f"  Expected win rate: {signal['expected_wr']*100:.0f}%")
    log(f"  Hours to resolve:  {signal['hours']:.1f}h")
    order_id = None
    if not DRY_RUN:
        try:
            order_data = {
                "token_id":   token_id,
                "price":      str(round(price, 4)),
                "size":       str(round(shares, 2)),
                "side":       "BUY",
                "order_type": "FOK",
            }
            result = fetch(f"{POLY_HOST}/order", data=order_data, method="POST", api_key=api_key)
            order_id = result.get("orderID") or result.get("id")
            log(f"  Order placed: {order_id}")
        except Exception as e:
            log(f"  Order failed: {e}", "ERROR")
            return False
    conn.execute("""
        INSERT INTO bot_trades
        (created_at, market_id, question, rule_id, direction,
         entry_prob, entry_price, size_usdc, shares, order_id, status, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
        signal["market_id"], signal["question"][:200],
        signal["rule_id"], signal["direction"],
        signal["yes_prob"], price, size, shares, order_id,
        "dry_run" if DRY_RUN else "open",
        f"Vol: ${signal['volume_24h']/1000:.0f}K | Spike: {signal['spike_ratio']*100:.0f}% | {signal['hours']:.1f}h"
    ))
    conn.commit()
    return True

def check_open_positions(conn, markets, api_key):
    open_trades = conn.execute(
        "SELECT * FROM bot_trades WHERE status IN ('open', 'dry_run')"
    ).fetchall()
    if not open_trades:
        return
    market_map = {str(m.get("id", "")): m for m in markets}
    for t in open_trades:
        mid = t["market_id"]
        if mid not in market_map:
            continue
        m = market_map[mid]
        prob = parse_prob(m, 0)
        if prob is None:
            continue
        if prob >= 0.98:
            resolved_yes = True
        elif prob <= 0.02:
            resolved_yes = False
        else:
            continue
        if (t["direction"] == "YES" and resolved_yes) or \
           (t["direction"] == "NO" and not resolved_yes):
            outcome = "WIN"
            exit_price = 1.0
            pnl_usdc = t["shares"] * (1.0 - t["entry_price"])
        else:
            outcome = "LOSS"
            exit_price = 0.0
            pnl_usdc = -t["size_usdc"]
        pnl_pct = pnl_usdc / t["size_usdc"] if t["size_usdc"] > 0 else 0
        conn.execute("""
            UPDATE bot_trades
            SET status='resolved', resolved_at=?, outcome=?,
                exit_price=?, pnl_usdc=?, pnl_pct=?
            WHERE id=?
        """, (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            outcome, exit_price, pnl_usdc, pnl_pct, t["id"]
        ))
        conn.commit()
        emoji = "WIN" if outcome == "WIN" else "LOSS"
        log(f"{emoji} Resolved: {t['question'][:50]} -- {outcome} P&L: ${pnl_usdc:+.2f} ({pnl_pct*100:+.1f}%)")


def analyse_rules(conn):
    log("\n" + "=" * 60)
    log("WEEKLY RULE ANALYSIS")
    log("=" * 60)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for rule in RULES:
        trades = conn.execute(
            "SELECT * FROM bot_trades WHERE rule_id=? AND status='resolved' ORDER BY created_at",
            (rule["id"],)
        ).fetchall()
        if not trades:
            log(f"\nRule {rule['id']}: No resolved trades yet")
            continue
        wins = [t for t in trades if t["outcome"] == "WIN"]
        win_rate = len(wins) / len(trades)
        avg_pnl = sum(t["pnl_pct"] for t in trades) / len(trades)
        total_pnl = sum(t["pnl_usdc"] for t in trades)
        log(f"\nRule: {rule['name']}")
        log(f"  Trades: {len(trades)} | Win rate: {win_rate*100:.1f}% | Expected: {rule['expected_win_rate']*100:.1f}%")
        log(f"  Avg P&L: {avg_pnl*100:+.1f}% | Total P&L: ${total_pnl:+.2f}")
        rec = None
        if len(trades) >= 10:
            if win_rate < rule["expected_win_rate"] - 0.15:
                rec = f"UNDERPERFORMING: Actual {win_rate*100:.0f}% vs expected {rule['expected_win_rate']*100:.0f}%. Tighten prob range or pause."
                log(f"  WARN: {rec}")
            elif win_rate > rule["expected_win_rate"] + 0.10:
                rec = f"OUTPERFORMING: {win_rate*100:.0f}% vs expected {rule['expected_win_rate']*100:.0f}%. Consider increasing size."
                log(f"  GREAT: {rec}")
            else:
                rec = f"ON TRACK: {win_rate*100:.0f}% win rate matches expectations."
                log(f"  OK: {rec}")
            if total_pnl < -50:
                rec += " ALERT: Total P&L below -$50. Review immediately."
                log(f"  ALERT: Total P&L: ${total_pnl:.2f}")
        else:
            rec = f"Insufficient data ({len(trades)} trades). Need 10+ to evaluate."
            log(f"  INFO: {rec}")
        conn.execute("""
            INSERT INTO rule_performance
            (evaluated_at, rule_id, trades_total, trades_won, win_rate, avg_pnl_pct, total_pnl_usdc, recommendation)
            VALUES (?,?,?,?,?,?,?,?)
        """, (now, rule["id"], len(trades), len(wins), win_rate, avg_pnl, total_pnl, rec))
    conn.commit()
    try:
        rows = conn.execute("""
            SELECT category, COUNT(*) as n,
                   SUM(CASE WHEN resolved_yes=1 THEN 1 ELSE 0 END) as yes_n,
                   SUM(total_volume)/1e6 as vol_m
            FROM historical_markets
            WHERE resolved_yes IS NOT NULL AND total_volume >= 10000
            GROUP BY category HAVING n >= 5 ORDER BY n DESC LIMIT 15
        """).fetchall()
        if rows:
            log(f"{'Category':35} {'N':>5} {'YES%':>6} {'Edge':>6}")
            for r in rows:
                yes_pct = (r["yes_n"] / r["n"]) * 100
                log(f"{(r['category'] or 'Other')[:35]:35} {r['n']:>5} {yes_pct:>5.0f}% {yes_pct-50:>+5.0f}%")
    except Exception as e:
        log(f"Historical analysis error: {e}")
    log("=" * 60)


def show_status(conn):
    print("\n" + "=" * 65)
    print("POLYWATCH BOT STATUS")
    print("=" * 65)
    open_trades = conn.execute(
        "SELECT * FROM bot_trades WHERE status IN ('open','dry_run') ORDER BY created_at DESC"
    ).fetchall()
    print(f"\nOpen positions: {len(open_trades)}")
    for t in open_trades:
        print(f"  {t['direction']:3} {t['question'][:50]:50} | ${t['size_usdc']:.0f} @ {t['entry_price']:.3f}")
    resolved = conn.execute(
        "SELECT * FROM bot_trades WHERE status='resolved' ORDER BY resolved_at DESC"
    ).fetchall()
    if resolved:
        wins = [t for t in resolved if t["outcome"] == "WIN"]
        total_pnl = sum(t["pnl_usdc"] for t in resolved)
        win_rate = len(wins) / len(resolved) * 100
        print(f"\nResolved: {len(resolved)} | Win rate: {win_rate:.1f}% | Total P&L: ${total_pnl:+.2f}")
        for t in resolved[:10]:
            emoji = "WIN" if t["outcome"] == "WIN" else "LOSS"
            print(f"  {emoji} {t['direction']:3} {t['question'][:45]:45} | ${t['pnl_usdc']:+.2f}")
    print("=" * 65)


def setup_api_keys(conn):
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    if not private_key:
        print("ERROR: Set POLY_PRIVATE_KEY environment variable first.")
        print("  export POLY_PRIVATE_KEY=0x...")
        sys.exit(1)
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient(host=POLY_HOST, chain_id=CHAIN_ID, key=private_key, signature_type=1)
        resp = client.create_api_key()
        set_config(conn, "api_key",        resp.api_key)
        set_config(conn, "api_secret",     resp.api_secret)
        set_config(conn, "api_passphrase", resp.api_passphrase)
        print(f"API keys generated and stored. Wallet: {client.get_address()}")
    except ImportError:
        print("Install: pip install py-clob-client")
        sys.exit(1)
    except Exception as e:
        print(f"Setup failed: {e}")
        sys.exit(1)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    if cmd == "setup":
        setup_api_keys(conn)
        conn.close()
        return
    if cmd == "status":
        show_status(conn)
        conn.close()
        return
    if cmd == "analyse":
        analyse_rules(conn)
        conn.close()
        return
    global DRY_RUN
    if cmd == "trade":
        DRY_RUN = False
        log("LIVE TRADING MODE -- real money will be spent")
        api_key = get_config(conn, "api_key")
        if not api_key:
            print("Run 'python3 bot.py setup' first")
            conn.close()
            return
    else:
        DRY_RUN = True
        api_key = None
        log("DRY RUN MODE -- no real trades will be placed")
    while True:
        try:
            log(f"\n{'--'*25}")
            markets = get_live_markets()
            log(f"Scanning {len(markets)} markets...")
            check_open_positions(conn, markets, api_key)
            signals = scan_signals(markets)
            log(f"Found {len(signals)} signal(s)")
            placed = 0
            for signal in signals:
                if cmd == "scan":
                    log(f"  SIGNAL [{signal['rule_name']}] {signal['direction']} -- {signal['question'][:55]}")
                    log(f"    Prob: {signal['yes_prob']*100:.1f}% | Vol: ${signal['volume_24h']/1000:.0f}K | {signal['hours']:.1f}h | WR: {signal['expected_wr']*100:.0f}%")
                else:
                    ok = place_order(conn, signal, api_key, balance_usdc=500)
                    if ok:
                        placed += 1
            if cmd != "scan":
                log(f"Placed {placed} orders")
            if datetime.datetime.now(datetime.timezone.utc).weekday() == 6:
                last = get_config(conn, "last_analysis", "")
                today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
                if last != today:
                    analyse_rules(conn)
                    set_config(conn, "last_analysis", today)
        except KeyboardInterrupt:
            log("Bot stopped")
            break
        except Exception as e:
            log(f"Run error: {e}", "ERROR")
            log(traceback.format_exc(), "ERROR")
        if cmd == "scan":
            break
        log("Sleeping 60 minutes...")
        time.sleep(3600)
    conn.close()


if __name__ == "__main__":
    main()
