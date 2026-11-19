'use strict';
// Ask box provider picker. Ported in behaviour from the TraderMatrix Desktop ExplainSetup; the rules mirror projectx_desk/llm_providers.py.
// The pure functions at the top are exported on window.pxLlmSetup (and module.exports under node) so tests/test_llmsetup.py can run them.
// A key is typed into a password input, read once at Save, POSTed, and the input is cleared. It is never put in the DOM as text,
// never stored in the browser, never logged. The server never returns it. Display only: no path to the guard or an order route.
(function(root){
  const MAX_URL = 200;
  const MODEL_OK = /^[A-Za-z0-9][A-Za-z0-9._\/:@+-]{0,99}$/;

  // ---- pure rules (mirror llm_providers.py) -----------------------------------------------------------------------------------
  function parseUrl(raw){
    const s = String(raw == null ? '' : raw).trim();
    if(!s || s.length > MAX_URL) return null;
    let u;
    try{ u = new URL(s); }catch(_){ return null; }
    if(u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if(!u.hostname || u.username || u.password || u.search || u.hash || s.indexOf('?') >= 0 || s.indexOf('#') >= 0) return null;
    if(u.host.indexOf('@') >= 0) return null;
    return u;
  }
  function isLoopbackHost(h){
    h = String(h || '').toLowerCase().replace(/^\[|\]$/g, '');
    if(!h) return false;
    if(h === 'localhost' || h === '::1') return true;
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
    return !!m && +m[1] === 127 && m.slice(1).every(x => +x <= 255);
  }
  function isPrivateLan(h){
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(String(h || ''));
    if(!m) return false;
    const o = m.slice(1).map(Number);
    if(o.some(x => x > 255)) return false;
    return o[0] === 10 || (o[0] === 172 && o[1] >= 16 && o[1] <= 31) || (o[0] === 192 && o[1] === 168) || (o[0] === 100 && o[1] >= 64 && o[1] <= 127);
  }
  function norm(u){ return u.protocol + '//' + u.host + u.pathname.replace(/\/+$/, ''); }
  function validateBaseUrl(raw){                                   // https anywhere, plain http only to loopback
    const u = parseUrl(raw);
    if(!u) return null;
    if(u.protocol === 'http:' && !isLoopbackHost(u.hostname)) return null;
    return norm(u);
  }
  function validateRemoteAgentUrl(raw, lan){                      // https anywhere; http to loopback, or to a private IPv4 literal with lan
    const u = parseUrl(raw);
    if(!u) return null;
    if(u.protocol === 'http:' && !(isLoopbackHost(u.hostname) || (lan && isPrivateLan(u.hostname)))) return null;
    return norm(u);
  }
  // card = one entry of GET /api/llm/detect providers. A card says what its form needs: takes_key, url_editable, kind, id.
  function keyShapeError(card, raw){
    const k = String(raw == null ? '' : raw).trim(), label = (card && card.label) || 'provider';
    if(!k) return 'Paste your ' + label + ' key first.';
    if(/\s/.test(k)) return 'The key must not contain spaces.';
    if(k.length > 256) return 'That key is too long.';
    const id = card && card.id;
    if(id === 'openrouter' && !(k.startsWith('sk-or-') && k.length > 6)) return 'OpenRouter keys start with sk-or-.';
    if(id === 'anthropic' && !(k.startsWith('sk-ant-') && k.length > 7)) return 'Anthropic keys start with sk-ant-.';
    if(id === 'openai' && (k.startsWith('sk-or-') || k.startsWith('sk-ant-'))) return "That looks like a key for a different provider. Choose that provider's card instead.";
    return null;
  }
  function modelError(card, raw){
    const m = String(raw == null ? '' : raw).trim();
    if(m && !MODEL_OK.test(m)) return 'Model names use letters, digits and . _ / : @ + - only.';
    if(!m && !(card && card.default_model)) return 'Type the model name (as your provider lists it).';
    return null;
  }
  function urlError(card, raw, lan){
    if(!card || !card.url_editable) return null;
    const s = String(raw == null ? '' : raw).trim();
    const local = card.id === 'ollama' || card.id === 'lmstudio';
    const eff = s || card.base_url || '';
    if(!eff) return 'Type the address of your endpoint.';
    if(card.id === 'hermes'){
      return validateRemoteAgentUrl(eff, !!lan) ? null : 'Use https, or plain http only to this computer (or a private LAN address with the LAN box ticked).';
    }
    const u = validateBaseUrl(eff);
    if(!u) return 'That address is not allowed. Use https, or http only to this computer.';
    if(local && !isLoopbackHost(parseUrl(u).hostname)) return 'A local runtime must be on this computer.';
    return null;
  }
  // f = {base_url, model, key, lan_http, key_present}. Returns {ok, errors:{url,model,key}}.
  function validateForm(card, f){
    const errors = {};
    if(!card) return {ok: false, errors: {card: 'Choose a provider.'}};
    if(card.kind === 'cli') return {ok: true, errors};
    f = f || {};
    const u = urlError(card, f.base_url, f.lan_http); if(u) errors.url = u;
    const m = modelError(card, f.model); if(m) errors.model = m;
    const typed = String(f.key == null ? '' : f.key).trim();
    if(card.takes_key && typed){ const k = keyShapeError(card, typed); if(k) errors.key = k; }
    else if(card.needs_key && !f.key_present) errors.key = keyShapeError(card, '');
    return {ok: Object.keys(errors).length === 0, errors};
  }
  // The body POSTed to /api/llm/config. A blank key is left out (the saved one stays); a URL only for editable presets.
  function buildConfigBody(card, f){
    const b = {provider: card.id};
    if(card.kind === 'cli') return b;
    b.model = String(f.model || '').trim();
    if(card.url_editable) b.base_url = String(f.base_url || '').trim();
    if(card.id === 'hermes') b.lan_http = !!f.lan_http;
    const k = String(f.key == null ? '' : f.key).trim();
    if(card.takes_key && k) b.key = k;
    return b;
  }
  const TEST_WORDS = {
    unauthorized: 'The provider rejected the key. Check it, or paste a new one.',
    rate_limited: 'The provider says slow down. Wait a minute and test again.',
    model_blocked: 'Your OpenRouter workspace blocks this model. Pick another model or change the workspace settings.',
    unreachable: 'Could not reach the provider. Check the address and this computer\'s network.',
    timeout: 'The provider took too long to answer.',
    bad_reply: 'The provider answered in a shape the desk does not understand. Check the model name and address.',
    not_configured: 'Nothing is saved yet. Save a provider first.',
  };
  const SAVE_WORDS = {
    bad_provider: 'Choose a provider from the list.', provider_off: 'That provider is not available.',
    bad_url: 'That address is not allowed. Use https, or http only to this computer.',
    bad_model: 'Model names use letters, digits and . _ / : @ + - only.', bad_key: 'That key does not look right for this provider.',
    no_key_needed: 'That provider does not take a key.', bad_request: 'The desk could not read that request.',
  };
  function safeCode(c){ return /^[a-z_]{1,30}$/.test(String(c)) ? String(c) : 'error'; }
  function testWords(code){ return TEST_WORDS[code] || ('The test failed (' + safeCode(code) + ').'); }
  function saveWords(code){ return SAVE_WORDS[code] || ('Could not save (' + safeCode(code) + ').'); }
  function isOpenRouterUrl(s){
    if(typeof s !== 'string' || !s.startsWith('https://openrouter.ai/')) return false;
    try{ const u = new URL(s); return u.protocol === 'https:' && u.hostname === 'openrouter.ai' && !u.username && !u.password; }catch(_){ return false; }
  }
  function routeLabel(view, cards){
    if(!view || !view.configured) return 'the desk agent on this computer';
    const c = (cards || []).find(x => x.id === view.provider);
    const name = c ? c.label : view.provider;
    return view.model && view.kind !== 'cli' ? name + ' (' + view.model + ')' : name;
  }
  function digestNotice(view, cards){
    if(!view || !view.configured || view.kind === 'cli') return 'Your question and the desk digest stay with the agent on this computer.';
    return 'Your question and the desk digest (prices, positions and orders as numbers, no account ids) are sent to ' + routeLabel(view, cards) + '.';
  }
  const LOGIN_POLL_MS = 2000, LOGIN_MAX_MS = 600000;

  const api = {validateBaseUrl, validateRemoteAgentUrl, isLoopbackHost, isPrivateLan, keyShapeError, modelError, urlError, validateForm,
    buildConfigBody, testWords, saveWords, isOpenRouterUrl, routeLabel, digestNotice, LOGIN_POLL_MS, LOGIN_MAX_MS};
  root.pxLlmSetup = api;
  if(typeof module !== 'undefined') module.exports = api;
  if(typeof document === 'undefined') return;

  // ---- DOM ---------------------------------------------------------------------------------------------------------------------
  const dock = document.getElementById('chat-dock');
  if(!dock) return;
  const el = (tag, cls, text) => { const n = document.createElement(tag); if(cls) n.className = cls; if(text != null) n.textContent = text; return n; };
  const S = {cards: [], notes: [], view: null, sel: '', poll: null};

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
    const data = await r.json().catch(() => ({}));
    return {ok: r.ok, status: r.status, data};
  }
  async function getJson(path){
    const r = await fetch(path, {credentials: 'same-origin'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // The persistent route line under the Ask box header.
  const route = el('div', 'llm-route');
  const routeTop = el('div', 'llm-route-top'), routeNote = el('div', 'llm-route-note');
  route.append(routeTop, routeNote);
  const head = dock.querySelector('.chat-h');
  if(head) head.after(route); else dock.prepend(route);
  function paintRoute(){
    routeTop.textContent = 'This chat goes to: ' + routeLabel(S.view, S.cards);
    routeNote.textContent = digestNotice(S.view, S.cards);
  }

  const panel = el('div', 'llm-panel'); panel.hidden = true;
  panel.setAttribute('role', 'region'); panel.setAttribute('aria-label', 'Choose the AI for the Ask box');
  dock.append(panel);

  function stopPoll(){ if(S.poll){ clearTimeout(S.poll.t); S.poll = null; } }
  function close(){ stopPoll(); panel.hidden = true; }

  async function load(){
    const [d, v] = await Promise.all([getJson('/api/llm/detect'), getJson('/api/llm/config')]);
    S.cards = Array.isArray(d.providers) ? d.providers : [];
    S.notes = Array.isArray(d.found_cli) ? d.found_cli : [];
    S.view = v; paintRoute();
  }

  function render(){
    stopPoll();
    panel.replaceChildren();
    const top = el('div', 'llm-top');
    const x = el('button', 'ghost', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Close settings'); x.onclick = close;
    top.append(el('strong', '', 'Choose the AI for this box'), el('span', 'push'), x);
    panel.append(top, el('p', 'llm-note', digestNotice({configured: true, kind: 'http', provider: 'x'}, [{id: 'x', label: 'the provider you choose'}])));

    const list = el('div', 'llm-cards');
    S.cards.forEach((c) => {
      const b = el('button', 'llm-card' + (c.id === S.sel ? ' on' : '') + (c.found ? ' found' : '')); b.type = 'button';
      b.setAttribute('aria-pressed', String(c.id === S.sel));
      const t = el('div', 'llm-card-t'); t.append(el('span', 'llm-name', c.label));
      if(c.found) t.append(el('span', 'llm-pill found', 'Detected where the desk runs'));
      if(S.view && S.view.configured && S.view.provider === c.id) t.append(el('span', 'llm-pill cur', 'In use'));
      b.append(t, el('div', 'llm-blurb', c.blurb));
      if(c.price_hint) b.append(el('div', 'llm-price', c.price_hint));
      b.onclick = () => { S.sel = c.id; render(); };
      list.append(b);
    });
    panel.append(list);
    if(S.notes.length){
      const n = el('div', 'llm-found');
      n.append(el('div', 'llm-note', 'Also found here, but the desk will not use these:'));
      S.notes.forEach((f) => n.append(el('div', 'llm-note dim', f.label + ': ' + f.reason)));
      panel.append(n);
    }
    const card = S.cards.find(c => c.id === S.sel);
    if(card) panel.append(form(card));
  }

  function field(label, input, errNode){
    const w = el('label', 'llm-field'); w.append(el('span', 'llm-lab', label), input); if(errNode) w.append(errNode); return w;
  }

  function form(card){
    const v = S.view && S.view.configured && S.view.provider === card.id ? S.view : null;
    const box = el('div', 'llm-form');
    const msg = el('div', 'llm-msg'); msg.setAttribute('role', 'status');
    const say = (t, bad) => { msg.textContent = t; msg.className = 'llm-msg' + (bad ? ' bad' : ''); };
    let urlIn = null, keyIn = null, lan = null;
    const errs = {};
    const mkErr = (k) => { errs[k] = el('div', 'llm-err'); return errs[k]; };

    if(card.url_editable){
      urlIn = el('input'); urlIn.type = 'url'; urlIn.autocomplete = 'off'; urlIn.spellcheck = false;
      urlIn.placeholder = card.base_url || 'https://your-endpoint.example/v1'; urlIn.value = (v && v.base_url) || '';
      box.append(field('Address', urlIn, mkErr('url')));
      if(card.id === 'hermes'){
        lan = el('input'); lan.type = 'checkbox'; lan.checked = !!(v && v.lan_http);
        const w = el('label', 'llm-check'); w.append(lan, el('span', '', 'Allow plain http to a private LAN address (10.x, 172.16-31.x, 192.168.x)'));
        box.append(w);
      }
    }
    let modelIn = null;
    if(card.kind !== 'cli'){
      modelIn = el('input'); modelIn.type = 'text'; modelIn.autocomplete = 'off'; modelIn.spellcheck = false;
      modelIn.placeholder = card.default_model || 'model name'; modelIn.value = (v && v.model) || '';
      if(card.models && card.models.length){
        const dl = el('datalist'); dl.id = 'llm-models-' + card.id;
        card.models.forEach(m => { const o = el('option'); o.value = m; dl.append(o); });
        modelIn.setAttribute('list', dl.id); box.append(dl);
      }
      box.append(field('Model', modelIn, mkErr('model')));
    }
    if(card.takes_key){
      keyIn = el('input'); keyIn.type = 'password'; keyIn.autocomplete = 'new-password'; keyIn.spellcheck = false; keyIn.value = '';
      keyIn.placeholder = v && v.key_present ? 'Saved. Type to replace.' : 'Paste your key';
      const kw = field('Key', keyIn, mkErr('key'));
      const st = el('div', 'llm-keystate');
      if(v && v.key_present) st.append(el('span', 'llm-pill cur', v.key_from_env ? 'Saved (from the desk environment)' : 'Saved'));
      if(v && v.needs_key_again) st.append(el('span', 'llm-pill warn', 'Paste the key again: the address changed'));
      if(v && v.key_present && !v.key_from_env){
        const rm = el('button', 'ghost mini', 'Remove'); rm.type = 'button';
        rm.onclick = async () => {
          rm.disabled = true;
          try{ const r = await post('/api/llm/clear', {}); if(r.ok){ S.view = r.data; await load(); S.sel = card.id; render(); } else say('Could not remove it.', true); }
          catch(_){ say('Could not reach the desk.', true); }
          rm.disabled = false;
        };
        st.append(rm);
      }
      kw.append(st);
      if(card.key_help_url && card.key_help_url.startsWith('https://')){
        const a = el('a', 'llm-link', 'Get a key'); a.href = card.key_help_url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        kw.append(a);
      }
      box.append(kw);
    }

    const read = () => ({base_url: urlIn ? urlIn.value : '', model: modelIn ? modelIn.value : '', key: keyIn ? keyIn.value : '',
      lan_http: lan ? lan.checked : false, key_present: !!(v && v.key_present)});
    const paintErrs = (e) => { Object.keys(errs).forEach(k => { errs[k].textContent = e[k] || ''; }); };

    const row = el('div', 'llm-btns');
    const save = el('button', '', 'Save'); save.type = 'button';
    const test = el('button', 'ghost', 'Test'); test.type = 'button';
    save.onclick = async () => {
      const f = read(), val = validateForm(card, f);
      paintErrs(val.errors);
      if(!val.ok){ say('Fix the highlighted fields first.', true); return; }
      save.disabled = true; say('Saving...');
      try{
        const r = await post('/api/llm/config', buildConfigBody(card, f));
        if(keyIn) keyIn.value = '';                        // the key is gone from the page the moment it has been sent
        if(r.ok){ S.view = r.data; await load(); S.sel = card.id; render(); const m = panel.querySelector('.llm-msg'); if(m){ m.textContent = 'Saved. Press Test to check it.'; } }
        else say(saveWords(r.data && r.data.detail), true);
      }catch(_){ say('Could not reach the desk.', true); }
      save.disabled = false;
    };
    test.onclick = async () => {
      test.disabled = true; say('Testing...');
      try{
        const r = await post('/api/llm/test', {});
        if(r.ok && r.data && r.data.ok) say('Works. The provider answered.');
        else if(r.ok) say(testWords(r.data && r.data.code), true);
        else say(testWords(r.data && r.data.detail), true);
      }catch(_){ say('Could not reach the desk.', true); }
      test.disabled = false;
    };
    row.append(save, test);
    box.append(row);

    if(card.id === 'openrouter') box.append(loginBlock(say));
    box.append(msg);
    return box;
  }

  function loginBlock(say){
    const w = el('div', 'llm-login');
    const b = el('button', 'ghost', 'Sign in with OpenRouter'); b.type = 'button';
    const out = el('div', 'llm-note'), link = el('div', 'llm-linkline mono');
    w.append(b, out, link);
    b.onclick = async () => {
      stopPoll(); link.textContent = ''; out.textContent = ''; b.disabled = true;
      try{
        const r = await post('/api/llm/login/start', {});
        if(!r.ok || !r.data || !r.data.pending_id){ say(r.data && r.data.detail === 'rate_limited' ? 'Too many sign-in tries. Wait a minute.' : 'Could not start the sign-in. Paste a key instead.', true); b.disabled = false; return; }
        link.textContent = String(r.data.url || '');
        if(isOpenRouterUrl(r.data.url)) window.open(r.data.url, '_blank', 'noopener');
        out.textContent = 'Waiting for the sign-in to finish. The sign-in comes back to this computer (localhost). If this desk runs on another machine, such as a headless server, the callback cannot arrive: paste a key instead.';
        pollLogin(r.data.pending_id, out, say);
      }catch(_){ say('Could not reach the desk.', true); }
      b.disabled = false;
    };
    return w;
  }
  function pollLogin(id, out, say){
    const t0 = Date.now();
    const cur = S.poll = {t: 0};
    const tick = async () => {
      if(S.poll !== cur) return;
      if(Date.now() - t0 > LOGIN_MAX_MS){ out.textContent = 'The sign-in timed out. Paste a key instead.'; stopPoll(); return; }
      try{
        const s = (await getJson('/api/llm/login/status?pending_id=' + encodeURIComponent(id))).status;
        if(S.poll !== cur) return;
        if(s === 'done'){ stopPoll(); await load(); S.sel = 'openrouter'; render(); const m = panel.querySelector('.llm-msg'); if(m) m.textContent = 'Signed in. The key is saved. Press Test to check it.'; return; }
        if(s === 'expired' || s === 'failed'){ out.textContent = s === 'expired' ? 'The sign-in expired. Try again, or paste a key.' : 'The sign-in failed. Try again, or paste a key.'; stopPoll(); return; }
      }catch(_){ /* keep polling */ }
      if(S.poll === cur) cur.t = setTimeout(tick, LOGIN_POLL_MS);
    };
    cur.t = setTimeout(tick, LOGIN_POLL_MS);
  }

  async function open(){
    panel.hidden = false; panel.replaceChildren(el('p', 'llm-note', 'Looking for AI on this computer...'));
    try{
      await load();
      if(!S.sel) S.sel = S.view && S.view.configured ? S.view.provider : ((S.cards.find(c => c.found) || S.cards[0] || {}).id || '');
      render();
    }catch(_){ panel.replaceChildren(el('p', 'llm-note bad', 'Could not read the provider list from the desk.')); }
  }

  api.open = open; api.close = close;
  dock.addEventListener('keydown', (e) => { if(e.key === 'Escape' && !panel.hidden){ e.stopPropagation(); close(); } }, true);
  getJson('/api/llm/config').then((v) => { S.view = v; paintRoute(); return getJson('/api/llm/detect'); })
    .then((d) => { S.cards = Array.isArray(d.providers) ? d.providers : []; paintRoute(); }).catch(() => { paintRoute(); });
})(typeof window !== 'undefined' ? window : globalThis);
