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
from typing import Any, Dict, Optional, Tuple

from . import costs as costs_mod
from .models import ExitReason, Position, Side, Signal
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
    """How many times sigma covers a round trip — shown on the window as a
    reading. The entry decision uses `can_it_pay`, which asks the sharper
    question: does a return to the mean pay the round trip AND the target."""
    if window.std is None or window.std <= 0:
        return None
    rt_points = costs_mod.cost_breakdown(
        qty, tick_size, tick_value, settings).get('round_trip_points')
    if rt_points is None or rt_points <= 0:
        return None
    return window.std / rt_points


def side_z(window: StatsWindow, book) -> Tuple[Optional[float], Optional[float]]:
    """(z of the BID, z of the OFFER) against the standing bands.

    A SHORT is sold into the bid, so it is the bid's z that says whether the
    spread is rich enough to sell; a LONG is bought on the offer. Deciding on
    the mid would fire on a level no order could get.
    """
    if book is None:
        return None, None
    return window.z_of(book.bid), window.z_of(book.ask)


def side_through(window: StatsWindow, book,
                 settings: Dict[str, Any]) -> Optional[Side]:
    """Which side's z is through the entry threshold on this sample, if any —
    what the engine counts consecutive samples of, for the confirmation."""
    z_bid, z_ask = side_z(window, book)
    threshold = _f(settings, 'entry_threshold', 2.5)
    sell = z_bid is not None and z_bid >= threshold
    buy = z_ask is not None and z_ask <= -threshold
    if sell and buy:                   # only on a book wider than 5 sigma
        return Side.SELL if abs(z_bid) >= abs(z_ask) else Side.BUY
    return Side.SELL if sell else Side.BUY if buy else None


def can_it_pay(window: StatsWindow, side: Side, entry_price: float, qty: float,
               tick_size, tick_value,
               settings: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """The target price for an entry here, or why there is none.

    The target is break-even plus `profit_target_pct` of the margin entered
    for the contract. It must lie between the entry and the MEAN: the trade
    is a bet on a return to the mean, and a target beyond it is one a full
    reversion does not reach — a trade entered only to sit until a stop.
    """
    margin = costs_mod.configured_margin(settings, qty)
    if margin is None:
        return None, ("no margin entered for this contract — the profit "
                      "target is a % of it; set Margin per contract")
    target = costs_mod.target_price(entry_price, side, qty, tick_size,
                                    tick_value, settings, margin_locked=margin)
    if target is None:
        return None, "the round trip cannot be priced — no tick value"
    reach = window.mean
    if reach is None:
        return None, "no mean yet"
    # A LONG's target is above its entry, so it is out of reach when it sits
    # ABOVE the mean; a SHORT's is below its entry, out of reach BELOW it.
    beyond = target > reach if side is Side.BUY else target < reach
    if beyond:
        return None, (f"a return to the mean ({reach:.4f}) does not reach "
                      f"the target ({target:.4f}) — costs plus "
                      f"{_f(settings, 'profit_target_pct', 0):g}% of margin "
                      f"need more than this move")
    return target, None


def entry_signal(window: StatsWindow, book, settings: Dict[str, Any],
                 tick_size, tick_value, now: datetime,
                 algo_on: bool = True, master_on: bool = True,
                 open_qty: float = 0.0, trades_today: int = 0,
                 pnl_today: float = 0.0,
                 last_trade_at: Optional[datetime] = None,
                 quote_stale: bool = False, jump_settling: bool = False,
                 in_session: bool = True,
                 confirmed: Optional[int] = None) -> Signal:
    """Should a position be opened, and if not, what is stopping it.

    SHORT when the z of the BID is at or beyond +entry_threshold; LONG when
    the z of the OFFER is at or beyond -entry_threshold — held for
    `confirm_samples` consecutive samples, inside the `max_entry_z` ceiling,
    in a direction this contract allows, outside the cooldown, and only where
    a return to the mean pays the round trip and the target.

    `confirmed` is how many consecutive samples the same side has been
    through the threshold; None (a caller that does not count) is taken as
    confirmed. `blocked_by` names the number that failed and the threshold it
    failed against, so the trader can tell a setting from a market.
    """
    sig = Signal(contract_key=window.contract_key, action="NONE", ts=now)
    qty = _f(settings, 'quantity', 1.0)

    if not master_on:
        sig.blocked_by = "the desk-wide algo master switch is off"
        return sig
    if not algo_on:
        return sig                       # IDLE is not "blocked", it is off
    if not window.is_warm:
        sig.blocked_by = (f"collecting — {window.history_minutes:.0f} of "
                          f"{window.min_history_minutes:.0f} minutes "
                          f"({window.warm_pct:.0f}%)")
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

    threshold = _f(settings, 'entry_threshold', 2.5)
    side = side_through(window, book, settings)
    if side is None:
        return sig                       # armed and waiting: not blocked
    z_bid, z_ask = side_z(window, book)
    z = z_bid if side is Side.SELL else z_ask
    which = "bid" if side is Side.SELL else "offer"

    # Which way this contract may be ENTERED. It reports the side it refused,
    # because "no trade" on a contract whose z is at -2.8 otherwise looks like
    # a broken threshold. Exits are never filtered by this.
    direction = str(settings.get('trade_direction', 'BOTH') or 'BOTH').upper()
    if direction == 'SELL_ONLY' and side is Side.BUY:
        sig.blocked_by = (f"long entries are off — offer z {z:+.2f} would "
                          f"have bought")
        return sig
    if direction == 'BUY_ONLY' and side is Side.SELL:
        sig.blocked_by = (f"short entries are off — bid z {z:+.2f} would "
                          f"have sold")
        return sig

    # The ceiling: beyond it the move is more likely to keep going than to
    # come back — and past the stop, a new position would be stopped out on
    # arrival.
    ceiling = min(_f(settings, 'max_entry_z', 3.5) or float('inf'),
                  _f(settings, 'stop_loss_z', 4.0) or float('inf'))
    if abs(z) > ceiling:
        sig.blocked_by = (f"{which} z {z:+.2f} is beyond the entry ceiling "
                          f"of {ceiling:.2f} — a blow-out, not an entry")
        return sig

    need = max(1, int(_f(settings, 'confirm_samples', 1)))
    if confirmed is not None and confirmed < need:
        sig.blocked_by = (f"confirming — {which} z {z:+.2f}, {confirmed} of "
                          f"{need} samples through {threshold:.2f}")
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

    entry_px = book.executable(side)
    target, why_not = can_it_pay(window, side, entry_px, qty, tick_size,
                                 tick_value, settings)
    if target is None:
        sig.blocked_by = why_not
        return sig

    sig.action = "OPEN"
    sig.side = side
    sig.qty = qty
    sig.reason = f"{which} z {z:+.2f} through {threshold:.2f}"
    return sig


def exit_signal(window: StatsWindow, book, position: Position,
                settings: Dict[str, Any], tick_size, tick_value,
                now: datetime, in_session: bool = True,
                session_flat_due: bool = False) -> Signal:
    """Should this position be closed, and for which recorded reason.

    Read at the side it would CLOSE on — a long leaves on the bid, a short
    buys back on the offer — first match wins, risk before reward:

      1. z stop       the closing side's z at `stop_loss_z` against it
      2. session      the configured flat time
      3. money stop   net P&L at or below -stop_loss_money per contract
      4. target       net P&L at break-even + profit_target_pct of margin
      5. time stop    held `max_hold_minutes`
      6. mean         back at the mean and already net positive

    **No filter, guard or limit appears in this function.** Everything here
    is a reason to get OUT, and the system must be able to get out of a
    position on a contract whose entries are all being withheld.
    """
    sig = Signal(contract_key=position.contract_key, action="NONE", ts=now)
    if position is None or not position.is_open:
        return sig

    closing_side = position.side.opposite
    close_px = book.executable(closing_side) if book is not None else None
    z = window.z_of(close_px)
    if z is None:
        z = window.z

    def close(reason: ExitReason, text: str) -> Signal:
        sig.action, sig.side, sig.qty = "CLOSE", closing_side, position.qty
        sig.exit_reason = reason
        sig.reason = text
        return sig

    # 1. the z stop, first, because it is the one that must never be missed
    stop_z = _f(settings, 'stop_loss_z', 4.0)
    if z is not None and stop_z:
        if position.side is Side.BUY and z <= -stop_z:
            return close(ExitReason.STOP_LOSS,
                         f"z {z:+.2f} through the stop at -{stop_z:.2f}")
        if position.side is Side.SELL and z >= stop_z:
            return close(ExitReason.STOP_LOSS,
                         f"z {z:+.2f} through the stop at {stop_z:.2f}")

    # 2. the session cutoff — decided, not optional
    if session_flat_due:
        return close(ExitReason.SESSION_FLAT,
                     "the session cutoff on the venue's clock")

    qty = position.qty
    net = costs_mod.open_net(position.side, qty, position.avg_price, close_px,
                             tick_size, tick_value, settings)

    # 3. the money stop, on NET — what the trade has actually lost
    stop_money = _f(settings, 'stop_loss_money', 0.0)
    if stop_money and net is not None and net <= -stop_money * qty:
        return close(ExitReason.MONEY_STOP,
                     f"net {net:+,.2f} through the stop of "
                     f"{-stop_money * qty:+,.2f}")

    # 4. the profit target: break-even plus the % of margin, at the side
    #    that would close it
    if position.target_price is not None and close_px is not None:
        reached = (close_px >= position.target_price
                   if position.side is Side.BUY
                   else close_px <= position.target_price)
        if reached:
            return close(ExitReason.TARGET,
                         f"{closing_side.value.lower()} side reached the "
                         f"target at {position.target_price:.4f}")

    # 5. the time stop — any P&L
    time_stop = _f(settings, 'max_hold_minutes', 0.0)
    if time_stop and position.opened_at is not None:
        held = (now - position.opened_at).total_seconds() / 60.0
        if held >= time_stop:
            return close(ExitReason.TIME_STOP,
                         f"held {held:.0f}m, limit {time_stop:.0f}m")

    # 6. back at the mean, and already paid
    if settings.get('exit_at_mean', True) and close_px is not None \
            and window.mean is not None and net is not None and net > 0:
        home = (close_px >= window.mean if position.side is Side.BUY
                else close_px <= window.mean)
        if home:
            return close(ExitReason.MEAN,
                         f"back at the mean ({window.mean:.4f}), net "
                         f"{net:+,.2f}")
    return sig
