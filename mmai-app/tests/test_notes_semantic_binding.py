"""Bad replies whose words/values occur in the source but mean something else.

Separate from the original ten-case prototype probe. These check elementary
binding, not a claim that regex rules solve arbitrary natural-language entailment.
"""
import pytest
from server import notes_facts


def evaluate(text, category, value=None, strength="essential", span=None):
    span = text if span is None else span
    start = text.index(span)
    return notes_facts.validate_reply({"items": [{
        "category": category, "polarity": "affirmed", "strength": strength,
        "subject": "self", "field": "else_text", "span": span,
        "start": start, "end": start + len(span), "value": value,
    }], "residual": False}, {"else_text": text})


@pytest.mark.parametrize("text,category,value,strength,span", [
    ("I work from 22:00 until 06:00.", "variable_or_night_hours",
     {"pattern": "night", "from": "06:00", "until": "22:00"}, "stated", None),
    ("I work from 22:00 until 06:00 and need quiet from 07:00 until 15:00.",
     "quiet_at_night", {"quiet_from": "22:00", "quiet_until": "06:00"}, "essential", None),
    ("Lift access is essential for me.", "step_free_access", None, "essential", None),
    ("Sharing a room is essential for me.", "own_room_need", None, "essential", None),
    ("I cook dinner every evening.", "kitchen_separation", None, "stated", None),
    ("A smoke-free interior is essential; outside smoking at home is fine.",
     "smoke_free_home", {"scope": "whole_home"}, "essential", None),
    ("I prefer step-free access but must be near a tram.",
     "step_free_access", None, "essential", "step-free access"),
])
def test_words_in_source_are_not_enough_to_support_a_different_assertion(text, category, value, strength, span):
    assert not evaluate(text, category, value, strength, span)["packet_items"]


@pytest.mark.parametrize("text,category,value,strength", [
    ("Step-free access is essential for me.", "step_free_access", None, "essential"),
    ("I work from 22:00 until 06:00.", "variable_or_night_hours",
     {"pattern": "night", "from": "22:00", "until": "06:00"}, "stated"),
    ("I cannot arrive before 2027-03-01.", "move_in_constraint",
     {"comparator": "on_or_after", "date": "2027-03-01"}, "essential"),
    ("I need my own room.", "own_room_need", None, "essential"),
    ("I need separate kitchen utensils.", "kitchen_separation", None, "essential"),
    ("A smoke-free interior is essential.", "smoke_free_home",
     {"scope": "interior"}, "essential"),
])
def test_supported_controls_are_not_all_rejected(text, category, value, strength):
    assert evaluate(text, category, value, strength)["packet_items"]


def test_explicit_essential_cannot_be_downgraded_to_matters_and_bypass_hold():
    out = evaluate("Step-free access is essential for me.", "step_free_access", strength="matters")
    assert not out["packet_items"] or any(i["strength"] == "essential" for i in out["packet_items"])
