"""A13(a) step-free access facts (T3(d) C3, 10 November 2026).

The apartment row carries the CURRENT recorded fact (`apartments.step_free_access`: 1 = completely step-free route from the
street into the apartment confirmed, 0 = recorded as NOT step-free, NULL = unknown / not recorded) with its provenance
(`step_free_source`, `step_free_recorded_by`, `step_free_recorded_at`). Every recording - yes, no, unknown, exclusion - is kept
as a row of `apartment_access_facts` (append-only history; the current row has superseded_at IS NULL) so the source and scope of
Bill's confirmation stay attached to each apartment record (A13(a): "retain the source and scope").

Boundaries (register): a lift is never a step-free fact (apartment_features lift_access is a different field and never read here);
nothing is inferred from an address, a building or another apartment; nothing extends automatically to new stock (the import
only touches ids whose address matches the confirmation file); unknown stays unknown (never treated as no, never as yes); a
shared step-free need is an apartment-suitability requirement, not a housemate incompatibility (notes_facts.verification /
hold_gate decide what the review may do with it; no matching weight, filter or rule changes here). A recorded, changed or revoked
fact is part of the review evidence (matching.evidence_from_rows), so it invalidates every stored review at the apartment and
queues a current one (apply_to_groups); nothing is approved or released by hand.
"""
from __future__ import annotations

import re

from .db import audit, maybe_commit, now_iso, row, rows

FACT = "step_free_access"
VALUES = {"yes": 1, "no": 0, "unknown": None}
LABELS = {1: "Completely step-free route confirmed", 0: "Recorded as not step-free", None: "Not recorded (unknown)"}
MOVED_LABEL = "Not recorded (apartment address changed since the record; confirm again)"
HISTORY_LIMIT = 50
MIRROR_FIELDS = ("step_free_access", "step_free_source", "step_free_recorded_by", "step_free_recorded_at", "step_free_record_id", "step_free_address")


def norm_address(s) -> str:
    s = (s or "").lower()
    s = re.sub(r"\bunit\b|\bapt\b|\bapartment\b|\bflat\b", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


LOCATION_FIELDS = ("address", "suburb", "city")   # the row's location identity a record is bound to (no geocoding/inference)


def location_key(a: dict | None) -> str:
    """Normalised location identity of an apartment row: address | suburb | city. A record is bound to this key, so a row moved to
    another suburb/city with unchanged street text (or re-addressed, or reused) never inherits a fixed-stock confirmation."""
    a = a or {}
    return "|".join(norm_address(a.get(f)) for f in LOCATION_FIELDS)


def effective(a: dict | None) -> dict:
    """The fact that counts for an apartment ROW as read today: {value, source, record_id, bound}. A record is bound to the
    apartment it was made for: when the row's location identity (address, suburb, city - location_key) no longer matches the one
    at the time of the record (row reused, re-addressed or moved to another suburb/city), the value is unknown (None) and `bound` is False - Bill's fixed-stock confirmation never travels with a row id.
    `record_id` is the generation of the current record (also for unknown / revoked records): review evidence carries it, so a
    late review result built on an older generation fails the evidence check even if the boolean value is the same again."""
    a = a or {}
    rid = a.get("step_free_record_id")
    if rid is None:
        return {"value": None, "source": None, "record_id": None, "bound": True}
    bound = (a.get("step_free_address") or "") == location_key(a) and bool(norm_address(a.get("address")))   # step_free_address holds the location key at record time
    v = a.get("step_free_access")
    return {"value": (None if v is None or not bound else int(v)), "source": a.get("step_free_source"), "record_id": int(rid), "bound": bound}


def value_from_text(v) -> int | None:
    """'yes' | 'no' | 'unknown' (case-insensitive; also True/False/None, 1/0) -> 1 | 0 | None. Raises ValueError otherwise."""
    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)) and v in (0, 1):
        return int(v)
    s = str(v).strip().lower()
    if s in VALUES:
        return VALUES[s]
    raise ValueError("step-free access must be yes, no or unknown")


def current(c, apartment_id: int) -> dict:
    """{value, label, source, scope, note, status, recorded_by, recorded_at, record_id, bound} of the apartment's current record
    (value None when unknown or when the record is not bound to the row's present address)."""
    a = row(c.execute("SELECT id, " + ", ".join(LOCATION_FIELDS + MIRROR_FIELDS) + " FROM apartments WHERE id=?", (apartment_id,)))
    eff = effective(a)
    cur = row(c.execute("SELECT * FROM apartment_access_facts WHERE id=?", (eff["record_id"],))) if eff["record_id"] is not None else None
    v = eff["value"]
    return {"value": v, "label": (MOVED_LABEL if not eff["bound"] else LABELS[v]), "source": eff["source"], "recorded_by": (a or {}).get("step_free_recorded_by"),
            "recorded_at": (a or {}).get("step_free_recorded_at"), "scope": (cur or {}).get("scope"), "note": (cur or {}).get("note"),
            "status": (cur or {}).get("status"), "record_id": eff["record_id"], "bound": eff["bound"]}


# --- stock-change impact (reviewer G2 / G4, 10 Nov 2026) ------------------------------------------------------------------------
# A3(a): a lost apartment is deleted outright (no archive, no reconstructable apartment history), so its confirmation rows go with
# it (ON DELETE CASCADE). What stays is an HONEST report + audit line of the confirmations removed (count, source, status - the
# A13(a) batch's provenance is the file itself), and an explicit control that new stock never inherits a fact: the only readable
# fact is the apartments-row mirror bound to the row's own location identity (effective()), which a new row starts without.
REMOVED_AUDIT = "apartment_access_facts_removed"
UNBOUND_AUDIT = "apartment_access_fact_unbound"


def confirmations_at(c, apartment_ids: list[int]) -> list[dict]:
    """Current (not superseded) step-free records at these apartments: [{apartment_id, address, value, status, source}]. Read-only;
    call it BEFORE a delete / replace so the impact can be reported and audited."""
    if not apartment_ids:
        return []
    q = ",".join("?" * len(apartment_ids))
    return rows(c.execute(f"""SELECT f.apartment_id, a.address, f.value, f.status, f.source FROM apartment_access_facts f JOIN apartments a ON a.id=f.apartment_id
                              WHERE f.fact=? AND f.superseded_at IS NULL AND f.apartment_id IN ({q}) ORDER BY f.apartment_id""", [FACT, *apartment_ids]))


def audit_removed(c, removed: list[dict], staff: str | None, cause: str) -> dict:
    """{count, by_source: {source: n}, apartments: [...]} and ONE audit line (plain counts, no raw rows) when anything was removed."""
    out = {"count": len(removed), "by_source": {}, "apartments": [{"apartment_id": r["apartment_id"], "address": r["address"], "value": r["value"], "status": r["status"], "source": r["source"]} for r in removed]}
    for r in removed:
        out["by_source"][r["source"] or "unknown source"] = out["by_source"].get(r["source"] or "unknown source", 0) + 1
    if removed:
        audit(c, staff, REMOVED_AUDIT, "apartments", None,
              f"{len(removed)} step-free confirmation(s) removed with the apartment(s) ({cause}): " + ", ".join(f"{n} from {src}" for src, n in sorted(out["by_source"].items())))
    return out


def bound_snapshot(c) -> dict[int, int]:
    """{apartment_id: record_id} of every apartment whose current record is BOUND to its present location identity. Take it before a
    stock change that may move rows (import by address, staff edit of address/suburb/city)."""
    out = {}
    for a in rows(c.execute("SELECT id, " + ", ".join(LOCATION_FIELDS + MIRROR_FIELDS) + " FROM apartments WHERE step_free_record_id IS NOT NULL")):
        eff = effective(a)
        if eff["record_id"] is not None and eff["bound"]:
            out[int(a["id"])] = eff["record_id"]
    return out


def unbound_since(c, snapshot: dict[int, int], staff: str | None, cause: str) -> list[dict]:
    """Records that were bound at `snapshot` time and are NOT bound now (the row's address/suburb/city changed): reported, never
    silent - [{apartment_id, address, source, value}] - with one audit line. Nothing is re-bound or re-recorded: the fact is unknown
    at the moved row until a fresh confirmation is recorded for it (effective() reads it so already)."""
    if not snapshot:
        return []
    q = ",".join("?" * len(snapshot))
    out = []
    for a in rows(c.execute("SELECT id, " + ", ".join(LOCATION_FIELDS + MIRROR_FIELDS) + f" FROM apartments WHERE id IN ({q})", list(snapshot))):
        eff = effective(a)
        if eff["record_id"] == snapshot.get(int(a["id"])) and not eff["bound"]:
            out.append({"apartment_id": int(a["id"]), "address": a.get("address"), "source": a.get("step_free_source"), "value": None})
    if out:
        audit(c, staff, UNBOUND_AUDIT, "apartments", None,
              f"{len(out)} step-free confirmation(s) no longer apply after a location change ({cause}); the fact is unknown there until confirmed again: apartment(s) "
              + ", ".join(str(x["apartment_id"]) for x in out))
    return out


def history(c, apartment_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    return rows(c.execute("SELECT id, value, status, source, scope, note, recorded_by, recorded_at, superseded_at, address_at_record FROM apartment_access_facts WHERE apartment_id=? AND fact=? ORDER BY id DESC LIMIT ?",
                          (apartment_id, FACT, int(limit))))


def record(c, apartment_id: int, value, *, source: str, staff: str | None, scope: str | None = None, note: str | None = None,
           status: str | None = None, commit: bool = True) -> dict:
    """Record the current step-free fact of ONE apartment with its provenance; the previous current row is superseded, nothing is
    deleted. `value`: yes/no/unknown (see value_from_text). `source` is who confirmed it (e.g. "Bill, 10 November 2026" or the
    staff member recording a site check); `status` is a closed word for the history ('confirmed', 'not_step_free', 'unknown',
    'excluded_from_confirmation'); `note` is free text for staff (never sent to the AI). Returns current(). Raises ValueError on a
    bad value or unknown apartment. Side effects on groups are the caller's (see apply_to_groups): this function only records."""
    v = value_from_text(value)
    a = row(c.execute("SELECT id, " + ", ".join(LOCATION_FIELDS) + " FROM apartments WHERE id=?", (apartment_id,)))
    if not a:
        raise ValueError("unknown apartment")
    loc = location_key(a)
    when = now_iso()
    st = status or {1: "confirmed", 0: "not_step_free", None: "unknown"}[v]
    c.execute("UPDATE apartment_access_facts SET superseded_at=? WHERE apartment_id=? AND fact=? AND superseded_at IS NULL", (when, apartment_id, FACT))
    cur = c.execute("INSERT INTO apartment_access_facts(apartment_id, fact, value, status, source, scope, note, recorded_by, recorded_at, address_at_record) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (apartment_id, FACT, v, st, source, scope, (note or None), staff, when, loc))
    rid = cur.lastrowid if cur.lastrowid else row(c.execute("SELECT MAX(id) AS id FROM apartment_access_facts WHERE apartment_id=? AND fact=?", (apartment_id, FACT)))["id"]
    c.execute("UPDATE apartments SET step_free_access=?, step_free_source=?, step_free_recorded_by=?, step_free_recorded_at=?, step_free_record_id=?, step_free_address=?, updated_at=? WHERE id=?",
              (v, source, staff, when, rid, loc, when, apartment_id))   # source kept for unknown / excluded records too (provenance of the absence)
    audit(c, staff, "apartment_access_fact", "apartment", apartment_id, f"{FACT}={'yes' if v == 1 else 'no' if v == 0 else 'unknown'} ({st}); source: {source}")
    if commit:
        maybe_commit(c)
    return current(c, apartment_id)


def affected_groups(c, apartment_id: int) -> list[dict]:
    """Live groups that OFFER this apartment (current or alternative offer) and carry a stored review evidence snapshot, plus any
    live group held for an access confirmation at it. Read-only, deterministic order."""
    from . import ai_queue
    live_q = ",".join("?" * len(ai_queue.LIVE))
    return rows(c.execute(f"""SELECT DISTINCT g.* FROM groups g LEFT JOIN group_apartments ga ON ga.group_id=g.id
                              WHERE g.status IN ({live_q}) AND (ga.apartment_id=? OR g.apartment_id=?) AND (g.evidence_hash IS NOT NULL OR g.ai_status='held')
                              ORDER BY g.id""", (*ai_queue.LIVE, apartment_id, apartment_id)))


def _members_for_gate(c, g: dict) -> list[dict]:
    from . import matching
    units = []
    for gm in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))):
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (gm["enquiry_id"],)))
        if not e:
            return []
        units.append(matching.unit_from_row(e, rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))))
    return matching.members_for_ai(units)


REQUEUE_AUDIT = "access_fact_rereview_queued"


def apply_to_groups(c, apartment_id: int, staff: str | None) -> dict:
    """After a fact is recorded (or revoked / changed) at ONE apartment: every live group that offers it and whose stored review
    evidence no longer matches today's facts is queued for a proper, CURRENT AI review (ai_queue.enqueue: the stored verdict,
    rating, brief and readiness become history - nothing is re-labelled, re-stamped or released by hand). Groups held for the
    step-free confirmation at this apartment are queued the same way: the next review runs with the recorded fact in its packet
    and the staff hold gate decides again on it. Groups with no stored evidence and nothing held are left alone. No provider call
    here (the queue makes it; MMAI_AI_QUEUE off = nothing automatic). One guarded step per group; a group that changed under us is
    skipped and reported. Access confirmation is authoritative DATA about the apartment, never approval of a review that did not
    see it (root, 10 Nov 2026)."""
    from . import ai_queue, matching
    out = {"checked": 0, "requeued": [], "unchanged": [], "skipped": []}
    if not row(c.execute("SELECT id FROM apartments WHERE id=?", (apartment_id,))):
        return out
    for g in affected_groups(c, apartment_id):
        out["checked"] += 1
        try:
            with ai_queue._step(c, label="access fact apply"):
                live = row(c.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
                if not live or live.get("member_key") != g.get("member_key") or live.get("updated_at") != g.get("updated_at"):
                    out["skipped"].append(int(g["id"]))
                    continue
                # held for the practical-need confirmation AT this apartment: a structural check (closed ranking basis + current offer),
                # not a substring of the staff label (reviewer G5). Such a hold always carries stored evidence (the review that held it),
                # so a record at one of its ALTERNATIVE apartments is caught by the evidence comparison below; a group held for an
                # unread note carries no evidence and is not re-queued here - the staff retry of the note owns that path.
                held_here = (live.get("ai_status") == "held" and live.get("apartment_id") == apartment_id
                             and live.get("ranking_basis") == ai_queue.PRACTICAL_HOLD_BASIS)
                stale = False
                if live.get("evidence_hash"):
                    now_hash, _ = matching.group_evidence(c, live["id"])
                    stale = now_hash != live["evidence_hash"]
                if not (stale or held_here):
                    out["unchanged"].append(int(live["id"]))
                    continue
                if live.get("ai_status") in ("pending", "running") and not live.get("ai_refresh"):
                    out["unchanged"].append(int(live["id"]))   # already awaiting a review that will carry the new fact (late results fail the evidence CAS)
                    continue
                ai_queue.enqueue(c, [int(live["id"])], reason="apartment access fact recorded", commit=False)
                audit(c, staff, REQUEUE_AUDIT, "group", live["id"], f"apartment {apartment_id} step-free record changed: stored review is no longer current; re-review queued")
                out["requeued"].append(int(live["id"]))
        except ai_queue.GUARD_FAILURES:
            out["skipped"].append(int(g["id"]))
    return out
