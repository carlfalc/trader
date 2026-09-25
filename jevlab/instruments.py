"""GAINEDGE instrument registry, ported for the MetaApi (FX / indices / commodities) venue.

Canonical GAINEDGE symbols and the broker-specific variants to try in order, mirrored
from the GainEdge project (`_shared/instrument-registry.ts` + `broker-symbol-variants.ts`).
The bot trades by CANONICAL symbol; MetaApiClient resolves it to whatever the broker
actually serves (e.g. XAUUSD -> XAUUSD.i on an Eightcap raw account).
"""

from __future__ import annotations

# Canonical -> broker variants, tried in order (first the broker serves wins).
SYMBOL_VARIANTS: dict[str, list[str]] = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSD.i"],
    "XAGUSD": ["XAGUSD", "SILVER", "XAGUSD.i"],
    "NAS100": ["NAS100", "NDX100", "USTEC", "NAS100.i"],
    "US30": ["US30", "DJ30", "US30.i"],
    "SPX500": ["SPX500", "SP500", "SPX500.i"],
    "UK100": ["UK100", "FTSE100", "UK100.i"],
    "GER40": ["GER40", "DAX40", "DE40", "GER40.i"],
    "HK50": ["HK50", "HK50.i"],
    "JP225": ["JP225", "JPN225", "JP225.i"],
    "AUS200": ["AUS200", "AUS200.i"],
    "USOUSD": ["XTIUSD", "USOUSD", "XTIUSD.i", "WTI"],
    "UKOUSD": ["XBRUSD", "UKOUSD", "XBRUSD.i", "BRENT"],
    "XNGUSD": ["XNGUSD", "NGAS", "XNGUSD.i"],
    "XCUUSD": ["XCUUSD", "COPPER", "XCUUSD.i"],
    "AUDUSD": ["AUDUSD.i", "AUDUSD"],
    "NZDUSD": ["NZDUSD.i", "NZDUSD"],
    "EURUSD": ["EURUSD.i", "EURUSD"],
    "GBPUSD": ["GBPUSD.i", "GBPUSD"],
    "USDJPY": ["USDJPY.i", "USDJPY"],
    "USDCAD": ["USDCAD.i", "USDCAD"],
    "USDCHF": ["USDCHF.i", "USDCHF"],
    "GBPJPY": ["GBPJPY.i", "GBPJPY"],
    "EURJPY": ["EURJPY.i", "EURJPY"],
    "AUDJPY": ["AUDJPY.i", "AUDJPY"],
    "NZDJPY": ["NZDJPY.i", "NZDJPY"],
    "EURGBP": ["EURGBP.i", "EURGBP"],
    "AUDNZD": ["AUDNZD.i", "AUDNZD"],
    "CADCHF": ["CADCHF.i", "CADCHF"],
}

# display name + asset class, for the dashboard/logs. Mirrors the RON watch set.
INSTRUMENTS: dict[str, dict[str, str]] = {
    "XAUUSD": {"name": "Gold", "asset_class": "metals"},
    "XAGUSD": {"name": "Silver", "asset_class": "metals"},
    "NAS100": {"name": "NASDAQ 100", "asset_class": "index"},
    "US30": {"name": "Dow 30", "asset_class": "index"},
    "SPX500": {"name": "S&P 500", "asset_class": "index"},
    "UK100": {"name": "FTSE 100", "asset_class": "index"},
    "GER40": {"name": "DAX 40", "asset_class": "index"},
    "HK50": {"name": "Hang Seng 50", "asset_class": "index"},
    "JP225": {"name": "Nikkei 225", "asset_class": "index"},
    "AUS200": {"name": "ASX 200", "asset_class": "index"},
    "USOUSD": {"name": "WTI Crude", "asset_class": "energy"},
    "UKOUSD": {"name": "Brent Crude", "asset_class": "energy"},
    "XNGUSD": {"name": "Natural Gas", "asset_class": "energy"},
    "XCUUSD": {"name": "Copper", "asset_class": "metals"},
    "EURUSD": {"name": "EUR/USD", "asset_class": "fx"},
    "GBPUSD": {"name": "GBP/USD", "asset_class": "fx"},
    "USDJPY": {"name": "USD/JPY", "asset_class": "fx"},
    "AUDUSD": {"name": "AUD/USD", "asset_class": "fx"},
    "NZDUSD": {"name": "NZD/USD", "asset_class": "fx"},
    "USDCAD": {"name": "USD/CAD", "asset_class": "fx"},
    "USDCHF": {"name": "USD/CHF", "asset_class": "fx"},
    "GBPJPY": {"name": "GBP/JPY", "asset_class": "fx"},
    "EURJPY": {"name": "EUR/JPY", "asset_class": "fx"},
    "EURGBP": {"name": "EUR/GBP", "asset_class": "fx"},
}

# The six instruments RON continuously watches in GAINEDGE.
RON_WATCH = ["XAUUSD", "NAS100", "GER40", "HK50", "NZDUSD", "USDCAD"]


def broker_variants_for(symbol: str) -> list[str]:
    """Broker symbols to try, in order. Unknown canonical resolves to itself only."""
    return SYMBOL_VARIANTS.get(symbol, [symbol])


def display_name(symbol: str) -> str:
    return INSTRUMENTS.get(symbol, {}).get("name", symbol)


def asset_class(symbol: str) -> str:
    return INSTRUMENTS.get(symbol, {}).get("asset_class", "fx")
