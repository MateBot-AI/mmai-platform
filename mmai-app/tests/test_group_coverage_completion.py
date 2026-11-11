"""Group coverage completion (full_run step 2b, 9 Nov 2026). Synthetic data, isolated SQLite, no provider.

The seed-and-greedy builder proposes, for every person, the group it likes best for them; in a pool of equals every candidate
holds the same early arrival, GP7(a) makes only one of them the proposal and everyone else is stranded although disjoint
rule-passing groups plainly exist (root probe reviews/20261109-group-coverage-probe.py). The fix re-runs the SAME rules-only
builder over the people not yet in a primary, round by round. Pinned here:
  * nine equal solo enquiries, one available two-bedroom: four disjoint primaries, eight people covered, one left over who still
    appears in alternatives (GP8: a proposal holds nothing, so the same apartment is proposed to each group);
  * every group added by a remainder round passes rules.check_group at its apartment and is disjoint from the primaries chosen
    before it (nothing is forced; no weights, no reservation);
  * stability: a second run keeps the same primary member sets (existing proposals first), groups_unchanged counts them;
  * GP7 pair rejections are honoured inside remainder rounds (the builder's pair check is the same);
  * linked parties are kept whole (root's control) and the summary counts per enquiry;
  * the summary carries coverage_rounds; progress phase label is in the closed vocabulary; enquiries_unmatched keeps its meaning
    (people in no candidate at all), separate from enquiries_in_primary_group.
"""
from server import matching, rules
from server.db import rows
from tests.test_launch_neutral import _apt, _enq


def _solo_pool(db, n, prefix="Solo"):
    ids = []
    for i in range(n):
        e = _enq(db, f"{prefix}{i}", f"{prefix.lower()}{i}@example.invalid")
        db.execute("UPDATE enquiries SET created_at=? WHERE id=?", (f"2026-11-01T10:{i:02d}:00+11:00", e["id"]))
        ids.append(e["id"])
    db.commit()
    return ids


def _primaries(db):
    return rows(db.execute("SELECT gm.enquiry_id, gm.group_id, g.member_key FROM group_members gm JOIN groups g ON g.id=gm.group_id "
                           "WHERE g.is_primary=1 AND g.status='Proposed' ORDER BY gm.group_id, gm.enquiry_id"))


def test_nine_equal_solos_one_two_bedroom_four_disjoint_primaries_one_left_over(db):
    a = _apt(db)
    ids = _solo_pool(db, 9)
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    prim = _primaries(db)
    covered = [r["enquiry_id"] for r in prim]
    assert len(covered) == len(set(covered)) == 8 and len({r["group_id"] for r in prim}) == 4
    left = set(ids) - set(covered)
    assert len(left) == 1
    # the left-over person is in alternatives (someone to propose when a group needs a replacement), not forgotten
    alts = rows(db.execute("SELECT DISTINCT gm.enquiry_id FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE g.is_primary=0 AND g.status='Proposed'"))
    assert left <= {r["enquiry_id"] for r in alts}
    assert res["primary_groups"] == 4 and res["enquiries_in_primary_group"] == 8 and res["enquiries_in_any_group"] == 9
    assert res["enquiries_unmatched"] == 0           # unchanged meaning: nobody is without ANY candidate
    # T3(d) search repair (10 Nov 2026): two-unit groups are now enumerated completely in step 2, so this pool of equals is
    # covered before the remainder rounds run (coverage_rounds may be 0); the rounds still exist for pools the bounded larger
    # search leaves uncovered (see test_remainder_round_still_runs_for_bounded_larger_groups).
    assert res["coverage_rounds"] >= 0 and "coverage_rounds" in matching.SUMMARY_COUNTER_KEYS
    assert "Looking for groups among people not yet proposed" in matching.RUN_PHASE_LABELS
    # every primary passes the rules at the apartment it is proposed for
    apt = matching.apartment_from_row(a)
    by_id = {int(u.id): u for u in matching.expand(matching.load_pool(db, "test"))}
    for key in {r["member_key"] for r in prim}:
        members = [by_id[int(x)] for x in key.split(",")]
        assert rules.check_group(apt, members).ok
    for e in ids:
        assert db.execute("SELECT status FROM enquiries WHERE id=?", (e,)).fetchone()[0] == ("Proposed in group" if e in covered else "New")


def test_second_run_keeps_the_same_primaries(db):
    _apt(db)
    _solo_pool(db, 8)
    first = matching.full_run(db, "tester", dataset="test", use_ai=False)
    keys1 = {r["member_key"] for r in _primaries(db)}
    second = matching.full_run(db, "tester", dataset="test", use_ai=False)
    keys2 = {r["member_key"] for r in _primaries(db)}
    assert keys1 == keys2 and len(keys1) == 4
    assert first["primary_groups"] == second["primary_groups"] == 4
    assert second["groups_unchanged"] >= 4 and second["groups_new"] == 0


def _twin_pool(db, n):
    """n equal solos who accept ONLY a twin-share room (so a 2B2B needs four of them: pairs and triples fail the bedding
    allocation), created in a fixed order - the starvation shape for the BOUNDED larger-group search (T3(d), 10 Nov 2026)."""
    ids = []
    for i in range(n):
        e = _enq(db, f"Twin{i}", f"twin{i}@example.invalid", rooms=["Share a room with one other person"], room_first="Share a room with one other person",
                 budget_option="2B2B-twin", mate_ideal="3", mate_max="3")
        db.execute("UPDATE enquiries SET created_at=? WHERE id=?", (f"2026-11-01T10:{i:02d}:00+11:00", e["id"]))
        ids.append(e["id"])
    db.commit()
    return ids


def test_remainder_rounds_add_only_rule_passing_disjoint_groups(db):
    # T3(d) search repair (10 Nov 2026): pairs are now enumerated completely, so eight equal own-room solos no longer show the
    # starvation shape in step 2. Four-person twin-share groups still come from the bounded search, every candidate holds the
    # earliest arrivals, and GP7(a) can make only one of them the proposal: the remainder rounds remain the safety net here.
    a = _apt(db)
    _twin_pool(db, 8)
    units = matching.load_pool(db, "test")
    apt = matching.apartment_from_row(a)
    compat, _, _ = matching.pair_matrix(units)
    cands, considered = matching.candidate_groups(apt, units, max_groups=None, seeds=None, compat=compat)
    groups = {}
    for g in cands:
        key = matching.member_key(g["units"])
        entry = groups.setdefault(key, {"key": key, "units": g["units"], "apartments": []})
        entry["apartments"].append(matching._apt_option(a, apt, g))
    for e in groups.values():
        matching._finalise_entry(e, 3)
    before = set(groups)
    glist0 = sorted(groups.values(), key=lambda e: -e["rule_score"])
    matching._choose_primaries(glist0, {})
    first_primary = {frozenset(int(u.id) for u in e["units"]) for e in glist0 if e["is_primary"]}
    assert len(first_primary) == 1                                   # the starvation shape: one star, one proposal
    box = [considered]
    rounds = matching._complete_coverage(groups, units, [(a, apt)], {}, compat, 3, box)
    assert rounds >= 1 and box[0] > considered
    added = [groups[k] for k in set(groups) - before]
    assert added
    for e in added:
        assert rules.check_group(apt, e["units"]).ok
        assert e["apartments"] and e["apartments"][0]["rule_rank"] == 1 and "members_for_ai" in e
        ids = {int(u.id) for u in e["units"]}
        assert all(not (ids & p) for p in first_primary)              # disjoint from what was already proposed
    glist = sorted(groups.values(), key=lambda e: -e["rule_score"])
    matching._choose_primaries(glist, {})
    covered = [int(u.id) for e in glist if e["is_primary"] for u in e["units"]]
    assert len(covered) == len(set(covered)) == 8
    assert matching.uncovered_units(units, glist) == []
    # a second completion pass over a complete plan adds nothing (idempotent)
    assert matching._complete_coverage(groups, units, [(a, apt)], {}, compat, 3, box) == 0


def test_remainder_round_still_runs_for_bounded_larger_groups(db):
    """Full run on the twin-share pool: step 2's bounded four-person search leaves people uncovered, the remainder round covers
    them, and the summary says so (coverage_rounds >= 1) - the mechanism the search repair did not remove."""
    _apt(db)
    ids = _twin_pool(db, 8)
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    prim = _primaries(db)
    covered = {r["enquiry_id"] for r in prim}
    assert covered == set(ids) and len({r["group_id"] for r in prim}) == 2
    assert res["coverage_rounds"] >= 1 and res["enquiries_in_primary_group"] == 8


def test_remainder_rounds_honour_gp7_pair_rejections(db):
    _apt(db)
    ids = _solo_pool(db, 4)
    # 0 rejected 1 and 2 (so 0 can only pair with 3); 1 and 2 pair with each other
    matching.load_rejections(db)
    for other in (ids[1], ids[2]):
        db.execute("INSERT INTO member_rejections(group_id, rejected_enquiry_id, other_enquiry_id, at, staff_member) VALUES (NULL,?,?,?,?)", (ids[0], other, "2026-11-01T00:00:00+11:00", "tester"))
    db.commit()
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    prim = _primaries(db)
    sets = {}
    for r in prim:
        sets.setdefault(r["group_id"], set()).add(r["enquiry_id"])
    assert len(sets) == 2 and all(len(s) == 2 for s in sets.values())
    for s in sets.values():
        assert not ({ids[0], ids[1]} <= s) and not ({ids[0], ids[2]} <= s)
    assert {ids[0], ids[3]} in sets.values() and {ids[1], ids[2]} in sets.values()


def test_too_few_people_left_stops_without_forcing_a_group(db):
    _apt(db)
    _solo_pool(db, 3)
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    prim = _primaries(db)
    assert len({r["group_id"] for r in prim}) == 1 and len(prim) == 2
    assert res["enquiries_in_primary_group"] == 2 and res["enquiries_in_any_group"] == 3
    # a single left-over person cannot form a group of MIN_GROUP on their own: no round is forced
    assert rules.MIN_GROUP == 2
