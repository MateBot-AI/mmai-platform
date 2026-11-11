"""Outcome ledger (10 November 2026): what has happened for every person in a dataset, in plain words for staff.

READ-ONLY. Nothing here writes to the database, queues work, calls a provider, reserves an apartment or changes a group. The
ledger answers, for every resident behind every enquiry form:

  * where they stand NOW - exactly one of
      ready_primary              in the one proposed group for them, and that group can be introduced today
      primary_held               in the one proposed group for them, held for staff
      primary_failed_or_pending  in the one proposed group for them, not ready (review pending / failed / revision asked / ...)
      alternative_only           only in alternative candidates; none is their proposal
      planning_only              only in a partial planning candidate (this build records none and never invents one)
      no_proposal                in no current group at all
    or one of the two states that are outside the proposal stage: introduced_or_later (history, not a current proposal) and
    not_in_pool (withdrawn / barred);
  * why, and what staff can do next;
  * what has already happened (introduced / responded / accepted / leased), kept apart from the current proposal and never
    added to it.

Counting rules: an enquiry FORM is not a person. A couple form is two residents, a friends form several; linked "bring a mate"
forms stay separate forms and are shown as one linked party. Resident ids follow matching._people_ids (lead = "<enquiry id>",
party member k = "<enquiry id>m<k>"). Every resident is in exactly one current state, so the states add up to the total.

Readiness is ai_queue.can_circulate / ai_queue.is_ready - the same checks the introduction endpoints use - never a guess from
status columns, and never rules alone. Only `ready_primary` counts as a READY PROPOSAL. A ready proposal is not a placement:
it holds no apartment, and nobody is placed until a lease is signed and paid (history, shown separately). Alternatives are
reported as a separate union; planning-only candidates are never counted.

Apartments: the ledger reports whether a group's current apartment is a record that is still listed and unleased. That is a
reference to a listing. The ledger does not re-check a stored offer (lease dates, rate, rents, bedding) against today's facts,
so offer validity is NOT VERIFIED here; a stale offer makes a group not ready only through ai_queue's own check.

Consistency: many SELECTs, no lock, no transaction. get_ledger checks afterwards whether anything changed while it read and
says so in `consistency`; it never claims one instant that it has not checked.

The "why no group" checks are NECESSARY conditions only. They can prove that no listed apartment works on a person's dates, or
that no other enquiry in the pool can share dates with them; they cannot prove that a complete group exists. Budget is never
tested for one person against a whole apartment's rent: the only budget test is the complete group's combined total.
Anything this build cannot check is labelled NOT VERIFIED, never passed.
"""
from __future__ import annotations

import importlib
import os
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from . import ai_queue, lease_dates, matching, review_loop, rules, staff_view
from . import db as dbmod
from .db import now_iso, pj, rows

SCHEMA = 3   # 2: "ready proposal" wording (never "placed"), listed-apartment references, consistency block
             # 3: notes-reading hold / scheduled retry labels (engine constants imported, not copied); search wording never
             #    claims completeness the recorded counters do not establish
NOT_VERIFIED = "NOT VERIFIED"

CATEGORIES = ("ready_primary", "primary_held", "primary_failed_or_pending", "alternative_only", "planning_only", "no_proposal")
OUTSIDE_PROPOSAL_STAGE = ("introduced_or_later", "not_in_pool")
ALL_STATES = CATEGORIES + OUTSIDE_PROPOSAL_STAGE
COVERAGE_CATEGORY = "ready_primary"     # the ONLY state that counts as a ready proposal (a proposal is not a placement: only a lease is)

CATEGORY_LABEL = {
    "ready_primary": "Ready proposal: in the one proposed group for them, reviewed and ready to introduce",
    "primary_held": "Proposed group held for staff",
    "primary_failed_or_pending": "Proposed group not ready yet",
    "alternative_only": "Only in alternative candidates (none is their proposal)",
    "planning_only": "Only in a partial planning candidate (not a complete group; never counted as a ready proposal)",
    "no_proposal": "No proposed group",
    "introduced_or_later": "Already introduced or further along (history, not a current proposal)",
    "not_in_pool": "Not being matched (withdrawn or barred)",
}

BUDGET_NOTE = ("Budget is not checked here: the only budget test is a complete group's combined total against the whole rent, "
               "so one person's budget cannot rule them out on its own.")
SEARCH_NOT_PROOF = ("They pass every check that can be made for one enquiry on its own. The search has not found a complete group "
                    "that passes every requirement; that is not proof that none exists.")
READY_NOT_PLACEMENT = ("A ready proposal is a group staff may introduce. It is not a placement and it holds no apartment: nobody has "
                       "an apartment until a lease is signed and paid. Leases are shown under history.")
LISTED_REFERENCE_NOTE = ("These count people whose group's current apartment is a record that is still listed and not leased. That is a "
                         "reference to a listing only. The ledger does not re-check a stored apartment offer against today's rent, dates "
                         f"or bedding, so whether each offer still holds is {NOT_VERIFIED} here. A group shown as ready passed the system's "
                         "own readiness check when it was read; for any other group the stored offer may be out of date. "
                         "Nobody in these figures has an apartment.")
STOCK_DIAGNOSTIC_NOTE = ("Hypothetical only. No apartment is reserved for any group: several groups may be offered the same apartment "
                         "until one of them leases it. This figure asks how many proposed groups could each be given a different one of "
                         "their stored, still-listed apartment offers at the same time. The offers themselves are not re-checked "
                         f"({NOT_VERIFIED}), so this is an upper bound on paper, not a number of groups with an apartment.")

# ---------------------------------------------------------------------------------------------------- readiness reasons
# ai_queue.is_ready / can_circulate return internal reason strings. They are mapped to a closed set of plain sentences here;
# a string that is not recognised becomes the generic sentence - the raw text is never shown (it may carry internal references).
_REASON_MAP = (   # (how the internal reason starts or what it contains, code, plain sentence, next action)
    ("AI review pending", "ai_pending", "The AI review has not run yet.", "Wait for the AI review; nothing for staff to do unless it stays pending."),
    ("AI review running", "ai_running", "The AI review is running now.", "Wait for the AI review to finish."),
    ("AI review failed", "ai_failed", "The AI review did not complete. The group is not approved: rules alone never make a group ready.",
     "Retry the AI review from the group page; if it keeps failing, tell a manager."),
    ("AI asked for a revision", "revision_requested", "The AI review asked for a member to be replaced before this group can go ahead.",
     "Wait for the replacement alternatives to be reviewed. If none is approved, these people stay unresolved."),
    ("held for staff", "held", "The group is held for staff.", "Open the group and deal with the hold."),
    ("assessment stale", "review_stale", "The earlier AI review is out of date and must be repeated.", "Wait for the fresh AI review."),
    ("no approved AI review", "no_review", "There is no approved AI review for this group.", "Wait for the AI review, or retry it from the group page."),
    ("assessed under an earlier rule version", "needs_fresh_review", "The group was reviewed before the matching rules last changed; it needs a fresh review.", "Wait for the fresh AI review."),
    ("assessed with a model other", "needs_fresh_review", "The group was reviewed by a model other than the approved one; it needs a fresh review.", "Wait for the fresh AI review."),
    ("reviewed under an earlier review contract", "needs_fresh_review", "The group was reviewed under an earlier review format; it needs a fresh review.", "Wait for the fresh AI review."),
    ("staff brief pending", "brief_pending", "The AI approved the group; the staff brief is still being written.", "Wait for the staff brief."),
    ("staff brief failed", "brief_failed", "The AI approved the group, but the staff brief could not be written.", "Retry from the group page; if it keeps failing, tell a manager."),
    ("staff brief stale", "brief_stale", "The staff brief is out of date and must be rewritten.", "Wait for the fresh staff brief."),
    ("no staff brief", "brief_missing", "There is no staff brief for this group yet.", "Wait for the staff brief."),
    ("not yet read into the review", "notes_unread", "A member's free-text notes have not been read into the review yet.", "Wait for the notes to be read; the review follows."),
    ("read under an earlier extraction contract", "notes_outdated", "A member's free-text notes were read under an earlier format and must be read again.", "Wait for the notes to be re-read."),
    ("await confirmation after a change of enquirer", "answers_unconfirmed", "A member's individual answers await confirmation after the enquiry changed hands.",
     "Confirm the answers with the person who took over the enquiry."),
    ("facts changed since the AI review", "facts_changed", "Someone's answers, the members or the apartments changed after the AI review; it must be repeated.", "Wait for the fresh AI review."),
    ("no evidence snapshot", "no_evidence", "The group has no record of what the review was based on; it must be reviewed.", "Wait for the AI review."),
    ("no current apartment", "no_current_apartment", "The group has no current apartment after a stock change.",
     "Check the group: it needs a currently listed apartment before it can go ahead."),
    ("being re-checked after a stock change", "stock_recheck", "The group is being re-checked after an apartment change.", "Wait for the re-check to finish."),
    ("hard rules no longer pass", "requirements_no_longer_met", "With today's facts the group no longer passes the matching requirements (for example dates, the adult declaration or the apartment's availability).",
     "Open the group to see which requirement changed; the group may need to be re-formed."),
    ("not the primary proposal", "not_primary", "This is an alternative candidate, not the proposal.", "Nothing to do: an alternative cannot be introduced as it is."),
    ("committed in another live group", "member_committed_elsewhere", "A member is already committed in another group.", "Check the other group before doing anything with this one."),
    ("status ", "not_live", "The group is not in a live state.", "Nothing to do."),
)
_GENERIC_REASON = ("other", "Another readiness check has not passed.", "Open the group page to see its readiness list.")
_PROCESSING_CODES = frozenset({"ai_pending", "ai_running", "ai_failed", "review_stale", "no_review", "needs_fresh_review", "brief_pending", "brief_failed",
                               "brief_stale", "brief_missing", "notes_unread", "notes_outdated", "facts_changed", "no_evidence", "notes_retry_scheduled",
                               "notes_not_read"})
_HELD_BASIS = {   # groups.ranking_basis is a closed constant written by the engine; held_reason prose is not parsed
    "Held for staff (practical need to confirm)": (
        "essential_fact_unverified",
        "An essential practical need (for example step-free access) is not confirmed as met at the apartment: either nothing is recorded for it, or what is recorded says the apartment does not meet it. The group is held for staff; it has not passed.",
        "Check the fact for the apartment and record it, or choose another apartment for the group."),
    "Held for staff (AI review)": (
        "ai_review_hold", "The AI review held this group for a staff decision.", "Read the review's reason on the group page and decide."),
}
# v3: the engine's own constants are imported, never copied; a build that does not define them simply has no such entry.
_NOTES_HELD_BASIS = getattr(ai_queue, "NOTES_HELD_BASIS", None)
_NOTES_RETRY_SCHEDULED = getattr(ai_queue, "NOTES_RETRY_SCHEDULED_TEXT", None)
if isinstance(_NOTES_HELD_BASIS, str) and _NOTES_HELD_BASIS:
    _HELD_BASIS[_NOTES_HELD_BASIS] = (
        "notes_not_read",
        "A member's free-text notes could not be read after repeated attempts, so the AI review has not run. The group is held for staff; "
        "it has not been reviewed or approved.",
        "Retry the group so the notes are read again; the AI review follows once they are read.")
_NOTES_RETRY_REASON = {"code": "notes_retry_scheduled",
                       "text": "A member's free-text notes could not be read yet. Another attempt is scheduled; the AI review has not run.",
                       "next_action": "Wait for the next attempt. If it keeps failing the group will be held for staff."}
_CODE_LIKE = re.compile(r"\b(?:[A-Z]{1,2}\d{1,2}[a-z]?(?:\([a-z0-9]+\))?|U\d+(?:-m\d+)?|R\.[A-Z]\d+)\b")   # Register codes / internal refs: never shown


def _plain_reasons(internal: list) -> list[dict]:
    out, seen = [], set()
    for text in internal or []:
        hit = next(((code, plain, act) for key, code, plain, act in _REASON_MAP if str(text).startswith(key) or key in str(text)), _GENERIC_REASON)
        if hit[0] not in seen:
            seen.add(hit[0])
            out.append({"code": hit[0], "text": hit[1], "next_action": hit[2]})
    return out


def _safe_text(text) -> Optional[str]:
    """Existing staff wording, passed through only when it carries nothing that looks like a Register code or internal ref."""
    t = " ".join(str(text or "").split())
    return t[:300] if t and not _CODE_LIKE.search(t) else None


# ---------------------------------------------------------------------------------------------------- date feasibility
# One structured date check per (group of enquiries, apartment): feasible when the EARLIEST start everyone accepts once the
# apartment is free, paired with the LATEST permitted move-out day everyone accepts, passes the minimum lease and the empty-days
# cap. That pair dominates every other pair inside the windows (an earlier start and a later exit only lengthen the stay), so
# "this pair fails" proves no dates work there. tests/test_outcome_ledger.py checks it against rules.lease_offer_candidates.
DATE_OK = "ok"
DATE_FAILURE_TEXT = {
    "own_dates_apart": "the linked party's own accepted dates do not overlap",
    "available_too_late": "the apartment becomes available only after the latest move-in date accepted",
    "no_permitted_move_out_day": "the accepted move-out dates contain no day on which a lease may end",
    "availability_past_move_out": "the apartment becomes available so late that the stay could only start after the last move-out day accepted "
                                  "(a stock-timing mismatch, not an error in the enquiry's dates)",
    "start_after_move_out": "the earliest accepted move-in is not before the last move-out day on which a lease may end",
    "below_minimum_lease": "the longest stay the dates allow is shorter than the apartment's minimum lease",
    "minimum_lease_setting": "the apartment's minimum-lease setting cannot be used (an apartment record problem, not the enquiry's)",
    "too_many_empty_days": "the apartment would stand empty for too long before the move-in",
    "declaration_date": "dates work only with a move-in earlier than the adult declaration covers; a renewed declaration is needed",
}


def _latest_exit_day(out_lo: date, out_hi: date, memo: dict) -> Optional[date]:
    key = ("exit", out_lo, out_hi)
    if key not in memo:
        best = None
        for k in range((out_hi - out_lo).days + 1):
            day = out_hi - timedelta(days=k)
            if lease_dates.exit_window(day) is not None:
                best = day
                break
        memo[key] = best
    return memo[key]


def _windows(units: list) -> Optional[tuple]:
    try:
        ins, outs = [u.move_in_window() for u in units], [u.move_out_window() for u in units]
    except Exception:
        return None
    return max(w[0] for w in ins), min(w[1] for w in ins), max(w[0] for w in outs), min(w[1] for w in outs)


def date_outcome(avail: date, min_lease, units: list, today: date, *, with_declaration: bool, memo: Optional[dict] = None) -> str:
    """DATE_OK, or the first reason no dates work for these enquiries together at an apartment with this availability and
    minimum lease. `with_declaration=False` ignores the adult-declaration date, so a caller can tell 'no dates work' from
    'dates work once the declaration is renewed'. No budget, bedding or pairing rule is looked at."""
    memo = memo if memo is not None else {}
    w = _windows(units)
    if w is None:
        return "own_dates_apart"
    declared = max([u.adults_confirmed_move_in for u in units if u.adults_confirmed_move_in is not None], default=None) if with_declaration else None
    key = (w, declared, avail, min_lease, today)
    if key in memo:
        return memo[key]
    memo[key] = out = _date_outcome(w, declared, avail, min_lease, today, memo)
    return out


def _date_outcome(w: tuple, declared: Optional[date], avail: date, min_lease, today: date, memo: dict) -> str:
    in_lo, in_hi, out_lo, out_hi = w
    if in_lo > in_hi or out_lo > out_hi:
        return "own_dates_apart"
    if avail > in_hi:
        return "available_too_late"
    start = max(in_lo, avail)
    if declared is not None and declared > start:
        if declared > in_hi:
            return "declaration_date"
        start = declared
    end = _latest_exit_day(out_lo, out_hi, memo)
    if end is None:
        return "no_permitted_move_out_day"
    if end <= start:
        if declared is not None and start == declared and max(in_lo, avail) < end:
            return "declaration_date"
        return "availability_past_move_out" if avail > in_lo else "start_after_move_out"
    ok, _why = rules.exit_ok(start, end, min_lease)
    if not ok:
        try:
            earliest = lease_dates.min_lease_end(start, min_lease)
        except ValueError:
            return "minimum_lease_setting"
        return "minimum_lease_setting" if earliest is None else "below_minimum_lease"
    if not lease_dates.check_vacancy(today, avail, start, end).passed:
        return "too_many_empty_days"
    return DATE_OK


# ---------------------------------------------------------------------------------------------------- loading (SELECT only)
def _has(c, table: str, column: Optional[str] = None) -> bool:
    try:
        if table not in dbmod.table_names(c):
            return False
        return column is None or column in dbmod.table_columns(c, table)
    except Exception:
        return False


def _load_forms(c, is_test: int) -> dict:
    enqs = rows(c.execute("SELECT * FROM enquiries WHERE is_test=? ORDER BY id", (is_test,)))
    members: dict[int, list] = {}
    for m in rows(c.execute("SELECT pm.id, pm.enquiry_id, pm.name, pm.email, pm.birth_country, pm.birth_country_raw FROM party_members pm "
                            "JOIN enquiries e ON e.id=pm.enquiry_id WHERE e.is_test=? ORDER BY pm.id", (is_test,))):
        members.setdefault(m["enquiry_id"], []).append(m)
    from .enquiries import adults_status
    forms = {}
    for e in enqs:
        ms = members.get(e["id"], [])
        unit, problem = None, None
        try:
            unit = matching.unit_from_row(e, ms)
            if unit.date_problem():
                problem = "dates"
        except Exception:
            unit, problem = None, "unreadable"
        if unit is not None:
            residents = [{"resident_id": str(p.id), "name": p.name, "role": "enquirer" if p.is_lead else "party member"} for p in unit.people]
        else:   # same ids as matching._people_ids would give, from the stored rows
            residents = [{"resident_id": str(e["id"]), "name": e.get("first_name") or "", "role": "enquirer"}]
            residents += [{"resident_id": f"{e['id']}m{k}", "name": m["name"], "role": "party member"} for k, m in enumerate(ms, start=1)]
        try:
            declaration = adults_status(e, ms)
        except Exception:
            declaration = None
        forms[e["id"]] = {"e": e, "members": ms, "unit": unit, "input_problem": problem, "residents": residents, "declaration": declaration,
                          "link_root": e.get("linked_to_enquiry_id") or e["id"]}
    by_root: dict = {}
    for i, f in forms.items():
        by_root.setdefault(f["link_root"], []).append(i)
    for i, f in forms.items():
        f["linked_forms"] = sorted(o for o in by_root[f["link_root"]] if o != i)
    return forms


def _load_groups(c, is_test: int) -> tuple[dict, dict]:
    groups = {g["id"]: g for g in rows(c.execute(
        "SELECT g.* FROM groups g WHERE g.id IN (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id", (is_test,)))}
    members: dict[int, list] = {gid: [] for gid in groups}
    for r in rows(c.execute("SELECT gm.group_id, gm.enquiry_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=? ORDER BY gm.enquiry_id", (is_test,))):
        if r["group_id"] in members:
            members[r["group_id"]].append(int(r["enquiry_id"]))
    return groups, members


def _load_apartments(c) -> tuple[list, int]:
    """Listed, unleased apartments (the same set a full run uses) as (row, rules.Apartment); unreadable rows are counted."""
    out, unreadable = [], 0
    for a in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL ORDER BY id")):
        try:
            apt = matching.apartment_from_row(a)
            code = rules.normalise_type(apt.type_code)
            _, cap = rules.apartment_capacity(apt.bedrooms)
            out.append({"row": a, "apt": apt, "id": int(a["id"]), "city": apt.city, "size": rules.APT_TYPES[code]["size"], "bedrooms": rules.APT_TYPES[code]["bedrooms"],
                        "limit": min(cap, rules.APT_TYPES[code]["a4b_max"], rules.MAX_GROUP), "cap": cap, "avail": apt.available_from, "min_lease": apt.min_lease_months})
        except Exception:
            unreadable += 1
    return out, unreadable


def _access_module():
    """The build's own access-facts module, when it has one. The ledger never reads apartment columns, fact tables or notes for
    this itself: which record is current is that module's business, not something to be guessed here."""
    try:
        mod = importlib.import_module(".access_facts", __package__)
    except Exception:
        return None
    return mod if callable(getattr(mod, "current", None)) else None


_ACCESS_LABEL = {1: "recorded: step-free", 0: "recorded: not step-free", None: "not recorded"}
_ACCESS_UNBOUND = "not recorded (an earlier record was for a different address and no longer applies)"


def _access_fact(c, apartment_row: Optional[dict]) -> str:
    """Step-free access exactly as the build's access-facts module reports the apartment's CURRENT record. No such module, no
    apartment, an error or an answer in a shape the ledger does not know is NOT VERIFIED - never a pass. Reported only; the
    ledger does not decide from it whether a group may go ahead (readiness is ai_queue's)."""
    mod = _access_module()
    if not apartment_row or apartment_row.get("id") is None or mod is None:
        return NOT_VERIFIED
    try:
        cur = mod.current(c, apartment_row["id"])
        v = cur["value"] if isinstance(cur, dict) and "value" in cur else "?"
        unbound = isinstance(cur, dict) and cur.get("bound") is False   # the module's own flag, when it has one
    except Exception:
        return NOT_VERIFIED
    if isinstance(v, bool) or not (v is None or (isinstance(v, int) and v in (0, 1))):
        return NOT_VERIFIED
    if unbound:   # a record that no longer applies is never a recorded fact, whatever value came with it
        return _ACCESS_UNBOUND if v is None else NOT_VERIFIED
    return _ACCESS_LABEL[v]


# ---------------------------------------------------------------------------------------------------- groups
def _group_view(c, g: dict, member_ids: list, forms: dict, apt_rows: dict) -> dict:
    residents = [r["resident_id"] for i in member_ids if i in forms for r in forms[i]["residents"]]
    apt = apt_rows.get(g.get("apartment_id"))
    view = {"group_id": g["id"], "status": g["status"], "is_primary": bool(g.get("is_primary")), "form_ids": list(member_ids), "resident_ids": residents,
            "residents": len(residents), "apartment_id": g.get("apartment_id"), "apartment": (apt or {}).get("address"),
            "current_apartment_listed": bool(apt), "stored_offer_validity": NOT_VERIFIED, "step_free_access": _access_fact(c, apt),
            "ready": False, "can_introduce": False, "reasons": [], "kind": None, "hold": None}
    if g["status"] != "Proposed":
        view["kind"] = "introduced_or_later" if g["status"] in review_loop.COMMITTED_STATUSES else "closed"
        return view
    try:
        chk = ai_queue.can_circulate(c, g)
        ready, ok, internal = bool(chk["ready_check"]["ready"]), bool(chk["ok"]), list(chk["reasons"])
    except Exception:   # fail closed: a check that cannot be completed is not a pass
        ready, ok, internal = False, False, ["__unchecked__"]
    view["ready"], view["can_introduce"] = ready, ok
    view["reasons"] = [{"code": "not_checked", "text": f"Readiness could not be checked ({NOT_VERIFIED}).", "next_action": "Tell a manager."}] if internal == ["__unchecked__"] else _plain_reasons(internal)
    if (g.get("ai_status") == "pending" and not ok and isinstance(_NOTES_RETRY_SCHEDULED, str) and _NOTES_RETRY_SCHEDULED
            and g.get("ranking_basis") == _NOTES_RETRY_SCHEDULED):
        view["reasons"] = [dict(_NOTES_RETRY_REASON)] + view["reasons"]
    if g.get("ai_status") == "held":
        code, text, act = _HELD_BASIS.get(g.get("ranking_basis"), ("held_other", "The group is held for staff.", "Open the group and read the hold reason."))
        detail = None   # existing staff wording only, and only when it is safe to show; the reason prose is never interpreted
        try:
            hv = staff_view.hold_view(c, g) or {}
            detail = _safe_text(hv.get("label")) if hv.get("category") not in (None, "held") else _safe_text(g.get("held_reason"))
        except Exception:
            detail = None
        view["hold"] = {"code": code, "text": text, "next_action": act, "detail": detail}
    if g.get("is_primary"):
        view["kind"] = "reviewed ready proposal" if ok else "proposal, not ready"
    else:
        view["kind"] = "alternative candidate, AI-approved but not the proposal" if ready else "alternative candidate"
    if g.get("ai_status") == "revise":
        view["revision_round"] = int(g.get("revision_round") or 0)
        view["revision_limit"] = review_loop.MAX_REVISION_ROUNDS
    if g.get("ai_status") == "failed":
        view["ai_attempts"] = int(g.get("ai_attempts") or 0)
    return view


def _primary_state(view: dict) -> tuple[str, dict]:
    """(category, main reason) for a resident whose proposal is this primary group."""
    if view["can_introduce"]:
        return "ready_primary", {"code": "ready", "text": "Reviewed and approved by the AI, staff brief written, and the system's readiness check passed when this was read. "
                                         "This is a proposal staff may introduce, not a placement.", "next_action": "Staff can introduce this group."}
    codes = [r["code"] for r in view["reasons"]]
    if view["hold"]:
        return "primary_held", {k: view["hold"][k] for k in ("code", "text", "next_action")}
    if "no_current_apartment" in codes:
        return "primary_held", next(r for r in view["reasons"] if r["code"] == "no_current_apartment")
    for first in ("revision_requested", "ai_failed", "ai_running", "notes_retry_scheduled", "ai_pending"):
        if first in codes:
            return "primary_failed_or_pending", next(r for r in view["reasons"] if r["code"] == first)
    return "primary_failed_or_pending", (view["reasons"][0] if view["reasons"] else dict(zip(("code", "text", "next_action"), _GENERIC_REASON)))


# ---------------------------------------------------------------------------------------------------- why no proposal
def _cluster_units(form_ids: list, forms: dict) -> list:
    return [forms[i]["unit"] for i in form_ids]


def _solo_only(units: list) -> Optional[str]:
    """The ANSWERS describe an apartment to themselves - no group of two or more can satisfy them. This is not the same as an
    individual enquiry (one person on a form, matched normally) or wanting a private bedroom in a shared apartment (matched
    normally into an apartment with enough bedrooms)."""
    n = sum(u.size for u in units)
    if min(u.mate_max for u in units) < n:   # every group has at least one person from outside the party
        return ("They accept no housemates" if n == 1 else "Their housemate limit leaves no room for anyone outside their own party") + \
               ", so a housemate group cannot be formed from these answers."
    most_bedrooms = max((rules.APT_TYPES[code]["bedrooms"] for code in rules.APT_TYPES
                         if all(rules.APT_TYPES[code]["size"] in u.apt_sizes for u in units)), default=0)
    own_room_only = [p for u in units for p in u.people if set(p.arrangements) == {"Own room"}]
    if most_bedrooms and len(own_room_only) >= most_bedrooms and len(own_room_only) == n:
        return ("They want their own room and ticked only apartment sizes with no bedroom left for a housemate, so the answers describe an "
                "apartment to themselves and a housemate group cannot be formed from them.")
    return None


def _fits(a: dict, units: list, n: int) -> bool:
    """Necessary conditions that need no partner: city, a ticked size, room for at least the stated people. No budget test."""
    return all(u.city == a["city"] and a["size"] in u.apt_sizes for u in units) and n <= a["limit"]


def _pair_basic(cu: list, du: list, rejections: set) -> bool:
    n = sum(u.size for u in cu) + sum(u.size for u in du)
    if n > rules.MAX_GROUP or any(n - 1 > u.mate_max for u in cu + du):
        return False
    for a in cu:
        for b in du:
            if a.city != b.city or not (set(a.apt_sizes) & set(b.apt_sizes)):
                return False
            if (int(a.id), int(b.id)) in rejections or (int(b.id), int(a.id)) in rejections:
                return False
            if any(rules.pair_conflict(p, q) or rules.pair_conflict(q, p) for p in a.people for q in b.people):
                return False
    return True


def _why_no_proposal(form_ids: list, forms: dict, clusters: dict, apartments: list, rejections: set, today: date, memo: dict) -> dict:
    """Necessary-condition diagnosis for one enquiry (with its linked party). `proven` lists what the checks prove; when it is
    empty the outcome is honestly 'not found by the search', never 'impossible'."""
    fs = [forms[i] for i in form_ids]
    proven: list[dict] = []
    checks: dict = {"budget": BUDGET_NOTE}

    def add(code, text, action):
        proven.append({"code": code, "text": text, "next_action": action})

    bad = [f for f in fs if f["input_problem"]]
    if bad:
        kind = bad[0]["input_problem"]
        text = {"unreadable": "The stored answers on this enquiry could not be read.",
                "dates": "A stored move-in or move-out date on this enquiry is missing or unusable."}[kind]
        add("enquiry_needs_correcting", text + " This is a problem with the stored enquiry itself, not with the apartments or the other enquirers.",
            "Correct the enquiry's dates or answers with the enquirer.")
        return {"proven": proven, "checks": checks}
    units = _cluster_units(form_ids, forms)
    n = sum(u.size for u in units)
    for f in fs:
        if f["declaration"] == "missing":
            add("declaration_missing", "There is no adult declaration on this enquiry, so no group can be put forward for them yet.",
                "Ask the enquirer to confirm that everyone will be 18 or older on the move-in date.")
        elif f["declaration"] == "renew":
            add("declaration_renewal", "The adult declaration no longer covers this enquiry (the move-in date moved earlier or the party changed), so no group can be put forward until it is renewed.",
                "Ask the enquirer for a renewed adult declaration.")
        elif f["declaration"] != "confirmed":
            add("declaration_not_verified", f"The adult declaration could not be checked ({NOT_VERIFIED}).", "Check the enquiry's adult declaration.")
    solo = _solo_only(units)
    if solo:
        add("solo_only_answers", solo, "If the enquirer would in fact share, update their apartment sizes, room choice or housemate limit; otherwise they are outside housemate matching.")
    if any(u.move_out <= u.move_in for u in units):   # a stored fact, stated as such; whether any dates work is still decided by the checks below
        checks["stored_dates_note"] = "The stored move-out date is not after the stored move-in date; check the enquiry's dates with the enquirer."
    structural = [a for a in apartments if _fits(a, units, n + 1)]
    checks["apartments_listed"] = len(apartments)
    checks["apartments_right_city_size_and_room"] = len(structural)
    feasible_any, feasible_declared, failures = 0, 0, {}
    for a in structural:
        o = date_outcome(a["avail"], a["min_lease"], units, today, with_declaration=False, memo=memo)
        if o == DATE_OK:
            feasible_any += 1
            if date_outcome(a["avail"], a["min_lease"], units, today, with_declaration=True, memo=memo) == DATE_OK:
                feasible_declared += 1
        else:
            failures[o] = failures.get(o, 0) + 1
    checks["apartments_where_their_dates_work"] = feasible_any
    checks["apartments_where_their_dates_work_under_current_declaration"] = feasible_declared
    checks["date_failures"] = [{"reason": DATE_FAILURE_TEXT[k], "apartments": v} for k, v in sorted(failures.items(), key=lambda kv: -kv[1])]
    if not solo:
        if not structural:
            add("no_stock_of_ticked_sizes", "No listed apartment is in their city, of a size they ticked, with room for them and at least one housemate.",
                "Ask whether another apartment size would suit, or wait for new stock.")
        elif not feasible_any:
            main = max(failures.items(), key=lambda kv: kv[1])[0]
            add("no_date_feasible_stock", f"No listed apartment works on their dates: for {failures[main]} of {len(structural)} suitable apartment(s), {DATE_FAILURE_TEXT[main]}.",
                "Ask whether their dates can move, or wait for stock with different availability.")
        elif not feasible_declared and all(f["declaration"] == "confirmed" for f in fs):
            add("declaration_date", "Apartments work on their dates only with a move-in earlier than their adult declaration covers.",
                "Ask the enquirer for a renewed adult declaration covering the earlier move-in.")
    # partners: every other party in the pool, as one unit each
    own = set(form_ids)
    stage = {"other_parties_in_pool": 0, "pass_basic_conditions": 0, "accepted_dates_overlap": 0, "share_an_apartment_on_dates": 0,
             "share_an_apartment_on_dates_under_current_declarations": 0}
    for key, other_ids in clusters.items():
        if own & set(other_ids):
            continue
        stage["other_parties_in_pool"] += 1
        if any(forms[i]["input_problem"] for i in other_ids):
            continue
        du = _cluster_units(other_ids, forms)
        if not _pair_basic(units, du, rejections):
            continue
        stage["pass_basic_conditions"] += 1
        both = units + du
        w = _windows(both)
        if w is None or w[0] > w[1] or w[2] > w[3]:
            continue
        stage["accepted_dates_overlap"] += 1
        m = n + sum(u.size for u in du)
        shared = [a for a in apartments if _fits(a, both, m)]
        if not any(date_outcome(a["avail"], a["min_lease"], both, today, with_declaration=False, memo=memo) == DATE_OK for a in shared):
            continue
        stage["share_an_apartment_on_dates"] += 1
        if all(forms[i]["declaration"] == "confirmed" for i in list(own) + list(other_ids)) and \
                any(date_outcome(a["avail"], a["min_lease"], both, today, with_declaration=True, memo=memo) == DATE_OK for a in shared):
            stage["share_an_apartment_on_dates_under_current_declarations"] += 1
    checks["partners"] = stage
    if not solo and structural and feasible_any:
        if not stage["pass_basic_conditions"]:
            add("no_compatible_partner", "No other enquiry now in the pool passes the basic pairing conditions with them (city, a shared apartment size, "
                "housemate limits, smoking answers and earlier staff decisions).", "Nothing to change on their side unless their answers can move; new enquiries may fit.")
        elif not stage["share_an_apartment_on_dates"]:
            add("no_date_compatible_partner", "No other enquiry now in the pool can share dates with them: either the accepted move-in or move-out dates do not "
                "overlap, or no listed apartment works on the dates both accept.", "Ask whether their dates can move; new enquiries or new stock may fit.")
        elif not stage["share_an_apartment_on_dates_under_current_declarations"] and not any(p["code"].startswith("declaration") for p in proven):
            add("partner_declarations", "Enquiries that could share dates with them exist, but only with a move-in earlier than an adult declaration (theirs or the other "
                "person's) covers, or once a missing declaration is confirmed.", "Follow up the adult declarations: a renewed declaration for the earlier move-in may open a group.")
    return {"proven": proven, "checks": checks}


# ---------------------------------------------------------------------------------------------------- history (separate)
def _history(c, is_test: int, forms: dict, groups: dict, members: dict) -> dict:
    """What has ALREADY happened, per resident: introduced / responded / accepted / leased. Kept apart from the current proposal;
    a person can appear here and also hold a current state - the two are never added together."""
    hist: dict[str, dict] = {}
    notes = []

    def mark(resident_id, key):
        hist.setdefault(resident_id, {"introduced": False, "responded": False, "accepted": False, "leased": False})[key] = True

    resident_of_uid: dict[str, str] = {}
    if _has(c, "people_identities"):
        order: dict[int, list] = {i: [m["id"] for m in f["members"]] for i, f in forms.items()}
        for r in rows(c.execute("SELECT pi.person_uid, pi.enquiry_id, pi.role, pi.party_member_id FROM people_identities pi JOIN enquiries e ON e.id=pi.enquiry_id WHERE e.is_test=?", (is_test,))):
            if r["role"] == "lead":
                resident_of_uid[r["person_uid"]] = str(r["enquiry_id"])
            elif r["party_member_id"] in order.get(r["enquiry_id"], []):
                resident_of_uid[r["person_uid"]] = f"{r['enquiry_id']}m{order[r['enquiry_id']].index(r['party_member_id']) + 1}"
    else:
        notes.append(f"Person-level identities are not recorded in this build: responses per person are {NOT_VERIFIED}.")

    def residents_of_form(eid):
        return [r["resident_id"] for r in forms[eid]["residents"]] if eid in forms else []

    for gid, g in groups.items():   # the group's own stage: everyone on a member form was introduced / leased together
        if g["status"] in review_loop.COMMITTED_STATUSES:
            for eid in members.get(gid, []):
                for rid in residents_of_form(eid):
                    mark(rid, "introduced")
                    if g["status"] == "Leased":
                        mark(rid, "leased")
    for eid, f in forms.items():
        if f["e"].get("status") == "Leased":
            for rid in residents_of_form(eid):
                mark(rid, "leased")
    if _has(c, "group_membership_versions"):
        for v in rows(c.execute("SELECT group_id, members_json FROM group_membership_versions WHERE introduced_at IS NOT NULL")):
            for m in pj(v.get("members_json"), []) or []:
                rid = resident_of_uid.get(str(m.get("person_uid") or ""))
                if rid is None and m.get("enquiry_id") in forms and m.get("role") == "lead":
                    rid = str(m["enquiry_id"])
                if rid is not None and int(rid.split("m")[0]) in forms:
                    mark(rid, "introduced")
    else:
        notes.append(f"Introduction history is not recorded in this build: earlier introductions of since-dissolved groups are {NOT_VERIFIED}.")
    if _has(c, "group_responses"):
        for r in rows(c.execute("SELECT gr.person_uid, gr.enquiry_id, gr.response FROM group_responses gr JOIN enquiries e ON e.id=gr.enquiry_id "
                                "WHERE e.is_test=? AND gr.superseded_at IS NULL", (is_test,))):
            rid = resident_of_uid.get(str(r["person_uid"]))
            if rid is None:
                continue
            mark(rid, "responded")
            if r["response"] == "accept":
                mark(rid, "accepted")
    else:
        notes.append(f"Responses are not recorded in this build: responded / accepted are {NOT_VERIFIED}.")
    return {"by_resident": hist, "notes": notes}


# ---------------------------------------------------------------------------------------------------- stock
def _stock(proposed_views: list) -> dict:
    """Two separate figures, neither of them a count of people with an apartment.
    `listed_apartment_references`: unique residents whose group's current apartment id points at a record that is still listed
    and unleased. The stored offer (lease dates, rate, rents, bed allocation) is NOT re-checked here: validity is NOT VERIFIED.
    `hypothetical_listed_offer_assignment`: how many primary proposals could each be given a different one of their stored,
    still-listed offers at once (largest groups first, augmenting paths). Nothing is reserved; the figure changes nothing."""
    def unique(views):
        return len({r for v in views for r in v["resident_ids"]})
    listed = [v for v in proposed_views if v["current_apartment_listed"]]
    prim = [v for v in listed if v["is_primary"]]
    out = {"listed_apartment_references": {
        "note": LISTED_REFERENCE_NOTE, "offer_validity": NOT_VERIFIED, "nobody_has_an_apartment_from_this": True,
        "unique_residents": {"ready_primary": unique([v for v in prim if v["can_introduce"]]), "any_primary": unique(prim), "primary_or_alternative": unique(listed)}},
        "primary_groups_without_a_listed_apartment": len([v for v in proposed_views if v["is_primary"] and not v["current_apartment_listed"]])}

    def assign(views):
        owner: dict[int, int] = {}

        def seat(i, seen):
            for a in views[i]["offered_listed"]:
                if a in seen:
                    continue
                seen.add(a)
                if a not in owner or seat(owner[a], seen):
                    owner[a] = i
                    return True
            return False
        order = sorted(range(len(views)), key=lambda i: (-views[i]["residents"], views[i]["group_id"]))
        assigned = [i for i in order if seat(i, set())]
        return {"groups": len(views), "groups_assigned_a_distinct_listed_offer": len(assigned),
                "residents": unique(views), "residents_in_those_groups": unique([views[i] for i in assigned])}
    out["hypothetical_listed_offer_assignment"] = {"note": STOCK_DIAGNOSTIC_NOTE, "offer_validity": NOT_VERIFIED, "nothing_reserved": True,
                                                   "ready_primary_groups": assign([v for v in prim if v["can_introduce"]]), "all_primary_groups": assign(prim)}
    return out


# ---------------------------------------------------------------------------------------------------- search uncertainty
_SEARCH_LIMIT_TEXT = {   # plain words for the run's own limit counters; nothing here says what WAS searched completely
    "search_targets_truncated": "some searches for a larger group ran out of their limit before finding one",
    "search_seat_options_untried": "some possible housemates beyond the per-place limit were not tried",
    "search_targets_stopped_at_first_valid": "some larger-group searches stopped at the first workable group, leaving other combinations unexplored",
    "selection_truncated": "the final choice between candidate groups stopped at its limit (best found, not proven best)",
    "coverage_selection_truncated": "a follow-up choice for people left over stopped at its limit (best found, not proven best)",
}


def _search(c, dataset: str) -> dict:
    """What the last completed full run says about how far it searched. A key the build does not write is NOT VERIFIED."""
    out = {"run_id": None, "finished_at": None, "flags": {}, "bounded_by": [], "flags_not_recorded": [], "exhaustive": NOT_VERIFIED, "uncertain": True,
           "text": "No completed matching run is recorded for this dataset, so how far the search went is " + NOT_VERIFIED + "."}
    if not _has(c, "match_runs", "summary_json") or not _has(c, "match_runs", "dataset"):
        return out
    r = rows(c.execute("SELECT id, finished_at, summary_json FROM match_runs WHERE dataset=? AND kind='full' AND status='done' ORDER BY id DESC LIMIT 1", (dataset,)))
    if not r:
        return out
    s = pj(r[0].get("summary_json"), {}) or {}
    limits = ("search_targets_truncated", "search_seat_options_untried", "search_targets_stopped_at_first_valid", "selection_truncated",
              "coverage_selection_truncated")
    required = limits + ("search_larger_groups_exhaustive", "complete")   # every one must be recorded before anything is said about completeness
    keys = required + ("search_seat_options_date_infeasible",)            # a count for information; it is not a search limit
    flags = {k: (s[k] if k in s else NOT_VERIFIED) for k in keys}

    def _count(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    bounded = [k for k in limits if (_count(flags[k]) or 0) > 0]
    # a flag that is absent, or recorded in a shape the ledger does not know, establishes nothing
    missing = [k for k in required if (flags[k] is NOT_VERIFIED or (k in limits and _count(flags[k]) is None)
                                       or (k not in limits and not isinstance(flags[k], bool)))]
    limited = bool(bounded) or flags["search_larger_groups_exhaustive"] is False or flags["complete"] is False
    if limited:      # a recorded limit is a recorded fact even when other counters are absent
        exhaustive = False
        text = ("The last run's recorded counters show its search stopped at its limits"
                + (" (" + "; ".join(_SEARCH_LIMIT_TEXT[k] for k in bounded) + ")" if bounded else "")
                + ", so it was not an exhaustive search. People without a group may still have one the search did not reach.")
        if missing:
            text += f" Some search counters were not recorded for that run ({NOT_VERIFIED})."
    elif missing:
        exhaustive = NOT_VERIFIED
        text = (f"The last run did not record all of its search counters, so the recorded counters do not establish an exhaustive search "
                f"({NOT_VERIFIED}). People without a group may still have one the search did not reach.")
    else:
        exhaustive = True
        text = "The last run's recorded counters show no search limit was reached."
    return {"run_id": r[0]["id"], "finished_at": r[0].get("finished_at"), "flags": flags, "bounded_by": bounded, "flags_not_recorded": missing,
            "exhaustive": exhaustive, "uncertain": exhaustive is not True, "text": text}


# ---------------------------------------------------------------------------------------------------- the ledger
def _change_marker(c):
    """What can be observed, without writing or taking a lock, about whether the database changed: (row changes made on THIS
    connection - the app's request handlers, scheduler and queue share it -, the counter SQLite moves when ANY OTHER connection
    commits, the schema counter, whether a write is open on this connection). Equal markers before and after a read, with no
    write open at either end, mean no row changed in between, so everything read is from one moment. SQLite only: on any other
    backend there is no marker (None) and one-moment consistency is NOT VERIFIED."""
    if getattr(c, "backend", None) != "sqlite":
        return None
    try:
        return (int(c.total_changes), int(c.execute("PRAGMA data_version").fetchone()[0]), int(c.execute("PRAGMA schema_version").fetchone()[0]),
                bool(c.in_transaction))
    except Exception:
        return None


MAX_READ_ATTEMPTS = 5


def open_frozen(path: str):
    """A read-only connection to a FROZEN SQLite file - a copy that nothing is writing - for measuring a ledger from it:
    `get_ledger(open_frozen(path), dataset)`. The database itself refuses every write on this connection, no migration or
    schema step is run, and no side file is created beside the copy. A file with unsaved write-ahead data beside it is not a
    frozen copy and is refused. The caller closes the connection. Not for the running app's database."""
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    wal = p + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise ValueError("this database file has unsaved write-ahead data beside it, so it is not a frozen copy")
    from urllib.parse import quote
    c = sqlite3.connect("file:" + quote(p) + "?mode=ro&immutable=1", uri=True, check_same_thread=False, factory=dbmod.Connection)
    c.row_factory = sqlite3.Row
    return c


def get_ledger(c, dataset: str = "real", *, today: Optional[date] = None, include_people: bool = True, feasibility: bool = True,
               attempts: int = 3) -> dict:
    """The outcome ledger for one dataset ('real' or 'test'). SELECT-only. `include_people=False` returns counts only;
    `feasibility=False` skips the per-person 'why no group' checks (those people are then labelled as not diagnosed).

    The ledger is read with many SELECTs. It takes no lock and opens no transaction (the app's shared connection must not be
    held or rolled back by a report). Instead it CHECKS, after reading, whether anything changed while it read (_change_marker)
    and reads again, up to `attempts` times, if something did. The result always says which it is in `consistency`:
    single_instant True (nothing changed: one moment), False (still changing after every attempt: figures may be from
    slightly different moments) or NOT VERIFIED (a backend with no change marker). A frozen database always reads True."""
    if dataset not in ("real", "test"):
        raise ValueError("dataset must be 'real' or 'test'")
    attempts = max(1, min(int(attempts), MAX_READ_ATTEMPTS))
    tried = 0
    while True:
        tried += 1
        started, before = now_iso(), _change_marker(c)
        out = _read_ledger(c, dataset, today=today, include_people=include_people, feasibility=feasibility)
        after = _change_marker(c)
        if before is None or after is None:
            single, text = NOT_VERIFIED, ("On this database the ledger cannot confirm that nothing changed while it was being read. "
                                          "Its figures may be from slightly different moments; they are not a picture of one instant.")
            break
        if before == after and not before[3]:
            single, text = True, "Nothing in the database changed while this was read, so every figure is from the same moment."
            break
        if tried >= attempts:
            single, text = False, (f"The database was being changed while this was read (tried {tried} time(s)). Figures may be from slightly "
                                   "different moments; read the ledger again before relying on it.")
            break
    out["consistency"] = {"single_instant": single, "checked": single != NOT_VERIFIED, "attempts": tried, "read_started_at": started, "text": text}
    if single is not True:
        out["limitations"].append(text)
    return out


def _read_ledger(c, dataset: str, *, today: Optional[date], include_people: bool, feasibility: bool) -> dict:
    """One pass of reads. Not a transaction: get_ledger decides whether the pass was consistent."""
    is_test = 1 if dataset == "test" else 0
    today = today or lease_dates.business_today()
    forms = _load_forms(c, is_test)
    groups, gmembers = _load_groups(c, is_test)
    apartments, apartments_unreadable = _load_apartments(c)
    listed = {a["id"]: a["row"] for a in apartments}

    views = {gid: _group_view(c, g, gmembers[gid], forms, listed) for gid, g in groups.items() if g["status"] != "Dissolved"}
    offered: dict[int, list] = {}
    if views:
        for r in rows(c.execute("SELECT group_id, apartment_id FROM group_apartments ORDER BY group_id, rank, apartment_id")):
            if r["group_id"] in views and r["apartment_id"] in listed:
                offered.setdefault(r["group_id"], []).append(r["apartment_id"])
    for gid, v in views.items():
        v["offered_listed"] = offered.get(gid, [])
    by_form: dict[int, list] = {}
    for gid, v in views.items():
        for eid in v["form_ids"]:
            by_form.setdefault(eid, []).append(v)

    # pool parties (linked forms kept together), for the partner checks
    pool_ids = [i for i, f in forms.items() if f["e"].get("status") in matching.POOL_STATUSES and not f["e"].get("barred_at")]
    clusters: dict[int, list] = {}
    for i in pool_ids:
        root = forms[i]["link_root"]
        clusters.setdefault(root, []).append(i)
    cluster_of = {i: ids for ids in clusters.values() for i in ids}
    rejections = {(r["rejected_enquiry_id"], r["other_enquiry_id"]) for r in rows(c.execute("SELECT rejected_enquiry_id, other_enquiry_id FROM member_rejections"))} \
        if _has(c, "member_rejections") else set()
    search = _search(c, dataset)
    memo: dict = {}
    anomalies: list[str] = []
    form_out: dict[int, dict] = {}
    for eid, f in forms.items():
        e = f["e"]
        mine = by_form.get(eid, [])
        committed = [v for v in mine if v["status"] in review_loop.COMMITTED_STATUSES]
        primaries = [v for v in mine if v["status"] == "Proposed" and v["is_primary"]]
        alternatives = [v for v in mine if v["status"] == "Proposed" and not v["is_primary"]]
        rec = {"form_id": eid, "residents": len(f["residents"]), "enquiry_status": e.get("status"), "linked_forms": f["linked_forms"],
               "adult_declaration": {"confirmed": "confirmed", "renew": "needs renewing", "missing": "missing"}.get(f["declaration"], NOT_VERIFIED),
               "group_id": None, "alternative_group_ids": [v["group_id"] for v in alternatives], "why": [], "proven": [], "checks": None}
        if committed or e.get("status") in review_loop.COMMITTED_STATUSES:
            stage_order = ("Leased", "Group confirmed", "Introduced", "Awaiting replacement")
            stage = next((s for s in stage_order if any(v["status"] == s for v in committed)), None)
            rec.update(state="introduced_or_later", stage=stage or f"{e.get('status')} ({NOT_VERIFIED}: no live group in that state was found)",
                       group_id=committed[0]["group_id"] if committed else None,
                       outcome=f"Already {('in a group that is ' + stage.lower()) if stage else 'past the proposal stage'}; this is history, not a current proposal.",
                       next_action="Follow the group's own page.")
            if not committed:
                anomalies.append(f"Enquiry {eid} is marked {e.get('status')} but no live group in that state contains it.")
        elif e.get("status") not in matching.POOL_STATUSES or e.get("barred_at"):
            rec.update(state="not_in_pool", outcome="Not being matched: " + ("barred." if e.get("barred_at") else f"the enquiry is {str(e.get('status')).lower()}."),
                       next_action="Nothing to do.")
        elif primaries:
            if len(primaries) > 1:
                anomalies.append(f"Enquiry {eid} has {len(primaries)} main proposals at once; a person can have only one. Tell a manager.")
            v = sorted(primaries, key=lambda x: (not x["can_introduce"], x["group_id"]))[0]
            state, main = _primary_state(v)
            rec.update(state=state, group_id=v["group_id"], reason_code=main["code"], outcome=main["text"], next_action=main["next_action"],
                       why=[r for r in v["reasons"] if r["code"] not in (main["code"], "held")], apartment=v["apartment"], current_apartment_listed=v["current_apartment_listed"])
            if v["hold"] and v["hold"].get("detail"):
                rec["hold_detail"] = v["hold"]["detail"]
            if v["hold"] and v["hold"]["code"] == "essential_fact_unverified":
                rec["step_free_access"] = v["step_free_access"]
            if v["hold"] and v["hold"]["code"] in _PROCESSING_CODES:   # held because the AI step could not run, not because of a decision
                rec["ai_processing_incomplete"] = True
            if state == "primary_failed_or_pending":
                rec["ai_processing_incomplete"] = any(r["code"] in _PROCESSING_CODES for r in v["reasons"])
                if main["code"] == "revision_requested" and v.get("revision_round", 0) >= v.get("revision_limit", 99):
                    rec["outcome"] += " The limit on revision rounds has been reached."
        elif alternatives:
            approved = [v for v in alternatives if v["ready"]]
            rec.update(state="alternative_only", reason_code="alternative_approved_not_selected" if approved else "alternative_not_ready",
                       outcome=(f"In {len(alternatives)} alternative candidate(s), none of which is their proposal. " +
                                (f"{len(approved)} of them is AI-approved with a staff brief, but an alternative cannot be introduced as it is."
                                 if approved else "None of them is reviewed and ready.")),
                       next_action="Nothing can be introduced yet. The next matching run or review may make one of these their proposal.")
        else:
            rec.update(state="no_proposal")
            if e.get("status") == "Proposed in group":
                anomalies.append(f"Enquiry {eid} is marked as proposed in a group but is in no current group.")
            if feasibility:
                ids = cluster_of.get(eid, [eid])
                d = _why_no_proposal(ids, forms, clusters, apartments, rejections, today, memo)
                rec["proven"], rec["checks"] = d["proven"], d["checks"]
                if d["proven"]:
                    rec.update(reason_code=d["proven"][0]["code"], outcome=d["proven"][0]["text"], next_action=d["proven"][0]["next_action"],
                               why=d["proven"][1:], proof="necessary-condition check: this alone rules out a group for them as things stand")
                else:
                    rec.update(reason_code="not_found_by_search", outcome=SEARCH_NOT_PROOF + (" " + search["text"] if search["uncertain"] else ""),
                               next_action="Nothing to correct on the enquiry. A later run, new enquiries or new stock may give them a group.",
                               proof="none: remaining search uncertainty")
            else:
                rec.update(reason_code="not_diagnosed", outcome=f"In no current group. The reason was not diagnosed in this summary ({NOT_VERIFIED}).",
                           next_action="Open the full ledger for the reason.")
        form_out[eid] = rec
    for eid, rec in form_out.items():   # a linked party is kept together: flag a split, never hide it
        if any(form_out[o]["state"] != rec["state"] or form_out[o]["group_id"] != rec["group_id"] for o in rec["linked_forms"] if o in form_out):
            rec["linked_party_split"] = True
            anomalies.append(f"Enquiry {eid} and its linked party are not in the same position; check them together.")

    hist = _history(c, is_test, forms, groups, gmembers)
    people = []
    for eid, f in forms.items():
        rec = form_out[eid]
        for r in f["residents"]:
            people.append({"resident_id": r["resident_id"], "name": r["name"], "role": r["role"], "form_id": eid, "state": rec["state"],
                           "state_label": CATEGORY_LABEL[rec["state"]], "group_id": rec["group_id"], "reason_code": rec.get("reason_code"),
                           "outcome": rec["outcome"], "also": [w["text"] for w in rec["why"]], "next_action": rec["next_action"],
                           "counts_as_ready_proposal": rec["state"] == COVERAGE_CATEGORY,
                           "history": hist["by_resident"].get(r["resident_id"], {"introduced": False, "responded": False, "accepted": False, "leased": False})})

    states = {s: {"forms": 0, "residents": 0} for s in ALL_STATES}
    for rec in form_out.values():
        states[rec["state"]]["forms"] += 1
        states[rec["state"]]["residents"] += rec["residents"]
    reasons: dict[str, dict] = {}
    for rec in form_out.values():
        if rec["state"] != COVERAGE_CATEGORY and rec.get("reason_code"):
            k = reasons.setdefault(f"{rec['state']}:{rec['reason_code']}", {"state": rec["state"], "reason_code": rec["reason_code"], "forms": 0, "residents": 0, "text": rec["outcome"]})
            k["forms"] += 1
            k["residents"] += rec["residents"]
    total_forms, total_residents = len(forms), sum(len(f["residents"]) for f in forms.values())
    assert sum(s["forms"] for s in states.values()) == total_forms and sum(s["residents"] for s in states.values()) == total_residents
    proposed_views = [v for v in views.values() if v["status"] == "Proposed"]
    in_alternative = {r for v in proposed_views if not v["is_primary"] for r in v["resident_ids"]}
    in_primary = {r for v in proposed_views if v["is_primary"] for r in v["resident_ids"]}
    hist_counts = {k: sum(1 for h in hist["by_resident"].values() if h[k]) for k in ("introduced", "responded", "accepted", "leased")}
    limitations = [
        "Only a ready proposal is counted as ready. Held, pending, failed and alternative-only people are not; a partial planning candidate never is.",
        READY_NOT_PLACEMENT,
        LISTED_REFERENCE_NOTE,
        "This build records no partial planning candidates and the ledger does not invent any, so that line is always zero here.",
        "The 'why no group' checks are necessary conditions only: they can prove that no group can be formed for a person as things stand, never that a complete group exists.",
        BUDGET_NOTE,
        "Bedding and room-arrangement fit is checked only for a complete group, so it is not part of the one-person checks.",
        "History (introduced, responded, accepted, leased) is shown separately and is never added to current proposals.",
    ] + hist["notes"]
    if _access_module() is None:
        limitations.append(f"This build has no record of apartment access facts such as step-free access: they are {NOT_VERIFIED}, not passed.")
    if apartments_unreadable:
        limitations.append(f"{apartments_unreadable} listed apartment record(s) could not be read and were left out of the stock checks ({NOT_VERIFIED}).")
    if search["uncertain"]:
        limitations.append(search["text"])
    out = {
        "schema": SCHEMA, "dataset": dataset, "business_date": today.isoformat(), "read_only": True,
        "totals": {"enquiry_forms": total_forms, "residents": total_residents,
                   "linked_parties": len({f["link_root"] for f in forms.values() if f["linked_forms"]}),
                   "forms_in_linked_parties": sum(1 for f in forms.values() if f["linked_forms"])},
        "states": {s: {**states[s], "label": CATEGORY_LABEL[s]} for s in ALL_STATES},
        "coverage": {
            "ready_proposal_residents": states[COVERAGE_CATEGORY]["residents"], "ready_proposal_forms": states[COVERAGE_CATEGORY]["forms"],
            "ready_proposal_means": "in the one proposed group for them, which staff may introduce; nothing else is counted",
            "not_a_placement": READY_NOT_PLACEMENT,
            "residents_in_any_primary_proposal": len(in_primary),
            "residents_in_any_alternative": len(in_alternative),
            "residents_in_primary_or_alternative": len(in_primary | in_alternative),
            "union_note": "The primary-or-alternative figure is a union of candidates that overlap each other; it is not a number of people with a ready proposal, and not a number of people with an apartment.",
            "planning_only_residents": states["planning_only"]["residents"]},
        "unresolved_reasons": sorted(reasons.values(), key=lambda r: (-r["residents"], r["state"], r["reason_code"])),
        "history": {"note": "Already happened; separate from current proposals and never added to them.", "residents": hist_counts},
        "stock": _stock(proposed_views),
        "search": search, "anomalies": anomalies, "limitations": limitations,
    }
    if include_people:
        out["people"] = people
        out["forms"] = [form_out[i] for i in sorted(form_out)]
        out["groups"] = [{k: v[k] for k in ("group_id", "status", "is_primary", "kind", "form_ids", "residents", "apartment_id", "apartment", "current_apartment_listed", "stored_offer_validity",
                                            "step_free_access", "ready", "can_introduce", "reasons", "hold")} for v in views.values()]
    return out


def summary(c, dataset: str = "real", *, today: Optional[date] = None, attempts: int = 3) -> dict:
    """Counts only (no names, no per-person diagnosis): for a dashboard tile or the match summary."""
    return get_ledger(c, dataset, today=today, include_people=False, feasibility=False, attempts=attempts)
