"""T5 failure-sink provenance, root ruling "Modified A" (9 Nov 2026). Every FAILURE write to groups.ai_error, a 'Staff brief failed: '
held_reason, group_briefs.error or review_rounds.invalid_reason is a closed plain sentence chosen by the exception's closed kind /
code; provider prose, Python exception text and our own validator sentences (which quote the reply) never reach a row or the log.
Valid model rationale (hold / revise reasons, apartment reasons, review_json) is intentional generated text under retention and is
not touched. Synthetic data, isolated SQLite, model doubles only; no provider traffic."""
import json
import logging
import re

import pytest

from server import ai, ai_queue, matching
from server.db import row, rows
from tests.test_t6_fail_closed import _run, _groups, _reply_builder, _good
from tests.test_t6_grounding import _run_and_approve
from tests.test_data_and_api import appdb, client  # noqa: F401  (fixtures re-used)

MARKER = "PRIVATE_FAILURE_TOKEN_Zq"


def _sinks(db) -> str:
    return json.dumps({
        "groups": rows(db.execute("SELECT ai_error, held_reason FROM groups")),
        "briefs": rows(db.execute("SELECT error FROM group_briefs")),
        "reviews": rows(db.execute("SELECT invalid_reason, review_json FROM review_rounds")),
        "state": dict(ai_queue.STATE),
    }, default=str)


# ---------------------------------------------------------------- the closed vocabulary itself
def test_generic_failures_are_worded_per_step_and_never_deny_a_completed_review():
    """Root read (9 Nov 2026): the generic provider/setup/credentials/unexpected sentences said 'the group was not assessed' even
    for brief / introduction failures after a valid review. Each step has its own closed outcome clause; all variants are closed."""
    e = ai.AIUnavailable("Connection error " + MARKER)
    assert ai.failure_text(e) == ai.failure_text(e, "review") == ai.FAILURE_TEXT["provider"]
    assert ai.failure_text(e, "review").endswith("the group was not assessed.")
    assert "not assessed" not in ai.failure_text(e, "brief") and ai.failure_text(e, "brief").endswith("the recorded review stands.")
    assert "not assessed" not in ai.failure_text(e, "introduction") and "staff brief stand" in ai.failure_text(e, "introduction")
    assert ai.failure_text(e, "not-a-step") == ai.FAILURE_TEXT["provider"]          # unknown step falls back to the review wording
    for step in ai.FAILURE_STEPS:
        for kind in ("provider", "setup", "credentials", "unexpected"):
            t = ai.failure_text(ai.AIUnavailable(MARKER, kind=kind), step)
            assert MARKER not in t and t in ai.ALL_FAILURE_SENTENCES and ai_queue.is_closed_failure_text(t)
            assert ai_queue.is_closed_failure_text("KeyError: " + t)
    # a coded failure (validator / size limit / packet) is step-specific already and unchanged by `step`
    coded = ai.AIUnusableReply(MARKER, code="brief_unusable")
    assert ai.failure_text(coded, "brief") == ai.failure_text(coded, "review") == ai.FAILURE_TEXT["brief_unusable"]


def test_failure_text_is_a_closed_set_of_plain_sentences():
    assert set(ai.FAILURE_TEXT) >= ai.ERROR_KINDS
    for code, text in ai.FAILURE_TEXT.items():
        assert text and text[0].isupper() and text.endswith(".") and "{" not in text and "%" not in text, code
    assert ai.failure_text(ai.AIUnavailable("provider said " + MARKER)) == ai.FAILURE_TEXT["provider"]
    assert ai.failure_text(ai.AIUnusableReply("validator quoted " + MARKER)) == ai.FAILURE_TEXT["validation"]
    assert ai.failure_text(ai.AIUnavailable(MARKER, kind="credentials", code="credentials")) == ai.FAILURE_TEXT["credentials"]
    assert ai.failure_text(ai.AIUnavailable(MARKER, kind="setup", code="setup")) == ai.FAILURE_TEXT["setup"]
    assert ai.failure_text(ai.AIOutputTruncated(MARKER, max_tokens=9, attempts=2)) == ai.FAILURE_TEXT["size_limit"]
    assert ai.failure_text(ai.AINoRetry(MARKER, code="size_limit_compact_unusable")) == ai.FAILURE_TEXT["size_limit_compact_unusable"]
    assert ai.failure_text(KeyError(MARKER)) == ai.FAILURE_TEXT["unexpected"]
    assert ai.failure_text(ai.AIUnavailable(MARKER, kind="not-a-kind", code="not-a-code")) == ai.FAILURE_TEXT["unexpected"]   # unknown kind -> unexpected
    for e in (ai.AIUnavailable(MARKER), ai.AIUnusableReply(MARKER), KeyError(MARKER)):
        assert MARKER not in ai.failure_text(e)


def test_queue_failure_text_carries_closed_metadata_and_a_closed_token_for_foreign_exceptions(caplog):
    caplog.set_level(logging.WARNING)
    t = ai_queue._failure(ai.AIUnavailable(MARKER, kind="credentials", code="credentials"))
    assert isinstance(t, str) and t == ai.FAILURE_TEXT["credentials"] and t.kind == "credentials" and t.code == "credentials"
    u = ai_queue._failure(type(MARKER, (RuntimeError,), {})("x " + MARKER))
    assert u == "unknown: " + ai.FAILURE_TEXT["unexpected"] and u.kind == "unexpected"
    k = ai_queue._failure(KeyError(MARKER))
    assert k == "KeyError: " + ai.FAILURE_TEXT["unexpected"]
    assert MARKER not in caplog.text and "unknown" in caplog.text
    assert ai_queue.is_closed_failure_text(t) and ai_queue.is_closed_failure_text(u) and ai_queue.is_closed_failure_text(k)
    assert ai_queue.is_closed_failure_text(ai.FAILURE_TEXT["review_unusable"] + " (3 problems found)")
    assert ai_queue.is_closed_failure_text(ai_queue.OFFER_DRIFT_REASON) and ai_queue.is_closed_failure_text(None)
    assert not ai_queue.is_closed_failure_text("Model reply unusable: it discussed Victoria without evidence")
    assert not ai_queue.is_closed_failure_text(MARKER + ": " + ai.FAILURE_TEXT["unexpected"])        # arbitrary token prefix is not closed


def test_retry_and_category_are_decided_by_trusted_kind_never_by_words():
    assert ai_queue._no_point_retrying(ai.AIUnavailable("x", kind="credentials"))
    assert ai_queue._no_point_retrying(ai.AIUnavailable("x", kind="setup"))
    assert not ai_queue._no_point_retrying(ai.AIUnavailable("No LLM credentials authentication_error Error code: 401"))
    assert not ai_queue._no_point_retrying(RuntimeError("No AI credentials"))
    assert ai_queue.failure_category(ai.AIUnavailable("x", kind="credentials")) == ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS
    assert ai_queue.failure_category(ai.AIUnavailable("No LLM credentials " + MARKER)) == ai_queue.STATE_ERROR_PROVIDER
    assert ai_queue.failure_category(ai.AIUnusableReply(MARKER)) == ai_queue.STATE_ERROR_PROVIDER
    assert ai_queue.failure_category(type(MARKER, (RuntimeError,), {})("x")) == "UNEXPECTED:unknown"
    assert ai_queue.failure_category(TimeoutError(MARKER)) == "UNEXPECTED:TimeoutError"


# ---------------------------------------------------------------- the provider's words go nowhere, not even the log
def test_provider_exception_words_reach_neither_row_nor_log(db, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _, run = _run(db)

    class _Messages:
        def create(self, **kw):
            raise RuntimeError("Error code: 401 - invalid x-api-key " + MARKER)

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(ai, "_client", lambda: _Client())
    monkeypatch.setattr(ai_queue.time, "sleep", lambda _: None)
    ai_queue.process_pending(db, batch_size=100, workers=1, attempts=2)
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "failed" and g["ai_error"] == ai.FAILURE_TEXT["provider"] for g in gs)
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER
    assert MARKER not in _sinks(db) and MARKER not in caplog.text and "x-api-key" not in caplog.text
    assert "AI call failed: provider" in caplog.text


def test_validator_sentences_stay_in_process_persisted_reason_is_code_plus_count(db, monkeypatch, caplog):
    """The validator's diagnosis (which quotes the reply, here a fabricated fact id) is available to the parser's caller and to unit
    tests, but review_rounds.invalid_reason / groups.ai_error / review_json hold only the closed sentence and a count."""
    caplog.set_level(logging.WARNING)
    _, run = _run(db)
    seen, real_parse = [], ai.parse_review_batch
    monkeypatch.setattr(ai, "parse_review_batch", lambda out, groups: seen.extend(real_parse(out, groups)) or seen[-len(groups):])
    monkeypatch.setattr(ai, "_ask", _reply_builder(lambda g: {**_good(g), "rating": 42, "findings": [{"fact_ids": [MARKER], "note": "A supplied fact."}]}))
    monkeypatch.setattr(ai_queue.time, "sleep", lambda _: None)
    ai_queue.process_pending(db, batch_size=100, workers=1, attempts=1)
    assert seen and all(not r["valid"] and MARKER in r["invalid_reason"] and "not a number in 1-10" in r["invalid_reason"]
                        and r["invalid_code"] == "review_unusable" and r["invalid_count"] >= 2 for r in seen)
    gs = _groups(db, run["run_id"])
    for g in gs:
        assert g["ai_status"] == "failed"
        assert re.fullmatch(re.escape(ai.FAILURE_TEXT["review_unusable"]) + r" \((\d+) problems found\)", g["ai_error"]), g["ai_error"]
        rr = rows(db.execute("SELECT valid, invalid_reason, review_json FROM review_rounds WHERE group_id=?", (g["id"],)))
        assert rr and rr[0]["valid"] == 0 and rr[0]["invalid_reason"] == g["ai_error"]
        kept = json.loads(rr[0]["review_json"])
        assert "invalid_reason" not in kept and kept["invalid_code"] == "review_unusable" and kept["invalid_count"] >= 2
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_missing_and_duplicate_review_objects_persist_their_own_closed_sentences(db, monkeypatch):
    _, run = _run(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda _: None)

    def duplicate_first(prompt, model=None, max_tokens=1500, system=""):
        gs = json.loads(prompt[prompt.rindex("\n\n") + 2:])
        out = [_good(g) for g in gs]
        if len(out) > 1:
            out[1]["index"] = out[0]["index"]        # one group reviewed twice, another not at all
        return json.dumps(out)

    monkeypatch.setattr(ai, "_ask", duplicate_first)
    ai_queue.process_pending(db, batch_size=100, workers=1, attempts=1)
    errs = {g["ai_error"] for g in _groups(db, run["run_id"]) if g["ai_status"] == "failed"}
    assert errs and errs <= {ai.FAILURE_TEXT["review_missing"], ai.FAILURE_TEXT["review_duplicate"]}


def test_staff_brief_failures_persist_closed_sentences_with_the_hold_prefix(db, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _, _, group = _run_and_approve(db, monkeypatch)
    for exc, expect in ((ai.AIUnusableReply("staff brief unusable: U1.strengths[0]: " + MARKER, code="brief_unusable"), ai.FAILURE_TEXT["brief_unusable"]),
                        # generic failures are worded for the BRIEF step: they never say the group was not assessed (the review did complete)
                        (ai.AIUnavailable("Connection error " + MARKER), ai.STEP_FAILURE_TEXT["brief"]["provider"]),
                        (KeyError(MARKER), "KeyError: " + ai.STEP_FAILURE_TEXT["brief"]["unexpected"])):
        db.execute("UPDATE groups SET brief_status='pending', held_reason=NULL WHERE id=?", (group["id"],))
        db.execute("DELETE FROM group_briefs WHERE group_id=?", (group["id"],))
        db.commit()
        monkeypatch.setattr(ai, "staff_briefs", lambda *a, **k: (_ for _ in ()).throw(exc))
        monkeypatch.setattr(ai_queue.time, "sleep", lambda _: None)
        ai_queue.process_briefs(db, attempts=2)
        g = row(db.execute("SELECT brief_status, held_reason, ready_at FROM groups WHERE id=?", (group["id"],)))
        assert g["brief_status"] == "failed" and g["ready_at"] is None and g["held_reason"] == ai_queue.BRIEF_FAILED_PREFIX + expect
        b = rows(db.execute("SELECT status, error FROM group_briefs WHERE group_id=?", (group["id"],)))
        assert b and all(x["status"] == "failed" and x["error"] == expect for x in b)
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_bounded_output_terminal_failures_are_closed_sentences(db, monkeypatch):
    """A second cut-off and an unusable / failed compact retry persist the fixed size-limit sentences (the phrases staff_view keys on)."""
    _, run = _run(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda _: None)
    monkeypatch.setattr(ai, "_ask_bounded", lambda *a, **k: (_ for _ in ()).throw(ai.AIOutputTruncated("review exceeded " + MARKER, max_tokens=5, attempts=2)))
    ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "failed" and g["ai_error"] == ai.FAILURE_TEXT["size_limit"] for g in gs)
    assert "exceeded the response size limit after a compact retry" in ai.FAILURE_TEXT["size_limit"]
    assert "compact reply after a cut-off reply was unusable" in ai.FAILURE_TEXT["size_limit_compact_unusable"]
    assert MARKER not in _sinks(db)


def test_candidate_option_ai_error_is_closed(monkeypatch):
    """matching's replacement / refill option ranking reports a closed sentence when the candidate review fails."""
    monkeypatch.setattr(ai, "assess_groups", lambda *a, **k: (_ for _ in ()).throw(ai.AIUnavailable("provider " + MARKER)))
    src = open(matching.__file__).read()
    assert "ai_error = str(e)" not in src and src.count("ai_error = ai.failure_text(e)") == 2


# ---------------------------------------------------------------- legacy rows: bounded projection, idempotent, distinct from the read mask
# what pre-Modified-A _apply_result stored in review_json: the whole in-process result INCLUDING the validator's diagnostic (quoting the reply)
LEGACY_REVIEW_JSON = {"index": 0, "rating": 7, "rationale": "fine so far", "verdict": "approve", "findings": [{"fact_ids": ["F1"], "note": "fine"}],
                      "apartments": [{"apartment_id": 1, "reason": "r"}], "valid": False, "invalid_reason": f"findings[0]: {MARKER} is not recorded"}


def _plant_legacy(db, gid):
    db.execute("UPDATE groups SET ai_status='failed', ai_error=?, brief_status='failed', held_reason=? WHERE id=?",
               (f"AIUnavailable: review missing for index 0 ({MARKER})", f"Staff brief failed: staff brief unusable: U1.strengths[0]: {MARKER}", gid))
    db.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at, refs_json)
                  VALUES (?,0,'group',NULL,NULL,NULL,NULL,'failed',?, 'm', 1, 'h', '2026-11-01T00:00:00+00:00', '{}')""", (gid, f"staff brief unusable: {MARKER}"))
    db.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at, refs_json)
                  VALUES (?,0,NULL,NULL,'','[]',?,0,?, 'm', 1, 'h', '2026-11-01T00:00:00+00:00', '{}')""",
               (gid, json.dumps(LEGACY_REVIEW_JSON), f"findings[0]: {MARKER} is not recorded"))
    db.commit()


def test_legacy_failure_text_projection_is_bounded_idempotent_and_leaves_intentional_text(db, monkeypatch):
    _, run = _run(db)
    gs = _groups(db, run["run_id"])
    legacy, intact, held = gs[0], gs[1], gs[2] if len(gs) > 2 else gs[1]
    _plant_legacy(db, legacy["id"])
    # already-closed failure rows and intentional generated text are NOT legacy
    db.execute("UPDATE groups SET ai_status='failed', ai_error=? WHERE id=?", (ai.FAILURE_TEXT["provider"], intact["id"]))
    db.execute("UPDATE groups SET held_reason=? WHERE id=?", ("AI review: hold. Oscar and Nia keep different hours (" + MARKER + ")", held["id"]))
    db.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=?", ("quiet street suits the early risers " + MARKER, held["id"]))
    db.commit()
    before = _sinks(db)
    assert MARKER in before
    counts = ai_queue.neutralise_legacy_failure_text(db, commit=True)
    assert counts == {"groups.ai_error": 1, "groups.held_reason": 1, "group_briefs.error": 1, "review_rounds.invalid_reason": 1,
                      "review_rounds.review_json.invalid_reason": 1}
    g = row(db.execute("SELECT ai_error, held_reason FROM groups WHERE id=?", (legacy["id"],)))
    assert g["ai_error"] == ai.LEGACY_FAILURE_TEXT and g["held_reason"] == ai_queue.BRIEF_FAILED_PREFIX + ai.LEGACY_FAILURE_TEXT
    assert row(db.execute("SELECT error FROM group_briefs WHERE group_id=?", (legacy["id"],)))["error"] == ai.LEGACY_FAILURE_TEXT
    rr = row(db.execute("SELECT invalid_reason, review_json FROM review_rounds WHERE group_id=?", (legacy["id"],)))
    assert rr["invalid_reason"] == ai.LEGACY_FAILURE_TEXT
    # the duplicated diagnostic key is gone from review_json; every other key of the stored review is kept byte-for-byte
    assert json.loads(rr["review_json"]) == {k: v for k, v in LEGACY_REVIEW_JSON.items() if k != "invalid_reason"}
    assert MARKER not in rr["review_json"]
    assert row(db.execute("SELECT ai_error FROM groups WHERE id=?", (intact["id"],)))["ai_error"] == ai.FAILURE_TEXT["provider"]
    h = row(db.execute("SELECT held_reason FROM groups WHERE id=?", (held["id"],)))
    assert h["held_reason"].startswith("AI review: hold. ") and MARKER in h["held_reason"]              # intentional rationale untouched
    assert MARKER in (row(db.execute("SELECT ai_reason FROM group_apartments WHERE group_id=?", (held["id"],)))["ai_reason"] or "")
    # failure sinks are clean; a second run is a no-op
    failure_only = json.dumps({"ai_error": rows(db.execute("SELECT ai_error FROM groups")), "brief": rows(db.execute("SELECT error FROM group_briefs")),
                               "inv": rows(db.execute("SELECT invalid_reason, review_json FROM review_rounds")),
                               "brief_holds": rows(db.execute("SELECT held_reason FROM groups WHERE held_reason LIKE 'Staff brief failed: %'"))})
    assert MARKER not in failure_only
    again = _sinks(db)
    assert ai_queue.neutralise_legacy_failure_text(db, commit=True) == {k: 0 for k in counts} and _sinks(db) == again


def test_read_mask_is_not_the_cleanup(client):
    """staff_view's plain presentation hides legacy text at read time without changing rows (test_t10 proves the hiding); the
    projection is the only thing that changes rows, and it is explicit and counted."""
    from tests.test_t10_plain_presentation import _plant
    o, n, c, gid, refs = _plant(client)
    c.execute("UPDATE groups SET ai_status='failed', ai_error=?, ai_attempts=2 WHERE id=?", (f"AIUnavailable: review missing ({MARKER})", gid))
    c.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert MARKER not in json.dumps(d)                                                          # masked for staff ...
    assert MARKER in row(c.execute("SELECT ai_error FROM groups WHERE id=?", (gid,)))["ai_error"]  # ... but the row is unchanged
    assert ai_queue.neutralise_legacy_failure_text(c, commit=True)["groups.ai_error"] == 1
    assert row(c.execute("SELECT ai_error FROM groups WHERE id=?", (gid,)))["ai_error"] == ai.LEGACY_FAILURE_TEXT
    assert MARKER not in json.dumps(client.get(f"/api/groups/{gid}").json())


def test_pre_queue_history_sentence_is_closed_and_fresh_review_json_never_duplicates_the_diagnostic(db, monkeypatch):
    """requeue_interrupted's round-0 history row carries a fixed code-authored sentence: the legacy projection must leave it alone.
    Reviews written by the current _apply_result store the result WITHOUT the in-process `invalid_reason`, so the projection is a no-op."""
    _, run = _run(db)
    gid = _groups(db, run["run_id"])[0]["id"]
    db.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at)
                  VALUES (?,0,NULL,5,'older text',NULL,NULL,0,?, 'm', 1, NULL, '2026-11-01T00:00:00+00:00')""", (gid, ai_queue.PRE_QUEUE_SUPERSEDED_TEXT))
    db.commit()
    assert ai_queue.is_closed_failure_text(ai_queue.PRE_QUEUE_SUPERSEDED_TEXT)

    def bad_note(g):
        item = _good(g)
        item["findings"] = [{"fact_ids": g["fact_ids"][:1], "note": f"{MARKER} wakes early"}]
        return item
    monkeypatch.setattr(ai, "_ask", _reply_builder(bad_note))
    ai_queue.process_pending(db, batch_size=100, workers=1, attempts=1)
    stored = rows(db.execute("SELECT review_json, invalid_reason FROM review_rounds WHERE review_json IS NOT NULL"))
    assert stored
    for r in stored:
        assert "invalid_reason" not in json.loads(r["review_json"])
        assert ai_queue.is_closed_failure_text(r["invalid_reason"])
    assert ai_queue.neutralise_legacy_failure_text(db, commit=True) == {"groups.ai_error": 0, "groups.held_reason": 0, "group_briefs.error": 0,
                                                                        "review_rounds.invalid_reason": 0, "review_rounds.review_json.invalid_reason": 0}


def test_legacy_review_json_projection_scope_unreadable_and_non_object_rows_are_left_byte_for_byte(db):
    """Explicit scope of the review_json projection: only a JSON object whose `invalid_reason` carries diagnostic content loses
    that one key. Unreadable JSON, readable non-object JSON (list / string), objects without the key, and healthy objects with
    `invalid_reason` null / empty / already closed are left byte-for-byte (root ruling, 9 Nov 2026), even when the bytes contain
    the words 'invalid_reason' - review_json is a retained record, not a failure sink."""
    _, run = _run(db)
    gid = _groups(db, run["run_id"])[0]["id"]
    untouched = ['{"invalid_reason": not json at all ' + MARKER, '["invalid_reason", "' + MARKER + '"]', '"invalid_reason ' + MARKER + '"',
                 json.dumps({"rationale": "mentions invalid_reason as a word " + MARKER, "verdict": "approve"}),
                 # healthy pre-Modified-A rows: the key is present but carries no diagnostic (null / empty) - byte identity preserved,
                 # including the stored key order and spacing
                 '{"verdict":"approve", "rating": 8,"rationale": "fine", "valid": true, "invalid_reason": null}',
                 '{"verdict": "approve", "invalid_reason": "", "findings": []}',
                 json.dumps({"verdict": "revise", "valid": False, "invalid_reason": ai.FAILURE_TEXT["review_missing"]})]   # already closed: not unsafe
    for t in untouched:
        db.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at)
                      VALUES (?,0,NULL,NULL,'',NULL,?,1,NULL,'m',1,NULL,'2026-11-01T00:00:00+00:00')""", (gid, t))
    db.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at)
                  VALUES (?,0,NULL,NULL,'',NULL,?,0,?,'m',1,NULL,'2026-11-01T00:00:00+00:00')""",
               (gid, json.dumps({"verdict": "revise", "rationale": "keep " + MARKER, "invalid_reason": "findings[0]: " + MARKER}), "findings[0]: " + MARKER))
    db.commit()
    counts = ai_queue.neutralise_legacy_failure_text(db, commit=True)
    assert counts["review_rounds.review_json.invalid_reason"] == 1 and counts["review_rounds.invalid_reason"] == 1
    stored = [r["review_json"] for r in rows(db.execute("SELECT review_json FROM review_rounds WHERE group_id=? ORDER BY id", (gid,)))]
    assert stored[:len(untouched)] == untouched                              # byte-for-byte, MARKER words included
    assert json.loads(stored[len(untouched)]) == {"verdict": "revise", "rationale": "keep " + MARKER}   # intentional generated content kept, diagnostic key gone
    assert ai_queue.neutralise_legacy_failure_text(db, commit=True) == {k: 0 for k in counts}
