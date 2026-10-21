"""
Whale screener: rank top Polymarket sports traders by how copyable they are on Kalshi.

For each trader on the SPORTS leaderboard (week / month / all-time) it checks:
  - profit this month and all-time (both must be positive)
  - trades per day and typical bet size
  - share of recent bets that the bot can copy to Kalshi automatically
    (game/match winners and esports map winners in leagues Kalshi lists)
  - share of bets at extreme prices (<10c or >90c), a sign of late / in-game betting
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter

import requests

from sports_match import SportsMatcher

log = logging.getLogger("screener")
POLY = "https://data-api.polymarket.com"
UA = {"User-Agent": "pm-kalshi-copybot/1.0"}


def _get(path: str, **params):
    r = requests.get(f"{POLY}{path}", params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _leaderboard(period: str, limit: int = 50, user: str | None = None) -> list[dict]:
    p = {"category": "SPORTS", "timePeriod": period, "orderBy": "PNL", "limit": limit}
    if user:
        p["user"] = user
    data = _get("/v1/leaderboard", **p)
    return data if isinstance(data, list) else data.get("data", [])


def screen(max_wallets: int = 90) -> list[dict]:
    classifier = SportsMatcher(lambda *a, **k: {})  # classify() needs no Kalshi calls
    board: dict[str, dict] = {}
    for period in ("WEEK", "MONTH", "ALL"):
        try:
            for e in _leaderboard(period):
                w = (e.get("proxyWallet") or "").lower()
                if not w:
                    continue
                d = board.setdefault(w, {"wallet": w, "name": e.get("userName") or w[:10]})
                d[f"pnl_{period.lower()}"] = float(e.get("pnl") or 0)
                d[f"vol_{period.lower()}"] = float(e.get("vol") or 0)
        except Exception as ex:
            log.warning("leaderboard %s failed: %s", period, ex)

    results = []
    for w, d in list(board.items())[:max_wallets]:
        try:
            for period in ("MONTH", "ALL"):
                if f"pnl_{period.lower()}" not in d:
                    rows = _leaderboard(period, limit=1, user=w)
                    d[f"pnl_{period.lower()}"] = float(rows[0].get("pnl") or 0) if rows else None
            trades = _get("/trades", user=w, limit=300)
            trades = trades if isinstance(trades, list) else trades.get("data", [])
            if not trades:
                continue
            ts = [int(t.get("timestamp") or 0) for t in trades]
            span = max(1e-9, (max(ts) - min(ts)) / 86400)
            usd = sorted(float(t.get("size") or 0) * float(t.get("price") or 0) for t in trades)
            kinds, leagues, copyable = Counter(), Counter(), 0
            for t in trades:
                c = classifier.classify(t)
                lg = ((t.get("eventSlug") or t.get("slug") or "").split("-") or ["?"])[0]
                leagues[lg] += 1
                if c and "skip" not in c:
                    copyable += 1
                    kinds[c["kind"]] += 1
                else:
                    kinds["other"] += 1
            extreme = sum(1 for t in trades if not 0.1 <= float(t.get("price") or 0) <= 0.9)
            cutoff = time.time() - 48 * 3600
            d["_buys"] = [{"w": w, "name": d["name"], "cond": t.get("conditionId"), "out": t.get("outcome"),
                           "usd": round(float(t.get("size") or 0) * float(t.get("price") or 0), 2),
                           "px": float(t.get("price") or 0), "ts": int(t.get("timestamp") or 0),
                           "slug": t.get("slug"), "eventSlug": t.get("eventSlug"), "title": t.get("title"),
                           "outcomeIndex": t.get("outcomeIndex")}
                          for t in trades if str(t.get("side")).upper() == "BUY"
                          and int(t.get("timestamp") or 0) >= cutoff]
            d.update({
                "trades_sampled": len(trades),
                "per_day": round(len(trades) / span, 1) if span > 0.05 else len(trades),
                "days_covered": round(span, 1),
                "median_usd": round(usd[len(usd) // 2], 0),
                "copyable_pct": round(100 * copyable / len(trades)),
                "extreme_price_pct": round(100 * extreme / len(trades)),
                "buys_pct": round(100 * sum(1 for t in trades if str(t.get("side")).upper() == "BUY") / len(trades)),
                "top_leagues": leagues.most_common(5),
                "kinds": dict(kinds),
                "last_trade_h": round((time.time() - max(ts)) / 3600, 1),
            })
            # score: copyable, profitable, sane frequency, not late/in-game, real sizes
            pm, pa = d.get("pnl_month") or 0, d.get("pnl_all") or 0
            ok = pm > 0 and pa > 0
            freq_ok = 2 <= d["per_day"] <= 150
            d["score"] = round(
                (d["copyable_pct"] / 100) * 50
                + (min(math.log10(max(pm, 1)), 6) / 6) * 25
                + (1 - d["extreme_price_pct"] / 100) * 15
                + (10 if freq_ok else 0)
                - (40 if not ok else 0)
                - (15 if d["median_usd"] < 20 else 0)
                - (10 if d["last_trade_h"] > 72 else 0), 1)
            results.append(d)
            time.sleep(0.3)
        except Exception as ex:
            log.warning("screen %s failed: %s", w[:10], ex)
    results.sort(key=lambda x: -x.get("score", -999))
    return results
