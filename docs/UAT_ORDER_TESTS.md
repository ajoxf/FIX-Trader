# Order tests on TT UAT

Every order path the desk uses in a live market, proven on TT UAT first — from
the screen. **Order tests** in the top bar (`/order-tests`).

## Running them

1. Start the program on the TT UAT venue (`run_fix.bat`) and connect.
2. Pick a contract that is **flat** (nothing open, nothing working) with a live
   bid and offer, and its Algo **Off** or **Signals**.
3. For the Algo tests (A1–A4) press **Arm LIVE** on the page (the engine's own
   confirmation, in the shared modal). Manual tests need no arming.
4. Tick the tests (Manual / Algo / All), set the quantity and **Run selected**.
   Each result lands as it finishes, with its evidence: TT's order id, the fill
   price, the FIX tags actually sent, TT's own words on a refusal.
5. **Stop** stops after the running test; it cleans up first.

Whatever a test opened is closed by ticket and whatever is still working is
cancelled — pass or fail. The last run is kept (`status.json.order-tests.json`)
and shown on the page after a restart.

From a terminal instead: `run_uat_tests.bat esz6` or
`python -m fixtrader.uat --contract esz6 [--only M1,M3] [--with-hits]`.

## Hands-on: the desk, as it is live

Under the tests table the page shows the desk's own **ladder and Algo window**
(and Trading Monitor) for the chosen contract — the same screen a live market
is traded on, embedded (`/desk?embed=uat&contract=KEY`).

- **Algo Off / Signals:** the ladder trades by hand — manual tickets (FTM-),
  reviewed in the modal, exactly as on the desk.
- **Algo on Trades, Execution LIVE (UAT only):** BUY / SELL and a click in Bids /
  Asks go through the **Algo's own order path** (`uat_order`: the executor, an
  FT- id, 77=O, 1028=N) as its signal would send them — the ladder banner turns
  purple and says so. The Algo's take-profit and stop loss then manage the
  position; CLOSE ALL and Close @ LMT close it as they would live. One test
  position at a time; the engine refuses it on any venue but UAT.

Do each test's **By hand** steps there and mark it **✓ PASS / ✗ FAIL** in the
table's *By hand* column: kept with the contract, the venue and the time
(`status.json.order-checks.json`), beside the automatic result.

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

Each test also has **By hand** steps on the page: the same check done on the
ladder, the ticket and the Trading Monitor. On a **live venue** the page
refuses to run anything and keeps the last UAT run as the record; the by-hand
steps are how a path is proven there, at the size the trader means.

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
