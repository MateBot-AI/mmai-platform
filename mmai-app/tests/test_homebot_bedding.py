"""Phase B1 acceptance tests: server/homebot_bedding.py — standalone bedding feasibility (A4, A5, H21, M3).
Pure functions; no database, no server, no personal attributes beyond the recorded sleeping choice. Inventory strings are the
exact A4(d) values plus malformed/adversarial inputs. Nothing here touches legacy rules.parse_bedding or matching."""
import time
from itertools import product

import pytest

from server import homebot, homebot_bedding as hb

P = lambda *choices: [{"sleep": c} for c in choices]   # saved people records carry more keys; only "sleep" matters here
A4D_VALUES = ["1 king or 2 singles", "1 king or 2 singles or 3 singles", "1 king or 2 singles or 3 singles or 1 king+1 single",
              "1 king or 3 singles", "1 king or 2 singles or 1 single+1 bunk bed", "1 single or 1 bunk bed",
              "1 double or 1 bunk bed", "1 bunk or 1 double", "1 queen", "1 double"]
KS = "1 king or 2 singles"


# ---------------------------------------------------------------------------------------------------- choice vocabulary
def test_choices_match_homebot_form_exactly():
    assert set(hb.CHOICES) == set(homebot.SLEEP) | {homebot.SLEEP_ALONE}
    assert hb.MAX_PEOPLE == homebot.MAX_PEOPLE == 7


# ------------------------------------------------------------------------------------------------------ strict parser
@pytest.mark.parametrize("value", A4D_VALUES)
def test_every_a4d_value_parses(value):
    setups = hb.parse_room(value)
    assert setups and all(1 <= hb.places(s) <= 3 for s in setups)


def test_parse_examples_and_dedup():
    assert hb.parse_room("1 king or 2 singles or 1 king+1 single") == (("king",), ("single", "single"), ("king", "single"))
    assert hb.parse_room("1 bunk or 1 double") == (("bunk",), ("double",))
    assert hb.parse_room("1 BUNK BED  or   1 bunk") == (("bunk",),)          # case/whitespace tolerant, equivalent setups deduplicated
    assert hb.parse_room("2 singles or 1 single+1 single") == (("single", "single"),)


@pytest.mark.parametrize("bad", [
    "1 king or 2 singles or",          # trailing connector
    "1 king or 2 singles xx",          # trailing garbage (legacy prefix regex would accept the prefix)
    "1 kings", "1 singles", "2 single", "1 king bed", "2 bunk bed",   # vocabulary is exact: plural only for 2+ singles, "bed" only after bunk
    "king", "1", "", "   ", "or",
    "1 king or", "1 king +", "+1 single", "1 king + + 1 single",
    "1 sofa", "1 king or 2 singel",
    "0 singles", "00 king",
    "2 bunk", "2 bunks", "1 bunk+1 bunk",                     # two bunk units in one room: impossible (A4(f))
    "4 singles", "2 singles+2 singles", "1 king+2 singles",       # more than three sleeping places (A4(c))
    "1 king+1 queen", "2 kings",                                  # two couple beds in one room is not an ALM setup
    "1000000000 singles", "99 singles", "10 singles",             # count bound (before any expansion)
    "1 king or 1 king or 1 king or 1 king or 1 king or 1 king or 1 king or 1 king or 1 king",  # > MAX_ALTERNATIVES
    "1 single+1 single+1 single+1 single",                        # > MAX_ITEMS
    "1 king" + " or 1 king" * 20,                                 # oversized raw string
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(hb.InvalidInventory):
        hb.parse_room(bad)


@pytest.mark.parametrize("bad", [None, 1, 1.0, True, ["1 king"], {"v": "1 king"}, b"1 king"])
def test_parse_rejects_non_text(bad):
    with pytest.raises(hb.InvalidInventory):
        hb.parse_room(bad)


def test_adversarial_strings_are_rejected_quickly_without_expansion():
    for s in ["9" * 100 + " singles", "1 single+" * 60 + "1 single", " or ".join(["1 king"] * 40), "1 king " + "x" * 5000]:
        t = time.perf_counter()
        with pytest.raises(hb.InvalidInventory):
            hb.parse_room(s)
        assert time.perf_counter() - t < 0.05


def test_apartment_columns_must_match_type():
    apt = hb.parse_apartment("2B2B", KS, "1 single or 1 bunk bed", None)
    assert apt.type_code == "2B2B" and len(apt.rooms) == 2 and apt.standard_capacity == 4 and apt.a4b_max == 5
    assert len(hb.parse_apartment("studio", "1 king or 2 singles or 3 singles").rooms) == 1
    assert len(hb.parse_apartment("3B", KS, KS, KS, ).rooms) == 3
    for args in [("2B2B", KS, None, None),            # missing bedroom2
                 ("2B2B", KS, "", None),              # blank bedroom2
                 ("2B2B", KS, KS, KS),                # too many rooms: never silently a smaller apartment
                 ("1BR", KS, KS, None),
                 ("3B", KS, None, KS),                # gap
                 ("2B2B", KS, "1 kings", None),       # one invalid column spoils the record
                 ("2B2B", KS, None, 5),               # non-text in an unused column
                 ("4B", KS, KS, KS), (None, KS, None, None), (3, KS, None, None)]:
        with pytest.raises(hb.InvalidInventory):
            hb.parse_apartment(*args)


def test_physical_combinations_validated_before_any_search():
    # two rooms each offering double OR bunk: a combination may pick one bunk + one double, never two bunks
    apt = hb.parse_apartment("2B2B", "1 double or 1 bunk bed", "1 double or 1 bunk bed", None)
    combos = apt.physical_combos
    assert len(combos) == 3 and all(sum(s.count("bunk") for s in c) <= 1 for c in combos)
    # two FIXED bunk-only rooms have no physical combination: unknown inventory, not a smaller apartment
    for args in [("2B2B", "1 bunk bed", "1 bunk bed", None),
                 ("2B2B", "3 singles", "3 singles", None),                     # 6 places in a 2-bedroom exceeds A4(b)
                 ("3B", "3 singles", "3 singles", "3 singles"),                # 9 places
                 ("2B2B", "1 single+1 bunk bed", "1 bunk bed", None)]:
        with pytest.raises(hb.InvalidInventory):
            hb.parse_apartment(*args)
        assert hb.assess(*args, P(hb.OWN)).status == hb.UNKNOWN_INVENTORY
    # two fixed bunk rooms are unknown stock whatever the party size (1 or 2 occupants included)
    for people in (P(hb.BUNK), P(hb.BUNK, hb.BUNK), P(hb.OWN)):
        assert hb.assess("2B2B", "1 bunk bed", "1 bunk bed", None, people).status == hb.UNKNOWN_INVENTORY
    # double-or-bunk in both rooms still verifies a partner pair + a bunk pair using one unit, with the other bunk never chosen
    r = hb.assess("2B2B", "1 double or 1 bunk bed", "1 double or 1 bunk bed", None, P(hb.PARTNER, hb.PARTNER, hb.BUNK, hb.BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["bunk_units_used"] == 1
    assert r.facts["arrangement"]["bunk_units_physical"] == 1 and r.facts["arrangement"]["places_physical"] == 4
    # both "2 singles or 3 singles": the 3+3 combination is dropped, 2+3 and 2+2 remain legal
    apt = hb.parse_apartment("2B2B", "2 singles or 3 singles", "2 singles or 3 singles", None)
    assert len(apt.physical_combos) == 3
    r = hb.assess("2B2B", "2 singles or 3 singles", "2 singles or 3 singles", None, P(*[hb.TWIN] * 4))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["places_physical"] == 4
    # a spare room keeps its hardware in the facts (occupants 0) so the limits stay verifiable
    r = hb.assess("2B2B", KS, "1 single or 1 bunk bed", None, P(hb.OWN))
    assert r.status == hb.VERIFIED_FEASIBLE
    spare = [room for room in r.facts["arrangement"]["rooms"] if room["people"] == 0][0]
    assert spare["setup"] != "no setup" and spare["occupancy"] == "unoccupied (spare bedroom)"
    # alternatives keep the apartment usable: only the over-capacity combination is dropped
    apt = hb.parse_apartment("2B2B", "1 king or 2 singles or 3 singles", "1 king or 2 singles or 3 singles", None)
    assert len(apt.physical_combos) == 8 and (("single",) * 3, ("single",) * 3) not in apt.physical_combos
    r = hb.assess("2B2B", "1 king or 2 singles or 3 singles", "1 king or 2 singles or 3 singles", None, P(*[hb.TWIN_OR_BUNK] * 5))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.facts["extra_occupants"] == 1


def test_apartment_rooms_direct_construction_is_validated():
    ok = hb.ApartmentRooms("1BR", ((("king",), ("single", "single")),))
    assert ok.rooms == ((("king",), ("single", "single")),)
    assert hb.ApartmentRooms("1BR", ((("single", "king"), ("king", "single")),)).rooms == (( ("king", "single"),),)  # sorted, deduplicated
    for code, rooms in [("1BR", ()), ("2B2B", ((("king",),),)), ("1BR", ((("king",),), (("king",),))), ("4B", ((("king",),),)),
                        (None, ((("king",),),)), ("1BR", [(("king",),)]), ("1BR", ((),)), ("1BR", (("king",),)),
                        ("1BR", ((("sofa",),),)), ("1BR", ((("bunk", "bunk"),),)), ("1BR", ((("king", "queen"),),)),
                        ("1BR", ((("single",) * 4,),)), ("1BR", ((("king",),) * 9,)), ("2B2B", ((("bunk",),), (("bunk",),)))]:
        with pytest.raises(hb.InvalidInventory):
            hb.ApartmentRooms(code, rooms)
    with pytest.raises(TypeError):
        hb.assess_bedding(("1BR", ((("king",),),)), hb.PartySleep(own_n=1))
    with pytest.raises(TypeError):
        hb.assess_bedding(ok, {"own_n": 1})


def test_assess_wraps_inventory_problems_as_unknown_not_feasible():
    r = hb.assess("2B2B", "1 king or 2 singles or", KS, None, P(hb.OWN))
    assert r.status == hb.UNKNOWN_INVENTORY and "unrecognised" in r.facts["detail"]
    assert hb.assess("2B2B", KS, None, None, P(hb.OWN)).status == hb.UNKNOWN_INVENTORY


# -------------------------------------------------------------------------------------------------------- party input
def test_party_counts_and_validation():
    p = hb.party_from_people(P(hb.OWN, hb.PARTNER, hb.PARTNER, hb.TWIN, hb.TWIN_OR_BUNK, hb.BUNK))
    assert (p.own_n, p.partner_n, p.twin_n, p.twin_or_bunk_n, p.bunk_n, p.alone, p.size, p.pairs) == (1, 2, 1, 1, 1, False, 6, 1)
    for bad in [[], P(*[hb.OWN] * 8), P(hb.PARTNER), P(hb.PARTNER, hb.PARTNER, hb.PARTNER), P(hb.ALONE, hb.OWN),
                [{"sleep": "Own room"}], [{"sleep": None}], [{"name": "x"}], ["Own bedroom"], "Own bedroom", None, [1]]:
        with pytest.raises(hb.InvalidParty):
            hb.party_from_people(bad)
    for kw in [dict(own_n=-1, twin_n=2), dict(own_n=True), dict(own_n=1.0), dict(own_n=0), dict(own_n=8), dict(partner_n=3),
               dict(alone=True, own_n=1), dict(alone=1)]:
        with pytest.raises(hb.InvalidParty):
            hb.PartySleep(**kw)
    assert hb.assess("2B2B", KS, KS, None, P(hb.PARTNER)).status == hb.INVALID_PARTY


# -------------------------------------------------------------------------------------------------- verified placements
def test_own_rooms_and_twin_pair_verified_with_spare_capacity_facts():
    r = hb.assess("2B2B", KS, KS, None, P(hb.OWN, hb.TWIN, hb.TWIN))
    assert r.status == hb.VERIFIED_FEASIBLE and r.reasons == ()
    f = r.facts
    assert f["head_count"] == 3 and f["extra_occupants"] == 0 and f["extra_bed_charge_weekly"] == "0.00"
    assert f["arrangement"]["rooms_used"] == 2 and f["arrangement"]["spare_rooms"] == 0 and not f["arrangement"]["uses_bunk"]
    assert "not a person-to-room assignment" in f["meaning"] and "pairing" not in f


def test_own_bedroom_is_never_shared_h21():
    # three own-bedroom people need three rooms; a 2-bedroom cannot do it even though a 3-singles room has the beds
    r = hb.assess("2B2B", "1 king or 2 singles or 3 singles", KS, None, P(hb.OWN, hb.OWN, hb.OWN))
    assert r.status == hb.NOT_FEASIBLE and r.reasons[0] == hb.ROOMS_INSUFFICIENT
    assert hb.assess("3B", KS, KS, KS, P(hb.OWN, hb.OWN, hb.OWN)).status == hb.VERIFIED_FEASIBLE


def test_spare_bedroom_permitted_h21():
    r = hb.assess("3B", KS, KS, KS, P(hb.PARTNER, hb.PARTNER))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["spare_rooms"] == 2
    assert r.facts["arrangement"]["rooms_used"] == 1


def test_own_room_occupant_in_three_single_room_leaves_beds_unused_not_removed():
    r = hb.assess("1BR", "1 king or 2 singles or 3 singles", None, None, P(hb.OWN))
    assert r.status == hb.VERIFIED_FEASIBLE
    assert r.facts["verified_arrangements"] == 3                           # king, 2 singles, 3 singles all work for one person
    # two twin acceptors in a 3-singles-only room: verified, third bed unoccupied
    r = hb.assess("1BR", "3 singles", None, None, P(hb.TWIN, hb.TWIN))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["beds_unused"] == 1


def test_partner_pair_needs_couple_bed_and_exclusive_room():
    assert hb.assess("1BR", "1 king or 2 singles", None, None, P(hb.PARTNER, hb.PARTNER)).status == hb.VERIFIED_FEASIBLE
    r = hb.assess("1BR", "2 singles", None, None, P(hb.PARTNER, hb.PARTNER))
    assert r.status == hb.NOT_FEASIBLE and hb.COUPLE_BED_UNAVAILABLE in r.reasons
    r = hb.assess("1BR", "1 bunk bed", None, None, P(hb.PARTNER, hb.PARTNER))     # a bunk is never a partner bed
    assert r.status == hb.NOT_FEASIBLE and hb.COUPLE_BED_UNAVAILABLE in r.reasons
    # pair in a king+single room with nobody else: the single stays unused, verified
    r = hb.assess("1BR", "1 king+1 single", None, None, P(hb.PARTNER, hb.PARTNER))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["beds_unused"] == 1


def test_pairing_is_disclosed_as_unassigned_especially_four_plus():
    r = hb.assess("2B2B", KS, KS, None, P(hb.PARTNER, hb.PARTNER, hb.PARTNER, hb.PARTNER))
    assert r.status == hb.VERIFIED_FEASIBLE
    assert "NOT assigned" in r.facts["pairing"] and "four or more" in r.facts["pairing"]
    assert "identities not assigned" in r.facts["arrangement"]["rooms"][0]["occupancy"]
    r2 = hb.assess("1BR", KS, None, None, P(hb.PARTNER, hb.PARTNER))
    assert "NOT assigned" in r2.facts["pairing"] and "four or more" not in r2.facts["pairing"]


def test_twin_only_people_need_two_singles_never_a_bunk():
    r = hb.assess("2B1B", KS, "1 single or 1 bunk bed", None, P(hb.OWN, hb.TWIN, hb.TWIN))
    assert r.status == hb.VERIFIED_FEASIBLE                     # own person takes the single room, twins the 2-singles room
    r = hb.assess("2B1B", "1 king", "1 single or 1 bunk bed", None, P(hb.OWN, hb.TWIN, hb.TWIN))
    assert r.status == hb.NOT_FEASIBLE and hb.TWIN_SETUP_UNAVAILABLE in r.reasons
    r = hb.assess("1BR", "1 bunk bed", None, None, P(hb.TWIN, hb.TWIN))
    assert r.status == hb.NOT_FEASIBLE and r.reasons[0] == hb.TWIN_SETUP_UNAVAILABLE
    # a lone twin acceptor may sleep alone in a twin room (second bed unoccupied)
    r = hb.assess("1BR", KS, None, None, P(hb.TWIN))
    assert r.status == hb.VERIFIED_FEASIBLE
    # ... but a room offering only a king/queen/double is not a bed type they chose: possible, not verified
    for value in ["1 king", "1 queen", "1 double"]:
        for choice in (hb.TWIN, hb.TWIN_OR_BUNK, hb.BUNK):
            r = hb.assess("1BR", value, None, None, P(choice))
            assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.BED_TYPE_CONSENT_UNCONFIRMED,)
    r = hb.assess("2B2B", "1 king", KS, None, P(hb.TWIN, hb.TWIN, hb.TWIN))    # third twin has only the king room
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.BED_TYPE_CONSENT_UNCONFIRMED,)


def test_bunk_only_for_people_who_said_yes_m3():
    # two willing of three suffice; the own-room person is never put in the bunk
    r = hb.assess("2B1B", KS, "1 single or 1 bunk bed", None, P(hb.OWN, hb.BUNK, hb.TWIN_OR_BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["uses_bunk"] and r.facts["arrangement"]["bunk_units_used"] == 1
    # two "Bunk bed" choosers + two twin-only people in a 2-bed: twins get the 2 singles, bunk pair the bunk
    r = hb.assess("2B1B", KS, "1 single or 1 bunk bed", None, P(hb.TWIN, hb.TWIN, hb.BUNK, hb.BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE
    # three twin-only people and one bunk place free: nobody unwilling is put in the bunk
    r = hb.assess("2B1B", KS, "1 single or 1 bunk bed", None, P(hb.TWIN, hb.TWIN, hb.TWIN, hb.BUNK))
    assert r.status == hb.NOT_FEASIBLE and hb.BUNK_UNWANTED in r.reasons
    r = hb.assess("1BR", KS, None, None, P(hb.BUNK, hb.BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.BED_TYPE_CONSENT_UNCONFIRMED,)
    r = hb.assess("1BR", "1 king", None, None, P(hb.BUNK, hb.BUNK))
    assert r.status == hb.NOT_FEASIBLE and hb.BUNK_UNAVAILABLE in r.reasons


def test_one_bunk_unit_per_apartment_combination_wise_a4f():
    both = "1 double or 1 bunk bed"
    # two rooms each offering a bunk are valid inventory; a plan may use 0 or 1 bunk, never 2
    assert hb.assess("2B2B", both, both, None, P(hb.PARTNER, hb.PARTNER, hb.PARTNER, hb.PARTNER)).status == hb.VERIFIED_FEASIBLE
    r = hb.assess("2B2B", both, both, None, P(hb.BUNK, hb.BUNK, hb.PARTNER, hb.PARTNER))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["bunk_units_used"] == 1
    r = hb.assess("2B2B", both, both, None, P(hb.BUNK, hb.BUNK, hb.BUNK, hb.BUNK))
    assert r.status == hb.NOT_FEASIBLE and hb.BUNK_UNIT_LIMIT in r.reasons
    # four bunk choosers where singles exist: possible only with a bed-type confirmation, still one bunk unit
    r = hb.assess("2B1B", "1 single or 1 bunk bed", "1 king or 2 singles or 1 single+1 bunk bed", None, P(hb.BUNK, hb.BUNK, hb.BUNK, hb.BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.facts["arrangement"]["bunk_units_used"] == 1
    assert hb.BED_TYPE_CONSENT_UNCONFIRMED in r.reasons


def test_twin_or_bunk_person_counted_once_h21():
    # two twin-or-bunk people fill a bunk OR a twin room, not both; three of them cannot occupy a single+bunk room as verified
    r = hb.assess("1BR", "1 single or 1 bunk bed", None, None, P(hb.TWIN_OR_BUNK, hb.TWIN_OR_BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE and r.facts["arrangement"]["uses_bunk"]
    r = hb.assess("1BR", "1 king or 2 singles or 1 single+1 bunk bed", None, None, P(hb.TWIN_OR_BUNK, hb.TWIN_OR_BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE and not r.facts["arrangement"]["uses_bunk"]  # the plain 2-singles setup is verified
    r = hb.assess("1BR", "1 king or 2 singles or 1 single+1 bunk bed", None, None, P(hb.TWIN_OR_BUNK, hb.TWIN_OR_BUNK, hb.TWIN_OR_BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION
    assert set(r.reasons) == {hb.THIRD_OCCUPANT_CONSENT_UNCONFIRMED, hb.MIXED_ROOM_CONSENT_UNCONFIRMED}
    assert r.facts["extra_occupants"] == 1 and r.facts["extra_bed_charge_weekly"] == "70.00"


def test_whole_apartment_alone():
    assert hb.assess("Studio", "1 king", None, None, P(hb.ALONE)).status == hb.VERIFIED_FEASIBLE
    assert hb.assess("2B2B", KS, KS, None, P(hb.ALONE)).facts["arrangement"]["spare_rooms"] == 1
    r = hb.assess("Studio", "1 bunk bed", None, None, P(hb.ALONE))            # alone does not establish bunk acceptance
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED,)
    r = hb.assess("1BR", "1 bunk bed", None, None, P(hb.OWN))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED,)


# --------------------------------------------------------------------------------------------- possible, never verified
def test_third_occupant_is_possible_not_verified_a4c():
    r = hb.assess("1BR", "1 king or 2 singles or 3 singles", None, None, P(hb.TWIN, hb.TWIN, hb.TWIN_OR_BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.THIRD_OCCUPANT_CONSENT_UNCONFIRMED,)
    assert r.facts["extra_occupants"] == 1 and r.facts["extra_bed_charge_weekly"] == "70.00"
    assert "not suitable yet" in r.facts["meaning"]
    # seven people in a 3-bedroom only via a third bed somewhere
    r = hb.assess("3B", KS, KS, "1 king or 2 singles or 3 singles", P(*[hb.TWIN_OR_BUNK] * 7))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.facts["extra_occupants"] == 1
    assert hb.assess("3B", KS, KS, KS, P(*[hb.TWIN_OR_BUNK] * 7)).status == hb.NOT_FEASIBLE


def test_a4e_king_plus_single_sharing_stays_unresolved():
    r = hb.assess("1BR", "1 king or 2 singles or 1 king+1 single", None, None, P(hb.PARTNER, hb.PARTNER, hb.TWIN_OR_BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.A4E_APPLICABILITY_UNRESOLVED,)
    r = hb.assess("1BR", "1 king+1 single", None, None, P(hb.PARTNER, hb.PARTNER, hb.BUNK))
    assert set(r.reasons) == {hb.A4E_APPLICABILITY_UNRESOLVED, hb.BED_TYPE_CONSENT_UNCONFIRMED}
    # a couple plus an own-bedroom person never share (H21), whatever the room offers
    r = hb.assess("1BR", "1 king+1 single", None, None, P(hb.PARTNER, hb.PARTNER, hb.OWN))
    assert r.status == hb.NOT_FEASIBLE and r.reasons[0] == hb.ROOMS_INSUFFICIENT
    # with a spare room available the pair and the third person get separate rooms: verified, A4(e) not needed
    r = hb.assess("2B2B", "1 king+1 single", KS, None, P(hb.PARTNER, hb.PARTNER, hb.TWIN_OR_BUNK))
    assert r.status == hb.VERIFIED_FEASIBLE


def test_possible_tier_keeps_firm_choices():
    # twin-only people are never pushed into a bunk even to make a "possible" plan
    r = hb.assess("1BR", "1 single+1 bunk bed", None, None, P(hb.TWIN, hb.TWIN))
    assert r.status == hb.NOT_FEASIBLE
    # twin + bunk acceptor in a mixed room is possible (mixed-room confirmation), not verified
    r = hb.assess("1BR", "1 single+1 bunk bed", None, None, P(hb.TWIN, hb.BUNK))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.MIXED_ROOM_CONSENT_UNCONFIRMED,)
    # possible plans list every confirmation code seen, and the reported plan needs the fewest confirmations
    r = hb.assess("2B1B", "1 king or 2 singles or 3 singles", "1 single or 1 bunk bed", None, P(hb.TWIN, hb.TWIN, hb.TWIN, hb.OWN))
    assert r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION and r.reasons == (hb.THIRD_OCCUPANT_CONSENT_UNCONFIRMED,)
    # the other possible plan (own person alone in the bunk room, three twins in the 3-singles room) needs two confirmations
    assert r.facts["all_confirmation_codes"] == [hb.BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED, hb.THIRD_OCCUPANT_CONSENT_UNCONFIRMED]


# ---------------------------------------------------------------------------------------------------------- A5 charge
@pytest.mark.parametrize("code,beds,people,extra,charge", [
    ("Studio", ("1 king or 2 singles or 3 singles",), 2, 0, "0.00"), ("Studio", ("1 king or 2 singles or 3 singles",), 3, 1, "70.00"),
    ("1BR", ("1 king or 3 singles",), 3, 1, "70.00"),
    ("2B2B", (KS, "1 king or 2 singles or 3 singles"), 4, 0, "0.00"), ("2B2B", (KS, "1 king or 2 singles or 3 singles"), 5, 1, "70.00"),
    ("3B", (KS, KS, "1 king or 2 singles or 3 singles"), 6, 0, "0.00"), ("3B", (KS, KS, "1 king or 2 singles or 3 singles"), 7, 1, "70.00"),
])
def test_extra_bed_charge_is_currency_and_occupant_count_separate(code, beds, people, extra, charge):
    r = hb.assess(code, *(list(beds) + [None] * (3 - len(beds))), P(*[hb.TWIN_OR_BUNK] * people))
    assert r.status in (hb.VERIFIED_FEASIBLE, hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION)
    assert r.facts["extra_occupants"] == extra and r.facts["extra_bed_charge_weekly"] == charge
    assert r.facts["currency"].startswith("AUD per week")
    assert (r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION) == (extra == 1)   # a third bed in a room is never verified


def test_capacity_exceeded_a4b():
    r = hb.assess("2B2B", KS, KS, None, P(*[hb.TWIN] * 6))
    assert r.status == hb.NOT_FEASIBLE and r.reasons == (hb.CAPACITY_EXCEEDED,) and r.facts["a4b_max"] == 5
    assert hb.assess("Studio", "1 king or 2 singles or 3 singles", None, None, P(*[hb.TWIN] * 4)).reasons == (hb.CAPACITY_EXCEEDED,)


# ------------------------------------------------------------------------------------------------ exhaustive property
def test_exhaustive_property_verified_never_exceeds_capacity_or_double_counts():
    rooms_pool = ["1 king or 2 singles", "1 king or 2 singles or 3 singles", "1 single or 1 bunk bed", "1 double or 1 bunk bed",
                  "1 king or 2 singles or 1 single+1 bunk bed", "1 king+1 single"]
    kinds = [hb.OWN, hb.TWIN, hb.TWIN_OR_BUNK, hb.BUNK]
    worst = 0.0
    for r1, r2 in product(rooms_pool, repeat=2):
        for n in range(1, 6):
            for combo in set(tuple(sorted(c)) for c in product(kinds, repeat=n)):
                people = P(*combo) + ([] if n % 2 == 0 else [])
                t = time.perf_counter()
                r = hb.assess("2B2B", r1, r2, None, people)
                worst = max(worst, time.perf_counter() - t)
                if (r1, r2) == ("1 king+1 single", "1 king+1 single"):
                    assert r.status == hb.UNKNOWN_INVENTORY       # fixed 3 + 3 places in a 2-bedroom is not valid A4 stock
                    continue
                assert r.status in (hb.VERIFIED_FEASIBLE, hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION, hb.NOT_FEASIBLE)
                if r.status != hb.NOT_FEASIBLE:
                    arr = r.facts["arrangement"]
                    assert sum(room["people"] for room in arr["rooms"]) == n       # every person placed exactly once
                    assert arr["bunk_units_used"] <= arr["bunk_units_physical"] <= 1 and arr["places_physical"] <= 5
                    assert n <= 5
                    for room in arr["rooms"]:
                        assert room["people"] <= 3
                        if r.status == hb.VERIFIED_FEASIBLE:
                            assert room["people"] <= 2 and room["reasons"] == []
                            # a bunk is occupied only when bunk acceptors exist
                            if room["bunk_places_used"]:
                                assert hb.BUNK in combo or hb.TWIN_OR_BUNK in combo
    assert worst < 0.5


def test_seven_people_three_rooms_search_is_bounded():
    t = time.perf_counter()
    r = hb.assess("3B", "1 king or 2 singles or 3 singles or 1 king+1 single", "1 king or 2 singles or 1 single+1 bunk bed",
                  "1 king or 2 singles or 3 singles", P(hb.PARTNER, hb.PARTNER, hb.OWN, hb.TWIN, hb.TWIN_OR_BUNK, hb.BUNK, hb.BUNK))
    assert time.perf_counter() - t < 2.0
    assert r.status in (hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION, hb.NOT_FEASIBLE)   # seven people need a third bed somewhere


MAX_ROOM = ("1 king or 1 queen or 1 double or 1 single or 2 singles or 3 singles or 1 king+1 single or 1 bunk bed")  # 8 alternatives


@pytest.mark.parametrize("people", [
    P(hb.PARTNER, hb.PARTNER, hb.OWN, hb.TWIN, hb.TWIN_OR_BUNK, hb.BUNK, hb.BUNK),
    P(hb.OWN, hb.OWN, hb.TWIN, hb.TWIN, hb.TWIN_OR_BUNK, hb.TWIN_OR_BUNK, hb.BUNK),
    P(hb.PARTNER, hb.PARTNER, hb.PARTNER, hb.PARTNER, hb.TWIN, hb.BUNK, hb.BUNK),
    P(*[hb.TWIN_OR_BUNK] * 7), P(*[hb.TWIN] * 7), P(*[hb.BUNK] * 7),
    P(hb.OWN, hb.OWN, hb.OWN, hb.TWIN, hb.TWIN, hb.BUNK, hb.BUNK),
])
def test_max_input_three_rooms_eight_alternatives_seven_people(people):
    hb._room_use_cached.cache_clear()
    apt = hb.parse_apartment("3B", MAX_ROOM, MAX_ROOM, MAX_ROOM)
    assert len(apt.rooms[0]) == 8 and len(apt.physical_combos) < 8 ** 3      # two-bunk / over-capacity combinations dropped
    t = time.perf_counter()
    r = hb.assess_bedding(apt, hb.party_from_people(people))
    dt = time.perf_counter() - t
    assert dt < 2.0, dt
    assert r.status in (hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION, hb.NOT_FEASIBLE)   # 7 people in a 3-bedroom need a third bed
    if r.status == hb.POSSIBLE_NEEDS_STAFF_CONFIRMATION:
        arr = r.facts["arrangement"]
        assert sum(room["people"] for room in arr["rooms"]) == 7 and arr["bunk_units_used"] <= 1
        assert r.facts["extra_occupants"] == 1 and r.facts["extra_bed_charge_weekly"] == "70.00"


def test_as_dict_is_plain_json_shape():
    d = hb.assess("1BR", KS, None, None, P(hb.OWN)).as_dict()
    assert set(d) == {"status", "reasons", "facts"} and isinstance(d["reasons"], list) and isinstance(d["facts"], dict)
