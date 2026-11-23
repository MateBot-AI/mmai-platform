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
23 Nov 2026 (Bill): Sex is now MULTI-SELECT (Female, Male, Non-binary, A different term) and, together with the person's
housemate sex preference, IS a matching input (the "Same sex only" hard filter, rules.pair_conflict). intro_sex is stored as the
selected options joined by ", " (see split()); older single values ("Another term", "Prefer not to say") stay readable and are
shown exactly as given. An absent answer is a staff flag, never a reason to refuse a submission.
"""
from __future__ import annotations

ANOTHER_TERM = "Another term"          # legacy single answer (pre 23 Nov 2026), still readable
NOT_SAID = "Prefer not to say"         # legacy single answer (pre 23 Nov 2026), still readable
DIFFERENT_TERM = "A different term (please specify)"
SEX_OPTIONS = ("Female", "Male", "Non-binary", DIFFERENT_TERM)   # what the form offers now
LEGACY_SEX_OPTIONS = (ANOTHER_TERM, NOT_SAID)
OTHER_TERMS = (ANOTHER_TERM, DIFFERENT_TERM)   # either of these needs the person's own words in intro_sex_other
SEX_SEP = ", "
AGE_MIN, AGE_MAX = 0, 999   # representation bound only (three digits), not an age rule
MAX_OTHER = 40
KEYS = ("intro_sex", "intro_sex_other", "intro_age")
PURPOSE = "This helps me form groups that respect people’s preferences about sex and age."
SEX_MESSAGE = "Please choose one of the options."
OTHER_MESSAGE = f"Please say the term you use (up to {MAX_OTHER} characters)."
AGE_MESSAGE = "Please enter the current age as a whole number of years."


def split(v) -> list[str]:
    """The sex options a stored/submitted value holds, in order, without duplicates. Accepts a list (form), the ", "-joined string
    stored in the column, or a legacy single answer. Never raises."""
    if v is None or v == "":
        return []
    items = v if isinstance(v, (list, tuple)) else str(v).split(SEX_SEP.strip())
    out: list[str] = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def hard_sexes(v) -> set[str]:
    """The Female / Male selections only: the sole values the "Same sex only" hard filter compares (Bill, 23 Nov 2026)."""
    return {x for x in split(v) if x in ("Female", "Male")}


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
        picked = split(v)
        if not picked:
            out["intro_sex"] = None
        elif isinstance(v, (list, tuple)) or isinstance(v, str):
            allowed = SEX_OPTIONS + LEGACY_SEX_OPTIONS
            if any(x not in allowed for x in picked):
                err["intro_sex"] = SEX_MESSAGE
            elif len(picked) > 1 and any(x in LEGACY_SEX_OPTIONS for x in picked):
                err["intro_sex"] = SEX_MESSAGE   # a legacy answer stands alone
            else:
                out["intro_sex"] = SEX_SEP.join(picked)
        else:
            err["intro_sex"] = SEX_MESSAGE
    # the sex the row will hold after this change: the supplied value when valid, None when supplied but invalid/blank, else the stored one
    sex_now = out["intro_sex"] if "intro_sex" in out else (None if "intro_sex" in values else existing.get("intro_sex"))
    if "intro_sex" in values or "intro_sex_other" in values:
        other = values.get("intro_sex_other", existing.get("intro_sex_other"))
        if any(x in OTHER_TERMS for x in split(sex_now)):
            if not isinstance(other, str) or not other.strip() or len(other.strip()) > MAX_OTHER:
                err["intro_sex_other"] = OTHER_MESSAGE
            else:
                out["intro_sex_other"] = other.strip()
        else:
            if other not in (None, "") and not isinstance(other, str):
                err["intro_sex_other"] = OTHER_MESSAGE
            out["intro_sex_other"] = None   # only meaningful with a "different term" answer; never stored otherwise
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


def sex_text(sex, other) -> str:
    """The sex answer as the person gave it: each option, with a "different term" replaced by their own words."""
    bits = []
    for x in split(sex):
        if x in OTHER_TERMS:
            bits.append((other or "").strip() or ("A different term" if x == DIFFERENT_TERM else x))
        else:
            bits.append(x)
    return SEX_SEP.join(bits)


def describe(rec: dict) -> str:
    """Plain text for receipts / staff: 'Female, 24' | 'Female, Male, 24' | '—' when not given. Never derived."""
    parts = []
    sex = sex_text(rec.get("intro_sex"), rec.get("intro_sex_other"))
    if sex:
        parts.append(sex)
    if rec.get("intro_age") not in (None, ""):
        parts.append(str(rec["intro_age"]))
    return ", ".join(parts) if parts else "—"
