"""Introduction-only profile details: sex and current age (Bill, 8 November 2026; register I8 / GP2).

Restored SOLELY so that introductions can describe each person; they play no part in matching. They are kept out of every
eligibility check, score, replacement search, review packet and staff compatibility assessment (see the pinning tests in
tests/test_intro_profile.py). Fresh answers only: the retired legacy sex / date-of-birth columns are never read, copied or
inferred from, and nothing here is derived from a date of birth. A missing answer is flagged for staff, never filled in.

Fields (enquirer on `enquiries`, each other party member on `party_members`, same names):
  intro_sex        one of SEX_OPTIONS
  intro_sex_other  the person's own words when intro_sex is ANOTHER_TERM (otherwise NULL)
  intro_age        current age in whole years as the person states it (not a date of birth); any non-negative whole number
                   that fits the column (0..AGE_MAX) is stored — no business age band, no exclusion, no derivation
Both questions are OPTIONAL at intake: "Prefer not to say" is an answer and is never overridden; an absent answer is a staff
flag (and an introduction draft omits/flags it), never a reason to refuse a submission or to exclude anyone from matching.
"""
from __future__ import annotations

ANOTHER_TERM = "Another term"
NOT_SAID = "Prefer not to say"
SEX_OPTIONS = ("Female", "Male", ANOTHER_TERM, NOT_SAID)
AGE_MIN, AGE_MAX = 0, 999   # representation bound only (three digits), not an age rule
MAX_OTHER = 40
KEYS = ("intro_sex", "intro_sex_other", "intro_age")
PURPOSE = "Shared in introductions to proposed housemates; not used for matching."
SEX_MESSAGE = "Please choose one of the options."
OTHER_MESSAGE = f"Please say the term you use (up to {MAX_OTHER} characters)."
AGE_MESSAGE = "Please enter the current age as a whole number of years (or leave it blank)."


def clean_age(v):
    """Whole number of years in range → int; None/'' → None (not given); anything else → ValueError. Booleans, floats with a
    fraction, lists and dicts are never an age; a digit string from a form field is accepted."""
    if v is None or v == "":
        return None
    if isinstance(v, bool) or isinstance(v, (list, dict)):
        raise ValueError(AGE_MESSAGE)
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(AGE_MESSAGE)
        v = int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s.isdigit():
            raise ValueError(AGE_MESSAGE)
        v = int(s)
    if not isinstance(v, int) or not (AGE_MIN <= v <= AGE_MAX):
        raise ValueError(AGE_MESSAGE)
    return v


def clean(values: dict, *, existing: dict | None = None) -> tuple[dict, dict]:
    """Validate the intro keys present in `values` (a submission or a staff patch). Returns (cleaned, errors) where `cleaned`
    holds only the keys that were supplied (plus intro_sex_other, which is tied to intro_sex) and `errors` maps key → message.
    `existing` (the stored row) lets a patch that changes only intro_sex_other be checked against the stored intro_sex."""
    out: dict = {}
    err: dict = {}
    existing = existing or {}
    if "intro_sex" in values:
        v = values["intro_sex"]
        if v is None or v == "":
            out["intro_sex"] = None
        elif isinstance(v, str) and v.strip() in SEX_OPTIONS:
            out["intro_sex"] = v.strip()
        else:
            err["intro_sex"] = SEX_MESSAGE
    # the sex the row will hold after this change: the supplied value when valid, None when supplied but invalid/blank, else the stored one
    sex_now = out["intro_sex"] if "intro_sex" in out else (None if "intro_sex" in values else existing.get("intro_sex"))
    if "intro_sex" in values or "intro_sex_other" in values:
        other = values.get("intro_sex_other", existing.get("intro_sex_other"))
        if sex_now == ANOTHER_TERM:
            if not isinstance(other, str) or not other.strip() or len(other.strip()) > MAX_OTHER:
                err["intro_sex_other"] = OTHER_MESSAGE
            else:
                out["intro_sex_other"] = other.strip()
        else:
            if other not in (None, "") and not isinstance(other, str):
                err["intro_sex_other"] = OTHER_MESSAGE
            out["intro_sex_other"] = None   # only meaningful with "Another term"; never stored otherwise
    if "intro_age" in values:
        try:
            out["intro_age"] = clean_age(values["intro_age"])
        except ValueError as e:
            err["intro_age"] = str(e)
    return out, err


def given(rec: dict) -> bool:
    """True when the person has answered both questions ("Prefer not to say" is an answer)."""
    return bool(rec.get("intro_sex")) and rec.get("intro_age") not in (None, "")


def status(lead: dict, members) -> tuple[str, list[str]]:
    """('complete', []) when every person has both answers; otherwise ('incomplete', [names missing]). For staff display only:
    an incomplete profile never blocks or alters matching."""
    missing = []
    if not given(lead):
        missing.append(f"{lead.get('first_name') or 'The enquirer'} (enquirer)")
    for i, m in enumerate(members or [], start=2):
        if isinstance(m, dict) and not given(m):
            missing.append(m.get("name") or f"Person {i}")
    return ("complete" if not missing else "incomplete"), missing


def describe(rec: dict) -> str:
    """Plain text for receipts / staff: 'Female, 24' | 'Prefer not to say, 24' | '—' when not given. Never derived."""
    parts = []
    sex = rec.get("intro_sex")
    if sex == ANOTHER_TERM:
        parts.append(rec.get("intro_sex_other") or ANOTHER_TERM)
    elif sex:
        parts.append(sex)
    if rec.get("intro_age") not in (None, ""):
        parts.append(str(rec["intro_age"]))
    return ", ".join(parts) if parts else "—"
