"""Triage fix 4, bounded follow-up (9 Nov 2026): a RETAINED existing alternative (a still-valid Proposed member set the greedy
builder did not regenerate this run) used to be re-checked at its stored apartments only, so a newly listed, suitable and
higher-scoring apartment never reached its options while the retained primary did get it (GP8(b) refresh). Option completeness
must hold for every retained member set: the same eligible stock, the same rules and weights, no new member sets, no change of
primaries, rejection / lease / concurrency guards untouched. A changed option set of an already-reviewed alternative goes through
the SAME refresh path the primary already uses (ranking refreshed in the background; the rating stands). Fabricated doubles only."""
from server import ai_queue, matching
from server.db import rows, row
from tests.test_launch_neutral import _apt, _pool


def _options(db, gid):
    return [(int(r["apartment_id"]), float(r["rule_score"])) for r in rows(db.execute("SELECT apartment_id, rule_score FROM group_apartments WHERE group_id=? ORDER BY rule_rank", (gid,)))]


def test_retained_alternative_gets_a_newly_suitable_higher_scoring_apartment(db, monkeypatch):
    a1 = _apt(db)
    _pool(db)
    res1 = matching.full_run(db, "tester", "test")
    gs = rows(db.execute("SELECT id, member_key, is_primary, ai_status FROM groups WHERE status='Proposed' AND match_run_id=? ORDER BY id", (res1["run_id"],)))
    alts = [g for g in gs if not g["is_primary"]]
    prims = [g for g in gs if g["is_primary"]]
    assert alts and prims, "the fixture needs retained alternatives next to the proposals"
    assert all(_options(db, g["id"]) == [(int(a1["id"]), _options(db, g["id"])[0][1])] for g in gs)
    # one alternative was already reviewed (fabricated: no provider): its rating must stand while the ranking is refreshed
    reviewed = alts[0]
    from server import ai, rules
    db.execute("UPDATE groups SET ai_status='ok', ai_rating=7, ai_rationale='fine', ranking_basis='AI assessment', ready_at=?, ai_model=?, rule_version=?, evidence_hash='fabricated' WHERE id=?",
               (matching.now_iso(), ai.MODEL, rules.RULE_VERSION, reviewed["id"]))
    db.commit()
    # inventory change: an otherwise identical apartment available on the members' move-in date is listed -> no vacancy gap,
    # so a higher rule score for every member set (existing weights, nothing re-tuned)
    a2 = _apt(db, avail="2027-02-01", address="Unit 2 / 1 Neutral Street")
    # second run with the greedy builder producing NOTHING: every candidate below is a RETAINED existing group (isolation)
    monkeypatch.setattr(matching, "candidate_groups", lambda *a, **k: ([], 0))
    res2 = matching.full_run(db, "tester", "test")
    gs2 = {g["member_key"]: g for g in rows(db.execute("SELECT * FROM groups WHERE status='Proposed' AND match_run_id=? ORDER BY id", (res2["run_id"],)))}
    # same member sets, same ids, same primaries: nothing regrouped, nobody re-chosen
    assert {g["member_key"]: (g["id"], g["is_primary"]) for g in gs} == {k: (g["id"], g["is_primary"]) for k, g in gs2.items()}
    assert res2["groups_new"] == 0 and res2["groups_unchanged"] == len(gs)
    for g in gs:
        opts = _options(db, g["id"])
        assert [a for a, _ in opts] == [int(a2["id"]), int(a1["id"])], (g["is_primary"], opts)   # new apartment first (higher score), old kept
        assert opts[0][1] > opts[1][1]
        assert abs(float(gs2[g["member_key"]]["rule_score"]) - opts[0][1]) < 1e-9
    # the reviewed alternative keeps its rating and goes to the background ranking refresh (GP8(b)), exactly as a primary would
    r = row(db.execute("SELECT ai_status, ai_rating, ai_refresh FROM groups WHERE id=?", (reviewed["id"],)))
    assert r["ai_status"] == "ok" and r["ai_rating"] == 7 and r["ai_refresh"] == 1
    # unreviewed retained groups stay pending, no extra AI work invented
    assert all(row(db.execute("SELECT ai_status, ai_refresh FROM groups WHERE id=?", (g["id"],)))["ai_refresh"] in (0, None) for g in gs if g["id"] != reviewed["id"])


def test_retained_alternative_still_drops_when_no_apartment_fits(db, monkeypatch):
    """the kept-or-dropped behaviour is unchanged: when the only apartment is leased the retained alternatives go like before"""
    a1 = _apt(db)
    _pool(db)
    res1 = matching.full_run(db, "tester", "test")
    n = db.execute("SELECT COUNT(*) FROM groups WHERE status='Proposed' AND match_run_id=?", (res1["run_id"],)).fetchone()[0]
    assert n > 0
    db.execute("UPDATE apartments SET leased_at=? WHERE id=?", (matching.now_iso(), a1["id"])); db.commit()
    monkeypatch.setattr(matching, "candidate_groups", lambda *a, **k: ([], 0))
    res2 = matching.full_run(db, "tester", "test")
    assert res2["groups_total"] == 0
    assert db.execute("SELECT COUNT(*) FROM groups WHERE status='Proposed'").fetchone()[0] == 0
