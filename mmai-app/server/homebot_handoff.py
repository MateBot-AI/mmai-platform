"""H5 / H14 — HomeBot → MateBot hand-off with reviewable, compatible prefill (9 November 2026).

When a HomeBot enquirer says their party is incomplete and wants MateBot's help finding the extra people, the HomeBot form ends
and the MateBot form opens (H2: in place, no leaving dialog).  At that point HomeBot knows exactly four things: the party is
incomplete, how many people it has now, how many it wants in total, and that help is wanted.  Nothing else has been asked yet,
so nothing else can be carried.

The carried answers become an ordinary MateBot saved form (form_drafts, I19(f)) marked origin='homebot', opened at the first
screen so every prefilled answer is reviewed on its own screen before the enquirer reaches it on the summary (H3/H5: "prefill
known compatible HomeBot answers for review and ask the missing MateBot questions").

Mapping — only what the MateBot question means the same thing (no guessing; H5):
  people now = 1      → party "Alone"                       (one person enquiring)
  people now = 2      → party_size "2", party left blank    (MateBot distinguishes "Couple sharing one bed" from "With friends";
                                                              HomeBot has not asked, so MateBot asks)
  people now = 3..6   → party "With friends", party_size n  (a couple is two people, so three or more are friends)
  total wanted = T    → mate_ideal T-1                      (MateBot 3.3 counts the OTHER people who would live with the
                                                              enquirer — rules.check_group: n-1 ≤ mate_max — so a wanted total
                                                              of T is T-1 others)
  mate_max            → NOT prefilled                        (HomeBot asked for the total wanted, not the most they would accept)
  budget              → NOT prefilled                        (HomeBot's budget is for the whole apartment; MateBot's is per person
                                                              — never confused, H5; and it has not been asked yet anyway)
No name, email, phone or any other identity detail exists at this point, and none is ever written into the hand-off draft.

The HomeBot enquiry table records nothing for this branch (unchanged): the party continues as a MateBot enquiry.
"""
from __future__ import annotations

import json
import secrets

from . import security as sec

ORIGIN = "homebot"
MIN_NOW, MAX_NOW, MIN_TARGET, MAX_TARGET = 1, 6, 2, 7   # the HomeBot hand-off screen shows for a wanted total of 2..7 (8+ is routed to staff)
DRAFT_DAYS = 365   # same retention as any other saved form (T5)


class HandoffError(ValueError):
    def __init__(self, errors: dict):
        super().__init__("hand-off answers invalid")
        self.errors = errors


def prefill(headcount_now, headcount_target) -> dict:
    """Validated MateBot answers for a HomeBot party of `headcount_now` people wanting `headcount_target` in total."""
    errors: dict = {}
    now = sec.as_int(headcount_now, -1, -1, 99)
    target = sec.as_int(headcount_target, -1, -1, 99)
    if not (MIN_NOW <= now <= MAX_NOW):
        errors["headcount_now"] = "Please say how many people are in your group now (1 to 6)."
    if not (MIN_TARGET <= target <= MAX_TARGET):
        errors["headcount_target"] = "MateBot can help groups of up to 7 people; for larger groups please call us."
    if not errors and target <= now:
        errors["headcount_target"] = "The total must be more than the people you have now."
    if errors:
        raise HandoffError(errors)
    answers: dict = {"mate_ideal": str(target - 1)}
    if now == 1:
        answers["party"] = "Alone"
    elif now == 2:
        answers["party_size"] = "2"
    else:
        answers["party"] = "With friends"
        answers["party_size"] = str(now)
    return answers


def carried_sentences(headcount_now: int, headcount_target: int) -> list[str]:
    """Plain words for the HomeBot hand-off screen: what is carried across, and what MateBot still asks (T10)."""
    now, target = int(headcount_now), int(headcount_target)
    extra = target - now
    out = []
    if now == 1:
        out.append("That you are enquiring on your own.")
    elif now == 2:
        out.append("That two of you are enquiring together (MateBot will ask whether you are a couple or friends).")
    else:
        out.append(f"That {now} of you are enquiring together as friends.")
    out.append(f"That you would ideally like {target - 1} other {'housemate' if target - 1 == 1 else 'housemates'} living with you in total "
               f"— the {now} {'person' if now == 1 else 'people'} you have now plus the {extra} more you want.")
    out.append("Everything else — your names and contact details, dates, budget per person, preferences and the most housemates you would accept — "
               "is asked by MateBot, and you can change the carried answers on their own screens.")
    return out


def create(c, headcount_now, headcount_target, *, is_test: bool, now_iso: str, expires_iso: str) -> dict:
    """Save the hand-off as a MateBot draft (origin='homebot'); returns the token, the carried answers and the plain sentences.
    The draft holds matching answers only (no identity); it expires like any saved form and is wiped with test data when is_test."""
    is_test = bool(is_test) or sec.pilot_gate_enabled()   # controlled pilot: saved forms are TEST too
    answers = prefill(headcount_now, headcount_target)
    token = secrets.token_urlsafe(18)
    c.execute("INSERT INTO form_drafts(token, answers_json, step, email, is_test, mate_token, email_token, created_at, updated_at, expires_at, submit_key, origin) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (token, json.dumps(answers, separators=(",", ":"), sort_keys=True), 0, None, 1 if is_test else 0, None, None, now_iso, now_iso, expires_iso, None, ORIGIN))
    return {"token": token, "resume": "#/resume?t=" + token + ("&test=1" if is_test else ""), "answers": answers,
            "carried": carried_sentences(int(headcount_now), int(headcount_target)), "is_test": bool(is_test)}
