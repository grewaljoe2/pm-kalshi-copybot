"""
Automatic Polymarket -> Kalshi matching for daily sports games.

Polymarket trades carry a slug like "nba-lal-bos-2026-10-21" (league, teams, date).
Kalshi lists each game as an event in a league series (e.g. KXNBAGAME) with one
market per side ("Lakers wins"). We match on league + date + team name.

Supported bet types (others are skipped):
  - match winner          "A vs B"                    -> "<A> wins"  YES
  - soccer "Will X win?"  Yes / No                    -> "<X> wins"  YES / NO   (No = draw or loss on both)
  - soccer draw           "... draw?" Yes / No        -> "Tie"       YES / NO
  - esports map N winner  "... Map N Winner"          -> "<A> wins map N" YES
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher

log = logging.getLogger("sports")

# Polymarket slug prefix -> Kalshi game series (winner) and map series
PREFIX_SERIES: dict[str, list[str]] = {
    "nba": ["KXNBAGAME"], "nfl": ["KXNFLGAME"], "nhl": ["KXNHLGAME"], "mlb": ["KXMLBGAME"],
    "wnba": ["KXWNBAGAME"], "cfb": ["KXNCAAFGAME"], "ncaaf": ["KXNCAAFGAME"],
    "cbb": ["KXNCAAMBGAME"], "ncaab": ["KXNCAAMBGAME"], "cwbb": ["KXNCAAWBGAME"],
    "epl": ["KXEPLGAME"], "ucl": ["KXUCLGAME"], "uel": ["KXUELGAME"], "uecl": ["KXUECLGAME"],
    "lal": ["KXLALIGAGAME"], "laliga": ["KXLALIGAGAME"], "sea": ["KXSERIEAGAME"],
    "seriea": ["KXSERIEAGAME"], "bun": ["KXBUNDESLIGAGAME"], "bundesliga": ["KXBUNDESLIGAGAME"],
    "fl1": ["KXLIGUE1GAME"], "ligue1": ["KXLIGUE1GAME"], "mls": ["KXMLSGAME"],
    "ere": ["KXEREDIVISIEGAME"], "mex": ["KXLIGAMXGAME"], "efl": ["KXEFLCHAMPIONSHIPGAME"],
    "elc": ["KXEFLCHAMPIONSHIPGAME"], "por": ["KXLIGAPORTUGALGAME"], "bra": ["KXBRASILEIROGAME"],
    "arg": ["KXARGPREMDIVGAME"], "spl": ["KXSAUDIPLGAME"], "efa": ["KXEFLCUPGAME"],
    "cs2": ["KXCS2GAME"], "lol": ["KXLOLGAME"], "dota2": ["KXDOTA2GAME"], "val": ["KXVALORANTGAME"],
    "atp": ["KXATPMATCH", "KXATPCHALLENGERMATCH"], "wta": ["KXWTAMATCH", "KXWTACHALLENGERMATCH"],
    "ufc": ["KXUFCFIGHT"],
}
MAP_SERIES = {"cs2": "KXCS2MAP", "lol": "KXLOLMAP", "dota2": "KXDOTA2MAP", "val": "KXVALORANTMAP"}

SKIP_WORDS = ("total", "spread", "handicap", "btts", "o/u", "over/under", "both teams", "exact",
              "first", "1h", "2h", "half", "quarter", "corners", "kills", "props", "more-markets")
STOP = {"fc", "cf", "sk", "afc", "sc", "ac", "club", "the", "cd", "ud", "rc", "sv", "fk", "bk",
        "if", "de", "of", "esports", "esport", "team", "gaming", "kv", "osc", "ssc", "as", "us"}
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    toks = [t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP and not re.fullmatch(r"\d{4}", t)]
    return " ".join(toks)


def name_sim(a: str, b: str) -> float:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    sa, sb = re.sub(r"^the", "", na.replace(" ", "")), re.sub(r"^the", "", nb.replace(" ", ""))
    if sa == sb:
        return 1.0
    if len(sa) >= 4 and len(sb) >= 4 and (sa in sb or sb in sa):
        return 0.92
    ta, tb = set(na.split()), set(nb.split())
    jac = len(ta & tb) / len(ta | tb)
    # surname match for players ("Novak Djokovic" vs "Djokovic")
    last = 0.9 if (na.split()[-1] == nb.split()[-1] and len(na.split()[-1]) > 3) else 0.0
    return max(SequenceMatcher(None, sa, sb).ratio(), jac, last)


def parse_kalshi_date(event_ticker: str) -> date | None:
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker)
    if not m or m.group(2) not in MONTHS:
        return None
    return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)))


def split_teams(title: str) -> tuple[str, str] | None:
    parts = re.split(r":|\s-\s|\(", title)
    for p in parts:
        m = re.search(r"(.+?)\s+vs\.?\s+(.+)", p.strip(), flags=re.I)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None


class SportsMatcher:
    def __init__(self, kalshi_get, cache_seconds: int = 300):
        """kalshi_get(path, **params) -> json"""
        self.get = kalshi_get
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _series_markets(self, series: str) -> list[dict]:
        hit = self._cache.get(series)
        if hit and time.time() - hit[0] < self.cache_seconds:
            return hit[1]
        out, cursor = [], None
        for _ in range(5):
            p = {"series_ticker": series, "status": "open", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            j = self.get("/markets", **p)
            out += j.get("markets", [])
            cursor = j.get("cursor")
            if not cursor:
                break
        self._cache[series] = (time.time(), out)
        return out

    # ------------------------------------------------------------------ #
    def classify(self, t: dict) -> dict | None:
        """Work out league, date and bet type from a Polymarket trade. None = unsupported."""
        slug = (t.get("slug") or "").lower()
        ev = (t.get("eventSlug") or "").lower()
        title = t.get("title") or ""
        m = re.match(r"([a-z0-9]+)-(.*?)-?(\d{4}-\d{2}-\d{2})", ev or slug)
        if not m:
            return None
        prefix, d = m.group(1), datetime.strptime(m.group(3), "%Y-%m-%d").date()
        codes = [x for x in m.group(2).split("-") if x]
        if prefix not in PREFIX_SERIES:
            return {"skip": f"league '{prefix}' not set up"}
        low = title.lower()
        rest = slug[len(ev):] if ev and slug.startswith(ev) else ""
        mg = re.search(r"-game(\d+)$", slug)
        if mg and prefix in MAP_SERIES and "winner" in low:
            return {"prefix": prefix, "date": d, "kind": "map", "map": int(mg.group(1)), "codes": codes}
        if any(w in low or w in rest for w in SKIP_WORDS):
            return {"skip": "bet type not copied (totals/spreads/props)"}
        mw = re.search(r"will (.+?) win on", low)
        if mw:
            team = title[mw.start(1):mw.end(1)]
            code = rest.strip("-").split("-")[0] if rest else None
            return {"prefix": prefix, "date": d, "kind": "team_yesno", "team": team,
                    "code": code, "codes": codes}
        if "draw" in low:
            return {"prefix": prefix, "date": d, "kind": "draw", "codes": codes}
        if slug == ev or not rest:
            return {"prefix": prefix, "date": d, "kind": "winner", "codes": codes}
        return {"skip": "bet type not copied"}

    def lookup(self, t: dict):
        """Return (Target-like dict {ticker, side}, None) or (None, reason)."""
        c = self.classify(t)
        if not c:
            return None, "not a recognised sports market"
        if "skip" in c:
            return None, c["skip"]
        outcome = (t.get("outcome") or "").strip()
        teams = split_teams(t.get("title") or "")

        if c["kind"] == "map":
            series = [MAP_SERIES[c["prefix"]]]
        else:
            series = PREFIX_SERIES[c["prefix"]]

        cands = []
        for s in series:
            try:
                cands += self._series_markets(s)
            except Exception as e:
                return None, f"Kalshi lookup failed: {e}"
        # same day (±1 for time zones)
        cands = [m for m in cands
                 if (kd := parse_kalshi_date(m.get("event_ticker", ""))) and abs((kd - c["date"]).days) <= 1]
        if not cands:
            return None, "no Kalshi game on that date"

        if c["kind"] == "map":
            cands = [m for m in cands if m.get("event_ticker", "").endswith(f"-{c['map']}")]

        by_event: dict[str, list[dict]] = {}
        for m in cands:
            by_event.setdefault(m["event_ticker"], []).append(m)

        def opponent_ok(m) -> bool:
            if not teams:
                return True
            other = teams[1] if name_sim(outcome, teams[0]) >= name_sim(outcome, teams[1]) else teams[0]
            sibs = [x.get("yes_sub_title") or "" for x in by_event.get(m["event_ticker"], []) if x is not m]
            rules = m.get("rules_primary") or ""
            return (any(name_sim(other, s) >= 0.8 for s in sibs) or name_sim(other, rules) > 0.5
                    or norm(other).replace(" ", "") in norm(rules).replace(" ", ""))

        def code_of(m) -> str:
            return m["ticker"].rsplit("-", 1)[-1].lower()

        codes = [x.lower() for x in c.get("codes", [])]

        def event_has_codes(m) -> bool:
            # e.g. PM "lal","bos" and Kalshi event "...26OCT21LALBOS"
            evtail = m["event_ticker"].split("-")[1].lower() if "-" in m["event_ticker"] else ""
            return len(codes) >= 2 and all(cd in evtail for cd in codes[:2])

        if c["kind"] == "draw":
            # find the game by the two teams, then its Tie market
            if not teams:
                return None, "couldn't read teams"
            for ev, ms in by_event.items():
                subs = [m.get("yes_sub_title", "") for m in ms]
                names_ok = max(name_sim(teams[0], s) for s in subs) > 0.8 and max(name_sim(teams[1], s) for s in subs) > 0.8
                if names_ok or event_has_codes(ms[0]):
                    tie = [m for m in ms if (m.get("yes_sub_title") or "").lower() == "tie"]
                    if tie:
                        return {"ticker": tie[0]["ticker"],
                                "side": "yes" if outcome.lower() == "yes" else "no"}, None
            return None, "no matching Kalshi tie market"

        if c["kind"] == "team_yesno":
            want, side = c["team"], ("yes" if outcome.lower() == "yes" else "no")
            want_code = c.get("code")
        else:  # winner / map: the outcome is the team or player backed
            want, side = outcome, "yes"
            idx = t.get("outcomeIndex")
            want_code = codes[idx] if isinstance(idx, int) and idx < len(codes) else None

        # 1) team codes in both IDs (handles "Lakers" vs "Los Angeles L")
        if want_code:
            for m in cands:
                if code_of(m) == want_code.lower() and event_has_codes(m):
                    return {"ticker": m["ticker"], "side": side, "score": 1.0, "how": "code"}, None
        # 2) team names
        best, best_s = None, 0.0
        for m in cands:
            s = name_sim(want, m.get("yes_sub_title") or "")
            if s > best_s:
                best, best_s = m, s
        if not best or best_s < 0.8:
            return None, f"no Kalshi market for '{want}' (best {best_s:.2f})"
        if c["kind"] != "team_yesno" and not opponent_ok(best):
            return None, "found team but opponent didn't match"
        return {"ticker": best["ticker"], "side": side, "score": round(best_s, 2), "how": "name"}, None
