'use strict';
// Alerts popover: installs /sw.js, subscribes this device to Web Push, and edits its per-event toggles.
// Notifications are text that goes OUT from the desk. Nothing here can place, cancel or change anything on the desk's order side.
(() => {
  const $ = (id) => document.getElementById(id);
  const btn = $('alerts-btn'), pop = $('alerts-pop');
  if(!btn || !pop) return;

  const LABELS = {
    halt: 'Halt',
    flat: 'Position went flat',
    fill: 'Fills',
    stop: 'Stop hit, stop failure, unprotected position',
    room: 'Daily or drawdown room under 25%',
    feed: 'Market or user feed down over 30 s, and recovered',
    callout: 'Analyst ENTRY callouts',
    regime: 'Regime flipped to RANGING',
  };
  const ORDER = ['halt', 'flat', 'fill', 'stop', 'room', 'feed', 'callout', 'regime'];
  const st = {sid: null, sub: null, events: null, busy: false};

  const store = {
    get(k){try{return localStorage.getItem(k)}catch(_){return null}},
    set(k, v){try{localStorage.setItem(k, v)}catch(_){}},
    del(k){try{localStorage.removeItem(k)}catch(_){}},
  };

  async function jget(path){
    const r = await fetch(path, {credentials: 'same-origin'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  async function jpost(path, body, retried){
    const tok = (await jget('/api/csrf')).token;
    const r = await fetch(path, {method: 'POST', credentials: 'same-origin',
      headers: {'content-type': 'application/json', 'x-px-csrf': tok}, body: JSON.stringify(body || {})});
    const data = await r.json().catch(() => null);
    return {ok: r.ok, status: r.status, data};
  }

  const isIOS = () => /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = () => !!(navigator.standalone || (window.matchMedia && matchMedia('(display-mode: standalone)').matches));

  // Why this device cannot get alerts, in plain words, or '' when it can.
  function blocker(){
    if(isIOS() && !standalone()) return 'Add to Home Screen first (Share, then Add to Home Screen), then open the desk from that icon and tap Alerts again.';
    if(!window.isSecureContext) return 'Alerts need HTTPS (or localhost). Open the desk through your Tailscale HTTPS name.';
    if(!('serviceWorker' in navigator)) return 'This browser has no service worker support, so it cannot receive alerts.';
    if(!('PushManager' in window)) return 'This browser has no push support' + (isIOS() ? ' (iOS 16.4 or newer is needed).' : '.');
    if(!('Notification' in window)) return 'This browser has no notification support.';
    return '';
  }

  function say(t, bad){const s = $('al-status'); s.textContent = t || ''; s.classList.toggle('bad', !!bad)}

  function urlB64(s){
    const pad = '='.repeat((4 - s.length % 4) % 4);
    const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  }
  async function idOf(endpoint){   // matches push.sub_id: first 16 hex of sha256(endpoint)
    const h = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(endpoint));
    return Array.from(new Uint8Array(h)).slice(0, 8).map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  async function registration(){
    return navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(() => navigator.serviceWorker.ready);
  }

  function buildEvents(){
    const box = $('al-events');
    if(box.querySelector('label')) return;
    for(const k of ORDER){
      const l = document.createElement('label'), i = document.createElement('input');
      i.type = 'checkbox'; i.dataset.kind = k;
      i.addEventListener('change', savePrefs);
      l.append(i, document.createTextNode(LABELS[k]));
      box.append(l);
    }
  }
  function paint(view){
    st.events = view.events;
    for(const i of $('al-events').querySelectorAll('input')) i.checked = !!view.events[i.dataset.kind];
    const m = /^(\d\d:\d\d)-(\d\d:\d\d)$/.exec(view.quiet || '');
    $('al-qf').value = m ? m[1] : ''; $('al-qt').value = m ? m[2] : '';
  }
  function mode(subscribed, perm){
    $('al-main').hidden = !subscribed;
    $('al-enable').hidden = subscribed || perm === 'denied';
    $('al-test').hidden = !subscribed; $('al-disable').hidden = !subscribed;
  }

  let saveT = 0;
  function savePrefs(){
    clearTimeout(saveT);
    saveT = setTimeout(async () => {
      if(!st.sid) return;
      const events = {};
      for(const i of $('al-events').querySelectorAll('input')) events[i.dataset.kind] = i.checked;
      const f = $('al-qf').value, t = $('al-qt').value;
      const r = await jpost('/api/push/prefs', {id: st.sid, events, quiet: f && t ? f + '-' + t : ''}).catch(() => null);
      say(r && r.ok ? 'Saved.' : 'Could not save. Is the desk reachable?', !(r && r.ok));
    }, 250);
  }

  async function refresh(){
    const why = blocker();
    mode(false, 'default');
    buildEvents();
    if(why){say(why, true); $('al-enable').hidden = true; return}
    const perm = Notification.permission;
    let sub = null;
    try{
      const reg = await navigator.serviceWorker.getRegistration('/');
      sub = reg ? await reg.pushManager.getSubscription() : null;
    }catch(_){}
    st.sub = sub;
    if(perm === 'denied'){
      say('Notifications are blocked for this site. Allow them in the browser site settings (lock icon, Site settings, Notifications), then reload.', true);
      mode(false, perm); return;
    }
    if(!sub){say('Off on this device. Enable asks the browser for permission.'); mode(false, perm); return}
    st.sid = await idOf(sub.endpoint).catch(() => store.get('px.push.id'));
    let v = null;
    try{v = await jget('/api/push/state?id=' + encodeURIComponent(st.sid || ''))}catch(_){}
    if(!v || !v.enabled){say('This browser is subscribed but the desk does not know it. Press Enable to register it again.', true); mode(false, perm); return}
    paint(v); mode(true, perm); say('On for this device.');
  }

  async function enable(){
    if(st.busy) return;
    st.busy = true; say('Working...');
    try{
      const perm = await Notification.requestPermission();      // first await: still inside the click gesture
      if(perm !== 'granted'){await refresh(); return}
      const reg = await registration();
      const key = (await jget('/api/push/vapid')).key;
      const opts = {userVisibleOnly: true, applicationServerKey: urlB64(key)};
      let sub = await reg.pushManager.getSubscription();
      try{if(!sub) sub = await reg.pushManager.subscribe(opts)}
      catch(e){
        if(e && e.name === 'InvalidStateError'){sub = await reg.pushManager.getSubscription(); if(sub) await sub.unsubscribe(); sub = await reg.pushManager.subscribe(opts)}
        else throw e;
      }
      const r = await jpost('/api/push/subscribe', {subscription: sub.toJSON()});
      if(!r.ok){say('The desk refused this device (' + ((r.data && r.data.detail) || 'HTTP ' + r.status) + ').', true); return}
      st.sid = r.data.id; store.set('px.push.id', st.sid);
      await refresh();
    }catch(e){
      say('Could not subscribe: ' + ((e && e.name) || 'error') + '. The browser push service may be unreachable.', true);
    }finally{st.busy = false}
  }

  async function test(){
    say('Sending test...');
    const r = await jpost('/api/push/test', {id: st.sid}).catch(() => null);
    const res = r && r.data && r.data.result;
    say(res === 'ok' ? 'Test sent. It should appear in a moment.' : res === 'gone' ? 'The push service dropped this device. Press Enable again.'
      : r && r.status === 429 ? 'Wait a few seconds between tests.' : 'Test failed (' + (res || 'no answer') + ').', res !== 'ok');
    if(res === 'gone') refresh();
  }

  async function disable(){
    try{if(st.sub) await st.sub.unsubscribe()}catch(_){}
    if(st.sid) await jpost('/api/push/unsubscribe', {id: st.sid}).catch(() => null);
    store.del('px.push.id'); st.sid = null; st.sub = null;
    await refresh(); say('Off on this device.');
  }

  function place(){
    const r = btn.getBoundingClientRect();
    pop.style.top = Math.round(r.bottom + 6) + 'px';
    pop.style.right = Math.max(8, Math.round(window.innerWidth - r.right)) + 'px';
  }
  function open(){pop.hidden = false; btn.setAttribute('aria-expanded', 'true'); place(); refresh()}
  function close(){pop.hidden = true; btn.setAttribute('aria-expanded', 'false')}

  btn.addEventListener('click', () => pop.hidden ? open() : close());
  $('al-close').addEventListener('click', () => {close(); btn.focus()});
  $('al-enable').addEventListener('click', enable);
  $('al-test').addEventListener('click', test);
  $('al-disable').addEventListener('click', disable);
  $('al-qf').addEventListener('change', savePrefs);
  $('al-qt').addEventListener('change', savePrefs);
  $('al-qclear').addEventListener('click', () => {$('al-qf').value = ''; $('al-qt').value = ''; savePrefs()});
  document.addEventListener('keydown', (e) => {if(e.key === 'Escape' && !pop.hidden){close(); btn.focus()}});
  document.addEventListener('click', (e) => {if(!pop.hidden && !pop.contains(e.target) && e.target !== btn) close()});
  window.addEventListener('resize', () => {if(!pop.hidden) place()});

  // Keep the worker installed on every load (it caches nothing and handles only push and notification clicks).
  if(!blocker()) registration().catch(() => {});
})();
