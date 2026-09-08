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

/* Send a command and WAIT for the engine's answer to it.
 *
 * Two reasons this does not just post and forget. A refusal has to be seen —
 * an algo switch the engine declined must not sit on the screen looking
 * armed. And a control has to feel immediate: without this the switch only
 * moves when the next snapshot lands, which is up to a screen refresh after
 * the engine has already acted.
 *
 * Resolves to the ENGINE's result, not the acknowledgement of the post.
 */
async function command(action, contract, args) {
  const res = await fetch('/api/command', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action, contract: contract || '', args: args || {} }),
  });
  const posted = await res.json();
  if (!posted.ok || !posted.id) {
    toast('REJECT', 'NOT SENT', posted.error || 'the command was not accepted',
      contract || '');
    return posted;
  }
  const answer = await commandResult(posted.id);
  if (answer === null) {
    // No answer is NOT success. The engine may be down, and a switch that
    // moved anyway would be a screen describing a system that is not there.
    toast('REJECT', 'NO ANSWER',
      'the engine did not answer ' + action + ' — it may not be running',
      contract || '');
    return { ok: false, error: 'no answer from the engine' };
  }
  if (answer.ok === false) {
    toast('REJECT', 'REFUSED', answer.error || 'the engine refused it',
      contract || '');
  }
  tick();                    // show the new state now, not at the next poll
  return answer;
}

/* Poll for one command's result. Bounded: a click must not hang for ever on
 * an engine that is not going to answer. */
async function commandResult(id, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 2000);
  while (Date.now() < deadline) {
    try {
      const r = await (await fetch('/api/result/' + encodeURIComponent(id),
        { cache: 'no-store' })).json();
      if (!r.pending) return r;
    } catch (e) { return null; }
    await new Promise((done) => setTimeout(done, 30));
  }
  return null;
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
  el.querySelector('.cog').onclick = () => configWindow(key);
  el.onmousedown = () => { state.focused = key; raise(el); };
  makeDraggable(el, key);
  document.getElementById('desktop').appendChild(el);

  const place = state.places[key];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

/* Bring a window to the front. The desk paints in DOM order otherwise, so a
 * window underneath another could never be read — and one of them is the
 * Positions window. */
let topZ = 5;
function raise(el) {
  document.querySelectorAll('.win.raised').forEach((w) => {
    if (w !== el) { w.classList.remove('raised'); w.style.zIndex = ''; }
  });
  el.classList.add('raised');
  el.style.zIndex = String(++topZ);
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
  // An em dash always carries its reason. Hurst reads high on a coarsely
  // quantised spread, so it ships OFF — and "off" and "could not be
  // computed" are different statements about the same dash.
  q('.f-hurst').title = (s.hurst !== null && s.hurst !== undefined)
    ? 'Hurst on the increments; below ' +
      num((c.filters || {}).hurst_threshold, 2) + ' is mean-reverting'
    : ((c.filters || {}).hurst_enabled === false
      ? 'the Hurst filter is off for this contract, so it is not computed'
      : 'not enough of a window to estimate it yet');
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
  el.onmousedown = () => raise(el);
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

/* -- the Analysis window --------------------------------------------------
 *
 * The feedback loop. One contract at a time by default, because a win rate
 * blended over eight of them cannot answer the only question it exists for.
 * It fetches its own data on a slow timer — this is history, not a market,
 * and re-reading it twice a second would be pointless work on the desk's
 * critical path.
 */

const analysis = { key: null, period: 'all', mode: 'live', timer: null,
                   modeChosen: false };

function analysisWindow() {
  let el = document.querySelector('.win[data-key="__analysis__"]');
  if (el) return el;
  const tpl = document.getElementById('analysis-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.querySelector('.close').onclick = () => {
    state.closed.add('__analysis__');
    localStorage.setItem('ft.closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  el.querySelector('.an-period').onchange = (e) => {
    analysis.period = e.target.value; loadAnalysis();
  };
  el.querySelector('.an-mode').onchange = (e) => {
    analysis.mode = e.target.value;
    analysis.modeChosen = true;        // a chosen filter is never overridden
    loadAnalysis();
  };
  el.onmousedown = () => raise(el);
  makeDraggable(el, '__analysis__');
  document.getElementById('desktop').appendChild(el);
  const place = state.places['__analysis__'];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

function tile(k, v, sub, cls) {
  return '<div class="tile"><div class="k">' + k + '</div>' +
    '<div class="v ' + (cls || '') + '">' + v + '</div>' +
    '<div class="s">' + (sub || '') + '</div></div>';
}

function minutes(m) {
  if (m === null || m === undefined) return DASH;
  if (m < 60) return Math.round(m) + 'm';
  return Math.floor(m / 60) + 'h ' + Math.round(m % 60) + 'm';
}

function seconds(s) {
  if (s === null || s === undefined) return DASH;
  return s < 90 ? Math.round(s) + 's' : minutes(s / 60);
}

function pct(v, d) {
  return (v === null || v === undefined) ? DASH : Number(v).toFixed(d || 1) + '%';
}

function bar(width, negative) {
  return '<div class="cbar"><i class="' + (negative ? 'neg' : 'pos') +
    '" style="width:' + Math.max(2, Math.min(100, width)) + '%"></i></div>';
}

function renderTiles(el, s) {
  el.querySelector('.an-tiles').innerHTML =
    tile('Trades', s.trades, s.won + ' won / ' + s.lost + ' lost' +
      (s.unmeasured ? ' · ' + s.unmeasured + ' unmeasured' : '')) +
    tile('Win rate', pct(s.win_rate), 'of ' + s.measured + ' measured') +
    tile('Net P&L', money(s.net), s.fees === null ? 'costs not measured'
      : 'after ' + money(s.fees).replace('+', '') + ' costs',
      s.net > 0 ? 'up' : s.net < 0 ? 'dn' : '') +
    tile('Avg / trade', money(s.avg),
      'win ' + money(s.avg_win) + ' · loss ' + money(s.avg_loss),
      s.avg > 0 ? 'up' : s.avg < 0 ? 'dn' : '') +
    tile('Expectancy', money(s.expectancy), 'per trade, weighted',
      s.expectancy > 0 ? 'up' : s.expectancy < 0 ? 'dn' : '') +
    tile('On margin', pct(s.on_margin, 2),
      s.on_margin === null ? 'margin not reported' : 'return on what was tied up',
      s.on_margin > 0 ? 'up' : s.on_margin < 0 ? 'dn' : '') +
    tile('Cost drag', pct(s.cost_drag), 'of gross') +
    tile('Avg hold', minutes(s.avg_hold_min), '') +
    tile('Worst run', money(s.worst_run), 'consecutive losses',
      s.worst_run < 0 ? 'dn' : '');
}

function renderTouches(el, study, threshold) {
  const body = el.querySelector('.an-touches tbody');
  body.innerHTML = '';
  const most = Math.max(1, ...study.levels.map((l) => l.touches));
  study.levels.forEach((l) => {
    const tr = document.createElement('tr');
    const tag = Math.abs(l.level) >= (threshold || 2)
      ? (l.level > 0 ? 't-sell' : 't-buy') : 't-cxl';
    tr.innerHTML =
      '<td><span class="tag ' + tag + '">' +
        (l.level > 0 ? '+' : '') + l.level.toFixed(0) + ' SD</span></td>' +
      '<td class="r">' + l.touches + '</td>' +
      '<td>' + bar(100 * l.touches / most, l.level < 0) + '</td>' +
      '<td class="r">' + pct(l.reverted_pct) + '</td>' +
      '<td class="r">' + seconds(l.median_seconds) + '</td>' +
      '<td class="r">' + (l.median_adverse_sigma === null ? DASH
        : l.median_adverse_sigma.toFixed(1) + 'σ') + '</td>' +
      '<td class="r">' + l.traded + '</td>' +
      '<td class="r">' + (l.unresolved || DASH) + '</td>';
    // A level whose move cannot cover the round trip is dimmed: it may
    // revert beautifully and still lose money every time.
    if (l.pays === false) tr.className = 'cannot-pay';
    body.appendChild(tr);
  });

  const finding = el.querySelector('.an-touch-finding');
  const tail = study.unresolved
    ? ' ' + study.unresolved + ' touch(es) still running are counted ' +
      'separately and left out of the percentages.' : '';
  finding.classList.remove('hidden', 'warn');
  if (study.best_level && study.best_reverted_pct !== null) {
    finding.innerHTML = '<b>±' + study.best_level.toFixed(1) +
      ' is the level to trade on this contract</b> — ' +
      study.best_reverted_pct.toFixed(0) + '% of those touches reverted, and ' +
      'the move back to the mean covers the round trip. The entry threshold ' +
      'is currently ±' + (threshold || 2).toFixed(2) + '.' + tail;
  } else if (study.nothing_pays) {
    // Rather than falling back to the innermost band and calling it an answer.
    finding.classList.add('warn');
    finding.innerHTML = '<b>No level covers its own round trip on this ' +
      'contract.</b> The inner bands revert more often, as they always do, ' +
      'but the move back to the mean does not pay for the trade — this is a ' +
      'costs problem or a contract to leave alone, not a threshold to lower.' +
      tail;
  } else {
    finding.innerHTML = 'Not enough resolved touches yet to say which level ' +
      'is worth trading.' + tail;
  }
}

function renderExits(el, rows) {
  const body = el.querySelector('.an-exits tbody');
  body.innerHTML = '';
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="txt">' + r.reason.toLowerCase().replace(/_/g, ' ') +
      '</td><td class="r">' + r.count + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
      money(r.net) + '</td>';
    body.appendChild(tr);
  });
  if (!rows.length) {
    body.innerHTML = '<tr><td class="txt" colspan="3">nothing closed yet</td></tr>';
  }
}

function renderCosts(el, costs, key) {
  const body = el.querySelector('.an-costs tbody');
  body.innerHTML = '';
  costs.lines.forEach((l) => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td class="txt">' + l.item + '</td>' +
      '<td class="r">' + money(l.budgeted) + '</td>' +
      '<td class="r">' + money(l.actual) + '</td>' +
      '<td class="r">' + (l.diff === null ? DASH : money(l.diff)) + '</td>';
    body.appendChild(tr);
  });
  const total = document.createElement('tr');
  total.innerHTML = '<td class="txt"><b>round trip</b></td>' +
    '<td class="r">' + money(costs.total.budgeted) + '</td>' +
    '<td class="r">' + money(costs.total.actual) + '</td>' +
    '<td class="r">' + money(costs.total.diff) + '</td>';
  body.appendChild(total);

  const ticks = document.createElement('tr');
  ticks.innerHTML = '<td class="txt">slippage, per side</td>' +
    '<td class="r">' + (costs.budget_ticks === null ? DASH
      : costs.budget_ticks.toFixed(2) + ' tk') + '</td>' +
    '<td class="r">' + (costs.measured_ticks === null ? DASH
      : costs.measured_ticks.toFixed(2) + ' tk') + '</td>' +
    '<td class="r">' + (costs.unmeasured_fills
      ? costs.unmeasured_fills + ' unmeasured' : '') + '</td>';
  body.appendChild(ticks);

  const finding = el.querySelector('.an-cost-finding');
  finding.innerHTML = '';
  if (!costs.finding) { finding.classList.add('hidden'); return; }
  finding.classList.remove('hidden');
  const text = document.createElement('span');
  text.innerHTML = '<b>' + costs.finding.text + '</b>';
  finding.appendChild(text);
  const apply = document.createElement('button');
  apply.className = 'btn sm';
  apply.style.marginTop = '5px';
  apply.textContent = 'Set the budget to ' +
    costs.finding.suggest_ticks.toFixed(2);
  // It PROPOSES. Nothing in this system applies its own findings.
  apply.onclick = async () => {
    const res = await fetch('/api/contracts/' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slippage_budget_ticks: costs.finding.suggest_ticks }),
    });
    const data = await res.json();
    if (data.ok) {
      toast('OPEN', 'APPLIED', 'Slippage budget set to ' +
        costs.finding.suggest_ticks.toFixed(2) + ' ticks', key);
      loadAnalysis();
    } else {
      toast('REJECT', 'NOT APPLIED', data.error || 'refused', key);
    }
  };
  finding.appendChild(apply);
}

function renderJournal(el, rows, decimals) {
  const body = el.querySelector('.an-journal tbody');
  body.innerHTML = '';
  const when = (iso) => iso ? new Date(iso).toLocaleString([],
    { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
    : DASH;
  rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + when(r.opened_at) + '</td><td>' + when(r.closed_at) + '</td>' +
      '<td><span class="tag ' + (r.side === 'BUY' ? 't-buy' : 't-sell') + '">' +
        (r.side === 'BUY' ? 'LONG' : 'SHORT') + '</span></td>' +
      '<td class="r">' + r.qty + '</td>' +
      '<td class="r">' + signed(r.entry_z, 2) + '</td>' +
      '<td class="r">' + signed(r.exit_z, 2) + '</td>' +
      '<td class="r">' + num(r.entry_price, decimals) + '</td>' +
      '<td class="r">' + num(r.exit_price, decimals) + '</td>' +
      '<td class="r">' + money(r.gross) + '</td>' +
      '<td class="r">' + money(r.fees) + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
        money(r.net) + '</td>' +
      '<td class="r">' + pct(r.on_margin, 2) + '</td>' +
      '<td class="r">' + minutes(r.held_min) + '</td>' +
      '<td class="txt">' + (r.exit_reason || DASH).toLowerCase().replace(/_/g, ' ') +
        (r.simulated ? ' <span class="tag t-cxl">sim</span>' : '') + '</td>';
    body.appendChild(tr);
  });
  if (!rows.length) {
    body.innerHTML = '<tr><td class="txt" colspan="14">no closed trades in ' +
      'this period</td></tr>';
  }
}

function renderDesk(el, report) {
  const body = el.querySelector('.an-desk-table tbody');
  body.innerHTML = '';
  const best = Math.max(1, ...report.rows.map((r) => Math.abs(r.net || 0)));
  report.rows.forEach((r) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="txt">' + r.name + '</td>' +
      '<td class="r">' + r.trades + '</td>' +
      '<td class="r">' + pct(r.win_rate) + '</td>' +
      '<td>' + bar(100 * Math.abs(r.net || 0) / best, (r.net || 0) < 0) + '</td>' +
      '<td class="r ' + (r.net > 0 ? 'up' : r.net < 0 ? 'dn' : '') + '">' +
        money(r.net) + '</td>' +
      '<td class="r">' + money(r.avg) + '</td>' +
      '<td class="r">' + pct(r.on_margin, 2) + '</td>' +
      '<td class="r">' + pct(r.cost_drag) + '</td>' +
      '<td class="r">' + minutes(r.avg_hold_min) + '</td>' +
      '<td class="r">' + (r.best_level ? '±' + r.best_level.toFixed(1) : DASH) + '</td>' +
      '<td class="txt">' + r.verdict + '</td>';
    body.appendChild(tr);
  });
  const t = report.total;
  el.querySelector('.an-desk-total').innerHTML =
    '<td class="txt"><b>All, blended</b></td><td class="r">' + t.trades +
    '</td><td class="r">' + pct(t.win_rate) + '</td><td></td>' +
    '<td class="r">' + money(t.net) + '</td><td class="r">' + money(t.avg) +
    '</td><td class="r">' + pct(t.on_margin, 2) + '</td><td class="r">' +
    pct(t.cost_drag) + '</td><td class="r">' + minutes(t.avg_hold_min) +
    '</td><td></td><td></td>';
  el.querySelector('.an-desk-note').innerHTML =
    'A contract with fewer than <b>' + report.min_trades_for_a_verdict +
    '</b> closed trades is marked <b>too few to judge</b> and is never given ' +
    'a verdict on a win rate. No figure here is blended across contracts ' +
    'except the last row, which says so.';
}

function renderAnalysisTabs(el, contracts) {
  const host = el.querySelector('.an-tabs');
  host.innerHTML = '';
  const add = (key, label) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.className = (analysis.key === key) ? 'on' : '';
    b.onclick = () => { analysis.key = key; loadAnalysis(); };
    host.appendChild(b);
  };
  contracts.forEach((c) => add(c.key, c.name));
  add(null, 'All contracts');
}

async function loadAnalysis() {
  if (state.closed.has('__analysis__')) return;
  const el = analysisWindow();
  const contracts = (window.__lastSnapshot || {}).contracts || [];
  if (analysis.key === undefined) analysis.key = null;
  if (analysis.key === null && contracts.length && analysis.key !== null) { /* noop */ }
  if (analysis.key === null && !el.dataset.touched) {
    analysis.key = contracts.length ? contracts[0].key : null;
    el.dataset.touched = '1';
  }
  // Simulated fills are never BLENDED into a live figure without being
  // asked for — but a desk running the simulator opening on "Live only" is
  // shown an empty window for the session it just watched. So the default
  // follows what the engine actually is, until somebody chooses otherwise.
  if (!analysis.modeChosen) {
    const sim = (window.__lastSnapshot || {}).engine
      && window.__lastSnapshot.engine.simulated;
    analysis.mode = sim ? 'sim' : 'live';
  }
  renderAnalysisTabs(el, contracts);
  el.querySelector('.an-period').value = analysis.period;
  el.querySelector('.an-mode').value = analysis.mode;

  const query = '?period=' + analysis.period + '&mode=' + analysis.mode;
  const say = (text) => {
    // Never silently render nothing: a window that has failed to load must
    // look different from one with nothing to report.
    el.querySelector('.an-foot').textContent = text;
    el.querySelector('.an-note').textContent = text;
  };
  const desk = el.querySelector('.an-desk');
  const perContract = el.querySelectorAll('.an-tiles, .an-two, .an-journal');

  if (analysis.key === null) {
    const report = await getAnalysis('/api/analysis' + query);
    if (!report) { say('the analysis could not be read'); return; }
    desk.classList.remove('hidden');
    el.querySelectorAll('.an-tiles, .an-two').forEach((n) => n.classList.add('hidden'));
    el.querySelector('.an-journal').closest('.card').classList.add('hidden');
    renderDesk(el, report);
    el.querySelector('.an-note').textContent = 'all contracts · closed trades only';
    el.querySelector('.an-foot').textContent =
      report.rows.length + ' contracts · ' + report.total.trades + ' closed trades';
    el.querySelector('.an-csv').classList.add('hidden');
  } else {
    const report = await getAnalysis('/api/analysis/' +
      encodeURIComponent(analysis.key) + query);
    if (!report) {
      say('no analysis for ' + analysis.key +
          ' — it is on the screen but not in the configuration');
      return;
    }
    desk.classList.add('hidden');
    el.querySelectorAll('.an-tiles, .an-two').forEach((n) => n.classList.remove('hidden'));
    el.querySelector('.an-journal').closest('.card').classList.remove('hidden');
    renderTiles(el, report.summary);
    renderTouches(el, report.touches, report.entry_threshold);
    renderExits(el, report.exits);
    renderCosts(el, report.costs, report.key);
    renderJournal(el, report.journal, report.decimals);
    el.querySelector('.an-note').textContent = report.symbol +
      ' · closed trades only';
    el.querySelector('.an-journal-note').textContent =
      report.journal.length + ' closed, newest first';
    el.querySelector('.an-foot').textContent =
      report.summary.trades + ' closed · ' + report.open_positions +
      ' open (excluded) · ' + report.touches.unresolved + ' touches unresolved';
    // Zero under one filter and non-zero under another is a statement about
    // the filter, not about the desk. Say which.
    if (!report.summary.trades) elsewhereNote(el, query);
    const csv = el.querySelector('.an-csv');
    csv.classList.remove('hidden');
    csv.href = '/api/analysis/' + encodeURIComponent(analysis.key) +
      '/trades.csv' + query;
  }
  el.querySelector('.an-updated').textContent =
    'updated ' + new Date().toLocaleTimeString([], { hour12: false });
}

/* Nothing here, but something under another filter? Say so. An empty
 * Analysis window that reads as "you have not traded" when in fact the rows
 * are one dropdown away is the window quietly lying. */
async function elsewhereNote(el, query) {
  const modes = { live: 'Live only', sim: 'Simulated only', both: 'Live + simulated' };
  const others = Object.keys(modes).filter((m) => m !== analysis.mode);
  for (const other of others) {
    const alt = await getAnalysis('/api/analysis/' +
      encodeURIComponent(analysis.key) +
      query.replace('mode=' + analysis.mode, 'mode=' + other));
    if (alt && alt.summary && alt.summary.trades) {
      el.querySelector('.an-foot').textContent =
        'nothing under ' + modes[analysis.mode] + ' — ' + alt.summary.trades +
        ' closed trade(s) are under ' + modes[other];
      return;
    }
  }
}

async function getAnalysis(url) {
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) return null;
    return res.json();
  } catch (e) {
    return null;
  }
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
  // UAT and PROD are separate venues and the screen always says which — and
  // a configured venue that nothing is connected to is NOT that venue. A
  // badge reading a plain "UAT" while the simulator drives is a screen
  // describing a session that does not exist.
  badge.textContent = engine.simulated
    ? (engine.environment && engine.environment !== 'SIMULATED'
      ? engine.environment + ' · SIM' : 'SIMULATED')
    : (engine.environment || DASH);
  badge.title = engine.simulated
    ? 'these prices come from the simulator, not from a venue'
    : 'live venue session';
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

  // A setting that looks saved and is not in force is worse than one that
  // plainly says it needs the engine bounced. The engine names them.
  const restart = document.getElementById('restart-banner');
  const waiting = engine.config_restart_needed || [];
  restart.classList.toggle('hidden', waiting.length === 0);
  if (waiting.length) {
    restart.textContent = 'SAVED, NOT IN FORCE — ' + waiting.join(', ') +
      '. These change something the running engine already holds; restart it ' +
      'for them to take effect. Everything else you saved is live now.';
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

/* -- one contract's settings, behind the gear -----------------------------
 *
 * The full set of §4.1, because the comprehensiveness is the point: a desk
 * that cannot set a contract's own costs ends up trading eight instruments
 * on one instrument's assumptions.
 *
 * Two rules the whole panel turns on:
 *
 *   - **A blank box means "use the desk default"**, and the effective value
 *     is rendered beside it in grey so a blank is never mistaken for a zero.
 *     `0` is a real number and sets an override.
 *   - **A change that cannot be adopted while running SAYS SO.** The engine
 *     reports what it could not apply; a setting that looks saved and is not
 *     in force is worse than one that plainly needs a restart.
 */

const CFG_GROUPS = [
  { name: 'Signal', fields: [
    ['lookback', 'Lookback', 'samples in the rolling window', 'number', { step: 10, min: 2 }],
    ['stats_update_interval_sec', 'Recompute mean and σ every', 'seconds; 0 = every update. Stable bands are easier to aim at', 'number', { step: 10, min: 0 }],
    ['entry_threshold', 'Enter at |z|', '', 'number', { step: 0.1, min: 0 }],
    ['exit_signal_mode', 'Exit on', '', 'select', { options: [['profit', 'Profit target'], ['zscore', 'z-score'], ['hybrid', 'Whichever comes first']] }],
    ['exit_threshold', 'Exit at |z|', 'used by z-score and hybrid', 'number', { step: 0.1, min: 0 }],
    ['stop_loss_z', 'Stop at |z|', 'emergency exit', 'number', { step: 0.5, min: 0 }],
    ['max_hold_minutes', 'Time stop', 'minutes; 0 = no time stop', 'number', { step: 5, min: 0 }],
  ] },
  { name: 'Filters', fields: [
    ['edge_filter_enabled', 'Edge filter', '', 'check'],
    ['min_std_multiple', 'σ must be at least', '× the round-trip cost', 'number', { step: 0.1, min: 0 }],
    ['hurst_enabled', 'Hurst filter', 'off by default: on a coarsely quantised spread it reads high', 'check'],
    ['hurst_threshold', 'Hurst below', 'H under this is mean-reverting', 'number', { step: 0.05, min: 0 }],
    ['half_life_enabled', 'Half-life filter', '', 'check'],
    ['max_half_life', 'Half-life at most', 'samples; slower reversion than the intended hold is refused', 'number', { step: 1, min: 0 }],
    ['min_book_size', 'Book size at least', 'contracts on the touch, both sides', 'number', { step: 1, min: 0 }],
    ['max_book_spread_ticks', 'Book no wider than', 'ticks', 'number', { step: 1, min: 0 }],
  ] },
  { name: 'Size & risk', fields: [
    ['quantity', 'Quantity per entry', 'contracts', 'number', { step: 1, min: 0 }],
    ['max_position', 'Maximum position', 'contracts; the hard ceiling', 'number', { step: 1, min: 0 }],
    ['max_trades_per_day', 'Trades per day', '0 = no limit', 'number', { step: 1, min: 0 }],
    ['daily_max_loss', 'Daily loss limit', 'money; hitting it turns THIS contract off and says so', 'number', { step: 50, min: 0 }],
    ['entry_cooldown_seconds', 'Cooldown after an entry', 'seconds', 'number', { step: 5, min: 0 }],
  ] },
  { name: 'Execution', fields: [
    ['entry_order_type', 'Entry', '', 'select', { options: [['LIMIT', 'Limit'], ['MARKET', 'Market']] }],
    ['exit_order_type', 'Exit', '', 'select', { options: [['MARKET', 'Market'], ['LIMIT', 'Limit']] }],
    ['entry_limit_offset_ticks', 'Entry priced behind its touch', 'ticks', 'number', { step: 1, min: 0 }],
    ['exit_limit_offset_ticks', 'Exit priced behind its touch', 'ticks &mdash; patience going in and coming out are different decisions', 'number', { step: 1, min: 0 }],
    ['entry_limit_timeout_sec', 'Entry unfilled after', 'seconds', 'number', { step: 1, min: 0 }],
    ['exit_limit_timeout_sec', 'Exit unfilled after', 'seconds', 'number', { step: 1, min: 0 }],
    ['entry_on_timeout', 'then the entry', '', 'select', { options: [['CANCEL', 'Cancel'], ['CROSS_AT_MARKET', 'Cross at market']] }],
    ['exit_on_timeout', 'then the exit', 'a missed entry is a trade not taken; a missed exit is a position you still hold', 'select', { options: [['CROSS_AT_MARKET', 'Cross at market'], ['CANCEL', 'Cancel']] }],
    ['repeg_dead_band_ticks', 'Re-peg dead band', 'ticks the touch must move before an amend &mdash; every amend costs queue position', 'number', { step: 1, min: 0 }],
    ['time_in_force', 'Time in force', '', 'select', { options: [['DAY', 'Day'], ['IOC', 'IOC'], ['GTC', 'GTC']] }],
    ['close_offset_mode', 'Closing flag', 'what this venue wants on a close. AUTO reads the venue; an unknown flag degrades to CLOSE, never to OPEN', 'select', { options: [['AUTO', 'Auto'], ['CLOSE', 'Close'], ['CLOSE_TODAY_FIRST', 'Close today first'], ['NONE', 'None (netting venue)']] }],
  ] },
  { name: 'Costs', fields: [
    ['commission_per_contract', 'Commission', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['exchange_fee_per_contract', 'Exchange fee', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['clearing_fee_per_contract', 'Clearing fee', 'per contract, per side', 'number', { step: 0.1, min: 0 }],
    ['slippage_budget_ticks', 'Slippage budget', 'ticks per side &mdash; a BUDGET. Analysis shows the measured figure beside it', 'number', { step: 0.1, min: 0 }],
    ['profit_target_pct', 'Profit target', '% &mdash; taken AFTER the round trip above', 'number', { step: 0.5, min: 0 }],
    ['profit_target_basis', 'as a % of', 'margin is the default. A basis with no number behind it renders the target as —, never as 0.00', 'select', { options: [['MARGIN', 'Initial margin'], ['NOTIONAL', 'Notional'], ['ENTRY_SIGMA', 'Entry sigma']] }],
  ] },
  { name: 'Display', fields: [
    ['name', 'Window title', '', 'text', { plain: true }],
    ['decimals', 'Decimals', 'how prices are printed on this window', 'number', { step: 1, min: 0, plain: true }],
    ['enabled', 'Trade this contract', 'switching it off needs a restart: its window, book and any position have nowhere to go mid-flight', 'check', { plain: true }],
  ] },
];

/* Which fields are NOT per-contract overrides but plain contract fields. A
 * blank one of these is not "use the desk default" — there is no desk
 * default for a window title. */
const CFG_PLAIN = new Set(['name', 'decimals', 'enabled']);

const cfgState = {};    // key -> { group, contract }

function configWindow(key) {
  let el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (el) { raise(el); return el; }
  const tpl = document.getElementById('config-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = '__config__' + key;
  el.dataset.contract = key;
  cfgState[key] = cfgState[key] || { group: CFG_GROUPS[0].name, contract: null };

  el.querySelector('.close').onclick = () => { el.remove(); renderTabs(); };
  el.querySelector('.cfg-revert').onclick = () => loadConfig(key);
  el.querySelector('.cfg-save').onclick = () => saveConfig(key);

  const tabs = el.querySelector('.cfg-tabs');
  CFG_GROUPS.forEach((g) => {
    const b = document.createElement('button');
    b.textContent = g.name;
    b.className = g.name === cfgState[key].group ? 'on' : '';
    b.onclick = () => {
      cfgState[key].group = g.name;
      tabs.querySelectorAll('button').forEach((x) =>
        x.classList.toggle('on', x.textContent === g.name));
      paintConfig(key);
    };
    tabs.appendChild(b);
  });

  el.onmousedown = () => raise(el);
  makeDraggable(el, '__config__' + key);
  const desk = document.getElementById('desktop');
  desk.appendChild(el);

  // A settings panel is a panel over the desk, not another tile in it:
  // opening one must not re-flow the windows the trader is reading. It
  // floats beside its own contract, out of the grid, and stays where it is
  // dragged like everything else.
  const place = state.places['__config__' + key];
  if (place) {
    placeWindow(el, place.x, place.y);
  } else {
    el.classList.add('floating');
    const owner = document.querySelector('.win[data-key="' + key + '"]');
    const box = desk.getBoundingClientRect();
    const from = owner ? owner.getBoundingClientRect() : box;
    const x = Math.max(8, Math.min(from.left - box.left + 24,
                                   desk.clientWidth - 470));
    el.style.left = x + 'px';
    el.style.top = (from.top - box.top + desk.scrollTop + 18) + 'px';
  }
  raise(el);
  loadConfig(key);
  return el;
}

async function loadConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (!el) return;
  try {
    const rows = await (await fetch('/api/contracts', { cache: 'no-store' })).json();
    const row = (rows || []).find((r) => r.key === key);
    if (!row) {
      el.querySelector('.cfg-note').textContent =
        'this contract is not in the configuration file';
      return;
    }
    cfgState[key].contract = row;
    paintConfig(key);
    el.querySelector('.cfg-note').textContent = '';
  } catch (e) {
    el.querySelector('.cfg-note').textContent = 'could not read the settings';
  }
}

function cfgField(field, label, hint, kind, opts, row) {
  const o = opts || {};
  const plain = CFG_PLAIN.has(field);
  // The saved OVERRIDE — blank means "use the desk default". Never the
  // effective value: filling the box with the default would turn every
  // fallback into an override the moment somebody pressed Save.
  const own = plain ? row[field] : (row[field] === undefined ? null : row[field]);
  const eff = row.effective ? row.effective[field] : undefined;
  const id = 'cf-' + field;
  let control;
  if (kind === 'check') {
    control = '<input type="checkbox" class="chk" id="' + id +
      '" data-field="' + field + '"' + (own === true ? ' checked' : '') + '>';
  } else if (kind === 'select') {
    control = '<select id="' + id + '" data-field="' + field + '">' +
      (plain ? '' : '<option value="">&mdash; desk default &mdash;</option>') +
      (o.options || []).map((op) =>
        '<option value="' + op[0] + '"' +
        (String(own) === op[0] ? ' selected' : '') + '>' + op[1] + '</option>').join('') +
      '</select>';
  } else {
    control = '<input type="' + (kind === 'text' ? 'text' : 'number') + '" id="' + id +
      '" data-field="' + field + '"' +
      (o.step !== undefined ? ' step="' + o.step + '"' : '') +
      (o.min !== undefined ? ' min="' + o.min + '"' : '') +
      ' value="' + (own === null || own === undefined ? '' : own) + '">';
  }
  // Unmeasured is not zero, and neither is blank: the grey figure says what
  // this contract will actually trade on.
  const effText = (plain || kind === 'check') ? '' :
    (eff === null || eff === undefined ? DASH : String(eff));
  const isDefault = !plain && (own === null || own === undefined);
  return '<label class="f cf-row"><span>' + label +
    (hint ? ' <small>' + hint + '</small>' : '') + '</span>' +
    '<span class="cf-c">' + control +
    (effText ? '<span class="cf-eff' + (isDefault ? '' : ' own') + '" title="' +
      (isDefault ? 'the desk default' : 'this contract&#39;s own setting') +
      '">' + effText + '</span>' : '') + '</span></label>';
}

function paintConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  const row = cfgState[key] && cfgState[key].contract;
  if (!el || !row) return;
  el.querySelector('.title').textContent = row.name || key;
  el.querySelector('.cfg-key').textContent = row.symbol || key;
  const group = CFG_GROUPS.find((g) => g.name === cfgState[key].group)
    || CFG_GROUPS[0];
  el.querySelector('.cfg-body').innerHTML = '<div class="cb">' +
    group.fields.map((f) => cfgField(f[0], f[1], f[2], f[3], f[4], row)).join('') +
    '</div>';
}

async function saveConfig(key) {
  const el = document.querySelector('.win[data-key="__config__' + key + '"]');
  if (!el) return;
  const body = {};
  el.querySelectorAll('[data-field]').forEach((input) => {
    const field = input.dataset.field;
    if (input.type === 'checkbox') body[field] = input.checked;
    else if (input.type === 'number') {
      // '' clears the override back to the desk default. 0 is a number.
      body[field] = input.value === '' ? '' : Number(input.value);
    } else body[field] = input.value;
  });
  const res = await fetch('/api/contracts/' + encodeURIComponent(key), {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const out = await res.json();
  if (!out.ok) {
    el.querySelector('.cfg-note').textContent = out.error || 'not saved';
    toast('REJECT', 'NOT SAVED', out.error || 'the settings were not saved', key);
    return;
  }
  cfgState[key].contract = Object.assign({}, cfgState[key].contract, body,
    { effective: out.effective });
  paintConfig(key);
  toast('ORDER', 'SAVED', 'the engine picks this up on its next pass', key);
  el.querySelector('.cfg-note').textContent = 'saved';
}

async function tick() {
  try {
    const res = await fetch('/api/snapshot', { cache: 'no-store' });
    const snap = await res.json();
    renderChrome(snap);
    window.__lastSnapshot = snap;
    const seen = new Set(['__positions__', '__analysis__']);
    (snap.contracts || []).forEach((c) => {
      seen.add(c.key);
      // A settings panel belongs to its contract and outlives a poll. The
      // sweep below removes windows for contracts that are gone; it must not
      // remove the panel of one that is still here.
      seen.add('__config__' + c.key);
      renderContract(c);
    });
    renderPositions(snap);
    if (!analysis.timer) {
      // History, not a market: a slow timer, off the desk's critical path.
      loadAnalysis();
      analysis.timer = setInterval(loadAnalysis, 15000);
    }
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
    b.textContent = key === '__positions__' ? 'Positions'
      : key === '__analysis__' ? 'Analysis' : key;
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
