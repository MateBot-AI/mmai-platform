"""T6 (replaced 26 Oct 2026): the AI assessment queue.

Hourly runs form groups with the RULES ONLY. As soon as a group is newly formed, or its members change (a replacement accepted,
a rejection refilled, a bar replacement), it is queued here with `ai_status='pending'`. The queue assesses the group (compatibility
rating and rationale) and ranks its apartments (GP6(a)) BEFORE the group is shown to reservations as "Ready for reservations".
Unchanged groups keep their existing assessment and are not re-run. GP8(b): after a lease, a group whose members did not change
stays ready and gets a low-priority background *refresh* of its apartment ranking (`ai_refresh=1`).

Work is batched (`MMAI_AI_BATCH` groups per call), parallel (`MMAI_AI_WORKERS` calls at once), retried (`MMAI_AI_ATTEMPTS` per
pass, with a back-off) and robust: a batch that fails leaves its groups `ai_status='failed'` with the error, clearly labelled for
staff, who can retry (`POST /api/ai-queue/retry`). A group marked 'running' when the server restarts is re-queued at start-up.

Group review state (ai_status; staff wording computed in app._group_out):
  pending / running  -> "AI assessment pending"      (reservations do not treat the group as ready)
  ok                 -> review valid and approved; READY ONLY when is_ready() also finds the brief ok and the evidence current
  revise             -> the AI asked for one or more members to be replaced; alternatives are being formed/reviewed (review_loop)
  held               -> held for staff (review 'hold', revision rounds exhausted, no eligible alternative, introduced group)
  stale              -> the facts the assessment rested on changed (rule version, answers, apartments, model): re-queued
  failed             -> "AI assessment failed - retry" (outage, or a review that was malformed / incomplete; never 'ready')

T6 fail-closed (8 Nov 2026): nothing is ever defaulted, clamped or auto-promoted. `is_ready(c, g)` is the ONE place that says a
group is ready for reservations: valid approved review + per-member and group staff briefs (T4(f)) + evidence hash recomputed
from the current rows + rule version + approved model + the hard rules still passing. GP8(b) carve-out: a ready group whose
members and retained apartments are unchanged stays ready while NEW apartments are added and its ranking is refreshed.
"""
from __future__ import annotations

import hashlib
import re
import inspect
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from . import access_facts, ai, rules
from . import db
from .db import audit, j, maybe_commit, now_iso, pj, row, rows

READY_STATUSES = ("Proposed", "Introduced", "Group confirmed")   # 'Awaiting replacement' (GP10) is never ready

AI_BATCH = int(os.environ.get("MMAI_AI_BATCH", "6"))
AI_WORKERS = int(os.environ.get("MMAI_AI_WORKERS", "4"))
AI_ATTEMPTS = int(os.environ.get("MMAI_AI_ATTEMPTS", "3"))
IDLE_POLL_S = float(os.environ.get("MMAI_AI_QUEUE_POLL_S", "20"))
LIVE = ("Proposed", "Introduced", "Group confirmed", "Awaiting replacement")  # groups the AI assesses; Leased / Dissolved never
PRIORITY_CHANGE, PRIORITY_NEW, PRIORITY_ALT, PRIORITY_REFRESH = 0, 1, 2, 3
log = logging.getLogger("mmai.ai_queue")
PENDING_BASIS = "AI assessment pending"

_wake = threading.Event()
_lock = threading.Lock()
_thread: threading.Thread | None = None
def _initial_state() -> dict:
    return {"calls": 0, "failed_calls": 0, "batches_done": 0, "groups_assessed": 0, "groups_failed": 0, "in_flight": 0,
            "last_error": None, "last_error_at": None, "last_started_at": None, "last_finished_at": None, "last_batch_s": None, "busy": False, "pass_total": 0, "pass_done": 0,
            # T3(d) C2(b): durable-for-process signal of the end-of-pass reselection. `reselect_unsettled` starts True (conservative: nothing
            # has been checked since this process started - `reselect_last` None says so), is set True again BEFORE every reselection
            # attempt (so an unexpected exception can never leave it False) and becomes False only when a completed reselection settles
            # BOTH data sets. The worker retries it on its next wake even when no AI work is pending, and the drain check must not read
            # the queue as drained while it is set. `reselect_last` holds only closed counts/reasons (no prose).
            "reselect_unsettled": True, "reselect_last": None,
            # C4 review-repair telemetry (closed kinds only): repair turns made, groups re-asked / fixed, failed repairs, last closed kind / scope
            "repairs": 0, "repair_groups": 0, "repair_fixed": 0, "repair_errors": 0, "repair_last_error": None, "repair_last_scope": None}


STATE = _initial_state()
RESELECT_INCOMPLETE = "reselection did not complete; retried next pass"


# T5 / Modified A (9 Nov 2026): STATE is process-global and outside the database's retention/scrub provenance, so it never holds
# provider or model prose (which can quote a person's answers). Only these static categories are recorded. A FAILED group's row and
# its guarded history row (deleted with the group) hold a closed sentence chosen by the exception's kind / code (ai.failure_text),
# never the exception's words either; logs carry the category and the operational id only.
STATE_ERROR_NONE = None
STATE_ERROR_PROVIDER_CREDENTIALS = "PROVIDER_CREDENTIALS"   # credentials missing/expired: retrying is pointless
STATE_ERROR_PROVIDER = "PROVIDER_ERROR"                     # the model/provider call failed or returned an unusable reply
STATE_ERROR_UNEXPECTED = "UNEXPECTED"                       # the batch future itself raised (suffixed with the CLOSED matching.run_error_type_token)
STATE_ERROR_WORKER = "QUEUE_WORKER"                         # the background worker loop caught an exception (suffixed with the closed token)


class FailureText(str):
    """The closed sentence persisted for a failed AI step (ai.failure_text), carrying its closed kind / code as attributes so the
    queue classifies by metadata, never by scanning words in the text (T5, Modified A). A plain str everywhere else."""
    kind = "unexpected"
    code = None


def _failure(e: BaseException, step: str = "review") -> FailureText:
    """Closed persisted text for an exception from an AI step ('review' | 'brief' | 'introduction': generic failures are worded for
    the step, so a brief failure never denies the review that completed). Nothing of the exception's own words survives: our
    exceptions map by kind / code; any other exception maps to the fixed 'unexpected' sentence prefixed with the closed
    run_error_type_token."""
    from . import matching
    if isinstance(e, ai.AIUnavailable):
        t = FailureText(ai.failure_text(e, step))
        t.kind, t.code = ai.failure_kind(e), e.code
        return t
    token = matching.run_error_type_token(e)
    log.warning("AI step failed: unexpected (%s)", token)   # closed token only
    t = FailureText(f"{token}: {ai.failure_text(e, step)}")
    t.kind, t.code = "unexpected", "unexpected"
    return t


def _tok(ex: BaseException) -> str:
    """Closed exception-type token for logs (matching.RUN_ERROR_TYPE_TOKENS, else 'unknown'): never an arbitrary class name."""
    from . import matching
    return matching.run_error_type_token(ex)


def _no_point_retrying(e: BaseException) -> bool:
    """Credentials missing/refused or the SDK absent: retrying spends time for the same answer. Decided by the trusted exception's
    closed kind only - never by words in a message (a generic AIUnavailable that merely mentions credentials is retried)."""
    return isinstance(e, ai.AIUnavailable) and e.kind in ("credentials", "setup")


def failure_category(e: BaseException) -> str:
    """Static STATE-style category for an exception from an AI step, decided by trusted type / closed kind only (for callers that
    hold the exception, e.g. the introduction route's audit detail): credentials/setup -> PROVIDER_CREDENTIALS, any other
    AIUnavailable -> PROVIDER_ERROR, anything else -> UNEXPECTED:<closed token>."""
    from . import matching
    if isinstance(e, ai.AIUnavailable):
        return STATE_ERROR_PROVIDER_CREDENTIALS if e.kind in ("credentials", "setup") else STATE_ERROR_PROVIDER
    return f"{STATE_ERROR_UNEXPECTED}:{matching.run_error_type_token(e)}"


def state_error_category(err: str | None, exc: BaseException | None = None) -> str | None:
    """Map a batch's failure to the static category STATE carries. Nothing from any text is ever returned: a FailureText yields a
    constant chosen by its closed kind; a plain string yields the provider constant; a real exception OBJECT contributes only the
    closed matching.run_error_type_token (never an arbitrary class name)."""
    from . import matching
    if err is None and exc is None:
        return STATE_ERROR_NONE
    if exc is not None:
        return f"{STATE_ERROR_UNEXPECTED}:{matching.run_error_type_token(exc)}"
    if getattr(err, "kind", None) in ("credentials", "setup"):
        return STATE_ERROR_PROVIDER_CREDENTIALS
    return STATE_ERROR_PROVIDER


def _bump(**kw):
    with _lock:
        for k, v in kw.items():
            if isinstance(v, (int, float)) and k in ("calls", "failed_calls", "batches_done", "groups_assessed", "groups_failed", "in_flight", "pass_done",
                                                     "repairs", "repair_groups", "repair_fixed", "repair_errors"):
                STATE[k] = STATE.get(k, 0) + v
            else:
                STATE[k] = v


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------
def enqueue(c, group_ids, priority: int = PRIORITY_NEW, reason: str = "", refresh: bool = False, commit: bool = True) -> int:
    """Mark groups for AI assessment. `refresh=True` (GP8(b)) keeps the group ready (its rating stands) while a new apartment
    ranking is fetched in the background; otherwise the group becomes 'AI assessment pending' until the AI has assessed it."""
    ids = [int(g) for g in (group_ids if isinstance(group_ids, (list, tuple, set)) else [group_ids])]
    if not ids:
        return 0
    when = now_iso()
    n = 0
    for gid in ids:
        g = row(c.execute("SELECT id, status, ai_status FROM groups WHERE id=?", (gid,)))
        if not g or g["status"] not in LIVE:
            continue
        if refresh and g["ai_status"] == "ok":
            c.execute("UPDATE groups SET ai_refresh=1, ai_priority=?, ai_queued_at=COALESCE(ai_queued_at, ?), ai_error=NULL, updated_at=? WHERE id=?", (PRIORITY_REFRESH, when, when, gid))
        else:
            c.execute("""UPDATE groups SET ai_status='pending', ranking_basis=?, ai_priority=?, ai_queued_at=?, ai_attempts=0, ai_error=NULL, ai_refresh=0,
                         ai_rating=NULL, ai_rationale=NULL, ai_model=NULL, review_verdict=NULL, held_reason=NULL, brief_status=NULL, ready_at=NULL,
                         rule_version=NULL, evidence_hash=NULL, evidence_json=NULL, updated_at=? WHERE id=?""", (PENDING_BASIS, priority, when, when, gid))
        n += 1
    if commit:
        maybe_commit(c)   # inside a retention guard the outer step commits once
    kick()
    return n


def kick():
    _wake.set()


def requeue_interrupted(c) -> int:
    """Start-up: groups left 'running' by a previous process go back to pending; failed ones are left for staff to retry."""
    when = now_iso()
    cur = c.execute("UPDATE groups SET ai_status='pending', ranking_basis=?, ai_queued_at=COALESCE(ai_queued_at, ?) WHERE ai_status='running' AND status IN (%s)" % ",".join("?" * len(LIVE)), (PENDING_BASIS, when, *LIVE))
    n = cur.rowcount
    # live groups that were never assessed (built before the T6 queue existed, or by an older rules-only run) are queued too
    cur = c.execute("""UPDATE groups SET ai_status='pending', ranking_basis=?, ai_priority=CASE WHEN is_primary=1 THEN ? ELSE ? END, ai_queued_at=?
                       WHERE status IN (%s) AND ai_rating IS NULL AND (ai_status IS NULL OR ai_status IN ('skipped','pending'))""" % ",".join("?" * len(LIVE)),
                    (PENDING_BASIS, PRIORITY_NEW, PRIORITY_ALT, when, *LIVE))
    n += cur.rowcount
    # groups assessed before the queue (or before the current rule set) have a rating but no queue state / evidence. They are
    # NOT promoted to 'ok' (T6 fail-closed, 8 Nov 2026): the old text is kept as history in review_rounds and the group is
    # re-queued for a review under the current rules.
    legacy = rows(c.execute("SELECT id, ai_rating, ai_rationale, ai_model, rule_version FROM groups WHERE ai_status IS NULL AND ai_rating IS NOT NULL AND status IN (%s)" % ",".join("?" * len(LIVE)), LIVE))
    for g in legacy:
        c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (g["id"], 0, None, g["ai_rating"], g["ai_rationale"], None, None, 0, PRE_QUEUE_SUPERSEDED_TEXT,
                                                        g["ai_model"], g["rule_version"], None, when))
        c.execute("""UPDATE groups SET ai_status='pending', ranking_basis=?, ai_priority=?, ai_queued_at=?, ai_rating=NULL, ai_rationale=NULL, ai_model=NULL,
                     held_reason='Earlier assessment superseded: awaiting a review under the current rules', updated_at=? WHERE id=?""", (PENDING_BASIS, PRIORITY_ALT, when, when, g["id"]))
    n += len(legacy)
    # approved groups whose review rests on an earlier review contract (prompt/schema), rule version or model: the old review and
    # briefs stay as history, the group is re-queued and is not ready meanwhile (is_ready says so independently of this sweep).
    for g in rows(c.execute("SELECT id, rule_version, ai_model, evidence_json FROM groups WHERE ai_status='ok' AND status IN (%s)" % ",".join("?" * len(LIVE)), LIVE)):
        contract = (pj(g.get("evidence_json"), {}) or {}).get("review_contract")
        if contract != ai.REVIEW_CONTRACT or g.get("rule_version") != rules.RULE_VERSION or g.get("ai_model") != ai.MODEL:
            why = ("review contract" if contract != ai.REVIEW_CONTRACT else "rule version" if g.get("rule_version") != rules.RULE_VERSION else "model")
            c.execute("""UPDATE groups SET ai_status='pending', brief_status=CASE WHEN brief_status='ok' THEN 'stale' ELSE brief_status END, ranking_basis=?, ready_at=NULL,
                         ai_priority=COALESCE(ai_priority, ?), ai_queued_at=?, held_reason=?, updated_at=? WHERE id=?""",
                      (PENDING_BASIS, PRIORITY_CHANGE, when, f"Earlier approval superseded: the {why} changed; awaiting a fresh review and brief", when, g["id"]))
            n += 1
    n += withdraw_outdated_notes(c, when)
    # GAP-T4c v2 (9 Nov 2026): an approved group with a member whose notes were never read into the review (legacy rows: NULL;
    # edited note: pending; extraction failed) is re-queued the same way - it is not ready meanwhile (is_ready says so too).
    for g in rows(c.execute("""SELECT g.id FROM groups g WHERE g.ai_status='ok' AND g.status IN (%s) AND EXISTS
                               (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND COALESCE(e.notes_status,'pending') NOT IN ('ok','none'))""" % ",".join("?" * len(LIVE)), LIVE)):
        c.execute("""UPDATE groups SET ai_status='pending', brief_status=CASE WHEN brief_status='ok' THEN 'stale' ELSE brief_status END, ranking_basis=?, ready_at=NULL,
                     ai_priority=COALESCE(ai_priority, ?), ai_queued_at=?, held_reason=?, updated_at=? WHERE id=?""",
                  (NOTES_PENDING_TEXT, PRIORITY_CHANGE, when, "Earlier approval superseded: a member's notes had not been read for the review; awaiting a fresh review and brief", when, g["id"]))
        n += 1
    maybe_commit(c)
    if n:
        kick()
    return n


def outdated_notes_members(c, gid: int) -> list[int]:
    """9 Nov 2026 (triage fix 2, read-time currentness): enquiry ids of the group's members whose ACCEPTED notes ('ok') were made
    under an earlier extraction contract (stored input key != cache key of the same text now). Such a group is never ready, whatever
    its stored review says, even while the worker (which withdraws them) is off. Read-only."""
    from . import notes_facts
    cols = ", ".join(f"e.{k}" for k in notes_facts.SOURCE_FIELDS)
    out = []
    for e in rows(c.execute(f"SELECT e.id, e.party_size, e.notes_status, e.notes_input_key, {cols} FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND e.notes_status='ok'", (gid,))):
        if not notes_facts.is_current(e):
            out.append(int(e["id"]))
    return out


def withdraw_outdated_notes(c, when: str | None = None) -> int:
    """9 Nov 2026 (triage fix 2, currentness): an ACCEPTED notes projection ('ok') whose stored input key is no longer the cache key
    of the same text under the current extraction contract (prompt / catalogue / feedback version or model) is withdrawn to
    'pending': notes_packet_json NULL, so matching.members_for_ai binds no outdated fact, is_ready reports the member's notes as
    not read, and requeue_interrupted's unread path re-queues an approved group holding such a member (brief 'stale', not ready).
    History stays (note_extraction_cache rows keep the earlier key's reply; the earlier review and brief rows are untouched).
    Nothing is re-read here: the worker's bounded notes step does that, and only while MMAI_AI_QUEUE is on. Bounded, idempotent
    (a second pass finds no mismatch), no provider call."""
    from . import notes_facts
    when = when or now_iso()
    n = 0
    cols = ", ".join(f"e.{k}" for k in notes_facts.SOURCE_FIELDS)
    for e in rows(c.execute(f"SELECT e.id, e.party_size, e.notes_input_key, {cols} FROM enquiries e WHERE e.notes_status='ok'")):
        key = notes_facts.cache_key({k: e.get(k) for k in notes_facts.SOURCE_FIELDS}, e.get("party_size") or 0)
        seen = e.get("notes_input_key")
        if seen != key:
            # compare-and-set on the exact stale key this sweep compared (9 Nov 2026, reviewer N3): a CURRENT projection committed
            # by another connection between the SELECT and this UPDATE carries the new key and is left alone. Portable shape: plain
            # equality for a non-NULL key and an explicit IS NULL branch (never `IS ?`, which PostgreSQL does not accept).
            if seen is None:
                n += c.execute("UPDATE enquiries SET notes_status='pending', notes_packet_json=NULL, notes_residual=NULL, notes_updated_at=? "
                               "WHERE id=? AND notes_status='ok' AND notes_input_key IS NULL", (when, e["id"])).rowcount
            else:
                n += c.execute("UPDATE enquiries SET notes_status='pending', notes_packet_json=NULL, notes_residual=NULL, notes_updated_at=? "
                               "WHERE id=? AND notes_status='ok' AND notes_input_key=?", (when, e["id"], seen)).rowcount
    return n


def retry_failed(c, staff: str | None = None, group_ids: list[int] | None = None) -> int:
    where, args = ["(ai_status='failed' OR (ai_status='held' AND ranking_basis=?))", "status IN (%s)" % ",".join("?" * len(LIVE))], [NOTES_HELD_BASIS, *LIVE]
    if group_ids:
        where.append("id IN (%s)" % ",".join("?" * len(group_ids))); args += [int(x) for x in group_ids]
    ids = [r["id"] for r in rows(c.execute("SELECT id FROM groups WHERE " + " AND ".join(where), args))]
    if ids:   # T4c: a failed notes extraction of a member is re-queued with the group (C4: with a fresh attempt budget)
        c.execute("UPDATE enquiries SET notes_status='pending', notes_packet_json=NULL, notes_attempts=0, notes_next_retry_at=NULL, notes_updated_at=? WHERE notes_status='failed' AND id IN (SELECT enquiry_id FROM group_members WHERE group_id IN (%s))" % ",".join("?" * len(ids)), (now_iso(), *ids))
    n = enqueue(c, ids, priority=PRIORITY_CHANGE, reason="staff retry")
    if staff:
        audit(c, staff, "ai_retry", "group", ",".join(map(str, ids))[:200], f"{n} group(s) re-queued for AI assessment")
    return n


def reassess_all(c, dataset: str, staff: str | None = None) -> int:
    """Staff 'Refresh every AI assessment' for one data set: every live group is re-queued (members unchanged, so this is a
    deliberate, costly refresh; T6 never does it on its own)."""
    is_test = 1 if dataset == "test" else 0
    ids = [r["id"] for r in rows(c.execute("""SELECT g.id FROM groups g WHERE g.status IN (%s) AND EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id
                                              WHERE gm.group_id=g.id AND e.is_test=?)""" % ",".join("?" * len(LIVE)), (*LIVE, is_test)))]
    n = enqueue(c, ids, priority=PRIORITY_ALT, reason="staff refresh all")
    if staff:
        audit(c, staff, "ai_reassess_all", "dataset", dataset, f"{n} group(s) re-queued")
    return n


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
def _payload(c, g: dict, cap: dict | None = None) -> dict | None:
    """What the AI sees for one group, built from CURRENT rows and the CURRENT rules (never from stored legacy scores):
    members as `matching.members_for_ai` (refs, practical facts), and every offered apartment with its lease facts, bed
    allocation by ref, the deterministic rule-check results with provenance (Bill step 4: the model checks consistency against
    the applicable rule facts; it cannot waive them), the neutral rule score recomputed now, and the recorded apartment-feature
    facts against what members asked for (apartment_features.compare: Yes/No/Unknown, no weights). Fails closed: None when any
    member or apartment is unreadable or no longer passes the hard rules at an offered apartment.
    T5/T6 capture rule: every row the packet is built from comes from ONE `matching.evidence_rows` capture (`cap`, taken by the
    caller inside its retention-guard step or here), and the packet's evidence hash is computed from that same capture - never
    from a second read - so the facts the model sees and the hash that pins them are one snapshot."""
    from . import matching  # late import: matching imports this module
    if cap is None:
        cap = matching.evidence_rows(c, g["id"])
    if not cap.get("group"):
        return None
    ids = [int(x) for x in (g.get("member_key") or "").split(",") if x.strip().isdigit()]
    captured = [m["enquiry_id"] for m in cap["members"]]
    if not ids:
        ids = list(captured)
    if sorted(ids) != sorted(captured) or cap["group"].get("member_key") != g.get("member_key"):
        return None   # the membership moved between the queue's view of the group and this capture: assess the newer state, not a mix
    units, enq, pm_rows = [], {}, {}
    by_eid = {m["enquiry_id"]: m for m in cap["members"]}
    for eid in ids:
        e = by_eid[eid]["enquiry"]
        if not e:
            return None   # fail closed: a group is assessed with ALL its members or not at all
        try:
            pm_rows[eid] = [{k: r.get(k) for k in ("id", "name", "email", "birth_country", "birth_country_raw")} for r in by_eid[eid]["party_members"]]
            units.append(matching.unit_from_row(e, pm_rows[eid]))
            enq[eid] = e
        except Exception as ex:
            log.warning("group %s: member %s cannot be read for the AI packet (%s); group not assessed", g.get("id"), eid, _tok(ex))
            return None
    if not units or len(units) != len(ids):
        return None
    refs = matching.person_refs(units)
    refs_json = ref_provenance(units, pm_rows, live_uids(c, ids))   # immutable ref -> person identity (row + uid), captured BEFORE the model call; metadata only
    by_id = {str(u.id): u for u in units}
    apts, offers_refreshed = [], []
    # GP14 (inc3): a row a current member's active record excludes is history, not an option: never shown to the model (the
    # same per-record decision the generator and the offer guard make; matching.effective_group_apartments on THIS capture)
    for ga in sorted(matching.effective_group_apartments(cap), key=lambda x: (x.get("rank") if x.get("rank") is not None else 999, x["apartment_id"])):
        a = cap["apartments"].get(ga["apartment_id"])
        if not a or a.get("leased_at") or not a.get("available_from"):
            continue   # GP8(a): a leased/unlisted apartment is simply not offered any more
        apt = matching.apartment_from_row(a)
        stored = pj(ga.get("allocation_json"), {}) or {}
        # (member order is immaterial since 10 Nov 2026: rules.allocate_beds puts people in canonical order itself; the replay of the
        # stored order below is kept as harmless legacy and must not be relied on - never re-introduce order-dependent allocation)
        order = []
        for r in stored.get("rooms") or []:
            for x in r.get("occupants") or []:
                uid = str(x.get("person_id") or "").split("m")[0]
                if uid in by_id and uid not in order:
                    order.append(uid)
        ordered = [by_id[u] for u in order] + [u for u in units if str(u.id) not in order]
        try:
            from . import exit_exceptions
            exact = exit_exceptions.offers_from_capture(cap.get("exceptions") or [], a["id"])
            chk = rules.check_group(apt, matching.expand(ordered), exception_offers=exact)
        except Exception as ex:
            log.warning("group %s apartment %s: rule check error (%s)", g.get("id"), a["id"], _tok(ex))   # T5: no reason prose in logs
            return None
        if not chk.ok:
            log.warning("group %s apartment %s: hard rules no longer pass; group not assessed (reasons stay in the engine result)", g.get("id"), a["id"])
            return None
        score, parts = matching.preference_score(apt, ordered, chk)   # launch-neutral score from current facts, NOT the stored number
        if not _offer_matches(ga, chk):
            # the stored offer (dates, rate, rent, beds) is not what the rules produce from the current facts (a rule or inventory
            # change since it was saved). The model must review exactly the offer that is stored, so the two are made one here:
            #  * a Proposed group (not yet shown to anyone) follows the rules, as a full run would: the stored offer is refreshed
            #    inside this capture step and the captured row updated with it, so packet, hash and stored offer agree;
            #  * an Introduced or later group was introduced on those terms: they are never rewritten by the queue. Fail closed -
            #    the group is held with the reason for staff (OfferDrift); readiness already refuses it independently.
            if (cap["group"].get("status") or g.get("status")) != "Proposed":
                raise OfferDrift(g["id"], a["id"])
            refreshed = {"lease_start": chk.lease_start.isoformat(), "lease_end": chk.lease_end.isoformat(), "rate_kind": chk.rate_kind, "exit_window": chk.exit_window,
                         "weekly_rent": chk.weekly_rent, "extra_bed": chk.extra_bed, "total_weekly": chk.total_weekly,
                         "allocation_json": j({"rooms": chk.allocation, "score_parts": parts, "campus_km": matching._campus_km(apt, ordered)}),
                         "exception_ref": chk.exception_ref, "exception_total": chk.exception_total}
            c.execute("UPDATE group_apartments SET lease_start=?, lease_end=?, rate_kind=?, exit_window=?, weekly_rent=?, extra_bed=?, total_weekly=?, allocation_json=?, exception_ref=?,exception_total=? WHERE group_id=? AND apartment_id=?",
                      (*refreshed.values(), g["id"], a["id"]))
            cap["group_apartments"] = [dict(x, **refreshed) if x["apartment_id"] == a["id"] else x for x in cap["group_apartments"]]
            ga = dict(ga, **refreshed)
            offers_refreshed.append(a["id"])
        apts.append({"apartment_id": a["id"], "address": a["address"], "suburb": a.get("suburb"), "type_label": rules.APT_TYPES[rules.normalise_type(a["type_code"])]["label"],
                     "bills_included": bool(a.get("bills_included")), "available_from": a.get("available_from"), "lease_start": chk.lease_start.isoformat(),
                     "lease_end": chk.lease_end.isoformat(), "rate_kind": chk.rate_kind, "rate_band": matching.rate_band_fact(chk.rate_kind),
                     "stay_days": matching.stay_days_fact(chk.lease_start, chk.lease_end), "total_weekly": chk.total_weekly, "extra_bed": chk.extra_bed,
                     "allocation": chk.allocation, "allocation_for_ai": matching.allocation_for_ai(chk.allocation, refs),
                     "campus_km": matching._campus_km(apt, ordered), "campus_distance": matching.campus_distance_fact(apt, ordered, refs),
                     "rule_score": score, "score_parts": parts, "rank": ga["rank"],
                     "rule_checks": matching.rule_check_facts(apt, ordered, chk, refs),
                     "step_free_access": access_facts.effective(a)["value"], "step_free_source": access_facts.effective(a)["source"],   # A13(a) recorded fact bound to this row's address: 1 / 0 / None (facts.catalogue decides recorded vs missing)
                     "features": matching.feature_facts(a, [enq[int(u.id)] for u in ordered if int(u.id) in enq], refs)})
        if chk.exception_ref is not None:
            exact_record = next(x for x in cap["exceptions"] if x["context_ref"] == chk.exception_ref and x["status"] == "passes")
            apts[-1]["exit_exception"] = {
                "basis": "Staff-authorised exact exit and whole-apartment weekly total, including any extra-bed charge",
                "move_in": chk.lease_start.isoformat(), "move_out": chk.lease_end.isoformat(),
                "total_weekly": chk.exception_total, "approved_at": exact_record["approved_at"],
                "customer_agreement_recorded": bool(exact_record.get("agreement_recorded_at")),
            }
    if not apts:
        return None
    if cap["group"].get("apartment_id") in offers_refreshed:
        matching._switch_current(c, g["id"], cap["group"]["apartment_id"])   # the group's own offer columns and member beds follow the refreshed row
    # current neutral rule order (1 = best); the stored rule_rank/rule_score are refreshed so no pre-policy number survives as current.
    # Same tie-break as matching (cheaper whole rent first, then apartment id): a score tie must never inherit the AI's order from
    # the stored rank, otherwise rule_rank would silently follow the model after a refresh. (rule_rank/rule_score are outside the
    # evidence hash, so this write does not move the hash computed from the capture.)
    for i, a in enumerate(sorted(apts, key=lambda x: (-x["rule_score"], x["total_weekly"], x["apartment_id"])), start=1):
        a["rule_rank"] = i
        c.execute("UPDATE group_apartments SET rule_rank=?, rule_score=? WHERE group_id=? AND apartment_id=?", (i, a["rule_score"], g["id"], a["apartment_id"]))
    ev_hash, _ = matching.evidence_from_rows(cap)   # pins exactly the captured facts; the snapshot itself is recomputed at write time, never sent to the model
    return {"group_id": g["id"], "key": g.get("member_key"), "members_for_ai": matching.members_for_ai(units),
            "person_refs": refs, "refs_json": refs_json, "apartments": apts, "evidence_hash": ev_hash, "rule_version": rules.RULE_VERSION,
            "round": int(g.get("revision_round") or 0)}


def live_uids(c, enquiry_ids) -> dict[tuple[int, int | None], str]:
    """{(enquiry_id, party_member_id|None): person_uid} of the ACTIVE (unretired) GP13 identities of these enquiries. Read-only.
    A row without an identity yet is simply absent (never guessed)."""
    out: dict[tuple[int, int | None], str] = {}
    for eid in {int(x) for x in enquiry_ids}:
        for i in rows(c.execute("SELECT person_uid, role, party_member_id FROM people_identities WHERE enquiry_id=? AND retired_at IS NULL", (eid,))):
            out[(eid, None if i["role"] == "lead" else i["party_member_id"])] = i["person_uid"]
    return out


def ref_provenance(units, pm_rows: dict, uids: dict | None = None) -> dict[str, dict]:
    """{ref: {"enquiry_id": int, "party_member_id": int|None, "person_uid": str|None}} for every person a packet describes (U1,
    U1-m1 ... as the model and the stored text use them). Written as refs_json on every review_rounds / group_briefs row so
    history stays attributable to the actual people AFTER members are replaced (refs are positional) and so T5 deletion can find
    a former member's rows. Never sent to the model. A party slot with no recorded party_members row (placeholder companion) has
    party_member_id None. `uids` (live_uids at capture time) pins the immutable person behind each row: after a GP9(e) lead
    takeover the SAME enquiries row carries another person, and only the uid tells the two apart (root/reviewer probe, 9 Nov 2026).
    Internal metadata only; absent identity -> None, never inferred."""
    out: dict[str, dict] = {}
    uids = uids or {}
    for i, u in enumerate(units, start=1):
        k = 0
        pms = pm_rows.get(int(u.id)) or []
        for p in u.people:
            if p.is_lead:
                out[f"U{i}"] = {"enquiry_id": int(u.id), "party_member_id": None, "person_uid": uids.get((int(u.id), None))}
            else:
                k += 1
                pmid = int(pms[k - 1]["id"]) if k - 1 < len(pms) and pms[k - 1].get("id") is not None else None
                out[f"U{i}-m{k}"] = {"enquiry_id": int(u.id), "party_member_id": pmid, "person_uid": uids.get((int(u.id), pmid)) if pmid is not None else None}
    return out


def refs_intact(c, refs_json: dict | None) -> bool:
    """T5 companion to people_intact: every party_members row the output refers to must still be the SAME row (a party member
    deleted and re-added in the same ordinal slot while the model ran is a different person), and - when the map carries a
    person_uid - the SAME person must still be the active identity of that row (a GP9(e) lead takeover keeps the enquiries row
    but changes the human behind U1; output captured about the old person must never be written as the new one's)."""
    for ref, who in (refs_json or {}).items():
        pid = who.get("party_member_id")
        if pid is not None and not row(c.execute("SELECT 1 FROM party_members WHERE id=? AND enquiry_id=?", (pid, who.get("enquiry_id")))):
            return False
        uid = who.get("person_uid")
        if uid:
            # portable on SQLite and Postgres: no "IS ?" parameter comparison (PG rejects it); the two cases are separate statements
            if pid is None:
                live = row(c.execute("SELECT person_uid FROM people_identities WHERE enquiry_id=? AND retired_at IS NULL AND role='lead' AND party_member_id IS NULL",
                                     (who.get("enquiry_id"),)))
            else:
                live = row(c.execute("SELECT person_uid FROM people_identities WHERE enquiry_id=? AND retired_at IS NULL AND role='member' AND party_member_id=?",
                                     (who.get("enquiry_id"), pid)))
            if not live or live["person_uid"] != uid:
                return False
    return True


def _assess_batch(batch: list[dict], attempts: int = AI_ATTEMPTS) -> tuple[list[dict] | None, str | None, int]:
    """One AI call for a batch of groups, retried with back-off. Returns (results, error, calls_made)."""
    err = None
    meter = {"asks": 0}   # counts every model call actually made, including ai's own compact retry after a cut-off reply
    for attempt in range(max(1, attempts)):
        try:
            res = _call_review(batch, meter)
            if res and any("valid" not in r for r in res):   # a raw review (e.g. a test double): strict parsing applies to it too
                res = ai.parse_review_batch(res, batch)
            _note_repair(meter)
            return res, None, max(attempt + 1, meter["asks"])
        except ai.AINoRetry as e:
            # bounded output: ai already made its one compact retry (or the compact reply was unusable); never run the pair again
            _note_repair(meter)
            return None, _failure(e), max(attempt + 1, meter["asks"])
        except ai.AIUnavailable as e:
            _note_repair(meter)
            err = _failure(e)   # T5 / Modified A: the closed sentence for its kind / code, never the exception's words
            if _no_point_retrying(e):
                return None, err, max(attempt + 1, meter["asks"])  # no point retrying: credentials missing or expired
            time.sleep(min(20.0, 1.5 * (2 ** attempt)))
        except Exception as e:  # never let an unexpected error take the worker down
            _note_repair(meter)
            err = _failure(e)
            time.sleep(1.0)
    return None, err, max(max(1, attempts), meter["asks"])


def _note_repair(meter: dict) -> None:
    """C4 telemetry: ai's bounded review-repair turn reports itself through the meter (repair / repair_scope / repair_groups /
    repair_fixed / repair_error - closed kinds only). Counted ONCE per meter into the process counters (the meter is reused across
    attempts), so the dashboard can see how often the repair turn ran, what it fixed and the closed kind when it failed."""
    if not meter.get("repair") or meter.get("_repair_counted"):
        return
    meter["_repair_counted"] = True
    from . import ai
    kind = meter.get("repair_error")
    kinds = getattr(ai, "ERROR_KINDS", None)
    if kind is not None and kinds and kind not in kinds:
        kind = "unknown"
    _bump(repairs=1, repair_groups=int(meter.get("repair_groups") or 0), repair_fixed=int(meter.get("repair_fixed") or 0),
          repair_errors=1 if kind else 0, repair_last_error=kind if kind else STATE.get("repair_last_error"),
          repair_last_scope=meter.get("repair_scope") if meter.get("repair_scope") in ("groups", "batch") else STATE.get("repair_last_scope"))


def pack_batches(payloads: list, batch_size: int) -> list[list]:
    """Groups per review call: at most `batch_size`, and - when ai exposes the uncapped `review_token_estimate(groups)` (C4) - never
    a batch whose estimated reply would exceed OUTPUT_TOKEN_CEILING, so a batch of large groups is split rather than silently capped
    (a single group always goes alone even if it caps). Engineering sizing only, no business weight; order preserved."""
    from . import ai
    budget = getattr(ai, "review_token_estimate", None)   # the UNCAPPED estimate (the capped review_token_budget can never exceed the ceiling, so it cannot split)
    ceiling = int(getattr(ai, "OUTPUT_TOKEN_CEILING", 0) or 0)
    size = max(1, int(batch_size or 1))
    if not callable(budget) or ceiling <= 0:
        return [payloads[i:i + size] for i in range(0, len(payloads), size)]
    out: list[list] = []
    cur: list = []
    for item in payloads:
        p = item[1] if isinstance(item, tuple) else item
        if cur and (len(cur) >= size or int(budget([x[1] if isinstance(x, tuple) else x for x in cur] + [p])) > ceiling):
            out.append(cur)
            cur = []
        cur.append(item)
    if cur:
        out.append(cur)
    return out


def _metered(fn, *args, meter: dict):
    """Call an ai entry point with the call meter when it accepts one; a double without the keyword counts as one call."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):   # pragma: no cover
        params = {}
    if "meter" in params or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(*args, meter=meter)
    meter["asks"] = int(meter.get("asks") or 0) + 1
    return fn(*args)


def _call_review(batch: list[dict], meter: dict):
    return _metered(ai.assess_groups_and_apartments, batch, meter=meter)


def people_intact(c, gid: int, person_refs: dict | None) -> bool:
    """T5 guard shared by every writer of generated text (review rounds, briefs, introduction drafts): every person the output
    is about must still exist - each enquiry row and each party member slot in `person_refs` ('12', '12m1' ...) and the group's
    membership must still be exactly those enquiries. Output about a deleted person is DROPPED before anything is persisted
    (no history row carries their text); ordinary answer changes are caught separately by the evidence hash.
    Callers run this inside the connection's RLock together with their inserts, so the reviewer's deletion/purge (which takes
    the same lock) cannot interleave between the check and the write on the shared connection."""
    want: dict[int, int] = {}
    for pid in (person_refs or {}):
        base, _, k = str(pid).partition("m")
        if not base.isdigit():
            return False
        want[int(base)] = max(want.get(int(base), 0), int(k) if k.isdigit() else 0)
    if not want:
        return False
    members = {int(r["enquiry_id"]) for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))}
    if members != set(want):
        return False
    for eid, n_party in want.items():
        e = row(c.execute("SELECT party, party_size FROM enquiries WHERE id=?", (eid,)))
        if not e:
            return False
        # party persons mirror matching.unit_from_row: the lead plus (party_size - 1) slots (a couple is exactly 2), whether or
        # not a party_members row was recorded for each slot - so the slot count, not the row count, says who still exists
        party = e.get("party") or "Alone"
        slots = 0 if party == "Alone" else (1 if party.startswith("Couple") else max(0, int(e.get("party_size") or 1) - 1))
        if n_party > slots:
            return False
    return True


class OfferDrift(Exception):
    """An Introduced-or-later group's stored offer no longer matches what the rules produce from the current facts. The queue never
    rewrites terms a group was introduced on; the group is held for staff with this reason (static text, ids only)."""
    def __init__(self, group_id: int, apartment_id: int):
        super().__init__(f"group {group_id} apartment {apartment_id}")
        self.group_id, self.apartment_id = group_id, apartment_id


OFFER_DRIFT_REASON = ("The offer this group was introduced on (dates, rent or bed allocation) no longer matches the current rules or "
                      "apartment record; use Re-check options on the group page, then wait for a fresh AI review before sharing")
NO_SUITABLE_OFFER_TEXT = ("No listed apartment currently passes every requirement for this group. "
                          "Re-check options after the requirements or available apartments change.")


def _step(c, label: str):
    """One T5 step on the shared connection: db.retention_guard (DB-enforced against OTHER cooperating connections, RLock against
    other threads of this one). Identity checks and every write about people happen inside; the step commits once at exit."""
    return db.retention_guard(c, label=label)


GUARD_FAILURES = (db.RetentionLockTimeout, db.ConnectionLostMidOperation)   # nothing written; the result is dropped and the group re-queued in a NEW step
REQUEUE_AFTER_GUARD_FAILURE = "Result not recorded: the database was busy or the connection was lost; re-queued"   # static text, no provider or personal content
REVIEW_DISCARDED_TEXT = "Review discarded: a person it described was deleted or the membership changed; re-queued"
FACTS_CHANGED_REQUEUED_TEXT = "Facts changed while the AI was reviewing; re-queued"
FACTS_CHANGED_STAFF_TEXT = "Facts changed while the AI was reviewing; staff retry needed"
UNREADABLE_RECORD_TEXT = "Group cannot be assessed: a member or apartment record is missing or unreadable"
BRIEF_FAILED_PREFIX = "Staff brief failed: "
# Every sentence the queue may write into a FAILURE sink (groups.ai_error, a 'Staff brief failed: ' held_reason, group_briefs.error,
# review_rounds.invalid_reason). Anything else found there is a legacy row written before Modified A (9 Nov 2026) and is replaced by
# ai.LEGACY_FAILURE_TEXT (neutralise_legacy_failure_text). Hold / revise rationale and apartment reasons are NOT failure sinks.
PRE_QUEUE_SUPERSEDED_TEXT = "pre-queue assessment, superseded (no evidence snapshot)"   # fixed sentence written by requeue_interrupted for round-0 history rows
CLOSED_FAILURE_SENTENCES = ai.ALL_FAILURE_SENTENCES | {ai.LEGACY_FAILURE_TEXT, OFFER_DRIFT_REASON, NO_SUITABLE_OFFER_TEXT, REQUEUE_AFTER_GUARD_FAILURE, REVIEW_DISCARDED_TEXT,
                                                                FACTS_CHANGED_REQUEUED_TEXT, FACTS_CHANGED_STAFF_TEXT, UNREADABLE_RECORD_TEXT, PRE_QUEUE_SUPERSEDED_TEXT}


def is_closed_failure_text(text) -> bool:
    """True when `text` is exactly one of the closed failure sentences (or a closed-token / closed-count variant of one)."""
    from . import matching
    if text is None:
        return True
    t = str(text)
    if t in CLOSED_FAILURE_SENTENCES:
        return True
    head, sep, tail = t.partition(": ")
    if sep and head in matching.RUN_ERROR_TYPE_TOKENS and tail in ai.ALL_FAILURE_SENTENCES:
        return True
    m = re.fullmatch(re.escape(ai.FAILURE_TEXT["review_unusable"]) + r" \((\d+) problems? found\)", t)
    return bool(m)


def neutralise_legacy_failure_text(c, commit: bool = False) -> dict:
    """Bounded, idempotent projection for rows written BEFORE Modified A: any failure-sink text that is not a closed sentence is
    replaced by ai.LEGACY_FAILURE_TEXT (a 'Staff brief failed: ' hold keeps its prefix). Nothing else is touched: valid hold / revise
    rationale, group_apartments.ai_reason and the review's own content in review_json (verdict, rating, rationale, findings,
    apartments, replace_refs ...) are intentional generated text under retention, not failure diagnostics. One exception inside
    review_json: before Modified A, _apply_result stored the whole in-process result INCLUDING the validator's diagnostic
    `invalid_reason` (which can quote the reply) as a duplicate of the column; that single key is removed from legacy review_json
    rows and every other key is kept byte-for-byte (root read, 9 Nov 2026). Distinct from staff_view's read-time presentation mask,
    which hides but does not change rows. Returns per-sink change counts; a second run changes nothing. The caller commits (or
    passes commit=True); the call site is the integration builder's."""
    counts = {"groups.ai_error": 0, "groups.held_reason": 0, "group_briefs.error": 0, "review_rounds.invalid_reason": 0,
              "review_rounds.review_json.invalid_reason": 0}
    # held_reason is a failure sink ONLY when the row's provenance says so (brief_status='failed': the staff-brief writer is the one
    # place that sets both). A hold rationale that merely begins with the same words is intentional generated text and is kept
    # byte-for-byte (prefix-collision correction, 9 Nov 2026; the integration builder's positive controls in test_t5_failure_text_hooks).
    for g in rows(c.execute("SELECT id, ai_error, held_reason, brief_status FROM groups WHERE ai_error IS NOT NULL OR (brief_status='failed' AND held_reason LIKE ?)",
                            (BRIEF_FAILED_PREFIX + "%",))):
        if g["ai_error"] is not None and not is_closed_failure_text(g["ai_error"]):
            c.execute("UPDATE groups SET ai_error=? WHERE id=?", (ai.LEGACY_FAILURE_TEXT, g["id"])); counts["groups.ai_error"] += 1
        hr = g["held_reason"] or ""
        if g["brief_status"] == "failed" and hr.startswith(BRIEF_FAILED_PREFIX) and not is_closed_failure_text(hr[len(BRIEF_FAILED_PREFIX):]):
            c.execute("UPDATE groups SET held_reason=? WHERE id=?", (BRIEF_FAILED_PREFIX + ai.LEGACY_FAILURE_TEXT, g["id"])); counts["groups.held_reason"] += 1
    for b in rows(c.execute("SELECT id, error FROM group_briefs WHERE error IS NOT NULL")):
        if not is_closed_failure_text(b["error"]):
            c.execute("UPDATE group_briefs SET error=? WHERE id=?", (ai.LEGACY_FAILURE_TEXT, b["id"])); counts["group_briefs.error"] += 1
    for r in rows(c.execute("SELECT id, invalid_reason FROM review_rounds WHERE invalid_reason IS NOT NULL")):
        if not is_closed_failure_text(r["invalid_reason"]):
            c.execute("UPDATE review_rounds SET invalid_reason=? WHERE id=?", (ai.LEGACY_FAILURE_TEXT, r["id"])); counts["review_rounds.invalid_reason"] += 1
    for r in rows(c.execute("SELECT id, review_json FROM review_rounds WHERE review_json IS NOT NULL AND review_json LIKE '%invalid_reason%'")):
        # Scope (explicit): only a JSON OBJECT whose `invalid_reason` actually CARRIES diagnostic content (a non-empty string that
        # is not already a closed sentence) is rewritten, and only that key is removed. Healthy rows keep byte identity: unreadable
        # JSON (pj -> None), readable non-object JSON (a list, string, number), objects without the key and objects with
        # `invalid_reason: null / ""` or a closed sentence are left exactly as stored - review_json is a retained review record,
        # not a failure sink, and nothing but that duplicated diagnostic is a projection target.
        doc = pj(r["review_json"], None)
        if isinstance(doc, dict) and isinstance(doc.get("invalid_reason"), str) and doc["invalid_reason"] and not is_closed_failure_text(doc["invalid_reason"]):
            del doc["invalid_reason"]
            c.execute("UPDATE review_rounds SET review_json=? WHERE id=?", (j(doc), r["id"])); counts["review_rounds.review_json.invalid_reason"] += 1
    if commit:
        maybe_commit(c)
    return counts


def _requeue_after_guard_failure(c, g: dict, kind: str) -> bool:
    """A review/brief step could not be recorded (lock timeout: clean, nothing written; connection lost: the outcome of that step is
    UNKNOWN). Reconcile from the database in a fresh guarded step, never from memory and never by replaying the model result:
      * review: the group is re-queued only if it is STILL 'running' for the same membership (a step whose commit did land left it
        'ok'/'held'/..., and is kept); the static text says why;
      * brief: a brief step that did not land leaves brief_status='pending', which the next pass picks up by itself; only the
        reason is recorded, again only when the row still says pending.
    Returns False when even this bookkeeping could not be done (logged with ids only; requeue_interrupted covers it at the next start)."""
    try:
        with _step(c, label=f"{kind} requeue"):
            when = now_iso()
            if kind == "review":
                c.execute("""UPDATE groups SET ai_status='pending', ranking_basis=?, ai_error=?, ai_refresh=0, ready_at=NULL, ai_queued_at=?, updated_at=?
                             WHERE id=? AND member_key=? AND ai_status='running' AND status IN (%s)""" % ",".join("?" * len(LIVE)),
                          (PENDING_BASIS, REQUEUE_AFTER_GUARD_FAILURE, when, when, g["id"], g.get("member_key"), *LIVE))
            else:
                c.execute("UPDATE groups SET held_reason=?, ready_at=NULL, updated_at=? WHERE id=? AND member_key=? AND brief_status='pending'",
                          (REQUEUE_AFTER_GUARD_FAILURE, when, g["id"], g.get("member_key")))
        return True
    except db.RetentionGuardError as ex:
        log.warning("group %s: %s could not be re-queued after a guard failure (%s)", g.get("id"), kind, _tok(ex))
        return False


def _apply_result(c, g: dict, p: dict, r: dict | None, err: str | None, calls: int) -> bool:
    """Persist one group's review (or failure). Returns True only for a VALID, APPROVED review written against evidence that is
    still current. Everything else leaves the group not ready, with the reason on the row and the raw review in review_rounds."""
    from . import matching, review_loop  # late imports: both import this module
    when = now_iso()
    live = row(c.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    if not live or live["status"] not in LIVE:
        return False
    if live["member_key"] != g.get("member_key"):
        return False  # the members changed while the AI was working: the newer queue entry will cover it
    rnd = int(live.get("revision_round") or 0)
    attempts = int(live.get("ai_attempts") or 0) + calls
    # T5: the identity checks come FIRST - before ANY persistence, the failure path included (root's probe: an outage after a
    # same-slot companion deletion must leave only a generic re-queue, never a row that is about the packet the model saw)
    if not people_intact(c, g["id"], p.get("person_refs")) or not refs_intact(c, p.get("refs_json")):
        c.execute("""UPDATE groups SET ai_status='pending', ranking_basis=?, ai_error=?, ai_attempts=?, ai_refresh=0, ready_at=NULL, ai_queued_at=?, updated_at=? WHERE id=?""",
                  (PENDING_BASIS, REVIEW_DISCARDED_TEXT, attempts, when, when, g["id"]))
        return False
    if r is None:
        c.execute("""UPDATE groups SET ai_status='failed', ranking_basis='Rules only (AI failed)', ai_error=?, ai_attempts=?, ai_refresh=0, ready_at=NULL, updated_at=? WHERE id=?""",
                  (err or ai.FAILURE_TEXT["no_review"], attempts, when, g["id"]))
        return False
    # the review is recorded whatever else it says (history of the people still in the system; the reviewer's T5 purge removes it with them)
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, replace_refs_json, review_json, valid, invalid_reason, model, rule_version, evidence_hash, created_at, refs_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (g["id"], rnd, r.get("verdict"), r.get("rating"), r.get("rationale"), j(r.get("replace_refs") or []), j({k: v for k, v in r.items() if k != "invalid_reason"}),
               1 if r.get("valid") else 0, ai.persisted_invalid_reason(r),   # Modified A: closed code + count; the validator's sentences (which quote the reply) stay in process
               ai.MODEL, rules.RULE_VERSION, p.get("evidence_hash"), when, j(p.get("refs_json") or {})))
    if not r.get("valid"):
        c.execute("""UPDATE groups SET ai_status='failed', ranking_basis='Rules only (AI review unusable)', ai_error=?, ai_attempts=?, ai_refresh=0, ready_at=NULL, updated_at=? WHERE id=?""",
                  (ai.persisted_invalid_reason(r), attempts, when, g["id"]))
        return False
    # the facts must still be what the AI saw: recomputed from the rows now, never trusted from the payload alone
    now_hash, now_snap = matching.group_evidence(c, g["id"])
    if now_hash != p.get("evidence_hash"):
        requeue = attempts < 3 * AI_ATTEMPTS
        c.execute("""UPDATE groups SET ai_status=?, ranking_basis=?, ai_error=?, ai_attempts=?, ai_refresh=0, ready_at=NULL, ai_queued_at=?, updated_at=? WHERE id=?""",
                  ("pending" if requeue else "failed", PENDING_BASIS if requeue else "Rules only (facts kept changing during review)",
                   FACTS_CHANGED_REQUEUED_TEXT if requeue else FACTS_CHANGED_STAFF_TEXT, attempts, when, when, g["id"]))
        return False
    # apartment ranks in the AI's order (GP6(a)); the parser guarantees every offered apartment exactly once
    reasons = {x["apartment_id"]: x["reason"] for x in r["apartments"]}
    ranked = [x["apartment_id"] for x in r["apartments"]]
    c.execute("UPDATE group_apartments SET rank=rank+1000 WHERE group_id=?", (g["id"],))
    for rank, aid in enumerate(ranked, start=1):
        c.execute("UPDATE group_apartments SET rank=?, ai_reason=? WHERE group_id=? AND apartment_id=?", (rank, reasons.get(aid), g["id"], aid))
    base = dict(ai_rating=r["rating"], ai_rationale=r["rationale"], ai_model=ai.MODEL, ai_assessed_at=when, ai_error=None, ai_refresh=0, ai_attempts=attempts,
                review_verdict=r["verdict"], rule_version=rules.RULE_VERSION, evidence_hash=now_hash, evidence_json=j(now_snap), updated_at=when)
    if r["verdict"] == "approve":
        # GAP-T4c v2 / T6 staff gate: an explicitly ESSENTIAL practical apartment need from a member's own notes that nothing recorded
        # can verify (step-free access) holds the approved group for staff with a plain label; no rule, weight or eligibility change.
        # C3 (reviewer G1, 10 Nov 2026): the gate is bound to the offer that BECOMES current - a Proposed group follows the AI's #1,
        # so that apartment is gated (and switched to) here; every later switch path is covered by the same gate inside is_ready.
        cur = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (g["id"],)))
        target = ranked[0] if (live["status"] == "Proposed" and ranked) else (cur or {}).get("apartment_id")
        if cur and target is not None and cur["apartment_id"] != target:   # a Proposed group follows its new #1; introduced or later groups keep theirs
            matching._switch_current(c, g["id"], target)
        gate = essential_need_gate(c, g["id"], target, members=p.get("members_for_ai") or [])
        if gate["hold"]:
            _update(c, g["id"], ai_status="held", ranking_basis=PRACTICAL_HOLD_BASIS, brief_status=None, ready_at=None,
                    held_reason=gate["reason"][:600], **base)
            return False
        _update(c, g["id"], ai_status="ok", ranking_basis="AI", brief_status="pending", held_reason=None, ready_at=None, **base)
        return True
    if r["verdict"] == "hold":
        _update(c, g["id"], ai_status="held", ranking_basis="Held for staff (AI review)", brief_status=None, ready_at=None,
                held_reason=("AI review: hold. " + r["rationale"])[:600], **base)
        return False
    # revise: alternatives are formed by the rules and queued; this group waits (held) until one is approved and briefed
    _update(c, g["id"], ai_status="revise", ranking_basis="Held: AI asked for a revision", brief_status=None, ready_at=None,
            held_reason=("AI review: revise " + ", ".join(r["replace_refs"]) + ". " + r["rationale"])[:600], **base)
    review_loop.revise(c, live, p, r)
    return False


PRACTICAL_HOLD_BASIS = "Held for staff (practical need to confirm)"
PRACTICAL_NOT_READY = "an essential practical need (from a member's notes) is not confirmed as met at the current apartment"


def essential_need_members(c, gid: int) -> list[dict]:
    """Read and bind current notes once, reusable across the group's options."""
    from . import matching, notes_facts
    es = rows(c.execute("SELECT e.* FROM enquiries e JOIN group_members gm ON gm.enquiry_id=e.id WHERE gm.group_id=? ORDER BY e.id", (gid,)))
    return [{"ref": f"U{i}", "notes_facts": notes_facts.bind(matching._notes_packet(e.get("notes_packet_json")) if notes_facts.is_current(e) else [], e)}
            for i, e in enumerate(es, start=1)]


def essential_need_gate(c, gid: int, apartment_id, members: list[dict] | None = None) -> dict:
    """notes_facts.hold_gate for ONE apartment (the offer in question) and the group's members: {hold, reason, refs}. `members`
    defaults to the group's current members with their current validated notes projection (same binding as the review packet).
    Read-only; used at review apply (against the apartment that becomes current) and inside is_ready (against the current offer),
    so a switch of the current apartment by any path can never make an essential need pass unchecked."""
    from . import notes_facts
    apt = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,))) if apartment_id is not None else None
    if members is None:
        members = essential_need_members(c, gid)
    return notes_facts.hold_gate(members, apt)


def _update(c, gid: int, **cols):
    keys = list(cols)
    c.execute("UPDATE groups SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE id=?", [cols[k] for k in keys] + [gid])


# ---------------------------------------------------------------------------
# Readiness (the single source of truth, T6 + T4(f))
# ---------------------------------------------------------------------------
def evidence_currency(c, g: dict) -> dict:
    """{current: bool, reasons: [str], carveout: bool} - the EVIDENCE part of readiness only (succ3, 10 Nov 2026, N4): does the stored
    AI review / staff brief still describe this group under today's review contract, rule version, approved model, every member's
    current validated notes and the current compatibility evidence (GP8(b) carve-out honoured exactly as is_ready does)? Ownership,
    circulation, status, pending stock work and staff holds are NOT currency and are left to is_ready / can_circulate - a legitimate
    hold does not make a current review historical, and a stored hash alone never makes an old one current. The GP8 carve-out is
    reported (carveout=True) but never makes the text current: display currency is separate from readiness. Read-only."""
    from . import matching
    reasons = []
    carveout = False
    if g.get("rule_version") != rules.RULE_VERSION:
        reasons.append("assessed under an earlier rule version")
    if g.get("ai_model") != ai.MODEL:
        reasons.append("assessed with a model other than the approved one")
    if (pj(g.get("evidence_json"), {}) or {}).get("review_contract") != ai.REVIEW_CONTRACT:
        reasons.append("reviewed under an earlier review contract (prompt/schema); re-review required")
    unread = c.execute("SELECT COUNT(*) FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND COALESCE(e.notes_status,'pending') NOT IN ('ok','none')",
                       (g["id"],)).fetchone()[0]
    if unread:
        reasons.append(f"{unread} member note(s) not yet read into the review")
    outdated = outdated_notes_members(c, g["id"])
    if outdated:
        reasons.append(f"{len(outdated)} member note(s) read under an earlier extraction contract; re-read required")
    if c.execute("SELECT COUNT(*) FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND e.lead_answers_unverified_at IS NOT NULL", (g["id"],)).fetchone()[0]:
        reasons.append("a member's individual answers await confirmation after a change of enquirer")
    if g.get("evidence_hash"):
        now_hash, now = matching.group_evidence(c, g["id"])
        if now_hash != g["evidence_hash"]:
            then = pj(g.get("evidence_json"), {}) or {}
            if _gp8b_carveout(c, then, now, bool(g.get("ai_refresh"))):
                # readiness may stand (GP8(a)/(b): a reviewed apartment dropped out leased / unlisted, the rest identical) - but the
                # stored text described the earlier offered set, so for DISPLAY it is history, not the current assessment
                carveout = True
                reasons.append("the offered apartments changed since this was written (readiness still stands under the availability exception; the text describes the earlier offer)")
            else:
                reasons.append("facts changed since the AI review (answers, members, apartments, rules or model)")
    else:
        reasons.append("no evidence snapshot")
    return {"current": not reasons, "reasons": reasons, "carveout": carveout}


def is_ready(c, g: dict) -> dict:
    """{ready: bool, reasons: [str]} for one group row. Ready means: live status, a valid approved review under the current rule
    version and the approved model, both staff briefs ok, the compatibility evidence unchanged (GP8(b) carve-out: new apartments
    may have been added while members and retained apartments are unchanged and a refresh is pending), and the hard rules still
    passing at the current apartment. Every ready list, counter, introduction and send endpoint must use this."""
    from . import matching
    reasons = []
    carveout = False
    if g.get("status") not in READY_STATUSES:
        reasons.append(f"status {g.get('status')}")
    if g.get("ai_status") != "ok" or g.get("review_verdict") != "approve":
        reasons.append({"pending": "AI review pending", "running": "AI review running", "failed": "AI review failed", "revise": "AI asked for a revision",
                        "held": "held for staff", "stale": "assessment stale"}.get(g.get("ai_status"), "no approved AI review"))
    if g.get("rule_version") != rules.RULE_VERSION:
        reasons.append("assessed under an earlier rule version")
    if g.get("ai_model") != ai.MODEL:
        reasons.append("assessed with a model other than the approved one")
    if (pj(g.get("evidence_json"), {}) or {}).get("review_contract") != ai.REVIEW_CONTRACT:
        reasons.append("reviewed under an earlier review contract (prompt/schema); re-review required")
    if g.get("brief_status") != "ok":
        reasons.append({"pending": "staff brief pending", "failed": "staff brief failed", "stale": "staff brief stale"}.get(g.get("brief_status"), "no staff brief"))
    # GAP-T4c v2: the review must have seen every member's validated notes. A member whose notes were never read (legacy row,
    # NULL), are pending again (note edited) or failed to extract makes the group not ready, whatever the stored review says.
    unread = c.execute("SELECT COUNT(*) FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND COALESCE(e.notes_status,'pending') NOT IN ('ok','none')",
                       (g["id"],)).fetchone()[0]
    if unread:
        reasons.append(f"{unread} member note(s) not yet read into the review")
    outdated = outdated_notes_members(c, g["id"])
    if outdated:
        reasons.append(f"{len(outdated)} member note(s) read under an earlier extraction contract; re-read required")
    if c.execute("SELECT COUNT(*) FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND e.lead_answers_unverified_at IS NOT NULL", (g["id"],)).fetchone()[0]:
        reasons.append("a member's individual answers await confirmation after a change of enquirer")
    if g.get("evidence_hash"):
        now_hash, now = matching.group_evidence(c, g["id"])
        if now_hash != g["evidence_hash"]:
            then = pj(g.get("evidence_json"), {}) or {}
            if _gp8b_carveout(c, then, now, bool(g.get("ai_refresh"))):
                carveout = True
            else:
                reasons.append("facts changed since the AI review (answers, members, apartments, rules or model)")
    elif not reasons:
        reasons.append("no evidence snapshot")
    # stock lifecycle: a group with no current offer (apartment deleted / withdrawn / leased elsewhere, nothing else valid) or with
    # follow-up work still pending after a stock change is NOT ready - an explicit gate, not just a page label
    if g.get("apartment_id") is None:
        reasons.append("no current apartment (held after a stock change)")
    if g.get("id") is not None:
        from . import stock_lifecycle
        if stock_lifecycle.group_has_pending_work(c, g["id"]):
            reasons.append("being re-checked after a stock change")
    if not reasons:
        ok, why = _hard_rules_still_pass(c, g)
        if not ok:
            reasons.append("hard rules no longer pass: " + why)
    if g.get("id") is not None and g.get("ai_status") == "ok" and g.get("apartment_id") is not None:
        # C3 (reviewer G1): the essential-need gate is re-evaluated against the CURRENT offer at read time, so a later switch of the
        # current apartment (stock follow-up, refresh, manual switch) to one with no record or a contrary record is never ready
        if essential_need_gate(c, g["id"], g.get("apartment_id"))["hold"]:
            reasons.append(PRACTICAL_NOT_READY)
    return {"ready": not reasons, "reasons": reasons, "carveout": carveout and not reasons}


def can_circulate(c, g: dict) -> dict:
    """Readiness is necessary, not sufficient, for circulating a group to its members (GP7): the group must also be the PRIMARY
    proposal and none of its members may be owned by another live group (committed later state anywhere, or another primary
    proposal). {ok, reasons, ready_check, busy}. Used by the introduction, mark-introduced and 'ready to circulate' endpoints;
    promote() applies the same ownership rule through review_loop.owners_elsewhere."""
    from . import review_loop
    chk = is_ready(c, g)
    reasons = list(chk["reasons"])
    if not g.get("is_primary"):
        reasons.append("not the primary proposal (alternative candidate)")
    members = sorted({int(r["enquiry_id"]) for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))})
    busy = review_loop.owners_elsewhere(c, members, [int(g["id"])], need_primary=True) if members else []
    if busy:
        reasons.append(f"member(s) {busy} are committed in another live group")
    return {"ok": not reasons, "reasons": reasons, "ready_check": chk, "busy": busy}


def _gp8b_carveout(c, then: dict, now: dict, refresh_pending: bool) -> bool:
    """GP8(a)/(b) exactly. A reviewed group stays usable while its AI refresh is pending ONLY when: members (every practical fact,
    party composition, rejections, pool eligibility), rule version and model are identical; every apartment still offered was
    reviewed with identical lease facts (dates, rate, rent, extra bed, bed allocation); any apartment that dropped out did so
    because it is now LEASED or unlisted (GP8(a): the next stored eligible option is offered on the stored rule ranking); new
    apartments may have been appended. At least one reviewed apartment must remain. Anything else is stale."""
    if not refresh_pending or not then or not now:
        return False
    for k in ("rule_version", "model", "review_contract", "members", "group"):
        if then.get(k) != now.get(k):
            return False
    then_by = {a["apartment_id"]: a for a in then.get("apartments") or []}
    now_by = {a["apartment_id"]: a for a in now.get("apartments") or []}
    if not then_by or not now_by or then_by == now_by:
        return False
    for aid, a in now_by.items():
        if aid in then_by and then_by[aid] != a:
            return False            # a reviewed apartment's lease facts changed: not a carve-out case
    for aid in set(then_by) - set(now_by):
        a = row(c.execute("SELECT leased_at, available_from FROM apartments WHERE id=?", (aid,)))
        if a and not a.get("leased_at") and a.get("available_from"):
            return False            # dropped while still available: that is a changed offer, not GP8(a)
    return bool(set(then_by) & set(now_by))


def _hard_rules_still_pass(c, g: dict) -> tuple[bool, str]:
    """Final deterministic guard (T6 B/C): the members as they are NOW still pass every hard rule at EVERY apartment the group is
    circulated with, and the persisted offer (lease dates, rate, rents, extra bed, bed allocation) is exactly what the rules
    produce today - not merely 'some feasible lease exists'. Nothing the AI said can waive this."""
    from . import matching
    units = []
    for gm in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))):
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (gm["enquiry_id"],)))
        if not e or e.get("barred_at"):
            return False, "a member is missing or barred"
        if e.get("status") not in matching.ACTIVE_ENQUIRY_STATUSES:
            return False, f"member {e['id']} is {e.get('status')}"
        try:
            units.append(matching.unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))))
        except Exception as ex:
            log.warning("group %s: member record unreadable (%s)", g.get("id"), matching.run_error_type_token(ex))
            return False, f"member {e['id']} unreadable"
    if not units:
        return False, "no members"
    offers = rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY COALESCE(rank, 999), apartment_id", (g["id"],)))
    # GP14 (inc3): rows a current member's active record excludes are history, not offers - they neither make the group unready
    # (e.g. when such an apartment is later leased) nor count as the current offer (matching.excluded_option_ids, one capture)
    eligible = {int(x["apartment_id"]) for x in matching.eligible_offers(c, g["id"])}
    offers = [o for o in offers if int(o["apartment_id"]) in eligible]
    if not offers or g.get("apartment_id") not in {o["apartment_id"] for o in offers}:
        return False, "the current apartment is not among the group's offered apartments"
    by_id = {str(u.id): u for u in units}
    for o in offers:
        a = row(c.execute("SELECT * FROM apartments WHERE id=?", (o["apartment_id"],)))
        if not a or a.get("leased_at") or not a.get("available_from"):
            return False, f"apartment {o['apartment_id']} leased or unavailable"
        # the bed allocation is deterministic for a given member SET (rules.allocate_beds orders the people canonically since 10 Nov
        # 2026); replaying the stored order below is harmless legacy, not what makes an unchanged offer reproduce exactly
        order = []
        for r in (pj(o.get("allocation_json"), {}) or {}).get("rooms") or []:
            for x in r.get("occupants") or []:
                uid = str(x.get("person_id") or "").split("m")[0]
                if uid in by_id and uid not in order:
                    order.append(uid)
        ordered = [by_id[u] for u in order] + [u for u in units if str(u.id) not in order]
        try:
            chk = rules.check_group(matching.apartment_from_row(a), matching.expand(ordered),
                                    exception_offers=matching.exception_offers_for(c, g["id"], a["id"]))
        except Exception as ex:
            log.warning("group %s apartment %s: rule check error (%s)", g.get("id"), o["apartment_id"], matching.run_error_type_token(ex))
            return False, f"apartment {o['apartment_id']}: the rule check could not be completed"
        if not chk.ok:
            return False, f"apartment {o['apartment_id']}: " + "; ".join(chk.reasons)[:240]
        if not _offer_matches(o, chk):
            return False, f"apartment {o['apartment_id']}: the stored offer (dates, rent or bed allocation) no longer matches the rules"
    return True, ""


def _offer_matches(o: dict, chk) -> bool:
    """The persisted offer row equals what rules.check_group produces now (dates, rate, rent, extra bed, who sleeps where)."""
    if ((o.get("exception_ref") or None) != getattr(chk, "exception_ref", None)
            or (o.get("exception_total") or None) != getattr(chk, "exception_total", None)):
        return False
    def num(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return None
    if (str(o.get("lease_start")) != chk.lease_start.isoformat() or str(o.get("lease_end")) != chk.lease_end.isoformat()
            or (o.get("rate_kind") or None) != (chk.rate_kind or None) or num(o.get("total_weekly")) != num(chk.total_weekly)
            or num(o.get("weekly_rent")) != num(chk.weekly_rent) or num(o.get("extra_bed") or 0) != num(chk.extra_bed or 0)):
        return False
    stored = {(str(x.get("person_id")), r.get("room"), x.get("bed"), num(x.get("share")))
              for r in (pj(o.get("allocation_json"), {}) or {}).get("rooms") or [] for x in r.get("occupants") or []}
    fresh = {(str(x.get("person_id")), r.get("room"), x.get("bed"), num(x.get("share"))) for r in chk.allocation or [] for x in r.get("occupants") or []}
    return stored == fresh


def ready_groups(c, dataset: str | None = None) -> list[dict]:
    """Live groups that is_ready() accepts; each row carries 'ready_check'."""
    q = "SELECT g.* FROM groups g WHERE g.status IN (%s)" % ",".join("?" * len(READY_STATUSES))
    args: list = [*READY_STATUSES]
    if dataset is not None:
        q += " AND EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=?)"
        args.append(1 if dataset == "test" else 0)
    out = []
    for g in rows(c.execute(q + " ORDER BY g.id", args)):
        chk = is_ready(c, g)
        if chk["ready"]:
            g["ready_check"] = chk
            out.append(g)
    return out


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def pending_count(c) -> int:
    """Live groups awaiting (or in) a REVIEW. Review-count semantics only; staff-brief work is counted separately (briefs_pending_count)."""
    return c.execute("SELECT COUNT(*) FROM groups WHERE status IN (%s) AND (ai_status IN ('pending','running') OR ai_refresh=1)" % ",".join("?" * len(LIVE)), LIVE).fetchone()[0]


BRIEFS_PER_PASS = AI_BATCH   # D2: standalone staff-brief work per pass (one model call each), so a HomeBot pass still runs between slices


NOTES_PER_PASS = max(1, int(os.environ.get("MMAI_NOTES_PER_PASS", "8") or 8))   # T4c: bounded extraction slice per pass (<= one model call each)
NOTES_FAILED_TEXT = "Rules only (a member's notes could not be read for the review) - retry"   # legacy basis text (rows written before C4); no longer written
NOTES_PENDING_TEXT = "AI assessment pending (reading members' notes first)"
# T3(d) C4 (10 Nov 2026): a member's notes extraction is retried across passes with backoff, at most NOTES_MAX_ROUNDS rounds for the
# same input tuple (each round itself is the bounded in-pass attempt loop). Exhausted -> the group is HELD for staff with a plain
# label (never 'Rules only': a rules-only basis is not a review outcome and is not a fallback); staff retry restarts the budget.
NOTES_MAX_ROUNDS = max(1, int(os.environ.get("MMAI_NOTES_MAX_ROUNDS", "3") or 3))
NOTES_BACKOFF_S = (30, 120, 600)                 # wait before round 2, 3, ... (capped at the last value); plain engineering backoff
NOTES_HELD_BASIS = "Held for staff (a member's notes could not be read)"
NOTES_HELD_LABEL = "A member's notes could not be read after repeated attempts; the team must retry before this group is reviewed"
NOTES_RETRY_SCHEDULED_TEXT = "AI assessment pending (a member's notes will be read again shortly)"
_LIVE_IN = ",".join("?" * 6)


def notes_pending_count(c) -> int:
    """T4c: members of live groups awaiting review whose notes are not yet read (NULL or 'pending'). 'failed' is terminal for the
    worker (staff retry re-queues it), 'ok' / 'none' are done. C4: members of a group HELD because another member's notes could not
    be read (NOTES_HELD_BASIS) are included - each person's notes are processed independently, so the held group never strands an
    unread member; reading them does not review or ready the held group and never retries the terminal failed member."""
    return c.execute("""SELECT COUNT(DISTINCT e.id) FROM enquiries e JOIN group_members gm ON gm.enquiry_id=e.id JOIN groups g ON g.id=gm.group_id
                        WHERE g.status IN (%s) AND (g.ai_status IN ('pending','running') OR g.ai_refresh=1 OR (g.ai_status='held' AND g.ranking_basis=?)) AND COALESCE(e.notes_status,'pending')='pending'""" % ",".join("?" * len(LIVE)), (*LIVE, NOTES_HELD_BASIS)).fetchone()[0]


def notes_scheduled_retry_count(c) -> int:
    """C4: of notes_pending_count, those whose next automatic attempt is scheduled in the future (backoff). They are pending work
    (counted above), not due now."""
    return c.execute("""SELECT COUNT(DISTINCT e.id) FROM enquiries e JOIN group_members gm ON gm.enquiry_id=e.id JOIN groups g ON g.id=gm.group_id
                        WHERE g.status IN (%s) AND (g.ai_status IN ('pending','running') OR g.ai_refresh=1 OR (g.ai_status='held' AND g.ranking_basis=?)) AND COALESCE(e.notes_status,'pending')='pending'
                          AND e.notes_next_retry_at IS NOT NULL AND e.notes_next_retry_at > ?""" % ",".join("?" * len(LIVE)), (*LIVE, NOTES_HELD_BASIS, now_iso())).fetchone()[0]


def _notes_candidates(c, limit: int | None = None, group_ids: list[int] | None = None) -> list[dict]:
    """Members with unread notes: of the given groups (one pass's selection) or of every live group awaiting review (including groups
    held for an unreadable member's notes: the other members are still read independently), oldest first; scheduled retries not yet due are skipped."""
    q = """SELECT DISTINCT e.* FROM enquiries e JOIN group_members gm ON gm.enquiry_id=e.id JOIN groups g ON g.id=gm.group_id
           WHERE g.status IN (%s) AND (g.ai_status IN ('pending','running') OR g.ai_refresh=1 OR (g.ai_status='held' AND g.ranking_basis=?)) AND COALESCE(e.notes_status,'pending')='pending'
             AND (e.notes_next_retry_at IS NULL OR e.notes_next_retry_at <= ?)""" % ",".join("?" * len(LIVE))
    args: list = [*LIVE, NOTES_HELD_BASIS, now_iso()]
    if group_ids is not None:
        if not group_ids:
            return []
        q += " AND g.id IN (%s)" % ",".join("?" * len(group_ids)); args += [int(x) for x in group_ids]
    q += " ORDER BY e.id"
    if limit:
        q += " LIMIT ?"; args.append(int(limit))
    return rows(c.execute(q, args))


def _extract_notes(fields: dict, party_size: int, meter: dict) -> dict:
    """The one provider call of the notes step (tests replace this with controlled replies). Raises ai.AIUnavailable."""
    from . import notes_facts
    return notes_facts._ask(fields, party_size, meter=meter)


def _apply_notes(c, enquiry_id: int, key: str, fields: dict, party_size: int, reply: dict | None, failed: bool, when: str, from_cache: bool) -> str:
    """Inside the guarded apply step - the ONLY place the notes step writes. Re-reads the enquiry: gone -> nothing written anywhere
    (no resurrected identity, no cache row); text or status changed since capture -> this result is for an older tuple, so it is
    dropped and the newer 'pending' stands. Returns the status written ('' when nothing was)."""
    from . import notes_facts
    live = row(c.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    if not live or (live.get("notes_status") or "pending") != "pending":
        return ""
    if notes_facts.cache_key({k: live.get(k) for k in notes_facts.SOURCE_FIELDS}, live.get("party_size") or 0) != key:
        return ""   # the person edited the note while the model was working; the re-queued state already covers the new text
    c.execute("DELETE FROM note_facts WHERE enquiry_id=?", (enquiry_id,))
    if failed or reply is None:
        # C4: one failed ROUND for this input tuple. Rounds are counted per tuple (a changed text starts afresh); below the bound the
        # person stays 'pending' with a scheduled next attempt (backoff) - the group waits, nothing is reviewed without the notes;
        # at the bound the state is 'failed' (closed) and the group is held for staff.
        prev_key = live.get("notes_input_key")
        rounds = (int(live.get("notes_attempts") or 0) if prev_key == key else 0) + 1
        if rounds < NOTES_MAX_ROUNDS:
            wait = NOTES_BACKOFF_S[min(rounds - 1, len(NOTES_BACKOFF_S) - 1)]
            next_at = (datetime.fromisoformat(when) + timedelta(seconds=wait)).isoformat()
            c.execute("UPDATE enquiries SET notes_status='pending', notes_packet_json=NULL, notes_input_key=?, notes_residual=NULL, notes_attempts=?, notes_next_retry_at=?, notes_updated_at=? WHERE id=?",
                      (key, rounds, next_at, when, enquiry_id))
            return "retry_scheduled"
        c.execute("UPDATE enquiries SET notes_status='failed', notes_packet_json=NULL, notes_input_key=?, notes_residual=NULL, notes_attempts=?, notes_next_retry_at=NULL, notes_updated_at=? WHERE id=?",
                  (key, rounds, when, enquiry_id))
        return "failed"
    v = notes_facts.validate_reply(reply, fields, party_size)   # per-person (re)validation, cache hit or not
    for it in v["staff_items"]:
        c.execute("""INSERT INTO note_facts(enquiry_id, field, category, polarity, strength, subject, span, start_offset, end_offset, value_json, attribution, validator_status, rejection_code, model, catalogue_version, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (enquiry_id, it["field"], it["category"], it["polarity"], it["strength"], it["subject"], it["span"], it["start"], it["end"], j(it.get("value")),
                   it.get("attribution"), "accepted", None, ai.MODEL, notes_facts.CATALOGUE_VERSION, when))
    for it in v["rejected"]:   # closed code only: no span, no model words (the validator's sentence stays in process)
        c.execute("""INSERT INTO note_facts(enquiry_id, field, category, validator_status, rejection_code, model, catalogue_version, created_at) VALUES (?,?,?,?,?,?,?,?)""",
                  (enquiry_id, it.get("field") or "", it.get("category"), "rejected", it["code"], ai.MODEL, notes_facts.CATALOGUE_VERSION, when))
    c.execute("UPDATE enquiries SET notes_status='ok', notes_packet_json=?, notes_input_key=?, notes_residual=?, notes_attempts=0, notes_next_retry_at=NULL, notes_updated_at=? WHERE id=?",
              (j(v["packet_items"]), key, 1 if v["residual"] else 0, when, enquiry_id))
    if not from_cache:
        # owned, sanitised cache row: accepted items only (verbatim spans of the OWNER's own text), residual flag; no raw reply
        c.execute("DELETE FROM note_extraction_cache WHERE enquiry_id=?", (enquiry_id,))
        c.execute("INSERT INTO note_extraction_cache(input_key, enquiry_id, accepted_json, residual, model, catalogue_version, created_at) VALUES (?,?,?,?,?,?,?)",
                  (key, enquiry_id, j(notes_facts.cache_reply(v)), 1 if v["residual"] else 0, ai.MODEL, notes_facts.CATALOGUE_VERSION, when))
    return "ok"


def process_notes(c, limit: int | None = NOTES_PER_PASS, attempts: int = AI_ATTEMPTS, group_ids: list[int] | None = None) -> dict:
    """T4c v2: read the free text of members whose group awaits review, at most `limit` people per pass and at most one bounded
    model call per distinct input tuple not already in the owned cache (hash of the exact tuple). Blank text -> 'none' with no
    call. Failure -> 'failed' (closed state; staff retry). T5 shape: read -> call (no guard, nothing written) -> guarded apply
    (identity / current-text checks, then every write: facts, status, cache)."""
    from . import notes_facts
    out = {"people": 0, "none": 0, "ok": 0, "failed": 0, "retry_scheduled": 0, "calls": 0, "cached": 0, "distinct_tuples": 0}
    seen_keys = set()
    for e in _notes_candidates(c, limit, group_ids):
        out["people"] += 1
        when = now_iso()
        fields = {k: e.get(k) for k in notes_facts.SOURCE_FIELDS}
        party = int(e.get("party_size") or 0)
        key = notes_facts.cache_key(fields, party)
        reply, failed, from_cache = None, False, False
        if notes_facts.has_text(fields):
            cached = row(c.execute("SELECT accepted_json, residual FROM note_extraction_cache WHERE input_key=? ORDER BY id DESC LIMIT 1", (key,)))
            if cached:
                reply = {"items": pj(cached["accepted_json"], []) or [], "residual": bool(cached["residual"])}
                from_cache = True
                out["cached"] += 1
            else:
                if key not in seen_keys:
                    out["distinct_tuples"] += 1
                seen_keys.add(key)
                db.assert_not_guarded(c, "model call")
                meter = {"asks": 0}
                for attempt in range(max(1, attempts)):
                    m0 = meter["asks"]
                    try:
                        reply = _extract_notes(fields, party, meter)
                        failed = False
                        break
                    except ai.AINoRetry:
                        failed = True
                        break
                    except ai.AIUnavailable as ex:
                        failed = True
                        if _no_point_retrying(ex) or isinstance(ex, ai.AIUnusableReply):
                            break
                        time.sleep(min(20.0, 1.5 * (2 ** attempt)))
                    except Exception as ex:  # pragma: no cover
                        log.warning("notes extraction failed: unexpected (%s)", _tok(ex))
                        failed = True
                        time.sleep(1.0)
                    finally:
                        out["calls"] += max(1, meter["asks"] - m0)   # actual calls this attempt (compact retry inside ai counted)
        try:
            with _step(c, label="notes apply"):
                if not notes_facts.has_text(fields):
                    st = _apply_blank_notes(c, e["id"], key, when)
                else:
                    st = _apply_notes(c, e["id"], key, fields, party, reply, failed, when, from_cache)
        except GUARD_FAILURES as ex:
            log.warning("enquiry %s: notes result not recorded (%s); left pending", e.get("id"), _tok(ex))
            st = ""
        if st in ("ok", "failed", "none", "retry_scheduled"):
            out[st] += 1
        maybe_commit(c)
    return out


def _apply_blank_notes(c, enquiry_id: int, key: str, when: str) -> str:
    from . import notes_facts
    live = row(c.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    if not live or (live.get("notes_status") or "pending") != "pending" or notes_facts.has_text({k: live.get(k) for k in notes_facts.SOURCE_FIELDS}):
        return ""
    c.execute("DELETE FROM note_facts WHERE enquiry_id=?", (enquiry_id,))
    c.execute("DELETE FROM note_extraction_cache WHERE enquiry_id=?", (enquiry_id,))
    c.execute("UPDATE enquiries SET notes_status='none', notes_packet_json='[]', notes_input_key=NULL, notes_residual=0, notes_updated_at=? WHERE id=?", (when, enquiry_id))
    return "none"


def _notes_gate_groups(c, gs: list[dict]) -> list[dict]:
    """Groups whose members' notes are all read go on to review. A member still pending keeps the group pending (fail closed, with a
    plain basis); a member whose extraction failed makes the group 'failed' with a closed sentence (staff retry re-queues both)."""
    ready = []
    for g in gs:
        sts = [r["s"] for r in rows(c.execute("SELECT COALESCE(e.notes_status,'pending') AS s FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=?", (g["id"],)))]
        if any(s == "failed" for s in sts):
            # C4: the bounded re-extraction rounds are exhausted for a member: the group is HELD for staff (plain label, closed basis);
            # no review runs without the notes and no rules-only basis stands in for one. Staff retry (retry_failed) restarts the budget.
            c.execute("UPDATE groups SET ai_status='held', ranking_basis=?, held_reason=?, ai_error=NULL, ai_refresh=0, ready_at=NULL, brief_status=NULL, updated_at=? WHERE id=?",
                      (NOTES_HELD_BASIS, NOTES_HELD_LABEL, now_iso(), g["id"]))
        elif any(s not in ("ok", "none") for s in sts):
            scheduled = c.execute("SELECT COUNT(*) FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND COALESCE(e.notes_status,'pending')='pending' AND e.notes_next_retry_at IS NOT NULL AND e.notes_next_retry_at > ?",
                                  (g["id"], now_iso())).fetchone()[0]
            c.execute("UPDATE groups SET ranking_basis=? WHERE id=? AND ai_status='pending'", (NOTES_RETRY_SCHEDULED_TEXT if scheduled else NOTES_PENDING_TEXT, g["id"]))
        elif outdated_notes_members(c, g["id"]):
            # fix 2 currentness: an accepted projection made under an earlier extraction contract is withdrawn to 'pending' here
            # too (the worker is the single writer), so the next notes step re-reads it before this group is reviewed
            withdraw_outdated_notes(c, now_iso())
            c.execute("UPDATE groups SET ranking_basis=? WHERE id=? AND ai_status='pending'", (NOTES_PENDING_TEXT, g["id"]))
        else:
            ready.append(g)
    return ready


def briefs_pending_count(c) -> int:
    """D2 (9 Nov 2026): approved live groups whose staff brief is still pending (interrupted before it landed, or a guarded step
    re-queued it). Only 'pending' counts: a 'failed' brief is terminal for the worker (staff retry), 'stale' is handled by the review path."""
    return c.execute("SELECT COUNT(*) FROM groups WHERE status IN (%s) AND ai_status='ok' AND brief_status='pending'" % ",".join("?" * len(LIVE)), LIVE).fetchone()[0]


RESELECT_RETRIES = 2   # bounded immediate retries of the end-of-pass reselection after a guard / compare-and-swap failure


def _reselect_after_pass(c) -> dict:
    """T3(d) C2(b): synchronous post-AI reselection of proposals at the END of a queue pass, per data set (review_loop.reselect_primaries:
    one guarded step each, no provider call, adds no pending work type so the drain counters keep their meaning). A guard failure or
    an abort because the groups changed under the step is retried at most RESELECT_RETRIES times; after that the result carries
    `settled: False` with a plain reason - the caller must NOT report the pass as settled, and the next pass reselects again.
    Nothing is written on a failed attempt (the step is one guarded transaction)."""
    _bump(reselect_unsettled=True)   # marked BEFORE the attempt: an exception below leaves the signal set, never a false 'drained'
    try:
        return _reselect_attempts(c)
    except Exception as ex:   # unexpected: closed reason only, the signal stays set and the next wake / pass retries
        log.warning("post-AI reselection did not complete (%s)", _tok(ex))
        res = {label: {"changed": 0, "gained": [], "lost": [], "truncated": 0, "settled": False, "reason": RESELECT_INCOMPLETE} for label in ("real", "test")}
        _bump(reselect_unsettled=True, reselect_last=_reselect_summary(res))
        return res


def _reselect_summary(res: dict) -> dict:
    return {"at": now_iso(), "settled": all(bool(v) and v.get("settled") for v in res.values()),
            **{label: {"settled": bool(v.get("settled")), "changed": int(v.get("changed") or 0), "promoted": len(v.get("promoted") or []),
                       "promote_failed": len(v.get("promote_failed") or []), "reason": v.get("reason")} for label, v in res.items()}}


def _reselect_attempts(c) -> dict:
    from . import review_loop
    res = {}
    for is_test, label in ((0, "real"), (1, "test")):
        last = None
        for attempt in range(1 + RESELECT_RETRIES):
            try:
                r = review_loop.reselect_primaries(c, is_test)
            except GUARD_FAILURES as ex:
                log.warning("post-AI reselection attempt %d failed for the %s data set (%s)", attempt + 1, label, _tok(ex))
                last = {"changed": 0, "gained": [], "lost": [], "truncated": 0, "settled": False, "reason": "database busy or connection lost; reselected next pass"}
                continue
            if r.get("reason") in ("groups changed under us", "group changed under us"):
                last = dict(r, settled=False)
                continue
            if r.get("promote_failed"):   # the step rolled back because a handoff did not complete: not settled, nothing moved
                last = dict(r, settled=False)
                continue
            last = dict(r, settled=not r.get("settle_after_promote"))
            if not last["settled"]:
                last["reason"] = r.get("settle_after_promote")
            break
        res[label] = last
    summary = _reselect_summary(res)
    _bump(reselect_unsettled=not summary["settled"], reselect_last=summary)
    return res


def process_pending(c, max_batches: int | None = None, batch_size: int = AI_BATCH, workers: int = AI_WORKERS, attempts: int = AI_ATTEMPTS,
                    notes_limit: int | None = 0) -> dict:
    """One pass: claim every pending group (priority order), assess in parallel batches, persist. Synchronous; the worker thread
    and the tests both use it. Returns counts for this pass."""
    with _lock:
        if STATE["busy"]:
            return {"skipped": True}
        STATE["busy"] = True
        STATE["reselect_unsettled"] = True   # a pass OWES a reselection from here: if it throws before reaching it, the earlier settled result is no certificate
    t0 = time.time()
    out = {"groups": 0, "ok": 0, "failed": 0, "calls": 0, "batches": 0, "skipped_dead": 0}
    try:
        when = now_iso()
        gs = rows(c.execute("""SELECT * FROM groups WHERE status IN (%s) AND (ai_status='pending' OR ai_refresh=1)
                               ORDER BY COALESCE(ai_priority, 1), is_primary DESC, COALESCE(ai_queued_at, created_at), id""" % ",".join("?" * len(LIVE)), LIVE))
        if max_batches:
            gs = gs[: max_batches * batch_size]
        # C4: groups HELD because one member's notes could not be read still have members whose notes are pending independently;
        # those people are read in the same bounded slice (never reviewing the held group, never retrying its failed member)
        notes_held = [int(r["id"]) for r in rows(c.execute("SELECT id FROM groups WHERE status IN (%s) AND ai_status='held' AND ranking_basis=?" % ",".join("?" * len(LIVE)), (*LIVE, NOTES_HELD_BASIS)))]
        if gs:
            # T4c: the notes of the members of THIS pass's groups are read first (one bounded call per distinct input tuple, cached;
            # counted apart from review calls), then a group whose members' notes are all read goes on to review
            # the worker passes notes_limit=NOTES_PER_PASS (bounded slice, HomeBot pass between slices); the direct staff/test entry
            # (notes_limit=0) reads every member of its selection
            n = process_notes(c, limit=(notes_limit or None), group_ids=[g["id"] for g in gs] + notes_held, attempts=attempts)
            if n["people"]:
                out["notes"] = n
            gs = _notes_gate_groups(c, gs)
            maybe_commit(c)
        elif notes_held and notes_pending_count(c) > 0:
            n = process_notes(c, limit=(notes_limit or None), group_ids=notes_held, attempts=attempts)
            if n["people"]:
                out["notes"] = n
            maybe_commit(c)
        if not gs:
            if out.get("notes", {}).get("people"):
                out["groups"] = 0   # notes work happened; the worker's remaining-work signal (notes_pending_count) re-wakes it
                _bump(last_finished_at=now_iso())
            # D2: no review work, but an approved group may still be waiting for its staff brief (interrupted earlier); the brief
            # step runs on its own, without any review being repeated
            if briefs_pending_count(c) > 0:
                b = process_briefs(c, attempts=attempts, limit=BRIEFS_PER_PASS)   # bounded slice; the worker's remaining-work signal re-wakes for the rest
                out["briefs_ok"], out["briefs_failed"], out["brief_calls"] = b["ok"], b["failed"], b["calls"]
                out["groups"] = b["groups"]   # work was done this pass (the worker loop treats 0 as 'nothing to do')
                _bump(last_finished_at=now_iso())
            out["reselect"] = _reselect_after_pass(c)
            out["settled"] = out["reselect"] and all(bool(v) and v.get("settled") for v in out["reselect"].values())   # False: the next pass reselects again
            out["duration_s"] = round(time.time() - t0, 1)
            return out
        ids = [g["id"] for g in gs]
        c.execute("UPDATE groups SET ai_status=CASE WHEN ai_status='ok' THEN 'ok' ELSE 'running' END, ai_queued_at=COALESCE(ai_queued_at, ?) WHERE id IN (%s)" % ",".join("?" * len(ids)), (when, *ids))
        maybe_commit(c)
        payloads = []
        for g in gs:
            # T5/T6 capture: one guarded step per group reads every row once (packet + hash from the same snapshot) and refreshes the
            # rule ranks; a deleter or editor on another connection waits for the step or finishes before it, never during it
            try:
                with _step(c, label="review capture"):
                    try:
                        p, drift = _payload(c, g), False
                    except OfferDrift:
                        p, drift = None, True
                        c.execute("UPDATE groups SET ai_status='failed', ranking_basis='Rules only (offer no longer matches)', ai_error=?, ai_refresh=0, ready_at=NULL, updated_at=? WHERE id=?",
                                  (OFFER_DRIFT_REASON, now_iso(), g["id"]))
                    if p is None and not drift:
                        c.execute("UPDATE groups SET ai_status='failed', ranking_basis='Rules only (AI failed)', ai_error=?, ai_refresh=0 WHERE id=?", (UNREADABLE_RECORD_TEXT, g["id"]))
            except GUARD_FAILURES as ex:
                log.warning("group %s: review capture not completed (%s)", g.get("id"), _tok(ex))
                _requeue_after_guard_failure(c, g, "review")
                out["skipped_dead"] += 1
                continue
            if p is None:
                out["skipped_dead"] += 1
                continue
            payloads.append((g, p))
        db.assert_not_guarded(c, "model call")   # the model is never called while a retention guard is held
        batches = pack_batches(payloads, batch_size)
        _bump(last_started_at=now_iso(), pass_total=len(batches), pass_done=-STATE["pass_done"], in_flight=len(batches))
        out["groups"] = len(payloads)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            def timed(payload):
                t = time.time()
                r = _assess_batch(payload, attempts)
                return (*r, round(time.time() - t, 1))
            futs = {ex.submit(timed, [p for _, p in b]): b for b in batches}
            for fut in as_completed(futs):
                b = futs[fut]
                batch_exc = None
                try:
                    res, err, calls, secs = fut.result()
                except Exception as e:  # pragma: no cover
                    res, err, calls, secs, batch_exc = None, _failure(e), 1, 0.0, e
                out["calls"] += calls
                out["batches"] += 1
                by_idx = {r["index"]: r for r in (res or [])}
                for idx, (g, p) in enumerate(b):
                    # T5: one guarded step PER GROUP - identity checks, history insert and group update commit together or not at all
                    try:
                        with _step(c, label="review apply"):
                            ok = _apply_result(c, g, p, by_idx.get(idx) if res is not None else None, err, calls)
                    except GUARD_FAILURES as ex:
                        log.warning("group %s: review result not recorded (%s); re-queued", g.get("id"), _tok(ex))
                        _requeue_after_guard_failure(c, g, "review")
                        ok = False
                    out["ok" if ok else "failed"] += 1
                _bump(calls=calls, failed_calls=0 if res is not None else 1, batches_done=1, groups_assessed=sum(1 for idx in range(len(b)) if by_idx.get(idx)) if res is not None else 0,
                      groups_failed=len(b) if res is None else sum(1 for idx in range(len(b)) if not by_idx.get(idx)), in_flight=-1, pass_done=1,
                      last_error=state_error_category(err, batch_exc) if res is None else STATE["last_error"], last_batch_s=secs,
                      last_error_at=now_iso() if res is None else STATE.get("last_error_at"))
        # T4(f): approved groups get their staff briefs before they can be ready; revisions are promoted only after that
        b = process_briefs(c, attempts=attempts)
        out["briefs_ok"], out["briefs_failed"], out["brief_calls"] = b["ok"], b["failed"], b["calls"]   # `calls` stays the review-call count
        out["reselect"] = _reselect_after_pass(c)
        out["settled"] = out["reselect"] and all(bool(v) and v.get("settled") for v in out["reselect"].values())   # False: the next pass reselects again
        _bump(last_finished_at=now_iso())
        out["duration_s"] = round(time.time() - t0, 1)
        return out
    finally:
        with _lock:
            STATE["busy"] = False
            STATE["in_flight"] = 0


# ---------------------------------------------------------------------------
# Staff briefs (T4(f)): one per member plus one for the group, for every approved review
# ---------------------------------------------------------------------------
def _ref_identities(c, g: dict) -> dict[str, dict]:
    """ref -> {enquiry_id, party_member_id|None, individually_recorded: bool}. Party members share the enquiry row; the ref,
    enquiry id and party_members.id together make each person unambiguous."""
    from . import matching
    ids = [int(x) for x in (g.get("member_key") or "").split(",") if x.strip().isdigit()]
    out: dict[str, dict] = {}
    units = []
    for eid in ids:
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        if e:
            pm = rows(c.execute("SELECT id, name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))
            try:
                units.append((matching.unit_from_row(e, pm), pm))
            except Exception:
                continue
    uids = live_uids(c, ids)
    for i, (u, pm) in enumerate(units, start=1):
        k = 0
        for p in u.people:
            if p.is_lead:
                # a promoted lead whose row still carries the former enquirer's individual answers has, for the brief, no personal facts of their own
                out[f"U{i}"] = {"enquiry_id": int(u.id), "party_member_id": None, "individually_recorded": not matching.lead_answers_unverified(u.extra),
                                "person_uid": uids.get((int(u.id), None))}
            else:
                k += 1
                pmid = pm[k - 1]["id"] if k - 1 < len(pm) else None
                out[f"U{i}-m{k}"] = {"enquiry_id": int(u.id), "party_member_id": pmid, "individually_recorded": False,
                                     "person_uid": uids.get((int(u.id), pmid)) if pmid is not None else None}
    return out


# ---------------------------------------------------------------------------
# GP2 customer introduction: identity payload (separate from the compatibility packet) and draft state
# ---------------------------------------------------------------------------
def _intro_sex_display(v, other) -> str | None:
    """Fresh introduction-only term exactly as supplied: 'Another term' shows the person's own words; 'Prefer not to say' shows nothing."""
    if not v or v == "Prefer not to say":
        return None
    if v == "Another term":
        return (other or "").strip() or None
    return str(v)


INTRO_LIFESTYLE = ("diet", "social", "routine")   # GP2 standard intro content when supplied: diet, social style, sleep pattern (the 'routine' answer)


def _person_shares(c, g: dict) -> dict[str, dict]:
    """person_id ('12' / '12m1') -> {share, room, bed, arrangement} from the stored allocation of the group's current apartment."""
    o = row(c.execute("SELECT allocation_json FROM group_apartments WHERE group_id=? AND apartment_id=?", (g["id"], g.get("apartment_id"))))
    out = {}
    for r in (pj((o or {}).get("allocation_json"), {}) or {}).get("rooms") or []:
        for x in r.get("occupants") or []:
            out[str(x.get("person_id"))] = {"share": x.get("share"), "room": r.get("room"), "bed": x.get("bed"), "arrangement": x.get("arrangement")}
    return out


# HomeBot H7/H14 hooks (lead slice, 9 Nov 2026): the shortlist module looks these two names up on ai_queue; the implementation
# lives in server/homebot_review.py (grounded review queue with its own closed states and failure sentences).
def enqueue_homebot_review(c, shortlist_id: int, commit: bool = False) -> dict:
    from . import homebot_review
    return homebot_review.enqueue_homebot_review(c, shortlist_id, commit=commit)


def homebot_review_state(c, shortlist_id: int) -> dict:
    from . import homebot_review
    return homebot_review.homebot_review_state(c, shortlist_id)


def intro_members(c, gid: int) -> list[dict]:
    """GP2 identity payload for ai.draft_introduction / fallback_introduction: ONE DICT PER REAL PERSON (lead I1, party members
    I1-m1, I1-m2, next lead I2 ...), each carrying ONLY the values ALM holds for THAT person, verbatim (Bill, 8 Nov 2026):
      lead: first name, country given, study/work answers, own diet / social / sleep-pattern answers, own intro text, fresh
            intro_sex / intro_age, own email + mobile (contact_method says whether the mobile is WhatsApp) when consent 6.3 is
            given, guide share / room from the stored allocation;
      party member: own name, own email (the lead's consent covers the party's emails), own country, fresh intro_sex / intro_age,
            own share / room; NOTHING inherited (no study/work, lifestyle, intro text or phone - ALM holds none for them).
    Missing intro details are flagged per person in `intro_missing` and never invented. Separate from the compatibility packet."""
    from . import matching
    g = row(c.execute("SELECT id, apartment_id FROM groups WHERE id=?", (gid,)))
    if not g:
        return []
    shares = _person_shares(c, g)
    out = []
    for i, m in enumerate(rows(c.execute("""SELECT gm.enquiry_id, gm.weekly_share, gm.room, gm.bed, gm.arrangement, e.* FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id
                                             WHERE gm.group_id=? ORDER BY gm.enquiry_id""", (gid,))), start=1):
        ref = f"I{i}"
        consent = bool(m.get("consent_share"))
        alloc = shares.get(str(m["enquiry_id"]), {})
        item = {"ref": ref, "enquiry_id": m["enquiry_id"], "party_member_id": None, "is_lead": True, "lead_ref": ref,
                "name": m.get("first_name"), "first_name": m.get("first_name"), "country_of_birth": m.get("birth_country") or m.get("birth_country_raw"),   # 1.8 "Country of birth" - never nationality/citizenship/home country
                "doing": m.get("doing"), "school": m.get("school"), "course": m.get("course"), "intro": m.get("intro"),
                **{k: m.get(k) for k in INTRO_LIFESTYLE},
                "intro_sex": _intro_sex_display(m.get("intro_sex"), m.get("intro_sex_other")), "intro_age": m.get("intro_age"),
                "weekly_share": alloc.get("share", m.get("weekly_share")), "room": alloc.get("room", m.get("room")), "arrangement": alloc.get("arrangement", m.get("arrangement")),
                "contact_method": m.get("contact_method"), "email": None, "phone": None, "whatsapp": False, "intro_missing": []}
        if consent:
            item["email"] = m.get("email")
            # a dialling code alone is no contact detail: without a recorded number nothing is offered (a person who took over an
            # enquiry has no phone on file until they give one)
            phone = f"{m.get('phone_code') or ''} {m.get('phone') or ''}".strip() if str(m.get("phone") or "").strip() else ""
            if phone:
                item["phone"] = phone
                item["whatsapp"] = m.get("contact_method") == "WhatsApp"
                if not item["whatsapp"]:
                    item["intro_missing"].append("WhatsApp not confirmed (best contact method is %s)" % (m.get("contact_method") or "not stated"))
        else:
            item["note"] = "has not consented to sharing contact details (6.3); ALM will relay messages"
        if matching.lead_answers_unverified(m):   # takeover: the former enquirer's study/work, lifestyle and intro text are not this person's
            item.update({"doing": None, "school": None, "course": None, "intro": None, **{k: None for k in INTRO_LIFESTYLE}})
            item["intro_missing"].append(matching.LEAD_ANSWERS_UNVERIFIED_TEXT)
        for k, label in (("intro_sex", "sex"), ("intro_age", "age"), ("country_of_birth", "country of birth"), ("diet", "diet"), ("social", "social style"), ("routine", "sleep pattern")):
            if item.get(k) in (None, ""):
                item["intro_missing"].append(f"{label} not supplied for {item['first_name']}")
        out.append(item)
        for k, pm in enumerate(rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (m["enquiry_id"],))), start=1):
            pid = f"{m['enquiry_id']}m{k}"
            palloc = shares.get(pid, {})
            d = {"ref": f"{ref}-m{k}", "enquiry_id": m["enquiry_id"], "party_member_id": pm["id"], "is_lead": False, "lead_ref": ref,
                 "name": pm.get("name"), "first_name": (pm.get("name") or "").split(" ")[0] or pm.get("name"),
                 "country_of_birth": pm.get("birth_country") or pm.get("birth_country_raw"),
                 "doing": None, "school": None, "course": None, "intro": None, **{x: None for x in INTRO_LIFESTYLE},
                 "intro_sex": _intro_sex_display(pm.get("intro_sex"), pm.get("intro_sex_other")), "intro_age": pm.get("intro_age"),
                 "weekly_share": palloc.get("share"), "room": palloc.get("room"), "arrangement": palloc.get("arrangement"),
                 "contact_method": None, "email": pm.get("email") if consent else None, "phone": None, "whatsapp": False, "intro_missing": []}
            if not consent:
                d["note"] = f"party of {item['first_name']}, who has not consented to sharing contact details (6.3)"
            for kk, label in (("intro_sex", "sex"), ("intro_age", "age"), ("country_of_birth", "country of birth")):
                if d.get(kk) in (None, ""):
                    d["intro_missing"].append(f"{label} not supplied for {d['name']}")
            d["intro_missing"].append(f"study/work, lifestyle answers and mobile are not recorded for {d['name']} (party member of {item['first_name']})")
            out.append(d)
    return out


def intro_basis(c, gid: int) -> list[str]:
    """Compatibility shim (pre-r7 callers): the rendered text of the current, verifiable basis items (see intro_basis_items).
    Returns [] when the stored brief has no r7 provenance metadata - the caller then has no attributable basis."""
    items = intro_basis_items(c, gid, (row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,))) or {}).get("apartment_id"))
    return [f"{x['section']}: {x['text']}" for x in (items or [])]


INTRO_PROFILE_FIELDS = ("ref", "enquiry_id", "party_member_id", "is_lead", "lead_ref", "name", "first_name", "country_of_birth", "doing", "school", "course", "intro",
                        *INTRO_LIFESTYLE, "intro_sex", "intro_age", "weekly_share", "room", "arrangement", "contact_method", "email", "phone", "whatsapp", "note")


def intro_profile_hash(members: list[dict]) -> str:
    """Canonical fingerprint of the per-person INTRODUCTION facts (identity, contact/consent, lifestyle, guide share) as handed to
    the drafter. Separate from the compatibility evidence hash on purpose: intro-only fields never enter compatibility evidence,
    and a compatibility hash cannot tell that someone's intro sex/age/contact changed while a draft was being written."""
    canon = sorted(({k: m.get(k) for k in INTRO_PROFILE_FIELDS} for m in members), key=lambda d: str(d.get("ref")))
    return hashlib.sha256(j(canon).encode("utf-8")).hexdigest()


def intro_profile_changed(c, enquiry_id: int, who: str | None = None) -> list[int]:
    """Identity-only edit (intro_sex / intro_age / intro text / contact): the compatibility evidence is unchanged, so no re-review
    and no re-rank; but every live group containing the person gets its CURRENT customer introduction draft marked stale (a
    group_briefs row kind='intro', status='stale'; earlier draft text is kept as history). Returns the group ids touched."""
    when = now_iso()
    touched = []
    for g in rows(c.execute("""SELECT g.id, g.intro_text FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status IN (%s)""" % ",".join("?" * len(LIVE)),
                            (enquiry_id, *LIVE))):
        c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (g["id"], 0, "intro", None, enquiry_id, None, j({"reason": "introduction details edited", "by": who}), "stale", None, None,
                                                            rules.RULE_VERSION, None, when))
        touched.append(g["id"])
    if touched:
        maybe_commit(c)
    return touched


INTRO_STATUS = {"AI": "ok", "staff": "edited"}   # anything else (template fallback) is 'template' and never counts as the AI draft

# ---------------------------------------------------------------------------------------------------------------------------
# r7 (root work order 8 Nov 2026, reviews/20261108-r7-implementation-contract.md): exact offer context and immutable basis
# provenance for the customer introduction.
#   * intro_offer_projection: PURE read of the stored selected offer + members + profile facts (plain SELECTs; never _payload,
#     never a refresh of stored terms, no enqueue/rank/write/commit). intro_state rests on it, so it stays side-effect free.
#   * intro_offer_fingerprint: sha256 over the EXACT apt + ginfo objects handed to the model (not a later re-read).
#   * intro_capture: ONE guarded read step giving the draft route everything the model sees plus the eligibility facts; raises
#     IntroNotCapturable with a closed reason code (the app maps codes to fixed sentences).
#   * intro_basis_items: the brief notes the fit prose may rest on, read ONLY from provenance stored with the brief at its own
#     guarded apply (_write_briefs); subjects are remapped to the CURRENT intro people by immutable ids, apartment notes are
#     re-verified field by field against the current offer projection, anything unverifiable is omitted, legacy = None.
#   * record_intro_draft(capture=...): refuses the draft when the fresh projection differs from the captured context.
#   * intro_state: additive keys (draft_id = stable CAS token, offer_current, profile_current, apartment_id, basis_scope,
#     context_hash); a legacy draft without offer context is never current. GP8 compatibility readiness is a separate contract.
# ---------------------------------------------------------------------------------------------------------------------------

INTRO_CAPTURE_CODES = ("group_gone", "no_offer", "no_members", "brief_not_ok", "not_circulable", "legacy_basis")
INTRO_REFUSAL_APARTMENT = "introduction discarded: the group's apartment changed while it was being drafted"
INTRO_REFUSAL_OFFER = "introduction discarded: the apartment or offer changed while it was being drafted"
INTRO_REFUSAL_PROFILE = "introduction discarded: a person's introduction details changed while it was being drafted"
INTRO_REFUSAL_NO_OFFER = "introduction discarded: the group has no stored offer for its apartment"
INTRO_REFUSAL_NO_IDENTITIES = "introduction discarded: the people it describes were not identified"
INTRO_REFUSAL_CONTEXT = "introduction discarded: the group's compatibility facts or membership changed while it was being drafted"
INTRO_STALE_FACTS = "drafted before the facts changed"
INTRO_STALE_OFFER = "drafted for a different apartment or offer"
INTRO_STALE_NO_CONTEXT = "drafted before offer provenance was recorded"
INTRO_STALE_PROFILE = "drafted before someone's introduction details changed"
INTRO_STALE_TEMPLATE = "template draft is not an AI introduction"
INTRO_STALE_NONE = "no introduction drafted"
INTRO_STALE_CONTRACT = "drafted under an earlier introduction wording check; draft it again"   # succ3 (10 Nov 2026): ai.INTRO_PROSE_CONTRACT moved
BASIS_META_VERSION = 1


class IntroNotCapturable(Exception):
    """The introduction cannot be drafted for this group right now. `code` is one of INTRO_CAPTURE_CODES (static, mapped to
    fixed staff sentences by the app); `details` carries the central readiness reasons when code == 'not_circulable'."""

    def __init__(self, code: str, details: list[str] | None = None):
        assert code in INTRO_CAPTURE_CODES, code
        super().__init__(code)
        self.code = code
        self.details = list(details or [])


def _canon_hash(obj) -> str:
    import json
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def intro_offer_fingerprint(apt: dict, ginfo: dict) -> str:
    """Fingerprint of the EXACT apartment + offer objects given to the drafter (every field participates; nothing is selected
    out, so a new displayed field automatically changes the fingerprint)."""
    return _canon_hash({"apt": apt, "ginfo": ginfo})


def intro_offer_projection(c, gid: int) -> dict | None:
    """PURE read-only projection of the group's selected offer as the introduction shows it: the stored group_apartments row for
    groups.apartment_id (as stored - never recomputed or refreshed here), the apartment's listing facts and displayed feature
    states, the members' introduction facts (intro_members) and both fingerprints. None when the group, its apartment or the
    stored offer row is missing. Plain SELECTs only: no _payload, no enqueue, no rank or term refresh, no write, no commit."""
    from . import apartment_features as feat
    g = row(c.execute("SELECT id, status, member_key, evidence_hash, apartment_id, brief_status FROM groups WHERE id=?", (gid,)))
    if not g or not g.get("apartment_id"):
        return None
    ga = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, g["apartment_id"])))
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    if not ga or not a:
        return None
    members = intro_members(c, gid)
    code = rules.normalise_type(a.get("type_code"))
    states = feat.apartment_features(a)
    apt = {"id": a["id"], "address": a.get("address"), "type_code": a.get("type_code"), "type_label": rules.APT_TYPES[code]["label"], "suburb": a.get("suburb"),
           "bills_included": bool(a.get("bills_included")),
           "features": [{"key": k, "label": feat.LABEL_OF[k], "recorded": states[k]} for k in feat.KEYS]}
    alloc = pj(ga.get("allocation_json"), {}) or {}
    rooms = [{"room": r.get("room"), "setup": r.get("setup"), "room_cost": r.get("room_cost"), "extra_bed": r.get("extra_bed", 0),
              "occupants": [{"person_id": str(x.get("person_id")), "share": x.get("share"), "bed": x.get("bed"), "arrangement": x.get("arrangement")} for x in (r.get("occupants") or [])]}
             for r in (alloc.get("rooms") or [])]
    ginfo = {"lease_start": ga.get("lease_start"), "lease_end": ga.get("lease_end"), "rate_kind": ga.get("rate_kind"),
             "rate": "6-month rate" if ga.get("rate_kind") == "6m" else "12-month rate", "exit_window": ga.get("exit_window"),
             "weekly_rent": ga.get("weekly_rent"), "total_weekly": ga.get("total_weekly"), "extra_bed_charge": ga.get("extra_bed"),
             "liability_note": rules.liability_note(ga.get("total_weekly") or 0), "rooms": rooms}
    return {"gid": gid, "status": g.get("status"), "member_key": g.get("member_key"), "evidence_hash": g.get("evidence_hash"), "brief_status": g.get("brief_status"),
            "apartment_id": g["apartment_id"], "members": members, "apt": apt, "ginfo": ginfo,
            "profile_hash": intro_profile_hash(members), "offer_fingerprint": intro_offer_fingerprint(apt, ginfo)}


def intro_context_hash(proj: dict) -> str:
    """One token for 'the context this draft was written for': profile + exact offer + compatibility evidence + membership."""
    return _canon_hash([proj.get("profile_hash"), proj.get("offer_fingerprint"), proj.get("evidence_hash"), proj.get("member_key"), proj.get("apartment_id")])


INTRO_HABIT_FIELDS = ("tidy", "social")   # the graded lifestyle answers ai.HABIT_DEGREES checks a stated degree against (succ4 scope: tidiness, social style)


def intro_habit_answers(g: dict, members: list[dict]) -> dict[str, dict]:
    """succ4 (10 Nov 2026): PRIVATE validation context for ai.habit_claim_problems - intro ref -> {tidy, social} as
    RECORDED in the group's captured compatibility evidence (groups.evidence_json, written atomically with evidence_hash, which the
    approved brief, intro_basis_items and the draft's context_hash are all bound to). So the values compared are exactly the evidence
    the current approved basis rests on, not a fresh read and not a rebuilt history. Leads only (a party member has no own lifestyle
    answers); a takeover lead (lead_answers_unverified_at) has none. Never written, never part of the prompt, bios or the intro payload:
    intro_members / INTRO_BIO_KEYS / INTRO_LIFESTYLE are unchanged."""
    snap = pj((g or {}).get("evidence_json"), {}) or {}
    by_enquiry = {m.get("enquiry_id"): (m.get("facts") or {}) for m in snap.get("members") or [] if isinstance(m, dict)}
    out: dict[str, dict] = {}
    for m in members or []:
        if not m.get("is_lead"):
            continue
        facts = by_enquiry.get(m.get("enquiry_id"))
        if facts is None or facts.get("lead_answers_unverified_at"):
            continue
        vals = {k: facts.get(k) for k in INTRO_HABIT_FIELDS if facts.get(k) not in (None, "")}
        if vals:
            out[m["ref"]] = vals
    return out


def intro_capture(c, gid: int, for_ai: bool = True) -> dict:
    """ONE guarded read step for the draft route: the projection (exact apt/ginfo/members the model will see), the current basis
    items, central readiness (can_circulate) and the context hash. Raises IntroNotCapturable(code) - static codes only. No
    writes happen inside; the step only makes the reads one consistent snapshot. `for_ai=False` lets the labelled template path
    proceed without an attributable basis (basis None)."""
    with _step(c, label="intro capture"):
        g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g:
            raise IntroNotCapturable("group_gone")
        proj = intro_offer_projection(c, gid)
        if proj is None:
            raise IntroNotCapturable("no_offer")
        if not proj["members"]:
            raise IntroNotCapturable("no_members")
        if g.get("brief_status") != "ok":
            raise IntroNotCapturable("brief_not_ok")
        circ = can_circulate(c, g)
        if not circ.get("ok"):
            raise IntroNotCapturable("not_circulable", circ.get("reasons") or [])
        basis = intro_basis_items(c, gid, proj["apartment_id"])
        if basis is None and for_ai:
            raise IntroNotCapturable("legacy_basis")
        proj["basis"] = basis
        proj["basis_scope"] = "current" if basis is not None else "legacy"
        proj["circulation"] = circ
        proj["context_hash"] = intro_context_hash(proj)
        proj["habit_answers"] = intro_habit_answers(g, proj["members"])   # succ4: private validation context from the SAME group row / evidence the basis is bound to
        return proj


# ---- basis provenance: written once at the brief's apply, consumed immutably -----------------------------------------------
_PROJECTION_FIELD_OF = {   # cited apartment fact field -> how to read the same value from the CURRENT offer projection
    "type_label": lambda proj: proj["apt"]["type_label"], "suburb": lambda proj: proj["apt"]["suburb"],
    "bills_included": lambda proj: bool(proj["apt"]["bills_included"]), "lease_start": lambda proj: proj["ginfo"]["lease_start"],
    "lease_end": lambda proj: proj["ginfo"]["lease_end"], "total_weekly": lambda proj: proj["ginfo"]["total_weekly"],
    "extra_bed": lambda proj: proj["ginfo"]["extra_bed_charge"],
    "rate_band": lambda proj: __import__("server.matching", fromlist=["rate_band_fact"]).rate_band_fact(proj["ginfo"]["rate_kind"]),
    "stay_days": lambda proj: __import__("server.matching", fromlist=["stay_days_fact"]).stay_days_fact(date.fromisoformat(proj["ginfo"]["lease_start"]), date.fromisoformat(proj["ginfo"]["lease_end"])) if proj["ginfo"].get("lease_start") and proj["ginfo"].get("lease_end") else None,
}
_FEATURE_WORD = {"yes": "available", "no": "unavailable", "unknown": "unverified"}


def basis_note_meta(item: dict, cat: dict, refs_json: dict) -> dict | None:
    """Immutable provenance for ONE brief note, computed at the brief's apply from the packet the model actually saw:
    subjects (ORIGINAL packet ref + stable real ids - never an intro I-ref), cited apartment ids, cited fields, per-subject
    per-field coverage with its basis ('own' personal answer / 'party_reported' lead-reported party-wide fact / 'none'), and a
    projection of the cited facts (status for everyone; VALUE only for apartment/listing facts, never a person's answer).
    None when a cited person cannot be resolved to a real identity (the note is then unattributable and never used)."""
    from . import facts as F
    subjects: dict[tuple, dict] = {}
    apartment_ids: list[int] = []
    fields: list[str] = []
    coverage: dict[str, dict] = {}
    proj: dict[str, dict] = {}
    for fid in item.get("fact_ids") or []:
        f = cat.get(fid)
        if not f:
            return None
        field = f.get("field")
        if field and field not in fields:
            fields.append(field)
        if f.get("scope") in ("apartment", "rule"):
            aid = f.get("apartment")
            if aid is not None and aid not in apartment_ids:
                apartment_ids.append(aid)
            proj[fid] = {"status": f.get("status"), "scope": f.get("scope"), "value": f.get("value") if f.get("scope") == "apartment" else None}
            continue
        ref = f.get("ref")
        ident = (refs_json or {}).get(ref)
        if not ref or not ident or ident.get("enquiry_id") is None:
            return None
        companion = "-m" in ref   # packet companion refs are 'U<n>-m<k>'; the lead is 'U<n>'
        if companion and ident.get("party_member_id") is None:
            return None   # a declared party slot with no real party_members row: unresolvable provenance, never aliased to the lead
        key = (int(ident["enquiry_id"]), ident.get("party_member_id") if companion else None)
        subjects.setdefault(key, {"packet_ref": ref, "enquiry_id": key[0], "party_member_id": key[1], "role": "companion" if companion else "lead"})
        basis = "none" if f.get("status") != "recorded" else ("own" if f.get("scope") == "personal" and f.get("source") == ref else "party_reported")
        coverage.setdefault(ref, {})[field] = {"status": f.get("status"), "basis": basis}
        proj[fid] = {"status": f.get("status"), "scope": f.get("scope"), "value": None}
    meta = {"subjects": list(subjects.values()), "apartment_ids": apartment_ids, "fields": fields, "coverage": coverage, "facts": proj}
    meta["facts_hash"] = _canon_hash(proj)
    return meta


def _attach_basis_meta(body: dict, cat: dict, refs_json: dict) -> dict:
    """Add `meta` to every group-brief note (BRIEF_GROUP_KEYS) and stamp basis_meta_version. A note whose provenance cannot be
    resolved keeps meta=None (it is simply never usable as introduction basis)."""
    for k in ai.BRIEF_GROUP_KEYS:
        for x in body.get(k) or []:
            if isinstance(x, dict):
                x["meta"] = basis_note_meta(x, cat, refs_json)
    body["basis_meta_version"] = BASIS_META_VERSION
    return body


def _current_identity_map(members: list[dict]) -> dict[tuple, dict]:
    """(enquiry_id, party_member_id, role) -> current intro person. The role is part of the key so (eid, None) can only ever
    be the lead; a companion is addressed by its real party_members id."""
    out = {}
    for m in members:
        if m.get("enquiry_id") is None:
            continue
        if m.get("is_lead"):
            out[(int(m["enquiry_id"]), None, "lead")] = m
        elif m.get("party_member_id") is not None:
            out[(int(m["enquiry_id"]), m["party_member_id"], "companion")] = m
    return out


_REF_TOKEN = re.compile(r"\bU\d+(?:-m\d+)?\b")


def _remap_ref_tokens(text: str, display: dict[str, str]) -> tuple[str, bool]:
    """Replace whole packet-ref tokens (U1, U10, U1-m1 ...) with the current person's display name. (text, False) when a token
    cannot be resolved to a current person - the note is then unusable rather than guessed."""
    unresolved = []

    def sub(m):
        tok = m.group(0)
        if tok in display:
            return display[tok]
        unresolved.append(tok)
        return tok
    out = _REF_TOKEN.sub(sub, text)
    return out, not unresolved


def intro_basis_items(c, gid: int, apartment_id: int | None, proj: dict | None = None) -> list[dict] | None:
    """The neutral notes the introduction's fit prose may rest on, from the CURRENT ok group brief (same evidence hash as the
    group), using ONLY the provenance stored with it at apply time. Returns None (legacy / unattributable -> labelled template)
    when the brief predates basis_meta_version or lacks refs_json. Each kept item: {section, text, fact_ids, subjects (remapped
    to CURRENT intro refs by immutable ids), fields, coverage, facts_hash}. Dropped: notes about another apartment; notes whose
    cited apartment facts differ from, or cannot be verified against, the exact current offer projection; notes whose subject is
    no longer a member / cannot be mapped. Nothing about people is recomputed from today's answers. [] is a valid empty basis."""
    g = row(c.execute("SELECT brief_status, evidence_hash, apartment_id FROM groups WHERE id=?", (gid,)))
    if not g or g.get("brief_status") != "ok":
        return []
    b = row(c.execute("SELECT body_json, evidence_hash, refs_json FROM group_briefs WHERE group_id=? AND kind='group' AND status='ok' ORDER BY id DESC LIMIT 1", (gid,)))
    if not b or b.get("evidence_hash") != g.get("evidence_hash"):
        return []
    body = pj(b.get("body_json"), {}) or {}
    refs_json = pj(b.get("refs_json"), {}) or {}
    if body.get("basis_meta_version") != BASIS_META_VERSION or not refs_json:
        return None
    if proj is None:
        proj = intro_offer_projection(c, gid)
    if proj is None or apartment_id is None or proj.get("apartment_id") != apartment_id:
        return []
    from . import facts as F
    ident = _current_identity_map(proj["members"])
    feature_now = {F.feature_fact_key(f["label"]): f["recorded"] for f in proj["apt"]["features"]}   # same label normalisation as the catalogue
    # token-safe display map for EVERY original packet ref the brief knew (resolved to the current person by stable ids)
    display: dict[str, str] = {}
    for pref, pid in refs_json.items():
        if not isinstance(pid, dict) or pid.get("enquiry_id") is None:
            continue
        comp = "-m" in pref
        if comp and pid.get("party_member_id") is None:
            continue
        m = ident.get((int(pid["enquiry_id"]), pid.get("party_member_id") if comp else None, "companion" if comp else "lead"))
        if m:
            display[pref] = m.get("first_name") or m.get("name") or m["ref"]
    out = []
    for section in ("strengths", "what_could_work", "discussion_points", "tensions"):
        for i, x in enumerate(body.get(section) or []):
            meta = (x or {}).get("meta") if isinstance(x, dict) else None
            if not meta or meta.get("facts_hash") != _canon_hash(meta.get("facts") or {}):
                return None   # ambiguous/legacy provenance anywhere in the brief: fail closed for the whole basis
            if any(a != apartment_id for a in meta.get("apartment_ids") or []):
                continue
            ok = True
            for fid, fx in (meta.get("facts") or {}).items():
                if fx.get("scope") == "rule":
                    ok = False   # rule results are not re-verifiable from the projection: omit safely
                    break
                if fx.get("scope") == "apartment":
                    field = fid.split(".", 1)[1] if "." in fid else ""
                    if field.startswith("feature."):
                        now = _FEATURE_WORD.get(feature_now.get(field[len("feature."):]))
                    elif field in _PROJECTION_FIELD_OF:
                        try:
                            now = _PROJECTION_FIELD_OF[field](proj)
                        except Exception:
                            now = None
                    else:
                        ok = False   # campus_km, available_from, rooms ...: not part of what the introduction shows
                        break
                    if fx.get("status") != "recorded" or now is None or str(now) != str(fx.get("value")):
                        ok = False
                        break
            if not ok:
                continue
            subjects = []
            for sj in meta.get("subjects") or []:
                role = sj.get("role") or ("companion" if sj.get("party_member_id") is not None else "lead")
                m = ident.get((int(sj["enquiry_id"]), sj.get("party_member_id") if role == "companion" else None, role))
                if not m or (role == "companion" and sj.get("party_member_id") is None):
                    ok = False
                    break
                subjects.append({"ref": m["ref"], "name": m.get("first_name") or m.get("name"), "enquiry_id": int(sj["enquiry_id"]), "party_member_id": sj.get("party_member_id"), "packet_ref": sj.get("packet_ref")})
            if not ok:
                continue
            # the ORIGINAL reference-bearing note (never the historically rendered text: it may carry old names or appended
            # ref=value facts); every U-ref token is remapped whole-token to the CURRENT person, or the note is dropped
            text, ok = _remap_ref_tokens(x.get("note") or "", display)
            if not ok:
                continue
            out.append({"section": section, "text": text, "fact_ids": list(x.get("fact_ids") or []), "subjects": subjects, "fields": list(meta.get("fields") or []),
                        "coverage": meta.get("coverage") or {}, "facts_hash": meta.get("facts_hash")})
    return out


def record_intro_draft(c, gid: int, text: str, basis: str, who: str | None = None, members: list[dict] | None = None, capture: dict | None = None) -> None:
    """Keep every introduction draft as history (kind='intro'), stamped with the group's current evidence hash: status 'ok' for
    an AI draft, 'edited' for a staff edit of a current draft, 'template' for the labelled fallback (which can never count as the
    required AI draft). Pass `capture` (intro_capture output, or for a staff edit the stored context of the draft row the staff
    member saw: apartment_id / profile_hash / offer_fingerprint / basis / basis_scope). The draft is REFUSED (ValueError, nothing
    written) if any person it describes was deleted, the membership changed, or the fresh offer projection differs from the
    captured context (apartment, exact offer, introduction details, evidence/membership context). Member identities are REQUIRED
    (capture["members"] from intro_capture, or members= built from the named stored row's refs_json for a staff edit); a call
    without them is refused (INTRO_REFUSAL_NO_IDENTITIES) so no history row is ever written with refs_json NULL. The old
    `members=` form without capture is still accepted: it records no offer context, so such a draft is never current. No commit
    here: the enclosing guard commits at its outermost exit."""
    with _step(c, label="intro draft record"):
        fresh = intro_offer_projection(c, gid)
        if capture is not None and members is None:
            members = capture.get("members")
        if members is None:
            # every intro history row carries each described person's immutable identity (refs_json). A staff edit must pass the
            # NAMED stored row's refs as members= alongside the stored context (app boundary); nothing is ever stamped from a
            # fresh projection here, and a row without identities is refused rather than written with refs_json NULL.
            raise ValueError(INTRO_REFUSAL_NO_IDENTITIES)
        profile_hash = None
        if members is not None:
            want: set[str] = set()
            for m in members:
                eid = m.get("enquiry_id")
                if eid is None:
                    raise ValueError("introduction discarded: member without an enquiry id")
                want.add(str(int(eid)))
                if m.get("party_member_id") is not None:
                    if not row(c.execute("SELECT 1 FROM party_members WHERE id=? AND enquiry_id=?", (m["party_member_id"], eid))):
                        raise ValueError("introduction discarded: a party member it describes was deleted")
            if not people_intact(c, gid, want):
                raise ValueError("introduction discarded: a person it describes was deleted or the group membership changed")
            profile_hash = intro_profile_hash(members)
        offer_fingerprint = apartment_id = None
        basis_items = None
        basis_scope = None
        if capture is not None:
            if fresh is None:
                raise ValueError(INTRO_REFUSAL_NO_OFFER)
            if capture.get("apartment_id") != fresh["apartment_id"]:
                raise ValueError(INTRO_REFUSAL_APARTMENT)
            if capture.get("offer_fingerprint") != fresh["offer_fingerprint"]:
                raise ValueError(INTRO_REFUSAL_OFFER)
            if (capture.get("profile_hash") or profile_hash) != fresh["profile_hash"]:
                raise ValueError(INTRO_REFUSAL_PROFILE)
            # the whole captured context (profile + offer + evidence hash + membership key + apartment) must still hold: a
            # compatibility-fact or membership change with an identical offer must not be re-tagged with today's context
            if (capture.get("context_hash") or intro_context_hash(capture)) != intro_context_hash(fresh):
                raise ValueError(INTRO_REFUSAL_CONTEXT)
            profile_hash = fresh["profile_hash"]
            offer_fingerprint, apartment_id = fresh["offer_fingerprint"], fresh["apartment_id"]
            basis_items, basis_scope = capture.get("basis"), capture.get("basis_scope") or ("current" if capture.get("basis") is not None else "legacy")
        elif members is not None:
            # pre-r7 call: re-read the people's intro facts and refuse if anything the drafter saw has moved
            if fresh is not None and fresh["profile_hash"] != profile_hash:
                raise ValueError(INTRO_REFUSAL_PROFILE)
            if fresh is None and intro_profile_hash(intro_members(c, gid)) != profile_hash:
                raise ValueError(INTRO_REFUSAL_PROFILE)
        g = row(c.execute("SELECT evidence_hash, member_key FROM groups WHERE id=?", (gid,))) or {}
        status = INTRO_STATUS.get(basis, "template")
        if basis_scope == "legacy" and status == "ok":
            status = "template"   # an AI draft written without an attributable basis is labelled, never current
        context_hash = _canon_hash([profile_hash, offer_fingerprint, g.get("evidence_hash"), g.get("member_key"), apartment_id]) if offer_fingerprint else None
        body = {"text": text, "basis": basis, "by": who, "profile_hash": profile_hash, "offer_fingerprint": offer_fingerprint, "apartment_id": apartment_id,
                "basis_items": basis_items, "basis_scope": basis_scope, "context_hash": context_hash, "prose_contract": ai.INTRO_PROSE_CONTRACT}
        refs_json = j({m["ref"]: {"enquiry_id": int(m["enquiry_id"]), "party_member_id": m.get("party_member_id")} for m in members if m.get("ref")}) if members is not None else None
        c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at, refs_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (gid, 0, "intro", None, None, None, j(body), status, None,
                                                            ai.MODEL if basis == "AI" else None, rules.RULE_VERSION, g.get("evidence_hash"), now_iso(), refs_json))


def intro_state(c, gid: int) -> dict:
    """{current, basis, status, stale_reason, drafted_at} (unchanged keys) + r7 additive keys {draft_id, context_hash,
    offer_current, profile_current, apartment_id, basis_scope}. The latest intro row decides. Current only when that row is an AI
    draft or a staff edit of one ('ok' / 'edited'), drafted under the group's current evidence hash, for the group's current
    apartment with the SAME exact offer fingerprint, with the people's introduction details unchanged, on a current (non-legacy)
    basis, under today's introduction wording check (ai.INTRO_PROSE_CONTRACT). A legacy draft without offer context is stale. PURE read: plain SELECTs and hashing; nothing is written, refreshed or
    queued (safe on every group render). Not a readiness/GP8 statement."""
    last = row(c.execute("SELECT id, status, body_json, created_at, evidence_hash FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (gid,)))
    base = {"current": False, "basis": None, "status": None, "stale_reason": INTRO_STALE_NONE, "drafted_at": None,
            "draft_id": None, "context_hash": None, "offer_current": False, "profile_current": False, "apartment_id": None, "basis_scope": None}
    if not last:
        return base
    body = pj(last.get("body_json"), {}) or {}
    out = dict(base, basis=body.get("basis"), status=last["status"], drafted_at=last["created_at"], draft_id=last["id"], context_hash=body.get("context_hash"),
               apartment_id=body.get("apartment_id"), basis_scope=body.get("basis_scope"))
    g = row(c.execute("SELECT evidence_hash, apartment_id FROM groups WHERE id=?", (gid,))) or {}
    if last["status"] not in ("ok", "edited"):
        out["stale_reason"] = body.get("reason") or (INTRO_STALE_TEMPLATE if last["status"] == "template" else last["status"])
        return out
    proj = intro_offer_projection(c, gid)
    out["offer_current"] = bool(proj and body.get("offer_fingerprint") and body.get("apartment_id") == g.get("apartment_id") == proj["apartment_id"]
                                and body["offer_fingerprint"] == proj["offer_fingerprint"])
    out["profile_current"] = bool(proj and body.get("profile_hash") and body["profile_hash"] == proj["profile_hash"])
    if last.get("evidence_hash") != g.get("evidence_hash"):
        out["stale_reason"] = INTRO_STALE_FACTS
        return out
    if not body.get("offer_fingerprint") or body.get("apartment_id") is None:
        out["stale_reason"] = INTRO_STALE_NO_CONTEXT
        return out
    if not out["offer_current"]:
        out["stale_reason"] = INTRO_STALE_OFFER
        return out
    if not out["profile_current"]:
        out["stale_reason"] = INTRO_STALE_PROFILE
        return out
    if body.get("basis_scope") == "legacy":
        out["stale_reason"] = INTRO_STALE_TEMPLATE
        return out
    if body.get("prose_contract") != ai.INTRO_PROSE_CONTRACT:
        # succ3: the wording checks the draft passed are not today's (or were never recorded): history, never current - even with
        # identical evidence (a member whose notes_status is 'none' gets no notes bump, so nothing else would stale it)
        out["stale_reason"] = INTRO_STALE_CONTRACT
        return out
    out["current"], out["stale_reason"] = True, None
    return out


def process_briefs(c, attempts: int = AI_ATTEMPTS, limit: int | None = None) -> dict:
    """Write per-member and group briefs for every approved, unbriefed live group. One AI call per group; strict parsing; a
    failure leaves brief_status='failed' (never ready, never fabricated). Promotes approved revisions afterwards.
    T5 shape per group: [guarded capture step] -> model call (no guard held) -> [guarded apply step]."""
    from . import review_loop
    out = {"groups": 0, "ok": 0, "failed": 0, "calls": 0}
    gs = rows(c.execute("SELECT * FROM groups WHERE status IN (%s) AND ai_status='ok' AND brief_status='pending' ORDER BY is_primary DESC, id" % ",".join("?" * len(LIVE)), LIVE))
    if limit:
        gs = gs[:limit]
    for g in gs:
        out["groups"] += 1
        when = now_iso()
        try:
            with _step(c, label="brief capture"):
                try:
                    p, drift = _payload(c, g), False
                except OfferDrift:
                    p, drift = None, True
                    _update(c, g["id"], brief_status="failed", ai_status="failed", ranking_basis="Rules only (offer no longer matches)", ai_error=OFFER_DRIFT_REASON, held_reason=OFFER_DRIFT_REASON, ready_at=None, updated_at=when)
                idents = _ref_identities(c, g)
                if (p is None or not idents) and not drift:
                    _update(c, g["id"], brief_status="failed", held_reason="Staff brief: group has no members or apartments", ready_at=None, updated_at=when)
                else:
                    review = dict(row(c.execute("SELECT rating, rationale, verdict FROM review_rounds WHERE group_id=? AND valid=1 ORDER BY id DESC LIMIT 1", (g["id"],))) or {})
                    last = row(c.execute("SELECT review_json FROM review_rounds WHERE group_id=? AND valid=1 ORDER BY id DESC LIMIT 1", (g["id"],))) or {}
                    review["findings"] = (pj(last.get("review_json"), {}) or {}).get("findings") or []
        except GUARD_FAILURES as ex:
            log.warning("group %s: brief capture not completed (%s)", g.get("id"), _tok(ex))
            _requeue_after_guard_failure(c, g, "brief")
            out["failed"] += 1
            continue
        if p is None or not idents:
            out["failed"] += 1
            continue
        packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": review,
                  "individually_recorded": {r: v["individually_recorded"] for r, v in idents.items()}}
        db.assert_not_guarded(c, "model call")
        res, err, calls = None, None, 0
        meter = {"asks": 0}   # every model call actually made (ai's compact retry after a cut-off reply included)
        for attempt in range(max(1, attempts)):
            calls += 1
            try:
                res = _call_briefs(packet, meter)
                break
            except ai.AINoRetry as e:
                err = _failure(e, "brief")   # bounded output: one compact retry already happened inside ai (or came back unusable); never re-run the pair
                break
            except ai.AIUnavailable as e:
                err = _failure(e, "brief")   # Modified A: closed sentence by kind / code worded for the brief step, never the exception's words
                if _no_point_retrying(e) or isinstance(e, ai.AIUnusableReply):   # decided by trusted exception types, not by words in a message
                    break
                time.sleep(min(20.0, 1.5 * (2 ** attempt)))
            except Exception as e:  # pragma: no cover
                err = _failure(e, "brief")
                time.sleep(1.0)
        calls = max(calls, meter["asks"])
        out["calls"] += calls
        try:
            with _step(c, label="brief apply"):   # T5: deletion/staleness checks and EVERY history insert (failure rows included) are one guarded step
                ok = _apply_briefs(c, g, p, idents, res, err, when)
        except GUARD_FAILURES as ex:
            log.warning("group %s: brief result not recorded (%s); re-queued", g.get("id"), _tok(ex))
            _requeue_after_guard_failure(c, g, "brief")
            ok = False
        out["ok" if ok else "failed"] += 1
        if ok and g.get("revision_of"):
            review_loop.promote(c, g["id"])
    return out


def _apply_briefs(c, g: dict, p: dict, idents: dict, res: dict | None, err: str | None, when: str) -> bool:
    """Inside the guarded apply step. Identity first (no history row of ANY kind - brief or failure row carrying refs/error text -
    about a snapshot whose person is gone or whose membership changed), then the failure row, then the staleness check against
    the hash the packet was captured with, then the briefs."""
    from . import matching
    rnd = int(g.get("revision_round") or 0)
    if not people_intact(c, g["id"], p["person_refs"]) or not refs_intact(c, p.get("refs_json")):
        _update(c, g["id"], brief_status="stale", ai_status="pending", ranking_basis=PENDING_BASIS, ai_queued_at=when, ready_at=None,
                held_reason="Brief discarded: a person it described was deleted or the membership changed; re-queued", updated_at=when)
        return False
    if res is None:
        c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at, refs_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (g["id"], rnd, "group", None, None, None, None, "failed", err, ai.MODEL, rules.RULE_VERSION, g.get("evidence_hash"), when, j(p.get("refs_json") or {})))
        _update(c, g["id"], brief_status="failed", held_reason=(BRIEF_FAILED_PREFIX + (err or ai.FAILURE_TEXT["no_review"]))[:600], ready_at=None, updated_at=when)
        return False
    now_hash, _ = matching.group_evidence(c, g["id"])
    if now_hash != g.get("evidence_hash") or now_hash != p.get("evidence_hash"):
        _update(c, g["id"], brief_status="stale", ai_status="pending", ranking_basis=PENDING_BASIS, ai_queued_at=when, ready_at=None,
                held_reason="Facts changed while the brief was written; re-queued", updated_at=when)
        return False
    _write_briefs(c, g, rnd, res, idents, now_hash, when, j(p.get("refs_json") or {}), packet=p)
    return True


def _call_briefs(packet: dict, meter: dict) -> dict:
    return _metered(ai.staff_briefs, packet, meter=meter)


def _write_briefs(c, g: dict, rnd: int, res: dict, idents: dict, now_hash: str, when: str, refs_json: str, packet: dict | None = None) -> None:
    from . import facts as F
    group_body = dict(res["group"])
    if packet is not None:   # r7: immutable per-note provenance from the packet the model actually saw (never rebuilt later)
        group_body = _attach_basis_meta(group_body, F.catalogue(packet), pj(refs_json, {}) or {})
    c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at, refs_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (g["id"], rnd, "group", None, None, None, j(group_body), "ok", None, ai.MODEL, rules.RULE_VERSION, now_hash, when, refs_json))
    for m in res["members"]:
        ident = idents[m["ref"]]
        body = dict(m)
        body["individually_recorded"] = ident["individually_recorded"]
        if not ident["individually_recorded"]:   # I16(d): a party member's study/work/contact/lifestyle are not separately recorded; never copied from the lead
            forced = [{"fact_ids": [f"{m['ref']}.{f}" for f in ("doing", "school", "course", "level")], "note": "Study or work: not recorded for this person (ask them directly)."},
                      {"fact_ids": [f"{m['ref']}.{f}" for f in ("routine", "tidy", "social", "guests", "diet")], "note": "Lifestyle answers: the enquirer answered for the party; this person's own answers are not recorded."}]
            have = {tuple(x["fact_ids"]) for x in m.get("missing_facts") or []}
            body["missing_facts"] = list(m.get("missing_facts") or []) + [x for x in forced if tuple(x["fact_ids"]) not in have]
            body["contact_recorded"] = False   # party members have name + email only (no phone, no contact method)
            body.setdefault("rendered", {})["missing_facts"] = [x["note"] for x in body["missing_facts"]]
        c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, error, model, rule_version, evidence_hash, created_at, refs_json)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (g["id"], rnd, "member", m["ref"], ident["enquiry_id"], ident["party_member_id"], j(body), "ok", None, ai.MODEL, rules.RULE_VERSION, now_hash, when, refs_json))
    _update(c, g["id"], brief_status="ok", held_reason=None, updated_at=when)
    chk = is_ready(c, row(c.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    _update(c, g["id"], ready_at=when if chk["ready"] else None)


def status(c) -> dict:
    """Queue status for the dashboard: counts by state over live groups, plus this process's counters."""
    live_q = ",".join("?" * len(LIVE))
    by = {r["ai_status"] or "none": r["n"] for r in rows(c.execute(f"SELECT ai_status, COUNT(*) n FROM groups WHERE status IN ({live_q}) GROUP BY ai_status", LIVE))}
    per_ds = {}
    for ds, flag in (("real", 0), ("test", 1)):
        per_ds[ds] = {r["ai_status"] or "none": r["n"] for r in rows(c.execute(f"""SELECT g.ai_status, COUNT(*) n FROM groups g WHERE g.status IN ({live_q}) AND EXISTS
            (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=?) GROUP BY g.ai_status""", (*LIVE, flag)))}
        per_ds[ds]["refresh"] = c.execute(f"""SELECT COUNT(*) FROM groups g WHERE g.status IN ({live_q}) AND g.ai_refresh=1 AND EXISTS
            (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=?)""", (*LIVE, flag)).fetchone()[0]
    refresh = c.execute(f"SELECT COUNT(*) FROM groups WHERE status IN ({live_q}) AND ai_refresh=1", LIVE).fetchone()[0]
    failed = rows(c.execute(f"SELECT id, ai_error, ai_attempts, is_primary FROM groups WHERE status IN ({live_q}) AND ai_status='failed' ORDER BY is_primary DESC, id DESC LIMIT 20", LIVE))
    with _lock:
        st = dict(STATE)
    ready = ready_groups(c)
    for ds, flag in (("real", 0), ("test", 1)):
        per_ds[ds]["ready"] = len(ready_groups(c, ds))
    not_ready = {}
    for g in rows(c.execute(f"SELECT * FROM groups WHERE status IN ({live_q}) AND ai_status='ok'", LIVE)):
        for why in is_ready(c, g)["reasons"]:
            not_ready[why] = not_ready.get(why, 0) + 1
    return {"pending": by.get("pending", 0), "running": by.get("running", 0), "ready": len(ready), "failed": by.get("failed", 0), "refresh": refresh,
            "briefs_pending": briefs_pending_count(c),   # D2: approved groups whose staff brief has not landed yet (worker resumes these)
            "notes_pending": notes_pending_count(c),     # T4c: members of groups awaiting review whose notes are not yet read (worker resumes these; C4: includes scheduled retries)
            "notes_retry_scheduled": notes_scheduled_retry_count(c),   # C4: of those, waiting for their backoff time (a later pass reads them again)
            "notes_failed": c.execute(f"""SELECT COUNT(DISTINCT e.id) FROM enquiries e JOIN group_members gm ON gm.enquiry_id=e.id JOIN groups g ON g.id=gm.group_id
                                         WHERE g.status IN ({live_q}) AND e.notes_status='failed'""", LIVE).fetchone()[0],
            "reviewed_ok": by.get("ok", 0), "revise": by.get("revise", 0), "held": by.get("held", 0), "stale": by.get("stale", 0), "not_ready_reasons": not_ready,
            "briefs": {r["brief_status"] or "none": r["n"] for r in rows(c.execute(f"SELECT brief_status, COUNT(*) n FROM groups WHERE status IN ({live_q}) AND ai_status='ok' GROUP BY brief_status", LIVE))},
            "rule_version": rules.RULE_VERSION,
            "per_dataset": per_ds, "failed_groups": failed, "workers": AI_WORKERS, "batch": AI_BATCH, "attempts": AI_ATTEMPTS, "model": ai.MODEL,
            "ai_configured": ai.ai_mode() != "none", "thread_alive": bool(_thread and _thread.is_alive()), **st}


def _loop(c):
    try:
        requeue_interrupted(c)
    except Exception:
        pass
    while True:
        _wake.wait(timeout=IDLE_POLL_S)
        _wake.clear()
        try:
            _iteration(c)
        except Exception as ex:  # keep the worker alive and the failure visible
            _record_worker_failure(ex)
            time.sleep(2.0)


MATEBOT_BATCHES_PER_WAKE = int(os.environ.get("MMAI_AI_QUEUE_BATCHES_PER_WAKE", "8"))   # fairness bound, not a business limit


def _iteration(c) -> dict:
    """One wake of the worker, both queues bounded so neither starves the other: a HomeBot review pass FIRST (homebot_review.consume,
    itself bounded; only when the HomeBot matching gate is ON; provider calls outside any retention guard), then at most
    MATEBOT_BATCHES_PER_WAKE MateBot batches, then a second HomeBot pass if MateBot had work. MateBot work left over re-wakes the
    loop at once (kick), so continuous MateBot arrivals are served in bounded slices with a HomeBot pass between slices rather than
    draining to exhaustion first. MMAI_AI_QUEUE=0 means this never runs, so no automatic provider call of either kind."""
    out = {"homebot": _consume_homebot_reviews(c), "matebot_batches": 0, "more": False, "reselect_retried": False}
    progressed = True
    for _ in range(max(1, MATEBOT_BATCHES_PER_WAKE)):
        if pending_count(c) <= 0 and briefs_pending_count(c) <= 0 and notes_pending_count(c) <= 0:   # D2: a pending staff brief is work too; T4c: unread notes too
            progressed = False
            with _lock:
                unsettled = bool(STATE.get("reselect_unsettled"))
            if unsettled:   # C2(b): an unsettled end-of-pass reselection is work too - retried once per wake, no provider call, no hot loop (the idle poll re-wakes)
                _reselect_after_pass(c)
                out["reselect_retried"] = True
            break
        res = process_pending(c, max_batches=1, batch_size=AI_BATCH, notes_limit=NOTES_PER_PASS)   # one MateBot batch (AI_BATCH groups) + at most NOTES_PER_PASS member notes per wake-slice
        out["matebot_batches"] += 1
        progressed = bool(res.get("groups")) or bool((res.get("notes") or {}).get("people"))   # notes-only progress is progress
        if res.get("skipped") or not progressed:
            break
    if progressed:   # work remains and this wake moved it on: the rest is served on the next wake (never a hot loop on stuck work)
        out["more"] = pending_count(c) > 0 or briefs_pending_count(c) > 0 or notes_pending_count(c) > 0
    if out["matebot_batches"]:
        out["homebot"] = _consume_homebot_reviews(c)
    if out["more"]:
        kick()
    return out


def _consume_homebot_reviews(c) -> dict:
    from . import homebot_review
    return homebot_review.consume(c)


def _record_worker_failure(ex: BaseException) -> None:
    """The worker loop's catch-all: a static category (exception type only) in STATE and the log; never the exception's text,
    which can carry provider/model prose."""
    from . import matching
    _bump(last_error=f"{STATE_ERROR_WORKER}:{matching.run_error_type_token(ex)}", last_error_at=now_iso())
    log.warning("AI queue worker iteration failed (%s); worker continues", matching.run_error_type_token(ex))


def start(c):
    """Start the queue worker once per process (no-op when MMAI_AI_QUEUE=0, for example in tests)."""
    global _thread
    if os.environ.get("MMAI_AI_QUEUE", "1") != "1" or (_thread and _thread.is_alive()):
        return None
    _thread = threading.Thread(target=_loop, args=(c,), daemon=True, name="mmai-ai-queue")
    _thread.start()
    return _thread
