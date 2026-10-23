'use strict';
/* ProjectX desk front end. Renders server numbers only; sizing is never recomputed here.
   Server strings always go through textContent. */
const $ = id => document.getElementById(id);
const UNIT = {second:1, minute:2, hour:3, day:4, week:5, month:6};
const HOLD_MS = 600, ON_SCREEN_MS = 1500, POLL_MS = 2000, REVEAL_MS = 30000;
const LS = {get(k, d){try{return JSON.parse(localStorage.getItem(k)) ?? d}catch{return d}}, set(k, v){try{localStorage.setItem(k, JSON.stringify(v))}catch{}}};
const mk = (tag, cls, text) => {const e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e};
async function j(u){const r = await fetch(u); if(!r.ok) throw new Error((await r.text().catch(() => '')) || r.status); return r.json()}
const q = new URLSearchParams(location.search);

/* ================= state ================= */
const S = {
  contractId: null, tf: 'minute,5', side: 'long',
  riskPct: LS.get('px.riskPct3', 0.05), stopK: LS.get('px.stopK', 1.5), sizeFor: 'AUTO',
  stream: q.get('stream') === '1',      // never persisted
  revealUntil: 0, aiOpen: q.get('ai') === '1',
  ai: null, st: null, digest: '', aiFails: 0, inflight: false, seq: 0,
  ticket: null,   // local copy of the last STAGE response
  placed: null,   // {contract, acked, state}
  prev: null, lastTickAt: 0,
  lvl: 'EMA21',   // selected level for reaction markers; memory only, never persisted
  sizeCid: null,
  cd: {left: 0, at: 0},   // last server cooldown_left_s and when we got it; ticked locally
};
const streaming = () => S.stream && Date.now() >= S.revealUntil;

/* ================= formatting + data-ai fill ================= */
const getPath = (o, p) => p.split('.').reduce((a, k) => (a == null ? undefined : a[k]), o);
const VERDICT = {ABOVE_RANDOM: 'Better than random', NOT_DISTINGUISHABLE: 'Same as random', BELOW_RANDOM: 'Worse than random', TOO_FEW_TESTS: 'Too few tests'};
function fmt(v, kind){
  if(kind === 'verdict') return VERDICT[v] || 'NA';
  if(v == null || (typeof v === 'number' && !isFinite(v))) return 'NA';
  if(typeof v === 'boolean') return v ? 'YES' : 'NO';
  if(typeof v !== 'number') return String(v);
  switch(kind){
    case 'int': return String(Math.round(v));
    case 'num1': return v.toFixed(1);
    case 'num': return v.toLocaleString('en-US', {maximumFractionDigits: 2});
    case 'num2': return v.toFixed(2);
    case 'rr': return (v > 0 ? '+' : '') + v.toFixed(2) + 'R';
    case 'sgn': return (v > 0 ? '+' : '') + v.toFixed(2);
    case 'dol': return '$' + v.toLocaleString('en-US', {maximumFractionDigits: 2});
    case 'usd': return (v > 0 ? '+' : v < 0 ? '-' : '') + '$' + Math.abs(v).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    case 'mmss': {const s = Math.max(0, Math.round(v)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0')}
    case 'pctfrac': return (v * 100).toFixed(v * 100 % 1 ? 1 : 0) + '%';
    default: return String(v);
  }
}
function fill(root, obj){
  for(const e of root.querySelectorAll('[data-ai]')){
    const own = e.closest('[data-own]');
    if(own && own !== root) continue;   // a self-filled region (pair table, level rows) is not part of /ai/state
    const v = obj ? getPath(obj, e.dataset.ai) : undefined;
    const t = v === undefined || v === null || v === '' ? 'NA' : fmt(v, e.dataset.fmt);
    if(e.textContent !== t) e.textContent = t;
    e.classList.toggle('na', t === 'NA');
    if(e.dataset.sign){e.classList.toggle('pos', typeof v === 'number' && v > 0); e.classList.toggle('neg', typeof v === 'number' && v < 0)}
  }
}
const num = v => typeof v === 'number' && isFinite(v);

/* ================= stream mode: nodes are removed, not hidden (D4) ================= */
function applySlots(){
  const hide = streaming();
  for(const s of document.querySelectorAll('[data-slot]')){
    const want = s.dataset.slot === 'sens' ? !hide : hide;
    if(want && !s.firstChild) s.append(document.getElementById(s.dataset.tpl).content.cloneNode(true));
    else if(!want && s.firstChild) s.replaceChildren();
  }
  const on = S.stream;
  $('stream-toggle').setAttribute('aria-pressed', String(on));
  $('reveal').hidden = !on;
  $('reveal').textContent = Date.now() < S.revealUntil ? 'Re-hide' : 'Reveal 30s';
  document.body.classList.toggle('streaming', hide);
}
let revealTimer = 0;
function setStream(on){
  S.stream = on; S.revealUntil = 0; clearTimeout(revealTimer);
  applySlots(); renderAll(); schedule(0);
}
$('stream-toggle').onclick = () => setStream(!S.stream);
$('reveal').onclick = () => {
  clearTimeout(revealTimer);
  if(Date.now() < S.revealUntil){S.revealUntil = 0}
  else{S.revealUntil = Date.now() + REVEAL_MS; revealTimer = setTimeout(() => {S.revealUntil = 0; applySlots(); renderAll(); schedule(0)}, REVEAL_MS)}
  applySlots(); renderAll(); schedule(0);
};

/* ================= CSRF + POST ================= */
let csrfP = null;
const csrf = (force) => (csrfP && !force) ? csrfP : (csrfP = j('/api/csrf').then(r => r.token).catch(e => {csrfP = null; throw e}));   // a failed fetch is never cached: HALT and FLATTEN must recover on their own
async function post(path, body, retried){
  let tok; try{tok = await csrf(retried)}catch(e){return {ok: false, status: 0, data: {detail: 'csrf unavailable'}}}
  let r, data = null;
  try{
    r = await fetch(path, {method: 'POST', headers: {'content-type': 'application/json', 'x-px-csrf': tok}, body: JSON.stringify(body || {})});
    data = await r.json().catch(() => null);
  }catch(e){return {ok: false, status: 0, data: {detail: 'network error'}}}
  if(r.status === 403 && data && data.detail === 'bad csrf token' && !retried) return post(path, body, true);
  return {ok: r.ok, status: r.status, data};
}
const detailOf = r => (r.data && typeof r.data.detail === 'string') ? r.data.detail : ('HTTP ' + r.status);
function actMsg(t){$('act-msg').textContent = t || ''}

/* ================= size inputs ================= */
const rootCache = {};
async function sizeContract(){
  if(S.sizeFor === 'AUTO') return S.contractId;
  if(!(S.sizeFor in rootCache)){
    rootCache[S.sizeFor] = null;
    try{
      const cs = await j('/contracts?q=' + encodeURIComponent(S.sizeFor));
      const re = new RegExp('^' + S.sizeFor + '[FGHJKMNQUVXZ]\\d{1,2}$');
      rootCache[S.sizeFor] = (cs.find(c => re.test(c.name)) || {}).id || null;
    }catch{rootCache[S.sizeFor] = null}
  }
  return rootCache[S.sizeFor];
}
function syncSizeInputs(){
  const pr = (v, w) => Math.abs(v - w) < 1e-6;
  for(const b of $('risk-pct').querySelectorAll('[data-v]')) b.setAttribute('aria-pressed', String(pr(S.riskPct, +b.dataset.v)));
  for(const b of $('stop-atr').querySelectorAll('[data-v]')) b.setAttribute('aria-pressed', String(pr(S.stopK, +b.dataset.v)));
  for(const b of $('side').querySelectorAll('[data-v]')) b.setAttribute('aria-pressed', String(S.side === b.dataset.v));
  for(const b of $('size-contract').querySelectorAll('[data-v]')) b.setAttribute('aria-pressed', String(S.sizeFor === b.dataset.v));
  $('risk-val').textContent = (S.riskPct * 100).toFixed(S.riskPct * 100 % 1 ? 1 : 0) + '%';
  LS.set('px.riskPct3', S.riskPct); LS.set('px.stopK', S.stopK);
}
const MQ = {qty: 1, stopTouched: false};   // manual BUY/SELL state (filled in below)
function inputChanged(){MQ.stopTouched = false; syncSizeInputs(); schedule(100)}   // the stop chips moved: the manual stop follows the ATR stop again
$('risk-pct').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.riskPct = +b.dataset.v; inputChanged()}};
$('risk-dec').onclick = () => {S.riskPct = Math.max(0.005, Math.round((S.riskPct - 0.005) * 1000) / 1000); inputChanged()};
$('risk-inc').onclick = () => {S.riskPct = Math.min(0.1, Math.round((S.riskPct + 0.005) * 1000) / 1000); inputChanged()};
$('stop-atr').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.stopK = +b.dataset.v; inputChanged()}};
function stepTicks(d){   // move the stop by whole ticks using the server's own stop_ticks, atr and tick
  const z = S.ai && S.ai.size, atr = S.ai && S.ai.ind && S.ai.ind.atr, tick = S.ai && S.ai.contract && S.ai.contract.tick;
  if(!z || z.status !== 'OK' || !atr || !tick) return;
  const t = Math.max(1, z.stop_ticks + d);
  S.stopK = Math.min(10, Math.max(0.1, Math.round((t - 0.5) * tick / atr * 10000) / 10000));
  if(S.stopK === 1.5) S.stopK = 1.5001;   // a manual step must never land exactly on the anchored default
  inputChanged();
}
$('stop-dec').onclick = () => stepTicks(-1);
$('stop-inc').onclick = () => stepTicks(1);
$('side').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.side = b.dataset.v; inputChanged()}};
$('size-contract').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.sizeFor = b.dataset.v; inputChanged()}};

/* ================= polling ================= */
let debounceT = 0;
function schedule(ms){clearTimeout(debounceT); debounceT = setTimeout(refresh, ms)}
async function refresh(){
  if(S.inflight){schedule(150); return}
  S.inflight = true; const my = ++S.seq;
  try{
    const cid = await sizeContract();
    const key = JSON.stringify({cid, side: S.side, stopK: S.stopK, riskPct: S.riskPct});     // what THIS size box was computed for
    const p = new URLSearchParams({side: S.side, risk_pct: String(S.riskPct), stop_k: String(S.stopK)});
    if(cid) p.set('contract', cid);
    if(streaming()) p.set('stream', '1');
    const sq = new URLSearchParams(); if(S.contractId) sq.set('contract', S.contractId); if(streaming()) sq.set('stream', '1');
    const stateUrl = '/state.json' + (sq.toString() ? '?' + sq : '');
    const [ai, st, dg] = await Promise.all([
      j('/ai/state?' + p).catch(() => null), j(stateUrl).catch(() => null),
      S.aiOpen ? fetch('/ai/digest?' + p).then(r => r.ok ? r.text() : null).catch(() => null) : null]);
    if(my !== S.seq) return;
    S.aiFails = ai ? 0 : S.aiFails + 1;
    S.prev = S.ai; S.ai = ai; S.st = st; S.sizeCid = cid; S.aiKey = key; S.cd = {left: (ai && ai.behavior && num(ai.behavior.cooldown_left_s)) ? ai.behavior.cooldown_left_s : 0, at: performance.now()}; if(dg != null) S.digest = dg;
    pairPoll(cid);
    announce(); renderAll();
  }finally{S.inflight = false}
}
setInterval(() => schedule(0), POLL_MS);

/* ================= render ================= */
function pill(id, text, cls){const e = $(id); e.textContent = text; e.className = 'pill' + (cls ? ' ' + cls : '')}
function renderRibbon(){
  const a = S.ai, st = S.st;
  const f = st && st.feed;
  const age = v => (v == null ? '' : ' ' + Math.round(v) + 's');
  pill('feed-market', f ? 'MKT ' + (f.market_up ? 'UP' : 'DOWN') + age(f.market_age_s) : 'MKT NA', f ? (f.market_up ? 'ok' : 'bad') : '');
  pill('feed-user', f ? 'USER ' + (f.user_up ? 'UP' : 'DOWN') + age(f.user_age_s) : 'USER NA', f ? (f.user_up ? 'ok' : 'bad') : '');
  const m = a && a.mode;
  const unknown = !!(m && m.unknown_order) || (S.placed && S.placed.state === 'UNKNOWN');
  if(!m){pill('engine', 'ENGINE NA'); pill('trading', 'TRADING NA'); pill('acct-kind', st && st.account && st.account.simulated != null ? (st.account.simulated ? 'SIM' : 'LIVE') : 'ACCT NA', st && st.account && st.account.simulated === false ? 'live' : '')}
  else{
    pill('engine', unknown ? 'UNKNOWN ORDER' : m.halt ? 'HALTED' : 'READY', unknown || m.halt ? 'bad' : 'ok');
    pill('trading', 'TRADING ' + (m.trading ? 'ON' : 'OFF'), m.trading ? 'warn' : '');
    pill('acct-kind', m.account_kind === 'LIVE' ? 'LIVE' : m.account_kind === 'SIM' ? 'SIM' : 'ACCT NA', m.account_kind === 'LIVE' ? 'live' : m.account_kind === 'SIM' ? 'sim' : '');
  }
  document.body.classList.toggle('alarm', !!(unknown || (m && m.halt)));
  document.body.classList.toggle('acct-live', !!(m && m.account_kind === 'LIVE'));
  fill($('ribbon'), a);
  if(st && st.boot_error) actMsg('Backend: ' + st.boot_error);
  if(st && st.quote && st.quote.lastPrice != null && Date.now() - S.lastTickAt > 4000) setPrice(st.quote.lastPrice, true);
}

function renderSize(){
  const a = S.ai, z = a && a.size;
  fill($('size'), a);
  const ok = z && z.status === 'OK';
  const bind = $('size-bind');
  const b = ok ? (z.refused ? 'granularity' : String(z.binding || '')).toLowerCase() : '';
  bind.textContent = ok ? (z.refused ? 'REFUSED' : String(z.binding || 'NA').toUpperCase()) : (z && z.status) || 'NA';
  bind.className = 'badge ' + (ok ? (z.refused ? 'bad' : b.replace(/[^a-z_]/g, '')) : '');
  const rf = $('size-refused'), na = $('size-notallowed'), al = $('size-alert');
  const notAllowed = !!(ok && z.allowed === false), refused = !!(ok && z.refused);
  // ONE banner. NOT ALLOWED outranks REFUSED; both lines only when both are true. The WHY list holds the reasons,
  // so the headline stays short whenever WHY is on screen (stream mode can hide WHY: then the longer text stays).
  const whyOn = !$('size-why').hidden;
  na.hidden = !notAllowed;
  if(refused){
    rf.hidden = false;
    rf.textContent = 'REFUSED: ' + (z.reason || 'unknown') + (z.reason === 'GRANULARITY' && !whyOn ? ': one contract risks more than the budget. Not rounded up.' : '');
  }else if(z && z.status && z.status !== 'OK'){
    rf.hidden = false; rf.textContent = 'NO SIZE (' + z.status + '): needs a live quote, ATR and a positive budget.';
  }else if(!a){rf.hidden = false; rf.textContent = 'NO SIZE: AI state unavailable.'}
  else rf.hidden = true;
  al.hidden = na.hidden && rf.hidden;
  const stop = $('stop-val'); stop.textContent = ok ? z.stop_ticks + 't' : 'NA';
  const plan = $('size-plan'), money = v => typeof v === 'number' ? '$' + v.toLocaleString('en-US', {maximumFractionDigits: 0}) : 'NA';
  plan.hidden = !(ok && z.stop_mode); plan.replaceChildren();
  if(!plan.hidden){
    const ln = (...parts) => {const d = mk('div'); for(const p of parts) d.append(p); plan.append(d)};
    ln(mk('b', '', 'STOP '), document.createTextNode(z.stop_mode === 'ANCHORED' ? 'beyond ' + String(z.stop_anchor || '').slice(0, 24) + ', ' + z.stop_k_eff + 'x ATR' : z.stop_mode === 'MANUAL' ? 'manual ' + z.stop_k_eff + 'x ATR' : '1.5x ATR (no respected level in reach)'));
    ln(mk('b', '', 'TARGET '), document.createTextNode(typeof z.target === 'number' ? z.target + ' (' + String(z.target_level || '').slice(0, 24) + ') ' + z.rr + 'R, needs ' + z.breakeven_win_rate + '% wins' : 'none: no respected level at 1R or better'));
    ln(mk('b', '', 'IF STOPPED '), document.createTextNode(streaming() ? 'hidden in stream mode' : 'drawdown room ' + money(z.drawdown_left) + ', daily ' + money(z.daily_left) + ', total open risk with this trade ' + money(z.heat_usd)));
    ln(mk('b', '', 'COST '), document.createTextNode(typeof z.cost_pct_r === 'number' ? z.cost_pct_r + '% of 1R' + (streaming() || typeof z.cost_usd !== 'number' ? '' : ' ($' + z.cost_usd.toFixed(2) + ' per contract, ' + z.slippage_ticks + 't slippage + fees)') + ', 1:1 needs ' + z.breakeven_1r_net + '% wins after costs' : 'unknown: set PX_COST_RT_USD (fees per contract per round turn)'));
  }
  for(const id of ['stop-dec', 'stop-inc']) $(id).disabled = !ok;
  const box = $('size');
  box.classList.toggle('is-refused', !!(ok && z.refused));
  box.classList.toggle('is-blocked', notAllowed || refused);
  box.classList.toggle('is-na', !ok);
}

function placeReady(){
  const t = S.ticket;
  if(!t || !t.ticket_id || t.consumed) return {ok: false, why: t ? (t.consumed ? 'Ticket used.' : 'Refused: nothing to place.') : 'No ticket.'};
  const left = (t.expiresAt - performance.now()) / 1000;
  if(left <= 0) return {ok: false, why: 'Ticket expired. Stage again.'};
  if(t.gone) return {ok: false, why: 'Ticket superseded or no longer staged.'};
  if(S.ai && S.ai.mode && S.ai.mode.halt) return {ok: false, why: 'Halted.'};
  if(!t.placeable) return {ok: false, why: 'Server says this ticket is not placeable (trading off?).'};
  if(t.placing) return {ok: false, why: 'Sending...'};
  const seen = performance.now() - t.shownAt;
  if(seen < ON_SCREEN_MS) return {ok: false, why: 'Read the ticket... ' + ((ON_SCREEN_MS - seen) / 1000).toFixed(1) + 's'};
  return {ok: true, why: 'Press and hold ' + HOLD_MS + ' ms. A click does nothing.'};
}

/* ---- behavior rails: the server decides, the page only shows it and ticks the clock between polls ---- */
function railState(){
  const b = S.ai && S.ai.behavior; if(!b) return {mode: null, left: 0};
  if(b.locked) return {mode: 'lock', left: 0};
  const left = Math.max(0, Math.ceil(S.cd.left - (performance.now() - S.cd.at) / 1000));
  return left > 0 ? {mode: 'cool', left} : {mode: null, left: 0};
}
let railFilled = null;
function renderRail(){
  const a = S.ai, b = a && a.behavior, rs = railState();
  const box = $('rail');
  box.hidden = !rs.mode;
  box.classList.toggle('locked', rs.mode === 'lock');
  $('rail-cool').hidden = rs.mode !== 'cool'; $('rail-lock').hidden = rs.mode !== 'lock';
  if(railFilled !== a){railFilled = a; fill(box, a)}
  if(rs.mode === 'cool') $('rail-clock').textContent = fmt(rs.left, 'mmss');
  // streak readout: pips out of the lockout threshold (server may send lockout_after; 3 is the documented default)
  const st = $('streak'); st.hidden = !b;
  if(b){
    const mx = num(b.lockout_after) && b.lockout_after > 0 ? Math.round(b.lockout_after) : 3;
    $('streak-max').textContent = String(mx);
    setPips('bar-streak', num(b.consecutive_losses) ? b.consecutive_losses : 0, mx);
    st.title = b.known ? 'Consecutive losing trades today. Lockout at ' + mx + '.' : 'Trade history not seeded: unknown counts as refused.';
  }
}
function renderTicketMeta(){
  renderRail();
  const t = S.ticket;
  const st = $('tk-state');
  if(!t){st.textContent = 'NONE'; st.className = 'badge'}
  else if(!t.ticket_id){st.textContent = 'REFUSED'; st.className = 'badge bad'}
  else{
    const left = Math.max(0, Math.round((t.expiresAt - performance.now()) / 1000));
    const dead = t.consumed || t.gone || left <= 0;
    st.textContent = t.consumed ? 'USED' : t.gone ? 'GONE' : left <= 0 ? 'EXPIRED' : 'STAGED';
    st.className = 'badge ' + (dead ? 'warn' : 'ok');
    $('tk-ttl').textContent = (dead ? 0 : left) + 's';
  }
  const prot = $('tk-prot'); const pv = prot.textContent;
  prot.classList.toggle('unprot', /UNPROTECTED/.test(pv)); prot.classList.toggle('atomic', pv === 'ATOMIC');
  const ph = $('place-hint'), pr = placeReady();
  ph.textContent = pr.why;
  $('place-btn').disabled = !pr.ok && !hold;
  $('place-label').textContent = hold ? 'HOLDING...' : 'PLACE (hold)';
  const s = $('stage-btn');
  const z = S.ai && S.ai.size, halted = S.ai && S.ai.mode && S.ai.mode.halt;
  const notAllowed = !!(z && z.status === 'OK' && z.allowed === false);
  const rs = railState(), locked = rs.mode === 'lock', cool = rs.mode === 'cool';
  s.disabled = !(z && z.status === 'OK' && !z.refused && !notAllowed && !halted && !locked && !cool && !staging);
  const refused = !!(z && z.status === 'OK' && z.refused);
  const label = staging ? 'STAGING...' : halted ? 'HALTED: staging blocked' : locked ? 'LOCKED FOR THE SESSION' + (num(S.ai.behavior.consecutive_losses) ? ': ' + Math.round(S.ai.behavior.consecutive_losses) + ' LOSSES IN A ROW' : '') : cool ? 'COOLDOWN ' + fmt(rs.left, 'mmss') : notAllowed ? 'NOT ALLOWED: STAGE WILL REFUSE' : refused ? 'REFUSED: ' + String(z.reason || 'size').toLowerCase()
    : (z && z.status && z.status !== 'OK') ? 'NO SIZE: cannot stage' : 'STAGE (preview, places nothing)';
  if(s.textContent !== label) s.textContent = label;
  s.classList.toggle('refused', refused || notAllowed || !!halted || locked); s.classList.toggle('cool', cool && !halted && !locked); s.classList.toggle('lockbtn', locked && !halted);
  renderTreeTick();
}
function renderTicketFields(){
  const t = S.ticket;
  const view = t && t.ticket_id ? {ticket: {id8: t.hash8, ttl_s: null, readback: t.readback, checks_failed: []}} : null;
  fill($('ticket'), view);
  const ttl = $('tk-ttl'); if(!view) ttl.textContent = 'NA';
  const sfx = $('tk-suffix'); if(sfx) sfx.textContent = t && t.account_suffix ? t.account_suffix : 'NA';
  const rf = $('tk-refused'), cl = $('tk-checks');
  cl.replaceChildren();
  if(t && !t.ticket_id){
    rf.hidden = false; rf.textContent = 'REFUSED: ' + (t.error || 'ticket not staged');
    for(const c of t.failed){cl.append(mk('li', '', c)); }
    cl.hidden = !t.failed.length;
  }else{rf.hidden = true; cl.hidden = true}
  renderTree();
}

/* ---- bracket tree: entry node -> stop node, plus an R ruler. Server values only. ---- */
function treeModel(){
  const tr = S.ai && S.ai.trade, t = S.ticket;
  const tkMatch = tr && t && t.ticket_id && t.hash8 === tr.id8 ? t : null;
  // a fresh staged ticket wins: it is what the hold would send, so its bracket is the one to read
  if(t && t.ticket_id && !t.consumed && !t.gone && t.expiresAt - performance.now() > 0)
    return {mode: 'preview', t, base: 'ticket.readback.', obj: {ticket: {readback: t.readback, id8: t.hash8}}};
  if(tr) return {mode: 'live', tr, t: tkMatch, base: 'trade.', obj: S.ai};
  return null;
}
function setState(id, text, cls){const e = $(id); if(e.textContent !== text) e.textContent = text; e.className = 'nstate ' + (cls || '')}
function bindAi(id, path, f){const e = $(id); if(!e) return; if(path){e.dataset.ai = path; if(f) e.dataset.fmt = f} else delete e.dataset.ai}
function renderTree(){
  const m = treeModel(), tree = $('tree');
  $('tree-empty').hidden = !!m || !!(S.ticket && !S.ticket.ticket_id);
  tree.hidden = !m;
  $('tk-details').hidden = !m || (m.mode === 'live' && !m.t);   // the readback belongs to a ticket; no ticket, no wall of NA
  if(!m) return;
  const live = m.mode === 'live', tr = m.tr || {}, rb = (m.t && m.t.readback) || {};
  const sideRaw = live ? String(tr.side || '') : (/^SELL/.test(String(rb.side)) ? 'SELL' : 'BUY');
  const buy = /^(BUY|LONG)/i.test(sideRaw);
  const prot = String(rb.protection || '');
  const mode = String(live ? tr.stop_mode : rb.stop_plan || '').toUpperCase();
  const oco = mode === 'OCO' || prot === 'ATOMIC';
  // which /ai/state path each value comes from
  bindAi('tr-e-size', m.base + 'size', 'int');
  bindAi('tr-e-ref', m.base + 'entry_ref');
  bindAi('tr-s-price', m.base + 'stop_price');
  bindAi('tr-id8', live ? 'trade.id8' : 'ticket.id8');
  bindAi('tr-s-ticks', live ? null : 'ticket.readback.stop_ticks', 'int');
  bindAi('tree-risk', m.base + 'risk_usd', 'num');
  bindAi('tree-r1', m.base + 'risk_usd', 'num');
  fill(tree, m.obj);
  if(live){$('tr-s-ticks').textContent = m.t && m.t.readback ? String(m.t.readback.stop_ticks) : ''}
  $('tr-s-ticks').parentElement.hidden = $('tr-s-ticks').textContent === '';
  const rootTxt = String((m.t && m.t.readback && m.t.readback.contract) || '').split(' ')[0] || (live ? '' : 'NA');
  $('tr-e-root').textContent = rootTxt;
  $('tr-e-type').textContent = String(rb.order_type || (live ? '' : 'NA'));
  const es = $('tr-e-side'); es.textContent = buy ? 'BUY' : 'SELL'; es.className = 'nside ' + (buy ? 'buy' : 'sell');
  const ss = $('tr-s-side'); ss.textContent = buy ? 'SELL' : 'BUY'; ss.className = 'nside ' + (buy ? 'sell' : 'buy');
  $('tree-link').textContent = oco ? 'native OCO bracket, atomic' : 'position bracket: stop sent after fill';
  const pb = $('tree-prot');
  pb.textContent = oco ? 'ATOMIC' : 'STOP AFTER FILL'; pb.className = 'badge ' + (oco ? 'atomic' : 'warn');
  const filled = live && (tr.filled || 0) > 0;
  let unprot = null;   // null | 'warn' | 'naked'
  if(!live){
    const left = (m.t.expiresAt - performance.now()) / 1000;
    $('tree-mode').textContent = left > 0 ? 'PREVIEW, NOTHING SENT' : 'PREVIEW EXPIRED';
    setState('tr-e-state', 'STAGED', 'staged');
    setState('tr-s-state', oco ? 'ATOMIC' : 'NOT SENT', oco ? 'ok' : 'staged');
    if(!oco) unprot = 'warn';
    $('tr-filled').textContent = '';
  }else{
    const flat = filled && S.ai && Array.isArray(S.ai.positions) && !S.ai.positions.length;
    $('tree-mode').textContent = flat ? 'LIVE BRACKET, BUT NO OPEN POSITION: STALE, RECONCILE' : 'LIVE BRACKET';
    const st = String(tr.state || '');
    if(st === 'UNKNOWN') setState('tr-e-state', 'UNKNOWN', 'bad');
    else if(st === 'PLACING') setState('tr-e-state', 'SENDING', 'warn');
    else if(filled) setState('tr-e-state', 'FILLED', 'ok');
    else if(tr.acked) setState('tr-e-state', 'ACCEPTED, NOT FILLED', 'info');
    else setState('tr-e-state', 'PLACED', 'info');
    const ss2 = String(tr.stop_state || 'PENDING');
    setState('tr-s-state', ss2, ss2 === 'ACKED' || ss2 === 'ATOMIC' ? 'ok' : ss2 === 'FAILED' ? 'bad' : 'warn');
    if(!(ss2 === 'ACKED' || ss2 === 'ATOMIC')) unprot = filled || ss2 === 'FAILED' ? 'naked' : 'warn';
    $('tr-filled').textContent = num(tr.size) ? 'filled ' + (tr.filled || 0) + '/' + tr.size : '';
  }
  const ub = $('tr-unprot');
  ub.hidden = !unprot; ub.className = 'unprot' + (unprot === 'naked' ? ' naked' : '');
  ub.textContent = unprot === 'naked' ? 'UNPROTECTED, POSITION OPEN' : 'UNPROTECTED';
  tree.className = 'tree ' + m.mode + (filled ? ' filled' : '') + (live && tr.state === 'UNKNOWN' ? ' unknown' : '') + (unprot === 'naked' ? ' naked' : '') + (live && !unprot ? ' protected' : '');
  // R ruler: where the last price sits between -1R (stop) and +3R
  const entry = Number(live ? tr.entry_ref : rb.entry_ref), stop = Number(live ? tr.stop_price : rb.stop_price);
  const last = S.ai && S.ai.quote && num(S.ai.quote.last) ? S.ai.quote.last : lastPx;
  const nowEl = $('rr-now'), rv = $('rr-val');
  if(isFinite(entry) && isFinite(stop) && entry !== stop && num(last)){
    const r = (last - entry) / (entry - stop);
    nowEl.classList.remove('off');
    nowEl.style.left = (Math.max(0, Math.min(1, (r + 1) / 4)) * 100).toFixed(1) + '%';
    rv.textContent = 'now ' + (r > 3 ? '> 3R' : r < -1 ? '< -1R' : (r >= 0 ? '+' : '') + r.toFixed(2) + 'R') + ' at ' + Number(last).toFixed(cur.dec ?? 2);
  }else{nowEl.classList.add('off'); rv.textContent = 'now NA'}
}
function renderTreeTick(){if(!$('tree').hidden || S.ticket) renderTree()}

/* room gauge: fill = room / track; colour turns amber under 50%, magenta under 25% of its own limit */
function setBar(id, frac, health){
  const i = $(id); if(frac == null || !isFinite(frac)){i.style.width = '0'; i.className = ''; return}
  const f = Math.max(0, Math.min(1, frac)); i.style.width = (f * 100).toFixed(1) + '%';
  const h = health == null ? f : health;
  i.className = h < 0.25 ? 'low' : h < 0.5 ? 'mid' : '';
}
function setPips(id, used, max){
  const box = $(id);
  if(!num(used) || !num(max) || max <= 0){box.replaceChildren(); box.className = 'pips'; return}
  const n = Math.round(max), u = Math.max(0, Math.round(used));
  if(n <= 20){
    if(box.children.length !== n || box.firstChild?.tagName !== 'I') box.replaceChildren(...Array.from({length: n}, () => mk('i')));
    [...box.children].forEach((p, k) => p.classList.toggle('on', k < u));
  }else{
    if(!box.querySelector('.cont')){const c = mk('span', 'cont'); c.append(mk('i')); box.replaceChildren(c)}
    box.querySelector('.cont i').style.width = Math.min(100, u / n * 100).toFixed(1) + '%';
  }
  box.className = 'pips' + (u >= n ? ' full' : u / n >= 0.7 ? ' warn' : '');
}
function renderRisk(){
  const a = S.ai, r = a && a.risk;
  fill($('risk'), a);
  $('risk-binds').title = 'which limit binds the budget';
  // daily room and drawdown buffer share one dollar scale, so the shorter bar IS the binding one
  let dFrac = null, dHealth = null, ddFrac = null, dTrack = 1, ddTrack = 1;
  if(r){
    const scale = Math.max(r.daily_limit || 0, r.dd_buffer || 0);
    if(scale > 0 && num(r.daily_room) && num(r.daily_limit)){
      dTrack = r.daily_limit / scale; dFrac = r.daily_room / r.daily_limit; dHealth = dFrac;
      if(num(r.dd_buffer)){ddTrack = r.dd_buffer / scale; ddFrac = 1}
    }else if(num(r.daily_used_pct)){dFrac = 1 - r.daily_used_pct / 100; dHealth = dFrac}   // stream: percent only
  }
  $('track-daily').style.width = (dTrack * 100).toFixed(1) + '%';
  $('track-dd').style.width = (ddTrack * 100).toFixed(1) + '%';
  setBar('bar-daily', dFrac, dHealth);
  setBar('bar-dd', ddFrac, ddFrac);
  const bind = r && r.binding;
  $('g-daily').classList.toggle('binds', bind === 'DAILY'); $('g-daily').querySelector('.bindtag').hidden = bind !== 'DAILY';
  $('g-dd').classList.toggle('binds', bind === 'DRAWDOWN'); $('g-dd').querySelector('.bindtag').hidden = bind !== 'DRAWDOWN';
}
function renderKpi(){
  const a = S.ai, k = (a && a.kpi) || {}, r = a && a.risk;
  fill($('kpis'), a);
  const n = k.n;
  for(const b of $('kpis').querySelectorAll('.ss')){b.hidden = !(num(n) && n > 0 && n < 30); b.title = 'n=' + n + ' is under 30 trades: noise, not an edge'}
  // win rate against break-even: one track, the fill is the live rate, the marker is the rate needed
  const wr = k.win_rate, be = k.breakeven_win_rate;
  const fillEl = $('wr-fill'), beEl = $('wr-be'), v = $('wr-verdict');
  fillEl.style.width = num(wr) ? Math.max(0, Math.min(100, wr)) + '%' : '0';
  beEl.hidden = !num(be); if(num(be)) beEl.style.left = Math.max(0, Math.min(100, be)) + '%';
  let tone = '';
  if(num(wr) && num(be)){
    const d = wr - be; tone = d >= 0 ? 'pos' : 'neg';
    v.textContent = (d >= 0 ? 'Above BE +' : 'Below BE ') + d.toFixed(1) + 'pt';
  }else v.textContent = num(wr) ? 'BE NA' : 'No trades';
  fillEl.className = 'wr-fill ' + tone; v.className = 'verdict ' + tone;
  $('wr-need').hidden = !num(be); $('wr-need-na').hidden = num(be);
  const tot = (k.wins || 0) + (k.losses || 0);
  $('wl-w').style.width = tot ? (k.wins / tot * 100) + '%' : '0'; $('wl-l').style.width = tot ? (k.losses / tot * 100) + '%' : '0';
  const sign = v2 => num(v2) && v2 !== 0 ? (v2 > 0 ? 'pos' : 'neg') : '';
  const pnlTone = streaming() ? '' : sign(r && r.pnl_day), expTone = streaming() ? '' : sign(k.expectancy);
  $('kpi-pnl').dataset.tone = pnlTone; $('kpi-exp').dataset.tone = expTone;
  $('kpi-wr').dataset.tone = tone;
  $('kpi-pf').dataset.tone = num(k.profit_factor) ? (k.profit_factor >= 1 ? 'pos' : 'neg') : '';
  setPips('bar-orders', r && r.orders_today, r && r.orders_max);
  setPips('bar-open', r && r.open_contracts, r && r.max_contracts);
  // the pip meters are not drawn; the value takes the tone instead (amber from 70% of the cap, magenta at it)
  const pt = id => $(id).classList.contains('full') ? 'neg' : $(id).classList.contains('warn') ? 'warn' : '';
  $('kpi-orders').dataset.tone = pt('bar-orders'); $('kpi-open').dataset.tone = pt('bar-open'); $('streak').dataset.tone = pt('bar-streak');
}

function renderBlotter(){
  const a = S.ai;
  fill($('blotter'), a);
  const pb = $('pos-body'); pb.replaceChildren();
  const ps = (a && a.positions) || [];
  ps.forEach((p, i) => {
    const tr = mk('tr');
    const td = (txt, cls, path, f) => {const c = mk('td', cls, f ? fmt(txt, f) : (txt == null ? 'NA' : txt)); if(path) c.dataset.ai = path; tr.append(c)};
    td(p.side, /long|buy/i.test(String(p.side)) ? 'side-long' : /short|sell/i.test(String(p.side)) ? 'side-short' : '', null); td(p.size, 'r', `positions.${i}.size`, 'int'); td(p.contract, '', null); td(p.avg_price, 'r', `positions.${i}.avg_price`, 'num');
    pb.append(tr);
  });
  if(!ps.length){const tr = mk('tr', 'empty'); const c = mk('td', '', a ? 'Flat' : 'NA'); c.colSpan = 4; tr.append(c); pb.append(tr)}
  const ob = $('ord-body'); ob.replaceChildren();
  const os = (a && a.orders) || [];
  os.forEach((o, i) => {
    const tr = mk('tr');
    const td = (txt, cls, path, f) => {const c = mk('td', cls, f ? fmt(txt, f) : (txt == null ? 'NA' : txt)); if(path) c.dataset.ai = path; tr.append(c)};
    td(o.side); td(o.type); td(o.size, 'r', `orders.${i}.size`, 'int');
    td(o.stop != null ? o.stop : o.limit, 'r', o.stop != null ? `orders.${i}.stop` : `orders.${i}.limit`, 'num');
    const c = mk('td'); if(o.tag){c.append(mk('span', 'tag ' + (o.tag === 'OWN' ? 'own' : 'ext'), o.tag))} else c.textContent = 'NA'; tr.append(c);
    ob.append(tr);
  });
  if(!os.length){const tr = mk('tr', 'empty'); const c = mk('td', '', a ? 'None' : 'NA'); c.colSpan = 5; tr.append(c); ob.append(tr)}
  const halted = a && a.mode && a.mode.halt;
  const unknown = !!(a && a.mode && a.mode.unknown_order) || (S.placed && S.placed.state === 'UNKNOWN');
  $('halt-banner').hidden = !(halted || unknown);
  $('halt-title').textContent = unknown ? 'UNKNOWN OUTCOME, HALTED' : 'HALTED';
  $('halt-sub').textContent = unknown ? 'An order write got no definitive reply. Reconcile before anything else.' : 'New orders are refused until a human clears the halt.';
}

function renderAiPanel(){
  $('ai-panel').hidden = !S.aiOpen;
  $('ai-toggle').setAttribute('aria-pressed', String(S.aiOpen));
  $('ai-state').textContent = JSON.stringify(S.ai).replace(/</g, '\\u003c');
  if(S.aiOpen) $('ai-digest').textContent = S.ai ? (S.digest || 'NA') : 'NA';
  $('asof').textContent = S.ai ? 'asof ' + S.ai.asof + (S.aiFails ? ' (stale)' : '') : (S.aiFails ? 'AI state unavailable' : 'asof NA');
}
function renderPlaceResult(){
  const el = $('place-result'), p = S.placed;
  if(!p){el.textContent = ''; el.className = 'presult'; return}
  // an ACK is not a fill: say so until a position for that contract appears
  const pos = S.ai && (S.ai.positions || []).some(x => x.contract && x.contract === p.contract && x.size);
  if(p.acked && pos){el.textContent = 'FILLED: position is open.'; el.className = 'presult done'}
  else if(p.acked){el.textContent = 'ACCEPTED, not filled. The broker accepted the order; waiting for a position.'; el.className = 'presult ok'}
  else{el.textContent = 'NOT PLACED (' + p.state + ')' + (p.reasons.length ? ': ' + p.reasons.join('; ') : ''); el.className = 'presult bad'}
}
function renderAll(){
  applySlots();
  renderWhy(); renderRibbon(); renderKpi(); renderSize(); renderManual(); renderTicketFields(); renderTicketMeta(); renderRisk(); renderBlotter(); renderAiPanel(); renderPlaceResult();
  fill($('size'), S.ai);
  renderPair(); renderEma(); renderRegime(); renderBias(); renderLevels(); renderManage(); drawReact(false);
  syncDesk();
}
setInterval(() => {renderTicketMeta()}, 250);


/* ================= size WHY, chips, not-allowed banner ================= */
function renderWhy(){
  const a = S.ai, z = a && a.size, ok = !!(z && z.status === 'OK');
  // a stream-mode view must never show the dollar-bearing text from a pre-stream response
  const safe = ok && !(streaming() && a.stream !== 'ON');
  const ul = $('size-why');
  ul.replaceChildren();
  const cuts = new Set(ok && z.scale && Array.isArray(z.scale.reasons) ? z.scale.reasons.filter(r => !/no cut/.test(String(r))) : []);
  if(safe && Array.isArray(z.why)) for(const w of z.why){
    const t = String(w), li = mk('li', '', t);
    if(/^REFUSED|not in PX_ALLOWED|not allowed/i.test(t)) li.className = 'bad';
    else if(cuts.has(t)) li.className = 'cut';
    ul.append(li);
  }
  ul.hidden = !ul.children.length;
  $('size-math').hidden = !ul.hidden;   // the prose math line repeats WHY; it only shows when WHY is off
  // scaler chip: ADX 15 chop x0.67
  const sc = ok && z.scale && z.scale.reduced ? z.scale : null;
  const rs = sc && Array.isArray(sc.reasons) ? sc.reasons.map(String) : [];
  const adxCut = rs.find(r => /^ADX/.test(r) && !/no cut/.test(r)), atrCut = rs.find(r => /^ATR/.test(r) && !/no cut/.test(r));
  $('cs-adx').hidden = !adxCut; $('cs-atr').hidden = !atrCut;
  $('cs-adx-note').textContent = adxCut ? (/unknown/.test(adxCut) ? 'unknown' : 'chop') : '';
  $('cs-atr-note').textContent = atrCut ? (/unknown/.test(atrCut) ? 'unknown' : 'high') : '';
  $('chip-scale').hidden = !sc;
  $('chip-cap').hidden = !(ok && z.capped);
  $('chip-lock').hidden = !((a && a.behavior && a.behavior.size_locked) || (ok && z.loss_capped));
  $('size-chips').hidden = $('chip-scale').hidden && $('chip-cap').hidden && $('chip-lock').hidden;
  fill($('size-chips'), a);
}

/* ================= micro vs full-size pair ================= */
const PAIR = {MES: 'ES', ES: 'MES', MNQ: 'NQ', NQ: 'MNQ', M2K: 'RTY', RTY: 'M2K', MYM: 'YM', YM: 'MYM', MGC: 'GC', GC: 'MGC', MCL: 'CL', CL: 'MCL'};
const MICRO = new Set(['MES', 'MNQ', 'M2K', 'MYM', 'MGC', 'MCL']);
const PAIR_MS = 3000;
const P = {key: '', at: 0, busy: false, mate: null};
const idRoot = id => { const p = String(id || '').split('.'); return p.length >= 5 && p[0] === 'CON' ? p[3] : null };
function mateOf(id){
  const p = String(id || '').split('.'), m = p.length >= 5 ? PAIR[p[3]] : null;
  if(!m) return null;
  p[3] = m; return p.join('.');
}
const pairKey = mid => [mid, S.side, S.riskPct, S.stopK, streaming() ? 's' : 'n'].join('|');
function pairPoll(cid){
  if(document.visibilityState !== 'visible') return;
  const mid = mateOf(cid);
  if(!mid){P.key = ''; P.mate = null; return}
  if(P.busy || Date.now() - P.at < PAIR_MS) return;
  P.busy = true; P.at = Date.now();
  const key = pairKey(mid);
  const qs = new URLSearchParams({side: S.side, risk_pct: String(S.riskPct), stop_k: String(S.stopK), contract: mid});
  if(streaming()) qs.set('stream', '1');
  j('/ai/state?' + qs).then(a => {P.key = key; P.mate = a && a.size ? a.size : null})
    .catch(() => {P.key = key; P.mate = null})
    .finally(() => {P.busy = false; renderPair()});
}
function pairStatus(z){
  if(!z || z.status !== 'OK') return 'NA';
  return z.allowed === false ? 'NOT ALLOWED' : z.refused ? 'REFUSED' : z.capped ? 'CAPPED' : 'OK';
}
function renderPair(){
  const box = $('pair'), cid = S.sizeCid, mid = mateOf(cid), myRoot = idRoot(cid);
  if(!mid || !myRoot){box.hidden = true; box.replaceChildren(); return}
  box.hidden = false;
  const own = S.ai && S.ai.size && S.ai.size.status === 'OK' ? S.ai.size : null;
  const mate = P.key === pairKey(mid) ? P.mate : null;
  const mateRoot = PAIR[myRoot];
  const [micro, full] = MICRO.has(myRoot) ? [[myRoot, own, true], [mateRoot, mate, false]] : [[mateRoot, mate, false], [myRoot, own, true]];
  const dollars = !streaming();
  const tbl = mk('table', 'tbl pairtbl'); tbl.setAttribute('aria-label', 'Micro versus full-size sizing');
  const hr = mk('tr');
  for(const [t, c] of [['Root', ''], ['Contracts', 'r'], ...(dollars ? [['Risk / ct', 'r']] : []), ['Status', '']]){const th = mk('th', c, t); th.scope = 'col'; hr.append(th)}
  tbl.append(mk('thead')); tbl.firstChild.append(hr);
  const tb = mk('tbody'); tbl.append(tb);
  const view = {pair: {}};
  [[micro, 'micro'], [full, 'full']].forEach(([[root, z, mine], key]) => {
    const ok = !!(z && z.status === 'OK'), st = pairStatus(z);
    view.pair[key] = {root: root, contracts: ok ? z.contracts : null, rpc: ok ? z.rpc : null, status: st};
    const tr = mk('tr', mine ? 'sel' : '');
    const td = (cls, path, f) => {const c = mk('td', cls); c.dataset.ai = path; if(f) c.dataset.fmt = f; tr.append(c); return c};
    td('root', `pair.${key}.root`);
    td('r', `pair.${key}.contracts`, 'int');
    if(dollars) td('r', `pair.${key}.rpc`, 'dol');
    const c = td('st st-' + st.toLowerCase().replace(/\s+/g, '-'), `pair.${key}.status`);
    tb.append(tr);
  });
  box.replaceChildren(mk('div', 'pair-h', 'Micro vs full-size, same risk'), tbl);
  fill(box, view);
}
document.addEventListener('visibilitychange', () => {if(document.visibilityState === 'visible') pairPoll(S.sizeCid)});

/* ================= EMA stack badge ================= */
function renderEma(){
  const e = S.ai && S.ai.ind && S.ai.ind.ema, st = e && ['BULL', 'BEAR', 'MIXED'].includes(e.state) ? e.state : null;
  const bd = $('ema-badge');
  bd.hidden = false;
  bd.dataset.state = st || 'NA';
  fill(bd, S.ai);
}

/* ================= regime chip: advisory only, never disables a button ================= */
function renderRegime(){
  const c = $('regime-chip'), g = S.ai && S.ai.regime, st = g && g.state;
  const adx = g && typeof g.adx === 'number' && isFinite(g.adx) ? Math.round(g.adx) : null;
  if((st !== 'RANGING' && st !== 'TRENDING') || adx === null){c.hidden = true; return}
  c.hidden = false; c.className = 'chip ' + (st === 'RANGING' ? 'warn' : 'muted');
  c.textContent = st === 'RANGING' ? `RANGING, ADX ${adx} (sit-out zone)` : `TRENDING, ADX ${adx}`;
  c.title = 'ADX below 20 means price is chopping. Trend setups fail more often here.';
}

/* ================= bias chip: advisory read of trend agreement, never gates or sizes anything ================= */
const BIAS_DIR = new Set(['LONG', 'SHORT', 'NEUTRAL']), BIAS_GRADE = new Set(['A+', 'A', 'B', 'C', 'D', 'F']);
function renderBias(){
  const c = $('bias-chip'), b = S.ai && S.ai.bias;
  if(!b || !BIAS_DIR.has(b.direction) || !BIAS_GRADE.has(b.grade)){c.hidden = true; return}
  c.hidden = false;
  c.className = 'chip bias ' + (b.direction === 'LONG' ? 'long' : b.direction === 'SHORT' ? 'short' : 'flat');
  const n = v => typeof v === 'number' && isFinite(v) ? v : null;
  c.textContent = b.direction === 'NEUTRAL' ? 'BIAS NEUTRAL' : `BIAS ${b.direction} ${b.grade}`;
  c.title = `Advisory only: ${n(b.agree)}/${n(b.reads)} reads agree, score ${n(b.score)}/5` + (Array.isArray(b.flags) && b.flags.length ? ` (${b.flags.filter(f => /^[A-Z_]{2,16}$/.test(f)).join(', ')})` : '') +
    '. 5m/15m/1h EMA stacks, VWAP side, tape delta. Not a signal.';
}

/* ================= level reactions panel ================= */
const LV_ORDER = ['EMA8', 'EMA21', 'EMA34', 'VWAP'];
const RESULTS = new Set(['BOUNCE', 'FAIL', 'NEUTRAL', 'PENDING']);
function renderLevels(){
  const a = S.ai, raw = a && Array.isArray(a.levels) ? a.levels : [];
  const list = $('lv-list');
  const byName = {}; for(const lv of raw) if(lv && LV_ORDER.includes(lv.name)) byName[lv.name] = lv;
  const names = LV_ORDER.filter(n => byName[n]);
  if(names.length && !byName[S.lvl]) S.lvl = names[0];
  list.replaceChildren();
  if(!names.length){list.append(mk('p', 'lv-empty', a ? 'No level data for this contract yet.' : 'NA')); $('lv-legend').hidden = true; return}
  $('lv-legend').hidden = false;
  const tf = a.ind && a.ind.tf;
  $('lv-tf').textContent = tf ? 'measured on ' + tf + ' bars' : '';
  const view = {levels: {}};
  for(const n of names){
    const lv = byName[n]; view.levels[n] = lv;
    const row = mk('button', 'lvrow'); row.type = 'button'; row.dataset.lv = n;
    row.setAttribute('aria-pressed', String(S.lvl === n));
    const pre = `levels.${n}.`;
    const sp = (cls, path, f, txt) => {const e = mk('span', cls, txt); if(path){e.dataset.ai = pre + path; if(f) e.dataset.fmt = f} return e};
    const l1 = mk('span', 'l1'); l1.append(mk('span', 'lv-n', n), sp('lv-px', 'price', 'num'), sp('lv-d', 'dist_pt', 'sgn'), mk('span', 'lv-u', 'pt'),
      sp('lv-pos ' + (lv.pos === 'ABOVE' ? 'above' : lv.pos === 'BELOW' ? 'below' : ''), 'pos'));
    const l2 = mk('span', 'l2');
    const held = mk('span', 'lv-held'); held.append('held ', sp('', 'bounced', 'int'), ' of ', sp('', 'n', 'int'));
    const hasRate = num(lv.rate) && num(lv.n) && lv.n > 0;
    if(hasRate){held.append(' (', sp('', 'rate', 'int'), '%)')}
    else held.append(num(lv.n) && lv.n === 0 ? ' (no tests yet)' : ' (NA)');
    l2.append(held);
    // baseline: the same rules on random control lines near price. A level only means something if it beats this.
    const rnd = mk('span', 'l2r');
    rnd.title = 'The same bounce rules applied to random lines near price. If random lines hold as often, this level shows nothing.';
    rnd.append('random ');
    if(num(lv.random_rate)) rnd.append(sp('', 'random_rate', 'int'), '% (n ', sp('', 'random_n', 'int'), ')');
    else rnd.append(num(lv.random_n) && lv.random_n > 0 ? sp('', 'random_rate', 'int') : 'NA');
    const vk = Object.prototype.hasOwnProperty.call(VERDICT, lv.verdict) ? lv.verdict : null;
    // ONE verdict per row: the comparison with random lines, plus 'small sample' in the same muted text (colour carries the state)
    const vd = mk('span', 'vd vd-' + (vk ? vk.toLowerCase() : 'na'));
    vd.append(sp('', 'verdict', 'verdict'));
    if(lv.small_sample) vd.append(' \u00b7 small sample');
    const bits = [];
    if(num(lv.failed) && lv.failed > 0) bits.push([lv.failed, 'failed']);
    if(num(lv.neutral) && lv.neutral > 0) bits.push([lv.neutral, 'flat']);
    if(num(lv.pending) && lv.pending > 0) bits.push([lv.pending, 'pending']);
    const l3 = mk('span', 'l3');
    const extra = mk('span', 'lv-x');
    bits.forEach(([v, w], i) => {if(i) extra.append(', '); const b = mk('b'); b.dataset.ai = pre + (w === 'failed' ? 'failed' : w === 'flat' ? 'neutral' : 'pending'); b.dataset.fmt = 'int'; extra.append(b, ' ' + w)});
    l3.append(vd, extra);
    const lr = RESULTS.has(lv.last_result) ? lv.last_result : null;
    const last = mk('span', 'lv-last');
    if(lr){last.append('last ', mk('span', 'rs rs-' + lr.toLowerCase(), lr));
      if(num(lv.last_bars_ago)){last.append(', '); const b = mk('b'); b.dataset.ai = pre + 'last_bars_ago'; b.dataset.fmt = 'int'; last.append(b, ' bars ago')}}
    else last.append('last none');
    l3.append(last);
    l2.append(rnd);
    row.append(l1, l2, l3);
    list.append(row);
  }
  fill($('levels'), view);
}
$('lv-list').addEventListener('click', e => {
  const b = e.target.closest('[data-lv]'); if(!b) return;
  S.lvl = b.dataset.lv;
  renderLevels(); drawReact(true);
});

/* ================= manage trade: tighten-only stop buttons (human clicks, like FLATTEN; no data-ai on controls) ================= */
const MG_LABEL = {BREAKEVEN: 'Stop to breakeven', ATR_TRAIL: 'Stop to ATR trail', EMA21_TRAIL: 'Stop to EMA21 trail'};
let mgBusy = false, mgMsgT = 0;
function mgSay(t, cls){
  const m = $('mg-msg'); m.textContent = t || ''; m.className = 'mg-msg' + (cls ? ' ' + cls : '');
  clearTimeout(mgMsgT); if(t) mgMsgT = setTimeout(() => {m.textContent = ''; m.className = 'mg-msg'}, 15000);
}
function renderManage(){
  const a = S.ai, g = a && a.manage, box = $('manage');
  box.hidden = !g;
  if(!g){mgSay(''); $('mg-opts').replaceChildren(); return}
  fill(box, a);
  const sugg = Array.isArray(g.suggestions) ? g.suggestions.filter(x => x && MG_LABEL[x.name] && num(x.price)) : [];
  const host = $('mg-opts');
  const want = sugg.map(x => x.name).join(',');
  if(host.dataset.names !== want){host.dataset.names = want; host.replaceChildren(...sugg.map(x => {
    const w = mk('div', 'mg-opt'); w.dataset.name = x.name;
    const b = mk('button', 'mg-btn'); b.type = 'button'; b.dataset.name = x.name;
    b.append(mk('span', 'mg-l'), mk('span', 'mg-tag'));
    w.append(b); return w;
  }))}
  const dec = cur.dec ?? 2;
  for(const x of sugg){
    const w = host.querySelector(`.mg-opt[data-name="${x.name}"]`), b = w.firstChild;
    b.dataset.price = String(x.price);
    const lab = MG_LABEL[x.name] + ' ' + Number(x.price).toFixed(dec);
    if(b.firstChild.textContent !== lab) b.firstChild.textContent = lab;
    const tag = b.lastChild;
    const hint = x.recommended ? 'recommended' : x.valid ? 'not earned yet' : 'not valid now';
    if(tag.textContent !== hint) tag.textContent = hint;
    tag.className = 'mg-tag ' + (x.recommended ? 'rec' : x.valid ? 'wait' : 'off');
    b.classList.toggle('rec', !!x.recommended);
    b.disabled = !x.valid || mgBusy;
    const why = String(x.why || '');
    b.title = x.valid ? (why + (x.recommended ? '' : '. Not earned yet: the trade has not moved far enough.')) : 'Not valid now: ' + why + ' would not tighten the stop, or it sits too close to the market.';
  }
}
$('mg-opts').addEventListener('click', async e => {
  const b = e.target.closest('button.mg-btn'); if(!b || b.disabled || mgBusy || !S.contractId) return;
  const px = Number(b.dataset.price); if(!isFinite(px)) return;
  mgBusy = true; renderManage(); mgSay('Moving stop...', '');
  const r = await post('/api/stop', {contract: S.contractId, stop_price: px});
  mgBusy = false;
  const d = r.data || {};
  if(r.ok && d.ok) mgSay('Stop moved to ' + (num(d.stop_price) ? d.stop_price : px), 'ok');
  else mgSay('Stop NOT moved: ' + (r.ok ? String(d.reason || 'refused') : detailOf(r)), 'bad');
  renderManage(); schedule(0); setTimeout(() => schedule(0), 600);
});

/* ================= announcements ================= */
let lastPol = '', lastAss = '';
function say(kind, t){
  const e = $(kind === 'a' ? 'live-assertive' : 'live-polite');
  if((kind === 'a' ? lastAss : lastPol) === t) return;
  if(kind === 'a') lastAss = t; else lastPol = t;
  e.textContent = t;
}
function announce(){
  const a = S.ai, p = S.prev;
  if(!a) return;
  if(a.mode.halt && !(p && p.mode.halt)) say('a', 'HALTED: new orders refused');
  if(!a.mode.halt && p && p.mode.halt){lastAss = ''; $('live-assertive').textContent = ''; say('p', 'Halt cleared')}
  const ab = a.behavior || {}, pb2 = (p && p.behavior) || {};
  if(ab.locked && !pb2.locked) say('p', 'Locked for the session');
  else if(!ab.locked && ab.cooldown_left_s > 0 && !(pb2.cooldown_left_s > 0)) say('p', 'Cooldown after a loss: walk away');
  const rz = a.size && a.size.refused, pz = p && p.size && p.size.refused;
  if(rz && !pz) say('p', 'REFUSED: ' + (a.size.reason || 'size') );
  if(p && a.fills_today != null && p.fills_today != null && a.fills_today > p.fills_today) say('p', 'Fill recorded');
  if(p && (a.positions || []).length !== (p.positions || []).length) say('p', (a.positions || []).length ? 'Position open' : 'Position closed');
  if(S.placed && S.placed.state === 'UNKNOWN') say('a', 'UNKNOWN order outcome: reconcile');
  // a staged ticket the server no longer reports as staged
  const t = S.ticket;
  if(t && t.ticket_id && !t.consumed && !t.gone && performance.now() - t.shownAt > 3000){
    if(!a.ticket || a.ticket.id8 !== t.hash8) t.gone = true;
  }
}

/* ================= actions: stage / place / halt / flatten / reconcile ================= */
let staging = false;
async function doStage(body, cid){        // ONE staging path for the STAGE button and the analyst-callout button: same server checks, same ticket
  staging = true; renderTicketMeta();
  const r = await post('/api/preview', body);
  staging = false;
  S.placed = null;
  if(!r.ok){S.ticket = {ticket_id: null, error: detailOf(r), failed: []}}
  else{
    const d = r.data, tk = d.ticket;
    const failed = (d.verdict || []).filter(v => !v.ok).map(v => v.check + ': ' + v.detail);
    for(const rsn of (Array.isArray(d.reasons) ? d.reasons : [])){   // rail refusals, if the verdict list did not already carry them
      const t = String(rsn), m = /^(cooldown_after_loss|loss_lockout):/.exec(t);
      if(m && !failed.some(f => f.startsWith(m[1] + ':'))) failed.push(t);
    }
    if(d.ok && tk){
      S.ticket = {ticket_id: tk.ticket_id, hash8: tk.hash8, readback: tk.readback, account_suffix: tk.account_suffix,
                  expiresAt: performance.now() + tk.ttl_s * 1000, shownAt: performance.now(), placeable: !!d.placeable,
                  contract: cid, gone: false, consumed: false, placing: false};
    }else S.ticket = {ticket_id: null, error: 'checks failed', failed};
  }
  renderAll(); schedule(0);
  if(S.ticket && S.ticket.ticket_id) say('p', 'Ticket staged');
}
/* ---- manual BUY / SELL: the trader's own contracts and stop, through the SAME preview and ticket (server derives the risk and runs every check) ---- */
MQ.qty = Math.max(1, parseInt(LS.get('px.mq', 1), 10) || 1);
function renderManual(){
  const a = S.ai, z = a && a.size, ok = !!(z && z.status === 'OK');
  const cap = ok && typeof z.cap === 'number' ? Math.max(1, z.cap) : null;
  if(cap !== null && MQ.qty > cap) MQ.qty = cap;
  $('mq-val').textContent = String(MQ.qty);
  const st = $('mq-stop');
  if(!MQ.stopTouched && ok && typeof z.stop_ticks === 'number') st.value = String(z.stop_ticks);   // mirrors the ATR stop until you type your own
  const can = !!(a && S.contractId) && !staging && parseInt(st.value, 10) >= 1;
  for(const id of ['buy-btn', 'sell-btn']) $(id).disabled = !can;
  $('mq-dec').disabled = MQ.qty <= 1; $('mq-inc').disabled = cap !== null && MQ.qty >= cap;
  $('mq-cap').textContent = cap !== null ? 'cap ' + cap : '';
}
$('mq-dec').onclick = () => {MQ.qty = Math.max(1, MQ.qty - 1); LS.set('px.mq', MQ.qty); renderManual()};
$('mq-inc').onclick = () => {MQ.qty += 1; LS.set('px.mq', MQ.qty); renderManual()};
$('mq-stop').oninput = () => {MQ.stopTouched = true; renderManual()};
$('mq-atr').onclick = () => {MQ.stopTouched = false; renderManual()};
async function manualStage(side){
  const t = parseInt($('mq-stop').value, 10);
  if(!(t >= 1)){$('msg').textContent = 'Manual order needs a stop distance in ticks.'; return}
  const cid = await sizeContract();
  if(!cid){$('msg').textContent = 'Pick a contract first.'; return}
  S.side = side; syncSizeInputs(); schedule(0);          // the LONG/SHORT chip shows the side being staged
  await doStage({contract: cid, side, stop_ticks: t, qty: MQ.qty, order_type: 'market', source: 'manual'}, cid);   // same checks, same ticket, same hold-to-place
}
$('buy-btn').onclick = () => manualStage('long');
$('sell-btn').onclick = () => manualStage('short');

$('stage-btn').onclick = async () => {
  const z = S.ai && S.ai.size; if(!z || z.status !== 'OK' || z.refused || z.allowed === false) return;
  const cid = await sizeContract();
  if(S.aiKey !== JSON.stringify({cid, side: S.side, stopK: S.stopK, riskPct: S.riskPct})){   // the chips moved since this size was computed
    $('msg').textContent = 'Size box is updating for your new selection. Check it, then STAGE again.'; schedule(0); return;
  }
  await doStage({contract: cid, side: S.side, stop_ticks: z.stop_ticks, risk_pct: S.riskPct, order_type: 'market'}, cid);
};

let hold = null;
const RING = 94.25;
function setRing(p){$('ring-fg').style.strokeDashoffset = String(RING * (1 - p)); $('place-btn').style.setProperty('--hold', String(p))}
function holdStart(){
  if(hold || !placeReady().ok) return;
  const h = hold = {t0: performance.now(), raf: 0};
  $('place-btn').classList.add('holding');
  const step = () => {
    if(hold !== h) return;
    const p = Math.min(1, (performance.now() - h.t0) / HOLD_MS);
    setRing(p);
    if(p >= 1){hold = null; $('place-btn').classList.remove('holding'); setRing(0); placeNow()}
    else h.raf = requestAnimationFrame(step);
  };
  h.raf = requestAnimationFrame(step);
  renderTicketMeta();
}
function holdCancel(){
  if(!hold) return;
  cancelAnimationFrame(hold.raf); hold = null; $('place-btn').classList.remove('holding'); setRing(0); renderTicketMeta();
}
async function placeNow(){
  const t = S.ticket; if(!t || !placeReady().ok) return;
  t.placing = true; renderTicketMeta();
  const r = await post('/api/place', {ticket_id: t.ticket_id, confirm_hash: t.hash8});
  t.placing = false; t.consumed = true;
  const d = r.data || {};
  S.placed = {contract: t.contract, acked: !!(r.ok && d.ok && d.acked), state: r.ok ? String(d.state || 'NA') : 'HTTP ' + r.status,
              reasons: r.ok ? (Array.isArray(d.reasons) ? d.reasons.map(String) : []) : [detailOf(r)]};
  if(S.placed.state === 'UNKNOWN') say('a', 'UNKNOWN order outcome: reconcile');
  renderAll(); schedule(0);
}
const pb = $('place-btn');
pb.addEventListener('pointerdown', e => {if(e.button === 0) holdStart()});
for(const ev of ['pointerup', 'pointercancel', 'pointerleave']) pb.addEventListener(ev, holdCancel);
pb.addEventListener('keydown', e => {if((e.key === ' ' || e.key === 'Enter') && !e.repeat){e.preventDefault(); holdStart()}});
pb.addEventListener('keyup', e => {if(e.key === ' ' || e.key === 'Enter') holdCancel()});
pb.addEventListener('blur', holdCancel);
pb.addEventListener('click', e => e.preventDefault());       // a plain click does nothing
pb.addEventListener('contextmenu', e => e.preventDefault());
document.addEventListener('visibilitychange', () => {if(S.ticket) S.ticket.shownAt = performance.now(); holdCancel()});

$('halt-btn').onclick = async () => {const r = await post('/api/halt', {}); actMsg(r.ok ? 'Halt set.' : 'Halt FAILED: ' + detailOf(r)); schedule(0)};
$('flatten-btn').onclick = async () => {
  actMsg('Flattening...');
  const r = await post('/api/flatten', {});
  if(!r.ok){actMsg('Flatten FAILED: ' + detailOf(r)); return}
  const d = r.data || {};
  actMsg((d.flat ? 'Flat.' : 'NOT FLAT.') + (d.orphan_orders ? ' Orphan orders: ' + Number(d.orphan_orders) + '.' : '') + (Array.isArray(d.reasons) && d.reasons.length ? ' ' + d.reasons.map(String).join('; ') : ''));
  schedule(0);
};
$('reconcile-btn').onclick = async () => {
  const r = await post('/api/reconcile', {});
  if(!r.ok){actMsg('Reconcile FAILED: ' + detailOf(r)); return}
  const d = r.data || {};
  actMsg('Reconcile: ' + Object.entries(d).filter(([, v]) => ['string', 'number', 'boolean'].includes(typeof v)).map(([k, v]) => k + '=' + v).join(' ').slice(0, 160));
  schedule(0);
};
$('resolve-btn').onclick = async () => {
  if(!confirm('Only after you checked the TopstepX platform and saw NO such order. Resolve the not-found orders?')) return;
  const r = await post('/api/intent/resolve', {});
  actMsg(r.ok ? 'Resolved ' + Number((r.data || {}).resolved || 0) + ' not-found order(s).' : 'Resolve FAILED: ' + detailOf(r));
  schedule(0);
};
$('clear-halt-btn').onclick = async () => {const r = await post('/api/halt/clear', {}); actMsg(r.ok ? (r.data.halted ? 'Halt still set.' : 'Halt cleared.') : 'Clear FAILED: ' + detailOf(r)); S.placed = null; schedule(0)};
$('ai-toggle').onclick = () => {S.aiOpen = !S.aiOpen; renderAiPanel(); schedule(0)};

window.px = {snapshot(){return S.ai == null ? null : JSON.parse(JSON.stringify(S.ai))}};

/* ================= chart (KLineChart v10) ================= */
const err = e => {$('msg').textContent = (e && e.message) || String(e)};
let cur = {ticker: null, tick: 0.25, tickValue: null, name: '', dec: 2};
// The TraderMatrix chart-card palette (engine constants, not eyeballed): gold up candles, steel down candles with light wicks, the
// EMA-21 blue, the ink and text tones. Keep in step with the :root tokens in desk.css. Green/magenta stay for OUTCOMES (held/failed,
// stop lines, badges): a colour that means "price went up" must not also mean "good".
const C = {up: '#D6A73A', dn: '#7088AD', dnWick: '#C8D8EE', flat: '#6C7A92', grid: 'rgba(200,216,238,.07)', axis: '#1A2238', text: '#6C7A92',
           cyan: '#5FB3D9', amber: '#D6A73A', purple: '#B49AE0', ink: '#F2F5FA', light: '#C8D8EE', green: '#22c55e', mag: '#f43f5e'};
const EMA_COL = ['#E9EEF6', '#5FB3D9', '#B49AE0'];   // EMA 8 / 21 / 34: the card's SMA light, EMA-21 blue and magnet lilac; keep in step with --ema8/21/34 in desk.css
const RC = {BOUNCE: C.green, FAIL: C.mag, NEUTRAL: '#76839a', PENDING: C.cyan};
const MONO = '"JetBrains Mono", ui-monospace, monospace';
const axis = {axisLine: {color: C.axis}, tickLine: {color: C.axis}, tickText: {color: C.text, size: 10, family: MONO}};
const chs = {line: {color: '#3B4A68', style: 'dashed', dashedValue: [3, 3]}, text: {backgroundColor: '#0F1522', borderColor: '#2A3550', color: C.ink, family: MONO, size: 11}};
const legendTxt = {size: 11, family: MONO, color: C.text};
const STYLES = {
  grid: {horizontal: {color: C.grid, style: 'solid'}, vertical: {color: C.grid, style: 'solid'}},
  candle: {bar: {upColor: C.up, downColor: C.dn, noChangeColor: C.flat, upBorderColor: C.up, downBorderColor: C.dn,
                 noChangeBorderColor: C.flat, upWickColor: C.up, downWickColor: C.dnWick, noChangeWickColor: C.flat},
           priceMark: {high: {show: false}, low: {show: false},
                       last: {upColor: C.cyan, downColor: C.cyan, noChangeColor: C.cyan, line: {style: 'dashed', dashedValue: [2, 3]},
                              text: {color: '#04121c', family: MONO, weight: '600', borderRadius: 3}}},
           tooltip: {title: {...legendTxt, color: '#93a0b4'}, legend: {...legendTxt, template: [{title: 'O ', value: '{open}'}, {title: 'H ', value: '{high}'}, {title: 'L ', value: '{low}'}, {title: 'C ', value: '{close}'}]}}},
  indicator: {bars: [{upColor: 'rgba(214,167,58,.55)', downColor: 'rgba(112,136,173,.55)', noChangeColor: 'rgba(108,122,146,.45)'}],
              lines: ['#5FB3D9', '#B49AE0', '#C8D8EE', '#fb7185', '#8EA3C4'].map(color => ({color, size: 1})),
              tooltip: {title: legendTxt, legend: legendTxt}},
  xAxis: axis, yAxis: axis, separator: {color: C.axis}, crosshair: {horizontal: chs, vertical: chs},
};
const dimText = {color: C.ink, backgroundColor: 'rgba(10,14,23,.92)', borderColor: '#2a3550', borderSize: 1, size: 12, family: MONO, paddingLeft: 6, paddingRight: 6, paddingTop: 3, paddingBottom: 3, style: 'stroke_fill', borderRadius: 3};
klinecharts.registerOverlay({
  name: 'zoneBand', totalStep: 3, needDefaultPointFigure: true, needDefaultXAxisFigure: false, needDefaultYAxisFigure: true,
  createPointFigures: ({coordinates: cs, bounding, overlay}) => {
    if(cs.length < 2) return [];
    const top = Math.min(cs[0].y, cs[1].y), bot = Math.max(cs[0].y, cs[1].y), w = bounding.width;
    return [{type: 'polygon', attrs: {coordinates: [{x: 0, y: top}, {x: w, y: top}, {x: w, y: bot}, {x: 0, y: bot}]},
             styles: {style: 'fill', color: overlay.extendData?.color || 'rgba(168,85,247,.12)'}}];
  }});
klinecharts.registerOverlay({
  name: 'measure', totalStep: 3, needDefaultPointFigure: true, needDefaultXAxisFigure: true, needDefaultYAxisFigure: true,
  createPointFigures: ({coordinates: cs, overlay}) => {
    if(cs.length < 2) return [];
    const [a, b] = overlay.points; if(a?.value == null || b?.value == null) return [];
    const d = b.value - a.value, up = d >= 0, ticks = Math.round(d / cur.tick), bars = Math.abs((b.dataIndex ?? 0) - (a.dataIndex ?? 0));
    const lines = [`${up ? '+' : ''}${d.toFixed(2)}  (${up ? '+' : ''}${ticks} ticks)`,
      cur.tickValue != null && !streaming() ? `${up ? '+' : '-'}$${Math.abs(ticks * cur.tickValue).toFixed(2)} / contract` : null, `${bars} bars`].filter(Boolean);
    const col = up ? 'rgba(34,197,94,.13)' : 'rgba(244,63,94,.13)';
    const x0 = Math.min(cs[0].x, cs[1].x), x1 = Math.max(cs[0].x, cs[1].x), y0 = Math.min(cs[0].y, cs[1].y), y1 = Math.max(cs[0].y, cs[1].y);
    return [
      {type: 'polygon', attrs: {coordinates: [{x: x0, y: y0}, {x: x1, y: y0}, {x: x1, y: y1}, {x: x0, y: y1}]}, styles: {style: 'fill', color: col}},
      {type: 'line', attrs: {coordinates: [cs[0], cs[1]]}, styles: {style: 'dashed', color: up ? C.up : C.dn}},
      ...lines.map((t, i) => ({type: 'text', ignoreEvent: true, attrs: {x: (x0 + x1) / 2, y: Math.min(y1 + 16 + i * 22, y1 + 80), text: t, align: 'center', baseline: 'middle'}, styles: dimText})),
    ];
  }});
/* horizontal level line with a label at the right edge; used for GEX levels and the staged ticket */
klinecharts.registerOverlay({
  name: 'deskLevel', totalStep: 2, lock: true, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: true,
  createPointFigures: ({coordinates: cs, bounding, overlay}) => {
    if(!cs.length || !isFinite(cs[0].y)) return [];
    const ed = overlay.extendData || {}, y = cs[0].y;
    return [
      {type: 'line', attrs: {coordinates: [{x: 0, y}, {x: bounding.width, y}]}, styles: {style: ed.dash ? 'dashed' : 'solid', color: ed.color || C.purple, size: ed.size || 1, dashedValue: [6, 4]}},
      {type: 'text', ignoreEvent: true, attrs: {x: bounding.width - 6, y: y - 2, text: String(ed.label || ''), align: 'right', baseline: 'bottom'},
       styles: {color: ed.ink || ed.color || C.purple, size: 10, family: MONO, weight: '600', backgroundColor: 'rgba(7,9,13,.78)', borderSize: 0, borderRadius: 2,
                paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, style: 'fill'}},
    ];
  }});

/* reaction markers for the selected level: shape AND colour carry the result */
klinecharts.registerOverlay({
  name: 'reactMark', totalStep: 2, lock: true, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: false,
  createPointFigures: ({coordinates: cs, overlay}) => {
    if(!cs.length || !isFinite(cs[0].x) || !isFinite(cs[0].y)) return [];
    const {x, y} = cs[0], ed = overlay.extendData || {}, col = RC[ed.result] || C.flat;
    switch(ed.result){
      case 'BOUNCE': {
        const up = ed.kind === 'SUPPORT', s = 6;   // support bounce points up, resistance rejection points down
        const pts = up ? [{x, y: y - s}, {x: x + s, y: y + s * 0.8}, {x: x - s, y: y + s * 0.8}] : [{x, y: y + s}, {x: x + s, y: y - s * 0.8}, {x: x - s, y: y - s * 0.8}];
        return [{type: 'polygon', ignoreEvent: true, attrs: {coordinates: pts}, styles: {style: 'stroke_fill', color: col, borderColor: '#07090d', borderSize: 1}}];
      }
      case 'FAIL': return [
        {type: 'line', ignoreEvent: true, attrs: {coordinates: [{x: x - 4.5, y: y - 4.5}, {x: x + 4.5, y: y + 4.5}]}, styles: {style: 'solid', color: col, size: 2}},
        {type: 'line', ignoreEvent: true, attrs: {coordinates: [{x: x - 4.5, y: y + 4.5}, {x: x + 4.5, y: y - 4.5}]}, styles: {style: 'solid', color: col, size: 2}}];
      case 'PENDING': return [{type: 'circle', ignoreEvent: true, attrs: {x, y, r: 5}, styles: {style: 'stroke', color: 'transparent', borderColor: col, borderSize: 1.5}}];
      default: return [{type: 'circle', ignoreEvent: true, attrs: {x, y, r: 2.5}, styles: {style: 'fill', color: col, borderColor: col, borderSize: 0}}];
    }
  }});

const ctHour = new Intl.DateTimeFormat('en-US', {timeZone: 'America/Chicago', hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit'});
const sessKeyCache = new Map();
function sessKey(ts){
  const h = Math.floor(ts / 3600000); let k = sessKeyCache.get(h);
  if(k === undefined){
    const p = Object.fromEntries(ctHour.formatToParts(new Date(h * 3600000)).map(x => [x.type, x.value]));
    k = Date.UTC(+p.year, +p.month - 1, +p.day) / 86400000 + (+p.hour >= 17 ? 1 : 0);   // after 17:00 CT belongs to the next trade date
    sessKeyCache.set(h, k);
  }
  return k;
}
klinecharts.registerIndicator({
  name: 'VWAP', shortName: 'VWAP', series: 'price', precision: 2,
  styles: {lines: [{color: C.amber, size: 2, style: 'solid', smooth: false, dashedValue: [2, 2]}]},
  figures: [{key: 'vwap', title: 'VWAP: ', type: 'line'}],
  calc: list => {
    let pv = 0, v = 0, key = null;
    return list.map(b => {
      const k = sessKey(b.timestamp); if(k !== key){key = k; pv = v = 0}
      pv += (b.high + b.low + b.close) / 3 * (b.volume || 0); v += b.volume || 0;
      return v ? {vwap: pv / v} : {};
    });
  }});

/* ---- real buy/sell volume (GET /api/flow): green up = buyers hit the ask, red down = sellers hit the bid, line = delta.
   Built from the trade tape's aggressor side, so it only exists from when the desk was subscribed (FLOW.since). A bar
   that starts before that, or touches a reconnect gap, shows nothing rather than a wrong number. Sub-minute bars: none. ---- */
const FLOW = {since: null, m: new Map(), holes: [], cid: null};
const PER_MS = {minute: 60000, hour: 3600000, day: 86400000};
klinecharts.registerIndicator({
  name: 'BSV', shortName: 'BUY/SELL', precision: 0,
  figures: [
    {key: 'buy', title: 'B: ', type: 'bar', baseValue: 0, styles: () => ({color: C.up})},
    {key: 'sell', title: 'S: ', type: 'bar', baseValue: 0, styles: () => ({color: C.dn})},
    {key: 'delta', title: '\u0394: ', type: 'line'}],
  styles: {lines: [{color: C.ink, size: 1, style: 'solid', smooth: false, dashedValue: [2, 2]}]},
  calc: list => {
    const per = chart.getPeriod(), ms = per && PER_MS[per.type] ? PER_MS[per.type] * per.span : 0, now = Date.now();
    return list.map(b => {
      if(!ms || FLOW.since == null || FLOW.cid !== S.contractId || b.timestamp < FLOW.since) return {};
      if(FLOW.holes.some(h => h[0] < b.timestamp + ms && h[1] + 60000 > b.timestamp)) return {};   // trades were not being received: draw nothing
      let buy = 0, sell = 0;
      for(let t = Math.floor(b.timestamp / 60000) * 60000; t < b.timestamp + ms && t <= now; t += 60000){
        const r = FLOW.m.get(t); if(!r) continue;
        if(r[3]) return {};          // partial minute: not trustworthy, draw nothing
        buy += r[0]; sell += r[1];
      }
      return {buy, sell: -sell, delta: buy - sell};
    });
  }});
async function flowFetch(){
  const cid = S.contractId; if(!cid) return;
  try{
    const f = await j('/api/flow?contract=' + encodeURIComponent(cid));
    if(cid !== S.contractId || !Array.isArray(f.minutes)) return;
    FLOW.since = typeof f.since === 'number' ? f.since : null; FLOW.cid = cid;
    FLOW.holes = Array.isArray(f.holes) ? f.holes.filter(h => Array.isArray(h) && h.length === 2 && h.every(Number.isFinite)) : [];
    FLOW.m = new Map(f.minutes.map(r => [r[0], [r[1], r[2], r[3], r[4]]]));
    if(indOn.BSV) chart.overrideIndicator({name: 'BSV', extendData: Date.now()});
  }catch(e){err(e)}
}
setInterval(() => {if(document.visibilityState === 'visible' && indOn.BSV) flowFetch()}, 3000);

const chart = klinecharts.init('chart', {styles: STYLES, timezone: LS.get('px.tz', 'America/Chicago')});
let want = null, restoring = false;
chart.setSymbol({ticker: '-', pricePrecision: 2, volumePrecision: 0});
chart.setPeriod({span: 5, type: 'minute'});
chart.setOffsetRightDistance(40);

function unsub(){clearInterval(window._px); window._es?.close(); window._es = null; window._stop = null; setDot('')}
function setDot(c){$('dot').className = c}
let lastPx = null;
function setPrice(p, fromState){
  const e = $('price');
  const moved = lastPx != null && p !== lastPx;
  e.className = 'price' + (!moved ? (e.classList.contains('up') ? ' up' : e.classList.contains('dn') ? ' dn' : '') : (p > lastPx ? ' up' : ' dn'));
  if(moved){e.classList.add(p > lastPx ? 'flash-up' : 'flash-dn'); clearTimeout(setPrice.t); setPrice.t = setTimeout(() => e.classList.remove('flash-up', 'flash-dn'), 120)}
  e.textContent = Number(p).toFixed(cur.dec ?? 2); lastPx = p;
  if(!fromState) S.lastTickAt = Date.now();
}

chart.setDataLoader({
  getBars: async ({type, timestamp, symbol, period, callback}) => {
    const stale = () => !want || symbol.ticker !== want.ticker || period.type !== want.type || period.span !== want.span;
    if(stale()){callback([], false); return}
    try{
      const end = type === 'forward' ? `&end=${timestamp - 1}` : '';
      const bars = await j(`/bars?contract=${symbol.ticker}&unit=${UNIT[period.type]}&n=${period.span}&limit=1000${end}`);
      if(stale()){callback([], false); return}
      $('msg').textContent = '';
      callback(bars, {forward: bars.length > 0, backward: false});
      if(type === 'init') setTimeout(() => {restoreOverlays(); drawDesk(true); drawReact(true); drawRespect(true); I.hsig = ''; indSync()}, 0);
    }catch(e){err(e); callback([], false)}
  },
  subscribeBar: async ({symbol, period, callback}) => {
    unsub(); const stop = window._stop = {};
    if(!/^CON\./.test(symbol.ticker)) return;   // placeholder symbol before contracts load
    const unit = UNIT[period.type], qs = `contract=${symbol.ticker}&unit=${unit}&n=${period.span}`;
    if(unit >= 4){   // day+ bars are session-bucketed server-side; keep polling
      window._px = setInterval(async () => {try{const bs = await j(`/bars?${qs}&limit=3`); if(stop === window._stop) bs.forEach(callback)}catch(e){err(e)}}, 3000);
      return;
    }
    try{
      const {DataAggregator} = await import('/static/aggregator.js');
      const agg = new DataAggregator(); let ready = false;
      const reseed = async () => {
        ready = false; agg.setPeriod({type: period.type, span: period.span});
        const [last] = (await j(`/bars?${qs}&limit=1`)).slice(-1);
        if(stop !== window._stop) return;
        if(last){agg.setBaseData({...last, turnover: last.close * last.volume}); callback(last)}
        ready = true;
      };
      const es = window._es = new EventSource(`/ticks?contract=${symbol.ticker}`);
      es.onmessage = e => {
        if(!ready) return;
        const t = JSON.parse(e.data); setDot('live'); setPrice(t.p);
        try{callback(agg.add({timestamp: t.t, price: t.p, volume: t.v}).current)}
        catch(x){if(!(x instanceof RangeError)) err(x)}
      };
      es.addEventListener('resync', () => {$('msg').textContent = ''; reseed().catch(err)});
      es.addEventListener('down', () => {setDot('warn'); $('msg').textContent = 'tick feed reconnecting...'});
      es.onopen = () => {setDot('warn'); $('msg').textContent = ''; reseed().catch(err)};
      es.onerror = () => {setDot('warn'); $('msg').textContent = 'tick feed reconnecting...'; if(es.readyState === 2) setTimeout(() => {if(window._es === es) load()}, 3000)};   // CLOSED = the browser will not retry
    }catch(e){err(e)}
  },
  unsubscribeBar: () => unsub(),
});

/* ---- desk lines: GEX levels + staged ticket entry/stop ---- */
let deskSig = '';
const GEX_COL = {flip: '#c084fc', pin: '#a855f7', wall_above: '#a855f7', wall_below: '#a855f7'};   // reference levels are purple; the label names which
function deskLines(){
  const out = [];
  const g = S.ai && S.ai.gex;
  if(g && Array.isArray(g.levels)) for(const lv of g.levels){
    if(typeof lv.price !== 'number') continue;
    const nm = String(lv.name || 'gex').slice(0, 24);
    out.push({price: lv.price, label: 'GEX ' + nm + ' ' + lv.price, color: GEX_COL[nm] || C.purple, dash: true});
  }
  const cl = CALL.live;       // the analyst's own entry, stop and first target, while it is recent and this is its chart
  if(cl && cl.root === curRoot() && cl.age0 + (performance.now() - cl.at) / 1000 < 5400){
    for(const [nm, p, col] of [['E', cl.entry, '#fbbf24'], ['SL', cl.stop, '#fb7185'], ['T1', cl.tp1, '#4ade80']])
      if(typeof p === 'number') out.push({price: p, label: 'CALLOUT ' + nm + ' ' + p, color: col, ink: col, dash: true, size: 2});
  }
  const t = S.ticket;
  if(t && t.ticket_id && !t.consumed && !t.gone && t.contract === S.contractId && t.readback){
    const e = Number(t.readback.entry_ref), s = Number(t.readback.stop_price);
    if(isFinite(e)) out.push({price: e, label: 'ENTRY ' + e, color: C.cyan, ink: '#7dd3fc', dash: false, size: 1});
    if(isFinite(s)) out.push({price: s, label: 'STOP ' + s, color: C.mag, ink: '#fb7185', dash: false, size: 1});
  }else{
    const tr = S.ai && S.ai.trade;   // live bracket on the contract we placed it on
    if(tr && S.placed && S.placed.contract === S.contractId){
      if(num(tr.entry_ref)) out.push({price: tr.entry_ref, label: 'ENTRY ' + tr.entry_ref + ' ' + (tr.state === 'PLACED' ? ((tr.filled || 0) > 0 ? 'FILLED' : tr.acked ? 'ACCEPTED' : 'PLACED') : tr.state), color: C.cyan, ink: '#7dd3fc', dash: false, size: 1});
      const sp = S.ai.manage && num(S.ai.manage.stop) ? S.ai.manage.stop : tr.stop_price;   // manage.stop follows a moved stop
      if(num(sp)) out.push({price: sp, label: 'STOP ' + sp + ' ' + (tr.stop_state || 'PENDING'), color: C.mag, ink: '#fb7185', dash: tr.stop_state !== 'ACKED' && tr.stop_state !== 'ATOMIC', size: 1});
    }else if(S.ai && S.ai.manage && num(S.ai.manage.stop)){   // position not placed from this page: still show where its stop is
      out.push({price: S.ai.manage.stop, label: 'STOP ' + S.ai.manage.stop, color: C.mag, ink: '#fb7185', dash: false, size: 1});
    }
  }
  return out;
}
function drawDesk(force){
  const lines = deskLines(), sig = JSON.stringify(lines);
  if(sig === deskSig && !force) return;
  const last = chart.getDataList().slice(-1)[0];
  if(!last && lines.length) return;                       // retry once bars exist
  deskSig = sig;
  try{
    chart.removeOverlay({groupId: 'desk'});
    for(const l of lines) chart.createOverlay({name: 'deskLevel', groupId: 'desk', lock: true, points: [{timestamp: last.timestamp, value: l.price}], extendData: l});
  }catch(e){err(e)}
}
function syncDesk(){drawDesk(false)}

/* ---- level reaction markers (group 'react': never saved with the user's drawings) ---- */
let reactSig = '';
function drawReact(force){
  const lv = (S.ai && Array.isArray(S.ai.levels) ? S.ai.levels : []).find(x => x && x.name === S.lvl);
  const evs = lv && Array.isArray(lv.events) ? lv.events.filter(e => e && num(e.ms) && num(e.price) && RESULTS.has(e.result)) : [];
  const sig = S.lvl + '|' + cur.ticker + '|' + JSON.stringify(evs);
  if(sig === reactSig && !force) return;
  if(evs.length && !chart.getDataList().length) return;   // retry once bars exist
  reactSig = sig;
  try{
    chart.removeOverlay({groupId: 'react'});
    for(const e of evs) chart.createOverlay({name: 'reactMark', groupId: 'react', lock: true, points: [{timestamp: e.ms, value: e.price}], extendData: {result: e.result, kind: e.kind}});
  }catch(x){err(x)}
}

/* ---- respected levels: per timeframe, the level price has actually been respecting (GET /api/respect). Prices and enums only. ---- */
const RSP_TFS = ['5m', '15m', '1h', '4h', '1d'];
const RSP_TF_OF = {'minute,5': '5m', 'minute,15': '15m', 'hour,1': '1h', 'hour,4': '4h', 'day,1': '1d'};
/* Okabe-Ito based: colorblind-safe on the dark theme; dash pattern is a second channel (solid, long, short, dot-dash, dotted) */
const RSP_STYLE = {'5m': {c: '#56b4e9', dv: [1, 0]}, '15m': {c: '#f0e442', dv: [10, 4]}, '1h': {c: '#cc79a7', dv: [4, 3]},
                   '4h': {c: '#ff8a3d', dv: [10, 3, 2, 3]}, '1d': {c: '#e6ebf2', dv: [2, 3]}};
const RSP_POLL_MS = 60000;
const R = {on: LS.get('px.respect', true) !== false, data: null, status: 'loading', code: '', pick: {}, sig: '', seq: 0, timer: null};   // pick: tf -> level id, or null for "off"; session only
klinecharts.registerOverlay({
  name: 'respectLevel', totalStep: 2, lock: true, needDefaultPointFigure: false, needDefaultXAxisFigure: false, needDefaultYAxisFigure: true,
  createPointFigures: ({coordinates: cs, bounding, overlay}) => {
    if(!cs.length || !isFinite(cs[0].y)) return [];
    const ed = overlay.extendData || {}, y = cs[0].y, a = ed.stable ? 1 : .55;
    return [
      {type: 'line', attrs: {coordinates: [{x: 0, y}, {x: bounding.width, y}]}, styles: {style: ed.stable ? 'solid' : 'dashed', color: ed.color, size: ed.stable ? 1.5 : 1, dashedValue: ed.stable ? [1, 0] : ed.dv}},
      {type: 'text', ignoreEvent: true, attrs: {x: y < 80 ? bounding.width - 6 : 6, y: y - 2, text: String(ed.label || ''), align: y < 80 ? 'right' : 'left', baseline: 'bottom'},
       styles: {color: ed.color, size: 10, family: MONO, weight: ed.stable ? '700' : '500', backgroundColor: `rgba(7,9,13,${.78 * a + .1})`, borderSize: 0, borderRadius: 2,
                paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2, style: 'fill'}},
    ];
  }});
const rspNum = v => typeof v === 'number' && isFinite(v);
const rspName = v => String(v == null ? '' : v).replace(/[^\w .\-\/+%()]/g, '').slice(0, 24) || 'level';
const rspLevels = tf => {
  const t = R.data && R.data.tfs && R.data.tfs[tf];
  return t && Array.isArray(t.levels) ? t.levels.filter(l => l && typeof l.id === 'string' && l.id.length <= 40) : [];
};
const rspOk = l => !!l && (l.verdict === 'ABOVE_RANDOM' || l.verdict === 'RESPECTED');   // ABOVE_RANDOM is the scanner's respected state
function rspDrawn(tf){   // the level drawn for this timeframe: session pick, else the best RESPECTED (stable preferred)
  const ok = rspLevels(tf).filter(l => rspOk(l) && rspNum(l.price));
  if(Object.prototype.hasOwnProperty.call(R.pick, tf)) return R.pick[tf] === null ? null : ok.find(l => l.id === R.pick[tf]) || null;
  return ok.find(l => l.stable === true) || ok[0] || null;
}
function respectLines(){
  const out = [];
  if(!R.on || !R.data || R.data.contract !== S.contractId) return out;
  for(const tf of RSP_TFS){
    const l = rspDrawn(tf); if(!l) continue;
    const st = RSP_STYLE[tf];
    out.push({price: l.price, color: st.c, dv: st.dv, stable: l.stable === true,
      label: `${tf} ${rspName(l.label)}${rspNum(l.held) && rspNum(l.tests) ? ` ${l.held}/${l.tests}` : ''}`});
  }
  return out;
}
function drawRespect(force){
  const lines = respectLines(), sig = JSON.stringify(lines);
  if(sig === R.sig && !force) return;
  const last = chart.getDataList().slice(-1)[0];
  if(!last && lines.length) return;   // retry once bars exist
  R.sig = sig;
  try{
    chart.removeOverlay({groupId: 'respect'});
    for(const l of lines) chart.createOverlay({name: 'respectLevel', groupId: 'respect', lock: true, points: [{timestamp: last.timestamp, value: l.price}], extendData: l});
  }catch(e){err(e)}
}
function renderRespect(){
  const list = $('rsp-list'), cvt = RSP_TF_OF[S.tf] || null;
  const tg = $('rsp-toggle'); tg.setAttribute('aria-pressed', String(R.on));
  const ok = R.status === 'ok' && R.data;
  $('rsp-state').textContent = R.status === 'loading' ? 'loading...' : R.status === 'error' ? 'unavailable' + (R.code ? ' (' + R.code + ')' : '') : '';
  list.replaceChildren();
  for(const tf of RSP_TFS){
    const t = ok && R.data.tfs ? R.data.tfs[tf] : null, st = RSP_STYLE[tf];
    const box = mk('div', 'rsptf' + (tf === cvt ? ' cur' : '')); box.dataset.tf = tf; box.style.setProperty('--tfc', st.c);
    if(tf === cvt) box.setAttribute('aria-current', 'true');
    const th = mk('div', 'th'); th.append(mk('b', '', tf));
    const lv = ok && t ? rspLevels(tf).slice(0, 3) : [];
    if(ok && t){
      th.append(mk('span', '', rspNum(t.bars) ? t.bars + ' bars' : ''));
      {const te = mk('span', 'tested', rspNum(t.tested) ? 'tested ' + t.tested : ''); if(rspNum(t.tested)) te.title = 'tested ' + t.tested + ' candidate levels' + (rspNum(t.family) && t.family > t.tested ? ' (FDR over ' + t.family + ')' : ''); th.append(te)}
    }
    box.append(th);
    if(!ok){box.append(mk('p', 'none', R.status === 'loading' ? 'loading...' : 'respect data unavailable'))}
    else if(!t){box.append(mk('p', 'none', 'no data for this timeframe'))}
    else{
      if(!lv.some(rspOk)) box.append(mk('p', 'none', 'nothing respected yet' + (typeof t.note === 'string' && t.note ? ' (' + t.note.slice(0, 80) + ')' : '')));
      const drawn = rspDrawn(tf);
      for(const l of lv){
        const resp = rspOk(l);
        const row = mk('button', 'rsprow'); row.type = 'button'; row.dataset.tf = tf; row.dataset.id = l.id; row.disabled = !resp;
        row.setAttribute('aria-pressed', String(!!drawn && drawn.id === l.id && R.on));
        row.title = resp ? 'Show or hide this level on the chart (this session)' : 'Not respected: never drawn';
        const hr = rspNum(l.hold_rate) ? Math.round(l.hold_rate) + '%' : 'NA', pr = rspNum(l.placebo_rate) ? Math.round(l.placebo_rate) + '%' : 'NA';
        const vk = resp ? 'RESPECTED' : Object.prototype.hasOwnProperty.call(VERDICT, l.verdict) ? l.verdict : null;
        row.append(mk('span', 'rn', rspName(l.label)),
          mk('span', '', `held ${rspNum(l.held) ? l.held : 'NA'}/${rspNum(l.tests) ? l.tests : 'NA'}`),
          mk('span', '', `hold ${hr} vs placebo ${pr}` + (rspNum(l.p_value) ? ` p=${l.p_value < 0.001 ? '<0.001' : l.p_value.toFixed(3)}` : '')),
          mk('span', 'vd vd-' + (vk ? vk.toLowerCase() : 'na'), [vk === 'RESPECTED' ? 'Respected' : vk ? VERDICT[vk] : 'NA',   // ONE verdict: respected / same as random, then stability and sample size in the same muted text
            l.stable === true ? 'stable' : l.stable === false ? 'not stable' : '', rspNum(l.tests) && l.tests < 30 ? 'small sample' : ''].filter(Boolean).join(' \u00b7 ')));
        box.append(row);
      }
    }
    list.append(box);
  }
}
async function respectFetch(){
  const cid = S.contractId; if(!cid || !R.on) return;
  const my = ++R.seq;
  try{
    const r = await fetch('/api/respect?contract=' + encodeURIComponent(cid), {headers: {accept: 'application/json'}});
    let body = null; try{body = await r.json()}catch{}
    if(my !== R.seq) return;   // a newer request or a contract change superseded this one
    if(!r.ok || !body || body.v !== 1 || typeof body.tfs !== 'object' || body.tfs === null){
      const c = body && typeof body.error === 'string' && /^[a-z0-9_]{1,32}$/.test(body.error) ? body.error : String(r.status);
      throw Object.assign(new Error('respect'), {code: c});
    }
    if(body.contract !== cid) return;
    R.data = body; R.status = 'ok'; R.code = '';
  }catch(e){
    if(my !== R.seq) return;
    R.status = 'error'; R.code = e && e.code ? e.code : 'network'; R.data = null;
  }
  renderRespect(); drawRespect(false); mnSync(); indSync();
}
function respectLoad(newContract){   // on load/contract change; a timeframe change only re-highlights
  if(newContract){R.data = null; R.status = 'loading'; R.code = ''; R.pick = {}; R.seq++; R.sig = ''; try{chart.removeOverlay({groupId: 'respect'})}catch(e){err(e)}}
  renderRespect();
  if(newContract){respectFetch(); mnLoad()} else mnSync();
}
function respectSetOn(on){
  R.on = on; LS.set('px.respect', on);
  if(on){R.status = R.data ? R.status : 'loading'; renderRespect(); respectFetch()}
  else{R.seq++; renderRespect(); drawRespect(true); mnSync()}
}
$('rsp-toggle').onclick = () => respectSetOn(!R.on);
$('rsp-list').addEventListener('click', e => {
  const b = e.target.closest('button[data-id]'); if(!b || b.disabled) return;
  const tf = b.dataset.tf, d = rspDrawn(tf);
  R.pick[tf] = d && d.id === b.dataset.id ? null : b.dataset.id;
  if(!R.on){R.on = true; LS.set('px.respect', true); respectFetch()}
  renderRespect(); drawRespect(false); mnSync();
});
R.timer = setInterval(() => {if(document.visibilityState === 'visible') respectFetch()}, RSP_POLL_MS);

/* ---- analyst callouts: GET /api/callouts. Allowlisted fields only; every server string goes through textContent. ---- */
const FD_POLL_MS = 20000, FD_AGE = s => s < 90 ? s + 's' : s < 5400 ? Math.round(s / 60) + 'm' : (s / 3600).toFixed(1) + 'h';
/* ---- analyst ENTRY alert: a fresh futures entry posts -> flash a banner, draw its levels, and (only if you are flat) switch the chart to
   that contract. Display only: it never stages or places anything. Seen entries are remembered so a reload does not re-flash them. ---- */
const CALL = {live: null, seen: new Set(LS.get('px.callSeen', [])), hideT: 0, titleT: 0, stage: false};   // stage: server says PX_FEED_STAGE=1
const TO_MICRO = {NQ: 'MNQ', ES: 'MES', GC: 'MGC', CL: 'MCL', RTY: 'M2K', YM: 'MYM'};      // this desk trades micros: show the micro chart
const rootOf = s => TO_MICRO[s] || s;
const curRoot = () => String(S.contractId || '').split('.')[3] || '';
function isFlat(){
  const a = S.ai, t = S.ticket;
  return !!a && Array.isArray(a.positions) && !a.positions.length && Array.isArray(a.orders) && !a.orders.length
    && !(t && t.ticket_id && !t.consumed && !t.gone);
}
function calloutAlert(i){
  const root = rootOf(i.symbol), flat = isFlat(), switched = flat && curRoot() !== root;
  CALL.live = {symbol: i.symbol, root, side: i.side, entry: i.entry, stop: i.stop, tp1: i.tp1, analyst: i.analyst, age0: i.age_s, at: performance.now()};
  if(switched){$('q').value = root; search()}
  const box = $('callout-alert'); box.replaceChildren();
  const px = [['E', i.entry], ['SL', i.stop], ['T1', i.tp1]].filter(p => typeof p[1] === 'number').map(p => p[0] + ' ' + p[1]).join('   ');
  box.append(mk('b', '', 'ANALYST ENTRY'), mk('span', 'ca-main', [root, i.side || '', px].filter(Boolean).join('  ')),
    mk('span', 'ca-sub', (i.analyst || 'analyst').slice(0, 24) + ' ' + FD_AGE(i.age_s) + ' ago' + (switched ? ' · chart switched to ' + root : flat ? '' : ' · you have an open trade: chart not switched')));
  if(flat && CALL.stage && typeof i.entry === 'number' && typeof i.stop === 'number'){   // one-click staging only with PX_FEED_STAGE=1: an unmeasured signal should not be the easiest entry on the screen
    const sd = i.side ? String(i.side).toUpperCase() : (i.stop > i.entry ? 'SHORT' : 'LONG');
    const go = mk('button', 'ca-go', 'STAGE ' + sd + ' \u00B7 stop ' + i.stop);
    go.onclick = async ev => {ev.stopPropagation(); calloutDismiss(); await stageCallout()};
    box.append(go);
  }
  box.hidden = false; box.className = 'callout-alert flash'; box.onclick = calloutDismiss;
  clearTimeout(CALL.hideT); CALL.hideT = setTimeout(() => {box.classList.remove('flash')}, 20000);
  clearInterval(CALL.titleT); let n = 0; const base = 'PXDESK';
  CALL.titleT = setInterval(() => {document.title = n++ % 2 ? base : '\u25B6 ENTRY ' + root; if(n > 40){clearInterval(CALL.titleT); document.title = base}}, 700);
  if(typeof drawDesk === 'function') drawDesk(true);
}
async function stageCallout(){
  const cl = CALL.live;
  if(!cl || cl.root !== curRoot() || !isFlat() || typeof cl.entry !== 'number' || typeof cl.stop !== 'number'){$('msg').textContent = 'Callout cannot be staged now (not flat, or this is not its chart).'; return}
  const side = cl.side ? String(cl.side).toLowerCase() : (cl.stop > cl.entry ? 'short' : 'long');     // stop above entry = short; the ticket readback shows the side in words
  const cid = await sizeContract();
  S.side = side; syncSizeInputs(); schedule(0);        // the LONG/SHORT chip must show the side we are staging
  await doStage({contract: cid, side, stop_price: cl.stop, risk_pct: S.riskPct, order_type: 'market', source: 'callout'}, cid);   // the server sizes it and runs every check; source is audited
}
function calloutDismiss(){const b = $('callout-alert'); b.hidden = true; clearInterval(CALL.titleT); document.title = 'PXDESK'}
function calloutWatch(items){
  if(!S.ai) return;       // wait for the account state: 'are we flat?' must be known, never guessed, before we switch the chart or offer to stage
  const fresh = items.filter(i => i.event === 'ENTRY' && i.symbol && typeof i.entry === 'number' && i.age_s <= 900).reverse();   // posted in the last 15 min, oldest first
  for(const i of fresh){
    const k = [i.ts, i.symbol, i.entry].join('|');
    if(CALL.seen.has(k)) continue;
    CALL.seen.add(k); LS.set('px.callSeen', [...CALL.seen].slice(-30));
    calloutAlert(i);
  }
}
async function feedFetch(){
  const st = $('fd-state'), box = $('fd-list');
  try{
    const f = await j('/api/callouts');
    box.replaceChildren();
    if(f.status === 'OFF'){st.textContent = 'off'; box.append(mk('p', 'none', 'feed not configured (PX_FEED_URL)')); return}
    if(f.status !== 'OK'){st.textContent = 'error'; box.append(mk('p', 'none', 'feed unavailable' + (typeof f.code === 'string' ? ' (' + f.code.slice(0, 20) + ')' : ''))); return}
    CALL.stage = f.stage === true;
    st.textContent = f.items.length ? 'newest ' + FD_AGE(f.items[0].age_s) + ' ago' : 'none';
    if(!f.items.length){box.append(mk('p', 'none', 'no structured callouts in the window')); return}
    calloutWatch(f.items);
    for(const i of f.items){
      const row = mk('div', 'fdrow');
      row.append(mk('span', 'age', FD_AGE(i.age_s)), mk('span', '', (i.analyst || i.channel || '?').slice(0, 24)), mk('span', 'ev', String(i.event).replace('_', ' ')));
      if(i.symbol) row.append(mk('span', '', i.symbol));
      if(i.side) row.append(mk('span', i.side === 'LONG' ? 'long' : 'short', i.side));
      for(const [k, lab] of [['entry', 'E'], ['stop', 'SL'], ['tp1', 'T1'], ['tp2', 'T2'], ['tp3', 'T3']]) if(typeof i[k] === 'number') row.append(mk('span', '', lab + ' ' + i[k]));
      if(typeof i.points === 'number') row.append(mk('span', '', (i.points > 0 ? '+' : '') + i.points + ' pts'));
      if(i.has_image) row.append(mk('span', 'age', '[chart]'));
      box.append(row);
    }
  }catch(e){st.textContent = 'error'; err(e)}
}
feedFetch();
setInterval(() => {if(document.visibilityState === 'visible') feedFetch()}, FD_POLL_MS);

/* ---- timeframe mini charts: plain canvas candles, last ~60 bars, EMA 8/21/34, the respected level. Prices only. ---- */
const MN = {open: LS.get('px.minis', true) !== false, tfs: ['15m', '1h'], unit: {'5m': [2, 5, 300000], '15m': [2, 15, 900000], '1h': [3, 1, 3600000]},
  bars: {}, seq: {}, at: {}, cv: {}, box: {}, px: {}};
const MN_SHOW = 60, MN_FETCH = 250, MN_MIN_MS = 30000, MN_PER = [8, 13, 21, 34, 55, 89];
const MN_COL = [EMA_COL[0], '#7fb08a', EMA_COL[1], EMA_COL[2], '#D6A73A', '#8EA3C4'];   // 8/21/34 as on the main chart; 13/55/89 muted
const MN_DASH = [[], [], [], [], [4, 3], [4, 3]];
function mnEma(closes, p){   // same recurrence as the main chart's EMA indicator (seeded with the first close)
  const out = []; let prev = null;
  for(const c of closes){prev = prev == null ? c : (2 * c + (p - 1) * prev) / (p + 1); out.push(prev)}
  return out;
}
function mnDraw(tf){
  const cv = MN.cv[tf]; if(!cv) return;
  const w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  if(!w || !h) return;
  if(cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)){cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr)}
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const all = MN.bars[tf] || [];
  if(!all.length){g.fillStyle = C.text; g.font = `11px ${MONO}`; g.fillText(MN.px[tf] === 'err' ? 'unavailable' : 'loading...', 6, 16); return}
  const closes = all.map(b => b.close), emas = MN_PER.map(p => mnEma(closes, p));
  const from = Math.max(0, all.length - MN_SHOW), bars = all.slice(from);
  const rl = R.on && R.data && R.data.contract === S.contractId ? rspDrawn(tf) : null;
  let lo = Infinity, hi = -Infinity;
  for(const b of bars){lo = Math.min(lo, b.low); hi = Math.max(hi, b.high)}
  if(rl && rspNum(rl.price) && rl.price > lo - (hi - lo) * .25 && rl.price < hi + (hi - lo) * .25){lo = Math.min(lo, rl.price); hi = Math.max(hi, rl.price)}
  const pad = (hi - lo) * .06 || 1; lo -= pad; hi += pad;
  const axW = 44, pw = w - axW, step = pw / bars.length, y = v => 4 + (hi - v) / (hi - lo) * (h - 8);
  const dec = cur.dec ?? 2;
  g.strokeStyle = C.grid; g.lineWidth = 1; g.fillStyle = C.text; g.font = `9.5px ${MONO}`; g.textBaseline = 'middle';
  for(const f of [.15, .5, .85]){const v = lo + (hi - lo) * f, yy = Math.round(y(v)) + .5; g.beginPath(); g.moveTo(0, yy); g.lineTo(pw, yy); g.stroke(); g.fillText(v.toFixed(dec), pw + 3, yy)}
  bars.forEach((b, i) => {
    const x = i * step + step / 2, up = b.close >= b.open, col = up ? C.up : C.dn, bw = Math.max(1, step * .62);
    g.strokeStyle = g.fillStyle = col; g.lineWidth = 1;
    g.beginPath(); g.moveTo(Math.round(x) + .5, y(b.high)); g.lineTo(Math.round(x) + .5, y(b.low)); g.stroke();
    const t = y(Math.max(b.open, b.close)), bh = Math.max(1, Math.abs(y(b.open) - y(b.close)));
    g.fillRect(x - bw / 2, t, bw, bh);
  });
  emas.forEach((e, k) => {
    g.strokeStyle = MN_COL[k]; g.lineWidth = 1; g.setLineDash(MN_DASH[k]); g.beginPath();
    e.slice(from).forEach((v, i) => {const x = i * step + step / 2; i ? g.lineTo(x, y(v)) : g.moveTo(x, y(v))});
    g.stroke(); g.setLineDash([]);
  });
  if(rl && rspNum(rl.price)){
    const st = RSP_STYLE[tf], yy = y(rl.price);
    g.strokeStyle = st.c; g.lineWidth = rl.stable === true ? 1.5 : 1; g.setLineDash(rl.stable === true ? [] : st.dv); g.beginPath(); g.moveTo(0, yy); g.lineTo(pw, yy); g.stroke(); g.setLineDash([]);
    const txt = rspName(rl.label) + (rspNum(rl.held) && rspNum(rl.tests) ? ` ${rl.held}/${rl.tests}` : '');
    g.font = `600 9.5px ${MONO}`; g.textBaseline = 'bottom'; const tw = g.measureText(txt).width;
    g.fillStyle = 'rgba(7,9,13,.78)'; g.fillRect(4, yy - 14, tw + 6, 13); g.fillStyle = st.c; g.fillText(txt, 7, yy - 3);
  }
}
function mnBias(tf){   // client-side read of the timeframe's own bars (full 8/13/21/34/55/89 ribbon); text + glyph, never colour alone
  const all = MN.bars[tf] || [], r = {stack: 'NA', pos: 'NA', bias: 'NA', ribbon: 'NA', note: ''};
  if(all.length >= 89){
    const closes = all.map(b => b.close), e = MN_PER.map(p => mnEma(closes, p).slice(-1)[0]), c = closes[closes.length - 1];
    let n = 0, broke = null;
    for(let i = 0; i < 5; i++){if(e[i] > e[i + 1]) n++; else if(broke == null) broke = MN_PER[i] + '/' + MN_PER[i + 1]}
    r.ribbon = String(n);
    r.stack = n === 5 ? 'BULL' : n === 0 ? 'BEAR' : 'MIXED';
    r.note = `ribbon ${n}/5 ${r.stack}` + (r.stack === 'MIXED' ? `, broke at ${broke}` : '');
    const ab = MN_PER.filter((p, i) => c > e[i]);
    r.pos = ab.length === 6 ? 'above all' : ab.length === 0 ? 'below all' : ab.length === 1 ? `above ${ab[0]} only` : 'above ' + ab.join(', ');
    r.bias = n === 5 && ab.length === 6 ? 'BULLISH' : n === 0 && ab.length === 0 ? 'BEARISH' : 'NEUTRAL';
  }
  return r;
}
const MN_GLYPH = {BULLISH: '▲', BEARISH: '▼', NEUTRAL: '◆', BULL: '▲', BEAR: '▼', MIXED: '◆'};
function mnMeta(tf){
  const box = MN.box[tf]; if(!box) return;
  const all = MN.bars[tf] || [], last = all[all.length - 1];
  box.querySelector('.mp').textContent = last && rspNum(last.close) ? last.close.toFixed(cur.dec ?? 2) : '';
  const bz = mnBias(tf), bl = box.querySelector('.mb');
  bl.dataset.stack = bz.stack; bl.dataset.ribbon = bz.ribbon; bl.dataset.pos = bz.pos; bl.dataset.bias = bz.bias;
  const kids = bl.children;
  kids[0].textContent = bz.bias === 'NA' ? 'NA' : MN_GLYPH[bz.bias] + ' ' + bz.bias;
  kids[0].className = 'bz bz-' + bz.bias.toLowerCase();
  kids[1].textContent = bz.stack === 'NA' ? '' : MN_GLYPH[bz.stack] + ' ' + bz.note;
  kids[2].textContent = bz.pos === 'NA' ? '' : bz.pos;
  box.classList.toggle('cur', RSP_TF_OF[S.tf] === tf);
  if(RSP_TF_OF[S.tf] === tf) box.setAttribute('aria-current', 'true'); else box.removeAttribute('aria-current');
}
function mnSync(){for(const tf of MN.tfs){mnMeta(tf); if(MN.open) mnDraw(tf)}}
async function mnFetch(tf){
  const cid = S.contractId; if(!cid || !MN.open) return;
  const [unit, n] = MN.unit[tf], my = MN.seq[tf] = (MN.seq[tf] || 0) + 1;
  MN.at[tf] = Date.now();
  try{
    const bs = await j(`/bars?contract=${encodeURIComponent(cid)}&unit=${unit}&n=${n}&limit=${MN_FETCH}`);
    if(my !== MN.seq[tf] || cid !== S.contractId) return;
    MN.bars[tf] = (Array.isArray(bs) ? bs : []).filter(b => b && [b.timestamp, b.open, b.high, b.low, b.close].every(rspNum)).slice(-MN_FETCH);
    MN.px[tf] = 'ok';
  }catch(e){
    if(my !== MN.seq[tf]) return;
    MN.px[tf] = 'err';
  }
  mnMeta(tf); mnDraw(tf);
  $('mn-state').textContent = MN.tfs.some(t => MN.px[t] === 'err') ? 'some feeds unavailable' : '';
}
function mnLoad(){   // contract change: drop old bars, one fetch per timeframe
  for(const tf of MN.tfs){MN.seq[tf] = (MN.seq[tf] || 0) + 1; MN.bars[tf] = []; MN.px[tf] = ''; MN.at[tf] = 0}
  mnSync(); if(MN.open) for(const tf of MN.tfs) mnFetch(tf);
}
function mnTick(){   // only when a new bar of that timeframe should exist, never faster than MN_MIN_MS
  if(!MN.open || document.visibilityState !== 'visible') return;
  const now = Date.now();
  for(const tf of MN.tfs){
    const all = MN.bars[tf] || [], last = all[all.length - 1];
    if(now - (MN.at[tf] || 0) < MN_MIN_MS) continue;
    if(!last || MN.px[tf] === 'err' || now >= last.timestamp + MN.unit[tf][2]) mnFetch(tf);
  }
}
for(const tf of MN.tfs){
  const box = mk('div', 'mn'); box.dataset.tf = tf; box.style.setProperty('--tfc', RSP_STYLE[tf].c);
  const mh = mk('div', 'mh'); mh.append(mk('b', '', tf), mk('span', 'mp', ''));
  const cv = mk('canvas'); cv.setAttribute('role', 'img'); cv.setAttribute('aria-label', tf + ' candles with EMA 8, 13, 21, 34, 55, 89');
  const mb = mk('div', 'mb'); mb.append(mk('span', 'bz bz-na', 'NA'), mk('span', 'bs', ''), mk('span', 'bp', ''));
  box.append(mh, cv, mb); $('mn-body').append(box); MN.cv[tf] = cv; MN.box[tf] = box;
}
function mnApplyOpen(){
  $('mn-body').hidden = !MN.open; $('mn-toggle').textContent = MN.open ? 'Hide' : 'Show'; $('mn-toggle').setAttribute('aria-expanded', String(MN.open));
}
$('mn-toggle').onclick = () => {
  MN.open = !MN.open; LS.set('px.minis', MN.open); mnApplyOpen();
  if(MN.open){mnSync(); for(const tf of MN.tfs) if(!(MN.bars[tf] || []).length || Date.now() - (MN.at[tf] || 0) >= MN_MIN_MS) mnFetch(tf)}
};
mnApplyOpen();
new ResizeObserver(() => {if(MN.open) for(const tf of MN.tfs) mnDraw(tf)}).observe($('mn-body'));
setInterval(mnTick, MN_MIN_MS);

/* ---- tick charts: GET /api/tickbars. Bars of N trade prints from the live tape; candles plus a buy-minus-sell strip. Numbers only. ---- */
const TK = {sizes: [], cv: {}, bars: {}, since: {}, box: {}};
function tkDraw(n){
  const cv = TK.cv[n]; if(!cv) return;
  const w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1; if(!w || !h) return;
  if(cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)){cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr)}
  const g = cv.getContext('2d'); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  const all = TK.bars[n] || [], bars = all.slice(-60);
  if(!bars.length){g.fillStyle = C.text; g.font = `11px ${MONO}`; g.fillText('waiting for prints...', 6, 16); return}
  const ph = Math.round(h * 0.72), dh = h - ph - 4, bw = w / Math.max(bars.length, 30), PAD = 6;
  let lo = Infinity, hi = -Infinity, md = 1;
  for(const b of bars){lo = Math.min(lo, b[3]); hi = Math.max(hi, b[2]); md = Math.max(md, Math.abs(b[6] - b[7]))}
  const y = p => PAD + (hi - p) / Math.max(hi - lo, 1e-9) * (ph - 2 * PAD);
  bars.forEach((b, i) => {
    const x = i * bw + bw / 2, [t, o, hh, l, c, v, buy, sell, gap] = b, col = gap ? '#4b5563' : c >= o ? C.up : C.dn;
    g.strokeStyle = g.fillStyle = col; g.lineWidth = 1;
    g.beginPath(); g.moveTo(x, y(hh)); g.lineTo(x, y(l)); g.stroke();
    g.fillRect(x - Math.max(1, bw * 0.3), Math.min(y(o), y(c)), Math.max(2, bw * 0.6), Math.max(1, Math.abs(y(o) - y(c))));
    if(!gap){const d = buy - sell, dhh = Math.abs(d) / md * (dh - 2); g.fillStyle = d >= 0 ? C.up : C.dn; g.fillRect(x - Math.max(1, bw * 0.3), ph + 2 + (dh - 2 - dhh), Math.max(2, bw * 0.6), Math.max(1, dhh))}
  });
}
function tkMeta(n){
  const box = TK.box[n]; if(!box) return;
  const bars = TK.bars[n] || [], last = bars.filter(b => b[9] >= n).slice(-1)[0], f = bars[bars.length - 1];
  const kids = box.querySelector('.mp'); if(!kids) return;
  kids.textContent = f ? (f[4] + '  ' + (last ? 'last \u0394 ' + (last[6] - last[7]) : 'building') + (f[9] < n ? '  ' + f[9] + '/' + n : '')) : '';
}
async function tkFetch(n){
  const cid = S.contractId; if(!cid) return;
  try{
    const r = await j(`/api/tickbars?contract=${encodeURIComponent(cid)}&n=${n}&limit=70`);
    if(cid !== S.contractId || !Array.isArray(r.bars)) return;
    TK.bars[n] = r.bars.filter(b => Array.isArray(b) && b.length >= 10 && b.slice(0, 8).every(Number.isFinite)); TK.since[n] = r.since;
  }catch(e){TK.bars[n] = []}
  tkMeta(n); tkDraw(n);
}
const tkAll = () => {for(const n of TK.sizes) tkFetch(n)};
(async () => {
  try{
    TK.sizes = ((await j('/api/tickbars/config')).sizes || []).filter(Number.isInteger).slice(0, 4);
    for(const n of TK.sizes){
      const box = mk('div', 'mn'); box.style.setProperty('--tfc', C.cyan);
      const mh = mk('div', 'mh'); mh.append(mk('b', '', n + 't'), mk('span', 'mp', ''));
      const cv = mk('canvas'); cv.setAttribute('role', 'img'); cv.setAttribute('aria-label', n + ' trade tick chart with buy minus sell volume');
      box.append(mh, cv); $('tk-body').append(box); TK.cv[n] = cv; TK.box[n] = box;
    }
    $('tk-state').textContent = TK.sizes.length ? '' : 'off';
    tkAll();
    new ResizeObserver(() => {for(const n of TK.sizes) tkDraw(n)}).observe($('tk-body'));
    setInterval(() => {if(document.visibilityState === 'visible') tkAll()}, 3000);
  }catch(e){$('tk-state').textContent = 'unavailable'}
})();

/* ---- drawing toolbar ---- */
const TOOLS = [['Trend', 'segment'], ['Ray', 'rayLine'], ['H-line', 'horizontalStraightLine'], ['V-line', 'verticalStraightLine'],
  ['Chan', 'parallelStraightLine'], ['Fib', 'fibonacciLine'], ['Zone', 'zoneBand'], ['Meas', 'measure'], ['Note', 'simpleAnnotation']];
const NONUSER = ['desk', 'react', 'respect', 'autolvl'];   // overlay groups that are never saved as user drawings
let magnet = LS.get('px.magnet', 0), locked = false, hidden = false;
const MODES = ['normal', 'weak_magnet', 'strong_magnet'];
function mkOv(o){
  const save = () => setTimeout(saveOverlays, 50);
  const ov = {mode: MODES[magnet], onDrawEnd: () => {save(); setActive(null)}, onPressedMoveEnd: save, onRemoved: save, ...o};
  if(ov.name === 'measure') ov.onDrawEnd = () => setActive(null);
  return chart.createOverlay(ov);
}
let active = null;
function setActive(n){
  active = n; [...$('tools').querySelectorAll('[data-tool]')].forEach(b => b.classList.toggle('on', b.dataset.tool === n));
  if(n) mkOv({name: n, lock: locked, visible: !hidden});
}
function saveOverlays(){
  if(restoring || !cur.ticker) return;
  const list = chart.getOverlays().filter(o => !NONUSER.includes(o.groupId) && o.name !== 'measure' && o.points?.length >= (o.totalStep - 1 || 1) && o.points.every(p => p.value != null))
    .map(o => ({name: o.name, points: o.points.map(p => ({timestamp: p.timestamp, value: p.value})), extendData: o.extendData ?? null, lock: !!o.lock}));
  LS.set('px.ov.' + cur.ticker, list);
}
function restoreOverlays(){
  restoring = true;
  try{
    chart.removeOverlay();
    for(const o of LS.get('px.ov.' + cur.ticker, [])) mkOv({name: o.name, points: o.points, extendData: o.extendData, lock: o.lock, visible: !hidden});
  }catch(e){err(e)}
  setTimeout(() => restoring = false, 100);
}
function toolbar(){
  const t = $('tools');
  t.innerHTML = TOOLS.map(([l, n]) => `<button data-tool="${n}" title="${n}">${l}</button>`).join('') + '<hr>' +
    '<button id="mag" title="magnet: off / weak / strong (snaps to OHLC)"></button><button id="lock" title="lock drawings">Lock</button>' +
    '<button id="hide" title="hide drawings">Hide</button><button id="clear" title="remove all drawings">Clear</button><hr>' +
    '<button id="shot" title="save PNG">Snap</button>';
  t.onclick = e => {
    const b = e.target.closest('button'); if(!b) return;
    if(b.dataset.tool) setActive(active === b.dataset.tool ? null : b.dataset.tool);
    else if(b.id === 'mag'){magnet = (magnet + 1) % 3; LS.set('px.magnet', magnet); magLabel(); chart.getOverlays().filter(o => !NONUSER.includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, mode: MODES[magnet]}))}
    else if(b.id === 'lock'){locked = !locked; b.classList.toggle('on', locked); chart.getOverlays().filter(o => !NONUSER.includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, lock: locked}))}
    else if(b.id === 'hide'){hidden = !hidden; b.classList.toggle('on', hidden); chart.getOverlays().filter(o => !NONUSER.includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, visible: !hidden}))}
    else if(b.id === 'clear'){chart.removeOverlay(); LS.set('px.ov.' + cur.ticker, []); drawDesk(true); drawReact(true); drawRespect(true); I.hsig = ''; indSync()}
    else if(b.id === 'shot'){const a = document.createElement('a'); a.href = chart.getConvertPictureUrl(true, 'png', '#0A0E17'); a.download = `${cur.name || 'chart'}-${Date.now()}.png`; a.click()}
  };
}
const magLabel = () => {$('mag').textContent = ['Mag', 'Mag~', 'Mag!'][magnet]; $('mag').classList.toggle('on', magnet > 0)};
addEventListener('keydown', e => {
  if(e.target.matches('input,select,button,textarea')) return;
  if(e.key === 'Escape'){if(active){chart.removeOverlay({id: chart.getOverlays().slice(-1)[0]?.id}); setActive(null)} return}
  const hot = {t: 'segment', h: 'horizontalStraightLine', v: 'verticalStraightLine', f: 'fibonacciLine', z: 'zoneBand', m: 'measure'}[e.key];
  if(hot && !e.ctrlKey && !e.metaKey && !e.altKey) setActive(hot);
});

/* ---- indicators: EMA 21, VWAP and the BUY/SELL VOL pane are always on. Everything else comes from GET /api/indicators
   (what price is respecting on the timeframe shown) or from a pin. The server picks; the page only draws the diff. ---- */
const indOn = {BSV: true, EMA: true};   // always on; renderEma and the flow poll read these
const ALWAYS = [['ema21', 'EMA 21', 'The 21 EMA, always on'], ['vwap', 'VWAP', 'Session VWAP, always on'], ['vol', 'Volume', 'Real buy/sell volume pane, always on']];
const PINS = [['EMA8', 'EMA 8 (fast)'], ['SMA50', 'SMA 50'], ['EMA34', 'EMA 34'], ['BOLL', 'BOLL'], ['RSI', 'RSI'], ['MACD', 'MACD'], ['VOL', 'VOL (candle colour)']];
const PIN = {...LS.get('px.ind.pin', {})};
const IND_TF = {'second,15': '5m', 'minute,1': '5m', 'minute,5': '5m', 'minute,15': '15m', 'minute,30': '15m', 'hour,1': '1h', 'hour,4': '4h', 'day,1': '1d'};
const IND_POLL_MS = 60000, IND_KEEP_MS = 600000;
const AUTO_COL = '#d6a73a';
const MA_COL = ['#E9EEF6', '#5FB3D9', '#B49AE0', '#7fb08a', '#D6A73A', '#8EA3C4'];
const I = {seq: 0, busy: false, key: '', sel: null, good: 0, applied: '', hsig: '', timer: null};   // sel: last good {levels, oscillators}
const indNum = v => typeof v === 'number' && isFinite(v);
function indSel(){   // the auto selection to honour now: the last good one for this contract and timeframe, for up to 10 minutes
  return I.sel && I.key === indKey() && Date.now() - I.good < IND_KEEP_MS ? I.sel : {levels: [], oscillators: []};
}
const indKey = () => (S.contractId || '') + '|' + (IND_TF[S.tf] || '5m');
function indWanted(){
  const sel = indSel(), ema = new Set([21]), ma = new Set(), hz = [], osc = new Set();
  if(PIN.EMA8) ema.add(8);
  if(PIN.EMA34) ema.add(34);
  if(PIN.SMA50) ma.add(50);
  let boll = !!PIN.BOLL;
  for(const l of sel.levels){
    if(l.draw === 'series' && (l.family === 'ema' || l.family === 'sma') && indNum(l.period) && l.period >= 2 && l.period <= 500) (l.family === 'ema' ? ema : ma).add(Math.round(l.period));
    else if(l.draw === 'series' && l.family === 'boll') boll = true;
    else if(l.draw === 'horizontal' && indNum(l.price)) hz.push(l);
  }
  for(const o of sel.oscillators) osc.add(o.id);
  if(PIN.RSI) osc.add('rsi');
  if(PIN.MACD) osc.add('macd');
  return {ema: [...ema].sort((a, b) => a - b), ma: [...ma].sort((a, b) => a - b), boll, rsi: osc.has('rsi'), macd: osc.has('macd'), vol: !!PIN.VOL, hz};
}
function maStyles(ps){return {lines: ps.map((p, i) => ({color: p === 8 ? EMA_COL[0] : p === 21 ? EMA_COL[1] : p === 34 ? EMA_COL[2] : MA_COL[3 + (i % 3)], size: 1.5, style: 'solid', smooth: false}))}}
function indHave(n){try{return chart.getIndicators({name: n}).length > 0}catch{return false}}
function indSync(){   // diff the wanted set against what is drawn; only touch what changed
  try{
    const w = indWanted(), a = I.applied ? JSON.parse(I.applied) : {ema: [], ma: [], boll: false, rsi: false, macd: false, vol: false};
    const sameArr = (x, y) => x.join() === y.join();
    if(!indHave('VWAP')) chart.createIndicator({name: 'VWAP', paneId: 'candle_pane'}, true);
    if(!indHave('BSV')) chart.createIndicator({name: 'BSV'}, false);
    if(!sameArr(w.ema, a.ema) || !indHave('EMA')){chart.removeIndicator({name: 'EMA'}); chart.createIndicator({name: 'EMA', paneId: 'candle_pane', calcParams: w.ema, styles: maStyles(w.ema)}, true)}
    if(!sameArr(w.ma, a.ma) || (w.ma.length > 0) !== indHave('MA')){
      chart.removeIndicator({name: 'MA'});
      if(w.ma.length) chart.createIndicator({name: 'MA', paneId: 'candle_pane', calcParams: w.ma, styles: {lines: w.ma.map((p, i) => ({color: MA_COL[3 + (i % 3)], size: 1.5, style: 'solid', smooth: false}))}}, true);
    }
    for(const [k, n, main] of [['boll', 'BOLL', true], ['rsi', 'RSI', false], ['macd', 'MACD', false], ['vol', 'VOL', false]]){
      if(w[k] === indHave(n) && (w[k] === a[k] || indHave(n))) continue;
      if(w[k]) chart.createIndicator(main ? {name: n, paneId: 'candle_pane'} : {name: n}, main); else chart.removeIndicator({name: n});
    }
    I.applied = JSON.stringify({ema: w.ema, ma: w.ma, boll: w.boll, rsi: w.rsi, macd: w.macd, vol: w.vol});
    drawAutoLevels(w.hz);
  }catch(e){err(e)}
  indChips();
}
function drawAutoLevels(hz){   // horizontal auto levels (fib, prior, keltner) on the respectLevel overlay; a line the Respect toggle already draws is skipped
  const tk = cur && cur.tick ? cur.tick : 0, rl = respectLines();
  const lines = hz.filter(l => !rl.some(r => Math.abs(r.price - l.price) <= Math.max(tk * 2, 1e-9))).map(l => ({price: l.price, color: AUTO_COL, dv: [6, 4], stable: false,
    label: `${rspName(l.label)}${indNum(l.held) && indNum(l.tests) ? ` ${l.held}/${l.tests}` : ''}`}));
  const sig = JSON.stringify(lines) + '|' + (S.contractId || '');
  if(sig === I.hsig) return;
  const last = chart.getDataList().slice(-1)[0];
  if(!last && lines.length) return;   // retry once bars exist
  I.hsig = sig;
  try{
    chart.removeOverlay({groupId: 'autolvl'});
    for(const l of lines) chart.createOverlay({name: 'respectLevel', groupId: 'autolvl', lock: true, points: [{timestamp: last.timestamp, value: l.price}], extendData: l});
  }catch(e){err(e)}
}
function indChips(){   // locked "always" chips, auto chips with the reason as tooltip, and the pin box
  const box = $('ind');
  if(!I.pinEl) I.pinEl = pinBox();   // built once, so a click keeps focus and the box stays open
  box.replaceChildren();
  for(const [, l, t] of ALWAYS){const c = mk('span', 'ichip always', l); c.title = t; box.append(c)}
  const sel = indSel();
  for(const l of sel.levels){
    const c = mk('span', 'ichip auto', rspName(l.label));
    c.title = (l.reason === 'RESPECTED' || !l.reason ? 'respected' : String(l.reason).toLowerCase()) + (indNum(l.held) && indNum(l.tests) ? ` ${l.held}/${l.tests}` : '') + (l.stable === true ? ', stable' : l.stable === false ? ', not stable' : '');
    box.append(c);
  }
  for(const o of sel.oscillators){
    const c = mk('span', 'ichip auto', rspName(o.label));
    c.title = 'active' + (indNum(o.pctile) ? ', top ' + Math.max(1, Math.round((1 - o.pctile) * 100)) + '% of recent swing' : '');
    box.append(c);
  }
  box.append(I.pinEl);
}
function pinBox(){
  const d = mk('details', 'pin');
  d.append(mk('summary', '', 'Pin more'));
  const body = mk('div', 'pinbody');
  for(const [k, l] of PINS){
    const lab = mk('label'), cb = mk('input'); cb.type = 'checkbox'; cb.dataset.pin = k; cb.checked = !!PIN[k];
    lab.append(cb, document.createTextNode(' ' + l)); body.append(lab);
  }
  d.append(body); return d;
}
$('ind').onchange = e => {const k = e.target.dataset.pin; if(!k) return; PIN[k] = e.target.checked; LS.set('px.ind.pin', PIN); indSync()};
async function indFetch(){
  const cid = S.contractId; if(!cid || I.busy) return;
  const key = indKey(), my = ++I.seq; I.busy = true;
  try{
    const r = await fetch('/api/indicators?contract=' + encodeURIComponent(cid) + '&tf=' + (IND_TF[S.tf] || '5m'), {headers: {accept: 'application/json'}});
    let b = null; try{b = await r.json()}catch{}
    if(my !== I.seq || key !== indKey()) return;   // superseded by a newer request, contract or timeframe
    if(r.ok && b && b.status === 'ok' && b.contract === cid && Array.isArray(b.levels)){
      I.sel = {
        levels: b.levels.filter(l => l && typeof l.label === 'string' && (l.draw === 'series' || l.draw === 'horizontal')).slice(0, 3),
        oscillators: (Array.isArray(b.oscillators) ? b.oscillators : []).filter(o => o && (o.id === 'macd' || o.id === 'rsi')).slice(0, 2)};
      I.key = key; I.good = Date.now();
    }   // loading, error, 404: keep the last good selection until it ages out
  }catch(e){}
  finally{if(my === I.seq) I.busy = false}
  indSync();
}
function indLoad(){   // contract or timeframe change: drop the old selection at once, then fetch
  I.seq++; I.busy = false;
  if(I.key !== indKey()){I.sel = null; I.hsig = ''; try{chart.removeOverlay({groupId: 'autolvl'})}catch(e){err(e)}}
  indSync(); indFetch();
}
I.timer = setInterval(() => {if(document.visibilityState === 'visible'){I.busy = false; indFetch()}}, IND_POLL_MS);
document.addEventListener('visibilitychange', () => {if(document.visibilityState === 'visible') indFetch()});

/* ---- contract + timeframe (ribbon) ---- */
const TFS = [['second,15', '15s'], ['minute,1', '1m'], ['minute,5', '5m'], ['minute,15', '15m'], ['minute,30', '30m'], ['hour,1', '1h'], ['hour,4', '4h'], ['day,1', '1D']];   // the desk always STARTS on 5m (never restored from a saved choice); switch freely during the session
if(!TFS.some(t => t[0] === S.tf)) S.tf = 'minute,5';
for(const [v, l] of TFS){const b = mk('button', '', l); b.dataset.v = v; b.setAttribute('aria-pressed', 'false'); $('tfs').append(b)}
const syncTf = () => {[...$('tfs').children].forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === S.tf))); if(typeof renderRespect === 'function' && typeof R !== 'undefined'){renderRespect(); mnSync()}};
$('tfs').onclick = e => {const b = e.target.closest('button'); if(b){S.tf = b.dataset.v; syncTf(); load()}};
async function search(){
  try{
    const cs = await j('/contracts?q=' + encodeURIComponent($('q').value));
    const sel = $('c'); sel.replaceChildren();
    for(const c of cs){
      const o = mk('option', '', c.desc ? `${c.name} ${c.desc}` : c.name); o.value = c.id; o.dataset.tick = c.tick; o.dataset.tv = c.tickValue ?? ''; o.dataset.name = c.name; sel.append(o);
    }
    const last = LS.get('px.contract'); if(last && [...sel.options].some(o => o.value === last)) sel.value = last;
    if(!cs.length) $('msg').textContent = 'no contracts';
    load();
  }catch(e){err('contracts unavailable'); S.contractId = null; schedule(0)}
}
function load(){
  const o = $('c').selectedOptions[0]; if(!o) return;
  const [type, span] = S.tf.split(',');
  const tick = +o.dataset.tick, dec = (o.dataset.tick.split('.')[1] || '').length;
  LS.set('px.contract', o.value);
  const sym = want?.ticker !== o.value, pp = chart.getPeriod(), per = pp?.type !== type || pp?.span !== +span;
  cur = {ticker: o.value, tick, tickValue: o.dataset.tv === '' ? null : +o.dataset.tv, name: o.dataset.name, dec};
  S.contractId = o.value; FLOW.since = null; FLOW.holes = []; FLOW.m = new Map(); for(const n of TK.sizes){TK.bars[n] = []; tkMeta(n); tkDraw(n)}; syncTf(); deskSig = ''; reactSig = ''; respectLoad(sym); indLoad();
  want = {ticker: o.value, type, span: +span}; lastPx = null; $('price').textContent = 'NA'; $('price').className = 'price';
  if(sym) chart.setSymbol({ticker: o.value, pricePrecision: dec, volumePrecision: 0});
  if(per) chart.setPeriod({span: +span, type});
  schedule(0);
}
$('q').onkeydown = e => {if(e.key === 'Enter') search()};
$('q').onchange = search;
$('c').onchange = load;
$('tz').value = LS.get('px.tz', 'America/Chicago');
$('tz').onchange = () => {LS.set('px.tz', $('tz').value); chart.setTimezone($('tz').value)};
toolbar(); magLabel(); syncTf(); syncSizeInputs();
indSync();
new ResizeObserver(() => chart.resize()).observe($('chart'));

applySlots(); renderAll(); renderRespect();
search();
schedule(0);
