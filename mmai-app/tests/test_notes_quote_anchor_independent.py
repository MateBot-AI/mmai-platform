"""Independent component checks; no DB and no provider calls."""
import copy
import pytest
from server import notes_facts as nf


def item(span, field="else_text", start=98, end=99):
    return dict(category="step_free_access", polarity="affirmed",
                strength="preference", subject="self", field=field,
                start=start, end=end, span=span, value=None)


def fields(text, work="", intro=""):
    return dict(else_text=text, work=work, intro=intro)


def test_overlapping_exact_occurrences_are_ambiguous():
    # str.count is non-overlapping and incorrectly reports just one occurrence.
    span = "Step-free access Step-free access"
    text = "Step-free access Step-free access Step-free access"
    reply = dict(items=[item(span)], residual=False)
    assert text.find(span, text.find(span) + 1) >= 0
    anchored, provenance = nf.anchor_items(reply, fields(text))
    assert provenance == []
    assert anchored["items"] == reply["items"]
    out = nf.validate_reply(anchored, fields(text))
    assert not out["packet_items"]
    assert out["rejected"][0]["code"] == "rejected_offsets"


@pytest.mark.parametrize("text,span", [
    ("Step-free access. Step-free access.", "Step-free access"),
    ("step-free access", "Step-free access"),
    ("Step‑free access", "Step-free access"),
    ("No request here", "Step-free access"),
])
def test_no_fuzzy_or_ambiguous_anchor(text, span):
    reply = dict(items=[item(span)], residual=False)
    saved = copy.deepcopy(reply)
    anchored, provenance = nf.anchor_items(reply, fields(text))
    assert not provenance
    assert anchored["items"] == saved["items"] and reply == saved
    assert not nf.validate_reply(anchored, fields(text))["packet_items"]


def test_unique_codepoint_anchor_and_no_cross_field_search():
    text = "🌿 Step-free access please."
    reply = dict(items=[item("Step-free access")], residual=False)
    anchored, provenance = nf.anchor_items(reply, fields(text))
    assert provenance[0]["anchored"] == [2, 18]
    assert provenance[0]["supplied"] == [98, 99]
    assert reply["items"][0]["start"] == 98
    assert nf.validate_reply(anchored, fields(text))["packet_items"]
    wrong_field = dict(items=[item("Step-free access", field="work")], residual=False)
    anchored, provenance = nf.anchor_items(wrong_field, fields(text, work="Nurse"))
    assert not provenance
    assert not nf.validate_reply(anchored, fields(text, work="Nurse"))["packet_items"]


def test_anchor_never_waives_quotation_or_negation():
    for text in ['They said "Step-free access".', "I do not need Step-free access."]:
        reply = dict(items=[item("Step-free access")], residual=False)
        anchored, provenance = nf.anchor_items(reply, fields(text))
        assert provenance
        assert not nf.validate_reply(anchored, fields(text))["packet_items"]


def test_field_coverage_floor_changes_no_packet_or_hold():
    text = "Step-free access please."
    reply = dict(items=[item(text, start=0, end=len(text))], residual=False)
    blank = nf.validate_reply(reply, fields(text))
    uncovered = nf.validate_reply(reply, fields(text, work="Nurse", intro="Hello"))
    assert not blank["residual"] and uncovered["residual"]
    assert blank["packet_items"] == uncovered["packet_items"]
    assert nf.hold_gate([{"ref": "U1", "notes_facts": blank["packet_items"]}], None) == nf.hold_gate(
        [{"ref": "U1", "notes_facts": uncovered["packet_items"]}], None)
