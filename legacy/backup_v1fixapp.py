"""
TT FIX Trading Terminal — single-file Streamlit application.

Connects to Trading Technologies (TT) FIX Order Routing and FIX Market Data
services using the QuickFIX engine. Supports a MOCK_MODE for safe UI testing
without any real TT connectivity.

IMPORTANT — things NOT verifiable from the public TT docs and left as
config, per your request not to guess TT-specific behavior:
  - Exact UAT host/port for your FIX Order Routing and FIX Market Data
    gateways (TT-Setup-specific, not in the sample you gave me)
  - TT's TargetCompID value for your sessions (separate from your own
    SenderCompID / "Remote Comp Id")
  - Whether your "AJUAT" / "33986" value belongs in Tag 50 (SenderSubID)
    or Tag 116 (OnBehalfOfSubID) — both exist in TT FIX and serve
    different purposes; confirm with TT which your session was
    provisioned for
  - Full list of order types / TIFs enabled for your account (varies by
    exchange and TT Setup configuration)

Verified against TT's public FIX documentation:
  - FIX.4.2 message set for Order Routing / Market Data
  - Session password is sent in Tag 96 (RawData) on Logon, not a
    generic password field
  - SenderCompID (49) = your Remote Comp Id, TargetCompID (56) = TT's
    LocalCompId for your session
  - Tag 141 (ResetSeqNumFlag) / Tag 34 (MsgSeqNum) logon negotiation
    rules, per "Logon (A) Message" — TT FIX Help
  - ClOrdID (11) must be unique per session since the last scheduled
    reset (Sat 22:00 UTC by default)

Run:
    streamlit run app.py

Requires (see requirements.txt):
    streamlit, quickfix

Credentials come ONLY from .streamlit/secrets.toml (see
.streamlit/secrets.toml.example) or environment variables — never
hardcode a password in this file.
"""

from __future__ import annotations

import os
import io
import json
import queue
import random
import socket
import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import streamlit as st

# ---------------------------------------------------------------------------
# QuickFIX is optional at import time so the app still boots in MOCK_MODE
# on a machine where the (C++-backed) quickfix package hasn't been built.
# ---------------------------------------------------------------------------
try:
    import quickfix as fix  # type: ignore
    QUICKFIX_AVAILABLE = True
except ImportError:
    QUICKFIX_AVAILABLE = False

# The native client below is used on Windows when QuickFIX's legacy extension
# cannot be built. It implements the narrow FIX 4.2 session subset this app
# uses, without a C++ dependency.
NATIVE_FIX_AVAILABLE = True
REAL_FIX_AVAILABLE = QUICKFIX_AVAILABLE or NATIVE_FIX_AVAILABLE

# Streamlit displays the latest in-memory FIX snapshot at this cadence. The
# FIX socket callbacks themselves remain event-driven and update immediately.
MARKET_DATA_REFRESH_INTERVAL = "100ms"


# ===========================================================================
# FIX TAG CONSTANTS  (kept in one place — do not scatter magic numbers)
# These are the tags this app actually uses. Verify any you extend against
# the TT FIX Tag Directory: https://library.tradingtechnologies.com/tt-fix/
# ===========================================================================
class Tag:
    BeginString = "8"
    BodyLength = "9"
    MsgType = "35"
    SenderCompID = "49"
    TargetCompID = "56"
    SenderSubID = "50"
    TargetSubID = "57"
    OnBehalfOfSubID = "116"
    MsgSeqNum = "34"
    SendingTime = "52"
    ResetSeqNumFlag = "141"
    RawDataLength = "95"
    RawData = "96"            # <-- TT session password goes here (per docs)
    HeartBtInt = "108"
    Account = "1"
    ClOrdID = "11"
    OrigClOrdID = "41"
    Symbol = "55"
    SecurityID = "48"
    Side = "54"
    OrdType = "40"
    OrderQty = "38"
    Price = "44"
    TimeInForce = "59"
    OrdStatus = "39"
    ExecType = "150"
    CumQty = "14"
    LeavesQty = "151"
    AvgPx = "6"
    OrderID = "37"
    ExecID = "17"
    Text = "58"
    MDReqID = "262"
    SubscriptionRequestType = "263"
    MarketDepth = "264"
    NoMDEntryTypes = "267"
    MDEntryType = "269"
    MDEntryPx = "270"
    MDEntrySize = "271"
    MDUpdateAction = "279"
    SecurityExchange = "207"
    SecurityType = "167"
    MaturityMonthYear = "200"
    SecurityIDSource = "22"
    NoRelatedSym = "146"
    MDUpdateType = "265"
    AggregatedBook = "266"
    NoMDEntries = "268"
    SecurityReqID = "320"
    SecurityResponseID = "322"
    SecurityRequestType = "321"
    SecurityDesc = "107"
    Currency = "15"


MSG_TYPE_NAMES = {
    "0": "Heartbeat", "1": "TestRequest", "2": "ResendRequest",
    "3": "Reject", "4": "SequenceReset", "5": "Logout", "A": "Logon",
    "D": "NewOrderSingle", "8": "ExecutionReport", "9": "OrderCancelReject",
    "F": "OrderCancelRequest", "G": "OrderCancelReplaceRequest",
    "V": "MarketDataRequest", "W": "MarketDataSnapshotFullRefresh",
    "X": "MarketDataIncrementalRefresh", "Y": "MarketDataRequestReject",
    "c": "SecurityDefinitionRequest", "d": "SecurityDefinition",
}

SIDES = {"BUY": "1", "SELL": "2"}
ORD_TYPES = {"LIMIT": "2", "MARKET": "1", "STOP": "3", "STOP_LIMIT": "4"}
TIFS = {"DAY": "0", "GTC": "1", "IOC": "3", "FOK": "4", "GTD": "6"}

EXEC_TYPE_TO_STATUS = {
    "0": "NEW", "1": "PARTIALLY_FILLED", "2": "FILLED", "4": "CANCELED",
    "5": "REPLACED", "6": "PENDING", "8": "REJECTED", "C": "EXPIRED",
    "A": "PENDING",
}

SENSITIVE_TAGS = {Tag.RawData, Tag.RawDataLength}


def mask_raw_message(raw_fix: str) -> str:
    """Mask password fields (Tag 96) before logging/displaying a raw FIX message."""
    if not raw_fix:
        return raw_fix
    sep = "\x01" if "\x01" in raw_fix else "|"
    fields = raw_fix.split(sep)
    masked = []
    for f in fields:
        if "=" in f:
            k, _, v = f.partition("=")
            if k == Tag.RawData:
                f = f"{k}=****"
        masked.append(f)
    return sep.join(masked)


def value_from_group(group, tag: int, default=""):
    """Read an optional field from a QuickFIX repeating group."""
    try:
        field = fix.StringField(tag)
        group.getField(field)
        return field.getValue()
    except Exception:
        return default


# ===========================================================================
# DATA MODELS
# ===========================================================================
@dataclass
class OrderRecord:
    client_order_id: str
    account: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: Optional[float]
    tif: str
    status: str = "PENDING"
    exchange_order_id: str = ""
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_px: float = 0.0
    reject_reason: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    raw_last_report: str = ""


@dataclass
class FixLogEntry:
    ts: str
    session: str
    direction: str  # IN / OUT
    msg_type: str
    seq_num: str
    raw: str


@dataclass
class MarketDataRecord:
    symbol: str
    bid: Optional[float] = None
    bid_size: Optional[float] = None
    ask: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    last_size: Optional[float] = None
    timestamp: str = ""


@dataclass
class MarketDataTick:
    received_at: str
    exchange_time: str
    sequence: str
    symbol: str
    update: str
    entry_type: str
    price: Optional[float]
    size: Optional[float]
    entry_id: str = ""
    position: str = ""


@dataclass
class SymbolMapping:
    """Maps a downstream feed/maker symbol to the TT instrument to trade."""
    bridge_symbol: str
    maker: str
    maker_symbol: str
    tt_symbol: str
    tt_exchange: str = ""
    tt_security_id: str = ""
    security_type: str = ""
    price_digits: int = 5
    enabled: bool = True
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class InstrumentDefinition:
    """A TT instrument returned by a Security Definition (35=d) message."""
    security_id: str
    symbol: str
    exchange: str = ""
    security_type: str = ""
    maturity_month_year: str = ""
    description: str = ""
    currency: str = ""
    security_req_id: str = ""
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ===========================================================================
# PERSISTENCE (SQLite)
# ===========================================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "storage.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS orders (
        client_order_id TEXT PRIMARY KEY, account TEXT, symbol TEXT, side TEXT,
        order_type TEXT, quantity REAL, price REAL, tif TEXT, status TEXT,
        exchange_order_id TEXT, filled_qty REAL, remaining_qty REAL, avg_px REAL,
        reject_reason TEXT, created_at TEXT, updated_at TEXT, raw_last_report TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS executions (
        execution_id TEXT PRIMARY KEY, client_order_id TEXT, exchange_order_id TEXT,
        symbol TEXT, side TEXT, quantity REAL, price REAL, execution_type TEXT,
        execution_status TEXT, timestamp TEXT, raw_message TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS symbol_mappings (
        bridge_symbol TEXT PRIMARY KEY, maker TEXT NOT NULL, maker_symbol TEXT NOT NULL,
        tt_symbol TEXT NOT NULL, tt_exchange TEXT, tt_security_id TEXT,
        security_type TEXT, price_digits INTEGER NOT NULL, enabled INTEGER NOT NULL,
        updated_at TEXT NOT NULL)""")
    conn.commit()
    return conn


def save_order(conn, o: OrderRecord):
    d = asdict(o)
    cols = ",".join(d.keys())
    ph = ",".join(["?"] * len(d))
    conn.execute(f"INSERT OR REPLACE INTO orders ({cols}) VALUES ({ph})", list(d.values()))
    conn.commit()


def save_execution(conn, exec_id, client_order_id, exchange_order_id, symbol, side,
                    qty, price, exec_type, exec_status, raw_message):
    conn.execute(
        "INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (exec_id, client_order_id, exchange_order_id, symbol, side, qty, price,
         exec_type, exec_status, datetime.now(timezone.utc).isoformat(), raw_message),
    )
    conn.commit()


def save_symbol_mapping(conn, mapping: SymbolMapping):
    conn.execute(
        """INSERT OR REPLACE INTO symbol_mappings
        (bridge_symbol, maker, maker_symbol, tt_symbol, tt_exchange, tt_security_id,
         security_type, price_digits, enabled, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mapping.bridge_symbol, mapping.maker, mapping.maker_symbol, mapping.tt_symbol,
         mapping.tt_exchange, mapping.tt_security_id, mapping.security_type,
         mapping.price_digits, int(mapping.enabled), mapping.updated_at),
    )
    conn.commit()


# ===========================================================================
# SESSION STATE (per FIX session: MarketData / OrderRouting)
# Thread-safe: the network thread mutates via a lock + queues; the
# Streamlit script only reads.
# ===========================================================================
class FixSessionState:
    def __init__(self, name: str):
        self.name = name
        self.lock = threading.Lock()
        self.status = "DISCONNECTED"  # DISCONNECTED/CONNECTING/CONNECTED/LOGGING_OUT/ERROR
        self.sender_comp_id = ""
        self.target_comp_id = ""
        self.last_heartbeat = ""
        self.last_message = ""
        self.last_logon = ""
        self.last_logout = ""
        self.in_seq = 0
        self.out_seq = 0
        self.incoming_count = 0
        self.outgoing_count = 0
        self.error = ""

    def snapshot(self):
        with self.lock:
            return dict(
                status=self.status, sender_comp_id=self.sender_comp_id,
                target_comp_id=self.target_comp_id, last_heartbeat=self.last_heartbeat,
                last_message=self.last_message, last_logon=self.last_logon,
                last_logout=self.last_logout, in_seq=self.in_seq, out_seq=self.out_seq,
                incoming_count=self.incoming_count, outgoing_count=self.outgoing_count,
                error=self.error,
            )


class AppService:
    """
    Service holding both FIX sessions, queues, and the DB connection. It is
    retained in Streamlit session state so ordinary reruns and source reloads
    do not create duplicate FIX socket threads for the same browser session.
    """
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self.md_state = FixSessionState("MarketData")
        self.or_state = FixSessionState("OrderRouting")
        self.fix_log: "queue.Queue[FixLogEntry]" = queue.Queue()
        self.fix_log_all: list[FixLogEntry] = []
        self.market_data_lock = threading.RLock()
        self.market_data: dict[str, MarketDataRecord] = {}
        self.market_subscriptions: dict[str, dict] = {}
        self.market_data_rejections: dict[str, str] = {}
        self.market_data_ticks: deque[MarketDataTick] = deque(maxlen=5000)
        self.market_data_tick_count = 0
        self.instrument_definitions: dict[str, InstrumentDefinition] = {}
        self.last_security_definition_request = ""
        self.symbol_mappings: dict[str, SymbolMapping] = {}
        self.orders: dict[str, OrderRecord] = {}
        self.md_initiator = None
        self.or_initiator = None
        self.md_app = None
        self.or_app = None
        self.mock_thread_running = False
        self.db_lock = threading.Lock()
        self.conn = get_db()
        self._load_orders_from_db()
        self._load_symbol_mappings()

    def _load_orders_from_db(self):
        cur = self.conn.execute("SELECT * FROM orders")
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            self.orders[d["client_order_id"]] = OrderRecord(**d)

    def _load_symbol_mappings(self):
        cur = self.conn.execute("SELECT * FROM symbol_mappings ORDER BY bridge_symbol")
        columns = [column[0] for column in cur.description]
        for row in cur.fetchall():
            data = dict(zip(columns, row))
            data["enabled"] = bool(data["enabled"])
            self.symbol_mappings[data["bridge_symbol"]] = SymbolMapping(**data)

    @classmethod
    def get(cls) -> "AppService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = AppService()
            return cls._instance

    # ---- logging -----------------------------------------------------
    def log_fix(self, session: str, direction: str, msg_type: str, seq_num: str, raw: str):
        entry = FixLogEntry(
            ts=datetime.now(timezone.utc).isoformat(), session=session,
            direction=direction, msg_type=MSG_TYPE_NAMES.get(msg_type, msg_type),
            seq_num=seq_num, raw=mask_raw_message(raw),
        )
        self.fix_log_all.append(entry)
        if len(self.fix_log_all) > 5000:
            self.fix_log_all = self.fix_log_all[-3000:]

    def upsert_order(self, order: OrderRecord):
        self.orders[order.client_order_id] = order
        with self.db_lock:
            save_order(self.conn, order)

    def upsert_symbol_mapping(self, mapping: SymbolMapping):
        mapping.updated_at = datetime.now(timezone.utc).isoformat()
        self.symbol_mappings[mapping.bridge_symbol] = mapping
        with self.db_lock:
            save_symbol_mapping(self.conn, mapping)

    def delete_symbol_mapping(self, bridge_symbol: str):
        with self.db_lock:
            self.conn.execute("DELETE FROM symbol_mappings WHERE bridge_symbol = ?", (bridge_symbol,))
            self.conn.commit()
        self.symbol_mappings.pop(bridge_symbol, None)

    def apply_native_security_definition(self, fields: dict[str, str], raw: str):
        """Store a TT Security Definition response for the instrument picker."""
        security_id = fields.get(Tag.SecurityID, "")
        symbol = fields.get(Tag.Symbol, "")
        if not security_id or not symbol:
            return
        definition = InstrumentDefinition(
            security_id=security_id,
            symbol=symbol,
            exchange=fields.get(Tag.SecurityExchange, fields.get("100", "")),
            security_type=fields.get(Tag.SecurityType, ""),
            maturity_month_year=fields.get(Tag.MaturityMonthYear, ""),
            description=fields.get(Tag.SecurityDesc, ""),
            currency=fields.get(Tag.Currency, ""),
            security_req_id=fields.get(Tag.SecurityReqID, ""),
        )
        self.instrument_definitions[security_id] = definition

    def apply_security_definition(self, message):
        """QuickFIX equivalent of apply_native_security_definition."""
        self.apply_native_security_definition(parse_fix_message(message.toString()), message.toString())


    def record_execution(self, exec_id, client_order_id, exchange_order_id, symbol,
                          side, qty, price, exec_type, exec_status, raw_message):
        with self.db_lock:
            save_execution(self.conn, exec_id, client_order_id, exchange_order_id,
                            symbol, side, qty, price, exec_type, exec_status, raw_message)

    def apply_execution_report(self, message):
        """Persist the execution state from an incoming ExecutionReport (35=8)."""
        def value(tag, default=""):
            try:
                field = fix.StringField(tag)
                message.getField(field)
                return field.getValue()
            except Exception:  # optional FIX field
                return default

        cl_ord_id = value(int(Tag.ClOrdID))
        if not cl_ord_id:
            return
        order = self.orders.get(cl_ord_id)
        if order is None:
            return  # Do not fabricate an order from an uncorrelated report.
        exec_type = value(int(Tag.ExecType))
        order.status = EXEC_TYPE_TO_STATUS.get(value(int(Tag.OrdStatus), exec_type), "PENDING")
        order.exchange_order_id = value(int(Tag.OrderID), order.exchange_order_id)
        order.filled_qty = float(value(int(Tag.CumQty), str(order.filled_qty)) or 0)
        order.remaining_qty = float(value(int(Tag.LeavesQty), str(order.remaining_qty)) or 0)
        order.avg_px = float(value(int(Tag.AvgPx), str(order.avg_px)) or 0)
        order.reject_reason = value(int(Tag.Text), "") if order.status == "REJECTED" else ""
        order.updated_at = datetime.now(timezone.utc).isoformat()
        raw = message.toString()
        order.raw_last_report = raw
        self.upsert_order(order)
        exec_id = value(int(Tag.ExecID))
        if exec_id and exec_type in ("1", "2", "F"):
            self.record_execution(exec_id, cl_ord_id, order.exchange_order_id, order.symbol,
                                  order.side, order.filled_qty, order.avg_px, exec_type,
                                  order.status, raw)

    def apply_market_data(self, message):
        """Route QuickFIX messages through the lossless repeating-tag parser."""
        raw = message.toString()
        self.apply_native_market_data(parse_fix_message(raw), raw)

    def apply_native_execution_report(self, fields: dict[str, str], raw: str):
        cl_ord_id = fields.get(Tag.ClOrdID, "")
        order = self.orders.get(cl_ord_id)
        if order is None:
            return
        order.status = EXEC_TYPE_TO_STATUS.get(fields.get(Tag.OrdStatus, fields.get(Tag.ExecType, "")), "PENDING")
        order.exchange_order_id = fields.get(Tag.OrderID, order.exchange_order_id)
        for attr, tag in (("filled_qty", Tag.CumQty), ("remaining_qty", Tag.LeavesQty), ("avg_px", Tag.AvgPx)):
            try:
                setattr(order, attr, float(fields.get(tag, getattr(order, attr))))
            except (TypeError, ValueError):
                pass
        order.reject_reason = fields.get(Tag.Text, "") if order.status == "REJECTED" else ""
        order.updated_at = datetime.now(timezone.utc).isoformat()
        order.raw_last_report = raw
        self.upsert_order(order)
        exec_id = fields.get(Tag.ExecID, "")
        if exec_id and fields.get(Tag.ExecType) in ("1", "2", "F"):
            self.record_execution(exec_id, cl_ord_id, order.exchange_order_id, order.symbol,
                                  order.side, order.filled_qty, order.avg_px,
                                  fields.get(Tag.ExecType, ""), order.status, raw)

    def apply_native_market_data(self, fields: dict[str, str], raw: str):
        message_type = fields.get(Tag.MsgType, "")
        default_symbol = fields.get(Tag.Symbol, "")
        # Preserve the ordered repeating groups. A dict loses repeated tags,
        # which would discard ticks in snapshots and incremental refreshes.
        entries: list[dict[str, str]] = []
        current: Optional[dict[str, str]] = None
        in_entries = False
        for tag, value in parse_fix_pairs(raw):
            if tag == Tag.NoMDEntries:
                in_entries = True
                continue
            if not in_entries:
                continue
            if tag == Tag.MDUpdateAction:
                if current:
                    entries.append(current)
                current = {tag: value}
            elif tag == Tag.MDEntryType:
                if current and Tag.MDEntryType in current:
                    entries.append(current)
                    current = {}
                if current is None:
                    current = {}
                current[tag] = value
            elif current is not None:
                current[tag] = value
        if current:
            entries.append(current)

        if not entries:
            return

        received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        entry_type_names = {"0": "BID", "1": "ASK", "2": "TRADE"}
        update_names = {"0": "NEW", "1": "CHANGE", "2": "DELETE"}
        with self.market_data_lock:
            for entry in entries:
                symbol = entry.get(Tag.Symbol) or default_symbol
                if not symbol and len(self.market_subscriptions) == 1:
                    symbol = next(iter(self.market_subscriptions))
                if not symbol:
                    continue
                entry_type = entry.get(Tag.MDEntryType, "")
                action = entry.get(Tag.MDUpdateAction, "SNAPSHOT" if message_type == "W" else "")
                px, size = entry.get(Tag.MDEntryPx), entry.get(Tag.MDEntrySize)
                try:
                    price = float(px) if px not in (None, "") else None
                    quantity = float(size) if size not in (None, "") else None
                except (TypeError, ValueError):
                    price, quantity = None, None

                tick = MarketDataTick(
                    received_at=received_at,
                    exchange_time=entry.get("273", fields.get("52", "")),
                    sequence=fields.get(Tag.MsgSeqNum, ""),
                    symbol=symbol,
                    update=update_names.get(action, action),
                    entry_type=entry_type_names.get(entry_type, entry_type),
                    price=price,
                    size=quantity,
                    entry_id=entry.get("278", ""),
                    position=entry.get("290", ""),
                )
                self.market_data_ticks.append(tick)
                self.market_data_tick_count += 1

                # Deletions are preserved on the tick tape but must not replace
                # the latest displayed price with an absent/deleted book level.
                if action == "2" or price is None:
                    continue
                record = self.market_data.setdefault(symbol, MarketDataRecord(symbol=symbol))
                if entry_type == "0":
                    record.bid, record.bid_size = price, quantity
                elif entry_type == "1":
                    record.ask, record.ask_size = price, quantity
                elif entry_type == "2":
                    record.last, record.last_size = price, quantity
                record.timestamp = received_at

    def apply_native_market_data_reject(self, fields: dict[str, str], raw: str):
        request_id = fields.get(Tag.MDReqID, "")
        reason = fields.get(Tag.Text, "TT rejected the market-data request without a text reason.")
        if request_id:
            with self.market_data_lock:
                self.market_data_rejections[request_id] = reason

    def market_data_snapshot(self):
        """Return a consistent copy for the high-frequency UI fragment."""
        with self.market_data_lock:
            return (
                [asdict(record) for record in self.market_data.values()],
                [(symbol, dict(subscription)) for symbol, subscription in self.market_subscriptions.items()],
                dict(self.market_data_rejections),
                [asdict(tick) for _, tick in zip(range(250), reversed(self.market_data_ticks))],
                self.market_data_tick_count,
            )

    def apply_market_data_reject(self, message):
        self.apply_native_market_data_reject(parse_fix_message(message.toString()), message.toString())


def gen_client_order_id() -> str:
    return f"APP-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def active_symbol_mappings(svc: AppService) -> list[SymbolMapping]:
    return sorted(
        (mapping for mapping in svc.symbol_mappings.values() if mapping.enabled),
        key=lambda mapping: mapping.bridge_symbol,
    )


def mapping_label(mapping: SymbolMapping) -> str:
    return (f"{mapping.bridge_symbol}  →  {mapping.tt_symbol}"
            f"  |  {mapping.maker}: {mapping.maker_symbol}")


# ===========================================================================
# MOCK MODE ENGINE
# Simulates logon, market data ticks, and order acks/fills so the full UI
# can be exercised with zero TT connectivity, per your MOCK_MODE spec.
# ===========================================================================
def mock_worker(svc: AppService, stop_event: threading.Event):
    symbols = ["EURUSD", "XAUUSD", "ES"]
    last_hb = time.time()
    while not stop_event.is_set():
        time.sleep(0.1)
        now = time.time()
        if now - last_hb >= 5:
            last_hb = now
            for state in (svc.md_state, svc.or_state):
                with state.lock:
                    if state.status == "CONNECTED":
                        state.last_heartbeat = datetime.now(timezone.utc).isoformat()
                        state.in_seq += 1
                        state.out_seq += 1
                        state.incoming_count += 1
                        state.outgoing_count += 1
                svc.log_fix(state.name, "OUT", "0", str(state.out_seq), "8=FIX.4.2|35=0|")

        for sym in symbols:
            with svc.market_data_lock:
                if sym not in svc.market_data:
                    continue
                rec = svc.market_data[sym]
                mid = (rec.bid or 100) + random.uniform(-0.05, 0.05)
                rec.bid = round(mid - 0.01, 5)
                rec.ask = round(mid + 0.01, 5)
                rec.bid_size = random.randint(1, 20)
                rec.ask_size = random.randint(1, 20)
                rec.last = round(mid, 5)
                rec.last_size = random.randint(1, 5)
                rec.timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                for entry_type, price, size in (
                    ("BID", rec.bid, rec.bid_size),
                    ("ASK", rec.ask, rec.ask_size),
                    ("TRADE", rec.last, rec.last_size),
                ):
                    svc.market_data_ticks.append(MarketDataTick(
                        received_at=rec.timestamp, exchange_time=rec.timestamp,
                        sequence=str(svc.md_state.in_seq + 1), symbol=sym,
                        update="CHANGE", entry_type=entry_type,
                        price=price, size=size,
                    ))
                    svc.market_data_tick_count += 1
            with svc.md_state.lock:
                svc.md_state.incoming_count += 1
                svc.md_state.in_seq += 1

        # progress working orders toward a fill
        for o in list(svc.orders.values()):
            if o.status in ("NEW", "PARTIALLY_FILLED") and random.random() < 0.3:
                fill_qty = min(o.remaining_qty, max(1, round(o.quantity * random.uniform(0.2, 0.6))))
                o.filled_qty += fill_qty
                o.remaining_qty = max(0, o.quantity - o.filled_qty)
                o.avg_px = o.price or o.avg_px or 100.0
                o.status = "FILLED" if o.remaining_qty <= 0 else "PARTIALLY_FILLED"
                o.updated_at = datetime.now(timezone.utc).isoformat()
                exec_type = "2" if o.status == "FILLED" else "1"
                raw = f"8=FIX.4.2|35=8|11={o.client_order_id}|150={exec_type}|39={exec_type}|"
                svc.log_fix("OrderRouting", "IN", "8", str(svc.or_state.in_seq + 1), raw)
                svc.or_state.in_seq += 1
                svc.or_state.incoming_count += 1
                svc.record_execution(str(uuid.uuid4()), o.client_order_id, o.exchange_order_id,
                                      o.symbol, o.side, fill_qty, o.avg_px, exec_type, o.status, raw)
                svc.upsert_order(o)


def start_mock_sessions(svc: AppService):
    """Create both simulated sessions before the first Streamlit render."""
    for state, sender, target in (
        (svc.md_state, "AJUATMARKET", "TT"),
        (svc.or_state, "AJUATORDER", "TT"),
    ):
        with state.lock:
            state.status = "CONNECTED"
            state.sender_comp_id = sender
            state.target_comp_id = target
            state.last_logon = datetime.now(timezone.utc).isoformat()
            state.out_seq = 1
            state.in_seq = 1
            state.outgoing_count += 1
            state.incoming_count += 1
        svc.log_fix(state.name, "OUT", "A", "1", f"8=FIX.4.2|35=A|49={sender}|56={target}|96=****|")
        svc.log_fix(state.name, "IN", "A", "1", f"8=FIX.4.2|35=A|49={target}|56={sender}|")


def ensure_mock_thread(svc: AppService):
    if not svc.mock_thread_running:
        start_mock_sessions(svc)
        svc.mock_thread_running = True
        svc._mock_stop = threading.Event()
        t = threading.Thread(target=mock_worker, args=(svc, svc._mock_stop), daemon=True)
        t.start()
        svc._mock_thread = t


# ===========================================================================
# REAL QUICKFIX APPLICATION  (used only when MOCK_MODE is False)
# Implements the callbacks required by the FIX session layer: onCreate,
# onLogon, onLogout, toAdmin, fromAdmin, toApp, fromApp.
# ===========================================================================
if QUICKFIX_AVAILABLE:

    class TTFixApplication(fix.Application):
        def __init__(self, svc: AppService, state: FixSessionState, session_name: str,
                     password: str, on_execution_report=None, on_market_data=None,
                     on_security_definition=None, on_market_data_reject=None):
            super().__init__()
            self.svc = svc
            self.state = state
            self.session_name = session_name
            self.password = password
            self.on_execution_report = on_execution_report
            self.on_market_data = on_market_data
            self.on_security_definition = on_security_definition
            self.on_market_data_reject = on_market_data_reject
            self.session_id = None

        def onCreate(self, sessionID):
            self.session_id = sessionID

        def onLogon(self, sessionID):
            with self.state.lock:
                self.state.status = "CONNECTED"
                self.state.last_logon = datetime.now(timezone.utc).isoformat()
                self.state.error = ""

        def onLogout(self, sessionID):
            with self.state.lock:
                self.state.status = "DISCONNECTED"
                self.state.last_logout = datetime.now(timezone.utc).isoformat()

        def toAdmin(self, message, sessionID):
            msg_type = fix.MsgType()
            message.getHeader().getField(msg_type)
            if msg_type.getValue() == fix.MsgType_Logon:
                # Password goes in Tag 96 (RawData) per TT docs.
                message.setField(fix.RawDataLength(len(self.password)))
                message.setField(fix.RawData(self.password))
            self._log("OUT", message)

        def fromAdmin(self, message, sessionID):
            self._log("IN", message)

        def toApp(self, message, sessionID):
            self._log("OUT", message)

        def fromApp(self, message, sessionID):
            self._log("IN", message)
            msg_type = fix.MsgType()
            message.getHeader().getField(msg_type)
            mt = msg_type.getValue()
            if mt == "8" and self.on_execution_report:
                self.on_execution_report(message)
            elif mt in ("W", "X") and self.on_market_data:
                self.on_market_data(message)
            elif mt == "Y" and self.on_market_data_reject:
                self.on_market_data_reject(message)
            elif mt == "d" and self.on_security_definition:
                self.on_security_definition(message)

        def _log(self, direction, message):
            with self.state.lock:
                if direction == "OUT":
                    self.state.outgoing_count += 1
                else:
                    self.state.incoming_count += 1
                self.state.last_message = datetime.now(timezone.utc).isoformat()
            try:
                mt = fix.MsgType()
                message.getHeader().getField(mt)
                seq = fix.MsgSeqNum()
                message.getHeader().getField(seq)
                self.svc.log_fix(self.session_name, direction, mt.getValue(),
                                  str(seq.getValue()), message.toString())
            except Exception as e:  # noqa: BLE001
                self.svc.log_fix(self.session_name, direction, "?", "?", str(e))


def build_quickfix_settings(cfg: dict, session_name: str) -> "fix.SessionSettings":
    """Build a QuickFIX SessionSettings object from resolved config values."""
    settings = fix.SessionSettings()
    default_dict = fix.Dictionary()
    default_dict.setString("ConnectionType", "initiator")
    default_dict.setString("ReconnectInterval", "5")
    default_dict.setString("FileStorePath", f"./fixstore_{session_name.lower()}")
    default_dict.setString("FileLogPath", f"./logs/fixlog_{session_name.lower()}")
    default_dict.setString("StartTime", "00:00:00")
    default_dict.setString("EndTime", "00:00:00")
    default_dict.setString("UseDataDictionary", "N")
    settings.set(default_dict)

    session_dict = fix.Dictionary()
    session_dict.setString("BeginString", "FIX.4.2")
    session_dict.setString("SenderCompID", cfg["sender_comp_id"])
    session_dict.setString("TargetCompID", cfg["target_comp_id"])
    session_dict.setString("SocketConnectHost", cfg["host"])
    session_dict.setString("SocketConnectPort", str(cfg["port"]))
    session_dict.setString("HeartBtInt", str(cfg.get("heartbeat", 30)))
    session_dict.setString("ResetOnLogon", "Y")
    if cfg.get("sender_sub_id"):
        session_dict.setString("SenderSubID", cfg["sender_sub_id"])
    if cfg.get("on_behalf_of_sub_id"):
        session_dict.setString("OnBehalfOfSubID", cfg["on_behalf_of_sub_id"])

    session_id = fix.SessionID("FIX.4.2", cfg["sender_comp_id"], cfg["target_comp_id"])
    settings.set(session_id, session_dict)
    return settings, session_id


def start_real_session(svc: AppService, state: FixSessionState, session_name: str, cfg: dict,
                        on_execution_report=None, on_market_data=None, on_security_definition=None,
                        on_market_data_reject=None):
    if not QUICKFIX_AVAILABLE:
        native = NativeFixSession(svc, state, session_name, cfg, on_execution_report, on_market_data,
                                  on_security_definition, on_market_data_reject)
        native.start()
        return native, native
    settings, session_id = build_quickfix_settings(cfg, session_name)
    application = TTFixApplication(svc, state, session_name, cfg["password"],
                                    on_execution_report, on_market_data, on_security_definition,
                                    on_market_data_reject)
    store_factory = fix.FileStoreFactory(settings)
    log_factory = fix.FileLogFactory(settings)
    initiator = fix.SocketInitiator(application, store_factory, settings, log_factory)
    state.status = "CONNECTING"
    state.sender_comp_id = cfg["sender_comp_id"]
    state.target_comp_id = cfg["target_comp_id"]
    initiator.start()
    return initiator, application


def stop_real_session(initiator):
    if initiator is not None:
        try:
            initiator.stop()
        except Exception:  # noqa: BLE001
            pass


SOH = "\x01"


def fix_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


def encode_fix_message(fields: list[tuple[str, str]]) -> bytes:
    """Encode a FIX 4.2 message with exact BodyLength and CheckSum values."""
    body = SOH.join(f"{tag}={value}" for tag, value in fields) + SOH
    prefix = f"8=FIX.4.2{SOH}".encode("ascii")
    middle = f"9={len(body.encode('ascii'))}{SOH}".encode("ascii") + body.encode("ascii")
    return prefix + middle + f"10={sum(prefix + middle) % 256:03d}{SOH}".encode("ascii")


def parse_fix_message(raw: str) -> dict[str, str]:
    return {tag: value for item in raw.split(SOH) if "=" in item
            for tag, value in [item.split("=", 1)]}


def parse_fix_pairs(raw: str) -> list[tuple[str, str]]:
    """Return ordered FIX fields; order is required for repeating groups."""
    return [tuple(item.split("=", 1)) for item in raw.split(SOH) if "=" in item]


class NativeFixSession:
    """Small threaded FIX 4.2 initiator for TT UAT; no native extension needed."""
    native_fix = True

    def __init__(self, svc, state, session_name, cfg, on_execution_report=None, on_market_data=None,
                 on_security_definition=None, on_market_data_reject=None):
        self.svc, self.state, self.session_name, self.cfg = svc, state, session_name, cfg
        self.on_execution_report, self.on_market_data = on_execution_report, on_market_data
        self.on_security_definition = on_security_definition
        self.on_market_data_reject = on_market_data_reject
        self.socket = None
        self.stop_event = threading.Event()
        self.send_lock = threading.Lock()
        self.last_send_monotonic = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        with self.state.lock:
            self.state.status = "CONNECTING"
            self.state.error = ""
        self.thread.start()

    def is_running(self) -> bool:
        return self.thread.is_alive() and not self.stop_event.is_set()

    def stop(self):
        self.stop_event.set()
        try:
            if self.socket:
                self.send("5", [("58", "Client disconnect")])
                self.socket.close()
        except (OSError, ConnectionError):
            pass

    def send(self, msg_type: str, fields: list[tuple[str, str]]):
        with self.send_lock:
            with self.state.lock:
                self.state.out_seq += 1
                seq = self.state.out_seq
                self.state.outgoing_count += 1
                self.state.last_message = datetime.now(timezone.utc).isoformat()
            header = [("35", msg_type), ("34", str(seq)), ("49", self.cfg["sender_comp_id"]),
                      ("52", fix_timestamp()), ("56", self.cfg["target_comp_id"])]
            if self.cfg.get("sender_sub_id"):
                header.append(("50", self.cfg["sender_sub_id"]))
            if self.cfg.get("on_behalf_of_sub_id"):
                header.append(("116", self.cfg["on_behalf_of_sub_id"]))
            payload = encode_fix_message(header + fields)
            if not self.socket:
                raise ConnectionError("FIX socket is not connected")
            self.socket.sendall(payload)
            self.last_send_monotonic = time.monotonic()
            self.svc.log_fix(self.session_name, "OUT", msg_type, str(seq), payload.decode("ascii"))

    def _run(self):
        try:
            self.socket = socket.create_connection((self.cfg["host"], int(self.cfg["port"])), timeout=15)
            self.socket.settimeout(1)
            with self.state.lock:
                self.state.sender_comp_id = self.cfg["sender_comp_id"]
                self.state.target_comp_id = self.cfg["target_comp_id"]
                self.state.out_seq = 0
                self.state.in_seq = 0
            password = self.cfg["password"]
            self.send("A", [("98", "0"), ("108", str(self.cfg.get("heartbeat", 30))),
                            ("95", str(len(password.encode("utf-8")))), ("96", password), ("141", "Y")])
            buffer = ""
            heartbeat_seconds = max(5, int(self.cfg.get("heartbeat", 30)))
            logon_deadline = time.monotonic() + 20
            while not self.stop_event.is_set():
                try:
                    data = self.socket.recv(8192)
                    if not data:
                        raise ConnectionError("TT closed the FIX socket")
                    buffer += data.decode("ascii", errors="replace")
                    while f"{SOH}10=" in buffer:
                        end = buffer.index(SOH, buffer.index(f"{SOH}10=") + 4) + 1
                        raw, buffer = buffer[:end], buffer[end:]
                        self._incoming(raw)
                except socket.timeout:
                    pass

                # FIX requires the initiator to send heartbeats when it has
                # been idle for the negotiated interval.  Without this, TT
                # will close an otherwise valid session after logon.
                if time.monotonic() - self.last_send_monotonic >= heartbeat_seconds:
                    self.send("0", [])

                if self.state.status == "CONNECTING" and time.monotonic() >= logon_deadline:
                    raise TimeoutError("TT did not answer the Logon request within 20 seconds")
        except Exception as exc:  # show the actual server/network reason in UI
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = str(exc)
        finally:
            try:
                if self.socket:
                    self.socket.close()
            except OSError:
                pass
            if self.state.status != "ERROR":
                self.state.status = "DISCONNECTED"

    def _incoming(self, raw: str):
        fields = parse_fix_message(raw)
        msg_type, seq = fields.get("35", "?"), fields.get("34", "?")
        with self.state.lock:
            self.state.incoming_count += 1
            self.state.in_seq = int(seq) if seq.isdigit() else self.state.in_seq
            self.state.last_message = datetime.now(timezone.utc).isoformat()
        self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
        if msg_type == "A":
            with self.state.lock:
                self.state.status, self.state.error = "CONNECTED", ""
                self.state.last_logon = datetime.now(timezone.utc).isoformat()
        elif msg_type == "5":
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = fields.get("58", "TT rejected or closed the logon")
            self.stop_event.set()
        elif msg_type == "0":
            self.state.last_heartbeat = datetime.now(timezone.utc).isoformat()
        elif msg_type == "1":
            self.send("0", [("112", fields.get("112", ""))])
        elif msg_type == "8" and self.on_execution_report:
            self.on_execution_report(fields, raw)
        elif msg_type in ("W", "X") and self.on_market_data:
            self.on_market_data(fields, raw)
        elif msg_type == "Y" and self.on_market_data_reject:
            self.on_market_data_reject(fields, raw)
        elif msg_type == "d" and self.on_security_definition:
            self.on_security_definition(fields, raw)


def send_market_data_request(app, subscription: dict, request_type: str):
    """Send a TT-compliant 35=V request with one NoRelatedSym instrument group."""
    if getattr(app, "native_fix", False):
        # TT requires NoRelatedSym (146). The instrument must be identified
        # either by TT Security ID (48/22=96) or exact product/type/month.
        fields = [("146", "1"), ("55", subscription["symbol"])]
        if subscription.get("security_id"):
            fields.extend([("48", subscription["security_id"]), ("22", "96")])
        if subscription.get("security_type"):
            fields.append(("167", subscription["security_type"]))
        if subscription.get("maturity_month_year"):
            fields.append(("200", subscription["maturity_month_year"]))
        if subscription.get("exchange"):
            fields.append(("207", subscription["exchange"]))
        fields.extend([("262", subscription["request_id"]), ("263", request_type)])
        if request_type != "2":
            fields.extend([
                ("264", "0" if subscription["md_type"] == "FULL_BOOK" else "1"),
                ("265", "1" if subscription["sub_type"] == "SNAPSHOT_PLUS_UPDATES" else "0"),
                ("266", "Y"), ("267", "3"), ("269", "0"), ("269", "1"), ("269", "2"),
            ])
        app.send("V", fields)
        return
    msg = fix.Message()
    msg.getHeader().setField(fix.MsgType("V"))
    msg.setField(fix.MDReqID(subscription["request_id"]))
    msg.setField(fix.SubscriptionRequestType(request_type))  # 0 snapshot, 1 subscribe, 2 unsubscribe
    instrument = fix.Group(int(Tag.NoRelatedSym), int(Tag.Symbol))
    instrument.setField(fix.Symbol(subscription["symbol"]))
    if subscription.get("security_id"):
        instrument.setField(fix.SecurityID(subscription["security_id"]))
        instrument.setField(fix.StringField(int(Tag.SecurityIDSource), "96"))
    if subscription.get("security_type"):
        instrument.setField(fix.StringField(int(Tag.SecurityType), subscription["security_type"]))
    if subscription.get("maturity_month_year"):
        instrument.setField(fix.StringField(int(Tag.MaturityMonthYear), subscription["maturity_month_year"]))
    if subscription.get("exchange"):
        instrument.setField(fix.StringField(int(Tag.SecurityExchange), subscription["exchange"]))
    msg.addGroup(instrument)
    if request_type != "2":
        msg.setField(fix.MarketDepth(0 if subscription["md_type"] == "FULL_BOOK" else 1))
        msg.setField(fix.StringField(int(Tag.MDUpdateType), "1" if subscription["sub_type"] == "SNAPSHOT_PLUS_UPDATES" else "0"))
        msg.setField(fix.StringField(int(Tag.AggregatedBook), "Y"))
        for entry_type in ("0", "1", "2"):
            group = fix.Group(int(Tag.NoMDEntryTypes), int(Tag.MDEntryType))
            group.setField(fix.MDEntryType(entry_type))
            msg.addGroup(group)
    fix.Session.sendToTarget(msg, app.session_id)


def send_security_definition_request(app, request: dict):
    """Request TT instrument definitions using 35=c on the Market Data session.

    TT keeps this request active.  Keep the filter narrow (exchange, type,
    and preferably product) so the client is not flooded with an entire
    exchange catalogue.
    """
    fields = [(Tag.SecurityReqID, request["request_id"]),
              (Tag.SecurityRequestType, "3"),
              (Tag.SecurityType, request["security_type"]),
              (Tag.SecurityExchange, request["exchange"])]
    if request.get("symbol"):
        fields.append((Tag.Symbol, request["symbol"]))
    if request.get("maturity_month_year"):
        fields.append((Tag.MaturityMonthYear, request["maturity_month_year"]))
    if getattr(app, "native_fix", False):
        app.send("c", fields)
        return
    msg = fix.Message()
    msg.getHeader().setField(fix.MsgType("c"))
    for tag, value in fields:
        msg.setField(fix.StringField(int(tag), value))
    fix.Session.sendToTarget(msg, app.session_id)


def make_cancel_request(order: OrderRecord):
    msg = fix.Message()
    msg.getHeader().setField(fix.MsgType("F"))
    msg.setField(fix.OrigClOrdID(order.client_order_id))
    msg.setField(fix.ClOrdID(gen_client_order_id()))
    msg.setField(fix.Symbol(order.symbol))
    msg.setField(fix.Side(SIDES[order.side]))
    msg.setField(fix.OrderQty(order.remaining_qty or order.quantity))
    return msg


def make_replace_request(order: OrderRecord, quantity: float, price: Optional[float]):
    msg = fix.Message()
    msg.getHeader().setField(fix.MsgType("G"))
    msg.setField(fix.OrigClOrdID(order.client_order_id))
    msg.setField(fix.ClOrdID(gen_client_order_id()))
    msg.setField(fix.Symbol(order.symbol))
    msg.setField(fix.Side(SIDES[order.side]))
    msg.setField(fix.OrdType(ORD_TYPES[order.order_type]))
    msg.setField(fix.OrderQty(quantity))
    if price is not None:
        msg.setField(fix.Price(price))
    return msg


# ===========================================================================
# CONFIG
# ===========================================================================
def get_secret(section: str, key: str, default=""):
    try:
        return st.secrets[section][key]
    except Exception:
        return os.environ.get(f"{section.upper()}_{key.upper()}", default)


def load_config():
    return {
        "environment": get_secret("application", "environment", "UAT"),
        # Real TT UAT is the default. Mock data is available only when explicitly enabled.
        "mock_mode": str(get_secret("application", "mock_mode", "false")).lower() == "true",
        "enable_live_orders": str(get_secret("application", "enable_live_order_submission", "false")).lower() == "true",
        "order": {
            # TT's published UAT Internet endpoint. Use the Stunnel endpoint only
            # when a local Stunnel proxy has been configured.
            "host": get_secret("fix_order", "host", "fixorderrouting-ext-uat-cert.trade.tt"),
            "port": get_secret("fix_order", "port", 11502),
            "sender_comp_id": get_secret("fix_order", "sender_comp_id", "AJUATORDER"),
            "target_comp_id": get_secret("fix_order", "target_comp_id", "TT"),
            "sender_sub_id": get_secret("fix_order", "sender_sub_id"),
            "on_behalf_of_sub_id": get_secret("fix_order", "on_behalf_of_sub_id", "AJUAT"),
            "password": get_secret("fix_order", "password"),
            "account": get_secret("fix_order", "account", "AJ_account"),
        },
        "market_data": {
            "host": get_secret("fix_market_data", "host", "fixmarketdata-ext-uat-cert.trade.tt"),
            "port": get_secret("fix_market_data", "port", 11503),
            "sender_comp_id": get_secret("fix_market_data", "sender_comp_id", "AJUATMARKET"),
            "target_comp_id": get_secret("fix_market_data", "target_comp_id", "TT"),
            "sender_sub_id": get_secret("fix_market_data", "sender_sub_id"),
            "on_behalf_of_sub_id": get_secret("fix_market_data", "on_behalf_of_sub_id", "AJUAT"),
            "password": get_secret("fix_market_data", "password"),
        },
    }


def get_app_service() -> AppService:
    """Return the one service instance owned by this Streamlit browser session."""
    if "tt_fix_app_service" not in st.session_state:
        st.session_state["tt_fix_app_service"] = AppService()
    svc = st.session_state["tt_fix_app_service"]

    # Streamlit preserves Session State across source-code reruns. Upgrade an
    # already-connected service in place so a hot reload gains the tick tape
    # without opening a second TT session or discarding current subscriptions.
    if svc.__class__ is not AppService:
        svc.__class__ = AppService
    if not hasattr(svc, "market_data_lock"):
        svc.market_data_lock = threading.RLock()
    if not hasattr(svc, "market_data_ticks"):
        svc.market_data_ticks = deque(maxlen=5000)
    if not hasattr(svc, "market_data_tick_count"):
        svc.market_data_tick_count = 0

    if svc.md_app is not None:
        md_is_native = getattr(svc.md_app, "native_fix", False)
        svc.md_app.on_market_data = svc.apply_native_market_data if md_is_native else svc.apply_market_data
        svc.md_app.on_market_data_reject = (
            svc.apply_native_market_data_reject if md_is_native else svc.apply_market_data_reject
        )
        svc.md_app.on_security_definition = (
            svc.apply_native_security_definition if md_is_native else svc.apply_security_definition
        )
    if svc.or_app is not None:
        or_is_native = getattr(svc.or_app, "native_fix", False)
        svc.or_app.on_execution_report = (
            svc.apply_native_execution_report if or_is_native else svc.apply_execution_report
        )
    return svc


def validate_config(cfg: dict) -> list[str]:
    missing = []
    if not cfg["mock_mode"]:
        for label, section in (("Order Routing", cfg["order"]), ("Market Data", cfg["market_data"])):
            for field_name in ("host", "port", "target_comp_id", "password"):
                if not section.get(field_name):
                    missing.append(f"{label}: {field_name}")
    return missing


# ===========================================================================
# STREAMLIT UI
# ===========================================================================
st.set_page_config(page_title="TT FIX Trading Terminal", page_icon="◈", layout="wide", initial_sidebar_state="expanded")

st.markdown(
    """
    <style>
    :root { --tt-bg: #07111f; --tt-surface: #0c1a2b; --tt-surface-2: #10253b;
            --tt-border: #1d3853; --tt-text: #e7eef8; --tt-muted: #8ea3bc;
            --tt-accent: #28d7a1; --tt-blue: #3d83f6; --tt-danger: #ff5d73; }
    [data-testid="stAppViewContainer"] { background: radial-gradient(circle at 85% -10%, #163a59 0, transparent 28rem), var(--tt-bg); }
    [data-testid="stHeader"] { background: transparent; }
    [data-testid="stSidebar"] { background: #091523; border-right: 1px solid var(--tt-border); }
    [data-testid="stSidebar"] > div:first-child { padding-top: 1.25rem; }
    .block-container { max-width: 1500px; padding-top: 1.55rem; padding-bottom: 2.5rem; }
    h1, h2, h3 { color: var(--tt-text) !important; letter-spacing: -0.025em; }
    .terminal-brand { display:flex; align-items:center; justify-content:space-between; gap:1rem;
        padding: 1.1rem 1.25rem; margin-bottom: 1.25rem; border: 1px solid var(--tt-border);
        border-radius: 14px; background: linear-gradient(115deg, rgba(15,36,57,.98), rgba(9,21,35,.92)); }
    .terminal-brand-title { font-size:1.14rem; font-weight:750; letter-spacing:.045em; color:#f3f7fc; }
    .terminal-brand-subtitle { margin-top:.18rem; font-size:.78rem; color:var(--tt-muted); }
    .status-strip { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:.48rem; }
    .status-pill { border:1px solid var(--tt-border); border-radius:999px; padding:.38rem .65rem;
        color:#c9d7e7; font-size:.76rem; font-weight:650; background:#0b1929; }
    .status-pill .indicator { display:inline-block; width:.48rem; height:.48rem; border-radius:50%; margin-right:.35rem; }
    .connected .indicator { background:var(--tt-accent); box-shadow:0 0 0 .2rem rgba(40,215,161,.12); }
    .connecting .indicator { background:#f8c451; }
    .error .indicator, .disconnected .indicator { background:var(--tt-danger); }
    .env-pill { color:#91b7ff; }
    div[data-testid="stMetric"] { background:rgba(12,26,43,.86); border:1px solid var(--tt-border); border-radius:12px; padding:.75rem .9rem; }
    div[data-testid="stMetric"] label { color:var(--tt-muted) !important; font-size:.74rem !important; }
    div[data-testid="stDataFrame"] { border:1px solid var(--tt-border); border-radius:11px; overflow:hidden; }
    [data-testid="stForm"] { border:1px solid var(--tt-border); border-radius:12px; background:rgba(12,26,43,.72); }
    [data-testid="stButton"] > button { border-radius:8px; border-color:#34516e; font-weight:650; transition:all .15s ease; }
    [data-testid="stButton"] > button:hover { border-color:#5b9cff; color:#fff; transform:translateY(-1px); }
    [data-testid="stButton"] > button[kind="primary"] { background:linear-gradient(120deg, #2673ee, #42a1ff); border:0; color:white; }
    [data-testid="stPills"] [role="radiogroup"] { gap:.35rem; }
    [data-testid="stPills"] label { border-radius:8px !important; border:1px solid #23405d !important;
        background:#0b1a2a !important; color:#b9c9da !important; font-weight:600; }
    [data-testid="stPills"] label:has(input:checked) { background:#153b62 !important; border-color:#3e83e8 !important; color:#fff !important; }
    [data-testid="stAlert"] { border-radius:10px; }
    .sidebar-section { color:#7089a4; font-size:.68rem; letter-spacing:.11em; font-weight:750; margin:1rem 0 .42rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

cfg = load_config()
svc = get_app_service()

if cfg["mock_mode"]:
    ensure_mock_thread(svc)

# ---- top bar ---------------------------------------------------------
md_snap = svc.md_state.snapshot()
or_snap = svc.or_state.snapshot()

def dot(status):
    return "🟢" if status == "CONNECTED" else ("🟡" if status == "CONNECTING" else "🔴")


def is_session_conflict(error: str) -> bool:
    """True when TT rejected a second active logon for the same CompID."""
    return "session is already connected" in (error or "").lower()


def is_socket_access_denied(error: str) -> bool:
    """True when Windows or endpoint security prevents Python opening a socket."""
    return "winerror 10013" in (error or "").lower()


def socket_access_denied_guidance() -> str:
    return (
        "Windows/endpoint policy blocked Python before any FIX Logon was sent. "
        "Ask IT to allow this Python executable outbound TCP access to TT UAT ports "
        "11502/11503 (or secure ports 11702/11703)."
    )

env_label = cfg["environment"].upper()
def status_css(status: str) -> str:
    return status.lower() if status.lower() in {"connected", "connecting", "error", "disconnected"} else "disconnected"

st.markdown(
    f"""
    <div class="terminal-brand">
      <div><div class="terminal-brand-title">◈ TT FIX TERMINAL</div>
      <div class="terminal-brand-subtitle">Order routing · market data · UAT diagnostics</div></div>
      <div class="status-strip">
        <span class="status-pill {status_css(md_snap['status'])}"><span class="indicator"></span>Market Data · {md_snap['status']}</span>
        <span class="status-pill {status_css(or_snap['status'])}"><span class="indicator"></span>Order Routing · {or_snap['status']}</span>
        <span class="status-pill env-pill">{env_label}</span>
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if cfg["mock_mode"]:
    st.info("🧪 MOCK MODE — NO REAL ORDERS. All connectivity, market data, and fills are simulated.", icon="🧪")
elif env_label == "PRODUCTION":
    st.error("⚠ PRODUCTION TRADING ENABLED — REAL ORDERS MAY BE SENT", icon="⚠️")
    if not cfg["enable_live_orders"]:
        st.warning("Live order submission is currently disabled (ENABLE_LIVE_ORDER_SUBMISSION=false). "
                    "Connectivity and market data still work; orders will not transmit.")

missing = validate_config(cfg)
if missing and not cfg["mock_mode"]:
    st.error(
        "Real TT FIX configuration is incomplete. No simulated data is being used. Missing:\n\n"
        + "\n".join(f"- {m}" for m in missing)
    )

# TT permits one active connection for each FIX session/CompID. Connections
# are therefore explicit operator actions from the Connectivity page; this
# prevents Streamlit reloads from creating duplicate session logons.


@st.fragment(run_every="2s")
def live_connection_status():
    """Refresh async FIX-session state without restarting either session."""
    md_live = svc.md_state.snapshot()
    or_live = svc.or_state.snapshot()
    c1, c2 = st.columns(2)
    with c1:
        st.caption(f"Live Market Data: {dot(md_live['status'])} {md_live['status']} | "
                   f"IN/OUT {md_live['incoming_count']}/{md_live['outgoing_count']}")
        if md_live["error"]:
            st.error(f"TT Market Data: {md_live['error']}")
            if is_session_conflict(md_live["error"]):
                st.warning("TT already has this Market Data CompID logged on. Disconnect the other client "
                           "or have a TT administrator reset the UAT FIX session; do not retry yet.")
            elif is_socket_access_denied(md_live["error"]):
                st.warning(socket_access_denied_guidance())
    with c2:
        st.caption(f"Live Order Routing: {dot(or_live['status'])} {or_live['status']} | "
                   f"IN/OUT {or_live['incoming_count']}/{or_live['outgoing_count']}")
        if or_live["error"]:
            st.error(f"TT Order Routing: {or_live['error']}")
            if is_session_conflict(or_live["error"]):
                st.warning("TT already has this Order Routing CompID logged on. Disconnect the other client "
                           "or have a TT administrator reset the UAT FIX session; do not retry yet.")
            elif is_socket_access_denied(or_live["error"]):
                st.warning(socket_access_denied_guidance())


if not cfg["mock_mode"]:
    live_connection_status()


@st.fragment(run_every="2s")
def direct_order_result_panel(order_id: str):
    """Show the latest TT Execution Report state for one direct order."""
    order = svc.orders.get(order_id)
    if order is None:
        st.info("Order is being registered locally. Waiting for TT response…")
        return
    status = order.status
    if status == "REJECTED":
        st.error(f"TT result: REJECTED — {order.reject_reason or 'TT did not include a reason.'}")
    elif status in ("NEW", "REPLACED"):
        st.success(f"TT result: ACCEPTED ({status})")
    elif status == "PARTIALLY_FILLED":
        st.warning("TT result: PARTIALLY FILLED")
    elif status == "FILLED":
        st.success("TT result: FILLED")
    elif status in ("CANCELED", "EXPIRED"):
        st.warning(f"TT result: {status}")
    else:
        st.info("TT result: SENT — awaiting an Execution Report.")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Status", status)
    c2.metric("Filled", f"{order.filled_qty:g}")
    c3.metric("Remaining", f"{order.remaining_qty:g}")
    c4.metric("Average Price", f"{order.avg_px:g}" if order.avg_px else "—")
    c5.metric("TT Order ID", order.exchange_order_id or "—")
    st.caption(f"Client Order ID: {order.client_order_id} · Updated: {order.updated_at}")
    if order.raw_last_report:
        with st.expander("Latest TT Execution Report"):
            st.code(mask_raw_message(order.raw_last_report).replace(SOH, "|"), language="text")


@st.fragment(run_every=MARKET_DATA_REFRESH_INTERVAL)
def live_market_data_panel():
    """Refresh market data and expose TT request-rejection details."""
    records, subscriptions, rejections, ticks, total_ticks = svc.market_data_snapshot()
    if not records:
        st.caption("No subscriptions yet.")
        return
    status_col, refresh_col = st.columns([5, 1], vertical_alignment="center")
    status_col.caption(f"Live feed · 100 ms display refresh · {total_ticks:,} tick entries received")
    if refresh_col.button(
        "Refresh table", icon=":material/refresh:", key="refresh_market_data_tables",
        width="stretch", help="Fetch the newest in-memory FIX snapshot now.",
    ):
        st.rerun(scope="fragment")
    st.dataframe(records, width="stretch", hide_index=True, key="live_market_quotes")
    for symbol, subscription in subscriptions:
        rejection = rejections.get(subscription["request_id"])
        if rejection:
            st.error(f"TT rejected market-data subscription for {symbol}: {rejection}")
    st.markdown("#### Real-time tick tape")
    if ticks:
        st.dataframe(ticks, width="stretch", height=360, hide_index=True, key="live_market_ticks")
        st.caption("Newest tick first. Every received bid, ask, trade, change, and delete is retained; "
                   "the table displays the latest 250 entries and memory retains the latest 5,000.")
    else:
        st.caption("Waiting for the first market-data tick from TT.")
    st.caption("A blank quote without a TT rejection usually means the instrument has no current quote, "
               "or the session lacks market-data entitlement.")

# ---- sidebar -----------------------------------------------------------
def select_primary_navigation():
    st.session_state["terminal_secondary_page"] = None


def select_secondary_navigation():
    st.session_state["terminal_page"] = None


st.sidebar.markdown("<div class='terminal-brand-title'>TT FIX</div><div class='terminal-brand-subtitle'>Trading workspace</div>", unsafe_allow_html=True)
st.sidebar.markdown("<div class='sidebar-section'>WORKSPACE</div>", unsafe_allow_html=True)
page = st.sidebar.pills(
    "Workspace navigation",
    ["Dashboard", "FIX Connectivity", "Instrument Lookup", "Market Data", "Order Entry", "Open Orders", "Executions"],
    default=st.session_state.get("terminal_page", "Dashboard"),
    key="terminal_page",
    on_change=select_primary_navigation,
    label_visibility="collapsed",
    width="stretch",
)
st.sidebar.markdown("<div class='sidebar-section'>CONFIGURATION</div>", unsafe_allow_html=True)
secondary_page = st.sidebar.pills(
    "Configuration navigation",
    ["Symbol Bridge", "FIX Message Log", "Settings", "Certification / Diagnostics"],
    key="terminal_secondary_page",
    on_change=select_secondary_navigation,
    label_visibility="collapsed",
    width="stretch",
)
if secondary_page:
    page = secondary_page
if not page:
    page = "Dashboard"

st.sidebar.divider()
mode_label = "MOCK" if cfg["mock_mode"] else ("REAL · Native FIX" if not QUICKFIX_AVAILABLE else "REAL · QuickFIX")
st.sidebar.caption(f"Mode: {mode_label}")
if not QUICKFIX_AVAILABLE and not cfg["mock_mode"]:
    st.sidebar.info("Using the built-in Python FIX 4.2 client — no QuickFIX installation is required.")

# ===========================================================================
# PAGE: Dashboard
# ===========================================================================
if page == "Dashboard":
    orders = list(svc.orders.values())
    open_orders = [o for o in orders if o.status in ("PENDING", "NEW", "PARTIALLY_FILLED")]
    filled_qty_today = sum(o.filled_qty for o in orders)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open Orders", len(open_orders))
    c2.metric("Orders Today", len(orders))
    c3.metric("Filled Quantity", f"{filled_qty_today:g}")
    c4.metric("FIX Messages", len(svc.fix_log_all))

    st.subheader("Market Data")
    if svc.market_data:
        st.dataframe([asdict(v) for v in svc.market_data.values()], use_container_width=True)
    else:
        st.caption("No active market data subscriptions. Add one on the Market Data page.")

    st.subheader("Recent FIX Messages")
    for entry in svc.fix_log_all[-8:][::-1]:
        st.text(f"[{entry.ts}] {entry.session} {entry.direction} {entry.msg_type} seq={entry.seq_num}")

# ===========================================================================
# PAGE: FIX Connectivity
# ===========================================================================
elif page == "FIX Connectivity":
    for label, state_obj, snap, section_cfg in (
        ("Market Data FIX", svc.md_state, md_snap, cfg["market_data"]),
        ("Order Routing FIX", svc.or_state, or_snap, cfg["order"]),
    ):
        st.markdown(f"## {label}")
        cols = st.columns(4)
        cols[0].write(f"**Status:** {dot(snap['status'])} {snap['status']}")
        cols[1].write(f"**SenderCompID:** {snap['sender_comp_id'] or section_cfg['sender_comp_id']}")
        cols[2].write(f"**TargetCompID:** {snap['target_comp_id'] or section_cfg.get('target_comp_id') or '(not set)'}")
        cols[3].write(f"**Last Heartbeat:** {snap['last_heartbeat'] or '—'}")
        cols2 = st.columns(4)
        cols2[0].write(f"**Last FIX Message:** {snap['last_message'] or '—'}")
        cols2[1].write(f"**Incoming:** {snap['incoming_count']}")
        cols2[2].write(f"**Outgoing:** {snap['outgoing_count']}")
        cols2[3].write(f"**In/Out Seq:** {snap['in_seq']}/{snap['out_seq']}")
        if snap["error"]:
            st.error(snap["error"])
            if is_socket_access_denied(snap["error"]):
                st.warning(socket_access_denied_guidance())

        b1, b2, b3 = st.columns(3)
        key_prefix = label.replace(" ", "_")
        if cfg["mock_mode"]:
            b1.button("Connect", key=f"conn_{key_prefix}", disabled=True, help="Mock mode auto-connects.")
            b2.button("Disconnect", key=f"disc_{key_prefix}", disabled=True)
            b3.button("Reconnect", key=f"recon_{key_prefix}", disabled=True)
        else:
            is_md = label.startswith("Market")
            initiator_attr = "md_initiator" if is_md else "or_initiator"
            app_attr = "md_app" if is_md else "or_app"
            blocked = bool(missing) or not REAL_FIX_AVAILABLE
            if b1.button("Connect", key=f"conn_{key_prefix}", disabled=blocked,
                         help="Install quickfix and provide TT's host, port, TargetCompID, and password." if blocked else None):
                existing = getattr(svc, initiator_attr)
                if existing is not None and not (getattr(existing, "native_fix", False) and not existing.is_running()):
                    st.warning(f"{label} is already started.")
                else:
                    if existing is not None:
                        stop_real_session(existing)
                    initiator, app_obj = start_real_session(
                        svc, state_obj, state_obj.name, section_cfg,
                        on_execution_report=None if is_md else (svc.apply_execution_report if QUICKFIX_AVAILABLE else svc.apply_native_execution_report),
                        on_market_data=(svc.apply_market_data if QUICKFIX_AVAILABLE else svc.apply_native_market_data) if is_md else None,
                        on_security_definition=(svc.apply_security_definition if QUICKFIX_AVAILABLE else svc.apply_native_security_definition) if is_md else None,
                        on_market_data_reject=(svc.apply_market_data_reject if QUICKFIX_AVAILABLE else svc.apply_native_market_data_reject) if is_md else None,
                    )
                    setattr(svc, initiator_attr, initiator)
                    setattr(svc, app_attr, app_obj)
                st.rerun()
            if b2.button("Disconnect", key=f"disc_{key_prefix}"):
                stop_real_session(getattr(svc, initiator_attr))
                setattr(svc, initiator_attr, None)
                setattr(svc, app_attr, None)
                with state_obj.lock:
                    state_obj.status = "DISCONNECTED"
                    state_obj.error = ""
                st.rerun()
            if b3.button("Reconnect", key=f"recon_{key_prefix}"):
                stop_real_session(getattr(svc, initiator_attr))
                setattr(svc, initiator_attr, None)
                setattr(svc, app_attr, None)
                with state_obj.lock:
                    state_obj.status = "DISCONNECTED"
                    state_obj.error = ""
                st.info("Disconnected locally. Wait 10 seconds, then press Connect once. This avoids a duplicate TT session logon.")
                st.rerun()
        st.divider()

# ===========================================================================
# ===========================================================================
# PAGE: Instrument Lookup
# ===========================================================================
elif page == "Instrument Lookup":
    st.subheader("TT Instrument Lookup")
    st.caption("Search TT's instrument catalogue first, then send the selected exact Security ID to Order Entry. "
               "This prevents orders being sent using an ambiguous product symbol such as HO or BZ.")
    st.warning("A request remains an active TT subscription. Search one exchange/type/product at a time; do not request "
               "the entire catalogue unless TT has approved that volume for this UAT session.")

    with st.form("security_definition_lookup"):
        col1, col2, col3 = st.columns(3)
        exchange = col1.text_input("Exchange (tag 207)", value="CME")
        security_type = col2.selectbox("Security Type (tag 167)", ["FUT", "OPT", "MLEG", "CS", "CUR", "FOR", "SPOT"])
        symbol = col3.text_input("Product Symbol (tag 55)", placeholder="BZ, HO, ES — leave empty only for a broad exchange search")
        maturity_month_year = st.text_input("Contract Month YYYYMM (optional, tag 200)", placeholder="202610 for Oct 2026")
        broad_search = st.checkbox("I understand an empty Product Symbol can return a very large result set", value=False)
        lookup = st.form_submit_button("Fetch Matching Instruments", type="primary")

    if lookup:
        if not exchange.strip() or not security_type:
            st.error("Exchange and Security Type are required.")
        elif not symbol.strip() and not broad_search:
            st.error("Enter a Product Symbol, or explicitly acknowledge the broad catalogue search.")
        elif md_snap["status"] != "CONNECTED" or svc.md_app is None:
            st.error("Market Data FIX must be CONNECTED before requesting instrument definitions.")
        else:
            request_id = f"SECDEF-{uuid.uuid4().hex[:16].upper()}"
            svc.instrument_definitions.clear()
            svc.last_security_definition_request = request_id
            try:
                send_security_definition_request(svc.md_app, {
                    "request_id": request_id,
                    "exchange": exchange.strip(),
                    "security_type": security_type,
                    "symbol": symbol.strip(),
                    "maturity_month_year": maturity_month_year.strip(),
                })
                st.success("Security Definition request sent. Wait a few seconds, then press Refresh results.")
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not request instruments: {exc}")

    definitions = list(svc.instrument_definitions.values())
    if definitions:
        definitions.sort(key=lambda item: (item.symbol, item.maturity_month_year, item.security_id))
        st.caption(f"Received {len(definitions)} matching TT instrument definitions.")
        st.dataframe([asdict(item) for item in definitions], use_container_width=True, hide_index=True)
        labels = {
            f"{item.symbol} | {item.exchange} | {item.security_type} | {item.maturity_month_year or 'no month'} | {item.security_id}": item
            for item in definitions
        }
        selected = labels[st.selectbox("Instrument to use", list(labels))]
        if st.button("Use Selected Instrument in Order Entry", type="primary"):
            st.session_state["order_instrument"] = asdict(selected)
            st.success("Instrument selected. Open Order Entry; the TT Security ID and contract fields will be prefilled. "
                       "You must still review and explicitly confirm any order.")
        if st.button("Use Selected Instrument in Market Data"):
            st.session_state["market_data_instrument"] = asdict(selected)
            st.success("Instrument selected. Open Market Data; its exact TT Security ID will be prefilled.")

        st.divider()
        st.markdown("#### Trade selected TT instrument")
        st.caption(f"Selected: **{selected.symbol}** · {selected.exchange} · {selected.security_type} · "
                   f"{selected.maturity_month_year or 'contract month not supplied'} · Security ID `{selected.security_id}`")
        with st.form("direct_lookup_order"):
            col1, col2, col3, col4 = st.columns(4)
            direct_account = col1.text_input("Account", value=cfg["order"]["account"])
            direct_side = col2.selectbox("Side", list(SIDES.keys()), key="direct_side")
            direct_order_type = col3.selectbox("Order Type", list(ORD_TYPES.keys()), index=1, key="direct_order_type")
            direct_qty = col4.number_input("Quantity", min_value=0.0, step=1.0, value=1.0, key="direct_qty")
            col5, col6 = st.columns(2)
            direct_price = col5.number_input("Limit / Stop Price", min_value=0.0, step=0.01, value=0.0,
                                               help="Required only for LIMIT and STOP_LIMIT orders.", key="direct_price")
            direct_tif = col6.selectbox("Time In Force", list(TIFS.keys()), key="direct_tif")
            direct_review = st.form_submit_button("Review Direct Order", type="primary")
        if direct_review:
            st.session_state["pending_direct_lookup_order"] = {
                "account": direct_account, "side": direct_side, "order_type": direct_order_type,
                "quantity": direct_qty, "price": direct_price, "tif": direct_tif,
                "symbol": selected.symbol, "security_id": selected.security_id,
                "exchange": selected.exchange, "security_type": selected.security_type,
                "maturity_month_year": selected.maturity_month_year,
                "client_order_id": gen_client_order_id(),
            }

        direct_pending = st.session_state.get("pending_direct_lookup_order")
        if direct_pending:
            direct_errors = []
            if svc.or_state.snapshot()["status"] != "CONNECTED" and not cfg["mock_mode"]:
                direct_errors.append("Order Routing session is not connected.")
            if direct_pending["quantity"] <= 0:
                direct_errors.append("Quantity must be greater than zero.")
            if direct_pending["order_type"] in ("LIMIT", "STOP_LIMIT") and direct_pending["price"] <= 0:
                direct_errors.append("Price is required for LIMIT/STOP_LIMIT orders.")
            if not direct_pending["account"]:
                direct_errors.append("Account is required.")
            if not direct_pending["security_id"]:
                direct_errors.append("The selected TT instrument has no Security ID and cannot be submitted directly.")
            if not cfg["mock_mode"] and not cfg["enable_live_orders"]:
                direct_errors.append("Live order submission is disabled (ENABLE_LIVE_ORDER_SUBMISSION=false).")
            if direct_errors:
                for error in direct_errors:
                    st.error(error)
            else:
                st.warning(
                    f"Direct order review: **{direct_pending['side']}** {direct_pending['quantity']:g} "
                    f"**{direct_pending['symbol']}** {direct_pending['order_type']}"
                    + (f" @ {direct_pending['price']}" if direct_pending['order_type'] in ("LIMIT", "STOP_LIMIT") else "")
                    + f"\n\nAccount: {direct_pending['account']} · Security ID: `{direct_pending['security_id']}`"
                )
                if st.button("Confirm — Submit Direct Order", type="primary"):
                    order = OrderRecord(
                        client_order_id=direct_pending["client_order_id"], account=direct_pending["account"],
                        symbol=direct_pending["symbol"], side=direct_pending["side"],
                        order_type=direct_pending["order_type"], quantity=direct_pending["quantity"],
                        price=direct_pending["price"] or None, tif=direct_pending["tif"], status="PENDING",
                    )
                    order.remaining_qty = order.quantity
                    # Register before sending so a very fast Execution Report
                    # can always be correlated to this client order ID.
                    svc.upsert_order(order)
                    st.session_state["last_direct_order_id"] = order.client_order_id
                    try:
                        if cfg["mock_mode"]:
                            order.status = "NEW"
                            order.exchange_order_id = f"MOCK-{uuid.uuid4().hex[:8]}"
                            svc.upsert_order(order)
                        elif svc.or_app is not None and getattr(svc.or_app, "native_fix", False):
                            fields = [("11", order.client_order_id), ("1", order.account),
                                      ("55", order.symbol), ("48", direct_pending["security_id"]), ("22", "96"),
                                      ("207", direct_pending["exchange"]), ("167", direct_pending["security_type"]),
                                      ("54", SIDES[order.side]), ("38", str(order.quantity)),
                                      ("40", ORD_TYPES[order.order_type]), ("59", TIFS[order.tif]),
                                      ("60", fix_timestamp())]
                            if direct_pending["maturity_month_year"]:
                                fields.append(("200", direct_pending["maturity_month_year"]))
                            if order.price:
                                fields.append(("44", str(order.price)))
                            svc.or_app.send("D", fields)
                            svc.upsert_order(order)
                        elif svc.or_app is not None:
                            msg = fix.Message()
                            msg.getHeader().setField(fix.MsgType("D"))
                            for tag, value in (("11", order.client_order_id), ("1", order.account), ("55", order.symbol),
                                               ("48", direct_pending["security_id"]), ("22", "96"), ("207", direct_pending["exchange"]),
                                               ("167", direct_pending["security_type"]), ("54", SIDES[order.side]),
                                               ("40", ORD_TYPES[order.order_type]), ("38", str(order.quantity)),
                                               ("59", TIFS[order.tif])):
                                msg.setField(fix.StringField(int(tag), value))
                            if direct_pending["maturity_month_year"]:
                                msg.setField(fix.StringField(200, direct_pending["maturity_month_year"]))
                            if order.price:
                                msg.setField(fix.Price(order.price))
                            fix.Session.sendToTarget(msg, svc.or_app.session_id)
                            svc.upsert_order(order)
                        else:
                            raise ConnectionError("Order Routing session is unavailable")
                        st.success("Direct FIX order transmitted. Awaiting Execution Report.")
                    except Exception as exc:  # noqa: BLE001
                        order.status = "REJECTED"
                        order.reject_reason = str(exc)
                        svc.upsert_order(order)
                        st.error(f"Failed to transmit direct order: {exc}")
                    finally:
                        st.session_state.pop("pending_direct_lookup_order", None)
        last_direct_order_id = st.session_state.get("last_direct_order_id")
        if not last_direct_order_id:
            matching_orders = [order for order in svc.orders.values() if order.symbol == selected.symbol]
            if matching_orders:
                last_direct_order_id = max(matching_orders, key=lambda order: order.updated_at).client_order_id
        if last_direct_order_id:
            st.divider()
            st.markdown("#### Latest order result for selected instrument")
            direct_order_result_panel(last_direct_order_id)
    else:
        st.info("No definitions received yet. Submit a narrow lookup, wait for TT responses, then refresh this page.")
    if st.button("Refresh results"):
        st.rerun()

# ===========================================================================
# PAGE: Symbol Bridge
# ===========================================================================
elif page == "Symbol Bridge":
    st.subheader("Symbol Bridge")
    st.caption("Map the symbol received from a maker/feed to the TT symbol and security identifiers "
               "used for market-data subscriptions and orders. This does not create a connection to "
               "the maker; it is the local translation layer.")

    mappings = sorted(svc.symbol_mappings.values(), key=lambda mapping: mapping.bridge_symbol)
    if mappings:
        st.dataframe([
            {
                "Bridge Symbol": mapping.bridge_symbol,
                "Maker": mapping.maker,
                "Maker Symbol": mapping.maker_symbol,
                "TT Symbol": mapping.tt_symbol,
                "TT Exchange": mapping.tt_exchange,
                "TT Security ID": mapping.tt_security_id,
                "Security Type": mapping.security_type,
                "Digits": mapping.price_digits,
                "Enabled": mapping.enabled,
                "Updated (UTC)": mapping.updated_at,
            }
            for mapping in mappings
        ], use_container_width=True, hide_index=True)
    else:
        st.info("No symbol mappings yet. Add the instruments you want to expose in Market Watch.")

    st.markdown("#### Add or update mapping")
    with st.form("symbol_bridge_mapping", clear_on_submit=True):
        col1, col2, col3 = st.columns(3)
        bridge_symbol = col1.text_input("Bridge Symbol", placeholder="XAUUSD")
        maker = col2.text_input("Feed label (optional)", value="")
        maker_symbol = col3.text_input("Maker Symbol", placeholder="XAUUSD.lp")
        col4, col5, col6 = st.columns(3)
        tt_symbol = col4.text_input("TT Symbol", placeholder="TT instrument symbol")
        tt_exchange = col5.text_input("TT Exchange", placeholder="Exchange MIC / TT value")
        tt_security_id = col6.text_input("TT Security ID", placeholder="Preferred when provisioned")
        col7, col8, col9 = st.columns(3)
        security_type = col7.text_input("Security Type", placeholder="FX / FUT / CFD")
        price_digits = col8.number_input("Price Digits", min_value=0, max_value=12, value=5, step=1)
        enabled = col9.checkbox("Enable mapping", value=True)
        save_mapping = st.form_submit_button("Save Mapping", type="primary")
        if save_mapping:
            if not bridge_symbol.strip() or not maker.strip() or not maker_symbol.strip() or not tt_symbol.strip():
                st.error("Bridge Symbol, Maker, Maker Symbol, and TT Symbol are required.")
            else:
                svc.upsert_symbol_mapping(SymbolMapping(
                    bridge_symbol=bridge_symbol.strip().upper(), maker=maker.strip(),
                    maker_symbol=maker_symbol.strip(), tt_symbol=tt_symbol.strip(),
                    tt_exchange=tt_exchange.strip(), tt_security_id=tt_security_id.strip(),
                    security_type=security_type.strip(), price_digits=int(price_digits), enabled=enabled,
                ))
                st.success(f"Saved bridge mapping for {bridge_symbol.strip().upper()}.")
                st.rerun()

    if mappings:
        st.markdown("#### Remove mapping")
        remove_symbol = st.selectbox("Bridge Symbol to remove", [mapping.bridge_symbol for mapping in mappings])
        if st.button("Remove Mapping"):
            svc.delete_symbol_mapping(remove_symbol)
            st.rerun()

# ===========================================================================
# PAGE: Market Data
# ===========================================================================
elif page == "Market Data":
    st.subheader("Subscribe to Market Data")
    lookup_instrument = st.session_state.get("market_data_instrument", {})
    if lookup_instrument:
        st.info("Using an exact TT Security Definition result for this market-data request.")
    active_mappings = active_symbol_mappings(svc)
    selected_mapping = None
    if active_mappings:
        mapping_lookup = {mapping_label(mapping): mapping for mapping in active_mappings}
        selected_label = st.selectbox("Market Watch symbol", ["Manual TT instrument"] + list(mapping_lookup))
        selected_mapping = mapping_lookup.get(selected_label)
        if selected_mapping:
            st.caption(f"Bridge: {selected_mapping.bridge_symbol} ({selected_mapping.maker_symbol}) → "
                       f"TT {selected_mapping.tt_symbol}")
    with st.form("md_sub"):
        c1, c2, c3 = st.columns(3)
        exchange = c1.text_input("Exchange", value=lookup_instrument.get("exchange", selected_mapping.tt_exchange if selected_mapping else ""))
        symbol = c2.text_input("TT Symbol", value=lookup_instrument.get("symbol", selected_mapping.tt_symbol if selected_mapping else "EURUSD"))
        security_id = c3.text_input("TT Security ID (preferred)", value=lookup_instrument.get("security_id", selected_mapping.tt_security_id if selected_mapping else ""))
        c4, c5 = st.columns(2)
        security_type = c4.text_input("Security Type (tag 167)", value=lookup_instrument.get("security_type", selected_mapping.security_type if selected_mapping else ""), placeholder="FUT for futures")
        maturity_month_year = c5.text_input("Contract Month YYYYMM (tag 200)", value=lookup_instrument.get("maturity_month_year", ""), placeholder="202610 for Oct 2026")
        c6, c7 = st.columns(2)
        md_type = c6.selectbox("Market Data Type", ["FULL_BOOK", "TOP_OF_BOOK"])
        sub_type = c7.selectbox("Subscription Type", ["SNAPSHOT_PLUS_UPDATES", "SNAPSHOT"])
        submitted = st.form_submit_button("Subscribe")
        if submitted and symbol:
            # A re-subscription must not retain a stale quote from the
            # previous instrument/request for the same product symbol.
            subscription = {
                "request_id": f"MD-{uuid.uuid4().hex[:16].upper()}", "symbol": symbol,
                "exchange": exchange, "security_id": security_id, "md_type": md_type,
                "sub_type": sub_type,
                "security_type": security_type.strip(),
                "maturity_month_year": maturity_month_year.strip(),
                "bridge_symbol": selected_mapping.bridge_symbol if selected_mapping else "",
            }
            with svc.market_data_lock:
                svc.market_data[symbol] = MarketDataRecord(symbol=symbol)
                svc.market_subscriptions[symbol] = subscription
                svc.market_data_rejections.pop(subscription["request_id"], None)
            if (md_snap["status"] == "CONNECTED" and not cfg["mock_mode"]
                    and svc.md_app is not None):
                try:
                    send_market_data_request(svc.md_app, subscription, "0" if sub_type == "SNAPSHOT" else "1")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not send market-data request: {exc}")
            if md_snap["status"] != "CONNECTED":
                st.warning("Market Data session is not connected — subscription queued but will not receive data.")
            else:
                st.success(f"Subscribed to {symbol} ({md_type}, {sub_type}).")

    if svc.market_data:
        st.subheader("Unsubscribe")
        to_remove = st.selectbox("Symbol", list(svc.market_data.keys()), key="unsub_sym")
        if st.button("Unsubscribe"):
            if not cfg["mock_mode"] and svc.md_app is not None:
                try:
                    send_market_data_request(svc.md_app, svc.market_subscriptions[to_remove], "2")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not send unsubscribe: {exc}")
                    st.stop()
            with svc.market_data_lock:
                svc.market_data.pop(to_remove, None)
                svc.market_subscriptions.pop(to_remove, None)
            st.rerun()

    st.subheader("Live Market Data")
    live_market_data_panel()

# ===========================================================================
# PAGE: Order Entry
# ===========================================================================
elif page == "Order Entry":
    st.subheader("New Order")
    lookup_instrument = st.session_state.get("order_instrument", {})
    if lookup_instrument:
        st.info("Using a TT Security Definition result. Do not edit the TT Security ID unless you intentionally choose a different instrument.")
    active_mappings = active_symbol_mappings(svc)
    selected_mapping = None
    if active_mappings:
        order_mapping_lookup = {mapping_label(mapping): mapping for mapping in active_mappings}
        selected_order_label = st.selectbox(
            "Trade through symbol bridge", ["Manual TT instrument"] + list(order_mapping_lookup),
        )
        selected_mapping = order_mapping_lookup.get(selected_order_label)
        if selected_mapping:
            st.info(f"Order mapping: {selected_mapping.bridge_symbol} / {selected_mapping.maker_symbol} → "
                    f"TT {selected_mapping.tt_symbol}")
    with st.form("order_entry"):
        c1, c2, c3 = st.columns(3)
        account = c1.text_input("Account", value=cfg["order"]["account"])
        exchange = c2.text_input("TT Exchange", value=lookup_instrument.get("exchange", selected_mapping.tt_exchange if selected_mapping else ""))
        symbol = c3.text_input("TT Symbol", value=lookup_instrument.get("symbol", selected_mapping.tt_symbol if selected_mapping else "ES"))
        c4, c5, c6 = st.columns(3)
        security_id = c4.text_input("TT Security ID", value=lookup_instrument.get("security_id", selected_mapping.tt_security_id if selected_mapping else ""))
        security_type = c5.text_input("Security Type (tag 167)", value=lookup_instrument.get("security_type", selected_mapping.security_type if selected_mapping else ""))
        maturity_month_year = c6.text_input("Contract Month YYYYMM (tag 200)", value=lookup_instrument.get("maturity_month_year", ""))
        side = st.selectbox("Side", list(SIDES.keys()))
        c6, c7, c8 = st.columns(3)
        # MARKET is the safe form default: it does not need a price and
        # avoids presenting a validation error before the user has chosen a
        # priced order type.
        order_type = c6.selectbox("Order Type", list(ORD_TYPES.keys()), index=1)
        qty = c7.number_input("Quantity", min_value=0.0, step=1.0, value=1.0)
        price = c8.number_input("Limit / Stop Price", min_value=0.0, step=0.01, value=0.0,
                                help="Required only for LIMIT and STOP_LIMIT orders. Ignored for MARKET and STOP.")
        tif = st.selectbox("Time In Force", list(TIFS.keys()))
        client_order_id = st.text_input("Client Order ID", value=gen_client_order_id())

        review = st.form_submit_button("Review Order")

    if review:
        st.session_state["pending_order"] = dict(
            account=account, exchange=exchange, symbol=symbol, security_id=security_id,
            security_type=security_type, maturity_month_year=maturity_month_year,
            side=side, order_type=order_type, quantity=qty, price=price, tif=tif,
            client_order_id=client_order_id,
            bridge_symbol=selected_mapping.bridge_symbol if selected_mapping else "",
            maker_symbol=selected_mapping.maker_symbol if selected_mapping else "",
        )

    pending = st.session_state.get("pending_order")
    if pending:
        errors = []
        if or_snap["status"] != "CONNECTED" and not cfg["mock_mode"]:
            errors.append("Order Routing session is not connected.")
        if pending["quantity"] <= 0:
            errors.append("Quantity must be greater than zero.")
        if pending["order_type"] in ("LIMIT", "STOP_LIMIT") and pending["price"] <= 0:
            errors.append("Price is required for LIMIT/STOP_LIMIT orders.")
        if not pending["account"]:
            errors.append("Account is required.")
        if not pending["symbol"]:
            errors.append("Symbol is required.")
        if not pending["security_id"] and not (pending["exchange"] and pending["security_type"] and pending["maturity_month_year"]):
            errors.append("Select an instrument from Instrument Lookup, or provide TT Security ID; otherwise provide Exchange, Security Type, and Contract Month.")
        if pending["client_order_id"] in svc.orders:
            errors.append("Client Order ID must be unique.")
        if not cfg["mock_mode"] and not cfg["enable_live_orders"]:
            errors.append("Live order submission is disabled (ENABLE_LIVE_ORDER_SUBMISSION=false).")

        if errors:
            for e in errors:
                st.error(e)
        else:
            st.warning(
                f"You are about to submit:\n\n"
                f"**{pending['side']}** {pending['quantity']:g} **{pending['symbol']}** "
                f"{pending['order_type']}"
                + (f" @ {pending['price']}" if pending['order_type'] in ('LIMIT', 'STOP_LIMIT') else "")
                + f"\n\nAccount: {pending['account']}"
                + (f"\n\nBridge: {pending['bridge_symbol']} / {pending['maker_symbol']} → TT {pending['symbol']}"
                   if pending.get("bridge_symbol") else "")
            )
            if st.button("Confirm — Submit Order", type="primary"):
                order = OrderRecord(
                    client_order_id=pending["client_order_id"], account=pending["account"],
                    symbol=pending["symbol"], side=pending["side"], order_type=pending["order_type"],
                    quantity=pending["quantity"], price=pending["price"] or None, tif=pending["tif"],
                    status="PENDING",
                )
                order.remaining_qty = order.quantity
                st.info("Order request created.")
                # Store before socket transmission. TT can return an
                # Execution Report quickly enough that storing afterwards
                # would lose the correlation and hide the result in the UI.
                svc.upsert_order(order)

                if cfg["mock_mode"]:
                    order.status = "NEW"
                    order.exchange_order_id = f"MOCK-{uuid.uuid4().hex[:8]}"
                    svc.upsert_order(order)
                    raw = (f"8=FIX.4.2|35=D|11={order.client_order_id}|55={order.symbol}|"
                           f"54={SIDES[order.side]}|38={order.quantity}|40={ORD_TYPES[order.order_type]}|")
                    svc.log_fix("OrderRouting", "OUT", "D", str(svc.or_state.out_seq + 1), raw)
                    svc.or_state.out_seq += 1
                    st.success("FIX message transmitted (mock). Order acknowledged as NEW.")
                elif svc.or_app is not None:
                    try:
                        if getattr(svc.or_app, "native_fix", False):
                            fields = [("11", order.client_order_id), ("1", order.account),
                                      ("55", order.symbol), ("54", SIDES[order.side]),
                                      ("38", str(order.quantity)), ("40", ORD_TYPES[order.order_type]),
                                      ("59", TIFS[order.tif]), ("60", fix_timestamp())]
                            if pending["security_id"]:
                                fields.extend([("48", pending["security_id"]), ("22", "96")])
                            if pending["exchange"]:
                                fields.append(("207", pending["exchange"]))
                            if pending["security_type"]:
                                fields.append(("167", pending["security_type"]))
                            if pending["maturity_month_year"]:
                                fields.append(("200", pending["maturity_month_year"]))
                            if order.price:
                                fields.append(("44", str(order.price)))
                            svc.or_app.send("D", fields)
                        else:
                            session_id = svc.or_app.session_id
                            msg = fix.Message()
                            msg.getHeader().setField(fix.MsgType("D"))
                            msg.setField(fix.ClOrdID(order.client_order_id))
                            msg.setField(fix.Symbol(order.symbol))
                            msg.setField(fix.Side(SIDES[order.side]))
                            msg.setField(fix.OrdType(ORD_TYPES[order.order_type]))
                            msg.setField(fix.OrderQty(order.quantity))
                            if pending["security_id"]:
                                msg.setField(fix.SecurityID(pending["security_id"]))
                                msg.setField(fix.StringField(int(Tag.SecurityIDSource), "96"))
                            if pending["exchange"]:
                                msg.setField(fix.StringField(int(Tag.SecurityExchange), pending["exchange"]))
                            if pending["security_type"]:
                                msg.setField(fix.StringField(int(Tag.SecurityType), pending["security_type"]))
                            if pending["maturity_month_year"]:
                                msg.setField(fix.StringField(int(Tag.MaturityMonthYear), pending["maturity_month_year"]))
                            if order.price:
                                msg.setField(fix.Price(order.price))
                            msg.setField(fix.TimeInForce(TIFS[order.tif]))
                            msg.setField(fix.Account(order.account))
                            fix.Session.sendToTarget(msg, session_id)
                        svc.upsert_order(order)
                        st.success("FIX message transmitted. Awaiting Execution Report.")
                    except Exception as e:  # noqa: BLE001
                        order.status = "REJECTED"
                        order.reject_reason = str(e)
                        svc.upsert_order(order)
                        st.error(f"Failed to transmit order: {e}")
                else:
                    st.error("Order Routing session not connected — order was NOT sent.")
                    order.status = "REJECTED"
                    order.reject_reason = "Session not connected"
                    svc.upsert_order(order)
                del st.session_state["pending_order"]

# ===========================================================================
# PAGE: Open Orders
# ===========================================================================
elif page == "Open Orders":
    st.subheader("Open Orders")
    open_orders = [o for o in svc.orders.values() if o.status in ("PENDING", "NEW", "PARTIALLY_FILLED")]
    if open_orders:
        st.dataframe([asdict(o) for o in open_orders], use_container_width=True)
        sel = st.selectbox("Select order for Cancel/Replace", [o.client_order_id for o in open_orders])
        c1, c2 = st.columns(2)
        if c1.button("Cancel Order"):
            o = svc.orders[sel]
            if cfg["mock_mode"]:
                o.status = "CANCELED"
                o.updated_at = datetime.now(timezone.utc).isoformat()
                svc.upsert_order(o)
                svc.log_fix("OrderRouting", "OUT", "F", "n/a", f"8=FIX.4.2|35=F|41={sel}|")
                st.success(f"Cancel request sent for {sel} (mock).")
            elif svc.or_app is not None:
                try:
                    if getattr(svc.or_app, "native_fix", False):
                        svc.or_app.send("F", [("41", o.client_order_id), ("11", gen_client_order_id()),
                                              ("55", o.symbol), ("54", SIDES[o.side]),
                                              ("38", str(o.remaining_qty or o.quantity))])
                    else:
                        fix.Session.sendToTarget(make_cancel_request(o), svc.or_app.session_id)
                    st.success(f"Cancel request sent for {sel}; awaiting Execution Report.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Could not send cancel request: {exc}")
                    st.stop()
            else:
                st.error("Order Routing session is unavailable; cancel was not sent.")
                st.stop()
            st.rerun()
        with c2.popover("Replace Order"):
            new_qty = st.number_input("New Quantity", min_value=0.0, value=float(svc.orders[sel].quantity))
            new_price = st.number_input("New Price", min_value=0.0, value=float(svc.orders[sel].price or 0.0))
            if st.button("Submit Replace"):
                o = svc.orders[sel]
                if new_qty <= 0:
                    st.error("New quantity must be greater than zero.")
                    st.stop()
                if cfg["mock_mode"]:
                    o.quantity = new_qty
                    o.price = new_price or None
                    o.updated_at = datetime.now(timezone.utc).isoformat()
                    svc.upsert_order(o)
                    svc.log_fix("OrderRouting", "OUT", "G", "n/a", f"8=FIX.4.2|35=G|41={sel}|")
                    st.success("Replace request sent (mock).")
                elif svc.or_app is not None:
                    try:
                        if getattr(svc.or_app, "native_fix", False):
                            fields = [("41", o.client_order_id), ("11", gen_client_order_id()),
                                      ("55", o.symbol), ("54", SIDES[o.side]),
                                      ("38", str(new_qty)), ("40", ORD_TYPES[o.order_type])]
                            if new_price:
                                fields.append(("44", str(new_price)))
                            svc.or_app.send("G", fields)
                        else:
                            fix.Session.sendToTarget(make_replace_request(o, new_qty, new_price or None), svc.or_app.session_id)
                        st.success("Replace request sent; awaiting Execution Report.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not send replace request: {exc}")
                        st.stop()
                else:
                    st.error("Order Routing session is unavailable; replace was not sent.")
                    st.stop()
                st.rerun()
    else:
        st.caption("No open orders.")

    st.subheader("All Orders")
    if svc.orders:
        st.dataframe([asdict(o) for o in svc.orders.values()], use_container_width=True)

# ===========================================================================
# PAGE: Executions
# ===========================================================================
elif page == "Executions":
    st.subheader("Executions")
    cur = svc.conn.execute("SELECT * FROM executions ORDER BY timestamp DESC")
    cols = [c[0] for c in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if rows:
        st.dataframe(rows, use_container_width=True)
    else:
        st.caption("No executions yet.")

# ===========================================================================
# PAGE: FIX Message Log
# ===========================================================================
elif page == "FIX Message Log":
    st.subheader("FIX Message Log")
    c1, c2, c3 = st.columns(3)
    session_filter = c1.selectbox("Session", ["All", "MarketData", "OrderRouting"])
    direction_filter = c2.selectbox("Direction", ["All", "IN", "OUT"])
    msgtype_filter = c3.text_input("Message Type contains", "")

    filtered = svc.fix_log_all
    if session_filter != "All":
        filtered = [e for e in filtered if e.session == session_filter]
    if direction_filter != "All":
        filtered = [e for e in filtered if e.direction == direction_filter]
    if msgtype_filter:
        filtered = [e for e in filtered if msgtype_filter.lower() in e.msg_type.lower()]

    st.dataframe([asdict(e) for e in filtered[-500:][::-1]], use_container_width=True)

    log_text = "\n".join(f"{e.ts}\t{e.session}\t{e.direction}\t{e.msg_type}\t{e.seq_num}\t{e.raw}" for e in filtered)
    st.download_button("Download FIX Log", data=log_text or "no messages", file_name="fix_message_log.tsv")

# ===========================================================================
# PAGE: Settings
# ===========================================================================
elif page == "Settings":
    st.subheader("Configuration (read-only view)")
    st.caption("Edit .streamlit/secrets.toml to change these values. Passwords are masked.")
    safe_cfg = json.loads(json.dumps(cfg))
    for section in ("order", "market_data"):
        if safe_cfg[section].get("password"):
            safe_cfg[section]["password"] = "****"
    st.json(safe_cfg)
    if missing:
        st.error("Missing configuration:\n" + "\n".join(f"- {m}" for m in missing))
    else:
        st.success("Configuration looks complete." if not cfg["mock_mode"] else "Running in MOCK_MODE — real config not required.")

# ===========================================================================
# PAGE: Certification / Diagnostics
# ===========================================================================
elif page == "Certification / Diagnostics":
    st.subheader("Local FIX Diagnostics")
    st.warning(
        "These are LOCAL diagnostics only. Passing these checks does NOT mean TT FIX "
        "certification is complete. Certification is performed by Trading Technologies "
        "per their official process:\n\n"
        "https://library.tradingtechnologies.com/tt-fix/tt-fix-general/getting-started-tt-fix-general/tt-fix-certification/"
    )

    checks = [
        ("Configuration", not missing),
        ("Market Data connectivity", md_snap["status"] == "CONNECTED"),
        ("Order Routing connectivity", or_snap["status"] == "CONNECTED"),
        ("Logon (Market Data)", bool(md_snap["last_logon"])),
        ("Logon (Order Routing)", bool(or_snap["last_logon"])),
        ("Heartbeat observed", bool(md_snap["last_heartbeat"]) or bool(or_snap["last_heartbeat"])),
        ("At least one order submitted", len(svc.orders) > 0),
        ("At least one execution received", svc.conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0] > 0),
    ]
    rows = [{"Test Name": name, "PASS/FAIL": "PASS" if ok else "FAIL",
             "Timestamp": datetime.now(timezone.utc).isoformat()} for name, ok in checks]
    st.dataframe(rows, use_container_width=True)

    export_text = "\n".join(f"{r['Test Name']}\t{r['PASS/FAIL']}\t{r['Timestamp']}" for r in rows)
    st.download_button("Export Certification Evidence", data=export_text, file_name="certification_log.tsv")

    if not cfg["mock_mode"]:
        st.divider()
        st.markdown("#### Developer / Certification Mode — Raw Message Sender")
        dev_mode = st.checkbox("Enable Developer / Certification Mode (disabled by default)")
        if dev_mode:
            st.text_area("Raw FIX message (diagnostic only, not for production order flow)")
            st.button("Send Diagnostic Message", disabled=not QUICKFIX_AVAILABLE)
