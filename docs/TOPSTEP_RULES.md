# Topstep rules and what this desk does about them

Collected 2026-10-15 from Topstep's own help center and rules pages (`help.topstep.com`, `topstep.com`). The pages were read
through a summarizing fetch tool, so quotes are relayed, not byte-checked. **Re-read the linked article before you rely on a
number.** Topstep changes these rules; several pages show only a relative "updated" age. This is not legal or compliance
advice, and the desk has never been run against Topstep's real gateway.

Status column: **ENFORCED** the guard refuses or acts on it. **CONFIG** you set a number and the guard uses it. **SHOWN** the
desk displays it but does not stop you. **NOT COVERED** the desk does nothing; you must handle it. **N/A** does not apply to how
the desk works.

H = `https://help.topstep.com/en/articles/`

## Rules that can end the account

| Rule | Topstep says | Applies to | Desk status |
|---|---|---|---|
| Maximum Loss Limit (MLL) | $2,000 / $3,000 / $4,500 for $50K / $100K / $150K. Trails your highest end-of-day balance upward, never down. Watched in real time including unrealized loss. Combine: locks at the starting balance; breach liquidates and you cannot be funded until a Reset. Express Funded: breach closes the account. ([H8284204](https://help.topstep.com/en/articles/8284204-what-is-the-maximum-loss-limit)) | Combine, Express | **CONFIG.** Set `PX_MLL_FLOOR` to the floor TopstepX shows you; drawdown room is then live balance minus floor, less the stop risk of orders already open. Without it, `PX_DRAWDOWN_BUFFER` is a number you typed. The desk does not compute the end-of-day trail or the lock: **you update the floor when it moves.** Unrealized loss is covered only through the stop on each open trade. |
| Flat by 3:10 PM CT | "All positions must be closed by 3:10 PM CT each weekday." Avoid new entries after 3:08 PM CT. The trading day runs 5:00 PM CT to 3:10 PM CT. An earlier product close means an earlier flat. ([H8284206](https://help.topstep.com/en/articles/8284206-when-and-what-products-can-i-trade)) | all | **ENFORCED** with `PX_FLAT_BY`. Set it to `15:08` or earlier. New entries are refused in the 2 minutes before it, and at the deadline the desk flattens every position and cancels working orders (sim), retrying every 5 seconds and halting if it cannot confirm flat. **Not covered:** holiday and early-close schedules (CME calendar); lower `PX_FLAT_BY` yourself on those days. |
| Daily Loss Limit (DLL) | $1,000 / $2,000 / $3,000. **Optional** in the Combine and Express, automatic on Live. Hitting it flattens, cancels orders, and locks you out until 5 PM CT; it is not a rule violation. ([H10490293](https://help.topstep.com/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account)) | Combine, Express, Live | **CONFIG.** `PX_DAILY_LOSS_LIMIT` is your own number and the desk refuses opens when day P&L plus open stop risk uses it up. It does not read Topstep's DLL and is not a substitute for it. |
| Personal Daily Loss Limit (PDLL) | A manual setting in TopstepX Risk Settings: do nothing, liquidate, or liquidate and block. ([H10490293](https://help.topstep.com/en/articles/10490293-daily-loss-limit-in-the-trading-combine-and-express-funded-account), [H14434175](https://help.topstep.com/en/articles/14434175-topstepx)) | Combine, Express | **NOT COVERED by the desk. Recommended:** set a PDLL in TopstepX as a second, platform-side brake that works even if the desk is down. |
| Maximum position size | 5 / 10 / 15 minis (50 / 100 / 150 micros); micros and minis count 10:1; you may always trade less. ([H8284197](https://help.topstep.com/en/articles/8284197)) | Combine | **CONFIG.** `PX_MAX_QTY_ORDER` / `_SYMBOL` / `_TOTAL` default to 1 / 2 / 3 contracts, far inside the limit. The desk counts contracts, not mini equivalents, so if you add minis to `PX_ALLOWED_ROOTS`, keep the totals low yourself. |
| Scaling Plan | Express Funded accounts have contract limits tied to balance, and the maximum does not rise mid-session. The tier table is an image on the page, so it is **not recorded here**. ([H8284223](https://help.topstep.com/en/articles/8284223-what-is-the-scaling-plan)) | Express | **NOT COVERED.** Read the plan in TopstepX and set `PX_MAX_QTY_*` to match. |

## Objectives (they decide whether you pass or get paid, not whether you are closed)

| Rule | Topstep says | Desk status |
|---|---|---|
| Profit target | $3,000 / $6,000 / $9,000 ([H8284208](https://help.topstep.com/en/articles/8284208-what-is-the-consistency-target)) | **NOT COVERED.** |
| Consistency, Combine | Best single day should stay at or under 55% of the profit target. Going over raises the target; it is not a fail. | **NOT COVERED** (no best-day tracking yet). |
| Consistency, Express (Consistency path) | Largest day over total net profit at or under 40%, minimum 3 trading days; resets each payout. The Standard path has none. ([H8284215](https://help.topstep.com/en/articles/8284215-express-funded-account-parameters)) | **NOT COVERED.** |
| Payout rules, inactivity | Standard path: 5 winning days of $150 or more; payout caps by size; accounts idle over 30 days "may be closed" ([H8284233](https://help.topstep.com/en/articles/8284233-topstep-payout-policy)). | **NOT COVERED.** |

## Conduct rules (judgment, not arithmetic)

| Rule | Topstep says | Desk status |
|---|---|---|
| **Orders must come from your own device** | "Custom automated strategies and bots are allowed via the TopstepX / ProjectX API", but all trading must originate from your personal device. A VPS, VPN or remote server may log, backtest or read, but **cannot place, modify, cancel or route orders.** High-frequency trading is prohibited. You are solely responsible for what automation does. ([H11187768](https://help.topstep.com/en/articles/11187768-topstepx-api-access)) | **Read this before deploying.** A human clicking PLACE on a desk running on your own machine or home network fits the stated conditions. **Putting the desk on a cloud VPS, or any server you do not physically control, is the case the rule prohibits** (the desk's order writes would originate there). A home LXC or Docker host on your own hardware is likely fine, but that is a judgment to confirm with Topstep support in writing before relying on it. See [DEPLOY.md](DEPLOY.md). |
| API not on Live | Automation through the API is not available on the Live Funded Account; it works on Combine and Express (simulated). | **N/A.** Topstep Combine and Express accounts are simulated, so use `PX_ACCOUNT_KIND=sim`. `PX_ALLOW_LIVE` should stay 0 for Topstep. |
| Hedging across accounts | Opposing positions in the same or correlated instruments across your accounts are prohibited, even briefly or by accident, and copiers count. Same market in different accounts is fine if not opposing. ([H13747047](https://help.topstep.com/en/articles/13747047-understanding-hedging)) | The desk is bound to one account (`PX_ACCOUNT_ID`) and refuses any other. **NOT COVERED** across accounts: if you run a copier or several accounts, you must keep them from opposing each other. |
| News | The only prohibition is purposefully trading your **full maximum position size** directly into a scheduled major news event. No blanket ban. ([H10305426](https://help.topstep.com/en/articles/10305426-prohibited-trading-strategies-at-topstep)) | Quantity caps keep you far under max size, but the desk has **no news calendar**. **NOT COVERED.** |
| Prohibited strategies | Includes: account stacking; unfair technology (software, AI, ultra-high-speed or mass data entry); trading outside real market behavior or outside the best bid and offer; SIM exploitation (scalping algorithms on simulated fills, hundreds of rapid trades for queue position, reckless gap trades, no-slippage stop abuse, tight bracket or auto-breakeven abuse); full max size into major news. ([H10305426](https://help.topstep.com/en/articles/10305426-prohibited-trading-strategies-at-topstep)) | Partly shaped by design: a human places every order, `PX_MAX_ORDERS_DAY` defaults to 10, the minimum stop is 4 ticks (TopstepX's own minimum bracket distance is 4 ticks), and entries are refused if the market drifted. The "unfair technology" wording is broad; whether an AI that stages tickets for you counts is **Topstep's call, not ours**. Ask them. |
| Prohibited conduct | 18 listed items, including price exploitation, spoofing, external or slow data feeds, coordinated trading, VPN or proxy use to bypass geographic limits, trading for others, platform bug exploitation. Outcomes range from a warning to deleted days, resets, closure and payout denial. ([H10296582](https://help.topstep.com/en/articles/10296582-prohibited-conduct)) | **NOT COVERED.** Read the full list. |
| Trade copier | TopstepX's native copier works on Combine and Express only. The lead account must have the smallest max position size. | **N/A** (the desk does not copy). |
| Multiple accounts | Several Combines under one profile; up to 5 active Express accounts. Multiple profiles violate the terms. | **N/A.** |

## Recommended settings for a $50K Combine (starting point, not advice)

```
PX_ACCOUNT_KIND=sim
PX_ALLOW_LIVE=0
PX_BRACKET_MODE=position
PX_FLAT_BY=15:08
PX_NO_NEW_AFTER=14:55
PX_DAILY_LOSS_LIMIT=500
PX_MLL_FLOOR=<the floor TopstepX shows you today, in dollars>
PX_MAX_QTY_ORDER=1
PX_MAX_ORDERS_DAY=10
```

`PX_DAILY_LOSS_LIMIT=500` is deliberately under Topstep's $1,000. Set a PDLL in TopstepX too.

## Not found in official pages

Scaling Plan tier numbers (image only), Dynamic Live Risk Expansion mechanics, numeric API rate limits (they live in the ProjectX
terms), a Combine inactivity rule, holiday handling beyond "follow CME holiday schedules", and Live account consistency or
per-size contract limits. Where two official pages disagreed, the 2026 help-center article was taken over the older marketing
page (for example, an older Express rules page says non-TopstepX platforms also have a Daily Loss Limit; the 2026 articles call
the DLL optional).

## Gaps this table leaves for you

The desk does not track the end-of-day MLL trail, the profit target, either consistency percentage, payout eligibility, the
scaling plan or news events, and it ships no holiday calendar: list early-close dates yourself in `PX_EARLY_CLOSES` (see
[LIMITATIONS.md](LIMITATIONS.md)). Those are listed in [LIMITATIONS.md](LIMITATIONS.md).
