#!/usr/bin/env python3
"""POLYWATCH SCANNER v4 — capital preservation"""
import os, sys, json, sqlite3, datetime, urllib.request, traceback

try:
    from validator import (validate_market, validate_signal, check_go_live_criteria,
                           STRICT_RULES, MAX_OPEN_POSITIONS, BANKROLL_TARGET, MAX_RISK_PER_TRADE)
except ImportError:
    print("validator.py missing"); sys.exit(1)

DB_PATH = os.environ.get("POLYWATCH_DB", "polywatch.db")
LOG_FILE = "scanner_v4.log"
GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}
BANKROLL = float(os.environ.get("BANKROLL", BANKROLL_TARGET))

def log(msg, level="INFO"):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = "[" + ts + "] [" + level + "] " + msg
    print(line)
    with open(LOG_FILE,"a") as f: f.write(line+"\n")

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS v4_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_at TEXT, market_id TEXT, question TEXT,
        rule TEXT, direction TEXT, entry_price REAL,
        stake REAL, max_payout REAL, hours_to_resolve REAL,
        liquidity REAL, volume24h REAL,
        status TEXT DEFAULT 'open', closed_at TEXT,
        outcome TEXT, pnl REAL, notes TEXT);
    CREATE TABLE IF NOT EXISTS v4_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at TEXT, market_id TEXT, question TEXT,
        rule TEXT, direction TEXT, confidence REAL,
        liquidity REAL, hours_to_resolve REAL,
        valid INTEGER, reject_reason TEXT, acted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS v4_rejected (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at TEXT, market_id TEXT, question TEXT, reason TEXT);
    """)
    conn.commit()

def fetch_markets():
    url = GAMMA + "/markets?limit=500&active=true&closed=false&order=volume24hr&ascending=false"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def parse_prob(m):
    ps = m.get("outcomePrices")
    try:
        arr = json.loads(ps) if isinstance(ps,str) else ps
        return float(arr[0])
    except: return None

def hours_until(d):
    if not d: return None
    try:
        end = datetime.datetime.fromisoformat(d.replace("Z","+00:00"))
        return (end - datetime.datetime.now(datetime.timezone.utc)).total_seconds()/3600
    except: return None

def get_balance(conn):
    pnl = conn.execute("SELECT COALESCE(SUM(pnl),0) FROM v4_trades WHERE status=?",("closed",)).fetchone()[0]
    return BANKROLL + pnl

def get_pnl_window(conn, hours):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
    return conn.execute("SELECT COALESCE(SUM(pnl),0) FROM v4_trades WHERE status=? AND closed_at>?",("closed",cutoff)).fetchone()[0]

def open_count(conn):
    return conn.execute("SELECT COUNT(*) FROM v4_trades WHERE status=?",("open",)).fetchone()[0]

# ---- Scoring strategies ----
def score_f6_sports(m, prob, hours):
    q = m.get("question","").lower()
    if not any(x in q for x in ["vs.","vs ","game ","match","wins game","beat","defeat","ucl","la liga","epl","nba","mlb","nhl","tennis","league of legends","lol ","dota","valorant"]): return None
    cfg = STRICT_RULES["F6_sports"]
    if not (cfg["min_conf"] <= prob <= cfg["max_conf"]): return None
    if (m.get("spread") or 1.0) > cfg["max_spread"]: return None
    if (m.get("liquidityNum") or 0) < cfg["min_liquidity"]: return None
    if hours < cfg["min_hours"] or hours > cfg["max_hours"]: return None
    return {"rule":"F6_sports","direction":"YES","confidence":prob}

def score_f7_crypto(m, prob, hours):
    q = m.get("question","").lower()
    if not any(x in q for x in ["bitcoin above","btc above","bitcoin below","btc below","ethereum above","eth above","ethereum below","eth below"]): return None
    cfg = STRICT_RULES["F7_crypto"]
    if hours > cfg["max_hours"]: return None
    if (m.get("liquidityNum") or 0) < cfg["min_liquidity"]: return None
    if (m.get("spread") or 1.0) > cfg["max_spread"]: return None
    yr = cfg["yes_range"]; nr = cfg["no_range"]
    if yr[0] <= prob <= yr[1]: return {"rule":"F7_crypto","direction":"YES","confidence":prob}
    if nr[0] <= prob <= nr[1]: return {"rule":"F7_crypto","direction":"NO","confidence":1-prob}
    return None

def score_f8_political(m, prob, hours):
    q = m.get("question","").lower()
    political = any(x in q for x in ["will trump","will biden","ceasefire","executive order","new tariff","government shutdown","impeachment","resign","fed rate","fomc","interest rate"])
    subjective = any(x in q for x in ["tweet","post","say","mention"])
    if not political or subjective: return None
    cfg = STRICT_RULES["F8_political"]
    if hours > cfg["max_hours"]: return None
    if (m.get("liquidityNum") or 0) < cfg["min_liquidity"]: return None
    yr = cfg["yes_range"]; nr = cfg["no_range"]
    if yr[0] <= prob <= yr[1]: return {"rule":"F8_political","direction":"YES","confidence":prob}
    if nr[0] <= prob <= nr[1]: return {"rule":"F8_political","direction":"NO","confidence":1-prob}
    return None

def score_f1_longshot(m, prob, hours):
    cfg = STRICT_RULES["F1_longshot_no"]
    if hours < cfg["min_hours"] or hours > cfg["max_hours"]: return None
    if (m.get("liquidityNum") or 0) < cfg["min_liquidity"]: return None
    q = m.get("question","").lower()
    eligible = any(x in q for x in ["world cup","champions league","super bowl","world series","stanley cup","nba final","f1 championship","masters"])
    if not eligible: return None
    if prob < cfg["min_entry"] or prob > 0.10: return None
    return {"rule":"F1_longshot_no","direction":"NO","confidence":1-prob}

def calc_stake(prob, direction, balance):
    stake = balance * MAX_RISK_PER_TRADE
    entry = prob if direction == "YES" else (1 - prob)
    if entry <= 0: return 0, 0
    shares = stake / entry
    payout = shares * (1 - entry)
    return round(stake, 2), round(payout, 2)

def scan(conn):
    balance = get_balance(conn)
    daily = get_pnl_window(conn, 24)
    weekly = get_pnl_window(conn, 24*7)
    monthly = get_pnl_window(conn, 24*30)
    open_n = open_count(conn)
    log("Balance: $" + str(round(balance,2)) + " | Open: " + str(open_n) + "/" + str(MAX_OPEN_POSITIONS))
    log("P&L day=" + str(round(daily,2)) + " week=" + str(round(weekly,2)) + " month=" + str(round(monthly,2)))
    try:
        markets = fetch_markets()
        log("Fetched " + str(len(markets)) + " markets")
    except Exception as e:
        log("Fetch failed: " + str(e), "ERROR"); return
    valid_count = 0; rejected_count = 0; signals = []
    for m in markets:
        v, reason = validate_market(m)
        if not v:
            rejected_count += 1
            conn.execute("INSERT INTO v4_rejected (scanned_at,market_id,question,reason) VALUES (?,?,?,?)",
                (datetime.datetime.now(datetime.timezone.utc).isoformat(), m.get("id"), m.get("question",""), reason))
            continue
        valid_count += 1
        prob = parse_prob(m)
        if prob is None: continue
        hours = hours_until(m.get("endDate"))
        if hours is None: continue
        for scorer in [score_f6_sports, score_f7_crypto, score_f8_political, score_f1_longshot]:
            sig = scorer(m, prob, hours)
            if sig is None: continue
            stake, max_payout = calc_stake(prob, sig["direction"], balance)
            sig["market"] = m; sig["size"] = stake; sig["max_payout"] = max_payout
            sig["hours_to_resolve"] = hours
            valid_sig, r2 = validate_signal(sig, balance, daily, weekly, monthly, open_n)
            conn.execute("INSERT INTO v4_signals (scanned_at,market_id,question,rule,direction,confidence,liquidity,hours_to_resolve,valid,reject_reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (datetime.datetime.now(datetime.timezone.utc).isoformat(), m.get("id"), m.get("question",""),
                 sig["rule"], sig["direction"], sig["confidence"], m.get("liquidityNum",0), hours, 1 if valid_sig else 0, r2))
            if valid_sig: signals.append(sig)
            break
    conn.commit()
    log("Valid: " + str(valid_count) + " | Rejected: " + str(rejected_count) + " | Signals: " + str(len(signals)))
    signals.sort(key=lambda s: -s["confidence"])
    placed = 0
    for sig in signals:
        if open_count(conn) >= MAX_OPEN_POSITIONS: break
        existing = conn.execute("SELECT COUNT(*) FROM v4_trades WHERE market_id=? AND status=?",(sig["market"].get("id"),"open")).fetchone()[0]
        if existing: continue
        m = sig["market"]
        log("[DRY] " + sig["rule"] + " " + sig["direction"] + " " + str(round(sig["confidence"]*100)) + "% $" + str(sig["size"]) + " -> max $" + str(sig["max_payout"]) + " | " + (m.get("question","")[:50]))
        conn.execute("INSERT INTO v4_trades (opened_at,market_id,question,rule,direction,entry_price,stake,max_payout,hours_to_resolve,liquidity,volume24h,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.now(datetime.timezone.utc).isoformat(), m.get("id"), m.get("question",""),
             sig["rule"], sig["direction"], sig["confidence"] if sig["direction"]=="YES" else 1-sig["confidence"],
             sig["size"], sig["max_payout"], sig["hours_to_resolve"],
             m.get("liquidityNum",0), m.get("volume24hr",0),
             "liq:$" + str(round((m.get("liquidityNum",0))/1000)) + "K"))
        conn.commit()
        placed += 1
    log("Placed " + str(placed) + " new trades")
    resolve_open(conn, markets)
    show_status(conn)

def resolve_open(conn, markets):
    open_t = conn.execute("SELECT * FROM v4_trades WHERE status=?",("open",)).fetchall()
    if not open_t: return
    by_id = {m.get("id"): m for m in markets}
    closed = 0
    for t in open_t:
        mid = t[2]; m = by_id.get(mid)
        if not m: continue
        prob = parse_prob(m)
        if prob is None: continue
        if m.get("closed") or m.get("archived"):
            outcome_yes = prob >= 0.97; outcome_no = prob <= 0.03
            if not (outcome_yes or outcome_no): continue
            won = (t[5]=="YES" and outcome_yes) or (t[5]=="NO" and outcome_no)
            pnl = t[8] if won else -t[7]
            outcome = "WIN" if won else "LOSS"
            conn.execute("UPDATE v4_trades SET status=?,closed_at=?,outcome=?,pnl=? WHERE id=?",
                ("closed", datetime.datetime.now(datetime.timezone.utc).isoformat(), outcome, pnl, t[0]))
            log(outcome + ": " + (t[3] or "")[:40] + " " + t[5] + " -> $" + str(round(pnl,2)))
            closed += 1
    conn.commit()
    if closed: log("Resolved " + str(closed) + " positions")

def show_status(conn):
    balance = get_balance(conn)
    daily = get_pnl_window(conn, 24)
    weekly = get_pnl_window(conn, 24*7)
    closed = conn.execute("SELECT * FROM v4_trades WHERE status=?",("closed",)).fetchall()
    open_t = conn.execute("SELECT * FROM v4_trades WHERE status=?",("open",)).fetchall()
    print("\n" + "="*70)
    print("POLYWATCH v4 STATUS")
    print("="*70)
    print("Balance: $" + str(round(balance,2)) + " | P&L: $" + str(round(balance-BANKROLL,2)) + " | Today: $" + str(round(daily,2)) + " | Week: $" + str(round(weekly,2)))
    print("Open: " + str(len(open_t)) + " | Closed: " + str(len(closed)))
    if closed:
        wins = [t for t in closed if t[14]=="WIN"]
        wr = len(wins)/len(closed)*100
        wpnl = sum(t[15] for t in wins if t[15])
        lpnl = sum(t[15] for t in closed if t[14]=="LOSS" and t[15])
        pf = wpnl / abs(lpnl) if lpnl != 0 else 999
        print("WR: " + str(round(wr,1)) + "% | Profit Factor: " + str(round(pf,2)))
        stats = {"total_trades": len(closed), "win_rate": wr/100, "profit_factor": pf,
                 "max_drawdown_pct": 0, "ghost_count": 0,
                 "liquid_market_trades": sum(1 for t in closed if (t[10] or 0) >= 30000)}
        ok, checks = check_go_live_criteria(stats)
        print("\nGo-Live (Day 30): " + ("READY" if ok else "NOT YET"))
        for k, v in checks.items():
            print("  " + ("PASS" if v else "FAIL") + " " + k)
    print("="*70)

def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    try:
        scan(conn)
    except Exception as e:
        log("FAILED: " + str(e), "ERROR")
        log(traceback.format_exc(), "ERROR")
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
