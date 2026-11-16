"""The AI's travel reply for one live travel check (H28(i), 16 Nov 2026): Claude receives the journey results that were just
worked out and reports its assessment of each option; the page shows it with its Google Maps sources straight after it.

Two different things, kept apart on purpose:
  * the SAVED AI review (homebot_review) is stored with the options and never receives journey content. It is unchanged.
  * THIS reply is transient. It is produced inside one travel-check request, returned to the page and dropped. Nothing here
    writes to the database, a file, a cache or a log; a failure is logged as a fixed category only.

What the model may and may not decide:
  * every figure is worked out in code first (homebot_travel, unchanged): each person's fastest accepted journey, whether
    it is within that person's stated limit, and the group result (closest-majority average against the doubled limit).
  * the model answers in fixed fields only, per option: how many people are within their own limit, how many are over it and
    how many are not known; WHICH people are over it and which are not known; and the group result. Code checks every one
    of those against its own figures. Any mismatch withholds the whole reply.
  * the model's own sentences are never shown. An earlier draft let it add a short note and checked the note's numbers and
    phrases; an independent review found realistic wrong sentences that passed, so the note is now written by code from
    the fields that were verified. Nothing reaches the page that code has not checked exactly.
  * a person whose journey is not fully measured is "not known": never counted as within the limit and never as over it.
  * "within their own limit" and the group result are different questions and are reported separately: several people can be
    over their own limit while the group result is within the doubled limit, and the reverse.

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

from . import ai, homebot_travel as tv

log = logging.getLogger("mmai.homebot_travel_reply")

REPLY_ENV = "MMAI_TRAVEL_AI_REPLY"
REPLY_CONTRACT = "homebot-travel-reply-v1"
MAX_OPTIONS = 12
OUTPUT_TOKENS = 1500

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
READY_TEXT = ("The AI read the Google Maps journey estimates in this check. Each count, each person named and the group result "
              "below is what it reported, and each was checked against the journey figures. Shown on this page only; not saved.")

SYSTEM = ("You are HomeBot AI, Apartment Living Melbourne's assistant for staff. You are given journey estimates from Google "
          "Maps that code has already compared with each party's stated travel limit. You report them faithfully. You never "
          "invent a journey, never treat a journey that is not known as within or over a limit, and never do your own arithmetic "
          "on the figures.")


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
            "people": people,
            "group": {"result": GROUP_RESULT[travel["status"]], "closest_majority_size": majority.get("size"),
                      "closest_majority_average_minutes": majority.get("average_display"),
                      "closest_majority_average_within_stated_limit": facts.get("meets_stated_limit")},
            "counts": {"within_own_limit": within, "over_own_limit": over, "not_known": unknown, "no_destination": none}}


def packet(candidates: list) -> dict:
    """What the model is given: positions and figures only. No names, addresses, destinations or apartment details."""
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
        "Return ONE JSON object only, with no other text and no other fields: {\"options\": [{\"apartment_id\": integer, "
        "\"within_own_limit\": integer, \"over_own_limit\": integer, \"not_known\": integer, \"people_over_own_limit\": [integers], "
        "\"people_not_known\": [integers], \"group_result\": string}]} with exactly one entry per option, in the order given.",
    ]
    if compact:
        rulesx.append("Be brief: the JSON object only.")
    return (SYSTEM + "\n\nRules:\n- " + "\n- ".join(rulesx) + "\n\nGOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps; " + pk["basis"] + ")\n\n"
            + json.dumps({"options": shown}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------------------ validator
REPLY_KEYS = frozenset(("apartment_id", "within_own_limit", "over_own_limit", "not_known", "people_over_own_limit",
                        "people_not_known", "group_result"))


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
    """The model's reply, accepted only if every option is present once, in order, with exactly the agreed fields, and every
    count, every person listed and the group result equal the code's own. Raises ReplyMismatch otherwise; nothing is partly
    accepted and no free text from the model is kept."""
    try:
        out = ai._json_from(text)
    except ai.AIUnavailable:
        raise ReplyMismatch("reply is not JSON")
    rows = out.get("options") if isinstance(out, dict) and set(out) == {"options"} else None
    if not isinstance(rows, list) or len(rows) != len(pk["options"]):
        raise ReplyMismatch("reply does not cover exactly the options given")
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
                      "group_plain": GROUP_PLAIN[f["group"]["result"]], "note": _note(f)})
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
