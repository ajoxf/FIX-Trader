"""Prices are shown with as many decimals as the tick needs, at least 2 —
never TT's padding (3890.8500) — and never more than the contract's own
Decimals."""
from types import SimpleNamespace as C

from fixtrader.engine import display_decimals


def test_a_cent_tick_shows_two_not_four():
    assert display_decimals(C(decimals=4, tick_size=0.01)) == 2
    assert display_decimals(C(decimals=4, tick_size=0.1)) == 2
    assert display_decimals(C(decimals=4, tick_size=0.25)) == 2
    assert display_decimals(C(decimals=4, tick_size=1)) == 2


def test_a_finer_tick_keeps_its_own_decimals():
    assert display_decimals(C(decimals=4, tick_size=0.005)) == 3
    assert display_decimals(C(decimals=4, tick_size=0.0001)) == 4


def test_the_contracts_decimals_is_the_ceiling_and_an_unknown_tick_keeps_it():
    assert display_decimals(C(decimals=1, tick_size=0.01)) == 1
    assert display_decimals(C(decimals=4, tick_size=None)) == 4
    assert display_decimals(C(decimals=4, tick_size=0)) == 4
