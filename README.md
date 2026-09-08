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

## Status

Specification only. Nothing is built yet.
