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

    def __init__(self, path: str):
        self.path = Path(path)
        self.db_url = os.environ.get("DATABASE_URL")
        self.data = json.loads(json.dumps(self.DEFAULT))
        loaded = None
        if self.db_url:
            try:
                import psycopg
                with psycopg.connect(self.db_url, connect_timeout=10) as c:
                    c.execute("create table if not exists copybot_state "
                              "(id text primary key, data jsonb not null, updated timestamptz default now())")
                    row = c.execute("select data from copybot_state where id='main'").fetchone()
                    loaded = row[0] if row else None
                log.info("State: using database")
            except Exception as e:
                log.error("State database unavailable, using local file: %s", e)
                self.db_url = None
        if loaded is None and self.path.exists():
            loaded = json.loads(self.path.read_text())
        if loaded:
            self.data.update(loaded)

    def save(self) -> None:
        blob = json.dumps(self.data)
        if self.db_url:
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

    def sell_lots(self, ticker: str, side: str, count: int, price: float, fee: float) -> None:
        """FIFO-close lots when we copy a whale's exit."""
        left, total = count, count
        for lot in [l for l in self.data["lots"] if l["ticker"] == ticker and l["side"] == side]:
            if left <= 0:
                break
            n = min(left, lot["count"])
            self._close(lot, n, n * price, fee * n / total, "sold")
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
        info: dict = {"start": None, "prices": {}}
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
        except Exception as e:
            log.debug("gamma lookup failed: %s", e)
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
        r = self.s.request(method, self.base + path,
                           headers=self._headers(method, path), timeout=15, **kw)
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
            if action != "buy" or "shard" not in str(e).lower():
                raise
            self.__dict__.setdefault("_shard_cache", {}).pop(ticker, None)
            self.ensure_shard_funds(ticker, need, wait_s=20)
            body["client_order_id"] = str(uuid.uuid4())
            res = self._req("POST", "/portfolio/events/orders", json=body)
        filled = float(res.get("fill_count") or 0)
        remaining = float(res.get("remaining_count") or 0)
        status = "executed" if remaining <= 0 and filled > 0 else (
            "resting" if body["time_in_force"] == "good_till_canceled" and remaining > 0 else "canceled")
        return {"order_id": res.get("order_id"), "fill_count": filled, "remaining_count": remaining,
                "status": status, "average_fill_price": res.get("average_fill_price"),
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
                 ("DELETE", "/portfolio/events/orders/batched",
                  {"json": {"orders": [{"order_id": order_id, **({"ticker": ticker} if ticker else {})}]}}),
                 ("DELETE", f"/portfolio/orders/{order_id}", {})]
        errs = []
        for method, path, kw in tries:
            try:
                self._req(method, path, **kw)
                if errs:
                    log.info("  cancel worked via %s %s (after: %s)", method, path, errs)
                return
            except Exception as e:
                errs.append(str(e)[:160])
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
        self.whale_stats: dict = self.state.data.get("whale_stats", {})  # wallet -> screener stats
        self.screen_buys: list = []  # recent buys of other top whales (from the screener)
        self.bank = cfg.get("bankroll", {}) or {}
        self.board_queue: list = []
        self.combo_picks: list = []
        self.prop_picks: list = []      # rated player props (props.py), refreshed by the dashboard thread
        self.prop_picks_new = False
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
            return self.balance + self.open_cost()
        start = float(self.bank.get("start_usd", 100))
        return start + sum(c["pnl"] for c in self.state.data["closed"])

    def cash(self) -> float:
        if self.mode == "live" and self.balance is not None:
            return self.balance
        return self.equity() - self.open_cost()

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
            budget, why = self._bankroll_budget(sc["score"], sc["ev"], held_usd)
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
            self._journal(name, t, tgt, "buy", filled, px, score=sc["score"])

    # ---- limit (resting) orders ------------------------------------------- #
    def _place_resting(self, name, t, tgt: Target, fair_px, whale_usd, stats, same, opp, ask, m, cls) -> None:
        """Rest a post-only buy at the best price that still has positive EV with the (lower) maker fee."""
        from scoring import score_bet, expected_value
        lim = self.cfg.get("limit_orders", {}) or {}
        rate = float(lim.get("maker_fee_rate", 0.0175))
        key = f"{tgt.ticker}|{tgt.side}"
        resting = self.state.data.setdefault("resting", [])
        if any(f'{x["ticker"]}|{x["side"]}' == key for x in resting):
            return self._skip(t, name, "limit order already resting on this market")
        price = math.floor(min(fair_px + 0.02, ask - 0.01) * 100) / 100
        while price >= 0.05 and expected_value(fair_px, price, stats, rate) < self.bank.get("min_ev", 0.0):
            price = round(price - 0.01, 2)
        if price < 0.05 or price > self.risk["max_entry_price"] or fair_px - price > 0.08:
            return self._skip(t, name, "no fair limit price")
        sc = score_bet(whale_px=fair_px, ask=price, whale_usd=whale_usd, stats=stats,
                       same=same, opposite=opp, rate=rate)
        held_usd = self.state.position(tgt.ticker, tgt.side) * price
        budget, why = self._bankroll_budget(sc["score"], sc["ev"], held_usd)
        if why:
            return self._skip(t, name, f"limit: {why}")
        count = math.floor(budget / price)
        if count < 1:
            return self._skip(t, name, "limit: risk limits leave no room")
        start = self.pm.game_start(t.get("conditionId", "")) if t.get("conditionId") else None
        start = start or self._kalshi_start(m)
        before = lim.get("cancel_before_start_minutes", 5) * 60
        expires = int(min(start - before, time.time() + lim.get("max_hours", 6) * 3600) if start
                      else time.time() + lim.get("max_hours", 6) * 3600)
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
                        "expires": expires, "placed": int(time.time()), "pm_price": fair_px})
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
                self.state.add_position(r["ticker"], r["side"], new)
                self.state.add_spent(new * px + fee)
                self.state.open_lot(r["whale"], r["ticker"], r["side"], new, px, fee, r["title"])
                self.state.data["lots"][-1]["score"] = r["score"]
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

    def _bankroll_budget(self, score: float, ev: float, held_usd: float) -> tuple[float, str | None]:
        """$ to stake from the bankroll for a bet with this score, or (0, reason to skip)."""
        from scoring import stake_pct
        b = self.bank
        pct = stake_pct(score, self.current_tiers())
        if pct <= 0:
            return 0.0, f"score {score} below minimum"
        if ev < b.get("min_ev", 0.0):
            return 0.0, f"expected value {100 * ev:+.1f}% after fees"
        eq = self.equity()
        if eq <= self.day_start_equity() * (1 - b.get("daily_stop_pct", 0.15)):
            return 0.0, "daily loss stop reached"
        budget = eq * pct
        budget = min(budget, eq * self.current_stage().get("max_game_pct", b.get("max_game_pct", 0.08)) - held_usd)
        budget = min(budget, eq * self.current_stage().get("max_open_pct", b.get("max_open_pct", 0.7)) - self.open_cost())
        budget = min(budget, self.cash())
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
            if (p["score"] < bc.get("min_score", 80) or p["whales"] < bc.get("min_whales", 2)
                    or p["ev"] <= bc.get("min_ev", 0.0)
                    or time.time() - p.get("latest_ts", 0) > bc.get("max_age_hours", 12) * 3600):
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
            budget, why = self._bankroll_budget(p["score"], p["ev"], 0.0)
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
        day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        bought = sum(float(l.get("cost", 0)) + float(l.get("fee", 0)) for l in self.state.data.get("lots", [])
                     if str(l.get("whale", "")).startswith("Combo") and float(l.get("opened", 0)) >= day0)
        waiting = sum(float(r.get("price", 0)) * float(r.get("remaining", 0)) for r in self.state.data.get("resting", [])
                      if str(r.get("whale", "")).startswith("Combo"))
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
        slates = list(self.combo_slate.get("slates", [])) if isinstance(self.combo_slate, dict) else []
        for lg in ("NBA", "NFL", "OTHER"):
            lc = leagues.get(lg, {}) or {}
            if not lc.get("enabled", True):
                continue
            if time.time() - float(last_by.get(lg, 0) or 0) < float(lc.get("every_hours", 24)) * 3600:
                continue
            lg_legs = [l for l in legs if self._combo_league(l["market_ticker"]) == lg and l["market_ticker"] not in used
                       and (not l.get("start") or l["start"] < time.time() + float(lc.get("max_hours_ahead", 30)) * 3600)]
            plan = self._plan_slate(lg_legs, cc, lc.get("sizes") or [2, 3, 4])
            if not plan:
                if lg_legs:
                    log.info("COMBO %s: %d strong legs, not enough for a combo yet", lg, len(lg_legs))
                continue
            rows = self._run_slate(lg, plan, cc)
            for combo, row in zip(plan, rows):
                if row.get("placed"):
                    for l in combo:
                        used[l["market_ticker"]] = time.time()
            last_by[lg] = time.time()
            slates = [s for s in slates if s.get("league") != lg] + [
                {"league": lg, "built": time.time(), "combos": rows, "legs_available": len(lg_legs),
                 "legs_used": sum(len(c) for c in plan)}]
        try:
            self.reconcile_combos()
        except Exception as e:
            log.warning("combo reconcile failed: %s", e)
        self.combo_slate = {"slates": slates, "dry_run": cc.get("dry_run", True),
                            "combos": [r | {"league": s["league"]} for s in slates for r in s["combos"]]}

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
            if not tk.startswith("KXMVE"):
                continue
            held = float(p.get("position_fp") or p.get("position") or 0)
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
                log.info("COMBO reconciled %d contracts of %s @ %.3f bought through a quote", missing, tk, px)
        self.state.save()

    def _combo_stake(self, hit: float, price: float, cc: dict) -> float:
        """Half-Kelly on the edge, capped per combo and by what's left of today's combo budget."""
        if price <= 0 or price >= 1 or hit <= price:
            return 0.0
        eq = self.equity()
        kelly = (hit - price) / (1 - price)
        stake = eq * kelly * float(cc.get("kelly_fraction", 0.5))
        stake = min(stake, eq * float(cc.get("max_combo_pct", 0.10)))
        left = eq * float(cc.get("daily_budget_pct", 0.45)) - self._combo_spent_today()
        stake = min(stake, left, max(0.0, self.cash() - 0.5))
        return round(stake, 2) if stake >= float(cc.get("min_stake_usd", 1.0)) else 0.0

    def _run_slate(self, lg: str, plan: list[list[dict]], cc: dict) -> list[dict]:
        from scoring import fee_per_contract
        dry = cc.get("dry_run", True)
        min_edge = float(cc.get("min_edge", 0.03))
        rows = []
        for combo in sorted(plan, key=lambda c: -self._combo_fair(c)):
            hit = self._combo_fair(combo)
            max_px = round(hit - min_edge - fee_per_contract(hit, self.fee_rate), 4)  # most we'll pay
            combo_tk, cm, errs = self._build_combo(combo)
            row = {"legs": [l["title"] for l in combo], "size": len(combo), "hit": round(hit, 3),
                   "ticker": combo_tk, "max_price": max_px, "price": None, "count": 0, "stake": 0.0,
                   "payout": 0, "action": "not offered" if not combo_tk else "", "placed": False,
                   "same_game": len({l["game"] for l in combo}) < len(combo),
                   "note": "; ".join(errs[-1:]) if not combo_tk else ""}
            stake = self._combo_stake(hit, max_px, cc)
            if combo_tk and stake <= 0:
                row["action"] = "skip (budget/edge)"
            elif combo_tk and not dry and self.mode == "live":
                q = self.kalshi.rfq_buy_yes(combo_tk, stake, max_px)
                row["quote"] = q.get("best")
                if q["status"] == "filled":
                    n, px = int(q["count"]), float(q["price"])
                    fee = kalshi_fee(n, px, self.fee_rate)
                    title = " + ".join(row["legs"])
                    name = f"Combo {len(combo)}-leg"
                    self.state.add_position(combo_tk, "yes", n)
                    self.state.add_spent(n * px + fee)
                    self.state.open_lot(name, combo_tk, "yes", n, px, fee, title)
                    self._journal(name, {"title": title, "outcome": "combo", "price": hit},
                                  Target(combo_tk, "yes"), "buy", n, px)
                    row.update(action="BOUGHT (quote)", price=px, count=n, stake=round(n * px, 2), payout=n, placed=True)
                elif q.get("accepted"):
                    row.update(action=f"quote accepted ({q['status']}), checking fills", price=q.get("best"), placed=True)
                    self._add_combo_spent(stake)
                else:
                    # no maker quote at our price: wait with a limit order a bit under fair
                    px = math.floor(max_px * 100) / 100
                    count = math.floor(stake / px) if px > 0 else 0
                    ok = count >= 1 and self._place_combo(combo_tk, combo, hit, None, px, count, False)
                    if q.get("error"):
                        log.warning("COMBO quote request error: %s", q["error"])
                    row.update(action=f"LIMIT (quote {q['status']}{' ' + str(q.get('best')) if q.get('best') else ''})",
                               price=px, count=count, stake=round(count * px, 2), payout=count, placed=bool(ok))
            elif combo_tk:
                px = max_px
                count = math.floor(stake / px) if px > 0 else 0
                row.update(action="(test) would request quote", price=px, count=count, stake=round(count * px, 2), payout=count)
            log.info("COMBO %s %d-leg%s hit %.0f%% max %s %s -> %s %s x%d (stake $%.2f, pays $%d) %s",
                     lg, len(combo), " same-game" if row["same_game"] else "", 100 * hit, max_px, row["legs"],
                     row["action"], row["price"], row["count"], row["stake"], row["payout"], row["note"])
            rows.append(row)
        return rows

    def _place_combo(self, combo_tk, legs, fair, ask, price, count, take) -> bool:
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
                 "pm_price": fair, "legs_tickers": ",".join(l["market_ticker"] for l in legs)})
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
        return self.state.data.get("events", [])[-n:][::-1]

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
            "scoreboard": self.state.scoreboard(),
            "equity": round(self.equity(), 2),
            "cash": round(self.cash(), 2),
            "start_usd": float(self.bank.get("start_usd", 100)),
            "stage": self.current_stage().get("name"),
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
