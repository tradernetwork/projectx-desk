# Prettier desk: three throwaway design directions

**Surface: Monitor.** The user watches state change (P&L, room, position, bias, regime, paper book); density and glanceability beat decoration. No hero, no centered stack, no feature-tile grid.

These are standalone prototypes. They do not touch `projectx_desk/static/`, call nothing, and have no order path: Stage, Place, Halt and Flatten are inert or not drawn. All figures are invented placeholders shaped like the digest lines (`RISK_DAILY`, `RISK_DD`, `BIAS`, `EMA`, `IND`, `REGIME`, `SESSION`, `SESSION_CTX`, `MAP`, `MAP_RECORD`, `POS`, `ORD`, `CALLOUT_n`) and the paper-book `stats()` shape. A simulated tick moves price and open P&L every 2.2 s so you can judge the monitor behaviour (Tweaks, `sim`).

| File | Direction |
|---|---|
| `a-conservative.html` | Same layout and information as today (ribbon, KPI strip, left tools, chart, right state), re-skinned. |
| `b-strong-fit.html` | Re-composed: one sticky glance strip (P&L, `RISK_DAILY` room, `RISK_DD` buffer, position, bias/regime), then a hairline 12-col grid of the real panels. |
| `c-divergent.html` | Editorial: the desk state is one serif sentence carrying mono figures; panels are grouped by question (Where am I / What am I allowed / What is the market doing / What has it paid). |

Open any file directly in a browser. Each has a `Tweaks` tab (bottom right): theme (dark, graphite, light), density, accent (default, amber, coral, bone), sim ticks. Stored in `localStorage` under `pxdesign.<a|b|c>.*`. `Hide` removes it; `Shift+T` brings it back; `Escape` closes it.

## Token system

Shared names across all three (values differ per direction):

| Token | Role | A (dark) | B (dark) | C (dark) |
|---|---|---|---|---|
| `--surface` / `--surface-2` / `--surface-3` | page / panel / hover | `#070b14` `#0b111c` `#121a29` | `#09090b` `#0e0e11` `#16161a` | `#0f0e0c` `#151411` `#1d1b17` |
| `--ink` / `--ink-2` / `--muted` | text, secondary, labels | `#f2f5fa` `#b4c0d4` `#7d8aa1` | `#ededf0` `#b0b0b8` `#7b7b86` | `#ece8df` `#bdb8ab` `#8a8578` |
| `--border` / `--border-strong` | hairline / emphasis line | `#182033` `#2a3550` | `#1d1d22` `#30303a` | `#2a2822` `#403d34` |
| `--accent` | the one accent (focus, selected, meter fill) | `#5fb3d9` (the repo's cyan) | `#e0a63a` | `#d9825f` |
| `--pos` / `--neg` | P&L and long/short only | `#3fcf80` / `#f2566f` | `#3ccf84` / `#f0586a` | `#7fc77f` / `#ec6a5c` |
| `--warn` | caution (TRADING OFF, small sample) | `#d6a73a` | `#e0a63a` | `#d9a43a` |

Rules: accent is never used for P&L; green and red are never the only signal (always a sign, a word or a label); `NA` is rendered muted text `NA`, never `0` or `-`. Alternate accents (amber, coral, bone) are one-line overrides in the Tweaks block, with darker light-theme variants.

Type: A = Inter 400/500/600 + JetBrains Mono 400/500 (the desk's own families, so it is the conservative pick). B = Geist + Geist Mono. C = Instrument Serif (the sentence and section questions only) + Inter + JetBrains Mono. Mono is for figures only; all use `font-variant-numeric: tabular-nums`. Scale: 11 label (uppercase, tracked) / 12-13 body and rows / 15 emphasis / 22-30 figures / 34 size count (A) or 26-44 serif sentence (C).

Numbers discipline kept from the desk: `n=` beside every rate and sample-dependent figure; win rate and mean R are `NA` under n=30 (the paper book shows one book at n=34 with rates and one at n=22 without); `MAP_RECORD` shows "3 of 10 sessions graded, no rate"; expectancy and profit factor carry a small-sample note. C also demonstrates the unknown branches: `?pos=unknown` prints "Position state is UNKNOWN, not verified against the broker; do not read it as flat" and `?pos=flat`.

## SLOP DIAGNOSTIC

Score is 10 minus the tells that fired (10 = clean). Tells: tech gradient, generic tech hue, feature-tile grid, accent rail, unearned blur, monument stat, icon topper, center stack, default type, wrong surface.

| Direction | Score | Fired |
|---|---|---|
| A conservative | **8/10** | *Generic tech hue*: cyan accent (inherited from the repo, deliberate for the baseline). *Default type*: Inter is the default-sans of the genre. Clear: gradient, tiles (the KPI row is hairline cells, not cards), rail, blur, icons, center stack. The 34 px size count is the desk's real hero figure, kept, not counted as a monument stat. |
| B strong-fit | **9/10** | *Default type* (partial): Geist is the current Vercel house face. Clear: amber accent, hairline-gap grid instead of boxes, tables for positions/orders, sticky strip as the only glance surface, nothing centered, no blur, no icons. The 30 px P&L is the same size as the other strip figures, so no monument. |
| C divergent | **8/10** | *Default type* (partial): an italic Instrument Serif headline is a current trope. *Wrong surface* (risk): a prose sentence is slower to scan than cells, mitigated by the fixed `deck` line of the same figures right under it and by keeping the sentence to one fixed shape. Clear: gradient, tiles, rails, blur, icons, center stack (left aligned, 46ch measure), hue (coral/ochre on warm black). |

No direction uses gradients, glass, emoji, left accent rails, per-card icons or oversized rounded rectangles (corners are 3 to 4 px, and C has none).

## Honest limits

- **No browser check was possible or allowed** (the task rules forbid starting a browser). What was done: every inline script passes `node --check`, and each page's script was run under Node against a stub DOM for 12 simulated ticks plus the `?pos=` branches with no exception; no duplicate ids; no `<script src>`. Layout, breakpoints (1199, 1099, 720, 640), contrast (estimated from the hex values, all muted text about 4.9:1 or better) and focus visibility are **unverified by eye**.
- The only network references are the Google Fonts link tags. Offline, the system fallbacks apply (the console may log the blocked font request, nothing else).
- Needs a 2023+ browser (`color-mix`, `clamp`, grid).
- The chart is a synthetic candle SVG, not klinecharts; none of the real chart overlays (GEX, trend, respected levels) are drawn.
- Data values are invented and only roughly self-consistent (e.g. room 967.50 = limit minus the $32.50 committed stop risk).

## Which to ship first

**B.** It changes the thing that matters on a monitor (one sticky strip answers "am I ok" without scanning), keeps every real panel and field, and its hairline grid maps straight onto the existing `.panel`/`.kpi` markup, so porting is mostly CSS plus moving the KPI ribbon and risk gauges into one strip. A is the safe fallback if the layout change is too big. Borrow from C: the single-sentence state line (with the `UNKNOWN` branch) as the ribbon text, and the question-style grouping if the right column ever needs reordering.
