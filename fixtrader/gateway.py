"""Venue protocol and TT UAT connection adapter.

The native FIX 4.2 session is extracted from backup_v1fixapp.py. Instrument,
quote and reviewed manual UAT order workflows live in ManualTerminal.
The strategy protocol still reports unknown account positions/orders as None.
"""

import copy
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable

from .models import (BookTop, Fill, GatewayEvent, OrderRequest, SecurityDef,
                     SessionState, VenueOrder, VenuePosition)
from .manual_terminal import ManualTerminal
from .fix_audit import FixAuditLog

#: Every order we send carries this. Anything at the venue without it belongs
#: to somebody else — a hand order in TT, most likely — and is never
#: cancelled, amended or counted as ours.
CLORDID_PREFIX = "FT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@runtime_checkable
class Gateway(Protocol):
    """What the rest of the system is allowed to ask a venue for.

    Note the return types that include None. `orders()` and `positions()`
    return None for **"could not read"**, which is not "there are none".
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> SessionState: ...
    def state_text(self) -> str: ...
    def subscribe(self, contract) -> None: ...
    def top_of_book(self, key: str) -> Optional[BookTop]: ...
    def security_definition(self, contract) -> Optional[SecurityDef]: ...
    def margin_for(self, key: str, qty: float) -> Optional[float]: ...
    def send(self, order: OrderRequest) -> str: ...
    def cancel(self, clordid: str) -> None: ...
    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None: ...
    def orders(self) -> Optional[List[VenueOrder]]: ...
    def positions(self) -> Optional[List[VenuePosition]]: ...
    def drain_events(self) -> List[GatewayEvent]: ...


class FixGateway:
    """TT UAT connectivity using the native client from backup_v1fixapp.py.

    Manual execution is isolated from the strategy gateway, which does not
    reconcile or automatically execute against the operator's manual book.
    """
    NOT_WIRED = "Algorithmic FIX execution is not wired; use the reviewed manual UAT ticket"
    connection_only = True

    def __init__(self, venue, contracts=None, manual_path=':memory:'):
        self.venue = venue
        self.contracts = list(contracts or [])
        self._sessions = {}
        self._text = "Disconnected"
        self._lock = threading.RLock()
        self._activity = deque(maxlen=80)
        self._activity_lock = threading.Lock()
        self._reconnect_at = None
        self._connect_not_before = 0
        # Disk IO never runs on the latency-sensitive FIX receiver threads.
        self.audit = FixAuditLog(async_write=True)
        self._contract_security_ids = {}
        self.terminal = ManualTerminal(self, manual_path)

    def start(self):
        with self._lock:
            if time.monotonic() < self._connect_not_before:
                self._reconnect_at = self._connect_not_before
                self._text = 'Waiting before reconnecting to TT'
                return
            if self._sessions and all(s.is_running() for s in self._sessions.values()):
                return
            v = self.venue
            if v.environment != 'UAT' or v.fix_version != 'FIX.4.2':
                self._text = "This backup connection adapter requires TT UAT and FIX.4.2"
                return
            if not v.reset_seq_on_logon:
                self._text = "The backup UAT client requires reset_seq_on_logon"
                return
            common = dict(target_comp_id=v.target_comp_id,
                          sender_sub_id=v.sender_sub_id,
                          target_sub_id=v.target_sub_id,
                          on_behalf_of_comp_id=v.on_behalf_of_comp_id,
                          on_behalf_of_sub_id=v.on_behalf_of_sub_id,
                          heartbeat=v.heartbeat_sec, use_tls=v.use_tls)
            configs = {'Order Routing': dict(common, host=v.host, port=v.port,
                sender_comp_id=v.sender_comp_id, password=v.password)}
            if v.md_host:
                configs['Market Data'] = dict(common, host=v.md_host, port=v.md_port,
                    sender_comp_id=v.md_sender_comp_id,
                    target_comp_id=v.md_target_comp_id or v.target_comp_id,
                    password=os.environ.get(v.md_password_env, ''))
            for name, cfg in configs.items():
                missing = [k for k in ('host', 'port', 'sender_comp_id', 'target_comp_id', 'password') if not cfg.get(k)]
                if missing:
                    self._text = name + ': missing ' + ', '.join(missing)
                    return
            for name, cfg in configs.items():
                cfg['allowed_messages'] = ('c', 'V') if name == 'Market Data' else ('D', 'F', 'G')
                existing = self._sessions.get(name)
                if existing is not None:
                    if existing.is_running():
                        continue
                    existing.stop()
                    existing.thread.join(timeout=17)
                state = SimpleNamespace(lock=threading.RLock(), status='DISCONNECTED',
                    error='', out_seq=0, in_seq=0, incoming_count=0, outgoing_count=0,
                    last_message='', last_heartbeat='', last_logon='')
                session = NativeFixSession(self, state, name, cfg)
                self._sessions[name] = session
                session.start()

    def stop(self):
        with self._lock:
            self._reconnect_at = None
            for session in self._sessions.values():
                session.stop()
            for session in self._sessions.values():
                session.thread.join(timeout=17)
            self._sessions.clear()
            self._text = 'Disconnected'
            self._connect_not_before = time.monotonic() + 10

    def reconnect(self):
        self.stop()
        self._reconnect_at = time.monotonic() + 10
        self._text = 'Reconnecting in 10 seconds'

    def state(self):
        if self._reconnect_at is not None:
            return SessionState.CONNECTING
        states = [s.state.status for s in self._sessions.values()]
        if states and all(s == 'CONNECTED' for s in states):
            return SessionState.LOGGED_ON
        if 'ERROR' in states:
            return SessionState.ERROR
        if 'CONNECTING' in states:
            return SessionState.CONNECTING
        return SessionState.DOWN

    def state_text(self):
        if not self._sessions:
            return self._text
        return '; '.join(name + ': ' + s.state.status +
            (' ? ' + self._redact(s.state.error) if s.state.error else '')
            for name, s in self._sessions.items())

    def _redact(self, text):
        for session in self._sessions.values():
            password = session.cfg.get('password')
            if password:
                text = text.replace(password, '[redacted]')
        return text

    def log_fix(self, session, direction, msg_type, seq_num, raw):
        # Raw protocol evidence is durable; sensitive fields are redacted by the writer.
        names = {'A': 'Logon', '0': 'Heartbeat', '1': 'Test request',
                 '5': 'Logout', '2': 'Resend request', '3': 'Reject', '4': 'Sequence reset'}
        fields = parse_fix_message(raw)
        category = ('Market Data' if msg_type in ('c', 'd', 'V', 'W', 'X', 'Y', 'j')
                    else 'Executions' if msg_type in ('8', '9')
                    else 'Orders' if msg_type in ('D', 'F', 'G') else 'FIX Session')
        details = {key: fields.get(tag) for key, tag in {
            'instrument': '55', 'security_id': '48', 'client_order_id': '11',
            'order_id': '37', 'execution_id': '17', 'execution_type': '150',
            'order_status': '39', 'side': '54', 'quantity': '38', 'price': '44',
            'fill_quantity': '32', 'fill_price': '31', 'remaining_quantity': '151',
            'average_price': '6', 'reason': '58', 'fix_sending_time': '52'
        }.items() if fields.get(tag) not in (None, '')}
        # A client-requested Logout is normal lifecycle activity, not an
        # error. An inbound Logout deserves attention, while explicit FIX,
        # order-change and market-data rejects remain errors.
        level = ('ERROR' if msg_type in ('3', '9', 'Y', 'j') else
                 'WARNING' if msg_type == '5' and direction == 'IN' else 'INFO')
        self.audit.write(level=level,
                         category=category, session=session, direction=direction,
                         event=names.get(msg_type, msg_type), sequence=str(seq_num),
                         details=details, raw=raw)
        with self._activity_lock:
            self._activity.append({'time': utcnow().isoformat(), 'session': session,
                'direction': direction, 'type': names.get(msg_type, msg_type),
                'sequence': seq_num})

    def connection_snapshot(self):
        sessions = []
        for name in ('Order Routing', 'Market Data'):
            session = self._sessions.get(name)
            md = name == 'Market Data'
            v = self.venue
            row = {'name': name, 'host': v.md_host if md else v.host,
                   'port': v.md_port if md else v.port,
                   'sender_comp_id': v.md_sender_comp_id if md else v.sender_comp_id,
                   'target_comp_id': (v.md_target_comp_id or v.target_comp_id) if md else v.target_comp_id,
                   'password_set': bool(os.environ.get(v.md_password_env)) if md else v.has_password,
                   'status': 'CONNECTING' if self._reconnect_at else 'DISCONNECTED',
                   'error': '', 'incoming_count': 0, 'outgoing_count': 0,
                   'in_seq': 0, 'out_seq': 0, 'last_message': '',
                   'last_heartbeat': '', 'last_logon': ''}
            if session:
                with session.state.lock:
                    for key in ('status', 'error', 'incoming_count', 'outgoing_count',
                                'in_seq', 'out_seq', 'last_message', 'last_heartbeat', 'last_logon'):
                        row[key] = getattr(session.state, key, '')
                row['error'] = self._redact(row['error'])
            sessions.append(row)
        with self._activity_lock:
            activity = list(reversed(self._activity))
        return {'venue': self.venue.name, 'environment': self.venue.environment,
                'state': self.state().value, 'text': self.state_text(),
                'sessions': sessions, 'activity': activity,
                'reconnect_in': max(0, round(self._reconnect_at - time.monotonic())) if self._reconnect_at else 0}

    def subscribe(self, contract):
        """Register a configured contract with TT's existing MD parser.

        The FIX session is asynchronous, so registration is deliberately
        separate from sending V.  ``ManualTerminal.poll`` sends the request
        as soon as Market Data is logged on (and re-sends it after reconnect).
        """
        security_id = str(getattr(contract, 'security_id', '') or '').strip()
        symbol = str(getattr(contract, 'symbol', '') or '').strip()
        exchange = str(getattr(contract, 'security_exchange', '') or '').strip()
        if not security_id or not symbol or not exchange:
            return None
        with self.terminal.lock:
            instrument = {
                'security_id': security_id, 'symbol': symbol,
                'exchange': exchange, 'description': getattr(contract, 'name', symbol),
                'full_depth': False,
            }
            self._contract_security_ids[contract.key] = security_id
            self.terminal.watch[security_id] = instrument
            self.terminal.catalogue[security_id] = copy.deepcopy(instrument)
            self.terminal._save('watch', security_id, instrument)
            session = self._sessions.get('Market Data')
            if (session is not None and session.state.status == 'CONNECTED'
                    and session.is_running()):
                self.terminal._subscribe(security_id)
        return None

    def top_of_book(self, key):
        security_id = self._contract_security_ids.get(key, key)
        with self.terminal.lock:
            book = self.terminal.books.get(security_id)
            if not book or book.get('bid') is None or book.get('ask') is None:
                return None
            stamp = book.get('timestamp')
            try:
                ts = datetime.fromisoformat(stamp) if stamp else None
            except (TypeError, ValueError):
                ts = None
            return BookTop(bid=book.get('bid'), ask=book.get('ask'),
                           bid_size=book.get('bid_size'), ask_size=book.get('ask_size'),
                           ts=ts)

    def security_definition(self, contract):
        return None

    def margin_for(self, key, qty):
        return None

    def send(self, order):
        raise NotImplementedError(self.NOT_WIRED)

    def cancel(self, clordid):
        raise NotImplementedError(self.NOT_WIRED)

    def amend(self, clordid, price=None, qty=None):
        raise NotImplementedError(self.NOT_WIRED)

    def orders(self):
        return None

    def positions(self):
        return None

    def drain_events(self):
        if self._reconnect_at is not None and time.monotonic() >= self._reconnect_at:
            self._reconnect_at = None
            self.start()
        self.terminal.poll()
        return []

    def diagnose(self):
        return [{'check': 'TT FIX sessions', 'ok': self.state() == SessionState.LOGGED_ON,
                 'detail': self.state_text(),
                 'fix': self._connection_fix()},
                {'check': 'Execution', 'ok': False, 'detail': self.NOT_WIRED,
                 'fix': 'Execution and reconciliation require a separate integration.'}]

    def _connection_fix(self):
        """Give the operator the remedy for the observed connection failure.

        A WinError 10013 is raised by Windows while opening the TCP socket.
        No FIX Logon has been put on the wire at that point, so suggesting a
        password change is both misleading and delays the actual repair.
        """
        errors = ' '.join(str(session.state.error) for session in self._sessions.values()).lower()
        if 'winerror 10013' in errors or 'access permissions' in errors:
            return ('Windows is blocking outbound TCP before FIX Logon. Allow the Python '
                    'executable through the firewall/endpoint security for TT UAT ports '
                    '11502 (Order Routing) and 11503 (Market Data), or use a network that '
                    'permits those ports. Credentials are not involved in this error.')
        if 'timed out' in errors:
            return ('TT did not respond. Check VPN/proxy/firewall access to the configured '
                    'TT UAT hosts and ports, then verify TT session provisioning.')
        return 'Check TT session provisioning and credentials in .env.'


# Native FIX session implementation extracted from backup_v1fixapp.py.
import os
from collections import deque
import socket
import ssl
import threading
import time
from types import SimpleNamespace

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
        self.svc.audit.write(level='INFO', category='FIX Session', session=self.session_name,
                             direction='LOCAL', event='Connection attempt', sequence='',
                             details={'host': self.cfg['host'], 'port': self.cfg['port']})

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
        if msg_type not in ("A", "0", "1", "5"):
            if msg_type not in self.cfg.get('allowed_messages', ()):
                raise NotImplementedError('This message is not enabled on this FIX session')
            if msg_type in ('D', 'F', 'G') and not dict(fields).get('11', '').startswith('FTM-'):
                raise NotImplementedError('Use a reviewed manual order ticket')
            if self.state.status != 'CONNECTED':
                raise ConnectionError('FIX session is not logged on')
        with self.send_lock:
            with self.state.lock:
                self.state.out_seq += 1
                seq = self.state.out_seq
                self.state.outgoing_count += 1
                self.state.last_message = datetime.now(timezone.utc).isoformat()
            header = [("35", msg_type), ("34", str(seq)), ("49", self.cfg["sender_comp_id"]),
                      ("52", fix_timestamp()), ("56", self.cfg["target_comp_id"])]
            if self.cfg.get("target_sub_id"):
                header.append(("57", self.cfg["target_sub_id"]))
            if self.cfg.get("on_behalf_of_comp_id"):
                header.append(("115", self.cfg["on_behalf_of_comp_id"]))
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
            if self.cfg.get("use_tls"):
                self.socket = ssl.create_default_context().wrap_socket(
                    self.socket, server_hostname=self.cfg["host"])
            self.socket.settimeout(1)
            with self.state.lock:
                self.state.sender_comp_id = self.cfg["sender_comp_id"]
                self.state.target_comp_id = self.cfg["target_comp_id"]
                self.state.out_seq = 0
                self.state.in_seq = 0
            if self.stop_event.is_set():
                return
            password = self.cfg["password"]
            self.send("A", [("98", "0"), ("108", str(self.cfg.get("heartbeat", 30))),
                            ("95", str(len(password.encode("utf-8")))), ("96", password), ("141", "Y")])
            buffer = ""
            heartbeat_seconds = max(5, int(self.cfg.get("heartbeat", 30)))
            logon_deadline = time.monotonic() + 20
            self.last_receive_monotonic = time.monotonic()
            while not self.stop_event.is_set():
                try:
                    data = self.socket.recv(8192)
                    if not data:
                        raise ConnectionError("TT closed the FIX socket")
                    buffer += data.decode("ascii", errors="replace")
                    while f"{SOH}10=" in buffer:
                        end_marker = buffer.find(SOH, buffer.index(f"{SOH}10=") + 4)
                        if end_marker < 0:
                            break
                        end = end_marker + 1
                        raw, buffer = buffer[:end], buffer[end:]
                        self._incoming(raw)
                        self.last_receive_monotonic = time.monotonic()
                except socket.timeout:
                    pass

                # FIX requires the initiator to send heartbeats when it has
                # been idle for the negotiated interval.  Without this, TT
                # will close an otherwise valid session after logon.
                if time.monotonic() - self.last_send_monotonic >= heartbeat_seconds:
                    self.send("0", [])

                if self.state.status == "CONNECTING" and time.monotonic() >= logon_deadline:
                    raise TimeoutError("TT did not answer the Logon request within 20 seconds")
                if time.monotonic() - self.last_receive_monotonic > heartbeat_seconds * 2 + 5:
                    raise TimeoutError("TT heartbeat timed out; reconnect the session")
        except Exception as exc:  # show the actual server/network reason in UI
            self.svc.audit.write(level='ERROR', category='Errors', session=self.session_name,
                                 direction='LOCAL', event='Connection failed', sequence='',
                                 details={'error': self.svc._redact(str(exc))})
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = self.svc._redact(str(exc))
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
        checksum_start = raw.rfind(f"{SOH}10=") + 1
        body_start = raw.find(SOH, raw.find(SOH) + 1) + 1
        if (fields.get('8') != 'FIX.4.2'
                or int(fields.get('9', '-1')) != checksum_start - body_start
                or int(fields.get('10', '-1')) != sum(raw[:checksum_start].encode('ascii')) % 256):
            raise ValueError('Invalid FIX message length or checksum')
        if (fields.get('49') != self.cfg['target_comp_id']
                or fields.get('56') != self.cfg['sender_comp_id']):
            raise ValueError('Incoming FIX CompIDs do not match the configured session')
        msg_type, seq = fields.get("35", "?"), fields.get("34", "?")
        if not seq.isdigit() or int(seq) != self.state.in_seq + 1:
            expected = self.state.in_seq + 1
            raise ConnectionError(
                f'FIX sequence mismatch on {msg_type}: expected {expected}, received {seq}. '
                'Session stopped; verify order status in TT before reconnecting.')
        with self.state.lock:
            self.state.incoming_count += 1
            self.state.in_seq = int(seq) if seq.isdigit() else self.state.in_seq
            self.state.last_message = datetime.now(timezone.utc).isoformat()
        self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
        if msg_type in ('d', 'W', 'X', 'Y', 'j', '3', '8', '9'):
            self.svc.terminal.on_message(self.session_name, fields, raw)
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
        elif msg_type in ("2", "3", "4"):
            with self.state.lock:
                self.state.status = "ERROR"
                self.state.error = fields.get("58", "Session recovery requires reconnect with TT-approved sequence reset")
            self.stop_event.set()
        elif msg_type == "8" and self.on_execution_report:
            self.on_execution_report(fields, raw)
        elif msg_type in ("W", "X") and self.on_market_data:
            self.on_market_data(fields, raw)
        elif msg_type == "Y" and self.on_market_data_reject:
            self.on_market_data_reject(fields, raw)
        elif msg_type == "d" and self.on_security_definition:
            self.on_security_definition(fields, raw)
