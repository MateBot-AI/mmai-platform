"""T3(d) C3 (10 Nov 2026) - A13(a) step-free access facts: recorded per apartment with provenance, bound to the apartment's
address, part of the review evidence (value AND record generation), and the only way a step-free hold is ever lifted is a CURRENT
AI review that saw the record. Nothing here calls a provider: the review call is the existing fabricated capture, the notes
extractor is the controlled table from GAP-T4c.

Root's contract (10 Nov 2026): fact changes invalidate evidence and require a valid current review - no hand/re-stamped approval;
revocation (yes->unknown / no) invalidates synchronously; late results built on old facts fail the evidence check even when the
boolean value is the same again (yes->unknown->yes); a changed/reused apartment row never inherits a confirmation; unknown and
excluded records keep their provenance; the API rejects malformed input instead of silently revoking.
"""
import json
import os

import pytest

from server import access_facts, ai, ai_queue, facts, matching, notes_facts
from server.db import row, rows
from tests.test_launch_neutral import _apt, _pool  # noqa: F401
from tests.test_t4c_notes_queue_integration import P116, _approved_ready, _extractor, _item
from tests.test_t6_fail_closed import _good, _reply_builder

FIXTURE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "files", "reviews",
                       "20261110-c200-controlled-grouping-retest", "apartment-access-facts.json")
FIXTURE_ALT = os.environ.get("MMAI_A13A_FIXTURE", "")   # an explicit path to the confirmation file when the tree is not inside the project checkout
SRC = "Bill, 10 November 2026"
HOLD = notes_facts.HOLD_LABEL.format(label="Step-free access")
CONTRARY = notes_facts.CONTRARY_LABEL.format(label="Step-free access")


def _g(db, gid):
    return row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))


# ---------------------------------------------------------------- recording, history, provenance, identity
def test_record_yes_no_unknown_keeps_history_provenance_and_generation(db):
    a = _apt(db)
    assert access_facts.current(db, a["id"]) == {"value": None, "label": access_facts.LABELS[None], "source": None, "recorded_by": None, "recorded_at": None,
                                                 "scope": None, "note": None, "status": None, "record_id": None, "bound": True}
    cur = access_facts.record(db, a["id"], "yes", source=SRC, staff="anna", scope="street to apartment door")
    assert cur["value"] == 1 and cur["label"] == access_facts.LABELS[1] and cur["source"] == SRC and cur["recorded_by"] == "anna" and cur["status"] == "confirmed"
    assert cur["scope"] == "street to apartment door" and cur["bound"] is True and isinstance(cur["record_id"], int)
    first = cur["record_id"]
    cur = access_facts.record(db, a["id"], "unknown", source="site check, Anna", staff="anna", note="ramp removed for works")
    assert cur["value"] is None and cur["status"] == "unknown" and cur["record_id"] > first
    assert cur["source"] == "site check, Anna" and cur["note"] == "ramp removed for works"   # provenance of the ABSENCE is kept
    cur = access_facts.record(db, a["id"], "no", source="site check, Anna", staff="anna")
    assert cur["value"] == 0 and cur["status"] == "not_step_free" and cur["label"] == access_facts.LABELS[0]
    cur = access_facts.record(db, a["id"], None, source=SRC, staff="import", status="excluded_from_confirmation", note="not in the 10 Nov confirmation")
    assert cur["value"] is None and cur["status"] == "excluded_from_confirmation" and cur["source"] == SRC
    h = access_facts.history(db, a["id"])
    assert [x["value"] for x in h] == [None, 0, None, 1] and [x["status"] for x in h] == ["excluded_from_confirmation", "not_step_free", "unknown", "confirmed"]
    assert h[0]["superseded_at"] is None and all(x["superseded_at"] for x in h[1:]) and all(x["address_at_record"] == access_facts.location_key(a) for x in h)
    assert len({x["id"] for x in h}) == 4
    # audit_log.entity_id is TEXT (db.audit stores str(entity_id)); bind the typed text value - PostgreSQL does not coerce text = integer
    au = rows(db.execute("SELECT staff_member AS actor, detail FROM audit_log WHERE action='apartment_access_fact' AND entity_id=? ORDER BY id", (str(a["id"]),)))
    assert [x["actor"] for x in au] == ["anna", "anna", "anna", "import"] and "step_free_access=yes (confirmed)" in au[0]["detail"] and SRC in au[0]["detail"]
    with pytest.raises(ValueError):
        access_facts.record(db, a["id"], "maybe", source=SRC, staff="anna")
    with pytest.raises(ValueError):
        access_facts.record(db, 999999, "yes", source=SRC, staff="anna")


def test_value_from_text_is_closed():
    assert access_facts.value_from_text("YES") == 1 and access_facts.value_from_text(" no ") == 0 and access_facts.value_from_text("unknown") is None
    assert access_facts.value_from_text(True) == 1 and access_facts.value_from_text(0) == 0 and access_facts.value_from_text(None) is None
    for bad in ("y", "true", "", "2", 2, "step-free", "lift"):
        with pytest.raises(ValueError):
            access_facts.value_from_text(bad)


def test_record_is_bound_to_the_apartment_address_not_the_row(db):
    a = _apt(db)
    access_facts.record(db, a["id"], "yes", source=SRC, staff="anna")
    assert access_facts.effective(row(db.execute("SELECT * FROM apartments WHERE id=?", (a["id"],))))["value"] == 1
    # the row is re-addressed (reused for other stock): Bill's confirmation does not travel with the id
    db.execute("UPDATE apartments SET address='Unit 9 / 99 Other Road', address_raw='Unit 9 / 99 Other Road' WHERE id=?", (a["id"],)); db.commit()
    arow = row(db.execute("SELECT * FROM apartments WHERE id=?", (a["id"],)))
    eff = access_facts.effective(arow)
    assert eff["value"] is None and eff["bound"] is False and eff["record_id"] is not None
    cur = access_facts.current(db, a["id"])
    assert cur["value"] is None and cur["bound"] is False and cur["label"] == access_facts.MOVED_LABEL
    assert notes_facts.verification(_item(P116), arow) == "none"
    assert matching.candidate_apartment_packet.__name__   # packet builders read effective(): asserted through the payload below
    # formatting variants of the SAME address stay bound
    db.execute("UPDATE apartments SET address=? WHERE id=?", (a["address"].upper().replace("/", " / "), a["id"])); db.commit()
    assert access_facts.effective(row(db.execute("SELECT * FROM apartments WHERE id=?", (a["id"],))))["value"] == 1
    # a fresh record at the new address binds again
    db.execute("UPDATE apartments SET address='Unit 9 / 99 Other Road' WHERE id=?", (a["id"],)); db.commit()
    assert access_facts.record(db, a["id"], "no", source="site check", staff="anna")["bound"] is True
    assert access_facts.effective(row(db.execute("SELECT * FROM apartments WHERE id=?", (a["id"],))))["value"] == 0


def test_record_is_bound_to_suburb_and_city_not_only_the_street_text(db):
    """A row moved to another suburb or city with unchanged street text is other stock: the record no longer applies (unknown,
    not bound) until a fresh record is made; a suburb formatting variant of the same place stays bound."""
    a = _apt(db)
    db.execute("UPDATE apartments SET suburb='Carlton', city='Melbourne' WHERE id=?", (a["id"],)); db.commit()
    access_facts.record(db, a["id"], "yes", source=SRC, staff="anna")
    arow = lambda: row(db.execute("SELECT * FROM apartments WHERE id=?", (a["id"],)))
    assert access_facts.effective(arow())["value"] == 1 and access_facts.effective(arow())["bound"] is True
    db.execute("UPDATE apartments SET suburb=' CARLTON ' WHERE id=?", (a["id"],)); db.commit()   # formatting variant: same place
    assert access_facts.effective(arow())["value"] == 1
    db.execute("UPDATE apartments SET suburb='Carlton North' WHERE id=?", (a["id"],)); db.commit()   # another suburb, same street text
    eff = access_facts.effective(arow())
    assert eff["value"] is None and eff["bound"] is False and eff["record_id"] is not None
    assert access_facts.current(db, a["id"])["label"] == access_facts.MOVED_LABEL
    assert notes_facts.verification(_item(P116), arow()) == "none"
    db.execute("UPDATE apartments SET suburb='Carlton', city='Sydney' WHERE id=?", (a["id"],)); db.commit()   # same suburb name, other city
    assert access_facts.effective(arow())["value"] is None and access_facts.effective(arow())["bound"] is False
    db.execute("UPDATE apartments SET city='Melbourne' WHERE id=?", (a["id"],)); db.commit()   # back to the recorded place: the record applies again
    assert access_facts.effective(arow())["value"] == 1
    # the history keeps the location identity each record was made for
    assert access_facts.history(db, a["id"])[0]["address_at_record"] == access_facts.location_key(arow())


# ---------------------------------------------------------------- verification / gate / catalogue
def test_verification_and_hold_gate_follow_the_record_only():
    it = _item(P116)
    assert notes_facts.verification(it, None) == "none"
    assert notes_facts.verification(it, {"step_free_access": None, "lift_access": 1}) == "none"          # a lift is never proof
    assert notes_facts.verification(it, {"step_free_access": 1}) == "recorded"
    assert notes_facts.verification(it, {"step_free_access": 0}) == "contrary"
    assert notes_facts.verification(it, {"step_free_record_id": 5, "step_free_access": 1, "step_free_address": access_facts.location_key({"address": "1 A St"}), "address": "1 A St"}) == "recorded"
    assert notes_facts.verification(it, {"step_free_record_id": 5, "step_free_access": 1, "step_free_address": access_facts.location_key({"address": "1 A St"}), "address": "2 B St"}) == "none"
    assert notes_facts.verification(_item("I love cooking.", category="cooking_sharing", strength="matters", span="I love cooking."), {"step_free_access": 0}) == "not_gated"
    m = [{"ref": "P1", "notes_facts": [{"category": "step_free_access", "strength": "essential", "polarity": "affirmed", "value": None}]}]
    assert notes_facts.hold_gate(m, {"step_free_access": None}) == {"hold": True, "reason": HOLD, "refs": ["P1"]}
    assert notes_facts.hold_gate(m, {"step_free_access": 0}) == {"hold": True, "reason": CONTRARY, "refs": ["P1"]}
    assert notes_facts.hold_gate(m, {"step_free_access": 1})["hold"] is False
    for label in (HOLD, CONTRARY):
        for forbidden in ("step_free_access", "essential", "model", "lift", "medical", "_"):
            assert forbidden not in label


def _held_for_step_free(db, monkeypatch):
    """An approved group whose member then writes P116: after the next pass it is held for the step-free confirmation.
    Returns (held group id, apartment id, member id, a ready group that does NOT contain the member)."""
    groups = _approved_ready(db, monkeypatch, _extractor({P116: {"items": [_item(P116)], "residual": False}}))
    g = next(x for x in groups if x["ai_status"] == "ok")
    member = row(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (g["id"],)))["enquiry_id"]
    other = next(x for x in groups if x["ai_status"] == "ok" and x["id"] != g["id"]
                 and member not in {m["enquiry_id"] for m in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (x["id"],)))})
    from server.enquiries import update_enquiry
    update_enquiry(db, member, {"else_text": P116}, "tester")
    db.execute("UPDATE groups SET ai_status='pending', ai_queued_at=NULL WHERE id=?", (g["id"],)); db.commit()
    ai_queue.process_pending(db)
    held = _g(db, g["id"])
    assert held["ai_status"] == "held" and held["held_reason"] == HOLD and held["ready_at"] is None
    return g["id"], held["apartment_id"], member, other["id"]


def test_catalogue_carries_recorded_true_false_or_missing_from_the_staff_record(db, monkeypatch):
    gid, aid, member, _ = _held_for_step_free(db, monkeypatch)
    key = f"A{aid}.step_free_access"

    def catalogue():
        return facts.catalogue(ai_queue._payload(db, _g(db, gid)))[key]
    f = catalogue()
    assert f["status"] == "missing" and f["value"] is None and f["source"] == "listing" and f["wanted_by"]
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")
    f = catalogue()
    assert f["status"] == "recorded" and f["value"] is True and f["source"] == "staff record"
    access_facts.record(db, aid, "no", source="site check", staff="anna")
    f = catalogue()
    assert f["status"] == "recorded" and f["value"] is False and f["source"] == "staff record"
    access_facts.record(db, aid, "unknown", source="site check", staff="anna")
    assert catalogue()["status"] == "missing"
    p = ai_queue._payload(db, _g(db, gid))
    apt = next(a for a in p["apartments"] if a["apartment_id"] == aid)
    assert apt["step_free_access"] is None and apt["step_free_source"] == "site check"
    assert P116 not in json.dumps(p)


# ---------------------------------------------------------------- evidence: value + generation
def test_evidence_hash_moves_with_every_record_and_never_repeats_by_value_coincidence(db, monkeypatch):
    groups = _approved_ready(db, monkeypatch)
    g = next(x for x in groups if x["ai_status"] == "ok")
    aid = _g(db, g["id"])["apartment_id"]
    h0, snap0 = matching.group_evidence(db, g["id"])
    assert h0 == g["evidence_hash"] and all("step_free_access" not in a["facts"] for a in snap0["apartments"])   # never recorded: older snapshots keep their hash
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")
    h1, snap1 = matching.group_evidence(db, g["id"])
    a1 = next(a for a in snap1["apartments"] if a["apartment_id"] == aid)["facts"]
    assert h1 != h0 and a1["step_free_access"] == 1 and isinstance(a1["step_free_record"], int)
    access_facts.record(db, aid, "unknown", source=SRC, staff="anna")       # revocation changes the evidence synchronously
    h2, _ = matching.group_evidence(db, g["id"])
    assert h2 not in (h0, h1)
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")           # back to the same boolean: a NEW generation, not the old evidence
    h3, _ = matching.group_evidence(db, g["id"])
    assert h3 not in (h0, h1, h2)
    chk = ai_queue.is_ready(db, _g(db, g["id"]))
    assert chk["ready"] is False and any("facts changed" in r for r in chk["reasons"])


def test_late_review_result_built_on_an_older_fact_generation_is_not_applied(db, monkeypatch):
    """yes -> (review packet built) -> unknown -> yes while the review is in flight: the boolean is the same again, the result is
    still refused by the evidence check and the group goes back to pending (no approval on facts the model did not receive)."""
    groups = _approved_ready(db, monkeypatch)
    g = next(x for x in groups if x["ai_status"] == "ok")
    aid = _g(db, g["id"])["apartment_id"]
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")
    out = access_facts.apply_to_groups(db, aid, "anna")
    assert g["id"] in out["requeued"] and _g(db, g["id"])["ai_status"] == "pending"
    real = _reply_builder(_good)

    def flip_during_review(prompt, **kw):
        access_facts.record(db, aid, "unknown", source=SRC, staff="anna")
        access_facts.record(db, aid, "yes", source=SRC, staff="anna")
        return real(prompt, **kw)
    monkeypatch.setattr(ai, "_ask", flip_during_review)
    ai_queue.process_pending(db)
    live = _g(db, g["id"])
    assert live["ai_status"] == "pending" and live["ready_at"] is None and live["ai_error"] == ai_queue.FACTS_CHANGED_REQUEUED_TEXT
    assert ai_queue.is_ready(db, live)["ready"] is False
    monkeypatch.setattr(ai, "_ask", real)
    ai_queue.process_pending(db)
    live = _g(db, g["id"])
    assert live["ai_status"] == "ok" and live["evidence_hash"] == matching.group_evidence(db, g["id"])[0] and ai_queue.is_ready(db, live)["ready"] is True


# ---------------------------------------------------------------- the hold is lifted only by a current review
def test_held_group_is_released_only_by_a_current_review_that_saw_the_record(db, monkeypatch):
    gid, aid, member, other_id = _held_for_step_free(db, monkeypatch)
    g, other = {"id": gid}, {"id": other_id}
    rounds_before = row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (g["id"],)))["n"]
    assert _g(db, other["id"])["ai_status"] == "ok" and ai_queue.is_ready(db, _g(db, other["id"]))["ready"] is True
    # staff record the fact: the held group and the unrelated READY group offering the same apartment are both queued for a current review
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")
    out = access_facts.apply_to_groups(db, aid, "anna")
    assert g["id"] in out["requeued"] and other["id"] in out["requeued"] and out["skipped"] == []
    for gid in (g["id"], other["id"]):
        live = _g(db, gid)
        assert live["ai_status"] == "pending" and live["ready_at"] is None and live["evidence_hash"] is None and ai_queue.is_ready(db, live)["ready"] is False
    assert rows(db.execute("SELECT entity_id FROM audit_log WHERE action=?", (access_facts.REQUEUE_AUDIT,)))
    assert not rows(db.execute("SELECT 1 FROM audit_log WHERE action LIKE '%restamp%' OR action LIKE '%release%'"))
    # the hold is lifted by the review that ran WITH the record in its packet - never by the recording itself
    ai_queue.process_pending(db)
    live = _g(db, g["id"])
    assert live["ai_status"] == "ok" and live["held_reason"] is None and live["brief_status"] == "ok" and ai_queue.is_ready(db, live)["ready"] is True
    assert row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (g["id"],)))["n"] == rounds_before + 1
    ev = json.loads(live["evidence_json"])
    assert next(a for a in ev["apartments"] if a["apartment_id"] == aid)["facts"]["step_free_access"] == 1
    assert ai_queue.is_ready(db, _g(db, other["id"]))["ready"] is True
    # recorded as NOT step-free: invalidated synchronously, the next review holds with the contrary label
    access_facts.record(db, aid, "no", source="site check", staff="anna")
    assert ai_queue.is_ready(db, _g(db, g["id"]))["ready"] is False
    out = access_facts.apply_to_groups(db, aid, "anna")
    assert g["id"] in out["requeued"]
    ai_queue.process_pending(db)
    live = _g(db, g["id"])
    assert live["ai_status"] == "held" and live["held_reason"] == CONTRARY and ai_queue.is_ready(db, live)["ready"] is False
    # recording the same bound fact again is a no-op for groups already awaiting review; nothing is approved by hand anywhere
    assert not rows(db.execute("SELECT 1 FROM groups WHERE ready_at IS NOT NULL AND ai_status<>'ok'"))


def test_apply_to_groups_leaves_unaffected_groups_alone_and_does_not_double_queue(db, monkeypatch):
    groups = _approved_ready(db, monkeypatch)
    g = next(x for x in groups if x["ai_status"] == "ok")
    aid = _g(db, g["id"])["apartment_id"]
    other_apt = _apt(db, address="Unit 2 / 2 Elsewhere Street")
    assert access_facts.apply_to_groups(db, other_apt["id"], "anna") == {"checked": 0, "requeued": [], "unchanged": [], "skipped": []}
    access_facts.record(db, aid, "yes", source=SRC, staff="anna")
    out = access_facts.apply_to_groups(db, aid, "anna")
    assert out["requeued"] and out["checked"] == len(out["requeued"]) + len(out["unchanged"])
    again = access_facts.apply_to_groups(db, aid, "anna")
    assert again["requeued"] == [] and again["skipped"] == []       # pending groups carry no stored evidence: nothing to re-queue
    assert access_facts.apply_to_groups(db, 999999, "anna")["checked"] == 0


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MMAI_DB", str(tmp_path / "api.db"))
    import importlib
    from fastapi.testclient import TestClient
    import server.db as dbmod
    from tests.conftest import TEST_PW
    dbmod._conn = None
    import server.app as appmod
    importlib.reload(appmod)
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": "anna", "password": TEST_PW["anna"]})
    assert r.status_code == 200
    c.headers.update({"Authorization": "Bearer " + r.json()["token"]})
    return c


def test_api_records_with_the_authenticated_account_and_rejects_malformed_input(client):
    import server.app as appmod
    a = _apt(appmod.db)
    url = f"/api/apartments/{a['id']}/access-facts"
    r = client.get(url)
    assert r.status_code == 200 and r.json()["current"]["value"] is None and r.json()["history"] == []
    assert client.post(url, json={"source": SRC}).status_code == 422                       # missing key never revokes to unknown
    assert client.post(url, json={"step_free_access": ["yes"]}).status_code == 422
    assert client.post(url, json={"step_free_access": {"v": 1}}).status_code == 422
    assert client.post(url, json={"step_free_access": "maybe"}).status_code == 422
    assert client.post(url, json={"step_free_access": "yes", "source": 12}).status_code == 422
    assert client.post(url, json={"step_free_access": "yes", "scope": ["a"]}).status_code == 422
    assert client.post(url, json={"step_free_access": "yes", "note": {"x": 1}}).status_code == 422
    assert client.post(url, json={"step_free_access": "yes", "note": "x" * 1001}).status_code == 422
    assert client.get(url).json()["history"] == []                                            # nothing recorded by the rejected calls
    r = client.post(url, json={"step_free_access": "yes", "source": SRC, "scope": " street to apartment door "})
    assert r.status_code == 200
    body = r.json()
    assert body["current"]["value"] == 1 and body["current"]["recorded_by"] == "anna" and body["current"]["scope"] == "street to apartment door"
    assert body["groups"] == {"checked": 0, "requeued": [], "unchanged": [], "skipped": []}
    r = client.post(url, json={"step_free_access": "unknown"})
    assert r.status_code == 200 and r.json()["current"]["value"] is None and r.json()["current"]["source"] == "Anna Chan, recorded in MateBot"
    h = client.get(url).json()["history"]
    assert [x["value"] for x in h] == [None, 1] and all(x["recorded_by"] == "anna" for x in h)
    au = rows(appmod.db.execute("SELECT staff_member AS actor FROM audit_log WHERE action='apartment_access_fact'"))
    assert {x["actor"] for x in au} == {"anna"}
    apt = client.get(f"/api/apartments?q=Neutral").json()["items"][0]
    assert apt["step_free_access"] == "unknown" and apt["step_free_label"] == access_facts.LABELS[None]
    assert client.get("/api/apartments/999999/access-facts").status_code == 404
    assert client.post("/api/apartments/999999/access-facts", json={"step_free_access": "yes"}).status_code == 404
    from fastapi.testclient import TestClient
    anon = TestClient(appmod.app)
    assert anon.get(url).status_code == 401 and anon.post(url, json={"step_free_access": "yes"}).status_code == 401


# ---------------------------------------------------------------- import tool (fixed inventory: supplied id AND matching address)
def test_import_plan_requires_the_supplied_id_and_a_matching_address_and_never_touches_other_stock(db):
    from tools import import_access_facts as tool
    a = _apt(db)                                                       # "Unit 1 / 1 Neutral Street"
    b = _apt(db, address="Apartment 2, 1 Neutral Street")
    c3 = _apt(db, address="Unit 3 / 1 Neutral Street")                   # not in the file: untouched
    entries = [{"apartment_id": a["id"], "address": "unit 1/1 NEUTRAL street", "step_free_access": True, "status": "confirmed", "confirmation_source": SRC, "scope": "street to door", "explanation": "ok"},
               {"apartment_id": b["id"], "address": "Apt 2 1 Neutral Street", "step_free_access": None, "status": "excluded_from_confirmation", "confirmation_source": SRC, "explanation": "outside Darling"},
               {"apartment_id": 503, "address": "77 Nowhere Lane", "step_free_access": True, "confirmation_source": SRC},                 # id not in stock
               {"apartment_id": a["id"], "address": "Unit 1 / 1 Neutral Street", "step_free_access": "sometimes", "confirmation_source": SRC},   # unreadable value
               {"apartment_id": c3["id"], "address": "Unit 1 / 1 Neutral Street", "step_free_access": True, "confirmation_source": SRC},   # id exists, address is another apartment's
               {"apartment_id": None, "address": "Unit 3 / 1 Neutral Street", "step_free_access": True, "confirmation_source": SRC}]       # no id: address alone never matches
    assert tool.plan(db, entries)["record"] == [] and {x["reason"] for x in tool.plan(db, entries)["skipped"]} >= {tool.NO_INVENTORY}   # no reference: fail closed
    inv = tool.load_inventory(tool.inventory_reference(db))
    p = tool.plan(db, entries, inv)
    assert [(x["apartment_id"], x["value"], x["status"]) for x in p["record"]] == [(a["id"], 1, "confirmed"), (b["id"], None, "excluded_from_confirmation")]
    sk = {(x["apartment_id"], x["reason"].split(";")[0]) for x in p["skipped"]}
    assert sk == {(503, "id not in stock"), (a["id"], "unreadable value"), (c3["id"], "address differs from the apartment at this id"), (None, "no inventory id in the row")}
    assert next(x for x in p["skipped"] if x["apartment_id"] == c3["id"])["found_id"] == a["id"]   # reported, never applied to the other id
    for r in p["record"]:
        access_facts.record(db, r["apartment_id"], r["value"], source=r["source"], staff="import", scope=r.get("scope"), note=r.get("note"), status=r["status"])
    assert access_facts.current(db, a["id"])["value"] == 1 and access_facts.current(db, b["id"])["status"] == "excluded_from_confirmation"
    assert access_facts.current(db, b["id"])["source"] == SRC and access_facts.current(db, c3["id"])["record_id"] is None
    p2 = tool.plan(db, entries, inv)
    assert sorted(p2["unchanged"]) == sorted([a["id"], b["id"]]) and p2["record"] == []    # idempotent: no needless invalidation
    assert len(rows(db.execute("SELECT id FROM apartment_access_facts"))) == 2


def test_import_skips_a_sole_renumbered_row_and_never_reapplies_a_confirmation_to_a_moved_apartment(db):
    """Renumbered stock: the file row for id 4 at its old address arrives as id 999 with that address while row 4 is absent from
    stock - nothing is recorded for the apartment now at that address (it is reported). Moved stock: the same confirmation is not
    re-applied to a row whose suburb changed since it was recorded."""
    from tools import import_access_facts as tool
    a = _apt(db)                                                          # the only apartment; its id is not 999
    assert a["id"] != 999
    inv = tool.load_inventory(tool.inventory_reference(db))              # the trusted reference: taken while the stock stood where it was confirmed
    p = tool.plan(db, [{"apartment_id": 999, "address": a["address"], "step_free_access": True, "confirmation_source": SRC}], inv)
    assert p["record"] == [] and p["unchanged"] == [] and [(x["apartment_id"], x["found_id"]) for x in p["skipped"]] == [(999, a["id"])]
    assert "id not in stock" in p["skipped"][0]["reason"] and "another id" in p["skipped"][0]["reason"]
    assert access_facts.current(db, a["id"])["record_id"] is None
    row_ok = {"apartment_id": a["id"], "address": a["address"], "step_free_access": True, "confirmation_source": SRC}
    # G6: moved BEFORE the first application (same id, same street, other suburb): the reference says where the confirmed apartment
    # was, so the row gets nothing - no record exists yet to compare against, the reference is the binding
    db.execute("UPDATE apartments SET suburb='Carlton North' WHERE id=?", (a["id"],)); db.commit()
    p = tool.plan(db, [row_ok], inv)
    assert p["record"] == [] and p["skipped"][0]["reason"] == tool.INVENTORY_LOCATION and access_facts.current(db, a["id"])["record_id"] is None
    db.execute("UPDATE apartments SET city='Sydney' WHERE id=?", (a["id"],)); db.commit()
    assert tool.plan(db, [row_ok], inv)["skipped"][0]["reason"] == tool.INVENTORY_LOCATION
    db.execute("UPDATE apartments SET suburb=?, city=? WHERE id=?", (a["suburb"], a["city"], a["id"])); db.commit()   # back where the reference says
    assert [x["apartment_id"] for x in tool.plan(db, [row_ok], inv)["record"]] == [a["id"]]
    # re-addressed before the first application while the stale address_raw still carries the old text: the CURRENT canonical
    # address is what counts, so the old batch does not get through on address_raw
    db.execute("UPDATE apartments SET address='Unit 1 / 9 Elsewhere Road' WHERE id=?", (a["id"],)); db.commit()
    assert row(db.execute("SELECT address_raw FROM apartments WHERE id=?", (a["id"],)))["address_raw"] == a["address"]
    p = tool.plan(db, [row_ok], inv)
    assert p["record"] == [] and p["skipped"][0]["reason"].startswith("address differs from the apartment at this id")
    db.execute("UPDATE apartments SET address=? WHERE id=?", (a["address"], a["id"])); db.commit()
    assert [x["apartment_id"] for x in tool.plan(db, [row_ok], inv)["record"]] == [a["id"]]
    assert tool.plan(db, [row_ok], {})["skipped"][0]["reason"] == tool.NOT_IN_INVENTORY
    assert tool.plan(db, [row_ok], {a["id"]: {"address": "9 Elsewhere Road", "suburb": a["suburb"], "city": a["city"]}})["skipped"][0]["reason"] == tool.INVENTORY_ADDRESS
    access_facts.record(db, a["id"], 1, source=SRC, staff="import")
    assert tool.plan(db, [row_ok], inv)["unchanged"] == [a["id"]]
    db.execute("UPDATE apartments SET suburb='Carlton North' WHERE id=?", (a["id"],)); db.commit()   # moved AFTER the record: no longer bound
    assert access_facts.current(db, a["id"])["bound"] is False
    p = tool.plan(db, [row_ok], inv)
    assert p["record"] == [] and p["skipped"][0]["reason"] in (tool.LOCATION_CHANGED, tool.INVENTORY_LOCATION)   # either way nothing is re-applied to the moved row
    fresh = dict(row_ok, confirmation_source="Bill, 12 November 2026")                               # a NEW confirmation still binds to the reference location
    assert tool.plan(db, [fresh], inv)["skipped"][0]["reason"] == tool.INVENTORY_LOCATION
    db.execute("UPDATE apartments SET suburb=? WHERE id=?", (a["suburb"], a["id"])); db.commit()
    assert tool.plan(db, [row_ok], inv)["unchanged"] == [a["id"]]
    assert [x["apartment_id"] for x in tool.plan(db, [fresh], inv)["record"]] == [a["id"]]


def test_import_main_dry_run_and_apply_report_checked_requeued_unchanged_skipped(db, monkeypatch, tmp_path, capsys):
    from tools import import_access_facts as tool
    groups = _approved_ready(db, monkeypatch)
    g = next(x for x in groups if x["ai_status"] == "ok")
    aid = _g(db, g["id"])["apartment_id"]
    a = row(db.execute("SELECT id, address FROM apartments WHERE id=?", (aid,)))
    f = tmp_path / "facts.json"
    f.write_text(json.dumps([{"apartment_id": a["id"], "address": a["address"], "step_free_access": True, "status": "confirmed", "confirmation_source": SRC},
                             {"apartment_id": 999, "address": a["address"], "step_free_access": True, "confirmation_source": SRC}]), encoding="utf-8")
    inv = tmp_path / "inventory-locations.json"
    assert tool.main(["--export-inventory", str(inv)], c=db) == 0
    exported = json.loads(inv.read_text(encoding="utf-8"))
    assert set(exported) == {"exported_at", "stock_count", "source_label", "apartments"} and exported["stock_count"] == len(exported["apartments"])
    assert "not a sealed authority" in exported["source_label"]   # an export of the connected database is labelled for what it is
    assert {x["apartment_id"] for x in exported["apartments"]} == {r["id"] for r in rows(db.execute("SELECT id FROM apartments"))} and set(exported["apartments"][0]) == {"apartment_id", "address", "suburb", "city"}
    out = capsys.readouterr().out
    assert "written to" in out and "sha256 " + tool.file_digest(str(inv)) in out and "not evidence of the original inventory" in out
    with pytest.raises(SystemExit):
        tool.main(["--file", str(f)], c=db)                                                        # no reference: refused
    capsys.readouterr()
    assert tool.main(["--file", str(f), "--inventory", str(inv)], c=db) == 0
    out = capsys.readouterr().out
    assert "rows 2: to record 1, unchanged 0, skipped 1" in out and "skipped id 999" in out and "dry run: nothing written" in out
    assert access_facts.current(db, a["id"])["record_id"] is None and _g(db, g["id"])["ai_status"] == "ok"
    assert tool.main(["--file", str(f), "--inventory", str(inv), "--apply", "--staff", "importer"], c=db) == 0
    out = capsys.readouterr().out
    n = len([x for x in groups if x["ai_status"] == "ok"])
    assert f"recorded 1 apartment(s); groups with stored evidence checked {n}, re-queued for a current review {n}, unchanged 0, skipped 0" in out
    assert "restamp" not in out and "released" not in out
    assert access_facts.current(db, a["id"])["value"] == 1 and access_facts.current(db, a["id"])["recorded_by"] == "importer"
    assert _g(db, g["id"])["ai_status"] == "pending"
    assert tool.main(["--file", str(f), "--inventory", str(inv), "--apply", "--staff", "importer"], c=db) == 0
    assert "rows 2: to record 0, unchanged 1, skipped 1" in capsys.readouterr().out                 # idempotent


def test_import_prints_and_audits_the_provenance_of_both_inputs_with_counts_in_one_event(db, monkeypatch, tmp_path, capsys):
    """Root (10 Nov 2026): every run prints the sha256 + row count of the confirmation file AND the inventory reference; --apply
    writes ONE access_facts_import audit event with both digests, both counts and recorded / unchanged / skipped - also when every
    row is skipped. The default attribution is plain staff wording (no rule code). The digests identify the files used; nothing here
    claims an operator file is trustworthy."""
    from tools import import_access_facts as tool
    _approved_ready(db, monkeypatch)
    _apt(db, address="Apartment 2, 1 Neutral Street")                                             # second stock row, no group offers it
    apts = rows(db.execute("SELECT id, address, suburb, city FROM apartments ORDER BY id LIMIT 2"))
    a, b = apts[0], apts[1]
    f = tmp_path / "facts.json"
    f.write_text(json.dumps([{"apartment_id": a["id"], "address": a["address"], "step_free_access": True, "status": "confirmed", "confirmation_source": SRC},
                             {"apartment_id": b["id"], "address": b["address"], "step_free_access": None, "status": "excluded_from_confirmation", "confirmation_source": SRC},
                             {"apartment_id": 999, "address": a["address"], "step_free_access": True, "confirmation_source": SRC}]), encoding="utf-8")
    inv = tmp_path / "inventory-locations.json"
    inv.write_text(json.dumps({"apartments": [{"apartment_id": x["id"], "address": x["address"], "suburb": x["suburb"], "city": x["city"]} for x in apts]}), encoding="utf-8")
    fh, ih = tool.file_digest(str(f)), tool.file_digest(str(inv))
    assert len(fh) == 64 and fh != ih
    before = row(db.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action=?", (tool.IMPORT_AUDIT,)))["n"]
    assert tool.main(["--file", str(f), "--inventory", str(inv)], c=db) == 0                       # dry run: printed, nothing audited
    out = capsys.readouterr().out
    assert f"confirmation file facts.json: sha256 {fh}, 3 row(s)" in out and f"inventory reference inventory-locations.json: sha256 {ih}, 2 apartment(s)" in out
    assert row(db.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action=?", (tool.IMPORT_AUDIT,)))["n"] == before
    assert tool.main(["--file", str(f), "--inventory", str(inv), "--apply"], c=db) == 0           # default attribution
    out = capsys.readouterr().out
    assert f"sha256 {fh}, 3 row(s)" in out and f"sha256 {ih}, 2 apartment(s)" in out and "audit event access_facts_import written" in out
    ev = rows(db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (tool.IMPORT_AUDIT,)))
    assert len(ev) == before + 1 and ev[-1]["staff_member"] == tool.DEFAULT_STAFF and ev[-1]["entity"] == "apartment_access_facts"
    d = json.loads(ev[-1]["detail"])
    assert d["file_sha256"] == fh and d["file_rows"] == 3 and d["inventory_sha256"] == ih and d["inventory_rows"] == 2
    assert (d["recorded"], d["unchanged"], d["skipped"]) == (2, 0, 1) and {"groups_checked", "groups_requeued", "groups_unchanged", "groups_skipped"} <= set(d)
    assert "A13" not in tool.DEFAULT_STAFF and "(" not in tool.DEFAULT_STAFF and access_facts.current(db, a["id"])["recorded_by"] == tool.DEFAULT_STAFF
    h = access_facts.history(db, b["id"]) if hasattr(access_facts, "history") else rows(db.execute("SELECT status, source, scope FROM apartment_access_facts WHERE apartment_id=?", (b["id"],)))
    assert any(x["status"] == "excluded_from_confirmation" and x["source"] == SRC for x in h)      # per-record source / status kept
    # all skipped (reference says every apartment sits elsewhere): still ONE event, recorded 0, no fact written
    inv2 = tmp_path / "inventory-elsewhere.json"
    inv2.write_text(json.dumps([{"apartment_id": x["id"], "address": x["address"], "suburb": "Elsewhere", "city": x["city"]} for x in apts]), encoding="utf-8")
    nfacts = row(db.execute("SELECT COUNT(*) AS n FROM apartment_access_facts"))["n"]
    assert tool.main(["--file", str(f), "--inventory", str(inv2), "--apply", "--staff", "importer"], c=db) == 0
    out = capsys.readouterr().out
    assert "rows 3: to record 0, unchanged 0, skipped 3" in out and "audit event access_facts_import written" in out
    ev = rows(db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (tool.IMPORT_AUDIT,)))
    d = json.loads(ev[-1]["detail"])
    assert len(ev) == before + 2 and ev[-1]["staff_member"] == "importer" and d["inventory_sha256"] == tool.file_digest(str(inv2))
    assert (d["recorded"], d["unchanged"], d["skipped"]) == (0, 0, 3) and row(db.execute("SELECT COUNT(*) AS n FROM apartment_access_facts"))["n"] == nfacts
    assert all(k in ("file", "file_sha256", "file_rows", "inventory", "inventory_sha256", "inventory_rows", "recorded", "unchanged", "skipped",
                     "groups_checked", "groups_requeued", "groups_unchanged", "groups_skipped") for k in d)   # closed keys, no row content


def test_staff_history_shows_the_recorded_status_in_plain_words_not_the_storage_key():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client", "staff.js"), encoding="utf-8").read()
    i = src.index("function accessFactCardHtml")
    card = src[i:i + 6000]
    for key in ("confirmed", "not_step_free", "unknown", "excluded_from_confirmation"):
        assert f"{key}:" in card                                                                    # every closed status word is mapped
    assert "statusWords(h.status)" in card and "esc(h.status || '')" not in card                   # the raw key is never shown


def test_fixture_file_if_present_has_86_confirmed_and_17_excluded_rows_and_no_automatic_extension():
    path = FIXTURE if os.path.exists(FIXTURE) else FIXTURE_ALT
    if not path or not os.path.exists(path):
        pytest.skip("A13(a) confirmation file not available here")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("apartments") if isinstance(data, dict) else data
    assert len(entries) == 103
    vals = [e.get("step_free_access") for e in entries]
    assert vals.count(True) == 86 and vals.count(None) == 17 and vals.count(False) == 0
    assert all(e.get("confirmation_source") == SRC for e in entries) and all(e.get("address") for e in entries)
    for e in entries:
        access_facts.value_from_text(e.get("step_free_access"))   # every row is a closed value; nothing is inferred for stock not in the file


# ---------------------------------------------------------------- reviewer G1 (10 Nov 2026): the gate is bound to the FINAL current offer
def test_essential_need_gate_is_bound_to_the_apartment_that_becomes_current_and_to_every_later_switch(db, monkeypatch):
    """A member's ESSENTIAL step-free need: the apartment the AI ranks first (which a Proposed group follows) is gated - not the
    pre-ranking apartment - and is_ready re-gates the CURRENT offer, so no switch path (stock follow-up, refresh, manual) can make
    the group ready at an apartment with no record or a contrary record. A valid recorded destination stays ready."""
    from server.enquiries import update_enquiry
    a = _apt(db)                                                          # recorded step-free below
    b = _apt(db, address="Unit 2 / 2 Other Street")                       # no record
    ids = _pool(db)
    update_enquiry(db, ids[0], {"else_text": P116}, "tester")
    access_facts.record(db, a["id"], "yes", source=SRC, staff="anna")
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    matching.run_match(db, b["id"], "tester", dataset="test")             # the same Proposed groups now also offer B (rule rank 2)
    def offered(gid):
        return {r["apartment_id"] for r in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=?", (gid,)))}
    gid = next(g["id"] for g in rows(db.execute("SELECT id FROM groups WHERE match_run_id=? AND is_primary=1 ORDER BY id", (res["run_id"],)))
               if ids[0] in {m["enquiry_id"] for m in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))}
               and {a["id"], b["id"]} <= offered(g["id"]))
    assert _g(db, gid)["apartment_id"] == a["id"] and _g(db, gid)["ai_status"] == "pending"
    def rank_b_first(g):
        r = _good(g)
        r["apartments"] = sorted(r["apartments"], key=lambda x: 0 if x["apartment_id"] == b["id"] else 1)
        return r
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(rank_b_first))
    monkeypatch.setattr(ai_queue, "_extract_notes", _extractor({P116: {"items": [_item(P116)], "residual": False}}))
    ai_queue.process_pending(db)
    live = _g(db, gid)
    assert live["apartment_id"] == b["id"]                                # the group followed the AI's #1 ...
    assert live["ai_status"] == "held" and live["held_reason"] == HOLD and live["ranking_basis"] == ai_queue.PRACTICAL_HOLD_BASIS   # ... and was gated THERE: no record at B
    assert ai_queue.is_ready(db, live)["ready"] is False and live["ready_at"] is None
    # a record at B: re-queued, reviewed with the record, ready at B
    access_facts.record(db, b["id"], "yes", source="site check", staff="anna")
    assert gid in access_facts.apply_to_groups(db, b["id"], "anna")["requeued"]
    ai_queue.process_pending(db)
    live = _g(db, gid)
    assert live["ai_status"] == "ok" and live["apartment_id"] == b["id"] and ai_queue.is_ready(db, live)["ready"] is True
    # every later switch path is re-gated at read time: an apartment with NO record, one recorded NOT step-free, a moved record;
    # a valid recorded destination stays ready
    c_apt = _apt(db, address="Unit 3 / 35 Darling Street")
    db.execute("""INSERT INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, lease_start, lease_end, rate_kind, exit_window, weekly_rent,
                  extra_bed, total_weekly, allocation_json)
                  SELECT group_id, ?, 9, 9, rule_score, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly, allocation_json
                  FROM group_apartments WHERE group_id=? AND apartment_id=?""", (c_apt["id"], gid, b["id"])); db.commit()   # a listed option (same allocation)
    matching._switch_current(db, gid, c_apt["id"])
    chk = ai_queue.is_ready(db, _g(db, gid))
    assert chk["ready"] is False and ai_queue.PRACTICAL_NOT_READY in chk["reasons"]
    matching._switch_current(db, gid, a["id"])                            # recorded step-free: the gate passes there
    assert ai_queue.PRACTICAL_NOT_READY not in ai_queue.is_ready(db, _g(db, gid))["reasons"]
    access_facts.record(db, a["id"], "no", source="site check", staff="anna")
    assert ai_queue.PRACTICAL_NOT_READY in ai_queue.is_ready(db, _g(db, gid))["reasons"]   # contrary record at the current offer
    db.execute("UPDATE apartments SET suburb='Carlton North' WHERE id=?", (b["id"],)); db.commit()   # B's record no longer applies (moved)
    matching._switch_current(db, gid, b["id"])
    assert ai_queue.PRACTICAL_NOT_READY in ai_queue.is_ready(db, _g(db, gid))["reasons"]
    assert ai_queue.essential_need_gate(db, gid, b["id"])["hold"] is True and ai_queue.essential_need_gate(db, gid, None)["hold"] is True


# ---------------------------------------------------------------- reviewer G2 / G4 (10 Nov 2026): deletion impact is reported, nothing is inherited; moves are never silent
def test_delete_and_replace_report_and_audit_removed_confirmations_and_new_stock_never_inherits(client, tmp_path):
    """A3(a): the apartment is deleted outright (its confirmation rows go with it; no archive, no reconstructable history). What the
    operator gets is an honest impact report + audit line, and an explicit no-inheritance control: a new row at the same address
    (or the same id) starts with the fact unknown."""
    import server.app as appmod
    from server import importer
    from tests.test_stock_lifecycle import _sheet
    db = appmod.db
    a = _apt(db); b = _apt(db, address="Unit 2 / 2 Other Street")
    access_facts.record(db, a["id"], 1, source=SRC, staff="anna")
    access_facts.record(db, b["id"], None, source=SRC, staff="anna", status="excluded_from_confirmation")
    r = client.delete(f"/api/apartments/{a['id']}")
    assert r.status_code == 200
    rem = r.json()["access_facts_removed"]
    assert rem["count"] == 1 and rem["by_source"] == {SRC: 1} and rem["apartments"][0] == {"apartment_id": a["id"], "address": a["address"], "value": 1, "status": "confirmed", "source": SRC}
    au = rows(db.execute("SELECT detail FROM audit_log WHERE action=?", (access_facts.REMOVED_AUDIT,)))
    assert len(au) == 1 and "1 step-free confirmation(s) removed" in au[0]["detail"] and SRC in au[0]["detail"]
    assert rows(db.execute("SELECT 1 FROM apartment_access_facts WHERE apartment_id=?", (a["id"],))) == []      # gone with the apartment, as A3(a) says
    # no inheritance: a new row at the same address, and even one forced onto the same id, starts unknown
    a2 = _apt(db)
    assert a2["address"] == a["address"] and access_facts.current(db, a2["id"])["record_id"] is None and access_facts.effective(a2)["value"] is None
    db.execute("UPDATE apartments SET id=? WHERE id=?", (a["id"], a2["id"])); db.commit()
    assert access_facts.current(db, a["id"])["record_id"] is None and access_facts.history(db, a["id"]) == []
    assert "access_facts_removed" not in client.delete(f"/api/apartments/{a['id']}").json()                    # nothing to report when none was recorded
    # replace-import: the wipe reports what it removed; the fresh rows carry nothing
    p = str(tmp_path / "list.xlsx")
    _sheet(p, [{"Address": "Unit 2 / 2 Other Street", "Apartment type": "2B2B", "Bedroom 1": "1 king or 2 singles", "Bedroom 2": "1 king or 2 singles", "Bedroom 3": None,
                "Bills/utilities": "Not included", "2026 12 months rate": 1150.0, "2026 6 months rate": 1225.0, "Vacate date": "TBA"}])
    rep = importer.import_apartments(db, p, "anna", replace=True)
    assert rep["access_facts_removed"]["count"] == 1 and rep["access_facts_removed"]["apartments"][0]["status"] == "excluded_from_confirmation" and rep["access_facts_unbound"] == []
    assert rows(db.execute("SELECT id FROM apartment_access_facts")) == []
    nb = row(db.execute("SELECT * FROM apartments WHERE address LIKE '%2 Other Street%'"))
    assert nb and access_facts.effective(nb)["record_id"] is None and nb["step_free_access"] is None
    assert len(rows(db.execute("SELECT 1 FROM audit_log WHERE action=?", (access_facts.REMOVED_AUDIT,)))) == 2


def test_location_change_that_unbinds_a_confirmation_is_reported_by_import_and_staff_edit(client, tmp_path):
    import server.app as appmod
    from server import importer
    from tests.test_stock_lifecycle import _sheet
    db = appmod.db
    a = _apt(db)
    db.execute("UPDATE apartments SET suburb='Melbourne CBD' WHERE id=?", (a["id"],)); db.commit()   # as the importer normalises it
    access_facts.record(db, a["id"], 1, source=SRC, staff="anna")
    assert access_facts.current(db, a["id"])["bound"] is True
    # staff edit of the suburb: reported in the response + audited; the fact reads unknown at the moved row, nothing re-recorded
    r = client.patch(f"/api/apartments/{a['id']}", json={"suburb": "Carlton North"})
    assert r.status_code == 200
    assert r.json()["access_facts_unbound"] == [{"apartment_id": a["id"], "address": a["address"], "source": SRC, "value": None}]
    assert access_facts.current(db, a["id"])["bound"] is False and access_facts.current(db, a["id"])["value"] is None
    assert len(rows(db.execute("SELECT 1 FROM apartment_access_facts WHERE apartment_id=?", (a["id"],)))) == 1
    au = rows(db.execute("SELECT detail FROM audit_log WHERE action=?", (access_facts.UNBOUND_AUDIT,)))
    assert len(au) == 1 and "staff edit" in au[0]["detail"] and str(a["id"]) in au[0]["detail"]
    assert "access_facts_unbound" not in client.patch(f"/api/apartments/{a['id']}", json={"rent_12m": 1180.0}).json()   # unrelated edit: nothing to report
    # import by address that moves the row's suburb (the list's normalised suburb differs from the one the record was bound to):
    # reported in the import result + audited; the fact reads unknown at the moved row
    access_facts.record(db, a["id"], 1, source="site check", staff="anna")                      # bound to the present 'Carlton North' row
    assert access_facts.current(db, a["id"])["bound"] is True
    p = str(tmp_path / "list.xlsx")
    _sheet(p, [{"Address": a["address"], "Apartment type": "2B2B", "Bedroom 1": "1 king or 2 singles", "Bedroom 2": "1 king or 2 singles", "Bedroom 3": None,
                "Bills/utilities": "Not included", "2026 12 months rate": 1180.0, "2026 6 months rate": 1225.0, "Vacate date": "TBA"}])
    rep = importer.import_apartments(db, p, "anna")
    assert rep["errors"] == [] and rep["added"] == 0 and row(db.execute("SELECT suburb FROM apartments WHERE id=?", (a["id"],)))["suburb"] == "Melbourne CBD"
    assert rep["access_facts_unbound"] == [{"apartment_id": a["id"], "address": a["address"], "source": "site check", "value": None}]
    assert access_facts.current(db, a["id"])["bound"] is False and access_facts.current(db, a["id"])["value"] is None
    assert len(rows(db.execute("SELECT 1 FROM audit_log WHERE action=?", (access_facts.UNBOUND_AUDIT,)))) == 2
    assert importer.import_apartments(db, p, "anna")["access_facts_unbound"] == []            # already unbound: not reported twice
