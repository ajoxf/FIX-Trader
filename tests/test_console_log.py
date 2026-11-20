"""The console says what HAPPENED: the page's own polling is not logged,
what the trader did and what failed is — one short line each."""
import io
import logging

from fixtrader.webapp import QuietAccessLog


def run(lines):
    buf = io.StringIO()
    logger = logging.getLogger('test.werkzeug.quiet')
    logger.handlers[:] = []
    logger.filters[:] = []
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addFilter(QuietAccessLog())
    for line in lines:
        logger.info(line)
    return buf.getvalue().splitlines()


def req(method, path, code):
    return f'127.0.0.1 - - [20/Nov/2026 14:00:00] "{method} {path} HTTP/1.1" {code} -'


def test_the_pages_polling_is_not_logged():
    assert run([req('GET', '/api/snapshot', 200), req('GET', '/api/contracts', 200),
                req('GET', '/static/app.js?v=3', '\x1b[36m304\x1b[0m'),
                'WARNING: This is a development server. Do not use it.']) == []


def test_what_the_trader_did_and_what_failed_is_kept_in_one_short_line():
    """The control: the filter does not swallow everything."""
    out = run([req('POST', '/api/command', 200), req('GET', '/api/chart/cl?tf=15', 500),
               ' * Running on http://127.0.0.1:8765'])
    assert out == ['INFO POST /api/command -> 200', 'WARNING GET /api/chart/cl -> 500',
                   'INFO  * Running on http://127.0.0.1:8765']
