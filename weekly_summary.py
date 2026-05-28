#!/usr/bin/env python3
"""POLYWATCH WEEKLY SUMMARY
Generates a comprehensive weekly P&L and performance report.
Runs every Friday at 18:00 UTC via GitHub Actions.
Posts as a GitHub Issue for easy review."""
import os, sys, sqlite3, datetime, json

DB_PATH = os.environ.get("POLYWATCH_DB", "polywatch.db")
BANKROLL_TARGET = 10000.0

def get_pnl_since(conn, hours):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
    return conn.execute(
        "SELECT COALESCE(SUM(pnl),0) FROM v4_trades WHERE status='closed' AND closed_at>?",
        (cutoff,)
    ).fetchone()[0]

def get_trades_since(conn, hours):
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
    return list(conn.execute(
        "SELECT id, opened_at, market_id, question, rule, direction, entry_price, stake, max_payout, hours_to_resolve, liquidity, volume24h, status, closed_at, outcome, pnl, notes FROM v4_trades WHERE status='closed' AND closed_at>? ORDER BY closed_at DESC",
        (cutoff,)
    ).fetchall())

def main():
    if not os.path.exists(DB_PATH):
        print("# Polywatch Weekly Summary\n\nNo database found yet. Bot just deployed.")
        return

    conn = sqlite3.connect(DB_PATH)
    now = datetime.datetime.now(datetime.timezone.utc)
    week_str = now.strftime("%b %d, %Y")

    # Pull all the data we need
    all_closed = list(conn.execute("SELECT outcome, pnl, rule, stake, max_payout, liquidity FROM v4_trades WHERE status='closed'"))
    all_open = list(conn.execute("SELECT rule, direction, question, max_payout, hours_to_resolve FROM v4_trades WHERE status='open'"))

    week_trades = get_trades_since(conn, 24*7)
    week_pnl = get_pnl_since(conn, 24*7)
    week_wins = [t for t in week_trades if t[14]=="WIN"]
    week_losses = [t for t in week_trades if t[14]=="LOSS"]

    # All-time
    total_pnl = sum(c[1] for c in all_closed if c[1])
    balance = BANKROLL_TARGET + total_pnl
    all_wins = [c for c in all_closed if c[0]=="WIN"]
    all_losses = [c for c in all_closed if c[0]=="LOSS"]
    wr = len(all_wins)/len(all_closed)*100 if all_closed else 0
    wpnl = sum(c[1] for c in all_wins if c[1])
    lpnl = sum(c[1] for c in all_losses if c[1])
    pf = wpnl/abs(lpnl) if lpnl else 0
    avg_win = wpnl/len(all_wins) if all_wins else 0
    avg_loss = lpnl/len(all_losses) if all_losses else 0
    breakeven_wr = abs(avg_loss)/(avg_win+abs(avg_loss))*100 if (avg_win+abs(avg_loss))>0 else 0
    edge = wr - breakeven_wr

    # Per-rule
    rules = {}
    for c in all_closed:
        r = c[2] or "unknown"
        if r not in rules: rules[r] = {"w":0,"l":0,"pnl":0}
        if c[0]=="WIN": rules[r]["w"] += 1
        else: rules[r]["l"] += 1
        rules[r]["pnl"] += c[1] or 0

    # Build markdown report
    out = []
    out.append(f"# 📊 POLYWATCH WEEKLY SUMMARY — {week_str}\n")
    out.append(f"_Generated automatically every Friday at 18:00 UTC_\n\n---\n")

    out.append(f"## 💰 Headline Numbers\n")
    out.append(f"| Metric | Value |")
    out.append(f"|---|---|")
    out.append(f"| **Balance** | ${balance:,.2f} |")
    out.append(f"| **All-time P&L** | ${total_pnl:+.2f} ({total_pnl/BANKROLL_TARGET*100:+.2f}%) |")
    out.append(f"| **This week P&L** | ${week_pnl:+.2f} |")
    out.append(f"| **Trades this week** | {len(week_trades)} ({len(week_wins)}W / {len(week_losses)}L) |")
    out.append(f"| **Win rate (all-time)** | {wr:.1f}% |")
    out.append(f"| **Profit factor** | {pf:.2f} |")
    out.append(f"| **Edge** | {edge:+.1f}% (breakeven WR: {breakeven_wr:.1f}%) |\n")

    if edge < 0:
        out.append(f"⚠️ **Edge is NEGATIVE** — current avg_win/avg_loss requires {breakeven_wr:.1f}% WR to break even. Currently at {wr:.1f}%.\n")
    elif edge > 0:
        out.append(f"✅ **Edge is POSITIVE** — strategy is profitable long-term at current performance.\n")

    out.append(f"\n## 📋 This Week's Trades ({len(week_trades)} closed)\n")
    if week_trades:
        out.append(f"| Day | Rule | Dir | Outcome | P&L | Market |")
        out.append(f"|---|---|---|---|---|---|")
        for t in week_trades[:20]:
            try:
                d = datetime.datetime.fromisoformat(t[13]).strftime("%a %m/%d")
            except:
                d = "?"
            icon = "✅" if t[14]=="WIN" else "❌"
            q = (t[3] or "")[:60]
            out.append(f"| {d} | {t[4]} | {t[5]} | {icon} {t[14]} | ${t[15]:+.2f} | {q} |")
        if len(week_trades) > 20:
            out.append(f"\n_+{len(week_trades)-20} more trades not shown_")
    else:
        out.append(f"_No trades resolved this week._")

    out.append(f"\n## 🎯 Rule Performance (All-Time)\n")
    out.append(f"| Rule | W/L | WR | P&L | Status |")
    out.append(f"|---|---|---|---|---|")
    for r in sorted(rules.keys()):
        d = rules[r]
        t = d["w"] + d["l"]
        rwr = d["w"]/t*100 if t else 0
        status = "✅ Profitable" if d["pnl"] > 0 else "❌ Losing"
        out.append(f"| `{r}` | {d['w']}/{d['l']} | {rwr:.0f}% | ${d['pnl']:+.2f} | {status} |")

    out.append(f"\n## 🟡 Open Positions ({len(all_open)})\n")
    if all_open:
        out.append(f"| Rule | Dir | Max Payout | Resolves | Market |")
        out.append(f"|---|---|---|---|---|")
        for r, d, q, p, h in all_open:
            h_str = f"{h:.0f}h" if h else "?"
            out.append(f"| {r} | {d} | +${p:.2f} | {h_str} | {(q or '')[:60]} |")
    else:
        out.append(f"_No open positions._")

    # Go-live readiness
    out.append(f"\n## 🏁 Go-Live Readiness\n")
    out.append(f"_Target deployment: July 1, 2026 (extended from June 1 for more data)_\n")
    days_to_golive = (datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc) - now).days
    out.append(f"- **Days until decision:** {days_to_golive}")
    out.append(f"- **Closed trades:** {len(all_closed)} / 80 needed ({len(all_closed)/80*100:.0f}%)")
    out.append(f"- **Win rate:** {wr:.1f}% (need ≥70%) — {'✅ PASS' if wr >= 70 else '❌ FAIL'}")
    out.append(f"- **Profit factor:** {pf:.2f} (need ≥1.5) — {'✅ PASS' if pf >= 1.5 else '❌ FAIL'}")
    out.append(f"- **Edge:** {edge:+.1f}% — {'✅ POSITIVE' if edge > 0 else '❌ NEGATIVE'}\n")

    # Action items
    out.append(f"## 💡 Auto-Generated Insights\n")
    if not all_closed:
        out.append(f"- Strategy too new to evaluate (0 trades closed)")
    else:
        if wr >= 70 and edge > 0:
            out.append(f"- ✅ Strategy showing positive edge with {len(all_closed)} trades")
        if wr < 70:
            out.append(f"- ⚠️ Win rate below 70% threshold ({wr:.1f}%)")
        if edge < -5:
            out.append(f"- ⚠️ Edge significantly negative ({edge:+.1f}%) — strategy losing money long-term")
        if len(all_closed) < 30:
            out.append(f"- ⏳ Sample size still small ({len(all_closed)} trades) — keep collecting data")
        # Best/worst rule
        if rules:
            best = max(rules.items(), key=lambda x: x[1]["pnl"])
            worst = min(rules.items(), key=lambda x: x[1]["pnl"])
            out.append(f"- 🏆 Best rule: **{best[0]}** (${best[1]['pnl']:+.2f}, {best[1]['w']}/{best[1]['w']+best[1]['l']})")
            if worst[1]["pnl"] < 0:
                out.append(f"- 🔻 Worst rule: **{worst[0]}** (${worst[1]['pnl']:+.2f}, {worst[1]['w']}/{worst[1]['w']+worst[1]['l']})")

    out.append(f"\n---\n_Next summary: Friday, {(now + datetime.timedelta(days=7)).strftime('%b %d')}_")
    out.append(f"\n_v6.0 CRYPTO-ONLY • 8 max positions • F7/F9/F12 rules • Run hourly at :15 UTC_")

    report = "\n".join(out)
    print(report)

    # Write to file for GitHub Action to pick up
    with open("/tmp/weekly_summary.md", "w") as f:
        f.write(report)
    print("\n[wrote /tmp/weekly_summary.md]")

if __name__ == "__main__":
    main()
