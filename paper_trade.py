#!/usr/bin/env python3
"""
PAPER TRADE LOGGER
==================
Log hypothetical trades based on signals from the dashboard.
Run alongside the collector to build a performance dataset.

Usage:
  python3 paper_trade.py add     -- add a new paper trade
  python3 paper_trade.py list    -- list open trades
  python3 paper_trade.py resolve -- resolve open trades against current prices
  python3 paper_trade.py stats   -- show performance stats
"""

import sqlite3
import json
import sys
import datetime
import urllib.request

DB_PATH = "polywatch.db"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_trade():
    print("\n=== ADD PAPER TRADE ===")
    print("(This logs a hypothetical trade based on a live signal)\n")

    market_id = input("Market ID (from Polymarket): ").strip()
    question  = input("Market question (short desc): ").strip()
    direction = input("Direction (YES/NO): ").strip().upper()
    signal    = input("Signal type (spike/high_conf/arb/moving/manual): ").strip()

    # Fetch current price
    try:
        data = fetch(f"https://gamma-api.polymarket.com/markets/{market_id}")
        prices = json.loads(data.get("outcomePrices","[0,1]"))
        yes_prob = float(prices[0])
        vol24 = data.get("volume24hr", 0)
        print(f"\nLive data: YES={yes_prob*100:.1f}% | Vol24h=${vol24/1000:.0f}K")
    except Exception as e:
        print(f"Could not fetch live price: {e}")
        yes_prob = float(input("Enter current YES probability (0-1): "))
        vol24 = float(input("Enter 24h volume: ") or "0")

    entry_prob = yes_prob if direction == "YES" else (1 - yes_prob)
    print(f"\nEntry probability: {entry_prob*100:.1f}% ({direction})")
    confirm = input("Confirm trade? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    conn = get_conn()
    conn.execute("""
        INSERT INTO paper_trades
        (created_at, market_id, question, direction, entry_prob, entry_vol24h, signal_type)
        VALUES (?,?,?,?,?,?,?)
    """, (
        datetime.datetime.utcnow().isoformat(),
        market_id, question, direction, entry_prob, vol24, signal
    ))
    conn.commit()
    conn.close()
    print(f"✓ Paper trade logged: {direction} on '{question[:50]}' at {entry_prob*100:.1f}%")


def list_trades():
    conn = get_conn()
    open_trades = conn.execute("""
        SELECT id, created_at, market_id, question, direction, entry_prob, signal_type
        FROM paper_trades WHERE resolved=0
        ORDER BY created_at DESC
    """).fetchall()

    if not open_trades:
        print("No open paper trades.")
        conn.close()
        return

    print(f"\n{'ID':>4} {'Created':>20} {'Dir':>4} {'Entry%':>7} {'Signal':>12}  Question")
    print("-" * 100)
    for t in open_trades:
        print(f"{t['id']:>4} {t['created_at'][:19]:>20} {t['direction']:>4} "
              f"{t['entry_prob']*100:>6.1f}% {t['signal_type'] or '':>12}  {t['question'][:55]}")
    conn.close()


def resolve_trades():
    conn = get_conn()
    open_trades = conn.execute("""
        SELECT * FROM paper_trades WHERE resolved=0
    """).fetchall()

    if not open_trades:
        print("No open trades to resolve.")
        conn.close()
        return

    print(f"\nChecking {len(open_trades)} open trades against live prices...")

    for t in open_trades:
        try:
            data = fetch(f"https://gamma-api.polymarket.com/markets/{t['market_id']}")
            prices = json.loads(data.get("outcomePrices", "[0,1]"))
            yes_prob = float(prices[0])
            is_closed = data.get("closed", False)

            exit_prob = yes_prob if t["direction"] == "YES" else (1 - yes_prob)

            # Determine outcome
            if is_closed or yes_prob >= 0.98 or yes_prob <= 0.02:
                # Market resolved
                if yes_prob >= 0.98:
                    resolved_yes = True
                elif yes_prob <= 0.02:
                    resolved_yes = False
                else:
                    resolved_yes = None

                if resolved_yes is None:
                    outcome = "PUSH"
                elif (t["direction"] == "YES" and resolved_yes) or \
                     (t["direction"] == "NO" and not resolved_yes):
                    outcome = "WIN"
                else:
                    outcome = "LOSS"

                # Simple P&L: if WIN, profit = (1 - entry_prob) / entry_prob
                # if LOSS, loss = -1 (lose stake)
                if outcome == "WIN":
                    pnl = (1 - t["entry_prob"]) / t["entry_prob"]
                elif outcome == "LOSS":
                    pnl = -1.0
                else:
                    pnl = 0.0

                conn.execute("""
                    UPDATE paper_trades
                    SET resolved=1, outcome=?, exit_prob=?, pnl_pct=?
                    WHERE id=?
                """, (outcome, exit_prob, pnl, t["id"]))

                print(f"  [{outcome}] {t['question'][:55]}")
                print(f"       Entry: {t['entry_prob']*100:.1f}% → Exit: {exit_prob*100:.1f}% | P&L: {pnl*100:+.0f}%")
            else:
                print(f"  [OPEN] {t['question'][:55]} — currently {yes_prob*100:.1f}% YES")

        except Exception as e:
            print(f"  [ERROR] {t['question'][:40]} — {e}")

    conn.commit()
    conn.close()
    print("\nDone. Run 'stats' to see performance.")


def show_stats():
    conn = get_conn()

    all_trades = conn.execute("SELECT * FROM paper_trades ORDER BY created_at").fetchall()
    resolved = [t for t in all_trades if t["resolved"]]
    open_t   = [t for t in all_trades if not t["resolved"]]

    print(f"\n{'='*50}")
    print("PAPER TRADE PERFORMANCE")
    print(f"{'='*50}")
    print(f"Total trades:  {len(all_trades)}")
    print(f"Resolved:      {len(resolved)}")
    print(f"Still open:    {len(open_t)}")

    if resolved:
        wins   = [t for t in resolved if t["outcome"] == "WIN"]
        losses = [t for t in resolved if t["outcome"] == "LOSS"]
        pushes = [t for t in resolved if t["outcome"] == "PUSH"]
        win_rate = len(wins) / len(resolved) * 100
        avg_pnl  = sum(t["pnl_pct"] or 0 for t in resolved) / len(resolved)
        total_pnl = sum(t["pnl_pct"] or 0 for t in resolved)

        print(f"\nWin rate:      {win_rate:.1f}%  ({len(wins)}W / {len(losses)}L / {len(pushes)}P)")
        print(f"Avg P&L/trade: {avg_pnl*100:+.1f}%")
        print(f"Total P&L:     {total_pnl*100:+.0f}% (on equal-stake basis)")

        # By signal type
        print("\nBy signal type:")
        signal_types = set(t["signal_type"] for t in resolved if t["signal_type"])
        for sig in signal_types:
            st = [t for t in resolved if t["signal_type"] == sig]
            sw = [t for t in st if t["outcome"] == "WIN"]
            print(f"  {sig:15}: {len(sw)}/{len(st)} wins ({len(sw)/len(st)*100:.0f}%)")

        print("\nAll resolved trades:")
        print(f"  {'Dir':>4} {'Entry%':>7} {'Exit%':>6} {'P&L':>7} {'Outcome':>7}  Question")
        print("  " + "-" * 80)
        for t in resolved:
            print(f"  {t['direction']:>4} {t['entry_prob']*100:>6.1f}% "
                  f"{(t['exit_prob'] or 0)*100:>5.1f}% "
                  f"{(t['pnl_pct'] or 0)*100:>+6.0f}% "
                  f"{t['outcome']:>7}  {t['question'][:50]}")

    conn.close()


COMMANDS = {
    "add":     add_trade,
    "list":    list_trades,
    "resolve": resolve_trades,
    "stats":   show_stats,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        print(f"Unknown command: {cmd}")
        print(f"Usage: python3 paper_trade.py [{' | '.join(COMMANDS)}]")
