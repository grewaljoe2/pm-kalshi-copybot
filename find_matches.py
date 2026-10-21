#!/usr/bin/env python3
"""
Suggest Kalshi markets that match a whale's Polymarket positions.

Polymarket and Kalshi list different markets with different wording and
resolution rules, so this NEVER writes mappings itself. It prints candidates
and a YAML snippet; you verify each one and paste it into mappings.yaml.

Usage:
    python find_matches.py 0xWHALEWALLET [--min-usd 1000] [--pages 10]
"""
import argparse
import difflib
import re

import requests

POLY = "https://data-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
STOP = set("will the a an of in on to by be is for and or at vs win 2025 2026 2027 before after".split())


def tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1}


def score(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / max(1, len(ta | tb))
    seq = difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
    return 0.6 * jac + 0.4 * seq


def kalshi_markets(pages: int) -> list[dict]:
    out, cursor = [], None
    for _ in range(pages):
        p = {"status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        r = requests.get(f"{KALSHI}/markets", params=p, timeout=30)
        r.raise_for_status()
        j = r.json()
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--min-usd", type=float, default=500, help="ignore smaller positions")
    ap.add_argument("--pages", type=int, default=10, help="Kalshi market pages (1000 each)")
    ap.add_argument("--top", type=int, default=3)
    a = ap.parse_args()

    pos = requests.get(f"{POLY}/positions", params={"user": a.wallet, "sizeThreshold": 1},
                       timeout=30).json()
    pos = [p for p in pos if float(p.get("currentValue", 0) or 0) >= a.min_usd]
    print(f"{len(pos)} Polymarket positions >= ${a.min_usd:.0f}")
    km = kalshi_markets(a.pages)
    print(f"{len(km)} open Kalshi markets loaded\n")
    for m in km:
        m["_text"] = f'{m.get("title", "")} {m.get("yes_sub_title", "")}'

    for p in pos:
        title = p.get("title", "")
        ranked = sorted(km, key=lambda m: score(title, m["_text"]), reverse=True)[: a.top]
        print("=" * 78)
        print(f'PM: {title}  [{p.get("outcome")}]  ${float(p.get("currentValue", 0)):,.0f}')
        print(f'    condition_id: {p.get("conditionId")}')
        for m in ranked:
            print(f'  {score(title, m["_text"]):.2f}  {m["ticker"]:<38} {m["_text"][:70]}')
        best = ranked[0] if ranked else None
        if best:
            print("\n  # verify resolution rules match before using!")
            print(f'  - polymarket: {{condition_id: "{p.get("conditionId")}"}}  # {title[:50]}')
            print("    outcomes:")
            print(f'      "Yes": {{ticker: "{best["ticker"]}", side: "yes"}}')
            print(f'      "No":  {{ticker: "{best["ticker"]}", side: "no"}}')
        print()


if __name__ == "__main__":
    main()
