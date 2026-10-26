/* Dealer-map words and level logic, pure (no DOM, no chart) so node can test them. Display only: the map describes dealer hedging, it never says buy or sell.
   desk.js draws with these; tests/test_tmap.py runs them. */
(function(root){
  const num = v => typeof v === 'number' && isFinite(v);
  /* What each level means in ordinary words. The chart chip carries the short gloss; the panel's key carries the sentence. Description, never advice. */
  const TM_ROLES = {
    'PIN': ['magnet', 'The biggest gamma strike right near price. Price gravitates to it and wrestles around it, like a magnet. Expect chop around it.'],
    'FLIP': ['chasing ends', 'Where dealers flip from net short gamma to net long. Under it they chase price, so moves speed up. Over it they brake price, so moves slow down.'],
    'ACCELERANT': ['speeds up', 'A short-gamma strike above price. Once price pushes through it, dealers have to buy into the rise, so the move speeds up. It is not a wall.'],
    'BRAKE': ['stalls', 'A long-gamma strike above price. Dealers sell into strength here, so rallies tend to stall. A wick over is normal; closing over is the real test.'],
    'CEILING': ['move dies', 'The heaviest long-gamma strike above. The move tends to die here.'],
    'TRAPDOOR': ['breakdown point', 'A breakdown point, not a buy level. Dealers are short gamma here, so if price falls through it they sell into the drop and it speeds up. Holding above it means nothing special.'],
    'LAST SHELF': ['last support', 'The last long-gamma strike below. Dealers buy dips here, so it is the last thing to lean on. Lose it and there is little underneath.'],
    'THIN CUSHION': ['small support', 'A small pocket of dealer buying below. Some support, but small.'],
    'EMPTY': ['no support', 'A stretch with no gamma at all. Nothing holds price here, so it can slide fast.'],
    'PUT WALL': ['fight, not floor', 'Heavy put positioning. A crowd, not a floor: lots of action here, and it can give way.'],
  };
  const TM_PAIRS = {   // two roles on the same strike: say what the COMBINATION means
    'ACCELERANT+PIN': 'Price is sitting on the biggest gamma strike, and dealers are short gamma there. Expect whippy chop around it, then a fast move once it breaks away, because dealers chase instead of fading.',
    'TRAPDOOR+PIN': 'Price is sitting on the biggest gamma strike, and it is also a breakdown point. Expect chop around it; if it gives way below, selling feeds on itself and the drop speeds up. It is not a buy level.',
    'LAST SHELF+PIN': 'Price is sitting on the biggest gamma strike, and it is also the last support below. Dealers defend it, so it tends to hold and settle. If it breaks there is little underneath.',
    'PUT WALL+PIN': 'Price is sitting on the biggest gamma strike, and it is the put wall: a crowd, not a floor. Expect a fight here, and it can give way.',
    'BRAKE+PIN': 'Price is sitting on the biggest gamma strike, and dealers sell rallies into it. Expect it to stall and chop here.',
    'CEILING+PIN': 'Price is sitting on the biggest gamma strike, and it is the ceiling above. The move tends to die here.',
  };
  const TM_EMO = {'PIN': '\u{1F9F2}', 'FLIP': '\u{1F504}', 'ACCELERANT': '\u{1F680}', 'BRAKE': '\u{1F6D1}', 'CEILING': '\u{1F9F1}', 'TRAPDOOR': '\u{1F6AA}',
    'LAST SHELF': '\u{1F6E1}\uFE0F', 'THIN CUSHION': '\u{1FAA3}', 'EMPTY': '\u{1F573}\uFE0F', 'PUT WALL': '\u{1F94A}'};      // magnet, flip, rocket, stop, brick wall, door, shield, bucket, hole, glove
  const TM_WATCH = {   // what to WATCH at each level, and what would change it. Never "buy" or "sell": the map describes dealer hedging, not a trade.
    'PIN': 'Price tends to wrestle around a pin: expect chop, not a trend.',
    'FLIP': 'This is the regime line. Over it dealers brake moves; under it they chase them.',
    'ACCELERANT': 'Watch for a push through. If it goes, dealers buy into the rise and it speeds up. Under it, nothing special.',
    'BRAKE': 'Watch for a stall. Rallies tend to fade here; closing over it is the real test.',
    'CEILING': 'Watch for the move to die here. Closing over it removes the ceiling.',
    'TRAPDOOR': 'Watch for a close below. If it goes, selling can feed on itself and the drop speeds up. Holding above, nothing special.',
    'LAST SHELF': 'Watch whether it holds. Dealers buy dips here; closing below it removes the last support.',
    'THIN CUSHION': 'Small support. If it fails there is little underneath.',
    'EMPTY': 'No support in this stretch. If price enters it, it can slide.',
    'PUT WALL': 'A fight, not a floor. Expect a messy reaction either way.',
  };
  const tmRole = tag => String(tag || '').replace(/^[0-9.]+\s*/, '');
  function tmapLevels(m){      // one entry per strike, roles merged (a pin on the same strike as another level), top to bottom like the chart
    const byPrice = new Map();
    for(const rd of m.roads) for(const w of rd.waypoints){const e = byPrice.get(w.price) || {price: w.price, tags: [], road: rd.key}; e.tags.push(w.tag); byPrice.set(w.price, e)}
    return [...byPrice.values()].map(e => ({...e, roles: [...new Set(e.tags.map(tmRole))]})).sort((a, b) => b.price - a.price);
  }
  /* The right-now line. p: last price; emoji: show the role emoji. Same words the panel always had. */
  function nowText(m, p, emoji){
    if(!num(p)) return '';
    const lv = tmapLevels(m); if(!lv.length) return 'No levels to watch on this map.';
    const tol = Math.max(0.1 * (m.typical_move || 0), 1), near = lv.slice().sort((a, b) => Math.abs(a.price - p) - Math.abs(b.price - p))[0];
    const name = it => (emoji ? it.roles.map(r => TM_EMO[r] || '').join('') + ' ' : '') + it.price + ' ' + it.roles.join(' + ');
    if(Math.abs(near.price - p) <= tol){
      const key = near.roles.slice().sort((a, b) => (a === 'PIN') - (b === 'PIN') || a.localeCompare(b)).join('+');
      const what = near.roles.length > 1 ? near.roles.filter(r => r !== 'PIN').map(r => TM_WATCH[r]).join(' ') + (near.roles.includes('PIN') ? ' ' + TM_WATCH.PIN : '') : TM_WATCH[near.roles[0]];
      return `Price ${p} is AT the ${name(near)}. ${what} A thing to WATCH, not a signal.`;
    }
    const up = lv.filter(i => i.price > p).pop(), dn = lv.find(i => i.price < p);       // nearest level above and below
    const gap = i => Math.abs(i.price - p).toFixed(2).replace(/\.?0+$/, '');
    return `Price ${p} is between ${dn ? name(dn) + ' (' + gap(dn) + ' below)' : 'nothing below'} and ${up ? name(up) + ' (' + gap(up) + ' above)' : 'nothing above'}. Nothing is being tested right now: just watch.`;
  }
  /* A breakdown point and the last support are warnings about what happens if price LOSES them, so they never take the road's "go" green. */
  const WARN = '#ff9f1a';
  const roleColor = (role, fallback) => (role === 'TRAPDOOR' || role === 'LAST SHELF') ? WARN : fallback;
  root.PXTmap = {TM_ROLES, TM_PAIRS, TM_EMO, TM_WATCH, tmRole, tmapLevels, nowText, roleColor, WARN};
  if(typeof module !== 'undefined') module.exports = root.PXTmap;
})(typeof window !== 'undefined' ? window : globalThis);
