# FIX — what the wiring will need, and what is still unknown

`fixtrader/gateway.py` is the only module allowed to import a FIX library.
`FixGateway` there has the shape and the configuration but **no message
handling**: every handler is marked `TODO(fix-wire)`. It imports cleanly with
no venue reachable, reports `DOWN` with a readable reason, and never takes the
engine down.

This file is the brief for finishing it. Nothing here has been guessed into
code: a stub that says "not wired" is honest, and a stub that invents a tag is
a bug with a long fuse.

## Open questions for Orient

These change what the code sends, so they are worth asking before writing it.

1. **How is a spread contract identified?** `Symbol(55)` alone, `SecurityID(48)`
   + `SecurityIDSource(22)` + `SecurityExchange(207)`, or a multi-leg
   `SecurityDefinition` with `NoLegs(555)`? The current `ContractConfig`
   carries `symbol`, `security_id` and `security_exchange` so any of the three
   fits, but the contracts table and the security-definition request differ.
2. **Is market data a separate session?** `VenueConfig` has `md_host` /
   `md_port`, blank meaning "the same session". Knowing now saves a refactor.
3. **FIX version and data dictionary.** 4.2, 4.4, or FIXT.1.1 + FIX50SP2, and
   the custom-tag XML.
4. **Sequence-reset policy** on logon, and whether they expect
   `ResetSeqNumFlag(141)=Y` on every connect.
5. **Does anything report margin per contract?** The profit target is a
   percentage of initial margin (the desk's choice), so where margin cannot be
   read the window correctly shows no target. If Orient will not report it over
   FIX, the desk either supplies a per-contract figure or switches that
   contract's basis to notional — **not** silently, which is why
   `costs.missing_for_target` names the missing figure in words.
6. **Trading-hours source.** Session windows are configured per contract today.
   If the venue publishes them in the security definition, read them instead.
7. **UAT endpoints and hours**, and whether UAT comp ids differ from PROD.

## The message set

| Purpose | Message |
|---|---|
| Session | `Logon(A)`, `Logout(5)`, `Heartbeat(0)`, `TestRequest(1)`, `ResendRequest(2)`, `SequenceReset(4)` |
| Contract specs | `SecurityDefinitionRequest(c)` → `SecurityDefinition(d)` |
| Market data | `MarketDataRequest(V)` → `MarketDataSnapshotFullRefresh(W)`, `MarketDataIncrementalRefresh(X)`, `MarketDataRequestReject(Y)` |
| Orders | `NewOrderSingle(D)`, `OrderCancelRequest(F)`, `OrderCancelReplaceRequest(G)` |
| Acknowledgement | `ExecutionReport(8)`, `OrderCancelReject(9)`, `BusinessMessageReject(j)` |

## Mapping onto what already exists

`FakeGateway` implements the same protocol and is the reference for behaviour:

- `top_of_book(key)` → `None` for **unknown**, never an empty book.
- `orders()` / `positions()` → `None` for **could not read**, which is not
  "none" and not "flat". `FakeGateway.readable = False` is the switch the
  tests use to produce that state.
- `send()` returns a `ClOrdID` prefixed `FT-`. Anything at the venue without
  that prefix belongs to somebody else — a hand order in TT — and is never
  cancelled, amended or counted as ours.
- **Amend is cancel-replace**, keeping the order id and its lineage. Never
  cancel-then-new: it gives up the queue and opens a window in which the order
  does not exist.
- **Every event must carry a snapshot of the order, not a live reference.**
  A real `ExecutionReport` is a distinct message; aliasing it broke the fill
  path once already (see `CLAUDE.md`).
- A reject carries **the venue's own text**, verbatim, into
  `GatewayEvent.text` — tag 58 and tag 103 where they are sent.

## Reduce-only

A closing order must be capped at the open position and, where the venue
supports the flag, sent reduce-only. `FakeGateway` refuses an oversized close
with `"Order would not reduce position size"` on purpose: an exit that
overfills opens the opposite side of a spread the desk thought it had left.

## Until it is wired

`start.py --simulated` (the default) runs the whole system against
`FakeGateway`, and the taskbar says **SIMULATED** in the place a **PROD** badge
would go. Every test in the suite runs against it, so the wiring phase changes
one module and re-runs the same tests.
