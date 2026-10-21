"""
Player-prop model: how likely is a Kalshi player prop ("Jalen Brunson: 25+ points") to hit?

For each open NBA / NFL prop on Kalshi it pulls the player's recent game logs from ESPN and
counts how often he cleared the line, weighting recent games more. That hit rate is pulled a
little toward Kalshi's own price (the market knows about injuries, matchups and minutes that
a box-score count can't see), so the bot only calls a prop "value" when the history clearly
beats the price.

Prop rating (0-100):
  hit chance   0-60   model chance 50% -> 0, 95% -> 60
  price edge   0-30   model chance minus Kalshi ask, +0 -> 0, +10c -> 30
  sample size  0-10   10 points at 20+ games (NBA) / 12+ games (NFL)
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("props")
ESPN = "https://site.web.api.espn.com/apis"
UA = {"User-Agent": "Mozilla/5.0 pm-kalshi-copybot/1.0"}

# Kalshi series -> (league, how to read the stat from an ESPN game-log row)
SERIES = {
    "KXNBAPTS": ("nba", ["points"], "points"),
    "KXNBAREB": ("nba", ["totalRebounds"], "rebounds"),
    "KXNBAAST": ("nba", ["assists"], "assists"),
    "KXNBA3PT": ("nba", ["threePointFieldGoalsMade-threePointFieldGoalsAttempted"], "threes"),
    "KXNBAPRA": ("nba", ["points", "totalRebounds", "assists"], "pts+reb+ast"),
    "KXNFLPASSYDS": ("nfl", ["passingYards"], "passing yds"),
    "KXNFLPASSTDS": ("nfl", ["passingTouchdowns"], "passing TDs"),
    "KXNFLRSHYDS": ("nfl", ["rushingYards"], "rushing yds"),
    "KXNFLRECYDS": ("nfl", ["receivingYards"], "receiving yds"),
    "KXNFLREC": ("nfl", ["receptions"], "receptions"),
}
SPORT = {"nba": "basketball/nba", "nfl": "football/nfl"}
GAME_SERIES = {"nba": "KXNBAGAME", "nfl": "KXNFLGAME"}
SAMPLE = {"nba": 20, "nfl": 12}          # games used
MAX_IDLE_DAYS = {"nba": 12, "nfl": 16}   # longer gap = injured / out of rotation: skip
DECAY = 0.92                             # weight of each older game
MARKET_WEIGHT = 0.35                     # pull toward Kalshi's mid price


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _num(v) -> float | None:
    if v in (None, "", "-", "--"):
        return None
    s = str(v).split("-")[0].replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _px(m: dict, f: str) -> float | None:
    v = m.get(f"{f}_dollars")
    if v not in (None, ""):
        return float(v)
    v = m.get(f)
    return float(v) / 100 if v not in (None, 0) else None


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


class PropModel:
    def __init__(self, kalshi_get, kalshi_start, fee_rate: float = 0.07):
        """kalshi_get(path, **params) -> json (public market data); kalshi_start(market) -> ts|None."""
        self.kget = kalshi_get
        self.kstart = kalshi_start
        self.fee_rate = fee_rate
        self.s = requests.Session()
        self.s.headers.update(UA)
        self._ids: dict[tuple, str | None] = {}
        self._logs: dict[tuple, tuple[float, list]] = {}
        self._starts: dict[str, float | None] = {}
        self.skips: dict[str, int] = {}

    def _k(self, path: str, **params) -> dict:
        """Kalshi public GET with backoff on 429 (rate limit)."""
        for i in range(5):
            try:
                out = self.kget(path, **params)
                time.sleep(0.12)  # stay under Kalshi's public rate limit
                return out
            except Exception as e:
                if "429" not in str(e) or i == 4:
                    raise
                time.sleep(1.5 * (i + 1))
        return {}

    def _skip(self, why: str) -> None:
        self.skips[why] = self.skips.get(why, 0) + 1

    # ---- ESPN ------------------------------------------------------------ #
    def _espn(self, path: str, **params) -> dict:
        for i in range(3):
            r = self.s.get(f"{ESPN}{path}", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def athlete_id(self, league: str, name: str) -> str | None:
        key = (league, _norm(name))
        if key in self._ids:
            return self._ids[key]
        aid = None
        try:
            j = self._espn("/common/v3/search", query=name, limit=5, type="player")
            for it in j.get("items") or []:
                if it.get("league") == league and _norm(it.get("displayName", "")) == key[1]:
                    aid = it.get("id")
                    break
            if not aid:
                for it in j.get("items") or []:
                    if it.get("league") == league:
                        aid = it.get("id")
                        break
        except Exception as e:
            log.debug("espn search %s: %s", name, e)
        self._ids[key] = aid
        return aid

    def game_log(self, league: str, aid: str) -> list[dict]:
        """Newest-first list of {date, stats{name: value}} over this season and last season."""
        key = (league, aid)
        hit = self._logs.get(key)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        rows: dict[str, dict] = {}
        now = datetime.now(timezone.utc)
        cur = now.year + 1 if (league == "nba" and now.month >= 9) else now.year
        if league == "nfl" and now.month < 3:
            cur = now.year - 1
        for season in (cur, cur - 1):
            try:
                g = self._espn(f"/common/v3/sports/{SPORT[league]}/athletes/{aid}/gamelog", season=season)
            except Exception:
                continue
            names = g.get("names") or []
            evmeta = g.get("events") or {}
            for st in g.get("seasonTypes") or []:
                if "pre" in (st.get("displayName") or "").lower():
                    continue
                for cat in st.get("categories") or []:
                    for ev in cat.get("events") or []:
                        eid = ev.get("eventId")
                        if not eid or eid in rows:
                            continue
                        meta = evmeta.get(eid) or {}
                        rows[eid] = {"date": meta.get("gameDate") or "",
                                     "stats": dict(zip(names, ev.get("stats") or []))}
        out = sorted(rows.values(), key=lambda r: r["date"], reverse=True)
        self._logs[key] = (time.time(), out)
        return out

    # ---- model ------------------------------------------------------------ #
    @staticmethod
    def stat_value(row: dict, fields: list[str]) -> float | None:
        tot = 0.0
        for f in fields:
            v = _num(row["stats"].get(f))
            if v is None:
                return None
            tot += v
        return tot

    def hit_rate(self, league: str, rows: list[dict], fields: list[str], line: float) -> dict | None:
        vals = [v for v in (self.stat_value(r, fields) for r in rows[:SAMPLE[league]]) if v is not None]
        if len(vals) < 6:
            return None
        w = [DECAY ** i for i in range(len(vals))]
        hits = sum(wi for wi, v in zip(w, vals) if v > line)
        p = (hits + 1) / (sum(w) + 2)  # weighted, smoothed hit rate
        last10 = vals[:10]
        return {"p_hist": p, "n": len(vals), "avg": sum(last10) / len(last10),
                "last10_hits": sum(1 for v in last10 if v > line), "last10_n": len(last10),
                "recent": vals[:5]}

    def start_time(self, league: str, prop_event: str) -> float | None:
        suffix = prop_event.split("-", 1)[1] if "-" in prop_event else ""
        if suffix in self._starts:
            return self._starts[suffix]
        st = None
        try:
            ms = self._k("/markets", event_ticker=f"{GAME_SERIES[league]}-{suffix}", limit=2).get("markets") or []
            if ms:
                st = self.kstart(ms[0])
        except Exception:
            return None  # don't cache failures
        self._starts[suffix] = st
        return st

    # ---- scan ------------------------------------------------------------- #
    def _markets(self, series: str) -> list[dict]:
        out, cursor = [], None
        for _ in range(15):
            p = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            j = self._k("/markets", **p)
            out += j.get("markets") or []
            cursor = j.get("cursor")
            if not cursor:
                break
        return out

    def scan(self, *, min_ask: float = 0.50, max_ask: float = 0.93, min_p: float = 0.62,
             hours_ahead: float = 36, nfl_hours_ahead: float = 110, min_before_s: int = 900) -> list[dict]:
        picks: list[dict] = []
        now = time.time()
        self.skips = {}
        for series, (league, fields, label) in SERIES.items():
            try:
                ms = self._markets(series)
            except Exception as e:
                log.warning("props: %s list failed: %s", series, e)
                continue
            horizon = (nfl_hours_ahead if league == "nfl" else hours_ahead) * 3600
            for m in ms:
                ask, bid = _px(m, "yes_ask"), _px(m, "yes_bid")
                line = m.get("floor_strike")
                if ask is None or line is None or not min_ask <= ask <= max_ask:
                    self._skip("price outside range")
                    continue
                if m.get("strike_type") not in (None, "greater"):
                    continue
                name = (m.get("title") or "").split(":")[0].strip()
                if not name:
                    continue
                start = self.start_time(league, m.get("event_ticker", ""))
                if start is None or not (now + min_before_s < start < now + horizon):
                    self._skip("start unknown / too soon / too far")
                    continue
                aid = self.athlete_id(league, name)
                if not aid:
                    self._skip("player not found on ESPN")
                    continue
                rows = self.game_log(league, aid)
                if not rows:
                    self._skip("no game log")
                    continue
                try:
                    last = datetime.fromisoformat(rows[0]["date"].replace("Z", "+00:00")).timestamp()
                except Exception:
                    continue
                if (now - last) / 86400 > MAX_IDLE_DAYS[league]:
                    self._skip("not played lately")
                    continue  # hasn't played lately (injury, benched, offseason)
                hr = self.hit_rate(league, rows, fields, float(line))
                if not hr:
                    self._skip("too few games")
                    continue
                mid = (ask + bid) / 2 if bid and ask - bid <= 0.12 else None
                p = (1 - MARKET_WEIGHT) * hr["p_hist"] + MARKET_WEIGHT * (mid if mid is not None else ask)
                if p < min_p:
                    self._skip("hit chance below min")
                    continue
                fee = self.fee_rate * ask * (1 - ask)
                edge = p - ask
                ev = (p - ask - fee) / ask
                score = round(clamp((p - 0.5) / 0.45 * 60, 0, 60) + clamp(edge / 0.10 * 30, 0, 30)
                              + clamp(hr["n"] / SAMPLE[league] * 10, 0, 10))
                picks.append({
                    "kind": "prop", "league": league.upper(), "ticker": m["ticker"], "side": "yes",
                    "event_ticker": m.get("event_ticker"), "title": m.get("title"), "player": name,
                    "stat": label, "line": float(line), "outcome": f"{name} {int(float(line) + 0.5)}+ {label}",
                    "ask": ask, "bid": bid, "p": round(p, 3), "p_hist": round(hr["p_hist"], 3),
                    "whale_px": round(p, 3), "ev": round(ev, 4), "edge": round(edge, 3), "score": score,
                    "n": hr["n"], "avg": round(hr["avg"], 1), "last10": f'{hr["last10_hits"]}/{hr["last10_n"]}',
                    "recent": hr["recent"], "start": start, "status": "pre",
                })
                time.sleep(0.05)
        # one line per player+stat: keep the best-rated line
        best: dict[tuple, dict] = {}
        for pk in picks:
            k = (pk["event_ticker"], pk["player"], pk["stat"])
            if k not in best or pk["score"] > best[k]["score"]:
                best[k] = pk
        out = sorted(best.values(), key=lambda x: (-x["score"], -x["p"]))
        log.info("props: %d candidate props (%d after one-per-player); skipped %s", len(picks), len(out),
                 sorted(self.skips.items(), key=lambda kv: -kv[1]))
        return out
