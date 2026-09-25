"""The 24/7 bot (Path 2): Jev + Claude + your strategy, trading on Bybit.

  uv run python -m jevlab bot --dry        # Bybit prices, simulated fills. No Bybit key needed
  uv run python -m jevlab bot              # Bybit Demo Trading (fake money), needs a demo API key
  uv run python -m jevlab bot --minutes 0  # run until stopped (this is what the server runs)

How it decides:
  * Claude (the brain) reads the market every --brain-every minutes and sets the bias:
    long, short or flat.
  * Jev makes a fast buy/sell call several times a second.
  * strategy.py combines them. By default it only trades in Claude's direction, and only
    when Jev is 85%+ sure.
  * Orders are post-only limit orders at the best bid/ask (maker fee, no spread),
    cancelled if they don't fill within --maker-wait seconds.

Safety, always on:
  * Demo Trading unless BYBIT_MODE=live AND JEV_LIVE_CONFIRM is the exact phrase (see bybit.py).
  * MAX_POSITION_USD caps the position size (default $1,000).
  * MAX_DAILY_LOSS_USD: hit it and the bot cancels everything, closes the position and stops
    trading for the day (default $50).
  * Kill switch: create a file called STOP in the project folder, and the bot closes out and halts.
  * On shutdown it cancels open orders and closes the position.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import strategy
from .brain import Brain
from .bybit import BybitError, BybitMarket, BybitTrader, bybit_mode
from .core import RESULTS, console, header
from .judges import JevJudge, JudgeError
from .loop import QUESTIONS
from .server import serve

MAKER_FEE = 0.0002  # Bybit base-tier maker fee on USDT perps, used for --dry fills
STOP_FILE = Path(__file__).resolve().parent.parent / "STOP"


class Book:
    """Position and P&L from actual fills: coins, cash and fees, marked to the live mid."""

    def __init__(self):
        self.qty, self.cash, self.fees, self.trades = 0.0, 0.0, 0.0, 0
        self.equity: list[list[float]] = []
        self.day = datetime.now(timezone.utc).date()
        self.day_start_net = 0.0

    def fill(self, dq: float, px: float, fee_usd: float) -> None:
        self.cash -= dq * px
        self.qty += dq
        self.fees += fee_usd
        self.trades += 1

    def net(self, mid: float) -> float:
        return self.cash + self.qty * mid - self.fees

    def snap(self, t: float, mid: float) -> dict:
        g = self.cash + self.qty * mid
        self.equity.append([round(t, 2), round(g, 3), round(g - self.fees, 3)])
        del self.equity[:-2400]
        pos = 1 if self.qty > 0 else -1 if self.qty < 0 else 0
        return {"pos": pos, "qty": self.qty, "gross": round(g, 3), "fees": round(self.fees, 3),
                "net": round(g - self.fees, 3), "trades": self.trades, "equity": self.equity}


def run_bot(coin: str, pace_s: float, minutes: float, port: int, open_browser: bool, late_ms: float = 1500,
            maker_wait: float = 10.0, brain_every: float = 10.0, dry: bool = False) -> None:
    symbol = f"{coin}USDT"
    try:
        mode = "dry" if dry else bybit_mode()
    except BybitError as exc:
        raise SystemExit(f"  {exc}")
    position_usd = float(os.getenv("MAX_POSITION_USD", "1000"))
    max_loss = float(os.getenv("MAX_DAILY_LOSS_USD", "50"))
    mode_label = {"dry": "Bybit prices · simulated fills", "demo": "Bybit DEMO · fake money", "live": "LIVE · REAL MONEY"}[mode]
    header("THE JEV BOT", f"{symbol} on Bybit · {mode_label} · Claude sets the bias every {brain_every:g} min · "
           f"Jev calls as fast as the key allows · strategy: {strategy.DESCRIPTION} · "
           f"max position ${position_usd:,.0f} · daily loss limit ${max_loss:,.0f}")
    if mode == "live":
        console.print("  [bold #ff5d6c]LIVE MODE: this bot is trading real money.[/] Kill switch: create a file named STOP.")

    try:
        jev = JevJudge()
    except JudgeError as exc:
        raise SystemExit(f"  Jev key missing: {exc}. Add AI_GATEWAY_API_KEY to .env first.")
    trader = None
    if mode != "dry":
        try:
            trader = BybitTrader(symbol, mode)
            trader.cancel_all()  # start clean: no stale orders from a previous run
            equity = trader.equity_usdt()
            console.print(f"  Bybit {mode} account connected · equity {equity:,.2f} USDT" if equity is not None
                          else f"  Bybit {mode} account connected")
        except BybitError as exc:
            raise SystemExit(f"  Bybit problem: {exc}")

    market = BybitMarket(symbol)
    market.start()
    if not market.ready.wait(15):
        raise SystemExit("  no price feed from Bybit after 15s, check the connection")

    book = Book()
    if trader:  # adopt any position already open, so the numbers are honest from the first second
        size, avg = trader.position()
        if size:
            book.qty, book.cash = size, -size * avg
            console.print(f"  existing position adopted: {size:+g} {coin} @ {avg:g}")

    stop = threading.Event()
    brain = Brain(symbol, brain_every)
    brain.start(stop)

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "loop.json"
    log = open(RESULTS / "bot_log.jsonl", "a")
    decisions: list[dict] = []
    fills: list[dict] = []
    latencies: list[float] = []
    counts = {"ok": 0, "late": 0, "throttled": 0, "error": 0}
    lock = threading.Lock()
    st = {"interval": pace_s, "streak": 0, "prev": None, "order": None, "last_trade_t": 0.0, "hits": 0, "scored": 0,
          "halted": None}
    started = time.time()
    serve(port, open_browser, page="loop.html")

    def record_fill(dq: float, px: float, fee: float, kind: str, call, waited: float) -> None:
        book.fill(dq, px, fee)
        fills.append({"t": time.time(), "side": "buy" if dq > 0 else "sell", "px": px, "qty": abs(dq), "kind": kind,
                      "call": call, "wait_s": round(waited, 1)})
        st["last_trade_t"] = time.time()

    def check_order() -> None:
        o = st["order"]
        if not o:
            return
        now = time.time()
        if trader is None:  # --dry: fill only if the market traded through our price
            through = any((o["buying"] and px < o["px"]) or (not o["buying"] and px > o["px"])
                          for _, _, px, _ in market.trades_since(o["t"]))
            if through:
                record_fill(o["qty"] if o["buying"] else -o["qty"], o["px"], o["qty"] * o["px"] * MAKER_FEE, "limit", o["call"], now - o["t"])
                st["order"] = None
            elif now > o["expires"]:
                st["order"] = None
            return
        try:
            s = trader.order(o["id"])
        except BybitError:
            return
        new = s["filled"] - o["seen_qty"]
        if new > 1e-12:  # count only the newly filled part, with its share of the fee
            px = s["avg_px"] or o["px"]
            record_fill(new if o["buying"] else -new, px, s["fee"] - o["seen_fee"], "limit", o["call"], now - o["t"])
            o["seen_qty"], o["seen_fee"] = s["filled"], s["fee"]
        if s["status"] in ("Filled", "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"):
            st["order"] = None
        elif now > o["expires"]:
            trader.cancel(o["id"])
            o["expires"] = now + 3  # look once more for fills that raced the cancel, then let go

    def close_out() -> None:
        try:
            f = trader.flatten()
        except BybitError as exc:
            console.print(f"  [#ff5d6c]couldn't close the position: {exc}. Close it on Bybit manually.[/]")
            return
        if f and f["px"]:
            record_fill(f["dq"], f["px"], f["fee"], "market", None, 0)

    def halt(reason: str) -> None:
        st["halted"] = reason
        st["order"] = None
        console.print(f"\n  [bold #f5b53d]HALTED: {reason}[/]. Cancelling orders and closing the position.")
        if trader:
            trader.cancel_all()
            close_out()
        else:
            mid = market.snapshot()["mid"]
            if book.qty:
                record_fill(-book.qty, mid, abs(book.qty) * mid * 0.00055, "market", None, 0)

    def risk_check(mid: float) -> None:
        if st["halted"]:
            return
        today = datetime.now(timezone.utc).date()
        if today != book.day:  # a new UTC day resets the loss limit
            book.day, book.day_start_net = today, book.net(mid)
        if book.net(mid) - book.day_start_net < -max_loss:
            halt(f"daily loss limit (${max_loss:,.0f}) reached")
        elif STOP_FILE.exists():
            halt("kill switch (STOP file)")

    def write(status: str) -> None:
        with lock:
            check_order()
            mid = market.snapshot()["mid"]
            risk_check(mid)
            recent = [d["t"] for d in decisions if d["t"] >= time.time() - 10]
            o = st["order"]
            payload = {
                "status": status, "venue": "bybit", "symbol": symbol, "coin": coin, "mode": mode,
                "venue_label": f"{symbol} · Bybit", "mode_label": mode_label + (f" · HALTED: {st['halted']}" if st["halted"] else ""),
                "pace": pace_s, "interval": round(st["interval"], 3), "rate_per_s": round(len(recent) / 10, 2),
                "late_ms": late_ms, "strategy_note": strategy.DESCRIPTION, "execution": "maker", "maker_wait": maker_wait,
                "started": started, "updated": time.time(), "model": jev.model, "notional": position_usd,
                "maker_fee_bps": MAKER_FEE * 1e4, "taker_fee_bps": 5.5, "counts": dict(counts), "blocks": len(decisions),
                "last_ms": latencies[-1] if latencies else None,
                "avg_ms": round(statistics.mean(latencies[-200:])) if latencies else None,
                "hits": st["hits"], "scored": st["scored"], "brain": brain.current(),
                "order": ({"target": o["target"], "buying": o["buying"], "px": o["px"], "t": o["t"]} if o else None),
                "decisions": decisions[-200:], "fills": fills[-100:],
                "book": book.snap(time.time(), mid), "ticks": market.recent_ticks(),
            }
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, out)

    def decide(rec: dict, now: dict) -> str:
        """Jev's call + Claude's bias -> strategy.decide() -> a post-only limit order. Runs under the lock."""
        if st["halted"]:
            return f"hold · halted ({st['halted']})"
        bias = brain.current().get("bias")
        if bias is None:
            return "hold · waiting for Claude's first read"
        market_view = {**rec["state"], "claude_bias": bias}
        pos = 1 if book.qty > 0 else -1 if book.qty < 0 else 0
        try:
            choice = strategy.decide({"side": rec["side"], "conf": rec["conf"]}, market_view, pos, time.time() - st["last_trade_t"])
        except Exception as exc:
            return f"hold · strategy error: {str(exc)[:60]}"
        if choice not in ("buy", "sell", "flat"):
            return str(choice or "hold")
        want = {"buy": 1, "sell": -1, "flat": 0}[choice]
        if want == pos:
            return "hold · already " + {1: "long", -1: "short", 0: "flat"}[want]
        if st["order"]:
            if st["order"]["target"] == want:
                return "hold · limit order working"
            if trader:
                trader.cancel(st["order"]["id"])
            st["order"] = None
        size = position_usd / now["mid"]
        size = trader.round_qty(size) if trader else round(size, 6)
        dq = want * size - book.qty
        buying = dq > 0
        qty = trader.round_qty(abs(dq)) if trader else round(abs(dq), 6)
        if trader and (qty < trader.min_qty or qty * now["mid"] < trader.min_notional):
            return "hold · order too small for Bybit"
        px = now["bid"] if buying else now["ask"]
        order = {"target": want, "buying": buying, "qty": qty, "px": px, "t": time.time(), "expires": time.time() + maker_wait,
                 "call": rec["block"], "seen_qty": 0.0, "seen_fee": 0.0, "id": None}
        if trader:
            try:
                order["id"] = trader.place_post_only("buy" if buying else "sell", qty, px, reduce_only=(want == 0))
            except BybitError as exc:
                return f"hold · order rejected: {str(exc)[:70]}"
        st["order"] = order
        return f"limit {'buy' if buying else 'sell'} {qty:g} @ {px:g}"

    def ask(seq: int) -> None:
        snap = market.snapshot()
        rec = {"block": seq, "t_ask": time.time(), "state": snap["state"]}
        try:
            ans, meta = jev.ask(snap["state"], QUESTIONS, timeout=5.0, retries=0)
            side = ans["side"]["choice"]
            rec.update(side=side, conf=round(ans["side"]["probs"][side], 3), ms=meta["latency_ms"],
                       status="ok" if meta["latency_ms"] <= late_ms else "late")
        except JudgeError as exc:
            rec.update(side=None, conf=None, ms=None, status="throttled" if "429" in str(exc) else "error", error=str(exc)[:160])
        now = market.snapshot()
        rec.update(t=time.time(), mid=now["mid"], micro=now["micro"])
        with lock:
            counts[rec["status"]] += 1
            if rec["ms"]:
                latencies.append(rec["ms"])
            if rec["status"] == "throttled":
                st["interval"], st["streak"] = min(4.0, st["interval"] * 1.6), 0
            elif rec["status"] == "ok":
                st["streak"] += 1
                if st["streak"] >= 3:
                    st["interval"] = max(pace_s, st["interval"] * 0.9)
            prev = st["prev"]
            if prev and now["mid"] != prev["mid"]:
                st["hits"] += (now["mid"] > prev["mid"]) == (prev["side"] == "buy")
                st["scored"] += 1
            if rec["status"] == "ok":
                st["prev"] = rec
                rec["action"] = decide(rec, now)
            else:
                rec["action"] = "hold · late" if rec["status"] == "late" else rec["status"]
            decisions.append(rec)
            log.write(json.dumps(rec) + "\n")
            log.flush()
        if rec["action"].startswith("limit") or rec["action"].startswith("hold · order"):
            console.print(f"  {time.strftime('%H:%M:%S')}  #{rec['block']:<6} Jev {(rec['side'] or '-').upper():<4} "
                          f"{(rec['conf'] or 0):.2f}  →  {rec['action']}")

    def writer():
        while not stop.is_set():
            try:
                write("running")
            except Exception as exc:  # a hiccup reading the exchange shouldn't stop the bot
                console.print(f"  [dim]status update skipped: {str(exc)[:80]}[/]")
            stop.wait(0.5)

    threading.Thread(target=writer, daemon=True).start()
    console.print("  [dim]trades and orders print here · the dashboard shows every call[/]")
    seq = 0
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            while not minutes or time.time() - started < minutes * 60:
                seq += 1
                pool.submit(ask, seq)
                time.sleep(st["interval"])
    except KeyboardInterrupt:
        pass
    stop.set()
    with lock:
        if trader:
            trader.cancel_all()
            if os.getenv("FLATTEN_ON_EXIT", "true").lower() != "false":
                close_out()
    write("done")
    log.close()
    mid = market.snapshot()["mid"]
    console.print(f"\n  {len(decisions)} Jev calls · {book.trades} fills · fees ${book.fees:.2f} · net ${book.net(mid):+.2f}")
