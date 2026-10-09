# Order tests on TT UAT

Every order path the desk uses in a live market, proven on TT UAT first — from
the screen. **Order tests** in the top bar (`/order-tests`).

## Using the page

**Order tests** in the top bar. Three steps:

1. **Pick a contract** — flat (nothing open, nothing working), with a bid and
   an offer. *Options* holds the quantity and the rest; the defaults are fine.
2. **Check automatically** — *Check manual orders* or *Check Algo orders*.
   The program places, fills, cancels and closes real orders on TT UAT and
   ticks each flow ✓ or ✗ with what TT answered. The contract ends flat.
   *Check Algo orders* first asks to send the Algo's orders to TT UAT (the
   engine's own confirmation).
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

| Test | Path | What it proves |
|------|------|----------------|
| M1 | Manual LIMIT | Rests away from the market, shows as working with TT's order id (37), cancels. 40=2, 44 in TT units, 54, 77=O, account. |
| M2 | Manual replace | A resting LIMIT is replaced (35=G) to a new price; TT confirms; cancelled. |
| M3 | Manual MARKET | Fills; the position shows; CLOSE by ticket goes 77=C 40=1 and flattens. |
| M4 | Manual marketable LIMIT + Close @ LMT | Fills at the offer; a Close @ LMT RESTS (77=C, 40=2); cancelled; market close. |
| M5 | Manual LIMIT hit | A LIMIT at the bid fills when the market trades there (waits; SKIP if it never does). |
| M6 | Manual refusal | An order TT refuses is shown REJECTED in TT's own words (tag 58). |
| A1 | Algo MARKET | FT- id, 77=O, 1028=N; the position carries TT tickets (not PAPER-); CLOSE NOW closes it by ticket (77=C, "Close P<id>" in 58). |
| A2 | Algo LIMIT | Rests (FT-), Cancel all pulls it. |
| A3 | Algo Close @ LMT + CLOSE ALL | The close rests PINNED; CLOSE ALL cancels it and, on TT's CANCELLED, sends ONE market close — never two. |
| A4 | Algo LIMIT hit | A LIMIT at the bid fills when the market trades there. |

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
