"""T6 revision loop (Bill, 8 November 2026): when the AI review of a proposed group says "revise", the RULES form eligible
alternatives around the kept members, each alternative is reviewed and briefed like any other group, and the first one that is
approved and briefed supersedes the original proposal. Bounded engineering iterations; no hard-rule override; no fake success.

Boundaries (register):
  * Internal AI rework is not a customer rejection: no member_rejections row, no GP9 bar, no 'Awaiting replacement' (GP10).
  * Only UNINTRODUCED proposals ('Proposed') are revised automatically; an introduced/confirmed group (GP11/GP13 approvals)
    is held with the AI's note for staff to decide.
  * A ref that points at a party member (U2-m1) or a linked friend (GP11) resolves to the WHOLE unit / linked party; parties
    are never split. If that would replace every member, the group is held.
  * GP7: alternatives are non-primary candidates while under review; promotion is one atomic step that supersedes the parent
    and makes the accepted alternative the proposal, and an enquiry goes back to 'New' only when no other live group owns it.
"""
from __future__ import annotations

from . import ai_queue, db, matching, rules
from .db import audit, j, maybe_commit, now_iso, row, rows

MAX_REVISION_ROUNDS = 2        # engineering bound: original -> round 1 -> round 2 -> hold
MAX_ALTERNATIVES_PER_ROUND = 3
AUTO_REVISABLE = ("Proposed",)


def _hold(c, gid: int, reason: str) -> None:
    when = now_iso()
    c.execute("UPDATE groups SET ai_status='held', ranking_basis='Held for staff (AI review)', held_reason=?, ready_at=NULL, updated_at=? WHERE id=?", (reason[:600], when, gid))


def refs_to_enquiry_ids(person_refs: dict, refs: list[str]) -> set[int]:
    """person_id ('12' or '12m1') -> ref map inverted: the enquiry ids the refs point at (a party member's ref is its enquiry)."""
    by_ref = {v: k for k, v in (person_refs or {}).items()}
    out = set()
    for r in refs:
        pid = by_ref.get(r)
        if pid is None:
            continue
        digits = "".join(ch for ch in str(pid).split("m")[0] if ch.isdigit())
        if digits:
            out.add(int(digits))
    return out


def revise(c, parent: dict, payload: dict, review: dict) -> dict:
    """Form and queue alternatives for `parent` (a live group row) from a valid 'revise' review. Returns a small report; the
    parent row is left 'revise' (alternatives queued) or 'held' (with the reason)."""
    gid = parent["id"]
    rnd = int(parent.get("revision_round") or 0)
    report = {"group_id": gid, "round": rnd + 1, "alternatives": [], "held": None}
    if parent.get("status") not in AUTO_REVISABLE:
        _hold(c, gid, f"AI suggested replacing {', '.join(review['replace_refs'])}; the group is already {parent.get('status').lower()}, so staff decide. " + review["rationale"])
        report["held"] = "introduced"
        return report
    if rnd >= MAX_REVISION_ROUNDS:
        _hold(c, gid, f"AI asked for another revision after {rnd} revision round(s); the bound is {MAX_REVISION_ROUNDS}. Staff decide. " + review["rationale"])
        report["held"] = "rounds exhausted"
        return report
    members = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))]
    replaced: set[int] = set()
    for eid in refs_to_enquiry_ids(payload.get("person_refs") or {}, review["replace_refs"]):
        replaced.update(matching.linked_party_ids(c, eid))   # GP11: a linked party moves as a whole
    replaced &= set(members)
    kept_ids = [m for m in members if m not in replaced]
    if not replaced:
        _hold(c, gid, "AI asked for a revision but named no member of this group. " + review["rationale"])
        report["held"] = "no member named"
        return report
    if not kept_ids:
        _hold(c, gid, "AI asked to replace every member (or the whole linked party); nothing to build around. Staff decide. " + review["rationale"])
        report["held"] = "nothing kept"
        return report
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (parent["apartment_id"],)))
    if not a or a.get("leased_at") or not a.get("available_from"):
        _hold(c, gid, "AI asked for a revision but the group's apartment is no longer available. " + review["rationale"])
        report["held"] = "apartment unavailable"
        return report
    apt = matching.apartment_from_row(a)
    kept = []
    for eid in kept_ids:
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        if not e:
            continue
        kept.append(matching.unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))))
    is_test = row(c.execute("SELECT is_test FROM enquiries WHERE id=?", (kept_ids[0],)))["is_test"]
    dataset = "test" if is_test else "real"
    # people already committed elsewhere (introduced or later, or awaiting their own replacement) are not alternatives
    committed = {r["enquiry_id"] for r in rows(c.execute("""SELECT gm.enquiry_id FROM group_members gm JOIN groups g ON g.id=gm.group_id
                                                              WHERE g.status IN ('Introduced','Group confirmed','Awaiting replacement')"""))}
    excluded = replaced | committed | set(members)
    options = matching._refill_options(c, apt, a, kept, excluded, dataset, limit=MAX_ALTERNATIVES_PER_ROUND)
    if not options:
        _hold(c, gid, f"AI asked to replace {', '.join(review['replace_refs'])} but the rules found no eligible alternative at this apartment (search limited to the current pool). Staff decide. " + review["rationale"])
        report["held"] = "no alternative"
        return report
    new_ids = []
    for o in options:
        entry = {"key": matching.member_key(o["units"]), "units": o["units"], "apartments": [matching._apt_option(a, apt, o)], "ai_rating": None, "ai_rationale": None,
                 "ai_status": "pending", "is_primary": False}
        if row(c.execute("SELECT id FROM groups WHERE member_key=? AND status IN ('Proposed','Introduced','Group confirmed','Awaiting replacement')", (entry["key"],))):
            continue   # that member set already exists as a live group
        ngid = matching.persist_group(c, None, entry, ai_queue.PENDING_BASIS, run_kind="revision")
        c.execute("UPDATE groups SET revision_of=?, revision_round=?, held_reason=? WHERE id=?",
                  (gid, rnd + 1, f"Alternative for group {gid} (AI revision round {rnd + 1}): replaces {', '.join(review['replace_refs'])}", ngid))
        new_ids.append(ngid)
    if not new_ids:
        _hold(c, gid, "AI asked for a revision; every rule-passing alternative already exists as a live group. Staff decide. " + review["rationale"])
        report["held"] = "alternatives exist"
        return report
    c.execute("UPDATE groups SET held_reason=? WHERE id=?",
              ((f"AI review: revise {', '.join(review['replace_refs'])}. {len(new_ids)} alternative(s) queued for review (round {rnd + 1}). " + review["rationale"])[:600], gid))
    maybe_commit(c)   # revise runs inside the queue's guarded review step; the step commits once at its exit
    ai_queue.enqueue(c, new_ids, priority=ai_queue.PRIORITY_CHANGE, reason=f"revision of group {gid}")
    audit(c, None, "ai_revision", "group", gid, f"round {rnd + 1}: {len(new_ids)} alternative(s) {new_ids} replace {sorted(replaced)}")
    report["alternatives"] = new_ids
    return report


COMMITTED_STATUSES = ("Introduced", "Group confirmed", "Awaiting replacement", "Leased")   # a person in any of these elsewhere is not free
SIBLING_BASIS = "Superseded: a sibling alternative was promoted"
SUPERSEDED_BASIS = "Superseded by AI revision"   # structural marker on a Dissolved parent: a descendant was promoted in its place


def _begin_guarded(c, group_ids: list[int], enquiry_ids: list[int]) -> None:
    """Take the database-side locks the promotion needs BEFORE re-reading state, so the re-validation and the writes are one
    guarded operation against OTHER connections:
      * SQLite: BEGIN IMMEDIATE takes the database write lock; every other connection's write waits until commit/rollback. (If the
        shared connection already has a transaction open from earlier statements, no new BEGIN is issued; the compare-and-swap
        writes below still refuse to act on state that moved.)
      * PostgreSQL: a no-op UPDATE on the group rows and SELECT ... FOR UPDATE on the member enquiries lock those rows for the
        transaction (the adapter would otherwise auto-commit a pure read). Groups of OTHER members are not locked: two promotions
        touching disjoint members may run concurrently, which is safe because the ownership check is per member and each member
        row is locked here.
    Lock ORDER is fixed - all group rows in ascending id, then all enquiry rows in ascending id (ORDER BY id inside the FOR UPDATE)
    - so two promotions that share any row (a common ancestor, a common member) always contend on the same first row instead of
    each holding its own branch and waiting for the other's (competing round-2 branches: both start at the shared root, whose id
    is the smallest in either chain).
    Same-connection threads are kept out by promote() holding the connection's RLock for the whole operation.
    Evidence so far: SQLite only (single-process tests). The PostgreSQL branch has NOT been exercised with two concurrent
    connections in this repository; production PostgreSQL behaviour is not evidenced by these tests."""
    if getattr(c, "backend", "sqlite") == "sqlite":
        if not getattr(c, "in_transaction", False):
            c.execute("BEGIN IMMEDIATE")
        return
    for gid in sorted({int(x) for x in group_ids}):
        c.execute("UPDATE groups SET id=id WHERE id=?", (gid,))
    ids = sorted({int(x) for x in enquiry_ids})
    if ids:
        c.execute("SELECT id FROM enquiries WHERE id IN (%s) ORDER BY id FOR UPDATE" % ",".join("?" * len(ids)), tuple(ids))


def _members(c, gid: int) -> list[int]:
    return sorted({int(r["enquiry_id"]) for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))})


def owners_elsewhere(c, enquiry_ids: list[int], exclude_group_ids: list[int], need_primary: bool) -> list[int]:
    """Enquiry ids among `enquiry_ids` that are OWNED by a live group other than `exclude_group_ids`. Ownership means a committed
    later state (Introduced / Group confirmed / Awaiting replacement / Leased) or, when the promoted group would be primary, the
    person's PRIMARY 'Proposed' group (GP7: one primary proposal per person). Non-primary 'Proposed' candidates - sibling
    alternatives, other rule candidates - own nobody and never block each other."""
    if not enquiry_ids:
        return []
    ex = ",".join("?" * len(exclude_group_ids))
    ins = ",".join("?" * len(enquiry_ids))
    cond = "g.status IN (%s)" % ",".join("?" * len(COMMITTED_STATUSES))
    args: list = [*COMMITTED_STATUSES]
    if need_primary:
        cond = "(" + cond + " OR (g.status='Proposed' AND g.is_primary=1))"
    q = f"SELECT DISTINCT gm.enquiry_id FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE {cond} AND g.id NOT IN ({ex}) AND gm.enquiry_id IN ({ins})"
    return sorted(int(r["enquiry_id"]) for r in rows(c.execute(q, (*args, *exclude_group_ids, *enquiry_ids))))


def promote(c, new_gid: int) -> dict:
    """Make an approved, briefed, ready alternative THE proposal and supersede its parent - or leave BOTH untouched.
    Returns {"promoted": bool, "reason": str, ...}. Guarantees:
      * the whole operation is ONE db.retention_guard step (the connection's RLock against other threads of this connection, the
        database write/advisory lock against other connections, a single commit at exit, rollback on any abort) plus the row
        locks from _begin_guarded; nothing inside commits or rolls back on its own;
      * membership is re-read under the lock; the pre-lock read is used only to know which enquiry rows to lock, and a change
        between the two reads aborts the attempt;
      * the parent is superseded only inside the same guarded operation that promotes the alternative (never dissolved first);
      * GP7: no member of the alternative may be OWNED elsewhere (committed later state anywhere; another PRIMARY proposal when
        this one would be primary). If one is, the alternative stays a non-primary candidate with the reason and the parent stays
        as it was. Sibling alternatives (non-primary 'Proposed') do not block each other;
      * the alternative is re-validated (is_ready: review + brief + evidence hash + hard rules) after the locks are taken; the
        writes are compare-and-swap against the exact parent/alternative state read under the lock;
      * on success the parent's OTHER still-pending alternatives are retired (Dissolved, labelled) in the same transaction, so a
        replacement candidate can never become an additional active primary group;
      * replaced members return to 'New' only when no other live group owns them; nothing is set 'New' while still owned.
    Verified with tests on SQLite only (same connection and a second connection); not evidenced on PostgreSQL."""
    walk0 = _ancestors(c, new_gid)
    chain0 = walk0["chain"]
    if walk0["state"] == "not a revision":
        return {"promoted": False, "reason": "not a revision"}
    if walk0["state"] == "already promoted":
        return {"promoted": False, "reason": "already promoted"}
    parent_id = int(row(c.execute("SELECT revision_of FROM groups WHERE id=?", (new_gid,)))["revision_of"])
    pre_members = sorted(set(_members(c, new_gid)).union(*[set(_members(c, a)) for a in chain0]))
    try:
        # T5: ONE retention-guard step (db.retention_guard: RLock + SQLite write lock / PG advisory lock, single commit at exit);
        # every "leave both untouched" path raises _Abort so the step rolls back without any hidden commit/rollback inside
        with db.retention_guard(c, label="revision promote"):
            _begin_guarded(c, [*chain0, new_gid], pre_members)
            result = _promote_guarded(c, new_gid, parent_id, chain0, pre_members)
    except _Abort as a:
        return a.result
    chain, make_primary, siblings, back = result["superseded"], result.pop("primary"), result["retired"], result["back_to_pool"]
    if not result["promoted"]:
        return {k: v for k, v in result.items() if k not in ("superseded", "retired", "back_to_pool")}
    audit(c, None, "ai_revision_promoted", "group", new_gid, f"supersedes groups {chain}; primary={1 if make_primary else 0}; retired siblings {siblings}; back to pool: {back}")
    return {"promoted": True, "reason": "promoted", "superseded": chain, "retired": siblings, "back_to_pool": back}


class _Abort(Exception):
    """Leave the alternative AND its ancestors untouched: the guarded step rolls back and promote() returns this result."""
    def __init__(self, result: dict):
        super().__init__(result.get("reason"))
        self.result = result


def _promote_guarded(c, new_gid: int, parent_id: int, chain0: list[int], pre_members: list[int]) -> dict:
    """The body of promote() inside the retention guard: no commit/rollback here (the guard commits once at exit; _Abort rolls back)."""
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (new_gid,)))
    walk = _ancestors(c, new_gid)   # re-read under the lock: the chain itself is part of the state we rely on
    chain = walk["chain"]
    if not g:
        raise _Abort({"promoted": False, "reason": "group vanished"})
    if walk["state"] == "already promoted":
        raise _Abort({"promoted": False, "reason": "already promoted"})
    when = now_iso()
    nothing = {"promoted": False, "superseded": chain, "primary": False, "retired": [], "back_to_pool": []}
    if walk["state"] == "blocked":
        # an ancestor has moved on (introduced, re-approved, dissolved by staff, or retired/superseded by a competing branch):
        # a late descendant cannot revive it. The candidate stays a plain, non-primary candidate with the reason; nothing else changes.
        b = walk["blocker"]
        c.execute("UPDATE groups SET is_primary=0, held_reason=?, updated_at=? WHERE id=? AND updated_at IS NOT DISTINCT FROM ?",
                  (f"Approved alternative for group {parent_id}; not promoted because ancestor group {b['id']} is {b['status']}/{b['ai_status']} (staff decide)",
                   when, new_gid, g.get("updated_at")))
        return {**nothing, "reason": f"ancestor {b['id']} is {b['status']}/{b['ai_status']}", "blocker": b["id"]}   # committed by the guard
    if chain != chain0 or not chain:
        raise _Abort({"promoted": False, "reason": "revision chain changed under us"})
    ancestors = [row(c.execute("SELECT * FROM groups WHERE id=?", (a,))) for a in chain]
    new_members = _members(c, new_gid)
    old_members = sorted(set().union(*[set(_members(c, a)) for a in chain]))
    if sorted(set(new_members) | set(old_members)) != pre_members or not new_members:
        raise _Abort({"promoted": False, "reason": "membership changed under us"})
    if g["status"] != "Proposed" or int(g.get("revision_of") or 0) != parent_id:
        raise _Abort({"promoted": False, "reason": f"alternative is {g['status']}"})
    # every ancestor in the chain (up to the proposal this revision ultimately replaces) is still waiting on it: _ancestors
    # guarantees Proposed + revise/held for each, and stops at the nearest earlier promotion boundary
    chk = ai_queue.is_ready(c, g)
    if not chk["ready"]:
        raise _Abort({"promoted": False, "reason": "alternative not ready: " + "; ".join(chk["reasons"])})
    make_primary = any(bool(a["is_primary"]) for a in ancestors)   # primary ownership is inherited from the original proposal, through any depth
    busy = owners_elsewhere(c, new_members, [*chain, new_gid], need_primary=make_primary)
    if busy:
        c.execute("UPDATE groups SET is_primary=0, held_reason=?, updated_at=? WHERE id=? AND updated_at IS NOT DISTINCT FROM ?",
                  (f"Approved alternative for group {parent_id}; not promoted because member(s) {busy} are committed in another live group", when, new_gid, g.get("updated_at")))
        audit(c, None, "ai_revision_blocked", "group", new_gid, f"ancestors {chain} kept; members {busy} owned elsewhere")
        return {**nothing, "reason": f"members {busy} committed elsewhere", "busy": busy}
    # compare-and-swap writes against the exact state read under the lock: every ancestor is superseded in the same transaction
    for a in ancestors:
        cur = c.execute("""UPDATE groups SET status='Dissolved', is_primary=0, ready_at=NULL, ranking_basis=?, held_reason=?, updated_at=?
                           WHERE id=? AND status=? AND ai_status=? AND updated_at IS NOT DISTINCT FROM ?""",
                        (SUPERSEDED_BASIS, f"Superseded by group {new_gid} (AI revision round {g.get('revision_round')})", when, a["id"], a["status"], a["ai_status"], a.get("updated_at")))
        if cur.rowcount != 1:
            raise _Abort({"promoted": False, "reason": "parent changed under us"})
    cur = c.execute("""UPDATE groups SET is_primary=?, held_reason=NULL, updated_at=? WHERE id=? AND status='Proposed' AND ai_status='ok' AND brief_status='ok'
                       AND evidence_hash IS NOT DISTINCT FROM ? AND updated_at IS NOT DISTINCT FROM ?""", (1 if make_primary else 0, when, new_gid, g.get("evidence_hash"), g.get("updated_at")))
    if cur.rowcount != 1:
        raise _Abort({"promoted": False, "reason": "alternative changed under us"})
    # retire every competing branch: other still-pending alternatives of ANY ancestor (siblings of the parent, siblings of the
    # candidate). They were candidates for THIS replacement and must not linger as a second path to a primary group.
    siblings: list[int] = []
    todo = list(chain)   # every ancestor's other branches, and recursively THEIR pending descendants
    while todo:
        a = todo.pop(0)
        for r in rows(c.execute("SELECT id FROM groups WHERE revision_of=? AND id<>? AND status='Proposed'", (a, new_gid))):
            sid = int(r["id"])
            if sid in chain or sid in siblings:
                continue
            siblings.append(sid)
            todo.append(sid)
            c.execute("UPDATE groups SET status='Dissolved', is_primary=0, ready_at=NULL, ranking_basis=?, held_reason=?, updated_at=? WHERE id=? AND status='Proposed'",
                      (SIBLING_BASIS, f"Retired: group {new_gid} was promoted in place of group {a}", when, sid))
    if make_primary:
        for eid in new_members:
            c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=? AND status='New'", (eid,))
    back = []
    for eid in sorted(set(old_members) - set(new_members)):   # replaced by the AI's rework: back to the pool, no rejection, no bar
        owned = row(c.execute("""SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.enquiry_id=? AND g.id<>? AND g.status IN (%s)"""
                              % ",".join("?" * len(ai_queue.LIVE)), (eid, new_gid, *ai_queue.LIVE)))
        if not owned:
            c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status='Proposed in group'", (eid,))
            back.append(eid)
    return {"promoted": True, "reason": "promoted", "superseded": chain, "primary": make_primary, "retired": siblings, "back_to_pool": back}


def _ancestors(c, gid: int) -> dict:
    """Walk revision_of upwards from `gid` to find the proposal this candidate would replace.
    Returns {"chain": [parent, grandparent, ...], "state": s, "blocker": row|None} where every id in `chain` is still WAITING on
    this revision (Proposed + revise/held) and `state` is one of:
      * "open"              - chain ends at the original proposal (revision_of NULL): promoting supersedes the whole chain;
      * "owner"             - chain ends at an ancestor whose own parent was superseded by an EARLIER promotion (Dissolved with
                              SUPERSEDED_BASIS): that ancestor is the current owner of the proposal, promoted before and later
                              re-reviewed/revised; the historical predecessor stays Dissolved and is neither revived nor a blocker;
      * "already promoted"  - `gid` itself sits on such a boundary (its parent was superseded by it): nothing to promote;
      * "blocked"           - an ancestor has moved on (introduced/committed/re-approved/dissolved by staff, or retired/superseded
                              by a COMPETING branch): a late descendant cannot revive it; `blocker` is that row;
      * "not a revision"    - `gid` has no revision_of.
    revision_of is also historical provenance after a promotion, which is why the walk stops at a promotion boundary instead of
    demanding every ancestor be live. Bounded by MAX_REVISION_ROUNDS + 1 hops so a corrupt cycle cannot loop."""
    chain: list[int] = []
    x = row(c.execute("SELECT id, revision_of FROM groups WHERE id=?", (gid,)))
    if not x or not x.get("revision_of"):
        return {"chain": [], "state": "not a revision", "blocker": None}
    while x and x.get("revision_of") and len(chain) <= MAX_REVISION_ROUNDS:
        nxt = int(x["revision_of"])
        if nxt in chain or nxt == gid:
            break
        par = row(c.execute("SELECT id, status, ai_status, is_primary, ranking_basis, revision_of FROM groups WHERE id=?", (nxt,)))
        if not par:
            return {"chain": chain, "state": "blocked", "blocker": {"id": nxt, "status": "missing", "ai_status": None}}
        if par["status"] in AUTO_REVISABLE and par["ai_status"] in ("revise", "held"):
            chain.append(nxt)
            x = par
            continue
        if par["status"] == "Dissolved" and par.get("ranking_basis") == SUPERSEDED_BASIS:
            return {"chain": chain, "state": "already promoted" if not chain else "owner", "blocker": None}
        return {"chain": chain, "state": "blocked", "blocker": dict(par)}
    return {"chain": chain, "state": "open", "blocker": None}


# ---------------------------------------------------------------------------
# T3(d) C2(b) (10 Nov 2026): post-AI reselection of proposals
# ---------------------------------------------------------------------------
RESELECT_AUDIT = "primary_reselect"
GUARD_FAILURES_LOCAL = ai_queue.GUARD_FAILURES
RESELECT_HOLD_ATTEMPTS = 3 * ai_queue.AI_ATTEMPTS   # the queue's own re-queue ceiling (ai_queue._apply_result): beyond it a pending review is exhausted


def _reselect_candidates(c, is_test: int) -> list[dict]:
    """Every 'Proposed' group of the data set with its member ids and resident count, in a deterministic order (read-only)."""
    gs = rows(c.execute("""SELECT g.* FROM groups g WHERE g.status='Proposed' AND g.member_key IS NOT NULL AND g.id IN
                            (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id""", (is_test,)))
    out = []
    for g in gs:
        g["_members"] = frozenset(_members(c, g["id"]))
        if not g["_members"]:
            continue
        ids = sorted(g["_members"])
        pm = {int(r["enquiry_id"]): int(r["n"]) for r in rows(c.execute("SELECT enquiry_id, COUNT(*) AS n FROM party_members WHERE enquiry_id IN (%s) GROUP BY enquiry_id" % ",".join("?" * len(ids)), ids))}
        g["_people"] = frozenset(x for eid in ids for x in ([f"{eid}"] + [f"{eid}m{k}" for k in range(1, pm.get(eid, 0) + 1)]))   # one id per resident (lead + party members)
        out.append(g)
    return out


def _communicated(c, gid: int) -> bool:
    """The group has been put to its members in any form: a membership version anchored for introduction, or any GP13 response
    link / response on one of its versions. Such a proposal is never reshuffled here (T3(d): protect introduced / responded /
    accepted / linked commitments; a 'Proposed' row normally has none of these - Introduced and later states are not 'Proposed')."""
    if c.execute("SELECT COUNT(*) FROM group_membership_versions WHERE group_id=? AND introduced_at IS NOT NULL", (gid,)).fetchone()[0]:
        return True
    if c.execute("SELECT COUNT(*) FROM group_response_links l JOIN group_membership_versions v ON v.id=l.version_id WHERE v.group_id=?", (gid,)).fetchone()[0]:
        return True
    return bool(c.execute("SELECT COUNT(*) FROM group_responses r JOIN group_membership_versions v ON v.id=r.version_id WHERE v.group_id=?", (gid,)).fetchone()[0])


def reselect_protection(c, g: dict, ready: bool) -> str | None:
    """Why a PRIMARY proposal is not reconsidered by the post-AI step (None = it competes). Labelled on purpose; the first two are
    the T3(d) commitments, the last is an ENGINEERING TEMPORARY HOLD, not a commitment:
      * 'communicated' - introduced / responded / linked history on the group (_communicated): never reshuffled after communication;
      * 'in_flight'    - a review is claimed right now (ai_status 'running');
      * 'pending_hold' - queued or re-queued and its review attempts are not exhausted (ai_attempts < RESELECT_HOLD_ATTEMPTS): the
        group was formed as the proposal and has not been judged yet, so it is not swapped for a smaller ready alternative only to
        swap back when it is approved. Released the moment the review fails, is held / asks for revision, or the re-queue ceiling
        is reached - never a permanent commitment; the result reports how many ready alternatives such holds block.
    A READY but uncommunicated primary is NOT protected here (root, 10 Nov 2026): it competes, and _reselect_score keeps it unless
    a permitted alternative set puts MORE residents into ready proposals (a ready AB must not block current-ready AC + BD covering
    four); at equal ready coverage ready incumbents are preserved ahead of rule score, so nobody is reshuffled for score alone."""
    if not g.get("is_primary"):
        return None
    if _communicated(c, int(g["id"])):
        return "communicated"
    if g.get("ai_status") == "running":
        return "in_flight"
    if g.get("ai_status") == "pending" and int(g.get("ai_attempts") or 0) < RESELECT_HOLD_ATTEMPTS:
        return "pending_hold"
    return None


def _reselect_score(chosen: list[dict], ready: dict, was_primary: dict) -> tuple:
    """Post-AI objective (per-person outcome, no weights): residents in a GENUINELY READY proposal first (a failed / held six-person
    primary must not block an approved four-person alternative merely because 6 > 4); then READY incumbents preserved - a ready
    proposal is displaced ONLY when ready coverage grows, never for more not-yet-ready residents (ready AB + failed CD must not yield
    to ready AC + failed BEF: 5 > 4 proposed, but ready coverage is still two) and never for rule score; then residents proposed at
    all; then the existing rule scores best-first; then any existing proposals retained."""
    rp = frozenset().union(*[g["_people"] for g in chosen if ready[int(g["id"])]]) if chosen else frozenset()
    people = frozenset().union(*[g["_people"] for g in chosen]) if chosen else frozenset()
    return (len(rp), sum(1 for g in chosen if ready[int(g["id"])] and was_primary[int(g["id"])]), len(people),
            tuple(sorted((round(float(g.get("rule_score") or 0), 6) for g in chosen), reverse=True)),
            sum(1 for g in chosen if was_primary[int(g["id"])]))


def _promotable_now(c, g: dict, ready: dict, committed: set) -> list[int] | None:
    """For a READY non-primary revision alternative: the ancestor chain it would supersede when promote() can run now (chain
    state 'open' / 'owner', every ancestor still waiting, no member committed elsewhere) else None. Read-only; promote() itself
    re-validates everything under its own guard - this only decides whether the alternative may enter the selection."""
    if g.get("is_primary") or not g.get("revision_of") or not ready.get(int(g["id"])) or (g["_members"] & committed):
        return None
    walk = _ancestors(c, int(g["id"]))
    if walk["state"] not in ("open", "owner") or not walk["chain"]:
        return None
    return list(walk["chain"])


def reselect_primaries(c, is_test: int, budget: int | None = None, _after_promote: bool = False) -> dict:
    """T3(d) C2(b): after a queue pass, make the PRIMARY proposals of one data set the disjoint set of 'Proposed' groups that puts
    the most residents into a genuinely ready proposal now (_reselect_score), without touching any commitment.
    Why: the run-time selection happens before any review; a pending-first holding can strand a ready alternative indefinitely.
    Once the bounded processing of a pass is over, unsuccessful provisional holdings are reconsiderable here (T3(d) supersedes the
    blanket primary-first default ONLY for controlled unsuccessful provisional rework).
    Protections (reselect_protection; never displaced / never newly proposed):
      * members owned by a committed later state anywhere (Introduced / confirmed / awaiting / leased: owners_elsewhere) are out;
      * primaries that are communicated, in flight, or under the disclosed pending hold keep their people (protected tier); a READY
        uncommunicated primary competes and is displaced only by a permitted set with MORE residents in ready proposals;
      * a candidate becomes primary only if the hard rules still pass for its members today; parties are whole groups' members
        already (GP11 / GP12), so no selection can split them.
    Revision alternatives (revision_of set) change hands ONLY through promote(): a READY one whose chain promote() could supersede
    now (_promotable_now) enters the selection as a candidate that, for disjointness, owns its members AND every ancestor's
    members (so two descendants of one chain can never both be chosen and nobody else takes an ancestor's people while the chain
    is still primary); when chosen, this step does NOT flip it or clear its ancestors' primary flag (promote decides inheritance)
    - it only releases the unsuccessful owners that blocked it (e.g. a failed primary holding one of its members) and lists it in
    `promote`; promote()'s guarded body (_promote_guarded) then runs for each INSIDE this same transaction. If any handoff does not
    complete, the whole step rolls back (reason + `promote_failed`): no incumbent is left demoted for a promotion that did not
    happen; the next pass tries again - no provider call is repeated merely to retry.
    A not-ready revision alternative never becomes primary here. A 'revise' primary parent is an unsuccessful holding: it
    competes like any other and is displaced only when the objective improves.
    Atomic: ONE db.retention_guard step (_begin_guarded locks; state re-read under the lock; compare-and-swap writes on
    updated_at; pool statuses adjusted in the same transaction) - no transient second primary for a person, no count inflation.
    No provider call, no new group, no deletion. Synchronous at the end of ai_queue.process_pending (adds no pending work type).
    Reports: changed / gained / lost, ready residents before -> after, residents proposed before -> after, truncated (bounded
    select_disjoint), holds (protected-tier counts by label), ready_alternatives_blocked (ready non-primary groups whose members
    sit in a pending_hold / in_flight primary: the starvation that is NOT resolved by this pass), promote / promoted / promote_failed."""
    pre = _reselect_candidates(c, is_test)
    def empty():   # a fresh dict each time: an aborted step must never report lists the attempt had started filling
        return {"changed": 0, "gained": [], "lost": [], "truncated": 0, "promote": [], "promoted": [], "promote_failed": []}
    if not pre:
        return empty()
    gids = [int(g["id"]) for g in pre]
    eids = sorted(set().union(*[g["_members"] for g in pre]))
    out = dict(empty(), ready_residents_before=0, ready_residents_after=0, residents_before=0, residents_after=0, holds={}, ready_alternatives_blocked=0)
    try:
        with db.retention_guard(c, label="primary reselect"):
            _begin_guarded(c, gids, eids)
            gs = _reselect_candidates(c, is_test)
            if [(int(g["id"]), g["member_key"], g.get("updated_at")) for g in gs] != [(int(g["id"]), g["member_key"], g.get("updated_at")) for g in pre]:
                raise _Abort(dict(empty(), reason="groups changed under us"))
            by_id = {int(g["id"]): g for g in gs}
            committed = set(owners_elsewhere(c, eids, gids, need_primary=False))
            # the product predicate for every group that could be ready; the others cannot be (is_ready requires all three fields)
            ready = {int(g["id"]): (bool(ai_queue.is_ready(c, g)["ready"]) if (g.get("ai_status") == "ok" and g.get("review_verdict") == "approve" and g.get("brief_status") == "ok") else False)
                     for g in gs}
            was_primary = {int(g["id"]): bool(g.get("is_primary")) for g in gs}
            taken: set[int] = set(committed)
            keep: list[dict] = []
            holds: dict[str, int] = {}
            for g in gs:
                why = reselect_protection(c, g, ready[int(g["id"])])
                if why and not (g["_members"] & taken):
                    keep.append(g)
                    taken |= g["_members"]
                    holds[why] = holds.get(why, 0) + 1
            held_people = frozenset().union(*[g["_members"] for g in keep if reselect_protection(c, g, ready[int(g["id"])]) in ("pending_hold", "in_flight")]) if keep else frozenset()
            # revision alternatives that promote() could hand the chain to right now: selection candidates owning their chain's people
            via_promote: dict[int, list[int]] = {}
            for g in gs:
                chain = _promotable_now(c, g, ready, committed)
                if chain:
                    chain_members = frozenset().union(*[by_id[a]["_members"] if a in by_id else frozenset(_members(c, a)) for a in chain])
                    g["_sel_members"] = g["_members"] | chain_members
                    via_promote[int(g["id"])] = chain
            cands = []
            for g in gs:
                sel = g.get("_sel_members", g["_members"])
                if g in keep or (g.get("revision_of") and int(g["id"]) not in via_promote and not g.get("is_primary")) or (sel & taken):
                    if ready[int(g["id"])] and not g.get("is_primary") and (g["_members"] & held_people):
                        out["ready_alternatives_blocked"] += 1
                    continue
                if not g.get("is_primary") and not ai_queue._hard_rules_still_pass(c, g)[0]:
                    continue
                cands.append(g)
            cands.sort(key=lambda g: (0 if ready[int(g["id"])] else 1, 0 if (ready[int(g["id"])] and was_primary[int(g["id"])]) else 1, -len(g["_people"]),
                                      -float(g.get("rule_score") or 0), 0 if was_primary[int(g["id"])] else 1, int(g["id"])))
            former = lambda g: (0 if was_primary[int(g["id"])] else 1, -float(g.get("rule_score") or 0), int(g["id"]))
            stats: dict = {}
            kw = {"budget": budget} if budget is not None else {}
            chosen = matching.select_disjoint(cands, ids_of=lambda g: g.get("_sel_members", g["_members"]), weight_ids=lambda g: g["_people"] if ready[int(g["id"])] else frozenset(),
                                              score=lambda ch: _reselect_score(ch, ready, was_primary), taken=taken, stats=stats, extra_orders=[former], **kw)
            out["truncated"] = int(stats.get("selection_truncated", 0))
            chosen_promote = [int(g["id"]) for g in chosen if int(g["id"]) in via_promote]
            defer = {a for gid in chosen_promote for a in via_promote[gid]}            # ancestors of a chosen alternative: promote() supersedes them, nothing here touches their flag
            # ancestors keep exactly the flag they have (a non-primary intermediate of a two-level chain is NOT made primary here):
            # only promote() changes a chain's ownership
            new_primary = ({int(g["id"]) for g in keep} | {int(g["id"]) for g in chosen}) - set(chosen_promote) | {a for a in defer if a in by_id and was_primary[a]}
            seen: set[int] = set()
            for g in gs:
                if int(g["id"]) in new_primary:
                    if g["_members"] & seen:
                        raise _Abort(dict(empty(), truncated=out["truncated"], reason="selection not disjoint"))
                    seen |= g["_members"]
            def _res(pred):
                return len(frozenset().union(*[g["_people"] for g in gs if pred(g)]) if any(pred(g) for g in gs) else frozenset())
            out["ready_residents_before"] = _res(lambda g: was_primary[int(g["id"])] and ready[int(g["id"])])
            # 'after' counts what THIS step made primary; the alternatives handed to promote() are a plan until the handoff succeeds
            # (recounted below once promote() has run, so a failed handoff never reports hypothetical ready coverage as actual)
            out["ready_residents_after"] = _res(lambda g: int(g["id"]) in new_primary and ready[int(g["id"])])
            out["ready_residents_planned"] = _res(lambda g: (int(g["id"]) in new_primary and ready[int(g["id"])]) or int(g["id"]) in chosen_promote)
            out["residents_before"] = _res(lambda g: was_primary[int(g["id"])])
            out["residents_after"] = _res(lambda g: int(g["id"]) in new_primary)
            out["holds"] = holds
            out["promote"] = chosen_promote
            when = now_iso()
            for g in gs:
                want = int(g["id"]) in new_primary
                if was_primary[int(g["id"])] == want or int(g["id"]) in chosen_promote:
                    continue
                cur = c.execute("UPDATE groups SET is_primary=?, updated_at=? WHERE id=? AND status='Proposed' AND member_key=? AND updated_at IS NOT DISTINCT FROM ?",
                                (1 if want else 0, when, g["id"], g["member_key"], g.get("updated_at")))
                if cur.rowcount != 1:
                    raise _Abort(dict(empty(), truncated=out["truncated"], reason="group changed under us"))
                out["gained" if want else "lost"].append(int(g["id"]))
                out["changed"] += 1
            planned_txt = f" ({out['ready_residents_planned']} once the {len(chosen_promote)} handoff(s) below complete in this transaction)" if chosen_promote else ""
            if out["changed"]:
                primary_people = set().union(*[g["_members"] for g in gs if int(g["id"]) in new_primary]) if new_primary else set()
                for eid in eids:
                    if eid in primary_people:
                        c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=? AND status='New'", (eid,))
                    elif eid not in committed:
                        owned = row(c.execute("SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.enquiry_id=? AND g.status IN (%s) AND g.is_primary=1"
                                              % ",".join("?" * len(ai_queue.LIVE)), (eid, *ai_queue.LIVE)))
                        if not owned:
                            c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status='Proposed in group'", (eid,))
                audit(c, None, RESELECT_AUDIT, "match_run", None,
                      f"dataset={'test' if is_test else 'real'}: {len(out['gained'])} group(s) became the proposal, {len(out['lost'])} became alternatives; "
                      f"residents in a ready proposal {out['ready_residents_before']} -> {out['ready_residents_after']}{planned_txt}; residents proposed "
                      f"{out['residents_before']} -> {out['residents_after']}; holds {holds}; selection truncated={out['truncated']}; "
                      f"revision alternatives handed to promote: {chosen_promote}")
            # the handoffs, in the SAME transaction (root, 10 Nov 2026): promote()'s guarded body (_promote_guarded: ancestry, readiness,
            # ownership and compare-and-swap, unchanged) runs here for each chosen alternative; if ANY handoff does not complete, the
            # whole step aborts - the demotions above roll back with it, so an incumbent ready proposal is never left displaced by a
            # promotion that did not happen, and the actual state never differs from what is reported
            for gid in chosen_promote:
                chain0 = via_promote[gid]
                pre_members = sorted(set(_members(c, gid)).union(*[set(_members(c, a)) for a in chain0]))
                parent_id = int(by_id[gid].get("revision_of") or 0)
                _begin_guarded(c, [*chain0, gid], pre_members)
                try:
                    r = _promote_guarded(c, gid, parent_id, chain0, pre_members)
                except _Abort as pa:
                    r = pa.result
                if not isinstance(r, dict):
                    r = {"promoted": False, "reason": "no result"}
                if not r.get("promoted"):
                    raise _Abort(dict(empty(), truncated=out["truncated"], reason="revision handoff did not complete; selection rolled back",
                                      promote=chosen_promote, promote_failed=[{"group_id": gid, "reason": r.get("reason")}]))
                audit(c, None, "ai_revision_promoted", "group", gid, f"supersedes groups {r['superseded']}; primary={1 if r.get('primary') else 0}; retired siblings {r['retired']}; back to pool: {r['back_to_pool']}")
                out["promoted"].append({"group_id": gid, "reason": "promoted"})
            if chosen_promote:   # every handoff completed in this transaction: the counts now reflect the state being committed
                out["ready_residents_after"] = out.pop("ready_residents_planned")
                out["residents_after"] = _res(lambda g: int(g["id"]) in new_primary or int(g["id"]) in chosen_promote)
                out["changed"] += len(chosen_promote)
    except _Abort as ab:
        return ab.result
    if out["promoted"] and not _after_promote:
        # a successful handoff dissolved the superseded ancestors: the members they held (and nobody else now covers) become
        # selectable only now, so settle the data set with ONE more selection step (no further handoffs are chained from it)
        again = reselect_primaries(c, is_test, budget, _after_promote=True)
        if again.get("reason"):
            out["settle_after_promote"] = again["reason"]
        else:
            out["gained"] = out["gained"] + [g for g in again["gained"] if g not in out["gained"]]
            out["lost"] = out["lost"] + [g for g in again["lost"] if g not in out["lost"]]
            out["changed"] = len(out["gained"]) + len(out["lost"]) + len(out["promoted"])
            out["ready_residents_after"] = again["ready_residents_after"]
            out["residents_after"] = again["residents_after"]
            out["truncated"] = max(out["truncated"], again["truncated"])
            out["ready_alternatives_blocked"] = again["ready_alternatives_blocked"]
            for k, v in again["holds"].items():
                out["holds"][k] = max(out["holds"].get(k, 0), v)
            if again["promote"]:
                out["settle_after_promote"] = f"{len(again['promote'])} further revision alternative(s) left for the next pass"
    return out
