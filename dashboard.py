"""
Dashboard for the Polymarket paper trader: JSON summary + a single HTML page.
Served by logger.py at  /  (page)  and  /api  (data).
"""
import sqlite3
import time
from collections import defaultdict
from datetime import datetime


def _pnl_rows(db, mode, coin=None):
    """One row per (window, strategy): fills and profit/loss."""
    col = "filled_cautious" if mode == "cautious" else "filled_hopeful"
    q = f"""SELECT m.slug, m.coin, m.window_start, m.window_end, m.winner,
                   o.strategy, o.side, o.usd, o.shares, o.fee, o.{col}
            FROM paper_orders o JOIN markets m ON m.slug = o.slug
            WHERE m.winner IS NOT NULL AND (o.mode IS NULL OR o.mode = ?)""" + (" AND m.coin = ?" if coin else "")
    out = {}
    for slug, c, ws, we, winner, strat, side, usd, shares, fee, filled in db.execute(q, (mode, coin) if coin else (mode,)):
        k = (slug, strat)
        r = out.setdefault(k, {"slug": slug, "coin": c, "start": ws, "end": we, "winner": winner,
                               "strategy": strat, "fills": set(), "pnl": 0.0, "spent": 0.0})
        if filled is None:
            continue
        r["fills"].add(side)
        r["pnl"] += (shares if side == winner else 0.0) - usd - fee
        r["spent"] += usd + fee
    return list(out.values())


def summary(db_path, mode="cautious", coin=None, start_balance=100.0, strategy_names=()):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    now = time.time()
    coin = coin or None

    last, first, nsnap = db.execute("SELECT MAX(ts), MIN(ts), COUNT(*) FROM snapshots").fetchone()
    rows = _pnl_rows(db, mode, coin)

    # ---------- per strategy ----------
    strat = defaultdict(lambda: {"windows": 0, "both": 0, "one": 0, "none": 0, "spent": 0.0, "pnl": 0.0,
                                 "today": 0.0, "wins": 0, "losses": 0})
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    for r in rows:
        s = strat[r["strategy"]]
        s["windows"] += 1
        n = len(r["fills"])
        s["both" if n == 2 else "one" if n == 1 else "none"] += 1
        s["spent"] += r["spent"]
        s["pnl"] += r["pnl"]
        if r["end"] >= midnight:
            s["today"] += r["pnl"]
        if n:
            if r["pnl"] > 1e-9:
                s["wins"] += 1
            elif r["pnl"] < -1e-9:
                s["losses"] += 1
    strategies = []
    for name, s in strat.items():
        w = s["windows"] or 1
        traded = s["wins"] + s["losses"]
        strategies.append({
            "name": name, "windows": s["windows"],
            "both": s["both"] / w, "one": s["one"] / w, "none": s["none"] / w,
            "spent": s["spent"], "pnl": s["pnl"], "today": s["today"],
            "roi": (s["pnl"] / s["spent"]) if s["spent"] else None,
            "win_rate": (s["wins"] / traded) if traded else None,
        })
    if strategy_names:
        strategies = [s for s in strategies if s["name"] in set(strategy_names)]
    have = {s["name"] for s in strategies}
    for n in strategy_names:
        if n not in have:
            strategies.append({"name": n, "windows": 0, "both": 0, "one": 0, "none": 0, "spent": 0.0,
                               "pnl": 0.0, "today": 0.0, "roi": None, "win_rate": None})
    strategies.sort(key=lambda x: x["pnl"], reverse=True)

    # money tied up in windows that haven't settled yet
    col = "filled_cautious" if mode == "cautious" else "filled_hopeful"
    tied = dict(db.execute(f"""
        SELECT o.strategy, SUM(o.usd + o.fee) FROM paper_orders o JOIN markets m ON m.slug = o.slug
        WHERE m.winner IS NULL AND m.window_end > ? AND (o.{col} IS NOT NULL OR o.cancelled_at IS NULL)
          AND (o.mode IS NULL OR o.mode = ?)
        """ + (" AND m.coin = ?" if coin else "") + " GROUP BY o.strategy",
        (now - 3 * 3600, mode, coin) if coin else (now - 3 * 3600, mode)).fetchall())
    for s in strategies:
        s["equity"] = start_balance + s["pnl"]
        s.setdefault("drawdown", 0.0)
        s["cash"] = s["equity"] - (tied.get(s["name"]) or 0.0)

    # ---------- equity curves ----------
    by_end = defaultdict(lambda: defaultdict(float))
    for r in rows:
        by_end[r["end"]][r["strategy"]] += r["pnl"]
    names = [s["name"] for s in strategies]
    cum = {n: start_balance for n in names}
    peak = {n: start_balance for n in names}
    dd = {n: 0.0 for n in names}
    series = {n: [] for n in names}
    times = []
    for t in sorted(by_end):
        for n in names:
            cum[n] += by_end[t].get(n, 0.0)
            peak[n] = max(peak[n], cum[n])
            dd[n] = max(dd[n], (peak[n] - cum[n]) / peak[n] if peak[n] else 0.0)
            series[n].append(round(cum[n], 4))
        times.append(t)
    for s in strategies:
        s["drawdown"] = dd.get(s["name"], 0.0)
    step = max(1, len(times) // 300)
    if step > 1:
        idx = list(range(0, len(times), step))
        if idx[-1] != len(times) - 1:
            idx.append(len(times) - 1)
        times = [times[i] for i in idx]
        series = {n: [v[i] for i in idx] for n, v in series.items()}

    # ---------- windows ----------
    settled = db.execute("SELECT COUNT(*), SUM(winner='up') FROM markets WHERE winner IS NOT NULL"
                         + (" AND coin = ?" if coin else ""), (coin,) if coin else ()).fetchone()

    # ---------- live markets ----------
    live = []
    for slug, c, ws, we, ptb in db.execute(
            "SELECT slug, coin, window_start, window_end, price_to_beat FROM markets "
            "WHERE window_start <= ? AND window_end > ? ORDER BY coin", (now, now)):
        if coin and c != coin:
            continue
        snap = db.execute("SELECT bid, ask, last_px, ts FROM snapshots WHERE slug = ? ORDER BY ts DESC LIMIT 1",
                          (slug,)).fetchone()
        bid, ask, lastpx, ts = snap if snap else (None, None, None, None)
        orders = defaultdict(dict)
        col = "filled_cautious" if mode == "cautious" else "filled_hopeful"
        for st, side, price, filled, canc in db.execute(
                f"SELECT strategy, side, price, {col}, cancelled_at FROM paper_orders "
                "WHERE slug = ? AND (mode IS NULL OR mode = ?)", (slug, mode)):
            orders[st][side] = {"price": price, "filled": filled is not None, "cancelled": canc is not None}
        sig = None
        try:
            sig = db.execute("SELECT p_up, t_left, ts FROM closer_signals WHERE slug = ? ORDER BY ts DESC LIMIT 1",
                             (slug,)).fetchone()
        except sqlite3.OperationalError:
            pass
        live.append({"coin": c, "slug": slug, "left": we - now, "elapsed": now - ws,
                     "model_up": sig[0] if sig and now - sig[2] < 5 else None,
                     "up_bid": bid, "up_ask": ask,
                     "down_bid": (1 - ask) if ask is not None else None,
                     "down_ask": (1 - bid) if bid is not None else None,
                     "last": lastpx, "age": (now - ts) if ts else None, "ptb": ptb, "orders": orders})

    # ---------- recent results ----------
    recent = defaultdict(lambda: {"pnl": {}})
    for r in rows:
        k = r["slug"]
        recent[k].update({"coin": r["coin"], "end": r["end"], "winner": r["winner"]})
        recent[k]["pnl"][r["strategy"]] = {"pnl": r["pnl"], "fills": len(r["fills"])}
    recent = sorted(recent.values(), key=lambda x: (x["end"], x["coin"]), reverse=True)[:40]

    # ---------- spread ----------
    spread = {}
    for c, avg, n in db.execute(
            "SELECT m.coin, AVG(s.ask - s.bid), COUNT(*) FROM snapshots s JOIN markets m ON m.slug = s.slug "
            "WHERE s.ask IS NOT NULL AND s.bid IS NOT NULL AND s.ts > ? GROUP BY m.coin", (now - 6 * 3600,)):
        spread[c] = avg

    # ---------- model check: our odds vs the market's, ~30s before each close ----------
    check = {"n": 0}
    try:
        pick = {}
        q = ("SELECT s.slug, s.t_left, s.p_up, s.up_ask, s.down_ask, m.winner FROM closer_signals s "
             "JOIN markets m ON m.slug = s.slug WHERE m.winner IN ('up','down') AND s.t_left BETWEEN 15 AND 45"
             + (" AND m.coin = ?" if coin else ""))
        for slug, tl, p, ua, da, w in db.execute(q, (coin,) if coin else ()):
            if ua is None or da is None:
                continue
            if slug not in pick or abs(tl - 30) < abs(pick[slug][0] - 30):
                pick[slug] = (tl, p, (ua + 1 - da) / 2, 1.0 if w == "up" else 0.0)
        rows = list(pick.values())
        if rows:
            n = len(rows)
            bm = sum((p - y) ** 2 for _, p, _, y in rows) / n
            bk = sum((mk - y) ** 2 for _, _, mk, y in rows) / n
            buckets = []
            for lo, hi in ((0, .1), (.1, .3), (.3, .7), (.7, .9), (.9, 1.01)):
                b = [r for r in rows if lo <= r[1] < hi]
                if b:
                    buckets.append({"range": f"{int(lo*100)}–{min(100,int(hi*100))}%", "n": len(b),
                                    "model": sum(r[1] for r in b) / len(b),
                                    "market": sum(r[2] for r in b) / len(b),
                                    "actual": sum(r[3] for r in b) / len(b)})
            check = {"n": n, "brier_model": bm, "brier_market": bk, "buckets": buckets}
    except sqlite3.OperationalError:
        pass

    db.close()
    return {
        "check": check,
        "now": now, "mode": mode, "coin": coin, "start": start_balance,
        "status": {"age": (now - last) if last else None, "hours": ((last - first) / 3600) if last else 0,
                   "book_reads": nsnap, "settled": settled[0] or 0, "up_wins": settled[1] or 0},
        "strategies": strategies,
        "equity": {"times": times, "series": series},
        "live": live, "recent": recent, "spread": spread,
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polybot</title>
<style>
:root{--bg:#0d1016;--card:#151a22;--line:#262d38;--txt:#e8ecf2;--mut:#8a94a6;--grn:#3ecf8e;--red:#f2555a;--blu:#7aa2ff;--chip:#1d2430}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:28px;margin:0;display:flex;align-items:center;gap:12px}
.badge{font-size:14px;font-weight:600;color:var(--blu);background:#1c2a4a;padding:5px 12px;border-radius:999px}
.sub{color:var(--mut);margin-top:6px;font-size:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0 6px}
.btn{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:12px;padding:10px 16px;font-size:15px;cursor:pointer;text-decoration:none}
.btn.on{border-color:var(--blu);color:var(--blu)}
h2{font-size:14px;letter-spacing:.12em;color:var(--mut);margin:30px 0 12px;text-transform:uppercase}
.tiles{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(min-width:700px){.tiles{grid-template-columns:repeat(4,1fr)}}
.tile{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
.tile .k{color:var(--mut);font-size:14px}
.tile .v{font-size:26px;font-weight:600;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .s{color:var(--mut);font-size:12px;margin-top:2px}
.pos{color:var(--grn)}.neg{color:var(--red)}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px;overflow:hidden}
svg{display:block;width:100%;height:auto}
.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:10px;font-size:13px;color:var(--mut)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.tbl{width:100%;border-collapse:collapse;font-size:14px}
.tbl th{color:var(--mut);font-weight:500;text-align:right;padding:8px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl td{text-align:right;padding:10px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tbl tr:last-child td{border-bottom:0}
.scroll{overflow-x:auto}
.mk{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(min-width:700px){.mk{grid-template-columns:repeat(4,1fr)}}
.mk .tile .v{font-size:20px}
.bar{height:6px;background:var(--chip);border-radius:3px;margin:10px 0 8px;overflow:hidden}
.bar b{display:block;height:100%;background:var(--blu)}
.dots{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.dot{font-size:11px;padding:3px 7px;border-radius:6px;background:var(--chip);color:var(--mut)}
.dot.f{background:#163a2b;color:var(--grn)}
.pill{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.up{background:#163a2b;color:var(--grn)}.pill.down{background:#3a1a1d;color:var(--red)}
.note{color:var(--mut);font-size:13px;margin-top:10px}
.live{border-radius:16px;padding:16px 18px;margin-top:16px;border:1px solid var(--line);background:var(--card)}
.live.on{border-color:#2f7d5b}.live.dry{border-color:#8a6d2a}.live.bad{border-color:#7d2f33}
.live h3{margin:0 0 4px;font-size:16px;display:flex;align-items:center;gap:8px}
.lv{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.lv div b{display:block;font-size:20px;margin-top:2px}
.lv div span{color:var(--mut);font-size:13px}
.sw{min-width:64px;padding:8px 10px;border-radius:999px;border:1px solid var(--line);background:var(--chip);color:var(--mut);font-weight:700;font-size:13px;cursor:pointer}
.sw-on{background:#2a2410;border-color:#8a6d2a;color:#f5b84a}.sw-live{background:#163a2b;border-color:#2f7d5b;color:var(--grn)}
.cool{margin-top:12px;padding:12px 14px;border-radius:12px;background:#2a2410;border:1px solid #8a6d2a;color:#f5d38a}
details.risk{margin-top:14px;border-top:1px solid var(--line);padding-top:12px}
details.risk summary{cursor:pointer;color:var(--txt);font-weight:600}
.form{display:grid;gap:10px;margin-top:10px}
.form label{display:flex;flex-direction:column;gap:4px;color:var(--mut);font-size:13px}
.form input{background:var(--chip);border:1px solid var(--line);color:var(--txt);border-radius:10px;padding:10px;font-size:16px}
.btn.red{border-color:#7d2f33;color:var(--red)}.btn.grn{border-color:#2f7d5b;color:var(--grn)}
.err{background:#3a1a1d;color:var(--red);padding:12px;border-radius:12px;margin-top:14px;display:none}
</style></head>
<body><div class="wrap">
  <h1>Polybot <span class="badge" id="modebadge">PAPER</span> <span class="badge" id="label" style="display:none"></span></h1>
  <div class="sub" id="sub">Loading…</div>
  <div class="err" id="err"></div>
  <div id="livebox"></div>

  <div class="row" id="coins"></div>
  <div class="row" id="modes"></div>

  <h2>Account</h2>
  <div class="row" id="strs" style="margin-top:0"></div>
  <div class="tiles" id="tiles"></div>

  <h2>Equity by strategy ($)</h2>
  <div class="card"><div id="chart"></div><div class="legend" id="legend"></div></div>

  <h2>Strategies</h2>
  <div class="card scroll"><table class="tbl" id="strats"></table></div>
  <div class="note">At most one bet per window, in the last 2 minutes. Windows = windows where it bet.</div>

  <h2>Live markets</h2>
  <div class="mk" id="live"></div>

  <h2>Model check</h2>
  <div class="card" id="check"></div>
  <div class="note">Compares our odds with the market's, 30 seconds before each close. Score = average squared error (lower is better). If the model's score isn't lower than the market's, there's no edge.</div>

  <h2>Recent windows</h2>
  <div class="card scroll"><table class="tbl" id="recent"></table></div>

  <div class="row" style="margin-top:28px" id="links"></div>
</div>
<script>
const qs = new URLSearchParams(location.search);
const KEY = qs.get("key") || "";
let coin = qs.get("coin") || "";
let mode = qs.get("mode") || "cautious";
let pick = qs.get("strategy") || "";
let last = null;
const COINS = ["", "btc", "eth", "sol", "xrp"];
let START = 100;
const COLORS = ["#7aa2ff","#3ecf8e","#f5b84a","#c58af9","#f2555a","#4fd1c5","#e879a7","#a3e635","#fb923c"];
const k = KEY ? "key=" + encodeURIComponent(KEY) : "";
const $ = id => document.getElementById(id);
const money = (v, d=2) => v == null ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(d);
const smoney = v => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2);
const cls = v => v > 1e-9 ? "pos" : v < -1e-9 ? "neg" : "";
const pct = v => v == null ? "–" : (v * 100).toFixed(0) + "%";
const spct = v => v == null ? "–" : (v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const c2 = v => v == null ? "–" : Math.round(v * 100) + "¢";
const fmtT = t => new Date(t * 1000).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
const mmss = s => s == null ? "–" : Math.max(0, Math.floor(s / 60)) + ":" + String(Math.max(0, Math.floor(s % 60))).padStart(2, "0");

function chips() {
  $("coins").innerHTML = COINS.map(c => `<button class="btn ${c===coin?'on':''}" data-c="${c}">${c ? c.toUpperCase() : "All coins"}</button>`).join("");
  $("modes").innerHTML = [["cautious","Cautious fills"],["hopeful","Hopeful fills"]]
    .map(([m,t]) => `<button class="btn ${m===mode?'on':''}" data-m="${m}">${t}</button>`).join("");
  document.querySelectorAll("[data-c]").forEach(b => b.onclick = () => { coin = b.dataset.c; chips(); load(); });
  document.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { mode = b.dataset.m; chips(); load(); });
  $("links").innerHTML = [["Text report","/report"],["Live log","/log"],["Market check","/check"]]
    .map(([t,u]) => `<a class="btn" href="${u}?${k}">${t}</a>`).join("");
}

function tile(k, v, c="", s="") { return `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div>${s?`<div class="s">${s}</div>`:""}</div>`; }

function chart(eq, names) {
  const W = 420, H = 230, P = {l: 50, r: 8, t: 10, b: 24};
  if (!eq.times.length) { $("chart").innerHTML = `<div class="mut" style="padding:40px 8px;text-align:center">The chart starts after the first 15-minute window settles.</div>`; $("legend").innerHTML=""; return; }
  let lo = Infinity, hi = -Infinity;
  names.forEach(n => eq.series[n].forEach(v => { lo = Math.min(lo, v); hi = Math.max(hi, v); }));
  lo = Math.min(lo, START); hi = Math.max(hi, START);
  if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
  const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
  const N = eq.times.length;
  const x = i => P.l + (N === 1 ? (W - P.l - P.r) / 2 : i * (W - P.l - P.r) / (N - 1));
  const y = v => P.t + (hi - v) * (H - P.t - P.b) / (hi - lo);
  let g = "";
  for (let j = 0; j <= 4; j++) {
    const v = lo + (hi - lo) * j / 4, yy = y(v);
    g += `<line x1="${P.l}" x2="${W-P.r}" y1="${yy}" y2="${yy}" stroke="#262d38" stroke-dasharray="3 4"/>`;
    g += `<text x="${P.l-8}" y="${yy+4}" fill="#8a94a6" font-size="12" text-anchor="end">${money(v)}</text>`;
  }
  g += `<line x1="${P.l}" x2="${W-P.r}" y1="${y(START)}" y2="${y(START)}" stroke="#4a5363"/>`;
  [0, N - 1].forEach((i, j) => { if (j && N < 2) return; g += `<text x="${x(i)}" y="${H-8}" fill="#8a94a6" font-size="12" text-anchor="${j?'end':'start'}">${fmtT(eq.times[i])}</text>`; });
  names.forEach((n, i) => {
    const pts = eq.series[n].map((v, j) => `${x(j).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    g += N === 1 ? `<circle cx="${x(0)}" cy="${y(eq.series[n][0])}" r="4" fill="${COLORS[i%COLORS.length]}"/>`
                 : `<polyline points="${pts}" fill="none" stroke="${COLORS[i%COLORS.length]}" stroke-width="2" stroke-linejoin="round"/>`;
  });
  $("chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}">${g}</svg>`;
  $("legend").innerHTML = names.map((n, i) => `<span><i style="background:${COLORS[i%COLORS.length]}"></i>${n} <b class="${cls(eq.series[n].at(-1) - START)}">${money(eq.series[n].at(-1))}</b></span>`).join("");
}

const OPEN_WHY = new Set();   // keeps "full details" open across refreshes
function liveBox(L, d) {
  const el = $("livebox");
  const a = document.activeElement;
  if (a && a.closest && a.closest("#livebox details.risk")) return;   // don't redraw while editing limits
  if (!L) { el.innerHTML = ""; return; }
  if (L.mode === "off") {
    el.innerHTML = `<div class="live"><h3>Live trading: not connected</h3><div class="mut">No Polymarket API keys on the server. Re-run setup to add them.</div></div>`;
    return;
  }
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
  const whyHtml = (w, k) => {
    const i = w.indexOf(" | order reply");
    const short = i > 0 ? w.slice(0, i) : w.slice(0, 200);
    const more = i > 0 ? w.slice(i + 3) : w.slice(200);
    return `<br><span class="mut" style="white-space:normal">${esc(short)}</span>` +
      (more ? `<details data-k="${k}" ${OPEN_WHY.has(String(k)) ? "open" : ""} class="mut" style="white-space:normal;word-break:break-all"><summary>full details</summary>${esc(more)}</details>` : "");
  };
  if (L.mode === "error") {
    el.innerHTML = `<div class="live bad"><h3><span class="neg">●</span> Live trading: key problem</h3><div class="mut">${L.auth_error || "Could not use the API keys."}</div></div>`;
    return;
  }
  const on = L.armed;
  const S = L.settings || {}, SL = L.setting_labels || {};
  const cd = on && L.cooldown_until && L.cooldown_until * 1000 > Date.now();
  const bal = L.balance != null ? money(L.balance) : "–";
  const chg = (L.balance != null && L.start_balance != null) ? L.balance - L.start_balance : null;
  const paper = Object.fromEntries((d.strategies || []).map(s => [s.name, s]));
  const names = Object.keys(L.sizes || {});
  const rows = names.map(n => {
    const sel = L.bots.includes(n), pb = L.per_bot[n] || {pnl: 0, bets: 0}, pp = paper[n];
    return `<tr><td><b>${n}</b><br><span class="mut">$${L.sizes[n]} per bet</span></td>
      <td>${pp ? `<span class="${cls(pp.pnl)}">${smoney(pp.pnl)}</span>` : "–"}<br><span class="mut">paper</span></td>
      <td>${pb.bets ? `<span class="${cls(pb.pnl)}">${smoney(pb.pnl)}</span>` : "–"}<br><span class="mut">live ${pb.bets || 0} bets</span></td>
      <td><button class="sw ${sel ? (on ? "sw-live" : "sw-on") : ""}" data-bot="${n}">${sel ? (on ? "LIVE" : "ON") : "OFF"}</button></td></tr>`;
  }).join("");
  const recent = (L.recent || []).slice(0, 12).map(r => {
    const what = r.mode === "dry-run" ? "would buy" : (r.filled ? "bought" : (r.state === "SKIPPED" ? "skipped" : (r.state === "DROPPED" ? "dropped" : r.state === "INTERRUPTED" ? "check Polymarket" : r.state === "SENDING" ? "sending..." : (r.error && r.error.startsWith("rejected") && !/EXPIRED|CANCELED/.test(r.error) ? "rejected" : "missed"))));
    const why = !r.filled && r.mode !== "dry-run" ? (r.error || (r.state === "ORDER_STATE_CANCELED" ? "price moved before it arrived" : (r.state || ""))) : "";
    const res = r.pnl != null ? `<span class="${cls(r.pnl)}">${smoney(r.pnl)}</span>` : (r.filled ? "open" : "");
    return `<tr><td>${fmtT(r.ts)} ${r.coin.toUpperCase()} ${r.side.toUpperCase()}<br><span class="mut">${r.bot || ""}</span></td><td>${what} @ ${c2(r.filled ? r.cost : r.seen)}${why ? whyHtml(why, r.ts) : ""}</td><td>${res}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="live ${on ? "on" : "dry"}">
    <h3><span class="${on ? "pos" : ""}" style="${on ? "" : "color:#f5b84a"}">●</span> ${on ? "LIVE TRADING ON - real money" : "Live trading OFF"}</h3>
    <div class="mut">${on ? "Selected bots place real bets." : "Selected bots only log the bets they would place."} Safety limits below.</div>
    ${cd ? `<div class="cool"><b>⏸ Cooling down until ${fmtT(L.cooldown_until)}</b><br>${L.cooldown_reason}. No real bets until then.<div class="row" style="margin:10px 0 0"><button class="btn" id="resume">Resume now</button></div></div>` : ""}
    <div class="row" style="margin:14px 0 4px"><button class="btn ${on ? "red" : "grn"}" id="master">${on ? "Turn live trading OFF" : "Turn live trading ON"}</button></div>
    <div class="lv">
      <div><span>Account balance</span><b>${bal}</b>${chg != null ? `<span class="${cls(chg)}">${smoney(chg)} since start</span>` : ""}</div>
      <div><span>Live P/L (settled)</span><b class="${cls(L.pnl)}">${smoney(L.pnl)}</b><span>today ${smoney(L.today_pnl)} (stops at -$${S.loss_limit}) · ${L.filled}/${L.sent} filled</span></div>
    </div>
    <div class="scroll" style="margin-top:12px"><table class="tbl">${rows}</table></div>
    <details class="risk"${window.__riskOpen ? " open" : ""}><summary>Safety limits</summary>
      <div class="form">${Object.keys(SL).map(k => `<label>${SL[k]}<input type="number" inputmode="decimal" min="0" step="any" id="set_${k}" value="${S[k]}"></label>`).join("")}</div>
      <div class="row" style="margin:10px 0 0"><button class="btn grn" id="saveset">Save limits</button></div>
      <div class="note">Limits apply to real-money bets only. Cooldowns count losses in a row (or $ lost in the last hour) across all live bots, then pause real betting for the set time.</div>
    </details>
    ${recent ? `<div class="mut" style="margin-top:14px">Recent</div><div class="scroll"><table class="tbl">${recent}</table></div>` : ""}
  </div>`;
  el.querySelectorAll("details[data-k]").forEach(x => x.addEventListener("toggle", () =>
    x.open ? OPEN_WHY.add(x.dataset.k) : OPEN_WHY.delete(x.dataset.k)));
  $("master").onclick = async () => {
    if (!on) {
      if (!L.bots.length) { alert("Switch at least one bot ON first."); return; }
      const list = L.bots.map(n => `  ${n}: $${L.sizes[n]} per bet`).join("\n");
      if (!confirm(`Start REAL-MONEY trading?\n\n${list}\n\nStops for the day if today's live result reaches -$${S.loss_limit}, or after ${S.max_bets} bets. Cooldown after ${S.cd_losses || "-"} losses in a row.`)) return;
    }
    await setLive({armed: !on});
  };
  const det = el.querySelector("details.risk");
  if (det) det.ontoggle = () => { window.__riskOpen = det.open; };
  $("saveset").onclick = async () => {
    const settings = {};
    for (const k of Object.keys(SL)) settings[k] = $("set_" + k).value;
    await setLive({settings}); alert("Limits saved.");
  };
  if ($("resume")) $("resume").onclick = async () => { if (confirm("End the cooldown and allow real bets again now?")) await setLive({end_cooldown: true}); };
  document.querySelectorAll("[data-bot]").forEach(b => b.onclick = async () => {
    const n = b.dataset.bot, sel = L.bots.includes(n);
    if (!sel && on && !confirm(`${n} will start placing REAL $${L.sizes[n]} bets right away. Continue?`)) return;
    await setLive({bots: sel ? L.bots.filter(x => x !== n) : L.bots.concat([n])});
  });
}

async function setLive(body) {
  try {
    const r = await fetch(`/live/set?${k}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || ("error " + r.status));
    if (last) { last.trader = j; render(last); }
  } catch (e) { alert("Couldn't change live trading: " + e.message); }
}

function render(d) {
  liveBox(d.trader, d);
  const s = d.status;
  const alive = s.heartbeat_age == null ? (s.age != null && s.age < 60) : s.heartbeat_age < 30;
  const fresh = s.age != null && s.age < 60;
  const state = !alive ? '<span class="neg">● stalled</span>'
              : fresh ? '<span class="pos">● running</span>'
              : '<span class="pos">● running</span> <span class="mut">(between windows)</span>';
  $("sub").innerHTML = `Updated ${new Date(d.now*1000).toLocaleTimeString()} · ${state} · ${s.hours.toFixed(1)}h of data`;
  const T = d.trader, liveOn = T && T.armed && T.mode === "live" && (T.bots || []).length;
  $("modebadge").textContent = liveOn ? "LIVE" : "PAPER";
  $("modebadge").style.cssText = liveOn ? "color:var(--grn);background:#163a2b" : "";
  const st = d.strategies;
  START = d.start;
  if (d.label) { $("label").textContent = d.label; $("label").style.display = ""; }
  if (!st.find(x => x.name === pick)) pick = st.length ? st[0].name : "";
  $("strs").innerHTML = st.map(x => `<button class="btn ${x.name===pick?'on':''}" data-s="${x.name}">${x.name}</button>`).join("");
  document.querySelectorAll("[data-s]").forEach(b => b.onclick = () => { pick = b.dataset.s; render(last); });
  const a = st.find(x => x.name === pick);
  $("tiles").innerHTML = a ? (
    tile("Equity", money(a.equity), "", `started with ${money(d.start, 0)}`) +
    tile("Return", spct(a.pnl / d.start), cls(a.pnl)) +
    tile("Today", smoney(a.today), cls(a.today)) +
    tile("Drawdown", (a.drawdown * 100).toFixed(1) + "%", a.drawdown > 0.1 ? "neg" : "") +
    tile("Cash", money(a.cash), "", "equity minus open orders") +
    tile("Win rate", pct(a.win_rate), "", "windows with a profit") +
    tile("Bets placed", a.windows.toLocaleString(), "", s.settled ? `Up won ${pct(s.up_wins / s.settled)}` : "waiting for first result") +
    tile("Avg profit per bet", a.windows ? smoney(a.pnl / a.windows) : "–", cls(a.pnl), "after fees")
  ) : "";

  chart(d.equity, st.map(x => x.name));

  $("strats").innerHTML = `<tr><th>Strategy</th><th>Equity</th><th>Windows</th><th>Both</th><th>One</th><th>None</th><th>Win rate</th><th>P/L</th><th>ROI</th></tr>` +
    (st.length ? st.map(x => `<tr><td>${x.name}</td><td class="${cls(x.pnl)}">${money(x.equity)}</td><td>${x.windows}</td><td>${pct(x.both)}</td><td>${pct(x.one)}</td><td>${pct(x.none)}</td><td>${pct(x.win_rate)}</td><td class="${cls(x.pnl)}">${smoney(x.pnl)}</td><td class="${cls(x.pnl)}">${spct(x.roi)}</td></tr>`).join("")
               : `<tr><td colspan="9" class="mut">No settled windows yet.</td></tr>`);

  $("live").innerHTML = d.live.length ? d.live.map(m => {
    const sp = (m.up_ask != null && m.up_bid != null) ? m.up_ask - m.up_bid : null;
    const dots = Object.entries(m.orders).map(([n, o]) => {
      const f = (o.up && o.up.filled ? "UP" : "") + (o.down && o.down.filled ? "DOWN" : "");
      if (!f) return "";
      const px = (o.up && o.up.filled ? o.up.price : o.down.price);
      return `<span class="dot f">${n} bought ${f} @ ${c2(px)}</span>`;
    }).join("");
    return `<div class="tile"><div class="k">${m.coin.toUpperCase()} · ${mmss(m.left)} left</div>
      <div class="bar"><b style="width:${Math.min(100, m.elapsed / 9)}%"></b></div>
      <div class="v"><span class="pos">↑ ${c2(m.up_ask)}</span> <span class="neg">↓ ${c2(m.down_ask)}</span></div>
      <div class="s">${m.model_up != null ? `model says Up <b>${(m.model_up*100).toFixed(0)}%</b> · market ${c2(m.up_ask)}` : (m.left > 120 ? `model starts with 2:00 left` : `model warming up`)}</div>
      <div class="dots">${dots || '<span class="dot">no bet this window yet</span>'}</div></div>`;
  }).join("") : `<div class="tile mut">No live 15-minute windows right now.</div>`;

  const ck = d.check || {n: 0};
  $("check").innerHTML = ck.n ? `
    <div class="tiles" style="margin-bottom:12px">
      ${tile("Model score", ck.brier_model.toFixed(4), ck.brier_model < ck.brier_market ? "pos" : "neg")}
      ${tile("Market score", ck.brier_market.toFixed(4))}
    </div>
    <div class="scroll"><table class="tbl"><tr><th>Model said Up</th><th>Windows</th><th>Model avg</th><th>Market avg</th><th>Actually Up</th></tr>
    ${ck.buckets.map(b => `<tr><td>${b.range}</td><td>${b.n}</td><td>${pct(b.model)}</td><td>${pct(b.market)}</td><td>${pct(b.actual)}</td></tr>`).join("")}
    </table></div><div class="note">${ck.n} windows checked</div>`
    : `<div class="mut" style="padding:10px">Fills in after the first windows close (the model needs ~45 min of price history first).</div>`;

  const names = st.map(x => x.name);
  $("recent").innerHTML = `<tr><th>Window</th><th>Result</th>${names.map(n => `<th>${n.replace("closer_strict","strict")}</th>`).join("")}</tr>` +
    (d.recent.length ? d.recent.map(r => `<tr><td>${r.coin.toUpperCase()} ${fmtT(r.end - 900)}</td><td><span class="pill ${r.winner}">${r.winner.toUpperCase()}</span></td>${names.map(n => { const p = r.pnl[n]; return `<td class="${p ? cls(p.pnl) : 'mut'}">${p && p.fills ? smoney(p.pnl) : "–"}</td>`; }).join("")}</tr>`).join("")
                     : `<tr><td colspan="9" class="mut">Nothing settled yet.</td></tr>`);
}

async function load() {
  try {
    const r = await fetch(`/api?mode=${mode}&coin=${coin}&${k}`);
    if (!r.ok) throw new Error(r.status === 403 ? "Wrong or missing key in the address." : "Server error " + r.status);
    last = await r.json(); render(last);
    $("err").style.display = "none";
  } catch (e) { $("err").textContent = e.message; $("err").style.display = "block"; }
}
chips(); load(); setInterval(load, 10000);
</script></body></html>"""
