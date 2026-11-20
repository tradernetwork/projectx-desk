'use strict';
// Leveraged ETF desk: the picker, two charts (underlying and fund) and the analytics strip inside the Robinhood panel.
// DISPLAY ONLY. This file reads three GET routes (/api/rh/letf, /api/rh/bars, /api/rh/quotes) and draws. It makes no POST, asks for no
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
  function neighbor(rows, sym, dir){
    if(!rows.length) return null;
    let i = rows.findIndex((r) => r.funds.some((f) => f.sym === sym));
    const cur = i >= 0 ? rows[i].funds.find((f) => f.sym === sym) : null;
    const side = cur ? cur.side : 'long';
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

  const api = {MIN_N, THIN, TFS, TF_MS, STYLES, fmtPct, fmtBps, fmtDollarVol, levLabel, fmtPrice, chartTitle, fundTitle, ageNote,
    filterRows, grouped, rowPick, displayRows, neighbor, flipSide, underlyingOf, parseHash, hashString,
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
    read_failed: 'A Robinhood read failed.', bad_tf: 'Unknown timeframe.', bad_symbol: 'Unknown symbol.', too_many: 'Too many symbols.',
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

  function mount(host, hooks){
    const H = hooks || {};
    const L = {uni: null, rows: [], opts: {q: '', groups: [], side: 'both', lev: 'all', hideThin: false, sort: 'dv'}, sym: null, tf: '5m',
      visible: false, loaded: false, ch: {u: null, e: null}, tk: {u: '', e: ''}, series: {}, forming: {u: null, e: null}, quotes: {}, quotesAt: 0,
      daily: null, dailyKey: '', seq: 0, sessionOpen: false, barMs: TF_MS['5m'], expanded: {}, msg: '', msg2: '', timers: {bars: 0, quotes: 0}, err: ''};
    host.classList.add('lx');
    // rail
    const rail = el('div', 'lx-rail'), search = el('input', 'lx-search'), chips = el('div', 'lx-chips'), seg = el('div', 'lx-seg'),
      levs = el('div', 'lx-seg'), thin = el('label', 'lx-thin'), thinBox = el('input'), sortSel = el('select'), list = el('div', 'lx-list'), foot = el('div', 'lx-foot');
    search.type = 'search'; search.placeholder = 'Search underlying, ticker or name  ( / )'; search.autocomplete = 'off'; search.setAttribute('aria-label', 'Search funds');
    thinBox.type = 'checkbox';
    thin.append(thinBox, el('span', '', 'Hide thin (under $5M/day)'));
    [['dv', 'Sort: $ volume'], ['az', 'Sort: A to Z'], ['lev', 'Sort: leverage']].forEach((o) => { const op = el('option', '', o[1]); op.value = o[0]; sortSel.append(op); });
    sortSel.setAttribute('aria-label', 'Sort');
    rail.append(search, chips, seg, levs, thin, sortSel, list, foot);
    // main
    const main = el('div', 'lx-main'), ribbon = el('div', 'lx-ribbon'), status = el('div', 'lx-status'), charts = el('div', 'lx-charts'), strip = el('div', 'lx-strip');
    const mkPane = (cls) => { const p = el('div', 'lx-chart ' + cls), t = el('div', 'lx-ct'), cv = el('div', 'lx-cv'), msg = el('div', 'lx-cmsg'); msg.hidden = true; p.append(t, cv, msg); return {p, t, cv, msg}; };
    const pu = mkPane('lx-chart-u'), pe = mkPane('lx-chart-e');
    charts.append(pu.p, pe.p);
    const tfBtns = {};
    TFS.forEach((tf, i) => { const b = btn('', tf, 'Timeframe (key ' + (i + 1) + ')'); b.onclick = () => setTf(tf); tfBtns[tf] = b; ribbon.append(b); });
    ribbon.append(status);
    main.append(ribbon, charts, strip);
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
          const row = el('div', 'lx-row'), u = el('div', 'lx-u', r.key);
          if(r.u.chart_kind === 'proxy') u.title = 'Charted via ' + r.u.chart + (r.u.proxy_quality === 'approximate' ? ' (approximate)' : '');
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

    function select(sym){
      if(!L.uni || !L.uni.funds[sym]) return;
      L.sym = sym; L.series = {}; L.forming = {u: null, e: null}; L.quotes = {}; L.daily = null; L.dailyKey = ''; L.err = ''; L.msg2 = '';
      try{ history.replaceState(null, '', hashString(sym, L.tf)); }catch(_){ /* a sandboxed frame: the hash is a convenience */ }
      renderList(); renderHead(); renderStrip(); renderQuotes(); renderActions();
      loadBars(); loadDaily(); pollQuotes(true);
    }
    function setTf(tf){
      if(TFS.indexOf(tf) < 0 || tf === L.tf) return;
      L.tf = tf; L.barMs = TF_MS[tf]; L.forming = {u: null, e: null}; L.err = '';
      TFS.forEach((t) => tfBtns[t].classList.toggle('on', t === tf));
      if(L.sym){ try{ history.replaceState(null, '', hashString(L.sym, tf)); }catch(_){ /* see select */ } loadBars(); }
    }
    function renderHead(){
      const f = curFund(), u = curU();
      pu.t.textContent = u ? chartTitle(u) : '';
      pe.t.textContent = f ? fundTitle(L.sym, f) : '';
      const none = !!u && !chartSym();
      pu.cv.hidden = none; pu.msg.hidden = !none;
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
      if(!L.ch.u && pu.cv.clientWidth > 0 && !pu.cv.hidden) L.ch.u = makeChart(pu.cv);
      if(!L.ch.e && pe.cv.clientWidth > 0) L.ch.e = makeChart(pe.cv);
      return true;
    }
    function paint(which, full){
      const C = L.ch[which], bars = barsFor(which);
      if(!C || !bars) return;
      const tk = (which === 'u' ? chartSym() : L.sym) + '|' + L.tf;
      if(full || L.tk[which] !== tk){ L.tk[which] = tk; resetChart(C, tk.split('|')[0], L.tf, bars); }
      else patchChart(C, tk.split('|')[0], L.tf, bars);
    }
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
      renderStrip();
      if(L.sessionOpen) pollQuotes(true);
    }
    async function loadDaily(){
      if(!L.visible || !L.sym) return;
      const key = symsOf().join(','), now = Date.now();
      if(L.daily && L.dailyKey === key && now - L.daily.at < 1800000) return;
      if(L.tf === '1D' && L.series[chartSym()] && L.series[L.sym]){ L.daily = {at: now, series: L.series}; L.dailyKey = key; renderStrip(); return; }
      const my = L.seq;
      let r;
      try{ r = await getJson('/api/rh/bars', {symbols: key, tf: '1D'}); }catch(_){ return; }
      if(!r.ok || my !== L.seq || !L.visible) { if(!r.ok && r.code === 'busy') setTimeout(loadDaily, 5000); return; }
      L.daily = {at: now, series: r.series}; L.dailyKey = key;
      renderStrip();
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
    function renderStrip(){
      strip.replaceChildren();
      const f = curFund(), u = curU();
      if(!f){ strip.append(el('p', 'lx-none', 'Pick a fund on the left.')); return; }
      if(L.err) strip.append(el('p', 'lx-bad', L.err));
      const us = chartSym(), qu = us && L.quotes[us], qe = L.quotes[L.sym];
      const dser = L.daily ? L.daily.series : {};
      const du = us && dser[us] ? dser[us].bars : [], de = dser[L.sym] ? dser[L.sym].bars : [];
      const uPrev = qu ? sessionPrevClose(du, qu) : null, ePrev = qe ? sessionPrevClose(de, qe) : null;
      const mv = todayMove(qu ? qu.last : null, uPrev, qe ? qe.last : null, ePrev, f.lev);
      const rets = us ? alignDaily(du, de) : [];
      const rl = realizedLeverage(rets), vd = volDecay(f.lev, rets.map((x) => x.u));
      const items = el('div', 'lx-items');
      items.append(
        kv('Underlying today', us ? fmtPct(mv.u) : 'NA'),
        kv('Fund today', mv.suspect ? 'NA (corporate action?)' : fmtPct(mv.e)),
        kv('Expected ' + levLabel(f.lev), fmtPct(mv.expected)),
        kv('Gap', fmtBps(mv.gap_bps)),
        kv('Realized leverage', rl.beta == null ? 'NA (' + rl.n + ' of ' + MIN_N + ' sessions)' : rl.beta.toFixed(2) + 'x  (R2 ' + (rl.r2 == null ? 'NA' : rl.r2.toFixed(2)) + ', n=' + rl.n + ')'),
        kv('Volatility decay', vd.decay_ann == null ? 'NA (' + vd.n + ' of ' + MIN_N + ' sessions)' : fmtPct(vd.decay_ann, 1) + ' a year at sigma ' + fmtPct(vd.sigma_ann, 0).replace('+', '') + '  (n=' + vd.n + ')'),
        kv('20d $ volume', fmtDollarVol(f.dv20) + '  (as of ' + (L.uni ? L.uni.as_of : '?') + ')', isNum(f.dv20) && f.dv20 < THIN ? 'warn' : ''),
        kv('Spread', (() => { const sp = qe ? spreadBps(qe.bid, qe.ask) : null; return sp == null ? 'NA' : sp.toFixed(1) + ' bps'; })()));
      strip.append(items);
      const notes = el('div', 'lx-notes');
      if(u && u.chart_kind === 'proxy') notes.append(el('p', 'lx-warn', 'Underlying charted via ' + u.chart + (u.proxy_quality === 'approximate' ? ' (approximate)' : '') + ': expected move, gap, beta and decay are approximate.' + (u.group === 'crypto' ? ' Regular-hours closes against a 24/7 asset.' : '')));
      if(u && !us) notes.append(el('p', 'lx-warn', 'No underlying chart for ' + u.key + ', so beta, decay and the expected move are NA.'));
      notes.append(el('p', '', 'Volatility decay = -L(L-1)/2 x sigma^2 per day, x252 a year, from the underlying\'s last ' + MIN_N + ' daily returns. No fees, financing or tracking error. A daily-reset fund drifts from L x the multi-day move; this is the part that is arithmetic, not a forecast.'));
      strip.append(notes);
    }

    // ---- keys: only while this view is on screen, and every key handled is stopped before desk.js sees it ----
    function typing(t){ return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable); }
    function onKey(e){
      if(!L.visible || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target, inSearch = t === search;
      const stop = () => { e.preventDefault(); e.stopPropagation(); };
      if(typing(t) && !inSearch) return;
      if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){
        const next = neighbor(displayRows(L.rows, groupNames()), L.sym, e.key === 'ArrowDown' ? 1 : -1);
        if(next) select(next);
        stop(); return;
      }
      if(inSearch){ if(e.key === 'Escape'){ search.blur(); stop(); } else if(e.key === 'Enter' && L.sym){ if(H.prefill) L.msg2 = H.prefill(L.sym); renderActions(); stop(); } return; }
      if(e.key === '/'){ search.focus(); stop(); return; }
      if(e.key === 'Enter' && L.sym){ L.msg2 = H.prefill ? H.prefill(L.sym) : ''; renderActions(); stop(); return; }
      const k = e.key.toLowerCase();
      if((k === 'l' || k === 's') && L.sym){
        const f = flipSide(L.uni, L.sym, k === 'l' ? 'long' : 'short');
        if(f) select(f);
        stop(); return;
      }
      if(/^[1-6]$/.test(k)){ setTf(TFS[+k - 1]); stop(); }
    }
    document.addEventListener('keydown', onKey, true);
    const resize = () => { ['u', 'e'].forEach((w) => { try{ if(L.ch[w]) L.ch[w].resize(); }catch(_){ /* mid-teardown */ } }); };
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
    renderQuotes(); renderStrip();

    const ctl = {
      ticketSlot,
      show(){
        L.visible = true;
        if(!L.loaded) init();
        else if(L.sym){ ensureCharts(); resize(); loadBars(); loadDaily(); pollQuotes(true); }
      },
      hide(){ L.visible = false; clearTimeout(L.timers.bars); clearTimeout(L.timers.quotes); L.seq += 1; },
      select,
      state: () => ({sym: L.sym, tf: L.tf}),
    };
    return ctl;
  }
  api.mount = mount;
})(typeof window !== 'undefined' ? window : globalThis);
