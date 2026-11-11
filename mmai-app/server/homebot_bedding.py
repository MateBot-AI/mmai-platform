"""Phase B1 — standalone HomeBot bedding feasibility (A4, A5, H21, M3). Pure module: no database, network, UI or legacy
matching. Nothing here ranks, prices or compares apartments; it answers one question per apartment and party:

    can the party's recorded sleeping choices be accommodated by this apartment's bedding options, and how surely?

Statuses
  VERIFIED_FEASIBLE                 anonymous sleeping-capacity feasibility only: every person has a bed of a type they chose,
                                    no room is shared beyond what the choices express, no consent is assumed. It is NOT a
                                    person-to-room assignment and does NOT identify who the partners are (see facts.pairing).
  POSSIBLE_NEEDS_STAFF_CONFIRMATION a physically possible arrangement exists only by relying on something the form never asked
                                    (third occupant in one room, mixed single+bunk room, bunk-only room for a person who did not
                                    choose a bunk, A4(e) king+single sharing). Firm choices are still honoured. Never "suitable".
  NOT_FEASIBLE                      no arrangement honours the firm choices (binding reasons listed).
  UNKNOWN_INVENTORY                 the apartment's bedding columns are missing, inconsistent with the type or not parseable.
                                    Unknown inventory never becomes feasible.
  INVALID_PARTY                     the party record itself is malformed (types, choices, 1–7 count, odd partner count).

Register anchors: A4(a)–(f) bedding values and bunk unit; A5 extra-bed charge (reported as a weekly currency amount, not as a
bed count); H21 own bedroom no exception, spare bedrooms permitted, per-person single counting, two willing bunk users suffice;
M3 only people who said yes go in a bunk. A4(e) (king+single shared by a couple and one other) requires one enquiry AND that the
party is seeking flatmates for the other bedrooms; a HomeBot party alone does not establish the second clause, so that case is
reported as POSSIBLE with A4E_APPLICABILITY_UNRESOLVED and never as verified.

Inventory parsing is strict: the whole bedding string must match the grammar
    value = setup (" or " setup)* ; setup = item ("+" item)* ; item = count bed
with bed in {king, queen, double, single, bunk}; the vocabulary is exact: "singles" (plural) only for two or three singles and the
word "bed" only in "bunk bed" ("1 king bed", "1 kings", "1 singles" are INVALID). Anything else is INVALID; there is no prefix
acceptance. A setup with two bunk units, two couple beds or more than three sleeping places is rejected (A4(c)/(f)).
Physical combinations are validated BEFORE any occupancy search: a chosen combination (one setup per room) may contain at most one
bunk unit even if unused and its total sleeping places may not exceed the A4(b) maximum for the type; an apartment with no valid
combination is UNKNOWN_INVENTORY (e.g. two fixed bunk-only rooms, or fixed "3 singles" + "3 singles" in a 2-bedroom).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from itertools import product
from typing import Optional

from . import budget, rules

# ----------------------------------------------------------------------------------------------------------- statuses
VERIFIED_FEASIBLE = "VERIFIED_FEASIBLE"
POSSIBLE_NEEDS_STAFF_CONFIRMATION = "POSSIBLE_NEEDS_STAFF_CONFIRMATION"
NOT_FEASIBLE = "NOT_FEASIBLE"
UNKNOWN_INVENTORY = "UNKNOWN_INVENTORY"
INVALID_PARTY = "INVALID_PARTY"

# reasons: POSSIBLE tier (what staff must confirm)
THIRD_OCCUPANT_CONSENT_UNCONFIRMED = "THIRD_OCCUPANT_CONSENT_UNCONFIRMED"   # three people in one room
MIXED_ROOM_CONSENT_UNCONFIRMED = "MIXED_ROOM_CONSENT_UNCONFIRMED"           # single bed(s) and the bunk occupied in one room
BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED = "BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED"   # own-room/alone person, only a bunk available
BED_TYPE_CONSENT_UNCONFIRMED = "BED_TYPE_CONSENT_UNCONFIRMED"               # "Bunk bed" chooser placed in an ordinary single
A4E_APPLICABILITY_UNRESOLVED = "A4E_APPLICABILITY_UNRESOLVED"               # couple + one other in a king+single room
# reasons: NOT_FEASIBLE tier (binding constraints)
ROOMS_INSUFFICIENT = "ROOMS_INSUFFICIENT"           # own rooms + partner pairs need more exclusive rooms than exist
COUPLE_BED_UNAVAILABLE = "COUPLE_BED_UNAVAILABLE"   # a partner pair has no king/queen/double room
TWIN_SETUP_UNAVAILABLE = "TWIN_SETUP_UNAVAILABLE"   # a twin-only person has no single bed
BUNK_UNAVAILABLE = "BUNK_UNAVAILABLE"               # a "Bunk bed" chooser and no bunk unit in any setup
BUNK_UNWANTED = "BUNK_UNWANTED"                     # capacity only reachable by putting a non-acceptor in a bunk
BUNK_UNIT_LIMIT = "BUNK_UNIT_LIMIT"                 # more than two "Bunk bed" choosers: one bunk unit per apartment (A4(f))
CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"             # more people than A4(b) allows for the type / than beds exist
NO_ARRANGEMENT = "NO_ARRANGEMENT"                   # fallback: exhaustive search found nothing

# sleeping choices exactly as HomeBot stores them (tests assert equality with server.homebot.SLEEP / SLEEP_ALONE)
OWN = "Own bedroom"
PARTNER = "Shares a bed with partner"
TWIN = "Twin room (two single beds)"
TWIN_OR_BUNK = "Twin room or bunk"
BUNK = "Bunk bed"
ALONE = "Whole apartment (alone)"
CHOICES = (OWN, PARTNER, TWIN, TWIN_OR_BUNK, BUNK, ALONE)
MIN_PEOPLE, MAX_PEOPLE = 1, 7   # eight or more never reaches the software (H22 terminal screen)

BEDS = ("king", "queen", "double", "single", "bunk")
COUPLE = frozenset(rules.COUPLE_BEDS)
_ITEM = re.compile(r"(\d{1,2})\s+(king|queen|double|single|bunk)(s?)(\s+bed)?")   # "2 singles", "1 bunk bed", "1 king"
# Engineering input limits (not accommodation policy): the A4(d) domain is tiny, so anything outside these bounds is rejected
# BEFORE any list is built or any search runs. The longest real value is 50 characters with 4 alternatives of up to 2 items.
MAX_VALUE_LEN = 120        # characters per bedding column
MAX_ALTERNATIVES = 8       # " or " alternatives per column
MAX_ITEMS = 3              # "+"-joined items per alternative
MAX_COUNT = 3              # beds of one kind in one item (a room never sleeps more than three, A4(c))


class InvalidInventory(ValueError):
    """A bedding value or the set of bedroom columns cannot be trusted."""


class InvalidParty(ValueError):
    """The party record is malformed; nothing can be assessed."""


# ------------------------------------------------------------------------------------------------------- inventory
Setup = tuple[str, ...]   # beds in one room for one alternative, e.g. ("king", "single")


def parse_room(value) -> tuple[Setup, ...]:
    """Strict full-match parse of one bedroom column into its alternative setups. Raises InvalidInventory."""
    if not isinstance(value, str):
        raise InvalidInventory(f"bedding value must be text, got {type(value).__name__}")
    if len(value) > MAX_VALUE_LEN:
        raise InvalidInventory(f"bedding value longer than {MAX_VALUE_LEN} characters")
    text = " ".join(value.strip().lower().split())
    if not text:
        raise InvalidInventory("empty bedding value")
    alts = re.split(r"\s+or\s+", text)
    if len(alts) > MAX_ALTERNATIVES:
        raise InvalidInventory(f"more than {MAX_ALTERNATIVES} alternatives in one bedding value")
    setups: list[Setup] = []
    for alt in alts:
        parts = alt.split("+")
        if len(parts) > MAX_ITEMS:
            raise InvalidInventory(f"more than {MAX_ITEMS} bed items in one setup: {alt!r}")
        beds: list[str] = []
        for part in parts:
            m = _ITEM.fullmatch(part.strip())
            if not m:
                raise InvalidInventory(f"unrecognised bedding text {value!r} (near {part.strip()!r})")
            n, bed, plural, suffix = int(m.group(1)), m.group(2), bool(m.group(3)), bool(m.group(4))
            if not 1 <= n <= MAX_COUNT:                 # two digits at most by the pattern; bounded again before expansion
                raise InvalidInventory(f"bed count must be 1–{MAX_COUNT} in {value!r}, got {n}")
            if plural != (bed == "single" and n > 1):   # only "singles" takes a plural; "kings"/"1 singles" are not values
                raise InvalidInventory(f"unrecognised bedding text {value!r} (near {part.strip()!r})")
            if suffix and bed != "bunk":                # "bed" suffix appears only as "bunk bed" in the A4(d) vocabulary
                raise InvalidInventory(f"unrecognised bedding text {value!r} (near {part.strip()!r})")
            beds.extend([bed] * n)
        s = _check_setup(tuple(beds))            # ≤1 bunk unit, ≤1 couple bed, ≤3 places (A4(c)/(f)); sorted, deduplicated
        if s not in setups:
            setups.append(s)
    return tuple(setups)


def places(setup: Setup) -> int:
    """Sleeping places in a setup: couple bed 2, single 1, bunk 2 (the bunk's places are bunk places only)."""
    return sum(rules.BED_SLEEPS[b] for b in setup)


def describe(setup: Optional[Setup]) -> str:
    if not setup:
        return "no setup"
    out, seen = [], []
    for b in setup:
        if b in seen:
            continue
        seen.append(b)
        n = setup.count(b)
        out.append(f"{n} {b}{'s' if n > 1 and b == 'single' else ''}{' bed' if b == 'bunk' else ''}")
    return " + ".join(out)


def _check_setup(setup) -> Setup:
    if not isinstance(setup, tuple) or not setup or not all(isinstance(b, str) and b in BEDS for b in setup):
        raise InvalidInventory(f"setup must be a non-empty tuple of beds from {BEDS}: {setup!r}")
    if setup.count("bunk") > 1:
        raise InvalidInventory(f"more than one bunk unit in one setup is impossible (A4(f)): {setup!r}")
    if sum(1 for b in setup if b in COUPLE) > 1:
        raise InvalidInventory(f"more than one king/queen/double in one room is not an ALM setup: {setup!r}")
    if places(setup) > 3:
        raise InvalidInventory(f"a room never sleeps more than three (A4(c)): {setup!r}")
    return tuple(sorted(setup))


def combo_is_physical(combo: tuple[Setup, ...], a4b_max: int) -> bool:
    """One setup per room: at most one bunk unit in the whole apartment (even unused) and places within the A4(b) type maximum."""
    return sum(s.count("bunk") for s in combo) <= 1 and sum(places(s) for s in combo) <= a4b_max


@dataclass(frozen=True)
class ApartmentRooms:
    """Validated at construction (so a caller bypassing the parser cannot defeat the caps): type code known, exactly one entry per
    bedroom, each a non-empty tuple of ≤ MAX_ALTERNATIVES valid setups, and at least one physical combination (see combo_is_physical)."""
    type_code: str                        # normalised: Studio, 1BR, 2B1B, 2B2B, 3B
    rooms: tuple[tuple[Setup, ...], ...]  # one entry per bedroom (studio: its sleeping area), each a tuple of setups

    def __post_init__(self):
        if not isinstance(self.type_code, str) or self.type_code not in rules.APT_TYPES:
            raise InvalidInventory(f"unknown apartment type {self.type_code!r}")
        n = rules.APT_TYPES[self.type_code]["bedrooms"]
        if not isinstance(self.rooms, tuple) or len(self.rooms) != n:
            raise InvalidInventory(f"{self.type_code} needs exactly {n} bedroom entr{'y' if n == 1 else 'ies'}")
        rooms = []
        for r in self.rooms:
            if not isinstance(r, tuple) or not r or len(r) > MAX_ALTERNATIVES:
                raise InvalidInventory(f"each room needs 1–{MAX_ALTERNATIVES} setups, got {r!r}")
            clean = []
            for st in r:
                st = _check_setup(st)
                if st not in clean:
                    clean.append(st)
            rooms.append(tuple(clean))
        object.__setattr__(self, "rooms", tuple(rooms))
        if not self.physical_combos:
            raise InvalidInventory(f"no physical setup combination fits a {self.type_code}: at most one bunk unit and "
                                   f"{self.a4b_max} sleeping places (A4(b)/(f))")

    @property
    def standard_capacity(self) -> int:
        return rules.APT_TYPES[self.type_code]["standard_capacity"]

    @property
    def a4b_max(self) -> int:
        return rules.APT_TYPES[self.type_code]["a4b_max"]

    @property
    def physical_combos(self) -> tuple[tuple[Setup, ...], ...]:
        return tuple(c for c in product(*self.rooms) if combo_is_physical(c, self.a4b_max))


def parse_apartment(type_code, bedroom1=None, bedroom2=None, bedroom3=None) -> ApartmentRooms:
    """Bedroom columns must match the type exactly: the first N non-empty and valid, the rest empty. Raises InvalidInventory."""
    try:
        code = rules.normalise_type(type_code if isinstance(type_code, str) else "")
    except ValueError as e:
        raise InvalidInventory(str(e)) from None
    n = rules.APT_TYPES[code]["bedrooms"]
    cols = [bedroom1, bedroom2, bedroom3]
    rooms: list[tuple[Setup, ...]] = []
    for i, col in enumerate(cols, start=1):
        present = isinstance(col, str) and col.strip() != ""
        if i <= n and not present:
            raise InvalidInventory(f"{code} needs bedroom{i} bedding; it is missing")
        if i > n and (present or (col is not None and not isinstance(col, str))):
            raise InvalidInventory(f"{code} has {n} bedroom(s) but bedroom{i} is filled")
        if i <= n:
            rooms.append(parse_room(col))
    return ApartmentRooms(code, tuple(rooms))


# ----------------------------------------------------------------------------------------------------------- party
@dataclass(frozen=True)
class PartySleep:
    """Counts only. No pairing identities: 'partner_n' people chose to share a bed with a partner; who pairs with whom is unknown."""
    own_n: int = 0
    partner_n: int = 0
    twin_n: int = 0          # "Twin room (two single beds)": single bed in a room of at most two, never a bunk
    twin_or_bunk_n: int = 0  # single bed under the twin rule, or a bunk place; counted once (H21)
    bunk_n: int = 0          # "Bunk bed": a bunk place
    alone: bool = False      # single-person enquiry taking the whole apartment

    def __post_init__(self):
        for name in ("own_n", "partner_n", "twin_n", "twin_or_bunk_n", "bunk_n"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise InvalidParty(f"{name} must be a non-negative integer")
        if not isinstance(self.alone, bool):
            raise InvalidParty("alone must be a bool")
        n = self.size
        if n < MIN_PEOPLE or n > MAX_PEOPLE:
            raise InvalidParty(f"party size must be {MIN_PEOPLE}–{MAX_PEOPLE}, got {n}")
        if self.partner_n % 2:
            raise InvalidParty("'Shares a bed with partner' needs an even number of people")
        if self.alone and n != 1:
            raise InvalidParty("'Whole apartment (alone)' is a single-person choice")

    @property
    def size(self) -> int:
        return self.own_n + self.partner_n + self.twin_n + self.twin_or_bunk_n + self.bunk_n + (1 if self.alone else 0)

    @property
    def pairs(self) -> int:
        return self.partner_n // 2


def party_from_people(people) -> PartySleep:
    """Build counts from saved HomeBot people records ({'sleep': <choice>, ...}). Raises InvalidParty on anything unexpected."""
    if not isinstance(people, (list, tuple)):
        raise InvalidParty("people must be a list")
    if not (MIN_PEOPLE <= len(people) <= MAX_PEOPLE):
        raise InvalidParty(f"party size must be {MIN_PEOPLE}–{MAX_PEOPLE}, got {len(people)}")
    counts = {c: 0 for c in CHOICES}
    for p in people:
        if not isinstance(p, dict):
            raise InvalidParty("each person must be a record")
        s = p.get("sleep")
        if not isinstance(s, str) or s not in counts:
            raise InvalidParty(f"unknown sleeping choice {s!r}")
        counts[s] += 1
    if counts[ALONE] and len(people) != 1:
        raise InvalidParty("'Whole apartment (alone)' is a single-person choice")
    return PartySleep(own_n=counts[OWN], partner_n=counts[PARTNER], twin_n=counts[TWIN],
                      twin_or_bunk_n=counts[TWIN_OR_BUNK], bunk_n=counts[BUNK], alone=bool(counts[ALONE]))


# ------------------------------------------------------------------------------------------------------- assessment
# occupancy "units" distributed over rooms; counts per unit kind
_OWN, _PAIR, _TWIN, _TOB, _BUNK, _ALONE = "own", "pair", "twin", "twin_or_bunk", "bunk", "alone"
_KINDS = (_OWN, _PAIR, _TWIN, _TOB, _BUNK, _ALONE)
_LABEL = {_OWN: "one person, own bedroom", _PAIR: "one partner pair (identities not assigned)", _TWIN: "twin-room person",
          _TOB: "twin-or-bunk person", _BUNK: "bunk-bed person", _ALONE: "one person, whole apartment"}


@dataclass(frozen=True)
class RoomUse:
    setup: Optional[Setup]
    occupants: dict            # unit kind -> count (people, pairs count as 2 people)
    tier: str                  # VERIFIED_FEASIBLE | POSSIBLE_NEEDS_STAFF_CONFIRMATION
    reasons: tuple[str, ...]
    bunk_places_used: int
    beds_unused: int

    @property
    def people(self) -> int:
        return sum(v * (2 if k == _PAIR else 1) for k, v in self.occupants.items())

    def describe(self) -> dict:
        who = ", ".join(f"{v} × {_LABEL[k]}" if v > 1 or k not in (_OWN, _PAIR, _ALONE) else _LABEL[k]
                        for k, v in self.occupants.items() if v)
        return {"setup": describe(self.setup), "occupancy": who or "unoccupied (spare bedroom)", "people": self.people,
                "beds_unused": self.beds_unused, "bunk_places_used": self.bunk_places_used, "tier": self.tier,
                "reasons": list(self.reasons)}


def _room_use(setup: Setup, occ: dict) -> Optional[RoomUse]:
    """Evaluate one room with one setup and a given set of occupant units. None = impossible without breaking a firm choice."""
    people = sum(v * (2 if k == _PAIR else 1) for k, v in occ.items())
    if people == 0:                                                 # spare bedroom (H21): hardware kept for the limit checks
        return RoomUse(setup, dict(occ), VERIFIED_FEASIBLE, (), 0, len(setup))
    singles = setup.count("single")
    couple = sum(1 for b in setup if b in COUPLE)
    bunk_places = 2 if "bunk" in setup else 0
    ordinary = singles + couple
    reasons: list[str] = []

    # exclusive-room people: an own-bedroom person or the sole enquirer
    if occ.get(_OWN) or occ.get(_ALONE):
        if people != 1:
            return None                                             # H21: own bedroom shared — no exception
        if ordinary:
            return RoomUse(setup, dict(occ), VERIFIED_FEASIBLE, (), 0, len(setup) - 1)
        # bunk-only room: possible, but a bunk was never chosen by this person
        return RoomUse(setup, dict(occ), POSSIBLE_NEEDS_STAFF_CONFIRMATION, (BUNK_ONLY_ROOM_CONSENT_UNCONFIRMED,), 1, 0)

    # a partner pair needs a couple bed; at most one pair per room (no setup has two couple beds)
    if occ.get(_PAIR):
        if occ[_PAIR] > 1 or couple < 1:
            return None
        others = people - 2
        if others == 0:
            return RoomUse(setup, dict(occ), VERIFIED_FEASIBLE, (), 0, len(setup) - 1)
        # couple + one other person: only the king+single room shape exists for this (A4(e)); applicability unresolved
        if others != 1 or singles != 1 or bunk_places:
            return None
        if occ.get(_TWIN) or occ.get(_TOB):
            reasons.append(A4E_APPLICABILITY_UNRESOLVED)           # the other person takes the single bed
        elif occ.get(_BUNK):
            reasons.extend([A4E_APPLICABILITY_UNRESOLVED, BED_TYPE_CONSENT_UNCONFIRMED])
        return RoomUse(setup, dict(occ), POSSIBLE_NEEDS_STAFF_CONFIRMATION, tuple(reasons), 0, 0)

    # twin / twin-or-bunk / bunk people
    t, u, b = occ.get(_TWIN, 0), occ.get(_TOB, 0), occ.get(_BUNK, 0)
    if couple and not singles and not bunk_places:
        # a room offering only a couple bed: nobody here chose that bed type, so one person alone is possible only with
        # confirmation (BED_TYPE); two unrelated people never share a couple bed
        if people == 1:
            return RoomUse(setup, dict(occ), POSSIBLE_NEEDS_STAFF_CONFIRMATION, (BED_TYPE_CONSENT_UNCONFIRMED,), 0, 0)
        return None
    if t > singles:
        return None                                                 # twin-only people never take a bunk place
    free_singles = singles - t
    in_bunk = min(b, bunk_places)
    b_in_single = b - in_bunk
    if b_in_single > free_singles:
        return None
    free_singles -= b_in_single
    bunk_left = bunk_places - in_bunk
    u_single = min(u, free_singles)
    u_bunk = u - u_single
    if u_bunk > bunk_left:
        return None
    bunk_used = in_bunk + u_bunk
    singles_used = t + b_in_single + u_single
    if b_in_single:
        reasons.append(BED_TYPE_CONSENT_UNCONFIRMED)
    if people > 2:
        reasons.append(THIRD_OCCUPANT_CONSENT_UNCONFIRMED)
    if singles_used and bunk_used:
        reasons.append(MIXED_ROOM_CONSENT_UNCONFIRMED)
    tier = POSSIBLE_NEEDS_STAFF_CONFIRMATION if reasons else VERIFIED_FEASIBLE
    unused = (singles - singles_used) + (1 if bunk_places and not bunk_used else 0) + (couple if couple else 0)
    return RoomUse(setup, dict(occ), tier, tuple(reasons), bunk_used, unused)


@dataclass(frozen=True)
class BeddingAssessment:
    status: str
    reasons: tuple[str, ...]
    facts: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons), "facts": dict(self.facts)}


def _charge_facts(apt: ApartmentRooms, people: int) -> dict:
    extra_occupants = max(0, people - apt.standard_capacity)
    charge: Decimal = budget.extra_bed_charge(apt.type_code, people)
    return {"head_count": people, "standard_capacity": apt.standard_capacity, "extra_occupants": extra_occupants,
            "extra_bed_charge_weekly": f"{budget.cents(charge):.2f}", "currency": "AUD per week (A5/A7)"}


def _pairing_note(party: PartySleep) -> Optional[str]:
    if party.partner_n == 0:
        return None
    return (f"{party.partner_n} people chose 'Shares a bed with partner' ({party.pairs} pair"
            f"{'s' if party.pairs > 1 else ''}); the form records no pairing, so who shares with whom is NOT assigned here"
            + (" — with four or more partner selectors staff must confirm the pairs." if party.partner_n >= 4 else "."))


@lru_cache(maxsize=4096)
def _room_use_cached(setup: Setup, occ: tuple[int, ...]) -> Optional[RoomUse]:
    return _room_use(setup, {k: v for k, v in zip(_KINDS, occ) if v})


def _summarise(arr: tuple[RoomUse, ...]) -> dict:
    return {"rooms": [ru.describe() for ru in arr],
            "rooms_used": sum(1 for ru in arr if ru.people), "spare_rooms": sum(1 for ru in arr if not ru.people),
            "uses_bunk": any(ru.bunk_places_used for ru in arr), "bunk_units_used": sum(1 for ru in arr if ru.bunk_places_used),
            "bunk_units_physical": sum(ru.setup.count("bunk") for ru in arr if ru.setup),   # ≤ 1 by combo validation, even unused
            "places_physical": sum(places(ru.setup) for ru in arr if ru.setup),              # ≤ A4(b) max, spare rooms included
            "beds_unused": sum(ru.beds_unused for ru in arr)}


class _Search:
    """Depth-first distribution of anonymous units over the rooms of one physical combination, pruning a room as soon as its
    occupancy breaks a firm choice. Only counts, one representative per tier and the set of confirmation codes are kept."""

    def __init__(self):
        self.verified_n = 0
        self.verified_rep: Optional[tuple[RoomUse, ...]] = None
        self.possible_n = 0
        self.possible_rep: Optional[tuple[RoomUse, ...]] = None
        self.possible_key = None
        self.codes: set[str] = set()

    def record(self, arr: tuple[RoomUse, ...]):
        if all(ru.tier == VERIFIED_FEASIBLE for ru in arr):
            self.verified_n += 1
            if self.verified_rep is None:
                self.verified_rep = arr
            return
        reasons = {r for ru in arr for r in ru.reasons}
        self.codes |= reasons
        self.possible_n += 1
        # representative: fewest distinct confirmations, then setup text (deterministic engineering choice, not a ranking rule)
        key = (len(reasons), [describe(ru.setup) for ru in arr])
        if self.possible_key is None or key < self.possible_key:
            self.possible_key, self.possible_rep = key, arr

    def run(self, combo: tuple[Setup, ...], units: tuple[int, ...]):
        self._rec(combo, 0, units, ())

    def _rec(self, combo, i, remaining, acc):
        setup = combo[i]
        if i == len(combo) - 1:                      # last room takes whatever is left
            ru = _room_use_cached(setup, remaining)
            if ru is not None:
                self.record(acc + (ru,))
            return
        for pick in product(*(range(n + 1) for n in remaining)):
            ru = _room_use_cached(setup, pick)
            if ru is None:
                continue
            rest = tuple(r - p for r, p in zip(remaining, pick))
            self._rec(combo, i + 1, rest, acc + (ru,))


def assess_bedding(apt: ApartmentRooms, party: PartySleep) -> BeddingAssessment:
    """Exhaustive search over physical setup combinations and anonymous unit distributions. No ranking between arrangements."""
    if not isinstance(apt, ApartmentRooms) or not isinstance(party, PartySleep):
        raise TypeError("assess_bedding needs a validated ApartmentRooms and PartySleep")
    base = _charge_facts(apt, party.size)
    base["rooms_total"] = len(apt.rooms)
    note = _pairing_note(party)
    if note:
        base["pairing"] = note
    if party.size > apt.a4b_max:
        return BeddingAssessment(NOT_FEASIBLE, (CAPACITY_EXCEEDED,), base | {"a4b_max": apt.a4b_max})

    units = (party.own_n, party.pairs, party.twin_n, party.twin_or_bunk_n, party.bunk_n, 1 if party.alone else 0)
    search = _Search()
    for combo in apt.physical_combos:           # validated BEFORE occupancy: ≤1 bunk unit in the chosen setups, places ≤ A4(b) max
        search.run(combo, units)

    if search.verified_rep is not None:
        facts = base | {"meaning": "anonymous sleeping-capacity feasibility only; not a person-to-room assignment",
                        "arrangement": _summarise(search.verified_rep), "verified_arrangements": search.verified_n}
        return BeddingAssessment(VERIFIED_FEASIBLE, (), facts)
    if search.possible_rep is not None:
        reasons = tuple(sorted({r for ru in search.possible_rep for r in ru.reasons}))
        facts = base | {"meaning": "physically possible only with staff confirmation of the listed points; not suitable yet",
                        "arrangement": _summarise(search.possible_rep), "possible_arrangements": search.possible_n,
                        "all_confirmation_codes": sorted(search.codes)}
        return BeddingAssessment(POSSIBLE_NEEDS_STAFF_CONFIRMATION, reasons, facts)
    return BeddingAssessment(NOT_FEASIBLE, _binding_reasons(apt, party), base)


def _binding_reasons(apt: ApartmentRooms, party: PartySleep) -> tuple[str, ...]:
    """Diagnostics for a NOT_FEASIBLE result. Apartment-level constraints first, then the reasons seen across setup
    combinations: every combination fails for at least one of the listed reasons (A4 alternatives are per room, so a bunk and
    the single beds it replaces are never available together)."""
    out: list[str] = []
    rooms = apt.rooms
    if party.own_n + party.pairs + (1 if party.alone else 0) > len(rooms):
        out.append(ROOMS_INSUFFICIENT)
    if party.bunk_n > 2 and any("bunk" in s for r in rooms for s in r):
        out.append(BUNK_UNIT_LIMIT)
    non_bunk_people = party.own_n + party.partner_n + party.twin_n + (1 if party.alone else 0)
    per_combo: list[str] = []
    for combo in apt.physical_combos:
        couple_rooms = sum(1 for s in combo if any(b in COUPLE for b in s))
        singles = sum(s.count("single") for s in combo)
        has_bunk = any("bunk" in s for s in combo)
        ordinary_places = sum(places(tuple(b for b in s if b != "bunk")) for s in combo)
        total_places = ordinary_places + (2 if has_bunk else 0)   # at most one bunk unit is ever usable (A4(f))
        found: list[str] = []
        if party.pairs > couple_rooms:
            found.append(COUPLE_BED_UNAVAILABLE)
        if party.twin_n > singles:
            found.append(TWIN_SETUP_UNAVAILABLE)
        if party.bunk_n and not has_bunk:
            found.append(BUNK_UNAVAILABLE)
        if non_bunk_people > ordinary_places:
            found.append(BUNK_UNWANTED if has_bunk else CAPACITY_EXCEEDED)
        if party.size > total_places:
            found.append(CAPACITY_EXCEEDED)
        per_combo.extend(found)
    order = (COUPLE_BED_UNAVAILABLE, TWIN_SETUP_UNAVAILABLE, BUNK_UNAVAILABLE, BUNK_UNWANTED, CAPACITY_EXCEEDED)
    out.extend(code for code in order if code in per_combo and code not in out)
    return tuple(out) or (NO_ARRANGEMENT,)


def assess(type_code, bedroom1, bedroom2, bedroom3, people) -> BeddingAssessment:
    """Convenience entry: raw inventory columns + saved people records → assessment, with parse failures as statuses."""
    try:
        party = party_from_people(people)
    except InvalidParty as e:
        return BeddingAssessment(INVALID_PARTY, ("INVALID_PARTY",), {"detail": str(e)})
    try:
        apt = parse_apartment(type_code, bedroom1, bedroom2, bedroom3)
    except InvalidInventory as e:
        return BeddingAssessment(UNKNOWN_INVENTORY, ("UNKNOWN_INVENTORY",), {"detail": str(e)})
    return assess_bedding(apt, party)
