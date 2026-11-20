"""The ladder's rows are whole ticks.

An Increment of 0.01 on GC (tick 0.10) made a 3.20-wide book 320 rows
across: the bid and the offer were off the screen and the ladder looked as
if TT had stopped sending prices. A step below the tick is the tick; a step
of several ticks is kept (the control)."""
from tests.test_ui_browser import _launch, server, sync_playwright  # noqa: F401


def _row_step(url, increment):
    with sync_playwright() as p:
        browser = _launch(p)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.add_init_script(
            "localStorage.setItem('ft.ladder.fef', JSON.stringify({increment: %s}))"
            % increment)
        page.goto(url, wait_until='domcontentloaded')
        page.wait_for_selector('.ladderwin td.price')
        page.wait_for_timeout(800)
        prices = page.evaluate("""() => [...document.querySelectorAll('.ladderwin td.price')]
            .slice(0, 3).map((td) => parseFloat(td.textContent))""")
        browser.close()
    return round(abs(prices[0] - prices[1]), 6)


def test_a_step_finer_than_the_tick_is_the_tick(server):
    url, _ = server
    assert _row_step(url, 0.001) == 0.01            # the fixture's tick


def test_a_step_of_whole_ticks_is_kept(server):
    url, _ = server
    assert _row_step(url, 0.05) == 0.05
