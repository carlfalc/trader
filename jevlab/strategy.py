"""YOUR STRATEGY (v2 — trend-following). This is the one file you're meant to edit.

Jev makes a call several times a second ("buy, 91% sure"). Your strategy decides
which of those calls actually become trades. Most should end in "hold": every
trade pays the spread, so a bot that trades every call bleeds — which is exactly
what v1 did in a flat market.

v2 only trades WITH momentum and at high conviction, so it sits out the chop and
takes fewer, more deliberate positions:
  * act only when Jev is at least `min_conf` sure (default 85%)
  * only go long when the short-term trend is UP, only short when it's DOWN
  * exit to flat when the trend turns against an open position
  * wait `min_hold` seconds between entries (no flip-flopping)
  * respect the brain's bias (Claude / RON): never trade against it, stay flat when it says flat

decide() is called every time Jev answers, with:

  call      Jev's answer, e.g. {"side": "buy", "conf": 0.91}
  market    the live numbers Jev was shown, for example:
              return_5s_bps, return_30s_bps     price change over 5s / 30s (1 bps = 0.01%)
              return_60s_bps                    (MetaApi feed) price change over 60s
              top_of_book_imbalance             -1 (all sellers) .. +1 (all buyers)  [crypto feed]
              aggressor_buy_share_5s / _30s     share of recent volume that was buyers  [crypto feed]
              spread_bps, tick_volatility_60s_bps
              claude_bias                       the brain's call: "long", "short" or "flat"
  position  1 = long, -1 = short, 0 = flat
  seconds_since_trade   seconds since your last fill

Return one of: "buy", "sell", "flat", or "hold · reason".
"""

# The knobs. Override --min-conf / --min-hold on the fxbot command, or edit here.
SETTINGS = {
    "min_conf": 0.85,   # only act when Jev is at least this sure
    "min_hold": 120,    # seconds to sit still after entering (no flip-flopping)
    "trend_bps": 1.0,   # how strong the 30s move must be to count as a trend (basis points)
}

DESCRIPTION = (f"trend-following · ≥{SETTINGS['min_conf']:.0%} conviction · with-trend only "
               f"(≥{SETTINGS['trend_bps']:g}bps/30s) · ≥{SETTINGS['min_hold']}s between entries")


def decide(call: dict, market: dict, position: int, seconds_since_trade: float) -> str:
    want = 1 if call["side"] == "buy" else -1
    bias = market.get("claude_bias")  # the brain: long / short / flat (absent = no gate)

    # 1) Brain gate — never fight the big-picture call.
    if bias == "flat":
        return "flat" if position else "hold · brain says stay out"
    if (bias == "long" and position < 0) or (bias == "short" and position > 0):
        return "flat"  # brain flipped: close the wrong-way position first
    if (bias == "long" and want < 0) or (bias == "short" and want > 0):
        return f"hold · against the {bias} bias"

    # 2) Conviction — ignore weak calls.
    if call["conf"] < SETTINGS["min_conf"]:
        return "hold · low conviction"

    # 3) Trend — only trade in the direction the market is actually moving.
    r30 = market.get("return_30s_bps", 0.0)
    thr = SETTINGS["trend_bps"]
    trend_up, trend_down = r30 >= thr, r30 <= -thr

    # exit to flat if the trend has turned against what we're holding
    if position > 0 and trend_down:
        return "flat"
    if position < 0 and trend_up:
        return "flat"

    if want > 0 and not trend_up:
        return "hold · no uptrend"
    if want < 0 and not trend_down:
        return "hold · no downtrend"

    # 4) Position & cooldown.
    if want == position:
        return "hold · already " + ("long" if want > 0 else "short")
    if seconds_since_trade < SETTINGS["min_hold"]:
        return "hold · cooling down"

    return call["side"]
