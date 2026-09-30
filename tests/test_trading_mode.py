"""ALGO or MANUAL — one at a time.

Two hands on one book fight: the algo closes a hand-placed position at its
own target, or re-enters the moment the trader gets flat, and the journal
then describes neither. So the desk is in one mode at a time. Each mode
refuses the OTHER side's new orders; a switch is refused while the side
being left still holds anything; and nothing ever refuses a close or a
cancel.
"""
import pytest

from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from tests.test_engine import put_book_at_z, warm_the_window
from tests.test_manual_terminal import report, terminal, ticket  # noqa: F401


def desk(tmp_path, terminal, mode_path=None):
    """The simulator engine, with a real manual terminal beside it."""
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        window_minutes=1e6, min_history_minutes=29 / 60.0,
        sample_interval_sec=1.0, stats_update_interval_sec=1e9,
        entry_threshold=2.0, confirm_samples=1, margin_per_contract=260.0,
        quantity=5.0, exit_at_mean=False, max_hold_minutes=0.0,
        entry_cooldown_seconds=0.0, entry_order_type='MARKET',
        exit_order_type='MARKET', commission_per_contract=1.0,
        profit_target_pct=2.0)
    gw = FakeGateway([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    gw.terminal = terminal
    engine = Engine(cfg, gw, db=Database(str(tmp_path / 't.db')),
                    simulated=True,
                    mode_path=mode_path or str(tmp_path / 'status.json.mode.json'))
    engine.start()
    return engine, gw


def manual_fill(terminal):
    p = terminal.preview(ticket(quantity='2'))
    oid = terminal.submit({'token': p['token'], 'confirmed': True})['order_id']
    report(terminal, {'35': '8', '11': oid, '39': '2', '150': '2', '17': 'F1',
                      '14': '2', '151': '0', '32': '2', '31': '-0.5'})
    return oid


# -- ALGO mode refuses a hand ---------------------------------------------

def test_algo_mode_refuses_a_new_manual_order(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    assert engine.trading_mode == 'ALGO'
    with pytest.raises(ValueError, match='ALGO mode'):
        terminal.preview(ticket())


def test_manual_mode_lets_the_hand_trade(tmp_path, terminal):
    """The control."""
    engine, _ = desk(tmp_path, terminal)
    assert engine.set_trading_mode('MANUAL')['ok']
    assert terminal.preview(ticket())['ok']


def test_a_ticket_reviewed_in_manual_is_not_sent_after_a_switch_to_algo(
        tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    engine.set_trading_mode('MANUAL')
    reviewed = terminal.preview(ticket())
    assert engine.set_trading_mode('ALGO')['ok']
    with pytest.raises(ValueError):
        terminal.submit({'token': reviewed['token'], 'confirmed': True})


# -- MANUAL mode stands the algo down ---------------------------------------

def test_manual_mode_withholds_every_algo_entry(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    engine.set_trading_mode('MANUAL')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert 'MANUAL mode' in rt.blocked_by
    assert engine.set_auto_trade(True)['ok'] is False


def test_algo_mode_lets_the_algo_enter(tmp_path, terminal):
    """The control: the same market, in ALGO mode, is an entry."""
    engine, gw = desk(tmp_path, terminal)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None


# -- a switch never leaves the other side's business on the book -----------

def test_the_desk_cannot_go_manual_with_an_algo_position_open(tmp_path, terminal):
    engine, gw = desk(tmp_path, terminal)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    refused = engine.set_trading_mode('MANUAL')
    assert refused['ok'] is False and 'open algo position' in refused['error']
    assert engine.trading_mode == 'ALGO'
    # CLOSE NOW still works, and once flat the switch goes through
    engine.close_now('fef'); engine.poll(now=gw.now)
    assert rt.position is None
    assert engine.set_trading_mode('MANUAL')['ok']


def test_the_desk_cannot_go_algo_with_a_manual_fill_open(tmp_path, terminal):
    engine, _ = desk(tmp_path, terminal)
    engine.set_trading_mode('MANUAL')
    oid = manual_fill(terminal)
    refused = engine.set_trading_mode('ALGO')
    assert refused['ok'] is False and 'not yet closed' in refused['error']
    assert engine.trading_mode == 'MANUAL'
    # closing it is always allowed; closed, the switch goes through
    close = terminal.preview_close({'order_id': oid})
    cid = terminal.submit({'token': close['token'], 'confirmed': True})['order_id']
    report(terminal, {'35': '8', '11': cid, '39': '2', '150': '2', '17': 'F2',
                      '14': '2', '151': '0', '32': '2', '31': '-0.5'})
    assert engine.set_trading_mode('ALGO')['ok']


def test_a_manual_close_is_never_refused_in_algo_mode(tmp_path, terminal):
    """If a hand position is ever on the book in ALGO mode — a restart, a
    recovered order — the way out is never the thing that is refused."""
    engine, _ = desk(tmp_path, terminal)
    engine.set_trading_mode('MANUAL')
    oid = manual_fill(terminal)
    engine.trading_mode = 'ALGO'              # forced, as a restart might
    close = terminal.preview_close({'order_id': oid})
    assert terminal.submit({'token': close['token'], 'confirmed': True})['ok']


def test_the_mode_survives_a_restart(tmp_path, terminal):
    path = str(tmp_path / 'mode.json')
    engine, _ = desk(tmp_path, terminal, mode_path=path)
    engine.set_trading_mode('MANUAL')
    again, _ = desk(tmp_path, terminal, mode_path=path)
    assert again.trading_mode == 'MANUAL'
    assert again.snapshot()['engine']['trading_mode'] == 'MANUAL'
