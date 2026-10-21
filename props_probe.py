"""One-time diagnostic: log Kalshi NBA/NFL player-prop market formats and an ESPN game log sample."""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("probe")
K = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "pm-kalshi-copybot/1.0"}


def _j(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def run() -> None:
    time.sleep(20)
    try:
        series = _j(f"{K}/series", category="Sports").get("series", [])
        tick = sorted(s["ticker"] for s in series
                      if any(k in s["ticker"] for k in ("NBA", "NFL")))
        log.info("PROBE series NBA/NFL (%d): %s", len(tick), " ".join(tick))
        props = [t for t in tick if not any(t.endswith(x) for x in
                 ("GAME", "SPREAD", "TOTAL", "SERIES", "CHAMP", "MVP"))]
        for t in props[:40]:
            try:
                ms = _j(f"{K}/markets", series_ticker=t, status="open", limit=3).get("markets", [])
            except Exception as e:
                log.info("PROBE %s err %s", t, e)
                continue
            if not ms:
                continue
            for m in ms[:2]:
                log.info("PROBE MKT %s", json.dumps({k: m.get(k) for k in (
                    "ticker", "event_ticker", "title", "yes_sub_title", "subtitle", "floor_strike",
                    "strike_type", "yes_ask_dollars", "yes_bid_dollars", "volume", "exchange_index",
                    "custom_strike")} | {"rules": (m.get("rules_primary") or "")[:260]}))
            time.sleep(0.3)
    except Exception as e:
        log.warning("PROBE kalshi failed: %s", e)
    for sport, aid in (("basketball/nba", "1966"), ("football/nfl", "3139477")):
        try:
            g = _j(f"https://site.web.api.espn.com/apis/common/v3/sports/{sport}/athletes/{aid}/gamelog")
            st = (g.get("seasonTypes") or [{}])[0]
            cat = (st.get("categories") or [{}])[0]
            ev = (cat.get("events") or [{}])[0]
            log.info("PROBE ESPN %s labels=%s names=%s first=%s n=%d", sport, g.get("labels"),
                     g.get("names"), json.dumps(ev)[:300], len(cat.get("events") or []))
        except Exception as e:
            log.warning("PROBE ESPN %s failed: %s", sport, e)
    try:
        sb = _j("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard")
        e0 = (sb.get("events") or [{}])[0]
        log.info("PROBE ESPN scoreboard events=%d first=%s", len(sb.get("events") or []), e0.get("name"))
    except Exception as e:
        log.warning("PROBE scoreboard failed: %s", e)
