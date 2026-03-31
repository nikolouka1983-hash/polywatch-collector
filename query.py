#!/usr/bin/env python3
"""
POLYWATCH QUERY TOOL
====================
Analyze the collected dataset. Run any time to explore patterns.

Usage:
  python3 query.py summary          -- weekly summary (paste into Claude)
  python3 query.py spikes           -- top volume spikes
  python3 query.py movers           -- biggest price movers
  python3 query.py highconf         -- high confidence markets
  python3 query.py resolved         -- recently resolved markets
  python3 query.py divergences      -- cross-platform divergences
  python3 query.py market <id>      -- full history for one market
  python3 query.py export           -- export full dataset to CSV
"""

import sqlite3
import sys
import csv
import io
import datetime
import os

DB_PATH = os.environ.get("POLYWATCH_DB", "polywatch.db")


def get_conn():
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        print("Run collector.py first to start collecting data.")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def cmd_summary():
    """Generate the weekly summary for pasting into Claude."""
    # Import and call from collector
    import importlib.util
    spec = importlib.util.spec_from_file_location("collector", "collector.py")
    col = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(col)
    conn = get_conn()
    col.generate_summary(conn)
    conn.close()


def cmd_spikes(n=20):
    conn = get_conn()
    now_iso = datetime.datetime.utcnow().isoformat()
    week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()

    print(f"\n{'='*70}")
    print(f"TOP {n} VOLUME SPIKES — last 7 days")
    print(f"{'='*70}")
    print(f"{'Ratio':>7} {'24hVol':>9} {'YesProb':>8} {'Hrs':>6}  Question")
    print("-" * 70)

    rows = conn.execute(f"""
        SELECT question, MAX(spike_ratio) as sr, MAX(volume_24h) as v24,
               AVG(yes_prob) as prob, MIN(hours_to_resolve) as hrs,
               COUNT(*) as snaps
        FROM snapshots
        WHERE captured_at > ?
        GROUP BY market_id
        ORDER BY sr DESC
        LIMIT ?
    """, (week_ago, n)).fetchall()

    for r in rows:
        hrs = f"{r['hrs']:.0f}h" if r['hrs'] is not None else "—"
        print(f"{r['sr']*100:>6.0f}% ${r['v24']/1000:>7.0f}K "
              f"{r['prob']*100:>7.0f}% {hrs:>6}  {r['question'][:52]}")
    conn.close()


def cmd_movers(n=20):
    conn = get_conn()
    week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()

    print(f"\n{'='*70}")
    print(f"BIGGEST PRICE MOVERS (1h change) — last 7 days")
    print(f"{'='*70}")
    print(f"{'MaxChg':>7} {'AvgProb':>8} {'24hVol':>9}  Question")
    print("-" * 70)

    rows = conn.execute(f"""
        SELECT question, MAX(ABS(chg_1h)) as max_chg,
               AVG(yes_prob) as prob, MAX(volume_24h) as v24,
               COUNT(*) as snaps
        FROM snapshots
        WHERE captured_at > ? AND chg_1h IS NOT NULL
        GROUP BY market_id
        ORDER BY max_chg DESC
        LIMIT ?
    """, (week_ago, n)).fetchall()

    for r in rows:
        print(f"{r['max_chg']*100:>6.1f}% {r['prob']*100:>7.0f}% "
              f"${r['v24']/1000:>7.0f}K  {r['question'][:52]}")
    conn.close()


def cmd_highconf(min_prob=0.80, n=20):
    conn = get_conn()
    week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()

    print(f"\n{'='*70}")
    print(f"HIGH CONFIDENCE MARKETS (YES ≥ {min_prob*100:.0f}%) — last 7 days")
    print(f"{'='*70}")
    print(f"{'AvgProb':>8} {'24hVol':>9} {'Hrs':>6}  Question")
    print("-" * 70)

    rows = conn.execute(f"""
        SELECT question, AVG(yes_prob) as prob, MAX(volume_24h) as v24,
               MIN(hours_to_resolve) as hrs, COUNT(*) as snaps
        FROM snapshots
        WHERE captured_at > ? AND yes_prob >= ?
        GROUP BY market_id
        ORDER BY prob DESC
        LIMIT ?
    """, (week_ago, min_prob, n)).fetchall()

    for r in rows:
        hrs = f"{r['hrs']:.0f}h" if r['hrs'] is not None else "—"
        print(f"{r['prob']*100:>7.0f}% ${r['v24']/1000:>7.0f}K {hrs:>6}  {r['question'][:52]}")
    conn.close()


def cmd_resolved(n=30):
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT r.resolved_at, r.question, r.resolved_yes, r.final_price, r.total_volume,
               (SELECT yes_prob FROM snapshots WHERE market_id=r.market_id
                ORDER BY captured_at ASC LIMIT 1) as first_prob
        FROM resolutions r
        ORDER BY r.resolved_at DESC
        LIMIT ?
    """, (n,)).fetchall()

    if not rows:
        print("No resolutions detected yet.")
        return

    print(f"\n{'='*70}")
    print(f"RECENT RESOLUTIONS ({len(rows)})")
    print(f"{'='*70}")
    print(f"{'Date':>10} {'Result':>6} {'FirstProb':>10} {'FinalProb':>10}  Question")
    print("-" * 70)
    for r in rows:
        result = "YES ✓" if r['resolved_yes'] else "NO ✗"
        first  = f"{r['first_prob']*100:.0f}%" if r['first_prob'] else "—"
        final  = f"{r['final_price']*100:.0f}%"
        print(f"{r['resolved_at'][:10]:>10} {result:>6} {first:>10} {final:>10}  {r['question'][:46]}")
    conn.close()


def cmd_divergences(n=20):
    conn = get_conn()
    week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()

    rows = conn.execute(f"""
        SELECT captured_at, poly_question, poly_prob, manifold_prob, spread, keywords
        FROM divergences
        WHERE captured_at > ?
        ORDER BY spread DESC
        LIMIT ?
    """, (week_ago, n)).fetchall()

    if not rows:
        print("No divergences recorded this week.")
        return

    print(f"\n{'='*70}")
    print(f"CROSS-PLATFORM DIVERGENCES — last 7 days")
    print(f"{'='*70}")
    for r in rows:
        direction = "POLY HIGH" if r['poly_prob'] > r['manifold_prob'] else "MFD HIGH"
        print(f"\n[{r['spread']*100:.0f}% spread | {direction} | {r['captured_at'][:10]}]")
        print(f"  Poly ({r['poly_prob']*100:.0f}%): {r['poly_question'][:65]}")
        print(f"  Keys: {r['keywords']}")
    conn.close()


def cmd_market(market_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT captured_at, yes_prob, chg_1h, volume_24h, spike_ratio, spread, hours_to_resolve
        FROM snapshots WHERE market_id=?
        ORDER BY captured_at
    """, (str(market_id),)).fetchall()

    if not rows:
        print(f"No data for market ID: {market_id}")
        return

    q = conn.execute("SELECT question FROM snapshots WHERE market_id=? LIMIT 1",
                     (str(market_id),)).fetchone()
    print(f"\nMarket: {q['question']}")
    print(f"Snapshots: {len(rows)}\n")
    print(f"{'Time':>19} {'YES%':>6} {'1hChg':>7} {'Vol24h':>9} {'Spike%':>7}")
    print("-" * 55)
    for r in rows:
        chg = f"{r['chg_1h']*100:+.1f}%" if r['chg_1h'] else "  —"
        print(f"{r['captured_at'][:19]:>19} {r['yes_prob']*100:>5.1f}% "
              f"{chg:>7} ${r['volume_24h']/1000:>7.0f}K {r['spike_ratio']*100:>6.0f}%")
    conn.close()


def cmd_export():
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM snapshots ORDER BY captured_at, market_id
    """).fetchall()

    fname = f"polywatch_export_{datetime.datetime.utcnow().strftime('%Y%m%d')}.csv"
    with open(fname, "w", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])

    print(f"Exported {len(rows):,} rows to {fname}")
    conn.close()


COMMANDS = {
    "summary":     cmd_summary,
    "spikes":      cmd_spikes,
    "movers":      cmd_movers,
    "highconf":    cmd_highconf,
    "resolved":    cmd_resolved,
    "divergences": cmd_divergences,
    "export":      cmd_export,
}

if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "summary"

    if cmd == "market" and len(args) > 1:
        cmd_market(args[1])
    elif cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        print(__doc__)
