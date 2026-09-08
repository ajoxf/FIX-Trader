"""The replay says what a DIFFERENT setting would have done to the same
market. Everything here turns on it being honest about what it does not
know: the book was not recorded, the costs are budgeted, and there is no
queue."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from fixtrader import replay as replay_mod
from fixtrader.config import DEFAULT_SETTINGS


def settings(**over):
    """Effective settings, as a contract would hand them over."""
    base = {
        'lookback': 40, 'stats_update_interval_sec': 0,
        'entry_threshold': 2.0, 'exit_threshold': 0.5, 'stop_loss_z': 4.0,
        'exit_signal_mode': 'profit', 'max_hold_minutes': 0,
        'hurst_enabled': False, 'edge_filter_enabled': False,
        'half_life_enabled': False, 'min_book_size': 0,
        'max_book_spread_ticks': 0, 'quantity': 5.0, 'max_position': 0,
        'max_trades_per_day': 0, 'daily_max_loss': 0,
        'entry_cooldown_seconds': 0,
        'commission_per_contract': 1.0, 'exchange_fee_per_contract': 0.0,
        'clearing_fee_per_contract': 0.0, 'slippage_budget_ticks': 0.0,
        'profit_target_pct': 0.0, 'profit_target_basis': 'MARGIN',
        'entry_order_type': 'MARKET', 'exit_order_type': 'MARKET',
    }
    base.update(over)
    return base


def a_wave(n=600, mean=0.50, amp=0.08, period=60, step_sec=1):
    """A series that reverts on a known schedule, so what the replay should
    find is arithmetic and not luck."""
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [(t0 + timedelta(seconds=i * step_sec),
             mean + amp * math.sin(2 * math.pi * i / period))
            for i in range(n)]


def test_a_reverting_series_produces_trades_and_a_net_after_costs():
    out = replay_mod.replay(a_wave(), settings(exit_signal_mode='zscore'),
                            tick_size=0.01, tick_value=1.0,
                            contract_key='fef')
    assert out['warm'] is True
    assert out['summary']['trades'] > 0
    # Every trade carries the z it was decided on, both ends.
    for t in out['trades']:
        assert t['entry_z'] is not None and abs(t['entry_z']) >= 2.0
        assert t['net'] is not None and t['gross'] is not None
        assert t['costs'] > 0                      # the round trip is charged
        assert t['net'] == pytest.approx(t['gross'] - t['costs'])


def test_a_series_too_short_to_warm_the_window_says_so_rather_than_zero():
    """Zero trades because nothing triggered and zero trades because the
    window never warmed are different statements."""
    out = replay_mod.replay(a_wave(n=20), settings(), 0.01, 1.0)
    assert out['warm'] is False
    assert out['summary']['trades'] == 0
    assert '40' in out['blocked_by'] and 'samples' in out['blocked_by']

    warm = replay_mod.replay(a_wave(n=600), settings(entry_threshold=99.0),
                             0.01, 1.0)
    assert warm['warm'] is True                    # the control
    assert warm['summary']['trades'] == 0
    assert 'threshold' in warm['blocked_by']


def test_nothing_recorded_is_not_a_flat_result():
    out = replay_mod.replay([], settings(), 0.01, 1.0)
    assert out['summary']['net'] is None           # not 0.0
    assert 'nothing recorded' in out['blocked_by']


def test_every_result_carries_the_assumptions_it_ran_under():
    """A figure whose assumptions are not on the page is a figure somebody
    will quote without them."""
    out = replay_mod.replay(a_wave(), settings(), 0.01, 1.0)
    a = out['assumptions']
    assert a['assumed_spread_ticks'] == replay_mod.DEFAULT_ASSUMED_SPREAD_TICKS
    assert 'not recorded' in a['book']
    assert 'queue' in a['fills']
    assert 'BUDGET' in a['costs']


def test_the_replay_never_reports_a_measured_slippage():
    """The measured figure lives in the Analysis window beside the budget.
    A backtest reporting a cost it never paid is how a budget stops being
    corrected from data."""
    out = replay_mod.replay(a_wave(), settings(slippage_budget_ticks=2.0),
                            0.01, 1.0)
    assert out['summary']['slippage_measured'] is None
    assert out['assumptions']['round_trip_money'] > 0


def test_the_assumed_spread_changes_what_the_exits_get():
    """An exit reads the EXECUTABLE side, so the spread assumption is not
    cosmetic — a replay run on the mid would flatter every exit by half of
    it."""
    tight = replay_mod.replay(a_wave(), settings(exit_signal_mode='zscore'),
                              0.01, 1.0, assumed_spread_ticks=1.0)
    wide = replay_mod.replay(a_wave(), settings(exit_signal_mode='zscore'),
                             0.01, 1.0, assumed_spread_ticks=8.0)
    assert tight['summary']['net'] is not None and wide['summary']['net'] is not None
    assert wide['summary']['net'] < tight['summary']['net']


def test_a_position_open_at_the_end_is_excluded_and_counted():
    """The same rule the Analysis window follows for a live position: an
    unclosed trade has no P&L, and pretending it has one at the last price
    is marking your own homework."""
    # A quiet window, then a jump at the very end: it enters and the series
    # stops before anything could bring it back.
    rows = a_wave(n=300, amp=0.02, period=50)
    last = rows[-1][0]
    rows += [(last + timedelta(seconds=i), 0.90) for i in range(1, 4)]
    out = replay_mod.replay(rows, settings(lookback=40, stop_loss_z=99.0,
                                           exit_signal_mode='zscore',
                                           exit_threshold=0.0),
                            0.01, 1.0)
    assert out['still_open'] == 1
    # every trade REPORTED is a closed one, with both ends
    assert all(t['closed_at'] and t['exit_price'] is not None
               for t in out['trades'])


def test_a_sweep_names_the_level_that_PAID_not_the_one_that_reverted():
    """The touch table says the inner bands revert most — they always do.
    This is the question that follows, and the answer is a different one."""
    rows = replay_mod.sweep(a_wave(n=2000, period=90),
                            settings(exit_signal_mode='zscore'),
                            0.01, 1.0, thresholds=[0.5, 1.0, 1.5, 2.0])
    assert [r['entry_threshold'] for r in rows['rows']] == [0.5, 1.0, 1.5, 2.0]
    best = rows['best']
    if best is not None:
        assert best['net'] > 0
        assert best['enough_to_judge'] is True
        # never beaten by a row with more money that nobody can judge
        for r in rows['rows']:
            if r['net'] is not None and r['net'] > best['net']:
                assert not r['enough_to_judge']


def test_a_sweep_row_with_too_few_trades_is_never_the_best():
    """One lucky trade with a percentage sign after it is not a finding."""
    rows = [
        {'entry_threshold': 1.0, 'net': 5000.0, 'enough_to_judge': False},
        {'entry_threshold': 2.0, 'net': 120.0, 'enough_to_judge': True},
    ]
    assert replay_mod.best_of(rows)['entry_threshold'] == 2.0
    # and where nothing clears its costs, there is no best
    assert replay_mod.best_of(
        [{'entry_threshold': 1.0, 'net': -50.0, 'enough_to_judge': True}]) is None


def test_costs_that_swallow_the_move_turn_a_winning_level_into_a_losing_one():
    """The control the whole feature exists for: the same series, the same
    threshold, and a round trip big enough to eat it."""
    cheap = replay_mod.replay(a_wave(), settings(exit_signal_mode='zscore'),
                              0.01, 1.0)
    dear = replay_mod.replay(
        a_wave(), settings(exit_signal_mode='zscore',
                           commission_per_contract=40.0),
        0.01, 1.0)
    assert cheap['summary']['net'] > dear['summary']['net']
    assert dear['summary']['net'] < 0


def test_a_filter_that_withheld_every_entry_says_SO_not_the_threshold():
    """The reason this exists: the edge filter can withhold every entry at
    every threshold, and reporting 'nothing crossed the threshold' sends the
    desk to change the number that was never the problem. The replay
    carries the signal's own words."""
    blocked = replay_mod.replay(
        a_wave(), settings(edge_filter_enabled=True, min_std_multiple=99.0),
        0.01, 1.0)
    assert blocked['summary']['trades'] == 0
    assert 'edge' in blocked['blocked_by']
    assert blocked['withheld']

    passing = replay_mod.replay(                      # the control
        a_wave(), settings(edge_filter_enabled=False,
                           exit_signal_mode='zscore'), 0.01, 1.0)
    assert passing['summary']['trades'] > 0


def test_a_cooldown_is_never_the_headline_reason_for_no_trades():
    """It is a consequence of trading, so it cannot explain having taken no
    trades at all — and it would mask the filter that did."""
    out = replay_mod.replay(
        a_wave(), settings(entry_cooldown_seconds=600,
                           edge_filter_enabled=True, min_std_multiple=99.0),
        0.01, 1.0)
    assert out['summary']['trades'] == 0
    assert 'cooling down' not in (out['blocked_by'] or '')
    assert 'edge' in out['blocked_by']
