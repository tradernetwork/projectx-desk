/* What happened to an analyst callout since it posted: pure (no DOM, no chart), node-tested (tests/test_callouts.py). Display only.
   Facts, not a rating: it walks the bars after the post and says whether the entry traded, whether the stop or a target was touched, and how far price
   is from entry and stop now. A bar that touches the stop and a target counts as the stop (the same convention as the graders). */
(function(root){
  const fin = v => typeof v === 'number' && isFinite(v);
  /* c: {side: 'LONG'|'SHORT', entry, stop, tp1, tp2, tp3}. bars: closed or forming {timestamp, high, low}, oldest first, only bars that started AFTER the post
     (a bar that began before the post would mix older prices in). last: latest price. atr: for the stop-near-level test. levels: [{price, roles:[...]}]. */
  function status(c, bars, last, atr, levels){
    if(!c || !fin(c.entry) || !fin(c.stop) || (c.side !== 'LONG' && c.side !== 'SHORT')) return null;
    const sgn = c.side === 'LONG' ? 1 : -1, tps = [c.tp1, c.tp2, c.tp3];
    const seq = (Array.isArray(bars) ? bars : []).filter(b => b && fin(b.high) && fin(b.low));
    if(fin(last)) seq.push({high: last, low: last});            // the latest print stands in for the bar still forming
    let entered = false, hit = 0, stopped = false;
    const stopHit = b => sgn > 0 ? b.low <= c.stop : b.high >= c.stop;
    const tgtHit = (b, k) => fin(tps[k]) && (sgn > 0 ? b.high >= tps[k] : b.low <= tps[k]);
    for(const b of seq){
      if(!entered){ if(b.low <= c.entry && b.high >= c.entry) entered = true; else continue }
      let top = hit;
      for(let k = hit; k < 3; k++) if(tgtHit(b, k)) top = k + 1;
      if(stopHit(b)){ stopped = true; break }                    // stop and target in one bar: the stop
      hit = top;
    }
    const state = stopped ? 'STOPPED' : hit ? 'T' + hit : entered ? 'IN' : 'WAITING';
    const out = {state, entered, targets_hit: hit, stopped, pts_from_entry: null, pts_to_stop: null, stop_near: null};
    if(fin(last)){
      out.pts_from_entry = Math.round(sgn * (last - c.entry) * 100) / 100;
      out.pts_to_stop = Math.round(sgn * (last - c.stop) * 100) / 100;
    }
    if(fin(atr) && atr > 0 && Array.isArray(levels)){
      let best = null;
      for(const L of levels){
        if(!L || !fin(L.price)) continue;
        const d = Math.abs(L.price - c.stop) / atr;
        if(d <= 0.25 && (!best || d < best.atr_dist)) best = {price: L.price, role: (L.roles || []).join('+') || 'level', atr_dist: Math.round(d * 100) / 100};
      }
      out.stop_near = best;
    }
    return out;
  }
  /* The one line the callout row shows. */
  function text(s){
    if(!s) return '';
    const sg = v => (v > 0 ? '+' : '') + v;
    const parts = [s.state === 'WAITING' ? 'entry not traded yet' : s.state === 'STOPPED' ? 'stop touched' + (s.targets_hit ? ' (after T' + s.targets_hit + ')' : '')
      : s.state === 'IN' ? 'entry traded' : 'T' + s.targets_hit + ' touched'];
    if(s.pts_from_entry != null) parts.push(sg(s.pts_from_entry) + ' pts from entry');
    if(!s.stopped && s.pts_to_stop != null) parts.push(s.pts_to_stop + ' pts to stop');
    if(s.stop_near) parts.push('stop sits at ' + s.stop_near.price + ' ' + s.stop_near.role);
    return parts.join(' · ');
  }
  root.PXCall = {status, text};
  if(typeof module !== 'undefined') module.exports = root.PXCall;
})(typeof window !== 'undefined' ? window : globalThis);
