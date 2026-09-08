"""The loop, end to end against the simulator: open, manage, close, recover."""
import pytest

from fixtrader.config import DEFAULT_SETTINGS, ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.models import ContractState, ExitReason, OrderType, Side


def build(tmp_path, **overrides):
    contract = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        venue='SIM', tick_size=0.01, tick_value=1.0,
        contract_multiplier=100.0, min_qty=1.0, qty_step=1.0, max_qty=50.0,
        enabled=True, algo_on=True,
        **dict({'lookback': 30, 'stats_update_interval_sec': 1e9,
                'entry_threshold': 2.0, 'quantity': 5.0,
                'hurst_enabled': False, 'edge_filter_enabled': False,
                'entry_cooldown_seconds': 0.0,
                'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
                'commission_per_contract': 1.0,
                'exchange_fee_per_contract': 0.0,
                'clearing_fee_per_contract': 0.0,
                'slippage_budget_ticks': 0.0,
                'profit_target_pct': 2.0}, **overrides))
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = contract
    gw = FakeGateway([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    db = Database(str(tmp_path / 'test.db'))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return engine, gw, db, cfg


def warm_the_window(engine, gw, n=40):
    """Alternate the book so the window fills with a known sigma."""
    for i in range(n):
        px = 0.50 + (0.10 if i % 2 else -0.10)
        gw.set_book('fef', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        gw.now = gw.now.replace(microsecond=0)
        gw.advance(seconds=1, steps=0) if False else None
        engine.poll(now=gw.now)
        gw.now = gw.now + __import__('datetime').timedelta(seconds=1)
    return engine.runtimes['fef']


def put_book_at_z(engine, gw, z):
    rt = engine.runtimes['fef']
    px = rt.window.price_at_z(z)
    gw.set_book('fef', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
    return px


def test_it_warms_before_it_trades(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = engine.runtimes['fef']
    assert engine.state_of(rt, gw.now) is ContractState.WARMING
    warm_the_window(engine, gw)
    assert rt.window.is_warm
    assert engine.state_of(rt, gw.now) is not ContractState.WARMING


def test_a_high_z_opens_a_short_and_records_the_state_at_entry(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)
    engine.poll(now=gw.now)              # the fill comes back on the next pass

    pos = rt.position
    assert pos is not None and pos.side is Side.SELL and pos.qty == 5
    # everything the Analysis window will need, written AT THE TIME
    assert pos.entry_z == pytest.approx(2.5, abs=0.1)
    assert pos.entry_mean is not None and pos.entry_std is not None
    assert pos.margin_locked == pytest.approx(260.0 * 5)
    assert pos.break_even is not None and pos.target_price is not None
    assert engine.state_of(rt, gw.now) is ContractState.IN


def test_the_short_target_is_below_the_entry_and_it_closes_there(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos.target_price < pos.avg_price          # a short leaves lower

    gw.set_book('fef', pos.target_price - 0.02, pos.target_price - 0.01, 50, 50)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    closed = db.closed_positions('fef')
    assert len(closed) == 1
    assert closed[0].exit_reason is ExitReason.TARGET
    assert closed[0].net_pnl is not None
    assert closed[0].pnl_pct_on_margin is not None


def test_the_stop_closes_even_with_every_filter_blocking_entries(tmp_path):
    engine, gw, db, cfg = build(tmp_path, hurst_enabled=True,
                                hurst_threshold=0.01, stop_loss_z=3.0)
    rt = warm_the_window(engine, gw)
    # open by hand-ish: turn the filter off for the entry, then back on
    cfg.contracts['fef'].overrides['hurst_enabled'] = False
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    cfg.contracts['fef'].overrides['hurst_enabled'] = True

    put_book_at_z(engine, gw, 3.6)                   # through the stop
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.STOP_LOSS


def test_a_stale_quote_halts_entries_but_not_the_close(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None

    later = gw.now + __import__('datetime').timedelta(seconds=120)
    engine.poll(now=later)
    assert engine.state_of(rt, later) is ContractState.HALTED
    # ...and CLOSE NOW still works
    assert engine.close_now('fef')['ok'] is True
    engine.poll(now=later)
    assert rt.position is None


def test_close_now_is_recorded_as_its_own_reason(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    engine.close_now('fef')
    engine.poll(now=gw.now)
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.CLOSE_NOW


def test_kill_all_stands_down_without_flattening_by_default(tmp_path):
    """The button pressed in a hurry must not also cross every spread."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None
    engine.kill_all()
    assert engine.killed and not engine.master_algo
    assert rt.position is not None                    # untouched
    assert engine.state_of(rt, gw.now) is ContractState.HALTED
    # ...and it flattens when explicitly asked
    engine.kill_all(close_positions=True)
    engine.poll(now=gw.now)
    assert rt.position is None


def test_the_algo_switch_is_per_contract_and_persists(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    engine.set_algo('fef', False)
    assert cfg.contracts['fef'].algo_on is False
    reloaded = TraderConfig.from_file(cfg.path)
    assert reloaded.contracts['fef'].algo_on is False


def test_the_position_survives_a_restart(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    entry_z = engine.runtimes['fef'].position.entry_z

    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    pos = engine2.runtimes['fef'].position
    assert pos is not None and pos.entry_z == pytest.approx(entry_z)
    assert engine2.book_complete


def test_an_unreadable_venue_leaves_the_book_incomplete_not_flat(tmp_path):
    """The failure this rule exists for: an empty read that gets adopted as
    'the venue is flat' closes real positions as orphans."""
    engine, gw, db, cfg = build(tmp_path)
    gw.readable = False
    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    assert engine2.book_complete is False
    assert engine2.unclaimed == []


def test_a_position_the_book_cannot_explain_is_listed_never_closed(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    # something at the venue that this book knows nothing about
    gw._short['fef'] = {'qty': 2.0, 'avg': 0.5}
    engine2 = Engine(cfg, gw, db=db, simulated=True)
    engine2.start()
    assert engine2.unclaimed and 'Nothing has been closed' in engine2.unclaimed[0]['text']
    assert gw.positions()[0].qty == -2.0             # still there


def test_the_snapshot_carries_every_field_the_window_needs(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    warm_the_window(engine, gw)
    snap = engine.snapshot(now=gw.now)
    c = snap['contracts'][0]
    for key in ('market', 'stats', 'filters', 'costs', 'position', 'state'):
        assert key in c
    for key in ('mean', 'std', 'z', 'buy_at', 'sell_at', 'hurst', 'half_life',
                'warm_pct', 'is_warm'):
        assert key in c['stats']
    assert snap['engine']['refresh_sec'] == 0.5


def test_the_snapshot_reports_unknowns_as_none_not_zero(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    snap = engine.snapshot(now=gw.now)          # nothing has warmed yet
    stats = snap['contracts'][0]['stats']
    assert stats['z'] is None and stats['mean'] is None
    assert snap['contracts'][0]['position'] is None


def test_specs_are_read_from_the_venue_and_labelled(tmp_path):
    engine, gw, db, cfg = build(tmp_path)
    contract = cfg.contracts['fef']
    assert contract.spec_source.get('tick_size') == 'venue'


def test_the_entry_z_recorded_is_the_one_the_decision_fired_at(tmp_path):
    """Not the z at the moment the fill lands. The market moves between the
    two, and every figure the Analysis window reports about WHY a trade
    happened is built on the z the algo actually acted on."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now)                       # the order goes here
    decided_z = rt.window.z
    assert decided_z == pytest.approx(2.5, abs=0.1)

    put_book_at_z(engine, gw, 0.4)                # the market moves away...
    engine.poll(now=gw.now)                       # ...before the fill lands
    assert rt.window.z == pytest.approx(0.4, abs=0.1)

    assert rt.position is not None
    assert rt.position.entry_z == pytest.approx(decided_z)
    assert rt.position.entry_std == pytest.approx(rt.window.std)


def test_every_close_carries_an_explicit_close_flag_and_its_tickets(tmp_path):
    """A close is never a bare opposite order. It says it is closing, names
    the position it closes, and carries that position's venue tickets."""
    from fixtrader.models import PositionEffect
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    pos = rt.position
    assert pos is not None and pos.tickets

    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]

    engine.close_now('fef')
    assert len(sent) == 1
    req = sent[0]
    assert req.position_effect is PositionEffect.CLOSE
    assert req.position_effect.is_close
    assert req.reduce_only is True          # the cap, as well as the flag
    assert req.position_id == pos.id
    assert req.close_tickets == pos.tickets
    assert req.qty == pos.qty               # never more than is open


def test_a_close_leaves_no_second_position_at_the_venue(tmp_path):
    """End to end against a venue that keeps the two sides apart: after a
    close there is nothing left, not a matched pair posting margin."""
    engine, gw, db, cfg = build(tmp_path)
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert gw.positions()[0].short_qty == 5.0

    engine.close_now('fef')
    engine.poll(now=gw.now)
    assert gw.positions() == []
    assert rt.position is None
    # and the algo went down with it, so it does not re-enter on the z that
    # is still sitting where it was
    assert cfg.contracts['fef'].algo_on is False


def test_the_close_flag_follows_the_contract_setting(tmp_path):
    """SHFE and INE price a close-today differently from a close-yesterday,
    so the contract chooses. AUTO reads it from the day it was opened."""
    from fixtrader.models import PositionEffect
    from fixtrader.executor import close_effect
    from datetime import datetime, timezone

    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    class P:
        opened_session = '2026-09-08'

    class Older:
        opened_session = '2026-09-07'

    assert close_effect({'close_offset_mode': 'CLOSE'}, P(), now) \
        is PositionEffect.CLOSE
    assert close_effect({'close_offset_mode': 'AUTO'}, P(), now) \
        is PositionEffect.CLOSE_TODAY
    assert close_effect({'close_offset_mode': 'AUTO'}, Older(), now) \
        is PositionEffect.CLOSE_YESTERDAY


def test_an_unknown_close_flag_degrades_to_CLOSE_and_never_to_OPEN(tmp_path):
    """Unknown is not a reason to open a position. A close whose flag could
    not be worked out is still a close."""
    from fixtrader.models import PositionEffect
    from fixtrader.executor import close_effect
    from datetime import datetime, timezone
    now = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)

    class NoSession:
        opened_session = None

    assert close_effect({'close_offset_mode': 'AUTO'}, NoSession(), now) \
        is PositionEffect.CLOSE
    assert close_effect({'close_offset_mode': 'nonsense'}, None, now) \
        is PositionEffect.CLOSE
    assert close_effect({}, None, now) is PositionEffect.CLOSE


def test_an_escalated_close_is_still_a_close(tmp_path):
    """The limit times out and crosses at market — and the market order that
    replaces it must carry the same close flag, not default to an open."""
    from fixtrader.models import PositionEffect
    engine, gw, db, cfg = build(tmp_path, exit_order_type='LIMIT',
                                exit_limit_timeout_sec=1,
                                exit_on_timeout='CROSS_AT_MARKET')
    rt = warm_the_window(engine, gw)
    put_book_at_z(engine, gw, 2.5)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position is not None

    engine.close_now('fef')                       # close_now crosses already
    sent = []
    original = gw.send
    gw.send = lambda req: (sent.append(req), original(req))[1]
    later = gw.now + __import__('datetime').timedelta(seconds=30)
    engine.poll(now=later)
    for req in sent:
        assert req.position_effect.is_close
