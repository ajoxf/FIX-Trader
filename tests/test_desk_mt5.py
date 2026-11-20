"""The MT5 desk's Market Grid and keys, on the FIX desk. Neither is an order
path of its own: a grid click and a key both go THROUGH the contract's
ladder — `terminal_preview`, a review, then the trader's word — and both are
refused while that contract's Algo trades it (each with its control)."""
import json

from tests.test_ui_browser import manual_desk, open_page, server, sync_playwright  # noqa: F401


def last_command(tmp):
    log = tmp / 'commands.jsonl'
    if not log.exists():
        return None
    lines = log.read_text().strip().splitlines()
    return json.loads(lines[-1]) if lines else None


def open_grid(page):
    page.click('#add-panel')
    page.click('#add-menu [data-panel="__grid__"]')
    page.wait_for_selector('.win.gridwin tbody tr[data-key="fef"] td.mg-bid')


def test_a_grid_click_on_bid_reviews_a_buy_at_the_bid_through_the_ladder(server):
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        open_grid(page)
        page.click('.win.gridwin td.mg-bid')
        page.wait_for_timeout(600)
        sent = last_command(tmp)
        assert sent and sent['action'] == 'terminal_preview'
        bid = json.loads((tmp / 'status.json').read_text())['contracts'][0]['market']['bid']
        assert sent['args']['side'] == 'BUY' and float(sent['args']['price']) == bid
        assert sent['args']['open_close'] == 'O'
        browser.close()


def test_a_grid_click_while_the_algo_trades_sends_nothing_and_says_why(server):
    """The control for the test above."""
    url, tmp = server
    manual_desk(tmp, mode='ALGO')
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .ld-grid tbody tr')
        open_grid(page)
        page.click('.win.gridwin td.mg-ask')
        page.wait_for_timeout(500)
        sent = last_command(tmp)
        assert not sent or sent['action'] != 'terminal_preview'
        assert 'Algo is trading it' in page.locator('#toasts').inner_text()
        browser.close()


def test_the_b_key_is_the_buy_button_and_never_fires_from_a_field(server):
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        # Typed into a box: a figure, never an order.
        page.locator('.ladderwin .ld-qtybox.buy').click()
        page.keyboard.press('b')
        page.wait_for_timeout(400)
        assert not last_command(tmp)
        page.locator('.ladderwin .titlebar .title').click()
        page.keyboard.press('3')                    # arm 10
        assert page.locator('.ladderwin .ld-qtybox.buy').input_value() == '10'
        page.keyboard.press('b')
        page.wait_for_timeout(600)
        sent = last_command(tmp)
        assert sent and sent['action'] == 'terminal_preview'
        assert sent['args']['side'] == 'BUY' and sent['args']['quantity'] == '10'
        browser.close()


def test_keys_off_means_off(server):
    """The control: with the Keys button off, B sends nothing."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        page.click('#keys-toggle')
        assert page.locator('#keys-toggle').inner_text() == 'Keys: off'
        page.locator('.ladderwin .titlebar .title').click()
        page.keyboard.press('b')
        page.wait_for_timeout(500)
        assert not last_command(tmp)
        page.keyboard.press('?')
        assert page.locator('#help-modal').count() == 0 or \
            not page.locator('#help-modal').is_visible()
        browser.close()


def test_question_mark_lists_the_keys_and_escape_closes_it(server):
    url, _ = server
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.keyboard.press('?')
        page.wait_for_selector('#help-modal:not(.hidden)')
        text = page.locator('#help-modal').inner_text()
        for k in ('B', 'S', 'F', 'X', '1 2 3 4 5', 'Tab', 'Esc'):
            assert k in text
        page.keyboard.press('Escape')
        assert not page.locator('#help-modal').is_visible()
        browser.close()
    assert errors == []


SPY = """() => {
  window.__sent = [];
  window.command = async (action, contract, args) => {
    window.__sent.push(action);
    if (action === 'terminal_preview') {
      return {ok: true, token: 'T1', ticket: {quantity: '5', price: '0.49',
              order_type: 'LIMIT', tif: 'DAY', account: 'ACC1'}};
    }
    if (action === 'terminal_submit') return {ok: true, order_id: 'FTM-1'};
    return {ok: true};
  };
}"""


def test_one_click_is_one_order_as_on_the_mt5_desk(server):
    """BUY: the engine's review (limits, the Algo's lock), then straight to
    TT — no dialog, as on the MT5 desk."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        page.evaluate(SPY)
        page.locator('.ladderwin .ld-buy').click()
        page.wait_for_function("() => window.__sent.length >= 2")
        assert page.evaluate("window.__sent") == ['terminal_preview', 'terminal_submit']
        assert page.locator('#modal').is_hidden()
        browser.close()


def test_ask_before_sending_puts_the_yes_back(server):
    """The control: with the desk's 'ask before sending' on, the same click
    stops at the dialog and nothing is submitted until the trader says yes."""
    url, tmp = server
    manual_desk(tmp)
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine']['confirm_clicks'] = True
    (tmp / 'status.json').write_text(json.dumps(snap))
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin.manual-on')
        page.evaluate(SPY)
        page.locator('.ladderwin .ld-buy').click()
        page.wait_for_selector('#modal:not(.hidden)')
        assert page.evaluate("window.__sent") == ['terminal_preview']
        page.locator('#modal-confirm').click()
        page.wait_for_function("() => window.__sent.length >= 2")
        assert page.evaluate("window.__sent") == ['terminal_preview', 'terminal_submit']
        browser.close()


def test_every_window_goes_full_screen_and_comes_back(server):
    """The ⛶ button (and a double click on the title bar) fills the desk;
    the same again puts the window back where it was, at its size."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin .winbtn.max')
        box = lambda: page.evaluate(
            "(()=>{const e=document.querySelector('.ladderwin');"
            "const d=document.getElementById('desktop');"
            "return [e.offsetWidth,e.offsetHeight,d.clientWidth,d.clientHeight]})()")
        w0, h0, dw, dh = box()
        assert w0 < dw
        page.click('.ladderwin .winbtn.max')
        w1, h1, _, _ = box()
        assert (w1, h1) == (dw, dh)
        page.click('.ladderwin .winbtn.max')
        assert box()[:2] == [w0, h0]
        page.dblclick('.ladderwin .titlebar .title')
        assert box()[:2] == [dw, dh]
        page.dblclick('.ladderwin .titlebar .title')
        assert box()[:2] == [w0, h0]
        assert not errors
        browser.close()


def test_a_page_window_opens_at_the_size_of_what_it_shows(server):
    """Exchanges is a short form: its window is as tall as the form, not a
    fixed box of empty desk — and the page inside has no navigation strip."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin')
        page.click('#open-exchanges')
        frame = page.frame_locator('.win.toolwin[data-tool="exchanges"] iframe')
        frame.locator('#pick-contract').wait_for()
        page.wait_for_timeout(2500)
        win_h, desk_h = page.evaluate(
            "(()=>[document.querySelector('.win.toolwin[data-tool=exchanges]').offsetHeight,"
            "document.getElementById('desktop').clientHeight])()")
        assert win_h < desk_h - 100
        assert not frame.locator('header.app-nav').is_visible()
        browser.close()


def test_exchanges_shows_a_contract_added_while_it_is_open(server):
    """Add instrument saves a contract while Exchanges stays open: the list
    there takes it without a reload."""
    url, tmp = server
    manual_desk(tmp)
    errors = []
    with sync_playwright() as p:
        browser, page = open_page(p, url, errors)
        page.wait_for_selector('.ladderwin')
        page.click('#open-exchanges')
        frame = page.frame_locator('.win.toolwin[data-tool="exchanges"] iframe')
        frame.locator('#pick-contract').wait_for()
        page.wait_for_timeout(1000)
        before = frame.locator('#contract-rows tr').count()
        res = page.request.post(url.rstrip('/') + '/api/contracts',
                                data={'symbol': 'GC', 'name': 'GC Feb27',
                                      'security_id': '27055936'})
        assert res.json()['ok']
        frame.locator('#contract-rows tr:has-text("GC Feb27")').wait_for(timeout=8000)
        assert frame.locator('#contract-rows tr').count() == before + 1
        browser.close()
