import socket
import time

import pytest

from fixtrader.config import VenueConfig
from fixtrader.gateway import FixGateway, SessionState, encode_fix_message, parse_fix_message


class Peer:
    def __init__(self):
        self.sent = []
        self.received = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def sendall(self, payload):
        fields = parse_fix_message(payload.decode('ascii'))
        self.sent.append(fields)
        if fields['35'] == 'A':
            reply = encode_fix_message([
                ('35', 'A'), ('34', '1'), ('49', 'TT'), ('56', fields['49']),
                ('98', '0'), ('108', '30'), ('141', 'Y')])
            # A checksum split across TCP reads used to break the backup client.
            self.received.extend([reply[:-2], reply[-2:]])

    def recv(self, size):
        if self.received:
            return self.received.pop(0)
        time.sleep(0.005)
        raise socket.timeout()

    def close(self):
        self.closed = True


def venue(monkeypatch):
    monkeypatch.setenv('TEST_OR', 'or-secret')
    monkeypatch.setenv('TEST_MD', 'md-secret')
    return VenueConfig(name='TT-UAT', host='or.example', port=11502,
        fix_version='FIX.4.2', use_tls=False, sender_comp_id='ORDER',
        target_comp_id='TT', password_env='TEST_OR',
        md_host='md.example', md_port=11503, md_sender_comp_id='MARKET',
        md_password_env='TEST_MD', on_behalf_of_sub_id='OPERATOR')


def test_two_sessions_logon_fragmented_response_heartbeat_and_logout(monkeypatch):
    peers = []
    def connect(*args, **kwargs):
        peer = Peer()
        peers.append(peer)
        return peer
    monkeypatch.setattr(socket, 'create_connection', connect)
    gateway = FixGateway(venue(monkeypatch))
    gateway.start()
    try:
        deadline = time.monotonic() + 2
        while gateway.state() == SessionState.CONNECTING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.state() == SessionState.LOGGED_ON
        assert len(peers) == 2
        gateway.start()
        assert len(peers) == 2  # Repeated Connect must not create another logon.
        assert {p.sent[0]['49'] for p in peers} == {'ORDER', 'MARKET'}
        assert {p.sent[0]['96'] for p in peers} == {'or-secret', 'md-secret'}
        assert all(p.sent[0]['116'] == 'OPERATOR' for p in peers)
        snapshot = gateway.connection_snapshot()
        assert len(snapshot['sessions']) == 2
        assert all(s['status'] == 'CONNECTED' for s in snapshot['sessions'])
        assert snapshot['activity'][0]['type'] == 'Logon'
        assert 'md-secret' not in str(snapshot) and 'or-secret' not in str(snapshot)
        session = gateway._sessions['Market Data']
        session._incoming(encode_fix_message([
            ('35', '1'), ('34', '2'), ('49', 'TT'), ('56', 'MARKET'),
            ('112', 'probe')]).decode('ascii'))
        assert session.socket.sent[-1]['112'] == 'probe'
        assert session.socket.sent[-1]['35'] == '0'
        with pytest.raises(NotImplementedError):
            session.send('D', [])
        with pytest.raises(NotImplementedError):
            gateway.send(None)
        assert gateway.orders() is None and gateway.positions() is None
        session.state.error = 'Rejected md-secret'
        assert 'md-secret' not in gateway.state_text()
        session.stop()
        session.thread.join(timeout=2)
        gateway.start()
        deadline = time.monotonic() + 2
        while gateway.state() == SessionState.CONNECTING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.state() == SessionState.LOGGED_ON
        assert len(peers) == 3  # A failed MD session leaves the OR session alone.
        gateway.reconnect()
        assert gateway.state() == SessionState.CONNECTING
        assert gateway.connection_snapshot()['reconnect_in'] > 0
        gateway.drain_events()
        assert len(peers) == 3  # Never log on during the TT cooldown.
        gateway._reconnect_at = time.monotonic() - 1
        gateway._connect_not_before = time.monotonic() - 1
        gateway.drain_events()
        deadline = time.monotonic() + 2
        while gateway.state() == SessionState.CONNECTING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gateway.state() == SessionState.LOGGED_ON
        assert len(peers) == 5
    finally:
        gateway.stop()
    assert gateway.state() == SessionState.DOWN
    assert all(p.closed and p.sent[-1]['35'] == '5' for p in peers)


def test_missing_credentials_and_unsupported_protocol_do_not_open_sockets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Must not connect with invalid configuration')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    v = venue(monkeypatch)
    monkeypatch.delenv('TEST_MD')
    gateway = FixGateway(v)
    gateway.start()
    assert 'Market Data: missing password' in gateway.state_text()
    v.fix_version = 'FIX.4.4'
    gateway.start()
    assert 'FIX.4.2' in gateway.state_text()


def test_socket_permission_denied_has_a_network_remedy(monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(10013, 'An attempt was made to access a socket in a way forbidden by its access permissions')

    monkeypatch.setattr(socket, 'create_connection', denied)
    gateway = FixGateway(venue(monkeypatch))
    gateway.start()
    deadline = time.monotonic() + 2
    while gateway.state() == SessionState.CONNECTING and time.monotonic() < deadline:
        time.sleep(0.01)
    diagnostic = gateway.diagnose()[0]
    assert diagnostic['ok'] is False
    assert 'firewall' in diagnostic['fix'].lower()
    assert '.env' not in diagnostic['fix']


def test_separate_session_settings_survive_config_roundtrip(monkeypatch):
    v = venue(monkeypatch)
    raw = v.to_dict()
    assert 'or-secret' not in str(raw) and 'md-secret' not in str(raw)
    loaded = VenueConfig.from_dict(v.name, raw)
    assert loaded.md_sender_comp_id == 'MARKET'
    assert loaded.md_password_env == 'TEST_MD'
    assert loaded.on_behalf_of_sub_id == 'OPERATOR'
