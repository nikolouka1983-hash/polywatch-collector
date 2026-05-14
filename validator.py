"""
POLYWATCH VALIDATOR + SAFETY RULES
====================================
Apply BEFORE scoring any market. Catches ghost markets + enforces 8 safety rules.
"""
import datetime, json

BANKROLL_TARGET      = 10000.0
MAX_RISK_PER_TRADE   = 0.015
MAX_OPEN_POSITIONS   = 4
MAX_PAIR_EXPOSURE    = 600.0
DAILY_STOP_LOSS      = 200.0
WEEKLY_STOP_LOSS     = 500.0
MONTHLY_CIRCUIT      = 1000.0
MIN_LIQUIDITY        = 30000.0
MIN_VOLUME_24H       = 5000.0
MIN_PAYOUT           = 5.0
MIN_HOURS_TO_RESOLVE = 2
MAX_HOURS_TO_RESOLVE = 24 * 7

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
        "min_hours": 24, "max_hours": 24 * 60,
        "priority": 4,
    },
    "F3_fomc": {
        "min_conf": 0.80, "max_conf": 0.95,
        "min_liquidity": 100000,
        "max_hours": 24 * 14,
        "priority": 5,
    },
}


def validate_market(m, min_liquidity=MIN_LIQUIDITY, min_volume=MIN_VOLUME_24H):
    """Returns (is_valid, reason). Run BEFORE scoring any market."""
    now = datetime.datetime.now(datetime.timezone.utc)
    if m.get("closed") is True:
        return False, "closed=True"
    if m.get("archived") is True:
        return False, "archived=True"
    end_str = m.get("endDate") or m.get("endDateIso")
    if not end_str:
        return False, "no endDate"
    try:
        if "T" in end_str:
            end_dt = datetime.datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        else:
            end_dt = datetime.datetime.fromisoformat(end_str + "T00:00:00+00:00")
        if end_dt <= now:
            return False, "resolved " + str((now - end_dt).days) + "d ago"
    except Exception as e:
        return False, "bad endDate: " + str(e)
    liq = m.get("liquidityNum") or 0
    if liq < min_liquidity:
        return False, "low liquidity $" + str(round(liq))
    vol = m.get("volume24hr") or 0
    if vol < min_volume:
        return False, "no recent volume $" + str(round(vol))
    if m.get("acceptingOrders") is False:
        return False, "not accepting orders"
    prices = m.get("outcomePrices")
    if not prices:
        return False, "no prices"
    try:
        arr = json.loads(prices) if isinstance(prices, str) else prices
        p = float(arr[0])
        if p == 0.0 or p == 1.0:
            return False, "degenerate price " + str(p)
    except Exception:
        return False, "bad price format"
    return True, "OK"


def validate_signal(sig, current_balance, daily_pnl, weekly_pnl, monthly_pnl, open_count):
    """Returns (is_valid, reason). Applies the 8 hardcoded safety rules."""
    if daily_pnl <= -DAILY_STOP_LOSS:
        return False, "daily stop hit (" + str(round(daily_pnl, 2)) + ")"
    if weekly_pnl <= -WEEKLY_STOP_LOSS:
        return False, "weekly stop hit (" + str(round(weekly_pnl, 2)) + ")"
    if monthly_pnl <= -MONTHLY_CIRCUIT:
        return False, "monthly circuit hit (" + str(round(monthly_pnl, 2)) + ")"
    if open_count >= MAX_OPEN_POSITIONS:
        return False, "max positions (" + str(open_count) + "/" + str(MAX_OPEN_POSITIONS) + ")"
    stake = sig.get("size") or sig.get("stake") or 0
    max_stake = current_balance * MAX_RISK_PER_TRADE
    # Allow 1 cent tolerance for float rounding (calc_stake rounds to 2 decimals)
    if stake > max_stake + 0.01:
        return False, "stake $" + str(round(stake, 2)) + " > max $" + str(round(max_stake, 2))
    payout = sig.get("max_payout") or 0
    if payout < MIN_PAYOUT:
        return False, "payout $" + str(round(payout, 2)) + " below $" + str(MIN_PAYOUT)
    hours = sig.get("hours_to_resolve")
    if hours is not None:
        if hours < MIN_HOURS_TO_RESOLVE:
            return False, "resolves in " + str(round(hours, 1)) + "h (too soon)"
        if hours > MAX_HOURS_TO_RESOLVE:
            return False, "resolves in " + str(round(hours)) + "h (cap exceeded)"
    return True, "OK"


def check_go_live_criteria(stats):
    """Day 30 decision. ALL must be True to deploy real capital."""
    checks = {
        "win_rate >= 70%":      stats.get("win_rate", 0) >= 0.70,
        "profit_factor >= 1.5": stats.get("profit_factor", 0) >= 1.5,
        "max_dd < 15%":         stats.get("max_drawdown_pct", 1) < 0.15,
        "zero ghosts":          stats.get("ghost_count", 999) == 0,
        "min 50 liquid trades": stats.get("liquid_market_trades", 0) >= 50,
        "min 80 closed trades": stats.get("total_trades", 0) >= 80,
    }
    return all(checks.values()), checks
