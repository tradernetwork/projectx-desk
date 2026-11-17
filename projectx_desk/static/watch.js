/* Level-cross detection for the dealer map. Pure: no DOM, no chart, no clock of its own. Display only; nothing here reaches the guard or sizing.
   A cross is price moving from one side of a level to the other by more than `hyst` (one tick), after the level has been seen on a side once (the
   first sight sets a silent baseline, so loading the page never fires). The same level and direction is not announced again inside `cooldownMs`.
   It says what HAPPENED. It is not a signal. */
(function(root){
  const fin = v => typeof v === 'number' && isFinite(v);
  /* items: [{price, roles: ['TRAPDOOR', ...]}]; sides and fired are Maps the caller keeps between calls; returns [{key, price, role, side}] (side -1 down, +1 up). */
  function crossEvents(items, p, hyst, sides, fired, now, cooldownMs, roleFilter, opts){
    /* opts (optional): {rearm, armed}. After key+direction fires, the same key+direction stays quiet until price has been `rearm` or more
       beyond the level on the OTHER side (armed is a Map the caller keeps). Without opts nothing changes. */
    const out = [], rearm = opts && fin(opts.rearm) && opts.armed ? opts.rearm : null, armed = rearm !== null ? opts.armed : null;

    if(!fin(p) || !Array.isArray(items)) return out;
    for(const it of items){
      if(!it || !fin(it.price)) continue;
      for(const role of it.roles || []){
        if(roleFilter && !roleFilter(role)) continue;
        const key = it.price + '|' + role, side = p > it.price + hyst ? 1 : p < it.price - hyst ? -1 : 0;
        if(side === 0) continue;                                   // inside the band around the level: keep the last side, announce nothing
        const prev = sides.get(key);
        sides.set(key, side);
        if(armed && Math.abs(p - it.price) >= rearm) armed.set(key + '|' + (-side), true);   // far enough on this side: the opposite-direction cross is re-armed
        if(prev === undefined || prev === side) continue;           // first sight is a silent baseline
        const k = key + '|' + side;
        const was = fired.get(k);
        if(was !== undefined && now - was < cooldownMs) continue;      // never fired is not "fired at time zero"
        if(armed && armed.get(k) === false) continue;                  // fired before and price has not retreated far enough yet
        fired.set(k, now);
        if(armed) armed.set(k, false);
        out.push({key, price: it.price, role, side});
      }
    }
    return out;
  }
  root.PXWatch = {crossEvents};
  if(typeof module !== 'undefined') module.exports = root.PXWatch;
})(typeof window !== 'undefined' ? window : globalThis);
