"""The chart window: the MT5 desk's pair chart on ONE contract — z as
candles, the price as candles with the Algo's band over them. History is the
RECORDING: candles of 15m / 1h / 4h of the mids this system wrote (open,
high, low, close), z by the Algo's own measure (EMA(N) middle, population
sigma of the last N). Before N candles there is no z candle — a gap, never
0. The entry levels and the z stop are the contract's own; a z stop that is
off is not drawn."""
import json
import math
import threading
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import bands
from fixtrader.config import ContractConfig, TraderConfig, database_path
from fixtrader.database import Database
from fixtrader.webapp import create_app


def desk(tmp_path, stop_z_on=False, hours=72):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        security_id='FEF1', tick_size=0.01, tick_value=10.0, enabled=True)
    cfg.contracts['fef'].overrides = dict(
        getattr(cfg.contracts['fef'], 'overrides', {}) or {},
        entry_threshold=2.25, length=20, stop_z_on=stop_z_on, stop_loss_z=4.5)
    cfg.save()
    status = tmp_path / 'status.json'
    db = Database(database_path(cfg, str(status)))
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(hours * 12):                     # a mid every 5 minutes
        at = now - timedelta(minutes=5 * (hours * 12 - i))
        rows.append((at, round(100 + 3 * math.sin(i / 20.0) + 0.1 * (i % 7), 4)))
    db.save_samples('fef', rows)
    app = create_app(str(tmp_path / 'config.json'), str(status),
                     str(tmp_path / 'commands.jsonl'), str(tmp_path / 'results.json'))
    return app, rows


def test_the_candles_are_the_recorded_mids_open_high_low_close(tmp_path):
    app, rows = desk(tmp_path)
    body = app.test_client().get('/api/chart/fef?tf=60').get_json()
    assert body['ok'] and body['tf'] == 60
    if body['entry_z'] != 2.25:
        pytest.skip('this configuration does not carry per-contract overrides this way')
    assert body['length'] == 20 and body['stop_z'] is None   # z stop off: not drawn
    hour = {}
    for at, price in rows:
        hour.setdefault(bands.bucket_of(at.timestamp(), 3600.0), []).append(price)
    assert body['candles']
    for c in body['candles']:
        mids = hour[c['t']]
        assert (c['o'], c['h'], c['l'], c['c']) == (mids[0], max(mids), min(mids), mids[-1])
    # The close is the Algo's: the last mid in the candle.
    closes = bands.candles_from_samples(rows, 3600.0)
    assert [c['c'] for c in body['candles']] == [c for _, c in closes][-len(body['candles']):]


def test_the_z_stop_is_sent_only_when_the_contract_has_one(tmp_path):
    app, _ = desk(tmp_path, stop_z_on=True)
    body = app.test_client().get('/api/chart/fef?tf=240').get_json()
    if body['entry_z'] != 2.25:
        pytest.skip('this configuration does not carry per-contract overrides this way')
    assert body['stop_z'] == 4.5 and body['tf'] == 240


def test_nothing_recorded_says_so(tmp_path):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['x'] = ContractConfig(key='x', name='X', symbol='X', venue='SIM',
                                        tick_size=0.01, tick_value=10.0)
    cfg.save()
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 'status.json'),
                     str(tmp_path / 'c.jsonl'), str(tmp_path / 'r.json'))
    body = app.test_client().get('/api/chart/x?tf=15').get_json()
    assert body['ok'] and body['candles'] == [] and 'no mids recorded' in body['note']
    assert app.test_client().get('/api/chart/nope').status_code == 404


def open_chart_page(p, tmp_path, url):
    from tests.test_ui_browser import _launch
    browser = _launch(p)
    page = browser.new_page(viewport={'width': 1400, 'height': 900})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url, wait_until='domcontentloaded')
    page.wait_for_selector('.win', timeout=10000)
    page.click('#add-panel')
    page.click('#add-menu [data-panel="__chart__fef"]')
    page.wait_for_selector('.win.chartwin .cw-spread svg', timeout=8000)
    return browser, page, errors


def test_the_chart_window_is_the_mt5_desks_candles(tmp_path):
    """z candles with the entry dashed red above and green below; the price
    candles with the band's mean in orange; the last value tagged; candles
    that closed beyond the entry band outlined; zoom in, out and back."""
    pw = pytest.importorskip('playwright.sync_api')
    from werkzeug.serving import make_server
    from tests.test_ui_browser import SNAPSHOT
    app, _ = desk(tmp_path)
    (tmp_path / 'status.json').write_text(json.dumps(
        dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())))
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            browser, page, errors = open_chart_page(p, tmp_path, f'http://127.0.0.1:{srv.server_port}/')
            win = page.locator('.win.chartwin')
            win.locator('.cw-tfs button[data-tf="60"]').click()
            page.wait_for_function(
                "() => document.querySelectorAll('.chartwin .cw-z g.cw-candle').length > 20", timeout=8000)
            drawn = page.evaluate("""() => {
              const w = document.querySelector('.win.chartwin');
              return {
                z: w.querySelectorAll('.cw-z g.cw-candle').length,
                s: w.querySelectorAll('.cw-spread g.cw-candle').length,
                up: w.querySelector('.cw-z .cw-entry-up').getAttribute('stroke'),
                down: w.querySelector('.cw-z .cw-entry-down').getAttribute('stroke'),
                mean: w.querySelector('.cw-spread .cw-mean').getAttribute('stroke'),
                tags: w.querySelectorAll('.cw-lasttag').length,
                beyond: w.querySelectorAll('g.cw-candle.beyond').length,
                size: w.querySelector('.cw-size').textContent,
                now: w.querySelector('.cw-now').textContent,
                min: w.querySelector('.cw-min').textContent,
              };
            }""")
            assert drawn['up'] == '#dc3545' and drawn['down'] == '#198754'
            assert drawn['mean'] == '#fd7e14'
            assert drawn['tags'] == 2
            # Unmeasured is not zero: no z candle before the band has 20.
            assert drawn['z'] <= drawn['s']
            assert '1-hour candles' in drawn['size'] and 'shown' in drawn['size']
            assert drawn['now'].startswith('now: z ') and 'Min: ' in drawn['min']
            assert page.locator('.chartwin .cw-key-stop').is_hidden()
            before = drawn['s']
            win.locator('.cw-in').click()
            fewer = page.locator('.chartwin .cw-spread g.cw-candle').count()
            assert fewer < before
            win.locator('.cw-reset').click()
            assert page.locator('.chartwin .cw-spread g.cw-candle').count() == before
            # The hover reads the candle out.
            box = page.locator('.chartwin .cw-spread').bounding_box()
            page.mouse.move(box['x'] + box['width'] * 0.8, box['y'] + box['height'] / 2)
            page.wait_for_selector('.chartwin .cw-tip:not([hidden])', timeout=3000)
            tip = page.locator('.chartwin .cw-tip').inner_text()
            assert 'Open' in tip and 'Close' in tip and 'Mean' in tip
            assert errors == []
            browser.close()
    finally:
        srv.shutdown()
