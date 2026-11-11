"""Source integrity after a GP9(e) lead takeover (root GO, 9 Nov 2026; reviewer probe /tmp/mmai-c200-probe-takeover-names).
The enquirer is barred and the party stays: the SAME enquiries row now carries the promoted companion. The stored review/briefs were
written about the old people, so (1) every saved ref map carries the immutable person_uid of the person it describes, (2) the group's
review/brief currentness is withdrawn in the same guarded step as the takeover (evidence hash cleared, approval and brief stale,
not ready; history rows kept), (3) output captured about the old identities can no longer be applied. Real matching.bar_enquiry
route; synthetic SQLite; scripted _ask; no provider."""
import json

from server import ai_queue, db as dbmod, identities, matching
from server.db import row, rows
from tests.test_t6_grounding import _party_group
from tests.test_uid_allocation import _uid_of


def _maps(db, gid):
    rr = [r["refs_json"] for r in rows(db.execute("SELECT refs_json FROM review_rounds WHERE group_id=? AND refs_json IS NOT NULL", (gid,)))]
    br = [r["refs_json"] for r in rows(db.execute("SELECT refs_json FROM group_briefs WHERE group_id=? AND refs_json IS NOT NULL", (gid,)))]
    return [json.loads(x) for x in rr + br]


def test_saved_ref_maps_carry_the_person_uid_of_each_described_person(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    maps = _maps(db, g["id"])
    assert maps, "fixture must have written at least one saved ref map"
    for m in maps:
        by_eid = {(i["enquiry_id"], i["party_member_id"]): i for i in m.values()}
        assert by_eid[(lead["id"], None)]["person_uid"] == astrid and by_eid[(solo["id"], None)]["person_uid"] == devi
        members = sorted(i["person_uid"] for k, i in by_eid.items() if k[0] == lead["id"] and k[1] is not None)
        assert members == sorted([bo, cy])
    idents = ai_queue._ref_identities(db, g)
    assert {i["person_uid"] for i in idents.values()} == {astrid, bo, cy, devi}
    # the live map is intact before the takeover
    assert ai_queue.refs_intact(db, maps[0]) is True


def test_lead_takeover_withdraws_review_and_brief_currentness_and_keeps_history(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    assert g["ai_status"] == "ok" and g["evidence_hash"], g
    n_rr = db.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (g["id"],)).fetchone()[0]
    n_br = db.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (g["id"],)).fetchone()[0]
    assert n_rr >= 1 and n_br >= 1
    out = matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    assert out["party_stayed"] and g["id"] in out["awaiting_groups"], out
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after["evidence_hash"] is None and after["ai_status"] == "stale" and after["brief_status"] != "ok" and after["ready_at"] is None
    assert after["status"] == matching.AWAITING
    assert "different set of people" in (after["held_reason"] or "")
    rd = ai_queue.is_ready(db, after)
    assert rd["ready"] is False and any("stale" in r for r in rd["reasons"])
    # history is preserved, not deleted
    assert db.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (g["id"],)).fetchone()[0] == n_rr
    assert db.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (g["id"],)).fetchone()[0] == n_br
    # no saved round still equals the group's (cleared) hash: nothing renders as current
    assert not row(db.execute("SELECT 1 FROM review_rounds WHERE group_id=? AND evidence_hash IS NOT NULL AND evidence_hash=?", (g["id"], after["evidence_hash"])))


def test_member_bar_with_party_staying_also_withdraws_currentness(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    bo_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))["id"]
    out = matching.bar_enquiry(db, lead["id"], "staff", "test bar", person=bo_pm, party_choice="stay")
    assert out["party_stayed"], out
    after = row(db.execute("SELECT evidence_hash, ai_status, brief_status, ready_at FROM groups WHERE id=?", (g["id"],)))
    assert after["evidence_hash"] is None and after["ai_status"] == "stale" and after["brief_status"] != "ok" and after["ready_at"] is None


def test_output_captured_about_the_old_identities_cannot_be_applied_after_the_takeover(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    with dbmod.retention_guard(db, label="test capture"):   # the capture refreshes rule ranks: one guarded step, as the worker does
        p = ai_queue._payload(db, g)          # captured BEFORE the takeover: U1 = Astrid
        idents = ai_queue._ref_identities(db, g)
    old_map = dict(p["refs_json"])
    matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    live = {i["person_uid"]: i for i in identities.active(db, lead["id"])}
    assert old_map["U1"]["person_uid"] not in live                      # the barred lead's identity is retired
    assert ai_queue.refs_intact(db, old_map) is False                   # same enquiries row, different person -> not intact
    assert ai_queue.people_intact(db, g["id"], p["person_refs"]) is False
    # the brief writer refuses the stale snapshot and writes no row about it
    n_br = db.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (g["id"],)).fetchone()[0]
    g_now = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    with dbmod.retention_guard(db, label="test stale brief apply"):
        ok = ai_queue._apply_briefs(db, g_now, p, idents, {"group": {}, "members": {}}, None, dbmod.now_iso())
    assert ok is False
    assert db.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (g["id"],)).fetchone()[0] == n_br
    # a map whose uid matches the row's CURRENT identity is intact (the promoted person's own new map)
    fresh = ai_queue._ref_identities(db, g_now)
    assert fresh["U1"]["person_uid"] in live and live[fresh["U1"]["person_uid"]]["role"] == "lead"
    assert ai_queue.refs_intact(db, {k: {kk: v[kk] for kk in ("enquiry_id", "party_member_id", "person_uid")} for k, v in fresh.items()}) is True


# --- provenance of the row's INDIVIDUAL answers after a lead takeover (root, 9 Nov 2026) ----------------------------------------
def _took_over(db, monkeypatch, with_notes=True):
    a, lead, solo, g = _party_group(db, monkeypatch)
    if with_notes:
        with dbmod.retention_guard(db, label="test seed notes"):
            db.execute("UPDATE enquiries SET else_text='Step-free access is essential for me.', notes_status='ok', notes_packet_json='[]', notes_input_key='k', notes_residual=0 WHERE id=?", (lead["id"],))
            db.execute("""INSERT INTO note_facts(enquiry_id, field, category, validator_status, model, catalogue_version, created_at)
                          VALUES (?,?,?,?,?,?,?)""", (lead["id"], "else_text", "step_free_access", "accepted", "m", "v", dbmod.now_iso()))
            db.execute("INSERT INTO note_extraction_cache(input_key, enquiry_id, accepted_json, residual, model, catalogue_version, created_at) VALUES (?,?,?,?,?,?,?)",
                       ("k", lead["id"], "[]", 0, "m", "v", dbmod.now_iso()))
    before = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    return lead, solo, g, before


def test_lead_takeover_flags_the_former_enquirers_individual_answers_and_closes_the_notes_step(db, monkeypatch):
    lead, solo, g, before = _took_over(db, monkeypatch)
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    assert e["first_name"] == "Bo" and e["lead_answers_unverified_at"]
    assert e["notes_status"] == "unverified" and e["notes_packet_json"] is None and e["notes_input_key"] is None
    assert db.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (lead["id"],)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM note_extraction_cache WHERE enquiry_id=?", (lead["id"],)).fetchone()[0] == 0
    # the stored answers themselves are history, not rewritten
    for k in ("doing", "school", "diet", "social", "routine", "else_text", "budget_option", "move_in", "rooms"):
        assert e[k] == before[k], k
    # the notes step never re-reads the former enquirer's text as the new lead's: not a candidate, not retried
    assert ai_queue.notes_pending_count(db) == 0
    assert not any(r["id"] == lead["id"] for r in ai_queue._notes_candidates(db, None, None))
    ai_queue.retry_failed(db, "staff", [g["id"]])
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (lead["id"],)))["notes_status"] == "unverified"
    rd = ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    assert rd["ready"] is False and any("change of enquirer" in r for r in rd["reasons"])


def test_masked_individual_answers_in_packet_brief_identities_and_introduction_while_shared_answers_stay(db, monkeypatch):
    lead, solo, g, before = _took_over(db, monkeypatch)
    gnow = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    idents = ai_queue._ref_identities(db, gnow)
    units = []
    for eid in (int(x) for x in gnow["member_key"].split(",")):
        e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        units.append(matching.unit_from_row(e, rows(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))))
    by_ref = {m["ref"]: m for m in matching.members_for_ai(units)}   # the compatibility packet as the writer builds it
    lead_ref = next(r for r, i in idents.items() if i["enquiry_id"] == lead["id"] and i["party_member_id"] is None)
    m = by_ref[lead_ref]
    for k in ("doing", "school", "course", "level", "routine", "tidy", "social", "guests", "diet"):
        assert m[k] is None, k                       # the former enquirer's self-description is not sent as Bo's
    assert m["notes_facts"] == []
    assert m["party"] == before["party"] and m["party_size"] == 2 and m["move_in"] == before["move_in"]   # party-wide answers kept
    assert idents[lead_ref]["individually_recorded"] is False     # the brief treats the promoted lead as having no personal facts
    assert idents[lead_ref]["person_uid"] == _uid_of(db, lead["id"])  # ... and names the promoted person's own uid
    solo_ref = next(r for r, i in idents.items() if i["enquiry_id"] == solo["id"])
    assert idents[solo_ref]["individually_recorded"] is True   # the other lead is untouched
    intro = {x["enquiry_id"]: x for x in ai_queue.intro_members(db, g["id"]) if x["is_lead"]}
    i1 = intro[lead["id"]]
    assert i1["first_name"] == "Bo" and i1["doing"] is None and i1["intro"] is None and i1["diet"] is None
    assert any("former enquirer" in s for s in i1["intro_missing"])
    assert not any("former enquirer" in s for s in intro[solo["id"]]["intro_missing"])


def test_explicit_staff_confirmation_lifts_the_flag_and_reopens_the_notes_step_but_an_edit_alone_does_not(db, monkeypatch):
    from server.enquiries import update_enquiry
    lead, solo, g, before = _took_over(db, monkeypatch)
    update_enquiry(db, lead["id"], {"else_text": "I would like somewhere quiet."}, "staff")
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    assert e["lead_answers_unverified_at"] and e["notes_status"] == "unverified" and e["else_text"] == "I would like somewhere quiet."
    update_enquiry(db, lead["id"], {"lead_answers_confirmed": True}, "staff")
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    assert e["lead_answers_unverified_at"] is None and e["notes_status"] == "pending"
    with dbmod.retention_guard(db, label="test capture"):
        idents = ai_queue._ref_identities(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    lead_ref = next(r for r, i in idents.items() if i["enquiry_id"] == lead["id"] and i["party_member_id"] is None)
    assert idents[lead_ref]["individually_recorded"] is True


def test_staff_can_record_the_former_enquirers_individual_answers_as_not_given(db, monkeypatch):
    """The client's 'Not theirs - record as not given' action: blanks the individual answers and lifts the flag in one save; the
    party-wide answers (budget, dates, rooms, party) are untouched; the sharing consent given on the original form is unchanged."""
    from server.enquiries import update_enquiry
    lead, solo, g, before = _took_over(db, monkeypatch)
    blank = {"lead_answers_confirmed": True, **{k: "" for k in ("doing", "school", "school_other", "course", "level", "work", "routine", "tidy", "social", "guests", "diet", "else_text", "intro")}}
    update_enquiry(db, lead["id"], blank, "staff")
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (lead["id"],)))
    assert e["lead_answers_unverified_at"] is None
    assert all((e[k] or "") == "" for k in ("doing", "school", "diet", "social", "routine", "else_text", "intro", "work"))
    assert e["budget_option"] == before["budget_option"] and e["move_in"] == before["move_in"] and e["rooms"] == before["rooms"]
    assert e["consent_share"] == before["consent_share"]
    assert e["notes_status"] in ("none", "pending")   # blank notes: nothing to read (or re-read on the next pass)


def test_refs_intact_checks_the_member_identity_branch_as_well_as_the_lead(db, monkeypatch):
    """Both statements of the uid check (lead: party_member_id IS NULL; member: party_member_id = id) on whichever database the
    suite runs (PG rejects a parameterised 'IS ?'). A companion who leaves retires their identity: the map captured before is not
    intact for that ref, while a map of the current identities is."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    with dbmod.retention_guard(db, label="test capture"):
        before = {k: {kk: v[kk] for kk in ("enquiry_id", "party_member_id", "person_uid")} for k, v in ai_queue._ref_identities(db, g).items()}
    member_refs = [k for k, v in before.items() if v["party_member_id"] is not None]
    assert member_refs and all(v["person_uid"] for v in before.values())
    assert ai_queue.refs_intact(db, before) is True                                     # lead + member branches, all live
    assert ai_queue.refs_intact(db, {k: dict(v, person_uid="not-a-live-uid") for k, v in before.items() if k == member_refs[0]}) is False
    bo_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))["id"]
    bo_ref = next(k for k, v in before.items() if v["party_member_id"] == bo_pm)
    matching.bar_enquiry(db, lead["id"], "staff", "test bar", person=bo_pm, party_choice="stay")
    assert ai_queue.refs_intact(db, {bo_ref: before[bo_ref]}) is False                   # Bo's row is gone and the identity retired
    assert ai_queue.refs_intact(db, {k: v for k, v in before.items() if k != bo_ref}) is True   # everyone else unchanged (lead branch included)
