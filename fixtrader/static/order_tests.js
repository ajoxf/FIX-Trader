/* Order tests: test every order flow on TT UAT.
 *   1  pick a contract
 *   2  check automatically — the program runs each flow (fixtrader/uat.py)
 *      through the same commands the ladder and the Algo window send
 *   3  try it yourself — the desk's own ladder, Algo window and Trading
 *      Monitor, embedded, as they are traded live
 */
'use strict';

const $ = (id) => document.getElementById(id);
const OT = { scenarios: [], env: null, running: false, contractsSeen: '',
             mode: null, sendWord: 'UAT', checks: {}, deskFor: null };
const ICON = { PASS: '✓', FAIL: '✗', SKIP: '–', RUNNING: '…' };

async function command(action, contract, args) {
  const q = await postJSON('/api/command', { action, contract: contract || '', args: args || {} });
  if (!q.data.ok) return q.data;
  const until = Date.now() + 20000;
  while (Date.now() < until) {
    const r = await getJSON('/api/result/' + q.data.id);
    if (!r.pending) return r;
    await new Promise((ok) => setTimeout(ok, 80));
  }
  return { ok: false, error: 'the engine did not answer' };
}

function flowRow(s, result, current) {
  const li = document.createElement('li');
  const status = current ? 'RUNNING' : (result ? result.status : '');
  li.className = status;
  const st = document.createElement('span'); st.className = 'st';
  st.textContent = ICON[status] || '·';
  const text = document.createElement('span');
  text.textContent = s.short;
  li.title = s.title;
  li.append(st, text);
  if (result || current) {
    const ev = document.createElement('small');
    ev.textContent = current ? 'running…' : String(result.detail || '').replace(/^SKIP: /, '');
    li.appendChild(ev);
  }
  return li;
}

function paint(body) {
  OT.env = body.environment || null;
  OT.checks = body.checks || {};
  const run = body.run && body.run.running ? body.run : null;
  OT.running = !!run;
  const shown = run || body.last || null;
  const results = {};
  ((shown && shown.results) || []).forEach((r) => { results[r.id] = r; });
  const ul = $('ot-flows');
  ul.textContent = '';
  if (results.PRE) {
    ul.appendChild(flowRow({ short: 'Ready to test', title: 'Preflight' }, results.PRE, false));
  }
  [['MANUAL ORDERS', 'manual'], ['ALGO ORDERS', 'algo']].forEach(([label, kind]) => {
    const g = document.createElement('li'); g.className = 'group'; g.textContent = label;
    ul.appendChild(g);
    OT.scenarios.filter((s) => s.kind === kind)
      .forEach((s) => ul.appendChild(flowRow(s, results[s.id], run && run.current === s.id)));
  });
  $('ot-log').textContent = ((shown && shown.log) || []).join('\n') ||
    (shown ? '' : 'Nothing has been run yet.');
  const uat = OT.env === 'UAT';
  $('ot-run-manual').disabled = $('ot-run-algo').disabled = OT.running || !uat;
  $('ot-stop').disabled = !OT.running;
  paintTried();
  const b = $('ot-banner');
  if (uat) {
    b.className = 'ot-banner uat';
    b.textContent = 'TT UAT — orders placed here are REAL orders on TT UAT (a test venue). ' +
      'Nothing goes to a live market.';
  } else if (OT.env === 'PROD') {
    b.className = 'ot-banner prod';
    b.textContent = 'LIVE MARKET — order tests are off here. The last UAT results are kept below.';
  } else {
    b.className = 'ot-banner off';
    b.textContent = 'Not connected to TT UAT (' + (OT.env || 'the engine is not up') +
      ') — start the program on TT UAT to test orders.';
  }
}

/* What the trader has tried by hand: a tick per flow, kept with the time. */
function paintTried() {
  const box = $('ot-tried');
  if (box.dataset.built === OT.scenarios.length + '' && box.querySelectorAll('input').length) {
    box.querySelectorAll('input').forEach((i) => { i.checked = !!(OT.checks[i.value] || {}).result; });
    return;
  }
  box.textContent = '';
  const lead = document.createElement('span'); lead.className = 'lead'; lead.textContent = 'Tried by hand:';
  box.appendChild(lead);
  OT.scenarios.filter((s) => s.id !== 'M6').forEach((s) => {
    const l = document.createElement('label');
    const i = document.createElement('input');
    i.type = 'checkbox'; i.value = s.id;
    i.checked = !!(OT.checks[s.id] || {}).result;
    i.onchange = () => postJSON('/api/order-tests/check',
      { id: s.id, result: i.checked ? 'PASS' : '', contract: $('ot-contract').value });
    l.append(i, document.createTextNode(s.short));
    box.appendChild(l);
  });
  box.dataset.built = OT.scenarios.length + '';
}

function paintDesk() {
  const key = $('ot-contract').value;
  if (!key || key === OT.deskFor) return;
  OT.deskFor = key;
  $('ot-desk').src = '/desk?embed=uat&contract=' + encodeURIComponent(key);
}

async function refresh() {
  try {
    const body = await getJSON('/api/order-tests');
    if (!OT.scenarios.length) OT.scenarios = body.scenarios;
    paint(body);
  } catch (e) { /* the next poll tries again */ }
  try {
    const snap = await getJSON('/api/snapshot');
    const contracts = (snap.contracts || []).map((c) => c.key + '|' + (c.name || ''));
    if (contracts.join() !== OT.contractsSeen) {
      OT.contractsSeen = contracts.join();
      const sel = $('ot-contract');
      const was = sel.value;
      sel.textContent = '';
      (snap.contracts || []).forEach((c) => {
        const o = document.createElement('option');
        o.value = c.key;
        o.textContent = `${c.name || c.symbol || c.key} (${c.key})`;
        sel.appendChild(o);
      });
      if (was) sel.value = was;
      paintDesk();
    }
    const ex = (snap.engine || {}).execution || {};
    OT.mode = ex.mode || null;
    OT.sendWord = ex.send_word || 'UAT';
    $('ot-exec').textContent = 'ALGO ORDERS: ' + (ex.mode === 'LIVE' ? 'TT ' + OT.sendWord
      : ex.mode === 'PAPER' ? 'PAPER' : (ex.mode || DASH));
  } catch (e) { /* idem */ }
}

function chosen(kind) {
  const hits = $('ot-hits').checked;
  return OT.scenarios.filter((s) => s.kind === kind && (hits || !s.waits)).map((s) => s.id);
}

async function run(kind) {
  const ids = chosen(kind);
  const contract = $('ot-contract').value;
  const qty = $('ot-qty').value;
  const sel = $('ot-contract');
  const name = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].textContent : contract;
  if (kind === 'algo' && OT.mode !== 'LIVE') {
    // The Algo's orders go to TT UAT, not filled on paper: the engine's own
    // words, confirmed once.
    const asked = await command('execution', '', { mode: 'LIVE' });
    if (!asked.ok && !asked.confirm) { toast('REJECT', 'Algo orders', asked.error || 'refused'); return; }
    if (asked.confirm) {
      if (!await ask('Send the Algo\'s orders to TT UAT?', asked.text, 'Send to UAT')) return;
      const done = await command('execution', '', { mode: 'LIVE', confirm: true });
      if (!done.ok) { toast('REJECT', 'Algo orders', done.error || 'refused'); return; }
    }
  }
  const what = kind === 'algo' ? 'the Algo\'s order flows' : 'the manual order flows';
  const yes = await ask('Check ' + what + ' on ' + name + '?',
    `${ids.length} checks, ${qty} lot(s) per order. Real orders go to TT UAT; each check cancels and closes ` +
    'what it placed, so the contract ends flat. Takes about a minute' +
    ($('ot-hits').checked ? ' — longer with the "wait for a fill" checks.' : '.'), 'Start');
  if (!yes) return;
  const r = await postJSON('/api/order-tests/run', {
    ids, contract, qty, away: $('ot-away').value, hit_wait: $('ot-wait').value, confirm: true });
  if (!r.data.ok) toast('REJECT', 'Order tests', r.data.error || 'refused');
  refresh();
}

$('ot-run-manual').onclick = () => run('manual');
$('ot-run-algo').onclick = () => run('algo');
$('ot-stop').onclick = async () => {
  const r = await postJSON('/api/order-tests/stop', {});
  toast(r.data.ok ? 'INFO' : 'REJECT', 'Order tests', r.data.text || r.data.error || '');
  refresh();
};
$('ot-contract').onchange = paintDesk;

refresh();
setInterval(refresh, 1000);
