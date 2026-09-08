# FIX — what the wiring will need, and what is still unknown

`fixtrader/gateway.py` is the only module allowed to import a FIX library.
`FixGateway` there has the shape and the configuration but **no message
handling**: every handler is marked `TODO(fix-wire)`. It imports cleanly with
no venue reachable, reports `DOWN` with a readable reason, and never takes the
engine down.

This file is the brief for finishing it. Nothing here has been guessed into
code: a stub that says "not wired" is honest, and a stub that invents a tag is
a bug with a long fuse.

## The counterparty is TT, not Orient directly

`*.trade.tt` is **Trading Technologies**. TT is the FIX counterparty and the
gateway to the exchange; Orient sits behind it as the clearing broker. That
matters in three places:

- The thing configured on the Exchanges page is a **TT session**, not an
  exchange. The exchange is `SecurityExchange(207)` on the contract.
- TT splits **order routing, market data and drop copy** into separate
  sessions with **separate comp ids**. `VenueConfig` holds all three.
- **Drop copy reports the whole account**, including anything a person does
  by hand in TT's own UI. See "Drop copy" below — subscribing to it is a
  decision, not a detail.

### UAT endpoints, as supplied

| Session | Internet | Stunnel |
|---|---|---|
| Drop copy | `fixdropcopy-ext-uat-cert.trade.tt:11501` | `:11701` |
| Order routing | `fixorderrouting-ext-uat-cert.trade.tt:11502` | `:11702` |
| Market data | `fixmarketdata-ext-uat-cert.trade.tt:11503` | `:11703` |
| Recovery (drop copy) | `fixrecovery-ext-uat-cert.trade.tt:11505` | `:11705` |
| Recovery (order routing) | `fixrecovery-ext-uat-cert.trade.tt:11508` | `:11708` |

| | Order routing | Market data |
|---|---|---|
| Remote CompID (our `TargetCompID`) | `AJUATORDER` | `AJUATMARKET` |
| Tag 116 `OnBehalfOfSubID` | `AJUAT` or `33986` | `AJUAT` or `33986` |
| Tag 1 `Account` | `AJ_account` | — |

The password is **not** recorded here or anywhere else in the repository. It
goes in `.env` under the key the venue names, and the screen only ever says
whether it is set.

### Still needed before a session can log on

1. **Our own `SenderCompID`.** What was supplied is the *remote* comp id —
   TT's side. Ours has not been given.
2. **Which of `AJUAT` / `33986`** tag 116 wants. Two values were offered for
   one field.
3. **The real value for tag 1.** `AJ_account` reads like a placeholder.
4. **Internet or stunnel.** Two port sets. Stunnel implies a local stunnel
   client and changes what `use_tls` means.
5. **FIX version and the data dictionary**, exactly — TT publishes its own,
   and a session run against the wrong one rejects messages it should accept.
6. **The recovery sessions**: whether we are expected to use them, or whether
   a resend request on the main session is enough.

### The algo trades its own sub-account

The desk's decision: **the algo gets its own sub-account** and trades from
that, while a person trading by hand in TT's UI is on a different one. This
is the cleanest possible answer to the problem drop copy raises, and the code
now leans on it:

- **Tag 1 is stamped on every order** from the venue's `account`, never left
  for the session to imply.
- **The reconciler is scoped by account.** A position on another account is
  skipped — not "unclaimed". Before this, every hand trade the session could
  see would have shown up as an unexplained position, every second, until
  nobody read the line.
- **A position with no account stated is still reconciled.** Unknown is not
  "not ours"; nothing is auto-closed on the strength of it either way.
- **The screen shows the account** beside UAT/PROD, and an unconfigured one
  renders as an em dash rather than a blank that reads like a default.

Still to confirm with TT:

- **Does the order-routing session report positions for the sub-account
  only, or for everything it can see?** The scoping above is written for the
  second case, which is the safe assumption either way.
- **What exactly goes in tag 1** for a sub-account — the sub-account name
  alone, or a parent/child form.
- **Is a second `SenderCompID` or `OnBehalfOfSubID` needed** to trade the
  sub-account, or is tag 1 the whole of it?
- **Does the sub-account need its own market-data entitlements**, or does
  the parent's cover it?

### Drop copy — a design decision, not a detail

Drop copy reports **everything on the account**, including trades a person
places by hand in TT's UI. This system is the algo and manual trading is a
separate program, so a hand trade is, to this engine, a position it did not
open and cannot explain.

Two coherent choices, and they must be made deliberately:

- **Do not subscribe.** The engine sees only its own orders. The reconciler
  still reports the venue position it cannot account for as UNCLAIMED and
  never touches it — which is what it does today, and is safe.
- **Subscribe, and mark what it reports as NOT OURS.** Better, because an
  unclaimed position with a known cause is a different thing on the screen
  from an unclaimed position of unknown origin. It must never feed the
  algo's own book, the journal Analysis reads, or any P&L figure.

What must NOT happen is drop-copy fills arriving in the book as though this
system had sent them. That mixes hand trades into a win rate that then
describes neither.

## Open questions for Orient

These change what the code sends, so they are worth asking before writing it.

1. **How is a spread contract identified?** `Symbol(55)` alone, `SecurityID(48)`
   + `SecurityIDSource(22)` + `SecurityExchange(207)`, or a multi-leg
   `SecurityDefinition` with `NoLegs(555)`? The current `ContractConfig`
   carries `symbol`, `security_id` and `security_exchange` so any of the three
   fits, but the contracts table and the security-definition request differ.
2. **ANSWERED — market data is a separate session**, with its own
   TargetCompID. `VenueConfig` now carries `md_sender_comp_id` /
   `md_target_comp_id`; blank falls back to the order session for a broker
   that runs one. Original note: `VenueConfig` has `md_host` /
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
7. **PARTLY ANSWERED — UAT endpoints supplied** (table above). Still open:
   UAT hours, and whether PROD comp ids follow the same pattern.
9. **Is tick value in the security definition?** Brokers are reliable about
   tick size, multiplier and lot bounds; tick VALUE is often absent and
   derived instead — and for a spread it can differ from the outright. If it
   is not reported, it is typed and the window marks it an `override`, which
   is correct but worth knowing before the first contract is configured.
8. **Which offset flag does each contract want?** Tag 77 `PositionEffect`, or
   the broker's `CombOffsetFlag`, and whether the close-today / close-yesterday
   split applies. See "Closing" below — this one is not cosmetic.

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

## Closing — the offset flag is not optional

**A close is never a bare opposite order.** On a venue that keeps long and
short apart — every Chinese exchange Orient routes to (SHFE, DCE, CZCE, INE,
GFEX) takes an offset flag on every order — an opposite order that does not
say it is closing is an order to OPEN the other way. The desk ends up long and
short at once, both live, both posting margin, and a screen that nets would
show it as flat.

This is the FIX equivalent of closing an MT5 ticket, and the system already
carries everything the wiring needs:

| Ours | What it becomes on the wire |
|---|---|
| `OrderRequest.position_effect` | `PositionEffect(77)`: `O` open, `C` close. On the Chinese exchanges, the broker's `CombOffsetFlag` — Open / Close / CloseToday / CloseYesterday |
| `OrderRequest.reduce_only` | The venue's reduce-only flag where it has one. **A cap, not an instruction** — sent as well as the effect, never instead of it |
| `OrderRequest.position_id` | Our own position, for a venue that wants a position reference |
| `OrderRequest.close_tickets` | The venue execution ids of the fills that built the position — its tickets |
| `qty` | Already capped at what is open on that side; a close can only reduce |

`close_offset_mode` is per contract: `CLOSE` (the plain flag, the default),
`CLOSE_TODAY`, `CLOSE_YESTERDAY`, or `AUTO`, which picks from the trading day
the position was opened on. **SHFE and INE price a close-today differently
from a close-yesterday**, so getting this wrong is a real cost rather than a
formality — ask Orient which flag each contract wants.

**Unknown degrades to `CLOSE`, never to `OPEN`.** A close whose flag could not
be determined is still a close; the other way round turns an exit into a
second position, which is the failure this whole mechanism exists to prevent.

`FakeGateway` models this properly rather than netting: it keeps the two sides
apart, obeys the flag it is given, and refuses an oversized close with
`"Order would not reduce position size"`. Two tests hold the line — one sends
an opposite order flagged OPEN and asserts the desk ends up with **both sides
live**, and its control sends the same order flagged CLOSE and asserts nothing
is left.

## Until it is wired

`start.py --simulated` (the default) runs the whole system against
`FakeGateway`, and the taskbar says **SIMULATED** in the place a **PROD** badge
would go. Every test in the suite runs against it, so the wiring phase changes
one module and re-runs the same tests.
