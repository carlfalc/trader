"""Claude as the big-picture brain (Path 2).

Every few minutes, Claude reads a plain summary of the market (recent price moves,
volatility, funding, and the latest crypto headlines) and sets the bias for the
next stretch: long, short, or flat. Jev and strategy.py still make the fast calls,
but only in the direction Claude allows.

Uses the same Vercel AI Gateway key as Jev. The model is CLAUDE_MODEL in .env
(default anthropic/claude-sonnet-5). Claude on the gateway needs AI Gateway
credits (the paid tier).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

CHAT_URL = "https://ai-gateway.vercel.sh/v1/chat/completions"
BYBIT_REST = "https://api.bybit.com/v5/market"


def market_summary(symbol: str) -> dict:
    """Plain numbers Claude can reason about, from Bybit's public market data."""
    k = requests.get(f"{BYBIT_REST}/kline", params={"category": "linear", "symbol": symbol, "interval": "5", "limit": 49},
                     timeout=10).json()["result"]["list"]
    closes = [float(r[4]) for r in reversed(k)]  # oldest -> newest, 5-minute bars, last ~4 hours
    t = requests.get(f"{BYBIT_REST}/tickers", params={"category": "linear", "symbol": symbol}, timeout=10).json()["result"]["list"][0]
    pct = lambda a, b: round(100 * (b / a - 1), 2)
    moves = [abs(pct(a, b)) for a, b in zip(closes, closes[1:])]
    return {
        "symbol": symbol,
        "price": closes[-1],
        "change_15m_pct": pct(closes[-4], closes[-1]),
        "change_1h_pct": pct(closes[-13], closes[-1]),
        "change_4h_pct": pct(closes[0], closes[-1]),
        "change_24h_pct": round(100 * float(t["price24hPcnt"]), 2),
        "avg_5m_move_pct": round(sum(moves) / len(moves), 3),
        "high_4h": max(closes), "low_4h": min(closes),
        "funding_rate_pct": round(100 * float(t.get("fundingRate") or 0), 4),
    }


def headlines(limit: int = 8) -> list[str]:
    try:
        from .news import fetch_headlines
        return [h["headline"] for h in fetch_headlines(6, quiet=True)][-limit:]
    except Exception:
        return []


PROMPT = """You are the portfolio manager for a small, cautious crypto trading bot.
A fast AI makes buy/sell calls every second, but it can only trade in the direction you allow.
Decide the bias for the next {minutes} minutes: "long" (only buying allowed), "short" (only selling allowed),
or "flat" (stay out, close any position).
If the evidence leans one way (momentum across timeframes, funding, the news), pick that side and let
"confidence" say how strongly it leans. Choose "flat" only when the signals genuinely conflict or the market is dead.

Market data:
{summary}

Latest crypto headlines:
{news}

Reply with ONLY a JSON object: {{"bias": "long" | "short" | "flat", "confidence": 0.0-1.0, "reason": "one short sentence"}}"""


class Brain:
    # Candidate model slugs, tried in order until one works. The configured CLAUDE_MODEL
    # goes first; the rest are current Vercel AI Gateway Anthropic slugs as fallbacks, so
    # the brain self-heals if the configured name is wrong or retired.
    FALLBACK_MODELS = [
        "anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4",
        "anthropic/claude-opus-5.5", "anthropic/claude-3.7-sonnet", "anthropic/claude-3.5-sonnet",
    ]

    def __init__(self, symbol: str, every_min: float, summary_fn=None):
        self.key = os.getenv("AI_GATEWAY_API_KEY", "").strip()
        configured = os.getenv("CLAUDE_MODEL", "anthropic/claude-sonnet-4.5").strip()
        self.models = [configured] + [m for m in self.FALLBACK_MODELS if m != configured]
        self.model = configured
        self.symbol, self.every = symbol, every_min
        # summary_fn(symbol) -> dict of plain numbers. Defaults to the Bybit crypto summary;
        # the FX/CFD bot passes a MetaApi-based one instead.
        self.summary_fn = summary_fn or market_summary
        self.state = {"bias": None, "confidence": None, "reason": "waiting for the brain's first read",
                      "t": None, "model": self.model, "ms": None, "error": None, "degraded": False}
        self.lock = threading.Lock()

    def _ask_model(self, model: str, content: str) -> dict:
        body = {"model": model, "temperature": 0, "max_tokens": 200,
                "messages": [{"role": "user", "content": content}]}
        r = requests.post(CHAT_URL, headers={"Authorization": f"Bearer {self.key}"}, json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        text = r.json()["choices"][0]["message"]["content"]
        return json.loads(re.search(r"\{.*\}", text, re.S).group(0))

    def think(self) -> None:
        t0 = time.time()
        try:
            summary = self.summary_fn(self.symbol)
            news = "\n".join(f"- {h}" for h in headlines()) or "- (none)"
            content = PROMPT.format(minutes=self.every, summary=json.dumps(summary, indent=1), news=news)
            out, used, last_err = None, self.model, None
            for m in [self.model] + [x for x in self.models if x != self.model]:  # working model first
                try:
                    out = self._ask_model(m, content)
                    used = m
                    break
                except Exception as exc:
                    last_err = str(exc)[:160]
            if out is None:
                raise RuntimeError(f"no Claude model available (last: {last_err})")
            self.model = used  # cache whichever worked
            bias = out.get("bias") if out.get("bias") in ("long", "short", "flat") else "flat"
            with self.lock:
                self.state.update(bias=bias, confidence=float(out.get("confidence") or 0),
                                  reason=str(out.get("reason", ""))[:160], t=time.time(),
                                  ms=round((time.time() - t0) * 1000), error=None, degraded=False,
                                  model=used, summary=summary)
        except Exception as exc:
            # Don't force the bot flat when the brain is unreachable — mark it degraded so the
            # strategy trades on trend + Jev alone (no directional gate) instead of stalling.
            with self.lock:
                self.state["error"] = str(exc)[:160]
                self.state["t"] = time.time()
                if self.state["bias"] in (None, "flat") and not self.state.get("confidence"):
                    self.state.update(bias=None, degraded=True,
                                      reason="brain unavailable — trading on trend + Jev only")

    def start(self, stop: threading.Event) -> None:
        def run():
            while not stop.is_set():
                self.think()
                stop.wait(self.every * 60)
        threading.Thread(target=run, daemon=True).start()

    def current(self) -> dict:
        with self.lock:
            return dict(self.state)
