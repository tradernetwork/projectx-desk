'use strict';
// Leveraged ETF desk: the picker, two charts (underlying and fund) and the analytics strip inside the Robinhood panel.
// DISPLAY ONLY. This file reads five GET routes (/api/rh/letf, bars, quotes, movers and gex) and draws. It makes no POST, asks for no
// CSRF token, never previews or places anything, and has no way to: a click only hands a ticker to `hooks.prefill`, and rh.js decides
// what to do with it. Every dynamic string is drawn with textContent; nothing is stored in the browser (the URL hash carries the
// selection); no console. It never registers a klinecharts indicator or overlay: that registry is global and desk.js owns `VWAP`.
// The pure functions at the top are exported on window.pxLetf (and module.exports under node) for tests/test_letf_ui.py.
// Every figure carries its sample size where it has one, and an unknown number is "NA", never 0.
(function(root){
  const MIN_N = 20;                    // sessions needed before a beta or a volatility is shown
  const THIN = 5e6;                    // under $5M a day (20 sessions) the spread is wide: watch only
  const TFS = ['1m', '5m', '15m', '30m', '1h', '1D'];
  const TF_MS = {'1m': 60000, '5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000, '1D': 86400000};
  const STYLES = {up: '#FFC93C', down: '#9CB8E6', downWick: '#E4EEFF', ema: '#5FB3D9', grid: '#1A2238', text: '#6C7A92'};
  const SYM = /^[A-Z][A-Z0-9.\-]{0,9}$/;

  const isNum = (n) => typeof n === 'number' && isFinite(n);

  // ---- formatting -----------------------------------------------------------------------------------------------------------------
  function fmtPct(x, digits){
    if(!isNum(x)) return 'NA';
    const d = digits == null ? 2 : digits;
    return (x > 0 ? '+' : '') + (x * 100).toFixed(d) + '%';
  }
  function fmtBps(x){
    if(!isNum(x)) return 'NA';
    return (x > 0 ? '+' : '') + x.toFixed(Math.abs(x) < 10 ? 1 : 0) + ' bps';
  }
  function fmtDollarVol(x){
    if(!isNum(x) || x < 0) return 'NA';
    if(x >= 1e9) return '$' + (x / 1e9).toFixed(1) + 'B';
    if(x >= 1e6) return '$' + (x / 1e6).toFixed(x >= 1e8 ? 0 : 1) + 'M';
    if(x >= 1e3) return '$' + Math.round(x / 1e3) + 'K';
    return '$' + Math.round(x);
  }
  function levLabel(l){
    if(!isNum(l)) return '?';
    return String(+l.toFixed(2)) + 'x';
  }
  function fmtPrice(n){ return isNum(n) ? n.toFixed(n >= 100 ? 2 : 3) : 'NA'; }
  function chartTitle(u){
    if(!u) return '';
    if(u.chart_kind === 'self') return u.key;
    if(u.chart_kind === 'proxy') return u.key + ', charted via ' + u.chart + (u.proxy_quality === 'approximate' ? ' (approximate)' : '');
    return u.key + ': no clean underlying chart, fund only';
  }
  function fundTitle(sym, f){
    if(!f) return sym;
    const side = f.lev > 0 ? 'long' : 'short';
    let s = sym + ' ' + levLabel(f.lev) + ' ' + side;
    if(f.name) s += ', ' + f.name;
    else if(f.issuer) s += ', ' + f.issuer;
    return s + ', ' + fmtDollarVol(f.dv20) + '/day (20d)';
  }
  // How old the vendored list is: plain words and a level the UI colours (ok, warn at 14 days, stale at 45).
  function ageNote(asOf, ageDays){
    const lvl = !isNum(ageDays) ? 'warn' : ageDays >= 45 ? 'stale' : ageDays >= 14 ? 'warn' : 'ok';
    let s = 'Fund list as of ' + asOf + (isNum(ageDays) ? ' (' + ageDays + (ageDays === 1 ? ' day' : ' days') + ' old)' : '');
    if(lvl === 'stale') s += ': stale, run scripts/letf_sync.py';
    return {text: s, level: lvl};
  }

  // ---- the picker -----------------------------------------------------------------------------------------------------------------
  // universe: the /api/rh/letf reply. opts: {q, groups:[slug], side:'both'|'long'|'short', lev:'all'|'2'|'3'|'other', hideThin, sort:'dv'|'az'|'lev'}
  // -> rows [{key, group, u, funds:[{sym, lev, dv20, name, side}]}], funds sorted by dollar volume (unknown last). An unknown dollar
  // volume is never hidden by "hide thin": unknown is not zero.
  function levMatch(lev, want){
    const a = Math.abs(lev);
    return want === 'all' || (want === '2' && a === 2) || (want === '3' && a === 3) || (want === 'other' && a !== 2 && a !== 3);
  }
  function dvKey(f){ return isNum(f.dv20) ? f.dv20 : -1; }
  function filterRows(universe, opts){
    const o = opts || {};
    const q = String(o.q || '').trim().toLowerCase();
    const groups = o.groups && o.groups.length ? o.groups : null;
    const side = o.side || 'both', lev = o.lev || 'all', sort = o.sort || 'dv';
    const rows = [];
    for(const u of (universe && universe.underlyings) || []){
      if(groups && groups.indexOf(u.group) < 0) continue;
      const labelHit = q === '' || u.key.toLowerCase().indexOf(q) >= 0;
      const funds = [];
      for(const s of ['long', 'short']){
        if(side !== 'both' && side !== s) continue;
        for(const sym of u[s]){
          const f = universe.funds[sym];
          if(!f || !levMatch(f.lev, lev)) continue;
          if(o.hideThin && isNum(f.dv20) && f.dv20 < THIN) continue;
          const hit = labelHit || sym.toLowerCase().indexOf(q) >= 0 || (f.name && f.name.toLowerCase().indexOf(q) >= 0);
          if(hit) funds.push({sym, lev: f.lev, dv20: f.dv20, name: f.name, side: s});
        }
      }
      if(funds.length) rows.push({key: u.key, group: u.group, u, funds: funds.sort((a, b) => dvKey(b) - dvKey(a) || (a.sym < b.sym ? -1 : 1))});
    }
    const top = (r) => Math.max.apply(null, r.funds.map(dvKey));
    const maxLev = (r) => Math.max.apply(null, r.funds.map((f) => Math.abs(f.lev)));
    if(sort === 'az') rows.sort((a, b) => a.key.localeCompare(b.key));
    else if(sort === 'lev') rows.sort((a, b) => maxLev(b) - maxLev(a) || top(b) - top(a));
    else rows.sort((a, b) => top(b) - top(a) || a.key.localeCompare(b.key));
    return rows;
  }
  function grouped(rows, groups){
    const out = [];
    for(const g of groups || []){
      const rs = rows.filter((r) => r.group === g[0]);
      if(rs.length) out.push({slug: g[0], label: g[1], rows: rs});
    }
    return out;
  }
  // The fund a row shows for a side: the underlying's own pick if it survived the filters, else the best visible fund on that side.
  function rowPick(row, side){
    const vis = row.funds.filter((f) => f.side === side);
    if(!vis.length) return null;
    const p = row.u[side + '_pick'];
    return vis.some((f) => f.sym === p) ? p : vis[0].sym;
  }
  // Display order of the rows as the rail draws them (groups in order, rows in sorted order): the keyboard walks this.
  function displayRows(rows, groups){
    const out = [];
    for(const g of grouped(rows, groups)) for(const r of g.rows) out.push(r);
    return out;
  }
  // Up/Down: the adjacent row, on the same side when it has one, else the other. dir is +1 or -1. Wraps nowhere: the ends stay put.
  function neighbor(rows, sym, dir, prefer){
    if(!rows.length) return null;
    let i = rows.findIndex((r) => r.funds.some((f) => f.sym === sym));
    const cur = i >= 0 ? rows[i].funds.find((f) => f.sym === sym) : null;
    const side = prefer === 'long' || prefer === 'short' ? prefer : (cur ? cur.side : 'long');
    if(i < 0) i = dir > 0 ? -1 : rows.length;
    const j = Math.max(0, Math.min(rows.length - 1, i + (dir > 0 ? 1 : -1)));
    const r = rows[j];
    return rowPick(r, side) || rowPick(r, side === 'long' ? 'short' : 'long');
  }
  // L / S: the long or short pick of the underlying this fund belongs to, or null when that side has none.
  function flipSide(universe, sym, want){
    const f = universe && universe.funds && universe.funds[sym];
    if(!f) return null;
    const u = universe.underlyings.find((x) => x.key === f.u);
    return u ? (u[want + '_pick'] || null) : null;
  }
  // The side a fund is on, and the fund a row shows when the trader's side is sticky: on AMZN's bear fund, a click on META lands on META's
  // bear fund. A row with no fund on that side falls back to the other one and says so (`fellBack`); the side preference itself stays.
  function sideOf(universe, sym){
    const f = universe && universe.funds && universe.funds[sym];
    return f && isNum(f.lev) ? (f.lev > 0 ? 'long' : 'short') : null;
  }
  function pickForRow(row, side){
    if(!row) return null;
    const want = side === 'short' ? 'short' : 'long', other = want === 'long' ? 'short' : 'long';
    const a = rowPick(row, want);
    if(a) return {sym: a, side: want, fellBack: false};
    const b = rowPick(row, other);
    return b ? {sym: b, side: other, fellBack: true} : null;
  }
  function underlyingOf(universe, sym){
    const f = universe && universe.funds && universe.funds[sym];
    return f ? universe.underlyings.find((x) => x.key === f.u) || null : null;
  }

  // ---- the URL hash ('#letf=TQQQ&tf=5m'): the only memory this view has -----------------------------------------------------------
  function parseHash(hash, universe){
    const out = {sym: null, tf: null, on: false};
    const m = /^#?(.*)$/.exec(String(hash || ''));
    const parts = (m ? m[1] : '').split('&');
    for(const p of parts){
      const kv = p.split('=');
      if(kv[0] === 'letf'){
        out.on = true;
        if(SYM.test(kv[1] || '') && universe && universe.funds && universe.funds[kv[1]]) out.sym = kv[1];
      }else if(kv[0] === 'tf' && TFS.indexOf(kv[1]) >= 0) out.tf = kv[1];
    }
    return out;
  }
  function hashString(sym, tf){
    return '#letf=' + sym + (TFS.indexOf(tf) >= 0 ? '&tf=' + tf : '');
  }

  // ---- analytics (display only; n beside every figure, NA never 0) -----------------------------------------------------------------
  const ET = typeof Intl !== 'undefined' ? new Intl.DateTimeFormat('en-CA', {timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit'}) : null;
  function etDate(ms){ return ET && isNum(ms) ? ET.format(new Date(ms)) : null; }
  function utcDate(ms){ return isNum(ms) ? new Date(ms).toISOString().slice(0, 10) : null; }

  // Yesterday's close for "today's move". The quote's own previous close wins. Otherwise the last COMPLETED daily bar, except when
  // that bar is the quote's own session (a weekend or after the close: the day has already printed), then the bar before it.
  // Daily bars are dated by their UTC start (00:00Z), which is the session date.
  function sessionPrevClose(daily, quote){
    if(quote && isNum(quote.prev_close) && quote.prev_close > 0) return quote.prev_close;
    if(!daily || !daily.length) return null;
    const qd = quote ? etDate(quote.updated_ms) : null;
    let i = daily.length - 1;
    if(qd && utcDate(daily[i][0]) >= qd) i -= 1;
    return i >= 0 && isNum(daily[i][4]) && daily[i][4] > 0 ? daily[i][4] : null;
  }
  // today's move of the underlying and the fund, the move L x the underlying implies, and the gap in bps. A fund move over 50% that
  // L x the underlying does not explain is read as a probable split or corporate action and shown as NA, not as a fake gap.
  function todayMove(uLast, uPrev, eLast, ePrev, L){
    const ok = (a, b) => isNum(a) && isNum(b) && b > 0;
    const u = ok(uLast, uPrev) ? uLast / uPrev - 1 : null;
    let e = ok(eLast, ePrev) ? eLast / ePrev - 1 : null;
    const expected = u != null && isNum(L) ? L * u : null;
    let suspect = false;
    if(e != null && expected != null && Math.abs(e) > 0.5 && Math.abs(e - expected) > 0.2){ suspect = true; e = null; }
    const gap_bps = e != null && expected != null ? (e - expected) * 1e4 : null;
    return {u, e, expected, gap_bps, suspect};
  }
  // Pair two daily series by date. A return is used only between two dates that are consecutive in BOTH series (no bar of either
  // sits between them): a gap-fill bar dropped from one series then cannot turn a one-day return into a multi-day one.
  function alignDaily(a, b){
    const mk = (bars) => { const m = {}; for(const x of bars || []) if(isNum(x[4]) && x[4] > 0) m[utcDate(x[0])] = x[4]; return m; };
    const ma = mk(a), mb = mk(b);
    const all = Array.from(new Set(Object.keys(ma).concat(Object.keys(mb)))).sort();
    const rets = [];
    let prev = null;
    for(const d of all){
      if(ma[d] != null && mb[d] != null){
        if(prev !== null) rets.push({d, u: ma[d] / ma[prev] - 1, e: mb[d] / mb[prev] - 1});
        prev = d;
      }else prev = null;                      // a date only one series has breaks the chain
    }
    return rets;
  }
  // OLS beta of fund returns on underlying returns over the last n paired sessions, with R^2. NA under MIN_N.
  function realizedLeverage(rets, n){
    const k = n || MIN_N;
    if(!rets || rets.length < k) return {beta: null, r2: null, n: rets ? rets.length : 0, reason: 'sample'};
    const r = rets.slice(-k);
    const mu = r.reduce((s, x) => s + x.u, 0) / k, me = r.reduce((s, x) => s + x.e, 0) / k;
    let sxx = 0, sxy = 0, syy = 0;
    for(const x of r){ sxx += (x.u - mu) * (x.u - mu); sxy += (x.u - mu) * (x.e - me); syy += (x.e - me) * (x.e - me); }
    if(!(sxx > 0)) return {beta: null, r2: null, n: k, reason: 'flat'};
    return {beta: sxy / sxx, r2: syy > 0 ? (sxy * sxy) / (sxx * syy) : null, n: k, reason: null};
  }
  // Volatility drag of a daily-reset fund: -L(L-1)/2 x sigma^2 per day (L signed, so a -3x fund drags more than a 3x one),
  // x252 a year. sigma is the sample standard deviation of the underlying's daily log returns over the last n sessions.
  // No fees, financing or tracking error: the screen says so.
  function volDecay(L, uReturns, n){
    const k = n || MIN_N;
    if(!isNum(L) || !uReturns || uReturns.length < k) return {sigma_ann: null, decay_day: null, decay_ann: null, n: uReturns ? uReturns.length : 0, reason: 'sample'};
    const r = uReturns.slice(-k).map((x) => Math.log(1 + x));
    const m = r.reduce((s, x) => s + x, 0) / k;
    const v = r.reduce((s, x) => s + (x - m) * (x - m), 0) / (k - 1);
    const decay = -(L * (L - 1) / 2) * v;
    return {sigma_ann: Math.sqrt(v) * Math.sqrt(252), decay_day: decay, decay_ann: decay * 252, n: k, reason: null};
  }
  function spreadBps(bid, ask){
    if(!isNum(bid) || !isNum(ask) || bid <= 0 || ask < bid) return null;
    const mid = (bid + ask) / 2;
    return mid > 0 ? (ask - bid) / mid * 1e4 : null;
  }
  function quoteAge(updatedMs, nowMs){
    return isNum(updatedMs) && isNum(nowMs) ? Math.max(0, Math.round((nowMs - updatedMs) / 1000)) : null;
  }

  // ---- the forming bar ------------------------------------------------------------------------------------------------------------
  // Only completed bars come back from the broker, so the bar in progress is built here from the 2-second quote samples. Open is the
  // previous close (or the first sample), volume is unknown (null). The real bar replaces it as soon as it is returned.
  function updateForming(forming, price, nowMs, barMs, prevClose){
    if(!isNum(price) || price <= 0 || !isNum(nowMs) || !(barMs > 0)) return forming || null;
    const t = Math.floor(nowMs / barMs) * barMs;
    if(!forming || forming.t !== t){
      const o = isNum(prevClose) && prevClose > 0 ? prevClose : price;
      return {t, o, h: Math.max(o, price), l: Math.min(o, price), c: price};
    }
    return {t, o: forming.o, h: Math.max(forming.h, price), l: Math.min(forming.l, price), c: price};
  }
  function withForming(bars, forming){
    if(!forming || (bars.length && bars[bars.length - 1][0] >= forming.t)) return bars;
    return bars.concat([[forming.t, forming.o, forming.h, forming.l, forming.c, null]]);
  }
  // Milliseconds until the next bar fetch: the next bar boundary plus 4 s (the broker publishes a bar after it closes), at least 15 s.
  function nextPollDelay(tf, nowMs){
    const bar = TF_MS[tf] || 300000;
    if(tf === '1D') return 1800000;
    const boundary = (Math.floor(nowMs / bar) + 1) * bar;
    return Math.max(15000, boundary - nowMs + 4000);
  }

  // ---- the ladder: every fund on one underlying, side by side ----------------------------------------------------------------------
  // The funds of an underlying (both sides), the most liquid `max` of them, bulls first. Quotes are read four symbols a call, so the
  // page asks for at most 8 funds plus the underlying.
  function ladderFunds(universe, key, max){
    const u = universe && universe.underlyings && universe.underlyings.find((x) => x.key === key);
    if(!u) return [];
    const all = [];
    for(const side of ['long', 'short']) for(const sym of u[side] || []){
      const f = universe.funds[sym];
      if(f) all.push({sym, side, lev: f.lev, dv20: f.dv20, name: f.name});
    }
    const keep = all.slice().sort((a, b) => dvKey(b) - dvKey(a) || (a.sym < b.sym ? -1 : 1)).slice(0, max || 8).map((f) => f.sym);
    return all.filter((f) => keep.indexOf(f.sym) >= 0)
      .sort((a, b) => (a.side === b.side ? 0 : a.side === 'long' ? -1 : 1) || dvKey(b) - dvKey(a) || (a.sym < b.sym ? -1 : 1));
  }
  // Rows for the table. `quotes` is {SYM: {bid, ask, last, prev_close}}, `uLast`/`uPrev` the underlying's price now and its prior close.
  // A missing number is null (NA on screen), never 0; the gap uses the same rule as the strip (todayMove).
  function ladderRows(funds, quotes, uLast, uPrev){
    return (funds || []).map((f) => {
      const q = (quotes && quotes[f.sym]) || {};
      const mv = todayMove(uLast, uPrev, q.last, q.prev_close, f.lev);
      return {sym: f.sym, side: f.side, lev: f.lev, dv20: f.dv20, thin: isNum(f.dv20) && f.dv20 < THIN, last: isNum(q.last) ? q.last : null,
        spread_bps: spreadBps(q.bid, q.ask), move: mv.e, expected: mv.expected, gap_bps: mv.gap_bps, suspect: mv.suspect};
    });
  }

  // ---- what holding it really did --------------------------------------------------------------------------------------------------
  // Over the last `w` paired sessions (alignDaily rows {u, e}): the underlying's move, L x that move, the daily-reset ideal (compound
  // 1 + L x each day's move, a fund cannot go below zero), and what the fund actually returned. path = ideal - L x move is the compounding
  // arithmetic; fees_tracking = fund - ideal is everything else (expense ratio, financing, tracking, a late rebalance). Price return only:
  // a distribution is not added back. Fewer than `w` sessions is NA with the count, never a shorter window passed off as the long one.
  function holdWindows(rets, lev, wins){
    const list = wins || [21, 63, 126, 252], n = rets ? rets.length : 0;
    return list.map((w) => {
      if(!isNum(lev) || n < w) return {w, n, na: true};
      const r = rets.slice(-w);
      let U = 1, E = 1, I = 1;
      for(const x of r){ U *= 1 + x.u; E *= 1 + x.e; I *= Math.max(0, 1 + lev * x.u); }
      const u = U - 1, simple = lev * u, ideal = I - 1, fund = E - 1;
      return {w, n: w, na: false, u, simple, ideal, fund, path: ideal - simple, fees_tracking: fund - ideal};
    });
  }
  // The same window as three running lines (index 0 is the start, 0 return): the fund, L x the underlying, the daily-reset ideal.
  function holdSeries(rets, lev, w){
    if(!rets || rets.length < w || !isNum(lev)) return null;
    const r = rets.slice(-w), out = {fund: [0], simple: [0], ideal: [0]};
    let U = 1, E = 1, I = 1;
    for(const x of r){
      U *= 1 + x.u; E *= 1 + x.e; I *= Math.max(0, 1 + lev * x.u);
      out.fund.push(E - 1); out.simple.push(lev * (U - 1)); out.ideal.push(I - 1);
    }
    return out;
  }

  // ---- the rebalance window ---------------------------------------------------------------------------------------------------------
  // Daily-reset funds rebalance into the close, and the buying or selling that follows a big move lands in the last half hour. This marks
  // the bars that START between 15:30 and 16:00 ET, one run per session, so the chart can shade them. A fact about the clock, not a call.
  const ETM = typeof Intl !== 'undefined' ? new Intl.DateTimeFormat('en-US', {timeZone: 'America/New_York', hourCycle: 'h23', hour: '2-digit', minute: '2-digit'}) : null;
  function etMinutes(ms){
    if(!ETM || !isNum(ms)) return null;
    let h = null, m = null;
    for(const p of ETM.formatToParts(new Date(ms))){ if(p.type === 'hour') h = +p.value; else if(p.type === 'minute') m = +p.value; }
    return h == null || m == null ? null : h * 60 + m;
  }
  function rebalanceRuns(bars, tf){
    if(tf === '1D' || !bars) return [];
    const runs = [];
    let cur = null;
    for(const b of bars){
      const m = etMinutes(b[0]), d = etDate(b[0]);
      const inside = m != null && m >= 930 && m < 960;
      if(inside && cur && cur.d === d) cur.to = b[0];
      else if(inside) { cur = {d, from: b[0], to: b[0]}; runs.push(cur); }
      else cur = null;
    }
    return runs.map((r) => ({from: r.from, to: r.to}));
  }

  // ---- dealer levels on the underlying chart ------------------------------------------------------------------------------------------
  // /api/rh/gex answers in the underlying's own price space. Shown: the flip, the pin and the two nearest walls on each side of the last
  // price, all within `maxDist` (5%) of it. Positioning, not a target: the label says what the level is, never what to do.
  function gexLines(g, last, maxDist){
    if(!g || !g.ok || !isNum(last) || last <= 0) return [];
    const lim = isNum(maxDist) ? maxDist : 0.05, near = (p) => isNum(p) && Math.abs(p / last - 1) <= lim;
    const out = [];
    if(near(g.flip)) out.push({label: 'GAMMA FLIP', price: g.flip, kind: 'flip'});
    if(near(g.pin)) out.push({label: 'PIN', price: g.pin, kind: 'pin'});
    const ws = (g.walls || []).filter((w) => near(w.strike) && w.strike !== g.pin);
    const name = (w) => (w.kind === 'PUT' ? 'PUT WALL' : w.kind === 'CALL' ? 'CALL WALL' : String(w.type || 'WALL'));
    for(const above of [true, false]){
      ws.filter((w) => (w.strike > last) === above).sort((a, b) => Math.abs(a.strike - last) - Math.abs(b.strike - last)).slice(0, 2)
        .forEach((w) => out.push({label: name(w), price: w.strike, kind: w.kind === 'PUT' ? 'put' : w.kind === 'CALL' ? 'call' : 'wall'}));
    }
    return out.sort((a, b) => b.price - a.price);
  }

  // ---- the movers list ---------------------------------------------------------------------------------------------------------------
  // rows: /api/rh/movers `rows`. sort 'gain' | 'loss' | 'gap' (biggest absolute gap against L x the underlying). A fund with no number to
  // sort by is left out of that list rather than ranked as zero.
  function moversRows(rows, opts){
    const o = opts || {}, sort = o.sort || 'gain', side = o.side || 'both';
    let r = (rows || []).filter((x) => side === 'both' || (side === 'long') === (x.lev > 0));
    if(o.hideThin) r = r.filter((x) => !(isNum(x.dv20) && x.dv20 < THIN));
    const key = sort === 'gap' ? 'gap_bps' : 'move';
    r = r.filter((x) => isNum(x[key]));
    if(sort === 'gain') r.sort((a, b) => b.move - a.move || (a.s < b.s ? -1 : 1));
    else if(sort === 'loss') r.sort((a, b) => a.move - b.move || (a.s < b.s ? -1 : 1));
    else r.sort((a, b) => Math.abs(b.gap_bps) - Math.abs(a.gap_bps) || (a.s < b.s ? -1 : 1));
    return r.slice(0, o.limit || 40);
  }

  // ---- the snapshot ------------------------------------------------------------------------------------------------------------------
  function snapshotName(sym, tf, nowMs){
    const d = new Date(isNum(nowMs) ? nowMs : 0), p = (n) => String(n).padStart(2, '0');
    return 'letf-' + sym + '-' + tf + '-' + d.getUTCFullYear() + p(d.getUTCMonth() + 1) + p(d.getUTCDate()) + '-' + p(d.getUTCHours()) + p(d.getUTCMinutes()) + '.png';
  }
  // The text under the two charts: one "label  value" line per strip figure, then the footer. Only market data and fund statistics: no
  // account, balance or order ever reaches it.
  function snapshotLines(items, uniAsOf, nowMs){
    const lines = (items || []).map((kv) => kv[0] + ':  ' + kv[1]);
    const t = isNum(nowMs) ? new Date(nowMs).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' : '';
    lines.push('', 'Fund list as of ' + (uniAsOf || '?') + (t ? '. Snapshot ' + t : '') + '. Display only, not advice. ProjectX Desk.');
    return lines;
  }

  const api = {MIN_N, THIN, TFS, TF_MS, STYLES, fmtPct, fmtBps, fmtDollarVol, levLabel, fmtPrice, chartTitle, fundTitle, ageNote,
    filterRows, grouped, rowPick, displayRows, neighbor, flipSide, underlyingOf, sideOf, pickForRow, parseHash, hashString,
    ladderFunds, ladderRows, holdWindows, holdSeries, etMinutes, rebalanceRuns, gexLines, moversRows, snapshotName, snapshotLines,
    etDate, sessionPrevClose, todayMove, alignDaily, realizedLeverage, volDecay, spreadBps, quoteAge,
    updateForming, withForming, nextPollDelay};
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  if(typeof window === 'undefined' || typeof document === 'undefined') return;
  root.pxLetf = api;

  // ---- DOM ------------------------------------------------------------------------------------------------------------------------
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text != null) n.textContent = String(text);
    return n;
  };
  const btn = (cls, text, title) => {
    const b = el('button', cls, text);
    b.type = 'button';
    if(title) b.title = title;
    return b;
  };
  async function getJson(path, params){
    const qs = Object.keys(params || {}).map((k) => encodeURIComponent(k) + '=' + encodeURIComponent(params[k])).join('&');
    const r = await fetch(qs ? path + '?' + qs : path, {credentials: 'same-origin'});
    if(!r.ok) throw new Error('http');
    return r.json();
  }
  // Plain words for every code the routes answer with (rh/letf.py CODES and rh/mcp.py CODES). Never provider text.
  const CODE_TEXT = {
    not_connected: 'Robinhood is not connected. Sign in first (scripts/rh_capture.py), then reload.',
    busy: 'Too many chart requests at once. Retrying.', unauthorized: 'The Robinhood sign-in expired. Sign in again.',
    rate_limited: 'Robinhood is rate limiting the desk. Retrying.', unreachable: 'Could not reach Robinhood.', timeout: 'Robinhood timed out.',
    bad_reply: 'Robinhood sent a reply the desk could not read.', too_big: 'That range was too large for one reply.',
    letf_unavailable: 'The fund list file is missing or damaged. Run scripts/letf_sync.py.', not_in_universe: 'That symbol is not in the fund list.',
    gex_off: 'Dealer levels need a GEX key (PX_GEX_KEY).', gex_auth: 'The GEX key was refused.', gex_stale: 'The dealer levels are too old to show.',
    gex_unavailable: 'Dealer levels are not available for this one.', read_failed: 'A Robinhood read failed.', bad_tf: 'Unknown timeframe.', bad_symbol: 'Unknown symbol.', too_many: 'Too many symbols.',
  };
  const codeText = (c) => (Object.prototype.hasOwnProperty.call(CODE_TEXT, c) ? CODE_TEXT[c] : 'Could not load.');

  const PERIOD = {'1m': [1, 'minute'], '5m': [5, 'minute'], '15m': [15, 'minute'], '30m': [30, 'minute'], '1h': [1, 'hour'], '1D': [1, 'day']};
  const MONO = '"JetBrains Mono", ui-monospace, monospace';
  // The desk's chart look (desk.js STYLES), trimmed: gold up, steel down, a solid dim grid, EMA 21 in the desk blue.
  const CH = (() => {
    const axis = {axisLine: {color: '#2a3140'}, tickLine: {color: '#2a3140'}, tickText: {color: '#8b97a8', size: 10, family: MONO}};
    const chs = {line: {color: '#3a4456', style: 'solid'}, text: {backgroundColor: '#1a212e', borderColor: '#2a3140', color: '#e7edf5', family: MONO, size: 11}};
    const lg = {size: 11, family: MONO, color: '#8b97a8'};
    return {
      grid: {horizontal: {color: '#161c27', style: 'solid'}, vertical: {color: '#161c27', style: 'solid'}},
      candle: {bar: {upColor: STYLES.up, downColor: STYLES.down, noChangeColor: '#8D9BB4', upBorderColor: STYLES.up, downBorderColor: STYLES.down,
                     noChangeBorderColor: '#8D9BB4', upWickColor: STYLES.up, downWickColor: STYLES.downWick, noChangeWickColor: '#8D9BB4'},
               priceMark: {last: {upColor: '#ffb000', downColor: '#ffb000', noChangeColor: '#ffb000', line: {style: 'solid', size: 1},
                                  text: {color: '#07090e', family: MONO, weight: '700', borderRadius: 2}}},
               tooltip: {title: {...lg, color: '#93a0b4', show: false}, legend: {...lg, color: '#e7edf5', template: [{title: 'O ', value: '{open}'}, {title: 'H ', value: '{high}'}, {title: 'L ', value: '{low}'}, {title: 'C ', value: '{close}'}]}}},
      indicator: {bars: [{upColor: 'rgba(214,167,58,.55)', downColor: 'rgba(112,136,173,.55)', noChangeColor: 'rgba(108,122,146,.45)'}],
                  lines: [STYLES.ema, '#B49AE0', '#C8D8EE'].map((color) => ({color, size: 1})), tooltip: {title: lg, legend: lg}},
      xAxis: axis, yAxis: axis, separator: {color: '#2a3140'}, crosshair: {horizontal: chs, vertical: chs},
    };
  })();
  const toK = (b) => ({timestamp: b[0], open: b[1], high: b[2], low: b[3], close: b[4], volume: b[5] == null ? 0 : b[5]});

  // One klinecharts instance fed from memory. v10 loads through a data loader: setSymbol with a fresh ticker reloads everything from
  // `C.data`; `C.cb` (the subscribeBar callback) updates the last bar or appends one without moving the view.
  function makeChart(div){
    const C = {div, data: [], cb: null, gen: 0, chart: null};
    C.chart = root.klinecharts.init(div, {styles: CH, timezone: 'America/New_York'});
    C.chart.setDataLoader({
      getBars: ({callback}) => callback(C.data.map(toK), {forward: false, backward: false}),
      subscribeBar: ({callback}) => { C.cb = callback; },
      unsubscribeBar: () => { C.cb = null; },
    });
    C.chart.setOffsetRightDistance(30);
    C.chart.createIndicator({name: 'EMA', paneId: 'candle_pane', calcParams: [21], styles: {lines: [{color: STYLES.ema, size: 1.5, style: 'solid', smooth: false}]}}, true);
    C.chart.createIndicator({name: 'VOL'});
    return C;
  }
  function resetChart(C, ticker, tf, bars){
    C.data = bars;
    C.cb = null;
    C.gen += 1;
    const last = bars.length ? bars[bars.length - 1][4] : 100;
    try{ C.chart.setTimezone(tf === '1D' ? 'UTC' : 'America/New_York'); }catch(_){ /* older build: keep the zone */ }
    C.chart.setSymbol({ticker: ticker + '|' + tf + '|' + C.gen, pricePrecision: last < 20 ? 3 : 2, volumePrecision: 0});
    C.chart.setPeriod({span: PERIOD[tf][0], type: PERIOD[tf][1]});
  }
  // New data for the chart on screen: append or update the tail through the loader callback; anything else is a full reload.
  function patchChart(C, ticker, tf, bars){
    const prev = C.data;
    const tail = prev.length && bars.length && C.cb && bars[0][0] === prev[0][0] && bars.length >= prev.length && prev[prev.length - 1][0] >= prev[0][0];
    if(!tail || bars[prev.length - 1][0] !== prev[prev.length - 1][0]){ resetChart(C, ticker, tf, bars); return; }
    for(let i = prev.length - 1; i < bars.length; i++) C.cb(toK(bars[i]));
    C.data = bars;
  }

  const mkLayer = () => el('div', 'lx-ov');
  const GEX_COLOR = {flip: '#ffb000', pin: '#B49AE0', call: STYLES.up, put: STYLES.down, wall: '#8b97a8'};
  const loadImage = (url) => new Promise((res) => { const i = new Image(); i.onload = () => res(i); i.onerror = () => res(null); i.src = url; });

  function mount(host, hooks){
    const H = hooks || {};
    const L = {uni: null, rows: [], opts: {q: '', groups: [], side: 'both', lev: 'all', hideThin: false, sort: 'dv'}, sym: null, tf: '5m', side: 'long', link: true,
      visible: false, loaded: false, ch: {u: null, e: null}, tk: {u: '', e: ''}, series: {}, forming: {u: null, e: null}, quotes: {}, quotesAt: 0,
      daily: null, dailyKey: '', seq: 0, sessionOpen: false, barMs: TF_MS['5m'], expanded: {}, msg: '', msg2: '', snapNote: '', err: '',
      timers: {bars: 0, quotes: 0, ladder: 0, movers: 0, gex: 0}, layerPending: {u: false, e: false}, ladder: null, gex: null, gexNote: '',
      tab: 'funds', movers: null, moversErr: '', mv: {sort: 'gain', side: 'both', hideThin: true}, holdW: 63, syncUntil: 0, snapBusy: false};
    host.classList.add('lx');
    // rail: two boxes (funds, movers) under a tab row
    const rail = el('div', 'lx-rail'), tabs = el('div', 'lx-seg lx-tabs'), fundsBox = el('div', 'lx-rbox'), movBox = el('div', 'lx-rbox'),
      search = el('input', 'lx-search'), chips = el('div', 'lx-chips'), seg = el('div', 'lx-seg'),
      levs = el('div', 'lx-seg'), thin = el('label', 'lx-thin'), thinBox = el('input'), sortSel = el('select'), list = el('div', 'lx-list'), foot = el('div', 'lx-foot');
    search.type = 'search'; search.placeholder = 'Search underlying, ticker or name  ( / )'; search.autocomplete = 'off'; search.setAttribute('aria-label', 'Search funds');
    thinBox.type = 'checkbox';
    thin.append(thinBox, el('span', '', 'Hide thin (under $5M/day)'));
    [['dv', 'Sort: $ volume'], ['az', 'Sort: A to Z'], ['lev', 'Sort: leverage']].forEach((o) => { const op = el('option', '', o[1]); op.value = o[0]; sortSel.append(op); });
    sortSel.setAttribute('aria-label', 'Sort');
    fundsBox.append(search, chips, seg, levs, thin, sortSel, list);
    const mvHead = el('div', 'lx-seg'), mvSide = el('div', 'lx-seg'), mvThin = el('label', 'lx-thin'), mvThinBox = el('input'), mvList = el('div', 'lx-list'), mvNote = el('p', 'lx-mvnote');
    mvThinBox.type = 'checkbox'; mvThinBox.checked = true;
    mvThin.append(mvThinBox, el('span', '', 'Hide thin (under $5M/day)'));
    movBox.append(mvHead, mvSide, mvThin, mvNote, mvList);
    movBox.hidden = true;
    const tabBtns = {};
    [['funds', 'Funds'], ['movers', 'Movers']].forEach((t) => { const b = btn(t[0] === L.tab ? 'on' : '', t[1]); b.onclick = () => setTab(t[0]); tabBtns[t[0]] = b; tabs.append(b); });
    rail.append(tabs, fundsBox, movBox, foot);
    // main: ribbon, two charts, the ladder and the hold replay, the strip
    const main = el('div', 'lx-main'), ribbon = el('div', 'lx-ribbon'), status = el('div', 'lx-status'), charts = el('div', 'lx-charts'), strip = el('div', 'lx-strip');
    const mkPane = (cls) => {
      const p = el('div', 'lx-chart ' + cls), t = el('div', 'lx-ct'), wrap = el('div', 'lx-cw'), cv = el('div', 'lx-cv'), ov = mkLayer(), msg = el('div', 'lx-cmsg');
      msg.hidden = true; wrap.append(cv, ov); p.append(t, wrap, msg);
      return {p, t, wrap, cv, ov, msg};
    };
    const pu = mkPane('lx-chart-u'), pe = mkPane('lx-chart-e');
    charts.append(pu.p, pe.p);
    const tfBtns = {};
    TFS.forEach((tf, i) => { const b = btn('', tf, 'Timeframe (key ' + (i + 1) + ')'); b.onclick = () => setTf(tf); tfBtns[tf] = b; ribbon.append(b); });
    const sideSeg = el('div', 'lx-seg lx-sidesw'), bullB = btn('lx-bull', 'Bull', 'Show the long fund of each underlying you pick (key L)'),
      bearB = btn('lx-bear', 'Bear', 'Show the short fund of each underlying you pick (key S)'),
      linkB = btn('', 'Linked', 'The two charts scroll, zoom and share one crosshair. Click to unlink.'),
      snapB = btn('', 'PNG', 'Save both charts and the numbers as one picture');
    bullB.onclick = () => setSide('long'); bearB.onclick = () => setSide('short');
    linkB.onclick = () => { L.link = !L.link; linkB.classList.toggle('on', L.link); linkB.textContent = L.link ? 'Linked' : 'Unlinked'; };
    linkB.classList.add('on');
    snapB.onclick = () => snapshot();
    sideSeg.append(bullB, bearB);
    ribbon.append(sideSeg, linkB, snapB, status);
    const panels = el('div', 'lx-panels'), ladderBox = el('div', 'lx-card lx-ladder'), holdBox = el('div', 'lx-card lx-hold');
    panels.append(ladderBox, holdBox);
    main.append(ribbon, charts, panels, strip);
    // side
    const side = el('div', 'lx-side'), qbox = el('div', 'lx-quotes'), actions = el('div', 'lx-actions'), ticketSlot = el('div', 'lx-ticket');
    side.append(qbox, actions, ticketSlot);
    host.append(rail, main, side);

    // ---- the picker ----
    const groupNames = () => (L.uni ? L.uni.groups : []);
    function renderChips(){
      chips.replaceChildren();
      groupNames().forEach((g) => {
        const b = btn('lx-chip' + (L.opts.groups.indexOf(g[0]) >= 0 ? ' on' : ''), g[1]);
        b.onclick = () => { const i = L.opts.groups.indexOf(g[0]); if(i >= 0) L.opts.groups.splice(i, 1); else L.opts.groups.push(g[0]); refilter(); };
        chips.append(b);
      });
    }
    function renderSeg(){
      seg.replaceChildren(); levs.replaceChildren();
      [['both', 'Both'], ['long', 'Long'], ['short', 'Short']].forEach((o) => { const b = btn(L.opts.side === o[0] ? 'on' : '', o[1]); b.onclick = () => { L.opts.side = o[0]; refilter(); }; seg.append(b); });
      [['all', 'All x'], ['2', '2x'], ['3', '3x'], ['other', 'Other']].forEach((o) => { const b = btn(L.opts.lev === o[0] ? 'on' : '', o[1]); b.onclick = () => { L.opts.lev = o[0]; refilter(); }; levs.append(b); });
    }
    function fundBtn(f, small){
      const b = btn('lx-f ' + (f.side === 'long' ? 'lx-long' : 'lx-short') + (small ? ' lx-sm' : '') + (isNum(f.dv20) && f.dv20 < THIN ? ' thin' : '') + (f.sym === L.sym ? ' on' : ''),
        f.sym + ' ' + levLabel(f.lev) + '  ' + fmtDollarVol(f.dv20), (f.name || '') + (isNum(f.dv20) && f.dv20 < THIN ? ' (thin: wide spreads, watch only)' : ''));
      b.dataset.sym = f.sym;
      b.onclick = () => select(f.sym);
      return b;
    }
    function renderList(){
      list.replaceChildren();
      const groups = grouped(L.rows, groupNames());
      if(!groups.length) list.append(el('p', 'lx-none', 'No fund matches.'));
      groups.forEach((g) => {
        list.append(el('div', 'lx-grp', g.label + '  ' + g.rows.length));
        g.rows.forEach((r) => {
          const row = el('div', 'lx-row'), u = btn('lx-u', r.key, 'Show this underlying and keep ' + (L.side === 'short' ? 'Bear' : 'Bull') + ' selected');
          u.onclick = () => selectRow(r);
          if(r.u.chart_kind === 'proxy') u.title = 'Charted via ' + r.u.chart + (r.u.proxy_quality === 'approximate' ? ' (approximate)' : '') + '. Click keeps ' + (L.side === 'short' ? 'Bear' : 'Bull') + ' selected.';
          row.append(u);
          const lp = rowPick(r, 'long'), sp = rowPick(r, 'short');
          const cell = (sym, sd) => { const f = sym && r.funds.find((x) => x.sym === sym); return f ? fundBtn(f) : el('span', 'lx-nofund', sd === 'short' ? 'no short fund' : 'no long fund'); };
          row.append(cell(lp, 'long'), cell(sp, 'short'));
          const rest = r.funds.filter((f) => f.sym !== lp && f.sym !== sp);
          if(rest.length){
            const open = !!L.expanded[r.key], more = btn('lx-more', (open ? '- ' : '+') + rest.length, open ? 'Hide the other funds' : 'Show the other funds');
            more.onclick = () => { L.expanded[r.key] = !open; renderList(); };
            row.append(more);
            if(open){ const ex = el('div', 'lx-others'); rest.forEach((f) => ex.append(fundBtn(f, true))); row.append(ex); }
          }
          list.append(row);
        });
      });
      const sel = list.querySelector('.lx-f.on');
      if(sel && sel.scrollIntoView) sel.scrollIntoView({block: 'nearest'});
    }
    function renderFoot(){
      if(!L.uni) { foot.textContent = ''; return; }
      const n = ageNote(L.uni.as_of, L.uni.age_days);
      foot.textContent = n.text;
      foot.className = 'lx-foot ' + n.level;
    }
    function refilter(){
      L.rows = L.uni ? filterRows(L.uni, L.opts) : [];
      renderChips(); renderSeg(); renderList();
    }
    search.oninput = () => { L.opts.q = search.value; refilter(); };
    thinBox.onchange = () => { L.opts.hideThin = thinBox.checked; refilter(); };
    sortSel.onchange = () => { L.opts.sort = sortSel.value; refilter(); };

    // ---- selection and data ----
    const curFund = () => (L.uni && L.sym ? L.uni.funds[L.sym] : null);
    const curU = () => (L.uni && L.sym ? underlyingOf(L.uni, L.sym) : null);
    const chartSym = () => { const u = curU(); return u && u.chart ? u.chart : null; };
    const symsOf = () => { const a = [chartSym(), L.sym].filter(Boolean); return a.filter((x, i) => a.indexOf(x) === i); };

    // `o.keepSide`: a move between underlyings (the list, the arrow keys, the Bull/Bear switch) keeps the trader's Bull or Bear choice even
    // when this underlying has no fund on that side. Picking a fund by name (a click on it, the movers list, the ladder) sets the side.
    function select(sym, o){
      if(!L.uni || !L.uni.funds[sym]) return;
      const opt = o || {}, prevU = curU() ? curU().key : null;
      L.sym = sym;
      if(!opt.keepSide) L.side = sideOf(L.uni, sym) || L.side;
      L.snapNote = opt.note || '';
      L.series = {}; L.forming = {u: null, e: null}; L.quotes = {}; L.daily = null; L.dailyKey = ''; L.err = ''; L.msg2 = '';
      if(!curU() || curU().key !== prevU){ L.ladder = null; L.gex = null; L.gexNote = ''; }
      try{ history.replaceState(null, '', hashString(sym, L.tf)); }catch(_){ /* a sandboxed frame: the hash is a convenience */ }
      renderList(); renderHead(); renderStrip(); renderQuotes(); renderActions(); renderSide(); renderLadder(); renderHold(); clearLayers();
      loadBars(); loadDaily(); pollQuotes(true); loadLadder(); loadGex();
    }
    function selectRow(row){
      const p = pickForRow(row, L.side);
      if(!p) return;
      select(p.sym, {keepSide: true, note: p.fellBack ? row.key + ' has no ' + (L.side === 'short' ? 'bear' : 'bull') + ' fund, so the ' + (p.side === 'short' ? 'bear' : 'bull') + ' fund is shown. ' + (L.side === 'short' ? 'Bear' : 'Bull') + ' stays selected for the next one.' : ''});
    }
    function setSide(want){
      if(want !== 'long' && want !== 'short') return;
      L.side = want;
      if(!L.sym){ renderSide(); return; }
      const f = flipSide(L.uni, L.sym, want), u = curU();
      if(f) select(f, {keepSide: true});
      else { L.snapNote = (u ? u.key : 'This underlying') + ' has no ' + (want === 'short' ? 'bear' : 'bull') + ' fund.'; renderSide(); renderStrip(); }
    }
    function renderSide(){
      bullB.classList.toggle('on', L.side === 'long'); bearB.classList.toggle('on', L.side === 'short');
      const cur = sideOf(L.uni, L.sym);
      bullB.classList.toggle('lx-off', !!cur && cur !== 'long' && L.side === 'long'); bearB.classList.toggle('lx-off', !!cur && cur !== 'short' && L.side === 'short');
    }
    function setTf(tf){
      if(TFS.indexOf(tf) < 0 || tf === L.tf) return;
      L.tf = tf; L.barMs = TF_MS[tf]; L.forming = {u: null, e: null}; L.err = '';
      TFS.forEach((t) => tfBtns[t].classList.toggle('on', t === tf));
      if(L.sym){ try{ history.replaceState(null, '', hashString(L.sym, tf)); }catch(_){ /* see select */ } loadBars(); }
    }
    function renderHead(){
      const f = curFund(), u = curU();
      pu.t.textContent = (u ? chartTitle(u) : '') + (L.gexNote ? '   |   ' + L.gexNote : '');
      pe.t.textContent = f ? fundTitle(L.sym, f) : '';
      const none = !!u && !chartSym();
      pu.wrap.hidden = none; pu.msg.hidden = !none;
      if(none) pu.msg.textContent = 'No clean chart of ' + u.key + ' to show next to the fund. The fund chart and its own numbers are on the right.';
      charts.classList.toggle('lx-solo', none);
    }
    function barsFor(which){
      const s = which === 'u' ? chartSym() : L.sym;
      const row = s && L.series[s];
      return row ? withForming(row.bars, L.forming[which]) : null;
    }
    function ensureCharts(){
      if(!root.klinecharts) { L.err = 'The chart library did not load.'; return false; }
      if(!L.ch.u && pu.cv.clientWidth > 0 && !pu.wrap.hidden) L.ch.u = makeChart(pu.cv);
      if(!L.ch.e && pe.cv.clientWidth > 0) L.ch.e = makeChart(pe.cv);
      ['u', 'e'].forEach(hookChart);
      return true;
    }
    function paint(which, full){
      const C = L.ch[which], bars = barsFor(which);
      if(!C || !bars) return;
      const tk = (which === 'u' ? chartSym() : L.sym) + '|' + L.tf;
      if(full || L.tk[which] !== tk){ L.tk[which] = tk; resetChart(C, tk.split('|')[0], L.tf, bars); }
      else patchChart(C, tk.split('|')[0], L.tf, bars);
      scheduleLayer(which);
    }

    // ---- the two charts as one: scroll, zoom and the crosshair follow each other ----
    const other = (w) => (w === 'u' ? 'e' : 'u');
    const PANE = {paneId: 'candle_pane'};
    function hookChart(w){
      const C = L.ch[w];
      if(!C || C.hooked) return;
      C.hooked = true;
      try{
        C.chart.subscribeAction('onVisibleRangeChange', (r) => onRange(w, r));
        C.chart.subscribeAction('onCrosshairChange', (c) => onCross(w, c));
      }catch(_){ C.hooked = false; return; }
      C.div.addEventListener('mouseleave', () => { const D = L.ch[other(w)]; if(L.link && D){ try{ D.chart.executeAction('onCrosshairChange', null); }catch(_e){ /* mid-teardown */ } } });
    }
    function onRange(w, r){
      scheduleLayer(w);
      const C = L.ch[w], D = L.ch[other(w)];
      if(!L.link || !C || !D || !r || Date.now() < L.syncUntil || pu.wrap.hidden) return;
      L.syncUntil = Date.now() + 120;                         // what we do to the other chart must not echo back
      try{
        const bs = C.chart.getBarSpace(), ds = D.chart.getBarSpace();
        if(bs && ds && isNum(bs.bar) && Math.abs(ds.bar - bs.bar) > 0.01) D.chart.setBarSpace(bs.bar);
        const dl = C.chart.getDataList();
        if(!dl.length) return;
        const edge = dl[Math.max(0, Math.min(dl.length - 1, (isNum(r.realTo) ? r.realTo : dl.length) - 1))].timestamp;
        const xs = C.chart.convertToPixel({timestamp: edge}, PANE), xd = D.chart.convertToPixel({timestamp: edge}, PANE);
        if(xs && xd && isNum(xs.x) && isNum(xd.x) && Math.abs(xs.x - xd.x) > 0.5) D.chart.scrollByDistance(xs.x - xd.x);
      }catch(_){ /* an older build without one of these calls: the charts just stay independent */ }
    }
    function onCross(w, c){
      const D = L.ch[other(w)];
      if(!L.link || !D || !c || c.timestamp == null || pu.wrap.hidden) return;
      try{
        const dl = D.chart.getDataList();
        let bar = null;
        for(let i = dl.length - 1; i >= 0; i--){ if(dl[i].timestamp <= c.timestamp){ bar = dl[i]; break; } }
        if(!bar) return;
        const p = D.chart.convertToPixel({timestamp: bar.timestamp, value: bar.close}, PANE);
        if(p && isNum(p.x) && isNum(p.y)) D.chart.executeAction('onCrosshairChange', {x: p.x, y: p.y, paneId: 'candle_pane'});
      }catch(_){ /* the mirrored crosshair is a courtesy */ }
    }

    // ---- what is drawn over a chart: dealer levels (underlying) and the last-half-hour band (both) ----
    // One model, two renderers: the page draws it as positioned divs, the PNG draws it onto the canvas.
    function layerModel(w){
      const C = L.ch[w], out = {lines: [], bands: [], w: 0, h: 0};
      if(!C) return out;
      const bars = barsFor(w) || [];
      let box = null;
      try{ box = C.chart.getSize('candle_pane', 'main'); }catch(_){ box = null; }
      out.w = box && isNum(box.width) ? box.width : C.div.clientWidth;
      out.h = box && isNum(box.height) ? box.height : C.div.clientHeight;
      if(!bars.length) return out;
      try{
        if(w === 'u' && L.gex && L.gex.sym === chartSym() && L.gex.r && L.gex.r.ok){
          for(const g of gexLines(L.gex.r, bars[bars.length - 1][4])){
            const p = C.chart.convertToPixel({value: g.price}, PANE);
            if(p && isNum(p.y) && p.y >= 0 && p.y <= out.h) out.lines.push({label: g.label + ' ' + fmtPrice(g.price), y: p.y, color: GEX_COLOR[g.kind] || GEX_COLOR.wall});
          }
        }
        if(L.tf !== '1D'){
          const key = bars.length + ':' + bars[0][0] + ':' + bars[bars.length - 1][0] + ':' + L.tf;
          if(C.runsKey !== key){ C.runsKey = key; C.runs = rebalanceRuns(bars, L.tf); }
          const half = (C.chart.getBarSpace().bar || 0) / 2;
          (C.runs || []).slice(-12).forEach((r, i, all) => {
            const a = C.chart.convertToPixel({timestamp: r.from}, PANE), b = C.chart.convertToPixel({timestamp: r.to}, PANE);
            if(!a || !b || !isNum(a.x) || !isNum(b.x)) return;
            const x1 = Math.max(0, a.x - half), x2 = Math.min(out.w, b.x + half);
            if(x2 > x1) out.bands.push({x1, x2, last: i === all.length - 1});
          });
        }
      }catch(_){ /* the chart is between loads: draw nothing */ }
      return out;
    }
    function renderLayer(w){
      const lay = w === 'u' ? pu.ov : pe.ov, m = layerModel(w);
      lay.replaceChildren();
      m.bands.forEach((b) => {
        const d = el('div', 'lx-band');
        d.style.left = b.x1 + 'px'; d.style.width = (b.x2 - b.x1) + 'px'; d.style.height = m.h + 'px';
        if(b.last) d.append(el('span', '', 'last 30 min: funds rebalance'));
        lay.append(d);
      });
      m.lines.forEach((l) => {
        const d = el('div', 'lx-gl');
        d.style.top = l.y + 'px'; d.style.borderTopColor = l.color;
        const c = el('span', '', l.label); c.style.color = l.color;
        d.append(c); lay.append(d);
      });
    }
    function scheduleLayer(w){
      if(L.layerPending[w]) return;
      L.layerPending[w] = true;
      (root.requestAnimationFrame || ((f) => setTimeout(f, 16)))(() => { L.layerPending[w] = false; renderLayer(w); });
    }
    function clearLayers(){ pu.ov.replaceChildren(); pe.ov.replaceChildren(); }

    async function loadBars(){
      if(!L.visible || !L.sym) return;
      const my = ++L.seq, tf = L.tf, syms = symsOf();
      clearTimeout(L.timers.bars);
      status.textContent = 'Loading bars...';
      let r;
      try{ r = await getJson('/api/rh/bars', {symbols: syms.join(','), tf}); }catch(_){ r = {ok: false, code: 'unreachable'}; }
      if(my !== L.seq || !L.visible) return;
      if(!r.ok){
        L.err = codeText(r.code); status.textContent = L.err;
        if(r.code === 'busy') L.timers.bars = setTimeout(loadBars, 3000);                 // the budget refills at 2 calls a second
        else if(r.code === 'rate_limited' || r.code === 'timeout' || r.code === 'unreachable') L.timers.bars = setTimeout(loadBars, 15000);
        return;
      }
      L.err = ''; L.sessionOpen = !!r.session_open; L.barMs = r.bar_ms || L.barMs;
      Object.keys(r.series).forEach((s) => { L.series[s] = r.series[s]; });
      if(!ensureCharts()){ status.textContent = L.err; return; }
      L.forming = {u: null, e: null};                         // a real bar arrived: the forming bar starts again from the quote
      paint('u'); paint('e');
      const e = L.series[L.sym], n = e ? e.n : 0, dr = e ? e.dropped : 0;
      status.textContent = (n ? n + ' bars' : 'No bars for ' + L.sym + ' (closed or renamed?)') + (dr ? ', ' + dr + ' gap-fill dropped' : '') + (L.sessionOpen ? ', market open' : ', market closed')
        + (tf !== '1D' && L.sessionOpen ? '. The bar in progress is built from 2 s quote samples.' : '');
      if(L.sessionOpen) L.timers.bars = setTimeout(loadBars, nextPollDelay(tf, Date.now()));
      renderStrip(); renderHold();
      if(L.sessionOpen) pollQuotes(true);
    }
    async function loadDaily(){
      if(!L.visible || !L.sym) return;
      const key = symsOf().join(','), now = Date.now();
      if(L.daily && L.dailyKey === key && now - L.daily.at < 1800000) return;
      if(L.tf === '1D' && L.series[chartSym()] && L.series[L.sym]){ L.daily = {at: now, series: L.series}; L.dailyKey = key; renderStrip(); renderHold(); renderLadder(); return; }
      const my = L.seq;
      let r;
      try{ r = await getJson('/api/rh/bars', {symbols: key, tf: '1D'}); }catch(_){ return; }
      if(!r.ok || my !== L.seq || !L.visible) { if(!r.ok && r.code === 'busy') setTimeout(loadDaily, 5000); return; }
      L.daily = {at: now, series: r.series}; L.dailyKey = key;
      renderStrip(); renderHold(); renderLadder();
    }
    async function pollQuotes(){
      clearTimeout(L.timers.quotes);
      if(!L.visible || !L.sym || document.hidden){ if(L.visible && L.sessionOpen) L.timers.quotes = setTimeout(pollQuotes, 2000); return; }
      const my = L.seq, key = symsOf().join(',');
      let r;
      try{ r = await getJson('/api/rh/quotes', {symbols: key}); }catch(_){ r = {ok: false, code: 'unreachable'}; }
      if(my !== L.seq || !L.visible) return;
      if(r.ok){
        L.quotes = r.quotes; L.quotesAt = r.as_of_ms;
        if(L.sessionOpen && L.tf !== '1D'){
          ['u', 'e'].forEach((w) => {
            const s = w === 'u' ? chartSym() : L.sym, q = s && r.quotes[s], row = s && L.series[s];
            if(!q || !isNum(q.last) || !row) return;
            const lastBar = row.bars.length ? row.bars[row.bars.length - 1] : null;
            L.forming[w] = updateForming(L.forming[w], q.last, r.as_of_ms, L.barMs, lastBar ? lastBar[4] : null);
            paint(w);
          });
        }
        renderQuotes(); renderStrip();
      }
      if(L.sessionOpen) L.timers.quotes = setTimeout(pollQuotes, 2000);       // closed market: one read per selection, no loop
    }

    // ---- dealer levels for the underlying ----
    async function loadGex(){
      clearTimeout(L.timers.gex);
      const s = chartSym();
      if(!L.visible || !s) return;
      if(L.gex && L.gex.sym === s && Date.now() - L.gex.at < 290000) { renderLayer('u'); return; }
      const my = L.seq;
      let r;
      try{ r = await getJson('/api/rh/gex', {symbol: s}); }catch(_){ r = {ok: false, code: 'unreachable'}; }
      if(my !== L.seq || !L.visible || chartSym() !== s) return;
      L.gex = {sym: s, at: Date.now(), r};
      L.gexNote = r.ok ? 'dealer levels (GEX, ' + Math.round(r.age_s / 60) + ' min old): positioning, not targets' : (r.code === 'gex_off' ? '' : 'dealer levels: ' + codeText(r.code));
      pu.t.textContent = chartTitle(curU()) + (L.gexNote ? '   |   ' + L.gexNote : '');
      renderLayer('u');
      L.timers.gex = setTimeout(loadGex, 300000);
    }

    // ---- the ladder: the other funds on this underlying ----
    async function loadLadder(){
      clearTimeout(L.timers.ladder);
      const u = curU();
      if(!L.visible || !u || document.hidden){ if(L.visible && L.sessionOpen) L.timers.ladder = setTimeout(loadLadder, 8000); return; }
      const my = L.seq, funds = ladderFunds(L.uni, u.key, 8), us = chartSym();
      const syms = (us ? [us] : []).concat(funds.map((f) => f.sym));
      const quotes = {};
      for(let i = 0; i < syms.length; i += 4){
        let r;
        try{ r = await getJson('/api/rh/quotes', {symbols: syms.slice(i, i + 4).join(',')}); }catch(_){ r = {ok: false, code: 'unreachable'}; }
        if(my !== L.seq || !L.visible) return;
        if(!r.ok){
          L.ladderErr = codeText(r.code); renderLadder();
          L.timers.ladder = setTimeout(loadLadder, r.code === 'busy' ? 3000 : 15000);
          return;
        }
        Object.assign(quotes, r.quotes);
      }
      L.ladderErr = ''; L.ladder = {quotes, at: Date.now(), key: u.key, funds};
      renderLadder();
      if(L.sessionOpen) L.timers.ladder = setTimeout(loadLadder, 8000);
    }
    const dailyPair = () => {
      const us = chartSym(), dser = L.daily ? L.daily.series : {};
      return {du: us && dser[us] ? dser[us].bars : [], de: dser[L.sym] ? dser[L.sym].bars : [], us};
    };
    function table(cols, rows, onRow, selSym){
      const t = el('table', 'lx-tbl'), hd = el('tr');
      cols.forEach((c) => hd.append(el('th', c[1] || '', c[0])));
      t.append(hd);
      rows.forEach((r) => {
        const tr = el('tr', r.cls || '');
        r.cells.forEach((c, i) => tr.append(el('td', (cols[i][1] || '') + (c.cls ? ' ' + c.cls : ''), c.t)));
        if(onRow){ tr.onclick = () => onRow(r); tr.className += ' lx-click' + (selSym && r.sym === selSym ? ' on' : ''); }
        t.append(tr);
      });
      return t;
    }
    function renderLadder(){
      ladderBox.replaceChildren();
      const u = curU();
      ladderBox.append(el('h3', '', u ? 'Every fund on ' + u.key : 'Every fund on this underlying'));
      if(!u || !L.ladder || L.ladder.key !== u.key){
        ladderBox.append(el('p', 'lx-none', L.ladderErr || (u ? 'Loading quotes...' : 'Pick a fund on the left.')));
        return;
      }
      const us = chartSym(), uq = us && L.ladder.quotes[us], du = dailyPair().du;
      const uPrev = uq ? sessionPrevClose(du, uq) : null;
      const rows = ladderRows(L.ladder.funds, L.ladder.quotes, uq && isNum(uq.last) ? uq.last : null, uPrev);
      const cols = [['Fund'], ['Lev', 'n'], ['$ vol 20d', 'n'], ['Last', 'n'], ['Spread', 'n'], ['Today', 'n'], ['Gap vs L x', 'n']];
      const body = rows.map((r) => ({sym: r.sym, cls: r.side === 'short' ? 'lx-rshort' : 'lx-rlong', cells: [
        {t: r.sym + (r.thin ? ' (thin)' : ''), cls: r.thin ? 'lx-dim' : ''}, {t: levLabel(r.lev)}, {t: fmtDollarVol(r.dv20)}, {t: fmtPrice(r.last)},
        {t: r.spread_bps == null ? 'NA' : r.spread_bps.toFixed(1) + ' bps'}, {t: r.suspect ? 'NA' : fmtPct(r.move)}, {t: r.suspect ? 'NA' : fmtBps(r.gap_bps)}]}));
      ladderBox.append(table(cols, body, (r) => select(r.sym), L.sym));
      ladderBox.append(el('p', 'lx-note', 'Same underlying' + (us ? ' (' + (uq && isNum(uq.last) ? us + ' ' + fmtPrice(uq.last) + ', ' + fmtPct(uPrev ? uq.last / uPrev - 1 : null) : us) + ')' : ', no underlying quote') +
        '. Gap = fund move minus L x the underlying move: the part leverage does not explain. Top ' + rows.length + ' funds by dollar volume. Click a row to chart it.'));
    }

    // ---- what holding it really did ----
    function renderHold(){
      holdBox.replaceChildren();
      holdBox.append(el('h3', '', 'What holding it did'));
      const f = curFund(), dp = dailyPair();
      if(!f || !dp.us){ holdBox.append(el('p', 'lx-none', f ? 'Needs an underlying chart to compare against.' : 'Pick a fund on the left.')); return; }
      const rets = alignDaily(dp.du, dp.de), wins = holdWindows(rets, f.lev, [21, 63, 126, 252]);
      if(!rets.length){ holdBox.append(el('p', 'lx-none', L.daily ? 'No paired daily history yet.' : 'Loading daily history...')); return; }
      const names = {21: '1 mo', 63: '3 mo', 126: '6 mo', 252: '1 yr'};
      const cols = [['Held'], ['Underlying', 'n'], ['L x it', 'n'], ['Ideal', 'n'], ['Fund', 'n'], ['Path', 'n'], ['Fees', 'n']];
      const body = wins.map((x) => ({w: x.w, cls: x.w === L.holdW ? 'on' : '', cells: x.na
        ? [{t: names[x.w] + ' (' + x.w + ')'}, {t: 'NA (' + x.n + ' of ' + x.w + ' sessions)'}, {t: ''}, {t: ''}, {t: ''}, {t: ''}, {t: ''}]
        : [{t: names[x.w] + ' (' + x.w + ')'}, {t: fmtPct(x.u, 1)}, {t: fmtPct(x.simple, 1)}, {t: fmtPct(x.ideal, 1)}, {t: fmtPct(x.fund, 1)}, {t: fmtPct(x.path, 1)}, {t: fmtPct(x.fees_tracking, 1)}]}));
      holdBox.append(table(cols, body, (r) => { L.holdW = r.w; renderHold(); }, null));
      const cv = el('canvas', 'lx-hcv');
      holdBox.append(cv);
      const ser = holdSeries(rets, f.lev, L.holdW);
      holdBox.append(el('p', 'lx-note', 'Sessions ending on the last paired daily bar, price return only (a distribution is not added back). Ideal = compounding L x each day\'s move. Path = ideal minus L x the underlying move: arithmetic. Fees = fund minus ideal: expense ratio, financing, tracking. A negative figure is a cost of holding, not a forecast.'));
      drawHold(cv, ser);
    }
    function drawHold(cv, ser){
      const W = Math.max(240, Math.floor(cv.parentNode && cv.parentNode.clientWidth ? cv.parentNode.clientWidth - 16 : 360)), Hh = 150, dpr = root.devicePixelRatio || 1;
      cv.width = Math.round(W * dpr); cv.height = Math.round(Hh * dpr); cv.style.width = W + 'px'; cv.style.height = Hh + 'px';
      const g = cv.getContext('2d');
      if(!g) return;
      g.scale(dpr, dpr); g.clearRect(0, 0, W, Hh);
      if(!ser){ g.fillStyle = '#8b97a8'; g.font = '11px ' + MONO; g.fillText('Not enough paired sessions for this window.', 8, 20); return; }
      const all = ser.fund.concat(ser.simple, ser.ideal, [0]), lo = Math.min.apply(null, all), hi = Math.max.apply(null, all), pad = {l: 44, r: 8, t: 10, b: 18};
      const span = hi - lo || 1, X = (i) => pad.l + (W - pad.l - pad.r) * i / Math.max(1, ser.fund.length - 1), Y = (v) => pad.t + (Hh - pad.t - pad.b) * (1 - (v - lo) / span);
      g.strokeStyle = '#2a3140'; g.lineWidth = 1; g.beginPath(); g.moveTo(pad.l, Y(0)); g.lineTo(W - pad.r, Y(0)); g.stroke();
      g.fillStyle = '#8b97a8'; g.font = '10px ' + MONO;
      [lo, 0, hi].forEach((v) => g.fillText(fmtPct(v, 0), 2, Y(v) + 3));
      g.fillText(L.holdW + ' sessions', pad.l, Hh - 4);
      const line = (arr, color, dash) => { g.strokeStyle = color; g.lineWidth = 1.6; g.setLineDash(dash || []); g.beginPath(); arr.forEach((v, i) => { if(i) g.lineTo(X(i), Y(v)); else g.moveTo(X(i), Y(v)); }); g.stroke(); g.setLineDash([]); };
      line(ser.simple, '#8b97a8', [4, 3]); line(ser.ideal, '#5FB3D9', [2, 2]); line(ser.fund, STYLES.up);
      const lg = [['Fund', STYLES.up], ['Daily-reset ideal', '#5FB3D9'], ['L x underlying', '#8b97a8']];
      let x = W - pad.r;
      lg.slice().reverse().forEach((e) => { const w = g.measureText(e[0]).width + 16; x -= w; g.fillStyle = e[1]; g.fillRect(x, 6, 8, 3); g.fillStyle = '#8b97a8'; g.fillText(e[0], x + 11, 11); });
    }

    // ---- the movers tab ----
    function setTab(t){
      L.tab = t === 'movers' ? 'movers' : 'funds';
      Object.keys(tabBtns).forEach((k) => tabBtns[k].classList.toggle('on', k === L.tab));
      fundsBox.hidden = L.tab !== 'funds'; movBox.hidden = L.tab !== 'movers';
      if(L.tab === 'movers') { renderMovBar(); renderMovers(); loadMovers(); } else { clearTimeout(L.timers.movers); renderList(); }
    }
    function renderMovBar(){
      mvHead.replaceChildren(); mvSide.replaceChildren();
      [['gain', 'Gainers'], ['loss', 'Losers'], ['gap', 'Biggest gap']].forEach((o) => { const b = btn(L.mv.sort === o[0] ? 'on' : '', o[1]); b.onclick = () => { L.mv.sort = o[0]; renderMovBar(); renderMovers(); }; mvHead.append(b); });
      [['both', 'Both'], ['long', 'Bull'], ['short', 'Bear']].forEach((o) => { const b = btn(L.mv.side === o[0] ? 'on' : '', o[1]); b.onclick = () => { L.mv.side = o[0]; renderMovBar(); renderMovers(); }; mvSide.append(b); });
    }
    mvThinBox.onchange = () => { L.mv.hideThin = mvThinBox.checked; renderMovers(); };
    function renderMovers(){
      mvList.replaceChildren();
      const m = L.movers;
      if(!m){ mvNote.textContent = L.moversErr || 'Scanning the most traded funds...'; return; }
      const when = isNum(m.quotes_ms) ? new Date(m.quotes_ms).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : 'unknown time';
      mvNote.textContent = 'Top ' + m.scan_n + ' funds by 20-day dollar volume (list as of ' + m.list_as_of + '), ' + m.n_quoted + ' quoted, as of ' + when + (m.session_open ? '' : ' (market closed: the last session)') + '. Not all ' + (L.uni ? Object.keys(L.uni.funds).length : '') + ' funds: the Robinhood call budget.';
      const rows = moversRows(m.rows, {sort: L.mv.sort, side: L.mv.side, hideThin: L.mv.hideThin, limit: 40});
      if(!rows.length) mvList.append(el('p', 'lx-none', 'Nothing to rank with these filters.'));
      rows.forEach((r) => {
        const b = btn('lx-mv ' + (r.lev > 0 ? 'lx-long' : 'lx-short') + (r.s === L.sym ? ' on' : ''), '', r.u + ': ' + (r.dv20 == null ? 'volume NA' : fmtDollarVol(r.dv20) + ' a day'));
        b.append(el('span', 'a', r.s + ' ' + levLabel(r.lev)), el('span', 'b', fmtPct(r.move)), el('span', 'c', fmtBps(r.gap_bps)));
        b.onclick = () => select(r.s);
        mvList.append(b);
      });
    }
    async function loadMovers(){
      clearTimeout(L.timers.movers);
      if(!L.visible || L.tab !== 'movers') return;
      let r;
      try{ r = await getJson('/api/rh/movers'); }catch(_){ r = {ok: false, code: 'unreachable'}; }
      if(!L.visible || L.tab !== 'movers') return;
      if(!r.ok){
        L.moversErr = codeText(r.code);
        if(!L.movers) mvNote.textContent = L.moversErr;
        L.timers.movers = setTimeout(loadMovers, r.code === 'busy' ? 3000 : 20000);
        return;
      }
      L.movers = r; L.moversErr = '';
      renderMovers();
      L.timers.movers = setTimeout(loadMovers, r.session_open ? 30000 : 600000);
    }

    // ---- the quote box, the actions and the analytics strip ----
    function kv(k, v, cls){ const d = el('div', 'lx-kv' + (cls ? ' ' + cls : '')); d.append(el('span', 'k', k), el('span', 'v', v)); return d; }
    function renderQuotes(){
      qbox.replaceChildren(el('h3', '', 'Live quotes'));
      symsOf().forEach((s) => {
        const q = L.quotes[s], box = el('div', 'lx-q');
        box.append(el('div', 'lx-qs', s));
        if(!q || !isNum(q.last)){ box.append(el('div', 'lx-qn', 'NA')); qbox.append(box); return; }
        const sp = spreadBps(q.bid, q.ask), age = quoteAge(q.updated_ms, L.quotesAt);
        box.append(el('div', 'lx-qn', fmtPrice(q.last)),
          el('div', 'lx-qd', 'bid ' + fmtPrice(q.bid) + '  ask ' + fmtPrice(q.ask)),
          el('div', 'lx-qd', 'spread ' + (sp == null ? 'NA' : sp.toFixed(1) + ' bps') + (age == null ? '' : '  age ' + age + ' s')));
        qbox.append(box);
      });
    }
    function renderActions(){
      actions.replaceChildren();
      if(!L.sym) return;
      const fill = btn('lx-fill', 'Use ' + L.sym + ' in the ticket', 'Fills the ticker box only. Nothing is previewed or sent.');
      fill.onclick = () => { L.msg2 = H.prefill ? H.prefill(L.sym) : ''; renderActions(); };
      const ask = btn('', 'Use ask as limit', 'Copies the live ask into the limit box. You still preview and hold to place.');
      ask.onclick = () => { const q = L.quotes[L.sym]; L.msg2 = isNum(q && q.ask) && H.useAsk ? H.useAsk(q.ask) : 'No live ask to copy.'; renderActions(); };
      actions.append(fill, ask);
      if(L.msg2) actions.append(el('p', 'lx-msg', L.msg2));
    }
    // The figures of the strip as [label, value, class] triples: the page draws them and the PNG prints them.
    function stripItems(){
      const f = curFund(), us = chartSym();
      if(!f) return [];
      const qu = us && L.quotes[us], qe = L.quotes[L.sym], dp = dailyPair();
      const uPrev = qu ? sessionPrevClose(dp.du, qu) : null, ePrev = qe ? sessionPrevClose(dp.de, qe) : null;
      const mv = todayMove(qu ? qu.last : null, uPrev, qe ? qe.last : null, ePrev, f.lev);
      const rets = us ? alignDaily(dp.du, dp.de) : [];
      const rl = realizedLeverage(rets), vd = volDecay(f.lev, rets.map((x) => x.u));
      const sp = qe ? spreadBps(qe.bid, qe.ask) : null;
      return [
        ['Underlying today', us ? fmtPct(mv.u) : 'NA', ''],
        ['Fund today', mv.suspect ? 'NA (corporate action?)' : fmtPct(mv.e), ''],
        ['Expected ' + levLabel(f.lev), fmtPct(mv.expected), ''],
        ['Gap', fmtBps(mv.gap_bps), ''],
        ['Realized leverage', rl.beta == null ? 'NA (' + rl.n + ' of ' + MIN_N + ' sessions)' : rl.beta.toFixed(2) + 'x  (R2 ' + (rl.r2 == null ? 'NA' : rl.r2.toFixed(2)) + ', n=' + rl.n + ')', ''],
        ['Volatility decay', vd.decay_ann == null ? 'NA (' + vd.n + ' of ' + MIN_N + ' sessions)' : fmtPct(vd.decay_ann, 1) + ' a year at sigma ' + fmtPct(vd.sigma_ann, 0).replace('+', '') + '  (n=' + vd.n + ')', ''],
        ['20d $ volume', fmtDollarVol(f.dv20) + '  (as of ' + (L.uni ? L.uni.as_of : '?') + ')', isNum(f.dv20) && f.dv20 < THIN ? 'warn' : ''],
        ['Spread', sp == null ? 'NA' : sp.toFixed(1) + ' bps', '']];
    }
    function renderStrip(){
      strip.replaceChildren();
      const f = curFund(), u = curU();
      if(!f){ strip.append(el('p', 'lx-none', 'Pick a fund on the left.')); return; }
      if(L.err) strip.append(el('p', 'lx-bad', L.err));
      const items = el('div', 'lx-items'), us = chartSym();
      stripItems().forEach((t) => items.append(kv(t[0], t[1], t[2])));
      strip.append(items);
      const notes = el('div', 'lx-notes');
      if(L.snapNote) notes.append(el('p', 'lx-warn', L.snapNote));
      if(u && u.chart_kind === 'proxy') notes.append(el('p', 'lx-warn', 'Underlying charted via ' + u.chart + (u.proxy_quality === 'approximate' ? ' (approximate)' : '') + ': expected move, gap, beta and decay are approximate.' + (u.group === 'crypto' ? ' Regular-hours closes against a 24/7 asset.' : '')));
      if(u && !us) notes.append(el('p', 'lx-warn', 'No underlying chart for ' + u.key + ', so beta, decay and the expected move are NA.'));
      notes.append(el('p', '', 'Volatility decay = -L(L-1)/2 x sigma^2 per day, x252 a year, from the underlying\'s last ' + MIN_N + ' daily returns. No fees, financing or tracking error. A daily-reset fund drifts from L x the multi-day move; this is the part that is arithmetic, not a forecast.'));
      strip.append(notes);
    }

    // ---- the picture: both charts, the dealer levels and band, and the numbers, as one PNG ----
    async function snapshot(){
      if(L.snapBusy || !L.sym) return;
      L.snapBusy = true; snapB.disabled = true;
      try{
        const which = ['u', 'e'].filter((w) => L.ch[w] && !(w === 'u' && pu.wrap.hidden));
        const shots = [];
        for(const w of which){
          const C = L.ch[w], img = await loadImage(C.chart.getConvertPictureUrl(true, 'png', '#0b0f17'));
          if(img) shots.push({w, img, m: layerModel(w), title: (w === 'u' ? pu.t : pe.t).textContent, css: C.div.clientWidth || img.width});
        }
        if(!shots.length) { status.textContent = 'Nothing to save yet.'; return; }
        const lines = snapshotLines(stripItems().map((t) => [t[0], t[1]]), L.uni ? L.uni.as_of : null, Date.now());
        const pad = 16, lh = 17, gap = 10, titleH = 22, W = shots.reduce((s, x) => s + x.img.width, 0) + pad * (shots.length + 1) + gap * (shots.length - 1) * 0;
        const imgH = Math.max.apply(null, shots.map((x) => x.img.height));
        const cv = document.createElement('canvas');
        cv.width = W; cv.height = pad + 26 + titleH + imgH + pad + lines.length * lh + pad;
        const g = cv.getContext('2d');
        g.fillStyle = '#0b0f17'; g.fillRect(0, 0, cv.width, cv.height);
        g.fillStyle = '#e7edf5'; g.font = '600 15px ' + MONO; g.fillText(L.sym + '  ' + L.tf + '  ' + (curU() ? curU().key : ''), pad, pad + 14);
        let x = pad;
        shots.forEach((s) => {
          g.fillStyle = '#8b97a8'; g.font = '11px ' + MONO; g.fillText(s.title.slice(0, Math.floor(s.img.width / 7)), x, pad + 26 + 14);
          const y0 = pad + 26 + titleH;
          g.drawImage(s.img, x, y0);
          const k = s.img.width / s.css;
          s.m.bands.forEach((b) => { g.fillStyle = 'rgba(180,154,224,0.12)'; g.fillRect(x + b.x1 * k, y0, (b.x2 - b.x1) * k, s.m.h * k); });
          s.m.lines.forEach((l) => {
            g.strokeStyle = l.color; g.lineWidth = 1; g.setLineDash([5, 4]); g.beginPath(); g.moveTo(x, y0 + l.y * k); g.lineTo(x + s.m.w * k, y0 + l.y * k); g.stroke(); g.setLineDash([]);
            g.fillStyle = l.color; g.font = '11px ' + MONO; g.fillText(l.label, x + 6, y0 + l.y * k - 3);
          });
          x += s.img.width + pad;
        });
        g.font = '12px ' + MONO;
        lines.forEach((t, i) => { g.fillStyle = i >= lines.length - 1 ? '#6C7A92' : '#c9d3e0'; g.fillText(t, pad, pad + 26 + titleH + imgH + pad + (i + 1) * lh - 4); });
        const blob = await new Promise((res) => cv.toBlob(res, 'image/png'));
        if(!blob) { status.textContent = 'Could not make the picture.'; return; }
        const a = document.createElement('a'), url = URL.createObjectURL(blob);
        a.href = url; a.download = snapshotName(L.sym, L.tf, Date.now());
        document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        status.textContent = 'Saved ' + a.download;
      }catch(_){ status.textContent = 'Could not make the picture.'; }
      finally{ L.snapBusy = false; snapB.disabled = false; }
    }

    // ---- keys: only while this view is on screen, and every key handled is stopped before desk.js sees it ----
    function typing(t){ return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); }
    function onKey(e){
      if(!L.visible || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target, inSearch = t === search;
      const stop = () => { e.preventDefault(); e.stopPropagation(); };
      if(typing(t) && !inSearch) return;
      if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        if(L.tab !== 'funds') return;
        const next = neighbor(displayRows(L.rows, groupNames()), L.sym, e.key === 'ArrowDown' ? 1 : -1, L.side);
        if(next){ const sd = sideOf(L.uni, next); select(next, {keepSide: true, note: sd !== L.side ? 'This underlying has no ' + (L.side === 'short' ? 'bear' : 'bull') + ' fund, so the ' + (sd === 'short' ? 'bear' : 'bull') + ' fund is shown. ' + (L.side === 'short' ? 'Bear' : 'Bull') + ' stays selected.' : ''}); }
        stop(); return;
      }
      if(inSearch){ if(e.key === 'Escape'){ search.blur(); stop(); } else if(e.key === 'Enter' && L.sym){ if(H.prefill) L.msg2 = H.prefill(L.sym); renderActions(); stop(); } return; }
      if(e.key === '/'){ if(L.tab !== 'funds') setTab('funds'); search.focus(); stop(); return; }
      if(e.key === 'Enter' && L.sym){ L.msg2 = H.prefill ? H.prefill(L.sym) : ''; renderActions(); stop(); return; }
      const k = e.key.toLowerCase();
      if(k === 'l' || k === 's'){ setSide(k === 'l' ? 'long' : 'short'); stop(); return; }
      if(/^[1-6]$/.test(k)){ setTf(TFS[+k - 1]); stop(); }
    }
    document.addEventListener('keydown', onKey, true);
    const resize = () => { ['u', 'e'].forEach((w) => { try{ if(L.ch[w]) L.ch[w].resize(); }catch(_){ /* mid-teardown */ } scheduleLayer(w); }); };
    if(typeof ResizeObserver !== 'undefined') new ResizeObserver(resize).observe(charts);

    async function init(){
      let r;
      try{ r = await getJson('/api/rh/letf'); }catch(_){ r = {ok: false, code: 'unreachable'}; }
      if(!r.ok){ list.replaceChildren(el('p', 'lx-bad', r.enabled === false ? 'Robinhood is off on this desk.' : codeText(r.code))); return; }
      L.uni = r; L.loaded = true;
      renderFoot(); refilter();
      const h = parseHash(location.hash, r);
      if(h.tf && h.tf !== L.tf){ L.tf = h.tf; L.barMs = TF_MS[h.tf]; }
      TFS.forEach((t) => tfBtns[t].classList.toggle('on', t === L.tf));
      let start = h.sym || (r.funds.TQQQ ? 'TQQQ' : (L.rows[0] ? rowPick(L.rows[0], 'long') || L.rows[0].funds[0].sym : null));
      if(start) select(start);
    }
    TFS.forEach((t) => tfBtns[t].classList.toggle('on', t === L.tf));
    renderQuotes(); renderStrip(); renderSide(); renderLadder(); renderHold();

    const ctl = {
      ticketSlot,
      show(){
        L.visible = true;
        if(!L.loaded) init();
        else if(L.sym){ ensureCharts(); resize(); loadBars(); loadDaily(); pollQuotes(true); loadLadder(); loadGex(); if(L.tab === 'movers') loadMovers(); }
      },
      hide(){ L.visible = false; ['bars', 'quotes', 'ladder', 'movers', 'gex'].forEach((k) => clearTimeout(L.timers[k])); L.seq += 1; },
      select,
      state: () => ({sym: L.sym, tf: L.tf, side: L.side}),
    };
    return ctl;
  }
  api.mount = mount;
})(typeof window !== 'undefined' ? window : globalThis);
