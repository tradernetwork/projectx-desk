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
  /* Wide landscape (2560 wide): the left column has spare height, the right rail does not. Analyst callouts move to the left, between the size box
     and the ticket, so the map, timeframes, plan, risk and positions all fit on the right. Back in the right rail (before the blotter) otherwise. */
  const placeFeed = () => {
    const feed = document.getElementById('feed'), left = document.querySelector('.col-left'), ticket = document.getElementById('ticket'), blotter = document.getElementById('blotter');
    if(!feed || !left || !ticket || !blotter) return;
    const wide = root.dataset.layout === 'landscape' && innerWidth >= 2200;
    if(wide && feed.parentElement !== left) left.insertBefore(feed, ticket);
    else if(!wide && feed.parentElement === left) blotter.parentElement.insertBefore(feed, blotter);
  };
  apply();
  mq.addEventListener('change', () => {apply(); placeFeed()});
  addEventListener('resize', placeFeed);
  document.addEventListener('DOMContentLoaded', () => {
    apply(); placeFeed();
    const seg = document.getElementById('layout-seg');
    if(seg) seg.addEventListener('click', e => {
      const b = e.target.closest('button[data-v]');
      if(!b || !MODES.includes(b.dataset.v)) return;
      pref = b.dataset.v;
      try{localStorage.setItem(KEY, pref)}catch{}
      apply(); placeFeed();
    });
  });
})();
