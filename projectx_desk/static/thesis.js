'use strict';
// Thesis card: a few taps to state the idea (type, target, invalidation, time limit), then plain-number facts against it.
// Display and logging only. The only POST is /api/thesis (the desk's own notebook); nothing here can reach an order, a ticket or a halt.
(() => {
  const TYPES = [['REVERSAL_HOLD', 'Reversal hold'], ['SCALP', 'Scalp'], ['FADE_RANGE', 'Fade the range'], ['TREND_ADD', 'Trend add']];
  const PRESET_S = {REVERSAL_HOLD: 3600, SCALP: 300, FADE_RANGE: 1800, TREND_ADD: 3600};
  const st = {type: null, target: null, inv: null, limit: null, levels: [], open: false};
  const el = (tag, cls, text) => {const e = document.createElement(tag); if(cls) e.className = cls; if(text != null) e.textContent = text; return e};
  const cid = () => { try { return typeof S !== 'undefined' && S.contractId ? S.contractId : ''; } catch(_) { return ''; } };
  const num = (v) => typeof v === 'number' && isFinite(v);

  const fab = el('button', 'thesis-fab', 'Thesis'); fab.type = 'button'; fab.setAttribute('aria-expanded', 'false');
  const card = el('section', 'thesis-card'); card.hidden = true; card.setAttribute('role', 'dialog'); card.setAttribute('aria-label', 'Thesis card');
  const head = el('div', 'thesis-h'); head.append(el('strong', '', 'Thesis'), el('span', 'dim mono', 'notes only, never an order'));
  const close = el('button', 'ghost', '×'); close.type = 'button'; close.setAttribute('aria-label', 'Close thesis card'); head.append(close);
  const types = el('div', 'thesis-row'), limits = el('div', 'thesis-row'), chipsT = el('div', 'thesis-row'), chipsI = el('div', 'thesis-row');
  const tIn = el('input', 'mono'), iIn = el('input', 'mono');
  for(const i of [tIn, iIn]){ i.type = 'number'; i.step = 'any'; i.inputMode = 'decimal'; }
  tIn.placeholder = 'target'; iIn.placeholder = 'invalidation';
  const save = el('button', '', 'Save'); save.type = 'button';
  const status = el('div', 'thesis-facts'); status.setAttribute('aria-live', 'polite');
  card.append(head, el('div', 'dim', 'Type'), types, el('div', 'dim', 'Time limit'), limits, el('div', 'dim', 'Target'), tIn, chipsT,
              el('div', 'dim', 'Invalidation'), iIn, chipsI, save, status);

  function sync(){
    for(const b of types.children) b.classList.toggle('on', b.dataset.v === st.type);
    for(const b of limits.children) b.classList.toggle('on', Number(b.dataset.v) === st.limit);
    tIn.value = num(st.target) ? st.target : ''; iIn.value = num(st.inv) ? st.inv : '';
  }
  for(const [v, label] of TYPES){
    const b = el('button', 'chip', label); b.type = 'button'; b.dataset.v = v;
    b.onclick = () => {st.type = v; st.limit = PRESET_S[v]; sync()};
    types.append(b);
  }
  for(const [s, label] of [[300, '5m'], [900, '15m'], [1800, '30m'], [3600, '1h']]){
    const b = el('button', 'chip', label); b.type = 'button'; b.dataset.v = String(s);
    b.onclick = () => {st.limit = s; sync()};
    limits.append(b);
  }
  tIn.oninput = () => {st.target = tIn.value === '' ? null : Number(tIn.value)};
  iIn.oninput = () => {st.inv = iIn.value === '' ? null : Number(iIn.value)};

  function chips(host, key){
    host.replaceChildren();
    for(const l of st.levels){
      const b = el('button', 'chip', l.name + ' ' + l.price); b.type = 'button';
      b.onclick = () => {st[key] = l.price; sync()};
      host.append(b);
    }
  }
  async function loadLevels(){
    const c = cid(); st.levels = [];
    if(c){
      try{
        const r = await fetch('/api/respect?contract=' + encodeURIComponent(c), {credentials: 'same-origin'});
        const d = r.ok ? await r.json() : null;
        const seen = new Set();
        for(const tf of ['5m', '15m', '1h', '4h', '1d']){
          const t = d && d.tfs && d.tfs[tf];
          for(const l of (t && Array.isArray(t.levels) ? t.levels : [])){
            if(!l || (l.verdict !== 'RESPECTED' && l.verdict !== 'ABOVE_RANDOM') || !num(l.price) || seen.has(l.price)) continue;
            seen.add(l.price);
            st.levels.push({name: tf + ' ' + String(l.id).replace(/[^\w .\-\/+%()]/g, '').slice(0, 16), price: l.price});
          }
        }
        st.levels.sort((a, b) => b.price - a.price); st.levels = st.levels.slice(0, 14);
      }catch(_){ /* chips are a convenience; typing still works */ }
    }
    chips(chipsT, 'target'); chips(chipsI, 'inv');
  }
  function paintFacts(d){
    status.replaceChildren();
    if(!d || !d.thesis){ status.append(el('div', 'dim', 'No thesis saved for this contract.')); return; }
    const t = d.thesis;
    status.append(el('div', 'mono', [t.type.replace('_', ' ').toLowerCase(), t.side || '', t.target != null ? 'target ' + t.target : '',
      t.invalidation != null ? 'inval ' + t.invalidation : '', t.limit_s ? 'limit ' + t.limit_s + 's' : ''].filter(Boolean).join(' | ')));
    const f = Array.isArray(d.facts) ? d.facts : [];
    for(const line of f) status.append(el('div', 'thesis-fact', String(line).slice(0, 120)));
    if(!f.length) status.append(el('div', 'dim', 'Nothing broken.'));
  }
  async function csrf(){
    const r = await fetch('/api/csrf', {credentials: 'same-origin'});
    if(!r.ok) throw new Error('csrf');
    return (await r.json()).token;
  }
  async function refresh(){
    const c = cid(); if(!c) return;
    try{
      const r = await fetch('/api/thesis?contract=' + encodeURIComponent(c), {credentials: 'same-origin'});
      if(r.ok) paintFacts(await r.json());
    }catch(_){}
  }
  save.onclick = async () => {
    const c = cid();
    if(!c || !st.type){ status.textContent = 'Pick a type first.'; return; }
    const go = async () => fetch('/api/thesis', {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'x-px-csrf': await csrf()},
      body: JSON.stringify({contract: c, type: st.type, target: num(st.target) ? st.target : null, invalidation: num(st.inv) ? st.inv : null, limit_s: st.limit})});
    try{
      let r = await go();
      if(r.status === 403) r = await go();
      if(!r.ok) throw new Error('HTTP ' + r.status);
      paintFacts(await r.json());
    }catch(e){ status.textContent = 'Not saved: ' + e.message; }
  };
  function open(on){
    st.open = on; card.hidden = !on; fab.setAttribute('aria-expanded', String(on));
    if(on){ sync(); loadLevels(); refresh(); }
  }
  fab.onclick = () => open(!st.open);
  close.onclick = () => {open(false); fab.focus()};
  card.onkeydown = (e) => {if(e.key === 'Escape'){open(false); fab.focus()}};
  setInterval(() => {if(st.open) refresh()}, 5000);
  fetch('/api/thesis?contract=CON.F.US.MES.X', {credentials: 'same-origin'}).then((r) => {
    if(r.status === 404 || r.status === 405) return;            // recorder mode has no thesis route: no button
    document.body.append(fab, card);
  }).catch(() => {});
})();
