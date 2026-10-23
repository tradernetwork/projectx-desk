# Feedback from DeepSeek V4.1 Flash, 2026-10-23

## Verdict

v2 is fit to show tomorrow only as context, not as the chip it is. Three things decide it: the input cliffs (ATR 90, RSI 30, ADX 20) still flip a letter on a wobble; the 2.0 entry bonus can award an A with the 1h stack opposed; the chart-bar chip can describe a different contract than the chart. This sample shows neither an edge nor its absence.

## Findings

### F1 No cancel for a resting limit  [high] [verified]
- Where: app.py:458-536 (no `/api/cancel`); guard.py:1382 (`cancel`, no route).
- What: a placed unfilled limit is a working order, so `contract_flat` (guard.py:290) blocks new entries there. FLATTEN is the only cancel path (app.py:496) and it also closes positions; live cannot cancel.
- Fix: add `POST /api/cancel` behind `post_gate`, human only, no MCP tool; test that it leaves a position untouched.

### F2 The v1 cliffs survived  [high] [verified]
- Where: bias.py:129-131, 121, 127.
- What: ATR 90.0 grades A, 90.1 B, pinned by test_bias.py; RSI 30 and ADX 20 step the same way. Round 17's cliff, moved not removed.
- Fix: grade the penalty, or keep VOL_EXTREME as a flag; test that a 0.1 change moves one band.

### F3 Entry location outvotes the higher timeframe  [high] [verified]
- Where: bias.py:16-17, 90-111.
- What: 5m and 15m BULL, 1h BEAR, VWAP and tape BULL, price at the 21 EMA scores 4.0, an A with only DISSENT flagged; the 2.0 entry term tops the three stack votes (1.5).
- Fix: cap at B on DISSENT, or set W_ENTRY to 1.0; test this case.

### F4 The chart bar reads another contract  [medium] [verified]
- Where: desk.js:644-652 (S.ai), 155-167 (S.ai is the Size-for contract), index.html:224.
- What: with Size for ES and the chart on MES, the MES bar reads "TREND LONG A" about ES, with no root.
- Fix: hide unless `S.sizeCid === S.contractId`, or print the root; test the mismatch.

### F5 BUY and SELL ignore the chart contract  [medium] [verified]
- Where: desk.js:842-849 versus 1885-1893.
- What: the limit menu refuses when the size box is on another contract; `manualStage` does not, so BUY stages the Size-for contract while the chart shows another. Only the readback says which.
- Fix: same `cid !== S.contractId` check, or the root on the buttons.

### F6 The bias log is not bar-keyed  [medium] [verified]
- Where: app.py:818.
- What: `bar_ts` floors the clock to five minutes while `price` is the live last: a 10:03 read becomes the 10:00 bar at 10:03, several reads can land in one bar, and the forward move starts inside it.
- Fix: log at the close with the bar close, skip duplicates; test two cycles in one bar.

### F7 MCLE cannot size  [medium] [verified]
- Where: sizing.py:53-65, 113.
- What: CON.F.US.MCLE.Z26 parses to MCLE, `spec_for` raises, so MCL never sizes; the allowed roots list MCL.
- Fix: add the MCLE row (0.01, 1.00, micro_of CL) and root; test that an MCLE id sizes.

### F8 The ORB box is wrong on 15m and 30m, absent on 45m  [low] [verified]
- Where: desk.js:1944-1957.
- What: `t1` is the last bar's start inside 8:30-8:45, so on 15m and 30m it equals `t0` and the box collapses to the `x0+2` fallback; a span must divide 510 to match, so 45m never draws. Both times are hardcoded for all.
- Fix: set `t1` to `t0` plus 15 minutes; test 1m, 5m, 15m, 30m and 45m.

## Grade redesign

Keep the fixed denominator, the THROUGH_EMA and REVERSAL_RISK caps, the flags and the log. The entry-location instinct is right: a pullback to the 21 EMA in a trend is defensible. But REVERSAL_RISK should need absorption (heavy sell volume with no new low), delta divergence and a close back inside the prior range, not just "extended and stretched".

Drop the headline letter, the 2.0 entry bonus, the three cliffs, and F for a neutral market.

Add a two-part read with no letter: TREND as agree/reads and ENTRY as PULLBACK, MID, EXTENDED or THROUGH_EMA, with two-bar hysteresis. Replace directions-differ with LONG versus SHORT plus a beta-adjusted spread beyond a stated sigma for three bars (digest.py:489-494 counts NEUTRAL as disagreement).

Pre-register, no fitting: H1, A/A+ beats C/D on the 30 minute signed ATR move, day-clustered. H2, PULLBACK beats EXTENDED. H3, DISSENT underperforms agreed reads. Nulls: always-long, always-short, and labels shuffled within contract-day.

Protocol: two years of 5m, 15m and 1h bars for MES, MNQ and MGC; one read per closed bar; no lookahead; day-block bootstrap; Benjamini-Hochberg at q = 0.10; split by ADX, ATR, time of day, direction; freeze v2, score at 60+ trade days, then a second holdout. Report the minimum detectable effect at 60 days, not a null. The v1 sample (n = 1010, sixfold overlapping windows in 21 days, 49%) supports neither claim.

Also print the score out of 4.5, not 5, and fix the stale GRANULARITY note: with scalers off it is the 1.5 x ATR stop against the 25% cap.

## What I did not verify

No real gateway and no sim rehearsal, so order-path claims rest on `dev_gateway.py`. I did not open the UI in a browser; DOM claims are from source. I did not backtest v2 or re-run v1. Whether a real position event carries `accountId` (state.py:177, which gates every stop arming) is unverified. Suite: 822 passed.
