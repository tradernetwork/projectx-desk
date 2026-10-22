'use strict';
// Guided tour and hover tips. Read-only: the tour only draws an overlay, it never clicks, stages or places anything,
// and while it is open the overlay swallows every click so a stray one cannot reach PLACE, HALT or FLATTEN.
(() => {
  const $ = (id) => document.getElementById(id);
  const q = (sel) => document.querySelector(sel);

  // [selector, title, body]. A step whose target is missing or hidden (no callouts feed, nothing open) is skipped.
  const STEPS = [
    ['#ribbon .contract', 'Contract', 'Type a root to search, then pick the exact contract. Everything on the desk follows this choice.'],
    ['#tfs', 'Chart timeframe', 'Bar size for the big chart, 15s up to 1D. The two small charts on the right stay on 15m and 1h.'],
    ['#ribbon .quote', 'Price', 'Last trade with the bid and ask. If this goes stale the desk refuses new orders rather than guess.'],
    ['#ribbon .feeds', 'Feed health', 'MKT is the market-data hub, USER is your account and orders hub. The number is seconds since the last message.'],
    ['#ribbon .states', 'Mode', 'ENGINE says the guard is ready. TRADING is the kill switch (off until you turn it on). SIM or LIVE shows which kind of account this is.'],
    ['#ribbon .actions', 'Stream, AI, HALT, FLATTEN', 'STREAM hides account and dollar figures for screen sharing. AI shows the text the AI surface reads. HALT refuses every new order. FLATTEN closes everything.'],
    ['#kpis', 'Session stats', 'Every stat carries its sample size and reads NA until there are trades. Nothing here is a forecast.'],
    ['#size', 'Size', 'The desk always computes size. 1R is a percent of your live drawdown room, the stop comes from ATR, and contracts round down to what the room and the caps allow.'],
    ['#ticket', 'Ticket', 'Staging runs every check against fresh state and makes a short-lived, single-use ticket. It counts down and expires on its own.'],
    ['#place', 'PLACE', 'The only order button. It stays disabled until a ticket exists, and you hold it to confirm. The hold is friction against fat-fingering, not security.'],
    ['.chartwrap .chartbar', 'Chart controls', 'Toggle the volume split, VWAP, the EMA 8/21/34 ribbon, Bollinger, RSI and MACD. Respect draws the level each timeframe has actually held, history only.'],
    ['#chart', 'Chart', 'Candles with the EMA ribbon and VWAP. Purple dashed lines are GEX levels: where options positioning sits, not targets. The pane under it is real buy and sell volume from the tape.'],
    ['#tools', 'Drawing tools', 'Trend lines, rays, horizontal and vertical lines, channels, fibs, zones, a measure tool and notes. Mag snaps to candles, Lock and Hide manage what you drew.'],
    ['#minis', 'Timeframes', 'Mini charts of the 15m and 1h with the same EMA ribbon, so the higher timeframes are always in view. The tag tells you whether the ribbon is stacked bull or bear.'],
    ['#ticks', 'Tick charts', 'Bars of N trade prints built from the live tape, with buy and sell volume. There is no history: they start when the desk starts listening.'],
    ['#risk', 'Risk budget', 'Daily loss room and drawdown buffer, with the one that binds marked. Open losses count against it, and gains never widen it.'],
    ['#feed', 'Analyst callouts', 'Third-party calls, shown as opinion. They are not signals and cannot place anything. Staging from one is off unless you turn it on.'],
    ['#blotter', 'Positions and orders', 'Open positions and working orders. Every open position must carry a stop; if one does not, the desk says so.'],
  ];

  // Hover tips for small things. Existing titles are never overwritten.
  const TIPS = [
    ['#kpi-pnl', 'Profit and loss for the current trading day (it rolls at 17:00 CT).'],
    ['#kpi-wr', 'Win rate against the break-even rate your payoff needs. n is the number of closed trades.'],
    ['#kpi-exp', 'Average dollars per trade. NA until there are trades.'],
    ['#kpi-pf', 'Gross wins divided by gross losses.'],
    ['#kpi-trades', 'Closed trades today, and how many losses in a row.'],
    ['#kpi-orders', 'Orders sent today against the daily cap.'],
    ['#kpi-open', 'Open contracts against the cap.'],
    ['#tfs', 'Chart timeframe'],
    ['#price', 'Last traded price'],
    ['#ai-toggle', 'Show the text the AI surface reads'],
    ['#place-btn', 'Hold to place. Disabled until a ticket is staged.'],
    ['#ind', 'Indicators drawn on the chart'],
    ['#tz', 'Time zone for the chart axis'],
    ['#g-daily', 'Daily loss limit left today, net of open risk and open losses.'],
    ['#g-dd', 'Room between the live balance and the firm drawdown floor.'],
    ['#minis .phead', 'The 15m and 1h at a glance'],
    ['#ticks .phead', 'Tick bars from the live tape, no history'],
    ['#feed .phead', 'Opinion from analysts, not a signal'],
    ['#risk .phead', 'What is left to lose today'],
  ];
  const applyTips = () => {
    for (const [sel, text] of TIPS) {
      const el = q(sel);
      if (el && text && !el.getAttribute('title')) el.setAttribute('title', text);
    }
  };

  const visible = (el) => {
    if (!el || el.hidden) return false;
    const r = el.getBoundingClientRect();
    return r.width > 4 && r.height > 4 && getComputedStyle(el).visibility !== 'hidden';
  };

  let steps = [], idx = 0, root = null, hi = null, pop = null, prevFocus = null;

  const place = () => {
    if (!root) return;
    const [sel] = steps[idx];
    const el = q(sel);
    if (!visible(el)) return;
    const pad = 6, r = el.getBoundingClientRect();
    Object.assign(hi.style, {left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + 2 * pad}px`, height: `${r.height + 2 * pad}px`});
    const vw = window.innerWidth, vh = window.innerHeight, pw = Math.min(340, vw - 24);
    pop.style.width = `${pw}px`;
    const ph = pop.offsetHeight;
    let left, top;
    if (vw < 700) { left = (vw - pw) / 2; top = r.top > vh / 2 ? 12 : vh - ph - 12; }
    else if (r.right + pw + 20 < vw) { left = r.right + 14; top = r.top; }        // room on the right
    else if (r.left - pw - 20 > 0) { left = r.left - pw - 14; top = r.top; }      // room on the left
    else if (r.bottom + ph + 20 < vh) { left = r.left; top = r.bottom + 14; }     // below
    else { left = r.left; top = Math.max(12, r.top - ph - 14); }                  // above
    left = Math.min(Math.max(12, left), vw - pw - 12);
    top = Math.min(Math.max(12, top), vh - ph - 12);
    Object.assign(pop.style, {left: `${left}px`, top: `${top}px`});
  };

  const show = (i) => {
    idx = i;
    const [sel, title, body] = steps[idx];
    const el = q(sel);
    el.scrollIntoView({block: 'nearest', inline: 'nearest'});
    pop.innerHTML = '';
    const h = document.createElement('h3'); h.id = 'tour-title'; h.textContent = title;
    const p = document.createElement('p'); p.textContent = body;
    const foot = document.createElement('div'); foot.className = 'tour-foot';
    const count = document.createElement('span'); count.className = 'tour-count'; count.textContent = `${idx + 1} of ${steps.length}`;
    const mk = (label, fn, cls) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = label; if (cls) b.className = cls; b.onclick = fn; return b; };
    const back = mk('Back', () => show(idx - 1), 'ghost'); back.disabled = idx === 0;
    const last = idx === steps.length - 1;
    const next = mk(last ? 'Done' : 'Next', () => (last ? end() : show(idx + 1)), 'primary');
    const skip = mk('Skip', end, 'ghost');
    foot.append(count, skip, back, next);
    pop.append(h, p, foot);
    place();
    next.focus({preventScroll: true});
  };

  function end() {
    if (!root) return;
    window.removeEventListener('resize', place);
    document.removeEventListener('scroll', place, true);
    document.removeEventListener('keydown', onKey, true);
    root.remove(); root = hi = pop = null;
    try { localStorage.setItem('px.tour', '1'); } catch (e) { /* private window: fine */ }
    if (prevFocus && prevFocus.focus) prevFocus.focus({preventScroll: true});
  }

  function onKey(e) {
    if (e.key === 'Escape') { e.preventDefault(); end(); }
    else if (e.key === 'ArrowRight' || e.key === 'Enter' && e.target.tagName !== 'BUTTON') { e.preventDefault(); idx < steps.length - 1 ? show(idx + 1) : end(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); if (idx > 0) show(idx - 1); }
    else if (e.key === 'Tab') {                       // keep focus inside the popover
      const f = [...pop.querySelectorAll('button:not([disabled])')];
      if (!f.length) return;
      const first = f[0], lastb = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); lastb.focus(); }
      else if (!e.shiftKey && document.activeElement === lastb) { e.preventDefault(); first.focus(); }
      else if (!pop.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    }
  }

  function start() {
    if (root) return;
    steps = STEPS.filter(([sel]) => visible(q(sel)));
    if (!steps.length) return;
    prevFocus = document.activeElement;
    root = document.createElement('div'); root.id = 'tour';
    hi = document.createElement('div'); hi.className = 'tour-hi';
    pop = document.createElement('div'); pop.className = 'tour-pop'; pop.setAttribute('role', 'dialog'); pop.setAttribute('aria-modal', 'true'); pop.setAttribute('aria-labelledby', 'tour-title');
    root.append(hi, pop);
    root.addEventListener('click', (e) => e.stopPropagation());     // the dim area is a shield, not a button
    document.body.append(root);
    window.addEventListener('resize', place);
    document.addEventListener('scroll', place, true);
    document.addEventListener('keydown', onKey, true);
    show(0);
  }

  applyTips();
  const btn = $('tour-btn');
  if (btn) btn.onclick = start;
  if (new URLSearchParams(location.search).get('tour') === '1') setTimeout(start, 600);
})();
