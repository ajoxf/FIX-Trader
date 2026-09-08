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
    # `pageerror` is a real JavaScript exception and always a failure. A
    # console error is not: a 404 for a contract that is on the screen but not
    # in this fixture's configuration is a state the window handles in words.
    page.on('console', lambda m: errors.append('console: ' + m.text)
            if m.type == 'error' and 'Failed to load resource' not in m.text
            else None)
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


# -- the Analysis window ---------------------------------------------------

def with_analysis(tmp_path, report=None, desk=None):
    """A config and a database the analysis routes can actually read."""
    from fixtrader.config import ContractConfig, TraderConfig
    from fixtrader.database import Database
    from fixtrader.models import ExitReason, Position, Side, TouchEvent, TouchState
    from datetime import timedelta

    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'a.db')
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        quantity=5, commission_per_contract=1.0, slippage_budget_ticks=0.5,
        entry_threshold=2.0)
    cfg.save()

    db = Database(str(tmp_path / 'a.db'))
    base = datetime.now(timezone.utc) - timedelta(hours=4)
    for i in range(12):
        db.save_position(Position(
            contract_key='fef', side=Side.SELL, qty=0.0, opened_qty=5.0,
            avg_price=0.693, opened_at=base + timedelta(minutes=i * 10),
            closed_at=base + timedelta(minutes=i * 10 + 45),
            entry_z=2.14 + i * 0.01, exit_z=0.31, exit_price=0.6465,
            margin_locked=1300.0, exit_reason=ExitReason.TARGET,
            gross_pnl=60.0, fees_paid=19.0, net_pnl=41.0,
            pnl_pct_on_margin=3.15))
    # a level that reverts often but cannot pay, and one that can
    for i in range(8):
        db.save_touch(TouchEvent(contract_key='fef', ts=base, level=1.0,
                                 direction='UP', std=0.08,
                                 state=TouchState.REVERTED,
                                 seconds_to_revert=300.0, adverse_sigma=0.9))
    for i in range(6):
        db.save_touch(TouchEvent(contract_key='fef', ts=base, level=2.0,
                                 direction='UP', std=0.08,
                                 state=TouchState.REVERTED,
                                 seconds_to_revert=120.0, adverse_sigma=0.4,
                                 became_trade=(i < 3)))
    db.save_touch(TouchEvent(contract_key='fef', ts=base, level=3.0,
                             direction='UP', std=0.08,
                             state=TouchState.UNRESOLVED))

    # Fills carrying MEASURED slippage, so the costs panel has something to
    # compare the budget against — and one that could not be priced, which
    # must be counted separately rather than averaged in as zero.
    from fixtrader.models import Fill
    for i in range(6):
        db.save_fill(Fill(venue='SIM', exec_id=f'E{i:03d}', clordid='FT-1',
                          contract_key='fef', side=Side.SELL, qty=5.0,
                          price=0.693, fees=19.0,
                          our_ts=base + timedelta(minutes=i),
                          slippage_ticks=0.9))
    db.save_fill(Fill(venue='SIM', exec_id='E999', clordid='FT-1',
                      contract_key='fef', side=Side.SELL, qty=5.0,
                      price=0.693, our_ts=base, slippage_ticks=None))
    return cfg


@pytest.fixture
def analysis_server(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with_analysis(tmp_path)
    snap = dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())
    (tmp_path / 'status.json').write_text(json.dumps(snap))
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    from werkzeug.serving import make_server
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/", tmp_path
    srv.shutdown()


def open_analysis(p, url, errors, mode='live'):
    """The Analysis window, on a chosen mode.

    The window OPENS on what the engine is — a desk running the simulator
    shown "Live only" is shown an empty window for the session it just
    watched. The fixture's rows are live, so these tests say so.
    """
    browser, page = open_page(p, url, errors)
    page.wait_for_selector('.win[data-key="__analysis__"]')
    page.select_option('.an-mode', mode)
    page.wait_for_timeout(400)
    page.wait_for_selector('.an-tiles .tile')
    return browser, page


def test_the_analysis_window_shows_the_tiles_and_the_journal(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        assert page.locator('.an-tiles .tile').count() == 9
        tiles = page.locator('.an-tiles').inner_text()
        assert '12' in tiles                       # trades
        assert '100.0%' in tiles                   # win rate
        assert page.locator('.an-journal tbody tr').count() == 12
        # the journal carries the z each decision fired at
        assert '+2.14' in page.locator('.an-journal').inner_text()
        browser.close()
    assert errors == []


def test_a_level_that_cannot_cover_its_costs_is_shown_but_marked(analysis_server):
    """It reverts more often than any other — and it is the one that cannot
    pay for the trade. Both facts have to be on the screen at once."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        rows = page.locator('.an-touches tbody tr')
        assert rows.count() >= 2
        # The unmeasured fill must be counted, not averaged in as zero.
        assert 'unmeasured' in page.locator('.an-costs').inner_text()
        finding = page.locator('.an-touch-finding').inner_text()
        assert 'covers the round trip' in finding or 'No level covers' in finding
        browser.close()
    assert errors == []


def test_an_unresolved_touch_is_reported_separately_on_the_screen(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(800)
        assert 'unresolved' in page.locator('.an-foot').inner_text()
        browser.close()
    assert errors == []


def test_the_cost_finding_offers_a_correction_and_does_not_apply_it(analysis_server):
    """Nothing in this system applies its own findings."""
    url, tmp = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(900)
        finding = page.locator('.an-cost-finding')
        assert not finding.is_hidden()
        text = finding.inner_text()
        # It names both numbers and proposes the measured one.
        assert '0.50' in text and '0.90' in text
        assert 'Set the budget to 0.90' in text
        # ...and the contract still holds what it was configured with.
        from fixtrader.config import TraderConfig
        assert TraderConfig.from_file(str(tmp / 'config.json')) \
            .contracts['fef'].overrides['slippage_budget_ticks'] == 0.5

        # pressing it applies the correction, once, deliberately
        page.locator('.an-cost-finding button').click()
        page.wait_for_timeout(900)
        assert TraderConfig.from_file(str(tmp / 'config.json')) \
            .contracts['fef'].overrides['slippage_budget_ticks'] == 0.9
        browser.close()
    assert errors == []


def test_the_all_contracts_tab_withholds_a_verdict_on_thin_evidence(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.locator('.an-tabs button', has_text='All contracts').click()
        page.wait_for_selector('.an-desk-table tbody tr')
        page.wait_for_timeout(600)
        text = page.locator('.an-desk').inner_text()
        assert 'too few to judge' in text
        assert 'blended' in text                   # the total row says so
        browser.close()
    assert errors == []


def test_simulated_trades_are_not_shown_in_a_live_figure(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        page.wait_for_timeout(700)
        assert page.locator('.an-journal tbody tr').count() == 12
        page.select_option('.an-mode', 'sim')      # a chosen filter stands
        page.wait_for_timeout(900)
        # the fixture's trades are all live, so simulated-only is empty
        assert 'no closed trades' in page.locator('.an-journal').inner_text()
        browser.close()
    assert errors == []


# -- windows that overlap --------------------------------------------------

def overlap_the_windows(page):
    """Put Analysis squarely on top of Positions, as a desk ends up after a
    few drags."""
    page.evaluate("""() => {
      document.getElementById('desktop').classList.add('free');
      const place = (k, x, y) => {
        const e = document.querySelector('.win[data-key="' + k + '"]');
        if (!e) return;
        e.classList.add('placed'); e.style.left = x + 'px'; e.style.top = y + 'px';
      };
      place('__positions__', 0, 0);
      place('__analysis__', 430, 0);
      document.querySelectorAll('.win.contractwin').forEach((e) => {
        e.classList.add('placed'); e.style.left = '0px'; e.style.top = '900px';
      });
    }""")
    page.wait_for_timeout(400)


def test_a_sticky_table_header_does_not_punch_through_the_window_above_it(analysis_server):
    """A child with a z-index is placed against the whole page unless its
    window is its own stacking context. The Positions column headers were
    painting straight across the middle of the Analysis window."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        overlap_the_windows(page)
        covered = page.evaluate("""() => {
          const pos = document.querySelector('.win[data-key="__positions__"]');
          const an = document.querySelector('.win[data-key="__analysis__"]');
          const anR = an.getBoundingClientRect();
          // a header cell that genuinely lies underneath the Analysis window
          const cell = Array.from(pos.querySelectorAll('table.grid th'))
            .map((t) => ({t, r: t.getBoundingClientRect()}))
            .find((o) => o.r.x > anR.x + 20 && o.r.y > anR.y && o.r.bottom < anR.bottom);
          if (!cell) return {found: false};
          const top = document.elementFromPoint(
            Math.round(cell.r.x + cell.r.width / 2),
            Math.round(cell.r.y + cell.r.height / 2));
          return {found: true, punchesThrough: pos.contains(top),
                  coveredByAnalysis: an.contains(top)};
        }""")
        assert covered['found'], "the windows did not overlap; nothing was tested"
        assert covered['punchesThrough'] is False
        assert covered['coveredByAnalysis'] is True
        browser.close()
    assert errors == []


def test_clicking_a_window_brings_it_to_the_front(analysis_server):
    """A desk of draggable windows with a fixed paint order has one you can
    never read."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_analysis(p, url, errors)
        overlap_the_windows(page)
        point = page.evaluate("""() => {
          const bar = document.querySelector('.win[data-key="__positions__"] .titlebar')
            .getBoundingClientRect();
          return [Math.round(bar.x + 30), Math.round(bar.y + 7)];
        }""")
        page.mouse.click(point[0], point[1])
        page.wait_for_timeout(300)
        after = page.evaluate("""() => {
          const pos = document.querySelector('.win[data-key="__positions__"]');
          const r = pos.getBoundingClientRect();
          const top = document.elementFromPoint(Math.round(r.right - 100),
                                                Math.round(r.y + 50));
          return {raised: pos.classList.contains('raised'),
                  onTop: pos.contains(top)};
        }""")
        assert after['raised'] is True
        assert after['onTop'] is True
        browser.close()
    assert errors == []


# -- one contract's settings, behind the gear ------------------------------

def open_config(page):
    page.locator('.contractwin .cog').click()
    page.wait_for_selector('.cfgwin')
    page.wait_for_selector('.cfgwin .cf-row')
    return page.locator('.cfgwin')


def test_the_gear_opens_this_contracts_settings(analysis_server):
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        assert cfg.locator('.title').inner_text() == 'Iron ore Oct/Nov'
        # its OWN setting is in the box, and marked as its own
        box = cfg.locator('#cf-entry_threshold')
        assert box.input_value() == '2'
        assert 'own' in (cfg.locator('.cf-row:has(#cf-entry_threshold) .cf-eff')
                         .get_attribute('class'))
        # a field it does not set is BLANK, with the desk default in grey
        blank = cfg.locator('#cf-stop_loss_z')
        assert blank.input_value() == ''
        eff = cfg.locator('.cf-row:has(#cf-stop_loss_z) .cf-eff')
        assert eff.inner_text() == '4'
        assert 'own' not in (eff.get_attribute('class') or '')
        browser.close()
    assert errors == []


def test_a_blank_box_clears_an_override_and_zero_sets_one(analysis_server):
    """The rule the panel turns on: blank means 'use the desk default', and
    0 is a real number. Confusing the two is how a contract quietly ends up
    with no cooldown, or with a target of break-even."""
    from fixtrader.config import TraderConfig
    url, tmp = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        cfg.locator('#cf-entry_threshold').fill('')       # back to the default
        cfg.locator('#cf-stop_loss_z').fill('0')          # a real number
        cfg.locator('.cfg-save').click()
        page.wait_for_timeout(600)

        saved = TraderConfig.from_file(str(tmp / 'config.json'))
        overrides = saved.contracts['fef'].overrides
        assert overrides.get('entry_threshold') is None
        assert overrides.get('stop_loss_z') == 0
        # and the panel now reads back what was actually saved
        assert cfg.locator('#cf-entry_threshold').input_value() == ''
        assert cfg.locator('#cf-stop_loss_z').input_value() == '0'
        browser.close()
    assert errors == []


def test_every_group_of_settings_renders(analysis_server):
    """The comprehensiveness is the point — a desk that cannot set a
    contract's own costs trades eight instruments on one's assumptions."""
    url, _ = analysis_server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        cfg = open_config(page)
        for group, probe in [('Filters', '#cf-min_std_multiple'),
                             ('Size & risk', '#cf-max_position'),
                             ('Execution', '#cf-exit_on_timeout'),
                             ('Costs', '#cf-profit_target_pct'),
                             ('Display', '#cf-decimals')]:
            cfg.locator('.cfg-tabs button', has_text=group).click()
            page.wait_for_selector('.cfgwin ' + probe)
        browser.close()
    assert errors == []


def test_a_setting_that_is_saved_but_not_in_force_says_so(server):
    """A setting that looks saved and is not is worse than one that plainly
    needs the engine bounced. The engine names them; the banner reads them."""
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine']['config_restart_needed'] = ['fef.tick_value', 'DATABASE_PATH']
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('#restart-banner:not(.hidden)')
        text = page.locator('#restart-banner').inner_text()
        assert 'fef.tick_value' in text and 'DATABASE_PATH' in text
        # the control: nothing waiting, nothing shown
        snap['engine']['config_restart_needed'] = []
        (tmp / 'status.json').write_text(json.dumps(snap))
        page.wait_for_function(
            "document.getElementById('restart-banner')"
            ".classList.contains('hidden')")
        browser.close()
    assert errors == []
