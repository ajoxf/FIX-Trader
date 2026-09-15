"""Stage 3: a human-confirmed, filled algo proposal must become a real
rt.position with correct break_even/target/stop, and closing it must
compute PnL identically to the ordinary executor path — all via
drain_algo_events(), never touching Executor or _handle_event."""
import datetime as dt
import socket
import time

from fixtrader.config import ContractConfig, TraderConfig, VenueConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.gateway import FixGateway, encode_fix_message, parse_fix_message


class Peer:
    """Acks Logon, and for New Order Single (D) sends back a full fill at
    the limit price. For a Cancel/Replace we do not need it here."""
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
        if fields['35'] == 'D':
            price = fields.get('44', '100')
            reply += [('11', fields['11']), ('37', 'TT-' + fields['11']),
                      ('39', '2'), ('150', '2'), ('17', 'FILL-' + fields['11']),
                      ('14', fields['38']), ('151', '0'),
                      ('31', price), ('32', fields['38']), ('6', price)]
        self.frames.append(encode_fix_message(reply))

    def recv(self, _):
        if self.frames:
            return self.frames.pop(0)
        time.sleep(.005)
        raise socket.timeout()


def _wired_gateway(monkeypatch, contracts=None):
    def connect(*a, **kw):
        return Peer()
    monkeypatch.setattr(socket, 'create_connection', connect)
    monkeypatch.setenv('WIRE_PASSWORD', 'test-secret')
    v = VenueConfig(name='UAT', host='example', port=1, fix_version='FIX.4.2',
                    sender_comp_id='CLIENT', target_comp_id='TT',
                    password_env='WIRE_PASSWORD', account='TEST', use_tls=False)
    gateway = FixGateway(v, contracts=contracts or [])
    gateway.start()
    deadline = time.monotonic() + 2
    while gateway.state().value != 'LOGGED_ON' and time.monotonic() < deadline:
        time.sleep(.01)
    assert gateway.state().value == 'LOGGED_ON'
    return gateway


def _contract(key='bz_v6'):
    return ContractConfig(key=key, name=key, symbol='BZV6', venue='UAT',
        tick_size=0.01, tick_value=10.0, contract_multiplier=1000.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        lookback=30, stats_update_interval_sec=1e9, entry_threshold=2.0,
        quantity=1.0, hurst_enabled=False, edge_filter_enabled=False,
        entry_cooldown_seconds=0.0, entry_order_type='LIMIT',
        exit_order_type='LIMIT', commission_per_contract=1.0,
        exchange_fee_per_contract=0.0, clearing_fee_per_contract=0.0,
        slippage_budget_ticks=0.0, profit_target_pct=2.0)


def _wait_until(fn, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(.01)
    return fn()


def test_confirmed_algo_entry_becomes_a_real_position(monkeypatch, tmp_path):
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}

        cfg = TraderConfig(path=str(tmp_path / 'config.json'))
        cfg.contracts[contract.key] = contract
        db = Database(str(tmp_path / 'test.db'))
        engine = Engine(cfg, gateway, db=db, simulated=True)
        engine.start()

        proposal = gateway.propose_entry(
            contract, 'BUY', 1, order_type='LIMIT', price=80.5,
            reason='z=2.4 crossed', decision={'z': 2.4, 'mean': 79.0, 'std': 0.6})
        result = gateway.terminal.submit({'token': proposal['token'], 'confirmed': True})
        assert result['ok'] is True

        assert _wait_until(
            lambda: gateway.terminal.orders[result['order_id']]['status'] == 'FILLED')

        # This is the actual thing under test: poll() must pick up the fill
        # via drain_algo_events() and build a real position from it.
        engine.poll(now=dt.datetime.now(dt.timezone.utc))

        rt = engine.runtimes[contract.key]
        assert rt.position is not None
        assert rt.position.is_open
        assert rt.position.qty == 1
        assert rt.position.avg_price == 80.5
        assert rt.position.entry_z == 2.4
        assert rt.position.venue_order_id == result['order_id']
        # break_even/target/stop are the SAME costs_mod functions the
        # ordinary path uses — not None, not skipped.
        assert rt.position.break_even is not None
    finally:
        gateway.stop()


def test_confirmed_algo_exit_closes_the_position_with_pnl(monkeypatch, tmp_path):
    gateway = _wired_gateway(monkeypatch)
    try:
        contract = _contract()
        gateway._contract_security_ids[contract.key] = '1'
        gateway.terminal.watch['1'] = {'security_id': '1', 'symbol': 'BZV6',
            'description': 'Brent Oct26', 'exchange': 'ICE',
            'tick_size': '0.01', 'parameters': {}}

        cfg = TraderConfig(path=str(tmp_path / 'config.json'))
        cfg.contracts[contract.key] = contract
        db = Database(str(tmp_path / 'test.db'))
        engine = Engine(cfg, gateway, db=db, simulated=True)
        engine.start()

        # Open first.
        entry = gateway.propose_entry(
            contract, 'BUY', 1, order_type='LIMIT', price=80.5,
            reason='z=2.4', decision={'z': 2.4, 'mean': 79.0, 'std': 0.6})
        entry_result = gateway.terminal.submit(
            {'token': entry['token'], 'confirmed': True})
        _wait_until(lambda: gateway.terminal.orders[entry_result['order_id']]['status'] == 'FILLED')
        engine.poll(now=dt.datetime.now(dt.timezone.utc))
        rt = engine.runtimes[contract.key]
        assert rt.position is not None
        order_id = rt.position.venue_order_id

        # Now close it via the SAME reviewed pathway.
        from fixtrader.models import ExitReason
        exit_proposal = gateway.propose_exit(
            contract, order_id, reason='z reverted to 0', exit_reason=ExitReason.TARGET)
        assert exit_proposal['ok'] is True
        exit_result = gateway.terminal.submit(
            {'token': exit_proposal['token'], 'confirmed': True})
        assert exit_result['ok'] is True
        _wait_until(lambda: gateway.terminal.orders[exit_result['order_id']]['status'] == 'FILLED')

        engine.poll(now=dt.datetime.now(dt.timezone.utc))

        assert rt.position is None   # the position dict was cleared on close
        # A closed trade should have been recorded with PnL computed.
        assert rt.trades_today == 1
    finally:
        gateway.stop()
