# jev-starter

A Jev trading bot you can watch, test, and plug your own strategy into.
**The laptop version is paper trading only**: it reads live public market data and simulates fills, with no exchange account needed. The optional **24/7 bot** (Path 2) trades on Bybit, starting on Bybit Demo Trading (fake money).

## What's inside

| | |
|---|---|
| **Jev Loop** · `uv run python -m jevlab loop` | Live dashboard: the real price ticking about 8 times a second, Jev's BUY/SELL calls landing on it, and your strategy deciding which calls become trades. Shows P&L before and after fees on a $1,000 paper position. |
| **Newsroom** · `uv run python -m jevlab newsroom` | A news terminal. It replays the last 36 hours of real crypto headlines in fast motion, Jev reads each one live (which coin, which way, how big), and then shows what the price actually did next. |
| **Your strategy** · `jevlab/strategy.py` | The one file you edit. `decide()` gets Jev's call and the live market numbers, and returns `"buy"`, `"sell"` or `"hold · reason"`. |
| **Setup check** · `uv run python -m jevlab check` | Tests prices, news feeds, your Jev key and your speed tier. |

## Setup

1. Install [uv](https://docs.astral.sh/uv/).
2. `cp .env.example .env` and paste your Vercel AI Gateway key after `AI_GATEWAY_API_KEY=`.
   Get one at vercel.com/dashboard → AI Gateway → API Keys. The free tier works but is slow (about one Jev call every 2 seconds); $5 of AI Gateway credits runs it at full speed. Each call costs a tiny fraction of a cent.
3. `uv sync`, then `uv run python -m jevlab check`.

## Plug in your own strategy

Open `jevlab/strategy.py`. The default is three simple rules:
- only act when Jev is at least 85% sure
- wait 15 seconds between flips
- let the loop use limit orders

Change `SETTINGS`, or rewrite `decide()`. There's a trend-following example at the bottom of the file. Easiest route: tell Claude your idea in one sentence and ask it to rewrite `decide()`. Then run `uv run python -m jevlab loop` again and watch what changes.

Before trusting any strategy, ask three questions:
1. Why should it make money?
2. Does it still make money after costs?
3. Does it work on data it's never seen?

## Flags

`--coin BTC` · `--minutes 0` (run until Ctrl+C) · `--taker` (market orders) · `--gap 4` (newsroom pace) · `--port 8766` (a second dashboard) · `--no-open`

## The 24/7 bot (Path 2): Claude + Jev on Bybit

`uv run python -m jevlab bot` runs the same loop against Bybit, with Claude as the big-picture brain:
- **Claude** reads the market every 10 minutes and sets the bias: long, short or flat
- **Jev** makes the fast calls
- **`strategy.py`** only trades in Claude's direction

It starts on **Bybit Demo Trading** (fake money). `--dry` runs it on Bybit prices with simulated fills and needs no Bybit key.

Safety, always on:
- post-only limit orders
- `MAX_POSITION_USD` and `MAX_DAILY_LOSS_USD` limits
- a kill switch: create a file called `STOP` and it closes out and halts
- on shutdown it cancels orders and closes the position

Real money needs `BYBIT_MODE=live` **and** the exact confirmation phrase in `JEV_LIVE_CONFIRM`. Set those yourself, after weeks of demo results.

To run it 24/7, put it on an always-on server (a small Linux VPS) with `deploy/jev-bot.service`. The setup prompt walks through all of it. Not financial advice.

## The FX / indices / commodities bot (Path 3): MetaApi hybrid

`uv run python -m jevlab fxbot` runs the same Jev + Claude + `strategy.py` engine against your **GAINEDGE instruments** through a **MetaApi-linked MetaTrader 5 account** — Gold, Hang Seng, DAX, NAS100, FX majors, oil, etc. It is **multi-instrument**: each market runs independently (its own Claude bias, Jev calls and position) under one account-wide loss limit and kill switch.

```bash
uv run python -m jevlab fxcheck                        # test the MetaApi connection + live quotes
uv run python -m jevlab fxbot                          # default: Gold + Hang Seng, 24/7
uv run python -m jevlab fxbot --symbols XAUUSD,HK50,NAS100,GER40
uv run python -m jevlab fxbot --lots 2 --direction buy --max-loss 0
```

Options:
- `--symbols` — comma list of canonical GAINEDGE symbols (`XAUUSD, HK50, NAS100, GER40, NZDUSD, USDCAD, EURUSD, …`). The broker's own variant (`XAUUSD.i` etc.) is resolved automatically.
- `--lots` — lots traded per instrument (default `MAX_LOTS`, which defaults to `2`).
- `--direction` — `both` (default), `buy` (longs only) or `sell` (shorts only).
- `--max-loss` — daily loss cap in account currency. `0` turns the daily-loss limit off (the STOP kill switch and per-instrument lot cap stay on).

Setup (`.env`):
- `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` — from metaapi.cloud. Start with a **demo** MetaTrader 5 account. The account must be **deployed** (that's what uses MetaApi credits).
- `METAAPI_REGION` — MetaApi data-centre (`london` default). This is a data-centre, not a market — the instruments decide the markets.
- `METAAPI_ACCOUNT_TYPE` — `demo` (default) or `live`. Live also needs `JEV_LIVE_CONFIRM` set to the exact phrase, exactly like the crypto bot.

Safety is the same model as the crypto bots: demo-first, per-instrument lot cap, account-wide daily-loss limit (or off), STOP kill switch, and flatten-on-exit. Orders are MetaApi market orders; P&L is read straight from the broker's live equity. The dashboard shows the first instrument in full plus a strip summarising every market. Not financial advice.
