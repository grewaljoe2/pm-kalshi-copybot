"""
Web app wrapper for the copy bot: runs the bot in a background thread and
serves a small password-protected dashboard (status, positions, trades, logs,
pause/resume).

Run locally:   python app.py
On Render:     gunicorn app:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
(Keep --workers 1, or you'd run several bots at once.)
"""
from __future__ import annotations

import collections
import hmac
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request

import copybot

# ---- logging: keep the last 300 lines in memory for the dashboard ---------- #
LOG_LINES: collections.deque[str] = collections.deque(maxlen=300)


class RingHandler(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(self.format(record))


fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%m-%d %H:%M:%S")
root = logging.getLogger()
root.setLevel(logging.INFO)
for h in (logging.StreamHandler(), RingHandler()):
    h.setFormatter(fmt)
    root.addHandler(h)
log = logging.getLogger("app")

# ---- background threads --------------------------------------------------- #
# Started lazily in the process that serves requests. Gunicorn may import this
# module in its master process and then fork a worker; threads don't survive a
# fork, so starting them at import time would run the bot where the dashboard
# can't see it (or twice). One process, one bot.
_THREADS: list[tuple[str, object]] = []
_STARTED_PID: int | None = None
_START_LOCK = threading.Lock()


def _register(name: str, fn) -> None:
    _THREADS.append((name, fn))


def start_background() -> None:
    global _STARTED_PID
    if _STARTED_PID == os.getpid():
        return
    with _START_LOCK:
        if _STARTED_PID == os.getpid():
            return
        _STARTED_PID = os.getpid()
        for name, fn in _THREADS:
            threading.Thread(target=fn, name=name, daemon=True).start()
        log.info("Background threads started in pid %d", os.getpid())



# ---- player props (rated from ESPN game logs, refreshed every 30 min) ------- #
PROPS: dict = {"picks": [], "updated": None, "error": None}


def _props_loop():
    import json
    from props import PropModel
    time.sleep(60)
    model = None
    while True:
        try:
            if BOT is not None:
                if model is None:
                    model = PropModel(lambda path, **p: BOT.kdata._req("GET", path, params=p),
                                      BOT._kalshi_start, BOT.fee_rate)
                picks = model.scan()
                PROPS.update(picks=picks, updated=time.time(), error=None)
                BOT.prop_picks = picks
                BOT.prop_picks_new = True
                log.info("PROPS top %s", json.dumps([{k: p[k] for k in (
                    "outcome", "ask", "p", "p_hist", "last10", "avg", "score")} for p in picks[:10]]))
        except Exception as e:
            PROPS["error"] = str(e)[:200]
            log.warning("props scan failed: %s", e)
        time.sleep(1800)


_register("props", _props_loop)


def _lag_loop():
    from lagwatch import LagWatch
    from kalshi_ws import KalshiStream
    time.sleep(90)
    while BOT is None:
        time.sleep(10)
    cfg = BOT.cfg.get("lag_watch", {}) or {}
    if not cfg.get("enabled", True):
        return
    lw = LagWatch(BOT, cfg)
    if BOT.kalshi.pkey:  # live Kalshi prices (websocket) for both watchers
        lw.ks = KalshiStream(BOT.kalshi).start()
    ccfg = BOT.cfg.get("crypto_lag", {}) or {}
    if ccfg.get("enabled", True):
        from crypto import CryptoLag
        cl = CryptoLag(BOT, lw, lw.ks, ccfg)
        threading.Thread(target=cl.run, args=(STOP,), name="crypto-lag", daemon=True).start()
    lw.run(STOP)


_register("lagwatch", _lag_loop)


def _series_probe():
    time.sleep(40)
    try:
        j = BOT.kdata._req("GET", "/series", params={"category": "Sports"})
        names = sorted(x["ticker"] for x in j.get("series", []) if x["ticker"].endswith(("GAME", "MATCH")))
        log.info("SERIES_PROBE %d: %s", len(names), " ".join(names))
    except Exception as e:
        log.info("SERIES_PROBE failed %s", e)


_register("series-probe", _series_probe)

# ---- bot thread ------------------------------------------------------------ #
BOT: copybot.CopyBot | None = None
BOT_ERROR: str | None = None
STOP = threading.Event()


def _bot_loop():
    global BOT, BOT_ERROR
    try:
        BOT = copybot.CopyBot(copybot.load_yaml(os.environ.get("CONFIG_PATH", "config.yaml")))
    except BaseException as e:  # includes SystemExit from bad live config
        BOT_ERROR = f"Bot failed to start: {e}"
        log.error(BOT_ERROR)
        return
    while not STOP.is_set():
        try:
            BOT.run(stop_event=STOP)
        except Exception as e:  # never let the thread die silently
            BOT.last_error = str(e)
            log.exception("bot loop crashed, restarting in 30s")
            STOP.wait(30)


_register("copybot", _bot_loop)


def _keep_awake():
    """Render's free plan sleeps after 15 min without visitors. When KEEP_AWAKE=1,
    visit our own public URL every 10 min so the bot keeps running."""
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    import requests
    while not STOP.wait(600):
        try:
            requests.get(url.rstrip("/") + "/healthz", timeout=20)
        except Exception as e:
            log.warning("keep-awake ping failed: %s", e)


if os.environ.get("KEEP_AWAKE") == "1":
    _register("keep-awake", _keep_awake)

# ---- web ------------------------------------------------------------------- #
app = Flask(__name__)
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def _authed() -> bool:
    a = request.authorization
    return bool(PASSWORD) and a is not None and hmac.compare_digest(a.password or "", PASSWORD)


PUBLIC = os.environ.get("PUBLIC_DASHBOARD") == "1"


@app.before_request
def require_auth():
    start_background()
    if request.path == "/healthz":
        return None
    # Public mode: no login while paper trading. Once live, the pause button needs the password again.
    if PUBLIC and (request.method == "GET" or os.environ.get("BOT_MODE", "paper") != "live"):
        return None
    if not PASSWORD:
        return Response("Set the DASHBOARD_PASSWORD environment variable.", 503)
    if not _authed():
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="copybot"'})
    if request.method == "POST":  # basic same-origin check for the pause button
        src = request.headers.get("Origin") or request.headers.get("Referer")
        if src and urlparse(src).netloc != request.host:
            abort(403)
    return None


@app.get("/healthz")
def healthz():
    return {"ok": True, "bot_running": BOT is not None and BOT_ERROR is None}


@app.get("/api/status")
def api_status():
    if BOT is None:
        return jsonify({"error": BOT_ERROR or "starting"}), 503
    return jsonify(BOT.status())


@app.post("/pause")
def pause():
    if BOT:
        BOT.set_paused(True)
    return redirect("/")


@app.post("/resume")
def resume():
    if BOT:
        BOT.set_paused(False)
    return redirect("/")


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago"


@app.get("/")
def index():
    st = BOT.status() if BOT else None
    return render_template_string(
        PAGE, st=st, err=BOT_ERROR, logs=list(LOG_LINES)[-80:][::-1],
        trades=BOT.recent_journal(40) if BOT else [], ago=_ago, slate=SLATE, best=BEST, props=PROPS, combos=(BOT.combo_slate if BOT else {}),
        now=datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    )


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Copy Bot</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1b;--mut:#6b6b66;--line:#e4e4df;--ok:#1f7a4d;--warn:#b26a00;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--mut:#9a9993;--line:#2e2e2b;--ok:#4cc38a;--warn:#e0a84a;--bad:#f2766b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 12px}h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
.k{font-size:12px;color:var(--mut)}.v{font-size:17px;font-weight:600}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:13px;font-weight:600;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button{font:inherit;font-weight:600;padding:10px 16px;border-radius:10px;border:1px solid var(--line);background:var(--ink);color:var(--bg);width:100%}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line)}
.scroll{overflow-x:auto}pre{margin:0;font-size:12px;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
</style></head><body><main>
<h1>Polymarket → Kalshi Copy Bot</h1>
<p style="margin:-6px 0 12px"><a href="/report" style="color:inherit">Whale report →</a></p>
{% if err %}<div class="card bad">{{ err }}</div>{% endif %}
{% if st %}
<div class="card">
  <div style="margin-bottom:10px">
  {% if st.mode == 'live' and st.kalshi_env == 'prod' %}<span class="pill bad">LIVE · REAL MONEY</span>
  {% elif st.mode == 'live' %}<span class="pill warn">LIVE · DEMO</span>
  {% else %}<span class="pill ok">PAPER</span>{% endif %}
  {% if st.paused %}<span class="pill warn">PAUSED</span>{% endif %}
  {% if st.kalshi_link.linked %}<span class="pill ok">Kalshi {{ st.kalshi_link.env }} connected ✓ · ${{ '%.2f'|format(st.kalshi_link.balance) }}</span>
  {% else %}<span class="pill warn">Kalshi not connected</span>{% endif %}
  {% if not st.kalshi_link.linked and st.kalshi_link.detail %}<div class="k" style="margin-top:6px">{{ st.kalshi_link.detail }}</div>{% endif %}
  </div>
  <div class="grid">
    <div><div class="k">Last check</div><div class="v">{{ ago(st.last_poll) }}</div></div>
    <div><div class="k">Spent today</div><div class="v">${{ '%.2f'|format(st.spent_today) }} / ${{ st.max_usd_per_day }}</div></div>
    <div><div class="k">Kalshi balance</div><div class="v">{{ '$%.2f'|format(st.balance) if st.balance is not none else '—' }}</div></div>
    <div><div class="k">Whales / mapped</div><div class="v">{{ st.whales|length }} / {{ st.mapped_markets }}</div></div>
  </div>
  {% if st.last_error %}<p class="bad" style="font-size:13px">Last error: {{ st.last_error }}</p>{% endif %}
  <form method="post" action="{{ '/resume' if st.paused else '/pause' }}" style="margin-top:12px">
    <button>{{ 'Resume copying' if st.paused else 'Pause copying' }}</button></form>
</div>
{% if st %}<div class="card"><h2>${{ '%.0f'|format(st.start_usd) }} bankroll ({{ 'paper' if st.mode != 'live' else 'live' }})</h2>
<div class="grid"><div><div class="k">Balance</div><div class="v {{ 'ok' if st.equity > st.start_usd else ('bad' if st.equity < st.start_usd else '') }}">${{ '%.2f'|format(st.equity) }}</div></div>
<div><div class="k">Free cash</div><div class="v">${{ '%.2f'|format(st.cash) }}</div>{% if st.reserve_usd %}<div class="k">reserve ${{ '%.2f'|format(st.reserve_free) }} of ${{ '%.0f'|format(st.reserve_usd) }} ready</div>{% endif %}</div>
<div><div class="k">Return</div><div class="v">{{ '%+.1f'|format(100 * (st.equity / st.start_usd - 1)) }}%</div></div>
<div><div class="k">Open bets</div><div class="v">{{ st.open_lots }}</div></div></div>
<div class="k" style="margin-top:8px">Stage: <b>{{ st.stage }}</b> · bet size {% for t in st.stage_tiers %}{{ t[0] }}+ → {{ '%g'|format(100*t[1]) }}%{% if not loop.last %}, {% endif %}{% endfor %} · peak ${{ '%.2f'|format(st.peak or st.start_usd) }}</div></div>{% endif %}
<div class="card"><h2>Best bets now</h2>
{% if best.bets %}<div class="scroll"><table><tr><th>Bet</th><th>Whales</th><th>Poly ¢ now</th><th>Kalshi ¢</th><th>Score</th><th>EV</th><th>Stake</th></tr>
{% for b in best.bets[:12] %}<tr><td>{{ b.title }}<br><span class="k">{{ b.outcome }} → {{ b.ticker }} {{ b.side|upper }}</span></td>
<td>{{ b.whales }}<br><span class="k">${{ '{:,}'.format(b.whale_usd) }}</span>{% if b.status == 'unknown' %}<br><span class="k">start time?</span>{% endif %}</td><td>{{ '%.0f'|format(100*b.whale_px) }}</td><td>{{ '%.0f'|format(100*b.ask) }}</td>
<td><b>{{ b.score }}</b></td><td class="{{ 'ok' if b.ev > 0 else 'bad' }}">{{ '%+.1f'|format(100*b.ev) }}%</td><td>{{ '$%.2f'|format(b.stake) if b.stake else '—' }}</td></tr>{% endfor %}
</table></div><div class="k" style="margin-top:6px">Whale bets from the last 24h on games that haven't started. EV compares Kalshi with Polymarket's price now. Refreshes every 10 min.</div>
{% else %}<div class="k">Building the list (a few minutes after a restart)…</div>{% endif %}</div>
<div class="card"><h2>Top player props (combo legs)</h2>
{% if props.picks %}<div class="scroll"><table><tr><th>Prop</th><th>Last 10</th><th>Avg</th><th>Hit chance</th><th>Kalshi ¢</th><th>Score</th></tr>
{% for p in props.picks[:12] %}<tr><td>{{ p.outcome }}<br><span class="k">{{ p.league }} · {{ p.ticker }}</span></td>
<td>{{ p.last10 }}<br><span class="k">{{ p.recent|join(', ') }}</span></td><td>{{ p.avg }}</td>
<td><b>{{ '%.0f'|format(100*p.p) }}%</b><br><span class="k">history {{ '%.0f'|format(100*p.p_hist) }}%</span></td>
<td>{{ '%.0f'|format(100*p.ask) }}</td><td class="{{ 'ok' if p.edge > 0 else '' }}"><b>{{ p.score }}</b></td></tr>{% endfor %}
</table></div><div class="k" style="margin-top:6px">Hit chance = how often the player cleared the line in his last {{ 'games' }} (recent games count more), pulled 35% toward Kalshi's price. Score 70+ with a positive edge can go into the combo, one leg per game. Refreshes every 30 min.</div>
{% elif props.error %}<div class="k">Props scan error: {{ props.error }}</div>
{% else %}<div class="k">Rating tonight's props (a few minutes after a restart)…</div>{% endif %}</div>
<div class="card"><h2>Today's combos{% if combos.dry_run %} (test mode, not placed){% endif %}</h2>
{% if combos.combos %}<div class="scroll"><table><tr><th>Legs</th><th>Hit chance</th><th>Price</th><th>Stake</th><th>Pays</th></tr>
{% for c in combos.combos %}<tr><td>{% for l in c.legs %}{{ l }}{% if not loop.last %}<br>{% endif %}{% endfor %}<br><span class="k">{{ c.league }} · {{ c.size }}-leg · {{ c.action }}{% if c.placed %} · placed{% endif %}{% if c.note %} · {{ c.note }}{% endif %}</span></td>
<td><b>{{ '%.0f'|format(100*c.hit) }}%</b></td><td>{{ '%.0f¢'|format(100*c.price) if c.price else '—' }}</td><td>${{ '%.2f'|format(c.stake) }}</td><td>${{ c.payout }}</td></tr>{% endfor %}
</table></div><div class="k" style="margin-top:6px">{% if combos.borrow %}<b>Using idle whale cash: {{ 'yes' if combos.borrow.on else 'no' }}</b> ({{ combos.borrow.why }}). {% endif %}{% for s in combos.slates %}{{ s.league }}: {{ s.legs_used }} of {{ s.legs_available }} strong legs. {% endfor %}One leg per game, each leg in only one combo. Hit chance = the legs' chances multiplied. NBA built daily for tonight, NFL for the upcoming games. LIMIT = waiting at a fair price; TAKE = bought at Kalshi's price.</div>
{% else %}<div class="k">Built once a day from the props above and the whale picks.</div>{% endif %}</div>
<div class="card"><h2>Lag trading (Kalshi vs Polymarket)</h2>
{% set lw = st.lagwatch if st and st.lagwatch else {} %}
{% if lw %}<div class="grid"><div><div class="k">Live markets watched</div><div class="v">{{ lw.pairs_watched or 0 }}</div></div>
<div><div class="k">Upcoming watched</div><div class="v">{{ lw.pre_watched or 0 }}</div></div>
<div><div class="k">Gaps seen</div><div class="v">{{ lw.gaps or 0 }}</div></div>
<div><div class="k">Profitable after fees</div><div class="v">{{ lw.converged or 0 }}</div></div>
<div><div class="k">False gaps filtered</div><div class="v">{{ lw.phantom or 0 }}</div></div></div>
{% set lp = st.lag_pnl or {} %}
<div class="grid" style="margin-top:10px"><div><div class="k">Lag trades done</div><div class="v">{{ lp.trades or 0 }}{% if lp.trades %} <span class="k">({{ lp.wins }} won)</span>{% endif %}</div></div>
<div><div class="k">Lag profit (after fees)</div><div class="v {{ 'ok' if (lp.pnl or 0) > 0 else ('bad' if (lp.pnl or 0) < 0 else '') }}">{{ '-' if (lp.pnl or 0) < 0 }}${{ '%.2f'|format((lp.pnl or 0)|abs) }}</div></div>
<div><div class="k">Open lag trades</div><div class="v">{{ lp.open or 0 }} <span class="k">(${{ '%.2f'|format(lp.open_cost or 0) }})</span></div></div>
<div><div class="k">Cash ready for lag</div><div class="v">${{ '%.2f'|format(lp.reserve_free or 0) }}</div></div></div>
{% if lp.by %}<div class="k" style="margin-top:4px">{% for n, g in lp.by|dictsort %}{{ n }}: {{ g.trades }} trades, {{ '-' if g.pnl < 0 }}${{ '%.2f'|format(g.pnl|abs) }}{% if not loop.last %} · {% endif %}{% endfor %}</div>{% endif %}
{% if lw.groups %}<div class="scroll" style="margin-top:8px"><table><tr><th>League</th><th>Status</th><th>Gaps</th><th>Profitable</th><th>Games</th><th>Buys filled</th></tr>
{% for name, g in lw.groups|dictsort %}<tr><td>{{ name }}</td><td class="{{ 'ok' if g.mode == 'trade' else '' }}"><b>{{ 'TRADING' if g.mode == 'trade' else 'watching' }}</b></td><td>{{ g.gaps }}</td><td>{{ (100*g.converged/g.gaps)|round|int if g.gaps else 0 }}%</td><td>{{ g.games|length }}</td><td>{{ g.fills or 0 }}/{{ g.attempts or 0 }}</td></tr>{% endfor %}
</table></div>{% endif %}
{% if lw.examples %}<div class="scroll" style="margin-top:8px"><table><tr><th>Recent gap</th><th>Kalshi</th><th>Poly</th><th>Gap</th><th>Profitable</th></tr>
{% for e in lw.examples[::-1][:8] %}<tr><td>{{ e.title }}<br><span class="k">{{ e.group }} · {{ e.ticker }} {{ e.side|upper }}</span></td><td>{{ '%.0f'|format(100*e.entry) }}¢</td><td>{{ '%.0f'|format(100*e.pm_mid) }}¢</td><td>{{ e.max_gap_c }}¢</td><td>{{ 'yes' if e.converged else 'no' }}</td></tr>{% endfor %}
</table></div>{% endif %}
<div class="k" style="margin-top:6px">Gaps are confirmed on Kalshi's live order book before they count. Every sports game on both exchanges: live every 5s, upcoming every 30s. Each league (and pre-game separately) switches to trading by itself after 8+ gaps across 3+ games with 60%+ profitable after both fees. 3% of balance per trade, max 10% at once, 10% of balance reserved for it.</div>
{% else %}<div class="k">Starting…</div>{% endif %}</div>
<div class="card"><h2>Today's slate</h2>
{% if slate %}<div class="grid">
<div><div class="k">Kalshi games today</div><div class="v">{{ slate.games }}</div></div>
<div><div class="k">Spread/total lines</div><div class="v">{{ slate.spread_total_markets }}</div></div>
<div><div class="k">Copied today</div><div class="v">{{ slate.copied_today }}</div></div>
<div><div class="k">Skipped today</div><div class="v">{{ slate.skipped_today }}</div></div></div>
<div class="k" style="margin-top:8px">{% for l,n in slate.by_league[:12] %}{{ l }} {{ n }}{% if not loop.last %} · {% endif %}{% endfor %}</div>
{% if slate.skip_reasons %}<div class="k" style="margin-top:6px">Top skip reasons: {% for r,n in slate.skip_reasons[:4] %}{{ r }} ({{ n }}){% if not loop.last %}; {% endif %}{% endfor %}</div>{% endif %}
{% else %}<div class="k">Counting today's games (about a minute after a restart)…</div>{% endif %}</div>
<div class="card"><h2>Whales followed</h2>
{% if st and st.whales %}<div>{% for w in st.whales %}<span class="pill {{ 'bad' if w.benched else ('ok' if w.auto else '') }}" style="margin:2px 4px 2px 0">{{ w.name }}{% if w.auto %} · auto{% endif %}{% if w.benched %} · benched{% endif %}</span>{% endfor %}</div>
<div class="k" style="margin-top:6px">Auto = added by the bot from the screener (strong, profitable, copyable traders). Benched = 20+ settled copies with ROI −10% or worse: new bets aren't copied (exits still are); un-benched if ROI turns positive.</div>{% endif %}</div>
<div class="card"><h2>Whale scoreboard (your copied results)</h2>
{% if st.scoreboard %}<div class="scroll"><table><tr><th>Whale</th><th>Settled</th><th>Win %</th><th>P&amp;L</th><th>ROI</th><th>Open</th></tr>
{% for b in st.scoreboard %}<tr><td>{{ b.whale }}</td><td>{{ b.closed }}</td><td>{{ b.win_rate if b.win_rate is not none else '—' }}{% if b.win_rate is not none %}%{% endif %}</td>
<td class="{{ 'ok' if b.pnl > 0 else ('bad' if b.pnl < 0 else '') }}">{{ '-' if b.pnl < 0 }}${{ '%.2f'|format(b.pnl|abs) }}</td>
<td>{{ b.roi if b.roi is not none else '—' }}{% if b.roi is not none %}%{% endif %}</td><td>{{ b.open_lots }} (${{ '%.0f'|format(b.open_cost) }})</td></tr>{% endfor %}
</table></div><div class="k" style="margin-top:6px">P&amp;L is after estimated Kalshi fees. Bets count once Kalshi settles the game or the whale exits.</div>
{% else %}<div class="k">No copied bets yet.</div>{% endif %}</div>
<div class="card"><h2>Waiting limit orders</h2>
{% if st.resting %}<div class="scroll"><table><tr><th>Bet</th><th>Price</th><th>Qty</th><th>Score</th><th>Whale</th></tr>
{% for r in st.resting %}<tr><td>{{ r.title }}<br><span class="k">{{ r.ticker }} {{ r.side|upper }}</span></td><td>{{ '%.0f'|format(100*r.price) }}¢</td><td>{{ r.remaining }}</td><td>{{ r.score }}</td><td>{{ r.whale }}</td></tr>{% endfor %}
</table></div><div class="k" style="margin-top:6px">Resting at a fair price; fills only if someone sells at that price. Cancelled automatically before the game starts.</div>
{% else %}<div class="k">None</div>{% endif %}</div>
<div class="card"><h2>Open positions</h2>
{% if st.positions %}<table>{% for k,v in st.positions.items() %}<tr><td>{{ k.split('|')[0] }}</td><td>{{ k.split('|')[1]|upper }}</td><td>{{ v }} contracts</td></tr>{% endfor %}</table>
{% else %}<div class="k">None</div>{% endif %}</div>
{% endif %}
<div class="card"><h2>Copied trades</h2>
{% if trades %}<div class="scroll"><table><tr><th>Time</th><th>Whale</th><th>Market</th><th>Action</th><th>Qty</th><th>Price</th></tr>
{% for t in trades %}<tr><td>{{ t.time_utc[5:16] }}</td><td>{{ t.whale }}</td><td>{{ t.kalshi_ticker }} {{ t.side|upper }}</td><td>{{ t.action }}{% if t.mode=='paper' %} (paper){% endif %}</td><td>{{ t.contracts }}</td><td>{{ t.price }}</td></tr>{% endfor %}
</table></div>{% else %}<div class="k">No copied trades yet.</div>{% endif %}</div>
<div class="card"><h2>Log</h2><pre>{% for l in logs %}{{ l }}
{% endfor %}</pre></div>
<div class="k" style="text-align:center">Updated {{ now }} · refreshes every 30s</div>
</main></body></html>"""

# ---- whale reports (refreshed every 6h, shown at /report) ------------------ #
REPORTS: dict[str, dict] = {}
SCREEN: list[dict] = []


def _report_loop():
    import json
    import whale_report
    STOP.wait(20)  # let the bot start first
    while not STOP.is_set():
        cfg = copybot.load_yaml(os.environ.get("CONFIG_PATH", "config.yaml"))
        for w in cfg.get("whales", []):
            addr = w["address"].lower()
            if addr.strip("0x") == "":
                continue
            try:
                rep = whale_report.build(addr)
                rep["name"] = w.get("name", addr[:8])
                REPORTS[addr] = rep
                # compact log lines so the report can also be read from Render logs
                summary = {k: rep.get(k) for k in ("name", "wallet", "open", "closed", "trades",
                                                  "kalshi_markets_scanned", "errors")}
                summary["open"] = {k: v for k, v in (rep.get("open") or {}).items() if k != "top"}
                log.info("WHALE_REPORT %s", json.dumps(summary, default=str))
                for p in (rep.get("open") or {}).get("top", []):
                    log.info("WHALE_POS %s", json.dumps(p, default=str))
                whale_report.add_matches(rep)
                log.info("WHALE_SCANNED %s kalshi markets", rep.get("kalshi_markets_scanned"))
                _match_test(addr)
                for c in rep.get("kalshi_candidates", []):
                    log.info("WHALE_MATCH %s", json.dumps(c, default=str))
            except Exception as e:
                log.warning("whale report for %s failed: %s", addr[:8], e)
        try:
            import screener
            res = screener.screen()
            SCREEN[:] = res
            if BOT is not None:
                good = [r for r in res if (r.get("pnl_month") or 0) > 0 and (r.get("pnl_all") or 0) > 0
                        and (r.get("per_day") or 0) <= 150 and (r.get("median_usd") or 0) >= 20
                        and (r.get("extreme_price_pct") or 0) <= 30]
                stats = {r["wallet"]: {k: r.get(k) for k in ("name", "pnl_month", "pnl_all", "median_usd",
                                                             "per_day", "score")} for r in res}
                BOT.whale_stats.update(stats)
                BOT.state.data["whale_stats"] = BOT.whale_stats
                BOT.screen_buys = [b for r in good[:30] for b in r.get("_buys", [])]
                log.info("Screener fed %d whale records and %d recent buys", len(stats), len(BOT.screen_buys))
                try:
                    BOT.manage_whales(res)
                except Exception as e:
                    log.warning("whale manager failed: %s", e)
                try:
                    bets = compute_best()
                    BEST.update(bets=bets, updated=time.time())
                    BOT.board_queue = list(bets)
                except Exception as e:
                    log.warning("best bets refresh failed: %s", e)
            keys = ("name", "wallet", "score", "pnl_week", "pnl_month", "pnl_all", "per_day",
                    "median_usd", "copyable_pct", "extreme_price_pct", "buys_pct", "top_leagues",
                    "kinds", "last_trade_h", "days_covered")
            log.info("SCREEN_DONE %d traders", len(res))
            for r in res[:25]:
                log.info("SCREEN %s", json.dumps({k: r.get(k) for k in keys}, default=str))
        except Exception as e:
            log.warning("screener failed: %s", e)
        STOP.wait(6 * 3600)


def _match_test(addr: str) -> None:
    """Run the sports matcher on the whale's recent real trades and log the results."""
    import json
    from collections import Counter
    import whale_report
    if BOT is None:
        return
    try:
        trades = whale_report._get(f"{whale_report.POLY}/trades", user=addr, limit=300)
        reasons, found, examples = Counter(), 0, []
        seen = set()
        from datetime import date, timedelta
        for t in trades:
            key = (t.get("slug"), t.get("outcome"))
            if key in seen:
                continue
            c = BOT.sports.classify(t)
            if c and "date" in c and c["date"] < date.today() - timedelta(days=0):
                continue  # finished games are closed on Kalshi; only test today and later
            seen.add(key)
            r, why = BOT.sports.lookup(t)
            if r:
                found += 1
                if len(examples) < 12:
                    examples.append({"pm": t.get("title"), "out": t.get("outcome"), "k": r["ticker"],
                                     "side": r["side"], "how": r.get("how")})
            else:
                reasons[why if not why.startswith("no Kalshi market for") else "no Kalshi market for team"] += 1
                if why.startswith(("no Kalshi", "found team")) and len(examples) < 24:
                    examples.append({"pm": t.get("title"), "slug": t.get("slug"), "out": t.get("outcome"), "miss": why})
        log.info("MATCHTEST %s", json.dumps({"unique_bets": len(seen), "matched": found,
                                             "reasons": reasons.most_common()}))
        for e in examples:
            log.info("MATCHEX %s", json.dumps(e))
    except Exception as e:
        log.warning("match test failed: %s", e)


_register("reports", _report_loop)


@app.get("/api/report")
def api_report():
    return jsonify(REPORTS)


@app.get("/report")
def report_page():
    return render_template_string(REPORT_PAGE, reports=list(REPORTS.values()), screen=SCREEN[:20])


REPORT_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Whale Report</title>
<style>body{margin:0;font:15px/1.45 system-ui,sans-serif;background:#f6f6f4;color:#1d1d1b}
@media (prefers-color-scheme:dark){body{background:#141413;color:#ecebe6}.card{background:#1e1e1c!important;border-color:#2e2e2b!important}}
main{max-width:860px;margin:0 auto;padding:16px}.card{background:#fff;border:1px solid #e4e4df;border-radius:12px;padding:14px;margin-bottom:12px}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:5px 4px;border-bottom:1px solid #8883}
.k{color:#888;font-size:12px}a{color:inherit}</style></head><body><main>
<p><a href="/">← Dashboard</a></p>
{% if not reports %}<div class="card">Report is being built (takes a minute or two after a restart). Refresh soon.</div>{% endif %}
{% for r in reports %}
<div class="card"><h2 style="margin:0 0 6px">{{ r.name }}</h2><div class="k">{{ r.wallet }}</div>
{% set o=r.open or {} %}{% set c=r.closed or {} %}{% set t=r.trades or {} %}
<p>Open: {{ o.count }} positions, ${{ '{:,.0f}'.format(o.value or 0) }} (unrealized ${{ '{:,.0f}'.format(o.unrealized_pnl or 0) }})<br>
Closed: {{ c.count }} · win rate {{ ((c.win_rate or 0)*100)|round(1) }}% · realized ${{ '{:,.0f}'.format(c.realized_pnl or 0) }}<br>
Trades: ~{{ t.per_day }}/day · median ${{ '{:,.0f}'.format(t.median_usd or 0) }} · last {{ t.last_trade_hours_ago }}h ago</p>
{% if r.errors %}<p class="k">Errors: {{ r.errors|join('; ') }}</p>{% endif %}</div>
<div class="card"><h3 style="margin-top:0">Kalshi match candidates</h3>
{% for m in r.kalshi_candidates or [] %}<p><b>{{ m.pm_title }}</b> [{{ m.outcome }}] ${{ '{:,.0f}'.format(m.value) }}<br>
{% for x in m.matches %}<span class="k">{{ x.score }}</span> {{ x.ticker }} — {{ x.text }}<br>{% endfor %}</p>{% endfor %}</div>
{% endfor %}
<div class="card"><h3 style="margin-top:0">Top sports whales to copy (ranked)</h3>
{% if not screen %}<div class="k">Screening runs a few minutes after each restart.</div>{% endif %}
<div style="overflow-x:auto"><table><tr><th>#</th><th>Trader</th><th>Score</th><th>Month P&amp;L</th><th>All-time</th><th>/day</th><th>Median $</th><th>Copyable</th></tr>
{% for s in screen %}<tr><td>{{ loop.index }}</td><td>{{ s.name }}<br><span class="k">{{ s.wallet }}</span></td><td>{{ s.score }}</td>
<td>${{ '{:,.0f}'.format(s.pnl_month or 0) }}</td><td>${{ '{:,.0f}'.format(s.pnl_all or 0) }}</td><td>{{ s.per_day }}</td>
<td>${{ '{:,.0f}'.format(s.median_usd or 0) }}</td><td>{{ s.copyable_pct }}%</td></tr>{% endfor %}</table></div></div>
</main></body></html>"""



# ---- today's slate: Kalshi games today in the leagues the bot copies -------- #
SLATE: dict = {}


def _slate_loop():
    import json
    from collections import Counter
    from datetime import datetime, timedelta, timezone
    import sports_match as sm
    STOP.wait(30)
    while not STOP.is_set():
        try:
            if BOT is not None:
                today = (datetime.now(timezone.utc) - timedelta(hours=5)).date()  # US Central-ish day
                per_league, lines = Counter(), 0
                seen_series = set()
                for prefix, series in sm.PREFIX_SERIES.items():
                    for s in series:
                        if s in seen_series:
                            continue
                        seen_series.add(s)
                        try:
                            ms = BOT.sports._series_markets(s)
                        except Exception:
                            continue
                        evs = {m["event_ticker"] for m in ms if sm.parse_kalshi_date(m.get("event_ticker", "")) == today}
                        if evs:
                            per_league[s.replace("KX", "").replace("GAME", "").replace("MATCH", "").replace("FIGHT", "")] += len(evs)
                for s in set(sm.SPREAD_SERIES.values()) | set(sm.TOTAL_SERIES.values()):
                    try:
                        ms = BOT.sports._series_markets(s)
                    except Exception:
                        continue
                    lines += sum(1 for m in ms if sm.parse_kalshi_date(m.get("event_ticker", "")) == today)
                ev = BOT.state.data.get("events", [])
                tday = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                copied = sum(1 for e in ev if e.get("time_utc", "").startswith(tday) and str(e.get("action", "")).startswith("buy"))
                skips = BOT.state.data.get("skips", {}).get(tday, {})
                SLATE.clear()
                SLATE.update({"date": str(today), "games": sum(per_league.values()),
                              "by_league": per_league.most_common(), "spread_total_markets": lines,
                              "copied_today": copied, "skipped_today": sum(skips.values()),
                              "skip_reasons": sorted(skips.items(), key=lambda x: -x[1])[:8],
                              "updated": time.time()})
                log.info("SLATE %s", json.dumps(SLATE, default=str))
        except Exception as e:
            log.warning("slate failed: %s", e)
        STOP.wait(1800)


_register("slate", _slate_loop)



# ---- best bets now: recent whale buys on open games, ranked by score -------- #
BEST: dict = {"bets": [], "updated": None}


def compute_best() -> list[dict]:
    """Recent whale buys (last 24h) on games still open on Kalshi, ranked by bet score."""
    from collections import defaultdict
    from scoring import score_bet, stake_pct
    if BOT is None:
        return []
    cutoff = time.time() - 24 * 3600
    pool = [b for b in BOT.state.data.get("recent_buys", []) + BOT.screen_buys
            if b.get("ts", 0) >= cutoff and b.get("cond")]
    groups = defaultdict(list)
    for b in pool:
        groups[(b["cond"], b["out"])].append(b)
    ranked = sorted(groups.items(), key=lambda kv: -sum(x["usd"] for x in kv[1]))[:60]
    out = []
    for (cond, outc), bs in ranked:
        sample = max(bs, key=lambda x: x["usd"])
        t = {"slug": sample["slug"], "eventSlug": sample["eventSlug"], "title": sample["title"],
             "outcome": outc, "outcomeIndex": sample.get("outcomeIndex"), "conditionId": cond}
        tgt = BOT.maps.lookup(t)
        if tgt:
            tk, side = tgt.ticker, tgt.side
        else:
            found, _ = BOT.sports.lookup(t)
            if not found:
                continue
            tk, side = found["ticker"], found["side"]
        BOT._register_pair(t, copybot.Target(tk, side))
        try:
            m = BOT.kdata.market(tk)
        except Exception:
            continue
        if m.get("status") not in (None, "open", "active"):
            continue
        ask = copybot._px(m, f"{side}_ask")
        if not ask or ask <= 0 or ask >= 1:
            continue
        cls = BOT.sports.classify(t) or {}
        status = BOT.game_status(cond, m, cls.get("date"))
        if status == "started":
            continue  # never show games already being played
        whales = {x["w"] for x in bs}
        opp = {x["w"] for x in pool if x["cond"] == cond and x["out"] != outc} - whales
        usd = sum(x["usd"] for x in bs)
        paid = sum(x["px"] * x["usd"] for x in bs) / usd if usd else sample["px"]
        now_px = BOT.pm.market_info(cond)["prices"].get(str(outc))
        px = now_px if now_px and 0.02 < now_px < 0.98 else paid  # fair price = Polymarket now
        best_stats = max((BOT.whale_stats.get(w) or {} for w in whales),
                         key=lambda s: s.get("pnl_all") or 0, default=None)
        sc = score_bet(whale_px=px, ask=ask, whale_usd=sample["usd"], stats=best_stats or None,
                       same=len(whales) - 1, opposite=len(opp), rate=BOT.fee_rate)
        if sc.get("parts", {}).get("stale"):
            continue
        pct = stake_pct(sc["score"], BOT.current_tiers())
        out.append({"title": sample["title"], "outcome": outc, "whales": len(whales),
                    "latest_ts": max(x["ts"] for x in bs),
                    "names": sorted({x["name"] for x in bs})[:4], "whale_usd": round(usd),
                    "whale_px": round(px, 3), "paid_px": round(paid, 3), "status": status,
                    "ticker": tk, "side": side, "ask": round(ask, 3),
                    "score": sc["score"], "ev": sc["ev"],
                    "stake": round(BOT.equity() * pct, 2) if sc["ev"] >= 0 and status == "pre" else 0})
    out.sort(key=lambda x: (-x["score"], -x["ev"]))
    return out[:20]


def _best_loop():
    import json
    STOP.wait(60)
    while not STOP.is_set():
        try:
            bets = compute_best()
            BEST.update(bets=bets, updated=time.time())
            if BOT is not None:
                BOT.board_queue = list(bets)  # bot thread decides which to auto-bet
                BOT.combo_picks = list(bets)  # bot thread builds the weekly combo
            log.info("BEST %s", json.dumps(bets[:8], default=str))
        except Exception as e:
            log.warning("best bets failed: %s", e)
        STOP.wait(600)


_register("best", _best_loop)


if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
