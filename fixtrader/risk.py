"""Portfolio-wide risk gate: how many positions and how much margin the WHOLE
book carries, checked once per pass — on top of, not instead of, what
`entry_signal` already checks per contract.

`entry_signal` cannot see other contracts; a desk running more than one
spread needs something that can. Same rule as `signals.py`, restated here on
purpose because it is the one rule that must never drift between the two
files:

    **This gates ENTRIES only. Nothing here may withhold an exit.**

A position already open is closed by `exit_signal` regardless of what the
rest of the book is doing — a portfolio at its cap is not a reason to strand
a position that should be flat.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PortfolioRiskLimits:
    """Two independent caps, both must clear: the whole-book cap and the
    cap for this contract's own spread family.

    `family` defaults to the contract's own key wherever a contract does not
    set one (see `family_of` below) — so a desk running one contract per
    spread sees no change from today: that contract's own family cap of 1 is
    exactly the single-position-per-contract behaviour already structural to
    `ContractRuntime.position`. The family cap only starts doing new work
    once two contracts (e.g. two maturities of the same spread) share one.
    """
    max_positions_total: int = 3
    max_positions_per_family: Dict[str, int] = field(default_factory=dict)
    default_family_cap: int = 1
    #: None means unlimited — an unset cap must not gate, the same
    #: convention `entry_signal` uses for a `0.0` limit meaning "off".
    max_margin_total: Optional[float] = None

    def family_cap(self, family: str) -> int:
        return self.max_positions_per_family.get(family, self.default_family_cap)


def family_of(contract, settings: Dict[str, object]) -> str:
    """The spread family a contract belongs to, for grouping the cap.

    Reads `settings['spread_family']` where an operator has set one —
    grouping e.g. two calendar months of the same crack spread — and falls
    back to the contract's own key otherwise, so an ungrouped contract is its
    own family of one.
    """
    family = settings.get('spread_family')
    return str(family) if family else contract.key


def portfolio_risk_check(prospective_family: str,
                         prospective_margin: Optional[float],
                         open_by_family: Dict[str, int],
                         margin_locked_total: float,
                         limits: PortfolioRiskLimits) -> Optional[str]:
    """None means allowed. Anything else is `blocked_by` text, in the same
    voice `entry_signal` already writes it in — named number, named limit.

    Call this ONLY when `entry_signal` has already said OPEN. It answers a
    different question — not "should this contract enter" but "can the book
    as a whole afford one more" — and running it first would report a
    portfolio-level block on a contract that was not going to enter anyway.
    """
    total_open = sum(open_by_family.values())
    if total_open >= limits.max_positions_total:
        return (f"portfolio position cap reached "
                f"({total_open}/{limits.max_positions_total})")

    family_open = open_by_family.get(prospective_family, 0)
    cap = limits.family_cap(prospective_family)
    if family_open >= cap:
        return (f"{prospective_family} position cap reached "
                f"({family_open}/{cap})")

    if limits.max_margin_total is not None:
        if prospective_margin is None:
            return ("margin unknown for this contract — entry withheld "
                    "until the venue can price it")
        projected = margin_locked_total + prospective_margin
        if projected > limits.max_margin_total:
            return (f"portfolio margin cap reached — this entry needs "
                    f"{prospective_margin:,.0f}, {margin_locked_total:,.0f} "
                    f"already locked, {limits.max_margin_total:,.0f} allowed")

    return None
