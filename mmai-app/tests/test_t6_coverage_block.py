"""T6 r6: code-derived people / coverage block and the narrow coverage-count guard (8 November 2026).

Background: a real seven-person run (lead with party_size 6 and five companions + one solo; two people with their own
lifestyle answers) produced a valid review whose rationale said "six of seven residents have no recorded lifestyle facts". The
packet carried party_size 6 next to five listed companions and left the counting to the model.

Pinned here (root-approved; structural consistency, not semantic acceptance):
  * review_packet and the brief packet carry "people": facts.coverage(packet) - total, refs, who gave their own lifestyle /
    compatibility answers, who did not (companions), each party with party_size AS STATED next to the companions listed,
    party_size_counts_the_enquirer, and a staff line; the prompt says party_size counts the enquirer and that any "n of m
    people" coverage count must repeat the block;
  * the SAME coverage block is stored with every result (review result dict, brief group body, every member body) so a
    historic row keeps its own counts and no renderer recomputes them from today's members;
  * guard: a sentence of the exact shape "<n> of <m> <people noun>" + negative phrase + generic lifestyle / compatibility /
    personal / answers word must give (without, total); the positive form must give (with, total). Nothing else is judged:
    feature counts, campus distance, single fields (routine), apartments, bedrooms are untouched. Applied to review rationale,
    findings, replace_reasons, apartment reasons, and every brief note (group and member);
  * wording says "no independently recorded lifestyle / compatibility answers" - companions do have introduction profiles
    outside this packet, so "no personal facts" would overstate it.
All model traffic is fabricated; no live call, no real people.
"""
import re
import json

import pytest

from server import ai, facts
from tests.conftest import fake_staff_briefs
from tests.test_t6_bounded_output import REAL_STAFF_BRIEFS
from tests.test_t6_grounding import _packet, _party_group, _run_and_approve

SEVEN = {"members_for_ai": [
    {"ref": "U1", "doing": "Studying", "routine": "Early riser", "tidy": "Very tidy", "social": "Friendly but independent", "guests": "Sometimes", "diet": "None",
     "party": "With friends", "party_size": 6, "move_in": "2027-02-01", "move_out": "2027-07-10", "party_members": ["U1-m1", "U1-m2", "U1-m3", "U1-m4", "U1-m5"]},
    {"ref": "U2", "doing": "Studying", "routine": "Early riser", "tidy": "Very tidy", "social": "Friendly but independent", "guests": "Sometimes", "diet": "None",
     "party": "Alone", "party_size": 1, "move_in": "2027-02-01", "move_out": "2027-07-10", "party_members": []}]}


def test_coverage_block_for_the_real_seven_shape():
    cov = facts.coverage(SEVEN)
    assert cov["total"] == 7 and cov["refs"] == ["U1", "U1-m1", "U1-m2", "U1-m3", "U1-m4", "U1-m5", "U2"]
    assert cov["with_own_lifestyle_answers"] == ["U1", "U2"]
    assert cov["without_own_lifestyle_answers"] == ["U1-m1", "U1-m2", "U1-m3", "U1-m4", "U1-m5"]
    assert cov["party_size_counts_the_enquirer"] is True
    assert cov["parties"] == [{"enquirer": "U1", "companions": ["U1-m1", "U1-m2", "U1-m3", "U1-m4", "U1-m5"],
                               "party_size_as_stated_including_enquirer": 6, "people_in_party_including_enquirer": 6}]
    assert cov["line"] == ("Coverage: 2 of 7 people (U1, U2) gave their own lifestyle / compatibility answers (at least one recorded; single unanswered questions "
                           "are listed under missing facts); 5 (U1-m1, U1-m2, U1-m3, U1-m4, U1-m5) have no "
                           "independently recorded lifestyle / compatibility answers in this packet - companions' dates and party details were given by their enquirer.")
    assert "no personal facts" not in cov["line"] and "nothing is known" not in cov["line"]
    # a stated party_size that disagrees with the companions listed is shown side by side, never silently corrected
    odd = json.loads(json.dumps(SEVEN))
    odd["members_for_ai"][0]["party_size"] = 7
    p = facts.coverage(odd)["parties"][0]
    assert p["party_size_as_stated_including_enquirer"] == 7 and p["people_in_party_including_enquirer"] == 6
    # two solos: no parties, everyone answered for themselves
    cov2 = facts.coverage({"members_for_ai": [{"ref": "U1", "routine": "x", "party_size": 1}, {"ref": "U2", "tidy": "y", "party_size": "1"}]})
    assert cov2["total"] == 2 and cov2["parties"] == [] and cov2["without_own_lifestyle_answers"] == [] and cov2["line"].endswith("(none) have no independently recorded lifestyle / compatibility answers in this packet.")


@pytest.mark.parametrize("sentence, bad", [
    ("However, six of seven residents have no recorded lifestyle facts, and the unit is crowded with bills excluded.", True),   # the real line
    ("Five of seven residents have no recorded lifestyle facts.", False),
    ("Six of seven people have no personal facts recorded.", True),
    ("6 of 7 of them have no compatibility answers", True),
    ("5 out of 7 members lack lifestyle answers", False),
    ("Six of the seven have no independently recorded lifestyle answers.", True),
    ("Five of the seven have no independently recorded lifestyle answers.", False),
    ("Only two of seven people gave their own lifestyle answers.", False),
    ("Three of seven people gave their own lifestyle answers.", True),
    ("Two of seven people have lifestyle facts recorded, which is not ideal.", False),
    # untouched: not a people-coverage claim of that shape
    ("Gym and campus distance are not recorded for two of three apartments.", False),
    ("Two of the seven people asked for a gym that is unrecorded.", False),
    ("Three of seven people are unrecorded on routine.", False),
    ("Two of the seven apartments have no lifestyle data.", False),
    ("Two of 3 bedrooms are singles.", False),
    ("Six of seven residents share one lease.", False),
    ("U1-m1 has no personal study, routine or guest facts recorded.", False),
    # field-specific or bare-'answers' counts are different counts that may be true: untouched
    ("Six of seven people lack university answers.", False),
    ("Six of seven people have no school profiles recorded.", False),
    ("Six of seven residents gave no contact answers.", False),
    # a true compound sentence: each count is judged against its own clause only
    ("Two of seven people have their own lifestyle answers, while five of seven people have no recorded lifestyle answers.", False),
    ("Two of seven people have their own lifestyle answers, while six of seven people have no recorded lifestyle answers.", True),
    ("Five of seven people have no recorded lifestyle answers and two of seven people have no recorded contact answers.", False),
    # a clause with several counts is left unjudged rather than guessed
    ("Six of seven or five of seven people have no lifestyle answers.", False),
])
def test_coverage_guard_is_exactly_the_known_shape(sentence, bad):
    pr = facts.coverage_claim_problems(sentence, facts.coverage(SEVEN), "rationale")
    assert bool(pr) is bad, (sentence, pr)
    if bad:
        assert len(pr) == 1 and pr[0].startswith("rationale: coverage count '") and "disagrees with the packet (" in pr[0] and "of 7 people" in pr[0]
    assert facts.coverage_claim_problems(None, facts.coverage(SEVEN), "x") == [] and facts.coverage_claim_problems("", facts.coverage(SEVEN), "x") == []


def test_contract_bumped_to_r6_and_packets_carry_people_block(db, monkeypatch):
    assert ai.REVIEW_CONTRACT >= "2026-11-08.r6" and re.match(r"^\d{4}-\d{2}-\d{2}\.", ai.REVIEW_CONTRACT)   # r6 or a later contract (r7 packet stage)
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    assert desc["people"] == facts.coverage(p) and desc["people"]["total"] == len(desc["people"]["refs"]) >= 2
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)
    seen: list = []

    def capture(prompt, model=None, max_tokens=1500, system=""):
        seen.append(prompt)
        raise ai.AIUnavailable("stop")
    monkeypatch.setattr(ai, "_ask", capture)
    with pytest.raises(ai.AIUnavailable):
        ai.assess_groups_and_apartments([p])
    with pytest.raises(ai.AIUnavailable):
        ai.staff_briefs({"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}})
    for prompt in seen:
        assert "party_size COUNTS THE ENQUIRER" in prompt and "any other 'n of m people' coverage count is rejected" in prompt
        assert "no independently recorded lifestyle answers here, not that nothing is known about them" in prompt
        assert '"people": {' in prompt and '"party_size_counts_the_enquirer": true' in prompt and '"with_own_lifestyle_answers"' in prompt


def test_review_result_stores_coverage_and_guards_every_text_field(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    cat = facts.catalogue(p)
    cov = facts.coverage(p)
    total, without = cov["total"], len(cov["without_own_lifestyle_answers"])
    wrong = without + 1
    u1 = [k for k, v in cat.items() if v.get("ref") == "U1" and v["status"] == "recorded"][:1]
    u2 = [k for k, v in cat.items() if v.get("ref") == "U2" and v["status"] == "recorded"][:1]
    apts = [{"apartment_id": x["apartment_id"], "reason": "r"} for x in desc["apartments_in_rule_order"]]
    good = {"index": 0, "rating": 7, "rationale": f"{without} of {total} people have no recorded lifestyle answers.", "verdict": "approve", "apartments": apts,
            "findings": [{"fact_ids": u1, "note": "both tidy"}]}
    r = ai.parse_review_batch([good], [p])[0]
    assert r["valid"], r["invalid_reason"]
    assert r["coverage"] == cov and r["coverage"]["line"].startswith("Coverage: ")
    # an invalid result still carries the block (stored with the row either way)
    bad = ai.parse_review_batch([{**good, "rationale": f"{wrong} of {total} residents have no recorded lifestyle facts."}], [p])[0]
    assert not bad["valid"] and bad["coverage"] == cov
    assert bad["invalid_reason"] == f"rationale: coverage count '{wrong} of {total} residents' disagrees with the packet ({without} of {total} people have no independently recorded lifestyle / compatibility answers)"
    # findings, replace_reasons and apartment reasons are guarded too
    bad = ai.parse_review_batch([{**good, "findings": [{"fact_ids": u1, "note": f"{wrong} of {total} people lack lifestyle answers"}]}], [p])[0]
    assert not bad["valid"] and bad["invalid_reason"].startswith("findings[0]: coverage count")
    rev = {**good, "verdict": "revise", "replace_refs": ["U2"], "replace_reasons": [{"ref": "U2", "fact_ids": u2, "note": f"{wrong} of {total} housemates have no compatibility answers"}]}
    bad = ai.parse_review_batch([rev], [p])[0]
    assert not bad["valid"] and bad["invalid_reason"].startswith("replace_reasons[0]: coverage count")
    bad = ai.parse_review_batch([{**good, "apartments": [{**x, "reason": f"{wrong} of {total} people without lifestyle answers"} for x in apts]}], [p])[0]
    assert not bad["valid"] and bad["invalid_reason"].startswith(f"apartments[{apts[0]['apartment_id']}]: coverage count")
    # an unrelated count in the rationale is untouched
    r = ai.parse_review_batch([{**good, "rationale": "Gym and campus distance are unrecorded for two of three apartments."}], [p])[0]
    assert r["valid"], r["invalid_reason"]


def test_brief_results_store_coverage_on_group_and_every_member_and_guard_notes(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    p = _packet(db, g)
    packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}}
    refs = [m["ref"] for m in p["members_for_ai"]] + [r for m in p["members_for_ai"] for r in (m.get("party_members") or [])]
    cov = facts.coverage(packet)
    assert cov["total"] == 4 and len(cov["with_own_lifestyle_answers"]) == 2 and len(cov["without_own_lifestyle_answers"]) == 2
    good = fake_staff_briefs(packet)
    res = ai.parse_staff_briefs(json.loads(json.dumps(good)), refs, packet)
    assert res["group"]["coverage"] == cov and all(m["coverage"] == cov for m in res["members"])
    assert "coverage" not in res["group"]["rendered"]
    # a correct count in a group note passes; a wrong one in a group or member note fails
    cat = facts.catalogue(packet)
    lead_ref = next(m["ref"] for m in p["members_for_ai"] if m.get("party_members"))
    own = next(k for k, v in cat.items() if v.get("ref") == lead_ref and v["status"] == "recorded" and v["scope"] == "personal")
    ok = json.loads(json.dumps(good))
    ok["group"]["discussion_points"].append({"fact_ids": [own], "note": "Two of four people have no independently recorded lifestyle answers yet."})
    assert ai.parse_staff_briefs(ok, refs, packet)["group"]["coverage"] == cov
    bad = json.loads(json.dumps(good))
    bad["group"]["discussion_points"].append({"fact_ids": [own], "note": "Three of four people have no independently recorded lifestyle answers yet."})
    with pytest.raises(ai.AIUnavailable, match=r"group.discussion_points\[\d+\]: coverage count 'Three of four people' disagrees with the packet \(2 of 4 people"):
        ai.parse_staff_briefs(bad, refs, packet)
    bad = json.loads(json.dumps(good))
    next(m for m in bad["members"] if m["ref"] == lead_ref)["discussion_points"] = [{"fact_ids": [own], "note": "Only one of four people gave lifestyle answers."}]
    with pytest.raises(ai.AIUnavailable, match=rf"{lead_ref}.discussion_points\[0\]: coverage count 'one of four people' disagrees with the packet \(2 of 4 people gave"):
        ai.parse_staff_briefs(bad, refs, packet)


def test_historic_coverage_is_the_stored_block_not_a_recount():
    """A stored result keeps the block it was produced with: the staff line for an old row is read from the row, not rebuilt."""
    cov = facts.coverage(SEVEN)
    stored = json.loads(json.dumps({"coverage": cov}))
    later = json.loads(json.dumps(SEVEN))
    later["members_for_ai"][0]["party_members"] = later["members_for_ai"][0]["party_members"][:2]   # three companions left since
    assert facts.coverage(later)["total"] == 4
    assert stored["coverage"]["total"] == 7 and stored["coverage"]["line"].startswith("Coverage: 2 of 7 people")


# ---------------------------------------------------------------- r6 parser hardening (fail closed, not business rules)
def test_finite_number_never_overflows():
    assert ai._finite_number(10 ** 1000) is None and ai._finite_number(-(10 ** 1000)) is None
    assert ai._finite_number(str(10 ** 1000)) is None
    assert ai._finite_number(7) == 7.0 and ai._finite_number("7.5") == 7.5 and ai._finite_number(True) is None and ai._finite_number(float("nan")) is None


@pytest.mark.parametrize("stray", [99, -1, None, True, 0.5, "x", 10 ** 1000, float("inf")])
def test_review_batch_with_an_index_outside_the_batch_is_unusable(db, monkeypatch, stray):
    """A valid object for group 0 does not carry an extraneous object (index 99 / -1 / missing / bool / fractional): the batch
    does not line up with what was asked, so the whole reply is unusable rather than the stray being ignored."""
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    cat = facts.catalogue(p)
    u1 = [k for k, v in cat.items() if v.get("ref") == "U1" and v["status"] == "recorded"][:1]
    apts = [{"apartment_id": x["apartment_id"], "reason": "r"} for x in desc["apartments_in_rule_order"]]
    good = {"index": 0, "rating": 7, "rationale": "fine", "verdict": "approve", "apartments": apts, "findings": [{"fact_ids": u1, "note": "both tidy"}]}
    assert ai.parse_review_batch([good], [p])[0]["valid"]
    extra = {k: v for k, v in good.items() if k != "index"}
    if stray is not None:
        extra["index"] = stray
    with pytest.raises(ai.AIUnavailable, match=r"AI review carried 1 object\(s\) with an index outside this batch of 1 group\(s\)"):
        ai.parse_review_batch([good, extra], [p])
    # the per-group failures are unchanged: a missing group fails that group, a duplicate fails that group
    r = ai.parse_review_batch([{**good, "index": 1}], [p, p])
    assert not r[0]["valid"] and r[0]["invalid_reason"] == "review missing for this group" and r[1]["valid"]
    r = ai.parse_review_batch([good, good], [p])
    assert not r[0]["valid"] and r[0]["invalid_reason"] == "2 review objects for one group"
