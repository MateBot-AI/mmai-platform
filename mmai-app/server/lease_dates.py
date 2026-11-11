"""Shared date rules for both bots (A9 minimum lease, A10 exit windows, A17 vacancy cap, A6 rate kind). Pure functions on
`datetime.date`, no database, no personal attributes. Wired (8 Nov 2026, date reconciliation approved): rules.exit_ok uses
exit_window/min_lease_end (the legacy seasonal windows are withdrawn) and rules.check_group applies check_vacancy with the
business date from business_today() (injectable; tests pin it, live code never uses a frozen date).

Scope: these are the date RULES only (minimum lease, exit window, vacancy cap, rate kind) for ONE concrete proposed move-in /
move-out pair against ONE apartment's availability. They do not expand a customer's date flexibility into candidate dates, do
not check accepted date limits, capacity, budget or any other eligibility rule; the eligibility layer does that and calls these
per candidate. Inputs must be `datetime.date` values (a `datetime` is rejected to avoid silent time-of-day truncation).

Rule text implemented (register, 5 Nov 2026):
  A10(b) normal move-outs: 10 January–20 February, 1–20 July, 5 October–7 December, first and last day INCLUDED, every year.
  A10(c) blackout: 21 February–30 June, 21 July–4 October, 8 December–9 January (across the year boundary). A move-out in a
         blackout requires (e); never granted silently.
  A10(e) exception = staff approval of each specific move-out date with an increased rent set case by case for that booking.
         Modelled as explicit evidence (`StaffExitException`) bound to the candidate context, the move-out date and the exact
         booking total; the proposal must be priced at that total. The customer's agreement to that total against A16 is a
         separate record (exception_rent.CustomerAgreement) and the combined check is exception_rent.check_exception_rent.
  A17(a) count only FUTURE empty days: from the later of today and the apartment's available date, to the proposed move-in.
  A17(b) permitted gap = 5% of the final agreed stay (move-in to move-out), rounded DOWN to whole days (365 → 18, 180 → 9).
  A17(c) exceeding the cap excludes the candidate; a start before actual availability is never permitted; min lease and exit
         rules are not waived.
  A9     minimum lease is the apartment's own minimum (A2(h)), measured in CALENDAR MONTHS (Bill, 8 November 2026): the stay
         meets an N-month minimum when the move-out is on or after the same day of the month N months after the move-in, or
         the last day of that month when the same day does not exist (31 Jan + 1 month = 28/29 Feb). 1 Feb → 1 Apr meets two
         months; 1 Feb → 31 Mar does not. A FRACTIONAL staff-entered minimum (e.g. 1.5) is not rounded either way: the check
         holds (fails) with LEASE_MINIMUM_FRACTIONAL_HOLD until staff enter a whole number of months or confirm the case.
         The former average-day calculation (days / 30.4375) is withdrawn from both this module and rules.exit_ok.
  A6     rate kind reused from rules.rate_kind (6-month rate for a January/February start ending in July, whichever year).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import calendar
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BUSINESS_TZ_KEY = "Australia/Melbourne"   # A17(a) "today" is ALM's business date, not the server's UTC date

from . import evidence_fields as ev, exact_decimal as xd, rules

# (start month, day, end month, day, label) — inclusive both ends; evaluated in the move-out's own calendar year
EXIT_WINDOWS = (
    (1, 10, 2, 20, "10 January to 20 February"),
    (7, 1, 7, 20, "1 to 20 July"),
    (10, 5, 12, 7, "5 October to 7 December"),
)
VACANCY_CAP_FRACTION = Decimal("0.05")   # documentary; the computation uses the exact integer divisor below
VACANCY_CAP_DIVISOR = 20                 # 5% == 1/20

# reason codes
EXIT_IN_WINDOW = "EXIT_IN_WINDOW"
EXIT_BLACKOUT_NEEDS_EXCEPTION = "EXIT_BLACKOUT_NEEDS_EXCEPTION"
EXIT_BLACKOUT_STAFF_EXCEPTION = "EXIT_BLACKOUT_STAFF_EXCEPTION"
EXIT_EXCEPTION_INVALID = "EXIT_EXCEPTION_INVALID"   # an exception record exists but does not cover this candidate/date/price
LEASE_BELOW_MINIMUM = "LEASE_BELOW_MINIMUM"
LEASE_MINIMUM_FRACTIONAL_HOLD = "LEASE_MINIMUM_FRACTIONAL_HOLD"   # A9: fractional minimum is held for staff, never rounded
LEASE_MINIMUM_INVALID_HOLD = "LEASE_MINIMUM_INVALID_HOLD"         # a stored minimum that is not a usable positive number (0, negative, NaN, absurdly large): held for staff, never defaulted
MAX_MIN_LEASE_MONTHS = 1200   # engineering VALIDATION limit (100 years), not a rule or eligibility threshold: a stored minimum beyond it is held for staff as a data problem, so the calendar arithmetic is never asked to overflow
LEASE_LENGTH_OK = "LEASE_LENGTH_OK"
DATES_INVALID = "DATES_INVALID"
START_BEFORE_AVAILABLE = "START_BEFORE_AVAILABLE"
VACANCY_OK = "VACANCY_OK"
VACANCY_CAP_EXCEEDED = "VACANCY_CAP_EXCEEDED"


class BusinessTimezoneUnavailable(RuntimeError):
    """The IANA zone data for Melbourne is not installed on this host. The clock fails closed (no UTC fallback: that would move
    the business date by up to 11 hours); deploy the pinned `tzdata` package (requirements.txt) or OS zoneinfo."""


def business_tz() -> ZoneInfo:
    """The Melbourne zone, resolved on first use (not at import, so a host without zone data still serves the forms and
    reports the problem where the clock is actually needed). Python's zoneinfo reads the OS database first and the `tzdata`
    package second, so the pinned package keeps the slim container image portable."""
    try:
        return ZoneInfo(BUSINESS_TZ_KEY)
    except ZoneInfoNotFoundError as ex:
        raise BusinessTimezoneUnavailable(f"time zone data for {BUSINESS_TZ_KEY} is not installed (install the pinned tzdata package); "
                                          "the business-date clock will not guess") from ex


def melbourne_today(now: Optional[datetime] = None) -> date:
    """The business date in Melbourne for an aware instant (default: the current UTC instant). A naive datetime is rejected
    rather than guessed: 13:30 UTC on 8 November is already 9 November in Melbourne (UTC+11 in summer)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise TypeError("melbourne_today needs an aware datetime (or None for now)")
    return now.astimezone(business_tz()).date()


CLOCK: Callable[[], date] = melbourne_today   # application-boundary injection point (tests pin a date; live code leaves it)


def business_today() -> date:
    """Today's business date via the injectable clock. The clock must return a datetime.date (a datetime is rejected)."""
    d = CLOCK()
    if isinstance(d, datetime) or not isinstance(d, date):
        raise TypeError(f"the business clock must return a datetime.date, got {type(d).__name__}")
    return d


def _date(v, name: str) -> date:
    if isinstance(v, datetime) or not isinstance(v, date):
        raise TypeError(f"{name} must be a datetime.date, got {type(v).__name__}")
    return v


def _min_lease_months(v) -> float:
    """A usable minimum: int/float/Decimal (not bool), finite, > 0 and <= MAX_MIN_LEASE_MONTHS. Wrong TYPE is a programming
    error (TypeError); a wrong VALUE raises ValueError, which check_min_lease / rules.exit_ok turn into a staff hold. The limit is
    compared BEFORE any float() or int() conversion, so a huge int (10**1000 would overflow float()) or a Decimal with a huge
    exponent (1E+100000000) never reaches float or calendar arithmetic; diagnostics use `_bounded_repr` so a huge int is never
    expanded into text (repr(10**10000) itself raises ValueError under Python's integer-string digit limit)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        raise TypeError(f"min_lease_months must be a number, got {type(v).__name__}")
    if isinstance(v, int):   # exact integer comparison first: no float() of a possibly enormous int
        if v <= 0:
            raise ValueError(f"min_lease_months must be a positive finite number: {_bounded_repr(v)}")
        if v > MAX_MIN_LEASE_MONTHS:
            raise ValueError(f"min_lease_months {_bounded_repr(v)} is beyond the supported validation limit of {MAX_MIN_LEASE_MONTHS} months")
        return float(v)
    if isinstance(v, Decimal):
        if not v.is_finite() or v <= 0:
            raise ValueError(f"min_lease_months must be a positive finite number: {_bounded_repr(v)}")
        if v > MAX_MIN_LEASE_MONTHS:
            raise ValueError(f"min_lease_months {_bounded_repr(v)} is beyond the supported validation limit of {MAX_MIN_LEASE_MONTHS} months")
        return v   # kept exact: a Decimal such as 1.00000000000000001 must stay fractional, not round to 1.0 through float
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"min_lease_months must be a positive finite number: {v!r}")
    if v > MAX_MIN_LEASE_MONTHS:
        raise ValueError(f"min_lease_months {v!r} is beyond the supported validation limit of {MAX_MIN_LEASE_MONTHS} months")
    return v


def _bounded_repr(v) -> str:
    """A short, safe description of a numeric value for diagnostics. Ints are described by sign and digit count when they are
    too large to print (repr(10**10000) raises ValueError under the interpreter's int-to-str digit limit); Decimals use their
    exponent form, which is already short; everything else is repr()."""
    if isinstance(v, bool) or not isinstance(v, int):
        return repr(v)
    if -10 ** 15 < v < 10 ** 15:
        return repr(v)
    bits = abs(v).bit_length()
    digits = int(bits * 0.30103) + 1   # upper estimate of decimal digits from the bit length, no string conversion needed
    return f"{'-' if v < 0 else ''}int(~{digits} digits)"


def _whole_months(minimum) -> Optional[int]:
    """The minimum as a whole number of months, or None when it is fractional (A9: held, never rounded)."""
    if isinstance(minimum, Decimal):
        return int(minimum) if minimum == minimum.to_integral_value() else None
    return int(minimum) if minimum == int(minimum) else None


def add_calendar_months(start: date, months: int) -> date:
    """A9: the same day of the month `months` calendar months after `start`, or the last day of that month when the same
    day does not exist (31 Jan + 1 → 28 Feb, or 29 Feb in a leap year; 30 Nov + 3 → 28/29 Feb). `months` must be a whole
    non-negative number (a fractional minimum is held by check_min_lease, not resolved here)."""
    start = _date(start, "start")
    if isinstance(months, bool) or not isinstance(months, int) or months < 0:
        raise ValueError(f"months must be a whole non-negative number: {months!r}")
    y, m = divmod(start.month - 1 + months, 12)
    y, m = start.year + y, m + 1
    if y > date.max.year:
        raise ValueError(f"{months} months after {start.isoformat()} is beyond the supported calendar (year {y})")
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(start.day, last))


def min_lease_end(move_in: date, min_lease_months) -> Optional[date]:
    """A9: the earliest move-out that meets the minimum, or None when the minimum is fractional (held, not rounded)."""
    whole = _whole_months(_min_lease_months(min_lease_months))
    return None if whole is None else add_calendar_months(move_in, whole)


def exit_window(move_out: date) -> Optional[str]:
    """Label of the A10(b) window containing `move_out`, or None when the date is a blackout day (A10(c))."""
    move_out = _date(move_out, "move_out")
    y = move_out.year
    for m1, d1, m2, d2, label in EXIT_WINDOWS:
        if date(y, m1, d1) <= move_out <= date(y, m2, d2):
            return label
    return None


def is_blackout(move_out: date) -> bool:
    return exit_window(move_out) is None


@dataclass(frozen=True)
class StaffExitException:
    """A10(e)/A6(d) evidence for ONE immutable candidate: `context_ref` is an opaque reference the CALLER builds from the
    candidate snapshot (enquiry or group identity + apartment + move-in/move-out + relevant membership/version); the helper only
    compares it. The exception names the specific approved move-out date and the increased WHOLE-apartment weekly total
    (including any extra-bed charge) staff set for that booking, who approved it and when (ISO date-time with timezone).
    Applies only to that exact context, date and price; a proposal priced at the normal rent is not covered."""
    context_ref: str
    move_out: date
    exception_total_weekly_rent: Decimal
    approved_by: str
    approved_at: str

    def __post_init__(self):
        object.__setattr__(self, "context_ref", ev.clean_text(self.context_ref, "context_ref"))
        object.__setattr__(self, "move_out", _date(self.move_out, "move_out"))
        object.__setattr__(self, "exception_total_weekly_rent", ev.clean_positive_money(self.exception_total_weekly_rent, "exception_total_weekly_rent"))
        object.__setattr__(self, "approved_by", ev.clean_text(self.approved_by, "approved_by", max_len=80))
        object.__setattr__(self, "approved_at", ev.clean_timestamp(self.approved_at, "approved_at"))

    def covers(self, context_ref: str, move_out: date, normal_total_weekly_rent, proposed_total_weekly_rent) -> tuple[bool, str]:
        """(True, '') when this record authorises the proposal: same context, same move-out, exception total strictly above
        the normal total (A6(d) "higher weekly rent"; no fixed uplift), and the proposal priced at exactly that total."""
        if ev.clean_text(context_ref, "context_ref") != self.context_ref:
            return False, "exception was approved for a different candidate"
        if _date(move_out, "move_out") != self.move_out:
            return False, "exception was approved for a different move-out date"
        normal = ev.clean_positive_money(normal_total_weekly_rent, "normal_total_weekly_rent", tier=xd.AGGREGATE)   # rent + A5
        proposed = ev.clean_positive_money(proposed_total_weekly_rent, "proposed_total_weekly_rent")
        if self.exception_total_weekly_rent <= normal:
            return False, "exception total is not above the normal total"
        if proposed != self.exception_total_weekly_rent:
            return False, "proposal is not priced at the authorised exception total"
        return True, ""


@dataclass(frozen=True)
class DateCheck:
    passed: bool
    reason_code: str
    detail: str = ""
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "reason_code": self.reason_code, "detail": self.detail, "facts": dict(self.facts)}


def check_exit(move_out: date, *, context_ref: Optional[str] = None, normal_total_weekly_rent=None, proposed_total_weekly_rent=None,
               staff_exception: Optional[StaffExitException] = None) -> DateCheck:
    """A10(b)/(c). A blackout move-out passes ONLY with a StaffExitException that covers this candidate context, this date and
    this proposal's exact total; with anything missing or mismatched the result is a failure that says why."""
    move_out = _date(move_out, "move_out")
    label = exit_window(move_out)
    if label is not None:
        return DateCheck(True, EXIT_IN_WINDOW, f"Move-out {move_out.isoformat()} is in the normal window {label}", {"window": label})
    if staff_exception is None:
        return DateCheck(False, EXIT_BLACKOUT_NEEDS_EXCEPTION,
                         f"Move-out {move_out.isoformat()} is a blackout day: needs staff approval of this specific date with a booking-specific rent")
    if context_ref is None or normal_total_weekly_rent is None or proposed_total_weekly_rent is None:
        return DateCheck(False, EXIT_EXCEPTION_INVALID, "an exit exception can only be applied to a priced candidate (context, normal total and proposed total required)")
    ok, why = staff_exception.covers(context_ref, move_out, normal_total_weekly_rent, proposed_total_weekly_rent)
    facts = {"normal_total_weekly_rent": ev.fmt_money(ev.clean_positive_money(normal_total_weekly_rent, "normal_total_weekly_rent", tier=xd.AGGREGATE)),
             "proposed_total_weekly_rent": ev.fmt_money(ev.clean_positive_money(proposed_total_weekly_rent, "proposed_total_weekly_rent")),
             "exception_total_weekly_rent": ev.fmt_money(staff_exception.exception_total_weekly_rent),
             "approved_by": staff_exception.approved_by, "approved_at": staff_exception.approved_at}
    if not ok:
        return DateCheck(False, EXIT_EXCEPTION_INVALID, f"Move-out {move_out.isoformat()} is a blackout day and the exception does not apply: {why}", facts)
    return DateCheck(True, EXIT_BLACKOUT_STAFF_EXCEPTION,
                     f"Move-out {move_out.isoformat()} is a blackout day; staff exception approved by {staff_exception.approved_by} "
                     f"for this candidate at the booking-specific total", facts)


def check_min_lease(move_in: date, move_out: date, min_lease_months) -> DateCheck:
    """A9 in calendar months (Bill, 8 November 2026): the stay meets the minimum when the move-out is on or after
    add_calendar_months(move_in, minimum). A fractional minimum is held for staff (LEASE_MINIMUM_FRACTIONAL_HOLD), not rounded."""
    move_in, move_out = _date(move_in, "move_in"), _date(move_out, "move_out")
    if move_out <= move_in:
        return DateCheck(False, DATES_INVALID, "Move-out must be after move-in")
    try:
        minimum = _min_lease_months(min_lease_months)
    except ValueError as e:   # a stored value that is not a usable minimum: hold for staff, never default to 2 or crash the run
        return DateCheck(False, LEASE_MINIMUM_INVALID_HOLD, f"This apartment's minimum lease setting is not usable ({e}); staff must correct it before the stay can be assessed (A9)",
                         {"min_lease_months": _bounded_repr(min_lease_months)})
    facts = {"min_lease_months": str(minimum) if isinstance(minimum, Decimal) else minimum,   # JSON-safe, exact
             "month_basis": "calendar months, same day or last day of month (A9, 8 November 2026)"}
    try:
        earliest = min_lease_end(move_in, minimum)
    except ValueError as e:   # the minimum is valid but the move-in is so late the calendar ends first (date.max neighbourhood)
        return DateCheck(False, DATES_INVALID, f"Cannot compute the earliest move-out: {e}", facts)
    if earliest is None:
        return DateCheck(False, LEASE_MINIMUM_FRACTIONAL_HOLD,
                         f"This apartment's minimum lease is {minimum:g} months, which is not a whole number of calendar months; "
                         f"staff must confirm the minimum before the stay can be assessed (A9; not rounded)", facts)
    facts["earliest_move_out"] = earliest.isoformat()
    if move_out < earliest:
        return DateCheck(False, LEASE_BELOW_MINIMUM,
                         f"Move-out {move_out.isoformat()} is before {earliest.isoformat()}, the earliest date that meets this apartment's "
                         f"minimum of {minimum:g} calendar months from {move_in.isoformat()}", facts)
    return DateCheck(True, LEASE_LENGTH_OK, f"Stay from {move_in.isoformat()} to {move_out.isoformat()} meets the minimum of {minimum:g} calendar months", facts)


def stay_days(move_in: date, move_out: date) -> int:
    """The final agreed stay from move-in to move-out, in days (A17(b))."""
    return (_date(move_out, "move_out") - _date(move_in, "move_in")).days


def permitted_gap_days(move_in: date, move_out: date) -> int:
    """A17(b): 5% of the stay, rounded down to whole days. Integer arithmetic: floor(days × 5/100) == days // 20 exactly, so the
    result cannot depend on the caller's Decimal context (at precision 3, Decimal(1999) × 0.05 rounded to 100 instead of 99)."""
    days = stay_days(move_in, move_out)
    if days <= 0:
        return 0
    return days // VACANCY_CAP_DIVISOR


def future_vacancy_days(today: date, available_from: date, move_in: date) -> int:
    """A17(a): empty days from the later of today and the available date to the move-in; elapsed vacancy is ignored.
    Negative when the move-in precedes the availability (handled by check_vacancy as START_BEFORE_AVAILABLE)."""
    return (_date(move_in, "move_in") - max(_date(today, "today"), _date(available_from, "available_from"))).days


def check_vacancy(today: date, available_from: date, move_in: date, move_out: date) -> DateCheck:
    today, available_from = _date(today, "today"), _date(available_from, "available_from")
    move_in, move_out = _date(move_in, "move_in"), _date(move_out, "move_out")
    if move_out <= move_in:
        return DateCheck(False, DATES_INVALID, "Move-out must be after move-in")
    if move_in < available_from:
        return DateCheck(False, START_BEFORE_AVAILABLE,
                         f"Move-in {move_in.isoformat()} is before the apartment is available ({available_from.isoformat()})",
                         {"available_from": available_from.isoformat()})
    gap = max(0, future_vacancy_days(today, available_from, move_in))
    cap = permitted_gap_days(move_in, move_out)
    facts = {"future_empty_days": gap, "permitted_days": cap, "stay_days": stay_days(move_in, move_out)}
    if gap > cap:
        return DateCheck(False, VACANCY_CAP_EXCEEDED, f"{gap} future empty days exceed the permitted {cap} (5% of a {facts['stay_days']}-day stay)", facts)
    return DateCheck(True, VACANCY_OK, f"{gap} future empty days within the permitted {cap}", facts)


def rate_kind(move_in: date, move_out: date) -> str:
    """A6 via the existing implementation: '6m' or '12m'."""
    return rules.rate_kind(_date(move_in, "move_in"), _date(move_out, "move_out"))


@dataclass(frozen=True)
class DateAssessment:
    passed: bool
    checks: tuple
    rate_kind: str

    @property
    def reason_codes(self) -> list[str]:
        return [c.reason_code for c in self.checks]

    def as_dict(self) -> dict:
        return {"passed": self.passed, "rate_kind": self.rate_kind, "checks": [c.as_dict() for c in self.checks]}


def assess_dates(today: date, available_from: date, move_in: date, move_out: date, min_lease_months, *,
                 context_ref: Optional[str] = None, normal_total_weekly_rent=None, proposed_total_weekly_rent=None,
                 staff_exception: Optional[StaffExitException] = None) -> DateAssessment:
    """The three date checks (A9 minimum lease in calendar months, A10 exit, A17 vacancy) for ONE concrete proposed
    move-in / move-out against ONE apartment's availability, plus the A6 rate kind. Not a full eligibility check: date
    flexibility, accepted date limits, capacity and budget are the eligibility layer's job. Every check is reported so staff
    see every failing reason, not just the first; A17(c): no check waives another."""
    checks = (check_min_lease(move_in, move_out, min_lease_months),
              check_exit(move_out, context_ref=context_ref, normal_total_weekly_rent=normal_total_weekly_rent,
                         proposed_total_weekly_rent=proposed_total_weekly_rent, staff_exception=staff_exception),
              check_vacancy(today, available_from, move_in, move_out))
    return DateAssessment(all(c.passed for c in checks), checks, rate_kind(move_in, move_out) if move_out > move_in else "12m")
