'use strict';
// Desk chat: a floating box that sends one typed question to POST /api/chat and shows the text that comes back.
// The agent behind it is read-only and out of process. Nothing here can touch an order, a ticket or a halt; the only POST is /api/chat.
(() => {
  const $ = (id) => document.getElementById(id);
  const fab = $('chat-fab'), dock = $('chat-dock'), log = $('chat-log'), form = $('chat-form'), input = $('chat-in'), send = $('chat-send');
  if(!fab || !dock) return;
  const KEY = 'px_chat_v1';
  const st = {msgs: [], busy: false};

  const store = {
    get(){try{return JSON.parse(sessionStorage.getItem(KEY) || '[]')}catch(_){return []}},
    set(v){try{sessionStorage.setItem(KEY, JSON.stringify(v.slice(-40)))}catch(_){}},
  };

  function paint(m){
    const d = document.createElement('div');
    d.className = 'chat-msg ' + m.who + (m.err ? ' err' : '');
    d.textContent = m.text;                       // text only: a reply is never parsed as HTML
    log.append(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }
  function add(who, text, err){
    const m = {who, text, err: !!err};
    st.msgs.push(m); store.set(st.msgs);
    return paint(m);
  }

  async function csrf(){
    const r = await fetch('/api/csrf', {credentials: 'same-origin'});
    if(!r.ok) throw new Error('csrf');
    return (await r.json()).token;
  }
  async function ask(prompt){
    const go = async () => fetch('/api/chat', {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'x-px-csrf': await csrf()}, body: JSON.stringify({prompt})});
    let r = await go();
    if(r.status === 403) r = await go();           // a restarted desk mints a new token
    const data = await r.json().catch(() => ({}));
    if(!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    return data.reply;
  }

  async function submit(){
    const text = input.value.trim();
    if(!text || st.busy) return;
    input.value = '';
    st.busy = true; send.disabled = true;
    add('me', text);
    const wait = document.createElement('div');
    wait.className = 'chat-msg bot';
    log.append(wait);
    const t0 = Date.now();
    const tick = setInterval(() => {wait.textContent = 'reading the desk... ' + Math.round((Date.now() - t0) / 1000) + 's'}, 1000);
    wait.textContent = 'reading the desk... 0s';
    try{
      add('bot', await ask(text));
    }catch(e){
      add('bot', e.message === 'busy' ? 'Still answering the last question. Try again in a moment.' : 'No answer: ' + e.message, true);
    }
    clearInterval(tick); wait.remove();
    st.busy = false; send.disabled = false; input.focus();
  }

  function open(on){
    dock.hidden = !on; fab.hidden = on;
    fab.setAttribute('aria-expanded', String(on));
    if(on) input.focus();
  }

  fab.onclick = () => open(true);
  $('chat-close').onclick = () => {open(false); fab.focus()};
  $('chat-clear').onclick = () => {st.msgs = []; store.set([]); log.replaceChildren()};
  form.onsubmit = (e) => {e.preventDefault(); submit()};
  input.onkeydown = (e) => {if(e.key === 'Enter' && !e.shiftKey){e.preventDefault(); submit()}};
  dock.onkeydown = (e) => {if(e.key === 'Escape'){open(false); fab.focus()}};

  st.msgs = store.get();
  st.msgs.forEach(paint);
  fetch('/api/chat/state', {credentials: 'same-origin'}).then((r) => r.ok ? r.json() : null).then((s) => {
    if(s && s.enabled) fab.hidden = false;         // PX_CHAT_URL unset: no button at all
  }).catch(() => {});
})();
