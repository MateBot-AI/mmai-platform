"""r7 introduction context (ai_queue side; 8 November 2026). Pins the builder-owned half of reviews/20261108-r7-implementation-contract.md:
  * intro_offer_projection / intro_state / intro_basis_items / intro_capture are PURE reads (repeated calls change no row);
  * the offer fingerprint covers the exact apt + ginfo objects: same price and bedding but another address or a changed displayed
    feature state is a different offer;
  * capture -> (model call) -> record: a change to the address / selected apartment / a person's intro details between the two
    steps is REFUSED with a static ValueError text and nothing is written; a draft recorded through the pre-r7 members= form has
    no offer context and is never current;
  * two drafts in the same second are distinguished by draft_id (the row id), not drafted_at;
  * basis provenance is written once at the brief's apply (subjects by original packet ref + stable ids, per-field own /
    party_reported / none coverage, cited apartment values) and consumed immutably: a note about another apartment, a changed
    or unverifiable apartment value, an unresolvable subject (companion slot without a real party_members row) or a legacy brief
    without metadata never reaches the drafter; a name change does not move a note; apartment-only notes need no subjects;
  * intro_state additive keys and the stale-reason order (facts -> no context -> offer -> profile -> legacy basis -> current).
Everything is a fabricated double; no model call, no network, no real people. Not a launch-readiness statement.
"""
import json
import pytest
from server import ai, ai_queue, facts, matching, rules
from server.db import rows, row
from tests.test_launch_neutral import _apt
from tests.test_t6_grounding import _run_and_approve


def _cap(db, gid, **over):
    proj = ai_queue.intro_offer_projection(db, gid)
    return dict(proj, basis=[], basis_scope="current", **over)


def _tables(db):
    from server import db as dbmod
    if dbmod.is_postgres():
        return [r["table_name"] for r in rows(db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"))]
    return [r["name"] for r in rows(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))]


def _dump(db):
    out = {}
    for t in _tables(db):
        out[t] = sorted(json.dumps(dict(r), sort_keys=True, default=str) for r in rows(db.execute(f"SELECT * FROM {t}")))
    return json.dumps(out, sort_keys=True)


def _group_brief(db, gid):
    return row(db.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='group' AND status='ok' ORDER BY id DESC LIMIT 1", (gid,)))


# ---------------------------------------------------------------- purity
def test_projection_state_basis_and_capture_are_pure_reads(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    before = _dump(db)
    for _ in range(3):
        p = ai_queue.intro_offer_projection(db, g["id"])
        assert p and p["apartment_id"] == a["id"] and p["members"] and p["offer_fingerprint"] and p["profile_hash"]
        assert ai_queue.intro_state(db, g["id"])["current"] is False
        assert ai_queue.intro_basis_items(db, g["id"], a["id"]) is not None
        cap = ai_queue.intro_capture(db, g["id"])
        assert cap["context_hash"] == ai_queue.intro_context_hash(p) and cap["basis_scope"] == "current" and cap["circulation"]["ok"]
    assert _dump(db) == before, "pure reads must not write, refresh or enqueue anything"
    # the exact objects the drafter sees are the fingerprinted ones
    assert cap["offer_fingerprint"] == ai_queue.intro_offer_fingerprint(cap["apt"], cap["ginfo"])
    assert {f["key"] for f in cap["apt"]["features"]} and all(f["recorded"] in ("yes", "no", "unknown") for f in cap["apt"]["features"])


# ---------------------------------------------------------------- fingerprint
def test_offer_fingerprint_moves_for_address_and_feature_not_only_price():
    apt = {"id": 1, "address": "Unit 1 / 1 Neutral Street", "type_code": "2B", "type_label": "Two bedroom", "suburb": "Carlton", "bills_included": True,
           "features": [{"key": "balcony", "label": "Balcony", "recorded": "unknown"}]}
    ginfo = {"lease_start": "2027-02-01", "lease_end": "2028-01-31", "rate_kind": "12m", "rate": "12-month rate", "total_weekly": 1100.0, "rooms": []}
    base = ai_queue.intro_offer_fingerprint(apt, ginfo)
    assert ai_queue.intro_offer_fingerprint(dict(apt), dict(ginfo)) == base
    assert ai_queue.intro_offer_fingerprint({**apt, "address": "Unit 2 / 1 Neutral Street"}, ginfo) != base, "same price + bedding, other address"
    assert ai_queue.intro_offer_fingerprint({**apt, "features": [{"key": "balcony", "label": "Balcony", "recorded": "yes"}]}, ginfo) != base, "displayed feature state"
    assert ai_queue.intro_offer_fingerprint(apt, {**ginfo, "lease_end": "2028-02-29"}) != base
    assert ai_queue.intro_offer_fingerprint({**apt, "id": 2}, ginfo) != base


def test_projection_fingerprint_moves_for_address_edit_and_feature_flag(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    f0 = ai_queue.intro_offer_projection(db, g["id"])["offer_fingerprint"]
    db.execute("UPDATE apartments SET address='Unit 9 / 9 Other Street' WHERE id=?", (a["id"],)); db.commit()
    f1 = ai_queue.intro_offer_projection(db, g["id"])["offer_fingerprint"]
    assert f1 != f0
    db.execute("UPDATE apartments SET feat_balcony=CASE WHEN feat_balcony=1 THEN 0 ELSE 1 END WHERE id=?", (a["id"],)); db.commit()
    assert ai_queue.intro_offer_projection(db, g["id"])["offer_fingerprint"] not in (f0, f1)


# ---------------------------------------------------------------- capture -> apply races
def test_address_edit_between_capture_and_record_is_refused_and_writes_nothing(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    cap = ai_queue.intro_capture(db, g["id"])
    n = row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE kind='intro'"))["n"]
    db.execute("UPDATE apartments SET address='Unit 9 / 9 Other Street' WHERE id=?", (a["id"],)); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_OFFER):
        ai_queue.record_intro_draft(db, g["id"], "drafted for the old address", "AI", "tester", capture=cap)
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE kind='intro'"))["n"] == n
    assert ai_queue.intro_state(db, g["id"])["stale_reason"] == ai_queue.INTRO_STALE_NONE
    # a fresh capture after the edit is accepted and current
    ai_queue.record_intro_draft(db, g["id"], "drafted for the new address", "AI", "tester", capture=ai_queue.intro_capture(db, g["id"])); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] and st["offer_current"] and st["profile_current"] and st["apartment_id"] == a["id"]


def test_feature_flag_edit_between_capture_and_record_is_refused(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    cap = ai_queue.intro_capture(db, g["id"])
    db.execute("UPDATE apartments SET feat_balcony=CASE WHEN feat_balcony=1 THEN 0 ELSE 1 END WHERE id=?", (a["id"],)); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_OFFER):
        ai_queue.record_intro_draft(db, g["id"], "x", "AI", "tester", capture=cap)


def test_selected_apartment_switch_between_capture_and_record_is_refused(db, monkeypatch):
    """Two equal offers (same type, price, bedding) listed BEFORE the review so the evidence covers both; a GP6(b)-style switch
    of groups.apartment_id to the rank-2 listing between capture and record is refused."""
    from tests.test_launch_neutral import _pool
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db)
    a2 = _apt(db, address="Unit 2 / 2 Neutral Street")
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND rank=1", (g["id"],)))
    cols = [k for k in ga.keys() if k not in ("id", "apartment_id", "rank")]
    db.execute(f"INSERT INTO group_apartments(apartment_id, rank, {', '.join(cols)}) VALUES (?, 2, {', '.join('?' * len(cols))})", (a2["id"], *[ga[k] for k in cols]))
    db.commit()
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    listed = [r["apartment_id"] for r in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],)))]
    assert len(listed) == 2 and listed[0] == g["apartment_id"]
    other = listed[1]
    cap = ai_queue.intro_capture(db, g["id"])
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (other, g["id"])); db.commit()
    a2 = {"id": other}
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_APARTMENT):
        ai_queue.record_intro_draft(db, g["id"], "x", "AI", "tester", capture=cap)
    # equal-price / equal-bedding switch: the projection's apartment id and address differ, so the old draft could never be current
    assert ai_queue.intro_offer_projection(db, g["id"])["offer_fingerprint"] != cap["offer_fingerprint"]
    # the stored offer row vanishing is its own static refusal
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], a2["id"])); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_NO_OFFER):
        ai_queue.record_intro_draft(db, g["id"], "x", "AI", "tester", capture=cap)


def test_intro_detail_edit_between_capture_and_record_is_refused(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    cap = ai_queue.intro_capture(db, g["id"])
    db.execute("UPDATE enquiries SET intro_age=? WHERE id=?", (44, cap["members"][0]["enquiry_id"])); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_PROFILE):
        ai_queue.record_intro_draft(db, g["id"], "x", "AI", "tester", capture=cap)
    # a draft recorded before the edit goes stale with the profile reason, offer still current
    ai_queue.record_intro_draft(db, g["id"], "fresh", "AI", "tester", capture=ai_queue.intro_capture(db, g["id"])); db.commit()
    assert ai_queue.intro_state(db, g["id"])["current"]
    db.execute("UPDATE enquiries SET intro_age=? WHERE id=?", (45, cap["members"][0]["enquiry_id"])); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and st["stale_reason"] == ai_queue.INTRO_STALE_PROFILE and st["offer_current"] is True and st["profile_current"] is False


def test_members_only_form_still_accepted_but_never_current(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    ai_queue.record_intro_draft(db, g["id"], "legacy form", "AI", "tester", members=ai_queue.intro_members(db, g["id"])); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and st["stale_reason"] == ai_queue.INTRO_STALE_NO_CONTEXT and st["status"] == "ok"
    assert st["offer_current"] is False and st["context_hash"] is None and st["apartment_id"] is None


# ---------------------------------------------------------------- draft identity
def test_two_drafts_in_the_same_second_have_distinct_draft_ids(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai_queue, "now_iso", lambda: "2026-11-08T10:00:00")
    ai_queue.record_intro_draft(db, g["id"], "first", "AI", "tester", capture=_cap(db, g["id"]))
    s1 = ai_queue.intro_state(db, g["id"])
    ai_queue.record_intro_draft(db, g["id"], "second", "AI", "tester", capture=_cap(db, g["id"])); db.commit()
    s2 = ai_queue.intro_state(db, g["id"])
    assert s1["drafted_at"] == s2["drafted_at"] == "2026-11-08T10:00:00"
    assert s1["draft_id"] and s2["draft_id"] and s1["draft_id"] != s2["draft_id"]
    assert s2["draft_id"] == row(db.execute("SELECT MAX(id) m FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))["m"]
    assert s1["context_hash"] == s2["context_hash"]   # same context, different rows: the row id is the CAS token


def test_edit_with_stored_context_keeps_each_persons_retention_reference(db, monkeypatch):
    """A staff edit passes the STORED context of the draft row (no member list). The new 'edited' history row must still carry
    refs_json with every person's immutable identity (enquiry_id / party_member_id), exactly as the AI draft row did."""
    a, ids, g = _run_and_approve(db, monkeypatch)
    ai_queue.record_intro_draft(db, g["id"], "ai draft", "AI", "tester", capture=ai_queue.intro_capture(db, g["id"])); db.commit()
    first = row(db.execute("SELECT id, refs_json, body_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (g["id"],)))
    stored = json.loads(first["body_json"])
    edit_ctx = {k: stored.get(k) for k in ("apartment_id", "profile_hash", "offer_fingerprint", "basis_scope", "context_hash")} | {"basis": stored.get("basis_items")}
    # without the named row's identities the helper refuses rather than writing refs_json NULL
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_NO_IDENTITIES):
        ai_queue.record_intro_draft(db, g["id"], "ai draft, edited", "staff", "tester", capture=edit_ctx)
    assert row(db.execute("SELECT MAX(id) m FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))["m"] == first["id"]
    stored_refs = [{"ref": r, **v} for r, v in json.loads(first["refs_json"]).items()]   # what the app passes: the stored row's refs
    ai_queue.record_intro_draft(db, g["id"], "ai draft, edited", "staff", "tester", members=stored_refs, capture=edit_ctx); db.commit()
    second = row(db.execute("SELECT id, status, refs_json, body_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (g["id"],)))
    assert second["id"] != first["id"] and second["status"] == "edited"
    assert second["refs_json"] and json.loads(second["refs_json"]) == json.loads(first["refs_json"])
    assert set(json.loads(second["refs_json"])) == {m["ref"] for m in ai_queue.intro_members(db, g["id"])}
    assert all(v["enquiry_id"] in ids for v in json.loads(second["refs_json"]).values())
    assert ai_queue.intro_state(db, g["id"])["current"]
    # fresh people never legitimise stale text: a profile edit after the draft makes the stored context refuse (nothing written)
    db.execute("UPDATE enquiries SET intro_age=? WHERE id=?", (52, ids[0])); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_PROFILE):
        ai_queue.record_intro_draft(db, g["id"], "late edit", "staff", "tester", members=stored_refs, capture=edit_ctx)
    assert row(db.execute("SELECT MAX(id) m FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))["m"] == second["id"]
    # a described person deleted mid-flight: refused as before
    db.execute("DELETE FROM enquiries WHERE id=?", (ids[1],)); db.commit()
    with pytest.raises(ValueError):
        ai_queue.record_intro_draft(db, g["id"], "late edit", "staff", "tester", members=stored_refs, capture=edit_ctx)


def test_record_is_commit_free_inside_the_guard(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    calls = []
    real = db._raw_commit

    def spy():
        calls.append(1)
        return real()
    monkeypatch.setattr(db, "_raw_commit", spy)
    with ai_queue._step(db, label="test outer"):   # inside a guard c.commit() raises, so any hidden commit would surface here
        ai_queue.record_intro_draft(db, g["id"], "x", "AI", "tester", capture=_cap(db, g["id"]))
        assert calls == [], "no hidden commit inside record_intro_draft"
    assert calls == [1], "the outermost guard commits exactly once"
    assert ai_queue.intro_state(db, g["id"])["current"]


# ---------------------------------------------------------------- basis provenance
def test_brief_apply_stores_immutable_basis_meta(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    b = _group_brief(db, g["id"])
    body = json.loads(b["body_json"])
    assert body["basis_meta_version"] == ai_queue.BASIS_META_VERSION
    note = body["strengths"][0]
    meta = note["meta"]
    assert meta["subjects"] and all(s["packet_ref"].startswith("U") and s["role"] in ("lead", "companion") for s in meta["subjects"])
    assert all(s["enquiry_id"] in ids for s in meta["subjects"])
    assert meta["fields"] and meta["coverage"] and meta["facts_hash"] == ai_queue._canon_hash(meta["facts"])
    for ref, cov in meta["coverage"].items():
        for f, c in cov.items():
            assert c["status"] == "recorded" and c["basis"] == "own"
    assert all(v["value"] is None for v in meta["facts"].values()), "a person's answers are never projected into the brief metadata"
    assert "I1" not in json.dumps(meta), "subjects are stored by original packet ref + ids, never by intro I-ref"
    # the rule-based discussion point is kept in the brief but is not verifiable from the offer: omitted from the basis, not an error
    items = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert items is not None and [x["section"] for x in items] == ["strengths"]
    it = items[0]
    assert it["subjects"][0]["ref"] == "I1" and it["subjects"][0]["enquiry_id"] == meta["subjects"][0]["enquiry_id"]
    assert it["fact_ids"] == note["fact_ids"] and it["facts_hash"] == meta["facts_hash"] and it["coverage"] == meta["coverage"]


def test_basis_note_meta_distinguishes_own_party_reported_and_unresolvable_companions():
    packet = {"members_for_ai": [{"ref": "U1", "diet": "Vegetarian", "move_in": "2027-02-01", "party_members": ["U1-m1"]}, {"ref": "U2", "diet": "No preference"}],
              "apartments": [{"apartment_id": 7, "suburb": "Carlton", "features": [{"key": "balcony", "label": "Balcony", "recorded": "available"}]}]}
    cat = facts.catalogue(packet)
    assert cat["U1.diet"]["scope"] == "personal" and cat["U1-m1.move_in"]["source"] == "U1" and cat["U1-m1.diet"]["status"] == "missing"
    refs = {"U1": {"enquiry_id": 1, "party_member_id": None}, "U1-m1": {"enquiry_id": 1, "party_member_id": 55}, "U2": {"enquiry_id": 2, "party_member_id": None}}
    m = ai_queue.basis_note_meta({"fact_ids": ["U1.diet", "U1-m1.move_in", "U1-m1.diet", "U2.diet"], "note": "n"}, cat, refs)
    assert m["coverage"]["U1"]["diet"] == {"status": "recorded", "basis": "own"}
    assert m["coverage"]["U1-m1"]["move_in"] == {"status": "recorded", "basis": "party_reported"}
    assert m["coverage"]["U1-m1"]["diet"] == {"status": "missing", "basis": "none"}
    assert [(s["packet_ref"], s["enquiry_id"], s["party_member_id"], s["role"]) for s in m["subjects"]] == [("U1", 1, None, "lead"), ("U1-m1", 1, 55, "companion"), ("U2", 2, None, "lead")]
    assert all(v["value"] is None for v in m["facts"].values())
    # a declared party slot with NO real party_members row: unresolvable, never aliased to the lead
    refs_none = {**refs, "U1-m1": {"enquiry_id": 1, "party_member_id": None}}
    assert ai_queue.basis_note_meta({"fact_ids": ["U1-m1.move_in"], "note": "n"}, cat, refs_none) is None
    assert ai_queue.basis_note_meta({"fact_ids": ["U1.diet"], "note": "n"}, cat, refs_none) is not None   # the lead itself resolves
    # unknown ref / unknown fact id: unattributable
    assert ai_queue.basis_note_meta({"fact_ids": ["U3.diet"], "note": "n"}, cat, refs) is None
    assert ai_queue.basis_note_meta({"fact_ids": ["U2.diet"], "note": "n"}, {}, refs) is None
    # apartment-only note: no subjects, cited value kept (listing fact), status recorded
    am = ai_queue.basis_note_meta({"fact_ids": ["A7.suburb", "A7.feature.balcony"], "note": "n"}, cat, refs)
    assert am["subjects"] == [] and am["apartment_ids"] == [7]
    assert am["facts"]["A7.suburb"] == {"status": "recorded", "scope": "apartment", "value": "Carlton"}
    assert am["facts"]["A7.feature.balcony"]["value"] == "available"


def _set_group_notes(db, gid, notes):
    b = _group_brief(db, gid)
    body = json.loads(b["body_json"])
    body["strengths"] = notes
    body["discussion_points"] = []
    body["rendered"] = {"strengths": [n.get("note") for n in notes], "discussion_points": []}
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), b["id"])); db.commit()
    return body


def test_companion_slot_without_row_in_stored_brief_fails_closed(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    b = _group_brief(db, g["id"])
    refs = json.loads(b["refs_json"])
    refs["U1-m1"] = {"enquiry_id": refs["U1"]["enquiry_id"], "party_member_id": None}
    body = json.loads(b["body_json"])
    # a stored note whose subject is that companion must never resolve to the lead
    body["strengths"].append({"fact_ids": ["U1-m1.move_in"], "note": "U1-m1 moves in with the party",
                              "meta": {"subjects": [{"packet_ref": "U1-m1", "enquiry_id": refs["U1"]["enquiry_id"], "party_member_id": None, "role": "companion"}],
                                       "apartment_ids": [], "fields": ["move_in"], "coverage": {"U1-m1": {"move_in": {"status": "recorded", "basis": "party_reported"}}},
                                       "facts": {"U1-m1.move_in": {"status": "recorded", "scope": "party-wide", "value": None}}}})
    body["strengths"][-1]["meta"]["facts_hash"] = ai_queue._canon_hash(body["strengths"][-1]["meta"]["facts"])
    body.setdefault("rendered", {})["strengths"] = [x["note"] for x in body["strengths"]]
    db.execute("UPDATE group_briefs SET body_json=?, refs_json=? WHERE id=?", (json.dumps(body), json.dumps(refs), b["id"])); db.commit()
    items = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert items is not None and all("U1-m1" not in x["text"] for x in items) and len(items) == 1
    # a legacy-shaped subject without role: party_member_id None means lead only when the original ref was not a companion
    body["strengths"][-1]["meta"]["subjects"][0].pop("role")
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), b["id"])); db.commit()
    items = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert len(items) == 1, "an unrolled (eid, None) subject from a companion-looking original ref still cannot alias the lead"
    assert items[0]["subjects"][0]["packet_ref"] == "U1"


def test_basis_items_remap_by_stable_ids_not_names_or_order(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    b = _group_brief(db, g["id"])
    body = json.loads(b["body_json"])
    body["strengths"][0]["note"] = "U1 keeps regular hours"
    body.setdefault("rendered", {})["strengths"] = ["HistoricalWrongName keeps regular hours [U1.routine=old]"]   # never reused
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), b["id"])); db.commit()
    before = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert len(before) == 1 and before[0]["subjects"][0]["name"] in before[0]["text"] and "HistoricalWrongName" not in before[0]["text"] and "U1" not in before[0]["text"]
    eid = before[0]["subjects"][0]["enquiry_id"]
    db.execute("UPDATE enquiries SET first_name='Renamed' WHERE id=?", (eid,)); db.commit()
    after = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert after[0]["subjects"][0] == {**before[0]["subjects"][0], "name": "Renamed"} and "Renamed" in after[0]["text"]
    assert after[0]["fact_ids"] == before[0]["fact_ids"] and after[0]["facts_hash"] == before[0]["facts_hash"]
    # the stored metadata itself did not change (nothing is reconstructed from current answers)
    assert json.loads(_group_brief(db, g["id"])["body_json"])["strengths"][0]["meta"]["subjects"][0]["enquiry_id"] == eid
    # answer edits do not alter stored coverage either; once the group's evidence generation moves the brief is out of evidence -> []
    db.execute("UPDATE enquiries SET diet='Vegan' WHERE id=?", (eid,)); db.commit()
    assert json.loads(_group_brief(db, g["id"])["body_json"])["strengths"][0]["meta"]["coverage"] == before[0]["coverage"]
    assert ai_queue.intro_basis_items(db, g["id"], a["id"])[0]["coverage"] == before[0]["coverage"]
    db.execute("UPDATE groups SET evidence_hash='synthetic-new-generation' WHERE id=?", (g["id"],)); db.commit()
    assert ai_queue.intro_basis_items(db, g["id"], a["id"]) == []


def test_ref_tokens_are_remapped_whole_token_with_overlapping_refs():
    display = {"U1": "Ana", "U1-m1": "Bo", "U10": "Cy"}
    text, ok = ai_queue._remap_ref_tokens("U1-m1, U10 and U1 share dates; U1's note.", display)
    assert ok and text == "Bo, Cy and Ana share dates; Ana's note."
    text, ok = ai_queue._remap_ref_tokens("U2 is not in this brief", display)
    assert not ok and "U2" in text
    assert ai_queue._remap_ref_tokens("no references at all", {}) == ("no references at all", True)
    assert ai_queue._remap_ref_tokens("U1 and U1-m2", {"U1": "Ana"})[1] is False


def test_basis_items_reorder_and_rename_with_overlapping_refs(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    proj = ai_queue.intro_offer_projection(db, g["id"])
    m1, m2 = proj["members"][0], proj["members"][1]
    refs = {"U1": {"enquiry_id": m1["enquiry_id"], "party_member_id": None}, "U10": {"enquiry_id": m2["enquiry_id"], "party_member_id": None}}
    cat = {f"{r}.move_in": {"ref": r, "field": "move_in", "scope": "party-wide", "source": r, "status": "recorded", "value": "2027-02-01"} for r in refs}
    item = {"note": "U10 and U1 have dates to discuss.", "fact_ids": list(cat)}
    item["meta"] = ai_queue.basis_note_meta(item, cat, refs)
    b = _group_brief(db, g["id"])
    body = {"basis_meta_version": ai_queue.BASIS_META_VERSION, "strengths": [item], "what_could_work": [], "discussion_points": [], "tensions": [],
            "rendered": {"strengths": ["HistoricalWrongName has an obsolete note."]}}
    db.execute("UPDATE group_briefs SET body_json=?, refs_json=? WHERE id=?", (json.dumps(body), json.dumps(refs), b["id"])); db.commit()
    import copy
    proj2 = copy.deepcopy(proj)
    proj2["members"].reverse()
    for i, m in enumerate(proj2["members"], 1):
        m["ref"], m["first_name"], m["name"] = f"I{i}", f"Current{i}", f"Current{i}"
    out = ai_queue.intro_basis_items(db, g["id"], a["id"], proj=proj2)
    assert len(out) == 1 and out[0]["text"] == "Current2 and Current1 have dates to discuss." if proj2["members"][1]["enquiry_id"] == m2["enquiry_id"] else True
    by_id = {m["enquiry_id"]: m for m in proj2["members"]}
    for sj in out[0]["subjects"]:
        assert sj["ref"] == by_id[sj["enquiry_id"]]["ref"] and sj["name"] == by_id[sj["enquiry_id"]]["first_name"] and sj["name"] in out[0]["text"]
    assert "HistoricalWrongName" not in out[0]["text"] and "U1" not in out[0]["text"]


def test_record_refuses_when_only_compatibility_evidence_or_membership_key_changed(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    cap = ai_queue.intro_capture(db, g["id"])
    db.execute("UPDATE groups SET evidence_hash='synthetic-new-generation' WHERE id=?", (g["id"],)); db.commit()
    fresh = ai_queue.intro_offer_projection(db, g["id"])
    assert fresh["offer_fingerprint"] == cap["offer_fingerprint"] and fresh["profile_hash"] == cap["profile_hash"]
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_CONTEXT):
        ai_queue.record_intro_draft(db, g["id"], "old capture", "AI", "tester", capture=cap)
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE kind='intro'"))["n"] == 0
    db.execute("UPDATE groups SET evidence_hash=?, member_key='synthetic-other-key' WHERE id=?", (g["evidence_hash"], g["id"])); db.commit()
    with pytest.raises(ValueError, match=ai_queue.INTRO_REFUSAL_CONTEXT):
        ai_queue.record_intro_draft(db, g["id"], "old capture", "AI", "tester", capture=cap)
    # a capture dict without a precomputed context_hash (e.g. a stored draft context) is compared on its constituent fields
    db.execute("UPDATE groups SET member_key=? WHERE id=?", (g["member_key"], g["id"])); db.commit()
    cap2 = {k: v for k, v in cap.items() if k != "context_hash"}
    ai_queue.record_intro_draft(db, g["id"], "ok", "AI", "tester", capture=cap2); db.commit()
    assert ai_queue.intro_state(db, g["id"])["current"]


def test_apartment_only_notes_are_checked_against_the_exact_current_offer(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    db.execute("UPDATE apartments SET feat_balcony=1 WHERE id=?", (a["id"],)); db.commit()   # one recorded feature to cite
    p = ai_queue._payload(db, g)
    cat = facts.catalogue(p)
    A = f"A{a['id']}"
    assert f"{A}.campus_km" not in cat and "campus_km" not in json.dumps(p) and "campus_distance" not in json.dumps(p)   # M4: no distance in the packet
    refs = json.loads(_group_brief(db, g["id"])["refs_json"])
    # the packet only carries features somebody asked for; cite the recorded balcony through the same catalogue
    feat_key = f"{A}.feature.balcony"
    cat[feat_key] = facts.catalogue({"apartments": [{"apartment_id": a["id"], "features": [{"key": "balcony", "label": "Balcony", "recorded": "available"}]}]})[feat_key]
    assert cat[feat_key]["status"] == "recorded"
    notes = [{"fact_ids": [f"{A}.suburb"], "note": "the suburb suits"}, {"fact_ids": [feat_key], "note": "a recorded feature"},
             {"fact_ids": [f"{A}.available_from"], "note": "available from"}, {"fact_ids": [f"R.{A}.budget"], "note": "budget rule"},
             {"fact_ids": [f"{A}.total_weekly"], "note": "the weekly total"}, {"fact_ids": [f"{A}.stay_days"], "note": "stay length"}]
    for n in notes:
        n["meta"] = ai_queue.basis_note_meta(n, cat, refs)
        assert n["meta"] is not None and n["meta"]["subjects"] == [] and n["meta"]["apartment_ids"] == [a["id"]]
    _set_group_notes(db, g["id"], notes)
    db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (g["evidence_hash"], g["id"])); db.commit()
    items = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert [x["text"] for x in items] == ["the suburb suits", "a recorded feature", "the weekly total", "stay length"], "available_from and rule results are not shown by the introduction: omitted"
    assert all(x["subjects"] == [] for x in items)
    # the cited feature state changes -> that note is dropped; the others stay
    col = "feat_" + feat_key.split(".feature.", 1)[1]
    db.execute(f"UPDATE apartments SET {col}=CASE WHEN {col}=1 THEN 0 ELSE 1 END WHERE id=?", (a["id"],)); db.commit()
    db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (g["evidence_hash"], g["id"])); db.commit()   # keep the brief in evidence for this check
    assert [x["text"] for x in ai_queue.intro_basis_items(db, g["id"], a["id"])] == ["the suburb suits", "the weekly total", "stay length"]
    # the suburb changes -> dropped
    db.execute("UPDATE apartments SET suburb='Elsewhere' WHERE id=?", (a["id"],)); db.commit()
    assert [x["text"] for x in ai_queue.intro_basis_items(db, g["id"], a["id"])] == ["the weekly total", "stay length"]
    # a note about another apartment never reaches this apartment's draft
    other = [{"fact_ids": [f"{A}.suburb"], "note": "x", "meta": {**notes[0]["meta"], "apartment_ids": [a["id"] + 100]}}]
    _set_group_notes(db, g["id"], other)
    assert ai_queue.intro_basis_items(db, g["id"], a["id"]) == []
    # tampered / inconsistent metadata anywhere: fail closed for the whole basis
    bad = [{"fact_ids": [f"{A}.suburb"], "note": "x", "meta": {**notes[0]["meta"], "facts_hash": "0" * 64}}]
    _set_group_notes(db, g["id"], bad)
    assert ai_queue.intro_basis_items(db, g["id"], a["id"]) is None


@pytest.mark.parametrize("key", ["balcony", "bbq", "car_parking", "air_conditioning"])
def test_feature_notes_map_through_the_single_feature_catalogue(db, monkeypatch, key):
    from server import apartment_features as feat
    a, ids, g = _run_and_approve(db, monkeypatch)
    assert key in feat.KEYS
    db.execute(f"UPDATE apartments SET feat_{key}=1 WHERE id=?", (a["id"],)); db.commit()
    cat = facts.catalogue({"apartments": [{"apartment_id": a["id"], "features": [{"key": key, "label": feat.LABEL_OF[key], "recorded": "available"}]}]})
    fid = next(k for k in cat if ".feature." in k)
    assert fid == f"A{a['id']}.feature.{facts.feature_fact_key(feat.LABEL_OF[key])}"
    refs = json.loads(_group_brief(db, g["id"])["refs_json"])
    note = {"note": "The listed feature is available.", "fact_ids": [fid]}
    note["meta"] = ai_queue.basis_note_meta(note, cat, refs)
    _set_group_notes(db, g["id"], [note])
    db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (g["evidence_hash"], g["id"])); db.commit()   # keep in evidence: the flag edit above moved the hash
    kept = ai_queue.intro_basis_items(db, g["id"], a["id"])
    assert kept is not None and len(kept) == 1 and kept[0]["subjects"] == []
    db.execute(f"UPDATE apartments SET feat_{key}=0 WHERE id=?", (a["id"],)); db.commit()
    assert ai_queue.intro_basis_items(db, g["id"], a["id"]) == []


def test_legacy_brief_without_meta_gives_no_basis_and_only_a_labelled_template(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    b = _group_brief(db, g["id"])
    body = json.loads(b["body_json"])
    body.pop("basis_meta_version")
    for k in ai.BRIEF_GROUP_KEYS:
        for x in body.get(k) or []:
            x.pop("meta", None)
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), b["id"])); db.commit()
    assert ai_queue.intro_basis_items(db, g["id"], a["id"]) is None
    assert ai_queue.intro_basis(db, g["id"]) == []
    with pytest.raises(ai_queue.IntroNotCapturable) as ex:
        ai_queue.intro_capture(db, g["id"])
    assert ex.value.code == "legacy_basis" and ex.value.code in ai_queue.INTRO_CAPTURE_CODES
    cap = ai_queue.intro_capture(db, g["id"], for_ai=False)
    assert cap["basis"] is None and cap["basis_scope"] == "legacy"
    # an 'AI' draft recorded on a legacy basis is stored as a template and is never the current AI draft
    ai_queue.record_intro_draft(db, g["id"], "would-be AI draft", "AI", "tester", capture=cap); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["status"] == "template" and st["current"] is False and st["stale_reason"] == ai_queue.INTRO_STALE_TEMPLATE and st["basis_scope"] == "legacy"
    ai_queue.record_intro_draft(db, g["id"], ai.FALLBACK_LABEL + "\nplain template", "template", "tester", capture=cap); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["status"] == "template" and st["current"] is False


# ---------------------------------------------------------------- capture codes and state order
def test_capture_codes_are_static_and_closed(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    with pytest.raises(ai_queue.IntroNotCapturable) as ex:
        ai_queue.intro_capture(db, g["id"] + 999)
    assert ex.value.code == "group_gone"
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],)); db.commit()
    with pytest.raises(ai_queue.IntroNotCapturable) as ex:
        ai_queue.intro_capture(db, g["id"])
    assert ex.value.code == "brief_not_ok"
    db.execute("UPDATE groups SET brief_status='ok', is_primary=0 WHERE id=?", (g["id"],)); db.commit()
    with pytest.raises(ai_queue.IntroNotCapturable) as ex:
        ai_queue.intro_capture(db, g["id"])
    assert ex.value.code == "not_circulable" and any("primary" in r for r in ex.value.details)
    db.execute("UPDATE groups SET is_primary=1 WHERE id=?", (g["id"],))
    db.execute("DELETE FROM group_apartments WHERE group_id=?", (g["id"],)); db.commit()
    with pytest.raises(ai_queue.IntroNotCapturable) as ex:
        ai_queue.intro_capture(db, g["id"])
    assert ex.value.code == "no_offer"
    assert set(ai_queue.INTRO_CAPTURE_CODES) >= {"group_gone", "no_offer", "no_members", "brief_not_ok", "not_circulable", "legacy_basis"}
    for code in ai_queue.INTRO_CAPTURE_CODES:
        assert str(ai_queue.IntroNotCapturable(code)) and not any(ch.isdigit() for ch in str(ai_queue.IntroNotCapturable(code)))


def test_intro_state_additive_keys_and_reason_order(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    base = ai_queue.intro_state(db, g["id"])
    assert set(base) >= {"current", "basis", "status", "stale_reason", "drafted_at", "draft_id", "context_hash", "offer_current", "profile_current", "apartment_id", "basis_scope"}
    assert base["stale_reason"] == ai_queue.INTRO_STALE_NONE and base["draft_id"] is None
    ai_queue.record_intro_draft(db, g["id"], "ok draft", "AI", "tester", capture=ai_queue.intro_capture(db, g["id"])); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] and st["context_hash"] == ai_queue.intro_context_hash(ai_queue.intro_offer_projection(db, g["id"])) and st["basis_scope"] == "current"
    # offer change -> offer reason (facts hash unchanged for an address edit)
    db.execute("UPDATE apartments SET address='Unit 9 / 9 Other Street' WHERE id=?", (a["id"],)); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and st["stale_reason"] == ai_queue.INTRO_STALE_OFFER and st["offer_current"] is False and st["profile_current"] is True
    # a new compatibility evidence generation on the group outranks everything: facts reason
    db.execute("UPDATE groups SET evidence_hash='synthetic-new-generation' WHERE id=?", (g["id"],)); db.commit()
    assert ai_queue.intro_state(db, g["id"])["stale_reason"] == ai_queue.INTRO_STALE_FACTS
