# Feedback from gpt-6, 2026-10-23

## Verdict
Show descriptive context tomorrow only after fixing contract identity and freshness.
Hide the letters: v2 is unvalidated and still presents preferences as quality.
Priorities: bind chart/size/quote identity, fix range construction, verify loss-breach operations offline.

## Findings
### F1 Chart and trading context can disagree [severity: high] [confidence: verified]
- Where: projectx_desk/static/desk.js:153, 644, 1812, 1885
- What happens: Chart MES, select Size for MNQ: `/ai/state` supplies MNQ bias, rendered on the MES chart. Manual BUY also targets MNQ. Separately, switch contracts during an outstanding refresh: `load()` neither clears `S.ai` nor increments `S.seq`; the old response is accepted. With AUTO, a subsequent limit menu uses the new chart contract but the old quote/stop ticks. Server checks may refuse, but cannot infer the intended contract. Ticket readback and hash preserve the submitted order, not the trader's intention.
- Fix: Separate chart context from sizing context; invalidate responses by selection generation and verify quote contract before staging. Test delayed MES responses after switching MNQ, and MES chart with MNQ sizing; require correct labels and disabled staging until synchronized.

### F2 Switching protection has an asynchronous gap [severity: high] [confidence: verified]
- Where: projectx_desk/static/desk.js:236, 802, 1840, 1864
- What happens: Start preview, then click a board chip before its response. `stagedOrOpen()` ignores `staging`; switching proceeds. The old-contract ticket then appears on the new chart and remains placeable. Dropdown and search bypass this check entirely. A resting entry without a position also passes it. Correct readback reduces, but does not eliminate, wrong-contract placement risk.
- Fix: Centralize switching refusal across all controls, including preview/place in flight and working entries; recheck after awaited search. Test preview-response delay plus chip/dropdown switching, and acknowledged resting limits. Keep ticket identity prominent beside PLACE.

### F3 Opening range changes with timeframe [severity: medium] [confidence: verified]
- Where: projectx_desk/static/desk.js:1944
- What happens: The 08:30 30m candle contributes its entire high/low, including 08:45 through 09:00. Missing 08:30 bars suppress the range; missing interior bars silently produce an incomplete range. Live partial ranges have no provisional label. On 5m, `t1` is 08:40, not the 08:45 boundary. America/Chicago handles DST independently of display timezone correctly.
- Fix: Build from complete 1m coverage over [08:30,08:45), explicitly label developing ranges and reject holes. Test 1m/5m/15m/30m equivalence, holes, partial bars and both DST offsets. Label this “US equity cash reference”; choose separately named commodity sessions. Fifteen minutes is a convention, not validated optimization for MGC/MCL.

### F4 Fresh-looking grades can use stale inputs [severity: medium] [confidence: verified]
- Where: projectx_desk/digest.py:41, 442, 465; projectx_desk/app.py:806
- What happens: Board calculation has no quote-age or HTF-cache-age gate. Failed board fetches retain old chips. Five-minute inputs include partial bars; log `bar_ts` is wall-clock flooring, not the input bar timestamp. Snapshot lacks RSI, entry label and rule version, preventing full reconstruction.
- Fix: Expose per-input timestamps, expire stale context, log version and complete inputs. Test stopped quotes, failed HTF refresh and a cycle crossing a bar boundary.

### F5 Letters and reversal wording overstate evidence [severity: medium] [confidence: verified]
- Where: projectx_desk/bias.py:99; projectx_desk/static/desk.js:653; projectx_desk/digest.py:798
- What happens: Entry location earns 2 points versus 0.5 per correlated vote; maximum score is 4.5 despite `/5`. Caps retain cliffs: ATR percentile 90 to 90.01 can change A to B. Extended price plus RSI alone becomes REVERSAL_RISK. Tooltip/digest incorrectly generalize v1's result to this untested read.
- Fix: Use neutral “Trend context: down | Near EMA21 | Unvalidated”, without letters or trade colours. Rename reversal flag EXTENDED_RSI. Test boundary behavior and exact visible wording.

## Grade redesign
Keep stacks, VWAP distance, entry distance and complete tape separately. Drop composite weights, F-as-neutral and reversal inference. Require failed continuation, reclaim of a prior level and complete opposing aggressor flow before an explicitly experimental reversal warning. [Gao et al.](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2552752) study a different intraday momentum horizon; it does not validate EMA pullbacks or fading stretched gold/index futures.

Preregister pullback-versus-extension and incremental-tape hypotheses. Freeze code; collect timestamped quotes, spreads, trades, closed bars, rolls and session calendars. Evaluate executable next-quote entries at nonoverlapping 30m observations, normalized by decision-time ATR, net of fees/slippage. Compare direction-only, always-long/short and session-matched randomized-direction nulls. Cluster bootstrap whole days jointly across instruments; Holm-correct primary contrasts. Collect 120 untouched days minimum; estimate required days for 80% power at 0.05 ATR using pilot day variance before collection. Report news, overnight/cash, volatility and trend/range strata descriptively. Sixty days is no power guarantee. V1's overlapping observations, correlated markets, selection and absent cost/CI details prohibit either edge conclusion.

Divergence should compare synchronized 30m volatility-normalized MES/MNQ returns with a preregistered materiality threshold and persistence, displaying values/age. Neutral-versus-direction is not opposing movement. Keep gold separate.

## What I did not verify
Code inspection confirms bias does not enter guard/sizing; limit orders share preview/place and hashed tickets. New bias outputs are constructed enums/numbers; no new free-text injection found. UI no-cache exists. Tests could not run: no checkout venv or system pytest. No browser, original backtest, broker or full allowlist audit was verified. Most consequential proposed missing rail: persistent loss-breach latch cancelling pending entries and flattening at a buffered threshold; budget refusal currently protects new opens, not an existing loss. Before day two, offline-test that behavior, slippage reserve and cancel acknowledgements; no software guarantees a $500 ceiling.
