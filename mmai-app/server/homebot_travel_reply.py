"""The AI's assessment of the final shortlist in one live travel check (H28(i), GP6(d), T4/T6; 16 Nov 2026): Claude receives
the journey results that were just worked out for the shortlisted apartments, together with those apartments' other
points, and gives its assessment of each; the page shows it with its Google Maps sources straight after it.

Two different things, kept apart on purpose:
  * the SAVED AI review (homebot_review) is stored with the options and never receives journey content. It is unchanged.
  * THIS assessment is transient. It is produced inside one travel-check request, returned to the page and dropped. Nothing
    here writes to the database, a file, a cache or a log; a failure is logged as a fixed category only.

What the model decides and what it cannot:
  * every figure is worked out in code first (homebot_travel, unchanged): each person's fastest accepted journey, whether
    it is within that person's stated limit, and the group result (closest-majority average against the doubled limit).
    The model cannot change a figure, pass an apartment that failed, or treat an unknown journey as known (T4(e)).
  * READING CHECK (exact): per option the model reports how many people are within their own limit, over it and not known,
    WHICH people, and the group result. Code checks each against its own figures. Any mismatch withholds everything.
  * JUDGEMENT (the model's own): per option a travel rating, the strengths and weaknesses it thinks matter (chosen from
    a fixed list of statements), and its advice to staff; and one order of preference across the shortlist. Code does
    not make these choices. It only refuses a choice that states something untrue for that option, hides something that
    must be flagged (an unknown journey, a long journey, an alternative), or contradicts itself.
  * the model's own sentences are never shown. An earlier draft let it add a short note and checked the note's numbers and
    phrases; an independent review found realistic wrong sentences that passed. Every sentence on the page is therefore
    written here, from a statement the model chose and code verified, so its judgement is shown without free wording.
  * a person whose journey is not fully measured is "not known": never counted as within the limit and never as over it.
  * "within their own limit" and the group result are different questions and are reported separately.

T6: the travel-checked shortlist is not presented as AI-assessed unless this assessment was produced and accepted, for
every shortlisted apartment including a replacement (see `gate`). A failure never falls back to a rules-only result.

Off unless MMAI_TRAVEL_AI_REPLY is exactly "1", live travel checks are on, and the call would go to ALM's own Anthropic
account with the approved model. Google requires that journey content is not cached by, stored by or used to improve the
model; that is a property of the Anthropic account, which the operator must have checked before setting the switch.
"""
from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction

from . import ai, homebot_match as hm, homebot_travel as tv

log = logging.getLogger("mmai.homebot_travel_reply")

REPLY_ENV = "MMAI_TRAVEL_AI_REPLY"
REPLY_CONTRACT = "homebot-travel-assessment-v2"
MAX_OPTIONS = 12
OUTPUT_TOKENS = 2000

WITHIN, OVER, NEEDS, NONE = "within_doubled_limit", "over_doubled_limit", "needs_confirmation", "no_destinations"
GROUP_RESULT = {tv.PASS: WITHIN, tv.EXCEEDS_CEILING: OVER, tv.NEEDS_CONFIRMATION: NEEDS, tv.NO_DESTINATIONS: NONE}
GROUP_PLAIN = {WITHIN: "within the doubled limit", OVER: "over the doubled limit", NEEDS: "needs confirmation",
               NONE: "no one has a place of work or study recorded yet"}

OFF_TEXT = "The AI travel assessment is switched off."
NOT_OWN_ACCOUNT_TEXT = "The AI travel assessment is not available: it runs only on ALM's own AI account."
NOTHING_TEXT = "No journey could be measured in this check, so there is nothing for the AI to assess."
UNAVAILABLE_TEXT = "The AI travel assessment could not be produced for this check. The journey figures are unaffected."
WITHHELD_TEXT = ("The AI travel assessment was withheld because what it said did not match the journey figures. "
                 "The journey figures are unaffected.")
READY_TEXT = ("The AI assessed these apartments using the Google Maps journey estimates in this check. The travel rating, the "
              "strengths and weaknesses it picked out and the order of preference are its choices, limited to statements that "
              "are true of the figures; some weaknesses must always be listed, and the advice line follows from what is listed "
              "and from the open points. Shown on this page only; not saved.")

# ----------------------------------------------------------------------------------------------------- the judgement
GOOD, ACCEPTABLE, POOR, NOT_KNOWN, NOT_APPLICABLE, FAILS = "good", "acceptable", "poor", "not_known", "not_applicable", "fails"
RATING_PLAIN = {GOOD: "good", ACCEPTABLE: "acceptable", POOR: "poor", NOT_KNOWN: "not known until the missing journeys are confirmed",
                NOT_APPLICABLE: "not applicable (no place of work or study recorded yet)", FAILS: "fails the travel check"}
FULL, ALT_AREA, ALT_BATH = "full_match", "alternative_different_area", "alternative_one_bathroom"
OFFER, OFFER_CAVEATS, CONFIRM_FIRST, DISCUSS_ALT = "offer", "offer_with_caveats", "confirm_first", "discuss_as_alternative"
ADVICE_PLAIN = {OFFER: "Suitable to offer.", OFFER_CAVEATS: "Can be offered, with the weaknesses explained to the customer.",
                CONFIRM_FIRST: "Confirm the open points before offering; it is not yet a fully suitable option.",
                DISCUSS_ALT: "An alternative the party did not ask for: discuss it, do not present it as a match."}
MAX_POINTS = 5


def _apartment(f):
    return f.get("apartment") or {}


def _rents(pk):
    return [Decimal(_apartment(o)["total_weekly_rent"]) for o in pk["options"] if _apartment(o).get("total_weekly_rent") is not None]


def _rent_is(f, pk, pick):
    rents, mine = _rents(pk), _apartment(f).get("total_weekly_rent")
    return mine is not None and len(rents) > 1 and Decimal(mine) == pick(rents) and rents.count(Decimal(mine)) == 1


def _others(f) -> int:
    """People who are NOT within their own limit: over it, not known, or with no place of work or study recorded yet. A
    statement about 'everyone' or 'most people' counts all of them."""
    n = f["counts"]
    return n["over_own_limit"] + n["not_known"] + n.get("no_destination", 0)


def _average_over_stated(f, pk=None) -> bool:
    return f["group"]["result"] == WITHIN and f["group"]["closest_majority_average_within_stated_limit"] is False


# code -> (side, the sentence shown, when it is true). The model chooses among the true ones; it cannot word its own.
POINTS = {
    "everyone_within_own_limit": ("strength", "Everyone's journey is within their own stated travel time.",
                                  lambda f, pk: f["counts"]["within_own_limit"] > 0 and not _others(f)),
    "most_within_own_limit": ("strength", "Most people's journeys are within their own stated travel time.",
                              lambda f, pk: f["counts"]["within_own_limit"] > _others(f) > 0),
    "majority_average_within_stated_limit": ("strength", "The closest majority's average journey is within the stated travel time itself, not only double it.",
                                             lambda f, pk: f["group"]["result"] == WITHIN and f["group"]["closest_majority_average_within_stated_limit"] is True),
    "dates_as_requested": ("strength", "Available for the dates the party asked for.", lambda f, pk: _apartment(f).get("days_from_preferred_dates") == 0),
    "no_empty_days": ("strength", "Leaves no empty days after the stay.", lambda f, pk: _apartment(f).get("future_empty_days") == 0),
    "lowest_rent_of_these": ("strength", "The lowest whole-apartment rent of these options.", lambda f, pk: _rent_is(f, pk, min)),
    "majority_average_over_stated_limit": ("weakness", "The closest majority's average journey is over the stated travel time, though within double it.",
                                           _average_over_stated),
    "some_over_own_limit": ("weakness", "At least one person's journey is over their own stated travel time.", lambda f, pk: f["counts"]["over_own_limit"] > 0),
    "long_journey_for_some": ("weakness", "At least one person has a long journey, over double the stated travel time.", lambda f, pk: bool(f.get("long_journey"))),
    "journeys_not_known": ("weakness", "At least one journey is not known, so travel cannot be confirmed yet.", lambda f, pk: f["counts"]["not_known"] > 0),
    "dates_differ": ("weakness", "The dates differ from the ones the party asked for.", lambda f, pk: (_apartment(f).get("days_from_preferred_dates") or 0) > 0),
    "highest_rent_of_these": ("weakness", "The highest whole-apartment rent of these options.", lambda f, pk: _rent_is(f, pk, max)),
    "other_points_to_confirm": ("weakness", "Other points still need confirming before it can be offered.", lambda f, pk: bool(_apartment(f).get("open_points"))),
    "alternative_not_full_match": ("weakness", "Not a match to what the party asked for: it is an alternative.", lambda f, pk: _apartment(f).get("kind", FULL) != FULL),
}


def _must_flag(f) -> list:
    """Weaknesses the model may not leave out, whatever it thinks of the option (H28(b)/(d), H25)."""
    need = []
    if f["counts"]["not_known"]:
        need.append("journeys_not_known")
    if f["counts"]["over_own_limit"]:
        need.append("some_over_own_limit")           # so "Suitable to offer" can never sit beside a person over their limit
    if _average_over_stated(f):
        need.append("majority_average_over_stated_limit")
    if f.get("long_journey"):
        need.append("long_journey_for_some")
    if _apartment(f).get("kind", FULL) != FULL:
        need.append("alternative_not_full_match")
    return need


def _ratings_allowed(f) -> tuple:
    """The ratings that do not contradict the code's result. Where more than one is left, the choice is the model's."""
    result = f["group"]["result"]
    if result != WITHIN:
        return {OVER: (FAILS,), NEEDS: (NOT_KNOWN,), NONE: (NOT_APPLICABLE,)}[result]
    allowed = [GOOD, ACCEPTABLE, POOR]
    if f.get("long_journey") or f["group"]["closest_majority_average_within_stated_limit"] is False:
        allowed.remove(GOOD)    # a flagged long journey, or an average over the stated time, is not "good"
    if not f["counts"]["over_own_limit"] and f["group"]["closest_majority_average_within_stated_limit"] is True:
        allowed.remove(POOR)    # everyone within their own time and the average within it is not "poor"
    return tuple(allowed)


def _advice_allowed(f, weaknesses) -> tuple:
    if _apartment(f).get("kind", FULL) != FULL:
        return (DISCUSS_ALT,)
    if f["group"]["result"] in (NEEDS, OVER) or _apartment(f).get("open_points"):
        return (CONFIRM_FIRST,)
    return (OFFER_CAVEATS,) if weaknesses else (OFFER,)

SYSTEM = ("You are HomeBot AI, Apartment Living Melbourne's assistant for staff. You are given a final shortlist of apartments "
          "for one party: journey estimates from Google Maps that code has already compared with the party's stated travel "
          "limit, and each apartment's other points. You report the figures faithfully and then give your own assessment of "
          "how suitable each apartment is. You never invent a journey, never treat a journey that is not known as within or "
          "over a limit, never do your own arithmetic on the figures, and never overrule the group result.")


class ReplyMismatch(ai.AIUnavailable):
    """The model's reply does not match the code's figures (or is not in the agreed shape). The reply is withheld."""
    def __init__(self, message: str):
        super().__init__(message, kind="validation", code="validation")


def switch_on() -> bool:
    return os.environ.get(REPLY_ENV, "0") == "1"


def enabled() -> bool:
    """The switch, live travel checks, ALM's own Anthropic account and the approved model: all four, or no call is made."""
    from . import routing
    return switch_on() and routing.live_enabled() and ai.using_own_key() and ai.model_name() == ai.APPROVED_MODEL


# --------------------------------------------------------------------------------------------------- the code's figures
def _exact(value) -> Fraction:
    text = str(value)
    if "/" in text:
        n, d = text.split("/", 1)
        return Fraction(int(n), int(d))
    return Fraction(Decimal(text))


def _shown(value) -> str:
    """Minutes to one decimal place for reading (the comparison itself has already been made on the exact value)."""
    f = _exact(value)
    d = (Decimal(f.numerator) / Decimal(f.denominator)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return format(d.normalize() if d == d.to_integral_value() else d, "f")


def option_figures(candidate: dict) -> dict | None:
    """The code's own travel figures for one option, from its unchanged H28 assessment. None when it has no assessment."""
    travel = candidate.get("travel") or {}
    facts = travel.get("facts") or {}
    if travel.get("status") not in GROUP_RESULT or not isinstance(facts.get("people"), list):
        return None
    limit, ceiling = int(facts["stated_limit_minutes"]), int(facts["ceiling_minutes"])
    people, within, over, unknown, none = [], 0, 0, 0, 0
    for p in facts["people"]:
        if p["status"] == tv.NO_DEST_PERSON:
            none += 1
            people.append({"person": p["index"], "journey": "no_destination"})
        elif p["status"] == tv.MEASURED and p.get("fastest_minutes") is not None:
            ok = _exact(p["fastest_minutes"]) <= limit
            within, over = within + ok, over + (not ok)
            people.append({"person": p["index"], "journey": "measured", "minutes": _shown(p["fastest_minutes"]),
                           "by": list(p.get("fastest_modes") or []), "own_limit": "within" if ok else "over"})
        else:   # partly or wholly unmeasured: not known, never within and never over
            unknown += 1
            people.append({"person": p["index"], "journey": "not_known", "own_limit": "not_known"})
    majority = facts.get("majority") or {}
    return {"apartment_id": int(candidate["apartment_id"]), "stated_limit_minutes": limit, "doubled_limit_minutes": ceiling,
            "people": people, "long_journey": hm.LONG_JOURNEY in (candidate.get("flags") or []),
            "apartment": _other_points(candidate),
            "group": {"result": GROUP_RESULT[travel["status"]], "closest_majority_size": majority.get("size"),
                      "closest_majority_average_minutes": majority.get("average_display"),
                      "closest_majority_average_within_stated_limit": facts.get("meets_stated_limit")},
            "counts": {"within_own_limit": within, "over_own_limit": over, "not_known": unknown, "no_destination": none}}


def _other_points(candidate: dict) -> dict:
    """The apartment's non-travel points the saved options already hold: kind, rent, date fit and what is still open. No
    address, building or name."""
    offer = candidate.get("offer") or {}
    words = lambda codes: [hm.PLAIN.get(k, k) for k in codes]   # noqa: E731
    return {"kind": {hm.ALTERNATIVE_AREA: ALT_AREA, hm.ALTERNATIVE_BATHROOM: ALT_BATH}.get(candidate.get("class"), FULL),
            "total_weekly_rent": offer.get("total_weekly_rent"), "days_from_preferred_dates": offer.get("date_distance_days"),
            "future_empty_days": offer.get("future_empty_days"),
            "open_points": words(r for r in candidate.get("confirmation_reasons") or [] if r != hm.TRAVEL_NEEDS_CONFIRMATION),
            "notes": words(r for r in candidate.get("flags") or [] if r not in (hm.LONG_JOURNEY, hm.NO_DESTINATIONS)),
            "took_the_place_of_one_that_failed_travel": hm.REPLACEMENT_AFTER_TRAVEL in (candidate.get("flags") or [])}


def packet(candidates: list) -> dict:
    """What the model is given: positions, figures and each apartment's other points. No names, addresses or destinations."""
    options, seen = [], set()
    for c in candidates:
        f = option_figures(c)
        if f and f["apartment_id"] not in seen and len(options) < MAX_OPTIONS:
            seen.add(f["apartment_id"])
            options.append(f)
    return {"contract": REPLY_CONTRACT, "source": "Google Maps",
            "basis": "Estimates for a typical weekday 08:00 Melbourne departure; not guaranteed journey times.",
            "options": options}


def build_prompt(pk: dict, compact: bool = False) -> str:
    shown = [{k: v for k, v in o.items() if k != "counts"} for o in pk["options"]]   # the model counts; code checks
    rulesx = [
        "The section below headed GOOGLE MAPS JOURNEY ESTIMATES is the only source for anything you say about travel.",
        "For each option, each person is one of: within their own stated limit, over it, not known, or no destination. These are "
        "already decided in the data (own_limit); do not change them and do not compare minutes yourself.",
        "Count, for each option: within_own_limit, over_own_limit and not_known. People with no destination are in none of the three.",
        "A person who is not known is neither within nor over the limit. Never describe them as meeting or missing it.",
        "group_result is the option's group.result exactly as given. It is a separate question from the own-limit counts: it "
        "compares the average of the closest majority with the doubled limit. Do not present one as the other.",
        "people_over_own_limit: the person numbers that are over their own limit. people_not_known: the person numbers that are not known.",
        "Then give YOUR OWN assessment of each option. This part is your judgement; weigh the journeys together with the "
        "apartment's other points (apartment).",
        "travel_rating: one of " + ", ".join(RATING_PLAIN) + ". Use not_known when group_result is needs_confirmation, "
        "not_applicable when it is no_destinations, fails when it is over_doubled_limit. Otherwise choose good, acceptable or "
        "poor: how well the journeys suit this party, considering how many people are over their own limit, any long journey "
        "and whether the closest majority's average is within the stated time itself.",
        "strengths and weaknesses: choose the statements that matter most for this option, most important first, at most "
        + str(MAX_POINTS) + " in each list, using only these codes and only where true for the option. Strengths: "
        + "; ".join(f"{k} ({v[1]})" for k, v in POINTS.items() if v[0] == "strength") + ". Weaknesses: "
        + "; ".join(f"{k} ({v[1]})" for k, v in POINTS.items() if v[0] == "weakness") + ".",
        "Always include journeys_not_known when a journey is not known, some_over_own_limit when anyone is over their own "
        "limit, majority_average_over_stated_limit when the closest majority's average is over the stated time, "
        "long_journey_for_some when long_journey is true, and alternative_not_full_match when the apartment is an alternative.",
        "advice: discuss_as_alternative for an alternative; otherwise confirm_first when group_result is needs_confirmation or "
        "the apartment has open_points; otherwise offer when you list no weaknesses and offer_with_caveats when you list any.",
        "preferred_order: every apartment_id once, the one you judge most suitable for this party first. Full matches come "
        "before alternatives.",
        "Do not write sentences of your own anywhere. Return ONE JSON object only, with no other text and no other fields: "
        "{\"options\": [{\"apartment_id\": integer, \"within_own_limit\": integer, \"over_own_limit\": integer, \"not_known\": integer, "
        "\"people_over_own_limit\": [integers], \"people_not_known\": [integers], \"group_result\": string, \"travel_rating\": string, "
        "\"strengths\": [codes], \"weaknesses\": [codes], \"advice\": string}], \"preferred_order\": [integers]} with exactly one "
        "options entry per option, in the order given.",
    ]
    if compact:
        rulesx.append("Be brief: the JSON object only.")
    return (SYSTEM + "\n\nRules:\n- " + "\n- ".join(rulesx) + "\n\nGOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps; " + pk["basis"] + ")\n\n"
            + json.dumps({"options": shown}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------------------ validator
READING_KEYS = ("apartment_id", "within_own_limit", "over_own_limit", "not_known", "people_over_own_limit", "people_not_known", "group_result")
JUDGEMENT_KEYS = ("travel_rating", "strengths", "weaknesses", "advice")
REPLY_KEYS = frozenset(READING_KEYS + JUDGEMENT_KEYS)


def _judgement(f: dict, r: dict, pk: dict) -> dict:
    """The model's own assessment of one option, accepted as it chose it unless a choice is untrue for the option, leaves out
    something that must be flagged, or contradicts the code's result or itself."""
    where = f"option {f['apartment_id']}"
    if not isinstance(r["travel_rating"], str) or r["travel_rating"] not in _ratings_allowed(f):
        raise ReplyMismatch(f"{where}: travel rating contradicts the figures")
    chosen = {}
    for side in ("strength", "weakness"):
        codes = r["strengths" if side == "strength" else "weaknesses"]
        if not isinstance(codes, list) or len(codes) > MAX_POINTS or any(not isinstance(k, str) for k in codes) or len(set(codes)) != len(codes):
            raise ReplyMismatch(f"{where}: {side} list is not in the agreed shape")
        for k in codes:
            if k not in POINTS or POINTS[k][0] != side or not POINTS[k][2](f, pk):
                raise ReplyMismatch(f"{where}: {side} is not true for this option")
        chosen[side] = codes
    if any(k not in chosen["weakness"] for k in _must_flag(f)):
        raise ReplyMismatch(f"{where}: a point that must be flagged was left out")
    if not isinstance(r["advice"], str) or r["advice"] not in _advice_allowed(f, chosen["weakness"]):
        raise ReplyMismatch(f"{where}: advice contradicts the option")
    return {"travel_rating": r["travel_rating"], "travel_rating_plain": RATING_PLAIN[r["travel_rating"]],
            "strengths": [POINTS[k][1] for k in chosen["strength"]], "weaknesses": [POINTS[k][1] for k in chosen["weakness"]],
            "strength_codes": chosen["strength"], "weakness_codes": chosen["weakness"],
            "advice": r["advice"], "advice_plain": ADVICE_PLAIN[r["advice"]]}


def _preference(order, pk: dict) -> dict:
    """The model's order of preference: every option once, full matches before alternatives. Apartment id -> place."""
    ids = [o["apartment_id"] for o in pk["options"]]
    if not isinstance(order, list) or any(type(n) is not int for n in order) or sorted(order) != sorted(ids):
        raise ReplyMismatch("order of preference does not cover exactly the options given")
    kinds = {o["apartment_id"]: _apartment(o).get("kind", FULL) != FULL for o in pk["options"]}
    if [kinds[n] for n in order] != sorted(kinds[n] for n in order):
        raise ReplyMismatch("order of preference puts an alternative before a full match")
    return {n: i for i, n in enumerate(order, start=1)}


def _who(f: dict, own_limit: str) -> list:
    return [p["person"] for p in f["people"] if p.get("own_limit") == own_limit]


def _note(f: dict) -> str:
    """The sentence shown for one option. Written here, from figures the model's reply has just been checked against; the
    model's own wording is never shown."""
    over, unknown = _who(f, "over"), _who(f, "not_known")
    parts = []
    if over:
        parts.append("Over their own limit: " + ", ".join(f"person {n}" for n in over) + ".")
    if unknown:
        parts.append("Not known, so neither within nor over a limit: " + ", ".join(f"person {n}" for n in unknown) + ".")
    return " ".join(parts) or "No one is over their own limit and no journey is unknown."


def parse_reply(text: str, pk: dict) -> list:
    """The model's reply, accepted only if every option is present once, in order, with exactly the agreed fields; every
    count, every person listed and the group result equal the code's own; and its judgement (rating, strengths, weaknesses,
    advice, order of preference) states nothing untrue and hides nothing that must be flagged. Raises ReplyMismatch
    otherwise; nothing is partly accepted and no free text from the model is kept."""
    try:
        out = ai._json_from(text)
    except ai.AIUnavailable:
        raise ReplyMismatch("reply is not JSON")
    rows = out.get("options") if isinstance(out, dict) and set(out) == {"options", "preferred_order"} else None
    if not isinstance(rows, list) or len(rows) != len(pk["options"]):
        raise ReplyMismatch("reply does not cover exactly the options given")
    place = _preference(out["preferred_order"], pk)
    clean = []
    for f, r in zip(pk["options"], rows):
        if not isinstance(r, dict) or set(r) != REPLY_KEYS or type(r["apartment_id"]) is not int or r["apartment_id"] != f["apartment_id"]:
            raise ReplyMismatch("reply entry is not for the option given")
        where = f"option {f['apartment_id']}"
        for key in ("within_own_limit", "over_own_limit", "not_known"):
            if type(r[key]) is not int or r[key] != f["counts"][key]:
                raise ReplyMismatch(f"{where}: {key} does not match")
        for key, own_limit in (("people_over_own_limit", "over"), ("people_not_known", "not_known")):
            named = r[key]
            if not isinstance(named, list) or any(type(n) is not int for n in named) or len(set(named)) != len(named) \
                    or sorted(named) != sorted(_who(f, own_limit)):
                raise ReplyMismatch(f"{where}: {key} does not match")
        if r["group_result"] != f["group"]["result"]:
            raise ReplyMismatch(f"{where}: group result does not match")
        clean.append({"apartment_id": f["apartment_id"], **f["counts"], "stated_limit_minutes": f["stated_limit_minutes"],
                      "doubled_limit_minutes": f["doubled_limit_minutes"], "group_result": f["group"]["result"],
                      "group_plain": GROUP_PLAIN[f["group"]["result"]], "note": _note(f),
                      **_judgement(f, r, pk), "preference": place[f["apartment_id"]], "of": len(place)})
    return clean


def travel_reply(candidates: list, meter: dict | None = None) -> dict:
    """The transient reply for one travel check. Never raises and never stores: any failure becomes a fixed sentence."""
    if not switch_on():
        return {"state": "off", "saved": False, "plain": OFF_TEXT, "options": []}
    if not enabled():
        return {"state": "off", "saved": False, "plain": NOT_OWN_ACCOUNT_TEXT, "options": []}
    pk = packet(candidates)
    if not any(p["journey"] == "measured" for o in pk["options"] for p in o["people"]):
        return {"state": "nothing", "saved": False, "plain": NOTHING_TEXT, "options": []}
    try:
        text = ai._ask_bounded(lambda compact: build_prompt(pk, compact), OUTPUT_TOKENS, "homebot travel reply", meter)
        options = parse_reply(text, pk)
    except ReplyMismatch:
        log.warning("travel reply withheld: validation")   # category only: the model's words may quote journey figures
        return {"state": "withheld", "saved": False, "plain": WITHHELD_TEXT, "options": []}
    except ai.AIUnavailable as e:
        log.warning("travel reply not produced: %s", ai.failure_kind(e))
        return {"state": "unavailable", "saved": False, "plain": UNAVAILABLE_TEXT, "options": []}
    except Exception:   # noqa: BLE001 - a travel check must never fail because of the AI note
        log.warning("travel reply not produced: unexpected")
        return {"state": "unavailable", "saved": False, "plain": UNAVAILABLE_TEXT, "options": []}
    return {"state": "ready", "saved": False, "plain": READY_TEXT, "model": ai.APPROVED_MODEL, "options": options}


# ------------------------------------------------------------------------------------------------------------ T6 gate
ASSESSED_TEXT = ("AI-assessed: the saved AI review covered and agreed with every apartment shown here, and the AI has assessed "
                 "each of them with the journey results of this check. Reservations still reviews it before contacting the customer.")
HELD_PREFIX = "Held, not a ready shortlist: "
HELD_REASON = {"off": "the AI travel assessment is switched off, so the journey results have not been assessed by the AI.",
               "nothing": "no journey could be measured, so there was nothing for the AI to assess.",
               "unavailable": "the AI travel assessment could not be produced.",
               "withheld": "the AI travel assessment did not match the journey figures and was withheld.",
               "incomplete": "the AI travel assessment does not cover every apartment shown.",
               "review": "the saved AI review of these options has not completed.",
               "review_hold": "the saved AI review of these options ended in a hold for staff.",
               "disagreed": "the saved AI review did not agree with an apartment shown here.",
               "replacement": "an apartment shown here took the place of one that failed the travel check and was not among "
                              "the apartments the saved AI review covered, so it has not been reviewed.",
               "empty": "no apartment is left to assess."}


def saved_review(state: dict) -> tuple:
    """(state, the apartments the saved AI review agreed with, the ones it covered but did not agree with) from the shortlist
    module's review state. 'reviewed' only when that review finished with the verdict approve; a finished review that ended
    in a hold is 'review_hold'."""
    name = state.get("state") if isinstance(state, dict) else None
    if name != "reviewed":
        return name, set(), set()
    detail = state.get("detail")
    if not isinstance(detail, dict):
        return None, set(), set()
    notes = [n for n in detail.get("option_notes") or [] if isinstance(n, dict) and n.get("apartment_id") is not None]
    agreed = {int(n["apartment_id"]) for n in notes if n.get("agrees") is True}
    return ("reviewed" if detail.get("verdict") == "approve" else "review_hold"), agreed, {int(n["apartment_id"]) for n in notes} - agreed


def gate(reply: dict, saved_review_state: str, shortlisted: list, reviewed_ids, disagreed_ids=()) -> dict:
    """T6 for the travel-checked shortlist: it is AI-assessed only when the saved AI review finished with the verdict approve,
    EVERY apartment shown is one that review covered and agreed with (`reviewed_ids`: the saved options and the reserves
    next in order), AND this check's assessment was produced, accepted and covers every apartment shown. A replacement is
    reviewed only if it was one of those reserves; this check's travel assessment alone is never a review of an apartment.
    Anything else is held, with the reason; the journey figures stay visible. There is no rules-only way through."""
    reviewed, disagreed = {int(i) for i in (reviewed_ids or ())}, {int(i) for i in (disagreed_ids or ())}
    ids = [int(x["apartment_id"]) for x in shortlisted]
    if not ids:
        why = "empty"
    elif reply.get("state") != "ready":
        why = reply.get("state") if reply.get("state") in HELD_REASON else "unavailable"
    elif sorted(o["apartment_id"] for o in reply.get("options") or []) != sorted(ids):
        why = "incomplete"
    elif saved_review_state != "reviewed":
        why = "review_hold" if saved_review_state == "review_hold" else "review"
    elif any(i in disagreed for i in ids):
        why = "disagreed"
    elif any(i not in reviewed for i in ids):
        why = "replacement"
    else:
        return {"state": "assessed", "held": False, "plain": ASSESSED_TEXT}
    return {"state": "held", "held": True, "reason": why, "plain": HELD_PREFIX + HELD_REASON[why]}
