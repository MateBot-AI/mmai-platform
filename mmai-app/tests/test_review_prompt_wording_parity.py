"""11 Nov 2026 (prompt / validator parity). facts.wording_problems judges three wording families on every validated note: (1) a
proposed share called agreed, (2) an accepted preference called a first choice, (3) a timetable / class-schedule alignment nothing
records. The review prompt stated (1) and (2) but not (3), which only the staff-brief prompt carried, so a review finding could be
rejected on a rule its prompt never gave. This file holds the three families in BOTH product-built prompts (and in the review repair
turn, which resends the first prompt), so a guidance sentence missing from either prompt fails here rather than in a live reply.
It checks that the sentences are present; it cannot show that a model will follow them.

Prompt guidance only. Nothing here changes or relaxes the validator: every rejection below is the validator's existing behaviour.
No model call: the prompt is captured at ai._ask_bounded, which is replaced before anything is sent."""
import importlib
import inspect
import json
import re

import pytest

from server import ai, facts

REAL_STAFF_BRIEFS = importlib.import_module("server.ai").__dict__["staff_briefs"]   # bound at import, before the conftest double replaces it

REPAIR_LINE = "Every rule in this request still applies to the whole corrected review, not only to the listed problems."
STUDY = ["U1.course", "U2.course", "U1.school", "U2.school"]


# ---------------------------------------------------------------- helpers
def _packet():
    return {"members_for_ai": [{"ref": "U1", "routine": "Early riser"}, {"ref": "U2", "routine": "Night owl"}], "person_refs": {}, "apartments": [],
            "review": {}, "individually_recorded": {"U1": True, "U2": True}}


def _build(monkeypatch, call):
    """The product's own prompt builder for one call, captured where the bounded model call would start (nothing is sent)."""
    seen = {}

    def ask_bounded(build, *a, **k):
        seen["build"] = build
        raise ai.AIUnavailable("stop", code="test")
    monkeypatch.setattr(ai, "_ask_bounded", ask_bounded)
    with pytest.raises(ai.AIUnavailable):
        call()
    assert "build" in seen, "the call failed before its prompt was built"
    return seen["build"]


def _review_build(monkeypatch):
    return _build(monkeypatch, lambda: ai.assess_groups_and_apartments([_packet()]))


def _brief_build(monkeypatch):
    return _build(monkeypatch, lambda: REAL_STAFF_BRIEFS(_packet()))


def _instructions(prompt):
    """Everything before the packet descriptor, which stays the last blank-line-separated block."""
    return prompt[:prompt.rindex("\n\n")]


def _study_cat():
    cat = {}
    for ref in ("U1", "U2"):
        for field, val in (("course", "Engineering"), ("school", "RMIT"), ("level", "Undergraduate"), ("doing", "Studying"), ("routine", "Early riser")):
            cat[f"{ref}.{field}"] = {"status": "recorded", "ref": ref, "field": field, "scope": "answer", "value": val}
    return cat


def _preference_cat():
    return {"R.A1.budget.share.U1-m1": {"status": "recorded", "ref": "U1-m1", "scope": "rule", "value": "266.67"},
            "U1.rooms_accepted": {"status": "recorded", "ref": "U1", "field": "rooms_accepted", "scope": "answer", "value": "Own room; Share with one other"},
            "U1.room_first": {"status": "recorded", "ref": "U1", "field": "room_first", "scope": "answer", "value": "Share with one other"}}


# One row per wording family the validator judges: a note it rejects, words of the validator's own diagnosis, and the guidance
# sentence(s) both prompts must carry for it.
FAMILIES = [
    ("share_agreed", _preference_cat, ["R.A1.budget.share.U1-m1"], "Bo has agreed to the share.",
     "proposals for the group's agreement",
     ["a proposal for the group's agreement - never call it agreed or accepted"]),
    ("accepted_not_first_choice", _preference_cat, ["U1.rooms_accepted"], "Astrid prefers an own room.",
     "what the party said it would accept",
     ["treat them with discretion as acceptance, never as the first choice"]),
    ("timetable", _study_cat, STUDY, "U1 and U2 are both studying Engineering at the same RMIT campus, so their study timetables are likely to line up.",
     "labels alone do not establish timetable alignment",
     ["labels alone do not establish timetable alignment", "never infer that timetables, class times or attendance line up",
      "quote supplied timing only as recorded, for the person who supplied it, without generalising to others"]),
]
FAMILY_IDS = [f[0] for f in FAMILIES]


# ---------------------------------------------------------------- the validator's families (existing behaviour, unchanged)
@pytest.mark.parametrize("name,cat,fids,note,diagnosis,guidance", FAMILIES, ids=FAMILY_IDS)
def test_each_family_is_one_the_validator_really_rejects(name, cat, fids, note, diagnosis, guidance):
    prob = facts.wording_problems(note, fids, cat(), ref=None)
    assert prob and diagnosis in prob, prob


def test_the_family_list_here_is_the_validators_whole_list():
    """A fourth wording family added to the validator must come with its guidance sentence in both prompts and a row above."""
    returns = re.findall(r"^\s+return f\"", inspect.getsource(facts.wording_problems), re.M)
    assert len(returns) == len(FAMILIES), "facts.wording_problems gained or lost a wording family: update FAMILIES and both prompts together"


# ---------------------------------------------------------------- the same guidance in both prompts and in the repair turn
@pytest.mark.parametrize("name,cat,fids,note,diagnosis,guidance", FAMILIES, ids=FAMILY_IDS)
def test_review_prompt_carries_the_guidance_for_every_family(monkeypatch, name, cat, fids, note, diagnosis, guidance):
    head = _instructions(_review_build(monkeypatch)(False))
    for sentence in guidance:
        assert sentence in head, f"review prompt lacks the {name} guidance: {sentence!r}"


@pytest.mark.parametrize("name,cat,fids,note,diagnosis,guidance", FAMILIES, ids=FAMILY_IDS)
def test_brief_prompt_carries_the_guidance_for_every_family(monkeypatch, name, cat, fids, note, diagnosis, guidance):
    head = _instructions(_brief_build(monkeypatch)(False))
    for sentence in guidance:
        assert sentence in head, f"staff-brief prompt lacks the {name} guidance: {sentence!r}"


@pytest.mark.parametrize("name,cat,fids,note,diagnosis,guidance", FAMILIES, ids=FAMILY_IDS)
def test_review_repair_turn_still_carries_the_guidance_for_every_family(monkeypatch, name, cat, fids, note, diagnosis, guidance):
    build = _review_build(monkeypatch)
    head = _instructions(build(False, feedback=ai.review_repair_feedback([{"invalid_reason": "findings[0]: example problem"}])))
    for sentence in guidance:
        assert sentence in head, f"review repair prompt lacks the {name} guidance: {sentence!r}"


def test_timetable_sentence_is_one_constant_used_once_by_each_prompt(monkeypatch):
    review = _instructions(_review_build(monkeypatch)(False))
    brief = _instructions(_brief_build(monkeypatch)(False))
    assert review.count(ai.TIMETABLE_GUIDANCE) == 1 and brief.count(ai.TIMETABLE_GUIDANCE) == 1
    assert ai.TIMETABLE_GUIDANCE not in ai.REVIEW_RULES_TEXT, "kept out of the shared rules text so the brief does not state it twice"
    # each prompt closes the sentence with its own place for an honest 'compare timetables' note
    assert "Timetables: " + ai.TIMETABLE_GUIDANCE + ai.REVIEW_TIMETABLE_PLACE + ai.REVIEW_GAP_GUIDANCE + "For EACH group do four things.\n" in review
    assert ai.TIMETABLE_GUIDANCE + ai.BRIEF_TIMETABLE_PLACE + "Return ONLY JSON matching this schema. " in brief
    assert ai.BRIEF_TIMETABLE_PLACE not in review and ai.REVIEW_TIMETABLE_PLACE not in brief
    assert "discussion_points" not in review, "the review has no discussion_points list to send a note to"
    # the staff-brief sentence reads exactly as it did before the constant was introduced
    assert ("do not convert, round, add up or re-interpret them. Course, school, level and campus labels alone do not establish timetable alignment: "
            "a shared course or campus is a recorded strength, but never infer that timetables, class times or attendance line up from them; quote "
            "supplied timing only as recorded, for the person who supplied it, without generalising to others (put 'compare timetables' under "
            "discussion_points if useful).\nReturn ONLY JSON matching this schema. ") in brief


# ---------------------------------------------------------------- what the review guidance tells the model matches the validator
def test_review_place_wording_names_what_the_validator_judges():
    place = ai.REVIEW_TIMETABLE_PLACE
    assert "need not mention timetables at all" in place
    assert "not recorded or are worth comparing" in place
    assert "kind 'gap' only when the item cites an id from missing_fact_ids" in place
    assert "even hedged as 'likely' or 'similar'" in place and "timetables or study schedules line up" in place
    assert place.endswith(").\n") and "\n\n" not in place and "\n\n" not in ai.TIMETABLE_GUIDANCE   # the packet descriptor stays the last block


def test_hedged_schedule_claim_on_study_facts_is_what_the_guidance_warns_against():
    """The class of note the review prompt did not warn against: a hedged claim about 'schedules' resting on study facts alone."""
    prob = facts.wording_problems("Both study at RMIT, so their schedules are likely similar.", STUDY, _study_cat(), ref=None)
    assert prob and "labels alone do not establish timetable alignment" in prob, prob


@pytest.mark.parametrize("note", [
    "Neither has reported class times.",
    "Worth comparing timetables once introduced, since both study at RMIT.",
])
def test_the_honest_forms_the_guidance_offers_pass_as_review_findings(note):
    clean, problems = facts.check_items([{"fact_ids": STUDY, "note": note, "kind": "evidence"}], _study_cat(), ref=None, allow_missing=False,
                                        require_own=False, label="findings")
    assert problems == [] and len(clean) == 1


def test_a_gap_finding_needs_a_missing_id_as_the_guidance_says():
    cat = _study_cat()
    note = "Worth comparing timetables once introduced, since both study at RMIT."
    clean, problems = facts.check_items([{"fact_ids": STUDY, "note": note, "kind": "gap"}], cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert clean == [] and "a gap item must cite at least one fact that is not recorded" in " ".join(problems)
    cat["U2.course"] = {"status": "missing", "ref": "U2", "field": "course", "scope": "answer", "value": None}
    clean, problems = facts.check_items([{"fact_ids": STUDY, "note": note, "kind": "gap"}], cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert problems == [] and clean[0]["kind"] == "gap"


# ---------------------------------------------------------------- the repair head line and the contract
def test_review_repair_head_says_every_rule_still_applies_in_fixed_words(monkeypatch):
    build = _review_build(monkeypatch)
    first = build(False)
    feedback = ai.review_repair_feedback([{"invalid_reason": "findings[0]: example problem"}])
    repair = build(False, feedback=feedback)
    assert ai.REVIEW_REPAIR_HEAD.startswith("REPAIR TURN (one only). ") and REPAIR_LINE in ai.REVIEW_REPAIR_HEAD
    assert ai.REVIEW_REPAIR_HEAD.endswith("Exact problems, verbatim, by group index in this request:")
    assert feedback.startswith(ai.REVIEW_REPAIR_HEAD + "\nGroup index 0: findings[0]: example problem\n")
    assert repair.count(REPAIR_LINE) == 1 and REPAIR_LINE not in first and "REPAIR TURN" not in first
    assert repair.index(REPAIR_LINE) > repair.index(ai.TIMETABLE_GUIDANCE) > repair.index(ai.REVIEW_RULES_TEXT)
    assert "This is the only correction turn." in repair
    # the descriptor is still the last block of both turns and is the same packet
    assert json.loads(repair[repair.rindex("\n\n") + 2:]) == json.loads(first[first.rindex("\n\n") + 2:])
    # fixed words: only the validator's own problem lines vary between two repair prompts
    a, b = (ai.review_repair_feedback([{"invalid_reason": r}]) for r in ("problem A", "problem B"))
    assert a.replace("Group index 0: problem A", "") == b.replace("Group index 0: problem B", "")


def test_contract_moved_with_the_prompt_wording():
    assert ai.REVIEW_CONTRACT == "2026-11-11.step-free-payload"
    assert ai.REVIEW_CONTRACT > "2026-11-10.c4-native-groups-wrapper" and re.match(r"^\d{4}-\d{2}-\d{2}\.", ai.REVIEW_CONTRACT)


@pytest.mark.parametrize("compact", [False, True])
def test_gap_guidance_is_actionable_in_normal_compact_and_repair(monkeypatch, compact):
    build = _review_build(monkeypatch)
    first = build(compact)
    feedback = ai.review_repair_feedback([{"invalid_reason": "findings[0]: a gap item must cite at least one fact that is not recorded"}])
    repair = build(compact, feedback=feedback)
    assert first.count(ai.REVIEW_GAP_GUIDANCE) == 1
    assert ai.REVIEW_GAP_GUIDANCE in feedback and repair.count(ai.REVIEW_GAP_GUIDANCE) == 2
    assert "RELEVANT id from this group's missing_fact_ids" in first
    assert "Never add an unrelated missing id just to pass validation" in first
    assert "grounded solely in recorded facts uses kind 'evidence', not 'gap'" in first
    assert "'self-report (free text), unverified'" in first
    assert "feature fact at level 'unverified', which is missing and belongs in missing_fact_ids" in first
    assert "A<id>.step_free_access with a recorded true or false value" in first
    assert "'note' item" not in ai.REVIEW_RULES_TEXT
    assert "kind 'evidence' item" in ai.REVIEW_RULES_TEXT
    # No new packet facts are fabricated by the added guidance.
    assert json.loads(first[first.rindex("\n\n") + 2:]) == json.loads(repair[repair.rindex("\n\n") + 2:])
