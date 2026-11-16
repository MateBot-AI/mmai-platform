"""H1: pure HomeBot candidate evaluation (server/homebot_match.py). Synthetic apartments and parties only; no database, clock,
provider or AI. Includes an independent brute-force oracle for the complete date/tariff/budget search."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from server import budget, homebot_bedding as bedding, homebot_match as hm, homebot_travel as travel, lease_dates, rules

TODAY = date(2027, 2, 20)
KING = "1 king or 2 singles"


def apt(i, type_code="2B1B", suburb="Melbourne CBD", rent12=1000, rent6=1300, avail="2027-02-25", building="10 Test Street", **kw):
    n = rules.APT_TYPES[type_code]["bedrooms"]
    row = {"id": i, "address": f"Unit {i} / {building}", "suburb": suburb, "type_code": type_code, "bathrooms": rules.APT_TYPES[type_code]["bathrooms"],
           "rent_12m": rent12, "rent_6m": rent6, "available_from": avail, "min_lease_months": 2,
           "bedroom1": KING, "bedroom2": KING if n >= 2 else None, "bedroom3": KING if n >= 3 else None}
    row.update(kw)
    return row


def enquiry(**kw):
    e = {"budget_total_weekly": 1000, "move_in": "2027-03-04", "move_in_flex": "Within a week either way", "move_out": "2027-07-10",
         "move_out_flex": "Exact date", "bedrooms": ["2 bedrooms"], "budget_types": ["2B1B"], "bathrooms": ["1 bathroom is fine"],
         "area": "Melbourne CBD", "transport": ["Tram"], "max_travel": "30 minutes", "extras": [], "extras_top": [], "apartment_notes": None,
         "bunk_condition": None, "headcount_target": None}
    e.update(kw)
    return e


def people(*sleeps, doing="Working"):
    return [{"position": i, "sleep": s, "doing": doing, "place": "work" if doing in hm.WORK else None} for i, s in enumerate(sleeps, start=1)]


TWO_OWN = people("Own bedroom", "Own bedroom")


def req(e=None, ppl=None, **kw):
    return hm.requirements_from_enquiry(e or enquiry(), ppl or TWO_OWN, **kw)


# ---------------------------------------------------------------------------------------------- independent oracle
def oracle(r: hm.Requirements, a: dict, today: date) -> set:
    """Brute force with the shared helpers only: every (start, end) in the accepted windows that passes assess_dates, with the
    tariff of that pair and the A16 result. Independent of search_offers' loop structure and pruning."""
    out = set()
    in_lo, in_hi = r.move_in_window
    out_lo, out_hi = r.move_out_window
    avail = date.fromisoformat(a["available_from"])
    for k in range((in_hi - in_lo).days + 1):
        s = in_lo + timedelta(days=k)
        for j in range((out_hi - out_lo).days + 1):
            en = out_lo + timedelta(days=j)
            if en <= s or s < avail:
                continue
            da = lease_dates.assess_dates(today, avail, s, en, a["min_lease_months"])
            if not da.passed:
                continue
            rent = a["rent_6m"] if da.rate_kind == "6m" else a["rent_12m"]
            res = budget.check_total_budget(rent, a["type_code"], r.head_count, r.whole_apartment_max)
            out.add((s, en, da.rate_kind, res.reason_code))
    return out


def test_root_case_later_start_escapes_the_jan_feb_july_tariff_and_is_found():
    """Root's acceptance case: today 20 Feb 2027, available 25 Feb, accepted start 25 Feb–11 Mar (4 Mar ± 7), exit fixed 10 Jul,
    minimum 2 months, rent6 1300 / rent12 1000, maximum 1000. The earliest start (Feb) prices at 1300 and fails A16; a March
    start prices at 1000 and passes. The apartment must not be excluded."""
    r = req()
    a = apt(1)
    ds = hm.search_offers(r, a, TODAY)
    found = {(o.start, o.end, o.rate_kind, o.budget_code) for o in ds.offers}
    assert found == oracle(r, a, TODAY)
    feb = [o for o in ds.offers if o.start.month == 2]
    assert feb and all(o.rate_kind == "6m" and o.budget_code == budget.OVER_ALLOWANCE for o in feb)
    assert (date(2027, 3, 1), date(2027, 7, 10), "12m", budget.WITHIN_MAX) in found
    c = hm.evaluate(r, a, TODAY)
    assert c.klass != hm.EXCLUDED and c.offer.rate_kind == "12m" and c.offer.total_weekly_rent == Decimal("1000")
    assert c.offer.start == date(2027, 3, 3)   # closest accepted start to 4 March that passes the vacancy cap (6 empty days of 129)


def test_end_date_only_tariff_boundary_counterpart():
    """Start fixed in February; the exit range spans the July window (6-month tariff, 1300, over budget) and the October window
    (12-month tariff, 1000, within). Only the exit changes the tariff; the October exits must be found."""
    e = enquiry(move_in="2027-02-26", move_in_flex="Exact date", move_out="2027-07-05", move_out_flex="Flexible by a month or more",
                move_out_earliest="2027-07-01", move_out_latest="2027-10-10")
    r = req(e)
    a = apt(1)
    ds = hm.search_offers(r, a, TODAY)
    found = {(o.start, o.end, o.rate_kind, o.budget_code) for o in ds.offers}
    assert found == oracle(r, a, TODAY)
    assert all(o.budget_code == budget.OVER_ALLOWANCE for o in ds.offers if o.end.month == 7)
    assert any(o.end.month == 10 and o.rate_kind == "12m" and o.within_budget for o in ds.offers)
    c = hm.evaluate(r, a, TODAY)
    assert c.klass != hm.EXCLUDED and c.offer.end.month == 10 and c.offer.rate_kind == "12m"


@pytest.mark.parametrize("flex_in,flex_out,mi,mo,avail,rent12,rent6,maximum", [
    ("Within two weeks either way", "Within a week either way", "2027-01-20", "2027-07-15", "2027-01-05", 900, 1150, 1000),
    ("Within a week either way", "Within two weeks either way", "2027-06-01", "2027-10-20", "2027-05-20", 1200, 1000, 1050),
    ("Exact date", "Within two weeks either way", "2027-12-01", "2028-02-10", "2027-12-01", 1000, 1000, 950),
    ("Flexible by a month or more", "Flexible by a month or more", "2027-03-15", "2027-10-15", "2027-02-01", 1000, 1300, 1000),
])
def test_search_matches_the_brute_force_oracle(flex_in, flex_out, mi, mo, avail, rent12, rent6, maximum):
    mi_d, mo_d = date.fromisoformat(mi), date.fromisoformat(mo)
    e = enquiry(move_in=mi, move_in_flex=flex_in, move_out=mo, move_out_flex=flex_out, budget_total_weekly=maximum,
                move_in_earliest=(mi_d - timedelta(days=40)).isoformat() if flex_in == hm.WIDE_FLEX else None,
                move_in_latest=(mi_d + timedelta(days=40)).isoformat() if flex_in == hm.WIDE_FLEX else None,
                move_out_earliest=(mo_d - timedelta(days=100)).isoformat() if flex_out == hm.WIDE_FLEX else None,
                move_out_latest=(mo_d + timedelta(days=60)).isoformat() if flex_out == hm.WIDE_FLEX else None)
    r = req(e)
    a = apt(1, rent12=rent12, rent6=rent6, avail=avail)
    today = date.fromisoformat(avail) - timedelta(days=10)
    ds = hm.search_offers(r, a, today)
    assert {(o.start, o.end, o.rate_kind, o.budget_code) for o in ds.offers} == oracle(r, a, today)
    assert not ds.unresolved
    for o in ds.offers:   # every offer's money is the shared helper's exact result for that exact pair
        res = budget.check_total_budget(a["rent_6m"] if o.rate_kind == "6m" else a["rent_12m"], a["type_code"], r.head_count, r.whole_apartment_max)
        assert o.total_weekly_rent == res.total_weekly_rent and o.budget_code == res.reason_code


def test_offer_preference_is_closest_dates_then_cheaper_and_cheapest_is_reported_separately():
    e = enquiry(move_in="2027-02-26", move_in_flex="Within two weeks either way", move_out="2027-07-05", move_out_flex="Within a week either way",
                budget_total_weekly=1200)
    r = req(e)
    c = hm.evaluate(r, apt(1, rent12=1000, rent6=1150, avail="2027-02-24"), TODAY)
    assert c.offer.start == date(2027, 2, 26) and c.offer.rate_kind == "6m"      # the party's own dates are affordable: offered
    assert c.cheapest.rate_kind == "12m" and c.cheapest.total_weekly_rent < c.offer.total_weekly_rent
    assert hm.CHEAPER_OFFER_AVAILABLE in c.flags


def test_accepted_start_beyond_400_days_is_searched_completely():
    """Root's boundary: an accepted start range wider than 400 days is enumerated in full (no silent cut); the only feasible
    offers lie at the far end and are found, and they agree with the brute-force oracle."""
    e = enquiry(move_in="2028-01-15", move_in_flex=hm.WIDE_FLEX, move_in_earliest="2027-02-25", move_in_latest="2028-06-01",
                move_out="2028-10-10", move_out_flex="Exact date")
    r = req(e)
    a = apt(1, avail="2028-05-20")
    today = TODAY
    ds = hm.search_offers(r, a, today)
    assert not ds.unresolved and ds.offers
    assert {(o.start, o.end, o.rate_kind, o.budget_code) for o in ds.offers} == oracle(r, a, today)
    assert min(o.start for o in ds.offers) >= date(2028, 5, 20) and (date(2028, 5, 20) - date(2027, 2, 25)).days > 400
    assert hm.evaluate(r, a, today).klass != hm.EXCLUDED


def test_ranges_beyond_the_pair_bound_are_explicitly_unresolved_never_excluded_or_priced(monkeypatch):
    monkeypatch.setattr(hm, "MAX_PAIRS", 50)
    e = enquiry(move_in="2027-03-04", move_in_flex="Within two weeks either way", move_out="2027-07-10", move_out_flex="Within a week either way")
    r = req(e)
    c = hm.evaluate(r, apt(1), TODAY, estimates_all(10))
    assert c.date_search_unresolved and c.klass == hm.NEEDS_CONFIRMATION and hm.DATE_SEARCH_UNRESOLVED in c.confirmation_reasons
    assert c.offer is None and c.cheapest is None and hm.NO_FEASIBLE_DATES not in c.exclusion_reasons and hm.OVER_BUDGET not in c.exclusion_reasons
    sl = hm.build_shortlist(r, [apt(1), apt(2, building="20 Other Street")], TODAY, estimates_all(10))
    assert sl.listed == [] and [c.apartment_id for c in sl.unresolved] == [1, 2] and sl.summary()["unresolved"] == [1, 2]
    # an unresolved non-bunk option cannot serve as proof that a bunk option is cheaper
    monkeypatch.setattr(hm, "MAX_PAIRS", 2_000_000)
    rb = req(enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1200), bunk_party())
    bunk_apt = apt(1, bedroom1="1 bunk bed", bedroom2=KING, rent12=850, rent6=850)
    wide_plain = apt(2, bedroom1="2 singles", bedroom2=KING, rent12=1000, rent6=1000, building="20 Other Street")
    cands = [hm.evaluate(rb, bunk_apt, TODAY)]
    monkeypatch.setattr(hm, "MAX_PAIRS", 5)
    cands.append(hm.evaluate(rb, wide_plain, TODAY))
    hm.apply_bunk_condition(rb, cands)
    assert cands[1].date_search_unresolved and cands[0].klass == hm.NEEDS_CONFIRMATION
    assert cands[0].bunk_comparison["result"] == "no_comparator" and cands[0].bunk_comparison["unresolved_comparators"] == [2]


def test_leased_stock_is_excluded_even_if_availability_is_recorded():
    c = hm.evaluate(req(), apt(1, leased_at="2027-02-18T10:00:00"), TODAY, estimates_all(10))
    assert c.klass == hm.EXCLUDED and hm.LEASED in c.exclusion_reasons and c.offer is None


# ---------------------------------------------------------------------------------------------------- firm requirements
def test_known_destination_without_measured_journey_is_never_suitable():
    c = hm.evaluate(req(), apt(1), TODAY)
    assert c.klass == hm.NEEDS_CONFIRMATION and c.confirmation_reasons == [hm.TRAVEL_NEEDS_CONFIRMATION]
    assert c.travel["status"] == travel.NEEDS_CONFIRMATION


def test_party_with_no_destinations_has_nothing_to_confirm_and_is_suitable():
    c = hm.evaluate(req(ppl=people("Own bedroom", "Own bedroom", doing="Not yet decided")), apt(1), TODAY)
    assert c.klass == hm.SUITABLE and hm.NO_DESTINATIONS in c.flags and c.travel["status"] == travel.NO_DESTINATIONS


def estimates_all(minutes):
    return lambda aid, pos, kind: {"Tram": minutes}


def test_measured_journeys_pass_or_exclude_by_the_doubled_ceiling():
    ok = hm.evaluate(req(), apt(1), TODAY, estimates_all(25))
    assert ok.klass == hm.SUITABLE and ok.travel["status"] == travel.PASS
    far = hm.evaluate(req(), apt(1), TODAY, estimates_all(70))
    assert far.klass == hm.EXCLUDED and hm.TRAVEL_EXCEEDS_CEILING in far.exclusion_reasons


def test_work_and_study_person_contributes_once_via_the_primary_destination():
    ppl = [{"position": 1, "sleep": "Own bedroom", "doing": "Studying and working", "institution": "Other", "place": "work", "primary_destination": "Work"},
           {"position": 2, "sleep": "Own bedroom", "doing": "Not yet decided"}]
    est = lambda aid, pos, kind: {"Tram": 20 if kind == "primary" else 200}
    c = hm.evaluate(req(ppl=ppl), apt(1), TODAY, est)
    assert c.klass == hm.SUITABLE and c.travel["facts"]["known_destinations"] == 1
    assert c.travel["facts"]["secondary_display"]   # the other journey is shown, not counted


def test_bedroom_count_not_selected_is_excluded_even_when_cheaper():
    c = hm.evaluate(req(), apt(1, type_code="1BR", rent12=500, rent6=500), TODAY)
    assert c.klass == hm.EXCLUDED and hm.BEDROOM_COUNT_NOT_SELECTED in c.exclusion_reasons


def test_layout_within_selected_counts_may_differ_from_the_priced_type():
    c = hm.evaluate(req(), apt(1, type_code="2B2B", bathrooms=2), TODAY, estimates_all(10))
    assert c.klass == hm.SUITABLE and not c.layout_priced and c.two_bathrooms


def test_area_and_bathroom_alternatives_never_both():
    r = req(enquiry(bathrooms=["Must have 2 bathrooms"], area="South Yarra"))
    area_only = hm.evaluate(r, apt(1, type_code="2B2B", bathrooms=2, suburb="Melbourne CBD"), TODAY, estimates_all(10))
    bath_only = hm.evaluate(r, apt(2, type_code="2B1B", suburb="South Yarra"), TODAY, estimates_all(10))
    both = hm.evaluate(r, apt(3, type_code="2B1B", suburb="Melbourne CBD"), TODAY, estimates_all(10))
    assert area_only.klass == hm.ALTERNATIVE_AREA and area_only.exclusion_reasons == [hm.AREA_NOT_SELECTED]
    assert bath_only.klass == hm.ALTERNATIVE_BATHROOM and bath_only.exclusion_reasons == [hm.BATHROOMS_BELOW_REQUIRED]
    assert both.klass == hm.EXCLUDED and set(both.exclusion_reasons) == {hm.AREA_NOT_SELECTED, hm.BATHROOMS_BELOW_REQUIRED}


def test_east_melbourne_is_outside_both_selectable_areas():
    for area in ("Melbourne CBD", "South Yarra", "Either"):
        c = hm.evaluate(req(enquiry(area=area)), apt(1, suburb="East Melbourne"), TODAY, estimates_all(10))
        assert c.klass == hm.ALTERNATIVE_AREA, area
    assert hm.evaluate(req(enquiry(area="Either")), apt(2, suburb="South Yarra"), TODAY, estimates_all(10)).klass == hm.SUITABLE


def test_alternative_that_fails_another_firm_rule_is_excluded_not_an_alternative():
    r = req(enquiry(area="South Yarra", budget_total_weekly=500))
    c = hm.evaluate(r, apt(1, suburb="Melbourne CBD"), TODAY)
    assert c.klass == hm.EXCLUDED and hm.OVER_BUDGET in c.exclusion_reasons and hm.AREA_NOT_SELECTED in c.exclusion_reasons


def test_own_bedroom_infeasible_is_excluded_with_no_exception():
    c = hm.evaluate(req(enquiry(bedrooms=["1 bedroom"]), people("Own bedroom", "Own bedroom")), apt(1, type_code="1BR"), TODAY)
    assert c.klass == hm.EXCLUDED and hm.BEDDING_NOT_FEASIBLE in c.exclusion_reasons


def test_spare_bedroom_is_permitted_and_flagged():
    c = hm.evaluate(req(enquiry(bedrooms=["3 bedrooms"]), TWO_OWN), apt(1, type_code="3B", bathrooms=2), TODAY, estimates_all(10))
    assert c.klass == hm.SUITABLE and hm.SPARE_BEDROOM in c.flags


def test_party_larger_than_the_type_allows_is_excluded():
    r = req(enquiry(bedrooms=["1 bedroom"]), people("Twin room (two single beds)", "Twin room (two single beds)", "Own bedroom", "Own bedroom"))
    c = hm.evaluate(r, apt(1, type_code="1BR"), TODAY)
    assert c.klass == hm.EXCLUDED and hm.PARTY_TOO_LARGE in c.exclusion_reasons


def test_over_allowance_excluded_within_allowance_flagged():
    over = hm.evaluate(req(enquiry(budget_total_weekly=900)), apt(1), TODAY, estimates_all(10))          # 1000 > 990
    assert over.klass == hm.EXCLUDED and hm.OVER_BUDGET in over.exclusion_reasons
    within = hm.evaluate(req(enquiry(budget_total_weekly=950)), apt(1), TODAY, estimates_all(10))        # 1000 <= 1045
    assert within.klass == hm.SUITABLE and hm.WITHIN_ALLOWANCE in within.flags and within.offer.budget_code == budget.WITHIN_ALLOWANCE


def test_extra_bed_charge_is_inside_the_budget_total():
    """Five planned for a 2B (standard capacity four, final-size scenario so bedding is capacity-only): the A5 extra-bed charge is
    part of the whole-apartment total that A16 checks, exactly as budget.check_total_budget computes it."""
    e = enquiry(headcount_target=5, bedrooms=["2 bedrooms"])
    r = hm.requirements_from_enquiry(e, TWO_OWN, scenario=hm.SCENARIO_FINAL_SIZE, planning_budget=1100)
    a = apt(1, rent12=1000, rent6=1300, bedroom1="1 king or 1 king+1 single")   # three places physically possible in room 1
    c = hm.evaluate(r, a, TODAY)
    ref = budget.check_total_budget(1000, "2B1B", 5, 1100)
    assert ref.total_weekly_rent > Decimal("1000")
    assert c.klass != hm.EXCLUDED and c.offer.total_weekly_rent == ref.total_weekly_rent and c.offer.budget_code == ref.reason_code
    tight = hm.evaluate(hm.requirements_from_enquiry(e, TWO_OWN, scenario=hm.SCENARIO_FINAL_SIZE, planning_budget=960), a, TODAY)
    assert tight.klass == hm.EXCLUDED and hm.OVER_BUDGET in tight.exclusion_reasons    # 1070 > 960 × 1.1 = 1056


def test_blackout_exit_and_vacancy_cap_exclude():
    black = hm.evaluate(req(enquiry(move_out="2027-08-15", move_out_flex="Exact date")), apt(1), TODAY)
    assert black.klass == hm.EXCLUDED and hm.NO_FEASIBLE_DATES in black.exclusion_reasons
    vac = hm.evaluate(req(enquiry(move_in="2027-04-20", move_in_flex="Exact date", move_out="2027-07-10", move_out_flex="Exact date")), apt(1), TODAY)
    assert vac.klass == hm.EXCLUDED and hm.NO_FEASIBLE_DATES in vac.exclusion_reasons   # 54 empty days vs 81 // 20 = 4 permitted


def test_no_availability_date_and_unreadable_bedding_are_held():
    assert hm.NOT_AVAILABLE in hm.evaluate(req(), apt(1, avail=None), TODAY).exclusion_reasons
    assert hm.INVENTORY_UNREADABLE in hm.evaluate(req(), apt(1, bedroom1="a sofa"), TODAY).exclusion_reasons


def test_essential_note_flags_every_candidate_for_review_and_nothing_is_suitable():
    r = req(enquiry(apartment_notes="Step-free access is essential for one of us."))
    assert r.notes_review
    sl = hm.build_shortlist(r, [apt(1), apt(2, building="20 Other Street")], TODAY, estimates_all(10))
    assert sl.listed and all(c.klass == hm.NEEDS_CONFIRMATION and hm.ESSENTIAL_NOTE_REVIEW in c.confirmation_reasons for c in sl.listed)
    assert sl.summary()["nothing_suitable_yet"] and sl.notes_review
    assert not req(enquiry(apartment_notes="A balcony would be lovely.")).notes_review


def test_rejected_apartment_and_building_are_excluded():
    r = req(rejected_apartment_ids=[1], rejected_buildings=["Unit 9 / 20 Other Street"])
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r, apt(1), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r, apt(2, building="20 Other Street"), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER not in hm.evaluate(r, apt(3), TODAY).exclusion_reasons
    # GP14 narrow re-offer allowance: one apartment in a rejected building may be re-offered; the building rejection stays
    r2 = req(rejected_buildings=["Unit 9 / 20 Other Street"], reoffer_allowed_ids=[2])
    assert hm.REJECTED_BY_CUSTOMER not in hm.evaluate(r2, apt(2, building="20 Other Street"), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r2, apt(5, building="20 Other Street"), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(req(rejected_apartment_ids=[2], reoffer_allowed_ids=[2]), apt(2), TODAY).exclusion_reasons  # allowance never overrides an apartment rejection
    assert r2.as_dict()["reoffer_allowed_ids"] == [2]


# ------------------------------------------------------------------------------------------------------ bunk condition
BUNK_ROOM = "2 singles or 1 bunk bed"


def bunk_party():
    return people("Bunk bed", "Bunk bed", "Own bedroom", doing="Not yet decided")


def test_bunk_only_if_cheaper_measured_not_cheaper_is_excluded():
    """Two bunk choosers + one own room: a 2B1B with a bunk room houses them (bunk used); a 2B1B with two singles houses the
    non-bunk variant at the same rent → the bunk option is NOT cheaper → excluded, with the comparison recorded."""
    r = req(enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1200), bunk_party())
    bunk_apt = apt(1, bedroom1="1 bunk bed", bedroom2=KING, rent12=1000, rent6=1000)
    plain = apt(2, bedroom1="2 singles", bedroom2=KING, rent12=1000, rent6=1000, building="20 Other Street")
    sl = hm.build_shortlist(r, [bunk_apt, plain], TODAY)
    by = {c.apartment_id: c for c in sl.candidates}
    assert by[1].uses_bunk and not by[1].nonbunk_feasible
    assert by[1].klass == hm.EXCLUDED and hm.BUNK_NOT_CHEAPER in by[1].exclusion_reasons
    assert by[1].bunk_comparison["result"] == "not_cheaper" and by[1].bunk_comparison["cheapest_non_bunk_apartment_id"] == 2
    assert by[2].klass != hm.EXCLUDED


def test_bunk_comparator_uses_the_cheapest_accepted_dates_not_the_closest():
    """Root's tariff-boundary case: the non-bunk option costs 1300 on the party's preferred February dates but 1000 on permitted
    March dates. A 1100 bunk option is NOT cheaper than the cheapest qualifying non-bunk offer, even though 1100 < 1300."""
    e = enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1400, move_in="2027-02-26", move_in_flex="Within a week either way",
                move_out="2027-07-10", move_out_flex="Exact date")
    r = req(e, bunk_party())
    bunk_apt = apt(1, bedroom1="1 bunk bed", bedroom2=KING, rent12=1100, rent6=1100, avail="2027-02-25")
    plain = apt(2, bedroom1="2 singles", bedroom2=KING, rent12=1000, rent6=1300, avail="2027-02-25", building="20 Other Street")
    sl = hm.build_shortlist(r, [bunk_apt, plain], TODAY)
    by = {c.apartment_id: c for c in sl.candidates}
    assert by[2].offer.total_weekly_rent == Decimal("1300") and by[2].cheapest.total_weekly_rent == Decimal("1000")
    assert by[1].klass == hm.EXCLUDED and hm.BUNK_NOT_CHEAPER in by[1].exclusion_reasons
    assert by[1].bunk_comparison["result"] == "not_cheaper" and by[1].bunk_comparison["cheapest_non_bunk_total_weekly_rent"] == "1000.00"
    assert by[1].bunk_comparison["cheapest_non_bunk_dates"][0] >= "2027-03-01"


def test_bunk_cheaper_only_at_other_accepted_dates_needs_confirmation():
    e = enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1400, move_in="2027-02-26", move_in_flex="Within a week either way",
                move_out="2027-07-10", move_out_flex="Exact date")
    r = req(e, bunk_party())
    bunk_apt = apt(1, bedroom1="1 bunk bed", bedroom2=KING, rent12=900, rent6=1250, avail="2027-02-25")     # 1250 in Feb, 900 in March
    plain = apt(2, bedroom1="2 singles", bedroom2=KING, rent12=1000, rent6=1000, avail="2027-02-25", building="20 Other Street")
    by = {c.apartment_id: c for c in hm.build_shortlist(r, [bunk_apt, plain], TODAY).candidates}
    assert by[1].klass == hm.NEEDS_CONFIRMATION and hm.BUNK_CHEAPER_OTHER_DATES in by[1].confirmation_reasons
    assert by[1].bunk_comparison["result"] == "cheaper_at_other_dates" and by[1].bunk_comparison["bunk_cheapest_total_weekly_rent"] == "900.00"


def test_bunk_only_if_cheaper_actually_cheaper_is_accepted_with_the_comparison():
    r = req(enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1200), bunk_party())
    bunk_apt = apt(1, bedroom1="1 bunk bed", bedroom2=KING, rent12=850, rent6=850)
    plain = apt(2, bedroom1="2 singles", bedroom2=KING, rent12=1000, rent6=1000, building="20 Other Street")
    sl = hm.build_shortlist(r, [bunk_apt, plain], TODAY)
    by = {c.apartment_id: c for c in sl.candidates}
    assert by[1].klass == hm.SUITABLE and by[1].bunk_comparison["result"] == "cheaper"
    assert by[1].bunk_comparison["cheapest_non_bunk_total_weekly_rent"] == "1000.00"


def test_bunk_only_if_cheaper_with_no_comparator_needs_confirmation_and_claims_no_saving():
    r = req(enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1200), bunk_party())
    sl = hm.build_shortlist(r, [apt(1, bedroom1="1 bunk bed", bedroom2=KING)], TODAY)
    c = sl.candidates[0]
    assert c.klass == hm.NEEDS_CONFIRMATION and hm.BUNK_COMPARISON_UNAVAILABLE in c.confirmation_reasons
    assert c.bunk_comparison == {"result": "no_comparator", "bunk_total_weekly_rent": "1000.00", "unresolved_comparators": []}


def test_bunk_any_price_needs_no_comparison_and_bunk_unused_arrangement_is_unaffected():
    r = req(enquiry(bunk_condition="Any price", budget_total_weekly=1200), bunk_party())
    c = hm.build_shortlist(r, [apt(1, bedroom1="1 bunk bed", bedroom2=KING)], TODAY).candidates[0]
    assert c.klass == hm.SUITABLE and c.bunk_comparison is None
    r2 = req(enquiry(bunk_condition="Only if cheaper", budget_total_weekly=1200), people("Twin room or bunk", "Twin room or bunk", "Own bedroom", doing="Not yet decided"))
    c2 = hm.build_shortlist(r2, [apt(1, bedroom1="2 singles", bedroom2=KING, rent12=1500, rent6=1500, bathrooms=1)], TODAY).candidates[0]
    assert c2.klass == hm.EXCLUDED and hm.OVER_BUDGET in c2.exclusion_reasons   # 1500 > 1320: budget decides, not the bunk rule
    c3 = hm.build_shortlist(r2, [apt(1, bedroom1="2 singles", bedroom2=KING)], TODAY).candidates[0]
    assert c3.klass == hm.SUITABLE and not c3.uses_bunk and c3.bunk_comparison is None


# ---------------------------------------------------------------------------------------------------------- ranking
def test_dominated_option_ranks_below_and_non_dominated_pair_is_a_flagged_trade_off():
    """Bill's example: option B is $200 cheaper with a much shorter commute; option A starts on the exact preferred day. Neither is
    hidden, neither is declared the winner by a weight: both carry TRADE_OFF. A third option worse than B on every point ranks last."""
    r = req(enquiry(budget_total_weekly=1300, move_in="2027-03-04", move_in_flex="Within a week either way"))
    exact = apt(1, rent12=1200, rent6=1200, avail="2027-03-04", building="10 A Street")
    cheaper = apt(2, rent12=1000, rent6=1000, avail="2027-03-04", building="20 B Street")
    worse = apt(3, rent12=1100, rent6=1100, avail="2027-03-04", building="30 C Street")
    est = lambda aid, pos, kind: {"Tram": {1: 50, 2: 15, 3: 55}[aid]}
    sl = hm.build_shortlist(r, [exact, cheaper, worse], TODAY, est)
    by = {c.apartment_id: c for c in sl.candidates}
    assert by[1].offer.start == date(2027, 3, 4) and by[2].offer.start == date(2027, 3, 4)   # same date fit here: B dominates A outright
    assert by[2].pareto_rank == 0 and by[1].pareto_rank >= 1 and by[3].pareto_rank >= 1
    assert [c.apartment_id for c in sl.listed][0] == 2
    # make A's date fit genuinely better (B can only start later) → neither dominates: trade-off flagged on both
    cheaper_late = dict(cheaper, available_from="2027-03-08")
    sl2 = hm.build_shortlist(r, [exact, cheaper_late], TODAY, est)
    by2 = {c.apartment_id: c for c in sl2.candidates}
    assert by2[1].pareto_rank == 0 and by2[2].pareto_rank == 0
    assert hm.TRADE_OFF in by2[1].flags and hm.TRADE_OFF in by2[2].flags
    assert {c.apartment_id for c in sl2.listed} == {1, 2}


def test_travel_is_not_an_initial_ranking_input():
    """GP6(d)/H20 (16 Nov 2026): the first order is made from the non-travel points only. A journey time, known or not,
    never beats, loses to, or breaks a tie between two apartments."""
    r = req()
    a = hm._dims(hm.evaluate(r, apt(1), TODAY))
    b = hm._dims(hm.evaluate(r, apt(2, rent12=900, rent6=900, building="20 B Street"), TODAY))
    assert "travel" not in a and "travel" not in b
    assert hm.dominates(b, a, False) and not hm.dominates(a, b, False)
    # same on every non-travel point, very different journeys: neither beats the other and the order does not move
    near, far = hm.evaluate(r, apt(1), TODAY, estimates_all(5)), hm.evaluate(r, apt(2, building="20 B Street"), TODAY, estimates_all(55))
    assert near.travel["status"] == far.travel["status"] == travel.PASS
    assert not hm.dominates(hm._dims(near), hm._dims(far), False) and not hm._material(hm._dims(near), hm._dims(far))
    assert [c.apartment_id for c in hm.rank([far, near], r)] == [1, 2] and near.pareto_rank == far.pareto_rank == 0
    # the whole shortlist comes out in the same order whichever apartment has the shorter journey
    apts = [apt(1, rent12=980, rent6=980), apt(2, rent12=950, rent6=950, building="20 B Street"), apt(3, rent12=900, rent6=900, building="30 C Street")]
    for minutes in ({1: 5, 2: 30, 3: 55}, {1: 55, 2: 30, 3: 5}, None):
        est = (lambda aid, pos, kind: {"Tram": minutes[aid]}) if minutes else None
        assert [c.apartment_id for c in hm.build_shortlist(r, apts, TODAY, est).listed] == [3, 2, 1]


def _five():
    return [apt(i, rent12=900 + 10 * i, rent6=900 + 10 * i, building=f"{i}0 A Street") for i in (1, 2, 3, 4, 5)]


class Staged:
    """Estimates that record which apartments were asked about and each prepare() stage, like the live object."""

    def __init__(self, minutes):
        self.minutes, self.asked, self.stages = minutes, [], []

    def prepare(self, ids):
        self.stages.append(list(ids))

    def __call__(self, aid, pos, kind):
        self.asked.append(aid)
        m = self.minutes.get(aid, 10)
        return {"Tram": travel.UNMEASURED if m is None else m}


def test_travel_is_checked_last_and_only_for_the_shortlisted():
    r, est = req(), Staged({})
    sl = hm.build_shortlist(r, _five(), TODAY, est)
    assert [c.apartment_id for c in sl.listed] == [1, 2, 3] and est.stages == [[1, 2, 3]] and set(est.asked) == {1, 2, 3}
    assert all(c.klass == hm.SUITABLE and c.travel["status"] == travel.PASS for c in sl.listed)
    by = {c.apartment_id: c for c in sl.candidates}
    for aid in (4, 5):   # not shortlisted: not measured, flagged, and never read as a pass
        assert hm.TRAVEL_NOT_CHECKED in by[aid].flags and by[aid].klass == hm.NEEDS_CONFIRMATION
        assert hm.TRAVEL_NEEDS_CONFIRMATION in by[aid].confirmation_reasons and by[aid].travel["status"] == travel.NEEDS_CONFIRMATION
    assert not any(hm.TRAVEL_NOT_CHECKED in c.flags for c in sl.listed)
    assert "not checked" in hm.PLAIN[hm.TRAVEL_NOT_CHECKED]


def test_a_failed_travel_check_is_replaced_by_the_next_and_the_replacement_is_checked():
    r, est = req(), Staged({2: 61})   # 30-minute limit: over 60 fails
    sl = hm.build_shortlist(r, _five(), TODAY, est)
    by = {c.apartment_id: c for c in sl.candidates}
    assert [c.apartment_id for c in sl.listed] == [1, 3, 4] and [c.order for c in sl.listed] == [1, 2, 3]
    assert est.stages == [[1, 2, 3], [4]] and set(est.asked) == {1, 2, 3, 4}          # the other qualifying choices are kept
    assert by[2].klass == hm.EXCLUDED and hm.TRAVEL_EXCEEDS_CEILING in by[2].exclusion_reasons and hm.REPLACED_AFTER_TRAVEL in by[2].flags
    assert hm.REPLACEMENT_AFTER_TRAVEL in by[4].flags and by[4].klass == hm.SUITABLE
    assert hm.TRAVEL_NOT_CHECKED in by[5].flags and hm.REPLACEMENT_AFTER_TRAVEL not in by[1].flags + by[3].flags
    # a replacement that fails is replaced in turn; when the candidates run out the shortlist is shorter, never padded
    est = Staged({1: 61, 2: 61, 4: 61})
    sl = hm.build_shortlist(r, _five(), TODAY, est)
    assert [c.apartment_id for c in sl.listed] == [3, 5] and est.stages == [[1, 2, 3], [4, 5]]
    est = Staged({i: 61 for i in (1, 2, 3, 4, 5)})
    sl = hm.build_shortlist(r, _five(), TODAY, est)
    assert sl.listed == [] and sl.alternatives == [] and not sl.has_shortlist and est.stages == [[1, 2, 3], [4, 5]]
    assert sl.summary()["counts"][hm.EXCLUDED] == 5


def test_an_unknown_journey_is_neither_a_pass_nor_a_failure_and_is_not_replaced():
    r, est = req(), Staged({2: None})
    sl = hm.build_shortlist(r, _five(), TODAY, est)
    assert [c.apartment_id for c in sl.listed] == [1, 2, 3] and est.stages == [[1, 2, 3]]
    two = sl.listed[1]
    assert two.klass == hm.NEEDS_CONFIRMATION and hm.TRAVEL_NEEDS_CONFIRMATION in two.confirmation_reasons and two.exclusion_reasons == []
    assert sl.summary()["nothing_suitable_yet"] is False
    # with no estimates at all (the stored options) every shortlisted apartment needs confirmation; none is dropped
    sl = hm.build_shortlist(r, _five(), TODAY)
    assert [c.apartment_id for c in sl.listed] == [1, 2, 3] and all(c.klass == hm.NEEDS_CONFIRMATION for c in sl.listed)
    assert sl.summary()["nothing_suitable_yet"] is True


def test_up_to_three_listed_then_alternatives_only_when_fewer_than_three():
    r = req(enquiry(area="South Yarra"))
    apts = [apt(i, suburb="South Yarra", building=f"{i}0 SY Street") for i in (1, 2, 3, 4)] + [apt(9, suburb="Melbourne CBD")]
    sl = hm.build_shortlist(r, apts, TODAY, estimates_all(10))
    assert len(sl.listed) == 3 and sl.alternatives == [] and sl.fourth_option is None
    sl2 = hm.build_shortlist(r, apts[:2] + [apts[-1]], TODAY, estimates_all(10))
    assert len(sl2.listed) == 2 and [c.apartment_id for c in sl2.alternatives] == [9] and sl2.alternatives[0].klass == hm.ALTERNATIVE_AREA


def test_never_more_than_three_and_no_fourth_option():
    """GP6(d) (16 Nov 2026): the maximum is three. The building-variety fourth option is gone, however the buildings fall."""
    r = req()
    apts = [apt(1, building="10 A Street"), apt(2, building="10 A Street"), apt(3, building="20 B Street"), apt(4, building="30 C Street")]
    sl = hm.build_shortlist(r, apts, TODAY, estimates_all(10))
    assert [c.apartment_id for c in sl.listed] == [1, 2, 3] and sl.fourth_option is None and sl.fourth_option_reason is None
    assert sl.summary()["fourth_option"] is None and sl.as_dict()["fourth_option"] is None
    assert not any(hm.FOURTH_OPTION_PROVISIONAL in c.flags for c in sl.candidates)
    assert hm.MAX_SHORTLIST == 3 and not hasattr(hm, "_fourth_option")
    many = [apt(i, building=f"{i} Z Street") for i in range(1, 12)]
    assert len(hm.build_shortlist(r, many, TODAY, estimates_all(10)).listed) == 3


def test_alternatives_stay_inside_the_same_maximum_of_three_and_are_travel_checked_last():
    r = req(enquiry(area="South Yarra"))
    full = [apt(i, suburb="South Yarra", building=f"{i}0 SY Street") for i in (1, 2)]
    alts = [apt(i, suburb="Melbourne CBD", rent12=900 + i, rent6=900 + i, building=f"{i}0 CBD Street") for i in (7, 8, 9)]
    est = Staged({})
    sl = hm.build_shortlist(r, full + alts, TODAY, est)
    assert [c.apartment_id for c in sl.listed] == [1, 2] and [c.apartment_id for c in sl.alternatives] == [7]   # three in all
    assert est.stages == [[1, 2], [7]] and sl.alternatives[0].klass == hm.ALTERNATIVE_AREA
    # an alternative that fails travel is replaced by the next alternative, which is checked
    est = Staged({7: 61})
    sl = hm.build_shortlist(r, full + alts, TODAY, est)
    assert [c.apartment_id for c in sl.alternatives] == [8] and est.stages == [[1, 2], [7], [8]]
    # a full match that fails travel leaves room for one more alternative; nothing is relabelled as a match
    est = Staged({2: 61})
    sl = hm.build_shortlist(r, full + alts, TODAY, est)
    assert [c.apartment_id for c in sl.listed] == [1] and [c.apartment_id for c in sl.alternatives] == [7, 8]
    assert all(c.klass == hm.ALTERNATIVE_AREA for c in sl.alternatives) and len(sl.listed) + len(sl.alternatives) == 3
    # three full matches: no alternatives at all
    three = full + [apt(3, suburb="South Yarra", building="30 SY Street")]
    assert hm.build_shortlist(r, three + alts, TODAY, Staged({})).alternatives == []


def test_no_candidate_means_no_shortlist():
    sl = hm.build_shortlist(req(enquiry(budget_total_weekly=300)), [apt(1), apt(2, suburb="South Yarra")], TODAY)
    assert not sl.has_shortlist and sl.listed == [] and sl.alternatives == []
    assert sl.summary()["counts"][hm.EXCLUDED] == 2


# ------------------------------------------------------------------------------------------------------- scenarios
def test_final_size_scenario_needs_planning_budget_and_is_provisional_capacity_only():
    e = enquiry(headcount_target=4, bedrooms=["2 bedrooms"])
    with pytest.raises(hm.InvalidRequirements):
        hm.requirements_from_enquiry(e, TWO_OWN, scenario=hm.SCENARIO_FINAL_SIZE)
    r = hm.requirements_from_enquiry(e, TWO_OWN, scenario=hm.SCENARIO_FINAL_SIZE, planning_budget=1300)
    assert r.head_count == 4 and r.whole_apartment_max == Decimal("1300")
    c = hm.evaluate(r, apt(1), TODAY)
    assert c.klass == hm.NEEDS_CONFIRMATION and hm.PROVISIONAL_FINAL_SIZE in c.confirmation_reasons
    assert c.bedding["status"] == "capacity_only" and c.travel is None   # future members' journeys cannot be assessed
    too_small = hm.evaluate(hm.requirements_from_enquiry(enquiry(headcount_target=6, bedrooms=["2 bedrooms"]), TWO_OWN, scenario=hm.SCENARIO_FINAL_SIZE, planning_budget=2000), apt(1), TODAY)
    assert too_small.klass == hm.EXCLUDED and hm.PARTY_TOO_LARGE in too_small.exclusion_reasons


def test_requirements_reject_open_ended_wide_flexibility_and_bad_inputs():
    with pytest.raises(hm.InvalidRequirements):
        req(enquiry(move_in_flex=hm.WIDE_FLEX))
    with pytest.raises(hm.InvalidRequirements):
        req(enquiry(area="Docklands"))
    with pytest.raises(hm.InvalidRequirements):
        req(enquiry(budget_total_weekly=0))
    r = req(enquiry(move_in_flex=hm.WIDE_FLEX, move_in_earliest="2027-02-25", move_in_latest="2027-03-20"))
    assert r.move_in_window == (date(2027, 2, 25), date(2027, 3, 20))


def test_requirements_snapshot_carries_every_matching_input_but_no_contacts():
    ppl = [{"position": 1, "sleep": "Own bedroom", "doing": "Studying", "institution": "RMIT", "place": None, "primary_destination": None,
            "first_name": "X", "email": "x@example.com", "phone": "000"},
           {"position": 2, "sleep": "Own bedroom", "doing": "Working", "place": "Docklands", "primary_destination": None}]
    d = req(ppl=ppl).as_dict()
    assert d["people"][0]["institution"] == "RMIT" and d["people"][1]["place"] == "Docklands"
    assert set(d["people"][0]) == set(hm.PERSON_KEYS) and "email" not in str(d) and "first_name" not in str(d)
    changed = req(ppl=[dict(ppl[0], institution="Monash"), ppl[1]]).as_dict()
    assert changed != d


def test_dict_output_has_no_contact_details_and_every_code_has_plain_words():
    sl = hm.build_shortlist(req(), [apt(1)], TODAY)
    text = str(sl.as_dict())
    for word in ("email", "phone", "first_name", "last_name"):
        assert word not in text
    codes = [v for k, v in vars(hm).items() if k.isupper() and isinstance(v, str) and v.isupper() and "_" in v]
    for code in codes:
        assert code in hm.PLAIN, code


def test_building_key():
    assert hm.building_key("Unit 11 / 35 Darling Street") == "35 Darling Street"
    assert hm.building_key("35 Darling Street") == "35 Darling Street"


def test_progress_callback_is_informational_and_may_abandon_the_run():
    """O2: `progress(done, total)` after each apartment; a raise abandons the run (used by H2 lease heartbeats); results are unchanged."""
    apts = [apt(1), apt(2, rent12=950), apt(3, type_code="3B", rent12=980, building="20 Other Road")]
    seen = []
    a = hm.build_shortlist(req(), apts, date(2026, 11, 8))
    b = hm.build_shortlist(req(), apts, date(2026, 11, 8), progress=lambda d, t: seen.append((d, t)))
    assert seen == [(1, 3), (2, 3), (3, 3)] and a.as_dict() == b.as_dict()

    class Stop(Exception):
        pass

    def stop(d, t):
        if d == 2:
            raise Stop()
    with pytest.raises(Stop):
        hm.build_shortlist(req(), apts, date(2026, 11, 8), progress=stop)
