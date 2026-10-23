# Feedback from gemini-4, 2026-10-23

## Verdict
Do not show v2 as a coloured letter grade tomorrow; v2 is unvalidated and reads as a signal.
1. `guard.place()` skips market-side and stop-vs-quote checks on limits, executing tickets whose stop is crossed by live price.
2. `_track_loop` subscribes only to trades, leaving unvisited roots with `last=None` (`C`/`D`) and `"price": null` in `bias_log.jsonl`.
3. `bias.py` grades three `MIXED` stacks plus a 1-tick VWAP lean as `TREND LONG B`, and misses the `D` cap when RSI is 25..30.

## Findings

### F1 Limit `place()` skips live market-side, stop-side, and tick-alignment checks  [severity: critical]  [confidence: verified]
- Where: `projectx_desk/guard.py:584-600`, `guard.py:781-789`, `guard.py:832-836`, `projectx_desk/static/desk.js:1903`
- What happens: `_evaluate()` checks `entry_deviation` symmetrically and compares `stop_price` against `entry`, never live `ref`; `place()` and `_commit_gate()` skip drift checks for `"limit"`. If MES drops below a BUY LIMIT (`5798.00`, stop `5796.50`) to `5796.25` within 120s, PLACE fills at `5796.25` and `_arm_stop` submits a crossed SELL STOP (`5796.50`), triggering `_naked` flatten+halt. `mcp.py:204` bypasses `desk.js:1892` and `_evaluate()` omits `entry` tick alignment.
- Fix: Require `entry` tick alignment, resting side (`entry < bestAsk` long, `> bestBid` short), and `stop_price < bestBid` / `> bestAsk` in `_evaluate()` and `_commit_gate()`; measure `near` from `ask`/`bid` in `desk.js:1903`. Test: `test_limit_place_refuses_when_market_crosses_entry_or_stop`.

### F2 `Size for` mismatch, stale `#mq-stop`, and `stagedOrOpen` holes  [severity: high]  [confidence: verified]
- Where: `projectx_desk/static/desk.js:155-167`, `desk.js:832-852`, `desk.js:1372-1378`, `desk.js:1840`, `desk.js:1864-1895`, `projectx_desk/digest.py:607-618`
- What happens: (1) Clicking `MNQ` on the Bias Board with `S.sizeFor` on `MES`/`ES` leaves `S.sizeFor` stale: `/ai/state` polls `MES` on the `MNQ` chart and `manualStage`/`STAGE`/`stageCallout` stage `MES`. (2) `load()` retains `#mq-stop` and `manualStage`/`lmStage` omit `S.sizeCid === cid`, staging stale stop ticks. (3) `stagedOrOpen()` omits `S.ai.orders`/`!S.ai` over working limits, and `FLATTEN` leaves `t.state == "PLACED"` in `digest.active_trade`.
- Fix: Reset `S.sizeFor='AUTO'` and clear `#mq-stop` in `load()`; require `S.sizeCid === cid && !S.aiFails` in `manualStage`/`lmStage`; use `!isFlat()` in `stagedOrOpen()`; gate `active_trade` with `guard._ticket_exposed(t)` and add `POST /api/cancel`. Test: `test_cancelled_limit_ticket_not_in_active_trade`.

### F3 Unvisited Bias Board contracts and `bias_log.jsonl` have `last=None`  [severity: high]  [confidence: verified]
- Where: `projectx_desk/app.py:806-841`, `projectx_desk/digest.py:75-100`, `digest.py:465-486`, `projectx_desk/state.py:65-76`
- What happens: `_track_loop` calls `live._ensure_tape(cid)` (trades only), leaving `live.quotes` empty, while `digest.indicators()` drops `cl[-1]`. Unvisited roots (and `PX_RECORD_ONLY=1`) have `last=None`, losing `vwap` (+0.5) and `entry` (+2.0), capping grades at `2.0` (`C`/`D`), and logging `"price": null` without `entry`/`rsi`.
- Fix: Return `"last": _n(last)` in `digest.indicators()`, fall back to `ind.get("last")`, and log `entry`/`rsi` in `_bias_snapshot`. Test: `test_bias_snapshot_falls_back_to_bar_close_and_logs_entry`.

### F4 `MIXED` stacks + 1-tick VWAP grades `B`, and `EXTENDED` + `STRETCHED` misses `D` cap  [severity: medium]  [confidence: verified]
- Where: `projectx_desk/bias.py:18`, `bias.py:34-48`, `bias.py:67-137`
- What happens: (1) `"MIXED"` votes `0` but counts toward `MIN_READS=3`: `5m/15m/1h=MIXED`, `flow=None`, `last > vwap` (`agree=1/4`), and `PULLBACK` (`+2.0`) yields `2.5` (`TREND LONG B`, `caution=False`). Max score is `4.5`, not `/5` (`digest.py:795`, `desk.js:653`). (2) `bias.py:118` checks `RSI_CLIMAX` (`25.0`) instead of `RSI_STRETCH` (`30.0`), capping `EXTENDED` + `rsi=27` at `C`, not `D` (`DECISIONS.md:451`). (3) `_flow_vote` shifts window (`5m`/`15m`/`60m`) and ignores `recorder` flow.
- Fix: Require `>=2` agreeing reads and an aligned `5m`/`15m` stack for `PULLBACK`; use `RSI_STRETCH` at line 118; pin `_flow_vote` to `15m`. Test: `test_mixed_stacks_cannot_grade_b`.

### F5 ORB and `08:30 CT` open misalign on `15m`/`30m`, partial bars, `MGC`, and `MCLE`  [severity: medium]  [confidence: verified]
- Where: `projectx_desk/static/desk.js:564`, `desk.js:1849`, `desk.js:1922-1969`, `projectx_desk/bias.py:19`, `projectx_desk/sizing.py:53`
- What happens: `orbDays()` runs on `30m` (`per.span < 60`), shading `08:30-09:00 CT` as a 15m ORB; on `15m`/`30m`, `t0 === t1`; before `08:45 CT`, `toEdge` extends unclosed highs/lows; missing `08:30` bars drop ORB. `08:30 CT` is wrong for `MGC` (`07:20 CT`)/`MCL` (`08:00 CT`), and `MCLE` is missing from `sizing.CONTRACT_SPECS` and `desk.js`.
- Fix: Require `15 % per.span === 0`, set `t1` to bar end, wait until `08:45 CT`, map open times per root, and alias `"MCLE"` in `sizing.py`/`desk.js`. Test: `test_spec_for_mcle`.

### F6 Coloured letter chips read as signals, and static `PX_MLL_FLOOR` misses intraday trail  [severity: medium]  [confidence: verified]
- Where: `projectx_desk/static/desk.js:649-654`, `projectx_desk/static/desk.css:785-795`, `projectx_desk/guard.py:382-391`
- What happens: Green `TREND LONG A` / red `TREND SHORT B` violates `desk.css:364` and invites signal-chasing; 99th-pctile ATR `GRANULARITY` refusals tempt tightening stops to `1.0x` ATR. On a $500 DLL / trailing-MLL eval, `guard.dd_room()` uses a static `PX_MLL_FLOOR` and never trails intraday equity highs (`balance + max(0, open_pnl)`), the top missing rail.
- Fix: Use neutral text (`ALIGNED ▲ 4/5 · NEAR EMA21` or `CAUTION: EXTENDED`), and trail `mll_floor` from an intraday high-water mark in `DayState`. Test: `test_mll_floor_trails_intraday_high_water`.

## Grade redesign
- **Components & weights**: Keep `15m`/`1h` stacks, VWAP, `ADX < 20`, open chop, and `EMA21` ATR distance. Drop letters (`A`-`F`), unconditional `+2.0` `PULLBACK`, and variable `_flow_vote`; add fixed `15m` delta and require `15m` stack alignment for `PULLBACK`. Trends stay `>2 ATR` from `EMA21` for hours; require tape absorption (`range >= 1.5 ATR`, `vol >= p90`, opposing wick `>=50%` or opposing 5m delta) before `REVERSAL_RISK`, and require opposing `LONG`/`SHORT` (`agree >= 3`) or 60m high/low non-confirmation (`>=0.5 ATR`) for ES/NQ divergence.
- **Backtest critique & protocol**: The 21-day v1 test used overlapping 5m bars (`N_eff ~ 60-90` after MA(5) overlap and MES/MNQ correlation) and unconditional 30m returns instead of stop/target hit rates (`C`'s `+0.11 ATR` is short-horizon mean reversion; v2 is untested). Pre-register on `>=60` days: (H1) given `15m == 1h`, `PULLBACK` hits `+1.5R` before `-1R` (`1.5x ATR` stop, 60m cap) more often than `MID`/`EXTENDED`; (H2) caution flags lower `+1R` hit rates via day-clustered block-bootstrap against always-long/short and random-time nulls (`q=0.05`).

## What I did not verify
- Verified: `guard.py`/`sizing.py` ignore `bias.py`, `test_px_guard_pin.py` holds, `/ai/*` stays numbers/enums. Unverified: live gateway, Topstep MLL rules, browser rendering.
