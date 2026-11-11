"""Staff brief: explicit response schema + ONE exact-validator-feedback repair turn (9 Nov 2026, neutral review builder; proposal).

Offline only: every model call is a scripted `ai._ask` double; no provider. The validator (parse_staff_briefs / facts.check_items)
is unchanged - these tests prove the call shape and accounting: valid first reply -> 0 repair calls; invalid -> valid once -> 2 calls,
meter marks the repair; invalid -> invalid -> terminal failure with the after-repair code, exactly 2 calls, never a third; repair cut
off or provider failure -> terminal, no success; an unusable COMPACT reply stays terminal with no repair (existing behaviour); and
the feedback block carries the validator's exact problems and the parsed previous reply only - no names, e-mails or phone numbers.
"""
import json

import pytest

from server import ai, facts
from tests.conftest import fake_staff_briefs

REAL_STAFF_BRIEFS = ai.__dict__["staff_briefs"]   # bound at import, before the conftest autouse double replaces it per test

PACKET = {"members_for_ai": [{"ref": "U1", "routine": "Early riser", "tidy": "Very tidy", "social": "Quiet", "doing": "Studying"},
                             {"ref": "U2", "routine": "Night owl", "tidy": "Relaxed", "social": "Sociable", "doing": "Working"}],
          "person_refs": {}, "apartments": [], "review": {}, "individually_recorded": {"U1": True, "U2": True}}
REFS = ["U1", "U2"]
IDENTITY = ("Jane Smith", "jane@example.com", "0400 000 000")


def _good():
    g = fake_staff_briefs(PACKET)
    return {"group": {k: g["group"][k] for k in ai.BRIEF_GROUP_KEYS}, "members": [{k: m[k] for k in ("ref",) + ai.BRIEF_MEMBER_KEYS} for m in g["members"]]}


def _bad_ownership():
    """U1's strengths cite U2's routine: the live-1 defect class (housemate id in own fact_ids)."""
    out = _good()
    out["members"][0]["strengths"] = [{"fact_ids": ["U2.routine"], "note": "U1 is an early riser"}]
    return out


def _script(monkeypatch, replies):
    """`replies`: list of callables or strings/dicts served in order to ai._ask; records (max_tokens, prompt) per call."""
    calls = []

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        calls.append({"max_tokens": max_tokens, "prompt": prompt})
        r = replies[len(calls) - 1]
        if callable(r):
            return r(prompt)
        return r if isinstance(r, str) else json.dumps(r)
    monkeypatch.setattr(ai, "_ask", _ask)
    return calls


def test_schema_text_is_built_from_the_validator_keys():
    assert set(ai.BRIEF_RESPONSE_SCHEMA["group"]) == set(ai.BRIEF_GROUP_KEYS)
    assert set(ai.BRIEF_RESPONSE_SCHEMA["members"][0]) == {"ref"} | set(ai.BRIEF_MEMBER_KEYS)
    assert "compared_with?" in ai.BRIEF_NOTE_SCHEMA and "compared_with" not in ai.BRIEF_RESPONSE_SCHEMA["group"]
    for k in ai.BRIEF_GROUP_KEYS + ai.BRIEF_MEMBER_KEYS:
        assert f'"{k}"' in ai.BRIEF_SCHEMA_TEXT
    assert "group notes never carry it" in ai.BRIEF_SCHEMA_TEXT and "exactly one object per listed ref" in ai.BRIEF_SCHEMA_TEXT


def test_valid_first_reply_means_zero_repair_calls(monkeypatch):
    calls = _script(monkeypatch, [_good()])
    meter = {"asks": 0}
    res = REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert [m["ref"] for m in res["members"]] == REFS
    assert len(calls) == 1 and meter["asks"] == 1 and meter.get("repair") is None and meter["turns"] == ["first"]
    p = calls[0]["prompt"]
    assert ai.BRIEF_SCHEMA_TEXT.strip() in p and ai.BRIEF_REPAIR_HEAD not in p
    assert "Refs listed for members (exactly one member object each): U1, U2." in p
    json.loads(p.rsplit("\n\n", 1)[1])   # the packet descriptor is still the last blank-line block (offline replay keys on it)


def test_invalid_then_valid_repairs_once_with_exact_validator_feedback(monkeypatch):
    try:
        ai.parse_staff_briefs(json.loads(json.dumps(_bad_ownership())), REFS, PACKET)
    except ai.AIUnusableReply as e:
        expected_problems = e.problems
    assert expected_problems and any("U2.routine is U2's personal fact, not U1's" in p for p in expected_problems)
    calls = _script(monkeypatch, [_bad_ownership(), _good()])
    meter = {"asks": 0}
    res = REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert [m["ref"] for m in res["members"]] == REFS
    assert len(calls) == 2 and meter["asks"] == 2 and meter["repair"] is True and meter["turns"] == ["first", "repair"]
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"] == ai.brief_token_budget(2)   # same budget, no enlargement
    p2 = calls[1]["prompt"]
    assert ai.BRIEF_REPAIR_HEAD in p2 and "Your previous reply was cut off" not in p2
    for i, prob in enumerate(expected_problems, 1):
        assert f"{i}. {prob}" in p2          # verbatim, untruncated
    assert '"fact_ids": ["U2.routine"]' in p2   # the parsed previous reply travels with the feedback
    assert json.loads(p2.rsplit("\n\n", 1)[1]) == json.loads(calls[0]["prompt"].rsplit("\n\n", 1)[1])   # same descriptor, last block


def test_invalid_then_invalid_is_terminal_with_exactly_two_calls(monkeypatch):
    calls = _script(monkeypatch, [_bad_ownership(), _bad_ownership(), _good()])   # a third reply is never requested
    meter = {"asks": 0}
    with pytest.raises(ai.AIUnusableReply) as ex:
        REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert ex.value.code == "brief_unusable_after_repair" and ex.value.problems
    assert ai.failure_text(ex.value, "brief") == ai.FAILURE_TEXT["brief_unusable_after_repair"]
    assert len(calls) == 2 and meter["asks"] == 2 and meter["turns"] == ["first", "repair"]
    assert ai.BRIEF_REPAIR_HEAD not in calls[0]["prompt"] and ai.BRIEF_REPAIR_HEAD in calls[1]["prompt"]


def test_shape_failure_is_also_repairable_once(monkeypatch):
    calls = _script(monkeypatch, [{"members": []}, _good()])
    res = REAL_STAFF_BRIEFS(PACKET, meter={"asks": 0})
    assert len(calls) == 2 and res["group"] and "1. reply is not an object with a group object and a members list" in calls[1]["prompt"]


def test_no_json_or_malformed_json_is_not_repaired(monkeypatch):
    for text in ("I cannot help with that.", '{"group": {"strengths": [}'):
        calls = _script(monkeypatch, [text, _good()])
        with pytest.raises(ai.AIUnusableReply) as ex:
            REAL_STAFF_BRIEFS(PACKET, meter={"asks": 0})
        assert len(calls) == 1 and ex.value.code is None and not ex.value.problems


def test_repair_reply_cut_off_is_terminal_no_success(monkeypatch):
    def cut(prompt):
        raise ai.AIOutputTruncated("cut", max_tokens=3600)
    calls = _script(monkeypatch, [_bad_ownership(), cut])
    meter = {"asks": 0}
    with pytest.raises(ai.AINoRetry) as ex:
        REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert ex.value.code == "brief_repair_truncated" and len(calls) == 2 and meter["asks"] == 2 and meter["turns"] == ["first", "repair"]
    assert ai.failure_text(ex.value, "brief") == ai.FAILURE_TEXT["brief_repair_truncated"]


def test_provider_failure_on_repair_is_terminal_no_success(monkeypatch):
    def down(prompt):
        raise ai.AIUnavailable("AI service call failed", kind="provider", code="provider")
    calls = _script(monkeypatch, [_bad_ownership(), down])
    meter = {"asks": 0}
    with pytest.raises(ai.AINoRetry) as ex:
        REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert ex.value.code == "brief_repair_failed" and len(calls) == 2 and meter["asks"] == 2
    assert ai.failure_text(ex.value, "brief") == ai.FAILURE_TEXT["brief_repair_failed"]
    # provider down on the FIRST call: unchanged - no repair, no success, one call; the attempted turn is still recorded
    calls = _script(monkeypatch, [down, _good()])
    meter = {"asks": 0}
    with pytest.raises(ai.AIUnavailable) as ex:
        REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert not isinstance(ex.value, ai.AIUnusableReply) and len(calls) == 1 and meter["asks"] == 1 and meter["turns"] == ["first"]


def test_turn_labels_are_recorded_for_attempted_calls_that_fail(monkeypatch):
    """Rev2 (root): asks and turn labels must agree even when the bounded pair fails - first cut off then compact cut off, and
    first cut off then compact provider failure, both show ['first', 'compact'] with asks == 2 and no repair."""
    def cut(prompt):
        raise ai.AIOutputTruncated("cut", max_tokens=3600)

    def down(prompt):
        raise ai.AIUnavailable("AI service call failed", kind="provider", code="provider")
    for second, code in ((cut, "size_limit"), (down, "size_limit_compact_failed")):
        calls = _script(monkeypatch, [cut, second, _good()])
        meter = {"asks": 0}
        with pytest.raises(ai.AINoRetry) as ex:
            REAL_STAFF_BRIEFS(PACKET, meter=meter)
        assert ex.value.code == code and len(calls) == 2 and meter["asks"] == 2 and meter["turns"] == ["first", "compact"] and meter.get("repair") is None
    # one cut-off then a valid compact reply: ['first', 'compact'], 2 asks (unchanged outcome)
    calls = _script(monkeypatch, [cut, _good()])
    meter = {"asks": 0}
    REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert meter["asks"] == len(meter["turns"]) == 2


def test_unusable_compact_reply_stays_terminal_without_repair(monkeypatch):
    """Existing bounded-output rule preserved: cut-off -> one compact retry; an unusable compact reply is terminal (AINoRetry,
    size_limit_compact_unusable) and gets NO repair turn - the two-call pair is never extended to three."""
    def cut(prompt):
        raise ai.AIOutputTruncated("cut", max_tokens=3600)
    calls = _script(monkeypatch, [cut, _bad_ownership(), _good()])
    meter = {"asks": 0}
    with pytest.raises(ai.AINoRetry) as ex:
        REAL_STAFF_BRIEFS(PACKET, meter=meter)
    assert ex.value.code == "size_limit_compact_unusable" and len(calls) == 2 and meter["asks"] == 2
    assert meter["turns"] == ["first", "compact"] and meter.get("repair") is None
    assert "Your previous reply was cut off" in calls[1]["prompt"] and ai.BRIEF_REPAIR_HEAD not in calls[1]["prompt"]
    # cut-off then a VALID compact reply still succeeds in two calls (unchanged)
    calls = _script(monkeypatch, [cut, _good()])
    meter = {"asks": 0}
    assert REAL_STAFF_BRIEFS(PACKET, meter=meter)["group"] and meter["asks"] == 2 and meter["turns"] == ["first", "compact"]


def test_repair_feedback_echoes_only_schema_keys_canaries_outside_and_inside_json(monkeypatch):
    """The feedback block is the validator's problems plus the previous reply PROJECTED onto the schema's known keys: prose outside
    the JSON is dropped, and so are unknown fields the model added at any level (top-level, member, note, compared_with). Canaries
    are planted in each place. Scope of the claim (root rev2): structural - a canary written INSIDE a note sentence is kept, because
    the validator's problems refer to those sentences; this is not a semantic proof that note text carries no personal detail."""
    bad = _bad_ownership()
    bad["private_staff_notes"] = "Jane Smith is the lead; call 0400 000 000"
    bad["person_refs"] = {"U1": "jane@example.com"}
    bad["members"][0]["name"] = "Jane Smith"
    bad["members"][0]["contact"] = "0400 000 000"
    bad["members"][0]["strengths"][0]["staff_only"] = "jane@example.com"
    bad["members"][0]["strengths"][0]["compared_with"] = [{"ref": "U2", "fact_ids": ["U2.routine"], "name": "Jane Smith"}]
    bad["group"]["extra_list"] = [{"fact_ids": ["U1.routine"], "note": "Jane Smith 0400 000 000"}]
    raw = ("Note for staff: Jane Smith (jane@example.com, 0400 000 000) is lovely.\n" + json.dumps(bad) + "\nCall Jane Smith.")
    calls = _script(monkeypatch, [raw, _good()])
    REAL_STAFF_BRIEFS(PACKET, meter={"asks": 0})
    p2 = calls[1]["prompt"]
    feedback = p2.split(ai.BRIEF_REPAIR_HEAD, 1)[1].rsplit("\n\n", 1)[0]
    for s in IDENTITY:
        assert s not in feedback and s not in p2
    assert "0400" not in p2 and "@" not in feedback and "private_staff_notes" not in p2 and "extra_list" not in p2 and "staff_only" not in p2
    assert '"compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]' in feedback   # known compared_with keys survive, the extra key does not
    # the problems quoted are exactly the validator's own strings (fact ids, refs, labels), nothing from the reply's prose
    prob_lines = [ln for ln in feedback.splitlines() if ln[:2] in ("1.", "2.", "3.")]
    assert prob_lines and all(("U1" in ln or "U2" in ln or "group" in ln) and "lovely" not in ln for ln in prob_lines)
    # explicit limitation: a canary INSIDE a note sentence is kept by the projection (known note content is fed back as written)
    inside = _bad_ownership()
    inside["members"][0]["strengths"][0]["note"] = "U1 is an early riser like Jane Smith"
    calls = _script(monkeypatch, [inside, _good()])
    REAL_STAFF_BRIEFS(PACKET, meter={"asks": 0})
    assert "Jane Smith" in calls[1]["prompt"].split(ai.BRIEF_REPAIR_HEAD, 1)[1]


def test_reply_projection_keeps_known_keys_only():
    proj = ai.brief_reply_projection({"group": {"strengths": [{"fact_ids": ["U1.routine", 7], "note": "n", "kind": "evidence", "x": 1}], "bogus": []},
                                      "members": [{"ref": "U1", "name": "Jane", "tensions": [{"fact_ids": ["U1.tidy"], "note": "t", "compared_with": [{"ref": "U2", "fact_ids": ["U2.tidy"], "z": 2}, "junk"]}]}, "junk"],
                                      "private": "x"})
    assert proj == {"group": {"strengths": [{"fact_ids": ["U1.routine"], "note": "n", "kind": "evidence"}]},
                    "members": [{"ref": "U1", "tensions": [{"fact_ids": ["U1.tidy"], "note": "t", "compared_with": [{"ref": "U2", "fact_ids": ["U2.tidy"]}]}]}]}
    assert ai.brief_reply_projection("not a dict") == {"group": {}, "members": []}


def test_validator_itself_is_unchanged_on_the_repaired_shape():
    """The accepted result of a repair is the same parse the first reply would have got: ownership, caps and coverage still reject."""
    with pytest.raises(ai.AIUnusableReply) as ex:
        ai.parse_staff_briefs(json.loads(json.dumps(_bad_ownership())), REFS, PACKET)
    assert ex.value.code == "brief_unusable" and ai.failure_text(ex.value, "brief") == ai.FAILURE_TEXT["brief_unusable"]
    assert str(ex.value).startswith("staff brief unusable: ") and len(str(ex.value)) <= len("staff brief unusable: ") + 380
    over = _good()
    over["group"]["strengths"] = over["group"]["strengths"] * (ai.BRIEF_GROUP_ITEMS_MAX + 1)
    with pytest.raises(ai.AIUnusableReply):
        ai.parse_staff_briefs(over, REFS, PACKET)
    ok = ai.parse_staff_briefs(_good(), REFS, PACKET)
    assert ok["group"]["coverage"] == facts.coverage(PACKET)
