#!/usr/bin/env python3
"""
Polymarket -> Kalshi whale copy-trading bot.

Watches chosen Polymarket wallets, and when one of them trades a market you've
mapped to a Kalshi equivalent, mirrors the trade on Kalshi (paper by default).

Usage:
    python copybot.py                 # run with config.yaml
    python copybot.py --once          # one polling pass, then exit
    python copybot.py --config my.yaml
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import logging
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

log = logging.getLogger("copybot")

POLY_DATA = "https://data-api.polymarket.com"
KALSHI_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #
def load_yaml(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class State:
    """Persists seen trades, our positions and daily spend between restarts."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data = {"seen": {}, "positions": {}, "spent_by_day": {}, "initialized": []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text()))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)

    # trades already processed, keyed per wallet
    def is_seen(self, wallet: str, key: str) -> bool:
        return key in self.data["seen"].get(wallet, [])

    def mark_seen(self, wallet: str, key: str) -> None:
        lst = self.data["seen"].setdefault(wallet, [])
        lst.append(key)
        del lst[:-2000]  # cap memory

    # our Kalshi positions, keyed "TICKER|side"
    def position(self, ticker: str, side: str) -> int:
        return int(self.data["positions"].get(f"{ticker}|{side}", 0))

    def add_position(self, ticker: str, side: str, delta: int) -> None:
        k = f"{ticker}|{side}"
        self.data["positions"][k] = max(0, self.position(ticker, side) + delta)

    def total_exposure_contracts(self) -> int:
        return sum(self.data["positions"].values())

    def spent_today(self) -> float:
        return float(self.data["spent_by_day"].get(_today(), 0.0))

    def add_spent(self, usd: float) -> None:
        d = _today()
        self.data["spent_by_day"][d] = round(self.spent_today() + usd, 4)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Polymarket (read-only, public)
# --------------------------------------------------------------------------- #
class Polymarket:
    def __init__(self, session: requests.Session):
        self.s = session

    def trades(self, wallet: str, limit: int = 50) -> list[dict]:
        r = self.s.get(f"{POLY_DATA}/trades", params={"user": wallet, "limit": limit}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def position_size(self, wallet: str, condition_id: str, outcome: str) -> float:
        r = self.s.get(f"{POLY_DATA}/positions",
                       params={"user": wallet, "market": condition_id, "sizeThreshold": 0},
                       timeout=15)
        r.raise_for_status()
        for p in r.json() or []:
            if str(p.get("outcome", "")).lower() == outcome.lower():
                return float(p.get("size", 0) or 0)
        return 0.0


# --------------------------------------------------------------------------- #
# Kalshi (authenticated)
# --------------------------------------------------------------------------- #
class Kalshi:
    def __init__(self, env: str, key_id: str | None, key_path: str | None,
                 session: requests.Session, key_pem: str | None = None):
        self.base = KALSHI_URLS[env]
        self.prefix = "/trade-api/v2"
        self.key_id = key_id
        self.s = session
        self.pkey = None
        pem: bytes | None = None
        if key_pem:  # key pasted into an env var (literal "\n" allowed)
            pem = key_pem.replace("\\n", "\n").encode()
        elif key_path and os.path.exists(os.path.expanduser(key_path)):
            with open(os.path.expanduser(key_path), "rb") as f:
                pem = f.read()
        if key_id and pem:
            from cryptography.hazmat.primitives import serialization
            self.pkey = serialization.load_pem_private_key(pem, password=None)

    def _headers(self, method: str, path: str) -> dict:
        if not self.pkey:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + self.prefix + path.split("?")[0]).encode()
        sig = self.pkey.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.s.request(method, self.base + path,
                           headers=self._headers(method, path), timeout=15, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def market(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}")["market"]

    def balance_usd(self) -> float:
        return self._req("GET", "/portfolio/balance").get("balance", 0) / 100.0

    def create_order(self, ticker: str, side: str, action: str, count: int,
                     price: float, tif: str) -> dict:
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": side,                # "yes" | "no"
            "action": action,            # "buy" | "sell"
            "count": int(count),
            "type": "limit",
            f"{side}_price_dollars": f"{price:.4f}",
            "time_in_force": tif,
        }
        if action == "sell":
            body["reduce_only"] = True
        return self._req("POST", "/portfolio/orders", json=body)["order"]


def _px(market: dict, field: str) -> float | None:
    """Read a price from a Kalshi market as dollars (handles cents + _dollars fields)."""
    v = market.get(f"{field}_dollars")
    if v not in (None, ""):
        return float(v)
    v = market.get(field)
    if v in (None, 0):
        return None
    return float(v) / 100.0


# --------------------------------------------------------------------------- #
# Market mapping
# --------------------------------------------------------------------------- #
@dataclass
class Target:
    ticker: str
    side: str  # "yes" / "no"


class Mappings:
    """Polymarket (condition_id or slug) + outcome  ->  Kalshi ticker + side."""

    def __init__(self, path: str):
        self.by_cond: dict[str, dict] = {}
        self.by_slug: dict[str, dict] = {}
        raw = load_yaml(path).get("mappings", []) if Path(path).exists() else []
        for m in raw:
            pm = m.get("polymarket", {})
            outs = {k.lower(): Target(v["ticker"], v["side"].lower())
                    for k, v in (m.get("outcomes") or {}).items()}
            if pm.get("condition_id"):
                self.by_cond[pm["condition_id"].lower()] = outs
            if pm.get("slug"):
                self.by_slug[pm["slug"].lower()] = outs

    def lookup(self, trade: dict) -> Target | None:
        outs = (self.by_cond.get(str(trade.get("conditionId", "")).lower())
                or self.by_slug.get(str(trade.get("slug", "")).lower()))
        if not outs:
            return None
        return outs.get(str(trade.get("outcome", "")).lower())

    def __len__(self) -> int:
        return len(self.by_cond) + len(self.by_slug)


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class CopyBot:
    def __init__(self, cfg: dict):
        # Environment variables override config.yaml (this is how Render sets them)
        env = os.environ
        cfg.setdefault("kalshi", {})
        if env.get("BOT_MODE"):
            cfg["mode"] = env["BOT_MODE"].strip().lower()
        if env.get("KALSHI_ENV"):
            cfg["kalshi"]["env"] = env["KALSHI_ENV"].strip().lower()
        self.cfg = cfg
        self.risk = cfg["risk"]
        self.mode = cfg.get("mode", "paper")
        self.kalshi_env = cfg["kalshi"].get("env", "demo")
        sess = requests.Session()
        sess.headers["User-Agent"] = "pm-kalshi-copybot/1.0"
        self.pm = Polymarket(sess)
        k = cfg["kalshi"]
        self.kalshi = Kalshi(
            self.kalshi_env,
            env.get("KALSHI_KEY_ID") or k.get("key_id"),
            env.get("KALSHI_KEY_PATH") or k.get("private_key_path"),
            sess,
            key_pem=env.get("KALSHI_PRIVATE_KEY"),
        )
        if self.mode == "live" and not self.kalshi.pkey:
            raise SystemExit("Live mode needs KALSHI_KEY_ID plus KALSHI_KEY_PATH or KALSHI_PRIVATE_KEY.")
        data_dir = Path(env.get("DATA_DIR", "."))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.maps = Mappings(cfg.get("mappings_file", "mappings.yaml"))
        self.state = State(str(data_dir / cfg.get("state_file", "state.json")))
        self.journal = data_dir / cfg.get("journal_file", "journal.csv")
        self.whales = {w["address"].lower(): w for w in cfg["whales"]}
        self.auto_sports = bool(cfg.get("auto_match_sports", True))
        from sports_match import SportsMatcher
        # Market data: real Kalshi prices, except when live-trading on the demo site
        data_env = "demo" if (self.mode == "live" and self.kalshi_env == "demo") else "prod"
        self.kdata = Kalshi(data_env, None, None, sess)
        self.sports = SportsMatcher(lambda path, **p: self.kdata._req("GET", path, params=p))
        self.last_poll: float | None = None
        self.last_error: str | None = None
        self.balance: float | None = None

    # ---- pause switch (persisted, toggled from the dashboard) ------------ #
    @property
    def paused(self) -> bool:
        return bool(self.state.data.get("paused", False))

    def set_paused(self, value: bool) -> None:
        self.state.data["paused"] = bool(value)
        self.state.save()
        log.warning("Bot %s from dashboard", "PAUSED" if value else "RESUMED")

    # ---- main loop ------------------------------------------------------- #
    def run(self, once: bool = False, stop_event=None) -> None:
        log.info("mode=%s  kalshi_env=%s  whales=%d  mapped_markets=%d",
                 self.mode, self.kalshi_env, len(self.whales), len(self.maps))
        if self.mode == "live" and self.kalshi_env == "prod":
            log.warning("*** LIVE TRADING WITH REAL MONEY ***")
        while True:
            if self.mode == "live":
                try:
                    self.balance = self.kalshi.balance_usd()
                except Exception as e:
                    self.last_error = f"balance: {e}"
            for addr, w in self.whales.items():
                try:
                    self.poll_wallet(addr, w)
                except Exception as e:  # keep running on transient errors
                    self.last_error = f"{w.get('name', addr[:8])}: {e}"
                    log.warning("poll %s failed: %s", w.get("name", addr[:8]), e)
            self.last_poll = time.time()
            self.state.save()
            if once:
                return
            wait = self.cfg.get("poll_seconds", 15)
            if stop_event is not None:
                if stop_event.wait(wait):
                    return
            else:
                time.sleep(wait)

    def poll_wallet(self, addr: str, whale: dict) -> None:
        trades = self.pm.trades(addr, limit=50)
        trades.sort(key=lambda t: int(t.get("timestamp", 0)))
        first_run = addr not in self.state.data["initialized"]
        for t in trades:
            key = f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("side")}:{t.get("size")}'
            if self.state.is_seen(addr, key):
                continue
            self.state.mark_seen(addr, key)
            if first_run:
                continue  # don't copy history on first start
            if self.paused:
                log.info("[%s] paused - not copying %s '%s'", whale.get("name", addr[:8]),
                         t.get("side"), t.get("title"))
                continue  # trades during a pause are skipped, not queued
            self.handle_trade(addr, whale, t)
        if first_run:
            self.state.data["initialized"].append(addr)
            log.info("Baseline set for %s (%d past trades ignored)",
                     whale.get("name", addr[:8]), len(trades))

    # ---- copy logic ------------------------------------------------------ #
    def handle_trade(self, addr: str, whale: dict, t: dict) -> None:
        name = whale.get("name", addr[:8])
        side = str(t.get("side", "")).upper()
        size = float(t.get("size", 0) or 0)
        price = float(t.get("price", 0) or 0)
        usd = size * price
        title = t.get("title", t.get("slug", "?"))
        outcome = t.get("outcome", "?")
        age = time.time() - int(t.get("timestamp", time.time()))

        log.info("[%s] %s %s '%s' %.0f sh @ %.3f ($%.0f)",
                 name, side, outcome, title, size, price, usd)

        if age > self.risk["max_trade_age_seconds"]:
            return self._skip(t, name, f"stale ({age:.0f}s old)")
        tgt = self.maps.lookup(t)
        if not tgt and self.auto_sports:
            found, reason = self.sports.lookup(t)
            if found:
                tgt = Target(found["ticker"], found["side"])
                log.info("  auto-matched -> %s %s", tgt.ticker, tgt.side.upper())
            else:
                return self._skip(t, name, reason)
        if not tgt:
            return self._skip(t, name, "no Kalshi mapping")

        if side == "BUY":
            min_usd = whale.get("min_trade_usd", self.risk["min_whale_trade_usd"])
            if usd < min_usd:
                return self._skip(t, name, f"below min ${min_usd}")
            self._copy_buy(name, whale, t, tgt, price, usd)
        elif side == "SELL":
            self._copy_sell(addr, name, t, tgt, price, size)

    def _copy_buy(self, name, whale, t, tgt: Target, whale_px, whale_usd) -> None:
        r = self.risk
        m = self.kdata.market(tgt.ticker)
        if m.get("status") not in (None, "open", "active"):
            return self._skip(t, name, f"Kalshi market {m.get('status')}")
        ask = _px(m, f"{tgt.side}_ask")
        if ask is None or ask <= 0 or ask >= 1:
            return self._skip(t, name, "no ask on Kalshi")
        limit = whale_px + r["max_slippage"]
        if ask > limit:
            return self._skip(t, name, f"Kalshi ask {ask:.3f} > whale {whale_px:.3f} + slippage")
        if ask > r["max_entry_price"]:
            return self._skip(t, name, f"price {ask:.3f} above max_entry_price")

        # sizing
        if r["sizing"] == "fixed":
            budget = r["fixed_usd"]
        else:  # proportional
            budget = whale_usd * whale.get("copy_ratio", r["copy_ratio"])
        budget = min(budget, r["max_usd_per_trade"])
        budget = min(budget, r["max_usd_per_day"] - self.state.spent_today())
        held_usd = self.state.position(tgt.ticker, tgt.side) * ask
        budget = min(budget, r["max_usd_per_market"] - held_usd)
        count = math.floor(budget / ask) if budget > 0 else 0
        if count < 1:
            return self._skip(t, name, "risk limits leave no room")

        filled, cost = self._execute(tgt, "buy", count, ask)
        if filled:
            self.state.add_position(tgt.ticker, tgt.side, filled)
            self.state.add_spent(cost)
            self._journal(name, t, tgt, "buy", filled, cost / filled)

    def _copy_sell(self, addr, name, t, tgt: Target, whale_px, sold) -> None:
        held = self.state.position(tgt.ticker, tgt.side)
        if held <= 0:
            return self._skip(t, name, "whale sold but we hold nothing")
        try:
            remaining = self.pm.position_size(addr, t.get("conditionId", ""), t.get("outcome", ""))
        except Exception:
            remaining = 0.0
        frac = 1.0 if remaining <= 0 else min(1.0, sold / (sold + remaining))
        count = max(1, math.floor(held * frac))
        m = self.kdata.market(tgt.ticker)
        bid = _px(m, f"{tgt.side}_bid")
        if not bid:
            return self._skip(t, name, "no bid on Kalshi to sell into")
        floor = max(0.01, whale_px - self.risk["max_slippage"])
        if bid < floor and not self.risk.get("always_follow_exits", True):
            return self._skip(t, name, f"bid {bid:.3f} below floor {floor:.3f}")
        filled, proceeds = self._execute(tgt, "sell", count, bid)
        if filled:
            self.state.add_position(tgt.ticker, tgt.side, -filled)
            self._journal(name, t, tgt, "sell", filled, proceeds / filled)

    def _execute(self, tgt: Target, action: str, count: int, px: float) -> tuple[int, float]:
        if self.mode != "live":
            log.info("  PAPER %s %d %s %s @ %.3f", action.upper(), count,
                     tgt.ticker, tgt.side.upper(), px)
            return count, count * px
        try:
            o = self.kalshi.create_order(tgt.ticker, tgt.side, action, count, px,
                                         self.risk.get("time_in_force", "fill_or_kill"))
        except Exception as e:
            log.error("  ORDER FAILED: %s", e)
            return 0, 0.0
        filled = int(float(o.get("fill_count_fp") or o.get("fill_count") or 0))
        log.info("  LIVE %s %s -> status=%s filled=%d/%d", action.upper(),
                 tgt.ticker, o.get("status"), filled, count)
        return filled, filled * px

    # ---- dashboard helpers ----------------------------------------------- #
    def recent_journal(self, n: int = 50) -> list[dict]:
        if not self.journal.exists():
            return []
        with self.journal.open(newline="") as f:
            rows = list(csv.DictReader(f))
        return rows[-n:][::-1]

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "kalshi_env": self.kalshi_env,
            "paused": self.paused,
            "whales": [{"name": w.get("name"), "address": a} for a, w in self.whales.items()],
            "mapped_markets": len(self.maps),
            "positions": {k: v for k, v in self.state.data["positions"].items() if v},
            "spent_today": self.state.spent_today(),
            "max_usd_per_day": self.risk["max_usd_per_day"],
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "balance": self.balance,
        }

    # ---- logging --------------------------------------------------------- #
    def _skip(self, t, name, reason) -> None:
        log.info("  skip: %s", reason)

    def _journal(self, name, t, tgt, action, count, px) -> None:
        new = not self.journal.exists()
        with self.journal.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time_utc", "mode", "whale", "pm_title", "pm_outcome",
                            "pm_price", "kalshi_ticker", "side", "action", "contracts", "price"])
            w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), self.mode,
                        name, t.get("title"), t.get("outcome"), t.get("price"),
                        tgt.ticker, tgt.side, action, count, f"{px:.4f}"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--once", action="store_true", help="one polling pass then exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    CopyBot(load_yaml(a.config)).run(once=a.once)


if __name__ == "__main__":
    main()
