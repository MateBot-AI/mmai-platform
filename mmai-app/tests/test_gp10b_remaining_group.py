"""GP10(b) x GP13 (9 Nov 2026, root-approved narrow staff-led path): a group awaiting a replacement may be asked to confirm, and then be
marked Group confirmed, ONLY after staff explicitly record that the remaining group and the empty space were explained to everyone.

- 'Awaiting replacement' is not a respond status by itself: no links, no page, no confirmation while the current version is unanchored.
- The record is a current-membership-bound action carrying the digest of the FULL disclosed context (apartment, dates, total rent, the
  actual remaining people and their provable shares, empty spaces, uncovered amount, discount note); a stale token writes nothing.
- The anchor kind is closed ('remaining_group'); an ordinary introduction anchor never authorises the awaiting exception.
- No status flip, no minting, no message. Each person answers through their own link about the HOUSEMATES, not the rent; the page says
  honestly that a space is empty. All accepted -> the existing staff Group confirmed step works and the awaiting history stays.
- A membership change after the record resets everyone's approval (ordinary GP13 versioning); a discount-note edit alone resets nobody.
Synthetic data, isolated SQLite, no provider."""
import json

from server import group_responses as gr, matching
from server.db import pj, retention_guard, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_gp13_group_responses import _anon, _introduced, _lookup, _mint, _respond, _state
from tests.test_t6_staff_api import _ready_party

AW = "Awaiting replacement"


def _awaiting(client, monkeypatch):
    """Astrid (lead) + Bo + Cy (party) + Devi (solo) proposed; staff reject Devi and no refill passes -> the group waits with one empty bed."""
    from server import matching as mt
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    monkeypatch.setattr(mt, "_refill_options", lambda *a, **k: [])
    r = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [solo["id"]]})
    assert r.status_code == 200 and r.json()["awaiting_replacement"], r.text
    d = client.get(f"/api/groups/{g['id']}").json()
    assert d["status"] == AW == matching.AWAITING == gr.AWAITING and d["empty_beds"] == 1 and d["awaiting_note"]
    return db, lead, solo, g["id"], d


def _remaining(client, gid):
    st = _state(client, gid)
    assert st["status"] == AW and st["remaining_group"] and st["remaining_group"]["applies"]
    return st


def _anchor(client, gid, **over):
    st = _remaining(client, gid)
    body = {"membership_hash": st["remaining_group"]["membership_hash"], "disclosure_hash": st["remaining_group"]["disclosure_hash"], **over}
    return client.post(f"/api/groups/{gid}/responses/introduce-remaining", json=body)


def _audit(db, action, gid):
    # audit_log.entity_id is TEXT: compare as text so the probe reads the same rows on SQLite and PostgreSQL (PG has no text=int operator)
    return rows(db.execute("SELECT * FROM audit_log WHERE action=? AND entity='group' AND entity_id=? ORDER BY id", (action, str(gid))))


# ------------------------------------------------------------------------------------------------ nothing without the explicit record
def test_awaiting_group_is_closed_until_staff_record_the_remaining_group(client, monkeypatch):
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    st = _remaining(client, gid)
    assert st["applies"] is False and st["remaining_group"]["anchored"] is False and st["version"]["introduced_at"] is None
    assert st["version"]["anchor_kind"] is None
    rg = st["remaining_group"]
    assert rg["people_count"] == 3 and rg["empty_space_count"] == 1 and rg["uncovered_weekly"] == d["uncovered_weekly"] > 0
    assert rg["discount_note"] is None and len(rg["membership_hash"]) == 64 and len(rg["disclosure_hash"]) == 64
    # the confirmation block says what is missing, in plain words (no rule codes)
    assert "awaiting a replacement" in st["confirmation_block"] and "explained to everyone" in st["confirmation_block"]
    uid = st["people"][0]["person_uid"]
    # no links ...
    r = client.post(f"/api/groups/{gid}/responses/links", json={"person_uid": uid})
    assert r.status_code == 409 and "explained to everyone" in r.json()["detail"], r.text
    # ... no confirmation ...
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "awaiting a replacement" in r.json()["detail"], r.text
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW
    # ... and a link minted by hand on the unanchored version shows nothing (the one fixed sentence)
    v = row(db.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    assert v["introduced_at"] is None
    from server.security import token_hash
    tok = "x" * 40
    db.execute("""INSERT INTO group_response_links(version_id, group_id, enquiry_id, person_uid, token_hash, is_test, created_at, created_by, expires_at)
                  VALUES (?,?,?,?,?,?,?,?,?)""", (v["id"], gid, lead["id"], uid, token_hash(tok), 1, "2026-11-09T01:00:00+00:00", "t", "2099-01-01T00:00:00+00:00"))
    db.commit()
    r = _lookup(client, tok)
    assert r.status_code == 410 and r.json()["detail"] == gr.INVALID
    assert _respond(client, tok, "accept").status_code == 410
    assert not _audit(db, "group_remaining_introduced", gid)


def test_an_ordinary_introduction_anchor_never_authorises_the_awaiting_exception(client, monkeypatch):
    """A version anchored by the Introduced action (anchor_kind 'introduction') and then found waiting is NOT the remaining-group record."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    _remaining(client, gid)   # the staff page read creates the current (unanchored) version
    v = row(db.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    # forge the state an ordinary anchor would leave (there is no product path to this; the check is that the kind is what gates)
    db.execute("UPDATE group_membership_versions SET introduced_at=?, introduced_by='t', anchor_kind=? WHERE id=?", ("2026-11-09T01:00:00+00:00", gr.ANCHOR_INTRODUCTION, v["id"]))
    db.commit()
    assert gr.respond_allowed(AW, row(db.execute("SELECT * FROM group_membership_versions WHERE id=?", (v["id"],)))) is False
    st = _remaining(client, gid)
    assert st["applies"] is False and st["remaining_group"]["anchored"] is False
    r = client.post(f"/api/groups/{gid}/responses/links", json={"person_uid": st["people"][0]["person_uid"]})
    assert r.status_code == 409
    assert client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"}).status_code == 409
    # a bare introduced_at with no kind is not enough either
    db.execute("UPDATE group_membership_versions SET anchor_kind=NULL WHERE id=?", (v["id"],))
    db.commit()
    assert gr.respond_allowed(AW, row(db.execute("SELECT * FROM group_membership_versions WHERE id=?", (v["id"],)))) is False
    # and the Introduced / Group confirmed statuses are untouched by the new kind: they still need their anchor, kind or not
    assert gr.respond_allowed("Introduced", {"introduced_at": None}) is True and gr.respond_allowed("Proposed", {"introduced_at": "x", "anchor_kind": gr.ANCHOR_REMAINING_GROUP}) is False


def test_the_record_is_refused_for_an_introduced_group(client, monkeypatch):
    db, lead, solo, gid = _introduced(client, monkeypatch)
    st = _state(client, gid)
    assert st["remaining_group"] is None and st["version"]["anchor_kind"] == gr.ANCHOR_INTRODUCTION
    r = client.post(f"/api/groups/{gid}/responses/introduce-remaining", json={"membership_hash": "a" * 64, "disclosure_hash": "b" * 64})
    assert r.status_code == 409 and r.json()["detail"] == gr.NOT_AWAITING
    assert not _audit(db, "group_remaining_introduced", gid)


def test_the_record_is_refused_for_a_proposed_group(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Proposed"
    r = client.post(f"/api/groups/{g['id']}/responses/introduce-remaining", json={"membership_hash": "a" * 64, "disclosure_hash": "b" * 64})
    assert r.status_code == 409 and r.json()["detail"] == gr.NOT_AWAITING
    st = _state(client, g["id"])
    assert st["remaining_group"] is None and st["applies"] is False and (st["version"] or {}).get("introduced_at") is None


# ------------------------------------------------------------------------------------------------ the token binds membership AND disclosure
def test_a_stale_or_wrong_token_writes_nothing(client, monkeypatch):
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    st = _remaining(client, gid)
    mh, dh = st["remaining_group"]["membership_hash"], st["remaining_group"]["disclosure_hash"]
    for body in ({}, {"membership_hash": mh}, {"disclosure_hash": dh}, {"membership_hash": mh, "disclosure_hash": "0" * 64},
                 {"membership_hash": "0" * 64, "disclosure_hash": dh}):
        r = client.post(f"/api/groups/{gid}/responses/introduce-remaining", json=body)
        assert r.status_code == 409 and r.json()["detail"] == gr.STALE_REMAINING, (body, r.text)
    # the discount note is PART of the disclosed context: changing it after the page loaded makes the page's token stale
    assert client.patch(f"/api/groups/{gid}", json={"discount_note": "Offered $20/wk off for 8 weeks"}).status_code == 200
    r = client.post(f"/api/groups/{gid}/responses/introduce-remaining", json={"membership_hash": mh, "disclosure_hash": dh})
    assert r.status_code == 409 and r.json()["detail"] == gr.STALE_REMAINING
    st2 = _remaining(client, gid)
    assert st2["remaining_group"]["membership_hash"] == mh and st2["remaining_group"]["disclosure_hash"] != dh
    assert st2["remaining_group"]["discount_note"] == "Offered $20/wk off for 8 weeks"
    v = row(db.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    assert v["introduced_at"] is None and v["anchor_kind"] is None and v["disclosure_hash"] is None
    assert not _audit(db, "group_remaining_introduced", gid)
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW


def test_the_disclosure_digest_covers_the_full_rendered_context(client, monkeypatch):
    """Apartment identity, dates, total rent, every remaining person (lead AND companions) with their provable share, empty spaces,
    uncovered amount and discount note are all inside the digest; it is computed and compared inside the guard."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    with retention_guard(db, "test read"):
        gr.on_membership_changed(db, gid, "test", "tester")   # the documented order: members first, then the context of those members
        ctx = gr.remaining_group_context(db, gid)
    c = ctx["context"]
    assert c["apartment_id"] == d["apartment_id"] and c["address"] == d["address"] and c["lease_start"] and c["lease_end"]
    assert c["total_weekly"] == d["total_weekly"] and c["empty_space_count"] == 1 and c["uncovered_weekly"] == d["uncovered_weekly"]
    names = sorted(p["display_name"] for p in c["people"])
    assert names == ["Astrid Person", "Bo Berg", "Cy Lund"], names      # the actual remaining people, companions included; Devi gone
    shares = {p["role"]: p["proposed_weekly_share"] for p in c["people"]}
    assert shares["lead"] == d["members"][0]["weekly_share"]             # the lead's own proven share
    # companions: their OWN stamped occupant share (never the lead's); an occupant without a provable uid would show None
    alloc = pj(row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"], {}) or {}
    by_uid = {o["person_uid"]: o.get("share") for r in alloc.get("rooms") or [] for o in r.get("occupants") or [] if o.get("person_uid")}
    for p in c["people"]:
        if p["role"] == "member":
            assert p["proposed_weekly_share"] == by_uid[p["person_uid"]] != shares["lead"], p
    import hashlib
    assert ctx["disclosure_hash"] == hashlib.sha256(json.dumps(c, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    # every field moves the digest
    for k, val in (("address", "Elsewhere"), ("lease_end", "2099-01-01"), ("total_weekly", 1.0), ("empty_space_count", 2), ("uncovered_weekly", 0.0), ("discount_note", "x")):
        c2 = dict(c, **{k: val})
        assert hashlib.sha256(json.dumps(c2, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest() != ctx["disclosure_hash"], k
    # guard required
    import pytest
    from server.db import RetentionGuardError
    with pytest.raises(RetentionGuardError):
        gr.remaining_group_context(db, gid)
    with pytest.raises(RetentionGuardError):
        gr.anchor_remaining_group(db, gid, "t", {"membership_hash": ctx["membership_hash"], "disclosure_hash": ctx["disclosure_hash"]})


# ------------------------------------------------------------------------------------------------ the happy path
def test_record_then_links_then_everyone_accepts_then_group_confirmed(client, monkeypatch):
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    assert client.patch(f"/api/groups/{gid}", json={"discount_note": "Offered $20/wk off the whole rent for the first 8 weeks"}).status_code == 200
    r = _anchor(client, gid)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["status"] == AW and st["applies"] is True and st["remaining_group"]["anchored"] is True
    assert st["version"]["introduced_at"] and st["version"]["introduced_by"] and st["version"]["anchor_kind"] == gr.ANCHOR_REMAINING_GROUP
    assert st["version"]["review_due_at"] and st["needs_reintroduction"] is False
    assert "one space stays empty" in st["meaning"] and "not agreeing to the rent" in st["meaning"]
    # recorded once, numbers only; no status change, no link, no outgoing message, nothing queued
    au = _audit(db, "group_remaining_introduced", gid)
    v0 = st["version"]["version"]   # the version current when staff record: the proposal's own version plus one per membership change so far
    alloc = pj(row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"], {}) or {}
    by_uid = {o["person_uid"]: o.get("share") for r in alloc.get("rooms") or [] for o in r.get("occupants") or [] if o.get("person_uid")}
    assert len(au) == 1 and json.loads(au[0]["detail"]) == {"version": v0, "people": 3} and au[0]["staff_member"] == st["version"]["introduced_by"]
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_response_links WHERE group_id=?", (gid,)))["n"] == 0
    assert row(db.execute("SELECT COUNT(*) AS n FROM internal_alert_outbox"))["n"] == 0
    v = row(db.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    assert v["disclosure_hash"] == _remaining(client, gid)["remaining_group"]["disclosure_hash"]
    # a second record is refused (already recorded); nothing else written
    r = _anchor(client, gid)
    assert r.status_code == 409 and "already been recorded" in r.json()["detail"]
    assert len(_audit(db, "group_remaining_introduced", gid)) == 1
    # still not confirmable: nobody has answered
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "still to answer" in r.json()["detail"]
    # individual links now work; the page is honest about the empty space and each person sees only a provable share
    toks = {}
    for p in st["people"]:
        out = _mint(client, gid, p["person_uid"])
        toks[p["person_uid"]] = out["token"]
        pg = _lookup(client, out["token"])
        assert pg.status_code == 200, pg.text
        g = pg.json()["group"]
        assert g["empty_space"] is True and g["empty_space_count"] == 1 and g["uncovered_weekly"] == d["uncovered_weekly"]
        assert g["discount_note"] == "Offered $20/wk off the whole rent for the first 8 weeks"
        assert g["meaning"] == gr.MEANING_REMAINING and "not agreeing to the rent" in g["meaning"]
        assert sorted(g["housemates"]) == sorted(x["display_name"].split(" ")[0] for x in st["people"] if x["person_uid"] != p["person_uid"])
        assert "Devi" not in g["housemates"]
        if p["role"] == "lead":
            assert g["proposed_weekly_share"] == d["members"][0]["weekly_share"]
        else:   # a companion sees the occupant stamped with THEIR uid (their own proposal), never the lead's figure
            assert g["proposed_weekly_share"] == by_uid[p["person_uid"]] != d["members"][0]["weekly_share"], p
    # everyone says yes (to the housemates)
    for uid, tok in toks.items():
        r = _respond(client, tok, "accept", "fine with the empty room for now")
        assert r.status_code == 200 and r.json()["recorded"] is True, r.text
    st = _state(client, gid)
    assert st["all_accepted"] is True and st["confirmation_block"] is None
    # the existing staff step now works from Awaiting replacement, and the awaiting history is kept
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 200 and r.json()["status"] == "Group confirmed", r.text
    g2 = client.get(f"/api/groups/{gid}").json()
    assert g2["awaiting_note"] and "1 empty bed" in g2["awaiting_note"] and g2["discount_note"].startswith("Offered")
    assert g2["uncovered_weekly"] == d["uncovered_weekly"] and g2["empty_beds"] == 1
    acts = [a["action"] for a in rows(db.execute("SELECT action FROM audit_log WHERE entity='group' AND entity_id=? ORDER BY id", (str(gid),)))]
    assert "group_remaining_introduced" in acts and acts[-1] == "group_status"
    assert rows(db.execute("SELECT detail FROM audit_log WHERE action='group_status' AND entity_id=? ORDER BY id DESC LIMIT 1", (str(gid),)))[0]["detail"] == "Group confirmed (was awaiting replacement)"
    # members' enquiry status follows, and the responses remain readable (Group confirmed is a respond status)
    assert all(x["status"] == "Group confirmed" for x in rows(db.execute("SELECT status FROM enquiries WHERE id=?", (lead["id"],))))
    assert _lookup(client, toks[st["people"][0]["person_uid"]]).status_code == 200
    # nothing was sent and the outbox is still empty
    assert row(db.execute("SELECT COUNT(*) AS n FROM internal_alert_outbox"))["n"] == 0


def test_decline_goes_to_staff_review_only_and_changes_no_status(client, monkeypatch):
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    assert _anchor(client, gid).status_code == 200
    st = _state(client, gid)
    toks = {p["person_uid"]: _mint(client, gid, p["person_uid"])["token"] for p in st["people"]}
    uids = list(toks)
    assert _respond(client, toks[uids[0]], "decline", "I would rather wait for a full group").status_code == 200
    for uid in uids[1:]:
        assert _respond(client, toks[uid], "accept").status_code == 200
    al = rows(db.execute("SELECT kind, record_id, is_test FROM internal_alert_outbox"))
    assert len(al) == 1 and al[0]["kind"] == gr.EV_DECLINED and al[0]["record_id"] == gid and al[0]["is_test"] == 1
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW
    st = _state(client, gid)
    assert st["all_accepted"] is False and "said no" in st["confirmation_block"]
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "said no" in r.json()["detail"]
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW
    # the person changes their mind through the same link: the group becomes confirmable (nothing automatic happens even then)
    assert _respond(client, toks[uids[0]], "accept").status_code == 200
    assert _state(client, gid)["all_accepted"] is True
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW


def test_membership_change_after_the_record_resets_everyone_but_a_discount_edit_resets_nobody(client, monkeypatch):
    from server import identities
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    assert _anchor(client, gid).status_code == 200
    st = _state(client, gid)
    v0 = st["version"]["version"]   # the version current at the record (whatever the proposal and the rejection produced)
    toks = {p["person_uid"]: _mint(client, gid, p["person_uid"])["token"] for p in st["people"]}
    for tok in toks.values():
        assert _respond(client, tok, "accept").status_code == 200
    assert _state(client, gid)["all_accepted"] is True
    # a financial edit alone (the discount note) resets no approval: the version and every link stay valid
    assert client.patch(f"/api/groups/{gid}", json={"discount_note": "Offered $30/wk off for 12 weeks"}).status_code == 200
    st = _state(client, gid)
    assert st["all_accepted"] is True and st["version"]["version"] == v0 and st["remaining_group"]["anchored"] is True
    assert st["remaining_group"]["disclosure_changed"] is True      # staff are told the details moved on; nobody's answer is touched
    for tok in toks.values():
        pg = _lookup(client, tok)
        assert pg.status_code == 200 and pg.json()["group"]["discount_note"] == "Offered $30/wk off for 12 weeks"
    # a membership change (a companion leaves the party) supersedes the version: every approval and link is gone, the awaiting
    # exception closes again until staff record the NEW remaining group
    pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? ORDER BY id LIMIT 1", (lead["id"],)))
    with retention_guard(db, "test: member leaves"):
        db.execute("DELETE FROM party_members WHERE id=?", (pm["id"],))
        identities.reconcile(db, lead["id"], "left")
        gr.on_membership_changed(db, gid, "left", "tester")
    db.commit()
    st = _state(client, gid)
    assert st["version"]["version"] == v0 + 1 and st["version"]["introduced_at"] is None and st["version"]["anchor_kind"] is None
    assert st["applies"] is False and st["remaining_group"]["anchored"] is False and st["remaining_group"]["people_count"] == 2
    assert st["all_accepted"] is False and all(p["response"] is None and p["link"] is None for p in st["people"])
    assert st["history"] and st["history"][0]["accepted_count"] == 3 and st["history"][0]["people_count"] == 3
    for tok in toks.values():
        assert _lookup(client, tok).status_code == 410
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409 and "awaiting a replacement" in r.json()["detail"]
    # recording again, for the new remaining group, re-opens the path - and is a second audit row (per version)
    assert _anchor(client, gid).status_code == 200
    assert len(_audit(db, "group_remaining_introduced", gid)) == 2
    assert json.loads(_audit(db, "group_remaining_introduced", gid)[-1]["detail"]) == {"version": v0 + 1, "people": 2}


# ------------------------------------------------------------------------------------------------ what did NOT change
def test_no_generic_readiness_relaxation(client, monkeypatch):
    """The exception lives only in group_responses.respond_allowed: READY_STATUSES, can_circulate and the Introduced gate still exclude
    an awaiting group; Awaiting is still set by MMAI only and cannot go back to Proposed by hand."""
    from server import ai_queue
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    assert AW not in ai_queue.READY_STATUSES
    assert _anchor(client, gid).status_code == 200
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert ai_queue.can_circulate(db, g)["ok"] is False
    assert client.get(f"/api/groups/{gid}").json()["ready"] is False
    assert client.patch(f"/api/groups/{gid}", json={"status": "Introduced"}).status_code == 409
    assert client.patch(f"/api/groups/{gid}", json={"status": "Proposed"}).status_code == 409
    assert client.post(f"/api/groups/{gid}/responses/introduce", json={}).status_code == 409   # the revised-introduction route is not the way in
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW


def test_sweep_review_alert_applies_to_the_remaining_group_too(client, monkeypatch):
    """The 24 h clock is anchored on the staff record; silence after it goes to staff review only (no removal, no message)."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    assert _anchor(client, gid).status_code == 200
    v = row(db.execute("SELECT introduced_at FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    later = gr._plus(v["introduced_at"], hours=gr.REVIEW_HOURS, minutes=1)
    assert gr.sweep_overdue(db, now=later) >= 1
    al = rows(db.execute("SELECT kind, record_id FROM internal_alert_outbox"))
    assert [a["kind"] for a in al] == [gr.EV_OVERDUE] and al[0]["record_id"] == gid
    assert client.get(f"/api/groups/{gid}").json()["status"] == AW
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_members WHERE group_id=?", (gid,)))["n"] == 1


# ------------------------------------------------------------------------------------------------ the uncovered amount (GP10(d))
# Before this correction both owned computations (staff group page and the remaining-group context) took total rent minus the sum of
# group_members.weekly_share - LEAD rows only (the lead's OWN share; companions have no row) - so a party's companions counted as
# uncovered. The figure now comes from every remaining person's own stored share in the bed allocation, or is unknown (None).
def _occupants(d):
    return [o for r in d["allocation"]["rooms"] for o in r["occupants"]]


def _lead_rows_only(d):
    return round(d["total_weekly"] - sum(m["weekly_share"] for m in d["members"]), 2)


def test_after_a_party_and_a_solo_are_proposed_and_the_solo_is_rejected_the_uncovered_part_is_the_empty_bed_only(client, monkeypatch):
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    occ = _occupants(d)
    assert [o["name"].split()[0] for o in occ] == ["Astrid", "Bo", "Cy"] and d["people"] == 3 == len(occ)
    assert d["total_weekly"] == 1600.0 and [o["share"] for o in occ] == [533.33, 266.67, 266.67]
    assert d["uncovered_weekly"] == 533.33 == round(1600.0 - (533.33 + 266.67 + 266.67), 2)
    assert d["uncovered_weekly"] != _lead_rows_only(d) == 1066.67          # the old lead-rows-only figure counted Bo and Cy as uncovered
    assert d["empty_beds"] == 1 and "about $533/wk is not covered" in next(o for o in d["staff_options"] if o["key"] == "lease")["detail"]
    st = _remaining(client, gid)
    assert st["remaining_group"]["uncovered_weekly"] == 533.33 and st["remaining_group"]["people_count"] == 3
    assert st["remaining_group"]["context"]["uncovered_weekly"] == 533.33


def test_lead_takeover_counts_the_companions_who_stayed_not_the_old_lead_row(client, monkeypatch):
    """Astrid (the enquirer) is barred and Bo + Cy stay: Bo takes over the form. Two beds are now empty; the uncovered part is the whole
    rent minus Bo's and Cy's own shares, whatever the lead row in group_members still says."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    r = client.post(f"/api/enquiries/{lead['id']}/bar", json={"reason": "test", "person": "lead", "party_choice": "stay"})
    assert r.status_code == 200 and r.json()["party_stayed"] and gid in r.json()["awaiting_groups"], r.text
    d = client.get(f"/api/groups/{gid}").json()
    occ = _occupants(d)
    assert d["status"] == AW and d["empty_beds"] == 2 and d["people"] == 2 == len(occ)
    assert [o["name"].split()[0] for o in occ] == ["Bo", "Cy"] and [o["share"] for o in occ] == [266.67, 266.67]
    assert d["uncovered_weekly"] == round(1600.0 - (266.67 + 266.67), 2) == 1066.66
    assert d["members"][0]["first_name"] == "Bo" and d["uncovered_weekly"] == round(d["total_weekly"] - sum(o["share"] for o in occ), 2)
    st = _remaining(client, gid)
    assert st["remaining_group"]["uncovered_weekly"] == 1066.66 and st["remaining_group"]["people_count"] == 2 and st["remaining_group"]["empty_space_count"] == 2


def test_companion_removal_keeps_the_lead_and_the_other_companion_shares(client, monkeypatch):
    """Bo is barred and Astrid + Cy stay: uncovered = whole rent minus Astrid's and Cy's own shares (the lead-rows-only figure would
    have ignored Cy)."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    bo = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name LIKE 'Bo%'", (lead["id"],)))
    r = client.post(f"/api/enquiries/{lead['id']}/bar", json={"reason": "test", "person": bo["id"], "party_choice": "stay"})
    assert r.status_code == 200 and r.json()["party_stayed"], r.text
    d = client.get(f"/api/groups/{gid}").json()
    occ = _occupants(d)
    assert d["status"] == AW and d["empty_beds"] == 2 and d["people"] == 2 == len(occ)
    assert [o["name"].split()[0] for o in occ] == ["Astrid", "Cy"] and [o["share"] for o in occ] == [533.33, 266.67]
    assert d["uncovered_weekly"] == 800.0 and d["uncovered_weekly"] != _lead_rows_only(d) == 1066.67
    st = _remaining(client, gid)
    assert st["remaining_group"]["uncovered_weekly"] == 800.0 and st["remaining_group"]["people_count"] == 2


def test_solo_only_group_control_the_two_computations_agree(client, monkeypatch):
    """Three solo enquirers on a 3-bedroom, one rejected: with no companions anywhere the allocation-based figure and the old lead-rows
    figure coincide (one empty bed's share), so the correction changes nothing for solo-only groups."""
    from server import matching as mt
    from tests.test_launch_neutral import _apt, _enq
    from tests.test_t6_staff_api import _db
    db = _db(client)
    a = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0)
    es = [_enq(db, n, f"{n.lower()}@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email") for n in ("Ela", "Fay", "Gus")]
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    assert g is not None and sorted(int(x) for x in g["member_key"].split(",")) == sorted(e["id"] for e in es)
    monkeypatch.setattr(mt, "_refill_options", lambda *a, **k: [])
    r = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [es[2]["id"]]})
    assert r.status_code == 200 and r.json()["awaiting_replacement"], r.text
    d = client.get(f"/api/groups/{g['id']}").json()
    occ = _occupants(d)
    assert d["status"] == AW and d["people"] == 2 == len(occ) == len(d["members"]) and all((m["party_size"] or 1) == 1 for m in d["members"])
    assert d["uncovered_weekly"] == _lead_rows_only(d) == round(d["total_weekly"] - sum(o["share"] for o in occ), 2) > 0
    st = _remaining(client, g["id"])
    assert st["remaining_group"]["uncovered_weekly"] == d["uncovered_weekly"] and st["remaining_group"]["people_count"] == 2


def test_a_malformed_or_incomplete_allocation_shows_unknown_never_an_invented_figure(client, monkeypatch):
    """None of these states has a product path (matching rewrites the allocation and the members in one guarded step); the check is
    that a stored allocation that cannot be read honestly yields 'not known' - no zero, no other person's contribution."""
    db, lead, solo, gid, d = _awaiting(client, monkeypatch)
    before = _remaining(client, gid)["remaining_group"]
    assert before["uncovered_weekly"] == 533.33 and d["uncovered_weekly"] == 533.33
    good = json.loads(row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"])

    def put(alloc):
        db.execute("UPDATE groups SET allocation_json=? WHERE id=?", (None if alloc is None else json.dumps(alloc), gid))
        db.commit()
        return client.get(f"/api/groups/{gid}").json(), _remaining(client, gid)["remaining_group"]

    # a companion's share missing / not a number
    bad = json.loads(json.dumps(good)); bad["rooms"][2]["occupants"][1]["share"] = None
    d1, rg1 = put(bad)
    assert d1["uncovered_weekly"] is None and rg1["uncovered_weekly"] is None and rg1["context"]["uncovered_weekly"] is None
    assert "cannot be read from the stored bed allocation" in next(o for o in d1["staff_options"] if o["key"] == "lease")["detail"]
    assert rg1["disclosure_hash"] != before["disclosure_hash"] and rg1["membership_hash"] == before["membership_hash"]   # the context changed, the people did not
    bad = json.loads(json.dumps(good)); bad["rooms"][2]["occupants"][1]["share"] = "266.67"
    assert put(bad)[0]["uncovered_weekly"] is None
    # an occupant missing without the bed being marked empty: fewer occupants than remaining people -> unknown, not 'Cy pays nothing'
    bad = json.loads(json.dumps(good)); del bad["rooms"][2]["occupants"][1]
    d2, rg2 = put(bad)
    assert d2["uncovered_weekly"] is None and rg2["uncovered_weekly"] is None and rg2["people_count"] == 3
    # a stray extra occupant (more occupants than people) -> unknown, never someone else's share counted as cover
    bad = json.loads(json.dumps(good)); bad["rooms"][1]["occupants"].append({"person_id": "9", "name": "Nobody", "bed": "king", "share": 533.33})
    assert put(bad)[0]["uncovered_weekly"] is None
    # no rooms / no allocation at all
    assert put({"rooms": "x"})[0]["uncovered_weekly"] is None
    assert put(None)[0]["uncovered_weekly"] is None
    # restored, the figure is back - the computation reads the stored allocation each time, nothing was cached or invented
    d3, rg3 = put(good)
    assert d3["uncovered_weekly"] == 533.33 and rg3["uncovered_weekly"] == 533.33 and rg3["disclosure_hash"] == before["disclosure_hash"]
    # the pure helper, for the record
    assert gr.uncovered_weekly(1600.0, good, 3) == 533.33 and gr.uncovered_weekly(1600.0, good, 2) is None and gr.uncovered_weekly(None, good, 3) is None
    assert gr.uncovered_weekly(1600.0, good, None) is None and gr.uncovered_weekly(1600.0, {"rooms": []}, 0) == 1600.0


def test_the_uncovered_helper_rejects_every_amount_that_is_not_a_finite_money_figure():
    """Defensive projection of STORED data (no intake claim): a total or share that is not a real, finite, non-negative number - or a
    sum / difference that overflows - is unknown (None), never 0, 1, inf or another figure."""
    def alloc(*shares, empty=1):
        rooms = [{"room": i + 1, "occupants": [{"person_id": str(i + 1), "share": s}], "empty_beds": 0} for i, s in enumerate(shares)]
        rooms.append({"room": len(shares) + 1, "occupants": [], "empty_beds": empty})
        return {"rooms": rooms}

    good = alloc(533.33, 266.67, 266.67)
    # positive controls: ints and floats, whole-rent coverage, a zero share (an uncovered bed recorded as 0 is a valid amount)
    assert gr.uncovered_weekly(1600, good, 3) == 533.33 and gr.uncovered_weekly(1600.0, alloc(800, 800, empty=0), 2) == 0.0
    assert gr.uncovered_weekly(1600.0, alloc(533.33, 0.0), 2) == 1066.67 and gr.uncovered_weekly(0, alloc(0), 1) == 0.0
    # totals that are not money figures
    for total in (float("nan"), float("inf"), float("-inf"), True, False, -1, -0.01, "1600", None, [1600], {"v": 1600}):
        assert gr.uncovered_weekly(total, good, 3) is None, total
    # shares that are not money figures
    for share in (float("nan"), float("inf"), float("-inf"), True, False, -1, -0.01, "266.67", None, [266.67], {}):
        assert gr.uncovered_weekly(1600.0, alloc(533.33, share), 2) is None, share
    # overflow in the sum or in the difference is not a figure either
    assert gr.uncovered_weekly(1600.0, alloc(1e308, 1e308), 2) is None
    assert gr.uncovered_weekly(1e308, alloc(1e308, 1e308), 2) is None
    assert gr.uncovered_weekly(1600.0, alloc(10 ** 400, 1), 2) is None        # an int too large for float arithmetic
    assert gr.uncovered_weekly(10 ** 400, alloc(1, 1), 2) is None
    # a people count that is not a whole number is unknown too
    for n in (True, 3.0, "3", None, -1):
        assert gr.uncovered_weekly(1600.0, good, n) is None, n
    # the shares covering more than the total is clamped at 0 (existing convention), not negative
    assert gr.uncovered_weekly(1000.0, good, 3) == 0.0
