"""T6 grounding, evidence and introduction regressions (8 November 2026).

  * review findings / replace reasons / brief items must cite validated fact ids; a missing fact may only be cited by an explicit
    'gap' item (which never counts as evidence); invented ids, another person's personal fact copied onto a party member,
    all-empty briefs, strings instead of objects, and protected-trait reasoning in free notes make the output unusable;
  * the evidence hash moves for real fact changes (answers, members, bar, apartment facts incl. feature flags, rule version) and
    NOT for lifecycle transitions (Proposed -> Introduced), identity-only introduction edits (intro_sex / intro_age) or a stale
    stored legacy score; a leased top apartment falls through the GP8(a) carve-out to the next stored option;
  * the GP2 introduction payload is one entry per real person with only that person's facts; bios are validated per person;
    'Prefer not to say' never licenses pronouns; group-fit prose cannot reason from identity; the template is labelled.

Everything is a fabricated double; no live model call, no real people. The validators are structural wording guards: they
prove what a note cites and who it is about, not that the prose is true - tests pin exactly that scope.
"""
import json

import pytest

from server import ai, ai_queue, facts, matching, rules
from server.db import rows, row
from tests.test_data_and_api import good_answers, member
from tests.test_launch_neutral import _apt, _enq, _pool
from tests.test_t6_fail_closed import _reply_builder, _good
from tests.conftest import fake_staff_briefs


def _run_and_approve(db, monkeypatch, build=_good):
    a = _apt(db)
    ids = _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(build))
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    return a, ids, g


def _packet(db, g):
    return ai_queue._payload(db, g)


# ---------------------------------------------------------------- review evidence
def test_gap_findings_allowed_but_never_evidence(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    cat = facts.catalogue(p)
    missing = [k for k, v in cat.items() if v["status"] == "missing"]
    recorded = [k for k, v in cat.items() if v["status"] == "recorded" and v.get("ref")]
    assert missing and recorded
    base = {"index": 0, "rating": 7, "rationale": "fine", "verdict": "approve", "apartments": [{"apartment_id": x["apartment_id"], "reason": "r"} for x in desc["apartments_in_rule_order"]]}
    # a gap item citing a missing fact is kept, labelled, and an evidence item alongside carries the verdict
    ok = ai.parse_review_batch([{**base, "findings": [{"fact_ids": recorded[:1], "note": "routines line up"},
                                                        {"fact_ids": missing[:1], "note": "not recorded; confirm the journey with the applicant", "kind": "gap"}]}], [p])[0]
    assert ok["valid"], ok["invalid_reason"]
    assert [x["kind"] for x in ok["findings"]] == ["evidence", "gap"]
    assert facts.render(ok["findings"][1], cat).startswith("[gap - not recorded] ")
    # the same missing fact cited as plain evidence ("commute meets requirement") is rejected
    bad = ai.parse_review_batch([{**base, "findings": [{"fact_ids": recorded[:1], "note": "routines line up"},
                                                         {"fact_ids": missing[:1], "note": "commute meets the requirement"}]}], [p])[0]
    assert not bad["valid"] and "is not recorded" in bad["invalid_reason"]
    # a gap item citing only recorded facts is not a gap
    bad2 = ai.parse_review_batch([{**base, "findings": [{"fact_ids": recorded[:1], "note": "x", "kind": "gap"}]}], [p])[0]
    assert not bad2["valid"] and "gap item must cite" in bad2["invalid_reason"]
    # gap items alone cannot support a verdict
    bad3 = ai.parse_review_batch([{**base, "findings": [{"fact_ids": missing[:1], "note": "not recorded", "kind": "gap"}]}], [p])[0]
    assert not bad3["valid"] and "gap items alone" in bad3["invalid_reason"]
    # unknown fact id / string items / missing findings
    for findings, frag in (([{"fact_ids": ["U9.routine"], "note": "x"}], "unknown fact"), (["U1 is tidy"], "not an object"), ([], "findings empty"), (None, "not a list")):
        r = ai.parse_review_batch([{**base, "findings": findings}], [p])[0]
        assert not r["valid"] and frag in r["invalid_reason"], (findings, r["invalid_reason"])


def test_protected_trait_reasoning_in_review_notes_is_rejected(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    cat = facts.catalogue(p)
    u1 = [k for k, v in cat.items() if v.get("ref") == "U1" and v["status"] == "recorded"][:1]
    u2 = [k for k, v in cat.items() if v.get("ref") == "U2" and v["status"] == "recorded"][:1]
    apts = [{"apartment_id": x["apartment_id"], "reason": "r"} for x in desc["apartments_in_rule_order"]]
    good = {"index": 0, "rating": 7, "rationale": "fine", "verdict": "approve", "apartments": apts, "findings": [{"fact_ids": u1, "note": "both tidy"}]}
    assert ai.parse_review_batch([good], [p])[0]["valid"]
    for note in ("they are the same age so it will work", "she is tidy", "both from the same country", "young women tend to get on", "speaks the same language"):
        r = ai.parse_review_batch([{**good, "findings": [{"fact_ids": u1, "note": note}]}], [p])[0]
        assert not r["valid"] and "trait the packet never carried" in r["invalid_reason"], note
    r = ai.parse_review_batch([{**good, "rationale": "A similar age group makes this an easy fit"}], [p])[0]
    assert not r["valid"] and "rationale reasons from a trait" in r["invalid_reason"]
    # the adult declaration IS a recorded rule fact and may be discussed
    r = ai.parse_review_batch([{**good, "findings": [{"fact_ids": u1, "note": "adults confirmed 18 at move-in; both tidy"}]}], [p])[0]
    assert r["valid"], r["invalid_reason"]
    # a revise verdict needs an own-fact reason for the named ref, free of trait reasoning
    rev = {**good, "verdict": "revise", "replace_refs": ["U2"], "replace_reasons": [{"ref": "U2", "fact_ids": u2, "note": "night owl against two early risers"}]}
    assert ai.parse_review_batch([rev], [p])[0]["valid"]
    for rr, frag in (([{"ref": "U2", "fact_ids": u2, "note": "he is older than the others"}], "trait the packet never carried"),
                     ([{"ref": "U2", "fact_ids": u1, "note": "clash"}], "personal fact, not U2"),
                     ([{"ref": "U2", "fact_ids": ["U2.hair"], "note": "clash"}], "unknown fact"),
                     ([], "no evidence-cited reason to replace U2"), (None, "replace_reasons missing")):
        r = ai.parse_review_batch([{**rev, "replace_reasons": rr}], [p])[0]
        assert not r["valid"] and frag in r["invalid_reason"], (rr, r["invalid_reason"])


# ---------------------------------------------------------------- brief grounding (with a real party)
def _party_group(db, monkeypatch):
    """Lead Astrid with two party members (Bo and Cy) + a solo lead Devi on a 3-bedroom: four real people, two refs with facts."""
    a = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0)
    lead = _enq(db, "Astrid", "astrid@example.com", party="With friends", party_size="3", budget_option="3B-single", apt_sizes=["3 bedrooms"], rooms=["Share with one other", "Own room"],
                room_first="Share with one other", mate_max="5", party_members=[member(name="Bo Berg", email="bo@example.com"), member(name="Cy Lund", email="cy@example.com")],
                couples_answer="No", contact_method="WhatsApp", diet="Vegetarian", social="Quiet", routine="Early riser")
    solo = _enq(db, "Devi", "devi@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    assert g is not None and g["member_key"] in (f"{lead['id']},{solo['id']}", f"{solo['id']},{lead['id']}")
    return a, lead, solo, g


def test_brief_grounding_rejects_copied_party_facts_empty_and_malformed(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    p = _packet(db, g)
    idents = ai_queue._ref_identities(db, g)
    packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {},
              "individually_recorded": {r: v["individually_recorded"] for r, v in idents.items()}}
    refs = [m["ref"] for m in p["members_for_ai"]] + [r for m in p["members_for_ai"] for r in (m.get("party_members") or [])]
    assert len(refs) == 4 and any("-m" in r for r in refs)
    good = fake_staff_briefs(packet)
    parsed = ai.parse_staff_briefs(good, refs, packet)
    assert {m["ref"] for m in parsed["members"]} == set(refs)
    cat = facts.catalogue(packet)
    pm_ref = next(r for r in refs if "-m" in r)
    lead_ref = pm_ref.split("-")[0]
    lead_personal = next(k for k, v in cat.items() if v.get("ref") == lead_ref and v["scope"] == "personal" and v["status"] == "recorded")
    party_wide = next(k for k, v in cat.items() if v.get("ref") == pm_ref and v["scope"] == "party-wide")
    # party member described with the lead's personal fact -> unusable; with the party-wide fact (reported by the lead) -> fine
    bad = json.loads(json.dumps(good))
    pm = next(m for m in bad["members"] if m["ref"] == pm_ref)
    pm["strengths"] = [{"fact_ids": [lead_personal], "note": "studies at the same campus"}]
    with pytest.raises(ai.AIUnavailable, match="personal fact, not"):
        ai.parse_staff_briefs(bad, refs, packet)
    ok = json.loads(json.dumps(good))
    next(m for m in ok["members"] if m["ref"] == pm_ref)["strengths"] = [{"fact_ids": [party_wide], "note": "moving in with the party on the stated date"}]
    r = ai.parse_staff_briefs(ok, refs, packet)
    assert "(reported by" in next(m for m in r["members"] if m["ref"] == pm_ref)["rendered"]["strengths"][0]
    # the forced party-member missing-facts note from the queue: personal facts are missing, never inherited
    assert cat[f"{pm_ref}.doing"]["status"] == "missing" and cat[f"{pm_ref}.doing"].get("scope") == "personal"
    # all-empty arrays are not a complete brief
    empty = {"group": {k: [] for k in ai.BRIEF_GROUP_KEYS}, "members": [{"ref": r, **{k: [] for k in ai.BRIEF_MEMBER_KEYS}} for r in refs]}
    with pytest.raises(ai.AIUnavailable, match="no supported strength"):
        ai.parse_staff_briefs(empty, refs, packet)
    # strings instead of {fact_ids, note}
    strs = json.loads(json.dumps(good))
    strs["group"]["strengths"] = ["Everyone is tidy"]
    with pytest.raises(ai.AIUnavailable, match="not an object with a note"):
        ai.parse_staff_briefs(strs, refs, packet)
    # hallucinated fact id
    hal = json.loads(json.dumps(good))
    hal["group"]["strengths"] = [{"fact_ids": ["U1.horoscope"], "note": "x"}]
    with pytest.raises(ai.AIUnavailable, match="unknown fact"):
        ai.parse_staff_briefs(hal, refs, packet)
    # missing member object / duplicate ref
    short = json.loads(json.dumps(good))
    short["members"] = short["members"][:-1]
    with pytest.raises(ai.AIUnavailable):
        ai.parse_staff_briefs(short, refs, packet)
    # protected-trait reasoning in a brief note
    trait = json.loads(json.dumps(good))
    trait["group"]["strengths"] = [{"fact_ids": good["group"]["strengths"][0]["fact_ids"], "note": "all girls of a similar age"}]
    with pytest.raises(ai.AIUnavailable, match="trait the packet never carried"):
        ai.parse_staff_briefs(trait, refs, packet)


def test_policy_violation_guard_scope_is_wording_only():
    assert facts.policy_violations("Both are night owls; adults confirmed 18 at move-in; tidy") == []
    assert facts.policy_violations("same age and she is tidy") == ["age", "she"]
    assert facts.policy_violations("over 18 at move-in confirmed by the adult declaration") == []
    # stated limitation: a bare country name is not a word the guard knows
    assert facts.policy_violations("both from India") == []


# ---------------------------------------------------------------- evidence hash: real changes vs lifecycle / identity-only edits
def test_evidence_hash_moves_for_facts_not_for_lifecycle_or_intro_edits(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    assert ai_queue.is_ready(db, g)["ready"]
    h0, snap0 = matching.group_evidence(db, g["id"])
    blob = json.dumps(snap0)
    assert "intro_sex" not in blob and "intro_age" not in blob and "Astrid" not in blob
    # lifecycle: introduced is not a changed fact
    db.execute("UPDATE groups SET status='Introduced' WHERE id=?", (g["id"],)); db.execute("UPDATE enquiries SET status='Introduced' WHERE id IN (1,2)"); db.commit()
    assert matching.group_evidence(db, g["id"])[0] == h0
    assert ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"]
    db.execute("UPDATE groups SET status='Proposed' WHERE id=?", (g["id"],)); db.execute("UPDATE enquiries SET status='Proposed in group' WHERE id IN (1,2)"); db.commit()
    # identity-only intro edits: hash, adults declaration untouched; the intro draft goes stale
    # r7: a draft is current only with its exact offer context (capture); the pre-r7 members-only form records no context
    ai_queue.record_intro_draft(db, g["id"], "draft legacy", "AI", "tester", members=ai_queue.intro_members(db, g["id"])); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and st["stale_reason"] == ai_queue.INTRO_STALE_NO_CONTEXT and st["draft_id"]
    def _cap():
        proj = ai_queue.intro_offer_projection(db, g["id"])
        return dict(proj, basis=[], basis_scope="current")
    ai_queue.record_intro_draft(db, g["id"], "draft", "AI", "tester", capture=_cap()); db.commit()
    assert ai_queue.intro_state(db, g["id"])["current"] is True
    before = row(db.execute("SELECT adults_confirmed, adults_confirmed_move_in, adults_confirmed_party FROM enquiries WHERE id=1"))
    # (9 Nov 2026, fix 2 currentness: the intro TEXT is a notes source field - the staff edit path puts the notes back to 'pending'
    # and readiness is withdrawn until re-read, so only the identity-only details are edited directly here)
    db.execute("UPDATE enquiries SET intro_sex='Female', intro_age=27 WHERE id=1"); db.commit()
    assert matching.group_evidence(db, g["id"])[0] == h0
    assert row(db.execute("SELECT adults_confirmed, adults_confirmed_move_in, adults_confirmed_party FROM enquiries WHERE id=1")) == before
    touched = ai_queue.intro_profile_changed(db, 1, "tester")
    assert g["id"] in touched   # every live group containing the person (primary and candidates)
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and "edited" in st["stale_reason"]
    assert ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"], "missing/changed intro details never block readiness"
    # staff edit of a fresh AI draft is history with status 'edited' and stays current; a template never is
    ai_queue.record_intro_draft(db, g["id"], "draft 2", "AI", "tester", capture=_cap())
    ai_queue.record_intro_draft(db, g["id"], "draft 2 edited", "staff", "tester", capture=_cap()); db.commit()
    st = ai_queue.intro_state(db, g["id"])
    assert {k: st[k] for k in ("current", "basis", "status", "stale_reason")} == {"current": True, "basis": "staff", "status": "edited", "stale_reason": None}
    assert st["offer_current"] and st["profile_current"] and st["draft_id"] and st["context_hash"] and st["apartment_id"] == g["apartment_id"] and st["basis_scope"] == "current"
    with pytest.raises(ValueError, match="were not identified"):   # r7: every intro row carries the described people's identities
        ai_queue.record_intro_draft(db, g["id"], ai.FALLBACK_LABEL + "\n...", "template", "tester")
    ai_queue.record_intro_draft(db, g["id"], ai.FALLBACK_LABEL + "\n...", "template", "tester", capture=_cap()); db.commit()
    assert ai_queue.intro_state(db, g["id"])["current"] is False
    # real changes DO move the hash: a compatibility answer, a bar, a member, an apartment feature flag, the rule version
    def moved(sql, args=()):
        db.execute(sql, args); db.commit()
        h, _ = matching.group_evidence(db, g["id"])
        return h != h0
    assert moved("UPDATE enquiries SET routine='Night owl' WHERE id=1")
    db.execute("UPDATE enquiries SET routine=? WHERE id=1", (snap0["members"][0]["facts"]["routine"],)); db.commit()
    assert matching.group_evidence(db, g["id"])[0] == h0
    assert moved("INSERT INTO member_rejections(rejected_enquiry_id, other_enquiry_id, group_id, at) VALUES (1, 2, ?, 'x')", (g["id"],))
    db.execute("DELETE FROM member_rejections"); db.commit()
    assert moved("UPDATE apartments SET feat_balcony=CASE WHEN feat_balcony=1 THEN 0 ELSE 1 END WHERE id=?", (a["id"],))
    db.execute("UPDATE apartments SET feat_balcony=CASE WHEN feat_balcony=1 THEN 0 ELSE 1 END WHERE id=?", (a["id"],)); db.commit()
    assert moved("UPDATE enquiries SET features_top=? WHERE id=1", (json.dumps(["Balcony"]),))
    assert moved("DELETE FROM group_members WHERE group_id=? AND enquiry_id=2", (g["id"],))
    monkeypatch.setattr(rules, "RULE_VERSION", rules.RULE_VERSION + "-test")
    assert matching.group_evidence(db, g["id"])[0] != h0
    assert "earlier rule version" in " ".join(ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["reasons"])


def test_leased_top_apartment_falls_through_to_next_stored_option(db, monkeypatch):
    a1 = _apt(db, address="Unit 1 / 1 Neutral Street")
    a2 = _apt(db, address="Unit 2 / 2 Neutral Street", rent12=1160.0, rent6=1235.0)
    _pool(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    res = matching.full_run(db, "tester", dataset="test")
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id LIMIT 1"))
    assert ai_queue.is_ready(db, g)["ready"]
    offered = [r["apartment_id"] for r in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],)))]
    assert set(offered) == {a1["id"], a2["id"]} and g["apartment_id"] == offered[0]
    other_group = row(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],)))
    out = matching.mark_apartment_leased(db, g["apartment_id"], "tester", group_id=other_group["id"] if other_group else None)
    assert any(x["group_id"] == g["id"] and x["outcome"] == "moved" for x in out["affected"])
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g2["apartment_id"] == offered[1] and g2["ai_refresh"] == 1
    chk = ai_queue.is_ready(db, g2)
    assert chk["ready"] and chk["carveout"] is True, chk
    # the historic apartment-specific brief text is kept, labelled with its own evidence hash (different from the current one)
    briefs = rows(db.execute("SELECT kind, status, evidence_hash FROM group_briefs WHERE group_id=? AND kind='group'", (g["id"],)))
    assert briefs and briefs[0]["evidence_hash"] == g2["evidence_hash"] != matching.group_evidence(db, g["id"])[0]
    # a plain apartment drop while still available is NOT a carve-out
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], offered[1])); db.commit()
    assert not ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"]


def test_legacy_stored_score_is_recomputed_neutrally_before_review(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE match_run_id=? AND is_primary=1", (res["run_id"],)))
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], a["id"])))
    alloc = json.loads(ga["allocation_json"])
    alloc["score_parts"] = {"origin_bonus": 0.4, "language": 0.2}   # pre-policy parts
    db.execute("UPDATE group_apartments SET rule_score=99.0, rank=7, allocation_json=? WHERE id=?", (json.dumps(alloc), ga["id"]))
    db.execute("UPDATE groups SET rule_score=99.0 WHERE id=?", (g["id"],)); db.commit()
    p = ai_queue._payload(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    assert p is not None
    apt = next(x for x in p["apartments"] if x["apartment_id"] == a["id"])
    assert apt["rule_score"] != 99.0 and apt["rule_rank"] == 1
    assert "origin" not in json.dumps(p) and "language" not in json.dumps(p)
    assert row(db.execute("SELECT rule_score, rank FROM group_apartments WHERE id=?", (ga["id"],)))["rule_score"] != 99.0


# ---------------------------------------------------------------- GP2 introduction payload and prose
def test_intro_members_is_one_entry_per_real_person_with_own_facts_only(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    people = ai_queue.intro_members(db, g["id"])
    assert len(people) == 4 and [p["ref"] for p in people if p["is_lead"]] == ["I1", "I2"]
    by = {p["ref"]: p for p in people}
    lead_p = next(p for p in people if p["enquiry_id"] == lead["id"] and p["is_lead"])
    pms = [p for p in people if p["enquiry_id"] == lead["id"] and not p["is_lead"]]
    solo_p = next(p for p in people if p["enquiry_id"] == solo["id"])
    assert [p["ref"] for p in pms] == [f"{lead_p['ref']}-m1", f"{lead_p['ref']}-m2"] and [p["name"] for p in pms] == ["Bo Berg", "Cy Lund"]
    assert lead_p["doing"] == "Studying" and lead_p["diet"] == "Vegetarian" and lead_p["routine"] == "Early riser" and lead_p["whatsapp"] is True
    for p in pms:
        assert p["doing"] is None and p["school"] is None and p["diet"] is None and p["intro"] is None and p["phone"] is None and p["whatsapp"] is False
        assert p["email"] in ("bo@example.com", "cy@example.com") and p["party_member_id"] is not None
        assert any("not recorded" in x for x in p["intro_missing"])
    assert solo_p["whatsapp"] is False and solo_p["contact_method"] == "Email" and solo_p["phone"]
    blocks = [ai.intro_profile_block(p) for p in people]
    assert "WhatsApp +61 0400000000" in blocks[people.index(lead_p)] and "mobile +61 0400000000" in blocks[people.index(solo_p)]
    bo = blocks[people.index(pms[0])]
    assert bo.startswith("Bo") and "[party member]" in bo and "email bo@example.com" in bo and "0400000000" not in bo and "RMIT" not in bo and "Vegetarian" not in bo
    assert "diet: Vegetarian; social style: Quiet; sleep pattern: Early riser" in blocks[people.index(lead_p)]
    # no consent: no contact lines for the lead OR the party
    db.execute("UPDATE enquiries SET consent_share=0 WHERE id=?", (lead["id"],)); db.commit()
    people2 = ai_queue.intro_members(db, g["id"])
    assert all(p["email"] is None and p["phone"] is None for p in people2 if p["enquiry_id"] == lead["id"])


def test_intro_prose_validation_is_strict_per_person():
    members = [{"ref": "I1", "first_name": "Astrid", "country_of_birth": "Sweden", "intro_sex": None, "intro_age": None, "is_lead": True, "lead_ref": "I1"},
               {"ref": "I1-m1", "first_name": "Bo", "name": "Bo Berg", "country_of_birth": "Sweden", "intro_sex": "Male", "intro_age": 24, "is_lead": False, "lead_ref": "I1"},
               {"ref": "I2", "first_name": "Devi", "country_of_birth": "India", "intro_sex": None, "intro_age": None, "school": "Monash", "is_lead": True, "lead_ref": "I2"}]
    base = {"opening": "Hello.", "group_fit": "Everyone keeps similar hours and likes a tidy kitchen.", "to_discuss": "How to share cleaning and guests.", "closing": "Say hello.",
            "bios": {"I1": "Astrid loves cooking.", "I1-m1": "Bo is 24 and enjoys football.", "I2": "Devi studies at Monash."}}
    assert ai.validate_intro_prose(base, members) == [] and ai.validate_group_prose(base, members) == []
    # r7: a supplied sex term never licenses pronouns or gendered nouns, even for that person; neutral 'they' and the name are fine
    for bio in ("Bo is 24 and he enjoys football.", "Bo is a man of 24 who enjoys football.", "Bo is 24; his football is a passion."):
        assert any("gendered word or pronoun" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": bio}}, members)), bio
    assert ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": "Bo is 24 and they enjoy football."}}, members) == []
    # a party member's supplied sex/age never licenses the lead's bio
    p = ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1": "Astrid is 24 and she loves cooking."}}, members)
    assert any("gendered word or pronoun" in x for x in p) and any("not this person's supplied age" in x for x in p)
    # no number of ANY length except the supplied age: a birth year (ALM never collects one) or a phone number is rejected too
    for bio in ("Bo was born in Sweden in 2000.", "Bo is 24, born in 2001.", "Bo can be reached on 0400000000.", "Bo is 2 4 years old."):
        assert any("not this person's supplied age" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": bio}}, members)), bio
    assert ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": "Bo is 24 and enjoys football."}}, members) == []
    # digits embedded in a word are not numbers: a name like "Companion1" or a course like "3D design" must not be rejected
    m3 = [dict(m) for m in members]; m3[1]["first_name"] = "Companion1"; m3[1]["name"] = "Companion1 Example"; m3[1]["course"] = "3D design"
    assert ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": "Companion1 is 24 and studies 3D design."}}, m3) == []
    assert any("not this person's supplied age" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1-m1": "Companion1 was born in 2001."}}, m3))
    # 'Prefer not to say' renders as None and never licenses pronouns; 'Another term' free text does not either
    assert any("gendered word or pronoun" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I2": "Devi says she studies at Monash."}}, members))
    m2 = [dict(m) for m in members]; m2[2]["intro_sex"] = "they/them person"
    assert any("gendered word or pronoun" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I2": "Devi says she studies at Monash."}}, m2))
    # another person's fact in a bio; a missing bio for a party member; an unknown ref
    assert any("another person's fact" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I1": "Astrid also studies at Monash."}}, members))
    assert "no bio for I1-m1" in ai.validate_intro_prose({**base, "bios": {k: v for k, v in base["bios"].items() if k != "I1-m1"}}, members)
    assert any("unknown refs" in x for x in ai.validate_intro_prose({**base, "bios": {**base["bios"], "I3": "Someone."}}, members))
    # group prose may not reason from identity or name people in to_discuss
    assert any("excluded trait" in x for x in ai.validate_group_prose({**base, "group_fit": "They are the same age and all from Sweden."}, members))
    assert any("identity detail" in x for x in ai.validate_group_prose({**base, "group_fit": "Two of you are from Sweden."}, members))
    assert any("singles out" in x for x in ai.validate_intro_prose({**base, "to_discuss": "Bo should be quieter."}, members))
    for ph in ("perfect match", "guaranteed"):
        assert any("certainty" in x for x in ai.validate_intro_prose({**base, "closing": f"A {ph}!"}, members))


def test_draft_introduction_needs_basis_and_assembles_per_person_blocks(monkeypatch):
    apt = {"address": "Unit 1 / 1 Test Street", "type_label": "3 bedroom / 2 bathroom"}
    grp = {"lease_start": "2027-02-01", "lease_end": "2028-02-01", "total_weekly": 1500.0, "liability_note": rules.liability_note(1500.0)}
    members = [{"ref": "I1", "first_name": "Astrid", "country_of_birth": "Sweden", "email": "astrid@example.com", "phone": "+61 0400000000", "contact_method": "WhatsApp", "whatsapp": True,
                "weekly_share": 500.0, "is_lead": True, "lead_ref": "I1", "diet": "Vegetarian"},
               {"ref": "I1-m1", "first_name": "Bo", "name": "Bo Berg", "email": "bo@example.com", "weekly_share": 500.0, "is_lead": False, "lead_ref": "I1"},
               {"ref": "I2", "first_name": "Devi", "country_of_birth": "India", "email": "devi@example.com", "phone": "+61 0411111111", "contact_method": "Email", "weekly_share": 500.0, "is_lead": True, "lead_ref": "I2"}]
    with pytest.raises(ai.AIUnavailable, match="no approved staff brief"):
        ai.draft_introduction(apt, grp, members, basis=[])
    seen = {}

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        seen["prompt"] = prompt
        return json.dumps({"opening": "Welcome.", "bios": {"I1": "Astrid cooks vegetarian food and loves a calm home.", "I1-m1": "Bo is coming along with Astrid.", "I2": "Devi is new to Melbourne."},
                           "group_fit": "The dates everyone gave line up for a February start.", "to_discuss": "Agree how guests and cleaning will work.", "closing": "Please say hello."})
    monkeypatch.setattr(ai, "_ask", _ask)
    text = ai.draft_introduction(apt, grp, members, basis=["strengths: dates line up for February"])   # legacy string basis still accepted
    packet = json.loads(seen["prompt"][seen["prompt"].rindex("\n\n") + 2:])
    assert [p["ref"] for p in packet["people"]] == ["I1", "I1-m1", "I2"] and packet["people"][1]["party_of"] == "I1"
    assert "astrid@example.com" not in seen["prompt"] and "0400000000" not in seen["prompt"], "contact values never go to the model"
    assert "WhatsApp +61 0400000000" in text and "mobile +61 0411111111" in text and "Bo Berg  [party member]" in text and "email bo@example.com" in text
    assert text.count("Guide share: about $500/week") == 3 and not text.startswith(ai.FALLBACK_LABEL)
    # identity reasoning in the group prose -> unusable -> the caller falls back to the labelled template
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: json.dumps({"opening": "Welcome.", "bios": {"I1": "Astrid cooks.", "I1-m1": "Bo comes along.", "I2": "Devi is new."},
                                                                 "group_fit": "You are all a similar age and two of you are from Sweden.", "to_discuss": "Guests.", "closing": "Hi."}))
    with pytest.raises(ai.AIUnavailable, match="excluded trait|identity detail"):
        ai.draft_introduction(apt, grp, members, basis=["x"])
    fb = ai.fallback_introduction(apt, grp, members)
    assert fb.startswith(ai.FALLBACK_LABEL) and "Bo Berg  [party member]" in fb


# ---------------------------------------------------------------- review contract version
def test_strengthened_review_contract_invalidates_old_approvals_not_intro_edits(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    assert ai_queue.is_ready(db, g)["ready"] and json.loads(g["evidence_json"])["review_contract"] == ai.REVIEW_CONTRACT
    # identical people and apartments, a newer review contract: not ready, re-queued by the start-up sweep, history kept
    monkeypatch.setattr(ai, "REVIEW_CONTRACT", ai.REVIEW_CONTRACT + ".next")
    chk = ai_queue.is_ready(db, g)
    assert not chk["ready"] and any("earlier review contract" in r for r in chk["reasons"])
    assert matching.group_evidence(db, g["id"])[0] != g["evidence_hash"]
    n_rounds = len(rows(db.execute("SELECT id FROM review_rounds WHERE group_id=?", (g["id"],))))
    assert ai_queue.requeue_interrupted(db) >= 1
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g2["ai_status"] == "pending" and g2["brief_status"] == "stale" and g2["ready_at"] is None and "review contract" in g2["held_reason"]
    assert len(rows(db.execute("SELECT id FROM review_rounds WHERE group_id=?", (g["id"],)))) == n_rounds
    assert len(rows(db.execute("SELECT id FROM group_briefs WHERE group_id=? AND status='ok'", (g["id"],)))) >= 1
    # the GP8 carve-out never bridges a contract change
    db.execute("UPDATE groups SET ai_status='ok', ai_refresh=1 WHERE id=?", (g["id"],)); db.commit()
    assert not ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"]
    monkeypatch.undo()   # also undoes the autouse brief double: re-patch everything this test needs
    monkeypatch.setattr(ai, "staff_briefs", fake_staff_briefs)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    # a fresh review under the current contract makes it ready again; an intro-only edit afterwards does not touch it
    db.execute("UPDATE groups SET ai_status='pending', ai_refresh=0, brief_status='stale' WHERE id=?", (g["id"],)); db.commit()
    ai_queue.process_pending(db)
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert ai_queue.is_ready(db, g3)["ready"], (ai_queue.is_ready(db, g3), g3["ai_status"], g3["ai_error"], g3["brief_status"], g3["held_reason"])
    db.execute("UPDATE enquiries SET intro_sex='Male', intro_age=31 WHERE id=2"); db.commit()
    assert ai_queue.is_ready(db, g3)["ready"] and matching.group_evidence(db, g["id"])[0] == g3["evidence_hash"]


def test_bio_may_say_born_in_but_not_nationality_or_demonym():
    """GP2 provenance (root, 8 Nov 2026): question 1.8 is 'Country of birth'. The profile block says 'born in X'; a bio may not
    restate it as a nationality or demonym, and 'Vegetarian'/'Christian' (a name) style words are not false positives."""
    members = [{"ref": "I1", "first_name": "Astrid", "country_of_birth": "Sweden", "diet": "Vegetarian", "is_lead": True, "lead_ref": "I1"},
               {"ref": "I2", "first_name": "Christian", "country_of_birth": "India", "school": "Australian Catholic University", "is_lead": True, "lead_ref": "I2"}]
    assert "born in Sweden" in ai.intro_profile_block(members[0]) and "from Sweden" not in ai.intro_profile_block(members[0])
    base = {"opening": "Hello all,", "group_fit": "Routines line up.", "to_discuss": "Guests.", "closing": "Warmly, ALM"}
    ok = {**base, "bios": {"I1": "Astrid was born in Sweden and cooks Vegetarian food.", "I2": "Christian studies at Australian Catholic University."}}
    assert ai.validate_intro_prose(ok, members) == []
    for bad, frag in ((" Astrid is Swedish and loves cooking.", "demonym"), ("Astrid, an Indian citizen, loves cooking.", "citizen"),
                      ("Astrid's nationality is Swedish.", "nationality")):
        probs = ai.validate_intro_prose({**ok, "bios": {**ok["bios"], "I1": bad}}, members)
        assert probs and any(frag in p or "nationality" in p for p in probs), (bad, probs)


def test_intro_draft_written_from_changed_intro_facts_is_refused(db, monkeypatch):
    """Root's intro race (8 Nov 2026): a person's intro sex/age/contact changes while the AI drafts; the draft completes and must
    NOT become the current introduction even though the compatibility hash is unchanged (intro fields are not evidence)."""
    from tests.test_t6_late_result import _primary
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert ai_queue.is_ready(db, g)["ready"]
    before = ai_queue.intro_members(db, g["id"])
    ev_before = g["evidence_hash"]
    # the "AI call" is in flight: the person edits their intro age (identity-only: no re-review, no re-rank)
    db.execute("UPDATE enquiries SET intro_age=? WHERE id=?", (31, members[0])); db.commit()
    ai_queue.intro_profile_changed(db, members[0], "tester")
    assert row(db.execute("SELECT evidence_hash FROM groups WHERE id=?", (g["id"],)))["evidence_hash"] == ev_before
    with pytest.raises(ValueError):
        ai_queue.record_intro_draft(db, g["id"], "draft from the old age", "AI", "tester", members=before)
    st = ai_queue.intro_state(db, g["id"])
    assert st["current"] is False and st["status"] == "stale"
    # a draft from the fresh facts is accepted and current; a later identity edit makes it stale again
    cap = dict(ai_queue.intro_offer_projection(db, g["id"]), basis=[], basis_scope="current")   # r7: exact offer context captured with the people
    ai_queue.record_intro_draft(db, g["id"], "draft from the new age", "AI", "tester", capture=cap)
    assert ai_queue.intro_state(db, g["id"])["current"] is True
    db.execute("UPDATE enquiries SET intro_sex=? WHERE id=?", ("Female", members[1])); db.commit()
    assert ai_queue.intro_state(db, g["id"])["current"] is False   # even without the stale marker: the stored profile snapshot no longer matches
