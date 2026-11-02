'use strict';
// Desk chat: a floating box that sends one typed question to POST /api/chat and shows the text that comes back.
// The agent behind it is read-only and out of process. Nothing here can touch an order, a ticket or a halt; the only POST is /api/chat.
(() => {
  const $ = (id) => document.getElementById(id);
  const fab = $('chat-fab'), dock = $('chat-dock'), log = $('chat-log'), form = $('chat-form'), input = $('chat-in'), send = $('chat-send');
  if(!fab || !dock) return;
  const KEY = 'px_chat_v1';
  const st = {msgs: [], busy: false, pics: []};
  const thumbs = $('chat-thumbs'), file = $('chat-file'), MAX_PICS = 3, MAX_BYTES = 3000000;

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
  // Screenshots: read in the browser, downsized when big, sent as base64 in the same JSON POST. Never stored; the desk and the agent re-check the bytes.
  function paintThumbs(){
    thumbs.replaceChildren(...st.pics.map((p, i) => {
      const d = document.createElement('div'); d.className = 'chat-th';
      const im = document.createElement('img'); im.src = p.url; im.alt = 'attachment ' + (i + 1);
      const x = document.createElement('button'); x.type = 'button'; x.textContent = '\u00d7'; x.setAttribute('aria-label', 'Remove attachment ' + (i + 1));
      x.onclick = () => {st.pics.splice(i, 1); paintThumbs()};
      d.append(im, x); return d;
    }));
    thumbs.hidden = !st.pics.length;
  }
  function shrink(f){                                   // anything over the cap goes through a canvas as a JPEG that fits
    return new Promise((res, rej) => {
      const u = URL.createObjectURL(f), im = new Image();
      im.onerror = () => {URL.revokeObjectURL(u); rej(new Error('unreadable image'))};
      im.onload = () => {
        const k = Math.min(1, 1800 / Math.max(im.width, im.height)), c = document.createElement('canvas');
        c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
        c.getContext('2d').drawImage(im, 0, 0, c.width, c.height); URL.revokeObjectURL(u);
        c.toBlob((b) => b ? res(b) : rej(new Error('unreadable image')), 'image/jpeg', 0.85);
      };
      im.src = u;
    });
  }
  function toDataUrl(b){return new Promise((res, rej) => {const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => rej(new Error('unreadable image')); r.readAsDataURL(b)})}
  async function attach(files){
    for(const f of files){
      if(!/^image\/(png|jpeg|webp|gif)$/.test(f.type)) continue;
      if(st.pics.length >= MAX_PICS){add('bot', 'Three screenshots max per question.', true); break}
      try{
        const b = f.size > MAX_BYTES ? await shrink(f) : f, url = await toDataUrl(b);
        st.pics.push({url, data: url.slice(url.indexOf(',') + 1)});
      }catch(e){add('bot', 'Could not read that image.', true)}
    }
    paintThumbs();
  }
  $('chat-attach').onclick = () => file.click();
  file.onchange = () => {attach([...file.files]); file.value = ''};
  input.addEventListener('paste', (e) => {
    const fs = [...(e.clipboardData ? e.clipboardData.files : [])].filter((f) => f.type.startsWith('image/'));
    if(fs.length){e.preventDefault(); attach(fs)}
  });
  dock.addEventListener('dragover', (e) => {e.preventDefault(); dock.classList.add('drop')});
  dock.addEventListener('dragleave', () => dock.classList.remove('drop'));
  dock.addEventListener('drop', (e) => {e.preventDefault(); dock.classList.remove('drop'); attach([...e.dataTransfer.files])});

  async function ask(prompt, images){
    const go = async () => fetch('/api/chat', {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'x-px-csrf': await csrf()}, body: JSON.stringify({prompt, images})});
    let r = await go();
    if(r.status === 403) r = await go();           // a restarted desk mints a new token
    const data = await r.json().catch(() => ({}));
    if(!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    return data.reply;
  }

  async function submit(){
    const text = input.value.trim(), pics = st.pics.slice();
    if((!text && !pics.length) || st.busy) return;
    input.value = ''; st.pics = []; paintThumbs();
    st.busy = true; send.disabled = true;
    add('me', (text || 'Look at the attached screenshot.') + (pics.length ? '\n[' + pics.length + ' screenshot' + (pics.length > 1 ? 's' : '') + ']' : ''));
    const wait = document.createElement('div');
    wait.className = 'chat-msg bot';
    log.append(wait);
    const t0 = Date.now();
    const tick = setInterval(() => {wait.textContent = 'reading the desk... ' + Math.round((Date.now() - t0) / 1000) + 's'}, 1000);
    wait.textContent = 'reading the desk... 0s';
    try{
      add('bot', await ask(text, pics.map((p) => ({data: p.data}))));
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
