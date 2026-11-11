"""T3(d) search repair (10 Nov 2026): bounded whole-pool alternatives / backtracking in matching.candidate_groups.

Pins, read-only against synthetic fixtures (no canonical data, no provider):
  * the reviewer's witness P003/P133 at A69 (frozen c200 run.db, enquiries 3/32/132/133 + apartment 69, answers copied verbatim):
    each person's former single greedy partner fails a complete-group rule, the pair (3,133) passes, and the repaired search finds it;
  * pair enumeration equals the pair oracle (every compatible rule-passing pair at the apartment is a candidate; none invented);
  * synthetic A/B/C: the best-affinity partner fails a complete-group rule, a lower-affinity rule-valid partner is found;
  * the adversarial larger-group case (a valid fourth seat only beyond the per-seat bound): the bounded search does NOT silently
    present itself as complete - it records an unresolved frontier (targets_truncated / seat_options_untried) that full_run surfaces
    as flat summary counters; a pairs-only pool records a zero frontier;
  * the bounds are module constants (no new weights, no new hard filter) and disjoint mode still places each person at most once.
"""
import itertools

from server import matching, rules
from tests.test_launch_neutral import _apt, _enq

TWIN = "Share a room with one other person"


def _created(db, eid, i):
    db.execute("UPDATE enquiries SET created_at=? WHERE id=?", (f"2026-11-01T10:{i:02d}:00+11:00", eid))


def _units(db):
    return {int(u.id): u for u in matching.load_pool(db, "test")}


def _find(apt, units_by_id, **kw):
    units = list(units_by_id.values())
    compat, _, _ = matching.pair_matrix(units)
    stats = {}
    cands, considered = matching.candidate_groups(apt, units, max_groups=None, seeds=None, compat=compat, stats=stats, **kw)
    return [frozenset(int(u.id) for u in g["units"]) for g in cands], stats, considered


def _pair_oracle(apt, units_by_id):
    out = set()
    for a, b in itertools.combinations(units_by_id.values(), 2):
        if matching._pair_ok(a, b) and rules.check_group(apt, matching.expand([a, b])).ok:
            out.add(frozenset((int(a.id), int(b.id))))
    return out


# --------------------------------------------------------------------------------------------------------------------------
# Witness: P003 / P133 at A69 (answers as stored in the frozen run; identities synthetic)
# --------------------------------------------------------------------------------------------------------------------------
W3 = dict(move_in="2027-03-15", move_in_flex="± 1 month", move_out="2028-03-15", move_out_flex="Open to extending", budget_option="2B2B-single",
          apt_sizes=["2 bedrooms"], mate_ideal="2", mate_max="2", rooms=["Own room"], room_first="Own room", bunk_ok="No",
          smoke="Yes, outside only (balcony or courtyard)", smoke_ok="Yes")
W32 = dict(move_in="2027-03-15", move_in_flex="Exact date", move_out="2028-03-15", move_out_flex="± 1 week", budget_option="2B2B-twin",
           apt_sizes=["2 bedrooms", "3 bedrooms"], mate_ideal="3", mate_max="5", rooms=[TWIN], room_first=TWIN, bunk_ok="Yes", smoke="No", smoke_ok="Yes")
W132 = dict(move_in="2027-03-01", move_in_flex="Exact date", move_out="2028-03-01", move_out_flex="± 1 week", budget_option="2B1B-twin",
            apt_sizes=["2 bedrooms", "3 bedrooms"], mate_ideal="2", mate_max="5", rooms=[TWIN], room_first=TWIN, bunk_ok="Yes", smoke="No", smoke_ok="No")
W133 = dict(move_in="2027-03-08", move_in_flex="± 1 week", move_out="2028-03-08", move_out_flex="Open to extending", budget_option="2B2B-twin",
            apt_sizes=["2 bedrooms"], mate_ideal="2", mate_max="3", rooms=["Own room", TWIN], room_first="Own room", bunk_ok="No", smoke="No", smoke_ok="Yes")


def _a69(db):
    return _apt(db, type_code="2B1B", avail="2027-02-19", beds=("1 king or 2 singles", "1 king or 2 singles", None), rent12=940.0, rent6=1015.0,
                address="Unit 105 / 336 Witness Street")


def test_witness_p003_p133_at_a69_found_by_backtracking(db):
    a = _a69(db)
    ids = {}
    for i, (tag, ans) in enumerate([("w3", W3), ("w32", W32), ("w132", W132), ("w133", W133)]):
        e = _enq(db, tag.upper(), f"{tag}@example.invalid", **ans)
        _created(db, e["id"], i)
        ids[tag] = e["id"]
    db.commit()
    apt = matching.apartment_from_row(a)
    u = _units(db)
    assert set(u) == set(ids.values())
    # each person's former single greedy partner fails a complete-group rule at this apartment ...
    assert not rules.check_group(apt, matching.expand([u[ids["w3"]], u[ids["w32"]]])).ok
    assert not rules.check_group(apt, matching.expand([u[ids["w133"]], u[ids["w132"]]])).ok
    # ... while the pair the old builder never tried passes every hard rule
    assert rules.check_group(apt, matching.expand([u[ids["w3"]], u[ids["w133"]]])).ok
    found, stats, considered = _find(apt, u)
    assert frozenset((ids["w3"], ids["w133"])) in found
    assert set(found) == _pair_oracle(apt, u)         # complete pair enumeration: nothing missed, nothing invented
    assert {k: v for k, v in stats.items() if k != "seat_options_date_infeasible"} == {"targets_truncated": 0, "seat_options_untried": 0, "targets_stopped_at_first_valid": 0}   # pairs are exhaustive: no frontier left behind
    assert stats["seat_options_date_infeasible"] >= 0   # date-screened options are removed BEFORE the per-seat bound, never counted as untried
    assert considered >= 3


# --------------------------------------------------------------------------------------------------------------------------
# Synthetic A/B/C: best-affinity partner fails, valid partner found
# --------------------------------------------------------------------------------------------------------------------------
def test_best_affinity_partner_fails_valid_partner_found(db):
    a = _apt(db, rent12=1150.0, rent6=1150.0)
    apt = matching.apartment_from_row(a)
    ea = _enq(db, "Alpha", "alpha@example.invalid", tidy="Very tidy", social="Quiet")
    # B: identical habits (top affinity) but a twin-share weekly maximum that cannot carry its half of this rent (A16 fails for the pair)
    eb = _enq(db, "Bravo", "bravo@example.invalid", tidy="Very tidy", social="Quiet", budget_option="2B1B-twin")
    # C: different habits (lower affinity), budget fine
    ec = _enq(db, "Charlie", "charlie@example.invalid", tidy="Relaxed", social="Sociable")
    for i, e in enumerate((ea, eb, ec)):
        _created(db, e["id"], i)
    db.commit()
    u = _units(db)
    A, B, C = u[ea["id"]], u[eb["id"]], u[ec["id"]]
    assert matching._pair_ok(A, B) and matching._pair_ok(A, C)
    assert matching._affinity(B, [A]) > matching._affinity(C, [A])          # the old builder's single pick for A was B
    assert not rules.check_group(apt, matching.expand([A, B])).ok            # ... and that pair fails a complete-group rule
    assert rules.check_group(apt, matching.expand([A, C])).ok
    found, stats, _ = _find(apt, u)
    assert frozenset((int(A.id), int(C.id))) in found and frozenset((int(A.id), int(B.id))) not in found
    assert set(found) == _pair_oracle(apt, u)


# --------------------------------------------------------------------------------------------------------------------------
# Adversarial larger group: a valid completion only beyond the per-seat bound -> recorded frontier, not a silent blind spot
# --------------------------------------------------------------------------------------------------------------------------
def _adversarial_pool(db):
    """Twin-share only (a 2B2B needs four). S (seed, earliest) + five decoys X1..X5 that share S's habits (top affinity) but whose
    weekly maxima make every group holding two or more decoys fail A16, + three valid partners Y1..Y3 with lower affinity. The
    only rule-passing groups holding S are S+Y+Y+Y and S+X+Y+Y; the latter sit beyond the per-seat bound in affinity order."""
    base = dict(rooms=[TWIN], room_first=TWIN, mate_ideal="3", mate_max="3")
    ids = {}
    e = _enq(db, "Seed", "seed@example.invalid", tidy="Very tidy", social="Quiet", budget_option="2B2B-twin", **base)
    _created(db, e["id"], 0)
    ids["S"] = e["id"]
    for i in range(1, 6):
        e = _enq(db, f"Decoy{i}", f"decoy{i}@example.invalid", tidy="Very tidy", social="Quiet", budget_option="2B1B-twin", **base)
        _created(db, e["id"], i)
        ids[f"X{i}"] = e["id"]
    for i in range(1, 4):
        e = _enq(db, f"Valid{i}", f"valid{i}@example.invalid", tidy="Relaxed", social="Sociable", school="Monash University – Clayton", budget_option="2B2B-twin", **base)
        _created(db, e["id"], 5 + i)
        ids[f"Y{i}"] = e["id"]
    db.commit()
    return ids


def test_adversarial_valid_partner_beyond_bound_records_frontier(db, monkeypatch):
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    apt = matching.apartment_from_row(a)
    ids = _adversarial_pool(db)
    u = _units(db)
    S = u[ids["S"]]
    X = [u[ids[f"X{i}"]] for i in range(1, 6)]
    Y = [u[ids[f"Y{i}"]] for i in range(1, 4)]
    # the fixture is what the docstring says: decoys first in affinity order, valid groups exist, decoy-heavy groups fail
    assert all(matching._affinity(x, [S]) > matching._affinity(y, [S]) for x in X for y in Y)
    assert rules.check_group(apt, matching.expand([S] + Y)).ok
    assert rules.check_group(apt, matching.expand([S, X[0], Y[0], Y[1]])).ok
    assert not rules.check_group(apt, matching.expand([S, X[0], X[1], Y[0]])).ok
    assert not rules.check_group(apt, matching.expand([S, X[0], X[1], X[2]])).ok
    assert set(_pair_oracle(apt, u)) == set()                                 # twin-only: no two-person group fits a 2B2B
    # the bounded search must either find a rule-passing group holding S or say that it did not finish looking
    found, stats, _ = _find(apt, u)
    valid_groups = [g for g in found if rules.check_group(apt, matching.expand([u[i] for i in g])).ok]
    assert valid_groups == found                                              # nothing returned fails the rules
    holds_s = [g for g in found if int(S.id) in g]
    assert holds_s or stats["targets_truncated"] > 0 or stats["seat_options_untried"] > 0
    # S's OWN seed search (seeds=1: S is the earliest arrival) walks decoy-first and runs out of budget before S+Y+Y+Y: the
    # frontier is reported, never a quiet "no group" (other seeds may still reach S from their side - that is the whole-pool part)
    units = list(u.values())
    cands_s, _ = matching.candidate_groups(apt, units, max_groups=None, seeds=1, stats=(stats_s := {}))
    assert not [g for g in cands_s if S in g["units"]]
    assert stats_s["targets_truncated"] > 0 and stats_s["seat_options_untried"] > 0
    # tightening the bounds (engineering constants, not rules) can only enlarge the recorded frontier, never hide it
    monkeypatch.setattr(matching, "SEARCH_CHECKS_PER_TARGET", 2)
    monkeypatch.setattr(matching, "SEARCH_ALTERNATIVES_PER_SEAT", 2)
    found2, stats2, _ = _find(apt, u)
    assert all(rules.check_group(apt, matching.expand([u[i] for i in g])).ok for g in found2)
    assert stats2["targets_truncated"] >= stats["targets_truncated"] > 0 or stats2["targets_truncated"] > 0
    assert stats2["seat_options_untried"] > 0
    # the summary of a rules-only run carries the frontier as flat counters inside the closed schema
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert {"search_targets_truncated", "search_seat_options_untried", "search_targets_stopped_at_first_valid", "search_larger_groups_exhaustive"} <= matching.SUMMARY_COUNTER_KEYS
    assert res["search_targets_truncated"] > 0 and res["search_seat_options_untried"] > 0
    assert res["search_larger_groups_exhaustive"] is False   # declared on every run: larger sizes are a bounded first-valid search
    # with a frontier > 0 an unplaced person is "search not exhausted", not proven infeasible (consumed by the C5 ledger)
    assert res["enquiries_unmatched"] + res["enquiries_in_any_group"] == 9


def test_pairs_only_pool_reports_zero_frontier(db):
    _apt(db)
    for i, name in enumerate(("Eve", "Finn", "Gus", "Hana")):
        e = _enq(db, name, f"{name.lower()}@example.invalid", mate_max="1")
        _created(db, e["id"], i)
    db.commit()
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert res["search_targets_truncated"] == 0 and res["search_seat_options_untried"] == 0 and res["search_targets_stopped_at_first_valid"] == 0
    assert res["search_larger_groups_exhaustive"] is False   # zero frontier counters still do not claim exhaustive larger-group search
    assert res["enquiries_in_primary_group"] == 4 and res["primary_groups"] == 2


def test_first_valid_larger_group_records_unexplored_branches(db):
    """A larger-group search that accepts its first rule-passing group with other branches left counts that frontier, so a zero
    in targets_truncated never reads as "every larger group was tried"."""
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    base = dict(rooms=[TWIN], room_first=TWIN, mate_ideal="3", mate_max="3", budget_option="2B2B-twin")   # six twin-sharers: many valid fours at a 2B2B
    for i, name in enumerate(("Ira", "Jon", "Kai", "Lou", "Mia", "Ned")):
        e = _enq(db, name, f"{name.lower()}@example.invalid", **base)
        _created(db, e["id"], i)
    db.commit()
    apt = matching.apartment_from_row(a)
    u = _units(db)
    stats = {}
    found, considered = matching.candidate_groups(apt, list(u.values()), max_groups=None, seeds=None, stats=stats)
    assert any(len(g["units"]) > 2 for g in found)
    assert stats["targets_stopped_at_first_valid"] > 0


def test_disjoint_mode_places_each_person_once_with_backtracking(db):
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    apt = matching.apartment_from_row(a)
    _adversarial_pool(db)
    u = _units(db)
    found, stats, _ = _find(apt, u, disjoint=True)
    seen = [i for g in found for i in g]
    assert len(seen) == len(set(seen))
    assert all(rules.check_group(apt, matching.expand([u[i] for i in g])).ok for g in found)


def test_bounds_are_module_constants_not_rules():
    assert isinstance(matching.SEARCH_CHECKS_PER_TARGET, int) and matching.SEARCH_CHECKS_PER_TARGET >= 1
    assert isinstance(matching.SEARCH_ALTERNATIVES_PER_SEAT, int) and matching.SEARCH_ALTERNATIVES_PER_SEAT >= 1
    assert not hasattr(rules, "SEARCH_CHECKS_PER_TARGET")   # the search bound is an engineering limit, not a Register rule


# --------------------------------------------------------------------------------------------------------------------------
# C1 repair (10 Nov 2026, reviewer finding W39): date-infeasible options must not consume the per-seat bound
# --------------------------------------------------------------------------------------------------------------------------
def _competition_pool(db):
    """Three mutually valid twin-sharers V1..V3 (exact 1 Feb start) created first, five decoys X1..X5 whose move-in window
    (20 Feb ± 1 month) overlaps 1 Feb but whose adult declaration (I11(d)) is for 20 Feb - so any group holding one of them has
    NO lawful start with an exact-1-Feb member (rules._lease_bounds fails) although every PAIR passes _pair_ok - and the valid
    final-seat unit S created LAST, so at equal affinity the decoys rank ahead of S for the fourth seat. Nothing else differs."""
    base = dict(rooms=[TWIN], room_first=TWIN, mate_ideal="3", mate_max="3", budget_option="2B2B-twin", move_in="2027-02-01", move_in_flex="Exact date")
    ids = {}
    for i in range(1, 4):
        e = _enq(db, f"Valid{i}", f"valid{i}@example.invalid", tidy="Relaxed", social="Sociable", **base)
        _created(db, e["id"], i)
        ids[f"V{i}"] = e["id"]
    for i in range(1, 6):
        e = _enq(db, f"Decoy{i}", f"decoy{i}@example.invalid", tidy="Very tidy", social="Quiet", **dict(base, move_in="2027-02-20", move_in_flex="± 1 month"))
        _created(db, e["id"], 10 + i)
        ids[f"X{i}"] = e["id"]
    e = _enq(db, "Seat", "seat@example.invalid", tidy="Very tidy", social="Quiet", **base)
    _created(db, e["id"], 20)
    ids["S"] = e["id"]
    db.commit()
    return ids


def test_date_infeasible_options_do_not_consume_the_per_seat_bound(db):
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    apt = matching.apartment_from_row(a)
    ids = _competition_pool(db)
    u = _units(db)
    V = [u[ids[f"V{i}"]] for i in range(1, 4)]
    X = [u[ids[f"X{i}"]] for i in range(1, 6)]
    S = u[ids["S"]]
    # fixture as described: every decoy pairs with V1 under _pair_ok, yet no group holding a decoy and an exact-1-Feb member has a lawful start
    assert all(matching._pair_ok(x, V[0]) for x in X)
    assert all(rules._lease_bounds(apt, [V[0], x])[2] for x in X)
    assert not rules._lease_bounds(apt, V + [S])[2]
    assert rules.check_group(apt, matching.expand(V + [S])).ok
    assert not any(rules.check_group(apt, matching.expand(V + [x])).ok for x in X)
    # the decoys outrank S for the final seat (equal affinity, earlier arrival) and outnumber the per-seat bound
    assert len(X) > matching.SEARCH_ALTERNATIVES_PER_SEAT
    assert all(x.extra["created_at"] < S.extra["created_at"] for x in X)
    assert all(matching._affinity(x, V) == matching._affinity(S, V) for x in X)
    found, stats, _ = _find(apt, u)
    assert frozenset(int(x.id) for x in V + [S]) in found          # the valid group is found: the decoys were screened out before the bound
    assert stats["seat_options_date_infeasible"] > 0                 # and the screen is reported, not silent
    assert stats["seat_options_untried"] == 0                        # S was never "beyond the bound"
    assert all(rules.check_group(apt, matching.expand([u[i] for i in g])).ok for g in found)
    res = matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert "search_seat_options_date_infeasible" in matching.SUMMARY_COUNTER_KEYS
    assert res["search_seat_options_date_infeasible"] > 0
    assert res["enquiries_in_primary_group"] == 4
