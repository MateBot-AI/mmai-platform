"""T5 §5 migration of the AI writers and the matching deleters onto db.retention_guard (8 Nov 2026).

What is evidenced here (SQLite, two connections / two threads; nothing about PostgreSQL or a live model):
  * the AI packet and its evidence hash come from ONE row capture (matching.evidence_rows): an edit on a second connection cannot
    interleave with the capture step, and an edit that lands after the capture (while the model "works") makes the result stale
    at the apply step - the old facts are never stamped with the new hash (root's capture probe, now with a real second connection);
  * the identity checks run BEFORE any persistence, the no-result (outage) path included: a companion deleted in the same slot
    after the capture leaves only the generic re-queue text, never the provider's error (root's error-path probe);
  * a guard failure at the apply step (lock timeout / connection lost) drops the result and re-queues the group in a new step;
  * the model is never called with a guard held (process_pending / process_briefs assert it);
  * review_loop.promote is one guarded step: abort paths roll back without touching parent or alternative; the matching deleters
    (full-run save, GP9 bar paths) run inside guarded steps and leave no pending transaction behind.
"""
import threading
import time

import pytest

from server import ai, ai_queue, db as dbmod, matching, review_loop
from server.db import row, rows
from tests.test_t6_fail_closed import _good, _reply_builder
from tests.test_t6_late_result import _primary


def _second_connection(db):
    """A second db.Connection to the same SQLite file (what another worker / the enquiry deleter would hold). SQLite only by
    design: the PostgreSQL two-connection evidence is the reviewer's pg_only suite (tests/test_retention_two_connections.py)."""
    if getattr(db, "backend", "sqlite") != "sqlite":
        pytest.skip("SQLite two-connection variant; the PostgreSQL variant lives in the reviewer's pg_only suite")
    return dbmod.connect(db.execute("PRAGMA database_list").fetchone()[2])


def _valid_result(packet):
    return ai.parse_review_batch([{
        "index": 0, "rating": 8, "verdict": "approve", "rationale": "Synthetic assessment based on the captured routine.",
        "findings": [{"fact_ids": ["U1.routine"], "note": "The captured routine can be discussed."}],
        "apartments": [{"apartment_id": a["apartment_id"], "reason": "Synthetic apartment assessment"} for a in packet["apartments"]],
    }], [packet])[0]


def _flip_routine(c, eid):
    before = row(c.execute("SELECT routine FROM enquiries WHERE id=?", (eid,)))["routine"]
    after = "Night owl" if before != "Night owl" else "Early riser"
    c.execute("UPDATE enquiries SET routine=? WHERE id=?", (after, eid))
    c.commit()
    return before, after


# ---------------------------------------------------------------- capture coherence against a SECOND connection
def test_packet_and_hash_are_one_snapshot_and_a_second_connection_cannot_edit_during_the_capture(db):
    group, members = _primary(db)
    eid = members[0]
    db.execute("UPDATE enquiries SET routine='Early riser' WHERE id=?", (eid,)); db.commit()
    other = _second_connection(db)
    started, attempting, done = threading.Event(), threading.Event(), threading.Event()
    outcome = {}

    def editor():
        started.wait(5)
        before = row(other.execute("SELECT routine FROM enquiries WHERE id=?", (eid,)))["routine"]   # reads are not blocked
        after = "Night owl" if before != "Night owl" else "Early riser"
        attempting.set()                        # barrier: the write statement is issued right after this
        t0 = time.monotonic()
        try:
            other.execute("UPDATE enquiries SET routine=? WHERE id=?", (after, eid))   # blocks on SQLite's write lock until the step ends
            other.commit()
            outcome["before"], outcome["after"] = before, after
        except Exception as ex:   # pragma: no cover - a busy timeout would be reported, not hidden
            outcome["error"] = ex
        outcome["elapsed"] = time.monotonic() - t0
        done.set()

    t = threading.Thread(target=editor, daemon=True)
    t.start()
    try:
        with ai_queue._step(db, label="review capture"):
            hash_in_step, _ = matching.group_evidence(db, group["id"])
            started.set()
            assert attempting.wait(5), "the editor never reached its write"
            time.sleep(0.3)                     # the write is in flight (or already waiting): it must not have completed
            assert not done.is_set(), "the second connection's write must wait for the capture step to finish"
            packet = ai_queue._payload(db, group)
            assert packet and packet["members_for_ai"][0]["routine"] == "Early riser"
            assert packet["evidence_hash"] == hash_in_step   # the hash in the packet is the hash of the captured rows
            assert row(db.execute("SELECT routine FROM enquiries WHERE id=?", (eid,)))["routine"] == "Early riser"   # still unedited at step end
        assert done.wait(15) and "error" not in outcome, outcome
        assert outcome["elapsed"] >= 0.2, "the write completed without waiting for the step (causality: it must have been blocked)"
    finally:
        other.close()
    # the edit landed AFTER the step: current evidence differs from the captured hash, so the result is stale at apply time
    now_hash, _ = matching.group_evidence(db, group["id"])
    assert now_hash != packet["evidence_hash"]
    assert row(db.execute("SELECT routine FROM enquiries WHERE id=?", (eid,)))["routine"] == outcome["after"]
    result = _valid_result(packet)
    assert result["valid"], result.get("invalid_reason")
    with ai_queue._step(db, label="review apply"):
        accepted = ai_queue._apply_result(db, group, packet, result, None, 1)
    assert not accepted, "an assessment of the old routine must not be accepted as current under the edited routine"
    hist = rows(db.execute("SELECT evidence_hash FROM review_rounds WHERE group_id=?", (group["id"],)))
    assert [h["evidence_hash"] for h in hist] == [packet["evidence_hash"]], "history keeps the review labelled with the facts it was about, never the new hash"
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (group["id"],)))
    assert g["ai_status"] == "pending" and g["evidence_json"] is None and g["evidence_hash"] != now_hash
    assert not db.in_transaction


def test_evidence_rows_is_the_single_source_for_packet_and_hash(db):
    """Pure check of the capture contract: the same capture gives the same hash as group_evidence; a changed capture changes it;
    the rule_rank/rule_score the capture step refreshes are outside the hash."""
    group, members = _primary(db)
    cap = matching.evidence_rows(db, group["id"])
    h1, snap = matching.evidence_from_rows(cap)
    assert (h1, snap) == matching.group_evidence(db, group["id"])
    assert len(snap["members"]) == 2 and snap["apartments"]
    p = ai_queue._payload(db, group, cap=cap)
    db.commit()
    assert p["evidence_hash"] == h1 == matching.group_evidence(db, group["id"])[0]   # rule_rank refresh did not move the hash
    cap2 = matching.evidence_rows(db, group["id"])
    cap2["members"][0]["enquiry"] = dict(cap2["members"][0]["enquiry"], routine="Night owl")
    assert matching.evidence_from_rows(cap2)[0] != h1
    # a capture whose membership differs from the queued group's member_key is refused (no mixed-state packet)
    stale_group = dict(group, member_key=group["member_key"] + ",999999")
    assert ai_queue._payload(db, stale_group, cap=cap) is None


# ---------------------------------------------------------------- identity before error persistence (root's probe, same slot)
def test_outage_after_same_slot_companion_deletion_leaves_only_the_generic_requeue(db, monkeypatch):
    from tests.test_t6_grounding import _party_group
    _, lead, _, group = _party_group(db, monkeypatch)
    with ai_queue._step(db, label="review capture"):
        packet = ai_queue._payload(db, group)
    victim = row(db.execute("SELECT id, name FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
    assert victim and packet
    other = _second_connection(db)
    try:
        other.execute("DELETE FROM party_members WHERE id=?", (victim["id"],)); other.commit()   # the companion goes while the model is "working"
    finally:
        other.close()
    assert not ai_queue.refs_intact(db, packet["refs_json"])
    history_before = (row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (group["id"],)))["n"],
                      row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=?", (group["id"],)))["n"])
    personal_error = f"Synthetic failed response mentioning {victim['name']}"
    with ai_queue._step(db, label="review apply"):
        assert ai_queue._apply_result(db, group, packet, None, personal_error, 1) is False
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (group["id"],)))
    assert g["ai_status"] == "pending" and "deleted or the membership changed" in g["ai_error"]
    assert victim["name"] not in g["ai_error"] and "Synthetic" not in g["ai_error"]
    assert (row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (group["id"],)))["n"],
            row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=?", (group["id"],)))["n"]) == history_before, "no history row of any kind"


# ---------------------------------------------------------------- guard failure at the apply step: drop + re-queue
@pytest.mark.parametrize("failure", [dbmod.RetentionLockTimeout("database busy (test)"), dbmod.ConnectionLostMidOperation("connection lost (test)")],
                         ids=["lock_timeout", "connection_lost"])
def test_guard_failure_at_the_apply_step_drops_the_result_and_requeues(db, monkeypatch, failure):
    group, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    real = ai_queue._step
    hits = {"apply": 0}

    class _Boom:
        def __enter__(self):
            raise failure   # the guard's own exception types; raised at acquisition, so nothing of the step was written

        def __exit__(self, *a):
            return False

    def flaky(c, label):
        if label == "review apply" and hits["apply"] == 0:
            hits["apply"] += 1
            return _Boom()
        return real(c, label)
    monkeypatch.setattr(ai_queue, "_step", flaky)
    out = ai_queue.process_pending(db)
    assert hits["apply"] == 1 and out["failed"] >= 1
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (group["id"],)))
    assert g["ai_status"] == "pending" and g["ai_error"] == ai_queue.REQUEUE_AFTER_GUARD_FAILURE
    assert row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (group["id"],)))["n"] == 0, "the result is never replayed or persisted"
    assert not db.in_transaction
    # the next pass (no failure) records the review normally
    out2 = ai_queue.process_pending(db)
    assert out2["ok"] == 1 and row(db.execute("SELECT ai_status FROM groups WHERE id=?", (group["id"],)))["ai_status"] == "ok"


def test_the_model_is_never_called_with_a_guard_held(db, monkeypatch):
    group, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    seen = []

    inner = _reply_builder(_good)

    def ask(*a, **k):
        seen.append(dbmod.guarded(db))
        return inner(*a, **k)
    monkeypatch.setattr(ai, "_ask", ask)
    ai_queue.process_pending(db)
    assert seen and not any(seen)
    with ai_queue._step(db, label="test"):
        with pytest.raises(dbmod.RetentionGuardError):
            dbmod.assert_not_guarded(db, "model call")


# ---------------------------------------------------------------- promote: one guarded step, abort paths roll back
def test_promote_abort_paths_leave_parent_and_alternative_untouched_without_pending_transaction(db):
    """A revision whose membership moved between the pre-lock read and the guarded re-read is aborted inside the guard: nothing
    is written, no transaction stays open, the shared RLock is released."""
    group, members = _primary(db)
    when = "2026-11-08T00:00:00"
    db.execute("UPDATE groups SET ai_status='revise', status='Proposed' WHERE id=?", (group["id"],))
    parent = dict(row(db.execute("SELECT * FROM groups WHERE id=?", (group["id"],))))
    parent.pop("id")
    parent.update(status="Proposed", is_primary=0, ai_status="ok", brief_status="ok", revision_of=group["id"], revision_round=1, created_at=when, updated_at=when)
    cols = list(parent)
    db.execute("INSERT INTO groups(%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), [parent[k] for k in cols])
    alt = row(db.execute("SELECT * FROM groups WHERE revision_of=?", (group["id"],)))
    for eid in members:
        db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?,?)", (alt["id"], eid))
    db.commit()
    before = (row(db.execute("SELECT status, is_primary, held_reason FROM groups WHERE id=?", (group["id"],))),
              row(db.execute("SELECT status, is_primary, held_reason FROM groups WHERE id=?", (alt["id"],))))
    real = review_loop._begin_guarded

    def move_membership(c, gids, eids):
        real(c, gids, eids)
        c.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (alt["id"], members[0]))   # lands inside the guarded step
    db.commit()
    review_loop._begin_guarded = move_membership   # restored by hand below
    try:
        res = review_loop.promote(db, alt["id"])
    finally:
        review_loop._begin_guarded = real
    assert res["promoted"] is False and res["reason"].startswith("alternative not ready")   # the abort happens INSIDE the guarded step
    assert not db.in_transaction and not dbmod.guarded(db)
    after = (row(db.execute("SELECT status, is_primary, held_reason FROM groups WHERE id=?", (group["id"],))),
             row(db.execute("SELECT status, is_primary, held_reason FROM groups WHERE id=?", (alt["id"],))))
    assert after == before
    assert len(rows(db.execute("SELECT 1 FROM group_members WHERE group_id=?", (alt["id"],)))) == 2, "the abort rolled the in-step delete back too"
    # membership moved between the pre-lock read and the guarded re-read (a member left the alternative AND the parent)
    def move_membership_everywhere(c, gids, eids):
        real(c, gids, eids)
        c.execute("DELETE FROM group_members WHERE enquiry_id=?", (members[0],))
    n_before = len(rows(db.execute("SELECT 1 FROM group_members WHERE enquiry_id=?", (members[0],))))
    review_loop._begin_guarded = move_membership_everywhere
    try:
        res = review_loop.promote(db, alt["id"])
    finally:
        review_loop._begin_guarded = real
    assert res["promoted"] is False and res["reason"] == "membership changed under us" and not db.in_transaction
    assert len(rows(db.execute("SELECT 1 FROM group_members WHERE enquiry_id=?", (members[0],)))) == n_before, "rolled back"


# ---------------------------------------------------------------- matching deleters run as guarded steps
def test_full_run_save_and_bar_paths_are_guarded_steps(db, monkeypatch):
    from tests.test_launch_neutral import _apt, _pool
    labels = []
    real = dbmod.retention_guard

    class Spy(real):
        def __init__(self, c, label=""):
            labels.append(label)
            super().__init__(c, label=label)
    monkeypatch.setattr(dbmod, "retention_guard", Spy)
    a = _apt(db)
    _pool(db)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert "full run save" in labels and not db.in_transaction
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
    labels.clear()
    matching.bar_enquiry(db, members[0], "tester", "test reason")
    assert "bar: drop alternatives" in labels and not db.in_transaction
    assert row(db.execute("SELECT barred_at FROM enquiries WHERE id=?", (members[0],)))["barred_at"]


# ---------------------------------------------------------------- reviewed offer == hashed offer == stored offer (root's coherence finding)
def _rent_rise_then_requeue(db, g, monkeypatch):
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    g1 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g1["ai_status"] == "ok" and ai_queue.is_ready(db, g1)["ready"]
    db.execute("UPDATE apartments SET rent_12m=rent_12m+50, rent_6m=rent_6m+50 WHERE id=?", (g1["apartment_id"],)); db.commit()   # inventory update after the review
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert not ai_queue.is_ready(db, g2)["ready"]
    db.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (g["id"],)); db.commit()   # what requeue_stale / a full run would do
    return g1


def test_proposed_group_re_review_refreshes_the_stored_offer_so_packet_hash_and_store_agree(db, monkeypatch):
    g, members = _primary(db)
    g1 = _rent_rise_then_requeue(db, g, monkeypatch)
    ai_queue.process_pending(db)
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], g3["apartment_id"])))
    assert g3["ai_status"] == "ok" and ai_queue.is_ready(db, g3)["ready"], ai_queue.is_ready(db, g3)
    assert float(ga["total_weekly"]) == float(g1["total_weekly"]) + 50 == float(g3["total_weekly"]), "the stored offer followed the rules (Proposed group)"
    assert g3["evidence_hash"] == matching.group_evidence(db, g["id"])[0]   # the hash stored with the review is the hash of the stored offer
    with ai_queue._step(db, label="review capture"):
        p = ai_queue._payload(db, g3)   # a fresh capture of the stored state reproduces exactly the reviewed packet and hash
    pa = next(x for x in p["apartments"] if x["apartment_id"] == g3["apartment_id"])
    assert float(pa["total_weekly"]) == float(ga["total_weekly"]) and p["evidence_hash"] == g3["evidence_hash"]


def test_introduced_group_offer_is_never_rewritten_by_the_queue_but_held_for_staff(db, monkeypatch):
    g, members = _primary(db)
    g1 = _rent_rise_then_requeue(db, g, monkeypatch)
    db.execute("UPDATE groups SET status='Introduced' WHERE id=?", (g["id"],)); db.commit()
    calls = []
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: calls.append(1))
    ai_queue.process_pending(db)
    g3 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    ga = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], g3["apartment_id"])))
    assert not calls, "no model call about an offer that is not the one stored"
    assert float(ga["total_weekly"]) == float(g1["total_weekly"]) == float(g3["total_weekly"]), "introduced terms untouched"
    assert g3["ai_status"] == "failed" and g3["ai_error"] == ai_queue.OFFER_DRIFT_REASON
    assert not ai_queue.is_ready(db, g3)["ready"] and not db.in_transaction


# ---------------------------------------------------------------- D: lease claim belongs to the caller; the helper verifies, then reranks per group
def test_mark_apartment_leased_with_claimed_at_verifies_the_landed_claim_and_reranks_under_guards(db, monkeypatch):
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    others = [r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],)))]
    assert others
    # a claim that did not land (nothing written) never triggers a rerank
    with pytest.raises(ValueError):
        matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at="2026-11-08T10:00:00")
    assert row(db.execute("SELECT COUNT(*) n FROM groups WHERE flag_kind='lease'"))["n"] == 0
    # a claim for ANOTHER group is not ours either
    when = "2026-11-08T10:00:00"
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester' WHERE id=?", (when, others[0], a["id"])); db.commit()
    with pytest.raises(ValueError):
        matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at=when)
    db.execute("UPDATE apartments SET leased_group_id=? WHERE id=?", (g["id"], a["id"])); db.commit()
    labels = []
    real = dbmod.retention_guard

    class Spy(real):
        def __init__(self, c, label=""):
            labels.append(label)
            super().__init__(c, label=label)
    monkeypatch.setattr(dbmod, "retention_guard", Spy)
    out = matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at=when)
    assert out["leased_at"] == when and out["leased_group_id"] == g["id"]
    assert labels.count("lease rerank") == len(out["affected"]) >= 1 and not db.in_transaction
    assert all(x["group_id"] != g["id"] for x in out["affected"])
    assert row(db.execute("SELECT COUNT(*) n FROM group_apartments WHERE apartment_id=? AND group_id<>?", (a["id"], g["id"])))["n"] == 0
    assert row(db.execute("SELECT leased_at FROM apartments WHERE id=?", (a["id"],)))["leased_at"] == when   # the helper wrote no claim of its own
    assert row(db.execute("SELECT COUNT(*) n FROM audit_log WHERE action='apartment_leased'"))["n"] == 1


# ---------------------------------------------------------------- full_run save: re-validate under the lock (root's save-race probes)
class _HookedGuard:
    """Runs `hook(label)` just BEFORE the real guard is acquired (a writer that committed while the run was computing)."""
    def __init__(self, real, hook):
        self.real, self.hook = real, hook

    def __call__(self, c, label=""):
        self.hook(c, label)
        return self.real(c, label=label)


def test_full_run_save_never_deletes_a_group_that_became_leased_while_the_run_computed(db, monkeypatch):
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    _pool(db)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
    # the inventory becomes unavailable for the NEXT run, so its planned result has no group with this member set at all
    db.execute("UPDATE apartments SET available_from=NULL WHERE id<>?", (a["id"],))
    db.execute("UPDATE apartments SET leased_at='2026-11-08T09:00:00', leased_group_id=? WHERE id=?", (g["id"], a["id"])); db.commit()
    fired = []

    def lease_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            c.execute("UPDATE groups SET status='Leased', updated_at='2026-11-08T09:00:01' WHERE id=?", (g["id"],))   # app._set_group_status(Leased) committed meanwhile
            for eid in members:
                c.execute("UPDATE enquiries SET status='Leased' WHERE id=?", (eid,))
            c.commit()
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, lease_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and not db.in_transaction
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after is not None and after["status"] == "Leased", "a group that was leased while the run computed must not be deleted by the stale plan"
    assert rows(db.execute("SELECT 1 FROM group_members WHERE group_id=?", (g["id"],)))
    assert all(row(db.execute("SELECT status FROM enquiries WHERE id=?", (eid,)))["status"] == "Leased" for eid in members)
    assert out["groups_discarded_at_save"] == 0


def test_full_run_save_discards_a_plan_whose_party_changed_while_the_run_computed(db, monkeypatch):
    from tests.test_t6_grounding import _apt, _enq, member
    _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0)
    lead = _enq(db, "Astrid", "astrid@example.com", party="With friends", party_size="3", budget_option="3B-single", apt_sizes=["3 bedrooms"], rooms=["Share with one other", "Own room"],
                room_first="Share with one other", mate_max="5", party_members=[member(name="Bo Berg", email="bo@example.com"), member(name="Cy Lund", email="cy@example.com")],
                couples_answer="No", contact_method="WhatsApp", diet="Vegetarian", social="Quiet", routine="Early riser")
    _enq(db, "Devi", "devi@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    companion = row(db.execute("SELECT id, name FROM party_members WHERE enquiry_id=? ORDER BY id DESC LIMIT 1", (lead["id"],)))
    fired = []

    def bar_companion_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            matching.bar_enquiry(c, lead["id"], "staff", "test reason", person=companion["id"], party_choice="stay")   # committed while the run computed
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, bar_companion_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and not db.in_transaction
    assert row(db.execute("SELECT COUNT(*) n FROM party_members WHERE id=?", (companion["id"],)))["n"] == 0
    assert out["groups_discarded_at_save"] >= 1 and out["complete"] is False and lead["id"] in out["discarded_enquiry_ids"]
    # honest progress (T8): the discarded enquiries count as unprocessed, so the run's watermark sits below them and the scheduler
    # sees them as new applicants; the run audit says it was incomplete
    run = row(db.execute("SELECT pool_watermark_id, summary_json, status FROM match_runs WHERE id=?", (out["run_id"],)))
    assert run["status"] == "done" and run["pool_watermark_id"] < min(out["discarded_enquiry_ids"]) and '"complete": false' in run["summary_json"]
    from server import scheduler
    assert scheduler.new_applicants_since(db, "test", scheduler.last_completed_run(db, "test")) >= 1
    assert "INCOMPLETE" in row(db.execute("SELECT detail FROM audit_log WHERE action='full_run' ORDER BY id DESC LIMIT 1"))["detail"]
    for g in rows(db.execute("SELECT id, allocation_json FROM groups")):
        for ga in rows(db.execute("SELECT allocation_json FROM group_apartments WHERE group_id=?", (g["id"],))):
            assert f"{lead['id']}m2" not in (ga["allocation_json"] or ""), "a bed allocation planned for the barred companion was persisted"
        assert f"{lead['id']}m2" not in (g["allocation_json"] or "")
    assert out["groups_total"] == 0 and row(db.execute("SELECT COUNT(*) n FROM groups"))["n"] == 0   # the stale plan was the only candidate
    # the next run computes from the current party and has nothing stale to discard
    out2 = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert out2["groups_discarded_at_save"] == 0 and out2["pool_size"] == 2 and out2["complete"] is True
    assert scheduler.new_applicants_since(db, "test", scheduler.last_completed_run(db, "test")) == 0


# ---------------------------------------------------------------- post-claim failures: the lease is durable, the rerank is reported and retried
def test_post_claim_rerank_failure_is_reported_as_pending_never_as_nothing_changed_and_is_retryable(db, monkeypatch):
    """Root (8 Nov 2026): the rerank runs AFTER the lease commit. An injected lock timeout on one group's step must leave the lease
    in place, still rerank the other groups, return rerank.status='partial' with the pending group, keep that group detectable
    (pending_lease_reranks) and finish on retry (idempotent), after which nothing is pending."""
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    others = [r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed' ORDER BY id", (g["id"],)))]
    assert len(others) >= 2
    when = "2026-11-08T10:00:00"
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester' WHERE id=?", (when, g["id"], a["id"])); db.commit()   # the caller's durable claim
    victim = others[0]
    real = dbmod.retention_guard
    fired = []

    class FailOnce(real):
        def __init__(self, c, label=""):
            if label == "lease rerank" and not fired and row(c.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (victim, a["id"]))):
                fired.append(1)
                raise dbmod.RetentionLockTimeout("injected: another writer held the step")
            super().__init__(c, label=label)
    monkeypatch.setattr(dbmod, "retention_guard", FailOnce)
    out = matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at=when)
    assert fired and not db.in_transaction
    assert out["rerank"]["status"] == "partial" and out["rerank"]["pending_group_ids"] == [victim]
    assert out["rerank"]["failed"][0]["category"] == "RetentionLockTimeout"
    assert row(db.execute("SELECT leased_at, leased_group_id FROM apartments WHERE id=?", (a["id"],))) == {"leased_at": when, "leased_group_id": g["id"]}   # never undone
    assert {x["group_id"] for x in out["affected"]} == set(others[1:])   # the other groups were still re-ranked
    assert row(db.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (victim, a["id"])))   # the pending group still lists it
    pending = matching.pending_lease_reranks(db)
    assert [p["group_id"] for p in pending] == [victim] and pending[0]["apartment_id"] == a["id"]
    assert "RERANK PENDING" in row(db.execute("SELECT detail FROM audit_log WHERE action='apartment_leased' ORDER BY id DESC LIMIT 1"))["detail"]
    # retry finishes exactly the pending group; a second retry is a no-op
    monkeypatch.setattr(dbmod, "retention_guard", real)
    r1 = matching.retry_pending_lease_reranks(db, "tester")
    assert r1["retried"] == 1 and [x["group_id"] for x in r1["affected"]] == [victim] and not r1["failed"] and not db.in_transaction
    assert matching.pending_lease_reranks(db) == []
    assert not row(db.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (victim, a["id"])))
    r2 = matching.retry_pending_lease_reranks(db, "tester")
    assert r2 == {"retried": 0, "affected": [], "failed": []}
    assert row(db.execute("SELECT COUNT(*) n FROM group_apartments WHERE apartment_id=? AND group_id<>?", (a["id"], g["id"])))["n"] == 0


def test_rerank_leaves_no_pending_marker_for_a_group_left_without_an_apartment(db, monkeypatch):
    """A group whose only apartment was leased keeps apartment_id (so staff see what happened) but its rerank is FINAL, not pending."""
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    when = "2026-11-08T10:00:00"
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester' WHERE id=?", (when, g["id"], a["id"])); db.commit()
    out = matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at=when)
    assert out["rerank"]["status"] == "complete" and out["affected"] and all(x["outcome"] == "no_apartment" for x in out["affected"])
    assert matching.pending_lease_reranks(db) == []


# ---------------------------------------------------------------- full_run save: apartment state, later writers, primaries (root's third probe and review)
def test_full_run_save_drops_options_on_an_apartment_leased_during_the_run_and_never_restores_them(db, monkeypatch):
    """Root's third save-race: a real lease claim (no group) lands between the computation and the save. No group_apartments row for
    that apartment may be written from the old options; candidates with another apartment keep going (trimmed), the rest are
    discarded and reported."""
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    b = _apt(db, address="Unit 2 / 2 Neutral Street", rent12=1100.0, rent6=1180.0)
    _pool(db)
    fired = []

    def lease_a_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            matching.mark_apartment_leased(c, a["id"], "staff", None)   # the real claim path, committed while the run computed
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, lease_a_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and not db.in_transaction
    assert row(db.execute("SELECT COUNT(*) n FROM group_apartments WHERE apartment_id=?", (a["id"],)))["n"] == 0, "options computed before the lease were written back"
    assert row(db.execute("SELECT COUNT(*) n FROM groups WHERE apartment_id=?", (a["id"],)))["n"] == 0
    assert out["groups_total"] >= 1 and all(r["apartment_id"] == b["id"] for r in rows(db.execute("SELECT apartment_id FROM group_apartments")))
    assert out["complete"] is True   # every candidate still had an unchanged apartment: trimmed, not discarded
    # the second run is a no-op on an already consistent store
    out2 = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert out2["groups_discarded_at_save"] == 0 and out2["groups_total"] == out["groups_total"]


def test_full_run_save_discards_candidates_whose_only_apartment_was_edited_during_the_run(db, monkeypatch):
    """A rent change while the run computed: the offer figures are stale, so nothing from the old option is persisted and the run
    reports itself incomplete; the next run computes from the new rent."""
    from tests.test_launch_neutral import _apt, _pool
    a = _apt(db)
    _pool(db)
    fired = []

    def raise_rent_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            c.execute("UPDATE apartments SET rent_12m=1190.0, updated_at='2026-11-08T09:00:00' WHERE id=?", (a["id"],)); c.commit()
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, raise_rent_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and out["groups_total"] == 0 and out["groups_discarded_at_save"] >= 1 and out["complete"] is False
    assert row(db.execute("SELECT COUNT(*) n FROM group_apartments"))["n"] == 0 and row(db.execute("SELECT COUNT(*) n FROM groups"))["n"] == 0
    assert out["enquiries_in_primary_group"] == 0, "discarded plans must not be counted as covered"
    out2 = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert out2["complete"] is True and out2["groups_total"] >= 1
    assert all(float(r["weekly_rent"]) == 1190.0 for r in rows(db.execute("SELECT weekly_rent FROM group_apartments WHERE rate_kind='12m'")))


def test_full_run_save_leaves_a_group_created_by_a_later_writer_alone_and_cedes_its_members(db, monkeypatch):
    """A per-apartment run (another writer) proposes a group after this run read its inputs. The full run neither deletes it nor
    proposes a second primary group for its members; those enquiries stay 'Proposed in group' for the later group."""
    from tests.test_launch_neutral import _apt, _pool
    _apt(db)
    _pool(db)
    fired, later_gid = [], []

    def other_run_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            a = row(c.execute("SELECT id FROM apartments"))
            matching.run_match(c, a["id"], "other staff", dataset="test")   # a complete per-apartment run committed meanwhile
            later_gid.append(row(c.execute("SELECT id FROM groups WHERE is_primary=1 ORDER BY id DESC LIMIT 1"))["id"])
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, other_run_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and later_gid and not db.in_transaction
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (later_gid[0],)))["status"] == "Proposed", "a later writer's group was deleted by the stale plan"
    later_members = {r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (later_gid[0],)))}
    for gid in [r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE is_primary=1 AND id<>?", (later_gid[0],)))]:
        mem = {r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))}
        assert not (mem & later_members), "two primary groups for one enquiry (GP7(a))"
    assert all(row(db.execute("SELECT status FROM enquiries WHERE id=?", (eid,)))["status"] == "Proposed in group" for eid in later_members)
    # the later group keeps its primary flag and is not rewritten by this run, even though this run computed the same member set
    later_row = row(db.execute("SELECT is_primary, match_run_id FROM groups WHERE id=?", (later_gid[0],)))
    assert later_row["is_primary"] == 1 and later_row["match_run_id"] != out["run_id"]
    assert row(db.execute("SELECT COUNT(*) n FROM groups WHERE member_key=(SELECT member_key FROM groups WHERE id=?)", (later_gid[0],)))["n"] == 1
    # every member set the later writer also computed is its group here (not rewritten, not deleted); what this run wrote is only
    # the coverage-completion group for the people the later writer left without a proposal (9 Nov 2026: {third, fourth}),
    # which shares no member with the later writer's primary group
    written = rows(db.execute("SELECT id, member_key, is_primary FROM groups WHERE match_run_id=?", (out["run_id"],)))
    later_keys = {r["member_key"] for r in rows(db.execute("SELECT member_key FROM groups WHERE match_run_id<>? OR match_run_id IS NULL", (out["run_id"],)))}
    assert out["groups_total"] == len(written) <= 1
    for w in written:
        assert w["member_key"] not in later_keys
        assert not ({int(x) for x in w["member_key"].split(",")} & later_members)
    # every group in the table is the later writer's (its run wrote them all); this run added none and deleted none
    n_later = row(db.execute("SELECT COUNT(*) n FROM groups WHERE match_run_id=?", (later_row["match_run_id"],)))["n"]
    assert row(db.execute("SELECT COUNT(*) n FROM groups"))["n"] == n_later >= 1


def test_full_run_save_reserves_members_of_a_protected_primary_group_so_no_alternative_becomes_a_second_proposal(db, monkeypatch):
    """GP7(a) at the guard boundary: the existing primary group behind a discarded candidate is kept (its members' facts changed, the
    next run decides). A validated alternative sharing one of its UNCHANGED members must not become primary beside it."""
    from tests.test_launch_neutral import _apt, _pool
    _apt(db)
    ids = _pool(db)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    g1 = row(db.execute("SELECT * FROM groups WHERE is_primary=1 ORDER BY id LIMIT 1"))
    g1_members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g1["id"],)))]
    assert len(g1_members) == 2
    unchanged, changed = g1_members   # the fixture's builder seeds every candidate with the first arrival, so edit the OTHER member:
                                      # the alternatives {first, third} and {first, fourth} stay valid and share the reserved first member
    fired = []

    def edit_one_member_before_save(c, label):
        if label == "full run save" and not fired:
            fired.append(1)
            c.execute("UPDATE enquiries SET diet='Vegan' WHERE id=?", (changed,)); c.commit()   # a staff edit committed meanwhile
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, edit_one_member_before_save))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and out["complete"] is False and changed in out["discarded_enquiry_ids"]
    kept = row(db.execute("SELECT is_primary, status, match_run_id FROM groups WHERE id=?", (g1["id"],)))
    assert kept and kept["is_primary"] == 1 and kept["status"] == "Proposed" and kept["match_run_id"] != out["run_id"]   # protected, untouched
    primaries_with_unchanged = rows(db.execute("SELECT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE g.is_primary=1 AND gm.enquiry_id=?", (unchanged,)))
    assert [r["id"] for r in primaries_with_unchanged] == [g1["id"]], "an alternative became a second primary proposal for a reserved member"
    for eid in g1_members:
        assert row(db.execute("SELECT status FROM enquiries WHERE id=?", (eid,)))["status"] == "Proposed in group"
    # the validated alternatives sharing the reserved member were saved as alternatives only; the coverage-completion group of the two
    # people outside the protected group (9 Nov 2026) is the one primary this run may write, disjoint from the protected members
    assert out["groups_total"] == 3 and out["primary_groups"] == 1
    assert row(db.execute("SELECT COUNT(*) n FROM groups WHERE match_run_id=? AND is_primary=0", (out["run_id"],)))["n"] == 2
    new_primary = row(db.execute("SELECT member_key FROM groups WHERE match_run_id=? AND is_primary=1", (out["run_id"],)))
    assert new_primary and not ({int(x) for x in new_primary["member_key"].split(",")} & set(g1_members))
    assert row(db.execute("SELECT COUNT(*) n FROM groups WHERE is_primary=1"))["n"] == 2   # the protected group and the disjoint completion group (coverage, 9 Nov 2026)


def test_full_run_ownership_snapshot_precedes_the_pool_read_and_a_group_re_membered_after_it_is_not_deleted(db, monkeypatch):
    """Root's capture-boundary note: the ownership snapshot of proposed groups (and the T8 watermark) is taken BEFORE the pool and
    apartments are read, so no between-reads proposal can look like this run's. A group whose MEMBERSHIP another writer changed
    after the capture (a refill with a newly arrived enquiry; same id, new member_key) is another writer's: the save neither
    deletes it nor re-proposes its members."""
    from tests.test_launch_neutral import _apt, _enq, _pool
    _apt(db)
    _pool(db)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    alt = row(db.execute("SELECT * FROM groups WHERE is_primary=0 ORDER BY id LIMIT 1"))
    assert alt
    labels, fired, newcomer = [], [], []

    def refill_after_capture(c, label):
        labels.append(label)
        if label == "full run save" and not fired:
            fired.append(1)
            e5 = _enq(c, "Erik", "erik@example.com")   # arrives after the capture ...
            old_member = row(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id DESC LIMIT 1", (alt["id"],)))["enquiry_id"]
            c.execute("UPDATE group_members SET enquiry_id=? WHERE group_id=? AND enquiry_id=?", (e5["id"], alt["id"], old_member))   # ... and a refill puts them in
            others = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (alt["id"],)))]
            c.execute("UPDATE groups SET member_key=?, updated_at='2026-11-08T09:00:00' WHERE id=?", (",".join(str(i) for i in sorted(others)), alt["id"]))
            c.commit()
            newcomer.append(e5["id"])
    monkeypatch.setattr(dbmod, "retention_guard", _HookedGuard(dbmod.retention_guard, refill_after_capture))
    out = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert fired and not db.in_transaction
    kept = row(db.execute("SELECT id, status, member_key, match_run_id FROM groups WHERE id=?", (alt["id"],)))
    assert kept and kept["status"] == "Proposed" and str(newcomer[0]) in kept["member_key"].split(","), "a group re-membered after the capture was deleted or rewritten by the stale plan"
    assert kept["match_run_id"] != out["run_id"]
    seen_members: set = set()   # GP7(a) still: one primary per enquiry (a second, disjoint primary may exist since coverage completion, 9 Nov 2026)
    for g in rows(db.execute("SELECT id FROM groups WHERE is_primary=1")):
        mem = {r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))}
        assert not (mem & seen_members); seen_members |= mem
    assert 1 <= row(db.execute("SELECT COUNT(*) n FROM groups WHERE is_primary=1"))["n"] <= 2
    # the newcomer was not seen by this run and is not reported as covered by it
    assert newcomer[0] not in {r["enquiry_id"] for r in rows(db.execute("SELECT gm.enquiry_id FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE g.match_run_id=?", (out["run_id"],)))}


def test_full_run_keeps_a_proposal_created_between_the_pool_read_and_the_apartment_read(db, monkeypatch):
    """Root's reproduced between-reads interleaving: the pool is read empty; a writer then creates enquiries and completes a real
    per-apartment run; the full run must not delete that proposal (its ownership snapshot predates the pool read)."""
    from tests.test_launch_neutral import _apt, _pool
    apartment = _apt(db)
    original_load = matching.load_pool
    later, seen = [], []

    def load_then_arrive(conn, dataset="real", **kw):
        captured = original_load(conn, dataset, **kw)
        if not seen:
            seen.append(1)
            assert captured == []
            _pool(conn)
            matching.run_match(conn, apartment["id"], "other staff", dataset="test", use_ai=False)
            later.append(row(conn.execute("SELECT * FROM groups WHERE is_primary=1 ORDER BY id DESC LIMIT 1")))
        return captured
    monkeypatch.setattr(matching, "load_pool", load_then_arrive)
    out = matching.full_run(db, "root probe", dataset="test", use_ai=False)
    assert later and later[0] is not None
    saved = row(db.execute("SELECT * FROM groups WHERE id=?", (later[0]["id"],)))
    assert saved is not None and saved["status"] == "Proposed" and saved["is_primary"] == 1 and saved["match_run_id"] != out["run_id"]
    assert all(row(db.execute("SELECT status FROM enquiries WHERE id=?", (r["enquiry_id"],)))["status"] == "Proposed in group"
               for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (saved["id"],))))
    assert out["groups_total"] == 0 and out["pool_size"] == 0
    # the arrivals after the watermark are unseen by this run: the scheduler counts them as new applicants
    from server import scheduler
    assert scheduler.new_applicants_since(db, "test", scheduler.last_completed_run(db, "test")) >= 1
