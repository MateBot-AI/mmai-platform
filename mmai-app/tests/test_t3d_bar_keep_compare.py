"""T3(d) C2 (10 Nov 2026): reproducible keep-this-group comparison for the GP9(d) bar case in test_data_and_api
(test_bar_from_matching_gp9c_gp9d). With seed 20261023 / n=120 the automatic 4-person replacement group (pending, unreviewed) is
NOT kept by the next run's RULES-STAGE binding selection. This test re-runs that scenario, captures the binding selection's inputs,
and compares the chosen selection with the best selection that is FORCED to keep the replacement group, so the "coverage gain, not
a tie / not a score reshuffle" classification is an independently re-runnable check rather than a mailed number (printed with -s).
The FINAL state after the AI pass is checked separately: the end-of-pass reselection (review_loop.reselect_primaries) optimises
residents in READY proposals, a different objective, so the group may lawfully be primary again at the end. Zero provider calls (fakes)."""
import json

from server import ai, matching as mt
from server.db import rows
from tests.test_data_and_api import _fake_assess, _refillable_group, _wait_run, appdb, client, fake_assess_groups  # noqa: F401


def _max_residents_keeping(glist, existing, keep, pre_taken, budget=3_000_000):
    """EXACT maximum residents any disjoint selection containing `keep` can house (pure coverage, nothing else). Person-branching
    search: at each step the next undecided person is either housed by one of the candidates holding that person (disjoint from
    the selection so far) or left out; bound = housed so far + persons still undecided, pruned when it cannot EXCEED the incumbent
    (we need the maximum value, not every tie). Returns (max residents, exhaustive: bool, nodes)."""
    for e in glist:
        e["_ids"] = frozenset(int(u.id) for u in e["units"]); e["_people"] = mt._people_ids(e["units"])
    # the protected tier (existing primaries that are genuinely ready) is fixed before the bounded selection in the product too
    protected = [e for e in glist if e is not keep and e["is_primary"] and mt._protected_primary(e, existing)]
    # oracle validity: the forced starting incumbent (pre-taken + protected tier + keep) must itself be a lawful disjoint selection,
    # otherwise there is no feasible maximum to report -> classify as infeasible instead of calling the union a maximum
    pre = frozenset(pre_taken or ())
    seen_ids, seen_people = set(pre), set()
    for e in [keep] + protected:
        if e["_ids"] & seen_ids or e["_people"] & seen_people:
            for x in glist:
                x.pop("_ids", None); x.pop("_people", None)
            return None, True, 0   # forced keep infeasible: keep overlaps pre-taken / protected, or protected groups overlap
        seen_ids |= e["_ids"]; seen_people |= e["_people"]
    taken = frozenset(seen_ids)
    start_people = frozenset(seen_people)
    cands = [e for e in glist if e is not keep and not (e["_ids"] & taken)]
    by_person: dict = {}
    for e in cands:
        for pid in e["_people"]:
            by_person.setdefault(pid, []).append(e)
    persons = sorted(by_person, key=lambda pid: (len(by_person[pid]), pid))   # fewest options first
    for pid in persons:
        by_person[pid].sort(key=lambda e: (-len(e["_people"]), e["key"]))
    best = len(start_people)
    stack = [(0, taken, start_people)]
    nodes, exhaustive = 0, True
    npers = len(persons)
    while stack:
        nodes += 1
        if nodes > budget:
            exhaustive = False
            break
        i, cov, ppl = stack.pop()
        if len(ppl) > best:
            best = len(ppl)
        while i < npers and persons[i] in ppl:
            i += 1
        if i >= npers:
            continue
        undecided = sum(1 for pid in persons[i:] if pid not in ppl)
        if len(ppl) + undecided <= best:
            continue
        stack.append((i + 1, cov, ppl))   # this person left out (explored last)
        for e in reversed(by_person[persons[i]]):
            if not (e["_ids"] & cov):
                stack.append((i + 1, cov | e["_ids"], ppl | e["_people"]))
    for e in glist:
        e.pop("_ids", None); e.pop("_people", None)
    return best, exhaustive, nodes


def test_bar_replacement_group_is_kept_or_displaced_only_for_more_residents(client, monkeypatch):
    monkeypatch.setattr(ai, "assess_groups_and_apartments", _fake_assess)
    monkeypatch.setattr(ai, "assess_groups", fake_assess_groups)
    client.post("/api/test-data/seed?n=120")
    _wait_run(client, client.post("/api/match/run-all", json={"dataset": "test"}).json()["run_id"])
    c = appdb(client)
    prim = [x for x in client.get("/api/groups?dataset=test").json()["items"] if x["status"] == "Proposed"]
    g, victim = _refillable_group(client, prim)
    r = client.post(f"/api/enquiries/{victim}/bar", json={"reason": "Abusive to staff on the phone"}).json()
    new_id = r["replaced_groups"][0]["new_group_id"]
    new_key = rows(c.execute("SELECT member_key FROM groups WHERE id=?", (new_id,)))[0]["member_key"]
    captured = []
    orig = mt._choose_primaries

    def spy(glist, existing, pre_taken=None, stats=None):
        orig(glist, existing, pre_taken=pre_taken, stats=stats)
        if stats is not None and pre_taken is not None:   # the binding selection under the save guard (full_run step 5)
            keep = next((e for e in glist if e["key"] == new_key), None)
            chosen = [e for e in glist if e["is_primary"]]
            for e in glist:
                e["_people"] = mt._people_ids(e["units"])
            cs = mt._selection_score(chosen, existing)
            for e in glist:
                e.pop("_people", None)
            ks = _max_residents_keeping(glist, existing, keep, pre_taken) if keep else None
            captured.append({"chosen": cs, "keep_max_residents": ks, "kept_in_chosen": bool(keep and keep["is_primary"]), "nodes": stats.get("selection_nodes"),
                             "truncated": stats.get("selection_truncated"), "candidate_present": keep is not None})
    monkeypatch.setattr(mt, "_choose_primaries", spy)
    _wait_run(client, client.post("/api/match/run-all", json={"dataset": "test"}).json()["run_id"])
    assert captured, "binding selection not observed"
    cap = captured[-1]
    print("\nT3D BAR KEEP-COMPARE:", json.dumps({k: (list(v[:2]) + [len(v[2]), v[3]] if k == "chosen" else v) for k, v in cap.items()}))
    assert cap["candidate_present"], "the replacement group must still be a rule-passing candidate"
    keep_max, exhaustive, _nodes = cap["keep_max_residents"]
    assert keep_max is not None, "forced keep is infeasible (overlaps the pre-taken / protected tier): no exact comparison to report"
    # (1) RULES-STAGE coverage proof, anchored to the CAPTURED binding selection (not to the state after the AI pass)
    if cap["kept_in_chosen"]:
        assert cap["chosen"][0] == keep_max   # kept at the rules stage: nothing covers more residents
    else:
        # displaced at the rules stage: the chosen selection houses MORE residents than ANY selection keeping the group - an exact
        # comparison when `exhaustive`, otherwise only "within the search budget" (the assertion message says which)
        assert cap["chosen"][0] > keep_max, (cap["chosen"][:2], keep_max, "exhaustive" if exhaustive else "keep search NOT exhaustive")
        assert exhaustive, "keep-this-group maximum not proven within budget"
        assert not cap["truncated"], "the binding selection must be a proven maximum for this claim"
    # (2) FINAL post-AI invariants, separately: the end-of-pass reselection (review_loop.reselect_primaries) has a DIFFERENT
    # objective (most residents in a genuinely READY proposal now), so the replacement group may lawfully end up primary even
    # though the rules-stage selection displaced it. What must hold regardless: it still exists as a candidate, the barred person
    # is in no proposal, and the final primaries of the data set are member-disjoint (no person in two primaries).
    kept = client.get(f"/api/groups/{new_id}").json()
    assert kept["id"] == new_id and kept["status"] in ("Proposed", "Introduced", "Awaiting confirmations", "Confirmed", "Leased")
    final_prim = rows(c.execute("SELECT g.id, gm.enquiry_id FROM groups g JOIN group_members gm ON gm.group_id=g.id "
                                "JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=1 AND g.is_primary=1 AND g.status='Proposed'"))
    by_enq: dict = {}
    for r_ in final_prim:
        by_enq.setdefault(r_["enquiry_id"], set()).add(r_["id"])
    assert all(len(v) == 1 for v in by_enq.values()), "a person sits in two final primary proposals"
    assert victim not in by_enq, "the barred person must be in no primary proposal"
    print("T3D BAR FINAL:", json.dumps({"final_primary": bool(kept["is_primary"]), "rules_stage_kept": cap["kept_in_chosen"],
                                        "classification": ("kept" if cap["kept_in_chosen"] else "displaced for more residents (rules stage)")
                                        + ("; re-chosen by post-AI ready-residents reselection" if kept["is_primary"] and not cap["kept_in_chosen"] else "")}))
