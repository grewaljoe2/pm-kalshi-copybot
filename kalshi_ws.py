"""
Live Kalshi prices over websocket (ticker channel): best YES bid/ask for each subscribed market,
updated the moment they change, instead of polling the (sometimes seconds-stale) market listing.

KalshiStream(kalshi_client).start()   -> background thread, reconnects on its own
stream.watch(["KXBTC15M-...", ...])   -> add markets (new ones are subscribed on the fly)
stream.quote("KX...")                 -> {"yes_bid": .47, "yes_ask": .49, "ts": epoch} or None
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time

log = logging.getLogger("kalshi_ws")


def _d(msg: dict, name: str):
    v = msg.get(f"{name}_dollars")
    if v not in (None, ""):
        return float(v)
    v = msg.get(name)
    if v in (None, ""):
        return None
    v = float(v)
    return v / 100 if v > 1 else v


class KalshiStream:
    def __init__(self, kalshi):
        self.k = kalshi
        host = kalshi.base.split("://", 1)[1].split("/", 1)[0]
        self.url = f"wss://{host}/trade-api/ws/v2"
        self.quotes: dict[str, dict] = {}
        self.wanted: set[str] = set()
        self.subscribed: set[str] = set()
        self.ws = None
        self.lock = threading.Lock()
        self.connected = False
        self.msgs = 0
        self._id = 0
        # live order books (orderbook_delta channel): ticker -> {"yes": {px: qty}, "no": {px: qty}, "sid", "ok"}
        self.books: dict[str, dict] = {}
        self.book_wanted: set[str] = set()
        self.book_subscribed: set[str] = set()
        self._sid_seq: dict[int, int] = {}

    # ---- auth (same RSA/Ed25519 signature as REST, path /trade-api/ws/v2) ---- #
    def _headers(self) -> list[str]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        ts = str(int(time.time() * 1000))
        msg = (ts + "GET" + "/trade-api/ws/v2").encode()
        pk = self.k.pkey
        if isinstance(pk, Ed25519PrivateKey):
            sig = pk.sign(msg)
        else:
            sig = pk.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                          hashes.SHA256())
        return [f"KALSHI-ACCESS-KEY: {self.k.key_id}", f"KALSHI-ACCESS-SIGNATURE: {base64.b64encode(sig).decode()}",
                f"KALSHI-ACCESS-TIMESTAMP: {ts}"]

    # ---- public ------------------------------------------------------------ #
    def watch(self, tickers) -> None:
        new = set(tickers) - self.wanted
        if not new:
            return
        with self.lock:
            self.wanted |= new
        self._subscribe(sorted(new))

    def watch_books(self, tickers) -> None:
        new = set(tickers) - self.book_wanted
        if not new:
            return
        with self.lock:
            self.book_wanted |= new
        self._subscribe_books(sorted(new))

    def prune_books(self, keep) -> None:
        """Forget books of markets no longer watched (closed windows)."""
        keep = set(keep)
        with self.lock:
            for t in [t for t in self.books if t not in keep]:
                del self.books[t]
            self.book_wanted &= keep

    def local_ask(self, ticker: str, side: str) -> tuple[float | None, int] | None:
        """Best ask and size for `side` from our live copy of the book (None if we don't trust it)."""
        b = self.books.get(ticker)
        if not b or not b.get("ok") or not self.connected:
            return None
        other = b["no" if side == "yes" else "yes"]
        live = [(p, q) for p, q in other.items() if q > 0]
        if not live:
            return None, 0
        p, q = max(live)
        return round(1 - p, 4), int(q)

    def quote(self, ticker: str, max_age: float = 15.0) -> dict | None:
        q = self.quotes.get(ticker)
        if not q or time.time() - q["ts"] > max_age:
            return None
        return q

    def start(self) -> "KalshiStream":
        threading.Thread(target=self._run, name="kalshi-ws", daemon=True).start()
        return self

    # ---- internals ------------------------------------------------------------ #
    def _subscribe(self, tickers: list[str]) -> None:
        if not self.connected or not self.ws or not tickers:
            return
        for i in range(0, len(tickers), 100):
            self._id += 1
            try:
                self.ws.send(json.dumps({"id": self._id, "cmd": "subscribe",
                                         "params": {"channels": ["ticker"], "market_tickers": tickers[i:i + 100]}}))
                self.subscribed |= set(tickers[i:i + 100])
            except Exception as e:
                log.warning("kalshi ws subscribe failed: %s", e)

    def _subscribe_books(self, tickers: list[str]) -> None:
        if not self.connected or not self.ws or not tickers:
            return
        for i in range(0, len(tickers), 50):
            self._id += 1
            try:
                self.ws.send(json.dumps({"id": self._id, "cmd": "subscribe",
                                         "params": {"channels": ["orderbook_delta"], "market_tickers": tickers[i:i + 50]}}))
                self.book_subscribed |= set(tickers[i:i + 50])
            except Exception as e:
                log.warning("kalshi ws book subscribe failed: %s", e)

    @staticmethod
    def _levels(msg: dict, side: str) -> dict:
        for key in (f"{side}_dollars_fp", f"{side}_dollars", side):
            lv = msg.get(key)
            if lv is None:
                continue
            out = {}
            for row in lv:
                try:
                    p, q = float(row[0]), float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
                p = p / 100 if p > 1 else p
                out[round(p, 4)] = q
            return out
        return {}

    def _on_book(self, j: dict) -> None:
        m = j.get("msg") or {}
        tk = m.get("market_ticker")
        sid, seq = j.get("sid"), j.get("seq")
        if sid is not None and seq is not None:  # every message on a subscription is numbered: a gap = missed update
            last = self._sid_seq.get(sid)
            self._sid_seq[sid] = seq
            if last is not None and seq != last + 1 and j.get("type") == "orderbook_delta":
                bad = [t for t, b in self.books.items() if b.get("sid") == sid]
                for t in bad:
                    self.books[t]["ok"] = False
                log.warning("kalshi ws: missed book update (sid %s, %s -> %s); re-syncing %d books", sid, last, seq, len(bad))
                try:  # drop the broken subscription, then get fresh snapshots
                    self._id += 1
                    self.ws.send(json.dumps({"id": self._id, "cmd": "unsubscribe", "params": {"sids": [sid]}}))
                except Exception:
                    pass
                self._sid_seq.pop(sid, None)
                self._subscribe_books(bad)
                return
        if not tk:
            return
        if j.get("type") == "orderbook_snapshot":
            self.books[tk] = {"yes": self._levels(m, "yes"), "no": self._levels(m, "no"), "sid": sid, "ok": True,
                              "ts": time.time()}
            return
        b = self.books.get(tk)
        if not b or b.get("sid") != sid:
            return  # update from an old subscription
        side = m.get("side")
        if side not in ("yes", "no"):
            return
        try:
            p = m.get("price_dollars") if m.get("price_dollars") not in (None, "") else m.get("price")
            p = float(p)
            p = p / 100 if p > 1 else p
            d = m.get("delta_fp") if m.get("delta_fp") not in (None, "") else m.get("delta")
            d = float(d)
        except (TypeError, ValueError):
            return
        p = round(p, 4)
        q = b[side].get(p, 0.0) + d
        if q <= 1e-9:
            b[side].pop(p, None)
        else:
            b[side][p] = q
        b["ts"] = time.time()

    def _on_open(self, ws):
        self.connected = True
        self.subscribed = set()
        self.book_subscribed = set()
        self._sid_seq = {}
        for b in self.books.values():
            b["ok"] = False  # stale until a fresh snapshot arrives
        log.info("Kalshi live stream connected (%d markets to watch)", len(self.wanted))
        self._subscribe(sorted(self.wanted))
        self._subscribe_books(sorted(self.book_wanted))

    def _on_message(self, ws, raw):
        try:
            j = json.loads(raw)
        except Exception:
            return
        if j.get("type") == "error":
            log.warning("kalshi ws error: %s", j.get("msg"))
            return
        if j.get("type") in ("orderbook_snapshot", "orderbook_delta"):
            if not getattr(self, "_logged_book", False):
                self._logged_book = True
                log.info("KALSHI_BOOK first message: %s", raw[:300] if isinstance(raw, str) else str(raw)[:300])
            self._on_book(j)
            return
        if j.get("type") != "ticker":
            return
        m = j.get("msg") or {}
        tk = m.get("market_ticker")
        if not tk:
            return
        bid, ask = _d(m, "yes_bid"), _d(m, "yes_ask")
        q = self.quotes.setdefault(tk, {})
        if bid is not None:
            q["yes_bid"] = bid
        if ask is not None:
            q["yes_ask"] = ask
        q["ts"] = time.time()
        self.msgs += 1

    def _on_close(self, ws, *a):
        self.connected = False

    def _run(self):
        import websocket  # websocket-client
        backoff = 2
        while True:
            if not self.k.pkey:
                time.sleep(30)
                continue
            try:
                self.ws = websocket.WebSocketApp(self.url, header=self._headers(), on_open=self._on_open,
                                                 on_message=self._on_message, on_close=self._on_close,
                                                 on_error=lambda ws, e: log.warning("kalshi ws: %s", e))
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
                backoff = 2
            except Exception as e:
                log.warning("kalshi ws crashed: %s", e)
            self.connected = False
            time.sleep(backoff)
            backoff = min(60, backoff * 2)
