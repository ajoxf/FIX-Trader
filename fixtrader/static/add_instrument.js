/* Add instrument: ONE row of choices, TT's answer as a list, Add per row.
 *
 * The catalogue TT has answered is read from /api/instruments — its own
 * file, never the twice-a-second snapshot — only when the engine says it
 * changed. Adding saves the contract from TT's own definition on the one
 * venue (with more than one, the Exchanges form opens to choose it), turns
 * its Algo to SIGNALS, and asks the desk to open its ladder and its chart.
 */
'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const host = window.parent !== window ? window.parent : null;
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const pick = { exchange: 'CME', type: 'MLEG', product: '', expiry: '', filter: '' };
  try { Object.assign(pick, JSON.parse(localStorage.getItem('ft.addinst') || '{}'), { filter: '' }); } catch (e) { /* defaults */ }
  const remember = () => {
    try { localStorage.setItem('ft.addinst', JSON.stringify({ exchange: pick.exchange, type: pick.type, product: pick.product })); }
    catch (e) { /* forgets */ }
  };
  let frame = { version: null, catalogue: [], instruments: [] };
  let search = {}, onDesk = new Map(), asked = null, adding = false;

  // `CL BZ`, `cl/bz` and `CL|BZ` are one product: TT writes the
  // inter-commodity ones with a bar.
  const productOf = (v) => String(v || '').trim().toUpperCase()
    .replace(/[\s/:+,]+|-(?=[A-Z])/g, '|').replace(/^\|+|\|+$/g, '');
  const same = (a, b) => a === b || (a.includes('|') &&
    [...a.split('|')].sort().join('|') === [...b.split('|')].sort().join('|'));
  const month = (yyyymm) => {
    const s = String(yyyymm || '');
    return s.length >= 6 ? MONTHS[Number(s.slice(4, 6)) - 1] + ' ' + s.slice(0, 4) : (s || '—');
  };

  async function command(action, args) {
    const queued = await postJSON('/api/command', { action: 'terminal_' + action, contract: '', args });
    if (!queued.data.ok) throw new Error(queued.data.error || 'refused');
    for (let i = 0; i < 100; i += 1) {
      const r = await getJSON('/api/result/' + queued.data.id);
      if (!r.pending) {
        if (r.ok === false) throw new Error(r.error || 'refused');
        return r.data || r;
      }
      await new Promise((res) => setTimeout(res, 100));
    }
    throw new Error('the engine did not answer');
  }

  function matches() {
    const all = frame.catalogue || [];
    const onVenue = all.filter((i) => i.exchange === pick.exchange && i.security_type === pick.type);
    const inProduct = pick.product ? onVenue.filter((i) => same(i.product_key || i.symbol, pick.product)) : [];
    const tokens = pick.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = inProduct
      .filter((i) => !pick.expiry || (i.first_month || i.maturity || '').startsWith(pick.expiry))
      .filter((i) => {
        const hay = [i.tt_name, i.structure, i.description, i.display_name, i.security_id, month(i.first_month || i.maturity)].join(' ').toLowerCase();
        return tokens.every((t) => hay.includes(t));
      })
      .sort((a, b) => (a.leg_months || a.maturity || '').localeCompare(b.leg_months || b.maturity || '') ||
        (a.tt_name || '').localeCompare(b.tt_name || ''));
    return { all, onVenue, inProduct, rows };
  }

  function options(sel, values, chosen) {
    const el = $(sel);
    const sig = JSON.stringify(values);
    if (el.dataset.sig !== sig) {
      el.dataset.sig = sig;
      el.replaceChildren(...values.map(([v, label]) => new Option(label, v)));
    }
    el.value = chosen;
  }

  function render() {
    const { all, onVenue, inProduct, rows } = matches();
    options('ai-exchange', [...new Set(['CME', 'ICE', 'NYMEX', 'CBOT', 'COMEX', 'SGX', 'EUREX', pick.exchange, ...all.map((i) => i.exchange)])]
      .filter(Boolean).sort().map((v) => [v, v]), pick.exchange);
    $('ai-type').value = pick.type;
    if (document.activeElement !== $('ai-product')) $('ai-product').value = pick.product;
    const products = [...new Set(onVenue.map((i) => i.product_key || i.symbol))].sort();
    $('ai-products').replaceChildren(...products.map((p) => new Option(p)));
    const months = [...new Set(inProduct.map((i) => String(i.first_month || i.maturity || '').slice(0, 6)).filter(Boolean))].sort();
    options('ai-expiry', [['', 'Any'], ...months.map((m) => [m, month(m)])], months.includes(pick.expiry) ? pick.expiry : '');
    $('ai-search').textContent = pick.product ? 'Search TT for ' + pick.product : 'Search TT';

    const busy = asked && ['Searching', 'Receiving results'].includes(search.status) &&
      search.request_id === asked;
    const status = $('ai-status');
    let html;
    if (!pick.product) html = 'Choose the exchange, the type and the product, then <b>Search TT</b>.';
    else if (busy && !inProduct.length) html = 'Asking TT for <b>' + esc(pick.product) + '</b>… the rows appear as TT answers.';
    else if (!inProduct.length) html = '<b>' + esc(pick.product) + '</b> is not loaded yet for ' + esc(pick.exchange) + ' — press <b>Search TT for ' + esc(pick.product) + '</b> (or Enter).';
    else html = 'TT answered: <b>' + inProduct.length + ' ' + esc(pick.product) + '</b> ' +
      ($('ai-type').selectedOptions[0] || {}).textContent.toLowerCase() + (inProduct.length === 1 ? '' : 's') +
      ' on ' + esc(pick.exchange) + (rows.length !== inProduct.length ? ' · ' + rows.length + ' shown' : '') +
      '. Click <b>Add</b> on the one you want — its ladder opens on the desk.' + (busy ? ' (still receiving…)' : '');
    if (search.error && asked && search.request_id === asked) html += ' <span class="bad">TT: ' + esc(search.error) + '</span>';
    if (status.innerHTML !== html) status.innerHTML = html;

    const body = $('ai-rows');
    const sig = rows.map((i) => i.security_id + (onDesk.has(String(i.security_id)) ? '*' : '')).join('|');
    if (body.dataset.sig === sig) return;
    body.dataset.sig = sig;
    body.replaceChildren(...rows.slice(0, 500).map((i) => {
      const tr = document.createElement('tr');
      const desk = onDesk.get(String(i.security_id));
      tr.innerHTML = '<td class="nm"></td><td></td><td class="ds"></td><td></td><td class="id"></td><td class="r"></td><td class="r"></td><td class="act"></td>';
      const c = tr.children;
      c[0].textContent = i.tt_name || i.display_name || i.symbol;
      c[1].textContent = i.structure || '';
      c[2].textContent = i.description || '';
      c[3].textContent = month(i.first_month || i.maturity);
      c[4].textContent = i.security_id;
      c[5].textContent = i.tick_size || '—';
      c[6].textContent = i.tick_value ? '$' + Number(i.tick_value).toFixed(2) : '—';
      const b = document.createElement('button');
      b.type = 'button';
      if (desk) {
        b.className = 'btn on-desk'; b.textContent = 'On desk ✓';
        b.title = 'Already on the desk — click to bring its ladder forward';
        b.onclick = () => { if (host && host.revealContract) host.revealContract(desk); };
      } else {
        b.className = 'btn pri add'; b.textContent = 'Add';
        b.title = 'Put it on the desk: saved from TT\'s definition; its ladder opens with the Algo on SIGNALS';
        b.onclick = () => add(i, b);
      }
      c[7].append(b);
      return tr;
    }));
    if (rows.length > 500) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="8" class="more">' + rows.length + ' rows — type in Filter or choose an Expiry to narrow them</td>';
      body.append(tr);
    }
  }

  function esc(t) { const d = document.createElement('div'); d.textContent = String(t); return d.innerHTML; }

  async function searchTT() {
    const legs = pick.product.split('|').filter(Boolean);
    if (!legs.length) { $('ai-product').focus(); return; }
    // TT files `CL|BZ` as a product of its own, so that is asked first; then
    // each leg product, where the venue may file the spread. Later searches
    // APPEND, so the first answers are not thrown away.
    const symbols = legs.length > 1 ? [pick.product, ...legs] : legs;
    $('ai-search').disabled = true;
    try {
      for (const [n, symbol] of symbols.entries()) {
        const r = await command('search', { exchange: pick.exchange, security_type: pick.type, symbol, append: n > 0 });
        asked = r.request_id;
      }
    } catch (e) {
      $('ai-status').innerHTML = '<span class="bad">Not searched: ' + esc(e.message) + '</span>';
    } finally {
      $('ai-search').disabled = false;
    }
  }

  async function add(i, button) {
    if (adding) return;
    adding = true;
    button.disabled = true; button.textContent = 'Adding…';
    try {
      const venues = (await getJSON('/api/venues')).filter((v) => v.enabled !== false);
      if (venues.length !== 1) {
        // More than one venue: the trader chooses it, on the contract form.
        const q = new URLSearchParams({ prefill: '1', name: i.tt_name || i.display_name || i.symbol, symbol: i.symbol,
          security_id: i.security_id, security_exchange: i.exchange, tick_size: i.tick_size || '',
          tick_value: i.tick_value || '', contract_multiplier: i.point_value || '', currency: i.currency || '' });
        if (host && host.openTool) host.openTool('/exchanges?' + q); else location.href = '/exchanges?' + q;
        return;
      }
      const tick = String(i.tick_size || ''), dot = tick.indexOf('.');
      const body = { name: i.tt_name || i.display_name || i.symbol, symbol: i.symbol, security_id: i.security_id,
        security_exchange: i.exchange, venue: venues[0].name, enabled: true,
        decimals: tick ? (dot < 0 ? 2 : Math.max(2, tick.length - dot - 1)) : 4 };
      for (const [k, v] of [['tick_size', i.tick_size], ['tick_value', i.tick_value], ['contract_multiplier', i.point_value], ['currency', i.currency]]) {
        if (v !== undefined && v !== null && v !== '') body[k] = k === 'currency' ? v : Number(v);
      }
      const res = await postJSON('/api/contracts', body);
      if (!res.data.ok) throw new Error(res.data.error || 'refused');
      const key = res.data.key;
      onDesk.set(String(i.security_id), key);
      if (host && host.contractAdded) host.contractAdded(key, body.name);
      else toast('OK', 'ADDED', body.name + ' is on the desk.');
    } catch (e) {
      button.disabled = false; button.textContent = 'Add';
      toast('REJECT', 'NOT ADDED', e.message);
    } finally {
      adding = false;
      render();
    }
  }

  async function poll() {
    try {
      const snap = await getJSON('/api/snapshot');
      const term = ((snap.engine || {}).manual_terminal) || {};
      search = term.search || {};
      onDesk = new Map((snap.contracts || []).filter((c) => c.security_id).map((c) => [String(c.security_id), c.key]));
      // Contracts saved but not yet on the engine's desk count as on it.
      try {
        for (const c of await getJSON('/api/contracts')) if (c.security_id && !onDesk.has(String(c.security_id))) onDesk.set(String(c.security_id), c.key);
      } catch (e) { /* the engine's list is enough */ }
      if (term.catalogue_version !== undefined && term.catalogue_version !== frame.version) {
        const f = await getJSON('/api/instruments');
        if (f && f.version !== undefined) frame = f;
      }
      render();
    } catch (e) {
      $('ai-status').innerHTML = '<span class="bad">The engine is not answering — ' + esc(e.message) + '</span>';
    }
    setTimeout(poll, 1000);
  }

  $('ai-exchange').onchange = () => { pick.exchange = $('ai-exchange').value; pick.expiry = ''; remember(); render(); };
  $('ai-type').onchange = () => { pick.type = $('ai-type').value; pick.expiry = ''; remember(); render(); };
  $('ai-product').oninput = () => { pick.product = productOf($('ai-product').value); pick.expiry = ''; remember(); render(); };
  $('ai-product').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); searchTT(); } };
  $('ai-expiry').onchange = () => { pick.expiry = $('ai-expiry').value; render(); };
  $('ai-filter').oninput = () => { pick.filter = $('ai-filter').value; render(); };
  $('ai-search').onclick = searchTT;
  render();
  poll();
  $('ai-product').focus();
})();
