"""T6 revision loop: promotion of an approved, briefed alternative (review_loop.promote) - GP7 ownership guard, sibling
retirement, parent retained on every failure path, under-lock re-validation, two-connection competition (8 November 2026).

All AI traffic is a fabricated `_ask` double (tests.test_t6_fail_closed._reply_builder) and the staff-brief double from
conftest; no live call, no real people. SQLite only: the PostgreSQL branch of _begin_guarded is NOT exercised here.
"""
import json
import threading

from server import ai, ai_queue, matching, review_loop
from server import db as dbmod
from server.db import rows, row
from tests.test_launch_neutral import _apt, _pool
from tests.test_t6_fail_closed import _reply_builder, _good
from tests.conftest import fake_staff_briefs


def _revise_u2(g):
    if g["index"] == 0:
        return {**_good(g), "verdict": "revise", "replace_refs": ["U2"],
                "replace_reasons": [{"ref": "U2", "fact_ids": [f for f in g["fact_ids"] if f.startswith("U2.")][:1], "note": "routine clash with the rest of the group"}]}
    return _good(g)


def _setup(db, monkeypatch):
    """One 2B2B, four solo enquirers; run_match forms every compatible pair (T3(d), 10 Nov 2026: pairs are enumerated completely,
    so (1,2) and (3,4) are both proposals and the other four pairs are plain candidates). The fixture keeps ONE deterministic
    parent - the (1,2) proposal - and removes every other group (members 3 and 4 go back to 'New'), so the revision loop has to
    form the alternatives (1,3) and (1,4) itself. Returns (parent_id, [alt ids]) with the alternatives still pending."""
    a = _apt(db)
    ids = _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    parent_key = ",".join(str(i) for i in sorted((ids[0], ids[1])))
    parent = row(db.execute("SELECT id FROM groups WHERE match_run_id=? AND member_key=? AND is_primary=1", (res["run_id"], parent_key)))["id"]
    for g in rows(db.execute("SELECT id FROM groups WHERE match_run_id=? AND id<>?", (res["run_id"], parent))):
        for t in ("group_members", "group_apartments"):
            db.execute(f"DELETE FROM {t} WHERE group_id=?", (g["id"],))
        db.execute("DELETE FROM groups WHERE id=?", (g["id"],))
    db.execute("UPDATE enquiries SET status='New' WHERE status='Proposed in group' AND id NOT IN (SELECT enquiry_id FROM group_members WHERE group_id=?)", (parent,))
    db.commit()
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_revise_u2))
    ai_queue.process_pending(db)
    p = row(db.execute("SELECT * FROM groups WHERE id=?", (parent,)))
    assert p["ai_status"] == "revise" and p["status"] == "Proposed" and p["is_primary"] == 1
    alts = [g["id"] for g in rows(db.execute("SELECT id FROM groups WHERE revision_of=? ORDER BY id", (parent,)))]
    assert len(alts) == 2 and all(row(db.execute("SELECT is_primary, ai_status FROM groups WHERE id=?", (x,)))["is_primary"] == 0 for x in alts)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    return parent, alts


def _approve_and_brief_without_promoting(db, monkeypatch):
    calls = []
    monkeypatch.setattr(review_loop, "promote", lambda c, gid: calls.append(gid))
    # end-of-pass reselection (T3(d) C2b) hands ready alternatives to promote()'s guarded body inside its own transaction; a
    # handoff that returns nothing rolls that step back, so the alternatives stay approved-but-pending for these tests
    monkeypatch.setattr(review_loop, "_promote_guarded", lambda c, gid, *a, **k: calls.append(gid))
    ai_queue.process_pending(db)
    monkeypatch.undo()   # restores the real promote - and undoes every other patch, so re-patch what the rest of the test needs
    monkeypatch.setattr(ai, "staff_briefs", fake_staff_briefs)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    return calls


def _invariants(db):
    live = rows(db.execute("""SELECT gm.enquiry_id, COUNT(*) n FROM group_members gm JOIN groups g ON g.id=gm.group_id
                              WHERE g.status='Proposed' AND g.is_primary=1 GROUP BY gm.enquiry_id HAVING COUNT(*) > 1"""))
    assert live == [], f"a person sits in two primary proposals: {live}"
    orphan = rows(db.execute("SELECT id FROM groups WHERE status IN ('Proposed','Introduced') AND id NOT IN (SELECT group_id FROM group_members)"))
    assert orphan == []
    for e in rows(db.execute("SELECT id, status FROM enquiries")):
        owned = row(db.execute("SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.enquiry_id=? AND g.status='Proposed' AND g.is_primary=1", (e["id"],)))
        if e["status"] == "New":
            assert not owned, f"enquiry {e['id']} is New but owned by a primary proposal"


def test_promotion_supersedes_parent_once_and_retires_sibling(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    out = ai_queue.process_pending(db)   # both alternatives approved + briefed; promote runs after each brief
    assert out["ok"] == 2 and out["briefs_ok"] == 2
    groups = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    promoted = [g for g in groups.values() if g["status"] == "Proposed" and g["is_primary"] == 1]
    assert len(promoted) == 1 and promoted[0]["revision_of"] == parent and promoted[0]["ai_status"] == "ok" and promoted[0]["brief_status"] == "ok"
    other = [x for x in alts if x != promoted[0]["id"]][0]
    assert groups[parent]["status"] == "Dissolved" and f"Superseded by group {promoted[0]['id']}" in groups[parent]["held_reason"]
    assert groups[other]["status"] == "Dissolved" and groups[other]["ranking_basis"] == review_loop.SIBLING_BASIS and groups[other]["is_primary"] == 0
    st = {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))}
    kept = {int(x) for x in promoted[0]["member_key"].split(",")}
    assert st[2] == "New" and all(st[k] == "Proposed in group" for k in kept)     # replaced member back to the pool, no rejection
    assert rows(db.execute("SELECT * FROM member_rejections")) == []
    _invariants(db)
    # idempotent: promoting again does nothing and reports why
    assert review_loop.promote(db, promoted[0]["id"])["promoted"] is False
    assert review_loop.promote(db, other)["promoted"] is False


def test_sibling_alternatives_do_not_block_each_other_but_committed_members_do(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    # enquiry 3 is committed elsewhere: an Introduced group (not a sibling) owns them
    db.execute("INSERT INTO groups(apartment_id, status, member_key, is_primary, created_at, updated_at) VALUES (?, 'Introduced', '3,99', 1, 'x', 'x')",
               (row(db.execute("SELECT apartment_id FROM groups WHERE id=?", (parent,)))["apartment_id"],))
    gi = row(db.execute("SELECT id FROM groups WHERE member_key='3,99'"))["id"]
    db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?, 3)", (gi,))
    db.commit()
    ai_queue.process_pending(db)
    groups = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    with_3 = [x for x in alts if "3" in groups[x]["member_key"].split(",")][0]
    with_4 = [x for x in alts if x != with_3][0]
    assert groups[with_4]["status"] == "Proposed" and groups[with_4]["is_primary"] == 1, "the alternative whose members are free is promoted"
    assert groups[parent]["status"] == "Dissolved"
    # the blocked one was retired as a sibling (or kept non-primary with the busy reason if briefed first) - never a second primary
    assert groups[with_3]["is_primary"] == 0 and groups[with_3]["status"] in ("Dissolved", "Proposed")
    assert groups[gi]["status"] == "Introduced"
    _invariants(db)


def test_owners_elsewhere_ignores_non_primary_candidates(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    # both alternatives share member 1 and are non-primary Proposed: neither owns anyone
    assert review_loop.owners_elsewhere(db, [1, 3], [parent, alts[0]], need_primary=True) == []
    # the parent (primary) owns 1 and 2 when not excluded
    assert review_loop.owners_elsewhere(db, [1, 2, 3], [alts[0]], need_primary=True) == [1, 2]
    # a non-primary promotion (parent not primary) only cares about committed later states
    db.execute("UPDATE groups SET status='Introduced' WHERE id=?", (parent,)); db.commit()
    assert review_loop.owners_elsewhere(db, [1, 2], [alts[0]], need_primary=False) == [1, 2]


def test_external_commit_between_ai_and_promotion_keeps_parent_and_alternative(db, monkeypatch, tmp_path):
    parent, alts = _setup(db, monkeypatch)
    _approve_and_brief_without_promoting(db, monkeypatch)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (alts[0],)))
    assert g["ai_status"] == "ok" and g["brief_status"] == "ok" and ai_queue.is_ready(db, g)["ready"]
    # staff introduce the parent from ANOTHER connection after the AI approved the alternative
    other = dbmod.connect(str(tmp_path / "test.db"))
    other.execute("UPDATE groups SET status='Introduced', updated_at='2026-11-08T12:00:00+11:00' WHERE id=?", (parent,))
    other.commit()
    other.close()
    r = review_loop.promote(db, alts[0])
    assert r["promoted"] is False and f"ancestor {parent} is Introduced" in r["reason"]
    p = row(db.execute("SELECT status, is_primary FROM groups WHERE id=?", (parent,)))
    a = row(db.execute("SELECT status, is_primary, ai_status, brief_status FROM groups WHERE id=?", (alts[0],)))
    assert p == {"status": "Introduced", "is_primary": 1} and a == {"status": "Proposed", "is_primary": 0, "ai_status": "ok", "brief_status": "ok"}
    assert rows(db.execute("SELECT id FROM enquiries WHERE status='New'")) == [{"id": 3}, {"id": 4}]


def test_not_ready_alternative_is_never_promoted(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    _approve_and_brief_without_promoting(db, monkeypatch)
    db.execute("UPDATE groups SET brief_status='stale' WHERE id=?", (alts[0],)); db.commit()
    r = review_loop.promote(db, alts[0])
    assert r["promoted"] is False and "staff brief stale" in r["reason"]
    assert row(db.execute("SELECT status, ai_status, is_primary FROM groups WHERE id=?", (parent,))) == {"status": "Proposed", "ai_status": "revise", "is_primary": 1}
    # a member's compatibility answer changes after the review: evidence hash moves, no promotion
    db.execute("UPDATE enquiries SET routine='Night owl' WHERE id=1"); db.commit()
    r = review_loop.promote(db, alts[1])
    assert r["promoted"] is False and "facts changed" in r["reason"]
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (parent,)))["status"] == "Proposed"


def test_membership_change_between_prelock_read_and_lock_aborts(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    _approve_and_brief_without_promoting(db, monkeypatch)
    real = review_loop._begin_guarded

    def sneaky(c, gids, eids):
        real(c, gids, eids)
        c.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?, 4)", (alts[0],))   # someone adds a member right after the locks
    monkeypatch.setattr(review_loop, "_begin_guarded", sneaky)
    r = review_loop.promote(db, alts[0])
    assert r["promoted"] is False and r["reason"] == "membership changed under us"
    assert row(db.execute("SELECT status FROM groups WHERE id=?", (parent,)))["status"] == "Proposed"
    assert rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (alts[0],))) == [{"enquiry_id": 1}, {"enquiry_id": 3}]   # rolled back


def test_two_connections_promoting_competing_alternatives_yield_one_primary(db, monkeypatch, tmp_path):
    parent, alts = _setup(db, monkeypatch)
    _approve_and_brief_without_promoting(db, monkeypatch)
    results = {}
    barrier = threading.Barrier(2)

    def worker(gid):
        c = dbmod.connect(str(tmp_path / "test.db"))
        try:
            barrier.wait()
            results[gid] = review_loop.promote(c, gid)
        except Exception as e:   # pragma: no cover - surfaced by the assertions below
            results[gid] = {"promoted": False, "reason": f"exception {e!r}"}
        finally:
            c.close()
    ts = [threading.Thread(target=worker, args=(x,)) for x in alts]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=60)
    assert sorted(bool(r["promoted"]) for r in results.values()) == [False, True], results
    groups = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    primaries = [g for g in groups.values() if g["status"] == "Proposed" and g["is_primary"] == 1]
    assert len(primaries) == 1 and primaries[0]["id"] in alts
    loser = [x for x in alts if x != primaries[0]["id"]][0]
    assert groups[parent]["status"] == "Dissolved" and groups[loser]["status"] == "Dissolved" and groups[loser]["ranking_basis"] == review_loop.SIBLING_BASIS
    assert "exception" not in results[loser]["reason"]
    _invariants(db)


def test_payload_fails_closed_for_whole_group_when_a_member_is_unreadable(db, monkeypatch):
    parent, alts = _setup(db, monkeypatch)
    db.execute("UPDATE enquiries SET move_in='not a date' WHERE id=3"); db.commit()   # unreadable member record
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (alts[0],)))
    assert ai_queue._payload(db, g) is None
    out = ai_queue.process_pending(db)
    assert out["skipped_dead"] >= 1
    assert row(db.execute("SELECT ai_status FROM groups WHERE id=?", (alts[0],)))["ai_status"] == "failed"


# ---------------------------------------------------------------- two revision rounds (root, 8 Nov 2026): the grandchild must own the ORIGINAL proposal
def _revise_all(g):
    return {**_good(g), "verdict": "revise", "replace_refs": ["U2"],
            "replace_reasons": [{"ref": "U2", "fact_ids": [f for f in g["fact_ids"] if f.startswith("U2.")][:1], "note": "routine clash with the rest of the group"}]}


def _two_rounds(db, monkeypatch):
    """P(1,2) primary -> revise -> A(1,3), B(1,4) [round 1]. A fifth enquirer joins; both round-1 alternatives ask for another
    revision: A spawns C(1,5) [round 2, revision_of=A]; B finds its only alternative already live and is held.
    Returns (P, A, B, C) with C still pending."""
    from tests.test_launch_neutral import _enq, SWE
    parent, (alt_a, alt_b) = _setup(db, monkeypatch)
    e5 = _enq(db, "Eve", "eve@example.com", intro="Hi, I am Eve", **SWE)
    db.execute("UPDATE enquiries SET created_at=? WHERE id=?", ("2026-11-05T10:00:00+11:00", e5["id"])); db.commit()
    monkeypatch.setattr(ai, "_ask", _reply_builder(_revise_all))
    ai_queue.process_pending(db)
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    assert st[alt_a]["ai_status"] == "revise" and st[alt_a]["status"] == "Proposed" and st[alt_a]["is_primary"] == 0
    assert st[alt_b]["ai_status"] in ("revise", "held") and st[alt_b]["status"] == "Proposed"
    kids = rows(db.execute("SELECT * FROM groups WHERE revision_of=? ORDER BY id", (alt_a,)))
    assert len(kids) == 1 and kids[0]["revision_round"] == 2 and kids[0]["ai_status"] == "pending"
    c_id = kids[0]["id"]
    assert sorted(_members_of(db, c_id)) == [1, e5["id"]]
    assert review_loop._ancestors(db, c_id) == {"chain": [alt_a, parent], "state": "open", "blocker": None}
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    return parent, alt_a, alt_b, c_id, e5["id"]


def _members_of(db, gid):
    return [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))]


def test_round_two_candidate_inherits_the_original_primary_proposal(db, monkeypatch):
    parent, alt_a, alt_b, c_id, e5 = _two_rounds(db, monkeypatch)
    out = ai_queue.process_pending(db)   # C approved + briefed -> promote(C) walks A -> P
    assert out["ok"] == 1 and out["briefs_ok"] == 1
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    assert st[c_id]["status"] == "Proposed" and st[c_id]["is_primary"] == 1 and st[c_id]["held_reason"] is None
    assert st[parent]["status"] == "Dissolved" and st[parent]["is_primary"] == 0 and f"Superseded by group {c_id}" in st[parent]["held_reason"]
    assert st[alt_a]["status"] == "Dissolved" and f"Superseded by group {c_id}" in st[alt_a]["held_reason"]
    assert st[alt_b]["status"] == "Dissolved" and st[alt_b]["ranking_basis"] == review_loop.SIBLING_BASIS
    enq = {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))}
    assert enq[1] == "Proposed in group" and enq[e5] == "Proposed in group"
    assert enq[2] == "New" and enq[3] == "New" and enq[4] == "New"   # replaced across both rounds: back to the pool, no rejection records
    assert rows(db.execute("SELECT 1 FROM member_rejections")) == []
    assert ai_queue.is_ready(db, st[c_id])["ready"]
    _invariants(db)


def test_late_descendant_cannot_revive_a_superseded_ancestor(db, monkeypatch):
    """B (round 1) is approved first and takes over the original proposal; A and its pending child C are retired with it. If C's
    review nevertheless lands later (same connection, same queue), C stays a non-primary candidate and nothing is dissolved."""
    parent, alt_a, alt_b, c_id, e5 = _two_rounds(db, monkeypatch)
    # make B approvable: clear its held state and queue it fresh, while C is parked so B goes first
    db.execute("UPDATE groups SET ai_status='pending', held_reason=NULL, ai_refresh=0 WHERE id=?", (alt_b,))
    db.execute("UPDATE groups SET ai_status='held' WHERE id=?", (c_id,)); db.commit()
    ai_queue.process_pending(db)
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    assert st[alt_b]["is_primary"] == 1 and st[alt_b]["status"] == "Proposed"
    assert st[parent]["status"] == "Dissolved" and st[alt_a]["status"] == "Dissolved"
    assert st[c_id]["status"] == "Dissolved" and st[c_id]["ranking_basis"] == review_loop.SIBLING_BASIS, "a pending grandchild of a retired branch is retired with it"
    # even if a stale C row were somehow still Proposed and approved, the dead ancestor blocks revival
    db.execute("UPDATE groups SET status='Proposed', ai_status='pending', ranking_basis='x', held_reason=NULL WHERE id=?", (c_id,)); db.commit()
    ai_queue.process_pending(db)
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    assert st[c_id]["ai_status"] == "ok" and st[c_id]["is_primary"] == 0 and st[c_id]["status"] == "Proposed"
    assert review_loop._ancestors(db, c_id)["state"] == "blocked" and review_loop._ancestors(db, c_id)["blocker"]["id"] == alt_a
    assert st[alt_b]["is_primary"] == 1 and st[alt_b]["status"] == "Proposed"
    assert st[parent]["status"] == "Dissolved" and st[alt_a]["status"] == "Dissolved"
    assert ai_queue.can_circulate(db, st[c_id])["ok"] is False
    _invariants(db)


def test_promoted_primary_can_be_revised_again_without_reviving_its_predecessors(db, monkeypatch):
    """revision_of stays as provenance after a promotion. C (promoted primary, parents A/P Dissolved) is re-reviewed before
    introduction and asked to revise: its child D must take over C (nearest current proposal owner); A and P stay Dissolved and
    neither block nor revive. A re-approval of C itself is a no-op ('already promoted'), not a demotion."""
    from tests.test_launch_neutral import _enq, SWE
    parent, alt_a, alt_b, c_id, e5 = _two_rounds(db, monkeypatch)
    ai_queue.process_pending(db)
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    assert st[c_id]["is_primary"] == 1 and st[parent]["status"] == "Dissolved" and st[alt_a]["status"] == "Dissolved"
    assert review_loop._ancestors(db, c_id)["state"] == "already promoted"
    # a re-review of the promoted primary approving again changes nothing
    r = review_loop.promote(db, c_id)
    assert r == {"promoted": False, "reason": "already promoted"}
    assert row(db.execute("SELECT is_primary, status FROM groups WHERE id=?", (c_id,))) == {"is_primary": 1, "status": "Proposed"}
    # facts change for C's partner; the re-review asks to replace U2 (Eve) -> D(1, Fay), revision_of=C
    e6 = _enq(db, "Fay", "fay@example.com", intro="Hi, I am Fay", **SWE)
    db.execute("UPDATE groups SET ai_status='pending', ai_refresh=0, revision_round=0 WHERE id=?", (c_id,)); db.commit()
    monkeypatch.setattr(ai, "_ask", _reply_builder(_revise_all))
    ai_queue.process_pending(db)
    kids = [k["id"] for k in rows(db.execute("SELECT id FROM groups WHERE revision_of=? AND status='Proposed' ORDER BY id", (c_id,)))]
    assert kids and all(1 in _members_of(db, k) and e5 not in _members_of(db, k) for k in kids)   # 2, 3, 4 and Fay are all free again
    for k in kids:
        walk = review_loop._ancestors(db, k)
        assert walk["chain"] == [c_id] and walk["state"] == "owner", walk   # stops at C: A and P are history, not blockers
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    st = {g["id"]: g for g in rows(db.execute("SELECT * FROM groups"))}
    winners = [k for k in kids if st[k]["is_primary"] == 1 and st[k]["status"] == "Proposed"]
    assert len(winners) == 1
    assert all(st[k]["status"] == "Dissolved" and st[k]["ranking_basis"] == review_loop.SIBLING_BASIS for k in kids if k != winners[0])
    assert st[c_id]["status"] == "Dissolved" and st[c_id]["ranking_basis"] == review_loop.SUPERSEDED_BASIS
    assert st[parent]["status"] == "Dissolved" and st[alt_a]["status"] == "Dissolved" and st[alt_b]["status"] == "Dissolved"
    enq = {e["id"]: e["status"] for e in rows(db.execute("SELECT id, status FROM enquiries"))}
    assert enq[1] == "Proposed in group" and enq[e5] == "New"
    assert all(enq[m] == "Proposed in group" for m in _members_of(db, winners[0]))
    _invariants(db)


def test_begin_guarded_locks_rows_in_ascending_id_order_on_postgres():
    """Lock-order contract (root, 8 Nov 2026): competing branches must contend on the same first row. Checked against a recording
    stand-in for the PostgreSQL adapter; the real PostgreSQL behaviour is not evidenced here."""
    class Rec:
        backend = "postgres"
        def __init__(self):
            self.sql = []
        def execute(self, q, args=()):
            self.sql.append((q, tuple(args)))
    c = Rec()
    review_loop._begin_guarded(c, [9, 4, 1, 4], [7, 2, 5])
    assert [a for q, a in c.sql if q.startswith("UPDATE groups")] == [(1,), (4,), (9,)]
    sel = [(q, a) for q, a in c.sql if q.startswith("SELECT")]
    assert len(sel) == 1 and sel[0][1] == (2, 5, 7) and "ORDER BY id FOR UPDATE" in sel[0][0]
    assert [q[:6] for q, _ in c.sql] == ["UPDATE", "UPDATE", "UPDATE", "SELECT"]   # groups first, then enquiries


def test_apartment_run_proposes_disjoint_pairs_for_everyone_and_keeps_one_proposal_per_person(db):
    """Regression for the T3(d) pair enumeration (10 Nov 2026): a per-apartment run over four compatible solos writes every
    compatible pair as a candidate, proposes a DISJOINT pair of pairs covering all four, and never gives one person two
    proposals; the plain candidates stay alternatives and their members keep the pool status of their one proposal."""
    a = _apt(db)
    ids = _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    gs = rows(db.execute("SELECT id, member_key, is_primary FROM groups WHERE match_run_id=? ORDER BY id", (res["run_id"],)))
    assert len(gs) == 6 and len({g["member_key"] for g in gs}) == 6
    prim = [g for g in gs if g["is_primary"]]
    assert len(prim) == 2
    covered = [int(x) for g in prim for x in g["member_key"].split(",")]
    assert sorted(covered) == sorted(ids), "the two proposals cover all four people exactly once"
    _invariants(db)
    assert all(row(db.execute("SELECT status FROM enquiries WHERE id=?", (i,)))["status"] == "Proposed in group" for i in ids)
