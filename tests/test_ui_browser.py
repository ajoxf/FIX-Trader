"""The UI, under a real browser, reading `pageerror`.

Python tests cannot see a temporal-dead-zone ReferenceError that aborts a
script block and silently unregisters a handler — and that is exactly what
happened in the system this is ported from. These do.

They skip cleanly where no browser is installed. Note the one trap: this page
polls twice a second, so `networkidle` NEVER fires — wait for
`domcontentloaded` and then for the thing you actually care about.
"""

import json
import os
import threading
from datetime import datetime, timezone

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")
from playwright.sync_api import sync_playwright  # noqa: E402

from fixtrader.webapp import create_app  # noqa: E402

#: Where this box keeps its browser. Playwright's own default is tried first.
BROWSER_CANDIDATES = [
    None,
    '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
]


def _launch(p):
    last = None
    for path in BROWSER_CANDIDATES:
        try:
            return p.chromium.launch(executable_path=path,
                                     args=['--no-sandbox'])
        except Exception as e:                       # noqa: BLE001
            last = e
    pytest.skip(f"no chromium available ({last})")


SNAPSHOT = {
    'engine': {
        'alive': True, 'loop_ms': 4.2, 'master_algo': True, 'killed': False,
        'environment': 'SIMULATED', 'simulated': True, 'book_complete': True,
        'unclaimed': [], 'refresh_sec': 0.5, 'sound': False,
        'confirm_close': True,
        'session': {'state': 'LOGGED_ON', 'text': 'simulator'},
        'notify': {'orders': False, 'fills': True, 'positions': True,
                   'rejects': True, 'withheld': True},
    },
    'contracts': [{
        'key': 'fef', 'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6',
        'venue': 'SIM', 'decimals': 4, 'tick_size': 0.01, 'state': 'IN',
        'algo_on': True,
        'market': {'bid': 0.48, 'ask': 0.49, 'bid_size': 25, 'ask_size': 25,
                   'mid': 0.485},
        'feed': {'age_sec': 0.2, 'stale': False, 'settling': False},
        'stats': {'mean': 0.5, 'std': 0.08, 'z': -0.19, 'buy_at': 0.34,
                  'sell_at': 0.66, 'hurst': 0.42, 'half_life': 18.0,
                  'samples': 120, 'need': 120, 'warm_pct': 100.0,
                  'is_warm': True, 'unresolved_touches': 0},
        'filters': {'edge_ratio': 2.1, 'edge_ok': True, 'blocked_by': None},
        'costs': {'round_trip_money': 19.0, 'round_trip_ticks': 0.38},
        'settings': {'entry_threshold': 2.0, 'stop_loss_z': 4.0, 'quantity': 5},
        'position': {'side': 'BUY', 'qty': 5, 'avg_price': 0.48,
                     'entry_z': -2.14, 'break_even': 0.518, 'target': 0.57,
                     'stop': 0.18, 'margin_locked': 1300.0,
                     'opened_at': datetime.now(timezone.utc).isoformat(),
                     'open_pnl': -20.0},
        'orders': [], 'pnl_today': 0.0, 'trades_today': 0,
        'last_event': 'BUY 5 @ 0.48', 'target_missing': None,
    }],
    'portfolio': {
        'rows': [{
            'key': 'fef', 'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6',
            'decimals': 4, 'side': 'BUY', 'qty': 5, 'avg_price': 0.48,
            'entry_z': -2.14, 'break_even': 0.518, 'target': 0.57,
            'stop': 0.18, 'opened_at': datetime.now(timezone.utc).isoformat(),
            'open_pnl': -20.0, 'margin_locked': 1300.0,
            'tickets': ['E000004', 'E000005'], 'mid': 0.485,
            'venue_qty': 5.0, 'venue_long': 5.0, 'venue_short': None,
            'venue_readable': True, 'both_sides_open': False, 'agrees': True,
        }],
        'venue_readable': True, 'open_pnl': -20.0, 'margin': 1300.0,
        'realised_today': 62.0, 'trades_today': 2,
    },
}


@pytest.fixture
def server(tmp_path):
    status = tmp_path / 'status.json'
    snap = dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())
    status.write_text(json.dumps(snap))
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}/", tmp_path
    srv.shutdown()


def open_page(p, url, errors):
    browser = _launch(p)
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
    page.on('console',
            lambda m: errors.append('console: ' + m.text) if m.type == 'error' else None)
    # NOT networkidle: the page polls twice a second and it never fires.
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_selector('.win', timeout=5000)
    return browser, page


def test_the_window_renders_every_field_without_a_page_error(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        assert page.locator('.contractwin').count() == 1
        assert page.locator('.state').inner_text() == 'IN'
        assert page.locator('.bidc .v').inner_text() == '0.4800'
        assert page.locator('.askc .v').inner_text() == '0.4900'
        assert page.locator('.f-z').inner_text() == '-0.19'
        assert page.locator('.f-buyat').inner_text() == '0.3400'
        assert page.locator('.f-sellat').inner_text() == '0.6600'
        # the position carries its own numbers
        assert page.locator('.p-zin').inner_text() == '-2.14'
        assert page.locator('.p-be').inner_text() == '0.5180'
        assert page.locator('.p-tgt').inner_text() == '0.5700'
        assert page.locator('.p-stop').inner_text() == '0.1800'
        assert '$1,300' in page.locator('.p-margin').inner_text()
        browser.close()
    assert errors == []


def test_a_missing_figure_renders_as_an_em_dash_never_as_zero(server):
    """The rule the whole system turns on, checked where the operator reads
    it: unmeasured is not zero."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['stats'].update({'z': None, 'hurst': None,
                                          'half_life': None, 'mean': None})
    snap['contracts'][0]['filters']['edge_ratio'] = None
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        assert page.locator('.f-z').inner_text() == '—'
        assert page.locator('.f-hurst').inner_text() == '—'
        assert page.locator('.f-hl').inner_text() == '—'
        assert page.locator('.f-mean').inner_text() == '—'
        assert '—' in page.locator('.f-edge').inner_text()
        browser.close()
    assert errors == []


def test_the_algo_switch_sends_a_command(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.locator('.sw').click()
        page.wait_for_timeout(400)
        lines = (tmp / 'commands.jsonl').read_text().strip().splitlines()
        assert json.loads(lines[-1])['action'] == 'algo_off'
        browser.close()
    assert errors == []


def test_close_now_asks_once_and_an_unanswered_prompt_means_no(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.locator('.close-now').click()
        page.wait_for_selector('#modal:not(.hidden)')
        page.locator('#modal-cancel').click()          # answered NO
        page.wait_for_timeout(300)
        assert not os.path.exists(tmp / 'commands.jsonl') or \
            'close_now' not in (tmp / 'commands.jsonl').read_text()
        # control: say yes and the command goes
        page.locator('.close-now').click()
        page.wait_for_selector('#modal:not(.hidden)')
        page.locator('#modal-confirm').click()
        page.wait_for_timeout(400)
        assert 'close_now' in (tmp / 'commands.jsonl').read_text()
        browser.close()
    assert errors == []


def test_there_are_no_native_dialogs_anywhere(server):
    """A native confirm() blocks the whole page and cannot be styled or
    tested. If one comes back, this fails the build."""
    url, _ = server
    errors, dialogs = [], []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.on('dialog', lambda d: (dialogs.append(d.message), d.dismiss()))
        page.locator('.close-now').click()
        page.wait_for_timeout(300)
        page.locator('#modal-cancel').click()
        page.locator('#kill').click()
        page.wait_for_timeout(300)
        browser.close()
    assert dialogs == []
    assert errors == []


def test_a_stale_quote_greys_the_prices_and_says_so(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['feed'] = {'age_sec': 41.2, 'stale': True,
                                    'settling': False}
    snap['contracts'][0]['state'] = 'HALTED'
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        assert 'stale' in (page.locator('.contractwin').get_attribute('class') or '')
        assert page.locator('.state').inner_text() == 'HALTED'
        # ...and the way OUT is still available
        assert page.locator('.close-now').is_enabled()
        browser.close()
    assert errors == []


def test_a_withheld_entry_says_why_on_the_window(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['contracts'][0]['position'] = None
    snap['contracts'][0]['state'] = 'BLOCKED'
    snap['contracts'][0]['filters']['blocked_by'] = \
        'edge 0.7x — sigma 0.42 against a round trip of 0.60'
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_timeout(900)
        text = page.locator('.f-blocked').inner_text()
        assert 'round trip' in text                # it names the numbers
        assert page.locator('.close-now').is_disabled()
        browser.close()
    assert errors == []


def test_eight_windows_fit_a_desk_without_the_body_scrolling_sideways(server):
    """The size budget: eight contracts on a 1920x1080 screen."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    one = snap['contracts'][0]
    snap['contracts'] = []
    for i in range(8):
        c = json.loads(json.dumps(one))
        c['key'] = f'c{i}'
        c['name'] = f'Contract {i}'
        snap['contracts'].append(c)
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1920, 'height': 1080})
        page.on('pageerror', lambda e: errors.append('pageerror: ' + str(e)))
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('.contractwin')
        page.wait_for_timeout(900)
        assert page.locator('.contractwin').count() == 8
        overflow = page.evaluate(
            "document.body.scrollWidth - document.body.clientWidth")
        assert overflow <= 0
        browser.close()
    assert errors == []


# -- the Positions window -------------------------------------------------

def test_the_positions_window_lists_every_open_position_with_its_tickets(server):
    """The screen that answers 'what am I in, across everything'."""
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.pv-rows tr')
        row = page.locator('.pv-rows tr').first.inner_text()
        for expected in ('Iron ore Oct/Nov', 'BUY', '0.4800', '-2.14',
                         '0.5180', '0.5700', '$1,300', 'E000004'):
            assert expected in row, f"{expected!r} missing from {row!r}"
        assert 'realised today' in page.locator('.pv-foot').inner_text()
        browser.close()
    assert errors == []


def test_both_sides_open_is_called_out_and_never_netted_to_flat(server):
    """Long and short at once is a close that went out as an open. Netting it
    to zero would show the desk as flat while it pays margin on both."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['portfolio']['rows'][0].update({
        'venue_qty': 0.0, 'venue_long': 5.0, 'venue_short': 5.0,
        'both_sides_open': True, 'agrees': False})
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.pv-rows tr')
        page.wait_for_timeout(900)
        row = page.locator('.pv-rows tr').first
        text = row.inner_text()
        assert 'LONG 5' in text and 'SHORT 5' in text
        assert 'hedged' in (row.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_an_unreadable_venue_says_so_instead_of_showing_an_empty_table(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['portfolio']['venue_readable'] = False
    snap['portfolio']['rows'][0]['venue_readable'] = False
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.pv-rows tr')
        page.wait_for_timeout(900)
        banner = page.locator('.pv-banner')
        assert not banner.is_hidden()
        assert 'NOT confirmation' in banner.inner_text()
        assert 'could not read' in page.locator('.pv-rows tr').first.inner_text()
        browser.close()
    assert errors == []


def test_closing_from_the_positions_window_asks_and_then_sends(server):
    url, tmp = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.pv-rows tr')
        page.locator('.pv-rows tr .btn').first.click()
        page.wait_for_selector('#modal:not(.hidden)')
        assert 'tickets' in page.locator('#modal-body').inner_text()
        page.locator('#modal-confirm').click()
        page.wait_for_timeout(400)
        sent = json.loads((tmp / 'commands.jsonl').read_text().strip().splitlines()[-1])
        assert sent['action'] == 'close_now' and sent['contract'] == 'fef'
        browser.close()
    assert errors == []
