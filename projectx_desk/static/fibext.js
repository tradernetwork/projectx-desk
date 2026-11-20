/* Fibonacci extension arithmetic for the trader's own `userFibExt` drawing, pure (no DOM, no chart) so node can test it.
   Display only: three points the trader placed in, prices out. Nothing here is a signal, nothing is read by the guard or sizing,
   and there is no request of any kind. desk.js draws with it; tests/test_fibext.py runs it.
   Points: p0 swing start, p1 swing end, p2 retracement end. Level price = p2 + (p1 - p0) x ratio, so a downward first leg
   projects downward from p2 and an upward leg projects upward. Ratio 0 is p2 itself. */
(function(root){
  const RATIOS = [0, 0.618, 1, 1.272, 1.618, 2, 2.618];
  const num = v => typeof v === 'number' && isFinite(v);

  /* [{ratio, price}] or [] when any of the three prices is missing or not finite (never a made-up level). */
  function fibExtLevels(p0, p1, p2){
    if(!num(p0) || !num(p1) || !num(p2)) return [];
    const leg = p1 - p0;
    return RATIOS.map(r => ({ratio: r, price: p2 + leg * r}));
  }
  /* "7712.50 (1.618)": the price at the chart's own precision and the ratio without trailing zeros. */
  function fibExtLabel(price, ratio, dec){
    if(!num(price) || !num(ratio)) return '';
    return price.toFixed(num(dec) ? dec : 2) + ' (' + String(+ratio.toFixed(3)) + ')';
  }

  root.PXFibExt = {RATIOS, fibExtLevels, fibExtLabel};
  if(typeof module !== 'undefined') module.exports = root.PXFibExt;
})(typeof window !== 'undefined' ? window : globalThis);
