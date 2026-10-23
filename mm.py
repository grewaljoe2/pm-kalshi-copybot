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
        for rec in self.bot.state.data.get("lots", []) + self.bot.state.data.get("closed", []):
            if rec.get("whale") == "Recovered" and self.covers(rec.get("ticker", "")):
                rec["whale"] = f'MM · {rec["ticker"][2:].split("15M")[0]} 15m'  # missed MM fills, credit them to MM
        self._fix_old_pairs()
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

    def _fix_old_pairs(self) -> None:
        """Older pair closes booked all the profit on YES and $0 on NO as ordinary sales: relabel them as
        pairs and split the profit evenly (same total)."""
        cl = self.bot.state.data.get("closed", [])
        mm = [c for c in cl if str(c.get("whale", "")).startswith("MM ") and c.get("how") == "sold"]
        for y in [c for c in mm if c["side"] == "yes"]:
            for n in mm:
                if n["side"] == "no" and n["ticker"] == y["ticker"] and n["count"] == y["count"] and \
                        abs(n.get("closed", 0) - y.get("closed", 0)) < 2 and abs(n["pnl"]) < 0.005 and n.get("how") == "sold":
                    half = y["pnl"] / 2
                    y["pnl"], n["pnl"] = round(half, 4), round(half, 4)
                    y["proceeds"] = round(y["cost"] + half, 4)
                    n["proceeds"] = round(n["cost"] + half, 4)
                    y["how"] = n["how"] = "pair"
                    break

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
            st["orders"] = {}  # quotes from before the restart: cancelled below; their fills come in via the position check
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

    def on_ws_fill(self, f: dict) -> None:
        """Live fill feed: a fill on one of our market-maker markets from an order this run isn't tracking
        (a quote left over from a restart, or a cancel that didn't take) goes straight into the records."""
        tk, oid = f.get("market_ticker") or f.get("ticker") or "", f.get("order_id")
        if not self.covers(tk):
            return
        known = self.__dict__.setdefault("_known", {})
        for k in [k for k, t in known.items() if time.time() - t > 1800]:
            known.pop(k, None)
        if oid in known:
            for st in self.mk.values():  # our tracked quote: poll it on the very next tick instead of within 2s
                if any(o.get("id") == oid for o in st.get("orders", {}).values()):
                    st["last_poll"] = 0
                    st["ws_fill_t"] = time.time()
            return
        if str(f.get("is_taker")).lower() in ("true", "1"):
            return  # our own taker orders (pair completion, exits) are booked where they're sent; quotes are always maker
        n = int(float(f.get("count_fp") or f.get("count") or 0))
        if n <= 0:
            return
        side, action = (f.get("side") or "yes").lower(), (f.get("action") or "buy").lower()
        def px_of(sd):
            v = f.get(f"{sd}_price_dollars")
            return float(v) if v is not None else float(f.get(f"{sd}_price") or 0) / 100
        if action == "buy":
            held, px = side, px_of(side)
        else:  # selling one side = buying the other
            held = "no" if side == "yes" else "yes"
            px = 1 - px_of(side)
        coin = tk[2:].split("15M")[0]
        self.bot.ledger_queue.append(("open", f"MM · {coin} 15m", tk, held, n, round(px, 4), f"{coin} 15m (stray fill)"))
        self._stray = getattr(self, "_stray", 0) + 1
        log.warning("MM STRAY FILL %s %s x%d @ %.2f (order %s not tracked) -> booked, exits will manage it",
                    tk, held.upper(), n, px, oid)

    def feed(self, tk, m, fy, q, now) -> None:
        """Latest prices for a market from the crypto loop; a per-coin worker thread does the trading."""
        import threading
        coin = m["prod"].split("-")[0]
        self.__dict__.setdefault("_latest", {})[tk] = (m, fy, dict(q), now)
        workers = self.__dict__.setdefault("_workers", {})
        if coin in workers and workers[coin].is_alive():
            return
        def run():
            while True:
                try:
                    for t, (mm_, f, qq, ts) in list(self._latest.items()):
                        if mm_["prod"].split("-")[0] != coin:
                            continue
                        if time.time() > mm_["close"] + 5:
                            self._latest.pop(t, None)
                            continue
                        if time.time() - ts > 3:
                            continue  # stale prices: wait for fresh ones
                        self.step(t, mm_, f, qq, time.time())
                except Exception as e:
                    log.warning("mm %s worker step failed: %s", coin, str(e)[:120])
                time.sleep(0.25)
        th = threading.Thread(target=run, name=f"mm-{coin}", daemon=True)
        workers[coin] = th
        th.start()
        log.info("MM worker thread started for %s", coin)

    def _start_bg(self) -> None:
        """Slow Kalshi housekeeping (stray-quote sweep, position check, Kalshi P&L) runs in its own thread,
        so the trading loop (stops, take-profits, pairs) never waits on it."""
        if getattr(self, "_bg", None):
            return
        import threading
        def loop():
            while True:
                try:
                    now = time.time()
                    self.sweep(now)
                    self.truth_stop(now)
                except Exception as e:
                    log.warning("MM housekeeping failed: %s", str(e)[:120])
                time.sleep(5)
        self._bg = threading.Thread(target=loop, name="mm-housekeeping", daemon=True)
        self._bg.start()

    def sweep(self, now: float) -> None:
        """Every 20s: any resting order of ours on a market-maker market that we aren't tracking (a cancel
        that didn't really go through, or a quote left from a restart) gets cancelled and checked."""
        if now - getattr(self, "_last_sweep", 0) < 20:
            return
        self._last_sweep = now
        if now - getattr(self, "_last_pos", 0) >= 20:
            self._last_pos = now
            self.check_positions()
        try:
            tracked = {o["id"] for st in self.mk.values() for o in st.get("orders", {}).values()}
            j = self.bot.kalshi._req("GET", "/portfolio/orders", params={"status": "resting", "limit": 200})
            for o in j.get("orders") or []:
                tk, oid = o.get("ticker") or "", o.get("order_id")
                if not self.covers(tk) or oid in tracked:
                    continue
                try:
                    self.bot.kalshi.cancel_order(oid, tk)
                    log.info("MM swept stray quote %s on %s (cancel verified)", oid, tk)
                except Exception as e:
                    log.warning("MM sweep cancel failed %s: %s", oid, str(e)[:120])
        except Exception as e:
            log.warning("MM sweep failed: %s", str(e)[:120])

    def truth_stop(self, now: float) -> bool:
        """Off switch on Kalshi's real numbers: market maker down kill_usd today (after fees) -> no new quotes."""
        if now - getattr(self, "_truth_t", 0) < (60 if getattr(self, "_truth_off", False) else 300):
            return getattr(self, "_truth_off", False)
        self._truth_t = now
        v = self.bot.truth_since_start("mm", tuple(self.coins()))
        if v is None:
            return getattr(self, "_truth_off", False)
        off = v < -float(self.cfg.get("kill_usd", 5))
        if off and not getattr(self, "_truth_off", False):
            log.warning("MM OFF for today: Kalshi says market making is %+.2f after fees since start (limit -$%.0f)", v,
                        float(self.cfg.get("kill_usd", 5)))
        self._truth_off = off
        self._truth_v = v
        return off

    def check_positions(self) -> None:
        """Every minute: Kalshi's position on each market-maker market vs our records. A difference seen
        twice in a row (not just a fill still on its way) is booked so the exits can manage it."""
        if self.bot.ledger_queue or time.time() - getattr(self, "_last_fill_t", 0) < 8:
            return  # fills still on their way into the records
        try:
            j = self.bot.kalshi._req("GET", "/portfolio/positions", params={"limit": 200, "settlement_status": "unsettled"})
        except Exception as e:
            log.warning("MM position check failed: %s", str(e)[:120])
            return
        seen = self.__dict__.setdefault("_mismatch", {})
        now_bad = {}
        for r in j.get("market_positions") or []:
            tk = r.get("ticker") or ""
            if not self.covers(tk):
                continue
            k_net = float(r.get("position_fp") or r.get("position") or 0)
            mine = self.bot.state.position(tk, "yes") - self.bot.state.position(tk, "no")
            d = int(round(k_net - mine))
            if d == 0:
                continue
            now_bad[tk] = d
            if not seen.get(tk) or (seen[tk] > 0) != (d > 0):
                log.info("MM position check %s: Kalshi %+d vs records %+d (re-checking in 20s)", tk, k_net, mine)
                continue
            held = "yes" if d > 0 else "no"
            expo = float(r.get("market_exposure_dollars") or 0)
            px = round(expo / abs(k_net), 4) if k_net and expo and (k_net > 0) == (d > 0) else 0.5
            coin = tk[2:].split("15M")[0]
            self.bot.ledger_queue.append(("open", f"MM · {coin} 15m", tk, held, abs(d), px, f"{coin} 15m (found on Kalshi)"))
            log.warning("MM POSITION FIX %s: booked %s x%d @ %.2f so records match Kalshi (%+d)", tk, held.upper(), abs(d), px, k_net)
            now_bad.pop(tk)
        self._mismatch = now_bad

    def status(self, now: float) -> None:
        if now - getattr(self, "_last_status", 0) < 60:
            return
        self._last_status = now
        quotes = {st["coin"] + tk[-5:]: {sd: o["price"] for sd, o in st["orders"].items()} for tk, st in self.mk.items() if st["orders"]}
        inv = {st["coin"] + tk[-5:]: (st["inv"]["yes"][0], st["inv"]["no"][0]) for tk, st in self.mk.items()
               if st["inv"]["yes"][0] or st["inv"]["no"][0]}
        log.info("MM status: quotes %s | holding (YES, NO) %s | gate %s | today $%.2f (Kalshi says %s)", quotes, inv,
                 getattr(self, "_last_gate", {}), self.daily_pnl(),
                 "?" if getattr(self, "_truth_v", None) is None else f"{self._truth_v:+.2f}")

    def step(self, tk: str, m: dict, fair: float, q: dict, now: float) -> None:
        if self.bot.mode != "live" or not self.bot.kalshi.pkey:
            return
        self.status(now)
        if not self._cleaned:
            self.cleanup_orphans()
        st = self.mk.setdefault(tk, {"orders": {}, "inv": {"yes": [0, 0.0], "no": [0, 0.0]}, "last_poll": 0.0,
                                     "coin": m["prod"].split("-")[0], "close": m["close"], "ticker": tk})
        st["close"] = m["close"]
        self._start_bg()
        if self.cfg.get("paused"):  # no new quotes; still manage what we hold
            for side in list(st["orders"]):
                self._cancel(tk, st, side, "paused")
            self._sync_inv(tk, st)
            self._take_profit(tk, st, fair, q, m["close"] - now)
            return
        if now - st["last_poll"] >= float(self.cfg.get("poll_s", 2)):
            st["last_poll"] = now
            self._poll_fills(tk, st)
        c = self.cfg
        self._markouts(st, fair, now)
        self._sync_inv(tk, st)
        self._take_profit(tk, st, fair, q, m["close"] - now)
        if self._pair_up(tk, st, fair, q, m["close"] - now, now):
            return  # traded this tick; quote again next tick with fresh numbers
        hs, size = float(c.get("half_spread", 0.03)), int(c.get("size", 5))
        inv_y, inv_n = st["inv"]["yes"][0], st["inv"]["no"][0]
        net = inv_y - inv_n
        allow_y, allow_n, why = self.gate(m, fair, now)
        self.__dict__.setdefault("_last_gate", {})[st["coin"]] = why or f"ok fair {fair:.2f}"
        stopped = self.daily_pnl() < -float(c.get("daily_stop_usd", 8)) or getattr(self, "_truth_off", False)
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
        if c.get("pairs_only", True):  # holding one side: only quote the side that completes the pair, never add more
            if net > 0:
                allow_y = False
            if net < 0:
                allow_n = False
        # completing an open pair at a profit is always allowed (and priced to stay profitable)
        if inv_y > inv_n:
            avg_y = st["inv"]["yes"][1] / inv_y
            want["no"] = min(want["no"], round(math.floor((1 - avg_y - float(c.get("pair_min_c", 0.02))) * 100) / 100, 2))
            allow_n = True
        elif inv_n > inv_y:
            avg_n = st["inv"]["no"][1] / inv_n
            want["yes"] = min(want["yes"], round(math.floor((1 - avg_n - float(c.get("pair_min_c", 0.02))) * 100) / 100, 2))
            allow_y = True
        if m["close"] - now < float(c.get("open_until_left_s", 300)):  # late in the window: only complete pairs
            if not (inv_y < inv_n):
                allow_y = False
            if not (inv_n < inv_y):
                allow_n = False
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
            if o and o["price"] > px + float(c.get("pull_c", 0.01)) + 1e-9:
                self._cancel(tk, st, side, "moved against")  # value dropped under our quote: pull it now
                continue  # re-quote on a later tick
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

    def _markouts(self, st, fair, now) -> None:
        """Fill quality: value of what we bought 10s and 60s after the fill minus what we paid.
        Consistently negative = our quotes are getting picked off."""
        agg = self.__dict__.setdefault("_mo", {"10": [0.0, 0], "60": [0.0, 0]})
        for mk in list(st.get("marks", [])):
            v = fair if mk["side"] == "yes" else 1 - fair
            for k, secs in (("10", 10), ("60", 60)):
                if k not in mk and now - mk["t"] >= secs:
                    mk[k] = v - mk["px"]
                    agg[k][0] += mk[k] * mk["n"]
                    agg[k][1] += mk["n"]
            if "60" in mk:
                st["marks"].remove(mk)
        if agg["60"][1] and now - getattr(self, "_mo_log", 0) > 600:
            self._mo_log = now
            log.info("MM_MARKOUT avg per contract: after 10s %+.1fc (%d), after 60s %+.1fc (%d)",
                     100 * agg["10"][0] / max(1, agg["10"][1]), agg["10"][1],
                     100 * agg["60"][0] / max(1, agg["60"][1]), agg["60"][1])

    def _pair_up(self, tk, st, fair, q, left, now) -> bool:
        """Hold both sides, not one: when only one side has filled, buy the other side right away if the pair
        still costs under $1 after the fee; if that isn't possible within max_single_s, sell the single side
        (small loss cap). Also take any pair the book offers for under pair_arb_max. True = traded."""
        from copybot import Target
        if not self.cfg.get("pairs_only", True) or self.bot.ledger_queue:
            return False
        rate = getattr(self.bot, "fee_rate", 0.07)
        fee = lambda p: rate * p * (1 - p)
        ask = {"yes": q["yes_ask"], "no": 1 - q["yes_bid"]}
        bid = {"yes": q["yes_bid"], "no": 1 - q["yes_ask"]}
        y, n = st["inv"]["yes"][0], st["inv"]["no"][0]
        net = y - n
        if net == 0:
            st.pop("single_t", None)
            k = int(self.cfg.get("size", 3))
            cost = ask["yes"] + ask["no"] + fee(ask["yes"]) + fee(ask["no"])
            if left > 120 and 0.02 <= ask["yes"] <= 0.98 and cost <= float(self.cfg.get("pair_arb_max", 0.98)) \
                    and self.bot.cash() > k * cost + 1:
                f1, p1 = self.bot._execute(Target(tk, "yes"), "buy", k, ask["yes"])
                if f1:
                    self._on_fill(tk, st, "yes", f1, p1 / f1, commit=False)
                    f2, p2 = self.bot._execute(Target(tk, "no"), "buy", f1, ask["no"])
                    if f2:
                        self._on_fill(tk, st, "no", f2, p2 / f2, commit=False)
                    log.info("MM free pair %s: YES %.2f + NO %.2f + fees = %.3f (x%d/%d)", tk, ask["yes"], ask["no"], cost, f1, f2)
                    return True
            return False
        held, other = ("yes", "no") if net > 0 else ("no", "yes")
        k = abs(net)
        avg = st["inv"][held][1] / st["inv"][held][0]
        oa = ask[other]
        if 0.01 <= oa <= 0.99 and avg + oa + fee(oa) <= 1 - float(self.cfg.get("pair_take_min", 0.01)) \
                and self.bot.cash() > k * (oa + fee(oa)) + 0.5:
            if st["orders"].get(other):
                self._cancel(tk, st, other, "completing pair now")
            f, pr = self.bot._execute(Target(tk, other), "buy", k, oa)
            if f:
                self._on_fill(tk, st, other, f, pr / f, commit=False)
                rt = time.time() - st.get("ws_fill_t", time.time())
                log.info("MM pair completed at once %s: %s %.2f + %s %.2f + fee -> %+.1fc each (%.1fs after the fill)", tk,
                         held.upper(), avg, other.upper(), oa, 100 * (1 - avg - oa - fee(oa)), rt)
                st.pop("single_t", None)
                return True
        t0 = st.setdefault("single_t", now)
        b = bid[held]
        max_loss = float(self.cfg.get("max_single_loss", 0.03))
        if left <= float(self.cfg.get("open_until_left_s", 300)):  # late: don't carry a single side into settlement
            max_loss = float(self.cfg.get("late_single_loss", 0.10))
        elif now - t0 > float(self.cfg.get("single_patience_s", 90)):  # stuck a while: accept a bit more to get flat
            max_loss = float(self.cfg.get("stuck_single_loss", 0.06))
        if now - t0 > float(self.cfg.get("max_single_s", 30)) and b is not None and b >= 0.02 and \
                b >= avg - max_loss and time.time() >= st.get("sell_wait", 0):
            f, pr = self.bot._execute(Target(tk, held), "sell", k, b)
            if f:
                px = pr / f
                self.bot.ledger_queue.append(("close", f'MM · {st["coin"]} 15m', tk, held, f, px, f'{st["coin"]} 15m market making'))
                inv = st["inv"][held]
                inv[1] -= f * avg
                inv[0] -= f
                if st["orders"].get(other):
                    self._cancel(tk, st, other, "single side closed")
                log.info("MM single side closed %s %s x%d @ %.2f (cost %.2f): no pair within %ds", tk, held.upper(), f, px,
                         avg, int(now - t0))
                st.pop("single_t", None)
                return True
            st["sell_wait"] = time.time() + 10
        return False

    def _sync_inv(self, tk, st) -> None:
        """Holdings always match the records (which match Kalshi after reconcile + netting),
        including positions recovered after a restart."""
        if self.bot.ledger_queue:  # our own fills/sales aren't in the records yet; keep local counts
            return
        d = {"yes": [0, 0.0], "no": [0, 0.0]}
        for l in list(self.bot.state.data.get("lots", [])):
            if l["ticker"] == tk and l.get("count", 0) > 0:
                d[l["side"]][0] += l["count"]
                d[l["side"]][1] += l["cost"]
        if (d["yes"][0], d["no"][0]) != (st["inv"]["yes"][0], st["inv"]["no"][0]):
            log.info("MM holdings %s synced to records: YES %d / NO %d (was %d / %d)", tk, d["yes"][0], d["no"][0],
                     st["inv"]["yes"][0], st["inv"]["no"][0])
        st["inv"] = d

    def _take_profit(self, tk, st, fair, q, left) -> None:
        """One-sided holdings that are up: sell instead of riding them to settlement.
        Sell when the bid is up take_profit_pct on cost, or pays more than the model's value, or (last
        4 minutes) anything in profit after the fee. Selling needs no cash."""
        from copybot import Target
        rate = getattr(self.bot, "fee_rate", 0.07)
        for side, other in (("yes", "no"), ("no", "yes")):
            n = st["inv"][side][0] - st["inv"][other][0]
            if n <= 0:
                continue
            avg = st["inv"][side][1] / st["inv"][side][0]
            bid = q["yes_bid"] if side == "yes" else 1 - q["yes_ask"]
            value = fair if side == "yes" else 1 - fair
            if bid is None or bid <= 0.01:
                continue
            net = bid - avg - rate * bid * (1 - bid)
            pk = st.setdefault("peak", {})
            pk[side] = max(pk.get(side, 0.0), bid)
            from lagwatch import profit_floor
            floor = profit_floor(avg, pk[side], 0.0)  # maker buys pay no fee; selling pays rate on the bid
            floor = floor and floor + rate * bid * (1 - bid)
            if self.cfg.get("profit_ladder", False) and floor and bid <= floor and value <= floor + 0.08:
                why_l = f"profit ladder: peak {pk[side]:.2f}, floor {floor:.2f}"
            else:
                why_l = ""
            tp = float(self.cfg.get("take_profit_pct", 0.5))
            why = ("up %.0f%%" % (100 * (bid / avg - 1)) if net >= max(0.02, tp * avg) else
                   "bid above value" if (bid >= value + 0.01 and net > 0) else
                   "locking profit near close" if (left <= float(self.cfg.get("lock_last_s", 240)) and net >= 0.01) else
                   why_l)
            if not why and left >= 90 and value <= avg - float(self.cfg.get("stop_c", 0.10)) and bid >= 0.75 * value:
                why = f"model stop: worth {value:.2f} vs cost {avg:.2f}"  # a market maker shouldn't ride a 10c+ loser
            if not why and left <= 240 and value < 0.25 and bid >= 0.75 * value and bid >= 0.02:
                why = f"late exit: probably losing (worth {value:.2f}), someone pays {bid:.2f}"
            if not why:
                continue
            if time.time() < st.get("sell_wait", 0):
                continue
            filled, proceeds = self.bot._execute(Target(tk, side), "sell", n, bid)
            if not filled:
                st["sell_wait"] = time.time() + 10  # nothing sold (no buyer, or Kalshi holds less): re-check first
                self._last_pos = 0
                continue
            px = proceeds / filled
            name = f'MM · {st["coin"]} 15m'
            self.bot.ledger_queue.append(("close", name, tk, side, filled, px, f'{st["coin"]} 15m market making'))
            inv = st["inv"][side]
            inv[1] -= filled * avg
            inv[0] -= filled
            if inv[0] <= 0:
                st.get("peak", {}).pop(side, None)
            if st["orders"].get(other):  # the pair-completing quote isn't needed any more
                self._cancel(tk, st, other, "profit taken")
            log.info("MM take profit %s %s x%d @ %.2f (cost %.2f, %+.1fc each) %s", tk, side.upper(), filled, px, avg,
                     100 * net, why)

    # ---- orders ----------------------------------------------------------------------------- #
    def _group(self, tk, st) -> str | None:
        """Kalshi order group per window: if more than breaker_contracts fill within 15s, Kalshi itself
        cancels all our quotes in that market (circuit breaker against getting run over)."""
        lim = int(self.cfg.get("breaker_contracts", 0) or 0)
        if lim <= 0 or getattr(self, "_no_groups", False):
            return None
        if st.get("group"):
            return st["group"]
        if time.time() < st.get("group_wait", 0):
            return "WAIT"
        try:
            j = self.bot.kalshi._req("POST", "/portfolio/order_groups/create",
                                     json={"contracts_limit": lim, "exchange_index": self.bot.kalshi.market_shard(tk)})
            st["group"] = j.get("order_group_id")
            if not st["group"]:
                raise RuntimeError(f"no order_group_id in {str(j)[:120]}")
            log.info("MM circuit breaker set for %s: Kalshi cancels all quotes if %d+ fill in 15s", tk, lim)
            return st["group"]
        except Exception as e:
            log.warning("MM order group create failed (quoting without it): %s", str(e)[:160])
            self._no_groups = True
            return None

    def _place(self, tk, st, side, n, px, m) -> None:
        grp = self._group(tk, st)
        if grp == "WAIT":
            return  # breaker tripped recently: sit out a little
        try:
            exp = int(m["close"] - float(self.cfg.get("expire_before_close_s", 150)))
            o = self.bot.kalshi.create_order(tk, side, "buy", n, px, None, post_only=True, expiration_ts=exp,
                                             order_group_id=grp)
        except Exception as e:
            msg = str(e).lower()
            if grp and "group" in msg:  # breaker tripped (or group refused): new group after a 2-min pause
                log.warning("MM CIRCUIT BREAKER on %s: Kalshi pulled our quotes (%s); pausing 2 min", tk, str(e)[:100])
                st["group"], st["group_wait"] = None, time.time() + 120
                for sd in list(st["orders"]):
                    self._poll_one(tk, st, sd)
                    st["orders"].pop(sd, None)
                return
            log.info("MM quote rejected %s %s @ %.2f: %s", tk, side.upper(), px, str(e)[:120])
            return
        oid = o.get("order_id")
        if not oid:
            return
        st["orders"][side] = {"id": oid, "price": px, "count": n, "remaining": n, "filled": 0, "t": time.time()}
        self.__dict__.setdefault("_known", {})[oid] = time.time()
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
        if st["orders"].get(side) is o:
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

    def _on_fill(self, tk, st, side, n, px, commit: bool = True) -> None:
        self._last_fill_t = time.time()
        st.setdefault("marks", []).append({"t": time.time(), "side": side, "px": px, "n": n})
        name = f'MM · {st["coin"]} 15m'
        if commit:
            self.bot._commit(n * px)
        self.bot.ledger_queue.append(("open", name, tk, side, n, px, f'{st["coin"]} 15m market making'))
        inv = st["inv"][side]
        inv[0] += n
        inv[1] += n * px
        y, no = st["inv"]["yes"], st["inv"]["no"]
        pairs = min(y[0], no[0])
        if pairs > 0:  # Kalshi nets YES against NO: $1 per pair comes back now (the ledger nets it too)
            ay, an = y[1] / y[0], no[1] / no[0]
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
