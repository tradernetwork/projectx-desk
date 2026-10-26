/* Layout mode: Auto follows the monitor's orientation, Landscape and Portrait force it. Display only; no server state.
   Loaded in <head> so data-layout is set before first paint. localStorage can throw (private window): fall back to Auto. */
(() => {
  const KEY = 'px.layout', MODES = ['auto', 'landscape', 'portrait'], root = document.documentElement;
  const mq = matchMedia('(orientation: portrait)');
  let pref = 'auto';
  try{const v = localStorage.getItem(KEY); if(MODES.includes(v)) pref = v}catch{}
  const apply = () => {
    root.dataset.layout = pref === 'auto' ? (mq.matches ? 'portrait' : 'landscape') : pref;
    root.dataset.layoutPref = pref;
    for(const b of document.querySelectorAll('#layout-seg button')) b.setAttribute('aria-pressed', String(b.dataset.v === pref));
  };
  apply();
  mq.addEventListener('change', apply);
  document.addEventListener('DOMContentLoaded', () => {
    apply();
    const seg = document.getElementById('layout-seg');
    if(seg) seg.addEventListener('click', e => {
      const b = e.target.closest('button[data-v]');
      if(!b || !MODES.includes(b.dataset.v)) return;
      pref = b.dataset.v;
      try{localStorage.setItem(KEY, pref)}catch{}
      apply();
    });
  });
})();
