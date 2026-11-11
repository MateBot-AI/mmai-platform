"""A10(e)/A6(d): staff authority for one exact booking, not a general blackout exemption.

Material requirements and current membership define authority; review timestamps and
introduction-only fields do not. Normal rent is read afresh, not part of that identity.
HELD and withdrawn records are never usable. Over-allowance totals require recorded
customer agreement. Every write requires the shared retention guard. The immutable
member link also lets retention scrub agreement prose after a person leaves a group.

All amounts are whole-apartment weekly totals: the exception includes A5 and must
never have it added again. Authorisation is not a complete apartment-fit check;
candidate consumers must still enforce dates, availability, bedding and other rules.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from typing import Optional

from . import budget, evidence_fields as ev, exception_rent, lease_dates, rules
from .db import RetentionGuardError, audit, guarded, now_iso, row, rows
from .lease_dates import StaffExitException

BOT_MATEBOT = "matebot"
BOT_HOMEBOT = "homebot"
PROPOSED = "proposed"                 # transient: written then immediately evaluated inside the same transaction
HELD = "held_over_allowance"
PASSES = "passes"
WITHDRAWN = "withdrawn"
LIVE = (HELD, PASSES, PROPOSED)
MAX_SCOPE = 200

# Booking requirements, not review freshness or introduction/profile information.
# Derived from matching.unit_from_row, rules._lease_bounds/check_group/parties,
# and the actual A16 baseline. Effective adult-declaration validity is bound below.
EXCEPTION_IDENTITY_FIELDS = (
    "city", "rooms", "apt_sizes", "budget_max", "move_in", "move_in_flex",
    "move_out", "move_out_flex", "mate_max", "bunk_ok", "party", "party_size",
    "couples_answer", "couples_json", "smoke", "smoke_ok", "linked_to_enquiry_id",
)


def _require_guard(c) -> None:
    if not guarded(c):
        raise RetentionGuardError("exit exception write outside retention guard")


class ExitExceptionError(ValueError):
    """Plain-language refusal; `status` is the HTTP status the route should answer with (404 / 409 / 422)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _iso_date(v, name: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v or "").strip())
    except ValueError:
        raise ExitExceptionError(f"{name} must be a date (YYYY-MM-DD)") from None


def context_ref(parts: dict) -> str:
    """sha256 of the canonical JSON of the immutable snapshot components (sorted keys, no whitespace). Deterministic: the same
    components give the same ref; any component change gives a different one."""
    canon = json.dumps({k: (str(v) if isinstance(v, (date, Decimal)) else v) for k, v in parts.items()}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _apartment(c, apartment_id: int) -> dict:
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (int(apartment_id),)))
    if not a:
        raise ExitExceptionError("No such apartment", 404)
    return a


def _normal_weekly_rent(a: dict, move_in: date, move_out: date) -> Decimal:
    """A6 rate for the booking's dates: the 6-month tariff when rules.rate_kind says so, else the 12-month rent. The extra-bed
    charge is added by the helper (budget.total_weekly_rent) for the party size."""
    kind = rules.rate_kind(move_in, move_out)
    return budget.money(a["rent_6m"] if kind == "6m" else a["rent_12m"], positive=True)


# ---------------------------------------------------------------- context builders (immutable snapshot -> parts + helper facts)
def matebot_context(c, group_id: int, apartment_id: int, move_in, move_out) -> dict:
    """Bind every current member, material requirements and effective adult declaration.

    Current rates are checked separately. Identity/name/email changes retain the
    existing conservative person-identity reset; phone and introduction edits do not.
    """
    from . import enquiries, identities, matching
    from .group_responses import current_version
    g = row(c.execute("SELECT id, status FROM groups WHERE id=?", (int(group_id),)))
    if not g:
        raise ExitExceptionError("No such group", 404)
    if g["status"] not in ("Proposed", "Introduced", "Group confirmed", "Awaiting replacement"):
        raise ExitExceptionError("This group is no longer active", 409)
    a = _apartment(c, apartment_id)
    move_in, move_out = _iso_date(move_in, "move_in"), _iso_date(move_out, "move_out")
    units, versions, membership = [], [], []
    for gm in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],))):
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (gm["enquiry_id"],)))
        if not e or e.get("barred_at") or e.get("status") not in matching.ACTIVE_ENQUIRY_STATUSES:
            raise ExitExceptionError("This group's members can no longer be evaluated", 409)
        members = rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))
        units.append(matching.unit_from_row(e, members))
        adult_state = enquiries.adults_status(e, members)
        effective_date = units[-1].adults_confirmed_move_in
        versions.append([int(e["id"]), {k: e.get(k) for k in EXCEPTION_IDENTITY_FIELDS},
                         (adult_state, effective_date.isoformat() if effective_date is not None else None)])
        membership.append([int(e["id"]), [(p["person_uid"], p["role"]) for p in identities.active(c, e["id"])]])
    if not units:
        raise ExitExceptionError("This group has no members", 409)
    people = [p for u in units for p in u.people]
    if any(p.budget_max is None for p in people):
        raise ExitExceptionError("Every member needs a stated weekly maximum before an exception can be assessed", 422)
    mv = current_version(c, g["id"])
    membership_hash = _sha({"version": int(mv["id"]) if mv else None, "members": membership})
    requirement_version = _sha(versions)
    parts = {"bot": BOT_MATEBOT, "subject_id": int(g["id"]), "membership_hash": membership_hash, "apartment_id": int(a["id"]),
             "move_in": move_in, "move_out": move_out, "requirement_version": requirement_version}
    return {"parts": parts, "context_ref": context_ref(parts), "bot": BOT_MATEBOT, "scenario": None,
            "homebot_enquiry_id": None, "group_id": int(g["id"]), "membership_hash": membership_hash, "requirement_version": requirement_version,
            "member_ids": [v[0] for v in versions],
            "apartment": a, "move_in": move_in, "move_out": move_out, "type_code": a["type_code"], "people": len(people),
            "normal_weekly_rent": _normal_weekly_rent(a, move_in, move_out),
            "whole_apartment_max": budget.matebot_baseline([p.budget_max for p in people])}


def homebot_context(c, enquiry_id: int, apartment_id: int, move_in, move_out, scenario: Optional[str] = None) -> dict:
    """Bind the actual shortlist inputs and recorded people, not contact or review timestamps.

    Positions matter: Requirements and bedding pairing consume that ordering. Row ids
    do not matter because a validated edit can reinsert otherwise identical people.
    """
    from . import homebot, homebot_match as hm, homebot_shortlist as hs
    scenario = scenario or hm.SCENARIO_CURRENT
    if scenario not in hs.SCENARIOS:
        raise ExitExceptionError("Unknown scenario", 422)
    try:
        e = homebot.get_record(c, int(enquiry_id))
    except Exception:  # noqa: BLE001 - get_record raises its own not-found error; the route answers 404 in plain words
        raise ExitExceptionError("No such HomeBot enquiry", 404) from None
    if not e:
        raise ExitExceptionError("No such HomeBot enquiry", 404)
    if e["status"] not in hs.OPEN_STATUSES:
        raise ExitExceptionError("This HomeBot enquiry is no longer open", 409)
    a = _apartment(c, apartment_id)
    move_in, move_out = _iso_date(move_in, "move_in"), _iso_date(move_out, "move_out")
    try:
        req = hs._requirements(c, e, scenario)
    except hm.InvalidRequirements as ex:
        raise ExitExceptionError(str(ex), 422) from None
    ver = hs._current_version(c, int(e["id"]), scenario)
    requirement_version = hs._hash({"version": int(ver["id"]) if ver else None, "requirements": req.as_dict()})
    membership_hash = hs._hash({
        "scenario": scenario, "head_count": int(req.head_count),
        "lead": [str(e.get(k) or "").strip().casefold() for k in ("first_name", "last_name", "email")],
        "people": [(int(p["position"]), str(p.get("name") or "").strip().casefold()) for p in e["people"]],
    })
    parts = {"bot": BOT_HOMEBOT, "subject_id": int(e["id"]), "membership_hash": membership_hash, "apartment_id": int(a["id"]),
             "move_in": move_in, "move_out": move_out, "requirement_version": requirement_version}
    return {"parts": parts, "context_ref": context_ref(parts), "bot": BOT_HOMEBOT, "scenario": scenario, "homebot_enquiry_id": int(e["id"]),
            "group_id": None, "member_ids": [], "membership_hash": membership_hash, "requirement_version": requirement_version, "apartment": a,
            "move_in": move_in, "move_out": move_out, "type_code": a["type_code"], "people": int(req.head_count),
            "normal_weekly_rent": _normal_weekly_rent(a, move_in, move_out), "whole_apartment_max": req.whole_apartment_max}


# ---------------------------------------------------------------- evaluation (existing helpers only)
def _staff_exception(r: dict) -> StaffExitException:
    return StaffExitException(r["context_ref"], date.fromisoformat(r["move_out"]), Decimal(r["exception_total_weekly_rent"]),
                              r["approved_by"], r["approved_at"])


def _agreement(r: dict) -> Optional[exception_rent.CustomerAgreement]:
    if not r.get("agreement_recorded_at"):
        return None
    return exception_rent.CustomerAgreement(r["context_ref"], Decimal(r["agreement_total"]), r["agreement_recorded_by"], r["agreement_recorded_at"])


def evaluate(ctx: dict, exception_total, staff_exception: StaffExitException, agreement=None) -> exception_rent.ExceptionRentResult:
    return exception_rent.check_exception_rent(ctx["context_ref"], ctx["type_code"], ctx["people"], ctx["whole_apartment_max"], ctx["move_out"],
                                               ctx["normal_weekly_rent"], exception_total, staff_exception=staff_exception, customer_agreement=agreement)


def _status_for(res: exception_rent.ExceptionRentResult) -> str:
    if res.passed:
        return PASSES
    if res.reason_code == exception_rent.OVER_ALLOWANCE_NEEDS_CUSTOMER_AGREEMENT:
        return HELD
    raise ExitExceptionError(_refusal(res), 422)


def _refusal(res: exception_rent.ExceptionRentResult) -> str:
    if res.reason_code == exception_rent.EXCEPTION_NOT_APPLICABLE:
        return "That move-out is in a normal exit window: no exit exception applies, use the ordinary offer"
    if "normal" in res.reason_code or "above" in res.reason_code or "price" in res.reason_code:
        return "The exception total must be above the normal whole-apartment weekly total for these dates and this party"
    return "This proposal cannot be authorised as stated"


def _build_context(c, bot: str, subject_id: int, apartment_id: int, move_in, move_out, scenario=None) -> dict:
    if bot == BOT_MATEBOT:
        return matebot_context(c, subject_id, apartment_id, move_in, move_out)
    if bot == BOT_HOMEBOT:
        return homebot_context(c, subject_id, apartment_id, move_in, move_out, scenario)
    raise ExitExceptionError("Unknown bot", 422)


# ---------------------------------------------------------------- writes (caller holds the retention guard)
def propose(c, bot: str, subject_id: int, apartment_id: int, move_in, move_out, exception_total_weekly_rent, staff: str, *,
            scenario: Optional[str] = None, now: Optional[str] = None) -> dict:
    """Record staff's exception for ONE booking and evaluate it. Refuses (422) a non-blackout move-out or a total not above the
    normal total; 409 when a live record for the same context already exists. Returns the stored record (see get)."""
    _require_guard(c)
    ctx = _build_context(c, bot, subject_id, apartment_id, move_in, move_out, scenario)
    # T14: freeze dataset provenance independently of later membership/owner edits.
    # Unknown or mixed ownership cannot be turned into an authorised booking.
    if bot == BOT_MATEBOT:
        ids = ctx["member_ids"]
        marks = ",".join("?" for _ in ids)
        owners = rows(c.execute(f"SELECT is_test FROM enquiries WHERE id IN ({marks})", tuple(ids)))
        flags = {r["is_test"] for r in owners}
        complete = len(owners) == len(ids)
    else:
        owners = rows(c.execute("SELECT is_test FROM homebot_enquiries WHERE id=?", (subject_id,)))
        flags = {r["is_test"] for r in owners}
        complete = len(owners) == 1
    if not complete or len(flags) != 1 or not flags.issubset({0, 1}):
        raise ExitExceptionError("The booking's real/test ownership is inconsistent; resolve it before approving an exception", 409)
    is_test = int(next(iter(flags)))
    try:
        total = ev.clean_positive_money(exception_total_weekly_rent, "exception_total_weekly_rent")
    except Exception:  # noqa: BLE001
        raise ExitExceptionError("exception_total_weekly_rent must be a positive amount", 422) from None
    when = now or now_iso()
    staff_name = ev.clean_text(staff, "approved_by", max_len=80)
    if row(c.execute("SELECT id FROM exit_exceptions WHERE context_ref=? AND status<>?", (ctx["context_ref"], WITHDRAWN))):
        raise ExitExceptionError("An exception for exactly this booking already exists; withdraw it before proposing another", 409)
    if not lease_dates.is_blackout(ctx["move_out"]):
        raise ExitExceptionError("That move-out is in a normal exit window: no exit exception applies, use the ordinary offer", 422)
    normal_total = budget.total_weekly_rent(ctx["normal_weekly_rent"], ctx["type_code"], ctx["people"])
    if total <= normal_total:
        raise ExitExceptionError("The exception total must be above the normal whole-apartment weekly total for these dates and this party", 422)
    res = evaluate(ctx, total, StaffExitException(ctx["context_ref"], ctx["move_out"], total, staff_name, when))
    status = _status_for(res)
    c.execute(
        "INSERT INTO exit_exceptions (bot, homebot_enquiry_id, group_id, scenario, membership_hash, apartment_id, move_in, move_out, requirement_version,"
        " context_ref, type_code, people, normal_weekly_rent, whole_apartment_max, exception_total_weekly_rent, normal_total_weekly_rent,"
        " approved_by, approved_at, status, evaluation_json, created_at, updated_at, is_test)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (bot, ctx["homebot_enquiry_id"], ctx["group_id"], ctx["scenario"], ctx["membership_hash"], int(ctx["apartment"]["id"]),
         ctx["move_in"].isoformat(), ctx["move_out"].isoformat(), ctx["requirement_version"], ctx["context_ref"], ctx["type_code"], int(ctx["people"]),
         rules._lossless_decimal_text(ctx["normal_weekly_rent"]), rules._lossless_decimal_text(ctx["whole_apartment_max"]),
         rules._lossless_decimal_text(total), rules._lossless_decimal_text(normal_total),
         staff_name, when, status, json.dumps(res.as_dict(), sort_keys=True), when, when, is_test))
    xid = int(row(c.execute("SELECT last_insert_rowid() AS id"))["id"])
    for eid in ctx["member_ids"]:
        c.execute("INSERT INTO exit_exception_members(exception_id,enquiry_id) VALUES (?,?)", (xid, eid))
    audit(c, staff_name, "exit_exception_proposed", "exit_exception", xid,
          f"{bot} subject {subject_id} apartment {int(ctx['apartment']['id'])} move-out {ctx['move_out'].isoformat()} total {ev.fmt_money(total)}: {status}")
    return get(c, xid)


def record_agreement(c, exception_id: int, staff: str, agreement_scope: str, total, *, now: Optional[str] = None) -> dict:
    """Write-once (R7): staff record whose agreement this is (free text) and the exact total agreed. Re-evaluates: `passes` only
    when the agreement covers this context and the authorised total exactly; a different total is refused before any write."""
    _require_guard(c)
    r = get(c, exception_id)
    if r["status"] == WITHDRAWN:
        raise ExitExceptionError("This exception was withdrawn", 409)
    if r.get("agreement_recorded_at"):
        raise ExitExceptionError("An agreement is already recorded for this exception; withdraw and propose again to change it", 409)
    scope = (agreement_scope or "").strip()
    if not scope:
        raise ExitExceptionError("Say whose agreement this records (for example: customer by phone, 11 Nov)", 422)
    try:
        agreed = ev.clean_positive_money(total, "agreement_total")
    except Exception:  # noqa: BLE001
        raise ExitExceptionError("total must be a positive amount", 422) from None
    if agreed != Decimal(r["exception_total_weekly_rent"]):
        raise ExitExceptionError("The agreed total must equal this booking's authorised total", 422)
    when = now or now_iso()
    staff_name = ev.clean_text(staff, "recorded_by", max_len=80)
    agreement = exception_rent.CustomerAgreement(r["context_ref"], agreed, staff_name, when)
    ctx = _fresh_context(c, r)
    if ctx["context_ref"] != r["context_ref"]:
        raise ExitExceptionError("The booking requirements or members changed; propose a new exception", 409)
    res = evaluate(ctx, Decimal(r["exception_total_weekly_rent"]), _staff_exception(r), agreement)
    status = _status_for(res)
    c.execute("UPDATE exit_exceptions SET agreement_recorded_by=?, agreement_recorded_at=?, agreement_scope=?, agreement_total=?, status=?,"
              " evaluation_json=?, updated_at=? WHERE id=?",
              (staff_name, when, scope[:MAX_SCOPE], rules._lossless_decimal_text(agreed), status, json.dumps(res.as_dict(), sort_keys=True), when, r["id"]))
    audit(c, staff_name, "exit_exception_agreement_recorded", "exit_exception", r["id"], f"agreed total {ev.fmt_money(agreed)}: {status}")
    return get(c, r["id"])


def withdraw(c, exception_id: int, staff: str, *, now: Optional[str] = None) -> dict:
    _require_guard(c)
    r = get(c, exception_id)
    if r["status"] == WITHDRAWN:
        raise ExitExceptionError("This exception was already withdrawn", 409)
    when = now or now_iso()
    staff_name = ev.clean_text(staff, "staff", max_len=80)
    c.execute("UPDATE exit_exceptions SET status=?, withdrawal_reason=?, updated_at=? WHERE id=?",
              (WITHDRAWN, "staff_withdrawn", when, r["id"]))
    audit(c, staff_name, "exit_exception_withdrawn", "exit_exception", r["id"], f"was {r['status']}")
    return get(c, r["id"])


def _stored_context(r: dict) -> dict:
    """Evaluation inputs frozen in the row at proposal time (type, party size, A6 rate, whole-apartment maximum): re-evaluation
    never re-reads live stock or members - the ref already binds the snapshot, and the helper adds the A5 charge exactly once."""
    return {"context_ref": r["context_ref"], "type_code": r["type_code"], "people": int(r["people"]), "whole_apartment_max": Decimal(r["whole_apartment_max"]),
            "move_out": date.fromisoformat(r["move_out"]), "normal_weekly_rent": Decimal(r["normal_weekly_rent"])}


# ---------------------------------------------------------------- reads
def get(c, exception_id: int) -> dict:
    r = row(c.execute("SELECT x.*, a.address AS address FROM exit_exceptions x LEFT JOIN apartments a ON a.id=x.apartment_id WHERE x.id=?",
                      (int(exception_id),)))
    if not r:
        raise ExitExceptionError("No such exception", 404)
    ev_json = json.loads(r.get("evaluation_json") or "{}")
    r["evaluation"] = {"passed": bool(ev_json.get("passed")), "reason_code": ev_json.get("reason_code"), "facts": ev_json.get("facts") or {}}
    r["people"] = int(r["people"])
    r["readiness"] = applies_now(c, r)
    return r


def _fresh_context(c, r: dict) -> dict:
    subject = r["group_id"] if r["bot"] == BOT_MATEBOT else r["homebot_enquiry_id"]
    return _build_context(c, r["bot"], subject, r["apartment_id"], r["move_in"], r["move_out"], r.get("scenario"))


def applies_now(c, r: dict, *, fresh: Optional[dict] = None) -> dict:
    """The sole authorisation gate. Read-only: stored approval status is never silently promoted."""
    if r["status"] == WITHDRAWN:
        return {"state": "withdrawn", "reason": r.get("withdrawal_reason") or "staff_withdrawn"}
    if r["status"] == HELD:
        return {"state": "held", "reason": "customer_agreement_needed"}
    if r["status"] != PASSES:
        return {"state": "held", "reason": "not_authorised"}
    try:
        ctx = fresh if fresh is not None else _fresh_context(c, r)
        if ctx["context_ref"] != r["context_ref"]:
            reason = "membership_changed" if ctx["membership_hash"] != r["membership_hash"] else "requirements_changed"
            return {"state": "stale", "reason": reason}
        result = evaluate(ctx, Decimal(r["exception_total_weekly_rent"]), _staff_exception(r), _agreement(r))
    except (ExitExceptionError, ValueError, ArithmeticError, KeyError, TypeError):
        return {"state": "stale", "reason": "context_unavailable"}
    return {"state": "authorised_now" if result.passed else "not_applicable", "reason": result.reason_code}


def authorised_offers(c, *, group_id: Optional[int] = None, homebot_enquiry_id: Optional[int] = None,
                      apartment_id: int, scenario: Optional[str] = None) -> tuple:
    """Exact authorised candidates only; no date invention and no reference-only authority."""
    if (group_id is None) == (homebot_enquiry_id is None):
        raise ValueError("Exactly one booking owner is required")
    field = "group_id" if group_id is not None else "homebot_enquiry_id"
    owner = group_id if group_id is not None else homebot_enquiry_id
    out = []
    for r in rows(c.execute(f"SELECT * FROM exit_exceptions WHERE {field}=? AND apartment_id=? ORDER BY id",
                           (owner, apartment_id))):
        if scenario is not None and r.get("scenario") != scenario:
            continue
        if applies_now(c, r)["state"] == "authorised_now":
            out.append(exception_rent.ExceptionOffer(
                int(r["id"]), r["context_ref"], date.fromisoformat(r["move_in"]),
                date.fromisoformat(r["move_out"]), Decimal(r["exception_total_weekly_rent"]),
                _staff_exception(r), _agreement(r), r["updated_at"]))
    return tuple(out)


CAPTURE_FIELDS = (
    "id", "bot", "group_id", "homebot_enquiry_id", "scenario", "apartment_id",
    "membership_hash", "requirement_version", "context_ref", "move_in", "move_out",
    "exception_total_weekly_rent", "approved_by", "approved_at", "status",
    "agreement_recorded_by", "agreement_recorded_at", "agreement_total", "updated_at",
)


def capture_for_group(c, gid: int) -> list[dict]:
    """Internal immutable evidence; never includes agreement_scope or audit prose."""
    out = []
    for r in rows(c.execute("SELECT * FROM exit_exceptions WHERE group_id=? ORDER BY id", (gid,))):
        out.append({**{key: r.get(key) for key in CAPTURE_FIELDS}, "applicability": applies_now(c, r)})
    return out


def capture_for_homebot(c, enquiry_id: int, scenario: str) -> list[dict]:
    """Capture one scenario's authority for pure computation and freshness fencing."""
    return [{**{key: r.get(key) for key in CAPTURE_FIELDS}, "applicability": applies_now(c, r)}
            for r in rows(c.execute("SELECT * FROM exit_exceptions WHERE homebot_enquiry_id=? AND scenario=? ORDER BY id",
                                    (enquiry_id, scenario)))]


def offers_from_capture(records: list[dict], apartment_id: int) -> tuple:
    """Build only from the caller's capture, without mixing in another live DB read."""
    return tuple(exception_rent.ExceptionOffer(
        int(r["id"]), r["context_ref"], date.fromisoformat(r["move_in"]), date.fromisoformat(r["move_out"]),
        Decimal(r["exception_total_weekly_rent"]), _staff_exception(r), _agreement(r), r["updated_at"])
        for r in records if int(r["apartment_id"]) == int(apartment_id)
        and r["status"] == PASSES and r["applicability"]["state"] == "authorised_now")


def forget_people(c, enquiry_ids: list[int]) -> int:
    """Scrub agreement prose for current AND former members, including already-withdrawn records."""
    _require_guard(c)
    ids = sorted(set(int(i) for i in enquiry_ids))
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    affected = rows(c.execute(
        f"""SELECT DISTINCT x.id AS exception_id FROM exit_exception_members m
            JOIN exit_exceptions x ON x.id=m.exception_id WHERE m.enquiry_id IN ({marks})
            AND (x.agreement_scope IS NOT NULL OR x.status<>?)""", (*ids, WITHDRAWN)))
    for item in affected:
        c.execute("""UPDATE exit_exceptions SET agreement_scope=NULL,
                     withdrawal_reason=CASE WHEN status=? THEN withdrawal_reason ELSE ? END,
                     status=?, updated_at=? WHERE id=?""",
                  (WITHDRAWN, "member_removed", WITHDRAWN, now_iso(), item["exception_id"]))
    if affected:
        audit(c, None, "exit_exception_personal_text_cleared", "exit_exception", None,
              f"{len(affected)} exception records scrubbed for {len(ids)} enquiries")
    return len(affected)


def for_group(c, group_id: int) -> list[dict]:
    return [get(c, r["id"]) for r in rows(c.execute("SELECT id FROM exit_exceptions WHERE group_id=? ORDER BY id DESC", (int(group_id),)))]


def for_homebot_enquiry(c, enquiry_id: int) -> list[dict]:
    return [get(c, r["id"]) for r in rows(c.execute("SELECT id FROM exit_exceptions WHERE homebot_enquiry_id=? ORDER BY id DESC", (int(enquiry_id),)))]
