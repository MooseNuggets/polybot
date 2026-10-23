"""
Live trading on Polymarket US: mirrors chosen paper bots with real orders.

Keys come from the server's environment (set by setup.sh):
  PM_KEY_ID / PM_SECRET    your Polymarket US API key        (never put these in GitHub)
  LIVE_DAILY_LOSS_LIMIT    stop for the day after losing this much      (default: 10)
  LIVE_MAX_BETS_PER_DAY    stop for the day after this many bets        (default: 40)

Which bots trade for real, and the master ON/OFF switch, are set from the dashboard and
saved in the database. Each live bot bets its own paper size ($1 / $5 / $10).
While the master switch is OFF, selected bots log the orders they *would* send (dry-run).

Orders are IMMEDIATE-OR-CANCEL limit orders at the exact price the bot saw: whatever is
available at that price or better fills instantly, the rest is cancelled. Never a worse price. Prices on Polymarket US are always quoted in Up ("YES") terms, so
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

import net
from datetime import datetime

log = logging.getLogger("polylog")
API = "https://api.polymarket.us"
MAX_SLIP = 0.04         # real orders may pay up to 4c more than the price the bot saw...
MIN_EDGE_AFTER_SLIP = 0.01   # ...but only while our odds still beat the price by 1c+ after fees
TAKER_THETA = 0.0695

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
        self.env_loss_limit = float(os.environ.get("LIVE_DAILY_LOSS_LIMIT", "10") or 10)
        self.env_max_bets = int(os.environ.get("LIVE_MAX_BETS_PER_DAY", "40") or 40)
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
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.executescript(SCHEMA)
        n = self.db.execute("UPDATE live_orders SET state='INTERRUPTED', error='bot restarted while sending - "
                            "check Polymarket activity for this order' WHERE state='SENDING'").rowcount
        if n:
            log.warning("%d live order(s) were mid-send during a restart - marked INTERRUPTED", n)
        self.q = queue.Queue()
        self.check_q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._checker, daemon=True).start()

    # ---------------- state ----------------
    def _get(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM live_state WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def _set(self, k, v):
        self._write("INSERT OR REPLACE INTO live_state VALUES(?,?)", (k, str(v)))

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
        try:
            _, raw = net.request(method, API + path, body=data, timeout=timeout, headers={
                "X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig,
                "Content-Type": "application/json"})
            return json.loads(raw) if raw else {}
        except net.HTTPError as e:
            detail = e.body[:400].decode(errors="replace")
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
                       m.winner, l.mode, l.state, l.seen_price, l.usd, l.error, l.latency_ms, m.settled_at
                FROM live_orders l LEFT JOIN markets m ON m.slug = l.slug
                WHERE l.ts >= ? ORDER BY l.ts""", (since,)).fetchall()

    @staticmethod
    def _row_pnl(r):
        filled, cost, comm, winner, side = r[5] or 0, r[6] or 0, r[7] or 0, r[8], r[4]
        if not filled or winner is None:
            return None
        return filled * ((1.0 if side == winner else 0.0) - cost) - comm

    # ---------------- risk settings (editable from the dashboard) ----------------
    SETTINGS = {   # name: (type, min, max, label)
        "loss_limit": (float, 0, 100000, "Daily loss limit ($ - stops when today's live result is down this much, 0 = off)"),
        "max_bets":   (int,   0, 10000,  "Daily bet cap (0 = off)"),
        "cd_losses":  (int,   0, 100,    "Losses in a row before cooldown (0 = off)"),
        "cd_dollars": (float, 0, 100000, "$ lost within 1 hour before cooldown (0 = off)"),
        "cd_minutes": (int,   1, 10080,  "Cooldown length (minutes)"),
    }

    def settings(self):
        defaults = {"loss_limit": self.env_loss_limit, "max_bets": self.env_max_bets,
                    "cd_losses": 3, "cd_dollars": 0, "cd_minutes": 60}
        out = {}
        for k, (typ, lo, hi, _) in self.SETTINGS.items():
            v = self._get("set_" + k)
            try:
                out[k] = typ(float(v)) if v not in (None, "") else defaults[k]
            except ValueError:
                out[k] = defaults[k]
        return out

    def save_settings(self, new):
        clean = {}
        for k, v in (new or {}).items():
            if k not in self.SETTINGS:
                raise ValueError(f"unknown setting: {k}")
            typ, lo, hi, label = self.SETTINGS[k]
            try:
                v = typ(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"{label}: not a number")
            if not lo <= v <= hi:
                raise ValueError(f"{label}: must be between {lo} and {hi}")
            clean[k] = v
        for k, v in clean.items():
            self._set("set_" + k, v)
        if clean:
            log.warning("risk settings changed: %s", ", ".join(f"{k}={v}" for k, v in clean.items()))

    def end_cooldown(self):
        self._set("cooldown_until", 0)
        self._set("streak_since", time.time())
        log.warning("cooldown ended from dashboard")

    def cooldown(self, now=None):
        """Returns (until_timestamp, reason) if trading is paused by a cooldown, else (0, '').
        Starts a new cooldown when the streak or fast-loss trigger is hit."""
        now = now or time.time()
        until = float(self._get("cooldown_until", 0) or 0)
        if until > now:
            return until, self._get("cooldown_reason", "")
        st = self.settings()
        since = float(self._get("streak_since", 0) or 0)
        settled = [(r, self._row_pnl(r)) for r in self._pnl_rows(since)
                   if r[9] == "live" and (r[5] or 0) > 0 and r[8] is not None and (r[15] or 0) > since]
        settled.sort(key=lambda x: x[0][15] or x[0][0])
        reason = ""
        if st["cd_losses"] > 0:
            streak = 0
            for _, p in reversed(settled):
                if p is not None and p < 0:
                    streak += 1
                else:
                    break
            if streak >= st["cd_losses"]:
                reason = f"{streak} losses in a row"
        if not reason and st["cd_dollars"] > 0:
            hour = sum(p for r, p in settled if p is not None and (r[15] or 0) >= now - 3600)
            if hour <= -st["cd_dollars"]:
                reason = f"lost ${-hour:.2f} in the last hour"
        if reason:
            until = now + st["cd_minutes"] * 60
            self._set("cooldown_until", until)
            self._set("cooldown_reason", reason)
            self._set("streak_since", now)
            log.warning("COOLDOWN for %d min: %s", st["cd_minutes"], reason)
            return until, reason
        return 0, ""

    def today_pnl(self, rows=None):
        """Settled live profit/loss since midnight (Alaska time on the server)."""
        if rows is None:
            rows = [r for r in self._pnl_rows(self._today_start()) if r[9] == "live"]
        return sum(p for p in (self._row_pnl(r) for r in rows) if p is not None)

    def _guards_ok(self, usd):
        st = self.settings()
        until, why = self.cooldown()
        if until:
            return False, f"cooling down until {datetime.fromtimestamp(until).strftime('%H:%M')} ({why})"
        rows = [r for r in self._pnl_rows(self._today_start()) if r[9] == "live"]
        bets = sum(1 for r in rows if (r[5] or 0) > 0)
        if st["max_bets"] > 0 and bets >= st["max_bets"]:
            return False, f"daily bet cap reached ({bets})"
        pnl = self.today_pnl(rows)
        if st["loss_limit"] > 0 and pnl <= -st["loss_limit"]:
            return False, f"daily loss limit hit: down ${-pnl:.2f} today (limit ${st['loss_limit']:.2f})"
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

    def _write(self, sql, args=()):
        """Database write with retries - a busy database must never lose a record of a real order."""
        for attempt in range(6):
            try:
                with self.lock:
                    cur = self.db.execute(sql, args)
                return cur.lastrowid
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) and "busy" not in str(e):
                    raise
                log.warning("database busy, retrying (%d)", attempt + 1)
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("database stayed busy - could not save live order record")

    def _record(self, **kw):
        cols = ",".join(kw)
        return self._write(f"INSERT INTO live_orders({cols}) VALUES({','.join('?' * len(kw))})", tuple(kw.values()))

    def _update(self, row_id, **kw):
        sets = ",".join(f"{k}=?" for k in kw)
        self._write(f"UPDATE live_orders SET {sets} WHERE id=?", tuple(kw.values()) + (row_id,))

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
        with self.lock:
            dup = self.db.execute("SELECT 1 FROM live_orders WHERE strategy=? AND slug=? AND mode='live' "
                                  "AND state NOT IN ('SKIPPED','DROPPED') LIMIT 1", (strategy, slug)).fetchone()
        if dup:
            log.warning("live bet skipped: %s already sent an order on %s", strategy, slug)
            return
        ok, why = self._guards_ok(usd)
        if not ok:
            self._record(mode="live", state="SKIPPED", filled_qty=0, error=why, **base)
            log.warning("live bet skipped: %s", why)
            return
        # wiggle room: allow paying up to MAX_SLIP more, as long as the bet stays profitable by our odds
        limit = price
        for c in range(int(round(MAX_SLIP * 100)), 0, -1):
            cand = round(price + c / 100, 2)
            if cand <= 0.99 and (p_model or 0) - cand - TAKER_THETA * cand * (1 - cand) >= MIN_EDGE_AFTER_SLIP:
                limit = cand
                break
        qty = math.floor(usd / limit * 100) / 100      # keep the bet at ~$usd even at the higher limit
        base["qty"] = qty
        yes_px = limit if side == "up" else round(1 - limit, 2)
        if limit > price:
            log.info("%s %s %s: seen %.2f, will pay up to %.2f (model %.0f%%)", strategy, coin.upper(),
                     side.upper(), price, limit, (p_model or 0) * 100)
        body = {
            "marketSlug": slug,
            "intent": "ORDER_INTENT_BUY_LONG" if side == "up" else "ORDER_INTENT_BUY_SHORT",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": f"{yes_px:.2f}", "currency": "USD"},
            "quantity": qty,
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "synchronousExecution": True,   # wait for the result, so the reply says exactly what happened
            "maxBlockTime": "1",
        }
        row = self._record(mode="live", state="SENDING", filled_qty=0, **base)   # saved before anything is sent
        if time.time() - ts > 10:
            self._update(row, state="DROPPED", error="too slow (signal >10s old) - not sent")
            log.warning("live bet not sent: signal went stale")
            return
        t0 = time.time()
        try:
            resp = self._req("POST", "/v1/orders", body)
        except Exception as e:
            self._update(row, state="ERROR", error=str(e)[:500], latency_ms=(time.time() - t0) * 1000)
            log.error("LIVE order error %s %s: %s", coin.upper(), side.upper(), e)
            return
        latency = (time.time() - t0) * 1000
        log.info("LIVE SENT %s %s %s limit %.2f in %.0fms (%.1fs after signal)", strategy, coin.upper(),
                 side.upper(), limit, latency, time.time() - ts)
        self.check_q.put((row, resp, latency, strategy, coin, side, price, slug))

    def _finish(self, row, resp, latency, strategy, coin, side, price, slug=None):
        """Find out how a sent order ended (runs on its own thread so new orders never wait for this)."""
        oid = resp.get("id")
        raw_post = json.dumps(resp, separators=(",", ":"))[:300]
        raw_get = ""
        filled, avg, comm, state, err = 0.0, None, 0.0, None, None
        ex_sh = ex_val = 0.0
        notes = []
        for ex in resp.get("executions") or []:
            if (ex.get("orderRejectReason") and "UNSPECIFIED" not in str(ex.get("orderRejectReason"))
                    and ex.get("type") not in ("EXECUTION_TYPE_EXPIRED", "EXECUTION_TYPE_CANCELED")):
                err = str(ex.get("orderRejectReason")).replace("ORD_REJECT_REASON_", "rejected: ")
            for k in ("type", "unsolicitedCancelReason", "text"):
                v = ex.get(k)
                if v and "UNSPECIFIED" not in str(v):
                    notes.append(str(v).replace("EXECUTION_TYPE_", "").replace("UNSOLICITED_CXL_REASON_", "cancel reason: "))
            sh, px = _f(ex.get("lastShares")) or 0.0, _f(ex.get("lastPx"))
            if sh > 0 and px is not None:
                ex_sh += sh
                ex_val += sh * px
        try:
            o = {}
            for wait in (0.25, 0.5, 1.0, 2.0):    # IOC settles almost instantly; check a few times
                time.sleep(wait)
                got = self._req("GET", f"/v1/order/{oid}") if oid else {}
                raw_get = json.dumps(got, separators=(",", ":"))[:300]
                o = (got or {}).get("order", {})
                if o.get("state") not in ("ORDER_STATE_NEW", "ORDER_STATE_PENDING_NEW", "ORDER_STATE_PENDING_RISK"):
                    break
            state = o.get("state")
            filled = _f(o.get("cumQuantity")) or 0.0
            avg = _f(o.get("avgPx"))
            comm = _f(o.get("commissionNotionalTotalCollected")) or 0.0
        except Exception as e:
            if "HTTP 404" in str(e):          # Polymarket doesn't keep orders that never traded
                raw_get = "order not kept by exchange (404)"
            else:
                err = (err + " | " if err else "") + f"status check failed: {e}"
        if not filled and ex_sh > 0:          # fall back to the fills reported with the order
            filled, avg = ex_sh, ex_val / ex_sh
        if state is None:
            state = "ORDER_STATE_REJECTED" if err else ("ORDER_STATE_FILLED" if filled else
                     ("ORDER_STATE_CANCELED" if "CANCELED" in notes else "UNKNOWN"))
        if not filled:
            notes = [n for n in notes if "UNDEFINED" not in n]
            said = ", ".join(dict.fromkeys(notes)) or "-"
            if "EXPIRED" in notes or "CANCELED" in notes:
                said += " (nothing for sale at our price)"
            mkt = self._market_now(slug, side)
            err = (err + " | " if err else "no fill. ") + f"Polymarket said: {said}. {mkt} | order reply: {raw_post} | status: {raw_get or '-'}"
        cost = None
        if filled and avg is not None:
            if side == "up":
                cost = avg
            else:   # the API may report Down fills in Up terms (0.07) or Down terms (0.93)
                cost = avg if abs(avg - price) < abs((1 - avg) - price) else 1 - avg
        self._update(row, order_id=oid, state=state, filled_qty=filled, cost_px=cost,
                     commission=comm, latency_ms=latency, error=err)
        if filled:
            log.info("LIVE FILLED " + strategy + " %s %s %.2f shares at %.3f (saw %.2f) fee $%.2f, %.0fms",
                     coin.upper(), side.upper(), filled, cost or 0, price, comm, latency)
        else:
            log.info("LIVE NOT FILLED %s %s at %.2f (%s) %.0fms %s",
                     coin.upper(), side.upper(), price, state, latency, err or "")
        self.balance_at = 0   # refresh balance soon

    @staticmethod
    def _market_now(slug, side):
        """Right after a miss: what was really on offer? Tells us if the price we saw was already gone."""
        if not slug:
            return ""
        try:
            d = net.get_json(f"https://gateway.polymarket.us/v1/markets/{slug}/book?_={int(time.time()*1000)}",
                             timeout=4, headers={"Cache-Control": "no-cache"})
            md = d.get("marketData", d)
            asks = sorted((_f(l.get("px")), _f(l.get("qty"))) for l in md.get("offers") or [] if _f(l.get("px")))
            bids = sorted(((_f(l.get("px")), _f(l.get("qty"))) for l in md.get("bids") or [] if _f(l.get("px"))), reverse=True)
            if side == "up":
                best = asks[0] if asks else None
            else:
                best = (round(1 - bids[0][0], 2), bids[0][1]) if bids else None
            if not best:
                return f"Market right after: nobody selling {side.upper()}."
            return f"Market right after: {side.upper()} cheapest {best[0]*100:.0f}c ({best[1]:.0f} available)."
        except Exception as e:
            return f"(market check failed: {e})"

    def _checker(self):
        while True:
            item = self.check_q.get()
            try:
                self._finish(*item)
            except Exception:
                log.exception("live result check error")
                try:
                    self._update(item[0], state="UNKNOWN", error="result check crashed - check Polymarket activity")
                except Exception:
                    pass

    def _worker(self):
        last_bal = 0
        if self.key is not None:
            self.refresh_balance()
            last_bal = time.time()
        while True:
            try:
                item = self.q.get(timeout=20)
            except queue.Empty:
                item = None
            try:
                # refresh the balance every 25s while live (also keeps the order connection warm)
                every = 25 if self.mode() == "live" else 120
                if self.key is not None and (time.time() - last_bal > every or (not item and self.balance_at == 0)):
                    self.refresh_balance()
                    last_bal = time.time()
                if item:
                    if time.time() - item[0] > 10:
                        log.warning("live bet dropped: signal is %ds old", time.time() - item[0])
                        ts, strategy, slug, coin, side, price, t_left, p_model, usd = item
                        self._record(ts=ts, strategy=strategy, slug=slug, coin=coin, side=side,
                                     seen_price=price, usd=usd, qty=0, t_left=t_left, p_model=p_model,
                                     mode="live", state="DROPPED", filled_qty=0, error="too slow (signal >10s old)")
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
        cd_until, cd_reason = self.cooldown() if self.armed() else (0, "")
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
            "settings": self.settings(),
            "setting_labels": {k: v[3] for k, v in self.SETTINGS.items()},
            "cooldown_until": cd_until, "cooldown_reason": cd_reason,
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
