"""
Live price streams: prices are pushed to the bot the instant they change, instead of the bot
asking once or twice a second.

  SpotStream  - Coinbase ticker (coin prices), no account needed
  BookStream  - Polymarket US best bid / best ask for the live windows (uses your API key)

Both reconnect on their own. While a stream is down the bot keeps working from its normal
once-a-second checks, so a stream problem can make the bot slower, never blind.
Needs the 'websockets' package; without it the streams simply stay off.
"""
import asyncio
import base64
import json
import logging
import time

log = logging.getLogger("polylog")
logging.getLogger("websockets").setLevel(logging.WARNING)

try:
    import websockets
except ImportError:          # bot still runs, just without streams
    websockets = None

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
POLY_WS = "wss://api.polymarket.us/v1/ws/markets"


def _px(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def _connect(url, headers=None):
    kw = dict(ping_interval=15, ping_timeout=10, close_timeout=2, max_size=4 * 1024 * 1024)
    if not headers:
        return await websockets.connect(url, **kw)
    try:
        return await websockets.connect(url, additional_headers=headers, **kw)   # websockets >= 14
    except TypeError:
        return await websockets.connect(url, extra_headers=headers, **kw)        # websockets 13


class _Stream:
    name = "stream"

    def __init__(self):
        self.last_msg = 0.0
        self.connected = False
        self.msgs = 0
        self.reconnects = 0

    def healthy(self, max_quiet=10):
        return self.connected and time.time() - self.last_msg < max_quiet

    def status(self):
        return {"on": self.healthy(), "msgs": self.msgs, "reconnects": self.reconnects,
                "quiet_s": round(time.time() - self.last_msg, 1) if self.last_msg else None}

    async def run(self):
        if websockets is None:
            log.warning("%s: 'websockets' package not installed - using normal price checks only", self.name)
            return
        delay = 1
        while True:
            began = time.time()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if time.time() - began > 10:
                    delay = 1             # it had been working - reconnect right away
                log.warning("%s disconnected: %r - reconnecting in %ds", self.name, e, delay)
            self.connected = False
            self.reconnects += 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


class SpotStream(_Stream):
    name = "coin price stream"

    def __init__(self, products, on_price):
        super().__init__()
        self.products = products          # {"btc": "BTC-USD", ...}
        self.by_id = {v: k for k, v in products.items()}
        self.on_price = on_price          # on_price(coin, ts, price)

    async def _session(self):
        ws = await _connect(COINBASE_WS)
        try:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": list(self.products.values()),
                                      "channels": ["ticker"]}))
            log.info("%s connected", self.name)
            while True:
                raw = await asyncio.wait_for(ws.recv(), 30)
                now = time.time()
                self.last_msg = now
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "subscriptions":
                    self.connected = True
                elif t == "ticker":
                    self.connected = True
                    coin = self.by_id.get(msg.get("product_id"))
                    p = _px(msg.get("price"))
                    if coin and p:
                        self.msgs += 1
                        self.on_price(coin, now, p)
                elif t == "error":
                    raise RuntimeError(f"Coinbase said: {msg.get('message')} {msg.get('reason', '')}")
        finally:
            await ws.close()


class BookStream(_Stream):
    name = "Polymarket price stream"

    def __init__(self, key_id, key, wanted, on_book):
        super().__init__()
        self.key_id, self.key = key_id, key
        self.wanted = wanted              # function -> set of slugs we want right now
        self.on_book = on_book            # on_book(slug, ts, {'bid': x, 'ask': y}) - only what was sent
        self.subscribed = set()
        self.req = 0
        self.shown_sample = False

    def _headers(self):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(self.key.sign(f"{ts}GET/v1/ws/markets".encode())).decode()
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig}

    async def _subscriber(self, ws):
        while True:
            new = sorted(self.wanted() - self.subscribed)
            for i in range(0, len(new), 100):
                part = new[i:i + 100]
                self.req += 1
                await ws.send(json.dumps({"subscribe": {
                    "requestId": f"lite-{self.req}",
                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                    "marketSlugs": part}}))
                self.subscribed.update(part)
                log.info("%s: watching %s", self.name, ", ".join(part))
            await asyncio.sleep(0.5)

    async def _session(self):
        self.subscribed = set()
        ws = await _connect(POLY_WS, self._headers())
        sub = None
        try:
            self.connected = True
            self.last_msg = time.time()
            log.info("%s connected", self.name)
            sub = asyncio.create_task(self._subscriber(ws))
            if True:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), 30)
                    now = time.time()
                    self.last_msg = now
                    msg = json.loads(raw)
                    if "heartbeat" in msg:
                        continue
                    if msg.get("error"):
                        log.warning("%s error: %s", self.name, str(msg)[:300])
                        continue
                    d = msg.get("marketDataLite")
                    if d is None:
                        md = msg.get("marketData")
                        if md and md.get("offers") is not None and md.get("bids") is not None:
                            bids = [_px(l.get("px")) for l in md["bids"]]
                            offers = [_px(l.get("px")) for l in md["offers"]]
                            bids = [b for b in bids if b is not None]
                            offers = [o for o in offers if o is not None]
                            d = {"marketSlug": md.get("marketSlug"),
                                 "bestBid": max(bids) if bids else None,
                                 "bestAsk": min(offers) if offers else None}
                    if not d or not d.get("marketSlug"):
                        continue
                    if not self.shown_sample:
                        self.shown_sample = True
                        log.info("%s first update: %s", self.name, raw[:300] if isinstance(raw, str) else raw[:300])
                    self.msgs += 1
                    upd = {}
                    if "bestBid" in d:
                        upd["bid"] = _px(d.get("bestBid"))
                    if "bestAsk" in d:
                        upd["ask"] = _px(d.get("bestAsk"))
                    if upd:
                        self.on_book(d["marketSlug"], now, upd)
        finally:
            if sub:
                sub.cancel()
            await ws.close()
