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

    def _on_open(self, ws):
        self.connected = True
        self.subscribed = set()
        log.info("Kalshi live stream connected (%d markets to watch)", len(self.wanted))
        self._subscribe(sorted(self.wanted))

    def _on_message(self, ws, raw):
        try:
            j = json.loads(raw)
        except Exception:
            return
        if j.get("type") == "error":
            log.warning("kalshi ws error: %s", j.get("msg"))
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
