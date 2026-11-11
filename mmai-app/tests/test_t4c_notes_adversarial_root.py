"""Independent bad-model-output probes, separate from happy-path extraction."""
import pytest
from server import notes_facts


def result(text, *, span=None, category="step_free_access", strength="essential", value=None):
    span = text if span is None else span
    start = text.index(span)
    return notes_facts.validate_reply({"items": [{
        "category": category, "polarity": "affirmed", "strength": strength,
        "subject": "self", "field": "else_text",
        "span": span, "start": start, "end": start + len(span), "value": value,
    }], "residual": False}, {"else_text": text})


@pytest.mark.parametrize("text,span", [
    ("I do not need step-free access.", "step-free access"),
    ("My friend needs step-free access, not me.", "step-free access"),
    ("I used to need step-free access, but I no longer do.", "step-free access"),
    ("If my plans change I might need step-free access.", "step-free access"),
    ("I prefer a balcony.", "I prefer a balcony."),
])
def test_wrong_affirmed_self_or_category_is_not_a_fact(text, span):
    assert not result(text, span=span)["packet_items"]


def test_unstated_date_is_not_grounded_by_a_verbatim_span():
    assert not result(
        "I cannot arrive before 2027-03-01.",
        category="move_in_constraint",
        value={"comparator": "on_or_after", "date": "2028-03-01"},
    )["packet_items"]


def test_reversed_date_comparator_is_not_grounded():
    assert not result(
        "I cannot arrive before 2027-03-01.",
        category="move_in_constraint",
        value={"comparator": "on_or_before", "date": "2027-03-01"},
    )["packet_items"]


def test_unstated_time_is_not_grounded():
    assert not result(
        "I work from 22:00 until 06:00.",
        category="variable_or_night_hours", strength="stated",
        value={"pattern": "night", "from": "21:00", "until": "06:00"},
    )["packet_items"]


def test_preference_is_not_upgraded_to_essential():
    out = result("Step-free access would be helpful.")
    assert not any(i["strength"] == "essential" for i in out["packet_items"])


def test_duplicate_resolution_keeps_essential_evidence():
    text = "Step-free access would be helpful. Step-free access is essential."
    items = []
    for span, strength in [
        ("Step-free access would be helpful.", "matters"),
        ("Step-free access is essential.", "essential"),
    ]:
        start = text.index(span)
        items.append({
            "category": "step_free_access", "polarity": "affirmed", "strength": strength,
            "subject": "self", "field": "else_text", "span": span,
            "start": start, "end": start + len(span), "value": None,
        })
    for sequence in (items, list(reversed(items))):
        out = notes_facts.validate_reply({"items": sequence, "residual": False}, {"else_text": text})
        assert any(i["strength"] == "essential" for i in out["packet_items"])
