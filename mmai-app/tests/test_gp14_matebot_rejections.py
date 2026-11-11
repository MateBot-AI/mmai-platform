"""GP14 (MateBot): a room/bed enquirer's "not this apartment" / "not this building" (server/apartment_rejections.py).
Before this increment (frozen inc1b 9aea791b…) no such record existed for MateBot: every test here fails there (ImportError / no
table / no exclusion). Rules, weights and coverage selection are untouched: an excluded apartment is simply not an eligible option
for a group containing that enquiry, decided where the eligible option set is built (matching._unit_fits_apartment)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import apartment_rejections as ar
from server import matching, rules
from server.db import now_iso, retention_guard, row, rows
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401
from tests.test_group_coverage_completion import _primaries, _solo_pool
from tests.test_launch_neutral import _apt, _enq

STAFF = "Anna Chan"


def _apt_same_building(db, address, rent12=1150.0, suburb="Melbourne"):
    """Another unit of a building, stored the way the inventory stores every row: 'Unit <n> / <street address>' (103/103 rows of the
    2026-10-23 stock follow it). The building identity is the street address without the unit + the recorded suburb and city."""
    db.execute("""INSERT INTO apartments(city, address, address_raw, suburb, type_code, bathrooms, rent_12m, rent_6m, available_from, min_lease_months,
                                         bedroom1, bedroom2, bedroom3, bills_included, updated_at)
                  VALUES ('Melbourne', ?, ?, ?, '2B2B', 2, ?, 1225.0, '2027-01-27', ?, '1 king or 2 singles', '1 king or 2 singles', NULL, 0, ?)""",
               (address, address, suburb, rent12, rules.DEFAULT_MIN_LEASE_MONTHS, now_iso()))
    db.commit()
    return row(db.execute("SELECT * FROM apartments ORDER BY id DESC LIMIT 1"))


def _unit(db, eid):
    return matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,))), [])


def _fits(db, apt_row, eid):
    return matching._unit_fits_apartment(matching.apartment_from_row(apt_row), _unit(db, eid))


def _group_of(db, eid):
    return row(db.execute("SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.is_primary=1 AND g.status='Proposed'", (eid,)))


def _listed(db, gid):
    return [int(r["apartment_id"]) for r in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,)))]


# ------------------------------------------------------------------------------------------------- record, scope, exclusions

def test_record_apartment_and_building_scope_travel_on_the_enquiry_row(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    b = _apt_same_building(db, "Unit 2 / 1 Neutral Street")        # same building (street without unit), different unit
    c = _apt(db, address="7 Other Road")
    e1, e2 = _solo_pool(db, 2)
    rec = ar.record(db, e1, "apartment", a["id"], STAFF, customer_reason="too far from work", group_id=None)
    assert rec["scope"] == "apartment" and rec["status"] == "active" and rec["basis_kind"] == "apartment_only" and rec["recorded_by"] == STAFF
    assert rec["offered_basis"]["apartment"]["rent_12m"] == 1150.0 and "enquiry" in rec["offered_basis"]
    ex = ar.exclusions_for(db, e1)
    assert (ex["apartments"], ex["buildings"], ex["allowed"]) == ([a["id"]], [], [])
    assert ex["records"] == [{"id": rec["id"], "scope": "apartment", "key": str(a["id"]), "allowed": []}]
    u = _unit(db, e1)
    assert u.apartment_exclusions == (("apartment", str(a["id"]), ()),)
    assert not matching._unit_fits_apartment(matching.apartment_from_row(a), u)
    assert matching._unit_fits_apartment(matching.apartment_from_row(b), u)      # apartment scope: the other unit is still fine
    assert matching._unit_fits_apartment(matching.apartment_from_row(c), u)
    # the other enquiry is untouched (records are per enquiry, independent)
    assert row(db.execute("SELECT apartment_exclusions_json AS x FROM enquiries WHERE id=?", (e2,)))["x"] is None
    # building scope on e2: both units of the building are out, the other road is not; a unit listed LATER in the building is out too
    ar.record(db, e2, "building", b["id"], STAFF)
    u2 = matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (e2,))), [])
    assert u2.apartment_exclusions == (("building", matching.building_identity(b), ()),) and matching.building_identity(b) == matching.building_identity(a)
    assert not matching._unit_fits_apartment(matching.apartment_from_row(a), u2) and not matching._unit_fits_apartment(matching.apartment_from_row(b), u2)
    assert matching._unit_fits_apartment(matching.apartment_from_row(c), u2)
    later = _apt_same_building(db, "Unit 3 / 1 Neutral Street")
    assert not matching._unit_fits_apartment(matching.apartment_from_row(later), u2)
    # duplicates refused; wrong scope refused; nothing in member_rejections (GP9 is people, not apartments)
    with pytest.raises(ar.RejectionError):
        ar.record(db, e1, "apartment", a["id"], STAFF)
    with pytest.raises(ar.RejectionError):
        ar.record(db, e1, "flat", a["id"], STAFF)
    assert db.execute("SELECT COUNT(*) FROM member_rejections").fetchone()[0] == 0
    acts = [r["action"] for r in rows(db.execute("SELECT action FROM audit_log ORDER BY id"))]
    assert acts.count("apartment_rejection_recorded") == 2


def test_empty_building_key_is_refused_and_never_matches_everything(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    db.execute("UPDATE apartments SET address='', suburb='', city='' WHERE id=?", (a["id"],)); db.commit()
    e1, = _solo_pool(db, 1)
    with pytest.raises(ar.RejectionError):
        ar.record(db, e1, "building", a["id"], STAFF)
    # run-1 finding: access_facts.location_key gives the truthy separators-only "||" for an address-less row; GP14 does not use it
    assert matching.access_facts.location_key({"address": "", "suburb": "", "city": ""}) == "||"
    assert matching.building_identity({"address": "", "suburb": "Carlton", "city": "Melbourne"}) == ""      # street REQUIRED: suburb/city never identify a building
    assert matching.apartment_from_row({**a, "address": "", "suburb": "", "city": ""}).building_key == ""
    # a stored record whose key is empty / separators-only is dropped, never a wildcard; a raw "||" key never matches
    db.execute("UPDATE enquiries SET apartment_exclusions_json=? WHERE id=?", ('{"records": [{"id": 1, "scope": "building", "key": "", "allowed": []}, {"id": 2, "scope": "building", "key": "||", "allowed": []}]}', e1)); db.commit()
    u = _unit(db, e1)
    assert u.apartment_exclusions == (("building", "||", ()),)
    blank = {**a, "address": "", "suburb": "", "city": ""}
    assert not matching.unit_excludes_apartment(u, matching.apartment_from_row(blank)) and not ar.excludes('{"records": [{"scope": "building", "key": "||"}]}', blank)
    # two address-less rows in the same suburb/city are NOT the same building
    x = _apt_same_building(db, "", suburb="Carlton"); y = _apt_same_building(db, "", suburb="Carlton")
    assert matching.building_identity(x) == "" == matching.building_identity(y)


def test_building_identity_is_street_without_unit_plus_recorded_locality():
    from server import homebot_match as hm
    a = {"address": "Unit 11 / 35 Darling Street", "suburb": "South Yarra", "city": "Melbourne"}
    b = {"address": "Unit 2 / 35 Darling Street", "suburb": "South Yarra", "city": "Melbourne"}
    assert matching.building_identity(a) == matching.building_identity(b) == "35 darling street|south yarra|melbourne"
    # parser parity with HomeBot on the street component (one parser for both bots), locality added on top
    assert matching.building_identity(a).split("|")[0] == matching.access_facts.norm_address(hm.building_key(a["address"])) == "35 darling street"
    # the same street text in another recorded suburb or city is a different building (identity keeps the actual locality)
    assert matching.building_identity({**a, "suburb": "Carlton"}) != matching.building_identity(a)
    assert matching.building_identity({**a, "city": "Sydney"}) != matching.building_identity(a)
    assert matching.building_identity({**a, "suburb": "  south  YARRA "}) == matching.building_identity(a)     # normalised text only
    # unknown formats are taken conservatively (whole text as the street): never merged into the 'Unit n / street' building
    assert matching.building_identity({**a, "address": "Unit 11, 35 Darling Street"}) != matching.building_identity(a)
    assert matching.building_identity({**a, "address": "35 Darling Street"}) == matching.building_identity(a)   # a row stored without a unit IS the building


def test_null_suburb_rows_record_and_match_as_the_recorded_empty_locality(db):
    """H-N1 (root, 11 Nov 2026): apartments.suburb is nullable. A building record for such a unit must be recordable (no crash),
    must keep excluding its own blank-locality siblings and must never reach the same street text in a NAMED suburb."""
    from server import homebot_match as hm
    a1 = _apt(db, address="Unit 1 / 5 Blank Street"); a2 = _apt(db, address="Unit 2 / 5 Blank Street"); a3 = _apt(db, address="Unit 3 / 5 Blank Street")
    db.execute("UPDATE apartments SET suburb=NULL WHERE id IN (?,?)", (a1["id"], a2["id"])); db.execute("UPDATE apartments SET suburb='Carlton' WHERE id=?", (a3["id"],)); db.commit()
    a1, a2, a3 = (row(db.execute("SELECT * FROM apartments WHERE id=?", (x["id"],))) for x in (a1, a2, a3))
    assert a1["suburb"] is None
    assert matching.building_identity(a1) == matching.building_identity(a2) == "5 blank street||melbourne" != matching.building_identity(a3)
    assert hm.row_identity({"address": a1["address"]}) == ("5 blank street", hm.UNKNOWN, hm.UNKNOWN)             # key absent: unknown
    assert hm.row_identity({"address": a1["address"], "suburb": None}) == ("5 blank street", "", hm.UNKNOWN)      # key present, NULL: known empty
    e = _enq(db, "Blank", "blank@example.invalid")["id"]
    rec = ar.record(db, e, "building", a1["id"], STAFF)
    assert rec["building_key"] == "5 blank street||melbourne"
    raw = row(db.execute("SELECT apartment_exclusions_json AS x FROM enquiries WHERE id=?", (e,)))["x"]
    assert ar.excludes(raw, a1) and ar.excludes(raw, a2) and not ar.excludes(raw, a3)
    assert ar.group_exclusion_reason(db, 0, a2["id"]) is None   # no group: the helper simply has no members to ask
    # inverse: a record for the NAMED suburb's unit never reaches the blank-locality siblings of the same street text
    e2 = _enq(db, "Named", "named@example.invalid")["id"]
    rec2 = ar.record(db, e2, "building", a3["id"], STAFF)
    raw2 = row(db.execute("SELECT apartment_exclusions_json AS x FROM enquiries WHERE id=?", (e2,)))["x"]
    assert rec2["building_key"] == "5 blank street|carlton|melbourne" and ar.excludes(raw2, a3) and not ar.excludes(raw2, a1) and not ar.excludes(raw2, a2)


def test_permissions_stay_scoped_to_their_record_and_any_independent_block_wins(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    b = _apt_same_building(db, "Unit 2 / 1 Neutral Street")
    e1, e2 = _solo_pool(db, 2)
    # direction 1: building record (permission later lifts unit A) + an independent apartment record on A → A stays excluded
    bld = ar.record(db, e1, "building", a["id"], STAFF)
    apt = ar.record(db, e1, "apartment", a["id"], STAFF)              # independent record, allowed alongside the building one
    db.execute("UPDATE apartments SET rent_12m=990.0 WHERE id=?", (a["id"],)); db.commit()
    ar.allow_reoffer(db, bld["id"], a["id"], STAFF, note="rent came down")
    assert ar.exclusions_for(db, e1)["allowed"] == [a["id"]]
    assert not _fits(db, a, e1), "the apartment-scope record has no permission: it still blocks A"
    assert not _fits(db, b, e1)                                        # the building record still blocks the other unit
    ar.reopen_by_customer(db, apt["id"], STAFF, note="ok with A again")
    assert _fits(db, a, e1) and not _fits(db, b, e1)                   # now only the building record remains, with its permission for A
    # direction 2 (other person): apartment record on B with a permission + a building record over B → B stays excluded
    apt2 = ar.record(db, e2, "apartment", b["id"], STAFF)
    db.execute("UPDATE apartments SET rent_12m=980.0 WHERE id=?", (b["id"],)); db.commit()
    ar.allow_reoffer(db, apt2["id"], b["id"], STAFF, note="cheaper")
    assert _fits(db, b, e2)
    bld2 = ar.record(db, e2, "building", b["id"], STAFF)
    assert not _fits(db, b, e2) and not _fits(db, a, e2), "a permission under the apartment record never reaches the building record"
    ar.reopen_by_customer(db, bld2["id"], STAFF, note="reconsidered")
    assert _fits(db, b, e2)
    # follow-up re-decline: the earlier record's permission does not make the new decline a 'duplicate', and it blocks again
    again = ar.record(db, e2, "apartment", b["id"], STAFF, customer_reason="saw it again, still no")
    assert again["id"] != apt2["id"] and not _fits(db, b, e2)
    with pytest.raises(ar.RejectionError):
        ar.record(db, e2, "apartment", b["id"], STAFF)                  # and THAT one is a duplicate while it blocks
    # reopening the new record does not revive the old (still permitted) record's exclusion
    ar.reopen_by_customer(db, again["id"], STAFF, note="changed mind")
    assert _fits(db, b, e2)
    # the stored shape is records only; nothing decides on the flat summary
    raw = row(db.execute("SELECT apartment_exclusions_json AS x FROM enquiries WHERE id=?", (e2,)))["x"]
    assert set(__import__("json").loads(raw)) == {"records"}
    assert ar.excludes(raw, b) is False and ar.excludes(raw, a) is False
    # same-scope dup guard for the building record of e1 while it still blocks unit B
    with pytest.raises(ar.RejectionError):
        ar.record(db, e1, "building", b["id"], STAFF)
    # but a building re-decline through the permitted unit A is a fresh record (A blocked again, B still blocked)
    re_bld = ar.record(db, e1, "building", a["id"], STAFF)
    assert re_bld["id"] != bld["id"] and not _fits(db, a, e1) and not _fits(db, b, e1)


# ---------------------------------------------------------------------------------- the run: eligibility, coverage, no bypass

def test_run_never_lists_an_excluded_apartment_and_covers_the_same_people(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    c = _apt(db, address="7 Other Road", rent12=1150.0)
    ids = _solo_pool(db, 4)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    before = {r["enquiry_id"] for r in _primaries(db)}
    assert before == set(ids)
    g0 = _group_of(db, ids[0])
    assert set(_listed(db, g0["id"])) == {a["id"], c["id"]}         # both apartments are options before any rejection
    ar.record(db, ids[0], "apartment", a["id"], STAFF, group_id=g0["id"])
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    after = {r["enquiry_id"] for r in _primaries(db)}
    assert after == before and res["enquiries_unmatched"] == 0        # the same people are covered: a valid alternative exists
    for g in rows(db.execute("SELECT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status<>'Dissolved'", (ids[0],))):
        assert a["id"] not in _listed(db, g["id"]), "an excluded apartment was saved as an option for a group containing the enquiry"
        assert row(db.execute("SELECT apartment_id FROM groups WHERE id=?", (g["id"],)))["apartment_id"] != a["id"]
    # people who did not decline it still get it as an option
    others = [g for g in rows(db.execute("SELECT DISTINCT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE g.status<>'Dissolved' AND g.id NOT IN (SELECT group_id FROM group_members WHERE enquiry_id=?)", (ids[0],)))]
    assert any(a["id"] in _listed(db, g["id"]) for g in others)


def test_excluded_best_apartment_forces_the_valid_alternative_not_an_empty_group(db):
    """The only-cheaper apartment is declined by one person: the run must still propose that person at the other apartment (eligible
    options are filtered BEFORE coverage/selection, so nothing is spent on an unusable candidate) and never list the declined one."""
    cheap = _apt(db, address="Unit 1 / 1 Neutral Street", rent12=900.0, rent6=950.0)
    dear = _apt(db, address="7 Other Road", rent12=1150.0)
    ids = _solo_pool(db, 2)
    ar.record(db, ids[0], "apartment", cheap["id"], STAFF)
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    g = _group_of(db, ids[0])
    assert g is not None and _listed(db, g["id"]) == [dear["id"]] and int(g["apartment_id"]) == dear["id"]
    assert res["enquiries_unmatched"] == 0
    # finalise-time guard is fail-closed too: an entry whose only option is excluded is dropped, not saved
    apt_by_id = {str(cheap["id"]): matching.apartment_from_row(cheap)}
    unit = matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (ids[0],))), [])
    entry = {"units": [unit], "apartments": [{"apartment_id": cheap["id"], "rule_score": 1.0, "total_weekly": 900.0}]}
    matching._finalise_entry(entry, 5, apt_by_id)
    assert entry["apartments"] == [] and entry["rule_score"] is None    # run_matching / _complete_coverage drop such an entry
    entry2 = {"units": [unit], "apartments": [{"apartment_id": cheap["id"], "rule_score": 1.0, "total_weekly": 900.0},
                                              {"apartment_id": dear["id"], "rule_score": 0.9, "total_weekly": 1150.0}]}
    matching._finalise_entry(entry2, 5, {**apt_by_id, str(dear["id"]): matching.apartment_from_row(dear)})
    assert [o["apartment_id"] for o in entry2["apartments"]] == [dear["id"]] and entry2["rule_score"] == 0.9


# -------------------------------------------------------------------------- current offer (GP6(d)), offer guard, group display

def test_rejecting_the_current_apartment_moves_the_pointer_and_blocks_the_offer(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    c = _apt(db, address="7 Other Road")
    ids = _solo_pool(db, 2)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g = _group_of(db, ids[0])
    current = int(g["apartment_id"]); other = ({a["id"], c["id"]} - {current}).pop()
    version_before = row(db.execute("SELECT COUNT(*) AS n FROM group_membership_versions WHERE group_id=?", (g["id"],)))["n"]
    rec = ar.record(db, ids[0], "apartment", current, STAFF, group_id=g["id"])
    assert rec["basis_kind"] == "group_option" and rec["offered_basis"]["option"]["total_weekly"] is not None and rec["groups_repointed"] == [g["id"]]
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert int(g2["apartment_id"]) == other                                    # GP6(d): next best listed apartment nobody excludes
    assert set(_listed(db, g["id"])) == {a["id"], c["id"]}                      # listed options stay as history / display
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_membership_versions WHERE group_id=?", (g["id"],)))["n"] == version_before
    with pytest.raises(ar.OfferRefused) as ei:
        matching.offer_apartment(db, g["id"], current, STAFF)                   # fail-closed guard at the offer (inc3: 409 class)
    assert "declined" in str(ei.value) and "reopens" in str(ei.value)
    matching.offer_apartment(db, g["id"], other, STAFF)                         # the other one is still offerable
    declined = ar.declined_in_group(db, g["id"])
    assert list(declined) == [current] and declined[current][0]["scope"] == "apartment" and declined[current][0]["at"] == rec["recorded_at"]
    # now the other one too: no listed apartment is left → held with no current offer
    ar.record(db, ids[0], "apartment", other, STAFF, group_id=g["id"])
    assert row(db.execute("SELECT apartment_id FROM groups WHERE id=?", (g["id"],)))["apartment_id"] is None
    assert [r["action"] for r in rows(db.execute("SELECT action FROM audit_log WHERE action='group_offer_moved_after_rejection'"))] == ["group_offer_moved_after_rejection"] * 2


# ------------------------------------------------------------------------------------------ reconsideration, allowance, cascade

def test_reopen_material_change_and_reoffer_permission(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    b = _apt_same_building(db, "Unit 2 / 1 Neutral Street")
    e1, = _solo_pool(db, 1)
    rec = ar.record(db, e1, "building", a["id"], STAFF)
    diff = ar.material_change(db, rec["id"], a["id"])
    assert diff["any"] is False and diff["changes"] == {} and diff["enquiry_changes"] == {}
    with pytest.raises(ar.RejectionError):
        ar.allow_reoffer(db, rec["id"], a["id"], STAFF)                        # nothing material changed: no re-offer
    with pytest.raises(ar.RejectionError):
        ar.material_change(db, rec["id"], _apt(db, address="7 Other Road")["id"])   # not this building
    db.execute("UPDATE apartments SET rent_12m=990.0 WHERE id=?", (a["id"],)); db.commit()
    diff = ar.material_change(db, rec["id"], a["id"])
    assert diff["any"] and diff["changes"] == {"rent_12m": [1150.0, 990.0]}
    rec2 = ar.allow_reoffer(db, rec["id"], a["id"], STAFF, note="rent came down")
    assert rec2["status"] == "active" and rec2["reoffer_permitted_apartments"] == [a["id"]]
    ex = ar.exclusions_for(db, e1)
    assert ex["buildings"] == [matching.building_identity(a)] and ex["allowed"] == [a["id"]] and ex["records"][0]["allowed"] == [a["id"]]
    u = matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (e1,))), [])
    assert matching._unit_fits_apartment(matching.apartment_from_row(a), u)      # exactly that unit may be offered again...
    assert not matching._unit_fits_apartment(matching.apartment_from_row(b), u)  # ...the rest of the building stays out
    # nothing is restored by the permission itself: only a run can list it again (pinned on a REAL group with cached rows in
    # test_permit_on_cached_current_row_purges_it_moves_the_pointer_and_refuses_until_a_run / test_customer_reopen_of_a_building_... below)
    with pytest.raises(ar.RejectionError):
        ar.allow_reoffer(db, rec["id"], a["id"], STAFF)                        # permission is recorded once
    # enquiry-side material change counts too
    rec3 = ar.record(db, e1, "apartment", b["id"], STAFF)
    db.execute("UPDATE enquiries SET budget_option='changed-option' WHERE id=?", (e1,)); db.commit()
    assert ar.material_change(db, rec3["id"])["enquiry_changes"].get("budget_option")
    # customer reopens: exclusion lifts, history kept
    r = ar.reopen_by_customer(db, rec["id"], STAFF, note="customer called back")
    assert r["status"] == "reopened" and r["reopen_route"] == "customer" and [e["event"] for e in r["events"]] == ["recorded", "material_change_reoffer", "reopened_by_customer"]
    assert ar.exclusions_for(db, e1)["buildings"] == []
    with pytest.raises(ar.RejectionError):
        ar.reopen_by_customer(db, rec["id"], STAFF)
    # basis never leaks names or AI text: apartment material fields + enquiry requirement fields only
    assert set(rec["offered_basis"]) == {"apartment", "enquiry", "taken_at"} and "first_name" not in rec["offered_basis"]["enquiry"]


def test_enquiry_deletion_removes_its_records(db):
    a = _apt(db)
    e1, = _solo_pool(db, 1)
    rec = ar.record(db, e1, "apartment", a["id"], STAFF)
    db.execute("DELETE FROM enquiries WHERE id=?", (e1,)); db.commit()
    assert db.execute("SELECT COUNT(*) FROM apartment_rejections").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM apartment_rejection_events").fetchone()[0] == 0
    with pytest.raises(ar.RejectionError):
        ar.get(db, rec["id"])


# ------------------------------------------------------------------------------------------------------------------- routes

def test_routes_record_list_diff_reoffer_reopen(client):
    c = appdb(client)
    a = _apt(c, address="Unit 1 / 1 Neutral Street")
    e = _enq(c, "Routa", "routa@example.invalid")
    r = client.post(f"/api/enquiries/{e['id']}/apartment-rejections", json={"scope": "apartment", "apartment_id": a["id"], "customer_reason": "x" * 600})
    assert r.status_code == 201 and r.json()["status"] == "active" and len(r.json()["customer_reason"]) == ar.MAX_REASON
    rid = r.json()["id"]
    assert client.post(f"/api/enquiries/{e['id']}/apartment-rejections", json={"scope": "apartment", "apartment_id": a["id"]}).status_code == 409
    assert client.post(f"/api/enquiries/{e['id']}/apartment-rejections", json={"scope": "apartment"}).status_code == 422
    assert client.post(f"/api/enquiries/{e['id']}/apartment-rejections", json={"scope": "apartment", "apartment_id": 999999}).status_code == 404
    lst = client.get(f"/api/enquiries/{e['id']}/apartment-rejections")
    assert lst.status_code == 200 and [x["id"] for x in lst.json()["items"]] == [rid] and lst.json()["exclusions"]["apartments"] == [a["id"]]
    assert client.get("/api/enquiries/999999/apartment-rejections").status_code == 404
    d = client.get(f"/api/apartment-rejections/{rid}/material-change")
    assert d.status_code == 200 and d.json()["any"] is False
    assert client.post(f"/api/apartment-rejections/{rid}/reoffer", json={"apartment_id": a["id"]}).status_code == 409
    c.execute("UPDATE apartments SET available_from='2027-03-01' WHERE id=?", (a["id"],)); c.commit()
    ok = client.post(f"/api/apartment-rejections/{rid}/reoffer", json={"apartment_id": a["id"], "note": "later availability"})
    assert ok.status_code == 200 and ok.json()["reoffer_permitted_apartments"] == [a["id"]]
    ro = client.post(f"/api/apartment-rejections/{rid}/reopen", json={"note": "customer reopened"})
    assert ro.status_code == 200 and ro.json()["status"] == "reopened"
    assert client.post("/api/apartment-rejections/999999/reopen", json={}).status_code == 404
    assert client.get(f"/api/enquiries/{e['id']}").status_code == 200
    anon = TestClient(client.app)
    assert anon.get(f"/api/enquiries/{e['id']}/apartment-rejections").status_code == 401


def test_routes_offered_apartments_are_unique_ordered_and_enquiry_scoped(client):
    """Read-route fixture only: overlapping group options retain their six-field API shape on both backends."""
    c = appdb(client)
    apartments = [_apt(c, address=f"Unit {i} / 2 Offered Street") for i in (1, 2)]
    enquiry = _enq(c, "Offered", "offered@example.invalid")
    other = _enq(c, "Other", "other-offered@example.invalid")
    stamp = now_iso()

    def add_group(eid, status, current_id):
        c.execute("INSERT INTO groups(apartment_id,status,created_at,updated_at) VALUES (?,?,?,?)",
                  (current_id, status, stamp, stamp))
        gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO group_members(group_id,enquiry_id) VALUES (?,?)", (gid, eid))
        # Deliberately insert rank 2 first: insertion order must not determine the result.
        for rank, apartment in ((2, apartments[0]), (1, apartments[1])):
            c.execute("INSERT INTO group_apartments(group_id,apartment_id,rank) VALUES (?,?,?)",
                      (gid, apartment["id"], rank))
        return gid

    expected = []
    for index, status in enumerate(("Proposed", "Introduced", "Group confirmed", "Awaiting replacement")):
        current_id = apartments[index % 2]["id"]
        gid = add_group(enquiry["id"], status, current_id)
        expected[0:0] = [
            {"apartment_id": apartment["id"], "address": apartment["address"],
             "type_code": apartment["type_code"], "group_id": gid, "group_status": status,
             "is_current": int(apartment["id"] == current_id)}
            for apartment in reversed(apartments)
        ]
    # Newer rows that the route must exclude, despite sharing the same apartments.
    add_group(other["id"], "Proposed", apartments[0]["id"])
    for status in ("Leased", "Dissolved"):
        add_group(enquiry["id"], status, apartments[0]["id"])
    c.commit()

    response = client.get(f"/api/enquiries/{enquiry['id']}/apartment-rejections")
    assert response.status_code == 200, response.text
    offered = response.json()["offered"]
    assert offered == expected and len(offered) == 8
    assert len({(item["group_id"], item["apartment_id"]) for item in offered}) == len(offered)
    assert all(set(item) == {"apartment_id", "address", "type_code", "group_id", "group_status", "is_current"}
               for item in offered)


# ---------------------------------------------------------------- inc3: ONE eligibility lens on a REAL live group (Neutral C4/P1-P6)

def _only_this_stock(db, keep_ids):
    """Fixture isolation: the app's bootstrap inventory (present in the per-test `client` database) is out of these tests' scope.
    Every other apartment is set 'not yet available' (a normal stock state the generator skips) so the exact two-option assertions
    below describe the whole eligible stock. Setup only - nothing under test runs here."""
    db.execute("UPDATE apartments SET available_from=NULL WHERE id NOT IN (%s)" % ",".join("?" * len(keep_ids)), tuple(int(i) for i in keep_ids))
    db.commit()


def _real_group(db, monkeypatch, second_address="7 Other Road"):
    """Two listed apartments (different buildings) for one real primary group out of a full run, then an approved review via the
    deterministic AI double (no provider call). Returns (apartments by id, member enquiry ids, fresh group row)."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    c = _apt(db, address=second_address)
    _only_this_stock(db, (a["id"], c["id"]))
    ids = _solo_pool(db, 2)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = _group_of(db, ids[0])
    assert g is not None and set(_listed(db, g["id"])) == {a["id"], c["id"]}, "the fixture needs a real group with BOTH apartments listed"
    return {a["id"]: a, c["id"]: c}, ids, g


def _g(db, gid):
    return row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))


def _approve(db, monkeypatch, gid):
    """Fresh approved review for the group as it is NOW (deterministic double). The group must already be pending: the writer
    that changed its option set (record / purge) is what queues it - no test-only enqueue, so a missing requeue fails here."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    assert _g(db, gid)["ai_status"] == "pending", "the normal writer must have queued the group"
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = _g(db, gid)
    assert g["ai_status"] == "ok" and g["review_verdict"] == "approve", (g["ai_status"], g["ai_error"])
    return g


def test_one_lens_active_decline_leaves_packet_readiness_and_evidence_on_the_same_effective_set(db, monkeypatch):
    """Neutral C4: while a record is ACTIVE its declined row is history only - the AI packet, readiness, the evidence hash and the
    offer path all work on ONE effective option set (matching.eligible_offers). The evidence is the effective set only: the tail
    of this test is the PURE evidence-function property (an excluded row's own status is not evidence); the real lease lifecycle
    is exercised in test_normal_lease_of_a_declined_history_row_* below."""
    from server import ai_queue
    apts, ids, g = _real_group(db, monkeypatch)
    assert ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    current = int(g["apartment_id"]); other = ({*apts} - {current}).pop()
    assert {int(x["apartment_id"]) for x in matching.eligible_offers(db, g["id"])} == {current, other}
    hash_before, snap_before = matching.group_evidence(db, g["id"])
    assert "excluded_apartments" not in snap_before

    ar.record(db, ids[0], "apartment", current, STAFF, group_id=g["id"])
    g = _g(db, g["id"])
    assert int(g["apartment_id"]) == other and set(_listed(db, g["id"])) == {current, other}   # pointer moved, row kept as history
    # the lens: one helper, one capture, one answer
    assert [int(x["apartment_id"]) for x in matching.eligible_offers(db, g["id"])] == [other]
    assert matching.excluded_option_ids(matching.evidence_rows(db, g["id"])) == {current}
    # AI packet (same lens): the declined apartment is not an option the reviewer sees
    # _payload also refreshes Proposed option/rank rows; production calls it inside its capture guard.
    with retention_guard(db, label="test packet capture"):
        packet = ai_queue._payload(db, g)
    assert not db.in_transaction
    assert [x["apartment_id"] for x in packet["apartments"]] == [other]
    # evidence hash moved because the EFFECTIVE offered set changed (no separate "excluded" fact: root, 11 Nov 2026); the earlier
    # review is therefore non-current
    hash_after, snap_after = matching.group_evidence(db, g["id"])
    assert hash_after != hash_before and "excluded_apartments" not in snap_after
    assert [a["apartment_id"] for a in snap_after["apartments"]] == [other]
    assert not ai_queue.evidence_currency(db, g)["current"]
    # offer path: refused with the GP14 sentence, through the same lens
    with pytest.raises(ar.OfferRefused):
        matching.offer_apartment(db, g["id"], current, STAFF)
    # fresh review on the effective set -> ready again
    g = _approve(db, monkeypatch, g["id"])
    assert ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    assert ai_queue.evidence_currency(db, g)["current"]
    # pure evidence-function property (raw column change, NO lifecycle): the excluded row's own status is not evidence, and the
    # evidence does not depend on the cached history row being listed at all
    hash_ready = matching.group_evidence(db, g["id"])[0]
    db.execute("UPDATE apartments SET leased_at=?, leased_by='someone else' WHERE id=?", (now_iso(), current)); db.commit()
    assert matching.group_evidence(db, g["id"])[0] == hash_ready
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], current)); db.commit()
    assert matching.group_evidence(db, g["id"])[0] == hash_ready
    g = _g(db, g["id"])
    assert int(g["apartment_id"]) == other and ai_queue.is_ready(db, g)["ready"] and ai_queue.evidence_currency(db, g)["current"]


def test_permit_on_cached_current_row_purges_it_moves_the_pointer_and_refuses_until_a_run(client, monkeypatch):
    """Root/Neutral P2 precision (legacy / inc2b state): the group's CURRENT apartment is a cached row covered by a record (the
    record was taken without the pointer moving), staff then permit a re-offer after a material change, no run. The permit must
    purge that row too - never exempt for being current - move the pointer in the same transaction, make the stored review and
    briefs non-current and requeue; offer_apartment answers the fixed PENDING_VALIDATION 409 on BOTH routes until a run relists it.
    The browser reproduction behind this test: enquiry 155 / group 101 / apartment 54 offered successfully without a run."""
    from server import ai_queue
    db = appdb(client)
    apts, ids, g = _real_group(db, monkeypatch)
    gid = g["id"]; current = int(g["apartment_id"]); other = ({*apts} - {current}).pop()
    # legacy state: a record covering the current apartment taken WITHOUT group context, pointer left on it (as inc2b data could be)
    rec = ar.record(db, ids[0], "apartment", current, STAFF)
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (current, gid)); db.commit()
    assert int(_g(db, gid)["apartment_id"]) == current and current in _listed(db, gid)
    db.execute("UPDATE apartments SET rent_12m=990.0 WHERE id=?", (current,)); db.commit()        # material change
    queued_before = db.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_options_purged_after_lift'").fetchone()[0]
    ar.allow_reoffer(db, rec["id"], current, STAFF, note="rent came down")
    # purge: the covered row is gone although it was current; the pointer moved to the remaining eligible row in the same write
    assert _listed(db, gid) == [other]
    g = _g(db, gid)
    assert int(g["apartment_id"]) == other and g["ai_status"] == "pending"
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_options_purged_after_lift'").fetchone()[0] == queued_before + 1
    assert any(r["action"] == "group_offer_moved_after_rejection" and "pending validation" in (r["detail"] or "")
               for r in rows(db.execute("SELECT action, detail FROM audit_log WHERE entity='group' AND entity_id=?", (str(gid),))))
    assert not ai_queue.is_ready(db, g)["ready"] and not ai_queue.evidence_currency(db, g)["current"]
    # PENDING_VALIDATION on the library call and on BOTH HTTP routes, fixed sentence
    with pytest.raises(ar.OfferRefused, match="not currently on the group's validated list"):
        matching.offer_apartment(db, gid, current, STAFF)
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": current})
    assert r.status_code == 409 and r.json()["detail"] == ar.PENDING_VALIDATION, r.text
    r = client.post(f"/api/apartments/{current}/lease", json={"group_id": gid})
    assert r.status_code == 409 and r.json()["detail"] == ar.PENDING_VALIDATION, r.text
    st = _g(db, gid)
    assert st["status"] == "Proposed" and int(st["apartment_id"]) == other and _listed(db, gid) == [other]   # nothing half-applied
    assert row(db.execute("SELECT leased_at FROM apartments WHERE id=?", (current,)))["leased_at"] is None
    # an apartment never covered by any record keeps the ordinary 422 wording
    assert client.patch(f"/api/groups/{gid}", json={"apartment_id": 999999}).status_code == 422
    # a run relists the permitted unit through normal generation + rules; then it is offerable again
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g = _group_of(db, ids[0])
    assert g is not None and current in _listed(db, g["id"])
    matching.offer_apartment(db, g["id"], current, STAFF)
    assert int(_g(db, g["id"])["apartment_id"]) == current


def test_customer_reopen_of_a_building_record_purges_every_covered_unit_but_nothing_else(db, monkeypatch):
    """Approved purge scope: a building-scope CUSTOMER reopening lifts ALL covered units, so every covered row in the live groups
    containing the enquiry goes; rows another member's / another record's active exclusion still blocks stay (history, ineligible
    as before); other groups and the group's own uncovered row are untouched; the review/brief go non-current and the group is
    requeued once."""
    from server import ai_queue
    a1 = _apt(db, address="Unit 1 / 1 Neutral Street")
    a2 = _apt_same_building(db, "Unit 2 / 1 Neutral Street", rent12=1140.0)
    c = _apt(db, address="7 Other Road")
    ids = _solo_pool(db, 4)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g = _group_of(db, ids[0])
    assert g is not None and set(_listed(db, g["id"])) == {a1["id"], a2["id"], c["id"]}
    others = rows(db.execute("SELECT id FROM groups WHERE status='Proposed' AND id NOT IN (SELECT group_id FROM group_members WHERE enquiry_id=?)", (ids[0],)))
    assert others, "the fixture needs an unrelated live group"
    members = [m["enquiry_id"] for m in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
    mate = next(m for m in members if m != ids[0]) if ids[0] in members else None
    assert mate is not None
    g = _approve(db, monkeypatch, g["id"])
    # the enquirer declines the BUILDING; the other member independently declines unit 2 as an apartment
    rec_b = ar.record(db, ids[0], "building", a1["id"], STAFF, group_id=g["id"])
    rec_u = ar.record(db, mate, "apartment", a2["id"], STAFF, group_id=g["id"])
    g = _g(db, g["id"])
    assert int(g["apartment_id"]) == c["id"] and set(_listed(db, g["id"])) == {a1["id"], a2["id"], c["id"]}
    assert [int(x["apartment_id"]) for x in matching.eligible_offers(db, g["id"])] == [c["id"]]
    g = _approve(db, monkeypatch, g["id"])
    hash_before = g["evidence_hash"]
    # run 2 (11 Nov 2026): the unrelated-group baseline is taken HERE, after the declines and the worker passes they queued (the
    # mate's decline legitimately re-reviewed and re-ranked other groups containing the mate); only the lift itself is measured
    other_lists = {o["id"]: _listed(db, o["id"]) for o in others}
    # customer reopens the building record: unit 1 is lifted and purged; unit 2 stays (the mate's record still blocks it); c untouched
    r = ar.reopen_by_customer(db, rec_b["id"], STAFF, note="customer called back")
    assert r["status"] == "reopened"
    assert set(_listed(db, g["id"])) == {a2["id"], c["id"]}
    g = _g(db, g["id"])
    assert int(g["apartment_id"]) == c["id"] and g["ai_status"] == "pending" and g["evidence_hash"] is None and g["review_verdict"] is None
    # effective set still [c] (unit 1 purged, unit 2 still blocked): the evidence hash is EQUAL; non-currency is the explicit enqueue
    assert hash_before and matching.group_evidence(db, g["id"])[0] == hash_before
    assert not ai_queue.is_ready(db, g)["ready"] and not ai_queue.evidence_currency(db, g)["current"]
    assert (g["rerank_note"], g["flag_kind"]) == (None, None)                                                 # a lift is not a loss
    assert [int(x["apartment_id"]) for x in matching.eligible_offers(db, g["id"])] == [c["id"]]            # unit 2 still ineligible
    with pytest.raises(ar.OfferRefused, match="not currently on the group's validated list"):
        matching.offer_apartment(db, g["id"], a1["id"], STAFF)                                                # lifted, pending the run
    with pytest.raises(ar.OfferRefused, match="declined"):
        matching.offer_apartment(db, g["id"], a2["id"], STAFF)                                                # still blocked by the mate
    assert {o["id"]: _listed(db, o["id"]) for o in others} == other_lists                                    # unrelated groups unchanged
    purged = rows(db.execute("SELECT entity_id FROM audit_log WHERE action='group_options_purged_after_lift'"))
    mine = {str(r["group_id"]) for r in rows(db.execute("SELECT group_id FROM group_members WHERE enquiry_id=?", (ids[0],)))}
    assert str(g["id"]) in {r["entity_id"] for r in purged} and {r["entity_id"] for r in purged} <= mine      # only groups containing the enquiry
    # the mate's record is unaffected by the enquirer's reopening
    assert ar.get(db, rec_u["id"])["status"] == "active"


def test_decline_requeues_the_affected_ready_group_so_the_normal_worker_pass_reviews_it(db, monkeypatch):
    """Root queue-gap trace (11 Nov 2026): record() changes the effective offer set but nothing re-queued the group, so an idle
    approved group stayed non-current indefinitely. Now the same guarded write queues every live group containing the enquiry
    whose evidence hash moved; the ordinary process_pending pass then reviews it. Unrelated groups, groups whose evidence did not
    move, membership versions and response clocks are untouched. No test-only enqueue anywhere here."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    c = _apt(db, address="7 Other Road")
    third = _apt(db, address="11 Third Street")
    unrelated_apt = _apt(db, address="9 Elsewhere Lane")
    db.execute("UPDATE apartments SET leased_at=?, leased_by='someone else' WHERE id=?", (now_iso(), unrelated_apt["id"])); db.commit()   # never listed
    ids = _solo_pool(db, 4)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = _group_of(db, ids[0])
    assert g is not None and {a["id"], c["id"], third["id"]} <= set(_listed(db, g["id"])) and ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    others = rows(db.execute("SELECT id, ai_status, evidence_hash, updated_at FROM groups WHERE status='Proposed' AND id NOT IN (SELECT group_id FROM group_members WHERE enquiry_id=?) ORDER BY id", (ids[0],)))
    assert others and all(o["ai_status"] == "ok" for o in others)
    versions = db.execute("SELECT COUNT(*) FROM group_membership_versions").fetchone()[0]
    responses = db.execute("SELECT COUNT(*) FROM group_responses").fetchone()[0]
    current = int(g["apartment_id"]); non_current = next(x for x in _listed(db, g["id"]) if x != current)

    # (a) a record that touches none of this group's listed apartments: evidence unchanged -> nothing queued
    assert unrelated_apt["id"] not in _listed(db, g["id"])
    rec0 = ar.record(db, ids[0], "apartment", unrelated_apt["id"], STAFF)
    assert rec0["groups_requeued"] == [] and rec0["groups_repointed"] == [] and _g(db, g["id"])["ai_status"] == "ok"

    # (b) decline a NON-current option: pointer stays, the group is queued and the normal pass reviews it again
    rec = ar.record(db, ids[0], "apartment", non_current, STAFF, group_id=g["id"])
    assert g["id"] in rec["groups_requeued"] and rec["groups_repointed"] == []
    g2 = _g(db, g["id"])
    assert g2["ai_status"] == "pending" and int(g2["apartment_id"]) == current and g2["evidence_hash"] is None
    assert not ai_queue.is_ready(db, g2)["ready"]
    assert ai_queue.process_pending(db)["ok"] >= 1
    g3 = _g(db, g["id"])
    assert g3["ai_status"] == "ok" and ai_queue.is_ready(db, g3)["ready"], ai_queue.is_ready(db, g3)["reasons"]
    assert [x["apartment_id"] for x in ai_queue._payload(db, g3)["apartments"]] == [x for x in _listed(db, g["id"]) if x != non_current]

    # (c) decline the CURRENT option: pointer moves AND the group is queued in the same write; the normal pass reviews it again.
    # The affected set is computed from the PRE-write state: every live group containing the enquiry whose current is the declined
    # apartment (alternates included - record() repoints by membership, not by primacy); every other live group is untouched.
    live = rows(db.execute("SELECT id, apartment_id, ai_status, evidence_hash FROM groups WHERE status IN (%s) ORDER BY id" % ",".join("?" * len(ar.LIVE_GROUP_STATUSES)), tuple(ar.LIVE_GROUP_STATUSES)))
    with_member = {int(r["id"]) for r in live if ids[0] in _members_of(db, r["id"])}
    expected = sorted(int(r["id"]) for r in live if int(r["id"]) in with_member and r["apartment_id"] is not None and int(r["apartment_id"]) == current)
    before = {int(r["id"]): (r["apartment_id"], r["ai_status"], r["evidence_hash"]) for r in live if int(r["id"]) not in with_member}
    rec2 = ar.record(db, ids[0], "apartment", current, STAFF, group_id=g["id"])
    assert g["id"] in expected and sorted(rec2["groups_repointed"]) == expected and g["id"] in rec2["groups_requeued"]
    for gid in expected:
        gx = _g(db, gid)
        assert gx["ai_status"] == "pending" and (gx["apartment_id"] is None or int(gx["apartment_id"]) not in (current, non_current))
    for gid, state in before.items():
        gx = _g(db, gid)
        assert (gx["apartment_id"], gx["ai_status"], gx["evidence_hash"]) == state, f"unrelated group {gid} changed"
    g4 = _g(db, g["id"])
    assert g4["apartment_id"] is not None and int(g4["apartment_id"]) not in (current, non_current)
    assert ai_queue.process_pending(db)["ok"] >= 1
    g5 = _g(db, g["id"])
    assert g5["ai_status"] == "ok" and ai_queue.is_ready(db, g5)["ready"], ai_queue.is_ready(db, g5)["reasons"]
    assert set(_listed(db, g["id"])) >= {current, non_current}                                   # history rows kept
    # audit trail names the queueing
    assert rows(db.execute("SELECT 1 FROM audit_log WHERE action='group_review_requeued_after_rejection' AND entity_id=?", (str(g["id"]),)))

    # unrelated groups, membership versions and response clocks: untouched
    assert rows(db.execute("SELECT id, ai_status, evidence_hash, updated_at FROM groups WHERE id IN (%s) ORDER BY id" % ",".join(str(o["id"]) for o in others))) == others
    assert db.execute("SELECT COUNT(*) FROM group_membership_versions").fetchone()[0] == versions
    assert db.execute("SELECT COUNT(*) FROM group_responses").fetchone()[0] == responses


def test_lift_with_nothing_cached_purges_nothing_and_pending_wording_does_not_promise_reinstatement(db):
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    e1, = _solo_pool(db, 1)
    rec = ar.record(db, e1, "apartment", a["id"], STAFF)
    assert ar._purge_lifted_rows(db, e1, {a["id"]}, STAFF, "probe") == []
    assert ar.reopen_by_customer(db, rec["id"], STAFF)["status"] == "reopened"
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_options_purged_after_lift'").fetchone()[0] == 0
    assert "next matching run decides" in ar.PENDING_VALIDATION and "cannot be offered before that run" in ar.PENDING_VALIDATION
    assert "will be offered" not in ar.PENDING_VALIDATION and "reinstat" not in ar.PENDING_VALIDATION.lower()


# -------------------------------------------------------------------------------------- staff page (static source checks)

def test_staff_page_card_and_group_marker_are_wired_and_escaped():
    import pathlib
    import re
    app_dir = pathlib.Path(__file__).resolve().parents[1]
    js = (app_dir / "client" / "staff.js").read_text(encoding="utf-8")
    css = (app_dir / "client" / "styles.css").read_text(encoding="utf-8")
    m = re.search(r"async function matebotRejections\(box, id\) \{(.*?)\n  \}\n", js, re.S)
    assert m, "the enquiry-page card renderer is missing"
    card = m.group(1)
    for ep in ("/api/enquiries/${id}/apartment-rejections", "/api/apartment-rejections/${b.dataset.rid}/reopen",
               "/api/apartment-rejections/${b.dataset.rid}/material-change?apartment_id=", "/api/apartment-rejections/${b.dataset.rid}/reoffer"):
        assert ep in card
    # everything printed from the server or the person's own words is escaped
    for raw in ("esc(r.address", "esc(r.recorded_by)", "esc(r.customer_reason)", "esc(ev.staff)", "esc(ev.note)", "esc(o.address)"):
        assert raw in card
    assert "innerHTML = `<h3>Apartments this person declined</h3>" in card and 'data-role="mb-rejections"' in js
    assert "matebotRejections(p.querySelector('[data-role=\"mb-rejections\"]'), id)" in js, "the enquiry page must start the card"
    assert "pending the next matching run" in card and "Nothing is offered yet" in card    # re-offer is permission only, never a restore
    # the group page shows "declined by <name> on <date>" and never shows an Offer button for a declined apartment
    assert "declined by ${esc(d.name)} on ${fmtD(d.at)}" in js
    assert "opts.offer && !cur && !declined &&" in js
    assert "declined_apartments" in js
    # the card's long text wraps instead of widening the page (root browser QA: 375px, no horizontal overflow)
    assert re.search(r"\.rej-item \{[^}]*overflow-wrap: anywhere;[^}]*min-width: 0;", css)


# ------------------------------------------------------------------- normal stock lifecycle and a declined history row (root, 11 Nov 2026)

def _five_groups(db, monkeypatch):
    """Two listed apartments and ten solos -> five real primary groups, each listing both apartments; every pointer on `x`.
    Returns (x, other, [group ids])."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    c = _apt(db, address="7 Other Road")
    _only_this_stock(db, (a["id"], c["id"]))
    _solo_pool(db, 10)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    gids = sorted({r["group_id"] for r in _primaries(db)})
    assert len(gids) == 5, gids
    for gid in gids:
        assert set(_listed(db, gid)) == {a["id"], c["id"]}, "the fixture needs both apartments listed for every group"
    x = int(_g(db, gids[0])["apartment_id"]); other = ({a["id"], c["id"]} - {x}).pop()
    for gid in gids:
        if int(_g(db, gid)["apartment_id"]) != x:
            matching.offer_apartment(db, gid, x, STAFF)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    return x, other, gids


def _members_of(db, gid):
    return [m["enquiry_id"] for m in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))]


def _alerts_for(db, gid):
    from server import stock_lifecycle as sl
    return rows(db.execute("SELECT id FROM internal_alert_outbox WHERE kind=? AND record_id=?", (sl.EV_APARTMENT_UNAVAILABLE, gid)))


def _work_for(db, gid):
    return rows(db.execute("SELECT status, outcome FROM offer_reconcile_work WHERE group_id=? ORDER BY id", (gid,)))


def test_normal_lease_of_a_declined_history_row_prunes_it_silently_while_effective_losses_keep_gp8_handling(client, monkeypatch):
    """Root/Neutral (11 Nov 2026), real lifecycle - not a raw column change. Apartment x is leased through the product route and
    its GP8 follow-up runs. G: x is a declined history row (alternative) -> pruned without a verdict: no note, flag, alert or
    refresh, work closed 'unchanged', evidence hash unchanged, review current, group ready. H: x an EFFECTIVE alternative -> trimmed
    with the existing GP8 note/flag/alert/refresh. K: x the EFFECTIVE current offer -> moved to the other option. K2: x current and
    the other option declined -> held with NO offer (a declined row is never a fallback) and the declined row pruned. L (legacy):
    current pointer left on the declined x -> judged, invalidated and moved like any current offer, not exempt."""
    from server import ai_queue
    db = appdb(client)
    x, other, gids = _five_groups(db, monkeypatch)
    G, H, K, K2, L = gids
    # G: a member declines x (group context) -> pointer moves to `other`, x stays cached as a declined row
    ar.record(db, _members_of(db, G)[0], "apartment", x, STAFF, group_id=G)
    assert int(_g(db, G)["apartment_id"]) == other and set(_listed(db, G)) == {x, other}
    # K2: the OTHER option is declined; x stays current (it is not excluded)
    ar.record(db, _members_of(db, K2)[0], "apartment", other, STAFF, group_id=K2)
    assert int(_g(db, K2)["apartment_id"]) == x
    # L: legacy state - x declined without group context (the pointer is set below, after the worker pass)
    ar.record(db, _members_of(db, L)[0], "apartment", x, STAFF)
    # fresh approved reviews for everything the writers queued (G, K2 by record; the rest by the run), through the normal queue.
    # Run 2 (11 Nov 2026): the worker pass comes FIRST - an approved review of a Proposed group follows the AI's #1 and re-ranks, so
    # any pointer set before it would be rewritten (H healed back to x, L healed to `other`); the states below are set after it.
    ai_queue.process_pending(db)
    for gid in gids:
        g = _g(db, gid)
        assert g["ai_status"] == "ok" and g["review_verdict"] == "approve", (gid, g["ai_status"], g["ai_error"])
    # H: x is an effective alternative (pointer on `other` through the normal offer path; no review change)
    matching.offer_apartment(db, H, other, STAFF)
    # L: legacy state - pointer left on the declined x (as inc2b data could be); explicitly labelled legacy-state setup, not a product path
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (x, L)); db.commit()
    # the intended pre-lease states, asserted immediately before the lease (cached rows AND effective sets)
    assert int(_g(db, G)["apartment_id"]) == other and set(_listed(db, G)) == {x, other} and [int(o["apartment_id"]) for o in matching.eligible_offers(db, G)] == [other]
    assert int(_g(db, H)["apartment_id"]) == other and set(_listed(db, H)) == {x, other} and {int(o["apartment_id"]) for o in matching.eligible_offers(db, H)} == {x, other}
    assert int(_g(db, K)["apartment_id"]) == x and set(_listed(db, K)) == {x, other} and {int(o["apartment_id"]) for o in matching.eligible_offers(db, K)} == {x, other}
    assert int(_g(db, K2)["apartment_id"]) == x and set(_listed(db, K2)) == {x, other} and [int(o["apartment_id"]) for o in matching.eligible_offers(db, K2)] == [x]
    assert int(_g(db, L)["apartment_id"]) == x and set(_listed(db, L)) == {x, other} and [int(o["apartment_id"]) for o in matching.eligible_offers(db, L)] == [other]
    assert all(_g(db, gid)["ai_status"] == "ok" for gid in gids) and all(_work_for(db, gid) == [] for gid in gids)
    g = _g(db, G)
    assert ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    h0 = g["evidence_hash"]
    assert h0 and matching.group_evidence(db, G)[0] == h0
    flags_before = {gid: (_g(db, gid)["flag_kind"], _g(db, gid)["rerank_note"]) for gid in gids}
    assert all(f == (None, None) for f in flags_before.values())
    # the product path: lease x to an outside lessee (no group) - claim + GP8 follow-up of every other group
    r = client.post(f"/api/apartments/{x}/lease", json={})
    assert r.status_code == 200, r.text
    assert row(db.execute("SELECT leased_at FROM apartments WHERE id=?", (x,)))["leased_at"]
    assert all(w["status"] != "pending" for gid in gids for w in _work_for(db, gid)), "every follow-up step closed"
    # G: history-only prune, nothing lost
    g = _g(db, G)
    assert _listed(db, G) == [other] and int(g["apartment_id"]) == other
    assert (g["flag_kind"], g["rerank_note"]) == (None, None) and g["reranked_at"] is None
    assert _alerts_for(db, G) == [] and not g["ai_refresh"] and g["ai_status"] == "ok"
    assert [w["outcome"] for w in _work_for(db, G)] == ["unchanged"]
    assert g["evidence_hash"] == h0 and matching.group_evidence(db, G)[0] == h0
    assert ai_queue.evidence_currency(db, g)["current"] and ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    # H: effective alternative lost - existing GP8 handling intact
    h = _g(db, H)
    assert _listed(db, H) == [other] and int(h["apartment_id"]) == other
    assert h["flag_kind"] == "lease" and "was leased" in (h["rerank_note"] or "") and _alerts_for(db, H)
    assert [w["outcome"] for w in _work_for(db, H)] == ["list_trimmed"] and h["ai_refresh"] == 1
    # K: effective current lost - moved to the other option
    k = _g(db, K)
    assert int(k["apartment_id"]) == other and _listed(db, K) == [other]
    assert k["flag_kind"] == "lease" and _alerts_for(db, K) and [w["outcome"] for w in _work_for(db, K)] == ["moved"]
    # K2: current lost and the only other row is declined -> held, never pointed at the declined row; the declined row pruned
    k2 = _g(db, K2)
    assert k2["apartment_id"] is None and _listed(db, K2) == []
    assert [w["outcome"] for w in _work_for(db, K2)] == ["no_apartment"] and k2["flag_kind"] == "lease" and _alerts_for(db, K2)
    # L: the legacy current pointer on the declined x is repaired by the normal path (judged + moved), not exempted
    l_ = _g(db, L)
    assert int(l_["apartment_id"]) == other and _listed(db, L) == [other] and [w["outcome"] for w in _work_for(db, L)] == ["moved"]
    # C2: lifting G's record now has nothing cached to purge -> no enqueue, review still current; the apartment is still not
    # offerable (PENDING_VALIDATION) until a run relists it (it is leased, so a run will not)
    rec = ar.records_for(db, _members_of(db, G)[0])[0]
    ar.reopen_by_customer(db, rec["id"], STAFF, note="changed their mind")
    g = _g(db, G)
    assert g["ai_status"] == "ok" and g["evidence_hash"] == h0 and ai_queue.evidence_currency(db, g)["current"]
    with pytest.raises(ar.OfferRefused, match="not currently on the group's validated list"):
        matching.offer_apartment(db, G, x, STAFF)


def test_normal_full_run_drops_a_declined_history_row_without_touching_the_reviewed_groups_evidence(db, monkeypatch):
    """Root (11 Nov 2026): a full run rewrites a retained group's list from the generator (which excludes declined apartments);
    the cached declined row goes, the effective set and hash are unchanged, the approved review stays current and the group ready."""
    from server import ai_queue
    apts, ids, g = _real_group(db, monkeypatch)
    x = int(g["apartment_id"]); other = ({*apts} - {x}).pop()
    ar.record(db, ids[0], "apartment", x, STAFF, group_id=g["id"])
    g = _approve(db, monkeypatch, g["id"])
    h0 = g["evidence_hash"]
    assert set(_listed(db, g["id"])) == {x, other} and ai_queue.is_ready(db, g)["ready"]
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g2 = _g(db, g["id"])
    assert g2 is not None and g2["status"] == g["status"] and _listed(db, g["id"]) == [other] and int(g2["apartment_id"]) == other
    assert g2["evidence_hash"] == h0 and matching.group_evidence(db, g["id"])[0] == h0
    assert g2["ai_status"] == "ok" and ai_queue.evidence_currency(db, g2)["current"]
    assert ai_queue.is_ready(db, g2)["ready"], ai_queue.is_ready(db, g2)["reasons"]
    # C2 after the prune: lifting the record purges nothing and queues nothing; x returns only through a run that relists it
    rec = ar.records_for(db, ids[0])[0]
    ar.reopen_by_customer(db, rec["id"], STAFF, note="call")
    g3 = _g(db, g["id"])
    assert g3["ai_status"] == "ok" and g3["evidence_hash"] == h0
    with pytest.raises(ar.OfferRefused, match="not currently on the group's validated list"):
        matching.offer_apartment(db, g["id"], x, STAFF)
    matching.full_run(db, "tester", dataset="test", use_ai=False)                     # the normal path relists x: the effective set grows
    assert set(_listed(db, g["id"])) == {x, other}
    g4 = _g(db, g["id"])
    assert matching.group_evidence(db, g["id"])[0] != h0 and g4["ai_refresh"] == 1    # evidence moved on the normal path; GP8(b) refresh queued as for any appended option
    assert int(x) in {int(o["apartment_id"]) for o in matching.eligible_offers(db, g["id"])}
    matching.offer_apartment(db, g["id"], x, STAFF)                                   # validated by the run: offerable again


def test_material_change_of_another_listed_option_prunes_the_declined_row_silently_and_refreshes_as_today(db, monkeypatch):
    """Root (11 Nov 2026): a 'changed' stock transition on the OTHER (effective) option: the declined row is pruned without a
    verdict and the only effect recorded is the real option's own (rewritten figures, GP8(b) refresh, hash moves as today)."""
    from server import ai_queue
    from server import stock_lifecycle as sl
    from server.db import retention_guard
    apts, ids, g = _real_group(db, monkeypatch)
    x = int(g["apartment_id"]); other = ({*apts} - {x}).pop()
    ar.record(db, ids[0], "apartment", x, STAFF, group_id=g["id"])
    g = _approve(db, monkeypatch, g["id"])
    h0 = g["evidence_hash"]
    db.execute("UPDATE apartments SET rent_12m=rent_12m-100.0, rent_6m=rent_6m-100.0 WHERE id=?", (other,)); db.commit()
    with retention_guard(db, label="test transition"):
        sl.record_transition(db, other, "changed", STAFF, changed_fields=["rent_12m", "rent_6m"])
    sl.run_pending(db, apartment_id=other)
    g2 = _g(db, g["id"])
    assert _listed(db, g["id"]) == [other] and int(g2["apartment_id"]) == other
    assert [w["outcome"] for w in _work_for(db, g["id"])] == ["refreshed"]
    assert (g2["flag_kind"], g2["rerank_note"]) == (None, None) and _alerts_for(db, g["id"]) == []    # nothing was lost
    assert g2["ai_refresh"] == 1 and matching.group_evidence(db, g["id"])[0] != h0                  # the real option's figures moved


def test_pending_stock_work_for_an_already_pruned_declined_row_closes_without_a_false_loss(db, monkeypatch):
    """Neutral G-SL1 (11 Nov 2026): the stock follow-up of a transition is durable work that may run AFTER another step or a
    normal full run has already pruned a declined (history-only) row. The retry must still decide "history" for that group from the
    members' records (shared lens), not from the cached rows that are gone - no false loss (note / flag / H19 alert / refresh).
    Controls: an EFFECTIVE option under a valid equal-figures 'changed' transition is refreshed as today; an effective option lost to
    a lease whose row is already gone still raises the original-loss handling; a legacy CURRENT pointer on an excluded apartment with
    no row is still judged and moved."""
    from server import ai_queue
    from server import stock_lifecycle as sl
    from server.db import retention_guard
    x, other, gids = _five_groups(db, monkeypatch)
    G, H, K, K2, L = gids
    ar.record(db, _members_of(db, G)[0], "apartment", x, STAFF, group_id=G)        # G: x declined -> pointer other, x row cached (history)
    ai_queue.process_pending(db)
    for gid in gids:
        assert _g(db, gid)["ai_status"] == "ok" and ai_queue.is_ready(db, _g(db, gid))["ready"]
    assert int(_g(db, G)["apartment_id"]) == other and set(_listed(db, G)) == {x, other}
    h0 = _g(db, G)["evidence_hash"]

    # ---- Phase A: a valid 'changed' transition on x (equal figures), captured while G still caches the x row; NOT run yet
    with retention_guard(db, label="test transition"):
        tr = sl.record_transition(db, x, "changed", STAFF, changed_fields=["rent_12m"])
    assert set(tr["group_ids"]) >= {G, H} and sl.group_has_pending_work(db, G) and sl.group_has_pending_work(db, H)
    matching.full_run(db, "tester", dataset="test", use_ai=False)                 # the normal run prunes G's declined row; H keeps x
    assert _listed(db, G) == [other] and set(_listed(db, H)) == {x, other}
    ai_queue.process_pending(db)                                                   # settle whatever the run queued, so the step's own effect is measured
    gA = _g(db, G)
    assert gA["ai_status"] == "ok" and gA["ai_refresh"] == 0 and gA["evidence_hash"] == h0 and _alerts_for(db, G) == []
    sl.run_pending(db, apartment_id=x)                                             # the ordinary retry of the pending work
    gA2, hA2 = _g(db, G), _g(db, H)
    assert [w["outcome"] for w in _work_for(db, G)] == ["unchanged"]              # G: history, nothing lost (fails without the G-SL1 repair)
    assert int(gA2["apartment_id"]) == other and (gA2["rerank_note"], gA2["flag_kind"]) == (None, None) and _alerts_for(db, G) == []
    assert gA2["ai_refresh"] == 0 and gA2["evidence_hash"] == h0 and ai_queue.is_ready(db, gA2)["ready"] and ai_queue.evidence_currency(db, gA2)["current"]
    assert [w["outcome"] for w in _work_for(db, H)] == ["refreshed"]              # H: effective, valid, equal figures -> GP8(b) refresh as today
    assert int(hA2["apartment_id"]) == x and (hA2["rerank_note"], hA2["flag_kind"]) == (None, None) and _alerts_for(db, H) == [] and hA2["ai_refresh"] == 1
    ai_queue.process_pending(db)

    # ---- Phase B: a REAL lease of x whose follow-up is left pending by a scoped fault, then pruned by the normal run, then retried
    ar.record(db, _members_of(db, K2)[0], "apartment", x, STAFF, group_id=K2)      # K2: declined (row cached) -> history case under the lease
    ar.record(db, _members_of(db, L)[0], "apartment", x, STAFF)                     # L: legacy shape (no group), pointer moves by membership
    ai_queue.process_pending(db)
    hK2 = _g(db, K2)["evidence_hash"]
    real = sl.reconcile_work
    def injected(c, work_id, staff=None):
        raise RuntimeError("injected: follow-up left pending")
    monkeypatch.setattr(sl, "reconcile_work", injected)
    res = matching.mark_apartment_leased(db, x, STAFF)                             # the genuine claim + transition capture; every step fails
    assert res["rerank"]["status"] == "partial" and set(res["rerank"]["pending_group_ids"]) >= {H, K, K2, L} and res["affected"] == []
    assert row(db.execute("SELECT leased_at FROM apartments WHERE id=?", (x,)))["leased_at"] == res["leased_at"]
    monkeypatch.setattr(sl, "reconcile_work", real)
    assert all(sl.group_has_pending_work(db, gid) for gid in (H, K, K2, L))
    matching.full_run(db, "tester", dataset="test", use_ai=False)                 # leased = unavailable: every x row is gone, pointers moved by the run
    for gid in (H, K, K2, L):
        assert _listed(db, gid) == [other] and int(_g(db, gid)["apartment_id"]) == other
    ai_queue.process_pending(db)                                                   # settle what the run queued FIRST (an approve follows the AI's #1 and would heal L)
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (x, L)); db.commit()  # L: legacy state (setup only) - current = excluded x, no row
    assert int(_g(db, L)["apartment_id"]) == x and _listed(db, L) == [other] and ar.group_exclusion_reason(db, L, x) is not None
    assert all(sl.group_has_pending_work(db, gid) for gid in (H, K, K2, L))        # the lease follow-up is still the pending work being retried
    alerts_before = {gid: len(_alerts_for(db, gid)) for gid in gids}
    sl.run_pending(db, apartment_id=x)                                             # the ordinary retry
    gK, gK2, gL, gG = _g(db, K), _g(db, K2), _g(db, L), _g(db, G)
    assert gK["flag_kind"] == "lease" and "was leased" in (gK["rerank_note"] or "") and len(_alerts_for(db, K)) == alerts_before[K] + 1   # K: original loss, row already gone
    assert [w["outcome"] for w in _work_for(db, K2)][-1] == "unchanged"           # K2: history under the lease, row already gone
    assert (gK2["rerank_note"], gK2["flag_kind"]) == (None, None) and len(_alerts_for(db, K2)) == alerts_before[K2] and gK2["evidence_hash"] == hK2
    assert int(gK2["apartment_id"]) == other and ai_queue.is_ready(db, gK2)["ready"]
    assert [w["outcome"] for w in _work_for(db, L)][-1] == "moved" and int(gL["apartment_id"]) == other   # L: current on excluded x still judged
    assert gL["flag_kind"] == "lease" and len(_alerts_for(db, L)) == alerts_before[L] + 1
    assert not sl.group_has_pending_work(db, G) and (gG["rerank_note"], gG["flag_kind"]) == (None, None) and gG["evidence_hash"] == h0
