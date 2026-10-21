"""
Whale report: how a Polymarket wallet has been doing, plus candidate Kalshi
matches for its open positions. Runs on the server (Polymarket/Kalshi public
APIs, no keys) and is shown on the dashboard at /report.
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter

import requests

from find_matches import score

log = logging.getLogger("report")
POLY = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "pm-kalshi-copybot/1.0"}

_kalshi_cache: dict = {"t": 0.0, "markets": []}


def _get(url: str, **params):
    r = requests.get(url, params=params, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _paged(path: str, wallet: str, cap: int = 1000, **extra) -> list[dict]:
    out, offset = [], 0
    while offset < cap:
        batch = _get(f"{POLY}/{path}", user=wallet, limit=500, offset=offset, **extra)
        if isinstance(batch, dict):
            batch = batch.get("data", [])
        out += batch
        if len(batch) < 500:
            break
        offset += 500
    return out


def kalshi_markets(max_pages: int = 25) -> list[dict]:
    """Open Kalshi markets (cached 1h), skipping multi-leg combo markets."""
    if time.time() - _kalshi_cache["t"] < 3600 and _kalshi_cache["markets"]:
        return _kalshi_cache["markets"]
    out, cursor = [], None
    for _ in range(max_pages):
        p = {"status": "open", "limit": 1000, "mve_filter": "exclude"}
        if cursor:
            p["cursor"] = cursor
        j = _get(f"{KALSHI}/markets", **p)
        for m in j.get("markets", []):
            if m.get("ticker", "").startswith("KXMVE"):
                continue
            m["_text"] = f'{m.get("title", "")} {m.get("yes_sub_title", "")}'
            out.append(m)
        cursor = j.get("cursor")
        if not cursor:
            break
    _kalshi_cache.update(t=time.time(), markets=out)
    return out


def build(wallet: str, match_min_usd: float = 500) -> dict:
    rep: dict = {"wallet": wallet, "generated": time.time(), "errors": []}

    # ---- open positions ----
    try:
        pos = _paged("positions", wallet, sizeThreshold=1)
    except Exception as e:
        pos = []
        rep["errors"].append(f"positions: {e}")
    open_val = sum(float(p.get("currentValue") or 0) for p in pos)
    unreal = sum(float(p.get("cashPnl") or 0) for p in pos)
    rep["open"] = {
        "count": len(pos),
        "value": round(open_val, 2),
        "unrealized_pnl": round(unreal, 2),
        "top": sorted(
            [{
                "title": p.get("title"), "outcome": p.get("outcome"),
                "value": round(float(p.get("currentValue") or 0), 2),
                "avg": p.get("avgPrice"), "cur": p.get("curPrice"),
                "pnl": round(float(p.get("cashPnl") or 0), 2),
                "end": p.get("endDate"), "condition_id": p.get("conditionId"),
            } for p in pos],
            key=lambda x: -x["value"])[:15],
    }

    # ---- closed positions (realized record) ----
    try:
        closed = _paged("closed-positions", wallet, cap=2000)
        wins = [c for c in closed if float(c.get("realizedPnl") or 0) > 0]
        realized = sum(float(c.get("realizedPnl") or 0) for c in closed)
        rep["closed"] = {
            "count": len(closed), "wins": len(wins),
            "win_rate": round(len(wins) / len(closed), 3) if closed else None,
            "realized_pnl": round(realized, 2),
            "biggest_win": round(max((float(c.get("realizedPnl") or 0) for c in closed), default=0), 2),
            "biggest_loss": round(min((float(c.get("realizedPnl") or 0) for c in closed), default=0), 2),
        }
    except Exception as e:
        rep["errors"].append(f"closed-positions: {e}")

    # ---- recent trading activity ----
    try:
        trades = _get(f"{POLY}/trades", user=wallet, limit=500)
        if isinstance(trades, dict):
            trades = trades.get("data", [])
        usd = [float(t.get("size") or 0) * float(t.get("price") or 0) for t in trades]
        ts = [int(t.get("timestamp") or 0) for t in trades if t.get("timestamp")]
        span_days = (max(ts) - min(ts)) / 86400 if len(ts) > 1 else 0
        rep["trades"] = {
            "sample": len(trades),
            "buys": sum(1 for t in trades if str(t.get("side")).upper() == "BUY"),
            "avg_usd": round(sum(usd) / len(usd), 2) if usd else 0,
            "median_usd": round(sorted(usd)[len(usd) // 2], 2) if usd else 0,
            "max_usd": round(max(usd), 2) if usd else 0,
            "per_day": round(len(trades) / span_days, 1) if span_days else None,
            "last_trade_hours_ago": round((time.time() - max(ts)) / 3600, 1) if ts else None,
            "top_markets": Counter(t.get("title") for t in trades).most_common(8),
        }
    except Exception as e:
        rep["errors"].append(f"trades: {e}")

    rep["_positions"] = pos
    return rep


def add_matches(rep: dict, match_min_usd: float = 500) -> None:
    """Kalshi match candidates for sizable open positions (slow part, done after the summary)."""
    import difflib
    from find_matches import tokens
    try:
        km = kalshi_markets()
        rep["kalshi_markets_scanned"] = len(km)
        for m in km:
            if "_tok" not in m:
                m["_tok"] = tokens(m["_text"])
        # inverted index: token -> markets containing it (fast pre-filter)
        index: dict[str, list[int]] = {}
        for i, m in enumerate(km):
            for tok in m["_tok"]:
                index.setdefault(tok, []).append(i)
        cands = []
        for p in rep.get("_positions", []):
            if float(p.get("currentValue") or 0) < match_min_usd:
                continue
            title = p.get("title", "")
            tt = tokens(title)
            hits: dict[int, float] = {}
            n = len(km)
            for tok in tt:
                ids = index.get(tok, [])
                if not ids:
                    continue
                w = math.log(1 + n / len(ids))  # rarer words count more
                for i in ids:
                    hits[i] = hits.get(i, 0.0) + w
            short = sorted(hits, key=lambda i: -hits[i])[:60]
            scored = []
            for i in short:
                m = km[i]
                jac = len(tt & m["_tok"]) / max(1, len(tt | m["_tok"]))
                seq = difflib.SequenceMatcher(None, title.lower(), m["_text"].lower()).ratio()
                scored.append((0.6 * jac + 0.4 * seq, m))
            scored.sort(key=lambda x: -x[0])
            cands.append({
                "pm_title": title, "outcome": p.get("outcome"),
                "condition_id": p.get("conditionId"),
                "value": round(float(p.get("currentValue") or 0), 2),
                "end": p.get("endDate"),
                "matches": [{"ticker": m["ticker"], "text": m["_text"][:120], "score": round(sc, 2),
                             "close": m.get("close_time")} for sc, m in scored[:3]],
            })
        rep["kalshi_candidates"] = sorted(cands, key=lambda c: -c["value"])[:25]
    except Exception as e:
        rep.setdefault("errors", []).append(f"kalshi: {e}")
