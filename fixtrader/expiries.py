"""Futures expiry months for the contracts that take an expiry, not a
free-typed symbol: BZ (ICE Brent) and CL (NYMEX WTI) today.

Placeholder list. `PLACEHOLDER_MONTHS_AHEAD` months are generated forward
from whatever `today()` returns — nothing here reads a real listed-contract
calendar from TT or the exchange yet. Swapping to a live list later is a
change to `available_months()` alone; nothing that calls it needs to change,
since it always returns the same shape.
"""

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

#: Real exchange-published specs (CME Group contract specifications,
#: confirmed independently of TT — these are exchange-wide, not
#: FCM-specific, so they hold regardless of which broker routes to them).
#: Placeholder only in the sense that TT/the FCM has not been asked to
#: confirm them via SecurityDefinitionRequest (35=c) yet — see the note on
#: `available_months` below for the harder placeholder, the calendar itself.
LEG_SPECS = {
    "CL": {"tick_size": 0.01, "tick_value": 10.0,
           "contract_multiplier": 1000.0, "currency": "USD"},
    "BZ": {"tick_size": 0.01, "tick_value": 10.0,
           "contract_multiplier": 1000.0, "currency": "USD"},
    # HO's *42 multiplier (gallons per barrel) converts its native
    # cents-per-gallon quoting into the same dollars-per-barrel terms as CL,
    # which is why the crack spread can share CL's tick convention below
    # rather than needing one of its own.
    "HO": {"tick_size": 0.0001, "tick_value": 4.20,
           "contract_multiplier": 42000.0, "currency": "USD"},
}

#: CME/ICE month codes, F=Jan through Z=Dec — the same convention already in
#: this repo's example symbols (e.g. 'FEFV6' = Iron ore, Oct 2026).
MONTH_CODES = "FGHJKMNQUVXZ"

#: How many months forward to offer. This is the ONE placeholder that
#: cannot be hardened with a public number the way LEG_SPECS was: CL lists
#: every month for the current year + 10 years + 2, BZ for + 7 years + 3,
#: HO for 18 consecutive months — real, different, and only the exchange
#: (or TT via a live SecurityListRequest) actually knows which specific
#: months are open for trading THIS WEEK, since months roll off and new
#: ones list continuously. Offering more months here than are truly listed
#: is a smaller failure than offering too few — TT will reject an entry on
#: an unlisted month with a real reject text, which is a safe failure mode;
#: silently hiding a real near-term month because this list guessed short
#: is not. 6 is a reasonable near-term working set until SecurityListRequest
#: replaces this function outright.
PLACEHOLDER_MONTHS_AHEAD = 6

#: Both legs of a family share one expiry, since a placeholder without real
#: tick/margin data has no basis to offer them separately.
FAMILIES = {
    "bz_cl": {"label": "Brent–WTI (BZ–CL)", "legs": ("BZ", "CL")},
    "crack": {"label": "Heating Oil–WTI crack (HO×42–CL)", "legs": ("HO", "CL")},
}


def today() -> date:
    return date.today()


@dataclass
class ExpiryMonth:
    code: str          # e.g. "V6" — single letter + single year digit
    label: str          # e.g. "Oct 2026" — what the dropdown shows
    year: int
    month: int


def _month_code(year: int, month: int) -> str:
    #: Single trailing digit only, matching this repo's existing symbols
    #: ('V6', not 'V26') — good for a decade, which a placeholder need not
    #: outlive.
    return f"{MONTH_CODES[month - 1]}{year % 10}"


def available_months(n: int = PLACEHOLDER_MONTHS_AHEAD,
                     start: Optional[date] = None) -> List[ExpiryMonth]:
    """The next `n` calendar months forward from `start` (today by default),
    front month first. A placeholder calendar: every month is offered,
    where a real listed-contract calendar would skip the ones the exchange
    does not list for a given product.
    """
    d = start or today()
    year, month = d.year, d.month
    out = []
    for _ in range(n):
        out.append(ExpiryMonth(
            code=_month_code(year, month),
            label=date(year, month, 1).strftime("%b %Y"),
            year=year, month=month,
        ))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def spread_symbol(family: str, code: str) -> str:
    """The symbol a contract-add form would submit, e.g. 'BZV6-CLV6' for
    bz_cl at code V6, or 'HOV6*42-CLV6' for the crack spread."""
    legs = FAMILIES[family]["legs"]
    if family == "crack":
        return f"{legs[0]}{code}*42-{legs[1]}{code}"
    return f"{legs[0]}{code}-{legs[1]}{code}"


def spread_key(family: str, code: str) -> str:
    """The contract config key — lowercase, stable, matches the pattern
    `api_create_contract` already derives from a symbol."""
    return f"{family}_{code.lower()}"


def spread_spec(family: str) -> dict:
    """Tick size, tick value, contract multiplier and currency for the
    WHOLE spread, derived from its legs' real exchange specs.

    Both families here reduce to CL's convention: bz_cl because BZ and CL
    already quote in the same units (both $/bbl, 1,000 bbl), and crack
    because HO's *42 multiplier converts it into $/bbl terms to match CL.
    This is a starting point for the contract form, not a substitute for
    reading it back from the venue once `security_definition()` is wired —
    see `docs/FIX_NOTES.md` for the same caveat on every other spec in
    this repo.
    """
    return dict(LEG_SPECS["CL"])
