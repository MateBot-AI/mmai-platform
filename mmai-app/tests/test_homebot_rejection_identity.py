"""GP14 HomeBot rejection identity follow-up (11 Nov 2026; Neutral note H-a / H-b / H-c): one justified building identity for both
bots (street component via homebot_match.building_key + recorded suburb + city), record-scoped re-offer allowances, a duplicate
guard that still lets a customer decline again after a permission, and an explicit legacy path for street-only stored keys derived
from immutable evidence only. On frozen inc2b (bab311d8…) the identity helpers do not exist and allowances are a flat union, so the
tests below fail there. No new business rule: fail-closed code hygiene under GP14/H25."""
from __future__ import annotations

import dataclasses
import json

import pytest

from server import homebot_match as hm, homebot_shortlist as hs, matching
from server.db import now_iso, row, rows
from server.enquiries import ValidationError
from tests.test_homebot import K, body, client, post  # noqa: F401  (fixture + helpers)
from tests.test_homebot_match import TODAY, apt, req
from tests.test_homebot_shortlist import _apt, current, inventory, job, on, run_all, submit  # noqa: F401

CUR = hm.SCENARIO_CURRENT


# ------------------------------------------------------------------------------------------------------ identity (pure)
def test_building_identity_is_the_street_component_plus_recorded_locality_and_both_bots_agree():
    row_ = {"address": "Unit 11 / 35 Darling Street", "suburb": "South Yarra", "city": "Melbourne"}
    assert hm.building_identity(row_["address"], row_["suburb"], row_["city"]) == "35 darling street|south yarra|melbourne"
    assert hm.identity_components(row_["address"], row_["suburb"], row_["city"])[0] == hm.building_key(row_["address"]).lower()
    assert matching.building_identity(row_) == hm.building_identity(row_["address"], row_["suburb"], row_["city"])
    # whitespace / case / punctuation never split one building in two
    assert hm.building_identity("unit 2 /  35  DARLING ST.", " south-yarra ", "MELBOURNE") == hm.building_identity("Unit 1 / 35 Darling St", "South Yarra", "Melbourne")
    # unknown format (no ' / '): taken whole, not merged into the 'Unit n / street' building
    assert hm.building_identity("Unit 11, 35 Darling Street", "South Yarra", "Melbourne") != hm.building_identity("Unit 11 / 35 Darling Street", "South Yarra", "Melbourne")


def test_street_is_required_and_locality_alone_never_identifies_a_building():
    assert hm.building_identity("", "Carlton", "Melbourne") == ""
    assert hm.building_identity(None, "Carlton", "Melbourne") == ""
    assert hm.building_identity("Unit 3 / ", "Carlton", "Melbourne") == ""
    # same street text, another suburb or another city: different buildings
    a = hm.building_identity("Unit 1 / 10 Station Street", "Carlton", "Melbourne")
    assert a != hm.building_identity("Unit 1 / 10 Station Street", "Box Hill", "Melbourne")
    assert a != hm.building_identity("Unit 1 / 10 Station Street", "Carlton", "Sydney")


def test_identity_matching_rules_known_components_bind_unknown_record_components_do_not():
    full = hm.identity_from_key("10 station street|carlton|melbourne")
    assert full == ("10 station street", "carlton", "melbourne")
    cand = hm.identity_components("Unit 4 / 10 Station Street", "Carlton", "Melbourne")
    assert hm.identity_matches(full, cand)
    assert not hm.identity_matches(full, hm.identity_components("Unit 4 / 10 Station Street", "Box Hill", "Melbourne"))
    # a FULL record never widens to a candidate whose suburb/city is missing (no inherited wildcard)
    assert not hm.identity_matches(full, hm.identity_components("Unit 4 / 10 Station Street"))
    assert not hm.identity_matches(full, hm.identity_components("Unit 4 / 10 Station Street", "Carlton"))
    # a LEGACY street-only record keeps exactly the scope it was recorded with: street equality, any locality
    legacy = hm.identity_from_key("10 Station Street")
    assert legacy == ("10 station street", hm.UNKNOWN, hm.UNKNOWN)
    assert hm.identity_matches(legacy, cand) and hm.identity_matches(legacy, hm.identity_components("Unit 9 / 10 Station Street", "Box Hill", "Sydney"))
    assert not hm.identity_matches(legacy, hm.identity_components("Unit 9 / 11 Station Street", "Carlton", "Melbourne"))
    # '' and '||' stored keys never match anything (address-less rows are never one building)
    for key in ("", "||", "|carlton|melbourne"):
        assert not hm.identity_matches(hm.identity_from_key(key), hm.identity_components("", "Carlton", "Melbourne"))
        assert not hm.identity_matches(hm.identity_from_key(key), cand)


def test_evaluate_uses_record_scoped_building_rejections_and_any_applicable_record_excludes():
    bld = hm.identity_components("Unit 9 / 20 Other Street", "Melbourne CBD", "Melbourne")
    # record 1 permits unit 2, record 2 (a later re-decline of the same building) permits nothing: unit 2 stays excluded
    r = req(building_rejections=[(bld, [2]), (bld, [])])
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r, apt(2, building="20 Other Street", city="Melbourne"), TODAY).exclusion_reasons
    # only record 1 active: unit 2 offerable, unit 5 excluded
    r1 = req(building_rejections=[(bld, [2])])
    assert hm.REJECTED_BY_CUSTOMER not in hm.evaluate(r1, apt(2, building="20 Other Street", city="Melbourne"), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r1, apt(5, building="20 Other Street", city="Melbourne"), TODAY).exclusion_reasons
    # same street in another suburb is not this building
    assert hm.REJECTED_BY_CUSTOMER not in hm.evaluate(r1, apt(6, building="20 Other Street", suburb="Box Hill", city="Melbourne"), TODAY).exclusion_reasons
    # a flat allowance in the DISPLAY fields never lifts a record's exclusion
    r2 = dataclasses.replace(r1, reoffer_allowed_ids=frozenset({5}))
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r2, apt(5, building="20 Other Street", city="Melbourne"), TODAY).exclusion_reasons
    # legacy flat kwargs keep their pre-11-Nov meaning (street-only identity, flat allowance) for older callers
    r3 = req(rejected_buildings=["Unit 9 / 20 Other Street"], reoffer_allowed_ids=[2])
    assert r3.building_rejections == ((("20 other street", None, None), frozenset({2})),)
    assert hm.REJECTED_BY_CUSTOMER not in hm.evaluate(r3, apt(2, building="20 Other Street"), TODAY).exclusion_reasons
    assert hm.REJECTED_BY_CUSTOMER in hm.evaluate(r3, apt(5, building="20 Other Street"), TODAY).exclusion_reasons
    # the requirement basis (hash input) carries the per-record shape
    d = r1.as_dict()
    assert d["building_rejections"] == [{"identity": list(bld), "allowed": [2]}]
    json.dumps(d)


# ------------------------------------------------------------------------------------------------ stored records (SQLite)
def _identity_of(on, aid):
    a = row(on.db.execute("SELECT address, suburb, city FROM apartments WHERE id=?", (aid,)))
    return hm.building_identity(a["address"], a["suburb"], a["city"])


def test_building_record_stores_the_full_identity_and_refuses_an_address_less_row(on):
    inventory(on.db)
    _apt(on.db, 7, rent12=900, address="", address_raw="")
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    assert rj["building_key"] == _identity_of(on, 1) == "10 test street|melbourne cbd|melbourne"
    assert rj["building_identity"]["precision"] == hs.PRECISION_FULL and rj["building_identity"]["suburb"] == "melbourne cbd"
    with pytest.raises(ValidationError) as ex:
        hs.record_rejection(on.db, eid, scope="building", apartment_id=7, staff="Anna")
    assert "scope" in ex.value.errors
    ok = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=7, staff="Anna")   # the apartment itself can still be declined
    assert ok["scope"] == "apartment" and ok["building_key"] == ""
    assert hs.active_rejection_records(on.db, eid) == [{"id": rj["id"], "components": ("10 test street", "melbourne cbd", "melbourne"), "allowed": []}]
    assert hs.active_rejections(on.db, eid) == ([7], [rj["building_key"]], [])


def test_same_street_in_another_suburb_is_not_the_declined_building(on):
    inventory(on.db)
    _apt(on.db, 8, rent12=900, building="10 Test Street", suburb="South Yarra")   # same street text, other recorded suburb (inside the form's 'Either' area)
    eid = submit(on)
    run_all(on)
    assert set(json.loads(current(on, eid)["listed_ids"])) >= {1, 2, 8}
    hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    run_all(on)
    listed = set(json.loads(current(on, eid)["listed_ids"]))
    assert 8 in listed and 3 in listed and not ({1, 2} & listed)


def test_permission_is_scoped_to_its_record_and_a_follow_up_decline_is_a_new_record(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    first = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    # duplicate while the record still excludes: refused in plain words
    with pytest.raises(ValidationError):
        hs.record_rejection(on.db, eid, scope="building", apartment_id=2, staff="Anna")
    on.db.execute("UPDATE apartments SET rent_12m=800 WHERE id=2")
    on.db.commit()
    hs.allow_reoffer(on.db, first["id"], 2, "Anna", "Rent came down")
    run_all(on)
    assert 2 in json.loads(current(on, eid)["listed_ids"])
    # the customer looks at unit 2 again and declines the building again: a NEW record, not a duplicate; it blocks unit 2 again
    second = hs.record_rejection(on.db, eid, scope="building", apartment_id=2, staff="Anna", customer_reason="Still no")
    assert second["id"] != first["id"] and second["building_key"] == first["building_key"]
    recs = hs.active_rejection_records(on.db, eid)
    assert [(r["id"], r["allowed"]) for r in recs] == [(first["id"], [2]), (second["id"], [])]
    run_all(on)
    assert not ({1, 2} & set(json.loads(current(on, eid)["listed_ids"])))
    # the first record's allowance is display-only in the flat union and never lifts the second record
    assert hs.active_rejections(on.db, eid)[2] == [2]
    # reopening the second record leaves the first record's permission in force: unit 2 offerable, unit 1 still excluded
    hs.reopen_by_customer(on.db, second["id"], "Anna", "Customer reconsidered the second decline")
    run_all(on)
    listed = set(json.loads(current(on, eid)["listed_ids"]))
    assert 2 in listed and 1 not in listed
    # history untouched: both records keep their rows and events
    assert row(on.db.execute("SELECT status FROM homebot_rejections WHERE id=?", (first["id"],)))["status"] == "active"
    assert [ev["event"] for ev in hs.rejection(on.db, second["id"])["events"]] == ["recorded", "reopened_customer"]


def test_apartment_duplicate_guard_only_while_that_record_is_active(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=3, staff="Anna")
    with pytest.raises(ValidationError):
        hs.record_rejection(on.db, eid, scope="apartment", apartment_id=3, staff="Anna")
    hs.reopen_by_customer(on.db, rj["id"], "Anna", "asked again")
    again = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=3, staff="Anna")
    assert again["id"] != rj["id"]


def _legacy_record(on, eid, aid, key, *, with_snapshot: bool):
    """A pre-11-Nov row: street-only building_key; offered basis with or without the snapshot apartment row. Inserted directly
    (the code no longer writes this shape); nothing else about the row is special."""
    a = row(on.db.execute("SELECT id, address, suburb FROM apartments WHERE id=?", (aid,)))
    basis = {"shortlist_id": None, "scenario": CUR, "requirement_version_id": None, "inventory_hash": None, "candidate": None,
             "apartment": ({"id": a["id"], "address": a["address"], "suburb": a["suburb"], "rent_12m": 900} if with_snapshot else None),
             "snapshot_missing": not with_snapshot}
    on.db.execute("INSERT INTO homebot_rejections(enquiry_id, scope, apartment_id, building_key, offered_basis_json, recorded_by, recorded_at, status) "
                  "VALUES (?,?,?,?,?,?,?,'active')", (eid, "building", aid, key, json.dumps(basis), "Earlier", now_iso()))
    rid = on.db.execute("SELECT last_insert_rowid()").fetchone()[0]
    on.db.execute("INSERT INTO homebot_rejection_events(rejection_id, enquiry_id, event, apartment_id, staff, at) VALUES (?,?,?,?,?,?)",
                  (rid, eid, "recorded", aid, "Earlier", now_iso()))
    on.db.commit()
    return rid


def _row_digest(on, rid):
    return tuple(row(on.db.execute("SELECT * FROM homebot_rejections WHERE id=?", (rid,))).items())


def test_legacy_street_only_keys_keep_their_recorded_scope_and_are_never_rewritten(on):
    inventory(on.db)
    _apt(on.db, 8, rent12=900, building="10 Test Street", suburb="South Yarra")              # same street, other suburb (listable area)
    _apt(on.db, 9, rent12=900, building="10 Test Street", suburb="Melbourne CBD", city="Sydney")   # same street+suburb, other city
    eid = submit(on)
    run_all(on)
    baseline = current(on, eid)                                       # no record yet: the listing with nothing excluded (top-3 cap)
    assert len(json.loads(baseline["listed_ids"])) == 3
    # legacy row WITH a snapshot apartment row: bound to street + suburb, city unknown
    rid = _legacy_record(on, eid, 1, "10 Test Street", with_snapshot=True)
    before = _row_digest(on, rid)
    r = hs.rejection(on.db, rid)
    assert r["building_identity"]["precision"] == hs.PRECISION_STREET_SUBURB and r["building_identity"]["city"] is None
    assert "before locality binding" in r["building_identity"]["note"]
    hs.request_rerun(on.db, eid, "Anna")
    run_all(on)
    listed = set(json.loads(current(on, eid)["listed_ids"]))
    assert 8 in listed and 3 in listed and not ({1, 2, 9} & listed)   # unit 9 (unknown city) stays excluded; South Yarra is not
    assert _row_digest(on, rid) == before                             # nothing written to the legacy row
    with pytest.raises(ValidationError):
        hs.material_change(on.db, rid, 8)                               # not in the declined building (other suburb)
    on.db.execute("UPDATE homebot_rejections SET status='reopened' WHERE id=?", (rid,))
    on.db.commit()
    # legacy row WITHOUT a snapshot: street only, any locality
    rid2 = _legacy_record(on, eid, 1, "10 Test Street", with_snapshot=False)
    r2 = hs.rejection(on.db, rid2)
    assert r2["building_identity"]["precision"] == hs.PRECISION_STREET and r2["building_identity"]["suburb"] is None
    hs.request_rerun(on.db, eid, "Anna")
    run_all(on)
    listed = set(json.loads(current(on, eid)["listed_ids"]))
    assert listed == {3}
    on.db.execute("UPDATE homebot_rejections SET status='reopened' WHERE id=?", (rid2,))
    on.db.commit()
    # a legacy key that does not name the basis row's street is taken from the stored key alone (street only), never from the basis
    rid3 = _legacy_record(on, eid, 3, "99 Elsewhere Road", with_snapshot=True)
    r3 = hs.rejection(on.db, rid3)
    assert r3["building_identity"]["precision"] == hs.PRECISION_STREET and r3["building_identity"]["street"] == "99 elsewhere road"
    hs.request_rerun(on.db, eid, "Anna")
    run_all(on)
    sl3 = current(on, eid)                                            # nothing on that street: excludes nothing
    cands = {int(c["apartment_id"]): c for c in json.loads(sl3["result_json"])["candidates"]}
    assert {1, 2, 3, 8, 9} <= set(cands) and not any(hm.REJECTED_BY_CUSTOMER in c["exclusion_reasons"] for c in cands.values())
    assert json.loads(sl3["listed_ids"]) == json.loads(baseline["listed_ids"])   # the same top-3 listing as with no record at all
    # a new full-identity record for the same building does not inherit the legacy wildcard
    new = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    assert "|" in new["building_key"]
    run_all(on)
    listed = set(json.loads(current(on, eid)["listed_ids"]))
    assert 8 in listed and 9 in listed and not ({1, 2} & listed)


def test_inventory_snapshot_carries_city_and_the_hash_moves_with_it(on):
    inventory(on.db)
    assert "city" in hs.INVENTORY_COLUMNS
    h0 = hs.inventory_hash(hs.inventory_rows(on.db))
    on.db.execute("UPDATE apartments SET city='Sydney' WHERE id=1")
    on.db.commit()
    assert hs.inventory_hash(hs.inventory_rows(on.db)) != h0
    eid = submit(on)
    run_all(on)
    snap = json.loads(current(on, eid)["result_json"])["inventory_snapshot"]
    assert {r["id"]: r["city"] for r in snap}[1] == "Sydney"
    rj = hs.record_rejection(on.db, eid, scope="building", apartment_id=2, staff="Anna")
    assert rj["offered_basis"]["apartment"]["city"] == "Melbourne"
    assert [r["id"] for r in rj["offered_basis"]["building_apartments"]] == [2]   # unit 1 (Sydney) is not this building any more


def _make_legacy_pre_city_snapshot(db, sl):
    """Rewrite a stored current shortlist the way an inc2b deployment left it: snapshot rows without `city`, inventory_hash
    computed over the pre-city column set (immutable legacy evidence; nothing else on the row changes)."""
    legacy_cols = tuple(k for k in hs.INVENTORY_COLUMNS if k != "city")
    res = json.loads(sl["result_json"])
    legacy_hash = hs._hash([{k: r.get(k) for k in legacy_cols} for r in hs.inventory_rows(db)])
    if sl["inventory_hash"] == legacy_hash:                              # explicit idempotence: already the legacy shape (a Leased /
        assert not any("city" in r for r in res["inventory_snapshot"])  # Closed enquiry is never recomputed, so the row stays legacy)
        return legacy_hash
    assert sl["inventory_hash"] == hs.inventory_hash(hs.inventory_rows(db)) and all("city" in r for r in res["inventory_snapshot"]), "start state must be a fresh current row"
    res["inventory_snapshot"] = [{k: r.get(k) for k in legacy_cols} for r in res["inventory_snapshot"]]
    db.execute("UPDATE homebot_shortlists SET result_json=?, inventory_hash=? WHERE id=?", (json.dumps(res), legacy_hash, sl["id"]))
    db.commit()
    assert row(db.execute("SELECT inventory_hash FROM homebot_shortlists WHERE id=?", (sl["id"],)))["inventory_hash"] == legacy_hash
    return legacy_hash


def test_legacy_pre_city_snapshot_fails_closed_until_the_drift_sweep_recomputes_it(on):
    """Neutral R2 (preliminary inc3 review): adding `city` to INVENTORY_COLUMNS changes inventory_hash for every stored snapshot.
    Window: a legacy (pre-city) current shortlist reads 'Out of date' and any decline recorded against it has no exact basis
    (snapshot_missing → material_change refuses, fail closed); the window closes when the run_pending drift sweep queues the
    recompute (no trigger needed) and the fresh snapshot in today's shape is stored. Records already taken keep their
    snapshot_missing basis (history is not rewritten); new records against the fresh shortlist carry a full basis."""
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sl = current(on, eid)
    assert hs.freshness(on.db, sl) is None and job(on, eid)["status"] == hs.DONE
    _make_legacy_pre_city_snapshot(on.db, sl)
    sl = current(on, eid)
    assert "apartment list changed" in (hs.freshness(on.db, sl) or "")                    # read side: out of date, never current options
    # a decline recorded against the legacy shortlist: recorded, but with no exact basis -> material change refuses
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=2, staff="Anna")
    assert rj["offered_basis"]["snapshot_missing"] is True and rj["offered_basis"]["apartment"] is None
    with pytest.raises(ValidationError) as ei:
        hs.material_change(on.db, rj["id"], 2)
    assert "not captured exactly" in json.dumps(ei.value.errors)
    # the record's own trigger queued a run; cancel that to show the sweep alone closes the window
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.DONE, eid)); on.db.commit()
    assert hs.sweep_inventory_drift(on.db, now_iso()) == [(eid, CUR)]
    assert job(on, eid)["status"] == hs.QUEUED and job(on, eid)["reason"] == "inventory_drift"
    assert hs.sweep_inventory_drift(on.db, now_iso()) == []                               # idempotent while queued
    run_all(on)                                                                          # run_pending: sweep + the queued job
    sl2 = current(on, eid)
    assert sl2["id"] != sl["id"] and hs.freshness(on.db, sl2) is None
    assert all("city" in r for r in json.loads(sl2["result_json"])["inventory_snapshot"])
    assert hs.sweep_inventory_drift(on.db, now_iso()) == []                               # drift cleared: nothing to queue
    # history untouched; a new record against the fresh shortlist has a full basis
    assert hs.rejection(on.db, rj["id"])["offered_basis"]["snapshot_missing"] is True
    rj2 = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Anna")
    assert rj2["offered_basis"]["snapshot_missing"] is False and rj2["offered_basis"]["apartment"]["city"] == "Melbourne"
    assert hs.material_change(on.db, rj2["id"], 1) == []


def test_one_ordinary_run_pending_pass_recomputes_a_legacy_snapshot_and_the_exclusions_hold(on, monkeypatch):
    """Neutral R2 / root: the ordinary worker pass (run_pending; the worker wakes every 60 s or on kick, switch on) is the trigger -
    no staff action, no inventory edit. Pinned limits: a FAILED job is staff's to retry (never re-queued by the sweep); a
    Leased / Closed enquiry is never recomputed (its old-shape snapshot reads snapshot_missing as history, not current options);
    with the switch OFF nothing runs at all."""
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    first = current(on, eid)
    _make_legacy_pre_city_snapshot(on.db, first)
    assert job(on, eid)["status"] == hs.DONE
    # ONE ordinary pass: the sweep queues the inventory_drift job and the same pass runs it
    out = run_all(on)
    assert [o["outcome"] for o in out] and job(on, eid)["reason"] == "inventory_drift" and job(on, eid)["status"] == hs.DONE
    sl2 = current(on, eid)
    assert sl2["id"] != first["id"] and hs.freshness(on.db, sl2) is None
    assert all("city" in r for r in json.loads(sl2["result_json"])["inventory_snapshot"])
    assert sl2["inventory_hash"] == hs.inventory_hash(hs.inventory_rows(on.db))
    # limits: running / failed job -> left alone
    _make_legacy_pre_city_snapshot(on.db, sl2)
    for st in (hs.RUNNING, hs.FAILED):
        on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (st, eid)); on.db.commit()
        assert hs.sweep_inventory_drift(on.db, now_iso()) == []
        assert job(on, eid)["status"] == st
    # a FAILED job is staff's to retry: the staff rerun request is the way back
    hs.request_rerun(on.db, eid, "Anna")
    assert job(on, eid)["status"] == hs.QUEUED
    run_all(on)
    assert hs.freshness(on.db, current(on, eid)) is None
    # Leased / Closed enquiry: never recomputed, the old-shape snapshot is history
    for st in ("Leased", "Closed"):
        _make_legacy_pre_city_snapshot(on.db, current(on, eid))
        on.db.execute("UPDATE homebot_enquiries SET status=? WHERE id=?", (st, eid)); on.db.commit()
        assert hs.sweep_inventory_drift(on.db, now_iso()) == []
        assert "apartment list changed" in (hs.freshness(on.db, current(on, eid)) or "")
        assert job(on, eid)["status"] == hs.DONE
    # switch OFF: no sweep, no run
    on.db.execute("UPDATE homebot_enquiries SET status='New' WHERE id=?", (eid,)); on.db.commit()
    monkeypatch.delenv(hs.GATE_ENV, raising=False)
    assert not hs.enabled()
    assert hs.sweep_inventory_drift(on.db, now_iso()) == [] and hs.run_pending(on.db) == []
    assert job(on, eid)["status"] == hs.DONE


def test_drift_sweep_filters_ineligible_jobs_before_the_page_limit_so_nothing_starves(on):
    """Root: a page of skipped (queued/running/FAILED) rows must never starve later eligible drift rows. With limit=1 the first
    stale row's FAILED job is passed over and the second, eligible row is queued; the first stays exactly as it was."""
    inventory(on.db)
    e1 = submit(on, "hb-key-0001")
    e2 = submit(on, "hb-key-0002", email="b@example.com")
    run_all(on)
    assert current(on, e1)["id"] < current(on, e2)["id"]
    _make_legacy_pre_city_snapshot(on.db, current(on, e1))
    _make_legacy_pre_city_snapshot(on.db, current(on, e2))
    for first_state in (hs.FAILED, hs.QUEUED, hs.RUNNING):
        on.db.execute("UPDATE homebot_jobs SET status=?, reason='probe' WHERE enquiry_id=?", (first_state, e1))
        on.db.execute("UPDATE homebot_jobs SET status=?, reason='done-before' WHERE enquiry_id=?", (hs.DONE, e2)); on.db.commit()
        assert hs.sweep_inventory_drift(on.db, now_iso(), limit=1) == [(e2, CUR)]
        j1 = job(on, e1)
        assert j1["status"] == first_state and j1["reason"] == "probe"                    # untouched
        assert job(on, e2)["status"] == hs.QUEUED and job(on, e2)["reason"] == "inventory_drift"
        assert hs.sweep_inventory_drift(on.db, now_iso(), limit=1) == []                  # both now ineligible: nothing more to queue
    # the FAILED one is staff's: a rerun request brings it back, the ordinary pass then recomputes both
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.FAILED, e1)); on.db.commit()
    hs.request_rerun(on.db, e1, "Anna")
    run_all(on)
    assert hs.freshness(on.db, current(on, e1)) is None and hs.freshness(on.db, current(on, e2)) is None


def test_out_of_date_wording_states_the_actual_job_state_and_never_promises_a_queued_run(on, monkeypatch):
    """Root (inc3): the freshness text may say 'queued' / 'in progress' only when the job row says so. Before the sweep, after a
    FAILED run, or with the switch OFF it says 'a fresh run is needed' (naming a failed run). Held/stale enforcement unchanged."""
    inventory(on.db)
    e1 = submit(on, "hb-key-0001")
    run_all(on)
    sl = current(on, e1)
    _make_legacy_pre_city_snapshot(on.db, sl)
    sl = current(on, e1)
    assert job(on, e1)["status"] == hs.DONE
    prefix = "Out of date: the apartment list changed after these options were prepared; "
    assert hs.freshness(on.db, sl) == prefix + hs.RUN_NEEDED                              # drift seen, sweep has not run yet
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.FAILED, e1)); on.db.commit()
    assert hs.freshness(on.db, sl) == prefix + hs.RUN_FAILED
    assert "failed" in hs.RUN_FAILED and "queued" not in hs.RUN_FAILED
    monkeypatch.delenv(hs.GATE_ENV, raising=False)
    assert not hs.enabled()
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.QUEUED, e1)); on.db.commit()
    assert hs.freshness(on.db, sl) == prefix + hs.RUN_NEEDED                              # OFF: a queued row is not a promise
    monkeypatch.setenv(hs.GATE_ENV, "1")
    assert hs.freshness(on.db, sl) == prefix + hs.RUN_QUEUED
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.RUNNING, e1)); on.db.commit()
    assert hs.freshness(on.db, sl) == prefix + hs.RUN_IN_PROGRESS
    on.db.execute("UPDATE homebot_jobs SET status=? WHERE enquiry_id=?", (hs.DONE, e1)); on.db.commit()
    # the ordinary pass (sweep + worker) recomputes it, after which no wording is shown at all
    run_all(on)
    assert hs.freshness(on.db, current(on, e1)) is None


def test_null_suburb_rows_never_crash_and_bind_as_the_recorded_empty_locality(on):
    """H-N1 (root, 11 Nov 2026): a live row with a NULL suburb is a KNOWN empty locality ('') - recordable, excluding its own
    blank-locality siblings on that street and never the same street text in a named suburb; a snapshot row WITHOUT the key stays
    UNKNOWN (legacy evidence), so a full-identity record never widens to it."""
    # pure helpers
    assert hm.building_identity("Unit 1 / 10 Test Street", None, "Melbourne") == "10 test street||melbourne"
    assert hm.row_identity({"address": "Unit 1 / 10 Test Street"}) == ("10 test street", hm.UNKNOWN, hm.UNKNOWN)
    assert hm.row_identity({"address": "Unit 1 / 10 Test Street", "suburb": None, "city": None}) == ("10 test street", "", "")
    full = hm.identity_from_key("10 test street||melbourne")
    assert hm.identity_matches(full, ("10 test street", "", "melbourne"))
    assert not hm.identity_matches(full, ("10 test street", "carlton", "melbourne"))
    assert not hm.identity_matches(full, ("10 test street", hm.UNKNOWN, "melbourne"))        # never widened to an unknown locality
    # legacy street-only record: snapshot row with the suburb key present but NULL binds to the empty locality; without the key -> street only
    leg = {"building_key": "10 Test Street", "offered_basis": {"apartment": {"address": "Unit 1 / 10 Test Street", "suburb": None}}}
    assert hs.record_identity(leg)["precision"] == hs.PRECISION_STREET_SUBURB and hs.record_identity(leg)["components"] == ("10 test street", "", hm.UNKNOWN)
    leg2 = {"building_key": "10 Test Street", "offered_basis": {"apartment": {"address": "Unit 1 / 10 Test Street"}}}
    assert hs.record_identity(leg2)["precision"] == hs.PRECISION_STREET and hs.record_identity(leg2)["components"] == ("10 test street", hm.UNKNOWN, hm.UNKNOWN)
    # product path: two blank-locality units on the same street as the named-suburb units 1 and 2
    inventory(on.db)
    _apt(on.db, 8, rent12=900, building="10 Test Street", suburb=None)
    _apt(on.db, 9, rent12=900, building="10 Test Street", suburb=None)
    eid = submit(on)
    run_all(on)
    rec = hs.record_rejection(on.db, eid, scope="building", apartment_id=8, staff="Anna")
    assert hs.rejection(on.db, rec["id"])["building_identity"]["suburb"] == ""
    hs.request_rerun(on.db, eid, "Anna")
    run_all(on)
    sl = current(on, eid)
    cands = {int(c["apartment_id"]): c for c in json.loads(sl["result_json"])["candidates"]}
    assert all(hm.REJECTED_BY_CUSTOMER in cands[i]["exclusion_reasons"] for i in (8, 9))
    assert not any(hm.REJECTED_BY_CUSTOMER in cands[i]["exclusion_reasons"] for i in (1, 2, 3, 4))
    assert not ({8, 9} & set(json.loads(sl["listed_ids"])))
    # guards keep working on the blank-locality building: the sibling is a duplicate of the same record; material change is like for like
    with pytest.raises(ValidationError):
        hs.record_rejection(on.db, eid, scope="building", apartment_id=9, staff="Anna")
    assert isinstance(hs.material_change(on.db, rec["id"], 9), list)                       # same (empty-locality) building: comparable
    with pytest.raises(ValidationError):
        hs.material_change(on.db, rec["id"], 1)                                            # named-suburb unit: not this building
    # inverse: a record for the NAMED suburb never reaches the blank-locality units of the same street text
    on.db.execute("UPDATE homebot_rejections SET status='reopened' WHERE id=?", (rec["id"],)); on.db.commit()
    rec2 = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    assert hs.rejection(on.db, rec2["id"])["building_identity"]["suburb"] == "melbourne cbd"
    hs.request_rerun(on.db, eid, "Anna")
    run_all(on)
    cands = {int(c["apartment_id"]): c for c in json.loads(current(on, eid)["result_json"])["candidates"]}
    assert all(hm.REJECTED_BY_CUSTOMER in cands[i]["exclusion_reasons"] for i in (1, 2))
    assert not any(hm.REJECTED_BY_CUSTOMER in cands[i]["exclusion_reasons"] for i in (8, 9, 3, 4))
    # a legacy street-only record stays unconstrained on locality: it covers blank and named localities of its street alike
    legacy = hm.identity_from_key("10 Test Street")
    assert hm.identity_matches(legacy, ("10 test street", "", "melbourne")) and hm.identity_matches(legacy, ("10 test street", "melbourne cbd", "melbourne"))
    assert not hm.identity_matches(legacy, ("20 other road", "", "melbourne"))
