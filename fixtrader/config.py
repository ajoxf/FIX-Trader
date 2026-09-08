"""Venues, contracts and settings in one JSON file.

Credentials never live in that file. Each venue names an environment variable
(`password_env`) holding its password, and the value lives in `.env`,
gitignored, written by the UI. Never in code, never in config, never in an API
response, never in a log line.

Three things in here carry weight beyond "read some settings":

- **Saves go through `atomicfile`.** See that module for the failure it exists
  to prevent.
- **A per-contract field left BLANK means "use the desk default", and 0 is a
  real number.** A loop that skips falsy values can only ever set an override,
  never clear one — and it silently ignores a commission of 0, a budget of 0
  and a limit of 0, all of which are genuine statements.
- **UAT and PROD are separate venues.** Nothing here merges them, and the
  environment is a required field with no default, so a venue saved without
  one is refused rather than assumed to be the safe kind.
"""

import os
import re
from typing import Any, Dict, List, Optional

from . import atomicfile
from .models import OrderType, TargetBasis, TimeInForce

try:
    from dotenv import load_dotenv
except ImportError:                      # optional; the shell can set them
    load_dotenv = None


ENVIRONMENTS = ("UAT", "PROD")
FIX_VERSIONS = ("FIX.4.2", "FIX.4.4", "FIXT.1.1")


# ---------------------------------------------------------------------------
# desk-wide defaults
#
# Every tunable the engine reads is here and is a visible field in the UI:
# a guessed number gets corrected from measurement, which needs it on screen
# first.
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    # -- the loop ---------------------------------------------------------
    #: How often the SCREEN refreshes. The headline requirement, and a
    #: setting rather than a constant so it can be measured against.
    'PRICE_REFRESH_SEC': 0.5,
    #: How often the engine takes a pass. Faster than the screen: the
    #: statistics should not be sampled at the rate a human reads them.
    'ENGINE_POLL_SEC': 0.1,
    #: How often commands are drained, on their own thread. A switch flipped
    #: in the UI must not wait for a price poll.
    'COMMAND_POLL_SEC': 0.02,

    # -- master switches --------------------------------------------------
    #: OFF stands every contract's algo down at once. Deliberately not per
    #: contract: a switch you have to find eight times is one that gets
    #: missed once.
    'ALGO_MASTER_ENABLED': True,
    'CONFIRM_CLOSE': True,
    'SOUND_ENABLED': True,
    #: 'ask' / 'always' / 'never'. An unanswered prompt means NO.
    'SHUTDOWN_CLOSE_POSITIONS': 'ask',

    # -- guards -----------------------------------------------------------
    #: How long a quote may go unchanged before entries are withheld. 0 = off,
    #: which also stops it catching a feed that has genuinely STOPPED.
    'MAX_QUOTE_AGE_SEC': 15.0,
    'MAX_PRICE_JUMP_SIGMA': 5.0,
    'JUMP_SETTLE_SEC': 2.0,
    'REFUSE_OUTSIDE_SESSION': True,

    # -- desk limits ------------------------------------------------------
    'DAILY_MAX_LOSS_TOTAL': 0.0,
    'MAX_OPEN_CONTRACTS': 0.0,

    # -- defaults a blank contract field falls back to --------------------
    'DEFAULT_LOOKBACK': 400,
    'DEFAULT_STATS_UPDATE_INTERVAL_SEC': 300.0,
    'DEFAULT_ENTRY_THRESHOLD': 2.0,
    'DEFAULT_EXIT_THRESHOLD': 0.5,
    'DEFAULT_STOP_LOSS_Z': 4.0,
    'DEFAULT_EXIT_SIGNAL_MODE': 'profit',
    'DEFAULT_MAX_HOLD_MINUTES': 0.0,
    #: OFF by default, deliberately. Hurst is an ESTIMATE, and on a spread
    #: quantised to a coarse tick it reads high however well the series
    #: reverts — runs of identical prices look persistent. Shipped on, with a
    #: 0.5 threshold, it would silently withhold every entry on every
    #: contract while looking like a filter that is merely strict. Read it on
    #: the window for a few sessions, see what THIS contract does, then set a
    #: threshold and turn it on. The edge filter, which is arithmetic rather
    #: than an estimate, is the one that ships armed.
    'DEFAULT_HURST_ENABLED': False,
    'DEFAULT_HURST_THRESHOLD': 0.5,
    'DEFAULT_EDGE_FILTER_ENABLED': True,
    'DEFAULT_MIN_STD_MULTIPLE': 1.5,
    'DEFAULT_HALF_LIFE_ENABLED': False,
    'DEFAULT_MAX_HALF_LIFE': 60.0,
    'DEFAULT_MIN_BOOK_SIZE': 0.0,
    'DEFAULT_MAX_BOOK_SPREAD_TICKS': 0.0,
    'DEFAULT_QUANTITY': 1.0,
    'DEFAULT_MAX_POSITION': 0.0,
    'DEFAULT_MAX_TRADES_PER_DAY': 0.0,
    'DEFAULT_DAILY_MAX_LOSS': 0.0,
    'DEFAULT_ENTRY_COOLDOWN_SECONDS': 60.0,
    'DEFAULT_ENTRY_ORDER_TYPE': OrderType.LIMIT.value,
    'DEFAULT_EXIT_ORDER_TYPE': OrderType.MARKET.value,
    'DEFAULT_ENTRY_LIMIT_OFFSET_TICKS': 1.0,
    'DEFAULT_EXIT_LIMIT_OFFSET_TICKS': 1.0,
    'DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC': 30.0,
    'DEFAULT_EXIT_LIMIT_TIMEOUT_SEC': 30.0,
    #: A missed ENTRY is a trade not taken; a missed EXIT is a position you
    #: still hold. That asymmetry is why these two defaults differ.
    'DEFAULT_ENTRY_ON_TIMEOUT': 'CANCEL',
    'DEFAULT_EXIT_ON_TIMEOUT': 'CROSS_AT_MARKET',
    #: Every amend loses queue position, so re-pricing on every pass
    #: guarantees you are never at the front of a queue.
    'DEFAULT_REPEG_DEAD_BAND_TICKS': 1.0,
    'DEFAULT_TIME_IN_FORCE': TimeInForce.DAY.value,
    #: Which close flag a closing order carries. 'CLOSE' is the plain offset
    #: flag and suits most venues. SHFE and INE price a close-today
    #: differently from a close-yesterday, so those contracts want
    #: 'CLOSE_TODAY', 'CLOSE_YESTERDAY', or 'AUTO' to pick from the trading
    #: day the position was opened on. It is never 'OPEN': an opposite order
    #: that does not say it is closing opens the other side instead.
    'DEFAULT_CLOSE_OFFSET_MODE': 'CLOSE',
    'DEFAULT_COMMISSION_PER_CONTRACT': 0.0,
    'DEFAULT_EXCHANGE_FEE_PER_CONTRACT': 0.0,
    'DEFAULT_CLEARING_FEE_PER_CONTRACT': 0.0,
    #: A BUDGET, not a measurement. Default 0: a fabricated cost is charged
    #: against every trade and the operator cannot tell it was never theirs.
    'DEFAULT_SLIPPAGE_BUDGET_TICKS': 0.0,
    'DEFAULT_PROFIT_TARGET_PCT': 2.0,
    'DEFAULT_PROFIT_TARGET_BASIS': TargetBasis.MARGIN.value,

    # -- notifications ----------------------------------------------------
    'NOTIFY_ORDERS': False,
    'NOTIFY_FILLS': True,
    'NOTIFY_POSITIONS': True,
    'NOTIFY_REJECTS': True,
    'NOTIFY_WITHHELD': True,
    'TELEGRAM_ENABLED': False,
    'TELEGRAM_CHAT_ID': '',

    # -- storage ----------------------------------------------------------
    'DATABASE_PATH': 'fixtrader.db',
    'PERSIST_STATS_SAMPLES': True,
    'EVENT_RETENTION_DAYS': 90,
}

#: Read once at startup. Changing one needs a restart and must SAY so;
#: warning "restart" on every save teaches the operator to ignore the line
#: that matters.
STRUCTURAL_SETTINGS = ('PRICE_REFRESH_SEC', 'ENGINE_POLL_SEC',
                       'DATABASE_PATH')

#: Per-contract field -> the desk-wide default it falls back to when blank.
CONTRACT_DEFAULTS: Dict[str, str] = {
    'lookback': 'DEFAULT_LOOKBACK',
    'stats_update_interval_sec': 'DEFAULT_STATS_UPDATE_INTERVAL_SEC',
    'entry_threshold': 'DEFAULT_ENTRY_THRESHOLD',
    'exit_threshold': 'DEFAULT_EXIT_THRESHOLD',
    'stop_loss_z': 'DEFAULT_STOP_LOSS_Z',
    'exit_signal_mode': 'DEFAULT_EXIT_SIGNAL_MODE',
    'max_hold_minutes': 'DEFAULT_MAX_HOLD_MINUTES',
    'hurst_enabled': 'DEFAULT_HURST_ENABLED',
    'hurst_threshold': 'DEFAULT_HURST_THRESHOLD',
    'edge_filter_enabled': 'DEFAULT_EDGE_FILTER_ENABLED',
    'min_std_multiple': 'DEFAULT_MIN_STD_MULTIPLE',
    'half_life_enabled': 'DEFAULT_HALF_LIFE_ENABLED',
    'max_half_life': 'DEFAULT_MAX_HALF_LIFE',
    'min_book_size': 'DEFAULT_MIN_BOOK_SIZE',
    'max_book_spread_ticks': 'DEFAULT_MAX_BOOK_SPREAD_TICKS',
    'quantity': 'DEFAULT_QUANTITY',
    'max_position': 'DEFAULT_MAX_POSITION',
    'max_trades_per_day': 'DEFAULT_MAX_TRADES_PER_DAY',
    'daily_max_loss': 'DEFAULT_DAILY_MAX_LOSS',
    'entry_cooldown_seconds': 'DEFAULT_ENTRY_COOLDOWN_SECONDS',
    'entry_order_type': 'DEFAULT_ENTRY_ORDER_TYPE',
    'exit_order_type': 'DEFAULT_EXIT_ORDER_TYPE',
    'entry_limit_offset_ticks': 'DEFAULT_ENTRY_LIMIT_OFFSET_TICKS',
    'exit_limit_offset_ticks': 'DEFAULT_EXIT_LIMIT_OFFSET_TICKS',
    'entry_limit_timeout_sec': 'DEFAULT_ENTRY_LIMIT_TIMEOUT_SEC',
    'exit_limit_timeout_sec': 'DEFAULT_EXIT_LIMIT_TIMEOUT_SEC',
    'entry_on_timeout': 'DEFAULT_ENTRY_ON_TIMEOUT',
    'exit_on_timeout': 'DEFAULT_EXIT_ON_TIMEOUT',
    'repeg_dead_band_ticks': 'DEFAULT_REPEG_DEAD_BAND_TICKS',
    'time_in_force': 'DEFAULT_TIME_IN_FORCE',
    'close_offset_mode': 'DEFAULT_CLOSE_OFFSET_MODE',
    'commission_per_contract': 'DEFAULT_COMMISSION_PER_CONTRACT',
    'exchange_fee_per_contract': 'DEFAULT_EXCHANGE_FEE_PER_CONTRACT',
    'clearing_fee_per_contract': 'DEFAULT_CLEARING_FEE_PER_CONTRACT',
    'slippage_budget_ticks': 'DEFAULT_SLIPPAGE_BUDGET_TICKS',
    'profit_target_pct': 'DEFAULT_PROFIT_TARGET_PCT',
    'profit_target_basis': 'DEFAULT_PROFIT_TARGET_BASIS',
}


def env_key_for(name: str) -> str:
    """The .env variable name for a venue. Sanitised, upper case, prefixed."""
    slug = re.sub(r'[^A-Za-z0-9]+', '_', str(name or '')).strip('_').upper()
    return f"FIX_{slug}" if slug else "FIX_UNNAMED"


def _blank_to_none(value: Any) -> Optional[float]:
    """A number from the UI, where blank means "unset" and 0 does not."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _blank_to_none_any(value: Any) -> Any:
    """As above, for a field that is not a number (a mode, a type)."""
    if value is None or value == '':
        return None
    return value


class VenueConfig:
    """One FIX session: one endpoint, one set of comp ids, one environment.

    The password is NOT here. `password_env` names the variable; the value
    lives in `.env` and is read through the `password` property, which is the
    only place in the system that can see it.
    """

    def __init__(self, name: str, environment: str = "UAT",
                 broker: str = "", host: str = "", port: Optional[int] = None,
                 md_host: str = "", md_port: Optional[int] = None,
                 sender_comp_id: str = "", target_comp_id: str = "",
                 sender_sub_id: str = "", target_sub_id: str = "",
                 on_behalf_of_comp_id: str = "",
                 on_behalf_of_sub_id: str = "",
                 md_sender_comp_id: str = "", md_target_comp_id: str = "",
                 dc_host: str = "", dc_port: Optional[int] = None,
                 dc_sender_comp_id: str = "", dc_target_comp_id: str = "",
                 fix_version: str = "FIX.4.4", username: str = "",
                 password_env: str = "", account: str = "",
                 heartbeat_sec: int = 30, reset_seq_on_logon: bool = True,
                 use_tls: bool = True, data_dictionary: str = "",
                 store_path: str = "", log_path: str = "",
                 enabled: bool = True):
        self.name = name
        self.environment = self._environment(environment, name)
        self.broker = broker
        self.host = host
        self.port = int(port) if port else None
        self.md_host = md_host
        self.md_port = int(md_port) if md_port else None
        self.sender_comp_id = sender_comp_id
        self.target_comp_id = target_comp_id
        self.sender_sub_id = sender_sub_id
        self.target_sub_id = target_sub_id
        self.on_behalf_of_comp_id = on_behalf_of_comp_id
        #: Tag 116. NOT tag 115 — a broker that asks for `OnBehalfOfSubID`
        #: and is sent `OnBehalfOfCompID` is a session that logs on and then
        #: rejects every order, which is a slow way to find a typo.
        self.on_behalf_of_sub_id = on_behalf_of_sub_id

        #: Market data is its own session at brokers that split them, with
        #: its OWN comp ids — not the order-routing ones. Blank falls back to
        #: the order session, which is right where a broker runs one session
        #: for both.
        self.md_sender_comp_id = md_sender_comp_id
        self.md_target_comp_id = md_target_comp_id

        #: Drop copy: a read-only feed of everything on the account,
        #: INCLUDING what a person did by hand in the broker's own UI. See
        #: `docs/FIX_NOTES.md` — subscribing to it is a decision, not a
        #: detail, because this system is the algo and a hand trade it did
        #: not send is a position it cannot explain.
        self.dc_host = dc_host
        self.dc_port = int(dc_port) if dc_port else None
        self.dc_sender_comp_id = dc_sender_comp_id
        self.dc_target_comp_id = dc_target_comp_id
        self.fix_version = fix_version
        self.username = username
        self.password_env = password_env or env_key_for(name)
        self.account = account
        self.heartbeat_sec = int(heartbeat_sec or 30)
        self.reset_seq_on_logon = bool(reset_seq_on_logon)
        self.use_tls = bool(use_tls)
        self.data_dictionary = data_dictionary
        self.store_path = store_path
        self.log_path = log_path
        self.enabled = bool(enabled)

    @staticmethod
    def _environment(value: Any, name: str) -> str:
        """UAT or PROD, and nothing else.

        There is no default. A venue whose environment is missing or
        unrecognised is REFUSED rather than assumed to be the safe kind —
        assuming UAT would eventually assume it about a live one.
        """
        env = str(value or '').strip().upper()
        if env not in ENVIRONMENTS:
            raise ValueError(
                f"venue '{name}': environment is {value!r}; it must be "
                f"one of {', '.join(ENVIRONMENTS)}. UAT and PROD are separate "
                f"venues with separate credentials and neither is a default.")
        return env

    @property
    def is_production(self) -> bool:
        return self.environment == "PROD"

    @property
    def password(self) -> Optional[str]:
        """The one place the secret is readable. Never returned by an API."""
        return os.environ.get(self.password_env) if self.password_env else None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def to_dict(self) -> Dict[str, Any]:
        """For config.json. Carries the .env KEY, never the value."""
        return {
            'environment': self.environment, 'broker': self.broker,
            'host': self.host, 'port': self.port,
            'md_host': self.md_host, 'md_port': self.md_port,
            'sender_comp_id': self.sender_comp_id,
            'target_comp_id': self.target_comp_id,
            'sender_sub_id': self.sender_sub_id,
            'target_sub_id': self.target_sub_id,
            'on_behalf_of_comp_id': self.on_behalf_of_comp_id,
            'on_behalf_of_sub_id': self.on_behalf_of_sub_id,
            'md_sender_comp_id': self.md_sender_comp_id,
            'md_target_comp_id': self.md_target_comp_id,
            'dc_host': self.dc_host, 'dc_port': self.dc_port,
            'dc_sender_comp_id': self.dc_sender_comp_id,
            'dc_target_comp_id': self.dc_target_comp_id,
            'fix_version': self.fix_version, 'username': self.username,
            'password_env': self.password_env, 'account': self.account,
            'heartbeat_sec': self.heartbeat_sec,
            'reset_seq_on_logon': self.reset_seq_on_logon,
            'use_tls': self.use_tls, 'data_dictionary': self.data_dictionary,
            'store_path': self.store_path, 'log_path': self.log_path,
            'enabled': self.enabled,
        }

    def session(self, which: str = "order") -> Dict[str, Any]:
        """The endpoint and comp ids for one of this venue's FIX sessions.

        `order`, `md` or `dropcopy`. A broker that splits them gives each its
        own host, port and comp ids; a broker that does not leaves them blank
        and every one of them falls back to the order session. Falling back
        is right — GUESSING is not, which is why a blank means "the same" and
        never "make something up".
        """
        which = (which or "order").lower()
        if which in ("md", "marketdata", "market_data"):
            return {
                'host': self.md_host or self.host,
                'port': self.md_port or self.port,
                'sender_comp_id': self.md_sender_comp_id or self.sender_comp_id,
                'target_comp_id': self.md_target_comp_id or self.target_comp_id,
                'shared': not (self.md_host or self.md_port
                               or self.md_target_comp_id),
            }
        if which in ("dc", "dropcopy", "drop_copy"):
            return {
                'host': self.dc_host,
                'port': self.dc_port,
                'sender_comp_id': self.dc_sender_comp_id or self.sender_comp_id,
                'target_comp_id': self.dc_target_comp_id,
                # Drop copy never falls back to the order session: an absent
                # drop copy is "not configured", not "use the other one".
                'shared': False,
                'configured': bool(self.dc_host and self.dc_port
                                   and self.dc_target_comp_id),
            }
        return {
            'host': self.host, 'port': self.port,
            'sender_comp_id': self.sender_comp_id,
            'target_comp_id': self.target_comp_id,
            'shared': False,
        }

    def to_public_dict(self) -> Dict[str, Any]:
        """For the UI. The password is reported as SET or NOT SET and never
        as a value — not even a masked one that could be echoed back."""
        d = self.to_dict()
        d['name'] = self.name
        d['password_set'] = self.has_password
        return d

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any]) -> "VenueConfig":
        raw = dict(raw or {})
        raw.pop('name', None)
        raw.pop('password_set', None)
        raw.pop('password', None)        # never accepted from a file
        return cls(name=name, **raw)


class ContractConfig:
    """One window on the screen: one spread contract at one venue.

    Specifications (tick size, tick value, multiplier, size bounds) are READ
    FROM THE VENUE and cached here so the screen can render before the session
    is up. `spec_source` records where each came from, because a number the
    operator typed and a number the venue reported must be distinguishable —
    every money figure on the window runs through them.
    """

    def __init__(self, key: str, name: str = "", symbol: str = "",
                 venue: str = "", security_id: str = "",
                 security_exchange: str = "",
                 tick_size: Any = None, tick_value: Any = None,
                 contract_multiplier: Any = None, currency: str = "USD",
                 min_qty: Any = None, qty_step: Any = None, max_qty: Any = None,
                 session_open: str = "", session_close: str = "",
                 decimals: int = 4, enabled: bool = True,
                 algo_on: bool = False,
                 spec_source: Optional[Dict[str, str]] = None,
                 **overrides: Any):
        self.key = key
        self.name = name or key
        self.symbol = symbol
        self.venue = venue
        self.security_id = security_id
        self.security_exchange = security_exchange

        self.tick_size = _blank_to_none(tick_size)
        self.tick_value = _blank_to_none(tick_value)
        self.contract_multiplier = _blank_to_none(contract_multiplier)
        self.currency = currency or "USD"
        self.min_qty = _blank_to_none(min_qty)
        self.qty_step = _blank_to_none(qty_step)
        self.max_qty = _blank_to_none(max_qty)

        self.session_open = session_open
        self.session_close = session_close
        self.decimals = int(decimals or 4)
        self.enabled = bool(enabled)
        #: Whether the algo is armed. Persisted so a restart does not silently
        #: re-arm a contract the trader stood down — or stand down one they
        #: left running.
        self.algo_on = bool(algo_on)
        self.spec_source = dict(spec_source or {})

        #: Per-contract overrides. None means "use the desk default", and 0 is
        #: a real number — see `settings_with_defaults`.
        self.overrides: Dict[str, Any] = {}
        for field in CONTRACT_DEFAULTS:
            if field in overrides:
                self.overrides[field] = _blank_to_none_any(overrides[field])

    def settings_with_defaults(self, desk: Dict[str, Any]) -> Dict[str, Any]:
        """This contract's effective settings: its own where set, the desk's
        where blank. `0` is set; `None` and `''` are blank."""
        out: Dict[str, Any] = {}
        for field, default_key in CONTRACT_DEFAULTS.items():
            value = self.overrides.get(field)
            if value is None:
                value = desk.get(default_key, DEFAULT_SETTINGS.get(default_key))
            out[field] = value
        # Booleans arrive from JSON as bools already; numbers may be strings
        # from a form post, so coerce the ones that are always numeric.
        for numeric in ('lookback', 'stats_update_interval_sec',
                        'entry_threshold', 'exit_threshold', 'stop_loss_z',
                        'max_hold_minutes', 'hurst_threshold',
                        'min_std_multiple', 'max_half_life', 'min_book_size',
                        'max_book_spread_ticks', 'quantity', 'max_position',
                        'max_trades_per_day', 'daily_max_loss',
                        'entry_cooldown_seconds', 'entry_limit_offset_ticks',
                        'exit_limit_offset_ticks', 'entry_limit_timeout_sec',
                        'exit_limit_timeout_sec', 'repeg_dead_band_ticks',
                        'commission_per_contract', 'exchange_fee_per_contract',
                        'clearing_fee_per_contract', 'slippage_budget_ticks',
                        'profit_target_pct'):
            try:
                out[numeric] = float(out[numeric]) if out[numeric] is not None else None
            except (TypeError, ValueError):
                out[numeric] = DEFAULT_SETTINGS.get(CONTRACT_DEFAULTS[numeric])
        out['lookback'] = int(out['lookback'] or 0)
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'name': self.name, 'symbol': self.symbol, 'venue': self.venue,
            'security_id': self.security_id,
            'security_exchange': self.security_exchange,
            'tick_size': self.tick_size, 'tick_value': self.tick_value,
            'contract_multiplier': self.contract_multiplier,
            'currency': self.currency, 'min_qty': self.min_qty,
            'qty_step': self.qty_step, 'max_qty': self.max_qty,
            'session_open': self.session_open,
            'session_close': self.session_close,
            'decimals': self.decimals, 'enabled': self.enabled,
            'algo_on': self.algo_on, 'spec_source': self.spec_source,
        }
        d.update(self.overrides)
        return d

    @classmethod
    def from_dict(cls, key: str, raw: Dict[str, Any]) -> "ContractConfig":
        return cls(key=key, **dict(raw or {}))


class TraderConfig:
    """The whole configuration: settings, venues and contracts."""

    def __init__(self, path: str = "config.json",
                 settings: Optional[Dict[str, Any]] = None,
                 venues: Optional[Dict[str, VenueConfig]] = None,
                 contracts: Optional[Dict[str, ContractConfig]] = None):
        self.path = path
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.venues: Dict[str, VenueConfig] = dict(venues or {})
        self.contracts: Dict[str, ContractConfig] = dict(contracts or {})

    # -- loading and saving ----------------------------------------------

    @classmethod
    def from_file(cls, path: str = "config.json",
                  env_path: str = ".env") -> "TraderConfig":
        if load_dotenv is not None and os.path.exists(env_path):
            load_dotenv(env_path, override=False)
        raw = atomicfile.read_json(path, default={}) or {}
        cfg = cls(path=path, settings=raw.get('settings') or {})
        for name, vraw in (raw.get('venues') or {}).items():
            cfg.venues[name] = VenueConfig.from_dict(name, vraw)
        for key, craw in (raw.get('contracts') or {}).items():
            cfg.contracts[key] = ContractConfig.from_dict(key, craw)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return {
            'settings': self.settings,
            'venues': {n: v.to_dict() for n, v in self.venues.items()},
            'contracts': {k: c.to_dict() for k, c in self.contracts.items()},
        }

    def save(self, path: Optional[str] = None) -> None:
        atomicfile.write_json(path or self.path, self.to_dict())

    # -- accessors -------------------------------------------------------

    def contract(self, key: str) -> Optional[ContractConfig]:
        return self.contracts.get(key)

    def venue(self, name: str) -> Optional[VenueConfig]:
        return self.venues.get(name)

    def enabled_contracts(self) -> List[ContractConfig]:
        return [c for c in self.contracts.values() if c.enabled]

    def effective(self, key: str) -> Dict[str, Any]:
        c = self.contracts.get(key)
        return c.settings_with_defaults(self.settings) if c else {}

    @property
    def has_production_venue(self) -> bool:
        """Whether ANY enabled venue is live. The taskbar badge reads this,
        and it is deliberately true if even one is."""
        return any(v.is_production and v.enabled for v in self.venues.values())

    @property
    def environment_label(self) -> str:
        if self.has_production_venue:
            return "PROD"
        return "UAT" if self.venues else "SIMULATED"

    def structural_changes(self, new_settings: Dict[str, Any]) -> List[str]:
        """Which of the incoming settings need a restart to take effect."""
        return [k for k in STRUCTURAL_SETTINGS
                if k in new_settings and new_settings[k] != self.settings.get(k)]


def write_env_value(key: str, value: str, env_path: str = ".env") -> None:
    """Set one variable in `.env`, leaving the rest of the file alone.

    The only writer of secrets in the system. It rewrites the whole file
    atomically because a partial `.env` is a venue that cannot log on.
    """
    lines: List[str] = []
    found = False
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    atomicfile.write_text(env_path, "\n".join(lines) + "\n")
    os.environ[key] = value
