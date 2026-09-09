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
- **The algo trades its OWN account** (a sub-account on this desk), and the
  screen says which — "whose money is this" is the same class of question as
  UAT or PROD. Tag 1 is stamped on every order rather than left for the
  session to imply, and the reconciler is scoped by it: a position the venue
  reports on ANOTHER account is not an anomaly, it is somebody else's work,
  and reporting it as UNCLAIMED every second trains the operator to ignore
  the one line that matters. A position with NO account stated is still
  reconciled — unknown is not "not ours", nothing is auto-closed on the
  strength of it, and the safe error is to report a position we may not own
  rather than ignore one we do. No account configured sends an EMPTY tag 1,
  never a guess at a default.

## Conventions that are easy to lose in a refactor

- **`orders()` and `positions()` return `None` for "unknown"**, which is NOT
  "no orders" / "flat". Code that treats the first as the second reports a
  clean account while the money sits at the venue.
- **The window marks its own position.** Blue while a position is open — a
  STATE read off the snapshot, so it survives a reload and cannot stick on a
  contract that is flat — and a green or red FLASH on the close, which fades.
  A window left red says "this is losing", which is a different statement
  from "the last trade lost". The close is read from `last_close.seq`, never
  from the wording of `last_event`: a highlight driven by a regex over a
  sentence stops working, silently, the day somebody rewords the sentence.
  A net of exactly 0 is not a win and a net of `None` is not a loss — both
  get the neutral mark. And the colour goes on the FRAME, the titlebar and
  the border, never on a layer over the figures: a tint that makes a price
  harder to read has cost more than it gave.
- **Unmeasured is not zero.** Return `None` and render `—`. A target of 0.00
  reads as "get out at break-even", which is a different instruction; a
  net P&L that quietly means gross makes a losing system look profitable.
- **`trade_direction` restricts ENTRIES only.** BOTH / SHORT_ONLY / LONG_ONLY,
  per contract: a desk that will only sell a rich spread passes over every
  long-spread signal. It can never withhold an exit — the position a one-way
  contract holds is by definition in the one direction it is allowed, so a
  direction filter on the close would strand exactly the position the desk
  was most careful about. An unrecognised value means BOTH, never a silent
  refusal to trade. The window carries a badge while it is in force, because
  a contract that is armed and passes over half its signals otherwise looks
  broken.
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
- **The schema MIGRATES itself, from the schema.** `CREATE TABLE IF NOT
  EXISTS` does nothing to a table that already exists, so every column added
  after a desk first ran is missing from that desk's database — and the first
  write naming it kills the engine mid-fill (`table positions has no column
  named opened_qty`, on a live desk, in a restart loop). `Database._migrate`
  reads the DECLARED schema, reads what the database has, and adds the
  difference: a column is migrated by having been declared, with no second
  list to keep in step. Additive only — nothing dropped, renamed or
  rewritten, because that would rewrite the recordings the replay reads and
  the positions the book is recovered from. Tables are created, THEN
  migrated, THEN indexed: an index naming a column the migration is about to
  add cannot be created before it.
- **`another_engine_is_running` checks the WRITER, not just the heartbeat.**
  A crashed engine leaves a snapshot seconds old, so an age check alone
  refuses the restart and the launcher spends its strikes on the guard rather
  than on the fault. The snapshot carries the writer's pid and host; a dead
  pid on this machine means a stale file. Anything it cannot tell — no pid,
  another host, no way to ask — counts as ALIVE, because being wrong that way
  refuses a start and being wrong the other way runs two engines against one
  book. **Never probe with `os.kill(pid, 0)`**: on Windows CPython maps every
  signal but CTRL_C/CTRL_BREAK onto TerminateProcess, so the harmless probe
  kills the engine it asked about.
- **The engine records the BOOK, not just its mid.** An exit reads the
  executable side, so a replay given only mids has to assume a spread — and
  that assumption cannot be corrected afterwards, which makes this the one
  thing in the system that got harder the longer it waited. `samples` carries
  `bid`/`ask`/`bid_size`/`ask_size`, added by an ADDITIVE migration because
  databases are already running; `None` there means NOT RECORDED, never a
  book of zero width. The replay counts what it read against what it had to
  assume and prints both, because a report whose exits came off a real book
  and one whose exits came off a guess must not look alike. A crossed book or
  one side alone is bad data, not a book: it falls back to the assumption and
  is counted as one.
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
