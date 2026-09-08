"""The venue seam. **This is the only module that may import a FIX library.**

Everything above it — the engine, the executor, the statistics, the screen —
talks to the `Gateway` protocol and knows nothing about FIX. That is what
makes wiring Orient's session later a change to one file, and what lets the
whole test suite run with no venue in sight.

Two implementations ship:

- `FakeGateway` (in `fake_gateway.py`) — a real book with a real fill model, a
  real reject path and a clock the caller controls. Every test runs against
  it, and so does the desk before UAT credentials exist.
- `FixGateway` (below) — the class, its configuration and its session-state
  reporting, with the message handlers **stubbed and marked TODO(fix-wire)**.
  It must import cleanly with no venue reachable, report DOWN with a readable
  reason, and never take the engine down with it.

`docs/FIX_NOTES.md` carries the message set the wiring will need and the
questions still open with Orient. Nothing in here guesses a tag: a stub that
says "not wired" is honest, and a stub that invents a tag is a bug with a long
fuse.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable

from .models import (BookTop, Fill, GatewayEvent, OrderRequest, SecurityDef,
                     SessionState, VenueOrder, VenuePosition)

#: Every order we send carries this. Anything at the venue without it belongs
#: to somebody else — a hand order in TT, most likely — and is never
#: cancelled, amended or counted as ours.
CLORDID_PREFIX = "FT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@runtime_checkable
class Gateway(Protocol):
    """What the rest of the system is allowed to ask a venue for.

    Note the return types that include None. `orders()` and `positions()`
    return None for **"could not read"**, which is not "there are none".
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> SessionState: ...
    def state_text(self) -> str: ...
    def subscribe(self, contract) -> None: ...
    def top_of_book(self, key: str) -> Optional[BookTop]: ...
    def security_definition(self, contract) -> Optional[SecurityDef]: ...
    def margin_for(self, key: str, qty: float) -> Optional[float]: ...
    def send(self, order: OrderRequest) -> str: ...
    def cancel(self, clordid: str) -> None: ...
    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None: ...
    def orders(self) -> Optional[List[VenueOrder]]: ...
    def positions(self) -> Optional[List[VenuePosition]]: ...
    def drain_events(self) -> List[GatewayEvent]: ...


class FixGateway:
    """The real session. **Not wired — see `docs/FIX_NOTES.md`.**

    Everything structural is here: configuration, the session state machine's
    public face, and the same method surface `FakeGateway` implements. What is
    missing is the message handling, and it is missing on purpose: the FIX
    version, the data dictionary, how Orient identifies a spread contract and
    whether market data is a separate session are all still open questions,
    and each one changes what these methods send.

    It is safe to construct and safe to start. It reports DOWN with a reason a
    person can act on, and every method that cannot answer returns None rather
    than a plausible-looking zero.
    """

    #: What is still needed before this can be implemented. Reported by
    #: `state_text` so the Exchanges page says it rather than a log file.
    NOT_WIRED = (
        "the FIX session is not wired yet — Orient's UAT endpoint, FIX "
        "version, data dictionary and spread-symbol convention are still to "
        "be confirmed (docs/FIX_NOTES.md)"
    )

    def __init__(self, venue, contracts: Optional[List[Any]] = None):
        self.venue = venue
        self.contracts = list(contracts or [])
        self._state = SessionState.DOWN
        self._text = self.NOT_WIRED
        self._events: List[GatewayEvent] = []

    # -- session ---------------------------------------------------------

    def start(self) -> None:
        # TODO(fix-wire): build the session settings from self.venue, load the
        # data dictionary, initiate Logon(A) and run the message loop.
        self._state = SessionState.DOWN
        self._text = self.NOT_WIRED

    def stop(self) -> None:
        # TODO(fix-wire): Logout(5), then close the initiator cleanly so the
        # sequence files are written.
        self._state = SessionState.DOWN

    def state(self) -> SessionState:
        return self._state

    def state_text(self) -> str:
        return self._text

    # -- market data -----------------------------------------------------

    def subscribe(self, contract) -> None:
        # TODO(fix-wire): MarketDataRequest(V) for the top of book, then
        # handle the snapshot (W) and incremental (X) refreshes.
        return None

    def top_of_book(self, key: str) -> Optional[BookTop]:
        return None                       # unknown, NOT an empty book

    def security_definition(self, contract) -> Optional[SecurityDef]:
        # TODO(fix-wire): SecurityDefinitionRequest(c) / response (d).
        return None

    def margin_for(self, key: str, qty: float) -> Optional[float]:
        # TODO(fix-wire): whatever Orient offers here. Until then None, which
        # correctly leaves the profit target unpriced rather than guessing it.
        return None

    # -- orders ----------------------------------------------------------

    def send(self, order: OrderRequest) -> str:
        raise NotImplementedError(self.NOT_WIRED)

    def cancel(self, clordid: str) -> None:
        raise NotImplementedError(self.NOT_WIRED)

    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None:
        raise NotImplementedError(self.NOT_WIRED)

    def orders(self) -> Optional[List[VenueOrder]]:
        return None                       # could not read

    def positions(self) -> Optional[List[VenuePosition]]:
        return None                       # could not read

    def drain_events(self) -> List[GatewayEvent]:
        out, self._events = self._events, []
        return out

    # -- diagnosis -------------------------------------------------------

    def diagnose(self) -> List[Dict[str, Any]]:
        """One row per check, each carrying the step that fixes it."""
        return [{
            'check': 'FIX session',
            'ok': False,
            'detail': self.NOT_WIRED,
            'fix': 'Run against the simulator until the session is wired.',
        }]
