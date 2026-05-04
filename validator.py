"""
POLYWATCH VALIDATOR + SAFETY RULES
====================================
Apply BEFORE scoring any market against any rule.

Catches:
  - Aston Villa style ghost markets (closed=True but in active feeds)
  - Resolved markets (endDate < now)
  - Dead markets (low liquidity, no recent volume)
  - Degenerate prices (already at 0 or 1)

Plus: 8 hardcoded safety rules for capital preservation.
"""
import datetime, json


# === SAFETY RULES (cannot be overridden by signal logic) ===
BANKROLL_TARGET     = 10000.0  # planned $10k deployment
MAX_RISK_PER_TRADE  = 0.015    # 1.5% of bankroll = $150 max
MAX_OPEN_POSITIONS  = 4        # never >4 simultaneous
MAX_PAIR_EXPOSURE   = 600.0    # total at-risk capital cap

DAILY_STOP_LOSS     = 200.0    # halt trading for the day
WEEKLY_STOP_LOSS    = 500.0    # halt trading for the week
MONTHLY_CIRCUIT     = 1000.0   # halt all trading, full review

MIN_LIQUIDITY       = 30000.0  # min market liquidity (was $5k)
MIN_VOLUME_24H      = 5000.0   # recent trading activity
MIN_PAYOUT          = 5.0      # no dust trades
MIN_HOURS_TO_RESOLVE = 2       # too noisy if <2h
MAX_HOURS_TO_RESOLVE = 24*7    # capital tied up too long if >7d


# === MARKET VALIDATOR (ghost market filter) ===
def validate_market(m, min_liquidity=MIN_LIQUIDITY, min_volume=MIN_VOLUME_24H):
    """
    Returns (is_valid, reason).
    Run BEFORE scoring against any signal rule.
    """
    now = datetime.datetime.now(datetime.timezone.utc)

    # 1. closed flag
    if m.get("closed") is True:
        return False, "closed=True"

    # 2. archived flag
    if m.get("archived") is True:
        return False, "archived=True"

    # 3. endDate must exist and be in the future
    end_str = m.get("endDate") or m.get("endDateIso")
    if not end_str:
        return False, "no endDate"
    try:
        if "T" in end_str:
            end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            end_dt = datetime.datetime.fromisoformat(end_str + "T00:00:00+00:00")
        if end_dt <= now:
            return False, f"resolved {(now-end_dt).days}d ago"
    except Exception as e:
        return False, f"bad endDate: {e}"

    # 4. liquidity floor
    liq = m.get("liquidityNum") or 0
    if liq < min_liquidity:
        return False, f"low liquidity ${liq:.0f}"

    # 5. recent trading volume
    vol = m.get("volume24hr") or 0
    if vol < min_volume:
        return False, f"no recent volume ${vol:.0f}"

    # 6. accepting orders flag
    if m.get("acceptingOrders") is False:
        return False, "not accepting orders"

    # 7. valid prices, not degenerate
    prices = m.get("outcomePrices")
    if not prices:
        return False, "no prices"
    try:
        arr = json.loads(prices) if isinstance(prices, str) else prices
        p = float(arr[0])
        if p == 0.0 or p == 1.0:
            return False, f"degenerate price {p}"
    except:
        return False, "bad price format"

    return True, "OK"


# === SIGNAL VALIDATOR (apply after scoring) ===
def validate_signal(sig, current_balance, daily_pnl, weekly_pnl, monthly_pnl, open_count):
    """
    Returns (is_valid, reason). Applies the 8 hardcoded safety rules.
    Pass in the current balance, P&L stats, and open position count.
    """
    # Safety rule 3: Daily loss limit
    if daily_pnl <= -DAILY_STOP_LOSS:
        return False, f"daily stop hit ({daily_pnl:.2f})"

    # Safety rule 4: Weekly drawdown
    if weekly_pnl <= -WEEKLY_STOP_LOSS:
        return False, f"weekly stop hit ({weekly_pnl:.2f})"

    # Safety rule 5: Monthly circuit breaker
    if monthly_pnl <= -MONTHLY_CIRCUIT:
        return False, f"monthly circuit hit ({monthly_pnl:.2f})"

    # Safety rule 2: Max open positions
    if open_count >= MAX_OPEN_POSITIONS:
        return False, f"max positions ({open_count}/{MAX_OPEN_POSITIONS})"

    # Safety rule 1: Risk per trade cap
    risk_dollars = current_balance * MAX_RISK_PER_TRADE
    stake = sig.get("size") or sig.get("stake") or 0
    if stake > risk_dollars:
        return False, f"stake ${stake:.2f} > max ${risk_dollars:.2f}"

    # Min payout check
    payout = sig.get("max_payout") or 0
    if payout < MIN_PAYOUT:
        return False, f"payout ${payout:.2f} below ${MIN_PAYOUT}"

    # Resolution time check
    hours = sig.get("hours_to_resolve")
    if hours is not None:
        if hours < MIN_HOURS_TO_RESOLVE:
            return False, f"resolves in {hours:.1f}h (too soon)"
        if hours > MAX_HOURS_TO_RESOLVE:
            return False, f"resolves in {hours:.0f}h (>{MAX_HOURS_TO_RESOLVE}h cap)"

    return True, "OK"


# === STRICT RULE THRESHOLDS (replace previous) ===
STRICT_RULES = {
    "F6_sports": {
        "min_conf": 0.70, "max_conf": 0.90,
        "max_spread": 0.010, "min_liquidity": 50000,
        "min_hours": 2, "max_hours": 24,
        "priority": 1,
    },
    "F7_crypto": {
        "yes_range": (0.90, 0.99), "no_range": (0.01, 0.10),
        "max_spread": 0.005, "min_liquidity": 50000,
        "max_hours": 30,
        "priority": 2,
    },
    "F8_political": {
        "yes_range": (0.92, 1.00), "no_range": (0.00, 0.08),
        "min_liquidity": 100000,
        "max_hours": 24,
        "priority": 3,
    },
    "F1_longshot_no": {
        "min_entry": 0.05,
        "min_liquidity": 30000,
        "min_hours": 24, "max_hours": 24*60,
        "priority": 4,
    },
    "F3_fomc": {
        "min_conf": 0.80, "max_conf": 0.95,
        "min_liquidity": 100000,
        "max_hours": 24*14,
        "priority": 5,
    },
}


# === GO/NO-GO CRITERIA ===
def check_go_live_criteria(stats):
    """
    Day 30 decision. ALL 5 must be True to deploy real capital.
    stats dict needs: total_trades, win_rate, profit_factor, max_drawdown_pct,
    ghost_count, liquid_market_trades
    """
    checks = {
        "win_rate >= 70%":     stats.get("win_rate", 0) >= 0.70,
        "profit_factor >= 1.5": stats.get("profit_factor", 0) >= 1.5,
        "max_dd < 15%":         stats.get("max_drawdown_pct", 1) < 0.15,
        "zero ghosts":          stats.get("ghost_count", 999) == 0,
        "min 50 liquid trades": stats.get("liquid_market_trades", 0) >= 50,
        "min 80 closed trades": stats.get("total_trades", 0) >= 80,
    }
    all_pass = all(checks.values())
    return all_pass, checks


if __name__ == "__main__":
    # Quick self-test
    import urllib.request
    HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    print("Testing validator on live API...")
    url = "https://gamma-api.polymarket.com/markets?limit=20&active=true&closed=false&order=volume24hr&ascending=false"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        markets = json.loads(r.read())
    p = f = 0
    for m in markets:
        v, reason = validate_market(m)
        if v: p += 1
        else:
            f += 1
            print(f"  REJECT: {reason} | {m.get('question','')[:50]}")
          print(f"\n{p} valid, {f} rejected")
