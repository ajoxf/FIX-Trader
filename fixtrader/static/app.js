/* The screen.
 *
 * It renders a snapshot and it sends commands. It computes nothing about
 * money — every figure here arrives already decided by the engine, because a
 * number worked out twice is a number that can disagree with itself.
 *
 * Two rules run through the whole file:
 *
 *   - **null renders as an em dash, never as 0.** A missing figure and a zero
 *     are different statements, and the second one is the dangerous kind.
 *   - **No native alert / confirm / prompt.** One shared modal; a test fails
 *     the build if the natives come back.
 */
'use strict';

const DASH = '—';
const state = {
  refresh: 500,
  timer: null,
  sound: true,
  closed: new Set(JSON.parse(localStorage.getItem('ft.closed') || '[]')),
  places: JSON.parse(localStorage.getItem('ft.places') || '{}'),
  lastEvent: {},
  focused: null,
  free: false,
  notify: { orders: false, fills: true, positions: true, rejects: true },
};

/* -- formatting ---------------------------------------------------------- */

function num(value, decimals) {
  if (value === null || value === undefined || Number.isNaN(value)) return DASH;
  return Number(value).toFixed(decimals === undefined ? 2 : decimals);
}
function signed(value, decimals) {
  if (value === null || value === undefined) return DASH;
  const s = Number(value).toFixed(decimals === undefined ? 2 : decimals);
  return Number(value) > 0 ? '+' + s : s;
}
function money(value) {
  if (value === null || value === undefined) return DASH;
  const sign = value < 0 ? '-' : '+';
  return sign + '$' + Math.abs(value).toLocaleString(undefined,
    { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}
function held(since) {
  if (!since) return DASH;
  const mins = Math.floor((Date.now() - new Date(since).getTime()) / 60000);
  if (mins < 60) return mins + 'm';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

/* -- sound: generated in the page, so nothing on the network can silence it */

let audio = null;
function beep(freqs, duration) {
  if (!state.sound) return;
  try {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    freqs.forEach((f, i) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = f;
      osc.type = 'sine';
      gain.gain.value = 0.05;
      osc.connect(gain); gain.connect(audio.destination);
      const start = audio.currentTime + i * duration;
      osc.start(start); osc.stop(start + duration);
    });
  } catch (e) { /* a browser that will not make sound is not a broken screen */ }
}
const SOUNDS = {
  ORDER: () => beep([660], 0.06),
  FILL: () => beep([660, 880], 0.08),
  OPEN: () => beep([660, 880], 0.08),
  CLOSED: () => beep([880, 660], 0.08),
  REJECT: () => beep([220], 0.18),
  GUARD: () => beep([220], 0.14),
};

/* -- toasts: errors stay until dismissed --------------------------------- */

function toast(kind, title, message, sub) {
  const host = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  const time = new Date().toLocaleTimeString([], { hour12: false });
  el.innerHTML = '<div class="hd"><span class="kind"></span>' +
    '<span class="tm"></span><span class="x">&times;</span></div>' +
    '<div class="msg"></div><div class="sub"></div>';
  el.querySelector('.kind').textContent = title;
  el.querySelector('.tm').textContent = time;
  el.querySelector('.msg').textContent = message;
  el.querySelector('.sub').textContent = sub || '';
  el.querySelector('.x').onclick = () => el.remove();
  host.prepend(el);
  if (SOUNDS[kind]) SOUNDS[kind]();
  // A reject or a withheld order STAYS: a failure that vanishes in three
  // seconds is one the operator misses.
  if (kind !== 'REJECT' && kind !== 'GUARD') {
    setTimeout(() => el.remove(), 12000);
  }
  while (host.children.length > 4) host.lastChild.remove();
}

/* -- the one modal ------------------------------------------------------- */

function ask(title, body, confirmLabel) {
  return new Promise((resolve) => {
    const modal = document.getElementById('modal');
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').textContent = body;
    const yes = document.getElementById('modal-confirm');
    const no = document.getElementById('modal-cancel');
    yes.textContent = confirmLabel || 'Confirm';
    modal.classList.remove('hidden');
    const done = (answer) => {
      modal.classList.add('hidden');
      yes.onclick = null; no.onclick = null;
      resolve(answer);
    };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);          // an unanswered prompt means NO
  });
}

/* -- talking to the engine ------------------------------------------------ */

async function command(action, contract, args) {
  const res = await fetch('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, contract: contract || '', args: args || {} }),
  });
  return res.json();
}

/* -- one window ----------------------------------------------------------- */

function windowFor(key) {
  let el = document.querySelector('.win[data-key="' + key + '"]');
  if (el) return el;
  const tpl = document.getElementById('contract-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = key;

  el.querySelector('.close').onclick = () => {
    state.closed.add(key);
    localStorage.setItem('ft.closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  el.querySelector('.sw').onclick = async () => {
    const on = el.querySelector('.sw').classList.contains('on');
    await command(on ? 'algo_off' : 'algo_on', key);
  };
  el.querySelector('.close-now').onclick = async () => {
    const name = el.querySelector('.title').textContent;
    const ok = await ask('Close ' + name + '?',
      'The position is closed at market, now — this crosses the spread. ' +
      'The algo on this contract is stood down with it, so it does not ' +
      're-enter on the next pass; switch it back on when you want it.',
      'CLOSE NOW');
    if (ok) { await command('close_now', key); toast('ORDER', 'CLOSING', key); }
  };
  el.querySelector('.cog').onclick = () => {
    toast('GUARD', 'NOT YET', 'The settings panel is the next build step.',
      key);
  };
  el.onmousedown = () => { state.focused = key; };
  makeDraggable(el, key);
  document.getElementById('desktop').appendChild(el);

  const place = state.places[key];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

function placeWindow(el, x, y) {
  document.getElementById('desktop').classList.add('free');
  state.free = true;
  el.classList.add('placed');
  el.style.left = x + 'px';
  el.style.top = y + 'px';
}

function makeDraggable(el, key) {
  const bar = el.querySelector('.titlebar');
  bar.addEventListener('mousedown', (down) => {
    if (down.target.closest('.winbtn')) return;
    const rect = el.getBoundingClientRect();
    const desk = document.getElementById('desktop').getBoundingClientRect();
    const dx = down.clientX - rect.left;
    const dy = down.clientY - rect.top;
    // Snapshot every other window's place first, or the ones still in the
    // grid jump to the corner the moment this one leaves it.
    document.querySelectorAll('.win').forEach((other) => {
      if (other === el || other.classList.contains('placed')) return;
      const r = other.getBoundingClientRect();
      placeWindow(other, r.left - desk.left + desk.left * 0,
        r.top - desk.top + document.getElementById('desktop').scrollTop);
      state.places[other.dataset.key] = {
        x: parseFloat(other.style.left), y: parseFloat(other.style.top) };
    });
    const move = (m) => {
      const x = m.clientX - desk.left - dx;
      const y = m.clientY - desk.top - dy + document.getElementById('desktop').scrollTop;
      placeWindow(el, Math.max(0, x), Math.max(0, y));
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      state.places[key] = { x: parseFloat(el.style.left), y: parseFloat(el.style.top) };
      localStorage.setItem('ft.places', JSON.stringify(state.places));
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    down.preventDefault();
  });
}

function renderContract(c) {
  if (state.closed.has(c.key)) return;
  const el = windowFor(c.key);
  const d = c.decimals === undefined ? 4 : c.decimals;
  const q = (sel) => el.querySelector(sel);

  q('.title').textContent = c.name;
  q('.venue').textContent = c.symbol || '';
  q('.state').textContent = c.state;
  q('.state').className = 'state s-' + c.state;
  el.classList.toggle('stale', !!(c.feed && c.feed.stale));

  const sw = q('.sw');
  sw.classList.toggle('on', !!c.algo_on);
  sw.querySelector('span').textContent = c.algo_on ? 'ON' : 'OFF';

  const m = c.market || {};
  q('.bidc .v').textContent = num(m.bid, d);
  q('.askc .v').textContent = num(m.ask, d);
  q('.bidc .sz').textContent = m.bid_size === null || m.bid_size === undefined
    ? DASH : String(m.bid_size);
  q('.askc .sz').textContent = m.ask_size === null || m.ask_size === undefined
    ? DASH : String(m.ask_size);
  q('.mid').textContent = num(m.mid, d);

  const s = c.stats || {};
  q('.f-mean').textContent = num(s.mean, d);
  q('.f-std').textContent = num(s.std, d);
  q('.f-z').textContent = signed(s.z, 2);
  q('.f-n').textContent = s.samples + '/' + s.need;
  q('.f-buyat').textContent = num(s.buy_at, d);
  q('.f-sellat').textContent = num(s.sell_at, d);
  q('.f-hurst').textContent = num(s.hurst, 2);
  q('.f-hl').textContent = s.half_life === null ? DASH : num(s.half_life, 1);

  // the z strip, drawn between the two entry thresholds
  const threshold = (c.settings && c.settings.entry_threshold) || 2;
  const span = threshold * 1.25;
  const strip = q('.zstrip');
  strip.querySelector('.lbl-lo').textContent = '-' + span.toFixed(1);
  strip.querySelector('.lbl-hi').textContent = '+' + span.toFixed(1);
  const mk = strip.querySelector('.mk');
  if (s.z === null || s.z === undefined) {
    mk.style.display = 'none';
  } else {
    mk.style.display = '';
    const pct = Math.max(0, Math.min(100, 50 + (s.z / span) * 50));
    mk.style.left = pct + '%';
    mk.className = 'mk' + (s.z >= threshold ? ' s' : s.z <= -threshold ? ' b' : '');
  }

  const warmed = s.is_warm;
  q('.warmbar').classList.toggle('full', !!warmed);
  q('.warmbar').classList.toggle('hidden', !!warmed);
  q('.warmrow').classList.toggle('hidden', !!warmed);
  q('.warmbar i').style.width = (s.warm_pct || 0) + '%';
  q('.f-warm').textContent = (s.warm_pct || 0).toFixed(0) + '% · ' +
    s.samples + '/' + s.need;

  const f = c.filters || {};
  const edge = q('.f-edge');
  if (f.edge_ratio === null || f.edge_ratio === undefined) {
    edge.innerHTML = '<span class="pill p-na">' + DASH + '</span>';
  } else {
    edge.innerHTML = num(f.edge_ratio, 1) + '× <span class="pill ' +
      (f.edge_ok ? 'p-pass">PASS' : 'p-block">BLOCK') + '</span>';
  }
  const blocked = q('.f-blocked');
  blocked.classList.toggle('hidden', !f.blocked_by);
  if (f.blocked_by) blocked.textContent = 'Withheld: ' + f.blocked_by;

  // position
  const pos = c.position;
  q('.posline').classList.toggle('hidden', !pos);
  q('.posgrid').classList.toggle('hidden', !pos);
  q('.flatline').classList.toggle('hidden', !!pos);
  q('.close-now').disabled = !pos;
  if (pos) {
    q('.posline .side').textContent = pos.side;
    q('.posline .side').className = 'side ' + pos.side;
    q('.posline .q').textContent = pos.qty;
    q('.posline .at').textContent = '@ ' + num(pos.avg_price, d);
    const pnl = q('.posline .pnl');
    pnl.textContent = money(pos.open_pnl);
    pnl.className = 'pnl ' + (pos.open_pnl > 0 ? 'up' : pos.open_pnl < 0 ? 'dn' : '');
    q('.p-zin').textContent = signed(pos.entry_z, 2);
    q('.p-be').textContent = num(pos.break_even, d);
    q('.p-tgt').textContent = num(pos.target, d);
    q('.p-stop').textContent = num(pos.stop, d);
    q('.p-held').textContent = held(pos.opened_at);
    q('.p-margin').textContent = pos.margin_locked === null ? DASH
      : '$' + Math.round(pos.margin_locked).toLocaleString();
    // Where the target cannot be priced, say WHICH figure is missing.
    const missing = q('.p-missing');
    missing.classList.toggle('hidden', !c.target_missing);
    if (c.target_missing) missing.textContent = 'No target — ' + c.target_missing;
  } else {
    const lo = num(s.buy_at, d), hi = num(s.sell_at, d);
    q('.flatline').textContent = warmed
      ? 'flat · buy ' + lo + ' / sell ' + hi
      : 'flat · collecting';
    q('.p-missing').classList.add('hidden');
  }

  // footer
  const foot = q('.wfoot');
  const age = c.feed && c.feed.age_sec;
  q('.age').textContent = age === null || age === undefined ? DASH : age + 's';
  q('.last-event').textContent = c.last_event || '';
  foot.className = 'wfoot' + (c.feed && c.feed.stale ? ' bad'
    : c.state === 'WARMING' ? ' warnf' : '');

  // A change of last_event is a thing that happened: say it once, and only
  // if the operator asked to hear about that kind. Order traffic is off by
  // default — every send and every fill on eight contracts buries the screen
  // it is meant to be reporting on.
  if (c.last_event && state.lastEvent[c.key] !== c.last_event) {
    if (state.lastEvent[c.key] !== undefined) {
      const kind = /reject/i.test(c.last_event) ? 'REJECT'
        : / out at /i.test(c.last_event) ? 'CLOSED'
          : /^(BUY|SELL) \d/i.test(c.last_event) ? 'OPEN'
            : 'ORDER';
      const want = state.notify;
      const allowed = kind === 'REJECT' ? want.rejects !== false
        : kind === 'CLOSED' || kind === 'OPEN' ? want.positions !== false
          : want.orders === true;
      if (allowed) toast(kind, kind, c.last_event, c.name);
    }
    state.lastEvent[c.key] = c.last_event;
  }
}

/* -- the Positions window -------------------------------------------------
 *
 * The per-contract window shows its own position. This is the one screen that
 * answers "what am I in, across everything" — and it puts what the VENUE says
 * beside what this book holds, because the two disagreeing is the whole
 * reason to look.
 */

function positionsWindow() {
  let el = document.querySelector('.win[data-key="__positions__"]');
  if (el) return el;
  const tpl = document.getElementById('positions-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.querySelector('.close').onclick = () => {
    state.closed.add('__positions__');
    localStorage.setItem('ft.closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  makeDraggable(el, '__positions__');
  document.getElementById('desktop').prepend(el);
  const place = state.places['__positions__'];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

function venueCell(r) {
  if (!r.venue_readable) return 'could not read';
  if (r.both_sides_open) {
    // Long and short at once is a close that went out as an open. It is
    // never netted to zero and shown as flat.
    return 'LONG ' + r.venue_long + ' + SHORT ' + r.venue_short;
  }
  if (r.venue_qty === null || r.venue_qty === undefined) return 'nothing';
  return signed(r.venue_qty, 0) + (r.agrees ? '' : '  ≠ book');
}

function renderPositions(snap) {
  if (state.closed.has('__positions__')) return;
  const p = snap.portfolio || { rows: [], venue_readable: true };
  const el = positionsWindow();
  const rows = p.rows || [];

  el.querySelector('.pv-count').textContent =
    rows.length + (rows.length === 1 ? ' open' : ' open');
  el.querySelector('.pv-empty').classList.toggle('hidden', rows.length > 0);

  // "could not read" is not "flat", and the table must not imply it is.
  const banner = el.querySelector('.pv-banner');
  const unreadable = p.venue_readable === false;
  banner.classList.toggle('hidden', !unreadable);
  if (unreadable) {
    banner.textContent = 'The venue could not be read, so what is shown is ' +
      'this book alone. It is NOT confirmation that the account is flat.';
  }

  const body = el.querySelector('.pv-rows');
  body.innerHTML = '';
  rows.forEach((r) => {
    const d = r.decimals === undefined ? 4 : r.decimals;
    const tr = document.createElement('tr');
    if (r.both_sides_open) tr.className = 'hedged';
    else if (r.venue_readable && !r.agrees) tr.className = 'disagrees';
    const cells = [
      ['txt', r.name],
      ['txt', r.side ? '<span class="tag ' + r.side + '">' + r.side + '</span>' : DASH],
      ['r', r.qty === null ? DASH : String(r.qty)],
      ['r', num(r.avg_price, d)],
      ['r', num(r.mid, d)],
      ['r', signed(r.entry_z, 2)],
      ['r', num(r.break_even, d)],
      ['r', num(r.target, d)],
      ['r', num(r.stop, d)],
      ['r', held(r.opened_at)],
      ['r', r.margin_locked === null || r.margin_locked === undefined ? DASH
        : '$' + Math.round(r.margin_locked).toLocaleString()],
      ['r pnl ' + (r.open_pnl > 0 ? 'up' : r.open_pnl < 0 ? 'dn' : ''), money(r.open_pnl)],
      ['txt', venueCell(r)],
      ['tickets', (r.tickets || []).join(' ') || DASH],
    ];
    cells.forEach(([cls, html]) => {
      const td = document.createElement('td');
      td.className = cls;
      td.innerHTML = html;
      tr.appendChild(td);
    });
    const act = document.createElement('td');
    const btn = document.createElement('button');
    btn.className = 'btn danger';
    btn.textContent = 'CLOSE';
    btn.disabled = !r.side;
    btn.onclick = async () => {
      const ok = await ask('Close ' + r.name + '?',
        'The position is closed at market by its own tickets, now. The algo ' +
        'on this contract is stood down with it.', 'CLOSE NOW');
      if (ok) await command('close_now', r.key);
    };
    act.appendChild(btn);
    tr.appendChild(act);
    body.appendChild(tr);
  });

  const total = el.querySelector('.pv-total');
  total.innerHTML = '';
  if (rows.length) {
    const spec = [['txt', rows.length + ' open'], ['', ''], ['', ''], ['', ''],
      ['', ''], ['', ''], ['', ''], ['', ''], ['', ''], ['', ''],
      ['r', p.margin === null ? DASH : '$' + Math.round(p.margin).toLocaleString()],
      ['r', money(p.open_pnl)], ['', ''], ['', ''], ['', '']];
    spec.forEach(([cls, text]) => {
      const td = document.createElement('td');
      td.className = cls;
      td.textContent = text;
      total.appendChild(td);
    });
  }

  el.querySelector('.pv-foot').textContent =
    'realised today ' + money(p.realised_today) + ' · ' +
    (p.trades_today || 0) + ' trades';
  el.querySelector('.pv-day').textContent = p.venue_readable
    ? 'book and venue agree' : 'venue unreadable';
}

/* -- chrome --------------------------------------------------------------- */

function renderTabs() {
  const host = document.getElementById('tabs');
  host.innerHTML = '';
  document.querySelectorAll('.win').forEach((el) => {
    const b = document.createElement('button');
    b.className = 'tk' + (state.focused === el.dataset.key ? ' act' : '');
    b.textContent = el.querySelector('.title').textContent;
    if (el.dataset.key === '__positions__') b.classList.add('wide-tab');
    b.onclick = () => { el.scrollIntoView({ block: 'nearest' }); state.focused = el.dataset.key; };
    host.appendChild(b);
  });
}

function renderChrome(snap) {
  const engine = snap.engine || {};
  const badge = document.getElementById('env-badge');
  badge.textContent = engine.environment || DASH;
  badge.className = 'env' + (engine.environment === 'PROD' ? ' prod'
    : engine.simulated ? ' sim' : '');

  const banner = document.getElementById('engine-banner');
  if (engine.alive === false) {
    banner.classList.remove('hidden');
    banner.classList.add('critical');
    banner.textContent = engine.text ||
      'the engine is not publishing — these prices are not live';
  } else if (engine.killed) {
    banner.classList.remove('hidden');
    banner.classList.remove('critical');
    banner.textContent = 'KILL ALL is on — every algo is stood down and ' +
      'our working orders are cancelled. Positions are untouched.';
  } else if (engine.book_complete === false) {
    banner.classList.remove('hidden');
    banner.classList.add('critical');
    banner.textContent = 'The venue could not be read, so this book is ' +
      'INCOMPLETE. Nothing will be closed automatically.';
  } else {
    banner.classList.add('hidden');
  }

  const unclaimed = document.getElementById('unclaimed-banner');
  const items = engine.unclaimed || [];
  unclaimed.classList.toggle('hidden', items.length === 0);
  if (items.length) {
    unclaimed.textContent = 'UNCLAIMED — ' +
      items.map((u) => u.text).join('  ');
  }

  const link = document.getElementById('link-badge');
  const session = engine.session || {};
  link.textContent = session.state === 'LOGGED_ON'
    ? (engine.simulated ? 'SIMULATED' : 'CONNECTED')
    : (session.text || 'no session');
  link.className = 'link ' + (session.state === 'LOGGED_ON'
    ? (engine.simulated ? 'part' : 'ok') : 'bad');

  const master = document.getElementById('master-toggle');
  master.textContent = 'Master: ' + (engine.master_algo ? 'ON' : 'OFF');
  master.classList.toggle('act', !!engine.master_algo);

  const stat = document.getElementById('loop-stat');
  stat.textContent = 'loop ' + (engine.loop_ms === undefined ? DASH
    : engine.loop_ms + 'ms') +
    (engine.snapshot_age_sec !== null && engine.snapshot_age_sec !== undefined
      ? ' · ' + engine.snapshot_age_sec + 's' : '');

  if (engine.notify) state.notify = engine.notify;
  if (engine.refresh_sec) {
    const ms = Math.round(engine.refresh_sec * 1000);
    if (ms !== state.refresh) { state.refresh = ms; restartTimer(); }
  }
}

/* -- the loop ------------------------------------------------------------- */

async function tick() {
  try {
    const res = await fetch('/api/snapshot', { cache: 'no-store' });
    const snap = await res.json();
    renderChrome(snap);
    const seen = new Set(['__positions__']);
    (snap.contracts || []).forEach((c) => { seen.add(c.key); renderContract(c); });
    renderPositions(snap);
    document.querySelectorAll('.win').forEach((el) => {
      if (!seen.has(el.dataset.key)) el.remove();
    });
    renderTabs();
  } catch (e) {
    const banner = document.getElementById('engine-banner');
    banner.classList.remove('hidden');
    banner.classList.add('critical');
    banner.textContent = 'The screen cannot reach its own web process. ' +
      'These prices are not live.';
  }
}

function restartTimer() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(tick, state.refresh);
}

/* -- wiring --------------------------------------------------------------- */

document.getElementById('kill').onclick = async () => {
  const ok = await ask('KILL ALL?',
    'Every algo is stood down and every working order of ours is cancelled. ' +
    'Open positions are NOT closed.', 'KILL ALL');
  if (ok) { await command('kill_all', '', { close_positions: false }); }
};

document.getElementById('master-toggle').onclick = async () => {
  const on = document.getElementById('master-toggle').classList.contains('act');
  await command('master_algo', '', { on: !on });
};

document.getElementById('sound-toggle').onclick = (e) => {
  state.sound = !state.sound;
  e.target.textContent = 'Sound: ' + (state.sound ? 'on' : 'off');
};

document.getElementById('tidy').onclick = () => {
  state.places = {};
  localStorage.removeItem('ft.places');
  state.free = false;
  document.getElementById('desktop').classList.remove('free');
  document.querySelectorAll('.win').forEach((el) => {
    el.classList.remove('placed');
    el.style.left = ''; el.style.top = '';
  });
};

document.getElementById('add-panel').onclick = () => {
  const menu = document.getElementById('add-menu');
  menu.innerHTML = '';
  if (state.closed.size === 0) {
    const b = document.createElement('button');
    b.textContent = 'every window is already open';
    b.disabled = true;
    menu.appendChild(b);
  }
  state.closed.forEach((key) => {
    const b = document.createElement('button');
    b.textContent = key === '__positions__' ? 'Positions' : key;
    b.onclick = () => {
      state.closed.delete(key);
      localStorage.setItem('ft.closed', JSON.stringify([...state.closed]));
      menu.classList.add('hidden');
      tick();
    };
    menu.appendChild(b);
  });
  menu.classList.toggle('hidden');
};

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    document.getElementById('modal').classList.add('hidden');
    document.getElementById('add-menu').classList.add('hidden');
  }
});

tick();
restartTimer();
