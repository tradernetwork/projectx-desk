'use strict';
// Service worker for phone alerts only. It deliberately does NOT cache the app shell and does NOT intercept fetch:
// a stale UI on a trading desk is dangerous, so every page load goes to the network exactly as it would without a worker.
// Payloads are fixed-template text from the desk (an event name and a contract root). Nothing sensitive is ever sent here.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));

self.addEventListener('push', (e) => {
  let d = {};
  try{d = e.data ? e.data.json() : {}}catch(_){d = {}}
  const title = (typeof d.title === 'string' && d.title.slice(0, 60)) || 'ProjectX Desk';
  const body = (typeof d.body === 'string' && d.body.slice(0, 120)) || 'Desk alert';
  const tag = (typeof d.tag === 'string' && d.tag.slice(0, 60)) || 'px-alert';
  e.waitUntil(self.registration.showNotification(title, {
    body, tag, renotify: true, icon: '/static/icon-192.png', badge: '/static/icon-192.png',
    requireInteraction: d.kind === 'halt' || d.kind === 'stop', data: {url: '/'},
  }));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil((async () => {
    const all = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for(const c of all){
      try{if(new URL(c.url).origin === self.location.origin){await c.focus(); return}}catch(_){}
    }
    await self.clients.openWindow('/');
  })());
});
