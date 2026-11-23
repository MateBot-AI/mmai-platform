"""Automated tests for every hard rule in the Rule Register (brief section 5)."""
from datetime import date
from decimal import Decimal

import pytest

from server import rules
from server.countries import is_western, normalise_country, region_of  # countries.py is reference data only; not an engine input
from tests.conftest import apartment, check, person, unit


# ---------------- A4(d) bedding, A4 capacity, A4(b) limits ----------------
def test_parse_bedding_options():
    assert rules.parse_bedding("1 king or 2 singles") == [["king"], ["single", "single"]]
    assert rules.parse_bedding("1 king or 2 singles or 1 king+1 single") == [["king"], ["single", "single"], ["king", "single"]]
    assert rules.parse_bedding("1 single or 1 bunk bed") == [["single"], ["bunk"]]
    assert rules.parse_bedding("1 king or 2 singles or 3 singles") == [["king"], ["single", "single"], ["single", "single", "single"]]


def test_parse_bedding_rejects_unknown():
    with pytest.raises(ValueError):
        rules.parse_bedding("1 sofa")


def test_capacity_counts_bunk_as_two():
    assert rules.apartment_capacity([rules.parse_bedding("1 king or 2 singles"), rules.parse_bedding("1 single or 1 bunk bed")]) == (2, 4)


def test_a4b_limits():
    assert rules.APT_TYPES["Studio"]["a4b_max"] == 3 and rules.APT_TYPES["1BR"]["a4b_max"] == 3
    assert rules.APT_TYPES["2B1B"]["a4b_max"] == 5 and rules.APT_TYPES["2B2B"]["a4b_max"] == 5
    assert rules.APT_TYPES["3B"]["a4b_max"] == 7
    assert not rules.capacity_fits_a4b("2B1B", 6)


def test_group_over_a4b_limit_fails():
    apt = apartment("2B1B", bedrooms=["1 king or 2 singles or 3 singles", "1 king or 2 singles or 3 singles"])
    units = [unit(f"u{i}", budget=300, arrangements=("Share with two others",)) for i in range(6)]
    r = check(apt, units)
    assert not r.ok and any("A4(b)" in x for x in r.reasons)


def test_group_over_bedding_capacity_fails():
    apt = apartment("2B2B")  # max 4
    units = [unit(f"u{i}", budget=300) for i in range(5)]
    r = check(apt, units)
    assert not r.ok and any("bedding capacity" in x for x in r.reasons)


# ---------------- A5 extra bed, A6 rates, A8 monthly ----------------
def test_extra_bed_charge():
    assert rules.extra_bed_charge("Studio", 2) == 0 and rules.extra_bed_charge("Studio", 3) == 70
    assert rules.extra_bed_charge("2B1B", 4) == 0 and rules.extra_bed_charge("2B2B", 5) == 70
    assert rules.extra_bed_charge("3B", 6) == 0 and rules.extra_bed_charge("3B", 7) == 70


def test_rate_kind_6m_for_jan_feb_start_ending_in_any_july():
    """A6(b) literal (root ruling 8 Nov 2026: the same-year condition was a code/register parity gap, not a rule)."""
    assert rules.rate_kind(date(2027, 1, 15), date(2027, 7, 10)) == "6m"
    assert rules.rate_kind(date(2027, 2, 28), date(2027, 7, 31)) == "6m"
    assert rules.rate_kind(date(2027, 1, 15), date(2028, 7, 10)) == "6m"     # regression: January 2027 -> July 2028
    assert rules.rate_kind(date(2028, 2, 29), date(2029, 7, 1)) == "6m"      # leap-day start, later July
    assert rules.rate_kind(date(2027, 2, 1), date(2030, 7, 20)) == "6m"      # any later July
    assert rules.rate_kind(date(2027, 7, 1), date(2028, 1, 15)) == "12m"
    assert rules.rate_kind(date(2027, 3, 1), date(2027, 7, 10)) == "12m"
    assert rules.rate_kind(date(2026, 12, 31), date(2027, 7, 10)) == "12m"   # December start is not January
    assert rules.rate_kind(date(2027, 1, 15), date(2028, 6, 30)) == "12m"    # June end is not July
    assert rules.rate_kind(date(2027, 1, 15), date(2028, 8, 1)) == "12m"


def test_monthly_multiplier():
    assert rules.monthly(100) == pytest.approx(433.4)


def test_group_uses_6m_rent_for_jan_to_july():
    apt = apartment("2B2B", rent12=1150, rent6=1225, avail=date(2027, 1, 25))   # 7 empty days: within A17's 8 for a 169-day stay
    units = [unit("a", move_in=date(2027, 2, 1), move_out=date(2027, 7, 10), budget=700), unit("b", move_in=date(2027, 2, 1), move_out=date(2027, 7, 10), budget=700)]
    r = check(apt, units)
    assert r.ok, r.reasons
    assert r.rate_kind == "6m" and r.weekly_rent == 1225 and r.total_weekly == 1225


# ---------------- A9 minimum lease, A10 exit windows ----------------
def test_min_lease_default_two_months_a9a_a15d():
    """A9(a): generally 2 months; A15(d): 2 months on every apartment today."""
    assert rules.DEFAULT_MIN_LEASE_MONTHS == 2
    ok, why = rules.exit_ok(date(2027, 10, 1), date(2027, 11, 15))      # 6 weeks: under 2 months (both dates are in-window days)
    assert not ok and "minimum" in why
    assert rules.exit_ok(date(2027, 10, 1), date(2027, 12, 5))[0]        # just over 2 months, move-out inside 5 Oct-7 Dec
    assert not rules.exit_ok(date(2027, 10, 1), date(2027, 12, 5), min_lease_months=3)[0]  # staff may set a longer minimum (A9(b))


def test_a10b_same_exit_windows_every_year_for_every_start():
    """A10(b): 10 Jan-20 Feb, 1-20 Jul, 5 Oct-7 Dec inclusive, whatever the start; the former start-season windows are gone."""
    for name in ("exit_windows", "effective_availability", "is_quiet_period"):
        assert not hasattr(rules, name), name
    for s in (date(2027, 2, 1), date(2027, 4, 1), date(2027, 7, 5), date(2027, 11, 1), date(2027, 12, 20)):
        for end, label in ((date(2029, 1, 10), "10 January to 20 February"), (date(2029, 2, 20), "10 January to 20 February"),
                           (date(2028, 7, 1), "1 to 20 July"), (date(2028, 7, 20), "1 to 20 July"),
                           (date(2028, 10, 5), "5 October to 7 December"), (date(2028, 12, 7), "5 October to 7 December")):
            ok, why = rules.exit_ok(s, end)
            assert ok and why == label, (s, end, why)
    s = date(2027, 2, 1)
    assert rules.exit_ok(s, date(2027, 7, 15))[0] and rules.exit_ok(s, date(2027, 11, 20))[0] and rules.exit_ok(s, date(2028, 2, 15))[0]


def test_a10c_blackout_is_a_reason_never_silently_granted():
    s = date(2026, 11, 1)   # every end below is past the 2-month minimum, so the exit rule is what decides
    for end in (date(2027, 2, 21), date(2027, 6, 30), date(2027, 7, 21), date(2027, 7, 31), date(2027, 9, 1), date(2027, 10, 4),
                date(2027, 12, 8), date(2027, 12, 20), date(2028, 1, 9), date(2028, 3, 1), date(2028, 4, 1)):
        ok, why = rules.exit_ok(s, end)
        assert not ok and "blackout" in why and "A10(e)" in why and end.isoformat() in why, (end, why)
    # a start in a former "quiet period" gets no "any term" licence any more
    assert not rules.exit_ok(date(2027, 3, 3), date(2028, 3, 3))[0] and "blackout" in rules.exit_ok(date(2027, 3, 3), date(2028, 3, 3))[1]
    assert rules.exit_ok(date(2027, 3, 3), date(2028, 2, 20))[0]


def test_a10_choose_lease_dates_prefers_latest_in_window_and_reports_blackout():
    apt = apartment()
    # move-out 1 Feb 2028 +/- 14 days: latest acceptable in-window day is 15 Feb 2028
    r = check(apt, [unit("a"), unit("b")])
    assert r.ok and r.lease_end == date(2028, 2, 15) and r.exit_window == "10 January to 20 February"
    # a move-out window wholly in a blackout: no exit day, the reason names the blackout and the exception route
    units = [unit("a", move_out=date(2028, 4, 15), out_flex=14), unit("b", move_out=date(2028, 4, 15), out_flex=14)]
    r = check(apt, units)
    assert not r.ok and any("blackout" in x and "A10(e)" in x for x in r.reasons), r.reasons
    # a window straddling 20 Feb picks 20 Feb
    units = [unit("a", move_out=date(2028, 2, 25), out_flex=7), unit("b", move_out=date(2028, 2, 25), out_flex=7)]
    r = check(apt, units)
    assert r.ok and r.lease_end == date(2028, 2, 20)


def test_choose_lease_prefers_longest_acceptable_exit():
    apt = apartment("2B2B", avail=date(2027, 1, 25))
    units = [unit("a", move_in=date(2027, 2, 1), in_flex=7, move_out=date(2028, 2, 1), out_flex=14),
             unit("b", move_in=date(2027, 2, 5), in_flex=7, move_out=date(2028, 1, 25), out_flex=14)]
    start, end, label, err = rules.choose_lease_dates(apt, units)
    assert err == "" and start == date(2027, 2, 5) and end == date(2028, 2, 8)   # I11(d): b's declaration is for 5 Feb, so 29 Jan is not proposed
    units[0].adults_confirmed_move_in = units[1].adults_confirmed_move_in = date(2027, 1, 25)   # staff-recorded declarations for an earlier date: the earliest window date is used again
    start, end, label, err = rules.choose_lease_dates(apt, units)
    assert err == "" and start == date(2027, 1, 29) and end == date(2028, 2, 8)


def test_apartment_without_date_never_matches():
    apt = apartment("2B2B", avail=None)
    r = check(apt, [unit("a"), unit("b")])
    assert not r.ok and any("no availability date" in x for x in r.reasons)


# ---------------- I11(d) adult declaration (no age arithmetic exists: I8, 8 Nov 2026) ----------------
def test_no_age_arithmetic_or_sex_fields_remain():
    assert not hasattr(rules, "age_on") and not hasattr(rules, "is_adult_on")
    assert not {"sex", "dob"} & set(rules.Person.__dataclass_fields__)   # sex_pref / own_sexes are matching inputs again (17 / 23 Nov 2026)
    assert "adults_confirmed_move_in" in rules.Unit.__dataclass_fields__
    import inspect
    assert "is_adult_check" not in inspect.signature(rules.check_group).parameters   # the adult check cannot be bypassed


def test_missing_adult_declaration_holds_group_i11d():
    apt = apartment("2B2B")
    r = check(apt, [unit("a", adults_for=None), unit("b")])
    assert not r.ok and any("I11(d)" in x and "a's party" in x and "staff to confirm" in x for x in r.reasons)
    assert check(apt, [unit("a"), unit("b")]).ok


def test_declared_date_bounds_the_proposed_start_option_c():
    """Preferred 1 Feb ± 7 days, declaration for 1 Feb, apartment free from 20 Jan: propose 1 Feb (not 25 Jan, not a hold)."""
    apt = apartment("2B2B", avail=date(2027, 1, 20))
    r = check(apt, [unit("a", move_in=date(2027, 2, 1), in_flex=7), unit("b", move_in=date(2027, 2, 1), in_flex=7)])
    assert r.ok and r.lease_start == date(2027, 2, 1), r.reasons
    # a declaration for an EARLIER date than preferred (staff-recorded) lets the earliest window date be used
    r2 = check(apt, [unit("a", move_in=date(2027, 2, 1), in_flex=7, adults_for=date(2027, 1, 20)), unit("b", move_in=date(2027, 2, 1), in_flex=7, adults_for=date(2027, 1, 1))])
    assert r2.ok and r2.lease_start == date(2027, 1, 25), r2.reasons


def test_overlapping_parties_take_the_latest_declared_date():
    apt = apartment("2B2B", avail=date(2027, 1, 20))   # 16 empty days to 5 Feb: within A17's 18 for the stay
    a = unit("a", move_in=date(2027, 1, 25), in_flex=14)                     # window 11 Jan to 8 Feb, declared for 25 Jan
    b = unit("b", move_in=date(2027, 2, 5), in_flex=7)                       # window 29 Jan to 12 Feb, declared for 5 Feb
    r = check(apt, [a, b])
    assert r.ok and r.lease_start == date(2027, 2, 5), r.reasons


def test_single_day_verified_intersection_boundary():
    apt = apartment("2B2B", avail=date(2027, 1, 25))   # 14 empty days to 8 Feb: within A17's 18
    a = unit("a", move_in=date(2027, 2, 1), in_flex=7)                       # window 25 Jan to 8 Feb
    b = unit("b", move_in=date(2027, 2, 15), in_flex=7, adults_for=date(2027, 2, 8))   # window 8 Feb to 22 Feb; declared for 8 Feb
    r = check(apt, [a, b])
    assert r.ok and r.lease_start == date(2027, 2, 8), r.reasons
    # one day later and no date inside everyone's windows is covered: explained, not inferred, windows not widened
    b2 = unit("b", move_in=date(2027, 2, 15), in_flex=7, adults_for=date(2027, 2, 9))
    r2 = check(apt, [a, b2])
    assert not r2.ok and any("I11(d)" in x and "renewed declaration" in x and "2027-02-09" in x for x in r2.reasons)


def test_no_verified_overlap_when_declared_after_every_window():
    apt = apartment("2B2B", avail=date(2027, 1, 1))
    a = unit("a", move_in=date(2027, 2, 1), in_flex=7, adults_for=date(2027, 3, 1))   # declared only for 1 Mar: outside the window
    r = check(apt, [a, unit("b", move_in=date(2027, 2, 1), in_flex=7)])
    assert not r.ok and r.lease_start is None and any("renewed declaration" in x for x in r.reasons)
    assert not any("staff to confirm" in x for x in r.reasons)   # a declaration exists; it just does not cover these dates


def test_direct_earlier_start_still_fails_the_adult_check():
    """A caller that forces a start before a party's declared date (exact-date windows) is held, never passed."""
    apt = apartment("2B2B", avail=date(2027, 1, 20))
    r = check(apt, [unit("a", move_in=date(2027, 1, 25), in_flex=0, adults_for=date(2027, 2, 1)), unit("b", move_in=date(2027, 1, 25), in_flex=0)])
    assert not r.ok and any("I11(d)" in x for x in r.reasons)


def test_age_and_sex_are_not_matching_factors():
    """Invariance: the same neutral facts give the same result whatever a legacy row held about sex or age (nothing is read)."""
    apt = apartment("2B2B")
    base = check(apt, [unit("a"), unit("b")])
    assert base.ok
    assert rules.pair_conflict(person("a"), person("b")) is None
    assert not any("5.1" in x or "5.2" in x or "age" in x.lower() for x in base.reasons)


# ---------------- I14 budget, A12 split ----------------
def test_budget_is_top_of_range():
    assert rules.budget_max_for("2B2B-single", "12m") == 575
    assert rules.budget_max_for("2B2B-single", "6m") == 613
    assert rules.budget_max_for("2B1B-triple", "12m") == 198  # I14(b), 26 Oct 2026
    assert rules.budget_max_for("2B1B-triple", "6m") == 211   # I14(c)
    assert rules.budget_max_for("nope", "12m") is None


def test_guide_kind_for_dates():
    assert rules.guide_kind_for(date(2027, 1, 20), date(2027, 7, 15)) == "6m"
    assert rules.guide_kind_for(date(2027, 7, 1), date(2028, 2, 1)) == "12m"
    assert rules.guide_kind_for(None, None) == "12m"


def test_split_per_room_then_per_person():
    apt = apartment("2B2B", rent12=1150)
    # room 1: a alone; room 2: b + c share
    units = [unit("a", budget=575, arrangements=("Own room",)),
             unit("b", budget=300, arrangements=("Share with one other",)), unit("c", budget=300, arrangements=("Share with one other",))]
    r = check(apt, units)
    assert r.ok, r.reasons
    assert r.shares["a"] == 575 and r.shares["b"] == 287.5 and r.shares["c"] == 287.5


def test_per_person_budget_ceiling_withdrawn_a16():
    """A16 (8 Nov 2026): no per-person ceiling anywhere; the former I14(d) per-share +10% helpers are gone from the engine."""
    for name in ("within_budget", "budget_ceiling", "BUDGET_TOLERANCE"):
        assert not hasattr(rules, name), name
    assert rules.RULE_VERSION == "2026-11-23.sex-culture-age-prefs"   # 23 Nov 2026: Same-sex-only hard filter, culture / age / language soft preferences; every earlier assessment re-reviews


def test_a16_household_sum_boundary_two_bedroom_10pct():
    """A16: whole rent vs the SUM of every member's maximum, +10% for a 2-bedroom. 1150 = (500 + 545.45..) x 1.1 boundary."""
    apt = apartment("2B2B", rent12=1150)   # $575 per room; a's own share is 575, far above a's 500 maximum - irrelevant under A16
    exact = check(apt, [unit("a", budget=500, arrangements=("Own room",)), unit("b", budget=545.46, arrangements=("Own room",))])  # 1045.46 x 1.1 = 1150.006
    assert exact.ok, exact.reasons
    hb = exact.household_budget
    assert hb["reason_code"] == "WITHIN_ALLOWANCE" and hb["whole_apartment_max"] == "1045.46" and hb["allowance_pct"] == "0.10"
    assert hb["total_weekly_rent"] == "1150.00" and hb["extra_bed_charge"] == "0.00" and hb["member_weekly_maximums"] == {"a": "500.00", "b": "545.46"}
    assert exact.shares == {"a": 575.0, "b": 575.0}   # proposed per-room shares are still reported for the group's agreement
    over = check(apt, [unit("a", budget=500, arrangements=("Own room",)), unit("b", budget=545.45, arrangements=("Own room",))])  # 1045.45 x 1.1 = 1149.995 < 1150
    assert not over.ok and len(over.reasons) == 1 and "A16" in over.reasons[0] and "I14" not in over.reasons[0]
    assert "$1150.00/wk" in over.reasons[0] and "$1045.45/wk" in over.reasons[0] and "exact limit $1149.995," in over.reasons[0]
    assert over.household_budget["limit"] == "1150.00" and over.household_budget["limit_exact"] == "1149.995"   # display rounding never decides
    assert over.household_budget["reason_code"] == "OVER_ALLOWANCE" and over.shares == {"a": 575.0, "b": 575.0}
    within = check(apt, [unit("a", budget=575, arrangements=("Own room",)), unit("b", budget=575, arrangements=("Own room",))])
    assert within.ok and within.household_budget["reason_code"] == "WITHIN_MAX"


def test_a16_detail_text_is_context_independent():
    """Adversarial decimal context: the caller's precision/rounding/traps must not change the decision or the exact-limit text."""
    import decimal
    apt = apartment("2B2B", rent12=1150)
    units = [unit("a", budget=500, arrangements=("Own room",)), unit("b", budget=545.45, arrangements=("Own room",))]
    ctxs = [decimal.Context(prec=2), decimal.Context(prec=1, rounding=decimal.ROUND_DOWN), decimal.Context(prec=3, rounding=decimal.ROUND_UP, traps=[decimal.Inexact, decimal.Rounded]),
            decimal.Context(prec=60)]
    for ctx in ctxs:
        with decimal.localcontext(ctx):
            reasons, detail = rules.household_budget("2B2B", 1150, [p for u in units for p in u.people])
            assert detail["limit_exact"] == "1149.995" and detail["limit"] == "1150.00" and detail["reason_code"] == "OVER_ALLOWANCE", ctx
            assert "exact limit $1149.995," in reasons[0]
            r = check(apt, units)
            assert not r.ok and r.household_budget["limit_exact"] == "1149.995"
            ok_reasons, ok_detail = rules.household_budget("2B2B", 1150, [p for u in [unit("a", budget=500), unit("b", budget=545.46)] for p in u.people])
            assert ok_reasons == [] and ok_detail["limit_exact"] == "1150.006"
    assert rules._lossless_decimal_text(Decimal("1150.00")) == "1150" and rules._lossless_decimal_text(Decimal("0.000")) == "0"
    assert rules._lossless_decimal_text(Decimal("1E+3")) == "1000" and rules._lossless_decimal_text(Decimal("1149.9950")) == "1149.995"


def test_a16_one_share_far_above_own_max_but_household_fits():
    """A16: a member whose proposed share is far above their own maximum is NOT a rule failure when the household total fits."""
    apt = apartment("2B2B", rent12=1150)
    r = check(apt, [unit("a", budget=200, arrangements=("Own room",)), unit("b", budget=950, arrangements=("Own room",))])   # sum 1150
    assert r.ok, r.reasons
    assert r.shares["a"] == 575.0 and r.household_budget["reason_code"] == "WITHIN_MAX"


def test_a16_three_bedroom_20pct_with_extra_bed():
    """A16: 3-bedroom allowance is 20% and the actual total INCLUDES the $70 7th-bed charge (A5)."""
    beds = ["1 king or 2 singles or 3 singles", "1 king or 2 singles", "1 king or 2 singles"]
    apt = apartment("3B", rent12=1500, bedrooms=beds)
    units = [unit(f"u{i}", budget=b, arrangements=("Share with two others", "Share with one other"), sizes=("3 bedrooms",))
             for i, b in enumerate([180, 180, 180, 200, 200, 185, 183.34])]   # sum = 1308.34; x 1.2 = 1570.008 >= 1570
    r = check(apt, units)
    assert r.ok, r.reasons
    hb = r.household_budget
    assert hb["extra_bed_charge"] == "70.00" and hb["total_weekly_rent"] == "1570.00" and hb["allowance_pct"] == "0.20" and hb["reason_code"] == "WITHIN_ALLOWANCE"
    units[-1] = unit("u6", budget=183.33, arrangements=("Share with two others", "Share with one other"), sizes=("3 bedrooms",))   # sum 1308.33 x 1.2 = 1569.996
    r2 = check(apt, units)
    assert not r2.ok and any("more than 20% above" in x and "incl. $70.00 extra bed" in x for x in r2.reasons), r2.reasons
    # six people: no extra bed (standard 3B capacity), but the 20% allowance still applies to the type (sum 1125 x 1.2 = 1350 < 1500: over)
    six = check(apt, units[:6])
    assert not six.ok and six.household_budget["extra_bed_charge"] == "0.00" and six.household_budget["allowance_pct"] == "0.20"
    assert six.household_budget["reason_code"] == "OVER_ALLOWANCE" and six.household_budget["limit"] == "1350.00"


def test_a16_missing_maximum_holds_not_passes():
    apt = apartment("2B2B", rent12=1150)
    r = check(apt, [unit("a", budget=None, arrangements=("Own room",)), unit("b", budget=2000, arrangements=("Own room",))])
    assert not r.ok and any("no stated weekly maximum" in x and "A16" in x for x in r.reasons) and r.household_budget is None
    bad = check(apt, [unit("a", budget=-5, arrangements=("Own room",)), unit("b", budget=2000, arrangements=("Own room",))])
    assert not bad.ok and any("not a usable amount" in x for x in bad.reasons) and bad.household_budget is None


def test_a16_no_per_unit_budget_prefilter_low_budget_lead_retained():
    """A16: the candidate pre-filter must not exclude an enquiry because its own maximum is below any conceivable share - a
    low-budget enquirer may be carried by a higher-budget partner, and the household test runs on complete groups only."""
    from server import matching
    apt = apartment("2B2B", rent12=1150)
    low = unit("low", budget=100, arrangements=("Own room",))
    high = unit("high", budget=1100, arrangements=("Own room",))
    assert matching._unit_fits_apartment(apt, low) and matching._unit_fits_apartment(apt, high)
    assert check(apt, [low, high]).ok   # sum 1200 >= 1150: the complete group passes although low's share (575) is far above 100
    # two low-budget enquirers together are excluded by the household test on their complete group, not before
    low2 = unit("low2", budget=100, arrangements=("Own room",))
    assert matching._unit_fits_apartment(apt, low2)
    r = check(apt, [low, low2])
    assert not r.ok and any("A16" in x for x in r.reasons) and r.household_budget["whole_apartment_max"] == "200.00"
    # and matching.py no longer carries any per-unit budget pre-filter
    import inspect
    assert "within_budget" not in inspect.getsource(matching) and "cheapest" not in inspect.getsource(matching._unit_fits_apartment)


def test_extra_bed_added_to_total_and_split():
    apt = apartment("2B1B", rent12=800, bedrooms=["1 king or 2 singles or 3 singles", "1 king or 2 singles or 3 singles"])
    units = [unit(f"u{i}", budget=225, arrangements=("Share with two others", "Share with one other")) for i in range(5)]
    r = check(apt, units)
    assert r.ok, r.reasons
    assert r.extra_bed == 70 and r.total_weekly == 870
    # A12(e): $400 per room; the $70 sits on the room with the extra (third) bed: 470/3 and 400/2
    assert sorted(r.shares.values()) == [156.67, 156.67, 156.67, 200.0, 200.0]
    triple = next(x for x in r.allocation if len(x["occupants"]) == 3)
    twin = next(x for x in r.allocation if len(x["occupants"]) == 2)
    assert triple["room_cost"] == 470 and triple["extra_bed"] == 70 and twin["room_cost"] == 400 and twin["extra_bed"] == 0
    assert sum(x["room_cost"] for x in r.allocation) == r.total_weekly


def test_extra_bed_in_studio_a12e():
    apt = apartment("Studio", rent12=600, bedrooms=["1 king or 2 singles or 3 singles"])
    units = [unit(f"u{i}", sizes=("Studio",), budget=250, arrangements=("Share with two others",)) for i in range(3)]
    r = check(apt, units)
    assert r.ok, r.reasons
    assert r.total_weekly == 670 and r.allocation[0]["room_cost"] == 670 and set(r.shares.values()) == {223.33}


def test_liability_note_b5():
    note = rules.liability_note(1000)
    assert "$1,000 per week" in note and "jointly and severally liable" in note and "guide only" in note


# ---------------- M3 bunks, 3.5 arrangements, couples ----------------
def test_bunk_only_for_people_who_said_yes():
    # Bedroom 1 is a queen (one person or a couple), so two sharers can only go in Bedroom 2's bunk
    apt = apartment("2B1B", rent12=800, bedrooms=["1 queen", "1 single or 1 bunk bed"])
    units = [unit("a", bunk_ok=False, budget=500, arrangements=("Own room",)),
             unit("b", bunk_ok=False, budget=300, arrangements=("Share with one other",)), unit("c", bunk_ok=False, budget=300, arrangements=("Share with one other",))]
    assert not check(apt, units).ok
    units[1].people[0].bunk_ok = True
    units[2].people[0].bunk_ok = True
    r = check(apt, units)
    assert r.ok, r.reasons
    beds = {o["person_id"]: o["bed"] for room in r.allocation for o in room["occupants"]}
    assert beds["b"] == "bunk" and beds["c"] == "bunk"
    # with "1 king or 2 singles" in Bedroom 1 the sharers take the two singles instead and nobody needs a bunk
    apt2 = apartment("2B1B", rent12=800, bedrooms=["1 king or 2 singles", "1 single or 1 bunk bed"])
    units[1].people[0].bunk_ok = False
    units[2].people[0].bunk_ok = False
    r2 = check(apt2, units)
    assert r2.ok and "bunk" not in {o["bed"] for room in r2.allocation for o in room["occupants"]}


def test_bed_allocation_is_independent_of_member_order():
    """Regression (26 Oct 2026): the allocator used to validate only the first bed assignment, so a valid group could fail
    depending on the order of its members."""
    conor = unit("7", arrangements=("Own room", "Share with one other"), bunk_ok=False, sizes=("3 bedrooms",))
    pablo = unit("49", arrangements=("Own room",), sizes=("3 bedrooms",))
    couple = [person("63", unit="63", couple=True, arrangements=("Couple sharing one bed", "Share with one other")),
              person("63m1", unit="63", couple=True, is_lead=False, arrangements=("Couple sharing one bed", "Share with one other"))]
    cu = unit("63", people=couple, kind="couple", sizes=("3 bedrooms",))
    mat = unit("69", arrangements=("Share with one other", "Share with two others"), sizes=("3 bedrooms",))
    apt = apartment("3B", rent12=1450)
    import itertools
    results = {tuple(u.id for u in perm): check(apt, list(perm)).ok for perm in itertools.permutations([conor, pablo, cu, mat])}
    assert all(results.values()), [k for k, v in results.items() if not v]


def test_own_room_only_person_is_not_put_in_shared_room():
    apt = apartment("2B2B")
    units = [unit("a", budget=575, arrangements=("Own room",)), unit("b", budget=575, arrangements=("Own room",)), unit("c", budget=575, arrangements=("Own room",))]
    assert not check(apt, units).ok  # 3 people, 2 rooms, nobody shares


def test_couple_needs_a_double_bed_in_one_room():
    apt = apartment("2B2B", bedrooms=["2 singles", "1 king or 2 singles"])
    couple = [person("c1", unit="c", couple=True, arrangements=("Couple sharing one bed",), budget=300),
              person("c1m1", unit="c", couple=True, is_lead=False, arrangements=("Couple sharing one bed",), budget=300)]
    units = [unit("c", people=couple, kind="couple"), unit("d", budget=600, arrangements=("Own room",))]
    r = check(apt, units)
    assert r.ok, r.reasons
    for room in r.allocation:
        ids = {o["person_id"] for o in room["occupants"]}
        if "c1" in ids:
            assert ids == {"c1", "c1m1"} and room["setup"] == ["king"]


def test_couple_fails_when_no_double_bed():
    apt = apartment("2B2B", bedrooms=["2 singles", "2 singles"])
    couple = [person("c1", unit="c", couple=True, arrangements=("Couple sharing one bed",), budget=300),
              person("c1m1", unit="c", couple=True, is_lead=False, arrangements=("Couple sharing one bed",), budget=300)]
    assert not check(apt, [unit("c", people=couple, kind="couple"), unit("d", budget=600)]).ok


# ---------------- 3.2 sizes, 3.4 max housemates, P3, C1 ----------------
def test_apartment_size_not_ticked_fails():
    apt = apartment("3B", rent12=1300)
    r = check(apt, [unit("a", sizes=("2 bedrooms",), budget=500), unit("b", budget=500), unit("c", budget=500)])
    assert not r.ok and any("3.2" in x for x in r.reasons)


def test_max_housemates_counts_everyone_else():
    apt = apartment("3B", rent12=1300)
    r = check(apt, [unit("a", mate_max=1, budget=500), unit("b", budget=500), unit("c", budget=500)])
    assert not r.ok and any("3.4" in x for x in r.reasons)


def test_group_size_bounds_p3():
    apt = apartment("2B2B")
    r = check(apt, [unit("a", budget=1200, arrangements=("Own room",))])
    assert not r.ok and any("P3" in x for x in r.reasons)


def test_city_mismatch_c1():
    apt = apartment("2B2B")
    u = unit("a"); u.city = "Sydney"
    r = check(apt, [u, unit("b")])
    assert not r.ok and any("C1" in x for x in r.reasons)


def test_every_bedroom_occupied_gp5():
    """GP5: a group that would leave a bedroom empty is never proposed, even when capacity allows it."""
    apt = apartment("3B", rent12=1500)
    # two people who both want their own room cannot fill three bedrooms
    r = check(apt, [unit("a", budget=600, arrangements=("Own room",)), unit("b", budget=600, arrangements=("Own room",))])
    assert not r.ok and any("bed allocation" in x for x in r.reasons)
    # two people willing to share could sit in one room but allocation must still use every bedroom -> impossible with 2 people in a 3B
    r2 = check(apt, [unit("a", budget=600), unit("b", budget=600)])
    assert not r2.ok
    r3 = check(apt, [unit("a", budget=600, arrangements=("Own room",)), unit("b", budget=600, arrangements=("Own room",)), unit("c", budget=600, arrangements=("Own room",))])
    assert r3.ok, r3.reasons and len(r3.allocation) == 3 and all(x["occupants"] for x in r3.allocation)


def test_smoking_pair():
    smoker = person("s", smokes=True)
    strict = person("n", smoke_ok=False)
    assert rules.pair_conflict(strict, smoker)
    assert rules.pair_conflict(smoker, strict) is None
    assert rules.pair_conflict(person("ok", smoke_ok=True), smoker) is None


def test_origin_is_not_a_pairwise_factor_launch_scope():
    """Launch scope (Bill, 8 November 2026): country or region of birth never produces a pairwise conflict, in either
    direction, whatever the countries are. There is no M8/M10/M11/M12 path and no hook for one."""
    for a_c, b_c in (("Brazil", "Brazil"), ("Brazil", "Chile"), ("Japan", "France"), ("United Kingdom", "India"), ("Poland", "Nepal"), (None, "China")):
        a, b = person("a", country=a_c), person("b", country=b_c)
        assert rules.pair_conflict(a, b) is None and rules.pair_conflict(b, a) is None, (a_c, b_c)
    assert not hasattr(rules, "SOUTH_ASIA_M11")
    import inspect
    sig = inspect.signature(rules.check_group)
    # 11 November 2026: A10 exact staff-recorded offers are a closed booking input,
    # not an origin/sex/age hook or a general bypass; today is the A17 business date.
    assert list(sig.parameters) == ["apt", "units", "today", "exception_offers"]
    assert sig.parameters["today"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["exception_offers"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["exception_offers"].default == ()
    assert all(p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
               for p in sig.parameters.values())
    assert not any(word in name for name in sig.parameters
                   for word in ("country", "region", "western", "origin", "sex", "age"))
    assert list(inspect.signature(rules.pair_conflict).parameters) == ["a", "b"]


def test_western_list_is_reference_data_only():
    """countries.is_western/region_of remain as reference data (country normalisation); nothing in rules/matching imports them."""
    import server.matching as matching, server.rules as rules_mod
    for mod in (matching, rules_mod):
        assert not hasattr(mod, "is_western") and not hasattr(mod, "region_of")
    for c in ("Australia", "United States", "Germany", "Sweden", "Italy", "Brazil", "Japan", "South Korea", "Singapore", "China", "Hong Kong", "Taiwan", "Ireland",
              "Guatemala", "Costa Rica", "Panama", "Cuba", "Jamaica", "Dominican Republic", "Mexico"):  # M11(e): Central America and the Caribbean count as Western
        assert is_western(c), c
    for c in ("India", "Poland", "Vietnam", "Nigeria", "Saudi Arabia", "Indonesia", "Turkey", "Israel", "South Africa"):
        assert not is_western(c), c


def test_country_normalisation():
    assert normalise_country("brasil") == ("Brazil", True)
    assert normalise_country("UK") == ("United Kingdom", True)
    assert normalise_country("Sverige") == ("Sweden", True)
    assert normalise_country("Zzzzland")[1] is False
    assert region_of("Vietnam") == "South-East Asia"
    assert region_of("India") == "South Asia"


# ---------------- 26 October 2026 approvals: I14(b)/(c), A4(e), I16(b) per-person M10 to M12 ----------------
def test_price_guide_triple_share_figures_i14():
    """I14(b)/(c): 2 bedroom / 1 bathroom triple share is $157 to $198 (12-month) and $169 to $211 (6-month), per-room method."""
    g12 = next(o for o in rules.price_guide("12m") if o["id"] == "2B1B-triple")
    g6 = next(o for o in rules.price_guide("6m") if o["id"] == "2B1B-triple")
    assert (g12["min"], g12["max"]) == (157, 198) and (g6["min"], g6["max"]) == (169, 211)
    assert "$157 to $198" in g12["label"] and "$169 to $211" in g6["label"]
    # the per-room method (A12(e)) reproduces Bill's figures from the cheapest and dearest 2B1B rents
    derived = rules.compute_price_guide_from_list([{"type_code": "2B1B", "rent_12m": 798, "rent_6m": 874}, {"type_code": "2B1B", "rent_12m": 1050, "rent_6m": 1126}])
    assert (round(derived["12m:2B1B-triple"]["min"]), round(derived["12m:2B1B-triple"]["max"])) == (156, 198)
    assert (round(derived["6m:2B1B-triple"]["min"]), round(derived["6m:2B1B-triple"]["max"])) == (169, 211)


def test_budget_matching_uses_new_triple_figures_a16():
    """The price-guide tops (I14(b)/(c)) feed A16 as each member's stated maximum: the household sum, not any share, is tested."""
    assert rules.budget_max_for("2B1B-triple", "12m") == 198
    apt = apartment("2B1B", rent12=1050, avail=date(2027, 6, 20), bedrooms=["1 king or 2 singles or 3 singles or 1 king+1 single", "1 king or 2 singles"])
    triple_budget, twin_budget = rules.budget_max_for("2B1B-triple", "12m"), rules.budget_max_for("2B1B-twin", "12m")
    units = [unit(f"u{i}", budget=triple_budget, arrangements=("Share with two others",), move_in=date(2027, 7, 1), move_out=date(2028, 7, 1)) for i in range(3)]
    units += [unit(f"t{i}", budget=twin_budget, arrangements=("Share with one other",), move_in=date(2027, 7, 1), move_out=date(2028, 7, 1)) for i in range(2)]
    r = check(apt, units)
    assert r.ok, r.reasons  # 1050 + 70 = 1120 vs sum 3x198 + 2x263 = 1120: within the maximum itself
    assert r.household_budget["reason_code"] == "WITHIN_MAX" and r.household_budget["total_weekly_rent"] == "1120.00"
    apt2 = apartment("2B1B", rent12=1300, avail=date(2027, 6, 20), bedrooms=["1 king or 2 singles or 3 singles", "1 king or 2 singles"])
    r2 = check(apt2, units)
    assert not r2.ok and any("A16" in x for x in r2.reasons)  # 1370 > 1120 x 1.1 = 1232


def test_king_plus_single_room_takes_couple_and_sibling_a4e():
    """A4(e): a "1 king+1 single" room may be shared by a couple and one other person, such as a sibling."""
    apt = apartment("2B1B", rent12=1000, rent6=1050, bedrooms=["1 king or 2 singles or 3 singles or 1 king+1 single", "1 king or 2 singles"])
    party = [person("c1", unit="p", name="Anna", couple=True, arrangements=("Couple sharing one bed",), budget=250),
             person("c1m1", unit="p", name="Bo", couple=True, is_lead=False, arrangements=("Couple sharing one bed",), budget=250),
             person("c1m2", unit="p", name="Sis", is_lead=False, arrangements=("Share with two others",), budget=250)]
    others = [unit("x", budget=300, arrangements=("Share with one other",)), unit("y", budget=300, arrangements=("Share with one other",))]
    r = check(apt, [unit("p", people=party, kind="friends", sizes=("2 bedrooms",))] + others)
    assert r.ok, r.reasons
    room = next(x for x in r.allocation if "c1" in {o["person_id"] for o in x["occupants"]})
    assert room["setup"] == ["king", "single"] and {o["person_id"] for o in room["occupants"]} == {"c1", "c1m1", "c1m2"}
    assert {o["bed"] for o in room["occupants"] if o["person_id"] in ("c1", "c1m1")} == {"king"}
    assert next(o["bed"] for o in room["occupants"] if o["person_id"] == "c1m2") == "single"
    # A5/A12(e): the third bed in a 2-bedroom apartment carries the $70 and sits on that room
    assert r.extra_bed == 70 and room["extra_bed"] == 70 and r.total_weekly == 1070
    # a room without the king+single setup cannot take them (3 people need 3 singles, but a couple needs one double bed)
    apt2 = apartment("2B1B", rent12=1000, bedrooms=["1 king or 2 singles or 3 singles", "1 king or 2 singles"])
    r2 = check(apt2, [unit("p", people=party, kind="friends", sizes=("2 bedrooms",))] + others)
    assert not r2.ok and any("No bed allocation" in x for x in r2.reasons)


def test_member_country_is_identity_only_in_groups_launch_scope():
    """Launch scope (8 Nov 2026): each person's country of birth is carried for identity (I16(b), GP2) but a group result is
    identical whatever the countries are, for leads and for party members, in both directions."""
    apt = apartment("2B2B", rent12=1150)
    def grp(lead_c, member_c, other_c):
        friends = [person("f1", unit="f", country=lead_c, budget=300, arrangements=("Share with one other",)),
                   person("f1m1", unit="f", country=member_c, is_lead=False, budget=300, arrangements=("Share with one other",))]
        return check(apt, [unit("f", people=friends, kind="friends"), unit("w", country=other_c, budget=600, arrangements=("Own room",))])
    base = grp("Sweden", "India", "Germany")
    assert base.ok
    for combo in (("Sweden", "Norway", "Brazil"), ("United Kingdom", "Nepal", "Japan"), (None, None, None), ("Chile", "Norway", "Chile")):
        r = grp(*combo)
        assert (r.ok, r.reasons, r.lease_start, r.lease_end, r.total_weekly, r.shares) == (base.ok, base.reasons, base.lease_start, base.lease_end, base.total_weekly, base.shares), combo
    assert not any("5.3" in x or "5.4" in x or "M1" in x for x in base.reasons)


# ---------------- 26 October 2026 (second register update): A4(e) refined, I16(d), I16(e) ----------------
def _king_single_apt(code="2B1B", rent12=1000):
    return apartment(code, rent12=rent12, rent6=rent12 + 50, bedrooms=["1 king or 2 singles or 3 singles or 1 king+1 single", "1 king or 2 singles"])


def test_couple_never_shares_bedroom_with_unrelated_individual_a4e():
    """A4(e): a couple enquiry plus a separate individual enquirer are never put together in a "1 king+1 single" room."""
    apt = _king_single_apt()
    couple = [person("c1", unit="c", name="Anna", couple=True, arrangements=("Couple sharing one bed", "Share with one other"), budget=300),
              person("c1m1", unit="c", name="Bo", couple=True, is_lead=False, arrangements=("Couple sharing one bed", "Share with one other"), budget=300)]
    # the stranger would happily share a room with two others, and the budget fits: only A4(e) stands in the way
    stranger = unit("s", budget=300, arrangements=("Share with two others", "Share with one other", "Own room"))
    twins = [unit("x", budget=300, arrangements=("Share with one other",)), unit("y", budget=300, arrangements=("Share with one other",))]
    r = check(apt, [unit("c", people=couple, kind="couple", sizes=("2 bedrooms",)), stranger] + twins)
    assert not r.ok and any("No bed allocation" in x for x in r.reasons)
    # the same five people fit as soon as the stranger is replaced by a member of the couple's own party
    party = couple + [person("c1m2", unit="c", name="Sis", is_lead=False, arrangements=("Share with two others",), budget=300)]
    r2 = check(apt, [unit("c", people=party, kind="friends", sizes=("2 bedrooms",))] + twins)
    assert r2.ok, r2.reasons
    room = next(x for x in r2.allocation if "c1" in {o["person_id"] for o in x["occupants"]})
    assert {o["person_id"] for o in room["occupants"]} == {"c1", "c1m1", "c1m2"}
    # direct check of the helper: couple + stranger in one room fails; couple alone passes; couple + own sibling passes
    assert rules._room_ok_a4e(couple + [stranger.people[0]], 2) is False
    assert rules._room_ok_a4e(couple, 1) is True
    assert rules._room_ok_a4e(party, 2) is True


def test_couple_plus_sibling_needs_other_bedrooms_a4e():
    """A4(e): the king+single trio is for a party seeking flatmates for the OTHER bedrooms, so not in a one-room apartment."""
    party = [person("c1", unit="c", couple=True, arrangements=("Couple sharing one bed",), budget=400),
             person("c1m1", unit="c", couple=True, is_lead=False, arrangements=("Couple sharing one bed",), budget=400),
             person("c1m2", unit="c", is_lead=False, arrangements=("Share with two others",), budget=400)]
    studio = apartment("Studio", rent12=700, rent6=750, bedrooms=["1 king or 2 singles or 3 singles or 1 king+1 single"])
    r = check(studio, [unit("c", people=party, kind="friends", sizes=("Studio",), mate_max=2)])
    assert not r.ok and any("No bed allocation" in x or "fill every bed" in x for x in r.reasons)  # GP12 now refuses the trio alone first
    assert rules._room_ok_a4e(party, 1) is False


def test_couple_still_gets_a_room_to_itself_in_a_shared_apartment():
    """A couple alone in one bedroom, strangers in the other bedroom: unchanged by the A4(e) refinement."""
    apt = _king_single_apt()
    couple = [person("c1", unit="c", couple=True, arrangements=("Couple sharing one bed",), budget=300),
              person("c1m1", unit="c", couple=True, is_lead=False, arrangements=("Couple sharing one bed",), budget=300)]
    twins = [unit("x", budget=300, arrangements=("Share with one other",)), unit("y", budget=300, arrangements=("Share with one other",))]
    r = check(apt, [unit("c", people=couple, kind="couple", sizes=("2 bedrooms",))] + twins)
    assert r.ok, r.reasons
    room = next(x for x in r.allocation if "c1" in {o["person_id"] for o in x["occupants"]})
    assert {o["person_id"] for o in room["occupants"]} == {"c1", "c1m1"} and room["setup"] == ["king"]


# ---------------- A17 vacancy cap in the engine, business clock, unusable dates ----------------
def test_a17_vacancy_cap_counts_from_later_of_today_and_availability():
    """A17(a)/(b): future empty days from max(today, available) to the chosen start; 5% of the stay rounded down."""
    # stay 1 Feb 2027 -> 15 Feb 2028 = 379 days -> 18 permitted
    units = lambda: [unit("a", in_flex=0), unit("b", in_flex=0)]
    ok = check(apartment(avail=date(2027, 1, 14)), units(), today=date(2026, 11, 8))      # 18 empty days: at the cap
    assert ok.ok, ok.reasons
    over = check(apartment(avail=date(2027, 1, 13)), units(), today=date(2026, 11, 8))    # 19 empty days
    assert not over.ok and over.reasons == ["19 future empty days exceed the permitted 18 (5% of a 379-day stay) (A17, counted from 2027-01-13)"]
    # elapsed vacancy is ignored: the apartment has been empty since October, but today is 20 January -> 12 empty days
    assert check(apartment(avail=date(2026, 10, 1)), units(), today=date(2027, 1, 20)).ok
    late = check(apartment(avail=date(2026, 10, 1)), units(), today=date(2027, 1, 13))
    assert not late.ok and "counted from 2027-01-13" in late.reasons[0]
    # the start is never before availability (A17(c)): availability inside the window moves the start, not the other way round
    r = check(apartment(avail=date(2027, 2, 5)), [unit("a", in_flex=7), unit("b", in_flex=7)], today=date(2026, 11, 8))
    assert r.ok and r.lease_start == date(2027, 2, 5)
    # the cap does not waive the minimum lease or the exit rule (A17(c)): a short stay fails on A9 even with no vacancy at all
    short = check(apartment(avail=date(2027, 2, 1)), [unit("a", move_out=date(2027, 3, 1), out_flex=0), unit("b", move_out=date(2027, 3, 1), out_flex=0)], today=date(2027, 2, 1))
    assert not short.ok and any("A9" in x or "blackout" in x for x in short.reasons)


def test_a17_business_clock_melbourne_and_injectable():
    from datetime import datetime, timezone
    from server import lease_dates as ld
    # 13:30 UTC on 8 Nov is 00:30 on 9 Nov in Melbourne (UTC+11 in summer): the business date is the 9th, not the UTC date
    assert ld.melbourne_today(datetime(2026, 11, 8, 13, 30, tzinfo=timezone.utc)) == date(2026, 11, 9)
    assert ld.melbourne_today(datetime(2026, 11, 8, 12, 59, tzinfo=timezone.utc)) == date(2026, 11, 8)
    # winter (UTC+10): 14:00 UTC on 1 July is 00:00 on 2 July in Melbourne
    assert ld.melbourne_today(datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)) == date(2026, 7, 2)
    assert ld.melbourne_today(datetime(2026, 7, 1, 13, 59, tzinfo=timezone.utc)) == date(2026, 7, 1)
    with pytest.raises(TypeError):
        ld.melbourne_today(datetime(2026, 11, 8, 13, 30))   # naive: never guessed
    assert isinstance(ld.melbourne_today(), date)
    # the suite pins the clock (conftest); check_group with today=None uses it, an explicit today overrides it
    from tests.conftest import TEST_TODAY
    assert ld.business_today() == TEST_TODAY == date(2026, 11, 8)
    before = ld.CLOCK
    try:
        ld.CLOCK = lambda: date(2027, 1, 13)
        assert not check(apartment(avail=date(2026, 10, 1)), [unit("a", in_flex=0), unit("b", in_flex=0)]).ok
        ld.CLOCK = lambda: date(2027, 1, 20)
        assert check(apartment(avail=date(2026, 10, 1)), [unit("a", in_flex=0), unit("b", in_flex=0)]).ok
        ld.CLOCK = lambda: datetime(2027, 1, 20, 9, 0, tzinfo=timezone.utc)   # a clock returning a datetime is a bug, not a date
        with pytest.raises(TypeError):
            check(apartment(), [unit("a"), unit("b")])
    finally:
        ld.CLOCK = before
    with pytest.raises(TypeError):
        check(apartment(), [unit("a"), unit("b")], today="2027-01-20")
    assert ld.CLOCK is ld.melbourne_today or ld.business_today() == TEST_TODAY   # live default is the Melbourne clock, never a frozen date


def test_unusable_dates_exclude_the_enquiry_with_a_reason_not_a_crash():
    """Root's probe: an enquiry at the calendar's edge overflowed the flexibility window and aborted the whole run."""
    from server import matching
    apt = apartment(avail=date(2027, 1, 20))
    far = [unit("a", move_in=date(9999, 10, 1), move_out=date(9999, 12, 31)), unit("b", move_in=date(9999, 10, 1), move_out=date(9999, 12, 31))]
    r = check(apt, far, today=date(2026, 11, 8))
    assert not r.ok and len(r.reasons) == 2 and all("supported calendar" in x and "staff must correct" in x for x in r.reasons) and r.lease_start is None
    assert far[0].date_problem() and unit("ok").date_problem() is None
    # the pre-filters leave the enquiry out instead of raising, so other candidates are still formed
    assert matching._unit_fits_apartment(apt, far[0]) is False
    assert matching._pair_ok(far[0], unit("c")) is False and matching._pair_ok(unit("c"), far[0]) is False
    assert matching._pair_ok(unit("c"), unit("d")) is True
    # a wrong type in a stored date is reported, not guessed
    bad = unit("x")
    bad.move_in = "2027-02-01"
    assert "not a usable date" in bad.date_problem()
    assert not check(apt, [bad, unit("y")], today=date(2026, 11, 8)).ok
    # near the edge but still computable: no problem reported (nothing is clamped)
    edge = unit("e", move_in=date(9999, 11, 1), move_out=date(9999, 12, 7), in_flex=7, out_flex=14)
    assert edge.date_problem() is None


def test_calendar_bounds_in_the_whole_engine_give_reasons_not_overflow():
    """Root's second probe: with every date at the calendar's lower bound (flex 0) choose_lease_dates stepped below date.min.
    Every corner of the supported calendar must come back as a reason (or a pass), never an OverflowError."""
    corners = [(date.min, date.min, date.min, date.min), (date.max, date.max, date.max, date.max), (date.min, date.max, date.min, date.min),
               (date.max, date.max, date.min, date.min), (date.min, date.min, date.max, date.max), (date(2027, 2, 1), date(2028, 2, 1), date.min, date.min)]
    for mi, mo, avail, today in corners:
        units = [unit("u1", move_in=mi, in_flex=0, move_out=mo, out_flex=0), unit("u2", move_in=mi, in_flex=0, move_out=mo, out_flex=0)]
        r = check(apartment(avail=avail), units, today=today)
        assert not r.ok and r.reasons, (mi, mo, avail, today)
    # the degenerate single-day exit window at the lower bound is the exact case that overflowed
    assert rules.choose_lease_dates(apartment(avail=date.min), [unit("u1", move_in=date.min, in_flex=0, move_out=date.min, out_flex=0)])[1] is None
    # the far-future long stay with an ancient listing is an A17 rejection with the real count, not a crash or a clamp
    r = check(apartment(avail=date.min), [unit("u1"), unit("u2")], today=date.min)
    assert any("future empty days exceed" in x and "(A17" in x for x in r.reasons)


def test_business_timezone_is_portable_and_fails_closed(monkeypatch):
    """Deployment check: the Melbourne zone must come from the pinned `tzdata` package when the host has no OS zoneinfo
    (python:slim images), and a host with neither must get a clear error, never a UTC guess. The lookup is lazy, so importing
    the module never depends on zone data."""
    import importlib.util
    import zoneinfo
    from datetime import datetime, timezone
    from server import lease_dates
    assert importlib.util.find_spec("tzdata") is not None, "tzdata is pinned in requirements.txt; install it in this environment"
    saved = zoneinfo.TZPATH
    try:
        zoneinfo.reset_tzpath(to=[])        # OS zone database disabled: only the tzdata package can answer
        zoneinfo.ZoneInfo.clear_cache()
        assert lease_dates.business_tz().key == "Australia/Melbourne"
        summer = datetime(2026, 11, 8, 13, 30, tzinfo=timezone.utc)
        assert lease_dates.melbourne_today(summer) == date(2026, 11, 9)
        # neither OS data nor the package: a named error at the point of use, no fallback
        monkeypatch.setattr(lease_dates, "ZoneInfo", lambda key: (_ for _ in ()).throw(zoneinfo.ZoneInfoNotFoundError(key)))
        with pytest.raises(lease_dates.BusinessTimezoneUnavailable, match="tzdata"):
            lease_dates.melbourne_today(summer)
    finally:
        zoneinfo.reset_tzpath(to=saved)
        zoneinfo.ZoneInfo.clear_cache()
