"""Bybit: the live price feed and order placement for the 24/7 bot (Path 2).

Two modes, set with BYBIT_MODE in .env:
  demo  (default)  Bybit Demo Trading: real market prices, fake money.
  live             Real money. Refused unless JEV_LIVE_CONFIRM is set to the exact
                   phrase in LIVE_PHRASE. Only the account owner should ever set it.

Orders are post-only limit orders at the best bid (to buy) or best ask (to sell),
so the bot never crosses the spread and pays the maker fee. An order that isn't
filled within the wait time is cancelled.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

import websocket
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from .loop import Market

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"
LIVE_PHRASE = "I accept the risk of trading real money"


class BybitError(Exception):
    pass


def bybit_mode() -> str:
    mode = os.getenv("BYBIT_MODE", "demo").strip().lower() or "demo"
    if mode not in ("demo", "live"):
        raise BybitError(f"BYBIT_MODE must be 'demo' or 'live', not '{mode}'")
    if mode == "live" and os.getenv("JEV_LIVE_CONFIRM", "").strip() != LIVE_PHRASE:
        raise BybitError("BYBIT_MODE=live, but JEV_LIVE_CONFIRM isn't set to the exact confirmation phrase. "
                         "Refusing to trade real money.")
    return mode


class BybitMarket(Market):
    """Same live snapshot as the Hyperliquid feed, from Bybit's public stream:
    best bid/ask (level 1 order book, ~25 updates a second) and every trade."""

    def __init__(self, symbol: str):
        super().__init__(symbol)
        self.symbol = symbol

    def start(self) -> None:
        topics = [f"orderbook.1.{self.symbol}", f"publicTrade.{self.symbol}"]

        def on_open(ws):
            ws.send(json.dumps({"op": "subscribe", "args": topics}))

            def heartbeat():  # Bybit drops connections that don't ping every ~20s
                while ws.sock and ws.sock.connected:
                    time.sleep(18)
                    try:
                        ws.send(json.dumps({"op": "ping"}))
                    except Exception:
                        return

            threading.Thread(target=heartbeat, daemon=True).start()

        self.app = websocket.WebSocketApp(WS_PUBLIC, on_open=on_open, on_message=lambda ws, m: self._on_bybit(m))
        threading.Thread(target=lambda: self.app.run_forever(reconnect=3), daemon=True).start()

    def _on_bybit(self, raw: str) -> None:
        msg = json.loads(raw)
        topic, data, now = msg.get("topic", ""), msg.get("data"), time.time()
        with self.lock:
            if topic.startswith("orderbook.1.") and data and data.get("b") and data.get("a"):
                bid, bsz = map(float, data["b"][0])
                ask, asz = map(float, data["a"][0])
                self.bbo = (bid, ask, bsz, asz, now)
                self.mids.append((now, (bid + ask) / 2))
                if not self.ticks or now - self.ticks[-1][0] >= 0.05:
                    self.ticks.append((now, (bid * asz + ask * bsz) / (bsz + asz), bid, ask))
                self.ready.set()
            elif topic.startswith("publicTrade.") and data:
                for tr in data:
                    self.trades.append((tr["T"] / 1000, tr["S"] == "Buy", float(tr["p"]), float(tr["v"])))
            for dq, keep in ((self.mids, 120), (self.trades, 120), (self.ticks, 300)):
                while dq and now - dq[0][0] > keep:
                    dq.popleft()


class BybitTrader:
    """Places and tracks post-only limit orders on one USDT perpetual."""

    def __init__(self, symbol: str, mode: str):
        key, secret = os.getenv("BYBIT_API_KEY", "").strip(), os.getenv("BYBIT_API_SECRET", "").strip()
        if not key or not secret:
            raise BybitError("BYBIT_API_KEY / BYBIT_API_SECRET are missing from .env")
        self.symbol, self.mode = symbol, mode
        self.http = HTTP(demo=(mode == "demo"), api_key=key, api_secret=secret, recv_window=10000)
        info = HTTP().get_instruments_info(category="linear", symbol=symbol)["result"]["list"]
        if not info:
            raise BybitError(f"{symbol} isn't a Bybit USDT perpetual")
        lot, price = info[0]["lotSizeFilter"], info[0]["priceFilter"]
        self.qty_step, self.min_qty = float(lot["qtyStep"]), float(lot["minOrderQty"])
        self.min_notional = float(lot.get("minNotionalValue") or 5)
        self.tick = float(price["tickSize"])

    # --- helpers -----------------------------------------------------------------
    def _decimals(self, step: float) -> int:
        return max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0

    def round_qty(self, qty: float) -> float:
        return round(math.floor(qty / self.qty_step + 1e-9) * self.qty_step, self._decimals(self.qty_step))

    def fmt_px(self, px: float) -> str:
        return f"{round(round(px / self.tick) * self.tick, self._decimals(self.tick)):.{self._decimals(self.tick)}f}"

    def _call(self, fn, **kw) -> dict:
        try:
            res = fn(**kw)
        except Exception as exc:  # pybit raises on non-zero retCode
            raise BybitError(str(exc)[:300]) from exc
        if res.get("retCode") not in (0, None):
            raise BybitError(f"{res.get('retCode')}: {res.get('retMsg')}")
        return res.get("result", {})

    # --- account -------------------------------------------------------------------
    def equity_usdt(self) -> float | None:
        res = self._call(self.http.get_wallet_balance, accountType="UNIFIED")
        for acct in res.get("list", []):
            if acct.get("totalEquity"):
                return float(acct["totalEquity"])
        return None

    def position(self) -> tuple[float, float]:
        """Signed size in coins (+ long, - short) and average entry price."""
        rows = self._call(self.http.get_positions, category="linear", symbol=self.symbol).get("list", [])
        size = sum(float(r["size"]) * (1 if r["side"] == "Buy" else -1) for r in rows if float(r.get("size") or 0))
        avg = next((float(r["avgPrice"]) for r in rows if float(r.get("size") or 0)), 0.0)
        return size, avg

    # --- orders --------------------------------------------------------------------
    def place_post_only(self, side: str, qty: float, px: float, reduce_only: bool = False) -> str:
        res = self._call(self.http.place_order, category="linear", symbol=self.symbol, side="Buy" if side == "buy" else "Sell",
                         orderType="Limit", qty=str(qty), price=self.fmt_px(px), timeInForce="PostOnly",
                         reduceOnly=reduce_only, orderLinkId=f"jev-{int(time.time() * 1000)}")
        return res["orderId"]

    def order(self, order_id: str) -> dict:
        """Current state of one order: status, filled qty, average price, fee paid."""
        for fn in (self.http.get_open_orders, self.http.get_order_history):
            rows = self._call(fn, category="linear", symbol=self.symbol, orderId=order_id).get("list", [])
            if rows:
                r = rows[0]
                return {"status": r["orderStatus"], "filled": float(r.get("cumExecQty") or 0),
                        "avg_px": float(r.get("avgPrice") or 0), "fee": float(r.get("cumExecFee") or 0)}
        return {"status": "Unknown", "filled": 0.0, "avg_px": 0.0, "fee": 0.0}

    def cancel(self, order_id: str) -> None:
        try:
            self._call(self.http.cancel_order, category="linear", symbol=self.symbol, orderId=order_id)
        except BybitError:
            pass  # already filled or gone

    def cancel_all(self) -> None:
        try:
            self._call(self.http.cancel_all_orders, category="linear", symbol=self.symbol)
        except BybitError:
            pass

    def flatten(self) -> dict | None:
        """Close any open position with a reduce-only market order (kill switch, loss limit, exit).
        Returns the closing fill (signed qty, price, fee) so the P&L includes it."""
        size, _ = self.position()
        if abs(size) < self.min_qty:
            return None
        res = self._call(self.http.place_order, category="linear", symbol=self.symbol, side="Sell" if size > 0 else "Buy",
                         orderType="Market", qty=str(self.round_qty(abs(size))), reduceOnly=True)
        for _ in range(10):  # market orders fill almost instantly; give it a moment to report
            time.sleep(0.3)
            o = self.order(res["orderId"])
            if o["status"] == "Filled":
                return {"dq": -size, "px": o["avg_px"], "fee": o["fee"]}
        return {"dq": -size, "px": 0.0, "fee": 0.0}
