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


threading.Thread(target=_bot_loop, name="copybot", daemon=True).start()


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
    threading.Thread(target=_keep_awake, name="keep-awake", daemon=True).start()

# ---- web ------------------------------------------------------------------- #
app = Flask(__name__)
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def _authed() -> bool:
    a = request.authorization
    return bool(PASSWORD) and a is not None and hmac.compare_digest(a.password or "", PASSWORD)


@app.before_request
def require_auth():
    if request.path == "/healthz":
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
        trades=BOT.recent_journal(40) if BOT else [], ago=_ago,
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
                for c in rep.get("kalshi_candidates", []):
                    log.info("WHALE_MATCH %s", json.dumps(c, default=str))
            except Exception as e:
                log.warning("whale report for %s failed: %s", addr[:8], e)
        STOP.wait(6 * 3600)


threading.Thread(target=_report_loop, name="reports", daemon=True).start()


@app.get("/api/report")
def api_report():
    return jsonify(REPORTS)


@app.get("/report")
def report_page():
    return render_template_string(REPORT_PAGE, reports=list(REPORTS.values()))


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
{% endfor %}</main></body></html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
