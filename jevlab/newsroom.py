"""The Jev newsroom: real crypto headlines arriving on a news terminal, Jev
reading each one live, and the real price move that followed.

  uv run python -m jevlab newsroom                 # replay the last 36h, one headline every 6s
  uv run python -m jevlab newsroom --gap 4 --hours 24

Why a replay: crypto news only lands a few times an hour, which is too slow to
watch. So the newsroom replays the last day or two of real headlines in fast
motion. Every headline is sent to Jev live as it appears on screen (real answer,
real response time), and because those headlines already happened, the chart can
show what the price actually did over the next hour. The headlines are newer
than Jev's training data, and Jev never sees the publish time.

The trading rule: trade only headlines Jev
rates "Notable" or "Major", in the direction it calls, from 1 minute after
publishing, held for 60 minutes. No orders are ever placed.
"""

from __future__ import annotations

import json
import os
import time

import pandas as pd

from . import hl
from .core import RESULTS, console, header
from .judges import JevJudge, JudgeError
from .news import COINS, QUESTIONS, fetch_headlines, judge_call
from .server import serve

BEFORE_MIN, AFTER_MIN = 10, 60  # the reaction chart spans publish-10m .. publish+60m


def price_paths(items: list[dict]) -> None:
    """Attach each coin's % move around every headline, from real 1m candles."""
    start = int((items[0]["published"] - pd.Timedelta(minutes=BEFORE_MIN + 5)).timestamp() * 1000)
    candles = {c: hl.candles(c, "1m", start) for c in COINS}
    for it in items:
        entry_t = it["published"].floor("min") + pd.Timedelta(minutes=1)
        it["paths"] = {}
        for c, df in candles.items():
            if entry_t not in df.index:
                continue
            base = df.loc[entry_t, "open"]
            window = df.loc[entry_t - pd.Timedelta(minutes=BEFORE_MIN): entry_t + pd.Timedelta(minutes=AFTER_MIN), "close"]
            it["paths"][c] = [[round((ts - entry_t).total_seconds() / 60, 1), round(float(1e4 * (px / base - 1)), 2)]
                              for ts, px in window.items()]


def run_newsroom(hours: float, every: float, port: int, open_browser: bool) -> None:
    header("THE JEV NEWSROOM", f"replaying the last {hours:g}h of real crypto headlines · one every {every:g}s · "
           f"Jev reads each one live · then the real {AFTER_MIN}-minute price move")
    try:
        jev = JevJudge()
    except JudgeError as exc:
        raise SystemExit(f"  Jev key missing: {exc}. Add AI_GATEWAY_API_KEY to .env first.")

    def load_items() -> list[dict]:
        now = pd.Timestamp.now(tz="UTC")
        fresh = [i for i in fetch_headlines(hours) if i["published"] + pd.Timedelta(minutes=AFTER_MIN + 2) < now]
        if fresh:
            console.print(f"  {len(fresh)} headlines · loading the price moves after each one…")
            price_paths(fresh)
        return fresh

    items = load_items()
    if not items:
        raise SystemExit("  no headlines old enough to score yet")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "newsroom.json"
    score = {"read": 0, "tagged": 0, "trades": 0, "right": 0, "net_bps": 0.0, "latency_ms": []}
    feed: list[dict] = []
    started = time.time()
    rnd = {"n": 1, "index": 0}

    def write(status: str) -> None:
        lat = sorted(score["latency_ms"])
        payload = {"status": status, "hours": hours, "every": every, "total": len(items), "index": rnd["index"], "round": rnd["n"],
                   "started": started, "updated": time.time(), "model": jev.model,
                   "cost_bps": round(2e4 * hl.TAKER_FEE + 1, 1), "after_min": AFTER_MIN, "before_min": BEFORE_MIN,
                   "score": {**{k: v for k, v in score.items() if k != "latency_ms"},
                             "median_ms": lat[len(lat) // 2] if lat else None},
                   "feed": feed[-300:]}
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, out)

    write("running")
    serve(port, open_browser, page="newsroom.html")
    time.sleep(2.5)  # let the page load before the first headline lands

    try:
        while True:
            for it in items:
                rnd["index"] += 1
                t0 = time.time()
                row = {"headline": it["headline"], "source": it["source"], "published": it["published"].timestamp(),
                       "paths": it["paths"], "arrived": t0, "answer": None}
                feed.append(row)
                write("running")  # the headline hits the wire before Jev has answered
                state = {"headline": it["headline"], "summary": it["summary"], "source": it["source"]}
                try:
                    ans, meta = jev.ask(state, QUESTIONS, timeout=10.0, retries=3)
                except JudgeError as exc:
                    row.update(error=str(exc)[:120], answered=time.time())
                    write("running")
                    console.print(f"  [#f5b53d]skip[/] {it['headline'][:70]} ({str(exc)[:40]})")
                    time.sleep(max(0.0, every - (time.time() - t0)))
                    continue
                coin, side = judge_call(ans)
                side = side if coin else 0  # no tradeable coin, no trade
                path = it["paths"].get(coin or "BTC") or []
                ret = next((v for m, v in path if m == AFTER_MIN), None)
                row.update(answer=ans, ms=meta["latency_ms"], answered=time.time(), coin=coin,
                           direction=ans["direction"]["choice"], impact=ans["impact"]["score"],
                           trade=bool(side), side=side, ret_bps=ret)
                score["read"] += 1
                score["latency_ms"].append(meta["latency_ms"])
                if coin:
                    score["tagged"] += 1
                if side and ret is not None:
                    score["trades"] += 1
                    score["right"] += int(side * ret > 0)
                    row["net_bps"] = round(side * ret - (2e4 * hl.TAKER_FEE + 1), 1)
                    score["net_bps"] += row["net_bps"]
                write("running")
                tag = f"{coin or '—':<4} {row['direction']:<8} impact {row['impact']:.1f}"
                console.print(f"  {meta['latency_ms']:>4} ms  {tag}  {'[bold]TRADE[/]' if side else '[dim]no trade[/]'}  "
                              f"[dim]{it['headline'][:60]}[/]")
                time.sleep(max(0.0, every - (time.time() - t0)))
            # round finished: pull the latest headlines and go again, with a fresh scoreboard
            console.print(f"\n  round {rnd['n']} done · reloading headlines for the next round")
            items = load_items() or items
            rnd["n"] += 1
            rnd["index"] = 0
            score.update(read=0, tagged=0, trades=0, right=0, net_bps=0.0, latency_ms=[])
    except KeyboardInterrupt:
        pass
    write("done")
    console.print(f"\n  {score['read']} headlines read · {score['tagged']} tagged to a coin · {score['trades']} worth trading · "
                  f"{score['right']} right · {score['net_bps']:+.1f} bps after costs")
