"""HomeBot integrated matching, slice H2: persistence and workflow around the pure module `homebot_match` (H1).

What this module adds (register H20–H31, GP6(d), GP14, T3(a) completion brief; root amendments 1–9 of 9 Nov 2026):
  * requirement VERSIONS per enquiry and scenario (immutable basis snapshot + hash; the staff-confirmed planning budget for the
    `final_size` scenario lives in its own table with the basis it was confirmed against, H24);
  * SHORTLIST runs as append-only history; exactly one current result per enquiry and scenario; `complete` means the computation
    finished — it is NOT an AI-reviewed or ready-to-send state (review_state stays `unreviewed` until the grounded H7/H14 review);
  * a durable JOB per enquiry and scenario with desired/claimed generations, owner fencing on every write, coalescing of repeated
    triggers, stale-claim takeover, bounded retries and finite error categories (no exception text is ever stored);
  * a final GUARDED write that re-checks the claim, the current requirement version and the exact inventory snapshot hash: a
    result computed on a stale basis is stored as `superseded` and never becomes current; the job is queued again;
  * customer REJECTIONS of an apartment or a building (immutable offered basis) with GP14's two reopening routes — explicit
    customer reopening, or a server-computed material change plus explicit staff action, scoped to ONE apartment while the
    building rejection stays active; every event is retained;
  * inventory triggers that re-run EVERY open enquiry against the whole inventory (newly feasible stock and previously excluded
    apartments included) and raise an internal attention alert when a listed apartment is no longer available;
  * retention: everything here is removed with the enquiry (delete, test-data purge, T5 purge), including attention alerts.

Switched OFF unless MMAI_HOMEBOT_MATCHING is set (restricted build): no jobs, no worker, routes report the switch plainly.
No travel provider is consulted (estimates=None): every known destination stays "needs confirmation" until the routing
credential works (root recheck 9 Nov 2026: API key invalid). No customer message is produced here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import alerts, homebot, homebot_bedding as bedding, homebot_match as hm, lease_dates
from . import apartment_features as feat
from .db import (DirtyConnectionAtGuardEntry, RetentionGuardError, RetentionLockTimeout, audit, bounded_write, j, now_iso, pj, retention_guard, row,
                 rows, transaction)
from .enquiries import ValidationError

log = logging.getLogger("mmai.homebot_shortlist")

GATE_ENV = "MMAI_HOMEBOT_MATCHING"
SCENARIOS = (hm.SCENARIO_CURRENT, hm.SCENARIO_FINAL_SIZE)
SCENARIO_LABEL = {hm.SCENARIO_CURRENT: "Options for the people in the party now",
                  hm.SCENARIO_FINAL_SIZE: "Provisional options for the planned final size (staff-confirmed planning budget)"}
OPEN_STATUSES = tuple(s for s in homebot.STATUSES if s not in ("Leased", "Closed"))
MAX_ATTEMPTS = 5
BACKOFF_MIN = (1, 5, 15, 60, 240)
STALE_CLAIM_MIN = 10
HEARTBEAT_SEC = 60          # while computing, the owner renews its lease (locked_at) at most this often: a short fenced write, no guard held
ALGORITHM_VERSION = "h1-" + hashlib.sha256(open(os.path.join(os.path.dirname(__file__), "homebot_match.py"), "rb").read()).hexdigest()[:12]

# job / run states
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
COMPLETE, UNRESOLVED_PARTIAL, SUPERSEDED, RUN_FAILED = "complete", "unresolved_partial", "superseded", "failed"
UNREVIEWED = "unreviewed"

# finite error categories (amendment 5): a fixed token and a fixed staff sentence; never an exception message or class name
ERROR_SENTENCES = {
    "requirements_invalid": "The saved requirements could not be used for matching; a staff member needs to review them.",
    "inventory_unreadable": "An apartment's bedding details could not be read; the apartment list needs correcting.",
    "database": "The database was busy or unavailable while preparing options; the run is retried automatically.",
    "unknown": "Options could not be prepared because of an unexpected problem; the run is retried automatically.",
}
SWITCHED_OFF = "HomeBot apartment matching is switched off in this build."
INVENTORY_COLUMNS = ("id", "address", "suburb", "city", "type_code", "bathrooms", "rent_12m", "rent_6m", "available_from", "min_lease_months",
                     "bedroom1", "bedroom2", "bedroom3", "leased_at") + tuple(f"feat_{k}" for k in feat.KEYS)

# attention alert kinds (alerts.EVENTS; record type homebot_enquiry)
EV_REVIEW_UNAVAILABLE = "homebot_review_unavailable"
EV_READY, EV_CHANGED, EV_UNAVAILABLE, EV_FAILED, EV_REVIEW = ("homebot_shortlist_ready", "homebot_shortlist_changed",
                                                              "homebot_apartment_unavailable", "homebot_matching_failed", "homebot_needs_review")

_worker: Optional[threading.Thread] = None
_wake = threading.Event()


def enabled() -> bool:
    return (os.environ.get(GATE_ENV) or "").strip().lower() in ("1", "on", "true", "yes")


class SwitchedOff(RuntimeError):
    pass


def _require_on() -> None:
    if not enabled():
        raise SwitchedOff(SWITCHED_OFF)


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _hash(v) -> str:
    return hashlib.sha256(_canon(v).encode("utf-8")).hexdigest()


def _now_dt(now: Optional[str]) -> datetime:
    return datetime.fromisoformat(now) if now else datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------------------------------------- inventory
def inventory_rows(c) -> list[dict]:
    """Every apartment row with exactly the columns matching reads (leased rows included: the pure module excludes them)."""
    return rows(c.execute("SELECT " + ", ".join(INVENTORY_COLUMNS) + " FROM apartments ORDER BY id"))


def inventory_hash(inv: list[dict]) -> str:
    """Canonical hash of the complete matching-relevant inventory: a deletion, lease, relist, rent, date, bedding or feature
    change anywhere changes it (amendment 1). Diagnostic watermarks are not used as tokens."""
    return _hash([{k: r.get(k) for k in INVENTORY_COLUMNS} for r in inv])


# ------------------------------------------------------------------------------------------------- planning (H24)
def planning_basis(e: dict) -> dict:
    return {"party_mode": e.get("party_mode"), "headcount_now": e.get("headcount_now"), "headcount_target": e.get("headcount_target"),
            "budget_total_weekly": e.get("budget_total_weekly")}


def active_planning(c, e: dict) -> Optional[dict]:
    """The staff-confirmed planning budget that still applies: latest unsuperseded confirmation whose basis (party mode, head
    counts, stated budget) matches the enquiry as it is now. A confirmation whose basis no longer applies is not used."""
    p = row(c.execute("SELECT * FROM homebot_planning WHERE enquiry_id=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (e["id"],)))
    if p and p["basis_hash"] == _hash(planning_basis(e)):
        return p
    return None


def confirm_planning(c, eid: int, budget_total, staff: str, *, now: Optional[str] = None) -> dict:
    """H24: staff record the planning budget for the party's final size. Carries the target size and the basis it was confirmed
    against; a later change to the head counts or stated budget means it must be confirmed again."""
    _require_on()
    e = homebot.get(c, eid)
    if e.get("party_mode") != "more_declined":
        raise ValidationError({"planning_budget": "A planning budget only applies while the party is still growing."})
    try:
        amount = int(budget_total)
    except (TypeError, ValueError):
        raise ValidationError({"planning_budget": "Whole dollars per week."})
    if not (1 <= amount <= homebot.MAX_WEEKLY_TOTAL):
        raise ValidationError({"planning_budget": "Whole dollars per week."})
    staff = str(staff or "").strip()[:homebot.MAX_NAME]
    if not staff:
        raise ValidationError({"staff": "The staff member confirming is required."})
    ts = now or now_iso()
    with retention_guard(c, "homebot planning confirm"):
        e = _capture(c, eid)   # fresh capture under the guard: the target size / basis recorded are the ones that apply now
        if e.get("party_mode") != "more_declined":
            raise ValidationError({"planning_budget": "A planning budget only applies while the party is still growing."})
        c.execute("UPDATE homebot_planning SET superseded_at=?, superseded_reason='confirmed again' WHERE enquiry_id=? AND superseded_at IS NULL", (ts, eid))
        c.execute("INSERT INTO homebot_planning(enquiry_id, target_size, budget_total, basis_json, basis_hash, confirmed_by, confirmed_at) VALUES (?,?,?,?,?,?,?)",
                  (eid, int(e["headcount_target"]), amount, j(planning_basis(e)), _hash(planning_basis(e)), staff, ts))
        _ensure_version(c, e, hm.SCENARIO_FINAL_SIZE, created_by=staff, note="Planning budget confirmed.", now=ts)
        _ensure_job(c, eid, hm.SCENARIO_FINAL_SIZE, "planning_confirmed", ts)
        audit(c, staff, "homebot.planning_confirm", "homebot_enquiry", eid, j({"target_size": e["headcount_target"], "budget_total": amount}))
    kick()
    return planning_state(c, e)


def planning_state(c, e: dict) -> dict:
    p = active_planning(c, e)
    latest = row(c.execute("SELECT * FROM homebot_planning WHERE enquiry_id=? ORDER BY id DESC LIMIT 1", (e["id"],)))
    state = {"applies": bool(p), "target_size": p and p["target_size"], "budget_total": p and p["budget_total"],
             "confirmed_by": p and p["confirmed_by"], "confirmed_at": p and p["confirmed_at"]}
    if latest and not p:
        state["note"] = ("The last confirmation no longer applies because the party's size or stated budget changed; confirm it again."
                         if latest["superseded_at"] is None or latest["superseded_reason"] == "party size or stated budget changed"
                         else "No planning budget is confirmed at the moment.")
    elif not latest:
        state["note"] = "No planning budget has been confirmed yet." if e.get("party_mode") == "more_declined" else "Not applicable: the party is complete."
    return state


# ------------------------------------------------------------------------------------------- rejections (H25, GP14)
def active_rejections(c, eid: int) -> tuple[list[int], list[str], list[int]]:
    """(rejected apartment ids, rejected building keys, apartments with a narrow re-offer allowance). DISPLAY shape: the flat
    union of every active record. The matching decision uses active_rejection_records (per record), never this union."""
    rj = rows(c.execute("SELECT id, scope, apartment_id, building_key FROM homebot_rejections WHERE enquiry_id=? AND status='active'", (eid,)))
    apts = [r["apartment_id"] for r in rj if r["scope"] == "apartment"]
    blds = [r["building_key"] for r in rj if r["scope"] == "building"]
    allowed = [r["apartment_id"] for r in rows(c.execute(
        "SELECT ev.apartment_id FROM homebot_rejection_events ev JOIN homebot_rejections r ON r.id=ev.rejection_id "
        "WHERE r.enquiry_id=? AND r.status='active' AND ev.event='reoffer_allowed' AND ev.apartment_id IS NOT NULL", (eid,)))]
    return apts, blds, allowed


PRECISION_FULL, PRECISION_STREET_SUBURB, PRECISION_STREET = "full", "street_suburb_legacy", "street_only_legacy"
PRECISION_WORDS = {PRECISION_FULL: "Building identified by street, suburb and city.",
                   PRECISION_STREET_SUBURB: "Declined building identified by street and suburb only (recorded before locality binding).",
                   PRECISION_STREET: "Declined building identified by street name only (recorded before locality binding; no offer snapshot)."}


def record_identity(r: dict) -> dict:
    """The building identity a stored rejection record binds to, derived ONLY from the record's own immutable data (11 Nov 2026,
    H-c legacy design). New records store the full 'street|suburb|city' key. A legacy street-only key is bound to the suburb of
    the apartment row in the record's offered-basis snapshot when that snapshot exists and names the same street (city was not
    in older snapshots -> unknown); otherwise it stays a street-only identity. The live apartments table is never consulted
    (that would rewrite the customer's recorded "no" from today's inventory). Nothing is written."""
    key = str(r.get("building_key") or "")
    if "|" in key:
        comps = hm.identity_from_key(key)
        return {"components": comps, "precision": PRECISION_FULL, "street": comps[0], "suburb": comps[1], "city": comps[2],
                "note": PRECISION_WORDS[PRECISION_FULL]}
    street = hm.identity_from_key(key)[0]
    basis = r.get("offered_basis")
    if basis is None and r.get("offered_basis_json"):
        basis = pj(r["offered_basis_json"])
    snap_row = (basis or {}).get("apartment") if isinstance(basis, dict) else None
    if isinstance(snap_row, dict) and street and hm.identity_components(snap_row.get("address"))[0] == street:
        suburb = hm.row_identity(snap_row)[1]   # key present (even NULL -> recorded empty '') binds; a snapshot without the key does not
        if suburb is not hm.UNKNOWN:
            comps = (street, suburb, hm.UNKNOWN)
            return {"components": comps, "precision": PRECISION_STREET_SUBURB, "street": street, "suburb": comps[1], "city": None,
                    "note": PRECISION_WORDS[PRECISION_STREET_SUBURB]}
    return {"components": (street, hm.UNKNOWN, hm.UNKNOWN), "precision": PRECISION_STREET, "street": street, "suburb": None, "city": None,
            "note": PRECISION_WORDS[PRECISION_STREET]}


def active_rejection_records(c, eid: int) -> list[dict]:
    """Every ACTIVE building rejection record with ITS OWN allowance: [{id, components, allowed}]. This is the matching input
    (hm.Requirements.building_rejections); a permission recorded on one record never reaches another (H-a)."""
    rj = rows(c.execute("SELECT id, scope, apartment_id, building_key, offered_basis_json FROM homebot_rejections "
                        "WHERE enquiry_id=? AND status='active' AND scope='building' ORDER BY id", (eid,)))
    out = []
    for r in rj:
        allowed = [x["apartment_id"] for x in rows(c.execute(
            "SELECT apartment_id FROM homebot_rejection_events WHERE rejection_id=? AND event='reoffer_allowed' AND apartment_id IS NOT NULL", (r["id"],)))]
        out.append({"id": r["id"], "components": record_identity(r)["components"], "allowed": sorted(set(int(a) for a in allowed))})
    return out


def _row_identity(a: dict) -> tuple:
    """Identity components of an apartment row / inventory-snapshot row (city unknown when the row does not carry it)."""
    return hm.row_identity(a)


def _still_excludes(c, eid: int, scope: str, apartment_id: int, identity: tuple) -> Optional[int]:
    """GP14 duplicate guard (approved shape, 11 Nov 2026): the id of an ACTIVE same-scope record that STILL excludes this
    apartment, else None. A record whose own allowance lifted the apartment does not count: the customer's follow-up decline is a
    new record that blocks again."""
    if scope == "apartment":
        r = row(c.execute("SELECT id FROM homebot_rejections WHERE enquiry_id=? AND status='active' AND scope='apartment' AND apartment_id=? ORDER BY id LIMIT 1",
                          (eid, int(apartment_id))))
        return r["id"] if r else None
    for rec in active_rejection_records(c, eid):
        if hm.identity_matches(rec["components"], identity) and int(apartment_id) not in rec["allowed"]:
            return rec["id"]
    return None


def record_rejection(c, eid: int, *, scope: str, apartment_id: int, staff: str, customer_reason: str = "",
                     shortlist_id: Optional[int] = None, now: Optional[str] = None) -> dict:
    """Staff record that the customer declined an apartment, or a whole building (H25). The offered basis (the candidate as it
    was shown, with its exact offer and the requirement version) is stored immutably for later comparison (GP14)."""
    _require_on()
    e = homebot.get(c, eid)
    if scope not in ("apartment", "building"):
        raise ValidationError({"scope": "Either the apartment or the building."})
    a = row(c.execute("SELECT id, address, suburb, city FROM apartments WHERE id=?", (int(apartment_id),)))
    if not a:
        raise ValidationError({"apartment_id": "Unknown apartment."})
    # GP14 identity (11 Nov 2026): the full 'street|suburb|city' identity of the row as recorded now, stored immutably on the
    # record for either scope. A building can only be declined when the row has a street (H-b): '' is never a building.
    identity_key = hm.building_identity(a.get("address"), a.get("suburb"), a.get("city"))
    if scope == "building" and not identity_key:
        raise ValidationError({"scope": "This apartment has no street address recorded, so only the apartment itself can be declined."})
    staff = str(staff or "").strip()[:homebot.MAX_NAME]
    if not staff:
        raise ValidationError({"staff": "The staff member recording this is required."})
    reason = str(customer_reason or "").strip()
    if len(reason) > 1000:
        raise ValidationError({"customer_reason": "Up to 1000 characters."})
    ts = now or now_iso()
    basis = _offered_basis(c, eid, int(apartment_id), shortlist_id, scope=scope)
    with retention_guard(c, "homebot rejection record"):
        dup = _still_excludes(c, eid, scope, int(apartment_id), _row_identity(a))
        if dup is not None:
            raise ValidationError({"apartment_id": "The customer's decline of this " + ("building" if scope == "building" else "apartment")
                                   + " is already on record and still excludes it; reopen that record instead of recording it again."})
        cur = c.execute("INSERT INTO homebot_rejections(enquiry_id, scope, apartment_id, building_key, shortlist_id, requirement_version_id, "
                        "offered_basis_json, customer_reason, recorded_by, recorded_at, status) VALUES (?,?,?,?,?,?,?,?,?,?,'active')",
                        (eid, scope, int(apartment_id), identity_key, basis.get("shortlist_id"), basis.get("requirement_version_id"),
                         j(basis), reason or None, staff, ts))
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO homebot_rejection_events(rejection_id, enquiry_id, event, apartment_id, staff, at, note) VALUES (?,?,?,?,?,?,?)",
                  (rid, eid, "recorded", int(apartment_id), staff, ts, reason or None))
        e = _capture(c, eid)
        _bump_all_versions(c, e, created_by=staff, note=f"Customer declined {'the building at ' if scope == 'building' else ''}{a['address']}.", now=ts)
        _ensure_job(c, eid, hm.SCENARIO_CURRENT, "rejection", ts)
        if active_planning(c, e):
            _ensure_job(c, eid, hm.SCENARIO_FINAL_SIZE, "rejection", ts)
        audit(c, staff, "homebot.rejection", "homebot_enquiry", eid, j({"scope": scope, "apartment_id": int(apartment_id)}))
    kick()
    return rejection(c, rid)


def _offered_basis(c, eid: int, apartment_id: int, shortlist_id: Optional[int], *, scope: str = "apartment") -> dict:
    """What the customer actually declined (GP14, root rejection-basis probe 9 Nov): the candidate dict as it was shown (from the
    given or current shortlist), the requirement version and SCENARIO it was computed under, and the apartment row - and for a
    building rejection every apartment of that building - AS THEY WERE IN THE INVENTORY SNAPSHOT THAT SHORTLIST WAS COMPUTED
    FROM (result_json['inventory_snapshot'], written at computation). The live apartment table is never consulted here: the
    inventory may have moved between the offer and the moment staff record the decline, and a later "material change" must be
    measured against the declined offer, not against whatever the table said at recording time. A shortlist without a snapshot
    (legacy row, or no shortlist at all) yields `snapshot_missing`: the rejection is still recorded, but no material change can
    ever be measured for it (material_change fails closed; only the customer's own request reopens it)."""
    sl = None
    if shortlist_id:
        sl = row(c.execute("SELECT * FROM homebot_shortlists WHERE id=? AND enquiry_id=?", (int(shortlist_id), eid)))
        if not sl:
            raise ValidationError({"shortlist_id": "That shortlist does not belong to this enquiry."})
    else:
        sl = row(c.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND is_current=1 ORDER BY id DESC LIMIT 1", (eid,)))
    cand = None
    snapshot = None
    if sl:
        res = pj(sl["result_json"]) or {}
        cand = next((x for x in res.get("candidates", []) if x.get("apartment_id") == apartment_id), None)
        snap = res.get("inventory_snapshot")
        # the snapshot must be the inventory the shortlist row says it was computed from; anything else is not evidence
        if isinstance(snap, list) and inventory_hash(snap) == sl["inventory_hash"]:
            snapshot = snap
    out = {"shortlist_id": sl and sl["id"], "scenario": sl and sl["scenario"], "requirement_version_id": sl and sl["requirement_version_id"],
           "inventory_hash": sl and sl["inventory_hash"], "candidate": cand, "apartment": None, "snapshot_missing": snapshot is None}
    if snapshot is not None:
        out["apartment"] = next((r for r in snapshot if r.get("id") == apartment_id), None)
        if scope == "building" and out["apartment"]:
            ident = _row_identity(out["apartment"])   # same snapshot, same known components on both sides
            out["building_apartments"] = [r for r in snapshot if ident[0] and hm.identity_matches(ident, _row_identity(r))]
    return out


def rejection(c, rid: int) -> dict:
    r = row(c.execute("SELECT * FROM homebot_rejections WHERE id=?", (rid,)))
    if not r:
        raise KeyError("rejection not found")
    r["offered_basis"] = pj(r.pop("offered_basis_json"))
    r["events"] = [dict(ev, change_basis=pj(ev.pop("change_basis_json"))) for ev in
                   rows(c.execute("SELECT * FROM homebot_rejection_events WHERE rejection_id=? ORDER BY id", (rid,)))]
    if r["scope"] == "building":
        ident = record_identity(r)
        r["building_identity"] = {k: ident[k] for k in ("street", "suburb", "city", "precision", "note")}
        r["allowed_apartment_ids"] = sorted({ev["apartment_id"] for ev in r["events"] if ev["event"] == "reoffer_allowed" and ev["apartment_id"] is not None})
    return r


def reopen_by_customer(c, rid: int, staff: str, note: str, *, now: Optional[str] = None) -> dict:
    """GP14 route 1: the customer explicitly asked to reconsider. Full scope of that rejection reopens; the rejection row and
    every earlier event stay visible. Staff action with a note is required; nothing reopens automatically."""
    _require_on()
    rejection(c, rid)   # 404 before the guard
    staff, note = _staff_note(staff, note)
    ts = now or now_iso()
    with retention_guard(c, "homebot rejection reopen"):
        r = rejection(c, rid)   # status re-read inside the guard: a reopening that landed meanwhile is not reopened twice
        if r["status"] != "active":
            raise ValidationError({"rejection": "This rejection is already reopened."})
        e = _capture(c, r["enquiry_id"])
        n = c.execute("UPDATE homebot_rejections SET status='reopened', reopened_at=?, reopened_by=?, reopen_route='customer_request' WHERE id=? AND status='active'", (ts, staff, rid)).rowcount
        if n != 1:
            raise ValidationError({"rejection": "This rejection is already reopened."})
        c.execute("INSERT INTO homebot_rejection_events(rejection_id, enquiry_id, event, apartment_id, staff, at, note) VALUES (?,?,?,?,?,?,?)",
                  (rid, r["enquiry_id"], "reopened_customer", r["apartment_id"], staff, ts, note))
        _bump_all_versions(c, e, created_by=staff, note="Customer asked to reconsider a declined option.", now=ts)
        _ensure_job(c, e["id"], hm.SCENARIO_CURRENT, "rejection_reopened", ts)
        if active_planning(c, e):
            _ensure_job(c, e["id"], hm.SCENARIO_FINAL_SIZE, "rejection_reopened", ts)
        audit(c, staff, "homebot.rejection_reopen", "homebot_enquiry", e["id"], j({"rejection_id": rid, "route": "customer_request"}))
    kick()
    return rejection(c, rid)


NO_BASIS = ("What the customer declined was not captured exactly (no inventory snapshot is on file for that offer), so a material change "
            "cannot be measured. Only the customer's own request to reconsider can reopen this.")


def material_change(c, rid: int, apartment_id: int) -> list[dict]:
    """Server-computed differences between what the customer declined and the apartment / requirements now, restricted to rent,
    dates and the requirement fields that matter to matching. Empty = nothing material changed (no re-offer basis).
    Like for like (root probe, 9 Nov): the apartment is compared with its row in the inventory snapshot the declined shortlist
    was computed from, and the requirements with the CURRENT applicable requirements of the SAME scenario the offer was made
    under (a final-size offer against the final-size scenario, never against the growing party). Missing evidence - no snapshot,
    no scenario, or a scenario that no longer applies (its planning budget was invalidated) - fails closed with a ValidationError
    instead of inventing a difference or declaring 'nothing changed'."""
    r = rejection(c, rid)
    basis = r["offered_basis"] or {}
    if r["scope"] == "apartment" and int(apartment_id) != r["apartment_id"]:
        raise ValidationError({"apartment_id": "An apartment rejection can only be re-offered for that apartment."})
    now_apt = row(c.execute("SELECT " + ", ".join(INVENTORY_COLUMNS) + " FROM apartments WHERE id=?", (int(apartment_id),)))
    if not now_apt:
        raise ValidationError({"apartment_id": "Unknown apartment."})
    if r["scope"] == "building" and not hm.identity_matches(record_identity(r)["components"], _row_identity(now_apt)):
        raise ValidationError({"apartment_id": "That apartment is not in the declined building."})
    if basis.get("snapshot_missing") or not basis.get("apartment") or not basis.get("scenario") or not basis.get("requirement_version_id"):
        raise ValidationError({"apartment_id": NO_BASIS})
    then = basis["apartment"]
    diff = []
    if int(apartment_id) != then.get("id"):
        # a different apartment in the declined building: measured against ITS OWN state in the same snapshot
        then = next((x for x in basis.get("building_apartments") or [] if x.get("id") == int(apartment_id)), None)
        if then is None:
            diff.append({"field": "apartment", "was": None, "now": now_apt["address"], "note": "This apartment was not on the list when the building was declined."})
            then = {}
    for k in ("rent_12m", "rent_6m", "available_from", "min_lease_months", "bedroom1", "bedroom2", "bedroom3", "leased_at"):
        if then and _canon(then.get(k)) != _canon(now_apt.get(k)):
            diff.append({"field": k, "was": then.get(k), "now": now_apt.get(k)})
    old = row(c.execute("SELECT basis_json FROM homebot_requirement_versions WHERE id=? AND enquiry_id=? AND scenario=?",
                        (basis["requirement_version_id"], r["enquiry_id"], basis["scenario"])))
    if not old:
        raise ValidationError({"apartment_id": NO_BASIS})
    try:
        e = homebot.get_record(c, r["enquiry_id"])
    except KeyError:
        raise ValidationError({"apartment_id": "The enquiry is no longer on file."})
    try:
        nb = _requirements(c, e, basis["scenario"]).as_dict()   # the requirements that apply NOW for the SAME scenario
    except hm.InvalidRequirements:
        raise ValidationError({"apartment_id": "The planning budget this offer was prepared under no longer applies (the party's size or stated budget "
                                               "changed), so a material change cannot be measured against it. Confirm the planning budget again first."})
    ob = pj(old["basis_json"]) or {}
    for k in ("whole_apartment_max", "move_in", "move_in_window", "move_out", "move_out_window", "bedroom_sizes", "bathrooms", "area",
              "transport", "max_travel", "extras", "extras_top", "bunk_condition", "people", "head_count"):
        if _canon(ob.get(k)) != _canon(nb.get(k)):
            diff.append({"field": "requirements." + k, "was": ob.get(k), "now": nb.get(k)})
    return diff


def allow_reoffer(c, rid: int, apartment_id: int, staff: str, note: str, *, now: Optional[str] = None) -> dict:
    """GP14 route 2: a material rent/date/requirement change permits STAFF to re-offer ONE apartment. The computed difference is
    stored with the event; the building rejection itself stays active for every unchanged apartment. For an apartment-scope
    rejection the rejection is reopened for that apartment only (it has no wider scope)."""
    _require_on()
    rejection(c, rid)   # 404 before the guard
    staff, note = _staff_note(staff, note)
    ts = now or now_iso()
    with retention_guard(c, "homebot reoffer allowance"):
        # GP14: the rejection's status AND the material-change basis are evaluated INSIDE the guard, from the rows as they are
        # now; a change observed earlier that has since been reverted (or a reopening that landed meanwhile) authorises nothing
        # (root re-offer freshness probe, 9 Nov). The difference stored with the event is exactly the one that authorised it.
        r = rejection(c, rid)
        if r["status"] != "active":
            raise ValidationError({"rejection": "This rejection is already reopened."})
        diff = material_change(c, rid, int(apartment_id))
        if not diff:
            raise ValidationError({"apartment_id": "Nothing material has changed for this apartment since it was declined (rent, dates, bedding or the party's requirements), so it cannot be re-offered on that basis."})
        e = _capture(c, r["enquiry_id"])
        if r["scope"] == "apartment":
            n = c.execute("UPDATE homebot_rejections SET status='reopened', reopened_at=?, reopened_by=?, reopen_route='material_change' WHERE id=? AND status='active'", (ts, staff, rid)).rowcount
            if n != 1:
                raise ValidationError({"rejection": "This rejection is already reopened."})
        c.execute("INSERT INTO homebot_rejection_events(rejection_id, enquiry_id, event, apartment_id, staff, at, note, change_basis_json) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, r["enquiry_id"], "reoffer_allowed", int(apartment_id), staff, ts, note, j(diff)))
        _bump_all_versions(c, e, created_by=staff, note="Staff allowed one declined option to be offered again after a material change.", now=ts)
        _ensure_job(c, e["id"], hm.SCENARIO_CURRENT, "reoffer_allowed", ts)
        if active_planning(c, e):
            _ensure_job(c, e["id"], hm.SCENARIO_FINAL_SIZE, "reoffer_allowed", ts)
        audit(c, staff, "homebot.rejection_reoffer", "homebot_enquiry", e["id"], j({"rejection_id": rid, "apartment_id": int(apartment_id), "changed": [d["field"] for d in diff]}))
    kick()
    return rejection(c, rid)


def _staff_note(staff, note) -> tuple[str, str]:
    staff = str(staff or "").strip()[:homebot.MAX_NAME]
    note = str(note or "").strip()
    if not staff:
        raise ValidationError({"staff": "The staff member is required."})
    if not note or len(note) > 1000:
        raise ValidationError({"note": "A short note (up to 1000 characters) saying why is required."})
    return staff, note


# ------------------------------------------------------------------------------------------- requirement versions
def _requirements(c, e: dict, scenario: str) -> hm.Requirements:
    apts, blds, allowed = active_rejections(c, e["id"])
    records = tuple((x["components"], frozenset(x["allowed"])) for x in active_rejection_records(c, e["id"]))   # the decision input
    planning = None
    if scenario == hm.SCENARIO_FINAL_SIZE:
        p = active_planning(c, e)
        if not p:
            raise hm.InvalidRequirements("final size scenario needs an applicable staff-confirmed planning budget")
        planning = p["budget_total"]
    return hm.requirements_from_enquiry(e, e.get("people") or [], scenario=scenario, planning_budget=planning,
                                        rejected_apartment_ids=apts, rejected_buildings=blds, reoffer_allowed_ids=allowed,
                                        building_rejections=records)


def _current_version(c, eid: int, scenario: str) -> Optional[dict]:
    return row(c.execute("SELECT * FROM homebot_requirement_versions WHERE enquiry_id=? AND scenario=? ORDER BY version DESC LIMIT 1", (eid, scenario)))


def _capture(c, eid: int) -> dict:
    """The enquiry as it is NOW, read inside the caller's guard. Every version/basis write and every computation uses a capture
    taken under the guard (root basis-freshness probe, 9 Nov): a record read before the guard may already be out of date by
    the time the guard is entered, and promoting it would silently undo a concurrent validated edit."""
    return homebot.get_record(c, eid)   # the record, not the matching note: a basis read never re-enters the review state


def _actual_basis_hash(c, e: dict, scenario: str) -> Optional[str]:
    """Hash of the requirements that apply RIGHT NOW for this scenario (enquiry + rejections + applicable planning), or None
    when the scenario cannot be built (final size without an applicable planning budget)."""
    try:
        return _hash(_requirements(c, e, scenario).as_dict())
    except hm.InvalidRequirements:
        return None


def _ensure_version(c, e: dict, scenario: str, *, created_by: str, note: Optional[str], now: str) -> Optional[dict]:
    """Write a new immutable version when the basis changed (or none exists). Inside the caller's transaction. Returns the
    current version row, or None when the scenario cannot be built (final size without an applicable planning budget)."""
    try:
        basis = _requirements(c, e, scenario).as_dict()
    except hm.InvalidRequirements:
        return None
    h = _hash(basis)
    cur = _current_version(c, e["id"], scenario)
    if cur and cur["basis_hash"] == h:
        return cur
    planning = active_planning(c, e) if scenario == hm.SCENARIO_FINAL_SIZE else None
    c.execute("INSERT INTO homebot_requirement_versions(enquiry_id, scenario, version, basis_json, basis_hash, planning_id, created_at, created_by, change_note) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (e["id"], scenario, (cur["version"] + 1) if cur else 1, j(basis), h, planning and planning["id"], now, created_by,
               note or (_change_note(pj(cur["basis_json"]) or {}, basis) if cur else "First version from the submitted form.")))
    return _current_version(c, e["id"], scenario)


def _bump_all_versions(c, e: dict, *, created_by: str, note: Optional[str], now: str) -> None:
    for sc in SCENARIOS:
        _ensure_version(c, e, sc, created_by=created_by, note=note, now=now)


_FIELD_WORDS = {"whole_apartment_max": "whole-apartment maximum", "move_in": "likely move-in date", "move_in_window": "accepted move-in range",
                "move_out": "likely move-out date", "move_out_window": "accepted move-out range", "bedroom_sizes": "bedroom counts",
                "bathrooms": "bathroom preference", "area": "area", "transport": "transport", "max_travel": "travel time", "extras": "extras",
                "extras_top": "most important extras", "bunk_condition": "bunk condition", "people": "people's details", "head_count": "number of people",
                "rejected_apartment_ids": "declined apartments", "rejected_buildings": "declined buildings", "reoffer_allowed_ids": "re-offer allowances",
                "building_rejections": "declined buildings and their re-offer allowances",
                "notes_review": "notes review flag", "priced_types": "priced apartment types", "scenario": "scenario"}


def _change_note(old: dict, new: dict) -> str:
    changed = [k for k in sorted(set(old) | set(new)) if _canon(old.get(k)) != _canon(new.get(k))]
    return "Changed: " + ", ".join(_FIELD_WORDS.get(k, k) for k in changed) + "." if changed else "No change."


def versions(c, eid: int) -> list[dict]:
    out = rows(c.execute("SELECT id, scenario, version, basis_hash, planning_id, created_at, created_by, change_note FROM homebot_requirement_versions "
                         "WHERE enquiry_id=? ORDER BY id", (eid,)))
    for v in out:
        v["scenario_label"] = SCENARIO_LABEL.get(v["scenario"], v["scenario"])
    return out


# ------------------------------------------------------------------------------------ staff requirement edits (H29)
EDITABLE = ("move_in", "move_in_flex", "move_in_earliest", "move_in_latest", "move_out", "move_out_flex", "move_out_earliest", "move_out_latest",
            "budget_total_weekly", "budget_types", "bedrooms", "bathrooms", "area", "transport", "max_travel", "extras", "extras_top",
            "apartment_notes", "bunk_condition", "headcount_target", "people")
PERSON_EDITABLE = ("sleep", "doing", "institution", "institution_other", "place", "primary_destination", "weekly_max", "name")


def _form_payload(e: dict) -> dict:
    """Rebuild a form-shaped submission from the stored enquiry so the SAME validator as the public form checks a staff edit."""
    a = {"first_name": e["first_name"], "last_name": e["last_name"], "email": e["email"], "phone_code": e["phone_code"], "phone": e["phone"],
         "contact_pref": e["contact_pref"], "viewing": e["viewing"], "heard": e.get("heard"),
         "consent_contact": bool(e["consent_contact"]), "consent_lease": bool(e["consent_lease"]), "consent_adults": bool(e["consent_adults"]),
         "move_in": e["move_in"], "move_in_flex": e["move_in_flex"], "move_in_earliest": e.get("move_in_earliest"), "move_in_latest": e.get("move_in_latest"),
         "move_out": e["move_out"], "move_out_flex": e["move_out_flex"], "move_out_earliest": e.get("move_out_earliest"), "move_out_latest": e.get("move_out_latest"),
         "budget_total": e["budget_total_weekly"], "budget_types": list(e["budget_types"] or []), "bedrooms": list(e["bedrooms"] or []),
         "bathrooms": list(e["bathrooms"] or []), "area": e["area"], "transport": list(e["transport"] or []), "max_travel": e["max_travel"],
         "extras": list(e.get("extras") or []), "extras_top": list(e.get("extras_top") or []), "apartment_notes": e.get("apartment_notes"),
         "bunk_condition": e.get("bunk_condition")}
    if e["party_mode"] == "more_declined":
        a.update(need_more="more", headcount_now=e["headcount_now"], headcount_target=e["headcount_target"], want_help="no")
    else:
        a.update(need_more="complete", headcount=e["headcount_now"])
    a["people"] = [{k: p.get(k) for k in ("name", "sleep", "weekly_max", "doing", "institution", "institution_other", "place") if p.get(k) is not None}
                   | ({"primary": p["primary_destination"]} if p.get("primary_destination") else {}) for p in e.get("people") or []]
    for p in a["people"]:
        if p.get("sleep") == homebot.SLEEP_ALONE:
            p.pop("sleep")
    return {k: v for k, v in a.items() if v is not None}


def update_requirements(c, eid: int, patch: dict, staff: str, *, today: Optional[date] = None, now: Optional[str] = None) -> dict:
    """Staff change the party's matching requirements after talking to them (H29). The merged record is validated by the form's
    own validator (same options, same date rules), the enquiry and its people are updated, a new requirement version records
    what changed, and a fresh shortlist is queued. Contact details and consents are not editable here."""
    _require_on()
    if not isinstance(patch, dict) or not patch:
        raise ValidationError({"form": "Nothing to change."})
    bad = sorted(k for k in patch if k not in EDITABLE)
    if bad:
        raise ValidationError({"form": "These fields cannot be changed here: " + ", ".join(str(k)[:40] for k in bad[:6])})
    staff = str(staff or "").strip()[:homebot.MAX_NAME]
    if not staff:
        raise ValidationError({"staff": "The staff member is required."})
    homebot.get(c, eid)   # 404 before the guard
    ts = now or now_iso()
    with retention_guard(c, "homebot requirements edit"):
        e = _capture(c, eid)   # merge against the record as it is NOW, under the guard: a concurrent edit is never undone
        clean, cols, answers = _merge_patch(e, patch, today=today)
        c.execute("UPDATE homebot_enquiries SET " + ", ".join(f"{k}=?" for k in cols) + ", review_flags=?, answers_json=?, updated_at=? WHERE id=?",
                  [*cols.values(), j(clean["flags"]), j(answers), ts, eid])
        c.execute("DELETE FROM homebot_people WHERE enquiry_id=?", (eid,))
        for p in clean["people"]:
            c.execute("INSERT INTO homebot_people(enquiry_id, position, name, sleep, weekly_max, doing, institution, institution_other, place, primary_destination) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (eid, p["position"], p["name"], p["sleep"], p["weekly_max"], p["doing"], p["institution"], p["institution_other"], p["place"], p["primary_destination"]))
        e2 = _capture(c, eid)
        latest = row(c.execute("SELECT id, basis_hash FROM homebot_planning WHERE enquiry_id=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1", (eid,)))
        if latest and latest["basis_hash"] != _hash(planning_basis(e2)):
            c.execute("UPDATE homebot_planning SET superseded_at=?, superseded_reason='party size or stated budget changed' WHERE id=?", (ts, latest["id"]))
        _bump_all_versions(c, e2, created_by=staff, note=None, now=ts)
        _ensure_job(c, eid, hm.SCENARIO_CURRENT, "requirements_changed", ts)
        if active_planning(c, e2):
            _ensure_job(c, eid, hm.SCENARIO_FINAL_SIZE, "requirements_changed", ts)
        audit(c, staff, "homebot.requirements_update", "homebot_enquiry", eid, j({"fields": sorted(patch)}))
    kick()
    return homebot.get(c, eid)


def _merge_patch(e: dict, patch: dict, *, today: Optional[date]) -> tuple[dict, dict, dict]:
    """Pure: apply the staff patch to the enquiry's form payload and validate the merged record with the form's own validator.
    Returns (clean, column values, merged answers). Raises ValidationError; writes nothing."""
    a = _form_payload(e)
    for k, v in patch.items():
        if k == "people":
            if not isinstance(v, list):
                raise ValidationError({"people": "A list of people is expected."})
            if len(v) != len(a["people"]):
                raise ValidationError({"people": f"Details are expected for exactly {len(a['people'])} people (the party's size is not changed here)."})
            for i, (cur, pv) in enumerate(zip(a["people"], v), start=1):
                if not isinstance(pv, dict) or any(x not in PERSON_EDITABLE for x in pv):
                    raise ValidationError({"people": f"Person {i}: unexpected fields."})
                for pk, val in pv.items():
                    key = "primary" if pk == "primary_destination" else pk
                    if val is None or val == "":
                        cur.pop(key, None)
                    else:
                        cur[key] = val
        elif k == "budget_total_weekly":
            a["budget_total"] = v
        elif k == "headcount_target":
            if e["party_mode"] != "more_declined":
                raise ValidationError({"headcount_target": "Only applies while the party is still growing."})
            a["headcount_target"] = v
        elif v is None or v == "":
            a.pop(k, None)
        else:
            a[k] = v
    # a way of travelling that is no longer offered stays on an existing record until staff untick it; it cannot be added
    clean = homebot.validate(a, today=today, keep_transport=tuple(e.get("transport") or ()))
    cols = {k: clean[k] for k in ("move_in", "move_in_flex", "move_in_earliest", "move_in_latest", "move_out", "move_out_flex", "move_out_earliest",
                                  "move_out_latest", "budget_total_weekly", "area", "max_travel", "apartment_notes", "bunk_condition", "headcount_target")}
    cols.update({k: j(clean[k]) for k in ("budget_types", "bedrooms", "bathrooms", "transport", "extras", "extras_top")})
    answers = pj(e.get("answers_json")) or {}
    answers.update({k: v for k, v in clean.items() if k not in ("flags",)})
    return clean, cols, answers


# ------------------------------------------------------------------------------------------------------------- jobs
def _ensure_job(c, eid: int, scenario: str, reason: str, now: str) -> None:
    """One durable job row per enquiry and scenario (inside the caller's transaction). A new trigger raises the DESIRED
    generation; a running claim keeps its own claimed generation and is re-queued at its final write if desired moved on."""
    if not enabled():
        return
    jb = row(c.execute("SELECT id, status, desired_generation FROM homebot_jobs WHERE enquiry_id=? AND scenario=?", (eid, scenario)))
    if not jb:
        c.execute("INSERT INTO homebot_jobs(enquiry_id, scenario, status, reason, desired_generation, claimed_generation, attempts, next_attempt_at, created_at, updated_at) "
                  "VALUES (?,?,?,?,1,0,0,?,?,?)", (eid, scenario, QUEUED, reason, now, now, now))
        return
    if jb["status"] == RUNNING:
        c.execute("UPDATE homebot_jobs SET desired_generation=desired_generation+1, reason=?, updated_at=? WHERE id=?", (reason, now, jb["id"]))
    else:
        c.execute("UPDATE homebot_jobs SET desired_generation=desired_generation+1, status=?, reason=?, attempts=0, next_attempt_at=?, owner=NULL, "
                  "locked_at=NULL, updated_at=? WHERE id=?", (QUEUED, reason, now, now, jb["id"]))


def on_enquiry_created(c, eid: int, *, now: Optional[str] = None) -> None:
    """Hook for homebot.create (same transaction as the enquiry): first requirement version and the first job. A crash before
    commit leaves neither the enquiry nor the job. No-op while the switch is off."""
    if not enabled():
        return
    ts = now or now_iso()
    e = homebot.get(c, eid)
    _ensure_version(c, e, hm.SCENARIO_CURRENT, created_by="customer form", note=None, now=ts)
    _ensure_job(c, eid, hm.SCENARIO_CURRENT, "submission", ts)


def request_rerun(c, eid: int, staff: str, scenario: str = hm.SCENARIO_CURRENT, *, now: Optional[str] = None) -> dict:
    _require_on()
    if scenario not in SCENARIOS:
        raise ValidationError({"scenario": "Unknown scenario."})
    homebot.get(c, eid)   # 404 before the guard; the capture used for the version is taken inside it
    ts = now or now_iso()
    with retention_guard(c, "homebot rerun request"):
        e = _capture(c, eid)
        if scenario == hm.SCENARIO_FINAL_SIZE and not active_planning(c, e):
            raise ValidationError({"scenario": "Confirm a planning budget first."})
        _ensure_version(c, e, scenario, created_by=staff, note=None, now=ts)
        _ensure_job(c, eid, scenario, "staff_rerun", ts)
        audit(c, staff, "homebot.rerun", "homebot_enquiry", eid, j({"scenario": scenario}))
    kick()
    return job_state(c, eid, scenario)


def on_inventory_changed(c, reason: str, apartment_ids: Optional[list] = None, *, now: Optional[str] = None) -> dict:
    """Apartment added / changed / leased / relisted / deleted / imported: queue a fresh run for EVERY open enquiry (the whole
    inventory is re-evaluated, so newly feasible stock and previously excluded apartments are reconsidered, amendment/correction
    5) and raise an attention alert for each current shortlist that lists an apartment which is no longer offerable."""
    if not enabled():
        return {"queued": 0, "alerts": 0}
    ts = now or now_iso()
    queued = alerted = 0
    with retention_guard(c, "homebot inventory trigger"):
        open_ids = [r["id"] for r in rows(c.execute("SELECT id FROM homebot_enquiries WHERE status IN (" + ",".join("?" * len(OPEN_STATUSES)) + ") ORDER BY id", OPEN_STATUSES))]
        for eid in open_ids:
            _ensure_job(c, eid, hm.SCENARIO_CURRENT, "availability", ts)
            if row(c.execute("SELECT id FROM homebot_jobs WHERE enquiry_id=? AND scenario=?", (eid, hm.SCENARIO_FINAL_SIZE))):
                _ensure_job(c, eid, hm.SCENARIO_FINAL_SIZE, "availability", ts)
            queued += 1
        if apartment_ids:
            gone = set()
            for aid in apartment_ids:
                a = row(c.execute("SELECT id, leased_at, available_from FROM apartments WHERE id=?", (int(aid),)))
                if a is None or a["leased_at"] or not a["available_from"]:
                    gone.add(int(aid))
            if gone:
                for sl in rows(c.execute("SELECT id, enquiry_id, listed_ids, is_test FROM homebot_shortlists WHERE is_current=1 AND enquiry_id IN ("
                                         + ",".join("?" * len(open_ids)) + ")", open_ids)) if open_ids else []:
                    if gone & set(pj(sl["listed_ids"]) or []):
                        alerts.raise_attention(c, EV_UNAVAILABLE, sl["enquiry_id"], is_test=bool(sl["is_test"]), revision=sl["id"], in_transaction=True)
                        alerted += 1
    if queued:
        kick()
    return {"queued": queued, "alerts": alerted}


def _claim(c, job_id: int, token: str, now: str) -> Optional[dict]:
    stale = _iso(_now_dt(now) - timedelta(minutes=STALE_CLAIM_MIN))
    with transaction(c):
        cur = c.execute("UPDATE homebot_jobs SET status=?, owner=?, claimed_generation=desired_generation, locked_at=?, attempts=attempts+1, updated_at=? "
                        "WHERE id=? AND ((status=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR (status=? AND locked_at<?))",
                        (RUNNING, token, now, now, job_id, QUEUED, now, RUNNING, stale))
        if cur.rowcount != 1:
            return None
        return row(c.execute("SELECT * FROM homebot_jobs WHERE id=?", (job_id,)))


class ClaimLost(RuntimeError):
    """Raised inside the computation when a heartbeat finds the job is no longer ours (stale takeover): abandon the work."""


HEARTBEAT_DB_TIMEOUT_MS = 2000   # bound on the renewal's wait for the connection and for the job row lock (root renewal probe, 9 Nov)


def _heartbeat(c, jb: dict, token: str, clock) -> None:
    """Owner-fenced lease renewal: one bounded UPDATE that only succeeds while we still own the claim at our generation. A long
    legitimate enumeration therefore never loses its lease to another worker just by taking longer than STALE_CLAIM_MIN; a
    worker that DID lose it (process paused past the threshold, another worker took over) learns so here and stops.
    Runs as a db.bounded_write: a write some other caller left pending on the shared connection is refused untouched (never
    committed as a side effect), and the wait for the connection / the row lock is bounded (RetentionLockTimeout) - a renewal that
    cannot run cleanly is skipped by the caller and tried again at the next tick; only a FAILED fence means the claim is gone."""
    with bounded_write(c, "homebot shortlist heartbeat", HEARTBEAT_DB_TIMEOUT_MS):
        cur = c.execute("UPDATE homebot_jobs SET locked_at=?, updated_at=? WHERE id=? AND owner=? AND status=? AND claimed_generation=?",
                        (clock(), clock(), jb["id"], token, RUNNING, jb["claimed_generation"]))
        if cur.rowcount != 1:
            raise ClaimLost(f"job {jb['id']}")


def _progress(c, jb: dict, token: str, clock, *, every_sec: float):
    last = [time.monotonic()]

    def tick(done: int, total: int) -> None:
        if time.monotonic() - last[0] >= every_sec:
            try:
                _heartbeat(c, jb, token, clock)
            except (DirtyConnectionAtGuardEntry, RetentionLockTimeout) as ex:
                # the renewal could not run cleanly right now (someone else's pending write, or a bounded wait ran out): nothing was
                # written; the lease simply is not refreshed this tick. ClaimLost is NOT caught: a lost claim stops the work.
                log.warning("homebot shortlist job %s: lease renewal skipped (%s)", jb["id"], type(ex).__name__)
            last[0] = time.monotonic()
    return tick


def _mine(c, jb: dict, token: str) -> bool:
    """Read-only check (diagnostics/tests): the job is ours at our claimed generation. Writes never rely on this — they use _fence."""
    r = row(c.execute("SELECT owner, status, claimed_generation FROM homebot_jobs WHERE id=?", (jb["id"],)))
    return bool(r and r["owner"] == token and r["status"] == RUNNING and r["claimed_generation"] == jb["claimed_generation"])


FENCE = "id=? AND owner=? AND status=? AND claimed_generation=?"   # every job write carries the owner + claimed generation


def _fence_args(jb: dict, token: str) -> tuple:
    return (jb["id"], token, RUNNING, jb["claimed_generation"])


def _fence(c, jb: dict, token: str, ts: str) -> None:
    """Owner fencing bound to the write itself: the FIRST statement of every final transaction is an UPDATE of the job row that
    only matches while we own the claim at our generation. On PostgreSQL that UPDATE also takes the row lock for the rest of the
    transaction, so a rival claim/takeover blocks until we commit and then re-evaluates against the committed row (it never
    sees a running job of ours as stale). If the fence matches nothing the claim is gone: ClaimLost is raised INSIDE the open
    transaction, so nothing of this write survives (the enclosing transaction / retention guard rolls back)."""
    if not _mine(c, jb, token):   # cheap read first (diagnostics); the UPDATE below is the real, lock-taking check
        raise ClaimLost(f"job {jb['id']}")
    cur = c.execute("UPDATE homebot_jobs SET updated_at=? WHERE " + FENCE, (ts, *_fence_args(jb, token)))
    if cur.rowcount != 1:
        raise ClaimLost(f"job {jb['id']}")


def _fenced(c, sql: str, params: tuple, jb: dict, token: str) -> None:
    """A job UPDATE whose WHERE ends with the fence; a miss means the claim was lost mid-transaction (cannot happen while the
    fence row lock is held on PostgreSQL, nor inside SQLite's single writer) and aborts the write."""
    cur = c.execute(sql + " WHERE " + FENCE, (*params, *_fence_args(jb, token)))
    if cur.rowcount != 1:
        raise ClaimLost(f"job {jb['id']}")


def _error_kind(ex: BaseException) -> str:
    if isinstance(ex, (hm.InvalidRequirements, ValidationError, KeyError)):
        return "requirements_invalid"
    if isinstance(ex, bedding.InvalidInventory):
        return "inventory_unreadable"
    if isinstance(ex, (sqlite3.Error, RetentionGuardError)) or type(ex).__module__.startswith(("psycopg", "pg8000")):
        return "database"
    return "unknown"


def run_job(c, job_id: int, *, today: Optional[date] = None, now: Optional[str] = None, estimates=None, clock=None,
            heartbeat_sec: Optional[float] = None) -> dict:
    """Claim → compute outside any lock (with owner-fenced lease heartbeats) → final guarded write with fencing and stale-basis
    checks. Returns a small status dict (for tests and the worker log); never raises for a business problem.
    `clock` (tests) returns the current ISO time; `now` fixes the claim time (then the clock defaults to it)."""
    from . import exit_exceptions
    token = secrets.token_hex(8)
    clock = clock or ((lambda: now) if now else now_iso)
    ts = now or clock()
    today = today or lease_dates.business_today()   # A17 business date (Melbourne clock), the same date the rest of MMAI uses
    jb = _claim(c, job_id, token, ts)
    if not jb:
        return {"job_id": job_id, "outcome": "not_claimed"}
    try:
        # ONE guarded capture: enquiry, inventory snapshot, requirement version and the requirements object all come from the
        # same instant under the guard, so the computation's basis is immutable and identical to the version it is stored against
        with retention_guard(c, "homebot version ensure"):
            try:
                e = _capture(c, jb["enquiry_id"])
            except KeyError:
                e = None
            if e is None or e["status"] not in OPEN_STATUSES:
                ver = req = None
            else:
                inv = inventory_rows(c)
                inv_hash = inventory_hash(inv)
                ver = _ensure_version(c, e, jb["scenario"], created_by="system", note=None, now=ts)
                req = _requirements(c, e, jb["scenario"]) if ver else None
                exception_capture = exit_exceptions.capture_for_homebot(c, e["id"], jb["scenario"])
        if e is None or e["status"] not in OPEN_STATUSES:
            return _finish(c, jb, token, ts, outcome="cancelled")
        if ver is None:
            return _finish(c, jb, token, ts, outcome="cancelled")   # final size without an applicable planning budget: nothing to run
        assert _hash(req.as_dict()) == ver["basis_hash"]   # the computation runs on exactly the stored version's basis
        from . import routing
        if estimates is None:
            estimates = routing.configured_estimates(req, inv)
        route_adapter = estimates if isinstance(estimates, routing.Estimates) else None
        # idempotency (amendment 3): the exact evaluation basis of THIS claim, not the requirement version alone
        basis = {"requirement_version": ver["id"], "requirement_hash": ver["basis_hash"], "inventory": inv_hash, "today": today.isoformat(),
                 "algorithm": ALGORITHM_VERSION, "estimates": route_adapter.evidence() if route_adapter else ("injected" if estimates else "none")}
        if exception_capture:
            basis["exception_authority"] = _hash(exception_capture)
        basis_hash = _hash(basis)
        prior = row(c.execute("SELECT id FROM homebot_shortlists WHERE job_id=? AND generation=? AND basis_hash=? AND status IN (?,?)",
                              (jb["id"], jb["claimed_generation"], basis_hash, COMPLETE, UNRESOLVED_PARTIAL)))
        if prior:
            return _finish(c, jb, token, ts, outcome="already_stored", shortlist_id=prior["id"])
        exact = {int(a["id"]): exit_exceptions.offers_from_capture(exception_capture, int(a["id"])) for a in inv}
        kwargs = {"exception_offers_by_apartment": exact} if exception_capture else {}
        sl = hm.build_shortlist(req, inv, today, estimates,
                                progress=_progress(c, jb, token, clock, every_sec=HEARTBEAT_SEC if heartbeat_sec is None else heartbeat_sec),
                                **kwargs)
        if route_adapter:
            sl.route_evidence = route_adapter.evidence()
            if routing.stale_evidence(sl.route_evidence):
                return _fail(c, jb, token, ts, "routing_stale")
        return _store(c, jb, token, ts, e, ver, inv_hash, basis_hash, today, sl, inv=inv, exception_capture=exception_capture)
    except ClaimLost:
        log.warning("homebot shortlist job %s: claim lost during computation; abandoned without writing", job_id)
        return {"job_id": jb["id"], "outcome": "lost_claim"}
    except Exception as ex:   # noqa: BLE001 — categorised, never re-raised to the worker loop
        kind = _error_kind(ex)
        log.warning("homebot shortlist job %s failed kind=%s", job_id, kind)
        return _fail(c, jb, token, ts, kind)


def _store(c, jb: dict, token: str, ts: str, e: dict, ver: dict, inv_hash: str, basis_hash: str, today: date, sl: hm.Shortlist,
           inv: Optional[list] = None, *, exception_capture=None) -> dict:
    result = sl.as_dict()
    if getattr(sl, "route_evidence", None):
        result["route_evidence"] = sl.route_evidence
    if exception_capture:
        # Only a digest is persisted here. Staff names and agreement prose are not AI inputs.
        result["exception_authority_hash"] = _hash(exception_capture)
    # GP14 evidence (root rejection-basis probe, 9 Nov): the exact inventory this shortlist was computed from travels with the
    # result, so a later decline is measured against the offer the customer saw, never against the live table at recording time.
    # Only a snapshot that hashes to the row's inventory_hash is stored (anything else would not be evidence).
    if inv is not None and inventory_hash(inv) == inv_hash:
        result["inventory_snapshot"] = [{k: r.get(k) for k in INVENTORY_COLUMNS} for r in inv]
    summary = sl.summary()
    status = UNRESOLVED_PARTIAL if sl.unresolved else COMPLETE
    listed_ids = [x.apartment_id for x in sl.listed]
    try:
        return _store_guarded(c, jb, token, ts, e, ver, inv_hash, basis_hash, today, sl, result, summary, status, listed_ids)
    except ClaimLost:
        # raised inside the guard, so the guard has already rolled back: nothing of this write exists
        return {"job_id": jb["id"], "outcome": "lost_claim"}


def _store_guarded(c, jb, token, ts, e, ver, inv_hash, basis_hash, today, sl, result, summary, status, listed_ids) -> dict:
    with retention_guard(c, "homebot shortlist write"):
        from . import routing
        if routing.stale_evidence(result.get("route_evidence")):
            raise ClaimLost("routing evidence expired before persistence")
        _fence(c, jb, token, ts)   # first statement: owner + generation check bound to this transaction (row lock on PostgreSQL)
        now_inv_hash = inventory_hash(inventory_rows(c))
        cur_ver = _current_version(c, e["id"], jb["scenario"])
        enquiry_now = row(c.execute("SELECT status, is_test FROM homebot_enquiries WHERE id=?", (e["id"],)))
        if enquiry_now is None or enquiry_now["status"] not in OPEN_STATUSES:
            # deleted, or Leased/Closed while we computed: nothing is published and no alert is raised (the job is cancelled;
            # a later reopening creates a new job through the usual triggers)
            _fenced(c, "UPDATE homebot_jobs SET status=?, owner=NULL, locked_at=NULL, updated_at=?", (CANCELLED, ts), jb, token)
            return {"job_id": jb["id"], "outcome": "enquiry_gone" if enquiry_now is None else "enquiry_closed"}
        # stale when the inventory moved, the version moved, or the requirements that apply NOW (enquiry + declines + applicable
        # planning) no longer hash to the computed version — e.g. a planning confirmation invalidated during the computation,
        # which leaves no new version behind (root basis-freshness probe case 6)
        stale = now_inv_hash != inv_hash or (cur_ver or {}).get("id") != ver["id"] or _actual_basis_hash(c, _capture(c, e["id"]), jb["scenario"]) != ver["basis_hash"]
        from . import exit_exceptions
        authority_now = exit_exceptions.capture_for_homebot(c, e["id"], jb["scenario"])
        stale = stale or result.get("exception_authority_hash") != (_hash(authority_now) if authority_now else None)
        c.execute("INSERT INTO homebot_shortlists(enquiry_id, scenario, job_id, generation, requirement_version_id, status, review_state, today, "
                  "inventory_hash, basis_hash, algorithm_version, result_json, summary_json, listed_ids, fourth_option_id, unresolved_ids, is_test, "
                  "is_current, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                  (e["id"], jb["scenario"], jb["id"], jb["claimed_generation"], ver["id"], SUPERSEDED if stale else status, UNREVIEWED, today.isoformat(),
                   inv_hash, basis_hash, ALGORITHM_VERSION, j(result), j(summary), j(listed_ids), sl.fourth_option.apartment_id if sl.fourth_option else None,
                   j([x.apartment_id for x in sl.unresolved]), int(enquiry_now["is_test"] or 0), ts))
        sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        if stale:
            # the basis moved while we computed: keep the row as history, never current; run again at the newest generation
            _fenced(c, "UPDATE homebot_jobs SET status=?, desired_generation=CASE WHEN desired_generation>claimed_generation THEN desired_generation ELSE claimed_generation+1 END, "
                       "owner=NULL, locked_at=NULL, attempts=0, next_attempt_at=?, updated_at=?", (QUEUED, ts, ts), jb, token)
            audit(c, "system", "homebot.shortlist_superseded", "homebot_enquiry", e["id"], j({"shortlist_id": sid, "scenario": jb["scenario"]}))
            return {"job_id": jb["id"], "outcome": "superseded", "shortlist_id": sid}
        prev = row(c.execute("SELECT id, listed_ids, summary_json, result_json FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (e["id"], jb["scenario"])))
        c.execute("UPDATE homebot_shortlists SET is_current=0 WHERE enquiry_id=? AND scenario=? AND is_current=1", (e["id"], jb["scenario"]))
        c.execute("UPDATE homebot_shortlists SET is_current=1 WHERE id=?", (sid,))
        is_test = bool(enquiry_now["is_test"])
        changed = prev is None or _offer_signature(pj(prev["result_json"]) or {}) != _offer_signature(result)
        if prev is None:
            alerts.raise_attention(c, EV_READY, e["id"], is_test=is_test, revision=sid, in_transaction=True)
        elif changed:
            alerts.raise_attention(c, EV_CHANGED, e["id"], is_test=is_test, revision=sid, in_transaction=True)
        if sl.notes_review or sl.unresolved or any(hm.BUNK_COMPARISON_UNAVAILABLE in x.confirmation_reasons or hm.BUNK_CHEAPER_OTHER_DATES in x.confirmation_reasons
                                                    for x in sl.listed):
            alerts.raise_attention(c, EV_REVIEW, e["id"], is_test=is_test, revision=sid, in_transaction=True)
        _finish_sql(c, jb, token, ts)
        audit(c, "system", "homebot.shortlist", "homebot_enquiry", e["id"],
              j({"shortlist_id": sid, "scenario": jb["scenario"], "status": status, "listed": len(listed_ids), "changed": changed}))
    # the grounded review (lead's H7/H14 queue) is asked for AFTER the shortlist is durably stored, in its own guarded step: a
    # failing queue can neither undo the shortlist nor leave a half-written enqueue, and the worker retries it (durably) below
    enqueue_review(c, sid, ts)
    kick_if_requeued(c, jb["id"])
    return {"job_id": jb["id"], "outcome": status, "shortlist_id": sid, "changed": changed}


REVIEW_ENQUEUE_MAX = MAX_ATTEMPTS


def _review_hook():
    return getattr(__import__("server.ai_queue", fromlist=["ai_queue"]), "enqueue_homebot_review", None)


def enqueue_review(c, sid: int, ts: str) -> dict:
    """Ask the lead's review queue for shortlist `sid` once, as ONE guarded step: hook + the shortlist's review_enqueued_at mark
    commit together or not at all (no partial enqueue writes). Without the hook (not landed yet) nothing happens and the mark
    stays empty, so the sweep picks the shortlist up when the hook appears. After REVIEW_ENQUEUE_MAX failed attempts automatic
    retries stop, the options stay HELD (never ready without the AI review, T6/H7) and the staff are alerted
    (homebot_review_unavailable) to retry the AI review explicitly. Never raises."""
    hook = _review_hook()
    if hook is None:
        return {"shortlist_id": sid, "outcome": "no_hook"}
    try:
        with retention_guard(c, "homebot review enqueue"):
            sl = row(c.execute("SELECT id, enquiry_id, is_test, is_current, review_enqueued_at, review_enqueue_attempts FROM homebot_shortlists WHERE id=?", (sid,)))
            if sl is None or sl["review_enqueued_at"] or not sl["is_current"]:
                return {"shortlist_id": sid, "outcome": "nothing_to_do"}
            attempts = int(sl["review_enqueue_attempts"] or 0) + 1
            c.execute("UPDATE homebot_shortlists SET review_enqueue_attempts=? WHERE id=?", (attempts, sid))
            try:
                hook(c, sid, commit=False)
            except Exception as ex:   # noqa: BLE001 — categorised; the step below rolls the hook's partial writes back
                kind = _error_kind(ex)
                log.warning("homebot review enqueue failed shortlist=%s attempt=%s kind=%s", sid, attempts, kind)
                raise _ReviewEnqueueFailed(attempts, kind)
            c.execute("UPDATE homebot_shortlists SET review_enqueued_at=? WHERE id=?", (ts, sid))
            return {"shortlist_id": sid, "outcome": "enqueued", "attempt": attempts}
    except _ReviewEnqueueFailed as f:
        # the guard above rolled back the hook's writes AND the attempt counter; record the attempt alone (bounded write)
        try:
            with transaction(c):
                c.execute("UPDATE homebot_shortlists SET review_enqueue_attempts=? WHERE id=? AND review_enqueued_at IS NULL", (f.attempts, sid))
                if f.attempts >= REVIEW_ENQUEUE_MAX:
                    sl = row(c.execute("SELECT enquiry_id, is_test FROM homebot_shortlists WHERE id=?", (sid,)))
                    if sl:
                        alerts.raise_attention(c, EV_REVIEW_UNAVAILABLE, sl["enquiry_id"], is_test=bool(sl["is_test"]), revision=sid, in_transaction=True)
        except Exception as ex:   # noqa: BLE001
            log.warning("homebot review enqueue bookkeeping failed kind=%s", _error_kind(ex))
        return {"shortlist_id": sid, "outcome": "enqueue_failed", "attempt": f.attempts, "error_kind": f.kind, "final": f.attempts >= REVIEW_ENQUEUE_MAX}
    except Exception as ex:   # noqa: BLE001
        log.warning("homebot review enqueue step failed kind=%s", _error_kind(ex))
        return {"shortlist_id": sid, "outcome": "enqueue_failed", "error_kind": _error_kind(ex)}


class _ReviewEnqueueFailed(Exception):
    def __init__(self, attempts: int, kind: str):
        super().__init__(kind)
        self.attempts, self.kind = attempts, kind


def sweep_inventory_drift(c, ts: str, *, limit: int = 50) -> list[tuple[int, str]]:
    """inc3 (HB-R5 consequence, 11 Nov 2026): a CURRENT shortlist of an open enquiry whose stored inventory_hash no longer equals the
    hash of the live inventory (read-side `_stale` shows it as 'Out of date …' with the ACTUAL job state: 'a fresh run is needed'
    until a job row is queued / running, see _run_state_phrase) gets that run queued
    here when no trigger did - e.g. the one-off change of INVENTORY_COLUMNS (city) at deploy, or an inventory edit made on
    another connection. Idempotent: a job already queued / running / failed is left alone (the failed job is staff's to retry);
    the fresh run stores a snapshot in today's shape, so the drift clears on success. Bounded per pass; no provider call."""
    if not enabled():
        return []
    live = inventory_hash(inventory_rows(c))
    out: list[tuple[int, str]] = []
    # ineligible job states are filtered BEFORE the ORDER/LIMIT (root: a page of skipped rows must never starve later eligible
    # rows); the in-transaction recheck below guards against a job claimed between the read and the write
    cand = rows(c.execute("SELECT s.id, s.enquiry_id, s.scenario, s.inventory_hash FROM homebot_shortlists s JOIN homebot_enquiries e ON e.id=s.enquiry_id "
                          "LEFT JOIN homebot_jobs jb ON jb.enquiry_id=s.enquiry_id AND jb.scenario=s.scenario "
                          "WHERE s.is_current=1 AND s.inventory_hash IS NOT NULL AND s.inventory_hash<>? AND e.status IN (" + ",".join("?" * len(OPEN_STATUSES)) + ") "
                          "AND (jb.id IS NULL OR jb.status NOT IN (?,?,?)) ORDER BY s.id LIMIT ?", (live, *OPEN_STATUSES, QUEUED, RUNNING, FAILED, int(limit))))
    if not cand:
        return out
    with retention_guard(c, "homebot inventory drift sweep"):
        for sl in cand:
            jb = row(c.execute("SELECT status FROM homebot_jobs WHERE enquiry_id=? AND scenario=?", (sl["enquiry_id"], sl["scenario"])))
            if jb and jb["status"] in (QUEUED, RUNNING, FAILED):
                continue   # claimed / failed since the read: left alone
            _ensure_job(c, sl["enquiry_id"], sl["scenario"], "inventory_drift", ts)
            out.append((int(sl["enquiry_id"]), sl["scenario"]))
    return out


def sweep_review_enqueue(c, ts: str, *, limit: int = 50) -> list[dict]:
    """Durable retry: every current, computed shortlist not yet handed to the review queue (hook missing earlier, or failed
    fewer than REVIEW_ENQUEUE_MAX times) is tried again. Called by run_pending; a no-op while the hook is absent."""
    if _review_hook() is None:
        return []
    cand = rows(c.execute("SELECT id, created_at, COALESCE(review_enqueue_attempts,0) AS attempts FROM homebot_shortlists WHERE is_current=1 "
                          "AND status IN (?,?) AND review_enqueued_at IS NULL AND COALESCE(review_enqueue_attempts,0)<? ORDER BY id LIMIT ?",
                          (COMPLETE, UNRESOLVED_PARTIAL, REVIEW_ENQUEUE_MAX, int(limit))))
    now_dt = _now_dt(ts)
    due = [r for r in cand if _review_retry_due(r["created_at"], int(r["attempts"]), now_dt)]
    return [enqueue_review(c, r["id"], ts) for r in due]


def _review_retry_due(created_at: str, attempts: int, now_dt: datetime) -> bool:
    """Backed-off retry schedule measured from the shortlist's own creation (no extra columns): attempt n+1 is due once
    BACKOFF_MIN[n-1] minutes have passed since the shortlist was stored; a shortlist never tried (hook absent) is due at once."""
    if attempts <= 0:
        return True
    wait = BACKOFF_MIN[min(attempts, len(BACKOFF_MIN)) - 1]
    return _now_dt(created_at) + timedelta(minutes=wait) <= now_dt


def _offer_signature(result: dict) -> list:
    out = []
    for key in ("listed", "alternatives"):
        for x in result.get(key) or []:
            o = x.get("offer") or {}
            out.append((key, x.get("apartment_id"), x.get("class"), o.get("start"), o.get("end"), o.get("total_weekly_rent")))
    return out


def _finish_sql(c, jb: dict, token: str, ts: str) -> None:
    """Job bookkeeping at a successful final write (inside the fenced transaction): done, or queued again if a trigger raised
    the desired generation while we worked."""
    _fenced(c, "UPDATE homebot_jobs SET status=CASE WHEN desired_generation>claimed_generation THEN ? ELSE ? END, owner=NULL, locked_at=NULL, "
               "attempts=0, next_attempt_at=?, updated_at=?", (QUEUED, DONE, ts, ts), jb, token)


def _finish(c, jb: dict, token: str, ts: str, *, outcome: str, shortlist_id: Optional[int] = None) -> dict:
    try:
        with transaction(c):
            _fence(c, jb, token, ts)
            if outcome == "cancelled":
                _fenced(c, "UPDATE homebot_jobs SET status=?, owner=NULL, locked_at=NULL, updated_at=?", (CANCELLED, ts), jb, token)
            else:
                _finish_sql(c, jb, token, ts)
    except ClaimLost:
        return {"job_id": jb["id"], "outcome": "lost_claim"}
    return {"job_id": jb["id"], "outcome": outcome, "shortlist_id": shortlist_id}


def _fail(c, jb: dict, token: str, ts: str, kind: str) -> dict:
    kind = kind if kind in ERROR_SENTENCES else "unknown"
    try:
        with transaction(c):
            _fence(c, jb, token, ts)
            r = row(c.execute("SELECT attempts, enquiry_id FROM homebot_jobs WHERE id=?", (jb["id"],)))
            attempts = int(r["attempts"] or 0)
            final = attempts >= MAX_ATTEMPTS
            nxt = _iso(_now_dt(ts) + timedelta(minutes=BACKOFF_MIN[min(attempts, len(BACKOFF_MIN)) - 1]))
            _fenced(c, "UPDATE homebot_jobs SET status=?, owner=NULL, locked_at=NULL, error_kind=?, next_attempt_at=?, updated_at=?",
                    (FAILED if final else QUEUED, kind, None if final else nxt, ts), jb, token)
            if final:
                en = row(c.execute("SELECT is_test FROM homebot_enquiries WHERE id=?", (r["enquiry_id"],)))
                alerts.raise_attention(c, EV_FAILED, r["enquiry_id"], is_test=bool(en and en["is_test"]), revision=jb["claimed_generation"], in_transaction=True)
            audit(c, "system", "homebot.shortlist_failed", "homebot_enquiry", r["enquiry_id"], j({"kind": kind, "attempt": attempts, "final": final}))
    except ClaimLost:
        return {"job_id": jb["id"], "outcome": "lost_claim"}
    except Exception as ex:   # noqa: BLE001
        log.warning("homebot job failure bookkeeping failed kind=%s", _error_kind(ex))
        return {"job_id": jb["id"], "outcome": "failed", "error_kind": kind, "recorded": False}
    return {"job_id": jb["id"], "outcome": "failed", "error_kind": kind, "final": final}


def run_pending(c, *, today: Optional[date] = None, now: Optional[str] = None, limit: int = 50, estimates=None, clock=None,
                heartbeat_sec: Optional[float] = None) -> list[dict]:
    """Run every due job once (queued and due, or running with a stale claim). Used by the worker and by tests."""
    if not enabled():
        return []
    ts = now or (clock() if clock else now_iso())
    try:
        sweep_inventory_drift(c, ts)   # HB-R5 (inc3): a current shortlist computed over another inventory snapshot shape/content is recomputed, not stranded
    except Exception as ex:   # noqa: BLE001
        log.warning("homebot inventory drift sweep failed kind=%s", _error_kind(ex))
    stale = _iso(_now_dt(ts) - timedelta(minutes=STALE_CLAIM_MIN))
    due = rows(c.execute("SELECT id FROM homebot_jobs WHERE (status=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)) OR (status=? AND locked_at<?) "
                         "ORDER BY updated_at, id LIMIT ?", (QUEUED, ts, RUNNING, stale, int(limit))))
    out = [run_job(c, r["id"], today=today, now=ts, estimates=estimates, clock=clock, heartbeat_sec=heartbeat_sec) for r in due]
    try:
        sweep_review_enqueue(c, ts)   # durable retry of the AI-review hand-over (bounded; a no-op while the hook is absent)
    except Exception as ex:   # noqa: BLE001
        log.warning("homebot review sweep failed kind=%s", _error_kind(ex))
    return out


def kick() -> None:
    _wake.set()


def kick_if_requeued(c, job_id: int) -> None:
    r = row(c.execute("SELECT status FROM homebot_jobs WHERE id=?", (job_id,)))
    if r and r["status"] == QUEUED:
        kick()


def start_worker(c) -> Optional[threading.Thread]:
    """Background worker (one per process), only while the switch is on. Wakes on kick() and every 60 s; never raises."""
    global _worker
    if not enabled() or (_worker and _worker.is_alive()):
        return None

    def _loop():
        while True:
            _wake.wait(60)
            _wake.clear()
            try:
                run_pending(c)
            except Exception as ex:   # noqa: BLE001
                log.warning("homebot worker pass failed kind=%s", _error_kind(ex))
            time.sleep(0.2)

    _worker = threading.Thread(target=_loop, daemon=True, name="mmai-homebot-shortlist")
    _worker.start()
    return _worker


# --------------------------------------------------------------------------------------------------- staff read side
def job_state(c, eid: int, scenario: str) -> dict:
    jb = row(c.execute("SELECT status, reason, desired_generation, claimed_generation, attempts, next_attempt_at, error_kind, updated_at FROM homebot_jobs "
                       "WHERE enquiry_id=? AND scenario=?", (eid, scenario)))
    if not jb:
        return {"status": "none", "plain": "No options run has been requested yet."}
    plain = {QUEUED: "Options are being prepared.", RUNNING: "Options are being prepared.", DONE: "Options are up to date.",
             FAILED: ERROR_SENTENCES.get(jb["error_kind"] or "unknown", ERROR_SENTENCES["unknown"]).replace("; the run is retried automatically", "; retries have stopped — use Run again"),
             CANCELLED: "No run is needed for this enquiry at the moment."}[jb["status"]]
    if jb["status"] == QUEUED and jb["error_kind"]:
        plain = ERROR_SENTENCES.get(jb["error_kind"], ERROR_SENTENCES["unknown"])
    return dict(jb, plain=plain)


REVIEW_HELD = "AI review did not complete; options remain held. Retry the AI review (Retry AI review) — options are not ready without it."
REVIEW_PENDING = "Waiting for the AI review; options are held until it completes."


def _review_state(c, sl: dict) -> dict:
    """What the grounded AI review (lead's H7/H14 queue) says about this shortlist, plus the hand-over state kept here. Options are
    never 'ready' from the rules check alone (T6/H7): without a completed AI review they are held."""
    sid = sl["id"]
    try:
        from . import ai_queue
        fn = getattr(ai_queue, "homebot_review_state", None)
        if fn is not None:
            r = fn(c, sid)
            if isinstance(r, dict) and r.get("reviewed"):
                return {"state": "reviewed", "detail": r["reviewed"]}
    except Exception as ex:   # noqa: BLE001
        log.warning("homebot review state unavailable kind=%s", _error_kind(ex))
    attempts = int(sl.get("review_enqueue_attempts") or 0)
    if sl.get("review_enqueued_at"):
        return {"state": "queued", "plain": REVIEW_PENDING, "queued_at": sl["review_enqueued_at"]}
    if attempts >= REVIEW_ENQUEUE_MAX:
        return {"state": "held", "plain": REVIEW_HELD, "attempts": attempts, "retry": True}
    if attempts:
        return {"state": "retrying", "plain": "The AI review could not be queued yet; it is retried automatically. Options are held until it completes.", "attempts": attempts}
    return {"state": UNREVIEWED, "plain": "Rules-based check only; the AI review has not run yet, so these options are held."}


def retry_review(c, eid: int, scenario: str, staff: str, *, now: Optional[str] = None) -> dict:
    """Explicit staff retry of the AI review hand-over for the current shortlist: resets the bounded attempt counter (the worker
    sweep then retries) and tries once right away. Never marks anything ready."""
    _require_on()
    homebot.get(c, eid)
    ts = now or now_iso()
    sl = row(c.execute("SELECT id, review_enqueued_at FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, scenario)))
    if not sl:
        raise ValidationError({"review": "There are no current options to review yet."})
    if sl["review_enqueued_at"]:
        return {"shortlist_id": sl["id"], "outcome": "already_queued"}
    with retention_guard(c, "homebot review retry"):
        c.execute("UPDATE homebot_shortlists SET review_enqueue_attempts=0 WHERE id=?", (sl["id"],))
        audit(c, staff, "homebot.review_retry", "homebot_enquiry", eid, j({"shortlist_id": sl["id"], "scenario": scenario}))
    out = enqueue_review(c, sl["id"], ts)
    kick()
    return out


def freshness(c, sl: dict) -> Optional[str]:
    """Public name for the read-side basis check (lead's H7/H14 review module binds to this, not to `_stale`): None when the
    current shortlist row still matches the basis that applies now, otherwise the plain 'Out of date: …' sentence."""
    return _stale(c, sl)


def _stale(c, sl: dict) -> Optional[str]:
    """A current shortlist whose basis has moved since it was stored (requirements/planning/rejections re-versioned, or the
    apartment list changed — on another connection or after the run) is shown as out of date, never as the current options.
    The usual triggers queue the fresh run; this is the read-side treatment until it lands. The closing phrase states the ACTUAL
    job state (root, inc3): 'queued' / 'in progress' only when the job row says so, otherwise 'needed' (switch off, no job, done
    without a trigger, or failed - a failed run is named and is staff's to request again)."""
    ver = _current_version(c, sl["enquiry_id"], sl["scenario"])
    if (ver or {}).get("id") != sl["requirement_version_id"]:
        return "Out of date: the party's requirements, planning budget or declines changed after these options were prepared; " + _run_state_phrase(c, sl)
    try:
        e = homebot.get_record(c, sl["enquiry_id"])   # no matching note: freshness must not re-enter the review state (lead, 9 Nov)
    except KeyError:
        return "Out of date: the enquiry is no longer on file."
    actual = _actual_basis_hash(c, e, sl["scenario"])
    if actual is None:
        return "Out of date: the planning budget these options were prepared under no longer applies (the party's size or stated budget changed); confirm it again."
    if actual != (ver or {}).get("basis_hash"):
        return "Out of date: the party's requirements, planning budget or declines changed after these options were prepared; " + _run_state_phrase(c, sl)
    if inventory_hash(inventory_rows(c)) != sl["inventory_hash"]:
        return "Out of date: the apartment list changed after these options were prepared; " + _run_state_phrase(c, sl)
    from . import exit_exceptions
    authority_now = exit_exceptions.capture_for_homebot(c, e["id"], sl["scenario"])
    stored = pj(sl["result_json"]) or {}
    if sl.get("algorithm_version") != ALGORITHM_VERSION:
        return "Out of date: the matching software changed; " + _run_state_phrase(c, sl)
    from . import routing
    route_stale = routing.stale_evidence(stored.get("route_evidence"))
    if route_stale:
        return "Out of date: " + route_stale + " " + _run_state_phrase(c, sl)
    if stored.get("exception_authority_hash") != (_hash(authority_now) if authority_now else None):
        return "Out of date: the booking exception authority changed after these options were prepared; " + _run_state_phrase(c, sl)
    return None


RUN_QUEUED, RUN_IN_PROGRESS, RUN_NEEDED, RUN_FAILED = ("a fresh run is queued.", "a fresh run is in progress.", "a fresh run is needed.",
                                                       "a fresh run is needed (the last run failed; staff can request it again).")


def _run_state_phrase(c, sl: dict) -> str:
    """Truthful closing phrase for an out-of-date shortlist from the job row itself; never a promise the queue has not made."""
    if not enabled():
        return RUN_NEEDED
    jb = row(c.execute("SELECT status FROM homebot_jobs WHERE enquiry_id=? AND scenario=?", (sl["enquiry_id"], sl["scenario"])))
    if jb and jb["status"] == QUEUED:
        return RUN_QUEUED
    if jb and jb["status"] == RUNNING:
        return RUN_IN_PROGRESS
    if jb and jb["status"] == FAILED:
        return RUN_FAILED
    return RUN_NEEDED


def current_shortlist(c, eid: int, scenario: str) -> Optional[dict]:
    sl = row(c.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, scenario)))
    return _shortlist_out(c, sl) if sl else None


def _shortlist_out(c, sl: dict, *, full: bool = True) -> dict:
    out = {k: sl[k] for k in ("id", "scenario", "generation", "requirement_version_id", "status", "today", "algorithm_version", "created_at", "is_current")}
    out["scenario_label"] = SCENARIO_LABEL.get(sl["scenario"], sl["scenario"])
    out["summary"] = pj(sl["summary_json"]) or {}
    out["status_plain"] = {COMPLETE: "Rules check complete (not yet reviewed).",
                           UNRESOLVED_PARTIAL: "Rules check complete for most apartments; some date ranges were too wide to check automatically and need a manual check (not yet reviewed).",
                           SUPERSEDED: "Out of date: the apartment list or the party's requirements changed while this run was being prepared; a newer run replaces it.",
                           RUN_FAILED: ERROR_SENTENCES["unknown"]}.get(sl["status"], sl["status"])
    out["review"] = _review_state(c, sl) if sl["is_current"] else {"state": "n/a"}
    out["stale"] = _stale(c, sl) if sl["is_current"] and full else None
    out["ready"] = False   # options are never ready from the rules check alone; the AI review (lead) decides readiness
    if full:
        res = pj(sl["result_json"]) or {}
        out["route_evidence"] = res.get("route_evidence")
        out["listed"] = [_candidate_out(x) for x in res.get("listed", [])]
        out["alternatives"] = [_candidate_out(x) for x in res.get("alternatives", [])]
        out["fourth_option"] = _candidate_out(res["fourth_option"]) if res.get("fourth_option") else None
        out["unresolved"] = [_candidate_out(x) for x in res.get("unresolved", [])]
        shown = {x["apartment_id"] for key in ("listed", "alternatives", "unresolved") for x in res.get(key, [])} | ({res["fourth_option"]["apartment_id"]} if res.get("fourth_option") else set())
        out["why_not"] = [_candidate_out(x, brief=True) for x in res.get("candidates", []) if x["apartment_id"] not in shown]
    return out


def _candidate_out(x: dict, *, brief: bool = False) -> dict:
    words = lambda codes: [hm.PLAIN.get(k, k) for k in codes]
    out = {"apartment_id": x["apartment_id"], "address": x["address"], "suburb": x["suburb"], "type_code": x["type_code"], "class": x["class"],
           "class_plain": {hm.SUITABLE: "Meets every checked requirement", hm.NEEDS_CONFIRMATION: "Needs confirmation", hm.ALTERNATIVE_AREA: "Alternative: different area",
                           hm.ALTERNATIVE_BATHROOM: "Alternative: one bathroom", hm.EXCLUDED: "Not suitable"}.get(x["class"], x["class"]),
           "reasons": words(x.get("exclusion_reasons", [])) + words(x.get("confirmation_reasons", [])), "notes": words(x.get("flags", []))}
    if not brief:
        out.update(offer=x.get("offer"), cheapest_offer=x.get("cheapest_offer"), travel=x.get("travel"), bedding=x.get("bedding"),
                   bunk_comparison=x.get("bunk_comparison"), features=x.get("features"), pareto_rank=x.get("pareto_rank"))
    return out


def history(c, eid: int) -> list[dict]:
    return [_shortlist_out(c, sl, full=False) for sl in rows(c.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? ORDER BY id DESC LIMIT 50", (eid,)))]


def overview(c, eid: int) -> dict:
    """Everything the staff Options panel shows for one enquiry. Plain words only; reason codes are translated."""
    e = homebot.get(c, eid)
    if not enabled():
        return {"enabled": False, "plain": SWITCHED_OFF, "enquiry_id": eid}
    scen = {}
    for sc in SCENARIOS:
        if sc == hm.SCENARIO_FINAL_SIZE and e.get("party_mode") != "more_declined":
            continue
        scen[sc] = {"label": SCENARIO_LABEL[sc], "job": job_state(c, eid, sc), "current": current_shortlist(c, eid, sc)}
    return {"enabled": True, "enquiry_id": eid, "scenarios": scen, "planning": planning_state(c, e), "versions": versions(c, eid),
            "rejections": [rejection(c, r["id"]) for r in rows(c.execute("SELECT id FROM homebot_rejections WHERE enquiry_id=? ORDER BY id", (eid,)))],
            "history": history(c, eid), "algorithm_version": ALGORITHM_VERSION, "live_travel": live_travel_state(),
            "travel_note": "Journey times are estimates, not guarantees. Check each person's journey and the evidence date. Missing or expired journeys need confirmation; an AI review cannot confirm them."}


# ------------------------------------------------------------------------------------------------- live travel check
# Journey times for the options a staff member is looking at, worked out on request and shown once. The stored options, their
# history, the saved AI review and the decline records are untouched: nothing from the journey provider is written to the
# database, kept between requests or logged. Only a count of provider requests is recorded, to hold the monthly allowance.
# When the separate AI travel reply is switched on (homebot_travel_reply), the journey results of this one check are also
# given to Claude for a short assessment that is returned with this reply and dropped; it is never the saved AI review.
TRAVEL_CHECK_ACTION = "homebot.travel_check"
TRAVEL_NOT_SAVED = ("Journey times are checked live and are not saved: they are shown on this page only, are not part of the stored "
                    "options or the AI review, and are checked again each time.")
_travel_lock = threading.Lock()   # one travel check at a time in this process, so the request allowance cannot be overrun


def _travel_requests_used(c, prefix: str) -> int:
    """Provider requests already made by travel checks recorded at a time starting with `prefix` (a UTC day or month)."""
    from . import routing
    used = 0
    for r in rows(c.execute("SELECT detail FROM audit_log WHERE action=? AND at LIKE ?", (TRAVEL_CHECK_ACTION, prefix + "%"))):
        n = (pj(r["detail"]) or {}).get("requests")
        # a record that cannot be read counts as a full check, so a damaged record can only tighten the allowance
        used += n if type(n) is int and n >= 0 else routing.LIVE_LIMITS["MMAI_ROUTING_LIVE_MAX_REQUESTS"][2]
    return used


def live_travel_state(c=None) -> dict:
    """For the Options panel: whether live travel checks can be run, and whether the page should run one when it opens."""
    from . import routing
    from . import homebot_travel_reply
    on = enabled() and routing.live_enabled()
    return {"enabled": on, "auto": on and routing.live_auto(), "plain": TRAVEL_NOT_SAVED if on else routing.LIVE_OFF,
            "ai_reply": on and homebot_travel_reply.enabled()}


def _travel_refusal(state: str, plain: str, **extra) -> dict:
    return {"ok": False, "state": state, "saved": False, "plain": plain, **extra}


def live_travel_check(c, eid: int, scenario: str, staff: str, *, now: Optional[str] = None, post=None) -> dict:
    """Work out the current options again with live journey estimates and return them for display. The stored options must be
    current and not out of date; the same enquiry, apartment list, business date and booking authority are used, so the only
    difference from the stored options is the journey times. Nothing is stored except one count-only audit record."""
    from . import exit_exceptions, homebot_travel_reply, routing
    _require_on()
    if scenario not in SCENARIOS:
        raise ValidationError({"scenario": "Unknown scenario."})
    homebot.get(c, eid)   # 404 before anything else
    if not routing.live_enabled():
        return _travel_refusal("off", routing.LIVE_OFF)
    when = _now_dt(now)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    if not _travel_lock.acquire(timeout=10):
        return _travel_refusal("busy", "Another travel check is running. Please try again in a moment.")
    try:
        # ONE guarded read: the stored options, the enquiry, the apartment list and the requirements all from the same instant
        with retention_guard(c, "homebot travel check read"):
            sl = row(c.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, scenario)))
            if not sl or sl["status"] not in (COMPLETE, UNRESOLVED_PARTIAL):
                return _travel_refusal("no_options", "There are no prepared options to check travel for yet.")
            stale = _stale(c, sl)
            if stale:
                return _travel_refusal("out_of_date", "Travel was not checked because these options are out of date. " + stale.replace("Out of date: ", "", 1),
                                       shortlist_id=sl["id"])
            e = _capture(c, eid)
            if e["status"] not in OPEN_STATUSES:
                return _travel_refusal("closed", "This enquiry is closed, so travel was not checked.", shortlist_id=sl["id"])
            inv = inventory_rows(c)
            req = _requirements(c, e, scenario)
            ver = _current_version(c, eid, scenario)
            exception_capture = exit_exceptions.capture_for_homebot(c, eid, scenario)
            used_day = _travel_requests_used(c, when.strftime("%Y-%m-%d"))
            used_month = _travel_requests_used(c, when.strftime("%Y-%m"))
        if _hash(req.as_dict()) != (ver or {}).get("basis_hash") or inventory_hash(inv) != sl["inventory_hash"]:
            return _travel_refusal("out_of_date", "Travel was not checked because these options are out of date.", shortlist_id=sl["id"])
        allowance = min(routing.live_limit("MMAI_ROUTING_LIVE_MAX_REQUESTS"),
                        routing.live_limit("MMAI_ROUTING_LIVE_DAILY_REQUESTS") - used_day,
                        routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") - used_month)
        if allowance <= 0:
            return _travel_refusal("limit", "The travel-check allowance for " + ("today" if routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") > used_month else "this month")
                                   + " has been used, so travel was not checked. Travel time needs confirmation.", shortlist_id=sl["id"])
        stored = pj(sl["result_json"]) or {}
        # the options staff see first are measured first; apartments already ruled out for another reason are not measured at all
        first = [x["apartment_id"] for key in ("listed", "alternatives", "unresolved") for x in stored.get(key, [])]
        if stored.get("fourth_option"):
            first.insert(len(stored.get("listed", [])), stored["fourth_option"]["apartment_id"])
        order = list(dict.fromkeys(first + [x["apartment_id"] for x in stored.get("candidates", []) if x.get("class") != hm.EXCLUDED]))
        est = routing.LiveEstimates(req, inv, max_requests=allowance, now=when, post=post)
        est.plan(order)   # how many requests this check will make; nothing has been sent yet
        if est.calls:
            # recorded BEFORE the first request leaves, so the allowance is held even if this process stops part-way.
            # Our own planned request counts only: nothing that depends on what the provider answers.
            audit(c, str(staff or "").strip()[:homebot.MAX_NAME] or "staff", TRAVEL_CHECK_ACTION, "homebot_enquiry", eid,
                  j({"scenario": scenario, "shortlist_id": sl["id"], "requests": est.calls, "not_requested": est.skipped}))
        est.prefetch()   # outside any database lock
        check = est.display()
        exact = {int(a["id"]): exit_exceptions.offers_from_capture(exception_capture, int(a["id"])) for a in inv} if exception_capture else None
        res = hm.build_shortlist(req, inv, date.fromisoformat(sl["today"]), est,
                                 **({"exception_offers_by_apartment": exact} if exact else {})).as_dict()
        now_current = row(c.execute("SELECT id FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, scenario)))
        if not now_current or now_current["id"] != sl["id"]:
            return _travel_refusal("replaced", "The options were replaced while travel was being checked. Open the new options and check travel again.",
                                   shortlist_id=sl["id"])
    finally:
        _travel_lock.release()
    shown = {x["apartment_id"] for key in ("listed", "alternatives", "unresolved") for x in res.get(key, [])} | (
        {res["fourth_option"]["apartment_id"]} if res.get("fourth_option") else set())
    # the options in the order the page shows them; the AI's transient travel reply covers these and nothing else
    in_order = res.get("listed", []) + ([res["fourth_option"]] if res.get("fourth_option") else []) + res.get("alternatives", []) + res.get("unresolved", [])
    reply = homebot_travel_reply.travel_reply(in_order)   # outside the lock and any database work; returned once, never stored

    def with_sources(x):
        # the provider's attribution for this option's journeys, to be shown straight after its figures and the AI's note
        return {**_candidate_out(x), "travel_sources": est.sources_for(x["apartment_id"])}
    return {"ok": True, "state": "checked", "saved": False, "plain": TRAVEL_NOT_SAVED, "shortlist_id": sl["id"], "scenario": scenario,
            "scenario_label": SCENARIO_LABEL.get(scenario, scenario), "check": check, "summary": res.get("summary") or {},
            "ai_reply": reply,
            "listed": [with_sources(x) for x in res.get("listed", [])],
            "alternatives": [with_sources(x) for x in res.get("alternatives", [])],
            "fourth_option": with_sources(res["fourth_option"]) if res.get("fourth_option") else None,
            "unresolved": [with_sources(x) for x in res.get("unresolved", [])],
            "why_not": [_candidate_out(x, brief=True) for x in res.get("candidates", []) if x["apartment_id"] not in shown]}


def matching_note(c, eid: int) -> dict:
    """For homebot.get: the honest state of automatic matching for this enquiry."""
    if not enabled():
        return {"automatic": False, "note": "No automatic apartment matching has run for this enquiry. " + SWITCHED_OFF +
                " HomeBot enquiries are never part of MateBot housemate matching."}
    st = job_state(c, eid, hm.SCENARIO_CURRENT)
    cur = row(c.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, hm.SCENARIO_CURRENT)))
    return {"automatic": True, "note": st["plain"], "job_status": st["status"], "current_shortlist_id": cur and cur["id"],
            "shortlist_status": cur and cur["status"], "review_state": _review_state(c, cur)["state"] if cur else None,
            "never_matebot": "HomeBot enquiries are never part of MateBot housemate matching."}


# ---------------------------------------------------------------------------------------------------------- retention
TABLES = ("homebot_reviews", "homebot_rejection_events", "homebot_rejections", "homebot_shortlists", "homebot_jobs", "homebot_requirement_versions", "homebot_planning")


def forget(c, ids: list) -> None:
    """Remove every H2 row for these enquiries (inside the caller's transaction): versions, planning, jobs, shortlists,
    rejections and their events, and the attention alerts that referenced them (amendment 9). Explicit deletes, so it does not
    depend on foreign-key enforcement."""
    for eid in ids:
        for t in TABLES:
            c.execute(f"DELETE FROM {t} WHERE enquiry_id=?", (eid,))
        c.execute("DELETE FROM internal_alert_outbox WHERE record_type='homebot_enquiry' AND record_id=?", (int(eid),))
