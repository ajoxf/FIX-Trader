"""The loop. One pass per contract, one snapshot out.

Everything the screen shows comes from `snapshot()`, and everything the screen
can ask for arrives as a command. The engine never talks to the browser and
the browser never talks to a venue.

Order of a pass, and it matters:

1. read the book, and let the guards observe it;
2. drain what the venue said since last time, and fold it into our own book
   FIRST — acting on a stale idea of the position is how a close becomes a
   reversal;
3. add the mid to the statistics, and record any standard-deviation touch;
4. manage working limits (re-price, escalate);
5. ask for an exit, then an entry. **Exits are considered first, always** —
   a pass that runs out of budget must have got the position out, not in.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import config as config_mod
from . import costs as costs_mod
from . import marketdata, signals as signals_mod, sizing
from .executor import Executor
from .marketdata import FeedGuard
from .models import (ContractState, ExitReason, Intent, OrderType, Position,
                     Side, TargetBasis, TouchState)
from .stats import StatsWindow

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ContractRuntime:
    """Everything the engine holds for one contract."""

    def __init__(self, contract, desk: Dict[str, Any]):
        self.contract = contract
        s = contract.settings_with_defaults(desk)
        self.window = StatsWindow(contract.key, lookback=int(s['lookback']),
                                  stats_update_interval_sec=s['stats_update_interval_sec'],
                                  entry_threshold=s['entry_threshold'])
        self.guard = FeedGuard(contract.key,
                               max_quote_age_sec=desk.get('MAX_QUOTE_AGE_SEC', 15.0),
                               max_jump_sigma=desk.get('MAX_PRICE_JUMP_SIGMA', 5.0),
                               jump_settle_sec=desk.get('JUMP_SETTLE_SEC', 2.0))
        self.position: Optional[Position] = None
        self.book = None
        self.blocked_by: Optional[str] = None
        self.last_event: str = ""
        self.last_trade_at: Optional[datetime] = None
        self.trades_today: int = 0
        self.pnl_today: float = 0.0
        self.day: Optional[str] = None
        #: Touches raised but not yet written; the engine drains these.
        self.pending_touches: List[Any] = []
        self.halted_reason: Optional[str] = None

    def roll_day(self, now: datetime) -> None:
        today = now.date().isoformat()
        if self.day != today:
            self.day = today
            self.trades_today = 0
            self.pnl_today = 0.0


class Engine:
    """One engine per process. Owns the contracts, the venue and the book."""

    def __init__(self, config, gateway, db=None, notify: Optional[Callable] = None,
                 simulated: bool = False):
        self.config = config
        self.gateway = gateway
        self.db = db
        self.notify = notify or (lambda *a, **k: None)
        self.simulated = simulated
        self.executor = Executor(gateway, db=db, notify=notify,
                                 simulated=simulated)
        self.runtimes: Dict[str, ContractRuntime] = {}
        self.master_algo: bool = bool(config.settings.get('ALGO_MASTER_ENABLED', True))
        self.killed: bool = False
        #: When an edited configuration was last picked up, and anything in
        #: it that is still waiting for a restart. The screen shows both: a
        #: setting that looks saved but is not in force is worse than one
        #: that plainly says it needs the engine bounced.
        self.config_reloaded_at: Optional[datetime] = None
        self.config_restart_needed: List[str] = []
        self.loop_ms: float = 0.0
        self.started_at: Optional[datetime] = None
        #: Why a close was sent, kept until the fill that completes it, so the
        #: exit reason on the position is the one that actually fired rather
        #: than something inferred from prices afterwards.
        self._exit_reasons: Dict[str, ExitReason] = {}
        #: The book is INCOMPLETE until recovery has run. While it is, the
        #: reconciler closes nothing: a position is only an orphan if we are
        #: sure it is not ours.
        self.book_complete: bool = False
        self.unclaimed: List[Dict[str, Any]] = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.gateway.start()
        for contract in self.config.enabled_contracts():
            self.add_contract(contract)
        self.recover()
        # Sweep our own working orders at STARTUP as well as at shutdown: a
        # crash leaves orders at the venue that this process no longer knows
        # about, and they are still ours.
        self.executor.cancel_all()
        self.started_at = utcnow()

    def stop(self) -> None:
        self.executor.cancel_all()
        self.gateway.stop()

    def add_contract(self, contract) -> ContractRuntime:
        rt = ContractRuntime(contract, self.config.settings)
        self.runtimes[contract.key] = rt
        self.gateway.subscribe(contract)
        self._fill_specs_from_venue(contract)
        if self.db is not None and self.config.settings.get('PERSIST_STATS_SAMPLES', True):
            for price in self.db.recent_samples(contract.key, rt.window.lookback):
                rt.window.add(price, utcnow())
        return rt

    def _fill_specs_from_venue(self, contract) -> None:
        """Take tick size, value, multiplier and bounds from the venue where
        the operator has not overridden them, and record where each came
        from — a typed number and a venue number must be distinguishable."""
        spec = self.gateway.security_definition(contract)
        if spec is None:
            return
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'min_qty', 'qty_step', 'max_qty', 'currency'):
            value = getattr(spec, field, None)
            if value is None:
                continue
            if getattr(contract, field, None) in (None, ''):
                setattr(contract, field, value)
                contract.spec_source[field] = 'venue'
            elif contract.spec_source.get(field) != 'operator':
                contract.spec_source.setdefault(field, 'venue')

    def recover(self) -> None:
        """Rebuild the book from the database, then compare it to the venue.

        Until this has completed, `book_complete` is False and NOTHING is
        auto-closed. Anything at the venue that recovery cannot explain is
        listed as UNCLAIMED for a person to decide about — never closed.
        """
        if self.db is not None:
            for pos in self.db.open_positions():
                rt = self.runtimes.get(pos.contract_key)
                if rt is not None:
                    rt.position = pos

        venue_positions = self.gateway.positions()
        if venue_positions is None:
            # Could not read. That is NOT "the venue is flat" — leave the book
            # incomplete and say so rather than adopting an empty account.
            self.book_complete = False
            self.unclaimed = []
            return

        self.unclaimed = []
        for vp in venue_positions:
            rt = self.runtimes.get(vp.contract_key)
            ours = rt.position.signed_qty if (rt and rt.position) else 0.0
            if abs(vp.qty - ours) > 1e-9:
                self.unclaimed.append({
                    'contract_key': vp.contract_key,
                    'venue_qty': vp.qty, 'our_qty': ours,
                    'text': (f"the venue reports {vp.qty:+g} on "
                             f"{vp.contract_key}; this book explains "
                             f"{ours:+g}. Nothing has been closed."),
                })
        self.book_complete = True

    # -- one pass ----------------------------------------------------------

    def poll(self, now: Optional[datetime] = None) -> None:
        now = now or utcnow()
        started = now

        events = self.gateway.drain_events()
        for event in events:
            self._handle_event(event, now)

        for key, rt in self.runtimes.items():
            try:
                self._poll_contract(rt, now)
            except Exception:                    # one contract must never
                logger.exception("contract %s failed its pass", key)
        self.loop_ms = (utcnow() - started).total_seconds() * 1000.0

    def _poll_contract(self, rt: ContractRuntime, now: datetime) -> None:
        contract = rt.contract
        settings = self.config.effective(contract.key)
        rt.roll_day(now)

        book = self.gateway.top_of_book(contract.key)
        rt.book = book
        rt.guard.observe(book, now)

        armed = bool(contract.algo_on and self.master_algo and not self.killed)
        if book is not None and book.usable:
            touches = rt.window.add(book.mid, now, algo_armed=armed)
            if touches and self.db is not None:
                for touch in touches:
                    self.db.save_touch(touch)
            if self.db is not None and self.config.settings.get(
                    'PERSIST_STATS_SAMPLES', True):
                self.db.save_samples(contract.key, [(now, book.mid)])

        said = self.executor.manage(contract, settings, book, now)
        for line in said:
            self._say(rt, "ORDER", line)

        # 1. the way OUT, always considered first
        if rt.position is not None and rt.position.is_open:
            flat_due = self._session_flat_due(contract, settings, now)
            sig = signals_mod.exit_signal(rt.window, book, rt.position,
                                          settings, contract.tick_size,
                                          contract.tick_value, now,
                                          session_flat_due=flat_due)
            if sig.action == "CLOSE" and not self._has_working_close(contract.key):
                self._send_close(rt, settings, sig.side, sig.qty,
                                 sig.exit_reason, sig.reason, book, now)
            return

        # 2. and only then the way in
        if self.killed:
            rt.blocked_by = "the kill switch is on"
            return
        status = rt.guard.status(now)
        sig = signals_mod.entry_signal(
            rt.window, book, settings, contract.tick_size, contract.tick_value,
            now, algo_on=contract.algo_on, master_on=self.master_algo,
            open_qty=0.0, trades_today=rt.trades_today, pnl_today=rt.pnl_today,
            last_trade_at=rt.last_trade_at, quote_stale=status['stale'],
            jump_settling=status['settling'],
            in_session=self._in_session(contract, settings, now))
        rt.blocked_by = sig.blocked_by
        if sig.action == "OPEN" and not self.executor.working_for(contract.key):
            self.executor.place(contract, settings, sig.side, sig.qty,
                                Intent.OPEN, book, now, reason=sig.reason,
                                decision=self._decision(rt.window))
            self._mark_touch_traded(rt, sig.side)
            self._say(rt, "ORDER",
                      f"{sig.side.value} {sig.qty:g} — {sig.reason}")

    @staticmethod
    def _decision(window) -> Dict[str, Any]:
        """The window's state at the moment a signal fires.

        Stamped onto the position when the fill lands. Reading these off the
        window at FILL time instead recorded whatever the market had moved to
        by then — an entry logged at z -0.67 for a trade taken at -2.24 — and
        every figure the Analysis window reports about why a trade happened
        would be the wrong one.
        """
        return {'z': window.z, 'mean': window.mean, 'std': window.std,
                'half_life': window.half_life}

    def _mark_touch_traded(self, rt: ContractRuntime, side: Side) -> None:
        """Record that THIS touch is the one an entry acted on.

        The touch study's "traded" column is what says whether the level the
        algo fires at is the level that actually reverts — a level that
        reverts 86% of the time and is never traded is a threshold set wrong.
        The mark has to be made when the order goes, because by the time the
        fill lands the z may have crossed into another band entirely.
        """
        touch = getattr(rt.window, 'last_touch', None)
        if touch is None:
            return
        # A SELL is taken at a high z, a BUY at a low one. A touch of the
        # opposite sign is not the one this entry acted on.
        wanted_positive = side is Side.SELL
        if (touch.level > 0) != wanted_positive:
            return
        if touch.became_trade:
            return
        touch.became_trade = True
        if self.db is not None:
            self.db.save_touch(touch)

    def _has_working_close(self, key: str) -> bool:
        return any(w.is_close for w in self.executor.working_for(key))

    def _send_close(self, rt, settings, side, qty, exit_reason, reason,
                    book, now) -> None:
        wo = self.executor.place(
            rt.contract, settings, side, qty, Intent.CLOSE, book, now,
            reason=reason, open_qty=rt.position.qty,
            position_id=rt.position.id,
            decision=self._decision(rt.window),
            # The position itself, so the order carries an explicit close
            # flag and the venue tickets it is closing — never a bare
            # opposite order, which opens the other side instead.
            position=rt.position)
        if wo is not None:
            self._exit_reasons[wo.clordid] = exit_reason or ExitReason.TARGET
            self._say(rt, "ORDER", f"closing {qty:g} — {reason}")

    # -- venue events ------------------------------------------------------

    def _handle_event(self, event, now: datetime) -> None:
        rt = self.runtimes.get(event.contract_key)
        contract = rt.contract if rt else None
        intent = self.executor.intent_of(event.clordid)
        self.executor.apply_event(
            event, contract.tick_size if contract else None, now)

        if rt is None:
            return
        if event.kind == "REJECTED":
            # The venue's own words, verbatim. Never "check the log".
            self._say(rt, "REJECT", event.text or "rejected")
            self.notify("REJECT", rt.contract.key, event.text or "rejected")
            return
        if event.kind in ("FILL", "PARTIAL") and event.fill is not None:
            self._apply_fill(rt, event, intent, now)
        elif event.kind == "CANCELLED":
            self._say(rt, "ORDER", "cancelled")

    def _apply_fill(self, rt: ContractRuntime, event, intent: Intent,
                    now: datetime) -> None:
        fill = event.fill
        contract = rt.contract
        settings = self.config.effective(contract.key)

        # The state the DECISION was made on, not the state now.
        decided = self.executor.decision_of(event.clordid)

        if intent is Intent.OPEN:
            if rt.position is None or not rt.position.is_open:
                margin = self.gateway.margin_for(contract.key, fill.qty)
                rt.position = Position(
                    contract_key=contract.key, side=fill.side, qty=fill.qty,
                    opened_qty=fill.qty, avg_price=fill.price, opened_at=now,
                    entry_z=decided.get('z', rt.window.z),
                    entry_mean=decided.get('mean', rt.window.mean),
                    entry_std=decided.get('std', rt.window.std),
                    entry_half_life=decided.get('half_life',
                                                rt.window.half_life),
                    margin_locked=margin, is_simulated=self.simulated,
                    tickets=[fill.exec_id],
                    opened_session=now.date().isoformat())
            else:
                pos = rt.position
                total = pos.qty + fill.qty
                pos.avg_price = ((pos.avg_price * pos.qty)
                                 + fill.price * fill.qty) / total
                pos.qty = total
                pos.opened_qty = total
                pos.tickets.append(fill.exec_id)
                pos.margin_locked = self.gateway.margin_for(contract.key, total)

            pos = rt.position
            pos.break_even = costs_mod.break_even(
                pos.avg_price, pos.side, pos.qty, contract.tick_size,
                contract.tick_value, settings)
            pos.target_price = costs_mod.target_price(
                pos.avg_price, pos.side, pos.qty, contract.tick_size,
                contract.tick_value, settings, margin_locked=pos.margin_locked,
                contract_multiplier=contract.contract_multiplier,
                entry_std=pos.entry_std)
            pos.stop_price = rt.window.price_at_z(
                float(settings.get('stop_loss_z', 4.0))
                * (1 if pos.side is Side.SELL else -1))
            if self.db is not None:
                self.db.save_position(pos)
            rt.last_trade_at = now
            self._say(rt, "OPEN",
                      f"{pos.side.value} {fill.qty:g} @ {fill.price:g}")
            self.notify("OPEN", contract.key,
                        f"{pos.side.value} {fill.qty:g} @ {fill.price:g}")
            return

        # a close
        pos = rt.position
        if pos is None or not pos.is_open:
            return
        pos.qty = round(pos.qty - fill.qty, 10)
        pos.tickets.append(fill.exec_id)
        if pos.qty > 1e-9:
            if self.db is not None:
                self.db.save_position(pos)
            return

        pos.closed_at = now
        pos.exit_price = fill.price
        pos.exit_z = decided.get('z', rt.window.z)
        pos.exit_reason = self._exit_reasons.pop(event.clordid,
                                                 ExitReason.TARGET)
        filled = pos.tickets
        result = costs_mod.net_pnl(pos.side, fill.qty, pos.avg_price,
                                   fill.price, contract.tick_size,
                                   contract.tick_value,
                                   fees_paid=self._fees_for(filled, settings,
                                                            fill.qty))
        pos.gross_pnl = result['gross']
        pos.fees_paid = result['fees']
        pos.net_pnl = result['net']
        if pos.net_pnl is not None and pos.margin_locked:
            pos.pnl_pct_on_margin = 100.0 * pos.net_pnl / pos.margin_locked
        if self.db is not None:
            self.db.save_position(pos)
        rt.trades_today += 1
        rt.last_trade_at = now
        if pos.net_pnl is not None:
            rt.pnl_today += pos.net_pnl
        money = f"{pos.net_pnl:+,.0f}" if pos.net_pnl is not None else "—"
        self._say(rt, "CLOSED",
                  f"{pos.side.value} {fill.qty:g} out at {fill.price:g} · {money}")
        self.notify("CLOSED", contract.key,
                    f"closed {fill.qty:g} @ {fill.price:g} · {money}")
        rt.position = None

    def _fees_for(self, tickets, settings, qty) -> Optional[float]:
        """What the venue charged, where it says. Falls back to the
        configured schedule, which is what the desk agreed with the broker —
        and is flagged in the Analysis window as budget, not measurement."""
        per_side = (float(settings.get('commission_per_contract', 0) or 0)
                    + float(settings.get('exchange_fee_per_contract', 0) or 0)
                    + float(settings.get('clearing_fee_per_contract', 0) or 0))
        return per_side * 2.0 * qty if per_side else 0.0

    # -- session -----------------------------------------------------------

    def _in_session(self, contract, settings, now: datetime) -> bool:
        if not self.config.settings.get('REFUSE_OUTSIDE_SESSION', True):
            return True
        return marketdata.in_session(now, contract.session_open,
                                     contract.session_close)

    def _session_flat_due(self, contract, settings, now: datetime) -> bool:
        flat_at = settings.get('session_flat_at') or ''
        if not flat_at:
            return False
        try:
            hh, mm = (int(x) for x in str(flat_at).split(':'))
        except ValueError:
            return False
        return (now.hour * 60 + now.minute) >= (hh * 60 + mm)

    # -- commands ----------------------------------------------------------

    def set_algo(self, key: str, on: bool) -> Dict[str, Any]:
        rt = self.runtimes.get(key)
        if rt is None:
            return {'ok': False, 'error': f"no contract {key}"}
        rt.contract.algo_on = bool(on)
        self.config.save()
        self._say(rt, "CONFIG", f"algo {'on' if on else 'off'}")
        return {'ok': True, 'algo_on': rt.contract.algo_on}

    def set_master(self, on: bool) -> Dict[str, Any]:
        self.master_algo = bool(on)
        self.config.settings['ALGO_MASTER_ENABLED'] = self.master_algo
        self.config.save()
        return {'ok': True, 'master_algo': self.master_algo}

    def close_now(self, key: str) -> Dict[str, Any]:
        """Cross out of this contract now, and stand its algo down.

        A guard may withhold an order; it may never withhold this.

        The algo goes off with it, and that is deliberate. The z that put the
        position on is still where it was, so an algo left armed re-enters on
        the very next pass — a tenth of a second after the trader pressed the
        button to get out. Pressing the safety control and watching the
        position come straight back is a control that did nothing. Turning it
        on again is one click, and it is a decision rather than an accident.
        """
        rt = self.runtimes.get(key)
        if rt is None or rt.position is None or not rt.position.is_open:
            return {'ok': False, 'error': "nothing open on that contract"}
        was_armed = rt.contract.algo_on
        if was_armed:
            self.set_algo(key, False)
        settings = dict(self.config.effective(key), exit_order_type='MARKET')
        now = utcnow()
        self._send_close(rt, settings, rt.position.side.opposite,
                         rt.position.qty, ExitReason.CLOSE_NOW,
                         "closed by hand", rt.book, now)
        return {'ok': True, 'algo_stood_down': was_armed}

    # -- picking up an edited configuration ---------------------------------

    #: Contract fields that cannot be changed under a running engine. Every
    #: one of them re-prices something the book already holds — a tick value
    #: changed while a position is open rewrites what that position made —
    #: or needs the gateway to subscribe again.
    STRUCTURAL_CONTRACT_FIELDS = ('symbol', 'venue', 'security_id',
                                  'security_exchange', 'tick_size',
                                  'tick_value', 'contract_multiplier',
                                  'currency', 'min_qty', 'qty_step',
                                  'max_qty')

    def apply_config(self, new: 'TraderConfig') -> Dict[str, Any]:
        """Adopt an edited configuration without stopping.

        Settings are edited in the web process, which writes `config.json`;
        this is the engine reading it back. It is deliberately narrow:

        - **The live switch wins over the file.** `algo_on` is not adopted.
          The file's copy is whatever was last written by the web process,
          and adopting it would flip a contract the trader had just stood
          down — or arm one they had not.
        - **Nothing here touches the book, a position or an open order.** A
          settings change is a change to what the algo does NEXT.
        - **Structural changes are REPORTED, not applied.** A contract added
          or removed needs the gateway to subscribe; a tick value changed
          under an open position rewrites what that position made. Those want
          a restart, and the screen says so rather than half-applying them.

        Returns what changed, and what is waiting on a restart.
        """
        changed: List[str] = []
        restart: List[str] = []

        for key, value in new.settings.items():
            if self.config.settings.get(key) != value:
                if key in config_mod.STRUCTURAL_SETTINGS:
                    restart.append(key)
                    continue
                self.config.settings[key] = value
                changed.append(key)

        for key in sorted(set(new.contracts) - set(self.config.contracts)):
            restart.append(f'contract {key} added')
        for key in sorted(set(self.config.contracts) - set(new.contracts)):
            restart.append(f'contract {key} removed')

        for key, incoming in new.contracts.items():
            mine = self.config.contracts.get(key)
            if mine is None:
                continue
            for field in self.STRUCTURAL_CONTRACT_FIELDS:
                if getattr(mine, field, None) != getattr(incoming, field, None):
                    restart.append(f'{key}.{field}')
            for field in ('name', 'decimals', 'session_open', 'session_close'):
                if getattr(mine, field) != getattr(incoming, field):
                    setattr(mine, field, getattr(incoming, field))
                    changed.append(f'{key}.{field}')
            # `enabled` is structural: a contract switched off mid-flight has
            # a window, a book and possibly a position that would have nowhere
            # to go.
            if mine.enabled != incoming.enabled:
                restart.append(f'{key}.enabled')
            if mine.overrides != incoming.overrides:
                mine.overrides = dict(incoming.overrides)
                changed.append(f'{key} settings')
            # `algo_on` is NOT adopted. See the docstring.
            mine.spec_source = dict(incoming.spec_source)

        # The statistics window holds its own copy of the three settings that
        # shape it, so it has to be told. A changed lookback resizes in place
        # and keeps the samples; the cached mean and sigma are invalidated,
        # because they were computed over a different window.
        for key, rt in self.runtimes.items():
            settings = self.config.effective(key)
            if rt.window.update_config(
                    lookback=int(settings['lookback']),
                    stats_update_interval_sec=settings[
                        'stats_update_interval_sec'],
                    entry_threshold=settings['entry_threshold']):
                changed.append(f'{key} window resized')

        self.config_reloaded_at = utcnow()
        self.config_restart_needed = restart
        if changed or restart:
            logger.info("configuration reloaded — %d changed, %d awaiting a "
                        "restart", len(changed), len(restart))
        return {'changed': changed, 'restart_needed': restart}

    def kill_all(self, close_positions: bool = False) -> Dict[str, Any]:
        """Stand every algo down and cancel our working orders.

        It does NOT flatten unless asked: the button pressed in a hurry must
        not also be the button that crosses eight spreads at market.
        """
        self.killed = True
        self.master_algo = False
        cancelled = self.executor.cancel_all()
        closed = 0
        if close_positions:
            for key, rt in self.runtimes.items():
                if rt.position is not None and rt.position.is_open:
                    self.close_now(key)
                    closed += 1
        return {'ok': True, 'cancelled': cancelled, 'closed': closed}

    def resume(self) -> Dict[str, Any]:
        self.killed = False
        return {'ok': True}

    def _say(self, rt: ContractRuntime, kind: str, text: str) -> None:
        rt.last_event = text
        if self.db is not None:
            self.db.log_event(kind, text, rt.contract.key)

    # -- the snapshot ------------------------------------------------------

    def state_of(self, rt: ContractRuntime, now: datetime) -> ContractState:
        if self.killed or rt.halted_reason:
            return ContractState.HALTED
        if rt.guard.is_stale(now):
            return ContractState.HALTED
        if rt.position is not None and rt.position.is_open:
            return ContractState.IN
        if self.executor.working_for(rt.contract.key):
            return ContractState.WORKING
        if not rt.contract.algo_on or not self.master_algo:
            return ContractState.IDLE
        if not rt.window.is_warm:
            return ContractState.WARMING
        if rt.blocked_by:
            return ContractState.BLOCKED
        return ContractState.ARMED

    def snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or utcnow()
        contracts = []
        for key, rt in self.runtimes.items():
            contract = rt.contract
            settings = self.config.effective(key)
            book = rt.book
            status = rt.guard.status(now)
            qty = float(settings.get('quantity', 1.0) or 1.0)

            ratio = signals_mod.edge_ratio(rt.window, qty, contract.tick_size,
                                           contract.tick_value, settings)
            breakdown = costs_mod.cost_breakdown(qty, contract.tick_size,
                                                 contract.tick_value, settings)
            min_mult = float(settings.get('min_std_multiple', 1.5) or 0)

            pos = rt.position
            open_pnl = None
            if pos is not None and pos.is_open and book is not None:
                close_px = book.executable(pos.side.opposite)
                if close_px is not None:
                    open_pnl = sizing.to_money(
                        (close_px - pos.avg_price) * pos.side.sign,
                        contract.tick_size, contract.tick_value, pos.qty)

            contracts.append({
                'key': key,
                'name': contract.name,
                'symbol': contract.symbol,
                'venue': contract.venue,
                'decimals': contract.decimals,
                'tick_size': contract.tick_size,
                'state': self.state_of(rt, now).value,
                'algo_on': bool(contract.algo_on),
                'market': (book.to_dict() if book is not None else
                           {'bid': None, 'ask': None, 'mid': None}),
                'feed': status,
                'stats': rt.window.to_dict(),
                'filters': {
                    'edge_ratio': round(ratio, 2) if ratio is not None else None,
                    'edge_ok': (ratio >= min_mult) if ratio is not None else None,
                    'min_std_multiple': min_mult,
                    'hurst_threshold': settings.get('hurst_threshold'),
                    'blocked_by': rt.blocked_by,
                },
                'costs': {
                    'round_trip_money': breakdown['round_trip_money'],
                    'round_trip_ticks': (round(breakdown['round_trip_ticks'], 2)
                                         if breakdown['round_trip_ticks'] is not None
                                         else None),
                },
                'settings': {
                    'entry_threshold': settings.get('entry_threshold'),
                    'stop_loss_z': settings.get('stop_loss_z'),
                    'quantity': qty,
                    'entry_order_type': settings.get('entry_order_type'),
                    'exit_order_type': settings.get('exit_order_type'),
                },
                'position': self._position_dict(pos, open_pnl),
                'orders': [w.to_dict() for w in
                           self.executor.working_for(key)],
                'pnl_today': round(rt.pnl_today, 2),
                'trades_today': rt.trades_today,
                'last_event': rt.last_event,
                'target_missing': (
                    costs_mod.missing_for_target(
                        settings, pos.margin_locked if pos else None,
                        contract.contract_multiplier,
                        pos.entry_std if pos else None, contract.tick_value)
                    if pos is not None and pos.is_open
                    and pos.target_price is None else None),
            })

        venue_positions = self.gateway.positions()
        return {
            'ts': now.isoformat(),
            'portfolio': self._portfolio(contracts, venue_positions),
            'engine': {
                'alive': True,
                'loop_ms': round(self.loop_ms, 1),
                'master_algo': self.master_algo,
                'killed': self.killed,
                'environment': self.config.environment_label,
                'simulated': self.simulated,
                'book_complete': self.book_complete,
                'unclaimed': self.unclaimed,
                #: An edited configuration is in force from the pass that
                #: picked it up. Anything the engine could not adopt while
                #: running is named here, so a setting that looks saved and
                #: is not never passes for one that is.
                'config_reloaded_at': (self.config_reloaded_at.isoformat()
                                       if self.config_reloaded_at else None),
                'config_restart_needed': list(self.config_restart_needed),
                'session': {
                    'state': self.gateway.state().value,
                    'text': self.gateway.state_text(),
                },
                'refresh_sec': self.config.settings.get('PRICE_REFRESH_SEC', 0.5),
                'sound': self.config.settings.get('SOUND_ENABLED', True),
                'confirm_close': self.config.settings.get('CONFIRM_CLOSE', True),
                'notify': {
                    'orders': self.config.settings.get('NOTIFY_ORDERS', False),
                    'fills': self.config.settings.get('NOTIFY_FILLS', True),
                    'positions': self.config.settings.get('NOTIFY_POSITIONS', True),
                    'rejects': self.config.settings.get('NOTIFY_REJECTS', True),
                    'withheld': self.config.settings.get('NOTIFY_WITHHELD', True),
                },
            },
            'contracts': contracts,
        }

    def _portfolio(self, contracts: List[Dict[str, Any]],
                   venue_positions) -> Dict[str, Any]:
        """Every open position across every contract, in one list.

        It carries what THIS BOOK holds and what the VENUE says beside it,
        because the two disagreeing is the thing worth seeing. A venue row
        with both sides open — long and short at once — is a close that went
        out as an open, and it is called out by name rather than netted to
        zero and shown as flat.

        `venue` is None where the account could not be READ. That is not
        "flat", and the screen says so rather than showing an empty table.
        """
        by_key = {}
        if venue_positions is not None:
            for vp in venue_positions:
                by_key[vp.contract_key] = vp

        rows: List[Dict[str, Any]] = []
        for c in contracts:
            pos = c.get('position')
            vp = by_key.get(c['key'])
            if pos is None and vp is None:
                continue
            rows.append({
                'key': c['key'],
                'name': c['name'],
                'symbol': c['symbol'],
                'decimals': c['decimals'],
                'side': pos['side'] if pos else None,
                'qty': pos['qty'] if pos else None,
                'avg_price': pos['avg_price'] if pos else None,
                'entry_z': pos['entry_z'] if pos else None,
                'break_even': pos['break_even'] if pos else None,
                'target': pos['target'] if pos else None,
                'stop': pos['stop'] if pos else None,
                'opened_at': pos['opened_at'] if pos else None,
                'open_pnl': pos['open_pnl'] if pos else None,
                'margin_locked': pos['margin_locked'] if pos else None,
                'tickets': pos.get('tickets') if pos else None,
                'mid': (c.get('market') or {}).get('mid'),
                'venue_qty': vp.qty if vp is not None else None,
                'venue_long': vp.long_qty if vp is not None else None,
                'venue_short': vp.short_qty if vp is not None else None,
                'venue_readable': venue_positions is not None,
                'both_sides_open': bool(vp is not None and vp.is_gross_hedged),
                'agrees': (vp is not None and pos is not None
                           and abs(vp.qty - (pos['qty'] *
                                             (1 if pos['side'] == 'BUY' else -1)))
                           < 1e-9),
            })

        pnls = [r['open_pnl'] for r in rows if r['open_pnl'] is not None]
        margins = [r['margin_locked'] for r in rows
                   if r['margin_locked'] is not None]
        return {
            'rows': rows,
            'venue_readable': venue_positions is not None,
            # Unmeasured is not zero: a total is only a total when every row
            # it covers was measured.
            'open_pnl': (round(sum(pnls), 2)
                         if len(pnls) == len(rows) and rows else None),
            'margin': (round(sum(margins), 2)
                       if len(margins) == len(rows) and rows else None),
            'realised_today': round(
                sum(rt.pnl_today for rt in self.runtimes.values()), 2),
            'trades_today': sum(rt.trades_today
                                for rt in self.runtimes.values()),
        }

    @staticmethod
    def _position_dict(pos: Optional[Position],
                       open_pnl: Optional[float]) -> Optional[Dict[str, Any]]:
        if pos is None or not pos.is_open:
            return None
        return {
            'side': pos.side.value, 'qty': pos.qty,
            'avg_price': pos.avg_price,
            'entry_z': pos.entry_z,
            'break_even': pos.break_even,
            'target': pos.target_price,
            'stop': pos.stop_price,
            'margin_locked': pos.margin_locked,
            'opened_at': pos.opened_at.isoformat() if pos.opened_at else None,
            'open_pnl': round(open_pnl, 2) if open_pnl is not None else None,
            'tickets': list(pos.tickets or []),
        }
