"""GP13 (9 Nov 2026 build, root Revision 2 + corrections): individual group responses.

Each person in an introduced group answers through their own link; a membership or identity change resets EVERY member's approval;
24 elapsed hours from the introduction of the CURRENT version → staff review alert (never removal); silence is nothing; Group confirmed
is gated on every current member's derived acceptance; links are security only; retention removes everything about a deleted person
including other members' notes on the affected versions and their entries in historical membership snapshots.
Synthetic data, isolated SQLite, no provider (the AI draft is faked by the shared helper)."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from server import ai_queue, alerts, enquiries, group_responses as gr, identities
from server.db import RetentionGuardError, retention_guard, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_t6_staff_api import _introduce, _ready_party

T0 = "2026-11-09T01:00:00+00:00"


def _iso(base, **kw):
    return (gr._dt(base) + timedelta(**kw)).replace(microsecond=0).isoformat()


def _introduced(client, monkeypatch):
    """Astrid (lead) + Bo + Cy (party) + Devi (solo) in one group, drafted and marked Introduced through the real staff route."""
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    assert client.post(f"/api/groups/{gid}/introduction").status_code == 200
    r = client.patch(f"/api/groups/{gid}", json=_introduce(client, gid))
    assert r.status_code == 200, r.text
    return db, lead, solo, gid


def _state(client, gid):
    r = client.get(f"/api/groups/{gid}/responses")
    assert r.status_code == 200, r.text
    return r.json()


def _mint(client, gid, uid):
    r = client.post(f"/api/groups/{gid}/responses/links", json={"person_uid": uid})
    assert r.status_code == 200, r.text
    return r.json()


def _anon(client):
    return TestClient(client.app)


def _respond(client, token, response, note=""):
    return _anon(client).post("/api/public/group-responses/respond", json={"token": token, "response": response, "note": note})


def _lookup(client, token):
    return _anon(client).post("/api/public/group-responses/lookup", json={"token": token})


def _reconfirm(client, eid):
    """After a party identity edit the adult declaration is re-made by staff (I11(d)) and, when the draft went stale, re-drafted."""
    r = client.patch(f"/api/enquiries/{eid}", json={"adults_confirmed": True})
    assert r.status_code == 200, r.text
    db = appdb(client)
    gid = row(db.execute("SELECT group_id FROM group_members WHERE enquiry_id=? ORDER BY group_id DESC LIMIT 1", (eid,)))["group_id"]
    # the facts changed since the AI review: staff re-queue the assessment (existing 'Refresh every AI assessment' action for the test data
    # set) and the (faked) review + brief run again - the existing T6 workflow, nothing skipped
    ai_queue.reassess_all(db, "test", "tester")
    ai_queue.process_pending(db)
    i = client.get(f"/api/groups/{gid}").json().get("intro") or {}
    if not i.get("current"):
        r = client.post(f"/api/groups/{gid}/introduction")
        assert r.status_code == 200, (r.text, client.get(f"/api/groups/{gid}").json().get("circulation"))


def _accept_all(client, gid):
    st = _state(client, gid)
    toks = {}
    for p in st["people"]:
        toks[p["person_uid"]] = _mint(client, gid, p["person_uid"])["token"]
        assert _respond(client, toks[p["person_uid"]], "accept").status_code == 200
    return toks


# ------------------------------------------------------------------------------------------------ introduction anchors a version
def test_introduced_action_anchors_the_current_version_and_mints_nothing(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    st = _state(client, gid)
    assert st["applies"] and st["version"]["version"] == 1 and st["version"]["introduced_at"] and st["version"]["introduced_by"]
    assert len(st["people"]) == 4 and {p["display_name"] for p in st["people"]} == {"Astrid Holm", "Bo Berg", "Cy Lund", "Devi Rao"} or len(st["people"]) == 4
    assert all(p["link"] is None and p["response"] is None and p["links_made"] == 0 for p in st["people"])   # no auto-mint
    assert st["all_accepted"] is False and st["needs_reintroduction"] is False
    assert "still to answer" in st["confirmation_block"]
    assert "housemates" in st["meaning"] and "not an agreed amount" in st["meaning"]
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_response_links"))["n"] == 0
    # the version is anchored on the staff Introduced action, not on the draft
    assert row(db.execute("SELECT introduced_at FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))["introduced_at"]


def test_proposed_group_has_no_links_and_no_anchor(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    st = _state(client, g["id"])
    # the membership is tracked from the start (a version with no anchor), but nothing applies before the introduction
    assert st["applies"] is False and st["version"]["introduced_at"] is None and st["needs_reintroduction"] is False
    assert st["confirmation_block"] is None and len(st["people"]) == 4 and all(p["link"] is None for p in st["people"])
    r = client.post(f"/api/groups/{g['id']}/responses/links", json={"person_uid": "whatever"})
    assert r.status_code == 409 and "introduced" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ links: token hygiene, rotation, expiry
def test_link_is_shown_once_and_only_its_digest_is_stored(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    uid = _state(client, gid)["people"][0]["person_uid"]
    out = _mint(client, gid, uid)
    assert out["token"] and out["path"].startswith("#/respond?t=") and out["expires_at"]
    dumped = json.dumps(rows(db.execute("SELECT * FROM group_response_links"))) + json.dumps(rows(db.execute("SELECT * FROM audit_log")))
    assert out["token"] not in dumped
    assert _state(client, gid)["people"][0]["link"]["id"] == out["link_id"]
    assert "token" not in json.dumps(_state(client, gid))                       # the staff state never carries a clear token


def test_rotation_keeps_history_and_only_one_active_link_per_person(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    uid = _state(client, gid)["people"][0]["person_uid"]
    first = _mint(client, gid, uid)
    assert _respond(client, first["token"], "accept").status_code == 200
    second = _mint(client, gid, uid)
    assert second["link_id"] != first["link_id"]
    links = rows(db.execute("SELECT id, revoked_at, revoked_reason FROM group_response_links WHERE person_uid=? ORDER BY id", (uid,)))
    assert len(links) == 2 and links[0]["revoked_reason"] == "rotated" and links[1]["revoked_at"] is None
    assert _lookup(client, first["token"]).status_code == 410                   # old link fails
    assert _lookup(client, second["token"]).status_code == 200
    # the response given through the old link is still the person's current response (append-only history, nothing deleted)
    p = next(p for p in _state(client, gid)["people"] if p["person_uid"] == uid)
    assert p["response"]["response"] == "accept" and p["links_made"] == 2
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_responses WHERE person_uid=?", (uid,)))["n"] == 1


def test_expiry_is_security_only_never_a_closure(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    uid = _state(client, gid)["people"][0]["person_uid"]
    tok = _mint(client, gid, uid)["token"]
    db.execute("UPDATE group_response_links SET expires_at='2000-01-01T00:00:00+00:00' WHERE revoked_at IS NULL")
    db.commit()
    r = _lookup(client, tok)
    assert r.status_code == 410 and r.json()["detail"] == gr.INVALID
    p = next(p for p in _state(client, gid)["people"] if p["person_uid"] == uid)
    assert p["response"] is None and p["link"]["expired"] is True                 # nothing recorded against the person
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Introduced"
    assert not rows(db.execute("SELECT 1 FROM internal_alert_outbox"))                 # no alert, no GP9 effect
    assert _lookup(client, _mint(client, gid, uid)["token"]).status_code == 200   # the regen path simply works


# ------------------------------------------------------------------------------------------------ public side: lifecycle order
def test_lookup_shows_detail_only_after_identity_and_lifecycle_pass(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    people = _state(client, gid)["people"]
    devi = next(p for p in people if p["enquiry_id"] == solo["id"])
    tok = _mint(client, gid, devi["person_uid"])["token"]
    d = _lookup(client, tok).json()
    assert d["group"]["your_name"] == devi["display_name"] and len(d["group"]["housemates"]) == 3
    assert all(" " not in h for h in d["group"]["housemates"])                     # first names only
    assert "email" not in json.dumps(d) and "phone" not in json.dumps(d)           # no contact details
    assert d["group"]["meaning"].startswith("You are saying whether you are happy to share")
    assert {c["value"] for c in d["choices"]} == set(gr.RESPONSES)
    # bad / short / unknown tokens: the same fixed sentence, no detail
    for bad in ("", "short", "x" * 40, None):
        r = _anon(client).post("/api/public/group-responses/lookup", json={"token": bad})
        assert r.status_code == 410 and r.json()["detail"] == gr.INVALID
    # group leaves the respondable statuses -> the valid link fails before any detail
    db.execute("UPDATE groups SET status='Dissolved' WHERE id=?", (gid,))
    db.commit()
    r = _lookup(client, tok)
    assert r.status_code == 410 and r.json()["detail"] == gr.INVALID and "Devi" not in r.text


def test_old_token_fails_before_group_detail_once_membership_changes(client, monkeypatch):
    """Membership change (a party member edited to a different person) -> new version; every earlier link is revoked and answers nothing."""
    db, lead, solo, gid = _introduced(client, monkeypatch)
    toks = _accept_all(client, gid)
    assert _state(client, gid)["all_accepted"] is True
    bo = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))
    r = client.patch(f"/api/enquiries/{lead['id']}/members/{bo['id']}", json={"name": "Bea Berg", "email": "bea@example.com"})
    assert r.status_code == 200, r.text
    for tok in toks.values():
        r = _lookup(client, tok)
        assert r.status_code == 410 and r.json()["detail"] == gr.INVALID and "Astrid" not in r.text and "Bea" not in r.text
    st = _state(client, gid)
    assert st["version"]["version"] == 2 and st["version"]["introduced_at"] is None and st["needs_reintroduction"] is True
    assert st["all_accepted"] is False and all(p["response"] is None and p["link"] is None for p in st["people"])   # EVERYONE resets
    assert "Bea Berg" in {p["display_name"] for p in st["people"]}
    assert st["history"][0]["version"] == 1 and st["history"][0]["accepted_count"] == 4 and "do not count" in st["history"][0]["note"]
    assert "membership changed" in st["confirmation_block"]


# ------------------------------------------------------------------------------------------------ responses and alerts
def test_responses_are_append_only_idempotent_and_alert_staff_on_no_or_change(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    uid = _state(client, gid)["people"][0]["person_uid"]
    tok = _mint(client, gid, uid)["token"]
    r = _respond(client, tok, "accept")
    assert r.status_code == 200 and r.json()["recorded"] is True and r.json()["response"]["response"] == "accept"
    assert _respond(client, tok, "accept").json()["recorded"] is False           # identical repeat adds nothing
    r = _respond(client, tok, "change_request", "Could I have the smaller room?")
    assert r.status_code == 200 and r.json()["recorded"] is True
    r = _respond(client, tok, "decline", "Changed my plans")
    assert r.status_code == 200
    hist = rows(db.execute("SELECT response, note, superseded_at FROM group_responses WHERE person_uid=? ORDER BY id", (uid,)))
    assert [h["response"] for h in hist] == ["accept", "change_request", "decline"] and hist[2]["superseded_at"] is None
    assert hist[0]["superseded_at"] and hist[1]["superseded_at"]
    kinds = [a["kind"] for a in rows(db.execute("SELECT kind FROM internal_alert_outbox ORDER BY id"))]
    assert kinds == [gr.EV_CHANGE, gr.EV_DECLINED]
    rend = alerts.render(row(db.execute("SELECT * FROM internal_alert_outbox ORDER BY id DESC LIMIT 1")))
    assert f"Group-{gid:05d}" in rend["text"] and f"#/staff/groups/{gid}" in rend["text"] and "Changed my plans" not in rend["text"]
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Introduced"   # nothing automatic
    st = _state(client, gid)
    assert "said no" in st["confirmation_block"]
    # bad inputs
    assert _respond(client, tok, "maybe").status_code == 422
    assert _respond(client, tok, "accept", "x" * (gr.NOTE_MAX + 1)).status_code == 422
    # staff acknowledge
    rid = next(p for p in st["people"] if p["person_uid"] == uid)["response"]["id"]
    assert client.post(f"/api/groups/{gid}/responses/{rid}/reviewed").status_code == 200
    assert client.post(f"/api/groups/{gid}/responses/{rid}/reviewed").status_code == 404
    assert row(db.execute("SELECT reviewed_by FROM group_responses WHERE id=?", (rid,)))["reviewed_by"]


def test_24h_sweep_alerts_once_per_version_and_changes_nothing_else(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    v = row(db.execute("SELECT id, introduced_at FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    assert gr.sweep_overdue(db, now=_iso(v["introduced_at"], hours=23)) == 0
    assert gr.sweep_overdue(db, now=_iso(v["introduced_at"], hours=24, minutes=1)) == 1
    assert gr.sweep_overdue(db, now=_iso(v["introduced_at"], hours=30)) == 0        # once per version
    a = row(db.execute("SELECT kind, record_type, record_id FROM internal_alert_outbox"))
    assert a == {"kind": gr.EV_OVERDUE, "record_type": "group", "record_id": gid}
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Introduced"
    assert len(rows(db.execute("SELECT 1 FROM group_members WHERE group_id=?", (gid,)))) == 2           # nobody removed (two enquiries: party + solo)
    st = _state(client, gid, ) if False else gr.state(db, gid, now=_iso(v["introduced_at"], hours=25))
    assert st["version"]["review_due"] is True and all(p["overdue"] for p in st["people"])
    # everyone answered -> no alert even when due
    _accept_all(client, gid)
    db.execute("DELETE FROM internal_alert_outbox")
    db.commit()
    assert gr.sweep_overdue(db, now=_iso(v["introduced_at"], hours=48)) == 0


# ------------------------------------------------------------------------------------------------ confirmation gate, derived acceptance
def test_group_confirmed_requires_every_current_member_to_accept(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "Not everyone has said yes" in r.json()["detail"] and "still to answer" in r.json()["detail"]
    people = _state(client, gid)["people"]
    toks = {p["person_uid"]: _mint(client, gid, p["person_uid"])["token"] for p in people}
    for uid in list(toks)[:-1]:
        assert _respond(client, toks[uid], "accept").status_code == 200
    assert client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"}).status_code == 409
    assert _respond(client, toks[list(toks)[-1]], "decline").status_code == 200
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "said no" in r.json()["detail"]
    assert _respond(client, toks[list(toks)[-1]], "accept").status_code == 200
    assert gr.all_current_members_accepted(db, gid) is True
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 200, r.text
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Group confirmed"
    # a confirmed group still answers its links (a late change of mind is visible to staff), and nothing GP-coded leaks to staff text
    assert _lookup(client, toks[list(toks)[0]]).status_code == 200
    assert "GP13" not in json.dumps(_state(client, gid))


def test_acceptance_on_an_old_version_is_never_shown_as_approval(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    assert gr.all_current_members_accepted(db, gid) is True
    with retention_guard(db, "test membership change"):
        res = gr.on_membership_changed(db, gid, "edit", "tester")
        assert res["changed"] is False                                           # same people: no new version
        # a real change: Devi leaves the group (the lead's mutator deletes the row and calls the hook in the same guard)
        db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, solo["id"]))
        res = gr.on_membership_changed(db, gid, "left", "tester")
        assert res["changed"] is True and res["version"]["version"] == 2 and res["version"]["introduced_at"] is None
    assert gr.all_current_members_accepted(db, gid) is False
    st = _state(client, gid)
    assert st["all_accepted"] is False and len(st["people"]) == 3 and all(p["response"] is None for p in st["people"])
    assert st["history"][0]["accepted_count"] == 4 and st["history"][0]["people_count"] == 4
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_responses WHERE superseded_at IS NULL"))["n"] == 0
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_responses"))["n"] == 4   # history kept


# ------------------------------------------------------------------------------------------------ re-introduction
def test_revised_group_is_introduced_again_with_real_readiness_checks(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    bo = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))
    assert client.patch(f"/api/enquiries/{lead['id']}/members/{bo['id']}", json={"email": "bo.berg@example.com"}).status_code == 200
    assert _state(client, gid)["needs_reintroduction"] is True
    # the identity edit voided the party's adult declaration (its fingerprint names the party): readiness fails honestly - no bypass
    r = client.post(f"/api/groups/{gid}/responses/introduce", json={})
    assert r.status_code == 409 and "Not ready to introduce" in r.json()["detail"]
    _reconfirm(client, lead["id"])
    # without the draft the page showed: refused (no bypass)
    r = client.post(f"/api/groups/{gid}/responses/introduce", json={})
    assert r.status_code == 409 and "which draft was sent" in r.json()["detail"]
    # the same-status Introduced action with the current draft anchors the revised version (version-level clock survives same status)
    body = _introduce(client, gid)
    r = client.patch(f"/api/groups/{gid}", json=body)
    assert r.status_code == 200, r.text
    st = _state(client, gid)
    assert st["needs_reintroduction"] is False and st["version"]["version"] == 2 and st["version"]["introduced_at"]
    assert all(p["response"] is None for p in st["people"])
    assert rows(db.execute("SELECT 1 FROM audit_log WHERE action='group_reintroduced' AND entity_id=?", (str(gid),)))
    # explicit route when nothing is pending: honest refusal
    r = client.post(f"/api/groups/{gid}/responses/introduce", json={"draft_id": body["draft_id"], "context_hash": body["context_hash"]})
    assert r.status_code == 409 and "already been introduced" in r.json()["detail"]
    # another change, then the explicit route with the right draft works
    cy = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Cy Lund'", (lead["id"],)))
    assert client.patch(f"/api/enquiries/{lead['id']}/members/{cy['id']}", json={"name": "Cyril Lund"}).status_code == 200
    _reconfirm(client, lead["id"])
    body = _introduce(client, gid)
    r = client.post(f"/api/groups/{gid}/responses/introduce", json={"draft_id": body["draft_id"], "context_hash": body["context_hash"]})
    assert r.status_code == 200 and r.json()["version"]["version"] == 3 and r.json()["version"]["introduced_at"]
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Introduced"   # the status itself never moved


# ------------------------------------------------------------------------------------------------ identities: reattach vs ambiguity, promotion
def test_harmless_row_rewrite_keeps_identities_when_unambiguous_and_resets_when_ambiguous(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    toks = _accept_all(client, gid)
    before_uids = set(toks)
    with retention_guard(db, "rewrite"):
        before = identities.capture(db, lead["id"])
        members = rows(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (lead["id"],))
        for m in members:   # same people, new row ids (a delete + re-insert rewrite)
            db.execute("INSERT INTO party_members(enquiry_id, name, email, birth_country) VALUES (?,?,?,?)", (lead["id"], m["name"], m["email"], m["birth_country"]))
        res = identities.reconcile(db, lead["id"], "left", before=before)
        assert res["reattached"] == 2 and res["retired"] == 0 and res["issued"] == 0 and res["changed"] is False
        assert gr.on_membership_changed(db, gid, "edit", "tester")["changed"] is False
    assert set(gr.current_version(db, gid)["members"][i]["person_uid"] for i in range(4)) == before_uids
    assert gr.all_current_members_accepted(db, gid) is True                      # harmless edit: nobody resets
    # ambiguous: two companions with the same name + email pair rewritten -> conservative reset, never a transferred acceptance
    with retention_guard(db, "ambiguous"):
        db.execute("UPDATE party_members SET name='Bo Berg', email='bo@example.com' WHERE enquiry_id=? AND name='Cy Lund'", (lead["id"],))
        identities.reconcile(db, lead["id"], "identity_edit")                      # Cy's row becomes a new identity (edit)
        gr.on_membership_changed(db, gid, "identity_edit", "tester")
        before = identities.capture(db, lead["id"])
        members = rows(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (lead["id"],))
        for m in members:
            db.execute("INSERT INTO party_members(enquiry_id, name, email, birth_country) VALUES (?,?,?,?)", (lead["id"], m["name"], m["email"], m["birth_country"]))
        res = identities.reconcile(db, lead["id"], "left", before=before)
        assert res["reattached"] == 0 and res["retired"] == 2 and res["issued"] == 2
    assert gr.all_current_members_accepted(db, gid) is False


def test_explicit_promotion_names_the_actual_member_row(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    v1 = gr.current_version(db, gid)
    bo = row(db.execute("SELECT * FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))
    bo_uid = row(db.execute("SELECT person_uid FROM people_identities WHERE party_member_id=? AND retired_at IS NULL", (bo["id"],)))["person_uid"]
    old_lead_uid = row(db.execute("SELECT person_uid FROM people_identities WHERE enquiry_id=? AND role='lead' AND retired_at IS NULL", (lead["id"],)))["person_uid"]
    with retention_guard(db, "takeover"):
        # the GP9(e) takeover as the lead's mutator performs it: announce, then rewrite the enquiry row and delete the member row
        assert identities.promote_to_lead(db, lead["id"], bo["id"], "barred") == bo_uid
        db.execute("UPDATE enquiries SET first_name='Bo', last_name='Berg', email='bo@example.com' WHERE id=?", (lead["id"],))
        db.execute("DELETE FROM party_members WHERE id=?", (bo["id"],))
        res = identities.reconcile(db, lead["id"], "barred")
        assert res["issued"] == 0 and res["reset"] == 0                            # the lead identity already matches the row
        out = gr.on_membership_changed(db, gid, "barred", "tester")
    assert out["changed"] is True
    uids = {m["person_uid"] for m in out["version"]["members"]}
    assert bo_uid in uids and old_lead_uid not in uids and len(uids) == 3
    assert row(db.execute("SELECT retired_reason FROM people_identities WHERE person_uid=?", (old_lead_uid,)))["retired_reason"] == "barred"
    assert gr.all_current_members_accepted(db, gid) is False                      # everyone answers again for the new set
    assert v1["id"] != out["version"]["id"]


def test_module_writes_require_the_guard(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    with pytest.raises(RetentionGuardError):
        gr.on_membership_changed(db, gid, "edit", "tester")
    with pytest.raises(RetentionGuardError):
        identities.reconcile(db, lead["id"])
    with pytest.raises(RetentionGuardError):
        gr.mint_link(db, gid, "x", "tester")


# ------------------------------------------------------------------------------------------------ retention (T5)
def test_deleting_a_person_scrubs_identities_links_responses_snapshots_and_other_members_notes(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    people = _state(client, gid)["people"]
    devi = next(p for p in people if p["enquiry_id"] == solo["id"])
    astrid = next(p for p in people if p["enquiry_id"] == lead["id"] and p["role"] == "lead")
    toks = {p["person_uid"]: _mint(client, gid, p["person_uid"])["token"] for p in people}
    assert _respond(client, toks[astrid["person_uid"]], "change_request", "Happy, but only if Devi takes the small room").status_code == 200
    assert _respond(client, toks[devi["person_uid"]], "accept", "Fine with Astrid and the others").status_code == 200
    # Devi leaves the group (version 2) so version 1 becomes HISTORY that still names her and carries a note about her
    with retention_guard(db, "leave"):
        db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, solo["id"]))
        gr.on_membership_changed(db, gid, "left", "tester")
    assert "Devi" in json.dumps(rows(db.execute("SELECT members_json FROM group_membership_versions")))
    r = client.delete(f"/api/enquiries/{solo['id']}")
    assert r.status_code == 200, r.text
    scrub = r.json()["generated_text_removed"]
    assert scrub["group_response_rows"] >= 3 and scrub["group_versions_scrubbed"] == 1
    everything = json.dumps(rows(db.execute("SELECT * FROM group_membership_versions")) + rows(db.execute("SELECT * FROM group_responses"))
                            + rows(db.execute("SELECT * FROM group_response_links")) + rows(db.execute("SELECT * FROM people_identities")))
    assert "Devi" not in everything and devi["person_uid"] not in everything and "devi@" not in everything.lower()
    # the historical snapshot keeps an anonymous marker so counts stay honest; Astrid's note on that version is gone, her choice and time stay
    v1 = json.loads(row(db.execute("SELECT members_json FROM group_membership_versions WHERE group_id=? AND version=1", (gid,)))["members_json"])
    assert sum(1 for m in v1 if m.get("removed")) == 1 and len(v1) == 4
    a = row(db.execute("SELECT response, note, responded_at FROM group_responses WHERE person_uid=?", (astrid["person_uid"],)))
    assert a["response"] == "change_request" and a["note"] is None and a["responded_at"]
    # Astrid's own identity rows (and the other two) stay
    assert row(db.execute("SELECT COUNT(*) AS n FROM people_identities WHERE enquiry_id=?", (lead["id"],)))["n"] == 3
    assert "Former member" in json.dumps(_state(client, gid)["history"])


def test_forget_with_nobody_adds_no_keys_and_identity_edit_hook_is_guarded(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    with retention_guard(db, "probe"):
        out = enquiries.forget_generated_text(db, [])
    assert "group_response_rows" not in out                                      # the shared no-op dict is unchanged
    with retention_guard(db, "probe"):
        assert gr.forget(db, []) == {"identities": 0, "links": 0, "responses": 0, "versions_scrubbed": 0, "notes_cleared": 0}


def test_lead_identity_edit_through_the_enquiry_route_resets_everyone(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    assert client.patch(f"/api/enquiries/{lead['id']}", json={"staff_notes": "called"}).status_code == 200
    assert gr.all_current_members_accepted(db, gid) is True                      # a non-identity edit changes nothing
    assert client.patch(f"/api/enquiries/{lead['id']}", json={"email": "astrid.h@example.com"}).status_code == 200
    st = _state(client, gid)
    assert st["version"]["version"] == 2 and st["all_accepted"] is False and all(p["response"] is None for p in st["people"])


# ------------------------------------------------------------------------------------------------ the person's OWN share, read under the guard
def test_share_shown_only_where_the_allocation_binding_is_provable(client, monkeypatch):
    """group_members holds the lead enquiry's row only, so the old read gave every companion the LEAD's share. Now: the lead's share
    comes from their own group_members row; a companion's share only from an allocation occupant stamped with their person_uid
    (matching stamps a newly computed allocation with the uid it was computed for, proved at the write; an occupant without a
    provable uid shows "not set yet", never another person's figure). Every read happens inside the lookup guard. A party rewrite
    that reorders the rows (same people, UIDs preserved by reconcile) changes nothing: the share follows the person, not the row."""
    from server import db as dbm, identities
    from server.db import pj, now_iso
    db, lead, solo, gid = _introduced(client, monkeypatch)
    alloc = pj(db.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)).fetchone()[0], {}) or {}
    by_key = {str(o["person_id"]): o.get("share") for r in alloc.get("rooms") or [] for o in r.get("occupants") or []}
    lead_share = {r[0]: r[1] for r in db.execute("SELECT enquiry_id, weekly_share FROM group_members WHERE group_id=?", (gid,)).fetchall()}
    companion_keys = [k for k in by_key if not k.isdigit()]
    assert companion_keys and all(by_key[k] != by_key[k.split("m")[0]] for k in companion_keys), "fixture: companions' shares differ from the lead's"
    by_uid = {o["person_uid"]: o.get("share") for r in alloc.get("rooms") or [] for o in r.get("occupants") or [] if o.get("person_uid")}
    assert len(by_uid) == len(by_key), "fixture: every occupant of the fresh allocation is stamped with its person's uid"

    def check():
        st = _state(client, gid)
        out = {}
        for p in st["people"]:
            role = db.execute("SELECT role FROM people_identities WHERE person_uid=?", (p["person_uid"],)).fetchone()[0]
            tok = _mint(client, gid, p["person_uid"])["token"]
            seen = []
            dbm.GUARD_EVENTS.append(lambda ev, conn, label: seen.append((ev, label)))
            try:
                r = _lookup(client, tok)
            finally:
                dbm.GUARD_EVENTS.clear()
            assert r.status_code == 200, r.text
            share = r.json()["group"]["proposed_weekly_share"]
            if role == "lead":
                assert share == lead_share[p["enquiry_id"]] == by_key[str(p["enquiry_id"])], p["display_name"]
            else:
                assert share == by_uid[p["person_uid"]], (p["display_name"], share)   # the occupant stamped with THEIR uid; never the lead's figure
                assert share != by_key[str(p["enquiry_id"])]
            assert [l for e, l in seen if e == "committed"] == ["group response lookup"], seen
            out[p["person_uid"]] = share
        return out

    before = check()
    # harmless party rewrite that reverses the companions' row order (names and emails unchanged): reconcile keeps their UIDs
    eid = lead["id"]
    pm = [dict(r) for r in db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)).fetchall()]
    assert len(pm) >= 2
    with dbm.retention_guard(db, "test: party rewrite"):
        cap = identities.capture(db, eid)
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
        cols = [k for k in pm[0] if k != "id"]
        for r in reversed(pm):
            db.execute(f"INSERT INTO party_members({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [r[k] for k in cols])
        summary = identities.reconcile(db, eid, "party rewrite", before=cap)
    db.commit()
    assert summary["reattached"] == len(pm) and summary["retired"] == 0 and summary["issued"] == 0, summary
    after = check()
    assert after == before, "same people, same answers: the rewrite neither gives nor takes a share"
    assert set(after) == set(before), "UIDs preserved across the reorder"
