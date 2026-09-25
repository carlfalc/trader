"""Crypto headlines from public RSS feeds, and the questions Jev answers about each one."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests

FEEDS = {
    "cointelegraph": "https://cointelegraph.com/rss",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "decrypt": "https://decrypt.co/feed",
    "theblock": "https://www.theblock.co/rss.xml",
}
COINS = ["BTC", "ETH", "SOL"]
QUESTIONS = {
    "asset": {
        "type": "choice",
        "instructions": "Which tradable asset does this news most directly affect?",
        "criteria": {"BTC": None, "ETH": None, "SOL": None, "whole_crypto_market": None, "none": None},
    },
    "direction": {
        "type": "choice",
        "instructions": "Is this news likely to push that asset's price up or down from here?",
        "criteria": {"bullish": None, "bearish": None, "neutral": None},
    },
    "impact": {
        "type": "score",
        "instructions": "How much is this news likely to move that asset's price?",
        "criteria": ["Noise", "Minor", "Notable", "Major"],
    },
}

def fetch_headlines(hours: float, quiet: bool = False) -> list[dict]:
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=hours)
    items, seen = [], set()
    for source, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "jev-lab/0.1 rss reader"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:
            if not quiet:
                print(f"  ! feed {source} unavailable: {str(exc)[:80]}")
            continue
        n = 0
        for it in root.iter("item"):
            title = html.unescape((it.findtext("title") or "").strip())
            pub = it.findtext("pubDate")
            if not title or not pub or title.lower() in seen:
                continue
            ts = pd.Timestamp(parsedate_to_datetime(pub)).tz_convert("UTC")
            if ts < cutoff:
                continue
            summary = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:400]
            seen.add(title.lower())
            items.append({"source": source, "headline": title, "summary": summary, "published": ts})
            n += 1
        if not quiet:
            print(f"  feed {source:<14} {n} headlines")
    return sorted(items, key=lambda x: x["published"])


def judge_call(ans: dict) -> tuple[str | None, int]:
    asset = ans["asset"]["choice"]
    coin = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "whole_crypto_market": "BTC"}.get(asset)
    side = {"bullish": 1, "bearish": -1}.get(ans["direction"]["choice"], 0)
    if ans["impact"]["score"] < 2:  # only act on "Notable" or "Major"
        side = 0
    return coin, side
