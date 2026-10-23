#!/usr/bin/env python3
"""
Polymarket US — 15-minute crypto "Up or Down" logger + paper trader.

For BTC, ETH, SOL and XRP it:
  1. LOGS the order book of every 15-minute Up/Down window, once a second
  2. PAPER TRADES several "bid both sides" strategies, $1 per order
  3. RECORDS which side won, so every paper trade gets a real profit/loss

No account, no API key, no real money. It only reads Polymarket US public data.
Everything goes into one file: data/polylog.db

Run `python logger.py check` once to confirm the markets are being found.
"""
import asyncio
import collections
import contextlib
import html
import http.server
import io
import json
import live
import net
import streams
import math
import threading
import traceback
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_HALF_EVEN

# ============================== SETTINGS ==============================
COINS = ["btc", "eth", "sol", "xrp"]
ORDER_USD = 1.00          # every paper order is $1
STARTING_BALANCE = 100.0  # each strategy gets its own $100 paper account
POLL_EVERY = 1.0          # seconds between order-book and price reads...
POLL_FAST = 0.5           # ...and twice as often during the last 2 minutes of a window, when bets happen
WINDOW_SECONDS = 900      # 15 minutes
ENTRY_SECONDS = 180       # paper orders can go in during the first 3 minutes of each window

# Paper strategies. Each one runs on every window, for every coin.
#   bid:    the price we rest a buy order at, on BOTH Up and Down
#   cancel: seconds into the window when any unfilled order is cancelled
#   kind:   "maker" = resting limit orders, "taker" = buy both instantly at window open
STRATEGIES = [
    # CLOSING-MINUTE: in the last 2 minutes of each window, work out the real odds of Up from
    # the live coin price (the result is the AVERAGE price over the final 60 seconds, so much of
    # it is already known), and buy whichever side the market is pricing too cheaply.
    # One $1 bet per window at most. Skips the window if there's no clear edge.
    #   edge: minimum gap (after fees) between our odds and the market price, in dollars per share
    #   min_price / min_prob: FAVORITES ONLY - only buy a side the market already prices at 70c+
    #   and our model puts at 85%+. (Betting on cheap long shots lost money: the market was right.)
    #   usd: size of each bet
    {"name": "closer", "kind": "closer", "usd": 1.0, "edge": 0.03, "min_price": 0.70, "min_prob": 0.85},
    {"name": "closer_strict", "kind": "closer", "usd": 1.0, "edge": 0.05, "min_price": 0.80, "min_prob": 0.95},
    {"name": "closer_5", "kind": "closer", "usd": 5.0, "edge": 0.03, "min_price": 0.70, "min_prob": 0.85},
    {"name": "closer_strict_5", "kind": "closer", "usd": 5.0, "edge": 0.05, "min_price": 0.80, "min_prob": 0.95},
    {"name": "closer_10", "kind": "closer", "usd": 10.0, "edge": 0.03, "min_price": 0.70, "min_prob": 0.85},
    {"name": "closer_strict_10", "kind": "closer", "usd": 10.0, "edge": 0.05, "min_price": 0.80, "min_prob": 0.95},
]
BOT_LABEL = ""            # optional tag shown next to the title on the dashboard
CLOSER_FROM = 120         # start looking this many seconds before the window closes
CLOSER_UNTIL = 5          # stop this many seconds before the close (orders need time to land)
VOL_MULT = 1.25           # widen the volatility estimate for safety (crypto has sudden jumps)
COINBASE = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD"}
LOCK_PROFIT = 0.01        # follow: minimum profit per share pair it aims to lock after the first fill

# Fees (Polymarket US fee schedule): fee = theta * contracts * p * (1 - p), rounded to the cent.
TAKER_THETA = 0.0695
MAKER_THETA = 0.0         # standard maker rebate for small accounts is 0 after Oct 19 2026
# ======================================================================

GATEWAY = "https://gateway.polymarket.us"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(HERE, "data", "polylog.db"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("polylog")

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug TEXT PRIMARY KEY, coin TEXT, window_start REAL, window_end REAL,
    price_to_beat REAL, winner TEXT, settled_at REAL
);
CREATE TABLE IF NOT EXISTS snapshots (
    ts REAL, slug TEXT, t_in REAL,
    bid REAL, bid_sz REAL, ask REAL, ask_sz REAL,
    last_px REAL, shares_traded REAL, state TEXT
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT, slug TEXT, coin TEXT, side TEXT,
    price REAL, usd REAL, shares REAL, fee REAL,
    placed_at REAL, filled_cautious REAL, filled_hopeful REAL, cancelled_at REAL, mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_slug ON snapshots(slug);
CREATE INDEX IF NOT EXISTS idx_po_slug ON paper_orders(slug);
CREATE TABLE IF NOT EXISTS closer_signals (
    ts REAL, slug TEXT, coin TEXT, t_left REAL, spot REAL, basis REAL, sigma REAL,
    ptb REAL, est_close REAL, est_sd REAL, p_up REAL, up_ask REAL, down_ask REAL, action TEXT
);
CREATE INDEX IF NOT EXISTS idx_sig_slug ON closer_signals(slug);
CREATE TABLE IF NOT EXISTS spot_prices (ts REAL, coin TEXT, price REAL);
CREATE INDEX IF NOT EXISTS idx_spot_ts ON spot_prices(ts);
CREATE TABLE IF NOT EXISTS basis_obs (ts REAL, coin TEXT, slug TEXT, basis REAL);
"""


# ------------------------------ helpers ------------------------------
def http_json(path, params=None):
    url = GATEWAY + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return net.get_json(url, timeout=10)      # kept-alive connection: much faster than reconnecting


def amt(x):
    """Polymarket US prices look like {"value": "0.55", "currency": "USD"}."""
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def iso_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def fee(theta, shares, p):
    exact = Decimal(str(theta)) * Decimal(str(shares)) * Decimal(str(p)) * Decimal(str(1 - p))
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


# ------------------------------ discovery ------------------------------
def parse_updown(mk):
    """Turn a Polymarket US market into our dict if it's a 15-min Up/Down for one of our coins."""
    if not isinstance(mk, dict):
        return None
    apt = mk.get("assetPriceTerms") or {}
    mtype = str(apt.get("marketType", ""))
    horizon = str(apt.get("horizon", "")).lower()
    coin = str((apt.get("asset") or {}).get("symbol", "")).lower()
    if "UP_DOWN" not in mtype or coin not in COINS:
        return None
    if horizon and horizon not in ("15m", "15min", "15", "pt15m", "900s"):
        return None
    ws, we = iso_ts(apt.get("windowStart")), iso_ts(apt.get("windowEnd"))
    if not ws or not we or abs((we - ws) - WINDOW_SECONDS) > 5 or not mk.get("slug"):
        return None
    return {"slug": mk["slug"], "coin": coin, "start": ws, "end": we, "ptb": amt(apt.get("priceToBeat"))}


def slug_stamp(t):
    return time.strftime("%Y-%m-%d-%H%M", time.gmtime(t))


def guess_next_slug(known, start):
    """Polymarket names windows like ...-updown-15m-2026-10-18-2045z. Swap in the next window's time."""
    old = slug_stamp(known["start"])
    if old not in known["slug"]:
        return None
    return known["slug"].replace(old, slug_stamp(start))


def lookup_slug(slug):
    try:
        data = http_json(f"/v1/market/slug/{urllib.parse.quote(slug)}")
    except (urllib.error.HTTPError, net.HTTPError) as e:
        if e.code == 404:
            return None
        raise
    mk = data.get("market", data) if isinstance(data, dict) else None
    return parse_updown(mk)


def find_markets():
    """All open 15-minute Up/Down crypto markets for our coins."""
    found, offset = [], 0
    while offset < 3000:
        data = http_json("/v1/markets", {"categories": "crypto", "closed": "false",
                                         "limit": 200, "offset": offset})
        batch = data.get("markets", data if isinstance(data, list) else [])
        for mk in batch:
            m = parse_updown(mk)
            if m:
                found.append(m)
        if len(batch) < 200:
            break
        offset += 200
    return found


def read_book(slug):
    # unique "_" value so no cache in between can ever hand us an old copy of the book
    data = net.get_json(GATEWAY + f"/v1/markets/{urllib.parse.quote(slug)}/book?_={int(time.time() * 1000)}",
                        timeout=10, headers={"Cache-Control": "no-cache"})
    md = data.get("marketData", data)
    bids = [(amt(l.get("px")), float(l.get("qty") or 0)) for l in md.get("bids", []) or []]
    offers = [(amt(l.get("px")), float(l.get("qty") or 0)) for l in md.get("offers", []) or []]
    bids = [b for b in bids if b[0] is not None and b[1] > 0]
    offers = [o for o in offers if o[0] is not None and o[1] > 0]
    stats = md.get("stats") or {}
    best_bid = max(bids) if bids else (None, None)
    best_ask = min(offers) if offers else (None, None)
    st = md.get("state")
    traded = stats.get("sharesTraded")
    return {"bid": best_bid[0], "bid_sz": best_bid[1], "ask": best_ask[0], "ask_sz": best_ask[1],
            "last": amt(stats.get("lastTradePx")),
            "traded": float(traded) if traded not in (None, "") else None,
            "state": st}


def read_settlement(slug):
    try:
        data = http_json(f"/v1/markets/{urllib.parse.quote(slug)}/settlement")
    except (urllib.error.HTTPError, net.HTTPError) as e:
        if e.code == 404:
            return None
        raise
    s = data.get("settlement")
    if s is None:
        return None
    s = float(s)
    if s >= 0.99:
        return "up"
    if s <= 0.01:
        return "down"
    return None


# ------------------------------ paper trading ------------------------------
def would_fill(side, p, book, last, mode):
    """Would our resting buy of `side` at price p have filled, given this book and trade?"""
    if side == "up":      # bid at p on the Up book
        ask = book["ask"]
        if mode == "cautious":
            return (ask is not None and ask < p - 1e-9) or (last is not None and last < p - 1e-9)
        return (ask is not None and ask <= p + 1e-9) or (last is not None and last <= p + 1e-9)
    q = 1 - p             # buying Down at p = offering Up at 1 - p
    bid = book["bid"]
    if mode == "cautious":
        return (bid is not None and bid > q + 1e-9) or (last is not None and last > q + 1e-9)
    return (bid is not None and bid >= q - 1e-9) or (last is not None and last >= q - 1e-9)


def side_ask(book, side):
    if side == "up":
        return book["ask"]
    return None if book["bid"] is None else round(1 - book["bid"], 4)


def cents_down(x):
    return math.floor(x * 100 + 1e-6) / 100


def follow_quotes(book, edge):
    """Bid both sides `edge` below the live midpoint, never crossing the book."""
    if book["bid"] is None or book["ask"] is None:
        return None
    mid = (book["bid"] + book["ask"]) / 2
    up = min(cents_down(mid - edge), round(book["ask"] - 0.01, 2))
    dn = min(cents_down(1 - mid - edge), round(1 - book["bid"] - 0.01, 2))
    if up < 0.02 or dn < 0.02:
        return None
    return {"up": up, "down": dn}


def in_band(book, band):
    if not band:
        return True
    if book["bid"] is None or book["ask"] is None:
        return False
    mid = (book["bid"] + book["ask"]) / 2
    return band[0] <= mid <= band[1]


def opening_prices(s, book):
    """Prices for a new pair of bids, or None if this strategy shouldn't open one now."""
    if not in_band(book, s.get("band")):
        return None
    if s["kind"] == "follow":
        return follow_quotes(book, s["edge"])
    prices = {}
    for side in ("up", "down"):
        p = s["bid"]
        a = side_ask(book, side)
        if a is not None and p >= a:
            p = round(a - 0.01, 2)
        if p > 0.01:
            prices[side] = p
    return prices if len(prices) == 2 else None


class DynSim:
    """
    One strategy whose behaviour depends on what has filled (follow / hedge / repeat).
    Only ONE pair (one Up bid + one Down bid) is ever working at a time - no laddering.
    Because fills differ between the cautious and hopeful views, each view runs as its own
    simulation, and its orders are tagged with that mode.
    """

    def __init__(self, db, s, m, mode, prices=None):
        self.db, self.s, self.m, self.mode = db, s, m, mode
        self.legs = {}
        self.first = None
        self.pairs = 0
        self.done = False
        self.max_pairs = s.get("pairs", 1)
        self.new_until = s.get("new_until", s["cancel"])
        if prices:
            self._open(prices)

    def _open(self, prices):
        self.first = None
        self.legs = {side: self._insert(side, p, ORDER_USD / p, MAKER_THETA, filled=False)
                     for side, p in prices.items()}

    def _insert(self, side, p, shares, theta, filled):
        now = time.time()
        usd = shares * p
        f = fee(theta, shares, p)
        cur = self.db.execute(
            "INSERT INTO paper_orders(strategy,slug,coin,side,price,usd,shares,fee,placed_at,"
            "filled_cautious,filled_hopeful,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.s["name"], self.m["slug"], self.m["coin"], side, p, usd, shares, f, now,
             now if filled else None, now if filled else None, self.mode))
        return {"id": cur.lastrowid, "price": p, "shares": shares, "filled": filled}

    def _reprice(self, side, p, shares):
        leg = self.legs[side]
        if abs(leg["price"] - p) < 1e-9 and abs(leg["shares"] - shares) < 1e-9:
            return
        leg["price"], leg["shares"] = p, shares
        self.db.execute("UPDATE paper_orders SET price=?, shares=?, usd=? WHERE id=?",
                        (p, shares, shares * p, leg["id"]))

    def _fill(self, side, now):
        self.legs[side]["filled"] = True
        self.db.execute("UPDATE paper_orders SET filled_cautious=?, filled_hopeful=? WHERE id=?",
                        (now, now, self.legs[side]["id"]))

    def _cancel_open_legs(self, now):
        for leg in self.legs.values():
            if not leg["filled"]:
                self.db.execute("UPDATE paper_orders SET cancelled_at=? WHERE id=? AND cancelled_at IS NULL",
                                (now, leg["id"]))

    def finish(self, now):
        self._cancel_open_legs(now)
        self.done = True

    def _pair_complete(self, t_in):
        self.pairs += 1
        self.legs, self.first = {}, None
        if self.pairs >= self.max_pairs or t_in >= self.new_until:
            self.done = True

    def step(self, book, last, t_in, now):
        if self.done:
            return
        if t_in >= self.s["cancel"]:
            self.finish(now)
            return
        # 0. no pair working: open one if conditions allow
        if not self.legs:
            if t_in >= self.new_until:
                self.done = True
                return
            prices = opening_prices(self.s, book)
            if prices and (self.pairs == 0 or strategy_cash(self.db, self.s["name"]) >= 2 * ORDER_USD + 0.10):
                self._open(prices)
            return
        # 1. did anything fill at the prices we were showing?
        for side, leg in self.legs.items():
            if not leg["filled"] and would_fill(side, leg["price"], book, last, self.mode):
                self._fill(side, now)
                if self.first is None:
                    self.first = side
        if all(l["filled"] for l in self.legs.values()):
            self._pair_complete(t_in)
            return
        # 2. nothing filled yet
        if self.first is None:
            if t_in >= self.new_until or (self.s.get("band") and not in_band(book, self.s["band"])):
                # too late, or market has gone lopsided: pull both bids (may reopen later)
                self._cancel_open_legs(now)
                self.legs = {}
                if t_in >= self.new_until:
                    self.done = True
                return
            if self.s["kind"] == "follow":
                q = follow_quotes(book, self.s["edge"])
                if q:
                    for side in ("up", "down"):
                        self._reprice(side, q[side], ORDER_USD / q[side])
            return
        # 3. one side filled: work the other side
        p_first = self.legs[self.first]["price"]
        shares = self.legs[self.first]["shares"]          # match shares so the pair pays out exactly
        other = "down" if self.first == "up" else "up"
        ask = side_ask(book, other)
        hedge = self.s.get("hedge")
        if hedge is not None and ask is not None:
            cost = ask + p_first + TAKER_THETA * ask * (1 - ask)
            if cost <= 1 + hedge + 1e-9:
                leg = self.legs[other]
                self.db.execute("UPDATE paper_orders SET price=?, shares=?, usd=?, fee=?, "
                                "filled_cautious=?, filled_hopeful=? WHERE id=?",
                                (ask, shares, shares * ask, fee(TAKER_THETA, shares, ask), now, now, leg["id"]))
                leg.update(price=ask, shares=shares, filled=True)
                self._pair_complete(t_in)
                return
        if self.s["kind"] == "follow":
            target = cents_down(1 - p_first - LOCK_PROFIT)
            if ask is not None:
                target = min(target, round(ask - 0.01, 2))
            if target >= 0.01:
                self._reprice(other, target, shares)


class PaperBook:
    """
    Polymarket US has ONE order book per market, priced in Up (long) terms.
      Buy Up   at p  = a bid at p          -> fills when sellers come down to p
      Buy Down at q  = an offer at (1 - q) -> fills when buyers come up to (1 - q)
    So "Down ask" = 1 - best bid, and "Up ask + Down ask" = 1 + spread.
    """

    def __init__(self, db):
        self.db = db
        self.open = {}   # slug -> list of order dicts still working
        self.dyn = {}    # slug -> list of DynSim

    def place_window(self, m, book):
        orders = []
        for s in STRATEGIES:
            if s["kind"] == "closer":
                continue
            cash = strategy_cash(self.db, s["name"])
            if cash < 2 * ORDER_USD + 0.10:
                log.info("%s skipped %s: only $%.2f paper cash left", s["name"], m["slug"], cash)
                continue
            if s["kind"] == "maker":
                for side in ("up", "down"):
                    p = s["bid"]
                    # post-only: never cross the book; step back one tick if we would
                    if side == "up" and book["ask"] is not None and p >= book["ask"]:
                        p = round(book["ask"] - 0.01, 2)
                    if side == "down" and book["bid"] is not None and (1 - p) <= book["bid"]:
                        p = round(1 - book["bid"] - 0.01, 2)
                    if p <= 0.01:
                        continue
                    orders.append(self._new(s, m, side, p, MAKER_THETA, filled=False))
            elif s["kind"] == "taker":
                if book["ask"] is None or book["bid"] is None:
                    continue
                for side, p in (("up", book["ask"]), ("down", round(1 - book["bid"], 4))):
                    orders.append(self._new(s, m, side, p, TAKER_THETA, filled=True))
            elif s["kind"] in ("follow", "maker_hedge"):
                prices = opening_prices(s, book)
                waits = bool(s.get("band")) or s.get("pairs", 1) > 1
                if not prices and not waits:
                    continue
                for mode in ("cautious", "hopeful"):
                    self.dyn.setdefault(m["slug"], []).append(DynSim(self.db, s, m, mode, prices))
        self.db.commit()
        self.open[m["slug"]] = [o for o in orders if o["filled_c"] is None or o["filled_h"] is None]

    def _new(self, s, m, side, p, theta, filled):
        shares = ORDER_USD / p
        now = time.time()
        f = fee(theta, shares, p)
        cur = self.db.execute(
            "INSERT INTO paper_orders(strategy,slug,coin,side,price,usd,shares,fee,placed_at,filled_cautious,filled_hopeful)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (s["name"], m["slug"], m["coin"], side, p, ORDER_USD, shares, f, now,
             now if filled else None, now if filled else None))
        return {"id": cur.lastrowid, "strategy": s, "side": side, "price": p,
                "filled_c": now if filled else None, "filled_h": now if filled else None}

    def update(self, m, book, prev, t_in):
        """Check resting orders against the latest book and the latest trade."""
        now = time.time()
        new_trade = (book["traded"] is not None and prev and prev.get("traded") is not None
                     and book["traded"] > prev["traded"])
        last = book["last"] if new_trade else None
        for sim in self.dyn.get(m["slug"], []):
            try:
                sim.step(book, last, t_in, now)
            except Exception:
                log.exception("%s error on %s (stopping it for this window)", sim.s["name"], m["slug"])
                sim.done = True
        orders = self.open.get(m["slug"])
        if not orders:
            return
        keep = []
        for o in orders:
            s, p = o["strategy"], o["price"]
            if s["kind"] == "maker" and t_in >= s["cancel"]:
                self.db.execute("UPDATE paper_orders SET cancelled_at=? WHERE id=?", (now, o["id"]))
                continue
            if o["side"] == "up":     # our bid at p
                cautious = (book["ask"] is not None and book["ask"] < p - 1e-9) or (last is not None and last < p - 1e-9)
                hopeful = (book["ask"] is not None and book["ask"] <= p + 1e-9) or (last is not None and last <= p + 1e-9)
            else:                     # our offer at 1 - p
                q = 1 - p
                cautious = (book["bid"] is not None and book["bid"] > q + 1e-9) or (last is not None and last > q + 1e-9)
                hopeful = (book["bid"] is not None and book["bid"] >= q - 1e-9) or (last is not None and last >= q - 1e-9)
            if hopeful and o["filled_h"] is None:
                o["filled_h"] = now
                self.db.execute("UPDATE paper_orders SET filled_hopeful=? WHERE id=?", (now, o["id"]))
            if cautious and o["filled_c"] is None:
                o["filled_c"] = now
                self.db.execute("UPDATE paper_orders SET filled_cautious=? WHERE id=?", (now, o["id"]))
            if o["filled_c"] is None:
                keep.append(o)
        self.open[m["slug"]] = keep

    def close_window(self, slug):
        now = time.time()
        for sim in self.dyn.pop(slug, []):
            if not sim.done:
                sim.finish(now)
        for o in self.open.pop(slug, []):
            self.db.execute("UPDATE paper_orders SET cancelled_at=? WHERE id=? AND cancelled_at IS NULL", (now, o["id"]))


def strategy_cash(db, strategy):
    """Paper cash: $100 + settled profit/loss - money tied up in open or resting orders."""
    realized = db.execute("""
        SELECT COALESCE(SUM(CASE WHEN o.side = m.winner THEN o.shares ELSE 0 END - o.usd - o.fee), 0)
        FROM paper_orders o JOIN markets m ON m.slug = o.slug
        WHERE o.strategy = ? AND m.winner IS NOT NULL AND o.filled_cautious IS NOT NULL
          AND (o.mode IS NULL OR o.mode = 'cautious')""", (strategy,)).fetchone()[0]
    tied = db.execute("""
        SELECT COALESCE(SUM(o.usd + o.fee), 0)
        FROM paper_orders o JOIN markets m ON m.slug = o.slug
        WHERE o.strategy = ? AND m.winner IS NULL AND m.window_end > ?
          AND (o.mode IS NULL OR o.mode = 'cautious')
          AND (o.filled_cautious IS NOT NULL OR o.cancelled_at IS NULL)""",
        (strategy, time.time() - 3 * 3600)).fetchone()[0]
    return STARTING_BALANCE + realized - tied


def window_pnl(db, slug, winner, mode="cautious"):
    """Profit/loss per strategy for one settled window."""
    col = "filled_cautious" if mode == "cautious" else "filled_hopeful"
    out = {}
    for strat, side, usd, shares, f, filled in db.execute(
            f"SELECT strategy, side, usd, shares, fee, {col} FROM paper_orders "
            "WHERE slug=? AND (mode IS NULL OR mode=?)", (slug, mode)):
        if filled is None:
            out.setdefault(strat, 0.0)
            continue
        payout = shares if side == winner else 0.0
        out[strat] = out.get(strat, 0.0) + payout - usd - f
    return out


# ------------------------------ main loops ------------------------------
# ------------------------------ closing-minute strategy ------------------------------
def read_spot(coin):
    """Live coin price from Coinbase (one of the exchanges in the index Polymarket settles on)."""
    return float(net.get_json(f"https://api.exchange.coinbase.com/products/{COINBASE[coin]}/ticker",
                              timeout=4)["price"])


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def close_distribution(t_left, spot, observed_avg, observed_n, sigma):
    """
    The close is the average of 60 one-second index prices in the last minute.
    Returns (expected close, standard deviation), treating the price as a random walk
    with `sigma` dollars of movement per second.
      t_left > 60: none of the average is known yet
      t_left <= 60: `observed_n` of the 60 seconds are already in (average `observed_avg`)
    """
    r = max(0.0, t_left)
    if r > 60:
        a = r - 60
        var = sigma ** 2 * (a + (60 * 61 * 121 / 6) / 3600.0)
        return spot, math.sqrt(var)
    k = max(0, min(60, observed_n))
    rem = 60 - k
    known = observed_avg * k if k else 0.0
    mean = (known + spot * rem) / 60.0
    var = sigma ** 2 * (rem * (rem + 1) * (2 * rem + 1) / 6.0) / 3600.0
    return mean, math.sqrt(var)


class PriceFeed:
    def __init__(self, db=None):
        self.db = db
        self.hist = {c: collections.deque(maxlen=1500) for c in COINBASE}   # (ts, price)
        self.basis = {c: collections.deque(maxlen=24) for c in COINBASE}    # index - coinbase, per window open
        self.basis_done = set()
        self.now = {}                                                        # coin -> newest (ts, price)
        if db is not None:
            self._reload()

    def _reload(self):
        """After a restart, pick up where we left off instead of warming up again."""
        since = time.time() - 1500
        for ts, coin, price in self.db.execute(
                "SELECT ts, coin, price FROM spot_prices WHERE ts >= ? ORDER BY ts", (since,)):
            if coin in self.hist:
                self.hist[coin].append((ts, price))
        for coin in COINBASE:
            rows = self.db.execute("SELECT slug, basis FROM basis_obs WHERE coin = ? ORDER BY ts DESC LIMIT 24",
                                   (coin,)).fetchall()
            for slug, b in reversed(rows):
                self.basis[coin].append(b)
                self.basis_done.add(slug)
        n = sum(len(h) for h in self.hist.values())
        log.info("price history reloaded: %d prices, basis samples %s", n,
                 {c: len(b) for c, b in self.basis.items()})

    def record(self, coin, ts, price):
        """Keeps one price per second (the latest in that second) - the stream can send dozens."""
        self.now[coin] = (ts, price)
        h = self.hist[coin]
        if h and int(h[-1][0]) == int(ts):
            h[-1] = (ts, price)
            return
        h.append((ts, price))
        if self.db is not None:
            self.db.execute("INSERT INTO spot_prices VALUES(?,?,?)", (ts, coin, price))

    def latest(self, coin):
        if coin in self.now:
            return self.now[coin]
        h = self.hist[coin]
        return h[-1] if h else (None, None)

    def avg_between(self, coin, t0, t1):
        """Average price in [t0, t1) and how many distinct seconds of that span we have prices for."""
        pts = [(t, p) for t, p in self.hist[coin] if t0 <= t < t1]
        if not pts:
            return None, 0
        return sum(p for _, p in pts) / len(pts), len({int(t) for t, _ in pts})

    def sigma(self, coin, lookback=900):
        # one price per second (the last one in each second), so faster polling doesn't change the estimate
        per_sec = {}
        for t, p in self.hist[coin]:
            if t >= time.time() - lookback:
                per_sec[int(t)] = (t, p)
        h = [per_sec[k] for k in sorted(per_sec)]
        if len(h) < 120:
            return None
        rets = []
        for (t0, p0), (t1, p1) in zip(h, h[1:]):
            dt = t1 - t0
            if 0.5 <= dt <= 3:
                rets.append((p1 - p0) / math.sqrt(dt))
        if len(rets) < 100:
            return None
        mu = sum(rets) / len(rets)
        return math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * VOL_MULT

    def basis_est(self, coin, spot):
        b = sorted(self.basis[coin])
        if len(b) < 3:
            return 0.0, spot * 0.0005            # unknown yet: assume none, but be very unsure
        med = b[len(b) // 2]
        mad = sorted(abs(x - med) for x in b)[len(b) // 2]
        return med, max(1.4826 * mad, spot * 0.00005)

    def learn_basis(self, m):
        """At each window open we know the index average (price to beat) - compare to Coinbase."""
        if m["slug"] in self.basis_done or not m.get("ptb"):
            return
        avg, n = self.avg_between(m["coin"], m["start"] - 60, m["start"])
        if n >= 40:
            self.basis[m["coin"]].append(m["ptb"] - avg)
            self.basis_done.add(m["slug"])
            if self.db is not None:
                self.db.execute("INSERT INTO basis_obs VALUES(?,?,?,?)", (time.time(), m["coin"], m["slug"], m["ptb"] - avg))


class Closer:
    def __init__(self, db, feed):
        self.db, self.feed = db, feed
        self.bet = set()              # (strategy, slug) already bet
        self.ptb_checked = {}
        self.sig_logged = {}

    def step(self, m, book, now):
        t_left = m["end"] - now
        if not (CLOSER_UNTIL <= t_left <= CLOSER_FROM):
            return
        coin = m["coin"]
        ts, spot = self.feed.latest(coin)
        sigma = self.feed.sigma(coin)
        if spot is None or now - ts > 3 or not sigma or not m.get("ptb"):
            return
        if len(self.feed.basis[coin]) < 3:
            return                    # still learning the Coinbase-vs-index gap; odds would be guesswork
        basis, basis_sd = self.feed.basis_est(coin, spot)
        obs_avg, obs_n = self.feed.avg_between(coin, m["end"] - 60, now)
        if t_left <= 60 and obs_n < (60 - t_left) * 0.7:
            return                    # missing too much of the averaging minute to trust it
        elapsed = max(0, min(60, int(60 - t_left))) if t_left <= 60 else 0   # seconds of the average already in
        mean, sd = close_distribution(t_left, spot, obs_avg or spot, elapsed, sigma)
        mean += basis
        sd = math.sqrt(sd ** 2 + basis_sd ** 2)
        p_up = 1 - norm_cdf((m["ptb"] - mean) / sd) if sd > 0 else (1.0 if mean >= m["ptb"] else 0.0)
        p_up = min(max(p_up, 0.001), 0.999)
        up_ask = book["ask"]
        down_ask = None if book["bid"] is None else round(1 - book["bid"], 4)
        actions = []
        for st in STRATEGIES:
            if st["kind"] != "closer" or (st["name"], m["slug"]) in self.bet:
                continue
            best = None
            for side, ask, p, depth in (("up", up_ask, p_up, book["ask_sz"]),
                                        ("down", down_ask, 1 - p_up, book["bid_sz"])):
                if ask is None or ask <= 0 or ask >= 1:
                    continue
                if ask < st.get("min_price", 0) or p < st.get("min_prob", 0):
                    continue          # favorites only
                edge = p - ask - TAKER_THETA * ask * (1 - ask)
                if edge >= st["edge"] and (depth or 0) >= st.get("usd", ORDER_USD) / ask and (best is None or edge > best[2]):
                    best = (side, ask, edge)
            usd = st.get("usd", ORDER_USD)
            if best and strategy_cash(self.db, st["name"]) >= usd + 0.10:
                side, ask, edge = best
                shares = usd / ask
                self.db.execute(
                    "INSERT INTO paper_orders(strategy,slug,coin,side,price,usd,shares,fee,placed_at,"
                    "filled_cautious,filled_hopeful) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (st["name"], m["slug"], coin, side, ask, usd, shares,
                     fee(TAKER_THETA, shares, ask), now, now, now))
                self.bet.add((st["name"], m["slug"]))
                actions.append(f"{st['name']}:{side}@{ask:.2f}")
                if LIVE is not None:
                    LIVE.submit(st["name"], m, side, ask, t_left, p_up if side == "up" else 1 - p_up)
                log.info("%s BUY $%.0f %s %s at %.2f | model %.0f%% Up, %.0fs left, edge %.1fc",
                         st["name"], usd, coin.upper(), side.upper(), ask, p_up * 100, t_left, edge * 100)
        if not actions and now - self.sig_logged.get(m["slug"], 0) < 1.0:
            return
        self.sig_logged[m["slug"]] = now
        self.db.execute("INSERT INTO closer_signals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (now, m["slug"], coin, round(t_left, 1), spot, basis, sigma, m["ptb"],
                         mean, sd, p_up, up_ask, down_ask, ",".join(actions) or None))


class Runner:
    def __init__(self, db):
        self.db = db
        self.paper = PaperBook(db)
        self.markets = {}      # slug -> market dict
        self.prev = {}         # slug -> previous book
        self.placed = set()
        self.totals = {}
        self.known = {}        # coin -> most recent market seen (used to guess the next window's name)
        self.feed = PriceFeed(db)
        self._reload_unsettled()
        self.closer = Closer(db, self.feed)
        self.ptb_try = {}
        self.books = {}        # slug -> freshest book (stream or normal check, whichever is newer)
        self.last_eval = {}
        self.spot_stream = streams.SpotStream(COINBASE, self.on_spot)
        self.book_stream = None   # set in main() when API keys are present

    # ---------- streams: react the instant a price changes ----------
    def merge_book(self, slug, book, read_started):
        """Normal check result: take its sizes etc., but never overwrite a newer streamed price."""
        cur = self.books.get(slug)
        new = dict(book, bid_ts=read_started, ask_ts=read_started)
        if cur:
            for k in ("bid", "ask"):
                if cur.get(k + "_ts", 0) > read_started:
                    new[k], new[k + "_ts"] = cur[k], cur[k + "_ts"]
        self.books[slug] = new
        return new

    def on_book(self, slug, ts, upd):
        cur = self.books.get(slug)
        if cur is None:
            return                     # wait for one normal check first (it brings the sizes)
        for k, v in upd.items():
            cur[k], cur[k + "_ts"] = v, ts
        m = self.markets.get(slug)
        if m:
            self._evaluate(m)

    def on_spot(self, coin, ts, price):
        self.feed.record(coin, ts, price)
        for m in list(self.markets.values()):
            if m["coin"] == coin:
                self._evaluate(m)

    def _evaluate(self, m):
        now = time.time()
        if not (CLOSER_UNTIL <= m["end"] - now <= CLOSER_FROM):
            return
        if now - self.last_eval.get(m["slug"], 0) < 0.05:
            return
        book = self.books.get(m["slug"])
        if not book or now - max(book.get("bid_ts", 0), book.get("ask_ts", 0)) > 3 and not (
                self.book_stream and self.book_stream.healthy()):
            return
        self.last_eval[m["slug"]] = now
        try:
            self.closer.step(m, book, now)
        except Exception:
            log.exception("closer error on %s", m["slug"])

    def stream_status(self):
        return {"coin_prices": self.spot_stream.status(),
                "polymarket": self.book_stream.status() if self.book_stream else None}

    def wanted_slugs(self):
        now = time.time()
        return {m["slug"] for m in self.markets.values() if m["start"] - 5 <= now <= m["end"]}

    def step(self):
        now = time.time()
        closing = any(m["start"] <= now and 0 <= m["end"] - now <= CLOSER_FROM + 10 for m in self.markets.values())
        return POLL_FAST if closing else POLL_EVERY

    def _reload_unsettled(self):
        """After a restart, keep waiting for results of windows that closed but weren't scored yet."""
        now = time.time()
        rows = self.db.execute(
            "SELECT slug, coin, window_start, window_end, price_to_beat FROM markets "
            "WHERE winner IS NULL AND window_end < ? AND window_end > ?", (now, now - 24 * 3600)).fetchall()
        for slug, coin, ws, we, ptb in rows:
            self.markets.setdefault(slug, {"slug": slug, "coin": coin, "start": ws, "end": we, "ptb": ptb})
            self.placed.add(slug)
        if rows:
            log.info("picked up %d closed windows still waiting for results", len(rows))

    def _track(self, m):
        now = time.time()
        if m["end"] < now or m["start"] > now + WINDOW_SECONDS + 120:
            return False
        if m["slug"] in self.markets:
            if m["ptb"] and not self.markets[m["slug"]].get("ptb"):
                self.markets[m["slug"]]["ptb"] = m["ptb"]
                self.db.execute("UPDATE markets SET price_to_beat=? WHERE slug=?", (m["ptb"], m["slug"]))
            return False
        self.markets[m["slug"]] = m
        self.known[m["coin"]] = m
        self.db.execute("INSERT OR IGNORE INTO markets(slug,coin,window_start,window_end,price_to_beat)"
                        " VALUES(?,?,?,?,?)", (m["slug"], m["coin"], m["start"], m["end"], m["ptb"]))
        log.info("tracking %s (%.0fs into window)", m["slug"], now - m["start"])
        return True

    async def discover(self):
        """Full market scan once a minute."""
        while True:
            try:
                found = await asyncio.to_thread(find_markets)
                for m in sorted(found, key=lambda x: x["start"]):
                    self._track(m)
                    if m["coin"] not in self.known or m["start"] > self.known[m["coin"]]["start"]:
                        self.known[m["coin"]] = m
                self.db.commit()
                missing = [c for c in COINS if c not in {m["coin"] for m in found}]
                if missing:
                    log.info("no open 15-min markets found for: %s", ",".join(missing))
            except Exception as e:
                log.warning("market lookup failed: %s", e)
            await asyncio.sleep(60)

    async def fast_discover(self):
        """Around each 15-minute boundary, look up the next window by name every 2 seconds
        so orders go in right as it opens."""
        while True:
            now = time.time()
            nxt = (now // WINDOW_SECONDS + 1) * WINDOW_SECONDS
            cur = nxt - WINDOW_SECONDS
            for start in (cur, nxt):
                if not (start - 30 <= now <= start + 60):
                    continue
                for coin in COINS:
                    k = self.known.get(coin)
                    if not k or any(m["coin"] == coin and abs(m["start"] - start) < 5 for m in self.markets.values()):
                        continue
                    slug = guess_next_slug(k, start)
                    if not slug:
                        continue
                    try:
                        m = await asyncio.to_thread(lookup_slug, slug)
                    except Exception as e:
                        log.warning("quick lookup %s failed: %s", slug, e)
                        m = None
                    if m and self._track(m):
                        self.db.commit()
            await asyncio.sleep(2)

    async def poll(self):
        last_commit = last_health = time.time()
        rows = 0
        errs = {}
        while True:
          try:
            step = self.step()
            await asyncio.sleep(step - (time.time() % step))
            now = time.time()
            HEARTBEAT[0] = now
            live = [m for m in self.markets.values() if m["start"] - 5 <= now <= m["end"]]
            if not live:
                continue
            results = await asyncio.gather(*(asyncio.wait_for(asyncio.to_thread(read_book, m["slug"]), 8)
                                             for m in live), return_exceptions=True)
            HEARTBEAT[0] = time.time()
            limited = False
            for m, book in zip(live, results):
                if isinstance(book, BaseException):
                    n = errs.get(m["slug"], 0) + 1
                    errs[m["slug"]] = n
                    if n in (1, 10) or n % 60 == 0:
                        log.warning("book read failed for %s (%d times): %r", m["slug"], n, book)
                    if isinstance(book, (urllib.error.HTTPError, net.HTTPError)) and book.code == 429:
                        limited = True
                    continue
                if errs.pop(m["slug"], 0):
                    log.info("book reads working again for %s", m["slug"])
                t_in = now - m["start"]
                self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (now, m["slug"], round(t_in, 2), book["bid"], book["bid_sz"], book["ask"],
                                 book["ask_sz"], book["last"], book["traded"], book["state"]))
                rows += 1
                if 0 <= t_in <= ENTRY_SECONDS and m["slug"] not in self.placed and book["bid"] is not None and book["ask"] is not None:
                    self.paper.place_window(m, book)
                    self.placed.add(m["slug"])
                elif m["slug"] in self.placed:
                    self.paper.update(m, book, self.prev.get(m["slug"]), t_in)
                self.prev[m["slug"]] = book
                merged = self.merge_book(m["slug"], book, now)
                if CLOSER_UNTIL <= m["end"] - time.time() <= CLOSER_FROM:
                    self.last_eval[m["slug"]] = time.time()
                    try:
                        self.closer.step(m, merged, time.time())
                    except Exception:
                        log.exception("closer error on %s", m["slug"])
            if now - last_commit > 5:
                self.db.commit()
                last_commit = now
            if now - last_health > 300:
                log.info("health: %d live windows, %d book reads in last 5 min | streams %s | paper P/L so far: %s",
                         len(live), rows, self.stream_status(), self._fmt_totals())
                rows, last_health = 0, now
            if limited:
                log.warning("rate limited by Polymarket; pausing 3s")
                await asyncio.sleep(3)
          except asyncio.CancelledError:
            raise
          except Exception:
            log.exception("poll loop error (continuing)")
            await asyncio.sleep(1)

    async def prices(self):
        """Coin prices once a second, plus the 'price to beat' for each live window."""
        errs = 0
        last_prune = 0
        while True:
            step = self.step()
            await asyncio.sleep(step - (time.time() % step))
            now = time.time()
            streamed = self.spot_stream.healthy(5)
            need = [c for c in COINS if not (streamed and now - self.feed.latest(c)[0] < 3)]
            res = await asyncio.gather(*(asyncio.wait_for(asyncio.to_thread(read_spot, c), 4) for c in need),
                                       return_exceptions=True)
            for c, p in zip(need, res):
                if isinstance(p, BaseException):
                    errs += 1
                    if errs in (1, 10) or errs % 300 == 0:
                        log.warning("coin price read failed for %s (%d errors so far): %r", c, errs, p)
                    continue
                self.feed.record(c, now, p)
            if now - last_prune > 600:
                self.db.execute("DELETE FROM spot_prices WHERE ts < ?", (now - 7200,))
                last_prune = now
            for m in list(self.markets.values()):
                if m["start"] <= now <= m["end"]:
                    if not m.get("ptb") and now - m["start"] > 3 and now - self.ptb_try.get(m["slug"], 0) > 10:
                        self.ptb_try[m["slug"]] = now
                        try:
                            fresh = await asyncio.to_thread(lookup_slug, m["slug"])
                            if fresh and fresh.get("ptb"):
                                m["ptb"] = fresh["ptb"]
                                self.db.execute("UPDATE markets SET price_to_beat=? WHERE slug=?", (m["ptb"], m["slug"]))
                        except Exception as e:
                            log.warning("price-to-beat lookup failed for %s: %r", m["slug"], e)
                    if now - m["start"] > 2:
                        self.feed.learn_basis(m)

    async def settle(self):
        while True:
            now = time.time()
            for m in list(self.markets.values()):
                if now < m["end"] + 45:
                    continue
                self.paper.close_window(m["slug"])
                try:
                    winner = await asyncio.to_thread(read_settlement, m["slug"])
                except Exception as e:
                    log.warning("settlement check %s failed: %s", m["slug"], e)
                    winner = None
                if winner:
                    self.db.execute("UPDATE markets SET winner=?, settled_at=? WHERE slug=?",
                                    (winner, now, m["slug"]))
                    self.db.commit()
                    pnl = window_pnl(self.db, m["slug"], winner)
                    for k, v in pnl.items():
                        self.totals[k] = self.totals.get(k, 0.0) + v
                    log.info("SETTLED %s %s -> %s | %s", m["coin"].upper(),
                             datetime.fromtimestamp(m["start"]).strftime("%H:%M"), winner.upper(),
                             "  ".join(f"{k} {v:+.2f}" for k, v in pnl.items()))
                    self._forget(m["slug"])
                elif now > m["end"] + 24 * 3600:
                    log.warning("gave up waiting for settlement of %s", m["slug"])
                    self._forget(m["slug"])
                await asyncio.sleep(0.3)
            await asyncio.sleep(20)

    def _forget(self, slug):
        self.markets.pop(slug, None)
        self.prev.pop(slug, None)
        self.placed.discard(slug)

    def _fmt_totals(self):
        return "  ".join(f"{k} ${v:+.2f}" for k, v in sorted(self.totals.items())) or "(nothing settled yet)"


LIVE = None   # set in main(): live.LiveTrader


# ------------------------------ watchdog ------------------------------
HEARTBEAT = [time.time()]
RUNNER = [None]


def start_watchdog(limit=90):
    """If the main loop goes quiet for `limit` seconds, log where it's stuck and restart."""
    main_id = threading.main_thread().ident

    def run():
        while True:
            time.sleep(5)
            quiet = time.time() - HEARTBEAT[0]
            if quiet > limit:
                frame = sys._current_frames().get(main_id)
                stack = "".join(traceback.format_stack(frame)) if frame else "(no stack)"
                log.error("main loop stuck for %.0fs - restarting. It was here:\n%s", quiet, stack)
                time.sleep(1)
                os._exit(1)

    threading.Thread(target=run, daemon=True).start()


# ------------------------------ web report (for checking from a phone) ------------------------------
RECENT_LOG = collections.deque(maxlen=400)


class _RingHandler(logging.Handler):
    def emit(self, record):
        RECENT_LOG.append(self.format(record))


_rh = _RingHandler()
_rh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_rh)

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Polybot</title>
<style>body{{margin:0;background:#111;color:#ddd;font:14px -apple-system,system-ui,sans-serif}}
nav{{display:flex;gap:8px;flex-wrap:wrap;padding:12px;background:#1b1b1b;position:sticky;top:0}}
nav a{{color:#fff;background:#333;padding:8px 12px;border-radius:8px;text-decoration:none}}
pre{{padding:12px;margin:0;font:12px ui-monospace,Menlo,monospace;white-space:pre;overflow-x:auto}}</style>
</head><body><nav>{nav}</nav><pre>{body}</pre></body></html>"""


def _capture(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            fn(*args)
        except Exception as e:
            print(f"\nERROR: {e!r}")
    return buf.getvalue()


def start_web():
    port = os.environ.get("PORT")
    if not port:
        return
    key = os.environ.get("REPORT_KEY", "")
    import analyze
    import dashboard

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if key and q.get("key", [""])[0] != key:
                self.send_response(403)
                self.end_headers()
                return
            code, out = 404, {"error": "not found"}
            if u.path == "/live/set" and LIVE is not None:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    if "settings" in body:
                        LIVE.save_settings(body["settings"])
                    if body.get("end_cooldown"):
                        LIVE.end_cooldown()
                    LIVE.set_control(armed=body.get("armed"), bots=body.get("bots"))
                    code, out = 200, LIVE.snapshot()
                except Exception as e:
                    code, out = 400, {"error": str(e)}
            b = json.dumps(out, default=list).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if key and q.get("key", [""])[0] != key:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"add ?key=YOUR_REPORT_KEY to the address")
                return
            k = f"&key={urllib.parse.quote(key)}" if key else ""
            links = [("Dashboard", f"/?x=1{k}"), ("All coins", f"/report?x=1{k}"), ("Last 24h", f"/report?hours=24{k}")]
            links += [(c.upper(), f"/report?coin={c}{k}") for c in COINS]
            links += [("Live log", f"/log?x=1{k}"), ("Market check", f"/check?x=1{k}")]
            nav = "".join(f'<a href="{h}">{t}</a>' for t, h in links)
            if u.path in ("/", "/api"):
                try:
                    if u.path == "/":
                        out, ctype = dashboard.PAGE.encode(), "text/html; charset=utf-8"
                    else:
                        data = dashboard.summary(DB_PATH, q.get("mode", ["cautious"])[0], q.get("coin", [""])[0],
                                                     STARTING_BALANCE, [x["name"] for x in STRATEGIES])
                        data["label"] = BOT_LABEL
                        data["trader"] = LIVE.snapshot() if LIVE is not None else None
                        data["status"]["heartbeat_age"] = time.time() - HEARTBEAT[0]
                        data["status"]["streams"] = RUNNER[0].stream_status() if RUNNER[0] else None
                        out, ctype = json.dumps(data, default=list).encode(), "application/json"
                    self.send_response(200)
                except Exception as e:
                    out, ctype = json.dumps({"error": repr(e)}).encode(), "application/json"
                    self.send_response(500)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(out)
                return
            if u.path == "/live/stop" and LIVE is not None:      # emergency OFF link
                LIVE.stop()
                self.send_response(303)
                self.send_header("Location", f"/?x=1{k}")
                self.end_headers()
                return
            if u.path == "/log":
                body = "\n".join(reversed(RECENT_LOG)) or "(no log lines yet)"
            elif u.path == "/check":
                body = _capture(check)
            elif u.path == "/report":
                argv = ["--db", DB_PATH]
                if q.get("coin"):
                    argv += ["--coin", q["coin"][0]]
                if q.get("hours"):
                    argv += ["--hours", q["hours"][0]]
                body = _capture(analyze.main, argv)
            else:
                self.send_response(404)
                self.end_headers()
                return
            out = PAGE.format(nav=nav, body=html.escape(body)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(out)

    srv = http.server.ThreadingHTTPServer((os.environ.get("BIND", "0.0.0.0"), int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("report page listening on port %s", port)


async def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # autocommit: every write is saved immediately, so the live trader never waits on a long-held lock
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    try:
        db.execute("ALTER TABLE paper_orders ADD COLUMN mode TEXT")
    except sqlite3.OperationalError:
        pass
    db.commit()
    global LIVE
    LIVE = live.LiveTrader(DB_PATH, STRATEGIES)
    log.info("live trading: %s | live bots: %s", "ON" if LIVE.armed() else "off",
             ", ".join(LIVE.live_bots()) or "none")
    r = Runner(db)
    RUNNER[0] = r
    log.info("Polymarket US 15-min logger + paper trader | coins: %s | $%.2f per order",
             ",".join(COINS), max(st.get("usd", ORDER_USD) for st in STRATEGIES))
    start_web()
    for ln in _capture(check, False).splitlines():
        log.info("check: %s", ln)
    start_watchdog()
    try:
        tasks = [r.discover(), r.fast_discover(), r.poll(), r.settle(), r.prices(), r.spot_stream.run()]
        if LIVE.key is not None:
            r.book_stream = streams.BookStream(LIVE.key_id, LIVE.key, r.wanted_slugs, r.on_book)
            tasks.append(r.book_stream.run())
        await asyncio.gather(*tasks)
    except Exception:
        log.exception("crashed - restarting")
        time.sleep(1)
        os._exit(1)


def check(verbose=True):
    """One-shot sanity check: are we finding markets and reading books?"""
    print("Looking up 15-minute Up/Down markets on Polymarket US...")
    raw = http_json("/v1/markets", {"categories": "crypto", "closed": "false", "limit": 200})
    mk = raw.get("markets", [])
    print(f"  crypto markets returned: {len(mk)}")
    sample = [x for x in mk if x.get("assetPriceTerms")][:1]
    if sample and verbose:
        print("  example market (raw):")
        print("   ", json.dumps({k: sample[0].get(k) for k in ("slug", "assetPriceTerms", "marketSides")})[:1500])
    found = find_markets()
    now = time.time()
    for coin in COINS:
        mine = sorted([m for m in found if m["coin"] == coin], key=lambda m: m["start"])
        cur = [m for m in mine if m["start"] <= now < m["end"]]
        print(f"  {coin.upper()}: {len(mine)} open windows" + (f", current = {cur[0]['slug']}" if cur else ""))
    cur = [m for m in found if m["start"] <= now < m["end"]]
    if cur:
        b = read_book(cur[0]["slug"])
        print(f"  book for {cur[0]['slug']}: Up bid {b['bid']} / Up ask {b['ask']}  "
              f"-> Down ask {None if b['bid'] is None else round(1 - b['bid'], 4)}  (last {b['last']}, state {b['state']})")
        if verbose:
            print("  raw book:", json.dumps(http_json(f"/v1/markets/{urllib.parse.quote(cur[0]['slug'])}/book"))[:1500])
    print("Done. If every coin shows open windows, the logger is ready.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check()
    else:
        asyncio.run(main())
