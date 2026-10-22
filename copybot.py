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
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    """Persists seen trades, positions, the trade ledger and daily spend between restarts.

    Stored in Postgres when DATABASE_URL is set (survives restarts on Render's free
    plan), otherwise in a local JSON file.
    """

    DEFAULT = {"seen": {}, "positions": {}, "spent_by_day": {}, "initialized": [],
               "lots": [], "closed": [], "events": []}

    DISK = Path("/var/data")  # Render persistent disk (survives restarts and deploys)

    def __init__(self, path: str):
        self.path = (self.DISK / "state.json") if self.DISK.is_dir() else Path(path)
        self.on_disk = self.DISK.is_dir()
        self.db_url = os.environ.get("DATABASE_URL")
        self.data = json.loads(json.dumps(self.DEFAULT))
        loaded = json.loads(self.path.read_text()) if self.path.exists() else None
        db_data = None
        if self.db_url:
            try:
                import psycopg
                with psycopg.connect(self.db_url, connect_timeout=10) as c:
                    c.execute("create table if not exists copybot_state "
                              "(id text primary key, data jsonb not null, updated timestamptz default now())")
                    row = c.execute("select data from copybot_state where id='main'").fetchone()
                    db_data = row[0] if row else None
                log.info("State: database reachable (%s)", "has records" if db_data else "empty")
            except Exception as e:
                log.error("State database unavailable: %s", e)
        if loaded is None:
            loaded = db_data
            log.info("State: %s", "loaded from database" if db_data else "starting empty")
        elif db_data and not loaded.get("db_merged_v1"):
            loaded = self._merge(db_data, loaded)
            log.warning("State: merged the database's full records with the records made since the outage")
        if loaded:
            self.data.update(loaded)
        if self.on_disk:
            self.data["db_merged_v1"] = bool(db_data) or self.data.get("db_merged_v1", False)
            log.info("State: saving to disk %s", self.path)

    @staticmethod
    def _merge(old: dict, new: dict) -> dict:
        """old = full history from the database; new = records made after it went offline."""
        out = json.loads(json.dumps(old))
        have = {(l["ticker"], l["side"]) for l in out.get("lots", [])}
        for l in new.get("lots", []):
            if (l["ticker"], l["side"]) not in have:
                out.setdefault("lots", []).append(l)
        for k in ("closed", "events"):
            seen = {json.dumps(x, sort_keys=True) for x in out.get(k, [])}
            out[k] = out.get(k, []) + [x for x in new.get(k, []) if json.dumps(x, sort_keys=True) not in seen]
        pos = dict(out.get("positions", {}))
        for key, n in (new.get("positions") or {}).items():
            pos[key] = max(pos.get(key, 0), n)
        out["positions"] = pos
        for w, lst in (new.get("seen") or {}).items():
            out.setdefault("seen", {}).setdefault(w, [])
            out["seen"][w] = (out["seen"][w] + [x for x in lst if x not in out["seen"][w]])[-2000:]
        out["db_merged_v1"] = True
        return out

    def save(self) -> None:
        blob = json.dumps(self.data)
        if not self.on_disk and self.db_url:
            try:
                import psycopg
                with psycopg.connect(self.db_url, connect_timeout=10) as c:
                    c.execute("insert into copybot_state (id, data, updated) values ('main', %s, now()) "
                              "on conflict (id) do update set data = excluded.data, updated = now()", (blob,))
                return
            except Exception as e:
                log.warning("State save to database failed (%s); writing local file", e)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(blob)
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
        if not self.data["positions"][k]:
            del self.data["positions"][k]

    def total_exposure_contracts(self) -> int:
        return sum(self.data["positions"].values())

    def spent_today(self) -> float:
        return float(self.data["spent_by_day"].get(_today(), 0.0))

    def add_spent(self, usd: float) -> None:
        d = _today()
        self.data["spent_by_day"][d] = round(self.spent_today() + usd, 4)
        for k in sorted(self.data["spent_by_day"])[:-14]:
            del self.data["spent_by_day"][k]

    # ---- ledger: which whale's copy made or lost what --------------------- #
    def open_lot(self, whale: str, ticker: str, side: str, count: int, price: float,
                 fee: float, title: str) -> None:
        self.data["lots"].append({"whale": whale, "ticker": ticker, "side": side, "count": count,
                                  "cost": round(count * price, 4), "fee": fee, "title": title,
                                  "opened": int(time.time())})

    def _close(self, lot: dict, count: int, proceeds: float, fee: float, how: str,
               result: str | None = None) -> None:
        frac = count / lot["count"]
        cost, ofee = lot["cost"] * frac, lot["fee"] * frac
        pnl = proceeds - cost - ofee - fee
        self.data["closed"].append({
            "whale": lot["whale"], "ticker": lot["ticker"], "side": lot["side"], "title": lot["title"],
            "count": count, "cost": round(cost, 4), "proceeds": round(proceeds, 4),
            "fees": round(ofee + fee, 4), "pnl": round(pnl, 4), "how": how, "result": result,
            "opened": lot["opened"], "closed": int(time.time()), "score": lot.get("score")})
        del self.data["closed"][:-3000]
        lot["count"] -= count
        lot["cost"] = round(lot["cost"] - cost, 4)
        lot["fee"] = round(lot["fee"] - ofee, 4)
        if lot.get("pot"):  # future-parlay pot: money comes back to the pot, profit above its size to main
            pot = self.data.setdefault("pot", {"usd": 0.0, "cash": 0.0})
            pot["cash"] = round(float(pot.get("cash", 0)) + proceeds - fee, 4)
            open_pot = sum(l["cost"] + l.get("fee", 0) for l in self.data["lots"] if l.get("pot") and l["count"] > 0)
            if pot["cash"] + open_pot > float(pot.get("usd", 0)):
                pot["cash"] = round(max(0.0, float(pot.get("usd", 0)) - open_pot), 4)

    def sell_lots(self, ticker: str, side: str, count: int, price: float, fee: float, how: str = "sold") -> None:
        """FIFO-close lots when we copy a whale's exit."""
        left, total = count, count
        for lot in [l for l in self.data["lots"] if l["ticker"] == ticker and l["side"] == side]:
            if left <= 0:
                break
            n = min(left, lot["count"])
            self._close(lot, n, n * price, fee * n / total, how)
            left -= n
        self.data["lots"] = [l for l in self.data["lots"] if l["count"] > 0]

    def settle(self, ticker: str, result: str) -> int:
        """Close every lot on a market Kalshi has settled. Returns lots closed."""
        n = 0
        for lot in [l for l in self.data["lots"] if l["ticker"] == ticker]:
            if result in ("yes", "no"):
                payout = lot["count"] * (1.0 if lot["side"] == result else 0.0)
            else:  # voided / cancelled: money back
                payout = lot["cost"]
            self._close(lot, lot["count"], payout, 0.0, "settled", result)
            self.data["positions"].pop(f"{ticker}|{lot['side']}", None)
            n += 1
        self.data["lots"] = [l for l in self.data["lots"] if l["count"] > 0]
        return n

    def scoreboard(self) -> list[dict]:
        board: dict[str, dict] = {}
        for c in self.data["closed"]:
            b = board.setdefault(c["whale"], {"whale": c["whale"], "closed": 0, "wins": 0, "pnl": 0.0,
                                              "staked": 0.0, "fees": 0.0, "open_lots": 0, "open_cost": 0.0})
            b["closed"] += 1
            b["wins"] += 1 if c["pnl"] > 0 else 0
            b["pnl"] += c["pnl"]
            b["staked"] += c["cost"]
            b["fees"] += c["fees"]
        for l in self.data["lots"]:
            b = board.setdefault(l["whale"], {"whale": l["whale"], "closed": 0, "wins": 0, "pnl": 0.0,
                                              "staked": 0.0, "fees": 0.0, "open_lots": 0, "open_cost": 0.0})
            b["open_lots"] += 1
            b["open_cost"] += l["cost"]
        out = []
        for b in board.values():
            b["win_rate"] = round(100 * b["wins"] / b["closed"]) if b["closed"] else None
            b["roi"] = round(100 * b["pnl"] / b["staked"], 1) if b["staked"] else None
            for k in ("pnl", "staked", "fees", "open_cost"):
                b[k] = round(b[k], 2)
            out.append(b)
        return sorted(out, key=lambda x: -x["pnl"])


LOCAL_TZ = "America/Chicago"


def _local_day(iso_utc: str) -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d")
    except Exception:
        return ""  # the bot's "day" (daily limits, skips, combo budget) follows Central time


def _today() -> str:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# Polymarket (read-only, public)
# --------------------------------------------------------------------------- #
class Polymarket:
    def __init__(self, session: requests.Session):
        self.s = session

    def trades(self, wallet: str, limit: int = 50) -> list[dict]:
        for attempt in range(3):  # Polymarket rate limit: wait and retry
            r = self.s.get(f"{POLY_DATA}/trades", params={"user": wallet, "limit": limit}, timeout=15)
            if r.status_code != 429:
                break
            time.sleep(1.5 * (attempt + 1))
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def open_positions(self, wallet: str) -> list[dict]:
        r = self.s.get(f"{POLY_DATA}/positions", params={"user": wallet, "sizeThreshold": 1, "limit": 500},
                       timeout=20)
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else d.get("data", [])

    _info_cache: dict = {}

    def market_info(self, condition_id: str) -> dict:
        """Game start time and current prices per outcome from Polymarket (cached 5 min)."""
        hit = self._info_cache.get(condition_id)
        if hit and time.time() - hit[0] < 300:
            return hit[1]
        info: dict = {"start": None, "prices": {}, "tokens": {}}
        try:
            r = self.s.get("https://gamma-api.polymarket.com/markets",
                           params={"condition_ids": condition_id}, timeout=15)
            r.raise_for_status()
            ms = r.json()
            m = ms[0] if isinstance(ms, list) and ms else (ms if isinstance(ms, dict) else {})
            ev = (m.get("events") or [{}])[0] if isinstance(m.get("events"), list) else {}
            for v in (m.get("gameStartTime"), m.get("game_start_time"), ev.get("startTime"),
                      ev.get("gameStartTime"), m.get("eventStartTime")):
                if v:
                    txt = str(v).strip().replace(" ", "T").replace("Z", "+00:00")
                    if re.search(r"[+-]\d\d$", txt):
                        txt += ":00"
                    try:
                        info["start"] = datetime.fromisoformat(txt).timestamp()
                        break
                    except ValueError:
                        continue
            outs, prices = m.get("outcomes"), m.get("outcomePrices")
            outs = json.loads(outs) if isinstance(outs, str) else (outs or [])
            prices = json.loads(prices) if isinstance(prices, str) else (prices or [])
            info["prices"] = {str(o): float(p) for o, p in zip(outs, prices)}
            toks = m.get("clobTokenIds")
            toks = json.loads(toks) if isinstance(toks, str) else (toks or [])
            info["tokens"] = {str(o): str(tk) for o, tk in zip(outs, toks)}
        except Exception as e:
            log.debug("gamma lookup failed: %s", e)
        if len(self._info_cache) > 3000:  # keep the cache bounded
            for k in sorted(self._info_cache, key=lambda k: self._info_cache[k][0])[:1000]:
                del self._info_cache[k]
        self._info_cache[condition_id] = (time.time(), info)
        return info

    def game_start(self, condition_id: str) -> float | None:
        return self.market_info(condition_id)["start"]

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
        self.key_source = None
        if key_pem:  # key pasted into an env var (literal "\n" allowed)
            pem = key_pem.replace("\\n", "\n").encode()
            self.key_source = "KALSHI_PRIVATE_KEY variable"
        elif key_path and os.path.exists(os.path.expanduser(key_path)):
            with open(os.path.expanduser(key_path), "rb") as f:
                pem = f.read()
            self.key_source = key_path
        elif key_id:
            pem, self.key_source = self._find_key_file()
        if key_id and pem:
            pem = self._normalize_pem(pem)
            from cryptography.hazmat.primitives import serialization
            try:
                self.pkey = serialization.load_pem_private_key(pem.strip() + b"\n", password=None)
            except Exception as e:
                log.error("Kalshi private key could not be read (%s): %s", self.key_source, e)
                self.pkey = None

    @staticmethod
    def _normalize_pem(pem: bytes) -> bytes:
        """Repair keys whose line breaks were lost when pasted (common from phones)."""
        text = pem.decode("utf-8", "ignore").replace("\\n", "\n").replace("\r", "")
        m = re.search(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----(.*?)-----END \1-----", text, flags=re.S)
        if not m:
            return pem
        body = re.sub(r"\s+", "", m.group(2))
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        return ("-----BEGIN %s-----\n%s\n-----END %s-----\n" % (m.group(1), "\n".join(lines), m.group(1))).encode()

    @staticmethod
    def _find_key_file() -> tuple[bytes | None, str | None]:
        """Look in Render's secret-file locations for any file that contains a private key."""
        seen = []
        for folder in ("/etc/secrets", os.getcwd()):
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            if folder == "/etc/secrets":
                seen = names
            for n in names:
                p = os.path.join(folder, n)
                try:
                    if os.path.isfile(p) and os.path.getsize(p) < 20000:
                        data = open(p, "rb").read()
                        if b"PRIVATE KEY" in data:
                            log.info("Kalshi key found in secret file '%s'", n)
                            return data, p
                except OSError:
                    continue
        for name, val in os.environ.items():  # pasted into an env var under another name?
            if "PRIVATE KEY" in (val or ""):
                log.info("Kalshi key found in environment variable '%s'", name)
                return val.encode(), f"env {name}"
        log.warning("No private key found. Files in /etc/secrets: %s", seen or "(none)")
        return None, None

    def _headers(self, method: str, path: str) -> dict:
        if not self.pkey:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + self.prefix + path.split("?")[0]).encode()
        if isinstance(self.pkey, Ed25519PrivateKey):  # Kalshi's recommended key type
            sig = self.pkey.sign(msg)
        else:  # RSA: PSS + SHA-256, salt = digest length
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
        for attempt in range(4):  # back off on Kalshi rate limits (429) instead of failing the step
            r = self.s.request(method, self.base + path,
                               headers=self._headers(method, path), timeout=15, **kw)
            if r.status_code != 429 or attempt == 3:
                break
            time.sleep(0.6 * (attempt + 1))
        if r.status_code >= 400:
            raise RuntimeError(f"Kalshi {method} {path} -> {r.status_code}: {r.text[:300]}")
        if r.status_code == 204 or not r.content.strip():
            return {}  # e.g. accepting an RFQ quote returns 204 with no body
        return r.json()

    def market(self, ticker: str) -> dict:
        return self._req("GET", f"/markets/{ticker}")["market"]

    def balance_usd(self) -> float:
        # Without exchange_index, Kalshi's balance covers every exchange shard.
        return self._req("GET", "/portfolio/balance").get("balance", 0) / 100.0

    # ---- exchange shards ---------------------------------------------------- #
    # Kalshi splits markets across exchange shards, each with its own cash. Sports like
    # basketball/tennis/baseball live on shard 3, combos on shard 1. API orders don't move
    # money between shards, so the bot moves just enough cash before each buy.
    _SHARD_GUESS = (("KXMVE", 1), ("KXNBA", 3), ("KXWNBA", 3), ("KXNCAAMB", 3), ("KXNCAAWB", 3),
                    ("KXMLB", 3), ("KXATP", 3), ("KXWTA", 3), ("KXTENNIS", 3))

    def shard_balances(self) -> dict[int, float]:
        j = self._req("GET", "/portfolio/balance")
        out = {int(b.get("exchange_index") or 0): float(b.get("balance") or 0)
               for b in j.get("balance_breakdown") or []}
        if not out:
            out = {0: (j.get("balance") or 0) / 100.0}
        return out

    def market_shard(self, ticker: str) -> int:
        cache = self.__dict__.setdefault("_shard_cache", {})
        if ticker in cache:
            return cache[ticker]
        idx = None
        try:
            v = self.market(ticker).get("exchange_index")
            idx = int(v) if v not in (None, "") else None
        except Exception:
            pass
        if idx is None:
            idx = next((s for p, s in self._SHARD_GUESS if ticker.upper().startswith(p)), 0)
        cache[ticker] = idx
        return idx

    def ensure_shard_funds(self, ticker: str, need_usd: float, wait_s: float = 12) -> bool:
        """Move cash to the market's shard if it has less than need_usd. True = funded."""
        idx = self.market_shard(ticker)
        bals = self.shard_balances()
        have = bals.get(idx, 0.0)
        if have + 1e-6 >= need_usd:
            return True
        short = need_usd - have + 0.25  # small buffer for rounding / fees
        for src, avail in sorted(bals.items(), key=lambda kv: -kv[1]):
            if src == idx or avail <= 0.01 or short <= 0:
                continue
            amt = min(short, avail)
            self._req("POST", "/portfolio/intra_exchange_instance_transfer", json={
                "client_transfer_id": str(uuid.uuid4()),
                "source": "event_contract", "destination": "event_contract",
                "amount": int(math.floor(amt * 10000)),  # centicents
                "source_exchange_shard": src, "destination_exchange_shard": idx})
            log.info("  moved $%.2f from shard %d to shard %d for %s", amt, src, idx, ticker)
            short -= amt
        deadline = time.time() + wait_s  # transfers are processed asynchronously
        while time.time() < deadline:
            time.sleep(1.5)
            if self.shard_balances().get(idx, 0.0) + 1e-6 >= need_usd:
                return True
        return False

    def create_order(self, ticker: str, side: str, action: str, count: int,
                     price: float, tif: str | None, post_only: bool = False,
                     expiration_ts: int | None = None) -> dict:
        """Kalshi Create Order V2 (POST /portfolio/events/orders).

        V2 orders are always on the YES leg: 'bid' buys YES, 'ask' sells YES. So buying NO at q
        is an ask at (1 - q), and selling NO at q is a bid at (1 - q).
        """
        if side == "yes":
            v2_side, v2_price = ("bid" if action == "buy" else "ask"), price
        else:
            v2_side, v2_price = ("ask" if action == "buy" else "bid"), 1.0 - price
        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": v2_side,
            "count": f"{int(count)}",
            "price": f"{v2_price:.4f}",
            "time_in_force": tif or "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
        }
        if action == "sell":
            body["time_in_force"] = "immediate_or_cancel"
            body["reduce_only"] = True  # V2 allows reduce_only only with IOC
        if post_only:
            body["post_only"] = True
        if expiration_ts and body["time_in_force"] == "good_till_canceled":
            body["expiration_time"] = int(expiration_ts)
        need = 0.0
        if action == "buy":
            need = count * (price + 0.07 * price * (1 - price)) + 0.02
            try:
                self.ensure_shard_funds(ticker, need)
            except Exception as e:
                log.warning("  shard funding check failed for %s: %s", ticker, e)
        try:
            res = self._req("POST", "/portfolio/events/orders", json=body)
        except RuntimeError as e:
            msg = str(e).lower()
            if action != "buy" or ("shard" not in msg and "insufficient" not in msg):
                raise
            # Cash sits in several Kalshi pools: pull it into this market's pool and retry,
            # buying fewer contracts if there still isn't enough for the full size.
            self.__dict__.setdefault("_shard_cache", {}).pop(ticker, None)
            try:
                self.ensure_shard_funds(ticker, need + 0.5, wait_s=20)
            except Exception as ee:
                log.warning("  shard top-up failed: %s", ee)
            try:
                bals = self.shard_balances()
                avail = bals.get(self.market_shard(ticker), 0.0)
            except Exception:
                bals, avail = {}, need
            unit = price + 0.07 * price * (1 - price)
            fit = int(max(0.0, avail - 0.05) // unit) if unit > 0 else 0
            log.info("  retry %s: pools %s, room for %d of %d contracts", ticker, bals, fit, count)
            if fit < 1:
                raise
            body["count"] = f"{min(int(count), fit)}"
            body["client_order_id"] = str(uuid.uuid4())
            res = self._req("POST", "/portfolio/events/orders", json=body)
        filled = float(res.get("fill_count") or 0)
        remaining = float(res.get("remaining_count") or 0)
        avg = res.get("average_fill_price")
        try:  # V2 reports prices on the YES leg: turn it into the price of the side we traded
            avg = float(avg) if avg not in (None, "") else None
            if avg is not None and avg > 1:
                avg /= 100
            if avg is not None and side == "no":
                avg = round(1 - avg, 4)
        except (TypeError, ValueError):
            avg = None
        status = "executed" if remaining <= 0 and filled > 0 else (
            "resting" if body["time_in_force"] == "good_till_canceled" and remaining > 0 else "canceled")
        return {"order_id": res.get("order_id"), "fill_count": filled, "remaining_count": remaining,
                "status": status, "average_fill_price": avg, "average_fill_price_yes_leg": res.get("average_fill_price"),
                "average_fee_paid": res.get("average_fee_paid"), "raw": res}

    def get_order(self, order_id: str) -> dict:
        try:
            return self._req("GET", f"/portfolio/orders/{order_id}")["order"]
        except Exception:
            # fallback: add up fills for this order
            j = self._req("GET", "/portfolio/fills", params={"order_id": order_id, "limit": 200})
            n = sum(float(f.get("count_fp") or f.get("count") or 0) for f in j.get("fills", []))
            return {"fill_count": n, "status": "unknown"}

    def cancel_order(self, order_id: str, ticker: str | None = None) -> None:
        """Cancel a resting order. Tries the V2 forms; logs each failure so the right one is known."""
        tries = [("DELETE", "/portfolio/events/orders/batched",
                  {"json": {"orders": [{"order_id": order_id, **({"ticker": ticker} if ticker else {})}]}}),
                 ("DELETE", f"/portfolio/events/orders/{order_id}", {"params": {"ticker": ticker}} if ticker else {}),
                 ("DELETE", f"/portfolio/events/orders/{order_id}", {}),
                 ("POST", f"/portfolio/events/orders/{order_id}/cancel", {"json": {"ticker": ticker} if ticker else {}}),
                 ("DELETE", "/portfolio/events/orders/batched", {"json": {"ids": [order_id]}}),
                 ("DELETE", "/portfolio/events/orders/batched", {"json": {"order_ids": [order_id]}}),
                 ("DELETE", "/portfolio/orders/batched", {"json": {"ids": [order_id]}}),
                 ("DELETE", f"/portfolio/orders/{order_id}", {})]
        tries = [t for t in tries if t[1] != f"/portfolio/orders/{order_id}"]  # v1 endpoint: gone (410)
        good = getattr(self, "_cancel_way", None)
        if good is not None:  # the form that worked last time goes first
            tries = [tries[good]] + [t for i, t in enumerate(tries) if i != good]
            idx = [good] + [i for i in range(len(tries)) if i != good]
        else:
            idx = list(range(len(tries)))
        errs = []
        for i, (method, path, kw) in zip(idx, tries):
            try:
                self._req(method, path, **kw)
            except Exception as e:
                errs.append(f"{method} {path}: {str(e)[:120]}")
                if "not found" in str(e).lower() or "404" in str(e):
                    return
                continue
            try:  # a 200 doesn't prove it: check the order is really gone
                st = self._req("GET", f"/portfolio/orders/{order_id}").get("order", {}).get("status")
            except Exception:
                st = None
            if st != "resting":
                if self.__dict__.get("_cancel_way") != i:
                    log.info("  cancel works via %s %s (status now %s)", method, path, st)
                self._cancel_way = i
                return
            errs.append(f"{method} {path}: returned OK but order still resting")
        raise RuntimeError(" | ".join(errs))

    def collections(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(10):
            p = {"status": "open", "limit": 200}
            if cursor:
                p["cursor"] = cursor
            j = self._req("GET", "/multivariate_event_collections", params=p)
            out += j.get("multivariate_contracts") or j.get("multivariate_event_collections") or j.get("collections") or []
            cursor = j.get("cursor")
            if not cursor:
                break
        return out

    # ---- RFQ (request for quote): how combo markets actually trade ---------- #
    def rfq_buy_yes(self, ticker: str, stake_usd: float, max_price: float, wait_s: float = 12) -> dict:
        """Ask market makers to price YES on `ticker` for about stake_usd. Accept the best quote if
        its YES price (1 - maker's no_bid) is <= max_price. Accepting the quote's NO side = we buy
        YES. Returns {status: filled|no_quote|too_expensive|error, price, count, best}."""
        need = stake_usd * 1.08 + 0.05
        try:
            self.ensure_shard_funds(ticker, need)
        except Exception as e:
            log.warning("  shard funding for RFQ %s failed: %s", ticker, e)
        rfq = self._req("POST", "/communications/rfqs", json={
            "market_ticker": ticker, "rest_remainder": False,
            "target_cost_dollars": f"{stake_usd:.2f}", "target_cost_excludes_fees": False})
        rid = rfq.get("id")
        out = {"status": "no_quote", "price": None, "count": 0, "best": None, "rfq": rid}
        try:
            deadline, best = time.time() + wait_s, None
            while time.time() < deadline:
                time.sleep(2.0)
                qs = self._req("GET", "/communications/quotes", params={"rfq_id": rid, "rfq_user_filter": "self", "limit": 100}).get("quotes") or []
                for q in qs:
                    if q.get("status") not in (None, "open"):
                        continue
                    nb = float(q.get("no_bid_dollars") or 0)
                    if nb <= 0:
                        continue
                    yes_px = round(1 - nb, 4)
                    if best is None or yes_px < best[0]:
                        best = (yes_px, q)
                if best and best[0] <= max_price:
                    break
            if not best:
                return out
            out["best"] = best[0]
            if best[0] > max_price:
                out["status"] = "too_expensive"
                return out
            q = best[1]
            self._req("PUT", f"/communications/rfqs/{rid}/quotes/{q['id']}/accept", json={"accepted_side": "no"})
            out["accepted"] = True
            # maker has ~3s to confirm, then ~1s to execute (combo markets are HVM)
            for _ in range(8):
                time.sleep(1.5)
                st = self._req("GET", "/communications/quotes", params={"rfq_id": rid, "rfq_user_filter": "self", "limit": 100}).get("quotes") or []
                me = next((x for x in st if x.get("id") == q["id"]), {})
                if me.get("status") == "executed":
                    break
                if me.get("status") == "cancelled":
                    out["status"] = "maker_cancelled"
                    return out
            fills = self._req("GET", "/portfolio/fills", params={"ticker": ticker, "limit": 50}).get("fills") or []
            fills = [f for f in fills if (f.get("side") == "yes" and f.get("action") == "buy")
                     and int(time.time()) - _ts(f.get("created_time")) < 300]
            n = sum(float(f.get("count_fp") or f.get("count") or 0) for f in fills)
            if n > 0:
                cost = sum(float(f.get("count_fp") or f.get("count") or 0) *
                           float(f.get("yes_price_dollars") or (float(f.get("yes_price") or 0) / 100)) for f in fills)
                out.update(status="filled", count=n, price=round(cost / n, 4))
            else:
                out["status"] = "accepted_no_fill_yet"
            return out
        except Exception as e:
            out.update(status="accepted_unknown" if out.get("accepted") else "error", error=str(e)[:200])
            return out
        finally:
            try:
                self._req("DELETE", f"/communications/rfqs/{rid}")
            except Exception:
                pass

    def held(self, ticker: str) -> float:
        j = self._req("GET", "/portfolio/positions", params={"ticker": ticker, "limit": 5})
        for p in j.get("market_positions") or []:
            if p.get("ticker") == ticker:
                return float(p.get("position_fp") or p.get("position") or 0)
        return 0.0

    def rfq_sell_yes(self, ticker: str, contracts: int, min_price: float, wait_s: float = 12) -> dict:
        """Ask market makers for a bid on `contracts` YES we hold; sell (accept the quote's YES
        bid) if it's >= min_price. Returns {status, price, sold}."""
        before = self.held(ticker)
        rfq = self._req("POST", "/communications/rfqs", json={
            "market_ticker": ticker, "rest_remainder": False, "contracts": int(contracts)})
        rid = rfq.get("id")
        out = {"status": "no_quote", "price": None, "sold": 0, "best": None}
        try:
            deadline, best = time.time() + wait_s, None
            while time.time() < deadline:
                time.sleep(2.0)
                qs = self._req("GET", "/communications/quotes",
                               params={"rfq_id": rid, "rfq_user_filter": "self", "limit": 100}).get("quotes") or []
                for q in qs:
                    yb = float(q.get("yes_bid_dollars") or 0)
                    if q.get("status") in (None, "open") and yb > 0 and (best is None or yb > best[0]):
                        best = (yb, q)
                if best and best[0] >= min_price:
                    break
            if not best:
                return out
            out["best"] = best[0]
            if best[0] < min_price:
                out["status"] = "too_low"
                return out
            self._req("PUT", f"/communications/rfqs/{rid}/quotes/{best[1]['id']}/accept", json={"accepted_side": "yes"})
            for _ in range(8):
                time.sleep(1.5)
                now = self.held(ticker)
                if now < before:
                    out.update(status="sold", price=best[0], sold=before - now)
                    return out
            out["status"] = "accepted_no_fill"
            return out
        except Exception as e:
            out.update(status="error", error=str(e)[:200])
            return out
        finally:
            try:
                self._req("DELETE", f"/communications/rfqs/{rid}")
            except Exception:
                pass

    def create_combo(self, collection_ticker: str, legs: list[dict]) -> dict:
        """legs: [{event_ticker, market_ticker, side}] -> {market_ticker, event_ticker} of the combo."""
        return self._req("POST", f"/multivariate_event_collections/{collection_ticker}",
                         json={"selected_markets": legs})




def _ts(v) -> int:
    """ISO time or epoch -> epoch seconds (0 if unknown)."""
    if v in (None, ""):
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        try:
            return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0


def kalshi_fee(count: int, price: float, rate: float = 0.07) -> float:
    """Estimated Kalshi taker fee: rate x contracts x P x (1-P), rounded up to the cent."""
    return math.ceil(rate * count * price * (1 - price) * 100 - 1e-9) / 100


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
        self.fee_rate = float(self.risk.get("fee_rate", 0.07))
        self.last_poll: float | None = None
        self.last_error: str | None = None
        self.balance: float | None = None
        self._last_settle_check = 0.0
        self._last_catch_up = 0.0
        # Switching paper <-> live: archive the old ledger so paper bets never mix with real money
        last_mode = self.state.data.get("ledger_mode")
        if last_mode is None and self.mode == "live" and (self.state.data.get("lots") or self.state.data.get("closed")
                                                         or self.state.data.get("events")):
            last_mode = "paper"  # data from before modes were recorded is paper data
        if last_mode and last_mode != self.mode:
            arch = self.state.data.setdefault("archive", {})
            arch[f"{last_mode}_{int(time.time())}"] = {
                k: self.state.data.get(k) for k in ("lots", "closed", "events", "positions", "spent_by_day",
                                                    "day_equity", "peak_equity", "catchup_done", "board_done")}
            for k, v in (("lots", []), ("closed", []), ("events", []), ("positions", {}), ("spent_by_day", {}),
                         ("day_equity", {}), ("catchup_done", {}), ("board_done", {}), ("resting", [])):
                self.state.data[k] = v
            self.state.data.pop("peak_equity", None)
            log.warning("Mode changed %s -> %s: previous ledger archived, starting clean", last_mode, self.mode)
        self.state.data["ledger_mode"] = self.mode
        # One-time cleanup: paper data left in a live ledger by an older version (no live bets yet)
        if (self.mode == "live" and not self.state.data.get("migr_paper_v1")
                and not any(e.get("mode") == "live" for e in self.state.data.get("events", []))
                and (self.state.data.get("lots") or self.state.data.get("events"))):
            arch = self.state.data.setdefault("archive", {})
            arch[f"paper_cleanup_{int(time.time())}"] = {
                k: self.state.data.get(k) for k in ("lots", "closed", "events", "positions", "spent_by_day",
                                                    "day_equity", "peak_equity", "catchup_done", "board_done")}
            for k, v in (("lots", []), ("closed", []), ("events", []), ("positions", {}), ("spent_by_day", {}),
                         ("day_equity", {}), ("catchup_done", {}), ("board_done", {}), ("resting", [])):
                self.state.data[k] = v
            self.state.data.pop("peak_equity", None)
            log.warning("Paper bets found in the live ledger: archived, starting clean")
        if self.mode == "live":
            self.state.data["migr_paper_v1"] = True
        for w in self.state.data.get("auto_whales", []):  # whales the bot added itself (screener)
            self.whales.setdefault(w["address"].lower(), w)
        self.whale_stats: dict = self.state.data.get("whale_stats", {})  # wallet -> screener stats
        self.screen_buys: list = []  # recent buys of other top whales (from the screener)
        self.bank = cfg.get("bankroll", {}) or {}
        self.board_queue: list = []
        self.combo_picks: list = []
        self.prop_picks: list = []      # rated player props (props.py), refreshed by the dashboard thread
        self.prop_picks_new = False
        self.ledger_queue: list = []   # ledger updates from other threads (lag trades), applied in the bot loop
        self.kalshi_link: dict = {"linked": False, "detail": "no API key set"}

    # ---- pause switch (persisted, toggled from the dashboard) ------------ #
    @property
    def paused(self) -> bool:
        return bool(self.state.data.get("paused", False))

    def set_paused(self, value: bool) -> None:
        self.state.data["paused"] = bool(value)
        self.state.save()
        log.warning("Bot %s from dashboard", "PAUSED" if value else "RESUMED")

    # ---- main loop ------------------------------------------------------- #
    def _net_ledger(self, tk: str) -> None:
        """Kalshi holds one net position per market: YES and NO lots in the same market cancel out and
        $1 per pair comes back right away. Mirror that in the records."""
        y, n = self.state.position(tk, "yes"), self.state.position(tk, "no")
        pairs = int(min(y, n))
        if pairs <= 0:
            return
        lots = self.state.data["lots"]
        def avg(side):
            ls = [l for l in lots if l["ticker"] == tk and l["side"] == side and l["count"] > 0]
            c = sum(l["count"] for l in ls)
            return (sum(l["cost"] for l in ls) / c) if c else 0.5
        ay, an = avg("yes"), avg("no")
        name = next((l["whale"] for l in lots if l["ticker"] == tk), "Pair")
        self.state.add_position(tk, "yes", -pairs)
        self.state.add_position(tk, "no", -pairs)
        g = (1 - ay - an) / 2  # pair profit split evenly between the two sides
        self.state.sell_lots(tk, "yes", pairs, ay + g, 0.0, "pair")
        self.state.sell_lots(tk, "no", pairs, an + g, 0.0, "pair")
        self._journal(name, {"title": f"pair x{pairs}", "outcome": "pair", "price": 1 - ay - an},
                      Target(tk, "yes"), f"pair done ({100 * (1 - ay - an):+.0f}c each)", pairs, ay + an)
        log.info("PAIR netted %s x%d: YES %.3f + NO %.3f -> %+.1fc each", tk, pairs, ay, an, 100 * (1 - ay - an))

    def _apply_ledger_queue(self) -> None:
        while self.ledger_queue:
            item = self.ledger_queue.pop(0)
            if item[0] == "net":  # (older queue entries) netting now happens on every open
                self._net_ledger(item[2])
                continue
            op, name, tk, sd, n, px, title = item
            fee = 0.0 if str(name).startswith("MM ") else kalshi_fee(n, px, self.fee_rate)  # maker fills: no fee
            tgt = Target(tk, sd)
            if op == "open":
                self.state.add_position(tk, sd, n)
                self.state.add_spent(n * px + fee)
                self.state.open_lot(name, tk, sd, n, px, fee, title)
                self._journal(name, {"title": title, "outcome": sd, "price": px}, tgt, "buy", n, px)
                self._net_ledger(tk)
            else:
                self.state.add_position(tk, sd, -n)
                self.state.sell_lots(tk, sd, n, px, fee)
                self._journal(name, {"title": title, "outcome": sd, "price": px}, tgt, "sell", n, px)

    def check_kalshi_link(self) -> None:
        """Log in to Kalshi with the API key (if set) and read the balance. Works in paper mode too."""
        if not self.kalshi.key_id:
            self.kalshi_link = {"linked": False, "detail": "no API key set"}
        elif not self.kalshi.pkey:
            self.kalshi_link = {"linked": False, "detail": "Key ID set but private key file missing or unreadable"}
        else:
            try:
                bal = self.kalshi.balance_usd()
                self.balance = bal if self.mode == "live" else self.balance
                self.kalshi_link = {"linked": True, "env": self.kalshi_env, "balance": round(bal, 2),
                                    "detail": f"connected to Kalshi {self.kalshi_env}"}
            except Exception as e:
                msg = str(e)
                hint = (" (demo keys need KALSHI_ENV=demo, real keys need prod)"
                        if "401" in msg or "403" in msg or "NOT_FOUND" in msg.upper() else "")
                self.kalshi_link = {"linked": False, "detail": f"login failed: {msg[:160]}{hint}"}
        log.info("KALSHI_LINK %s", self.kalshi_link)

    def run(self, once: bool = False, stop_event=None) -> None:
        log.info("mode=%s  kalshi_env=%s  whales=%d  mapped_markets=%d",
                 self.mode, self.kalshi_env, len(self.whales), len(self.maps))
        try:
            self.check_kalshi_link()
        except Exception as e:
            log.warning("kalshi link check failed: %s", e)
        if self.mode == "live" and self.kalshi_env == "prod":
            log.warning("*** LIVE TRADING WITH REAL MONEY ***")
        while True:
            self._apply_ledger_queue()
            if self.mode == "live":
                try:
                    old = self.balance
                    self.balance = self.kalshi.balance_usd()
                    self._unsynced = 0.0
                    if old is None or abs(self.balance - old) >= 5:
                        log.info("BALANCE cash $%.2f (was %s): main free $%.2f, reserve ready $%.2f, "
                                 "future pot free $%.2f, equity $%.2f", self.balance,
                                 "?" if old is None else f"${old:.2f}", max(0.0, self.spendable_cash()),
                                 self.reserve_free(), self.pot_free(), self.equity())
                except Exception as e:
                    self.last_error = f"balance: {e}"
            for addr, w in self.whales.items():
                try:
                    self.poll_wallet(addr, w)
                except Exception as e:  # keep running on transient errors
                    self.last_error = f"{w.get('name', addr[:8])}: {e}"
                    log.warning("poll %s failed: %s", w.get("name", addr[:8]), e)
            try:
                self.process_board_queue()
            except Exception as e:
                log.warning("board picks failed: %s", e)
            if self.combo_picks or self.prop_picks_new:
                picks, self.combo_picks, self.prop_picks_new = self.combo_picks, [], False
                try:
                    self.weekly_combo(picks)
                except Exception as e:
                    log.warning("weekly combo failed: %s", e)
            if time.time() - getattr(self, "_last_resting", 0) > self.cfg.get("limit_orders", {}).get("check_seconds", 30):
                self._last_resting = time.time()
                try:
                    self.check_resting()
                except Exception as e:
                    log.warning("resting check failed: %s", e)
            if time.time() - getattr(self, "_last_lockcheck", 0) > float(
                    (self.cfg.get("parlay_lock", {}) or {}).get("check_seconds", 120)):
                self._last_lockcheck = time.time()
                try:
                    self.check_parlay_locks()
                except Exception as e:
                    log.warning("parlay lock check failed: %s", e)
            if time.time() - getattr(self, "_last_reconcile", 0) > 300:
                self._last_reconcile = time.time()
                try:
                    self.reconcile_combos()
                except Exception as e:
                    log.warning("combo reconcile failed: %s", e)
            if time.time() - self._last_catch_up > self.cfg.get("catch_up", {}).get("every_seconds", 900):
                self._last_catch_up = time.time()
                try:
                    self.catch_up()
                except Exception as e:
                    log.warning("catch-up failed: %s", e)
            self.last_poll = time.time()
            if time.time() - self._last_settle_check > self.cfg.get("settle_check_seconds", 600):
                self._last_settle_check = time.time()
                try:
                    self.check_settlements()
                except Exception as e:
                    log.warning("settlement check failed: %s", e)
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
            self._remember_buy(addr, whale.get("name", addr[:8]), t)
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

    # ---- consensus + bankroll helpers -------------------------------------- #
    def _remember_buy(self, addr: str, name: str, t: dict) -> None:
        if str(t.get("side", "")).upper() != "BUY":
            return
        rb = self.state.data.setdefault("recent_buys", [])
        rb.append({"w": addr, "name": name, "cond": t.get("conditionId"), "out": t.get("outcome"),
                   "usd": round(float(t.get("size") or 0) * float(t.get("price") or 0), 2),
                   "px": float(t.get("price") or 0), "ts": int(t.get("timestamp") or 0),
                   "slug": t.get("slug"), "eventSlug": t.get("eventSlug"), "title": t.get("title"),
                   "outcomeIndex": t.get("outcomeIndex")})
        cutoff = time.time() - 48 * 3600
        self.state.data["recent_buys"] = [b for b in rb if b["ts"] >= cutoff][-4000:]

    def consensus(self, addr: str, t: dict) -> tuple[int, int]:
        """(# other whales on the same side, # on the other side) in the last 48h."""
        cond, out = t.get("conditionId"), t.get("outcome")
        same, opp = set(), set()
        for b in self.state.data.get("recent_buys", []) + self.screen_buys:
            if b.get("cond") != cond or b.get("w") == addr:
                continue
            (same if b.get("out") == out else opp).add(b.get("w"))
        return len(same), len(opp - same)

    def open_cost(self) -> float:
        lots = sum(l["cost"] + l.get("fee", 0) for l in self.state.data["lots"])
        resting = sum(r["remaining"] * r["price"] for r in self.state.data.get("resting", []))
        return lots + resting


    def equity(self) -> float:
        """Paper: starting bankroll + settled P&L. Live: Kalshi cash + money in open bets."""
        if self.mode == "live" and self.balance is not None:
            # Kalshi's balance still includes money sitting in waiting orders: add only bets that filled
            lots = sum(l["cost"] + l.get("fee", 0) for l in self.state.data["lots"])
            return self.balance - getattr(self, "_unsynced", 0.0) + lots
        start = float(self.bank.get("start_usd", 100))
        return start + sum(c["pnl"] for c in self.state.data["closed"])

    def cash(self) -> float:
        if self.mode == "live" and self.balance is not None:
            waiting = sum(r["remaining"] * r["price"] for r in self.state.data.get("resting", []))
            mm = getattr(self, "mm", None)
            waiting += mm.waiting_usd() if mm else 0.0  # market-maker quotes waiting on Kalshi
            # minus fills since the last balance read, minus money promised to waiting orders
            return self.balance - getattr(self, "_unsynced", 0.0) - waiting
        return self.equity() - self.open_cost()

    # ---- future-parlay pot (bets settling 3+ days out live only here) ---------- #
    def pot_state(self) -> dict:
        usd = max(0.0, float(self.bank.get("future_pot_usd", 0) or 0))
        st = self.state.data.setdefault("pot", {"usd": 0.0, "cash": 0.0})
        if abs(float(st.get("usd", 0)) - usd) > 1e-9:  # pot size changed in settings: add/remove the difference
            st["cash"] = round(max(0.0, float(st.get("cash", 0)) + usd - float(st.get("usd", 0))), 4)
            st["usd"] = usd
        return st

    def pot_open(self) -> float:
        return sum(l["cost"] + l.get("fee", 0) for l in self.state.data["lots"] if l.get("pot"))

    def pot_waiting(self) -> float:
        return sum(r["remaining"] * r["price"] for r in self.state.data.get("resting", []) if r.get("pot"))

    def pot_cash(self) -> float:
        return float(self.pot_state().get("cash", 0)) if self.pot_state()["usd"] > 0 else 0.0

    def pot_value(self) -> float:
        return self.pot_cash() + self.pot_open()

    def pot_free(self) -> float:
        return round(max(0.0, min(self.pot_cash() - self.pot_waiting(), self.cash() - 0.5)), 2)

    def _pot_take(self, lot: dict) -> None:
        """Mark a just-opened bet as paid from the pot."""
        lot["pot"] = True
        st = self.pot_state()
        st["cash"] = round(max(0.0, float(st.get("cash", 0)) - lot["cost"] - lot.get("fee", 0)), 4)

    # ---- when a bet settles, and how much edge that wait needs ---------------- #
    def settle_ts(self, ticker: str, m: dict | None = None) -> float | None:
        cache = self.__dict__.setdefault("_settle_cache", {})
        if ticker in cache:
            return cache[ticker]
        try:
            m = m or self.kdata.market(ticker)
            v = m.get("expected_expiration_time") or m.get("close_time")
            ts = datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() if v else None
        except Exception:
            ts = None
        if ts:
            cache[ticker] = ts
        return ts

    def horizon(self) -> dict:
        return self.bank.get("horizon", {}) or {}

    def days_out(self, settle: float | None) -> float:
        return max(0.0, (settle - time.time()) / 86400) if settle else 0.0

    def extra_edge(self, days: float) -> float:
        """Money locked longer must earn more: +1% required edge per day until settlement (max +4%)."""
        h = self.horizon()
        return float(h.get("edge_per_day", 0.01)) * min(float(h.get("max_days", 4)), days)

    def is_far(self, days: float) -> bool:
        return days >= float(self.horizon().get("far_days", 3))

    @staticmethod
    def game_of(ticker: str) -> str:
        parts = ticker.split("-")
        return parts[1] if len(parts) > 1 else ticker

    def game_exposure(self, game: str) -> float:
        tot = 0.0
        for l in self.state.data["lots"]:
            if self.game_of(l["ticker"]) == game or game in (l.get("games") or []):
                tot += l["cost"] + l.get("fee", 0)
        for r in self.state.data.get("resting", []):
            if self.game_of(r["ticker"]) == game or game in (r.get("games") or []):
                tot += r["remaining"] * r["price"]
        return tot

    def game_room(self, game: str) -> float:
        return self.sizing_equity() * float(self.horizon().get("max_game_total_pct", 0.15)) - self.game_exposure(game)

    def reserve_usd(self) -> float:
        """Fixed $ reserve (your extra deposit): only strong whale trades and lag trades may use it.
        Ordinary copies and parlays always leave this much cash untouched."""
        return max(0.0, float(self.bank.get("reserve_usd", 0) or 0))

    def reserve_free(self) -> float:
        return round(min(self.reserve_usd(), max(0.0, self.cash())), 2)

    def sizing_equity(self) -> float:
        """Balance used to size ordinary bets: the reserve and the future pot don't make normal bets bigger."""
        return max(0.0, self.equity() - self.reserve_free() - self.pot_value() - self.crypto_pot_usd())

    def lag_reserve(self) -> float:
        """Cash kept free for live lag trades (only once lag trading is switched on).
        With a fixed reserve set, lag trades use that shared reserve instead."""
        if self.reserve_usd() > 0:
            return 0.0
        lw = self.state.data.get("lagwatch") or {}
        lc = self.cfg.get("lag_watch", {}) or {}
        if lw.get("mode") != "trade" or not lc.get("enabled", True):
            return 0.0
        in_use = sum(x["count"] * x["entry"] for x in lw.get("trades", []))
        return max(0.0, self.equity() * float(lc.get("reserve_pct", 0.10)) - in_use)

    def lag_pnl(self) -> dict:
        """Results of lag trades: realized profit, wins, open positions (overall and per league)."""
        out = {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0, "open": 0, "open_cost": 0.0, "by": {}}
        for c in self.state.data.get("closed", []):
            if not str(c.get("whale", "")).startswith("Lag trade"):
                continue
            g = out["by"].setdefault(c["whale"].replace("Lag trade · ", "") or "?", {"trades": 0, "wins": 0, "pnl": 0.0})
            for d in (out, g):
                d["trades"] += 1
                d["wins"] += 1 if c["pnl"] > 0 else 0
                d["pnl"] = round(d["pnl"] + c["pnl"], 2)
            out["fees"] = round(out["fees"] + c.get("fees", 0), 2)
        for l in self.state.data.get("lots", []):
            if str(l.get("whale", "")).startswith("Lag trade") and l.get("count", 0) > 0:
                out["open"] += 1
                out["open_cost"] = round(out["open_cost"] + l["cost"], 2)
        out["reserve_free"] = (self.reserve_free() if self.reserve_usd() > 0
                               else round(min(self.lag_reserve(), max(0.0, self.cash())), 2))
        return out

    def kalshi_truth(self, coins: tuple[str, ...]) -> float | None:
        """Kalshi's own realized P&L today after fees on 15m crypto markets for these coins (cached 5 min).
        The off switches use this, not our records."""
        c = self.__dict__.setdefault("_truth", {})
        now = time.time()
        if c.get("t", 0) > now - 300 and "rows" in c:
            rows = c["rows"]
        else:
            try:
                rows, cur = [], None
                for _ in range(10):
                    p = {"limit": 200, "settlement_status": "all"}
                    if cur:
                        p["cursor"] = cur
                    j = self.kalshi._req("GET", "/portfolio/positions", params=p)
                    rows += j.get("market_positions") or []
                    cur = j.get("cursor")
                    if not cur:
                        break
                c.update(t=now, rows=rows)
            except Exception as e:
                log.warning("kalshi truth check failed: %s", e)
                return None
        day = time.strftime("%y%b%d", time.gmtime(now - 4 * 3600)).upper()
        tot = 0.0
        for r in rows:
            tk = r.get("ticker") or ""
            if "15M-" in tk and day in tk and tk[2:].split("15M")[0] in coins:
                tot += float(r.get("realized_pnl_dollars") or 0) - float(r.get("fees_paid_dollars") or 0)
        return tot

    def truth_since_start(self, key: str, coins: tuple[str, ...]) -> float | None:
        """Kalshi realized P&L after fees for these coins since the strategy's baseline today (the first
        check of the day, or a manual reset). Saved, so restarts don't reset it."""
        v = self.kalshi_truth(coins)
        if v is None:
            return None
        from copybot import _today
        base = self.state.data.setdefault("truth_base", {})
        day = _today()
        b = base.get(key)
        if not b or b.get("day") != day:
            b = base[key] = {"day": day, "v": v}
            log.info("Kalshi baseline for %s today: %+.2f (limits count from here)", key, v)
        return v - b["v"]

    def crypto_pot_usd(self) -> float:
        """Money deposited only for lag trades + the market maker: whales, combos and board picks never use it."""
        return max(0.0, float(self.bank.get("crypto_pot_usd", 0) or 0))

    def crypto_open(self) -> float:
        return sum(l["cost"] for l in self.state.data.get("lots", [])
                   if l.get("count", 0) > 0 and str(l.get("whale", "")).startswith(("Lag trade", "MM ")))

    def crypto_pot_held(self) -> float:
        """The part of the crypto pot that is sitting in cash right now (the rest is in open crypto trades)."""
        return max(0.0, self.crypto_pot_usd() - self.crypto_open())

    def spendable_cash(self, strong: bool = False) -> float:
        """Free cash for whale copies, board picks and parlays (the lag reserve is set aside).
        Ordinary bets also leave the fixed reserve alone; strong trades may use it. The crypto pot is never theirs."""
        return (self.cash() - self.pot_cash() - self.lag_reserve() - self.crypto_pot_held()
                - (0.0 if strong else self.reserve_usd()))

    def is_strong(self, score: float, same: int = 0) -> bool:
        """Strong trade (may use the reserve): high score, or 3+ whales on the same side."""
        rc = self.bank.get("reserve", {}) or {}
        return score >= float(rc.get("min_score", 80)) or same + 1 >= int(rc.get("min_whales", 3))

    def current_stage(self) -> dict:
        """Growth plan: bet heavier while the account is small, taper as it grows.
        Drawdown brake: 30% below peak -> safest stage until it recovers."""
        b = self.bank
        stages = b.get("stages") or [{"up_to": None, "tiers": b.get("tiers", [[80, 0.05], [65, 0.035], [50, 0.025]])}]
        eq = self.equity()
        peak = max(float(self.state.data.get("peak_equity", 0) or 0), eq, float(b.get("start_usd", 100)))
        self.state.data["peak_equity"] = round(peak, 2)
        brake = eq < peak * (1 - b.get("drawdown_brake_pct", 0.30))
        if brake:
            st = dict(stages[-1], name=f"{stages[-1].get('name', 'safe')} (drawdown brake)", brake=True)
            return st
        for st in stages:
            if st.get("up_to") is None or eq < float(st["up_to"]):
                return st
        return stages[-1]

    def current_tiers(self) -> list:
        return self.current_stage()["tiers"]

    def day_start_equity(self) -> float:
        d = self.state.data.setdefault("day_equity", {})
        if _today() not in d:
            d[_today()] = round(self.equity(), 2)
            for k in sorted(d)[:-14]:
                del d[k]
        return d[_today()]

    # ---- copy logic ------------------------------------------------------ #
    # ---- whale list: bench losers, add strong screener traders ------------- #
    def manage_whales(self, screened: list[dict]) -> None:
        wc = self.cfg.get("whale_manager", {}) or {}
        if not wc.get("enabled", True):
            return
        benched = self.state.data.setdefault("benched_whales", {})
        # 1) our own copied results per whale (live + pre-game copies together)
        agg: dict[str, dict] = {}
        for b in self.state.scoreboard():
            n = b["whale"].split(" · ")[0]
            if n.startswith(("Combo", "Lag trade", "Board picks")):
                continue
            a = agg.setdefault(n, {"closed": 0, "pnl": 0.0, "staked": 0.0})
            a["closed"] += b["closed"]
            a["pnl"] += b["pnl"]
            a["staked"] += b["staked"]
        for n, a in agg.items():
            roi = a["pnl"] / a["staked"] if a["staked"] else 0
            if n not in benched and a["closed"] >= int(wc.get("bench_min_copies", 20)) and roi <= float(wc.get("bench_roi", -0.10)):
                benched[n] = {"since": int(time.time()), "copies": a["closed"], "roi": round(100 * roi, 1)}
                log.warning("WHALES benched %s: %d settled copies, ROI %.1f%%", n, a["closed"], 100 * roi)
            elif n in benched and roi > 0:
                benched.pop(n, None)
                log.info("WHALES un-benched %s: ROI back to %.1f%%", n, 100 * roi)
        # 2) add strong, copyable traders from the screener
        if not wc.get("auto_add", True) or not screened:
            return
        auto = self.state.data.setdefault("auto_whales", [])
        auto[:] = [w for w in auto if w["name"] not in benched]
        have = set(self.whales)
        for r in sorted(screened, key=lambda r: -(r.get("score") or 0)):
            if len(auto) >= int(wc.get("max_auto", 6)):
                break
            w = (r.get("wallet") or "").lower()
            if not w or w in have:
                continue
            ok = ((r.get("score") or 0) >= float(wc.get("min_score", 85))
                  and (r.get("pnl_month") or 0) > 0 and (r.get("pnl_all") or 0) > 0
                  and 2 <= (r.get("per_day") or 0) <= float(wc.get("max_per_day", 60))
                  and (r.get("median_usd") or 0) >= float(wc.get("min_median_usd", 100))
                  and (r.get("copyable_pct") or 0) >= float(wc.get("min_copyable_pct", 80))
                  and (r.get("extreme_price_pct") or 100) <= 15
                  and (r.get("last_trade_h") or 999) <= 48)
            if not ok:
                continue
            name = re.sub(r"[^A-Za-z0-9_-]", "", str(r.get("name") or w[:10]))[:20] or w[:10]
            if name in benched:
                continue
            entry = {"name": name, "address": w, "auto": True, "added": int(time.time()),
                     "min_trade_usd": max(25, int(0.25 * (r.get("median_usd") or 100)))}
            auto.append(entry)
            self.whales[w] = entry
            have.add(w)
            log.warning("WHALES added %s (screener score %.0f, month +$%.0f, %.1f bets/day, typical $%.0f)",
                        name, r.get("score") or 0, r.get("pnl_month") or 0, r.get("per_day") or 0, r.get("median_usd") or 0)
        # drop auto whales benched since
        for w in [a for a, x in self.whales.items() if x.get("auto") and x["name"] in benched]:
            self.whales.pop(w, None)

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

        self._register_pair(t, tgt)
        if side == "BUY" and self._follow_hedge(addr, name, t, tgt, price, size):
            return
        if side == "BUY":
            min_usd = whale.get("min_trade_usd", self.risk["min_whale_trade_usd"])
            if usd < min_usd:
                return self._skip(t, name, f"below min ${min_usd}")
            self._copy_buy(name, whale, t, tgt, price, usd)
        elif side == "SELL":
            self._copy_sell(addr, name, t, tgt, price, size)

    # ---- pairs (same bet on Polymarket and Kalshi) for the live lag watcher -- #
    def _register_pair(self, t: dict, tgt: Target) -> None:
        cond, outc = t.get("conditionId"), t.get("outcome")
        if not cond or outc is None:
            return
        pairs = self.__dict__.setdefault("pairs", {})
        key = f"{tgt.ticker}|{tgt.side}"
        if key in pairs:
            return
        pairs[key] = {"cond": cond, "outcome": str(outc), "ticker": tgt.ticker, "side": tgt.side,
                      "title": t.get("title") or "", "added": time.time()}
        if len(pairs) > 400:
            for k in sorted(pairs, key=lambda k: pairs[k]["added"])[:100]:
                pairs.pop(k, None)

    # ---- whale in-game moves ------------------------------------------------ #
    @staticmethod
    def _is_winner_ticker(tk: str) -> bool:
        series = tk.split("-", 1)[0].upper()
        return series.endswith("GAME") or series.endswith("MATCH") or "MAP" in series

    def _opposite_holding(self, tgt: Target, whale_name: str):
        """A position we copied from this whale on the other side of the same bet, or None."""
        base = tgt.ticker.rsplit("-", 1)[0]
        for lot in self.state.data.get("lots", []):
            if not str(lot.get("whale", "")).split(" · ")[0] == whale_name.split(" · ")[0]:
                continue
            tk, sd = lot["ticker"], lot["side"]
            same_mkt_other_side = tk == tgt.ticker and sd != tgt.side
            other_team_same_game = (tk != tgt.ticker and tk.rsplit("-", 1)[0] == base and sd == tgt.side == "yes"
                                    and self._is_winner_ticker(tk))
            if (same_mkt_other_side or other_team_same_game) and self.state.position(tk, sd) > 0:
                return tk, sd
        return None

    def _follow_hedge(self, addr, name, t, tgt: Target, whale_px, shares) -> bool:
        """Whale bought the other side of a bet we copied from them = a hedge / partial cash-out.
        We sell the same share of our position (one fee) instead of buying the other side."""
        lc = self.cfg.get("live_moves", {}) or {}
        if not lc.get("follow_hedges", True):
            return False
        hit = self._opposite_holding(tgt, name)
        if not hit:
            return False
        tk, sd = hit
        held = self.state.position(tk, sd)
        try:  # whale's shares still on the original side of this market
            r = self.pm.s.get(f"{POLY_DATA}/positions", params={"user": addr, "market": t.get("conditionId", ""),
                                                                "sizeThreshold": 0}, timeout=15)
            pos = [p for p in (r.json() or []) if str(p.get("outcome", "")).lower() != str(t.get("outcome", "")).lower()]
            orig = max((float(p.get("size") or 0) for p in pos), default=0.0)
        except Exception:
            orig = 0.0
        frac = 1.0 if orig <= 0 else min(1.0, shares / (orig + shares))
        count = max(1, math.floor(held * frac))
        try:
            m = self.kdata.market(tk)
        except Exception:
            return False
        bid = _px(m, f"{sd}_bid")
        log.info("  HEDGE: %s bought the other side (%s, %.0f sh vs %.0f held) -> sell %d/%d of %s %s",
                 name, t.get("outcome"), shares, orig, count, held, tk, sd.upper())
        if not bid:
            self._skip(t, name, "hedge: no bid on Kalshi to sell into")
            return True
        filled, proceeds = self._execute(Target(tk, sd), "sell", count, bid)
        if filled:
            px = proceeds / filled
            self.state.add_position(tk, sd, -filled)
            self.state.sell_lots(tk, sd, filled, px, kalshi_fee(filled, px, self.fee_rate))
            self._journal(f"{name} · hedge", t, Target(tk, sd), "sell", filled, px)
        return True

    def _copy_buy(self, name, whale, t, tgt: Target, whale_px, whale_usd) -> None:
        r = self.risk
        base = name.split(" · ")[0]
        if base in (self.state.data.get("benched_whales") or {}):
            return self._skip(t, name, "whale benched (losing copy record)")
        m = self.kdata.market(tgt.ticker)
        if m.get("status") not in (None, "open", "active"):
            return self._skip(t, name, f"Kalshi market {m.get('status')}")
        ask = _px(m, f"{tgt.side}_ask")
        if ask is None or ask <= 0 or ask >= 1:
            return self._skip(t, name, "no ask on Kalshi")

        # Live (in-game) bets: allowed only if very fresh and no worse than the whale's price
        lv = self.cfg.get("live_games", {}) or {}
        cls = self.sports.classify(t) or {}
        status = (self.game_status(t.get("conditionId", ""), m, cls.get("date"))
                  if t.get("conditionId") and cls.get("date") else "pre")  # non-sports markets: no game clock
        is_live = status != "pre"
        size_mult = 1.0
        if is_live:
            if not lv.get("enabled", False):
                return self._skip(t, name, "game in progress (live copying off)")
            age = time.time() - int(t.get("timestamp") or time.time())
            if age > lv.get("max_age_seconds", 30):
                return self._skip(t, name, f"live game: whale bet too old ({age:.0f}s)")
            slip = lv.get("max_slippage", 0.0)
            size_mult = float(lv.get("size_mult", 0.5))
            name = f"{name} · live"
        else:
            slip = r["max_slippage"]
        # score the bet at Kalshi's current ask
        from scoring import score_bet, stake_pct
        addr = whale.get("address", "").lower()
        stats = self.whale_stats.get(addr)
        same, opp = self.consensus(addr, t)
        sc = score_bet(whale_px=whale_px, ask=ask, whale_usd=whale_usd, stats=stats,
                       same=same, opposite=opp, rate=self.fee_rate)
        log.info("  score %d (EV %+.1f%%) %s  whales same/opp %d/%d", sc["score"], 100 * sc["ev"],
                 sc["parts"], same, opp)

        # Buying at the ask isn't worth it? Pre-game, rest a limit order at a fair price instead.
        take_ok = (ask <= whale_px + slip + 1e-9 and ask <= r["max_entry_price"]
                   and sc["ev"] >= self.bank.get("min_ev", 0.0))
        lim = self.cfg.get("limit_orders", {}) or {}
        if not take_ok and r["sizing"] == "score" and lim.get("enabled", False) and not is_live:
            return self._place_resting(name, t, tgt, whale_px, whale_usd, stats, same, opp, ask, m, cls)
        if ask > whale_px + slip + 1e-9:
            return self._skip(t, name, f"Kalshi ask {ask:.3f} > whale {whale_px:.3f} + slippage")
        if ask > r["max_entry_price"]:
            return self._skip(t, name, f"price {ask:.3f} above max_entry_price")

        # sizing
        held_usd = self.state.position(tgt.ticker, tgt.side) * ask
        if r["sizing"] == "score":
            budget, why = self._bankroll_budget(sc["score"], sc["ev"], held_usd,
                                                self.is_strong(sc["score"], same),
                                                settle=self.settle_ts(tgt.ticker, m), game=self.game_of(tgt.ticker))
            use_pot = getattr(self, "_budget_pot", False)
            if why:
                return self._skip(t, name, why)
            budget *= size_mult
            if budget < self.bank.get("min_stake_usd", 2.0):
                return self._skip(t, name, "stake below minimum after live-game reduction")
        else:
            if r["sizing"] == "fixed":
                budget = r["fixed_usd"]
            else:  # proportional
                budget = whale_usd * whale.get("copy_ratio", r["copy_ratio"])
            budget = min(budget, r["max_usd_per_trade"])
            budget = min(budget, r["max_usd_per_day"] - self.state.spent_today())
            budget = min(budget, r["max_usd_per_market"] - held_usd)
        count = math.floor(budget / ask) if budget > 0 else 0
        if count < 1:
            return self._skip(t, name, "risk limits leave no room")
        fee_pct = 100 * self.fee_rate * (1 - ask)  # fee as % of stake
        if fee_pct > r.get("max_fee_pct", 6):
            return self._skip(t, name, f"Kalshi fee ~{fee_pct:.1f}% of stake is too high")

        filled, cost = self._execute(tgt, "buy", count, ask)
        if filled:
            px = cost / filled
            fee = kalshi_fee(filled, px, self.fee_rate)
            self.state.add_position(tgt.ticker, tgt.side, filled)
            self.state.add_spent(cost + fee)
            self.state.open_lot(name, tgt.ticker, tgt.side, filled, px, fee, t.get("title") or "")
            self.state.data["lots"][-1]["score"] = sc["score"]
            if r["sizing"] == "score" and use_pot:
                self._pot_take(self.state.data["lots"][-1])
            self._journal(name, t, tgt, "buy", filled, px, score=sc["score"])

    # ---- limit (resting) orders ------------------------------------------- #
    def _place_resting(self, name, t, tgt: Target, fair_px, whale_usd, stats, same, opp, ask, m, cls) -> None:
        """Rest a post-only buy at the best price that still has positive EV with the (lower) maker fee."""
        from scoring import score_bet, expected_value
        lim = self.cfg.get("limit_orders", {}) or {}
        rate = float(lim.get("maker_fee_rate", 0.0175))
        key = f"{tgt.ticker}|{tgt.side}"
        resting = self.state.data.setdefault("resting", [])
        price = math.floor(min(fair_px + 0.02, ask - 0.01) * 100) / 100
        while price >= 0.05 and expected_value(fair_px, price, stats, rate) < self.bank.get("min_ev", 0.0):
            price = round(price - 0.01, 2)
        if price < 0.05 or price > self.risk["max_entry_price"] or fair_px - price > 0.08:
            return self._skip(t, name, "no fair limit price")
        old = [x for x in resting if f'{x["ticker"]}|{x["side"]}' == key]
        if old:
            if all(abs(x["price"] - price) < 0.01 for x in old):
                return self._skip(t, name, "limit order already resting at this price")
            # whale added at a new price: move our waiting order to the new fair price
            for x in old:
                if self.mode == "live" and not str(x["id"]).startswith("paper-"):
                    try:
                        self.kalshi.cancel_order(x["id"], x["ticker"])
                    except Exception as e:
                        log.warning("  reprice cancel failed: %s", e)
                        return self._skip(t, name, "limit order already resting (reprice failed)")
            resting[:] = [x for x in resting if f'{x["ticker"]}|{x["side"]}' != key]
            log.info("  REPRICE limit %s %s: %.2f -> %.2f", tgt.ticker, tgt.side.upper(), old[0]["price"], price)
        sc = score_bet(whale_px=fair_px, ask=price, whale_usd=whale_usd, stats=stats,
                       same=same, opposite=opp, rate=rate)
        held_usd = self.state.position(tgt.ticker, tgt.side) * price
        budget, why = self._bankroll_budget(sc["score"], sc["ev"], held_usd, self.is_strong(sc["score"], same),
                                            settle=self.settle_ts(tgt.ticker, m), game=self.game_of(tgt.ticker))
        use_pot = getattr(self, "_budget_pot", False)
        if why:
            return self._skip(t, name, f"limit: {why}")
        count = math.floor(budget / price)
        if count < 1:
            return self._skip(t, name, "limit: risk limits leave no room")
        start = self.pm.game_start(t.get("conditionId", "")) if t.get("conditionId") else None
        start = start or self._kalshi_start(m)
        before = lim.get("cancel_before_start_minutes", 5) * 60
        expires = int(min(start - before, time.time() + lim.get("max_hours", 6) * 3600) if start
                      else time.time() + min(6, lim.get("max_hours", 6)) * 3600)  # unknown start: short
        if expires <= time.time() + 60:
            return self._skip(t, name, "limit: too close to game start")
        if self.mode == "live":
            try:
                o = self.kalshi.create_order(tgt.ticker, tgt.side, "buy", count, price, None,
                                             post_only=True, expiration_ts=expires)
            except Exception as e:
                log.error("  LIMIT ORDER FAILED: %s", e)
                return self._skip(t, name, "limit order rejected")
            oid = o.get("order_id")
        else:
            oid = f"paper-{uuid.uuid4()}"
        resting.append({"id": oid, "ticker": tgt.ticker, "side": tgt.side, "price": price, "count": count,
                        "remaining": count, "filled": 0, "fee_rate": rate, "whale": name,
                        "title": t.get("title") or "", "outcome": t.get("outcome"), "score": sc["score"],
                        "expires": expires, "placed": int(time.time()), "pm_price": fair_px, "pot": use_pot})
        log.info("  LIMIT %s BUY %d %s %s @ %.2f (ask %.2f, fair %.2f) score %d EV %+.1f%%, expires in %.0f min",
                 self.mode.upper(), count, tgt.ticker, tgt.side.upper(), price, ask, fair_px, sc["score"],
                 100 * sc["ev"], (expires - time.time()) / 60)

    def check_resting(self) -> None:
        """Record fills on resting orders; drop expired/cancelled ones."""
        resting = self.state.data.get("resting", [])
        keep = []
        for r in resting:
            new, done = 0, False
            if self.mode == "live" and not str(r["id"]).startswith("paper-"):
                try:
                    o = self.kalshi.get_order(r["id"])
                except Exception as e:
                    log.warning("resting order check failed: %s", e)
                    keep.append(r)
                    continue
                filled = int(float(o.get("fill_count_fp") or o.get("fill_count") or 0))
                new = max(0, filled - r["filled"])
                done = o.get("status") in ("canceled", "cancelled", "executed", "expired")
                if not done and time.time() > r["expires"] + 120:
                    try:
                        self.kalshi.cancel_order(r["id"], r.get("ticker"))
                    except Exception:
                        pass
                    done = True
            else:  # paper: fills when Kalshi's ask comes down to our price
                try:
                    m = self.kdata.market(r["ticker"])
                    ask = _px(m, f'{r["side"]}_ask')
                except Exception:
                    ask = None
                if ask is not None and ask <= r["price"] + 1e-9 and time.time() < r["expires"]:
                    new = r["remaining"]
                done = time.time() > r["expires"]
            if new:
                px = r["price"]
                fee = kalshi_fee(new, px, r["fee_rate"])
                self._commit(new * px + fee)
                self.state.add_position(r["ticker"], r["side"], new)
                self.state.add_spent(new * px + fee)
                self.state.open_lot(r["whale"], r["ticker"], r["side"], new, px, fee, r["title"])
                self.state.data["lots"][-1]["score"] = r["score"]
                if r.get("games"):
                    self.state.data["lots"][-1]["games"] = r["games"]
                if r.get("pot"):
                    self._pot_take(self.state.data["lots"][-1])
                self._journal(r["whale"], {"title": r["title"], "outcome": r["outcome"], "price": r["pm_price"]},
                              Target(r["ticker"], r["side"]), "buy (limit)", new, px, score=r["score"])
                log.info("LIMIT FILLED %d %s %s @ %.2f (%s)", new, r["ticker"], r["side"].upper(), px, r["whale"])
                r["filled"] += new
                r["remaining"] -= new
            if r["remaining"] > 0 and not done:
                keep.append(r)
        self.state.data["resting"] = keep

    def cancel_resting(self, ticker: str, side: str) -> None:
        for r in [x for x in self.state.data.get("resting", []) if x["ticker"] == ticker and x["side"] == side]:
            if self.mode == "live" and not str(r["id"]).startswith("paper-"):
                try:
                    self.kalshi.cancel_order(r["id"], r.get("ticker"))
                except Exception as e:
                    log.warning("cancel failed: %s", e)
            log.info("Cancelled resting order on %s %s (whale exited)", ticker, side.upper())
        self.state.data["resting"] = [x for x in self.state.data.get("resting", [])
                                      if not (x["ticker"] == ticker and x["side"] == side)]

    def _bankroll_budget(self, score: float, ev: float, held_usd: float,
                         strong: bool = False, settle: float | None = None,
                         game: str | None = None) -> tuple[float, str | None]:
        """$ to stake from the bankroll for a bet with this score, or (0, reason to skip)."""
        from scoring import stake_pct
        b = self.bank
        pct = stake_pct(score, self.current_tiers())
        if pct <= 0:
            return 0.0, f"score {score} below minimum"
        self._budget_pot = False
        if ev < b.get("min_ev", 0.0):
            return 0.0, f"expected value {100 * ev:+.1f}% after fees"
        days = self.days_out(settle)
        if ev < b.get("min_ev", 0.0) + self.extra_edge(days):
            return 0.0, f"edge {100 * ev:+.1f}% too small for money locked {days:.1f} days"
        eq = self.equity()
        if eq <= self.day_start_equity() * (1 - b.get("daily_stop_pct", 0.15)):
            return 0.0, "daily loss stop reached"
        strong = strong or self.is_strong(score)
        seq = self.sizing_equity()
        budget = seq * pct
        budget = min(budget, seq * self.current_stage().get("max_game_pct", b.get("max_game_pct", 0.08)) - held_usd)
        budget = min(budget, seq * self.current_stage().get("max_open_pct", b.get("max_open_pct", 0.7))
                     - (self.open_cost() - self.pot_open()))
        if game:
            budget = min(budget, self.game_room(game))
        if self.is_far(days) and self.pot_state()["usd"] > 0:  # 3+ days out: only the future pot pays
            budget = min(seq * pct, self.pot_value() * float(self.horizon().get("pot_max_bet_pct", 0.3)),
                         self.pot_free(), self.game_room(game) if game else 1e9)
            if budget < b.get("min_stake_usd", 2.0):
                return 0.0, "settles 3+ days out and the future pot has no room"
            self._budget_pot = True
            return budget, None
        free = self.spendable_cash(strong)
        if strong and self.reserve_usd() > 0 and self.spendable_cash() < budget <= free:
            log.info("  using the reserve for a strong trade (score %.0f)", score)
        budget = min(budget, free)
        if budget < b.get("min_stake_usd", 2.0):
            return 0.0, "bankroll limits leave no room"
        return budget, None

    def process_board_queue(self) -> None:
        """Auto-bet the strongest 'Best bets' picks (several proven traders agree, high score)."""
        picks, self.board_queue = self.board_queue, []
        bc = self.bank.get("board", {}) or {}
        if not picks or not bc.get("enabled", False) or self.paused or self.risk.get("sizing") != "score":
            return
        done = self.state.data.setdefault("board_done", {})
        for k in [k for k, ts in done.items() if ts < time.time() - 48 * 3600]:
            del done[k]
        for p in picks:
            if time.time() - p.get("latest_ts", 0) > bc.get("max_age_hours", 12) * 3600:
                continue
            # consensus: 3+ proven traders on the same side counts even if no single bet scores high
            consensus = (p["whales"] >= int(bc.get("consensus_min_whales", 3))
                         and p["score"] >= float(bc.get("consensus_min_score", 55)))
            strong = p["score"] >= bc.get("min_score", 80) and p["whales"] >= bc.get("min_whales", 2)
            if not (strong or consensus):
                continue
            if consensus and not strong:  # size it like a solid pick, more with more traders agreeing
                p = dict(p, score=min(80, max(p["score"], 65 + 5 * (p["whales"] - 3))))
            if p["ev"] <= bc.get("min_ev", 0.0):
                # good pick, bad price right now: wait for it with a fair-priced limit order
                if p.get("status") == "pre" and (self.cfg.get("limit_orders", {}) or {}).get("enabled", False):
                    key = f'{p["ticker"]}|{p["side"]}'
                    if key not in done and self.state.position(p["ticker"], p["side"]) <= 0:
                        try:
                            m = self.kdata.market(p["ticker"])
                            ask = _px(m, f'{p["side"]}_ask')
                            if ask:
                                t0 = {"title": p["title"], "outcome": p["outcome"], "price": p["whale_px"]}
                                self._place_resting("Board picks", t0, Target(p["ticker"], p["side"]), p["whale_px"],
                                                    p.get("whale_usd", 0), None, p["whales"] - 1, 0, ask, m, {})
                                done[key] = time.time()
                        except Exception as e:
                            log.warning("board limit failed: %s", e)
                continue
            key = f'{p["ticker"]}|{p["side"]}'
            if key in done or self.state.position(p["ticker"], p["side"]) > 0:
                continue
            t = {"title": p["title"], "outcome": p["outcome"], "price": p["whale_px"]}
            name = "Board picks"
            try:
                m = self.kdata.market(p["ticker"])
            except Exception as e:
                log.warning("board pick lookup failed: %s", e)
                continue
            if m.get("status") not in (None, "open", "active"):
                continue
            ask = _px(m, f'{p["side"]}_ask')
            if (not ask or ask <= 0 or ask >= 1 or ask > p["ask"] + 0.01 or ask < p["ask"] - 0.03
                    or p["whale_px"] - ask > 0.08 or ask > self.risk["max_entry_price"]):
                continue  # price moved, game likely started, or unusable
            if 100 * self.fee_rate * (1 - ask) > self.risk.get("max_fee_pct", 6):
                continue
            if p.get("status") != "pre":
                continue  # only bet games confirmed not to have started
            log.info("[Board] %s %s -> %s %s  score %d EV %+.1f%%  (%d traders: %s)", p["title"], p["outcome"],
                     p["ticker"], p["side"].upper(), p["score"], 100 * p["ev"], p["whales"], ", ".join(p["names"]))
            budget, why = self._bankroll_budget(p["score"], p["ev"], 0.0, strong=True,  # 3+ whales agree
                                                settle=self.settle_ts(p["ticker"]), game=self.game_of(p["ticker"]))
            use_pot = getattr(self, "_budget_pot", False)
            if why:
                self._skip(t, name, why)
                continue
            count = math.floor(budget / ask)
            if count < 1:
                continue
            tgt = Target(p["ticker"], p["side"])
            filled, cost = self._execute(tgt, "buy", count, ask)
            done[key] = time.time()
            if filled:
                px = cost / filled
                fee = kalshi_fee(filled, px, self.fee_rate)
                self.state.add_position(tgt.ticker, tgt.side, filled)
                self.state.add_spent(cost + fee)
                self.state.open_lot(name, tgt.ticker, tgt.side, filled, px, fee, p["title"])
                self.state.data["lots"][-1]["score"] = p["score"]
                if use_pot:
                    self._pot_take(self.state.data["lots"][-1])
                self._journal(name, t, tgt, "buy", filled, px, score=p["score"])

    def _copy_sell(self, addr, name, t, tgt: Target, whale_px, sold) -> None:
        self.cancel_resting(tgt.ticker, tgt.side)
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
            px = proceeds / filled
            self.state.add_position(tgt.ticker, tgt.side, -filled)
            self.state.sell_lots(tgt.ticker, tgt.side, filled, px, kalshi_fee(filled, px, self.fee_rate))
            self._journal(name, t, tgt, "sell", filled, px)

    @staticmethod
    def _kalshi_start(m: dict) -> float | None:
        """Start time from Kalshi rules text: '...originally scheduled for Oct 22, 2026 at 10:00 AM EDT'."""
        mm = re.search(r"scheduled for ([A-Z][a-z]{2} \d{1,2}, \d{4}) at (\d{1,2}:\d{2} [AP]M) (EDT|EST)",
                       m.get("rules_primary") or "")
        if not mm:
            return None
        dt = datetime.strptime(f"{mm.group(1)} {mm.group(2)}", "%b %d, %Y %I:%M %p")
        offset = 4 if mm.group(3) == "EDT" else 5
        return dt.replace(tzinfo=timezone.utc).timestamp() + offset * 3600

    def game_status(self, cond: str, kalshi_market: dict, game_date) -> str:
        """'pre' (safe to bet), 'started', or 'unknown' (same-day game with no start time)."""
        start = self.pm.game_start(cond) or self._kalshi_start(kalshi_market)
        buffer = (self.cfg.get("catch_up", {}) or {}).get("min_minutes_before", 10) * 60
        if start is not None:
            return "started" if time.time() > start - buffer else "pre"
        today = datetime.now(timezone.utc).date()
        if game_date is not None and game_date > today:
            return "pre"
        return "unknown"

    def catch_up(self) -> None:
        """Copy followed whales' open bets on games that haven't started yet (bets made before we watched)."""
        cc = self.cfg.get("catch_up", {}) or {}
        if not cc.get("enabled", True) or self.paused:
            return
        done = self.state.data.setdefault("catchup_done", {})
        for k in [k for k, ts in done.items() if ts < time.time() - 4 * 86400]:
            del done[k]
        today = datetime.now(timezone.utc).date()
        for addr, whale in self.whales.items():
            name = whale.get("name", addr[:8])
            try:
                pos = self.pm.open_positions(addr)
            except Exception as e:
                log.warning("catch-up positions %s failed: %s", name, e)
                continue
            min_usd = whale.get("min_trade_usd", self.risk["min_whale_trade_usd"])
            for p in pos:
                cond, out = p.get("conditionId"), p.get("outcome")
                key = f"{cond}|{out}"
                cur = float(p.get("curPrice") or 0)
                value = float(p.get("currentValue") or 0)
                if key in done or value < min_usd or not 0.05 < cur < 0.95:
                    continue
                t = {"slug": p.get("slug"), "eventSlug": p.get("eventSlug"), "title": p.get("title"),
                     "outcome": out, "outcomeIndex": p.get("outcomeIndex"), "conditionId": cond,
                     "price": cur, "size": value / cur if cur else 0, "side": "BUY",
                     "timestamp": int(time.time())}
                c = self.sports.classify(t)
                if not c or "skip" in c or c.get("date") is None:
                    continue
                if not (today - timedelta(days=1) <= c["date"] <= today + timedelta(days=cc.get("days_ahead", 3))):
                    continue
                tgt = self.maps.lookup(t)
                if not tgt:
                    found, _ = self.sports.lookup(t)
                    if not found:
                        continue
                    tgt = Target(found["ticker"], found["side"])
                if self.state.position(tgt.ticker, tgt.side) > 0:
                    done[key] = time.time()
                    continue
                # make sure the game hasn't started
                try:
                    km = self.kdata.market(tgt.ticker)
                except Exception:
                    continue
                status = self.game_status(cond, km, c["date"])
                if status == "unknown":
                    continue  # same-day game with unknown start time: don't risk copying a live game
                if status == "started":
                    done[key] = time.time()  # started (or about to): never copy
                    continue
                log.info("[%s] CATCH-UP open bet %s '%s' $%.0f now %.0fc", name, out, t["title"], value, 100 * cur)
                before = (len(self.state.data["lots"]), len(self.state.data.get("resting", [])))
                self._register_pair(t, tgt)
                self._copy_buy(name, whale, t, tgt, cur, value)
                if (len(self.state.data["lots"]), len(self.state.data.get("resting", []))) != before:
                    done[key] = time.time()

    # ---- daily combo slate ------------------------------------------------- #
    _coll_cache: tuple = (0.0, [])
    combo_slate: dict = {"combos": [], "slates": []}

    def _combo_pool(self, picks: list[dict], cc: dict) -> list[dict]:
        """Every strong leg (whale picks + top player props), most likely first."""
        pool = list(picks) + list(getattr(self, "prop_picks", []) or [])
        legs, seen = [], set()
        for p in pool:
            if p.get("kind") == "prop":
                ok = (p.get("p", 0) >= cc.get("min_prop_chance", 0.70) and p["ev"] > 0
                      and p["score"] >= cc.get("min_prop_score", 55))
            else:
                ok = p["score"] >= cc.get("min_score", 70) and p["ev"] > 0
            if (not ok or p.get("status") != "pre" or p["side"] != "yes" or not 0.25 <= p["ask"] <= 0.93
                    or p["ticker"] in seen):
                continue
            if p.get("start") and p["start"] < time.time() + 900:
                continue
            ev = p.get("event_ticker")
            if not ev:
                try:
                    ev = self.kdata.market(p["ticker"]).get("event_ticker")
                except Exception:
                    continue
            if not ev:
                continue
            seen.add(p["ticker"])
            legs.append({"event_ticker": ev, "market_ticker": p["ticker"], "side": "yes",
                         "fair": float(p.get("p") or p["whale_px"]), "ask": p["ask"], "score": p["score"],
                         "game": ev.split("-", 1)[1] if "-" in ev else ev, "start": p.get("start"),
                         "player": p.get("player"), "hist": p.get("hist"), "p_hist": p.get("p_hist"),
                         "title": p["outcome"] if p.get("kind") == "prop" else f'{p["title"]} — {p["outcome"]}'})
        return sorted(legs, key=lambda l: (-l["fair"], -l["score"]))

    @staticmethod
    def _pair_factor(a: dict, b: dict) -> float | None:
        """How two legs from the same game move together, from games both players played:
        joint hit rate / (rate A x rate B). >1 = they tend to hit together. None = not enough
        shared games to tell. Pulled halfway to 1 to stay conservative."""
        ha = {e: h for e, h in (a.get("hist") or []) if e}
        hb = {e: h for e, h in (b.get("hist") or []) if e}
        shared = [e for e in ha if e in hb]
        if len(shared) < 6:
            return None
        n = len(shared)
        both = sum(1 for e in shared if ha[e] and hb[e])
        pa = (sum(ha[e] for e in shared) + 1) / (n + 2)
        pb = (sum(hb[e] for e in shared) + 1) / (n + 2)
        pj = (both + pa * pb * 2) / (n + 2)
        f = max(0.80, min(1.25, pj / (pa * pb)))
        return 1 + 0.5 * (f - 1)

    def _combo_fair(self, legs: list[dict]) -> float:
        p = math.prod(l["fair"] for l in legs)
        for i, a in enumerate(legs):
            for b in legs[i + 1:]:
                if a["game"] == b["game"]:
                    p *= self._pair_factor(a, b) or 0.97
        return min(p, min(l["fair"] for l in legs))

    def _can_join(self, leg: dict, take: list[dict], cc: dict) -> bool:
        """Same game (or same player) is fine when the history shows the legs hit together
        (or are unrelated); never when they work against each other or there's no shared data."""
        same = [x for x in take if x["game"] == leg["game"]]
        if not same:
            return True
        if not cc.get("same_game", True) or len(same) >= int(cc.get("max_per_game", 3)):
            return False
        floor = float(cc.get("min_pair_factor", 1.0))
        return all((self._pair_factor(leg, x) or 0) >= floor for x in same)

    def _plan_slate(self, legs: list[dict], cc: dict, sizes: list[int]) -> list[list[dict]]:
        """Put every leg into a combo, each leg used once. Legs from the same game (or the same
        player) only share a combo when their game logs show they hit together. Strongest legs go
        into the small combos; 5-6 leg combos only use elite legs; the size list repeats until
        all legs are used. A combo below the hit-chance floor is shrunk until it clears it."""
        min_hit = float(cc.get("min_hit_chance", 0.30))
        elite = float(cc.get("elite_leg_chance", 0.84))
        left, out, i, stalls = list(legs), [], 0, 0
        while len(left) >= 2 and stalls < len(sizes):
            n = sizes[i % len(sizes)]
            i += 1
            take = []
            for l in left:
                if len(take) >= n:
                    break
                if n >= 5 and l["fair"] < elite:
                    continue
                if self._can_join(l, take, cc):
                    take.append(l)
            while len(take) > 2 and self._combo_fair(take) < min_hit:
                take.pop()
            if len(take) < 2 or self._combo_fair(take) < min_hit:
                stalls += 1
                continue
            stalls = 0
            out.append(take)
            ids = {id(l) for l in take}
            left = [l for l in left if id(l) not in ids]
        for l in left:  # leftover legs join a combo they fit in
            for c in reversed(out):
                if len(c) < max(sizes) and self._can_join(l, c, cc) and self._combo_fair(c + [l]) >= min_hit:
                    c.append(l)
                    break
        return out

    def _build_combo(self, legs: list[dict]) -> tuple[str | None, dict, list]:
        if time.time() - self._coll_cache[0] > 6 * 3600 or not self._coll_cache[1]:
            self._coll_cache = (time.time(), self.kalshi.collections())
        sel = [{k: l[k] for k in ("event_ticker", "market_ticker", "side")} for l in legs]
        want = {l["event_ticker"] for l in legs}
        cands = []
        for c in self._coll_cache[1]:
            evs = {e.get("event_ticker") if isinstance(e, dict) else e
                   for e in (c.get("associated_events") or c.get("associated_event_tickers") or [])}
            if evs and want <= evs:
                cands.insert(0, c)
            elif "SPORTS" in (c.get("collection_ticker") or "").upper() or \
                    "CROSSCATEGORY" in (c.get("collection_ticker") or "").upper():
                cands.append(c)  # listing doesn't say which events it takes: let Kalshi decide
        errs = []
        for c in cands:
            try:
                tk = self.kalshi.create_combo(c["collection_ticker"], sel).get("market_ticker")
                if tk:
                    for i in range(6):  # a new combo market can take a few seconds to show up
                        for cli in (self.kdata, self.kalshi):
                            try:
                                return tk, cli.market(tk), errs
                            except Exception:
                                pass
                        time.sleep(1.5 + i)
                    return tk, {}, errs + ["combo price not available yet"]
            except Exception as e:
                errs.append(f'{c.get("collection_ticker")}: {str(e)[:160]}')
        return None, {}, errs

    @staticmethod
    def _combo_league(ticker: str) -> str:
        t = ticker.upper()
        return "NBA" if t.startswith("KXNBA") else ("NFL" if t.startswith("KXNFL") else "OTHER")

    def _combo_spent_today(self) -> float:
        """Money in combos today = combos bought today + combo limit orders still waiting.
        (Waiting orders that expire or get cancelled free their money again.)"""
        from zoneinfo import ZoneInfo
        day0 = datetime.now(ZoneInfo(LOCAL_TZ)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        bought = sum(float(l.get("cost", 0)) + float(l.get("fee", 0)) for l in self.state.data.get("lots", [])
                     if str(l.get("whale", "")).startswith("Combo") and float(l.get("opened", 0)) >= day0
                     and not l.get("pot"))
        waiting = sum(float(r.get("price", 0)) * float(r.get("remaining", 0)) for r in self.state.data.get("resting", [])
                      if str(r.get("whale", "")).startswith("Combo") and not r.get("pot"))
        return round(bought + waiting, 2)

    def _add_combo_spent(self, usd: float) -> None:
        d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        m = self.state.data.setdefault("combo_spent_by_day", {})
        m[d] = round(float(m.get(d, 0.0)) + usd, 2)
        for k in sorted(m)[:-10]:
            m.pop(k, None)

    def weekly_combo(self, picks: list[dict]) -> None:
        """Combo slates per league: NBA daily (tonight's games), NFL for the upcoming games,
        other whale picks on their own. Every strong leg goes into a combo; each combo is
        priced by Kalshi market makers (RFQ) and bought only at or below our fair price."""
        cc = self.cfg.get("combos", {}) or {}
        if not cc.get("enabled", False) or self.paused or not self.kalshi.pkey:
            return
        if not self.state.data.get("combo_reset_v8"):  # RFQ + edge sizing: redo today's combos
            self.state.data["combo_reset_v8"] = True
            self.state.data["combo_last_by"] = {}
            keep = []
            for r in self.state.data.get("resting", []):
                if str(r.get("whale", "")).startswith("Combo") and not r.get("filled"):
                    try:
                        if self.mode == "live" and not str(r.get("id", "")).startswith("paper"):
                            self.kalshi.cancel_order(r["id"], r.get("ticker"))
                        log.info("COMBO cancelled old waiting combo %s to re-price it", r.get("title"))
                        continue
                    except Exception as e:
                        log.warning("combo cancel failed: %s", e)
                keep.append(r)
            self.state.data["resting"] = keep
        legs = self._combo_pool(picks, cc)
        in_play = " | ".join(str(x.get("title", "")) for x in
                             self.state.data.get("resting", []) + self.state.data.get("lots", [])
                             if str(x.get("whale", "")).startswith("Combo"))
        legs = [l for l in legs if l["title"] not in in_play]
        used = {}  # legs in open/waiting combos are excluded above (by title)
        if not self.state.data.get("combo_reset_v10"):
            self.state.data["combo_reset_v10"] = True
            self.state.data["combo_last_by"] = {}
            self.state.data.pop("combo_legs_used", None)
        last_by = self.state.data.setdefault("combo_last_by", {})
        leagues = cc.get("leagues") or {}
        self._borrow_ok, why = self._whale_prices_bad(picks or getattr(self, "_last_whale_picks", []), cc)
        if picks:
            self._last_whale_picks = picks
        if self._borrow_ok != getattr(self, "_last_borrow_log", None):
            self._last_borrow_log = self._borrow_ok
            log.info("COMBO borrow whale share: %s (%s)", "ON" if self._borrow_ok else "off", why)
        self.combo_borrow = {"on": self._borrow_ok, "why": why}
        slates = list(self.combo_slate.get("slates", [])) if isinstance(self.combo_slate, dict) else []
        for lg in ("NBA", "NFL", "OTHER"):
            lc = leagues.get(lg, {}) or {}
            if not lc.get("enabled", True):
                continue
            every = float(lc.get("every_hours", 24))
            if self._borrow_ok:  # idle whale cash: look for more parlays every few hours
                every = min(every, float((cc.get("borrow", {}) or {}).get("rebuild_hours", 3)))
            if time.time() - float(last_by.get(lg, 0) or 0) < every * 3600:
                continue
            lg_legs = [l for l in legs if self._combo_league(l["market_ticker"]) == lg and l["market_ticker"] not in used
                       and (not l.get("start") or l["start"] < time.time() + float(lc.get("max_hours_ahead", 30)) * 3600)]
            fc = cc.get("far", {}) or {}
            far_on = self.pot_state()["usd"] > 0 and fc.get("enabled", True)
            far_s = float(self.horizon().get("far_days", 3)) * 86400 - 4 * 3600  # game start that settles 3+ days out
            near_legs = [l for l in lg_legs if not far_on or not l.get("start") or l["start"] - time.time() < far_s]
            far_legs = [l for l in lg_legs if far_on and l.get("start") and l["start"] - time.time() >= far_s]
            plan = self._plan_slate(near_legs, cc, lc.get("sizes") or [2, 3, 4])
            fcc = dict(cc, elite_leg_chance=float(fc.get("elite_leg_chance", 0.80)),
                       min_hit_chance=float(fc.get("min_hit_chance", 0.20)))
            far_plan = self._plan_slate(far_legs, fcc, fc.get("sizes") or [6, 5, 4, 3]) if far_legs else []
            if not plan and not far_plan:
                if lg_legs:
                    log.info("COMBO %s: %d strong legs, not enough for a combo yet", lg, len(lg_legs))
                continue
            rows = self._run_slate(lg, plan, cc) if plan else []
            far_rows = self._run_slate(lg, far_plan, cc, pot=True) if far_plan else []
            for combo, row in list(zip(plan, rows)) + list(zip(far_plan, far_rows)):
                if row.get("placed"):
                    for l in combo:
                        used[l["market_ticker"]] = time.time()
            rows = rows + far_rows
            plan = plan + far_plan
            last_by[lg] = time.time()
            slates = [s for s in slates if s.get("league") != lg] + [
                {"league": lg, "built": time.time(), "combos": rows, "legs_available": len(lg_legs),
                 "legs_used": sum(len(c) for c in plan)}]
        try:
            self.reconcile_combos()
        except Exception as e:
            log.warning("combo reconcile failed: %s", e)
        self.combo_slate = {"slates": slates, "dry_run": cc.get("dry_run", True),
                            "borrow": getattr(self, "combo_borrow", {}),
                            "combos": [r | {"league": s["league"]} for s in slates for r in s["combos"]]}

    def check_parlay_locks(self) -> None:
        """Lock in part of a parlay once every leg but one has won: sell a share of it if the
        price we can get is close to its fair value (the last leg's live price) and at least a
        set multiple of what we paid."""
        pc = self.cfg.get("parlay_lock", {}) or {}
        if not pc.get("enabled", True) or self.mode != "live" or not self.kalshi.pkey:
            return
        lots = [l for l in self.state.data.get("lots", []) if str(l.get("whale", "")).startswith("Combo")
                and l.get("count", 0) > 0 and not l.get("locked")]
        for tk in sorted({l["ticker"] for l in lots}):
            held = self.state.position(tk, "yes")
            if held < 2:
                continue
            mine = [l for l in lots if l["ticker"] == tk]
            entry = sum(l["cost"] for l in mine) / max(1, sum(l["count"] for l in mine))
            try:
                cm = self.kalshi.market(tk)
            except Exception:
                continue
            legs = cm.get("mve_selected_legs") or cm.get("selected_legs") or []
            if not legs:
                if not getattr(self, "_legs_warned", False):
                    self._legs_warned = True
                    log.info("PARLAY lock: combo market has no legs field (keys %s)", sorted(cm.keys())[:30])
                continue
            won, pending, lost = 0, [], False
            for lg in legs:
                ltk = lg.get("market_ticker") or lg.get("ticker")
                lside = (lg.get("side") or "yes").lower()
                try:
                    lm = self.kdata.market(ltk)
                except Exception:
                    pending.append(None)
                    continue
                res = (lm.get("result") or "").lower()
                if res in ("yes", "no"):
                    if res == lside:
                        won += 1
                    else:
                        lost = True
                else:
                    pending.append((lm, lside))
            if lost or len(pending) != 1 or pending[0] is None:
                continue
            lm, lside = pending[0]
            lb, la = _px(lm, f"{lside}_bid"), _px(lm, f"{lside}_ask")
            if not lb or not la:
                continue
            fair = (lb + la) / 2  # value of the parlay now = chance the last leg hits
            min_px = max(fair * float(pc.get("min_of_fair", 0.85)), entry * float(pc.get("min_multiple", 2.0)))
            n = int(held * float(pc.get("sell_fraction", 0.5)))
            if n < 1 or min_px >= 0.99:
                continue
            sold, px = 0, None
            book_bid = _px(cm, "yes_bid")
            if book_bid and book_bid >= min_px:
                f, proceeds = self._execute(Target(tk, "yes"), "sell", n, book_bid)
                sold, px = f, (proceeds / f if f else None)
            else:
                q = self.kalshi.rfq_sell_yes(tk, n, min_px)
                if q["status"] == "sold":
                    sold, px = int(q["sold"]), q["price"]
                log.info("PARLAY lock %s: last leg %.0f%%, entry %.2f, need >= %.2f -> quote %s (%s)",
                         tk, 100 * fair, entry, min_px, q.get("best"), q["status"])
            if sold and px:
                self.state.add_position(tk, "yes", -sold)
                self.state.sell_lots(tk, "yes", sold, px, kalshi_fee(sold, px, self.fee_rate))
                self._journal("Combo lock-in", {"title": mine[0].get("title"), "outcome": "combo", "price": fair},
                              Target(tk, "yes"), "sell", sold, px)
                for l in self.state.data.get("lots", []):
                    if l["ticker"] == tk:
                        l["locked"] = True
                log.info("PARLAY LOCKED %s: sold %d @ %.2f (paid %.2f), keeping %d riding", tk, sold, px, entry,
                         self.state.position(tk, "yes"))

    def reconcile_combos(self) -> None:
        """Make the ledger match Kalshi for combo markets: record combo contracts bought through
        quotes that the ledger missed, and cancel a waiting limit order if that combo is already held."""
        if self.mode != "live" or not self.kalshi.pkey:
            return
        try:
            j = self.kalshi._req("GET", "/portfolio/positions", params={"limit": 500, "count_filter": "position"})
        except Exception as e:
            log.warning("combo reconcile: positions failed: %s", e)
            return
        rows = j.get("market_positions") or []
        summ = [(p.get("ticker", "")[-12:], p.get("position_fp", p.get("position")),
                 self.state.position(p.get("ticker") or "", "yes")) for p in rows if (p.get("ticker") or "").startswith("KXMVE")]
        if summ != getattr(self, "_last_recon", None):
            self._last_recon = summ
            log.info("COMBO reconcile: Kalshi vs ledger %s (keys %s)", summ, sorted(rows[0].keys())[:14] if rows else [])
        for p in rows:
            tk = p.get("ticker") or ""
            raw = float(p.get("position_fp") or p.get("position") or 0)
            if not tk.startswith("KXMVE"):
                # any other position Kalshi holds that our records lost: add it back
                side, held = ("yes", raw) if raw > 0 else ("no", -raw)
                have = self.state.position(tk, side)
                miss = int(held - have)
                if miss > 0:
                    cost = float(p.get("market_exposure_dollars") or 0)
                    px = round(cost / held, 4) if held and cost else 0.5
                    self.state.add_position(tk, side, miss)
                    mm = getattr(self, "mm", None)
                    name, fee = "Recovered", kalshi_fee(miss, px, self.fee_rate)
                    if mm and mm.covers(tk):  # a market-maker fill we missed during a restart
                        name, fee = f'MM · {tk[2:].split("15M")[0]} 15m', 0.0
                    self.state.open_lot(name, tk, side, miss, px, fee, tk)
                    log.info("RECOVER %d %s %s @ %.3f from Kalshi into the records", miss, tk, side.upper(), px)
                    self._net_ledger(tk)
                continue
            held = raw
            if held <= 0:
                continue  # we only buy YES on combos
            have = self.state.position(tk, "yes")
            # a waiting limit order on a combo we already hold: cancel it (avoid doubling up)
            for r in list(self.state.data.get("resting", [])):
                if r.get("ticker") == tk and str(r.get("whale", "")).startswith("Combo"):
                    try:
                        self.kalshi.cancel_order(r["id"], tk)
                        self.state.data["resting"].remove(r)
                        log.info("COMBO cancelled duplicate limit order on %s (already hold %g)", tk, held)
                    except Exception as e:
                        log.warning("COMBO cancel duplicate failed: %s", e)
            missing = int(held - have)
            if missing > 0:
                cost = float(p.get("market_exposure_dollars") or (float(p.get("market_exposure") or 0) / 100) or 0)
                px = round(cost / held, 4) if held and cost else 0.5
                title = next((r.get("title") for r in self.state.data.get("events", [])[::-1]
                              if r.get("kalshi_ticker") == tk), None) or f"combo {tk[-10:]}"
                fee = kalshi_fee(missing, px, self.fee_rate)
                self.state.add_position(tk, "yes", missing)
                self.state.open_lot("Combo (quote)", tk, "yes", missing, px, fee, title)
                pend = (self.state.data.get("combo_pending") or {}).pop(tk, None)
                if pend:
                    self.state.data["lots"][-1]["games"] = pend.get("games") or []
                    if pend.get("pot"):
                        self._pot_take(self.state.data["lots"][-1])
                log.info("COMBO reconciled %d contracts of %s @ %.3f bought through a quote", missing, tk, px)
        self.state.save()

    def _whale_prices_bad(self, picks: list[dict], cc: dict) -> tuple[bool, str]:
        """True when today's whale bets mostly can't be copied at a good price on Kalshi, and no
        good whale bet is waiting - then parlays may use the whale share's idle cash."""
        bc = cc.get("borrow", {}) or {}
        if not bc.get("enabled", True):
            return False, "off"
        today = _today()
        skips = (self.state.data.get("skips") or {}).get(today, {})
        bad_words = ("expected value", "slippage", "price", "stale", "fee")
        bad = sum(n for k, n in skips.items() if any(w in k.lower() for w in bad_words))
        copied = sum(1 for e in self.state.data.get("events", [])
                     if _local_day(e.get("time_utc", "")) == today and str(e.get("action", "")).startswith("buy")
                     and not str(e.get("whale", "")).startswith("Combo"))
        board_min = float(((self.bank.get("board") or {}).get("min_score")) or 70)
        pending = sum(1 for p in picks if p.get("kind") != "prop" and p.get("status") == "pre"
                      and p.get("ev", 0) > 0 and p.get("score", 0) >= board_min
                      and self.state.position(p.get("ticker", ""), p.get("side", "yes")) <= 0)
        share = copied / max(1, copied + bad)
        ok = (bad >= int(bc.get("min_bad_price_skips", 5)) and share <= float(bc.get("max_copy_share", 0.35))
              and pending == 0)
        why = f"whale bets today: {copied} copied, {bad} bad prices, {pending} good waiting"
        return ok, why

    def _combo_stake(self, hit: float, price: float, cc: dict, pot: bool = False) -> float:
        """Half-Kelly on the edge, capped per combo and by what's left of today's combo budget.
        Future-pot combos (3+ days out): quarter-Kelly of the pot, paid only from the pot."""
        if price <= 0 or price >= 1 or hit <= price:
            return 0.0
        if pot:
            fc = cc.get("far", {}) or {}
            pv = self.pot_value()  # quarter-Kelly on the whole balance, capped per ticket by the pot
            stake = self.equity() * (hit - price) / (1 - price) * float(fc.get("kelly_fraction", 0.25))
            stake = min(stake, pv * float(fc.get("max_combo_pct", 0.15)), self.pot_free())
            return round(stake, 2) if stake >= float(cc.get("min_stake_usd", 1.0)) else 0.0
        eq = self.sizing_equity()
        kelly = (hit - price) / (1 - price)
        stake = eq * kelly * float(cc.get("kelly_fraction", 0.5))
        stake = min(stake, eq * float(cc.get("max_combo_pct", 0.10)))
        left = eq * float(cc.get("daily_budget_pct", 0.45)) - self._combo_spent_today()
        if getattr(self, "_borrow_ok", False):  # whale share idle (bad prices): use it, keep a reserve
            reserve = eq * float((cc.get("borrow", {}) or {}).get("keep_free_pct", 0.15))
            left = max(left, self.spendable_cash() - reserve)
        stake = min(stake, left, max(0.0, self.spendable_cash() - 0.5))
        return round(stake, 2) if stake >= float(cc.get("min_stake_usd", 1.0)) else 0.0

    def _run_slate(self, lg: str, plan: list[list[dict]], cc: dict, pot: bool = False) -> list[dict]:
        from scoring import fee_per_contract
        dry = cc.get("dry_run", True)
        rows = []
        for combo in sorted(plan, key=lambda c: -self._combo_fair(c)):
            hit = self._combo_fair(combo)
            starts = [l["start"] for l in combo if l.get("start")]
            days = self.days_out(max(starts) + 4 * 3600) if starts else 0.0
            min_edge = float(cc.get("min_edge", 0.03)) + self.extra_edge(days)  # longer wait, bigger edge
            max_px = round(hit - min_edge - fee_per_contract(hit, self.fee_rate), 4)  # most we'll pay
            combo_tk, cm, errs = self._build_combo(combo)
            row = {"legs": [l["title"] for l in combo], "size": len(combo), "hit": round(hit, 3),
                   "ticker": combo_tk, "max_price": max_px, "price": None, "count": 0, "stake": 0.0,
                   "payout": 0, "action": "not offered" if not combo_tk else "", "placed": False,
                   "same_game": len({l["game"] for l in combo}) < len(combo), "pot": pot,
                   "note": "; ".join(errs[-1:]) if not combo_tk else ""}
            stake = self._combo_stake(hit, max_px, cc, pot)
            games = sorted({l["game"] for l in combo})
            if stake > 0:  # per-game cap across whale copies + parlays
                stake = max(0.0, min([stake] + [self.game_room(g) for g in games]))
                if stake < float(cc.get("min_stake_usd", 1.0)):
                    stake = 0.0
            if combo_tk and stake <= 0:
                row["action"] = "skip (budget/edge)"
            elif combo_tk and not dry and self.mode == "live":
                q = self.kalshi.rfq_buy_yes(combo_tk, stake, max_px)
                row["quote"] = q.get("best")
                if q["status"] == "filled":
                    n, px = int(q["count"]), float(q["price"])
                    fee = kalshi_fee(n, px, self.fee_rate)
                    self._commit(n * px + fee)
                    title = " + ".join(row["legs"])
                    name = f"Combo {len(combo)}-leg"
                    self.state.add_position(combo_tk, "yes", n)
                    self.state.add_spent(n * px + fee)
                    self.state.open_lot(name, combo_tk, "yes", n, px, fee, title)
                    self.state.data["lots"][-1]["games"] = games
                    if pot:
                        self._pot_take(self.state.data["lots"][-1])
                    self._journal(name, {"title": title, "outcome": "combo", "price": hit},
                                  Target(combo_tk, "yes"), "buy", n, px)
                    row.update(action="BOUGHT (quote)", price=px, count=n, stake=round(n * px, 2), payout=n, placed=True)
                elif q.get("accepted"):
                    row.update(action=f"quote accepted ({q['status']}), checking fills", price=q.get("best"), placed=True)
                    self._add_combo_spent(stake)
                    self._commit(stake)
                    self.state.data.setdefault("combo_pending", {})[combo_tk] = {"pot": pot, "games": games}
                else:
                    # no maker quote at our price: wait with a limit order a bit under fair
                    px = math.floor(max_px * 100) / 100
                    count = math.floor(stake / px) if px > 0 else 0
                    ok = count >= 1 and self._place_combo(combo_tk, combo, hit, None, px, count, False, pot)
                    if q.get("error"):
                        log.warning("COMBO quote request error: %s", q["error"])
                    row.update(action=f"LIMIT (quote {q['status']}{' ' + str(q.get('best')) if q.get('best') else ''})",
                               price=px, count=count, stake=round(count * px, 2), payout=count, placed=bool(ok))
            elif combo_tk:
                px = max_px
                count = math.floor(stake / px) if px > 0 else 0
                row.update(action="(test) would request quote", price=px, count=count, stake=round(count * px, 2), payout=count)
            log.info("COMBO %s%s %d-leg%s hit %.0f%% max %s %s -> %s %s x%d (stake $%.2f, pays $%d) %s",
                     lg, " future-pot" if pot else "", len(combo), " same-game" if row["same_game"] else "",
                     100 * hit, max_px, row["legs"],
                     row["action"], row["price"], row["count"], row["stake"], row["payout"], row["note"])
            rows.append(row)
        return rows

    def _place_combo(self, combo_tk, legs, fair, ask, price, count, take, pot: bool = False) -> bool:
        tgt = Target(combo_tk, "yes")
        name = f"Combo {len(legs)}-leg"
        title = " + ".join(l["title"] for l in legs)
        starts = [x for x in (l.get("start") for l in legs) if x]
        expires = int(min(min(starts) - 300 if starts else time.time() + 86400, time.time() + 86400))
        if expires <= time.time() + 300:
            return False
        oid = f"paper-{uuid.uuid4()}"
        if self.mode == "live":
            try:
                oid = self.kalshi.create_order(combo_tk, "yes", "buy", count, price, None,
                                               post_only=True, expiration_ts=expires).get("order_id")
            except Exception as e:
                log.error("combo limit order failed: %s", e)
                oid = None
        if oid:
            self.state.data.setdefault("resting", []).append(
                {"id": oid, "ticker": combo_tk, "side": "yes", "price": price, "count": count,
                 "remaining": count, "filled": 0, "fee_rate": 0.0175, "whale": name, "title": title,
                 "outcome": "combo", "score": None, "expires": expires, "placed": int(time.time()),
                 "pm_price": fair, "legs_tickers": ",".join(l["market_ticker"] for l in legs),
                 "games": sorted({l["game"] for l in legs}), "pot": pot})
        return bool(oid)

    def check_settlements(self) -> None:
        """Close out lots on Kalshi markets that have settled, so wins/losses are counted."""
        tickers = sorted({l["ticker"] for l in self.state.data["lots"]})
        for tk in tickers:
            try:
                m = self.kdata.market(tk)
            except Exception as e:
                log.warning("settle lookup %s failed: %s", tk, e)
                continue
            result = (m.get("result") or "").lower()
            if result and m.get("status") in ("settled", "finalized", "determined", "closed"):
                n = self.state.settle(tk, result)
                log.info("SETTLED %s result=%s (%d lots)", tk, result, n)

    def _commit(self, usd: float) -> None:
        """Money just put into an order: count it before Kalshi's balance is read again."""
        self._unsynced = getattr(self, "_unsynced", 0.0) + max(0.0, usd)

    def _execute(self, tgt: Target, action: str, count: int, px: float) -> tuple[int, float]:
        res = self._execute_raw(tgt, action, count, px)
        if action == "buy" and res[0]:
            self._commit(res[1] * (1 + self.fee_rate * (1 - px)))
        return res

    def _execute_raw(self, tgt: Target, action: str, count: int, px: float) -> tuple[int, float]:
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
        return self.state.data.get("events", [])[-n:][::-1]

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "kalshi_env": self.kalshi_env,
            "paused": self.paused,
            "whales": [{"name": w.get("name"), "address": a, "auto": bool(w.get("auto")),
                        "benched": w.get("name") in (self.state.data.get("benched_whales") or {})}
                       for a, w in self.whales.items()],
            "benched": self.state.data.get("benched_whales") or {},
            "mapped_markets": len(self.maps),
            "positions": {k: v for k, v in self.state.data["positions"].items() if v},
            "spent_today": self.state.spent_today(),
            "max_usd_per_day": self.risk["max_usd_per_day"],
            "scoreboard": self.state.scoreboard(),
            "equity": round(self.equity(), 2),
            "cash": round(self.cash(), 2),
            "lag_reserve": round(self.lag_reserve(), 2),
            "reserve_usd": self.reserve_usd(),
            "reserve_free": self.reserve_free(),
            "pot_usd": self.pot_state()["usd"],
            "pot_free": self.pot_free(),
            "pot_open": round(self.pot_open(), 2),
            "start_usd": float(self.bank.get("start_usd", 100)),
            "stage": self.current_stage().get("name"),
            "lagwatch": self.state.data.get("lagwatch") or {},
            "lag_pnl": self.lag_pnl(),
            "stage_tiers": self.current_tiers(),
            "peak": self.state.data.get("peak_equity"),
            "sizing": self.risk.get("sizing"),
            "kalshi_link": self.kalshi_link,
            "open_lots": len(self.state.data["lots"]),
            "resting": [{k: r[k] for k in ("ticker", "side", "price", "remaining", "whale", "title", "score", "expires")}
                        for r in self.state.data.get("resting", [])],
            "last_poll": self.last_poll,
            "last_error": self.last_error,
            "balance": self.balance,
        }

    # ---- logging --------------------------------------------------------- #
    def _skip(self, t, name, reason) -> None:
        log.info("  skip: %s", reason)
        day = self.state.data.setdefault("skips", {})
        today = day.setdefault(_today(), {})
        key = re.sub(r"[+-]?\d+(\.\d+)?%?", "#", reason.split("(")[0].split("'")[0]).strip()[:60]
        today[key] = today.get(key, 0) + 1
        for k in sorted(day)[:-7]:
            del day[k]

    def _journal(self, name, t, tgt, action, count, px, score=None) -> None:
        ev = self.state.data.setdefault("events", [])
        ev.append({"time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": self.mode,
                   "whale": name, "pm_title": t.get("title"), "pm_outcome": t.get("outcome"),
                   "pm_price": t.get("price"), "kalshi_ticker": tgt.ticker, "side": tgt.side,
                   "action": action, "contracts": count, "price": f"{px:.4f}", "score": score})
        del ev[:-500]
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
