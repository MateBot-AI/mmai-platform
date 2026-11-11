"""Root's two provenance probes (UID2 ruling): identical-pair companions reordered with their distinct uids preserved, and an
identity edit A->B->A (uid retired and replaced, pair restored) between computation and the guarded write. Invariant under test:
PROVABLE identity attribution only - an occupant is stamped with a uid only when that uid is the identity the allocation was
computed for; anything else stays unstamped (unknown). Positive controls: unchanged rows, and distinct-pair companions reordered
with their uids kept (the attribution must follow the people, not the positions)."""
import json

import pytest

from server import ai, ai_queue, identities, matching
from server.db import pj, retention_guard, row, rows
from tests.test_data_and_api import member
from tests.test_launch_neutral import _apt, _enq
from tests.test_t6_fail_closed import _reply_builder, _good
from tests.test_t6_grounding import _party_group  # noqa: F401
from tests.test_uid_allocation import _occupants, _uid_of, _units_now, _fresh_rooms


def _twins_group(db, monkeypatch):
    """Astrid (lead) with two DISTINCT companions who share name AND email (GP13 permits this), plus Devi solo, on a 3B."""
    a = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0)
    lead = _enq(db, "Astrid", "astrid@example.com", party="With friends", party_size="3", budget_option="3B-single", apt_sizes=["3 bedrooms"],
                rooms=["Share with one other", "Own room"], room_first="Share with one other", mate_max="5",
                party_members=[member(name="Kim Lee", email="kim@example.com"), member(name="Kim Lee", email="kim@example.com")],
                couples_answer="No", contact_method="WhatsApp")
    solo = _enq(db, "Devi", "devi@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    assert g is not None
    return a, lead, solo, g


def _member_rows(c, eid):
    return rows(c.execute("SELECT id, name, email FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))


def _ident_by_row(c, eid):
    return {i["party_member_id"]: i["person_uid"] for i in identities.active(c, eid) if i["role"] == "member"}


def _reinsert_reversed_keeping_uids(c, eid):
    """Delete + re-insert the party rows in REVERSED order and keep each human's uid bound to the row that is now theirs (the
    caller's own knowledge of who is who - e.g. a form edit that re-saves the party). Returns {old_row_id: new_row_id}."""
    old = _member_rows(c, eid)
    by_old = _ident_by_row(c, eid)
    c.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
    mapping = {}
    for m in reversed(old):
        c.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?,?,?)", (eid, m["name"], m["email"]))
        mapping[m["id"]] = c.execute("SELECT last_insert_rowid()").fetchone()[0]   # portable: lastval() on PostgreSQL (cur.lastrowid is None there)
        c.execute("UPDATE people_identities SET party_member_id=? WHERE person_uid=?", (mapping[m["id"]], by_old[m["id"]]))
    return mapping


def test_probe_1_identical_pair_companions_reordered_with_uids_kept(db, monkeypatch):
    a, lead, solo, g = _twins_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])                       # computation
    r1, r2 = _member_rows(db, lead["id"])
    assert (r1["name"], r1["email"]) == (r2["name"], r2["email"])
    uid1, uid2 = _ident_by_row(db, lead["id"])[r1["id"]], _ident_by_row(db, lead["id"])[r2["id"]]
    assert uid1 != uid2                                                   # two humans, two identities
    rooms = _fresh_rooms(db, g["id"])
    occ = {o["person_id"]: o for r in rooms for o in r["occupants"]}
    occ[f"{lead['id']}m1"]["share"], occ[f"{lead['id']}m2"]["share"] = 300.0, 500.0   # unequal proposals, computed for Kim#1 / Kim#2
    with retention_guard(db, label="rewrite"):
        _reinsert_reversed_keeping_uids(db, lead["id"])                  # rows reordered; uid1 is now bound to the SECOND row
    assert _ident_by_row(db, lead["id"]) == {_member_rows(db, lead["id"])[1]["id"]: uid1, _member_rows(db, lead["id"])[0]["id"]: uid2}
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, rooms, units)
    got = {o["person_id"]: o.get("person_uid") for r in out for o in r["occupants"]}
    # the invariant: Kim#1's 300 proposal may carry uid1 or nothing - never uid2 (and vice versa)
    assert got[f"{lead['id']}m1"] in (uid1, None), got
    assert got[f"{lead['id']}m2"] in (uid2, None), got
    assert got[str(lead["id"])] == _uid_of(db, lead["id"]) and got[str(solo["id"])] == _uid_of(db, solo["id"])


def test_probe_1b_identical_pair_companions_what_reconcile_itself_does_on_a_reorder(db, monkeypatch):
    """Information for the record: the identity module's own capture/reconcile cannot tell identical-pair rows apart on a
    delete+re-insert, so it retires both and issues fresh uids (ambiguous -> no reattach). Stamping afterwards must then be absent."""
    a, lead, solo, g = _twins_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    before_uids = set(_ident_by_row(db, lead["id"]).values())
    with retention_guard(db, label="rewrite"):
        cap = identities.capture(db, lead["id"])
        old = _member_rows(db, lead["id"])
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (lead["id"],))
        for m in reversed(old):
            db.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?,?,?)", (lead["id"], m["name"], m["email"]))
        res = identities.reconcile(db, lead["id"], "left", before=cap)
    after_uids = set(_ident_by_row(db, lead["id"]).values())
    assert res["reattached"] == 0 and not (before_uids & after_uids)       # ambiguous: both retired, two new identities
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    got = {o["person_id"]: o.get("person_uid") for r in out for o in r["occupants"]}
    assert got[f"{lead['id']}m1"] is None and got[f"{lead['id']}m2"] is None   # the computed-for identities no longer exist


def test_probe_2_identity_edit_a_b_a_between_computation_and_write(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    bo = _uid_of(db, lead["id"], "Bo Berg")
    with retention_guard(db, label="edit"):
        db.execute("UPDATE party_members SET email='b@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
        r1 = identities.reconcile(db, lead["id"], "left")                 # A -> B: Bo's uid retired, a new one issued
        db.execute("UPDATE party_members SET email='bo@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
        r2 = identities.reconcile(db, lead["id"], "left")                 # B -> A: that one retired too, a THIRD uid now carries Bo's pair
    assert r1["reset"] == 1 and r2["reset"] == 1
    bo_now = _uid_of(db, lead["id"], "Bo Berg")
    assert bo_now != bo and row(db.execute("SELECT retired_at FROM people_identities WHERE person_uid=?", (bo,)))["retired_at"]
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    got = {o["person_id"]: o.get("person_uid") for r in out for o in r["occupants"]}
    # the allocation was computed for identity `bo`, which is retired: provable attribution -> nothing (never the replacement uid)
    assert got[f"{lead['id']}m1"] is None, got
    assert got[f"{lead['id']}m2"] == _uid_of(db, lead["id"], "Cy Lund") and got[str(lead["id"])] == _uid_of(db, lead["id"])


def test_control_unchanged_rows_stamp_all_four(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    got = {o["person_id"]: o.get("person_uid") for r in out for o in r["occupants"]}
    assert got == {str(lead["id"]): _uid_of(db, lead["id"]), f"{lead['id']}m1": _uid_of(db, lead["id"], "Bo Berg"),
                   f"{lead['id']}m2": _uid_of(db, lead["id"], "Cy Lund"), str(solo["id"]): _uid_of(db, solo["id"])}


def test_control_distinct_pair_companions_reordered_with_uids_kept_follow_the_people(db, monkeypatch):
    """Bo and Cy (distinct pairs) re-saved in reversed order with their uids kept (capture/reconcile reattaches them): the
    allocation computed for Bo at position 1 is attributed to Bo (or to nobody), never to Cy."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    bo, cy = _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund")
    with retention_guard(db, label="rewrite"):
        cap = identities.capture(db, lead["id"])
        old = _member_rows(db, lead["id"])
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (lead["id"],))
        for m in reversed(old):
            db.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?,?,?)", (lead["id"], m["name"], m["email"]))
        assert identities.reconcile(db, lead["id"], "left", before=cap)["reattached"] == 2
    assert _uid_of(db, lead["id"], "Bo Berg") == bo and _uid_of(db, lead["id"], "Cy Lund") == cy
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    got = {o["person_id"]: o.get("person_uid") for r in out for o in r["occupants"]}
    assert got[f"{lead['id']}m1"] in (bo, None) and got[f"{lead['id']}m2"] in (cy, None), got
    assert got[f"{lead['id']}m1"] != cy and got[f"{lead['id']}m2"] != bo


# ---------------------------------------------------------------- UID3: identity captured at computation, proved at the write
def test_twins_end_to_end_immediate_save_attributes_each_kim_their_own_row_identity_and_share(db, monkeypatch):
    """Immediate (guarded run save): two distinct same-name-same-email companions each get THEIR row's identity on their own
    occupant - distinct uids, the first row's on m1 and the second row's on m2 - and the shares stay individual figures."""
    a, lead, solo, g = _twins_group(db, monkeypatch)
    r1, r2 = _member_rows(db, lead["id"])
    by_row = _ident_by_row(db, lead["id"])
    occ = {o["person_id"]: o for o in _occupants(db, g["id"])}
    assert occ[f"{lead['id']}m1"]["person_uid"] == by_row[r1["id"]] and occ[f"{lead['id']}m2"]["person_uid"] == by_row[r2["id"]]
    assert occ[f"{lead['id']}m1"]["person_uid"] != occ[f"{lead['id']}m2"]["person_uid"]
    assert occ[str(lead["id"])]["person_uid"] == _uid_of(db, lead["id"]) and occ[str(solo["id"])]["person_uid"] == _uid_of(db, solo["id"])
    assert all(o.get("share") is not None for o in occ.values())
    for o in _occupants(db, g["id"], "group_apartments"):
        assert o["person_uid"] == occ[o["person_id"]]["person_uid"]


def test_identity_missing_at_computation_is_never_backfilled_by_pair_at_the_save(db, monkeypatch):
    """Units built WITHOUT the identity capture (the person had no identity when the plan was computed) stamp nothing at the
    save - the pairs match the rows, but there is no computation identity to prove, so the save does not even reconcile them
    (nothing is issued at the save to be attributed)."""
    a = _apt(db, type_code="3B", beds=("1 king or 2 singles",) * 3, rent12=1500.0, rent6=1600.0)
    lead = _enq(db, "Astrid", "astrid@example.com", party="With friends", party_size="3", budget_option="3B-single", apt_sizes=["3 bedrooms"],
                rooms=["Share with one other", "Own room"], room_first="Share with one other", mate_max="5",
                party_members=[member(name="Bo Berg", email="bo@example.com"), member(name="Cy Lund", email="cy@example.com")], couples_answer="No", contact_method="WhatsApp")
    assert identities.active(db, lead["id"]) == []                        # nothing issued yet (creation does not issue)
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    pm = rows(db.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
    bare = [matching.unit_from_row(e, pm)]                                 # computed with no identity captured
    assert all(p.ident_uid is None and p.ident_email for p in bare[0].people)
    rooms = [{"room": 1, "occupants": [{"person_id": p.id, "name": p.name, "bed": "single", "share": 100.0, "arrangement": []} for p in bare[0].people]}]
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, rooms, bare)
    assert identities.active(db, lead["id"]) == []                        # nothing issued at the save ...
    assert all("person_uid" not in o for r in out for o in r["occupants"])   # ... and nothing attributed: not computed for them
    # positive control: the same rows computed WITH the capture (as every engine path does) stamp all three
    captured = _units_now(db, lead["id"])
    rooms = [{"room": 1, "occupants": [{"person_id": p.id, "name": p.name, "bed": "single", "share": 100.0, "arrangement": []} for p in captured[0].people]}]
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, rooms, captured)
    assert sorted(o["person_uid"] for r in out for o in r["occupants"]) == sorted(i["person_uid"] for i in identities.active(db, lead["id"]))


def test_identity_issue_before_computation_is_one_step_that_rolls_back_whole_on_failure(db, monkeypatch):
    """The pre-computation issue runs as one guarded step on a clean connection: a failure part-way leaves NO identity behind
    for any of the enquiries (never a half-issued pool), and the computation does not proceed with a partial capture."""
    lead = _enq(db, "Astrid", "astrid@example.com", party="With friends", party_size="3", budget_option="3B-single", apt_sizes=["3 bedrooms"],
                rooms=["Share with one other", "Own room"], room_first="Share with one other", mate_max="5",
                party_members=[member(name="Bo Berg", email="bo@example.com"), member(name="Cy Lund", email="cy@example.com")], couples_answer="No", contact_method="WhatsApp")
    solo = _enq(db, "Devi", "devi@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    real = identities.reconcile
    calls = []

    def flaky(c, eid, reason="left", before=None, now=None):
        calls.append(eid)
        if len(calls) == 2:
            raise RuntimeError("second enquiry fails")
        return real(c, eid, reason, before=before, now=now)
    monkeypatch.setattr(identities, "reconcile", flaky)
    assert not db.in_transaction
    with pytest.raises(RuntimeError):
        matching.load_pool(db, "test")
    assert not db.in_transaction and not db.guarded(db) if hasattr(db, "guarded") else not db.in_transaction
    assert identities.active(db, lead["id"]) == [] and identities.active(db, solo["id"]) == []   # the first issue was rolled back too
    monkeypatch.setattr(identities, "reconcile", real)
    units = matching.load_pool(db, "test")
    assert len(identities.active(db, lead["id"])) == 3 and len(identities.active(db, solo["id"])) == 1
    assert all(p.ident_uid for u in matching.expand(units) for p in u.people)


def test_a_pending_foreign_write_means_no_identity_issue_and_no_stamp_for_people_without_one(db, monkeypatch):
    """On a connection with somebody else's pending write (unguarded, dirty) the pre-computation issue is NOT attempted: the
    pending work is left to its owner; people who already have an identity are still captured (read-only), people without
    one are left unstamped in that computation. Nothing is guessed, nothing of the pending write is committed or rolled back."""
    a, lead, solo, g = _party_group(db, monkeypatch)                      # Astrid/Bo/Cy/Devi have identities
    newcomer = _enq(db, "Eli", "eli@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    assert identities.active(db, newcomer["id"]) == []
    db.execute("UPDATE enquiries SET status='New' WHERE id IN (?, ?)", (lead["id"], solo["id"]))
    db.execute("UPDATE enquiries SET mate_note='pending write by someone else' WHERE id=?", (newcomer["id"],))   # left uncommitted
    assert db.in_transaction
    units = {int(u.id): u for u in matching.expand(matching.load_pool(db, "test"))}
    assert db.in_transaction                                               # untouched: neither committed nor rolled back
    assert identities.active(db, newcomer["id"]) == []                      # not issued on a dirty connection
    assert all(p.ident_uid for p in units[lead["id"]].people) and units[solo["id"]].people[0].ident_uid
    assert units[newcomer["id"]].people[0].ident_uid is None
    db.rollback()
    rooms = [{"room": 1, "occupants": [{"person_id": str(newcomer["id"]), "name": "Eli", "bed": "single", "share": 100.0, "arrangement": []}]}]
    with retention_guard(db, label="save"):
        out = matching._stamp_people(db, rooms, [units[newcomer["id"]]])
    assert "person_uid" not in out[0]["occupants"][0] and identities.active(db, newcomer["id"]) == []


def test_a_failed_save_step_persists_neither_stamps_nor_the_steps_identity_changes(db, monkeypatch):
    """Rollback control: a guarded write that stamps and then fails leaves the stored allocation and the identities exactly as
    before (the stamp lives and dies with its step)."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    stored_before = row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (g["id"],)))["allocation_json"]
    db.execute("UPDATE party_members SET email='b@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
    db.commit()
    idents_before = sorted(i["person_uid"] for i in identities.active(db, lead["id"]))
    with pytest.raises(RuntimeError):
        with retention_guard(db, label="save"):
            rooms = _fresh_rooms(db, g["id"])
            matching._stamp_people(db, rooms, units)                       # reconciles (Bo reset) and stamps the provable ones
            db.execute("UPDATE groups SET allocation_json=? WHERE id=?", (json.dumps({"rooms": rooms}), g["id"]))
            raise RuntimeError("later part of the step fails")
    assert row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (g["id"],)))["allocation_json"] == stored_before
    assert sorted(i["person_uid"] for i in identities.active(db, lead["id"])) == idents_before


def test_deferred_creator_follow_up_uses_the_computation_identities_not_the_pairs(db, monkeypatch):
    """Deferred (creator's follow-up, _stamp_stored): after the creator committed an unstamped group from `units`, an A->B->A edit
    on Bo before the follow-up leaves Bo unstamped (his computation identity is retired) while the others are stamped; the
    reversed-twins case (uids kept) stamps each proposal with its own human's uid."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    bo = _uid_of(db, lead["id"], "Bo Berg")
    for table, key in (("groups", "id"), ("group_apartments", "group_id")):
        for r in rows(db.execute(f"SELECT id, allocation_json FROM {table} WHERE {key}=?", (g["id"],))):
            alloc = pj(r["allocation_json"], {})
            for rm in alloc.get("rooms") or []:
                for o in rm.get("occupants") or []:
                    o.pop("person_uid", None)
            db.execute(f"UPDATE {table} SET allocation_json=? WHERE id=?", (json.dumps(alloc), r["id"]))
    db.commit()
    with retention_guard(db, label="edit"):
        db.execute("UPDATE party_members SET email='b@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
        identities.reconcile(db, lead["id"], "left")
        db.execute("UPDATE party_members SET email='bo@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
        identities.reconcile(db, lead["id"], "left")
    with retention_guard(db, label="follow-up"):
        matching._stamp_stored(db, g["id"], units)
    occ = {o["person_id"]: o.get("person_uid") for o in _occupants(db, g["id"])}
    assert occ[f"{lead['id']}m1"] is None and occ[f"{lead['id']}m2"] == _uid_of(db, lead["id"], "Cy Lund")
    assert occ[str(lead["id"])] == _uid_of(db, lead["id"]) and occ[str(solo["id"])] == _uid_of(db, solo["id"])
    assert _uid_of(db, lead["id"], "Bo Berg") != bo


def test_provider_packets_carry_no_identity_provenance(db, monkeypatch):
    """Neither the captured uid nor any stamped uid reaches an AI packet; the allocation holds person_uid only (no ident_*)."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    from tests.test_t6_grounding import _packet
    p = _packet(db, g)
    uids = [i["person_uid"] for eid in (lead["id"], solo["id"]) for i in identities.active(db, eid)]
    # 9 Nov 2026: the payload's refs_json (internal provenance metadata, never sent) now pins person_uid per ref; everything the
    # provider sees (ai.review_packet) and every other part of the payload must still carry no uid
    from server import ai as _ai
    model_blob = json.dumps(_ai.review_packet([p]))
    rest_blob = json.dumps({k: v for k, v in p.items() if k != "refs_json"})
    for blob in (model_blob, rest_blob):
        assert uids and not any(u in blob for u in uids) and "ident_" not in blob and "person_uid" not in blob
    assert all(v.get("person_uid") in uids for v in p["refs_json"].values())
    for o in _occupants(db, g["id"]) + _occupants(db, g["id"], "group_apartments"):
        assert set(o) <= {"person_id", "name", "bed", "share", "arrangement", "person_uid"}, o
