# Order tests on TT UAT

Every order path the desk uses in a live market, proven on TT UAT first — from
the screen. **Order tests** in the top bar (`/order-tests`).

## Using the page

**Order tests** in the top bar. Three steps:

1. **Pick a contract** — flat (nothing open, nothing working), with a bid and
   an offer. *Options* holds the quantity and the rest; the defaults are fine.
2. **Check automatically** — four groups, each with its own **Check** button:
   **Manual · Market**, **Manual · Limit**, **Algo · Market**, **Algo · Limit**.
   The program places, fills, cancels and closes real orders on TT UAT and
   ticks each flow ✓ or ✗ with what TT answered and when; each flow keeps its
   newest result, whichever group was run. The contract ends flat. An Algo
   group first asks to send the Algo's orders to TT UAT (the engine's own
   confirmation). "Fills when the market reaches it" waits for the market and
   is off unless ticked under *Options*. A price that has not moved does not
   stop a check — a quiet UAT market is normal; a bid and an offer is enough.
3. **Try it yourself** — the desk's own ladder, Algo window and Trading
   Monitor, as you will trade live:
   - **By hand:** Algo switch on **OFF** (or SIGNALS). BUY / SELL, or click a
     price (Bids buys, Asks sells); cancel; CLOSE ALL; Close @ LMT.
   - **As the Algo:** Algo switch on **UAT**. BUY / SELL or a price click makes
     the Algo send its order to TT UAT now, as on a signal; its take-profit
     and stop loss then manage it.
   Tick what you have tried.

## The words

| Algo switch | What happens |
|---|---|
| **OFF** | You trade by hand. |
| **SIGNALS** | The Algo alerts and watches your position's TP/SL; you trade by hand. |
| **PAPER** | The Algo trades; its orders are filled inside the program — nothing goes to TT. |
| **UAT** | The Algo trades; its orders go to TT UAT. (On a live venue this choice reads **LIVE**.) |

"LIVE" on the screen only ever means a live market.

From a terminal instead: `run_uat_tests.bat esz6` or
`python -m fixtrader.uat --contract esz6 [--only M1,M3] [--with-hits]`.

## The tests

| Group | Test | What it proves |
|------|------|----------------|
| Manual · Market | M3 | BUY at market fills; the position shows; CLOSE ALL by ticket goes 77=C 40=1 and flattens. |
| Manual · Market | M7 | SELL at market fills short; CLOSE ALL buys it back by ticket (77=C, 54=1). |
| Manual · Limit | M1 | Rests away from the market with TT's order id (37); cancels. 40=2, 44 in TT units, 77=O. |
| Manual · Limit | M2 | A resting LIMIT is replaced (35=G) to a new price; cancelled. |
| Manual · Limit | M4 | Fills at the offer; a Close @ LMT RESTS (77=C, 40=2); cancelled; market close. |
| Manual · Limit | M5 | A LIMIT at the bid fills when the market trades there (waits; skipped if it never does). |
| Manual · Limit | M6 | An order TT refuses shows REJECTED in TT's own words (tag 58). |
| Algo · Market | A1 | BUY: FT- id, 77=O, 1028=N; TT tickets (not PAPER-); CLOSE ALL closes by ticket ("Close P<id>" in 58). |
| Algo · Market | A5 | SELL: the same, short. |
| Algo · Limit | A2 | Rests (FT-); Cancel all pulls it. |
| Algo · Limit | A3 | Close @ LMT rests PINNED; CLOSE ALL cancels it and, on TT's CANCELLED, sends ONE market close. |
| Algo · Limit | A4 | A LIMIT at the bid fills when the market trades there. |

On a **live venue** the page runs nothing and keeps the last UAT results.

## How they are proven before UAT

`tests/test_uat_orders.py` runs every test through the real web app, the real
engine loop and the real FIX sessions against a fake TT exchange
(`tests/fake_tt.py`) that quotes in TT's FIX units with a DisplayFactor — so
the price conversion is tested on the same pass. They found one real bug on
the way: with Auto trade off, CLOSE ALL over a resting Close @ LMT cancelled it
and then never sent the market close (the executor only learned the contract
while Auto trade was on). Fixed, with a regression test.

The fake exchange is not TT's matching. A pass here means the program does
what it should with TT's answers; the UAT run proves TT gives those answers.
