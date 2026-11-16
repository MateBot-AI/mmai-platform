"""r7-Q: subject scoping of the introduction habit guard, the sex/age-free model payload, and the brief prompt hints.

Background (live synthetic Sonnet replay, 8 Nov 2026, seven-person group): the model wrote "ExampleOne and ExampleTwo gave matching
answers about routine, tidiness, social style and guests" - a NAMED two-person comparison that the basis covered for exactly those
two - and the guard refused it as a whole-household claim because "matching" counted as a collective marker. Bios also said
"listed as male" because the prompt both permitted and forbade sex words. These tests pin the engineering fix: scope a clause to the
people it is actually about (everyone / the named people), require own recorded coverage for every person in that scope, and keep
sex/age out of the model's payload altogether. Coverage semantics are NOT relaxed: nothing here lets a claim about everyone rest on
a subset, or a named claim rest on a lead-reported or missing answer. Only the model transport is doubled; no provider is called.
"""
import json

import pytest

from server import ai

from tests.test_r7_intro_wording import APT, OFFER, _basis, _draft, _people, _prose

REAL_STAFF_BRIEFS = ai.staff_briefs   # bound at import, before the autouse conftest double replaces it


def _subset_basis(fields, refs=("I1", "I2")):
    """a basis note about `refs` only, with own recorded coverage of `fields` for each of them"""
    items = _basis(tuple(fields))
    for item in items:
        keep = {sj["ref"] for sj in item["subjects"] if sj["ref"] in refs}
        item["subjects"] = [sj for sj in item["subjects"] if sj["ref"] in keep]
        prefs = {sj["packet_ref"] for sj in item["subjects"]}
        item["coverage"] = {p: c for p, c in item["coverage"].items() if p in prefs}
        item["fact_ids"] = [f for f in item["fact_ids"] if f.split(".")[0] in prefs]
    return items


ALL = ("routine", "tidy", "social", "guests")


# ---------------------------------------------------------------- named subsets: positive
@pytest.mark.parametrize("sentence", [
    "Alex and Bailey recorded matching routines.",
    "Bailey and Alex recorded matching routines.",
    "Alex and Bailey gave matching answers about routine, tidiness, social style and guests, which suggests their day-to-day "
    "rhythms should sit comfortably together. The listed balcony gives the household some shared outdoor space.",
    "Alex and Bailey's routines, tidiness and social styles line up well.",
    "Alex, Bailey and Casey all recorded early routines.",   # three named, basis covers three
])
def test_named_comparison_covered_for_exactly_those_people_is_accepted(monkeypatch, sentence):
    basis = _basis(ALL) if "Casey" in sentence else _subset_basis(ALL)
    text = _draft(monkeypatch, _prose(group_fit=sentence), basis)
    assert sentence in text


def test_named_list_keeps_both_names_through_the_clause_split():
    names = ai._member_names(_people())
    assert ai._protect_name_lists("Alex and Bailey recorded matching routines, and everyone is tidy.", names) == \
        "Alex & Bailey recorded matching routines, and everyone is tidy."
    assert ai._protect_name_lists("Casey, Alex and Bailey keep early hours", names) == "Casey & Alex & Bailey keep early hours"
    assert ai._protect_name_lists("Casey and Alex and Bailey are early risers.", names) == "Casey & Alex & Bailey are early risers."
    assert ai._protect_name_lists("Alex, Bailey, and Casey keep early hours", names) == "Alex & Bailey & Casey keep early hours"
    assert ai._protect_name_lists("Alex and the balcony", names) == "Alex and the balcony"   # not a name list: still a clause break
    assert ai._named_refs("Alex & Bailey recorded", names) == ({"I1", "I2"}, False)


# ---------------------------------------------------------------- named subsets: negative (no bypass)
@pytest.mark.parametrize("sentence", [
    "Alex and Casey recorded matching routines.",
    "Casey and Alex recorded matching routines.",          # reverse order: the uncovered person is still in scope
])
def test_a_named_person_without_own_coverage_is_refused(monkeypatch, sentence):
    with pytest.raises(ai.AIUnavailable, match=r"habit \(routine\) for named people .*I3"):
        _draft(monkeypatch, _prose(group_fit=sentence), _subset_basis(("routine",)))


def test_repeated_and_list_keeps_the_uncovered_person_in_scope(monkeypatch):
    people = _people() + [{"ref": "I4", "first_name": "Dee", "name": "Dee", "is_lead": True, "lead_ref": "I4", "enquiry_id": 4, "party_member_id": None}]
    bios = {**_prose()["bios"], "I4": "Dee is looking forward to meeting the group."}
    sentence = "Casey and Alex and Bailey are early risers."   # three of four named, repeated 'and', Casey uncovered
    with pytest.raises(ai.AIUnavailable, match=r"\(routine\) for named people .*I3"):
        _draft(monkeypatch, _prose(group_fit=sentence, bios=bios), _subset_basis(("routine",)), people=people)
    # all three named people covered by one note about them -> accepted (Dee, unnamed, is not asserted about)
    ok = _subset_basis(("routine",), refs=("I1", "I2", "I3"))
    assert sentence in _draft(monkeypatch, _prose(group_fit=sentence, bios=bios), ok, people=people)


def test_naming_every_member_is_a_whole_household_claim(monkeypatch):
    with pytest.raises(ai.AIUnavailable, match=r"shared household habit \(routine\)"):
        _draft(monkeypatch, _prose(group_fit="Alex, Bailey and Casey recorded matching routines."), _subset_basis(("routine",)))


def test_named_scope_carries_through_a_field_list_and_each_field_needs_coverage(monkeypatch):
    listed = "Alex and Bailey's routines, tidiness and social styles line up well."
    with pytest.raises(ai.AIUnavailable, match=r"\(tidy\) for named people"):
        _draft(monkeypatch, _prose(group_fit=listed), _subset_basis(("routine", "social")))


def test_later_everyone_clause_resets_to_the_whole_household(monkeypatch):
    sentence = "Alex and Bailey recorded matching routines, and everyone keeps the home equally tidy."
    with pytest.raises(ai.AIUnavailable, match=r"shared household habit \(tidy\)"):
        _draft(monkeypatch, _prose(group_fit=sentence), _subset_basis(ALL))
    # ... and a whole-household marker in the SAME clause as names means everyone, not the named two
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(group_fit="Alex and Bailey, like everyone here, keep early routines."), _subset_basis(ALL))


def test_duplicate_first_name_cannot_select_the_covered_person(monkeypatch):
    people = _people()
    people[2]["first_name"] = people[2]["name"] = "Alex"
    prose = _prose(group_fit="Alex and Bailey recorded matching routines.")
    probs = ai.validate_group_prose(prose, people, basis=_subset_basis(("routine",)))
    assert probs and "every person's own recorded answer" in probs[0]
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, prose, _subset_basis(("routine",)), people=people)


def test_single_name_with_sameness_word_is_about_everyone(monkeypatch):
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(group_fit="Alex has a matching routine."), _subset_basis(("routine",)))


def test_named_people_must_be_subjects_of_one_basis_note_together(monkeypatch):
    # own coverage exists for both, but from two separate notes (one about each) - a comparison needs a note about them together
    basis = _subset_basis(("routine",), refs=("I1",)) + _subset_basis(("routine",), refs=("I2",))
    with pytest.raises(ai.AIUnavailable, match="not together the subject of any basis note"):
        _draft(monkeypatch, _prose(group_fit="Alex and Bailey recorded matching routines."), basis)


def test_lead_reported_or_missing_answers_never_license_a_named_claim(monkeypatch):
    for kind in (("own", "missing"), ("party_reported", "recorded")):
        basis = _basis(("routine",), basis=kind[0], status=kind[1])
        with pytest.raises(ai.AIUnavailable, match="for named people"):
            _draft(monkeypatch, _prose(group_fit="Alex and Bailey recorded matching routines."), basis)


# ---------------------------------------------------------------- apartment words and missing subjects
def test_apartment_word_does_not_bypass_a_people_claim(monkeypatch):
    for sentence in ("Alex and Bailey keep the kitchen tidy.", "Everyone keeps the apartment's kitchen tidy.", "The household's balcony routines line up."):
        with pytest.raises(ai.AIUnavailable, match="tidy|routine"):
            _draft(monkeypatch, _prose(group_fit=sentence), _subset_basis(("social",)))


def test_listing_statements_without_a_people_subject_are_still_fine(monkeypatch):
    ok = _prose(group_fit="The apartment has a separate kitchen for cooking and sits on a quiet street. The listed balcony is available.")
    basis = [{"section": "strengths", "text": "balcony", "fact_ids": ["A981.feature.balcony"], "subjects": [], "fields": ["feature:Balcony"], "coverage": {}, "facts_hash": "x"}]
    assert ok["group_fit"] in _draft(monkeypatch, ok, basis)


def test_habit_asserted_about_nobody_in_particular_is_refused_in_assertive_parts(monkeypatch):
    with pytest.raises(ai.AIUnavailable, match=r"\(routine\) without saying who it is about"):
        _draft(monkeypatch, _prose(group_fit="Mornings are quiet and routines are settled here."), _basis(ALL))
    with pytest.raises(ai.AIUnavailable, match="without saying who it is about"):
        _draft(monkeypatch, _prose(opening="A tidy home awaits."), _basis(ALL))
    # to_discuss is a list of topics: a bare habit word there is not an assertion about anyone
    text = _draft(monkeypatch, _prose(to_discuss="Guests, cleaning and cooking arrangements."), _basis(ALL))
    assert "Guests, cleaning and cooking arrangements." in text


def test_existing_household_wording_keeps_requiring_everyone(monkeypatch):
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(group_fit="Routines line up well."), _subset_basis(("routine",)))
    assert "Everyone's routine preferences are recorded." in _draft(monkeypatch, _prose(group_fit="Everyone's routine preferences are recorded."), _basis(("routine",)))


def test_collective_markers_are_split_and_to_discuss_still_names_nobody():
    assert ai._ALL_PEOPLE.search("everyone") and not ai._ALL_PEOPLE.search("matching")
    assert ai._SAMENESS.search("matching") and not ai._SAMENESS.search("everyone")
    probs = ai.validate_intro_prose(_prose(to_discuss="Alex should say when guests are fine."), _people())
    assert any("singles out Alex" in p for p in probs)


# ---------------------------------------------------------------- sex / age: deterministic label only
def test_model_payload_and_prompt_carry_no_sex_or_age(monkeypatch):
    prompts = []
    _draft(monkeypatch, _prose(), _basis(), prompts=prompts)
    head, payload = prompts[0].rsplit("\n\n", 1)
    people = json.loads(payload)["people"]
    assert all("intro_sex" not in p and "intro_age" not in p for p in people)
    assert "Male" not in payload and "Female" not in payload and "Agender" not in payload and "24" not in payload
    assert "only if intro_sex/intro_age" not in head.lower() and "sex and age are never written by you" in head.lower()
    assert "you may compare named people" in head.lower() and "names are attribution of who a note is about, never a reason" in head.lower()
    # the deterministic profile line still carries the supplied label (GP2)
    text = _draft(monkeypatch, _prose(), _basis())
    assert "Male" in text and "Female" in text and "Agender" in text and "24" in text


@pytest.mark.parametrize("bio", ["Alex is listed as male and says hi to everyone.", "Alex, a man, says hi to everyone.", "Alex says hi; he is friendly."])
def test_bio_sex_words_are_refused_with_the_profile_line_message(monkeypatch, bio):
    with pytest.raises(ai.AIUnavailable, match="uses a sex label, gendered word or pronoun .*profile line"):
        _draft(monkeypatch, _prose(bios={**_prose()["bios"], "I1": bio}), _basis())


def test_numeric_bio_guard_is_unchanged(monkeypatch):
    # root ruling: no blanket number ban - the supplied age is still tolerated by the validator, other numbers are not
    assert "Alex is 24" in _draft(monkeypatch, _prose(bios={**_prose()["bios"], "I1": "Alex is 24 and says hi to everyone."}), _basis())
    with pytest.raises(ai.AIUnavailable, match="not this person's supplied age"):
        _draft(monkeypatch, _prose(bios={**_prose()["bios"], "I1": "Alex is 25 and says hi to everyone."}), _basis())


# ---------------------------------------------------------------- staff brief prompt: gap placement + plain wording hints
def _brief_packet():
    members = [{"ref": "U1", "doing": "Studying", "school": "Monash", "routine": "Early riser", "party": "Alone", "party_size": "1", "move_in": "2027-02-01"},
               {"ref": "U2", "doing": "Working", "routine": "Night owl", "party": "Alone", "party_size": "1", "move_in": "2027-02-01"}]
    apts = [{"apartment_id": 981, "type_label": "Two bedroom", "suburb": "Synthetic", "bills_included": True, "lease_start": "2027-02-01", "lease_end": "2027-07-10",
             "total_weekly": 1225.0, "campus_km": None, "features": [{"key": "gym", "label": "Gym", "recorded": "unknown"}], "rule_checks": []}]
    return {"members_for_ai": members, "person_refs": ["U1", "U2"], "apartments": apts, "review": {}, "individually_recorded": {}}


def test_brief_prompt_partitions_missing_ids_and_states_where_gaps_go(monkeypatch):
    prompts = []

    def reply(build, *a, **k):
        prompts.append(build(False))
        raise ai.AIUnavailable("stop here")
    monkeypatch.setattr(ai, "_ask_bounded", reply)
    with pytest.raises(ai.AIUnavailable):
        REAL_STAFF_BRIEFS(_brief_packet())
    head, payload = prompts[0].rsplit("\n\n", 1)
    desc = json.loads(payload)
    assert set(desc["missing_fact_ids_by_ref"]) == {"U1", "U2"}
    assert "U1.tidy" in desc["missing_fact_ids_by_ref"]["U1"] and "U2.school" in desc["missing_fact_ids_by_ref"]["U2"]
    assert "A981.feature.gym" in desc["apartment_missing_fact_ids"]
    assert "A981.campus_km" not in desc["missing_fact_ids"] and "campus_km" not in payload   # M4: an unsupplied distance is not a "missing" detail
    assert not any(k.startswith("A") for v in desc["missing_fact_ids_by_ref"].values() for k in v)
    assert sorted(set(desc["apartment_missing_fact_ids"]) | {k for v in desc["missing_fact_ids_by_ref"].values() for k in v}) == desc["missing_fact_ids"]
    low = head.lower()
    assert "an unknown is a missing fact, never evidence" in low and "may appear only under missing_facts or discussion_points" in low
    assert "never in a member's lists" in low and "may be empty" in low
    assert "never paste field names or fact ids such as bunk_ok, allowance_fraction or u1.routine into the sentence" in low
    assert "do not convert, round, add up or re-interpret" in low and "as a raw fraction" in low
    # person refs (U1, U1-m1) stay the way a note names a person: identity-free prose that staff can map back later
    assert "refer to people by their person ref (u1, u1-m1, u2 ...) in the sentence - never by name" in low


def test_brief_validator_still_rejects_the_two_live_shapes():
    """the live 8 Nov failures (gap under what_could_cause_issues; apartment gaps in a member's missing_facts) remain refusals -
    the prompt steers, the validator is unchanged and nothing is relocated silently"""
    packet = _brief_packet()
    refs = ["U1", "U2"]
    good = {"group": {"strengths": [{"fact_ids": ["U1.routine", "U2.routine"], "note": "different routines recorded"}], "tensions": [],
                      "missing_facts": [{"fact_ids": ["A981.feature.gym"], "note": "gym not recorded", "kind": "gap"}],
                      "discussion_points": [{"fact_ids": ["U1.routine"], "note": "talk about mornings"}], "what_could_work": [], "what_could_cause_issues": []},
            "members": [{"ref": "U1", "strengths": [{"fact_ids": ["U1.routine"], "note": "early riser"}], "tensions": [], "missing_facts": [], "discussion_points": []},
                        {"ref": "U2", "strengths": [{"fact_ids": ["U2.routine"], "note": "night owl"}], "tensions": [], "missing_facts": [], "discussion_points": []}]}
    res = ai.parse_staff_briefs(json.loads(json.dumps(good)), refs, packet)
    assert res["group"]["missing_facts"][0]["fact_ids"] == ["A981.feature.gym"]
    bad = json.loads(json.dumps(good))
    bad["group"]["what_could_cause_issues"] = [{"fact_ids": ["A981.feature.gym"], "note": "no gym recorded", "kind": "gap"}]
    with pytest.raises(ai.AIUnavailable, match="what_could_cause_issues\\[0\\]: a gap item cannot stand as evidence"):
        ai.parse_staff_briefs(bad, refs, packet)
    bad = json.loads(json.dumps(good))
    bad["members"][0]["missing_facts"] = [{"fact_ids": ["A981.feature.gym"], "note": "apartment details unknown", "kind": "gap"}]
    with pytest.raises(ai.AIUnavailable, match="U1.missing_facts"):
        ai.parse_staff_briefs(bad, refs, packet)
