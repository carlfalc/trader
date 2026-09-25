"""The Jev loop: Jev trading calls landing on a live Hyperliquid price feed,
with YOUR strategy (jevlab/strategy.py) deciding which calls become trades.

  uv run python -m jevlab loop                          # HYPE, 10 minutes
  uv run python -m jevlab loop --coin BTC --minutes 0   # 0 = run until Ctrl+C

The dashboard plots the live microprice (size-weighted mid) from Hyperliquid's
best-bid/ask stream, about 8 updates a second. Jev is asked "buy or sell?" every
--pace seconds (default 0.3s). Calls overlap, each answer lands the moment it
arrives, and the pace backs off by itself if the key gets rate-limited (the free
Vercel tier allows roughly one call every 2 seconds; $5 of credits removes that).

Every Jev answer goes to strategy.decide(). If it says "buy" or "sell", the loop
rests a limit order at the best bid/ask (maker fee, no spread). The order counts
as filled only when the market trades THROUGH its price, and it's cancelled
after --maker-wait seconds. --taker trades immediately at the ask/bid instead.
Positions are $1,000, P&L is shown before and after fees, and nothing is hidden.

PAPER ONLY: this reads public market data and simulates fills. It never places
a real order and never needs an exchange key.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import websocket

from . import hl
from .core import RESULTS, console, header
from . import strategy
from .judges import JevJudge, JudgeError
from .server import serve

WS_URL = "wss://api.hyperliquid.xyz/ws"
NOTIONAL = 1000.0  # $ per side of the paper position
TICK_KEEP_S = 300  # price history kept for the dashboard

QUESTIONS = {
    "side": {
        "type": "choice",
        "instructions": "Which side should this bot hold for the next few seconds: buy (be long) or sell (be short)?",
        "criteria": {"buy": None, "sell": None},
    }
}


class Market:
    """Live Hyperliquid feed for one coin: best bid/ask (~8 updates a second),
    top-5 depth, and every trade. Runs in a background thread and reconnects."""

    def __init__(self, coin: str):
        self.coin = coin
        self.lock = threading.Lock()
        self.bbo = None  # (bid, ask, bid_sz, ask_sz, t)
        self.depth5 = None  # (bid_usd, ask_usd, t)
        self.trades = deque()  # (t, is_buy, px, sz)
        self.mids = deque()  # (t, mid) on every bbo update
        self.ticks = deque()  # (t, microprice, bid, ask), thinned to 20/s for the dashboard
        self.ready = threading.Event()

    def start(self) -> None:
        def on_open(ws):
            for sub in ("bbo", "l2Book", "trades"):
                ws.send(json.dumps({"method": "subscribe", "subscription": {"type": sub, "coin": self.coin}}))

        self.app = websocket.WebSocketApp(WS_URL, on_open=on_open, on_message=lambda ws, m: self._on_message(m))
        threading.Thread(target=lambda: self.app.run_forever(ping_interval=20, reconnect=3), daemon=True).start()

    def _on_message(self, raw: str) -> None:
        msg = json.loads(raw)
        ch, data, now = msg.get("channel"), msg.get("data"), time.time()
        with self.lock:
            if ch == "bbo" and data.get("bbo") and None not in data["bbo"]:
                b, a = data["bbo"]
                self.bbo = (float(b["px"]), float(a["px"]), float(b["sz"]), float(a["sz"]), now)
                bid, ask, bsz, asz, _ = self.bbo
                self.mids.append((now, (bid + ask) / 2))
                if not self.ticks or now - self.ticks[-1][0] >= 0.05:
                    self.ticks.append((now, (bid * asz + ask * bsz) / (bsz + asz), bid, ask))
                self.ready.set()
            elif ch == "l2Book":
                bids, asks = data["levels"]
                self.depth5 = (sum(float(l["px"]) * float(l["sz"]) for l in bids[:5]),
                               sum(float(l["px"]) * float(l["sz"]) for l in asks[:5]), now)
            elif ch == "trades":
                for tr in data:
                    self.trades.append((tr["time"] / 1000, tr["side"] == "B", float(tr["px"]), float(tr["sz"])))
            for dq, keep in ((self.mids, 120), (self.trades, 120), (self.ticks, TICK_KEEP_S)):
                while dq and now - dq[0][0] > keep:
                    dq.popleft()

    def snapshot(self) -> dict:
        """The state Jev sees: small, numeric, and fresh. No dates, no coin name."""
        with self.lock:
            bid, ask, bsz, asz, _ = self.bbo
            now = time.time()
            mid = (bid + ask) / 2
            micro = (bid * asz + ask * bsz) / (bsz + asz)

            def ret(sec):
                past = next((m for t, m in self.mids if t >= now - sec), mid)
                return round(1e4 * (mid / past - 1), 2)

            def flow(sec):
                tr = [(b, px * sz) for t, b, px, sz in self.trades if t >= now - sec]
                vol = sum(v for _, v in tr)
                return (round(sum(v for b, v in tr if b) / vol, 3) if vol else 0.5), len(tr)

            buy5, n5 = flow(5)
            buy30, _ = flow(30)
            m60 = [m for t, m in self.mids if t >= now - 60]
            steps = [1e4 * (b / a - 1) for a, b in zip(m60, m60[1:]) if a != b]
            state = {
                "spread_bps": round(1e4 * (ask - bid) / mid, 3),
                "microprice_vs_mid_bps": round(1e4 * (micro / mid - 1), 3),
                "top_of_book_imbalance": round((bsz - asz) / (bsz + asz), 3),
                "aggressor_buy_share_5s": buy5,
                "aggressor_buy_share_30s": buy30,
                "trades_last_5s": n5,
                "return_5s_bps": ret(5),
                "return_30s_bps": ret(30),
                "tick_volatility_60s_bps": round(statistics.pstdev(steps), 3) if len(steps) > 2 else 0.0,
            }
            if self.depth5:
                bd, ad, _ = self.depth5
                state["depth5_imbalance"] = round((bd - ad) / (bd + ad), 3)
            return {"mid": mid, "micro": micro, "bid": bid, "ask": ask, "spread_bps": 1e4 * (ask - bid) / mid, "state": state}

    def trades_since(self, t0: float) -> list[tuple]:
        with self.lock:
            return [tr for tr in self.trades if tr[0] >= t0]

    def recent_ticks(self, seconds: float = 90) -> list[list[float]]:
        with self.lock:
            now = time.time()
            return [[round(t, 3), m, b, a] for t, m, b, a in self.ticks if t >= now - seconds]


class PaperBook:
    """A $1,000 long-or-short paper position. Tracks coins and cash, so buying at
    the bid or selling at the ask is credited exactly, and fees are kept apart."""

    def __init__(self):
        self.pos, self.qty, self.cash, self.fees = 0, 0.0, 0.0, 0.0
        self.trades = 0
        self.equity = []  # [t, before_fees_usd, after_fees_usd]

    def fill(self, target: int, px: float, fee_rate: float) -> None:
        dq = target * NOTIONAL / px - self.qty
        self.cash -= dq * px
        self.fees += abs(dq) * px * fee_rate
        self.qty += dq
        self.pos = target
        self.trades += 1

    def gross(self, mid: float) -> float:
        return self.cash + self.qty * mid

    def snap(self, t: float, mid: float) -> dict:
        g = self.gross(mid)
        self.equity.append([round(t, 2), round(g, 3), round(g - self.fees, 3)])
        del self.equity[:-2400]
        return {"pos": self.pos, "gross": round(g, 3), "fees": round(self.fees, 3), "net": round(g - self.fees, 3),
                "trades": self.trades, "equity": self.equity}


def run_loop(coin: str, pace_s: float, minutes: float, port: int, open_browser: bool, late_ms: float = 1500,
             maker: bool = True, maker_wait: float = 10.0) -> None:
    exec_txt = f"limit orders ({hl.MAKER_FEE * 1e4:.1f} bps)" if maker else f"market orders ({hl.TAKER_FEE * 1e4:.1f} bps + spread)"
    header("THE JEV LOOP", f"{coin}-PERP · Jev calls as fast as the key allows (floor {pace_s:g}s) · "
           f"strategy: {strategy.DESCRIPTION} · {exec_txt} · "
           f"{'until Ctrl+C' if not minutes else f'{minutes:g} min'} · $1,000 paper position")
    try:
        jev = JevJudge()
    except JudgeError as exc:
        raise SystemExit(f"  Jev key missing: {exc}. Add AI_GATEWAY_API_KEY to .env first.")

    market = Market(coin)
    market.start()
    if not market.ready.wait(15):
        raise SystemExit("  no price feed from Hyperliquid after 15s, check the connection")
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "loop.json"
    log = open(RESULTS / "loop_log.jsonl", "a")
    book = PaperBook()
    decisions: list[dict] = []
    fills: list[dict] = []
    latencies: list[float] = []
    counts = {"ok": 0, "late": 0, "throttled": 0, "error": 0}
    lock = threading.Lock()
    st = {"interval": pace_s, "streak": 0, "prev": None, "order": None, "last_trade_t": 0.0, "hits": 0, "scored": 0}
    started = time.time()
    serve(port, open_browser, page="loop.html")

    def check_order() -> None:
        """Fill the resting limit order only if the market traded through its price."""
        o = st["order"]
        if not o:
            return
        now = time.time()
        buying = o["buying"]
        through = any((buying and px < o["px"]) or (not buying and px > o["px"])
                      for _, _, px, _ in market.trades_since(o["t"]))
        snap = market.snapshot()
        through |= (buying and snap["ask"] < o["px"]) or (not buying and snap["bid"] > o["px"])
        if through:
            book.fill(o["target"], o["px"], hl.MAKER_FEE)
            fills.append({"t": now, "side": "buy" if buying else "sell", "px": o["px"], "kind": "limit",
                          "call": o["call"], "wait_s": round(now - o["t"], 1)})
            st["order"], st["last_trade_t"] = None, now
        elif now > o["expires"]:
            st["order"] = None

    def write(status: str) -> None:
        with lock:
            check_order()
            mid = market.snapshot()["mid"]
            recent = [d["t"] for d in decisions if d["t"] >= time.time() - 10]
            payload = {
                "status": status, "coin": coin, "pace": pace_s, "interval": round(st["interval"], 3),
                "rate_per_s": round(len(recent) / 10, 2), "late_ms": late_ms,
                "strategy_note": strategy.DESCRIPTION, "execution": "maker" if maker else "taker", "maker_wait": maker_wait,
                "started": started, "updated": time.time(), "model": jev.model, "notional": NOTIONAL,
                "maker_fee_bps": hl.MAKER_FEE * 1e4, "taker_fee_bps": hl.TAKER_FEE * 1e4,
                "counts": dict(counts), "blocks": len(decisions),
                "last_ms": latencies[-1] if latencies else None,
                "avg_ms": round(statistics.mean(latencies[-200:])) if latencies else None,
                "hits": st["hits"], "scored": st["scored"],
                "order": st["order"], "decisions": decisions[-200:], "fills": fills[-100:],
                "book": book.snap(time.time(), mid), "ticks": market.recent_ticks(),
            }
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, out)

    def decide(rec: dict, now: dict) -> str:
        """Hand Jev's call to strategy.decide(), then turn its answer into a (paper) order. Runs under the lock."""
        try:
            choice = strategy.decide({"side": rec["side"], "conf": rec["conf"]}, rec["state"], book.pos,
                                     time.time() - st["last_trade_t"])
        except Exception as exc:  # a broken strategy shouldn't kill the loop: show it and hold
            return f"hold · strategy error: {str(exc)[:60]}"
        if choice not in ("buy", "sell", "flat"):
            return str(choice or "hold")
        want = {"buy": 1, "sell": -1, "flat": 0}[choice]
        if want == book.pos:
            if st["order"] and st["order"]["target"] != want:
                st["order"] = None
            return "hold · already " + {1: "long", -1: "short", 0: "flat"}[want]
        if st["order"] and st["order"]["target"] == want:
            return "hold · limit order working"
        if maker:
            buying = want > book.pos
            px = now["bid"] if buying else now["ask"]
            st["order"] = {"target": want, "buying": buying, "px": px, "t": time.time(), "expires": time.time() + maker_wait,
                           "call": rec["block"]}
            return f"limit {'buy' if buying else 'sell'} @ {px:g}"
        buying = want > book.pos
        px = now["ask"] if buying else now["bid"]
        book.fill(want, px, hl.TAKER_FEE)
        fills.append({"t": time.time(), "side": "buy" if buying else "sell", "px": px, "kind": "market", "call": rec["block"], "wait_s": 0})
        st["last_trade_t"] = time.time()
        return f"market {choice} @ {px:g}"

    def ask(seq: int) -> None:
        snap = market.snapshot()
        rec = {"block": seq, "t_ask": time.time(), "state": snap["state"]}
        try:
            ans, meta = jev.ask(snap["state"], QUESTIONS, timeout=5.0, retries=0)
            side = ans["side"]["choice"]
            rec.update(side=side, conf=round(ans["side"]["probs"][side], 3), ms=meta["latency_ms"],
                       status="ok" if meta["latency_ms"] <= late_ms else "late")
        except JudgeError as exc:
            rec.update(side=None, conf=None, ms=None, status="throttled" if "429" in str(exc) else "error",
                       error=str(exc)[:160])
        now = market.snapshot()  # the market as it is when the answer ARRIVES
        rec.update(t=time.time(), mid=now["mid"], micro=now["micro"])
        with lock:
            counts[rec["status"]] += 1
            if rec["ms"]:
                latencies.append(rec["ms"])
            if rec["status"] == "throttled":  # back off hard on a 429, creep back on good answers
                st["interval"], st["streak"] = min(4.0, st["interval"] * 1.6), 0
            elif rec["status"] == "ok":
                st["streak"] += 1
                if st["streak"] >= 3:
                    st["interval"] = max(pace_s, st["interval"] * 0.9)
            prev = st["prev"]
            if prev and now["mid"] != prev["mid"]:  # was the previous call's direction right, up to this one?
                st["hits"] += (now["mid"] > prev["mid"]) == (prev["side"] == "buy")
                st["scored"] += 1
            if rec["status"] == "ok":
                st["prev"] = rec
                rec["action"] = decide(rec, now)
            else:
                rec["action"] = "hold · late" if rec["status"] == "late" else rec["status"]
            decisions.append(rec)
            log.write(json.dumps(rec) + "\n")
        colour = {"buy": "#3fd68a", "sell": "#ff5d6c"}.get(rec["side"], "#f5b53d")
        console.print(f"  {rec['block']:>5}  [{colour}]{(rec['side'] or '-').upper():<5}[/]  "
                      f"{(rec['conf'] or 0):.2f}  {rec['ms'] or '-':>5} ms  [dim]{rec['action']}[/]")

    console.print("  [dim]  #    side   conf     ms       action[/]")
    stop = threading.Event()

    def writer():  # fills resting orders and keeps the dashboard's price history fresh
        while not stop.is_set():
            write("running")
            stop.wait(0.25)

    threading.Thread(target=writer, daemon=True).start()
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
    write("done")
    log.close()
    mid = market.snapshot()["mid"]
    g = book.gross(mid)
    console.print(f"\n  {len(decisions)} Jev calls · {book.trades} trades · before fees ${g:+.2f} · fees ${book.fees:.2f} · "
                  f"after fees [bold]{'[#3fd68a]' if g - book.fees > 0 else '[#ff5d6c]'}${g - book.fees:+.2f}[/][/]")
    console.print(f"  direction right {st['hits']}/{st['scored']} · late {counts['late']} · throttled {counts['throttled']}")
