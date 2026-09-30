"""The engine around the 2.5-sigma signal: the confirmation counted in
samples, paper fills on a gateway that cannot trade, the window resumed
across a restart, and the margin the target is a percentage of."""
from datetime import timedelta

import pytest

from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract
from fixtrader.models import ExitReason, Side


class PaperGateway(FakeGateway):
    """Quotes like a venue, but — like TT today — cannot take an algo order,
    and reports no margin. Any order reaching it is a test failure."""
    connection_only = True

    def send(self, order):
        raise AssertionError("a paper engine sent an order to the venue")

    def margin_for(self, key, qty):
        return None


def contract(**over):
    return ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        **dict({'window_minutes': 1e6, 'min_history_minutes': 29 / 60.0,
                'sample_interval_sec': 1.0, 'stats_update_interval_sec': 1e9,
                'entry_threshold': 2.0, 'max_entry_z': 3.5,
                'confirm_samples': 3, 'margin_per_contract': 260.0,
                'quantity': 5.0, 'exit_at_mean': True, 'max_hold_minutes': 0.0,
                'entry_cooldown_seconds': 0.0,
                'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
                'commission_per_contract': 1.0, 'profit_target_pct': 2.0},
               **over))


def build(tmp_path, gateway_cls=FakeGateway, db_name='t.db', **over):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['fef'] = contract(**over)
    gw = gateway_cls([SimContract('fef', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    db = Database(str(tmp_path / db_name))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return engine, gw, db


def warm(engine, gw, n=40, step=1.0):
    for i in range(n):
        px = 0.50 + (0.10 if i % 2 else -0.10)
        gw.set_book('fef', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        engine.poll(now=gw.now)
        gw.now = gw.now + timedelta(seconds=step)
    return engine.runtimes['fef']


def at_z(engine, gw, z):
    rt = engine.runtimes['fef']
    px = rt.window.price_at_z(z)
    gw.set_book('fef', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)


# -- the confirmation ---------------------------------------------------------

def test_the_confirmation_counts_SAMPLES_not_polls(tmp_path):
    """Ten polls a second is not ten confirmations a second. Polls inside the
    same second add nothing; the third one-second sample through the level
    is what enters."""
    engine, gw, _ = build(tmp_path)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    for _ in range(10):                          # ten polls, one second
        engine.poll(now=gw.now)
        gw.now = gw.now + timedelta(seconds=0.1)
    assert rt.confirm_count <= 2 and rt.position is None
    assert 'confirming' in rt.blocked_by
    for _ in range(3):
        engine.poll(now=gw.now)
        gw.now = gw.now + timedelta(seconds=1)
    engine.poll(now=gw.now)
    assert rt.position is not None and rt.position.side is Side.SELL


def test_a_sample_back_inside_the_level_resets_the_count(tmp_path):
    engine, gw, _ = build(tmp_path)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); gw.now += timedelta(seconds=1)
    engine.poll(now=gw.now); gw.now += timedelta(seconds=1)
    assert rt.confirm_count == 2
    at_z(engine, gw, 0.5)
    engine.poll(now=gw.now); gw.now += timedelta(seconds=1)
    assert rt.confirm_count == 0 and rt.position is None


# -- paper ----------------------------------------------------------------------

def test_a_gateway_that_cannot_trade_is_traded_on_PAPER(tmp_path):
    """TT: the market is live, the order path is not. The signal still runs
    end to end — filled at the live BID for a sale, never the mid — and not
    one order reaches the venue (PaperGateway fails the test if it does)."""
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    assert engine.paper is True
    assert engine.set_auto_trade(True)['ok']
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    pos = rt.position
    assert pos is not None and pos.side is Side.SELL
    assert pos.avg_price == pytest.approx(rt.book.bid)      # the bid, not the mid
    assert pos.is_simulated is True
    assert pos.tickets and pos.tickets[0].startswith('PAPER-')
    assert pos.margin_locked == pytest.approx(260.0 * 5)     # the entered margin
    assert pos.target_price is not None and pos.target_price < pos.avg_price

    # back to the mean: paid, so it closes — bought back on the OFFER
    at_z(engine, gw, -0.2)
    gw.now += timedelta(seconds=1)
    engine.poll(now=gw.now)
    assert rt.position is None
    closed = db.closed_positions('fef')[0]
    assert closed.is_simulated is True
    assert closed.exit_reason in (ExitReason.TARGET, ExitReason.MEAN)
    assert closed.exit_price == pytest.approx(rt.book.ask)
    assert closed.net_pnl is not None and closed.net_pnl > 0


def test_paper_waits_for_auto_trade_to_be_armed(tmp_path):
    """The control: the same market with Auto trade off proposes and fills
    nothing."""
    engine, gw, _ = build(tmp_path, PaperGateway, confirm_samples=1)
    assert engine.set_auto_trade(False)['ok']
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None
    assert rt.proposal and rt.proposal['action'] == 'OPEN'


def test_close_now_on_paper_closes_at_the_live_price(tmp_path):
    engine, gw, db = build(tmp_path, PaperGateway, confirm_samples=1)
    engine.set_auto_trade(True)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is not None
    assert engine.close_now('fef')['ok']
    assert rt.position is None
    assert db.closed_positions('fef')[0].exit_reason is ExitReason.CLOSE_NOW


# -- margin -----------------------------------------------------------------------

def test_the_entered_margin_wins_over_the_venues(tmp_path):
    """One rule for the entry check and the position alike: the margin the
    trader entered; the venue's only where none was entered."""
    engine, gw, _ = build(tmp_path, confirm_samples=1, margin_per_contract=400.0)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position.margin_locked == pytest.approx(400.0 * 5)


def test_with_no_margin_entered_the_venues_stands_in(tmp_path):
    engine, gw, _ = build(tmp_path, confirm_samples=1, margin_per_contract=0.0)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now); engine.poll(now=gw.now)
    assert rt.position.margin_locked == pytest.approx(260.0 * 5)   # FakeGateway's


def test_with_no_margin_anywhere_nothing_enters_and_the_window_says_why(tmp_path):
    engine, gw, _ = build(tmp_path, PaperGateway, confirm_samples=1,
                          margin_per_contract=0.0)
    engine.set_auto_trade(True)
    rt = warm(engine, gw)
    at_z(engine, gw, 2.6)
    engine.poll(now=gw.now)
    assert rt.position is None
    assert 'no margin entered' in rt.blocked_by


# -- restart --------------------------------------------------------------------

def test_a_quick_restart_resumes_the_window(tmp_path):
    """A window recorded up to a minute ago is reloaded, so a restart trades
    again at once instead of collecting for two hours."""
    from fixtrader.engine import utcnow
    db = Database(str(tmp_path / 'r.db'))
    now = utcnow()
    db.save_samples('fef', [(now - timedelta(seconds=100 - i),
                             0.5 + (0.1 if i % 2 else -0.1)) for i in range(60)])
    engine, _, _ = build(tmp_path, db_name='r.db')
    rt = engine.runtimes['fef']
    assert rt.window.is_warm
    assert rt.window.samples == 60
    assert 'resumed' in rt.last_event


def test_a_restart_after_a_long_gap_starts_afresh(tmp_path):
    """The control: the same recorded window, three hours old, is not
    adopted — its mean belongs to a market that has moved on."""
    from fixtrader.engine import utcnow
    db = Database(str(tmp_path / 'old.db'))
    now = utcnow()
    db.save_samples('fef', [(now - timedelta(hours=3, seconds=60 - i),
                             0.5 + (0.1 if i % 2 else -0.1)) for i in range(60)])
    engine, _, _ = build(tmp_path, db_name='old.db')
    rt = engine.runtimes['fef']
    assert rt.window.samples == 0
    assert 'afresh' in rt.last_event
