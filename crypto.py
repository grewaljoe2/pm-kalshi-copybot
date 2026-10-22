"""
Crypto lag watcher: Kalshi's short-term BTC / ETH markets vs the live coin price.

Price: Coinbase websocket (BTC-USD, ETH-USD), every trade. Volatility: per-second log
returns, exponentially weighted (recent minutes count most).

Markets (found every 60s, closing within the next 70 minutes):
  15-minute up/down  (KXBTC15M, KXETH15M): YES if the price at close >= the price at open
  hourly above/below (KXBTCD, KXETHD):    YES if the price at close is above the level

Fair chance of YES = N( ln(S/K) / sqrt(sigma^2 * t + basis^2) ), where S = live price,
K = start price or level, t = seconds left, basis = allowance for Kalshi settling on the
CF Benchmarks index (several exchanges averaged) rather than Coinbase alone.

A gap = fair value minus Kalshi's live ask minus the fee >= min_gap, on YES or NO, confirmed
on Kalshi's order book. Gaps feed the same evidence / auto-switch / trading logic as sports
(groups "BTC 15m", "BTC hourly", "ETH 15m", ...). No trades in the last 45s of a window.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from datetime import datetime

log = logging.getLogger("crypto")
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


class CoinStream:
    """Live Coinbase prices + per-second volatility."""

    def __init__(self, products=("BTC-USD", "ETH-USD")):
        self.products = list(products)
        self.price: dict[str, float] = {}
        self.ts: dict[str, float] = {}
        self.var: dict[str, float] = {}        # EWMA of squared 1s log returns
        self.sec: dict[str, tuple] = {}        # last (second, price)
        self.hist: dict[str, deque] = {p: deque(maxlen=7200) for p in self.products}  # (sec, price)
        self.since: dict[str, float] = {}      # when we started receiving prices

    SAVE = "/var/data/coins.json"

    def start(self) -> "CoinStream":
        self._load()
        threading.Thread(target=self._run, name="coinbase-ws", daemon=True).start()
        threading.Thread(target=self._saver, name="coin-save", daemon=True).start()
        return self

    def _load(self) -> None:
        """After a restart, reuse the saved volatility if it's under 5 minutes old (no 10-minute warmup)."""
        import os
        try:
            if not os.path.exists(self.SAVE) or time.time() - os.path.getmtime(self.SAVE) > 300:
                return
            with open(self.SAVE) as f:
                d = json.load(f)
            for p in self.products:
                if p in (d.get("var") or {}):
                    self.var[p] = float(d["var"][p])
                    self.since[p] = time.time() - self.WARMUP - 1
                    for s, px in (d.get("hist") or {}).get(p, [])[-900:]:
                        self.hist[p].append((int(s), float(px)))
            log.info("CRYPTO model restored from %.0fs ago: %s", time.time() - os.path.getmtime(self.SAVE),
                     sorted(self.var))
        except Exception as e:
            log.warning("crypto model restore failed: %s", e)

    def _saver(self) -> None:
        import os
        while True:
            time.sleep(30)
            try:
                if not os.path.isdir(os.path.dirname(self.SAVE)):
                    continue
                ready = [p for p in self.products if self.vol(p)]
                d = {"var": {p: self.var[p] for p in ready},
                     "hist": {p: list(self.hist[p])[-900:] for p in ready}}
                tmp = self.SAVE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(d, f)
                os.replace(tmp, self.SAVE)
            except Exception as e:
                log.warning("crypto model save failed: %s", e)

    def price_at(self, product: str, t: float) -> float | None:
        """Our recorded price at epoch second t (for 15-minute windows' start price)."""
        best = None
        for s, p in reversed(self.hist[product]):
            if s <= t:
                best = p if t - s <= 5 else None
                break
        return best

    WARMUP_S = 600
    MIN_ANNUAL_VOL = 0.30  # never assume calmer than 30%/yr (BTC/ETH rarely trade below that)

    def vol(self, product: str) -> float | None:
        """Per-second volatility (std of 1s log returns). None until 10 min of data (too noisy before)."""
        v = self.var.get(product)
        if not v or time.time() - self.since.get(product, time.time()) < self.WARMUP_S:
            return None
        floor = self.MIN_ANNUAL_VOL / math.sqrt(365 * 24 * 3600)
        return max(math.sqrt(v), floor)

    def _tick(self, product: str, px: float) -> None:
        now = time.time()
        self.price[product], self.ts[product] = px, now
        self.since.setdefault(product, now)
        s = int(now)
        last = self.sec.get(product)
        if last is None or s > last[0]:
            if last is not None:
                gap = max(1, s - last[0])
                r2 = (math.log(px / last[1])) ** 2 / gap
                lam = 0.995  # ~3 min half-life at 1 update/s
                self.var[product] = lam * self.var.get(product, r2) + (1 - lam) * r2
            self.sec[product] = (s, px)
            self.hist[product].append((s, px))

    def _run(self):
        import websocket
        while True:
            try:
                ws = websocket.create_connection(COINBASE_WS, timeout=30)
                ws.send(json.dumps({"type": "subscribe", "product_ids": self.products, "channels": ["ticker"]}))
                log.info("Coinbase live stream connected: %s", self.products)
                while True:
                    j = json.loads(ws.recv())
                    if j.get("type") == "ticker" and j.get("price"):
                        self._tick(j["product_id"], float(j["price"]))
            except Exception as e:
                log.warning("coinbase ws: %s", e)
                time.sleep(3)


SERIES = {  # Kalshi series -> (coin product, kind); series Kalshi doesn't list are simply skipped
    "KXBTC15M": ("BTC-USD", "15m"), "KXETH15M": ("ETH-USD", "15m"),
    "KXBTCD": ("BTC-USD", "hourly"), "KXETHD": ("ETH-USD", "hourly"),
    "KXSOL15M": ("SOL-USD", "15m"), "KXXRP15M": ("XRP-USD", "15m"), "KXDOGE15M": ("DOGE-USD", "15m"),
    "KXSOLD": ("SOL-USD", "hourly"), "KXXRPD": ("XRP-USD", "hourly"), "KXDOGED": ("DOGE-USD", "hourly"),
}
COINS = tuple(sorted({p.split("-")[0] for p, _ in SERIES.values()}))


class CryptoLag:
    def __init__(self, bot, lagwatch, stream, cfg: dict):
        self.bot, self.lw, self.ks, self.cfg = bot, lagwatch, stream, cfg or {}
        self.coins = CoinStream(tuple(sorted({p for p, _ in SERIES.values()}))).start()
        self.markets: dict[str, dict] = {}
        self._last_disc = 0.0
        self._logged_series: set[str] = set()

    # ---- discovery ------------------------------------------------------------- #
    def discover(self) -> None:
        now = time.time()
        keep = {}
        for series, (prod, kind) in SERIES.items():
            try:
                j = self.bot.kdata._req("GET", "/markets", params={"series_ticker": series, "status": "open", "limit": 200})
            except Exception as e:
                log.debug("crypto discover %s: %s", series, e)
                continue
            ms = j.get("markets") or []
            if ms and series not in self._logged_series:
                self._logged_series.add(series)
                m = ms[0]
                log.info("CRYPTO market format %s: %s", series, json.dumps({k: m.get(k) for k in (
                    "ticker", "title", "subtitle", "yes_sub_title", "floor_strike", "cap_strike", "strike_type",
                    "open_time", "close_time", "expected_expiration_time", "functional_strike", "custom_strike")}
                    | {"rules": (m.get("rules_primary") or "")[:240]}))
            for m in ms:
                close = self._ts(m.get("close_time"))
                if not close or not (now < close <= now + 70 * 60):
                    continue
                st = (m.get("strike_type") or "").lower()
                k = m.get("floor_strike") if m.get("floor_strike") is not None else m.get("cap_strike")
                info = {"ticker": m["ticker"], "series": series, "prod": prod, "kind": kind, "close": close,
                        "open": self._ts(m.get("open_time")), "strike": float(k) if k is not None else None,
                        "strike_type": st, "title": m.get("title") or m["ticker"]}
                if kind == "hourly" and (info["strike"] is None or st not in ("greater", "greater_or_equal", "less",
                                                                              "less_or_equal")):
                    continue  # range buckets etc: not handled
                if kind == "hourly":  # only levels near the price matter (far ones never move)
                    px = self.coins.price.get(prod)
                    if px and abs(math.log(px / info["strike"])) > 0.015:
                        continue
                keep[m["ticker"]] = info
        self.markets = keep
        if self.ks:
            self.ks.watch(list(keep))
            if hasattr(self.ks, "watch_books"):  # live books only where speed matters (15-minute markets)
                fast = [t for t, m in keep.items() if m["kind"] == "15m"]
                self.ks.prune_books(fast)
                self.ks.watch_books(fast)
        log.info("CRYPTO watching %d Kalshi crypto markets (%s)", len(keep),
                 ", ".join(f"{s}:{sum(1 for x in keep.values() if x['series'] == s)}" for s in SERIES))

    @staticmethod
    def _ts(v):
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    # ---- pricing ----------------------------------------------------------------- #
    def fair_yes(self, m: dict) -> float | None:
        S = self.coins.price.get(m["prod"])
        if not S or time.time() - self.coins.ts.get(m["prod"], 0) > 10:
            return None
        sig = self.coins.vol(m["prod"])
        if not sig:
            return None
        t = max(1.0, m["close"] - time.time())
        if m["kind"] == "15m":
            K = m["strike"] or (self.coins.price_at(m["prod"], m["open"]) if m.get("open") else None)
            if not K:
                return None
            above = True
        else:
            K = m["strike"]
            above = m["strike_type"].startswith("greater")
        basis = float(self.cfg.get("basis", 0.0003))
        sd = math.sqrt(sig * sig * t + basis * basis)
        p = ncdf(math.log(S / K) / sd)
        return p if above else 1 - p

    # ---- loop ------------------------------------------------------------------- #
    def tick(self) -> None:
        now = time.time()
        if now - self._last_disc > 60:
            self._last_disc = now
            self.discover()
        rate = getattr(self.bot, "fee_rate", 0.07)
        margin = float(self.cfg.get("min_gap", 0.04))
        exit_s = float(self.cfg.get("exit_seconds", 150))
        for tk, m in list(self.markets.items()):
            if m["close"] <= now:
                continue
            q = self.ks.quote(tk) if self.ks else None
            if not q or q.get("yes_ask") is None or q.get("yes_bid") is None:
                continue
            fy = self.fair_yes(m)
            if fy is None:
                continue
            if m["close"] - now <= exit_s:  # last minutes: get out of trades that are going wrong
                self.lw.exit_late(tk, fy, q["yes_bid"], q["yes_ask"], rate, float(self.cfg.get("exit_below", 0.40)))
            if m["close"] - now < 45:
                continue
            kind = "15m" if m["kind"] == "15m" else "hourly"
            grp = f'{m["prod"].split("-")[0]} {kind}'
            # YES side and NO side
            for side, fair, ask, bid in (("yes", fy, q["yes_ask"], q["yes_bid"]),
                                         ("no", 1 - fy, 1 - q["yes_bid"], 1 - q["yes_ask"])):
                self.lw.shadow_bid(f"{tk}|{side}", bid, now)  # keep tracking sold trades at any price
                lo = float(self.cfg.get("min_price", 0.10))  # the model is least reliable on longshots
                if not (lo <= ask <= 1 - lo):
                    continue
                p = {"ticker": tk, "side": side, "title": m["title"], "start": m.get("open"), "close": m["close"]}
                self.lw.check_external(p, grp, fair, ask, bid, now, rate, margin, window=min(60, m["close"] - now))

    def run(self, stop) -> None:
        every = float(self.cfg.get("poll_seconds", 1))
        time.sleep(20)
        while not stop.is_set():
            try:
                if not self.bot.paused:
                    self.tick()
            except Exception as e:
                log.warning("crypto tick failed: %s", e)
            stop.wait(every)
