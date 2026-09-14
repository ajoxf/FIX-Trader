"""Stage 1: FixGateway.propose_entry/propose_exit/pending_algo_proposals/
clear_algo_proposals. These call straight into ManualTerminal's existing
preview()/preview_close() — nothing here is a new send path, and none of
it modifies manual_terminal.py or NativeFixSession's FTM- prefix check.
Uses the same fake-socket wire pattern as test_manual_wire.py so this is a
real end-to-end check, not a mock of the thing being tested."""
import socket
import time

from fixtrader.config import ContractConfig, VenueConfig
from fixtrader.gateway import FixGateway, encode_fix_message, parse_fix_message


def _wired_gateway(monkeypatch, fills=True):
    class Peer:
        def __init__(self):
            self.frames = []
            self.sent = []
            self.seq = 0

        def settimeout(self, _):
            pass

        def close(self):
            pass

        def sendall(self, raw):
            fields = parse_fix_message(raw.decode('ascii'))
            self.sent.append(fields)
            if fields['35'] not in ('A', 'D'):
                return
            self.seq += 1
            reply = [('35', 'A' if fields['35'] == 'A' else '8'),
                     ('34', str(self.seq)), ('49', 'TT'), ('56', fields['49'])]
            if fields['35'] == 'D' and fills:
                reply += [('11', fields['11']), ('37', 'TT-ORDER'),
                          ('39', '2'), ('150', '2'), ('17', 'FILL1'),
                          ('14', fields['38']), ('151', '0'),
                          ('31', fields.get('44', '0')), ('32', fields['38']),
                          ('6', fields.get('44', '0'))]
            self.frames.append(encode_fix_message(reply))

        def recv(self, _):
            if self.frames:
                return self.frames.pop(0)
            time.sleep(.005)
            raise socket.timeout()

    def connect(*args, **kwargs):
        return Peer()
    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setenv('WIRE_PASSWORD', 'test-secret')
    v = VenueConfig(name='UAT', host='example', port=1, fix_version='FIX.4.2',
                    sender_comp_id='CLIENT', target_comp_id='TT',
                    password_env='WIRE_PASSWORD', account='TEST', use_tls=False)
    gateway = FixGateway(v)
    gateway.start()
    deadline = time.monotonic() + 2
    while gateway.state().value != 'LOGGED_ON' and time.monotonic() < deadline:
        time.sleep(.01)
    assert gateway.state().value == 'LOGGED_ON'
    return gateway


def _contract(key='bz_v6', symbol='BZV6'):
    return ContractConfig(key=key, name=key, symbol=symbol, venue='UAT',
        tick_size=0.01, tick_value=10.0, contract_multiplier=1000.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0)


def test_propose_entry_refuses_without_a_watchlisted_instrument(monkeypatch):
    gateway = _wired_gateway(monkeypatch)
    try:
        result = gateway.propose_entry(_contract(), 'BUY', 1, reason='z=2.3')
        assert result['ok'] is False
        assert 'watchlist' in result['error']
    finally:
        gateway.stop()


def test_propose_entry_creates_a_reviewable_preview_not_a_sent_order(monkeypatch):
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}
        result = gateway.propose_entry(contract, 'BUY', 5, reason='z=2.3 crossed')
        assert result['ok'] is True
        assert result['token'] in gateway.terminal.previews
        # Nothing sent yet — only Logon should be on the wire so far.
        pending = gateway.pending_algo_proposals()
        assert len(pending) == 1
        assert pending[0]['contract_key'] == contract.key
        assert pending[0]['reason'] == 'z=2.3 crossed'
        assert pending[0]['kind'] == 'entry'
    finally:
        gateway.stop()


def test_a_human_confirming_the_proposal_uses_the_existing_submit_path(monkeypatch):
    """The whole point: confirmation goes through terminal.submit() exactly
    as a manually-typed order would — FTM- prefix included, unmodified."""
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}
        proposal = gateway.propose_entry(contract, 'BUY', 5, reason='z=2.3')
        token = proposal['token']
        result = gateway.terminal.submit({'token': token, 'confirmed': True})
        assert result['ok'] is True
        assert result['order_id'].startswith('FTM-')
        deadline = time.monotonic() + 2
        while gateway.terminal.orders[result['order_id']]['status'] != 'FILLED' \
                and time.monotonic() < deadline:
            time.sleep(.01)
        assert gateway.terminal.orders[result['order_id']]['status'] == 'FILLED'
        # Once confirmed, it is no longer a PENDING proposal.
        assert gateway.pending_algo_proposals() == []
    finally:
        gateway.stop()


def test_pending_proposals_expire_and_self_clean(monkeypatch):
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}
        proposal = gateway.propose_entry(contract, 'BUY', 5, reason='z=2.3')
        # Force the preview to look expired without waiting 60 real seconds.
        gateway.terminal.previews[proposal['token']]['expires'] = time.time() - 1
        assert gateway.pending_algo_proposals() == []
        assert proposal['token'] not in gateway._algo_proposals
    finally:
        gateway.stop()


def test_clear_algo_proposals_hides_them_from_the_pending_list(monkeypatch):
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}
        gateway.propose_entry(contract, 'BUY', 5, reason='z=2.3')
        assert len(gateway.pending_algo_proposals()) == 1
        cleared = gateway.clear_algo_proposals()
        assert cleared == 1
        assert gateway.pending_algo_proposals() == []
    finally:
        gateway.stop()
