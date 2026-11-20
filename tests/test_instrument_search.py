"""Instruments are PULLED FROM THE EXCHANGE: a search goes to TT as a
Security Definition Request (35=c) by exchange (207), product (55) and type
(167), and what comes back is what TT lists — one Security Definition (35=d)
per instrument — never a list this program made up. The real runner, web app
and FIX sessions, against the fake TT; and the same search typed into the
Instruments window on the desk."""
import threading
import time

import pytest

from tests.test_uat_orders import open_desk


@pytest.fixture
def desk(tmp_path, monkeypatch):
    with open_desk(tmp_path, monkeypatch) as d:
        # What TT lists besides the configured CL1: another Crude spread, a
        # Crude future and an E-mini — the search must tell them apart.
        d.tt.list('CL2', 8510, 8512, tick=1, factor=0.01, symbol='CL',
                  maturity='202701', description='CL Jan27-Feb27 Calendar')
        d.tt.list('CLF7', 7000, 7001, tick=1, factor=0.01, symbol='CL',
                  security_type='FUT', maturity='202701', description='CL Jan27')
        d.tt.list('ES1', 600000, 600025, tick=25, factor=0.01, symbol='ES',
                  security_type='FUT', description='ES Dec26')
        yield d


def terminal(d):
    """The terminal's snapshot, with the search answers and the catalogue
    read where the screen reads them: their own file (/api/instruments),
    never the twice-a-second snapshot."""
    t = dict((d.d.snapshot().get('engine') or {}).get('manual_terminal') or {})
    frame = d.d._get('/api/instruments') or {}
    same = (frame.get('search') or {}).get('request_id') == (t.get('search') or {}).get('request_id')
    t['instruments'] = (frame.get('instruments') or []) if same else []
    t['catalogue'] = frame.get('catalogue') or []
    return t


def search(d, timeout=10, **args):
    res = d.d.command('terminal_search', '', args)
    assert res.get('ok'), res
    deadline = time.monotonic() + timeout
    found = []
    while time.monotonic() < deadline:
        t = terminal(d)
        found = sorted(i['security_id'] for i in t.get('instruments') or [])
        if (t.get('search') or {}).get('request_id') == res['request_id'] and found \
                and len(found) == t.get('instruments_count'):
            return t, found
        time.sleep(0.1)
    return terminal(d), found


def test_a_search_asks_tt_and_shows_what_tt_lists(desk):
    t, found = search(desk, exchange='CME', symbol='CL', security_type='MLEG')
    assert found == ['CL1', 'CL2']
    asked = [f for f in desk.tt.definition_requests if not f.get('48')][-1]
    assert (asked.get('207'), asked.get('55'), asked.get('167')) == ('CME', 'CL', 'MLEG')
    cl2 = next(i for i in t['instruments'] if i['security_id'] == 'CL2')
    assert cl2['description'] == 'CL Jan27-Feb27 Calendar'
    assert cl2['maturity'] == '202701'
    assert t['search']['status'] == 'Receiving results'
    # The catalogue the explorer reads keeps them.
    assert {'CL1', 'CL2'} <= {c['security_id'] for c in t.get('catalogue') or []}


def test_the_type_and_the_product_narrow_it_as_tt_does(desk):
    _, futures = search(desk, exchange='CME', symbol='CL', security_type='FUT')
    assert futures == ['CLF7']
    _, es = search(desk, exchange='CME', symbol='ES', security_type='FUT')
    assert es == ['ES1']


def test_a_product_tt_does_not_list_finds_nothing(desk):
    """Control for the two above: the answers come from TT, not the screen."""
    res = desk.d.command('terminal_search', '', {
        'exchange': 'CME', 'symbol': 'ZZ', 'security_type': 'FUT'})
    assert res.get('ok'), res
    time.sleep(3.0)
    assert terminal(desk).get('instruments') == []


def test_an_instrument_found_is_added_and_priced_in_trader_units(desk):
    search(desk, exchange='CME', symbol='CL', security_type='MLEG')
    assert desk.d.command('terminal_add', '', {'security_id': 'CL2'}).get('ok')
    deadline = time.monotonic() + 10
    quote = {}
    while time.monotonic() < deadline:
        rows = terminal(desk).get('watchlist') or []
        quote = next((r['quote'] for r in rows
                      if r['instrument']['security_id'] == 'CL2'), {})
        if quote.get('bid') is not None:
            break
        time.sleep(0.1)
    assert (quote.get('bid'), quote.get('ask')) == (85.10, 85.12), quote


def test_the_instruments_window_on_the_desk_pulls_them(desk):
    """The same search, typed into the Instruments window opened from the
    desk's + menu: TT's instruments appear in its results table."""
    pw = pytest.importorskip('playwright.sync_api')
    from werkzeug.serving import make_server
    from tests.test_ui_browser import _launch
    srv = make_server('127.0.0.1', 0, desk.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{srv.server_port}/',
                      wait_until='domcontentloaded')
            page.wait_for_selector('.win', timeout=10000)
            page.click('#add-panel')
            page.click('#add-menu [data-panel="__tool__instruments"]')
            win = page.locator('.win.toolwin[data-tool="instruments"]')
            win.wait_for(timeout=5000)
            frame = page.frame_locator('.win.toolwin[data-tool="instruments"] iframe')
            # No navigation of its own: the desk's taskbar is the navigation.
            frame.locator('#search-form').wait_for(timeout=10000)
            assert frame.locator('nav').count() == 0 or \
                not frame.locator('header.app-nav').is_visible()
            frame.locator('#search-form [name=exchange]').fill('CME')
            frame.locator('#search-form [name=symbol]').fill('CL')
            frame.locator('#search-form [name=security_type]').select_option('MLEG')
            frame.locator('#search-form button[type=submit]').click()
            results = frame.locator('#results')
            results.locator('text=CL2').wait_for(timeout=15000)
            text = results.inner_text()
            assert 'CL1' in text and 'CL2' in text and 'ES1' not in text
            assert errors == []
            browser.close()
    finally:
        srv.shutdown()


def test_a_contract_is_added_to_the_desk_in_one_step_and_its_ladder_appears(desk):
    """Exchanges -> Pick from TT -> exchange, type, product, instrument ->
    Add to the desk: the contract is SAVED from TT's own definition, the
    RUNNING engine takes it (no restart), the explorer's window closes and
    the new ladder comes forward on the desk with TT's prices on it."""
    pw = pytest.importorskip('playwright.sync_api')
    from werkzeug.serving import make_server
    from tests.test_ui_browser import _launch
    srv = make_server('127.0.0.1', 0, desk.app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={'width': 1500, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{srv.server_port}/', wait_until='domcontentloaded')
            page.wait_for_selector('.win.ladderwin', timeout=10000)
            page.click('#open-exchanges')
            ex = page.frame_locator('.win.toolwin[data-tool="exchanges"] iframe')
            ex.locator('#pick-contract').click()
            inst = page.frame_locator('.win.toolwin[data-tool="instruments"] iframe')
            inst.locator('#explore-dialog[open]').wait_for(timeout=10000)
            assert inst.locator('#explore-algo').is_disabled()       # nothing picked yet
            inst.locator('#explore-exchange-input').fill('CME')
            inst.locator('#explore-type').select_option('MLEG')
            inst.locator('#explore-product-input').fill('CL')
            inst.locator('#explore-search').click()
            inst.locator('#explore-contract option[value="CL2"]').wait_for(state='attached', timeout=15000)
            inst.locator('#explore-contract').select_option('CL2')
            inst.locator('#explore-algo').click()
            # Saved from TT's definition — nothing typed.
            deadline = time.monotonic() + 15
            saved = None
            while time.monotonic() < deadline and saved is None:
                rows = desk.d.client.get('/api/contracts').get_json()
                saved = next((r for r in rows if r.get('security_id') == 'CL2'), None)
                time.sleep(0.2)
            assert saved and saved['symbol'] == 'CL' and saved['venue'] == 'TT-UAT'
            assert float(saved['tick_size']) > 0
            # The running engine took it: its ladder is on the desk, with prices.
            key = saved['key']
            page.wait_for_selector(f'.win.ladderwin[data-key="__ladder__{key}"]', timeout=20000)
            page.wait_for_function(
                "(k) => (document.querySelector('.win[data-key=\"__ladder__' + k + '\"] .ld-bk-bid') || {}).textContent === '85.10'",
                arg=key, timeout=20000)
            # The explorer opened from Exchanges closed itself.
            page.wait_for_function(
                "() => !document.querySelector('.win.toolwin[data-tool=\"instruments\"]')", timeout=5000)
            snap = desk.d.snapshot()
            assert key not in ' '.join(snap['engine'].get('config_restart_needed') or [])
            assert errors == []
            browser.close()
    finally:
        srv.shutdown()


def test_thousands_of_definitions_never_ride_the_status_snapshot(desk):
    """A product with thousands of contracts made the twice-a-second status
    file megabytes; the engine stalled and Market Data dropped. The answers
    go to their own file, written when they change; the status stays small."""
    for i in range(2500):
        desk.tt.list(f'Q{i}', 9050, 9052, tick=1, factor=0.01, symbol='CL',
                     security_type='FUT', description=f'CL {i}')
    t, found = search(desk, timeout=20, exchange='CME', symbol='CL', security_type='FUT')
    assert len(found) >= 2000
    import os
    status = os.path.join(os.path.dirname(desk.d.client.application.config.get('STATUS_PATH', '')) or '.', 'x')
    snap = desk.d.snapshot()
    import json as _json
    assert len(_json.dumps(snap)) < 300_000
    assert 'instruments' not in snap['engine']['manual_terminal']
    assert 'catalogue' not in snap['engine']['manual_terminal']
