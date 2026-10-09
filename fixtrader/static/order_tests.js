/* Order tests: every order path the desk uses, run on TT UAT from the screen.
 * The tests run in the web process (fixtrader/uat.py) through the same
 * commands the ladder and the Algo window send; this page starts them,
 * shows each result as it lands, and keeps the last run.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const OT = { scenarios: [], picked: new Set(), env: null, running: false,
             contractsSeen: '', mode: null };

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

function rowFor(s, result, current) {
  const tr = document.createElement('tr');
  const status = current ? 'RUNNING' : (result ? result.status : '');
  tr.className = status;
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = OT.picked.has(s.id);
  box.disabled = OT.running;
  box.onchange = () => { box.checked ? OT.picked.add(s.id) : OT.picked.delete(s.id); paintSummary(); };
  const td0 = document.createElement('td'); td0.appendChild(box);
  const td1 = document.createElement('td');
  td1.innerHTML = '<span class="kind"></span><b></b>';
  td1.querySelector('.kind').textContent = s.kind === 'algo' ? 'ALGO' : 'MANUAL';
  td1.querySelector('b').textContent = s.id;
  const td2 = document.createElement('td'); td2.className = 'txt';
  td2.textContent = s.title + (s.waits ? ' (waits for the market)' : '');
  const how = document.createElement('details');
  how.innerHTML = '<summary>By hand</summary><div></div>';
  how.querySelector('div').textContent = s.steps;
  td2.appendChild(how);
  const td3 = document.createElement('td'); td3.className = 'res';
  td3.textContent = status || DASH;
  const td4 = document.createElement('td'); td4.className = 'ev';
  td4.textContent = result ? result.detail : (current ? 'running…' : '');
  const td5 = document.createElement('td'); td5.className = 'r';
  td5.textContent = result && result.seconds != null ? result.seconds : '';
  tr.append(td0, td1, td2, td3, td4, td5);
  return tr;
}

function paintSummary(results) {
  const n = OT.picked.size;
  const r = results || [];
  const count = (k) => r.filter((x) => x.status === k).length;
  $('ot-summary').textContent = r.length
    ? `${count('PASS')} passed · ${count('FAIL')} failed · ${count('SKIP')} skipped`
    : `${n} selected`;
  $('ot-run').disabled = OT.running || !n || OT.env !== 'UAT';
}

function paint(body) {
  OT.env = body.environment || null;
  const run = body.run && body.run.running ? body.run : null;
  OT.running = !!run;
  const shown = run || body.last || null;
  const results = {};
  ((shown && shown.results) || []).forEach((r) => { results[r.id] = r; });
  const rows = $('ot-rows');
  rows.textContent = '';
  if (results.PRE) {
    const s = { id: 'PRE', title: 'Preflight — the venue, the session, the contract, flat', steps: '', kind: 'manual' };
    rows.appendChild(rowFor(s, results.PRE, false));
    rows.lastChild.firstChild.textContent = '';
  }
  OT.scenarios.forEach((s) => rows.appendChild(rowFor(s, results[s.id], run && run.current === s.id)));
  paintSummary(shown ? shown.results : null);
  $('ot-stop').disabled = !OT.running;
  $('ot-log').textContent = ((shown && shown.log) || []).join('\n');
  $('ot-when').textContent = shown
    ? `${run ? 'running since' : 'last run'} ${shown.started || ''} · ${shown.contract || ''} · ` +
      `${shown.environment || ''}${shown.finished ? ' · finished ' + shown.finished : ''}`
    : 'no run yet';
  const b = $('ot-banner');
  if (OT.env === 'UAT') {
    b.className = 'ot-banner uat';
    b.textContent = 'TT UAT — these tests send REAL orders to TT UAT and close them again. ' +
      'Start from flat on the contract. Algo tests need Execution LIVE.';
  } else if (OT.env === 'PROD') {
    b.className = 'ot-banner prod';
    b.textContent = 'LIVE venue — automatic order tests are refused here. The last UAT run is shown ' +
      'as the record of what was proven; test by hand with the steps under each test, at the size you mean.';
  } else {
    b.className = 'ot-banner off';
    b.textContent = 'No TT UAT venue is running (' + (OT.env || 'engine not up') +
      ') — start the program on TT UAT to run the order tests.';
  }
}

async function refresh() {
  try {
    const body = await getJSON('/api/order-tests');
    if (!OT.scenarios.length) {
      OT.scenarios = body.scenarios;
      OT.scenarios.filter((s) => !s.waits && s.kind === 'manual').forEach((s) => OT.picked.add(s.id));
    }
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
        o.textContent = `${c.key} — ${c.name || c.symbol || ''}`;
        sel.appendChild(o);
      });
      if (was) sel.value = was;
    }
    const ex = (snap.engine || {}).execution || {};
    OT.mode = ex.mode || null;
    $('ot-exec').textContent = 'EXECUTION ' + (ex.mode || DASH);
    $('ot-live-text').textContent = ex.mode === 'LIVE'
      ? 'Execution LIVE — the Algo tests send to TT.'
      : 'Execution ' + (ex.mode || DASH) + ' — the Algo tests need LIVE (manual tests do not).';
    $('ot-arm').disabled = ex.mode === 'LIVE' || OT.env !== 'UAT';
  } catch (e) { /* idem */ }
}

$('ot-pick-manual').onclick = () => { OT.picked = new Set(OT.scenarios.filter((s) => s.kind === 'manual' && !s.waits).map((s) => s.id)); refresh(); };
$('ot-pick-algo').onclick = () => { OT.picked = new Set(OT.scenarios.filter((s) => s.kind === 'algo' && !s.waits).map((s) => s.id)); refresh(); };
$('ot-pick-all').onclick = () => { OT.picked = new Set(OT.scenarios.map((s) => s.id)); refresh(); };

$('ot-arm').onclick = async () => {
  const first = await command('execution', '', { mode: 'LIVE' });
  if (first.ok) { refresh(); return; }
  if (!first.confirm) { toast('REJECT', 'Execution', first.error || 'refused'); return; }
  if (!await ask('Arm LIVE execution', first.text, 'Arm LIVE')) return;
  const done = await command('execution', '', { mode: 'LIVE', confirm: true });
  if (!done.ok) toast('REJECT', 'Execution', done.error || 'refused');
  refresh();
};

$('ot-run').onclick = async () => {
  const ids = OT.scenarios.map((s) => s.id).filter((id) => OT.picked.has(id));
  const contract = $('ot-contract').value;
  const qty = $('ot-qty').value;
  const yes = await ask('Run order tests on TT UAT',
    `${ids.join(', ')} on ${contract}, ${qty} lot(s) per order. These send REAL orders to TT UAT ` +
    '(and close or cancel them again). Nothing on another contract is touched.', 'Run');
  if (!yes) return;
  const r = await postJSON('/api/order-tests/run', {
    ids, contract, qty, away: $('ot-away').value, hit_wait: $('ot-wait').value, confirm: true });
  if (!r.data.ok) toast('REJECT', 'Order tests', r.data.error || 'refused');
  refresh();
};

$('ot-stop').onclick = async () => {
  const r = await postJSON('/api/order-tests/stop', {});
  toast(r.data.ok ? 'INFO' : 'REJECT', 'Order tests', r.data.text || r.data.error || '');
  refresh();
};

refresh();
setInterval(refresh, 1000);
