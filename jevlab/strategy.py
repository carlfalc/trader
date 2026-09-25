"""YOUR STRATEGY. This is the one file you're meant to edit.

Jev makes a call several times a second ("buy, 91% sure"). Your strategy decides
which of those calls actually become trades. Most should end in "hold": every
trade has a cost, so a bot that trades every call bleeds.

decide() is called every time Jev answers, with:

  call      Jev's answer, e.g. {"side": "buy", "conf": 0.91}
  market    the live numbers Jev was shown, for example:
              return_5s_bps, return_30s_bps     price change over 5s / 30s (1 bps = 0.01%)
              top_of_book_imbalance             -1 (all sellers) .. +1 (all buyers) at the best price
              depth5_imbalance                  the same, over the top 5 price levels
              aggressor_buy_share_5s / _30s     share of recent trade volume that was buyers (0..1)
              trades_last_5s                    how busy the market is right now
              spread_bps, microprice_vs_mid_bps, tick_volatility_60s_bps
              claude_bias                       24/7 bot only: "long", "short" or "flat", Claude's
                                                big-picture call, refreshed every few minutes
  position  1 = you're long, -1 = you're short, 0 = flat
  seconds_since_trade   seconds since your last fill

Return one of:
  "buy"            go (or stay) long
  "sell"           go (or stay) short
  "flat"           close the position
  "hold · reason"  do nothing. The reason shows up in the dashboard feed.

The loop handles everything else: placing the (paper) order, fees, the
dashboard. Want a different strategy? Describe it to Claude in one sentence
and ask it to rewrite decide(), e.g. "only buy when Jev is 90%+ sure AND
buyers have been in control for the last 30 seconds".
"""

# The defaults are the three rules from the video.
SETTINGS = {
    "min_conf": 0.85,  # only act when Jev is at least this sure
    "min_hold": 15,    # seconds to sit still after a trade (no flip-flopping)
}

DESCRIPTION = f"trade only at ≥{SETTINGS['min_conf']:.0%} conviction · ≥{SETTINGS['min_hold']}s between flips"


def decide(call: dict, market: dict, position: int, seconds_since_trade: float) -> str:
    want = 1 if call["side"] == "buy" else -1
    bias = market.get("claude_bias")  # only set on the 24/7 bot, where Claude is the brain
    if bias == "flat":
        return "flat" if position else "hold · Claude says stay out"
    if (bias == "long" and position < 0) or (bias == "short" and position > 0):
        return "flat"  # Claude changed its mind: get out of the old direction first
    if (bias == "long" and want < 0) or (bias == "short" and want > 0):
        return f"hold · against Claude's {bias} bias"
    if call["conf"] < SETTINGS["min_conf"]:
        return "hold · low conviction"
    if want == position:
        return "hold · already " + ("long" if want > 0 else "short")
    if seconds_since_trade < SETTINGS["min_hold"]:
        return "hold · too soon to flip"
    return call["side"]


# ---------------------------------------------------------------------------
# Example: trade with the trend only. Uncomment to use it (and delete the
# decide() above).
#
# def decide(call, market, position, seconds_since_trade):
#     trend_up = market.get("return_30s_bps", 0) > 0 and market.get("aggressor_buy_share_30s", 0.5) > 0.55
#     trend_down = market.get("return_30s_bps", 0) < 0 and market.get("aggressor_buy_share_30s", 0.5) < 0.45
#     if call["conf"] < 0.8:
#         return "hold · low conviction"
#     if call["side"] == "buy" and trend_up and position != 1:
#         return "buy"
#     if call["side"] == "sell" and trend_down and position != -1:
#         return "sell"
#     return "hold · against the trend"
