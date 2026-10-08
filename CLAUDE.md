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
- **What comes BACK is applied as a close, never as a new position.** An
  order's purpose survives a restart: `Executor.intent_of` reads it from the
  orders table it was written to when sent — a close still working when the
  engine stopped was otherwise read as an OPEN. And the book guards itself
  whatever the record says: a fill on the OPPOSITE side of an open position
  only reduces it (never averaged in, never opens the other side); a
  "close" on the SAME side is reported, not applied; a close larger than
  what is open closes it and the excess is reported, never booked; a close
  with nothing open opens nothing. `tests/test_close_safety.py` pins each,
  for a Take Profit and a Stop Loss alike.
- **Manual AND algo, but never at the same time.** This program trades by
  hand (Instruments & orders: tickets, ladders) and by algo (the Algo desk),
  and the desk is in exactly ONE trading mode, `ALGO` or `MANUAL`
  (`Engine.trading_mode`, switched from the Algo desk taskbar or the
  Instruments page, kept beside the status file so a restart comes back in
  it). An algo and a hand on the same book fight — the trader puts a
  position on and the algo closes it at its own target, or the trader gets
  flat and the algo re-enters on the next pass — and a journal mixing the
  two describes neither. So:
  - In `ALGO` mode a NEW manual order is refused by `ManualTerminal` itself
    (`mode_block`), at the review AND at the send, so no page or command can
    go round it; a ticket reviewed in MANUAL is discarded on the switch.
  - In `MANUAL` mode the algo neither enters nor proposes, and automatic
    trading cannot be turned on. Statistics keep running.
  - A switch is REFUSED while the side being left still has anything open
    or working — an algo position or order, a manual working order or an
    unclosed manual fill — and the refusal names each one.
  - Closes and cancels are never refused, in either mode: the guard is on
    new exposure only. A manual order recovered as UNKNOWN after a restart
    is named, not counted as open (it would block the switch for ever).
- **The Algo desk is a ladder and an Algo window per contract**, each with
  a taskbar button that minimises and restores it. The desk ladder shows the
  book and the Algo's levels against it; it does not take manual orders
  (those are on Instruments & orders, in MANUAL mode). Its CLOSE always
  closes.
- **CLOSE NOW stands that contract's algo down.** The z that put the position
  on has not moved, so an algo left armed re-enters on the next pass — a
  tenth of a second after the trader pressed the button to get out.
- **UAT and PROD are separate venues** and the screen always says which.

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
- **The Algo is the MT5 desk's Algo, on ONE contract** (`algo.py`,
  `bands.py`, `algofilters.py`, `algodesk.py`, `backtest.py`). Bollinger
  bands, TradingView's convention: candles of `timeframe_min` closing on the
  last MID in them (the forming candle counts), middle = EMA(N), sigma = the
  POPULATION sigma of the last N. "H to L" sells the contract into its BID,
  "L to H" buys its OFFER — there are no legs and no beta. A side is ARMED
  when its own z reaches the entry z and, with re-entry on, ENTERS on the way
  back in, inside the re-entry window; it must hold for `confirm_samples`
  FRESH QUOTES (a quote read again is not a new one). The gates — health,
  mode, the day's limits, collecting candles, the live warm-up, cooldown,
  session cutoff, the |z| cap, levels, and the filters (edge, regime, trend,
  half-life) — hold ENTRIES only and each says why; a filter it cannot price
  BLOCKS. Those modules DECIDE; the engine acts — `tests/test_algo.py` fails
  the build if one of them imports an order path.
- **History is the recording.** A FIX market-data session has no bars to
  backfill from: the band is rebuilt from the mids this system recorded, on
  the Algo's FIRST pass and on the clock the passes run on (seeding on a
  clock of its own left a band empty over hours of recordings). The live
  warm-up is time WATCHED since the Algo was armed; a quick restart carries
  it, standing the Algo down resets it.
- **Levels are from BREAK-EVEN and frozen at entry**: the target is
  `profit_target_pct` % of the margin the trader ENTERS (or `atr_target_mult`
  x the ATR at entry), the stop loss `stop_loss_pct` % (or `atr_stop_mult` x
  ATR), ON by default. TT reports no margin: `margin_per_contract` wins over
  the venue's, and with neither the levels cannot be priced and nothing
  enters — the window says so. It is never read as zero. The z stop, the
  mean exit (in profit only) and the time stop are OFF until a contract asks.
- **A checkbox shows what is IN FORCE, and Save writes it only if it was
  changed.** A box drawn unticked for a desk default of ON wrote OFF on the
  next Save — a stop loss switched off by saving a different field.
- **With no algo order path the algo trades on PAPER** (`Engine.paper`,
  from `gateway.connection_only`): fills simulated at the live bid/offer the
  trade would cross, `PAPER-n` tickets, `is_simulated` on the position, and
  nothing reaches the venue. The screen says PAPER; the same signal drives
  real orders once the path exists.
- **The touch study (Analysis) keeps its own rolling time window**
  (`stats.StatsWindow`), resumed across a restart only if the gap is short
  (`RESUME_MAX_GAP_MINUTES`). It is a reading for the Analysis window, not
  the Algo's band.
- **Hurst is computed on the INCREMENTS, not the levels**, and is a reading
  only. R/S over a price series reads ~1.0 for everything, a random walk
  included.
- **Statistics stay live until the window is warm.** The update interval holds
  the bands still for a trader to aim at; applied during warm-up it froze a
  sigma computed from two samples.
- **A level that reverts is not a level that pays.** The inner bands always
  revert more often — a spread one sigma from its mean comes back more
  reliably than one at three — and they are also the ones whose move cannot
  cover the round trip. Any "best level" must clear its costs, or the Analysis
  window invites lowering the threshold onto something that reverts
  beautifully and loses money every time.
- **The replay and the backtest are SIGNAL replays and say so on their own
  face.** They build candles from the recorded mids and run them through
  `backtest.run` — the live `AlgoSignal`, `judge_filters` and `levels`, never
  a second implementation of a rule. The book either side of the mid was never stored, so the
  spread is a stated assumption printed under the table; costs are the
  configured BUDGET and `slippage_measured` is None for ever; there is no
  queue. Where every entry was withheld it reports the signal's own words,
  because "nothing crossed the threshold" when the edge filter was the
  blocker sends the desk to change the number that was never the problem.
- **Slippage is measured from the DECISION price** (`slippage.py`, ported
  from the MT5 desk): each position keeps `entry_slippage` and
  `exit_slippage` in price points against the executable price when the
  Algo decided — or the touch when a close was pressed — with the order type
  each end went as. Positive is a COST at both ends (`slip` takes the side
  of the order that paid). An unfilled limit escalated to market keeps its
  ORIGINAL decision and is labelled `LIMIT escalated`: the wait is part of
  what it cost. A PAPER fill is made at the decision price by construction —
  None, never a perfect 0.00. The Analysis Costs card and the slippage
  report read the SAME measure; a trade recorded before decisions were kept
  falls back to its fills' send-touch figure. A round turn counts only with
  both ends measured, and an open position has no exit, not an unmeasured
  one.
- **A manual ticket's slippage** is its fills against the touch when it was
  SENT — the offer for a buy, the bid for a sell (`ManualTerminal._touch`).
  No fresh quote then means unmeasured, never zero. It is shown on the
  Executions table and joins the Analysis slippage card under live only.
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
