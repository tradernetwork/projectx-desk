// Public tab (Round 36): a READ-ONLY view of what the separate Public desk reports, fetched by this desk from its loopback link.
// Display only. One GET route (/api/pub/state), no POST, no storage, no console; every string goes in through textContent.
// The tab exists only when the desk answers that route (PX_PUB_URL set, never in a recorder). It joins the Robinhood switch
// when rh.js has drawn one, else it draws its own ProjectX | Public switch; rh.js is not edited.
(function(root){
  'use strict';
  const NOTE = 'Display only. Orders happen in the Public desk, not here.';
  const ROUTE = '/api/pub/state';
  const POLL_MS = 5000;
  const CODE_WORDS = {
    unset: 'the link is not configured',
    unreachable: 'the Public desk did not answer (is it running?)',
    timeout: 'the Public desk did not answer within 3 seconds',
    bad_status: 'the Public desk answered with an error',
    too_big: 'the Public desk sent more than this tab reads',
    bad_json: 'the Public desk sent something this tab cannot read',
    not_allowed: 'PX_PUB_URL is not a loopback http address, so nothing is fetched',
  };

  // ---- pure helpers (node-tested) -----------------------------------------------------------------------------------------
  const U = 'UNKNOWN';
  function codeText(code){ return CODE_WORDS[code] || CODE_WORDS.unreachable; }
  function yn(v){ return v === true ? 'on' : v === false ? 'off' : U; }
  // One cell: the value, UNKNOWN when the Public desk listed the field as unreadable, '-' when it does not apply.
  function cell(row, key){
    if(row && row[key] != null && row[key] !== '') return String(row[key]);
    if(row && Array.isArray(row.unknown) && row.unknown.indexOf(key) >= 0) return U;
    return '-';
  }
  function ageText(age){
    if(typeof age !== 'number' || !isFinite(age) || age < 0) return 'data age ' + U;
    return 'data ' + Math.floor(age) + ' s old';
  }
  function statusLine(v){
    if(!v || v.reachable !== true) return 'Public desk not readable: ' + codeText(v && v.code) + '. Everything below is UNKNOWN.';
    const m = v.mode || {}, d = v.data || {};
    let s = ageText(v.age_s) + '. Trading ' + yn(m.trading) + ', options ' + yn(m.options) + '.';
    if(d.as_of) s += ' Read at ' + d.as_of + '.';
    if(Array.isArray(d.unknown) && d.unknown.length) s += ' Not readable there: ' + d.unknown.join(', ') + '.';
    return s;
  }
  function haltLine(v){
    if(!v || v.reachable !== true) return null;
    const h = v.halt || {};
    if(h.halted === true) return 'The Public desk is HALTED (cause: ' + (h.cause || 'unknown') + '). Only a human clears it, in the Public desk.';
    if(h.halted == null) return 'Public desk halt state: ' + U + '.';
    return null;
  }
  function accountLine(v){
    if(!v || v.account == null) return 'Account: ' + U;
    const a = v.account;
    let s = 'Account: ' + cell(a, 'type') + ', ' + cell(a, 'permissions') + ', options ' + cell(a, 'options_level');
    if('balances' in v){                               // stream mode removes the block: no dollar is drawn
      const b = v.balances;
      s += b == null ? '. Balances ' + U : '. Equity ' + cell(b, 'equity') + ', cash ' + cell(b, 'cash') + ', buying power ' + cell(b, 'buying_power');
    }
    return s;
  }
  const money = (v) => !(v && v.stream);
  // Rows the link could not read (or a list it cut) for one section: {unreadable, more} or null.
  function meta(v, name){
    const m = v && v.incomplete && v.incomplete[name];
    return m && (m.unreadable > 0 || m.more) ? {unreadable: m.unreadable > 0 ? m.unreadable : 0, more: !!m.more} : null;
  }
  // The words under a table when it is not the whole list: an unreadable row is never shown as "no rows".
  function incompleteText(t){
    if(!t || (!t.unreadable && !t.more)) return null;
    const bits = [];
    if(t.unreadable) bits.push(t.unreadable + (t.unreadable === 1 ? ' row' : ' rows') + ' the Public desk sent could not be read here');
    if(t.more) bits.push('the list was cut at ' + t.rows.length + ' rows');
    return 'INCOMPLETE: ' + bits.join('; ') + '. Check the Public desk itself.';
  }
  // A reply belongs on screen only when its stream flag matches the mode now: a non-stream reply that lands after stream
  // mode was switched on carries dollars and is dropped (and fetched again).
  function acceptReply(v, streamingNow){ return !!v && (v.stream === true) === !!streamingNow; }
  function positionRows(v){
    const cols = [['Symbol', 'symbol'], ['Asset', 'asset'], ['Qty', 'qty']];
    if(money(v)) cols.push(['Cost', 'cost_basis'], ['Value', 'market_value'], ['Unrealized', 'unrealized_pl']);
    return table(v && v.positions, cols, meta(v, 'positions'));
  }
  function orderRows(v){
    const cols = [['Symbol', 'symbol'], ['Side', 'side'], ['Type', 'type'], ['Status', 'status'], ['Qty', 'qty'], ['Filled', 'filled_qty']];
    if(money(v)) cols.push(['Price', 'price'], ['Avg', 'avg_price']);
    return table(v && v.orders, cols, meta(v, 'orders'));
  }
  function ticketRows(v){
    const cols = [['State', 'state'], ['Symbol', 'symbol'], ['Side', 'side'], ['Asset', 'asset'], ['Qty', 'qty'], ['Purpose', 'purpose']];
    if(money(v)) cols.push(['Limit', 'limit_price']);
    return table(v && v.tickets, cols, meta(v, 'tickets'));
  }
  function basisText(c){
    if(c.basis_state === 'KNOWN') return c.basis != null ? c.basis : 'KNOWN (hidden in stream mode)';
    if(c.basis_state === 'NOSHARES') return 'no shares';
    return U;
  }
  function cycleRows(v){
    const w = v && v.wheel;
    if(!w || w.cycles == null) return null;
    return withMeta({head: ['Underlying', 'Open', 'Shares', 'True basis', 'Legs'],
      rows: w.cycles.map((c) => [c.underlying, c.open ? 'yes' : 'no', c.shares == null ? U : c.shares, basisText(c), c.legs == null ? U : String(c.legs)])},
      meta(v, 'cycles'));
  }
  function washRows(v){
    const w = v && v.wheel;
    if(!w || w.wash_mode === 'off') return {off: true};
    if(w.wash == null) return null;
    return withMeta({head: ['Underlying', 'Wash flag', 'Window'],
      rows: w.wash.map((r) => [r.underlying, r.flag, (r.window_start || '?') + ' to ' + (r.window_end || '?')])}, meta(v, 'wash'));
  }
  function wheelLine(v){
    const w = v && v.wheel;
    if(!w) return 'Wheel: ' + U;
    const s = w.summary || {};
    const n = (k) => (s[k] == null ? U : String(s[k]));
    return 'Wheel: ' + n('cycles_open') + ' cycles open, ' + n('legs_open') + ' legs open, ' + n('basis_unknown') + ' with unknown basis, '
      + n('rolls_active') + ' rolls active, ' + n('rolls_stuck') + ' stuck' + (s.ledger ? ', ledger ' + s.ledger : '') + '.';
  }
  function withMeta(t, m){ t.unreadable = m ? m.unreadable : 0; t.more = m ? m.more : false; return t; }
  function table(rows, cols, m){
    if(rows == null) return null;                      // a section the Public desk could not read: UNKNOWN, never empty
    return withMeta({head: cols.map((c) => c[0]), rows: rows.map((r) => cols.map((c) => cell(r, c[1])))}, m);
  }
  function withStream(path, on){ return on ? path + '?stream=1' : path; }

  const api = {NOTE, ROUTE, codeText, cell, ageText, statusLine, haltLine, accountLine, positionRows, orderRows, ticketRows,
    cycleRows, washRows, wheelLine, withStream, acceptReply, incompleteText};
  if(typeof module !== 'undefined' && module.exports) module.exports = api;
  if(typeof window === 'undefined' || typeof document === 'undefined') return;
  root.pxPub = api;

  // ---- DOM --------------------------------------------------------------------------------------------------------------------
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if(cls) n.className = cls;
    if(text != null) n.textContent = String(text);
    return n;
  };
  const streaming = () => document.body.classList.contains('streaming');
  const S = {open: false, view: null, timer: 0, busy: false};
  let panel, bodyHost, btn, own, ownPx, host, opening = false;

  async function fetchOnce(){
    const r = await fetch(withStream(ROUTE, streaming()), {credentials: 'same-origin'});
    if(!r.ok) throw new Error('http');
    return r.json();
  }
  // A reply whose stream flag no longer matches the mode is dropped and fetched again at once (at most twice more).
  async function load(){
    for(let i = 0; i < 3; i++){
      const v = await fetchOnce();
      if(acceptReply(v, streaming())) return v;
    }
    return null;
  }

  function build(){
    btn = el('button', '', 'Public');
    btn.type = 'button';
    btn.onclick = () => setOpen(true);
    panel = el('section', 'rh-panel pub-panel');
    panel.hidden = true;
    panel.setAttribute('aria-label', 'Public');
    const head = el('div', 'rh-head');
    head.append(el('h2', '', 'Public'));
    const close = el('button', 'rh-x', 'Back to ProjectX');
    close.type = 'button'; close.onclick = () => setOpen(false);
    head.append(close);
    bodyHost = el('div', 'rh-body');
    panel.append(head, el('div', 'pub-note', NOTE), bodyHost);
    document.body.append(panel);
    attach();
    new MutationObserver(attach).observe(document.body, {childList: true});
  }

  // Join the Robinhood switch when there is one; otherwise draw a ProjectX | Public switch of our own. If rh.js draws its switch
  // later, the Public button moves into it and ours is removed, so there is never more than one switch on screen.
  function attach(){
    const rh = Array.prototype.find.call(document.querySelectorAll('.rh-switch'), (n) => n !== own);
    if(rh){
      if(own){ own.remove(); own = null; ownPx = null; }
      if(host !== rh){ host = rh; rh.append(btn); rh.addEventListener('click', onSwitch, true); }
    }else if(!own){
      own = el('div', 'rh-switch pub-own');
      own.setAttribute('role', 'group'); own.setAttribute('aria-label', 'Broker');
      ownPx = el('button', 'on', 'ProjectX');
      ownPx.type = 'button';
      own.append(ownPx, btn);
      own.addEventListener('click', onSwitch, true);
      document.body.append(own);
      host = own;
    }
    mark();
  }
  // Any other button on the switch (ProjectX, Robinhood) closes the Public panel first; its own handler then runs as before.
  function onSwitch(e){
    const b = e.target && e.target.closest ? e.target.closest('button') : null;
    if(!b || b === btn || opening) return;
    if(S.open) setOpen(false);
  }
  function mark(){
    if(!host || !btn) return;
    btn.classList.toggle('on', S.open);
    if(S.open) Array.prototype.forEach.call(host.querySelectorAll('button'), (b) => { if(b !== btn) b.classList.remove('on'); });
    else if(host === own && ownPx) ownPx.classList.add('on');
  }

  function setOpen(on){
    if(on && document.body.classList.contains('rh-open') && host && host !== own){
      const px = host.querySelector('button');           // the Robinhood switch's ProjectX button closes its panel
      opening = true;
      try{ if(px && px !== btn) px.click(); } finally { opening = false; }
    }
    S.open = !!on;
    panel.hidden = !S.open;
    document.body.classList.toggle('pub-open', S.open);
    clearInterval(S.timer);
    mark();
    if(S.open){ render(); refresh(); S.timer = setInterval(() => { if(!document.hidden) refresh(); }, POLL_MS); }
  }

  async function refresh(){
    if(S.busy) return;
    S.busy = true;
    try{ S.view = await load(); }catch(_){ S.view = {enabled: true, reachable: false, code: 'unreachable', stream: streaming()}; }
    finally{ S.busy = false; }
    render();
  }

  function drawTable(t, emptyText){
    if(t == null) return el('p', 'rh-bad', U + ': the Public desk could not read this.');
    const inc = incompleteText(t);
    if(!t.rows.length) return inc ? el('p', 'rh-bad', inc) : el('p', 'rh-none', emptyText);
    const tb = el('table', 'rh-table'), hr = el('tr');
    t.head.forEach((h) => hr.append(el('th', '', h)));
    tb.append(hr);
    t.rows.forEach((r) => {
      const tr = el('tr');
      r.forEach((c) => tr.append(el('td', c === U ? 'pub-unk' : '', c)));
      tb.append(tr);
    });
    if(!inc) return tb;
    const box = el('div');
    box.append(tb, el('p', 'rh-bad', inc));
    return box;
  }
  function section(title, ...kids){
    const s = el('div', 'rh-sec');
    s.append(el('h3', '', title), ...kids);
    return s;
  }

  function render(){
    if(!bodyHost) return;
    const v = S.view && acceptReply(S.view, streaming()) ? S.view : null;   // never draw a reply from the other mode
    const kids = [];
    const halt = haltLine(v);
    if(halt) kids.push(el('div', 'rh-halt', halt));
    kids.push(el('p', v && v.reachable === true ? 'rh-note' : 'rh-bad', v ? statusLine(v) : 'Loading.'));
    if(v){
      kids.push(el('p', 'rh-acct', accountLine(v)));
      kids.push(section('Positions', drawTable(positionRows(v), 'No positions.')));
      kids.push(section('Working orders', drawTable(orderRows(v), 'No orders.')));
      kids.push(section('Tickets in the Public desk', drawTable(ticketRows(v), 'No tickets.')));
      const wash = washRows(v);
      kids.push(section('Wheel', el('p', 'rh-note', wheelLine(v)), drawTable(cycleRows(v), 'No wheel cycles.'),
        wash && wash.off ? el('p', 'rh-note', 'Wash sale watch is off in the Public desk.')
          : drawTable(wash, 'No wash sale window open.'),
        el('p', 'rh-note', 'Wash flags are a bookkeeping watch, not tax advice.')));
    }
    bodyHost.replaceChildren(...kids);
  }

  // ---- boot: the tab exists only when the desk answers the route ---------------------------------------------------------------
  async function boot(){
    let v;
    try{ v = await fetchOnce(); }catch(_){ return; }    // no route: PX_PUB_URL unset, a recorder, or the desk is down
    if(!v || v.enabled !== true) return;
    S.view = v;
    build();
    let was = streaming();                              // stream mode flipped: refetch so no dollar value stays on screen
    new MutationObserver(() => { const now = streaming(); if(now !== was){ was = now; S.view = null; render(); if(S.open) refresh(); } })
      .observe(document.body, {attributes: true, attributeFilter: ['class']});
    document.addEventListener('visibilitychange', () => { if(S.open && !document.hidden) refresh(); });
  }
  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})(typeof window !== 'undefined' ? window : globalThis);
