"""G23 follow-up (9 Nov 2026, neutral review builder; proposal, not integrated): two members with the same first name must stay
tellable apart in staff prose. Observed on the live-1 snapshot: G23 members 143/193 both first-named Mateo rendered "Mateo and Mateo".
Rule: distinguishable full recorded names where available; otherwise the same words plus a neutral ordinal in saved-map order;
retired duplicates "a former member" / "another former member". Display only - no packet, selection, saved map or rule change;
never a raw U-code. Unique names are untouched (existing suites cover that)."""
import json

from server import staff_view
from tests.test_data_and_api import appdb, client, member  # noqa: F401  (fixtures re-used)
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _add_member, _submit


def _map(*eids):
    return json.dumps({f"U{i}": {"enquiry_id": e, "party_member_id": None} for i, e in enumerate(eids, 1)})


def test_same_first_name_different_surnames_show_full_recorded_names(client):
    a = _submit(client, first_name="Mateo", last_name="Rossi", email="m1@example.com")
    b = _submit(client, first_name="Mateo", last_name="Silva", email="m2@example.com")
    o = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    names = staff_view.names_from_refs(c, _map(a["id"], b["id"], o["id"]))
    assert names == {"U1": "Mateo Rossi", "U2": "Mateo Silva", "U3": "Nia"}   # only the colliding pair is widened
    text = staff_view.plain_text("U1 and U2 wake early; U3 prefers quiet.", names)
    assert text == "Mateo Rossi and Mateo Silva wake early; Nia prefers quiet." and "U1" not in text and "U2" not in text


def test_identical_full_names_get_a_neutral_ordinal_in_saved_map_order(client):
    a = _submit(client, first_name="Mateo", last_name="Rossi", email="m1@example.com")
    b = _submit(client, first_name="Mateo", last_name="Rossi", email="m2@example.com")
    c = appdb(client)
    assert staff_view.names_from_refs(c, _map(a["id"], b["id"])) == {"U1": "Mateo (first listed)", "U2": "Mateo (second listed)"}
    # the order follows the saved map, not the enquiry ids
    assert staff_view.names_from_refs(c, _map(b["id"], a["id"])) == {"U1": "Mateo (first listed)", "U2": "Mateo (second listed)"}
    out = staff_view.plain_text("U2 is tidier than U1.", staff_view.names_from_refs(c, _map(a["id"], b["id"])))
    assert out == "Mateo (second listed) is tidier than Mateo (first listed)."


def test_two_retired_people_in_a_saved_map_are_told_apart(client):
    a = _submit(client, first_name="Mateo", last_name="Rossi", email="m1@example.com")
    b = _submit(client, first_name="Mateo", last_name="Silva", email="m2@example.com")
    o = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    m = _map(a["id"], b["id"], o["id"])
    c.execute("DELETE FROM enquiries WHERE id IN (?, ?)", (a["id"], b["id"])); c.commit()
    names = staff_view.names_from_refs(c, m)
    assert names == {"U1": "a former member", "U2": "another former member", "U3": "Nia"}
    assert "Mateo" not in json.dumps(names)   # a deleted person's name never comes back through the widening


def test_party_member_sharing_the_leads_first_name_is_distinguished(client):
    a = _submit(client, first_name="Mateo", last_name="Rossi", email="m1@example.com")
    c = appdb(client)
    c.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?, 'Mateo', 'pm@example.com')", (a["id"],))
    pm = c.execute("SELECT id FROM party_members WHERE enquiry_id=?", (a["id"],)).fetchone()[0]
    c.commit()
    refs = json.dumps({"U1": {"enquiry_id": a["id"], "party_member_id": None}, "U1-m1": {"enquiry_id": a["id"], "party_member_id": pm}})
    names = staff_view.names_from_refs(c, refs)
    assert names["U1"] == "Mateo Rossi" and names["U1-m1"] == "Mateo"   # lead widened to the full recorded name; the companion keeps the one name recorded
    # a companion recorded under the lead's full name no longer collides with the lead's first name: both stay as recorded
    c.execute("UPDATE party_members SET name='Mateo Rossi' WHERE id=?", (pm,)); c.commit()
    assert staff_view.names_from_refs(c, refs) == {"U1": "Mateo", "U1-m1": "Mateo Rossi"}


def test_current_member_path_without_a_saved_map_is_widened_too(client):
    a = _submit(client, first_name="Mateo", last_name="Rossi", email="m1@example.com")
    b = _submit(client, first_name="Mateo", last_name="Silva", email="m2@example.com")
    c = appdb(client)
    gid = _plant_group(c, a["id"])
    _add_member(c, gid, b["id"])
    c.execute("UPDATE groups SET evidence_hash='h' WHERE id=?", (gid,)); c.commit()
    names = staff_view.ref_names(c, gid, "h", None)
    assert sorted(names.values()) == ["Mateo Rossi", "Mateo Silva"]
    assert isinstance(staff_view.ref_names(c, gid, "other", None), staff_view.EarlierMembers)   # stale hash: still withheld, not widened


def test_unique_names_are_returned_exactly_as_before(client):
    a = _submit(client, first_name="Oscar", last_name="Other", email="o@example.com")
    b = _submit(client, first_name="Nia", last_name="New", email="n@example.com")
    c = appdb(client)
    assert staff_view.names_from_refs(c, _map(a["id"], b["id"])) == {"U1": "Oscar", "U2": "Nia"}
    assert staff_view.distinct_names(c, {"U1": "Oscar", "U2": "Nia"}, {}) == {"U1": "Oscar", "U2": "Nia"}
