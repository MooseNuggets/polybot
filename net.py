"""
Fast HTTP: keeps connections open and reuses them (one per host per thread), so each request
skips the ~100-300 ms of connecting and TLS handshake. Falls back to a fresh connection once if
a kept-alive connection has gone stale.
"""
import http.client
import json
import ssl
import threading
import time
import urllib.parse

_local = threading.local()
IDLE_LIMIT = 40     # seconds; reconnect before use if a connection sat idle longer than this
_ctx = ssl.create_default_context()


class HTTPError(Exception):
    def __init__(self, code, reason, body=b""):
        super().__init__(f"HTTP {code}: {reason}")
        self.code = code
        self.reason = reason
        self.body = body


def _conn(scheme, host, timeout):
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    key = (scheme, host)
    c = pool.get(key)
    if c is None:
        if scheme == "https":
            c = http.client.HTTPSConnection(host, timeout=timeout, context=_ctx)
        else:
            c = http.client.HTTPConnection(host, timeout=timeout)
        pool[key] = c
    # an order should never ride on a connection the server may have quietly closed
    if getattr(c, "_last_used", None) and time.time() - c._last_used > IDLE_LIMIT and c.sock is not None:
        try:
            c.close()
        except Exception:
            pass
    c.timeout = timeout
    if c.sock is not None:
        c.sock.settimeout(timeout)
    return c


def _drop(scheme, host):
    pool = getattr(_local, "pool", {})
    c = pool.pop((scheme, host), None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def request(method, url, body=None, headers=None, timeout=8):
    """Returns (status, body_bytes). Raises HTTPError for 4xx/5xx."""
    u = urllib.parse.urlsplit(url)
    path = u.path + (("?" + u.query) if u.query else "")
    hdrs = {"User-Agent": "polylog/2.0", "Accept": "application/json", "Connection": "keep-alive"}
    hdrs.update(headers or {})
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    for attempt in (1, 2):
        c = _conn(u.scheme, u.netloc, timeout)
        reused = c.sock is not None
        try:
            c.request(method, path, body=data, headers=hdrs)
            r = c.getresponse()
            raw = r.read()
            c._last_used = time.time()
            if r.getheader("Connection", "").lower() == "close":
                _drop(u.scheme, u.netloc)
            if r.status >= 400:
                raise HTTPError(r.status, r.reason, raw)
            return r.status, raw
        except HTTPError:
            raise
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            _drop(u.scheme, u.netloc)
            # Only resend if this is safe: GETs always; anything else (an ORDER) only when a kept-alive
            # connection had already been closed by the server, so the request never got through.
            stale = reused and isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError))
            if attempt == 2 or (method != "GET" and not stale):
                raise


def get_json(url, timeout=8, headers=None):
    _, raw = request("GET", url, headers=headers, timeout=timeout)
    return json.loads(raw) if raw else {}
