"""MateBot group travel arithmetic and shortlist roles (GP6(d), H28, M4). Pure: no provider, model or database is touched.

Members who state different maximum travel times (H28(j)): the closest strict majority, chosen by journey time, is compared
with twice the average of THOSE members' stated times. A missing stated time, anywhere in the group, is "needs confirmation".
Members level on journey time at the edge of the majority are an engineering safeguard, not a ruling: when counting one or
another of them would change the result, the result is "needs confirmation"; nobody is picked by position or by stated time.
"""
import itertools
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
        for k in ("within_ceiling", "meets_stated_limit", "ceiling_minutes", "majority_size", "known_destinations"):
            assert mine.facts[k] == theirs.facts[k], k
        mm, tm = mine.facts["majority"], theirs.facts["majority"]
        for k in ("size", "total_minutes", "average_display", "comparison"):
            assert mm[k] == tm[k], k
        assert mm["figure_determined"] is True and mm["limit_total_minutes"] == 30 * mm["size"]
        assert (mm["limit_average_display"], mm["ceiling_display"], mm["ceiling_range_display"]) == ("30.0", "60.0", None)
        mine_p, theirs_p = mine.facts["people"], theirs.facts["people"]
        assert [(p["fastest_minutes"], p["fastest_modes"], p["warning"]) for p in mine_p] == \
               [(p["fastest_minutes"], p["fastest_modes"], p["warning"]) for p in theirs_p]
        if mm["membership_determined"]:
            assert mm["indices"] == tm["indices"] and mm["tied_indices"] == [] and mine.facts["majority_tie"] is None
            assert [p["in_majority"] for p in mine_p] == [p["in_majority"] for p in theirs_p]
            assert mm["stated_limits_minutes"] == [30] * mm["size"]
        else:
            # members level on journey time at the edge: this calculation names them as tied and counts nobody by position
            assert set(mm["indices"]) < set(tm["indices"]) and mm["stated_limits_minutes"] is None
            assert len(mm["tied_indices"]) > mm["size"] - len(mm["indices"]) > 0
            assert all(p["tied_for_majority"] for p in mine_p if p["index"] in mm["tied_indices"])
            assert not any(p["in_majority"] for p in mine_p if p["index"] in mm["tied_indices"])


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


# --------------------------------------------------------------------------- differing stated times: H28(j) as approved
T = {5: "5 minutes", 15: "15 minutes", 30: "30 minutes", 45: "45 minutes", 60: "60+ minutes"}


def G(*pairs, modes=(PT,)):
    """(stated time in minutes or None, journey minutes or None for no destination) for each member, in order."""
    return [M(i, T.get(t), modes, None if v is None else {m: v for m in modes}) for i, (t, v) in enumerate(pairs)]


def test_differing_times_compare_the_closest_majority_with_twice_the_average_of_its_own_stated_times():
    # five members; the three closest by journey are 0, 1 and 2: average journey 30, their stated times 15, 30, 45 average 30,
    # doubled 60. The two far members' stated times (5 and 60+) play no part.
    r = mt.assess_group(G((15, 20), (30, 30), (45, 40), (5, 200), (60, 300)))
    m = r.facts["majority"]
    assert r.status == mt.PASS and m["indices"] == [0, 1, 2] and m["membership_determined"] and m["figure_determined"]
    assert (m["total_minutes"], m["average_display"], m["stated_limits_minutes"], m["limit_total_minutes"]) == ("90", "30.0", [15, 30, 45], 90)
    assert (m["limit_average_display"], m["ceiling_display"], m["ceiling_range_display"]) == ("30.0", "60.0", None)
    assert r.facts["within_ceiling"] is True and r.facts["meets_stated_limit"] is True
    assert r.facts["same_stated_limit"] is False and r.facts["stated_limit_minutes"] is None and r.facts["ceiling_minutes"] is None
    assert "limit_note" not in r.facts and "tie_note" not in r.facts and r.facts["majority_tie"] is None
    assert [p["in_majority"] for p in r.facts["people"]] == [True, True, True, False, False]
    # the same journeys with the two far members' stated times swapped round: nothing changes
    again = mt.assess_group(G((15, 20), (30, 30), (45, 40), (60, 200), (5, 300)))
    assert again.status == r.status and again.facts["majority"] == r.facts["majority"]


def test_the_majority_is_chosen_by_journey_time_and_never_by_the_size_of_the_stated_times():
    """The members with the most generous stated times are far away. Choosing by stated time would pass; the rule fails it."""
    r = mt.assess_group(G((5, 10), (5, 11), (5, 12), (60, 100), (60, 101)))
    m = r.facts["majority"]
    assert m["indices"] == [0, 1, 2] and m["stated_limits_minutes"] == [5, 5, 5] and m["ceiling_display"] == "10.0"
    assert r.status == mt.EXCEEDS_CEILING and r.facts["within_ceiling"] is False        # 33 in total against 2 × 15 = 30
    # and the same members in both averages: the journeys of 0, 1, 2 are never set against the stated times of 3 and 4
    assert m["total_minutes"] == "33" and m["limit_total_minutes"] == 15


@pytest.mark.parametrize("journeys, status", [((30, 40, 50), mt.PASS),                 # 30 + 40 = 70 against 2 × (15 + 30) = 90
                                              ((40, 50, 500), mt.PASS),                # exactly 90 against 90: the boundary passes
                                              ((40, Decimal("50.01"), 500), mt.EXCEEDS_CEILING),
                                              ((Fraction(2700, 60), 45, 500), mt.PASS),
                                              ((Fraction(2701, 60), 45, 500), mt.EXCEEDS_CEILING)])   # one second over
def test_the_exact_boundary_passes_and_anything_over_it_does_not(journeys, status):
    r = mt.assess_group(G(*zip((15, 30, 60), journeys)))
    assert r.status == status and sorted(r.facts["majority"]["indices"]) == [0, 1] and r.facts["majority"]["limit_total_minutes"] == 45
    assert r.facts["majority"]["ceiling_display"] == "45.0" and r.facts["within_ceiling"] is (status == mt.PASS)


def test_the_comparison_is_made_on_exact_totals_and_not_on_rounded_averages():
    # stated 5, 5, 15 → total 25, doubled 50; journeys total 50.01: the averages both show as 16.7 but it is over
    r = mt.assess_group(G((5, 16), (5, 17), (15, Decimal("17.01")), (30, 900), (30, 901)))
    m = r.facts["majority"]
    assert r.status == mt.EXCEEDS_CEILING and (m["average_display"], m["ceiling_display"], m["limit_average_display"]) == ("16.7", "16.7", "8.3")
    assert mt.assess_group(G((5, 16), (5, 17), (15, 17), (30, 900), (30, 901))).status == mt.PASS


@pytest.mark.parametrize("pairs", [((None, 10), (30, 11), (30, 12), (30, 300), (30, 400)),     # missing inside the closest majority
                                   ((30, 10), (30, 11), (30, 12), (30, 300), (None, 400)),     # missing outside it
                                   ((30, 10), (30, 11), (None, None)),                         # missing for someone with no destination
                                   ((None, 1), (None, 1), (None, 1)),
                                   ((5, 500), (60, 500), (None, 500))])
def test_a_missing_stated_time_anywhere_is_needs_confirmation_and_nothing_is_assumed(pairs):
    r = mt.assess_group(G(*pairs))
    f = r.facts
    assert r.status == mt.NEEDS_CONFIRMATION and r.reasons == (mt.TRAVEL_LIMIT_MISSING,)   # however short or long the journeys are
    assert f["majority"] is None and f["within_ceiling"] is None and f["meets_stated_limit"] is None and f["ceiling_minutes"] is None
    assert f["limit_note"] == mt.LIMIT_MISSING_NOTE and f["limit_missing"] == [i for i, (t, _) in enumerate(pairs) if t is None]
    assert not any(p["in_majority"] or p["warning"] or p.get("tied_for_majority") for p in f["people"])
    # each member is still reported against their OWN stated time, and only that
    for p, (t, v) in zip(f["people"], pairs):
        assert p["stated_limit_minutes"] == t
        if v is not None:
            assert p["fastest_minutes"] == str(v)
            assert p["own_limit"] == (mt.NO_STATED_LIMIT if t is None else mt.WITHIN_OWN_LIMIT if v <= t else mt.OVER_OWN_LIMIT)
    assert not hasattr(mt, "TRAVEL_LIMITS_DIFFER") and not hasattr(mt, "UNRULED_NOTE") and "unruled" not in f


def test_one_member_far_away_or_with_a_short_stated_time_does_not_veto_the_group():
    # the outlier is 400 minutes away and states 5 minutes: outside the closest majority, so neither figure counts
    r = mt.assess_group(G((30, 20), (45, 30), (5, 400)))
    assert r.status == mt.PASS and r.facts["majority"]["indices"] == [0, 1] and r.facts["majority"]["stated_limits_minutes"] == [30, 45]
    far = r.facts["people"][2]
    assert far["in_majority"] is False and far["warning"] == ht.LONG_JOURNEY_WARNING and far["own_limit"] == mt.OVER_OWN_LIMIT
    assert ht.LONG_JOURNEY_WARNING in r.reasons and r.facts["over_own_limit"] == [2]
    # a member inside the majority who is over double their OWN stated time is warned about as well; it excludes nothing
    r = mt.assess_group(G((5, 25), (60, 5), (60, 400)))
    assert r.status == mt.PASS and sorted(r.facts["majority"]["indices"]) == [0, 1] and r.facts["majority"]["stated_limits_minutes"] == [5, 60]
    assert [p["warning"] for p in r.facts["people"]] == [ht.LONG_JOURNEY_WARNING, None, ht.LONG_JOURNEY_WARNING]


@pytest.mark.parametrize("n, k", [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (6, 4), (7, 4)])
def test_the_majority_is_a_strict_majority_of_those_with_a_known_destination(n, k):
    times = [5, 15, 30, 45, 60, 15, 30]
    pairs = [(times[i], 10 + i) for i in range(n)]
    r = mt.assess_group(G(*pairs))
    m = r.facts["majority"]
    assert m["size"] == k == r.facts["majority_size"] and m["indices"] == list(range(k)) and 2 * k > n >= 2 * (k - 1)
    assert m["stated_limits_minutes"] == times[:k] and m["limit_total_minutes"] == sum(times[:k])
    assert m["total_minutes"] == str(sum(10 + i for i in range(k)))
    # someone with no destination does not enlarge the majority, but their stated time must still be there
    if n < 7:
        r2 = mt.assess_group(G(*pairs, (30, None)))
        drop = lambda d: {x: y for x, y in d.items() if x != "comparison"}             # its wording differs when one time is shared
        assert drop(r2.facts["majority"]) == drop(m) and r2.status == r.status
        assert r2.facts["known_destinations"] == n and r2.facts["no_destination"] == [n]
        assert mt.assess_group(G(*pairs, (None, None))).status == mt.NEEDS_CONFIRMATION


def test_the_same_stated_time_for_everyone_is_exactly_the_calculation_as_it_was():
    for t in (5, 15, 30, 45, 60):
        for journeys in ((t, t, 9 * t), (2 * t, 2 * t, 2 * t), (2 * t, 2 * t + 1, 2 * t + 2), (0, 0, 0)):
            r = mt.assess_group(G(*[(t, v) for v in journeys]))
            two = sum(sorted(journeys)[:2])
            assert r.status == (mt.PASS if two <= 4 * t else mt.EXCEEDS_CEILING)
            assert r.facts["same_stated_limit"] is True and r.facts["ceiling_minutes"] == 2 * t and r.facts["stated_limit_minutes"] == t
            assert r.facts["meets_stated_limit"] is (two <= 2 * t)


# ----------------------------------------------- level journey times at the edge of the majority (engineering safeguard)
def test_a_tie_that_would_change_the_result_is_needs_confirmation_and_nobody_is_picked():
    # 0 is in. 1 and 2 are both 50 minutes away and one place is left. With 1 (stated 5): 60 against 2 × 20 = 40, over.
    # With 2 (stated 60+): 60 against 2 × 75 = 150, within. The result depends on who is counted, so it is not given.
    r = mt.assess_group(G((15, 10), (5, 50), (60, 50)))
    f = r.facts
    assert r.status == mt.NEEDS_CONFIRMATION and r.reasons == (mt.TRAVEL_MAJORITY_TIED,) and f["tie_note"] == mt.MAJORITY_TIED_NOTE
    assert f["majority"] is None and f["within_ceiling"] is None and f["meets_stated_limit"] is None
    assert f["majority_tie"] == {"journey_minutes": "50", "indices": [1, 2], "places": 1}
    assert not any(p["in_majority"] or p["warning"] or p.get("tied_for_majority") for p in f["people"])
    # not decided by position: the same answer whichever of the two comes first
    swapped = mt.assess_group(G((15, 10), (60, 50), (5, 50)))
    assert swapped.status == mt.NEEDS_CONFIRMATION and swapped.reasons == r.reasons and swapped.facts["majority_tie"]["indices"] == [1, 2]
    # and not decided by stated time either: neither the smaller nor the larger one is quietly used
    r = mt.assess_group(G((15, 10), (5, 50), (60, 50), (30, 50)))                      # three level for two places
    assert r.status == mt.NEEDS_CONFIRMATION and r.facts["majority_tie"] == {"journey_minutes": "50", "indices": [1, 2, 3], "places": 2}
    # level journeys that fill every remaining place are not a tie at all
    r = mt.assess_group(G((15, 10), (5, 50), (60, 50), (30, 900)))
    assert r.status == mt.PASS and r.facts["majority_tie"] is None and r.facts["majority"]["membership_determined"] is True


def test_a_tie_that_cannot_change_the_result_reports_only_what_every_choice_supports():
    # within, whoever is counted: 10 + 20 = 30 against 2 × (30 + 15) = 90 or 2 × (30 + 45) = 150
    r = mt.assess_group(G((30, 10), (15, 20), (45, 20)))
    m = r.facts["majority"]
    assert r.status == mt.PASS and r.facts["within_ceiling"] is True
    assert m["indices"] == [0] and m["tied_indices"] == [1, 2] and m["size"] == 2
    assert (m["membership_determined"], m["figure_determined"], m["stated_limits_minutes"], m["limit_total_minutes"]) == (False, False, None, None)
    assert (m["limit_average_display"], m["ceiling_display"], m["ceiling_range_display"]) == (None, None, ["45.0", "75.0"])
    assert "depending on which tied members are counted" in m["comparison"]
    assert r.facts["majority_tie"] == {"journey_minutes": "20", "indices": [1, 2], "places": 1}
    people = r.facts["people"]
    assert [p["in_majority"] for p in people] == [True, False, False]                  # the tied ones are not shown as chosen
    assert [bool(p.get("tied_for_majority")) for p in people] == [False, True, True]
    # over, whoever is counted
    r = mt.assess_group(G((5, 100), (5, 200), (15, 200)))
    assert r.status == mt.EXCEEDS_CEILING and r.facts["majority"]["ceiling_range_display"] == ["10.0", "20.0"]
    # against the undoubled stated times the tied members WOULD change the answer: that one figure is left unknown
    r = mt.assess_group(G((30, 10), (5, 40), (60, 40)))        # 50 against 35 or 90 undoubled; 70 or 180 doubled
    assert r.status == mt.PASS and r.facts["meets_stated_limit"] is None
    # tied members who state the same time: the figure is one number, the membership still is not
    r = mt.assess_group(G((30, 10), (15, 20), (15, 20)))
    m = r.facts["majority"]
    assert (m["membership_determined"], m["figure_determined"], m["limit_total_minutes"], m["ceiling_display"]) == (False, True, 45, "45.0")
    assert m["indices"] == [0] and m["tied_indices"] == [1, 2] and m["stated_limits_minutes"] is None


def test_every_way_of_filling_the_tied_places_is_covered_by_the_two_extremes():
    """Brute force over small groups: PASS / over only when every k-subset among the closest agrees; otherwise needs confirmation."""
    seen = set()
    for n, times, minutes in ((2, (5, 30, 60), (10, 40, 70)), (3, (5, 30, 60), (10, 40, 70)), (4, (5, 30, 60), (10, 40, 70)), (5, (5, 60), (10, 40))):
        k = n // 2 + 1
        for journeys in itertools.product(minutes, repeat=n):
            for stated in itertools.product(times, repeat=n):
                edge = sorted(journeys)[k - 1]
                sure = [i for i in range(n) if journeys[i] < edge]
                tied = [i for i in range(n) if journeys[i] == edge]
                outcomes = set()
                for pick in itertools.combinations(tied, k - len(sure)):
                    chosen = sure + list(pick)
                    outcomes.add(sum(journeys[i] for i in chosen) <= 2 * sum(stated[i] for i in chosen))
                r = mt.assess_group(G(*zip(stated, journeys)))
                want = mt.NEEDS_CONFIRMATION if len(outcomes) == 2 else mt.PASS if outcomes == {True} else mt.EXCEEDS_CEILING
                assert r.status == want, (journeys, stated)
                seen.add((want, len(tied) > k - len(sure)))
                # the same whatever order the members are given in
                back = mt.assess_group([M(n - 1 - i, T[stated[n - 1 - i]], [PT], {PT: journeys[n - 1 - i]}) for i in range(n)])
                assert back.status == r.status and back.facts["majority"] == r.facts["majority"]
    assert seen == {(s, t) for s in (mt.PASS, mt.EXCEEDS_CEILING, mt.NEEDS_CONFIRMATION) for t in (True, False)} - {(mt.NEEDS_CONFIRMATION, False)}


def test_no_default_and_no_positional_or_stated_time_choice_is_in_the_code():
    """A missing answer is never read as 30 minutes; the stated times are only ever summed over the chosen members."""
    src = inspect.getsource(mt._assess) + inspect.getsource(mt._own_limit) + inspect.getsource(mt.Member)
    for banned in ("max(", "mean(", "statistics", ", 30)", "or 30", "get(self.stated_time, "):
        assert banned not in src, banned
    assert mt.Member(0, None, [W], {W: 1}).limit is None
    assert src.count("min(") == 1                                                      # only the fastest accepted journey of one person
    assert "key=lambda m: (m.best, m.index)" in src and "m.limit" not in src.split("ordered = sorted(")[1].split("\n")[0]


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
