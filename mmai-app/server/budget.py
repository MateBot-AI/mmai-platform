"""A16 whole-apartment budget test (both bots). Pure functions, exact money arithmetic (Decimal), no database, no personal
attributes. Wiring (8 Nov 2026, A16 reconciliation approved): MateBot's rules.household_budget / rules.check_group call
matebot_baseline + check_total_budget for every complete candidate group; the legacy per-share 10% test in rules.py is withdrawn.
HomeBot wiring (H9 explicit maximum) is the HomeBot owner's.

Scope: the budget arithmetic only. These helpers do not check bedding capacity, date flexibility, availability or any other
eligibility rule; the eligibility layer calls them with already-validated occupant counts.

Rule text implemented (register, 5 Nov 2026):
  A16  actual whole-apartment weekly rent INCLUDING applicable extra-bed charges may be up to 10% above the whole-apartment
       maximum for studios, one- and two-bedroom apartments and up to 20% above it for three-bedroom apartments. MateBot
       baseline = sum of every member's stated weekly maximum, before the percentage. HomeBot: the explicit whole-apartment
       maximum (H9). Only the total is tested: no separate per-person ceiling.
  A5   $70/week extra-bed charge beyond standard capacity (3rd bed studio/1BR, 5th in 2-bedroom, 7th in 3-bedroom) — reused
       from rules.extra_bed_charge, which already implements it.
  A10(e) an exception rent may exceed A16's allowance ONLY with the customer's explicit agreement, recorded by staff. That
       path is NOT in this module: see exception_rent.py (requires the staff exit exception for the same candidate context
       AND the customer's recorded agreement to the same total). check_total_budget has no override.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from . import exact_decimal as xd, rules

ALLOWANCE = {"Studio": Decimal("0.10"), "1BR": Decimal("0.10"), "2B1B": Decimal("0.10"), "2B2B": Decimal("0.10"), "3B": Decimal("0.20")}

# reason codes (stable strings for staff views and later AI/validator exchanges)
WITHIN_MAX = "WITHIN_MAX"                                   # total rent at or below the stated whole-apartment maximum
WITHIN_ALLOWANCE = "WITHIN_ALLOWANCE"                       # above the maximum but within the A16 percentage
OVER_ALLOWANCE = "OVER_ALLOWANCE"                           # above the A16 limit: excluded


def money(v, *, positive: bool = False, tier: str = xd.RAW) -> Decimal:
    """Exact decimal from int/str/Decimal/float input (floats go through str() so 1105.5 stays 1105.5). Fail closed: booleans,
    None, other types, NaN/Infinity and negative amounts are rejected; `positive=True` also rejects zero (actual rents, stated
    maxima and member maxima are positive amounts in the intake). Representation tiers (exact_decimal): RAW amounts ≤ 12
    integer / ≤ 6 decimal digits; AGGREGATE (a whole-apartment maximum, which may be a MateBot baseline of ≤ 7 maxima) ≤ 13 / 6;
    DERIVED (computed figures for display only) ≤ 15 / 8. Out of bounds is rejected, never rounded."""
    if isinstance(v, bool) or v is None or not isinstance(v, (int, float, str, Decimal)):
        raise TypeError("money needs a number")
    try:
        with xd.exact():   # parse with InvalidOperation trapped even if the caller disabled it (no silent NaN)
            d = v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a money amount: {v!r}")
    if not d.is_finite():
        raise ValueError(f"not a finite money amount: {v!r}")
    if d < 0 or (positive and d <= 0):
        raise ValueError(f"money amount must be {'positive' if positive else 'zero or more'}: {v!r}")
    try:
        return xd.bounded(d, tier=tier)
    except ValueError as e:
        raise ValueError(f"money amount {e}: {v!r}")


def head_count(people) -> int:
    """Number of occupants: a positive whole number (no silent truncation of floats, no booleans). No maximum is applied
    here; capacity and the P3/A4 limits are checked by the eligibility layer, not by the budget arithmetic."""
    if isinstance(people, bool) or not isinstance(people, int) or people < 1:
        raise ValueError(f"people must be a positive whole number: {people!r}")
    return people


def cents(v: Decimal) -> Decimal:
    """Presentation rounding only (half up to the cent) of an exact DERIVED value (totals, limits, baselines may carry 7–8 dp
    or 13+ integer digits); comparisons always use the exact values, which are classified before anything is rounded."""
    with xd.display():
        return money(v, tier=xd.DERIVED).quantize(Decimal("0.01"))


def allowance_pct(type_code: str) -> Decimal:
    return ALLOWANCE[rules.normalise_type(type_code)]


def extra_bed_charge(type_code: str, people: int) -> Decimal:
    """A5 via the existing implementation; returned as exact Decimal."""
    return money(rules.extra_bed_charge(type_code, head_count(people)))


def total_weekly_rent(weekly_rent, type_code: str, people: int) -> Decimal:
    """Actual whole-apartment weekly rent for `people` occupants: rent + applicable extra-bed charge (A16, A5)."""
    with xd.exact():
        return money(weekly_rent, positive=True) + extra_bed_charge(type_code, people)


def budget_limit(whole_apartment_max, type_code: str) -> Decimal:
    """A16 limit = whole-apartment maximum x (1 + allowance). Exact (no rounding)."""
    with xd.exact():
        return money(whole_apartment_max, positive=True, tier=xd.AGGREGATE) * (Decimal(1) + allowance_pct(type_code))


def matebot_baseline(member_weekly_maximums: Iterable) -> Decimal:
    """A16: for a newly formed MateBot group the baseline is the sum of every member's stated weekly maximum."""
    vals = [money(v, positive=True) for v in member_weekly_maximums]
    if not vals:
        raise ValueError("a group needs at least one stated weekly maximum")
    with xd.exact():
        return sum(vals, Decimal(0))


@dataclass(frozen=True)
class BudgetResult:
    passed: bool
    reason_code: str
    total_weekly_rent: Decimal
    whole_apartment_max: Decimal
    allowance_pct: Decimal
    limit: Decimal
    extra_bed_charge: Decimal

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reason_code": self.reason_code, "total_weekly_rent": str(cents(self.total_weekly_rent)),
                "whole_apartment_max": str(cents(self.whole_apartment_max)), "allowance_pct": str(self.allowance_pct),
                "limit": str(cents(self.limit)), "extra_bed_charge": str(cents(self.extra_bed_charge))}


def check_total_budget(weekly_rent, type_code: str, people: int, whole_apartment_max) -> BudgetResult:
    """The A16 test, strictly: actual total (rent + A5 extra bed for `people`) against the whole-apartment maximum and its
    allowance. `whole_apartment_max` is HomeBot's explicit maximum (H9) or `matebot_baseline(...)` for a MateBot group.
    No override of any kind here; the A10(e) exception-rent path with its evidence lives in exception_rent.check_exception_rent.
    Individual shares are never tested (A16: no per-person ceiling)."""
    code = rules.normalise_type(type_code)
    with xd.exact():   # exact arithmetic regardless of the caller's Decimal context (see exact_decimal)
        extra = extra_bed_charge(code, people)
        total = money(weekly_rent, positive=True) + extra
        maximum = money(whole_apartment_max, positive=True, tier=xd.AGGREGATE)   # raw H9 maximum or MateBot baseline
        pct = allowance_pct(code)
        limit = maximum * (Decimal(1) + pct)
        return BudgetResult(*classify_total(total, maximum, limit), total, maximum, pct, limit, extra)


def classify_total(total: Decimal, maximum: Decimal, limit: Decimal) -> tuple[bool, str]:
    """A16 classification of an already-computed total (shared with the exception-rent helper, which tests the authorised
    exception total — a whole-apartment figure that already includes any extra-bed charge)."""
    if total <= maximum:
        return True, WITHIN_MAX
    if total <= limit:
        return True, WITHIN_ALLOWANCE
    return False, OVER_ALLOWANCE
