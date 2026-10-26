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
    const a = ok && cl.length >= 29 ? adx(hi, lo, cl, 14) : null, er = ok ? efficiency(cl, 12) : null;
    return {adx: a, er: er, state: a == null ? 'UNKNOWN' : a < chop ? 'RANGING' : 'TRENDING', choppy: er == null ? null : er < 0.20};
  }
  root.PXRegime = {adx, efficiency, tfRegime};
  if(typeof module !== 'undefined') module.exports = root.PXRegime;
})(typeof window !== 'undefined' ? window : globalThis);
