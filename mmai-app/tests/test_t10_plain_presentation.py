"""T10 staff presentation (8 Nov 2026): the AI's internal references (U1, U1-m1, I2, U1.routine, A12, R.A12.budget) and the
validator's schema messages never reach staff as prose.

- Names come from the row's OWN immutable ref map (refs_json); a deleted person is "a former member"; a ref the map does not
  know, or a legacy row about an earlier member set, becomes a stable neutral word ("one member", "another member") - today's
  members are never assigned to text that may describe other people.
- Substitution is one pass: inserted names and addresses are not scanned again.
- r5 compared_with counterpart facts are shown as "Compared with <housemate>'s <field>" on both the rendered-string and the
  structured path; notes themselves are put into plain words.
- Failure detail (review set aside, brief unusable, AI error) stays on the database rows (T5 cleanup covers them; nothing is
  logged); staff see a plain label, and
  groups.held_reason is serialised as an actionable label that keeps its category.
The introduction e-mail body is NOT touched by this normaliser (its own guards apply).
"""
import json
import re

from server import staff_view as sv
from server.db import now_iso, row
from tests.test_data_and_api import appdb, client, good_answers, member  # noqa: F401  (fixtures re-used)
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _add_member, _brief, _review, _submit

RAW = re.compile(r"(?<![A-Za-z0-9_-])(?:[UIR]\d+(?:-m\d+)?|R\.A\d+|A\d+\.[a-z_]+)(?![A-Za-z0-9_-])")
NAMES = {"U1": "Oscar", "U2": "Nia", "U1-m1": "Priya"}
APTS = {"A12": "12 Smith St", "A1": "Unit A1 / 5 Ref Road"}


def _visible_strings(d) -> list[str]:
    """Every human-visible string in the staff payload: review text/labels, brief notes and evidence, hold/error labels."""
    out = []
    r, b = d.get("review") or {}, d.get("brief") or {}
    out += [r.get("rationale") or "", r.get("label") or "", r.get("coverage") or "", *(r.get("replace_names") or [])]
    out += [b.get("label") or "", b.get("error") or "", b.get("coverage") or "", *[m.get("coverage") or "" for m in b.get("members") or []]]
    for sec in (b.get("sections") or []) + [s for m in b.get("members") or [] for s in m.get("sections") or []]:
        for it in sec["items"]:
            out += [it["note"], *it["evidence"]]
    out += [m.get("name") or "" for m in b.get("members") or []]
    out += [d.get("ai_rationale") or "", d.get("ai_error") or "", d.get("held_reason") or "", (d.get("hold") or {}).get("label") or ""]
    return out


# ---------------------------------------------------------------- unit: plain_text


def test_plain_text_names_people_facts_and_apartments_from_the_given_map():
    s = sv.plain_text("U1 and U1-m1 rise early; U2.routine differs (U2 is a night owl). R.A12.budget passes and A12 suits. A12.total_weekly is fair.", NAMES, APTS)
    assert s == ("Oscar and Priya rise early; Nia's daily routine differs (Nia is a night owl). budget check at 12 Smith St passes and 12 Smith St suits. "
                 "12 Smith St: whole-apartment weekly rent is fair.")
    assert not RAW.search(s)


def test_plain_text_unknown_refs_become_stable_neutral_words_never_todays_names():
    s = sv.plain_text("U3 is tidy; U3 and U4 both cook; U4 again.", NAMES, APTS)
    assert s == "one member is tidy; one member and another member both cook; another member again."
    e = sv.plain_text("U1 and U2 both tidy; U1.routine fits", sv.EarlierMembers(), APTS)   # legacy row about an earlier member set
    assert e == "one member and another member both tidy; one member's daily routine fits"
    assert "Oscar" not in e and "Nia" not in e


def test_plain_text_is_one_pass_inserted_names_and_addresses_are_not_rescanned():
    # the address itself contains "A1"; a person's recorded name could equally contain a ref-like token
    assert sv.plain_text("A1 suits U1", {"U1": "U2 Nakamura"}, APTS) == "Unit A1 / 5 Ref Road suits U2 Nakamura"
    assert sv.plain_text("", NAMES, APTS) == "" and sv.plain_text(None, NAMES, APTS) == ""
    # ordinary words and sentence boundaries are left alone
    assert sv.plain_text("UNIT 12 is fine. I am sure; U1.", NAMES, APTS) == "UNIT 12 is fine. I am sure; Oscar."


def test_plain_text_deleted_person_is_a_former_member(client):
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    c = appdb(client)
    refs = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": v["id"], "party_member_id": None}})
    assert sv.plain_text("U1 and U2 agree", sv.names_from_refs(c, refs), {}) == "Oscar and Victoria agree"
    c.execute("DELETE FROM enquiries WHERE id=?", (v["id"],)); c.commit()
    assert sv.plain_text("U1 and U2 agree; U2.routine", sv.names_from_refs(c, refs), {}) == "Oscar and a former member agree; a former member's daily routine"


def test_plain_text_register_codes_become_the_requirement_they_stand_for_never_the_code():
    apts = {"A16": "16 Smith St", "A12": "12 Smith St"}
    # the actual 7-member tension note: "under A16" is the total-rent allowance even though the group also has apartment 16
    note = "U1 and U2 both pay the top share; the rent is negotiable under A16 only if U1-m1 accepts a single bed."
    assert sv.plain_text(note, NAMES, apts) == "Oscar and Nia both pay the top share; the rent is negotiable under the total-rent allowance only if Priya accepts a single bed."
    # the same code as apartment evidence keeps the address; quoted as a rule it does not
    assert sv.plain_text("R.A16.budget passes; A16.total_weekly fits; A16 suits U2; see A16(a); budget (A16) ok", NAMES, apts) == \
        "budget check at 16 Smith St passes; 16 Smith St: whole-apartment weekly rent fits; 16 Smith St suits Nia; see the total-rent allowance; budget (the total-rent allowance) ok"
    # bare register codes from other sections, including I16(d) which is a rule not an introduction ref
    s = sv.plain_text("GP11 keeps U1 with U1-m1 (T6 review; I16(d) applies; A9 and A10 pass; GP13 pending).", NAMES, apts)
    assert s == ("the bring-a-mate (linked friends) rules keeps Oscar with Priya (the AI review before circulation review; the couple and group details applies; "
                 "the minimum lease period and the lease cycle and accepted exit windows pass; individual group responses pending).")
    # an unknown register-shaped token is a neutral phrase, not a leaked code; A16 with no such apartment is the rule
    assert sv.plain_text("Fails GP99 and A16.", NAMES, {}) == "Fails the agreed requirements and the total-rent allowance."
    assert not RAW.search(s) and not re.search(r"\b(?:GP|[ABT])\d+\b", s)


def test_plain_review_note_with_u_refs_reads_as_names():
    assert sv.plain_text("U1 and U2 keep similar hours; U2's guests answer (U2.guests) is the only tension.", NAMES, APTS) == \
        "Oscar and Nia keep similar hours; Nia's guests answer (Nia's guests) is the only tension."


# ---------------------------------------------------------------- unit: friendly_item (both paths)


def test_friendly_item_rendered_string_with_compared_with_bracket():
    it = sv.friendly_item("U1 and U2 both rise early  [U1.routine=Early riser]  [compared with U2: U2.routine=Early riser]", NAMES, APTS)
    assert it == {"note": "Oscar and Nia both rise early", "evidence": ["Oscar's daily routine: Early riser", "Compared with Nia's daily routine: Early riser"]}
    gap = sv.friendly_item("[gap - not recorded] U1-m1 diet unknown  [U1-m1.diet: not recorded]", NAMES, APTS)
    assert gap == {"note": "Not recorded: Priya diet unknown", "evidence": ["Priya's diet (not recorded)"]}
    party = sv.friendly_item("Guests fine  [U1-m1.guests=Rarely (reported by U1 for the party)]", NAMES, APTS)
    assert party["evidence"] == ["Priya's guests: Rarely (answered by Oscar for the whole party)"]
    apt = sv.friendly_item("Budget ok  [R.A12.budget=pass; A12.total_weekly=900]", NAMES, APTS)
    assert apt["evidence"] == ["Budget check at 12 Smith St: pass", "12 Smith St: whole-apartment weekly rent: 900"]
    assert not any(RAW.search(x) for x in [it["note"], *it["evidence"], gap["note"], *gap["evidence"], *party["evidence"], *apt["evidence"]])


def test_friendly_item_structured_with_compared_with_and_neutral_names():
    item = {"note": "U1 and U2 both rise early", "fact_ids": ["U1.routine"], "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]}
    assert sv.friendly_item(item, NAMES, APTS) == {"note": "Oscar and Nia both rise early", "evidence": ["Oscar's daily routine", "Compared with Nia's daily routine"]}
    neutral = sv.friendly_item(item, sv.EarlierMembers(), APTS)
    assert neutral == {"note": "one member and another member both rise early", "evidence": ["one member's daily routine", "Compared with another member's daily routine"]}
    unknown = sv.friendly_item({"note": "U7 cooks", "fact_ids": ["U7.diet"], "compared_with": [{"ref": "U1", "fact_ids": ["U1.diet"]}]}, NAMES, APTS)
    assert unknown == {"note": "one member cooks", "evidence": ["one member's diet", "Compared with Oscar's diet"]}
    assert sv.friendly_item({"note": "U1 diet not recorded", "fact_ids": ["U1.diet"], "kind": "gap"}, NAMES, APTS)["note"] == "Not recorded: Oscar diet not recorded"


# ---------------------------------------------------------------- API: the staff payload


def _plant(client):
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    n = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    gid = _plant_group(c, o["id"])
    _add_member(c, gid, n["id"])
    refs = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": n["id"], "party_member_id": None}})
    return o, n, c, gid, refs


def test_group_payload_names_people_through_the_rows_own_map_and_hides_raw_refs(client):
    o, n, c, gid, refs = _plant(client)
    at = now_iso()
    _review(c, gid, "U1 and U2 share an early U1.routine; U2.tidy is a small gap.", rnd=1, h="h1", at=at)
    _brief(c, gid, "group", {"rendered": {"strengths": ["U1 and U2 both rise early  [U1.routine=Early riser; U2.routine=Early riser]"]},
                              "strengths": [{"fact_ids": ["U1.routine", "U2.routine"], "note": "U1 and U2 both rise early"}]}, rnd=1, h="h1", at=at)
    _brief(c, gid, "member", {"ref": "U1", "rendered": {"strengths": ["U1 rises as early as U2  [U1.routine=Early riser]  [compared with U2: U2.routine=Early riser]"]},
                               "strengths": [{"fact_ids": ["U1.routine"], "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}], "note": "U1 rises as early as U2"}]},
           eid=o["id"], ref="U1", rnd=1, h="h1", at=at)
    _brief(c, gid, "member", {"ref": "U2", "strengths": [{"fact_ids": ["U2.routine"], "compared_with": [{"ref": "U1", "fact_ids": ["U1.routine"]}], "note": "U2 matches U1"}]},
           eid=n["id"], ref="U2", rnd=1, h="h1", at=at)
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=?", (refs, gid))
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=?", (refs, gid))
    c.execute("UPDATE groups SET evidence_hash='h1', ai_status='ok', review_verdict='approve', brief_status='ok', ai_rationale=?, ai_rating=8 WHERE id=?",
              ("U1 and U2 share an early U1.routine; U2.tidy is a small gap.", gid))
    c.commit()
    cov = {"total": 2, "line": "Coverage: 1 of 2 people (U1) gave their own lifestyle / compatibility answers; 1 (U2) have no independently recorded lifestyle / compatibility answers in this packet."}
    c.execute("UPDATE review_rounds SET review_json=? WHERE group_id=?", (json.dumps({"findings": [], "coverage": cov}), gid)); c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"]["rationale"] == "Oscar and Nia share an early Oscar's daily routine; Nia's tidiness is a small gap." and d["review"]["names_available"] is True
    assert d["review"]["coverage"] == "Coverage: 1 of 2 people (Oscar) gave their own lifestyle / compatibility answers; 1 (Nia) have no independently recorded lifestyle / compatibility answers in this packet."
    assert d["ai_rationale"] == d["review"]["rationale"]
    sec = d["brief"]["sections"][0]
    assert sec["items"][0] == {"note": "Oscar and Nia both rise early", "evidence": ["Oscar's daily routine: Early riser", "Nia's daily routine: Early riser"]}
    m1, m2 = d["brief"]["members"]
    assert (m1["name"], m2["name"]) == ("Oscar", "Nia")
    assert m1["sections"][0]["items"][0] == {"note": "Oscar rises as early as Nia", "evidence": ["Oscar's daily routine: Early riser", "Compared with Nia's daily routine: Early riser"]}
    assert m2["sections"][0]["items"][0] == {"note": "Nia matches Oscar", "evidence": ["Nia's daily routine", "Compared with Oscar's daily routine"]}
    bad = [s for s in _visible_strings(d) if RAW.search(s)]
    assert bad == [], bad
    # list payload carries the same plain assessment line
    lst = client.get("/api/groups", params={"dataset": "real"}).json()["items"] + client.get("/api/groups", params={"dataset": "test"}).json()["items"]
    item = next(x for x in lst if x["id"] == gid)
    assert item["ai_rationale"] == d["ai_rationale"] and not RAW.search(item["ai_rationale"])


def test_brief_coverage_line_comes_from_the_rows_own_stored_block_never_recomputed(client):
    """r6: the brief's coverage block is stored WITH each brief row (group body and every member body). The staff payload shows
    that stored line through the row's own name map; a row written without the block (pre-r6) shows none - the count is never
    recomputed from whoever is in the group today."""
    o, n, c, gid, refs = _plant(client)
    at = now_iso()
    line = "Coverage: 1 of 3 people (U1) gave their own lifestyle / compatibility answers (at least one recorded; single unanswered questions are listed under missing facts); 2 (U2, U1-m1) have no independently recorded lifestyle / compatibility answers in this packet - companions' dates and party details were given by their enquirer."
    cov = {"total": 3, "refs": ["U1", "U2", "U1-m1"], "with_own_lifestyle_answers": ["U1"], "without_own_lifestyle_answers": ["U2", "U1-m1"], "parties": [], "party_size_counts_the_enquirer": True, "line": line}
    _brief(c, gid, "group", {"coverage": cov, "strengths": [{"fact_ids": ["U1.routine"], "note": "U1 is settled"}]}, rnd=1, h="h1", at=at)
    _brief(c, gid, "member", {"ref": "U1", "coverage": cov, "strengths": [{"fact_ids": ["U1.routine"], "note": "U1 is settled"}]}, eid=o["id"], ref="U1", rnd=1, h="h1", at=at)
    _brief(c, gid, "member", {"ref": "U2", "individually_recorded": False, "strengths": []}, eid=n["id"], ref="U2", rnd=1, h="h1", at=at)   # no block on this row
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=?", (refs, gid))
    c.execute("UPDATE groups SET evidence_hash='h1', ai_status='ok', review_verdict='approve', brief_status='ok' WHERE id=?", (gid,))
    c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    b = d["brief"]
    assert b["coverage"] == ("Coverage: 1 of 3 people (Oscar) gave their own lifestyle / compatibility answers (at least one recorded; single unanswered questions are listed "
                             "under missing facts); 2 (Nia, one member) have no independently recorded lifestyle / compatibility answers in this packet - companions' dates and party details were given by their enquirer.")
    m1, m2 = b["members"]
    assert m1["coverage"] == b["coverage"]                      # the member row's own stored block, same immutable map
    assert m2["coverage"] is None and m2["individually_recorded"] is False   # no block stored on that row: nothing is recomputed
    bad = [s for s in _visible_strings(d) if RAW.search(s)]
    assert bad == [], bad
    # a block whose refs name an EARLIER member set is rendered through that row's map, never today's members
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim2@example.com")
    old = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": v["id"], "party_member_id": None}})
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=? AND kind='group'", (old, gid)); c.commit()
    d2 = client.get(f"/api/groups/{gid}").json()
    assert "(Victoria, one member)" in d2["brief"]["coverage"] and "Nia" not in d2["brief"]["coverage"]
    # legacy group row (no map at all) written against a different evidence hash stays neutral and says so
    c.execute("UPDATE group_briefs SET refs_json=NULL, evidence_hash='old' WHERE group_id=? AND kind='group'", (gid,)); c.commit()
    d3 = client.get(f"/api/groups/{gid}").json()
    assert d3["brief"]["names_available"] is False and "Oscar" not in d3["brief"]["coverage"] and not RAW.search(d3["brief"]["coverage"])


def test_old_round_is_named_by_its_own_map_and_a_legacy_round_stays_neutral(client):
    o, n, c, gid, refs = _plant(client)
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    old = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": v["id"], "party_member_id": None}})
    # the latest review was written when U2 was Victoria (she has since been replaced by Nia): her name, not Nia's
    _review(c, gid, "U1 and U2 keep similar hours.", rnd=1, h="old", at=now_iso())
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=?", (old, gid))
    c.execute("UPDATE groups SET evidence_hash='new', ai_status='ok', ai_rationale='U1 and U2 keep similar hours.', review_verdict='approve', brief_status='ok' WHERE id=?", (gid,))
    c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"]["rationale"] == "Oscar and Victoria keep similar hours." and d["review"]["current"] is False
    assert d["ai_rationale"] == "Oscar and Victoria keep similar hours."   # attributed through the round it was copied from
    # Victoria deleted: her map entry answers "a former member"
    c.execute("DELETE FROM enquiries WHERE id=?", (v["id"],)); c.commit()
    d2 = client.get(f"/api/groups/{gid}").json()
    assert d2["review"]["rationale"] == "Oscar and a former member keep similar hours." and "Nia" not in d2["review"]["rationale"]
    # legacy round (no map) written against a different evidence hash: neutral words, names withheld, never today's members
    c.execute("UPDATE review_rounds SET refs_json=NULL WHERE group_id=?", (gid,)); c.commit()
    d3 = client.get(f"/api/groups/{gid}").json()
    assert d3["review"]["rationale"] == "one member and another member keep similar hours." and d3["review"]["names_available"] is False
    assert d3["ai_rationale"] == "one member and another member keep similar hours."
    # a groups.ai_rationale no round accounts for is neutral as well
    c.execute("UPDATE groups SET ai_rationale='U2 cooks for U1 at A%d.' WHERE id=?" % row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))["apartment_id"], (gid,)); c.commit()
    d4 = client.get(f"/api/groups/{gid}").json()
    assert d4["ai_rationale"] == "one member cooks for another member at Unit 9 / 9 Intro Street." and not RAW.search(d4["ai_rationale"])


def test_failure_detail_stays_in_the_database_staff_see_plain_labels_with_category(client):
    o, n, c, gid, refs = _plant(client)
    at = now_iso()
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, invalid_reason, evidence_hash, created_at, refs_json)
                 VALUES (?,1,NULL,NULL,'',NULL,0,'findings[0]: U2.routine is not recorded in the packet',?, ?, ?)""", (gid, "h1", at, refs))
    _brief(c, gid, "group", None, status="failed", rnd=1, h="h1", at=at)
    c.execute("UPDATE group_briefs SET error='staff brief unusable: U1.strengths[0]: U2.routine is U2''s personal fact, not U1''s', refs_json=? WHERE group_id=?", (refs, gid))
    c.execute("UPDATE groups SET evidence_hash='h1', ai_status='failed', ai_error='AIUnavailable: review missing for index 0 (U1,U2)', ai_attempts=2, brief_status='failed', "
              "held_reason='Staff brief failed: staff brief unusable: U1.strengths[0]: U2.routine is U2''s personal fact' WHERE id=?", (gid,))
    c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"]["label"] == sv.REVIEW_SET_ASIDE and "invalid_reason" not in d["review"]
    assert d["brief"]["error"] == sv.BRIEF_FAILED and d["brief"]["label"] == "Staff brief failed"
    assert d["ai_error"] == "The AI assessment did not complete after 2 attempts; the group is not ready for reservations - retry the assessment or ask a manager"
    assert d["hold"] == {"category": "brief_failed", "label": sv.BRIEF_FAILED} and d["held_reason"] == sv.BRIEF_FAILED
    bad = [s for s in _visible_strings(d) if RAW.search(s) or "unusable" in s or "AIUnavailable" in s]
    assert bad == [], bad
    # the raw technical text is untouched on the rows
    g = row(c.execute("SELECT ai_error, held_reason FROM groups WHERE id=?", (gid,)))
    assert "U2.routine" in g["held_reason"] and g["ai_error"].startswith("AIUnavailable")
    assert "U2.routine" in row(c.execute("SELECT invalid_reason FROM review_rounds WHERE group_id=?", (gid,)))["invalid_reason"]


def test_hold_categories_review_hold_revision_pending_stale_data_and_brief_pending(client):
    o, n, c, gid, refs = _plant(client)
    at = now_iso()
    # revision: names through the round written against the present evidence
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, evidence_hash, created_at, refs_json)
                 VALUES (?,1,'revise',4,'U2.guests clashes with U1.',?,NULL,1,'h1',?,?)""", (gid, json.dumps(["U2"]), at, refs))
    c.execute("UPDATE groups SET evidence_hash='h1', ai_status='revise', held_reason='AI review: revise U2. U2.guests clashes with U1.' WHERE id=?", (gid,)); c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["hold"] == {"category": "revision_pending", "label": "The AI review asked for a revision - replace Nia: Nia's guests clashes with Oscar."}
    assert d["review"]["replace_names"] == ["Nia"] and d["review"]["rationale"] == "Nia's guests clashes with Oscar."
    # the same hold text after the evidence moved on: neutral, never today's names
    c.execute("UPDATE groups SET evidence_hash='h2' WHERE id=?", (gid,)); c.commit()
    d2 = client.get(f"/api/groups/{gid}").json()
    assert d2["hold"]["label"] == "The AI review asked for a revision - replace one member: one member's guests clashes with another member."
    # hold verdict
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at, refs_json)
                 VALUES (?,2,'hold',5,'U1 and U2 differ on guests.',NULL,1,'h2',?,?)""", (gid, at, refs))
    c.execute("UPDATE groups SET ai_status='hold', held_reason='AI review: hold. U1 and U2 differ on guests.' WHERE id=?", (gid,)); c.commit()
    d3 = client.get(f"/api/groups/{gid}").json()
    assert d3["hold"] == {"category": "review_hold", "label": "The AI review held this group for staff: Oscar and Nia differ on guests."}
    # stale-data and superseded holds keep their category; brief pending derives from the statuses
    for raw, cat in (("Facts changed while the brief was written; re-queued", "stale_data"),
                     ("Brief discarded: a person it described was deleted or the membership changed; re-queued", "stale_data"),
                     ("Earlier approval superseded: the review contract changed; awaiting a fresh review and brief", "review_pending"),
                     ("something new from the queue: U1.routine", "held")):
        c.execute("UPDATE groups SET held_reason=? WHERE id=?", (raw, gid)); c.commit()
        h = client.get(f"/api/groups/{gid}").json()["hold"]
        assert h["category"] == cat and not RAW.search(h["label"]), (raw, h)
    c.execute("UPDATE groups SET held_reason=NULL, ai_status='ok', brief_status='pending' WHERE id=?", (gid,)); c.commit()
    assert client.get(f"/api/groups/{gid}").json()["hold"]["category"] == "brief_pending"
    c.execute("UPDATE groups SET brief_status='ok' WHERE id=?", (gid,)); c.commit()
    d5 = client.get(f"/api/groups/{gid}").json()
    assert d5["hold"] is None and d5["held_reason"] is None


def test_intro_email_body_is_not_normalised(client):
    o, n, c, gid, refs = _plant(client)
    c.execute("UPDATE groups SET intro_text='Hello Oscar and Nia - unit U1 of the building is yours' WHERE id=?", (gid,)); c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["text"] == "Hello Oscar and Nia - unit U1 of the building is yours"
