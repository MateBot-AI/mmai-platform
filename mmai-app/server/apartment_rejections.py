"""GP14 (MateBot): a room/bed enquirer tells staff "not this apartment" or "not this building".

What it is: a record OWNED BY THE ENQUIRY (it survives regrouping), written by staff from the customer's words, with the basis
that was shown at the time (the group's option for that apartment plus the apartment's material fields and the enquiry's own
material requirements). It is immutable history: nothing in here ever turns a stored basis back into a current offer.

What it does: while a rejection is active the apartment (scope 'apartment') or every apartment whose location key equals the
building key (scope 'building', including units listed later) is NOT an eligible option for any group that contains the enquiry.
The exclusion travels on the enquiry row (`enquiries.apartment_exclusions_json`, refreshed in the same write as every change here)
so every path that builds a rules.Unit from `SELECT * FROM enquiries` carries it, and matching._unit_fits_apartment - the one
eligibility gate used by candidate generation, option completion, coverage rounds, refills, replacements and the stock lifecycle -
refuses the pairing before any coverage budget or top-N slot is spent. Fail-closed guards repeat the check at finalise, offer and in
the staff view. People are never rejected here (that is GP9, member_rejections) and no customer message is sent.

Reconsideration (GP14 "unless the customer reopens it or something material changes"):
  * reopen_by_customer: staff record that the customer reopened it (status 'reopened'; the exclusion lifts).
  * material_change + allow_reoffer: the server diff of the stored basis against the apartment and the enquiry NOW, restricted to
    material fields; only a non-empty diff allows staff to permit a re-offer of exactly that apartment. The permission lifts the
    exclusion for that apartment only. Either lift PURGES the lifted apartments' stale listed rows from the live groups containing
    the enquiry (_purge_lifted_rows, inc3 11 Nov 2026); the apartment re-enters through normal generation and rules validation on
    the next run, and offer_apartment refuses it until then with PENDING_VALIDATION (409) - no guaranteed reinstatement.
While a record is ACTIVE its declined rows are not deleted at RECORD time (R5) but are no longer options: one eligibility lens
(matching.excluded_option_ids on the group's evidence capture) removes them from the AI packet, readiness, the evidence hash and
the offer path alike. They are cached working-list rows, pruned silently by the next stock reconcile step or full run; the history
record is this table (+ offered_basis_json), never group_apartments (root, 11 Nov 2026).
Current offer (GP6(d)): when the rejected apartment is a live group's CURRENT apartment, the group is pointed at its best remaining
listed apartment that no member excludes (matching._switch_current - the one place a pointer moves), else at none (held). While a
declined row is still cached the group card shows "declined by <name> on <date>" for it from these records.
No Register text is added by this module; it implements GP14 as written.
"""
from __future__ import annotations

import json
from typing import Optional

from . import stock_lifecycle
from .db import audit, j, now_iso, pj, row, rows

SCOPES = ("apartment", "building")
MAX_REASON = 500
# the enquiry's own material requirements (what the customer asked for): a change here is a "material change" for GP14 too (D2)
ENQUIRY_MATERIAL_FIELDS = ("rooms", "apt_sizes", "budget_option", "move_in", "move_out", "mate_ideal", "areas", "city", "bunk_ok", "party")
LIVE_GROUP_STATUSES = ("Proposed", "Introduced", "Group confirmed", "Awaiting replacement")


class RejectionError(ValueError):
    """Plain-language refusal for the route layer (409/404 decided there)."""


class OfferRefused(RejectionError):
    """GP14-specific refusal of an offer (active exclusion, or a lifted exclusion not yet re-validated by a matching run): the
    routes answer 409 with this sentence; every other offer ValueError keeps its existing 422 (inc3, 11 Nov 2026)."""


PENDING_VALIDATION = ("This apartment is not currently on the group's validated list: the customer's decline was lifted, so the next "
                      "matching run decides whether it is offered to this group again. It cannot be offered before that run.")
LIFT_EVENTS = ("reopened_by_customer", "material_change_reoffer")


# ------------------------------------------------------------------------------------------------------------- exclusions

def building_identity(a: dict | None) -> str:
    """The building a row is in (matching.building_identity: street without unit + recorded suburb + city; '' when no street)."""
    from . import matching
    return matching.building_identity(a)


def records_for(c, enquiry_id: int) -> list[dict]:
    """The enquiry's ACTIVE records as the matcher sees them: [{'id', 'scope', 'key', 'allowed': [apartment ids permitted under
    THAT record]}], ordered by id. Decisions are made on these; the flat summary below is for display only."""
    out = []
    for r in rows(c.execute("SELECT id, scope, apartment_id, building_key FROM apartment_rejections WHERE enquiry_id=? AND status='active' ORDER BY id", (enquiry_id,))):
        key = (str(int(r["apartment_id"])) if r["apartment_id"] is not None else "") if r["scope"] == "apartment" else (r["building_key"] or "")
        if not key:
            continue   # apartment-scope record whose apartment row was deleted: nothing left to exclude
        permitted = sorted({int(e["apartment_id"]) for e in rows(c.execute(
            "SELECT apartment_id FROM apartment_rejection_events WHERE rejection_id=? AND event='material_change_reoffer' AND apartment_id IS NOT NULL", (r["id"],)))})
        out.append({"id": int(r["id"]), "scope": r["scope"], "key": key, "allowed": permitted})
    return out


def records_for_decision(c, enquiry_id: int) -> list[tuple]:
    """G-SL2: the enquiry's ACTIVE records as (scope, key, allowed) tuples for a decision about an apartment whose ROW no longer exists.
    Same shape and allowance rule as records_for, except that an apartment-scope record whose apartment_id was set NULL by the row's
    deletion takes its key from the record's own immutable `recorded` event (no inference). A record with no resolvable key is dropped
    (never a wildcard)."""
    out = []
    for r in rows(c.execute("SELECT id, scope, apartment_id, building_key FROM apartment_rejections WHERE enquiry_id=? AND status='active' ORDER BY id", (enquiry_id,))):
        if r["scope"] == "apartment":
            key = str(int(r["apartment_id"])) if r["apartment_id"] is not None else ""
            if not key:
                ev = row(c.execute("SELECT apartment_id FROM apartment_rejection_events WHERE rejection_id=? AND event='recorded' AND apartment_id IS NOT NULL ORDER BY id LIMIT 1", (r["id"],)))
                key = str(int(ev["apartment_id"])) if ev else ""
        else:
            key = r["building_key"] or ""
        if not key:
            continue
        permitted = tuple(sorted({str(int(e["apartment_id"])) for e in rows(c.execute(
            "SELECT apartment_id FROM apartment_rejection_events WHERE rejection_id=? AND event='material_change_reoffer' AND apartment_id IS NOT NULL", (r["id"],)))}))
        out.append((r["scope"], key, permitted))
    return out


def group_declined_absent_row(c, group_id: int, apartment_id: int, building_key: str) -> bool:
    """G-SL2: for an apartment whose row is GONE - does any CURRENT member hold an active record that applies to it? Apartment scope by
    the record's recorded event id; building scope only when the caller's captured key is non-empty and equal (matching.exclusion_applies:
    '' never matches). Allowances are each record's own `material_change_reoffer` events."""
    from . import matching
    bk = building_key or ""
    for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (group_id,))):
        if any(matching.exclusion_applies(rec, apartment_id, bk) for rec in records_for_decision(c, int(m["enquiry_id"]))):
            return True
    return False


def _as_tuple(rec: dict) -> tuple:
    return (rec["scope"], str(rec["key"]), tuple(sorted(str(x) for x in rec.get("allowed") or [])))


def exclusions_for(c, enquiry_id: int) -> dict:
    """DISPLAY summary {'apartments': [ids], 'buildings': [keys], 'allowed': [ids], 'records': [...]} from the ACTIVE records.
    'apartments' lists apartment-scope keys that still exclude their apartment, 'allowed' every permitted apartment id under any
    record. Nothing decides on the flat lists: the matcher and `excludes` evaluate `records` one by one."""
    recs = records_for(c, enquiry_id)
    apartments = sorted({int(r["key"]) for r in recs if r["scope"] == "apartment" and int(r["key"]) not in set(r["allowed"])})
    buildings = sorted({r["key"] for r in recs if r["scope"] == "building"})
    allowed = sorted({a for r in recs for a in r["allowed"]})
    return {"apartments": apartments, "buildings": buildings, "allowed": allowed, "records": recs}


def refresh_exclusions(c, enquiry_id: int) -> dict:
    """Write the enquiry's current records onto its row (NULL when none). Same write as the change that caused it."""
    ex = exclusions_for(c, enquiry_id)
    val = j({"records": ex["records"]}) if ex["records"] else None
    c.execute("UPDATE enquiries SET apartment_exclusions_json=? WHERE id=?", (val, enquiry_id))
    return ex


def excludes(ex, apartment: dict) -> bool:
    """Does this enquiry's stored exclusion data (the row's JSON, parsed or raw) exclude this apartment ROW now? Same decision as
    matching.unit_excludes_apartment: ANY active record that applies and has no permission for this apartment wins."""
    from . import matching
    if isinstance(ex, (tuple, list)) and all(isinstance(x, tuple) for x in ex):
        recs = tuple(ex)
    else:
        recs = matching.exclusions_from_json(ex if isinstance(ex, (str, bytes)) or ex is None else j(ex))
    bk = building_identity(apartment)
    return any(matching.exclusion_applies(r, apartment["id"], bk) for r in recs)


def group_exclusion_reason(c, group_id: int, apartment_id: int) -> Optional[str]:
    """Plain sentence when a member of the group has declined this apartment (or its building); None when nobody has."""
    apt = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
    if not apt:
        return None
    names = []
    for m in rows(c.execute("SELECT e.id, e.first_name, e.apartment_exclusions_json FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? ORDER BY e.id", (group_id,))):
        if excludes(m.get("apartment_exclusions_json"), apt):
            names.append(m.get("first_name") or f"enquiry {m['id']}")
    if not names:
        return None
    return f"{', '.join(names)} declined this apartment or its building; it cannot be offered to this group unless the customer reopens it or something material changes"


# ------------------------------------------------------------------------------------------------------------------ basis

def _apartment_material(a: dict) -> dict:
    return {k: v for k, v in a.items() if stock_lifecycle.is_material_field(k)}


def _enquiry_material(e: dict) -> dict:
    return {k: e.get(k) for k in ENQUIRY_MATERIAL_FIELDS if k in e}


def _basis(c, enquiry_id: int, apartment: dict, group_id: int | None) -> tuple[str, dict]:
    """(basis_kind, basis). 'group_option' when the enquiry is a member of `group_id` and that group lists the apartment: the option as
    shown (rank, dates, rate kind, totals, rooms) + material fields; otherwise 'apartment_only' (material fields only)."""
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    basis = {"apartment": _apartment_material(apartment), "enquiry": _enquiry_material(e or {}), "taken_at": now_iso()}
    if group_id is not None:
        member = row(c.execute("SELECT 1 FROM group_members WHERE group_id=? AND enquiry_id=?", (group_id, enquiry_id)))
        ga = row(c.execute("SELECT rank, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly, allocation_json FROM group_apartments WHERE group_id=? AND apartment_id=?",
                           (group_id, int(apartment["id"]))))
        if member and ga:
            alloc = pj(ga.get("allocation_json"), {}) or {}
            basis["option"] = {k: ga.get(k) for k in ("rank", "lease_start", "lease_end", "rate_kind", "exit_window", "weekly_rent", "extra_bed", "total_weekly")}
            basis["option"]["rooms"] = alloc.get("rooms")
            return "group_option", basis
    return "apartment_only", basis


# ------------------------------------------------------------------------------------------------------------------ writes

def record(c, enquiry_id: int, scope: str, apartment_id: int, staff: str, customer_reason: str | None = None, group_id: int | None = None) -> dict:
    """Record the customer's rejection (staff enter it). Returns the stored record. Applies GP6(d) to the enquiry's live groups."""
    from . import matching
    if scope not in SCOPES:
        raise RejectionError("Scope must be 'apartment' or 'building'")
    e = row(c.execute("SELECT id, is_test FROM enquiries WHERE id=?", (enquiry_id,)))
    if not e:
        raise RejectionError("No such enquiry")
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
    if not a:
        raise RejectionError("No such apartment")
    bk = building_identity(a)
    if scope == "building" and not bk:
        raise RejectionError("This apartment has no street address to identify its building; record the apartment instead")
    reason = (customer_reason or "").strip()[:MAX_REASON] or None
    # duplicate = an active record of the same scope and key that STILL excludes this apartment. A record whose permission has
    # lifted the exclusion for it does not count: the customer's follow-up decline is a new record (and blocks again).
    from . import matching
    key = str(apartment_id) if scope == "apartment" else bk
    for rec in records_for(c, enquiry_id):
        if rec["scope"] == scope and rec["key"] == key and matching.exclusion_applies(_as_tuple(rec), apartment_id, bk):
            raise RejectionError("This is already recorded for the enquiry")
    kind, basis = _basis(c, enquiry_id, a, group_id)
    before = _live_group_evidence(c, enquiry_id)   # literal pre-write capture of the live groups' evidence (inc3 queue-gap fix)
    when = now_iso()
    cur = c.execute("""INSERT INTO apartment_rejections(enquiry_id, scope, apartment_id, building_key, group_id, basis_kind, offered_basis_json, customer_reason,
                        recorded_by, recorded_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,'active')""",
                    (enquiry_id, scope, apartment_id, bk, group_id, kind, j(basis), reason, staff, when))
    rid = cur.lastrowid or row(c.execute("SELECT MAX(id) AS m FROM apartment_rejections WHERE enquiry_id=?", (enquiry_id,)))["m"]
    _event(c, rid, enquiry_id, "recorded", apartment_id, staff, reason, None)
    refresh_exclusions(c, enquiry_id)
    moved = _apply_to_live_groups(c, enquiry_id, staff)
    requeued = _requeue_changed_evidence(c, enquiry_id, before, staff, f"GP14 record {rid}: the group's effective offer set changed")
    audit(c, staff, "apartment_rejection_recorded", "enquiry", enquiry_id, f"{scope} apartment {apartment_id}" + (f"; current offer moved in group(s) {moved}" if moved else "")
          + (f"; re-review queued for group(s) {requeued}" if requeued else ""))
    rec = get(c, rid)
    rec["groups_repointed"] = moved
    rec["groups_requeued"] = requeued
    return rec


def _live_group_evidence(c, enquiry_id: int) -> dict:
    """{group_id: evidence hash} for the live groups containing the enquiry (one lens: matching.group_evidence)."""
    from . import matching
    return {int(g["id"]): matching.group_evidence(c, int(g["id"]))[0]
            for g in rows(c.execute("""SELECT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id
                                       WHERE gm.enquiry_id=? AND g.status IN (?,?,?,?) ORDER BY g.id""", (enquiry_id, *LIVE_GROUP_STATUSES)))}


def _requeue_changed_evidence(c, enquiry_id: int, before: dict, staff: str, why: str) -> list[int]:
    """inc3 (root queue-gap trace, 11 Nov 2026): a decline changes the group's EFFECTIVE offer set, so the stored review / briefs
    describe an earlier offer. Nothing else re-queues on evidence drift (requeue_interrupted checks contract / rules / model /
    notes only), so the writer does: every live group containing the enquiry whose evidence hash moved in this write is queued
    for a fresh review in the same guarded write (ai_queue.enqueue, commit=False; the stored text stays as history, the group is
    not ready meanwhile). Groups whose evidence did not move - and every unrelated group - are untouched; membership versions and
    response clocks never change here. Returns the group ids queued."""
    from . import ai_queue, matching
    changed = [gid for gid, h in before.items() if matching.group_evidence(c, gid)[0] != h]
    if changed:
        ai_queue.enqueue(c, changed, priority=ai_queue.PRIORITY_NEW, reason=why, commit=False)
        for gid in changed:
            audit(c, staff, "group_review_requeued_after_rejection", "group", gid, why)
    return changed


def reopen_by_customer(c, rejection_id: int, staff: str, note: str | None = None) -> dict:
    r = get(c, rejection_id)
    if r["status"] != "active":
        raise RejectionError("This record is not active")
    when = now_iso()
    c.execute("UPDATE apartment_rejections SET status='reopened', reopened_at=?, reopened_by=?, reopen_route='customer' WHERE id=?", (when, staff, rejection_id))
    _event(c, rejection_id, r["enquiry_id"], "reopened_by_customer", r["apartment_id"], staff, (note or "").strip()[:MAX_REASON] or None, None)
    refresh_exclusions(c, r["enquiry_id"])
    # inc3: the reopening lifts every unit the record covered (its apartment, or every unit of its building); their stale listed
    # rows leave the live groups' option sets and come back only through generation + rules validation (R1)
    _purge_lifted_rows(c, r["enquiry_id"], _covered_by(c, r), staff, f"record {rejection_id} reopened by the customer")
    audit(c, staff, "apartment_rejection_reopened", "enquiry", r["enquiry_id"], f"record {rejection_id}: customer reopened")
    return get(c, rejection_id)


def material_change(c, rejection_id: int, apartment_id: int | None = None) -> dict:
    """{'apartment_id', 'changes': {field: [then, now]}, 'enquiry_changes': {...}, 'any': bool} - server-side, material fields only."""
    r = get(c, rejection_id)
    aid = int(apartment_id if apartment_id is not None else (r["apartment_id"] or 0))
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (aid,))) if aid else None
    if not a:
        raise RejectionError("No such apartment")
    if r["scope"] == "building" and building_identity(a) != r["building_key"]:
        raise RejectionError("That apartment is not in the building this record is about")
    if r["scope"] == "apartment" and aid != int(r["apartment_id"] or 0):
        raise RejectionError("That is not the apartment this record is about")
    basis = r.get("offered_basis") or {}
    then_a = basis.get("apartment") or {}
    # the basis was taken for the record's own apartment; for another unit of the building there is nothing to compare against,
    # so only the enquiry side can show a change (the unit itself was never offered to this person)
    changes = stock_lifecycle.material_changes(then_a, _apartment_material(a)) if then_a and aid == int(r["apartment_id"] or 0) else {}
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (r["enquiry_id"],))) or {}
    then_e = basis.get("enquiry") or {}
    now_e = _enquiry_material(e)
    enquiry_changes = {k: [then_e.get(k), now_e.get(k)] for k in ENQUIRY_MATERIAL_FIELDS if k in then_e and _norm(then_e.get(k)) != _norm(now_e.get(k))}
    return {"apartment_id": aid, "changes": changes, "enquiry_changes": enquiry_changes, "any": bool(changes or enquiry_changes)}


def allow_reoffer(c, rejection_id: int, apartment_id: int, staff: str, note: str | None = None) -> dict:
    """Staff permit a re-offer of exactly this apartment after a material change. Lifts the exclusion for that apartment only; the
    record stays active as history; the apartment's stale listed rows are purged from the live groups containing the enquiry and it
    must pass normal generation and rules on the next run before it can be offered (offer_apartment: PENDING_VALIDATION until then)."""
    r = get(c, rejection_id)
    if r["status"] != "active":
        raise RejectionError("This record is not active")
    diff = material_change(c, rejection_id, apartment_id)
    if not diff["any"]:
        raise RejectionError("Nothing material has changed about this apartment or the enquiry since it was declined, so it cannot be offered again")
    if row(c.execute("SELECT 1 FROM apartment_rejection_events WHERE rejection_id=? AND event='material_change_reoffer' AND apartment_id=?", (rejection_id, apartment_id))):
        raise RejectionError("A re-offer of this apartment is already permitted; it is offered again once a matching run validates it")
    _event(c, rejection_id, r["enquiry_id"], "material_change_reoffer", apartment_id, staff, (note or "").strip()[:MAX_REASON] or None,
           j({"changes": diff["changes"], "enquiry_changes": diff["enquiry_changes"]}))
    refresh_exclusions(c, r["enquiry_id"])
    _purge_lifted_rows(c, r["enquiry_id"], {int(apartment_id)}, staff, f"record {rejection_id}: re-offer of apartment {apartment_id} permitted")   # inc3: the ONE permitted unit
    audit(c, staff, "apartment_rejection_reoffer_permitted", "enquiry", r["enquiry_id"], f"record {rejection_id}: apartment {apartment_id}, pending validation by the next matching run")
    return get(c, rejection_id)


# ------------------------------------------------------------------------------------------------------------------- reads

def get(c, rejection_id: int) -> dict:
    r = row(c.execute("SELECT * FROM apartment_rejections WHERE id=?", (rejection_id,)))
    if not r:
        raise RejectionError("No such record")
    r["events"] = rows(c.execute("SELECT event, apartment_id, staff, at, note, change_basis_json FROM apartment_rejection_events WHERE rejection_id=? ORDER BY id", (rejection_id,)))
    r["reoffer_permitted_apartments"] = sorted({int(e["apartment_id"]) for e in r["events"] if e["event"] == "material_change_reoffer" and e["apartment_id"] is not None})
    r["offered_basis"] = pj(r.pop("offered_basis_json"), {})
    for e in r["events"]:
        e["change_basis"] = pj(e.pop("change_basis_json"), None)
    a = row(c.execute("SELECT address, suburb FROM apartments WHERE id=?", (r["apartment_id"],))) if r["apartment_id"] else None
    r["address"] = (a or {}).get("address") or (r["offered_basis"].get("apartment") or {}).get("address")
    r["suburb"] = (a or {}).get("suburb") or (r["offered_basis"].get("apartment") or {}).get("suburb")
    return r


def for_enquiry(c, enquiry_id: int) -> list[dict]:
    return [get(c, r["id"]) for r in rows(c.execute("SELECT id FROM apartment_rejections WHERE enquiry_id=? ORDER BY id DESC", (enquiry_id,)))]


def declined_in_group(c, group_id: int) -> dict:
    """{apartment_id: [{'name', 'at', 'scope'}]} for the group's listed apartments that a current member's active record still
    excludes (a unit a permission has lifted is not shown as declined). Same decision as the matcher, record by record."""
    from . import matching
    out: dict = {}
    listed = rows(c.execute("SELECT ga.apartment_id, a.address, a.suburb, a.city FROM group_apartments ga JOIN apartments a ON a.id=ga.apartment_id WHERE ga.group_id=?", (group_id,)))
    members = rows(c.execute("SELECT e.id, e.first_name FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=?", (group_id,)))
    for m in members:
        recs = records_for(c, m["id"])
        if not recs:
            continue
        at_by_id = {int(r["id"]): r["recorded_at"] for r in rows(c.execute("SELECT id, recorded_at FROM apartment_rejections WHERE enquiry_id=? AND status='active'", (m["id"],)))}
        for ap in listed:
            bk = building_identity(ap)
            for rec in recs:
                if matching.exclusion_applies(_as_tuple(rec), ap["apartment_id"], bk):
                    out.setdefault(int(ap["apartment_id"]), []).append({"name": m.get("first_name") or f"enquiry {m['id']}", "at": at_by_id.get(rec["id"]), "scope": rec["scope"]})
    return out


# ----------------------------------------------------------------------------------------------------------------- helpers

def _norm(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return "" if v is None else str(v)


def _event(c, rid: int, enquiry_id: int, event: str, apartment_id, staff: str, note, change_basis_json) -> None:
    c.execute("INSERT INTO apartment_rejection_events(rejection_id, enquiry_id, event, apartment_id, staff, at, note, change_basis_json) VALUES (?,?,?,?,?,?,?,?)",
              (rid, enquiry_id, event, apartment_id, staff, now_iso(), note, change_basis_json))


def _covered_by(c, r: dict) -> set:
    """Apartment ids a record covers among the live groups' listed rows: its apartment (apartment scope) or every listed unit of
    its building (building scope, same immutable identity the record was stored with)."""
    if r["scope"] == "apartment":
        return {int(r["apartment_id"])} if r.get("apartment_id") is not None else set()
    key = r.get("building_key") or ""
    if not key:
        return set()
    out = set()
    for ga in rows(c.execute("""SELECT DISTINCT ga.apartment_id FROM group_apartments ga JOIN groups g ON g.id=ga.group_id
                                JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status IN (?,?,?,?)""",
                             (r["enquiry_id"], *LIVE_GROUP_STATUSES))):
        a = row(c.execute("SELECT * FROM apartments WHERE id=?", (ga["apartment_id"],)))
        if a and building_identity(a) == key:
            out.add(int(ga["apartment_id"]))
    return out


def _purge_lifted_rows(c, enquiry_id: int, lifted: set, staff: str, why: str) -> list[dict]:
    """inc3 (approved R1 shape, 11 Nov 2026): when an exclusion is LIFTED (customer reopening, material-change permission) the stale
    group_apartments rows of the lifted apartments are deleted from every live group containing this enquiry - the option can only
    return through normal generation + rules validation (the next run / 'Run matching now'); until then offer_apartment refuses it
    (PENDING_VALIDATION). Only rows whose apartment is no longer excluded for that group go (a unit another member's or another
    record's active exclusion still blocks stays, as history, ineligible either way). A covered row that is the group's CURRENT
    apartment (legacy / inc2b state: a cached row that survived the lift) is purged too - never exempt for being current - and in
    the SAME transaction the pointer moves to the best remaining eligible listed row (matching._switch_current, the one place a
    pointer moves) or to none (held), the _apply_to_live_groups pattern including the offer-changed stamp. The purged row was
    already outside the EFFECTIVE offer set, so the evidence hash does not move by itself (root, 11 Nov 2026): the review and
    briefs go non-current because this writer queues the group (ai_queue.enqueue PRIORITY_NEW clears the stored evidence) - the
    lifted apartment is a pending candidate, not a guarantee of reinstatement. With nothing cached to purge (row already pruned by
    a stock step or a full run, or never listed) nothing effective changed: no enqueue, the existing review stays current, and the
    apartment still answers PENDING_VALIDATION on offer until a run relists it (then the effective set grows and the hash moves).
    No other group's rows or pointer are touched."""
    from . import ai_queue, matching
    if not lifted:
        return []
    purged = []
    for g in rows(c.execute("""SELECT g.id, g.apartment_id, g.ai_status FROM groups g JOIN group_members gm ON gm.group_id=g.id
                               WHERE gm.enquiry_id=? AND g.status IN (?,?,?,?) ORDER BY g.id""", (enquiry_id, *LIVE_GROUP_STATUSES))):
        gone = []
        for ga in rows(c.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],))):
            aid = int(ga["apartment_id"])
            if aid not in lifted:
                continue
            if group_exclusion_reason(c, g["id"], aid):
                continue   # still excluded for this group by another record / member: the row stays as history, ineligible as before
            c.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], aid))
            gone.append(aid)
        if gone:
            if g["apartment_id"] is not None and int(g["apartment_id"]) in gone:
                nxt = None
                for ga in rows(c.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],))):
                    if not group_exclusion_reason(c, g["id"], int(ga["apartment_id"])):
                        nxt = int(ga["apartment_id"])
                        break
                if nxt is not None:
                    matching._switch_current(c, g["id"], nxt)
                else:
                    c.execute("UPDATE groups SET apartment_id=NULL, offer_changed_since_intro_at=COALESCE(offer_changed_since_intro_at, CASE WHEN status IN ('Introduced','Group confirmed') THEN ? END), updated_at=? WHERE id=?",
                              (now_iso(), now_iso(), g["id"]))
                audit(c, staff, "group_offer_moved_after_rejection", "group", g["id"], f"from apartment {g['apartment_id']} to {nxt if nxt is not None else 'none (held)'} (lifted row purged, pending validation)")
            c.execute("UPDATE groups SET updated_at=? WHERE id=?", (now_iso(), g["id"]))
            ai_queue.enqueue(c, [g["id"]], priority=ai_queue.PRIORITY_NEW, reason="GP14 option set changed after a lifted decline", commit=False)
            audit(c, staff, "group_options_purged_after_lift", "group", g["id"], f"{why}; apartments {', '.join(map(str, gone))} await the next matching run")
            purged.append({"group_id": int(g["id"]), "apartment_ids": gone})
    return purged


def pending_validation_reason(c, group_id: int, apartment_id: int) -> Optional[str]:
    """GP14 (inc3): PENDING_VALIDATION when a member of the group has a record covering this apartment whose exclusion was lifted
    (customer reopening or a permission for this apartment) and the apartment is not on the group's validated list; None otherwise
    (the caller then reports the ordinary 'not on this group's list')."""
    apt = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
    if not apt:
        return None
    bk = building_identity(apt)
    for r in rows(c.execute("""SELECT r.id, r.scope, r.apartment_id, r.building_key, r.status FROM apartment_rejections r
                               JOIN group_members gm ON gm.enquiry_id=r.enquiry_id WHERE gm.group_id=? ORDER BY r.id""", (group_id,))):
        covers = (int(r["apartment_id"] or 0) == int(apartment_id)) if r["scope"] == "apartment" else bool(bk) and r["building_key"] == bk
        if not covers:
            continue
        if r["status"] == "reopened":
            return PENDING_VALIDATION
        if row(c.execute("SELECT 1 FROM apartment_rejection_events WHERE rejection_id=? AND event='material_change_reoffer' AND apartment_id=?", (r["id"], int(apartment_id)))):
            return PENDING_VALIDATION
    return None


def _apply_to_live_groups(c, enquiry_id: int, staff: str) -> list[int]:
    """GP6(d): for every live group containing the enquiry whose CURRENT apartment is now excluded, point the group at its best
    remaining listed apartment no member excludes, else at none. Listed rows are not deleted here (R5). Returns group ids moved."""
    from . import matching
    moved = []
    for g in rows(c.execute("""SELECT g.id, g.apartment_id FROM groups g JOIN group_members gm ON gm.group_id=g.id
                               WHERE gm.enquiry_id=? AND g.status IN (?,?,?,?)""", (enquiry_id, *LIVE_GROUP_STATUSES))):
        if g["apartment_id"] is None or not group_exclusion_reason(c, g["id"], int(g["apartment_id"])):
            continue
        nxt = None
        for ga in rows(c.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (g["id"],))):
            if int(ga["apartment_id"]) != int(g["apartment_id"]) and not group_exclusion_reason(c, g["id"], int(ga["apartment_id"])):
                nxt = int(ga["apartment_id"])
                break
        if nxt is not None:
            matching._switch_current(c, g["id"], nxt)
        else:
            c.execute("UPDATE groups SET apartment_id=NULL, offer_changed_since_intro_at=COALESCE(offer_changed_since_intro_at, CASE WHEN status IN ('Introduced','Group confirmed') THEN ? END), updated_at=? WHERE id=?",
                      (now_iso(), now_iso(), g["id"]))
        audit(c, staff, "group_offer_moved_after_rejection", "group", g["id"], f"from apartment {g['apartment_id']} to {nxt if nxt is not None else 'none (held)'}")
        moved.append(int(g["id"]))
    return moved
