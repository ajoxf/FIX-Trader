# Market-data `position`

The `position` column comes from FIX tag **290** (`MDEntryPositionNo`). It is the
price level's rank on its own side of the order book, measured from the best
available price.

| Position | BID meaning | ASK meaning |
|---:|---|---|
| 1 | Best/highest bid | Best/lowest ask |
| 2 | Second-best bid | Second-best ask |
| 3 | Third-best bid | Third-best ask |
| 4+ | Progressively deeper bid levels | Progressively deeper ask levels |

Bid and ask positions are separate. A BID at position 1 and an ASK at position
1 are both top-of-book entries.

## Reading the tick tape

Use `position` together with `entry_type` and `update`:

- `BID + CHANGE + position 1`: the best bid level changed.
- `ASK + NEW + position 2`: a new ask level was inserted at the second-best level.
- `ASK + DELETE + position 3`: remove the third-best ask level from the book state
  that existed before processing that incremental message.
- A blank position is normal for entries that are not book levels, such as trades.

For the highlighted screenshot row:

```text
update=DELETE, entry_type=ASK, price=9485, size=1, position=3
```

This means TT instructed the client to delete the ask price level at **ask depth
3**. It does not mean three contracts, three ticks of price movement, or the
third message received.

## Important incremental-update rule

A TT Market Data Incremental Refresh (`35=X`) can contain several updates for
the same instrument and side. TT specifies that tag 290 refers to the book
position **before the current incremental message is processed**. Therefore,
do not delete one row and immediately reinterpret the later positions against
the already-shifted ladder. Apply the message as one ordered book update using
a before/after book state.

## Related FIX fields

| FIX tag | Tick-tape column | Meaning |
|---:|---|---|
| 269 | `entry_type` | BID, ASK, TRADE, or another market-data type |
| 270 | `price` | Price associated with the entry |
| 271 | `size` | Quantity at that price level |
| 279 | `update` | `NEW`, `CHANGE`, or `DELETE` |
| 290 | `position` | Rank relative to the best bid or best ask |

## Official TT references

- [TT Market Data Incremental Refresh (X)](https://library.tradingtechnologies.com/tt-fix/tt-fix-market-data/supported-application-messages-tt-fix-market-data/market-data-incremental-refresh-x-message/)
- [TT Market Data Snapshot (W)](https://library.tradingtechnologies.com/tt-fix/tt-fix-market-data/supported-application-messages-tt-fix-market-data/market-data-snapshot-w-message/)

