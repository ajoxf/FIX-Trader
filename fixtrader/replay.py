"""Replay recorded prices through the SAME signal code the desk runs.

The Analysis window says what happened. This says what a different setting
would have done to the same market — which is the other half of the loop, and
the half that stops "±1.0 reverts more often" from being acted on before
anybody checks whether it pays.

**What this is, exactly.** A SIGNAL replay. It re-runs `stats.StatsWindow`,
`signals.entry_signal` and `signals.exit_signal` over the mids this engine
recorded, and charges the contract's own configured round trip against every
trade. It is not a fill simulator and it never pretends to be one:

- **The recorded series is mids.** The book either side of the mid was not
  stored, so it is ASSUMED — one tick wide by default — and every report says
  so in `assumptions`. A replay whose spread assumption is wrong is wrong
  about its exits, because an exit reads the executable side.
- **Costs are BUDGETED, never measured.** The slippage in here is the number
  from the settings, not a fill that happened. `slippage_measured` is None and
  stays None: the measured figure lives in the Analysis window beside the
  budget, and blending the two would let a backtest report a cost it never
  paid.
- **Queue position does not exist here.** A limit entry is treated as filled
  at the price the signal fired on. That flatters a limit strategy and the
  report says which order types it assumed.
- **A position still open at the end is excluded** from every P&L figure and
  counted separately — the same rule the Analysis window follows for a live
  position, and for the same reason.

Nothing in this module re-implements a rule. It calls the engine's own
functions, so a threshold changed in `signals.py` changes what the replay
says on the same commit.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import costs as costs_mod
from . import signals as signals_mod
from . import sizing
from .models import BookTop, ExitReason, Position, Side
from .stats import StatsWindow

#: How wide the book is assumed to be, in ticks, when it was not recorded.
#: One tick is the usual quote on a listed spread. It is a stated assumption
#: and not a measurement, which is why it is reported back on every result.
DEFAULT_ASSUMED_SPREAD_TICKS = 1.0

#: Below this many closed trades a replay reports its figures and refuses to
#: draw a conclusion from them. Same threshold the Analysis window uses.
MIN_TRADES_FOR_A_VERDICT = 10


def assumptions(settings: Dict[str, Any], tick_size: float,
                tick_value: float,
                assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS
                ) -> Dict[str, Any]:
    """What the replay had to assume, in words, for the page it prints on.

    Attached to every result. A figure whose assumptions are not beside it is
    a figure somebody will quote without them.
    """
    qty = float(settings.get('quantity', 1.0) or 1.0)
    return {
        'book': (f'assumed {assumed_spread_ticks:g} tick wide around the '
                 f'recorded mid — the book itself was not recorded'),
        'fills': ('every order is treated as filled at the price its signal '
                  'fired on; there is no queue here, which flatters a limit '
                  'entry'),
        'costs': ("the contract's configured round trip, including the "
                  "slippage BUDGET. Nothing here is a measured cost"),
        'assumed_spread_ticks': assumed_spread_ticks,
        'round_trip_money': costs_mod.cost_breakdown(
            qty, tick_size, tick_value, settings)['round_trip_money'],
    }


def _book(mid: float, tick_size: float, spread_ticks: float,
          ts: datetime) -> BookTop:
    """A book around a recorded mid. The half-spread is the assumption."""
    half = (spread_ticks * tick_size) / 2.0
    return BookTop(bid=mid - half, ask=mid + half,
                   bid_size=None, ask_size=None, ts=ts)


def replay(samples: Sequence[Tuple[datetime, float]],
           settings: Dict[str, Any],
           tick_size: float, tick_value: float,
           contract_multiplier: Optional[float] = None,
           contract_key: str = "",
           assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS
           ) -> Dict[str, Any]:
    """Run one set of settings over one recorded series.

    Returns the trades it would have taken and what they would have made
    after the configured round trip — with the assumptions it ran under
    attached, because a figure whose assumptions are not on the page is a
    figure somebody will quote without them.
    """
    tick_size = float(tick_size or 0.0)
    qty = float(settings.get('quantity', 1.0) or 1.0)
    lookback = int(settings.get('lookback', 400) or 400)

    result: Dict[str, Any] = {
        'contract_key': contract_key,
        'samples': len(samples),
        'warm': False,
        'trades': [],
        'still_open': 0,
        'summary': _empty_summary(),
        'assumptions': assumptions(settings, tick_size, tick_value,
                                   assumed_spread_ticks),
        'blocked_by': None,
    }
    if not tick_size or not samples:
        result['blocked_by'] = ('nothing recorded for this contract over that '
                                'period' if not samples else
                                'this contract has no tick size')
        return result
    if len(samples) <= lookback:
        result['blocked_by'] = (
            f'{len(samples)} samples recorded, and the window needs '
            f'{lookback} before it produces a single statistic. Record more, '
            f'or replay a shorter lookback.')
        return result

    window = StatsWindow(contract_key or 'replay', lookback=lookback,
                         stats_update_interval_sec=float(
                             settings.get('stats_update_interval_sec', 0) or 0),
                         entry_threshold=float(
                             settings.get('entry_threshold', 2.0) or 2.0))
    position: Optional[Position] = None
    #: Why entries were withheld while the window was warm, counted. A
    #: cooldown between trades is not the same finding as a filter that
    #: withheld every entry there was.
    withheld: Dict[str, int] = {}
    entry_z: Optional[float] = None
    entry_std: Optional[float] = None
    trades: List[Dict[str, Any]] = []
    round_trip = costs_mod.round_trip_money(
        qty, tick_value,
        float(settings.get('commission_per_contract', 0.0) or 0.0),
        float(settings.get('exchange_fee_per_contract', 0.0) or 0.0),
        float(settings.get('clearing_fee_per_contract', 0.0) or 0.0),
        float(settings.get('slippage_budget_ticks', 0.0) or 0.0))

    for ts, mid in samples:
        book = _book(float(mid), tick_size, assumed_spread_ticks, ts)
        window.add(book.mid, ts, algo_armed=True)
        if not window.is_warm:
            continue
        result['warm'] = True

        if position is not None and position.is_open:
            sig = signals_mod.exit_signal(window, book, position, settings,
                                          tick_size, tick_value, ts)
            if sig.action == 'CLOSE':
                exit_px = book.executable(position.side.opposite)
                if exit_px is None:
                    continue
                money = costs_mod.net_pnl(position.side, qty,
                                          position.avg_price, exit_px,
                                          tick_size, tick_value,
                                          fees_paid=round_trip)
                trades.append({
                    'opened_at': position.opened_at.isoformat(),
                    'closed_at': ts.isoformat(),
                    'side': position.side.value,
                    'qty': qty,
                    'entry_z': entry_z,
                    'exit_z': window.z,
                    'entry_price': position.avg_price,
                    'exit_price': exit_px,
                    'gross': money['gross'],
                    'costs': money['fees'],
                    'net': money['net'],
                    'held_sec': (ts - position.opened_at).total_seconds(),
                    'exit_reason': (sig.exit_reason or ExitReason.TARGET).value,
                })
                position = None
            continue

        sig = signals_mod.entry_signal(
            window, book, settings, tick_size, tick_value, ts,
            algo_on=True, master_on=True, open_qty=0.0,
            last_trade_at=(datetime.fromisoformat(trades[-1]['closed_at'])
                           if trades else None))
        if sig.action != 'OPEN':
            # Why NOT, in the signal's own words. Without this a replay
            # blocked by a filter reports "nothing crossed the threshold",
            # which is a different — and wrong — answer, and it sends the
            # desk to change the number that was never the problem.
            if sig.blocked_by:
                withheld[sig.blocked_by] = withheld.get(sig.blocked_by, 0) + 1
            continue
        entry_px = book.executable(sig.side)
        if entry_px is None:
            continue
        entry_z, entry_std = window.z, window.std
        position = Position(contract_key=contract_key, side=sig.side,
                            qty=qty, opened_qty=qty, avg_price=entry_px,
                            opened_at=ts, entry_z=entry_z, entry_std=entry_std)
        # The exits read this. Computed the same way the engine computes it,
        # from the same function, so a replay and a live trade aim at the
        # same price.
        position.break_even = costs_mod.break_even(
            entry_px, sig.side, qty, tick_size, tick_value, settings)
        position.target_price = costs_mod.target_price(
            entry_px, sig.side, qty, tick_size, tick_value, settings,
            margin_locked=None, contract_multiplier=contract_multiplier,
            entry_std=entry_std)

    result['trades'] = trades
    result['still_open'] = 1 if (position is not None and position.is_open) else 0
    result['summary'] = summarise(trades, round_trip)
    result['withheld'] = dict(sorted(withheld.items(), key=lambda kv: -kv[1]))
    if result['warm'] and not trades:
        # A cooldown is a consequence of trading, so it is never the headline
        # reason for having taken no trades at all.
        reasons = [(n, why) for why, n in withheld.items()
                   if 'cooling down' not in why]
        if reasons:
            result['blocked_by'] = max(reasons)[1]
        else:
            result['blocked_by'] = ('the window warmed but nothing crossed '
                                    'the entry threshold over this period')
    return result


def _empty_summary() -> Dict[str, Any]:
    return {'trades': 0, 'won': 0, 'lost': 0, 'win_rate': None,
            'gross': None, 'costs': None, 'net': None, 'per_trade': None,
            'worst': None, 'best': None, 'median_hold_sec': None,
            'enough_to_judge': False, 'slippage_measured': None}


def summarise(trades: List[Dict[str, Any]],
              round_trip: Optional[float] = None) -> Dict[str, Any]:
    """The figures, and whether there are enough of them to mean anything."""
    out = _empty_summary()
    if not trades:
        return out
    nets = [t['net'] for t in trades if t['net'] is not None]
    out['trades'] = len(trades)
    if not nets:
        # Costs unmeasurable means net unmeasurable. Not zero.
        return out
    won = [n for n in nets if n > 0]
    out['won'] = len(won)
    out['lost'] = len(nets) - len(won)
    out['win_rate'] = round(100.0 * len(won) / len(nets), 1)
    out['gross'] = round(sum(t['gross'] for t in trades
                             if t['gross'] is not None), 2)
    out['costs'] = round(sum(t['costs'] for t in trades
                             if t['costs'] is not None), 2)
    out['net'] = round(sum(nets), 2)
    out['per_trade'] = round(out['net'] / len(nets), 2)
    out['worst'] = round(min(nets), 2)
    out['best'] = round(max(nets), 2)
    holds = sorted(t['held_sec'] for t in trades)
    out['median_hold_sec'] = round(holds[len(holds) // 2], 1)
    out['enough_to_judge'] = len(nets) >= MIN_TRADES_FOR_A_VERDICT
    return out


def sweep(samples: Sequence[Tuple[datetime, float]],
          settings: Dict[str, Any], tick_size: float, tick_value: float,
          thresholds: Sequence[float],
          contract_multiplier: Optional[float] = None,
          contract_key: str = "",
          assumed_spread_ticks: float = DEFAULT_ASSUMED_SPREAD_TICKS
          ) -> Dict[str, Any]:
    """The same series at several entry thresholds.

    This is the answer to the question the touch table invites. The inner
    bands revert more often — they always do — and this says what each one
    would have MADE after its round trip, which is a different question with
    a different answer.
    """
    rows = []
    for threshold in thresholds:
        run = replay(samples, dict(settings, entry_threshold=float(threshold)),
                     tick_size, tick_value, contract_multiplier,
                     contract_key, assumed_spread_ticks)
        rows.append({
            'entry_threshold': float(threshold),
            'trades': run['summary']['trades'],
            'win_rate': run['summary']['win_rate'],
            'net': run['summary']['net'],
            'per_trade': run['summary']['per_trade'],
            'worst': run['summary']['worst'],
            'median_hold_sec': run['summary']['median_hold_sec'],
            'enough_to_judge': run['summary']['enough_to_judge'],
            'blocked_by': run['blocked_by'],
            'withheld': run.get('withheld', {}),
        })
    # Every threshold blocked for the same reason is a statement about the
    # RECORDING, not about the thresholds. Said once, at the top, rather
    # than as a table of dashes that reads like "nothing paid".
    reasons = {r['blocked_by'] for r in rows}
    blocked = (rows[0]['blocked_by']
               if rows and len(reasons) == 1 and rows[0]['blocked_by']
               and not any(r['trades'] for r in rows) else None)
    return {'rows': rows, 'best': best_of(rows), 'blocked_by': blocked,
            'assumptions': assumptions(settings, tick_size, tick_value,
                                       assumed_spread_ticks)}


def best_of(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The threshold that MADE the most, not the one that reverted most.

    A level that reverts is not a level that pays, and a row with too few
    trades behind it is not a finding — it is one lucky trade with a
    percentage sign after it.
    """
    judged = [r for r in rows
              if r['enough_to_judge'] and r['net'] is not None and r['net'] > 0]
    if not judged:
        return None
    return max(judged, key=lambda r: r['net'])
