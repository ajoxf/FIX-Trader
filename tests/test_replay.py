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
    # These samples are mids only, so the spread is a guess and says so
    assert 'carries a book' in a['book'] and 'guess' in a['book']
    assert a['book_recorded'] == 0 and a['book_assumed'] == 600
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


# -- the recorded book ------------------------------------------------------

def a_recorded_wave(n=600, half_spread=0.005, **kw):
    """A series carrying the book it came from, as the engine records now."""
    from fixtrader.database import RecordedSample
    return [RecordedSample(ts, px, round(px - half_spread, 6),
                           round(px + half_spread, 6))
            for ts, px in a_wave(n=n, **kw)]


def test_a_recorded_book_is_used_and_nothing_is_assumed():
    out = replay_mod.replay(a_recorded_wave(),
                            settings(exit_signal_mode='zscore'), 0.01, 1.0)
    assert out['book'] == {'recorded': 600, 'assumed': 0}
    assert 'nothing about the spread was assumed' in out['assumptions']['book']


def test_a_recording_of_mids_alone_still_replays_and_says_it_guessed():
    """Every row written before the book was recorded is a mid. Those still
    replay — they just cannot claim to know the spread."""
    out = replay_mod.replay(a_wave(), settings(exit_signal_mode='zscore'),
                            0.01, 1.0)
    assert out['book'] == {'recorded': 0, 'assumed': 600}
    assert 'guess' in out['assumptions']['book']


def test_a_recording_that_changed_mid_way_reports_BOTH_counts():
    """A desk that upgrades has a database with both kinds in it, and a
    figure two thirds measured is not a measured figure."""
    rows = a_recorded_wave(n=400) + a_wave(n=200)[:200]
    out = replay_mod.replay(rows, settings(exit_signal_mode='zscore'),
                            0.01, 1.0)
    assert out['book']['recorded'] == 400 and out['book']['assumed'] == 200
    said = out['assumptions']['book']
    assert '400' in said and '200' in said


def test_a_recorded_book_beats_the_assumption_on_the_exits_it_prices():
    """The point of recording it. A real wide book and an assumed tight one
    price the same exits differently, and the recorded answer is the one
    that happened."""
    tight_assumption = replay_mod.replay(
        a_wave(), settings(exit_signal_mode='zscore'), 0.01, 1.0,
        assumed_spread_ticks=1.0)
    really_wide = replay_mod.replay(
        a_recorded_wave(half_spread=0.04),
        settings(exit_signal_mode='zscore'), 0.01, 1.0,
        assumed_spread_ticks=1.0)
    assert tight_assumption['summary']['net'] is not None
    assert really_wide['summary']['net'] is not None
    # the recording knows the book was wide; the assumption never would have
    assert really_wide['summary']['net'] < tight_assumption['summary']['net']


def test_a_crossed_or_half_recorded_book_falls_back_rather_than_trusting_it():
    """A bid above its ask is bad data, not an arbitrage, and one side alone
    is not a book. Either way the assumption is used and COUNTED as one."""
    from fixtrader.database import RecordedSample
    rows = [RecordedSample(ts, px, px + 0.02, px - 0.02)      # crossed
            for ts, px in a_wave(n=300)]
    rows += [RecordedSample(ts, px, px - 0.005, None)         # half a book
             for ts, px in a_wave(n=300)]
    out = replay_mod.replay(rows, settings(exit_signal_mode='zscore'),
                            0.01, 1.0)
    assert out['book'] == {'recorded': 0, 'assumed': 600}


# -- a database written before the book was recorded ------------------------

def test_an_older_database_gains_the_columns_and_keeps_its_rows(tmp_path):
    """Databases are already running on the desk. `CREATE TABLE IF NOT
    EXISTS` does nothing to a table that exists, so the columns are added by
    migration — additively, because a migration that rewrote history would
    rewrite the recordings the replay reads."""
    import sqlite3
    from datetime import datetime, timezone
    from fixtrader.database import Database

    path = str(tmp_path / 'old.db')
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE samples (contract_key TEXT NOT NULL, "
                "ts TEXT NOT NULL, price REAL NOT NULL)")
    when = datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat()
    old.execute("INSERT INTO samples VALUES (?,?,?)", ('fef', when, 0.61))
    old.commit(); old.close()

    db = Database(path)                       # opening it migrates it
    rows = db.samples_between('fef')
    assert len(rows) == 1
    assert rows[0].price == 0.61
    assert rows[0].has_book is False          # not recorded, not zero-width

    # and it records a book from here on, in the same table
    db.save_samples('fef', [(datetime.now(timezone.utc), 0.62, 0.615, 0.625,
                             25.0, 30.0)])
    rows = db.samples_between('fef')
    assert [r.has_book for r in rows] == [False, True]


def test_migrating_twice_is_harmless(tmp_path):
    from datetime import datetime, timezone
    from fixtrader.database import Database
    path = str(tmp_path / 'twice.db')
    db = Database(path)
    db.save_samples('fef', [(datetime.now(timezone.utc), 0.5, 0.49, 0.51,
                             1.0, 1.0)])
    again = Database(path)                    # a second process, or a restart
    assert again.samples_between('fef')[0].bid == 0.49


def test_the_crash_that_took_the_engine_down_mid_fill(tmp_path):
    """Reported from a desk: `table positions has no column named
    opened_qty`, in a restart loop, with a position open.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that exists, so
    every column added after that desk first ran was missing from its
    database — and the first write that named one killed the engine while
    it was applying a FILL.
    """
    import sqlite3
    from datetime import datetime, timezone
    from fixtrader.database import Database
    from fixtrader.models import Position, Side

    path = str(tmp_path / 'old.db')
    old = sqlite3.connect(path)
    # positions as it was BEFORE opened_qty, entry_std, tickets and the rest
    old.execute("""CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, contract_key TEXT NOT NULL,
        side TEXT NOT NULL, qty REAL NOT NULL, avg_price REAL NOT NULL,
        opened_at TEXT, closed_at TEXT)""")
    old.execute("INSERT INTO positions (contract_key, side, qty, avg_price)"
                " VALUES ('fef', 'BUY', 5, 0.48)")
    old.commit(); old.close()

    db = Database(path)                        # opening it migrates it
    saved = db.save_position(Position(
        contract_key='fef', side=Side.BUY, qty=5.0, opened_qty=5.0,
        avg_price=0.48, opened_at=datetime.now(timezone.utc),
        entry_z=-2.14, entry_std=0.08, tickets=['E1', 'E2']))
    assert saved is not None

    # the row that was already there is untouched — additive, never a rewrite
    with sqlite3.connect(path) as check:
        rows = check.execute("SELECT contract_key, qty, avg_price FROM"
                             " positions ORDER BY id").fetchall()
    assert rows[0] == ('fef', 5.0, 0.48)


def test_the_migration_reads_the_schema_rather_than_a_list(tmp_path):
    """A hand-kept list of additions is what drifted in the first place. A
    column is migrated by having been DECLARED, with nothing else to
    remember — so a table stripped to one column comes back whole."""
    import sqlite3
    from fixtrader.database import Database, _declared_tables, SCHEMA

    path = str(tmp_path / 'bare.db')
    bare = sqlite3.connect(path)
    bare.execute("CREATE TABLE sd_touches (contract_key TEXT NOT NULL)")
    bare.commit(); bare.close()

    Database(path)
    with sqlite3.connect(path) as check:
        have = {r[1] for r in check.execute("PRAGMA table_info(sd_touches)")}
    declared = {name for name, _ in _declared_tables(SCHEMA)['sd_touches']}
    assert declared <= have


def test_a_composite_primary_key_is_not_read_as_a_column(tmp_path):
    """`PRIMARY KEY (venue, exec_id)` split naively yields a column called
    `exec_id)`, and the migration then fails on every startup."""
    from fixtrader.database import _declared_tables, SCHEMA
    names = [name for name, _ in _declared_tables(SCHEMA)['fills']]
    assert 'exec_id' in names
    assert not any(')' in n or n.upper() == 'PRIMARY' for n in names)
