/* Wilder ADX(14) and efficiency ratio for the mini charts. Same arithmetic as projectx_desk/regime.py and sizing.adx (tests/test_regime.py
   runs both on the same bars and compares). Display only. bars: [{high, low, close}], oldest first, closed bars. */
(function(root){
  const fin = v => typeof v === 'number' && isFinite(v);
  function adx(hi, lo, cl, n){
    n = n || 14;
    if(cl.length < 2 * n + 1) return null;
    const tr = [], pdm = [], mdm = [];
    for(let i = 1; i < cl.length; i++){
      tr.push(Math.max(hi[i] - lo[i], Math.abs(hi[i] - cl[i - 1]), Math.abs(lo[i] - cl[i - 1])));
      const up = hi[i] - hi[i - 1], dn = lo[i - 1] - lo[i];
      pdm.push(up > dn && up > 0 ? up : 0); mdm.push(dn > up && dn > 0 ? dn : 0);
    }
    const sum = (a, k) => { let s = 0; for(let i = 0; i < k; i++) s += a[i]; return s };
    let sT = sum(tr, n), sP = sum(pdm, n), sM = sum(mdm, n);
    const dx = (t, p, m) => { if(t <= 0) return 0; const a = 100 * p / t, b = 100 * m / t, s = a + b; return s === 0 ? 0 : 100 * Math.abs(a - b) / s };
    const dxs = [dx(sT, sP, sM)];
    for(let i = n; i < tr.length; i++){
      sT = sT - sT / n + tr[i]; sP = sP - sP / n + pdm[i]; sM = sM - sM / n + mdm[i];
      dxs.push(dx(sT, sP, sM));
    }
    let a = sum(dxs, n) / n;
    for(let i = n; i < dxs.length; i++) a = (a * (n - 1) + dxs[i]) / n;
    return a;
  }
  function efficiency(cl, n){
    n = n || 12;
    if(cl.length < n + 1) return null;
    const s = cl.slice(-(n + 1)); if(!s.every(fin)) return null;
    let path = 0; for(let i = 0; i < n; i++) path += Math.abs(s[i + 1] - s[i]);
    return path <= 0 ? null : Math.abs(s[n] - s[0]) / path;
  }
  function tfRegime(bars, chop){
    chop = chop == null ? 20 : chop;
    const hi = bars.map(b => b.high), lo = bars.map(b => b.low), cl = bars.map(b => b.close);
    const ok = [...hi, ...lo, ...cl].every(fin);
    const a = ok && cl.length >= 29 ? adx(hi, lo, cl, 14) : null, er = efficiency(cl, 12);      // ER needs only the last 13 closes, exactly as regime.py does
    return {adx: a, er: er, state: a == null ? 'UNKNOWN' : a < chop ? 'RANGING' : 'TRENDING', choppy: er == null ? null : er < 0.20};
  }
  /* Ribbon slope: is each EMA rising, falling or flat over the last k bars, judged against ATR so noise is not a slope. Same EMA recurrence as the
     mini charts (seeded with the first close). dirs are +1 / -1 / 0 in period order (fast first). `turn` is 'up' when the three fastest are all
     rising while at least one slow EMA is not, 'down' for the mirror: a ribbon that is not stacked yet but is starting to line up. */
  function ema(cl, p){const o = []; let prev = null; for(const c of cl){prev = prev == null ? c : (2 * c + (p - 1) * prev) / (p + 1); o.push(prev)} return o}
  function atrOf(hi, lo, cl, n){
    n = n || 14; const tr = [];
    for(let i = 1; i < cl.length; i++) tr.push(Math.max(hi[i] - lo[i], Math.abs(hi[i] - cl[i - 1]), Math.abs(lo[i] - cl[i - 1])));
    const t = tr.slice(-n); return t.length ? t.reduce((a, b) => a + b, 0) / t.length : null;
  }
  function ribbonSlopes(bars, periods, k, dead){
    k = k || 5; dead = dead == null ? 0.03 : dead;
    const hi = bars.map(b => b.high), lo = bars.map(b => b.low), cl = bars.map(b => b.close);
    if(![...hi, ...lo, ...cl].every(fin) || cl.length < Math.max(...periods) + k + 1) return null;
    const a = atrOf(hi, lo, cl, 14); if(!a || a <= 0) return null;
    const dirs = periods.map(p => {const e = ema(cl, p), d = (e[e.length - 1] - e[e.length - 1 - k]) / a; return d > dead ? 1 : d < -dead ? -1 : 0});
    const fast = dirs.slice(0, 3), slow = dirs.slice(3);
    const turn = fast.every(x => x > 0) && slow.some(x => x <= 0) ? 'up' : fast.every(x => x < 0) && slow.some(x => x >= 0) ? 'down' : null;
    return {dirs, arrows: dirs.map(d => d > 0 ? '\u25B2' : d < 0 ? '\u25BC' : '\u25AC').join(''), rising: dirs.filter(d => d > 0).length, falling: dirs.filter(d => d < 0).length, turn};
  }
  root.PXRegime = {adx, efficiency, tfRegime, ribbonSlopes};
  if(typeof module !== 'undefined') module.exports = root.PXRegime;
})(typeof window !== 'undefined' ? window : globalThis);
