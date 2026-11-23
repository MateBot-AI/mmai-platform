"""MMAI Stage 1: rule-based engine (pure functions, no I/O).

Every function cites the MMAI Rule Register rule it enforces. Rule-based code runs BEFORE
the AI assessment (T4(b)) so no proposed group can break a rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import product
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# A14 apartment type codes; A2(b) bathrooms come from the type code
# ---------------------------------------------------------------------------
APT_TYPES = {
    "Studio": {"label": "Studio", "bedrooms": 1, "bathrooms": 1, "size": "Studio", "standard_capacity": 2, "a4b_max": 3},
    "1BR": {"label": "1 bedroom", "bedrooms": 1, "bathrooms": 1, "size": "1 bedroom", "standard_capacity": 2, "a4b_max": 3},
    "2B1B": {"label": "2 bedroom / 1 bathroom", "bedrooms": 2, "bathrooms": 1, "size": "2 bedrooms", "standard_capacity": 4, "a4b_max": 5},
    "2B2B": {"label": "2 bedroom / 2 bathroom", "bedrooms": 2, "bathrooms": 2, "size": "2 bedrooms", "standard_capacity": 4, "a4b_max": 5},
    "3B": {"label": "3 bedroom / 2 bathroom", "bedrooms": 3, "bathrooms": 2, "size": "3 bedrooms", "standard_capacity": 6, "a4b_max": 7},
}
EXTRA_BED_CHARGE = 70.0  # A5, per week (A7)
MONTHLY_FACTOR = 4.334   # A8
MIN_GROUP, MAX_GROUP = 2, 7  # P3
# T6 evidence: the rule set a review or brief was made under. Bump whenever a hard rule, a scoring component or the assessment
# packet changes; every stored assessment carries it and a group is only "ready" while it matches (ai_queue.is_ready).
RULE_VERSION = "2026-11-23.sex-culture-age-prefs"   # 10 Nov: 3.7 first-choice hit = exact arrangement label (prefix bug), candidate members in canonical id order before allocation (same member set -> same allocation/score); weights unchanged. Earlier: A6(b) literal: Jan/Feb start + July end = 6-month rate in any year (same-year code restriction removed)
DEFAULT_MIN_LEASE_MONTHS = 2  # A9(a): the minimum lease is generally 2 months; A15(d): 2 months on every apartment today; staff can change it per apartment (A9(b))
# Budget (A16, 8 Nov 2026): the ONLY budget test is the whole household's - actual whole-apartment weekly rent including the A5
# extra-bed charge against the SUM of every member's stated weekly maximum, +10% (studio/1BR/2-bedroom) or +20% (3-bedroom), via
# budget.check_total_budget. There is no per-person ceiling: a share above someone's own maximum is a discussion point for the
# group, not a rule failure. The former I14(d) per-share +10% test (BUDGET_TOLERANCE / within_budget) is withdrawn.
A1_INVENTORY = 103            # A1: Melbourne inventory at launch


def normalise_type(code: str) -> str:
    c = (code or "").strip().upper().replace(" ", "")
    if c in ("STUDIO",):
        return "Studio"
    if c in ("1BR", "1B", "1BED", "1BEDROOM"):
        return "1BR"
    if c in ("2B1B",):
        return "2B1B"
    if c in ("2B2B",):
        return "2B2B"
    if c in ("3B", "3B2B", "3BR"):
        return "3B"
    raise ValueError(f"Unknown apartment type code: {code!r}. Use Studio, 1BR, 2B1B, 2B2B or 3B")


def monthly(weekly: float) -> float:
    """A8: monthly rate = weekly x 4.334."""
    return round(weekly * MONTHLY_FACTOR, 2)


# ---------------------------------------------------------------------------
# A4(d) bedding values. A bed sleeps: king/queen/double = 1 person or a couple;
# single = 1; bunk = 2 (M3: both must have said yes to a bunk).
# ---------------------------------------------------------------------------
BED_SLEEPS = {"king": 2, "queen": 2, "double": 2, "single": 1, "bunk": 2}
COUPLE_BEDS = {"king", "queen", "double"}


def parse_bedding(value: str) -> list[list[str]]:
    """'1 king or 2 singles or 1 king+1 single' -> [['king'], ['single','single'], ['king','single']]."""
    if value is None:
        return []
    text = str(value).strip().lower()
    if not text:
        return []
    setups: list[list[str]] = []
    for alt in re.split(r"\s+or\s+", text):
        beds: list[str] = []
        for part in alt.split("+"):
            part = part.strip()
            m = re.match(r"(\d+)\s*(king|queen|double|single|bunk)", part)
            if not m:
                raise ValueError(f"Unrecognised bedding value: {value!r}. Use setups like \"1 king or 2 singles\" or \"1 single or 1 bunk bed\"")
            n, bed = int(m.group(1)), m.group(2)
            beds.extend([bed] * n)
        setups.append(beds)
    return setups


def setup_capacity(setup: list[str]) -> int:
    return sum(BED_SLEEPS[b] for b in setup)


def room_capacity(setups: list[list[str]]) -> tuple[int, int]:
    """(min, max) people a room can take: min is one person; max is the biggest setup."""
    if not setups:
        return (0, 0)
    return (1, max(setup_capacity(s) for s in setups))


def apartment_capacity(bedrooms: list[list[list[str]]]) -> tuple[int, int]:
    """Sum of per-room capacities. Each bedroom column is a list of setups (A2(g), A2a)."""
    rooms = [room_capacity(r) for r in bedrooms if r]
    return (sum(r[0] for r in rooms), sum(r[1] for r in rooms))


def capacity_fits_a4b(type_code: str, max_capacity: int) -> bool:
    """A4(b): studio/1BR up to 3; 2-bedroom up to 5; 3-bedroom up to 7."""
    return max_capacity <= APT_TYPES[normalise_type(type_code)]["a4b_max"]


def extra_bed_charge(type_code: str, people: int) -> float:
    """A5: $70/week when occupied beyond standard capacity (3rd in studio/1BR, 5th in 2B, 7th in 3B)."""
    std = APT_TYPES[normalise_type(type_code)]["standard_capacity"]
    return EXTRA_BED_CHARGE if people > std else 0.0


# ---------------------------------------------------------------------------
# Dates: A6 rates, A9 minimum lease, A10 exit windows
# ---------------------------------------------------------------------------
FLEX_DAYS = {"Exact date": 0, "± 1 week": 7, "±1 week": 7, "± 2 weeks": 14, "±2 weeks": 14, "± 1 month": 30, "±1 month": 30}
OPEN_TO_EXTENDING_DAYS = 365


def flex_days(value: str) -> int:
    return FLEX_DAYS.get((value or "").strip(), 0)


def rate_kind(start: date, end: date) -> str:
    """A6(b), literal register wording (parity patch 8 Nov 2026): the 6-month rate applies to every lease that starts in January
    or February and ends in July - whichever July that is (a January 2027 start ending July 2028 is a 6-month-rate lease). The
    former same-year condition was code only, never a ruling; the label '6m' names the tariff, not a duration limit."""
    if start.month in (1, 2) and end.month == 7:
        return "6m"
    return "12m"


# A10 (8 Nov 2026 reconciliation): the former start-season exit windows, quiet-period "any term" rule and the 6-9 December
# availability shift are withdrawn. Normal move-outs are the SAME three windows every year for every start (A10(b)):
# 10 January-20 February, 1-20 July, 5 October-7 December, inclusive; every other day is a blackout (A10(c)) that needs a
# staff-approved exception for that specific booking (A10(e)) - never granted silently here. Windows live in lease_dates.EXIT_WINDOWS.
def exit_ok(start: date, end: date, min_lease_months: float = DEFAULT_MIN_LEASE_MONTHS) -> tuple[bool, str]:
    """A9 minimum lease (calendar months, Bill 8 November 2026: same day N months on, or the last day of that month; a fractional
    minimum is held, never rounded) + A10(b) exit windows / A10(c) blackout. Pure: no clock, no vacancy (see check_group)."""
    from . import lease_dates   # local import: lease_dates imports rules
    if end <= start:
        return False, "Move-out must be after move-in"
    try:
        earliest = lease_dates.min_lease_end(start, min_lease_months)
    except ValueError as e:   # unusable stored minimum (0, negative, NaN, beyond the bound) or a start too late for the calendar: a reason, not a crash
        return False, f"The apartment's minimum lease setting cannot be applied ({e}); staff must correct it (A9)"
    if earliest is None:
        return False, f"The apartment's minimum lease of {min_lease_months:g} months is not a whole number of months; staff must confirm it (A9)"
    if end < earliest:
        return False, f"Lease shorter than the apartment's minimum of {min_lease_months:g} months (earliest move-out {earliest.isoformat()}, A9)"
    label = lease_dates.exit_window(end)
    if label is None:
        return False, f"Move-out {end.isoformat()} is in a blackout period (A10(c)); only a staff-approved exception for this specific booking could allow it (A10(e))"
    return True, label


# I8 / I11(d), 8 November 2026: dates of birth are no longer collected, so there is no age arithmetic anywhere in MMAI.
# Adult eligibility (18 or older at move-in, every occupant) comes ONLY from the enquirer's explicit declaration, carried on the
# Unit as `adults_confirmed_move_in` (the move-in date it was made for) and checked in check_group().
ADULTS_UNCONFIRMED = "Adult declaration missing for {name}'s party: the enquirer must confirm everyone will be 18 or older on the move-in date (I11(d)); staff to confirm"
ADULTS_RENEW = "{name}'s adult declaration was made for a move-in on {declared}; the proposed move-in {start} is earlier, so it needs a renewed declaration (I11(d))"


# ---------------------------------------------------------------------------
# I14 price guide (Bill's figures, per person per week). Keys are stable option ids.
# ---------------------------------------------------------------------------
PRICE_GUIDE = {
    "12m": [
        ("Studio", "Studio", "single", "Single occupancy", 599, 695),
        ("Studio", "Studio", "twin", "Twin occupancy", 299, 348),
        ("1BR", "1 bedroom", "single", "Single occupancy", 675, 785),
        ("1BR", "1 bedroom", "twin", "Twin occupancy", 338, 393),
        ("2B1B", "2 bedroom / 1 bathroom", "single", "Single occupancy bedroom", 399, 525),
        ("2B1B", "2 bedroom / 1 bathroom", "twin", "Twin share bedroom", 200, 263),
        ("2B1B", "2 bedroom / 1 bathroom", "triple", "Triple share bedroom", 157, 198),  # I14(b), per-room method (A12(e)), 26 Oct 2026
        ("2B2B", "2 bedroom / 2 bathroom", "single", "Single occupancy bedroom", 513, 575),
        ("2B2B", "2 bedroom / 2 bathroom", "twin", "Twin share bedroom", 257, 288),
        ("3B", "3 bedroom / 2 bathroom", "single", "Single occupancy bedroom", 432, 483),
        ("3B", "3 bedroom / 2 bathroom", "twin", "Twin share bedroom", 216, 242),
    ],
    "6m": [
        ("Studio", "Studio", "single", "Single occupancy", 649, 745),
        ("Studio", "Studio", "twin", "Twin occupancy", 325, 373),
        ("1BR", "1 bedroom", "single", "Single occupancy", 725, 835),
        ("1BR", "1 bedroom", "twin", "Twin occupancy", 363, 418),
        ("2B1B", "2 bedroom / 1 bathroom", "single", "Single occupancy bedroom", 437, 563),
        ("2B1B", "2 bedroom / 1 bathroom", "twin", "Twin share bedroom", 219, 282),
        ("2B1B", "2 bedroom / 1 bathroom", "triple", "Triple share bedroom", 169, 211),  # I14(c), per-room method (A12(e)), 26 Oct 2026
        ("2B2B", "2 bedroom / 2 bathroom", "single", "Single occupancy bedroom", 551, 613),
        ("2B2B", "2 bedroom / 2 bathroom", "twin", "Twin share bedroom", 276, 307),
        ("3B", "3 bedroom / 2 bathroom", "single", "Single occupancy bedroom", 465, 517),
        ("3B", "3 bedroom / 2 bathroom", "twin", "Twin share bedroom", 233, 259),
    ],
}


def price_guide(kind: str) -> list[dict]:
    out = []
    for code, type_label, occ, occ_label, lo, hi in PRICE_GUIDE[kind]:
        out.append({
            "id": f"{code}-{occ}", "type_code": code, "type_label": type_label, "occupancy": occ,
            "occupancy_label": occ_label, "min": lo, "max": hi,
            "label": f"{type_label}: {occ_label.lower()}, ${lo} to ${hi}",
        })
    return out


def guide_kind_for(move_in: Optional[date], move_out: Optional[date]) -> str:
    """I14(c)/A6(b): show the 6-month guide when move-in is Jan/Feb and move-out is July."""
    if move_in and move_out:
        return rate_kind(move_in, move_out)
    return "12m"


def budget_max_for(option_id: str, kind: str) -> Optional[float]:
    """I14(d): the option chosen is the MOST the person can pay (top of the range)."""
    for o in price_guide(kind):
        if o["id"] == option_id:
            return float(o["max"])
    return None


def _lossless_decimal_text(d) -> str:
    """Plain text of an exact Decimal with no rounding and no dependence on the caller's decimal context: str() of a Decimal is
    context-free, and trailing zeros are stripped textually (normalize() would round to the ambient precision, e.g. 1149.995 →
    1.1E+3 under a prec-2 context)."""
    t = format(d, "f")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t or "0"


def household_budget(type_code: str, rent: float, people: list["Person"]) -> tuple[list[str], Optional[dict]]:
    """A16 for a complete candidate group: (reasons, detail). Baseline = sum of EVERY member's stated weekly maximum (no per-person
    ceiling anywhere); actual total = rent + A5 extra-bed charge for len(people); allowance 10% / 20% by type. A member without a
    usable stated maximum makes the household test impossible: the group is held with a reason, never passed or defaulted.
    Exact Decimal arithmetic via budget.py (the approved standalone helper); the detail carries the actual figures so staff and
    the AI reviewer see the household sum, allowance and limit rather than a bare pass."""
    from . import budget   # local import: budget imports rules
    missing = [p.name for p in people if p.budget_max is None]
    if missing:
        return [f"{', '.join(missing)}: no stated weekly maximum recorded, so the household budget cannot be assessed (A16)"], None
    try:
        baseline = budget.matebot_baseline([p.budget_max for p in people])
        res = budget.check_total_budget(rent, type_code, len(people), baseline)
    except (ValueError, ArithmeticError) as e:   # unusable amount (negative, NaN, out of representable range): hold, don't guess
        return [f"A stated weekly maximum or the rent is not a usable amount ({e}); staff must correct it before the household budget can be assessed (A16)"], None
    detail = res.as_dict()   # figures rounded to the cent for display; the classification used the exact values
    detail["limit_exact"] = _lossless_decimal_text(res.limit)   # so a reason never reads "$1150.00 is above limit $1150.00" when the exact limit is 1149.995
    # r7: the same exact figures the classification used, lossless, for citable evidence (display fields above stay cent-rounded)
    detail["total_weekly_rent_exact"] = _lossless_decimal_text(res.total_weekly_rent)
    detail["whole_apartment_max_exact"] = _lossless_decimal_text(res.whole_apartment_max)
    detail["allowance_fraction"] = _lossless_decimal_text(res.allowance_pct)   # raw fraction: "0.1" means ten percent
    detail["member_weekly_maximums"] = {p.id: str(budget.cents(budget.money(p.budget_max, positive=True))) for p in people}
    detail["basis"] = "A16: whole-apartment rent incl. extra bed vs the sum of every member's stated weekly maximum; no per-person ceiling"
    if res.passed:
        return [], detail
    pct = {"0.10": 10, "0.20": 20}.get(str(res.allowance_pct)) or _lossless_decimal_text(res.allowance_pct)   # no ambient-context arithmetic in the message
    return [f"Whole-apartment rent ${detail['total_weekly_rent']}/wk (incl. ${detail['extra_bed_charge']} extra bed) is more than {pct}% above the "
            f"household's combined stated maximum of ${detail['whole_apartment_max']}/wk (exact limit ${detail['limit_exact']}, A16)"], detail


def per_room_shares(type_code: str, rent: float, rooms: list[dict], extra: float) -> dict[int, float]:
    """A12(d)/(e): room cost = rent / bedrooms; the $70 extra-bed charge (A5) is added to the room holding the
    extra bed (the room with the most occupants). Returns {room number: room cost}."""
    code = normalise_type(type_code)
    n_rooms = APT_TYPES[code]["bedrooms"]
    base = rent / n_rooms
    costs = {r["room"]: base for r in rooms}
    if extra and rooms:
        holder = max(rooms, key=lambda r: (len(r["occupants"]), -r["room"]))
        costs[holder["room"]] = base + extra
    return {k: round(v, 2) for k, v in costs.items()}


def liability_note(total_weekly: float) -> str:
    """B5: shown on every group proposal and introduction."""
    return (f"One whole lease: the group leases the entire apartment for ${total_weekly:,.0f} per week, and everyone named on the lease "
            f"is jointly and severally liable for the full weekly rent. The shares shown are a guide only; how the group divides the rent is its own arrangement.")


def compute_price_guide_from_list(apartments: Iterable[dict]) -> dict:
    """Derive Bill's I14 figures from the live list for comparison (not used for the form)."""
    res: dict[str, dict] = {}
    for a in apartments:
        code = normalise_type(a["type_code"])
        rooms = APT_TYPES[code]["bedrooms"]
        for kind, key in (("12m", "rent_12m"), ("6m", "rent_6m")):
            rent = a.get(key)
            if rent is None:
                continue
            single = rent / rooms
            twin = rent / rooms / 2
            # I14(b)/(c) triple-share figures use the per-room method (A12(e)): half the rent plus the $70 extra bed (A5), split three ways
            triple = (rent / rooms + EXTRA_BED_CHARGE) / 3 if code == "2B1B" else None
            for occ, val in (("single", single), ("twin", twin), ("triple", triple)):
                if val is None:
                    continue
                k = f"{kind}:{code}-{occ}"
                r = res.setdefault(k, {"min": val, "max": val})
                r["min"], r["max"] = min(r["min"], val), max(r["max"], val)
    return res


# ---------------------------------------------------------------------------
# Group model used by the hard filters
# ---------------------------------------------------------------------------
@dataclass
class Person:
    """One occupant. Party members (couple partner, friends) inherit the lead's answers (see report)."""
    id: str
    unit_id: str
    name: str
    birth_country: Optional[str]
    budget_max: Optional[float]
    arrangements: set
    bunk_ok: bool
    smokes: bool
    smoke_ok: bool
    is_lead: bool = True
    is_couple_member: bool = False
    # I16(f): which couple this person belongs to. Only the enquirer can create a couple: 3.8 "Couple sharing one bed"
    # (couple_id = the unit id) or an explicit 3.10 answer for a "With friends" party (couple_id = "<unit>c1", "<unit>c2", ...,
    # so one party may hold more than one couple). MMAI never infers a couple from party size or from 3.5 ticks alone.
    couple_id: Optional[str] = None
    # Identity provenance captured from the row this person was built from (GP13 uid stamping, matching._stamp_people): the
    # name as the identity module keys it (enquirer: first + last name; party member: the row's name) and the row's email.
    # Never a matching factor; never written to an allocation.
    ident_name: Optional[str] = None
    ident_email: Optional[str] = None
    # The server-issued person_uid that was ACTIVE for this very row when the unit was built (matching._attach_identities):
    # the identity the allocation is computed for. Stamped onto the occupant only if that same uid is still active and bound
    # to the row at the guarded write; a later or replacement identity is never substituted. None when not resolvable.
    ident_uid: Optional[str] = None
    # 23 Nov 2026 matching inputs (Bill: sex preference hard filter; age / culture soft preferences)
    own_sexes: tuple = ()                  # selected sex options (multi-select, from intro_sex); only Female / Male are ever compared
    sex_pref: Optional[str] = None         # "Same sex only" | "Mixed is fine" | "No preference"
    age_pref: tuple = ()                   # preferred housemate age bands (18–21, 22–25, 26–30, 31+, No preference)
    own_age: Optional[int] = None          # current age as stated (intro_age)
    languages: tuple = ()
    birth_region: Optional[str] = None     # derived from birth_country via countries.region_of
    live_country: Optional[str] = None
    culture_interest: Optional[str] = None
    culture_languages: tuple = ()
    culture_regions: tuple = ()
    culture_countries: tuple = ()

    def __post_init__(self):
        if self.couple_id is None and self.is_couple_member:
            self.couple_id = self.unit_id
        elif self.couple_id is not None:
            self.is_couple_member = True


@dataclass
class Unit:
    """An enquiry = one atomic unit (3.8/3.9: couples and friend groups stay together)."""
    id: str
    kind: str  # 'single' | 'couple' | 'friends'
    people: list
    city: str
    apt_sizes: set
    mate_max: int
    move_in: date
    move_in_flex: int
    move_out: date
    move_out_flex: int
    open_to_extending: bool = False
    extra: dict = field(default_factory=dict)
    # I11(d): the move-in date the enquirer's "everyone will be 18+ at move-in" declaration was made for; None = no valid
    # declaration (never made, or the party changed since). Set only from the stored declaration, never from any date of birth.
    adults_confirmed_move_in: Optional[date] = None
    # GP14 (MateBot): apartments / buildings this enquiry has declined (apartment_rejections, active). An apartment the enquiry
    # excludes is simply not an eligible option for any group containing the enquiry (matching._unit_fits_apartment). Immutable here.
    # GP14: the enquiry's ACTIVE apartment/building rejections, one entry per record: (scope, key, allowed) with scope
    # 'apartment' (key = str apartment id) or 'building' (key = homebot_match.building_key(address): the street address without
    # the unit number, as HomeBot stores it) and `allowed` = the apartment ids a material-change re-offer permission under THAT
    # record lifted the exclusion for. A permission never reaches another record (see matching.unit_excludes_apartment).
    apartment_exclusions: tuple = ()

    @property
    def size(self) -> int:
        return len(self.people)

    def move_in_window(self) -> tuple[date, date]:
        return (self.move_in - timedelta(days=self.move_in_flex), self.move_in + timedelta(days=self.move_in_flex))

    def move_out_window(self) -> tuple[date, date]:
        hi = self.move_out + timedelta(days=OPEN_TO_EXTENDING_DAYS if self.open_to_extending else self.move_out_flex)
        return (self.move_out - timedelta(days=self.move_out_flex), hi)

    def date_problem(self) -> Optional[str]:
        """A stored date that cannot be worked with (missing, wrong type, or so near the calendar's limits that its flexibility
        window overflows): the reason text, or None when the dates are usable. Callers exclude the enquiry with this reason
        rather than crashing a whole run on one bad row; nothing is rounded or defaulted."""
        for name, v in (("move_in", self.move_in), ("move_out", self.move_out)):
            if isinstance(v, datetime) or not isinstance(v, date):
                return f"{name} is not a usable date ({type(v).__name__}); staff must correct the enquiry's dates"
        try:
            self.move_in_window()
            self.move_out_window()
        except OverflowError:
            return (f"move-in {self.move_in.isoformat()} / move-out {self.move_out.isoformat()} with the stated flexibility fall outside the "
                    f"supported calendar; staff must correct the enquiry's dates")
        return None


@dataclass
class Apartment:
    id: str
    address: str
    type_code: str
    rent_12m: float
    rent_6m: float
    available_from: Optional[date]
    bedrooms: list  # list of setups per bedroom
    min_lease_months: float = DEFAULT_MIN_LEASE_MONTHS
    bills_included: bool = False
    city: str = "Melbourne"
    suburb: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    building_key: str = ""   # GP14 building scope: homebot_match.building_key(address) - street address without the unit; '' never matches


# ---------------------------------------------------------------------------
# Pairwise checks (both directions: the caller runs every ordered pair)
# ---------------------------------------------------------------------------
def pair_conflict(a: Person, b: Person) -> Optional[str]:
    """Return a reason if `a` cannot live with `b` (checked from a's point of view).
    Hard pairwise rules: smoking (5.5/5.6) and the housemate sex preference "Same sex only" (Bill, 17 and 23 November 2026).
    Same sex only compares Female / Male only; with several selections one shared Female / Male option is enough; a person
    with no Female / Male selection (Non-binary, a different term, or no answer) never triggers it. Origin, language, culture
    and age are scored (matching.preference_score), never filtered here."""
    if a.sex_pref == "Same sex only":
        mine = {x for x in a.own_sexes if x in ("Female", "Male")}
        theirs = {x for x in b.own_sexes if x in ("Female", "Male")}
        if mine and theirs and not (mine & theirs):
            return f"{a.name} prefers to live with the same sex only"
    # 5.5 / 5.6 smoking (B3: outside only is the only smoking allowed)
    if b.smokes and not a.smoke_ok:
        return f"{a.name} will not live with someone who smokes or vapes outside (5.6)"
    return None


# ---------------------------------------------------------------------------
# Group-level hard filters
# ---------------------------------------------------------------------------
@dataclass
class GroupCheck:
    ok: bool
    reasons: list
    lease_start: Optional[date] = None
    lease_end: Optional[date] = None
    rate_kind: Optional[str] = None
    exit_window: Optional[str] = None
    weekly_rent: Optional[float] = None
    extra_bed: float = 0.0
    total_weekly: Optional[float] = None
    allocation: Optional[list] = None  # per room: {room, setup, occupants:[{person_id, share, arrangement}]}
    shares: Optional[dict] = None       # person_id -> weekly share (A12 proposal for the group's agreement; not a per-person test)
    household_budget: Optional[dict] = None  # A16 figures: total_weekly_rent, whole_apartment_max (sum of maxima), allowance_pct, limit, extra_bed_charge, member_weekly_maximums
    lease_choice: Optional[dict] = None      # A6/A9(c) fact when the offered dates are not the earliest start + latest exit: which tariff-boundary candidate was chosen and why
    exception_ref: Optional[str] = None
    exception_total: Optional[str] = None   # canonical whole-apartment total, never a base tariff


def _date_intersection(windows: list[tuple[date, date]]) -> Optional[tuple[date, date]]:
    lo = max(w[0] for w in windows)
    hi = min(w[1] for w in windows)
    return (lo, hi) if lo <= hi else None


def _lease_bounds(apt: Apartment, units: list[Unit]) -> tuple[Optional[tuple[date, date]], Optional[tuple[date, date]], str]:
    """The accepted start window (every member's move-in window ∩ availability ∩ every party's adult declaration, I11(d)) and the
    accepted move-out window (every member's). Windows are never widened. Returns (start window, move-out window, error)."""
    if apt.available_from is None:
        return None, None, "Apartment has no availability date yet"
    win_in = _date_intersection([u.move_in_window() for u in units] + [(apt.available_from, date.max)])
    if not win_in:
        return None, None, "Move-in windows do not overlap with each other and the apartment's availability (2.2, 2.3, A2(e))"
    # I11(d): each party's adult declaration was made for a stated move-in date, so that date is a lower bound on the start
    # MMAI may propose for them: the start is the earliest date inside every accepted window AND on or after every party's
    # declared date. The windows themselves are never widened; a party without a valid declaration gives no bound here (it is
    # held by check_group's mandatory adult check instead). Nothing about age is inferred before a declared date.
    declared = [u.adults_confirmed_move_in for u in units if u.adults_confirmed_move_in is not None]
    verified = _date_intersection([win_in] + [(dd, date.max) for dd in declared])
    if not verified:
        return None, None, (f"No move-in date inside everyone's accepted windows ({win_in[0].isoformat()} to {win_in[1].isoformat()}) is covered by every party's "
                            f"adult declaration (latest declared move-in {max(declared).isoformat()}); a renewed declaration for an earlier date is needed (I11(d))")
    win_out = _date_intersection([u.move_out_window() for u in units])
    if not win_out:
        return verified, None, "Move-out windows do not overlap (2.4, 2.5)"
    return verified, win_out, ""


def choose_lease_dates(apt: Apartment, units: list[Unit]) -> tuple[Optional[date], Optional[date], str, str]:
    """The BASELINE offer: the earliest accepted start and the latest accepted exit that passes A9/A10 for it (A9(c): longest term).
    Pure (no clock, no vacancy, no budget). check_group uses it as the reference the A6 tariff search is compared with and as the
    candidate whose reasons are reported when no offer is feasible."""
    win_in, win_out, err = _lease_bounds(apt, units)
    if err:
        return (win_in[0] if win_in else None), None, "", err
    start = win_in[0]  # earliest date everyone can move in once the apartment is free, within what each party has declared for
    lo, hi = win_out
    # scan from the latest acceptable date downwards (prefer the longest lease, A9(c)); counted by offset from `hi` so the scan
    # can never step below `lo` (or the calendar's own lower bound) and overflow on a degenerate window
    for k in range((hi - lo).days + 1):
        d = hi - timedelta(days=k)
        ok, label = exit_ok(start, d, apt.min_lease_months)
        if ok:
            return start, d, label, ""
    ok, label = exit_ok(start, hi, apt.min_lease_months)
    return start, None, "", label


def _tariff_starts(lo: date, hi: date) -> list[date]:
    """A6 start candidates inside [lo, hi]: the earliest accepted start plus every tariff boundary inside the window (1 January:
    the start enters the January/February season; 1 March: it leaves it). Within one tariff an earlier start only lengthens
    the stay and eases A9/A17 at the same rent, so no other start can beat these (proved against the exhaustive oracle in tests)."""
    out = [lo]
    for y in range(lo.year, hi.year + 2):
        for m in (1, 3):
            try:
                b = date(y, m, 1)
            except ValueError:   # pragma: no cover - calendar bound
                continue
            if lo < b <= hi:
                out.append(b)
    return sorted(set(out))


def _latest_exit(out_lo: date, out_hi: date, start: date, kind: str) -> Optional[date]:
    """The latest date in the accepted move-out window that lies in a normal A10(b) exit window and that rate_kind() classes as
    `kind` with `start` (the tariff test itself lives only in rate_kind; nothing is assumed here about which July or which start
    months qualify). Blackout days (A10(c)) are never candidates; A10(e) staff exceptions are a separate, unwired record and are
    not consulted here."""
    from . import lease_dates   # local import: lease_dates imports rules
    best = None
    for y in range(out_lo.year, out_hi.year + 1):
        for m1, d1, m2, d2, _label in lease_dates.EXIT_WINDOWS:
            seg_lo, seg_hi = max(out_lo, date(y, m1, d1)), min(out_hi, date(y, m2, d2))
            if seg_lo > seg_hi:
                continue
            if rate_kind(start, seg_hi) != kind:   # the whole segment lies in one month, so one probe decides the tariff for all of it
                continue
            if best is None or seg_hi > best:
                best = seg_hi
    return best


def lease_offer_candidates(apt: Apartment, units: list[Unit], today: date, people: list[Person], *,
                           exception_offers=()) -> tuple[list[dict], str]:
    """A6 + A9(c) + A17 + A16 together: every tariff-boundary offer (start candidate × tariff → its latest legal exit), each
    assessed with the SAME checks a concrete offer gets (A9/A10 via exit_ok, A17 vacancy from `today`, A16 household budget at
    that tariff's rent). Returns (candidates, error); each candidate carries start, end, kind, label, rent, stay_days, feasible,
    reasons. No window is widened or capped; the rules are not changed, only which concrete offer inside the accepted windows is
    put forward. A6 price direction is not assumed (a 6-month rate may be dearer or cheaper than the 12-month rate)."""
    from . import lease_dates   # local import: lease_dates imports rules
    win_in, win_out, err = _lease_bounds(apt, units)
    if err:
        return [], err
    code = normalise_type(apt.type_code)
    out = []
    for start in _tariff_starts(*win_in):
        for kind in ("6m", "12m"):
            end = _latest_exit(win_out[0], win_out[1], start, kind)
            if end is None:
                continue
            reasons = []
            ok, label = exit_ok(start, end, apt.min_lease_months)
            if not ok:
                reasons.append(label)
                label = ""
            vac = lease_dates.check_vacancy(today, apt.available_from, start, end)
            if not vac.passed:
                reasons.append(f"{vac.detail} (A17, counted from {max(today, apt.available_from).isoformat()})")
            rent = apt.rent_6m if kind == "6m" else apt.rent_12m
            budget_reasons, detail = household_budget(code, rent, people)
            out.append({"start": start, "end": end, "kind": kind, "label": label, "rent": rent, "stay_days": (end - start).days,
                        "feasible": not reasons and not budget_reasons, "reasons": reasons + budget_reasons,
                        "date_reasons": reasons, "budget_reasons": budget_reasons, "budget": detail})
    # A10: staff supplies EACH exact booking. Never scan/invent blackout dates, and
    # never use exceptions only as a fallback after ordinary candidates fail.
    for exc in exception_offers:
        start, end = exc.move_in, exc.move_out
        if not (win_in[0] <= start <= win_in[1] and win_out[0] <= end <= win_out[1]):
            continue
        from . import budget, exception_rent
        reasons = []
        minimum = lease_dates.check_min_lease(start, end, apt.min_lease_months)
        if not minimum.passed:
            reasons.append(minimum.detail)
        vacancy = lease_dates.check_vacancy(today, apt.available_from, start, end)
        if not vacancy.passed:
            reasons.append(vacancy.detail)
        kind = rate_kind(start, end)
        rent = apt.rent_6m if kind == "6m" else apt.rent_12m
        budget_reasons, detail = [], None
        try:
            maximum = budget.matebot_baseline([p.budget_max for p in people])
            res = exception_rent.check_exception_rent(
                exc.exception_ref, code, len(people), maximum, end, rent, exc.exception_total,
                staff_exception=exc.staff_exception, customer_agreement=exc.customer_agreement)
            if not res.passed:
                budget_reasons.append(res.detail)
            # Same household evidence shape, but the already-complete exception
            # total is assessed once and is not fed back through the normal A5 path.
            _, detail = household_budget(code, rent, people)
            if detail is not None:
                detail.update(
                    passed=res.passed, reason_code=res.reason_code,
                    total_weekly_rent=str(budget.cents(exc.exception_total)),
                    total_weekly_rent_exact=_lossless_decimal_text(exc.exception_total),
                    exception_ref=exc.exception_ref,
                    basis="Staff-authorised exact exit and total; customer agreement required above the normal allowance",
                )
        except (ValueError, ArithmeticError, TypeError) as error:
            budget_reasons.append(f"The exception rent or household maximum is not usable ({type(error).__name__})")
        out.append({
            "start": start, "end": end, "kind": kind, "label": "Staff-authorised exact exit",
            "rent": rent, "stay_days": (end - start).days,
            "exception_ref": exc.exception_ref, "exception_total": _lossless_decimal_text(exc.exception_total),
            "feasible": not reasons and not budget_reasons, "reasons": reasons + budget_reasons,
            "date_reasons": reasons, "budget_reasons": budget_reasons, "budget": detail,
        })
    return out, ""


def choose_lease_offer(apt: Apartment, units: list[Unit], today: date, people: list[Person], *, exception_offers=()) -> dict:
    """The offer check_group puts forward: the LONGEST feasible candidate from lease_offer_candidates (A9(c)); ties go to the
    earliest start. When no candidate is feasible the baseline (choose_lease_dates: earliest start, latest exit) is returned with
    `feasible` False so its reasons are reported, exactly as before the A6 search existed. `lease_choice` is a fact for the record
    (and the AI packet's rule facts) whenever the chosen offer is not the baseline."""
    cands, err = lease_offer_candidates(apt, units, today, people, exception_offers=exception_offers)
    b_start, b_end, b_label, b_err = choose_lease_dates(apt, units)
    if err or (b_err and not cands):
        return {"feasible": False, "start": b_start, "end": b_end, "kind": None, "label": "", "rent": None, "reasons": [err or b_err],
                "date_reasons": [err or b_err], "budget_reasons": [], "budget": None, "lease_choice": None}
    feasible = [c for c in cands if c["feasible"]]
    if feasible:
        best = max(feasible, key=lambda c: (c["stay_days"], -c["start"].toordinal()))
        choice = None
        if (best["start"], best["end"]) != (b_start, b_end):
            choice = {"basis": "A6/A9(c): longest offer passing every rule; the earliest start + latest exit was not feasible at its tariff or is shorter",
                      "earliest_start": b_start.isoformat() if b_start else None, "baseline_end": b_end.isoformat() if b_end else None,
                      "chosen_start": best["start"].isoformat(), "chosen_end": best["end"].isoformat(), "rate_kind": best["kind"],
                      "candidates_considered": len(cands), "candidates_feasible": len(feasible)}
        return {**best, "lease_choice": choice}
    # nothing feasible: report the baseline candidate's reasons (the one staff and the earlier engine would look at)
    base = next((c for c in cands if (c["start"], c["end"]) == (b_start, b_end)), None)
    if base is None:
        if b_err:
            return {"feasible": False, "start": b_start, "end": b_end, "kind": None, "label": "", "rent": None, "reasons": [b_err],
                    "date_reasons": [b_err], "budget_reasons": [], "budget": None, "lease_choice": None}
        base = cands[0] if cands else {"feasible": False, "start": b_start, "end": b_end, "kind": None, "label": "", "rent": None, "budget": None,
                                       "reasons": ["No exit date inside the accepted move-out window lies in a permitted exit window (A10)"],
                                       "date_reasons": ["No exit date inside the accepted move-out window lies in a permitted exit window (A10)"], "budget_reasons": []}
    return {**base, "feasible": False, "lease_choice": None}


def _arrangement_for(room_people: list[Person], p: Person) -> set:
    """Arrangement labels a person must have ticked (3.5) for the room they are placed in."""
    others = [q for q in room_people if q.id != p.id]
    need = set()
    if p.is_couple_member:
        need.add("Couple sharing one bed")
        non_partner = [q for q in others if q.unit_id != p.unit_id]
        if len(non_partner) == 1:
            need.add("Share with one other")
        elif len(non_partner) >= 2:
            need.add("Share with two others")
    else:
        if not others:
            need.add("Own room")
        elif len(others) == 1:
            need.add("Share with one other")
        else:
            need.add("Share with two others")
    return need


def _room_ok_a4e(room_people: list[Person], n_rooms: int) -> bool:
    """A4(e): if a couple is in the room, everyone else in that room must be from the same party (same enquiry), and the
    apartment must have other bedrooms (the party is seeking flatmates for them). A couple alone in a room is always fine."""
    couple_ids = {p.couple_id for p in room_people if p.is_couple_member}
    if not couple_ids:
        return True
    if len(couple_ids) > 1:
        return False  # two couples never share one bedroom (I16(f): each couple is its own pair)
    couple_unit = next(p.unit_id for p in room_people if p.is_couple_member)
    others = [p for p in room_people if not p.is_couple_member]
    if not others:
        return True
    if any(p.unit_id != couple_unit for p in others):
        return False  # an unrelated individual never shares a bedroom with a couple
    return n_rooms >= 2  # the party is seeking flatmates for the other bedrooms


def _unit_sort_key(unit_id) -> tuple:
    """Stable identity order for enquiries: numeric ids ascending, any non-numeric id after them by text."""
    t = str(unit_id)
    return (0, int(t), "") if t.isdigit() else (1, 0, t)


def canonical_people_order(people: list[Person]) -> list[Person]:
    """The people of a group in canonical order: by enquiry (unit) id, keeping each enquiry's own people in the order given.
    Precondition relied on: every caller lists an enquiry's people lead first, then its party members in form order (party_members
    rows by id) - matching.unit_from_row is the one constructor of Unit.people and every unit-building query orders by id. An
    arbitrary permutation WITHIN one enquiry is out of scope here (it does not occur). A stable sort, so an already-canonical list
    is unchanged."""
    return sorted(people, key=lambda p: _unit_sort_key(p.unit_id))


def allocate_beds(apt: Apartment, people: list[Person], rent: Optional[float] = None, extra: float = 0.0) -> Optional[tuple[list, str]]:
    """Find a bed allocation honouring A4(d), M3 (bunks), couples in one king/queen/double, 3.5 arrangements.
    A4(e): a "1 king+1 single" room may hold a couple in the king plus one other person (for example a sibling) in the single
    ONLY when all three belong to the same party (one enquiry) and the apartment has other bedrooms for flatmates; a couple is
    never put in one bedroom with an unrelated individual enquirer (see `_room_ok_a4e`).
    Every bedroom must be occupied (GP5: one whole lease, B5). Budget is NOT an allocation criterion: A16 tests the whole
    household's total only (check_group), so no allocation is rejected because of one person's share. `rent`/`extra` are
    accepted for call compatibility and unused.
    Determinism (T3(d), 10 Nov 2026 - engineering repair, no rule change): the search returns the first feasible assignment it
    meets, so its result depended on the ORDER the people were listed in. The same members at the same apartment must always get
    the same allocation (otherwise an unchanged group's evidence hash moved between runs and it was sent back for a needless
    review), so the people are first put in a canonical order - by enquiry id, each enquiry's own people in their stable form
    order (lead first) - by `canonical_people_order`. Feasibility, preferences and every rule above are untouched.
    Returns (rooms, note) or None."""
    rooms_setups = [r for r in apt.bedrooms if r]
    code = normalise_type(apt.type_code)
    n_rooms = len(rooms_setups)
    if n_rooms == 0:
        return None
    people = canonical_people_order(people)
    couples: dict[str, list[Person]] = {}
    for p in people:
        if p.is_couple_member:
            couples.setdefault(p.couple_id, []).append(p)  # I16(f): keyed by couple, so a party may hold more than one couple
    if any(len(v) != 2 for v in couples.values()):
        return None  # a couple is exactly two people
    best = None
    for combo in product(*rooms_setups):
        beds = []  # (room_idx, bed_type)
        for ri, setup in enumerate(combo):
            for b in setup:
                beds.append((ri, b))
        if sum(BED_SLEEPS[b] for _, b in beds) < len(people):
            continue
        # units to place: couples as pairs, others individually
        items: list[list[Person]] = []
        seen = set()
        for p in people:
            if p.is_couple_member:
                if p.couple_id not in seen:
                    seen.add(p.couple_id)
                    items.append(couples[p.couple_id])
            else:
                items.append([p])
        occupancy: list[list[Person]] = [[] for _ in beds]

        def room_people(ri):
            return [p for bi2, (r2, _) in enumerate(beds) if r2 == ri for p in occupancy[bi2]]

        found: list = []

        def complete():
            """All items placed: GP5 (every bedroom occupied), A4(e) and 3.5 arrangements must hold, else keep searching."""
            rooms_: list[dict] = [{"room": ri + 1, "setup": list(combo[ri]), "occupants": []} for ri in range(n_rooms)]
            for bi, (ri, bed) in enumerate(beds):
                for p in occupancy[bi]:
                    rooms_[ri]["occupants"].append({"person": p, "bed": bed})
            if any(not r["occupants"] for r in rooms_):
                return False
            for r in rooms_:
                ppl = [o["person"] for o in r["occupants"]]
                if not _room_ok_a4e(ppl, n_rooms):
                    return False
                for o in r["occupants"]:
                    need = _arrangement_for(ppl, o["person"])
                    if not need.issubset(o["person"].arrangements):
                        return False
                    o["arrangement"] = sorted(need)
            found.append(rooms_)
            return True

        def backtrack(i: int):
            if i == len(items):
                return complete()
            item = items[i]
            tried = set()
            for bi, (ri, bed) in enumerate(beds):
                key = (ri, bed, len(occupancy[bi]))
                if key in tried:
                    continue
                tried.add(key)
                if len(item) == 2:  # couple needs an empty king/queen/double
                    if bed in COUPLE_BEDS and not occupancy[bi]:
                        occupancy[bi].extend(item)
                        if _room_ok_a4e(room_people(ri), n_rooms) and backtrack(i + 1):  # A4(e)
                            return True
                        occupancy[bi].clear()
                else:
                    p = item[0]
                    if bed == "bunk":
                        if p.bunk_ok and len(occupancy[bi]) < 2:
                            occupancy[bi].append(p)
                            if _room_ok_a4e(room_people(ri), n_rooms) and backtrack(i + 1):
                                return True
                            occupancy[bi].pop()
                    elif not occupancy[bi]:
                        occupancy[bi].append(p)
                        if _room_ok_a4e(room_people(ri), n_rooms) and backtrack(i + 1):  # A4(e)
                            return True
                        occupancy[bi].pop()
            return False

        if not backtrack(0):
            continue
        rooms = found[0]
        # prefer allocations with the fewest beds used (no wasted beds), then fewer bunks
        score = (len(beds), sum(1 for _, b in beds if b == "bunk"))
        if best is None or score < best[0]:
            best = (score, rooms)
    if best is None:
        return None
    return best[1], "ok"


def linked_parties(units: list) -> dict:
    """GP11: the "Bring a mate" friend parties in a group, keyed by the enquirer whose link was used (root id) → units, root first.
    Only clusters of two or more enquiries count; a lone enquirer whose friends are elsewhere is not a party here."""
    buckets: dict = {}
    for u in units:
        root = (u.extra or {}).get("linked_to_enquiry_id")
        key = str(root) if root else str(u.id)
        buckets.setdefault(key, []).append(u)
    out = {}
    for key, members in buckets.items():
        if len(members) >= 2:
            members.sort(key=lambda u: (0 if str(u.id) == key else 1, str(u.id)))
            out[key] = members
    return out


def parties(units: list) -> list[tuple[str, list]]:
    """GP12: every party in a group that came to ALM together, as (lead name, units): a "Bring a mate" friend party (linked
    enquiries, with any people on their forms) or one enquiry with several people on it (a couple or friends, I16). A lone
    enquirer is not a party."""
    out = []
    seen: set[str] = set()
    for key, members in linked_parties(units).items():
        out.append((members[0].people[0].name, members))
        seen.update(str(u.id) for u in members)
    for u in units:
        if str(u.id) not in seen and len(u.people) >= 2:
            out.append((u.people[0].name, [u]))
    return out


def check_group(apt: Apartment, units: list[Unit], *, today: Optional[date] = None, exception_offers=()) -> GroupCheck:
    """Run every hard filter (T4(b)). Returns a GroupCheck with reasons for any failure. Launch scope (8 November 2026): no
    origin, sex or age-preference filter exists in this path; there are no hooks or parameters for them.
    `today` (A17(a)) is the business date; None = lease_dates.business_today() via the injectable Melbourne clock. It is only
    used for the future-vacancy count, never for anything about the people."""
    from . import lease_dates   # local import: lease_dates imports rules
    reasons: list[str] = []
    if today is None:
        today = lease_dates.business_today()
    elif isinstance(today, datetime) or not isinstance(today, date):
        raise TypeError(f"today must be a datetime.date, got {type(today).__name__}")
    people: list[Person] = [p for u in units for p in u.people]
    n = len(people)
    code = normalise_type(apt.type_code)
    tinfo = APT_TYPES[code]

    # C1 city
    for u in units:
        if u.city != apt.city:
            reasons.append(f"City mismatch: {u.city} vs {apt.city} (C1)")
    # P3 group size
    if n < MIN_GROUP or n > MAX_GROUP:
        reasons.append(f"Group size {n} outside 2 to 7 (P3)")
    # A4 capacity
    _, cap = apartment_capacity(apt.bedrooms)
    if n > cap:
        reasons.append(f"{n} people exceed the apartment's bedding capacity of {cap} (A4)")
    if n > tinfo["a4b_max"]:
        reasons.append(f"{n} people exceed the A4(b) limit of {tinfo['a4b_max']} for a {tinfo['label']}")
    # GP12 / GP11(c) (Bill, 28 Oct 2026): every group needs at least one housemate from outside the enquirer's own party. Any
    # party (people who enquired together on one form, "Bring a mate" friends, or both together) must always be smaller than the
    # beds in the apartment it is matched to, and can never be the whole group.
    for lead, members in parties(units):
        party_n = sum(len(u.people) for u in members)
        if party_n >= cap:
            reasons.append(f"{lead}'s party of {party_n} would fill every bed ({cap}) in this apartment; a party must leave room for at least one more housemate (GP12)")
        elif party_n >= n:
            reasons.append(f"{lead}'s party of {party_n} would be the whole group; MateBot AI must find them at least one housemate (GP12)")
    # 3.2 apartment sizes
    for u in units:
        if tinfo["size"] not in u.apt_sizes:
            reasons.append(f"{u.people[0].name} did not tick {tinfo['size']} (3.2)")
    # 3.4 maximum housemates
    for u in units:
        if n - 1 > u.mate_max:
            reasons.append(f"{u.people[0].name} accepts at most {u.mate_max} housemates (3.4)")
    # pairwise, both directions
    for a in people:
        for b in people:
            if a.id == b.id or a.unit_id == b.unit_id:
                continue
            r = pair_conflict(a, b)
            if r and r not in reasons:
                reasons.append(r)
    # dates
    bad_dates = [f"{u.people[0].name}: {u.date_problem()}" for u in units if u.date_problem()]
    if bad_dates:   # one unusable row excludes this candidate with a reason; it never aborts the run
        reasons.extend(bad_dates)
        return GroupCheck(False, reasons)
    # dates + tariff + vacancy + budget as ONE search over the accepted windows (A6/A9(c)/A17/A16): the longest offer that passes
    # every rule is put forward; when none does, the baseline offer's reasons are reported. Exceeding A17 excludes THIS candidate
    # apartment only (A17(c)/(d)).
    offer = choose_lease_offer(apt, units, today, people, exception_offers=exception_offers)
    start, end, window = offer["start"], offer["end"], offer["label"]
    budget_reasons_early = offer["budget_reasons"]   # A16 reasons are reported after the bed allocation, as before
    reasons.extend(offer["date_reasons"])
    if reasons or end is None:
        return GroupCheck(False, reasons, start, end)
    # I11(d): every occupant 18 or older on the move-in date, by the enquirer's explicit declaration for each party (I16(b)).
    # A missing declaration, or one made for a later move-in than this group's start, holds the group for staff; MMAI never
    # infers age from anything else. (A declaration for an earlier date covers a later start: the same people are older.)
    # There is no bypass: every caller gets this check.
    for u in units:
        if u.adults_confirmed_move_in is None:
            reasons.append(ADULTS_UNCONFIRMED.format(name=u.people[0].name))
        elif start < u.adults_confirmed_move_in:
            reasons.append(ADULTS_RENEW.format(name=u.people[0].name, declared=u.adults_confirmed_move_in.isoformat(), start=start.isoformat()))
    # A6 rate: the chosen candidate's tariff (equal to rate_kind(start, end) by construction)
    kind = offer["kind"] or rate_kind(start, end)
    rent = offer["rent"] if offer["rent"] is not None else (apt.rent_6m if kind == "6m" else apt.rent_12m)
    extra = extra_bed_charge(code, n)  # A5
    exception_total = offer.get("exception_total")
    total = float(exception_total) if exception_total is not None else rent + extra
    # beds (A4(d), M3, 3.5, couples)
    alloc = allocate_beds(apt, people)
    if alloc is None:
        reasons.append("No bed allocation fits the bedding options, bunk answers and room arrangements (A4, M3, 3.5)")
        return GroupCheck(False, reasons, start, end, kind, window, rent, extra, total)
    rooms, _ = alloc
    # A16 household budget: the only budget test (sum of every member's maximum, +10%/+20%; no per-person ceiling), at the chosen
    # tariff's rent (already assessed in the offer search; recomputed here for the detail figures)
    if exception_total is not None:
        budget_reasons, household = offer["budget_reasons"], offer["budget"]
    else:
        budget_reasons, household = household_budget(code, rent, people)
    reasons.extend(budget_reasons or budget_reasons_early)
    # A12(d)/(e) per-room split (the $70 sits on the room holding the extra bed): proposed shares for the group's agreement, not a test
    # Allocate the approved total, including its already-counted A5 charge. The
    # allocation subtotal is not saved or displayed as a replacement base tariff.
    allocation_subtotal = total - extra if exception_total is not None else rent
    room_costs = per_room_shares(code, allocation_subtotal, rooms, extra)
    shares: dict[str, float] = {}
    for r in rooms:
        k = len(r["occupants"])
        r["room_cost"] = room_costs[r["room"]]
        r["extra_bed"] = extra if extra and room_costs[r["room"]] > allocation_subtotal / tinfo["bedrooms"] + 1e-6 else 0.0
        for o in r["occupants"]:
            share = round(r["room_cost"] / k, 2)
            o["share"] = share
            shares[o["person"].id] = share
    serialisable = [{"room": r["room"], "setup": r["setup"], "room_cost": r["room_cost"], "extra_bed": r["extra_bed"],
                     "occupants": [{"person_id": o["person"].id, "name": o["person"].name, "bed": o["bed"],
                                    "share": o["share"], "arrangement": o.get("arrangement", [])} for o in r["occupants"]]}
                    for r in rooms]
    return GroupCheck(not reasons, reasons, start, end, kind, window, rent, extra, total, serialisable, shares,
                      household, offer.get("lease_choice"), offer.get("exception_ref"), exception_total)
