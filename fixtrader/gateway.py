"""Venue protocol and TT UAT connection adapter.

The native FIX 4.2 session is extracted from backup_v1fixapp.py. Instrument,
quote and reviewed manual UAT order workflows live in ManualTerminal.
The strategy protocol still reports unknown account positions/orders as None.
"""

import copy
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable

from .models import (BookTop, Fill, GatewayEvent, OrderRequest, OrderState,
                     SecurityDef, SessionState, VenueOrder, VenuePosition)
from .manual_terminal import ManualTerminal
from .fix_audit import FixAuditLog
from .algo_feed import AlgoDataFeed, MarketDataEvent

logger = logging.getLogger(__name__)

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
    #: The Algo's orders go to TT over Order Routing (`AlgoOrderRouter`).
    #: Whether they are SENT is the engine's choice: PAPER after every
    #: restart, LIVE only when armed and confirmed.
    connection_only = False

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
        self.algo_feed = AlgoDataFeed()
        self.terminal = ManualTerminal(self, manual_path)
        self.algo = AlgoOrderRouter(self)

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
                cfg['allowed_messages'] = (('c', 'V') if name == 'Market Data'
                                           else ('D', 'F', 'G', 'AN'))
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
            self.algo_feed.clear()
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
        if not security_id or not symbol:
            return None
        with self.terminal.lock:
            # TT's own definition WINS. This used to replace it with a stub —
            # symbol, id, exchange — so a contract on the algo desk lost its
            # tick size, type and expiry on the Instruments page, and its
            # ladder waited for ever for "a valid tick size". The stub only
            # fills what TT has not said; the contract's configured tick size
            # stands in until TT's definition is seen again.
            known = (self.terminal.watch.get(security_id)
                     or self.terminal.catalogue.get(security_id) or {})
            instrument = copy.deepcopy(known)
            for field, value in (
                    ('security_id', security_id), ('symbol', symbol),
                    ('exchange', exchange),
                    ('description', getattr(contract, 'name', '') or symbol),
                    ('security_type', ''), ('maturity', ''), ('legs', []),
                    ('parameters', {}), ('full_depth', False)):
                if instrument.get(field) in (None, ''):
                    instrument[field] = value
            tick = getattr(contract, 'tick_size', None)
            if not instrument.get('tick_size') and tick:
                instrument['tick_size'] = format(tick, 'g')
            self.terminal._enrich(instrument)
            if not instrument.get('tick_value') and getattr(contract, 'tick_value', None):
                instrument['tick_value'] = format(contract.tick_value, 'g')
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
        md = self._sessions.get('Market Data')
        if md is None or md.state.status != 'CONNECTED' or not md.is_running():
            return None
        security_id = self._contract_security_ids.get(key, key)
        with self.terminal.lock:
            book = self.terminal.books.get(security_id)
            # One side alone IS returned: it is what TT is publishing, and
            # the window has to show it rather than a blank that looks like
            # no subscription. It is not a usable book — BookTop.usable is
            # False, so it gives no mid, no statistic and no entry.
            if (not book or book.get('integrity_ok') is False
                    or (book.get('bid') is None and book.get('ask') is None)):
                return None
            stamp = book.get('timestamp')
            try:
                ts = datetime.fromisoformat(stamp) if stamp else None
            except (TypeError, ValueError):
                ts = None
            return BookTop(bid=book.get('bid'), ask=book.get('ask'),
                           bid_size=book.get('bid_size'), ask_size=book.get('ask_size'),
                           ts=ts)

    def on_market_book(self, security_id, book):
        """Called after the existing FIX parser updates a book, under its lock."""
        for key, mapped_id in self._contract_security_ids.items():
            if mapped_id != security_id:
                continue
            stamp = book.get('timestamp')
            try:
                received_at = datetime.fromisoformat(stamp) if stamp else utcnow()
            except (TypeError, ValueError):
                received_at = utcnow()
            event = MarketDataEvent(
                contract_key=key, security_id=security_id,
                symbol=self.terminal.watch.get(security_id, {}).get('symbol', ''),
                bid=book.get('bid'), ask=book.get('ask'), last=book.get('last'),
                bid_size=book.get('bid_size'), ask_size=book.get('ask_size'),
                received_at=received_at, sequence=str(book.get('fix_sequence') or ''))
            if self.algo_feed.publish(event):
                logger.debug('normalized FIX quote %s seq=%s bid=%s ask=%s last=%s',
                             key, event.sequence, event.bid, event.ask, event.last)

    def drain_market_data(self):
        return self.algo_feed.drain()

    def security_definition(self, contract):
        return None

    def margin_for(self, key, qty):
        return None

    def send(self, order):
        return self.algo.send(order)

    def cancel(self, clordid):
        self.algo.cancel(clordid)

    def amend(self, clordid, price=None, qty=None):
        self.algo.amend(clordid, price=price, qty=qty)

    def adopt(self, row):
        return self.algo.adopt(row)

    def orders(self):
        # TT's list of working orders is not read on this session: unknown,
        # which is NOT "none". Our own are swept by their recorded ids.
        return None

    def positions(self):
        return self.algo.positions()

    def positions_status(self):
        return self.algo.positions_status()

    def drain_events(self):
        if self._reconnect_at is not None and time.monotonic() >= self._reconnect_at:
            self._reconnect_at = None
            self.start()
        self.terminal.poll()
        self.algo.request_positions(time.monotonic())
        return self.algo.drain()

    def diagnose(self):
        state = self.state()
        # CONNECTING is a handshake in progress, not a failure: the Logon is
        # on the wire and TT has up to 20 s to answer it. Reporting it as FAIL
        # with "check your credentials" sent the operator after a password
        # that had not been refused.
        pending = state == SessionState.CONNECTING
        return [{'check': 'TT FIX sessions', 'ok': state == SessionState.LOGGED_ON,
                 'pending': pending,
                 'detail': self.state_text(),
                 'fix': ('Logon sent — waiting for TT to answer (up to 20 seconds). '
                         'This page keeps checking; nothing has failed yet.')
                        if pending else self._connection_fix()},
                {'check': 'Positions', 'ok': self.algo.positions_status()['status'] == 'complete',
                 'detail': (self.algo.positions_status()['why']
                            or self.algo.positions_status()['status']),
                 'fix': ('TT positions could not be read on this session: arming the '
                         'Algo LIVE asks you to confirm that this book\'s own fills '
                         'are the record.')}]

    def _connection_fix(self):
        """Give the operator the remedy for the observed connection failure.

        A WinError 10013 is raised by Windows while opening the TCP socket.
        No FIX Logon has been put on the wire at that point, so suggesting a
        password change is both misleading and delays the actual repair.
        """
        errors = ' '.join(str(session.state.error) for session in self._sessions.values()).lower()
        if 'sequence mismatch' in errors:
            return ('TT FIX sequence numbers are out of sync. Do not reconnect or reset them '
                    'until TT confirms the Order Routing sequence/reset procedure and all '
                    'working orders and fills have been reconciled. The gateway cannot '
                    'safely skip a missing order-session message.')
        if 'delayed logon processing' in errors:
            return ('TT rejected the Order Routing logon as delayed. Verify the host clock is '
                    'synchronized, then ask TT to confirm the session latency limit before '
                    'retrying the logon.')
        if 'winerror 10013' in errors or 'access permissions' in errors:
            return ('Windows is blocking outbound TCP before FIX Logon. Allow the Python '
                    'executable through the firewall/endpoint security for TT UAT ports '
                    '11502 (Order Routing) and 11503 (Market Data), or use a network that '
                    'permits those ports. Credentials are not involved in this error.')
        if 'already connected' in errors:
            return ('TT already has these sessions logged on from somewhere else — '
                    'usually another copy of this program still running (an older '
                    'start.py window, or a second PC using the same .env). Close it, '
                    'then press Connect. If nothing else is running, the last run was '
                    'closed without logging out and TT still holds the session: wait '
                    'about a minute for TT to drop it, then Connect. The credentials '
                    'are fine — TT recognised them.')
        if 'did not answer the logon' in errors:
            return ('TT accepted the connection but never answered the Logon. TT usually '
                    'stays silent when it does not recognise the session: confirm the '
                    'SenderCompID / TargetCompID and the password in .env match what TT '
                    'provisioned for this session, and that the session is enabled.')
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
            # Only OUR orders go out: a reviewed manual ticket (FTM-) or the
            # Algo's own (FT-). Nothing else may be sent on this session.
            if msg_type in ('D', 'F', 'G') and not dict(fields).get('11', '').startswith(
                    ('FTM-', CLORDID_PREFIX + '-')):
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
            # A sequenced Logout still carries the server's actual rejection
            # reason in tag 58. Keep that evidence before reporting the gap;
            # previously the strict sequence check hid the most useful clue.
            remote_reason = fields.get('58', '') if msg_type == '5' else ''
            if msg_type == '5':
                self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
            detail = (f' TT Logout reason: {remote_reason}' if remote_reason else '')
            raise ConnectionError(
                f'FIX sequence mismatch on {msg_type}: expected {expected}, received {seq}. '
                f'Session stopped; verify order status in TT before reconnecting.{detail}')
        with self.state.lock:
            self.state.incoming_count += 1
            self.state.in_seq = int(seq) if seq.isdigit() else self.state.in_seq
            self.state.last_message = datetime.now(timezone.utc).isoformat()
        self.svc.log_fix(self.session_name, "IN", msg_type, seq, raw)
        if msg_type in ('d', 'W', 'X', 'Y', 'j', '3', '8', '9'):
            self.svc.terminal.on_message(self.session_name, fields, raw)
        algo = getattr(self.svc, 'algo', None)
        if (algo is not None and self.session_name == 'Order Routing'
                and msg_type in ('8', '9', 'j', '3', 'AO', 'AP')):
            algo.on_message(msg_type, fields, raw)
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
        elif msg_type == "3" and fields.get("372") == "AN":
            # TT refusing our positions request is an ANSWER — positions
            # unknown — not a broken session. Every other Reject still stops
            # the session as before.
            pass
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


# ---------------------------------------------------------------------------
# The Algo's orders to TT, over the Order Routing session
# ---------------------------------------------------------------------------

#: FIX 4.2 OrdType / TimeInForce for what the executor sends.
_TT_ORD_TYPE = {'MARKET': '1', 'LIMIT': '2'}
_TT_TIF = {'DAY': '0', 'GTC': '1', 'IOC': '3'}
#: OrdStatus (39) -> our order state.
_ORD_STATUS = {'0': 'WORKING', '1': 'PARTIAL', '2': 'FILLED', '3': 'EXPIRED',
               '4': 'CANCELLED', '5': 'WORKING', '8': 'REJECTED',
               'C': 'EXPIRED', 'A': 'PENDING', '6': None, 'E': None}
#: How long TT has to answer a positions request before it is UNAVAILABLE.
POSITIONS_TIMEOUT_SEC = 15.0


def _fix_number(value: float) -> str:
    """A price or quantity as FIX wants it: no exponent, no float noise."""
    text = format(round(float(value), 10), 'f').rstrip('0').rstrip('.')
    return text if text not in ('', '-0') else '0'


class AlgoOrderRouter:
    """The Algo's orders to TT: New Order Single, Cancel, Cancel/Replace, and
    the execution reports, cancel rejects and business rejects that come back
    for them — folded into `GatewayEvent`s that carry a SNAPSHOT of the
    order, never the live object.

    Every order is ours by its ClOrdID (`FT-…`); anything else on the
    session — a manual ticket (`FTM-…`), a hand order in TT — is not read
    here and is never touched. A close says it is a close (77=C), carries
    the position it closes and that position's tickets (58), and is capped
    by the executor at what is open. Orders go with TT's cancel-on-disconnect
    so a dropped engine does not leave them working.

    Positions: a Request For Positions (AN) at each logon. If TT answers
    (AO / AP), `positions()` is the account's book; if TT rejects it or does
    not answer, it is None — unknown, which is NOT flat — with the reason.
    """

    def __init__(self, gateway):
        self.gw = gateway
        self.lock = threading.RLock()
        self.orders: Dict[str, Dict[str, Any]] = {}     # root id -> record
        self.ids: Dict[str, str] = {}                    # any id -> root id
        self.events: List[GatewayEvent] = []
        self.exec_ids = set()
        self.seq = 0
        # Unique across restarts: TT refuses a ClOrdID it has seen today.
        self.prefix = f"{CLORDID_PREFIX}-{int(time.time() * 1000):x}"
        self.pos = {'status': 'not requested', 'why': None, 'req_id': None,
                    'sent_at': None, 'expected': None, 'reports': {},
                    'logon': None}
        #: The FILLS TAPE: every execution report on this session that
        #: carries a fill, ours or not, as TT sent it. For the Account page's
        #: Fills tab only — it is never applied to the book (`_execution`
        #: does that, for OUR ids alone).
        self.tape: List[Dict[str, Any]] = []
        self.tape_ids = set()

    # -- ids -----------------------------------------------------------------

    def _next_id(self) -> str:
        self.seq += 1
        return f"{self.prefix}-{self.seq}"

    def owns(self, clordid: Optional[str]) -> bool:
        return bool(clordid) and clordid in self.ids

    # -- sending -------------------------------------------------------------

    def _session(self):
        session = self.gw._sessions.get('Order Routing')
        if (session is None or session.state.status != 'CONNECTED'
                or not session.is_running()):
            return None
        return session

    def _instrument(self, contract_key):
        security_id = self.gw._contract_security_ids.get(contract_key)
        if not security_id:
            return None
        terminal = self.gw.terminal
        return (terminal.watch.get(security_id)
                or terminal.catalogue.get(security_id))

    def _emit(self, kind, rec, text='', fill=None):
        from dataclasses import replace
        vo = rec['order']
        self.events.append(GatewayEvent(
            kind=kind, contract_key=vo.contract_key, clordid=vo.clordid,
            text=text, order=replace(vo), fill=fill, ts=utcnow()))

    def _refuse(self, rec, text):
        rec['order'].state = OrderState.REJECTED
        rec['order'].text = text
        self._emit('REJECTED', rec, text)

    def send(self, order: OrderRequest) -> str:
        with self.lock:
            clordid = self._next_id()
            vo = VenueOrder(clordid=clordid, contract_key=order.contract_key,
                            side=order.side, qty=order.qty, price=order.price,
                            order_type=order.order_type,
                            state=OrderState.PENDING, ts=utcnow())
            rec = {'order': vo, 'current': clordid, 'pending': None,
                   'request': order, 'security_id': None}
            self.orders[clordid] = rec
            self.ids[clordid] = clordid
            instrument = self._instrument(order.contract_key)
            account = str(getattr(self.gw.venue, 'account', '') or '')
            session = self._session()
            why = (None if instrument else
                   f'{order.contract_key} has no TT security id on this '
                   f'session — subscribe it before the Algo can trade it')
            why = why or (None if account else
                          'no TT account is set on the venue (tag 1)')
            why = why or (None if session else
                          'Order Routing FIX is not connected')
            why = why or (None if order.order_type.value in _TT_ORD_TYPE
                          else f'{order.order_type.value} is not an Algo '
                               f'order type here')
            if why:
                self._refuse(rec, why)
                return clordid
            rec['security_id'] = instrument['security_id']
            rec['instrument'] = instrument
            try:
                session.send('D', self._new_order_fields(rec, clordid))
            except Exception as e:                   # noqa: BLE001
                self._refuse(rec, f'not sent: {self.gw._redact(str(e))}')
            return clordid

    def _order_fields(self, rec, qty, price):
        order = rec['request']
        tif = getattr(order.tif, 'value', order.tif) or 'DAY'
        fields = [('1', str(self.gw.venue.account))]
        fields += self.gw.terminal.instrument_fields(rec['instrument'])
        fields += [('54', '1' if order.side.value == 'BUY' else '2'),
                   ('38', _fix_number(qty)),
                   ('40', _TT_ORD_TYPE[order.order_type.value]),
                   ('59', _TT_TIF.get(str(tif), '0')),
                   ('60', fix_timestamp()),
                   # An AUTOMATED order: the exchange's manual-order flag
                   # says no. A manual ticket says Y.
                   ('1028', 'N'),
                   # OPEN or CLOSE, always said. An unknown effect is a
                   # close — never an open.
                   ('77', 'O' if not order.position_effect.is_close else 'C')]
        if order.order_type.value == 'LIMIT' and price is not None:
            fields.append(('44', _fix_number(price)))
        if str(tif) != 'GTC':
            # TT cancels it if this session drops: a dead engine leaves no
            # Algo order working.
            fields.append(('18', 'o 2'))
        return fields

    def _new_order_fields(self, rec, clordid):
        order = rec['request']
        fields = [('11', clordid)] + self._order_fields(rec, order.qty,
                                                        order.price)
        text = order.reason or ''
        if order.position_effect.is_close:
            # The position it closes, and that position's venue tickets.
            refs = ' '.join(order.close_tickets[-6:])
            text = (f"Close P{order.position_id or '?'} {refs}").strip()
        if text:
            fields.append(('58', text[:60]))
        return fields

    def cancel(self, clordid: str) -> None:
        with self.lock:
            rec = self.orders.get(self.ids.get(clordid, ''))
            if rec is None or rec['order'].state.is_done or rec['pending']:
                return
            session = self._session()
            if session is None or 'instrument' not in rec:
                return
            new_id = self._next_id()
            self.ids[new_id] = rec['order'].clordid
            order = rec['request']
            fields = [('11', new_id), ('41', rec['current'])]
            if rec['order'].venue_id:
                fields.append(('37', rec['order'].venue_id))
            fields += [('1', str(self.gw.venue.account))]
            fields += self.gw.terminal.instrument_fields(rec['instrument'])
            fields += [('54', '1' if order.side.value == 'BUY' else '2'),
                       ('38', _fix_number(rec['order'].qty)),
                       ('60', fix_timestamp())]
            rec['pending'] = {'id': new_id, 'kind': 'CANCEL'}
            try:
                session.send('F', fields)
            except Exception as e:                   # noqa: BLE001
                rec['pending'] = None
                self._emit('CANCEL_REJECTED', rec,
                           f'cancel not sent: {self.gw._redact(str(e))}')

    def amend(self, clordid: str, price: Optional[float] = None,
              qty: Optional[float] = None) -> None:
        """Cancel/Replace: the order keeps its lineage and its root id."""
        with self.lock:
            rec = self.orders.get(self.ids.get(clordid, ''))
            if rec is None or rec['order'].state.is_done or rec['pending']:
                return
            session = self._session()
            if session is None or 'instrument' not in rec:
                return
            new_id = self._next_id()
            self.ids[new_id] = rec['order'].clordid
            vo = rec['order']
            fields = [('11', new_id), ('41', rec['current'])]
            if vo.venue_id:
                fields.append(('37', vo.venue_id))
            fields += self._order_fields(
                rec, qty if qty is not None else vo.qty,
                price if price is not None else vo.price)
            rec['pending'] = {'id': new_id, 'kind': 'REPLACE'}
            try:
                session.send('G', fields)
            except Exception as e:                   # noqa: BLE001
                rec['pending'] = None
                self._emit('CANCEL_REJECTED', rec,
                           f'replace not sent: {self.gw._redact(str(e))}')

    def adopt(self, row: Dict[str, Any]) -> bool:
        """An order of ours recorded as working when the engine last ran —
        taken back under management so it can be cancelled, and so a fill
        for it is applied to the book it belongs to."""
        clordid = str(row.get('clordid') or '')
        if not clordid.startswith(CLORDID_PREFIX + '-') or clordid in self.ids:
            return False
        from .models import Intent, OrderType as OT, PositionEffect, Side as S
        with self.lock:
            request = OrderRequest(
                contract_key=row.get('contract_key') or '',
                side=S(row.get('side') or 'BUY'), qty=float(row.get('qty') or 0),
                order_type=OT(row.get('order_type') or 'MARKET'),
                intent=Intent(row.get('intent') or 'OPEN'),
                price=row.get('price'),
                position_effect=(PositionEffect.CLOSE
                                 if (row.get('intent') or '') == 'CLOSE'
                                 else PositionEffect.OPEN))
            vo = VenueOrder(clordid=clordid, contract_key=request.contract_key,
                            side=request.side, qty=request.qty,
                            filled_qty=float(row.get('filled_qty') or 0),
                            price=request.price, order_type=request.order_type,
                            state=OrderState.WORKING, ts=utcnow())
            rec = {'order': vo, 'current': clordid, 'pending': None,
                   'request': request, 'adopted': True}
            instrument = self._instrument(request.contract_key)
            if instrument:
                rec['instrument'] = instrument
                rec['security_id'] = instrument['security_id']
            self.orders[clordid] = rec
            self.ids[clordid] = clordid
            return True

    # -- what TT sends back --------------------------------------------------

    def on_message(self, msg_type: str, fields: Dict[str, str], raw: str):
        with self.lock:
            if msg_type == '8':
                self._record_fill(fields)
                self._execution(fields)
            elif msg_type == '9':
                self._cancel_reject(fields)
            elif msg_type in ('j', '3'):
                self._reject(fields)
            elif msg_type == 'AO':
                self._positions_ack(fields)
            elif msg_type == 'AP':
                self._position_report(fields, raw)

    def _record_fill(self, f) -> None:
        """One fill on the tape, in TT's own fields: 60 TransactTime, 1
        Account, 48/55 instrument, 54 side, 77 Open/Close, 32/31 LastQty/
        LastPx, 14/151 cum/leaves, 37 OrderID, 17 ExecID, 11 ClOrdID, 58."""
        if f.get('150') not in ('1', '2', 'F'):
            return
        exec_id = f.get('17')
        try:
            qty = float(f.get('32') or 0)
            price = float(f.get('31'))
        except (TypeError, ValueError):
            return
        if not exec_id or qty <= 0 or exec_id in self.tape_ids:
            return
        self.tape_ids.add(exec_id)
        clordid = f.get('11') or ''
        sid = f.get('48') or ''
        contract = next((c for c in self.gw.contracts
                         if str(getattr(c, 'security_id', '') or '') == sid), None)
        self.tape.append({
            'exec_id': exec_id, 'clordid': clordid,
            'orig_clordid': f.get('41') or '',
            'order_id': f.get('37') or '',
            'tt_time': f.get('60') or '', 'account': f.get('1') or '',
            'security_id': sid, 'symbol': f.get('55') or '',
            'contract_key': getattr(contract, 'key', '') if contract else '',
            'side': {'1': 'BUY', '2': 'SELL'}.get(f.get('54'), f.get('54') or ''),
            'open_close': {'O': 'OPEN', 'C': 'CLOSE', 'F': 'FIFO'}.get(
                f.get('77'), f.get('77') or ''),
            'qty': qty, 'price': price,
            'cum_qty': float(f['14']) if f.get('14') else None,
            'leaves_qty': float(f['151']) if f.get('151') else None,
            'exec_type': f.get('150'), 'ord_status': f.get('39') or '',
            'text': self.gw._redact(f.get('58', '')),
            'ours': ('ALGO' if clordid.startswith(CLORDID_PREFIX + '-') else
                     'MANUAL' if clordid.startswith('FTM-') else ''),
            'received': utcnow().isoformat(),
        })

    def take_tape(self) -> List[Dict[str, Any]]:
        """The fills recorded since the last call, for the engine to keep."""
        with self.lock:
            out, self.tape = self.tape, []
        return out

    def _rec_for(self, fields):
        for tag in ('11', '41'):
            root = self.ids.get(fields.get(tag) or '')
            if root:
                return self.orders.get(root)
        return None

    def _execution(self, f):
        rec = self._rec_for(f)
        if rec is None:
            return                      # not ours: a manual ticket, a hand order
        vo = rec['order']
        vo.venue_id = f.get('37') or vo.venue_id
        exec_type = f.get('150', '')
        state = _ORD_STATUS.get(f.get('39', ''))
        if state:
            vo.state = OrderState(state)
        if f.get('14') not in (None, ''):
            vo.filled_qty = float(f['14'])
        vo.text = self.gw._redact(f.get('58', '')) or vo.text
        vo.ts = utcnow()
        if exec_type == '5':                           # replaced
            rec['current'] = f.get('11') or rec['current']
            if f.get('44'):
                vo.price = float(f['44'])
            if f.get('38'):
                vo.qty = float(f['38'])
        if exec_type in ('4', '5', '8') and rec['pending']:
            rec['pending'] = None
        if exec_type in ('1', '2', 'F'):
            if f.get('151') == '0' and not f.get('39'):
                vo.state = OrderState.FILLED       # nothing left: filled
            exec_id = f.get('17')
            qty = float(f.get('32') or 0)
            if not exec_id or exec_id in self.exec_ids or qty <= 0:
                return
            self.exec_ids.add(exec_id)
            venue_ts = None
            try:
                venue_ts = datetime.strptime(f.get('60', ''),
                                             '%Y%m%d-%H:%M:%S.%f').replace(
                                                 tzinfo=timezone.utc)
            except ValueError:
                pass
            fill = Fill(venue=getattr(self.gw.venue, 'name', 'TT'),
                        exec_id=exec_id, clordid=vo.clordid,
                        contract_key=vo.contract_key, side=vo.side, qty=qty,
                        price=float(f.get('31')), our_ts=utcnow(),
                        venue_ts=venue_ts)
            self._emit('FILL' if vo.state is OrderState.FILLED else 'PARTIAL',
                       rec, f"filled {qty:g} @ {fill.price:g}", fill)
            return
        kind = {'0': 'ACK', '4': 'CANCELLED', '5': 'REPLACED', '8': 'REJECTED',
                'C': 'EXPIRED', '3': 'EXPIRED'}.get(exec_type)
        if kind:
            self._emit(kind, rec, vo.text or kind.lower())

    def _cancel_reject(self, f):
        rec = self._rec_for(f)
        if rec is None:
            return
        rec['pending'] = None
        state = _ORD_STATUS.get(f.get('39', ''))
        if state:
            rec['order'].state = OrderState(state)
        self._emit('CANCEL_REJECTED', rec,
                   self.gw._redact(f.get('58', 'TT refused the change')))

    def _reject(self, f):
        """A business (j) or session (3) reject: ours if it names one of our
        ids — or our positions request."""
        ref = f.get('379') or ''
        if f.get('372') == 'AN' or (ref and ref == self.pos.get('req_id')):
            self._positions_unavailable(
                'TT rejected the positions request: ' +
                self.gw._redact(f.get('58', 'no reason given')))
            return
        root = self.ids.get(ref)
        if root:
            rec = self.orders[root]
            self._refuse(rec, self.gw._redact(f.get('58', 'TT rejected it')))

    # -- positions -------------------------------------------------------------

    def request_positions(self, now_monotonic: float) -> None:
        """Ask TT for the account's positions, once per logon."""
        session = self._session()
        if session is None:
            return
        logon = session.state.last_logon
        with self.lock:
            if self.pos['logon'] == logon:
                if (self.pos['status'] == 'requested' and self.pos['sent_at']
                        and now_monotonic - self.pos['sent_at']
                        > POSITIONS_TIMEOUT_SEC):
                    self._positions_unavailable(
                        'TT did not answer the positions request within '
                        f'{POSITIONS_TIMEOUT_SEC:.0f} s')
                return
            account = str(getattr(self.gw.venue, 'account', '') or '')
            self.pos.update(logon=logon, reports={}, expected=None)
            if not account:
                self._positions_unavailable('no TT account is set on the venue')
                return
            req = self._next_id().replace(CLORDID_PREFIX, 'POS', 1)
            self.pos.update(req_id=req, status='requested', why=None,
                            sent_at=now_monotonic)
            try:
                session.send('AN', [('710', req), ('724', '0'), ('263', '0'),
                                    ('1', account),
                                    ('715', utcnow().strftime('%Y%m%d')),
                                    ('60', fix_timestamp())])
            except Exception as e:                   # noqa: BLE001
                self._positions_unavailable(
                    f'positions request not sent: {self.gw._redact(str(e))}')

    def _positions_unavailable(self, why):
        self.pos.update(status='unavailable', why=why)

    def _positions_ack(self, f):
        if f.get('710') and f.get('710') != self.pos.get('req_id'):
            return
        result = f.get('728', '0')
        if result != '0':
            self._positions_unavailable(
                f'TT answered the positions request with result {result}: '
                + self.gw._redact(f.get('58', '')))
            return
        total = f.get('727')
        self.pos['expected'] = int(total) if total and total.isdigit() else None
        if self.pos['expected'] == 0:
            self.pos.update(status='complete', why=None)

    def _position_report(self, f, raw):
        if f.get('710') and f.get('710') != self.pos.get('req_id'):
            return
        long_qty = short_qty = 0.0
        security_id = None
        for tag, value in _pairs(raw):
            if tag == '48' and security_id is None:
                security_id = value
            elif tag == '704':
                long_qty += float(value or 0)
            elif tag == '705':
                short_qty += float(value or 0)
        if security_id:
            self.pos['reports'][security_id] = (long_qty, short_qty)
        total = f.get('727')
        if self.pos['expected'] is None and total and total.isdigit():
            self.pos['expected'] = int(total)
        if (self.pos['expected'] is not None
                and len(self.pos['reports']) >= self.pos['expected']):
            self.pos.update(status='complete', why=None)

    def positions(self) -> Optional[List[VenuePosition]]:
        with self.lock:
            if self.pos['status'] != 'complete':
                return None
            by_sid = {sid: key for key, sid in
                      self.gw._contract_security_ids.items()}
            out = []
            for sid, (long_qty, short_qty) in self.pos['reports'].items():
                if not long_qty and not short_qty:
                    continue
                out.append(VenuePosition(
                    contract_key=by_sid.get(sid, f'TT:{sid}'),
                    qty=round(long_qty - short_qty, 10),
                    long_qty=long_qty or None, short_qty=short_qty or None))
            return out

    def positions_status(self) -> Dict[str, Any]:
        with self.lock:
            return {'status': self.pos['status'], 'why': self.pos['why']}

    def drain(self) -> List[GatewayEvent]:
        with self.lock:
            out, self.events = self.events, []
        return out


def _pairs(raw: str):
    for item in raw.split(SOH):
        if '=' in item:
            tag, value = item.split('=', 1)
            yield tag, value
