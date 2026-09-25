"""Hyperliquid public market data. Mainnet, read-only, no key, no orders.

Fills in every lab are simulated against this real data with Hyperliquid's
real base-tier perp fees. Nothing here can place an order.
"""

from __future__ import annotations

import time

import pandas as pd
import requests

INFO_URL = "https://api.hyperliquid.xyz/info"

# Hyperliquid perps, base fee tier (per side).
TAKER_FEE = 0.00045
MAKER_FEE = 0.00015


def _info(body: dict, timeout: float = 10.0):
    r = requests.post(INFO_URL, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def l2_book(coin: str) -> dict:
    """Best 20 levels a side, as floats: {"bids": [(px, sz)], "asks": [...], "time": ms}."""
    d = _info({"type": "l2Book", "coin": coin})
    bids, asks = d["levels"]
    return {
        "bids": [(float(l["px"]), float(l["sz"])) for l in bids],
        "asks": [(float(l["px"]), float(l["sz"])) for l in asks],
        "time": d["time"],
    }


def mids(coins: list[str]) -> dict[str, float]:
    d = _info({"type": "allMids"})
    return {c: float(d[c]) for c in coins if c in d}


def candles(coin: str, interval: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
    """OHLCV candles, indexed by open time (UTC). Pages through the 5000-bar cap."""
    end_ms = end_ms or int(time.time() * 1000)
    rows, cursor = [], start_ms
    while cursor < end_ms:
        batch = _info(
            {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms}}
        )
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1]["t"]
        if last_open <= cursor or len(batch) < 2:
            break
        cursor = last_open + 1
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).drop_duplicates("t")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    out = df[["o", "h", "l", "c", "v"]].astype(float)
    out.columns = ["open", "high", "low", "close", "volume"]
    return out.sort_index()
