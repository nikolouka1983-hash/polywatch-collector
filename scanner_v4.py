#!/usr/bin/env python3
"""POLYWATCH SCANNER v6.1 - CRYPTO ONLY
Max 48h. Crypto markets only. 12 concurrent positions.
Rules: F7 (binary above/below), F9 (up/down daily), F12 (level markets)
Killed: F6, F8, F10, F11, F13, F14 (all non-crypto + sports = no edge)
v6.1: F9 prioritized in signal sort (proven edge source), max positions 8→12"""
import os,sys,json,sqlite3,datetime,urllib.request,traceback,time
try:
    from validator import(validate_market,validate_signal,check_go_live_criteria,STRICT_RULES,BANKROLL_TARGET,MAX_RISK_PER_TRADE)
except ImportError:
    print("validator.py missing");sys.exit(1)
DB_PATH=os.environ.get("POLYWATCH_DB","polywatch.db")
LOG_FILE="scanner_v4.log"
GAMMA="https://gamma-api.polymarket.com"
HEADERS={"User-Agent":"Mozilla/5.0","Accept":"application/json"}
BANKROLL=float(os.environ.get("BANKROLL",BANKROLL_TARGET))
MIN_PAYOUT_HARD=5.0
MAX_HOURS=48
# v6.1: Increased 8→12 to capture more F9 signals (F9 is the true edge source)
MAX_OPEN_POSITIONS=12

# Expanded crypto coverage — most liquid coins on Polymarket
CRYPTO_KEYWORDS=["bitcoin","btc","ethereum","eth","solana","sol ","xrp","ripple","dogecoin","doge","cardano","ada","binance coin","bnb","avalanche","avax","chainlink","link","polkadot","dot","polygon","matic","litecoin","ltc","sui ","near","tron","trx","hyperliquid","hype","stellar","xlm","cosmos","atom","aptos","apt","arbitrum","arb","optimism","op ","aave","uniswap","uni "]

def log(msg,level="INFO"):
    ts=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line="["+ts+"] ["+level+"] "+msg
    print(line)
    with open(LOG_FILE,"a") as f: f.write(line+"\n")

def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS v4_trades(id INTEGER PRIMARY KEY AUTOINCREMENT,opened_at TEXT,market_id TEXT,question TEXT,rule TEXT,direction TEXT,entry_price REAL,stake REAL,max_payout REAL,hours_to_resolve REAL,liquidity REAL,volume24h REAL,status TEXT DEFAULT 'open',closed_at TEXT,outcome TEXT,pnl REAL,notes TEXT);
    CREATE TABLE IF NOT EXISTS v4_signals(id INTEGER PRIMARY KEY AUTOINCREMENT,scanned_at TEXT,market_id TEXT,question TEXT,rule TEXT,direction TEXT,confidence REAL,liquidity REAL,hours_to_resolve REAL,valid INTEGER,reject_reason TEXT,acted INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS v4_rejected(id INTEGER PRIMARY KEY AUTOINCREMENT,scanned_at TEXT,market_id TEXT,question TEXT,reason TEXT);
    """)
    conn.commit()

def fetch_markets():
    all_m=[]
    for offset in [0,500,1000,1500]:
        try:
            url=GAMMA+"/markets?limit=500&active=true&closed=false&order=volume24hr&ascending=false&offset="+str(offset)
            req=urllib.request.Request(url,headers=HEADERS)
            with urllib.request.urlopen(req,timeout=20) as r: all_m.extend(json.loads(r.read()))
        except: pass
    return all_m

def fetch_market_by_id(market_id):
    try:
        url=GAMMA+"/markets/"+str(market_id)
        req=urllib.request.Request(url,headers=HEADERS)
        with urllib.request.urlopen(req,timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None

def parse_prob(m):
    try:
        arr=json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"),str) else m.get("outcomePrices")
        return float(arr[0])
    except: return None

def hours_until(d):
    if not d: return None
    try:
        end=datetime.datetime.fromisoformat(d.replace("Z","+00:00"))
        return(end-datetime.datetime.now(datetime.timezone.utc)).total_seconds()/3600
    except: return None

def get_balance(conn):
    pnl=conn.execute("SELECT COALESCE(SUM(pnl),0) FROM v4_trades WHERE status=?",("closed",)).fetchone()[0]
    return BANKROLL+pnl

def get_pnl_window(conn,hours):
    cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=hours)).isoformat()
    return conn.execute("SELECT COALESCE(SUM(pnl),0) FROM v4_trades WHERE status=? AND closed_at>?",("closed",cutoff)).fetchone()[0]

def open_count(conn):
    return conn.execute("SELECT COUNT(*) FROM v4_trades WHERE status=?",("open",)).fetchone()[0]

def calc_stake(prob,direction,balance):
    stake=balance*MAX_RISK_PER_TRADE
    entry=prob if direction=="YES" else(1-prob)
    if entry<=0 or entry>=1: return 0,0
    payout=round((stake/entry)*(1-entry),2)
    if payout<MIN_PAYOUT_HARD: return 0,0
    return round(stake,2),payout

# === CRYPTO RULES ONLY ===

def score_f7_crypto(m,prob,hours):
    """High-confidence binary BTC/ETH above/below price. 90%+ confident."""
    q=m.get("question","").lower()
    if not any(x in q for x in["bitcoin above","btc above","btc below","bitcoin below","ethereum above","eth above","ethereum below","eth below"]): return None
    if hours>30 or(m.get("liquidityNum") or 0)<30000 or(m.get("spread") or 1.0)>0.010: return None
    if prob>=0.90: return{"rule":"F7_crypto","direction":"YES","confidence":prob}
    if prob<=0.10: return{"rule":"F7_crypto","direction":"NO","confidence":1-prob}
    return None

def score_f9_crypto_updown(m,prob,hours):
    """Daily up/down direction. BTC/ETH 80%+ confident."""
    q=m.get("question","").lower()
    if "up or down" not in q or not any(x in q for x in["bitcoin","btc","ethereum","eth"]): return None
    if hours>24 or(m.get("liquidityNum") or 0)<20000 or(m.get("spread") or 1.0)>0.02: return None
    if prob>=0.80: return{"rule":"F9_crypto_updown","direction":"YES","confidence":prob}
    if prob<=0.20: return{"rule":"F9_crypto_updown","direction":"NO","confidence":1-prob}
    return None

def score_f12_crypto_level(m,prob,hours):
    """Crypto price level markets. Wide altcoin coverage. 78%+ confident."""
    q=m.get("question","").lower()
    if not any(x in q for x in CRYPTO_KEYWORDS): return None
    if "up or down" in q: return None
    if "above" not in q and "below" not in q and "reach" not in q and "exceed" not in q: return None
    if hours>48 or(m.get("liquidityNum") or 0)<10000 or(m.get("spread") or 1.0)>0.015: return None
    if prob>=0.78: return{"rule":"F12_crypto_level","direction":"YES","confidence":prob}
    if prob<=0.22: return{"rule":"F12_crypto_level","direction":"NO","confidence":1-prob}
    return None

SCORERS=[score_f7_crypto,score_f9_crypto_updown,score_f12_crypto_level]


def scan(conn):
    balance=get_balance(conn);daily=get_pnl_window(conn,24);weekly=get_pnl_window(conn,24*7);monthly=get_pnl_window(conn,24*30);open_n=open_count(conn)
    log("v6.0 CRYPTO-ONLY Balance: $"+str(round(balance,2))+" | Open: "+str(open_n)+"/"+str(MAX_OPEN_POSITIONS))
    log("P&L day="+str(round(daily,2))+" week="+str(round(weekly,2))+" month="+str(round(monthly,2)))
    resolve_open(conn)
    try:
        markets=fetch_markets();log("Fetched "+str(len(markets))+" markets")
    except Exception as e:
        log("Fetch failed: "+str(e),"ERROR");return
    valid_count=0;rejected_count=0;dust_count=0;signals=[]
    for m in markets:
        h=hours_until(m.get("endDate"))
        if h is None or h>MAX_HOURS: continue
        v,reason=validate_market(m)
        if not v:
            rejected_count+=1
            conn.execute("INSERT INTO v4_rejected(scanned_at,market_id,question,reason)VALUES(?,?,?,?)",(datetime.datetime.now(datetime.timezone.utc).isoformat(),m.get("id"),m.get("question",""),reason))
            continue
        valid_count+=1
        prob=parse_prob(m)
        if prob is None: continue
        for scorer in SCORERS:
            sig=scorer(m,prob,h)
            if sig is None: continue
            stake,max_payout=calc_stake(prob,sig["direction"],balance)
            if stake==0:
                dust_count+=1
                conn.execute("INSERT INTO v4_rejected(scanned_at,market_id,question,reason)VALUES(?,?,?,?)",(datetime.datetime.now(datetime.timezone.utc).isoformat(),m.get("id"),m.get("question",""),"payout <$5 DUST"))
                break
            sig["market"]=m;sig["size"]=stake;sig["max_payout"]=max_payout;sig["hours_to_resolve"]=h
            valid_sig,r2=validate_signal(sig,balance,daily,weekly,monthly,open_n)
            conn.execute("INSERT INTO v4_signals(scanned_at,market_id,question,rule,direction,confidence,liquidity,hours_to_resolve,valid,reject_reason)VALUES(?,?,?,?,?,?,?,?,?,?)",(datetime.datetime.now(datetime.timezone.utc).isoformat(),m.get("id"),m.get("question",""),sig["rule"],sig["direction"],sig["confidence"],m.get("liquidityNum",0),h,1 if valid_sig else 0,r2))
            if valid_sig: signals.append(sig)
            break
    conn.commit()
    log("Valid:"+str(valid_count)+" | Ghost:"+str(rejected_count)+" | Dust:"+str(dust_count)+" | Signals:"+str(len(signals)))
    # v6.1: Sort priority — F9 first (proven edge +2.2%), then F7 (high-conf), then F12
    # Within each rule, sort by max_payout descending (highest EV first)
    rule_priority={"F9_crypto_updown":0,"F7_crypto":1,"F12_crypto_level":2}
    signals.sort(key=lambda s:(rule_priority.get(s["rule"],99),-s["max_payout"]))
    placed=0
    for sig in signals:
        if open_count(conn)>=MAX_OPEN_POSITIONS: break
        if conn.execute("SELECT COUNT(*) FROM v4_trades WHERE market_id=? AND status=?",(sig["market"].get("id"),"open")).fetchone()[0]: continue
        m=sig["market"]
        log("[DRY] "+sig["rule"]+" "+sig["direction"]+" "+str(round(sig["confidence"]*100))+"% $"+str(sig["size"])+" -> +$"+str(sig["max_payout"])+" in "+str(round(sig["hours_to_resolve"],1))+"h | "+(m.get("question","")[:50]))
        conn.execute("INSERT INTO v4_trades(opened_at,market_id,question,rule,direction,entry_price,stake,max_payout,hours_to_resolve,liquidity,volume24h,notes)VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(datetime.datetime.now(datetime.timezone.utc).isoformat(),m.get("id"),m.get("question",""),sig["rule"],sig["direction"],sig["confidence"] if sig["direction"]=="YES" else 1-sig["confidence"],sig["size"],sig["max_payout"],sig["hours_to_resolve"],m.get("liquidityNum",0),m.get("volume24hr",0),"liq:$"+str(round((m.get("liquidityNum",0))/1000))+"K"))
        conn.commit();placed+=1
    log("Placed "+str(placed)+" new trades")
    show_status(conn)

def resolve_open(conn):
    """v5.1 resolver — query each open market_id directly to catch resolved markets."""
    open_t=conn.execute("SELECT * FROM v4_trades WHERE status=?",("open",)).fetchall()
    if not open_t:
        log("No open positions to check")
        return
    log("Checking "+str(len(open_t))+" open positions via direct fetch")
    closed=0
    errors=0
    for t in open_t:
        market_id=t[2]
        m=fetch_market_by_id(market_id)
        time.sleep(0.3)
        if m is None:
            errors+=1
            log("Could not fetch market "+str(market_id),"WARN")
            continue
        prob=parse_prob(m)
        if prob is None: continue
        is_done=m.get("closed") or m.get("archived")
        outcome_yes=prob>=0.97
        outcome_no=prob<=0.03
        if is_done and (outcome_yes or outcome_no):
            won=(t[5]=="YES" and outcome_yes) or (t[5]=="NO" and outcome_no)
            pnl=t[8] if won else-t[7]
            outcome="WIN" if won else"LOSS"
            conn.execute("UPDATE v4_trades SET status=?,closed_at=?,outcome=?,pnl=? WHERE id=?",("closed",datetime.datetime.now(datetime.timezone.utc).isoformat(),outcome,pnl,t[0]))
            log(outcome+": "+(t[3] or"")[:45]+" "+t[5]+" -> $"+str(round(pnl,2)))
            closed+=1
        elif is_done:
            log("STALE: "+(t[3] or "")[:45]+" closed but prob="+str(round(prob*100))+"%","WARN")
    conn.commit()
    if closed: log("Resolved "+str(closed)+" positions (errors:"+str(errors)+")")

def show_status(conn):
    balance=get_balance(conn)
    daily=get_pnl_window(conn,24)
    week=get_pnl_window(conn,24*7)
    closed=conn.execute("SELECT * FROM v4_trades WHERE status=?",("closed",)).fetchall()
    open_t=conn.execute("SELECT * FROM v4_trades WHERE status=?",("open",)).fetchall()
    print("\n"+"="*70);print("POLYWATCH v6.0 CRYPTO-ONLY STATUS");print("="*70)
    print("Balance: $"+str(round(balance,2))+" | P&L: $"+str(round(balance-BANKROLL,2)))
    print("Today:   $"+str(round(daily,2))+" | 7-day: $"+str(round(week,2)))
    print("Open: "+str(len(open_t))+"/"+str(MAX_OPEN_POSITIONS)+" | Closed: "+str(len(closed)))

    if not closed:
        print("="*70);return

    wins=[t for t in closed if t[14]=="WIN"]
    losses=[t for t in closed if t[14]=="LOSS"]
    wr=len(wins)/len(closed)*100 if closed else 0
    wpnl=sum(t[15] for t in wins if t[15])
    lpnl=sum(t[15] for t in losses if t[15])
    pf=wpnl/abs(lpnl) if lpnl else 999
    avg_win=wpnl/len(wins) if wins else 0
    avg_loss=lpnl/len(losses) if losses else 0
    breakeven_wr=abs(avg_loss)/(avg_win+abs(avg_loss))*100 if (avg_win+abs(avg_loss))>0 else 0
    edge=wr-breakeven_wr

    print("\n--- OVERALL ---")
    print("Trades:    "+str(len(closed))+" ("+str(len(wins))+"W / "+str(len(losses))+"L)")
    print("WR:        "+str(round(wr,1))+"%   PF: "+str(round(pf,2)))
    print("Avg win:   +$"+str(round(avg_win,2))+"   Avg loss: $"+str(round(avg_loss,2)))
    print("Breakeven: "+str(round(breakeven_wr,1))+"% WR needed | Edge: "+str(round(edge,1))+"%")

    cutoff_7d=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=24*7)).isoformat()
    week_closed=[t for t in closed if t[13] and t[13]>cutoff_7d]
    if week_closed:
        w7=[t for t in week_closed if t[14]=="WIN"]
        wr7=len(w7)/len(week_closed)*100
        pnl7=sum(t[15] for t in week_closed if t[15])
        print("\n--- LAST 7 DAYS ---")
        print("Trades: "+str(len(week_closed))+"  WR: "+str(round(wr7,1))+"%  P&L: $"+str(round(pnl7,2)))

    print("\n--- BY RULE ---")
    rules={}
    for t in closed:
        r=t[4] or "unknown"
        if r not in rules: rules[r]={"w":0,"l":0,"pnl":0}
        if t[14]=="WIN": rules[r]["w"]+=1
        else: rules[r]["l"]+=1
        rules[r]["pnl"]+=t[15] or 0
    print("  "+"Rule".ljust(20)+"  W/L      WR      P&L")
    for r in sorted(rules.keys()):
        d=rules[r];t=d["w"]+d["l"];wr_r=d["w"]/t*100 if t else 0
        marker="OK" if d["pnl"]>0 else "BAD"
        print("  "+r.ljust(20)+f'{d["w"]}/{d["l"]}'.ljust(8)+f'{wr_r:>5.0f}%'+f'  ${d["pnl"]:>+8.2f}  {marker}')

    stats={"total_trades":len(closed),"win_rate":wr/100,"profit_factor":pf,"max_drawdown_pct":0,"ghost_count":0,"liquid_market_trades":sum(1 for t in closed if(t[10] or 0)>=50000)}
    ok,checks=check_go_live_criteria(stats)
    print("\n--- GO-LIVE READINESS ---")
    print("Status: "+("READY" if ok else "NOT YET"))
    for k,v in checks.items(): print("  "+("PASS" if v else "FAIL")+" "+k)
    print("="*70)

def main():
    conn=sqlite3.connect(DB_PATH);init_db(conn)
    try: scan(conn)
    except Exception as e: log("FAILED: "+str(e),"ERROR");log(traceback.format_exc(),"ERROR");sys.exit(1)
    finally: conn.close()

if __name__=="__main__": main()
