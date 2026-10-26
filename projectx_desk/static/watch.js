/* Level-cross detection for the dealer map. Pure: no DOM, no chart, no clock of its own. Display only; nothing here reaches the guard or sizing.
   A cross is price moving from one side of a level to the other by more than `hyst` (one tick), after the level has been seen on a side once (the
   first sight sets a silent baseline, so loading the page never fires). The same level and direction is not announced again inside `cooldownMs`.
   It says what HAPPENED. It is not a signal. */
(function(root){
  const fin = v => typeof v === 'number' && isFinite(v);
  /* items: [{price, roles: ['TRAPDOOR', ...]}]; sides and fired are Maps the caller keeps between calls; returns [{key, price, role, side}] (side -1 down, +1 up). */
  function crossEvents(items, p, hyst, sides, fired, now, cooldownMs, roleFilter){
    const out = [];
    if(!fin(p) || !Array.isArray(items)) return out;
    for(const it of items){
      if(!it || !fin(it.price)) continue;
      for(const role of it.roles || []){
        if(roleFilter && !roleFilter(role)) continue;
        const key = it.price + '|' + role, side = p > it.price + hyst ? 1 : p < it.price - hyst ? -1 : 0;
        if(side === 0) continue;                                   // inside the band around the level: keep the last side, announce nothing
        const prev = sides.get(key);
        sides.set(key, side);
        if(prev === undefined || prev === side) continue;           // first sight is a silent baseline
        const k = key + '|' + side;
        const was = fired.get(k);
        if(was !== undefined && now - was < cooldownMs) continue;      // never fired is not "fired at time zero"
        fired.set(k, now);
        out.push({key, price: it.price, role, side});
      }
    }
    return out;
  }
  root.PXWatch = {crossEvents};
  if(typeof module !== 'undefined') module.exports = root.PXWatch;
})(typeof window !== 'undefined' ? window : globalThis);
