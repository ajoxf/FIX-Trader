from types import SimpleNamespace

from fixtrader.commands import apply_command
from fixtrader.config import TraderConfig, VenueConfig
from fixtrader.gateway import SessionState
from fixtrader.webapp import create_app


def test_tt_landing_page_is_connection_dashboard_and_desk_is_available(tmp_path):
    path = tmp_path / 'config.json'
    cfg = TraderConfig(path=str(path))
    cfg.venues['TT-UAT'] = VenueConfig(name='TT-UAT', host='example.invalid', fix_version='FIX.4.2')
    cfg.save()
    client = create_app(str(path)).test_client()
    for route in ('/', '/connection'):
        page = client.get(route)
        assert page.status_code == 200
        assert b'FIX connection dashboard' in page.data
        assert b'id="connect"' in page.data
        assert b'id="disconnect"' in page.data
        assert b'id="reconnect"' in page.data
    assert b'contract-template' in client.get('/desk').data
    instruments = client.get('/instruments')
    assert instruments.status_code == 200
    for element in ('explore-dialog', 'explore-exchange', 'explore-type', 'explore-product', 'explore-contract', 'ladders', 'review-dialog'):
        assert ('id="' + element + '"').encode() in instruments.data


def test_controls_execute_only_on_the_engine_owned_venue():
    calls = []
    gateway = SimpleNamespace(venue=SimpleNamespace(name='TT-UAT'),
        start=lambda: calls.append('start'), stop=lambda: calls.append('stop'),
        reconnect=lambda: calls.append('reconnect'),
        state=lambda: SessionState.DOWN, diagnose=lambda: [])
    engine = SimpleNamespace(gateway=gateway, simulated=False)
    for action in ('fix_connect', 'fix_disconnect', 'fix_reconnect'):
        assert apply_command(engine, {'action': action, 'args': {'venue': 'TT-UAT'}})['ok']
    assert calls == ['start', 'stop', 'reconnect']
    result = apply_command(engine, {'action': 'fix_disconnect', 'args': {'venue': 'other'}})
    assert result['ok'] is False
    assert calls == ['start', 'stop', 'reconnect']
