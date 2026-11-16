"""HomeBot candidate evaluation (H1 of the T3(a) completion brief, 9 November 2026). PURE: no database, clock, network,
AI or provider. The caller supplies the saved enquiry, its people, the apartment rows, the business date and (optionally)
journey estimates; this module returns, for every apartment, what it is for this party and why, and assembles the internal
staff shortlist structure (H25/H27/GP6(d)). The persisted workflow (runs, history, triggers) lives in homebot_shortlist.py.

What is decided here, and by which register rule
  * availability (A2(e)), selected bedroom counts (H26), area (H27), "Must have 2 bathrooms" (H10/H27)
  * party size and bedding feasibility via homebot_bedding (A4, H21: own room no exception, one bunk unit)
  * the COMPLETE search of concrete move-in / move-out offers inside the accepted windows (H23): every accepted start day
    × every accepted exit day is checked for A9 (calendar-month minimum), A10 (exit windows, blackout), A17 (vacancy from the
    business date), A6 (tariff of that exact pair) and A16 (actual total rent incl. extra-bed charge against the whole-apartment
    maximum and its allowance). Nothing is narrowed: a later start that moves the lease out of the Jan/Feb→July tariff, or an
    earlier exit, is found whenever it exists (root's 2027-02-25 vs 2027-03-01 case).
  * "bunks only if cheaper" (H21): a bunk-using arrangement is accepted only when its total is LOWER than the cheapest
    non-bunk option meeting the same firm requirements; a measured not-cheaper bunk option is excluded (it is not something staff
    can confirm away); with no valid comparison it needs confirmation and no saving is claimed.
  * travel via homebot_travel (H28): no provider is wired, so journeys are UNMEASURED unless the caller injects estimates;
    a known destination without a measured journey is NEVER a pass — the candidate needs confirmation (H20/H28(d)).
  * features (H11/H20) are preference facts only (apartment_features.compare); nothing unknown is assumed present.
  * an essential-type practical need written in the notes (H20) flags every candidate for staff review before anything is
    treated as suitable; nothing is silently downgraded.

What is NOT decided here
  * No numeric business weights exist (H20). Ranking is Pareto screening (an option beaten on every measured dimension by
    another ranks below it) followed by a PROVISIONAL order in Bill's stated priority (date fit, layout, price, travel,
    amenities). Non-dominated options that differ materially are flagged as trade-offs for staff / the grounded AI review;
    the provisional order is not a verdict that one outranks the other.
  * Staff never see these reason codes: homebot_shortlist / the staff client translate them (PLAIN below).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Callable, Optional

from . import apartment_features as feat
from . import budget, homebot_bedding as bedding, homebot_travel as travel, lease_dates, rules

# ------------------------------------------------------------------------------------------------------------ vocabulary
SUITABLE = "suitable"                        # every firm requirement met and verified; nothing left to confirm
NEEDS_CONFIRMATION = "needs_confirmation"    # firm requirements met as far as known; listed points must be confirmed first
ALTERNATIVE_AREA = "alternative_area"        # H27: misses the selected area only; otherwise qualifies
ALTERNATIVE_BATHROOM = "alternative_bathroom"  # H27: misses "Must have 2 bathrooms" only; otherwise qualifies
EXCLUDED = "excluded"
CLASSES = (SUITABLE, NEEDS_CONFIRMATION, ALTERNATIVE_AREA, ALTERNATIVE_BATHROOM, EXCLUDED)

SCENARIO_CURRENT = "current"       # the confirmed members now (complete party, or an incomplete party's current members, H24)
SCENARIO_FINAL_SIZE = "final_size"  # provisional: intended final size with the staff-confirmed planning budget (H24)

# exclusion reasons
NOT_AVAILABLE = "NOT_AVAILABLE"                       # no availability date (A2(e))
BEDROOM_COUNT_NOT_SELECTED = "BEDROOM_COUNT_NOT_SELECTED"  # H26
AREA_NOT_SELECTED = "AREA_NOT_SELECTED"              # H27 (alternative when it is the only miss)
BATHROOMS_BELOW_REQUIRED = "BATHROOMS_BELOW_REQUIRED"  # H10/H27 (alternative when it is the only miss)
PARTY_TOO_LARGE = "PARTY_TOO_LARGE"                   # A4(b)
BEDDING_NOT_FEASIBLE = "BEDDING_NOT_FEASIBLE"         # H21 / A4
NO_FEASIBLE_DATES = "NO_FEASIBLE_DATES"               # no (start, exit) pair inside the accepted windows passes A9/A10/A17
OVER_BUDGET = "OVER_BUDGET"                           # every date-feasible offer is above the A16 limit
BUNK_NOT_CHEAPER = "BUNK_NOT_CHEAPER"                 # H21: measured comparison; the bunk option is not cheaper
TRAVEL_EXCEEDS_CEILING = "TRAVEL_EXCEEDS_CEILING"     # H28(a)
REJECTED_BY_CUSTOMER = "REJECTED_BY_CUSTOMER"         # H25/GP14: apartment or building declined for this enquiry
INVENTORY_UNREADABLE = "INVENTORY_UNREADABLE"         # bedding columns not parseable: a data problem, held for staff
LEASED = "LEASED"                                     # GP8: the apartment is recorded as leased
# confirmation reasons
TRAVEL_NEEDS_CONFIRMATION = "TRAVEL_NEEDS_CONFIRMATION"     # H28(d): known destination, journey not measured
BEDDING_NEEDS_CONFIRMATION = "BEDDING_NEEDS_CONFIRMATION"   # homebot_bedding POSSIBLE_NEEDS_STAFF_CONFIRMATION
BUNK_COMPARISON_UNAVAILABLE = "BUNK_COMPARISON_UNAVAILABLE"  # H21: no valid non-bunk comparison exists
ESSENTIAL_NOTE_REVIEW = "ESSENTIAL_NOTE_REVIEW"             # H20: the notes state an essential need
DATE_SEARCH_UNRESOLVED = "DATE_SEARCH_UNRESOLVED"           # accepted ranges too wide to enumerate completely: explicit unknown
PROVISIONAL_FINAL_SIZE = "PROVISIONAL_FINAL_SIZE"           # H24: future members' choices unknown; capacity only
# informational flags
NO_DESTINATIONS = "NO_DESTINATIONS"                 # nobody has a destination to travel to: nothing to check
SPARE_BEDROOM = "SPARE_BEDROOM"                     # H21: the party would leave a bedroom unused (permitted for HomeBot)
WITHIN_ALLOWANCE = "WITHIN_ALLOWANCE"               # A16: above the stated maximum but inside the allowance
LONG_JOURNEY = "LONG_JOURNEY"                       # H28(e) warning carried from the travel helper
CHEAPER_OFFER_AVAILABLE = "CHEAPER_OFFER_AVAILABLE"  # a different accepted date pair is cheaper than the closest-dates offer
TRADE_OFF = "TRADE_OFF"                             # non-dominated against another listed option; staff/AI balance it
FOURTH_OPTION_PROVISIONAL = "FOURTH_OPTION_PROVISIONAL"  # RETIRED 16 Nov 2026 (GP6(d): the maximum is three). Kept only so options
                                                         # prepared before then still read in plain words; never set now.
TRAVEL_NOT_CHECKED = "TRAVEL_NOT_CHECKED"           # GP6(d): travel is checked last, on the shortlisted apartments only
REPLACED_AFTER_TRAVEL = "REPLACED_AFTER_TRAVEL"     # GP6(d): shortlisted on the other points, failed the travel check, replaced
REPLACEMENT_AFTER_TRAVEL = "REPLACEMENT_AFTER_TRAVEL"   # GP6(d): took the place of an apartment that failed the travel check
MAX_SHORTLIST = 3                                   # GP6(d)/H25/O4(d): never more, and never padded to reach it
BUNK_CHEAPER_OTHER_DATES = "BUNK_CHEAPER_OTHER_DATES"  # H21: not cheaper at the offered dates, cheaper at other accepted dates

PLAIN = {
    NOT_AVAILABLE: "No availability date is recorded for this apartment yet.",
    BEDROOM_COUNT_NOT_SELECTED: "Not one of the bedroom counts the party selected.",
    AREA_NOT_SELECTED: "Not in the area the party selected.",
    BATHROOMS_BELOW_REQUIRED: "Has one bathroom; the party must have two.",
    PARTY_TOO_LARGE: "More people than this apartment type may sleep.",
    BEDDING_NOT_FEASIBLE: "The party's sleeping choices cannot be met with this apartment's beds.",
    NO_FEASIBLE_DATES: "No move-in and move-out dates inside the party's accepted ranges pass the lease rules for this apartment.",
    OVER_BUDGET: "Every workable date combination costs more than the party's whole-apartment maximum allows.",
    BUNK_NOT_CHEAPER: "Using the bunk here is not cheaper than the cheapest option without a bunk, so the party's condition is not met.",
    TRAVEL_EXCEEDS_CEILING: "The estimated journeys for most of the party are more than double their stated travel time.",
    REJECTED_BY_CUSTOMER: "The party has already declined this apartment or building.",
    INVENTORY_UNREADABLE: "The apartment's bedding details cannot be read; the apartment list needs correcting.",
    TRAVEL_NEEDS_CONFIRMATION: "Travel time needs confirmation: journeys to the party's destinations have not been measured.",
    BEDDING_NEEDS_CONFIRMATION: "Sleeping arrangement needs confirming with the party before this counts as suitable.",
    BUNK_COMPARISON_UNAVAILABLE: "The party accepts a bunk only if cheaper, and there is no option without a bunk to compare against; confirm with them.",
    ESSENTIAL_NOTE_REVIEW: "The party's notes state an essential need; read and confirm it before treating any apartment as suitable.",
    PROVISIONAL_FINAL_SIZE: "Provisional: planned for the party's final size using the planning budget staff confirmed; future members' bed choices are unknown.",
    NO_DESTINATIONS: "Nobody in the party has a workplace or campus to travel to, so there is no travel time to check.",
    SPARE_BEDROOM: "The party would leave a bedroom unused.",
    WITHIN_ALLOWANCE: "Above the party's stated maximum but within the allowed margin.",
    LONG_JOURNEY: "At least one person's estimated journey is well above the party's stated travel time.",
    CHEAPER_OFFER_AVAILABLE: "Different dates inside the party's accepted ranges would cost less; both are shown.",
    TRADE_OFF: "Neither this option nor another listed option is better on every point; the team weighs the trade-off.",
    DATE_SEARCH_UNRESOLVED: "The party's accepted date ranges are too wide for the automatic date check to cover completely; dates and cost for this apartment are unresolved and need a manual check.",
    FOURTH_OPTION_PROVISIONAL: "Candidate fourth option for building variety; whether it is comparably suitable has not been assessed yet.",
    TRAVEL_NOT_CHECKED: "Travel was not checked for this apartment: travel is checked last, only for the apartments shortlisted on the other points.",
    REPLACED_AFTER_TRAVEL: "Was shortlisted on the other points, then replaced because it failed the travel check.",
    REPLACEMENT_AFTER_TRAVEL: "Took the place of an apartment that failed the travel check.",
    BUNK_CHEAPER_OTHER_DATES: "Using the bunk is not cheaper at the offered dates but is cheaper at other dates inside the party's accepted ranges; confirm with the party.",
    LEASED: "This apartment is recorded as leased.",
}

MAX_PAIRS = 2_000_000          # engineering bound on (start, end) pairs enumerated per apartment; beyond it the search is UNRESOLVED
                               # (explicit unknown: never an exclusion, never a cheapest-price claim), not silently narrowed
ESSENTIAL_RE = re.compile(r"\b(essential|must|need|needs|needed|require[sd]?|wheelchair|step[- ]free|accessib\w*|disabilit\w*|"
                          r"mobility|medical|allerg\w*|ground floor only|no stairs)\b", re.I)
FLEX_DAYS = {"Exact date": 0, "Within a week either way": 7, "Within two weeks either way": 14}
WIDE_FLEX = "Flexible by a month or more"
MUST_TWO_BATHS = "Must have 2 bathrooms"
PREFER_TWO_BATHS = "Prefer 2 bathrooms"
AREA_EITHER = "Either"
AREAS = {"Melbourne CBD": ("Melbourne CBD",), "South Yarra": ("South Yarra",), AREA_EITHER: ("Melbourne CBD", "South Yarra")}
# East Melbourne stock is outside both areas the form offers (literal interpretation, recorded 9 Nov 2026): it can only be an H27
# area alternative, never a full match for a CBD / South Yarra / Either selection.
SIZE_OF_TYPE = {code: info["size"] for code, info in rules.APT_TYPES.items()}
ONLY_IF_CHEAPER = "Only if cheaper"
STUDY = {"Studying", "Studying and working"}
WORK = {"Working", "Studying and working"}
MAY_WORK = {"Looking for work"}

TravelEstimates = Callable[[int, int, str], Optional[dict]]   # (apartment_id, person position, 'primary'|'secondary') -> {mode: minutes|UNMEASURED} | None


class InvalidRequirements(ValueError):
    pass


# ------------------------------------------------------------------------------------------------------- requirements
@dataclass(frozen=True)
class Requirements:
    """The party's firm requirements and preferences for ONE scenario, taken from the saved enquiry. No contact details."""
    scenario: str
    head_count: int
    people: tuple                        # per-person records: position, sleep, doing, institution, place, primary_destination
    bedroom_sizes: tuple                 # H26: selected sizes ("Studio", "1 bedroom", ...)
    priced_types: tuple                  # H9: the type codes the party priced (a preference, H26)
    bathrooms: tuple                     # H10 answers
    area: str
    transport: tuple
    max_travel: str
    extras: tuple
    extras_top: tuple
    whole_apartment_max: Decimal         # H9 numeric maximum (current) or the staff-confirmed planning budget (final_size)
    move_in: date
    move_in_window: tuple                # (earliest, latest) accepted move-in, inclusive
    move_out: date
    move_out_window: tuple
    bunk_condition: Optional[str]
    notes_review: bool                   # H20: notes carry an essential-type need
    rejected_apartment_ids: frozenset = frozenset()
    rejected_buildings: frozenset = frozenset()    # display: the stored building keys of active building rejections
    reoffer_allowed_ids: frozenset = frozenset()   # display: every apartment some record's narrow re-offer allowance names
    building_rejections: tuple = ()                # DECISION (GP14, 11 Nov 2026): one entry per active building rejection record,
                                                   # ((street, suburb, city), frozenset(allowed ids)); an allowance lifts only its own
                                                   # record and ANY applicable record excludes (see evaluate)

    @property
    def must_two_baths(self) -> bool:
        return MUST_TWO_BATHS in self.bathrooms

    @property
    def prefers_two_baths(self) -> bool:
        return PREFER_TWO_BATHS in self.bathrooms or self.must_two_baths

    @property
    def known_people(self) -> bool:
        return self.scenario == SCENARIO_CURRENT

    def as_dict(self) -> dict:
        return {"scenario": self.scenario, "head_count": self.head_count, "bedroom_sizes": list(self.bedroom_sizes),
                "priced_types": list(self.priced_types), "bathrooms": list(self.bathrooms), "area": self.area,
                "transport": list(self.transport), "max_travel": self.max_travel, "extras": list(self.extras), "extras_top": list(self.extras_top),
                "whole_apartment_max": str(self.whole_apartment_max), "move_in": self.move_in.isoformat(),
                "move_in_window": [d.isoformat() for d in self.move_in_window], "move_out": self.move_out.isoformat(),
                "move_out_window": [d.isoformat() for d in self.move_out_window], "bunk_condition": self.bunk_condition,
                "notes_review": self.notes_review, "rejected_apartment_ids": sorted(self.rejected_apartment_ids),
                "rejected_buildings": sorted(self.rejected_buildings), "reoffer_allowed_ids": sorted(self.reoffer_allowed_ids),
                "building_rejections": [{"identity": list(comps), "allowed": sorted(allowed)} for comps, allowed in
                                        sorted(self.building_rejections, key=lambda x: (tuple(c or "" for c in x[0]), sorted(x[1])))],
                "people": [{k: p.get(k) for k in PERSON_KEYS} for p in self.people]}   # every matching-relevant input, no contacts


PERSON_KEYS = ("position", "sleep", "doing", "institution", "place", "primary_destination")   # the per-person inputs matching uses


def _d(v, name: str) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except (TypeError, ValueError):
        raise InvalidRequirements(f"{name} must be an ISO date") from None


def _window(likely: date, flex: str, lo, hi, name: str) -> tuple[date, date]:
    if flex == WIDE_FLEX:
        if not lo or not hi:
            raise InvalidRequirements(f"{name}: wide flexibility needs explicit earliest and latest dates (H23)")
        lo_d, hi_d = _d(lo, name), _d(hi, name)
        if not (lo_d <= likely <= hi_d):
            raise InvalidRequirements(f"{name}: the likely date must lie inside the earliest/latest limits")
        return lo_d, hi_d
    if flex not in FLEX_DAYS:
        raise InvalidRequirements(f"{name}: unknown flexibility {flex!r}")
    k = FLEX_DAYS[flex]
    return likely - timedelta(days=k), likely + timedelta(days=k)


def building_key(address: str) -> str:
    """The building an apartment is in: 'Unit 11 / 35 Darling Street' -> '35 Darling Street'. No unit, no suburb lookup."""
    a = " ".join(str(address or "").split())
    if " / " in a:
        return a.split(" / ", 1)[1].strip()
    if a.endswith(" /"):
        return ""   # the unit/street delimiter with nothing after it ('Unit 3 / '): no street, so no building (HB-R2); nothing is guessed
    return a


# GP14 building identity shared by HomeBot and MateBot (11 Nov 2026, H-a/H-b/H-c follow-up). One building = the STREET component
# of the recorded address (building_key above, the single street parser) + the RECORDED suburb + the RECORDED city, each normalised
# like access facts normalise location text. The street is required: an apartment without one is never "a building". Suburb or
# city alone never identify a building; the same street text in another recorded locality is a different building. Unknown
# address formats (no " / ") are taken whole as the street and are not claimed canonical. No geocoding, no inference from current
# inventory: identities are computed from the row (or the immutable offered-basis snapshot row) they describe.
UNKNOWN = None   # an identity component the stored evidence does not record (legacy rows only; see identity_matches)


def _norm_location(s) -> str:
    from .access_facts import norm_address   # local import: access_facts imports db
    return norm_address(s)


def identity_components(address, suburb=UNKNOWN, city=UNKNOWN) -> tuple:
    """(street, suburb, city) normalised; UNKNOWN (None) marks a component the evidence does not carry."""
    return (_norm_location(building_key(address)),
            UNKNOWN if suburb is UNKNOWN else _norm_location(suburb),
            UNKNOWN if city is UNKNOWN else _norm_location(city))


def row_identity(a) -> tuple:
    """Identity components of an apartment ROW (live table row, inventory-snapshot row or offered-basis snapshot row). A locality key
    PRESENT on the row is KNOWN - its value normalised, a NULL/None value being the recorded EMPTY locality '' (H-N1: the apartments
    table allows a NULL suburb; inc2b normalised it to '' and a record for such a building must keep matching its own blank-locality
    units and never a named suburb). A key ABSENT from the row is UNKNOWN (legacy evidence that never carried that component)."""
    a = a or {}
    return (_norm_location(building_key(a.get("address"))),
            _norm_location(a.get("suburb")) if "suburb" in a else UNKNOWN,
            _norm_location(a.get("city")) if "city" in a else UNKNOWN)


def building_identity(address, suburb, city) -> str:
    """The stored form: 'street|suburb|city' ('35 darling street|south yarra|melbourne'); '' when there is no street. All three
    components are PRESENT here (current row values), so a NULL suburb/city is the recorded empty locality, never UNKNOWN."""
    comps = row_identity({"address": address, "suburb": suburb, "city": city})
    return "" if not comps[0] else "|".join(comps)


def identity_from_key(key) -> tuple:
    """Components of a STORED building key. A full identity contains '|' (legacy street-only keys never do: the parser keeps
    the text and the normaliser removes every non-alphanumeric). A legacy key yields (street, UNKNOWN, UNKNOWN)."""
    key = str(key or "")
    if "|" in key:
        parts = (key.split("|") + ["", ""])[:3]
        return (parts[0], parts[1], parts[2])
    return (_norm_location(key), UNKNOWN, UNKNOWN)


def identity_matches(record: tuple, candidate: tuple) -> bool:
    """Does a RECORD's building identity cover a candidate apartment's? The street must be present and equal; every component
    the record KNOWS must be known and equal on the candidate (a full-identity record never widens to a candidate whose suburb
    or city is missing); a component the record does not know (legacy row) is not constrained - the legacy record keeps the
    scope it was recorded with, never narrowed from live inventory and never widened beyond its street."""
    if not record or not candidate or len(record) < 3 or len(candidate) < 3:
        return False
    if not record[0] or record[0] != candidate[0]:
        return False
    for r, c in zip(record[1:3], candidate[1:3]):
        if r is UNKNOWN:
            continue
        if c is UNKNOWN or r != c:
            return False
    return True


def requirements_from_enquiry(e: dict, people: list, *, scenario: str = SCENARIO_CURRENT, planning_budget=None,
                              rejected_apartment_ids=(), rejected_buildings=(), reoffer_allowed_ids=(), building_rejections=None) -> Requirements:
    """Build the firm requirements from a saved HomeBot enquiry (homebot.get shape) and its people. `final_size` needs the
    staff-confirmed planning budget (H24) and plans for headcount_target people; it never invents future members' answers."""
    if scenario not in (SCENARIO_CURRENT, SCENARIO_FINAL_SIZE):
        raise InvalidRequirements("unknown scenario")
    ppl = []
    for p in people or []:
        if not isinstance(p, dict) or "sleep" not in p:
            raise InvalidRequirements("each person needs a saved record with a sleep choice")
        ppl.append({"position": int(p.get("position") or len(ppl) + 1), "sleep": p["sleep"], "doing": p.get("doing"),
                    "institution": p.get("institution"), "place": p.get("place"), "primary_destination": p.get("primary_destination")})
    ppl.sort(key=lambda p: p["position"])
    if scenario == SCENARIO_CURRENT:
        n = len(ppl)
        maximum = e.get("budget_total_weekly")
    else:
        n = int(e.get("headcount_target") or 0)
        if n <= len(ppl):
            raise InvalidRequirements("final-size planning needs a target larger than the current party (H24)")
        if planning_budget is None:
            raise InvalidRequirements("final-size planning needs the staff-confirmed planning budget (H24)")
        maximum = planning_budget
    if not 1 <= n <= bedding.MAX_PEOPLE:
        raise InvalidRequirements(f"party size must be 1–{bedding.MAX_PEOPLE}")
    try:
        maximum_d = budget.money(maximum, positive=True)
    except Exception:
        raise InvalidRequirements("the whole-apartment weekly maximum must be a positive amount (H9)") from None
    mi, mo = _d(e.get("move_in"), "move_in"), _d(e.get("move_out"), "move_out")
    if mo <= mi:
        raise InvalidRequirements("move-out must be after move-in")
    win_in = _window(mi, e.get("move_in_flex") or "", e.get("move_in_earliest"), e.get("move_in_latest"), "move_in")
    win_out = _window(mo, e.get("move_out_flex") or "", e.get("move_out_earliest"), e.get("move_out_latest"), "move_out")
    sizes = tuple(s for s in (e.get("bedrooms") or []) if s in SIZE_OF_TYPE.values())
    if not sizes:
        raise InvalidRequirements("at least one selected bedroom count is required (H26)")
    baths = tuple(b for b in (e.get("bathrooms") or []))
    area = e.get("area")
    if area not in AREAS:
        raise InvalidRequirements("unknown area selection")
    transport = tuple(t for t in (e.get("transport") or []) if t in travel.TRANSPORT)
    if not transport or (e.get("max_travel") not in travel.STATED_LIMIT):
        raise InvalidRequirements("transport choices and a maximum travel time are required (H28)")
    notes = e.get("apartment_notes") or ""
    return Requirements(scenario=scenario, head_count=n, people=tuple(ppl), bedroom_sizes=sizes,
                        priced_types=tuple(t for t in (e.get("budget_types") or []) if t in rules.APT_TYPES), bathrooms=baths, area=area,
                        transport=transport, max_travel=e["max_travel"], extras=tuple(e.get("extras") or []), extras_top=tuple(e.get("extras_top") or []),
                        whole_apartment_max=maximum_d, move_in=mi, move_in_window=win_in, move_out=mo, move_out_window=win_out,
                        bunk_condition=e.get("bunk_condition"), notes_review=bool(notes and ESSENTIAL_RE.search(notes)),
                        rejected_apartment_ids=frozenset(int(x) for x in rejected_apartment_ids),
                        rejected_buildings=frozenset(building_key(b) for b in rejected_buildings),
                        reoffer_allowed_ids=frozenset(int(x) for x in reoffer_allowed_ids),
                        building_rejections=_building_rejection_records(rejected_buildings, reoffer_allowed_ids, building_rejections))


def _building_rejection_records(rejected_buildings, reoffer_allowed_ids, building_rejections) -> tuple:
    """`building_rejections` (iterable of (components, allowed ids)) is the record-scoped shape homebot_shortlist supplies. When a
    caller only passes the flat legacy kwargs, each building becomes one record with the flat allowance (the pre-11-Nov meaning,
    street-only identity, for older callers and tests); the two shapes are never mixed."""
    if building_rejections is not None:
        return tuple((tuple(comps), frozenset(int(x) for x in allowed)) for comps, allowed in building_rejections)
    flat = frozenset(int(x) for x in reoffer_allowed_ids)
    return tuple((identity_components(b), flat) for b in rejected_buildings)


# ------------------------------------------------------------------------------------------------------------- offers
@dataclass(frozen=True)
class Offer:
    """One concrete (move-in, move-out) pair for one apartment that passes every date rule, with its exact money."""
    start: date
    end: date
    rate_kind: str
    weekly_rent: Decimal
    total_weekly_rent: Decimal          # rent + extra-bed charge for the party (A5/A16)
    budget_code: str                    # budget.WITHIN_MAX / WITHIN_ALLOWANCE / OVER_ALLOWANCE
    future_empty_days: int
    permitted_empty_days: int
    stay_days: int
    date_distance_days: int             # |start - preferred move-in| + |end - preferred move-out|
    exit_window: str
    exception_ref: Optional[str] = None
    exception_total: Optional[Decimal] = None
    exception_approved_at: Optional[str] = None
    exception_customer_agreed: bool = False

    @property
    def within_budget(self) -> bool:
        return self.budget_code != budget.OVER_ALLOWANCE

    def as_dict(self) -> dict:
        result = {"start": self.start.isoformat(), "end": self.end.isoformat(), "rate_kind": self.rate_kind, "weekly_rent": f"{budget.cents(self.weekly_rent):.2f}",
                "total_weekly_rent": f"{budget.cents(self.total_weekly_rent):.2f}", "budget_code": self.budget_code,
                "future_empty_days": self.future_empty_days, "permitted_empty_days": self.permitted_empty_days, "stay_days": self.stay_days,
                "date_distance_days": self.date_distance_days, "exit_window": self.exit_window}
        if self.exception_ref is not None:
            result.update(exception_ref=self.exception_ref,
                          exception_total=rules._lossless_decimal_text(self.exception_total),
                          exception_approved_at=self.exception_approved_at,
                          exception_customer_agreed=self.exception_customer_agreed)
        return result


@dataclass(frozen=True)
class DateSearch:
    offers: tuple                 # every date-feasible offer (passing A9/A10/A17), any budget outcome
    unresolved: bool              # True = the accepted ranges exceed MAX_PAIRS; offers is empty and proves nothing
    starts_checked: int
    pairs_checked: int

    @property
    def affordable(self) -> tuple:
        return tuple(o for o in self.offers if o.within_budget)


def _rent(apt: dict, kind: str) -> Decimal:
    return budget.money(apt["rent_6m"] if kind == "6m" else apt["rent_12m"], positive=True)


def search_offers(req: Requirements, apt: dict, today: date, *, exception_offers=()) -> DateSearch:
    """COMPLETE enumeration of every (start, end) inside the accepted windows, day by day, that passes A9 (calendar-month
    minimum), A10 (exit window) and A17 (vacancy counted from the later of today and the availability date), each priced at ITS
    OWN tariff (A6 via rules.rate_kind) with the A5 extra-bed charge and classed under A16. Nothing is pre-chosen: the earliest
    start, later starts, tariff boundaries and earlier exits are all here. The only pruning is exact: a start before the
    availability date can never pass A17, and an end on or before the start or before the A9 minimum can never pass. If the
    ranges would need more than MAX_PAIRS pairs the result is UNRESOLVED (explicit unknown), never a narrowed scan."""
    avail = _d(apt["available_from"], "available_from")
    in_lo, in_hi = req.move_in_window
    out_lo, out_hi = req.move_out_window
    if ((in_hi - in_lo).days + 1) * ((out_hi - out_lo).days + 1) > MAX_PAIRS:
        return DateSearch((), True, 0, 0)
    min_months = apt.get("min_lease_months") if apt.get("min_lease_months") is not None else rules.DEFAULT_MIN_LEASE_MONTHS
    code = rules.normalise_type(apt["type_code"])
    extra = budget.extra_bed_charge(code, req.head_count)
    offers, starts, pairs = [], 0, 0
    start = max(in_lo, avail)                       # A2(e)/A17: a start before availability can never pass check_vacancy
    exits = [d for d in (out_lo + timedelta(days=k) for k in range((out_hi - out_lo).days + 1)) if lease_dates.exit_window(d)]
    while start <= in_hi:
        starts += 1
        try:
            earliest_end = lease_dates.min_lease_end(start, min_months)
        except ValueError:
            earliest_end = None
        if earliest_end is None:                     # fractional / unusable minimum: held for staff by every date (A9), no offer
            start += timedelta(days=1)
            continue
        gap = max(0, lease_dates.future_vacancy_days(today, avail, start))
        for end in exits:
            if end <= start or end < earliest_end:
                continue
            pairs += 1
            stay = (end - start).days
            cap = stay // lease_dates.VACANCY_CAP_DIVISOR
            if gap > cap:
                continue
            kind = rules.rate_kind(start, end)
            rent = _rent(apt, kind)
            res = budget.check_total_budget(rent, code, req.head_count, req.whole_apartment_max)
            offers.append(Offer(start, end, kind, rent, res.total_weekly_rent, res.reason_code, gap, cap, stay,
                                abs((start - req.move_in).days) + abs((end - req.move_out).days), lease_dates.exit_window(end) or ""))
        start += timedelta(days=1)
    assert all(o.total_weekly_rent == o.weekly_rent + extra for o in offers)
    # A10 adds only the exact authorised pairs, never a scan of blackout dates.
    from . import exception_rent
    for exc in exception_offers:
        start, end = exc.move_in, exc.move_out
        if not (in_lo <= start <= in_hi and out_lo <= end <= out_hi):
            continue
        minimum = lease_dates.check_min_lease(start, end, min_months)
        vacancy = lease_dates.check_vacancy(today, avail, start, end)
        if not minimum.passed or not vacancy.passed:
            continue
        kind = rules.rate_kind(start, end)
        rent = _rent(apt, kind)
        result = exception_rent.check_exception_rent(
            exc.exception_ref, code, req.head_count, req.whole_apartment_max, end, rent, exc.exception_total,
            staff_exception=exc.staff_exception, customer_agreement=exc.customer_agreement)
        if not result.passed:
            continue
        stay = (end - start).days
        gap = max(0, lease_dates.future_vacancy_days(today, avail, start))
        offers.append(Offer(start, end, kind, rent, exc.exception_total, result.reason_code, gap,
                            stay // lease_dates.VACANCY_CAP_DIVISOR, stay,
                            abs((start - req.move_in).days) + abs((end - req.move_out).days),
                            "Staff-authorised exact exit", exc.exception_ref, exc.exception_total,
                            exc.staff_exception.approved_at, exc.customer_agreement is not None))
    return DateSearch(tuple(offers), False, starts, pairs)


def closest_offer(offers) -> Optional[Offer]:
    """Preference among affordable offers for ONE apartment: closest to the party's own dates, then cheaper total, then fewer
    future empty days, then longer stay, then earlier start. Deterministic; the full list stays available to staff."""
    return min(offers, key=lambda o: (o.date_distance_days, o.total_weekly_rent, o.future_empty_days, -o.stay_days, o.start), default=None)


def cheapest_offer(offers) -> Optional[Offer]:
    return min(offers, key=lambda o: (o.total_weekly_rent, o.date_distance_days, o.future_empty_days, -o.stay_days, o.start), default=None)


# ------------------------------------------------------------------------------------------------------------- travel
def _destinations(p: dict) -> tuple[Optional[str], Optional[str]]:
    """(primary kind, secondary kind) for a person: 'study' / 'work' / None. H28(f): work + study → the nominated primary."""
    doing = p.get("doing")
    if doing == "Studying and working":
        prim = "study" if (p.get("primary_destination") or "Study") == "Study" else "work"
        return prim, ("work" if prim == "study" else "study")
    if doing in STUDY:
        return "study", None
    if doing in WORK:
        return "work", None
    if doing in MAY_WORK and p.get("place"):
        return "work", None
    return None, None


def _destination(pref: travel.TravelPreference, routes: Optional[dict]) -> travel.Destination:
    if not routes:
        routes = {m: travel.UNMEASURED for m in pref.accepted_modes}
    return travel.Destination({m: routes.get(m, travel.UNMEASURED) for m in pref.accepted_modes}, pref)


def assess_travel(req: Requirements, apt: dict, estimates: Optional[TravelEstimates]) -> travel.TravelAssessment:
    pref = travel.TravelPreference(req.max_travel, req.transport)
    people = []
    for p in req.people:
        prim_kind, sec_kind = _destinations(p)
        if prim_kind is None:
            people.append(travel.PersonTravel(p["position"], None))
            continue
        prim = _destination(pref, estimates(int(apt["id"]), p["position"], "primary") if estimates else None)
        sec = _destination(pref, estimates(int(apt["id"]), p["position"], "secondary") if estimates else None) if sec_kind else None
        people.append(travel.PersonTravel(p["position"], prim, sec))
    return travel.assess_travel(pref, people)


# ------------------------------------------------------------------------------------------------------- evaluation
@dataclass
class Candidate:
    apartment_id: int
    address: str
    building: str
    suburb: str
    type_code: str
    bathrooms: int
    klass: str = EXCLUDED
    exclusion_reasons: list = field(default_factory=list)
    confirmation_reasons: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    offer: Optional[Offer] = None            # the closest-dates affordable offer
    cheapest: Optional[Offer] = None         # the cheapest affordable offer (same as offer when they coincide)
    identity: tuple = ()                     # GP14 building identity components (street, suburb, city); UNKNOWN where the row lacks one
    offers_affordable: int = 0
    offers_date_feasible: int = 0
    date_search_unresolved: bool = False
    bedding: Optional[dict] = None
    uses_bunk: bool = False
    nonbunk_feasible: bool = False
    travel: Optional[dict] = None
    features: Optional[dict] = None
    layout_priced: bool = False
    two_bathrooms: bool = False
    area_ok: bool = True
    baths_ok: bool = True
    bunk_comparison: Optional[dict] = None
    pareto_rank: Optional[int] = None        # number of listed options that beat this one on every measured point (0 = none)
    order: Optional[int] = None

    @property
    def listed(self) -> bool:
        return self.klass in (SUITABLE, NEEDS_CONFIRMATION) and self.offer is not None

    @property
    def unresolved(self) -> bool:
        return self.date_search_unresolved and self.klass != EXCLUDED

    @property
    def alternative(self) -> bool:
        return self.klass in (ALTERNATIVE_AREA, ALTERNATIVE_BATHROOM)

    def as_dict(self) -> dict:
        return {"apartment_id": self.apartment_id, "address": self.address, "building": self.building, "suburb": self.suburb,
                "type_code": self.type_code, "bathrooms": self.bathrooms, "class": self.klass,
                "exclusion_reasons": list(self.exclusion_reasons), "confirmation_reasons": list(self.confirmation_reasons), "flags": list(self.flags),
                "offer": self.offer.as_dict() if self.offer else None, "cheapest_offer": self.cheapest.as_dict() if self.cheapest else None,
                "offers_affordable": self.offers_affordable, "offers_date_feasible": self.offers_date_feasible,
                "date_search_unresolved": self.date_search_unresolved, "bedding": self.bedding, "uses_bunk": self.uses_bunk,
                "travel": self.travel, "features": self.features, "layout_priced": self.layout_priced, "two_bathrooms": self.two_bathrooms,
                "bunk_comparison": self.bunk_comparison, "pareto_rank": self.pareto_rank, "order": self.order}


def _nonbunk_people(people) -> list:
    """The same party with every bunk acceptance replaced by a single bed in a twin room: the non-bunk option H21 compares against."""
    return [dict(p, sleep=bedding.TWIN if p["sleep"] in (bedding.BUNK, bedding.TWIN_OR_BUNK) else p["sleep"]) for p in people]


def evaluate(req: Requirements, apt: dict, today: date, estimates: Optional[TravelEstimates] = None, *, exception_offers=()) -> Candidate:
    """Everything about ONE apartment for this party except the cross-apartment bunk comparison and ranking (see build_shortlist):
    the non-travel checks (evaluate_without_travel) followed by the travel check (apply_travel). build_shortlist runs the two
    steps apart, because travel is checked last and only for the shortlisted apartments (GP6(d), H20)."""
    c = evaluate_without_travel(req, apt, today, exception_offers=exception_offers)
    apply_travel(req, c, apt, estimates)
    return c


def _classify(c: Candidate) -> None:
    """The class from the reasons recorded so far. Firm exclusions first; H27 allows one of the two exceptions, never both."""
    firm = [r for r in c.exclusion_reasons if r not in (AREA_NOT_SELECTED, BATHROOMS_BELOW_REQUIRED)]
    if firm or (not c.area_ok and not c.baths_ok):
        c.klass = EXCLUDED
    elif not c.area_ok:
        c.klass = ALTERNATIVE_AREA
    elif not c.baths_ok:
        c.klass = ALTERNATIVE_BATHROOM
    else:
        c.klass = NEEDS_CONFIRMATION if c.confirmation_reasons else SUITABLE


def apply_travel(req: Requirements, c: Candidate, apt: dict, estimates: Optional[TravelEstimates]) -> None:
    """The H28 travel check for one apartment, applied on top of its non-travel result. Never a pass without measured
    journeys for every known destination; an unknown journey is not a failure either (needs confirmation). Safe to apply
    once per candidate only."""
    if not req.known_people:
        return
    t = assess_travel(req, apt, estimates)
    c.travel = t.as_dict()
    if t.status == travel.EXCEEDS_CEILING:
        firm = [r for r in c.exclusion_reasons if r not in (AREA_NOT_SELECTED, BATHROOMS_BELOW_REQUIRED)]
        c.exclusion_reasons = firm + [TRAVEL_EXCEEDS_CEILING] + [r for r in c.exclusion_reasons if r not in firm]
    elif t.status == travel.NEEDS_CONFIRMATION:
        # kept where it has always been read: before the note-review and bunk-comparison reasons
        later = [i for i, r in enumerate(c.confirmation_reasons) if r in (ESSENTIAL_NOTE_REVIEW, BUNK_COMPARISON_UNAVAILABLE, BUNK_CHEAPER_OTHER_DATES)]
        c.confirmation_reasons.insert(later[0] if later else len(c.confirmation_reasons), TRAVEL_NEEDS_CONFIRMATION)
    elif t.status == travel.NO_DESTINATIONS:
        c.flags.append(NO_DESTINATIONS)
    if any(r in (travel.LONG_JOURNEY_WARNING, travel.LONG_JOURNEY_PROVISIONAL) for r in t.reasons):
        c.flags.append(LONG_JOURNEY)
    was = c.klass
    _classify(c)
    if was == EXCLUDED:
        c.klass = EXCLUDED   # a travel result never brings back an apartment that was already ruled out


def evaluate_without_travel(req: Requirements, apt: dict, today: date, *, exception_offers=()) -> Candidate:
    """Every non-travel check for ONE apartment. Order of checks is fixed so staff always see every binding reason, not just
    the first. Travel is not looked at here."""
    code = rules.normalise_type(apt["type_code"])
    c = Candidate(int(apt["id"]), apt.get("address") or "", building_key(apt.get("address") or ""), apt.get("suburb") or "", code,
                  int(apt.get("bathrooms") or rules.APT_TYPES[code]["bathrooms"]))
    c.identity = row_identity(apt)   # present NULL locality = known empty ''; absent key (legacy snapshot) = UNKNOWN
    c.two_bathrooms = c.bathrooms >= 2
    c.layout_priced = code in req.priced_types
    excl: list = []
    # GP14: an apartment record always excludes its apartment; a building record excludes every apartment whose identity it
    # covers except the ones ITS OWN allowance names; any applicable record excludes. '' street never matches on either side.
    if c.apartment_id in req.rejected_apartment_ids or any(identity_matches(comps, c.identity) and c.apartment_id not in allowed
                                                            for comps, allowed in req.building_rejections):
        excl.append(REJECTED_BY_CUSTOMER)
    if apt.get("leased_at"):
        excl.append(LEASED)
    if not apt.get("available_from"):
        excl.append(NOT_AVAILABLE)
    if SIZE_OF_TYPE[code] not in req.bedroom_sizes:
        excl.append(BEDROOM_COUNT_NOT_SELECTED)
    c.area_ok = c.suburb in AREAS[req.area]
    c.baths_ok = not req.must_two_baths or c.two_bathrooms
    if req.head_count > rules.APT_TYPES[code]["a4b_max"]:
        excl.append(PARTY_TOO_LARGE)
    # bedding (A4/H21): the party's own choices, and the non-bunk variant for the "only if cheaper" comparison
    if PARTY_TOO_LARGE not in excl:
        if req.known_people:
            b = bedding.assess(code, apt.get("bedroom1"), apt.get("bedroom2"), apt.get("bedroom3"), list(req.people))
            c.bedding = b.as_dict()
            if b.status == bedding.UNKNOWN_INVENTORY:
                excl.append(INVENTORY_UNREADABLE)
            elif b.status in (bedding.NOT_FEASIBLE, bedding.INVALID_PARTY):
                excl.append(BEDDING_NOT_FEASIBLE)
            else:
                if b.status == bedding.POSSIBLE_NEEDS_STAFF_CONFIRMATION:
                    c.confirmation_reasons.append(BEDDING_NEEDS_CONFIRMATION)
                arr = b.facts.get("arrangement") or {}
                c.uses_bunk = bool(arr.get("uses_bunk"))
                if arr.get("spare_rooms"):
                    c.flags.append(SPARE_BEDROOM)
                if c.uses_bunk:
                    nb = bedding.assess(code, apt.get("bedroom1"), apt.get("bedroom2"), apt.get("bedroom3"), _nonbunk_people(req.people))
                    c.nonbunk_feasible = nb.status == bedding.VERIFIED_FEASIBLE
                else:
                    c.nonbunk_feasible = True
        else:
            # final-size planning (H24): future members' bed choices are unknown, so only physical capacity is checked
            try:
                rooms = bedding.parse_apartment(code, apt.get("bedroom1"), apt.get("bedroom2"), apt.get("bedroom3"))
                places = max(sum(bedding.places(s) for s in combo) for combo in rooms.physical_combos)
                if req.head_count > places:
                    excl.append(BEDDING_NOT_FEASIBLE)
                c.bedding = {"status": "capacity_only", "places_physical_max": places, "head_count": req.head_count}
            except bedding.InvalidInventory:
                excl.append(INVENTORY_UNREADABLE)
            c.confirmation_reasons.append(PROVISIONAL_FINAL_SIZE)
            c.nonbunk_feasible = True
    # dates and money (A9/A10/A17/A6/A16): the complete offer search
    if NOT_AVAILABLE not in excl and LEASED not in excl:
        ds = search_offers(req, apt, today, exception_offers=exception_offers) if exception_offers else search_offers(req, apt, today)
        c.offers_date_feasible, c.date_search_unresolved = len(ds.offers), ds.unresolved
        aff = ds.affordable
        c.offers_affordable = len(aff)
        if ds.unresolved:
            c.confirmation_reasons.append(DATE_SEARCH_UNRESOLVED)    # unknown, not excluded; no offer and no price is claimed
        elif not ds.offers:
            excl.append(NO_FEASIBLE_DATES)
        elif not aff:
            excl.append(OVER_BUDGET)
        else:
            c.offer, c.cheapest = closest_offer(aff), cheapest_offer(aff)
            if c.offer.budget_code == budget.WITHIN_ALLOWANCE:
                c.flags.append(WITHIN_ALLOWANCE)
            if c.cheapest.total_weekly_rent < c.offer.total_weekly_rent:
                c.flags.append(CHEAPER_OFFER_AVAILABLE)
    # features: preference facts only (H11/H20)
    c.features = feat.compare(list(req.extras), list(req.extras_top), apt)
    if req.notes_review:
        c.confirmation_reasons.append(ESSENTIAL_NOTE_REVIEW)
    # class
    c.exclusion_reasons = excl + ([AREA_NOT_SELECTED] if not c.area_ok else []) + ([BATHROOMS_BELOW_REQUIRED] if not c.baths_ok else [])
    _classify(c)
    return c


# ------------------------------------------------------------------------------------------------- bunk comparison
def apply_bunk_condition(req: Requirements, cands: list) -> None:
    """H21 'bunks only if cheaper', across the evaluated inventory. A candidate that can ONLY house the party by using the bunk is
    compared against the CHEAPEST qualifying non-bunk option across all of that option's accepted dates (its `cheapest`, exact
    money, same class tier: full matches with full matches, each alternative kind with its own kind).
      - bunk offer (closest accepted dates) cheaper than that → accepted, comparison recorded;
      - not cheaper at the offered dates but the bunk's own cheapest accepted dates are → needs confirmation (BUNK_CHEAPER_OTHER_DATES);
      - not cheaper anywhere → excluded (BUNK_NOT_CHEAPER);
      - no resolved comparator → needs confirmation, no saving claimed. A comparator whose dates are UNRESOLVED cannot prove
        the bunk cheaper (its price is unknown) but a resolved not-cheaper comparison still stands."""
    if req.bunk_condition != ONLY_IF_CHEAPER:
        return
    tiers = {SUITABLE: "match", NEEDS_CONFIRMATION: "match", ALTERNATIVE_AREA: ALTERNATIVE_AREA, ALTERNATIVE_BATHROOM: ALTERNATIVE_BATHROOM}
    for c in cands:
        if c.klass == EXCLUDED or not c.uses_bunk or c.nonbunk_feasible or c.offer is None:
            continue
        tier = tiers[c.klass]
        pool = [o for o in cands if o is not c and o.klass != EXCLUDED and o.nonbunk_feasible and tiers[o.klass] == tier]
        resolved = [o for o in pool if o.cheapest is not None]
        unresolved = [o for o in pool if o.cheapest is None]
        bunk_offer, bunk_cheapest = c.offer.total_weekly_rent, c.cheapest.total_weekly_rent
        money = lambda v: f"{budget.cents(v):.2f}"
        if not resolved:
            c.bunk_comparison = {"result": "no_comparator", "bunk_total_weekly_rent": money(bunk_offer),
                                 "unresolved_comparators": [o.apartment_id for o in unresolved]}
            c.confirmation_reasons.append(BUNK_COMPARISON_UNAVAILABLE)
            if c.klass == SUITABLE:
                c.klass = NEEDS_CONFIRMATION
            continue
        best = min(resolved, key=lambda o: (o.cheapest.total_weekly_rent, o.apartment_id))
        ref = best.cheapest.total_weekly_rent
        c.bunk_comparison = {"bunk_total_weekly_rent": money(bunk_offer), "bunk_cheapest_total_weekly_rent": money(bunk_cheapest),
                             "cheapest_non_bunk_total_weekly_rent": money(ref), "cheapest_non_bunk_apartment_id": best.apartment_id,
                             "cheapest_non_bunk_dates": [best.cheapest.start.isoformat(), best.cheapest.end.isoformat()],
                             "unresolved_comparators": [o.apartment_id for o in unresolved]}
        if bunk_offer < ref and not unresolved:
            c.bunk_comparison["result"] = "cheaper"
        elif bunk_offer < ref:
            c.bunk_comparison["result"] = "cheaper_than_resolved_options_only"
            c.confirmation_reasons.append(BUNK_COMPARISON_UNAVAILABLE)
            if c.klass == SUITABLE:
                c.klass = NEEDS_CONFIRMATION
        elif bunk_cheapest < ref:
            c.bunk_comparison["result"] = "cheaper_at_other_dates"
            c.confirmation_reasons.append(BUNK_CHEAPER_OTHER_DATES)
            if c.klass == SUITABLE:
                c.klass = NEEDS_CONFIRMATION
        else:
            c.bunk_comparison["result"] = "not_cheaper"
            c.klass = EXCLUDED
            c.exclusion_reasons.append(BUNK_NOT_CHEAPER)


# ------------------------------------------------------------------------------------------------------------ ranking
def _dims(c: Candidate) -> dict:
    """Measured NON-TRAVEL dimensions for Pareto screening. Lower is better. Travel is not here: it is checked last, on the
    shortlisted apartments only, and is never an initial ranking input (GP6(d), H20)."""
    return {"date_distance": c.offer.date_distance_days, "empty_days": c.offer.future_empty_days, "total_rent": c.offer.total_weekly_rent,
            "features_missing": -((c.features or {}).get("counts", {}).get("available", 0)),   # more available features = better
            "layout": 0 if c.layout_priced else 1, "baths": 0 if c.two_bathrooms else 1}


def dominates(a: dict, b: dict, prefers_two_baths: bool) -> bool:
    """a beats b: no worse on every comparable dimension and strictly better on at least one."""
    better = False
    for k in ("date_distance", "empty_days", "total_rent", "features_missing", "layout", "baths"):
        if k == "baths" and not prefers_two_baths:
            continue
        x, y = a[k], b[k]
        if x is None or y is None:
            continue
        if x > y:
            return False
        if x < y:
            better = True
    return better


def _material(a: dict, b: dict) -> bool:
    """Two non-dominated options differ 'materially' when they differ on any measured point at all; the module does not
    decide how much matters (no weights, H20) — it only refuses to hide the difference."""
    return any(a[k] != b[k] for k in ("date_distance", "empty_days", "total_rent") if a[k] is not None and b[k] is not None)


def _settled(c: Candidate) -> bool:
    """Nothing but travel still needs confirming (travel plays no part in the initial order)."""
    return not [r for r in c.confirmation_reasons if r != TRAVEL_NEEDS_CONFIRMATION]


def rank(cands: list, req: Requirements) -> list:
    """Pareto screening then the PROVISIONAL priority order on the non-travel points (H20: date fit, layout, price,
    amenities). Returns the candidates that have an offer in order, with pareto_rank, order and TRADE_OFF flags set.
    Alternatives are ranked the same way, separately. Travel is not an input (GP6(d))."""
    out = []
    for group in ([c for c in cands if c.listed], [c for c in cands if c.klass == ALTERNATIVE_AREA and c.offer is not None],
                  [c for c in cands if c.klass == ALTERNATIVE_BATHROOM and c.offer is not None]):
        dims = {c.apartment_id: _dims(c) for c in group}
        for c in group:
            c.pareto_rank = sum(1 for o in group if o is not c and dominates(dims[o.apartment_id], dims[c.apartment_id], req.prefers_two_baths))
        for c in group:
            if c.pareto_rank == 0 and any(o is not c and o.pareto_rank == 0 and _material(dims[c.apartment_id], dims[o.apartment_id]) for o in group):
                c.flags.append(TRADE_OFF)
        group.sort(key=lambda c: (c.pareto_rank, 0 if _settled(c) else 1, c.offer.date_distance_days, c.offer.future_empty_days,
                                  0 if c.layout_priced else 1, 0 if (c.two_bathrooms or not req.prefers_two_baths) else 1,
                                  c.offer.total_weekly_rent, dims[c.apartment_id]["features_missing"], c.apartment_id))
        for i, c in enumerate(group, start=1):
            c.order = i
        out.extend(group)
    return out


# ---------------------------------------------------------------------------------------------------------- shortlist
@dataclass
class Shortlist:
    scenario: str
    listed: list                   # up to three full matches in provisional order
    alternatives: list             # H27 alternatives, only when fewer than three full matches exist
    candidates: list               # every evaluated apartment (for "why not" and reruns)
    fourth_option: Optional[Candidate] = None   # RETIRED (GP6(d), 16 Nov 2026): always None now; the key stays for older readers
    fourth_option_reason: Optional[str] = None
    unresolved: list = field(default_factory=list)   # candidates whose dates/cost could not be enumerated (explicit unknown)
    notes_review: bool = False

    @property
    def has_shortlist(self) -> bool:
        return bool(self.listed or self.alternatives)

    def summary(self) -> dict:
        n = {k: 0 for k in CLASSES}
        for c in self.candidates:
            n[c.klass] += 1
        return {"scenario": self.scenario, "counts": n, "listed": len(self.listed), "alternatives": len(self.alternatives),
                "fourth_option": self.fourth_option.apartment_id if self.fourth_option else None, "fourth_option_reason": self.fourth_option_reason,
                "unresolved": [c.apartment_id for c in self.unresolved], "notes_review": self.notes_review, "has_shortlist": self.has_shortlist,
                "nothing_suitable_yet": not any(c.klass == SUITABLE for c in self.listed)}

    def as_dict(self) -> dict:
        return {"summary": self.summary(), "listed": [c.as_dict() for c in self.listed], "alternatives": [c.as_dict() for c in self.alternatives],
                "fourth_option": self.fourth_option.as_dict() if self.fourth_option else None,
                "unresolved": [c.as_dict() for c in self.unresolved], "candidates": [c.as_dict() for c in self.candidates]}


def _prepare(estimates, apartment_ids) -> None:
    """Let a live estimates object measure the journeys for these apartments now (and only these). Stored or absent
    estimates have nothing to prepare."""
    prepare = getattr(estimates, "prepare", None)
    if prepare is not None and apartment_ids:
        prepare(list(apartment_ids))


def _travel_last(req: Requirements, ordered: list, by_id: dict, estimates, room: int) -> list:
    """GP6(d): take the candidates in their non-travel order, check travel for as many as there is room for, replace any
    that fail (over the doubled limit) with the next candidate and check that one, and stop when the room is filled or the
    candidates run out. An unknown journey is not a failure: that apartment stays, reading 'needs confirmation'. Journeys
    are measured for the apartments reached here and for no others."""
    kept, queue, replacing = [], list(ordered), False
    while len(kept) < room and queue:
        batch, queue = queue[:room - len(kept)], queue[room - len(kept):]
        _prepare(estimates, [c.apartment_id for c in batch])
        failed = False
        for c in batch:
            apply_travel(req, c, by_id[c.apartment_id], estimates)
            if replacing:
                c.flags.append(REPLACEMENT_AFTER_TRAVEL)
            if c.klass == EXCLUDED:
                c.flags.append(REPLACED_AFTER_TRAVEL)
                failed = True
            else:
                kept.append(c)
        replacing = replacing or failed
    return kept


def build_shortlist(req: Requirements, apartments: list, today: date, estimates: Optional[TravelEstimates] = None, progress=None, *,
                    exception_offers_by_apartment=None) -> Shortlist:
    """The pure entry point: evaluate every apartment row, apply the bunk condition across the inventory, rank, and assemble the
    H25/H27/GP6(d) structure. Apartment rows are plain dicts in the `apartments` table shape (id, address, suburb, type_code,
    bathrooms, rent_12m, rent_6m, available_from, min_lease_months, bedroom1..3, feat_* columns).
    `progress(done, total)` (optional) is called after each apartment is evaluated; it may raise to abandon the run (the H2 job
    runner uses it for owner-fenced lease heartbeats). It never changes a result."""
    cands, by_id = [], {int(a["id"]): a for a in apartments}
    for i, a in enumerate(apartments, start=1):
        exact = (exception_offers_by_apartment or {}).get(int(a["id"]), ())
        cands.append(evaluate_without_travel(req, a, today, exception_offers=exact) if exact else evaluate_without_travel(req, a, today))
        if progress is not None:
            progress(i, len(apartments))
    apply_bunk_condition(req, cands)
    rank(cands, req)
    # GP6(d): travel last. Full matches first, up to three; a failed one is replaced by the next and the replacement checked.
    listed = _travel_last(req, sorted([c for c in cands if c.listed], key=lambda c: c.order), by_id, estimates, MAX_SHORTLIST)
    # H27/H25: alternatives only when fewer than three full matches remain, and only inside the same maximum of three
    alts: list = []
    if len(listed) < MAX_SHORTLIST:
        alts = _travel_last(req, sorted([c for c in cands if c.alternative and c.offer is not None], key=lambda c: (c.klass, c.order)),
                            by_id, estimates, MAX_SHORTLIST - len(listed))
    # everything else was not shortlisted, so its journeys are not measured: it reads 'not checked', never a pass
    for c in cands:
        if c.travel is None and req.known_people:
            apply_travel(req, c, by_id[c.apartment_id], None)
            if c.klass != EXCLUDED:
                c.flags.append(TRAVEL_NOT_CHECKED)
    for i, c in enumerate(listed, start=1):
        c.order = i
    return Shortlist(req.scenario, listed, alts, cands, None, None, [c for c in cands if c.unresolved], req.notes_review)
