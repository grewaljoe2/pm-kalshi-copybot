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
    # more soccer / basketball / hockey leagues Kalshi lists (Polymarket slug prefix -> Kalshi series)
    "col1": ["KXDIMAYORGAME"], "col": ["KXDIMAYORGAME"], "lib": ["KXCONMEBOLLIBGAME"],
    "sud": ["KXCONMEBOLSUDGAME"], "usoc": ["KXUSOPENCUPGAME"], "kor": ["KXKLEAGUEGAME"],
    "kor1": ["KXKLEAGUEGAME"], "jap": ["KXJLEAGUEGAME"], "j1": ["KXJLEAGUEGAME"], "fr2": ["KXLIGUE2GAME"],
    "es2": ["KXLALIGA2GAME"], "bl2": ["KXBUNDESLIGA2GAME"], "ita2": ["KXSERIEBGAME"], "itb": ["KXSERIEBGAME"],
    "tur": ["KXSUPERLIGGAME"], "sco": ["KXSCOTTISHPREMGAME"], "bel": ["KXBELGIANPLGAME"],
    "den": ["KXDENSUPERLIGAGAME"], "swe": ["KXALLSVENSKANGAME"], "nor": ["KXELITESERIENGAME"],
    "aus": ["KXALEAGUEGAME"], "egy1": ["KXEGYPLGAME"], "egy": ["KXEGYPLGAME"], "pol": ["KXEKSTRAKLASAGAME"],
    "gre": ["KXSLGREECEGAME"], "sui": ["KXSWISSLEAGUEGAME"], "aut": ["KXAUTBUNDESGAME"],
    "chn": ["KXCHNSLGAME"], "nwsl": ["KXNWSLGAME"], "bra2": ["KXBRASILEIROBGAME"],
    "fac": ["KXFACUPGAME"], "cdr": ["KXCOPADELREYGAME"], "dfb": ["KXDFBPOKALGAME"],
    "euroleague": ["KXEUROLEAGUEGAME"], "eul": ["KXEUROLEAGUEGAME"], "khl": ["KXKHLGAME"],
    "kbo": ["KXKBOGAME"], "npb": ["KXNPBGAME"], "cfl": ["KXCFLGAME"], "afl": ["KXAFLGAME"],
}
MAP_SERIES = {"cs2": "KXCS2MAP", "lol": "KXLOLMAP", "dota2": "KXDOTA2MAP", "val": "KXVALORANTMAP"}

# Full-game spreads and totals (Kalshi lists one market per line)
_LEAGUE = {"nba": "NBA", "nfl": "NFL", "nhl": "NHL", "mlb": "MLB", "wnba": "WNBA", "cfb": "NCAAF",
           "ncaaf": "NCAAF", "cbb": "NCAAMB", "ncaab": "NCAAMB", "epl": "EPL", "ucl": "UCL", "uel": "UEL",
           "lal": "LALIGA", "laliga": "LALIGA", "sea": "SERIEA", "seriea": "SERIEA", "bun": "BUNDESLIGA",
           "bundesliga": "BUNDESLIGA", "fl1": "LIGUE1", "ligue1": "LIGUE1", "mls": "MLS",
           "cs2": "CS2", "lol": "LOL", "dota2": "DOTA2", "val": "VALORANT"}
SPREAD_SERIES = {k: f"KX{v}SPREAD" for k, v in _LEAGUE.items()}
TOTAL_SERIES = {k: f"KX{v}TOTAL" for k, v in _LEAGUE.items()}
TOTAL_SERIES.update({"cs2": "KXCS2TOTALMAPS", "lol": "KXLOLTOTALMAPS", "dota2": "KXDOTA2TOTALMAPS",
                     "val": "KXVALORANTTOTALMAPS"})

# First half / first quarter (Kalshi: KXNBA1H, KXNBA1HSPREAD, KXNBA1HTOTAL, ...)
PERIOD_LEAGUES = {"nba": "NBA", "nfl": "NFL", "cfb": "NCAAF", "ncaaf": "NCAAF", "wnba": "WNBA",
                  "cbb": "NCAAMB", "ncaab": "NCAAMB"}

# Player props: (league prefix, stat words) -> Kalshi series. Kalshi lines are "N+" (floor N-0.5).
PROP_SERIES = {
    "nba": [("pts+reb+ast|points, rebounds|pra", "KXNBAPRA"), ("three|3-point|3pt", "KXNBA3PT"),
            ("rebound", "KXNBAREB"), ("assist", "KXNBAAST"), ("point", "KXNBAPTS")],
    "nfl": [("passing yards|pass yds", "KXNFLPASSYDS"), ("passing touchdown|passing td", "KXNFLPASSTDS"),
            ("rushing yards|rush yds", "KXNFLRSHYDS"), ("receiving yards|rec yds", "KXNFLRECYDS"),
            ("reception", "KXNFLREC"), ("anytime|touchdown|td", "KXNFLTD")],
}

# part-game / prop bets we never copy
SKIP_WORDS = ("btts", "both teams", "exact", "first", "1h", "2h", "half", "quarter", "1st", "2nd",
              "corners", "kills", "props", "team total", "team-total", "player", "inning", "period")
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
        keep = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "no_sub_title", "floor_strike",
                "cap_strike", "strike_type", "close_time", "expected_expiration_time", "status")
        slim = []
        for m in out:  # keep only what matching needs (full listings with rules text eat memory)
            d = {k: m.get(k) for k in keep if m.get(k) is not None}
            if m.get("rules_primary"):
                d["rules_primary"] = m["rules_primary"][:300]
            slim.append(d)
        now = time.time()
        for k in [k for k, v in self._cache.items() if now - v[0] > 2 * self.cache_seconds]:
            del self._cache[k]  # drop stale leagues instead of keeping every listing forever
        self._cache[series] = (now, slim)
        return slim

    # ------------------------------------------------------------------ #
    def classify(self, t: dict) -> dict | None:
        """Work out league, date and bet type from a Polymarket trade. None = unsupported."""
        slug = (t.get("slug") or "").lower()
        ev = (t.get("eventSlug") or "").lower()
        title = t.get("title") or ""
        m = re.match(r"([a-z0-9]+)-(.*?)-?(\d{4}-\d{2}-\d{2})", ev or slug)
        if not m:
            return None
        try:
            prefix, d = m.group(1), datetime.strptime(m.group(3), "%Y-%m-%d").date()
        except ValueError:
            return None  # malformed date in the slug
        codes = [x for x in m.group(2).split("-") if x]
        if prefix not in PREFIX_SERIES:
            return {"skip": f"league '{prefix}' not set up"}
        low = title.lower()
        base = re.sub(r"-more-markets$", "", ev) if ev else ""
        if base and slug.startswith(base):
            rest = slug[len(base):]
        else:
            rest = "-" + slug  # unknown layout: never treat as the main winner market
        mg = re.search(r"-game(\d+)$", slug)
        if mg and prefix in MAP_SERIES and "winner" in low:
            return {"prefix": prefix, "date": d, "kind": "map", "map": int(mg.group(1)), "codes": codes}
        # player props: "LeBron James: Points O/U 25.5" (Over/Under) or "Will X score 25+ points?"
        if prefix in PROP_SERIES:
            pr = self._classify_prop(prefix, title, low, rest)
            if pr:
                pr.update(prefix=prefix, date=d, codes=codes)
                return pr
        # first half / first quarter: same bet types as the full game, on the period's own series
        period = None
        if prefix in PERIOD_LEAGUES:
            if re.search(r"\b(1h|first half|1st half)\b", low) or re.search(r"-1h(-|$)|first-half", rest):
                period = "1H"
            elif re.search(r"\b(1q|first quarter|1st quarter)\b", low) or re.search(r"-1q(-|$)|first-quarter", rest):
                period = "1Q"
        if period:
            low_p = re.sub(r"\b(1h|1q|first half|1st half|first quarter|1st quarter)\b", "", low)
            rest_p = re.sub(r"-(1h|1q|first-half|first-quarter)", "", rest)
            rest_p = re.sub(r"-(moneyline|winner|ml)$", "", rest_p)
            if any(w in low_p or w in rest_p for w in SKIP_WORDS):
                return {"skip": "bet type not copied (part-game / props)"}
            c = self._classify_main(prefix, d, codes, title, low_p, rest_p)
            if c and "skip" not in c:
                c["period"] = period
            return c
        if any(w in low or w in rest for w in SKIP_WORDS):
            return {"skip": "bet type not copied (part-game / props)"}
        return self._classify_main(prefix, d, codes, title, low, rest)

    @staticmethod
    def _classify_prop(prefix, title, low, rest):
        series = None
        for words, ser in PROP_SERIES[prefix]:
            if re.search(words, low):
                series = ser
                break
        if not series:
            return None
        if re.search(r"\bvs\.?\b", title.split(":")[0], flags=re.I):
            return None  # "Team vs. Team: ..." is a game market, not a player prop
        mo = re.search(r"^(.+?):\s*.*?(?:o/u|over/under)\s*([\d.]+)", title, flags=re.I)
        if mo:
            return {"kind": "prop", "series": series, "player": mo.group(1).strip(), "floor": float(mo.group(2)),
                    "ou": True}
        my = re.search(r"will (.+?) (?:score|record|have|get|throw|rush|catch)\s+(\d+)\+", title, flags=re.I)
        if my:
            return {"kind": "prop", "series": series, "player": my.group(1).strip(),
                    "floor": float(my.group(2)) - 0.5, "ou": False}
        if series == "KXNFLTD":
            ma = re.search(r"^(.+?):\s*anytime", title, flags=re.I) or re.search(r"will (.+?) score a touchdown", title, flags=re.I)
            if ma:
                return {"kind": "prop", "series": series, "player": ma.group(1).strip(), "floor": 0.5, "ou": False}
        return None

    def _classify_main(self, prefix, d, codes, title, low, rest):
        mt = re.search(r"o/u\s*([\d.]+)", low)
        if ("total" in rest or mt) and mt:
            if prefix not in TOTAL_SERIES:
                return {"skip": f"no Kalshi totals for '{prefix}'"}
            return {"prefix": prefix, "date": d, "kind": "total", "line": float(mt.group(1)), "codes": codes}
        body = title.split(":", 1)[1] if ":" in title else title
        pairs = re.findall(r"([^()]+?)\s*\(([+-][\d.]+)\)", body)
        neg = [(n.strip(" vs.").strip(), v) for n, v in pairs if v.startswith("-")]
        ms = (neg[0][0], neg[0][1][1:]) if neg else None
        if ms and ("spread" in rest or "handicap" in rest):
            if prefix not in SPREAD_SERIES:
                return {"skip": f"no Kalshi spreads for '{prefix}'"}
            fav_code = None
            if len(codes) >= 2:
                fav_code = codes[0] if "-away-" in rest + "-" else (codes[1] if "-home-" in rest + "-" else None)
            return {"prefix": prefix, "date": d, "kind": "spread", "line": float(ms[1]),
                    "fav": re.sub(r"^vs\.?\s+", "", ms[0]), "fav_code": fav_code, "codes": codes}
        if "total" in rest or "spread" in rest or "handicap" in rest:
            return {"skip": "couldn't read the line"}
        mw = re.search(r"will (.+?) win on", low)
        if mw:
            team = title[mw.start(1):mw.end(1)]
            code = rest.strip("-").split("-")[0] if rest else None
            return {"prefix": prefix, "date": d, "kind": "team_yesno", "team": team,
                    "code": code, "codes": codes}
        if "draw" in low:
            return {"prefix": prefix, "date": d, "kind": "draw", "codes": codes}
        if rest == "":
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

        if c["kind"] == "prop":
            return self._prop_lookup(c, outcome)
        per = c.get("period")
        if per:
            lg = PERIOD_LEAGUES[c["prefix"]]
            series = [f"KX{lg}{per}" + {"total": "TOTAL", "spread": "SPREAD"}.get(c["kind"], "")]
            if c["kind"] not in ("winner", "total", "spread"):
                return None, "period bet type not copied"
        elif c["kind"] == "map":
            series = [MAP_SERIES[c["prefix"]]]
        elif c["kind"] == "total":
            series = [TOTAL_SERIES[c["prefix"]]]
        elif c["kind"] == "spread":
            series = [SPREAD_SERIES[c["prefix"]]]
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

        if c["kind"] in ("total", "spread"):
            return self._line_lookup(c, t, by_event, teams, codes, outcome, event_has_codes)

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

    def _prop_lookup(self, c, outcome):
        """Player prop: same player, same game date, same line ("26+" on Kalshi = O/U 25.5)."""
        try:
            cands = self._series_markets(c["series"])
        except Exception as e:
            return None, f"Kalshi lookup failed: {e}"
        best, best_s = None, 0.0
        for m in cands:
            kd = parse_kalshi_date(m.get("event_ticker", ""))
            if not kd or abs((kd - c["date"]).days) > 1:
                continue
            k = self._strike(m)
            if k is None or abs(k - c["floor"]) > 1e-6:
                continue
            who = (m.get("title") or "").split(":")[0]
            sc = name_sim(c["player"], who)
            if sc > best_s:
                best, best_s = m, sc
        if not best or best_s < 0.85:
            return None, f"no Kalshi {c['series']} {c['floor'] + 0.5:g}+ for {c['player']}"
        o = (outcome or "").strip().lower()
        side = "no" if o in ("under", "no") else "yes"
        return {"ticker": best["ticker"], "side": side, "how": "prop"}, None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _strike(m: dict) -> float | None:
        v = m.get("floor_strike")
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def _line_lookup(self, c, t, by_event, teams, codes, outcome, event_has_codes):
        """Totals and spreads: same game, exact same line."""
        # 1) find the game: team codes in the event ticker, else both team names in the rules
        game = None
        for ev, ms in by_event.items():
            if event_has_codes(ms[0]):
                game = ms
                break
        if game is None and teams:
            for ev, ms in by_event.items():
                rules = norm(ms[0].get("rules_primary") or "").replace(" ", "")
                if all(norm(x).replace(" ", "")[:8] in rules for x in teams):
                    game = ms
                    break
        line = c["line"]
        if game is None and c["kind"] == "spread":
            # fallback: the favourite's own "wins by over <line>" market on that date
            for ms in by_event.values():
                for m in ms:
                    k = self._strike(m)
                    team = re.split(r"\s+wins by", m.get("yes_sub_title") or "")[0]
                    if k is not None and abs(k - line) < 1e-6 and name_sim(c["fav"], team) >= 0.85:
                        game = ms
                        break
                if game:
                    break
        if game is None:
            return None, "couldn't find the same game on Kalshi"
        at_line = [m for m in game if (k := self._strike(m)) is not None and abs(k - line) < 1e-6]
        if not at_line:
            return None, f"Kalshi doesn't list the {line} line for this game"

        if c["kind"] == "total":
            o = outcome.lower()
            if o not in ("over", "under"):
                return None, f"unexpected total outcome '{outcome}'"
            m = at_line[0]
            return {"ticker": m["ticker"], "side": "yes" if o == "over" else "no", "how": "line"}, None

        # spread: find the favourite's "wins by over <line>" market
        def team_of(m):
            return re.split(r"\s+wins by", m.get("yes_sub_title") or m.get("title") or "")[0]

        def suffix_code(m):
            mm = re.match(r"([A-Z]+)\d*$", m["ticker"].rsplit("-", 1)[-1])
            return mm.group(1).lower() if mm else ""

        fav = None
        if c.get("fav_code"):
            fav = next((m for m in at_line if suffix_code(m) == c["fav_code"].lower()), None)
        if fav is None:
            scored = sorted(((name_sim(c["fav"], team_of(m)), m) for m in at_line), key=lambda x: -x[0])
            if scored and scored[0][0] >= 0.8:
                fav = scored[0][1]
        if fav is None:
            return None, f"Kalshi doesn't list {c['fav']} -{line}"
        if name_sim(outcome, c["fav"]) >= 0.8:
            backs_fav = True
        elif teams and any(name_sim(outcome, x) >= 0.8 for x in teams):
            backs_fav = False
        else:
            backs_fav = t.get("outcomeIndex") == 0
        return {"ticker": fav["ticker"], "side": "yes" if backs_fav else "no", "how": "line"}, None
