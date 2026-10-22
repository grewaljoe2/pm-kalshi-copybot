"""
Live lag watcher: during games, does Kalshi's price trail Polymarket's?

For every bet the bot has matched on both exchanges (whale trades, top-trader picks), while
the game is live it polls Polymarket's order book and Kalshi's market every few seconds.

A "gap" opens when Kalshi's ask is cheaper than Polymarket's mid price by more than the
Kalshi fee plus a margin: buying on Kalshi would be getting the bet below the price the
bigger market says it's worth. For each gap it records how big it got, how long it lasted,
and whether Kalshi caught up within a minute (could we have sold for a profit?).

mode "watch" only measures and logs. mode "trade" (not used yet) would buy the gap and sell
when prices converge; that switch is left for after the numbers prove the gaps are real.
"""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("lagwatch")
CLOB = "https://clob.polymarket.com"


def _px(m: dict, f: str):
    v = m.get(f"{f}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(f)
    return float(v) / 100 if v not in (None, 0) else None


class LagWatch:
    def __init__(self, bot, cfg: dict):
        self.bot = bot
        self.cfg = cfg or {}
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "pm-kalshi-copybot/1.0"})
        self.open: dict[str, dict] = {}      # key -> gap in progress
        self.follow: list[dict] = []         # closed gaps waiting for the 60s convergence check
        st = bot.state.data.setdefault("lagwatch", {})
        st.setdefault("gaps", 0)
        st.setdefault("converged", 0)
        st.setdefault("examples", [])
        st.setdefault("pairs_watched", 0)
        self.stats = st

    # ---- prices ---------------------------------------------------------- #
    def pm_book(self, token: str) -> tuple[float | None, float | None]:
        r = self.s.get(f"{CLOB}/book", params={"token_id": token}, timeout=8)
        r.raise_for_status()
        j = r.json()
        bids = [float(x["price"]) for x in j.get("bids") or []]
        asks = [float(x["price"]) for x in j.get("asks") or []]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def live_pairs(self) -> list[dict]:
        now = time.time()
        out = []
        for p in list((getattr(self.bot, "pairs", {}) or {}).values()):
            if now - p.get("added", now) > 30 * 3600:
                continue
            if "start" not in p:  # look up once per pair (start time + Polymarket token)
                try:
                    info = self.bot.pm.market_info(p["cond"])
                except Exception:
                    continue
                p["start"] = info.get("start")
                p["token"] = (info.get("tokens") or {}).get(p["outcome"])
            start, tok = p.get("start"), p.get("token")
            if not start or not tok or not (start <= now <= start + 4 * 3600):
                continue
            out.append(p)
        return out[: int(self.cfg.get("max_pairs", 12))]

    # ---- loop ------------------------------------------------------------- #
    def tick(self) -> None:
        margin = float(self.cfg.get("min_gap", 0.03))
        rate = getattr(self.bot, "fee_rate", 0.07)
        pairs = self.live_pairs()
        self.stats["pairs_watched"] = len(pairs)
        now = time.time()
        for p in pairs:
            key = f'{p["ticker"]}|{p["side"]}'
            try:
                bid, ask = self.pm_book(p["token"])
                m = self.bot.kdata.market(p["ticker"])
            except Exception:
                continue
            if bid is None or ask is None or ask - bid > 0.04:
                continue  # Polymarket book too thin to trust
            pm_mid = (bid + ask) / 2
            k_ask = _px(m, f'{p["side"]}_ask')
            k_bid = _px(m, f'{p["side"]}_bid')
            if not k_ask or k_ask <= 0 or k_ask >= 1:
                continue
            gap = pm_mid - k_ask - rate * k_ask * (1 - k_ask)
            g = self.open.get(key)
            if gap >= margin:
                if not g:
                    self.open[key] = {"key": key, "title": p["title"], "ticker": p["ticker"], "side": p["side"],
                                      "t0": now, "entry": k_ask, "pm_mid": round(pm_mid, 3), "max_gap": gap}
                    log.info("LAGWATCH gap open %s %s: Kalshi ask %.2f vs Polymarket %.3f (gap %.1fc)",
                             p["ticker"], p["side"].upper(), k_ask, pm_mid, 100 * gap)
                else:
                    g["max_gap"] = max(g["max_gap"], gap)
            elif g:
                g["secs"] = round(now - g["t0"])
                g["closed"] = now
                self.follow.append(g)
                del self.open[key]
            # convergence check: within 60s of the gap, could we sell above our entry?
            for f in [f for f in self.follow if f["key"] == key]:
                if k_bid and k_bid >= f["entry"] + 0.02:
                    f["converged"] = True
                if now - f["t0"] > 60 or f.get("converged"):
                    self._record(f)
                    self.follow.remove(f)
            time.sleep(0.15)

    def _record(self, g: dict) -> None:
        self.stats["gaps"] += 1
        if g.get("converged"):
            self.stats["converged"] += 1
        ex = {"title": g["title"][:60], "ticker": g["ticker"], "side": g["side"], "entry": g["entry"],
              "pm_mid": g["pm_mid"], "max_gap_c": round(100 * g["max_gap"], 1), "secs": g.get("secs"),
              "converged": bool(g.get("converged")), "at": int(g["t0"])}
        self.stats["examples"] = (self.stats["examples"] + [ex])[-25:]
        log.info("LAGWATCH %s", json.dumps(ex))

    def run(self, stop) -> None:
        every = float(self.cfg.get("poll_seconds", 5))
        while not stop.is_set():
            try:
                if not self.bot.paused:
                    self.tick()
            except Exception as e:
                log.warning("lagwatch tick failed: %s", e)
            stop.wait(every)
