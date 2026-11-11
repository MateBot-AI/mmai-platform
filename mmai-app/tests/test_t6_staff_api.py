"""T6 staff API wiring (8 Nov 2026): the staff screens see readiness only through the central predicate (ai_queue.can_circulate:
valid review + current brief + primary + members not owned by another live group); the introduction routes (draft, edit,
mark introduced) are guarded by it and by the draft's own currency; one block per REAL person (lead and each party member);
stale text is kept as labelled history, never shown as current; staff-facing evidence is plain language (no U1./R.A ids).
Nothing here sends anything to a customer."""
import json
import os
import re

import pytest

from server import db as dbm

from server import ai, ai_queue, matching
from server.db import row, rows
from tests.test_data_and_api import client, appdb  # noqa: F401  (fixtures)
from tests.test_t6_grounding import _party_group
from tests.test_t6_promote import _setup as _promote_setup, _approve_and_brief_without_promoting


def _db(client):
    return appdb(client)


def _fake_draft(apartment, group, members, basis=None):
    """Deterministic AI draft: one line per real person, nothing but their own facts."""
    return "Hello everyone,\n\n" + "\n".join(f"{m['name']} ({m['ref']}) joins the group." for m in members) + "\n\nBest, ALM"


def _ready_party(client, monkeypatch):
    db = _db(client)
    a, lead, solo, g = _party_group(db, monkeypatch)
    assert ai_queue.can_circulate(db, g)["ok"]
    monkeypatch.setattr(ai, "draft_introduction", _fake_draft)
    return db, a, lead, solo, g


def _edit(d, text, **over):
    """r7 edit body: the draft row the page showed (stable id + its context hash); the timestamp is display only."""
    i = d["intro"] if "intro" in d else d
    return {"intro_text": text, "draft_id": i.get("draft_id"), "context_hash": i.get("context_hash"), "intro_drafted_at": i.get("drafted_at"), **over}


def _introduce(client, gid, **over):
    """r7 Introduced action body: names the draft row the page showed (stable id + its context), like the edit does."""
    i = client.get(f"/api/groups/{gid}").json().get("intro") or {}
    return {"status": "Introduced", "draft_id": i.get("draft_id"), "context_hash": i.get("context_hash"), **over}


RAW_ID = re.compile(r"\b[UIR]\d+(-m\d+)?\.|\bR\.A\d+\.")


# ---------------------------------------------------------------- lists, counters, detail
def test_ready_list_counter_and_detail_follow_the_central_predicate(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    ready = client.get("/api/groups", params={"ai_state": "ready", "dataset": "test"}).json()["items"]
    assert [x["id"] for x in ready] == [g["id"]]
    assert ready[0]["ready"] is True and ready[0]["review_state"] == "Ready for reservations" and ready[0]["circulation"]["ok"] is True
    assert client.get("/api/stats", params={"dataset": "test"}).json()["groups_ready"] == 1

    d = client.get(f"/api/groups/{g['id']}").json()
    assert d["ready_check"]["ready"] is True and d["circulation"]["ok"] is True
    assert d["review"]["current"] is True and d["review"]["verdict"] == "approve"
    assert d["brief"]["current"] is True and d["brief"]["status"] == "ok"
    # no draft yet: nothing presented as a current introduction
    assert d["intro"]["current"] is False and d["intro"]["text"] in (None, "") and d["intro_history"] == []
    # four real people, each their own recipient block; party members' emails covered by the lead's consent
    names = sorted(p["name"] for p in d["intro_recipients"])
    assert names == ["Astrid", "Bo Berg", "Cy Lund", "Devi"], names
    assert sorted(p["email"] for p in d["intro_recipients"]) == ["astrid@example.com", "bo@example.com", "cy@example.com", "devi@example.com"]
    # staff evidence is plain language: names and fact labels, not fact ids or rule codes
    blob = json.dumps({"review": d["review"], "brief": d["brief"]})
    assert "Astrid" in blob and not RAW_ID.search(blob), blob[:800]

    # the brief stops being current (and the group stops being ready) when the evidence moves on; the old text stays, labelled
    db.execute("UPDATE groups SET evidence_hash='changed' WHERE id=?", (g["id"],)); db.commit()
    d2 = client.get(f"/api/groups/{g['id']}").json()
    assert d2["ready"] is False and d2["review_state"] != "Ready for reservations"
    assert d2["brief"]["current"] is False and d2["brief"]["sections"], d2["brief"]
    assert client.get("/api/groups", params={"ai_state": "ready", "dataset": "test"}).json()["items"] == []
    assert client.get("/api/stats", params={"dataset": "test"}).json()["groups_ready"] == 0


def test_non_primary_alternative_that_passes_is_ready_is_not_ready_to_circulate(client, monkeypatch):
    """GP7: a technically approved and briefed alternative is shown as an alternative only; it is not in the ready list, not
    counted, and cannot be introduced while its members belong to the primary group."""
    db = _db(client)
    parent, alts = _promote_setup(db, monkeypatch)
    _approve_and_brief_without_promoting(db, monkeypatch)
    alt = next(x for x in alts if ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (x,))))["ready"])
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (alt,)))
    assert g["is_primary"] == 0 and ai_queue.is_ready(db, g)["ready"]
    assert not ai_queue.can_circulate(db, g)["ok"]
    ready_ids = [x["id"] for x in client.get("/api/groups", params={"ai_state": "ready", "dataset": "test", "alternatives": "true"}).json()["items"]]
    assert alt not in ready_ids
    d = client.get(f"/api/groups/{alt}").json()
    assert d["ready"] is False and d["ready_check"]["ready"] is True and d["circulation"]["ok"] is False and d["circulation"]["reasons"]
    assert client.post(f"/api/groups/{alt}/introduction").status_code == 409
    assert client.patch(f"/api/groups/{alt}", json={"status": "Introduced"}).status_code == 409
    assert client.get("/api/stats", params={"dataset": "test"}).json()["groups_ready"] == sum(
        1 for x in rows(db.execute("SELECT * FROM groups WHERE status='Proposed'")) if ai_queue.can_circulate(db, x)["ok"])


# ---------------------------------------------------------------- drafting the introduction
def _outbox_snapshot(db):
    """Everything queued for customers (receipt outbox) and for staff (internal alerts): id, kind, recipient, status. Portable
    across SQLite and Postgres; compared before/after so an introduction draft is shown to add or send nothing."""
    cust = rows(db.execute("SELECT id, enquiry_id, kind, to_email, status FROM email_outbox ORDER BY id"))
    alerts = rows(db.execute("SELECT id, event_id, status FROM internal_alert_outbox ORDER BY id"))
    return [tuple(sorted(r.items())) for r in cust], [tuple(sorted(r.items())) for r in alerts]


def test_draft_is_refused_until_ready_and_lists_every_real_person(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    before = _outbox_snapshot(db)          # pre-existing authorised receipts (intake) may be there; the introduction adds nothing
    # not ready: a plain group straight out of run_match, never reviewed
    db.execute("UPDATE groups SET ai_status='pending', review_verdict=NULL, brief_status=NULL WHERE id=?", (g["id"],)); db.commit()
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 409 and "Not ready" in r.json()["detail"]
    assert rows(db.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],))) == []
    db.execute("UPDATE groups SET ai_status='ok', review_verdict='approve', brief_status='ok' WHERE id=?", (g["id"],)); db.commit()

    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["basis"] == "AI" and body["current"] is True
    for name in ("Astrid", "Bo Berg", "Cy Lund", "Devi"):
        assert name in body["intro_text"]
    assert sorted(x["email"] for x in body["recipients"]) == ["astrid@example.com", "bo@example.com", "cy@example.com", "devi@example.com"]
    assert body["without_email"] == []
    # one history row, 'ok', stamped with the group's evidence hash
    hist = rows(db.execute("SELECT status, evidence_hash FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))
    assert [h["status"] for h in hist] == ["ok"] and hist[0]["evidence_hash"] == g["evidence_hash"]
    d = client.get(f"/api/groups/{g['id']}").json()
    assert d["intro"]["current"] is True and d["intro"]["text"] == body["intro_text"] and len(d["intro_history"]) == 1
    # the party members' blocks carry their own facts only: no course/work/WhatsApp borrowed from the lead
    people = {p["name"]: p for p in ai_queue.intro_members(db, g["id"])}
    for pm in ("Bo Berg", "Cy Lund"):
        assert people[pm]["doing"] is None and people[pm]["school"] is None and people[pm]["phone"] is None and people[pm]["whatsapp"] is False
    assert people["Astrid"]["whatsapp"] is True
    # nothing was queued or sent to anyone: customer receipt outbox and internal alert outbox are exactly as before
    assert _outbox_snapshot(db) == before
    from server.receipts import KIND_RECEIPT
    assert all(r["kind"] == KIND_RECEIPT for r in rows(db.execute("SELECT kind FROM email_outbox")))   # the only authorised customer email


def test_template_fallback_is_labelled_and_never_current(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)

    def down(*a, **k):
        raise ai.AIUnavailable("simulated outage")
    monkeypatch.setattr(ai, "draft_introduction", down)
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["basis"].startswith("Template") and body["current"] is False and ai.FALLBACK_LABEL in body["intro_text"]
    d = client.get(f"/api/groups/{g['id']}").json()
    assert d["intro"]["current"] is False and d["intro"]["text"] and d["intro_history"][0]["label"].lower().startswith("template")
    # a template draft cannot be edited as if it were the AI draft, nor does it let the group be marked introduced
    assert client.patch(f"/api/groups/{g['id']}", json=_edit(d, "x")).status_code == 409
    assert client.patch(f"/api/groups/{g['id']}", json={"status": "Introduced"}).status_code == 409
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (g["id"],)))["status"] == "Proposed"


def test_late_ai_result_cannot_land_on_a_changed_group(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)

    def slow_draft(apartment, group, members, basis=None):
        db.execute("UPDATE groups SET evidence_hash='moved-on' WHERE id=?", (g["id"],)); db.commit()   # facts changed under the AI
        return _fake_draft(apartment, group, members, basis)
    monkeypatch.setattr(ai, "draft_introduction", slow_draft)
    r = client.post(f"/api/groups/{g['id']}/introduction")
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    g2 = row(db.execute("SELECT intro_text FROM groups WHERE id=?", (g["id"],)))
    assert g2["intro_text"] is None
    assert rows(db.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],))) == []


# ---------------------------------------------------------------- staff edits and marking introduced
def test_edit_requires_current_draft_and_matching_version(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    # no draft yet
    assert client.patch(f"/api/groups/{gid}", json={"intro_text": "Hello", "draft_id": 1, "context_hash": "x", "intro_drafted_at": None}).status_code == 409
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["draft_id"] and d["intro"]["context_hash"]
    assert client.patch(f"/api/groups/{gid}", json=_edit(d, "")).status_code == 422
    # an edit that does not name the draft it was made on (pre-r7 client, timestamp only) is refused before anything else
    r = client.patch(f"/api/groups/{gid}", json={"intro_text": "Edited by staff", "intro_drafted_at": d["intro"]["drafted_at"]})
    assert r.status_code == 422 and "Reload the page" in r.json()["detail"]
    # a different draft id or a different context than the latest row: refused, nothing written
    r = client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited by staff", draft_id=d["intro"]["draft_id"] + 1))
    assert r.status_code == 409 and "Reload" in r.json()["detail"]
    r = client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited by staff", context_hash="0" * 64))
    assert r.status_code == 409 and "Reload" in r.json()["detail"]
    assert row(db.execute("SELECT intro_text FROM groups WHERE id=?", (gid,)))["intro_text"] == d["intro"]["text"]
    r = client.patch(f"/api/groups/{gid}", json=_edit(d, "Edited by staff"))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["intro"]["text"] == "Edited by staff" and out["intro"]["current"] is True
    assert [h["status"] for h in rows(db.execute("SELECT status FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id", (gid,)))] == ["ok", "edited"]
    assert len(out["intro_history"]) == 2
    # the evidence moves on: the edited text is kept but labelled stale, and a further edit is refused
    db.execute("UPDATE groups SET evidence_hash='changed' WHERE id=?", (gid,)); db.commit()
    d2 = client.get(f"/api/groups/{gid}").json()
    assert d2["intro"]["current"] is False and d2["intro"]["text"] == "Edited by staff" and d2["intro"]["stale_reason"]
    assert client.patch(f"/api/groups/{gid}", json=_edit(d2, "Again")).status_code == 409


def test_mark_introduced_needs_ready_group_and_current_draft(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    r = client.patch(f"/api/groups/{gid}", json={"status": "Introduced"})
    assert r.status_code == 409 and "no current introduction draft" in r.json()["detail"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    # a stale draft (evidence moved) is refused, and the group keeps its status
    tok = _introduce(client, gid)
    db.execute("UPDATE groups SET evidence_hash='changed' WHERE id=?", (gid,)); db.commit()
    r = client.patch(f"/api/groups/{gid}", json=tok)
    assert r.status_code == 409 and "Not ready to introduce" in r.json()["detail"], r.text   # readiness (facts moved) is checked first
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Proposed"
    # back to current: the change is one conditional update
    db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (g["evidence_hash"], gid)); db.commit()
    # ... but only for the draft row the page showed: an action that does not name it, or names another, is refused unchanged
    r = client.patch(f"/api/groups/{gid}", json={"status": "Introduced"})
    assert r.status_code == 409 and "does not say which draft was sent" in r.json()["detail"], r.text
    r = client.patch(f"/api/groups/{gid}", json=tok | {"draft_id": tok["draft_id"] + 1})
    assert r.status_code == 409 and "newer introduction draft" in r.json()["detail"], r.text
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Proposed"
    r = client.patch(f"/api/groups/{gid}", json=tok)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Introduced"
    assert {x["status"] for x in rows(db.execute("SELECT e.status FROM enquiries e JOIN group_members m ON m.enquiry_id=e.id WHERE m.group_id=?", (gid,)))} == {"Introduced"}


def test_mark_introduced_race_fails_closed(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    real = ai_queue.can_circulate

    def racing(c, grp):
        out = real(c, grp)
        if grp["id"] == gid:   # the brief is invalidated between the check and the write (inside the T5 guard: same transaction,
            c.execute("UPDATE groups SET brief_status='stale' WHERE id=?", (gid,))   # no commit of its own - the guard commits or rolls back)
            dbm.maybe_commit(c)
        return out
    tok = _introduce(client, gid)
    monkeypatch.setattr(ai_queue, "can_circulate", racing)
    r = client.patch(f"/api/groups/{gid}", json=tok)
    assert r.status_code == 409, r.text
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Proposed"


def test_carveout_shows_a_facts_note_and_missing_intro_profiles_only_flag(client, monkeypatch):
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    d = client.get(f"/api/groups/{g['id']}").json()
    assert d.get("facts_note") in (None, "")
    # nobody gave an introduction profile: the group is still ready, staff just see what is missing
    assert d["ready"] is True and d["intro_missing"], d["intro_missing"]
    assert not any(RAW_ID.search(x) for x in d["intro_missing"]), d["intro_missing"]


def test_earlier_round_is_not_attributed_to_todays_members(client, monkeypatch):
    """After a membership change the old brief's U1/U2 refs must not be mapped onto the people who are in the group NOW.
    Rows written with the immutable ref map (refs_json) are named from THAT map - the earlier people, a since-deleted one as
    "a former member"; legacy rows without a map (NULL) have their names withheld with the 'earlier member' label."""
    from tests.test_t6_grounding import _enq
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    d = client.get(f"/api/groups/{gid}").json()
    assert d["brief"]["names_available"] is True and d["brief"]["members"] and all(m["name"] not in ("U1", "U2", "I1", "I2") for m in d["brief"]["members"])
    assert rows(db.execute("SELECT 1 FROM group_briefs WHERE group_id=? AND refs_json IS NULL AND kind IN ('member','group')", (gid,))) == [], "writer must store the ref map"
    # Devi leaves, Zelda takes her place in the same group row (facts changed -> different evidence hash)
    zelda = _enq(db, "Zelda", "zelda@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, solo["id"]))
    db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?,?)", (gid, zelda["id"]))
    db.execute("UPDATE groups SET evidence_hash='members-changed', member_key=? WHERE id=?", (",".join(sorted([str(lead["id"]), str(zelda["id"])], key=int)), gid)); db.commit()

    d2 = client.get(f"/api/groups/{gid}").json()
    assert d2["brief"]["current"] is False and d2["brief"]["names_available"] is True
    names2 = [m["name"] for m in d2["brief"]["members"]]
    blob2 = json.dumps(d2["brief"]["sections"]) + json.dumps(names2)
    assert "Devi" in names2 and "Zelda" not in blob2, blob2[:600]           # the earlier people, never today's newcomer
    assert not RAW_ID.search(blob2)

    # Devi's record is later deleted: the map still names Astrid; Devi becomes "a former member" (no guess, no raw id)
    db.execute("DELETE FROM enquiries WHERE id=?", (solo["id"],)); db.commit()
    d3 = client.get(f"/api/groups/{gid}").json()
    names3 = [m["name"] for m in d3["brief"]["members"]]
    assert "Astrid" in names3 and "a former member" in names3 and "Devi" not in json.dumps(d3["brief"]) and "Zelda" not in json.dumps(d3["brief"])

    # legacy rows (no map): names withheld, labelled, nobody named
    db.execute("UPDATE group_briefs SET refs_json=NULL WHERE group_id=?", (gid,)); db.commit()
    d4 = client.get(f"/api/groups/{gid}").json()
    assert d4["brief"]["current"] is False and d4["brief"]["names_available"] is False
    blob4 = json.dumps(d4["brief"]["sections"]) + json.dumps([m["name"] for m in d4["brief"]["members"]])
    for who in ("Astrid", "Devi", "Zelda", "Bo Berg", "Cy Lund"):
        assert who not in blob4, blob4[:600]
    assert not RAW_ID.search(blob4) and "earlier member" in blob4


def test_intro_detail_edit_during_drafting_rejects_the_older_result(client, monkeypatch):
    """Introduction details (sex/age/contact/consent) are not in the compatibility hash. An edit while the AI is writing leaves a
    'stale' marker; the older AI result must then be refused, not written over it as a newer 'ok' draft. The compatibility review
    and brief are untouched by such an edit."""
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]

    def editing_draft(apartment, group, members, basis=None):
        r = client.patch(f"/api/enquiries/{solo['id']}", json={"intro_age": 31, "intro_sex": "Female"})
        assert r.status_code == 200, r.text
        return _fake_draft(apartment, group, members, basis)
    monkeypatch.setattr(ai, "draft_introduction", editing_draft)
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 409 and "introduction details changed" in r.json()["detail"].lower(), r.text
    hist = rows(db.execute("SELECT status FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id", (gid,)))
    assert [h["status"] for h in hist] == ["stale"], hist          # the marker stands; no 'ok' row was written after it
    live = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert live["intro_text"] is None and live["evidence_hash"] == g["evidence_hash"] and live["ai_status"] == "ok" and live["brief_status"] == "ok"
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["current"] is False and d["review"]["current"] is True and d["brief"]["current"] is True and d["ready"] is True
    # drafting again, with nobody editing, succeeds and uses the new detail
    monkeypatch.setattr(ai, "draft_introduction", _fake_draft)
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 200 and r.json()["current"] is True
    assert any(p["intro_age"] == 31 for p in ai_queue.intro_members(db, gid))


# ---------------------------------------------------------------- D (8 Nov 2026): group status / apartment claim are one guarded step
# Every read that decides a status change or an apartment claim happens inside the "group status" guard; the writes are conditional
# (status read under the guard; apartment unclaimed). These are single-connection, same-thread reproductions of state changing
# while the request waited (root's probe shape); the two-connection duplicate-lease proof is in tests/test_retention_two_connections.py.
from contextlib import contextmanager
from datetime import date

from fastapi import HTTPException

from server import app as application, enquiries
from server.db import now_iso
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _submit as _submit_public


def _second_apartment(c, address="Unit 2 / 2 Claim Street"):
    c.execute("""INSERT INTO apartments(city, address, address_raw, suburb, type_code, bathrooms, rent_12m, rent_6m, available_from, min_lease_months,
                                        bedroom1, bedroom2, bedroom3, bills_included, updated_at)
                 VALUES ('Melbourne', ?, ?, 'Melbourne', '2B2B', 2, 1150.0, 1225.0, '2027-01-27', 12, '1 king or 2 singles', '1 king or 2 singles', NULL, 0, ?)""",
              (address, address, now_iso()))
    c.commit()
    return row(c.execute("SELECT id FROM apartments WHERE address=?", (address,)))["id"]


def _list_apartment(c, gid, aid, rank, eid):
    alloc = {"rooms": [{"room": 1, "setup": "1 king or 2 singles", "room_cost": 575.0, "occupants": [{"person_id": str(eid), "name": "Member", "bed": "king", "share": 575.0}]}]}
    c.execute("""INSERT INTO group_apartments(group_id, apartment_id, rank, lease_start, lease_end, rate_kind, weekly_rent, total_weekly, allocation_json)
                 VALUES (?, ?, ?, '2027-02-01', '2028-01-31', '12m', 1150.0, 1150.0, ?)""", (gid, aid, rank, json.dumps(alloc)))
    c.commit()


def _apt(c, aid):
    return row(c.execute("SELECT leased_at, leased_group_id, leased_by FROM apartments WHERE id=?", (aid,)))


def _status(c, gid):
    return row(c.execute("SELECT status, apartment_id FROM groups WHERE id=?", (gid,)))


def test_lease_cannot_revive_a_group_dissolved_before_the_guard_was_acquired(client, monkeypatch):
    """Root's shape: a real purge commits (dissolving the group) immediately BEFORE the group-status guard is acquired. The outer
    read is not authoritative; the re-read inside the guard sees the final status and refuses. Nothing is revived or claimed."""
    victim = _submit_public(client, first_name="Root", last_name="Expired", email="root-expired@example.com")
    c = _db(client)
    gid = _plant_group(c, victim["id"])
    aid = _status(c, gid)["apartment_id"]
    c.execute("UPDATE enquiries SET last_contact_at=? WHERE id=?", ("2024-01-01T00:00:00+00:00", victim["id"]))
    c.commit()
    original = application.retention_guard
    purged = []

    @contextmanager
    def purge_before_acquisition(conn, label=""):
        if label == "group status" and not purged:
            purged.append(enquiries.purge_expired(conn, "root interleaving", today=date(2026, 11, 8)))
            assert purged[0]["deleted"] == 1 and _status(conn, gid)["status"] == "Dissolved"
        with original(conn, label=label):
            yield conn
    monkeypatch.setattr(application, "retention_guard", purge_before_acquisition)
    with pytest.raises(HTTPException) as ei:
        application._set_group_status(gid, "Leased", "root probe")
    assert ei.value.status_code == 409 and purged
    assert _status(c, gid)["status"] == "Dissolved"
    assert _apt(c, aid)["leased_at"] is None, "no claim for a refused status change"


def test_group_status_write_is_conditional_on_the_status_read_under_the_guard(client, monkeypatch):
    """The status changes underneath (same connection, inside the step, after the guarded read): the conditional UPDATE matches no
    row and the step is refused; the other change stands, nothing half-applies."""
    e = _submit_public(client, first_name="Cas", last_name="Member", email="cas@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    # the only call between the guarded read and the conditional write for a plain transition is the group re-read itself, so
    # interpose on `row`: the first group read INSIDE the guard returns the Proposed row, then the status is moved on before the
    # UPDATE runs (the route's own earlier read, outside the guard, is left alone: it is not authoritative anyway)
    real_row = application.row
    flipped = []

    def racing_row(cur):
        r = real_row(cur)
        if r and not flipped and dbm.guarded(c) and isinstance(r, dict) and r.get("id") == gid and r.get("status") == "Proposed" and "member_key" in r:
            flipped.append(True)
            c.execute("UPDATE groups SET status='Dissolved' WHERE id=?", (gid,))   # same connection, same step: no commit of its own
            dbm.maybe_commit(c)
        return r
    monkeypatch.setattr(application, "row", racing_row)
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 409, r.text
    assert flipped
    # the refusal rolled the step back: the in-step flip is gone too (it was part of the refused transaction), so the group is as before
    assert _status(c, gid)["status"] == "Proposed"


def test_post_lease_refusal_leaves_the_group_unleased_and_the_offer_unswitched(client):
    """Before D the route marked the group Leased (committed) and only then noticed the apartment was already taken, answering
    409 after the fact. Now the switch, the claim and the status are one step: the refusal leaves everything as it was."""
    e = _submit_public(client, first_name="Atomic", last_name="Lease", email="atomic@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    first = _status(c, gid)["apartment_id"]
    other = _second_apartment(c)
    _list_apartment(c, gid, first, 1, e["id"])
    _list_apartment(c, gid, other, 2, e["id"])
    c.execute("UPDATE apartments SET leased_at=?, leased_group_id=NULL, leased_by='someone else' WHERE id=?", (now_iso(), other))
    c.commit()
    r = client.post(f"/api/apartments/{other}/lease", json={"group_id": gid})
    assert r.status_code == 409 and "already marked Leased" in r.json()["detail"], r.text
    st = _status(c, gid)
    assert st["status"] == "Proposed" and st["apartment_id"] == first, "the offer switch was rolled back with the refused claim"
    assert _apt(c, other)["leased_by"] == "someone else" and _apt(c, first)["leased_at"] is None
    assert not rows(c.execute("SELECT 1 FROM audit_log WHERE entity='group' AND entity_id=? AND action IN ('offer_apartment','group_status')", (str(gid),)))


def test_post_lease_with_an_offer_switch_claims_and_leases_in_one_step(client):
    e = _submit_public(client, first_name="Switch", last_name="Lease", email="switch@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    first = _status(c, gid)["apartment_id"]
    other = _second_apartment(c)
    _list_apartment(c, gid, first, 1, e["id"])
    _list_apartment(c, gid, other, 2, e["id"])
    r = client.post(f"/api/apartments/{other}/lease", json={"group_id": gid})
    assert r.status_code == 200, r.text
    st = _status(c, gid)
    assert st["status"] == "Leased" and st["apartment_id"] == other
    a = _apt(c, other)
    assert a["leased_group_id"] == gid and a["leased_at"] == r.json()["leased_at"]
    assert _apt(c, first)["leased_at"] is None
    assert row(c.execute("SELECT status FROM enquiries WHERE id=?", (e["id"],)))["status"] == "Leased"
    detail = row(c.execute("SELECT detail FROM audit_log WHERE action='group_status' AND entity_id=? ORDER BY id DESC", (str(gid),)))["detail"]
    assert f"claimed apartment {other}" in detail and a["leased_at"] in detail
    assert row(c.execute("SELECT 1 FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(other),)))
    # a second POST for the unchanged Leased group is not a second claim: the claim stands as it was and only the follow-up
    # (re-ranking the other groups, idempotent) runs again - that is the recovery path when the follow-up did not finish
    r2 = client.post(f"/api/apartments/{other}/lease", json={"group_id": gid})
    assert r2.status_code == 200 and r2.json()["repeated"] is True and "not repeated" in r2.json()["message"], r2.text
    assert _apt(c, other) == a and r2.json()["leased_at"] == a["leased_at"]
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_status' AND entity_id=? AND detail LIKE '%claimed apartment%'", (str(gid),)).fetchone()[0] == 1
    # and a Leased group is never switched to another apartment
    third = _second_apartment(c, "Unit 3 / 3 Claim Street")
    _list_apartment(c, gid, third, 3, e["id"])
    r3 = client.post(f"/api/apartments/{third}/lease", json={"group_id": gid})
    assert r3.status_code == 409 and "never switched" in r3.json()["detail"], r3.text
    assert _apt(c, third)["leased_at"] is None and _status(c, gid)["apartment_id"] == other


def test_post_lease_completes_a_leased_groups_unclaimed_apartment_but_never_another_claim(client):
    """Legacy state (today's sequential bug): group Leased, its apartment never claimed. The claim is completed for that same group
    and apartment only; an apartment claimed by someone else meanwhile is refused and the group is left alone."""
    e = _submit_public(client, first_name="Repair", last_name="Lease", email="repair@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    aid = _status(c, gid)["apartment_id"]
    c.execute("UPDATE groups SET status='Leased' WHERE id=?", (gid,))
    c.commit()
    r = client.post(f"/api/apartments/{aid}/lease", json={"group_id": gid})
    assert r.status_code == 200, r.text
    a = _apt(c, aid)
    assert a["leased_group_id"] == gid and a["leased_at"] == r.json()["leased_at"]
    assert row(c.execute("SELECT detail FROM audit_log WHERE action='apartment_claim' AND entity_id=?", (str(aid),)))["detail"].startswith("Unit 9 / 9 Intro Street; group")
    # not found / already claimed by another group
    assert client.post("/api/apartments/999999/lease", json={}).status_code == 404
    e2 = _submit_public(client, first_name="Other", last_name="Group", email="other-group@example.com")
    gid2 = _plant_group(c, e2["id"])
    aid2 = _status(c, gid2)["apartment_id"]
    c.execute("UPDATE groups SET status='Leased' WHERE id=?", (gid2,))
    c.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='x' WHERE id=?", (now_iso(), gid, aid2))
    c.commit()
    r2 = client.post(f"/api/apartments/{aid2}/lease", json={"group_id": gid2})
    assert r2.status_code == 409
    assert _apt(c, aid2)["leased_group_id"] == gid, "another group's claim is never overwritten"


def test_post_lease_without_a_group_is_a_guarded_claim_and_a_second_one_is_refused(client):
    c = _db(client)
    aid = _second_apartment(c, "Unit 4 / 4 Claim Street")
    r = client.post(f"/api/apartments/{aid}/lease", json={})
    assert r.status_code == 200, r.text
    a = _apt(c, aid)
    assert a["leased_group_id"] is None and a["leased_at"] == r.json()["leased_at"] and a["leased_by"]
    r2 = client.post(f"/api/apartments/{aid}/lease", json={})   # same binding (no group): follow-up again, no second claim
    assert r2.status_code == 200 and r2.json()["repeated"] is True, r2.text
    assert _apt(c, aid) == a
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='apartment_claim' AND entity_id=?", (str(aid),)).fetchone()[0] == 1
    # a different binding is refused: the claim belongs to nobody-in-particular, a group cannot take it over
    e = _submit_public(client, first_name="Late", last_name="Group", email="late-group@example.com")
    gid = _plant_group(c, e["id"])
    _list_apartment(c, gid, aid, 2, e["id"])
    assert client.post(f"/api/apartments/{aid}/lease", json={"group_id": gid}).status_code == 409
    assert _apt(c, aid) == a and _status(c, gid)["status"] == "Proposed"


def test_group_leased_by_status_patch_claims_its_apartment_in_the_same_step(client, monkeypatch):
    e = _submit_public(client, first_name="Patch", last_name="Lease", email="patch-lease@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    aid = _status(c, gid)["apartment_id"]
    r = client.patch(f"/api/groups/{gid}", json={"status": "Leased"})
    assert r.status_code == 200, r.text
    assert _apt(c, aid)["leased_group_id"] == gid and _status(c, gid)["status"] == "Leased"
    # the group's stored apartment gone between the route's read and the guard: today the schema cascades the group away with its
    # apartment (groups.apartment_id NOT NULL ... ON DELETE CASCADE), so the controlled answer is 404, never a claim or a reassignment
    e2 = _submit_public(client, first_name="Gone", last_name="Apartment", email="gone-apt@example.com")
    gid2 = _plant_group(c, e2["id"])
    aid2 = _status(c, gid2)["apartment_id"]
    original = application.retention_guard

    @contextmanager
    def delete_before_acquisition(conn, label=""):
        if label == "group status":
            conn.execute("DELETE FROM apartments WHERE id=?", (aid2,))
            conn.commit()
        with original(conn, label=label):
            yield conn
    monkeypatch.setattr(application, "retention_guard", delete_before_acquisition)
    r2 = client.patch(f"/api/groups/{gid2}", json={"status": "Leased"})
    # Stock lifecycle (9 Nov 2026, D1): BEFORE, deleting the apartment cascaded the group away (404 here, group row gone).
    # AFTER, the group survives its apartment (groups.apartment_id ON DELETE SET NULL): it is held with NO current offer and the
    # lease is refused inside the guard (409, "no apartment to lease"); nothing is claimed or revived. The protected property is
    # the same - the pre-guard read is not authoritative and the stale lease never lands.
    assert r2.status_code == 409, r2.text
    assert _status(c, gid2) == {"status": _status(c, gid2)["status"], "apartment_id": None} and _status(c, gid2)["status"] != "Leased" and _apt(c, aid2) is None
    monkeypatch.undo()
    assert client.patch("/api/groups/999999", json={"status": "Leased"}).status_code == 404


def test_enquiry_status_edit_is_conditional_and_a_purged_enquiry_is_a_controlled_404(client, monkeypatch):
    e = _submit_public(client, first_name="Edit", last_name="Race", email="edit-race@example.com")
    c = _db(client)
    real = enquiries._length_errors
    flipped = []

    def flip_status_between_read_and_write(fields):
        if not flipped:
            flipped.append(True)   # inside the "enquiry edit" step, after the guarded read, before the conditional write
            assert dbm.guarded(c)
            c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=?", (e["id"],))
            dbm.maybe_commit(c)
        return real(fields)
    monkeypatch.setattr(enquiries, "_length_errors", flip_status_between_read_and_write)
    r = client.patch(f"/api/enquiries/{e['id']}", json={"status": "Withdrawn"})
    assert r.status_code == 409 and "changed while you were working" in r.json()["detail"], r.text
    assert flipped
    assert row(c.execute("SELECT status FROM enquiries WHERE id=?", (e["id"],)))["status"] == "New", "the refused step rolled back as a whole"
    monkeypatch.undo()
    r = client.patch(f"/api/enquiries/{e['id']}", json={"status": "Withdrawn"})
    assert r.status_code == 200 and r.json()["status"] == "Withdrawn", r.text
    # gone before the guard: controlled 404, nothing written
    original = enquiries.retention_guard

    @contextmanager
    def delete_before_acquisition(conn, label=""):
        if label == "enquiry edit":
            conn.execute("DELETE FROM enquiries WHERE id=?", (e["id"],))
            conn.commit()
        with original(conn, label=label):
            yield conn
    monkeypatch.setattr(enquiries, "retention_guard", delete_before_acquisition)
    assert client.patch(f"/api/enquiries/{e['id']}", json={"status": "Withdrawn"}).status_code == 404


def test_follow_up_failure_after_the_committed_claim_is_reported_honestly_and_is_finished_by_repeating(client, monkeypatch):
    """The claim commits at guard exit; the GP8 re-rank of the other groups runs after it. If that follow-up fails, the lease IS
    recorded and the answer must say so (never 'nothing was changed'), nothing is unclaimed, and repeating the request for the
    same group finishes the follow-up without a second claim. Two failure shapes: the whole follow-up raising (503), and the
    builder's isolated per-group steps leaving some groups pending (200 with rerank.status 'partial' and a plain message)."""
    e = _submit_public(client, first_name="Follow", last_name="Up", email="follow-up@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    aid = _status(c, gid)["apartment_id"]
    e2 = _submit_public(client, first_name="Other", last_name="Lister", email="other-lister@example.com")
    gid2 = _plant_group(c, e2["id"])
    other = _second_apartment(c)
    c.execute("UPDATE groups SET apartment_id=? WHERE id=?", (aid, gid2))   # the second group also sits on the first apartment
    c.commit()
    _list_apartment(c, gid2, aid, 1, e2["id"])
    _list_apartment(c, gid2, other, 2, e2["id"])
    real = matching.rerank_after_lease
    failures = []

    def failing_rerank(*a, **k):
        if not failures:
            failures.append(True)
            raise dbm.RetentionLockTimeout("lease rerank: lock not obtained")
        return real(*a, **k)
    monkeypatch.setattr(matching, "rerank_after_lease", failing_rerank)
    r = client.post(f"/api/apartments/{aid}/lease", json={"group_id": gid})
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "The lease is recorded" in detail and f"for group {gid}" in detail and "did not finish" in detail and "Nothing was changed" not in detail
    a = _apt(c, aid)
    assert a["leased_group_id"] == gid and a["leased_at"] and _status(c, gid)["status"] == "Leased", "the committed claim is never rolled back or hidden"
    assert row(c.execute("SELECT status FROM enquiries WHERE id=?", (e["id"],)))["status"] == "Leased"
    assert not rows(c.execute("SELECT 1 FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(aid),))), "no follow-up audit for an unfinished follow-up"
    assert _status(c, gid2)["apartment_id"] == aid, "the other group was not re-ranked yet"
    assert [p["group_id"] for p in matching.pending_lease_reranks(c)] == [gid2], "the pending work is visible from the data, not from memory"
    # recovery: the same request again (or PATCH status Leased on the unchanged group) runs only the follow-up
    r2 = client.post(f"/api/apartments/{aid}/lease", json={"group_id": gid})
    assert r2.status_code == 200 and r2.json()["repeated"] is True and "not repeated" in r2.json()["message"], r2.text
    assert _apt(c, aid) == a
    assert [x["group_id"] for x in r2.json()["affected"]] == [gid2]
    # Stock lifecycle (9 Nov 2026): BEFORE, the follow-up moved the group blindly to the next LISTED option (`other`). AFTER, the
    # next option is taken only when it passes every hard rule today; this planted one-person fixture passes none (P3), so the
    # group is held with no current offer (NULL pointer), the leased apartment is off its list and the 'lease' flag is set. The
    # moved-to-a-valid-option path is covered with real matched groups in test_data_and_api (GP8) and test_stock_lifecycle.
    assert _status(c, gid2)["apartment_id"] is None and row(c.execute("SELECT flag_kind FROM groups WHERE id=?", (gid2,)))["flag_kind"] == "lease"
    assert aid not in {x["apartment_id"] for x in rows(c.execute("SELECT apartment_id FROM group_apartments WHERE group_id=?", (gid2,)))}
    assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(aid),)).fetchone()[0] == 1
    assert matching.pending_lease_reranks(c) == []
    r3 = client.patch(f"/api/groups/{gid}", json={"status": "Leased"})
    assert r3.status_code == 200 and r3.json()["gp8"]["repeated"] is True, r3.text
    assert _apt(c, aid) == a and c.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_status' AND entity_id=? AND detail LIKE '%claimed%'", (str(gid),)).fetchone()[0] == 1
    # a group that is Leased with a different binding is still never switched, and the claim is never repeated or moved
    assert client.post(f"/api/apartments/{other}/lease", json={"group_id": gid}).status_code == 409
    assert _apt(c, other)["leased_at"] is None


def test_partial_follow_up_is_answered_as_recorded_lease_with_pending_groups(client, monkeypatch):
    e = _submit_public(client, first_name="Partial", last_name="Rerank", email="partial@example.com")
    c = _db(client)
    gid = _plant_group(c, e["id"])
    aid = _status(c, gid)["apartment_id"]
    e2 = _submit_public(client, first_name="Pending", last_name="Group", email="pending-group@example.com")
    gid2 = _plant_group(c, e2["id"])
    other = _second_apartment(c)
    c.execute("UPDATE groups SET apartment_id=? WHERE id=?", (aid, gid2))
    c.commit()
    _list_apartment(c, gid2, aid, 1, e2["id"])
    _list_apartment(c, gid2, other, 2, e2["id"])
    real = matching._rerank_after_lease_isolated
    once = []

    def one_group_fails(*a, **k):
        if not once:   # the builder's isolation: the step for gid2 failed, the lease stands, the group is reported pending
            once.append(True)
            return [], [{"group_id": gid2, "category": "lock_timeout"}]
        return real(*a, **k)
    monkeypatch.setattr(matching, "_rerank_after_lease_isolated", one_group_fails)
    r = client.post(f"/api/apartments/{aid}/lease", json={"group_id": gid})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rerank"]["status"] == "partial" and body["rerank"]["pending_group_ids"] == [gid2]
    assert "The lease is recorded" in body["message"] and "still pending for 1 group(s)" in body["message"] and "will not be repeated" in body["message"]
    a = _apt(c, aid)
    assert a["leased_group_id"] == gid and _status(c, gid)["status"] == "Leased"
    assert "RERANK PENDING" in row(c.execute("SELECT detail FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(aid),)))["detail"]
    # the staff recovery path: the apartment page shows the pending work from the data and offers "Finish updating other groups",
    # which is the same POST with the apartment's own leased_group_id (idempotent; never a second claim, never a switch)
    d = client.get(f"/api/apartments/{aid}").json()
    assert d["is_leased"] and d["leased_group_id"] == gid and d["rerank_pending_group_ids"] == [gid2]
    r2 = client.post(f"/api/apartments/{aid}/lease", json={"group_id": d["leased_group_id"]})
    assert r2.status_code == 200 and r2.json()["repeated"] is True and r2.json()["rerank"]["status"] == "complete", r2.text
    # (stock lifecycle, 9 Nov 2026: BEFORE the group moved blindly to `other`; AFTER only a rule-passing option is taken and this
    # one-person fixture passes none, so the group is held with no current offer - see the test above)
    assert _apt(c, aid) == a and _status(c, gid2)["apartment_id"] is None and _status(c, gid2)["status"] != "Leased"
    assert client.get(f"/api/apartments/{aid}").json()["rerank_pending_group_ids"] == []
    staff_js = open(os.path.join(os.path.dirname(__file__), "..", "client", "staff.js"), encoding="utf-8").read()
    assert "finish-lease" in staff_js and "rerank_pending_group_ids" in staff_js and "Finish updating other groups" in staff_js


# ---------------------------------------------------------------- E (8 Nov 2026): exactly one operation per group PATCH
def test_group_patch_accepts_exactly_one_operation_and_refuses_mixed_or_forbidden_bodies_before_any_write(client, monkeypatch):
    """A status change is its own guarded step; the introduction edit / offer switch / notes work from the pre-step read and commit
    on their own, and is_primary used to be refused only after those had committed. Now: is_primary -> 403 first; any body with
    other than exactly one operation (status, intro_text, apartment_id, rerank_ack, discount_note) -> 422; in every refused case
    nothing is written: status, lease claim, apartment, introduction draft, notes and audit are unchanged."""
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    client.post(f"/api/groups/{gid}/introduction").raise_for_status()
    cols = "status, apartment_id, intro_text, is_primary, rerank_note, flag_kind, discount_note, updated_at"
    before = row(db.execute(f"SELECT {cols} FROM groups WHERE id=?", (gid,)))
    apt_before = row(db.execute("SELECT leased_at, leased_group_id FROM apartments WHERE id=?", (before["apartment_id"],)))
    ist_before = ai_queue.intro_state(db, gid)
    audits = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    calls = []
    monkeypatch.setattr(application, "_set_group_status", lambda *a, **k: calls.append(a) or None)
    cases = [({"status": "Leased", "is_primary": True}, 403, "by hand"),
             ({"is_primary": True, "discount_note": "x"}, 403, "by hand"),
             ({"status": "Leased", "intro_text": "Hello all", "draft_id": ist_before.get("draft_id"), "context_hash": ist_before.get("context_hash")}, 422, "status change and the introduction edit"),
             ({"status": "Introduced", "intro_text": "Hello all"}, 422, "status change and the introduction edit"),
             ({"status": "Leased", "apartment_id": before["apartment_id"]}, 422, "status change and the apartment offer"),
             ({"status": "Leased", "discount_note": "note"}, 422, "status change and the discount note"),
             ({"status": "Group confirmed", "rerank_ack": True}, 422, "status change and the re-rank acknowledgement"),
             ({"intro_text": "Hello all", "apartment_id": before["apartment_id"]}, 422, "introduction edit and the apartment offer"),
             ({}, 422, "nothing to change"),
             ({"intro_drafted_at": ist_before.get("drafted_at")}, 422, "nothing to change")]
    for body, code, words in cases:
        r = client.patch(f"/api/groups/{gid}", json=body)
        assert r.status_code == code and words in r.json()["detail"], (body, r.status_code, r.text)
    assert calls == [], "the status step was never started"
    assert row(db.execute(f"SELECT {cols} FROM groups WHERE id=?", (gid,))) == before
    assert row(db.execute("SELECT leased_at, leased_group_id FROM apartments WHERE id=?", (before["apartment_id"],))) == apt_before
    assert ai_queue.intro_state(db, gid) == ist_before
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits
    monkeypatch.undo()
    # each operation on its own still works (the page sends them one at a time): note, acknowledgement, edit, then the status
    assert client.patch(f"/api/groups/{gid}", json={"discount_note": "Offered $10/wk off"}).status_code == 200
    assert client.patch(f"/api/groups/{gid}", json={"rerank_ack": True}).status_code == 200
    r = client.patch(f"/api/groups/{gid}", json=_edit(ist_before, before["intro_text"] + "\nSee you soon."))
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/groups/{gid}", json=_introduce(client, gid))   # the edit made a newer row: the page re-read it
    assert r.status_code == 200 and row(db.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Introduced", r.text
    # the intended switch + lease in ONE step is the POST apartment lease route, unaffected by the PATCH rule (tested above)
