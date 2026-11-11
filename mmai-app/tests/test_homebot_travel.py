"""Pure H28 travel maths: server/homebot_travel.py. No provider, network, AI, database or personal data — journeys are injected
minutes per accepted mode, people are integer positions. Worked examples are the register's own (H28(a),(b),(e))."""
from decimal import Decimal, localcontext
from itertools import permutations

import pytest

from server import homebot, homebot_travel as tv

ALL = list(tv.TRANSPORT)


def pref(max_travel="30 minutes", modes=("Tram",)):
    return tv.TravelPreference(max_travel, modes)


def person(idx, minutes, p=None, modes=("Tram",), secondary=None):
    """minutes: a number (same for every accepted mode), a dict mode->value, or None for NO_DESTINATION."""
    p = p or pref(modes=modes)
    if minutes is None:
        return tv.PersonTravel(idx, tv.NO_DESTINATION, secondary)
    routes = minutes if isinstance(minutes, dict) else {m: minutes for m in p.accepted_modes}
    return tv.PersonTravel(idx, tv.Destination(routes, p), secondary)


def party(values, p=None, modes=("Tram",)):
    p = p or pref(modes=modes)
    return p, [person(i, v, p) for i, v in enumerate(values, start=1)]


# ------------------------------------------------------------------------------------------------------- vocabulary
def test_vocabulary_matches_the_form_exactly():
    assert list(tv.MAX_TRAVEL) == homebot.MAX_TRAVEL and list(tv.TRANSPORT) == homebot.TRANSPORT
    assert tv.MAX_PEOPLE == homebot.MAX_PEOPLE


@pytest.mark.parametrize("option,limit,ceiling", [("5 minutes — walking distance only", 5, 10), ("15 minutes", 15, 30),
                                                   ("30 minutes", 30, 60), ("45 minutes", 45, 90), ("60 minutes or more", 60, 120)])
def test_limits_and_doubled_ceiling_h28a(option, limit, ceiling):
    p = tv.TravelPreference(option, ALL)
    assert p.limit == limit and p.ceiling == ceiling


# ------------------------------------------------------------------------------------------------------- validation
def test_preference_validation():
    for bad in [("30 min", ["Tram"]), ("30 minutes", []), ("30 minutes", ["Uber"]), ("30 minutes", "Tram"), (30, ["Tram"]),
                ("30 minutes", [None]), ("30 minutes", ["tram"])]:
        with pytest.raises(tv.InvalidTravelInput):
            tv.TravelPreference(*bad)
    p = tv.TravelPreference("30 minutes", ["Car", "Tram", "Tram", "Walk"])
    assert p.accepted_modes == ("Walk", "Tram", "Car")      # deduplicated, form order, immutable tuple


def test_destination_validation_exact_accepted_modes_only():
    p = pref(modes=("Tram", "Bus"))
    tv.Destination({"Tram": 10, "Bus": tv.UNMEASURED}, p)
    for routes in [{"Tram": 10}, {"Tram": 10, "Bus": 12, "Car": 5}, {"Tram": 10, "Car": 5}, {"Tram": 10, "Bus": "12"},
                   {"Tram": 10, "Bus": "unknown"}, {"Tram": 10, "Bus": None}, {"Tram": True, "Bus": 1}, {"Tram": 1.5, "Bus": 1},
                   {"Tram": -1, "Bus": 1}, {"Tram": Decimal("NaN"), "Bus": 1}, {"Tram": Decimal("Infinity"), "Bus": 1},
                   {"Tram": Decimal("1.0000000001"), "Bus": 1}, {"Tram": 12345678, "Bus": 1}, {"Tram": Decimal("1E+7"), "Bus": 1},
                   {"Tram": Decimal("1E+1000000"), "Bus": 1}, {"Tram": Decimal("1E-1000000"), "Bus": 1},
                   {"Tram": Decimal("-1E-999999"), "Bus": 1}, [("Tram", 1), ("Bus", 1)]]:
        with pytest.raises(tv.InvalidTravelInput):
            tv.Destination(routes, p)
    # no duration cut-off: long finite times are valid input and simply long; sub-minute precision up to 9 places is exact
    d = tv.Destination({"Tram": 1500, "Bus": Decimal("1440.25")}, p)
    assert d.status == tv.MEASURED and d.best == Decimal("1440.25") and d.best_modes == ("Bus",)
    d = tv.Destination({"Tram": Decimal("60.001"), "Bus": Decimal("9999999.999999999")}, p)
    assert d.best == Decimal("60.001")


def test_minutes_magnitude_checked_before_formatting_and_zero_canonicalised():
    for v in [Decimal("1E+1000000"), Decimal("1E+7"), Decimal("-1"), Decimal("1E-10"), Decimal("1E-999999"), Decimal("NaN"),
              Decimal("-Infinity"), 1.0, True, "5", None]:
        with pytest.raises(tv.InvalidTravelInput):
            tv._minutes(v)
    for z in [0, Decimal("0"), Decimal("-0"), Decimal("0E+1000000"), Decimal("-0E-999999"), Decimal("0.000")]:
        assert tv._minutes(z) == 0 and str(tv._minutes(z)) == "0"
    assert str(tv._minutes(Decimal("1E+6"))) == "1000000" and str(tv._minutes(Decimal("1E-9"))) == "1E-9"
    assert tv._minutes(Decimal("60.001")) == Decimal("60.001")   # never rounded


def test_destinations_are_revalidated_against_the_assessment_preference():
    car = tv.TravelPreference("5 minutes — walking distance only", ["Car"])
    walk = tv.TravelPreference("5 minutes — walking distance only", ["Walk"])
    fast_by_car = tv.Destination({"Car": 3}, car)
    with pytest.raises(tv.InvalidTravelInput):                   # a Car-only destination can never pass a Walk-only preference
        tv.assess_travel(walk, [tv.PersonTravel(1, fast_by_car)])
    both = tv.TravelPreference("30 minutes", ["Walk", "Car"])
    only_walk = tv.TravelPreference("30 minutes", ["Walk"])
    d_both = tv.Destination({"Walk": 20, "Car": 5}, both)
    d_walk = tv.Destination({"Walk": 20}, only_walk)
    with pytest.raises(tv.InvalidTravelInput):                   # extra mode
        tv.assess_travel(only_walk, [tv.PersonTravel(1, d_both)])
    with pytest.raises(tv.InvalidTravelInput):                   # missing mode
        tv.assess_travel(both, [tv.PersonTravel(1, d_walk)])
    with pytest.raises(tv.InvalidTravelInput):                   # secondary checked too
        tv.assess_travel(only_walk, [tv.PersonTravel(1, d_walk, d_both)])
    assert tv.assess_travel(only_walk, [tv.PersonTravel(1, d_walk)]).status == tv.PASS


def test_parent_reproduced_regressions_pinned():
    # (a) Car-only destination assessed under a Walk-only preference used to PASS with fastest_modes ['Car']
    car = tv.TravelPreference("5 minutes — walking distance only", ["Car"])
    walk = tv.TravelPreference("5 minutes — walking distance only", ["Walk"])
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(walk, [tv.PersonTravel(1, tv.Destination({"Car": 5}, car))])
    # (b) Decimal('1E+10') was accepted despite the 7-integer-digit cap
    with pytest.raises(tv.InvalidTravelInput):
        tv._minutes(Decimal("1E+10"))
    with pytest.raises(tv.InvalidTravelInput):
        tv.Destination({"Tram": Decimal("1E+10")}, pref())
    # (c) 60.00 + 60.01: prec=3 used to return PASS with total 120; prec=2 raised decimal.InvalidOperation
    p = pref()
    for prec in (2, 3):
        with localcontext() as c:
            c.prec = prec
            r = tv.assess_travel(p, [person(1, Decimal("60.00"), p), person(2, Decimal("60.01"), p)])
        assert r.status == tv.EXCEEDS_CEILING and r.facts["majority"]["total_minutes"] == "120.01"


def test_verdicts_independent_of_ambient_decimal_precision():
    p = pref()
    cases = [([Decimal("59.99"), Decimal("60.01")], tv.PASS, "120"), ([Decimal("60.00"), Decimal("60.01")], tv.EXCEEDS_CEILING, "120.01"),
             ([Decimal("59.999999999"), Decimal("60.000000001")], tv.PASS, "120"),
             ([Decimal("60"), Decimal("60.000000001")], tv.EXCEEDS_CEILING, "120.000000001"),
             ([Decimal("9999999.999999999")] * 7, tv.EXCEEDS_CEILING, "39999999.999999996")]
    for values, status, total in cases:
        expected = tv.assess_travel(p, [person(i, v, p) for i, v in enumerate(values, 1)]).as_dict()
        assert expected["status"] == status and expected["facts"]["majority"]["total_minutes"] == total
        for prec in (1, 2, 5, 100):
            with localcontext() as c:
                c.prec = prec
                got = tv.assess_travel(p, [person(i, v, p) for i, v in enumerate(values, 1)]).as_dict()
            assert got == expected, (prec, values)


def test_person_validation_and_contradiction_guard():
    p = pref()
    d = tv.Destination({"Tram": 10}, p)
    for idx in [True, -1, "1", 1.0, None]:
        with pytest.raises(tv.InvalidTravelInput):
            tv.PersonTravel(idx, d)
    with pytest.raises(tv.InvalidTravelInput):
        tv.PersonTravel(1, {"Tram": 10})
    with pytest.raises(tv.InvalidTravelInput):
        tv.PersonTravel(1, d, {"Tram": 10})
    with pytest.raises(tv.InvalidTravelInput):          # known secondary with NO primary is contradictory: pick the primary
        tv.PersonTravel(1, tv.NO_DESTINATION, d)
    tv.PersonTravel(0, tv.NO_DESTINATION)                # position 0 allowed; no destination allowed alone


def test_party_validation():
    p = pref()
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(p, [])
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(p, [person(i, 10, p) for i in range(8)])
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(p, [person(1, 10, p), person(1, 12, p)])          # duplicate positions
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(p, [person(1, 10, p), {"index": 2}])
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(("30 minutes", ["Tram"]), [person(1, 10, p)])
    with pytest.raises(tv.InvalidTravelInput):
        tv.assess_travel(p, person(1, 10, p))


def test_inputs_are_immutable_copies():
    p = pref(modes=("Tram", "Bus"))
    routes = {"Tram": 10, "Bus": 20}
    d = tv.Destination(routes, p)
    routes["Tram"] = 999
    assert d.best == 10
    with pytest.raises(Exception):
        d.routes = ()


# ------------------------------------------------------------------------------------------------- majority maths
@pytest.mark.parametrize("n,k", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (6, 4), (7, 4)])
def test_strict_majority_sizes(n, k):
    assert tv.majority_size(n) == k
    p, people = party([10] * n)
    r = tv.assess_travel(p, people)
    assert r.facts["majority_size"] == k and len(r.facts["majority"]["indices"]) == k


def test_register_example_h28e_three_warnings_and_pass():
    # 10, 10, 90, 100, 110 against a 30-minute preference: closest three average about 37 → within the 60-minute ceiling,
    # and every journey above 60 is warned — including the 90 inside the selected majority (H28(e)).
    p, people = party([10, 10, 90, 100, 110])
    r = tv.assess_travel(p, people)
    assert r.status == tv.PASS and r.reasons == (tv.TRAVEL_MAJORITY_WITHIN_CEILING, tv.LONG_JOURNEY_WARNING)
    m = r.facts["majority"]
    assert m["indices"] == [1, 2, 3] and m["total_minutes"] == "110" and m["average_display"] == "36.7"
    assert r.facts["within_ceiling"] is True and r.facts["meets_stated_limit"] is False
    assert [f["index"] for f in r.facts["people"] if f["warning"] == tv.LONG_JOURNEY_WARNING] == [3, 4, 5]
    assert [f["index"] for f in r.facts["people"] if f["in_majority"]] == [1, 2, 3]


def test_register_example_h28b_two_long_journeys_qualify_with_flags():
    p, people = party([10, 10, 10, 90, 90])
    r = tv.assess_travel(p, people)
    assert r.status == tv.PASS and r.facts["meets_stated_limit"] is True
    assert [f["index"] for f in r.facts["people"] if f["warning"]] == [4, 5]


def test_sixty_or_more_uses_120_ceiling():
    p, people = party([100, 110, 119, 200], p=pref("60 minutes or more"))
    r = tv.assess_travel(p, people)
    assert r.status == tv.PASS and r.facts["ceiling_minutes"] == 120 and r.facts["majority"]["total_minutes"] == "329"
    p, people = party([121, 121, 121], p=pref("60 minutes or more"))
    assert tv.assess_travel(p, people).status == tv.EXCEEDS_CEILING


def test_exact_total_comparison_before_rounding():
    # k = 2, ceiling 60: total 120 passes exactly; 120.01 fails although the rounded average "60.0" would look equal
    p, people = party([Decimal("59.99"), Decimal("60.01")])
    r = tv.assess_travel(p, people)
    assert r.status == tv.PASS and r.facts["majority"]["total_minutes"] == "120"
    p, people = party([Decimal("60.00"), Decimal("60.01")])
    r = tv.assess_travel(p, people)
    assert r.status == tv.EXCEEDS_CEILING and r.facts["majority"]["total_minutes"] == "120.01"
    assert r.facts["majority"]["average_display"] == "60.0"          # display only; the decision used the exact total
    # repeating average (k = 3 of 5): 181 / 3 is not a finite decimal, the exact total 181 > 180 decides
    p, people = party([60, 60, 61, 200, 200])
    r = tv.assess_travel(p, people)
    assert r.status == tv.EXCEEDS_CEILING and r.facts["majority"]["total_minutes"] == "181" and r.facts["majority"]["average_display"] == "60.3"
    p, people = party([60, 60, 60, 200, 200])
    r = tv.assess_travel(p, people)
    assert r.status == tv.PASS and r.facts["majority"]["total_minutes"] == "180"


def test_exceeds_ceiling_only_when_everything_is_measured():
    p, people = party([70, 80, 90])
    r = tv.assess_travel(p, people)
    assert r.status == tv.EXCEEDS_CEILING and r.reasons[0] == tv.TRAVEL_MAJORITY_EXCEEDS_CEILING
    assert r.facts["within_ceiling"] is False and r.facts["meets_stated_limit"] is False
    # same numbers plus one unmeasured known destination: no automatic rejection
    p = pref()
    people = [person(1, 70, p), person(2, 80, p), person(3, 90, p), person(4, tv.UNMEASURED, p)]
    people[3] = tv.PersonTravel(4, tv.Destination({"Tram": tv.UNMEASURED}, p))
    r = tv.assess_travel(p, people)
    assert r.status == tv.NEEDS_CONFIRMATION and r.facts["within_ceiling"] is None and r.facts["meets_stated_limit"] is None
    assert r.facts["majority"] is None and r.facts["known_destinations"] == 4 and r.facts["majority_size"] == 3


# ------------------------------------------------------------------------------------------- unknown states, honestly
def test_fastest_accepted_mode_identified_and_ties_listed_h28c():
    p = pref(modes=("Walk", "Tram", "Bus"))
    r = tv.assess_travel(p, [person(1, {"Walk": 40, "Tram": 12, "Bus": 12}, p)])
    f = r.facts["people"][0]
    assert r.status == tv.PASS and f["fastest_minutes"] == "12" and f["fastest_modes"] == ["Tram", "Bus"]


def test_partial_estimates_never_confirm_pass_but_show_a_labelled_bound():
    p = pref(modes=("Tram", "Bus"))
    people = [person(1, {"Tram": 10, "Bus": tv.UNMEASURED}, p), person(2, 10, p), person(3, 10, p)]
    r = tv.assess_travel(p, people)
    assert r.status == tv.NEEDS_CONFIRMATION
    assert r.reasons == (tv.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES, tv.FASTEST_MODE_UNCONFIRMED)
    assert r.facts["within_ceiling"] is None and r.facts["meets_stated_limit"] is None and r.facts["majority"] is None
    d = r.facts["upper_bound_diagnostic"]
    assert d["label"].startswith("Diagnostic only") and d["within_ceiling_if_bounds_hold"] is True and d["uses_bounds_for"] == [1]
    f1 = r.facts["people"][0]
    assert f1["status"] == tv.PARTIAL and f1["bound_minutes"] == "10" and f1["fastest_minutes"] is None
    assert f1["best_known_modes"] == ["Tram"] and f1["unknown_modes"] == ["Bus"]


def test_partial_best_known_long_time_is_provisional_not_confirmed():
    p = pref(modes=("Tram", "Train"))
    people = [person(1, {"Tram": 90, "Train": tv.UNMEASURED}, p), person(2, 10, p), person(3, 10, p)]
    r = tv.assess_travel(p, people)
    assert r.status == tv.NEEDS_CONFIRMATION
    assert r.facts["people"][0]["warning"] == tv.LONG_JOURNEY_PROVISIONAL and tv.LONG_JOURNEY_PROVISIONAL in r.reasons
    assert tv.LONG_JOURNEY_WARNING not in r.reasons


def test_insufficient_measured_values_for_a_majority():
    p = pref()
    unm = lambda i: tv.PersonTravel(i, tv.Destination({"Tram": tv.UNMEASURED}, p))
    r = tv.assess_travel(p, [person(1, 10, p), unm(2), unm(3)])
    assert r.status == tv.NEEDS_CONFIRMATION and r.facts["upper_bound_diagnostic"] is None
    assert set(r.reasons) == {tv.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES, tv.TRAVEL_NEEDS_CONFIRMATION_INSUFFICIENT_MEASURED}
    assert r.facts["unmeasured"] == [2, 3] and r.facts["measured"] == [1]


def test_no_destination_distinct_and_excluded_from_denominator():
    p = pref()
    # 2 known of 4 people: n = 2, k = 2 (both known must be measured); the two with no destination are listed, not counted
    r = tv.assess_travel(p, [person(1, 20, p), person(2, None, p), person(3, 30, p), person(4, None, p)])
    assert r.status == tv.PASS and r.facts["known_destinations"] == 2 and r.facts["majority_size"] == 2
    assert r.facts["no_destination"] == [2, 4] and r.facts["majority"]["indices"] == [1, 3]
    # unmeasured is NOT the same as no destination: it stays in n and blocks a confirmed pass
    r = tv.assess_travel(p, [person(1, 20, p), tv.PersonTravel(2, tv.Destination({"Tram": tv.UNMEASURED}, p)), person(3, 30, p)])
    assert r.status == tv.NEEDS_CONFIRMATION and r.facts["known_destinations"] == 3 and r.facts["unmeasured"] == [2]


def test_nobody_has_a_destination():
    p = pref()
    r = tv.assess_travel(p, [person(1, None, p), person(2, None, p)])
    assert r.status == tv.NO_DESTINATIONS and r.reasons == (tv.TRAVEL_NO_DESTINATIONS,)
    assert r.facts["majority"] is None and r.facts["within_ceiling"] is None and r.facts["majority_size"] == 0


def test_secondary_destination_is_display_only_h28f():
    p = pref(modes=("Tram", "Bus"))
    sec = tv.Destination({"Tram": 500, "Bus": tv.UNMEASURED}, p)       # long and incomplete: must not affect the result
    r = tv.assess_travel(p, [person(1, 10, p, secondary=sec), person(2, 10, p), person(3, 10, p)])
    assert r.status == tv.PASS and r.facts["majority"]["total_minutes"] == "20"
    s = r.facts["secondary_display"]
    assert s == [{"index": 1, "status": tv.PARTIAL, "best_known_minutes": "500", "best_known_modes": ["Tram"], "unknown_modes": ["Bus"],
                  "note": "secondary destination: shown only, never counted (H28(f))"}]
    assert r.facts["people"][0]["warning"] is None


def test_walk_only_option_with_other_modes_is_unresolved_not_silently_decided():
    p = tv.TravelPreference("5 minutes — walking distance only", ["Walk", "Car"])
    assert p.walk_only_ambiguous
    r = tv.assess_travel(p, [person(1, {"Walk": 4, "Car": 2}, p)])
    assert r.status == tv.NEEDS_CONFIRMATION and r.reasons[0] == tv.TRAVEL_PREFERENCE_MODE_AMBIGUITY
    assert r.facts["within_ceiling"] is None and r.facts["majority"] is None and "walking distance only" in r.facts["pending_interpretation"]
    # Walk-only follows the 5 / 10 maths
    p = tv.TravelPreference("5 minutes — walking distance only", ["Walk"])
    assert tv.assess_travel(p, [person(1, {"Walk": 10}, p)]).status == tv.PASS
    assert tv.assess_travel(p, [person(1, {"Walk": 11}, p)]).status == tv.EXCEEDS_CEILING
    # the 5-minute option with other modes and an incomplete estimate carries both codes
    p = tv.TravelPreference("5 minutes — walking distance only", ["Walk", "Tram"])
    r = tv.assess_travel(p, [person(1, {"Walk": 4, "Tram": tv.UNMEASURED}, p)])
    assert r.reasons[:2] == (tv.TRAVEL_PREFERENCE_MODE_AMBIGUITY, tv.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES)


def test_labels_present_in_every_result():
    p, people = party([10])
    r = tv.assess_travel(p, people)
    assert "08:00 Australia/Melbourne" in r.facts["routing_basis"] and "NOT independently provider-verified" in r.facts["routing_basis"]
    assert "supplied-estimate arithmetic only" in r.facts["pass_meaning"]
    assert set(r.as_dict()) == {"status", "reasons", "facts"}


def test_order_invariance_under_person_permutation():
    p = pref(modes=("Tram", "Bus"))
    base = [person(1, {"Tram": 10, "Bus": 50}, p), person(2, None, p), person(3, 90, p), person(4, {"Tram": 20, "Bus": tv.UNMEASURED}, p),
            person(5, 10, p)]
    expected = tv.assess_travel(p, base).as_dict()
    for perm in permutations(base):
        assert tv.assess_travel(p, list(perm)).as_dict() == expected
    assert expected["status"] == tv.NEEDS_CONFIRMATION and expected["facts"]["people"][0]["index"] == 1


def test_ties_in_the_majority_are_resolved_by_position_only():
    p, people = party([30, 30, 30, 30, 30])
    r = tv.assess_travel(p, people)
    assert r.facts["majority"]["indices"] == [1, 2, 3] and r.status == tv.PASS and r.facts["meets_stated_limit"] is True
