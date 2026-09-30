"""The algo: enter at 2.5 sigma on the side you would trade, leave once the
round trip AND a profit on the margin are covered.

Every blocked entry has a CONTROL that unblocks it and asserts the opposite —
a guard test without one only proves the code never fires.
"""
import pytest

from fixtrader import signals
from fixtrader.models import BookTop, ExitReason, Position, Side
from tests.conftest import at, warm, window as make_window

TICK, VALUE = 0.01, 1.0          # $100 a point


def window(entry=2.5, n=30):
    """Bands frozen after warm-up (mean 0.5, sigma about 0.1017), so a price
    placed at a z is still at that z when it is read back."""
    w = make_window(n, interval=1e9, threshold=entry, key='fef')
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(n)])
    return w


def book_at(w, z, size=100.0, width_ticks=1):
    """A book whose MID is at z, `width_ticks` wide."""
    px = w.price_at_z(z)
    half = width_ticks * TICK / 2
    return BookTop(bid=px - half, ask=px + half, bid_size=size, ask_size=size,
                   ts=at(0))


def cfg(**over):
    base = {
        'entry_threshold': 2.5, 'max_entry_z': 3.5, 'confirm_samples': 3,
        'trade_direction': 'BOTH', 'stop_loss_z': 4.0, 'stop_loss_money': 0.0,
        'max_hold_minutes': 0.0, 'exit_at_mean': True,
        'min_book_size': 0.0, 'max_book_spread_ticks': 0.0,
        'quantity': 5.0, 'max_position': 0.0, 'max_trades_per_day': 0.0,
        'daily_max_loss': 0.0, 'entry_cooldown_seconds': 0.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15, 'slippage_budget_ticks': 0.0,
        # $19 round trip on 5 lots; 2% of $500 x 5 = $50 target: 0.138 points,
        # well inside a return from 2.6 sigma to the mean.
        'profit_target_pct': 2.0, 'margin_per_contract': 500.0,
    }
    base.update(over)
    return base


def entry(w, z, settings=None, book=None, **kw):
    """Put the window's last price AND the book at z."""
    w.add(w.price_at_z(z), at(999))
    return signals.entry_signal(w, book or book_at(w, z), settings or cfg(),
                                TICK, VALUE, at(1000), **kw)


# -- which side, on which price ----------------------------------------------

def test_a_spread_whose_BID_is_through_plus_threshold_is_sold():
    sig = entry(window(), 2.6)
    assert sig.action == "OPEN" and sig.side is Side.SELL
    assert "bid z" in sig.reason


def test_a_spread_whose_OFFER_is_through_minus_threshold_is_bought():
    sig = entry(window(), -2.6)
    assert sig.action == "OPEN" and sig.side is Side.BUY
    assert "offer z" in sig.reason


def test_the_decision_is_on_the_executable_side_not_the_mid():
    """A wide book whose MID is through 2.5 but whose BID is not: a sale
    there would fill below the level. The control is a tight book."""
    w = window()
    wide = book_at(w, 2.6, width_ticks=8)            # bid about 0.4 sigma lower
    assert signals.side_z(w, wide)[0] < 2.5
    assert entry(w, 2.6, book=wide).action == "NONE"
    assert entry(w, 2.6).action == "OPEN"             # control: 1 tick wide


def test_inside_the_threshold_it_is_armed_and_waiting_not_blocked():
    sig = entry(window(), 1.2)
    assert sig.action == "NONE" and sig.blocked_by is None


def test_side_through_names_the_side_or_nothing():
    w = window()
    assert signals.side_through(w, book_at(w, 2.6), cfg()) is Side.SELL
    assert signals.side_through(w, book_at(w, -2.6), cfg()) is Side.BUY
    assert signals.side_through(w, book_at(w, 1.0), cfg()) is None


# -- warm-up and switches -----------------------------------------------------

def test_nothing_enters_before_the_window_is_warm():
    w = make_window(400, interval=1e9, threshold=2.5, key='fef')
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(100)])
    sig = signals.entry_signal(w, book_at(w, 2.6), cfg(), TICK, VALUE, at(1000))
    assert sig.action == "NONE"
    assert "collecting" in sig.blocked_by and "minutes" in sig.blocked_by
    # control: warm it and the same z enters
    warm(w, [0.5 + (0.1 if i % 2 else -0.1) for i in range(300)], start=100)
    assert entry(w, 2.6).action == "OPEN"


def test_the_master_switch_stands_every_contract_down():
    assert entry(window(), 2.6, master_on=False).blocked_by is not None
    assert entry(window(), 2.6, master_on=True).action == "OPEN"     # control


def test_algo_off_is_idle_and_is_not_reported_as_blocked():
    """'Blocked' means something is holding it back. Off is not that."""
    sig = entry(window(), 2.6, algo_on=False)
    assert sig.action == "NONE" and sig.blocked_by is None


# -- direction ----------------------------------------------------------------

def test_sell_only_passes_over_a_long_and_says_which_side():
    w = window()
    sig = entry(w, -2.6, cfg(trade_direction='SELL_ONLY'))
    assert sig.action == "NONE" and "long entries are off" in sig.blocked_by
    assert entry(w, 2.6, cfg(trade_direction='SELL_ONLY')).action == "OPEN"
    assert entry(w, -2.6, cfg(trade_direction='BOTH')).action == "OPEN"


def test_buy_only_passes_over_a_short():
    w = window()
    sig = entry(w, 2.6, cfg(trade_direction='BUY_ONLY'))
    assert sig.action == "NONE" and "short entries are off" in sig.blocked_by
    assert entry(w, 2.6, cfg(trade_direction='BOTH')).action == "OPEN"


def test_an_unrecognised_direction_is_both_never_a_silent_refusal():
    from fixtrader.config import ContractConfig
    c = ContractConfig.from_dict('x', {'symbol': 'X',
                                       'trade_direction': 'sideways'})
    assert c.settings_with_defaults({})['trade_direction'] == 'BOTH'


# -- the ceiling, the confirmation, the cooldown ------------------------------

def test_beyond_the_ceiling_is_a_blow_out_not_an_entry():
    w = window()
    sig = entry(w, 3.7)
    assert sig.action == "NONE" and "ceiling" in sig.blocked_by
    # control: a wider ceiling, and a stop beyond it
    assert entry(w, 3.7, cfg(max_entry_z=4.5, stop_loss_z=5.0)).action == "OPEN"


def test_the_ceiling_never_sits_beyond_the_stop():
    """An entry past the stop would be stopped out on arrival."""
    w = window()
    sig = entry(w, 3.7, cfg(max_entry_z=9.0, stop_loss_z=3.6))
    assert "ceiling" in sig.blocked_by


def test_it_waits_for_the_confirming_samples():
    w = window()
    sig = entry(w, 2.6, confirmed=1)
    assert sig.action == "NONE" and "confirming" in sig.blocked_by
    assert "1 of 3" in sig.blocked_by
    assert entry(w, 2.6, confirmed=3).action == "OPEN"               # control
    assert entry(w, 2.6, cfg(confirm_samples=1), confirmed=1).action == "OPEN"


def test_the_cooldown_withholds_and_says_how_long_is_left():
    w = window()
    sig = entry(w, 2.6, cfg(entry_cooldown_seconds=300.0),
                last_trade_at=at(900))
    assert "cooling down" in sig.blocked_by and "200s" in sig.blocked_by
    assert entry(w, 2.6, cfg(entry_cooldown_seconds=300.0),
                 last_trade_at=at(600)).action == "OPEN"


# -- can it pay ---------------------------------------------------------------

def test_no_margin_entered_means_no_target_and_no_entry():
    """TT does not report margin. Without the one the trader enters, the
    target has no base — and a trade with no target is not taken."""
    w = window()
    sig = entry(w, 2.6, cfg(margin_per_contract=0.0))
    assert sig.action == "NONE" and "no margin entered" in sig.blocked_by
    assert entry(w, 2.6).action == "OPEN"                            # control


def test_a_target_beyond_the_mean_is_not_entered():
    """The trade is a bet on a return to the mean. A target a full return
    does not reach is a trade that can only end at a stop."""
    w = window()
    sig = entry(w, 2.6, cfg(profit_target_pct=40.0))
    assert sig.action == "NONE" and "does not reach" in sig.blocked_by
    assert entry(w, 2.6, cfg(profit_target_pct=2.0)).action == "OPEN"


# -- the limits that were already there -----------------------------------------

def test_the_position_limit_withholds_and_names_the_numbers():
    w = window()
    sig = entry(w, 2.6, cfg(max_position=5.0), open_qty=5.0)
    assert "position limit" in sig.blocked_by
    assert entry(w, 2.6, cfg(max_position=20.0), open_qty=5.0).action == "OPEN"


def test_the_daily_loss_limit_withholds():
    w = window()
    sig = entry(w, 2.6, cfg(daily_max_loss=1000.0), pnl_today=-1200.0)
    assert "daily loss limit" in sig.blocked_by
    assert entry(w, 2.6, cfg(daily_max_loss=1000.0),
                 pnl_today=-200.0).action == "OPEN"


def test_a_stale_quote_withholds_an_entry():
    w = window()
    assert entry(w, 2.6, quote_stale=True).blocked_by is not None
    assert entry(w, 2.6, quote_stale=False).action == "OPEN"


def test_a_one_sided_book_is_not_a_market():
    w = window()
    sig = entry(w, 2.6, book=BookTop(bid=0.5, ask=None))
    assert sig.action == "NONE" and "one-sided" in sig.blocked_by


def test_a_book_with_no_published_size_is_unknown_not_infinite():
    w = window()
    b = book_at(w, 2.6)
    b.bid_size = None
    sig = entry(w, 2.6, cfg(min_book_size=10.0), book=b)
    assert "no size" in sig.blocked_by


def test_a_book_too_wide_to_cross_withholds():
    w = window()
    wide = book_at(w, 2.9, width_ticks=4)
    sig = entry(w, 2.9, cfg(max_book_spread_ticks=3.0), book=wide)
    assert "wide" in sig.blocked_by
    assert entry(w, 2.9, cfg(max_book_spread_ticks=20.0),
                 book=wide).action == "OPEN"


# -- exits --------------------------------------------------------------------

def position(w, side=Side.SELL, entry_z=2.6, target=None, qty=5.0):
    return Position(contract_key='fef', side=side, qty=qty, opened_qty=qty,
                    avg_price=w.price_at_z(entry_z), opened_at=at(0),
                    target_price=target, entry_z=entry_z)


def exit_for(w, z, pos, settings=None, book=None, **kw):
    w.add(w.price_at_z(z), at(3599))
    return signals.exit_signal(w, book or book_at(w, z), pos,
                               settings or cfg(), TICK, VALUE, at(3600), **kw)


def test_a_short_leaves_when_the_offer_reaches_its_target():
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(1.2))
    sig = exit_for(w, 1.1, pos)
    assert sig.action == "CLOSE" and sig.side is Side.BUY
    assert sig.exit_reason is ExitReason.TARGET


def test_a_long_leaves_when_the_bid_reaches_its_target():
    w = window()
    pos = position(w, Side.BUY, entry_z=-2.6, target=w.price_at_z(-1.2))
    sig = exit_for(w, -1.1, pos)
    assert sig.action == "CLOSE" and sig.side is Side.SELL
    assert sig.exit_reason is ExitReason.TARGET


def test_the_target_is_measured_on_the_closing_side_not_the_mid():
    """A short buys back on the OFFER. Marking it at the mid flatters every
    open position by half the spread."""
    w = window()
    px = w.price_at_z(1.2)
    b = BookTop(bid=px - 0.05, ask=px + 0.05, bid_size=10, ask_size=10)
    pos = position(w, Side.SELL, target=px)
    sig = signals.exit_signal(w, b, pos, cfg(exit_at_mean=False), TICK, VALUE,
                              at(3600))
    assert sig.action == "NONE"


def test_the_z_stop_reads_the_closing_side():
    """A short is stopped when the OFFER it would buy back on is at +4."""
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(1.2))
    mid_just_under = book_at(w, 3.97, width_ticks=2)   # offer about 4.07
    sig = exit_for(w, 3.97, pos, book=mid_just_under)
    assert sig.action == "CLOSE" and sig.exit_reason is ExitReason.STOP_LOSS
    # control: a tight book with its offer under 4 is not stopped
    assert exit_for(w, 3.9, pos).action == "NONE"


def test_the_stop_fires_regardless_of_every_entry_filter():
    """A position on a contract whose entries are all withheld — no margin,
    the direction switched off — must still be able to get out."""
    w = window()
    shut = cfg(margin_per_contract=0.0, trade_direction='BUY_ONLY')
    assert entry(w, 2.6, shut).action == "NONE"
    pos = position(w, Side.SELL, target=w.price_at_z(1.2))
    sig = exit_for(w, 4.2, pos, shut)
    assert sig.action == "CLOSE" and sig.exit_reason is ExitReason.STOP_LOSS


def test_the_money_stop_closes_on_net_loss():
    """Net P&L at or below -stop_loss_money per contract. The control: off
    (0) and the same loss is held."""
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(1.2))
    # short 5 from z 2.6, now offer at z 3.2: about -0.06 points x $500
    sig = exit_for(w, 3.2, pos, cfg(stop_loss_money=5.0))
    assert sig.action == "CLOSE" and sig.exit_reason is ExitReason.MONEY_STOP
    assert exit_for(w, 3.2, pos, cfg(stop_loss_money=0.0)).action == "NONE"


def test_the_session_cutoff_closes_whatever_the_z_says():
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(-9))
    sig = exit_for(w, 2.0, pos, session_flat_due=True)
    assert sig.exit_reason is ExitReason.SESSION_FLAT


def test_the_time_stop_closes_whatever_the_pnl():
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(-9))
    sig = exit_for(w, 2.8, pos, cfg(max_hold_minutes=30.0))
    assert sig.exit_reason is ExitReason.TIME_STOP
    assert exit_for(w, 2.8, pos, cfg(max_hold_minutes=0.0)).action == "NONE"


def test_back_at_the_mean_and_paid_closes_short_of_the_target():
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(-9))    # never reached
    sig = exit_for(w, -0.1, pos)
    assert sig.action == "CLOSE" and sig.exit_reason is ExitReason.MEAN
    # control: switched off, the same price is held for the target
    assert exit_for(w, -0.1, pos, cfg(exit_at_mean=False)).action == "NONE"


def test_back_at_the_mean_but_not_paid_is_held():
    """An entry close to the mean, back at the mean: the round trip is not
    covered, so this is not a profit to take."""
    w = window()
    pos = position(w, Side.SELL, entry_z=0.05, target=w.price_at_z(-9))
    assert exit_for(w, -0.1, pos).action == "NONE"


def test_a_closed_position_produces_nothing():
    w = window()
    pos = position(w, Side.SELL, target=w.price_at_z(1.2))
    pos.closed_at = at(10)
    assert exit_for(w, 1.1, pos).action == "NONE"
