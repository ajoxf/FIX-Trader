# Working on FIX-Trader

Read **`BUILD_PROMPT.md`** first — it is the specification. **`docs/screens.html`**
is the layout reference: where the two disagree about what a screen looks like,
the drawing wins.

## Hard rules

- **`pytest tests/ -q` must pass before any commit**, and a PROD venue must
  never be run without it.
- **One contract, not two legs.** The venue lists the spread itself. If you
  find yourself writing a hedge ratio, stop — you are building the wrong
  system.
- **Price is the spread's own price, from the MID OF THE BOOK.** Never the last
  trade. A trigger reads the executable side for its own direction; an open
  position reads the **opposite** side to close.
- **One conversion, in `sizing.py`**: `money = points × tick_value / tick_size
  × qty`. Every money figure on the screen goes through it.
- **`gateway.py` is the only module that may import a FIX library.**
- **Credentials live only in `.env`** — never in code, config, an API response,
  a toast or a log line. A password is reported as set or not set, never
  returned, not even masked.
- **Cancel our own working orders at startup AND at shutdown**, scoped to our
  `ClOrdID` prefix. Never touch an order this system did not send.
- **The book is persisted and recovered.** The reconciler auto-closes nothing
  until recovery says the book is complete, and never touches a position it
  cannot explain.
- **A close is never a bare opposite order.** It carries an explicit
  `PositionEffect`, the position it closes and that position's venue tickets,
  and it is capped at what is open on that side. `reduce_only` is a CAP, not
  an instruction — sent as well as the flag, never instead of it. On a venue
  that keeps long and short apart, an opposite order flagged OPEN opens the
  other side: the desk is long AND short, both posting margin, and a netting
  screen calls it flat. An unknown flag degrades to CLOSE, never to OPEN.
- **No manual order entry. This program is the ALGO.** The only controls that
  send are the algo switch, CLOSE NOW and KILL ALL. Manual trading is a
  SEPARATE program — it is not a feature to add here, and a ladder was built
  and taken back out for exactly that reason. An algo and a hand on the same
  contract fight: the trader puts a position on and the algo closes it at its
  own target, or the trader gets flat and the algo re-enters on the next pass.
  Keeping them in one process means every guard has to answer "which of you
  is trading this contract?", and the journal that Analysis reads mixes hand
  trades into a win rate that then describes neither.
- **CLOSE NOW stands that contract's algo down.** The z that put the position
  on has not moved, so an algo left armed re-enters on the next pass — a
  tenth of a second after the trader pressed the button to get out.
- **UAT and PROD are separate venues** and the screen always says which.

## Conventions that are easy to lose in a refactor

- **`orders()` and `positions()` return `None` for "unknown"**, which is NOT
  "no orders" / "flat". Code that treats the first as the second reports a
  clean account while the money sits at the venue.
- **Unmeasured is not zero.** Return `None` and render `—`. A target of 0.00
  reads as "get out at break-even", which is a different instruction; a
  net P&L that quietly means gross makes a losing system look profitable.
- **A guard may withhold an ORDER. A guard must never prevent a close** — and
  nothing withholds the escalation to market on an exit.
- **A refusal carries the venue's own words** (`tag 58: "Instrument not open
  for trading"`), never "check the log".
- **The figures a decision was made on are recorded at the time**, not read off
  the window when the fill lands. `Executor.decisions` exists for exactly this:
  by fill time the market has moved, and an entry logged at z −0.67 for a trade
  taken at −2.24 makes every Analysis figure the wrong one.
- **`cancel` is a REQUEST, not a fact.** The resting order is still live at
  the venue until the venue says otherwise, and it can fill in between. The
  escalation from an unfilled limit is therefore ARMED on the timeout and
  SENT on the CANCELLED event, for whatever is still outstanding then.
  Sending the replacement alongside the cancel puts two orders out for one
  position: on a close the second finds nothing to close (the simulator says
  so in the venue's own words), and on an open it doubles the position.
- **A gateway event carries a SNAPSHOT of the order, never the live object.**
  Aliasing made a queued ACK report the order's current state, the reader
  marked it done, and the fill that followed was applied as an open — doubling
  the position instead of closing it.
- **Hurst is computed on the INCREMENTS, not the levels.** R/S over a price
  series reads ~1.0 for everything, a random walk included, and a 0.5 threshold
  then withholds every entry on every contract for ever. It also ships **off**:
  it is an estimate, and on a coarsely quantised spread it reads high.
- **Statistics stay live until the window is warm.** The update interval holds
  the bands still for a trader to aim at; applied during warm-up it froze a
  sigma computed from two samples.
- **A level that reverts is not a level that pays.** The inner bands always
  revert more often — a spread one sigma from its mean comes back more
  reliably than one at three — and they are also the ones whose move cannot
  cover the round trip. Any "best level" must clear its costs, or the Analysis
  window invites lowering the threshold onto something that reverts
  beautifully and loses money every time.
- **The replay is a SIGNAL replay and says so on its own face.** It calls
  `stats` and `signals` — never a second implementation of a rule — over
  recorded mids. The book either side of the mid was never stored, so the
  spread is a stated assumption printed under the table; costs are the
  configured BUDGET and `slippage_measured` is None for ever; there is no
  queue. Where every entry was withheld it reports the signal's own words,
  because "nothing crossed the threshold" when the edge filter was the
  blocker sends the desk to change the number that was never the problem.
- **Slippage can be NEGATIVE** — the market moving our way between the price an
  order was aimed at and the fill. That is a price improvement, not a cost, and
  a budget is never negative: proposing one would have the edge filter pay the
  desk to trade.
- **`Position.qty` is what REMAINS; `opened_qty` is the size it was opened at.**
  Closing decrements the first, so a journal reading the wrong one reports
  every trade as size 0 — and every cost figure fed from it as unmeasured.
- **The engine reads `config.json` back while it runs.** Settings are edited
  in the WEB process, which writes that file and nothing else; the runner
  watches it and calls `Engine.apply_config`. Three rules hold there: the
  LIVE switch wins over the file (`algo_on` is never adopted, or a reload
  stands down a contract the trader just armed); nothing touches the book, a
  position or an open order; and a change the running engine cannot adopt —
  a contract added or removed, a tick value, `enabled`, `DATABASE_PATH` — is
  REPORTED in `config_restart_needed`, never half-applied. A setting that
  looks saved and is not in force is worse than one that plainly says it
  needs a restart.
- **Only one engine may run against a book.** Two trade the same signals on the
  same account and each sees the other's fills as positions it cannot explain.
  The runner refuses to start when the snapshot is being published already.
- **Every test that asserts a guard withholds something needs a control** that
  turns the guard off and asserts the opposite.
- **No native `alert()` / `confirm()` / `prompt()`.** One shared modal, and a
  test fails the build if they come back.
- **The page polls twice a second, so `networkidle` never fires.** In browser
  tests wait for `domcontentloaded` and then for the thing you care about.
- **Every window is its own stacking context.** A child with a `z-index` — the
  sticky table header in Positions and Analysis — is otherwise placed against
  the whole page and paints straight through any window drawn over it. And a
  desk of draggable windows needs click-to-raise, or one of them can never be
  read.
