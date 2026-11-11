"""Phase A acceptance tests: server/budget.py (A16, A5, A10(e) agreement evidence) and server/lease_dates.py (A10, A17, A9, A6).
Pure functions; no database, no server, no personal attributes. Boundary days and worked examples are taken verbatim from
the register text; nothing here changes the legacy rules.py behaviour."""
from datetime import date
from decimal import Decimal

import pytest

from server import budget, exception_rent as xr, lease_dates as ld, rules


# ------------------------------------------------------------------------------------------------------------ A16 budget
@pytest.mark.parametrize("code,pct", [("Studio", "0.10"), ("1BR", "0.10"), ("2B1B", "0.10"), ("2B2B", "0.10"), ("3B", "0.20")])
def test_a16_allowance_percentages(code, pct):
    assert budget.allowance_pct(code) == Decimal(pct)
    assert budget.allowance_pct(code.lower()) == Decimal(pct)  # type codes normalised as elsewhere


def test_a16_limit_is_exact_decimal_arithmetic():
    # 1005 x 1.10 = 1105.50 exactly. In float arithmetic 1005 * 1.10 is 1105.5000000000002, so a rent of 1105.5000000000001
    # (over the true limit) would be a FALSE PASS; Decimal keeps the limit exact and the cent above it fails.
    assert budget.budget_limit(1005, "2B2B") == Decimal("1105.50")
    r = budget.check_total_budget("1105.50", "2B2B", 4, 1005)
    assert r.passed and r.reason_code == budget.WITHIN_ALLOWANCE and r.limit == Decimal("1105.50")
    r = budget.check_total_budget("1105.51", "2B2B", 4, 1005)
    assert not r.passed and r.reason_code == budget.OVER_ALLOWANCE
    assert budget.cents(Decimal("1105.505")) == Decimal("1105.51")  # presentation only


def test_a16_boundaries_per_type():
    # at the maximum → WITHIN_MAX; one cent above → WITHIN_ALLOWANCE; at the limit → pass; one cent over → excluded
    for code, maximum, limit in (("Studio", 500, "550.00"), ("1BR", 600, "660.00"), ("2B1B", 900, "990.00"), ("2B2B", 1000, "1100.00"), ("3B", 1500, "1800.00")):
        assert budget.check_total_budget(maximum, code, 1, maximum).reason_code == budget.WITHIN_MAX
        assert budget.check_total_budget(Decimal(maximum) + Decimal("0.01"), code, 1, maximum).reason_code == budget.WITHIN_ALLOWANCE
        at = budget.check_total_budget(limit, code, 1, maximum)
        assert at.passed and at.limit == Decimal(limit)
        over = budget.check_total_budget(Decimal(limit) + Decimal("0.01"), code, 1, maximum)
        assert not over.passed and over.reason_code == budget.OVER_ALLOWANCE


def test_a16_total_includes_a5_extra_bed_charge():
    # 2-bedroom at $1,000 with 5 people: 5th bed adds $70 (A5(b)) → $1,070 tested against the limit, not $1,000
    r4 = budget.check_total_budget(1000, "2B2B", 4, 1000)
    r5 = budget.check_total_budget(1000, "2B2B", 5, 1000)
    assert r4.extra_bed_charge == 0 and r4.total_weekly_rent == 1000 and r4.reason_code == budget.WITHIN_MAX
    assert r5.extra_bed_charge == 70 and r5.total_weekly_rent == 1070 and r5.reason_code == budget.WITHIN_ALLOWANCE
    # with a $972 maximum the 5-person total ($1,070) exceeds 10% while the bare rent would not
    assert budget.check_total_budget(1000, "2B2B", 4, 972).passed
    assert not budget.check_total_budget(1000, "2B2B", 5, 972).passed
    # A5 thresholds: 3rd bed studio/1BR, 5th bed 2-bedroom, 7th bed 3-bedroom
    assert budget.extra_bed_charge("Studio", 2) == 0 and budget.extra_bed_charge("Studio", 3) == 70
    assert budget.extra_bed_charge("1BR", 3) == 70 and budget.extra_bed_charge("2B1B", 4) == 0 and budget.extra_bed_charge("2B1B", 5) == 70
    assert budget.extra_bed_charge("3B", 6) == 0 and budget.extra_bed_charge("3B", 7) == 70


def test_a16_matebot_baseline_is_sum_of_maximums_and_no_per_person_ceiling():
    base = budget.matebot_baseline([300, 300, "400"])
    assert base == Decimal(1000)
    # 3B group of six, rent $1,150: within 20% of $1,000. A share above 110% of one member's own maximum is NOT tested (A16)
    r = budget.check_total_budget(1150, "3B", 6, budget.matebot_baseline([100, 100, 200, 200, 200, 200]))
    assert r.passed and r.reason_code == budget.WITHIN_ALLOWANCE and r.limit == Decimal("1200.0")
    with pytest.raises(ValueError):
        budget.matebot_baseline([])


def test_a16_checker_has_no_override_parameter():
    import inspect
    assert list(inspect.signature(budget.check_total_budget).parameters) == ["weekly_rent", "type_code", "people", "whole_apartment_max"]
    assert not budget.check_total_budget(1200, "2B2B", 4, 1000).passed


def test_money_inputs_and_serialisation():
    assert budget.money(1105.5) == Decimal("1105.5") and budget.money("12") == 12 and budget.money(0) == 0
    for bad in (None, True, False, [1], {"v": 1}, object()):
        with pytest.raises(TypeError):
            budget.money(bad)
    for bad in ("nan", "inf", "-inf", float("nan"), float("inf"), -1, "-0.01", "abc", ""):
        with pytest.raises(ValueError):
            budget.money(bad)
    with pytest.raises(ValueError):
        budget.money(0, positive=True)


def test_fail_closed_inputs_cannot_make_a_budget_pass():
    # an infinite/NaN/negative maximum or rent, or a float/bool head count, is an error — never a pass
    for maximum in (float("inf"), "Infinity", "NaN", -1000, 0):
        with pytest.raises((ValueError, TypeError)):
            budget.check_total_budget(1000, "2B2B", 4, maximum)
    for rent in (-5, 0, "nan", None, True):
        with pytest.raises((ValueError, TypeError)):
            budget.check_total_budget(rent, "2B2B", 4, 1000)
    for people in (4.0, 4.9, True, 0, -1, "4", None):
        with pytest.raises((ValueError, TypeError)):
            budget.check_total_budget(1000, "2B2B", people, 1000)
    for member in (0, -100, "nan", float("inf")):
        with pytest.raises(ValueError):
            budget.matebot_baseline([300, member])
    d = budget.check_total_budget(1000, "3B", 7, 1000).as_dict()
    assert d == {"passed": True, "reason_code": budget.WITHIN_ALLOWANCE, "total_weekly_rent": "1070.00", "whole_apartment_max": "1000.00",
                 "allowance_pct": "0.20", "limit": "1200.00", "extra_bed_charge": "70.00"}


# ---------------------------------------------------------------------------------------------------------- A10 windows
@pytest.mark.parametrize("d,label", [
    (date(2027, 1, 10), "10 January to 20 February"), (date(2027, 2, 20), "10 January to 20 February"),
    (date(2027, 7, 1), "1 to 20 July"), (date(2027, 7, 20), "1 to 20 July"),
    (date(2027, 10, 5), "5 October to 7 December"), (date(2027, 12, 7), "5 October to 7 December"),
    (date(2028, 2, 20), "10 January to 20 February"),  # every year, leap year included
])
def test_a10b_window_endpoints_are_inclusive(d, label):
    assert ld.exit_window(d) == label and not ld.is_blackout(d)
    assert ld.check_exit(d).passed and ld.check_exit(d).reason_code == ld.EXIT_IN_WINDOW


@pytest.mark.parametrize("d", [
    date(2027, 1, 9), date(2027, 2, 21), date(2027, 6, 30), date(2027, 7, 21), date(2027, 10, 4), date(2027, 12, 8),
    date(2027, 12, 31), date(2028, 1, 1), date(2028, 1, 9), date(2027, 4, 15), date(2027, 8, 31),
])
def test_a10c_blackout_days_need_an_exception_never_silent(d):
    assert ld.is_blackout(d)
    r = ld.check_exit(d)
    assert not r.passed and r.reason_code == ld.EXIT_BLACKOUT_NEEDS_EXCEPTION


CTX = "homebot:12|apt:77|in:2027-02-01|out:2027-03-15|members:v3"
TS = "2026-11-06T09:00:00+11:00"


def _exc(total="1250", ctx=CTX, day=date(2027, 3, 15), by="anna", at=TS):
    return ld.StaffExitException(ctx, day, total, by, at)  # raw total: the record itself validates and converts


def test_a10e_exception_is_evidence_bound_to_candidate_date_and_exact_total():
    blackout = date(2027, 3, 15)
    exc = _exc()
    kw = dict(context_ref=CTX, normal_total_weekly_rent=Decimal("1070"), proposed_total_weekly_rent=Decimal("1250"), staff_exception=exc)
    r = ld.check_exit(blackout, **kw)
    assert r.passed and r.reason_code == ld.EXIT_BLACKOUT_STAFF_EXCEPTION
    assert r.facts["normal_total_weekly_rent"] == "1070.00" and r.facts["exception_total_weekly_rent"] == "1250.00" and r.facts["proposed_total_weekly_rent"] == "1250.00"
    # no exception at all → needs one; exception without the priced-candidate context → invalid
    assert ld.check_exit(blackout).reason_code == ld.EXIT_BLACKOUT_NEEDS_EXCEPTION
    assert ld.check_exit(blackout, staff_exception=exc).reason_code == ld.EXIT_EXCEPTION_INVALID
    # different candidate context (another apartment / dates / membership) → not covered
    assert not ld.check_exit(blackout, **dict(kw, context_ref=CTX.replace("apt:77", "apt:78"))).passed
    assert not ld.check_exit(blackout, **dict(kw, context_ref=CTX.replace("v3", "v4"))).passed
    # different date → not covered
    assert not ld.check_exit(date(2027, 3, 16), **kw).passed
    # proposal priced at the normal total while a higher authorised price exists → NOT valid
    assert not ld.check_exit(blackout, **dict(kw, proposed_total_weekly_rent=Decimal("1070"))).passed
    assert not ld.check_exit(blackout, **dict(kw, proposed_total_weekly_rent=Decimal("1250.01"))).passed
    # exception total equal to or below the normal total is not an "increased rent"
    assert not ld.check_exit(blackout, **dict(kw, staff_exception=_exc("1070"), proposed_total_weekly_rent=Decimal("1070"))).passed
    assert not ld.check_exit(blackout, **dict(kw, staff_exception=_exc("900"), proposed_total_weekly_rent=Decimal("900"))).passed
    # not needed, not reported, when the date is already in a window
    assert ld.check_exit(date(2027, 7, 10), **kw).reason_code == ld.EXIT_IN_WINDOW


def test_a10e_exception_record_rejects_non_evidence():
    for bad in (dict(by="   "), dict(by=""), dict(by=None), dict(ctx=" "), dict(ctx=12), dict(at="2026-11-06"), dict(at="2026-11-06T09:00:00"),
                dict(at="yesterday"), dict(at=""), dict(total="nan"), dict(total="-1250"), dict(total="0"), dict(total=float("inf")), dict(total=True),
                dict(day="2027-03-15")):
        with pytest.raises((TypeError, ValueError)):
            _exc(**bad)
    assert _exc(at="2026-11-06T09:00:00Z").approved_at == "2026-11-06T09:00:00+00:00"
    assert _exc(by="  anna ").approved_by == "anna"


def test_exception_rent_combined_helper():
    blackout = date(2027, 3, 15)
    exc = _exc("1250")
    base = dict(context_ref=CTX, type_code="2B2B", people=5, whole_apartment_max=1000, move_out=blackout, normal_weekly_rent=1000)
    # normal total 1000 + 70 extra bed = 1070; exception total 1250 is a whole-apartment figure (extra bed not added again)
    r = xr.check_exception_rent(**base, proposed_total_weekly_rent=1250, staff_exception=exc)
    assert not r.passed and r.reason_code == xr.OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT
    assert r.facts["normal_total_weekly_rent"] == "1070.00" and r.facts["exception_total_weekly_rent"] == "1250.00" and r.facts["limit"] == "1100.00"
    agree = xr.CustomerAgreement(CTX, Decimal("1250"), "mylene", TS)
    r = xr.check_exception_rent(**base, proposed_total_weekly_rent=1250, staff_exception=exc, customer_agreement=agree)
    assert r.passed and r.reason_code == xr.EXCEPTION_RENT_OVER_ALLOWANCE_CUSTOMER_AGREED and r.facts["recorded_by"] == "mylene"
    # agreement for another candidate, or another amount, is not evidence
    assert not xr.check_exception_rent(**base, proposed_total_weekly_rent=1250, staff_exception=exc,
                                       customer_agreement=xr.CustomerAgreement(CTX + "|x", Decimal("1250"), "mylene", TS)).passed
    assert not xr.check_exception_rent(**base, proposed_total_weekly_rent=1250, staff_exception=exc,
                                       customer_agreement=xr.CustomerAgreement(CTX, Decimal("1200"), "mylene", TS)).passed
    # exception total within the allowance needs no agreement (A16 holds), but the exit exception must still cover the proposal
    r = xr.check_exception_rent(**base, proposed_total_weekly_rent=1100, staff_exception=_exc("1100"))
    assert r.passed and r.reason_code == xr.EXCEPTION_RENT_WITHIN_ALLOWANCE
    r = xr.check_exception_rent(**base, proposed_total_weekly_rent=1070, staff_exception=_exc("1100"))  # priced at normal, not authorised
    assert not r.passed and r.reason_code == ld.EXIT_EXCEPTION_INVALID
    r = xr.check_exception_rent(**base, proposed_total_weekly_rent=1250, staff_exception=None)
    assert not r.passed and r.reason_code == ld.EXIT_BLACKOUT_NEEDS_EXCEPTION
    # an ordinary (in-window) booking can never use an agreement to exceed A16
    r = xr.check_exception_rent(**dict(base, move_out=date(2027, 7, 10)), proposed_total_weekly_rent=1250, staff_exception=exc, customer_agreement=agree)
    assert not r.passed and r.reason_code == xr.EXCEPTION_NOT_APPLICABLE
    for bad in (dict(recorded_by=" "), dict(recorded_at="2026-11-06"), dict(agreed="nan"), dict(ctx="")):
        with pytest.raises((TypeError, ValueError)):
            xr.CustomerAgreement(bad.get("ctx", CTX), Decimal(bad.get("agreed", "1250")) if bad.get("agreed") != "nan" else Decimal("nan"),
                                 bad.get("recorded_by", "mylene"), bad.get("recorded_at", TS))


# ------------------------------------------------------------------------------------------------------- A17 vacancy cap
def test_a17b_permitted_gap_examples_round_down():
    assert ld.permitted_gap_days(date(2027, 2, 1), date(2028, 2, 1)) == 18      # 365 days → 18
    assert ld.permitted_gap_days(date(2027, 1, 1), date(2027, 6, 30)) == 9      # 180 days → 9
    assert ld.permitted_gap_days(date(2027, 1, 1), date(2027, 1, 20)) == 0      # 19 days → 0.95 → 0
    assert ld.permitted_gap_days(date(2027, 1, 1), date(2027, 1, 21)) == 1      # 20 days → 1
    assert ld.permitted_gap_days(date(2027, 1, 1), date(2027, 1, 1)) == 0


def test_a17a_only_future_empty_days_count():
    today, avail = date(2026, 11, 6), date(2026, 10, 7)  # empty for 30 days already
    move_in, move_out = date(2026, 11, 13), date(2027, 11, 13)  # start in seven days, 365-day stay
    assert ld.future_vacancy_days(today, avail, move_in) == 7
    r = ld.check_vacancy(today, avail, move_in, move_out)
    assert r.passed and r.facts == {"future_empty_days": 7, "permitted_days": 18, "stay_days": 365}
    # future availability: counted from the available date, not from today
    assert ld.future_vacancy_days(today, date(2027, 1, 1), date(2027, 1, 15)) == 14


def test_a17c_cap_boundary_and_no_start_before_availability():
    today = avail = date(2027, 1, 1)
    ok = ld.check_vacancy(today, avail, date(2027, 1, 19), date(2028, 1, 19))      # 18 days gap, 365-day stay → cap 18
    over = ld.check_vacancy(today, avail, date(2027, 1, 20), date(2028, 1, 20))    # 19 days gap → excluded
    assert ok.passed and ok.reason_code == ld.VACANCY_OK
    assert not over.passed and over.reason_code == ld.VACANCY_CAP_EXCEEDED
    early = ld.check_vacancy(date(2026, 12, 1), date(2027, 1, 10), date(2027, 1, 5), date(2028, 1, 5))
    assert not early.passed and early.reason_code == ld.START_BEFORE_AVAILABLE
    # recalculated when the dates change: a shorter stay permits fewer empty days
    assert not ld.check_vacancy(today, avail, date(2027, 1, 19), date(2027, 6, 30)).passed


# ---------------------------------------------------------------------------------------------------- A9 / A6 / combined
def test_a9_minimum_lease_is_the_apartments_own_minimum():
    assert ld.check_min_lease(date(2027, 2, 1), date(2027, 4, 15), 2).passed
    # A9 month basis ruled by Bill, 8 November 2026: CALENDAR months (same day, or the last day of the month). 1 Feb → 1 Apr
    # (59 days) IS two months; 1 Feb → 31 Mar is not. The former days / 30.4375 reading is withdrawn.
    ok = ld.check_min_lease(date(2027, 2, 1), date(2027, 4, 1), 2)
    assert ok.passed and ok.reason_code == ld.LEASE_LENGTH_OK and ok.facts["earliest_move_out"] == "2027-04-01"
    assert ld.check_min_lease(date(2027, 2, 1), date(2027, 3, 31), 2).reason_code == ld.LEASE_BELOW_MINIMUM
    short = ld.check_min_lease(date(2027, 2, 1), date(2027, 3, 20), 2)
    assert not short.passed and short.reason_code == ld.LEASE_BELOW_MINIMUM
    assert ld.check_min_lease(date(2027, 2, 1), date(2027, 3, 20), 1).passed   # staff-set 1-month minimum (A9(a)/(b))
    assert ld.check_min_lease(date(2027, 2, 1), date(2027, 2, 1), 1).reason_code == ld.DATES_INVALID


def test_a9_calendar_month_arithmetic_and_month_end_clamping():
    add = ld.add_calendar_months
    assert add(date(2027, 1, 31), 1) == date(2027, 2, 28)     # no 31 Feb: last day of February
    assert add(date(2028, 1, 31), 1) == date(2028, 2, 29)     # leap year
    assert add(date(2027, 11, 30), 3) == date(2028, 2, 29)    # across the year boundary into a leap February
    assert add(date(2027, 8, 31), 1) == date(2027, 9, 30)
    assert add(date(2027, 3, 15), 12) == date(2028, 3, 15)
    assert add(date(2027, 2, 1), 0) == date(2027, 2, 1)
    for bad in (1.5, -1, True, "2"):
        with pytest.raises(ValueError):
            add(date(2027, 2, 1), bad)
    # clamped boundary: 31 Jan → 28 Feb meets a 1-month minimum; 27 Feb does not
    assert ld.check_min_lease(date(2027, 1, 31), date(2027, 2, 28), 1).passed
    assert ld.check_min_lease(date(2027, 1, 31), date(2027, 2, 27), 1).reason_code == ld.LEASE_BELOW_MINIMUM
    # 12 months: 1 Feb 2027 → 1 Feb 2028 passes, 31 Jan 2028 fails (365 days; the old 30.4375 reading called it 11.99 months too)
    assert ld.check_min_lease(date(2027, 2, 1), date(2028, 2, 1), 12).passed
    assert not ld.check_min_lease(date(2027, 2, 1), date(2028, 1, 31), 12).passed


def test_a9_fractional_minimum_is_held_not_rounded():
    held = ld.check_min_lease(date(2027, 2, 1), date(2027, 6, 1), 1.5)
    assert not held.passed and held.reason_code == ld.LEASE_MINIMUM_FRACTIONAL_HOLD and "not rounded" in held.detail
    assert ld.min_lease_end(date(2027, 2, 1), 1.5) is None
    assert ld.min_lease_end(date(2027, 2, 1), 2.0) == date(2027, 4, 1)   # a whole number given as a float is whole
    assert ld.min_lease_end(date(2027, 2, 1), Decimal("2")) == date(2027, 4, 1) and ld.min_lease_end(date(2027, 2, 1), Decimal("2.000")) == date(2027, 4, 1)
    tiny = Decimal("1.00000000000000001")   # float(tiny) == 1.0: must still be held as fractional, not silently whole
    assert ld.min_lease_end(date(2027, 2, 1), tiny) is None
    assert ld.check_min_lease(date(2027, 2, 1), date(2027, 6, 1), tiny).reason_code == ld.LEASE_MINIMUM_FRACTIONAL_HOLD
    assert not rules.exit_ok(date(2027, 2, 1), date(2027, 7, 10), tiny)[0]
    # the legacy engine path (rules.exit_ok, used by choose_lease_dates) applies the same basis and the same hold
    assert rules.exit_ok(date(2027, 2, 1), date(2027, 4, 1), 2)[0] is True or "minimum" not in rules.exit_ok(date(2027, 2, 1), date(2027, 4, 1), 2)[1]
    ok, why = rules.exit_ok(date(2027, 2, 1), date(2027, 3, 31), 2)
    assert not ok and "minimum" in why and "2027-04-01" in why
    ok, why = rules.exit_ok(date(2027, 2, 1), date(2027, 7, 10), 1.5)
    assert not ok and "not a whole number" in why
    assert not hasattr(rules, "months_between")


def test_date_and_min_lease_inputs_fail_closed():
    from datetime import datetime
    d1, d2 = date(2027, 2, 1), date(2027, 4, 15)
    for bad in (datetime(2027, 2, 1, 10), "2027-02-01", None, 20270201):
        with pytest.raises(TypeError):
            ld.exit_window(bad)
        with pytest.raises(TypeError):
            ld.check_vacancy(date(2026, 11, 6), d1, bad, d2)
        with pytest.raises(TypeError):
            ld.check_min_lease(bad, d2, 2)
    # wrong TYPE is a programming error
    for bad in (True, "2", None, [2]):
        with pytest.raises(TypeError):
            ld.check_min_lease(d1, d2, bad)
        with pytest.raises(TypeError):
            ld.min_lease_end(d1, bad)
    # a wrong VALUE in a stored setting is a staff hold, never a default to 2 and never a crash (calendar hardening, 8 Nov 2026)
    for bad in (float("nan"), float("inf"), float("-inf"), 0, -2, 0.0, Decimal("NaN"), Decimal("Infinity"), Decimal("0"), Decimal("-1")):
        r = ld.check_min_lease(d1, d2, bad)
        assert not r.passed and r.reason_code == ld.LEASE_MINIMUM_INVALID_HOLD and "staff must correct" in r.detail, bad
        with pytest.raises(ValueError):
            ld.min_lease_end(d1, bad)


def test_calendar_hardening_huge_minimums_and_date_range_hold_not_crash():
    """Engineering bound (not a rule): a minimum beyond MAX_MIN_LEASE_MONTHS (100 years) or a move-in so late that N months
    overflow the calendar can never reach date() arithmetic; the check holds with a code and the engine returns a reason."""
    d1, d2 = date(2027, 2, 1), date(2027, 7, 10)
    assert ld.MAX_MIN_LEASE_MONTHS == 1200
    for huge in (Decimal("1E+100000000"), Decimal("1E+308"), 1e308, 10 ** 30, 1201, Decimal("1200.5") + 1, 1200.000001):
        r = ld.check_min_lease(d1, d2, huge)
        assert r.reason_code == ld.LEASE_MINIMUM_INVALID_HOLD and "beyond the supported validation limit of 1200 months" in r.detail, huge
        assert r.facts["min_lease_months"] == (repr(huge) if huge != 10 ** 30 else "int(~31 digits)")
        ok, why = rules.exit_ok(d1, d2, huge)
        assert ok is False and "cannot be applied" in why and "staff must correct it (A9)" in why
    # ints beyond float range: the limit is compared as an int before any float(), and the diagnostics never print the digits
    # (repr(10**10000) itself raises ValueError under Python's int-to-str digit limit)
    for n in (10 ** 1000, 10 ** 10000, -(10 ** 1000), -(10 ** 10000)):
        r = ld.check_min_lease(d1, d2, n)
        assert r.reason_code == ld.LEASE_MINIMUM_INVALID_HOLD, n > 0
        assert r.facts["min_lease_months"].startswith("-int(~" if n < 0 else "int(~") and len(r.facts["min_lease_months"]) < 30
        assert len(r.detail) < 300 and ("validation limit" in r.detail if n > 0 else "positive finite" in r.detail)
        ok, why = rules.exit_ok(d1, d2, n)
        assert ok is False and len(why) < 300
        with pytest.raises(ValueError):
            ld.min_lease_end(d1, n)
    assert ld.check_min_lease(d1, d2, 10 ** 15).facts["min_lease_months"] == "int(~16 digits)"
    assert ld.check_min_lease(d1, d2, 10 ** 15 - 1).facts["min_lease_months"] == "999999999999999"
    # exactly at the bound is still a real (if absurd) minimum: evaluated, not held
    at = ld.check_min_lease(d1, d2, 1200)
    assert at.reason_code == ld.LEASE_BELOW_MINIMUM and at.facts["earliest_move_out"] == "2127-02-01"
    assert ld.min_lease_end(d1, Decimal("1200")) == date(2127, 2, 1)
    # a tiny fractional minimum is a fractional hold (A9), not an invalid one
    assert ld.check_min_lease(d1, d2, Decimal("1E-30")).reason_code == ld.LEASE_MINIMUM_FRACTIONAL_HOLD
    assert ld.check_min_lease(d1, d2, 1e-300).reason_code == ld.LEASE_MINIMUM_FRACTIONAL_HOLD
    # the calendar's end: 2 months after 1 Nov 9999 does not exist
    with pytest.raises(ValueError, match="beyond the supported calendar"):
        ld.add_calendar_months(date(9999, 11, 1), 2)
    late = ld.check_min_lease(date(9999, 11, 1), date(9999, 12, 31), 2)
    assert not late.passed and late.reason_code == ld.DATES_INVALID and "beyond the supported calendar" in late.detail
    ok, why = rules.exit_ok(date(9999, 11, 1), date(9999, 12, 31), 2)
    assert ok is False and "beyond the supported calendar" in why
    assert ld.check_min_lease(date(9999, 10, 1), date(9999, 12, 7), 2).passed   # 1 Dec 9999 exists
    assert ld.add_calendar_months(date(9999, 10, 31), 1) == date(9999, 11, 30)
    # the staff-facing hold text never claims a default was applied
    assert "2 months" not in ld.check_min_lease(d1, d2, 0).detail


def test_a6_rate_kind_reused():
    assert ld.rate_kind(date(2027, 2, 1), date(2027, 7, 10)) == "6m"
    assert ld.rate_kind(date(2027, 3, 1), date(2027, 7, 10)) == "12m"
    assert ld.rate_kind(date(2027, 2, 1), date(2028, 2, 1)) == "12m"


def test_assess_dates_reports_every_failing_reason_and_waives_nothing():
    today = date(2026, 11, 6)
    good = ld.assess_dates(today, date(2027, 1, 25), date(2027, 2, 1), date(2027, 7, 10), 2)  # 7 empty days, 159-day stay → cap 7
    assert good.passed and good.rate_kind == "6m" and good.reason_codes == [ld.LEASE_LENGTH_OK, ld.EXIT_IN_WINDOW, ld.VACANCY_OK]
    # blackout exit AND vacancy cap exceeded: both reported; a staff exit exception does not waive the vacancy failure (A17(c))
    exc = ld.StaffExitException("ctx-a", date(2027, 8, 1), Decimal("1300"), "anna", TS)
    bad = ld.assess_dates(today, date(2026, 11, 6), date(2027, 1, 15), date(2027, 8, 1), 2, context_ref="ctx-a",
                          normal_total_weekly_rent=1000, proposed_total_weekly_rent=1300, staff_exception=exc)
    assert not bad.passed
    assert bad.reason_codes == [ld.LEASE_LENGTH_OK, ld.EXIT_BLACKOUT_STAFF_EXCEPTION, ld.VACANCY_CAP_EXCEEDED]
    d = bad.as_dict()
    assert d["passed"] is False and len(d["checks"]) == 3 and d["checks"][2]["facts"]["future_empty_days"] == 70


# ------------------------------------------------------------- arithmetic must not depend on the caller's Decimal context
import decimal
from datetime import timedelta
from decimal import localcontext

from server import evidence_fields as ev, exact_decimal as xd

HOSTILE_CONTEXTS = [
    dict(prec=2), dict(prec=3), dict(prec=5), dict(prec=1), dict(prec=100),
    dict(prec=3, rounding=decimal.ROUND_UP), dict(prec=3, rounding=decimal.ROUND_DOWN), dict(prec=3, rounding=decimal.ROUND_CEILING),
    dict(prec=28, rounding=decimal.ROUND_FLOOR), dict(prec=28, Emax=3, Emin=-3), dict(prec=28, traps_off=True),
]


def _hostile(spec):
    c = localcontext()
    ctx = c.__enter__()
    for k, v in spec.items():
        if k == "traps_off":
            for sig in list(ctx.traps):
                ctx.traps[sig] = False
        else:
            setattr(ctx, k, v)
    return c


@pytest.mark.parametrize("spec", HOSTILE_CONTEXTS, ids=str)
def test_parent_reproduced_budget_regression_pinned(spec):
    # 1100.01 against max 1000 (limit 1100.00) was PASS at precision 3: both sum and product rounded to 1.10E+3.
    with _hostile(spec):
        r = budget.check_total_budget("1100.01", "2B2B", 4, 1000)
        assert not r.passed and r.reason_code == budget.OVER_ALLOWANCE
        assert r.total_weekly_rent == Decimal("1100.01") and r.limit == Decimal("1100.00")
        r = budget.check_total_budget("1100.00", "2B2B", 4, 1000)
        assert r.passed and r.reason_code == budget.WITHIN_ALLOWANCE
        # 1005 × 1.10 = 1105.50 exactly; A5 extra bed added exactly
        assert budget.budget_limit(1005, "2B2B") == Decimal("1105.50")
        assert budget.total_weekly_rent("1035.51", "2B2B", 5) == Decimal("1105.51")
        assert not budget.check_total_budget("1035.51", "2B2B", 5, 1005).passed
        assert budget.matebot_baseline(["333.33", "333.33", "333.34"]) == Decimal("1000.00")
        assert budget.matebot_baseline(["123456.789012", "0.000001"]) == Decimal("123456.789013")
        assert budget.cents(Decimal("1105.505")) == Decimal("1105.51")
        assert ev.fmt_money(Decimal("1105.505")) == "1105.51"


@pytest.mark.parametrize("spec", HOSTILE_CONTEXTS, ids=str)
def test_parent_reproduced_vacancy_floor_regression_pinned(spec):
    # 1999-day stay: floor(5%) is 99; Decimal(1999) × 0.05 at precision 3 rounded to 100.
    start = date(2027, 1, 1)
    with _hostile(spec):
        assert ld.permitted_gap_days(start, start + timedelta(days=1999)) == 99
        assert ld.permitted_gap_days(start, start + timedelta(days=2000)) == 100
        assert ld.permitted_gap_days(start, start + timedelta(days=19)) == 0
        assert ld.permitted_gap_days(start, start + timedelta(days=20)) == 1
        for days in (1, 7, 39, 40, 41, 365, 1999, 2000, 36500):
            assert ld.permitted_gap_days(start, start + timedelta(days=days)) == days // 20 == int(days * 5 // 100)


@pytest.mark.parametrize("spec", HOSTILE_CONTEXTS, ids=str)
def test_exception_rent_verdicts_context_independent(spec):
    blackout = date(2027, 3, 15)
    base = dict(context_ref=CTX, type_code="2B2B", people=5, whole_apartment_max=1000, move_out=blackout, normal_weekly_rent=1000)
    with _hostile(spec):
        # exception total 1100.01 is one cent over the exact limit 1100.00 → needs the customer's agreement (not a pass)
        r = xr.check_exception_rent(**base, proposed_total_weekly_rent="1100.01", staff_exception=_exc("1100.01"))
        assert not r.passed and r.reason_code == xr.OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT
        assert r.facts["limit"] == "1100.00" and r.facts["normal_total_weekly_rent"] == "1070.00"
        r = xr.check_exception_rent(**base, proposed_total_weekly_rent="1100.00", staff_exception=_exc("1100.00"))
        assert r.passed and r.reason_code == xr.EXCEPTION_RENT_WITHIN_ALLOWANCE
        # exact-total evidence matching is not loosened by a coarse context
        r = xr.check_exception_rent(**base, proposed_total_weekly_rent="1250.01", staff_exception=_exc("1250"))
        assert not r.passed and r.reason_code == ld.EXIT_EXCEPTION_INVALID
        agree = xr.CustomerAgreement(CTX, Decimal("1250.01"), "mylene", TS)
        assert not xr.check_exception_rent(**base, proposed_total_weekly_rent="1250", staff_exception=_exc("1250"), customer_agreement=agree).passed


def test_malformed_money_still_rejected_when_caller_disabled_invalid_operation_trap():
    with localcontext() as c:
        c.traps[decimal.InvalidOperation] = False
        assert Decimal("abc").is_nan()  # the hostile condition: parsing silently yields NaN in this context
        for bad in ("abc", "12abc", "NaN", "sNaN", "Infinity", ""):
            with pytest.raises(ValueError):
                budget.money(bad)
            with pytest.raises(ValueError):
                ev.clean_positive_money(bad, "x")
        assert budget.money("1105.5") == Decimal("1105.5")


def test_money_magnitude_guard_rejects_before_formatting_and_canonicalises_zero():
    # sign/exponent/adjusted() are examined before any arithmetic or text expansion; nothing is rounded
    for huge in (Decimal("1E+1000000"), Decimal("1E+12"), "1000000000000", Decimal("9.99E+12"), Decimal("1E-7"), "0.0000001"):
        with pytest.raises(ValueError):
            budget.money(huge)
        with pytest.raises(ValueError):
            ev.clean_positive_money(huge, "x")
    with pytest.raises(ValueError):
        budget.money(Decimal("1E-1000000"))
    with pytest.raises(ValueError):
        budget.money(Decimal("-0E+1000000"), positive=True)   # zero stays zero (not positive) even in exotic form
    z = budget.money(Decimal("-0E+1000000"))
    assert z == 0 and z.as_tuple() == Decimal(0).as_tuple()
    z = budget.money(Decimal("0E-1000000"))
    assert z.as_tuple() == Decimal(0).as_tuple() and str(z) == "0"
    ok = budget.money(Decimal("1.5E+3"))                      # positive exponent canonicalised, value unchanged
    assert ok == Decimal("1500") and str(ok) == "1500" and ok.as_tuple().exponent == 0
    assert budget.money("999999999999.999999") == Decimal("999999999999.999999")   # the upper representation bound, exact
    assert budget.money(1105.5) == Decimal("1105.5")           # float via str(): no binary-expansion digits
    assert xd.bounded(Decimal("0.000001")) == Decimal("0.000001")
    assert (xd.MAX_INT_DIGITS, xd.MAX_FRACTION_DIGITS) == (12, 6)
    assert (xd.MAX_AGGREGATE_INT_DIGITS, xd.MAX_AGGREGATE_FRACTION_DIGITS) == (13, 6)
    assert (xd.MAX_DERIVED_INT_DIGITS, xd.MAX_DERIVED_FRACTION_DIGITS) == (15, 8)
    with pytest.raises(ValueError):
        xd.bounded(Decimal(1), tier="whatever")


MAX_RAW = "999999999999.999999"   # the largest raw amount the input tier admits


@pytest.mark.parametrize("spec", HOSTILE_CONTEXTS + [dict()], ids=str)
def test_accepted_raw_inputs_flow_through_derived_results_and_serialisation(spec):
    with _hostile(spec):
        # a legal 6 dp maximum × 1.10 yields a 7 dp limit: classified exactly, serialised to cents, never rejected by the raw bound
        r = budget.check_total_budget("1000", "2B2B", 4, "1000.000001")
        assert r.limit == Decimal("1100.0000011") and r.passed and r.reason_code == budget.WITHIN_MAX
        assert r.as_dict() == {"passed": True, "reason_code": budget.WITHIN_MAX, "total_weekly_rent": "1000.00", "whole_apartment_max": "1000.00",
                               "allowance_pct": "0.10", "limit": "1100.00", "extra_bed_charge": "0.00"}
        r = budget.check_total_budget("1100.000001", "2B2B", 4, "1000.000001")      # 0.0000001 under the 7 dp limit → pass
        assert r.passed and r.reason_code == budget.WITHIN_ALLOWANCE and r.as_dict()["limit"] == "1100.00"
        r = budget.check_total_budget("1100.000002", "2B2B", 4, "1000.000001")      # 0.0000009 over → fail (no rounding first)
        assert not r.passed and r.reason_code == budget.OVER_ALLOWANCE
        # near the raw maximum: total + A5 extra gains a 13th integer digit and must still flow
        r = budget.check_total_budget(MAX_RAW, "2B2B", 5, MAX_RAW)
        assert r.total_weekly_rent == Decimal("1000000000069.999999") and r.passed and r.reason_code == budget.WITHIN_ALLOWANCE
        assert r.as_dict()["total_weekly_rent"] == "1000000000070.00" and r.limit == Decimal("1099999999999.9999989") and r.as_dict()["limit"] == "1100000000000.00"
        # sum of 7 maxima (A4 / MAX_PEOPLE) and the 3B limit on it: 13 integer digits, 8 dp
        baseline = budget.matebot_baseline([MAX_RAW] * 7)
        assert baseline == Decimal("6999999999999.999993")
        limit = budget.budget_limit(baseline, "3B")
        assert limit == Decimal("8399999999999.9999916") and budget.cents(limit) == Decimal("8400000000000.00")
        r = budget.check_total_budget(MAX_RAW, "3B", 7, baseline)
        assert r.passed and r.reason_code == budget.WITHIN_MAX and r.as_dict()["whole_apartment_max"] == "7000000000000.00"
        # exception-rent path: derived normal total (13 digits) flows into the evidence comparison and facts
        exc = ld.StaffExitException(CTX, date(2027, 3, 15), MAX_RAW, "anna", TS)
        r = xr.check_exception_rent(CTX, "2B2B", 5, MAX_RAW, date(2027, 3, 15), "999999999000", MAX_RAW, staff_exception=exc)
        assert r.passed and r.reason_code == xr.EXCEPTION_RENT_WITHIN_ALLOWANCE and r.facts["normal_total_weekly_rent"] == "999999999070.00"
        # ... and a 13-integer-digit derived normal total (MAX_RAW rent + A5 charge) is compared, not rejected: exception total below it
        r = xr.check_exception_rent(CTX, "2B2B", 5, MAX_RAW, date(2027, 3, 15), MAX_RAW, MAX_RAW, staff_exception=exc)
        assert not r.passed and r.reason_code == ld.EXIT_EXCEPTION_INVALID and r.facts["normal_total_weekly_rent"] == "1000000000070.00"
        # raw tier still rejects a 13-digit RAW rent / evidence total; a 7 dp RAW amount is rejected rather than rounded
        with pytest.raises(ValueError):
            budget.check_total_budget("1000000000000", "2B2B", 4, 1000)
        with pytest.raises(ValueError):
            ld.StaffExitException(CTX, date(2027, 3, 15), "1000000000000", "anna", TS)
        with pytest.raises(ValueError):
            budget.check_total_budget("1000.0000001", "2B2B", 4, 1000)
        # AGGREGATE input bound for a whole-apartment maximum: 13 integer / 6 decimal digits — at the bound accepted and every
        # accepted maximum serialises after the allowance; just over → rejected as input (never rounded into a verdict)
        MAX_AGG = "9999999999999.999999"
        for code, pct, exact_limit in (("2B2B", "1.10", "10999999999999.9999989"), ("3B", "1.20", "11999999999999.9999988")):
            r = budget.check_total_budget(MAX_RAW, code, 2, MAX_AGG)
            assert r.limit == Decimal(exact_limit) and r.passed and r.reason_code == budget.WITHIN_MAX   # literal: test-side maths would use the hostile context
            d = r.as_dict()
            assert d["whole_apartment_max"] == "10000000000000.00" and d["limit"] == {"1.10": "11000000000000.00", "1.20": "12000000000000.00"}[pct]
            assert xr.check_exception_rent(CTX, code, 2, MAX_AGG, date(2027, 3, 15), MAX_RAW, MAX_RAW, staff_exception=exc).facts["whole_apartment_max"] == "10000000000000.00"
        for over in ("10000000000000", "1000.0000001", "1000.00000001", Decimal("1E+13")):
            with pytest.raises(ValueError):
                budget.check_total_budget("1000", "2B2B", 4, over)
            with pytest.raises(ValueError):
                budget.budget_limit(over, "3B")
            with pytest.raises(ValueError):
                xr.check_exception_rent(CTX, "2B2B", 5, over, date(2027, 3, 15), "1000", MAX_RAW, staff_exception=exc)
        # a 13-digit maximum is NOT accepted as a raw rent or member maximum
        with pytest.raises(ValueError):
            budget.matebot_baseline([MAX_AGG])
        with pytest.raises(ValueError):
            budget.check_total_budget(MAX_AGG, "2B2B", 4, MAX_AGG)
        # derived tier is still bounded (absurd computed magnitudes are rejected, not formatted)
        with pytest.raises(ValueError):
            budget.cents(Decimal("1E+15"))
        with pytest.raises(ValueError):
            ev.fmt_money(Decimal("1E+1000000"))
        assert budget.cents(Decimal("0.00000001")) == Decimal("0.00") and ev.fmt_money(Decimal("1E+14")) == "100000000000000.00"


def test_exact_context_is_isolated_from_caller_and_does_not_mutate_caller_flags():
    with localcontext() as c:
        c.prec = 3
        c.rounding = decimal.ROUND_UP
        c.traps[decimal.InvalidOperation] = False
        c.Emax, c.Emin = 20, -20
        c.clear_flags()
        with xd.exact() as inner:
            assert inner.prec == 40 and inner.traps[decimal.Inexact] and inner.traps[decimal.InvalidOperation]
            assert inner.rounding == decimal.ROUND_HALF_EVEN and (inner.Emax, inner.Emin) == (999999, -999999)
            assert not any(inner.flags.values())
            with pytest.raises(decimal.Inexact):
                Decimal(1) / Decimal(3)   # exactness is enforced, not silently rounded
        with xd.display() as d:
            assert d.rounding == decimal.ROUND_HALF_UP and not d.traps[decimal.Inexact]
            assert Decimal("2.345").quantize(Decimal("0.01")) == Decimal("2.35")
        budget.check_total_budget("1100.01", "2B2B", 4, 1000)
        ev.fmt_money(Decimal("1105.505"))
        # caller's context restored and untouched: same settings, no flags raised by our work, shared constants not mutated
        assert c.prec == 3 and c.rounding == decimal.ROUND_UP and not c.traps[decimal.InvalidOperation] and (c.Emax, c.Emin) == (20, -20)
        assert not any(c.flags.values())
        assert decimal.getcontext() is c
    assert not any(xd.EXACT_CTX.flags.values()) and not any(xd.DISPLAY_CTX.flags.values())
    assert xd.EXACT_CTX.prec == 40 and xd.EXACT_CTX.traps[decimal.Inexact]
