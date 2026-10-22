"""
Live lag watcher: during games, does Kalshi's price trail Polymarket's?

For every bet the bot has matched on both exchanges (whale trades, top-trader picks), while
the game is live it polls Polymarket's order book and Kalshi's market every few seconds.

A "gap" opens when Kalshi's ask is cheaper than Polymarket's mid price by more than the
Kalshi fee plus a margin: buying on Kalshi would be getting the bet below the price the
bigger market says it's worth. For each gap it records how big it got, how long it lasted,
and whether Kalshi caught up within a minute (could we have sold for a profit?).

mode "watch" only measures. With auto_switch on, it flips itself to "trade" once enough gaps
have shown up across several games and most of them could have been sold for a profit after
both fees. In "trade" mode it buys the gap on Kalshi (small size) and sells as soon as selling
nets at least 1c after fees; if Kalshi never catches up it simply holds a bet it bought below
Polymarket's price until the game settles.
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
        if not st.get("strict_v2"):  # earlier counts used a looser "caught up" test: start fresh
            st.update(gaps=0, converged=0, examples=[], games=[], strict_v2=True)
        st.setdefault("mode", self.cfg.get("mode", "watch"))
        st.setdefault("trades", [])
        st.setdefault("games", [])
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
                    if self.stats.get("mode") == "trade":
                        self._buy(p, k_ask, pm_mid)
                else:
                    g["max_gap"] = max(g["max_gap"], gap)
            elif g:
                g["secs"] = round(now - g["t0"])
                g["closed"] = now
                self.follow.append(g)
                del self.open[key]
            # convergence check: within 60s, could we have sold for >= 1c after both fees?
            for f in [f for f in self.follow if f["key"] == key]:
                if k_bid and self._net(f["entry"], k_bid, rate) >= 0.01:
                    f["converged"] = True
                if now - f["t0"] > 60 or f.get("converged"):
                    self._record(f)
                    self.follow.remove(f)
            if self.stats.get("mode") == "trade" and k_bid:
                self._maybe_sell(key, k_bid, rate)
            time.sleep(0.15)
        # trades on games that ended are left to settle normally
        self.stats["trades"] = [x for x in self.stats["trades"] if now - x["t0"] < 5 * 3600]

    @staticmethod
    def _fee(p: float, rate: float) -> float:
        return rate * p * (1 - p)

    def _net(self, entry: float, bid: float, rate: float) -> float:
        """Profit per contract from buying at entry and selling at bid, after both taker fees."""
        return bid - entry - self._fee(entry, rate) - self._fee(bid, rate)

    # ---- trade mode -------------------------------------------------------- #
    def _buy(self, p: dict, ask: float, pm_mid: float) -> None:
        from copybot import Target
        if any(x["key"] == f'{p["ticker"]}|{p["side"]}' for x in self.stats["trades"]):
            return
        bot = self.bot
        eq = bot.equity()
        open_cost = sum(x["count"] * x["entry"] for x in self.stats["trades"])
        stake = min(eq * float(self.cfg.get("stake_pct", 0.03)),
                    eq * float(self.cfg.get("max_open_pct", 0.10)) - open_cost, bot.cash() - 0.5)
        n = int(stake // ask) if stake > 0 else 0
        if n < 1:
            return
        filled, cost = bot._execute(Target(p["ticker"], p["side"]), "buy", n, ask)
        if not filled:
            return
        px = cost / filled
        bot.ledger_queue.append(("open", "Lag trade", p["ticker"], p["side"], filled, px, p["title"]))
        self.stats["trades"].append({"key": f'{p["ticker"]}|{p["side"]}', "ticker": p["ticker"], "side": p["side"],
                                     "count": filled, "entry": px, "t0": time.time(), "pm_mid": pm_mid,
                                     "title": p["title"]})
        log.info("LAGTRADE BUY %d %s %s @ %.2f (Polymarket %.3f)", filled, p["ticker"], p["side"].upper(), px, pm_mid)

    def _maybe_sell(self, key: str, bid: float, rate: float) -> None:
        from copybot import Target
        for x in [x for x in self.stats["trades"] if x["key"] == key]:
            if self._net(x["entry"], bid, rate) < float(self.cfg.get("min_profit", 0.01)):
                continue
            filled, proceeds = self.bot._execute(Target(x["ticker"], x["side"]), "sell", x["count"], bid)
            if filled:
                px = proceeds / filled
                self.bot.ledger_queue.append(("close", "Lag trade", x["ticker"], x["side"], filled, px, x["title"]))
                log.info("LAGTRADE SELL %d %s %s @ %.2f (bought %.2f, +%.1fc each after fees)", filled, x["ticker"],
                         x["side"].upper(), px, x["entry"], 100 * self._net(x["entry"], px, rate))
                x["count"] -= filled
            if x["count"] <= 0:
                self.stats["trades"].remove(x)

    def _maybe_switch(self) -> None:
        st, c = self.stats, self.cfg
        if st.get("mode") != "watch" or not c.get("auto_switch", True):
            return
        n, conv, games = st["gaps"], st["converged"], len(set(st.get("games", [])))
        if (n >= int(c.get("min_gaps", 8)) and conv / max(1, n) >= float(c.get("min_converge_rate", 0.6))
                and games >= int(c.get("min_games", 3))):
            st["mode"] = "trade"
            st["switched_at"] = int(time.time())
            log.warning("LAGWATCH switched to TRADE mode: %d gaps, %d profitable after fees (%.0f%%), %d games",
                        n, conv, 100 * conv / n, games)

    def _record(self, g: dict) -> None:
        self.stats["gaps"] += 1
        if g.get("converged"):
            self.stats["converged"] += 1
        ex = {"title": g["title"][:60], "ticker": g["ticker"], "side": g["side"], "entry": g["entry"],
              "pm_mid": g["pm_mid"], "max_gap_c": round(100 * g["max_gap"], 1), "secs": g.get("secs"),
              "converged": bool(g.get("converged")), "at": int(g["t0"])}
        self.stats["examples"] = (self.stats["examples"] + [ex])[-25:]
        game = g["ticker"].rsplit("-", 1)[0]
        self.stats["games"] = (self.stats.get("games", []) + [game])[-200:]
        log.info("LAGWATCH %s", json.dumps(ex))
        self._maybe_switch()

    def run(self, stop) -> None:
        every = float(self.cfg.get("poll_seconds", 5))
        while not stop.is_set():
            try:
                if not self.bot.paused:
                    self.tick()
            except Exception as e:
                log.warning("lagwatch tick failed: %s", e)
            stop.wait(every)
