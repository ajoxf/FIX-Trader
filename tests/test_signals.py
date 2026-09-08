"""The algo. Every blocked entry has a CONTROL that unblocks it and asserts
the opposite — a guard test without one only proves the code never fires."""
from datetime import timedelta

import pytest

from fixtrader import signals
from fixtrader.models import (BookTop, ExitReason, Position, Side, TargetBasis)
from fixtrader.stats import StatsWindow
from tests.conftest import at, warm

TICK, VALUE = 0.01, 1.0


def window(entry=2.0, lookback=30):
    w = StatsWindow('fef', lookback=lookback, stats_update_interval_sec=1e9,
                    entry_threshold=entry)
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(lookback)])
    return w


def book_at(w, z, size=100.0, width_ticks=1):
    px = w.price_at_z(z)
    half = width_ticks * TICK / 2
    return BookTop(bid=px - half, ask=px + half, bid_size=size, ask_size=size,
                   ts=at(0))


def cfg(**over):
    base = {
        'entry_threshold': 2.0, 'exit_threshold': 0.5, 'stop_loss_z': 4.0,
        'exit_signal_mode': 'profit', 'max_hold_minutes': 0.0,
        'hurst_enabled': False, 'hurst_threshold': 0.5,
        'edge_filter_enabled': False, 'min_std_multiple': 1.5,
        'half_life_enabled': False, 'max_half_life': 60.0,
        'min_book_size': 0.0, 'max_book_spread_ticks': 0.0,
        'quantity': 5.0, 'max_position': 0.0, 'max_trades_per_day': 0.0,
        'daily_max_loss': 0.0, 'entry_cooldown_seconds': 0.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15, 'slippage_budget_ticks': 0.0,
        'profit_target_pct': 2.0, 'profit_target_basis': TargetBasis.MARGIN,
    }
    base.update(over)
    return base


def entry(w, z, settings=None, **kw):
    """Put the window AND the book at z. The signal reads the window's z; the
    book is what it would trade against."""
    w.add(w.price_at_z(z), at(999))
    return signals.entry_signal(w, book_at(w, z), settings or cfg(), TICK,
                                VALUE, at(1000), **kw)


# -- entries ---------------------------------------------------------------

def test_a_spread_far_above_its_mean_is_sold():
    sig = entry(window(), 2.4)
    assert sig.action == "OPEN" and sig.side is Side.SELL


def test_a_spread_far_below_its_mean_is_bought():
    sig = entry(window(), -2.4)
    assert sig.action == "OPEN" and sig.side is Side.BUY


def test_inside_the_threshold_it_is_armed_and_waiting_not_blocked():
    sig = entry(window(), 1.2)
    assert sig.action == "NONE" and sig.blocked_by is None


def test_nothing_enters_before_the_window_is_full():
    w = StatsWindow('fef', lookback=400, stats_update_interval_sec=1e9)
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(100)])
    sig = signals.entry_signal(w, book_at(w, 2.5), cfg(), TICK, VALUE, at(1000))
    assert sig.action == "NONE"
    assert "collecting" in sig.blocked_by
    # control: fill the window and the same z enters
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(300)], start=200)
    assert entry(w, 2.5).action == "OPEN"


def test_the_master_switch_stands_every_contract_down():
    assert entry(window(), 2.5, master_on=False).blocked_by is not None
    assert entry(window(), 2.5, master_on=True).action == "OPEN"     # control


def test_algo_off_is_idle_and_is_not_reported_as_blocked():
    """'Blocked' means something is holding it back. Off is not that."""
    sig = entry(window(), 2.5, algo_on=False)
    assert sig.action == "NONE" and sig.blocked_by is None


def test_hurst_withholds_a_trending_contract():
    w = window()
    trending = dict(cfg(), hurst_enabled=True, hurst_threshold=0.3)
    sig = entry(w, 2.5, trending)
    assert sig.action == "NONE" and "Hurst" in sig.blocked_by
    # control: the filter off, and the same z enters
    assert entry(w, 2.5, dict(trending, hurst_enabled=False)).action == "OPEN"


def test_the_edge_filter_withholds_when_sigma_cannot_cover_the_round_trip():
    w = window()
    strict = dict(cfg(), edge_filter_enabled=True, min_std_multiple=50.0)
    sig = entry(w, 2.5, strict)
    assert sig.action == "NONE" and "edge" in sig.blocked_by
    # the reason names both numbers, not just "filtered"
    assert "round trip" in sig.blocked_by
    # control: a multiple sigma can meet
    assert entry(w, 2.5, dict(strict, min_std_multiple=0.1)).action == "OPEN"


def test_a_z_already_past_the_stop_is_too_late_not_early():
    w = window()
    sig = entry(w, 4.5, cfg(stop_loss_z=4.0))
    assert sig.action == "NONE" and "past the stop" in sig.blocked_by
    assert entry(w, 4.5, cfg(stop_loss_z=6.0)).action == "OPEN"       # control


def test_the_position_limit_withholds_and_names_the_numbers():
    w = window()
    sig = entry(w, 2.5, cfg(max_position=5.0), open_qty=5.0)
    assert "position limit" in sig.blocked_by
    assert entry(w, 2.5, cfg(max_position=20.0), open_qty=5.0).action == "OPEN"


def test_the_daily_loss_limit_withholds():
    w = window()
    sig = entry(w, 2.5, cfg(daily_max_loss=1000.0), pnl_today=-1200.0)
    assert "daily loss limit" in sig.blocked_by
    assert entry(w, 2.5, cfg(daily_max_loss=1000.0), pnl_today=-200.0).action == "OPEN"


def test_the_cooldown_withholds_and_says_how_long_is_left():
    w = window()
    sig = entry(w, 2.5, cfg(entry_cooldown_seconds=60.0),
                last_trade_at=at(970))
    assert "cooling down" in sig.blocked_by
    assert entry(w, 2.5, cfg(entry_cooldown_seconds=60.0),
                 last_trade_at=at(900)).action == "OPEN"


def test_a_stale_quote_withholds_an_entry():
    w = window()
    assert entry(w, 2.5, quote_stale=True).blocked_by is not None
    assert entry(w, 2.5, quote_stale=False).action == "OPEN"


def test_a_one_sided_book_is_not_a_market():
    w = window()
    w.add(w.price_at_z(2.5), at(999))
    sig = signals.entry_signal(w, BookTop(bid=0.5, ask=None), cfg(), TICK,
                               VALUE, at(1000))
    assert sig.action == "NONE" and "one-sided" in sig.blocked_by


def test_a_book_with_no_published_size_is_unknown_not_infinite():
    w = window()
    w.add(w.price_at_z(2.5), at(999))
    b = book_at(w, 2.5)
    b.bid_size = None
    sig = signals.entry_signal(w, b, cfg(min_book_size=10.0), TICK, VALUE,
                               at(1000))
    assert "no size" in sig.blocked_by


def test_a_book_too_wide_to_cross_withholds():
    w = window()
    w.add(w.price_at_z(2.5), at(999))
    wide = book_at(w, 2.5, width_ticks=8)
    sig = signals.entry_signal(w, wide, cfg(max_book_spread_ticks=3.0), TICK,
                               VALUE, at(1000))
    assert "wide" in sig.blocked_by
    ok = signals.entry_signal(w, wide, cfg(max_book_spread_ticks=20.0), TICK,
                              VALUE, at(1000))
    assert ok.action == "OPEN"


# -- exits -----------------------------------------------------------------

def position(side=Side.SELL, target=None, qty=5.0):
    return Position(contract_key='fef', side=side, qty=qty, avg_price=0.693,
                    opened_at=at(0), target_price=target, entry_z=2.1)


def exit_for(w, z, pos, settings=None, **kw):
    w.add(w.price_at_z(z), at(3599))
    return signals.exit_signal(w, book_at(w, z), pos, settings or cfg(), TICK,
                               VALUE, at(3600), **kw)


def test_a_short_leaves_when_the_ask_reaches_its_target():
    w = window()
    pos = position(Side.SELL, target=w.price_at_z(0.4))
    sig = exit_for(w, 0.3, pos)
    assert sig.action == "CLOSE" and sig.side is Side.BUY
    assert sig.exit_reason is ExitReason.TARGET


def test_a_long_leaves_when_the_bid_reaches_its_target():
    w = window()
    pos = position(Side.BUY, target=w.price_at_z(-0.4))
    sig = exit_for(w, -0.3, pos)
    assert sig.action == "CLOSE" and sig.side is Side.SELL


def test_the_target_is_measured_on_the_closing_side_not_the_mid():
    """A short buys back on the OFFER. Marking it at the mid flatters every
    open position by half the spread."""
    w = window()
    # A wide book whose mid is through the target but whose ask is not.
    px = w.price_at_z(0.4)
    b = BookTop(bid=px - 0.05, ask=px + 0.05, bid_size=10, ask_size=10)
    pos = position(Side.SELL, target=px)
    sig = signals.exit_signal(w, b, pos, cfg(), TICK, VALUE, at(3600))
    assert sig.action == "NONE"


def test_the_stop_fires_regardless_of_every_filter():
    """A position on a contract whose entries are all being withheld must
    still be able to get out."""
    w = window()
    everything_on = cfg(hurst_enabled=True, hurst_threshold=0.01,
                        edge_filter_enabled=True, min_std_multiple=99.0)
    # entries are certainly blocked under these settings
    assert entry(w, 2.5, everything_on).action == "NONE"
    pos = position(Side.SELL, target=w.price_at_z(-9))
    sig = exit_for(w, 4.5, pos, everything_on)
    assert sig.action == "CLOSE" and sig.exit_reason is ExitReason.STOP_LOSS


def test_the_session_cutoff_closes_whatever_the_z_says():
    w = window()
    pos = position(Side.SELL, target=w.price_at_z(-9))
    sig = exit_for(w, 2.0, pos, session_flat_due=True)
    assert sig.exit_reason is ExitReason.SESSION_FLAT


def test_the_time_stop_closes_and_reports_itself():
    w = window()
    pos = position(Side.SELL, target=w.price_at_z(-9))
    sig = exit_for(w, 2.0, pos, cfg(max_hold_minutes=30.0))
    assert sig.exit_reason is ExitReason.TIME_STOP


def test_zscore_mode_exits_on_the_z_and_profit_mode_does_not():
    w = window()
    unreachable = w.price_at_z(-9)
    pos = position(Side.SELL, target=unreachable)
    assert exit_for(w, 0.2, pos, cfg(exit_signal_mode='profit')).action == "NONE"
    z_mode = exit_for(w, 0.2, pos, cfg(exit_signal_mode='zscore'))
    assert z_mode.action == "CLOSE" and z_mode.exit_reason is ExitReason.ZSCORE


def test_hybrid_fires_on_whichever_comes_first():
    w = window()
    pos = position(Side.SELL, target=w.price_at_z(-9))
    assert exit_for(w, 0.2, pos, cfg(exit_signal_mode='hybrid')).action == "CLOSE"


def test_a_closed_position_produces_nothing():
    w = window()
    pos = position(Side.SELL, target=w.price_at_z(0.4))
    pos.closed_at = at(10)
    assert exit_for(w, 0.3, pos).action == "NONE"
