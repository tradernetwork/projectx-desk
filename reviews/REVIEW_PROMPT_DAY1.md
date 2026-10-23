# Review prompt: projectx-desk, day-one changes (grade rebuild, limit orders, board, ORB)

Paste everything below the line into the model. Replace `<MODEL>` with a short slug (for example `gpt-6`, `gemini-3.8-flash`, `deepseek-v4-flash`).
Give the model the repo (https://github.com/tradernetwork/projectx-desk at the latest `main`, or the local checkout `~/projects/projectx-desk`).
This round's changes are only on `main` once pushed; if the model cannot see `projectx_desk/bias.py` containing the text "v2", it is reading a stale copy.

---

You are an independent reviewer with intraday futures trading, quantitative research and safety-critical UI experience. I want a second opinion, not agreement. Be blunt.

## The project, in four lines

`projectx-desk` is a desk for TopstepX / ProjectX micro futures (MES, MNQ, MGC). A human places every order. `guard.py` is the only order path. Today was the first trading day, on a SIM account.
An advisory "TREND" read (direction plus a letter) sits on the chart bar, on a ribbon strip with one chip per contract (click to switch the chart), and in the AI digest. It is display only: it must never gate or size an order.
Read `AGENTS.md` first (rules with teeth), then `docs/DECISIONS.md` Round 17 (what changed and why).

## What happened today (so you do not repeat it)

- The first version of the grade (`bias.py` v1: votes from 5m/15m/1h EMA stacks, VWAP side, tape delta, plus ADX and ATR bonuses) was backtested on about 21 days of 5m bars for MES, MNQ, MGC with no lookahead:
  forward 30 minute move in ATR units in the bias direction: A/A+ n=1010 mean 0.00, hit 49%; B n=979 mean -0.04, hit 49%; C n=439 mean +0.11, hit 54%. ADX bands, ATR percentile and stretch from the 21 EMA were flat.
- A quant lens and a risk lens (two Sonnet subagents) critiqued it. Their points: the votes are one correlated variable counted several times; naive standard errors are 1.5 to 2x too small (day-clustered); 20 trade days cannot detect a 0.05 ATR edge; the missing-read denominator inflated grades; ATR 90th and score-band cliffs flipped letters on a wobble; a green/red letter implies a signal.
- v2 (hand-set, not fitted): fixed denominator, no ADX/ATR bonuses, an entry-location component (pullback to the 21 EMA scores, chasing scores nothing), caps instead of additive penalties (C when extended, through the EMA, RSI stretched, ADX under 20, or the first 15 minutes after the 08:30 CT open; D when extended and RSI stretched; B when ATR is above the 90th percentile), an ungraded INDEX_DIVERGENCE flag when MES and MNQ disagree, and a per-cycle log (`data/bias_log.jsonl`) for out-of-sample scoring. v2 has NOT been backtested. Say so if you think that matters.
- Also built today: right-click on the chart stages a LIMIT entry (sim only, goes through the same preview, ticket and 600 ms hold); the 08:30 CT open marker and shaded 15 minute opening range; click-to-switch contract chips (refused while a ticket is staged or a position is open); the no-cache headers for the UI.
- Known and not fixed: `MCLE` (micro crude) is not recognised by `sizing.spec_for`, so MCL cannot size (fails closed); a resting limit can only be cancelled with FLATTEN; the cash open is hardcoded to 08:30 CT for every contract; GRANULARITY refusals when ATR is at the 99th percentile.

## What to read

1. `projectx_desk/bias.py` (the whole module, it is short) and its tests `tests/test_bias.py`
2. `projectx_desk/digest.py`: `bias_state`, `htf_stacks`, `bias_board`, the `BIAS:` digest lines, and `indicators()` (the inputs)
3. `projectx_desk/app.py`: `_bias_snapshot`, `_track_loop`, `/api/bias_board`, the `revalidate_ui` middleware
4. `projectx_desk/static/desk.js`: `renderBias`, `boardDraw`, the board click handler, `lmStage` and the contextmenu handler, `orbDays`/`drawOrb`/`orbBox`, `fitPanes`, `snapOv`
5. `docs/DECISIONS.md` Round 17, `AGENTS.md`, and `tests/test_px_guard_pin.py` (the pin that stops a second order path)

Read the code, not just the docs. Where they disagree, the code wins and that is a finding.

## The job

1. **Grade design.** Is v2 sound, or still a trend label dressed as a grade? Which components would you keep, drop, or add, and why? Name the weights you object to. Do NOT propose fitting weights on this sample; say what you would pre-register instead.
2. **Backtest critique.** What is wrong with the v1 backtest and with the conclusion drawn from it? What would a defensible test of v2 look like (data needed, horizon, nulls, multiple-testing correction, how many trade days, which regimes)? Give the exact protocol.
3. **Entry location and reversal.** v2 rewards price near the 21 EMA and caps extended moves. Is that the right instinct for intraday index and gold futures? What does the literature or practice say about pullback-in-trend versus fading extended moves, and what volume or tape evidence would you require before showing "reversal risk"?
4. **Cross-market divergence.** MES versus MNQ (and gold). How would you define and display divergence so it is useful and not noise? Is a bare "directions differ" flag good enough?
5. **UI safety.** Can a click, a stale value, or the chip switching ever make the trader stage or place the wrong contract, side, price or size? Walk the limit-order menu (`lmStage`), the chip switch, the Size-for selector and the ticket. Show the exact sequence for any hole. Is "TREND SHORT B" plus colours still read as a signal by a first-day trader? Propose exact wording or visuals.
6. **Order safety unchanged.** Confirm from the code that nothing new reaches `guard.py` or sizing from the bias read, that the AI surface stays an allowlist of numbers and enums, and that the limit order flow adds no new order path. Find anything I missed.
7. **Day-one operations.** Given the rules in `AGENTS.md` and a $500 daily limit on a trailing-drawdown evaluation: what would you change before the second day, and what single missing rail matters most?
8. **Opening range and sessions.** Check the 08:30 CT open marker and ORB code for timezone, DST, bar-alignment and partial-bar errors. Is a fixed 15 minute range and a hardcoded 08:30 CT sensible for MGC and MCL?

## Rules

- Assume nothing works until you see the code that makes it work. Write "I did not verify X" instead of guessing.
- No flattery, no generic advice, no "consider adding tests". Name the missing test and what it asserts.
- Do not suggest weakening a safety property for convenience unless you state the exact new risk.
- Never ask for, print or invent credentials, account numbers or balances. Do not run anything against a real ProjectX gateway. Read-only: do not modify any file except your own feedback file.
- Do not claim the grade has an edge. Do not claim it has none either: the sample cannot support either claim.
- No em dashes in your output.

## Output

Write ONE markdown file named `reviews/feedback_<MODEL>.md` (for example `reviews/feedback_gpt-6.md`) with this structure:

```
# Feedback from <MODEL>, <date>

## Verdict
(5 lines max: is v2 fit to show a trader tomorrow, and the 3 things that decide it)

## Findings
### F1 <title>  [severity: critical|high|medium|low]  [confidence: verified|likely|guess]
- Where: (file:line)
- What happens: (exact sequence or numbers)
- Fix: (concrete change, and the test that would catch it)

## Grade redesign
(the components you would keep, drop, add; the pre-registered hypotheses; the validation protocol)

## What I did not verify
```

Rank findings by severity. Keep it under 900 words.
