/* Automatic trendlines and channels from the candles on screen. Pure: no DOM, no chart. Display only; nothing here reaches the guard or sizing.
   bars: [{high, low, close}], oldest first. Everything is scaled by ATR so a line means the same on 1m and 1h and on MES and MGC.

   What is drawn, and why it is honest about what it is:
   - A support / resistance line runs through two swing pivots and is kept only if at least THREE pivots touch it (within 0.3 ATR), no more
     than one close has gone through it by 0.25 ATR, its last touch is recent, and price has not already broken it by more than one ATR.
     The label says how many touches. Touches are counted, not tested against a random-line baseline the way respect.py tests levels:
     a line is a description of where price turned, not a claim that it will again.
   - A channel is a line plus a parallel rail through the far extreme, kept only when that far rail is touched by at least two pivots too.
   - A regression channel (least squares on closes +/- 2 sigma) is the fallback, shown only when R^2 is at least 0.5 and no pivot line fits. */
(function(root){
  const fin = v => typeof v === 'number' && isFinite(v);
  const OPT = {k: 4, minGap: 6, tolTouch: 0.3, tolBreak: 0.25, maxBreaks: 1, minTouches: 3, recent: 60, brokenAtr: 1.0, maxSlopeAtrPerBar: 0.6, window: 300, regN: 120, regMinR2: 0.5, regSigma: 2};

  function atrOf(bars, n){
    n = n || 14; const tr = [];
    for(let i = 1; i < bars.length; i++){const b = bars[i], p = bars[i - 1]; tr.push(Math.max(b.high - b.low, Math.abs(b.high - p.close), Math.abs(b.low - p.close)))}
    const t = tr.slice(-n); return t.length ? t.reduce((a, b) => a + b, 0) / t.length : null;
  }
  /* A swing high is a bar whose high beats the k bars before it and is not beaten by the k after it; confirmed pivots only (a bar needs k bars after it). */
  function pivots(bars, k){
    const hi = [], lo = [];
    for(let i = k; i < bars.length - k; i++){
      let isH = true, isL = true;
      for(let j = 1; j <= k; j++){
        if(!(bars[i].high > bars[i - j].high && bars[i].high >= bars[i + j].high)) isH = false;
        if(!(bars[i].low < bars[i - j].low && bars[i].low <= bars[i + j].low)) isL = false;
      }
      if(isH) hi.push({i, price: bars[i].high});
      if(isL) lo.push({i, price: bars[i].low});
    }
    return {highs: hi, lows: lo};
  }
  const lineAt = (a, s, i) => a.price + s * (i - a.i);

  /* The best line through pairs of `piv`. kind 'support' uses lows (price must stay above), 'resistance' uses highs (price must stay below). */
  function bestLine(bars, piv, kind, atr, o){
    const sgn = kind === 'support' ? 1 : -1, last = bars.length - 1;
    let best = null;
    for(let x = 0; x < piv.length; x++) for(let y = x + 1; y < piv.length; y++){
      const a = piv[x], b = piv[y];
      if(b.i - a.i < o.minGap) continue;
      const s = (b.price - a.price) / (b.i - a.i);
      if(Math.abs(s) > o.maxSlopeAtrPerBar * atr) continue;
      let breaks = 0;
      for(let j = a.i; j <= last; j++){
        const d = sgn * (bars[j].close - lineAt(a, s, j));            // positive = on the right side of the line
        if(d < -o.tolBreak * atr) breaks++;
      }
      if(breaks > o.maxBreaks) continue;
      const touches = piv.filter(c => c.i >= a.i && Math.abs(c.price - lineAt(a, s, c.i)) <= o.tolTouch * atr);
      if(touches.length < o.minTouches) continue;
      const lastTouch = touches[touches.length - 1].i;
      if(lastTouch < last - o.recent) continue;
      if(sgn * (bars[last].close - lineAt(a, s, last)) < -o.brokenAtr * atr) continue;      // already broken through by more than an ATR: not a line to draw
      const span = lastTouch - touches[0].i;
      const score = touches.length * 3 + Math.min(span, 100) / 10 - breaks * 2 + 3 * lastTouch / last;
      if(!best || score > best.score) best = {kind, i1: touches[0].i, p1: lineAt(a, s, touches[0].i), slope: s, touches: touches.length, firstTouch: touches[0].i, lastTouch, breaks, score, atr};
    }
    return best;
  }
  const yAt = (ln, i) => ln.p1 + ln.slope * (i - ln.i1);

  /* A parallel rail through the far extreme of the pivots between the line's first touch and now, kept only if two pivots touch it. */
  function railFor(line, piv, o, atr){
    const sgn = line.kind === 'support' ? 1 : -1;          // support's rail is above (positive offset), resistance's below
    const inside = piv.filter(c => c.i >= line.firstTouch);
    if(!inside.length) return null;
    const off = inside.reduce((m, c) => (sgn > 0 ? Math.max(m, c.price - yAt(line, c.i)) : Math.min(m, c.price - yAt(line, c.i))), sgn > 0 ? -Infinity : Infinity);
    if(!fin(off) || Math.abs(off) < 1.0 * atr) return null;   // a channel narrower than an ATR is noise
    const touches = inside.filter(c => Math.abs((c.price - yAt(line, c.i)) - off) <= o.tolTouch * atr);
    if(touches.length < 2) return null;
    return {offset: off, touches: touches.length, width: Math.abs(off)};
  }

  /* Least squares on the last N closes. r2 is the share of variance the line explains; the band is +/- sigma residual deviations. */
  function regression(bars, o){
    const n = Math.min(o.regN, bars.length); if(n < 30) return null;
    const seg = bars.slice(-n), base = bars.length - n; let sx = 0, sy = 0, sxx = 0, sxy = 0, syy = 0;
    seg.forEach((b, x) => {sx += x; sy += b.close; sxx += x * x; sxy += x * b.close; syy += b.close * b.close});
    const den = n * sxx - sx * sx; if(den === 0) return null;
    const m = (n * sxy - sx * sy) / den, c = (sy - m * sx) / n;
    let ssr = 0, sst = 0; const mean = sy / n;
    seg.forEach((b, x) => {const e = b.close - (c + m * x); ssr += e * e; sst += (b.close - mean) * (b.close - mean)});
    const r2 = sst > 0 ? 1 - ssr / sst : 0, sd = Math.sqrt(ssr / n);
    return {i1: base, p1: c, slope: m, r2, sigma: sd, bandWidth: o.regSigma * sd};
  }

  function fit(bars, opts){
    const o = Object.assign({}, OPT, opts || {});
    if(!Array.isArray(bars) || bars.length < 40 || !bars.every(b => b && fin(b.high) && fin(b.low) && fin(b.close))) return null;
    const off = Math.max(0, bars.length - o.window), win = bars.slice(off), atr = atrOf(win, 14);
    if(!atr || atr <= 0) return null;
    const pv = pivots(win, o.k), out = {atr, support: null, resistance: null, channel: null, regression: null, offset: off, last: win.length - 1};
    out.support = bestLine(win, pv.lows, 'support', atr, o);
    out.resistance = bestLine(win, pv.highs, 'resistance', atr, o);
    for(const ln of [out.support, out.resistance]) if(ln) ln.rail = railFor(ln, ln.kind === 'support' ? pv.highs : pv.lows, o, atr);
    // a channel is the line with the stronger rail; when both lines exist and run nearly parallel, that confirmation is reported too
    const cands = [out.support, out.resistance].filter(ln => ln && ln.rail);
    if(cands.length){
      const c = cands.sort((a, b) => (b.touches + b.rail.touches) - (a.touches + a.rail.touches))[0];
      out.channel = {base: c.kind, line: c, rail: c.rail, touches: [c.touches, c.rail.touches]};
    }
    if(out.support && out.resistance){
      const a = out.support.slope, b = out.resistance.slope, mx = Math.max(Math.abs(a), Math.abs(b), 0.02 * atr);
      out.parallel = Math.abs(a - b) <= 0.25 * mx;
    }
    if(!out.support && !out.resistance){
      const r = regression(win, o);
      if(r && r.r2 >= o.regMinR2 && r.bandWidth >= 0.5 * atr) out.regression = r;
    }
    return out;
  }
  /* Lines from another timeframe, expressed in TIME so they can be drawn on whatever chart is on screen. res.support / resistance are indexed
     into the window the fit ran on (res.offset into `bars`); slopeMs is price per millisecond. */
  function timeLines(res, bars, barMs){
    if(!res || !Array.isArray(bars) || !(barMs > 0)) return null;
    const conv = ln => {
      if(!ln) return null;
      const b = bars[res.offset + ln.i1]; if(!b || !fin(b.timestamp)) return null;
      return {kind: ln.kind, t1: b.timestamp, v1: ln.p1, slopeMs: ln.slope / barMs, touches: ln.touches, breaks: ln.breaks, rail: ln.rail ? {offset: ln.rail.offset, touches: ln.rail.touches, width: ln.rail.width} : null};
    };
    return {support: conv(res.support), resistance: conv(res.resistance), parallel: !!res.parallel, atr: res.atr};
  }
  const valueAt = (tl, t) => tl.v1 + tl.slopeMs * (t - tl.t1);
  /* Where a timestamp lands on a chart whose candles are `timestamps` (ascending, one every barMs): the nearest candle inside the data, a
     fractional-free extrapolation by whole bars beyond the last one, and the first candle for anything before the data starts. */
  function indexAt(timestamps, t, barMs){
    const n = timestamps.length; if(!n || !(barMs > 0)) return 0;
    if(t <= timestamps[0]) return 0;
    if(t >= timestamps[n - 1]) return n - 1 + Math.round((t - timestamps[n - 1]) / barMs);
    let lo = 0, hi = n - 1;
    while(hi - lo > 1){const mid = (lo + hi) >> 1; if(timestamps[mid] <= t) lo = mid; else hi = mid}
    return t - timestamps[lo] <= timestamps[hi] - t ? lo : hi;
  }
  root.PXTrend = {fit, pivots, atrOf, regression, timeLines, valueAt, indexAt, OPT};
  if(typeof module !== 'undefined') module.exports = root.PXTrend;
})(typeof window !== 'undefined' ? window : globalThis);
