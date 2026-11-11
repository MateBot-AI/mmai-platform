"""T3(d) C4 - the queue side of the native-schema review (10 Nov 2026): budget-aware batch packing (engineering sizing, no business
weight), closed repair telemetry read from ai's meter, and the schema-failure class token. No provider call; ai.py itself is covered
by tests/test_c4_schema_output.py."""
import pytest

from server import ai, ai_queue, matching


def _p(members: int, apts: int = 1) -> dict:
    return {"members_for_ai": [{"ref": f"U{i}"} for i in range(members)], "apartments": [{"apartment_id": i} for i in range(apts)]}


def test_pack_batches_falls_back_to_fixed_size_without_a_budget_function(monkeypatch):
    monkeypatch.setattr(ai, "review_token_estimate", None, raising=False)
    items = [(f"g{i}", _p(2)) for i in range(7)]
    assert [len(b) for b in ai_queue.pack_batches(items, 3)] == [3, 3, 1]
    assert ai_queue.pack_batches([], 3) == [] and [len(b) for b in ai_queue.pack_batches(items, 0)] == [1] * 7


def test_pack_batches_splits_on_the_reply_budget_and_never_drops_or_reorders(monkeypatch):
    # a stand-in budget: 1000 per group + 500 per member; ceiling 8000 -> six 2-person groups (12000) must be split, order kept
    monkeypatch.setattr(ai, "review_token_estimate", lambda groups: sum(1000 + 500 * len(g["members_for_ai"]) for g in groups), raising=False)
    monkeypatch.setattr(ai, "OUTPUT_TOKEN_CEILING", 8000)
    items = [(f"g{i}", _p(2)) for i in range(6)]
    batches = ai_queue.pack_batches(items, 6)
    assert [len(b) for b in batches] == [4, 2] and [g for b in batches for g, _ in b] == [f"g{i}" for i in range(6)]
    assert all(ai.review_token_estimate([p for _, p in b]) <= 8000 for b in batches)
    # a single group over the ceiling still goes (alone): the cap applies to it, never a silent drop
    big = [("big", _p(20))] + items[:2]
    batches = ai_queue.pack_batches(big, 6)
    assert [len(b) for b in batches] == [1, 2] and batches[0][0][0] == "big"
    # the configured batch size is still an upper bound
    assert [len(b) for b in ai_queue.pack_batches([(f"g{i}", _p(1)) for i in range(5)], 2)] == [2, 2, 1]


def test_repair_telemetry_is_counted_once_per_meter_with_closed_kinds_only(monkeypatch):
    saved = dict(ai_queue.STATE)
    try:
        ai_queue.STATE.update({"repairs": 0, "repair_groups": 0, "repair_fixed": 0, "repair_errors": 0, "repair_last_error": None, "repair_last_scope": None})
        monkeypatch.setattr(ai, "ERROR_KINDS", {"schema", "unavailable", "unknown"}, raising=False)
        m = {"asks": 2, "repair": True, "repair_scope": "groups", "repair_groups": 3, "repair_fixed": 2}
        ai_queue._note_repair(m); ai_queue._note_repair(m)                      # the meter is reused across attempts: counted once
        st = ai_queue.STATE
        assert (st["repairs"], st["repair_groups"], st["repair_fixed"], st["repair_errors"], st["repair_last_error"], st["repair_last_scope"]) == (1, 3, 2, 0, None, "groups")
        ai_queue._note_repair({"asks": 1})                                        # no repair: nothing counted
        assert st["repairs"] == 1
        ai_queue._note_repair({"asks": 2, "repair": True, "repair_scope": "batch", "repair_error": "schema"})
        assert st["repairs"] == 2 and st["repair_errors"] == 1 and st["repair_last_error"] == "schema" and st["repair_last_scope"] == "batch"
        ai_queue._note_repair({"asks": 2, "repair": True, "repair_scope": "elsewhere", "repair_error": "Traceback: KeyError('x')"})   # not a closed kind: never stored as text
        assert st["repair_last_error"] == "unknown" and st["repair_last_scope"] == "batch" and st["repair_errors"] == 2
        assert "Traceback" not in str(st)
    finally:
        ai_queue.STATE.clear(); ai_queue.STATE.update(saved)


def test_assess_batch_reads_the_meter_and_the_status_exposes_the_counters(db, monkeypatch):
    saved = dict(ai_queue.STATE)
    try:
        ai_queue.STATE.update({"repairs": 0, "repair_groups": 0, "repair_fixed": 0, "repair_errors": 0, "repair_last_error": None, "repair_last_scope": None})
        def fake_review(batch, meter):
            meter["asks"] = meter.get("asks", 0) + 2
            meter.update({"repair": True, "repair_scope": "groups", "repair_groups": 1, "repair_fixed": 1})
            return [{"index": g["index"], "valid": True} for g in batch]
        monkeypatch.setattr(ai_queue, "_call_review", fake_review)
        res, err, calls = ai_queue._assess_batch([{"index": 0}], attempts=1)
        assert err is None and calls == 2 and ai_queue.STATE["repairs"] == 1 and ai_queue.STATE["repair_fixed"] == 1
        st = ai_queue.status(db)
        assert st["repairs"] == 1 and st["repair_last_scope"] == "groups" and st["repair_last_error"] is None
        def failing_review(batch, meter):
            meter["asks"] = meter.get("asks", 0) + 2
            meter.update({"repair": True, "repair_scope": "batch", "repair_error": "unknown"})
            raise ai.AINoRetry("simulated shape failure 0x99")
        monkeypatch.setattr(ai_queue, "_call_review", failing_review)
        res, err, calls = ai_queue._assess_batch([{"index": 0}], attempts=1)
        assert res is None and err and "0x99" not in err and ai_queue.STATE["repairs"] == 2 and ai_queue.STATE["repair_errors"] == 1
    finally:
        ai_queue.STATE.clear(); ai_queue.STATE.update(saved)


def test_schema_failure_class_is_a_known_run_error_token():
    assert "AISchemaUnsupported" in matching.RUN_ERROR_TYPE_TOKENS
    cls = type("AISchemaUnsupported", (Exception,), {})
    assert matching.run_error_type_token(cls("x")) == "AISchemaUnsupported"


def _packet(people: int, apts: int) -> dict:
    refs = {str(i): f"U{i}" for i in range(people)}
    return {"person_refs": refs, "members_for_ai": [{"ref": f"U{i}"} for i in range(people)], "apartments": [{"apartment_id": i} for i in range(apts)]}


def test_pack_batches_splits_real_oversized_batches_with_the_real_ai_helper():
    """Root's blocker (10 Nov 2026): the capped review_token_budget can never exceed the ceiling, so packing must use the UNCAPPED
    estimate. Six 5-person / one-apartment packets now estimate 10920 > 8000 -> split; the actual
    request budget of each resulting batch is the capped value and stays under the ceiling."""
    est = ai.review_token_estimate   # the real, shared, uncapped formula (C4 v3)
    six = [_packet(5, 1) for _ in range(6)]
    assert est(six) == 10920 and ai.review_token_budget(six) == ai.OUTPUT_TOKEN_CEILING == 8000
    items = [(f"g{i}", p) for i, p in enumerate(six)]
    batches = ai_queue.pack_batches(items, 6)
    assert len(batches) == 2 and [len(b) for b in batches] == [4, 2]
    assert [g for b in batches for g, _ in b] == [f"g{i}" for i in range(6)]
    for b in batches:
        ps = [p for _, p in b]
        assert est(ps) <= 8000 and ai.review_token_budget(ps) == est(ps) < 8000
    # Six 2-person groups with three apartments now split before reaching the ceiling.
    typical = [(f"g{i}", _packet(2, 3)) for i in range(6)]
    assert [len(b) for b in ai_queue.pack_batches(typical, 6)] == [4, 2] and est([p for _, p in typical]) == 10320
    # one group alone can still cap: it goes alone, never dropped
    assert [len(b) for b in ai_queue.pack_batches([("huge", _packet(40, 1))] + typical[:1], 6)] == [1, 1]
