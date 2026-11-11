"""C200 introduction rendering (9 Nov 2026): two deterministic glitches seen in every REAL approved-model draft of the GP13 run.

1. Double salutation: the frame always printed "Hello everyone," and the model's opening also greeted ("Hello Marco and Aiko, ...",
   "Hello from everyone at ALM!"). The fixed line is now omitted only when the opening already starts with a greeting; an opening that
   does not greet keeps the fixed salutation exactly as before.
2. Literal form code in the profile line: 4.1 "Other" rendered as "other in Melbourne" (T10 plain language). It now reads
   "in Melbourne for other reasons"; Studying / Working / Working holiday are unchanged.

Nothing else in the frame changes: AI prose and the deterministic fact blocks (country of birth, contact, guide share) are kept as they
were (GP2 / I8 / consent govern those; not touched here).
"""
import json

import pytest

from server import ai

APT = {"id": 981, "address": "9 Synthetic Street", "type_code": "2B", "type_label": "Two bedroom", "suburb": "Synthetic Suburb"}
OFFER = {"lease_start": "2027-02-01", "lease_end": "2027-07-10", "rate_kind": "6m", "rate": "6-month rate", "weekly_rent": 900.0, "total_weekly": 900.0}


def _people():
    return [{"ref": "I1", "enquiry_id": 1, "is_lead": True, "first_name": "Alex", "name": "Alex Synthetic", "country_of_birth": "Sweden", "doing": "Other",
             "diet": "None", "social": "Friendly but independent", "routine": "Early riser", "email": "alex@example.test", "phone": "0490000001", "weekly_share": 450.0},
            {"ref": "I2", "enquiry_id": 2, "is_lead": True, "first_name": "Bailey", "name": "Bailey Synthetic", "country_of_birth": "Brazil", "doing": "Working",
             "diet": "Halal", "social": "Very social", "routine": "Night owl", "email": "bailey@example.test", "phone": "0490000002", "weekly_share": 450.0}]


def _basis():
    return [{"section": "strengths", "text": "Both recorded their own routines.", "fact_ids": ["U1.routine", "U2.routine"],
             "subjects": [{"ref": "I1", "packet_ref": "U1", "enquiry_id": 1, "party_member_id": None, "name": "Alex"},
                          {"ref": "I2", "packet_ref": "U2", "enquiry_id": 2, "party_member_id": None, "name": "Bailey"}],
             "fields": ["routine"], "coverage": {"U1": {"routine": {"status": "recorded", "basis": "own"}}, "U2": {"routine": {"status": "recorded", "basis": "own"}}},
             "facts_hash": "synthetic"}]


def _prose(opening):
    return {"opening": opening,
            "bios": {"I1": "Alex was born in Sweden and is an early riser who enjoys quiet evenings.", "I2": "Bailey was born in Brazil, works in the city and eats halal."},
            "group_fit": "Alex and Bailey both recorded their own daily routines, which gives you a starting point.",
            "to_discuss": "It would be worth talking together about routines and guests so everyone feels comfortable.",
            "closing": "Please say hello to each other and reply to ALM with any questions."}


def _draft(monkeypatch, opening):
    monkeypatch.setattr(ai, "_ask_bounded", lambda build, *a, **k: json.dumps(_prose(opening)))
    return ai.draft_introduction(APT, OFFER, _people(), basis=_basis())


# ------------------------------------------------------------------ profile line: plain language for the 4.1 choice
def test_other_renders_in_plain_words_not_as_the_form_code():
    block = ai.intro_profile_block(_people()[0])
    assert "other in Melbourne" not in block
    assert block.splitlines()[0] == "Alex (born in Sweden, in Melbourne for other reasons)"


@pytest.mark.parametrize("doing,expect", [("Working", "working in Melbourne"), ("Working holiday", "working holiday in Melbourne"), ("Studying", "studying in Melbourne")])
def test_other_activities_render_exactly_as_before(doing, expect):
    m = dict(_people()[1], doing=doing, school=None, course=None)
    assert ai.intro_profile_block(m).splitlines()[0] == f"Bailey (born in Brazil, {expect})"


def test_studying_with_school_is_unchanged():
    m = dict(_people()[1], doing="Studying", school="Synthetic University", course="Nursing")
    assert ai.intro_profile_block(m).splitlines()[0] == "Bailey (born in Brazil, studying Nursing at Synthetic University)"


def test_profile_block_keeps_every_other_fact():
    block = ai.intro_profile_block(_people()[0])
    assert "  diet: None; social style: Friendly but independent; sleep pattern: Early riser" in block
    assert "  Contact: email alex@example.test, mobile 0490000001" in block
    assert "  Guide share: about $450/week" in block


# ------------------------------------------------------------------ salutation: exactly one greeting
@pytest.mark.parametrize("opening", ["Hello Alex and Bailey, and welcome from all of us at Apartment Living Melbourne!",
                                     "Hello from everyone at Apartment Living Melbourne! We're delighted to introduce you.",
                                     "Hi both, we are pleased to introduce you as proposed housemates.",
                                     "Welcome from Apartment Living Melbourne."])
def test_opening_that_already_greets_is_not_stacked_under_hello_everyone(monkeypatch, opening):
    text = _draft(monkeypatch, opening)
    assert not text.startswith("Hello everyone,")
    assert text.startswith(opening)
    paras = text.split("\n\n")
    assert sum(1 for p in paras if ai.GREETING_RE.match(p)) == 1
    assert paras[1].startswith("Proposed apartment: 9 Synthetic Street")


def test_opening_without_a_greeting_keeps_the_fixed_salutation(monkeypatch):
    opening = "We think the two of you could make good housemates and have put together a proposal."
    text = _draft(monkeypatch, opening)
    assert text.startswith("Hello everyone,\n\n" + opening + "\n\nProposed apartment: 9 Synthetic Street")


def test_greeting_detection_is_a_prefix_test_only():
    assert ai.GREETING_RE.match("Hello everyone,") and ai.GREETING_RE.match("  hi there") and ai.GREETING_RE.match("Good morning all")
    assert not ai.GREETING_RE.match("We say hello to the group later.") and not ai.GREETING_RE.match("Highlights of the apartment")


def test_rest_of_the_frame_is_unchanged(monkeypatch):
    text = _draft(monkeypatch, "Hello Alex and Bailey!")
    p = _prose("")
    for part in (p["bios"]["I1"], p["bios"]["I2"], p["group_fit"], p["to_discuss"], p["closing"]):
        assert part in text
    assert text.endswith("\n\nApartment Living Melbourne")
    assert "Alex (born in Sweden, in Melbourne for other reasons)" in text and "Bailey (born in Brazil, working in Melbourne)" in text
    assert text.count("Hello") == 1
