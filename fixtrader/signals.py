"""The algo: what to do, and — when it is doing nothing — why.

Pure. Given a window, a book, a position and some settings it returns an
intent; it sends nothing, stores nothing and reads no clock of its own. That
is what makes every rule below testable without a venue.

The one rule that governs the shape of this module:

    **Filters gate ENTRIES. Nothing here may withhold an exit.**

Every blocking check lives in `entry_signal`. `exit_signal` consults no
filter, no guard and no limit — a position that should be closed is closed
whether or not the Hurst exponent likes the regime today.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from . import costs as costs_mod
from .models import (ExitReason, Intent, Position, Side, Signal, TargetBasis)
from .stats import StatsWindow


def _f(settings: Dict[str, Any], key: str, default: float) -> float:
    v = settings.get(key, default)
    return default if v is None else float(v)


def round_trip_ticks(qty: float, tick_size, tick_value,
                     settings: Dict[str, Any]) -> Optional[float]:
    """The round trip expressed in ticks of this contract, or None."""
    return costs_mod.cost_breakdown(qty, tick_size, tick_value,
                                    settings).get('round_trip_ticks')


def edge_ratio(window: StatsWindow, qty: float, tick_size, tick_value,
               settings: Dict[str, Any]) -> Optional[float]:
    """How many times sigma covers a round trip.

    This is the number the whole strategy turns on: if the spread does not
    move further than it costs to trade, the trade cannot pay however
    reliably it reverts. None where either side of the comparison is unknown
    — and an unknown edge blocks an entry, because "we could not tell" is not
    "it is fine".
    """
    if window.std is None or window.std <= 0:
        return None
    rt_points = costs_mod.cost_breakdown(
        qty, tick_size, tick_value, settings).get('round_trip_points')
    if rt_points is None or rt_points <= 0:
        return None
    return window.std / rt_points


def entry_signal(window: StatsWindow, book, settings: Dict[str, Any],
                 tick_size, tick_value, now: datetime,
                 algo_on: bool = True, master_on: bool = True,
                 open_qty: float = 0.0, trades_today: int = 0,
                 pnl_today: float = 0.0,
                 last_trade_at: Optional[datetime] = None,
                 quote_stale: bool = False, jump_settling: bool = False,
                 in_session: bool = True) -> Signal:
    """Should a position be opened, and if not, what is stopping it.

    `blocked_by` is written for the window, not for a log: it names the
    number that failed and the threshold it failed against, so the trader can
    see whether to change the setting or the contract.
    """
    sig = Signal(contract_key=window.contract_key, action="NONE", ts=now)
    qty = _f(settings, 'quantity', 1.0)

    if not master_on:
        sig.blocked_by = "the desk-wide algo master switch is off"
        return sig
    if not algo_on:
        return sig                       # IDLE is not "blocked", it is off
    if not window.is_warm:
        sig.blocked_by = (f"collecting — {window.samples} of {window.lookback} "
                          f"samples ({window.warm_pct:.0f}%)")
        return sig
    if window.z is None or window.std is None or window.std <= 0:
        sig.blocked_by = "no sigma yet — the window has not produced statistics"
        return sig
    if book is None or not book.usable:
        sig.blocked_by = "the book is one-sided or crossed"
        return sig
    if quote_stale:
        sig.blocked_by = "the quote is stale — entries withheld"
        return sig
    if jump_settling:
        sig.blocked_by = "the price jumped — settling before any new entry"
        return sig
    if not in_session:
        sig.blocked_by = "outside this contract's trading hours"
        return sig

    threshold = _f(settings, 'entry_threshold', 2.0)
    stop_z = _f(settings, 'stop_loss_z', 4.0)

    # Which way, if any. A spread far ABOVE its mean is sold.
    if window.z >= threshold:
        side = Side.SELL
    elif window.z <= -threshold:
        side = Side.BUY
    else:
        return sig                       # armed and waiting: not blocked

    # Past the stop already: too late to enter, not early. Entering here buys
    # a position that is immediately eligible to be stopped out.
    if abs(window.z) >= stop_z:
        sig.blocked_by = (f"z {window.z:+.2f} is already past the stop at "
                          f"{stop_z:.2f}")
        return sig

    max_position = _f(settings, 'max_position', 0.0)
    if max_position and abs(open_qty) + qty > max_position:
        sig.blocked_by = (f"position limit — {abs(open_qty):g} open, "
                          f"{max_position:g} allowed")
        return sig

    max_trades = _f(settings, 'max_trades_per_day', 0.0)
    if max_trades and trades_today >= max_trades:
        sig.blocked_by = f"{trades_today:g} trades today, limit {max_trades:g}"
        return sig

    daily_loss = _f(settings, 'daily_max_loss', 0.0)
    if daily_loss and pnl_today <= -abs(daily_loss):
        sig.blocked_by = (f"daily loss limit reached "
                          f"({pnl_today:+,.0f} against {-abs(daily_loss):+,.0f})")
        return sig

    cooldown = _f(settings, 'entry_cooldown_seconds', 0.0)
    if cooldown and last_trade_at is not None:
        waited = (now - last_trade_at).total_seconds()
        if waited < cooldown:
            sig.blocked_by = (f"cooling down — {cooldown - waited:.0f}s of "
                              f"{cooldown:.0f}s left")
            return sig

    # -- the filters, entries only ---------------------------------------
    if settings.get('hurst_enabled', True):
        h_max = _f(settings, 'hurst_threshold', 0.5)
        if window.hurst is None:
            sig.blocked_by = "Hurst not measured yet"
            return sig
        if window.hurst >= h_max:
            sig.blocked_by = (f"Hurst {window.hurst:.2f} is above {h_max:.2f} "
                              f"— trending, not mean-reverting")
            return sig

    if settings.get('edge_filter_enabled', True):
        min_mult = _f(settings, 'min_std_multiple', 1.5)
        ratio = edge_ratio(window, qty, tick_size, tick_value, settings)
        if ratio is None:
            sig.blocked_by = "the round trip cannot be priced — edge unknown"
            return sig
        if ratio < min_mult:
            rt = costs_mod.cost_breakdown(qty, tick_size, tick_value,
                                          settings).get('round_trip_points')
            sig.blocked_by = (f"edge {ratio:.1f}x — sigma {window.std:.4f} "
                              f"against a round trip of {rt:.4f}, "
                              f"below the {min_mult:.1f}x required")
            return sig

    if settings.get('half_life_enabled', False):
        hl_max = _f(settings, 'max_half_life', 60.0)
        if window.half_life is None:
            sig.blocked_by = "no half-life — the series is not mean-reverting"
            return sig
        if window.half_life > hl_max:
            sig.blocked_by = (f"half-life {window.half_life:.0f} is longer "
                              f"than the {hl_max:.0f} allowed")
            return sig

    min_book = _f(settings, 'min_book_size', 0.0)
    if min_book:
        sizes = [book.bid_size, book.ask_size]
        if any(s is None for s in sizes):
            sig.blocked_by = "the venue publishes no size — book depth unknown"
            return sig
        if min(sizes) < min_book:
            sig.blocked_by = (f"book is {min(sizes):g} against the "
                              f"{min_book:g} required")
            return sig

    max_width = _f(settings, 'max_book_spread_ticks', 0.0)
    if max_width and tick_size:
        width_ticks = (book.width or 0.0) / tick_size
        if width_ticks > max_width:
            sig.blocked_by = (f"book is {width_ticks:.1f} ticks wide, "
                              f"wider than the {max_width:g} allowed")
            return sig

    sig.action = "OPEN"
    sig.side = side
    sig.qty = qty
    sig.reason = f"z {window.z:+.2f} through {threshold:.2f}"
    return sig


def exit_signal(window: StatsWindow, book, position: Position,
                settings: Dict[str, Any], tick_size, tick_value,
                now: datetime, in_session: bool = True,
                session_flat_due: bool = False) -> Signal:
    """Should this position be closed, and for which recorded reason.

    **No filter, guard or limit appears in this function.** Everything here
    is a reason to get OUT, and the system must be able to get out of a
    position on a contract whose entries are all being withheld.
    """
    sig = Signal(contract_key=position.contract_key, action="NONE", ts=now)
    if position is None or not position.is_open:
        return sig

    closing_side = position.side.opposite
    # A position is marked at the side it would CLOSE on: a long leaves on the
    # bid, a short buys back on the offer. Marking at the mid flatters every
    # open position by half the spread.
    close_px = book.executable(closing_side) if book is not None else None

    # 1. the stop, first, because it is the one that must never be missed
    stop_z = _f(settings, 'stop_loss_z', 4.0)
    if window.z is not None and stop_z:
        if position.side is Side.BUY and window.z <= -stop_z:
            sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
            sig.exit_reason = ExitReason.STOP_LOSS
            sig.reason = f"z {window.z:+.2f} through the stop at -{stop_z:.2f}"
            return sig
        if position.side is Side.SELL and window.z >= stop_z:
            sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
            sig.exit_reason = ExitReason.STOP_LOSS
            sig.reason = f"z {window.z:+.2f} through the stop at {stop_z:.2f}"
            return sig

    # 2. the session cutoff and the time stop — both decided, not optional
    if session_flat_due:
        sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
        sig.exit_reason = ExitReason.SESSION_FLAT
        sig.reason = "the session cutoff on the venue's clock"
        return sig

    time_stop = _f(settings, 'max_hold_minutes', 0.0)
    if time_stop and position.opened_at is not None:
        held = (now - position.opened_at).total_seconds() / 60.0
        if held >= time_stop:
            sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
            sig.exit_reason = ExitReason.TIME_STOP
            sig.reason = f"held {held:.0f}m, limit {time_stop:.0f}m"
            return sig

    # 3. the profit target, or the z, per this contract's exit mode
    mode = str(settings.get('exit_signal_mode', 'profit') or 'profit').lower()

    target_hit = False
    if position.target_price is not None and close_px is not None:
        # A long leaves by SELLING into the bid, so the bid must reach UP to
        # the target; a short buys back on the offer, which must come DOWN.
        target_hit = (close_px >= position.target_price
                      if position.side is Side.BUY
                      else close_px <= position.target_price)

    z_exit_hit = False
    if window.z is not None:
        exit_z = _f(settings, 'exit_threshold', 0.5)
        z_exit_hit = (window.z >= -exit_z if position.side is Side.BUY
                      else window.z <= exit_z)

    fire = (target_hit if mode == 'profit'
            else z_exit_hit if mode == 'zscore'
            else (target_hit or z_exit_hit))

    if fire:
        sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
        if mode == 'zscore' or (mode == 'hybrid' and not target_hit):
            sig.exit_reason = ExitReason.ZSCORE
            sig.reason = f"z reverted to {window.z:+.2f}"
        else:
            sig.exit_reason = ExitReason.TARGET
            sig.reason = (f"{closing_side.value.lower()} side reached the "
                          f"target at {position.target_price:.4f}")
    return sig
