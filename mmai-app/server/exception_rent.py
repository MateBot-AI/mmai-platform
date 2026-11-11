"""A10(e) exception rent for a blackout move-out, combined with A16 (both bots). Pure; Phase A; not wired.

Order of evidence (none of it is automatic):
  1. the move-out is a blackout day (otherwise an exception rent is not a concept: use budget.check_total_budget);
  2. a StaffExitException covers THIS candidate context, THIS move-out and the proposal is priced at exactly the authorised
     exception total, which must be above the normal total (A6(d)/A10(e));
  3. the authorised exception total (a whole-apartment weekly figure that already includes any extra-bed charge — it is not
     recomputed, so nothing is double counted) is tested against A16: within the allowance → pass;
  4. above the allowance → pass ONLY with the customer's recorded agreement for the same context and the same total
     (A10(e): "only with the customer's explicit agreement, which staff records; do not treat it as agreed beforehand").
Normal total and exception total are both returned so a staff view can show what was quoted and what was authorised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from . import budget, evidence_fields as ev, exact_decimal as xd, lease_dates
from .lease_dates import StaffExitException

EXCEPTION_NOT_APPLICABLE = "EXCEPTION_NOT_APPLICABLE"            # move-out is in a normal window: ordinary A16 applies
EXCEPTION_RENT_WITHIN_ALLOWANCE = "EXCEPTION_RENT_WITHIN_ALLOWANCE"
EXCEPTION_RENT_OVER_ALLOWANCE_CUSTOMER_AGREED = "EXCEPTION_RENT_OVER_ALLOWANCE_CUSTOMER_AGREED"
OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT = "OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT"


@dataclass(frozen=True)
class CustomerAgreement:
    """The customer's explicit agreement to a specific exception total for ONE candidate context, recorded by staff."""
    context_ref: str
    agreed_total_weekly_rent: Decimal
    recorded_by: str
    recorded_at: str

    def __post_init__(self):
        object.__setattr__(self, "context_ref", ev.clean_text(self.context_ref, "context_ref"))
        object.__setattr__(self, "agreed_total_weekly_rent", ev.clean_positive_money(self.agreed_total_weekly_rent, "agreed_total_weekly_rent"))
        object.__setattr__(self, "recorded_by", ev.clean_text(self.recorded_by, "recorded_by", max_len=80))
        object.__setattr__(self, "recorded_at", ev.clean_timestamp(self.recorded_at, "recorded_at"))

    def covers(self, context_ref: str, total: Decimal) -> bool:
        return ev.clean_text(context_ref, "context_ref") == self.context_ref and self.agreed_total_weekly_rent == total


@dataclass(frozen=True)
class ExceptionRentResult:
    passed: bool
    reason_code: str
    detail: str = ""
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reason_code": self.reason_code, "detail": self.detail, "facts": dict(self.facts)}


@dataclass(frozen=True)
class ExceptionOffer:
    """An exact booking candidate, freshly authorised by the DB boundary.

    The total already includes A5; it is never an inventory base tariff. Pure rule
    consumers independently check the supplied evidence against current money facts.
    """
    record_id: int
    exception_ref: str
    move_in: date
    move_out: date
    exception_total: Decimal
    staff_exception: StaffExitException
    customer_agreement: Optional[CustomerAgreement]
    updated_at: str


def check_exception_rent(context_ref: str, type_code: str, people: int, whole_apartment_max, move_out: date, normal_weekly_rent,
                         proposed_total_weekly_rent, *, staff_exception: Optional[StaffExitException],
                         customer_agreement: Optional[CustomerAgreement] = None) -> ExceptionRentResult:
    """`normal_weekly_rent` is the apartment's normal weekly rent (A6 rate); the normal TOTAL adds the A5 extra-bed charge for
    `people`. `proposed_total_weekly_rent` is what this proposal would charge in total; it must equal the authorised exception
    total exactly. The caller builds `context_ref` from the immutable candidate snapshot (see StaffExitException)."""
    context_ref = ev.clean_text(context_ref, "context_ref")
    normal_total = budget.total_weekly_rent(normal_weekly_rent, type_code, people)
    proposed = ev.clean_positive_money(proposed_total_weekly_rent, "proposed_total_weekly_rent")
    maximum = budget.money(whole_apartment_max, positive=True, tier=xd.AGGREGATE)   # raw H9 maximum or MateBot baseline
    pct = budget.allowance_pct(type_code)
    limit = budget.budget_limit(maximum, type_code)   # exact, context-independent (exact_decimal)
    with xd.exact():
        return _decide(context_ref, type_code, people, move_out, staff_exception, customer_agreement, normal_total, proposed, maximum, pct, limit)


def _decide(context_ref, type_code, people, move_out, staff_exception, customer_agreement, normal_total, proposed, maximum, pct, limit):
    facts = {"normal_total_weekly_rent": ev.fmt_money(normal_total), "proposed_total_weekly_rent": ev.fmt_money(proposed),
             "whole_apartment_max": ev.fmt_money(maximum), "allowance_pct": str(pct), "limit": ev.fmt_money(limit)}
    if not lease_dates.is_blackout(move_out):
        return ExceptionRentResult(False, EXCEPTION_NOT_APPLICABLE, "move-out is in a normal exit window: no exception rent applies, use the ordinary budget test", facts)
    exit_check = lease_dates.check_exit(move_out, context_ref=context_ref, normal_total_weekly_rent=normal_total,
                                        proposed_total_weekly_rent=proposed, staff_exception=staff_exception)
    facts.update(exit_check.facts)
    if not exit_check.passed:
        return ExceptionRentResult(False, exit_check.reason_code, exit_check.detail, facts)
    exception_total = staff_exception.exception_total_weekly_rent  # == proposed, by covers(); already a whole-apartment total
    facts["exception_total_weekly_rent"] = ev.fmt_money(exception_total)
    ok, a16 = budget.classify_total(exception_total, maximum, limit)
    if ok:
        return ExceptionRentResult(True, EXCEPTION_RENT_WITHIN_ALLOWANCE, f"authorised exception total is within the A16 allowance ({a16})", facts)
    if customer_agreement is not None and customer_agreement.covers(context_ref, exception_total):
        facts.update({"recorded_by": customer_agreement.recorded_by, "recorded_at": customer_agreement.recorded_at})
        return ExceptionRentResult(True, EXCEPTION_RENT_OVER_ALLOWANCE_CUSTOMER_AGREED,
                                   "authorised exception total exceeds the A16 allowance; the customer's agreement to this exact total is recorded", facts)
    return ExceptionRentResult(False, OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT,
                               "authorised exception total exceeds the A16 allowance and no matching recorded customer agreement exists", facts)
