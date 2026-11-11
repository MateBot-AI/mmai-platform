"""Recorded apartment access must reach the actual AI descriptors, not just their id lists."""
import json

import pytest

from server import ai, facts
from tests.test_triage_fix1_step_free_gap_id import _packet

REAL_STAFF_BRIEFS = ai.staff_briefs


@pytest.mark.parametrize("value,source", [
    (1, "Synthetic current-stock confirmation"),
    (0, "Synthetic site inspection"),
    (None, None),
    (None, "Synthetic superseded or unknown record"),
])
@pytest.mark.parametrize("stage", ["review", "brief"])
def test_access_value_and_source_reach_model(monkeypatch, value, source, stage):
    packet = _packet()
    packet["apartments"][0].update(step_free_access=value, step_free_source=source)
    if stage == "review":
        desc = ai.review_packet([packet])[0]
        apartment = desc["apartments_in_rule_order"][0]
    else:
        captured = []

        def capture(prompt, **kwargs):
            captured.append(json.loads(prompt.rsplit("\n\n", 1)[-1]))
            raise ai.AIUnavailable("synthetic stop before provider")

        monkeypatch.setattr(ai, "_ask", capture)
        with pytest.raises(ai.AIUnavailable):
            REAL_STAFF_BRIEFS(packet)
        assert len(captured) == 1
        desc = captured[0]
        apartment = desc["apartments_best_first"][0]
    assert "step_free_access" in apartment
    assert apartment["step_free_access"] == value
    assert apartment["step_free_source"] == source
    fact = facts.catalogue(packet)["A74.step_free_access"]
    assert fact["value"] == apartment["step_free_access"]
    recorded = apartment["step_free_access"] is not None
    assert (fact["status"] == "recorded") == recorded
    assert ("A74.step_free_access" in desc["fact_ids"]) == recorded
    assert ("A74.step_free_access" in desc["missing_fact_ids"]) != recorded


def test_no_access_need_does_not_invent_citable_fact():
    packet = _packet("matters")
    packet["apartments"][0].update(step_free_access=1, step_free_source="Synthetic confirmation")
    desc = ai.review_packet([packet])[0]
    assert "A74.step_free_access" not in desc["fact_ids"] + desc["missing_fact_ids"]
