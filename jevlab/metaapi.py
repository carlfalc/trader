"""MetaApi venue for the hybrid bot: live FX / indices / commodities feed and trading
through a MetaApi-linked MetaTrader account (metaapi.cloud), mirroring the GainEdge
Supabase edge functions but as a plain Python REST client (no SDK needed).

Two REST hosts, per MetaApi's client API:
  CLIENT_URL  : account info, current price, positions, order placement
  MARKET_URL  : historical candles (used for Claude's market summary)

Auth is the `auth-token` header carrying METAAPI_TOKEN. The account must be a
*deployed* MetaApi account (that's what needs credits). Start with a DEMO account.

Set in .env:
  METAAPI_TOKEN                your MetaApi API token
  METAAPI_ACCOUNT_ID           the provisioned MetaApi account id (uuid-ish, 16-80 chars)
  METAAPI_REGION               london (default) | new-york | singapore ...
  METAAPI_ACCOUNT_TYPE         demo (default) | live   -- live needs JEV_LIVE_CONFIRM too
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections import deque

import requests
from dotenv import load_dotenv

from .instruments import broker_variants_for

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

LIVE_PHRASE = "I accept the risk of trading real money"


class MetaApiError(Exception):
    pass


def _region() -> str:
    return (os.getenv("METAAPI_REGION", "london").strip() or "london").lower()


def client_url() -> str:
    return f"https://mt-client-api-v1.{_region()}.agiliumtrade.ai"


def market_url() -> str:
    return f"https://mt-market-data-client-api-v1.{_region()}.agiliumtrade.ai"


def metaapi_mode() -> str:
    """demo (default) or live. live is refused without the exact confirmation phrase."""
    mode = (os.getenv("METAAPI_ACCOUNT_TYPE", "demo").strip().lower() or "demo")
    if mode not in ("demo", "live"):
        raise MetaApiError(f"METAAPI_ACCOUNT_TYPE must be 'demo' or 'live', not '{mode}'")
    if mode == "live" and os.getenv("JEV_LIVE_CONFIRM", "").strip() != LIVE_PHRASE:
        raise MetaApiError("METAAPI_ACCOUNT_TYPE=live, but JEV_LIVE_CONFIRM isn't set to the exact "
                           "confirmation phrase. Refusing to trade real money.")
    return mode


class MetaApiClient:
    """Thin REST client for one MetaApi account. Resolves canonical symbols to the
    broker's own variant (XAUUSD -> XAUUSD.i etc.) and caches what worked."""

    def __init__(self, timeout: float = 20.0):
        self.token = os.getenv("METAAPI_TOKEN", "").strip()
        self.account_id = os.getenv("METAAPI_ACCOUNT_ID", "").strip()
        if not self.token:
            raise MetaApiError("METAAPI_TOKEN is missing from .env")
        if not self.account_id:
            raise MetaApiError("METAAPI_ACCOUNT_ID is missing from .env")
        self.timeout = timeout
        self.mode = metaapi_mode()
        self._resolved: dict[str, str] = {}  # canonical -> broker symbol that worked
        self._headers = {"auth-token": self.token, "Content-Type": "application/json"}

    # ---- low-level ----------------------------------------------------------
    def _get(self, host: str, path: str) -> tuple[int, object]:
        r = requests.get(f"{host}/users/current/accounts/{self.account_id}{path}",
                         headers={"auth-token": self.token}, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"message": r.text[:300]}

    def _post(self, path: str, body: dict) -> tuple[int, object]:
        r = requests.post(f"{client_url()}/users/current/accounts/{self.account_id}{path}",
                          headers=self._headers, json=body, timeout=self.timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"message": r.text[:300]}

    # ---- account ------------------------------------------------------------
    def account_information(self) -> dict:
        code, data = self._get(client_url(), "/accountInformation")
        if code != 200 or not isinstance(data, dict) or data.get("balance") is None:
            msg = data.get("message") if isinstance(data, dict) else str(data)
            raise MetaApiError(f"accountInformation failed (HTTP {code}): {msg}")
        return data

    # ---- market data --------------------------------------------------------
    def current_price(self, symbol: str) -> dict | None:
        """{'bid','ask','broker_symbol','time'} or None. Tries broker variants, caches the winner."""
        variants = [self._resolved[symbol]] if symbol in self._resolved else broker_variants_for(symbol)
        for v in variants:
            code, p = self._get(client_url(), f"/symbols/{requests.utils.quote(v)}/current-price")
            if code == 200 and isinstance(p, dict) and isinstance(p.get("bid"), (int, float)) \
                    and isinstance(p.get("ask"), (int, float)):
                self._resolved[symbol] = v
                return {"bid": float(p["bid"]), "ask": float(p["ask"]),
                        "broker_symbol": v, "time": p.get("time")}
        return None

    def broker_symbol(self, symbol: str) -> str:
        return self._resolved.get(symbol, broker_variants_for(symbol)[0])

    def candles(self, symbol: str, timeframe: str = "5m", limit: int = 60) -> list[dict]:
        """Recent OHLCV for Claude's market summary. Empty list on failure."""
        start = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - limit * 6 * 60 * 60))
        for v in ([self._resolved[symbol]] if symbol in self._resolved else broker_variants_for(symbol)):
            path = (f"/historical-market-data/symbols/{requests.utils.quote(v)}"
                    f"/timeframes/{timeframe}/candles?startTime={start}&limit={int(limit)}")
            code, data = self._get(market_url(), path)
            if code == 200 and isinstance(data, list) and data:
                return data
        return []

    # ---- positions ----------------------------------------------------------
    def positions(self) -> list[dict]:
        code, data = self._get(client_url(), "/positions")
        return data if code == 200 and isinstance(data, list) else []

    def net_position(self, symbol: str) -> tuple[float, list[str]]:
        """Signed volume in lots (+long/-short) across our symbol's broker variants, and their ids."""
        wanted = set(broker_variants_for(symbol)) | {self.broker_symbol(symbol)}
        vol, ids = 0.0, []
        for p in self.positions():
            if p.get("symbol") in wanted:
                sign = 1 if p.get("type") == "POSITION_TYPE_BUY" else -1
                vol += sign * float(p.get("volume") or 0)
                ids.append(p["id"])
        return vol, ids

    # ---- trading ------------------------------------------------------------
    def market_order(self, symbol: str, side: str, volume: float,
                     stop_loss: float | None = None, take_profit: float | None = None) -> dict:
        """Place a market buy/sell for `volume` lots on the resolved broker symbol."""
        action = "ORDER_TYPE_BUY" if side == "buy" else "ORDER_TYPE_SELL"
        last = {}
        for v in ([self._resolved[symbol]] if symbol in self._resolved else broker_variants_for(symbol)):
            body: dict = {"actionType": action, "symbol": v, "volume": round(float(volume), 2)}
            if stop_loss is not None:
                body["stopLoss"] = float(stop_loss)
            if take_profit is not None:
                body["takeProfit"] = float(take_profit)
            code, data = self._post("/trade", body)
            if code == 200:
                self._resolved[symbol] = v
                return data if isinstance(data, dict) else {}
            last = data if isinstance(data, dict) else {"message": str(data)}
            msg = str(last).lower()
            if "symbol" in msg and ("not found" in msg or "not exist" in msg):
                continue  # try next broker variant
            raise MetaApiError(f"order rejected: {last.get('message') or last.get('error') or last}")
        raise MetaApiError(f"no valid broker symbol for {symbol}: {last.get('message') if last else ''}")

    def close_position(self, position_id: str) -> None:
        code, data = self._post("/trade", {"actionType": "POSITION_CLOSE_ID", "positionId": position_id})
        if code != 200:
            raise MetaApiError(f"close failed: {data.get('message') if isinstance(data, dict) else data}")

    def flatten(self, symbol: str) -> int:
        """Close every open position on this symbol, retrying until flat (handles hedging
        accounts where positions can stack). Returns how many were closed."""
        closed = 0
        for _ in range(3):
            vol, ids = self.net_position(symbol)
            if abs(vol) < 1e-9 and not ids:
                break
            for pid in ids:
                try:
                    self.close_position(pid)
                    closed += 1
                except MetaApiError:
                    pass
            time.sleep(0.3)
        return closed

    def close_all(self, side: str | None = None) -> dict:
        """Close open positions on the account, retrying until clear. side=None closes
        everything; side='buy'/'sell' closes only longs/shorts. Returns {closed, profit}."""
        want = {"buy": "POSITION_TYPE_BUY", "sell": "POSITION_TYPE_SELL"}.get(side)
        seen: dict[str, float] = {}
        for _ in range(5):
            ps = [p for p in self.positions() if want is None or p.get("type") == want]
            if not ps:
                break
            for p in ps:
                pid = p.get("id")
                if pid is None:
                    continue
                seen.setdefault(pid, float(p.get("profit") or p.get("unrealizedProfit") or 0))
                try:
                    self.close_position(pid)
                except MetaApiError:
                    pass
            time.sleep(0.4)
        return {"closed": len(seen), "profit": round(sum(seen.values()), 2)}


class MetaApiMarket:
    """Live one-symbol feed by polling MetaApi current-price, exposing the same
    interface the bot expects from the crypto Market classes: start(), ready,
    snapshot(), trades_since(), recent_ticks().

    MetaApi's current-price gives only bid/ask (no depth or trade tape), so the
    Jev state here is the price-action subset: spread, short-horizon returns and
    tick volatility. strategy.decide() reads everything via .get(), so the missing
    order-flow fields simply don't fire."""

    def __init__(self, client: MetaApiClient, symbol: str, poll_s: float = 1.0):
        self.client = client
        self.symbol = symbol
        self.poll_s = poll_s
        self.lock = threading.Lock()
        self.bbo = None  # (bid, ask, t)
        self.mids: deque = deque()   # (t, mid)
        self.ticks: deque = deque()  # (t, mid, bid, ask)
        self.ready = threading.Event()
        self.market_open = True
        self.last_error: str | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        misses = 0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                p = self.client.current_price(self.symbol)
            except Exception as exc:
                p, self.last_error = None, str(exc)[:160]
            now = time.time()
            if p:
                misses = 0
                self.market_open = True
                self.last_error = None
                with self.lock:
                    self.bbo = (p["bid"], p["ask"], now)
                    mid = (p["bid"] + p["ask"]) / 2
                    self.mids.append((now, mid))
                    self.ticks.append((now, mid, p["bid"], p["ask"]))
                    for dq, keep in ((self.mids, 120), (self.ticks, 300)):
                        while dq and now - dq[0][0] > keep:
                            dq.popleft()
                self.ready.set()
            else:
                misses += 1
                if misses >= 5:  # market closed (weekend/holiday) or symbol unavailable
                    self.market_open = False
            self._stop.wait(max(0.0, self.poll_s - (time.time() - t0)))

    def snapshot(self) -> dict:
        with self.lock:
            if not self.bbo:
                return {}
            bid, ask, _ = self.bbo
            now = time.time()
            mid = (bid + ask) / 2

            def ret(sec: float) -> float:
                past = next((m for t, m in self.mids if t >= now - sec), mid)
                return round(1e4 * (mid / past - 1), 2) if past else 0.0

            m60 = [m for t, m in self.mids if t >= now - 60]
            steps = [1e4 * (b / a - 1) for a, b in zip(m60, m60[1:]) if a]
            state = {
                "spread_bps": round(1e4 * (ask - bid) / mid, 3) if mid else 0.0,
                "return_5s_bps": ret(5),
                "return_30s_bps": ret(30),
                "return_60s_bps": ret(60),
                "tick_volatility_60s_bps": round(statistics.pstdev(steps), 3) if len(steps) > 2 else 0.0,
            }
            return {"mid": mid, "micro": mid, "bid": bid, "ask": ask,
                    "spread_bps": state["spread_bps"], "state": state}

    def trades_since(self, t0: float) -> list:
        return []  # MetaApi current-price has no trade tape

    def recent_ticks(self, seconds: float = 120) -> list[list[float]]:
        with self.lock:
            now = time.time()
            return [[round(t, 3), m, b, a] for t, m, b, a in self.ticks if t >= now - seconds]
