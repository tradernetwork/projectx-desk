/* Display preferences from the desk agent (Round 38): pure and node-tested, display only. No network, no POST, no storage of its own.
   `diff(prefs, current)` lists the keys whose wanted value differs from what the page shows; desk.js clicks the matching
   existing toggle for each, so the page's own handlers do the work. Unknown keys and non-booleans are ignored. */
(function(root){
  'use strict';
  const KEYS = ['gex', 'chartMore', 'tmap', 'trend', 'trend_1m', 'trend_5m', 'trend_15m'];
  function sig(prefs){
    if(!prefs || typeof prefs !== 'object') return '';
    return KEYS.filter(k => typeof prefs[k] === 'boolean').map(k => k + '=' + (prefs[k] ? 1 : 0)).join(',');
  }
  function diff(prefs, current){
    const out = [];
    if(!prefs || typeof prefs !== 'object' || !current || typeof current !== 'object') return out;
    for(const k of KEYS) if(typeof prefs[k] === 'boolean' && typeof current[k] === 'boolean' && prefs[k] !== current[k]) out.push(k);
    return out;
  }
  root.PXView = {KEYS, sig, diff};
  if(typeof module !== 'undefined') module.exports = root.PXView;
})(typeof window !== 'undefined' ? window : globalThis);
