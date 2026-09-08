"""SQLite: positions, orders, fills, touches, events — and the samples that
let a restart warm up without waiting for a fresh window.

WAL with a 30-second busy timeout, because the web process reads while the
engine writes and neither may block the other into a timeout that reads on
screen as a dead engine.

The one keying rule worth stating: **fills are keyed (venue, exec_id)**. Two
venues' identical execution ids must not overwrite each other, and a system
that keys on exec_id alone loses a fill the first time a second venue is
added — silently, and only for the overlapping ids.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import (ExitReason, Position, Side, TouchEvent, TouchState)

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_key TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_price REAL NOT NULL,
    opened_at TEXT,
    entry_z REAL, entry_mean REAL, entry_std REAL, entry_half_life REAL,
    margin_locked REAL,
    break_even REAL, target_price REAL, stop_price REAL,
    closed_at TEXT, exit_price REAL, exit_z REAL, exit_reason TEXT,
    gross_pnl REAL, fees_paid REAL, net_pnl REAL, pnl_pct_on_margin REAL,
    is_simulated INTEGER DEFAULT 0,
    tickets TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_positions_key ON positions(contract_key);
CREATE INDEX IF NOT EXISTS ix_positions_open ON positions(closed_at);

CREATE TABLE IF NOT EXISTS orders (
    clordid TEXT PRIMARY KEY,
    venue_id TEXT, contract_key TEXT, side TEXT, qty REAL, filled_qty REAL,
    price REAL, order_type TEXT, intent TEXT, state TEXT, text TEXT,
    reason TEXT, position_id INTEGER, sent_at TEXT, updated_at TEXT,
    sent_at_touch REAL, is_simulated INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_orders_key ON orders(contract_key);

CREATE TABLE IF NOT EXISTS fills (
    venue TEXT NOT NULL,
    exec_id TEXT NOT NULL,
    clordid TEXT, contract_key TEXT, side TEXT, qty REAL, price REAL,
    fees REAL, slippage_ticks REAL, venue_ts TEXT, our_ts TEXT,
    PRIMARY KEY (venue, exec_id)
);
CREATE INDEX IF NOT EXISTS ix_fills_key ON fills(contract_key);

CREATE TABLE IF NOT EXISTS sd_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_key TEXT NOT NULL, ts TEXT NOT NULL,
    level REAL, direction TEXT, price REAL, z REAL, mean REAL, std REAL,
    half_life REAL, algo_armed INTEGER, became_trade INTEGER DEFAULT 0,
    state TEXT DEFAULT 'UNRESOLVED', resolved_at TEXT,
    seconds_to_revert REAL, adverse_sigma REAL
);
CREATE INDEX IF NOT EXISTS ix_touches_key ON sd_touches(contract_key, level);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, contract_key TEXT, kind TEXT, text TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(id);

CREATE TABLE IF NOT EXISTS samples (
    contract_key TEXT NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_samples_key ON samples(contract_key, ts);
"""


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _dt(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class Database:
    def __init__(self, path: str = "fixtrader.db"):
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- positions -------------------------------------------------------

    def save_position(self, pos: Position) -> int:
        """Written on EVERY change: the book has to survive a crash between
        the fill and the next poll."""
        with self._lock, self._connect() as conn:
            row = (pos.contract_key, pos.side.value, pos.qty, pos.avg_price,
                   _iso(pos.opened_at), pos.entry_z, pos.entry_mean,
                   pos.entry_std, pos.entry_half_life, pos.margin_locked,
                   pos.break_even, pos.target_price, pos.stop_price,
                   _iso(pos.closed_at), pos.exit_price, pos.exit_z,
                   pos.exit_reason.value if pos.exit_reason else None,
                   pos.gross_pnl, pos.fees_paid, pos.net_pnl,
                   pos.pnl_pct_on_margin, int(pos.is_simulated),
                   json.dumps(pos.tickets))
            if pos.id is None:
                cur = conn.execute(
                    "INSERT INTO positions (contract_key, side, qty, avg_price,"
                    " opened_at, entry_z, entry_mean, entry_std,"
                    " entry_half_life, margin_locked, break_even, target_price,"
                    " stop_price, closed_at, exit_price, exit_z, exit_reason,"
                    " gross_pnl, fees_paid, net_pnl, pnl_pct_on_margin,"
                    " is_simulated, tickets) VALUES (" + ",".join("?" * 23) + ")",
                    row)
                pos.id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE positions SET contract_key=?, side=?, qty=?,"
                    " avg_price=?, opened_at=?, entry_z=?, entry_mean=?,"
                    " entry_std=?, entry_half_life=?, margin_locked=?,"
                    " break_even=?, target_price=?, stop_price=?, closed_at=?,"
                    " exit_price=?, exit_z=?, exit_reason=?, gross_pnl=?,"
                    " fees_paid=?, net_pnl=?, pnl_pct_on_margin=?,"
                    " is_simulated=?, tickets=? WHERE id=?",
                    row + (pos.id,))
            conn.commit()
        return pos.id

    @staticmethod
    def _position_from_row(r: sqlite3.Row) -> Position:
        return Position(
            id=r['id'], contract_key=r['contract_key'], side=Side(r['side']),
            qty=r['qty'], avg_price=r['avg_price'],
            opened_at=_dt(r['opened_at']), entry_z=r['entry_z'],
            entry_mean=r['entry_mean'], entry_std=r['entry_std'],
            entry_half_life=r['entry_half_life'],
            margin_locked=r['margin_locked'], break_even=r['break_even'],
            target_price=r['target_price'], stop_price=r['stop_price'],
            closed_at=_dt(r['closed_at']), exit_price=r['exit_price'],
            exit_z=r['exit_z'],
            exit_reason=ExitReason(r['exit_reason']) if r['exit_reason'] else None,
            gross_pnl=r['gross_pnl'], fees_paid=r['fees_paid'],
            net_pnl=r['net_pnl'], pnl_pct_on_margin=r['pnl_pct_on_margin'],
            is_simulated=bool(r['is_simulated']),
            tickets=json.loads(r['tickets'] or '[]'))

    def open_positions(self) -> List[Position]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM positions WHERE closed_at IS NULL").fetchall()
        return [self._position_from_row(r) for r in rows]

    def closed_positions(self, contract_key: Optional[str] = None,
                         limit: int = 500,
                         include_simulated: bool = True,
                         only_simulated: bool = False) -> List[Position]:
        sql = "SELECT * FROM positions WHERE closed_at IS NOT NULL"
        args: List[Any] = []
        if contract_key:
            sql += " AND contract_key = ?"
            args.append(contract_key)
        if only_simulated:
            sql += " AND is_simulated = 1"
        elif not include_simulated:
            sql += " AND is_simulated = 0"
        sql += " ORDER BY closed_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._position_from_row(r) for r in rows]

    # -- orders and fills -------------------------------------------------

    def save_order(self, order: Dict[str, Any]) -> None:
        cols = ('clordid', 'venue_id', 'contract_key', 'side', 'qty',
                'filled_qty', 'price', 'order_type', 'intent', 'state', 'text',
                'reason', 'position_id', 'sent_at', 'updated_at',
                'sent_at_touch', 'is_simulated')
        values = [order.get(c) for c in cols]
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO orders ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})", values)
            conn.commit()

    def save_fill(self, fill) -> None:
        """INSERT OR IGNORE: a venue that resends an execution report must not
        double-count the fill."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO fills (venue, exec_id, clordid,"
                " contract_key, side, qty, price, fees, slippage_ticks,"
                " venue_ts, our_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (fill.venue, fill.exec_id, fill.clordid, fill.contract_key,
                 fill.side.value, fill.qty, fill.price, fill.fees,
                 fill.slippage_ticks, _iso(fill.venue_ts), _iso(fill.our_ts)))
            conn.commit()

    def fills(self, contract_key: Optional[str] = None,
              limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM fills"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY our_ts DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def orders(self, contract_key: Optional[str] = None,
               limit: int = 500) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM orders"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY sent_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -- touches ----------------------------------------------------------

    def save_touch(self, touch: TouchEvent) -> int:
        with self._lock, self._connect() as conn:
            if touch.id is None:
                cur = conn.execute(
                    "INSERT INTO sd_touches (contract_key, ts, level,"
                    " direction, price, z, mean, std, half_life, algo_armed,"
                    " became_trade, state, resolved_at, seconds_to_revert,"
                    " adverse_sigma) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (touch.contract_key, _iso(touch.ts), touch.level,
                     touch.direction, touch.price, touch.z, touch.mean,
                     touch.std, touch.half_life, int(touch.algo_armed),
                     int(touch.became_trade), touch.state.value,
                     _iso(touch.resolved_at), touch.seconds_to_revert,
                     touch.adverse_sigma))
                touch.id = cur.lastrowid
            else:
                conn.execute(
                    "UPDATE sd_touches SET state=?, resolved_at=?,"
                    " seconds_to_revert=?, adverse_sigma=?, became_trade=?"
                    " WHERE id=?",
                    (touch.state.value, _iso(touch.resolved_at),
                     touch.seconds_to_revert, touch.adverse_sigma,
                     int(touch.became_trade), touch.id))
            conn.commit()
        return touch.id

    def touches(self, contract_key: Optional[str] = None,
                limit: int = 5000) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sd_touches"
        args: List[Any] = []
        if contract_key:
            sql += " WHERE contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    # -- events and samples ------------------------------------------------

    def log_event(self, kind: str, text: str, contract_key: str = "",
                  detail: Any = None, ts: Optional[datetime] = None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO events (ts, contract_key, kind, text, detail)"
                " VALUES (?,?,?,?,?)",
                (_iso(ts or datetime.now(timezone.utc)), contract_key, kind,
                 text, json.dumps(detail) if detail is not None else None))
            conn.commit()

    def events(self, since_id: int = 0, limit: int = 200,
               contract_key: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM events WHERE id > ?"
        args: List[Any] = [since_id]
        if contract_key:
            sql += " AND contract_key = ?"
            args.append(contract_key)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    def save_samples(self, key: str, rows: List[tuple]) -> None:
        if not rows:
            return
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO samples (contract_key, ts, price) VALUES (?,?,?)",
                [(key, _iso(ts), price) for ts, price in rows])
            conn.commit()

    def recent_samples(self, key: str, limit: int = 400) -> List[float]:
        """Oldest-first, so the window can be refilled in order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT price FROM samples WHERE contract_key = ?"
                " ORDER BY ts DESC LIMIT ?", (key, limit)).fetchall()
        return [r['price'] for r in reversed(rows)]

    def trim_samples(self, key: str, keep: int = 2000) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM samples WHERE contract_key = ? AND rowid NOT IN "
                "(SELECT rowid FROM samples WHERE contract_key = ?"
                " ORDER BY ts DESC LIMIT ?)", (key, key, keep))
            conn.commit()
