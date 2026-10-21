"""
Bet score (0-100): how good a whale bet looks to copy on Kalshi right now.

  whale record   0-25  profitable this month and all-time, bigger long-run profit = more
  conviction     0-20  this bet vs the whale's usual bet size
  consensus      0-20  other tracked whales on the same side (minus if on the other side)
  price edge     0-25  Kalshi price vs the price the whale paid (cheaper than whale = better)
  sweet spot     0-10  prices between 25c and 75c; longshots and heavy favourites score less

Expected value uses the whale's price as the fair chance (plus a small skill bump for
proven whales) minus the Kalshi price and fee.
"""
from __future__ import annotations

import math


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def whale_record_points(stats: dict | None) -> float:
    if not stats:
        return 12.0  # unknown: middling
    pm, pa = stats.get("pnl_month") or 0, stats.get("pnl_all") or 0
    pts = clamp(5 * (math.log10(max(pa, 1)) - 3), 0, 15)  # $1k->0, $100k->10, $1M->15
    pts += 10 if pm > 0 else 0
    if pa <= 0:
        pts -= 10
    return clamp(pts, 0, 25)


def conviction_points(whale_usd: float, stats: dict | None) -> float:
    median = (stats or {}).get("median_usd") or 0
    if median <= 0:
        return 8.0
    ratio = whale_usd / median
    return clamp(20 * math.log2(1 + ratio) / 3, 0, 20)  # 1x ->6.7, 3x ->13, 7x ->20


def consensus_points(same: int, opposite: int) -> float:
    return clamp(10 * same - 15 * opposite, -20, 20)


def price_edge_points(whale_px: float, ask: float) -> float:
    edge = whale_px - ask  # +0.03 = Kalshi 3c cheaper than whale paid
    return clamp(12.5 + 250 * edge, 0, 25)


def sweet_spot_points(ask: float) -> float:
    if 0.25 <= ask <= 0.75:
        return 10.0
    if 0.15 <= ask <= 0.85:
        return 5.0
    return 0.0


def fee_per_contract(price: float, rate: float = 0.07) -> float:
    return rate * price * (1 - price)


def expected_value(whale_px: float, ask: float, stats: dict | None, rate: float = 0.07) -> float:
    """EV per $1 staked. Fair chance = whale price + up to 2c skill bump for proven whales."""
    bump = 0.02 * clamp(whale_record_points(stats) / 25, 0, 1)
    p = clamp(whale_px + bump, 0.01, 0.99)
    return (p - ask - fee_per_contract(ask, rate)) / ask


MAX_DISCOUNT = 0.08  # Kalshi this much cheaper than the whale paid = the situation changed


def score_bet(*, whale_px: float, ask: float, whale_usd: float, stats: dict | None,
              same: int = 0, opposite: int = 0, rate: float = 0.07) -> dict:
    if whale_px - ask > MAX_DISCOUNT:
        # Not a bargain: the game has likely started, or news/results moved the price.
        return {"score": 0, "parts": {"stale": True}, "ev": -1.0,
                "note": "price dropped a lot since the whale bet"}
    parts = {
        "record": round(whale_record_points(stats), 1),
        "conviction": round(conviction_points(whale_usd, stats), 1),
        "consensus": round(consensus_points(same, opposite), 1),
        "price": round(price_edge_points(whale_px, ask), 1),
        "sweet": sweet_spot_points(ask),
    }
    total = clamp(sum(parts.values()), 0, 100)
    return {"score": round(total), "parts": parts,
            "ev": round(expected_value(whale_px, ask, stats, rate), 4)}


def stake_pct(score: float, tiers: list) -> float:
    """tiers like [[90, 0.05], [75, 0.035], [60, 0.02]] -> fraction of balance (0 = skip)."""
    for min_score, pct in sorted(tiers, key=lambda x: -x[0]):
        if score >= min_score:
            return float(pct)
    return 0.0
