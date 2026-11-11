"""T3(d) C2 (10 Nov 2026): coverage-first disjoint selection of proposals (matching._choose_primaries / select_disjoint).
Pure-function tests on synthetic candidate entries: no database, no provider.
  * the disjoint set covering the MOST people wins over the single best-scoring group (the former primary-first/rule-score greedy);
  * an existing primary whose AI review is approved keeps its place (conservative stability boundary, labelled); a pending or
    failed existing primary competes like any other candidate;
  * pre_taken people (owned by a later writer) are never proposed again;
  * the bounded branch-and-bound never returns less coverage than its greedy incumbent and reports truncation honestly;
  * deterministic: the same input gives the same selection."""
from types import SimpleNamespace

from server import matching


def _u(i, size=1):
    """A unit (one enquiry form): the lead plus size-1 party members, person ids as rules.Unit.people carries them."""
    people = [SimpleNamespace(id=str(i))] + [SimpleNamespace(id=f"{i}m{k}") for k in range(1, size)]
    return SimpleNamespace(id=str(i), size=size, people=people)


def _e(key, members, score=1.0, sizes=None):
    sizes = sizes or {}
    return {"key": key, "units": [_u(i, sizes.get(i, 1)) for i in members], "rule_score": score, "apartments": []}


def _prim(glist):
    return sorted(e["key"] for e in glist if e["is_primary"])


def test_coverage_beats_single_best_score():
    # old greedy: best score first -> {1,2,3,4} (4 people). Coverage-first: {1,2}+{3,5}+{4,6} = 6 people.
    glist = [_e("big", [1, 2, 3, 4], 9.0), _e("a", [1, 2], 1.0), _e("b", [3, 5], 1.0), _e("c", [4, 6], 1.0)]
    stats = {}
    matching._choose_primaries(glist, {}, stats=stats)
    assert _prim(glist) == ["a", "b", "c"]
    assert stats["selection_truncated"] == 0 and stats["selection_nodes"] >= 1
    # equal coverage: the selection holding the highest-scoring group wins (former "best rule score wins"), not the most groups
    glist = [_e("big", [1, 2, 3, 4], 9.0), _e("a", [1, 2], 5.0), _e("b", [3, 4], 5.0)]
    matching._choose_primaries(glist, {})
    assert _prim(glist) == ["big"]


READY = {"is_primary": True, "ai_status": "ok", "review_verdict": "approve", "brief_status": "ok", "ai_refresh": 0, "_ready": True}   # annotate_ready's stamp


def test_protected_ready_primary_keeps_place_but_every_other_primary_competes():
    glist = [_e("old", [1, 2, 3, 4], 2.0), _e("a", [1, 5], 1.0), _e("b", [2, 6], 1.0), _e("c", [3, 7], 1.0), _e("d", [4, 8], 1.0)]
    matching._choose_primaries(glist, {"old": dict(READY)})
    assert _prim(glist) == ["old"]           # genuinely ready proposal stays; nothing else is disjoint from it
    matching._choose_primaries(glist, {"old": dict(READY, _ready=True)})
    assert _prim(glist) == ["old"]           # the run-time annotation is what decides when present
    # NOT protected: 'ok' alone (brief pending / failed, refresh outstanding, stale annotation) and every unsuccessful state
    # annotate_ready stamps _ready=False for every one of these in production (brief pending / failed / missing, refresh outstanding,
    # no verdict); the two unstamped rows at the end fail closed (no status-field approximation of readiness)
    unstamped = {k: v for k, v in READY.items() if k != "_ready"}
    not_ready = [dict(unstamped, brief_status="pending", _ready=False), dict(unstamped, brief_status="failed", _ready=False), dict(unstamped, brief_status=None, _ready=False),
                 dict(unstamped, ai_refresh=1, _ready=False), dict(unstamped, review_verdict=None, _ready=False), dict(READY, _ready=False),
                 unstamped, {"is_primary": True, "ai_status": "ok"}]
    not_ready += [{"is_primary": True, "ai_status": st} for st in ("pending", "running", "failed", "held", "revise", "skipped", None)]
    for ex in not_ready:
        glist = [_e("old", [1, 2, 3, 4], 2.0), _e("a", [1, 5], 1.0), _e("b", [2, 6], 1.0), _e("c", [3, 7], 1.0), _e("d", [4, 8], 1.0)]
        matching._choose_primaries(glist, {"old": ex})
        assert _prim(glist) == ["a", "b", "c", "d"], ex   # more people covered: unsuccessful / undetermined provisional work is re-packed


def test_ready_groups_break_coverage_ties_but_ok_alone_does_not():
    glist = [_e("x", [1, 2], 1.0), _e("y", [1, 2], 1.0)]   # same people, same score: the one that is genuinely ready wins
    matching._choose_primaries(glist, {"y": dict(READY, is_primary=False)})
    assert _prim(glist) == ["y"]
    glist = [_e("x", [1, 2], 1.0), _e("y", [1, 2], 1.0)]
    matching._choose_primaries(glist, {"y": {"is_primary": False, "ai_status": "ok"}})   # 'ok' without briefs is not readiness
    assert _prim(glist) == ["x"]
    # fail closed: approved-looking status fields WITHOUT the annotate_ready stamp are not readiness either (no status-field approximation)
    glist = [_e("x", [1, 2], 1.0), _e("y", [1, 2], 1.0)]
    unstamped = {k: v for k, v in READY.items() if k != "_ready"}
    matching._choose_primaries(glist, {"y": dict(unstamped, is_primary=False)})
    assert _prim(glist) == ["x"]
    assert not matching._protected_primary(_e("y", [1, 2]), {"y": unstamped}) and matching._protected_primary(_e("y", [1, 2]), {"y": READY})


def test_existing_proposal_is_retained_only_at_equal_coverage_readiness_and_quality():
    # stability tie (labelled engineering fallback): same residents covered, nobody ready, SAME scores -> the existing proposals stay
    glist = [_e("p", [1, 2], 1.0), _e("q", [3, 4], 1.0), _e("r", [1, 3], 1.0), _e("s", [2, 4], 1.0)]
    pend = {"p": {"is_primary": True, "ai_status": "pending"}, "q": {"is_primary": True, "ai_status": "pending"}}
    matching._choose_primaries(glist, pend)
    assert _prim(glist) == ["p", "q"]
    # ... but a better rule score at equal coverage and readiness is a quality improvement, not a needless swap: no quality-blind incumbency
    glist = [_e("p", [1, 2], 1.0), _e("q", [3, 4], 1.0), _e("r", [1, 3], 5.0), _e("s", [2, 4], 5.0)]
    matching._choose_primaries(glist, pend)
    assert _prim(glist) == ["r", "s"]
    # ... and more coverage always beats both
    glist = [_e("p", [1, 2], 9.0), _e("r", [1, 3], 0.5), _e("s", [2, 4], 0.5)]
    matching._choose_primaries(glist, {"p": {"is_primary": True, "ai_status": "pending"}})
    assert _prim(glist) == ["r", "s"]


def test_objective_counts_residents_not_forms_and_never_splits_a_party():
    # unit 1 is a couple form (2 residents). X={1,2} = 2 forms / 3 residents; Y={2,3} = 2 forms / 2 residents with the better score:
    # the per-person objective takes X (3 people housed), and the couple travels whole (disjointness is by form / party ownership)
    glist = [_e("x", [1, 2], 1.0, sizes={1: 2}), _e("y", [2, 3], 9.0)]
    matching._choose_primaries(glist, {})
    assert _prim(glist) == ["x"]
    # equal residents: X={1(couple),2} = 3 vs Y={2,3,4} = 3 (they share person 2, so only one can be chosen) -> the tie falls to score best-first
    glist = [_e("x", [1, 2], 1.0, sizes={1: 2}), _e("y", [2, 3, 4], 9.0)]
    matching._choose_primaries(glist, {})
    assert _prim(glist) == ["y"]   # equal residents, nobody ready -> the selection holding the best score (9.0) wins
    # an alone person and a coupled form are each counted once per resident: never a form counted as a person
    glist = [_e("c", [1, 2], 1.0, sizes={1: 2, 2: 2}), _e("d", [3, 4, 5], 1.0)]
    matching._choose_primaries(glist, {})
    assert _prim(glist) == ["c", "d"]   # disjoint anyway; both proposed (4 + 3 residents)
    assert len(matching._people_ids(glist[0]["units"])) == 4 and len(matching._people_ids(glist[1]["units"])) == 3


def test_ready_residents_beat_ready_group_count_at_equal_total_coverage():
    # A = ready seven-person group + E (pending pair) = 9 residents, 7 ready. B,C,D = three ready pairs + G (pending triple) = 9 residents, 6 ready.
    glist = [_e("A", [1, 2, 3, 4, 5, 6, 7], 1.0), _e("E", [8, 9], 1.0), _e("B", [1, 2], 1.0), _e("C", [3, 4], 1.0), _e("D", [5, 6], 1.0), _e("G", [7, 8, 9], 1.0)]
    existing = {k: dict(READY, is_primary=False) for k in ("A", "B", "C", "D")}
    matching._choose_primaries(glist, existing)
    assert _prim(glist) == ["A", "E"], "7 ready residents beat 6 ready residents in three groups"
    # and with the pairs READY but the seven-person group only 'ok' without briefs, the pairs win (6 ready residents vs 0)
    existing["A"] = {"is_primary": False, "ai_status": "ok"}
    glist = [_e("A", [1, 2, 3, 4, 5, 6, 7], 1.0), _e("E", [8, 9], 1.0), _e("B", [1, 2], 1.0), _e("C", [3, 4], 1.0), _e("D", [5, 6], 1.0), _e("G", [7, 8, 9], 1.0)]
    matching._choose_primaries(glist, existing)
    assert _prim(glist) == ["B", "C", "D", "G"]


def test_pre_taken_people_are_never_proposed_again():
    glist = [_e("a", [1, 2], 1.0), _e("b", [3, 4], 1.0)]
    matching._choose_primaries(glist, {}, pre_taken={2})
    assert _prim(glist) == ["b"]
    assert all(e["is_primary"] in (True, False) for e in glist) and "_ids" not in glist[0]


def test_bounded_search_never_below_greedy_and_reports_truncation(monkeypatch):
    # 40 overlapping candidates over 30 people: a tiny node budget truncates; the result must still be a valid disjoint
    # selection at least as good as the greedy incumbent, and say that it was truncated
    glist = [_e(f"g{i}", [i % 30, (i * 7 + 1) % 30, (i * 11 + 2) % 30], 1.0 + (i % 5) / 10) for i in range(40)]
    full_stats = {}
    matching._choose_primaries([dict(e) for e in glist], {}, stats=full_stats)
    greedy_cover = 0
    taken = set()
    for e in sorted(glist, key=lambda e: (-len(set(int(u.id) for u in e["units"])), 1, -e["rule_score"], e["key"])):
        ids = {int(u.id) for u in e["units"]}
        if not ids & taken:
            taken |= ids
            greedy_cover += len(ids)
    small = {}
    sel = [dict(e) for e in glist]
    chosen = matching.select_disjoint(sorted(sel, key=lambda e: (-len(e["units"]), -e["rule_score"], e["key"])),
                                      ids_of=lambda e: frozenset(int(u.id) for u in e["units"]), weight_ids=lambda e: frozenset(int(u.id) for u in e["units"]),
                                      score=lambda ch: (len(frozenset().union(*[frozenset(int(u.id) for u in e["units"]) for e in ch])) if ch else 0,), budget=5, stats=small)
    covered = [int(u.id) for e in chosen for u in e["units"]]
    assert len(covered) == len(set(covered))            # disjoint
    assert len(covered) >= greedy_cover                 # never below the greedy fallback
    assert small["selection_truncated"] == 1 and small["selection_nodes"] >= 5
    assert full_stats["selection_truncated"] in (0, 1)   # the default budget may or may not finish: it must say which


def test_selection_is_deterministic():
    glist = [_e(f"g{i}", [i, (i + 1) % 9], 1.0) for i in range(9)]
    a = [dict(e) for e in glist]; b = [dict(e) for e in reversed(glist)]
    matching._choose_primaries(a, {}); matching._choose_primaries(b, {})
    assert _prim(a) == _prim(b) and len(_prim(a)) == 4


# --------------------------------------------------------------------------------------------------------------------------
# C2 repair (10 Nov 2026, reviewer finding): unit-branching search order - stored candidates that support a larger cover must
# be found within the node budget, not lost to traversal order
# --------------------------------------------------------------------------------------------------------------------------
def _cover_instance():
    """30 people. A perfect cover exists only as 15 PAIRS (2k+1, 2k+2); 13 higher-scoring, larger TRIPLES (2k+2, 2k+3, 2k+5) cross
    the pairs and come first in every greedy order (more residents, higher score). No person is in more than three candidates."""
    triples = [_e(f"t{k}", [2 * k + 2, 2 * k + 3, 2 * k + 5], 9.0) for k in range(13)]
    pairs = [_e(f"p{k}", [2 * k + 1, 2 * k + 2], 1.0) for k in range(15)]
    return triples + pairs


def _run(glist, budget=None):
    sel = [dict(e) for e in glist]
    stats = {}
    kw = {"budget": budget} if budget is not None else {}
    ids = lambda e: frozenset(int(u.id) for u in e["units"])
    chosen = matching.select_disjoint(sorted(sel, key=lambda e: (-len(e["units"]), -e["rule_score"], e["key"])), ids_of=ids, weight_ids=ids,
                                      score=lambda ch: (len(frozenset().union(*[ids(e) for e in ch])) if ch else 0, tuple(sorted((e["rule_score"] for e in ch), reverse=True))),
                                      stats=stats, **kw)
    covered = [int(u.id) for e in chosen for u in e["units"]]
    assert len(covered) == len(set(covered))
    return chosen, len(covered), stats


def test_unit_branching_finds_the_full_cover_the_greedy_orders_miss():
    glist = _cover_instance()
    # greedy in the selection order takes crossing triples and strands people
    taken, greedy_cover = set(), 0
    for e in sorted(glist, key=lambda e: (-len(e["units"]), -e["rule_score"], e["key"])):
        ids = {int(u.id) for u in e["units"]}
        if not ids & taken:
            taken |= ids
            greedy_cover += len(ids)
    assert greedy_cover < 30
    chosen, covered, stats = _run(glist, budget=400)
    assert covered == 30                                  # the cover the stored candidates already support
    assert stats["selection_truncated"] == 0              # proven within a small budget: a unit with one option is forced, not searched
    assert {e["key"] for e in chosen} == {f"p{k}" for k in range(15)}
    full = [dict(e) for e in glist]
    s = {}
    matching._choose_primaries(full, {}, stats=s)
    assert set(_prim(full)) == {f"p{k}" for k in range(15)} and s["selection_truncated"] == 0


def test_bounded_selection_matches_brute_force_on_small_random_instances():
    import itertools, random
    rnd = random.Random(20261110)
    for trial in range(12):
        n_people, n_cands = 9, rnd.randint(6, 11)
        glist = [_e(f"g{i}", sorted(rnd.sample(range(n_people), rnd.choice((2, 2, 3, 4)))), round(rnd.uniform(1, 9), 3)) for i in range(n_cands)]
        ids = lambda e: frozenset(int(u.id) for u in e["units"])
        best = None
        for r in range(len(glist) + 1):
            for combo in itertools.combinations(glist, r):
                if sum(len(ids(e)) for e in combo) != len(frozenset().union(*[ids(e) for e in combo]) if combo else frozenset()):
                    continue
                sc = (len(frozenset().union(*[ids(e) for e in combo])) if combo else 0, tuple(sorted((e["rule_score"] for e in combo), reverse=True)))
                best = sc if best is None or sc > best else best
        chosen, covered, stats = _run(glist)
        got = (covered, tuple(sorted((e["rule_score"] for e in chosen), reverse=True)))
        assert got == best, (trial, got, best)
        assert stats["selection_truncated"] == 0
