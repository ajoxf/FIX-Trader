import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixtrader.config import DEFAULT_SETTINGS, ContractConfig, TraderConfig
from fixtrader.stats import StatsWindow

T0 = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def iron_ore():
    """A contract with the specs of an SGX iron-ore calendar spread:
    tick 0.01, tick value $1.00 (100 tonnes), so one point is $100."""
    return ContractConfig(
        key='fef_v6x6', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6',
        venue='SGX-UAT', tick_size=0.01, tick_value=1.0,
        contract_multiplier=100.0, currency='USD',
        min_qty=1.0, qty_step=1.0, max_qty=50.0)


@pytest.fixture
def desk():
    return dict(DEFAULT_SETTINGS)


@pytest.fixture
def settings(iron_ore, desk):
    return iron_ore.settings_with_defaults(desk)


def warm(window: StatsWindow, values, start=0.0, step=1.0, armed=False):
    """Push a series through a window, returning every touch event raised."""
    events = []
    t = start
    for v in values:
        events.extend(window.add(v, at(t), algo_armed=armed))
        t += step
    return events


def flat_series(n, value=0.5):
    return [value] * n
