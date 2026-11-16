"""MateBot group travel: the arithmetic and the three-apartment shortlist roles (GP6(d), H28, M4, 16 November 2026).

Pure: no database, no provider, no model, no clock. It is NOT connected to a journey provider or to the AI assessment yet;
nothing here measures a journey. It exists so the rules that are settled can be tested exactly, and so the part that is not
settled cannot be decided by accident.

What is settled, and applied here:
  * Travel is checked last, on up to three apartments chosen without travel; an apartment that fails is replaced by the next
    otherwise-eligible candidate, which is checked in its turn (GP6(d)).
  * For each person the fastest journey by a choice THEY accepted is used and named; a choice they did not accept is never
    used, and someone who did not answer the travel choices question has no journey selected at all (H28(c)).
  * When every member states the same maximum travel time, H28 applies as written: the closest strict majority of the people
    with known destinations, their average against twice the stated time (H28(a), (b), (e)).
  * An unknown or incomplete journey is neither a pass nor a failure (H28(d)).

What is NOT settled, and therefore never decided here (register, "MateBot group travel threshold", not yet ruled):
  * MateBot's maximum travel time (4.7) is optional and is each member's own. When the stated times differ, or any is missing,
    there is no approved way to combine them. No default, average, minimum or maximum is used: every member's own measured
    journey is reported against their own stated time, and the group result is NEEDS_CONFIRMATION.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import localcontext
from typing import Callable, Optional

from . import homebot_travel as ht

MAX_SHORTLIST = 3                                            # GP6(d): no fourth apartment
MODES = ht.TRANSPORT[:3]                                     # Walk, Bicycle, Public transport (H28(c), H31)
TRAVEL_TIMES = ("5 minutes", "15 minutes", "30 minutes", "45 minutes", "60+ minutes")   # the MateBot form's 4.7 answers
STATED_LIMIT = dict(zip(TRAVEL_TIMES, (5, 15, 30, 45, 60)))

PASS, EXCEEDS_CEILING, NEEDS_CONFIRMATION, NO_DESTINATIONS = ht.PASS, ht.EXCEEDS_CEILING, ht.NEEDS_CONFIRMATION, ht.NO_DESTINATIONS
MEASURED, PARTIAL, UNMEASURED, NO_DESTINATION = ht.MEASURED, ht.PARTIAL, ht.UNMEASURED_PERSON, ht.NO_DEST_PERSON
NO_MODE_ANSWER = "NO_MODE_ANSWER"                            # destination known, travel choices never answered: nothing is selected

# each member against their OWN stated time (a fact for staff; it is not the group result)
WITHIN_OWN_LIMIT, OVER_OWN_LIMIT, NO_STATED_LIMIT, NOT_MEASURED = "WITHIN_OWN_LIMIT", "OVER_OWN_LIMIT", "NO_STATED_LIMIT", "NOT_MEASURED"

TRAVEL_LIMIT_MISSING = "TRAVEL_LIMIT_MISSING"
TRAVEL_LIMITS_DIFFER = "TRAVEL_LIMITS_DIFFER"
TRAVEL_MODE_NOT_ANSWERED = "TRAVEL_MODE_NOT_ANSWERED"
UNRULED_NOTE = ("The members' maximum travel times differ or are missing. How to combine them has not been ruled, so no group "
                "result is calculated: each member's own journey is shown against their own stated time and the group needs "
                "confirmation.")

ROLE_SHORTLIST, ROLE_RESERVE, ROLE_UNAVAILABLE = "shortlist", "reserve", "unavailable"


@dataclass(frozen=True)
class Member:
    """One person. `stated_time` is their 4.7 answer or None; `accepted_modes` their travel choices, () when never answered;
    `journeys` maps each accepted choice to minutes or ht.UNMEASURED for their primary destination, None when no destination
    is known. A journey for a choice the person did not accept is refused, not ignored."""
    index: int
    stated_time: Optional[str]
    accepted_modes: tuple
    journeys: Optional[dict]

    def __post_init__(self):
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise ht.InvalidTravelInput("index must be a non-negative integer position")
        if self.stated_time is not None and self.stated_time not in STATED_LIMIT:
            raise ht.InvalidTravelInput(f"stated_time must be one of {TRAVEL_TIMES} or None")
        modes = self.accepted_modes
        if isinstance(modes, (str, bytes)) or not isinstance(modes, (list, tuple)):
            raise ht.InvalidTravelInput("accepted_modes must be a list of travel choices")
        if any(not isinstance(m, str) or m not in MODES for m in modes) or len(set(modes)) != len(modes):
            raise ht.InvalidTravelInput(f"accepted_modes must be distinct choices from {MODES}")
        object.__setattr__(self, "accepted_modes", tuple(sorted(modes, key=MODES.index)))
        if self.journeys is None:
            return
        if not isinstance(self.journeys, dict):
            raise ht.InvalidTravelInput("journeys must be a mapping choice -> minutes | UNMEASURED, or None")
        if set(self.journeys) != set(self.accepted_modes):
            raise ht.InvalidTravelInput(f"journeys must cover exactly the accepted choices {self.accepted_modes}, got {tuple(self.journeys)}")
        clean = {m: (None if (isinstance(v, str) and v == ht.UNMEASURED) else ht._minutes(v))
                 for m, v in ((m, self.journeys[m]) for m in self.accepted_modes)}
        object.__setattr__(self, "journeys", clean)

    @property
    def limit(self) -> Optional[int]:
        return STATED_LIMIT.get(self.stated_time)

    @property
    def status(self) -> str:
        if self.journeys is None:
            return NO_DESTINATION
        if not self.accepted_modes:
            return NO_MODE_ANSWER
        known = [v for v in self.journeys.values() if v is not None]
        return UNMEASURED if not known else MEASURED if len(known) == len(self.journeys) else PARTIAL

    @property
    def best(self):
        """Fastest measured accepted journey: the fastest when MEASURED, only an upper bound when PARTIAL."""
        return min((v for v in (self.journeys or {}).values() if v is not None), default=None)

    @property
    def best_modes(self) -> tuple:
        b = self.best
        return tuple(m for m, v in (self.journeys or {}).items() if v is not None and v == b) if b is not None else ()


def _own_limit(m: Member) -> str:
    if m.limit is None:
        return NO_STATED_LIMIT
    if m.status == MEASURED:
        return WITHIN_OWN_LIMIT if m.best <= m.limit else OVER_OWN_LIMIT
    if m.status == PARTIAL and m.best <= m.limit:
        return WITHIN_OWN_LIMIT                              # an unmeasured accepted choice could only be faster still
    return NOT_MEASURED


def _fact(m: Member) -> dict:
    f = {"index": m.index, "status": m.status, "stated_limit_minutes": m.limit, "accepted_modes": list(m.accepted_modes),
         "fastest_minutes": None, "fastest_modes": [], "bound_minutes": None, "best_known_modes": [],
         "unknown_modes": [k for k, v in (m.journeys or {}).items() if v is None],
         "own_limit": _own_limit(m), "warning": None, "in_majority": False}
    if m.status == MEASURED:
        f["fastest_minutes"], f["fastest_modes"] = ht._fmt(m.best), list(m.best_modes)
    elif m.status == PARTIAL:
        f["bound_minutes"], f["best_known_modes"] = ht._fmt(m.best), list(m.best_modes)
    return f


def assess_group(members) -> ht.TravelAssessment:
    """Group result for one apartment. Exact arithmetic, independent of the caller's Decimal context."""
    with localcontext(ht.EXACT_CTX):
        return _assess(members)


def _assess(members) -> ht.TravelAssessment:
    if isinstance(members, (str, bytes, dict)) or not isinstance(members, (list, tuple)):
        raise ht.InvalidTravelInput("members must be a list of Member")
    members = tuple(members)
    if not (ht.MIN_PEOPLE <= len(members) <= ht.MAX_PEOPLE):
        raise ht.InvalidTravelInput(f"group size must be {ht.MIN_PEOPLE}–{ht.MAX_PEOPLE}")
    if not all(isinstance(m, Member) for m in members):
        raise ht.InvalidTravelInput("every member must be a Member")
    if len({m.index for m in members}) != len(members):
        raise ht.InvalidTravelInput("member positions must be unique")
    members = tuple(sorted(members, key=lambda m: m.index))   # order invariance

    known = [m for m in members if m.journeys is not None]
    n, k = len(known), ht.majority_size(len(known))
    pf = {m.index: _fact(m) for m in members}
    limits = sorted({m.limit for m in members if m.limit is not None})
    missing = [m.index for m in members if m.limit is None]
    uniform = not missing and len(limits) == 1
    facts = {
        "stated_limits_minutes": limits, "limit_missing": missing, "same_stated_limit": uniform,
        "stated_limit_minutes": limits[0] if uniform else None, "ceiling_minutes": 2 * limits[0] if uniform else None,
        "known_destinations": n, "majority_size": k if n else 0,
        "no_destination": [m.index for m in members if m.journeys is None],
        "no_mode_answer": [m.index for m in known if m.status == NO_MODE_ANSWER],
        "measured": [m.index for m in known if m.status == MEASURED],
        "partial": [m.index for m in known if m.status == PARTIAL],
        "unmeasured": [m.index for m in known if m.status == UNMEASURED],
        "within_own_limit": [m.index for m in members if pf[m.index]["own_limit"] == WITHIN_OWN_LIMIT],
        "over_own_limit": [m.index for m in members if pf[m.index]["own_limit"] == OVER_OWN_LIMIT],
        "people": [pf[m.index] for m in members],
        "majority": None, "within_ceiling": None, "meets_stated_limit": None,
        "routing_basis": ht.ROUTING_BASIS, "pass_meaning": ht.PASS_MEANING,
    }
    if n == 0:
        return ht.TravelAssessment(NO_DESTINATIONS, (ht.TRAVEL_NO_DESTINATIONS,), facts)

    reasons: list[str] = []
    if missing:
        reasons.append(TRAVEL_LIMIT_MISSING)
    if len(limits) > 1:
        reasons.append(TRAVEL_LIMITS_DIFFER)
    if not uniform:
        facts["unruled"] = UNRULED_NOTE
    if facts["no_mode_answer"]:
        reasons.append(TRAVEL_MODE_NOT_ANSWERED)
    if facts["partial"] or facts["unmeasured"]:
        reasons.append(ht.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES)
    if facts["partial"]:
        reasons.append(ht.FASTEST_MODE_UNCONFIRMED)
    if reasons:
        return ht.TravelAssessment(NEEDS_CONFIRMATION, tuple(reasons), facts)   # never a pass, never an automatic failure

    # every member states the same time and every known destination is fully measured: H28 as written
    limit, ceiling = limits[0], 2 * limits[0]
    chosen = sorted(known, key=lambda m: (m.best, m.index))[:k]
    total = ht._sum_minutes(m.best for m in chosen)
    for m in chosen:
        pf[m.index]["in_majority"] = True
    for m in known:
        if m.best > ceiling:
            pf[m.index]["warning"] = ht.LONG_JOURNEY_WARNING
    facts["majority"] = {"indices": [m.index for m in chosen], "size": k, "total_minutes": ht._fmt(total),
                         "average_display": ht._avg_display(total, k),
                         "comparison": f"exact total {ht._fmt(total)} vs {k} × ceiling {ceiling} = {k * ceiling} and {k} × limit {limit} = {k * limit}"}
    facts["within_ceiling"] = total <= k * ceiling
    facts["meets_stated_limit"] = total <= k * limit
    warn = [ht.LONG_JOURNEY_WARNING] if any(pf[m.index]["warning"] for m in known) else []
    if facts["within_ceiling"]:
        return ht.TravelAssessment(PASS, tuple([ht.TRAVEL_MAJORITY_WITHIN_CEILING] + warn), facts)
    return ht.TravelAssessment(EXCEEDS_CEILING, tuple([ht.TRAVEL_MAJORITY_EXCEEDS_CEILING] + warn), facts)


# ---------------------------------------------------------------------------------------------- travel last (GP6(d))
def travel_last(ordered_ids, assess: Callable, max_shortlist: int = MAX_SHORTLIST) -> dict:
    """Walk the apartments in their non-travel order. Each is assessed at most once, and only while a place is open: an
    apartment that EXCEEDS_CEILING is replaced by the next candidate; one that needs confirmation keeps its place (it is not
    a pass and not a failure). The apartments that qualified earlier are never dropped or reordered. Bounded by the list
    given: nothing is searched for beyond it."""
    ids = list(ordered_ids)
    if len(set(ids)) != len(ids):
        raise ht.InvalidTravelInput("an apartment may appear only once")
    if isinstance(max_shortlist, bool) or not isinstance(max_shortlist, int) or not (1 <= max_shortlist <= MAX_SHORTLIST):
        raise ht.InvalidTravelInput(f"the shortlist is at most {MAX_SHORTLIST}")
    out = {"shortlist": [], "failed": [], "not_checked": []}
    for i, aid in enumerate(ids):
        if len(out["shortlist"]) == max_shortlist:
            out["not_checked"] = ids[i:]
            break
        r = assess(aid)
        if not isinstance(r, ht.TravelAssessment):
            raise ht.InvalidTravelInput("assess must return a TravelAssessment")
        (out["failed"] if r.status == EXCEEDS_CEILING else out["shortlist"]).append((aid, r))
    return out


# -------------------------------------------------------------------------------- three at most, for staff (GP6(d))
def shortlist_roles(apartments, max_shortlist: int = MAX_SHORTLIST) -> list:
    """Role of each of a group's stored apartments, in the stored order, WITHOUT travel (which is not connected for MateBot).
    Each item: {"id", "current": bool, "unavailable": bool}. An apartment leased to another group or declined by a current
    member is history and takes no place, including the one currently offered (staff still see it first, with its warning, and
    its place goes to the next candidate). The apartment currently offered otherwise always holds a place. The rest fill the
    places in order; what is left over is a reserve, which moves up only when a place opens."""
    items = list(apartments)
    roles = [ROLE_UNAVAILABLE if a.get("unavailable") else None for a in items]
    places = max_shortlist
    for i, a in enumerate(items):
        if a.get("current") and roles[i] is None:
            roles[i] = ROLE_SHORTLIST
            places -= 1
    for i in range(len(items)):
        if roles[i] is None:
            roles[i] = ROLE_SHORTLIST if places > 0 else ROLE_RESERVE
            places -= 1
    return roles
