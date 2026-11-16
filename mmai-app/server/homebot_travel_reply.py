"""The AI's travel reply for one live travel check (H28(i), 16 Nov 2026): Claude receives the journey results that were just
worked out and writes a short assessment of each option; the page shows it with its Google Maps sources straight after it.

Two different things, kept apart on purpose:
  * the SAVED AI review (homebot_review) is stored with the options and never receives journey content. It is unchanged.
  * THIS reply is transient. It is produced inside one travel-check request, returned to the page and dropped. Nothing here
    writes to the database, a file, a cache or a log; a failure is logged as a fixed category only.

What the model may and may not decide:
  * every figure is worked out in code first (homebot_travel, unchanged): each person's fastest accepted journey, whether
    it is within that person's stated limit, and the group result (closest-majority average against the doubled limit).
  * the model states, per option, how many people are within their own limit, how many are over it and how many are not
    known, the group result, and a short note. Code then checks every count and the group result against its own figures,
    and every number in the note against the figures for that option. Any mismatch withholds the whole reply.
  * a person whose journey is not fully measured is "not known": never counted as within the limit and never as over it.
  * "within their own limit" and the group result are different questions and are reported separately: several people can be
    over their own limit while the group result is within the doubled limit, and the reverse.

The note's numbers and counting phrases are checked; its other wording is not proven by this validator. Staff read it as an
aid beside the code's own figures, never instead of them.

Off unless MMAI_TRAVEL_AI_REPLY is exactly "1", live travel checks are on, and the call would go to ALM's own Anthropic
account with the approved model. Google requires that journey content is not cached by, stored by or used to improve the
model; that is a property of the Anthropic account, which the operator must have confirmed before setting the switch.
"""
from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction

from . import ai, homebot_travel as tv

log = logging.getLogger("mmai.homebot_travel_reply")

REPLY_ENV = "MMAI_TRAVEL_AI_REPLY"
REPLY_CONTRACT = "homebot-travel-reply-v1"
MAX_OPTIONS = 12
NOTE_MAX = 400
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
READY_TEXT = ("Written by the AI from the Google Maps journey estimates in this check. It is shown on this page only and is "
              "not saved. The counts were checked against the journey figures.")

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
        f"note: at most two plain sentences (<= {NOTE_MAX} characters) for staff about that option's travel. Write every number as "
        "digits (for example '5 people', '1 person') and name each individual separately as 'person 2'; do not use number words or words such as all, both, most, none or everyone. "
        "Use only numbers that appear in that option's data. Do not mention the departure time, addresses, names or any web link.",
        "Return ONE JSON object only: {\"options\": [{\"apartment_id\": integer, \"within_own_limit\": integer, \"over_own_limit\": "
        "integer, \"not_known\": integer, \"group_result\": string, \"note\": string}]} with exactly one entry per option, in the order given.",
    ]
    if compact:
        rulesx.append("Be brief: one sentence per note.")
    return (SYSTEM + "\n\nRules:\n- " + "\n- ".join(rulesx) + "\n\nGOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps; " + pk["basis"] + ")\n\n"
            + json.dumps({"options": shown}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------------------ validator
_NUMBER_WORD = re.compile(r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|both|half|couple|several|few|"
                          r"all|most|none|nobody|everyone|everybody|every|neither|either|dozen|majority of them)\b", re.I)
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")
_UNSAFE = re.compile(r"[<>]|https?:|www\.|[\x00-\x1f\x7f]", re.I)
_WHO = r"(?:\s+of\s+(?:the\s+)?\d+)?(?:\s+(?:people|person|persons|housemates?|members?|residents?|travellers?|journeys?))?"
# a bare count of people: not "person 2", not the tail of "22.3", not "limit of 30". A list such as "persons 2 and 3 are over"
# reads as a count of 3 and is refused unless it happens to be right: a withheld note is the safe side of that doubt.
_COUNT = r"(?<![\d.,])(?<!person )(?<!persons )(?<!of )\b(\d+)(?![\d.,]*\d|-)"
_COUNT_CLAIMS = (
    ("not_known", re.compile(rf"{_COUNT}{_WHO}\s+(?:is|are|was|were|remains?|still)?\s*(?:not known|unknown|not measured|unmeasured|"
                             r"needs? confirm\w*|need to be confirmed|could not be (?:measured|checked))", re.I)),
    ("over_own_limit", re.compile(rf"{_COUNT}{_WHO}\s+(?:is|are|was|were|did|do|does|falls?|goes?|go)?\s*(?:not (?:meet|within|inside|under)|"
                                  r"n't (?:meet|within)|(?<=did )not\b|(?<=do )not\b|(?<=does )not\b|(?<=did)n't|(?<=does)n't|over|above|exceeds?|exceeded|beyond|outside|longer than|misses|missed|miss)", re.I)),
    ("within_own_limit", re.compile(rf"{_COUNT}{_WHO}\s+(?:is|are|was|were|falls?|fall|fell|stays?|stay)?\s*(?:met|meets?|within|inside|under|"
                                    r"at or under|satisf\w+)", re.I)),
)
_GROUP_SUBJECT = r"\b(?:group|party|household|overall|option|apartment|closest majority|majority average|average)\b"
_IN_SENTENCE = r"(?:[^.;!?]|(?<=\d)\.(?=\d))*?"   # up to the end of the sentence; the point in "21.8" does not end one
_GROUP_PASS = re.compile(_GROUP_SUBJECT + _IN_SENTENCE + r"\b(?:pass(?:es|ed)?|meets?|met|satisf\w+|is within|are within|falls? within|"
                         r"within the doubled|qualif\w+|suitable|acceptable|fine)\b", re.I)
_GROUP_FAIL = re.compile(_GROUP_SUBJECT + _IN_SENTENCE + r"\b(?:fails?|failed|exceeds?|exceeded|is over|are over|over the doubled|"
                         r"above the doubled|too long|unsuitable|ruled out|excluded)\b", re.I)
# a statement that the group result is NOT settled ("whether the group meets ...", "the group may pass once ..."). It has
# to sit in the same clause, at or before the claim; "person 7 is not known, but the group passes" is still a claim.
_HEDGE = re.compile(r"\b(?:whether|if|unknown|not (?:yet )?(?:known|clear|confirmed|settled|possible to say)|needs?|until|would|could|might|"
                    r"may|to be confirmed|unconfirmed|(?:cannot|can't) (?:yet )?be (?:said|confirmed|determined|assessed|judged))\b", re.I)
_NEGATION = re.compile(r"\b(?:not|never|cannot|no longer)\b|n't\b", re.I)
_CLAUSE_START = re.compile(r"[,.;:!?]\s|\s(?:but|and|although|though|while|yet|so|because|since)\s", re.I)


def _group_claims(note: str):
    """Each thing the note asserts about the group result, as WITHIN or OVER. A hedged clause asserts nothing; a negated
    one asserts the opposite ("does not meet the doubled limit" is a claim that it is over)."""
    for rx, says in ((_GROUP_PASS, WITHIN), (_GROUP_FAIL, OVER)):
        for m in rx.finditer(note):
            starts = [s.end() for s in _CLAUSE_START.finditer(note, 0, m.start())]
            clause = note[starts[-1] if starts else 0:m.end()]
            if _HEDGE.search(clause):
                continue
            if _NEGATION.search(clause):
                yield OVER if says == WITHIN else WITHIN
            else:
                yield says


def _number(token: str):
    try:
        return Decimal(token.replace(",", "."))
    except InvalidOperation:
        return None


def _allowed_numbers(f: dict) -> set:
    values = [f["stated_limit_minutes"], f["doubled_limit_minutes"], len(f["people"]),
              sum(1 for p in f["people"] if p["journey"] != "no_destination"),
              sum(1 for p in f["people"] if p["journey"] == "measured"), *f["counts"].values(),
              f["group"]["closest_majority_size"], f["group"]["closest_majority_average_minutes"]]
    values += [p["person"] for p in f["people"]] + [p["minutes"] for p in f["people"] if p["journey"] == "measured"]
    return {Decimal(str(v)) for v in values if v is not None}


def _check_note(note, f: dict) -> str:
    where = f"option {f['apartment_id']}"
    if not isinstance(note, str) or not note.strip() or len(note) > NOTE_MAX:
        raise ReplyMismatch(f"{where}: note missing or too long")
    note = " ".join(note.split())
    if _UNSAFE.search(note):
        raise ReplyMismatch(f"{where}: note carries markup or a link")
    if _NUMBER_WORD.search(note):
        raise ReplyMismatch(f"{where}: note uses a number word, so its count cannot be checked")
    allowed = _allowed_numbers(f)
    for token in _DIGITS.findall(note):
        if _number(token) not in allowed:
            raise ReplyMismatch(f"{where}: note states a number that is not one of this option's figures")
    for key, rx in _COUNT_CLAIMS:
        for m in rx.finditer(note):
            if int(m.group(1)) != f["counts"][key]:
                raise ReplyMismatch(f"{where}: note's count for {key} does not match")
    for says in _group_claims(note):
        if says != f["group"]["result"]:
            raise ReplyMismatch(f"{where}: note states a group result the figures do not support")
    return note


def parse_reply(text: str, pk: dict) -> list:
    """The model's reply, accepted only if every option is present once, every count and the group result equal the code's
    own, and every number in every note belongs to that option. Raises ReplyMismatch otherwise; nothing is partly accepted."""
    try:
        out = ai._json_from(text)
    except ai.AIUnavailable:
        raise ReplyMismatch("reply is not JSON")
    rows = out.get("options") if isinstance(out, dict) and set(out) == {"options"} else None
    if not isinstance(rows, list) or len(rows) != len(pk["options"]):
        raise ReplyMismatch("reply does not cover exactly the options given")
    clean = []
    for f, r in zip(pk["options"], rows):
        keys = {"apartment_id", "within_own_limit", "over_own_limit", "not_known", "group_result", "note"}
        if not isinstance(r, dict) or set(r) != keys or type(r["apartment_id"]) is not int or r["apartment_id"] != f["apartment_id"]:
            raise ReplyMismatch("reply entry is not for the option given")
        for key in ("within_own_limit", "over_own_limit", "not_known"):
            if type(r[key]) is not int or r[key] != f["counts"][key]:
                raise ReplyMismatch(f"option {f['apartment_id']}: {key} does not match")
        if r["group_result"] != f["group"]["result"]:
            raise ReplyMismatch(f"option {f['apartment_id']}: group result does not match")
        clean.append({"apartment_id": f["apartment_id"], **f["counts"], "stated_limit_minutes": f["stated_limit_minutes"],
                      "doubled_limit_minutes": f["doubled_limit_minutes"], "group_result": f["group"]["result"],
                      "group_plain": GROUP_PLAIN[f["group"]["result"]], "note": _check_note(r["note"], f)})
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
