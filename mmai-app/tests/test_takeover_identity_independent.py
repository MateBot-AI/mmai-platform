"""Independent historical-attribution probes; synthetic DB, no provider."""
import json

from server import identities, staff_view
from server.db import retention_guard, row
from tests.test_t6_grounding import _party_group
from tests.test_uid_allocation import _uid_of
from tests.test_launch_neutral import _enq


def test_retired_uid_cannot_follow_two_slot_successors(db, monkeypatch):
    _, lead, _, _ = _party_group(db, monkeypatch)
    original = _uid_of(db, lead["id"])
    for number, name in enumerate(("New Person", "Later Person"), 1):
        with retention_guard(db, label="independent identity reset"):
            db.execute("UPDATE enquiries SET first_name=?,email=? WHERE id=?",
                       (name, f"new{number}@example.test", lead["id"]))
            identities.reconcile(db, lead["id"], "left",
                                 now=f"2090-01-0{number}T00:00:00+00:00")
    refs = json.dumps({"U1": {"enquiry_id": lead["id"], "party_member_id": None,
                             "person_uid": original}})
    assert staff_view.names_from_refs(db, refs, "2099-01-01T00:00:00Z") == {
        "U1": "an earlier member"}
    assert staff_view._live_identity(db, original) is None


def test_promoted_uid_duplicate_names_use_current_slots(db, monkeypatch):
    from server import matching
    _, lead, _, _ = _party_group(db, monkeypatch)
    bo = _uid_of(db, lead["id"], "Bo Berg")
    other = _enq(db, "Bo", "other-bo@example.test", budget_option="3B-single",
                 apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    with retention_guard(db, label="independent second Bo"):
        db.execute("UPDATE enquiries SET last_name='Different' WHERE id=?", (other["id"],))
        identities.reconcile(db, other["id"], "left")
    other_uid = _uid_of(db, other["id"])
    old_party = row(db.execute(
        "SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'",
        (lead["id"],)))["id"]
    matching.bar_enquiry(db, lead["id"], "staff", "test", person="lead", party_choice="stay")
    refs = json.dumps({
        "U1-m1": {"enquiry_id": other["id"], "party_member_id": old_party, "person_uid": bo},
        "U2": {"enquiry_id": lead["id"], "party_member_id": None, "person_uid": other_uid},
    })
    assert staff_view.names_from_refs(db, refs) == {
        "U1-m1": "Bo Berg", "U2": "Bo Different"}


def test_equal_hash_without_refs_is_not_identity_proof(db, monkeypatch):
    _, lead, _, group = _party_group(db, monkeypatch)
    before = row(db.execute("SELECT evidence_hash FROM groups WHERE id=?", (group["id"],)))["evidence_hash"]
    with retention_guard(db, label="independent identity edit"):
        db.execute("UPDATE enquiries SET first_name='Replacement',email='replacement@example.test' WHERE id=?",
                   (lead["id"],))
        identities.reconcile(db, lead["id"], "left", now="2090-01-01T00:00:00+00:00")
    assert isinstance(staff_view.ref_names(
        db, group["id"], evidence_hash=before, written_at="2089-12-31T23:59:59Z"),
        staff_view.EarlierMembers)
