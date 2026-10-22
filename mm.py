"""
Two-sided market maker for Kalshi 15-minute crypto markets (gabagool-style, done the Kalshi way).

Kalshi has one book per market: a resting "buy YES at b" and a resting "buy NO at n" are a bid and an
offer. When both fill, Kalshi nets YES against NO and pays $1 per pair right away, so each completed
pair locks in 1 - b - n (no maker fee on these series, settlement is free).

Quotes sit around OUR fair value (live coin price model), half_spread away on each side, and are
moved as the coin moves. A trend gate decides which sides may be quoted:
  - fair value outside 20-80%   -> one side is close to certain: no new quotes
  - strong 2-minute move        -> don't quote the side a trend would fill against us
  - volatility spike             -> pull everything
  - BTC trending                 -> same direction applies to the other coins (they follow BTC)
  - first 20s / last 3 minutes   -> no quotes (orders also expire 2.5 min before close on Kalshi's side)
The side that completes an open pair at a profit stays allowed whatever the gate says.
Inventory is capped per window; a daily loss stop pauses new quotes.
"""
from __future__ import annotations

import logging
import math
import time

log = logging.getLogger("mm")


class MarketMaker:
    def __init__(self, bot, crypto, cfg: dict):
        self.bot, self.cl, self.cfg = bot, crypto, cfg or {}
        # ticker -> {"orders": {side: {...}}, "inv": {"yes": [n, cost], "no": [n, cost]}}; kept with the bot's
        # records so a restart remembers what each window holds (old quotes are cancelled at startup)
        self.mk: dict[str, dict] = bot.state.data.setdefault("mm_state", {})
        self._rebuild_from_ledger()
        self._sig: dict[str, tuple] = {}   # product -> (time, momentum z, vol ratio)
        self._cleaned = False
        self.name = "MM"

    def _rebuild_from_ledger(self) -> None:
        """Holdings per window come from the bot's open lots (the source of truth after a restart)."""
        inv: dict[str, dict] = {}
        for l in self.bot.state.data.get("lots", []):
            if str(l.get("whale", "")).startswith("MM ") and l.get("count", 0) > 0:
                d = inv.setdefault(l["ticker"], {"yes": [0, 0.0], "no": [0, 0.0]})
                d[l["side"]][0] += l["count"]
                d[l["side"]][1] += l["cost"]
        for tk, d in inv.items():
            st = self.mk.setdefault(tk, {"orders": {}, "last_poll": 0.0, "ticker": tk,
                                         "coin": tk[2:].split("15M")[0], "close": time.time() + 900})
            st["inv"] = d
            log.info("MM restored holdings %s: YES %d / NO %d", tk, d["yes"][0], d["no"][0])
        for tk, st in self.mk.items():
            if tk not in inv:
                st["inv"] = {"yes": [0, 0.0], "no": [0, 0.0]}

    # ---- settings ---------------------------------------------------------------- #
    def coins(self) -> set[str]:
        return {c.upper() for c in (self.cfg.get("coins") or ["SOL", "XRP", "DOGE"])}

    def covers(self, ticker: str) -> bool:
        return self.cfg.get("enabled", False) and any(ticker.startswith(f"KX{c}15M") for c in self.coins())

    # ---- trend gate ------------------------------------------------------------------ #
    def signals(self, prod: str) -> tuple[float, float]:
        """(2-minute move in standard deviations, short/long volatility ratio) for a coin."""
        now = time.time()
        hit = self._sig.get(prod)
        if hit and now - hit[0] < 2:
            return hit[1], hit[2]
        cs = self.cl.coins
        sig = cs.vol(prod) or 0.0
        S, S0 = cs.price.get(prod), cs.price_at(prod, now - 120)
        mom = math.log(S / S0) / (sig * math.sqrt(120)) if S and S0 and sig else 0.0
        h = list(cs.hist.get(prod, []))[-301:]
        rets = [math.log(h[i][1] / h[i - 1][1]) / max(1, h[i][0] - h[i - 1][0]) ** 0.5 for i in range(1, len(h))
                if h[i - 1][1] > 0]
        short = (sum(r * r for r in rets) / len(rets)) ** 0.5 if len(rets) > 30 else 0.0
        ratio = short / sig if sig else 0.0
        self._sig[prod] = (now, mom, ratio)
        return mom, ratio

    def gate(self, m: dict, fair: float, now: float) -> tuple[bool, bool, str]:
        """(may quote YES, may quote NO, reason)."""
        c = self.cfg
        left, age = m["close"] - now, now - (m.get("open") or now - 999)
        if left < float(c.get("stop_before_close_s", 180)) or age < 20:
            return False, False, "window edge"
        lo = float(c.get("fair_min", 0.20))
        if not (lo <= fair <= 1 - lo):
            return False, False, "one side near certain"
        mom, ratio = self.signals(m["prod"])
        if ratio > float(c.get("vol_spike", 2.0)):
            return False, False, "volatility spike"
        bmom, bratio = self.signals("BTC-USD") if m["prod"] != "BTC-USD" else (mom, ratio)
        if bratio > float(c.get("vol_spike", 2.0)):
            return False, False, "BTC volatility spike"
        lim = float(c.get("trend_z", 2.0))
        up = mom > lim or bmom > lim
        down = mom < -lim or bmom < -lim
        if up and down:
            return False, False, "mixed trend"
        # rising price: buyers of YES would lift our NO bid -> don't quote NO; falling: don't quote YES
        return (not down), (not up), "trend up" if up else ("trend down" if down else "")

    # ---- per-tick work ------------------------------------------------------------------ #
    def daily_pnl(self) -> float:
        from copybot import _today, _local_day
        from datetime import datetime, timezone
        day = _today()
        return sum(c.get("pnl", 0) for c in self.bot.state.data.get("closed", [])
                   if str(c.get("whale", "")).startswith("MM ") and
                   _local_day(datetime.fromtimestamp(c.get("closed", 0), timezone.utc).isoformat()) == day)

    def waiting_usd(self) -> float:
        return sum(o["remaining"] * o["price"] for s in self.mk.values() for o in s["orders"].values())

    def cleanup_orphans(self) -> None:
        """After a restart: cancel any of our old resting quotes on market-maker markets."""
        self._cleaned = True
        for st in self.mk.values():
            for side in list(st.get("orders", {})):  # catch fills on quotes from before the restart, then drop them
                try:
                    self._poll_one(st.get("ticker", ""), st, side)
                except Exception:
                    pass
            st["orders"] = {}
        try:
            j = self.bot.kalshi._req("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
            for o in j.get("orders") or []:
                tk = o.get("ticker") or ""
                if self.covers(tk):
                    try:
                        self.bot.kalshi.cancel_order(o.get("order_id"), tk)
                        log.info("MM cancelled leftover quote %s on %s", o.get("order_id"), tk)
                    except Exception as e:
                        log.warning("MM leftover cancel failed: %s", e)
        except Exception as e:
            log.warning("MM orphan check failed: %s", e)

    def status(self, now: float) -> None:
        if now - getattr(self, "_last_status", 0) < 60:
            return
        self._last_status = now
        quotes = {tk[-14:]: {sd: o["price"] for sd, o in st["orders"].items()} for tk, st in self.mk.items() if st["orders"]}
        inv = {tk[-14:]: (st["inv"]["yes"][0], st["inv"]["no"][0]) for tk, st in self.mk.items()
               if st["inv"]["yes"][0] or st["inv"]["no"][0]}
        log.info("MM status: quotes %s | holding (YES, NO) %s | gate %s | today $%.2f", quotes, inv,
                 getattr(self, "_last_gate", {}), self.daily_pnl())

    def step(self, tk: str, m: dict, fair: float, q: dict, now: float) -> None:
        if self.bot.mode != "live" or not self.bot.kalshi.pkey:
            return
        self.status(now)
        if not self._cleaned:
            self.cleanup_orphans()
        st = self.mk.setdefault(tk, {"orders": {}, "inv": {"yes": [0, 0.0], "no": [0, 0.0]}, "last_poll": 0.0,
                                     "coin": m["prod"].split("-")[0], "close": m["close"], "ticker": tk})
        st["close"] = m["close"]
        if now - st["last_poll"] >= float(self.cfg.get("poll_s", 2)):
            st["last_poll"] = now
            self._poll_fills(tk, st)
        c = self.cfg
        hs, size = float(c.get("half_spread", 0.03)), int(c.get("size", 5))
        inv_y, inv_n = st["inv"]["yes"][0], st["inv"]["no"][0]
        net = inv_y - inv_n
        allow_y, allow_n, why = self.gate(m, fair, now)
        self.__dict__.setdefault("_last_gate", {})[st["coin"]] = why or f"ok fair {fair:.2f}"
        stopped = self.daily_pnl() < -float(c.get("daily_stop_usd", 8))
        if stopped:
            allow_y = allow_n = False
        # lean toward the side that completes a pair
        lean = float(c.get("lean_per_contract", 0.002)) * net
        want = {"yes": round(math.floor((fair - hs - lean) * 100) / 100, 2),
                "no": round(math.floor((1 - fair - hs + lean) * 100) / 100, 2)}
        cap = int(c.get("max_inventory", 10))
        if net >= cap:
            allow_y = False
        if net <= -cap:
            allow_n = False
        # completing an open pair at a profit is always allowed (and priced to stay profitable)
        if inv_y > inv_n:
            avg_y = st["inv"]["yes"][1] / inv_y
            want["no"] = min(want["no"], round(math.floor((1 - avg_y - 0.01) * 100) / 100, 2))
            allow_n = True
        elif inv_n > inv_y:
            avg_n = st["inv"]["no"][1] / inv_n
            want["yes"] = min(want["yes"], round(math.floor((1 - avg_n - 0.01) * 100) / 100, 2))
            allow_y = True
        if m["close"] - now < float(c.get("stop_before_close_s", 180)):
            allow_y = allow_n = False
        # never cross the book (post-only): stay at least a cent under the other side's price
        best_yes_ask, best_no_ask = q["yes_ask"], 1 - q["yes_bid"]
        want["yes"] = min(want["yes"], round(best_yes_ask - 0.01, 2))
        want["no"] = min(want["no"], round(best_no_ask - 0.01, 2))
        for side, ok in (("yes", allow_y), ("no", allow_n)):
            o = st["orders"].get(side)
            px = want[side]
            if not ok or px < 0.02 or px > 0.98:
                if o:
                    self._cancel(tk, st, side, why or "off")
                continue
            if o and (abs(o["price"] - px) < float(c.get("reprice_c", 0.02)) - 1e-9 or now - o["t"] < float(c.get("min_replace_s", 4))):
                continue  # close enough / changed too recently
            if o:
                self._cancel(tk, st, side, "reprice")
                if st["orders"].get(side):
                    continue  # cancel failed: try again next tick
            n = size
            closing = inv_y != inv_n and ((side == "no" and inv_y > inv_n) or (side == "yes" and inv_n > inv_y))
            if closing:
                n = max(1, min(size, abs(net)))  # just enough to complete the pair
            free = self.bot.cash() - 0.5  # may use the future pot's idle cash: it comes back within 15 min
            keep = float(self.cfg.get("keep_free_for_closing", 5.0))  # cash kept free to complete open pairs
            if (not closing and free - n * px < keep) or (closing and free - n * px < 0.1):
                self.__dict__.setdefault("_last_gate", {})[st["coin"]] = f"no cash (${free:.2f} free)"
                continue
            self._place(tk, st, side, n, px, m)

    # ---- orders ----------------------------------------------------------------------------- #
    def _place(self, tk, st, side, n, px, m) -> None:
        try:
            exp = int(m["close"] - float(self.cfg.get("expire_before_close_s", 150)))
            o = self.bot.kalshi.create_order(tk, side, "buy", n, px, None, post_only=True, expiration_ts=exp)
        except Exception as e:
            log.info("MM quote rejected %s %s @ %.2f: %s", tk, side.upper(), px, str(e)[:120])
            return
        oid = o.get("order_id")
        if not oid:
            return
        st["orders"][side] = {"id": oid, "price": px, "count": n, "remaining": n, "filled": 0, "t": time.time()}
        if not getattr(self, "_logged_first", False):
            self._logged_first = True
            log.info("MM first quote live: %s %s x%d @ %.2f", tk, side.upper(), n, px)

    def _cancel(self, tk, st, side, why) -> None:
        o = st["orders"].get(side)
        if not o:
            return
        try:
            self.bot.kalshi.cancel_order(o["id"], tk)
        except Exception as e:
            msg = str(e).lower()
            if "not found" not in msg and "404" not in msg and "executed" not in msg:
                log.info("MM cancel failed %s %s: %s", tk, side, str(e)[:120])
                return
        self._poll_one(tk, st, side)  # catch any fill that happened before the cancel
        st["orders"].pop(side, None)

    def _poll_fills(self, tk, st) -> None:
        for side in list(st["orders"]):
            self._poll_one(tk, st, side)

    def _poll_one(self, tk, st, side) -> None:
        o = st["orders"].get(side)
        if not o:
            return
        try:
            r = self.bot.kalshi.get_order(o["id"])
        except Exception:
            return
        filled = int(float(r.get("fill_count_fp") or r.get("fill_count") or 0))
        new = filled - o["filled"]
        if new > 0:
            o["filled"], o["remaining"] = filled, max(0, o["count"] - filled)
            self._on_fill(tk, st, side, new, o["price"])
        status = str(r.get("status") or "").lower()
        if o["remaining"] <= 0 or status in ("canceled", "cancelled", "executed", "expired"):
            st["orders"].pop(side, None)

    def _on_fill(self, tk, st, side, n, px) -> None:
        name = f'MM · {st["coin"]} 15m'
        self.bot._commit(n * px)
        self.bot.ledger_queue.append(("open", name, tk, side, n, px, f'{st["coin"]} 15m market making'))
        inv = st["inv"][side]
        inv[0] += n
        inv[1] += n * px
        y, no = st["inv"]["yes"], st["inv"]["no"]
        pairs = min(y[0], no[0])
        if pairs > 0:  # Kalshi nets YES against NO: $1 per pair comes back now
            ay, an = y[1] / y[0], no[1] / no[0]
            self.bot.ledger_queue.append(("net", name, tk, pairs, ay, an))
            y[0] -= pairs
            y[1] -= pairs * ay
            no[0] -= pairs
            no[1] -= pairs * an
            log.info("MM pair done %s x%d: YES %.2f + NO %.2f = %.2f -> +%.1fc each", tk, pairs, ay, an, ay + an,
                     100 * (1 - ay - an))
        log.info("MM fill %s %s x%d @ %.2f (holding YES %d / NO %d)", tk, side.upper(), n, px, y[0], no[0])

    def prune(self, keep) -> None:
        """Forget windows that have closed (leftover contracts settle through the normal ledger)."""
        now = time.time()
        for tk in [t for t, s in self.mk.items() if now > s["close"] + 60]:
            s = self.mk.pop(tk)
            if s["inv"]["yes"][0] or s["inv"]["no"][0]:
                log.info("MM window closed %s holding YES %d / NO %d to settlement", tk, s["inv"]["yes"][0],
                         s["inv"]["no"][0])
