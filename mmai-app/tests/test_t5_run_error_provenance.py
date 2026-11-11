"""T5 run-error provenance for match_runs (matching.py slice).

Three properties, each with its own evidence:
1. NEW failures: the full-run worker stores one fixed plain sentence chosen by exception TYPE plus closed metadata; the raw
   message / class of an arbitrary exception reaches neither the row, the live RUNS entry, run_status nor the server log.
2. CURRENT rows are left alone: a real rules-only full run and a real apartment run project onto themselves with zero changes
   (byte-identical JSON). This fails if the projection ever drops a key the current code writes.
3. LEGACY rows are projected by neutralise_run_errors (commit-free, idempotent, every row): free text anywhere in the record
   (column, nested progress.summary, odd keys, personal-text dict keys, identifier-shaped values, malformed JSON) is replaced
   by the fixed legacy sentence or removed with a count, while operational counters, labels, staff_member and timestamps survive.
   run_status applies the SAME projection to a copy and never writes.
Not a launch-readiness claim; the enquiries.py hook that calls neutralise_run_errors is covered by the reviewer's tests.
"""
import json
import logging
import sqlite3
import threading

import pytest

from server import ai, db as dbmod, matching
from server.db import row
from tests.test_data_and_api import client, XLSX  # noqa: F401  (db fixture comes from conftest)
from tests.test_t6_staff_api import _ready_party

LEGACY = matching.RUN_FAILED_LEGACY_TEXT
REMOVED = matching.RUN_TEXT_REMOVED
MARK = "SyntheticWorkerTextAboutAstrid_MustNotPersist"


def _rec(c, rid):
    return row(c.execute("SELECT * FROM match_runs WHERE id=?", (rid,)))


def _dump(x):
    return json.dumps(x, default=str, sort_keys=True)


# ------------------------------------------------------------------ 1. new failures: fixed sentence by exception type
class _SyntheticAI(ai.AIUnavailable):
    pass


class _OddError(Exception):
    """Unlisted exception class: its NAME must not be stored either (error_type falls back to 'unknown')."""


_CASES = [
    (lambda: _SyntheticAI(MARK), "ai", "unknown"),
    (lambda: ai.AIOutputTruncated(MARK), "ai", "AIOutputTruncated"),
    (lambda: dbmod.RetentionGuardError(MARK), "database", "RetentionGuardError"),
    (lambda: sqlite3.OperationalError(MARK), "database", "OperationalError"),
    (lambda: matching.RunInProgress(7, "test"), "conflict", "RunInProgress"),
    (lambda: TimeoutError(MARK), "timeout", "TimeoutError"),
    (lambda: ValueError(MARK), "data", "ValueError"),
    (lambda: KeyError(MARK), "data", "KeyError"),
    (lambda: ZeroDivisionError(MARK), "data", "ZeroDivisionError"),
    (lambda: RuntimeError(MARK), "unexpected", "RuntimeError"),
    (lambda: _OddError(MARK), "unexpected", "unknown"),
]


def _fail_worker(c, monkeypatch, make):
    finished = threading.Event()
    progress = matching._progress
    monkeypatch.setattr(matching, "RUNS", {})

    def failing(*a, **k):
        raise make()

    def observed(*a, **k):
        progress(*a, **k)
        if k.get("status") == "failed":
            finished.set()

    monkeypatch.setattr(matching, "full_run", failing)
    monkeypatch.setattr(matching, "_progress", observed)
    rid = matching.start_full_run(c, "synthetic verifier", "test")
    assert finished.wait(10)
    return rid


@pytest.mark.parametrize("make,kind,token", _CASES, ids=[k[2] + "-" + k[1] for k in _CASES])
def test_worker_stores_fixed_sentence_and_closed_metadata_only(client, monkeypatch, caplog, make, kind, token):
    c, *_ = _ready_party(client, monkeypatch)
    caplog.set_level(logging.DEBUG)
    rid = _fail_worker(c, monkeypatch, make)
    text = matching.RUN_ERROR_TEXT[kind]
    r = _rec(c, rid)
    assert r["status"] == "failed" and r["ai_error"] == text
    prog = json.loads(r["progress_json"])
    assert prog["error"] == text and prog["error_kind"] == kind and prog["error_type"] == token and prog["error_schema"] == 1
    assert prog["status"] == "failed" and prog["phase"] == "failed" and prog["phase_label"] == "Run failed"
    with matching._RUN_LOCK:
        live = dict(matching.RUNS[rid])
    assert live["error"] == text and live["error_kind"] == kind
    st = matching.run_status(c, rid)
    assert st["ai_error"] == text and st["error_kind"] == kind and st["error_schema"] == 1 and "error_masked" not in st
    for surface in (r, live, st, caplog.text):
        assert MARK not in _dump(surface)
    if token == "unknown":
        assert "_OddError" not in _dump(r) + _dump(live) + _dump(st) + caplog.text and "_SyntheticAI" not in _dump(r) + caplog.text
    assert "Traceback" not in caplog.text
    # the stored record is already in-schema: the cleanup projection changes nothing on it
    assert matching.project_run_record(r["ai_error"], r["progress_json"], r["summary_json"], r["staff_member"])[3] == 0


def test_error_texts_are_fixed_plain_sentences_not_prefixes():
    assert set(matching.RUN_ERROR_TEXT) == {"ai", "database", "conflict", "timeout", "data", "unexpected"}
    for t in matching.RUN_ERROR_VOCABULARY:
        assert "{" not in t and ":" not in t and "Error" not in t
    assert matching.RUN_ERROR_TEXT["ai"] == "The AI review did not complete. Check the affected groups before retrying."
    # a sentence that merely STARTS like a fixed one is not trusted
    assert matching.project_run_record(matching.RUN_ERROR_TEXT["ai"] + " Astrid", None, None, None)[0] == LEGACY


def test_kind_is_decided_by_type_not_message():
    assert matching.run_error_kind(RuntimeError("sqlite3.OperationalError: database is locked")) == "unexpected"
    assert matching.run_error_kind(ValueError("AIUnavailable")) == "data"
    assert matching.run_error_type_token(type("AstridError", (Exception,), {})("x")) == "unknown"


# ------------------------------------------------------------------ 2. current successful rows project unchanged
@pytest.mark.skipif(not XLSX or not __import__("os").path.exists(XLSX), reason="apartment list not present")
def test_real_full_run_and_apartment_run_rows_project_to_themselves(db, monkeypatch):
    from server import importer, seed
    importer.import_apartments(db, XLSX, "test")
    seed.seed_test_data(db, n=60)
    monkeypatch.setattr(matching, "RUNS", {})
    res = matching.full_run(db, None, "test", apartments_per_group=3)        # system run: staff_member NULL
    rid = res.get("run_id") or row(db.execute("SELECT max(id) AS id FROM match_runs WHERE kind='full'"))["id"]
    r = _rec(db, rid)
    assert r["status"] == "done" and r["staff_member"] is None and r["progress_json"] and r["summary_json"]
    prog, summ = json.loads(r["progress_json"]), json.loads(r["summary_json"])
    # the real key sets (this is the proof that fails if the projection misses a key the code writes)
    # (dataset / staff_member are added by start_full_run's initial RUNS entry, covered by the next test)
    assert {"status", "phase", "phase_label", "done", "total", "pool_size", "apartments", "ai_calls", "ai_failed", "started_at", "summary"} <= set(prog) <= matching.PROGRESS_KEYS
    assert set(summ) <= matching.SUMMARY_KEYS and {"pairs_compared", "groups_total", "ai_error", "dataset", "complete"} <= set(summ)
    assert set(prog["summary"]) == set(summ)
    ai_err, p2, s2, changes = matching.project_run_record(r["ai_error"], r["progress_json"], r["summary_json"], r["staff_member"])
    assert changes == 0 and ai_err == r["ai_error"] and json.dumps(p2) == json.dumps(prog) and json.dumps(s2) == json.dumps(summ)
    with matching._RUN_LOCK:
        live = dict(matching.RUNS.get(rid) or {})
    counter = [0]
    assert matching._project_progress(live, None, counter) == live and counter[0] == 0
    a = row(db.execute("SELECT id FROM apartments WHERE available_from IS NOT NULL ORDER BY available_from LIMIT 1"))
    ares = matching.run_match(db, a["id"], "tester", dataset="test", use_ai=False)
    ar = _rec(db, ares["run_id"])
    assert ar["progress_json"] is None and ar["ai_error"] is None
    assert matching.project_run_record(ar["ai_error"], ar["progress_json"], ar["summary_json"], ar["staff_member"]) == (None, None, json.loads(ar["summary_json"]), 0)
    # neutralise over the whole table touches nothing
    before = [tuple(x) for x in db.execute("SELECT id, ai_error, progress_json, summary_json FROM match_runs ORDER BY id")]
    out = matching.neutralise_run_errors(db)
    assert out == {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}
    assert before == [tuple(x) for x in db.execute("SELECT id, ai_error, progress_json, summary_json FROM match_runs ORDER BY id")]
    st = matching.run_status(db, rid)
    assert "error_masked" not in st and st["progress"] == (live or prog) and st["summary"] == summ


def test_start_full_run_initial_runs_entry_and_running_row_project_unchanged(client, monkeypatch):
    c, *_ = _ready_party(client, monkeypatch)
    monkeypatch.setattr(matching, "RUNS", {})
    gate = threading.Event()
    monkeypatch.setattr(matching, "full_run", lambda *a, **k: gate.wait(10))
    rid = matching.start_full_run(c, "anna", "test")
    with matching._RUN_LOCK:
        live = dict(matching.RUNS[rid])
    assert set(live) == {"status", "phase", "phase_label", "dataset", "done", "total", "staff_member"}
    counter = [0]
    assert matching._project_progress(live, "anna", counter) == live and counter[0] == 0
    r = _rec(c, rid)
    assert matching.project_run_record(r["ai_error"], r["progress_json"], r["summary_json"], r["staff_member"])[3] == 0
    gate.set()


# ------------------------------------------------------------------ 3. legacy rows
def _legacy_summary(mark):
    return {"dataset": "test", "groups_total": 2, "pairs_compared": 17, "ai_error": mark}


def _legacy_progress(mark, **extra):
    p = {"status": "failed", "phase": "failed", "phase_label": "Run failed", "dataset": "test", "staff_member": "Synthetic operator",
         "done": 3, "total": 7, "started_at": "2026-11-01T09:00:00+11:00", "error": mark, "summary": _legacy_summary(mark)}
    p.update(extra)
    return p


def _seed_legacy(c, rid, mark, progress=None, summary=None, raw_progress=None, raw_summary=None, staff="Synthetic operator"):
    progress = _legacy_progress(mark) if progress is None else progress
    summary = _legacy_summary(mark) if summary is None else summary
    c.execute("""UPDATE match_runs SET kind='full', status='failed', dataset='test', staff_member=?, ai_error=?, progress_json=?, summary_json=?,
                 groups_total=2, pairs_compared=17 WHERE id=?""",
              (staff, mark, raw_progress if raw_progress is not None else json.dumps(progress),
               raw_summary if raw_summary is not None else json.dumps(summary), rid))
    c.commit()


def test_legacy_free_text_everywhere_is_projected_and_counters_survive(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticLegacyApplicantText_NotAnOperationalCount"
    prog = _legacy_progress(M, note=M, when="2031-03-04T05:06:07", error_type="Astrid", dataset="Astrid")
    prog["summary"]["Astrid Berg"] = 3
    prog["summary"]["appointment"] = "2031-03-04T05:06:07"
    prog[M + "_as_key"] = 1
    _seed_legacy(c, rid, M, progress=prog)
    monkeypatch.setattr(matching, "RUNS", {rid: dict(prog)})
    out = matching.neutralise_run_errors(c)
    assert out["match_runs_rows"] >= 1 and out["process_entries"] == 1 and out["unreadable_json"] == 0
    r = _rec(c, rid)
    with matching._RUN_LOCK:
        live = dict(matching.RUNS[rid])
    st = matching.run_status(c, rid)
    for surface in (r, live, st):
        s = _dump(surface)
        assert M not in s and "Astrid" not in s and "2031-03-04" not in s
    assert r["ai_error"] == LEGACY and r["groups_total"] == 2 and r["pairs_compared"] == 17 and r["dataset"] == "test" and r["staff_member"] == "Synthetic operator"
    p = json.loads(r["progress_json"])
    assert p["error"] == LEGACY and p["done"] == 3 and p["total"] == 7 and p["phase_label"] == "Run failed" and p["staff_member"] == "Synthetic operator"
    assert p["started_at"] == "2026-11-01T09:00:00+11:00" and p["dataset"] == REMOVED and p["error_type"] == "unknown"
    assert p["error_schema"] == 1 and p["error_kind"] == "unknown" and p["removed_keys"] == 3        # note, when, <M>_as_key
    assert p["summary"] == {"dataset": "test", "groups_total": 2, "pairs_compared": 17, "ai_error": LEGACY, "removed_keys": 2}
    assert json.loads(r["summary_json"]) == {"dataset": "test", "groups_total": 2, "pairs_compared": 17, "ai_error": LEGACY}
    assert live == p and st["progress"] == p and st["ai_error"] == LEGACY and "error_masked" not in st
    # idempotent: a second pass changes nothing (its own markers are part of the schema)
    before = (_dump(_rec(c, rid)), _dump(live))
    assert matching.neutralise_run_errors(c) == {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}
    with matching._RUN_LOCK:
        assert (_dump(_rec(c, rid)), _dump(matching.RUNS[rid])) == before


def test_malformed_and_non_object_json_become_fixed_replacement_but_null_stays_null(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticMalformed_Astrid"
    _seed_legacy(c, rid, M, raw_progress="malformed: " + M, raw_summary='["' + M + '"]')
    monkeypatch.setattr(matching, "RUNS", {rid: "not a dict " + M})
    out = matching.neutralise_run_errors(c)
    assert out["unreadable_json"] == 2 and out["process_entries"] == 1
    r = _rec(c, rid)
    assert json.loads(r["progress_json"]) == matching.UNREADABLE_PROGRESS and json.loads(r["summary_json"]) == matching.UNREADABLE_SUMMARY
    assert r["ai_error"] == LEGACY and r["groups_total"] == 2 and r["pairs_compared"] == 17
    with matching._RUN_LOCK:
        assert matching.RUNS[rid] == matching.UNREADABLE_PROGRESS
    assert M not in _dump(matching.run_status(c, rid))
    assert matching.neutralise_run_errors(c) == {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}
    # SQL NULL means absent and is preserved, never replaced
    c.execute("UPDATE match_runs SET progress_json=NULL, summary_json=NULL, ai_error=NULL WHERE id=?", (rid,))
    c.commit()
    assert matching.neutralise_run_errors(c)["match_runs_rows"] == 0
    r = _rec(c, rid)
    assert r["progress_json"] is None and r["summary_json"] is None and r["ai_error"] is None


def test_staff_member_in_progress_must_match_the_row_and_none_is_kept(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    _seed_legacy(c, rid, "x", progress=_legacy_progress("x", staff_member="Astrid wrote this"), staff=None)
    matching.neutralise_run_errors(c)
    p = json.loads(_rec(c, rid)["progress_json"])
    assert p["staff_member"] == REMOVED
    _seed_legacy(c, rid, "x", progress=_legacy_progress("x", staff_member=None), staff=None)
    matching.neutralise_run_errors(c)
    p = json.loads(_rec(c, rid)["progress_json"])
    assert "staff_member" in p and p["staff_member"] is None


def test_run_status_masks_on_a_copy_and_flags_it_without_writing(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticLegacy_Astrid"
    _seed_legacy(c, rid, M)
    monkeypatch.setattr(matching, "RUNS", {rid: _legacy_progress(M)})
    before = _dump(_rec(c, rid))
    with matching._RUN_LOCK:
        live_before = _dump(matching.RUNS)
    st = matching.run_status(c, rid)
    assert M not in _dump(st) and st["error_masked"] is True and st["ai_error"] == LEGACY and st["progress"]["summary"]["ai_error"] == LEGACY
    assert _dump(_rec(c, rid)) == before
    with matching._RUN_LOCK:
        assert _dump(matching.RUNS) == live_before
    assert M in before


def test_neutralise_is_commit_free_inside_callers_guard(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticRollback_Astrid"
    _seed_legacy(c, rid, M)
    monkeypatch.setattr(matching, "RUNS", {})
    before = _dump(_rec(c, rid))
    with pytest.raises(RuntimeError, match="Synthetic rollback"):
        with dbmod.retention_guard(c, "t5 rollback probe"):
            matching.neutralise_run_errors(c)
            assert M not in _dump(_rec(c, rid))
            raise RuntimeError("Synthetic rollback")
    assert _dump(_rec(c, rid)) == before


def test_wrongly_typed_fields_scrub_without_raising_and_second_pass_is_zero(client, monkeypatch):
    """Lists/dicts/numbers where a closed-vocabulary string belongs must never raise (a TypeError here would block a deletion)."""
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticTyped_Astrid"
    prog = {"status": [M], "phase": {"x": M}, "phase_label": 3, "dataset": [], "staff_member": {"name": M}, "done": "three", "total": [7],
            "started_at": {"when": M}, "error": [M], "error_kind": [], "error_type": {}, "summary": [M], "legacy_unreadable": M}
    summ = {"dataset": {}, "ai_error": [M], "discarded_enquiry_ids": [1, M, 2], "groups_total": [2], "legacy_unreadable": 0}
    _seed_legacy(c, rid, M, progress=prog, summary=summ)
    monkeypatch.setattr(matching, "RUNS", {rid: dict(prog)})
    out = matching.neutralise_run_errors(c)
    assert out["match_runs_rows"] == 1 and out["process_entries"] == 1
    r = _rec(c, rid)
    p, s = json.loads(r["progress_json"]), json.loads(r["summary_json"])
    assert M not in _dump(r) and M not in _dump(matching.run_status(c, rid))
    assert p["status"] is None and p["phase"] is None and p["phase_label"] is None and p["dataset"] == REMOVED and p["staff_member"] == REMOVED
    assert p["error"] == LEGACY and p["error_kind"] == "unknown" and p["error_type"] == "unknown" and p["error_schema"] == 1
    assert "done" not in p and "total" not in p and "started_at" not in p and "summary" not in p and "legacy_unreadable" not in p
    assert p["removed_keys"] == 5
    assert s == {"dataset": REMOVED, "ai_error": LEGACY, "discarded_enquiry_ids": [1, 2], "removed_keys": 1}
    assert r["groups_total"] == 2 and r["pairs_compared"] == 17
    with matching._RUN_LOCK:
        assert matching.RUNS[rid] == p
    assert matching.neutralise_run_errors(c) == {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}


def test_legacy_sentence_is_accurate_for_both_rest_and_read_mask():
    """The same sentence serves the read mask (DB may still hold raw text) and the cleanup; it must not claim details were removed."""
    assert LEGACY == "An earlier run did not complete. Its detailed error message is not shown."
    assert REMOVED == "[earlier detail not shown]"
    assert "removed" not in LEGACY.lower() and "removed" not in REMOVED.lower()


def test_failure_of_the_error_write_itself_is_contained(client, monkeypatch, caplog):
    """Broken DB while persisting the failure: no uncaught thread exception (which would print the raw chained context), the live
    RUNS state still says failed with the fixed sentence, the log names only id + category and says the row was NOT written."""
    c, *_ = _ready_party(client, monkeypatch)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(matching, "RUNS", {})
    uncaught, spawned = [], []
    real_execute, real_thread = c.execute, threading.Thread

    def broken_execute(sql, *a, **k):
        if sql.lstrip().startswith("UPDATE match_runs"):
            raise sqlite3.OperationalError("database is locked " + MARK)
        return real_execute(sql, *a, **k)

    def failing(*a, **k):
        raise RuntimeError(MARK)

    def tracked(*a, **k):
        t = real_thread(*a, **k)
        spawned.append(t)
        return t

    monkeypatch.setattr(matching, "full_run", failing)
    monkeypatch.setattr(c, "execute", broken_execute)
    monkeypatch.setattr(threading, "Thread", tracked)
    monkeypatch.setattr(threading, "excepthook", lambda args: uncaught.append(args.exc_type.__name__))
    rid = matching.start_full_run(c, "synthetic verifier", "test")
    assert len(spawned) == 1
    spawned[0].join(10)
    assert not spawned[0].is_alive() and uncaught == []
    with matching._RUN_LOCK:
        live = dict(matching.RUNS[rid])
    assert live["status"] == "failed" and live["error"] == matching.RUN_ERROR_TEXT["unexpected"] and live["error_kind"] == "unexpected"
    assert MARK not in caplog.text and "Traceback" not in caplog.text and "database is locked" not in caplog.text
    assert "could not be written to the database" in caplog.text
    monkeypatch.setattr(c, "execute", real_execute)
    r = _rec(c, rid)
    assert r["status"] == "running" and r["ai_error"] is None            # nothing was persisted - and nothing claims it was
    st = matching.run_status(c, rid)
    assert st["status"] == "failed" and MARK not in _dump(st) and MARK not in _dump(r)


def test_non_finite_numbers_and_bad_removed_keys_do_not_break_cleanup(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    prog = _legacy_progress("x", removed_keys=float("nan"), done=float("inf"), total=-0.0)
    prog["summary"]["removed_keys"] = -3
    prog["summary"]["pairs_compared"] = float("-inf")
    summ = {"dataset": "test", "removed_keys": 2.5, "groups_total": 2}
    _seed_legacy(c, rid, "x", progress=prog, summary=summ)
    monkeypatch.setattr(matching, "RUNS", {rid: dict(prog)})
    matching.neutralise_run_errors(c)
    r = _rec(c, rid)
    p, s = json.loads(r["progress_json"]), json.loads(r["summary_json"])
    assert "done" not in p and p["total"] == 0 and isinstance(p["removed_keys"], int) and p["removed_keys"] == 2
    assert "pairs_compared" not in p["summary"] and p["summary"]["removed_keys"] == 2
    assert s == {"dataset": "test", "groups_total": 2, "removed_keys": 1}
    assert matching.neutralise_run_errors(c) == {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}


def test_timestamps_must_be_real_iso_datetimes_not_just_iso_shaped(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    prog = _legacy_progress("x", started_at="2026-99-99T09:00:00", finished_at="2026-11-08T10:15:00+11:00")
    _seed_legacy(c, rid, "x", progress=prog)
    monkeypatch.setattr(matching, "RUNS", {})
    matching.neutralise_run_errors(c)
    p = json.loads(_rec(c, rid)["progress_json"])
    assert "started_at" not in p and p["finished_at"] == "2026-11-08T10:15:00+11:00" and p["removed_keys"] == 1
    assert matching._is_timestamp("2026-11-08T10:15:00.123456Z") and not matching._is_timestamp("2026-13-01") and not matching._is_timestamp(["2026-11-08"])


@pytest.mark.parametrize("where", ["runs", "row", "both"])
def test_top_level_status_never_echoes_raw_text(client, monkeypatch, where):
    """Adversarial status labels in the live RUNS entry and/or the row column: read-only, DB + RUNS unchanged, no raw status in the response."""
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    M = "SyntheticStatus_Astrid"
    prog = _legacy_progress("x", status=M if where in ("runs", "both") else "failed")
    _seed_legacy(c, rid, "x", progress=prog)
    if where in ("row", "both"):
        c.execute("UPDATE match_runs SET status=? WHERE id=?", (M, rid))
        c.commit()
    monkeypatch.setattr(matching, "RUNS", {rid: dict(prog)} if where != "row" else {})
    before = _dump(_rec(c, rid))
    with matching._RUN_LOCK:
        live_before = _dump(matching.RUNS)
    st = matching.run_status(c, rid)
    assert M not in _dump(st) and st["status"] in matching.RUN_STATUS_VOCAB and st["error_masked"] is True
    if where == "row":   # no live entry + unknown column -> fixed fallback from finished_at ('running' then reads as interrupted)
        assert st["status"] == ("done" if _rec(c, rid)["finished_at"] else "interrupted")
    assert _dump(_rec(c, rid)) == before
    with matching._RUN_LOCK:
        assert _dump(matching.RUNS) == live_before
    assert M in before or where == "runs"


def test_status_semantics_preserved(client, monkeypatch):
    c, _a, _lead, _solo, g = _ready_party(client, monkeypatch)
    rid = g["match_run_id"]
    c.execute("UPDATE match_runs SET status='running', finished_at=NULL, progress_json=NULL WHERE id=?", (rid,))
    c.commit()
    monkeypatch.setattr(matching, "RUNS", {})
    assert matching.run_status(c, rid)["status"] == "interrupted"
    monkeypatch.setattr(matching, "RUNS", {rid: {"status": "running", "phase": "rules", "done": 1, "total": 2}})
    assert matching.run_status(c, rid)["status"] == "running"
    c.execute("UPDATE match_runs SET status='done', finished_at='2026-11-09T01:00:00+11:00' WHERE id=?", (rid,))
    c.commit()
    monkeypatch.setattr(matching, "RUNS", {})
    st = matching.run_status(c, rid)
    assert st["status"] == "done" and "error_masked" not in st
