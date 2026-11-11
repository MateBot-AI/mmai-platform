"""S1b (9 Nov 2026, C200 live-1): the staff-brief prompt shows the compared_with shape with a worked example, says group items never
carry it and that ids are used exactly as listed. Prompt guidance only; the validator is unchanged (the rejection cases below still reject)."""
import importlib

from server import ai, facts
from tests.test_a3_timetable_inference import _cat

REAL_STAFF_BRIEFS = importlib.import_module("server.ai").__dict__["staff_briefs"]   # bound at import, before the conftest double replaces it


def _prompt(monkeypatch):
    seen = {}

    def ask_bounded(build, *a, **k):
        seen["prompt"] = build(False)
        raise ai.AIUnavailable("stop", code="test")
    monkeypatch.setattr(ai, "_ask_bounded", ask_bounded)
    packet = {"members_for_ai": [{"ref": "U1", "routine": "Early riser"}, {"ref": "U2", "routine": "Night owl"}], "person_refs": {}, "apartments": [], "review": {},
              "individually_recorded": {"U1": True, "U2": True}}
    try:
        REAL_STAFF_BRIEFS(packet)
    except ai.AIUnavailable as e:
        assert "prompt" in seen, f"staff_briefs failed before the model call: {e}"
    return seen["prompt"]


def test_prompt_shows_compared_with_shape_example_and_exact_ids(monkeypatch):
    p = _prompt(monkeypatch)
    assert '"compared_with": [{"ref": <housemate ref>' in p
    assert "Worked example of a member comparison" in p and '"compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]' in p
    assert "U2.routine appears ONLY under compared_with, never in U1's fact_ids" in p
    assert "Group-level items (group.*) list everyone's ids in fact_ids and NEVER carry compared_with" in p
    assert "Use fact ids exactly as listed" in p
    for banned in ("sex", "age", "country", "nationality", "language", "@"):
        assert banned not in p.split("Worked example")[1].split("Where gaps go")[0].lower()


def test_validator_unchanged_housemate_id_in_own_fact_ids_still_rejected():
    cat = _cat()
    for v in cat.values():
        if v.get("ref"):
            v["scope"] = "personal"
    clean, problems = facts.check_items([{"fact_ids": ["U1.routine", "U2.routine"], "note": "U1 rises early while U2 stays up late."}], cat, ref="U1",
                                        allow_missing=False, require_own=True, label="U1.tensions", allow_gap=False)
    assert problems and "U2.routine is U2's personal fact, not U1's" in problems[0]
    clean, problems = facts.check_items([{"fact_ids": ["U1.routine"], "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}], "note": "U1 rises early while U2 stays up late."}],
                                        cat, ref="U1", allow_missing=False, require_own=True, label="U1.tensions", allow_gap=False)
    assert not problems and clean[0]["compared_with"]
