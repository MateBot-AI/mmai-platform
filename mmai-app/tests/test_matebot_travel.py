"""MateBot group travel arithmetic and shortlist roles (GP6(d), H28, M4). Pure: no provider, model or database is touched.

The differing or missing stated-time case is NOT ruled (register, "MateBot group travel threshold"): these tests pin that it
is never decided, by any default, average, minimum or maximum.
"""
import inspect
from decimal import Decimal
from fractions import Fraction

import pytest

from server import homebot_travel as ht
from server import matebot_travel as mt

W, B, PT = mt.MODES
U = ht.UNMEASURED


def M(i, time, modes, journeys):
    return mt.Member(i, time, modes, journeys)


def same(time, minutes, modes=(PT,)):
    return [M(i, time, modes, {m: v for m in modes}) for i, v in enumerate(minutes)]


def test_the_choices_and_times_are_the_forms_own():
    assert mt.MODES == ("Walk", "Bicycle", "Public transport") and mt.MAX_SHORTLIST == 3
    assert mt.STATED_LIMIT == {"5 minutes": 5, "15 minutes": 15, "30 minutes": 30, "45 minutes": 45, "60+ minutes": 60}
    src = inspect.getsource(mt)
    for banned in ("import os", "requests", "anthropic", "sqlite", "datetime", "from .db", "from . import ai"):
        assert banned not in src, banned


# ------------------------------------------------------------------------------------- same stated time: H28 as written
def test_h28_examples_apply_when_everyone_states_the_same_time():
    r = mt.assess_group(same("30 minutes", [10, 10, 10, 90, 90]))                     # H28(b)
    assert r.status == mt.PASS and r.facts["majority"]["indices"] == [0, 1, 2] and r.facts["ceiling_minutes"] == 60
    assert ht.LONG_JOURNEY_WARNING in r.reasons and [p["warning"] for p in r.facts["people"]][3:] == [ht.LONG_JOURNEY_WARNING] * 2
    r = mt.assess_group(same("30 minutes", [10, 10, 90, 100, 110]))                   # H28(e): about 37 minutes
    assert r.status == mt.PASS and r.facts["majority"]["average_display"] == "36.7" and r.facts["meets_stated_limit"] is False
    r = mt.assess_group(same("30 minutes", [10, 90, 90, 100, 110]))
    assert r.status == mt.EXCEEDS_CEILING and r.facts["within_ceiling"] is False
    r = mt.assess_group(same("60+ minutes", [120, 120, 121]))                         # H28(a): 120-minute ceiling, exact
    assert r.status == mt.PASS and r.facts["ceiling_minutes"] == 120
    assert mt.assess_group(same("60+ minutes", [120, Decimal("120.01"), 300])).status == mt.EXCEEDS_CEILING


@pytest.mark.parametrize("minutes", [[10, 10, 10, 90, 90], [61, 61, 61], [60, 60, 61, 61], [0], [59, 61], [Fraction(3601, 60), 60, 60],
                                     [10, 20, 30, 40, 50, 60, 70]])
def test_it_agrees_exactly_with_the_homebot_calculation_for_one_shared_preference(minutes):
    """Same people, same choices, same stated time: the two calculations must give the same result and the same figures."""
    for modes in ((PT,), (W, B, PT)):
        mine = mt.assess_group([M(i, "30 minutes", modes, {m: (v if m == modes[-1] else v + 7) for m in modes}) for i, v in enumerate(minutes)])
        pref = ht.TravelPreference("30 minutes", modes)
        theirs = ht.assess_travel(pref, [ht.PersonTravel(i, ht.Destination({m: (v if m == modes[-1] else v + 7) for m in modes}, pref))
                                         for i, v in enumerate(minutes)])
        assert mine.status == theirs.status
        for k in ("majority", "within_ceiling", "meets_stated_limit", "ceiling_minutes", "majority_size", "known_destinations"):
            assert mine.facts[k] == theirs.facts[k], k
        assert [(p["fastest_minutes"], p["fastest_modes"], p["in_majority"], p["warning"]) for p in mine.facts["people"]] == \
               [(p["fastest_minutes"], p["fastest_modes"], p["in_majority"], p["warning"]) for p in theirs.facts["people"]]


def test_each_member_uses_only_the_choices_they_accepted():
    """One walks only, one cycles or walks, one takes public transport: each fastest ACCEPTED journey, named."""
    r = mt.assess_group([M(0, "30 minutes", [W], {W: 50}), M(1, "30 minutes", [B, W], {W: 50, B: 20}), M(2, "30 minutes", [PT], {PT: 25})])
    assert [(p["fastest_minutes"], p["fastest_modes"]) for p in r.facts["people"]] == [("50", [W]), ("20", [B]), ("25", [PT])]
    assert r.status == mt.PASS and r.facts["majority"]["indices"] == [1, 2]
    assert r.facts["within_own_limit"] == [1, 2] and r.facts["over_own_limit"] == [0]
    with pytest.raises(ht.InvalidTravelInput):                                        # a journey by a choice not accepted is refused
        M(0, "30 minutes", [W], {W: 50, PT: 10})
    with pytest.raises(ht.InvalidTravelInput):
        M(0, "30 minutes", [W, PT], {W: 50})                                          # and an accepted choice cannot be left out silently
    for bad in (["Car"], ["Tram"], "Walk", [W, W], [None]):
        with pytest.raises(ht.InvalidTravelInput):
            M(0, "30 minutes", bad, None)
    for bad in ("20 minutes", ["30 minutes"], 30, {"30 minutes": 1}):                 # refused as travel input, never a TypeError
        with pytest.raises(ht.InvalidTravelInput):
            M(0, bad, [W], None)
    with pytest.raises(ht.InvalidTravelInput):
        M(0, "60 minutes or more", [W], None)                                         # the HomeBot wording is not a MateBot answer


def test_no_destination_is_left_out_of_the_majority_and_nobody_at_all_is_no_result():
    r = mt.assess_group([M(0, "15 minutes", [W], {W: 10}), M(1, "15 minutes", [W], {W: 12}), M(2, "15 minutes", [W], None)])
    assert r.status == mt.PASS and r.facts["known_destinations"] == 2 and r.facts["majority_size"] == 2 and r.facts["no_destination"] == [2]
    r = mt.assess_group([M(0, "15 minutes", [W], None), M(1, None, [], None)])
    assert r.status == mt.NO_DESTINATIONS and r.facts["majority"] is None


# ---------------------------------------------------------------------------- differing or missing times: never decided
@pytest.mark.parametrize("times", [("15 minutes", "30 minutes", "30 minutes"), ("30 minutes", None, "30 minutes"), (None, None, None),
                                   ("5 minutes", "60+ minutes", None)])
@pytest.mark.parametrize("minutes", [(1, 1, 1), (500, 500, 500), (10, 40, 70)])
def test_differing_or_missing_times_never_produce_a_group_result(times, minutes):
    r = mt.assess_group([M(i, t, [PT], {PT: v}) for i, (t, v) in enumerate(zip(times, minutes))])
    assert r.status == mt.NEEDS_CONFIRMATION                                           # however short or long the journeys are
    f = r.facts
    assert f["majority"] is None and f["within_ceiling"] is None and f["meets_stated_limit"] is None
    assert f["stated_limit_minutes"] is None and f["ceiling_minutes"] is None and f["same_stated_limit"] is False
    assert f["unruled"] == mt.UNRULED_NOTE and not any(p["in_majority"] or p["warning"] for p in f["people"])
    assert (mt.TRAVEL_LIMIT_MISSING in r.reasons) == (None in times)
    assert (mt.TRAVEL_LIMITS_DIFFER in r.reasons) == (len({t for t in times if t}) > 1)
    # each member is still reported against their OWN stated time, and only that
    for p, t, v in zip(f["people"], times, minutes):
        assert p["fastest_minutes"] == str(v) and p["stated_limit_minutes"] == mt.STATED_LIMIT.get(t)
        assert p["own_limit"] == (mt.NO_STATED_LIMIT if t is None else mt.WITHIN_OWN_LIMIT if v <= mt.STATED_LIMIT[t] else mt.OVER_OWN_LIMIT)


def test_no_default_average_minimum_or_maximum_is_in_the_code():
    """A missing answer is never read as 30 minutes, and the times are never combined."""
    src = inspect.getsource(mt._assess) + inspect.getsource(mt._own_limit) + inspect.getsource(mt.Member)
    for banned in ("max(", "mean(", "statistics", ", 30)", "or 30", "get(self.stated_time, "):
        assert banned not in src, banned
    assert mt.Member(0, None, [W], {W: 1}).limit is None
    assert src.count("min(") == 1                                                      # only the fastest accepted journey of one person


def test_someone_who_never_answered_the_travel_choices_has_no_journey_selected():
    r = mt.assess_group([M(0, "30 minutes", [PT], {PT: 5}), M(1, "30 minutes", [PT], {PT: 5}), M(2, "30 minutes", [], {})])
    assert r.status == mt.NEEDS_CONFIRMATION and r.reasons == (mt.TRAVEL_MODE_NOT_ANSWERED,)
    p = r.facts["people"][2]
    assert p["status"] == mt.NO_MODE_ANSWER and p["fastest_minutes"] is None and p["own_limit"] == mt.NOT_MEASURED and r.facts["no_mode_answer"] == [2]
    assert r.facts["majority"] is None                                                 # even though two of three would carry it


def test_an_unmeasured_or_partly_measured_journey_is_neither_a_pass_nor_a_failure():
    r = mt.assess_group([M(0, "30 minutes", [PT], {PT: 5}), M(1, "30 minutes", [PT], {PT: 5}), M(2, "30 minutes", [PT], {PT: U})])
    assert r.status == mt.NEEDS_CONFIRMATION and r.reasons == (ht.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES,)
    r = mt.assess_group([M(0, "30 minutes", [W, PT], {W: 500, PT: U}), M(1, "30 minutes", [W, PT], {W: 20, PT: U})])
    assert r.status == mt.NEEDS_CONFIRMATION and ht.FASTEST_MODE_UNCONFIRMED in r.reasons
    a, b = r.facts["people"]
    assert (a["bound_minutes"], a["fastest_minutes"], a["own_limit"]) == ("500", None, mt.NOT_MEASURED)   # another choice may be faster
    assert (b["bound_minutes"], b["own_limit"]) == ("20", mt.WITHIN_OWN_LIMIT)                            # it can only be faster still


def test_order_and_decimal_context_do_not_matter_and_bad_input_is_refused():
    from decimal import getcontext
    ms = same("30 minutes", [Decimal("59.999"), 60, Decimal("60.001"), 200, 300])
    a = mt.assess_group(ms)
    getcontext().prec = 2
    try:
        b = mt.assess_group(list(reversed(ms)))
    finally:
        getcontext().prec = 28
    assert a == b and a.status == mt.PASS and a.facts["majority"]["total_minutes"] == "180"
    for bad in ([], "x", {}, same("30 minutes", [1] * 8), [M(0, None, [], None), M(0, None, [], None)], [object()]):
        with pytest.raises(ht.InvalidTravelInput):
            mt.assess_group(bad)
    for bad in (True, -1, "0"):
        with pytest.raises(ht.InvalidTravelInput):
            M(bad, None, [], None)


# ------------------------------------------------------------------------------------------------ travel last (GP6(d))
def _result(status):
    return ht.TravelAssessment(status, (), {})


def test_a_failed_apartment_is_replaced_by_the_next_and_the_others_keep_their_places():
    calls = []
    verdict = {1: mt.PASS, 2: mt.EXCEEDS_CEILING, 3: mt.NEEDS_CONFIRMATION, 4: mt.EXCEEDS_CEILING, 5: mt.PASS, 6: mt.PASS}
    out = mt.travel_last([1, 2, 3, 4, 5, 6], lambda a: calls.append(a) or _result(verdict[a]))
    assert [a for a, _ in out["shortlist"]] == [1, 3, 5] and [a for a, _ in out["failed"]] == [2, 4] and out["not_checked"] == [6]
    assert calls == [1, 2, 3, 4, 5]                                                    # the replacement is checked; nothing beyond need
    assert [r.status for _, r in out["shortlist"]] == [mt.PASS, mt.NEEDS_CONFIRMATION, mt.PASS]   # needs confirmation keeps its place


def test_one_or_two_are_enough_and_nothing_is_padded_or_searched_for():
    out = mt.travel_last([1, 2, 3, 4], lambda a: _result(mt.PASS if a == 3 else mt.EXCEEDS_CEILING))
    assert [a for a, _ in out["shortlist"]] == [3] and len(out["failed"]) == 3 and out["not_checked"] == []
    out = mt.travel_last([1, 2], lambda a: _result(mt.EXCEEDS_CEILING))
    assert out["shortlist"] == [] and len(out["failed"]) == 2
    assert mt.travel_last([], lambda a: 1 / 0) == {"shortlist": [], "failed": [], "not_checked": []}
    out = mt.travel_last([1, 2, 3, 4], lambda a: _result(mt.NO_DESTINATIONS))
    assert [a for a, _ in out["shortlist"]] == [1, 2, 3] and out["not_checked"] == [4]
    for bad in (4, 0, True, "3"):
        with pytest.raises(ht.InvalidTravelInput):
            mt.travel_last([1], lambda a: _result(mt.PASS), max_shortlist=bad)         # no fourth apartment
    with pytest.raises(ht.InvalidTravelInput):
        mt.travel_last([1, 1], lambda a: _result(mt.PASS))
    with pytest.raises(ht.InvalidTravelInput):
        mt.travel_last([1], lambda a: "PASS")


# --------------------------------------------------------------------------------------------- three at most for staff
def _roles(spec):
    return mt.shortlist_roles([{"id": i, "current": "c" in s, "unavailable": "x" in s} for i, s in enumerate(spec)])


def test_at_most_three_hold_a_place_and_the_rest_are_reserves():
    S, R, X = mt.ROLE_SHORTLIST, mt.ROLE_RESERVE, mt.ROLE_UNAVAILABLE
    assert _roles(["c", "", "", "", "", ""]) == [S, S, S, R, R, R]
    assert _roles(["", ""]) == [S, S] and _roles([]) == []
    assert _roles(["", "", "", "", "c", ""]) == [S, S, R, R, S, R]                     # the current offer always holds a place
    assert _roles(["c", "x", "", "x", "", ""]) == [S, X, S, X, S, R]                   # leased or declined ones take no place
    assert _roles(["cx", "", "", "", ""]) == [X, S, S, S, R]                           # a leased current offer gives up its place to the next
    assert _roles(["x", "x", "x", "x"]) == [X, X, X, X]
    for spec in (["c", "", "", "", "", ""], ["", "x", "c", "", "", "", "", ""]):
        assert _roles(spec).count(S) <= 3
