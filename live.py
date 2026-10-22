"""
Live trading on Polymarket US: mirrors chosen paper bots with real orders.

Keys come from the server's environment (set by setup.sh):
  PM_KEY_ID / PM_SECRET    your Polymarket US API key        (never put these in GitHub)
  LIVE_DAILY_LOSS_LIMIT    stop for the day after losing this much      (default: 10)
  LIVE_MAX_BETS_PER_DAY    stop for the day after this many bets        (default: 40)

Which bots trade for real, and the master ON/OFF switch, are set from the dashboard and
saved in the database. Each live bot bets its own paper size ($1 / $5 / $10).
While the master switch is OFF, selected bots log the orders they *would* send (dry-run).

Orders are FILL-OR-KILL limit orders at the exact price the bot saw: they fill completely at
that price or not at all. Prices on Polymarket US are always quoted in Up ("YES") terms, so
buying Down at 0.93 is sent as BUY_SHORT with price 0.07. If that convention were ever wrong,
the order could only fail to fill - it can't buy at a worse price.
"""
import base64
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

log = logging.getLogger("polylog")
API = "https://api.polymarket.us"

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, strategy TEXT, slug TEXT, coin TEXT, side TEXT,
    seen_price REAL, usd REAL, qty REAL, mode TEXT,
    order_id TEXT, state TEXT, filled_qty REAL, cost_px REAL, commission REAL,
    latency_ms REAL, error TEXT, t_left REAL, p_model REAL
);
CREATE TABLE IF NOT EXISTS live_state (k TEXT PRIMARY KEY, v TEXT);
"""


def _f(x):
    if x is None:
        return None
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class LiveTrader:
    def __init__(self, db_path, strategies):
        self.key_id = os.environ.get("PM_KEY_ID", "").strip()
        secret = os.environ.get("PM_SECRET", "").strip()
        self.sizes = {st["name"]: float(st.get("usd", 1.0)) for st in strategies}
        self.loss_limit = float(os.environ.get("LIVE_DAILY_LOSS_LIMIT", "10") or 10)
        self.max_bets = int(os.environ.get("LIVE_MAX_BETS_PER_DAY", "40") or 40)
        self.key = None
        self.key_error = None
        if self.key_id and secret:
            try:
                from cryptography.hazmat.primitives.asymmetric import ed25519
                self.key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
            except Exception as e:
                self.key_error = f"secret key unreadable: {e!r}"
        self.auth_ok = None
        self.auth_error = None
        self.balance = None
        self.buying_power = None
        self.balance_at = 0
        self.lock = threading.Lock()
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=15)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---------------- state ----------------
    def _get(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM live_state WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _set(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO live_state VALUES(?,?)", (k, str(v)))
            self.db.commit()

    # ---------------- dashboard controls ----------------
    def armed(self):
        return self._get("armed", "0") == "1"

    def live_bots(self):
        return [b for b in (self._get("bots", "") or "").split(",") if b in self.sizes]

    def set_control(self, armed=None, bots=None):
        if bots is not None:
            unknown = [b for b in bots if b not in self.sizes]
            if unknown:
                raise ValueError(f"unknown bot(s): {', '.join(unknown)}")
            self._set("bots", ",".join(bots))
            log.warning("live bots set to: %s", ", ".join(bots) or "(none)")
        if armed is not None:
            if armed and self.mode() == "error":
                raise ValueError("API keys aren't working")
            self._set("armed", "1" if armed else "0")
            log.warning("LIVE TRADING %s from dashboard (bots: %s)", "ON" if armed else "OFF",
                        ", ".join(self.live_bots()) or "none")

    def stop(self, why="stopped from dashboard"):
        self.set_control(armed=False)

    def mode(self):
        if not self.key_id:
            return "off"
        if self.key is None or self.auth_ok is False:
            return "error"
        return "live" if self.armed() else "dry-run"

    # ---------------- HTTP ----------------
    def _req(self, method, path, body=None, timeout=8):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(self.key.sign(f"{ts}{method}{path}".encode())).decode()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig,
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "polylog/2.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read()[:400].decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}") from None

    def refresh_balance(self):
        if self.key is None:
            return
        try:
            data = self._req("GET", "/v1/account/balances")
            bals = data.get("balances") or []
            usd = next((b for b in bals if str(b.get("currency", "USD")).upper() == "USD"), bals[0] if bals else {})
            self.balance = _f(usd.get("currentBalance"))
            self.buying_power = _f(usd.get("buyingPower"))
            self.balance_at = time.time()
            if self.auth_ok is not True:
                log.info("Polymarket US API keys OK - balance $%.2f, buying power $%.2f",
                         self.balance or 0, self.buying_power or 0)
                if self._get("start_balance") is None and self.balance is not None:
                    self._set("start_balance", self.balance)
            self.auth_ok, self.auth_error = True, None
        except Exception as e:
            if self.auth_ok is not False:
                log.error("Polymarket US API key check failed: %s", e)
            self.auth_error = str(e)
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                self.auth_ok = False

    # ---------------- guards ----------------
    def _today_start(self):
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def _pnl_rows(self, since=0):
        with self.lock:
            return self.db.execute("""
                SELECT l.ts, l.strategy, l.slug, l.coin, l.side, l.filled_qty, l.cost_px, l.commission,
                       m.winner, l.mode, l.state, l.seen_price, l.usd, l.error, l.latency_ms
                FROM live_orders l LEFT JOIN markets m ON m.slug = l.slug
                WHERE l.ts >= ? ORDER BY l.ts""", (since,)).fetchall()

    @staticmethod
    def _row_pnl(r):
        filled, cost, comm, winner, side = r[5] or 0, r[6] or 0, r[7] or 0, r[8], r[4]
        if not filled or winner is None:
            return None
        return filled * ((1.0 if side == winner else 0.0) - cost) - comm

    def _guards_ok(self, usd):
        rows = [r for r in self._pnl_rows(self._today_start()) if r[9] == "live"]
        bets = sum(1 for r in rows if (r[5] or 0) > 0)
        if bets >= self.max_bets:
            return False, f"daily bet cap reached ({bets})"
        realized = sum(p for p in (self._row_pnl(r) for r in rows) if p is not None)
        open_risk = sum((r[5] or 0) * (r[6] or 0) for r in rows if (r[5] or 0) > 0 and r[8] is None)
        if realized - open_risk <= -self.loss_limit:
            return False, f"daily loss limit hit (${realized:.2f} settled, ${open_risk:.2f} open)"
        if time.time() - self.balance_at > 300:
            self.refresh_balance()
        if self.buying_power is not None and self.buying_power < usd + 0.25:
            return False, f"not enough buying power (${self.buying_power:.2f})"
        return True, ""

    # ---------------- orders ----------------
    def submit(self, strategy, m, side, price, t_left, p_model):
        """Called by the paper bot right after it places a paper bet."""
        if strategy not in self.live_bots() or self.mode() not in ("live", "dry-run"):
            return
        self.q.put((time.time(), strategy, m["slug"], m["coin"], side, float(price), t_left, p_model,
                    self.sizes[strategy]))

    def _record(self, **kw):
        cols = ",".join(kw)
        with self.lock:
            self.db.execute(f"INSERT INTO live_orders({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))
            self.db.commit()

    def _place(self, ts, strategy, slug, coin, side, price, t_left, p_model, usd):
        qty = math.floor(usd / price * 100) / 100
        base = dict(ts=ts, strategy=strategy, slug=slug, coin=coin, side=side, seen_price=price,
                    usd=usd, qty=qty, t_left=t_left, p_model=p_model)
        mode = self.mode()
        if mode == "dry-run":
            self._record(mode="dry-run", state="NOT SENT", filled_qty=0, **base)
            log.info("DRY-RUN %s would buy $%.0f %s %s at %.2f", strategy, usd, coin.upper(), side.upper(), price)
            return
        if mode != "live":
            return
        ok, why = self._guards_ok(usd)
        if not ok:
            self._record(mode="live", state="SKIPPED", filled_qty=0, error=why, **base)
            log.warning("live bet skipped: %s", why)
            return
        yes_px = price if side == "up" else round(1 - price, 2)
        body = {
            "marketSlug": slug,
            "intent": "ORDER_INTENT_BUY_LONG" if side == "up" else "ORDER_INTENT_BUY_SHORT",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{yes_px:.2f}", "currency": "USD"},
            "quantity": qty,
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "3",
        }
        t0 = time.time()
        try:
            resp = self._req("POST", "/v1/orders", body)
        except Exception as e:
            self._record(mode="live", state="ERROR", filled_qty=0, error=str(e)[:500],
                         latency_ms=(time.time() - t0) * 1000, **base)
            log.error("LIVE order error %s %s: %s", coin.upper(), side.upper(), e)
            return
        latency = (time.time() - t0) * 1000
        oid = resp.get("id")
        filled, avg, comm, state, err = 0.0, None, 0.0, None, None
        for ex in resp.get("executions") or []:
            if ex.get("orderRejectReason"):
                err = str(ex.get("orderRejectReason"))
        try:
            o = (self._req("GET", f"/v1/order/{oid}") or {}).get("order", {}) if oid else {}
            state = o.get("state")
            filled = _f(o.get("cumQuantity")) or 0.0
            avg = _f(o.get("avgPx"))
            comm = _f(o.get("commissionNotionalTotalCollected")) or 0.0
        except Exception as e:
            err = (err + " | " if err else "") + f"status check failed: {e}"
        cost = None
        if filled and avg is not None:
            if side == "up":
                cost = avg
            else:   # the API may report Down fills in Up terms (0.07) or Down terms (0.93)
                cost = avg if abs(avg - price) < abs((1 - avg) - price) else 1 - avg
        self._record(mode="live", order_id=oid, state=state, filled_qty=filled, cost_px=cost,
                     commission=comm, latency_ms=latency, error=err, **base)
        if filled:
            log.info("LIVE FILLED " + strategy + " %s %s %.2f shares at %.3f (saw %.2f) fee $%.2f, %.0fms",
                     coin.upper(), side.upper(), filled, cost or 0, price, comm, latency)
        else:
            log.info("LIVE NOT FILLED %s %s at %.2f (%s) %.0fms %s",
                     coin.upper(), side.upper(), price, state, latency, err or "")
        self.balance_at = 0   # refresh balance on next guard check

    def _worker(self):
        last_bal = 0
        if self.key is not None:
            self.refresh_balance()
            last_bal = time.time()
        while True:
            try:
                item = self.q.get(timeout=30)
            except queue.Empty:
                item = None
            try:
                if self.key is not None and time.time() - last_bal > 120:
                    self.refresh_balance()
                    last_bal = time.time()
                if item:
                    if time.time() - item[0] > 10:
                        log.warning("live bet dropped: signal is %ds old", time.time() - item[0])
                        continue
                    self._place(*item)
            except Exception:
                log.exception("live worker error")

    # ---------------- dashboard ----------------
    def snapshot(self):
        rows = self._pnl_rows(0)
        live = [r for r in rows if r[9] == "live"]
        sent = [r for r in live if r[10] not in ("SKIPPED",)]
        filled = [r for r in live if (r[5] or 0) > 0]
        settled = [(r, self._row_pnl(r)) for r in filled if r[8] is not None]
        today = self._today_start()
        start_bal = self._get("start_balance")
        per_bot = {}
        for r, p in settled:
            b = per_bot.setdefault(r[1], {"pnl": 0.0, "bets": 0, "wins": 0})
            b["pnl"] += p
            b["bets"] += 1
            b["wins"] += p > 0
        recent = []
        for r in reversed(rows[-25:]):
            p = self._row_pnl(r)
            recent.append({"ts": r[0], "bot": r[1], "coin": r[3], "side": r[4], "seen": r[11], "cost": r[6],
                           "filled": r[5] or 0, "mode": r[9], "state": r[10], "error": r[13],
                           "latency": r[14], "winner": r[8], "pnl": p})
        return {
            "mode": self.mode(), "armed": self.armed(), "bots": self.live_bots(),
            "sizes": self.sizes, "per_bot": per_bot,
            "loss_limit": self.loss_limit, "max_bets": self.max_bets,
            "auth_error": self.key_error or self.auth_error,
            "balance": self.balance, "buying_power": self.buying_power,
            "start_balance": float(start_bal) if start_bal else None,
            "sent": len(sent), "filled": len(filled),
            "pnl": sum(p for _, p in settled), "settled": len(settled),
            "wins": sum(1 for _, p in settled if p > 0),
            "today_pnl": sum(p for r, p in settled if r[0] >= today),
            "fees": sum(r[7] or 0 for r in filled),
            "avg_slip": (sum((r[6] or 0) - (r[11] or 0) for r in filled) / len(filled)) if filled else None,
            "recent": recent,
        }
