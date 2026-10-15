# Review prompt: projectx-desk, blueprint vs what got built

Paste everything below the line into the model. Replace `<MODEL>` with a short slug.

---

You are a senior reviewer with futures trading, broker-API and safety-critical software experience. I want an independent second opinion, not agreement.

## The project

`projectx-desk` (https://github.com/tradernetwork/projectx-desk, local path `~/projects/projectx-desk`) is a desk for TopstepX / ProjectX micro futures. A human places every order. An AI assistant (Codex in Chrome by voice, or any MCP client) can read the desk, compute size and STAGE a ticket, but cannot place, flatten, cancel or clear a halt. It was built and tested only against a mock gateway (`dev_gateway.py`). It has never touched a real gateway, so every claim about ProjectX behavior is unverified until a sim rehearsal.

## What to read, in this order

1. `README.md` (what we say it is)
2. `docs/DECISIONS.md` (THE BLUEPRINT: the rulings D1 to D10, the round 2 requirements, and the "intent vs built" table)
3. `AGENTS.md` (rules with teeth)
4. `docs/LIMITATIONS.md` (what we admit is unverified or missing)
5. `docs/SIZING.md`, `docs/GEX_FUTURES.md`, `docs/AI_AGENTS.md`, `docs/DEPLOY.md`
6. Code: `px_guard.py` (the only order path), `px_client.py`, `px_limits.py`, `px_state.py`, `px_digest.py`, `px_mcp.py`, `px_app.py`, `px_net.py`, `sizing.py`, `px_levels.py`
7. Tests: `tests/test_px_guard_pin.py` and whatever else you need to check a claim

Read the code, not just the docs. Where a doc and the code disagree, the code wins and that disagreement is a finding.

## The job

Walk the blueprint item by item (D1 to D10, the three round 2 requirements, the "v1 can place a sim order" checklist) and for each one say:

- **Built as designed / built differently / not built / built but not wired**, citing file and line.
- Whether the built version is stronger or weaker than the blueprint, and why.

Then give me your own ideas. Rank them. Be concrete.

## Specific questions

1. **Order safety.** Find a path where an order write happens twice, happens without a stop, happens while halted, or happens that nobody approved. Include crash and restart timing, hub reconnects, and clock or trading-day roll. Show the exact sequence.
2. **UNKNOWN handling.** Is "never replay, halt, reconcile by tag" sound given ProjectX facts we have NOT verified (customTag search, tag length, whether a timed-out POST can still land)? What would you verify first in a sim rehearsal, in order?
3. **Prop-firm rules.** Trailing drawdown, daily loss, flat-by time, consistency rules. Is the "conservative budget" model defensible? What do firms do that this model misses?
4. **AI surface.** The MCP and `/ai/*` are an allowlist with no place tool. Can a hostile contract name, order tag or gateway error still reach the model as an instruction? Can an agent with only these tools cause harm anyway (staging spam, halt abuse, info leaks)?
5. **Sizing and the behavioral rails.** Round-down sizing, reduce-only ADX/ATR scalers (thresholds are untested hypotheses), cooldown after a loss, lockout after 3 losses, no sizing up after a loss. Which rails are worth it, which are noise, which are missing? What is the single most valuable rail that does not exist yet?
6. **Network exposure.** Binding 0.0.0.0 behind `PX_OPERATOR_TOKEN` with HTTP Basic, Host allowlist, and a separate MCP bearer. What is the weakest link? What would you change before this goes in a Docker container or LXC on a tailnet?
7. **Docs.** Cold-read the README as a trader who has never seen it. Where do you get lost, what claim feels unearned, what is missing? Is anything in the docs overclaiming?
8. **Product.** What would make a retail futures trader on an evaluation account actually keep using this? What should be cut?

## Rules for your review

- Assume nothing works until you see the code that makes it work. Say "I did not verify X" rather than guessing.
- No flattery, no generic advice, no "consider adding tests". Name the test that is missing and what it would assert.
- Do not suggest weakening a safety property to gain convenience unless you state the exact new risk.
- Never ask for, print or invent credentials, account numbers or balances. Do not run anything against a real ProjectX gateway.
- No em dashes in your output.

## Output

Write ONE markdown file named `reviews/review-<MODEL>.md` (for example `reviews/review-gpt-5-mini.md`, `reviews/review-deepseek-v4-flash.md`). Use exactly this structure:

```
# Review by <MODEL>, <date>

## Verdict
(5 lines max: ship to sim rehearsal or not, and the 3 things that decide it)

## Blueprint vs built
| Item | Status | Evidence (file:line) | Better or worse than blueprint |
|---|---|---|---|

## Findings
### F1 <title>  [severity: critical|high|medium|low]  [confidence: verified|likely|guess]
- Where:
- Failure sequence:
- Fix:

## Answers to the 8 questions
(numbered, short)

## Ideas, ranked
| # | Idea | Why | Cost (S/M/L) | Risk it adds |
|---|---|---|---|---|

## What I did not check
```

If you cannot write files, output the same markdown in one block and say so on the first line.
