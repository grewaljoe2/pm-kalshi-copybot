"""One-time diagnostic: log Kalshi NBA/NFL player-prop market formats and ESPN lookups."""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger("probe")
K = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "Mozilla/5.0 pm-kalshi-copybot/1.0"}
ESPN = "https://site.web.api.espn.com/apis"

PROPS = ["KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBAPRA", "KXNFLPASSYDS",
         "KXNFLRSHYDS", "KXNFLRECYDS", "KXNFLREC", "KXNFLANYTD", "KXNFLPASSTDS"]


def _j(url, **params):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def _try(label, url, **params):
    try:
        d = _j(url, **params)
        log.info("PROBE2 %s OK %s", label, json.dumps(d)[:900])
        return d
    except Exception as e:
        log.info("PROBE2 %s FAIL %s", label, e)


def run() -> None:
    time.sleep(20)
    for t in PROPS:
        try:
            ms = _j(f"{K}/markets", series_ticker=t, status="open", limit=200).get("markets", [])
            evs = sorted({m["event_ticker"] for m in ms})
            log.info("PROBE2 %s n=%d events=%s", t, len(ms), evs[:6])
            for m in ms[:2]:
                log.info("PROBE2 MKT %s", json.dumps({k: m.get(k) for k in (
                    "ticker", "event_ticker", "title", "yes_sub_title", "floor_strike", "strike_type",
                    "yes_ask_dollars", "yes_bid_dollars", "volume_fp", "volume", "exchange_index",
                    "custom_strike", "close_time")} | {"rules": (m.get("rules_primary") or "")[:300]}))
        except Exception as e:
            log.info("PROBE2 %s err %s", t, e)
        time.sleep(0.3)
    _try("search", f"{ESPN}/common/v3/search", query="Jalen Brunson", limit=3, type="player")
    _try("roster", f"{ESPN}/site/v2/sports/basketball/nba/teams/nyk/roster")
    _try("gamelog-prev", f"{ESPN}/common/v3/sports/basketball/nba/athletes/1966/gamelog", season=2026)
    _try("scoreboard-web", f"{ESPN}/site/v2/sports/basketball/nba/scoreboard")
    try:
        j = _j(f"{K}/multivariate_event_collections", status="open", limit=200)
        cols = j.get("multivariate_contracts") or j.get("multivariate_event_collections") or []
        for c in cols:
            ser = sorted({e.get("series_ticker") or e.get("event_ticker", "")[:14]
                          for e in (c.get("associated_events") or [])})
            log.info("PROBE2 COL %s | %s | %s", c.get("collection_ticker"), c.get("title"),
                     " ".join(s for s in ser if s)[:400])
    except Exception as e:
        log.info("PROBE2 collections err %s", e)
