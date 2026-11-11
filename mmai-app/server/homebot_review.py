"""HomeBot grounded AI review (H7/H14 hook into the H2 shortlist workflow; lead slice, 9 November 2026).

What this module is
  The integration builder's shortlist (server/homebot_shortlist.py) is a RULES result: which apartments pass the party's firm
  requirements, what each would cost at which dates, and which points still need confirming. T6/H7 require an evidence-supported
  Sonnet review before anything is treated as reviewed; the shortlist module calls two hooks (exposed through server/ai_queue.py):
      ai_queue.enqueue_homebot_review(c, shortlist_id, commit=False)   # after the shortlist commit, inside ITS guarded step
      ai_queue.homebot_review_state(c, shortlist_id) -> {"eligible": bool, "reviewed": dict | None}
  Everything else here is the queue behind those hooks: a pending row per shortlist version, the grounded packet, the model call,
  the validator, the guarded publication, and the read-side recheck. The existing AI queue worker consumes it (ai_queue._iteration
  -> consume): bounded batches per wake, only while the HomeBot matching gate is ON; MMAI_AI_QUEUE=0 means no automatic call.

What the review may and may not do (binding boundaries)
  * It reviews the shortlist AS COMPUTED. It cannot change an apartment's class, clear a confirmation reason, resolve an
    unresolved date search, or add an option. Its output has no field for any of that; every option note is "agrees / disagrees
    with the rules result" plus a grounded note. The stored shortlist (result_json) is never written here.
  * Unknowns stay unknown. Travel that was not measured, an essential need written in the notes, dates the automatic search
    could not cover: the packet states each as an OPEN point, and a reply that asserts such a point is verified / suitable /
    fine is an INVALID reply (validator), not a review - and so is a reply that states a fact about such a point (a travel time,
    a lift, step-free access, dates or cost) without marking it unknown / to be checked. Locally scoped clause analysis, in every
    prose location (option notes, findings, rationale). The read side lists the open points FROM THE RULES, never from the model.
  * Readiness: `homebot_shortlist` keeps `ready = False` for every shortlist. This module does not flip it. It publishes the
    explicit conditions a later ruling could bind readiness to (READY_CONDITIONS) and reports which hold, as data; no caller here
    acts on them. (Root, 9 Nov: the existing ready=False boundary only changes with explicit well-defined conditions.)
  * Freshness: `is_current=1` is only the latest stored-run pointer. Before a result is STORED as reviewed, and again every time
    the state is READ, the shortlist must be current, its basis must still apply (the shortlist module's own freshness check,
    `freshness` / `_stale`: requirement version, applicable planning, inventory) and the bound `version` fingerprint must still
    match the row. Otherwise the review is STALE, never "reviewed". When the shortlist module offers no freshness check at
    all, nothing is ever reported as reviewed (fail closed).
  * Lifecycle (same discipline as the H2 jobs): a durable compare-and-set CLAIM (state pending -> running, owner token, lease) inside
    a guarded capture step, so exactly one consumer calls the model for a row; the provider call OUTSIDE any guard; a guarded apply
    step whose first statement is the owner FENCE and which re-evaluates enquiry status, version and freshness in the SAME
    transaction as the publication write. A lost fence drops the result; an expired lease (CLAIM_TTL_S) may be taken over; a
    Closed / Leased enquiry is never reviewed. Retries after RETRY_AFTER_S, attempts counted at claim time, bounded.
  * Failure sinks hold closed sentences only (FAILURE_TEXT); the exception's words go nowhere (T5 ruling, Modified A).

Schema (PROPOSED - DDL is the integration builder's; `PROPOSED_DDL` below is the exact text handed over; tests create it locally)
  homebot_reviews(id, shortlist_id -> homebot_shortlists ON DELETE CASCADE, enquiry_id, state, version, attempts, owner, claimed_at,
                  verdict, review_json, error, model, rule_version, contract, created_at, updated_at, reviewed_at; UNIQUE(shortlist_id, version))
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
import threading
import json
import logging
import os
import re
import secrets
from typing import Optional

from . import ai, rules
from . import db
from .db import j, maybe_commit, now_iso, pj, row, rows

log = logging.getLogger("mmai.homebot_review")

TABLE = "homebot_reviews"
PROPOSED_DDL = """
CREATE TABLE IF NOT EXISTS homebot_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shortlist_id INTEGER NOT NULL REFERENCES homebot_shortlists(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  state TEXT NOT NULL,                 -- pending | running | reviewed | invalid | failed | stale   (closed: STATES)
  version TEXT NOT NULL,               -- fingerprint of the shortlist basis this review is bound to (shortlist_version)
  attempts INTEGER NOT NULL DEFAULT 0,
  owner TEXT,                          -- claim token of the consumer that is running this row (CAS claim; every write is fenced on it)
  claimed_at TEXT,                     -- lease start; a claim older than CLAIM_TTL_S may be taken over (crashed consumer)
  verdict TEXT,                        -- approve | hold (reviewed rows only; closed: VERDICTS)
  review_json TEXT,                    -- the validated review: intentional generated text, retained with the enquiry
  error TEXT,                          -- closed sentence only (FAILURE_TEXT); never an exception's words
  model TEXT,
  rule_version TEXT,
  contract TEXT,                       -- REVIEW_CONTRACT the reply was validated against
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_at TEXT,
  UNIQUE(shortlist_id, version)
);
CREATE INDEX IF NOT EXISTS ix_homebot_reviews_state ON homebot_reviews(state);
"""

# closed vocabularies -------------------------------------------------------------------------------------------------------
PENDING, RUNNING, REVIEWED, INVALID, FAILED, STALE = "pending", "running", "reviewed", "invalid", "failed", "stale"
STATES = frozenset((PENDING, RUNNING, REVIEWED, INVALID, FAILED, STALE))
APPROVE, HOLD = "approve", "hold"
VERDICTS = frozenset((APPROVE, HOLD))
REVIEW_CONTRACT = "homebot-review-v1"
MAX_ATTEMPTS = 3
FINDINGS_MAX = 10
NOTE_MAX, RATIONALE_MAX = 300, 600
OUTPUT_TOKENS = 1800
# automatic consumer (runs inside the existing AI queue worker, ai_queue._iteration): rows per batch, batches per wake, and the
# delay before a row that stays pending after an attempt (provider failure / unusable reply) is tried again
BATCH = int(os.environ.get("MMAI_HOMEBOT_REVIEW_BATCH", "5"))
BATCHES_PER_WAKE = int(os.environ.get("MMAI_HOMEBOT_REVIEW_BATCHES", "3"))
RETRY_AFTER_S = int(os.environ.get("MMAI_HOMEBOT_REVIEW_RETRY_S", "60"))
CLAIM_TTL_S = int(os.environ.get("MMAI_HOMEBOT_REVIEW_CLAIM_TTL_S", "600"))   # a running claim older than this is a crashed consumer's
HEARTBEAT_SEC = float(os.environ.get("MMAI_HOMEBOT_REVIEW_HEARTBEAT_S", "60"))  # while the provider call runs, the owner renews its lease (owner-fenced
# write, same discipline as the H2 shortlist jobs' heartbeat): a healthy slow call never loses its lease just by outlasting CLAIM_TTL_S; a
# consumer that DID lose it (paused past the TTL, another consumer took over) learns so and drops its result. The model is not retuned.
HEARTBEAT_DB_TIMEOUT_MS = int(os.environ.get("MMAI_HOMEBOT_REVIEW_HEARTBEAT_DB_TIMEOUT_MS", "1000"))  # bound on BOTH waits of one beat: the
# connection lock and the database lock (PG lock_timeout / SQLite busy_timeout, via db.bounded_write). A beat that cannot get in within
# the bound is skipped, never queued behind unknown work.
RUNNING_TEXT = "The AI review is in progress; options are held until it completes."

FAILURE_TEXT = {
    "provider": "The AI service call failed (provider or network error); the options were not reviewed.",
    "size_limit": "The AI reply exceeded the response size limit after a compact retry; no further automatic retry - staff decide.",
    "unusable": "AI review unusable: the reply failed the output validator; the options were not reviewed.",
    "stale": "The options changed while the AI review was being prepared; the review was discarded and the fresh options are reviewed instead.",
    "packet": "The options could not be assembled for review (missing stored result or requirements); the options were not reviewed.",
    "unexpected": "An unexpected error stopped the AI review step; the options were not reviewed.",
    "credentials": "No AI credentials are configured in this process; the options were not reviewed.",
    "setup": "The AI SDK is not installed in this process; the options were not reviewed.",
    "freshness_unavailable": "The options' freshness cannot be checked in this build; nothing is reported as reviewed.",
    "closed": "The enquiry is closed or leased; its options are not reviewed.",
}
CLOSED_FAILURE_SENTENCES = frozenset(FAILURE_TEXT.values())

# the rules-side codes that mark an OPEN point the model may never close (homebot_match confirmation reasons + unresolved search)
OPEN_POINT_CODES = ("TRAVEL_NEEDS_CONFIRMATION", "ESSENTIAL_NOTE_REVIEW", "DATE_SEARCH_UNRESOLVED", "BEDDING_NEEDS_CONFIRMATION",
                    "BUNK_COMPARISON_UNAVAILABLE", "BUNK_CHEAPER_OTHER_DATES", "PROVISIONAL_FINAL_SIZE", "FOURTH_OPTION_PROVISIONAL")
OPEN_POINT_TOPIC = {   # words a note about THAT open point would use; used only to detect an assertion that the point is settled
    "TRAVEL_NEEDS_CONFIRMATION": r"travel|journey|journeys|commute|commuting|commutes|trip|transport|distance|walk|walking|tram|train|bus|minutes? away|close to campus|near campus",
    "ESSENTIAL_NOTE_REVIEW": r"essential|step[- ]free|accessib\w*|wheelchair|lift|lifts|elevator|ramp|ground floor|stairs|need|needs|requirement|notes?",
    "DATE_SEARCH_UNRESOLVED": r"date|dates|move[- ]in|move[- ]out|lease|cost|price|rent",
    "BEDDING_NEEDS_CONFIRMATION": r"bed|beds|bedding|sleep\w*|room|rooms",
    "BUNK_COMPARISON_UNAVAILABLE": r"bunk|cheaper|price|cost",
    "BUNK_CHEAPER_OTHER_DATES": r"bunk|cheaper|price|cost",
    "PROVISIONAL_FINAL_SIZE": r"final size|future member\w*|planning|capacity|beds",
    "FOURTH_OPTION_PROVISIONAL": r"fourth|comparab\w*|variety",
}
# settling predicates: the words a sentence uses to say an open point is closed (past participles / evaluative adjectives)
_SETTLED = re.compile(r"\b(verified|confirmed|measured|resolved|settled|checked|meets|met|satisf(?:ied|ies|actory)|"
                      r"within (?:the )?(?:\w+ )?(?:limit|ceiling|preference|budget|time|range)|well within|"
                      r"suitable|fine|acceptable|ok|okay|no (?:issue|problem|concern)s?|works?|fits?|comfortabl\w+|is (?:short|fast|quick|close|cheap|affordable))\b", re.I)
# a negation / pending marker counts ONLY when it is local to the settling predicate: within the few words before it in the same
# clause ("is not confirmed", "has not been measured", "needs to be confirmed", "still to be checked"). Idioms that merely
# intensify ("no doubt", "not only", "no question") are not negations.
_LOCAL_NEG = re.compile(r"\b(not|never|none|nor|cannot|can't|isn't|aren't|hasn't|haven't|wasn't|weren't|won't|doesn't|don't|"
                        r"un(?:confirmed|measured|verified|resolved|checked|known)|to be|still|yet|until|before|pending|whether|if|"
                        r"must be|should be|may be|might be|could be|would be|awaiting|outstanding)\b", re.I)
_NEG_IDIOM = re.compile(r"\b(no doubt|no question|not only|without doubt|no wonder)\b", re.I)
# a clause that opens with a determiner negation, a question word or an imperative to check is not an assertion that the point is settled
_CLAUSE_NEGATED_OPEN = re.compile(r"^\s*(no|none of|neither|nothing|nobody|whether|if|confirm|check|verify|measure|review|ask|please)\b(?!\s+(?:doubt|question|wonder)\b)", re.I)
_CLAUSE = re.compile(r"\s*[;:.!?]\s*|\s+-\s+|\s+\u2014\s+|,\s*|\s+(?=(?:and|but|while|whereas|although|though|yet|so|which|because|since|whilst)\b)")
_NEG_WINDOW_WORDS = 4
# factual claims about an open point (the second kind of false assurance): a stated measurement / amount / date, or a copula /
# possession / availability predicate, in a clause that names the topic and carries no non-assertive marker anywhere
_NUMBER = r"(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|ninety|half an?|a few|several|a couple of)"
_MEASURE = re.compile(rf"\b{_NUMBER}\s*(?:-\s*)?(?:minutes?|mins?|hours?|hrs?|km|kilomet\w+|metres?|meters?|miles?|stops?|stations?|blocks?|"
                      rf"weeks?|months?|nights?|days?|dollars?|per week|pw|a week|%)\b|\$\s?\d|\b\d{{1,2}}(?:st|nd|rd|th)? (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b", re.I)
_CLAIM_VERB = re.compile(r"\b(is|are|was|were|has|have|had|offers?|includes?|features?|provides?|comes? with|takes?|lasts?|costs?|runs? from|"
                         r"available|provided|present|exists?|equipped|fitted|located|sits?|lies?)\b", re.I)
_NON_ASSERTIVE = re.compile(r"\b(not|never|none|nor|cannot|can't|isn't|aren't|hasn't|haven't|wasn't|weren't|won't|doesn't|don't|no|"
                            r"un(?:confirmed|measured|verified|resolved|checked|known|clear|certain)|unknown|not known|unclear|to be|still|yet|until|before|"
                            r"pending|whether|if|must|should|need\w*|awaiting|outstanding|subject to|remains?|open|check\w*|confirm\w*|verify\w*|"
                            r"measur\w*|ask\w*|question\w*|unsure|uncertain|would|could|might|may)\b", re.I)

# Facts the staff screen may show; NOT a readiness decision. Nothing in this module (or in what it returns) says 'ready': the
# shortlist module keeps out["ready"] = False and only an explicit, well-defined readiness rule may ever change that.
READY_CONDITIONS = (
    "review_state_reviewed",        # a review bound to this exact shortlist version is stored
    "verdict_approve",              # the model agreed the computed shortlist is consistent with the recorded facts
    "fresh_now",                    # the shortlist is current and its basis still applies at read time
    "has_listed_options",           # the stored result lists at least one deliverable option (an unresolved-only result lists none)
    "no_open_points",               # no listed / fourth / unresolved option carries a confirmation reason or unresolved search (rules-side)
)


class UnusableReview(ai.AIUnavailable):
    """The model answered but the reply failed this module's validator. The message is in-process diagnosis only."""


class PacketIncomplete(ai.AIUnavailable):
    """The stored shortlist cannot be turned into a review packet (no stored result / requirements). Terminal for the row."""


# shortlist module adapter ---------------------------------------------------------------------------------------------------
def _hs():
    from . import homebot_shortlist
    return homebot_shortlist


_reentry = threading.local()


def freshness(c, sl: dict) -> Optional[str]:
    """The shortlist module's own out-of-date verdict for a stored shortlist (None = its basis still applies), or
    FAILURE_TEXT['freshness_unavailable'] when the module offers no such check (fail closed: never 'fresh' by default).
    Binds to the public `freshness` when the integration builder exposes it, else to today's `_stale`.
    Re-entrancy: today's `_stale` reads the enquiry through homebot.get, whose matching note asks THIS module for the review
    state again; the nested call answers 'not reviewed' (see homebot_review_state) instead of recursing."""
    hs = _hs()
    fn = getattr(hs, "freshness", None) or getattr(hs, "_stale", None)
    if fn is None:
        return FAILURE_TEXT["freshness_unavailable"]
    if not sl.get("is_current"):
        return "Out of date: a newer run replaced these options."
    _reentry.depth = getattr(_reentry, "depth", 0) + 1
    try:
        return fn(c, sl)
    finally:
        _reentry.depth -= 1


def _shortlist(c, sid: int) -> Optional[dict]:
    return row(c.execute("SELECT * FROM homebot_shortlists WHERE id=?", (sid,)))


def _enquiry_open(c, eid: int) -> bool:
    """Only an open enquiry (the shortlist module's OPEN_STATUSES: not Leased / Closed) has its options reviewed."""
    e = row(c.execute("SELECT status FROM homebot_enquiries WHERE id=?", (eid,)))
    return bool(e) and e["status"] in tuple(_hs().OPEN_STATUSES)


def shortlist_version(sl: dict) -> str:
    """Fingerprint of everything the review is about: requirement version + basis + inventory snapshot + the offers as listed
    (apartment, class, offered dates and total weekly rent, open points). Any change makes a stored review STALE."""
    res = pj(sl.get("result_json"), {}) or {}
    offers = []
    for key in ("listed", "alternatives", "unresolved"):
        for x in res.get(key) or []:
            o = x.get("offer") or {}
            offers.append([key, x.get("apartment_id"), x.get("class"), o.get("start"), o.get("end"), o.get("total_weekly_rent"),
                           sorted(x.get("confirmation_reasons") or []), bool(x.get("date_search_unresolved"))])
    f = res.get("fourth_option") or {}
    basis = {"requirement_version_id": sl.get("requirement_version_id"), "basis_hash": sl.get("basis_hash"), "inventory_hash": sl.get("inventory_hash"),
             "algorithm_version": sl.get("algorithm_version"), "status": sl.get("status"), "offers": offers,
             "fourth": [f.get("apartment_id"), (f.get("offer") or {}).get("total_weekly_rent")] if f else None}
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode()).hexdigest()[:32]


# hooks (exposed through ai_queue) -------------------------------------------------------------------------------------------
def enqueue_homebot_review(c, shortlist_id: int, commit: bool = False) -> dict:
    """Record that shortlist `shortlist_id` wants a grounded review. Inside the CALLER's guarded step (the shortlist module
    commits or rolls back together with its own hand-over mark). Idempotent per (shortlist, version). No model call here.
    Raises on a missing / non-current shortlist so the caller's step fails visibly (it retries, bounded)."""
    sl = _shortlist(c, shortlist_id)
    if sl is None:
        raise LookupError("shortlist not on file")   # in-process only; the caller records its own closed category
    if not sl.get("is_current"):
        return {"shortlist_id": shortlist_id, "outcome": "superseded"}
    version = shortlist_version(sl)
    existing = row(c.execute(f"SELECT id, state FROM {TABLE} WHERE shortlist_id=? AND version=?", (shortlist_id, version)))
    if existing:
        return {"shortlist_id": shortlist_id, "review_id": existing["id"], "state": existing["state"], "outcome": "already_queued"}
    when = now_iso()
    c.execute(f"""INSERT INTO {TABLE}(shortlist_id, enquiry_id, state, version, attempts, model, rule_version, contract, created_at, updated_at)
                  VALUES (?,?,?,?,0,?,?,?,?,?)""", (shortlist_id, sl["enquiry_id"], PENDING, version, ai.MODEL, rules.RULE_VERSION, REVIEW_CONTRACT, when, when))
    rid = row(c.execute(f"SELECT id FROM {TABLE} WHERE shortlist_id=? AND version=?", (shortlist_id, version)))["id"]
    if commit:
        maybe_commit(c)
    _kick()   # wake the AI queue worker (an Event only; the worker reads the row after the caller's step has committed)
    return {"shortlist_id": shortlist_id, "review_id": rid, "state": PENDING, "outcome": "queued"}


def _kick() -> None:
    try:
        from . import ai_queue
        ai_queue.kick()
    except Exception:  # noqa: BLE001  - waking the worker is best effort; the idle poll picks the row up anyway
        pass


def homebot_review_state(c, shortlist_id: int) -> dict:
    """Read-side state for the shortlist module: {"eligible": bool, "reviewed": dict | None, "state": ..., "plain": ...}.
    `reviewed` is set ONLY when a review bound to the shortlist's CURRENT version fingerprint is stored as reviewed AND the
    shortlist is fresh right now. It never contains `ready`; it lists the rules-side open points and which READY_CONDITIONS hold."""
    sl = _shortlist(c, shortlist_id)
    if sl is None:
        return {"eligible": False, "reviewed": None, "state": "missing"}
    if getattr(_reentry, "depth", 0):
        # asked from inside our own freshness check (enquiry read -> matching note -> review state): fail closed, no recursion
        return {"eligible": bool(sl.get("is_current")), "reviewed": None, "state": "checking"}
    version = shortlist_version(sl)
    r = row(c.execute(f"SELECT * FROM {TABLE} WHERE shortlist_id=? AND version=?", (shortlist_id, version)))
    stale = freshness(c, sl) if _enquiry_open(c, sl["enquiry_id"]) else FAILURE_TEXT["closed"]
    open_points = _open_points(sl)
    approved = bool(r and r["state"] == REVIEWED and r["verdict"] == APPROVE and stale is None)
    out = {"eligible": bool(sl.get("is_current")), "reviewed": None, "version": version, "stale": stale,
           "state": r["state"] if r else "not_queued", "attempts": int(r["attempts"]) if r else 0,
           # the staff screen reads these in EVERY state (reviewer's UI): they are facts about the stored review and the rules result,
           # never a readiness decision (no 'ready' key exists here by design); an open point stays listed whatever the model said
           "summary": {"approved": approved, "fresh": stale is None, "has_listed_options": _has_listed(sl), "open_points": open_points,
                       "all_ready_conditions_hold": approved and _has_listed(sl) and not open_points}}
    if r and r["state"] in (INVALID, FAILED, STALE):
        out["plain"] = r["error"] if r["error"] in CLOSED_FAILURE_SENTENCES else FAILURE_TEXT["unexpected"]
    elif r and r["state"] == RUNNING:
        out["plain"] = RUNNING_TEXT
    if not r or r["state"] != REVIEWED:
        return out
    if stale is not None:
        out["state"] = STALE
        out["plain"] = stale
        return out
    review = pj(r["review_json"], {}) or {}
    conditions = {"review_state_reviewed": True, "verdict_approve": r["verdict"] == APPROVE, "fresh_now": True,
                  "has_listed_options": _has_listed(sl), "no_open_points": not open_points}
    out["reviewed"] = {"verdict": r["verdict"], "rationale": review.get("rationale"), "findings": review.get("findings") or [],
                       "option_notes": review.get("option_notes") or [], "reviewed_at": r["reviewed_at"], "model": r["model"],
                       "rule_version": r["rule_version"], "contract": r["contract"], "open_points": open_points,
                       "ready_conditions": conditions, "all_ready_conditions_hold": all(conditions.values()),
                       "plain": ("AI review complete: the options are consistent with the recorded facts; staff confirm the open points listed."
                                 if r["verdict"] == APPROVE else "AI review complete: hold for staff - the review found points that conflict with the recorded facts.")}
    return out


def _has_listed(sl: dict) -> bool:
    res = pj(sl.get("result_json"), {}) or {}
    return bool(res.get("listed"))


def _open_points(sl: dict) -> list[dict]:
    """Rules-side open points of the listed options, the fourth option AND the unresolved candidates (an unresolved-only result has
    no listed option but is not 'free of open points'): confirmation reasons and unresolved date searches, in the shortlist
    module's plain words. Computed from the stored result, never from the model."""
    from . import homebot_match as hm
    res = pj(sl.get("result_json"), {}) or {}
    out = []
    items = list(res.get("listed") or []) + ([res["fourth_option"]] if res.get("fourth_option") else [])
    for x in items:
        codes = list(x.get("confirmation_reasons") or []) + (["DATE_SEARCH_UNRESOLVED"] if x.get("date_search_unresolved") else [])
        for code in codes:
            out.append({"apartment_id": x.get("apartment_id"), "code": code, "plain": hm.PLAIN.get(code, code)})
    for x in res.get("unresolved") or []:
        codes = list(x.get("confirmation_reasons") or [])
        if "DATE_SEARCH_UNRESOLVED" not in codes:
            codes.append("DATE_SEARCH_UNRESOLVED")
        for code in codes:
            out.append({"apartment_id": x.get("apartment_id"), "code": code, "plain": hm.PLAIN.get(code, code), "listed_as": "unresolved"})
    return out


# packet -------------------------------------------------------------------------------------------------------------------
def packet(c, sl: dict) -> dict:
    """The exact JSON the model sees (also used by tests). Facts carry ids the reply must cite. Nothing about the people beyond
    their matching answers (position, sleeping choice, what they do, institution, destination); no names, no contacts."""
    ver = row(c.execute("SELECT basis_json FROM homebot_requirement_versions WHERE id=?", (sl["requirement_version_id"],)))
    basis = pj((ver or {}).get("basis_json"), {}) or {}
    res = pj(sl.get("result_json"), {}) or {}
    if not basis or not res:
        raise PacketIncomplete("homebot review packet: stored result or requirements missing")
    if res.get("route_evidence"):
        from . import routing
        if routing.stale_evidence(res["route_evidence"]):
            raise PacketIncomplete("routing evidence is no longer authorised or fresh")
    facts: dict[str, object] = {}
    for k in ("scenario", "head_count", "bedroom_sizes", "priced_types", "bathrooms", "area", "transport", "max_travel", "extras", "extras_top",
              "whole_apartment_max", "move_in", "move_in_window", "move_out", "move_out_window", "bunk_condition", "notes_review"):
        if k in basis:
            facts[f"R.{k}"] = basis[k]
    for i, p in enumerate(basis.get("people") or [], 1):
        facts[f"R.person{i}"] = {k: v for k, v in (p or {}).items() if k != "name"}
    options = []

    def _opt(x: dict, listed_as: str) -> dict:
        ref = f"O{len(options) + 1}"
        codes = list(x.get("confirmation_reasons") or []) + (["DATE_SEARCH_UNRESOLVED"] if x.get("date_search_unresolved") else [])
        from . import homebot_match as hm
        o = {"ref": ref, "listed_as": listed_as, "apartment_id": x.get("apartment_id"), "class": x.get("class"), "type_code": x.get("type_code"),
             "bathrooms": x.get("bathrooms"), "suburb": x.get("suburb"), "offer": x.get("offer"), "cheapest_offer": x.get("cheapest_offer"),
             "travel": x.get("travel"), "bedding": x.get("bedding"), "features": x.get("features"), "bunk_comparison": x.get("bunk_comparison"),
             "exclusion_reasons": [hm.PLAIN.get(k, k) for k in x.get("exclusion_reasons") or []],
             "open_points": [{"code": k, "plain": hm.PLAIN.get(k, k)} for k in codes],
             "notes": [hm.PLAIN.get(k, k) for k in x.get("flags") or []]}
        for k in ("class", "offer", "cheapest_offer", "travel", "bedding", "features", "bunk_comparison", "open_points", "notes", "exclusion_reasons"):
            facts[f"{ref}.{k}"] = o[k]
        options.append(o)
        return o

    for x in res.get("listed") or []:
        _opt(x, "listed")
    if res.get("fourth_option"):
        _opt(res["fourth_option"], "fourth_option")
    for x in res.get("alternatives") or []:
        _opt(x, "alternative")
    for x in res.get("unresolved") or []:
        _opt(x, "unresolved")
    return {"shortlist_id": sl["id"], "scenario": sl["scenario"], "status": sl["status"], "contract": REVIEW_CONTRACT,
            "requirements": {k: v for k, v in facts.items() if k.startswith("R.")}, "options": options, "fact_ids": sorted(facts),
            "open_point_codes": list(OPEN_POINT_CODES)}


SYSTEM = ("You are HomeBot AI, Apartment Living Melbourne's apartment-matching reviewer. You check a rules-based shortlist against the "
          "recorded facts only. You never invent facts, never assume an unknown feature exists, and never treat an unmeasured journey, "
          "an unresolved date search or an essential need written in the notes as settled.")


def build_prompt(pk: dict, compact: bool = False) -> str:
    rulesx = [
        "Review the shortlist AS COMPUTED. You cannot change an apartment's class, add or remove an option, or close an open point.",
        "Every open point listed under an option stays open: do not say travel, dates, cost, bedding or an essential need is verified, suitable, fine or within limits for that option. You may say it needs confirming.",
        "Cite fact ids from fact_ids for every finding. A finding without a cited fact is invalid.",
        f"Return ONE JSON object only: {{\"verdict\": \"approve\" | \"hold\", \"rationale\": string (<= {RATIONALE_MAX} chars), "
        f"\"findings\": [{{\"fact_ids\": [string, ...], \"note\": string (<= {NOTE_MAX} chars)}}] (at most {FINDINGS_MAX}), "
        "\"option_notes\": [{\"ref\": string, \"agrees\": true | false, \"note\": string}] (one per option in the packet)}.",
        "'approve' = the listed options and their order are consistent with the recorded facts and the stated open points; 'hold' = a listed option, its order or its stated reasons conflict with a recorded fact (cite it).",
        "Plain words, no rule codes, no names, no nationality or ethnicity, no gendered pronouns.",
    ]
    if compact:
        rulesx.append("Be brief: one sentence per note; rationale at most two sentences.")
    return "Rules:\n- " + "\n- ".join(rulesx) + "\n\nPacket:\n\n" + json.dumps(pk, ensure_ascii=False, default=str)


# validator ------------------------------------------------------------------------------------------------------------------
def _asserts_settled(note: str, topic: str) -> Optional[str]:
    """The clause of `note` that asserts an open point of kind `topic` is settled, else None. Locally scoped: a clause must name
    the topic AND contain a settling predicate that is not negated / qualified by a marker within the few words before it
    (idioms such as 'no doubt' or 'not only' are not negations; a clause opening with 'No …', 'Whether …' or an imperative
    'Confirm …' is not an assertion). Other clauses of the same sentence never exempt it."""
    topic_re = re.compile(rf"\b(?:{topic})\b", re.I)
    for clause in _CLAUSE.split(note or ""):
        if not clause or not topic_re.search(clause):
            continue
        if _CLAUSE_NEGATED_OPEN.search(clause):
            continue
        for m in _SETTLED.finditer(clause):
            if m.group(0).lower().startswith("no "):      # 'no issue' is itself the settling predicate
                return clause.strip()
            head = clause[:m.start()]
            head = _NEG_IDIOM.sub(" ", head)
            window = " ".join(head.split()[-_NEG_WINDOW_WORDS:])
            if _LOCAL_NEG.search(window):
                continue
            return clause.strip()
    return None


def _asserts_fact(note: str, topic: str) -> Optional[str]:
    """The clause of `note` that states a fact about an open point - a measurement, amount or date, or a copula / possession /
    availability claim ('the commute takes 12 minutes', 'a lift is available', 'the apartment has step-free access') - with no
    non-assertive marker (negation, unknown, pending, question, imperative to check) anywhere in the clause. Such a clause invents
    or presumes what the rules recorded as open."""
    topic_re = re.compile(rf"\b(?:{topic})\b", re.I)
    for clause in _CLAUSE.split(note or ""):
        if not clause or not topic_re.search(clause):
            continue
        if _CLAUSE_NEGATED_OPEN.search(clause):
            continue
        body = _NEG_IDIOM.sub(" ", clause)
        if _NON_ASSERTIVE.search(body):
            continue
        if _MEASURE.search(body) or _CLAIM_VERB.search(body):
            return clause.strip()
    return None


def _upgrade_check(texts: list[str], codes: list[str], where: str) -> None:
    for code in codes:
        topic = OPEN_POINT_TOPIC.get(code)
        if not topic:
            continue
        for t in texts:
            hit = _asserts_settled(t, topic)
            if hit:
                raise UnusableReview(f"{where}: asserts the open point {code} is settled ({hit[:80]})")
            hit = _asserts_fact(t, topic)
            if hit:
                raise UnusableReview(f"{where}: states a fact about the open point {code} ({hit[:80]})")


def parse_review(text: str, pk: dict) -> dict:
    """Validate the reply against the packet. Returns the clean review dict or raises UnusableReview (in-process diagnosis)."""
    try:
        doc = ai._json_from(text)
    except ai.AIUnavailable as e:   # no / malformed JSON is an unusable reply of this step, not a provider failure
        raise UnusableReview(str(e))
    if not isinstance(doc, dict):
        raise UnusableReview("reply is not a JSON object")
    extra = set(doc) - {"verdict", "rationale", "findings", "option_notes"}
    if extra:
        raise UnusableReview(f"unexpected keys: {sorted(extra)}")
    if doc.get("verdict") not in VERDICTS:
        raise UnusableReview("verdict not approve/hold")
    rationale = doc.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > RATIONALE_MAX:
        raise UnusableReview("rationale missing or too long")
    fact_ids = set(pk.get("fact_ids") or [])
    findings = doc.get("findings")
    if not isinstance(findings, list) or len(findings) > FINDINGS_MAX:
        raise UnusableReview("findings missing or more than the maximum")
    clean_f = []
    for i, f in enumerate(findings):
        ids = f.get("fact_ids") if isinstance(f, dict) else None
        note = f.get("note") if isinstance(f, dict) else None
        if not isinstance(ids, list) or not ids or not all(isinstance(x, str) and x in fact_ids for x in ids):
            raise UnusableReview(f"findings[{i}]: fact_ids must be non-empty ids from the packet")
        if not isinstance(note, str) or not note.strip() or len(note) > NOTE_MAX:
            raise UnusableReview(f"findings[{i}]: note missing or too long")
        clean_f.append({"fact_ids": list(ids), "note": note.strip()})
    options = {o["ref"]: o for o in pk.get("options") or []}
    notes = doc.get("option_notes")
    if not isinstance(notes, list):
        raise UnusableReview("option_notes missing")
    seen, clean_o = set(), []
    for i, n in enumerate(notes):
        if not isinstance(n, dict) or n.get("ref") not in options or n["ref"] in seen:
            raise UnusableReview(f"option_notes[{i}]: unknown or repeated option ref")
        if not isinstance(n.get("agrees"), bool) or not isinstance(n.get("note"), str) or len(n["note"]) > NOTE_MAX:
            raise UnusableReview(f"option_notes[{i}]: agrees must be true/false and note a short string")
        seen.add(n["ref"])
        clean_o.append({"ref": n["ref"], "apartment_id": options[n["ref"]]["apartment_id"], "agrees": n["agrees"], "note": n["note"].strip()})
    missing = set(options) - seen
    if missing:
        raise UnusableReview(f"option_notes: no note for {sorted(missing)}")
    # the upgrade guard - every prose location: an option's open points may not be asserted settled in that option's note or in a
    # finding that cites it; the rationale and findings that cite requirements only (no option) are UNSCOPED prose and may not
    # assert ANY listed option's open point settled (an unmeasured matter cannot become settled by saying so in general terms)
    all_codes = sorted({op["code"] for o in options.values() for op in (o.get("open_points") or [])})
    for ref, o in options.items():
        texts = [n["note"] for n in clean_o if n["ref"] == ref] + [f["note"] for f in clean_f if any(x.startswith(ref + ".") for x in f["fact_ids"])]
        _upgrade_check(texts, [op["code"] for op in o.get("open_points") or []], ref)
    unscoped = [rationale] + [f["note"] for f in clean_f if not any(x.startswith("O") and "." in x for x in f["fact_ids"])]
    _upgrade_check(unscoped, all_codes, "rationale/requirements")
    # wording boundaries shared with MateBot (gendered pronouns prohibited; origin wording never)
    for t in [rationale] + [f["note"] for f in clean_f] + [n["note"] for n in clean_o]:
        if re.search(r"\b(he|she|him|her|his|hers|himself|herself)\b", t, re.I):
            raise UnusableReview("gendered pronoun in the reply")
    return {"verdict": doc["verdict"], "rationale": rationale.strip(), "findings": clean_f, "option_notes": clean_o}


# queue ----------------------------------------------------------------------------------------------------------------------
def _failure_text(e: BaseException) -> str:
    if isinstance(e, UnusableReview):
        return FAILURE_TEXT["unusable"]
    if isinstance(e, (ai.AIOutputTruncated, ai.AINoRetry)):
        return FAILURE_TEXT["size_limit"]
    if isinstance(e, PacketIncomplete):
        return FAILURE_TEXT["packet"]
    if isinstance(e, ai.AIUnavailable):
        kind = ai.failure_kind(e)
        return FAILURE_TEXT[kind if kind in CONFIGURATION_KINDS else "provider"]
    return FAILURE_TEXT["unexpected"]


CONFIGURATION_KINDS = frozenset(("credentials", "setup"))   # explicit operator problems (ai.failure_kind): named, terminal, never a provider retry


def review_shortlist(pk: dict, meter: dict | None = None) -> dict:
    """One bounded model call (normal + at most one compact retry, ai._ask_bounded) and the validator. Raises ai exceptions /
    UnusableReview; returns the clean review."""
    text = ai._ask_bounded(lambda compact: build_prompt(pk, compact), OUTPUT_TOKENS, "homebot review", meter)
    try:
        return parse_review(text, pk)
    except UnusableReview as e:
        raise ai._terminal_if_compact(meter or {}, "homebot review", e)


GUARD_FAILURES = (db.RetentionLockTimeout, db.ConnectionLostMidOperation, db.DirtyConnectionAtGuardEntry)


def consumer_enabled(c) -> bool:
    """The worker consumes HomeBot reviews only when the HomeBot matching gate is ON (MMAI_HOMEBOT_MATCHING) and the table exists.
    The AI queue gate itself (MMAI_AI_QUEUE) decides whether the worker runs at all - OFF means no automatic provider call."""
    try:
        return bool(_hs().enabled()) and table_present(c)
    except Exception:  # noqa: BLE001
        return False


def _due_clause(now: str) -> tuple[str, tuple]:
    """Rows a consumer may claim now: pending and due (first attempt, or RETRY_AFTER_S after the last), or running under a claim
    older than CLAIM_TTL_S (the consumer that held it is gone)."""
    t = datetime.fromisoformat(now)
    cutoff = (t - timedelta(seconds=RETRY_AFTER_S)).isoformat()
    expired = (t - timedelta(seconds=CLAIM_TTL_S)).isoformat()
    return "((state=? AND (attempts=0 OR updated_at<=?)) OR (state=? AND claimed_at<=?))", (PENDING, cutoff, RUNNING, expired)


class ClaimLost(RuntimeError):
    """The owner fence matched nothing: another consumer took the row over. Raised INSIDE the guarded step so nothing of this
    write survives. In-process only."""


def _claim(c, r: dict, token: str, now: str) -> bool:
    """Compare-and-set claim inside the capture step: the row must still be claimable (same predicate as _due_clause, evaluated
    now, in this transaction). On PostgreSQL the UPDATE takes the row lock for the rest of the step; on SQLite the guard's
    BEGIN IMMEDIATE is the single writer. Exactly one consumer wins."""
    where, args = _due_clause(now)
    cur = c.execute(f"UPDATE {TABLE} SET state=?, owner=?, claimed_at=?, attempts=attempts+1, updated_at=? WHERE id=? AND {where}", (RUNNING, token, now, now, r["id"], *args))
    return cur.rowcount == 1


FENCE = "id=? AND owner=? AND state=?"


def _fenced(c, sql: str, params: tuple, rid: int, token: str) -> None:
    """A review-row UPDATE whose WHERE ends with the owner fence; a miss means the claim was lost (takeover after a stalled lease)
    and aborts the step - the result is dropped, never stored over another consumer's."""
    cur = c.execute(sql + " WHERE " + FENCE, (*params, rid, token, RUNNING))
    if cur.rowcount != 1:
        raise ClaimLost(str(rid))


class _Lease:
    """Owner-fenced lease renewal for the duration of the provider call (outside any guard). A daemon thread renews `claimed_at`
    every min(HEARTBEAT_SEC, CLAIM_TTL_S / 3) seconds with ONE fenced UPDATE (id, owner, state=running) inside db.bounded_write,
    the one helper shared with the H2 shortlist heartbeat: bounded connection-lock wait, refusal (no commit, no rollback) when
    another caller's write is pending, bounded database lock wait on both backends, commit of this statement only. A beat that
    cannot get in is skipped. If the fenced UPDATE matches nothing the claim is gone (takeover) and `lost` is set - the caller
    drops the result; the apply step's fence is still the binding check."""

    def __init__(self, c, rid: int, token: str):
        self.c, self.rid, self.token = c, rid, token
        self.lost = False
        self.skipped = 0
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def _interval(self) -> float:
        return max(0.01, min(float(HEARTBEAT_SEC), float(CLAIM_TTL_S) / 3.0))

    def renew(self) -> bool:
        """One beat through db.bounded_write (the single helper shared with the H2 shortlist heartbeat): bounded connection-lock
        wait, refusal without commit/rollback when another caller's write is pending, bounded database lock wait on both backends,
        commit of this statement only. True = the claim is (still) ours or the beat was skipped; False = the claim is gone."""
        try:
            with db.bounded_write(self.c, "homebot review lease", HEARTBEAT_DB_TIMEOUT_MS):
                cur = self.c.execute(f"UPDATE {TABLE} SET claimed_at=? WHERE " + FENCE, (now_iso(), self.rid, self.token, RUNNING))
                n = cur.rowcount
        except (db.DirtyConnectionAtGuardEntry, db.RetentionLockTimeout):
            self.skipped += 1                               # someone else's work or lock: never waited past the bound, nothing of theirs touched
            log.warning("homebot review %s: lease renewal skipped (connection busy)", self.rid)
            return True
        except Exception:  # noqa: BLE001  - connection lost etc.: the helper undid only our statement; category-only log
            self.skipped += 1
            log.warning("homebot review %s: lease renewal skipped (database busy)", self.rid)
            return True
        if n != 1:
            self.lost = True
            return False
        return True

    def _run(self) -> None:
        while not self._stop.wait(self._interval()):
            if not self.renew():
                return

    def __enter__(self):
        self._t = threading.Thread(target=self._run, name=f"homebot-review-lease-{self.rid}", daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=max(1.0, self._interval() + 2 * HEARTBEAT_DB_TIMEOUT_MS / 1000.0))
        return False


def due_count(c, now: Optional[str] = None) -> int:
    where, args = _due_clause(now or now_iso())
    return int(c.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE {where}", args).fetchone()[0])


def consume(c, max_batches: int = BATCHES_PER_WAKE, batch: int = BATCH) -> dict:
    """What the AI queue worker calls once per wake (ai_queue._iteration), after the MateBot work and independently of whether
    MateBot had any: bounded batches of due HomeBot reviews. Gate off / table absent -> nothing is read and no call is made."""
    out = {"skipped": None, "batches": 0, "reviewed": 0, "invalid": 0, "failed": 0, "stale": 0, "calls": 0}
    if not consumer_enabled(c):
        out["skipped"] = "homebot_gate_off"
        return out
    for _ in range(max(1, int(max_batches))):
        if due_count(c) == 0:
            break
        res = process_pending(c, limit=batch)
        out["batches"] += 1
        for k in ("reviewed", "invalid", "failed", "stale", "calls"):
            out[k] += res[k]
        if res.get("guard_failures"):
            break   # the database is busy / the connection was lost: leave the rest for the next wake
    return out


def process_pending(c, limit: int = 20, attempts: int = MAX_ATTEMPTS, now: Optional[str] = None) -> dict:
    """Run the claimable reviews (pending and due, or running under an expired claim). Per row, three parts:
    capture - one guarded step (db.retention_guard): CAS claim (state -> running, owner token, lease start; exactly one consumer
    wins), freshness + version check in the same transaction (a stale row is released as STALE without a call), packet assembly;
    the model call - OUTSIDE any guard (never a provider call under the DB lock);
    apply - one guarded step: the first statement is the owner fence (a lost claim drops the result), then freshness + version
    are re-evaluated in the SAME transaction as the publication write (a basis that moved while the model was writing makes the
    result STALE - it is never stored), then the review or a closed failure sentence is written, fenced. A guard failure leaves
    the row as it is (pending, or running until its lease expires); logs carry a category only."""
    out = {"reviewed": 0, "invalid": 0, "failed": 0, "stale": 0, "calls": 0, "guard_failures": 0, "not_claimed": 0, "lost_claim": 0}
    when0 = now or now_iso()
    where, args = _due_clause(when0)
    cands = rows(c.execute(f"SELECT id, attempts FROM {TABLE} WHERE {where} ORDER BY id LIMIT ?", (*args, int(limit))))
    for cand in cands:
        token = secrets.token_hex(8)
        when = now or now_iso()
        # capture ------------------------------------------------------------------------------------------------------
        try:
            with db.retention_guard(c, label="homebot review capture"):
                if not _claim(c, cand, token, when):
                    out["not_claimed"] += 1          # another consumer holds it (or it was finished meanwhile)
                    pk = None
                else:
                    r = row(c.execute(f"SELECT * FROM {TABLE} WHERE id=?", (cand["id"],)))   # attempts already counts this claim
                    sl = _shortlist(c, r["shortlist_id"])
                    if int(r["attempts"]) > attempts:
                        # taken over more often than the bounded attempts allow (consumers kept stalling on it): stop, staff decide
                        _set(c, r["id"], FAILED, error=FAILURE_TEXT["unexpected"], when=when, token=token); out["failed"] += 1
                        pk = None
                    elif sl is None:
                        c.execute(f"DELETE FROM {TABLE} WHERE " + FENCE, (r["id"], token, RUNNING))
                        pk = None
                    elif not _enquiry_open(c, sl["enquiry_id"]):
                        _set(c, r["id"], STALE, error=FAILURE_TEXT["closed"], when=when, token=token); out["stale"] += 1
                        pk = None
                    elif freshness(c, sl) is not None or shortlist_version(sl) != r["version"]:
                        _set(c, r["id"], STALE, error=FAILURE_TEXT["stale"], when=when, token=token); out["stale"] += 1
                        pk = None
                    else:
                        try:
                            pk = packet(c, sl)
                        except PacketIncomplete:
                            _set(c, r["id"], FAILED, error=FAILURE_TEXT["packet"], when=when, token=token); out["failed"] += 1
                            pk = None
        except GUARD_FAILURES:
            log.warning("homebot review %s: capture not completed (database busy or connection lost); not claimed", cand["id"])
            out["guard_failures"] += 1
            continue
        if pk is None:
            continue
        # the model call: outside any guard ----------------------------------------------------------------------------
        assert not db.guarded(c), "homebot review: provider call must not run inside a retention guard"
        meter = {"asks": 0}
        review, failure, terminal = None, None, False
        lease = _Lease(c, r["id"], token)
        try:
            with lease:   # lease renewed while the call runs; the call itself is outside every lock
                review = review_shortlist(pk, meter)
        except UnusableReview as e:
            log.warning("homebot review %s: unusable reply", r["id"])   # category only
            failure, terminal = e, (int(r["attempts"]) >= attempts or isinstance(e, ai.AINoRetry))
        except ai.AIUnavailable as e:
            kind = "size_limit" if isinstance(e, ai.AINoRetry) else ai.failure_kind(e)
            log.warning("homebot review %s: AI step failed: %s", r["id"], kind)
            # credentials / SDK problems are the operator's to fix: named in the row, terminal now, never an automatic provider retry
            failure, terminal = e, (isinstance(e, ai.AINoRetry) or kind in CONFIGURATION_KINDS or int(r["attempts"]) >= attempts)
        except Exception as e:  # noqa: BLE001  - nothing of the exception reaches a row or the log
            log.warning("homebot review %s: AI step failed: unexpected", r["id"])
            failure, terminal = e, True
        out["calls"] += meter["asks"]
        when = now or now_iso()   # COMPLETION time: the retry backoff, updated_at and reviewed_at all count from when the call ended
        if lease.lost:
            log.warning("homebot review %s: claim lost during the provider call; result dropped", r["id"])
            out["lost_claim"] += 1
            continue
        # apply: fence first, then freshness and publication in ONE transaction --------------------------------------------
        try:
            with db.retention_guard(c, label="homebot review apply"):
                _fenced(c, f"UPDATE {TABLE} SET updated_at=?", (when,), r["id"], token)   # owner fence bound to this transaction
                if failure is not None:
                    if terminal:
                        state = INVALID if isinstance(failure, UnusableReview) else FAILED
                        _set(c, r["id"], state, error=_failure_text(failure), when=when, token=token)
                        out["invalid" if state == INVALID else "failed"] += 1
                    else:
                        _set(c, r["id"], PENDING, when=when, token=token)   # released; retried after RETRY_AFTER_S
                    continue
                sl2 = _shortlist(c, r["shortlist_id"])
                if sl2 is None:
                    c.execute(f"DELETE FROM {TABLE} WHERE " + FENCE, (r["id"], token, RUNNING))
                    continue
                if not _enquiry_open(c, sl2["enquiry_id"]):
                    _set(c, r["id"], STALE, error=FAILURE_TEXT["closed"], when=when, token=token); out["stale"] += 1
                    continue
                if shortlist_version(sl2) != r["version"] or freshness(c, sl2) is not None:
                    _set(c, r["id"], STALE, error=FAILURE_TEXT["stale"], when=when, token=token); out["stale"] += 1
                    continue
                _fenced(c, f"UPDATE {TABLE} SET state=?, verdict=?, review_json=?, error=NULL, model=?, rule_version=?, contract=?, owner=NULL, claimed_at=NULL, updated_at=?, reviewed_at=?",
                        (REVIEWED, review["verdict"], j(review), ai.MODEL, rules.RULE_VERSION, REVIEW_CONTRACT, when, when), r["id"], token)
                out["reviewed"] += 1
        except ClaimLost:
            log.warning("homebot review %s: claim lost before the result was recorded; result dropped", r["id"])
            out["lost_claim"] += 1
            continue
        except GUARD_FAILURES:
            log.warning("homebot review %s: result not recorded (database busy or connection lost); lease left to expire", r["id"])
            out["guard_failures"] += 1
            continue
    return out


def _set(c, rid: int, state: str, *, when: str, token: str, error: Optional[str] = None) -> None:
    """Inside the caller's guarded step (the step commits once at exit); fenced on the claim token; the claim is released
    (attempts were counted at claim time)."""
    assert state in STATES and state != RUNNING and (error is None or error in CLOSED_FAILURE_SENTENCES)
    _fenced(c, f"UPDATE {TABLE} SET state=?, error=?, owner=NULL, claimed_at=NULL, updated_at=?", (state, error, when), rid, token)


def forget(c, shortlist_ids: list[int]) -> int:
    """Retention / test-data wipe / staff delete: remove every review row of these shortlists (cascades cover the enquiry path)."""
    n = 0
    for sid in shortlist_ids:
        n += c.execute(f"DELETE FROM {TABLE} WHERE shortlist_id=?", (sid,)).rowcount or 0
    return n


def table_present(c) -> bool:
    try:
        c.execute(f"SELECT 1 FROM {TABLE} LIMIT 1").fetchall()
        return True
    except Exception:  # noqa: BLE001
        return False
