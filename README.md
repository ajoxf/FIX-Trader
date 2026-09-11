# FIX-Trader

**Instruments & manual UAT orders:** open `/instruments` to search TT futures,
options and listed spreads, save a watchlist, receive bid/ask prices, and
review/send manual orders with cancel/replace and execution reports.
See [symbol and order instructions](docs/INSTRUMENTS_AND_ORDERS.md).
This manual workflow was added explicitly at the operator's request and is
separate from the existing algorithm desk. Algorithmic execution and complete
account-position recovery remain unfinished.

**Local TT UAT connection:** the native connection code from
`backup_v1fixapp.py` is now integrated. Run `run_fix.ps1` for the configured
Order Routing and Market Data sessions. See [TT connection](docs/TT_CONNECTION.md)
for setup and scope. This adds session connectivity; the execution and market
data translation work described below remains unfinished.

A **statistical-arbitrage monitor and execution terminal for exchange-listed
spread contracts**, traded over **FIX** through Orient Futures.

One screen, many contracts. Each contract gets a small window carrying the
numbers that decide the trade — rolling mean, standard deviation, z-score, the
edge filter, position and P&L — an **ALGO ON/OFF** switch, and a gear for that
contract's own settings. Prices refresh twice a second.

No ladders, no charts, no manual order entry: discretionary trading is done in
TT's own front end. This screen runs the algo and shows why it is or is not
trading.

Beside the live windows there is an **Analysis** window, one contract at a
time: what the standard-deviation touches at each level actually did next, how
the trades ended, and what the costs really were against what was budgeted for
them. That is the loop that tells you whether a contract's entry threshold is
right, or whether it should be switched off.

## Why it is simpler than a two-leg spread system

Orient lists the spread itself as a single tradeable contract. There is no leg
A and leg B, no hedge ratio, no matched clip, no naked-leg window and no
orphan-leg recovery — a spread is bought and sold like an outright.

## Contributing

**[`CONTRIBUTING.md`](CONTRIBUTING.md)** is the brief for anyone joining, and
**[`docs/WORK_FIX_GATEWAY.md`](docs/WORK_FIX_GATEWAY.md)** breaks the FIX and
order-execution work into six packages, one PR each.

The handover is a test file. `tests/test_gateway_contract.py` runs against the
simulator the whole system is built on, and it is the same suite a real
`FixGateway` has to pass against Orient's UAT. A gateway that passes it drops
in with no change above `fixtrader/gateway.py`.

## The build specification

**[`BUILD_PROMPT.md`](BUILD_PROMPT.md)** is the brief: the screen, the
per-contract and desk-wide settings, the Exchanges page and its FIX session
fields, the algo, the guards, the hard rules, the tests and the build order.
Read it before writing any code.

FIX connectivity is **provisioned but not wired** in this phase: `gateway.py`
is the only module that may import the FIX library, `FakeGateway` drives every
test and the pre-credentials desk, and `FixGateway` is stubbed behind the same
interface. See `docs/FIX_NOTES.md` for the message set and the open questions
for Orient.

## Running it — one file

```
python start.py
```

**Python 3.9 or newer, and Flask 2.0 or newer.** `start.py` checks both before
it starts anything and names the interpreter it is running on, because the
usual way to get this wrong is a new terminal that has forgotten the
environment:

```
conda activate fixtrader      # a new terminal does not keep it
python start.py
```

Anaconda's `base` is commonly Python 3.7 with an old Flask, and on it the
imports fail rather than the program misbehaving — `typing.Protocol` is 3.8,
and `@app.get` is Flask 2.0.

On a first run it writes `config.json` and `.env`, brings the **web UI up
first** (the venues are entered on that screen, so it has to be reachable
before there are any), starts the engine, opens the terminal in a window of
its own, and restarts a crashed child with backoff. It ships with three
example contracts against the simulator, so there is something on the screen
before a venue exists — and the taskbar says **SIMULATED** in the place a
**PROD** badge would go.

```
pytest tests/ -q
```

128 tests, everything faked: no venue, no network, no clock. The browser suite
drives the real UI under Playwright and reads `pageerror`, because a
temporal-dead-zone `ReferenceError` that aborts a script block and silently
unregisters a handler is invisible to a Python test — and is exactly what
happened in the system this is ported from. They skip cleanly where no browser
is installed.

## What is built

| Module | What it does |
|---|---|
| `fixtrader/config.py` | Venues, contracts, settings; atomic saves; `.env` keys; blank-versus-zero |
| `fixtrader/sizing.py` | `money = points × tick_value / tick_size × qty` — the one conversion |
| `fixtrader/costs.py` | The round trip, break-even, and the target as a percentage of margin |
| `fixtrader/stats.py` | Rolling mean, sigma and z; Hurst; half-life; and the SD touches |
| `fixtrader/signals.py` | The algo: entry with its filters, exit with none |
| `fixtrader/gateway.py` | The venue seam — **the only module that may import FIX** |
| `fixtrader/fake_gateway.py` | A real book, a real fill model, a real reject |
| `fixtrader/marketdata.py` | The staleness and jump guards, and the session clock |
| `fixtrader/executor.py` | One order path for entries and exits, market or limit |
| `fixtrader/engine.py` | The loop, the book, recovery, and one snapshot per pass |
| `fixtrader/database.py` | SQLite (WAL): positions, orders, fills, touches, events |
| `fixtrader/analysis.py` | The feedback loop: the touch study, the costs, the journal |
| `fixtrader/webapp.py` | The Flask process: it renders and it asks; it never trades |
| `fixtrader/static/`, `templates/` | The terminal — self-hosted, no CDN, no framework |

**Positions** is its own window on the desk: every open position across every
contract in one list — side, quantity, the price it filled at, the z the entry
fired at, break-even, target, stop, margin, open P&L — with **what the venue
says beside what this book holds**, and the venue's own tickets for the fills
that built each one. A row where the two disagree is tinted; a venue showing
long AND short at once is called out in red and never netted to zero, because
that is a close that went out as an open.

**Closes go out by reference, not as opposite orders.** Every closing order
carries an explicit close flag, the position it closes and that position's
tickets, capped at what is open on that side — see `docs/FIX_NOTES.md`.

**Settings** is the desk-wide page: the loop, the master switches, the guards,
the desk limits, and the defaults every blank box on a contract falls back to.
It shows what the engine **achieved** beside what was asked of it, and names
the two settings that need a restart rather than warning about it on every
save.

**Exchanges** is where the system is connected: venues with their FIX session
fields, UAT and PROD as separate rows that never look alike, **Connect**,
**Test** and **Diagnose** answering in the venue's own words with the step
that fixes each failure, and the contracts table with **Read specifications
from the venue** — which reports rather than applies, because a specification
changed under a running desk is every money figure on that window changing
without anybody being told. Against the simulator it says so: a gateway built
from your own configuration cannot confirm it.

**Analysis** is the feedback loop, one contract at a time — because a win rate
blended across eight of them cannot answer the only question it exists for,
which is which contract to turn off. It reports what happened **after** each
standard-deviation touch: how many came back, how long they took, how far they
went against you first, and how many became a trade. A level is only named as
the one to trade if the move back to the mean **covers the round trip** — the
inner bands always revert more often, and they are also the ones that cannot
pay for the trade. Costs are shown as budget against measurement, which closes
the loop the settings page opens: the edge filter refuses entries using the
*budgeted* slippage, so a budget that is wrong silently refuses trades that
would have paid, or passes trades that do not. The correction is offered as a
button; nothing here applies its own findings.

A contract with fewer than ten closed trades is marked **too few to judge** and
given no verdict. An open position is named and excluded from every figure. A
touch still running is counted separately and left out of the percentages —
folding it in as a miss understates every level, and understates the widest
levels most, because those are the ones still open.

## Status

The main terminal runs end to end against the simulator: contracts warm,
arm, enter, manage their positions and close on their targets, with the
notifications, the guards and restart recovery working. FIX is provisioned and
not wired — see `docs/FIX_NOTES.md`.
