"""r7 (8 Nov 2026 work order) - the application side of the introduction routes that does not depend on the new ai_queue
interfaces: static template basis / closed error category, the central readiness re-check INSIDE each application guard, and
the selected apartment as part of every introduction CAS (draft, staff edit, Introduced). Builder-interface parts (exact offer
fingerprint, stable draft id CAS, new stale reasons) are covered separately once those land."""
import json

import pytest

from server import ai, ai_queue, matching
from server import app as application
from server.db import row, rows
from tests.test_data_and_api import accept_all_members, client, appdb  # noqa: F401  (fixtures)
from tests.test_t6_staff_api import _ready_party, _fake_draft, _db, _edit, _introduce
from tests.test_launch_neutral import _apt
from tests.test_t6_grounding import _party_group


def _ready_party_with_two_offers(client, monkeypatch, address="Unit 2 / 2 Switch Street"):
    """A ready group whose ranked list holds TWO apartments: the second (same type and price) is listed at rank 2 BEFORE the AI
    review and brief run, so the evidence the review covered includes both and readiness holds. A GP6(b) switch to the second is
    then an allowed staff action that must not carry an introduction written for the first."""
    db = _db(client)
    other = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0, address=address)["id"]
    real_pp = ai_queue.process_pending
    monkeypatch.setattr(ai_queue, "process_pending", lambda c: None)      # pause the AI step until the second listing exists
    a, lead, solo, g = _party_group(db, monkeypatch)
    monkeypatch.setattr(ai_queue, "process_pending", real_pp)
    monkeypatch.setattr(ai, "draft_introduction", _fake_draft)
    first = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND rank=1", (g["id"],)))
    cols = [k for k in first.keys() if k not in ("id", "apartment_id", "rank")]
    db.execute(f"INSERT INTO group_apartments(apartment_id, rank, {', '.join(cols)}) VALUES (?, 2, {', '.join('?' for _ in cols)})",
               (other, *[first[k] for k in cols]))
    db.commit()
    real_pp(db)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert ai_queue.can_circulate(db, g)["ok"], ai_queue.can_circulate(db, g)
    listed = [r["apartment_id"] for r in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],)))]
    assert len(listed) == 2 and set(listed) == {a["id"], other} and listed[0] == g["apartment_id"], listed   # the AI step may re-rank the pair
    other = listed[1]   # the allowed switch target: the group's rank-2 apartment, whichever the ranking chose
    return db, a, lead, solo, g, other


def _group(c, gid):
    return row(c.execute("SELECT status, intro_text, apartment_id, evidence_hash, member_key FROM groups WHERE id=?", (gid,)))


def _intro_rows(c, gid):
    return rows(c.execute("SELECT id, status, body_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id", (gid,)))


def _audit(c, action, gid):
    return rows(c.execute("SELECT detail FROM audit_log WHERE action=? AND entity_id=? ORDER BY id", (action, str(gid))))


# ---------------------------------------------------------------- T5 / r7 §6: static template basis, category-only audit
def test_ai_unavailable_gives_a_static_template_basis_and_a_category_only_audit(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    prose = "Error code: 401 - {'type': 'authentication_error', 'message': 'invalid x-api-key sk-ant-SECRET'}"

    def down(*a, **k):
        raise ai.AIUnavailable(prose, kind="credentials")   # Modified A: the category comes from the trusted kind, not from the words
    monkeypatch.setattr(ai, "draft_introduction", down)
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["basis"] == application.TEMPLATE_AI_UNAVAILABLE == "Template (AI unavailable)" and body["current"] is False
    assert "SECRET" not in r.text and "x-api-key" not in r.text and "401" not in body["basis"]
    hist = _intro_rows(db, g["id"])
    assert len(hist) == 1 and hist[0]["status"] == "template"
    assert "SECRET" not in hist[0]["body_json"] and "authentication_error" not in hist[0]["body_json"]
    assert json.loads(hist[0]["body_json"])["basis"] == "Template (AI unavailable)"
    audits = _audit(db, "group_intro_draft", g["id"])
    assert audits and audits[-1]["detail"] == f"Template (AI unavailable); {ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS}"
    d = client.get(f"/api/groups/{g['id']}").json()
    assert "SECRET" not in json.dumps(d) and d["intro"]["current"] is False
    # a plain provider failure maps to the other closed category; still no prose anywhere
    monkeypatch.setattr(ai, "draft_introduction", lambda *a, **k: (_ for _ in ()).throw(ai.AIUnavailable("model returned garbage <<raw>>")))
    assert client.post(f"/api/groups/{g['id']}/introduction").status_code == 200
    assert _audit(db, "group_intro_draft", g["id"])[-1]["detail"] == f"Template (AI unavailable); {ai_queue.STATE_ERROR_PROVIDER}"
    assert all("<<raw>>" not in (x["detail"] or "") for x in _audit(db, "group_intro_draft", g["id"]))
    assert all("<<raw>>" not in x["body_json"] for x in _intro_rows(db, g["id"]))


# ---------------------------------------------------------------- r7: readiness re-check inside the guard (draft)
def test_draft_is_refused_inside_the_guard_when_readiness_was_lost_during_the_model_call(client, monkeypatch):
    """The CAS predicate names evidence/status/brief/member_key/apartment; readiness also depends on columns it does not name
    (review verdict, primary flag, ownership). Those move while the model writes -> the fresh re-check inside the guard refuses
    and nothing is persisted: no intro_text, no history row, no audit."""
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    before = _group(db, g["id"])
    n_hist = len(_intro_rows(db, g["id"]))

    def slow_draft(apartment, group, members, basis=None):
        db.execute("UPDATE groups SET review_verdict='reject' WHERE id=?", (g["id"],)); db.commit()   # review turned while the AI wrote
        return _fake_draft(apartment, group, members, basis)
    monkeypatch.setattr(ai, "draft_introduction", slow_draft)
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 409 and "no longer ready to circulate" in r.json()["detail"], r.text
    assert _group(db, g["id"]) == before, "the late model text never landed"
    assert len(_intro_rows(db, g["id"])) == n_hist and not _audit(db, "group_intro_draft", g["id"])


def test_draft_is_refused_when_the_offer_switched_during_the_model_call(client, monkeypatch):
    """An allowed GP6(b) switch to another listed apartment between capture and apply: the CAS now binds the selected apartment,
    so the draft written for the former apartment is not stored against the new one."""
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    before = _group(db, g["id"])
    n_hist = len(_intro_rows(db, g["id"]))

    def switching_draft(apartment, group, members, basis=None):
        matching.offer_apartment(db, g["id"], other, "tester"); db.commit()
        return _fake_draft(apartment, group, members, basis)
    monkeypatch.setattr(ai, "draft_introduction", switching_draft)
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 409, r.text
    after = _group(db, g["id"])
    assert after["apartment_id"] == other and after["intro_text"] == before["intro_text"]
    assert len(_intro_rows(db, g["id"])) == n_hist and not _audit(db, "group_intro_draft", g["id"])


# ---------------------------------------------------------------- r7: staff edit and Introduced bind the apartment / readiness
def test_staff_edit_is_refused_inside_the_guard_when_readiness_or_apartment_moved(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    assert client.post(f"/api/groups/{g['id']}/introduction").status_code == 200
    d = client.get(f"/api/groups/{g['id']}").json()
    assert d["intro"]["current"] is True
    text0 = _group(db, g["id"])["intro_text"]
    n_hist = len(_intro_rows(db, g["id"]))
    # (1) the pre-guard read said ready, the row inside the guard is not primary any more -> the in-guard re-check refuses
    monkeypatch.setattr(application, "_circulation", lambda g: {"ok": True, "reasons": []})   # stale outer read, as in a race
    db.execute("UPDATE groups SET is_primary=0 WHERE id=?", (g["id"],)); db.commit()
    r = client.patch(f"/api/groups/{g['id']}", json=_edit(d, text0 + "\nEdited."))
    assert r.status_code == 409 and "no longer ready to circulate" in r.json()["detail"], r.text
    assert _group(db, g["id"])["intro_text"] == text0 and len(_intro_rows(db, g["id"])) == n_hist
    db.execute("UPDATE groups SET is_primary=1 WHERE id=?", (g["id"],)); db.commit()
    monkeypatch.undo()
    monkeypatch.setattr(ai, "draft_introduction", _fake_draft)
    # (2) the apartment switched after the page was opened: the edit CAS binds apartment_id and refuses
    real_update = application.db.execute
    state = {"switched": False}

    def switch_once(sql, *params):
        if sql.startswith("UPDATE groups SET intro_text=") and not state["switched"]:
            state["switched"] = True
            matching.offer_apartment(db, g["id"], other, "tester")   # same connection, inside the step: lands before the CAS
        return real_update(sql, *params)
    monkeypatch.setattr(application.db, "execute", switch_once, raising=False)
    try:
        r = client.patch(f"/api/groups/{g['id']}", json=_edit(d, text0 + "\nEdited again."))
    finally:
        monkeypatch.undo()
    assert r.status_code == 409 and "changed while you were editing" in r.json()["detail"], r.text
    g2 = _group(db, g["id"])
    assert g2["intro_text"] == text0 and len(_intro_rows(db, g["id"])) == n_hist
    assert not _audit(db, "group_intro_edit", g["id"])


def test_introduced_cas_binds_the_selected_apartment(client, monkeypatch):
    """Introduced is refused when the apartment the status step read is no longer the group's apartment at write time."""
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    assert client.post(f"/api/groups/{g['id']}/introduction").status_code == 200
    real_update = application.db.execute
    state = {"done": False}

    def switch_before_cas(sql, *params):
        if "SET status='Introduced'" in sql and not state["done"]:
            state["done"] = True
            matching.offer_apartment(db, g["id"], other, "tester")
        return real_update(sql, *params)
    tok = _introduce(client, g["id"])
    monkeypatch.setattr(application.db, "execute", switch_before_cas, raising=False)
    try:
        r = client.patch(f"/api/groups/{g['id']}", json=tok)
    finally:
        monkeypatch.undo()
    assert r.status_code == 409 and "changed while you were working" in r.json()["detail"], r.text
    # the step rolled back as a whole (the simulated switch ran inside it, so it is undone too): the group is still Proposed
    assert _group(db, g["id"])["status"] == "Proposed"
    assert not rows(db.execute("SELECT 1 FROM audit_log WHERE action='group_status' AND entity_id=?", (str(g["id"]),)))


# ---------------------------------------------------------------- r7: capture refusals are fixed sentences; legacy brief -> labelled template
def test_capture_refusal_codes_map_to_fixed_sentences_and_legacy_brief_gives_a_labelled_template(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    calls = []
    monkeypatch.setattr(ai, "draft_introduction", lambda *a, **k: calls.append(1) or _fake_draft(*a, **k))
    # legacy brief: the stored group brief predates note provenance -> no AI draft, a labelled template instead (no model call)
    b = row(db.execute("SELECT id, body_json FROM group_briefs WHERE group_id=? AND kind='group' AND status='ok' ORDER BY id DESC LIMIT 1", (gid,)))
    body = json.loads(b["body_json"]); body.pop("basis_meta_version", None)
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), b["id"])); db.commit()
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 200, r.text
    assert r.json()["basis"] == application.TEMPLATE_BASIS_INCOMPLETE == "Template (brief provenance incomplete)" and r.json()["current"] is False
    assert calls == [], "a legacy basis never reaches the model"
    hist = _intro_rows(db, gid)
    assert hist[-1]["status"] == "template" and json.loads(hist[-1]["body_json"])["basis_scope"] == "legacy"
    assert _audit(db, "group_intro_draft", gid)[-1]["detail"] == "Template (brief provenance incomplete)"
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["current"] is False and d["intro"]["draft_id"] == hist[-1]["id"]
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, "x")).status_code == 409   # a template is never edited as the AI draft
    assert client.patch(f"/api/groups/{gid}", json={"status": "Introduced"}).status_code == 409
    # no stored offer row for the group's apartment -> fixed sentence, nothing written
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, g["apartment_id"])); db.commit()
    n_hist = len(_intro_rows(db, gid))
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 409 and r.json()["detail"] == "Not ready to introduce: this group has no current apartment offer to introduce. Check its apartment list first.", r.text
    assert len(_intro_rows(db, gid)) == n_hist and calls == []
    # the closed code set is fully mapped (group_gone -> 404, not_circulable -> the readiness reasons, the rest fixed text)
    for code in ai_queue.INTRO_CAPTURE_CODES:
        exc = application._intro_capture_refusal(ai_queue.IntroNotCapturable(code, ["reason one", "reason two"]))
        assert exc.status_code == (404 if code == "group_gone" else 409)
        assert code not in exc.detail or code == "group_gone", (code, exc.detail)   # the code itself is never shown
        if code == "not_circulable":
            assert exc.detail == "Not ready to introduce: reason one; reason two"


# ---------------------------------------------------------------- r7: a staff edit carries the draft's own people map; a deleted person refuses the edit
def test_staff_edit_keeps_the_drafts_identity_map_and_refuses_when_a_described_person_is_gone(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    refs_of = lambda: rows(db.execute("SELECT id, refs_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id", (gid,)))
    ai_row = refs_of()[-1]
    ai_refs = json.loads(ai_row["refs_json"])
    assert ai_refs and all(v.get("enquiry_id") for v in ai_refs.values()), ai_refs
    d = client.get(f"/api/groups/{gid}").json()
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited once")).status_code == 200
    edited = refs_of()[-1]
    assert edited["id"] != ai_row["id"] and json.loads(edited["refs_json"]) == ai_refs, "the edit row keeps the SAME immutable ref -> identity map, not today's members"
    # a second edit builds on the edit row (same map again)
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["draft_id"] == edited["id"]
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited twice")).status_code == 200
    assert json.loads(refs_of()[-1]["refs_json"]) == ai_refs
    # an intro row without its people map (pre-provenance history) is never edited as current
    d = client.get(f"/api/groups/{gid}").json()
    db.execute("UPDATE group_briefs SET refs_json=NULL WHERE id=?", (d["intro"]["draft_id"],)); db.commit()
    n = len(refs_of())
    r = client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited thrice"))
    assert r.status_code == 409 and "Draft it again with the AI" in r.json()["detail"] and len(refs_of()) == n, r.text
    db.execute("UPDATE group_briefs SET refs_json=? WHERE id=?", (json.dumps(ai_refs), d["intro"]["draft_id"])); db.commit()
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited thrice")).status_code == 200
    # a described companion is deleted: the edit is refused inside the guard with nothing written, the old text and rows stay
    d = client.get(f"/api/groups/{gid}").json()
    pm = next(v for v in ai_refs.values() if v.get("party_member_id"))
    n, before = len(refs_of()), row(db.execute("SELECT intro_text FROM groups WHERE id=?", (gid,)))["intro_text"]
    monkeypatch.setattr(application, "_circulation", lambda g_: {"ok": True, "reasons": []})   # pre-checks pass on the stale page read
    monkeypatch.setattr(ai_queue, "intro_state", (lambda orig: lambda c, gid_: orig(c, gid_) | {"current": True})(ai_queue.intro_state))
    db.execute("DELETE FROM party_members WHERE id=?", (pm["party_member_id"],)); db.commit()
    r = client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited after a deletion"))
    assert r.status_code == 409 and r.json()["detail"].startswith("The edit was not kept"), r.text   # the readiness recheck inside the guard catches it first
    assert len(refs_of()) == n and row(db.execute("SELECT intro_text FROM groups WHERE id=?", (gid,)))["intro_text"] == before
    # the record step itself (last line of defence, same stored map the route passes) refuses the deleted person independently
    from server.db import retention_guard
    stored = json.loads(row(db.execute("SELECT body_json FROM group_briefs WHERE id=?", (d["intro"]["draft_id"],)))["body_json"])
    described = [{"ref": k, "enquiry_id": v.get("enquiry_id"), "party_member_id": v.get("party_member_id")} for k, v in ai_refs.items()]
    with pytest.raises(ValueError, match="deleted"):
        with retention_guard(db, label="probe"):
            ai_queue.record_intro_draft(db, gid, "x", "staff", "tester", members=described,
                                        capture={k: stored.get(k) for k in ("apartment_id", "profile_hash", "offer_fingerprint", "basis_scope", "context_hash")} | {"basis": stored.get("basis_items")})
    assert len(refs_of()) == n


# ---------------------------------------------------------------- r7 (root 11th case): Mark Introduced is bound to the draft row the page showed
def _snapshot(c, gid):
    return (_group(c, gid), [(r["id"], r["status"]) for r in _intro_rows(c, gid)],
            rows(c.execute("SELECT e.id, e.status FROM enquiries e JOIN group_members m ON m.enquiry_id=e.id WHERE m.group_id=? ORDER BY e.id", (gid,))),
            c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0])


def test_introduced_refuses_an_old_page_after_a_newer_draft_on_the_same_offer(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    old_page = _introduce(client, gid)                    # page 1 shows the AI draft
    d = client.get(f"/api/groups/{gid}").json()
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, d["intro"]["text"] + "\nEdited on page 2.")).status_code == 200   # page 2 edits: newer current row, same offer
    newer = _introduce(client, gid)
    assert newer["draft_id"] != old_page["draft_id"] and newer["context_hash"] == old_page["context_hash"], "same offer, same context, newer row"
    before = _snapshot(db, gid)
    for body, words in ((old_page, "newer introduction draft"), ({"status": "Introduced"}, "does not say which draft was sent"),
                        ({"status": "Introduced", "draft_id": newer["draft_id"]}, "does not say which draft was sent"),
                        (newer | {"context_hash": "0" * 64}, "newer introduction draft"), (newer | {"draft_id": "abc"}, "does not say which draft was sent")):
        r = client.patch(f"/api/groups/{gid}", json=body)
        assert r.status_code == 409 and words in r.json()["detail"] and "Reload the page" in r.json()["detail"], (body, r.text)
        assert "hash" not in r.json()["detail"].lower() and str(newer["draft_id"]) not in r.json()["detail"], r.text
    assert _snapshot(db, gid) == before, "a refused action writes nothing: status, drafts, members' status, audit"
    # the page that re-read the group succeeds; other status transitions carry no draft binding
    r = client.patch(f"/api/groups/{gid}", json=newer)
    assert r.status_code == 200 and r.json()["status"] == "Introduced", r.text
    accept_all_members(client, gid)   # GP13: every member's own yes first; the status step itself carries no draft binding
    assert client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"}).status_code == 200


def test_introduced_refuses_an_old_page_after_an_offer_switch_and_a_fresh_draft(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    old_page = _introduce(client, gid)
    # GP6(b) switch to the group's other listed apartment, then another page drafts for it
    assert client.patch(f"/api/groups/{gid}", json={"apartment_id": other}).status_code == 200
    assert client.patch(f"/api/groups/{gid}", json=old_page).status_code == 409   # old draft, no longer current
    r = client.post(f"/api/groups/{gid}/introduction"); assert r.status_code == 200 and r.json()["current"], r.text
    fresh = _introduce(client, gid)
    assert fresh["draft_id"] != old_page["draft_id"] and fresh["context_hash"] != old_page["context_hash"]
    before = _snapshot(db, gid)
    r = client.patch(f"/api/groups/{gid}", json=old_page)
    assert r.status_code == 409 and "newer introduction draft" in r.json()["detail"], r.text
    assert _snapshot(db, gid) == before
    r = client.patch(f"/api/groups/{gid}", json=fresh)
    assert r.status_code == 200 and row(db.execute("SELECT status, apartment_id FROM groups WHERE id=?", (gid,))) == {"status": "Introduced", "apartment_id": other}


# ---------------------------------------------------------------- r7 (stage B): the drafter receives the captured basis items unchanged
def test_drafter_receives_the_structured_basis_items_as_captured(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    seen = {}

    def spy(apartment, group, members, basis=None, meter=None):
        seen.update({"basis": basis, "apartment": apartment, "members": members})
        return _fake_draft(apartment, group, members, basis)
    monkeypatch.setattr(ai, "draft_introduction", spy)
    expected = ai_queue.intro_basis_items(db, gid, g["apartment_id"])
    assert expected and all(isinstance(it, dict) and it.get("text") and "coverage" in it and "fact_ids" in it for it in expected), expected
    r = client.post(f"/api/groups/{gid}/introduction"); assert r.status_code == 200 and r.json()["current"], r.text
    assert seen["basis"] == expected, "the model double got the captured items, not flattened sentences"
    assert all(isinstance(it, dict) for it in seen["basis"]) and seen["apartment"]["id"] == g["apartment_id"]
    stored = json.loads(_intro_rows(db, gid)[-1]["body_json"])
    assert stored["basis_items"] == expected and stored["basis_scope"] == "current"
    # the real drafter accepts exactly that shape (prompt projection built from items, no exception before the model call)
    assert isinstance(ai._basis_for_prompt(expected), list) and len(ai._basis_for_prompt(expected)) == len(expected)
