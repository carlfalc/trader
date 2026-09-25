"""The hybrid FX / indices / commodities bot (Path 3): Jev + Claude + your strategy,
trading through a MetaApi-linked MetaTrader account instead of a crypto exchange.

MULTI-INSTRUMENT: it runs several GAINEDGE markets at once, each traded independently
(its own Claude bias, its own Jev calls, its own position), all under one account-wide
loss limit and kill switch.

  uv run python -m jevlab fxbot                          # default: Gold + Hang Seng, 24/7
  uv run python -m jevlab fxbot --symbols XAUUSD,HK50,NAS100
  uv run python -m jevlab fxbot --minutes 0              # run until stopped (what a server runs)

Per instrument:
  * Claude reads it every --brain-every minutes and sets the bias (long/short/flat).
  * Jev makes the fast buy/sell calls on the live MetaApi quote.
  * strategy.py combines them and only trades in Claude's direction.
  * Orders are MARKET orders sized in LOTS (MAX_LOTS, default 0.01).

Account-wide safety, always on:
  * DEMO account unless METAAPI_ACCOUNT_TYPE=live AND JEV_LIVE_CONFIRM is the exact phrase.
  * MAX_LOTS caps each instrument's position.
  * MAX_DAILY_LOSS_USD on live account equity: hit it and the bot flattens EVERYTHING and stops for the day.
  * Kill switch: create a file called STOP and it flattens everything and halts.
  * On shutdown it closes all open positions.

The dashboard shows the first instrument in full, plus a strip summarising every instrument.
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
from .core import RESULTS, console, header
from .instruments import SYMBOL_VARIANTS, asset_class, display_name
from .judges import JevJudge, JudgeError
from .loop import QUESTIONS
from .metaapi import MetaApiClient, MetaApiError, MetaApiMarket, metaapi_mode
from .server import serve

STOP_FILE = Path(__file__).resolve().parent.parent / "STOP"


def canonical_of(broker_symbol: str) -> str | None:
    """Map a broker symbol (e.g. XAUUSD.i) back to its canonical (XAUUSD)."""
    for canon, variants in SYMBOL_VARIANTS.items():
        if broker_symbol == canon or broker_symbol in variants:
            return canon
    return None


def metaapi_summary_fn(client: MetaApiClient):
    """A summary_fn(symbol) for Claude, built from MetaApi candles + the live quote."""
    def summary(symbol: str) -> dict:
        candles = client.candles(symbol, "5m", 60)
        closes = [float(c["close"]) for c in candles if c.get("close") is not None]
        price = client.current_price(symbol)
        mid = ((price["bid"] + price["ask"]) / 2) if price else (closes[-1] if closes else None)

        def pct(a, b):
            return round(100 * (b / a - 1), 3) if a else 0.0

        out: dict = {"symbol": symbol, "display_name": display_name(symbol),
                     "asset_class": asset_class(symbol), "price": mid}
        if len(closes) >= 13:
            out.update(change_15m_pct=pct(closes[-4], closes[-1]),
                       change_1h_pct=pct(closes[-13], closes[-1]),
                       change_since_open_pct=pct(closes[0], closes[-1]),
                       high=max(closes), low=min(closes))
            moves = [abs(pct(a, b)) for a, b in zip(closes, closes[1:])]
            out["avg_5m_move_pct"] = round(sum(moves) / len(moves), 4) if moves else 0.0
        return out
    return summary


class SymbolEngine:
    """Everything for trading one instrument: its live feed, its Claude brain,
    its decisions/fills, and its current position. Shares the MetaApi client and
    the account-wide halt flag."""

    def __init__(self, client: MetaApiClient, symbol: str, max_lots: float, brain_every: float,
                 jev: JevJudge, late_ms: float, gstate: dict, direction: str = "both"):
        self.client, self.symbol, self.max_lots = client, symbol, max_lots
        self.name = display_name(symbol)
        self.direction = direction  # "both" | "buy" (longs only) | "sell" (shorts only)
        self.jev, self.late_ms, self.g = jev, late_ms, gstate
        self.market = MetaApiMarket(client, symbol, poll_s=float(os.getenv("METAAPI_POLL_S", "1.0")))
        self.brain = Brain(symbol, brain_every, summary_fn=metaapi_summary_fn(client))
        self.lock = threading.Lock()
        self.decisions: list[dict] = []
        self.fills: list[dict] = []
        self.latencies: list[float] = []
        self.counts = {"ok": 0, "late": 0, "throttled": 0, "error": 0}
        self.pos_lots = 0.0
        self.unrealized = 0.0
        self.prev = None
        self.last_trade_t = 0.0
        self.hits = self.scored = 0

    def start(self, stop: threading.Event) -> None:
        self.market.start()
        self.brain.start(stop)

    def pos_sign(self) -> int:
        return 1 if self.pos_lots > 1e-9 else -1 if self.pos_lots < -1e-9 else 0

    def flatten(self) -> None:
        try:
            self.client.flatten(self.symbol)
        except MetaApiError as exc:
            console.print(f"  [#ff5d6c]couldn't close {self.symbol}: {exc}. Close it in MT5 manually.[/]")
        self.pos_lots = 0.0

    def decide(self, rec: dict) -> str:
        if self.g["halted"]:
            return f"hold · halted ({self.g['halted']})"
        if not self.market.market_open:
            return "hold · market closed"
        bias = self.brain.current().get("bias")
        if bias is None:
            return "hold · waiting for Claude's first read"
        pos = self.pos_sign()
        try:
            choice = strategy.decide({"side": rec["side"], "conf": rec["conf"]},
                                     {**rec["state"], "claude_bias": bias}, pos,
                                     time.time() - self.last_trade_t)
        except Exception as exc:
            return f"hold · strategy error: {str(exc)[:60]}"
        if choice not in ("buy", "sell", "flat"):
            return str(choice or "hold")
        want = {"buy": 1, "sell": -1, "flat": 0}[choice]
        # direction filter: if this side isn't allowed, don't take it — go flat instead of reversing
        if want > 0 and self.direction == "sell":
            return "flat" if pos else "hold · sells only"
        if want < 0 and self.direction == "buy":
            return "flat" if pos else "hold · buys only"
        if want == pos:
            return "hold · already " + {1: "long", -1: "short", 0: "flat"}[want]
        try:
            if pos != 0:
                self.client.flatten(self.symbol)
                self.pos_lots = 0.0
            if want != 0:
                side = "buy" if want > 0 else "sell"
                self.client.market_order(self.symbol, side, self.max_lots)
                self.pos_lots = want * self.max_lots
                self.fills.append({"t": time.time(), "side": side, "px": rec.get("mid"),
                                   "qty": self.max_lots, "kind": "market", "call": rec["block"], "wait_s": 0})
        except MetaApiError as exc:
            return f"hold · order rejected: {str(exc)[:70]}"
        self.last_trade_t = time.time()
        return f"market {'buy' if want > 0 else 'sell' if want < 0 else 'flat'} {self.max_lots:g} lots"

    def ask(self, seq: int) -> None:
        snap = self.market.snapshot()
        if not snap:
            return
        rec = {"block": seq, "t_ask": time.time(), "state": snap["state"]}
        try:
            ans, meta = self.jev.ask(snap["state"], QUESTIONS, timeout=5.0, retries=0)
            side = ans["side"]["choice"]
            rec.update(side=side, conf=round(ans["side"]["probs"][side], 3), ms=meta["latency_ms"],
                       status="ok" if meta["latency_ms"] <= self.late_ms else "late")
        except JudgeError as exc:
            rec.update(side=None, conf=None, ms=None,
                       status="throttled" if "429" in str(exc) else "error", error=str(exc)[:160])
        now = self.market.snapshot() or snap
        rec.update(t=time.time(), mid=now.get("mid"), micro=now.get("micro"))
        with self.lock:
            self.counts[rec["status"]] += 1
            if rec["ms"]:
                self.latencies.append(rec["ms"])
            if rec["status"] == "throttled":
                self.g["interval"], self.g["streak"] = min(4.0, self.g["interval"] * 1.6), 0
            elif rec["status"] == "ok":
                self.g["streak"] += 1
                if self.g["streak"] >= 6:
                    self.g["interval"] = max(self.g["pace"], self.g["interval"] * 0.9)
            if self.prev and now.get("mid") and self.prev.get("mid") and now["mid"] != self.prev["mid"]:
                self.hits += (now["mid"] > self.prev["mid"]) == (self.prev["side"] == "buy")
                self.scored += 1
            if rec["status"] == "ok":
                self.prev = rec
                rec["action"] = self.decide(rec)
            else:
                rec["action"] = "hold · late" if rec["status"] == "late" else rec["status"]
            self.decisions.append(rec)
            self.g["log"].write(json.dumps({"sym": self.symbol, **rec}) + "\n")
            self.g["log"].flush()
        if rec["action"].startswith("market"):
            console.print(f"  {time.strftime('%H:%M:%S')}  {self.symbol:<7} #{rec['block']:<6} "
                          f"Jev {(rec['side'] or '-').upper():<4} {(rec['conf'] or 0):.2f}  →  {rec['action']}")

    def summary(self) -> dict:
        b = self.brain.current()
        last = next((d for d in reversed(self.decisions) if d.get("side")), None)
        return {"symbol": self.symbol, "name": self.name, "pos": self.pos_sign(), "lots": self.pos_lots,
                "unrealized": round(self.unrealized, 2), "bias": b.get("bias"),
                "bias_conf": b.get("confidence"), "market_open": self.market.market_open,
                "calls": len(self.decisions), "trades": len(self.fills),
                "last_side": last.get("side") if last else None,
                "last_conf": last.get("conf") if last else None,
                "avg_ms": round(statistics.mean(self.latencies[-100:])) if self.latencies else None}

    def detail(self) -> dict:
        """Full per-instrument block the dashboard renders when this is the primary symbol."""
        recent = [d["t"] for d in self.decisions if d["t"] >= time.time() - 10]
        return {"decisions": self.decisions[-200:], "fills": self.fills[-100:],
                "ticks": self.market.recent_ticks(), "counts": dict(self.counts),
                "blocks": len(self.decisions), "rate_per_s": round(len(recent) / 10, 2),
                "last_ms": self.latencies[-1] if self.latencies else None,
                "avg_ms": round(statistics.mean(self.latencies[-200:])) if self.latencies else None,
                "hits": self.hits, "scored": self.scored, "brain": self.brain.current()}


def run_fxbot(symbols: list[str], pace_s: float, minutes: float, port: int, open_browser: bool,
              late_ms: float = 1500, brain_every: float = 10.0,
              lots: float | None = None, direction: str = "both", max_loss: float | None = None) -> None:
    symbols = [s.strip().upper() for s in symbols if s.strip()]
    if not symbols:
        raise SystemExit("  no symbols given")
    direction = direction if direction in ("both", "buy", "sell") else "both"
    try:
        mode = metaapi_mode()
    except MetaApiError as exc:
        raise SystemExit(f"  {exc}")
    # default size is 2 lots (override with --lots or MAX_LOTS in .env)
    max_lots = float(lots if lots is not None else os.getenv("MAX_LOTS", "2"))
    # daily loss cap in account currency. 0 (or negative) = no daily-loss limit.
    max_loss = float(max_loss if max_loss is not None else os.getenv("MAX_DAILY_LOSS_USD", "50"))
    loss_on = max_loss > 0
    names = ", ".join(f"{display_name(s)} ({s})" for s in symbols)
    dir_label = {"both": "buys & sells", "buy": "buys only", "sell": "sells only"}[direction]
    loss_label = f"daily loss limit ${max_loss:,.0f}" if loss_on else "no daily loss limit"
    mode_label = {"demo": "MetaApi DEMO · fake money", "live": "MetaApi LIVE · REAL MONEY"}[mode]
    header("THE JEV FX BOT", f"{names} · {mode_label} · {dir_label} · Claude sets each bias every "
           f"{brain_every:g} min · strategy: {strategy.DESCRIPTION} · {max_lots:g} lots each · {loss_label}")
    if max_lots >= 1 and loss_on:
        console.print(f"  [#f5b53d]Note: {max_lots:g} lots is a large size — one small move can exceed the "
                      f"${max_loss:,.0f} daily-loss limit and halt the bot. Raise it (or set 0 to turn it off) to suit.[/]")
    if not loss_on:
        console.print("  [#f5b53d]Daily loss limit is OFF. The STOP kill switch and per-instrument lot cap are still active.[/]")
    if mode == "live":
        console.print("  [bold #ff5d6c]LIVE MODE: this bot is trading real money.[/] Kill switch: create a file named STOP.")

    try:
        jev = JevJudge()
    except JudgeError as exc:
        raise SystemExit(f"  Jev key missing: {exc}. Add AI_GATEWAY_API_KEY to .env first.")
    try:
        client = MetaApiClient()
        info = client.account_information()
    except MetaApiError as exc:
        raise SystemExit(f"  MetaApi problem: {exc}\n  (Is the account deployed and does it have credits? "
                         "Check METAAPI_TOKEN / METAAPI_ACCOUNT_ID / METAAPI_REGION in .env.)")
    currency = info.get("currency", "USD")
    equity_start = float(info.get("equity") or info.get("balance") or 0)
    console.print(f"  MetaApi {mode} account connected · equity {equity_start:,.2f} {currency}")

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "loop.json"
    log = open(RESULTS / "fxbot_log.jsonl", "a")
    stop = threading.Event()
    g = {"halted": None, "pace": pace_s, "interval": pace_s, "streak": 0, "log": log}
    engines = [SymbolEngine(client, s, max_lots, brain_every, jev, late_ms, g, direction) for s in symbols]
    by_symbol = {e.symbol: e for e in engines}
    for e in engines:
        e.start(stop)

    acct = {"equity": equity_start, "balance": float(info.get("balance") or equity_start),
            "currency": currency, "equity_start": equity_start, "day": datetime.now(timezone.utc).date(),
            "day_start_equity": equity_start, "equity_curve": []}
    started = time.time()

    # give feeds a moment (markets may be closed — that's reported, not fatal)
    for _ in range(20):
        if any(e.market.ready.is_set() for e in engines):
            break
        time.sleep(1)
    serve(port, open_browser, page="loop.html")

    def refresh_account() -> None:
        try:
            i = client.account_information()
            acct["equity"] = float(i.get("equity") or i.get("balance") or acct["equity"])
            acct["balance"] = float(i.get("balance") or acct["balance"])
        except MetaApiError:
            pass
        try:
            positions = client.positions()
        except Exception:
            positions = []
        agg: dict[str, list[float]] = {e.symbol: [0.0, 0.0] for e in engines}  # [lots, unrealized]
        for p in positions:
            canon = canonical_of(p.get("symbol", ""))
            if canon in agg:
                sign = 1 if p.get("type") == "POSITION_TYPE_BUY" else -1
                agg[canon][0] += sign * float(p.get("volume") or 0)
                agg[canon][1] += float(p.get("profit") or 0)
        for e in engines:
            e.pos_lots, e.unrealized = agg[e.symbol][0], agg[e.symbol][1]

    def flatten_all(reason: str) -> None:
        for e in engines:
            e.flatten()

    def risk_check() -> None:
        if g["halted"]:
            return
        today = datetime.now(timezone.utc).date()
        if today != acct["day"]:
            acct["day"], acct["day_start_equity"] = today, acct["equity"]
        if loss_on and acct["equity"] - acct["day_start_equity"] < -max_loss:
            g["halted"] = f"daily loss limit (${max_loss:,.0f}) reached"
            console.print(f"\n  [bold #f5b53d]HALTED: {g['halted']}[/]. Flattening everything.")
            flatten_all(g["halted"])
        elif STOP_FILE.exists():
            g["halted"] = "kill switch (STOP file)"
            console.print("\n  [bold #f5b53d]HALTED: kill switch (STOP file)[/]. Flattening everything.")
            flatten_all(g["halted"])

    def write(status: str) -> None:
        primary = engines[0]
        net = acct["equity"] - acct["equity_start"]
        acct["equity_curve"].append([round(time.time(), 2), round(net, 3), round(net, 3)])
        del acct["equity_curve"][:-2400]
        det = primary.detail()
        label = mode_label + (f" · HALTED: {g['halted']}" if g["halted"]
                              else "" if primary.market.market_open else " · MARKET CLOSED")
        payload = {
            "status": status, "venue": "metaapi", "symbol": primary.symbol, "coin": primary.symbol,
            "venue_label": f"{primary.name} · MetaApi", "mode": mode, "mode_label": label,
            "market_open": primary.market.market_open, "pace": pace_s, "interval": round(g["interval"], 3),
            "rate_per_s": det["rate_per_s"], "late_ms": late_ms, "strategy_note": strategy.DESCRIPTION,
            "execution": "market", "maker_wait": 0, "started": started, "updated": time.time(),
            "model": jev.model, "notional": max_lots, "unit": "lots", "currency": acct["currency"],
            "maker_fee_bps": 0, "taker_fee_bps": 0, "counts": det["counts"], "blocks": det["blocks"],
            "last_ms": det["last_ms"], "avg_ms": det["avg_ms"], "hits": det["hits"], "scored": det["scored"],
            "brain": det["brain"], "order": None, "equity": round(acct["equity"], 2),
            "balance": round(acct["balance"], 2), "decisions": det["decisions"], "fills": det["fills"],
            "book": {"pos": primary.pos_sign(), "qty": primary.pos_lots, "gross": round(net, 3), "fees": 0.0,
                     "net": round(net, 3), "trades": len(primary.fills), "equity": acct["equity_curve"]},
            "ticks": det["ticks"],
            "instruments": [e.summary() for e in engines],  # the multi-market strip
        }
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, default=str))
        os.replace(tmp, out)

    def writer():
        tick = 0
        while not stop.is_set():
            try:
                if tick % 5 == 0:
                    refresh_account()
                risk_check()
                write("running")
            except Exception as exc:
                console.print(f"  [dim]status update skipped: {str(exc)[:80]}[/]")
            tick += 1
            stop.wait(0.5)

    threading.Thread(target=writer, daemon=True).start()
    console.print(f"  [dim]{len(engines)} markets · trades print here · the dashboard shows {engines[0].name} + a strip for all[/]")
    seq = 0
    try:
        with ThreadPoolExecutor(max_workers=4 * len(engines) + 2) as pool:
            while not minutes or time.time() - started < minutes * 60:
                seq += 1
                for e in engines:
                    pool.submit(e.ask, seq)
                time.sleep(g["interval"])
    except KeyboardInterrupt:
        pass
    stop.set()
    for e in engines:
        e.market.stop()
    if os.getenv("FLATTEN_ON_EXIT", "true").lower() != "false":
        flatten_all("shutdown")
    write("done")
    log.close()
    refresh_account()
    net = acct["equity"] - acct["equity_start"]
    console.print(f"\n  session over · equity {acct['equity']:,.2f} {currency} · P&L {net:+.2f} {currency}")
    for e in engines:
        console.print(f"    {e.symbol:<7} {len(e.decisions)} calls · {len(e.fills)} orders · "
                      f"direction right {e.hits}/{e.scored}")
