"""T3(d) engineering determinism repair (10 Nov 2026): the bed allocation of ONE member set at ONE apartment is the same whatever
order the members are listed in. Before the repair rules.allocate_beds returned the first feasible assignment for the order
given, so the same group got a different (equally lawful) allocation on the queue's replay path (which lists the couple's
enquiry first) than on the run's canonical path, its evidence hash moved between two runs with no change of facts, and it was
sent back for a needless review. No feasibility, preference, weight or validator changes: only the traversal order is fixed.
Upgrade effect (disclosed, not zero): a store written before the repair may hold allocations the canonical search does not
reproduce. A Proposed group's options are refreshed by the next full run as any rule result is (one re-review, once); an
Introduced-or-later group is never rewritten - it meets the existing fail-closed OfferDrift hold for staff (tests below)."""
import json
from itertools import permutations

from server import ai, ai_queue, matching, rules
from server.db import row, rows
from tests.test_data_and_api import client  # noqa: F401
from tests.conftest import apartment, person, unit


def _party_with_couple():
    """Enquiry 100: lead (no partner) + a couple (I16(f) couple_id '100c1') + one more friend - the shape of the group that drifted."""
    lead = person("100", unit="100", arrangements=("Share with one other", "Share with two others", "Own room"))
    c1 = rules.Person(id="100m1", unit_id="100", name="100m1", birth_country="Sweden", budget_max=600.0, arrangements={"Couple sharing one bed"},
                      bunk_ok=True, smokes=False, smoke_ok=True, is_lead=False, is_couple_member=True, couple_id="100c1")
    c2 = rules.Person(id="100m2", unit_id="100", name="100m2", birth_country="Sweden", budget_max=600.0, arrangements={"Couple sharing one bed"},
                      bunk_ok=True, smokes=False, smoke_ok=True, is_lead=False, is_couple_member=True, couple_id="100c1")
    m3 = person("100m3", unit="100", is_lead=False, arrangements=("Share with one other", "Share with two others", "Own room"))
    return unit("100", people=[lead, c1, c2, m3], kind="friends", mate_max=8)


def _beds(alloc):
    """Room number, bed type and person for every occupant - the same things _offer_matches compares."""
    return [(r["room"], sorted((o["person"].id, o["bed"]) for o in r["occupants"])) for r in alloc]


def test_allocation_is_the_same_for_every_member_order_including_a_multi_person_party():
    apt = apartment("3B", rent12=1395.0, rent6=1450.0)
    five = unit("5", arrangements=("Share with one other", "Own room"))
    hundred = _party_with_couple()
    onetwenty = unit("120", arrangements=("Share with one other", "Own room"))
    results = {}
    for order in permutations([five, hundred, onetwenty]):
        people = [p for u in order for p in u.people]
        alloc = rules.allocate_beds(apt, people)
        assert alloc is not None, [u.id for u in order]
        results[tuple(u.id for u in order)] = _beds(alloc[0])
    distinct = {json.dumps(v, sort_keys=True) for v in results.values()}
    assert len(distinct) == 1, results                                  # one allocation, whatever the listing order
    rooms = next(iter(results.values()))
    assert [("100m1", "king"), ("100m2", "king")] in [occ for _, occ in rooms]   # the couple shares one king
    assert sum(len(occ) for _, occ in rooms) == 6 and all(occ for _, occ in rooms)   # everyone placed, every bedroom occupied (GP5)
    # the queue's replay order (couple's enquiry first: 100, 5, 120) and the run's canonical order (5, 100, 120) now agree
    assert results[("100", "5", "120")] == results[("5", "100", "120")]


def test_check_group_allocation_and_shares_do_not_depend_on_unit_order():
    apt = apartment("3B", rent12=1395.0, rent6=1450.0)
    units = [unit("5", arrangements=("Share with one other", "Own room")), _party_with_couple(), unit("120", arrangements=("Share with one other", "Own room"))]
    outs = []
    for order in permutations(units):
        chk = rules.check_group(apt, list(order))
        assert chk.ok, (chk.reasons, [u.id for u in order])
        outs.append(([(r["room"], r["setup"], sorted((o["person_id"], o["bed"], o["share"]) for o in r["occupants"])) for r in chk.allocation],
                     chk.total_weekly, chk.lease_start, chk.lease_end))
    assert all(o == outs[0] for o in outs), outs


def test_canonical_people_order_is_stable_within_an_enquiry_and_sorts_enquiries_by_id():
    """Scope: the order BETWEEN enquiries is canonicalised; the order WITHIN one enquiry (lead first, party members in form order)
    is the stable order every constructor already supplies and is kept as given - arbitrary within-enquiry permutations are out of scope."""
    hundred = _party_with_couple()
    nine = unit("9")
    ppl = rules.canonical_people_order(hundred.people + nine.people)
    assert [p.id for p in ppl] == ["9", "100", "100m1", "100m2", "100m3"]   # numeric ids ascending (not text order), form order kept
    assert rules.canonical_people_order(ppl) == ppl                           # a stable sort: already-canonical input is unchanged


# ---------------------------------------------------------------- the whole loop: run -> queue -> run leaves every evidence hash alone
def test_unchanged_groups_keep_their_allocation_and_evidence_hash_across_the_queue_and_a_rerun(db, monkeypatch):
    """The failure this repair closes, end to end: the queue re-checks a group with its members in the STORED allocation's order
    and a full run re-checks it in canonical order; both must produce one allocation, so a second run with nothing new finds every
    group ready (no 'facts changed since the AI review' from an allocation that merely shuffled equivalent beds).
    (Invariant check: this small pool did not reproduce the drift on its own; the inventory-gated t6_gp6 run in test_data_and_api
    did, and the permutation tests above fail without the repair.)"""
    from server.db import pj
    from tests.test_launch_neutral import SWE, _apt, _enq, _fake_assess, _pool
    _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1395.0, rent6=1450.0)
    _apt(db, address="Unit 2 / 1 Neutral Street", avail="2027-01-25")
    ids = _pool(db)
    # a friends party (lead + one person on the form) that wants to share a room: the party's enquiry is listed first by the
    # queue's replay order whenever its people open the stored allocation, which is exactly the order difference that drifted
    from tests.test_data_and_api import member
    _enq(db, "Erik", "erik@example.com", party="With friends", party_size="2", party_members=[member(name="Fatima", email="fatima@example.com", birth_country_raw="Sweden")],
         rooms=["Share a room with one other person", "Own room"], room_first="Share a room with one other person", budget_option="2B2B-twin", **SWE)
    monkeypatch.setattr(ai, "assess_groups_and_apartments", _fake_assess)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    r1 = matching.full_run(db, "tester", dataset="test")
    assert r1["groups_total"] > 0 and r1["ai_calls"] == 0
    def _allocs():
        return {(ga["group_id"], ga["apartment_id"]): [sorted(str(o["person_id"]) for o in r["occupants"]) for r in (pj(ga["allocation_json"], {}) or {}).get("rooms") or []]
                for ga in rows(db.execute("SELECT group_id, apartment_id, allocation_json FROM group_apartments"))}
    a0 = _allocs()
    out = ai_queue.process_pending(db)
    assert out["ok"] == r1["groups_total"] and out["failed"] == 0 and out.get("settled") is True
    assert _allocs() == a0                                                           # the queue's capture did not reshuffle any bed
    hashes = {g["id"]: g["evidence_hash"] for g in rows(db.execute("SELECT id, evidence_hash FROM groups WHERE status='Proposed'"))}
    assert hashes and all(hashes.values())
    r2 = matching.full_run(db, "tester", dataset="test")
    assert r2["groups_total"] == r1["groups_total"] and r2["groups_new"] == 0 and r2["groups_unchanged"] == r1["groups_total"]
    assert r2["groups_ready"] == r1["groups_total"], r2                               # every unchanged group is still ready
    assert _allocs() == a0                                                           # and the rerun kept every allocation byte-for-byte in substance
    assert {g["id"]: matching.group_evidence(db, g["id"])[0] for g in rows(db.execute("SELECT id FROM groups WHERE status='Proposed'"))} == hashes
    assert ai_queue.pending_count(db) == 0


# ---------------------------------------------------------------- a legacy store: an Introduced group whose allocation is NOT the canonical one
def _swap_rooms(alloc: dict) -> dict:
    """The same people in the other, equally lawful bedrooms - what an older traversal order could have stored."""
    rooms = alloc["rooms"]
    assert len(rooms) == 2 and rooms[0]["setup"] == rooms[1]["setup"]
    out = json.loads(json.dumps(alloc))
    out["rooms"][0]["occupants"], out["rooms"][1]["occupants"] = rooms[1]["occupants"], rooms[0]["occupants"]
    return out


def test_legacy_non_canonical_allocation_of_an_introduced_group_is_held_for_staff_not_rearranged(db, monkeypatch):
    """Upgrade behaviour (root ruling, 10 Nov 2026): canonical allocation must never silently overwrite the terms a group was
    introduced on. A stored allocation the canonical search no longer reproduces is the existing fail-closed OfferDrift hold:
    no model call, the stored rooms/shares/evidence untouched, the group not ready, the reason recorded for staff."""
    from server.db import pj, row
    from tests.test_t6_fail_closed import _good, _reply_builder
    from tests.test_t6_late_result import _primary
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g1 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g1["ai_status"] == "ok" and ai_queue.is_ready(db, g1)["ready"]
    # the store now holds the canonical allocation; make it the OTHER lawful one, as a pre-repair traversal could have written
    ga1 = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], g1["apartment_id"])))
    legacy = _swap_rooms(pj(ga1["allocation_json"]))
    assert legacy != pj(ga1["allocation_json"])
    db.execute("UPDATE group_apartments SET allocation_json=? WHERE group_id=? AND apartment_id=?", (json.dumps(legacy), g["id"], g1["apartment_id"]))
    db.execute("UPDATE groups SET allocation_json=?, status='Introduced' WHERE id=?", (json.dumps(legacy), g["id"]))
    for r in legacy["rooms"]:
        for o in r["occupants"]:
            db.execute("UPDATE group_members SET room=? WHERE group_id=? AND enquiry_id=?", (r["room"], g["id"], int(o["person_id"])))
    db.commit()
    before = (rows(db.execute("SELECT enquiry_id, room, bed, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))),
              row(db.execute("SELECT allocation_json, total_weekly, weekly_rent, evidence_hash, lease_start, lease_end FROM groups WHERE id=?", (g["id"],))),
              rows(db.execute("SELECT apartment_id, allocation_json, total_weekly FROM group_apartments WHERE group_id=? ORDER BY apartment_id", (g["id"],))))
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert not ai_queue.is_ready(db, g2)["ready"]                                    # readiness refuses the drifted offer on its own
    db.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (g["id"],)); db.commit()   # what requeue_stale would do
    calls = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: calls.append(1))
    ai_queue.process_pending(db)
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert not calls, "no model call about an offer that is not the one stored"
    assert g3["ai_status"] == "failed" and g3["ai_error"] == ai_queue.OFFER_DRIFT_REASON and g3["status"] == "Introduced"
    assert not ai_queue.is_ready(db, g3)["ready"] and not db.in_transaction
    after = (rows(db.execute("SELECT enquiry_id, room, bed, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))),
             row(db.execute("SELECT allocation_json, total_weekly, weekly_rent, evidence_hash, lease_start, lease_end FROM groups WHERE id=?", (g["id"],))),
             rows(db.execute("SELECT apartment_id, allocation_json, total_weekly FROM group_apartments WHERE group_id=? ORDER BY apartment_id", (g["id"],))))
    assert after == before, "introduced rooms, shares, terms and evidence untouched"
    # a full run re-checks Proposed groups only: the introduced group's stored terms stay exactly as they are
    matching.full_run(db, "tester", dataset="test")
    after2 = (rows(db.execute("SELECT enquiry_id, room, bed, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))),
              row(db.execute("SELECT allocation_json, total_weekly, weekly_rent, evidence_hash, lease_start, lease_end FROM groups WHERE id=?", (g["id"],))),
              rows(db.execute("SELECT apartment_id, allocation_json, total_weekly FROM group_apartments WHERE group_id=? ORDER BY apartment_id", (g["id"],))))
    assert after2 == before


# ---------------------------------------------------------------- the product predicate on the replay path (positive control)
def _couple_party_pool(db):
    """The drifted shape through the real form path: enquiry A = a friends party of four whose enquirer and Person 2 are a couple
    (3.10 / I16(f)) + two solo twin-sharers, on a 3-bedroom whose rooms are each '1 king or 2 singles'."""
    from tests.test_data_and_api import member
    from tests.test_launch_neutral import SWE, _apt, _enq
    a = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1395.0, rent6=1450.0)
    # the solos are created FIRST so the party's enquiry id is the highest: canonical order lists the party last, the queue's replay
    # order (room 1 = the couple) lists it first - the exact order difference that drifted before the repair
    solos = [_enq(db, n, f"{n.lower()}@example.com", rooms=["Share a room with one other person"], room_first="Share a room with one other person",
                  budget_option="3B-twin", apt_sizes=["3 bedrooms"], mate_max="6", **SWE) for n in ("Astrid", "Bo")]
    party = _enq(db, "Erik", "erik@example.com", party="With friends", party_size="4", couples_answer="Yes", couples=[["lead", "m1"]],
                 rooms=["Couple sharing one bed", "Share a room with one other person"], room_first="Couple sharing one bed", budget_option="3B-twin",
                 apt_sizes=["3 bedrooms"], mate_max="6", party_members=[member(name="Partner", email="partner@example.com", birth_country_raw="Sweden"),
                                                                        member(name="Friend A", email="fa@example.com", birth_country_raw="Sweden"),
                                                                        member(name="Friend B", email="fb@example.com", birth_country_raw="Sweden")], **SWE)
    return a, party, solos


def test_replay_order_with_the_couple_listed_first_reproduces_the_stored_offer_on_the_product_predicate(db, monkeypatch):
    from server.db import pj, row
    from tests.test_t6_fail_closed import _good, _reply_builder
    a, party, solos = _couple_party_pool(db)
    key = ",".join(str(i) for i in sorted([party["id"]] + [s["id"] for s in solos]))
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE member_key=?", (key,)))
    assert g is not None, "the six-person group forms at the 3-bedroom"
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], a["id"])))
    rooms = pj(ga["allocation_json"])["rooms"]
    assert sorted(str(o["person_id"]) for o in rooms[0]["occupants"]) == [str(party["id"]), f"{party['id']}m1"]   # the couple holds room 1
    # the queue's replay order (derived room by room, as ai_queue does) lists the couple's enquiry first; canonical order lists it last
    replay = []
    for r in rooms:
        for o in r["occupants"]:
            uid = str(o["person_id"]).split("m")[0]
            if uid not in replay:
                replay.append(uid)
    assert replay[0] == str(party["id"]) and party["id"] > max(s["id"] for s in solos)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g1 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g1["ai_status"] == "ok", g1["ai_error"]
    assert ai_queue._hard_rules_still_pass(db, g1) == (True, "")          # replay order == canonical allocation: the stored offer matches
    ga1 = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], a["id"])))
    assert pj(ga1["allocation_json"])["rooms"] == rooms, "the capture step did not rewrite the allocation"
    assert ai_queue.is_ready(db, g1)["ready"], ai_queue.is_ready(db, g1)


# ---------------------------------------------------------------- commitments: Introduced + all accepted through the real routes
def _swap_equivalent_rooms(alloc: dict) -> dict:
    """Exchange the occupants of two rooms with the same setup and the same occupant count - equally lawful, equally priced."""
    rooms = alloc["rooms"]
    for i in range(len(rooms)):
        for k in range(i + 1, len(rooms)):
            if rooms[i]["setup"] == rooms[k]["setup"] and len(rooms[i]["occupants"]) == len(rooms[k]["occupants"]) and rooms[i]["occupants"] != rooms[k]["occupants"]:
                out = json.loads(json.dumps(alloc))
                out["rooms"][i]["occupants"], out["rooms"][k]["occupants"] = rooms[k]["occupants"], rooms[i]["occupants"]
                return out
    raise AssertionError("no two equivalent rooms to swap")


def _commitment_state(db, gid):
    return (rows(db.execute("SELECT enquiry_id, room, bed, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,))),
            row(db.execute("SELECT status, allocation_json, total_weekly, weekly_rent, evidence_hash, lease_start, lease_end, apartment_id FROM groups WHERE id=?", (gid,))),
            rows(db.execute("SELECT id, apartment_id, rank, allocation_json, total_weekly FROM group_apartments WHERE group_id=? ORDER BY apartment_id", (gid,))),
            rows(db.execute("SELECT id, group_id, enquiry_id, response, superseded_at FROM group_responses WHERE group_id=? ORDER BY id", (gid,))),
            rows(db.execute("SELECT id, group_id, version, introduced_at, superseded_at FROM group_membership_versions WHERE group_id=? ORDER BY id", (gid,))))


def _introduced_and_accepted(client, monkeypatch):
    from tests.test_gp13_group_responses import _accept_all, _introduced, _state
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    st = _state(client, gid)
    assert st["all_accepted"] is True
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert g["status"] == "Introduced" and ai_queue.is_ready(db, g)["ready"]
    return db, gid


def test_legacy_non_canonical_allocation_of_an_introduced_accepted_group_is_held_not_rearranged(client, monkeypatch):
    """Upgrade behaviour (root ruling, 10 Nov 2026) on a commitment made through the real routes (introduced, every person
    accepted): a stored allocation the canonical search no longer reproduces is the existing fail-closed OfferDrift hold. No model
    call, no row of the offer, members, responses or versions changed, no re-insert, not ready, reason recorded for staff."""
    from server.db import pj
    db, gid = _introduced_and_accepted(client, monkeypatch)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, g["apartment_id"])))
    legacy = _swap_equivalent_rooms(pj(ga["allocation_json"]))
    db.execute("UPDATE group_apartments SET allocation_json=? WHERE group_id=? AND apartment_id=?", (json.dumps(legacy), gid, g["apartment_id"]))
    db.execute("UPDATE groups SET allocation_json=? WHERE id=?", (json.dumps(legacy), gid))
    for r in legacy["rooms"]:
        for o in r["occupants"]:
            if str(o["person_id"]).isdigit():
                db.execute("UPDATE group_members SET room=? WHERE group_id=? AND enquiry_id=?", (r["room"], gid, int(o["person_id"])))
    db.commit()
    before = _commitment_state(db, gid)
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    ok, why = ai_queue._hard_rules_still_pass(db, g2)
    assert not ok and "no longer matches" in why
    assert not ai_queue.is_ready(db, g2)["ready"]
    db.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (gid,)); db.commit()      # what requeue_stale would do after an upgrade
    calls = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: calls.append(1))
    ai_queue.process_pending(db)
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert not calls and g3["ai_status"] == "failed" and g3["ai_error"] == ai_queue.OFFER_DRIFT_REASON and g3["status"] == "Introduced"
    assert not ai_queue.is_ready(db, g3)["ready"] and not db.in_transaction
    assert _commitment_state(db, gid) == before, "introduced and accepted terms untouched"
    matching.full_run(db, "tester", dataset="test")                                          # Proposed groups only: the commitment is not re-checked
    assert _commitment_state(db, gid) == before
    assert row(db.execute("SELECT ai_error FROM groups WHERE id=?", (gid,)))["ai_error"] == ai_queue.OFFER_DRIFT_REASON


def test_canonical_allocation_of_an_introduced_accepted_group_stays_ready_and_untouched(client, monkeypatch):
    """Control: the same commitment with the allocation the product itself stored stays ready through a requeue and a full run."""
    from tests.test_t6_fail_closed import _good, _reply_builder
    db, gid = _introduced_and_accepted(client, monkeypatch)
    before = _commitment_state(db, gid)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert ai_queue._hard_rules_still_pass(db, g) == (True, "")
    db.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (gid,)); db.commit()
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert g2["ai_status"] == "ok" and g2["status"] == "Introduced" and ai_queue.is_ready(db, g2)["ready"], ai_queue.is_ready(db, g2)
    matching.full_run(db, "tester", dataset="test")
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert ai_queue.is_ready(db, g3)["ready"]
    after = _commitment_state(db, gid)
    assert after[0] == before[0] and after[2] == before[2] and after[3] == before[3] and after[4] == before[4]
    assert {k: after[1][k] for k in after[1].keys() if k != "evidence_hash"} == {k: before[1][k] for k in before[1].keys() if k != "evidence_hash"}
