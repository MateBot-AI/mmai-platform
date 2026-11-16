"""MateBot group travel: the arithmetic and the three-apartment shortlist roles (GP6(d), H28, M4, 16 November 2026).

Pure: no database, no provider, no model, no clock. Nothing here measures a journey or asks the AI anything: matebot_live
supplies each person's journeys from the journey provider for one live check and matebot_travel_reply obtains the AI's
assessment of the result. It is kept separate so the rules that are settled can be tested exactly, and so the part that is
not settled cannot be decided by accident.

What is settled, and applied here:
  * Travel is checked last, on up to three apartments chosen without travel; an apartment that fails is replaced by the next
    otherwise-eligible candidate, which is checked in its turn (GP6(d)).
  * For each person the fastest journey by a choice THEY accepted is used and named; a choice they did not accept is never
    used, and someone who did not answer the travel choices question has no journey selected at all (H28(c)).
  * The closest strict majority of the people with known destinations is chosen by journey time; its average journey is
    compared with twice the average of those SAME members' stated maximum travel times (H28(a), (b), (e), (j)). When every
    member states the same time this is H28 as first written: the average against twice that time.
  * If any member's maximum travel time is missing the group result is NEEDS_CONFIRMATION (H28(j)). No default, minimum,
    maximum or other member's time is used in its place.
  * An unknown or incomplete journey is neither a pass nor a failure (H28(d)).
  * A long individual journey is flagged and never excludes the apartment by itself (H28(b), (e)).

What is NOT settled, and therefore never decided here:
  * Two or more members with exactly the same journey time at the edge of the closest majority, and different stated
    times. The majority is chosen by journey time and never by the size of the members' limits (H28(j)), and no tie-break
    is ruled. When every way of filling the tied places gives the same result, that result stands. When the ways disagree
    the group result is NEEDS_CONFIRMATION, with that reason: no order of the members decides a pass or a failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
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
TRAVEL_MODE_NOT_ANSWERED = "TRAVEL_MODE_NOT_ANSWERED"
TRAVEL_MAJORITY_TIED = "TRAVEL_MAJORITY_TIED"
LIMIT_MISSING_NOTE = ("At least one member has not stated a maximum travel time, so no group result is calculated: each member's "
                      "own journey is shown against their own stated time and the group needs confirmation. No time is assumed "
                      "for them.")
MAJORITY_TIED_NOTE = ("Members with the same journey time sit at the edge of the closest majority and have different maximum "
                      "travel times, and the result depends on which of them is counted. No group result is calculated: the "
                      "group needs confirmation.")

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
        if self.stated_time is not None and (not isinstance(self.stated_time, str) or self.stated_time not in STATED_LIMIT):
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
         "own_limit": _own_limit(m), "warning": None, "in_majority": False, "tied_for_majority": False}
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
        "majority": None, "majority_tie": None, "within_ceiling": None, "meets_stated_limit": None,
        "routing_basis": ht.ROUTING_BASIS, "pass_meaning": ht.PASS_MEANING,
    }
    if n == 0:
        return ht.TravelAssessment(NO_DESTINATIONS, (ht.TRAVEL_NO_DESTINATIONS,), facts)

    reasons: list[str] = []
    if missing:
        reasons.append(TRAVEL_LIMIT_MISSING)
        facts["limit_note"] = LIMIT_MISSING_NOTE
    if facts["no_mode_answer"]:
        reasons.append(TRAVEL_MODE_NOT_ANSWERED)
    if facts["partial"] or facts["unmeasured"]:
        reasons.append(ht.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES)
    if facts["partial"]:
        reasons.append(ht.FASTEST_MODE_UNCONFIRMED)
    if reasons:
        return ht.TravelAssessment(NEEDS_CONFIRMATION, tuple(reasons), facts)   # never a pass, never an automatic failure

    # every member states a time and every known destination is fully measured. H28(b): the closest strict majority BY JOURNEY
    # TIME. H28(j): its average journey against twice the average of those same members' stated times; with one shared stated
    # time that is H28(a) exactly. Compared as exact totals (k × average on both sides), never as rounded averages.
    ordered = sorted(known, key=lambda m: (m.best, m.index))
    chosen = ordered[:k]
    total = ht._sum_minutes(m.best for m in chosen)
    edge = chosen[-1].best
    sure = [m for m in ordered if m.best < edge]
    tied = [m for m in ordered if m.best == edge]           # every one of these has an equal claim to the last place(s)
    places = k - len(sure)
    tied_limits = sorted(m.limit for m in tied)
    least = sum(m.limit for m in sure) + sum(tied_limits[:places])
    most = sum(m.limit for m in sure) + sum(tied_limits[len(tied_limits) - places:])
    facts["majority_tie"] = ({"journey_minutes": ht._fmt(edge), "indices": [m.index for m in tied], "places": places}
                             if len(tied) > places else None)
    if (total <= 2 * least) != (total <= 2 * most):
        # the journey total is the same whichever tied members are counted; only their stated times differ, and they decide
        facts["tie_note"] = MAJORITY_TIED_NOTE
        return ht.TravelAssessment(NEEDS_CONFIRMATION, (TRAVEL_MAJORITY_TIED,), facts)

    # What is reported is only what holds for EVERY way of filling the tied places. The members strictly inside the edge are
    # in the majority; the tied ones are named as tied, never as chosen. The stated-time figure is given as one number only
    # when it is the same for every way; otherwise as its range.
    one_set, one_figure = len(tied) == places, least == most
    counted = chosen if one_set else sure
    for m in counted:
        pf[m.index]["in_majority"] = True
    if not one_set:
        for m in tied:
            pf[m.index]["tied_for_majority"] = True
    for m in known:
        # long for this person: over double their own stated time, or over the group's doubled figure (the same thing when
        # every member states the same time; the lowest such figure when tied members could change it). A warning only; it
        # never excludes the apartment (H28(b), (e)).
        if m.best > 2 * m.limit or k * m.best > 2 * least:
            pf[m.index]["warning"] = ht.LONG_JOURNEY_WARNING
    if uniform:
        limit, ceiling = limits[0], 2 * limits[0]
        comparison = f"exact total {ht._fmt(total)} vs {k} × ceiling {ceiling} = {k * ceiling} and {k} × limit {limit} = {k * limit}"
    elif one_figure:
        comparison = (f"exact total {ht._fmt(total)} vs that member's stated time {least}, doubled {2 * least}" if k == 1 else
                      f"exact total {ht._fmt(total)} vs the same {k} members' stated times totalling {least}, doubled {2 * least}")
    else:
        comparison = (f"exact total {ht._fmt(total)} vs the counted members' stated times totalling between {least} and {most}, "
                      f"doubled {2 * least} to {2 * most}, depending on which tied members are counted; the result is the same for all")
    facts["majority"] = {"indices": [m.index for m in counted], "tied_indices": [] if one_set else [m.index for m in tied],
                         "size": k, "membership_determined": one_set, "figure_determined": one_figure,
                         "total_minutes": ht._fmt(total), "average_display": ht._avg_display(total, k),
                         "stated_limits_minutes": [m.limit for m in sorted(chosen, key=lambda m: m.index)] if one_set else None,
                         "limit_total_minutes": least if one_figure else None,
                         "limit_average_display": ht._avg_display(Decimal(least), k) if one_figure else None,
                         "ceiling_display": ht._avg_display(Decimal(2 * least), k) if one_figure else None,
                         "ceiling_range_display": None if one_figure else [ht._avg_display(Decimal(2 * least), k), ht._avg_display(Decimal(2 * most), k)],
                         "comparison": comparison}
    facts["within_ceiling"] = total <= 2 * least                      # equal to `total <= 2 * most`: checked above
    # against the stated times themselves (not doubled): not known when the tied members' times would change the answer
    facts["meets_stated_limit"] = (total <= least) if (total <= least) == (total <= most) else None
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
    """Role of each of a group's stored apartments, in the stored order, WITHOUT travel (the stored list never holds a travel result; travel is checked live, last, by matebot_live).
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
