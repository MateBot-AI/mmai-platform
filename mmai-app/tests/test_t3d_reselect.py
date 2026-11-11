"""T3(d) C2(b) (10 Nov 2026): post-AI reselection of proposals (review_loop.reselect_primaries, run at the end of every
ai_queue.process_pending pass). One 2B2B and four compatible solo enquirers: run_match enumerates every pair, (1,2) and (3,4) are
the proposals, the other four pairs are candidates; a fabricated `_ask` approves every review and the conftest brief double makes
every reviewed group GENUINELY ready (ai_queue.is_ready). The tests then put the proposals into the states the step must respect or
reconsider. No live call; SQLite only."""
import json

import pytest

from server import ai, ai_queue, matching, review_loop
from server.db import now_iso, row, rows
from tests.test_launch_neutral import _apt, _pool
from tests.test_t6_fail_closed import _good, _reply_builder
from tests.test_t6_promote import _invariants


def _setup(db, monkeypatch):
    a = _apt(db)
    ids = _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    out = ai_queue.process_pending(db)
    assert out["ok"] == 6 and out["briefs_ok"] == 6
    gs = {g["member_key"]: g for g in rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],)))}
    assert len(gs) == 6 and all(ai_queue.is_ready(db, g)["ready"] for g in gs.values())
    key = lambda *i: ",".join(str(ids[x - 1]) for x in sorted(i))
    assert gs[key(1, 2)]["is_primary"] == 1 and gs[key(3, 4)]["is_primary"] == 1 and sum(g["is_primary"] for g in gs.values()) == 2
    return ids, key, gs


def _state(db, gid, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    db.execute(f"UPDATE groups SET {sets}, updated_at=? WHERE id=?", (*cols.values(), now_iso(), gid))
    db.commit()


def _fail(db, gid):
    _state(db, gid, ai_status="failed", review_verdict=None, brief_status=None, ready_at=None, ranking_basis="Rules only (AI failed)")


def _primaries(db):
    return sorted(g["member_key"] for g in rows(db.execute("SELECT member_key FROM groups WHERE status='Proposed' AND is_primary=1")))


def test_failed_primaries_are_displaced_by_ready_alternatives_atomically(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(1, 2)]["id"]); _fail(db, gs[key(3, 4)]["id"])
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 4 and sorted(out["lost"]) == sorted([gs[key(1, 2)]["id"], gs[key(3, 4)]["id"]])
    assert out["ready_residents_before"] == 0 and out["ready_residents_after"] == 4 and out["residents_before"] == 4 and out["residents_after"] == 4
    assert out["truncated"] == 0 and out["holds"] == {} and out["ready_alternatives_blocked"] == 0
    assert _primaries(db) in ([key(1, 3), key(2, 4)], [key(1, 4), key(2, 3)])
    assert all(ai_queue.is_ready(db, g)["ready"] for g in rows(db.execute("SELECT * FROM groups WHERE is_primary=1")))
    _invariants(db)
    assert all(e["status"] == "Proposed in group" for e in rows(db.execute("SELECT status FROM enquiries WHERE id IN (%s)" % ",".join("?" * 4), ids)))
    assert rows(db.execute("SELECT * FROM audit_log WHERE action=?", (review_loop.RESELECT_AUDIT,)))
    # deterministic and idempotent: the same state gives the same answer and a second pass moves nothing
    assert review_loop.reselect_primaries(db, 1)["changed"] == 0


def test_ready_primaries_are_not_reshuffled_for_rule_score_alone(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    for k in (key(1, 3), key(2, 4), key(1, 4), key(2, 3)):
        _state(db, gs[k]["id"], rule_score=99.0)
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 0 and out["holds"] == {}          # ready incumbents compete (no 'ready' hold) and win the tie: equal ready coverage
    assert _primaries(db) == sorted([key(1, 2), key(3, 4)])


def test_ready_uncommunicated_primary_yields_to_a_larger_ready_cover_but_a_communicated_one_does_not(db, monkeypatch):
    """Root's control (10 Nov 2026): AB ready primary, CD failed; AC and BD current-ready alternatives cover four ready residents
    against AB's two -> AB (never put to anyone) gives way. The same state with AB's membership version introduced is blocked."""
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(3, 4)]["id"])
    for k in (key(1, 4), key(2, 3)):
        _fail(db, gs[k]["id"])                                   # leave exactly AC + BD as the ready alternatives
    out = review_loop.reselect_primaries(db, 1)
    assert out["ready_residents_before"] == 2 and out["ready_residents_after"] == 4 and out["residents_before"] == 4 and out["residents_after"] == 4
    assert out["changed"] == 4 and sorted(out["lost"]) == sorted([gs[key(1, 2)]["id"], gs[key(3, 4)]["id"]]) and out["holds"] == {}
    assert _primaries(db) == sorted([key(1, 3), key(2, 4)])
    _invariants(db)
    assert review_loop.reselect_primaries(db, 1)["changed"] == 0
    # ---- same facts, but AB was introduced: the commitment holds and nothing moves (CD failed keeps its place for coverage)
    _state(db, gs[key(1, 2)]["id"], is_primary=1); _state(db, gs[key(3, 4)]["id"], is_primary=1)
    _state(db, gs[key(1, 3)]["id"], is_primary=0); _state(db, gs[key(2, 4)]["id"], is_primary=0)
    cur = db.execute("UPDATE group_membership_versions SET introduced_at=?, introduced_by=?, anchor_kind='introduction' WHERE group_id=? AND superseded_at IS NULL",
                     (now_iso(), "bill", gs[key(1, 2)]["id"]))
    assert cur.rowcount == 1
    db.commit()
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 0 and out["holds"] == {"communicated": 1} and out["ready_residents_after"] == 2
    assert _primaries(db) == sorted([key(1, 2), key(3, 4)])


def test_pending_hold_is_temporary_and_its_blocked_ready_alternatives_are_reported(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    _state(db, gs[key(1, 2)]["id"], ai_status="pending", review_verdict=None, brief_status=None, ready_at=None, ai_attempts=0)   # queued, not judged yet
    _fail(db, gs[key(3, 4)]["id"])
    out = review_loop.reselect_primaries(db, 1)
    # (1,2) is held (engineering hold, disclosed); (1,3),(1,4),(2,3),(2,4) are ready alternatives blocked by it; (3,4) stays for coverage
    assert out["changed"] == 0 and out["holds"] == {"pending_hold": 1} and out["ready_alternatives_blocked"] == 4
    assert _primaries(db) == sorted([key(1, 2), key(3, 4)])
    # an in-flight (claimed) review is protected the same way, and reported under its own label
    _state(db, gs[key(1, 2)]["id"], ai_status="running")
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 0 and out["holds"] == {"in_flight": 1} and out["ready_alternatives_blocked"] == 4
    # the hold is released when the re-queue ceiling is reached (exhausted) ...
    _state(db, gs[key(1, 2)]["id"], ai_status="pending", ai_attempts=review_loop.RESELECT_HOLD_ATTEMPTS)
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 4 and out["holds"] == {} and out["ready_residents_after"] == 4
    _invariants(db)


def test_hold_released_on_failure_held_or_revise(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    for st in ("failed", "held", "revise"):
        # reset: (1,2),(3,4) primaries in state `st`, alternatives ready and non-primary
        for k, g in gs.items():
            db.execute("UPDATE groups SET is_primary=? WHERE id=?", (1 if k in (key(1, 2), key(3, 4)) else 0, g["id"]))
        db.commit()
        for k in (key(1, 2), key(3, 4)):
            _state(db, gs[k]["id"], ai_status=st, review_verdict=None, brief_status=None, ready_at=None, ai_attempts=0)
        out = review_loop.reselect_primaries(db, 1)
        assert out["changed"] == 4 and out["ready_residents_after"] == 4, st
        _invariants(db)


def test_communicated_primary_is_protected(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    gid = gs[key(1, 2)]["id"]
    _fail(db, gid); _fail(db, gs[key(3, 4)]["id"])
    # the group's current membership version (written by run_match) gets its introduction anchor, as anchor_introduction records it
    cur = db.execute("UPDATE group_membership_versions SET introduced_at=?, introduced_by=?, anchor_kind='introduction' WHERE group_id=? AND superseded_at IS NULL", (now_iso(), "bill", gid))
    assert cur.rowcount == 1
    db.commit()
    out = review_loop.reselect_primaries(db, 1)
    assert out["holds"] == {"communicated": 1}
    # (1,2) stays; (3,4) failed competes but every ready alternative overlaps 1 or 2 -> (3,4) keeps its place for coverage
    assert out["changed"] == 0 and _primaries(db) == sorted([key(1, 2), key(3, 4)])


def test_ready_revision_alternative_blocked_by_a_failed_owner_is_released_through_promote_only(db, monkeypatch):
    """Root's original-shape case (POST139 G32, 10 Nov 2026): a READY revision alternative (1,3) of the 'revise' primary (1,2) is
    blocked only because member 3 sits in the FAILED primary (3,4). Reselection releases that unsuccessful owner and hands the
    chain to promote() - the alternative is never flipped here, the ancestor's primary flag is never cleared before promote decides
    inheritance - and the result is one atomic promotion: (1,3) primary, (1,2) superseded, 2 and 4 back in the pool."""
    ids, key, gs = _setup(db, monkeypatch)
    parent, alt = gs[key(1, 2)]["id"], gs[key(1, 3)]["id"]
    _state(db, parent, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, alt, revision_of=parent, revision_round=1, is_primary=0)
    for k in (key(1, 4), key(2, 3), key(2, 4)):
        _fail(db, gs[k]["id"])
    _fail(db, gs[key(3, 4)]["id"])
    assert review_loop.promote(db, alt)["promoted"] is False          # today: blocked by the failed owner of 3
    assert row(db.execute("SELECT is_primary FROM groups WHERE id=?", (gs[key(3, 4)]["id"],)))["is_primary"] == 1
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote"] == [alt] and out["promoted"] and out["promoted"][0]["group_id"] == alt and out["promote_failed"] == []
    assert out["lost"] == [gs[key(3, 4)]["id"]]                           # the unsuccessful owner was released by this step
    assert out["gained"] == [gs[key(2, 4)]["id"]]                          # settle step after the handoff: the members the dissolved parent held are covered again (rules-stage, not ready)
    assert out["ready_residents_before"] == 0 and out["ready_residents_after"] == 2 and out["residents_after"] == 4
    assert "settle_after_promote" not in out
    g_alt, g_par, g_34 = (row(db.execute("SELECT * FROM groups WHERE id=?", (x,))) for x in (alt, parent, gs[key(3, 4)]["id"]))
    assert g_alt["is_primary"] == 1 and g_alt["status"] == "Proposed" and g_alt["revision_of"] == parent
    assert g_par["status"] == "Dissolved" and f"Superseded by group {alt}" in g_par["held_reason"]
    assert g_34["is_primary"] == 0 and g_34["status"] == "Proposed"
    st = {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))}
    assert all(st[i] == "Proposed in group" for i in ids)                 # 1,3 ready primary; 2,4 proposed (failed review) primary
    assert rows(db.execute("SELECT * FROM audit_log WHERE action='ai_revision_promoted'"))
    _invariants(db)
    assert review_loop.reselect_primaries(db, 1)["changed"] == 0        # settled: a second pass moves nothing and repeats no review


def test_not_ready_revision_alternatives_and_committed_owners_never_move_through_reselection(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    parent, alt = gs[key(1, 2)]["id"], gs[key(1, 3)]["id"]
    _state(db, parent, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, alt, revision_of=parent, revision_round=1, is_primary=0, brief_status="pending")   # approved but NOT current-ready
    _fail(db, gs[key(3, 4)]["id"])
    for k in (key(1, 4), key(2, 3), key(2, 4)):
        _fail(db, gs[k]["id"])
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote"] == [] and out["changed"] == 0
    assert row(db.execute("SELECT is_primary FROM groups WHERE id=?", (alt,)))["is_primary"] == 0
    assert row(db.execute("SELECT is_primary, status FROM groups WHERE id=?", (parent,)))["is_primary"] == 1
    # the alternative becomes ready, but the owner of 3 has been INTRODUCED: a committed owner is never released, nothing moves
    _state(db, alt, brief_status="ok")
    db.execute("UPDATE group_membership_versions SET introduced_at=?, introduced_by=?, anchor_kind='introduction' WHERE group_id=? AND superseded_at IS NULL", (now_iso(), "bill", gs[key(3, 4)]["id"]))
    db.commit()
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote"] == [] and out["changed"] == 0 and out["holds"] == {"communicated": 1}
    assert row(db.execute("SELECT is_primary, status FROM groups WHERE id=?", (parent,)))["is_primary"] == 1
    assert row(db.execute("SELECT is_primary FROM groups WHERE id=?", (alt,)))["is_primary"] == 0
    _invariants(db)


def test_two_descendants_of_one_chain_are_never_both_selected(db, monkeypatch):
    """Ancestor-chain conflict is represented in the selection even when two terminal descendants have disjoint member sets."""
    ids, key, gs = _setup(db, monkeypatch)
    parent = gs[key(1, 2)]["id"]
    a1, a2 = gs[key(1, 3)]["id"], gs[key(2, 4)]["id"]          # disjoint members, same parent
    _state(db, parent, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, a1, revision_of=parent, revision_round=1, is_primary=0)
    _state(db, a2, revision_of=parent, revision_round=1, is_primary=0)
    _fail(db, gs[key(3, 4)]["id"]); _fail(db, gs[key(1, 4)]["id"]); _fail(db, gs[key(2, 3)]["id"])
    out = review_loop.reselect_primaries(db, 1)
    assert len(out["promote"]) == 1 and len(out["promoted"]) == 1
    prom = out["promote"][0]
    other = a2 if prom == a1 else a1
    assert row(db.execute("SELECT is_primary, status FROM groups WHERE id=?", (prom,)))["is_primary"] == 1
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (other,)))["status"] == "Dissolved"   # retired by promote() as the competing branch
    _invariants(db)


def test_alternative_failing_hard_rules_today_is_never_proposed_and_trade_off_is_reported(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(1, 2)]["id"]); _fail(db, gs[key(3, 4)]["id"])
    # the deterministic hard-rule gate is applied to every candidate that would become primary: here it fails for every group
    # holding person 3 (the gate itself is tested in its own module; this test checks that reselection obeys it)
    real = ai_queue._hard_rules_still_pass
    monkeypatch.setattr(ai_queue, "_hard_rules_still_pass", lambda c, g: (False, "hard rule") if str(ids[2]) in (g.get("member_key") or "").split(",") else real(c, g))
    out = review_loop.reselect_primaries(db, 1)
    prim = _primaries(db)
    assert key(1, 3) not in prim and key(2, 3) not in prim and len(prim) == 1 and prim[0] in (key(1, 4), key(2, 4))
    # ready residents first: one ready pair beats keeping two failed pairs; the coverage trade-off is reported, not hidden
    assert out["ready_residents_before"] == 0 and out["ready_residents_after"] == 2 and out["residents_before"] == 4 and out["residents_after"] == 2
    assert row(db.execute("SELECT status FROM enquiries WHERE id=?", (ids[2],)))["status"] == "New"   # person 3 is proposed nowhere now
    _invariants(db)


def test_process_pending_runs_reselection_at_the_end_of_the_pass(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(1, 2)]["id"]); _fail(db, gs[key(3, 4)]["id"])
    out = ai_queue.process_pending(db)
    assert out["groups"] == 0 and set(out["reselect"]) == {"real", "test"} and out["reselect"]["test"]["changed"] == 4 and out["reselect"]["real"]["changed"] == 0
    _invariants(db)


def test_concurrent_change_aborts_without_writing(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(1, 2)]["id"]); _fail(db, gs[key(3, 4)]["id"])
    real = review_loop._begin_guarded

    def racing(c, group_ids, enquiry_ids):
        real(c, group_ids, enquiry_ids)
        c.execute("UPDATE groups SET updated_at=? WHERE id=?", ("2030-01-01T00:00:00+11:00", gs[key(1, 3)]["id"]))   # another writer moved a row
    monkeypatch.setattr(review_loop, "_begin_guarded", racing)
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 0 and out["reason"] == "groups changed under us"
    assert _primaries(db) == sorted([key(1, 2), key(3, 4)])
    assert not rows(db.execute("SELECT * FROM audit_log WHERE action=?", (review_loop.RESELECT_AUDIT,)))


def test_ready_incumbent_is_never_traded_for_more_not_yet_ready_residents(db, monkeypatch):
    """Root's counterexample (10 Nov 2026): ready AB + failed CD (four proposed) must NOT yield to ready AC + failed BEF (five
    proposed) - ready coverage would still be two, and a ready proposal is displaced only when ready coverage grows."""
    from tests.test_launch_neutral import _enq
    ids, key, gs = _setup(db, monkeypatch)
    _fail(db, gs[key(3, 4)]["id"])
    for k in (key(1, 4), key(2, 3), key(2, 4)):
        _fail(db, gs[k]["id"])                                  # AC is the only ready alternative
    e5 = _enq(db, "Eli", "eli@example.com")["id"]; e6 = _enq(db, "Fay", "fay@example.com")["id"]
    when = now_iso()
    db.execute("INSERT INTO groups(apartment_id, status, member_key, is_primary, ai_status, rule_score, created_at, updated_at) VALUES (?, 'Proposed', ?, 0, 'failed', 50.0, ?, ?)",
               (gs[key(1, 2)]["apartment_id"], ",".join(str(x) for x in sorted((ids[1], e5, e6))), when, when))
    bef = db.execute("SELECT id FROM groups WHERE member_key=?", (",".join(str(x) for x in sorted((ids[1], e5, e6))),)).fetchone()[0]
    for eid in (ids[1], e5, e6):
        db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?, ?)", (bef, eid))
    db.commit()
    monkeypatch.setattr(ai_queue, "_hard_rules_still_pass", lambda c, g: (True, ""))   # the synthetic BEF would fail the real gate; the test is about the objective
    pre_state = sorted((r["id"], r["is_primary"]) for r in rows(db.execute("SELECT id, is_primary FROM groups")))
    out = review_loop.reselect_primaries(db, 1)
    assert out["changed"] == 0 and out["ready_residents_before"] == 2 and out["ready_residents_after"] == 2 and out["residents_after"] == 4
    assert sorted((r["id"], r["is_primary"]) for r in rows(db.execute("SELECT id, is_primary FROM groups"))) == pre_state
    assert _primaries(db) == sorted([key(1, 2), key(3, 4)])
    # the pure objective says the same: the incumbent set outranks the five-resident alternative
    ready = {int(g["id"]): False for g in rows(db.execute("SELECT id FROM groups"))}
    ready[gs[key(1, 2)]["id"]] = True; ready[gs[key(1, 3)]["id"]] = True
    was = {int(g["id"]): bool(g["is_primary"]) for g in rows(db.execute("SELECT id, is_primary FROM groups"))}
    cand = {int(g["id"]): g for g in review_loop._reselect_candidates(db, 1)}
    incumbent = [cand[gs[key(1, 2)]["id"]], cand[gs[key(3, 4)]["id"]]]
    alternative = [cand[gs[key(1, 3)]["id"]], cand[bef]]
    assert review_loop._reselect_score(incumbent, ready, was)[0] == review_loop._reselect_score(alternative, ready, was)[0] == 2
    assert review_loop._reselect_score(incumbent, ready, was) > review_loop._reselect_score(alternative, ready, was)
    _invariants(db)


def test_end_of_pass_reselection_reports_unsettled_after_bounded_retries(db, monkeypatch):
    ids, key, gs = _setup(db, monkeypatch)
    calls = []
    def flaky(c, is_test):
        calls.append(is_test)
        return {"changed": 0, "gained": [], "lost": [], "truncated": 0, "reason": "groups changed under us"}
    monkeypatch.setattr(review_loop, "reselect_primaries", flaky)
    res = ai_queue._reselect_after_pass(db)
    assert res["test"]["settled"] is False and res["real"]["settled"] is False
    assert calls.count(1) == 1 + ai_queue.RESELECT_RETRIES and calls.count(0) == 1 + ai_queue.RESELECT_RETRIES
    # a clean step is settled, and process_pending surfaces the per-data-set result
    monkeypatch.undo()
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    out = ai_queue.process_pending(db)
    assert out["reselect"]["test"]["settled"] is True and out["reselect"]["test"]["changed"] == 0


def test_two_level_chain_keeps_the_intermediate_non_primary_and_promotes_in_the_same_transaction(db, monkeypatch):
    """Root's original two-level shape (G32 -> G30 -> G21, 10 Nov 2026): the 'revise' PRIMARY (1,2) has a non-primary 'revise'
    revision (1,3), which in turn has a READY revision (1,4); member 4 sits in the FAILED primary (3,4). The intermediate overlaps
    its parent, so it must never be flagged primary by reselection (that would make the chain look like two primaries per person
    and abort). Only the flags that were primary are retained for the ancestors, the failed owner is released, and the ready
    descendant is promoted in the same transaction: (1,4) primary, (1,2) and (1,3) superseded."""
    ids, key, gs = _setup(db, monkeypatch)
    g21, g30, g32, g11 = gs[key(1, 2)]["id"], gs[key(1, 3)]["id"], gs[key(1, 4)]["id"], gs[key(3, 4)]["id"]
    _state(db, g21, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, g30, revision_of=g21, revision_round=1, is_primary=0, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, g32, revision_of=g30, revision_round=2, is_primary=0)
    _fail(db, g11); _fail(db, gs[key(2, 3)]["id"]); _fail(db, gs[key(2, 4)]["id"])
    assert review_loop._ancestors(db, g32)["chain"] == [g30, g21]
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote"] == [g32] and [x["group_id"] for x in out["promoted"]] == [g32] and out["promote_failed"] == []
    assert out["lost"] == [g11] and out["ready_residents_before"] == 0 and out["ready_residents_after"] == 2
    st = {x: row(db.execute("SELECT * FROM groups WHERE id=?", (x,))) for x in (g21, g30, g32, g11)}
    assert st[g32]["is_primary"] == 1 and st[g32]["status"] == "Proposed"
    assert st[g21]["status"] == "Dissolved" and st[g30]["status"] == "Dissolved" and st[g30]["is_primary"] == 0
    assert st[g11]["is_primary"] == 0 and st[g11]["status"] == "Proposed"
    _invariants(db)
    assert review_loop.reselect_primaries(db, 1)["promote"] == []


def test_failed_handoff_rolls_back_the_whole_step_so_no_owner_is_left_released_for_a_promotion_that_did_not_happen(db, monkeypatch):
    """Same shape as the release-through-promote test: the FAILED primary (3,4) would be released so that the ready alternative (1,3)
    can be promoted over its 'revise' parent (1,2). The handoff runs in the SAME transaction as the release; if it does not
    complete, the release rolls back with it - the database never shows a primary demoted for a promotion that did not happen,
    nothing is audited as done, and the step reports what actually happened (nothing). The next pass completes it.
    (With the four-person pair fixture a handoff can never displace a READY incumbent on its own: an alternative owns its whole
    chain's members for disjointness, so the released owners here are the unsuccessful ones; the rollback path is the same code.)"""
    ids, key, gs = _setup(db, monkeypatch)
    parent, alt, g34 = gs[key(1, 2)]["id"], gs[key(1, 3)]["id"], gs[key(3, 4)]["id"]
    _state(db, parent, ai_status="revise", review_verdict="revise", brief_status=None, ready_at=None)
    _state(db, alt, revision_of=parent, revision_round=1, is_primary=0)
    for k in (key(1, 4), key(2, 3), key(2, 4), key(3, 4)):
        _fail(db, gs[k]["id"])
    before = {g["id"]: (g["is_primary"], g["status"], g["held_reason"], g["updated_at"]) for g in rows(db.execute("SELECT * FROM groups"))}
    est = {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))}
    real = review_loop._promote_guarded
    monkeypatch.setattr(review_loop, "_promote_guarded", lambda *a, **k: (_ for _ in ()).throw(review_loop._Abort({"promoted": False, "reason": "alternative changed under us"})))
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote"] == [alt] and out["promoted"] == [] and out["promote_failed"] == [{"group_id": alt, "reason": "alternative changed under us"}]
    assert out["changed"] == 0 and out["gained"] == [] and out["lost"] == [] and "rolled back" in out["reason"]
    assert "ready_residents_after" not in out                                         # no actual-after count is reported for a step that did nothing
    assert {g["id"]: (g["is_primary"], g["status"], g["held_reason"], g["updated_at"]) for g in rows(db.execute("SELECT * FROM groups"))} == before
    assert {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))} == est
    assert row(db.execute("SELECT is_primary FROM groups WHERE id=?", (g34,)))["is_primary"] == 1   # the owner of 3 was NOT left released
    assert not rows(db.execute("SELECT 1 FROM audit_log WHERE action IN (?, 'ai_revision_promoted')", (review_loop.RESELECT_AUDIT,)))
    # a blocked handoff that returns (no _Abort) rolls back the same way
    monkeypatch.setattr(review_loop, "_promote_guarded", lambda *a, **k: {"promoted": False, "reason": "members [9] committed elsewhere"})
    out = review_loop.reselect_primaries(db, 1)
    assert out["promote_failed"] == [{"group_id": alt, "reason": "members [9] committed elsewhere"}] and out["changed"] == 0
    assert {g["id"]: (g["is_primary"], g["status"], g["held_reason"], g["updated_at"]) for g in rows(db.execute("SELECT * FROM groups"))} == before
    # end-of-pass driver: a rolled-back step is not 'settled'
    monkeypatch.setattr(review_loop, "_promote_guarded", lambda *a, **k: None)
    r = ai_queue._reselect_after_pass(db)["test"]
    assert r["settled"] is False and r["promote_failed"] and r["changed"] == 0
    assert {g["id"]: (g["is_primary"], g["status"], g["held_reason"], g["updated_at"]) for g in rows(db.execute("SELECT * FROM groups"))} == before
    monkeypatch.setattr(review_loop, "_promote_guarded", real)
    out = review_loop.reselect_primaries(db, 1)
    assert [x["group_id"] for x in out["promoted"]] == [alt] and out["lost"] == [g34] and out["gained"] == [gs[key(2, 4)]["id"]]
    assert out["ready_residents_before"] == 0 and out["ready_residents_after"] == 2 and out["residents_after"] == 4
    _invariants(db)


def test_unsettled_reselection_is_visible_in_status_and_retried_by_the_worker_without_ai_work(db, monkeypatch):
    """Root's drain gap (10 Nov 2026): a pass that ends unsettled must leave a durable-for-process signal (`reselect_unsettled`,
    `reselect_last` in ai_queue.status), so a drain check with pending/running/briefs/notes all 0 is NOT read as drained; the
    worker's next wake retries the reselection even with no AI work (no provider call, no hot loop), and the signal clears only
    once BOTH data sets settle."""
    # process start: nothing checked yet is UNKNOWN, never 'settled' (conservative init: unsettled True, reselect_last None)
    init = ai_queue._initial_state()
    assert init["reselect_unsettled"] is True and init["reselect_last"] is None
    ids, key, gs = _setup(db, monkeypatch)
    assert ai_queue.status(db)["reselect_unsettled"] is False and ai_queue.status(db)["reselect_last"]["settled"] is True   # _setup's pass settled
    calls = []
    def flaky(c, is_test, *a, **k):
        calls.append(is_test)
        return {"changed": 0, "gained": [], "lost": [], "truncated": 0, "promote": [], "promoted": [], "promote_failed": [], "reason": "groups changed under us"}
    real = review_loop.reselect_primaries
    monkeypatch.setattr(review_loop, "reselect_primaries", flaky)
    monkeypatch.setattr(ai_queue, "_consume_homebot_reviews", lambda c: {"skipped": True})
    out = ai_queue.process_pending(db)   # nothing pending: the pass ends with the reselection only
    assert out["settled"] is False and out["groups"] == 0
    st = ai_queue.status(db)
    assert st["reselect_unsettled"] is True and st["reselect_last"]["settled"] is False and st["reselect_last"]["test"]["reason"] == "groups changed under us"
    assert st["pending"] == 0 and st["running"] == 0 and st["briefs_pending"] == 0 and st["notes_pending"] == 0   # every other drain counter is 0: only the signal says 'not drained'
    assert "simulated" not in str(st["reselect_last"]) and set(st["reselect_last"]["real"]) == {"settled", "changed", "promoted", "promote_failed", "reason"}
    n = len(calls)
    it = ai_queue._iteration(db)   # worker wake with no AI work: the reselection is retried once, no kick (the idle poll re-wakes it)
    assert it["reselect_retried"] is True and it["matebot_batches"] == 0 and it["more"] is False
    assert len(calls) == n + 2 * (1 + ai_queue.RESELECT_RETRIES) and ai_queue.status(db)["reselect_unsettled"] is True
    # one data set settling is not enough
    monkeypatch.setattr(review_loop, "reselect_primaries", lambda c, is_test, *a, **k: real(c, is_test) if is_test == 1 else flaky(c, is_test))
    assert ai_queue._iteration(db)["reselect_retried"] is True and ai_queue.status(db)["reselect_unsettled"] is True
    assert ai_queue.status(db)["reselect_last"]["test"]["settled"] is True and ai_queue.status(db)["reselect_last"]["real"]["settled"] is False
    monkeypatch.setattr(review_loop, "reselect_primaries", real)
    it = ai_queue._iteration(db)
    assert it["reselect_retried"] is True
    st = ai_queue.status(db)
    assert st["reselect_unsettled"] is False and st["reselect_last"]["settled"] is True
    assert ai_queue._iteration(db)["reselect_retried"] is False   # settled: a quiet wake does nothing
    # an UNEXPECTED exception before the reselection completes: the signal was set before the attempt, so it stays set, the pass does
    # not crash, the reason is closed (no exception words), and the next idle wake retries and settles
    def boom(c, is_test, *a, **k):
        raise RuntimeError("simulated unexpected failure 0x42")
    monkeypatch.setattr(review_loop, "reselect_primaries", boom)
    out = ai_queue.process_pending(db)
    st = ai_queue.status(db)
    assert out["settled"] is False and st["reselect_unsettled"] is True and st["reselect_last"]["settled"] is False
    assert st["reselect_last"]["real"]["reason"] == ai_queue.RESELECT_INCOMPLETE and "simulated" not in str(st["reselect_last"]) and "0x42" not in str(st["reselect_last"])
    assert ai_queue._iteration(db)["reselect_retried"] is True and ai_queue.status(db)["reselect_unsettled"] is True
    monkeypatch.setattr(review_loop, "reselect_primaries", real)
    assert ai_queue._iteration(db)["reselect_retried"] is True
    st = ai_queue.status(db)
    assert st["reselect_unsettled"] is False and st["reselect_last"]["settled"] is True and st["reselect_last"]["real"]["settled"] and st["reselect_last"]["test"]["settled"]
    # a pass that throws BEFORE reaching the reselection (after possibly changing assessments) owes one: marked when busy is taken,
    # so the earlier settled reselect_last is no certificate once `finally` clears busy
    real_now, armed = ai_queue.now_iso, [True]
    def early():
        if armed[0]:
            armed[0] = False
            raise RuntimeError("simulated early failure")
        return real_now()
    monkeypatch.setattr(ai_queue, "now_iso", early)   # the pass's first step after taking `busy` fails
    with pytest.raises(RuntimeError):
        ai_queue.process_pending(db)
    st = ai_queue.status(db)
    assert st["busy"] is False and st["reselect_unsettled"] is True and st["reselect_last"]["settled"] is True   # last completed result kept, but not fresh
    assert ai_queue._iteration(db)["reselect_retried"] is True and ai_queue.status(db)["reselect_unsettled"] is False
    # restart simulation: a fresh process state is unknown until the worker's first idle wake reselects (no AI work needed)
    monkeypatch.setattr(ai_queue, "STATE", ai_queue._initial_state())
    assert ai_queue.status(db)["reselect_unsettled"] is True and ai_queue.status(db)["reselect_last"] is None
    assert ai_queue._iteration(db)["reselect_retried"] is True and ai_queue.status(db)["reselect_unsettled"] is False
