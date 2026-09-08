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
    # No host: the three buttons run against the simulator, which is what a
    # desk sees before Orient's endpoint exists.
    cfg.venues['orient'] = VenueConfig('Orient UAT', environment='UAT',
                                       password_env='FIX_ORIENT_UAT')
    cfg.venues['wired'] = VenueConfig('Orient PROD-ish', environment='UAT',
                                      host='uat.example', port=9823)
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


# -- venues, for the Exchanges page ----------------------------------------

def test_a_venue_can_be_created_and_its_password_goes_only_to_env(client, tmp_path, monkeypatch):
    """config.json holds the NAME of the key. The value goes to .env, and
    nothing anywhere returns it."""
    import os
    c, tp = client
    monkeypatch.chdir(tp)
    res = c.post('/api/venues/Orient SGX UAT', json={
        'environment': 'UAT', 'host': 'uat.example', 'port': 9823,
        'sender_comp_id': 'AJOX', 'target_comp_id': 'OFSG',
        'password': 'sup3rsecret'})
    assert res.get_json()['ok'] is True

    config_text = (tp / 'config.json').read_text()
    assert 'sup3rsecret' not in config_text
    assert 'FIX_ORIENT_SGX_UAT' in config_text
    assert 'sup3rsecret' in (tp / '.env').read_text()
    assert 'sup3rsecret' not in c.get('/api/venues').data.decode()


def test_a_venue_without_an_environment_is_refused(client):
    """There is no default. Assuming UAT would eventually assume it about a
    live venue."""
    c, _ = client
    res = c.post('/api/venues/Nameless', json={'host': 'h'})
    assert res.status_code == 400
    assert 'UAT' in res.get_json()['error']


def test_an_empty_password_field_leaves_the_stored_one_alone(client, tmp_path, monkeypatch):
    """Re-saving a form that shows no password must not wipe the real one."""
    c, tp = client
    monkeypatch.chdir(tp)
    c.post('/api/venues/V', json={'environment': 'UAT', 'password': 'keepme'})
    c.post('/api/venues/V', json={'environment': 'UAT', 'host': 'changed',
                                  'password': ''})
    assert 'keepme' in (tp / '.env').read_text()


def test_a_venue_in_use_cannot_be_deleted(client, tmp_path):
    from fixtrader.config import ContractConfig, TraderConfig
    c, tp = client
    cfg = TraderConfig.from_file(str(tp / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(key='fef', venue='orient')
    cfg.save()
    res = c.delete('/api/venues/orient')
    assert res.status_code == 409
    assert 'fef' in res.get_json()['error']


def test_connect_reports_the_sessions_own_words(client):
    c, _ = client
    rows = c.get('/api/venues/orient/connect').get_json()['rows']
    assert rows and rows[0]['check'] == 'Session'
    assert rows[0]['detail']


def test_diagnose_names_a_tick_mismatch_as_a_failure():
    """Not a warning: every money figure on that window runs through it."""
    from fixtrader.config import ContractConfig
    from fixtrader.fake_gateway import FakeGateway, SimContract
    from fixtrader.webapp import _contract_check

    # A venue that says 0.01 against a config that says 0.05.
    gateway = FakeGateway([SimContract('fef', tick_size=0.01, tick_value=1.0)])
    contract = ContractConfig(key='fef', symbol='FEF', tick_size=0.05,
                              tick_value=1.0)
    row = _contract_check(gateway, contract)
    assert row['ok'] is False
    assert 'tick_size' in row['detail']
    assert '0.01' in row['detail'] and '0.05' in row['detail']
    assert row['fix']                      # every failure carries its step


def test_diagnose_passes_a_contract_the_venue_agrees_about():
    from fixtrader.config import ContractConfig
    from fixtrader.fake_gateway import FakeGateway, SimContract
    from fixtrader.webapp import _contract_check
    gateway = FakeGateway([SimContract('fef', tick_size=0.01, tick_value=1.0)])
    row = _contract_check(gateway, ContractConfig(key='fef', tick_size=0.01,
                                                  tick_value=1.0))
    assert row['ok'] is True


def test_a_contract_the_venue_does_not_know_is_a_failure_with_a_step():
    from fixtrader.config import ContractConfig
    from fixtrader.fake_gateway import FakeGateway
    from fixtrader.webapp import _contract_check
    row = _contract_check(FakeGateway([]), ContractConfig(key='nope'))
    assert row['ok'] is False
    assert 'FIX_NOTES' in row['fix']       # it points at the open question


def test_connect_against_an_unwired_session_says_so_honestly(client):
    """A venue with a host takes the real FixGateway, which is not wired yet.
    It must say that in words rather than looking like a connection failure
    somebody could try to fix by retyping the port."""
    c, _ = client
    body = c.get('/api/venues/wired/connect').get_json()
    assert body['ok'] is False
    assert 'not wired' in body['rows'][0]['detail']


def test_a_missing_password_is_reported_as_not_set_never_as_a_value(client):
    c, _ = client
    rows = c.get('/api/venues/orient/test').get_json()['rows']
    row = [r for r in rows if r['check'] == 'Password'][0]
    assert row['ok'] is True               # the fixture sets one
    assert 'hunter2' not in row['detail']
    assert 'FIX_ORIENT_UAT' in row['detail']


# -- contracts -------------------------------------------------------------

def test_a_contract_is_created_with_a_key_derived_from_its_symbol(client):
    c, _ = client
    res = c.post('/api/contracts', json={'symbol': 'FEFV6-FEFX6',
                                         'name': 'Iron ore Oct/Nov',
                                         'venue': 'orient'})
    assert res.get_json()['key'] == 'fefv6_fefx6'


def test_two_contracts_cannot_share_a_key(client):
    c, _ = client
    c.post('/api/contracts', json={'symbol': 'FEFV6-FEFX6', 'venue': 'orient'})
    res = c.post('/api/contracts', json={'symbol': 'FEFV6-FEFX6',
                                         'venue': 'orient'})
    assert res.status_code == 409


def test_a_contract_with_an_open_position_cannot_be_deleted(client, tmp_path):
    import json as _json
    from datetime import datetime, timezone
    from fixtrader.config import ContractConfig, TraderConfig
    c, tp = client
    cfg = TraderConfig.from_file(str(tp / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(key='fef', symbol='FEF')
    cfg.save()
    (tp / 'status.json').write_text(_json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True, 'refresh_sec': 0.5},
        'contracts': [{'key': 'fef',
                       'position': {'side': 'SELL', 'qty': 5}}]}))
    res = c.delete('/api/contracts/fef')
    assert res.status_code == 409
    assert 'SELL 5' in res.get_json()['error']


def test_reading_specs_from_the_venue_reports_rather_than_applies(client, tmp_path):
    """A specification changed under a running desk is every money figure on
    that window changing without anybody being told."""
    from fixtrader.config import ContractConfig, TraderConfig
    c, tp = client
    cfg = TraderConfig.from_file(str(tp / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(key='fef', symbol='FEF',
                                          venue='orient', tick_size=0.05)
    cfg.save()
    body = c.post('/api/contracts/fef/read-from-venue').get_json()
    assert body['ok'] is True
    tick = [f for f in body['fields'] if f['field'] == 'tick_size'][0]
    assert tick['config'] == 0.05
    assert tick['venue'] is not None
    # ...and nothing was written: the operator presses the button.
    after = TraderConfig.from_file(str(tp / 'config.json'))
    assert after.contracts['fef'].tick_size == 0.05


def test_diagnose_against_the_simulator_says_it_confirms_nothing():
    """A green Diagnose against a gateway built from this very configuration
    is not a check. It reads as one unless it says otherwise, and then a
    misconfiguration stays hidden until the real session arrives."""
    from fixtrader.config import ContractConfig
    from fixtrader.fake_gateway import FakeGateway, SimContract
    from fixtrader.webapp import _contract_check
    gateway = FakeGateway([SimContract('fef', tick_size=0.01, tick_value=1.0)])
    contract = ContractConfig(key='fef', tick_size=0.01, tick_value=1.0)

    row = _contract_check(gateway, contract, simulated=True)
    assert row['ok'] is True
    assert 'confirms nothing' in row['detail']

    # against a real venue the same pass says nothing of the kind
    plain = _contract_check(gateway, contract, simulated=False)
    assert 'confirms nothing' not in plain['detail']
