"""T5 (9 Nov 2026): the two remaining str(exception) audit sinks write closed tokens, and the ONE retention hook neutralises legacy
AI failure-sink text and the legacy free-text of exactly those two audit actions.

Wiring under test (integration builder): enquiries.forget_generated_text calls ai_queue.neutralise_legacy_failure_text (lead's bounded
projection: groups.ai_error, a 'Staff brief failed: ' held_reason, group_briefs.error, review_rounds.invalid_reason, the duplicated
review_json.invalid_reason key - intentional hold / revise rationale and apartment reasons are NOT failure sinks) and rewrites the detail
of audit actions bring_mate_error / submit_side_effect_error unless it is already a closed `UNEXPECTED:<type token>` or the legacy
sentence. Both run BEFORE the empty-id early return, inside the caller's retention guard, with no commit of their own and no startup
sweep. Evidence here is the written bytes; the staff read mask (staff_view) is shown NOT to be the cleanup.

The prefix-collision controls at the end are the positive controls root asked for on the lead's helper: a held_reason that begins with
'Staff brief failed: ' but whose provenance is not a failed brief (brief_status != 'failed') is an intentional hold rationale and must
stay byte-identical. They document the helper's current text-prefix selection; the lead applies the provenance-based correction in
ai_queue.py (their file) - these tests are the acceptance for it.
"""
import json
from datetime import date

import pytest

from server import ai, ai_queue, enquiries, matching
from server.db import retention_guard, row, rows
from fastapi.testclient import TestClient
from tests.test_bring_a_mate import _fake_ai, _person, _submit
from tests.test_data_and_api import client  # noqa: F401
from tests.test_t6_staff_api import _ready_party

MARKER = "SyntheticLegacyFailureText_AuditProbe"
TODAY = date(2026, 11, 9)
TWO = ("bring_mate_error", "submit_side_effect_error")


def _audit_rows(c):
    return rows(c.execute("SELECT id, action, entity_id, detail FROM audit_log WHERE action IN (?,?) ORDER BY id", TWO))


def _seed(client, monkeypatch):
    """A ready party group (Astrid + Bo + Cy, solo Devi) with: legacy failure text in groups.ai_error and a failed-brief held_reason,
    legacy free-text on both audit actions (one row each, on two different people), one already-closed token detail, one legacy-sentence
    detail, and an unrelated action carrying the marker (control)."""
    c, _apt, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    c.execute("UPDATE groups SET ai_status='failed', ai_error=?, brief_status='failed', held_reason=? WHERE id=?",
              ("AIUnavailable: provider said " + MARKER, ai_queue.BRIEF_FAILED_PREFIX + "Traceback " + MARKER, gid))
    for action, eid, detail in (("bring_mate_error", lead["id"], "ValueError: " + MARKER + " from the group step"),
                                ("submit_side_effect_error", solo["id"], "RuntimeError: " + MARKER + " while finishing"),
                                ("bring_mate_error", solo["id"], "UNEXPECTED:ValueError"),
                                ("submit_side_effect_error", lead["id"], ai.LEGACY_FAILURE_TEXT),
                                ("bring_mate_linked", solo["id"], "linked via share link " + MARKER)):
        c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES ('2026-11-01T00:00:00+00:00', NULL, ?, 'enquiry', ?, ?)",
                  (action, str(eid), detail))
    c.commit()
    return c, lead, solo, gid


def _group_sinks(c, gid):
    return row(c.execute("SELECT ai_error, held_reason, brief_status FROM groups WHERE id=?", (gid,)))


def _assert_clean(c, gid):
    g = _group_sinks(c, gid)
    assert g["ai_error"] == ai.LEGACY_FAILURE_TEXT and g["held_reason"] == ai_queue.BRIEF_FAILED_PREFIX + ai.LEGACY_FAILURE_TEXT
    details = [r["detail"] for r in _audit_rows(c)]
    assert MARKER not in json.dumps(details)
    assert set(details) == {ai.LEGACY_FAILURE_TEXT, "UNEXPECTED:ValueError"}        # closed token and legacy sentence kept, legacy text replaced


# ------------------------------------------------------------------------------------------- the hook, each retention path
def test_expiry_purge_with_nobody_eligible_cleans_both_and_audits_numbers(client, monkeypatch):
    c, _lead, solo, gid = _seed(client, monkeypatch)
    out = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    scrub = out["generated_text_removed"]
    assert out["deleted"] == 0 and scrub["ai_failure_sinks_neutralised"] == 2 and scrub["audit_errors_neutralised"] == 2
    _assert_clean(c, gid)
    # the unrelated action is not a target of the legacy step (nobody was deleted, so the per-person NULL statement did not run either)
    assert MARKER in row(c.execute("SELECT detail FROM audit_log WHERE action='bring_mate_linked' AND entity_id=?", (str(solo["id"]),)))["detail"]
    last = row(c.execute("SELECT detail FROM audit_log WHERE action='purge_expired' ORDER BY id DESC LIMIT 1"))["detail"]
    assert "2 earlier AI failure text(s) neutralised; 2 earlier link/submission error note(s) neutralised" in last and MARKER not in last
    # idempotent at rest: a second purge changes nothing and reports zero
    before = (_group_sinks(c, gid), _audit_rows(c))
    scrub2 = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)["generated_text_removed"]
    assert scrub2["ai_failure_sinks_neutralised"] == 0 and scrub2["audit_errors_neutralised"] == 0
    assert (_group_sinks(c, gid), _audit_rows(c)) == before


def test_staff_delete_cleans_in_the_same_guarded_step_and_reports_numbers(client, monkeypatch):
    c, lead, _solo, gid = _seed(client, monkeypatch)
    res = client.delete(f"/api/enquiries/{lead['id']}")
    assert res.status_code == 200, res.text
    scrub = res.json()["generated_text_removed"]
    assert scrub["ai_failure_sinks_neutralised"] == 2 and scrub["audit_errors_neutralised"] == 2
    assert row(c.execute("SELECT id FROM enquiries WHERE id=?", (lead["id"],))) is None
    details = [r["detail"] for r in _audit_rows(c)]
    assert MARKER not in json.dumps(details)
    # the deleted person's own rows of the two actions were then NULLed by the existing per-person statement; the others carry the sentence / token
    mine = [r["detail"] for r in _audit_rows(c) if r["entity_id"] == str(lead["id"])]
    assert mine and all(d is None for d in mine)
    others = {r["detail"] for r in _audit_rows(c) if r["entity_id"] != str(lead["id"])}
    assert others == {ai.LEGACY_FAILURE_TEXT, "UNEXPECTED:ValueError"}
    note = row(c.execute("SELECT detail FROM audit_log WHERE action='delete_enquiry' ORDER BY id DESC LIMIT 1"))["detail"]
    assert "2 earlier AI failure text(s) neutralised; 2 earlier link/submission error note(s) neutralised" in note and MARKER not in note
    # the group lost its lead: its failure sinks were neutralised by the legacy step before the group was cleared / dissolved
    assert MARKER not in json.dumps(_group_sinks(c, gid))


def test_hook_does_not_commit_outside_the_callers_guard(client, monkeypatch):
    c, lead, _solo, gid = _seed(client, monkeypatch)
    before = (_group_sinks(c, gid), _audit_rows(c))
    with pytest.raises(RuntimeError, match="Synthetic rollback"):
        with retention_guard(c, "rollback probe"):
            out = enquiries.forget_generated_text(c, [lead["id"]])
            assert out["ai_failure_sinks_neutralised"] == 2 and out["audit_errors_neutralised"] == 2
            assert MARKER not in json.dumps([_group_sinks(c, gid), _audit_rows(c)])   # (the group step then cleared the lead's group as today)
            raise RuntimeError("Synthetic rollback")
    assert (_group_sinks(c, gid), _audit_rows(c)) == before                 # the guard's rollback undid everything: no hidden commit


def test_forget_with_an_empty_id_list_runs_both_steps_and_nothing_else(client, monkeypatch):
    c, _lead, _solo, gid = _seed(client, monkeypatch)
    with retention_guard(c, "hook probe"):
        out = enquiries.forget_generated_text(c, [])
    assert out["people"] == 0 and out["ai_failure_sinks_neutralised"] == 2 and out["audit_errors_neutralised"] == 2
    assert all(v == 0 for k, v in out.items() if k not in ("people", "ai_failure_sinks_neutralised", "audit_errors_neutralised", "run_errors_neutralised"))
    assert isinstance(out["ai_failure_sinks_neutralised"], int) and isinstance(out["audit_errors_neutralised"], int)
    _assert_clean(c, gid)


def test_legacy_projection_is_called_exactly_once_per_retention_path(client, monkeypatch):
    c, lead, _solo, _gid = _seed(client, monkeypatch)
    calls = []
    real = ai_queue.neutralise_legacy_failure_text

    def counting(conn, commit=False):
        calls.append(commit)
        return real(conn, commit=commit)
    monkeypatch.setattr(ai_queue, "neutralise_legacy_failure_text", counting)
    enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert calls == [False]                                                 # once, and never with its own commit
    assert client.delete(f"/api/enquiries/{lead['id']}").status_code == 200
    assert calls == [False, False]


def test_only_the_two_named_actions_are_rewritten(client, monkeypatch):
    """Every other audit action keeps its detail byte-for-byte even when it carries the marker (the legacy step is not a text scan)."""
    c, _lead, solo, _gid = _seed(client, monkeypatch)
    for action in ("log_contact", "group_status", "bar_from_matching", "delete_enquiry"):
        c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES ('2026-11-01T00:00:00+00:00', 'Tester', ?, 'enquiry', ?, ?)",
                  (action, str(solo["id"]), "RuntimeError: " + MARKER))
    c.commit()
    before = rows(c.execute("SELECT id, action, detail FROM audit_log WHERE action NOT IN (?,?) ORDER BY id", TWO))
    enquiries.purge_expired(c, "Synthetic operator", today=TODAY)          # nobody eligible: only the legacy steps run
    after = rows(c.execute("SELECT id, action, detail FROM audit_log WHERE action NOT IN (?,?) AND action <> 'purge_expired' ORDER BY id", TWO))
    assert after == before


# ------------------------------------------------------------------------------------------- new writes are closed tokens
def test_bring_mate_failure_writes_a_closed_token_never_the_exception_text(client, monkeypatch):
    anon = TestClient(client.app)
    _fake_ai(monkeypatch)
    first = _submit(anon, _person(1)).json()
    tok = first["mate_link"]

    def boom(*a, **k):
        raise ValueError("applicant said " + MARKER)
    monkeypatch.setattr(matching, "bring_mate", boom)
    r = _submit(anon, _person(2), mate_token=tok)
    assert r.status_code == 201, r.text
    assert r.json()["mate"]["outcome"] == "error" and MARKER not in r.text
    c = enquiries_db(client)
    d = row(c.execute("SELECT detail FROM audit_log WHERE action='bring_mate_error' ORDER BY id DESC LIMIT 1"))["detail"]
    assert d == "UNEXPECTED:ValueError"


def test_submit_side_effect_failure_writes_a_closed_token_never_the_exception_text(client, monkeypatch):
    from server import app as application

    def boom(*a, **k):
        raise RuntimeError("provider replied " + MARKER)
    monkeypatch.setattr(application, "_public_finalise", boom)
    r = _submit(TestClient(client.app), _person(3))
    assert r.status_code == 201, r.text
    assert "did not finish" in r.json().get("warning", "") and MARKER not in r.text
    c = enquiries_db(client)
    d = row(c.execute("SELECT detail FROM audit_log WHERE action='submit_side_effect_error' ORDER BY id DESC LIMIT 1"))["detail"]
    assert d == "UNEXPECTED:RuntimeError"
    # the closed detail is left alone by the legacy step
    enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    assert row(c.execute("SELECT detail FROM audit_log WHERE action='submit_side_effect_error' ORDER BY id DESC LIMIT 1"))["detail"] == "UNEXPECTED:RuntimeError"


def test_foreign_exception_types_fall_back_to_the_unknown_token(client, monkeypatch):
    class ProviderSaid(Exception):
        pass

    def boom(*a, **k):
        raise ProviderSaid(MARKER)
    from server import app as application
    monkeypatch.setattr(application, "_public_finalise", boom)
    assert _submit(TestClient(client.app), _person(4)).status_code == 201
    c = enquiries_db(client)
    assert row(c.execute("SELECT detail FROM audit_log WHERE action='submit_side_effect_error' ORDER BY id DESC LIMIT 1"))["detail"] == "UNEXPECTED:unknown"


# ------------------------------------------------------------------------------------------- read mask is not the cleanup; intentional text stays
def test_staff_read_mask_hides_but_the_hook_is_what_changes_rows(client, monkeypatch):
    c, _lead, _solo, gid = _seed(client, monkeypatch)
    before = (_group_sinks(c, gid), _audit_rows(c))
    d = client.get(f"/api/groups/{gid}").json()
    assert MARKER not in json.dumps(d)                                      # masked for staff ...
    assert (_group_sinks(c, gid), _audit_rows(c)) == before                 # ... and reading changed nothing: the mask is not the cleanup
    enquiries.purge_expired(c, "Synthetic operator", today=TODAY)
    _assert_clean(c, gid)
    assert MARKER not in json.dumps(client.get(f"/api/groups/{gid}").json())


def test_healthy_intentional_rationale_and_closed_failures_are_left_byte_identical(client, monkeypatch):
    c, _apt, _lead, _solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    c.execute("UPDATE groups SET held_reason=?, brief_status='ok' WHERE id=?", ("AI review: hold. Astrid and Bo keep different hours (" + MARKER + ")", gid))
    c.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=?", ("quiet street suits the early risers " + MARKER, gid))
    c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES ('2026-11-01T00:00:00+00:00', NULL, 'bring_mate_error', 'enquiry', '999999', ?)",
              ("UNEXPECTED:KeyError",))
    c.commit()
    before = (row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))), rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY id", (gid,))), _audit_rows(c))
    scrub = enquiries.purge_expired(c, "Synthetic operator", today=TODAY)["generated_text_removed"]
    assert scrub["ai_failure_sinks_neutralised"] == 0 and scrub["audit_errors_neutralised"] == 0
    assert (row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))), rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY id", (gid,))), _audit_rows(c)) == before


# ------------------------------------------------------------------------------------------- positive controls for the helper's held_reason branch
def _held(client, monkeypatch, held_reason, brief_status):
    c, _apt, _lead, _solo, g = _ready_party(client, monkeypatch)
    c.execute("UPDATE groups SET held_reason=?, brief_status=? WHERE id=?", (held_reason, brief_status, g["id"]))
    c.commit()
    return c, g["id"]


@pytest.mark.parametrize("brief_status", ["ok", "pending", "stale", None])
def test_prefix_collision_intentional_hold_rationale_with_non_failed_brief_is_never_rewritten(client, monkeypatch, brief_status):
    """A held_reason that happens to START with 'Staff brief failed: ' while the brief did NOT fail is intentional text, not a failure
    sink: selection must be by provenance (brief_status = 'failed'), never by the text prefix alone."""
    c, gid = _held(client, monkeypatch, ai_queue.BRIEF_FAILED_PREFIX + "is what the lead applicant wrote; hold for a staff call (" + MARKER + ")", brief_status)
    before = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    counts = ai_queue.neutralise_legacy_failure_text(c, commit=True)
    assert counts["groups.held_reason"] == 0
    assert row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))) == before


def test_prefix_collision_failed_brief_legacy_text_is_rewritten_once(client, monkeypatch):
    c, gid = _held(client, monkeypatch, ai_queue.BRIEF_FAILED_PREFIX + "Traceback (most recent call last) " + MARKER, "failed")
    assert ai_queue.neutralise_legacy_failure_text(c, commit=True)["groups.held_reason"] == 1
    g = row(c.execute("SELECT held_reason, brief_status FROM groups WHERE id=?", (gid,)))
    assert g == {"held_reason": ai_queue.BRIEF_FAILED_PREFIX + ai.LEGACY_FAILURE_TEXT, "brief_status": "failed"}
    assert ai_queue.neutralise_legacy_failure_text(c, commit=True)["groups.held_reason"] == 0     # idempotent


def test_prefix_collision_failed_brief_with_closed_sentence_is_byte_identical(client, monkeypatch):
    c, gid = _held(client, monkeypatch, ai_queue.BRIEF_FAILED_PREFIX + ai.FAILURE_TEXT["no_review"], "failed")
    before = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert ai_queue.neutralise_legacy_failure_text(c, commit=True)["groups.held_reason"] == 0
    assert row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))) == before


def enquiries_db(client):
    from tests.test_data_and_api import appdb
    return appdb(client)
