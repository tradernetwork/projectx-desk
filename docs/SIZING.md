# Position sizing

The desk never leaves sizing to mental math. `projectx_desk/sizing.py` is the single source of truth and every size shown in the
UI, the digest and the MCP tools comes from it.

## The rule

```
loss_room     = balance - PX_MLL_FLOOR                                  # the firm's loss limit is the base, not the account size
risk_budget   = min(risk_pct x loss_room, daily loss room, loss_room)   # 1R in dollars; shrinks as you lose; PX_RISK_BASE=remaining restores the old base
stop_ticks    = ceil(k x ATR(14, 5m) / tick_size)                       # k = 1.5 by default: the stop (ATR) sets the size
risk_per_ct   = stop_ticks x tick_value                                 # live tickValue from the contract record wins
contracts     = floor(risk_budget / risk_per_ct)                        # ALWAYS rounds down, never up
```

Default `risk_pct` is 5% of the loss room (chips 2/3/5/6). On a 50K Combine that is $100 of 1R while the room is $2,000,
which fits one MNQ at a 1.5 ATR stop but not two. An unreadable balance or floor sizes to 0. Micros only (`PX_ALLOWED_ROOTS`
defaults to MES, MNQ, MCL, MGC).

If `contracts` is 0 the answer is **REFUSED (granularity)**: one contract risks more than the budget. It is never
rounded up to 1. The trader's options are shown (tighter stop, the micro, or skip), not applied.

## Reduce-only scalers (hypotheses, untested)

ADX and ATR can only ever shrink the risk you chose, never grow it, and unknown inputs shrink it too:

| Condition | Multiplier |
|---|---|
| ADX < 20 (chop) | x0.67 |
| ATR above the 80th percentile of its recent history | x0.67 |
| ADX or ATR percentile unknown | x0.75 each |

These thresholds are starting points to be backtested, not findings. `PX_SIZE_SCALERS=0` turns them off. Every size
answer lists the reasons (`size.why`), so the UI can always say why it is on this many contracts.

## Behavioral rails (refuse or reduce, never allow more)

Built from the observation that the usual leaks are exits and sizing, not entries: an inverted payoff (average loss
much larger than average win), sizing up after a loss, and one outsized loss.

- cooldown after a losing trade (`PX_COOLDOWN_AFTER_LOSS_S`, default 300)
- session lockout after N straight losses (`PX_MAX_CONSEC_LOSSES`, default 3)
- after a loss, risk may not exceed what the losing trade risked (`PX_SIZE_AFTER_LOSS_LOCK`)

## Honesty about statistics

Every stat shows its sample size N and a SMALL SAMPLE badge under 30 trades. Telling a 55% win rate from 50% takes
roughly 780 trades; a handful of trades proves nothing. The KPI card shows win rate against the break-even win rate
implied by your payoff ratio, because that is the number that decides whether you are ahead.
