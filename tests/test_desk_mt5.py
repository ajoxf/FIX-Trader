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
