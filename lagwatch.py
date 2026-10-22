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
        """Best ask and size for `side` from Kalshi's live order book (the market listing can lag).
        Kalshi's book lists bids only: YES ask = 1 - best NO bid, and the reverse."""
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
                self._maybe_sell(key, k_bid, rate)

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
        bot = self.bot
        eq = bot.equity()
        open_cost = sum(x["count"] * x["entry"] for x in self.stats["trades"])
        stake = min(eq * float(self.cfg.get("stake_pct", 0.03)),
                    eq * float(self.cfg.get("max_open_pct", 0.10)) - open_cost,
                    bot.cash() - bot.pot_cash() - 0.5)  # main + reserve, never the future pot
        n = int(stake // ask) if stake > 0 else 0
        if n < 1:
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
                                     "group": grp, "close": p.get("close")})
        log.info("LAGTRADE [%s] BUY %d %s %s @ %.2f (Polymarket %.3f)", grp, filled, p["ticker"],
                 p["side"].upper(), px, pm_mid)

    def _maybe_sell(self, key: str, bid: float, rate: float) -> None:
        from copybot import Target
        for x in [x for x in self.stats["trades"] if x["key"] == key]:
            if self._net(x["entry"], bid, rate) < float(self.cfg.get("min_profit", 0.01)):
                continue
            filled, proceeds = self.bot._execute(Target(x["ticker"], x["side"]), "sell", x["count"], bid)
            if filled:
                px = proceeds / filled
                self.bot.ledger_queue.append(("close", f"Lag trade · {x.get('group', '')}", x["ticker"], x["side"],
                                              filled, px, x["title"]))
                log.info("LAGTRADE SELL %d %s %s @ %.2f (bought %.2f, +%.1fc each after fees)", filled, x["ticker"],
                         x["side"].upper(), px, x["entry"], 100 * self._net(x["entry"], px, rate))
                x["count"] -= filled
            if x["count"] <= 0:
                self.stats["trades"].remove(x)

    def prune_trades(self, now: float) -> None:
        """Forget lag trades whose market has closed (held to settlement): the settlement itself is
        booked by the main ledger. Otherwise they keep counting against the lag money cap."""
        def done(x):
            if x.get("close"):
                return now > float(x["close"]) + 60
            crypto = x["ticker"].startswith(("KXBTC15M", "KXETH15M", "KXBTCD", "KXETHD"))
            return now - x["t0"] > (75 * 60 if crypto else 18 * 3600)
        with self.lock:
            self.stats["trades"] = [x for x in self.stats["trades"] if not done(x)]

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
        crypto = name.split(" ")[0] in ("BTC", "ETH")  # model-priced: needs far more evidence
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
