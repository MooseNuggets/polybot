#!/usr/bin/env python3
"""
Paper-trading report for logger.py.

  venv/bin/python analyze.py            # all coins
  venv/bin/python analyze.py --coin btc # one coin
  venv/bin/python analyze.py --hours 24 # last 24 hours only
"""
import argparse
import os
import sqlite3
import time
from collections import defaultdict

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "polylog.db")


def line(t=""):
    print("=" * 66)
    if t:
        print(t)
        print("=" * 66)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--coin", default=None)
    ap.add_argument("--hours", type=float, default=None)
    a = ap.parse_args()
    if not os.path.exists(a.db):
        print("No data yet. Is the logger running?")
        return
    db = sqlite3.connect(a.db)

    # ---------- 1. status ----------
    line("1. STATUS")
    last, first, nsnap = db.execute("SELECT MAX(ts), MIN(ts), COUNT(*) FROM snapshots").fetchone()
    if not last:
        print("  No order-book data yet.")
        return
    age = time.time() - last
    print(f"  Last data: {age:.0f}s ago -> " + ("OK, running" if age < 60 else f"WARNING: stalled {age/60:.0f} min"))
    print(f"  Running for {(last - first)/3600:.1f} hours, {nsnap:,} book reads")

    where, args = ["m.winner IS NOT NULL"], []
    if a.coin:
        where.append("m.coin = ?"); args.append(a.coin.lower())
    if a.hours:
        where.append("m.window_start >= ?"); args.append(time.time() - a.hours * 3600)
    w = " AND ".join(where)
    settled = db.execute(f"SELECT m.slug, m.coin, m.winner FROM markets m WHERE {w}", args).fetchall()
    traded = {r[0] for r in db.execute("SELECT DISTINCT slug FROM paper_orders")}
    settled = [s for s in settled if s[0] in traded]
    print(f"  Settled windows with paper trades: {len(settled)}")
    if settled:
        ups = sum(1 for s in settled if s[2] == "up")
        print(f"  Up won {100*ups/len(settled):.0f}%, Down won {100*(len(settled)-ups)/len(settled):.0f}%")
    if not settled:
        print("  Nothing settled yet. Check back after the first 15-minute window ends.")
        return
    winners = {s[0]: s[2] for s in settled}
    coin_of = {s[0]: s[1] for s in settled}

    orders = db.execute("""SELECT strategy, slug, side, price, usd, shares, fee, filled_cautious, filled_hopeful
                           FROM paper_orders""").fetchall()

    # ---------- 2. paper results ----------
    for mode, idx, title in (("cautious", 7, "2. PAPER RESULTS - CAUTIOUS fills (price traded THROUGH our order)"),
                             ("hopeful", 8, "3. PAPER RESULTS - HOPEFUL fills (price TOUCHED our order)")):
        print()
        line(title)
        # per strategy, per window
        win = defaultdict(lambda: defaultdict(lambda: {"fills": set(), "pnl": 0.0, "spent": 0.0}))
        for strat, slug, side, price, usd, shares, f, fc, fh in orders:
            if slug not in winners:
                continue
            cell = win[strat][slug]
            if (fc if mode == "cautious" else fh) is None:
                continue
            cell["fills"].add(side)
            cell["pnl"] += (shares if side == winners[slug] else 0) - usd - f
            cell["spent"] += usd + f
        print(f"  {'strategy':11} {'coin':4} {'windows':>7} {'both':>6} {'one':>6} {'none':>6} "
              f"{'$ spent':>8} {'P/L':>8} {'ROI':>7}")
        for strat in sorted(win):
            groups = defaultdict(list)
            for slug in winners:
                groups["ALL"].append(slug)
                groups[coin_of[slug]].append(slug)
            for g in ["ALL"] + sorted(k for k in groups if k != "ALL"):
                slugs = groups[g]
                cells = [win[strat][s] if s in win[strat] else {"fills": set(), "pnl": 0.0, "spent": 0.0}
                         for s in slugs]
                n = len(cells)
                both = sum(1 for c in cells if len(c["fills"]) == 2)
                one = sum(1 for c in cells if len(c["fills"]) == 1)
                spent = sum(c["pnl"] * 0 + c["spent"] for c in cells)
                pnl = sum(c["pnl"] for c in cells)
                roi = f"{100*pnl/spent:+.1f}%" if spent else "-"
                label = strat if g == "ALL" else ""
                print(f"  {label:11} {g:4} {n:7} {100*both/n:5.0f}% {100*one/n:5.0f}% {100*(n-both-one)/n:5.0f}% "
                      f"{spent:8.2f} {pnl:+8.2f} {roi:>7}")
            print()

    # ---------- 4. spread ----------
    line("4. MARKET SPREAD (what buying both sides instantly costs)")
    rows = db.execute(f"""SELECT m.coin, AVG(s.ask - s.bid), AVG(CASE WHEN s.ask - s.bid <= 0.011 THEN 1.0 ELSE 0 END)
                          FROM snapshots s JOIN markets m ON m.slug = s.slug
                          WHERE s.ask IS NOT NULL AND s.bid IS NOT NULL AND s.t_in BETWEEN 0 AND 900
                          {'AND m.coin = ?' if a.coin else ''}
                          GROUP BY m.coin""", ([a.coin.lower()] if a.coin else [])).fetchall()
    for coin, spread, tight in rows:
        print(f"  {coin.upper():4} avg spread {spread*100:.1f} cents (Up+Down cost ${1+spread:.3f}), "
              f"1-cent spread {100*tight:.0f}% of the time")
    print()
    print("  Reading this: on Polymarket US, Up and Down share one order book, so buying")
    print("  both instantly always costs $1 + the spread + fees. Profit has to come from")
    print("  resting orders (maker) that get filled on both sides.")
    print("  The truth sits between CAUTIOUS and HOPEFUL, since other orders at the same")
    print("  price can be filled before yours.")


if __name__ == "__main__":
    main()
