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
import threading
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
POLL_EVERY = 1.0          # seconds between order-book reads
WINDOW_SECONDS = 900      # 15 minutes

# Paper strategies. Each one runs on every window, for every coin.
#   bid:    the price we rest a buy order at, on BOTH Up and Down
#   cancel: seconds into the window when any unfilled order is cancelled
#   kind:   "maker" = resting limit orders, "taker" = buy both instantly at window open
STRATEGIES = [
    {"name": "maker_49", "kind": "maker", "bid": 0.49, "cancel": 720},
    {"name": "maker_48", "kind": "maker", "bid": 0.48, "cancel": 720},
    {"name": "maker_46", "kind": "maker", "bid": 0.46, "cancel": 720},
    {"name": "maker_44", "kind": "maker", "bid": 0.44, "cancel": 720},
    {"name": "taker_both", "kind": "taker"},   # baseline: what instantly buying both costs
]

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
    placed_at REAL, filled_cautious REAL, filled_hopeful REAL, cancelled_at REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_slug ON snapshots(slug);
CREATE INDEX IF NOT EXISTS idx_po_slug ON paper_orders(slug);
"""


# ------------------------------ helpers ------------------------------
def http_json(path, params=None):
    url = GATEWAY + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "polylog/2.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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
def find_markets():
    """All open 15-minute Up/Down crypto markets for our coins."""
    found, offset = [], 0
    while offset < 3000:
        data = http_json("/v1/markets", {"categories": "crypto", "closed": "false",
                                         "limit": 200, "offset": offset})
        batch = data.get("markets", data if isinstance(data, list) else [])
        for mk in batch:
            apt = mk.get("assetPriceTerms") or {}
            mtype = str(apt.get("marketType", ""))
            horizon = str(apt.get("horizon", "")).lower()
            coin = str((apt.get("asset") or {}).get("symbol", "")).lower()
            if "UP_DOWN" not in mtype or coin not in COINS:
                continue
            if horizon and horizon not in ("15m", "15min", "15", "pt15m", "900s"):
                continue
            ws, we = iso_ts(apt.get("windowStart")), iso_ts(apt.get("windowEnd"))
            if not ws or not we or abs((we - ws) - WINDOW_SECONDS) > 5:
                continue
            found.append({"slug": mk["slug"], "coin": coin, "start": ws, "end": we,
                          "ptb": amt(apt.get("priceToBeat"))})
        if len(batch) < 200:
            break
        offset += 200
    return found


def read_book(slug):
    data = http_json(f"/v1/markets/{urllib.parse.quote(slug)}/book")
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
    except urllib.error.HTTPError as e:
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

    def place_window(self, m, book):
        orders = []
        for s in STRATEGIES:
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
        orders = self.open.get(m["slug"])
        if not orders:
            return
        now = time.time()
        new_trade = (book["traded"] is not None and prev and prev.get("traded") is not None
                     and book["traded"] > prev["traded"])
        last = book["last"] if new_trade else None
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
        for o in self.open.pop(slug, []):
            self.db.execute("UPDATE paper_orders SET cancelled_at=? WHERE id=? AND cancelled_at IS NULL", (now, o["id"]))


def window_pnl(db, slug, winner, mode="cautious"):
    """Profit/loss per strategy for one settled window."""
    col = "filled_cautious" if mode == "cautious" else "filled_hopeful"
    out = {}
    for strat, side, usd, shares, f, filled in db.execute(
            f"SELECT strategy, side, usd, shares, fee, {col} FROM paper_orders WHERE slug=?", (slug,)):
        if filled is None:
            out.setdefault(strat, 0.0)
            continue
        payout = shares if side == winner else 0.0
        out[strat] = out.get(strat, 0.0) + payout - usd - f
    return out


# ------------------------------ main loops ------------------------------
class Runner:
    def __init__(self, db):
        self.db = db
        self.paper = PaperBook(db)
        self.markets = {}      # slug -> market dict
        self.prev = {}         # slug -> previous book
        self.placed = set()
        self.totals = {}

    async def discover(self):
        while True:
            try:
                found = await asyncio.to_thread(find_markets)
                now = time.time()
                n_new = 0
                for m in found:
                    # only keep the window running now and the next one
                    if m["end"] < now or m["start"] > now + WINDOW_SECONDS + 120:
                        continue
                    if m["slug"] not in self.markets:
                        self.markets[m["slug"]] = m
                        self.db.execute("INSERT OR IGNORE INTO markets(slug,coin,window_start,window_end,price_to_beat)"
                                        " VALUES(?,?,?,?,?)", (m["slug"], m["coin"], m["start"], m["end"], m["ptb"]))
                        n_new += 1
                    elif m["ptb"] and not self.markets[m["slug"]].get("ptb"):
                        self.markets[m["slug"]]["ptb"] = m["ptb"]
                        self.db.execute("UPDATE markets SET price_to_beat=? WHERE slug=?", (m["ptb"], m["slug"]))
                self.db.commit()
                if n_new:
                    log.info("tracking %d new windows", n_new)
                coins_seen = {m["coin"] for m in found}
                missing = [c for c in COINS if c not in coins_seen]
                if missing:
                    log.info("no open 15-min markets found for: %s", ",".join(missing))
            except Exception as e:
                log.warning("market lookup failed: %s", e)
            await asyncio.sleep(60)

    async def poll(self):
        last_commit = last_health = time.time()
        rows = 0
        while True:
            await asyncio.sleep(POLL_EVERY - (time.time() % POLL_EVERY))
            now = time.time()
            live = [m for m in self.markets.values() if m["start"] - 5 <= now <= m["end"]]
            if not live:
                continue
            results = await asyncio.gather(*(asyncio.to_thread(read_book, m["slug"]) for m in live),
                                           return_exceptions=True)
            for m, book in zip(live, results):
                if isinstance(book, Exception):
                    if isinstance(book, urllib.error.HTTPError) and book.code == 429:
                        log.warning("rate limited; slowing down")
                        await asyncio.sleep(5)
                    continue
                t_in = now - m["start"]
                self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
                                (now, m["slug"], round(t_in, 2), book["bid"], book["bid_sz"], book["ask"],
                                 book["ask_sz"], book["last"], book["traded"], book["state"]))
                rows += 1
                if 0 <= t_in <= 10 and m["slug"] not in self.placed and book["bid"] is not None and book["ask"] is not None:
                    self.paper.place_window(m, book)
                    self.placed.add(m["slug"])
                elif m["slug"] in self.placed:
                    self.paper.update(m, book, self.prev.get(m["slug"]), t_in)
                self.prev[m["slug"]] = book
            if now - last_commit > 5:
                self.db.commit()
                last_commit = now
            if now - last_health > 300:
                log.info("health: %d live windows, %d book reads in last 5 min | paper P/L so far: %s",
                         len(live), rows, self._fmt_totals())
                rows, last_health = 0, now

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
                elif now > m["end"] + 3 * 3600:
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

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if key and q.get("key", [""])[0] != key:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"add ?key=YOUR_REPORT_KEY to the address")
                return
            k = f"&key={urllib.parse.quote(key)}" if key else ""
            links = [("All coins", f"/?x=1{k}"), ("Last 24h", f"/?hours=24{k}")]
            links += [(c.upper(), f"/?coin={c}{k}") for c in COINS]
            links += [("Live log", f"/log?x=1{k}"), ("Market check", f"/check?x=1{k}")]
            nav = "".join(f'<a href="{h}">{t}</a>' for t, h in links)
            if u.path == "/log":
                body = "\n".join(reversed(RECENT_LOG)) or "(no log lines yet)"
            elif u.path == "/check":
                body = _capture(check)
            elif u.path == "/":
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

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", int(port)), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("report page listening on port %s", port)


async def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    r = Runner(db)
    log.info("Polymarket US 15-min logger + paper trader | coins: %s | $%.2f per order",
             ",".join(COINS), ORDER_USD)
    start_web()
    for ln in _capture(check).splitlines():
        log.info("check: %s", ln)
    await asyncio.gather(r.discover(), r.poll(), r.settle())


def check():
    """One-shot sanity check: are we finding markets and reading books?"""
    print("Looking up 15-minute Up/Down markets on Polymarket US...")
    raw = http_json("/v1/markets", {"categories": "crypto", "closed": "false", "limit": 200})
    mk = raw.get("markets", [])
    print(f"  crypto markets returned: {len(mk)}")
    sample = [x for x in mk if x.get("assetPriceTerms")][:1]
    if sample:
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
        print("  raw book:", json.dumps(http_json(f"/v1/markets/{urllib.parse.quote(cur[0]['slug'])}/book"))[:1500])
    print("Done. If every coin shows open windows, the logger is ready.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        check()
    else:
        asyncio.run(main())
