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
  contractId: null, tf: LS.get('px.tf', 'minute,5'), side: 'long',
  riskPct: LS.get('px.riskPct', 0.03), stopK: LS.get('px.stopK', 1.5), sizeFor: 'AUTO',
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
const VERDICT = {ABOVE_RANDOM: 'BETTER THAN RANDOM', NOT_DISTINGUISHABLE: 'SAME AS RANDOM', BELOW_RANDOM: 'WORSE THAN RANDOM', TOO_FEW_TESTS: 'TOO FEW TESTS'};
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
const csrf = (force) => (csrfP && !force) ? csrfP : (csrfP = j('/api/csrf').then(r => r.token));
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
  LS.set('px.riskPct', S.riskPct); LS.set('px.stopK', S.stopK);
}
function inputChanged(){syncSizeInputs(); schedule(100)}
$('risk-pct').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.riskPct = +b.dataset.v; inputChanged()}};
$('risk-dec').onclick = () => {S.riskPct = Math.max(0.005, Math.round((S.riskPct - 0.005) * 1000) / 1000); inputChanged()};
$('risk-inc').onclick = () => {S.riskPct = Math.min(0.1, Math.round((S.riskPct + 0.005) * 1000) / 1000); inputChanged()};
$('stop-atr').onclick = e => {const b = e.target.closest('[data-v]'); if(b){S.stopK = +b.dataset.v; inputChanged()}};
function stepTicks(d){   // move the stop by whole ticks using the server's own stop_ticks, atr and tick
  const z = S.ai && S.ai.size, atr = S.ai && S.ai.ind && S.ai.ind.atr, tick = S.ai && S.ai.contract && S.ai.contract.tick;
  if(!z || z.status !== 'OK' || !atr || !tick) return;
  const t = Math.max(1, z.stop_ticks + d);
  S.stopK = Math.min(10, Math.max(0.1, Math.round((t - 0.5) * tick / atr * 10000) / 10000));
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
    const p = new URLSearchParams({side: S.side, risk_pct: String(S.riskPct), stop_k: String(S.stopK)});
    if(cid) p.set('contract', cid);
    if(streaming()) p.set('stream', '1');
    const stateUrl = '/state.json' + (S.contractId ? '?contract=' + encodeURIComponent(S.contractId) : '');
    const [ai, st, dg] = await Promise.all([
      j('/ai/state?' + p).catch(() => null), j(stateUrl).catch(() => null),
      S.aiOpen ? fetch('/ai/digest?' + p).then(r => r.ok ? r.text() : null).catch(() => null) : null]);
    if(my !== S.seq) return;
    S.aiFails = ai ? 0 : S.aiFails + 1;
    S.prev = S.ai; S.ai = ai; S.st = st; S.sizeCid = cid; S.cd = {left: (ai && ai.behavior && num(ai.behavior.cooldown_left_s)) ? ai.behavior.cooldown_left_s : 0, at: performance.now()}; if(dg != null) S.digest = dg;
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
    v.textContent = (d >= 0 ? 'ABOVE BE +' : 'BELOW BE ') + d.toFixed(1) + 'pt';
  }else v.textContent = num(wr) ? 'BE NA' : 'NO TRADES';
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
  renderWhy(); renderRibbon(); renderKpi(); renderSize(); renderTicketFields(); renderTicketMeta(); renderRisk(); renderBlotter(); renderAiPanel(); renderPlaceResult();
  fill($('size'), S.ai);
  renderPair(); renderEma(); renderLevels(); renderManage(); drawReact(false);
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
  bd.hidden = !indOn.EMA;
  bd.dataset.state = st || 'NA';
  fill(bd, S.ai);
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
    if(lv.small_sample) l2.append(mk('span', 'ss', 'SMALL SAMPLE'));
    // baseline: the same rules on random control lines near price. A level only means something if it beats this.
    const rnd = mk('span', 'l2r');
    rnd.title = 'The same bounce rules applied to random lines near price. If random lines hold as often, this level shows nothing.';
    rnd.append('random lines ');
    if(num(lv.random_rate)) rnd.append(sp('', 'random_rate', 'int'), '% (n ', sp('', 'random_n', 'int'), ')');
    else rnd.append(num(lv.random_n) && lv.random_n > 0 ? sp('', 'random_rate', 'int') : 'NA');
    const vk = Object.prototype.hasOwnProperty.call(VERDICT, lv.verdict) ? lv.verdict : null;
    const vd = sp('vd vd-' + (vk ? vk.toLowerCase() : 'na'), 'verdict', 'verdict');
    rnd.append(vd);
    const bits = [];
    if(num(lv.failed) && lv.failed > 0) bits.push([lv.failed, 'failed']);
    if(num(lv.neutral) && lv.neutral > 0) bits.push([lv.neutral, 'flat']);
    if(num(lv.pending) && lv.pending > 0) bits.push([lv.pending, 'pending']);
    const l3 = mk('span', 'l3');
    const extra = mk('span', 'lv-x');
    bits.forEach(([v, w], i) => {if(i) extra.append(', '); const b = mk('b'); b.dataset.ai = pre + (w === 'failed' ? 'failed' : w === 'flat' ? 'neutral' : 'pending'); b.dataset.fmt = 'int'; extra.append(b, ' ' + w)});
    l3.append(extra);
    const lr = RESULTS.has(lv.last_result) ? lv.last_result : null;
    const last = mk('span', 'lv-last');
    if(lr){last.append('last ', mk('span', 'rs rs-' + lr.toLowerCase(), lr));
      if(num(lv.last_bars_ago)){last.append(', '); const b = mk('b'); b.dataset.ai = pre + 'last_bars_ago'; b.dataset.fmt = 'int'; last.append(b, ' bars ago')}}
    else last.append('last none');
    l3.append(last);
    row.append(l1, l2, rnd, l3);
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
const MG_LABEL = {BREAKEVEN: 'STOP TO BREAKEVEN', ATR_TRAIL: 'STOP TO ATR TRAIL', EMA21_TRAIL: 'STOP TO EMA21 TRAIL'};
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
    const hint = x.recommended ? 'RECOMMENDED' : x.valid ? 'not earned yet' : 'not valid now';
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
$('stage-btn').onclick = async () => {
  const z = S.ai && S.ai.size; if(!z || z.status !== 'OK' || z.refused || z.allowed === false) return;
  staging = true; renderTicketMeta();
  const cid = await sizeContract();
  const r = await post('/api/preview', {contract: cid, side: S.side, stop_ticks: z.stop_ticks, risk_pct: S.riskPct, order_type: 'market'});
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
$('clear-halt-btn').onclick = async () => {const r = await post('/api/halt/clear', {}); actMsg(r.ok ? (r.data.halted ? 'Halt still set.' : 'Halt cleared.') : 'Clear FAILED: ' + detailOf(r)); S.placed = null; schedule(0)};
$('ai-toggle').onclick = () => {S.aiOpen = !S.aiOpen; renderAiPanel(); schedule(0)};

window.px = {snapshot(){return S.ai == null ? null : JSON.parse(JSON.stringify(S.ai))}};

/* ================= chart (KLineChart v10) ================= */
const err = e => {$('msg').textContent = (e && e.message) || String(e)};
let cur = {ticker: null, tick: 0.25, tickValue: null, name: '', dec: 2};
const C = {up: '#22c55e', dn: '#f43f5e', flat: '#76839a', grid: '#121a2a', axis: '#1c2438', text: '#76839a', cyan: '#38bdf8', amber: '#f59e0b', purple: '#a855f7', ink: '#e8eef7'};
const EMA_COL = ['#2dd4bf', '#60a5fa', '#e879f9'];   // EMA 8 / 21 / 34; keep in step with --ema8/21/34 in desk.css
const RC = {BOUNCE: C.up, FAIL: C.dn, NEUTRAL: '#76839a', PENDING: C.cyan};
const MONO = '"JetBrains Mono", ui-monospace, monospace';
const axis = {axisLine: {color: C.axis}, tickLine: {color: C.axis}, tickText: {color: C.text, size: 10, family: MONO}};
const chs = {line: {color: '#3b4a68', style: 'dashed', dashedValue: [3, 3]}, text: {backgroundColor: '#1b2538', borderColor: '#2a3550', color: C.ink, family: MONO, size: 11}};
const legendTxt = {size: 11, family: MONO, color: C.text};
const STYLES = {
  grid: {horizontal: {color: C.grid, style: 'solid'}, vertical: {color: C.grid, style: 'solid'}},
  candle: {bar: {upColor: C.up, downColor: C.dn, noChangeColor: C.flat, upBorderColor: C.up, downBorderColor: C.dn,
                 noChangeBorderColor: C.flat, upWickColor: C.up, downWickColor: C.dn, noChangeWickColor: C.flat},
           priceMark: {high: {show: false}, low: {show: false},
                       last: {upColor: C.cyan, downColor: C.cyan, noChangeColor: C.cyan, line: {style: 'dashed', dashedValue: [2, 3]},
                              text: {color: '#04121c', family: MONO, weight: '600', borderRadius: 3}}},
           tooltip: {title: {...legendTxt, color: '#94a3b8'}, legend: {...legendTxt, template: [{title: 'O ', value: '{open}'}, {title: 'H ', value: '{high}'}, {title: 'L ', value: '{low}'}, {title: 'C ', value: '{close}'}]}}},
  indicator: {bars: [{upColor: 'rgba(34,197,94,.45)', downColor: 'rgba(244,63,94,.45)', noChangeColor: 'rgba(118,131,154,.45)'}],
              lines: ['#60a5fa', '#c084fc', '#38bdf8', '#fb7185', '#94a3b8'].map(color => ({color, size: 1})),
              tooltip: {title: legendTxt, legend: legendTxt}},
  xAxis: axis, yAxis: axis, separator: {color: C.axis}, crosshair: {horizontal: chs, vertical: chs},
};
const dimText = {color: C.ink, backgroundColor: 'rgba(13,18,29,.92)', borderColor: '#2a3550', borderSize: 1, size: 12, family: MONO, paddingLeft: 6, paddingRight: 6, paddingTop: 3, paddingBottom: 3, style: 'stroke_fill', borderRadius: 3};
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
      cur.tickValue != null ? `${up ? '+' : '-'}$${Math.abs(ticks * cur.tickValue).toFixed(2)} / contract` : null, `${bars} bars`].filter(Boolean);
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
       styles: {color: ed.ink || ed.color || C.purple, size: 10, family: MONO, weight: '600', backgroundColor: 'rgba(8,10,15,.78)', borderSize: 0, borderRadius: 2,
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
        return [{type: 'polygon', ignoreEvent: true, attrs: {coordinates: pts}, styles: {style: 'stroke_fill', color: col, borderColor: '#080a0f', borderSize: 1}}];
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
      if(type === 'init') setTimeout(() => {restoreOverlays(); drawDesk(true); drawReact(true)}, 0);
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
      es.onerror = () => {setDot('warn'); $('msg').textContent = 'tick feed reconnecting...'};
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
  const t = S.ticket;
  if(t && t.ticket_id && !t.consumed && !t.gone && t.contract === S.contractId && t.readback){
    const e = Number(t.readback.entry_ref), s = Number(t.readback.stop_price);
    if(isFinite(e)) out.push({price: e, label: 'ENTRY ' + e, color: C.cyan, ink: '#7dd3fc', dash: false, size: 1});
    if(isFinite(s)) out.push({price: s, label: 'STOP ' + s, color: C.dn, ink: '#fb7185', dash: false, size: 1});
  }else{
    const tr = S.ai && S.ai.trade;   // live bracket on the contract we placed it on
    if(tr && S.placed && S.placed.contract === S.contractId){
      if(num(tr.entry_ref)) out.push({price: tr.entry_ref, label: 'ENTRY ' + tr.entry_ref + ' ' + (tr.state === 'PLACED' ? ((tr.filled || 0) > 0 ? 'FILLED' : tr.acked ? 'ACCEPTED' : 'PLACED') : tr.state), color: C.cyan, ink: '#7dd3fc', dash: false, size: 1});
      const sp = S.ai.manage && num(S.ai.manage.stop) ? S.ai.manage.stop : tr.stop_price;   // manage.stop follows a moved stop
      if(num(sp)) out.push({price: sp, label: 'STOP ' + sp + ' ' + (tr.stop_state || 'PENDING'), color: C.dn, ink: '#fb7185', dash: tr.stop_state !== 'ACKED' && tr.stop_state !== 'ATOMIC', size: 1});
    }else if(S.ai && S.ai.manage && num(S.ai.manage.stop)){   // position not placed from this page: still show where its stop is
      out.push({price: S.ai.manage.stop, label: 'STOP ' + S.ai.manage.stop, color: C.dn, ink: '#fb7185', dash: false, size: 1});
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

/* ---- drawing toolbar ---- */
const TOOLS = [['Trend', 'segment'], ['Ray', 'rayLine'], ['H-line', 'horizontalStraightLine'], ['V-line', 'verticalStraightLine'],
  ['Chan', 'parallelStraightLine'], ['Fib', 'fibonacciLine'], ['Zone', 'zoneBand'], ['Meas', 'measure'], ['Note', 'simpleAnnotation']];
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
  const list = chart.getOverlays().filter(o => !['desk', 'react'].includes(o.groupId) && o.name !== 'measure' && o.points?.length >= (o.totalStep - 1 || 1) && o.points.every(p => p.value != null))
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
    else if(b.id === 'mag'){magnet = (magnet + 1) % 3; LS.set('px.magnet', magnet); magLabel(); chart.getOverlays().filter(o => !['desk', 'react'].includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, mode: MODES[magnet]}))}
    else if(b.id === 'lock'){locked = !locked; b.classList.toggle('on', locked); chart.getOverlays().filter(o => !['desk', 'react'].includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, lock: locked}))}
    else if(b.id === 'hide'){hidden = !hidden; b.classList.toggle('on', hidden); chart.getOverlays().filter(o => !['desk', 'react'].includes(o.groupId)).forEach(o => chart.overrideOverlay({id: o.id, visible: !hidden}))}
    else if(b.id === 'clear'){chart.removeOverlay(); LS.set('px.ov.' + cur.ticker, []); drawDesk(true); drawReact(true)}
    else if(b.id === 'shot'){const a = document.createElement('a'); a.href = chart.getConvertPictureUrl(true, 'png', '#0d121d'); a.download = `${cur.name || 'chart'}-${Date.now()}.png`; a.click()}
  };
}
const magLabel = () => {$('mag').textContent = ['Mag', 'Mag~', 'Mag!'][magnet]; $('mag').classList.toggle('on', magnet > 0)};
addEventListener('keydown', e => {
  if(e.target.matches('input,select,button,textarea')) return;
  if(e.key === 'Escape'){if(active){chart.removeOverlay({id: chart.getOverlays().slice(-1)[0]?.id}); setActive(null)} return}
  const hot = {t: 'segment', h: 'horizontalStraightLine', v: 'verticalStraightLine', f: 'fibonacciLine', z: 'zoneBand', m: 'measure'}[e.key];
  if(hot && !e.ctrlKey && !e.metaKey && !e.altKey) setActive(hot);
});

/* ---- indicators ---- */
const INDS = [['VOL', 'VOL', false], ['VWAP', 'VWAP', true], ['EMA', 'EMA 8 21 34', true], ['BOLL', 'BOLL', false], ['RSI', 'RSI', false], ['MACD', 'MACD', false]];
const indOn = {VOL: true, VWAP: true, EMA: true, ...LS.get('px.ind', {})};
const MAIN = new Set(['VWAP', 'EMA', 'BOLL']);
function applyInd(n, on){
  if(on && n === 'EMA') chart.createIndicator({name: 'EMA', paneId: 'candle_pane', calcParams: [8, 21, 34], styles: {lines: EMA_COL.map(color => ({color, size: 1.5, style: 'solid', smooth: false}))}}, true);
  else if(on) chart.createIndicator(MAIN.has(n) ? {name: n, paneId: 'candle_pane'} : {name: n}, MAIN.has(n));
  else chart.removeIndicator({name: n});
}
for(const [n, l] of INDS){
  const lab = mk('label'), cb = mk('input'); cb.type = 'checkbox'; cb.dataset.ind = n; cb.checked = !!indOn[n];
  lab.append(cb, document.createTextNode(' ' + l));
  if(n === 'EMA'){lab.classList.add('ema'); for(const k of [0, 1, 2]){const sw = mk('i', 'sw'); sw.style.background = EMA_COL[k]; sw.setAttribute('aria-hidden', 'true'); lab.append(sw)}}
  $('ind').append(lab);
}
$('ind').onchange = e => {const n = e.target.dataset.ind; indOn[n] = e.target.checked; LS.set('px.ind', indOn); applyInd(n, e.target.checked); renderEma()};

/* ---- contract + timeframe (ribbon) ---- */
const TFS = [['second,15', '15s'], ['minute,1', '1m'], ['minute,5', '5m'], ['minute,15', '15m'], ['minute,30', '30m'], ['hour,1', '1h'], ['hour,4', '4h'], ['day,1', '1D']];
if(!TFS.some(t => t[0] === S.tf)) S.tf = 'minute,5';
for(const [v, l] of TFS){const b = mk('button', '', l); b.dataset.v = v; b.setAttribute('aria-pressed', 'false'); $('tfs').append(b)}
const syncTf = () => [...$('tfs').children].forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === S.tf)));
$('tfs').onclick = e => {const b = e.target.closest('button'); if(b){S.tf = b.dataset.v; syncTf(); load()}};
async function search(){
  try{
    const cs = await j('/contracts?q=' + encodeURIComponent($('q').value));
    const sel = $('c'); sel.replaceChildren();
    for(const c of cs){
      const o = mk('option', '', `${c.name} ${c.desc}`); o.value = c.id; o.dataset.tick = c.tick; o.dataset.tv = c.tickValue ?? ''; o.dataset.name = c.name; sel.append(o);
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
  LS.set('px.contract', o.value); LS.set('px.tf', S.tf);
  const sym = want?.ticker !== o.value, pp = chart.getPeriod(), per = pp?.type !== type || pp?.span !== +span;
  cur = {ticker: o.value, tick, tickValue: o.dataset.tv === '' ? null : +o.dataset.tv, name: o.dataset.name, dec};
  S.contractId = o.value; syncTf(); deskSig = ''; reactSig = '';
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
for(const [n] of INDS) if(indOn[n]) applyInd(n, true);
new ResizeObserver(() => chart.resize()).observe($('chart'));

applySlots(); renderAll();
search();
schedule(0);
