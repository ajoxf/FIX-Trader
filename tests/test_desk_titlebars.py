"""A window's title bar is never out of reach, and an unchanged price on a
live feed is not called a dead one.

The desk scrolls sideways only, but the browser scrolled it DOWN when
something in a tall window was brought into view: the ladder's title bar —
drag, full screen, close — went under the banners with no scrollbar to bring
it back. And a ladder said STALE in red over a quiet UAT market while TT's
heartbeats were arriving, which reads as "the FIX feed is broken"."""
import json

from tests.test_ui_browser import _launch, server, sync_playwright  # noqa: F401


def _short_page(p, url, height=450):
    browser = _launch(p)
    page = browser.new_page(viewport={'width': 1000, 'height': height})
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_selector('.ladderwin .titlebar')
    page.wait_for_timeout(900)
    return browser, page


def _title_top(page):
    return page.evaluate("""() => [
      document.querySelector('.ladderwin .titlebar').getBoundingClientRect().top,
      document.getElementById('desktop').getBoundingClientRect().top,
      document.getElementById('desktop').scrollTop]""")


def test_bringing_a_ladders_foot_into_view_never_hides_its_title_bar(server):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    snap['engine']['book_complete'] = False          # the yellow banner is up
    (tmp / 'status.json').write_text(json.dumps(snap))
    with sync_playwright() as p:
        browser, page = _short_page(p, url)
        page.evaluate("() => document.querySelector('.ladderwin .ld-feed').scrollIntoView()")
        page.wait_for_timeout(700)
        title, desk, scroll = _title_top(page)
        assert scroll == 0 and title >= desk
        # Every window fits the desk: none reaches below it.
        bottoms = page.evaluate("""() => {
          const d = document.getElementById('desktop').getBoundingClientRect().bottom;
          return [...document.querySelectorAll('#desktop > .win')]
            .map((w) => w.getBoundingClientRect().bottom - d);
        }""")
        assert all(b <= 1 for b in bottoms), bottoms
        browser.close()


def _feed_text(server, md):
    url, tmp = server
    snap = json.loads((tmp / 'status.json').read_text())
    c = snap['contracts'][0]
    c['feed'] = {'age_sec': 70.9, 'stale': True, 'settling': False}
    c['md'] = md
    (tmp / 'status.json').write_text(json.dumps(snap))
    with sync_playwright() as p:
        browser, page = _short_page(p, url, 900)
        el = page.locator('.ladderwin .ld-feed')
        page.wait_for_function(
            "() => document.querySelector('.ladderwin .ld-feed').textContent !== '\u2014'")
        out = el.inner_text(), el.get_attribute('class')
        browser.close()
    return out


def test_an_unchanged_price_on_a_live_feed_is_said_as_unchanged(server):
    text, cls = _feed_text(server, {'state': 'LIVE', 'last_msg_sec': 70.9,
                                    'text': 'prices arriving from TT'})
    assert text == 'LIVE · unchanged 71s' and 'warn' in cls


def test_without_word_from_tt_it_is_still_stale(server):
    """The control: no proof the feed is live, and it stays STALE in red."""
    text, cls = _feed_text(server, None)
    assert text.startswith('STALE') and 'bad' in cls
