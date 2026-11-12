"""Offline regressions for the retained constructive-campaign failure classes."""
import json

import pytest

from server import ai, facts, notes_facts as nf


GOOD = '{"items": [], "residual": false}'


@pytest.mark.parametrize("replies", [
    [GOOD],
    ["not JSON", GOOD],
    [ai.AIOutputTruncated("test truncation"), GOOD],
])
def test_notes_schema_on_every_bounded_turn(monkeypatch, replies):
    seen = []
    turns = iter(replies)

    def ask(*args, **kwargs):
        seen.append(ai._SCHEMA.get())
        reply = next(turns)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ai, "_ask", ask)
    meter = {}
    nf._ask({"else_text": "", "work": "", "intro": ""}, 1, meter)
    assert all(schema is not None and schema["type"] == "object" for schema in seen)
    assert len(seen) == len(replies) == meter["asks"]
    assert ai._SCHEMA.get() is None


@pytest.mark.parametrize("reply", ["before " + GOOD, GOOD + " after",
                                  "```json\n" + GOOD + "\n```", GOOD + GOOD])
def test_notes_require_the_whole_reply_to_be_one_object(reply):
    assert nf._parse_problem(reply)[1] is not None


def test_notes_schema_rejection_is_terminal(monkeypatch):
    seen = []

    def ask(*args, **kwargs):
        seen.append(ai._SCHEMA.get())
        raise ai.AISchemaUnsupported()

    monkeypatch.setattr(ai, "_ask", ask)
    with pytest.raises(ai.AISchemaUnsupported):
        nf._ask({}, 1, {})
    assert len(seen) == 1 and seen[0] is not None
    assert ai._SCHEMA.get() is None


@pytest.mark.parametrize("note", [
    "U2 has no recorded school or course, so study or work schedule overlap with U1's nursing study is unknown and worth confirming.",
    "Whether study or work schedules overlap with U1's is unknown.",
    "Schedule alignment between U1 and U2 remains unconfirmed.",
    "Timetable alignment cannot be inferred from the recorded facts.",
    "Schedule alignment cannot be confirmed.",
])
def test_explicit_schedule_unknown_is_not_an_assertion(note):
    assert facts.timetable_inference(note, routine_cited=False) is None


@pytest.mark.parametrize("note", [
    "Their schedules line up.",
    "Their schedules likely overlap.",
    "Schedule overlap is unknown but probably good.",
    "Schedule overlap is unknown, though likely fine.",
    "Schedule overlap is not disputed.",
    "Schedule overlap is unknown; their class times align.",
    "Their class times align. Schedule overlap is unknown.",
    "Schedules overlap despite an unknown campus.",
    "Timetable overlap with U1 is unknown, probably fine.",
    "Timetable overlap with U1 is unknown, similar hours though.",
    "Timetable overlap is unknown though likely fine.",
    "Timetable overlap is unknown but probably good.",
    "Their timetable alignment is not disputed.",
    "Their timetable alignment is undeniable.",
    "Timetable alignment cannot be inferred from the recorded facts, probably fine.",
    "Schedule alignment cannot be confirmed but probably good.",
])
def test_positive_schedule_assertions_still_fail(note):
    assert facts.timetable_inference(note, routine_cited=False) is not None
