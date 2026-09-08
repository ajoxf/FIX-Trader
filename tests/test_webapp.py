"""The web process renders and it asks. It must never be able to trade, and
it must never hand out a secret."""
import json

import pytest

from fixtrader.config import TraderConfig, VenueConfig
from fixtrader.webapp import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    monkeypatch.setenv('FIX_ORIENT_UAT', 'hunter2')
    cfg.venues['orient'] = VenueConfig('Orient UAT', environment='UAT',
                                       password_env='FIX_ORIENT_UAT',
                                       host='h', port=1)
    cfg.save()
    app = create_app(str(tmp_path / 'config.json'), str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'),
                     str(tmp_path / 'results.json'))
    app.config['TESTING'] = True
    return app.test_client(), tmp_path


def write_status(tmp_path, snap):
    (tmp_path / 'status.json').write_text(json.dumps(snap))


def test_the_page_renders(client):
    c, _ = client
    body = c.get('/').data.decode()
    assert 'NEXUS FIX' in body
    assert 'contract-template' in body


def test_no_snapshot_yet_says_so_rather_than_showing_a_dead_screen(client):
    c, _ = client
    data = c.get('/api/snapshot').get_json()
    assert data['engine']['alive'] is False
    assert 'starting' in data['engine']['text']


def test_a_snapshot_that_has_stopped_moving_is_a_dead_engine(client):
    """The confusion this prevents is the expensive one: a dead engine read
    as a quiet market."""
    c, tmp = client
    write_status(tmp, {'ts': '2020-01-01T00:00:00+00:00',
                       'engine': {'alive': True, 'refresh_sec': 0.5},
                       'contracts': []})
    data = c.get('/api/snapshot').get_json()
    assert data['engine']['alive'] is False
    assert 'not live' in data['engine']['text']


def test_a_fresh_snapshot_is_passed_through_alive(client):
    from datetime import datetime, timezone
    c, tmp = client
    write_status(tmp, {'ts': datetime.now(timezone.utc).isoformat(),
                       'engine': {'alive': True, 'refresh_sec': 0.5},
                       'contracts': [{'key': 'fef'}]})
    data = c.get('/api/snapshot').get_json()
    assert data['engine']['alive'] is True
    assert data['contracts'][0]['key'] == 'fef'


def test_a_command_is_queued_for_the_engine_and_never_executed_here(client):
    """The web process must not be able to trade. All it can do is write a
    line for the engine to read."""
    c, tmp = client
    res = c.post('/api/command', json={'action': 'algo_on', 'contract': 'fef'})
    assert res.get_json()['ok'] is True
    lines = (tmp / 'commands.jsonl').read_text().strip().splitlines()
    assert json.loads(lines[0])['action'] == 'algo_on'


def test_a_command_with_no_action_is_refused(client):
    c, _ = client
    assert c.post('/api/command', json={}).status_code == 400


def test_a_venue_never_returns_its_password(client):
    """Not even masked: a masked value is one that gets echoed back into the
    form and saved over the real one."""
    c, _ = client
    body = c.get('/api/venues').data.decode()
    assert 'hunter2' not in body
    venue = json.loads(body)[0]
    assert venue['password_set'] is True
    assert 'password' not in venue


def test_settings_round_trip_and_name_what_needs_a_restart(client):
    c, _ = client
    res = c.post('/api/settings', json={'PRICE_REFRESH_SEC': 0.25,
                                        'MAX_QUOTE_AGE_SEC': 20})
    assert res.get_json()['restart_needed'] == ['PRICE_REFRESH_SEC']
    assert c.get('/api/settings').get_json()['MAX_QUOTE_AGE_SEC'] == 20


def test_a_typed_specification_becomes_an_override_and_says_so(client, tmp_path):
    from fixtrader.config import ContractConfig
    cfg = TraderConfig.from_file(str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(key='fef', tick_size=0.01,
                                          spec_source={'tick_size': 'venue'})
    cfg.save()
    c, _ = client
    c.post('/api/contracts/fef', json={'tick_size': 0.05})
    back = TraderConfig.from_file(str(tmp_path / 'config.json'))
    assert back.contracts['fef'].tick_size == 0.05
    assert back.contracts['fef'].spec_source['tick_size'] == 'operator'


def test_a_blank_override_clears_and_zero_sets_one(client, tmp_path):
    from fixtrader.config import ContractConfig
    cfg = TraderConfig.from_file(str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(key='fef')
    cfg.save()
    c, _ = client
    c.post('/api/contracts/fef', json={'commission_per_contract': 0})
    back = TraderConfig.from_file(str(tmp_path / 'config.json'))
    assert back.contracts['fef'].overrides['commission_per_contract'] == 0
    c.post('/api/contracts/fef', json={'commission_per_contract': ''})
    back2 = TraderConfig.from_file(str(tmp_path / 'config.json'))
    assert back2.contracts['fef'].overrides['commission_per_contract'] is None


def test_saving_a_contract_that_does_not_exist_is_refused(client):
    c, _ = client
    assert c.post('/api/contracts/nope', json={}).status_code == 404
