"""r7 stage B: introduction prose guards and prompt binding (ai.py; 8 November 2026).
  * a supplied sex term (Male / Female / anything) never licenses pronouns or gendered nouns for ANY person; the name and neutral
    'they' are allowed; the supplied term still appears verbatim in that person's deterministic profile line;
  * a collective household-habit claim (routines, tidiness, social style, guests, diet) in group prose is licensed only when the
    structured basis covers that field for EVERY person with their OWN recorded answer: study/work coverage, a missing answer, a
    lead-reported (party_reported) fact, a companion without answers, or a legacy string basis never license it; sentences that
    invite the group to discuss something are exempt; apartment-only notes are a valid basis;
  * the prompt carries the EXACT apartment and offer objects (the ones ai_queue fingerprints) and the basis note text + who it is
    about - never fact ids, coverage, stored values or contact details.
Synthetic people and a doubled transport only; no model call. Wording guards, not proof of truth. Not a launch-readiness claim.
"""
import json
import pytest
from server import ai


def _people(third_sex="Agender"):
    return [{"ref": "I1", "first_name": "Alex", "name": "Alex", "is_lead": True, "lead_ref": "I1", "enquiry_id": 1, "party_member_id": None, "intro_sex": "Male", "intro_age": 24, "intro": "Hi all."},
            {"ref": "I2", "first_name": "Bailey", "name": "Bailey", "is_lead": True, "lead_ref": "I2", "enquiry_id": 2, "party_member_id": None, "intro_sex": "Female", "diet": "Vegetarian"},
            {"ref": "I3", "first_name": "Casey", "name": "Casey", "is_lead": True, "lead_ref": "I3", "enquiry_id": 3, "party_member_id": None, "intro_sex": third_sex}]


def _prose(**over):
    p = {"opening": "Here is a proposed household for you to consider.",
         "bios": {"I1": "Alex says hi to everyone.", "I2": "Bailey enjoys vegetarian cooking.", "I3": "Casey is looking forward to meeting everyone."},
         "group_fit": "The listed balcony is available to the household.", "to_discuss": "Everyone should discuss their routines and expectations.",
         "closing": "Please say hello and let ALM know what you think."}
    p.update(over)
    return p


def _basis(fields=("routine",), basis="own", status="recorded", refs=("I1", "I2", "I3")):
    return [{"section": "strengths", "text": "The cited answers give some context.", "fact_ids": [f"U{i}.{f}" for i in range(1, 4) for f in fields],
             "subjects": [{"ref": r, "packet_ref": f"U{i}", "enquiry_id": i, "party_member_id": None, "name": n} for i, (r, n) in enumerate(zip(refs, ("Alex", "Bailey", "Casey")), 1)],
             "fields": list(fields), "coverage": {f"U{i}": {f: {"status": status, "basis": basis} for f in fields} for i in range(1, 4)},
             "facts_hash": "synthetic"}]


APT = {"id": 981, "address": "9 Synthetic Street", "type_code": "2B", "type_label": "Two bedroom", "suburb": "Synthetic Suburb", "bills_included": True,
       "features": [{"key": "balcony", "label": "Balcony", "recorded": "yes"}, {"key": "bbq", "label": "BBQ facilities", "recorded": "unknown"}]}
OFFER = {"lease_start": "2027-02-01", "lease_end": "2027-07-10", "rate_kind": "6m", "rate": "6-month rate", "exit_window": "July", "weekly_rent": 1200.0, "total_weekly": 1225.0,
         "extra_bed_charge": 25.0, "liability_note": "One lease; shares are a guide.", "rooms": [{"room": 1, "setup": "double", "room_cost": 600.0, "extra_bed": 0, "occupants": [{"person_id": "1", "share": 600.0, "bed": "double", "arrangement": "single"}]}]}


def _draft(monkeypatch, prose, basis, people=None, prompts=None):
    def reply(build, *a, **k):
        if prompts is not None:
            prompts.append(build(False))
        return json.dumps(prose)
    monkeypatch.setattr(ai, "_ask_bounded", reply)
    return ai.draft_introduction(APT, OFFER, people or _people(), basis=basis)


# ---------------------------------------------------------------- sex label
def test_sex_allows_is_empty_for_every_label():
    for v in ("Male", "Female", "male", "Agender", "Prefer not to say", None, "", "Another term", "they/them"):
        assert ai._sex_allows(v) == ()


@pytest.mark.parametrize("ref,bio", [("I1", "Alex is a man who says hi to everyone."), ("I1", "Alex says hi; he welcomes a chat."), ("I1", "Alex says hi; his cooking is good."),
                                     ("I2", "Bailey is a woman who enjoys vegetarian cooking."), ("I2", "Bailey enjoys cooking; she welcomes a chat."), ("I3", "Casey is a lady.")])
def test_supplied_label_never_licenses_gendered_prose(monkeypatch, ref, bio):
    with pytest.raises(ai.AIUnavailable, match="gendered word or pronoun"):
        _draft(monkeypatch, _prose(bios={**_prose()["bios"], ref: bio}), _basis())


def test_they_and_names_are_allowed_and_label_shows_only_in_profile_line(monkeypatch):
    prose = _prose(bios={"I1": "Alex says hi to everyone; they are happy to chat.", "I2": "Bailey enjoys vegetarian cooking.", "I3": "Casey is looking forward to meeting everyone."})
    text = _draft(monkeypatch, prose, _basis())
    assert "they are happy to chat" in text
    assert "Alex (24, Male)" in text and "Bailey (Female)" in text and "Casey (Agender)" in text   # verbatim supplied terms, deterministic block only
    assert "Male" not in prose["group_fit"] and "Male" not in prose["bios"]["I1"]


# ---------------------------------------------------------------- collective habit claims
HOUSEHOLD = "The household's day-to-day routines, tidiness and social styles line up well."


@pytest.mark.parametrize("fields,basis,status", [(("doing",), "own", "recorded"), (("routine",), "none", "missing"), (("routine",), "party_reported", "recorded"),
                                                 (("routine", "tidy"), "own", "recorded")])   # social style never covered
def test_household_claim_needs_every_persons_own_recorded_answer(monkeypatch, fields, basis, status):
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(group_fit=HOUSEHOLD), _basis(fields, basis, status))


def test_household_claim_licensed_when_all_three_fields_are_own_recorded_for_everyone(monkeypatch):
    text = _draft(monkeypatch, _prose(group_fit=HOUSEHOLD), _basis(("routine", "tidy", "social")))
    assert HOUSEHOLD in text


def test_one_person_without_own_answer_blocks_the_claim(monkeypatch):
    b = _basis(("routine", "tidy", "social"))
    b[0]["coverage"]["U3"]["routine"] = {"status": "recorded", "basis": "party_reported"}   # Casey's routine is lead-reported
    with pytest.raises(ai.AIUnavailable, match="\\(routine\\)"):
        _draft(monkeypatch, _prose(group_fit=HOUSEHOLD), b)
    # a companion in the introduction with no own answers blocks it too
    people = _people() + [{"ref": "I1-m1", "first_name": "Dee", "name": "Dee Example", "is_lead": False, "lead_ref": "I1", "enquiry_id": 1, "party_member_id": 9}]
    prose = _prose(group_fit=HOUSEHOLD, bios={**_prose()["bios"], "I1-m1": "Dee is coming along with Alex."})
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, prose, _basis(("routine", "tidy", "social")), people=people)


def test_legacy_string_basis_cannot_license_a_habit_claim_but_still_drafts_neutral_prose(monkeypatch):
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(group_fit="Everyone keeps similar hours."), ["strengths: early risers [U1.routine=Early riser]"])
    assert "balcony" in _draft(monkeypatch, _prose(), ["strengths: dates line up"])


def test_discussion_invitations_and_apartment_only_basis_are_fine(monkeypatch):
    basis = [{"section": "strengths", "text": "The selected apartment has a recorded balcony.", "fact_ids": ["A981.feature.balcony"], "subjects": [], "fields": ["feature:Balcony"], "coverage": {}, "facts_hash": "x"}]
    prose = _prose(group_fit="The listed balcony is available to the household; it is worth agreeing how to share it.",
                   to_discuss="Everyone should discuss their routines, tidiness, guests and cooking so expectations are clear.")
    text = _draft(monkeypatch, prose, basis)
    assert prose["to_discuss"] in text and prose["group_fit"] in text
    # the same words as an assertion (no invitation) fail
    with pytest.raises(ai.AIUnavailable, match="shared household habit"):
        _draft(monkeypatch, _prose(to_discuss="Everyone keeps a tidy kitchen."), basis)


def test_mixed_assertion_plus_invitation_still_asserts_and_apartment_statements_are_not_habit_claims(monkeypatch):
    basis = [{"section": "strengths", "text": "The selected apartment has a recorded balcony.", "fact_ids": ["A981.feature.balcony"], "subjects": [], "fields": ["feature:Balcony"], "coverage": {}, "facts_hash": "x"}]
    for text in ("Everyone has matching routines, which you can discuss together.", "You all keep a tidy home and should agree on chores.",
                 "The household's cooking habits line up - worth agreeing who shops."):
        with pytest.raises(ai.AIUnavailable, match="shared household habit"):
            _draft(monkeypatch, _prose(group_fit=text), basis)
    # each habit in a collective list needs its own field coverage; the subject carries across the clauses (curly apostrophe too)
    listed = "The household\u2019s routines, tidiness and social styles line up well."
    with pytest.raises(ai.AIUnavailable, match="\\(tidy\\)"):
        _draft(monkeypatch, _prose(group_fit=listed), _basis(("routine", "social")))
    assert listed in _draft(monkeypatch, _prose(group_fit=listed), _basis(("routine", "social", "tidy")))
    # an invitation does not launder a later clause that names the people again
    with pytest.raises(ai.AIUnavailable, match="\\(tidy\\)"):
        _draft(monkeypatch, _prose(group_fit="You could discuss routines, and everyone is tidy anyway."), _basis(("routine",)))
    # statements about the LISTING (no collective-people marker) are not habit claims
    ok = _prose(group_fit="The apartment has a separate kitchen for cooking and sits on a quiet street. The listed balcony is available.")
    assert ok["group_fit"] in _draft(monkeypatch, ok, basis)
    assert ai.habit_claim_problems(ok, _people(), basis) == []
    assert ai.habit_claim_problems({"group_fit": "Everyone has matching routines, which you can discuss together."}, _people(), basis) != []


def test_prompt_shows_field_level_provenance_and_allowed_claims_without_values(monkeypatch):
    prompts = []
    b = _basis(("routine", "tidy"))
    b[0]["coverage"]["U3"]["tidy"] = {"status": "recorded", "basis": "party_reported"}
    b[0]["coverage"]["U2"]["routine"] = {"status": "missing", "basis": "none"}
    _draft(monkeypatch, _prose(), b, prompts=prompts)
    payload = json.loads(prompts[0][prompts[0].rindex("\n\n") + 2:])
    note = payload["group_basis"][0]
    assert note["own_answers"] == {"I1": ["routine", "tidy"], "I2": ["tidy"], "I3": ["routine"]}
    assert note["lead_reported"] == {"I3": ["tidy"]}
    # two people sharing a first name stay distinct (keyed by current ref, name shown alongside)
    b2 = _basis(("routine",))
    b2[0]["subjects"][2]["name"] = "Alex"
    assert ai._basis_for_prompt(b2)[0]["about"][2] == {"ref": "I3", "name": "Alex"} and set(ai._basis_for_prompt(b2)[0]["own_answers"]) == {"I1", "I2", "I3"}
    assert payload["household_claims_allowed_for"] == []
    assert ai.household_claims_allowed(_people(), _basis(("routine", "tidy", "social"))) == ["routine", "social", "tidy"]
    blob = json.dumps(payload)
    for forbidden in ("Early riser", "coverage", "facts_hash", "party_reported", "\"own\"", "U1.", "enquiry_id", "packet_ref"):
        assert forbidden not in blob, forbidden
    assert blob.count("Vegetarian") == 1 and payload["people"][1]["diet"] == "Vegetarian"   # a person's own answer appears only in their own entry
    assert "household_claims_allowed_for" in prompts[0]


def test_validate_group_prose_without_basis_is_the_pure_wording_guard():
    assert ai.validate_group_prose(_prose(group_fit=HOUSEHOLD), _people()) == []            # legacy call: no coverage check
    assert ai.validate_group_prose(_prose(group_fit=HOUSEHOLD), _people(), basis=[]) != []  # any basis given: checked
    assert ai.habit_claim_problems(_prose(), _people(), []) == []                            # invitation + apartment statement only


# ---------------------------------------------------------------- prompt binding
def test_prompt_carries_exact_apartment_and_offer_objects_and_only_note_text_from_the_basis(monkeypatch):
    prompts = []
    basis = _basis(("routine",))
    basis[0]["text"] = "Alex and Bailey keep early hours."
    _draft(monkeypatch, _prose(), basis, prompts=prompts)
    assert len(prompts) == 1
    payload = json.loads(prompts[0][prompts[0].rindex("\n\n") + 2:])
    assert payload["apartment"] == APT and payload["offer"] == OFFER, "the fingerprinted objects go to the model whole"
    assert payload["group_basis"] == [{"note": "Alex and Bailey keep early hours.", "about": [{"ref": "I1", "name": "Alex"}, {"ref": "I2", "name": "Bailey"}, {"ref": "I3", "name": "Casey"}],
                                       "own_answers": {"I1": ["routine"], "I2": ["routine"], "I3": ["routine"]}}]
    assert payload["household_claims_allowed_for"] == ["routine"]
    blob = json.dumps(payload)
    for forbidden in ("fact_ids", "coverage", "facts_hash", "U1.routine", "party_reported", "enquiry_id", "packet_ref"):
        assert forbidden not in blob, forbidden
    # r7-Q: the supplied sex label and age never reach the model - intro_profile_block prints them deterministically (GP2)
    assert "Male" not in blob and "Agender" not in blob and "intro_sex" not in blob and "intro_age" not in blob
    assert all(k not in person for person in payload["people"] for k in ("intro_sex", "intro_age"))


def test_prompt_never_carries_contact_values(monkeypatch):
    prompts = []
    people = _people()
    people[0].update(email="alex@example.com", phone="+61 0400000000", contact_method="WhatsApp", whatsapp=True)
    _draft(monkeypatch, _prose(), _basis(), people=people, prompts=prompts)
    assert "alex@example.com" not in prompts[0] and "0400000000" not in prompts[0]
