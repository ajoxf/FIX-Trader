"""Streamlit-independent FIX 4.2 service for the TT desktop terminal."""

from __future__ import annotations

import os
import random
import socket
import sqlite3
import threading
import time
import tomllib
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


SOH = "\x01"
ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "storage.db"
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fix_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:-3]


def parse_pairs(raw: str) -> list[tuple[str, str]]:
    return [tuple(item.split("=", 1)) for item in raw.split(SOH) if "=" in item]


def parse_fields(raw: str) -> dict[str, str]:
    return dict(parse_pairs(raw))


def encode_fix(fields: list[tuple[str, str]]) -> bytes:
    body = SOH.join(f"{tag}={value}" for tag, value in fields) + SOH
    prefix = f"8=FIX.4.2{SOH}".encode("ascii")
    middle = f"9={len(body.encode('ascii'))}{SOH}".encode("ascii") + body.encode("ascii")
    return prefix + middle + f"10={sum(prefix + middle) % 256:03d}{SOH}".encode("ascii")


def mask_fix(raw: str) -> str:
    fields = []
    for tag, value in parse_pairs(raw):
        fields.append(f"{tag}={'****' if tag == '96' else value}")
    return "|".join(fields)


def market_data_request_fields(
    subscription: dict[str, Any], request_type: str,
) -> list[tuple[str, str]]:
    """Build a TT FIX 35=V body in TT's documented component order."""
    if request_type not in {"0", "1", "2"}:
        raise ValueError(f"Unsupported market-data request type: {request_type}")

    fields: list[tuple[str, str]] = [
        ("262", str(subscription["request_id"])),
        ("263", request_type),
    ]
    if request_type != "2":
        fields.append(("264", "0" if subscription["full_book"] else "1"))
        if request_type == "1":
            fields.append(("265", "1" if subscription["continuous"] else "0"))
        fields.append(("266", "Y"))

    # NoRelatedSym is a repeating group whose delimiter is Symbol (55).
    fields.extend([("146", "1"), ("55", str(subscription["symbol"]))])
    if subscription.get("exchange"):
        fields.append(("207", str(subscription["exchange"])))
    if subscription.get("security_type"):
        fields.append(("167", str(subscription["security_type"])))
    if subscription.get("maturity"):
        fields.append(("200", str(subscription["maturity"])))
    if subscription.get("security_id"):
        fields.extend([
            ("48", str(subscription["security_id"])),
            ("22", "96"),
        ])

    if request_type != "2":
        fields.extend([
            ("267", "3"), ("269", "0"), ("269", "1"), ("269", "2"),
        ])
    return fields


def _config_value(data: dict, section: str, key: str, default: Any = "") -> Any:
    env_name = f"{section}_{key}".upper()
    return os.environ.get(env_name, data.get(section, {}).get(key, default))


def load_config(path: Path = SECRETS_PATH) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)

    def truthy(section: str, key: str, default: bool) -> bool:
        return str(_config_value(data, section, key, str(default))).lower() == "true"

    return {
        "environment": _config_value(data, "application", "environment", "UAT"),
        "mock_mode": truthy("application", "mock_mode", False),
        "enable_live_orders": truthy("application", "enable_live_order_submission", False),
        "order": {
            "host": _config_value(data, "fix_order", "host", "fixorderrouting-ext-uat-cert.trade.tt"),
            "port": int(_config_value(data, "fix_order", "port", 11502)),
            "sender_comp_id": _config_value(data, "fix_order", "sender_comp_id", "AJUATORDER"),
            "target_comp_id": _config_value(data, "fix_order", "target_comp_id", "TT"),
            "sender_sub_id": _config_value(data, "fix_order", "sender_sub_id", ""),
            "on_behalf_of_sub_id": _config_value(data, "fix_order", "on_behalf_of_sub_id", "AJUAT"),
            "password": _config_value(data, "fix_order", "password", ""),
            "account": _config_value(data, "fix_order", "account", "AJ_account"),
            "heartbeat": int(_config_value(data, "fix_order", "heartbeat", 30)),
        },
        "market_data": {
            "host": _config_value(data, "fix_market_data", "host", "fixmarketdata-ext-uat-cert.trade.tt"),
            "port": int(_config_value(data, "fix_market_data", "port", 11503)),
            "sender_comp_id": _config_value(data, "fix_market_data", "sender_comp_id", "AJUATMARKET"),
            "target_comp_id": _config_value(data, "fix_market_data", "target_comp_id", "TT"),
            "sender_sub_id": _config_value(data, "fix_market_data", "sender_sub_id", ""),
            "on_behalf_of_sub_id": _config_value(data, "fix_market_data", "on_behalf_of_sub_id", "AJUAT"),
            "password": _config_value(data, "fix_market_data", "password", ""),
            "heartbeat": int(_config_value(data, "fix_market_data", "heartbeat", 30)),
        },
    }


@dataclass
class SessionSnapshot:
    name: str
    status: str = "DISCONNECTED"
    sender_comp_id: str = ""
    target_comp_id: str = ""
    incoming_count: int = 0
    outgoing_count: int = 0
    in_seq: int = 0
    out_seq: int = 0
    last_message: str = ""
    last_heartbeat: str = ""
    last_logon: str = ""
    error: str = ""


@dataclass
class Quote:
    symbol: str
    bid: Optional[float] = None
    bid_size: Optional[float] = None
    ask: Optional[float] = None
    ask_size: Optional[float] = None
    last: Optional[float] = None
    last_size: Optional[float] = None
    timestamp: str = ""


@dataclass
class Tick:
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
class Order:
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
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    raw_last_report: str = ""


@dataclass
class Instrument:
    security_id: str
    symbol: str
    exchange: str = ""
    security_type: str = ""
    maturity_month_year: str = ""
    description: str = ""
    currency: str = ""
    request_id: str = ""
    received_at: str = field(default_factory=utc_now)


@dataclass
class SymbolMapping:
    bridge_symbol: str
    maker: str
    maker_symbol: str
    tt_symbol: str
    tt_exchange: str = ""
    tt_security_id: str = ""
    security_type: str = ""
    price_digits: int = 5
    enabled: bool = True
    updated_at: str = field(default_factory=utc_now)


class FixSession:
    """Small threaded FIX 4.2 session used by the desktop application."""

    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        on_message: Callable[[str, dict[str, str]], None],
        on_state: Callable[[SessionSnapshot], None],
        on_log: Callable[[dict[str, str]], None],
    ):
        self.config = config
        self.on_message = on_message
        self.on_state = on_state
        self.on_log = on_log
        self.state = SessionSnapshot(
            name=name,
            sender_comp_id=str(config["sender_comp_id"]),
            target_comp_id=str(config["target_comp_id"]),
        )
        self.state_lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.socket: Optional[socket.socket] = None
        self.thread: Optional[threading.Thread] = None
        self.last_send = 0.0

    def snapshot(self) -> SessionSnapshot:
        with self.state_lock:
            return SessionSnapshot(**asdict(self.state))

    def _publish_state(self) -> None:
        self.on_state(self.snapshot())

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        with self.state_lock:
            self.state.status = "CONNECTING"
            self.state.error = ""
        self._publish_state()
        self.thread = threading.Thread(target=self._run, name=f"FIX-{self.state.name}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.socket:
            try:
                self.send("5", [("58", "Client disconnect")])
            except Exception:
                pass
        self.stop_event.set()
        if self.socket:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.socket.close()
            except OSError:
                pass

    def send(self, msg_type: str, fields: list[tuple[str, str]]) -> None:
        with self.send_lock:
            if not self.socket:
                raise ConnectionError(f"{self.state.name} socket is not connected")
            with self.state_lock:
                self.state.out_seq += 1
                seq = self.state.out_seq
                self.state.outgoing_count += 1
                self.state.last_message = utc_now()
            header = [
                ("35", msg_type), ("34", str(seq)),
                ("49", str(self.config["sender_comp_id"])), ("52", fix_timestamp()),
                ("56", str(self.config["target_comp_id"])),
            ]
            if self.config.get("sender_sub_id"):
                header.append(("50", str(self.config["sender_sub_id"])))
            if self.config.get("on_behalf_of_sub_id"):
                header.append(("116", str(self.config["on_behalf_of_sub_id"])))
            payload = encode_fix(header + fields)
            self.socket.sendall(payload)
            self.last_send = time.monotonic()
            self.on_log({
                "timestamp": utc_now(), "session": self.state.name, "direction": "OUT",
                "message_type": msg_type, "sequence": str(seq),
                "raw": mask_fix(payload.decode("ascii")),
            })
            self._publish_state()

    def _run(self) -> None:
        try:
            self.socket = socket.create_connection(
                (str(self.config["host"]), int(self.config["port"])), timeout=15,
            )
            self.socket.settimeout(0.25)
            with self.state_lock:
                self.state.in_seq = 0
                self.state.out_seq = 0
            password = str(self.config.get("password", ""))
            self.send("A", [
                ("98", "0"), ("108", str(self.config.get("heartbeat", 30))),
                ("95", str(len(password.encode("utf-8")))), ("96", password), ("141", "Y"),
            ])
            buffer = ""
            logon_deadline = time.monotonic() + 20
            heartbeat = max(5, int(self.config.get("heartbeat", 30)))
            while not self.stop_event.is_set():
                try:
                    chunk = self.socket.recv(65536)
                    if not chunk:
                        raise ConnectionError("TT closed the FIX socket")
                    buffer += chunk.decode("ascii", errors="replace")
                    while f"{SOH}10=" in buffer:
                        checksum_start = buffer.index(f"{SOH}10=") + 1
                        end = buffer.index(SOH, checksum_start) + 1
                        raw, buffer = buffer[:end], buffer[end:]
                        self._receive(raw)
                except socket.timeout:
                    pass
                if time.monotonic() - self.last_send >= heartbeat:
                    self.send("0", [])
                if self.state.status == "CONNECTING" and time.monotonic() >= logon_deadline:
                    raise TimeoutError("TT did not answer Logon within 20 seconds")
        except Exception as exc:
            if not self.stop_event.is_set():
                with self.state_lock:
                    self.state.status = "ERROR"
                    self.state.error = str(exc)
                self._publish_state()
        finally:
            if self.socket:
                try:
                    self.socket.close()
                except OSError:
                    pass
            self.socket = None
            if self.state.status != "ERROR":
                with self.state_lock:
                    self.state.status = "DISCONNECTED"
                self._publish_state()

    def _receive(self, raw: str) -> None:
        fields = parse_fields(raw)
        msg_type = fields.get("35", "?")
        seq = fields.get("34", "?")
        with self.state_lock:
            self.state.incoming_count += 1
            if seq.isdigit():
                self.state.in_seq = int(seq)
            self.state.last_message = utc_now()
        self.on_log({
            "timestamp": utc_now(), "session": self.state.name, "direction": "IN",
            "message_type": msg_type, "sequence": seq, "raw": mask_fix(raw),
        })
        if msg_type == "A":
            with self.state_lock:
                self.state.status = "CONNECTED"
                self.state.error = ""
                self.state.last_logon = utc_now()
        elif msg_type == "5":
            with self.state_lock:
                self.state.status = "ERROR"
                self.state.error = fields.get("58", "TT closed the FIX session")
            self.stop_event.set()
        elif msg_type == "0":
            with self.state_lock:
                self.state.last_heartbeat = utc_now()
        elif msg_type == "1":
            self.send("0", [("112", fields.get("112", ""))])
        self._publish_state()
        self.on_message(raw, fields)


class TradingService:
    """Thread-safe application service shared by all desktop widgets."""

    def __init__(self, config: Optional[dict[str, Any]] = None):
        self.config = config or load_config()
        self.listeners: list[Callable[[str, Any], None]] = []
        self.data_lock = threading.RLock()
        self.db_lock = threading.Lock()
        self.quotes: dict[str, Quote] = {}
        self.ticks: deque[Tick] = deque(maxlen=5000)
        self.tick_count = 0
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.instruments: dict[str, Instrument] = {}
        self.logs: deque[dict[str, str]] = deque(maxlen=5000)
        self.orders: dict[str, Order] = {}
        self._mock_stop = threading.Event()
        self._mock_thread: Optional[threading.Thread] = None
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._create_schema()
        self._load_orders()
        self.market_session = FixSession(
            "MarketData", self.config["market_data"], self._market_message,
            lambda state: self.emit("session", state), self._record_log,
        )
        self.order_session = FixSession(
            "OrderRouting", self.config["order"], self._order_message,
            lambda state: self.emit("session", state), self._record_log,
        )

    def add_listener(self, callback: Callable[[str, Any], None]) -> None:
        self.listeners.append(callback)

    def emit(self, event: str, payload: Any) -> None:
        for callback in tuple(self.listeners):
            try:
                callback(event, payload)
            except Exception:
                continue

    def _create_schema(self) -> None:
        self.conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            client_order_id TEXT PRIMARY KEY, account TEXT, symbol TEXT, side TEXT,
            order_type TEXT, quantity REAL, price REAL, tif TEXT, status TEXT,
            exchange_order_id TEXT, filled_qty REAL, remaining_qty REAL, avg_px REAL,
            reject_reason TEXT, created_at TEXT, updated_at TEXT, raw_last_report TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS executions (
            execution_id TEXT PRIMARY KEY, client_order_id TEXT, exchange_order_id TEXT,
            symbol TEXT, side TEXT, quantity REAL, price REAL, execution_type TEXT,
            execution_status TEXT, timestamp TEXT, raw_message TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS symbol_mappings (
            bridge_symbol TEXT PRIMARY KEY, maker TEXT NOT NULL, maker_symbol TEXT NOT NULL,
            tt_symbol TEXT NOT NULL, tt_exchange TEXT, tt_security_id TEXT,
            security_type TEXT, price_digits INTEGER NOT NULL, enabled INTEGER NOT NULL,
            updated_at TEXT NOT NULL)""")
        self.conn.commit()

    def _load_orders(self) -> None:
        cursor = self.conn.execute("SELECT * FROM orders")
        columns = [column[0] for column in cursor.description]
        for row in cursor.fetchall():
            data = dict(zip(columns, row))
            self.orders[data["client_order_id"]] = Order(**data)

    def _save_order(self, order: Order) -> None:
        values = asdict(order)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        with self.db_lock:
            self.conn.execute(
                f"INSERT OR REPLACE INTO orders ({columns}) VALUES ({placeholders})",
                list(values.values()),
            )
            self.conn.commit()

    def _record_log(self, item: dict[str, str]) -> None:
        with self.data_lock:
            self.logs.append(item)
        self.emit("log", item)

    def connect_market(self) -> None:
        if self.config["mock_mode"]:
            self._connect_mock(self.market_session)
            if not self._mock_thread or not self._mock_thread.is_alive():
                self._mock_stop.clear()
                self._mock_thread = threading.Thread(target=self._mock_worker, name="FIX-MockData", daemon=True)
                self._mock_thread.start()
            return
        self.market_session.start()

    def connect_orders(self) -> None:
        if self.config["mock_mode"]:
            self._connect_mock(self.order_session)
            return
        self.order_session.start()

    def disconnect_market(self) -> None:
        if self.config["mock_mode"]:
            self._mock_stop.set()
            self._disconnect_mock(self.market_session)
            return
        self.market_session.stop()

    def disconnect_orders(self) -> None:
        if self.config["mock_mode"]:
            self._disconnect_mock(self.order_session)
            return
        self.order_session.stop()

    def _connect_mock(self, session: FixSession) -> None:
        with session.state_lock:
            session.state.status = "CONNECTED"
            session.state.error = ""
            session.state.last_logon = utc_now()
        session._publish_state()

    def _disconnect_mock(self, session: FixSession) -> None:
        with session.state_lock:
            session.state.status = "DISCONNECTED"
        session._publish_state()

    def _mock_worker(self) -> None:
        mids: dict[str, float] = {}
        while not self._mock_stop.wait(0.05):
            with self.data_lock:
                symbols = list(self.subscriptions)
            for symbol in symbols:
                mid = mids.get(symbol, 100.0) + random.uniform(-0.025, 0.025)
                mids[symbol] = mid
                bid, ask, trade = round(mid - 0.01, 5), round(mid + 0.01, 5), round(mid, 5)
                with self.market_session.state_lock:
                    self.market_session.state.in_seq += 1
                    self.market_session.state.incoming_count += 1
                    self.market_session.state.last_message = utc_now()
                    sequence = self.market_session.state.in_seq
                raw = SOH.join([
                    "8=FIX.4.2", "35=W", f"34={sequence}", f"52={fix_timestamp()}",
                    f"55={symbol}", "268=3",
                    "269=0", f"270={bid}", f"271={random.randint(1, 20)}", "290=1",
                    "269=1", f"270={ask}", f"271={random.randint(1, 20)}", "290=1",
                    "269=2", f"270={trade}", f"271={random.randint(1, 5)}", "10=000",
                ]) + SOH
                self._apply_market_data(raw, parse_fields(raw))
            if symbols:
                self.market_session._publish_state()

    def close(self) -> None:
        self.disconnect_market()
        self.disconnect_orders()
        with self.db_lock:
            self.conn.close()

    def _market_message(self, raw: str, fields: dict[str, str]) -> None:
        msg_type = fields.get("35", "")
        if msg_type in {"W", "X"}:
            self._apply_market_data(raw, fields)
            self.emit("market_response", {
                "message_type": msg_type,
                "request_id": fields.get("262", ""),
                "symbol": fields.get("55", ""),
            })
        elif msg_type == "Y":
            reason = fields.get("58") or "TT rejected the market-data request"
            request_id = fields.get("262", "")
            self.emit("market_reject", f"{request_id + ': ' if request_id else ''}{reason}")
        elif msg_type == "j" and fields.get("372") == "V":
            reason_names = {
                "0": "Other", "1": "Unknown ID", "2": "Unknown security",
                "3": "Unsupported message type", "4": "Application unavailable",
                "5": "Conditionally required field missing",
            }
            code = fields.get("380", "")
            reason = fields.get("58") or reason_names.get(code, f"reason {code or 'unknown'}")
            self.emit("market_reject", f"TT business reject: {reason}")
        elif msg_type == "3":
            reason = fields.get("58") or f"session reject reason {fields.get('373', 'unknown')}"
            ref_tag = fields.get("371", "")
            detail = f" (tag {ref_tag})" if ref_tag else ""
            self.emit("market_reject", f"TT FIX session reject: {reason}{detail}")
        elif msg_type == "d":
            security_id = fields.get("48", "")
            symbol = fields.get("55", "")
            if security_id and symbol:
                instrument = Instrument(
                    security_id=security_id, symbol=symbol, exchange=fields.get("207", ""),
                    security_type=fields.get("167", ""), maturity_month_year=fields.get("200", ""),
                    description=fields.get("107", ""), currency=fields.get("15", ""),
                    request_id=fields.get("320", ""),
                )
                with self.data_lock:
                    self.instruments[security_id] = instrument
                self.emit("instrument", instrument)

    def _market_groups(self, raw: str) -> list[dict[str, str]]:
        groups: list[dict[str, str]] = []
        current: Optional[dict[str, str]] = None
        started = False
        for tag, value in parse_pairs(raw):
            if tag == "268":
                started = True
                continue
            if not started:
                continue
            if tag == "279":
                if current:
                    groups.append(current)
                current = {tag: value}
            elif tag == "269":
                if current and "269" in current:
                    groups.append(current)
                    current = {}
                if current is None:
                    current = {}
                current[tag] = value
            elif current is not None and tag != "10":
                current[tag] = value
        if current:
            groups.append(current)
        return groups

    def _apply_market_data(self, raw: str, fields: dict[str, str]) -> None:
        type_names = {"0": "BID", "1": "ASK", "2": "TRADE"}
        action_names = {"0": "NEW", "1": "CHANGE", "2": "DELETE"}
        received_at = utc_now()
        default_symbol = fields.get("55", "")
        emitted: list[tuple[Tick, Quote]] = []
        with self.data_lock:
            for entry in self._market_groups(raw):
                symbol = entry.get("55") or default_symbol
                if not symbol and len(self.subscriptions) == 1:
                    symbol = next(iter(self.subscriptions))
                if not symbol:
                    continue
                entry_type = entry.get("269", "")
                action = entry.get("279", "SNAPSHOT" if fields.get("35") == "W" else "")
                try:
                    price = float(entry["270"]) if entry.get("270") else None
                    size = float(entry["271"]) if entry.get("271") else None
                except ValueError:
                    price, size = None, None
                tick = Tick(
                    received_at=received_at,
                    exchange_time=entry.get("273", fields.get("52", "")),
                    sequence=fields.get("34", ""), symbol=symbol,
                    update=action_names.get(action, action),
                    entry_type=type_names.get(entry_type, entry_type),
                    price=price, size=size, entry_id=entry.get("278", ""),
                    position=entry.get("290", ""),
                )
                self.ticks.append(tick)
                self.tick_count += 1
                quote = self.quotes.setdefault(symbol, Quote(symbol=symbol))
                if action != "2" and price is not None:
                    if entry_type == "0":
                        quote.bid, quote.bid_size = price, size
                    elif entry_type == "1":
                        quote.ask, quote.ask_size = price, size
                    elif entry_type == "2":
                        quote.last, quote.last_size = price, size
                    quote.timestamp = received_at
                emitted.append((tick, Quote(**asdict(quote))))
        if emitted:
            # One cross-thread notification per FIX message is substantially
            # cheaper than one GUI notification per individual depth entry.
            self.emit("market_batch", emitted)

    def subscribe_market_data(
        self, symbol: str, exchange: str = "", security_id: str = "",
        security_type: str = "", maturity: str = "", full_book: bool = True,
        continuous: bool = True,
    ) -> None:
        symbol = symbol.strip()
        if not symbol:
            raise ValueError("Symbol is required for a market-data subscription")
        if self.market_session.snapshot().status != "CONNECTED":
            raise ConnectionError("Market Data session is not connected")
        request_id = f"MD-{uuid.uuid4().hex[:16].upper()}"
        subscription = {
            "request_id": request_id, "symbol": symbol, "exchange": exchange,
            "security_id": security_id, "security_type": security_type,
            "maturity": maturity, "full_book": full_book, "continuous": continuous,
        }
        if self.config["mock_mode"]:
            with self.data_lock:
                self.subscriptions[symbol] = subscription
                self.quotes[symbol] = Quote(symbol=symbol)
            self.emit("market_subscription", subscription)
            return
        # Register before send so an immediate snapshot/incremental refresh can
        # still be correlated when TT omits Symbol from subsequent entries.
        with self.data_lock:
            previous_subscription = self.subscriptions.get(symbol)
            previous_quote = self.quotes.get(symbol)
            self.subscriptions[symbol] = subscription
            self.quotes[symbol] = Quote(symbol=symbol)
        request_type = "1" if continuous else "0"
        try:
            self.market_session.send("V", market_data_request_fields(subscription, request_type))
        except Exception:
            with self.data_lock:
                if previous_subscription is None:
                    self.subscriptions.pop(symbol, None)
                else:
                    self.subscriptions[symbol] = previous_subscription
                if previous_quote is None:
                    self.quotes.pop(symbol, None)
                else:
                    self.quotes[symbol] = previous_quote
            raise
        self.emit("market_subscription", subscription)

    def unsubscribe_market_data(self, symbol: str) -> None:
        with self.data_lock:
            subscription = self.subscriptions.get(symbol)
        if not subscription:
            return
        if not self.config["mock_mode"]:
            if self.market_session.snapshot().status != "CONNECTED":
                raise ConnectionError("Market Data session is not connected")
            self.market_session.send("V", market_data_request_fields(subscription, "2"))
        with self.data_lock:
            self.subscriptions.pop(symbol, None)
            self.quotes.pop(symbol, None)
        self.emit("unsubscribed", symbol)

    def request_instruments(
        self, exchange: str, security_type: str, symbol: str = "", maturity: str = "",
    ) -> None:
        request_id = f"SECDEF-{uuid.uuid4().hex[:16].upper()}"
        with self.data_lock:
            self.instruments.clear()
        if self.config["mock_mode"]:
            if self.market_session.snapshot().status != "CONNECTED":
                raise ConnectionError("Mock Market Data session is not connected")
            instrument = Instrument(
                security_id=f"MOCK-{(symbol or 'INSTRUMENT').upper()}-{maturity or 'SPOT'}",
                symbol=(symbol or "ES").upper(), exchange=exchange,
                security_type=security_type, maturity_month_year=maturity,
                description="Mock instrument definition", currency="USD", request_id=request_id,
            )
            with self.data_lock:
                self.instruments[instrument.security_id] = instrument
            self.emit("instrument", instrument)
            return
        fields: list[tuple[str, str]] = [("320", request_id), ("321", "0")]
        if symbol:
            fields.append(("55", symbol))
        if exchange:
            fields.append(("207", exchange))
        if security_type:
            fields.append(("167", security_type))
        if maturity:
            fields.append(("200", maturity))
        self.market_session.send("c", fields)

    def submit_order(
        self, account: str, symbol: str, side: str, order_type: str, quantity: float,
        price: Optional[float], tif: str, security_id: str = "", exchange: str = "",
        security_type: str = "", maturity: str = "", client_order_id: str = "",
    ) -> Order:
        if not self.config["mock_mode"] and not self.config["enable_live_orders"]:
            raise PermissionError("Live order submission is disabled in secrets.toml")
        if self.order_session.snapshot().status != "CONNECTED":
            raise ConnectionError("Order Routing session is not connected")
        client_order_id = client_order_id or f"DESK-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"
        if client_order_id in self.orders:
            raise ValueError("Client Order ID must be unique")
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero")
        if order_type in {"LIMIT", "STOP_LIMIT"} and not price:
            raise ValueError("A price is required for LIMIT and STOP_LIMIT orders")
        sides = {"BUY": "1", "SELL": "2"}
        order_types = {"MARKET": "1", "LIMIT": "2", "STOP": "3", "STOP_LIMIT": "4"}
        tifs = {"DAY": "0", "GTC": "1", "IOC": "3", "FOK": "4", "GTD": "6"}
        order = Order(
            client_order_id=client_order_id, account=account, symbol=symbol,
            side=side, order_type=order_type, quantity=quantity, price=price, tif=tif,
            remaining_qty=quantity,
        )
        self.orders[client_order_id] = order
        self._save_order(order)
        if self.config["mock_mode"]:
            order.status = "NEW"
            order.exchange_order_id = f"MOCK-{uuid.uuid4().hex[:8].upper()}"
            order.updated_at = utc_now()
            self._save_order(order)
            self.emit("order", order)
            return order
        fields = [
            ("11", client_order_id), ("1", account), ("55", symbol),
            ("54", sides[side]), ("38", str(quantity)), ("40", order_types[order_type]),
            ("59", tifs[tif]), ("60", fix_timestamp()),
        ]
        if security_id:
            fields.extend([("48", security_id), ("22", "96")])
        if exchange:
            fields.append(("207", exchange))
        if security_type:
            fields.append(("167", security_type))
        if maturity:
            fields.append(("200", maturity))
        if price:
            fields.append(("44", str(price)))
        try:
            self.order_session.send("D", fields)
        except Exception as exc:
            order.status = "REJECTED"
            order.reject_reason = str(exc)
            order.updated_at = utc_now()
            self._save_order(order)
            raise
        self.emit("order", order)
        return order

    def cancel_order(self, client_order_id: str) -> None:
        order = self.orders[client_order_id]
        if self.config["mock_mode"]:
            order.status = "CANCELED"
            order.updated_at = utc_now()
            self._save_order(order)
            self.emit("order", order)
            return
        side = {"BUY": "1", "SELL": "2"}[order.side]
        self.order_session.send("F", [
            ("41", order.client_order_id),
            ("11", f"CXL-{uuid.uuid4().hex[:12].upper()}"),
            ("55", order.symbol), ("54", side),
            ("38", str(order.remaining_qty or order.quantity)), ("60", fix_timestamp()),
        ])

    def replace_order(self, client_order_id: str, quantity: float, price: Optional[float]) -> None:
        order = self.orders[client_order_id]
        if self.config["mock_mode"]:
            order.quantity = quantity
            order.remaining_qty = max(0.0, quantity - order.filled_qty)
            order.price = price
            order.status = "REPLACED"
            order.updated_at = utc_now()
            self._save_order(order)
            self.emit("order", order)
            return
        side = {"BUY": "1", "SELL": "2"}[order.side]
        order_type = {"MARKET": "1", "LIMIT": "2", "STOP": "3", "STOP_LIMIT": "4"}[order.order_type]
        fields = [
            ("41", order.client_order_id),
            ("11", f"RPL-{uuid.uuid4().hex[:12].upper()}"),
            ("55", order.symbol), ("54", side), ("38", str(quantity)),
            ("40", order_type), ("60", fix_timestamp()),
        ]
        if price:
            fields.append(("44", str(price)))
        self.order_session.send("G", fields)

    def _order_message(self, raw: str, fields: dict[str, str]) -> None:
        if fields.get("35") != "8":
            return
        client_order_id = fields.get("11", "")
        order = self.orders.get(client_order_id)
        if not order:
            return
        statuses = {
            "0": "NEW", "1": "PARTIALLY_FILLED", "2": "FILLED", "4": "CANCELED",
            "5": "REPLACED", "6": "PENDING", "8": "REJECTED", "C": "EXPIRED",
            "A": "PENDING",
        }
        order.status = statuses.get(fields.get("39", fields.get("150", "")), "PENDING")
        order.exchange_order_id = fields.get("37", order.exchange_order_id)
        for attribute, tag in (("filled_qty", "14"), ("remaining_qty", "151"), ("avg_px", "6")):
            try:
                setattr(order, attribute, float(fields.get(tag, getattr(order, attribute))))
            except (TypeError, ValueError):
                pass
        order.reject_reason = fields.get("58", "") if order.status == "REJECTED" else ""
        order.updated_at = utc_now()
        order.raw_last_report = raw
        self._save_order(order)
        execution_id = fields.get("17", "")
        if execution_id and fields.get("150") in {"1", "2", "F"}:
            with self.db_lock:
                self.conn.execute(
                    "INSERT OR REPLACE INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (execution_id, client_order_id, order.exchange_order_id, order.symbol,
                     order.side, order.filled_qty, order.avg_px, fields.get("150", ""),
                     order.status, utc_now(), raw),
                )
                self.conn.commit()
            self.emit("execution", execution_id)
        self.emit("order", order)

    def execution_rows(self) -> list[dict[str, Any]]:
        with self.db_lock:
            cursor = self.conn.execute("SELECT * FROM executions ORDER BY timestamp DESC")
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def mapping_rows(self) -> list[dict[str, Any]]:
        with self.db_lock:
            cursor = self.conn.execute("SELECT * FROM symbol_mappings ORDER BY bridge_symbol")
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def save_mapping(self, mapping: SymbolMapping) -> None:
        mapping.updated_at = utc_now()
        with self.db_lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO symbol_mappings
                (bridge_symbol, maker, maker_symbol, tt_symbol, tt_exchange, tt_security_id,
                 security_type, price_digits, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mapping.bridge_symbol, mapping.maker, mapping.maker_symbol, mapping.tt_symbol,
                 mapping.tt_exchange, mapping.tt_security_id, mapping.security_type,
                 mapping.price_digits, int(mapping.enabled), mapping.updated_at),
            )
            self.conn.commit()
        self.emit("mapping", mapping)
