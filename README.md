# FIX-Trader

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

Still to come, in this order: the **Settings** page, the **Exchanges** page,
and the **Analysis** window. All three are drawn in `docs/screens.html`.

## Status

The main terminal runs end to end against the simulator: contracts warm,
arm, enter, manage their positions and close on their targets, with the
notifications, the guards and restart recovery working. FIX is provisioned and
not wired — see `docs/FIX_NOTES.md`.
