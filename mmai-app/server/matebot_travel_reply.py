"""The AI's assessment of a MateBot group's final shortlist in one live travel check (H28(i), GP6(d), M4, T4/T6; 16 Nov 2026).

The MateBot counterpart of homebot_travel_reply, with the same division of work and the same safeguards:
  * every figure is worked out in code first (matebot_travel): each person's fastest journey by a choice THEY accepted, that
    journey against their OWN stated time, and the group result. The model cannot change a figure, pass an apartment that
    failed, or treat an unknown journey as known (T4(e)).
  * READING CHECK (exact): per apartment the model reports how many people are within their own stated time, over it, not
    known and without a stated time, WHICH people are over or not known, and the group result. Code checks each against its
    own figures. Any mismatch withholds everything.
  * JUDGEMENT (the model's own): a travel rating, the strengths and weaknesses it thinks matter (chosen from a fixed list of
    statements), its advice to staff, and one order of preference. Code only refuses a choice that is untrue for the
    apartment, hides something that must be flagged, or contradicts the result or itself.
  * the model's own sentences are never shown: every sentence on the page is written here.
  * transient: produced inside one travel-check request, returned to the page and dropped. Nothing here writes to the
    database, a file, a cache or a log; a failure is logged as a fixed category only. It is never the group's saved review.

What differs from HomeBot, and why. A MateBot group's members each give their own maximum travel time, and it is optional.
When the stated times differ or one is missing there is no approved way to combine them (register, "MateBot group travel
threshold", not yet ruled), so the code's group result is "needs confirmation" and the model is told exactly that. It may
not rate such an apartment's travel as good, acceptable or poor, and may not advise offering it without confirmation.

The model is given positions and figures only: no names, addresses, institutions or destinations.

Off unless homebot_travel_reply.enabled() (the MMAI_TRAVEL_AI_REPLY switch, live travel checks on, ALM's own Anthropic
account and the approved model). There is no rules-only way through `gate`.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from . import ai, homebot_travel as ht, homebot_travel_reply as tr, matebot_travel as mt

log = logging.getLogger("mmai.matebot_travel_reply")

REPLY_CONTRACT = "matebot-travel-assessment-v1"
MAX_OPTIONS = mt.MAX_SHORTLIST
OUTPUT_TOKENS = 1200

WITHIN, OVER, NEEDS, NONE = tr.WITHIN, tr.OVER, tr.NEEDS, tr.NONE
GROUP_RESULT = {mt.PASS: WITHIN, mt.EXCEEDS_CEILING: OVER, mt.NEEDS_CONFIRMATION: NEEDS, mt.NO_DESTINATIONS: NONE}
GROUP_PLAIN = {WITHIN: "within the doubled limit", OVER: "over the doubled limit", NEEDS: "needs confirmation",
               NONE: "no member has a place of study recorded that a journey can be measured to"}
OWN = {mt.WITHIN_OWN_LIMIT: "within", mt.OVER_OWN_LIMIT: "over", mt.NOT_MEASURED: "not_known", mt.NO_STATED_LIMIT: "no_stated_time"}
JOURNEY = {mt.MEASURED: "measured", mt.PARTIAL: "partly_measured", mt.UNMEASURED: "not_known", mt.NO_MODE_ANSWER: "travel_choice_not_answered"}
WHY = {mt.TRAVEL_LIMITS_DIFFER: "stated_times_differ", mt.TRAVEL_LIMIT_MISSING: "stated_time_missing",
       mt.TRAVEL_MODE_NOT_ANSWERED: "travel_choice_not_answered",
       ht.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES: "journeys_not_known", ht.FASTEST_MODE_UNCONFIRMED: "fastest_choice_not_confirmed"}

OFF_TEXT, NOT_OWN_ACCOUNT_TEXT, NOTHING_TEXT = tr.OFF_TEXT, tr.NOT_OWN_ACCOUNT_TEXT, tr.NOTHING_TEXT
UNAVAILABLE_TEXT, WITHHELD_TEXT = tr.UNAVAILABLE_TEXT, tr.WITHHELD_TEXT
READY_TEXT = ("The AI assessed these apartments using the Google Maps journey estimates in this check. The travel rating, the "
              "strengths and weaknesses it picked out and the order of preference are its choices, limited to statements that "
              "are true of the figures; some weaknesses must always be listed, and the advice line follows from what is listed "
              "and from the open points. Shown on this page only; not saved.")

GOOD, ACCEPTABLE, POOR, NOT_KNOWN, NOT_APPLICABLE, FAILS = tr.GOOD, tr.ACCEPTABLE, tr.POOR, tr.NOT_KNOWN, tr.NOT_APPLICABLE, tr.FAILS
RATING_PLAIN = {GOOD: "good", ACCEPTABLE: "acceptable", POOR: "poor", NOT_KNOWN: "not known: the group travel result needs confirmation",
                NOT_APPLICABLE: "not applicable (no place of study or work recorded)", FAILS: "fails the travel check"}
OFFER, OFFER_CAVEATS, CONFIRM_FIRST = tr.OFFER, tr.OFFER_CAVEATS, tr.CONFIRM_FIRST
ADVICE_PLAIN = {OFFER: "Suitable to offer.", OFFER_CAVEATS: "Can be offered, with the weaknesses explained to the group.",
                CONFIRM_FIRST: "Confirm the open points before offering; it is not yet a confirmed suitable option."}
MAX_POINTS = 5

ReplyMismatch = tr.ReplyMismatch


def _apartment(f):
    return f.get("apartment") or {}


def _rents(pk):
    return [Decimal(str(_apartment(o)["total_weekly_rent"])) for o in pk["options"] if _apartment(o).get("total_weekly_rent") is not None]


def _rent_is(f, pk, pick):
    rents, mine = _rents(pk), _apartment(f).get("total_weekly_rent")
    return mine is not None and len(rents) > 1 and Decimal(str(mine)) == pick(rents) and rents.count(Decimal(str(mine))) == 1


def _others(f) -> int:
    """People with a known destination who are NOT within their own stated time: over it, not known, or with no stated time."""
    n = f["counts"]
    return n["over_own_limit"] + n["not_known"] + n["no_stated_limit"]


def _average_over_stated(f, pk=None) -> bool:
    return f["group"]["result"] == WITHIN and f["group"]["closest_majority_average_within_stated_limit"] is False


def _journey_unknown(f, pk=None) -> bool:
    return any(p["journey"] in ("partly_measured", "not_known", "travel_choice_not_answered") for p in f["people"])


def _times_unsettled(f, pk=None) -> bool:
    return f["group"]["result"] != NONE and f["group"]["same_stated_limit"] is False


# code -> (side, the sentence shown, when it is true). The model chooses among the true ones; it cannot word its own.
POINTS = {
    "everyone_within_own_limit": ("strength", "Every member with a known destination has a journey within their own stated travel time.",
                                  lambda f, pk: f["counts"]["within_own_limit"] > 0 and not _others(f)),
    "most_within_own_limit": ("strength", "Most members with a known destination have a journey within their own stated travel time.",
                              lambda f, pk: f["counts"]["within_own_limit"] > _others(f) > 0),
    "majority_average_within_stated_limit": ("strength", "The closest majority's average journey is within the stated travel time itself, not only double it.",
                                             lambda f, pk: f["group"]["result"] == WITHIN and f["group"]["closest_majority_average_within_stated_limit"] is True),
    "lowest_rent_of_these": ("strength", "The lowest whole-apartment rent of these apartments.", lambda f, pk: _rent_is(f, pk, min)),
    "majority_average_over_stated_limit": ("weakness", "The closest majority's average journey is over the stated travel time, though within double it.",
                                           _average_over_stated),
    "some_over_own_limit": ("weakness", "At least one member's journey is over their own stated travel time.", lambda f, pk: f["counts"]["over_own_limit"] > 0),
    "long_journey_for_some": ("weakness", "At least one member has a long journey, over double the stated travel time.", lambda f, pk: bool(f.get("long_journey"))),
    "journeys_not_known": ("weakness", "At least one member's journey is not fully known, so travel cannot be confirmed yet.", _journey_unknown),
    "stated_times_differ_or_missing": ("weakness", "The members' maximum travel times differ or are missing, so no group travel result is calculated.",
                                       _times_unsettled),
    "no_destination_for_some": ("weakness", "At least one member has no place of study or work of their own recorded.",
                                lambda f, pk: f["counts"]["no_destination"] > 0),
    "highest_rent_of_these": ("weakness", "The highest whole-apartment rent of these apartments.", lambda f, pk: _rent_is(f, pk, max)),
    "other_points_to_confirm": ("weakness", "Other points still need confirming before it can be offered.", lambda f, pk: bool(_apartment(f).get("open_points"))),
}


def _must_flag(f) -> list:
    """Weaknesses the model may not leave out, whatever it thinks of the apartment (H28(b)/(d), and the unruled threshold)."""
    need = []
    if _journey_unknown(f):
        need.append("journeys_not_known")
    if f["counts"]["over_own_limit"]:
        need.append("some_over_own_limit")
    if _times_unsettled(f):
        need.append("stated_times_differ_or_missing")
    if _average_over_stated(f):
        need.append("majority_average_over_stated_limit")
    if f.get("long_journey"):
        need.append("long_journey_for_some")
    if _apartment(f).get("open_points"):
        need.append("other_points_to_confirm")
    return need


def _ratings_allowed(f) -> tuple:
    result = f["group"]["result"]
    if result != WITHIN:
        return {OVER: (FAILS,), NEEDS: (NOT_KNOWN,), NONE: (NOT_APPLICABLE,)}[result]
    allowed = [GOOD, ACCEPTABLE, POOR]
    if f.get("long_journey") or f["group"]["closest_majority_average_within_stated_limit"] is False:
        allowed.remove(GOOD)
    if not f["counts"]["over_own_limit"] and f["group"]["closest_majority_average_within_stated_limit"] is True:
        allowed.remove(POOR)
    return tuple(allowed)


def _advice_allowed(f, weaknesses) -> tuple:
    if f["group"]["result"] in (NEEDS, OVER) or _apartment(f).get("open_points"):
        return (CONFIRM_FIRST,)
    return (OFFER_CAVEATS,) if weaknesses else (OFFER,)


SYSTEM = ("You are MateBot AI, Apartment Living Melbourne's assistant for staff. You are given the final shortlist of apartments "
          "for one group of housemates: journey estimates from Google Maps that code has already compared with each member's own "
          "stated travel time, the group result, and each apartment's other points. You report the figures faithfully and then "
          "give your own assessment of how suitable each apartment is. You never invent a journey, never treat a journey that is "
          "not known as within or over a time, never do your own arithmetic on the figures, never combine members' different "
          "stated times yourself, and never overrule the group result.")


# --------------------------------------------------------------------------------------------------- the code's figures
def option_figures(option: dict) -> dict | None:
    """The code's own travel figures for one apartment, from its matebot_travel assessment. None when it has none."""
    travel = option.get("travel") or {}
    facts = travel.get("facts") or {}
    if travel.get("status") not in GROUP_RESULT or not isinstance(facts.get("people"), list):
        return None
    people = []
    counts = {"within_own_limit": 0, "over_own_limit": 0, "not_known": 0, "no_stated_limit": 0, "no_destination": 0}
    for p in facts["people"]:
        if p["status"] == mt.NO_DESTINATION:
            counts["no_destination"] += 1
            people.append({"person": p["index"], "journey": "no_destination"})
            continue
        own = OWN[p["own_limit"]]
        counts[{"within": "within_own_limit", "over": "over_own_limit", "not_known": "not_known", "no_stated_time": "no_stated_limit"}[own]] += 1
        row = {"person": p["index"], "journey": JOURNEY[p["status"]], "own_stated_time_minutes": p["stated_limit_minutes"], "own_limit": own}
        if p["status"] == mt.MEASURED:
            row.update(minutes=tr._shown(p["fastest_minutes"]), by=list(p["fastest_modes"]))
        elif p["status"] == mt.PARTIAL:
            row.update(minutes_at_most=tr._shown(p["bound_minutes"]), by=list(p["best_known_modes"]), choices_not_measured=list(p["unknown_modes"]))
        people.append(row)
    majority = facts.get("majority") or {}
    apartment = option.get("apartment") or {}
    return {"apartment_id": int(option["apartment_id"]), "people": people,
            "long_journey": any(p.get("warning") for p in facts["people"]),
            "apartment": {"total_weekly_rent": apartment.get("total_weekly_rent"), "open_points": list(apartment.get("open_points") or []),
                          "took_the_place_of_one_that_failed_travel": bool(apartment.get("replacement"))},
            "group": {"result": GROUP_RESULT[travel["status"]], "same_stated_limit": bool(facts.get("same_stated_limit")),
                      "stated_limit_minutes": facts.get("stated_limit_minutes"), "doubled_limit_minutes": facts.get("ceiling_minutes"),
                      "why_needs_confirmation": [WHY[r] for r in travel.get("reasons") or [] if r in WHY] if travel["status"] == mt.NEEDS_CONFIRMATION else [],
                      "closest_majority_size": majority.get("size"), "closest_majority_average_minutes": majority.get("average_display"),
                      "closest_majority_average_within_stated_limit": facts.get("meets_stated_limit")},
            "counts": counts}


def packet(options: list) -> dict:
    """What the model is given: positions, figures and each apartment's other points. No names, addresses or destinations."""
    out, seen = [], set()
    for o in options:
        f = option_figures(o)
        if f and f["apartment_id"] not in seen and len(out) < MAX_OPTIONS:
            seen.add(f["apartment_id"])
            out.append(f)
    return {"contract": REPLY_CONTRACT, "source": "Google Maps",
            "basis": "Estimates for a typical weekday 08:00 Melbourne departure; not guaranteed journey times.", "options": out}


def build_prompt(pk: dict, compact: bool = False) -> str:
    shown = [{k: v for k, v in o.items() if k != "counts"} for o in pk["options"]]   # the model counts; code checks
    rulesx = [
        "The section below headed GOOGLE MAPS JOURNEY ESTIMATES is the only source for anything you say about travel.",
        "Each member gave their own maximum travel time, or none. For each apartment, each person with a destination is one of: within "
        "their own stated time, over it, not known, or no stated time. These are already decided in the data (own_limit); do not "
        "change them and do not compare minutes yourself. A person whose journey is no_destination is in none of the four.",
        "Count, for each apartment: within_own_limit, over_own_limit, not_known and no_stated_limit.",
        "A person who is not known, or has no stated time, is neither within nor over a time. Never describe them as meeting or missing it.",
        "group_result is the apartment's group.result exactly as given. It is a separate question from the own-time counts. When the "
        "members' stated times differ or one is missing, no rule for combining them has been approved, so the result is "
        "needs_confirmation (see group.why_needs_confirmation). Do not work out a group result of your own.",
        "people_over_own_limit: the person numbers that are over their own stated time. people_not_known: the person numbers whose own_limit is not_known.",
        "Then give YOUR OWN assessment of each apartment. This part is your judgement; weigh the journeys together with the apartment's other points (apartment).",
        "travel_rating: one of " + ", ".join(RATING_PLAIN) + ". Use not_known when group_result is needs_confirmation, not_applicable "
        "when it is no_destinations, fails when it is over_doubled_limit. Otherwise choose good, acceptable or poor: how well the "
        "journeys suit this group, considering how many people are over their own stated time, any long journey and whether the "
        "closest majority's average is within the stated time itself.",
        "strengths and weaknesses: choose the statements that matter most for this apartment, most important first, at most "
        + str(MAX_POINTS) + " in each list, using only these codes and only where true for the apartment. Strengths: "
        + "; ".join(f"{k} ({v[1]})" for k, v in POINTS.items() if v[0] == "strength") + ". Weaknesses: "
        + "; ".join(f"{k} ({v[1]})" for k, v in POINTS.items() if v[0] == "weakness") + ".",
        "Always include journeys_not_known when any person's journey is partly_measured, not_known or travel_choice_not_answered; "
        "some_over_own_limit when anyone is over their own stated time; stated_times_differ_or_missing when group.same_stated_limit is "
        "false and group_result is not no_destinations; majority_average_over_stated_limit when the closest majority's average is over "
        "the stated time; long_journey_for_some when long_journey is true; other_points_to_confirm when the apartment has open_points.",
        "advice: confirm_first when group_result is needs_confirmation or over_doubled_limit, or the apartment has open_points; otherwise "
        "offer when you list no weaknesses and offer_with_caveats when you list any.",
        "preferred_order: every apartment_id once, the one you judge most suitable for this group first.",
        "Do not write sentences of your own anywhere. Return ONE JSON object only, with no other text and no other fields: "
        "{\"options\": [{\"apartment_id\": integer, \"within_own_limit\": integer, \"over_own_limit\": integer, \"not_known\": integer, "
        "\"no_stated_limit\": integer, \"people_over_own_limit\": [integers], \"people_not_known\": [integers], \"group_result\": string, "
        "\"travel_rating\": string, \"strengths\": [codes], \"weaknesses\": [codes], \"advice\": string}], \"preferred_order\": [integers]} "
        "with exactly one options entry per apartment, in the order given.",
    ]
    if compact:
        rulesx.append("Be brief: the JSON object only.")
    return ("Rules:\n- " + "\n- ".join(rulesx) + "\n\nGOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps; " + pk["basis"] + ")\n\n"
            + json.dumps({"options": shown}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------------------ validator
COUNT_KEYS = ("within_own_limit", "over_own_limit", "not_known", "no_stated_limit")
READING_KEYS = ("apartment_id",) + COUNT_KEYS + ("people_over_own_limit", "people_not_known", "group_result")
JUDGEMENT_KEYS = ("travel_rating", "strengths", "weaknesses", "advice")
REPLY_KEYS = frozenset(READING_KEYS + JUDGEMENT_KEYS)


def _judgement(f: dict, r: dict, pk: dict) -> dict:
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
    ids = [o["apartment_id"] for o in pk["options"]]
    if not isinstance(order, list) or any(type(n) is not int for n in order) or sorted(order) != sorted(ids):
        raise ReplyMismatch("order of preference does not cover exactly the options given")
    return {n: i for i, n in enumerate(order, start=1)}


def _who(f: dict, own_limit: str) -> list:
    return [p["person"] for p in f["people"] if p.get("own_limit") == own_limit]


def _note(f: dict) -> str:
    """The sentence shown for one apartment, written here from figures the reply has just been checked against."""
    parts = []
    for own, lead in (("over", "Over their own stated time: "), ("not_known", "Not known, so neither within nor over a time: "),
                      ("no_stated_time", "No stated time to compare with: ")):
        who = _who(f, own)
        if who:
            parts.append(lead + ", ".join(f"person {n}" for n in who) + ".")
    return " ".join(parts) or "No one is over their own stated time and no journey is unknown."


def parse_reply(text: str, pk: dict) -> list:
    """Accepted only if every apartment is present once, in order, with exactly the agreed fields; every count, every person
    listed and the group result equal the code's own; and the judgement states nothing untrue and hides nothing that must be
    flagged. Raises ReplyMismatch otherwise; nothing is partly accepted and no free text from the model is kept."""
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
        for key in COUNT_KEYS:
            if type(r[key]) is not int or r[key] != f["counts"][key]:
                raise ReplyMismatch(f"{where}: {key} does not match")
        for key, own_limit in (("people_over_own_limit", "over"), ("people_not_known", "not_known")):
            named = r[key]
            if not isinstance(named, list) or any(type(n) is not int for n in named) or len(set(named)) != len(named) \
                    or sorted(named) != sorted(_who(f, own_limit)):
                raise ReplyMismatch(f"{where}: {key} does not match")
        if r["group_result"] != f["group"]["result"]:
            raise ReplyMismatch(f"{where}: group result does not match")
        clean.append({"apartment_id": f["apartment_id"], **f["counts"], "same_stated_limit": f["group"]["same_stated_limit"],
                      "stated_limit_minutes": f["group"]["stated_limit_minutes"], "doubled_limit_minutes": f["group"]["doubled_limit_minutes"],
                      "group_result": f["group"]["result"], "group_plain": GROUP_PLAIN[f["group"]["result"]], "note": _note(f),
                      **_judgement(f, r, pk), "preference": place[f["apartment_id"]], "of": len(place)})
    return clean


def travel_reply(options: list, meter: dict | None = None) -> dict:
    """The transient reply for one travel check. Never raises and never stores: any failure becomes a fixed sentence."""
    if not tr.switch_on():
        return {"state": "off", "saved": False, "plain": OFF_TEXT, "options": []}
    if not tr.enabled():
        return {"state": "off", "saved": False, "plain": NOT_OWN_ACCOUNT_TEXT, "options": []}
    pk = packet(options)
    if not any(p["journey"] in ("measured", "partly_measured") for o in pk["options"] for p in o["people"]):
        return {"state": "nothing", "saved": False, "plain": NOTHING_TEXT, "options": []}
    try:
        text = ai._ask_bounded(lambda compact: build_prompt(pk, compact), OUTPUT_TOKENS, "matebot travel reply", meter, system=SYSTEM)
        clean = parse_reply(text, pk)
    except ReplyMismatch:
        log.warning("travel reply withheld: validation")   # category only: the model's words may quote journey figures
        return {"state": "withheld", "saved": False, "plain": WITHHELD_TEXT, "options": []}
    except ai.AIUnavailable as e:
        log.warning("travel reply not produced: %s", ai.failure_kind(e))
        return {"state": "unavailable", "saved": False, "plain": UNAVAILABLE_TEXT, "options": []}
    except Exception:   # noqa: BLE001 - a travel check must never fail because of the AI note
        log.warning("travel reply not produced: unexpected")
        return {"state": "unavailable", "saved": False, "plain": UNAVAILABLE_TEXT, "options": []}
    return {"state": "ready", "saved": False, "plain": READY_TEXT, "model": ai.APPROVED_MODEL, "options": clean}


# ------------------------------------------------------------------------------------------------------------ T6 gate
ASSESSED_TEXT = ("AI-assessed: the group's saved AI review is current and covered every apartment shown here, and the AI has "
                 "assessed each of them with the journey results of this check. Reservations still reviews it before contacting anyone.")
HELD_PREFIX = "Held, not a ready shortlist: "
HELD_REASON = {"off": "the AI travel assessment is switched off, so the journey results have not been assessed by the AI.",
               "nothing": "no journey could be measured, so there was nothing for the AI to assess.",
               "unavailable": "the AI travel assessment could not be produced.",
               "withheld": "the AI travel assessment did not match the journey figures and was withheld.",
               "incomplete": "the AI travel assessment does not cover every apartment shown.",
               "review": "the group does not have a current, approved AI review.",
               "not_reviewed": "an apartment shown here was not among the apartments the group's saved AI review covered, so it has "
                               "not been reviewed; staff decide how to proceed.",
               "empty": "no apartment is left to assess."}


def gate(reply: dict, review_ready: bool, shortlisted_ids, reviewed_ids) -> dict:
    """T6 for the travel-checked shortlist: AI-assessed only when the group's saved review is current and approved
    (`review_ready`, from ai_queue.is_ready), EVERY apartment shown is one that review covered (`reviewed_ids`), AND this
    check's assessment was produced, accepted and covers every apartment shown. Anything else is held, with the reason; the
    journey figures stay visible. This check's travel assessment alone is never a review of an apartment, and there is no
    rules-only way through."""
    reviewed = {int(i) for i in (reviewed_ids or ())}
    ids = [int(i) for i in shortlisted_ids]
    if not ids:
        why = "empty"
    elif reply.get("state") != "ready":
        why = reply.get("state") if reply.get("state") in HELD_REASON else "unavailable"
    elif sorted(o["apartment_id"] for o in reply.get("options") or []) != sorted(ids):
        why = "incomplete"
    elif review_ready is not True:
        why = "review"
    elif any(i not in reviewed for i in ids):
        why = "not_reviewed"
    else:
        return {"state": "assessed", "held": False, "plain": ASSESSED_TEXT}
    return {"state": "held", "held": True, "reason": why, "plain": HELD_PREFIX + HELD_REASON[why]}
