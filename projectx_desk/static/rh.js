'use strict';
// Robinhood panel: a broker switch (ProjectX | Robinhood), a full-width overlay with positions, orders, approvals and tickets, an order
// ticket (preview, hashed ticket, 600 ms hold to place) and the DEMO banner. It talks to /api/rh/* and /api/csrf only, renders every
// dynamic string with textContent, keeps no ticket data in the browser (no storage, no console) and never touches desk.js.
// The pure functions at the top are exported on window.pxRh (and module.exports under node) for tests/test_rh_ui.py.
// Robinhood has no paper account: every placed order is real money, a fill is unprotected (the desk places no stop), and the
// final approval is a tap in the Robinhood app. The copy below says so and must keep saying so.
(function(root){
  const HOLD_MS = 600, ON_SCREEN_MS = 1500, POLL_MS = 5000;

  // ---- plain words for every refusal code the guard can answer (rh/guard.py CODES) ---------------------------------------------
  const REFUSALS = {
    trading_off: 'Robinhood trading is switched off on this desk (PX_RH_TRADING).',
    halted: 'Robinhood is halted. Find out why, then clear the halt yourself below.',
    account_unknown: 'The desk could not read the Robinhood account, so it will not guess. Try again.',
    approvals_off: 'Trade approvals are OFF in the Robinhood app. Turn them on first: no order goes out without your tap.',
    approvals_unknown: 'The desk could not read the trade-approvals setting, so it will not send anything.',
    bad_intent: 'Check the form: a ticker, buy or sell, a whole number of shares and a limit price above zero.',
    qty_cap: 'That is more shares than the desk allows per order (PX_RH_MAX_QTY).',
    notional_cap: 'That order is worth more than the desk allows (PX_RH_MAX_NOTIONAL).',
    symbol_busy: 'This ticker already has a live ticket, working order or pending approval. Finish or cancel that one first.',
    sell_exceeds_position: 'You are trying to sell more shares than you hold.',
    no_position: 'You hold none of this ticker, and the desk only sells to close.',
    market_closed: 'The regular market is closed (Mon to Fri, 9:30 to 16:00 ET).',
    quote_stale: 'No fresh quote for this ticker. Try again in a moment.',
    cap_unset: 'The order caps are not configured, so every order is refused (set PX_RH_MAX_QTY and PX_RH_MAX_NOTIONAL).',
    read_failed: 'A Robinhood read failed, so the desk will not send. Try again.',
    ticket_unknown: 'The desk has no such ticket.',
    ticket_used: 'That ticket was already used. Preview a new one.',
    ticket_expired: 'The ticket expired. Preview again.',
    hash_mismatch: 'The ticket changed or the code did not match. Preview again.',
    state_unwritable: 'The desk could not save the ticket to disk, so it did not send. Nothing was placed.',
    bad_state: 'That ticket is not in a state where this action applies.',
    no_order: 'There is no order on record for that ticket.',
    still_live: 'Robinhood still shows that order or approval, so it cannot be released.',
  };
  const REFUSAL_FALLBACK = 'The desk refused it.';
  function refusalText(code){
    return Object.prototype.hasOwnProperty.call(REFUSALS, code) ? REFUSALS[code] : REFUSAL_FALLBACK;
  }

  // ---- ticket states, in plain words ------------------------------------------------------------------------------------------
  const STATES = {
    previewed: 'Previewed, not sent. Nothing has reached Robinhood yet.',
    sending: 'Sending to Robinhood now.',
    awaiting_approval: 'Waiting for you: approve it in the Robinhood app.',
    approved: 'You approved it in the Robinhood app. The order is working or filling.',
    placed: 'Placed at Robinhood and working.',
    filled: 'Filled. The desk places no stop: you own this position unprotected.',
    declined: 'You declined it in the Robinhood app. Nothing was bought or sold.',
    expired: 'The approval expired without a tap. Nothing was bought or sold.',
    cancelled: 'Cancelled.',
    rejected: 'Robinhood rejected the order. Nothing was bought or sold.',
    refused: 'The desk refused it before sending anything.',
    discarded: 'Discarded before it was sent.',
    released: 'Released by you: the desk stopped tracking it.',
    unknown: 'Outcome unclear. The desk is halted for Robinhood. Reconcile it, or release it once the Robinhood app shows nothing.',
  };
  function stateText(s){
    return Object.prototype.hasOwnProperty.call(STATES, s) ? STATES[s] : 'State not recognised: check the Robinhood app.';
  }
  const WARNINGS = {
    no_paper_trading: 'No paper trading. This is real money.',
    fill_unprotected: 'A fill is unprotected: the desk places no stop.',
    approve_in_app: 'Approve in the Robinhood app.',
  };
  const WARNING_LINE = 'No paper trading. This is real money. A fill is unprotected: the desk places no stop. Approve in the Robinhood app.';
  const HALTS = {
    place_unknown: 'A place left an unclear outcome.', live_order: 'Robinhood answered something other than "awaiting approval".',
    cancel_unknown: 'A cancel left an unclear outcome.', state_unreadable: 'The ticket file could not be read.',
    manual: 'Halted by hand.', unknown: 'Halted, cause not recorded.',
  };
  function haltText(h){
    return Object.prototype.hasOwnProperty.call(HALTS, h) ? HALTS[h] : 'Halted.';
  }
  const DEMO_LINE = 'DEMO: mock Robinhood, no real account';

  // ---- which buttons a state allows ---------------------------------------------------------------------------------------------
  function actionsFor(state){
    return {
      cancel: state === 'awaiting_approval' || state === 'approved' || state === 'placed',
      reconcile: state === 'unknown',
      release: state === 'awaiting_approval' || state === 'approved' || state === 'placed' || state === 'unknown',
    };
  }
  const LIVE = ['previewed', 'sending', 'awaiting_approval', 'approved', 'placed', 'unknown'];
  function isLive(state){ return LIVE.indexOf(state) >= 0; }

  // ---- formatting -----------------------------------------------------------------------------------------------------------------
  const isNum = (n) => typeof n === 'number' && isFinite(n);
  function fmtQty(n){
    if(!isNum(n)) return '?';
    return Math.abs(n - Math.round(n)) < 1e-9 ? String(Math.round(n)) : String(+n.toFixed(4));
  }
  function fmtPrice(n){ return isNum(n) ? n.toFixed(2) : '?'; }
  function fmtMoney(n){
    if(!isNum(n)) return '?';
    const s = Math.abs(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (n < 0 ? '-$' : '$') + s;
  }
  function optionLabel(p){
    if(!p) return '?';
    const parts = [String(p.symbol || '?')];
    parts.push(typeof p.expiry === 'string' ? p.expiry : '?');
    parts.push(isNum(p.strike) ? String(+p.strike.toFixed(2)) : '?');
    parts.push(p.right === 'call' ? 'C' : p.right === 'put' ? 'P' : '?');
    return parts.join(' ');
  }
  function countdown(expiresAt, nowSec){
    if(!isNum(expiresAt) || !isNum(nowSec)) return '';
    const left = Math.ceil(expiresAt - nowSec);
    if(left <= 0) return 'expired';
    return Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0');
  }
  function accountLine(a, setting){
    if(!a) return 'Account: unknown';
    const t = String(a.type || 'other').replace(/_/g, ' ');
    let s = 'Account: ' + t + ', ' + String(a.state || 'unknown');
    if(a.option_level) s += ', ' + String(a.option_level).replace(/_/g, ' ');
    const v = setting && typeof setting === 'object' ? setting.human_must_approve : null;
    s += v === true ? '. Trade approvals: ON.' : v === false ? '. Trade approvals: OFF (the desk will refuse to place).' : '. Trade approvals: unknown (the desk will refuse to place).';
    return s;
  }
  // The form: returns {ok:true, intent} or {ok:false, code:'bad_intent'}. The server re-checks everything; this only saves a round trip.
  function readForm(f){
    const symbol = String(f && f.symbol != null ? f.symbol : '').trim().toUpperCase();
    const side = f && f.side;
    const q = String(f && f.qty != null ? f.qty : '').trim(), p = String(f && f.limit != null ? f.limit : '').trim();
    const qty = /^\d{1,7}$/.test(q) ? parseInt(q, 10) : NaN;
    const px = /^\d{1,7}(\.\d{1,4})?$/.test(p) ? parseFloat(p) : NaN;
    if(!/^[A-Z][A-Z0-9.\-]{0,9}$/.test(symbol) || (side !== 'buy' && side !== 'sell') || !(qty >= 1) || !(px > 0)) return {ok: false, code: 'bad_intent'};
    return {ok: true, intent: {symbol, side, qty, limit_price: px}};
  }
  function holdProgress(heldMs){ return Math.max(0, Math.min(1, heldMs / HOLD_MS)); }

  const api = {HOLD_MS, ON_SCREEN_MS, POLL_MS, REFUSALS, STATES, WARNINGS, WARNING_LINE, DEMO_LINE, refusalText, stateText, haltText, actionsFor, isLive,
    fmtQty, fmtPrice, fmtMoney, optionLabel, countdown, accountLine, readForm, holdProgress};
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  if(typeof window === 'undefined' || typeof document === 'undefined') return;
  root.pxRh = api;

  // ---- DOM ------------------------------------------------------------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text != null) n.textContent = String(text);
    return n;
  };
  const streaming = () => document.body.classList.contains('streaming');
  const S = {open: false, snap: null, tickets: null, timer: 0, form: null, busy: false, msg: null, confirm: null, hold: null, tick: 0};

  async function csrf(){
    const r = await fetch('/api/csrf', {credentials: 'same-origin'});
    if(!r.ok) throw new Error('csrf');
    return (await r.json()).token;
  }
  async function post(path, body){
    const go = async () => fetch(path, {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'x-px-csrf': await csrf()}, body: JSON.stringify(body || {})});
    let r = await go();
    if(r.status === 403) r = await go();                 // a restarted desk mints a new token
    if(!r.ok) return {ok: false, code: 'http_' + r.status};
    return r.json();
  }
  async function getJson(path){
    const q = streaming() ? '?stream=1' : '';
    const r = await fetch(path + q, {credentials: 'same-origin'});
    if(!r.ok) throw new Error('http');
    return r.json();
  }

  let panel, sw, banner, body, topHost, formHost, listHost;
  function build(){
    sw = el('div', 'rh-switch');
    sw.setAttribute('role', 'group'); sw.setAttribute('aria-label', 'Broker');
    const bpx = el('button', 'on', 'ProjectX'), brh = el('button', '', 'Robinhood');
    bpx.type = brh.type = 'button';
    bpx.onclick = () => setOpen(false);
    brh.onclick = () => setOpen(true);
    sw.append(bpx, brh);
    sw._px = bpx; sw._rh = brh;
    panel = el('section', 'rh-panel');
    panel.hidden = true;
    panel.setAttribute('aria-label', 'Robinhood');
    banner = el('div', 'rh-demo', DEMO_LINE);
    banner.hidden = true;
    const head = el('div', 'rh-head');
    head.append(el('h2', '', 'Robinhood'));
    const close = el('button', 'rh-x', 'Back to ProjectX');
    close.type = 'button'; close.onclick = () => setOpen(false);
    head.append(close);
    body = el('div', 'rh-body');
    topHost = el('div'); formHost = el('div'); listHost = el('div');   // the form host is NOT rebuilt by the poll: typing and a hold survive it
    body.append(topHost, formHost, listHost);
    panel.append(head, banner, body);
    document.body.append(panel, sw);
  }

  function setOpen(on){
    S.open = !!on;
    panel.hidden = !S.open;
    sw._px.classList.toggle('on', !S.open); sw._rh.classList.toggle('on', S.open);
    document.body.classList.toggle('rh-open', S.open);
    clearInterval(S.timer); clearInterval(S.tick);
    if(S.open){ renderForm(); refresh(); S.timer = setInterval(() => { if(!document.hidden) refresh(); }, POLL_MS); S.tick = setInterval(tickClock, 1000); }
  }

  async function refresh(){
    try{
      const [s, t] = await Promise.all([getJson('/api/rh/state'), getJson('/api/rh/tickets')]);
      S.snap = s; S.tickets = t;
    }catch(_){ S.snap = {error: true}; }
    render();
  }

  // a table whose cells are all text; cols = [[header, fn(row) -> string]]
  function table(rows, cols, emptyText){
    if(!rows || !rows.length) return el('p', 'rh-none', emptyText);
    const t = el('table', 'rh-table'), hr = el('tr');
    cols.forEach((c) => hr.append(el('th', '', c[0])));
    t.append(hr);
    rows.forEach((r) => {
      const tr = el('tr');
      cols.forEach((c) => tr.append(el('td', '', c[1](r))));
      t.append(tr);
    });
    return t;
  }
  function section(title, ...kids){
    const s = el('div', 'rh-sec');
    s.append(el('h3', '', title), ...kids);
    return s;
  }
  const dash = (v) => (v == null ? '?' : String(v));
  const money = (n) => (streaming() ? 'hidden' : fmtMoney(n));

  function render(){
    if(!S.open) return;
    const snap = S.snap || {};
    banner.hidden = !snap.demo;
    const out = [];
    if(snap.error){ out.push(el('p', 'rh-bad', 'Could not reach the desk. Retrying.')); topHost.replaceChildren(...out); listHost.replaceChildren(); return; }
    if(snap.enabled === false){ out.push(el('p', 'rh-bad', 'Robinhood is off on this desk (PX_RH=0).')); topHost.replaceChildren(...out); listHost.replaceChildren(); return; }
    const tk = S.tickets || {};
    if(tk.halt){
      const h = el('div', 'rh-halt');
      h.append(el('p', '', 'ROBINHOOD HALTED: ' + haltText(tk.halt)), el('p', 'rh-note', 'Nothing new goes out until you clear it. Check the Robinhood app first.'));
      h.append(confirmBtn('halt', 'Clear halt', 'Clear the Robinhood halt? Only do this after checking the Robinhood app.', () => act('/api/rh/halt/resolve', {})));
      out.push(h);
    }
    if(!snap.connected) out.push(el('p', 'rh-bad', 'Not connected to Robinhood. Sign in (scripts/rh_capture.py), then reload.'));
    out.push(el('p', 'rh-acct', accountLine(snap.account, snap.approval_setting)));
    if(snap.ok === false && snap.unknown && snap.unknown.length) out.push(el('p', 'rh-bad', 'Could not read: ' + snap.unknown.join(', ') + '. Unknown is shown as unknown, never as empty.'));
    if(snap.pnl && !streaming() && snap.pnl.realized != null) out.push(el('p', 'rh-note', 'Realized P&L (' + snap.pnl.window + '): ' + fmtMoney(snap.pnl.realized)));

    const pos = snap.positions;
    const eq = (pos || []).filter((p) => p.asset !== 'option'), op = (pos || []).filter((p) => p.asset === 'option');
    out.push(section('Positions', pos == null ? el('p', 'rh-bad', 'Unknown: could not be read.') :
      table(eq, [['Symbol', (p) => p.symbol], ['Side', (p) => dash(p.side)], ['Qty', (p) => fmtQty(p.qty)], ['Avg price', (p) => (p.avg_price == null ? (streaming() ? 'hidden' : '?') : money(p.avg_price))]], 'No stock positions.')));
    out.push(section('Option positions (read only: the desk places no option orders)', pos == null ? el('p', 'rh-bad', 'Unknown.') :
      table(op, [['Contract', optionLabel], ['Side', (p) => dash(p.side)], ['Qty', (p) => fmtQty(p.qty)]], 'No option positions.')));
    out.push(section('Working orders', snap.orders == null ? el('p', 'rh-bad', 'Unknown: could not be read.') :
      table(snap.orders, [['Symbol', (o) => o.symbol], ['Side', (o) => dash(o.side)], ['Qty', (o) => fmtQty(o.qty)], ['Limit', (o) => (o.price == null ? '' : money(o.price))], ['Status', (o) => dash(o.status)]], 'No orders.')));
    const pend = (snap.approvals || []).filter((a) => a.status === 'pending');
    out.push(section('Pending approvals (tap them in the Robinhood app)', snap.approvals == null ? el('p', 'rh-bad', 'Unknown: could not be read.') :
      table(pend, [['Symbol', (a) => dash(a.symbol)], ['Side', (a) => dash(a.side)], ['Qty', (a) => fmtQty(a.qty)]], 'Nothing waiting for you.')));
    topHost.replaceChildren(...out);
    listHost.replaceChildren(ticketList(tk));
  }
  function renderForm(){ formHost.replaceChildren(ticketForm()); }

  // ---- confirm step: a destructive button asks once, in place -------------------------------------------------------------------------
  function confirmBtn(key, label, question, run){
    const wrap = el('span', 'rh-confirm');
    if(S.confirm === key){
      wrap.append(el('span', 'rh-ask', question + ' '));
      const yes = el('button', 'rh-danger', 'Yes, ' + label.toLowerCase()), no = el('button', '', 'No');
      yes.type = no.type = 'button';
      yes.onclick = () => { S.confirm = null; run(); };
      no.onclick = () => { S.confirm = null; render(); };
      wrap.append(yes, no);
    }else{
      const b = el('button', '', label);
      b.type = 'button';
      b.onclick = () => { S.confirm = key; render(); };
      wrap.append(b);
    }
    return wrap;
  }

  async function act(path, bodyObj){
    if(S.busy) return;
    S.busy = true;
    let r;
    try{ r = await post(path, bodyObj); }catch(_){ r = {ok: false, code: 'net'}; }
    S.busy = false;
    S.msg = r && r.ok ? {ok: true, text: r.outcome ? 'Result: ' + r.outcome.replace(/_/g, ' ') + '.' : (r.state ? 'Ticket is now ' + r.state.replace(/_/g, ' ') + '.' : 'Done.')}
      : {ok: false, text: r && r.code && r.code.indexOf('http_') !== 0 && r.code !== 'net' ? refusalText(r.code) : 'The request did not go through. Check the Robinhood app before trying again.'};
    renderForm();
    await refresh();
  }

  function ticketList(tk){
    const rows = (tk.tickets || []).slice().reverse();
    const wrap = section('Tickets');
    if(!rows.length){ wrap.append(el('p', 'rh-none', 'No tickets yet.')); return wrap; }
    rows.forEach((t) => {
      const card = el('div', 'rh-tk st-' + t.state);
      const line = t.symbol + ' ' + t.side + ' ' + fmtQty(t.qty) + (t.limit_price == null ? '' : ' @ ' + money(t.limit_price)) + '  [' + t.hash8 + ']';
      card.append(el('div', 'rh-tk-top', line), el('div', 'rh-tk-state', t.state.replace(/_/g, ' ').toUpperCase()), el('p', 'rh-note', stateText(t.state)));
      const a = actionsFor(t.state), bar = el('div', 'rh-btns');
      if(a.cancel) bar.append(confirmBtn('c' + t.ticket_id, 'Cancel', 'Cancel this order at Robinhood?', () => act('/api/rh/cancel', {ticket_id: t.ticket_id})));
      if(a.reconcile) bar.append(confirmBtn('r' + t.ticket_id, 'Reconcile', 'Look this ticket up at Robinhood?', () => act('/api/rh/reconcile', {ticket_id: t.ticket_id})));
      if(a.release) bar.append(confirmBtn('l' + t.ticket_id, 'Release', 'Stop tracking it? Only if the Robinhood app shows nothing for it.', () => act('/api/rh/release', {ticket_id: t.ticket_id})));
      if(bar.childNodes.length) card.append(bar);
      wrap.append(card);
    });
    return wrap;
  }

  // ---- the order ticket ------------------------------------------------------------------------------------------------------------------
  function warnBox(){
    const w = el('div', 'rh-warn');
    w.append(el('strong', '', 'READ THIS. '), document.createTextNode(WARNING_LINE));
    return w;
  }

  function ticketForm(){
    const wrap = section('Order ticket (stocks, whole shares, limit orders)');
    wrap.append(warnBox());
    const f = S.form;
    if(S.msg){ wrap.append(el('p', S.msg.ok ? 'rh-ok' : 'rh-bad', S.msg.text)); }
    if(f && f.ticket){                                   // a previewed ticket is on screen: read-only, with the hold button
      const t = f.ticket;
      const box = el('div', 'rh-ticket');
      box.append(el('div', 'rh-tk-top', t.side.toUpperCase() + ' ' + fmtQty(t.qty) + ' ' + t.symbol + ' limit ' + (streaming() ? 'hidden' : fmtPrice(t.limit_price)) +
        (streaming() ? '' : ' (about ' + fmtMoney(t.notional) + ')')));
      box.append(el('div', 'rh-hash', 'Ticket code ' + t.hash8));
      const cd = el('div', 'rh-cd'); cd.id = 'rh-cd'; cd.textContent = 'Expires in ' + countdown(t.expires_at, Date.now() / 1000);
      box.append(cd);
      const btns = el('div', 'rh-btns');
      const place = el('button', 'rh-place', 'PLACE (hold ' + HOLD_MS + ' ms)');
      place.type = 'button'; place.id = 'rh-place';
      wireHold(place, t);
      const dis = el('button', '', 'Discard'); dis.type = 'button';
      dis.onclick = () => discard(t);
      btns.append(place, dis);
      box.append(btns, el('p', 'rh-note', 'A click does nothing: press and hold. Placing sends the order to Robinhood and waits for your tap in the Robinhood app.'));
      wrap.append(box);
      return wrap;
    }
    const form = el('form', 'rh-form');
    const sym = el('input'); sym.placeholder = 'Ticker'; sym.maxLength = 10; sym.autocomplete = 'off'; sym.setAttribute('aria-label', 'Ticker');
    const side = el('select'); side.setAttribute('aria-label', 'Buy or sell');
    [['buy', 'Buy to open'], ['sell', 'Sell to close']].forEach((o) => { const op = el('option', '', o[1]); op.value = o[0]; side.append(op); });
    const qty = el('input'); qty.placeholder = 'Shares'; qty.inputMode = 'numeric'; qty.autocomplete = 'off'; qty.setAttribute('aria-label', 'Whole shares');
    const lim = el('input'); lim.placeholder = 'Limit price'; lim.inputMode = 'decimal'; lim.autocomplete = 'off'; lim.setAttribute('aria-label', 'Limit price');
    const go = el('button', '', 'Preview'); go.type = 'submit';
    form.append(sym, side, qty, lim, go);
    const err = el('p', 'rh-bad'); err.hidden = true;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const r = readForm({symbol: sym.value, side: side.value, qty: qty.value, limit: lim.value});
      if(!r.ok){ err.textContent = refusalText(r.code); err.hidden = false; return; }
      err.hidden = true; go.disabled = true;
      let out;
      try{ out = await post('/api/rh/preview', r.intent); }catch(_){ out = {ok: false, code: 'net'}; }
      if(out && out.ok && out.ticket){ S.msg = null; S.form = {ticket: out.ticket, shownAt: Date.now()}; renderForm(); return; }
      go.disabled = false;
      err.textContent = out && out.code && out.code.indexOf('http_') !== 0 && out.code !== 'net' ? refusalText(out.code) : 'The request did not go through.';
      err.hidden = false;
    };
    wrap.append(form, err);
    return wrap;
  }

  async function discard(t){
    S.form = null;
    try{ await post('/api/rh/discard', {ticket_id: t.ticket_id}); }catch(_){ /* it expires on its own */ }
    S.msg = {ok: true, text: 'Discarded. Nothing was sent to Robinhood.'};
    renderForm();
  }

  function tickClock(){
    const c = $('rh-cd');
    if(c && S.form && S.form.ticket){
      const left = countdown(S.form.ticket.expires_at, Date.now() / 1000);
      c.textContent = left === 'expired' ? 'Expired. Preview again.' : 'Expires in ' + left;
      if(left === 'expired'){ const p = $('rh-place'); if(p) p.disabled = true; }
    }
  }

  // 600 ms hold, pointer or Space/Enter. The ticket must have been on screen ON_SCREEN_MS first, like the ProjectX ticket.
  function wireHold(btn, t){
    const cancel = () => {
      if(S.hold){ cancelAnimationFrame(S.hold.raf); S.hold = null; }
      btn.classList.remove('holding'); btn.style.removeProperty('--hold');
    };
    const start = () => {
      if(S.hold || S.busy) return;
      if(Date.now() - S.form.shownAt < ON_SCREEN_MS) return;
      if(countdown(t.expires_at, Date.now() / 1000) === 'expired') return;
      const h = S.hold = {t0: performance.now(), raf: 0};
      btn.classList.add('holding');
      const step = () => {
        if(S.hold !== h) return;
        const p = holdProgress(performance.now() - h.t0);
        btn.style.setProperty('--hold', String(p));
        if(p >= 1){ cancel(); placeNow(t); return; }
        h.raf = requestAnimationFrame(step);
      };
      h.raf = requestAnimationFrame(step);
    };
    btn.addEventListener('pointerdown', (e) => { if(e.button === 0 || e.pointerType !== 'mouse') start(); });
    ['pointerup', 'pointerleave', 'pointercancel', 'blur'].forEach((ev) => btn.addEventListener(ev, cancel));
    btn.addEventListener('keydown', (e) => { if((e.key === ' ' || e.key === 'Enter') && !e.repeat){ e.preventDefault(); start(); } });
    btn.addEventListener('keyup', (e) => { if(e.key === ' ' || e.key === 'Enter') cancel(); });
    btn.addEventListener('click', (e) => e.preventDefault());
    btn.addEventListener('contextmenu', (e) => e.preventDefault());
  }

  async function placeNow(t){
    if(S.busy) return;
    S.busy = true;
    S.form = null;                                       // one placement per ticket: the form is gone before the reply arrives
    renderForm();
    let r;
    try{ r = await post('/api/rh/place', {ticket_id: t.ticket_id, confirm_hash: t.hash8}); }catch(_){ r = {ok: false, code: 'net'}; }
    S.busy = false;
    if(r && r.ok) S.msg = {ok: true, text: 'Sent. Now approve it in the Robinhood app. Until you do, nothing happens.'};
    else if(r && r.code && r.code.indexOf('http_') !== 0 && r.code !== 'net') S.msg = {ok: false, text: refusalText(r.code)};
    else S.msg = {ok: false, text: 'No clear answer. Do NOT press again: check the Robinhood app and the ticket list, then reconcile.'};
    renderForm();
    refresh();
  }

  // ---- boot: show the switch only when the desk has Robinhood on ------------------------------------------------------------------------------
  async function boot(){
    let s;
    try{ s = await getJson('/api/rh/state'); }catch(_){ return; }     // no route (record-only) or desk down: no switch
    if(!s || s.enabled !== true) return;
    build();
    S.snap = s;
    let was = streaming();                               // stream mode flipped: redraw so no dollar value stays on screen
    new MutationObserver(() => { const now = streaming(); if(now !== was){ was = now; if(S.open){ renderForm(); refresh(); } } })
      .observe(document.body, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('visibilitychange', () => { if(S.open && !document.hidden) refresh(); });
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})(typeof window !== 'undefined' ? window : globalThis);
