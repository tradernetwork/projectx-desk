# Statistics review — 2026-10-17

Written by a model the owner did not name, against commit `b9c6b27`, from the README, `docs/`, `levels.py`, `respect.py`,
`sizing.py` and `guard.py`. Reproduced verbatim below the line; the response follows it. Nothing in the review was run against a
gateway. Numbers it quotes about Topstep come from the help articles cited in `docs/TOPSTEP_RULES.md`.

---

## What you actually sent me

Not a phenomenon. An execution and risk harness. There is no alpha in this repo — the README says so twice ("No signals of its own", "History, not a forecast"). So I'll grade it the way it should be graded: as a **cost-control and behavioral-leak layer**, plus the one genuinely research-shaped thing buried in it (`levels.py`).

Honest build. The AST pin on the order path, the single-attempt write with `UNKNOWN` → halt, the placebo baseline on levels, the "what it does not model" section — that is more intellectual hygiene than 95% of trading repos. The problems are statistical, not engineering.

---

## 1. The claim (restated as rules)

Four separable claims live in here. Grading each.

**C1 — Behavioral rails.** *Forced size-from-remaining-loss-room, mandatory stop, cooldown after a loss, lockout after 3 consecutive losses, and a post-loss risk cap, raises terminal equity for a discretionary trader whose avg loss > avg win.*

**C2 — Respected levels.** *Among ~25 candidate lines × 5 timeframes, those whose bounce rate exceeds a placebo baseline (same line shifted ±0.5/1.0/1.5 ATR) by ≥15 percentage points, with n≥8 resolved tests and passing in both halves, are more likely to produce a bounce on the next test.*

**C3 — Size scalers.** *Cutting size when ADX(5m) < 20 or ATR is in its top quintile improves risk-adjusted outcome.*

**C4 — Level-anchored stops.** *Tucking the stop just beyond a "respected" level (floor 1.5×ATR, ceiling 2×ATR) improves expectancy vs a flat ATR stop.*

---

## 2. Verdicts

| | Verdict | Confidence | Biggest reason |
|---|---|---|---|
| **C1 Behavioral rails** | **Established (mechanism), unproven (this implementation)** | med | Disposition effect and post-loss risk-taking are documented in CME locals, KOSPI futures day traders and retail FX. The *mechanism* is real. The *evidence offered* is 42 stock round trips over one month. |
| **C2 Respected levels** | **Probably noise as implemented** | high | `min_n=8`, `margin=15.0`. See the arithmetic below. The design is right; the thresholds make it a random number generator. |
| **C3 Size scalers** | **Weak** (ATR cut) / **Probably noise** (ADX cut) | med | The ATR cut double-counts — the repo admits this. ADX<20 as a size filter has no literature behind it. |
| **C4 Level-anchored stops** | **Plausible, direction unknown** | low | Inherits C2's classification error, and crowded levels are where stops get farmed. Could easily be negative. |

---

## 3. The number that kills C2

`verdict()` fires `ABOVE_RANDOM` when the gap ≥ 15pp with as few as 8 resolved tests.

Standard error of a proportion at p≈0.6, n=8:

√(0.6·0.4/8) = **0.173 → 17.3pp**

So the acceptance threshold (15pp) sits at **0.87 standard errors**. Under the null, roughly **19%** of candidates clear it by chance. The desk tests ~25 candidate levels across 5 timeframes:

125 × 0.193 ≈ **24 expected false "ABOVE_RANDOM" winners per run**, with zero real effect present.

The STABLE badge (both halves) helps but doesn't save it — each half now has n≈4, SE ≈ 24.5pp, P(pass) ≈ 0.27, squared ≈ 0.073 → still ~9 false STABLE badges out of 125.

To detect a true 15pp gap (60% vs 45%) at 95%/80% power you need:

7.849 × (0.24 + 0.2475) / 0.15² ≈ **170 resolved events per arm**

The code accepts **8**. That is a 21× shortfall. The "tested K candidates" disclosure next to winners is a good instinct, but disclosure is not a correction.

**Three more problems with C2, in order of severity:**

1. **The placebo is contaminated.** Shifting EMA21 by −1.0 ATR produces something very close to a Keltner lower band — which is itself in the candidate set. The control line is a real level. That biases the placebo rate *up*, which suppresses true positives, but it also makes the gap statistic uninterpretable.
2. **Asymmetric thresholds.** BOUNCE needs 0.75 ATR but only on a wick (`h`/`l`). FAIL needs 0.25 ATR but on a *close*. Different distances, different price types. That is not a fair coin, and the direction of the bias depends on bar-level wick distribution — i.e. on volatility regime. Your "respected level" rate will drift with VIX for mechanical reasons.
3. **Degenerate on dynamic levels.** For EMAs, the level moves toward price every bar. A `touch` on a fast EMA in a chop is nearly guaranteed; `cluster=3` dedupes grinding but 3 bars is thin on 5m.

**What to do instead:** raise `min_n` to 30 (and label anything under 100 as indicative), apply a Benjamini-Hochberg FDR correction across the 125 tests at q=0.10, build the placebo from a *circular block bootstrap of the return series* rather than ATR-shifted copies of the same line, and symmetrize bounce/fail on the same price type. Then re-run. My prior: most of the 125 collapse and you keep prior-day high/low, overnight high/low, and VWAP. Those three have the clearest mechanism — they are where resting orders and benchmark-execution flow actually sit. EMA 34 does not.

---

## 4. Mechanism check

**C1 has the best mechanism in the repo.** Who pays: you, to yourself. Documented: Locke & Mann on CME floor locals riding losses; KOSPI 200 futures day traders reluctant to close losers; retail FX disposition effect (Ben-David & Hirshleifer). The "size 55% bigger after a loss" pattern is the textbook post-loss risk-seeking result. A hard lockout is the only known reliable fix because the trader cannot talk himself past an `HTTP 403`.

But note what the rails do and don't do. They **compress the left tail**. They do not create expectancy. A negative-expectancy trader with perfect risk control still goes to zero, just slower and with a prettier equity curve. The README says this ("It will not make you profitable"). Believe that line.

**C3's mechanism is borrowed and misapplied.** Vol targeting improves Sharpe on equities because expected return does not rise with volatility (Man Group, Harvey et al). That is a *portfolio-level, slow-moving* result. It does not transfer to "cut MNQ size when 5m ATR is in its top quintile." And crucially: sizing already contains 1/ATR, because contracts = risk$ / (1.5 × ATR × tick value). Multiplying by 0.67 on top gives you exposure ∝ ATR^−1.4. You are short volatility twice. Set `PX_SIZE_SCALERS=0`. The repo tells you this itself; take the advice.

**C2's mechanism is the weakest link.** Osler (2000, FRBNY) found published FX support/resistance levels *did* predict intraday trend interruptions — but those were **round numbers and levels published to customers**, i.e. coordination points where orders actually rested. That is a mechanism: a limit order book clusters there. An EMA 34 on a 5m chart has no such book. Prior-day high, overnight high, VWAP, round hundreds on ES — those do. The repo's candidate set mixes both and treats them identically.

---

## 5. Costs — the part that decides everything on micros

You're on MES/MNQ. MES = $1.25/tick, MNQ = $0.50/tick.

Take a representative MES trade: 5m ATR ≈ 2.5 pts, stop = 1.5 × ATR = 3.75 pts ≈ 15 ticks ≈ **$18.75 risk per contract**.

Costs, 1 contract round turn: commission ~$0.70–1.00, plus realistic slippage of one tick on entry and one on the stop exit ≈ $2.50. Call it **$3.00–3.50 all-in**.

Breakeven win rate at 1:1 R:R with cost c:

p = (R + c) / 2R = (18.75 + 3.25) / 37.50 = **58.7%**

You need to be right **8.7 percentage points** more often than a coin just to cover friction. That is before anything else. On MNQ with a 1.5×ATR stop the ratio is worse because the tick value is $0.50 against the same commission.

The implication for the whole design: **anything that raises turnover is lethal here**, and the cooldown-after-a-loss rail is, by accident, the most valuable P&L feature in the repo — not because of behavior, but because it cuts trade count. Measure it that way. Log cost as a percentage of R on every trade and put it in the digest next to win rate. Right now the digest shows `SIZE_WHY` reasoning in detail and shows cost nowhere.

---

## 6. Failure modes

- **Realised-P&L-only accounting vs Topstep's MLL.** The MLL is computed off end-of-day balance but **monitored in real time and breaches on unrealized P&L** (Topstep help article 8284204). The desk counts realised only and treats the budget as a number you type in. So: one large open loser, inside your stop, inside the desk's rules, and the account is liquidated while the digest reads `locked=NO`. This is the single most dangerous gap in the repo. The README discloses it. Disclosure does not make it safe. Fix: feed the live MLL floor from the account and include open P&L in the remaining-room calculation, or run with a manual haircut of at least one full stop's worth.
- **Tape gaps.** Buy/sell volume is recorded from process start with no backfill. Every restart is an unrecoverable hole. Any statistic computed off the tape is therefore conditioned on your uptime, which correlates with nothing useful. Treat delta as intraday context only, never as an input to a rule with a history.
- **Alpha status, no real fill.** "No order has ever been placed through a real gateway." The restart-protection logic, the UNKNOWN handling, and the stop-ack-within-5-seconds flatten have been tested against a mock that the same author wrote. Mocks encode the author's assumptions about the venue. The failure you care about is the one he didn't imagine.
- **Loopback = no auth.** Anything on your machine that can reach port 8613 can stage and place within your caps, including an agent with a shell. The 600ms hold is, as stated, anti-fat-finger only.
- **The real risk vector is the analyst callout.** A Discord bot posts an entry, a banner flashes, the chart switches, their levels get drawn, and STAGE is one click. The discipline layer has just made an **unmeasured third-party signal** frictionless. Every statistical control in this repo points at levels; zero point at the callout feed. That is backwards.

---

## 7. Invalidation

Specific numbers that kill each piece:

- **C2 dies** if, after raising `min_n` to 30 and applying FDR at q=0.10 across all 125 candidate×timeframe tests, fewer than 5 candidates survive on 12 months of 5m data. My prediction: 0–3 survive, all of them prior-day/overnight/VWAP.
- **C1 dies as implemented** if, over 200+ logged trades, the distribution of realised R after a loss is statistically indistinguishable from after a win (two-sample KS, p > 0.10). That means the rails are binding on nothing.
- **C3 dies** if, over 200+ trades, the scaler-on subset has a lower R-per-trade *and* a lower R-per-unit-of-realised-vol than the scaler-off subset. Given the double-counting, I expect this.
- **C4 dies** if stop-out rate within 1 bar of the fill is ≥ 1.4× the rate under a flat 1.75×ATR stop. That's the adverse-selection signature at crowded levels.

---

## 8. Next tests, smallest first

1. **Add cost-as-%-of-R to the digest.** One line. Acceptance: it prints. Changes: you stop taking sub-$15-risk trades on MNQ once you see 20%+ of R going to friction.
2. **Fix the C2 thresholds.** `min_n=30`, FDR correction across candidates, symmetrize bounce/fail on close-vs-close. Acceptance: the surviving-candidate count drops and is stable across two independent 6-month windows. Changes: the Respect overlay becomes 2–4 lines instead of a decorated chart, and C4 inherits a signal worth anchoring to.
3. **Rebuild the placebo properly.** Circular block bootstrap, 1000 draws, block length 20 bars. Acceptance: the null distribution of the gap statistic is characterized, so the verdict becomes a p-value rather than a magic 15. Changes: C2 moves from Probably noise to either Plausible or Dead — you'll finally know which.
4. **Put the MLL on unrealized.** Acceptance: digest shows `room_including_open` and refuses opens against it. Changes: the desk stops being able to lie to you on the one number that ends the account.
5. **Backtest the callout feed.** 100+ historical ENTRY posts, entry/stop/T1 as published, net of the costs above. Acceptance: lower bound of a bootstrap CI on expectancy > 0. Changes: either the one-click STAGE button is justified, or it is the most expensive feature in the repo and should be removed.

---

## Bottom line

Keep it for what it does well: it stops you from doing the dumb thing, and the cooldown cuts turnover, which on micros is worth more than any level. Turn the scalers off. Treat the Respect overlay as decoration until test 2 and 3 pass. Fix the unrealized-P&L gap before you point it at a Combine account. And do not let a frictionless STAGE button in front of an unvalidated signal feed be the thing that uses up all the discipline you just built.

---

# Response (2026-10-17, Round 10 in `docs/DECISIONS.md`)

Every claim was checked against the code at `b9c6b27` before anything moved. Verdict per item:

| Review item | Checked against the code | Done |
|---|---|---|
| §3 `min_n=8`, 15pp = 0.87 SE | **Correct.** `respect.MIN_N = 8`, `levels.verdict(min_n=8)`. What it missed: the Round 5 surrogate calibration already held the measured false-positive rate on 600 null rows to 2 (Gaussian walks) and 0 (volatility-switching walks). Calibration is still not a significance test. | `MIN_N = 30`, `HALF_MIN_N = 15`. New `levels.binom_sf` (exact one-sided binomial) and `levels.benjamini_hochberg`. RESPECTED now needs the gap, the p-value and the FDR pass at q = 0.10 over the whole scan. Rows show `p_value`, `fdr`, `family`. Measured on the same 600 null rows: 2 and 0 after, same as before; the gates cost nothing on the null and bind on real data. |
| §3.1 placebo contaminated (EMA21 − 1 ATR ≈ Keltner lower) | **Partly.** Keltner is EMA20 ± 1.5/2.0 × ATR10; the placebo offsets are ±0.5/1.0/1.5 × ATR14. They sit near each other. The direction is conservative (the gap shrinks), and the surrogate calibration exists for exactly the self-referential case. | Not changed. The block-bootstrap surrogates (below) are the correction for lines that are functions of the same bars. |
| §3.2 wick-vs-close asymmetry | **Correct about the hold RATE, not about the gap.** The placebo lines are scored with identical rules at the same bars; the mechanical component cancels to first order in the gap and the p-value. | Not changed; documented in the `respect.py` docstring. Symmetrising would redefine "held" on every chart and fixture for a bias the design already cancels. |
| §3.3 degenerate on EMAs | **Correct as a description;** the placebo is a shifted copy of the same EMA, tested at the same frequency, so it is the matched control for exactly this. | Not changed. |
| §3 "what to do": block bootstrap, 1000 draws | **Right idea, wrong budget.** The scan runs in the request path in pure Python on five timeframes. | Surrogates are now a circular block bootstrap of the bar shapes (block 20), still 3 draws, calibrating the placebo rate. The p-value is exact against that rate, not drawn from the surrogates. Said plainly in Round 10. |
| §4 C3: ATR scaler double-counts, set `PX_SIZE_SCALERS=0` | **Correct.** Round 9 admitted it and shipped the default on anyway. | Default is now OFF. `PX_SIZE_SCALERS=1` enables. Both cuts still only ever reduce. |
| §5 cost shown nowhere | **Correct.** No commission or slippage figure existed anywhere in the package. | `SIZE_COST` in the size box, `/ai/state` and the digest: cost per contract per round turn, % of 1R, win rate 1:1 needs after costs. `PX_COST_RT_USD` (unset prints NA, never 0) and `PX_SLIPPAGE_TICKS` (default 2). `KPI_COST` reports fees actually paid today. Stream mode hides the dollars. |
| §6 realised-only vs the MLL | **Correct on the gap, stale on one fact:** with `PX_MLL_FLOOR` the room already followed the live balance (`Guard.dd_room()`), it was not only a typed number. But that balance moves on fills, so open P&L was indeed uncounted except through our own tickets' stop risk. | `Guard.open_pnl()` marks every open position from a fresh quote; `Guard.draw()` is the worse of committed stop risk and the open loss; both rooms are net of it. An unvaluable position makes the room UNKNOWN and opens refuse (`open_pnl_known`). `RISK_OPEN` shows `open_pnl` and `draw`. Assumption stated: the gateway balance excludes open P&L (true of the mock, unverified live). |
| §6 tape gaps, mocks, loopback | **Correct and already disclosed** (`docs/LIMITATIONS.md`). | No change. |
| §6/§8.5 callout STAGE is frictionless | **Correct.** One click from a Discord post to a staged ticket, no measurement anywhere. | One-click STAGE is behind `PX_FEED_STAGE=1` (default off; banner, chart switch and drawn levels stay). When used, the ticket is audited `stage_source=callout`. The backtest needs history the desk does not store; not pretended. |
| §7 invalidation tests | **Agreed as the tests to run.** They need 200+ logged trades and 12 months of 5m bars; neither exists here. | The audit now records ticket, risk and source, which is the log those tests need. |

What was NOT done, on purpose: the 1000-draw null distribution (runtime), symmetrising bounce/fail (already cancels in the gap), and
the callout backtest (no data). The README's "What it does not model" was rewritten so it no longer claims realised-only accounting.
