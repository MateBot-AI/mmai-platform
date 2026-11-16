"""Pure H28 travel-assessment maths for HomeBot. No routing provider, network, AI, database or personal data: callers inject
per-person journey estimates (minutes) for each ACCEPTED mode to the person's PRIMARY destination; people are referred to by
their integer position only.

Register H28 (5 November 2026):
  (a) exclusion ceiling = twice the stated time, applied to the closest-majority average; "60 minutes or more" → 120-minute ceiling;
  (b) score the average journey time of the closest strict majority of people WITH KNOWN DESTINATIONS (3 of 5, 3 of 4); show
      everyone else's known journey separately; an outlier above the doubled limit does not by itself exclude the apartment;
  (c) several accepted modes → use the fastest explicitly accepted mode and identify it; never substitute an unaccepted mode;
  (d) an unmeasurable journey stays visible as "Travel time needs confirmation", never a confirmed pass;
  (e) a long journey inside the selected majority does not exclude when the majority average passes — it is clearly warned;
  (f) work + study: one contribution via the primary destination; the other journey is shown, never counted twice;
  (g) routing basis: typical weekday 8:00 am Melbourne departure, labelled an estimate. Supplied estimates are NOT verified here.

Truthfulness rules implemented here (parent review, 6 November 2026):
  * Any known primary destination whose accepted modes are partly or wholly unmeasured makes the overall result
    NEEDS_CONFIRMATION — never a confirmed PASS. A separately labelled upper-bound diagnostic may be shown; it is not the
    closest-majority score, not a fastest-mode claim, and the confirmed comparison booleans stay null.
  * NO_DESTINATION people are distinct and excluded from n (as ruled); secondary-destination incompleteness is display-only.
  * Comparisons use the exact total of the closest k against k × limit / k × ceiling (integer/Decimal arithmetic) BEFORE any
    rounding; the displayed one-decimal average is presentation only; the exact sum and k are kept in the facts.
  * Long-journey warnings: a MEASURED fastest time above the ceiling is a confirmed warning even inside the majority (H28(e));
    a PARTIAL best-known time above the ceiling is only a provisional note (another accepted mode might be faster).
  * The 5-minute option reads "walking distance only": until its interpretation with other accepted modes is ruled, any accepted
    mode other than Walk with that option yields NEEDS_CONFIRMATION with TRAVEL_PREFERENCE_MODE_AMBIGUITY; Walk-only uses the
    5 / 10-minute maths. (Pending interpretation, documented; no form change, no new rule.)
  * No duration cut-off: any finite non-negative time evaluates (a 1,500-minute journey is simply long). Only representation
    limits apply (MAX_INT_DIGITS integer digits, MAX_FRACTION_DIGITS decimal places, checked on the true magnitude including
    the Decimal exponent BEFORE any arithmetic or formatting) as engineering safeguards, not policy. Inputs are never rounded;
    all sums and comparisons run in a local exact Decimal context (EXACT_CTX, precision 40, Inexact trapped) so the caller's
    ambient precision cannot change a verdict. A future seconds-based adapter must supply exact decimals within these bounds
    (or rational handling must be added explicitly); nothing may round before the decision. The adapter is not built here.
  * Every Destination is re-validated against the assessment preference at assessment time (exact accepted-mode set), so a
    destination built for another preference (e.g. Car-only) can never be scored under this one.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
from fractions import Fraction
from typing import Optional, Union

# ------------------------------------------------------------------------------------------------- exact form vocabulary
MAX_TRAVEL = ("5 minutes — walking distance only", "15 minutes", "30 minutes", "45 minutes", "60 minutes or more")
STATED_LIMIT = {MAX_TRAVEL[0]: 5, MAX_TRAVEL[1]: 15, MAX_TRAVEL[2]: 30, MAX_TRAVEL[3]: 45, MAX_TRAVEL[4]: 60}
WALK_ONLY_OPTION = MAX_TRAVEL[0]
# Walk, Bicycle and Public transport are the choices offered (H28(c), 16 Nov 2026). The rest are earlier answers kept exactly as
# given on existing enquiries; the relative order of the earlier names is unchanged.
TRANSPORT = ("Walk", "Bicycle", "Public transport", "Tram", "Train", "Bus", "Bike or scooter", "Car")
WALK = "Walk"

UNMEASURED = "UNMEASURED"          # route for an accepted mode not (yet) measured
NO_DESTINATION = None              # person has no primary destination ("Not yet decided"; "Looking for work" without a place)

# statuses
PASS = "PASS"
EXCEEDS_CEILING = "EXCEEDS_CEILING"
NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
NO_DESTINATIONS = "NO_DESTINATIONS"
# per-person
MEASURED, PARTIAL, UNMEASURED_PERSON, NO_DEST_PERSON = "MEASURED", "PARTIAL", "UNMEASURED", "NO_DESTINATION"
# reason codes
TRAVEL_MAJORITY_WITHIN_CEILING = "TRAVEL_MAJORITY_WITHIN_CEILING"
TRAVEL_MAJORITY_EXCEEDS_CEILING = "TRAVEL_MAJORITY_EXCEEDS_CEILING"
TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES = "TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES"
TRAVEL_NEEDS_CONFIRMATION_INSUFFICIENT_MEASURED = "TRAVEL_NEEDS_CONFIRMATION_INSUFFICIENT_MEASURED"
TRAVEL_PREFERENCE_MODE_AMBIGUITY = "TRAVEL_PREFERENCE_MODE_AMBIGUITY"
TRAVEL_NO_DESTINATIONS = "TRAVEL_NO_DESTINATIONS"
LONG_JOURNEY_WARNING = "LONG_JOURNEY_WARNING"
LONG_JOURNEY_PROVISIONAL = "LONG_JOURNEY_PROVISIONAL"
FASTEST_MODE_UNCONFIRMED = "FASTEST_MODE_UNCONFIRMED"

PASS_MEANING = ("PASS is supplied-estimate arithmetic only (closest-majority total within the ceiling); it is not independently "
                "verified route availability or timing.")
ROUTING_BASIS = ("Supplied estimates for a typical weekday 08:00 Australia/Melbourne departure (H28(g)); estimates only, not "
                 "guaranteed journey times, and NOT independently provider-verified here — the later routing adapter must "
                 "honour and verify this basis.")
WALK_ONLY_PENDING = ("The 5-minute option reads 'walking distance only'. Its meaning when other modes are accepted is not yet "
                     "ruled; such enquiries need staff confirmation. Walk-only enquiries use the 5-minute limit / 10-minute ceiling.")
UPPER_BOUND_LABEL = ("Diagnostic only: upper bound using best-known times for people with incomplete estimates. It is NOT the "
                     "closest-majority score and NOT a fastest-mode claim; the confirmed comparisons remain unset.")

# engineering representation limits (not policy): minutes with more than 7 integer digits or 9 decimal places are malformed input
MAX_INT_DIGITS, MAX_FRACTION_DIGITS = 7, 9
MIN_PEOPLE, MAX_PEOPLE = 1, 7
# exact arithmetic: 7 + 9 digits per value, at most 7 values summed → far below 40 digits; any inexact operation is an error
EXACT_CTX = Context(prec=40, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, Inexact])
DISPLAY_CTX = Context(prec=40, rounding=ROUND_HALF_UP)   # one-decimal presentation only


class InvalidTravelInput(ValueError):
    """Malformed preference, route or person record; nothing is assessed."""


Minutes = Union[int, Decimal, Fraction]


def _minutes(v) -> Decimal:
    """Validated, bounded, exact minutes. Magnitude is checked from sign/exponent BEFORE any arithmetic or text formatting,
    so a Decimal like 1E+1000000 or 0E+1000000 is rejected/canonicalised without ever being expanded."""
    if isinstance(v, Fraction):
        # Provider seconds, including nanoseconds, divided by 60 without rounding.
        if v < 0 or v >= 10 ** MAX_INT_DIGITS or v.denominator > 60 * 10 ** 9:
            raise InvalidTravelInput("rational minutes outside representation bounds")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        raise InvalidTravelInput(f"minutes must be an int or Decimal, got {type(v).__name__}")
    d = v if isinstance(v, Decimal) else Decimal(v)
    if not d.is_finite():
        raise InvalidTravelInput("minutes must be finite")
    sign, digits, exp = d.as_tuple()
    if d.is_zero():
        return Decimal(0)                                   # canonical zero (drops any exponent and the sign)
    if sign:
        raise InvalidTravelInput("minutes cannot be negative")
    frac = -exp if exp < 0 else 0
    if frac > MAX_FRACTION_DIGITS:
        raise InvalidTravelInput(f"minutes limited to {MAX_FRACTION_DIGITS} decimal places (representation limit)")
    if d.adjusted() >= MAX_INT_DIGITS:                      # adjusted() = exponent of the leading digit: 7 digits → max 6
        raise InvalidTravelInput(f"minutes limited to {MAX_INT_DIGITS} integer digits (representation limit)")
    with localcontext(EXACT_CTX):
        return d.quantize(Decimal(1).scaleb(-frac)) if exp > 0 else +d   # canonical: no positive exponent, no rounding


# ------------------------------------------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class TravelPreference:
    max_travel: str
    accepted_modes: tuple[str, ...]

    def __init__(self, max_travel, accepted_modes):
        if not isinstance(max_travel, str) or max_travel not in STATED_LIMIT:
            raise InvalidTravelInput(f"max_travel must be one of {MAX_TRAVEL}")
        if isinstance(accepted_modes, (str, bytes)) or not isinstance(accepted_modes, (list, tuple, set, frozenset)):
            raise InvalidTravelInput("accepted_modes must be a collection of transport options")
        modes = []
        for m in accepted_modes:
            if not isinstance(m, str) or m not in TRANSPORT:
                raise InvalidTravelInput(f"unknown transport mode {m!r}")
            if m not in modes:
                modes.append(m)
        if not modes:
            raise InvalidTravelInput("at least one accepted mode is required")
        object.__setattr__(self, "max_travel", max_travel)
        object.__setattr__(self, "accepted_modes", tuple(sorted(modes, key=TRANSPORT.index)))

    @property
    def limit(self) -> int:
        return STATED_LIMIT[self.max_travel]

    @property
    def ceiling(self) -> int:
        return 2 * self.limit

    @property
    def walk_only_ambiguous(self) -> bool:
        return self.max_travel == WALK_ONLY_OPTION and self.accepted_modes != (WALK,)


@dataclass(frozen=True)
class Destination:
    """Journey estimates to ONE destination: exactly one entry per accepted mode, each minutes or UNMEASURED. Copied, immutable.
    Built for one preference; assess_travel re-checks the modes against ITS preference (a Car-only destination is never scored
    under a Walk-only preference)."""
    routes: tuple[tuple[str, Optional[Decimal]], ...]   # (mode, minutes) with None for UNMEASURED, ordered as TRANSPORT

    def __init__(self, routes, pref: TravelPreference):
        if not isinstance(routes, dict):
            raise InvalidTravelInput("routes must be a mapping mode -> minutes | UNMEASURED")
        if set(routes) != set(pref.accepted_modes):
            raise InvalidTravelInput(f"routes must cover exactly the accepted modes {pref.accepted_modes}, got {tuple(routes)}")
        out = []
        for m in pref.accepted_modes:
            v = routes[m]
            out.append((m, None if (isinstance(v, str) and v == UNMEASURED) else _minutes(v)))
        object.__setattr__(self, "routes", tuple(out))

    @property
    def modes(self) -> tuple[str, ...]:
        return tuple(m for m, _ in self.routes)

    def check_against(self, pref: "TravelPreference", what: str, index: int) -> None:
        if self.modes != pref.accepted_modes:
            raise InvalidTravelInput(f"person {index} {what} destination covers modes {self.modes}, assessment preference accepts "
                                     f"{pref.accepted_modes}; rebuild the destination for this preference")

    @property
    def measured(self) -> tuple[tuple[str, Decimal], ...]:
        return tuple((m, v) for m, v in self.routes if v is not None)

    @property
    def unknown_modes(self) -> tuple[str, ...]:
        return tuple(m for m, v in self.routes if v is None)

    @property
    def status(self) -> str:
        if not self.measured:
            return UNMEASURED_PERSON
        return MEASURED if not self.unknown_modes else PARTIAL

    @property
    def best(self) -> Optional[Decimal]:
        """Fastest measured time: the confirmed fastest when MEASURED, only an upper bound when PARTIAL."""
        return min((v for _, v in self.measured), default=None)

    @property
    def best_modes(self) -> tuple[str, ...]:
        b = self.best
        return tuple(m for m, v in self.measured if v == b) if b is not None else ()


@dataclass(frozen=True)
class PersonTravel:
    index: int                              # strict integer position; unique within the party; no names or addresses
    primary: Optional[Destination]          # NO_DESTINATION (None) when the person has no primary destination
    secondary: Optional[Destination] = None  # display-only (H28(f))

    def __post_init__(self):
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise InvalidTravelInput("index must be a non-negative integer position")
        if self.primary is not None and not isinstance(self.primary, Destination):
            raise InvalidTravelInput("primary must be a Destination or NO_DESTINATION")
        if self.secondary is not None and not isinstance(self.secondary, Destination):
            raise InvalidTravelInput("secondary must be a Destination or None")
        if self.primary is None and self.secondary is not None:
            # a person with a known secondary destination cannot have NO primary: the caller must pick the primary (H28(f))
            raise InvalidTravelInput("a person with a secondary destination must have a primary destination; choose the primary")

    @property
    def status(self) -> str:
        return NO_DEST_PERSON if self.primary is None else self.primary.status


# ---------------------------------------------------------------------------------------------------------- assessment
@dataclass(frozen=True)
class TravelAssessment:
    status: str
    reasons: tuple[str, ...]
    facts: dict

    def as_dict(self) -> dict:
        return {"status": self.status, "reasons": list(self.reasons), "facts": dict(self.facts)}


def majority_size(n: int) -> int:
    """Closest STRICT majority of people with known destinations: 1→1, 2→2, 3→2, 4→3, 5→3, 6→4, 7→4."""
    return n // 2 + 1


def _fmt(d: Optional[Decimal]) -> Optional[str]:
    if isinstance(d, Fraction):
        # Exact evidence, not a rounded decision input. Terminating decimals retain
        # the established representation; recurring values use numerator/denominator.
        with localcontext(EXACT_CTX):
            try:
                return _fmt(Decimal(d.numerator) / Decimal(d.denominator))
            except Inexact:
                return str(d)
    return None if d is None else format(d.normalize() if d == d.to_integral_value() else d, "f")


def _avg_display(total: Decimal, k: int) -> str:
    with localcontext(DISPLAY_CTX):
        if isinstance(total, Fraction):
            total = Decimal(total.numerator) / Decimal(total.denominator)
        return str((total / Decimal(k)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _sum_minutes(values):
    values = list(values)
    if any(isinstance(v, Fraction) for v in values):
        return sum((Fraction(v) for v in values), Fraction(0))
    return sum(values, Decimal(0))


def _person_fact(p: PersonTravel, ceiling: int) -> dict:
    f = {"index": p.index, "status": p.status, "fastest_minutes": None, "fastest_modes": [], "bound_minutes": None,
         "best_known_modes": [], "unknown_modes": [], "warning": None, "in_majority": False}
    d = p.primary
    if d is None:
        return f
    f["unknown_modes"] = list(d.unknown_modes)
    if d.status == MEASURED:
        f["fastest_minutes"], f["fastest_modes"] = _fmt(d.best), list(d.best_modes)
        if d.best > ceiling:
            f["warning"] = LONG_JOURNEY_WARNING
    elif d.status == PARTIAL:
        f["bound_minutes"], f["best_known_modes"] = _fmt(d.best), list(d.best_modes)
        if d.best > ceiling:
            f["warning"] = LONG_JOURNEY_PROVISIONAL      # best known so far; another accepted mode may be faster
    return f


def _secondary_fact(p: PersonTravel) -> Optional[dict]:
    d = p.secondary
    if d is None:
        return None
    return {"index": p.index, "status": d.status, "best_known_minutes": _fmt(d.best), "best_known_modes": list(d.best_modes),
            "unknown_modes": list(d.unknown_modes), "note": "secondary destination: shown only, never counted (H28(f))"}


def assess_travel(pref: TravelPreference, people) -> TravelAssessment:
    """Independent of the caller's Decimal context: every sum and comparison runs in EXACT_CTX (no rounding possible)."""
    with localcontext(EXACT_CTX):
        return _assess(pref, people)


def _assess(pref: TravelPreference, people) -> TravelAssessment:
    if not isinstance(pref, TravelPreference):
        raise InvalidTravelInput("pref must be a TravelPreference")
    if isinstance(people, (str, bytes, dict)) or not isinstance(people, (list, tuple)):
        raise InvalidTravelInput("people must be a list of PersonTravel")
    people = tuple(people)
    if not (MIN_PEOPLE <= len(people) <= MAX_PEOPLE):
        raise InvalidTravelInput(f"party size must be {MIN_PEOPLE}–{MAX_PEOPLE}")
    if not all(isinstance(p, PersonTravel) for p in people):
        raise InvalidTravelInput("every person must be a PersonTravel")
    if len({p.index for p in people}) != len(people):
        raise InvalidTravelInput("person positions must be unique")
    for p in people:                                             # (1) exact accepted-mode set for THIS preference, primary and secondary
        if p.primary is not None:
            p.primary.check_against(pref, "primary", p.index)
        if p.secondary is not None:
            p.secondary.check_against(pref, "secondary", p.index)
    people = tuple(sorted(people, key=lambda p: p.index))       # order invariance

    limit, ceiling = pref.limit, pref.ceiling
    known = [p for p in people if p.primary is not None]
    n, k = len(known), majority_size(len(known))
    pf = {p.index: _person_fact(p, ceiling) for p in people}
    facts = {
        "stated_limit_minutes": limit, "ceiling_minutes": ceiling, "accepted_modes": list(pref.accepted_modes),
        "known_destinations": n, "majority_size": k if n else 0,
        "no_destination": [p.index for p in people if p.primary is None],
        "measured": [p.index for p in known if p.status == MEASURED],
        "partial": [p.index for p in known if p.status == PARTIAL],
        "unmeasured": [p.index for p in known if p.status == UNMEASURED_PERSON],
        "people": [pf[p.index] for p in people],
        "majority": None, "within_ceiling": None, "meets_stated_limit": None, "upper_bound_diagnostic": None,
        "secondary_display": [s for s in (_secondary_fact(p) for p in people) if s],
        "routing_basis": ROUTING_BASIS, "pass_meaning": PASS_MEANING,
    }
    reasons: list[str] = []
    warnings = [p.index for p in known if pf[p.index]["warning"] == LONG_JOURNEY_WARNING]
    provisional = [p.index for p in known if pf[p.index]["warning"] == LONG_JOURNEY_PROVISIONAL]
    if warnings:
        reasons.append(LONG_JOURNEY_WARNING)
    if provisional:
        reasons.append(LONG_JOURNEY_PROVISIONAL)
    if facts["partial"]:
        reasons.append(FASTEST_MODE_UNCONFIRMED)

    if n == 0:
        return TravelAssessment(NO_DESTINATIONS, tuple([TRAVEL_NO_DESTINATIONS] + reasons), facts)

    complete = not facts["partial"] and not facts["unmeasured"]
    if complete and not pref.walk_only_ambiguous:
        chosen = sorted(known, key=lambda p: (p.primary.best, p.index))[:k]
        total = _sum_minutes(p.primary.best for p in chosen)
        for p in chosen:
            pf[p.index]["in_majority"] = True
        facts["majority"] = {"indices": [p.index for p in chosen], "size": k, "total_minutes": _fmt(total),
                             "average_display": _avg_display(total, k),
                             "comparison": f"exact total {_fmt(total)} vs {k} × ceiling {ceiling} = {k * ceiling} and {k} × limit {limit} = {k * limit}"}
        facts["within_ceiling"] = total <= k * ceiling
        facts["meets_stated_limit"] = total <= k * limit
        if facts["within_ceiling"]:
            return TravelAssessment(PASS, tuple([TRAVEL_MAJORITY_WITHIN_CEILING] + reasons), facts)
        return TravelAssessment(EXCEEDS_CEILING, tuple([TRAVEL_MAJORITY_EXCEEDS_CEILING] + reasons), facts)

    # incomplete estimates or pending interpretation: never a confirmed pass, never an automatic rejection
    if pref.walk_only_ambiguous:
        reasons.insert(0, TRAVEL_PREFERENCE_MODE_AMBIGUITY)
        facts["pending_interpretation"] = WALK_ONLY_PENDING
    if not complete:
        reasons.insert(0 if not pref.walk_only_ambiguous else 1, TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES)
        with_values = sorted((p for p in known if p.primary.best is not None), key=lambda p: (p.primary.best, p.index))
        if len(with_values) >= k:
            chosen = with_values[:k]
            total = _sum_minutes(p.primary.best for p in chosen)
            facts["upper_bound_diagnostic"] = {
                "label": UPPER_BOUND_LABEL, "indices": [p.index for p in chosen], "size": k, "bound_total_minutes": _fmt(total),
                "bound_average_display": _avg_display(total, k),
                "within_ceiling_if_bounds_hold": total <= k * ceiling, "meets_stated_limit_if_bounds_hold": total <= k * limit,
                "uses_bounds_for": [p.index for p in chosen if p.status == PARTIAL]}
        else:
            reasons.append(TRAVEL_NEEDS_CONFIRMATION_INSUFFICIENT_MEASURED)
    return TravelAssessment(NEEDS_CONFIRMATION, tuple(reasons), facts)
