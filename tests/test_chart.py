"""The chart window: W3's charts, on the FIX desk. History is the
RECORDING — candles of 15m / 1h / 4h built from the mids this system wrote,
with z the Algo's own measure (EMA(N) middle, population sigma of the last
N). Before N candles there is no z: None, never 0. The entry levels and the
z stop are the contract's own; a z stop that is off is not drawn."""
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


def test_history_is_candles_of_the_recorded_mids_with_the_algos_z(tmp_path):
    app, rows = desk(tmp_path)
    if app.test_client().get('/api/chart/fef?tf=60').get_json().get('entry_z') != 2.25:
        pytest.skip('this configuration does not carry per-contract overrides this way')
    body = app.test_client().get('/api/chart/fef?tf=60').get_json()
    assert body['ok'] and body['tf'] == 60 and body['length'] == 20
    pts = body['points']
    closes = bands.candles_from_samples(rows, 3600.0)
    assert len(pts) == min(150, len(closes))
    # Each point closes on the LAST mid recorded in its hour.
    assert [p['mid'] for p in pts] == [c for _, c in closes][-len(pts):]
    # z, independently: EMA(20) seeded with the SMA, population sigma.
    vals = [c for _, c in closes]
    ema, zs = None, []
    for i, v in enumerate(vals):
        if i + 1 < 20:
            zs.append(None)
            continue
        ema = sum(vals[:20]) / 20 if ema is None else (2 / 21) * v + (1 - 2 / 21) * ema
        win = vals[i - 19:i + 1]
        m = sum(win) / 20
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / 20)
        zs.append(round((v - ema) / sd, 3))
    assert [p['z'] for p in pts] == zs[-len(pts):]
    assert body['stop_z'] is None                  # the z stop is off: not drawn


def test_before_n_candles_there_is_no_z_never_zero(tmp_path):
    app, _ = desk(tmp_path, hours=10)
    body = app.test_client().get('/api/chart/fef?tf=60').get_json()
    assert body['ok'] and body['points']
    assert all(p['z'] is None for p in body['points'])   # 10 hours < 20 candles


def test_the_z_stop_is_drawn_only_when_the_contract_has_one(tmp_path):
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
    assert body['ok'] and body['points'] == [] and 'no mids recorded' in body['note']
    assert app.test_client().get('/api/chart/nope').status_code == 404


def test_the_chart_window_draws_w3s_lines(tmp_path):
    """Opened from the + menu: the z line in W3's blue, the entry levels
    dashed green, and — on 1h — the recorded history."""
    pw = pytest.importorskip('playwright.sync_api')
    from werkzeug.serving import make_server
    from tests.test_ui_browser import SNAPSHOT, _launch
    app, _ = desk(tmp_path)
    (tmp_path / 'status.json').write_text(json.dumps(
        dict(SNAPSHOT, ts=datetime.now(timezone.utc).isoformat())))
    srv = make_server('127.0.0.1', 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pw.sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{srv.server_port}/', wait_until='domcontentloaded')
            page.wait_for_selector('.win', timeout=10000)
            page.click('#add-panel')
            page.click('#add-menu [data-panel="__chart__fef"]')
            page.wait_for_selector('.win.chartwin canvas.ch-z', timeout=5000)
            page.wait_for_function("() => (chartData.fef.live || []).length >= 1", timeout=8000)
            live = page.evaluate("""() => {
              const z = chartData.fef.charts.z;
              return {colour: z.data.datasets[0].borderColor,
                      entry: z.data.datasets[1].data[0], dash: z.data.datasets[1].borderDash,
                      price: chartData.fef.charts.p.data.datasets[0].borderColor,
                      ymin: z.options.scales.y.min, ymax: z.options.scales.y.max};
            }""")
            assert live['colour'] == '#0d6efd' and live['price'] == '#6f42c1'
            assert live['entry'] == 2.0 and live['dash'] == [5, 5]
            assert (live['ymin'], live['ymax']) == (-5, 5)
            page.click('.win.chartwin .ch-tf[data-tf="60"]')
            page.wait_for_function(
                "() => chartData.fef.hist && chartData.fef.charts.z.data.labels.length > 20",
                timeout=8000)
            assert errors == []
            browser.close()
    finally:
        srv.shutdown()
