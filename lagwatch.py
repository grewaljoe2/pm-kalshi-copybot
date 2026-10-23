"""
Lag watcher / trader: when Kalshi's price trails Polymarket's for the same bet, buy on Kalshi.

Which bets: every sports game listed on both exchanges. Every 10 minutes it pulls Polymarket's
sports events, matches each market to Kalshi with the bot's SportsMatcher (winner, spread,
total, maps), and keeps the ones that are live now or start within the next 12 hours.
Bets from whale trades and top-trader picks are added too.

How often: live games every 5s, upcoming (pre-game) games every 30s. Kalshi prices come in
batches (many tickers per request); Polymarket books come in one batched request too.

A "gap" = Kalshi's ask is cheaper than Polymarket's mid by more than the Kalshi fee + margin.
For each gap it records size, length, and whether we could have sold on Kalshi for 1c+ per
contract after both fees within the check window (60s live, 10 min pre-game).

Evidence is kept per league and per phase (e.g. "NBA", "NBA pre", "ATP", "CS2"). Each group
switches itself from watch to trade separately once it has enough gaps across enough games
with a high enough after-fee success rate. In trade mode it buys the gap (small size), sells
once it nets 1c+ after fees, and otherwise holds a bet bought below Polymarket's price.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

log = logging.getLogger("lagwatch")
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"


def _px(m: dict, f: str):
    v = m.get(f"{f}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(f)
    return float(v) / 100 if v not in (None, 0) else None


def league_of(ticker: str) -> str:
    """KXNBAGAME-.. -> NBA, KXATPMATCH-.. -> ATP, KXCS2MAP-.. -> CS2, KXNFLSPREAD -> NFL."""
    s = ticker.split("-", 1)[0].upper()
    s = s[2:] if s.startswith("KX") else s
    s = re.sub(r"(1H|2H|1Q|2Q|3Q|4Q)?(GAME|SPREAD|TOTAL|TEAMTOTAL|MATCH|MAP|WINNER|BTTS|TIE)$", "", s)
    return s or "OTHER"


def _jl(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


LADDER = ((1.00, 0.70), (0.60, 0.60), (0.30, 0.40), (0.10, 0.0))  # peak gain on cost -> share of it kept


def profit_floor(entry: float, peak: float, rate: float, ladder=LADDER) -> float | None:
    """Profit ladder: once a holding has been up 10%+, never let it go back below break-even; the
    further it ran, the more of the gain is locked (30%+ keeps 40%, 60%+ keeps 60%, 100%+ keeps 70%).
    Returns the lowest bid we accept before selling, or None while not armed yet."""
    if entry <= 0 or peak <= entry:
        return None
    up = peak / entry - 1
    fee = lambda b: rate * b * (1 - b)
    be = entry + 2 * fee(entry) + 0.01  # selling here is roughly flat after both fees
    for lvl, keep in ladder:
        if up >= lvl:
            return round(max(be, entry + keep * (peak - entry)), 4)
    return None


class LagWatch:
    def __init__(self, bot, cfg: dict):
        self.bot = bot
        self.cfg = cfg or {}
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "pm-kalshi-copybot/1.0"})
        self.open: dict[str, dict] = {}
        self.follow: list[dict] = []
        self._last_disc = 0.0
        self._last_pre = 0.0
        import threading
        self.lock = threading.RLock()
        self.ks = None  # live Kalshi stream (set by the app)
        st = bot.state.data.setdefault("lagwatch", {})
        for k, v in (("gaps", 0), ("converged", 0), ("examples", []), ("pairs_watched", 0),
                     ("trades", []), ("games", []), ("groups", {})):
            st.setdefault(k, v)
        if not st.get("strict_v2"):
            st.update(gaps=0, converged=0, examples=[], games=[], strict_v2=True)
        if not st.get("groups_v1"):  # tonight's evidence was NBA live games: carry it over
            st["groups_v1"] = True
            if st.get("mode") == "trade":
                st["groups"]["NBA"] = {"gaps": st["gaps"], "converged": st["converged"],
                                       "games": list(set(st.get("games", []))), "mode": "trade",
                                       "switched_at": st.get("switched_at")}
        if not st.get("verified_v1"):  # earlier gaps weren't checked against the order book: re-prove
            st["verified_v1"] = True
            st.update(gaps=0, converged=0, examples=[], groups={}, mode="watch", phantom=0)
        if not st.get("crypto_reset_v1"):  # first crypto minutes used an unwarmed model: discard
            st["crypto_reset_v1"] = True
            st["groups"] = {k: v for k, v in st.get("groups", {}).items() if k.split(" ")[0] not in ("BTC", "ETH")}
        self.stats = st
        if not st.get("no_price_fix_v1"):
            st["no_price_fix_v1"] = True
            data = bot.state.data
            for rec in data.get("closed", []) + data.get("lots", []):
                if str(rec.get("whale", "")).startswith("Lag trade") and rec.get("side") == "no" and rec.get("count"):
                    rec["cost"] = round(rec["count"] - rec["cost"], 4)  # count x (1 - yes-leg price)
                    if "proceeds" in rec:
                        rec["pnl"] = round(rec["proceeds"] - rec["cost"] - rec.get("fees", 0), 4)
            for x in st.get("trades", []):
                if x.get("side") == "no":
                    x["entry"] = round(1 - x["entry"], 4)
            log.warning("LAGWATCH corrected lag NO trades recorded at the YES-leg price")
        for name in list(st["groups"]):  # settings may have changed: re-check every league now
            self._maybe_switch(name)

    # ---- discovery ---------------------------------------------------------- #
    def discover(self) -> None:
        """Find every Polymarket sports market that the bot can match to Kalshi."""
        now = time.time()
        found, scanned, in_window = 0, 0, 0
        from collections import Counter
        why = Counter()
        pairs = self.bot.__dict__.setdefault("pairs", {})
        for offset in range(0, int(self.cfg.get("discover_max_events", 600)), 100):
            try:
                from datetime import datetime, timezone, timedelta
                iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
                nowdt = datetime.now(timezone.utc)
                r = self.s.get(f"{GAMMA}/events", params={
                    "active": "true", "closed": "false", "tag_slug": "sports", "limit": 100, "offset": offset,
                    "end_date_min": iso(nowdt - timedelta(hours=6)), "end_date_max": iso(nowdt + timedelta(days=2))},
                    timeout=20)
                r.raise_for_status()
                evs = r.json()
            except Exception as e:
                log.warning("lagwatch discover failed: %s", e)
                break
            if not evs:
                break
            for ev in evs:
                for m in ev.get("markets") or []:
                    scanned += 1
                    start = self._start(m, ev)
                    if not start and m.get("conditionId"):
                        t0 = {"slug": m.get("slug"), "eventSlug": ev.get("slug"), "title": m.get("question") or "",
                              "outcome": "", "conditionId": m.get("conditionId")}
                        c = self.bot.sports.classify(t0) or {}
                        if c.get("date") and "skip" not in c:  # a dated game market: get its start once
                            try:
                                start = self.bot.pm.market_info(m["conditionId"]).get("start")
                            except Exception:
                                start = None
                    if not start or not (now - 4 * 3600 <= start <= now + 12 * 3600):
                        why["no start" if not start else "not today"] += 1
                        continue
                    in_window += 1
                    outs, toks = _jl(m.get("outcomes")), _jl(m.get("clobTokenIds"))
                    for i, (o, tok) in enumerate(zip(outs, toks)):
                        t = {"slug": m.get("slug"), "eventSlug": ev.get("slug"), "title": m.get("question") or ev.get("title"),
                             "outcome": o, "outcomeIndex": i, "conditionId": m.get("conditionId")}
                        try:
                            hit, reason = self.bot.sports.lookup(t)
                        except Exception as e:
                            hit, reason = None, f"error {type(e).__name__}"
                        if not hit:
                            why[str(reason)[:50]] += 1
                            if "part-game" in str(reason) and len(self.__dict__.setdefault("_pg", [])) < 25:
                                self._pg.append({"slug": t["slug"], "ev": t["eventSlug"], "title": t["title"], "o": o})
                            if len(self.__dict__.setdefault("_samples", [])) < 5:
                                self._samples.append({"slug": t["slug"], "eventSlug": t["eventSlug"], "title": t["title"],
                                                      "outcome": o, "reason": str(reason)[:60]})
                            continue
                        key = f'{hit["ticker"]}|{hit["side"]}'
                        p = pairs.setdefault(key, {"cond": t["conditionId"], "outcome": str(o), "ticker": hit["ticker"],
                                                   "side": hit["side"], "title": t["title"] or "", "added": now})
                        p.update(start=start, token=str(tok), seen=now)
                        found += 1
            time.sleep(0.3)
        log.info("LAGWATCH discovery: %d Polymarket markets scanned, %d in the next 12h, %d matched to Kalshi "
                 "(%d pairs tracked); misses %s; samples %s", scanned, in_window, found, len(pairs),
                 why.most_common(6), json.dumps(getattr(self, "_samples", []))[:900])
        if getattr(self, "_pg", None):
            for x in self._pg:
                log.info("PARTGAME_SAMPLE %s", json.dumps(x))
            self._pg = []

    def _safe_discover(self) -> None:
        try:
            self.discover()
        except Exception as e:
            log.warning("lagwatch discovery crashed: %s", e)

    @staticmethod
    def _start(m: dict, ev: dict) -> float | None:
        from datetime import datetime
        for v in (m.get("gameStartTime"), m.get("game_start_time"), ev.get("gameStartTime"), ev.get("startTime"),
                  m.get("eventStartTime")):
            if not v:
                continue
            txt = str(v).strip().replace(" ", "T").replace("Z", "+00:00")
            if re.search(r"[+-]\d\d$", txt):
                txt += ":00"
            try:
                return datetime.fromisoformat(txt).timestamp()
            except ValueError:
                continue
        return None

    # ---- prices (batched) ------------------------------------------------------ #
    def pm_books(self, tokens: list[str]) -> dict[str, tuple]:
        out = {}
        for i in range(0, len(tokens), 50):
            chunk = tokens[i:i + 50]
            try:
                r = self.s.post(f"{CLOB}/books", json=[{"token_id": t} for t in chunk], timeout=10)
                r.raise_for_status()
                for b in r.json() or []:
                    bids = [float(x["price"]) for x in b.get("bids") or []]
                    asks = [float(x["price"]) for x in b.get("asks") or []]
                    out[str(b.get("asset_id"))] = (max(bids) if bids else None, min(asks) if asks else None)
            except Exception:
                for t in chunk:  # fall back to one book at a time
                    try:
                        r = self.s.get(f"{CLOB}/book", params={"token_id": t}, timeout=8)
                        b = r.json()
                        bids = [float(x["price"]) for x in b.get("bids") or []]
                        asks = [float(x["price"]) for x in b.get("asks") or []]
                        out[t] = (max(bids) if bids else None, min(asks) if asks else None)
                    except Exception:
                        pass
        return out

    def k_markets(self, tickers: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            try:
                j = self.bot.kdata._req("GET", "/markets", params={"tickers": ",".join(chunk), "limit": len(chunk)})
                for m in j.get("markets") or []:
                    out[m.get("ticker")] = m
            except Exception as e:
                log.debug("kalshi batch failed: %s", e)
        return out

    def real_ask(self, ticker: str, side: str) -> tuple[float | None, int]:
        """Best ask and size for `side`. Fast path: our live copy of the book from the stream (no request).
        It is checked against Kalshi's order book first and only trusted after 50 checks agree 95%+;
        every 20th use is re-checked and it switches itself off if agreement drops."""
        fb = self.stats.setdefault("fast_book", {"checks": 0, "agree": 0, "on": False, "recent": []})
        local = self.ks.local_ask(ticker, side) if getattr(self, "ks", None) and hasattr(self.ks, "local_ask") else None
        fb["uses"] = fb.get("uses", 0) + (1 if local else 0)
        if local and fb.get("on") and fb["uses"] % 20:
            return local
        rest = self._rest_ask(ticker, side)
        if local:
            ok = (local[0] is None and rest[0] is None) or (
                local[0] is not None and rest[0] is not None and abs(local[0] - rest[0]) <= 0.011)
            fb["checks"] += 1
            fb["agree"] += 1 if ok else 0
            fb["recent"] = (fb.get("recent", []) + [1 if ok else 0])[-50:]
            if not ok and fb["checks"] - fb.get("last_mis_log", -100) > 10:
                fb["last_mis_log"] = fb["checks"]
                log.info("FASTBOOK mismatch %s %s: live copy %s vs Kalshi %s", ticker, side, local, rest)
            rate = sum(fb["recent"]) / len(fb["recent"])
            if not fb["on"] and len(fb["recent"]) >= 50 and rate >= 0.95:
                fb["on"] = True
                log.warning("FASTBOOK on: live order books matched Kalshi %d/%d times", sum(fb["recent"]), len(fb["recent"]))
            elif fb["on"] and len(fb["recent"]) >= 20 and rate < 0.90:
                fb["on"] = False
                log.warning("FASTBOOK off: live order books drifted (%.0f%% agreement)", 100 * rate)
        return rest

    def _rest_ask(self, ticker: str, side: str) -> tuple[float | None, int]:
        """Kalshi's book lists bids only: YES ask = 1 - best NO bid, and the reverse."""
        j = self.bot.kdata._req("GET", f"/markets/{ticker}/orderbook", params={"depth": 5})
        ob = j.get("orderbook") or j.get("orderbook_fp") or {}
        other = "no" if side == "yes" else "yes"
        levels = ob.get(f"{other}_dollars") or ob.get(other) or []
        best, qty = None, 0
        for lv in levels:
            try:
                px, q = float(lv[0]), float(lv[1])
            except (TypeError, ValueError, IndexError):
                continue
            px = px / 100 if px > 1 else px
            if best is None or px > best:
                best, qty = px, q
        if best is None:
            return None, 0
        return round(1 - best, 4), int(qty)

    # ---- groups ---------------------------------------------------------------- #
    @staticmethod
    def group_of(p: dict, live: bool) -> str:
        lg = league_of(p["ticker"])
        return lg if live else f"{lg} pre"

    def _g(self, name: str) -> dict:
        return self.stats["groups"].setdefault(name, {"gaps": 0, "converged": 0, "games": [], "mode": "watch"})

    def trading(self, group: str) -> bool:
        return self._g(group).get("mode") == "trade"

    # ---- loop ----------------------------------------------------------------- #
    def select(self) -> tuple[list[dict], list[dict]]:
        now = time.time()
        live, pre = [], []
        for p in list((getattr(self.bot, "pairs", {}) or {}).values()):
            if "start" not in p:  # from whale trades: look up once
                try:
                    info = self.bot.pm.market_info(p["cond"])
                    p["start"], p["token"] = info.get("start"), (info.get("tokens") or {}).get(p["outcome"])
                except Exception:
                    continue
            st, tok = p.get("start"), p.get("token")
            if not st or not tok or p.get("bad", 0) >= 2:
                continue
            if st <= now <= st + 4 * 3600:
                live.append(p)
            elif now < st <= now + 12 * 3600:
                pre.append(p)
        pre.sort(key=lambda p: p["start"])
        return live[: int(self.cfg.get("max_live", 60))], pre[: int(self.cfg.get("max_pre", 60))]

    def tick(self) -> None:
        now = time.time()
        if now - self._last_disc > float(self.cfg.get("discover_every_s", 600)):
            self._last_disc = now
            import threading  # discovery can take a minute: don't stall live polling
            threading.Thread(target=self._safe_discover, daemon=True, name="lag-discover").start()
        live, pre = self.select()
        do_pre = now - self._last_pre > float(self.cfg.get("pre_poll_seconds", 30))
        if do_pre:
            self._last_pre = now
        batch = [(p, True) for p in live] + ([(p, False) for p in pre] if do_pre else [])
        self.stats["pairs_watched"] = len(live)
        self.stats["pre_watched"] = len(pre)
        if not batch:
            return
        books = self.pm_books(sorted({p["token"] for p, _ in batch}))
        tickers = sorted({p["ticker"] for p, _ in batch})
        if self.ks:
            self.ks.watch(tickers)
            missing = [t for t in tickers if not self.ks.quote(t)]
        else:
            missing = tickers
        kms = self.k_markets(missing) if missing else {}
        for p, is_live in batch:
            b, m = books.get(p["token"]), kms.get(p["ticker"])
            if b and (m or (self.ks and self.ks.quote(p["ticker"]))):
                self._check(p, is_live, b, m, now)
        self.prune_trades(now)
        if now - getattr(self, "_last_shadow", 0) > 60:
            self._last_shadow = now
            try:
                self.settle_shadows(now)
            except Exception as e:
                log.warning("lag shadow check failed: %s", e)

    def _k_quote(self, p: dict, m: dict | None) -> tuple:
        """Kalshi ask/bid for this side: live stream first, market listing as fallback."""
        q = self.ks.quote(p["ticker"]) if getattr(self, "ks", None) else None
        if q and q.get("yes_ask") is not None and q.get("yes_bid") is not None:
            ya, yb = q["yes_ask"], q["yes_bid"]
            return (ya, yb) if p["side"] == "yes" else (1 - yb, 1 - ya)
        if m:
            return _px(m, f'{p["side"]}_ask'), _px(m, f'{p["side"]}_bid')
        return None, None

    def _check(self, p: dict, is_live: bool, book: tuple, m: dict | None, now: float) -> None:
        rate = getattr(self.bot, "fee_rate", 0.07)
        margin = float(self.cfg.get("min_gap", 0.03))
        bid, ask = book
        if bid is None or ask is None or ask - bid > 0.04:
            return
        pm_mid = (bid + ask) / 2
        k_ask, k_bid = self._k_quote(p, m)
        if not k_ask or k_ask <= 0.05 or k_ask > float(self.cfg.get("max_price", 0.92)):
            return  # near-certain or near-dead: no room, and end-of-game prices jump
        gap = pm_mid - k_ask - self._fee(k_ask, rate)
        k_mid = (k_ask + k_bid) / 2 if k_bid else k_ask
        if gap > float(self.cfg.get("max_gap", 0.15)) or abs(pm_mid - k_mid) > 0.20:
            # far too big to be lag: the two markets aren't the same bet (or one is stale/settling)
            p["bad"] = p.get("bad", 0) + 1
            if p["bad"] == 1:
                log.info("LAGWATCH ignoring %s %s: Kalshi %.2f vs Polymarket %.3f looks like a different bet",
                         p["ticker"], p["side"].upper(), k_ask, pm_mid)
            self.open.pop(f'{p["ticker"]}|{p["side"]}', None)
            return
        self.check_external(p, self.group_of(p, is_live), pm_mid, k_ask, k_bid, now, rate, margin,
                            window=60 if is_live else 600)

    def check_external(self, p: dict, grp: str, fair: float, k_ask: float, k_bid: float | None, now: float,
                       rate: float, margin: float, window: float = 60) -> None:
        """Shared gap logic (sports and crypto): fair = what the bet is worth (Polymarket mid, or the
        crypto model). Confirms on the order book, records evidence, trades if the group is on."""
        with self.lock:
            key = f'{p["ticker"]}|{p["side"]}'
            gap = fair - k_ask - self._fee(k_ask, rate)
            g = self.open.get(key)
            if gap >= margin and not g:
                try:
                    ra, rq = self.real_ask(p["ticker"], p["side"])
                except Exception:
                    ra, rq = None, 0
                real_gap = (fair - ra - self._fee(ra, rate)) if ra else -1
                if real_gap < margin or rq < 1:
                    self.stats["phantom"] = self.stats.get("phantom", 0) + 1
                    self._g(grp)["phantom"] = self._g(grp).get("phantom", 0) + 1
                    return  # quote showed a gap the order book doesn't have
                k_ask, gap = ra, real_gap
            if gap >= margin:
                if not g:
                    self.open[key] = {"key": key, "title": p["title"], "ticker": p["ticker"], "side": p["side"],
                                      "t0": now, "entry": k_ask, "pm_mid": round(fair, 3), "max_gap": gap,
                                      "group": grp}
                    log.info("LAGWATCH [%s] gap %s %s: Kalshi %.2f vs fair %.3f (%.1fc)",
                             grp, p["ticker"], p["side"].upper(), k_ask, fair, 100 * gap)
                    if self.trading(grp):
                        if p.get("close") and self.cfg.get("crypto_follow"):
                            self._follow(p, fair, grp)  # bet WITH Kalshi: buy the side Kalshi favors
                        else:
                            self._buy(p, k_ask, fair, grp)
                else:
                    g["max_gap"] = max(g["max_gap"], gap)
            elif g:
                g["secs"] = round(now - g["t0"])
                self.follow.append(g)
                del self.open[key]
            for f in [f for f in self.follow if f["key"] == key]:
                if k_bid and self._net(f["entry"], k_bid, rate) >= 0.01:
                    f["converged"] = True
                if now - f["t0"] > window or f.get("converged"):
                    self._record(f)
                    self.follow.remove(f)
            if k_bid:
                self._maybe_sell(key, k_bid, rate, fair)
                self.shadow_bid(key, k_bid, now)

    @staticmethod
    def _fee(p: float, rate: float) -> float:
        return rate * p * (1 - p)

    def _net(self, entry: float, bid: float, rate: float) -> float:
        return bid - entry - self._fee(entry, rate) - self._fee(bid, rate)

    # ---- trading ------------------------------------------------------------- #
    def _buy(self, p: dict, ask: float, pm_mid: float, grp: str) -> None:
        from copybot import Target
        key = f'{p["ticker"]}|{p["side"]}'
        if any(x["key"] == key for x in self.stats["trades"]):
            return
        if p.get("close"):  # crypto windows
            ent = self.stats.setdefault("entries", {})
            for k in [k for k, v in ent.items() if v.get("close", 0) < time.time() - 60]:
                ent.pop(k, None)
            if ent.get(p["ticker"], {}).get("n", 0) >= int(self.cfg.get("max_entries_per_window", 2)):
                return  # already traded this window enough: no chasing a falling price
            if p["close"] - time.time() <= float(self.cfg.get("no_entry_last_s", 150)):
                return  # inside the late-exit zone: a buy here would just be sold again at a loss
            lo = float(self.cfg.get("crypto_min_price", 0.10))
            if not (lo <= ask <= 1 - lo):
                return  # the real order-book price is a longshot even if the quote wasn't
            if any(x["ticker"] == p["ticker"] and x["side"] != p["side"] for x in self.stats["trades"]):
                return  # already holding the other side of this window: don't bet both ways
        bot = self.bot
        eq = bot.equity()
        pct, max_open, why = self.sizing(eq)
        if why:
            return
        open_cost = sum(x["count"] * x["entry"] for x in self.stats["trades"])
        stake = min(eq * pct, eq * max_open - open_cost,
                    bot.cash() - (0.0 if self.cfg.get("borrow_pot", True) else bot.pot_cash()) - 0.5
                    - (float(self.cfg.get("mm_keep_usd", 10)) if getattr(bot, "mm", None) and bot.mm.cfg.get("enabled") else 0.0))
        # lag trades may borrow the future pot's idle cash: it comes back within 15 minutes
        n = int(stake // ask) if stake > 0 else 0
        if n < 1:
            return
        if bot.mode == "live" and p.get("close") and self.cfg.get("maker_entry", True):
            # crypto windows: rest a limit order one tick under the ask (maker: no fee) instead of paying the ask
            if any(q["key"] == key for q in self.stats.get("pending", [])):
                return
            px = round(ask - 0.01, 2)
            try:
                o = bot.kalshi.create_order(p["ticker"], p["side"], "buy", n, px, None, post_only=True,
                                            expiration_ts=int(time.time() + float(self.cfg.get("maker_wait_s", 30)) + 5))
            except Exception as e:
                log.info("LAGTRADE maker order failed %s: %s", p["ticker"], str(e)[:120])
                return
            if o.get("order_id"):
                e = self.stats.setdefault("entries", {}).setdefault(p["ticker"], {"n": 0, "close": p["close"]})
                e["n"] += 1
                bot._commit(n * px)
                self.stats.setdefault("pending", []).append({"id": o["order_id"], "key": key, "p": p, "n": n, "px": px,
                                                             "filled": 0, "t0": time.time(), "grp": grp, "pm_mid": pm_mid})
                log.info("LAGTRADE [%s] MAKER BID %d %s %s @ %.2f (ask %.2f, model %.2f)", grp, n, p["ticker"],
                         p["side"].upper(), px, ask, pm_mid)
            return
        if bot.mode == "live":
            # take whatever is available at the gap price (thin books: all-or-nothing orders fail)
            try:
                o = bot.kalshi.create_order(p["ticker"], p["side"], "buy", n, round(ask + 0.01, 2), "immediate_or_cancel")
                filled = int(float(o.get("fill_count") or 0))
            except Exception as e:
                log.info("LAGTRADE buy failed %s: %s", p["ticker"], str(e)[:120])
                return
            cost = filled * (float(o.get("average_fill_price") or 0) or ask)
            if hasattr(bot, "_commit"):
                bot._commit(cost)
            g = self._g(grp)
            g["attempts"] = g.get("attempts", 0) + 1
            g["fills"] = g.get("fills", 0) + (1 if filled else 0)
            if g["attempts"] >= 8 and g["fills"] / g["attempts"] < 0.25:  # gaps not really buyable: stop
                g.update(mode="watch", gaps=0, converged=0, games=[], attempts=0, fills=0)
                log.warning("LAGWATCH [%s] back to WATCH: buys rarely fill at the gap price", grp)
        else:
            filled, cost = bot._execute(Target(p["ticker"], p["side"]), "buy", n, ask)
        if not filled:
            log.info("LAGTRADE [%s] no contracts left at %.2f on %s", grp, ask, p["ticker"])
            return
        px = cost / filled
        bot.ledger_queue.append(("open", f"Lag trade · {grp}", p["ticker"], p["side"], filled, px, p["title"]))
        self.stats["trades"].append({"key": key, "ticker": p["ticker"], "side": p["side"], "count": filled,
                                     "entry": px, "t0": time.time(), "pm_mid": pm_mid, "title": p["title"],
                                     "group": grp, "close": p.get("close"), "cond": p.get("cond")})
        log.info("LAGTRADE [%s] BUY %d %s %s @ %.2f (Polymarket %.3f)", grp, filled, p["ticker"],
                 p["side"].upper(), px, pm_mid)

    def _follow(self, p: dict, fair: float, grp: str) -> None:
        """Follow-Kalshi test (real orders, small size): when the model says Kalshi underprices a side,
        buy the OTHER side (the one Kalshi favors) at the real ask, once per window, and hold it to
        settlement. Study 10/22: the side the model liked won 20% vs Kalshi's 28%."""
        if not self.cfg.get("crypto_follow") or self.bot.mode != "live" or "15M-" not in p["ticker"]:
            return  # 15-minute windows only (what the study covered)
        opp = "no" if p["side"] == "yes" else "yes"
        key = f'{p["ticker"]}|{opp}'
        with self.lock:
            if any(x["ticker"] == p["ticker"] for x in self.stats["trades"]):
                return  # one follow trade per window
            ent = self.stats.setdefault("entries", {})
            for k in [k for k, v in ent.items() if v.get("close", 0) < time.time() - 60]:
                ent.pop(k, None)
            if ent.get(p["ticker"], {}).get("n", 0) >= 1:
                return
            if p["close"] - time.time() <= float(self.cfg.get("no_entry_last_s", 150)):
                return
        try:
            ask, qty = self.real_ask(p["ticker"], opp)
        except Exception:
            return
        lo, hi = float(self.cfg.get("follow_min_price", 0.55)), float(self.cfg.get("follow_max_price", 0.85))
        if ask is None or not lo <= ask <= hi or qty < 1:
            return
        pct, max_open, why = self.sizing(self.bot.equity())
        if why:
            return
        n = int(self.cfg.get("follow_contracts", 2))
        if self.bot.cash() < n * ask + 1:
            return
        self.stats.setdefault("entries", {}).setdefault(p["ticker"], {"n": 0, "close": p["close"]})["n"] += 1
        try:  # real taker order at the real ask (what live execution actually gets)
            o = self.bot.kalshi.create_order(p["ticker"], opp, "buy", n, round(ask, 2), "immediate_or_cancel")
            filled = int(float(o.get("fill_count") or 0))
        except Exception as e:
            log.info("LAGFOLLOW buy failed %s: %s", p["ticker"], str(e)[:120])
            return
        if not filled:
            log.info("LAGFOLLOW [%s] no fill at %.2f on %s %s", grp, ask, p["ticker"], opp.upper())
            return
        px = float(o.get("average_fill_price") or 0) or ask
        self.bot._commit(filled * px)
        self.bot.ledger_queue.append(("open", f"Lag follow · {grp}", p["ticker"], opp, filled, px, p["title"]))
        with self.lock:
            self.stats["trades"].append({"key": key, "ticker": p["ticker"], "side": opp, "count": filled, "entry": px,
                                         "t0": time.time(), "pm_mid": round(1 - fair, 3), "title": p["title"],
                                         "group": grp, "close": p.get("close"), "cond": p.get("cond"), "follow": True})
        log.info("LAGFOLLOW [%s] BUY %d %s %s @ %.2f (Kalshi favors it; model said %.2f) - holding to settlement",
                 grp, filled, p["ticker"], opp.upper(), px, 1 - fair)

    # ---- sizing that follows results --------------------------------------------- #
    def day_pnl(self, day: str) -> tuple[float, int]:
        from copybot import _local_day
        from datetime import datetime, timezone
        pnl, n = 0.0, 0
        for c in self.bot.state.data.get("closed", []):
            if str(c.get("whale", "")).startswith("Lag trade"):
                d = _local_day(datetime.fromtimestamp(c.get("closed", 0), timezone.utc).isoformat())
                if d == day:
                    pnl += c.get("pnl", 0)
                    n += 1
        return pnl, n

    def sizing(self, eq: float) -> tuple[float, float, str | None]:
        """Stake per trade steps up after profitable days and down after losing days.
        Lag buying stops for the day after losing lag_daily_stop of the balance."""
        from copybot import _today
        from datetime import datetime, timedelta
        levels = [float(x) for x in (self.cfg.get("size_levels") or [0.05, 0.065, 0.08])]
        st = self.stats
        today = _today()
        if st.get("size_day") != today:
            if st.get("size_day"):
                pnl, n = self.day_pnl(st["size_day"])
                lvl = int(st.get("size_level", 0))
                if pnl > 0 and n >= int(self.cfg.get("size_up_min_trades", 10)):
                    lvl += 1
                elif pnl < 0:
                    lvl -= 1
                st["size_level"] = max(0, min(len(levels) - 1, lvl))
                log.warning("LAGWATCH sizing: %s lag P&L $%.2f over %d trades -> %.1f%% per trade",
                            st["size_day"], pnl, n, 100 * levels[st["size_level"]])
            st["size_day"] = today
        pct = levels[max(0, min(len(levels) - 1, int(st.get("size_level", 0))))]
        max_open = pct * float(self.cfg.get("max_open_trades", 3))
        truth = None
        if hasattr(self.bot, "truth_since_start") and time.time() - st.get("truth_t", 0) > 300:
            st["truth_t"] = time.time()  # at most every 5 min (uses the bot's 5-min Kalshi cache)
            st["truth_v"] = self.bot.truth_since_start("lag", ("BTC", "ETH", "SOL", "XRP", "DOGE"))
        truth = st.get("truth_v")
        if truth is not None and truth < -float(self.cfg.get("kill_usd", 10)):
            if st.get("truth_stopped_day") != today:
                st["truth_stopped_day"] = today
                log.warning("LAG OFF for today: Kalshi says crypto lag is %+.2f after fees", truth)
            return pct, max_open, "daily stop (Kalshi)"
        pnl_today, _ = self.day_pnl(today)
        rid = str(self.cfg.get("stop_reset", ""))
        b = st.get("stop_base") or {}
        if rid and (b.get("rid") != rid or b.get("day") != today):  # manual reset: count today's stop from now
            b = st["stop_base"] = {"rid": rid, "day": today, "pnl": pnl_today}
            log.warning("LAGWATCH daily stop reset: counting from now (today so far $%.2f)", pnl_today)
        if b.get("day") == today and b.get("rid") == rid:
            pnl_today -= float(b.get("pnl", 0))
        if pnl_today < -eq * float(self.cfg.get("daily_stop_pct", 0.05)):
            if st.get("stopped_day") != today:
                st["stopped_day"] = today
                log.warning("LAGWATCH daily loss stop: lag trades down $%.2f today, paused until tomorrow", -pnl_today)
            return pct, max_open, "daily stop"
        return pct, max_open, None

    # ---- exits ------------------------------------------------------------------ #
    def _flip_exit(self, x: dict, fair: float, bid: float, rate: float, now: float) -> bool:
        """Convergence flip: keep a resting sell (maker, no fee) just under the model value, so when Kalshi
        catches up someone buys from us. Repriced as the value moves; never below entry + flip_min.
        True = the trade is fully sold."""
        if not self.cfg.get("flip_exit", True) or self.bot.mode != "live":
            return False
        k = self.bot.kalshi
        eo = x.get("exit_order")
        if eo and now - eo.get("polled", 0) >= 2:
            eo["polled"] = now
            try:
                r = k.get_order(eo["id"])
                f = int(float(r.get("fill_count_fp") or r.get("fill_count") or 0))
                new = f - eo["filled"]
                if new > 0:
                    eo["filled"] = f
                    self.bot.ledger_queue.append(("close", f"Lag trade · {x.get('group', '')}", x["ticker"], x["side"],
                                                  new, eo["px"], x["title"]))
                    log.info("LAGTRADE FLIP %d %s %s @ %.2f (bought %.2f, %+.1fc each, no fees)", new, x["ticker"],
                             x["side"].upper(), eo["px"], x["entry"], 100 * (eo["px"] - x["entry"]))
                    self._add_shadow(x, eo["px"], new)
                    x["count"] -= new
                    if x["count"] <= 0:
                        self._log_path(x, "flipped")
                        if x in self.stats["trades"]:
                            self.stats["trades"].remove(x)
                        return True
                if str(r.get("status") or "").lower() in ("canceled", "cancelled", "expired", "executed"):
                    x.pop("exit_order", None)
                    eo = None
            except Exception as e:
                log.info("LAGTRADE exit poll %s: %s", eo.get("id"), str(e)[:100])
        if now < x.get("exit_wait", 0):
            return False
        lo = x["entry"] + float(self.cfg.get("flip_min", 0.02))
        target = round(max(lo, round(fair - 0.01, 2)), 2)
        if target <= bid or target >= 0.99:
            return False  # would cross (the bid already pays it: the normal take-profit sells) or nothing to gain
        if eo and (abs(eo["px"] - target) < 0.02 or now - eo["t"] < 5):
            return False
        if eo:
            try:
                k.cancel_order(eo["id"], x["ticker"])
            except Exception as e:
                log.info("LAGTRADE exit cancel %s: %s", eo["id"], str(e)[:100])
                return False
            x.pop("exit_order", None)
        try:
            o = k.create_order(x["ticker"], x["side"], "sell", int(x["count"]), target, None, post_only=True,
                               expiration_ts=int(float(x["close"]) - 60), resting_sell=True)
            if o.get("order_id"):
                x["exit_order"] = {"id": o["order_id"], "px": target, "filled": 0, "t": now, "polled": now}
                log.info("LAGTRADE EXIT POSTED %d %s %s @ %.2f (bought %.2f, model %.2f)", x["count"], x["ticker"],
                         x["side"].upper(), target, x["entry"], fair)
        except Exception as e:
            x["exit_wait"] = now + 60  # don't retry every tick (e.g. Kalshi wants cash to rest a sell)
            log.info("LAGTRADE exit post failed %s: %s", x["ticker"], str(e)[:120])
        return False

    def _drop_exit(self, x: dict) -> None:
        eo = x.pop("exit_order", None)
        if not eo:
            return
        try:
            self.bot.kalshi.cancel_order(eo["id"], x["ticker"])
            r = self.bot.kalshi.get_order(eo["id"])
            f = int(float(r.get("fill_count_fp") or r.get("fill_count") or 0))
            if f > eo["filled"]:  # filled just before the cancel
                new = f - eo["filled"]
                self.bot.ledger_queue.append(("close", f"Lag trade · {x.get('group', '')}", x["ticker"], x["side"],
                                              new, eo["px"], x["title"]))
                x["count"] -= new
        except Exception as e:
            log.info("LAGTRADE exit drop %s: %s", eo.get("id"), str(e)[:100])

    def _sell(self, x: dict, bid: float, rate: float, why: str) -> None:
        from copybot import Target
        self._drop_exit(x)  # pull the resting flip exit first so we don't sell twice
        if x["count"] <= 0:
            if x in self.stats["trades"]:
                self.stats["trades"].remove(x)
            return
        filled, proceeds = self.bot._execute(Target(x["ticker"], x["side"]), "sell", x["count"], bid)
        if filled:
            px = proceeds / filled
            self.bot.ledger_queue.append(("close", f"Lag trade · {x.get('group', '')}", x["ticker"], x["side"],
                                          filled, px, x["title"]))
            log.info("LAGTRADE SELL %d %s %s @ %.2f (bought %.2f, %+.1fc each after fees) %s", filled, x["ticker"],
                     x["side"].upper(), px, x["entry"], 100 * self._net(x["entry"], px, rate), why)
            self._add_shadow(x, px, filled)
            x["count"] -= filled
        if x["count"] <= 0:
            self._log_path(x, "sold")
        if x["count"] <= 0 and x in self.stats["trades"]:
            self.stats["trades"].remove(x)

    def poll_pending(self, ticker: str, fair_yes: float, now: float) -> None:
        """Maker entries: book fills as they come; cancel after maker_wait_s or once the gap has closed."""
        pend = self.stats.get("pending", [])
        for q in [q for q in pend if q["p"]["ticker"] == ticker]:
            if now - q.get("polled", 0) < 2 and now - q["t0"] < float(self.cfg.get("maker_wait_s", 30)):
                continue
            q["polled"] = now
            fair = fair_yes if q["p"]["side"] == "yes" else 1 - fair_yes
            done = now - q["t0"] >= float(self.cfg.get("maker_wait_s", 30)) or \
                fair - q["px"] < float(self.cfg.get("min_gap", 0.03)) / 2 or q["p"]["close"] - now <= 180
            if done:
                try:
                    self.bot.kalshi.cancel_order(q["id"], ticker)
                except Exception as e:
                    log.info("LAGTRADE maker cancel %s: %s", q["id"], str(e)[:100])
            try:
                r = self.bot.kalshi.get_order(q["id"])
            except Exception:
                continue
            f = int(float(r.get("fill_count_fp") or r.get("fill_count") or 0))
            new = f - q["filled"]
            if new > 0:
                q["filled"] = f
                self._book_fill(q, new)
            st = str(r.get("status") or "").lower()
            if done or st in ("canceled", "cancelled", "executed", "expired") or f >= q["n"]:
                pend.remove(q)

    def _book_fill(self, q: dict, n: int) -> None:
        p, grp, key = q["p"], q["grp"], q["key"]
        self.bot.ledger_queue.append(("open", f"Lag trade · {grp}", p["ticker"], p["side"], n, q["px"], p["title"]))
        with self.lock:
            x = next((x for x in self.stats["trades"] if x["key"] == key), None)
            if x:
                x["entry"] = (x["entry"] * x["count"] + q["px"] * n) / (x["count"] + n)
                x["count"] += n
            else:
                self.stats["trades"].append({"key": key, "ticker": p["ticker"], "side": p["side"], "count": n,
                                             "entry": q["px"], "t0": time.time(), "pm_mid": q["pm_mid"],
                                             "title": p["title"], "group": grp, "close": p.get("close"),
                                             "cond": p.get("cond"), "maker": True})
        log.info("LAGTRADE [%s] MAKER FILL %d %s %s @ %.2f", grp, n, p["ticker"], p["side"].upper(), q["px"])

    def protect(self, ticker: str, fair_yes: float, yes_bid: float, yes_ask: float, rate: float, now: float) -> None:
        """Every tick on crypto windows: track each trade's best bid and price path, and sell when the
        bid falls through its profit-ladder floor -- unless the model still values it well above the
        floor (a dip in the book, not a reversal)."""
        if not self.cfg.get("profit_ladder", True):
            return
        with self.lock:
            for x in [x for x in self.stats["trades"] if x["ticker"] == ticker and x.get("close") and not x.get("follow")]:
                fair = fair_yes if x["side"] == "yes" else 1 - fair_yes
                bid = yes_bid if x["side"] == "yes" else 1 - yes_ask
                if bid is None or bid < 0.02:
                    continue
                x["peak"] = max(x.get("peak", 0.0), bid)
                if self._flip_exit(x, fair, bid, rate, now):
                    continue  # fully sold by the resting exit
                path = x.setdefault("path", [])
                if not path or now - path[-1][0] >= 5:
                    path.append((int(now), round(bid, 3), round(fair, 3)))
                    del path[:-200]
                left = float(x["close"]) - now
                cut = float(self.cfg.get("stop_c", 0.10))
                if left >= 60 and fair <= x["entry"] - cut and bid >= float(self.cfg.get("stop_min_bid_share", 0.5)) * fair:
                    self._sell(x, bid, rate, f"(stop: worth {fair:.2f}, {100 * cut:.0f}c+ under cost {x['entry']:.2f})")
                    continue
                if left <= float(self.cfg.get("late_cut_s", 300)) and fair < x["entry"] and bid >= x["entry"] - cut:
                    self._sell(x, bid, rate, f"(late cut: worth {fair:.2f} < cost {x['entry']:.2f}, small loss)")
                    continue
                floor = profit_floor(x["entry"], x["peak"], rate)
                if x["peak"] - x["entry"] < float(self.cfg.get("ladder_min_gain_c", 0.05)):
                    floor = None  # not up enough in cents yet: let stops/take-profit handle it
                if floor is None or bid > floor:
                    continue
                if fair > floor + float(self.cfg.get("ladder_model_margin", 0.08)):
                    continue  # model still says it's worth clearly more: don't get shaken out
                self._sell(x, bid, rate, f"(profit ladder: peak {x['peak']:.2f}, floor {floor:.2f}, worth {fair:.2f})")

    def exit_late(self, ticker: str, fair_yes: float, yes_bid: float, yes_ask: float, rate: float,
                  exit_below: float) -> None:
        """Last minutes of a crypto window: sell a trade that's probably losing (our model now gives
        it under exit_below) or when the market pays more than it's worth, instead of riding it to 0."""
        with self.lock:
            for x in [x for x in self.stats["trades"] if x["ticker"] == ticker and not x.get("follow")]:
                fair = fair_yes if x["side"] == "yes" else 1 - fair_yes
                bid = yes_bid if x["side"] == "yes" else 1 - yes_ask
                if bid < 0.02:
                    continue
                floor = float(self.cfg.get("exit_min_bid_share", 0.75))
                if bid >= fair or (fair < exit_below and bid >= floor * fair):  # never dump far below value
                    self._sell(x, bid, rate, f"(late exit: worth {fair:.2f}, bid {bid:.2f})")

    def _maybe_sell(self, key: str, bid: float, rate: float, fair: float | None = None) -> None:
        from copybot import Target
        for x in [x for x in self.stats["trades"] if x["key"] == key and not x.get("follow")]:
            if self._net(x["entry"], bid, rate) < float(self.cfg.get("min_profit", 0.01)):
                continue
            # crypto windows: hold to settlement (the full gap, one fee) unless the market already pays
            # at least what the trade is worth
            if x.get("close") and self.cfg.get("hold_to_settle", True) and fair is not None and bid < fair:
                x.setdefault("flip_px", bid)  # what the old quick flip would have sold at
                continue
            self._drop_exit(x)
            if x["count"] <= 0:
                continue
            filled, proceeds = self.bot._execute(Target(x["ticker"], x["side"]), "sell", x["count"], bid)
            if filled:
                px = proceeds / filled
                self.bot.ledger_queue.append(("close", f"Lag trade · {x.get('group', '')}", x["ticker"], x["side"],
                                              filled, px, x["title"]))
                log.info("LAGTRADE SELL %d %s %s @ %.2f (bought %.2f, +%.1fc each after fees)", filled, x["ticker"],
                         x["side"].upper(), px, x["entry"], 100 * self._net(x["entry"], px, rate))
                self._add_shadow(x, px, filled)
                x["count"] -= filled
            if x["count"] <= 0:
                self.stats["trades"].remove(x)

    # ---- "what if we had held?" records ------------------------------------------ #
    def _add_shadow(self, x: dict, sold: float, n: int) -> None:
        if not x.get("close"):
            return  # only crypto windows (known close time) are tracked
        sh = self.stats.setdefault("shadow", [])
        sh.append({"key": x["key"], "ticker": x["ticker"], "side": x["side"], "group": x.get("group"),
                   "entry": x["entry"], "sold": sold, "count": n, "close": x["close"], "t_sell": int(time.time()),
                   "max_bid": sold, "bid_late": None, "cond": x.get("cond")})
        del sh[:-300]

    def paper_stop(self, ticker: str, fair_yes: float, yes_bid: float, yes_ask: float, secs_left: float) -> None:
        """Model stop: with 5+ min left, if a trade's model value falls to half its entry (the coin really
        moved against us) and someone pays 75%+ of that value, sell (model_stop true) or just note it."""
        live = self.cfg.get("model_stop", True)
        with self.lock:
            for x in [x for x in self.stats.get("trades", []) if x["ticker"] == ticker and not x.get("follow")]:
                if (x.get("stop_px") is not None and not live) or secs_left < 300:
                    continue
                fair = fair_yes if x["side"] == "yes" else 1 - fair_yes
                bid = yes_bid if x["side"] == "yes" else 1 - yes_ask
                if bid is None or bid < 0.02:
                    continue
                if fair <= float(self.cfg.get("stop_value_share", 0.5)) * x["entry"] and \
                        bid >= float(self.cfg.get("exit_min_bid_share", 0.75)) * fair:
                    x["stop_px"] = round(bid, 4)
                    if live:
                        self._sell(x, bid, getattr(self.bot, "fee_rate", 0.07),
                                   f"(model stop: worth {fair:.2f} vs bought {x['entry']:.2f})")

    def shadow_bid(self, key: str, bid: float, now: float) -> None:
        for s in self.stats.get("shadow", []):  # what a sold trade would have been worth if kept
            if s["key"] == key:
                s["max_bid"] = max(s.get("max_bid", 0), bid)
                if s.get("close") and s["close"] - now <= 120 and s.get("bid_late") is None:
                    s["bid_late"] = bid

    def settle_shadows(self, now: float) -> None:
        """After a window settles: compare what we made (quick flip) with holding to the end, and with
        a 'lock in 2 minutes before close' exit. Logged per trade and kept for the daily report."""
        rate = getattr(self.bot, "fee_rate", 0.07)
        done = self.stats.setdefault("shadow_done", [])
        keep = []
        for s in self.stats.get("shadow", []):
            if now < s["close"] + 180:
                keep.append(s)
                continue
            try:
                res = (self.bot.kdata.market(s["ticker"]).get("result") or "").lower()
            except Exception:
                res = ""
            if res not in ("yes", "no"):
                if now < s["close"] + 3600:
                    keep.append(s)
                continue
            e, n = s["entry"], s["count"]
            flip = n * self._net(e, s["sold"], rate)
            hold = n * ((1.0 if res == s["side"] else 0.0) - e - self._fee(e, rate))
            late = s.get("bid_late")
            lock = n * self._net(e, late, rate) if late is not None else hold
            best = n * self._net(e, s.get("max_bid", s["sold"]), rate)
            stop = n * self._net(e, s["stop_px"], rate) if s.get("stop_px") is not None else hold
            rec = {"held": bool(s.get("held")), "ticker": s["ticker"], "side": s["side"], "group": s.get("group"), "n": n, "entry": e,
                   "sold": s["sold"], "result": res, "flip": round(flip, 2), "hold": round(hold, 2),
                   "lock_2min": round(lock, 2), "best_possible": round(best, 2), "stop_loss": round(stop, 2),
                   "stop_hit": s.get("stop_px") is not None, "day": __import__("copybot")._today(),
                   "actual": round(hold if s.get("held") else flip, 2), "cond": s.get("cond")}
            done.append(rec)
            log.info("LAG_SHADOW %s", json.dumps(rec))
        del done[:-1000]
        self.stats["shadow"] = keep

    def prune_trades(self, now: float) -> None:
        """Forget lag trades whose market has closed (held to settlement): the settlement itself is
        booked by the main ledger. Otherwise they keep counting against the lag money cap."""
        def done(x):
            if x.get("close"):
                return now > float(x["close"]) + 60
            crypto = x["ticker"].startswith(("KXBTC15M", "KXETH15M", "KXBTCD", "KXETHD", "KXSOL15M", "KXXRP15M", "KXDOGE15M"))
            return now - x["t0"] > (75 * 60 if crypto else 18 * 3600)
        with self.lock:
            for x in [x for x in self.stats["trades"] if done(x) and x.get("close")]:
                self._log_path(x, "held")
                sh = self.stats.setdefault("shadow", [])  # held to the end: compare with a quick flip
                sh.append({"key": x["key"], "ticker": x["ticker"], "side": x["side"], "group": x.get("group"),
                           "entry": x["entry"], "sold": x.get("flip_px") or x["entry"], "count": x["count"],
                           "close": x["close"], "t_sell": None, "max_bid": x.get("flip_px") or x["entry"],
                           "bid_late": None, "held": True, "stop_px": x.get("stop_px"), "cond": x.get("cond")})
            self.stats["trades"] = [x for x in self.stats["trades"] if not done(x)]

    def _log_path(self, x: dict, how: str) -> None:
        if x.get("path"):
            log.info("LAG_PATH %s", json.dumps({"ticker": x["ticker"], "side": x["side"], "n": x["count"],
                                                 "entry": x["entry"], "how": how, "path": x["path"]}))

    def _record(self, g: dict) -> None:
        st, grp = self.stats, self._g(g["group"])
        st["gaps"] += 1
        grp["gaps"] += 1
        if g.get("converged"):
            st["converged"] += 1
            grp["converged"] += 1
        game = g["ticker"].rsplit("-", 1)[0]
        grp["games"] = list(set(grp.get("games", [])) | {game})[-100:]
        ex = {"group": g["group"], "title": g["title"][:60], "ticker": g["ticker"], "side": g["side"],
              "entry": g["entry"], "pm_mid": g["pm_mid"], "max_gap_c": round(100 * g["max_gap"], 1),
              "secs": g.get("secs"), "converged": bool(g.get("converged")), "at": int(g["t0"])}
        st["examples"] = (st["examples"] + [ex])[-25:]
        log.info("LAGWATCH %s", json.dumps(ex))
        self._maybe_switch(g["group"])

    def _maybe_switch(self, name: str) -> None:
        g, c = self._g(name), self.cfg
        if g.get("mode") != "watch" or not c.get("auto_switch", True):
            return
        n, conv, games = g["gaps"], g["converged"], len(g.get("games", []))
        crypto = name.split(" ")[0] in ("BTC", "ETH", "SOL", "XRP", "DOGE")  # model-priced: needs far more evidence
        need_gaps = int(c.get("crypto_min_gaps", 30) if crypto else c.get("min_gaps", 8))
        need_games = int(c.get("crypto_min_games", 8) if crypto else c.get("min_games", 3))
        if (n >= need_gaps and conv / max(1, n) >= float(c.get("min_converge_rate", 0.6))
                and games >= need_games):
            g["mode"] = "trade"
            g["switched_at"] = int(time.time())
            self.stats["mode"] = "trade"  # at least one group trading (dashboard + reserve)
            log.warning("LAGWATCH [%s] switched to TRADE: %d gaps, %d profitable after fees (%.0f%%), %d games",
                        name, n, conv, 100 * conv / n, games)

    def run(self, stop) -> None:
        every = float(self.cfg.get("poll_seconds", 5))
        while not stop.is_set():
            t0 = time.time()
            try:
                if not self.bot.paused:
                    self.tick()
            except Exception as e:
                log.warning("lagwatch tick failed: %s", e)
            took = time.time() - t0
            if took > 2 * every and time.time() - getattr(self, "_slow_log", 0) > 600:
                self._slow_log = time.time()
                log.warning("LAGWATCH slow round: %.1fs for %d live + %d upcoming markets", took,
                            self.stats.get("pairs_watched", 0), self.stats.get("pre_watched", 0))
            stop.wait(max(0.5, every - took))
