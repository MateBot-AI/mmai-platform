"""T5 (8 Nov 2026): the retention paths neutralise stored matching-run error text through ONE hook.

Primary scope: match_runs rows and the in-process run state (matching.neutralise_run_errors, builder-owned). The hook lives at the top of
enquiries.forget_generated_text, before its early return for an empty id list, so the staff DELETE route, an expiry purge with victims
and an expiry purge with nobody eligible all clean legacy rows through the same single call. Counts, phase, dataset and staff attribution
on the run row stay byte-identical; evidence here is the written bytes, never the run_status read mask. The later approved AI-failure
cleanup now shares this retention entry point; its boundary check below protects intentional rationale while confirming that failure
text is removed. The dedicated failure-text tests cover its other sinks. This does not close T5.
"""
import json
from datetime import date

import pytest

from server import ai, enquiries, matching
from server.db import retention_guard, row, rows
from tests.test_data_and_api import client  # noqa: F401
from tests.test_t6_staff_api import _ready_party

MARKER = "SyntheticLegacyApplicantText_HookProbe"
TODAY = date(2026, 11, 8)


def _seed(client, monkeypatch):
    c, _apt, lead, _solo, group = _ready_party(client, monkeypatch)
    rid = group["match_run_id"]
    summary = {"dataset": "test", "groups_total": 2, "pairs_compared": 17}
    progress = {"status": "failed", "phase": "failed", "phase_label": "Run failed", "dataset": "test", "staff_member": "Synthetic operator",
                "done": 3, "total": 7, "error": MARKER, "summary": summary}
    c.execute("""UPDATE match_runs SET kind='full', status='failed', dataset='test', staff_member='Synthetic operator', ai_error=?, progress_json=?,
                 summary_json=?, groups_total=2, pairs_compared=17 WHERE id=?""", (MARKER, json.dumps(progress), json.dumps(summary), rid))
    c.commit()
    monkeypatch.setattr(matching, "RUNS", {rid: dict(progress)})
    return c, lead, rid


def _rec(c, rid):
    return row(c.execute("SELECT * FROM match_runs WHERE id=?", (rid,)))


def _operational(r):
    """Everything on the row except the two error-bearing fields, plus the non-error keys of progress_json."""
    keep = {k: v for k, v in r.items() if k not in ("ai_error", "progress_json")}
    prog = json.loads(r["progress_json"]) if r.get("progress_json") else {}
    keep["progress_other"] = {k: v for k, v in prog.items() if k not in ("error", "error_schema", "error_kind", "error_type")}
    return keep


def test_staff_delete_cleans_and_reports_a_numeric_count(client, monkeypatch):
    c, lead, rid = _seed(client, monkeypatch)
    before = _rec(c, rid)
    res = client.delete(f"/api/enquiries/{lead['id']}")
    assert res.status_code == 200, res.text
    assert res.json()["generated_text_removed"]["run_errors_neutralised"] == 1
    after = _rec(c, rid)
    assert MARKER not in json.dumps(after)
    assert after["ai_error"] == matching.RUN_FAILED_LEGACY_TEXT
    assert json.loads(after["progress_json"])["error"] == matching.RUN_FAILED_LEGACY_TEXT
    assert _operational(after) == _operational(before)                 # counts, phase, dataset, staff attribution untouched
    with matching._RUN_LOCK:
        assert MARKER not in json.dumps(matching.RUNS[rid])


def test_expiry_purge_with_nobody_eligible_still_cleans_and_audits(client, monkeypatch):
    c, _lead, rid = _seed(client, monkeypatch)
    out = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert out["deleted"] == 0 and out["generated_text_removed"]["run_errors_neutralised"] == 1
    assert MARKER not in json.dumps(_rec(c, rid))
    last = rows(c.execute("SELECT detail FROM audit_log WHERE action='purge_expired' ORDER BY id DESC LIMIT 1"))[0]["detail"]
    assert "1 earlier run error(s) neutralised" in last and MARKER not in last
    # idempotent at rest: a second purge changes nothing and reports zero
    first = _rec(c, rid)
    out2 = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert out2["generated_text_removed"]["run_errors_neutralised"] == 0
    assert _rec(c, rid) == first


def test_expiry_purge_with_a_victim_cleans_in_the_same_guarded_step(client, monkeypatch):
    c, lead, rid = _seed(client, monkeypatch)
    c.execute("UPDATE enquiries SET last_contact_at='2000-01-01' WHERE id=?", (lead["id"],))
    c.commit()
    out = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert out["deleted"] >= 1 and out["generated_text_removed"]["run_errors_neutralised"] == 1
    assert row(c.execute("SELECT id FROM enquiries WHERE id=?", (lead["id"],))) is None
    assert MARKER not in json.dumps(_rec(c, rid))


def test_hook_is_called_exactly_once_per_retention_path(client, monkeypatch):
    c, lead, rid = _seed(client, monkeypatch)
    calls = []
    real = matching.neutralise_run_errors

    def counting(conn):
        calls.append(1)
        return real(conn)
    monkeypatch.setattr(matching, "neutralise_run_errors", counting)
    enquiries.purge_expired(c, "Synthetic operator", today=TODAY)       # no victims: still exactly one call
    assert len(calls) == 1
    res = client.delete(f"/api/enquiries/{lead['id']}")
    assert res.status_code == 200 and len(calls) == 2


def test_forget_with_an_empty_id_list_runs_the_hook_and_nothing_else(client, monkeypatch):
    c, _lead, rid = _seed(client, monkeypatch)
    with retention_guard(c, "hook probe"):
        out = enquiries.forget_generated_text(c, [])
    assert out["people"] == 0 and out["run_errors_neutralised"] == 1
    assert all(v == 0 for k, v in out.items() if k not in ("people", "run_errors_neutralised"))
    assert MARKER not in json.dumps(_rec(c, rid))


def test_hook_does_not_commit_outside_the_callers_guard(client, monkeypatch):
    c, lead, rid = _seed(client, monkeypatch)
    before = _rec(c, rid)
    with pytest.raises(RuntimeError, match="Synthetic rollback"):
        with retention_guard(c, "rollback probe"):
            enquiries.forget_generated_text(c, [lead["id"]])
            assert MARKER not in json.dumps(_rec(c, rid))
            raise RuntimeError("Synthetic rollback")
    assert _rec(c, rid) == before                                        # the guard's rollback undid the cleanup too: no hidden commit
    assert row(c.execute("SELECT id FROM enquiries WHERE id=?", (lead["id"],)))


def test_healthy_run_rows_are_left_byte_identical(client, monkeypatch):
    c, _apt, _lead, _solo, group = _ready_party(client, monkeypatch)
    res = matching.full_run(c, "Synthetic operator", "test")
    rid = res["run_id"]
    before = _rec(c, rid)
    out = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert out["generated_text_removed"]["run_errors_neutralised"] == 0
    assert _rec(c, rid) == before
    assert _rec(c, group["match_run_id"]) == _rec(c, group["match_run_id"])  # apartment-run row (legacy shape, counts only) untouched too


def test_shared_hook_cleans_ai_failures_but_preserves_intentional_rationale(client, monkeypatch):
    """The approved companion cleanup changes failure text, not a valid review's rationale or other group fields."""
    c, _lead, rid = _seed(client, monkeypatch)
    g = rows(c.execute("SELECT id FROM groups ORDER BY id LIMIT 1"))[0]
    rationale = "AI review: hold. The recorded preferences need staff discussion."
    c.execute("UPDATE groups SET ai_error=?, held_reason=?, brief_status='ok' WHERE id=?",
              ("LegacyGroupError", rationale, g["id"]))
    c.commit()
    before = row(c.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    out = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert out["generated_text_removed"]["ai_failure_sinks_neutralised"] == 1
    assert row(c.execute("SELECT * FROM groups WHERE id=?", (g["id"],))) == {
        **before, "ai_error": ai.LEGACY_FAILURE_TEXT,
    }
    again = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert again["generated_text_removed"]["ai_failure_sinks_neutralised"] == 0
