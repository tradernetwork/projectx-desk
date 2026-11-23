/* Chart declutter logic, pure (no DOM, no chart) so node can test it. Display only: nothing here reaches the guard, sizing or the respect gates.
   desk.js draws with these; tests/test_declutter.py runs them.
   Rules that matter:
   - A RESPECTED level is never removed or demoted by a merge. Only a lower-priority duplicate goes.
   - Priority (low number wins): position/stop 0, analyst callout 1, respected 2, map 3, GEX 4, auto 5. Priority 0 and respected levels are never dropped.
   - Labels are short: the long gloss lives in a tooltip or the panel. */
(function(root){
  const num = v => typeof v === 'number' && isFinite(v);
  const PRIO = {pos: 0, callout: 1, respect: 2, map: 3, gex: 4, auto: 5};
  const PLUS = {gex: '+GEX', map: '+MAP'};
  const LABEL_MAX = 22;

  /* One-line label cap: 22 characters, a hard cut so a chip never grows past the cap. */
  function shortLabel(s, max){
    s = String(s == null ? '' : s); max = max || LABEL_MAX;
    return s.length <= max ? s : s.slice(0, max);
  }

  const gexRole = lv => /^flip/.test(String(lv && lv.name || '')) ? 'flip' : String(lv && lv.name || '') === 'pin' ? 'pin' : 'wall';
  const gexRel = lv => num(lv && lv.rel) ? Math.max(0, Math.min(1, lv.rel)) : null;

  /* Which GEX levels get drawn. Flip and pin always. Walls: the 2 nearest above and the 2 nearest below the last price; with `more`, every wall.
     Returns [{lv, role}] sorted high to low. Without a last price the walls fall back to the 4 biggest. */
  function gexPick(levels, last, more){
    const flips = [], pins = [], walls = [];
    for(const lv of Array.isArray(levels) ? levels : []){
      if(!lv || !num(lv.price)) continue;
      const r = gexRole(lv);
      (r === 'flip' ? flips : r === 'pin' ? pins : walls).push({lv, role: r});
    }
    let kept;
    if(more) kept = walls;
    else if(num(last)){
      const up = walls.filter(w => w.lv.price > last).sort((a, b) => a.lv.price - b.lv.price).slice(0, 2);
      const dn = walls.filter(w => w.lv.price <= last).sort((a, b) => b.lv.price - a.lv.price).slice(0, 2);
      kept = [...up, ...dn];
    }else kept = walls.slice().sort((a, b) => (gexRel(b.lv) ?? 0) - (gexRel(a.lv) ?? 0)).slice(0, 4);
    return [...flips, ...pins, ...kept].sort((a, b) => b.lv.price - a.lv.price);
  }

  /* Short chip text plus the gloss that goes to a tooltip: "wall 7704 BIG", "flip 7700", "pin 7710". */
  function gexLabel(lv, role){
    role = role || gexRole(lv);
    const rl = gexRel(lv), sz = rl == null ? '' : rl >= 0.66 ? 'BIG' : rl >= 0.33 ? 'MED' : 'SMALL';
    const label = role === 'wall' ? 'wall ' + lv.price + (sz ? ' ' + sz : '') : role + ' ' + lv.price;
    const kind = lv.kind === 'PUT' ? 'PUT wall: dealers short gamma, moves speed up' : lv.kind === 'CALL' ? 'CALL wall: dealers long gamma, moves stall' : role === 'flip' ? 'flip: where dealers go from chasing to braking' : role === 'pin' ? 'pin: the magnet strike' : 'wall';
    return {label, title: lv.price + ' ' + kind + (sz ? ', ' + sz + ' against the biggest wall today' : '') + '. Positioning, not a target.'};
  }

  /* The bands behind the lines: only the top `n` (default 2) by size. The pin counts as size 1. Returns a Set of prices. */
  function gexBandPrices(picked, n){
    const sized = picked.filter(p => p.role !== 'flip').map(p => ({price: p.lv.price, rel: p.role === 'pin' ? 1 : gexRel(p.lv) ?? 0.3}))
      .filter(x => x.rel >= 0.25).sort((a, b) => b.rel - a.rel).slice(0, n == null ? 2 : n);
    return new Set(sized.map(x => x.price));
  }

  const within = (price, last, atr, k) => num(price) && num(last) && num(atr) && atr > 0 && Math.abs(price - last) <= k * atr;

  /* Respected and auto lines: only those within `k` ATR of the last price (default 2). With no ATR or last price nothing can be judged,
     so nothing is dropped. */
  function nearFilter(lines, last, atr, k){
    if(!num(last) || !num(atr) || !(atr > 0)) return lines.slice();
    return lines.filter(l => within(l.price, last, atr, k == null ? 2 : k));
  }

  /* Cap the LABELLED chips. Respected lines (prio 2) are never removed: the ones past the cap keep their line but lose the label.
     Everything else past the cap is dropped. Nearest to the last price first within a source. */
  function capChips(lines, last, cap){
    cap = cap == null ? 4 : cap;
    const dist = l => num(last) ? Math.abs(l.price - last) : 0;
    const order = lines.map((l, i) => ({l, i})).sort((a, b) => (a.l.prio ?? 9) - (b.l.prio ?? 9) || dist(a.l) - dist(b.l) || a.i - b.i);
    const keep = new Set(), unlabel = new Set();
    let n = 0;
    for(const {l, i} of order){
      if(n < cap){keep.add(i); n++}
      else if(l.prio === PRIO.respect){keep.add(i); unlabel.add(i)}
    }
    return lines.map((l, i) => keep.has(i) ? (unlabel.has(i) ? {...l, label: ''} : l) : null).filter(Boolean);
  }

  /* One pass over every source. items: [{key, src, price, prio?}]. Within `tol` ATR (default 0.25) of an already kept item the lower
     priority one is dropped (never a respected level, whatever sits next to it); a dropped GEX or map level leaves "+GEX" / "+MAP" on the keeper's chip. Priority 0 is never dropped.
     Returns {drop: Set(key), plus: Map(key -> ['+GEX', ...])}. */
  function mergeLines(items, atr, tol){
    const drop = new Set(), plus = new Map();
    if(!num(atr) || !(atr > 0)) return {drop, plus};
    const t = (tol == null ? 0.25 : tol) * atr;
    const order = items.filter(x => x && num(x.price)).map((x, i) => ({x, i, p: x.prio ?? PRIO[x.src] ?? 9}))
      .sort((a, b) => a.p - b.p || a.i - b.i);
    const kept = [];
    for(const {x, p} of order){
      const hit = p === 0 || x.src === 'respect' ? null : kept.find(k => Math.abs(k.x.price - x.price) <= t && k.p < p);
      if(!hit){kept.push({x, p}); continue}
      drop.add(x.key);
      const tag = PLUS[x.src];
      if(tag){const arr = plus.get(hit.x.key) || []; if(!arr.includes(tag)) arr.push(tag); plus.set(hit.x.key, arr)}
    }
    return {drop, plus};
  }
  const withPlus = (label, plusList) => plusList && plusList.length ? label + ' ' + plusList.join(' ') : label;

  /* Trend tags: only when the line is within `k` ATR (default 1.5) of price, and at most `cap` (default 4) per draw. */
  function tagGate(last, atr, cap, k){
    let n = 0; cap = cap == null ? 4 : cap; k = k == null ? 1.5 : k;
    return (value, fallbackAtr) => {      // fallbackAtr: used only when the gate has no ATR of its own
      const a = num(atr) && atr > 0 ? atr : fallbackAtr;
      if(n >= cap) return false;
      if(num(last) && num(a) && a > 0 && !within(value, last, a, k)) return false;
      n++; return true;
    };
  }

  /* Which trendlines draw. cands: [{id, own, kind, now, past, touches, recency}] where `now` is the line's price at the last candle, `past` its
     price 40 bars earlier (both on the chart on screen), recency 0..1 (1 = last touch on the newest bar). Rules, in order:
     1. drop a line farther than `far` ATR (default 3) from the last price;
     2. rank by touches, recency, a bonus for the chart's own timeframe, minus distance (touches are counted, not an edge claim);
     3. walk the ranking and drop a line of the same kind that sits within `dup` ATR (default 0.3) of a stronger kept line at BOTH ends (near parallel);
     4. keep at most `cap` (default 4). Own lines are never dropped by the cap or the duplicate rule (they are the chart's own channel), only counted.
     Without a last price or ATR nothing can be judged and only the cap applies. Returns a Set of kept ids. */
  function pickTrendLines(cands, last, atr, o){
    o = o || {}; const far = o.far == null ? 3 : o.far, dup = o.dup == null ? 0.3 : o.dup, cap = o.cap == null ? 4 : o.cap;
    const ok = num(last) && num(atr) && atr > 0;
    const d = c => ok ? Math.abs(c.now - last) / atr : 0;
    const rows = (Array.isArray(cands) ? cands : []).filter(c => c && num(c.now) && (c.own || !ok || d(c) <= far))
      .map((c, i) => ({c, i, s: (num(c.touches) ? c.touches : 0) + 2 * (num(c.recency) ? Math.max(0, Math.min(1, c.recency)) : 0) + (c.own ? 3 : 0) - 0.5 * d(c)}))
      .sort((a, b) => b.s - a.s || a.i - b.i);
    const kept = [];
    for(const {c} of rows){
      if(!c.own){
        if(kept.length >= cap) continue;
        if(ok && kept.some(k => k.kind === c.kind && Math.abs(k.now - c.now) <= dup * atr && num(c.past) && num(k.past) && Math.abs(k.past - c.past) <= dup * atr)) continue;
      }
      kept.push(c);
    }
    return new Set(kept.map(c => c.id));
  }

  /* Relative age: "just now", "5m", "2h"; coarse on purpose. */
  function fmtAge(s){
    if(!num(s) || s < 0) return '';
    if(s < 90) return 'now';
    if(s < 5400) return Math.round(s / 60) + 'm';
    if(s < 172800) return Math.round(s / 3600) + 'h';
    return Math.round(s / 86400) + 'd';
  }
  /* If every row names the same analyst, the handle is said once in the header, not on every row. */
  function sharedHandle(items){
    const names = items.map(i => String(i && (i.analyst || i.channel) || '')).filter(Boolean);
    return names.length > 1 && names.length === items.length && names.every(n => n === names[0]) ? names[0] : '';
  }
  /* "n=19 small sample" style qualifier: one short phrase instead of a sentence. */
  function sampleNote(n, min){
    if(!num(n)) return '';
    return 'n=' + n + (n < (min || 30) ? ' small sample' : '');
  }

  root.PXDeclutter = {PRIO, LABEL_MAX, shortLabel, gexRole, gexPick, gexLabel, gexBandPrices, nearFilter, capChips, mergeLines, withPlus, tagGate, pickTrendLines, fmtAge, sharedHandle, sampleNote};
  if(typeof module !== 'undefined') module.exports = root.PXDeclutter;
})(typeof window !== 'undefined' ? window : globalThis);
