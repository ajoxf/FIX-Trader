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
  //: Windows minimised to the taskbar. Kept in this browser.
  minimised: new Set(JSON.parse(localStorage.getItem('ft.min') || '[]')),
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

/* The engine takes on a new contract when it starts. It stops the way it
 * always stops — this system's own working orders cancelled, the book
 * already on disk — and the launcher starts it again. */
async function restartEngine() {
  const ok = await ask('Restart the engine',
    'The engine stops — cancelling any working orders this system sent — ' +
    'and the launcher starts it again with the saved changes. Open ' +
    'positions are kept and recovered. Prices pause and the TT sessions ' +
    'log on again: allow about 15 seconds.', 'Restart engine');
  if (!ok) return;
  const answer = await command('restart_engine');
  if (answer && answer.ok) {
    toast('OK', 'RESTARTING', 'the engine is restarting — the desk comes ' +
      'back within about 15 seconds');
  }
}

/* -- one window ----------------------------------------------------------- */

/* What every desk window does with its own chrome: close, minimise,
 * click-to-raise, drag and resize. */
function wireWindow(el, key) {
  el.querySelector('.close').onclick = () => {
    state.closed.add(key);
    localStorage.setItem('ft.closed', JSON.stringify([...state.closed]));
    el.remove();
    renderTabs();
  };
  const min = el.querySelector('.min');
  if (min) min.onclick = () => setMinimised(el, true);
  if (state.minimised.has(key)) el.classList.add('minimised');
  el.onmousedown = () => { state.focused = key; raise(el); };
  makeDraggable(el, key);
}

/* Minimised: off the desk, its button still on the taskbar. */
function setMinimised(el, on) {
  const key = el.dataset.key;
  el.classList.toggle('minimised', on);
  if (on) state.minimised.add(key); else state.minimised.delete(key);
  try { localStorage.setItem('ft.min', JSON.stringify([...state.minimised])); }
  catch (e) { /* a private window forgets; the desk still works */ }
  if (!on) { state.focused = key; raise(el); }
  renderTabs();
}

function closeNowAsk(key, name) {
  return ask('Close ' + name + '?',
    'The position is closed at market, now — this crosses the spread. ' +
    'The algo on this contract is stood down with it, so it does not ' +
    're-enter on the next pass; switch it back on when you want it.',
    'CLOSE NOW').then(async (ok) => {
    if (ok) { await command('close_now', key); toast('ORDER', 'CLOSING', key); }
  });
}

function windowFor(key) {
  let el = document.querySelector('.win[data-key="' + key + '"]');
  if (el) return el;
  const tpl = document.getElementById('contract-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = key;
  wireWindow(el, key);
  el.querySelector('.sw').onclick = async () => {
    const on = el.querySelector('.sw').classList.contains('on');
    await command(on ? 'algo_off' : 'algo_on', key);
  };
  el.querySelector('.close-now').onclick = () => closeNowAsk(key,
    el.querySelector('.title').textContent);
  el.querySelector('.cog').onclick = () => configWindow(key);
  el.querySelector('.chart').onclick = () => chartWindow(key);
  el.querySelector('.aw-bt-run').onclick = () => runBacktest(el, key);
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
      state.places[other.dataset.key] = Object.assign({},
        state.places[other.dataset.key],
        { x: parseFloat(other.style.left), y: parseFloat(other.style.top) });
    });
    const move = (m) => {
      const x = m.clientX - desk.left - dx;
      const y = m.clientY - desk.top - dy + document.getElementById('desktop').scrollTop;
      placeWindow(el, Math.max(0, x), Math.max(0, y));
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      state.places[key] = Object.assign({}, state.places[key],
        { x: parseFloat(el.style.left), y: parseFloat(el.style.top) });
      savePlaces();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
    down.preventDefault();
  });
  makeResizable(el, key);
}

function savePlaces() {
  try { localStorage.setItem('ft.places', JSON.stringify(state.places)); }
  catch (e) { /* a private window: the desk still works, it just forgets */ }
}

/* The corner handle is the browser's own (`resize: both`). What is kept is
 * the size the TRADER set: a resize writes an inline width and height, which
 * a window growing with its own content never does — so a window whose
 * table got longer is not remembered at that height. */
function makeResizable(el, key) {
  const saved = state.places[key];
  if (saved && saved.w && saved.h) {
    el.style.width = saved.w + 'px';
    el.style.height = saved.h + 'px';
  }
  if (!window.ResizeObserver) return;
  let timer = null;
  new ResizeObserver(() => {
    if (!el.style.width && !el.style.height) return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.places[key] = Object.assign({}, state.places[key],
        { w: el.offsetWidth, h: el.offsetHeight });
      savePlaces();
    }, 250);
  }).observe(el);
}

/* Minutes as a short figure: 84 not 84.0, 0.5 as 0.5. */
function minutes(v) {
  if (v === null || v === undefined || isNaN(v)) return DASH;
  return v >= 10 ? Math.round(v).toString() : (Math.round(v * 10) / 10).toString();
}

function renderContract(c) {
  if (state.closed.has(c.key)) return;
  const el = windowFor(c.key);
  const q = (sel) => el.querySelector(sel);

  q('.title').textContent = c.name + ' · Algo';
  q('.state').textContent = c.state;
  q('.state').className = 'state s-' + c.state;
  q('.state').title = c.halted_by ? 'Halted: ' + c.halted_by
    : c.market_note ? 'Market: ' + c.market_note : '';
  el.classList.toggle('stale', !!(c.feed && c.feed.stale));

  const sw = q('.sw');
  sw.classList.toggle('on', !!c.algo_on);
  sw.querySelector('span').textContent = c.algo_on ? 'ON' : 'OFF';

  // The one-way badge: a contract that is armed and passes over half its
  // signals otherwise looks broken.
  const dir = (c.filters || {}).trade_direction || 'BOTH';
  const badge = q('.dirbadge');
  badge.classList.toggle('hidden', dir === 'BOTH');
  badge.textContent = dir === 'SELL_ONLY' ? 'H to L ONLY'
    : dir === 'BUY_ONLY' ? 'L to H ONLY' : '';

  renderAlgoBody(el, c);

  // footer
  const foot = q('.wfoot');
  const age = c.feed && c.feed.age_sec;
  q('.age').textContent = age === null || age === undefined ? DASH : age + 's';
  q('.last-event').textContent = c.last_event || '';
  foot.className = 'wfoot' + (c.feed && c.feed.stale ? ' bad'
    : c.state === 'WARMING' ? ' warnf' : '');

  markPosition(el, c);

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

/* -- the Algo window --------------------------------------------------------
 *
 * SIGNAL & POSITION, STATISTICS, FILTERS — the MT5 desk's Algo window, on
 * ONE contract. The venue lists the spread itself, so "H to L" is selling
 * the contract into its BID and "L to H" is buying its OFFER; there are no
 * legs. Every figure comes from the engine's Algo block.
 */

function esc(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* Money with cents: a round trip of $4.15 is not "$4". */
function cash(value) {
  if (value === null || value === undefined || isNaN(value)) return DASH;
  const sign = value < 0 ? '-' : '';
  return sign + '$' + Math.abs(value).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function zText(value) {
  if (value === null || value === undefined || isNaN(value)) return DASH;
  return (value > 0 ? '+' : '') + Number(value).toFixed(2);
}

function kv(label, value, cls, title, wide) {
  return '<div class="aw-kv' + (wide ? ' wide' : '') + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '><span>' + label +
    '</span><b' + (cls ? ' class="' + cls + '"' : '') + '>' + value +
    '</b></div>';
}

function awBadge(text, tone, title) {
  return '<span class="aw-badge ' + tone + '"' +
    (title ? ' title="' + esc(title) + '"' : '') + '>' + text + '</span>';
}

function sideWords(side) {
  return side === 'SELL' ? 'H to L' : side === 'BUY' ? 'L to H' : (side || DASH);
}

const ALGO_EXIT_WORDS = {
  STOP_LOSS: 'stop loss', PROFIT_TARGET: 'profit target',
  Z_STOP: 'z-stop', MEAN_REVERSION: 'back to the mean', TIME_STOP: 'time stop',
};

function levelWords(level, worth, d) {
  /* "13.7000 (+$4.00)": the price to compare with the closing side, and
   * what closing there is worth to the whole position, net. */
  return num(level, d) + (worth === null || worth === undefined ? ''
    : ' (' + (worth > 0 ? '+' : '') + cash(worth) + ')');
}

function positionWords(p, d) {
  return (p.side || '?') + ' @ ' + num(p.entry, d);
}

function algoLine(block, d) {
  /* One short line: what the Algo says right now. */
  const st = block.state;
  if (st === 'SIGNAL') {
    const z = block.signal === 'SELL' ? block.z_sell : block.z_buy;
    return sideWords(block.signal) + ' signal  z ' + zText(z);
  }
  if (st === 'EXIT') {
    return (block.positions || []).filter((p) => p.exit).map((p) =>
      'EXIT ' + positionWords(p, d) + ': ' + (ALGO_EXIT_WORDS[p.exit] || p.exit))
      .join('; ');
  }
  if (st === 'IN_POSITION') {
    const first = (block.positions || [])[0] || {};
    return 'in ' + positionWords(first, d) + ' — TP ' +
      levelWords(first.tp, first.tp_money, d) + ' · SL ' +
      (first.sl === null || first.sl === undefined
        ? ((block.params || {}).stop_loss_on ? DASH : 'off')
        : levelWords(first.sl, first.sl_money, d));
  }
  if (st === 'BLOCKED') return 'held: ' + (block.blocked || '');
  if (st === 'CONFIRMING') {
    const streak = block.streak || {};
    return 'confirming ' + Math.max(streak.BUY || 0, streak.SELL || 0) + '/' +
      ((block.params || {}).confirm_ticks || '?');
  }
  if (st === 'STARTING') return 'starting…';
  return 'watching';
}

function progressHtml(first, d) {
  /* SL <- entry -> TP, with the closing price on it; each half on its own
   * scale, as the engine measures them. */
  const entry = first.entry;
  const toTp = first.tp === null || first.tp === undefined ? null
    : Math.abs(first.tp - entry);
  const toSl = first.sl === null || first.sl === undefined ? null
    : Math.abs(entry - first.sl);
  const split = (toTp && toSl) ? toSl / (toSl + toTp) : (toSl ? 0.5 : 0);
  const p = first.progress;
  const at = p === null || p === undefined ? null
    : (p >= 0 ? split + p * (1 - split) : split + p * split);
  const fill = at === null ? '' :
    '<div class="ap-fill ' + (p >= 0 ? 'up' : 'down') + '" style="left:' +
    (Math.min(at, split) * 100) + '%;width:' + (Math.abs(at - split) * 100) +
    '%"></div><div class="ap-mark" style="left:calc(' + (at * 100) +
    '% - 1px)"></div>';
  return '<div class="algo-progress" title="' + esc(positionWords(first, d) +
    ', closing at ' + num(first.closing, d)) + '"><div class="ap-pct">' +
    (p === null || p === undefined ? DASH : (p >= 0
      ? Math.round(p * 100) + '% to TP' : Math.round(-p * 100) + '% to SL')) +
    '</div><div class="ap-track">' + fill + '<div class="ap-entry" style="left:' +
    (split * 100) + '%"></div></div><div class="ap-ends"><span>' +
    (toSl === null ? 'no SL' : 'SL ' + levelWords(first.sl, first.sl_money, d)) +
    '</span><span>' + (toTp === null ? 'no TP'
      : 'TP ' + levelWords(first.tp, first.tp_money, d)) + '</span></div></div>';
}

function warmupHtml(warmup) {
  /* Live time watched since the Algo was armed, against what it needs. A
   * band rebuilt from the recording in a second is not a feed watched. */
  if (!warmup || !warmup.need_sec) return '';
  const done = !!warmup.done;
  const pct = done ? 100 : Math.floor(100 * warmup.sec / warmup.need_sec);
  return '<div class="aw-data aw-warmup" title="No entry until the Algo has ' +
    'watched this long of live prices since it was armed. Time with no price ' +
    'does not count."><span>Live</span><div class="aw-bar"><div class="' +
    (done ? 'ok' : 'wait') + '" style="width:' + pct + '%"></div></div><span>' +
    (done ? 'warmed up' : 'warming up ' + Math.floor(warmup.sec / 60) + '/' +
      Math.round(warmup.need_sec / 60) + ' min') + '</span></div>';
}

function trendBadge(trend) {
  /* Which way the band's middle moved over the lookback: up holds H to L,
   * down holds L to H, flat holds neither. */
  if (!trend.on) return awBadge('OFF', 'off');
  const drift = trend.drift_sigma;
  const why = drift === null || drift === undefined
    ? 'not enough candles to measure the trend yet'
    : 'the middle moved ' + (drift >= 0 ? '+' : '') + drift.toFixed(2) +
      'σ in ' + trend.lookback_min + ' min (limit ' + trend.limit + 'σ)';
  if (trend.state === 'UP') return awBadge('↑', 'bad', why + ' — no H to L');
  if (trend.state === 'DOWN') return awBadge('↓', 'bad', why + ' — no L to H');
  if (trend.state === 'FLAT') return awBadge('–', 'ok', why);
  return awBadge('WAIT', 'wait', why);
}

function lastOrderHtml(block) {
  /* What the Algo last DID, and what became of it — a refusal in its own
   * words, never a blank. */
  const last = (block.recent || [])[0];
  let html = '<div class="aw-blocked aw-last-order"><div class="aw-head2">' +
    'Last order</div>';
  if (!last) return html + '<div class="hint">none yet</div></div>';
  const what = esc(last.action || '') + ' ' + esc(sideWords(last.side)) +
    (last.z === null || last.z === undefined ? '' : ' z ' + zText(last.z)) +
    ' · ' + new Date(last.at * 1000).toLocaleTimeString();
  let outcome;
  if (last.mode === 'DRY RUN') {
    outcome = '<div class="hint">dry run — nothing sent</div>';
  } else if (last.done) {
    outcome = '<div class="up">' + (last.mode === 'PAPER'
      ? 'filled on paper — nothing sent' : 'sent — done') + '</div>';
  } else {
    outcome = '<div class="down">REFUSED — ' +
      esc(last.result || 'no reason given') + '</div>';
  }
  return html + '<div><b>' + what + '</b></div>' + outcome + '</div>';
}

function renderAlgoBody(el, c) {
  const d = c.decimals === undefined ? 4 : c.decimals;
  const block = c.algo || {};
  const params = block.params || {};
  const filters = block.filters || {};
  const market = c.market || {};
  const entryZ = params.entry_z;
  const direction = params.direction || 'BOTH';

  const mode = el.querySelector('.aw-mode');
  mode.textContent = block.mode || DASH;
  mode.className = 'aw-mode ' + (block.mode === 'LIVE' ? 'live'
    : block.mode === 'PAPER' ? 'paper' : 'dry');
  mode.title = block.mode === 'LIVE'
    ? 'the Algo SENDS its orders to the venue'
    : block.mode === 'PAPER'
      ? 'the Algo fills on paper at the live bid/offer — nothing is sent'
      : 'signals only — nothing is sent or filled (Auto trade is off)';

  // -- SIGNAL & POSITION ----------------------------------------------------
  function entryLine(sell) {
    if (!params.reentry_on) {
      return '<div class="aw-tile-entry">' + (sell ? 'short' : 'long') +
        ' at ' + (sell ? '≥ +' : '≤ −') + (entryZ || '?') + ' (' +
        num(sell ? block.upper : block.lower, d) + ')</div>';
    }
    const inZ = Math.max(0, (entryZ || 0) - (params.reentry_back || 0));
    const windowPct = params.reentry_window_pct === undefined ||
      params.reentry_window_pct === null ? 50 : params.reentry_window_pct;
    const outZ = inZ * (1 - windowPct / 100);
    const priced = !(block.mean === null || block.mean === undefined ||
                     !block.sigma);
    const level = priced ? block.mean + (sell ? 1 : -1) * inZ * block.sigma : null;
    const outLevel = priced ? block.mean + (sell ? 1 : -1) * outZ * block.sigma : null;
    const armed = (block.armed || {})[sell ? 'SELL' : 'BUY'];
    return '<div class="aw-tile-entry" title="Armed when the stretch reaches ' +
      (sell ? '+' : '−') + entryZ + '; entered on the way back in, between ' +
      (sell ? '+' : '−') + inZ.toFixed(2) + ' and ' + (sell ? '+' : '−') +
      outZ.toFixed(2) + '. Past ' + outZ.toFixed(2) + ' it disarms — too close ' +
      'to the mean to trade.">' +
      (armed ? '<b class="aw-armed">ARMED</b> ' : 'arm ' +
        (sell ? '≥ +' : '≤ −') + entryZ + ' · ') +
      (sell ? 'short' : 'long') + ' back at ' + (sell ? '+' : '−') +
      inZ.toFixed(2) + ' (' + num(level, d) + ') to ' + (sell ? '+' : '−') +
      outZ.toFixed(2) + ' (' + num(outLevel, d) + ')</div>';
  }
  function tile(sell) {
    const z = sell ? block.z_sell : block.z_buy;
    const hit = z !== null && z !== undefined && entryZ &&
      (sell ? z >= entryZ : z <= -entryZ);
    const off = (sell && direction === 'L_TO_H') || (!sell && direction === 'H_TO_L');
    return '<div class="aw-tile ' + (sell ? 'sell' : 'buy') + (hit ? ' hit' : '') +
      (off ? ' off' : '') + '" title="' + (sell
        ? 'What SELLING the contract gets now (its bid). The Algo sells when '
          + 'this z reaches +' + entryZ + ', and closes a long here.'
        : 'What BUYING the contract costs now (its offer). The Algo buys when '
          + 'this z reaches −' + entryZ + ', and closes a short here.') +
      '"><div class="aw-tile-head">' + (sell ? 'H to L' : 'L to H') +
      (off ? ' <small>(entries off)</small>' : '') +
      '</div><div class="aw-tile-price">' + num(sell ? market.bid : market.ask, d) +
      '</div><div class="aw-tile-z">' + zText(z) + '</div>' + entryLine(sell) +
      '</div>';
  }
  const first = (block.positions || [])[0] || null;
  const position = first ? (first.side === 'BUY' ? 'LONG' : 'SHORT') + ' ' +
    num(first.quantity, 0) : 'FLAT';
  let html = '<div class="aw-head">Signal &amp; Position</div>' +
    '<div class="aw-tiles">' + tile(true) + tile(false) +
    '<div class="aw-pos ' + (first ? (first.side === 'BUY' ? 'long' : 'short')
      : 'flat') + '">' + position + '</div></div>' +
    '<div class="aw-line" title="' + esc(block.blocked || '') + '">' +
    esc(algoLine(block, d)) + '</div>';
  if (first) {
    const delta = first.closing === null || first.closing === undefined ||
      first.entry === null ? null : first.closing - first.entry;
    const good = delta === null ? ''
      : ((first.side === 'BUY' ? delta >= 0 : delta <= 0) ? 'up' : 'down');
    html += '<div class="aw-grid">' +
      kv('Entry', num(first.entry, d)) +
      kv('Δ price', delta === null ? DASH : (delta > 0 ? '+' : '') + num(delta, d),
        good, 'closing price now − entry: a LONG makes money when this is ' +
        'positive, a SHORT when it is negative') +
      kv('Entry z', zText(first.entry_z)) +
      kv('Net P&amp;L', cash(first.net_pnl), first.net_pnl === null ||
        first.net_pnl === undefined ? '' : (first.net_pnl >= 0 ? 'up' : 'down'),
        'after the whole round trip, at the side it would close on') +
      kv('Age', first.age_sec === null || first.age_sec === undefined ? DASH
        : Math.floor(first.age_sec / 60) + 'm ' + Math.floor(first.age_sec % 60) + 's') +
      kv('Size', num(first.quantity, 0) + ' contract(s)' +
        (first.paper ? ' <small>paper</small>' : '')) +
      '</div>' +
      kv('Levels', 'BE ' + num(first.break_even, d) + ' · TP ' +
        (first.tp === null || first.tp === undefined ? DASH
          : levelWords(first.tp, first.tp_money, d)) + ' · SL ' +
        (first.sl === null || first.sl === undefined
          ? (params.stop_loss_on ? DASH : 'off')
          : levelWords(first.sl, first.sl_money, d)) +
        ((params.stop_mode === 'ATR' || params.target_mode === 'ATR')
          ? ' <small>(ATR ' + num(first.entry_atr, d) + ' at entry)</small>' : ''),
        '', 'compare with the CLOSING price: the bid for a LONG, the offer ' +
        'for a SHORT. In brackets: the net P&L of closing there', true) +
      (params.progress_bar === false ? '' : progressHtml(first, d));
  }
  el.querySelector('.aw-signal').innerHTML = html;
  el.querySelector('.aw-closebar').classList.toggle('hidden', !c.position);
  el.querySelector('.close-now').disabled = !c.position;

  // -- STATISTICS -------------------------------------------------------------
  const history = block.history || {};
  const regime = filters.regime || {};
  const count = block.count || 0;
  const needed = block.needed || params.length || 0;
  const pctDone = needed ? Math.min(100, Math.round(count * 100 / needed)) : 0;
  const regimeText = regime.state === 'TRENDING'
    ? 'Trend ' + ((regime.slope || 0) > 0 ? '↑' : '↓')
    : regime.state === 'RANGE' ? 'Mean-rev'
      : regime.state === 'COLLECTING' ? 'Collect' : DASH;
  el.querySelector('.aw-stats').innerHTML =
    '<div class="aw-head">Statistics</div><div class="aw-grid">' +
    kv('Mean (EMA)', num(block.mean, d)) +
    kv('Std dev', num(block.sigma, d)) +
    kv('ATR(' + (block.atr_period || params.atr_period || 14) + ')',
      num(block.atr, d), '', 'the average size of one candle’s move, close ' +
      'to close (Wilder). In ATR mode the stop and target are multiples of ' +
      'it, frozen when the trade opens') +
    kv('Half-life', filters.half_life_minutes === null ||
      filters.half_life_minutes === undefined ? DASH
      : Math.round(filters.half_life_minutes) + ' min', '',
      'how long a stretch takes to halve, from the candles (AR(1))') +
    kv('Regime', regimeText, regime.state === 'TRENDING' ? 'down'
      : (regime.state === 'RANGE' ? 'up' : ''),
      regime.efficiency_ratio === null || regime.efficiency_ratio === undefined
        ? '' : 'efficiency ' + regime.efficiency_ratio.toFixed(2) + ', ' +
          regime.crossings + ' mean crossings') +
    kv('Candles', (block.timeframe_min || params.timeframe_min) + 'm × ' + needed) +
    kv('Band', num(block.lower, d) + ' … ' + num(block.upper, d), '', '', true) +
    '</div><div class="aw-data" title="' + esc(history.note || '') + '">' +
    '<span>Data</span><div class="aw-bar"><div class="' +
    (block.ready ? 'ok' : 'wait') + '" style="width:' +
    (block.ready ? 100 : pctDone) + '%"></div></div><span>' +
    (block.ready ? 'candles ready' : count + '/' + needed + ' candles') +
    '</span></div>' + warmupHtml(block.warmup);

  // -- FILTERS ---------------------------------------------------------------
  const edge = filters.edge || {};
  const cost = filters.cost || {};
  const band = filters.half_life_band || [0, 0];
  function onOff(on, ok, yes, no, why) {
    if (!on) return awBadge('OFF', 'off', why);
    if (ok === null || ok === undefined) return awBadge('—', 'wait', why);
    return awBadge(ok ? yes : no, ok ? 'ok' : 'bad', why);
  }
  const trending = regime.state === 'TRENDING';
  const day = block.day || {};
  const last = block.last_blocked;
  el.querySelector('.aw-filters').innerHTML =
    '<div class="aw-head">Filters</div><div class="aw-badges">' +
    '<div>' + onOff(edge.on, edge.ok, '✓', '✗',
      'expected capture against the round-trip cost, at the entry z') +
    '<small>Edge</small></div>' +
    '<div>' + (regime.on ? awBadge(trending ? 'TR' : (regime.state === 'RANGE'
      ? 'MR' : 'WAIT'), trending ? 'bad' : (regime.state === 'RANGE' ? 'ok'
      : 'wait')) : awBadge('OFF', 'off')) + '<small>Regime</small></div>' +
    '<div>' + trendBadge(filters.trend || {}) + '<small>Trend</small></div>' +
    '<div>' + awBadge(filters.ready ? 'YES' : 'NO', filters.ready ? 'ok' : 'wait')
    + '<small>Ready</small></div>' +
    ((band[0] || band[1]) ? '<div>' + awBadge(Math.round(band[0] || 0) + '–' +
      (band[1] ? Math.round(band[1]) : '∞'), 'off') + '<small>HL min</small></div>'
      : '') +
    '</div><div class="aw-grid">' +
    kv('Capture / cost', (edge.ratio === null || edge.ratio === undefined
      ? DASH : edge.ratio.toFixed(2) + '×') + ' / req ' +
      (edge.required === undefined ? DASH : edge.required + '×'),
      edge.ok === true ? 'up' : (edge.ok === false ? 'down' : ''),
      'expected capture ' + cash(edge.capture) + ' (' +
      (params.edge_capture_frac || 0.5) + ' × entry z × σ) against the round ' +
      'trip, at the entry z', true) +
    kv('Round trip', cash(cost.total), '', 'crossing ' + cash(cost.crossing) +
      ' + fees ' + cash(cost.commission) + ' + slippage ' + cash(cost.slippage)) +
    kv('Today', (day.trades || 0) + (params.max_trades_day
      ? '/' + params.max_trades_day : '') + ' trades · ' + (day.losses_row || 0) +
      ' in a row · ' + cash(day.pnl), '', 'the day’s limits stop entries, ' +
      'never exits', true) +
    kv('Size', num(filters.qty || params.algo_qty || 1, 0) + ' contract(s)') +
    '</div><div class="aw-blocked"><div class="aw-head2">Last signal blocked</div>' +
    (last ? '<div><b>' + esc(sideWords(last.side)) + '</b> z ' + zText(last.z) +
      ' · ' + new Date(last.at * 1000).toLocaleTimeString() + '</div><div>' +
      esc(last.reason || '') + '</div>'
      : '<div class="hint">none yet</div>') + '</div>' + lastOrderHtml(block);
}

/* -- the backtest ------------------------------------------------------------ */

async function runBacktest(el, key) {
  const out = el.querySelector('.aw-bt-out');
  const button = el.querySelector('.aw-bt-run');
  const days = parseInt(el.querySelector('.aw-bt-days').value, 10) || 5;
  out.className = 'aw-bt-out hint';
  out.textContent = 'running on the last ' + days + ' days…';
  button.disabled = true;
  try {
    const res = await fetch('/api/backtest/' + encodeURIComponent(key) +
      '?days=' + days, { cache: 'no-store' });
    const data = await res.json();
    if (!data.ok) {
      out.className = 'aw-bt-out down';
      out.textContent = 'could not run: ' + (data.reason || data.error || 'unknown');
    } else {
      out.className = 'aw-bt-out';
      out.innerHTML = backtestHtml(data, (window.__lastSnapshot || {}).contracts
        ? ((window.__lastSnapshot.contracts.find((c) => c.key === key) || {})
          .decimals) : 4);
    }
  } catch (e) {
    out.className = 'aw-bt-out down';
    out.textContent = 'could not run: ' + e.message;
  }
  button.disabled = false;
}

function backtestHtml(data, d) {
  const s = data.summary || {};
  const plain = data.without_protections || {};
  function line(sum) {
    return (sum.trades || 0) + ' trade(s) · ' + (sum.wins || 0) + ' won, ' +
      (sum.losses || 0) + ' lost · net ' + cash(sum.net) + ' · worst run ' +
      cash(sum.max_drawdown);
  }
  function when(at) {
    return at ? new Date(at * 1000).toLocaleString([], { month: 'short',
      day: 'numeric', hour: '2-digit', minute: '2-digit' }) : DASH;
  }
  const heldBy = Object.keys(data.held || {}).map((r) => [r, data.held[r]])
    .sort((a, b) => b[1] - a[1]).slice(0, 3);
  const rows = (data.trades || []).slice(-8).reverse().map((t) =>
    '<tr><td>' + when(t.opened_at) + '</td><td>' + sideWords(t.side) +
    '</td><td>' + zText(t.entry_z) + '</td><td>' +
    esc(ALGO_EXIT_WORDS[t.reason] || t.reason || '') + '</td><td class="' +
    (t.pnl > 0 ? 'up' : (t.pnl < 0 ? 'down' : '')) + '">' + cash(t.pnl) +
    '</td></tr>').join('');
  return '<div><b>These settings:</b> ' + line(s) + '</div>' +
    '<div class="hint">Without re-entry and the trend filter: ' + line(plain) +
    '</div>' + (heldBy.length ? '<div class="hint">Held back most by: ' +
      heldBy.map((h) => esc(h[0]) + ' (' + h[1] + ')').join('; ') + '</div>' : '') +
    (rows ? '<table class="aw-bt-trades"><tr><th>Entered</th><th>Side</th>' +
      '<th>z</th><th>Exit</th><th>P&amp;L</th></tr>' + rows + '</table>' : '') +
    '<div class="hint aw-bt-caveats" title="' + esc((data.caveats || []).join('; ')) +
    '">' + (s.candles || 0) + ' candles of recorded mids, ' + when(s.from) +
    ' – ' + when(s.to) + ' · hover for what a backtest cannot see</div>';
}

/* -- the ladder ----------------------------------------------------------------
 *
 * The contract's price ladder, beside its Algo: the touch, and where the
 * Algo's own levels sit — the band, the entry, break-even, the target and
 * the stop. In ALGO mode a person does not trade it (the banner says so);
 * CLOSE always closes.
 */

const LADDER_ROWS = 24;          // each side of the centre
const ladderCentre = {};         // key -> centre price, held until Centre

function ladderFor(key) {
  const wkey = '__ladder__' + key;
  let el = document.querySelector('.win[data-key="' + wkey + '"]');
  if (el) return el;
  el = document.getElementById('ladder-template').content.firstElementChild
    .cloneNode(true);
  el.dataset.key = wkey;
  el.dataset.contract = key;
  wireWindow(el, wkey);
  el.querySelector('.ld-centre').onclick = () => { delete ladderCentre[key]; };
  el.querySelector('.ld-close').onclick = () => closeNowAsk(key,
    el.querySelector('.title').textContent);
  document.getElementById('desktop').appendChild(el);
  const place = state.places[wkey];
  if (place) placeWindow(el, place.x, place.y);
  return el;
}

function renderLadder(c, engine) {
  const wkey = '__ladder__' + c.key;
  if (state.closed.has(wkey)) return;
  const el = ladderFor(c.key);
  const d = c.decimals === undefined ? 4 : c.decimals;
  const tick = c.tick_size || Math.pow(10, -d);
  const m = c.market || {};
  const block = c.algo || {};
  const pos = c.position;
  el.querySelector('.title').textContent = c.name;
  el.querySelector('.ld-route').textContent = c.symbol || '';
  const mode = el.querySelector('.ld-mode');
  const tradingMode = (engine || {}).trading_mode || 'ALGO';
  mode.textContent = tradingMode === 'ALGO' ? 'ALGO ' + (block.mode || '') : 'MANUAL';
  mode.className = 'ld-mode ' + (tradingMode === 'ALGO'
    ? (block.mode === 'LIVE' ? 'live' : block.mode === 'PAPER' ? 'paper' : 'dry')
    : 'manual');

  const net = el.querySelector('.ld-net');
  const value = pos ? pos.net : null;
  net.textContent = pos ? (value === null || value === undefined ? DASH
    : (value > 0 ? '+' : '') + cash(value)) : 'flat';
  net.className = 'ld-net ' + (value > 0 ? 'up' : value < 0 ? 'dn' : '');
  el.querySelector('.ld-hl').textContent = pos
    ? pos.side + ' ' + pos.qty + ' @ ' + num(pos.avg_price, d) : '';

  const banner = el.querySelector('.ld-banner');
  if (tradingMode === 'ALGO') {
    banner.className = 'ld-banner algo';
    banner.textContent = 'ALGO ' + (block.mode || '') + ' — manual orders ' +
      'are off on this desk. CLOSE still closes. Switch the desk to MANUAL ' +
      'to trade by hand.';
  } else {
    banner.className = 'ld-banner manual';
    banner.textContent = 'MANUAL — the Algo neither enters nor proposes. ' +
      'Trade by hand on Instruments & orders.';
  }
  el.querySelector('.ld-close').disabled = !pos;

  // The ladder, centred on the market until Centre is pressed again.
  const mid = m.mid;
  if (mid === null || mid === undefined) {
    el.querySelector('.ld-quote').textContent = c.market_note || 'no price yet';
    return;
  }
  // Re-centred when the market leaves the middle half of the ladder — and
  // scrolled so the touch is in view, or a ladder can sit showing prices
  // nobody is quoting while the market trades below the fold.
  let recentred = false;
  if (ladderCentre[c.key] === undefined ||
      Math.abs(mid - ladderCentre[c.key]) > tick * (LADDER_ROWS / 2)) {
    ladderCentre[c.key] = mid;
    recentred = true;
  }
  const centre = ladderCentre[c.key];
  const round = (p) => Math.round(p / tick) * tick;
  const top = round(centre) + LADDER_ROWS * tick;
  const marks = {};
  function mark(price, label, cls) {
    if (price === null || price === undefined) return;
    const k = round(price).toFixed(d);
    (marks[k] = marks[k] || []).push('<span class="' + cls + '">' + label + '</span>');
  }
  if (block.ready) {
    mark(block.mean, 'MEAN', 'lm-mean');
    mark(block.upper, '+' + (block.params || {}).entry_z + 'σ', 'lm-band');
    mark(block.lower, '−' + (block.params || {}).entry_z + 'σ', 'lm-band');
  }
  if (pos) {
    mark(pos.avg_price, 'ENTRY', 'lm-entry');
    mark(pos.break_even, 'BE', 'lm-be');
    mark(pos.target, 'TP', 'lm-tp');
    mark(pos.stop, 'SL', 'lm-sl');
  }
  const bidK = m.bid === null || m.bid === undefined ? null : round(m.bid).toFixed(d);
  const askK = m.ask === null || m.ask === undefined ? null : round(m.ask).toFixed(d);
  const body = el.querySelector('.ld-table tbody');
  let html = '';
  for (let i = 0; i <= LADDER_ROWS * 2; i += 1) {
    const price = top - i * tick;
    const k = price.toFixed(d);
    const isBid = k === bidK, isAsk = k === askK;
    html += '<tr class="' + (isBid ? 'ld-bidrow' : '') + (isAsk ? ' ld-askrow' : '') +
      '"><td class="ld-bid">' + (isBid ? (m.bid_size === null || m.bid_size ===
        undefined ? '' : m.bid_size) : '') + '</td><td class="ld-px">' + k +
      '</td><td class="ld-ask">' + (isAsk ? (m.ask_size === null || m.ask_size ===
        undefined ? '' : m.ask_size) : '') + '</td><td class="ld-marks">' +
      (marks[k] || []).join(' ') + '</td></tr>';
  }
  body.innerHTML = html;
  if (recentred) {
    const scroll = el.querySelector('.ld-scroll');
    const row = body.rows[LADDER_ROWS];
    if (row) scroll.scrollTop = row.offsetTop - scroll.clientHeight / 2;
  }
  el.querySelector('.ld-quote').textContent = 'B ' + num(m.bid, d) + ' × ' +
    (m.bid_size === null || m.bid_size === undefined ? DASH : m.bid_size) +
    '   A ' + num(m.ask, d) + ' × ' +
    (m.ask_size === null || m.ask_size === undefined ? DASH : m.ask_size) +
    '   spread ' + num(m.ask - m.bid, d);
}

/* -- marking the window ---------------------------------------------------
 *
 * BLUE while a position is open, and a flash of GREEN or RED when one
 * closes. Two different kinds of thing, marked differently on purpose: the
 * blue is a STATE read straight off the snapshot, so it is still there after
 * a page reload and it cannot get stuck on a contract that is flat. The
 * flash is an EVENT, so it fades — a window left red says "this is losing",
 * which is a different statement from "the last trade lost".
 *
 * The close is read from `last_close.seq`, not from the wording of the event
 * line. A highlight driven by a regex over a sentence stops working the day
 * somebody rewords the sentence, and stops working silently.
 */
const HIGHLIGHT_HOLD_MS = 2600;
const marks = {};        // key -> { seq, timer }

function markPosition(el, c) {
  const open = !!(c.position && c.position.qty);
  el.classList.toggle('in-position', open);

  const close = c.last_close;
  const seq = (close && close.seq) || 0;
  const seen = marks[c.key];

  // First sight of a contract is not an event. Whatever it had closed
  // before this page loaded is history — without this, reloading after a
  // session flashes every window at once for trades nobody was watching.
  // Note it is recorded even when there is NO close yet: "watched, and it
  // had not closed anything" and "never seen" are different, and confusing
  // them swallows the first close of the session.
  if (seen === undefined) {
    marks[c.key] = { seq: seq, timer: null };
    return;
  }
  if (seq === seen.seq) return;                    // already marked this one
  seen.seq = seq;
  if (!seq) return;                                // nothing to mark
  flash(el, closeColour(close.net));
}

/* Green for a profit, red for a loss — and NEITHER for the two cases that
 * are not either. A net of exactly zero is not a win. A net of null is
 * UNMEASURED, and colouring it red would state a loss the system never
 * measured. Both get the neutral mark. */
function closeColour(net) {
  if (net === null || net === undefined) return 'closed-flat';
  if (net > 0) return 'closed-up';
  if (net < 0) return 'closed-down';
  return 'closed-flat';
}

function flash(el, cls) {
  const key = el.dataset.key;
  const mark = marks[key] || (marks[key] = {});
  if (mark.timer) clearTimeout(mark.timer);
  el.classList.remove('closed-up', 'closed-down', 'closed-flat', 'fading');
  // Forces the browser to notice the class went away before it comes back,
  // so two closes in a row flash twice rather than once.
  void el.offsetWidth;
  el.classList.add(cls);
  mark.timer = setTimeout(() => {
    el.classList.add('fading');
    mark.timer = setTimeout(() => {
      el.classList.remove('closed-up', 'closed-down', 'closed-flat', 'fading');
      mark.timer = null;
    }, 1700);
  }, HIGHLIGHT_HOLD_MS);
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
  const localOnly = p.position_scope === 'algo_local' || p.venue_readable === false;
  const unsupported = p.account_status === 'unavailable' || snap.engine?.connection_only;
  el.querySelector('.title').textContent = localOnly ? 'Algo positions' : 'Positions';

  el.querySelector('.pv-count').textContent =
    rows.length + (localOnly ? ' tracked' : ' open');
  el.querySelector('.pv-empty').classList.toggle('hidden', rows.length > 0);
  el.querySelector('.pv-empty').textContent = localOnly
    ? 'No open algo positions recorded by this app.' : 'No open positions reported.';

  // "could not read" is not "flat", and the table must not imply it is.
  const banner = el.querySelector('.pv-banner');
  const unreadable = p.venue_readable === false;
  banner.classList.toggle('hidden', !unreadable);
  banner.classList.toggle('critical', unreadable && !unsupported);
  if (unreadable) {
    banner.textContent = unsupported
      ? 'Showing this app\'s algo book. Account-wide positions are not verified. Live quotes and strategy monitoring remain available.'
      : 'The venue could not be read, so what is shown is ' +
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
    ? (rows.every((r) => r.agrees) ? 'book and venue agree' : 'position reconciliation required')
    : 'account verification unavailable';
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
                   modeChosen: false,
                   //: Which load is current. Two are easily in flight at
                   //: once — a poll and a filter change — and if the slower
                   //: one renders last the window shows one filter's rows
                   //: under another filter's label. On this window that is
                   //: not cosmetic: it is simulated trades appearing in a
                   //: figure labelled live.
                   seq: 0,
                   //: The replay is expensive and its answer does not change
                   //: with a poll — only with the contract or the period.
                   replayFor: null, replayCache: null };

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

/* -- what a different threshold would have done ---------------------------
 *
 * The touch table says which level REVERTS most — the inner bands always do.
 * This is the question that follows, and it has a different answer: what did
 * each level MAKE, after this contract's own round trip.
 *
 * Everything the replay does not know is printed under the table. A backtest
 * whose assumptions are not on the page is a backtest somebody will quote
 * without them.
 */
function renderReplay(el, report, live) {
  const body = el.querySelector('.an-replay tbody');
  const finding = el.querySelector('.an-replay-finding');
  const note = el.querySelector('.an-replay-assumptions');
  body.innerHTML = '';
  // Nothing to run, or nothing long enough to run: that is about the
  // RECORDING, and a table of dashes would read as "nothing paid".
  if (!report || !report.rows || !report.rows.length || report.blocked_by) {
    finding.className = 'finding warn an-replay-finding';
    finding.textContent = 'Nothing to replay — ' +
      ((report && report.blocked_by) ||
       'no recorded prices for this contract over this period') +
      '. The engine records a sample per pass while it runs.';
    note.textContent = '';
    return;
  }
  const best = report.best;
  report.rows.forEach((r) => {
    const tr = document.createElement('tr');
    const isLive = live !== null && live !== undefined &&
      Math.abs(r.entry_threshold - live) < 1e-9;
    // A row nobody can judge is shown — the contrast is the point — and
    // dimmed, because it is not a threshold to aim at.
    if (!r.enough_to_judge) tr.classList.add('cannot-pay');
    if (best && r.entry_threshold === best.entry_threshold) {
      tr.classList.add('pays');
    }
    tr.innerHTML =
      '<td><span class="tag">±' + num(r.entry_threshold, 2) + '</span>' +
      (isLive ? ' <small>live</small>' : '') + '</td>' +
      '<td class="r">' + (r.trades || 0) + '</td>' +
      '<td class="r">' + (r.win_rate === null ? DASH : num(r.win_rate, 1) + '%') + '</td>' +
      '<td class="r ' + cls(r.net) + '">' + money(r.net) + '</td>' +
      '<td class="r ' + cls(r.per_trade) + '">' + money(r.per_trade) + '</td>' +
      '<td class="r ' + cls(r.worst) + '">' + money(r.worst) + '</td>' +
      '<td class="r">' + (r.median_hold_sec === null ? DASH
        : seconds(r.median_hold_sec)) + '</td>' +
      '<td>' + (r.enough_to_judge ? 'yes'
        : '<span title="fewer than ten closed trades">too few</span>') + '</td>';
    body.appendChild(tr);
  });

  if (best) {
    finding.className = 'finding an-replay-finding';
    finding.innerHTML = '<b>±' + num(best.entry_threshold, 2) +
      ' is the threshold that PAID on this recording</b> — ' + best.trades +
      ' closed trades for ' + money(best.net) + ' after costs (' +
      money(best.per_trade) + ' each). A level that reverts more often is ' +
      'not the same as a level that pays: the inner bands come back more ' +
      'reliably and their move cannot cover the round trip.';
  } else {
    finding.className = 'finding warn an-replay-finding';
    // If every row was withheld, the reason is the signal's own words, not
    // a guess about the threshold — that is what sends a desk to change the
    // number that was never the problem.
    const withheld = report.rows.map((r) => r.blocked_by).filter(Boolean);
    finding.textContent = withheld.length === report.rows.length
      ? 'Nothing was entered at any threshold — ' + withheld[0] +
        '. The threshold is not what is stopping this.'
      : 'No threshold cleared its own costs on this recording with enough ' +
        'trades to judge. That is a finding, not a missing number — ' +
        'lowering the threshold onto something that reverts beautifully is ' +
        'exactly how it gets worse.';
  }

  const a = report.assumptions || {};
  note.innerHTML = 'This is a <b>signal</b> replay, not a fill simulator. ' +
    'The book was ' + (a.book || 'assumed') + '; ' + (a.fills || '') + '; ' +
    (a.costs || '') + '. ' +
    (a.margin ? '<b>Margin:</b> ' + a.margin + '. ' : '') +
    'Read against ' + (report.samples || 0) +
    ' recorded prices.';
}

function cls(v) {
  if (v === null || v === undefined) return '';
  return v > 0 ? 'up' : v < 0 ? 'dn' : '';
}

async function loadAnalysis() {
  if (state.closed.has('__analysis__')) return;
  const mine = ++analysis.seq;
  const current = () => mine === analysis.seq;
  const el = analysisWindow();
  const contracts = (window.__lastSnapshot || {}).contracts || [];
  if (analysis.key && !contracts.some((c) => c.key === analysis.key)) {
    analysis.key = contracts.length ? contracts[0].key : null;
  }
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
    if (!current()) return;                 // superseded while in flight
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
    if (!current()) return;                 // superseded while in flight
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
    // Much slower than the rest of the window — it re-runs the whole
    // recorded series at every threshold — so it is fetched separately, and
    // only when the question it answers has actually changed. Re-running it
    // on the fifteen-second refresh would spend most of the desk's CPU
    // recomputing an answer nobody asked again.
    const asked = analysis.key + '|' + analysis.period;
    if (asked !== analysis.replayFor) {
      analysis.replayFor = asked;
      getAnalysis('/api/replay/' + encodeURIComponent(analysis.key) +
        '?period=' + analysis.period).then((r) => {
          analysis.replayCache = r;
          if (current()) renderReplay(el, r, report.entry_threshold);
        });
    } else if (analysis.replayCache) {
      renderReplay(el, analysis.replayCache, report.entry_threshold);
    }
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
  const mine = analysis.seq;
  const others = Object.keys(modes).filter((m) => m !== analysis.mode);
  for (const other of others) {
    if (mine !== analysis.seq) return;      // the filter moved on
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
    if (el.classList.contains('minimised')) b.classList.add('minimised');
    // Like any taskbar: a minimised window comes back; the window in front
    // goes down; any other comes to the front.
    b.onclick = () => {
      if (el.classList.contains('minimised')) { setMinimised(el, false); return; }
      if (state.focused === el.dataset.key && el.classList.contains('raised')) {
        setMinimised(el, true);
        return;
      }
      state.focused = el.dataset.key;
      raise(el);
      el.scrollIntoView({ block: 'nearest' });
      renderTabs();
    };
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
  } else if (engine.connection_only) {
    banner.classList.remove('hidden', 'critical');
    banner.textContent = 'Live FIX market data and algo proposals are available. Automatic orders require account recovery and FIX execution integration. Reviewed manual UAT orders remain under Instruments & orders.';
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
    const said = 'SAVED, NOT IN FORCE — ' + waiting.join(', ') +
      '. These change something the running engine already holds; restart it ' +
      'for them to take effect. Everything else you saved is live now.';
    // Rebuilt only when the list changes, or the button would be replaced
    // under the pointer twice a second.
    if (restart.dataset.said !== said) {
      restart.dataset.said = said;
      restart.textContent = said + ' ';
      if (engine.supervised) {
        const b = document.createElement('button');
        b.className = 'btn sm';
        b.textContent = 'Restart engine now';
        b.onclick = restartEngine;
        restart.appendChild(b);
      }
    }
  } else {
    restart.dataset.said = '';
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
  const mode = document.getElementById('mode-toggle');
  mode.textContent = 'Mode: ' + (engine.trading_mode || '—');
  mode.dataset.mode = engine.trading_mode || '';
  mode.classList.toggle('manual', engine.trading_mode === 'MANUAL');
  const auto = document.getElementById('auto-trade-toggle');
  // On a venue whose order path is not wired (TT today) automatic trading is
  // PAPER: filled at the live bid/offer inside the engine, nothing sent.
  auto.textContent = 'Auto trade: ' + (engine.auto_trade_enabled
    ? (engine.paper ? 'PAPER' : 'ON') : 'OFF');
  auto.classList.toggle('act', !!engine.auto_trade_enabled);
  auto.title = engine.auto_trade_available
    ? 'Turn automatic order placement on or off'
    : 'Automatic FIX orders require account recovery and execution integration';

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
    ['timeframe_min', 'Candles of', 'minutes; each closes on the last MID in it, the one forming now counts', 'select', { options: [['1', '1 min'], ['5', '5 min'], ['15', '15 min'], ['30', '30 min'], ['60', '1 hour'], ['240', '4 hours']] }],
    ['length', 'Band length N', 'the middle is EMA(N) of the closes, σ the population σ of the last N', 'number', { step: 1, min: 2 }],
    ['entry_threshold', 'Entry z', 'H to L when the BID z reaches +this; L to H when the OFFER z reaches &minus;this', 'number', { step: 0.1, min: 0 }],
    ['max_entry_z', 'No entry beyond |z|', 'a blow-out, not a stretch; 0 = no cap', 'number', { step: 0.1, min: 0 }],
    ['confirm_samples', 'Confirm for', 'fresh quotes in a row', 'number', { step: 1, min: 1 }],
    ['trade_direction', 'Enter', 'entries only &mdash; an exit is never restricted', 'select', { options: [['BOTH', 'Both ways'], ['SELL_ONLY', 'H to L only (sell)'], ['BUY_ONLY', 'L to H only (buy)']] }],
    ['reentry_on', 'Re-entry', 'ARMED at the band, entered on the way back in &mdash; a trend rides the band and never gives one', 'check'],
    ['reentry_back', 'Enter back by', 'z back inside the band', 'number', { step: 0.1, min: 0.05 }],
    ['reentry_window_pct', 'Re-entry window', '% of the way back to the mean that still counts; past it the side disarms', 'number', { step: 5, min: 5 }],
    ['entry_cooldown_seconds', 'Cooldown after an exit', 'seconds before the next entry', 'number', { step: 30, min: 0 }],
    ['warmup_min', 'Warm-up', 'minutes of LIVE prices watched since the Algo was armed; 0 = off', 'number', { step: 10, min: 0 }],
    ['cutoff_buffer_min', 'No entry before the close', 'minutes before the session close; 0 = off', 'number', { step: 5, min: 0 }],
  ] },
  { name: 'Filters', fields: [
    ['edge_on', 'Edge', 'expected capture must be a multiple of the round trip', 'check'],
    ['edge_multiple', 'Edge required', '&times; the round-trip cost', 'number', { step: 0.1, min: 0 }],
    ['edge_capture_frac', 'Capture expected', 'of the full move back to the mean, 0&ndash;1', 'number', { step: 0.05, min: 0 }],
    ['regime_on', 'Regime', 'no entry while the contract is TRENDING', 'check'],
    ['regime_er_max', 'Trending above efficiency', 'net move over path, 0&ndash;1', 'number', { step: 0.05, min: 0 }],
    ['regime_min_crossings', '&hellip; and mean crossings at most', 'over the last 2N candles', 'number', { step: 1, min: 0 }],
    ['trend_on', 'Trend', 'no entry AGAINST a middle that moved this far', 'check'],
    ['trend_sigma', 'Trend limit', 'σ of drift in the EMA', 'number', { step: 0.1, min: 0 }],
    ['trend_lookback_min', 'over the last', 'minutes', 'number', { step: 15, min: 1 }],
    ['half_life_min_min', 'Half-life at least', 'minutes; 0 = off', 'number', { step: 5, min: 0 }],
    ['half_life_max_min', 'Half-life at most', 'minutes; 0 = off', 'number', { step: 30, min: 0 }],
  ] },
  { name: 'Exit', fields: [
    ['margin_per_contract', 'Margin per contract', 'money; TT does not report it. Target and stop are % of this &mdash; no margin, no entries', 'number', { step: 50, min: 0 }],
    ['target_mode', 'Target sized by', '', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['profit_target_pct', 'Profit target', '% of the margin, from break-even (after the whole round trip)', 'number', { step: 0.5, min: 0 }],
    ['stop_loss_on', 'Stop loss', 'from break-even, the mirror of the target', 'check'],
    ['stop_mode', 'Stop sized by', '', 'select', { options: [['MARGIN', '% of margin'], ['ATR', 'ATR multiple']] }],
    ['stop_loss_pct', 'Stop loss at', '% of the margin, below break-even', 'number', { step: 0.5, min: 0 }],
    ['atr_period', 'ATR period', 'candles, Wilder, close to close', 'number', { step: 1, min: 2 }],
    ['atr_target_mult', 'Target', '&times; ATR (in ATR mode), frozen at entry', 'number', { step: 0.1, min: 0 }],
    ['atr_stop_mult', 'Stop', '&times; ATR (in ATR mode), frozen at entry', 'number', { step: 0.1, min: 0 }],
    ['stop_z_on', 'Z stop', 'on the side it would close on', 'check'],
    ['stop_loss_z', 'Z stop at |z|', '', 'number', { step: 0.5, min: 0 }],
    ['exit_at_mean', 'Exit at the mean when paid', 'back at the mean and net positive, short of the target', 'check'],
    ['max_hold_minutes', 'Time stop', 'minutes, whatever the P&amp;L; 0 = none', 'number', { step: 15, min: 0 }],
    ['progress_bar', 'SL &larr; entry &rarr; TP bar', 'in the Algo window while a position is on', 'check'],
  ] },
  { name: 'Book', fields: [
    ['min_book_size', 'Book size at least', 'contracts on the touch, both sides', 'number', { step: 1, min: 0 }],
    ['max_book_spread_ticks', 'Book no wider than', 'ticks', 'number', { step: 1, min: 0 }],
  ] },
  { name: 'Size & risk', fields: [
    ['quantity', 'Algo quantity', 'contracts per entry', 'number', { step: 1, min: 0 }],
    ['max_position', 'Maximum position', 'contracts; the hard ceiling', 'number', { step: 1, min: 0 }],
    ['max_trades_per_day', 'Trades per day', 'Algo entries; 0 = no limit', 'number', { step: 1, min: 0 }],
    ['max_losses_row', 'Losing trades in a row', 'pauses entries for the day; 0 = off', 'number', { step: 1, min: 0 }],
    ['daily_max_loss', 'Daily loss limit', 'money, closed plus open; stops entries for the day', 'number', { step: 50, min: 0 }],
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

/* -- the chart ----------------------------------------------------------- */

/* One contract's price over its window, with the mean and the bands the
 * signal is reading NOW, and the entries and exits inside the window. The
 * bands are the standing ones — recomputed every few minutes — drawn across
 * the whole window, and the footnote says so: they are today's levels, not a
 * history of where the levels were. */
const charts = {};

function chartWindow(key) {
  const wkey = '__chart__' + key;
  let el = document.querySelector('.win[data-key="' + wkey + '"]');
  if (el) { raise(el); return el; }
  const tpl = document.getElementById('chart-template');
  el = tpl.content.firstElementChild.cloneNode(true);
  el.dataset.key = wkey;
  el.dataset.contract = key;
  el.querySelector('.close').onclick = () => {
    clearInterval((charts[key] || {}).timer);
    delete charts[key];
    el.remove();
    renderTabs();
  };
  el.onmousedown = () => raise(el);
  makeDraggable(el, wkey);
  const desk = document.getElementById('desktop');
  desk.appendChild(el);
  const place = state.places[wkey];
  if (place) {
    placeWindow(el, place.x, place.y);
  } else {
    el.classList.add('floating');
    const owner = document.querySelector('.win[data-key="' + key + '"]');
    const box = desk.getBoundingClientRect();
    const from = owner ? owner.getBoundingClientRect() : box;
    el.style.left = Math.max(8, Math.min(from.left - box.left + 40,
                                         desk.clientWidth - 620)) + 'px';
    el.style.top = (from.top - box.top + desk.scrollTop + 30) + 'px';
  }
  raise(el);
  charts[key] = { timer: setInterval(() => loadChart(key), 5000) };
  loadChart(key);
  return el;
}

async function loadChart(key) {
  const el = document.querySelector('.win[data-key="__chart__' + key + '"]');
  if (!el) return;
  try {
    const data = await (await fetch('/api/series/' + encodeURIComponent(key),
      { cache: 'no-store' })).json();
    const snap = window.__lastSnapshot || {};
    const c = (snap.contracts || []).find((x) => x.key === key) || {};
    drawChart(el, data, c);
  } catch (e) {
    el.querySelector('.chart-note').textContent = 'The chart could not be loaded.';
  }
}

function svgEl(tag, attrs) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
  return n;
}

function drawChart(el, data, c) {
  el.querySelector('.title').textContent = (c.name || data.key) + ' · chart';
  const note = el.querySelector('.chart-note');
  const svg = el.querySelector('svg.chart');
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const pts = (data.series || []).map(([t, p]) => [Date.parse(t), p]);
  if (pts.length < 2) {
    note.textContent = 'Nothing recorded for this contract in the last ' +
      Math.round(data.minutes || 0) + ' minutes yet.';
    return;
  }
  // The Algo's band: the EMA middle and its sigma, as the window shows them.
  const a = c.algo || {};
  const s = { mean: a.mean, std: a.sigma };
  const d = c.decimals === undefined ? (data.decimals || 4) : c.decimals;
  const k = Number((a.params || {}).entry_z) || Number(data.entry_threshold) || 2.5;
  const stop = Number(data.stop_loss_z) || 4;
  const levels = [];
  if (s.mean !== null && s.mean !== undefined && s.std) {
    levels.push(['mean', s.mean, 'mean']);
    levels.push(['entry', s.mean + k * s.std, '+' + k + 'σ sell']);
    levels.push(['entry', s.mean - k * s.std, '−' + k + 'σ buy']);
    levels.push(['stop', s.mean + stop * s.std, '+' + stop + 'σ stop']);
    levels.push(['stop', s.mean - stop * s.std, '−' + stop + 'σ stop']);
  }
  const marks = data.marks || [];
  const ys = pts.map((p) => p[1]).concat(levels.map((l) => l[1]),
    marks.map((m) => m.price));
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi === lo) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const t0 = pts[0][0], t1 = Math.max(pts[pts.length - 1][0], t0 + 1);
  const W = 600, H = 300, L = 8, R = 88, T = 8, B = 20;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  const x = (t) => L + (t - t0) / (t1 - t0) * (W - L - R);
  const y = (p) => T + (hi - p) / (hi - lo) * (H - T - B);

  levels.forEach(([kind, v, label]) => {
    svg.appendChild(svgEl('line', { x1: L, x2: W - R, y1: y(v), y2: y(v),
      class: 'lvl lvl-' + kind }));
    const t = svgEl('text', { x: W - R + 4, y: y(v) + 3, class: 'lvl-label' });
    t.textContent = label + ' ' + num(v, d);
    svg.appendChild(t);
  });
  svg.appendChild(svgEl('polyline', { class: 'px',
    points: pts.map((p) => x(p[0]).toFixed(1) + ',' + y(p[1]).toFixed(1)).join(' ') }));
  marks.forEach((m) => {
    const cx = x(Date.parse(m.ts)), cy = y(m.price);
    if (cx < L || cx > W - R) return;
    const up = (m.kind === 'open') === (m.side === 'BUY');
    const tri = up
      ? [cx, cy - 6, cx - 5, cy + 3, cx + 5, cy + 3]
      : [cx, cy + 6, cx - 5, cy - 3, cx + 5, cy - 3];
    const shape = svgEl('polygon', { points: tri.join(' '),
      class: 'mark ' + m.kind + ' ' + m.side });
    const tip = svgEl('title');
    tip.textContent = (m.kind === 'open' ? 'entry ' : 'exit ') + m.side +
      ' @ ' + num(m.price, d) + (m.reason ? ' · ' + m.reason : '');
    shape.appendChild(tip);
    svg.appendChild(shape);
  });
  const fmt = (t) => new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  [[t0, 'start'], [t1, 'end']].forEach(([t, a]) => {
    const lab = svgEl('text', { x: a === 'start' ? L : W - R, y: H - 5,
      class: 'axis', 'text-anchor': a === 'start' ? 'start' : 'end' });
    lab.textContent = fmt(t);
    svg.appendChild(lab);
  });
  note.textContent = 'The last ' + Math.round(data.minutes) + ' minutes of the ' +
    'recorded mid. The EMA middle and its bands are the ones the Algo is ' +
    'reading now, drawn across the window — not a history ' +
    'of where they were. ▲ buy · ▼ sell; hover a mark for its reason.';
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
    // The box shows what is IN FORCE: its own setting, or the desk's. A box
    // that read unticked for a desk default of ON would, on Save, write OFF —
    // a stop loss switched off by pressing Save on another field.
    const on = (own === null || own === undefined) ? eff === true : own === true;
    control = '<input type="checkbox" class="chk" id="' + id +
      '" data-field="' + field + '" data-was="' + (on ? '1' : '0') + '"' +
      (own === null || own === undefined ? ' data-default="1"' : '') +
      (on ? ' checked' : '') + '>';
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
    if (input.type === 'checkbox') {
      // Untouched and still on the desk default: leave it on the default.
      if (input.dataset.default === '1' &&
          input.checked === (input.dataset.was === '1')) return;
      body[field] = input.checked;
    }
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
      seen.add('__chart__' + c.key);
      seen.add('__ladder__' + c.key);
      // The ladder first: on a fresh desk it sits to the LEFT of its Algo.
      renderLadder(c, snap.engine);
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
/* ALGO or MANUAL — one at a time. The engine refuses the switch while the
 * side being left has anything open or working, and says what. */
document.getElementById('mode-toggle').onclick = async (e) => {
  const now = e.currentTarget.dataset.mode;
  const next = now === 'MANUAL' ? 'ALGO' : 'MANUAL';
  const ok = await ask('Switch the desk to ' + next,
    next === 'MANUAL'
      ? 'The algo stops entering and proposing on every contract, and ' +
        'automatic trading is turned off. Manual orders on Instruments & ' +
        'orders are allowed. Refused while the algo has a position or ' +
        'working order open.'
      : 'Manual orders are refused from now on, and any reviewed manual ' +
        'ticket is discarded. The algo may trade again. Refused while a ' +
        'manual order is working or a manual fill is not yet closed.',
    'Switch to ' + next);
  if (!ok) return;
  const answer = await command('trading_mode', '', { mode: next });
  if (answer && answer.ok) toast('OK', 'MODE', 'the desk is in ' + next + ' mode');
};

document.getElementById('auto-trade-toggle').onclick = async () => {
  const on = document.getElementById('auto-trade-toggle').classList.contains('act');
  await command('auto_trade', '', { on: !on });
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
    el.style.width = ''; el.style.height = '';
  });
};

/* Text size, for the whole terminal. Kept per browser: it is a matter of
 * the screen and the eyes in front of it, not of the desk. */
function setTextSize(scale) {
  const fs = Math.round(Math.min(2.2, Math.max(0.9, scale)) * 10) / 10;
  document.documentElement.style.setProperty('--fs', String(fs));
  try { localStorage.setItem('ft.fs', String(fs)); } catch (e) { /* forgets */ }
  const label = document.getElementById('text-size');
  if (label) label.textContent = 'Text ' + Math.round(fs * 100) + '%';
}
function textSize() {
  return parseFloat(getComputedStyle(document.documentElement)
    .getPropertyValue('--fs')) || 1.3;
}
document.getElementById('text-smaller').onclick = () => setTextSize(textSize() - 0.1);
document.getElementById('text-bigger').onclick = () => setTextSize(textSize() + 0.1);
setTextSize(textSize());

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
