"""Enquiry validation and storage. One code path for all three intake channels (I4)."""
from __future__ import annotations

import re
from datetime import date

from . import ai, rules, submissions
from . import security as sec
from . import intro_profile as ip
from . import apartment_features as feat
from . import tracking as trk
from .countries import normalise_country
from .db import audit, j, now_iso, pj, retention_guard, row, rows, transaction
from .institutions import institution_list

# Question classification (I7 / I12): lives in code only; staff cannot change it.
QUESTION_TYPES = {
    "1.1": "Contact", "1.2": "Contact", "1.3": "Contact", "1.4": "Contact", "1.5": "Contact",
    # 1.6 (date of birth) and 1.7 (sex) are no longer asked (I8, Bill 8 November 2026); 6.4 is the all-occupants adult declaration (I11(d))
    # 1.8-1.10: country of birth, country now and languages are details shown to staff; languages also feed a soft score (23 Nov 2026)
    "1.8": "Information", "1.9": "Information", "1.10": "Preference",
    "2.1": "Must-match", "2.2": "Must-match", "2.3": "Must-match", "2.4": "Must-match", "2.5": "Must-match",
    "3.1": "Must-match", "3.2": "Must-match", "3.3": "Preference", "3.4": "Must-match", "3.5": "Must-match", "3.6": "Must-match", "3.7": "Preference", "3.8": "Must-match", "3.9": "Must-match",
    "3.10": "Must-match",  # I16(f): couples within a "With friends" group, stated by the enquirer; MMAI never assumes a couple
    "4.1": "Preference", "4.2": "Preference", "4.3": "Preference", "4.4": "Preference", "4.5": "Preference", "4.6": "Preference", "4.7": "Preference",
    # 17 / 23 Nov 2026 (Bill): 5.1 housemate sex preference is a hard filter; 5.2 age range and 5.4 culture question are soft preferences.
    # 5.3 (own country / region) was replaced by 5.4 on 23 Nov 2026.
    "5.1": "Must-match", "5.2": "Preference", "5.4": "Preference", "5.5": "Must-match", "5.6": "Must-match",
    "5.7": "Preference", "5.8": "Preference", "5.9": "Preference", "5.10": "Preference", "5.11": "Preference", "5.12": "Preference", "5.13": "Contact",
    "6.1": "Contact", "6.2": "Consent", "6.3": "Consent", "6.4": "Consent", "6.6": "Consent",  # 6.5 (guardian details) is no longer on the form: staff process (I11(b), I16(c))
}

STATUSES = ["New", "Proposed in group", "Introduced", "Group confirmed", "Leased", "Withdrawn", "Awaiting replacement"]  # GP10 (set by MMAI when a group loses a member)
CHANNELS = ["Phone", "Emailed form", "Landing page"]
ROOM_OPTIONS = ["Own room", "Share a room with one other person", "Share a room with two others", "Couple sharing one bed"]
SIZE_OPTIONS = ["Studio", "1 bedroom", "2 bedrooms", "3 bedrooms"]
# I8 (8 November 2026): sex/gender, date of birth, exact age, age preference and housemate-sex preference are no longer collected or
# used for anyone (lead or party member). Keys arriving from an old cached client are STRIPPED before validation/storage and never
# written to answers_json; a staff edit naming one is refused. Historical rows keep their stored values untouched (nothing is
# purged), but they are not returned by the API, shown, matched on, scored or sent to the AI.
RETIRED_FIELDS = frozenset({"dob", "sex", "sex_other", "guardian_name", "guardian_relationship", "guardian_email", "guardian_phone",
                            "consent_age"})   # consent_age: the old lead-only 18+ tick; never read as the all-occupants declaration
# 17 Nov 2026: sex_pref, age_pref, own_region, avoid_* restored as matching inputs (Bill supersession of I8 / M10–M12 / T17(g)).
# Legacy dob / sex / guardian fields remain retired.
RETIRED_MEMBER_FIELDS = frozenset({"sex", "sex_other", "dob", "needs_guardian"})
NOT_COLLECTED = ""   # sentinel stored in the legacy NOT NULL columns (dob, sex, sex_pref) for new rows: "not collected", never a value
CULTURE_YES = "Yes — I’m interested in experiencing new cultures and languages"
CULTURE_OPTIONS = ("I prefer to live with people from my own country or region", "I’m happy to live with anyone", CULTURE_YES)
CULTURE_LISTS = ("culture_languages", "culture_regions", "culture_countries")
# No total cap across the three lists (Bill, 25 Nov 2026 — previously max 3 on 23 Nov 2026)
ADULTS_FIELD = "adults_confirm"   # 6.4 / I11(d): the enquirer declares that EVERY occupant will be 18 or older on the move-in date
ADULTS_MESSAGE = "Please confirm that everyone in your group, including you, will be 18 or older on the move-in date."

# Every key a current submission may carry. answers_json stores only these (nested party members likewise), never the raw body.
ANSWER_KEYS = frozenset({
    "first_name", "last_name", "email", "phone_code", "phone", "contact_method", "birth_country_raw", "live_country_raw", "languages", "languages_other",
    "city", "move_in", "move_in_flex", "move_out", "move_out_flex", "budget_option", "apt_sizes", "mate_ideal", "mate_max", "rooms", "room_first", "bunk_ok",
    "party", "party_size", "party_members", "couples_answer", "couples", "doing", "school", "school_other", "course", "level", "course_start", "course_end",
    "work", "areas", "travel", "travel_modes", "features", "features_top", "smoke", "smoke_ok", "routine", "tidy", "social", "guests",
    "diet", "else_text", "intro", "source", "consent_store", "consent_share", "consent_marketing", ADULTS_FIELD,
    "sex_pref", "age_pref", "culture_interest", "culture_languages", "culture_regions", "culture_countries",
    "own_region", "avoid_answer", "avoid_regions", "avoid_countries",   # own_region / avoid_*: retired 23 Nov 2026, still accepted from old cached clients and never read
    *ip.KEYS,   # introduction-only sex / current age (optional) — still display data, separate from sex_pref
    "campaign", "staff_member", "staff_notes", "channel", "enq_date", "is_test", "referrer",
})
MEMBER_KEYS = frozenset({"name", "email", "birth_country_raw", *ip.KEYS})
# Staff-only section (7.x) and record metadata: set by staff on the Phone channel or by the server, never by an enquirer. A public
# submission that carries them (the hidden Staff-only inputs have DOM defaults, e.g. channel "Phone") has them dropped before
# validation and storage, so answers_json for a public enquiry never says a channel the enquiry did not come through (D1, 9 Nov 2026).
STAFF_ONLY_KEYS = frozenset({"channel", "campaign", "staff_member", "staff_notes", "enq_date", "referrer"})


def strip_staff_only(a: dict) -> dict:
    """Copy of a PUBLIC submission without the staff-only / metadata keys (the submission fingerprint is taken from the raw body
    before this, so an in-flight retry from an older cached form still replays rather than conflicts)."""
    return {k: v for k, v in a.items() if k not in STAFF_ONLY_KEYS} if isinstance(a, dict) else a


YES_NO_MESSAGE = "Please answer Yes or No."
SMOKE_OPTIONS = ("No", "Yes, outside only (balcony or courtyard)", "Yes")   # the form's two answers + the pre-8-Nov alias still on older rows (matching.LEGACY_SMOKE_ALIASES)
SMOKE_OK_OPTIONS = ("Yes", "No")


def yes_no_value(v) -> int | None:
    """M3 (3.6): the only accepted readings of a Yes/No answer - bool, the integers 0/1, and the strings the landing form
    ("Yes"/"No"), the staff page ("1"/"0") and the seed send, case- and space-insensitive. Anything else (None, "", "maybe",
    2, 0.0, lists, dicts, "on") is malformed -> None and is refused by the caller; it is never recorded as a No."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int) and v in (0, 1):
        return v
    if isinstance(v, str):
        word = v.strip().lower()
        if word in ("yes", "1", "true"):
            return 1
        if word in ("no", "0", "false"):
            return 0
    return None


def adults_confirm_value(v) -> bool | None:
    """Strict reading of the 6.4 declaration: JSON true (Python bool True) is the ONLY affirmative; bool False is an explicit
    negative; anything else (strings such as "true"/"yes"/"on", numbers, lists, dicts, None) is malformed → None. Nothing is
    ever inferred from a date of birth, a name, a party list or an older consent column."""
    if isinstance(v, bool):
        return v
    return None


def _party_size(v) -> int:
    try:
        return max(1, int(v or 1))
    except (TypeError, ValueError):
        return 0   # unreadable size: never matches a fingerprint


def party_fingerprint(lead: dict, members, party_size) -> str:
    """What the 6.4 declaration was made about: the enquirer (first name, last name, email), every other person in the party
    (name and email), the number of member rows and the DECLARED party size (Alone = 1). Any identity edit, a renamed member
    (even with the same email), a member leaving, a new lead taking over the form, or a changed size changes the fingerprint,
    so a stored declaration no longer covers the party and staff must obtain a renewed one. Lower-cased and trimmed so a
    cosmetic case change is not a party change."""
    def norm(v):
        return (v or "").strip().lower()
    people = [[norm(m.get("name")), norm(m.get("email"))] for m in (members or []) if isinstance(m, dict)]
    return j({"lead": [norm(lead.get("first_name")), norm(lead.get("last_name")), norm(lead.get("email"))], "size": _party_size(party_size),
              "n": len(people), "members": sorted(people)})


def adults_status(e: dict, members, move_in=None) -> str:
    """I11(d) eligibility from the stored declaration ONLY (never DOB, names, consent_age or party rows):
    'confirmed'   the flag is set and the party and move-in date still match what was declared (a LATER date than the one
                  declared is still covered: anyone 18 on the declared date is 18 later, provided the people are the same);
    'renew'       a declaration exists but the move-in date is now EARLIER than the one declared, or the party changed →
                  staff must obtain a renewed declaration for the new facts;
    'missing'     no declaration (e.g. an enquiry made before 8 November 2026) → staff to confirm with the enquirer."""
    if not e.get("adults_confirmed"):
        return "missing"
    declared_for = _d(e.get("adults_confirmed_move_in"))
    mi = move_in if move_in is not None else _d(e.get("move_in"))
    if isinstance(mi, str):
        mi = _d(mi)
    if declared_for is None or mi is None or mi < declared_for:
        return "renew"
    members = [m for m in (members or []) if isinstance(m, dict)]
    if _party_size(e.get("party_size")) != 1 + len(members):   # fail closed: a declaration never covers phantom or missing occupants
        return "renew"
    if (e.get("adults_confirmed_party") or "") != party_fingerprint(e, members, e.get("party_size")):
        return "renew"
    return "confirmed"


def clean_answers(a: dict) -> dict:
    """The allowlisted submission that is stored as answers_json: current question keys only (no retired identity fields, no
    unknown keys), party members reduced to name, email and country of birth."""
    out = {k: v for k, v in a.items() if k in ANSWER_KEYS}
    if isinstance(out.get("party_members"), list):
        out["party_members"] = [{k: v for k, v in m.items() if k in MEMBER_KEYS} for m in out["party_members"] if isinstance(m, dict)]
    return out


def strip_retired(a: dict) -> dict:
    """Copy of a public submission without the retired identity fields (lead and party members). Used before validation so an
    old cached form cannot store them, and so answers_json never contains them."""
    out = {k: v for k, v in a.items() if k not in RETIRED_FIELDS}
    if isinstance(out.get("party_members"), list):
        out["party_members"] = [{k: v for k, v in m.items() if k not in RETIRED_MEMBER_FIELDS} if isinstance(m, dict) else m for m in out["party_members"]]
    return out
FLEX_IN = ["Exact date", "± 1 week", "± 2 weeks", "± 1 month"]
FLEX_OUT = FLEX_IN + ["Open to extending"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ValidationError(Exception):
    def __init__(self, errors: dict):
        super().__init__("Validation failed")
        self.errors = errors


class ConcurrentEdit(Exception):
    """The row changed between the guarded read and the conditional write (another staff member, a purge, the AI). Nothing was
    written; the caller answers 409 and the staff member reloads."""


# Review M2/H1: length caps per field type and no angle brackets in names (output is also HTML-escaped in the client).
NAME_FIELDS = ("first_name", "last_name")
LONG_TEXT_FIELDS = ("intro", "else_text", "work", "course", "staff_notes", "travel", "school_other", "languages_other")
MAX_NAME, MAX_TEXT, MAX_LONG_TEXT, MAX_NOTES, MAX_LIST = 80, 300, 2000, 5000, 60
NAME_BAD_RE = re.compile(r"[<>{}\\]")


def _length_errors(a: dict) -> dict:
    """{field: message} for values that are longer than the form allows or of the wrong shape."""
    err: dict[str, str] = {}
    for k, v in list(a.items()):
        if k == "party_members" or k == "couples":
            continue
        if isinstance(v, str):
            cap = MAX_NAME if k in NAME_FIELDS else MAX_NOTES if k == "staff_notes" else MAX_LONG_TEXT if k in LONG_TEXT_FIELDS else MAX_TEXT
            if len(v) > cap:
                err[k] = f"Please keep this to {cap} characters."
            elif k in NAME_FIELDS and NAME_BAD_RE.search(v):
                err[k] = "Please use letters only here (no < > { } or \\)."
        elif isinstance(v, list):
            if len(v) > MAX_LIST or any(isinstance(x, str) and len(x) > MAX_TEXT for x in v):
                err[k] = "Too many entries."
            elif any(isinstance(x, (dict, list)) for x in v):
                err[k] = "Invalid value."
        elif isinstance(v, dict):
            err[k] = "Invalid value."
    members = a.get("party_members")
    if members is not None:
        if not isinstance(members, list) or len(members) > 6 or any(not isinstance(m, dict) for m in members):
            err["party_members"] = "Invalid value."
        else:
            for i, m in enumerate(members):
                for k, v in m.items():
                    if not isinstance(v, str):
                        continue
                    cap = MAX_NAME if k in ("name", "sex_other", "intro_sex_other") else MAX_TEXT
                    if len(v) > cap:
                        err[f"party_members.{i}.{k}"] = f"Please keep this to {cap} characters."
                    elif k == "name" and NAME_BAD_RE.search(v):
                        err[f"party_members.{i}.{k}"] = "Please use letters only here (no < > { } or \\)."
    return err


def _d(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except Exception:
        return None


# H31 / H28(c) / GP6(d) (16 Nov 2026): how the person would travel to their place of study or work. The same three choices as the
# HomeBot form; public transport is one combined choice. DERIVED wording: Bill approved the three choices for both forms, the
# question text here is taken from the HomeBot form's and is not his own wording. Optional, like 4.7. An enquiry saved before
# this question, or one that leaves it blank, has NO answer (NULL): a choice is never inferred, and no choice is assumed.
TRAVEL_MODES = ("Walk", "Bicycle", "Public transport")
TRAVEL_MODES_MESSAGE = "Please choose from the travel choices shown."


def travel_modes_value(v):
    """The clean list of chosen travel choices in the form's order; [] for no answer; None when the value is not a list of
    the three choices (a string, an unknown or repeated choice): refused, never repaired."""
    if v is None or v == "" or v == []:
        return []
    if not isinstance(v, list) or any(not isinstance(x, str) or x not in TRAVEL_MODES for x in v) or len(set(v)) != len(v):
        return None
    return [m for m in TRAVEL_MODES if m in v]


def validate(a: dict, today: date | None = None, party_max: int | None = None) -> dict:
    """Server-side validation shared by every channel. Returns {field: message}. `party_max` (GP12): the most people one
    party may hold, one less than the beds in ALM's largest available apartment (matching.mate_limit); 7 when not given."""
    today = today or date.today()
    party_max = min(int(party_max or 7), 7)
    if not isinstance(a, dict):
        return {"form": "Invalid form data."}
    a = strip_retired(a)
    err: dict[str, str] = _length_errors(a)
    if err:
        return err
    req = ["first_name", "last_name", "email", "phone_code", "phone", "contact_method", "birth_country_raw", "live_country_raw",
           "move_in", "move_in_flex", "move_out", "move_out_flex", "budget_option", "mate_ideal", "mate_max", "bunk_ok", "room_first", "party",
           "doing", "smoke", "smoke_ok", "source"]
    for k in req:
        v = a.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            err[k] = "Please complete this."
    if "bunk_ok" not in err and yes_no_value(a.get("bunk_ok")) is None:   # M3: present but not a Yes/No answer
        err["bunk_ok"] = YES_NO_MESSAGE
    # 10 Nov 2026 (HC review): the smoking answers are the form's own strings. A non-string (e.g. JSON true) or any other value
    # is rejected here instead of being stored and later read as "No" / "not ok" by the unit builder (matching.unit_from_row).
    if "smoke" not in err and a.get("smoke") not in SMOKE_OPTIONS:
        err["smoke"] = "Please choose one of the smoking answers."
    if "smoke_ok" not in err and a.get("smoke_ok") not in SMOKE_OK_OPTIONS:
        err["smoke_ok"] = YES_NO_MESSAGE
    if a.get("email") and not EMAIL_RE.match(str(a["email"])):
        err["email"] = "Please enter a valid email."
    if not pj(j(a.get("languages") or [])):
        err["languages"] = "Please tick at least one language."  # M9
    if not a.get("apt_sizes"):
        err["apt_sizes"] = "Please tick at least one apartment size."
    if not a.get("rooms"):
        err["rooms"] = "Please tick at least one room arrangement."
    if (a.get("city") or "Melbourne") != "Melbourne":
        err["city"] = "Only Melbourne is available at launch."  # C1
    if travel_modes_value(a.get("travel_modes")) is None:
        err["travel_modes"] = TRAVEL_MODES_MESSAGE
    mi, mo = _d(a.get("move_in")), _d(a.get("move_out"))
    if a.get("move_in") and not mi:
        err["move_in"] = "Please enter a valid date."
    if a.get("move_out") and not mo:
        err["move_out"] = "Please enter a valid date."
    if mi and mo and mo <= mi:
        err["move_out"] = "Move-out must be after move-in."
    # I11(d) / 6.4: the enquirer must affirmatively declare that everyone included in this enquiry, themselves included, will
    # be 18 or older on the move-in date. False, missing or malformed → rejected; nothing is inferred from any date.
    if adults_confirm_value(a.get(ADULTS_FIELD)) is not True:
        err[ADULTS_FIELD] = ADULTS_MESSAGE
    # Introduction-only sex / current age (optional): validated when supplied, never required, never a matching input.
    _, ierr = ip.clean({k: a[k] for k in ip.KEYS if k in a})
    err.update(ierr)
    if a.get("move_in_flex") and a["move_in_flex"] not in FLEX_IN:
        err["move_in_flex"] = "Invalid option."
    if a.get("move_out_flex") and a["move_out_flex"] not in FLEX_OUT:
        err["move_out_flex"] = "Invalid option."
    try:
        if int(a.get("mate_max") or 0) < int(a.get("mate_ideal") or 0):
            err["mate_max"] = "This can't be fewer than your ideal number."
        if not (1 <= int(a.get("mate_max") or 0) <= 6):
            err["mate_max"] = "Choose 1 to 6."
    except ValueError:
        err["mate_max"] = "Choose 1 to 6."
    kind = rules.guide_kind_for(mi, mo)
    if a.get("budget_option") and rules.budget_max_for(a["budget_option"], kind) is None:
        err["budget_option"] = "Please choose a budget from the price guide."
    if a.get("party") and a["party"] != "Alone":
        try:
            n = int(a.get("party_size") or 0)
        except ValueError:
            n = 0
        if a["party"].startswith("Couple"):
            n = 2
        if not (2 <= n <= 7):
            err["party_size"] = f"Choose 2 to {party_max} people."
        elif n > party_max:  # GP12: a party must be smaller than the apartment, so MateBot AI can add at least one housemate from outside it
            err["party_size"] = (f"A group of {n} is more than MateBot AI can match: our largest apartment sleeps {party_max + 1}, and every group needs "
                                 f"at least one housemate from outside your own party, so a party can have at most {party_max} people. "
                                 "Please call us if you are a bigger group.")
        members = a.get("party_members") or []
        if len(members) < n - 1:
            err["party_members"] = "Please give a name, email and country of birth for each other person."  # I16(a), I16(b)
        for i, m in enumerate(members[: max(0, n - 1)]):
            err.update({f"party_members.{i}.{k}": v for k, v in validate_member(m, mi).items()})
        if a["party"] == "With friends":
            err.update(validate_couples(a, n))  # 3.10 / I16(f)
    if a.get("doing") == "Studying" and not (a.get("school") or "").strip():
        err["school"] = "Please choose your institution."
    if a.get("school") in ("Other", "English language school (enter name)") and not (a.get("school_other") or "").strip():
        err["school_other"] = "Please enter the name."
    # 17 / 23 Nov 2026: housemate sex preference (Must Match), age range and culture interest (Preference)
    SEX_PREF_OPTIONS = ("Same sex only", "Mixed is fine", "No preference")
    AGE_BANDS = ("18–21", "22–25", "26–30", "31+", "No preference")
    sp = (a.get("sex_pref") or "").strip()
    if sp not in SEX_PREF_OPTIONS:
        err["sex_pref"] = "Please choose a housemate sex preference."
    ci = (a.get("culture_interest") or "").strip()
    if ci not in CULTURE_OPTIONS:
        err["culture_interest"] = "Please answer the culture and language question."
    else:
        a["culture_interest"] = ci
    for k in CULTURE_LISTS:
        v = a.get(k)
        if v in (None, "", []):
            a[k] = []
        elif not isinstance(v, list) or not all(isinstance(x, str) and x.strip() and len(x) <= 80 for x in v):
            err[k] = "Please choose from the list."
        else:
            a[k] = list(dict.fromkeys(x.strip() for x in v))
    if ci != CULTURE_YES:
        for k in CULTURE_LISTS:
            a[k] = []   # the lists only apply to the "Yes" answer
    age = a.get("age_pref")
    if age not in (None, "", []):
        if not isinstance(age, list) or not all(isinstance(x, str) for x in age):
            err["age_pref"] = "Please choose age ranges from the list."
        else:
            bad = [x for x in age if x not in AGE_BANDS]
            if bad:
                err["age_pref"] = "Please choose age ranges from the list."
            elif "No preference" in age and len(age) > 1:
                a["age_pref"] = ["No preference"]  # No preference overrides other selections

    # 3.11 / 3.12 (8 Nov 2026): wanted apartment and building features and the up-to-three that matter most. Both optional; wishes only.
    a["features"], a["features_top"] = feat.clean_wanted(a.get("features"), top=a.get("features_top"), errors=err)
    # 6.1 / 6.3 agreements (8 Nov 2026 hardening): JSON true is the ONLY affirmative, as for the 6.4 declaration. Strings such as
    # "false", "no", "0" (or "true", "yes") from a non-browser caller are never read as agreement; the browser sends real booleans.
    for k in ("consent_store", "consent_share"):
        if a.get(k) is not True:
            err[k] = "This agreement is required."
    return err


COUPLE_REFS = ["lead"] + [f"m{i}" for i in range(1, 7)]  # 3.10 person references: the enquirer, then Person 2..7 in 3.9 order


def normalise_couples(a: dict) -> tuple[str | None, list[list[str]]]:
    """3.10 / I16(f). Returns (answer, couples). Couples exist only when the enquirer states them:
    - 3.8 "Couple sharing one bed": the enquirer and Person 2 are the couple (answer 'Yes');
    - 3.8 "With friends": the 3.10 answer and the pairs the enquirer picked (refs 'lead', 'm1'...);
    - 3.8 "Alone": no couple. Enquiring with one or two friends never makes anyone a couple."""
    party = a.get("party") or "Alone"
    if party.startswith("Couple"):
        return "Yes", [["lead", "m1"]]
    if party != "With friends":
        return None, []
    ans = (a.get("couples_answer") or "").strip() or None
    raw = a.get("couples") if a.get("couples") is not None else a.get("couples_json")
    if isinstance(raw, str):
        raw = pj(raw)
    pairs = []
    for pr in raw or []:
        if isinstance(pr, dict):
            pr = [pr.get("a"), pr.get("b")]
        pr = [str(x).strip() for x in (pr or []) if x not in (None, "")]
        pairs.append(pr)
    if ans == "Yes":
        return ans, pairs
    return ans, []


def validate_couples(a: dict, n: int) -> dict:
    """3.10 / I16(f) for a "With friends" party: the answer is required; if Yes, each couple is two different people from the
    party (the enquirer and/or the people listed in 3.9), nobody is in two couples, and at least one couple is given."""
    err: dict[str, str] = {}
    ans, pairs = normalise_couples(a)
    if ans not in ("No", "Yes"):
        err["couples_answer"] = "Please say whether anyone in your group is a couple who will share one bed."
        return err
    if ans == "No":
        return err
    valid_refs = set(COUPLE_REFS[:max(1, n)])
    if not pairs:
        err["couples"] = "Please pick the two people in each couple, or answer No."
        return err
    used: set[str] = set()
    for i, pr in enumerate(pairs):
        if len(pr) != 2 or pr[0] == pr[1]:
            err[f"couples.{i}"] = "A couple is two different people."
            continue
        if any(x not in valid_refs for x in pr):
            err[f"couples.{i}"] = "Pick people from your group only."
            continue
        if used & set(pr):
            err[f"couples.{i}"] = "Someone is listed in two couples."
        used |= set(pr)
    return err


def validate_member(m: dict, move_in: date | None, today: date | None = None) -> dict:
    """I16(a)/(b): each other person in the party needs a name, email and country of birth (text, I15). Sex and date of birth
    are no longer collected (I8); adult status for every occupant comes only from the enquirer's 6.4 declaration (I11(d)).
    Any guardian arrangement for someone under 18 at enquiry is a staff process (I11(b), I16(c)), not a form field.
    Returns {field: message}."""
    err: dict[str, str] = {}
    if not isinstance(m, dict):
        return {"name": "Invalid person."}
    if not (m.get("name") or "").strip():
        err["name"] = "Name is required."
    if not EMAIL_RE.match(str(m.get("email") or "")):
        err["email"] = "A valid email is required."
    if not (m.get("birth_country_raw") or "").strip():
        err["birth_country_raw"] = "Country of birth is required."  # I16(b)
    _, ierr = ip.clean({k: m[k] for k in ip.KEYS if k in m})   # introduction-only details: optional, each person's own answer
    err.update(ierr)
    return err


def resolve_country(raw: str, use_ai: bool = True) -> tuple[str | None, bool]:
    name, confident = normalise_country(raw)
    if name:
        return name, True
    if use_ai:
        try:
            guess = ai.normalise_country_ai(raw)
            if guess:
                name2, ok2 = normalise_country(guess)
                return (name2 or guess), False  # AI guess: staff confirm (brief 2.6)
        except ai.AIUnavailable:
            pass
    return None, False


def create_enquiry(c, a: dict, channel: str, staff: str | None = None, is_test: bool = False, email_link_id: int | None = None,
                   use_ai_countries: bool = True, created_at: str | None = None, party_max: int | None = None, on_inserted=None,
                   submission_key: str | None = None, tracking=None) -> dict:
    """Validate and store one enquiry. The enquiry row, its party members, the email-link mark and anything `on_inserted(c, eid)`
    writes are committed together: the whole write runs in one db.transaction block (connection lock held from the first
    INSERT to the commit, so no other thread can commit or roll back half of it), and `on_inserted` runs right after the
    enquiry INSERT inside that block, so a submission-key row records the new id atomically with the enquiry itself.
    `submission_key` goes on the enquiry row in the same INSERT; enquiries.submission_key is UNIQUE, so the database itself
    refuses a second enquiry for one key (callers catch the violation and replay). Anything that may call out (country
    normalisation, possibly the AI) is done before the block. Any exception inside the block rolls everything back.
    `tracking` is the optional Google Ads object the public form sent (server/tracking.py): it is cleaned here, written in the same
    INSERT, and can never fail the save. It is not part of `a`, so it never touches validation or answers_json.
    Note: get_enquiry() may raise after the commit; callers that need the id regardless should use on_inserted."""
    is_test = bool(is_test) or sec.pilot_gate_enabled()   # controlled pilot: nothing new is ever stored as real data
    a = strip_retired(a)   # retired identity fields never reach storage (answers_json included)
    errors = validate(a, party_max=party_max)
    if errors:
        raise ValidationError(errors)
    # 3.11 / 3.12: store the cleaned lists (catalogue order, no duplicates), not the raw submission (validate() works on a copy)
    a["features"], a["features_top"] = feat.clean_wanted(a.get("features"), top=a.get("features_top"))
    # 5.4 (23 Nov 2026): the three culture lists only apply to the "Yes" answer; stored without blanks or duplicates
    for k in CULTURE_LISTS:
        a[k] = list(dict.fromkeys(x.strip() for x in (a.get(k) or []))) if a.get("culture_interest") == CULTURE_YES else []
    if channel not in CHANNELS:
        raise ValidationError({"channel": "Invalid channel"})
    mi, mo = _d(a["move_in"]), _d(a["move_out"])
    kind = rules.guide_kind_for(mi, mo)
    budget_max = rules.budget_max_for(a["budget_option"], kind)
    bc, bc_conf = resolve_country(a["birth_country_raw"], use_ai_countries)
    lc, _ = resolve_country(a["live_country_raw"], use_ai_countries)
    party = a["party"]
    party_size = 1 if party == "Alone" else (2 if party.startswith("Couple") else int(a["party_size"]))
    couples_answer, couples = normalise_couples(a)  # 3.10 / I16(f)
    now = created_at or now_iso()
    cols = dict(
        is_test=1 if is_test else 0, channel=channel, campaign=a.get("campaign"), staff_member=staff if channel == "Phone" else a.get("staff_member"),
        created_at=now, last_contact_at=now, status="New", staff_notes=a.get("staff_notes"), email_link_id=email_link_id,
        first_name=a["first_name"].strip(), last_name=a["last_name"].strip(), email=a["email"].strip().lower(), phone_code=a.get("phone_code"), phone=a.get("phone"),
        contact_method=a.get("contact_method"), dob=NOT_COLLECTED, sex=NOT_COLLECTED, sex_other=None,   # I8: not collected (legacy NOT NULL columns)
        birth_country_raw=a["birth_country_raw"], birth_country=bc, birth_country_confirmed=1 if bc_conf else 0,
        live_country_raw=a["live_country_raw"], live_country=lc, languages=j(a.get("languages") or []), city=a.get("city") or "Melbourne",
        move_in=a["move_in"], move_in_flex=a["move_in_flex"], move_out=a["move_out"], move_out_flex=a["move_out_flex"],
        budget_option=a["budget_option"], budget_guide=kind, budget_max=budget_max, apt_sizes=j(a["apt_sizes"]), mate_ideal=int(a["mate_ideal"]), mate_max=int(a["mate_max"]),
        rooms=j(a["rooms"]), bunk_ok=yes_no_value(a["bunk_ok"]), room_first=a.get("room_first"), party=party, party_size=party_size,
        couples_answer=couples_answer, couples_json=j(couples),
        doing=a.get("doing"), school=a.get("school"), school_other=a.get("school_other"), course=a.get("course"), level=a.get("level"),
        course_start=a.get("course_start"), course_end=a.get("course_end"), work=a.get("work"), areas=j(a.get("areas") or []), travel=a.get("travel"),
        travel_modes=j(travel_modes_value(a.get("travel_modes"))) if travel_modes_value(a.get("travel_modes")) else None,   # NULL = no answer
        sex_pref=a.get("sex_pref") or None, age_pref=j(a.get("age_pref") or []) if a.get("age_pref") is not None else None,
        # own_region and avoid_* were retired on 23 Nov 2026 (replaced by the culture question); avoid_answer is a legacy NOT NULL column
        own_region=None, avoid_answer=NOT_COLLECTED, avoid_regions=None, avoid_countries=None,
        culture_interest=a.get("culture_interest"), culture_languages=j(a.get("culture_languages") or []),
        culture_regions=j(a.get("culture_regions") or []), culture_countries=j(a.get("culture_countries") or []),
        features=j(a.get("features") or []), features_top=j(a.get("features_top") or []), smoke=a["smoke"], smoke_ok=a["smoke_ok"],
        routine=a.get("routine"), tidy=a.get("tidy"), social=a.get("social"), guests=a.get("guests"), diet=a.get("diet"), else_text=a.get("else_text"), intro=a.get("intro"),
        source=a["source"], consent_store=1, consent_share=1, consent_marketing=1 if a.get("consent_marketing") is True else 0,   # validated True above; optional marketing only on JSON true
        # I11(d): the 6.4 declaration covers every occupant (validated True above). consent_age is the legacy self-only column.
        consent_age=1, adults_confirmed=1, adults_confirmed_by="enquirer", adults_confirmed_at=now,
        adults_confirmed_move_in=a["move_in"],
        adults_confirmed_party=party_fingerprint({"first_name": a["first_name"], "last_name": a["last_name"], "email": a["email"]}, (a.get("party_members") or [])[: max(0, party_size - 1)], party_size),
        guardian_name=None, guardian_relationship=None, guardian_email=None, guardian_phone=None,
        answers_json=j(clean_answers(a)),
    )
    # Introduction-only sex / current age for the enquirer (validated above; optional). Nothing is taken from legacy columns.
    cols.update({k: None for k in ip.KEYS})
    cols.update(ip.clean({k: a[k] for k in ip.KEYS if k in a})[0])
    if submission_key:
        cols["submission_key"] = submission_key  # same INSERT as the row: the UNIQUE index is the last line of defence against a duplicate
    cols.update(trk.columns(tracking, now))  # Google Ads click id / campaign names: only the columns that have a value (the rest stay NULL)
    members = []
    for m in (a.get("party_members") or [])[: max(0, party_size - 1)]:
        # I16(b): country of birth per person, normalised like 1.8 (I15; staff confirm anything MMAI could not recognise).
        # Resolved here, before the write block, so the AI is never called while the connection lock is held.
        mraw = (m.get("birth_country_raw") or "").strip() or None
        mbc, mbc_conf = resolve_country(mraw, use_ai_countries) if mraw else (None, False)
        mintro = {**{k: None for k in ip.KEYS}, **ip.clean({k: m[k] for k in ip.KEYS if k in m})[0]}   # this person's own intro answers (optional)
        members.append((m["name"].strip(), m["email"].strip().lower(), mraw, mbc, 1 if mbc_conf else 0, mintro["intro_sex"], mintro["intro_sex_other"], mintro["intro_age"]))
    keys = list(cols)
    with transaction(c):  # one atomic block: enquiry row, key mark, party members, email-link mark; all or nothing
        c.execute(f"INSERT INTO enquiries({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [cols[k] for k in keys])
        eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        if on_inserted:
            on_inserted(c, eid)  # same transaction as the enquiry row (committed below, or rolled back together with it)
        for mrow in members:
            c.execute("""INSERT INTO party_members(enquiry_id, name, email, birth_country_raw, birth_country, birth_country_confirmed, intro_sex, intro_sex_other, intro_age)
                         VALUES (?,?,?,?,?,?,?,?,?)""", (eid,) + mrow)   # legacy sex/sex_other/dob stay NULL, needs_guardian stays 0
        if email_link_id:
            c.execute("UPDATE email_links SET used_at=? WHERE id=?", (now, email_link_id))
    audit(c, staff, "create_enquiry", "enquiry", eid, f"channel={channel}; test={is_test}")
    return get_enquiry(c, eid)


EDITABLE = {"status", "staff_notes", "campaign", "staff_member", "first_name", "last_name", "email", "phone_code", "phone", "contact_method",
            "birth_country", "birth_country_confirmed", "live_country", "move_in", "move_in_flex", "move_out", "move_out_flex", "budget_option", "mate_ideal", "mate_max",
            "bunk_ok", "room_first", "doing", "school", "school_other", "course", "level", "work", "travel", "travel_modes", "smoke", "smoke_ok",
            "routine", "tidy", "social", "guests", "diet", "else_text", "intro", "source", "languages", "apt_sizes", "rooms", "areas", "features", "features_top",
            "consent_share", "consent_marketing", "city", "couples_answer", "couples_json", "adults_confirmed", *ip.KEYS}
JSON_FIELDS = {"languages", "apt_sizes", "rooms", "areas", "features", "features_top", "couples_json", "age_pref", "culture_languages", "culture_regions", "culture_countries"}
RETIRED_EDIT_MESSAGE = ("This is no longer collected or used (sex, date of birth, age and sex preferences, and the own-country/region and "
                        "avoid-country/region preferences were removed on 8 November 2026).")


def update_enquiry(c, eid: int, patch: dict, staff: str | None) -> dict:
    """Staff edit. T5: the read, the validation and the write are ONE guarded step (the status chain changes purge eligibility,
    and the facts are what the AI captures), committed at guard exit; a status change is conditional on the status that was
    read under the guard, so a purge, a group transition or another staff member in between makes it fail, never half-apply."""
    with retention_guard(c, label="enquiry edit"):
        _update_enquiry_guarded(c, eid, patch, staff)
    return get_enquiry(c, eid)


def _update_enquiry_guarded(c, eid: int, patch: dict, staff: str | None) -> None:
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    if not e:
        raise KeyError("Enquiry not found")
    if not isinstance(patch, dict):
        raise ValidationError({"form": "Invalid form data."})
    retired = sorted(k for k in patch if k in RETIRED_FIELDS)
    if retired:   # a staff edit naming a retired field is refused outright (not silently dropped), so old tooling fails loudly
        raise ValidationError({k: RETIRED_EDIT_MESSAGE for k in retired})
    lerr = _length_errors({k: v for k, v in patch.items() if k not in ("couples_json",)})
    if lerr:
        raise ValidationError(lerr)
    sets, vals = [], []
    couple_patch = {k: patch[k] for k in ("couples_answer", "couples_json") if k in patch}
    if "features" in patch or "features_top" in patch:   # 3.11 / 3.12: validated together, like the form (top must stay within the wanted list)
        ferr = {}
        wanted, most = feat.clean_wanted(patch["features"] if "features" in patch else pj(e.get("features"), []),
                                         top=patch["features_top"] if "features_top" in patch else pj(e.get("features_top"), []), errors=ferr)
        if ferr:
            raise ValidationError(ferr)
        sets += ["features=?", "features_top=?"]
        vals += [j(wanted), j(most)]
    intro_patch = {k: patch[k] for k in ip.KEYS if k in patch}
    if intro_patch:   # introduction-only details: validated as a set; touches ONLY these columns (see intro_changed below)
        cleaned, ierr = ip.clean(intro_patch, existing=e)
        if ierr:
            raise ValidationError(ierr)
        sets += [f"{k}=?" for k in cleaned]
        vals += list(cleaned.values())
    for k, v in patch.items():
        if k not in EDITABLE or k in couple_patch or k in ("features", "features_top") or k in intro_patch:
            continue
        if k == "status" and v not in STATUSES:
            raise ValidationError({"status": "Invalid status"})
        if k in JSON_FIELDS:
            v = j(v or [])
        if k == "bunk_ok":
            v = yes_no_value(v)
            if v is None:   # M3: refused before any column is written (the UPDATE and its audit row come after this loop)
                raise ValidationError({"bunk_ok": YES_NO_MESSAGE})
        if k == "smoke" and v not in SMOKE_OPTIONS:   # 10 Nov 2026: the form's strings only; a JSON boolean is refused, never stored
            raise ValidationError({"smoke": "Please choose one of the smoking answers."})
        if k == "smoke_ok" and v not in SMOKE_OK_OPTIONS:
            raise ValidationError({"smoke_ok": YES_NO_MESSAGE})
        if k == "travel_modes":   # the form's three choices only; an empty list clears the answer (NULL), nothing is inferred
            modes = travel_modes_value(v)
            if modes is None:
                raise ValidationError({"travel_modes": TRAVEL_MODES_MESSAGE})
            v = j(modes) if modes else None
        if k == "birth_country_confirmed":
            v = 1 if v in (True, 1, "1", "Yes", "yes", "true") else 0
        if k == "adults_confirmed":
            # I11(d): staff record that the enquirer declared (e.g. by phone, or for a pre-8 November enquiry, or renewed for an
            # earlier move-in date / changed party) that every occupant will be 18+ at move-in. JSON true/false only; who and
            # when are recorded, together with the move-in date and party the declaration was made about. false clears it.
            val = adults_confirm_value(v)
            if val is None:
                raise ValidationError({"adults_confirmed": "Use true or false."})
            # Bound to the RESULTING state of this same edit (identity fields in the patch included), so a declaration recorded
            # together with a correction covers the corrected party, and only that.
            members = rows(c.execute("SELECT email, name FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))
            resulting = {k: (patch[k] if k in patch and k in EDITABLE else e.get(k)) for k in ("first_name", "last_name", "email")}
            sets += ["adults_confirmed=?", "adults_confirmed_by=?", "adults_confirmed_at=?", "adults_confirmed_move_in=?", "adults_confirmed_party=?"]
            vals += ([1, f"staff:{staff}" if staff else "staff", now_iso(), patch.get("move_in") or e["move_in"], party_fingerprint(resulting, members, e.get("party_size"))] if val
                     else [0, None, None, None, None])
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if couple_patch:  # 3.10 / I16(f): staff correction after a phone call; the same validation as the form
        merged = {"party": e["party"], "couples_answer": couple_patch.get("couples_answer", e.get("couples_answer")),
                  "couples": couple_patch["couples_json"] if "couples_json" in couple_patch else (e.get("couples_json") or [])}
        if e["party"] == "With friends":
            cerr = validate_couples(merged, int(e.get("party_size") or 1))
            if cerr:
                raise ValidationError(cerr)
        ans, pairs = normalise_couples(merged)
        sets += ["couples_answer=?", "couples_json=?"]
        vals += [ans, j(pairs)]
    if "move_in" in patch or "move_out" in patch or "budget_option" in patch:
        mi = _d(patch.get("move_in") or e["move_in"])
        mo = _d(patch.get("move_out") or e["move_out"])
        kind = rules.guide_kind_for(mi, mo)
        bm = rules.budget_max_for(patch.get("budget_option") or e["budget_option"], kind)
        if bm is None:
            raise ValidationError({"budget_option": "Budget option is not in the applicable price guide"})
        sets += ["budget_guide=?", "budget_max=?"]
        vals += [kind, bm]
    notes_touched = any(k in patch and k in EDITABLE for k in ("else_text", "work", "intro"))
    if "lead_answers_confirmed" in patch and patch["lead_answers_confirmed"] is not True:
        # ownership confirmation is a deliberate staff act: only the literal JSON value true counts; strings such as "false",
        # "0" or "yes", numbers and objects are rejected and the row stays held (root ownership-boundary probe, 9 Nov 2026)
        raise ValidationError({"lead_answers_confirmed": "Must be true to confirm the enquirer's individually recorded answers"})
    confirm_lead = patch.get("lead_answers_confirmed") is True and e.get("lead_answers_unverified_at")
    if confirm_lead:
        # GP9(e) takeover: staff have gone through the row's individual answers (as edited in this same save, if at all) with the person
        # who took over the enquiry. Only this explicit confirmation lifts the provenance flag; the notes step then starts afresh.
        sets += ["lead_answers_unverified_at=?"]
        vals += [None]
        notes_touched = True
    elif e.get("lead_answers_unverified_at") and notes_touched:
        notes_touched = False   # unconfirmed row: an edit alone does not make the former enquirer's text this person's; the notes step stays closed
        sets += ["notes_status=?", "notes_packet_json=?", "notes_input_key=?", "notes_residual=?", "notes_updated_at=?"]
        vals += ["unverified", None, None, None, now_iso()]
        c.execute("DELETE FROM note_facts WHERE enquiry_id=?", (eid,))
        c.execute("DELETE FROM note_extraction_cache WHERE enquiry_id=?", (eid,))
    if notes_touched:
        # GAP-T4c v2: the person's free text is the source of the validated notes facts; an edit invalidates them at once
        # (status back to pending, packet cleared, provenance rows deleted), so no review or brief built on the old text stays
        # ready and no extraction result captured for the old text can land (ai_queue._apply_notes re-checks the input key).
        sets += ["notes_status=?", "notes_packet_json=?", "notes_input_key=?", "notes_residual=?", "notes_updated_at=?"]
        vals += ["pending", None, None, None, now_iso()]
    if sets:
        if notes_touched:
            c.execute("DELETE FROM note_facts WHERE enquiry_id=?", (eid,))
            c.execute("DELETE FROM note_extraction_cache WHERE enquiry_id=?", (eid,))
        vals.append(eid)
        cond = ""
        if "status" in patch:   # the status write is conditional on the status read under this guard
            cond = " AND status=?"
            vals.append(e["status"])
        n = c.execute(f"UPDATE enquiries SET {', '.join(sets)} WHERE id=?{cond}", vals).rowcount
        if n != 1:
            raise ConcurrentEdit("The enquiry changed while you were working. Reload the page and check it again.")
        audit(c, staff, "update_enquiry", "enquiry", eid, ", ".join(k for k in patch if k in EDITABLE))
        if intro_patch:
            intro_changed(c, eid, staff)   # nested same-thread guard / builder's hook (maybe_commit): commits with this step
        if any(k in patch for k in ("first_name", "last_name", "email")):
            _identity_changed(c, eid, staff)   # GP13: a different person (conservatively) -> every member of their live groups answers again


def intro_changed(c, eid: int, staff: str | None = None) -> None:
    """A person's introduction-only details changed. The facts matching and the AI review work from are unchanged, so nothing
    is re-ranked, re-queued or re-reviewed; but any customer introduction already drafted for a group this enquiry is in now
    describes the person wrongly, so it is marked stale for re-drafting (builder's ai_queue hook when present; otherwise an
    append-only 'stale' marker row in group_briefs, which the drafting code treats as the latest intro state)."""
    try:
        from . import ai_queue
        hook = getattr(ai_queue, "intro_profile_changed", None)
    except Exception:   # pragma: no cover - the AI queue module is optional at import time
        hook = None
    if hook:
        hook(c, eid)
        return
    with retention_guard(c, label="intro stale marker"):   # keyed to the person: written under the shared T5 guard like every other row about them
        gids = [r["group_id"] for r in rows(c.execute("SELECT DISTINCT group_id FROM group_members WHERE enquiry_id=?", (eid,)))]
        for gid in gids:
            c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, body_json, status, error, created_at)
                         VALUES (?, COALESCE((SELECT MAX(round) FROM group_briefs WHERE group_id=?), 0), 'intro', NULL, ?, NULL, 'stale', ?, ?)""",
                      (gid, gid, eid, "Introduction details were edited; the customer introduction must be drafted again.", now_iso()))
        if gids:
            audit(c, staff, "intro_draft_stale", "enquiry", eid, ",".join(str(g) for g in gids))


NOTES_STATUS_LABEL = {"pending": "Not yet read for the review", "none": "No notes written", "ok": "Read for the review",
                      "failed": "Could not be read - retry with the group's AI assessment",
                      "unverified": "Written by the former enquirer - not read until the person who took over confirms the answers"}


def notes_review_view(c, e: dict) -> dict:
    """Staff view of the T4c notes step for one enquiry: status label, accepted items with the person's own supporting words
    (plain label, polarity, strength, typed value, source field, quote), count of items the validator could not ground, and the
    'unreviewed note' marker. Never a model sentence, never a category code as the label."""
    from . import notes_facts
    st = e.get("notes_status") or "pending"
    current = notes_facts.is_current(e)   # fix 2 currentness: accepted under an earlier extraction contract -> shown as awaiting re-read, items not in review
    if st == "ok" and not current:
        st = "pending"
    items = rows(c.execute("SELECT * FROM note_facts WHERE enquiry_id=? AND validator_status='accepted' ORDER BY id", (e["id"],)))
    unresolved = c.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=? AND validator_status='rejected' AND rejection_code<>'rejected_duplicate'", (e["id"],)).fetchone()[0]
    out_items = []
    for it in items:
        out_items.append({"label": notes_facts.plain_label(it["category"]), "polarity": it["polarity"], "strength": it["strength"], "subject": it["subject"],
                          "attribution": it.get("attribution"), "value": pj(it.get("value_json")), "field": it["field"], "quote": it["span"],
                          "in_review": current and it["polarity"] == "affirmed" and it["subject"] == "self", "basis": notes_facts.BASIS})
    return {"status": st, "status_label": NOTES_STATUS_LABEL.get(st, NOTES_STATUS_LABEL["pending"]), "current": current, "items": out_items, "unresolved_count": unresolved,
            "unreviewed_note": bool(e.get("notes_residual")) or unresolved > 0, "updated_at": e.get("notes_updated_at"),
            "lead_answers_unverified_at": e.get("lead_answers_unverified_at")}


def get_enquiry(c, eid: int) -> dict:
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    if not e:
        raise KeyError("Enquiry not found")
    for k in JSON_FIELDS:
        e[k] = pj(e[k])
    e["travel_modes"] = pj(e.get("travel_modes"))   # [] = no answer (not asked, or left blank)
    # I8: retired identity fields are never returned (historical values stay in the row, untouched and unused)
    for k in RETIRED_FIELDS:
        e.pop(k, None)
    e["party_members"] = rows(c.execute("""SELECT id, name, email, birth_country_raw, birth_country, birth_country_confirmed, intro_sex, intro_sex_other, intro_age
                                          FROM party_members WHERE enquiry_id=? ORDER BY id""", (eid,)))
    # Sex and current age: shown to staff and used in introductions; sex also feeds the Same-sex-only filter. Missing = a flag for staff, never inferred.
    e["intro_profile_status"], e["intro_missing"] = ip.status(e, e["party_members"])
    e["adults_confirmed"] = bool(e.get("adults_confirmed"))   # I11(d): every occupant 18+ at move-in, declared (enquirer or staff) — never inferred
    e["adults_status"] = adults_status(e, e["party_members"])   # confirmed | renew | missing (see adults_status)
    # adults_confirmed_party (the fingerprint of who the declaration covered) stays in the record: matching.unit_from_row
    # re-derives the status from it, and it only repeats names/emails already in the record (lower-cased).
    e["barred_people"] = rows(c.execute("SELECT id, name, was_lead, reason, barred_at, barred_by FROM barred_people WHERE enquiry_id=? ORDER BY id", (eid,)))  # GP9(e)
    # 3.10 / I16(f): couples named by the enquirer, as names for staff ("lead" = the enquirer, "m1" = Person 2 ...)
    e["couples"] = couples_named(e, e["party_members"])
    # GAP-T4c v2: what the review was told from this person's own notes (staff see the supporting quote of the person's own text,
    # plain labels, and an 'unreviewed note' marker when something practical could not be read into the catalogue)
    e["notes_review"] = notes_review_view(c, e)
    e.pop("notes_packet_json", None); e.pop("notes_input_key", None)
    e["groups"] = rows(c.execute("""SELECT g.id, g.status, g.apartment_id, a.address, gm.weekly_share, g.is_primary, g.ai_rating FROM group_members gm JOIN groups g ON g.id=gm.group_id
                                   LEFT JOIN apartments a ON a.id=g.apartment_id WHERE gm.enquiry_id=? ORDER BY g.id DESC""", (eid,)))
    e["answers_json"] = clean_answers(pj(e["answers_json"], {}))   # historical submissions: retired/unknown keys are not exposed
    # GP11: linked friends ("Bring a mate"), in plain language for staff
    root_id = e.get("linked_to_enquiry_id") or eid
    linked = rows(c.execute("SELECT id, first_name, last_name, status, created_at, party_size FROM enquiries WHERE (id=? OR linked_to_enquiry_id=?) AND id<>? ORDER BY id", (root_id, root_id, eid)))
    for l in linked:
        l["role"] = "enquirer" if l["id"] == root_id else "friend"
    e["linked_friends"] = linked
    e["linked_root_id"] = root_id
    e["mate_link"] = row(c.execute("SELECT token, created_at, expires_at FROM mate_links WHERE enquiry_id=? ORDER BY created_at DESC LIMIT 1", (root_id,)))  # GP11(i)/(n): the party's one link, held by its current root
    e["mate_requests"] = rows(c.execute("SELECT id, group_id, status, note, created_at FROM mate_requests WHERE (friend_enquiry_id=? OR root_enquiry_id=?) AND status='pending'", (eid, eid)))
    return e


def couples_named(e: dict, members: list[dict]) -> list[dict]:
    """Each couple as {refs: [..], names: [..]}; derived from 3.8 for couple enquiries, from 3.10 for friends parties."""
    cj = e.get("couples_json")
    ans, pairs = normalise_couples({"party": e.get("party"), "couples_answer": e.get("couples_answer"), "couples": cj if isinstance(cj, list) else pj(cj)})
    names = {"lead": f"{e.get('first_name', '')} (enquirer)".strip()}
    for i, m in enumerate(members, start=1):
        names[f"m{i}"] = m.get("name") or f"Person {i + 1}"
    return [{"refs": pr, "names": [names.get(x, x) for x in pr]} for pr in pairs if len(pr) == 2]


MEMBER_EDITABLE = {"name", "email", "birth_country", "birth_country_confirmed", *ip.KEYS}


def update_party_member(c, eid: int, mid: int, patch: dict, staff: str | None) -> dict:
    """Staff edit of one party member (I16(b)); used to confirm a country MMAI could not recognise (I15)."""
    m = row(c.execute("SELECT * FROM party_members WHERE id=? AND enquiry_id=?", (mid, eid)))
    if not m:
        raise KeyError("Party member not found")
    retired = sorted(k for k in patch if k in RETIRED_MEMBER_FIELDS)
    if retired:
        raise ValidationError({k: RETIRED_EDIT_MESSAGE for k in retired})
    sets, vals = [], []
    intro_patch = {k: patch[k] for k in ip.KEYS if k in patch}
    if intro_patch:   # this person's own introduction-only details (optional; validated as a set)
        cleaned, ierr = ip.clean(intro_patch, existing=m)
        if ierr:
            raise ValidationError(ierr)
        sets += [f"{k}=?" for k in cleaned]
        vals += list(cleaned.values())
    for k, v in patch.items():
        if k not in MEMBER_EDITABLE or k in intro_patch:
            continue
        if k == "birth_country_confirmed":
            v = 1 if v in (True, 1, "1", "Yes", "yes", "true") else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        vals += [mid]
        # T5/GP13 (9 Nov 2026): one guarded step (DB lock shared with every writer), committed at guard exit - the identity hook below reads
        # and writes the group membership versions, which the public link checks read under the same guard. No commit of its own any more.
        with retention_guard(c, label="party member edit"):
            n = c.execute(f"UPDATE party_members SET {', '.join(sets)} WHERE id=? AND enquiry_id=?", vals + [eid]).rowcount
            if n != 1:
                raise ConcurrentEdit("The enquiry changed while you were working. Reload the page and check it again.")
            audit(c, staff, "update_party_member", "party_member", mid, ", ".join(k for k in patch if k in MEMBER_EDITABLE))
            if intro_patch:
                intro_changed(c, eid, staff)
            if "name" in patch or "email" in patch:
                _identity_changed(c, eid, staff)   # GP13: see update_enquiry
    return get_enquiry(c, eid)


def _identity_changed(c, eid: int, staff: str | None) -> None:
    """GP13 (inside the caller's guard): the name or email of a person on this enquiry changed. Identities are reconciled conservatively
    (a changed name/email pair is a new person; the old identity is retired) and every live group the enquiry is in gets a new membership
    version when its set of people changed - which resets EVERY member's approval, not only the edited person's. Unchanged pairs: no-op."""
    from . import group_responses, identities   # local: group_responses imports alerts; keep enquiries light at import time
    identities.reconcile(c, eid, "identity_edit")
    for g in rows(c.execute("""SELECT g.id FROM group_members gm JOIN groups g ON g.id=gm.group_id
                               WHERE gm.enquiry_id=? AND g.status IN ('Proposed','Introduced','Group confirmed','Awaiting replacement')""", (eid,))):
        group_responses.on_membership_changed(c, g["id"], "identity_edit", staff)


def log_contact(c, eid: int, staff: str | None, note: str | None = None) -> dict:
    """T5: resets the 12-month retention clock. Guarded: the contact date decides purge eligibility, which purge_expired reads
    under the same guard, so a contact logged on another connection can never land between its eligibility check and delete."""
    with retention_guard(c, label="log contact"):
        c.execute("UPDATE enquiries SET last_contact_at=? WHERE id=?", (now_iso(), eid))
        audit(c, staff, "log_contact", "enquiry", eid, note)   # committed once at guard exit
    return get_enquiry(c, eid)


def forget_generated_text(c, enquiry_ids: list[int], include_leased: bool = False) -> dict:
    """T5 enforcement for everything persisted ABOUT a person outside the rows the enquiry cascade removes (8 Nov 2026).
    Call BEFORE the enquiry rows are deleted (membership and keys are needed). Nothing is matched by name: only rows keyed
    to the person (enquiry id, party-member id, link tokens) or to a group they are or were in are touched.

    Persisted-payload inventory (schema walk, 8 Nov) and what happens to each:
    - enquiries / party_members / answers, barred_people, mate_links, member_rejections, email_outbox: enquiry-row cascade (db schema).
    - enquiry_submissions: opaque tombstone (submissions.tombstone, called by the delete routes).
    - group_briefs rows keyed to the person (member brief, intro-stale marker): deleted whatever the group's status.
    - Groups they are a CURRENT member of (Leased ones only with include_leased): the whole brief round, review rounds and
      intro drafts (each was written about that member set), groups.intro_text / ai_rationale / evidence_json (per-person
      fact snapshot) / discount_note / rerank_note / awaiting_note / held_reason, group_apartments.ai_reason, and the free-text
      audit notes of the group. The assessment is marked stale; the group with nobody left is Dissolved. Lease terms
      (dates, rate, rents, shares, beds, apartment) stay.
    - Groups they were EARLIER in: rows whose immutable ref map (refs_json, written by the AI writer before each call) names
      them are deleted exactly; legacy rows without a map are found through their keyed brief rows and member_rejections and
      deleted by round / time (same round as their member brief, or written no later than their removal). The current member
      set's rounds stay.
    - Bed allocations (groups / group_apartments.allocation_json): the person's name becomes "Former member"; ids, beds,
      shares and room costs (lease terms) stay.
    - groups.replacement_json (they were a suggested replacement somewhere): their entry is removed.
    - mate_requests naming them as root or friend: deleted (the note names them).
    - audit_log: free-text notes about them (contact log, bar, link errors) are cleared; field-name / count notes stay.
    - email_links (emailed-form invitation with their name and address) and form_drafts reached through their tokens: deleted.
    - exit exceptions naming them in the immutable original member links: agreement prose cleared and authority withdrawn;
      already-withdrawn reasons remain history. Opaque links and lease amounts are retained.
    - Not touched: groups.rejected_json / member ids (opaque ids only), match_runs (counts only), HomeBot tables (separate
      product and deletion path), barred_people (cascade; see T15 note).
    Returns counts for the audit note."""
    ids = [int(i) for i in enquiry_ids]
    out = {"people": len(ids), "brief_rows": 0, "review_rows": 0, "groups_cleared": 0, "groups_dissolved": 0, "historical_groups": 0,
           "allocations_scrubbed": 0, "audit_notes_cleared": 0, "email_links_deleted": 0, "drafts_deleted": 0, "replacement_entries": 0, "mate_requests_deleted": 0,
           "run_errors_neutralised": 0, "ai_failure_sinks_neutralised": 0, "audit_errors_neutralised": 0,
           "exception_records_scrubbed": 0}
    # The new authority writer requires the caller's guard. Check before any cleanup writes,
    # including legacy error neutralisation; an unguarded call with authority must fail without partial work.
    if ids:
        marks = ",".join("?" for _ in ids)
        if row(c.execute(f"SELECT 1 FROM exit_exception_members WHERE enquiry_id IN ({marks}) LIMIT 1", tuple(ids))):
            from . import exit_exceptions
            out["exception_records_scrubbed"] = exit_exceptions.forget_people(c, ids)
    # T5 (8 Nov 2026): stored matching-run failure text (match_runs.ai_error / progress_json error, and the in-process run state) is not
    # attributable to any one person, so EVERY retention step neutralises whatever legacy text is there - before the early return, so a
    # purge with nobody eligible still cleans. One call site: the staff delete route and purge_expired both come through here. Commit-free
    # (the caller's retention_guard commits or rolls back); operational counts, phase, dataset and staff attribution on the row are kept.
    from . import matching   # local import: matching imports ai_queue and rules, which must not pull enquiries in at module load
    out["run_errors_neutralised"] = int(matching.neutralise_run_errors(c)["match_runs_rows"])
    # T5 (9 Nov 2026, same hook, same transaction): legacy AI FAILURE-sink text written before Modified A (ai_queue's bounded projection -
    # intentional hold / revise rationale and review content are not failure sinks and are not touched), and the legacy free-text of
    # exactly two audit actions that used to persist str(exception): every row of those two actions, whoever they belong to, becomes the
    # fixed legacy sentence unless it already is a closed `UNEXPECTED:<type token>` detail or that sentence. No other action is read.
    from . import ai_queue   # local import for the same reason as matching
    out["ai_failure_sinks_neutralised"] = int(sum(ai_queue.neutralise_legacy_failure_text(c, commit=False).values()))
    for r in rows(c.execute("SELECT id, detail FROM audit_log WHERE action IN ('bring_mate_error','submit_side_effect_error') AND detail IS NOT NULL")):
        d = r["detail"]
        head, sep, tail = d.partition(":")
        if d == ai.LEGACY_FAILURE_TEXT or (sep and head == "UNEXPECTED" and tail in matching.RUN_ERROR_TYPE_TOKENS):
            continue
        out["audit_errors_neutralised"] += c.execute("UPDATE audit_log SET detail=? WHERE id=?", (ai.LEGACY_FAILURE_TEXT, r["id"])).rowcount
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    sids = [str(i) for i in ids]
    pids = set(sids)
    when = now_iso()
    # GP13 (9 Nov 2026): everything the group-response module keeps about these people (identities incl. name/email comparison values,
    # links, responses, their entries in historical membership snapshots, and the notes of ALL responses on those versions). Counts only.
    from . import group_responses
    gr = group_responses.forget(c, ids)
    out["group_response_rows"] = int(gr["identities"] + gr["links"] + gr["responses"])
    out["group_versions_scrubbed"] = int(gr["versions_scrubbed"])

    # ---- their own keyed rows: audit notes, invitation link, saved drafts, mate requests
    out["audit_notes_cleared"] += c.execute(f"""UPDATE audit_log SET detail=NULL WHERE entity='enquiry' AND detail IS NOT NULL AND entity_id IN ({q})
                                               AND action IN ('log_contact','bar_from_matching','lift_bar','bring_mate_error','submit_side_effect_error')""", sids).rowcount
    links = [r["email_link_id"] for r in rows(c.execute(f"SELECT email_link_id FROM enquiries WHERE id IN ({q}) AND email_link_id IS NOT NULL", ids))]
    tokens = [r["token"] for r in rows(c.execute(f"SELECT token FROM mate_links WHERE enquiry_id IN ({q})", ids))]
    if links:
        lq = ",".join("?" * len(links))
        tokens += [r["token"] for r in rows(c.execute(f"SELECT token FROM email_links WHERE id IN ({lq})", links))]
        out["email_links_deleted"] += c.execute(f"DELETE FROM email_links WHERE id IN ({lq})", links).rowcount
    if tokens:
        tq = ",".join("?" * len(tokens))
        out["drafts_deleted"] += c.execute(f"DELETE FROM form_drafts WHERE mate_token IN ({tq}) OR email_token IN ({tq})", tokens + tokens).rowcount
    out["drafts_deleted"] += c.execute(f"DELETE FROM form_drafts WHERE submit_key IN (SELECT submit_key FROM enquiry_submissions WHERE enquiry_id IN ({q}))", ids).rowcount
    out["mate_requests_deleted"] += c.execute(f"DELETE FROM mate_requests WHERE root_enquiry_id IN ({q}) OR friend_enquiry_id IN ({q})", ids + ids).rowcount

    # ---- groups: current membership, and earlier membership through keyed history
    current = {g["id"]: g for g in rows(c.execute(f"SELECT DISTINCT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id IN ({q})", ids))}
    keyed = rows(c.execute(f"""SELECT group_id, round, created_at FROM group_briefs WHERE enquiry_id IN ({q})
                               OR party_member_id IN (SELECT id FROM party_members WHERE enquiry_id IN ({q}))""", ids + ids))
    removed_at: dict[int, str] = {}
    for r in rows(c.execute(f"SELECT group_id, MAX(at) AS at FROM member_rejections WHERE rejected_enquiry_id IN ({q}) AND group_id IS NOT NULL GROUP BY group_id", ids)):
        removed_at[r["group_id"]] = r["at"]
    hist_rounds: dict[int, set] = {}
    for r in keyed:
        hist_rounds.setdefault(r["group_id"], set()).add(r["round"])
        removed_at[r["group_id"]] = max(removed_at.get(r["group_id"], ""), r["created_at"] or "")
    # rows that carry the writer's immutable ref map (refs_json, 8 Nov): exact membership of each round, no time heuristics needed
    mapped: dict[str, list[int]] = {"review_rounds": [], "group_briefs": []}
    for table in mapped:
        for r in rows(c.execute(f"SELECT id, group_id, round, created_at, refs_json FROM {table} WHERE refs_json IS NOT NULL")):
            refs = pj(r.get("refs_json"), None)
            if isinstance(refs, dict) and any(str((v or {}).get("enquiry_id")) in pids for v in refs.values()):
                mapped[table].append(r["id"])
                hist_rounds.setdefault(r["group_id"], set()).add(r["round"])
    historical = {gid for gid in set(hist_rounds) | set(removed_at) if gid not in current}

    # names in every bed allocation they appear in (current AND earlier groups; any status: the lease terms stay, the name goes)
    alloc_gids = sorted(set(current) | historical)
    for table, key in (("groups", "id"), ("group_apartments", "group_id")):
        if not alloc_gids:
            break
        for r in rows(c.execute(f"SELECT id AS rid, allocation_json FROM {table} WHERE {key} IN ({','.join('?' * len(alloc_gids))})", alloc_gids)):
            al = pj(r.get("allocation_json"), None)
            if not isinstance(al, dict):
                continue
            changed = False
            for room in al.get("rooms") or []:
                for o in room.get("occupants") or []:
                    if str(o.get("person_id") or "").split("m")[0] in pids and o.get("name") not in (None, "Former member"):
                        o["name"] = "Former member"
                        changed = True
            if changed:
                c.execute(f"UPDATE {table} SET allocation_json=? WHERE id=?", (j(al), r["rid"]))
                out["allocations_scrubbed"] += 1
    # a suggested replacement that was this person: their entry goes
    for g in rows(c.execute("SELECT id, replacement_json FROM groups WHERE replacement_json IS NOT NULL")):
        top = pj(g.get("replacement_json"), None)
        if not isinstance(top, list):
            continue
        kept = [o for o in top if not (set(map(str, (o or {}).get("added_enquiry_ids") or [])) & pids)]
        if len(kept) != len(top):
            out["replacement_entries"] += len(top) - len(kept)
            if kept:
                c.execute("UPDATE groups SET replacement_json=? WHERE id=?", (j(kept), g["id"]))
            else:
                c.execute("UPDATE groups SET replacement_json=NULL, replacement_found_at=NULL WHERE id=?", (g["id"],))

    out["note_fact_rows"] = c.execute(f"DELETE FROM note_facts WHERE enquiry_id IN ({q})", ids).rowcount   # T4c: spans / offsets of the person's own notes
    out["note_fact_rows"] += c.execute(f"DELETE FROM note_extraction_cache WHERE enquiry_id IN ({q})", ids).rowcount   # and the owned extraction-cache rows
    out["brief_rows"] += c.execute(f"DELETE FROM group_briefs WHERE enquiry_id IN ({q})", ids).rowcount
    out["brief_rows"] += c.execute(f"DELETE FROM group_briefs WHERE party_member_id IN (SELECT id FROM party_members WHERE enquiry_id IN ({q}))", ids).rowcount

    for gid, g in current.items():
        leased = g["status"] == "Leased"
        if leased and not include_leased:
            continue
        out["brief_rows"] += c.execute("DELETE FROM group_briefs WHERE group_id=?", (gid,)).rowcount   # the whole brief round: it was written about this member set
        out["review_rows"] += c.execute("DELETE FROM review_rounds WHERE group_id=?", (gid,)).rowcount
        # ai_error too: the last model/queue error text can quote the reply or the packet (a name); it has no place after the member is gone
        c.execute("""UPDATE groups SET intro_text=NULL, ai_rationale=NULL, evidence_json=NULL, review_verdict=NULL, brief_status=NULL, ready_at=NULL,
                     discount_note=NULL, rerank_note=NULL, awaiting_note=NULL, ai_error=NULL,
                     ai_status=CASE WHEN ai_status IS NULL THEN NULL ELSE 'stale' END, held_reason=?, updated_at=? WHERE id=?""",
                  ("A member's record was deleted (retention): earlier assessment, brief and introduction removed", when, gid))
        c.execute("UPDATE group_apartments SET ai_reason=NULL WHERE group_id=?", (gid,))
        out["audit_notes_cleared"] += c.execute("UPDATE audit_log SET detail=NULL WHERE entity='group' AND entity_id=? AND detail IS NOT NULL AND action<>'group_status'", (str(gid),)).rowcount
        out["groups_cleared"] += 1
        survivors = sorted(r_["enquiry_id"] for r_ in rows(c.execute(f"SELECT enquiry_id FROM group_members WHERE group_id=? AND enquiry_id NOT IN ({q})", (gid, *ids))))
        # Shared late-result protocol with the AI writers (ai_queue._apply_result / process_briefs / record_intro_draft): in the same
        # transaction as the deletion the group's member_key becomes the survivors' key and its evidence hash is cleared, so a
        # review, brief or introduction that was in flight for the OLD member set fails its member_key / evidence_hash check and
        # is never written back (the writers check both before inserting). A group with nobody left is Dissolved.
        c.execute("UPDATE groups SET member_key=?, evidence_hash=NULL WHERE id=?", (",".join(map(str, survivors)), gid))
        if not survivors and not leased and g["status"] != "Dissolved":
            c.execute("UPDATE groups SET status='Dissolved', updated_at=? WHERE id=?", (when, gid))
            out["groups_dissolved"] += 1

    for gid in sorted(historical):
        g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g or (g["status"] == "Leased" and not include_leased):
            continue
        # the last error text has no provenance (no round, no ref map) and the person was in this group: it cannot be shown
        # to be about the current members only, so it goes (status and attempt counters stay)
        c.execute("UPDATE groups SET ai_error=NULL WHERE id=? AND ai_error IS NOT NULL", (gid,))
        rnds = sorted(hist_rounds.get(gid, set()))
        until = removed_at.get(gid) or ""
        for table in mapped:   # exact: every row whose own map names the person
            hit = [i for i in mapped[table] if row(c.execute(f"SELECT 1 FROM {table} WHERE id=? AND group_id=?", (i, gid)))]
            if hit:
                n = c.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(hit))})", hit).rowcount
                out["brief_rows" if table == "group_briefs" else "review_rows"] += n
        rq = ",".join("?" * len(rnds))
        cond = " OR ".join(x for x in ([f"round IN ({rq})"] if rnds else []) + (["created_at<=?"] if until else []))
        args = list(rnds) + ([until] if until else [])
        if not cond:
            continue
        # legacy rows without a map: the rounds that included them (same round as their member brief, or written before removal)
        n_b = c.execute(f"DELETE FROM group_briefs WHERE group_id=? AND refs_json IS NULL AND ({cond})", [gid, *args]).rowcount
        n_r = c.execute(f"DELETE FROM review_rounds WHERE group_id=? AND refs_json IS NULL AND ({cond})", [gid, *args]).rowcount
        out["brief_rows"] += n_b
        out["review_rows"] += n_r
        if n_b or n_r or any(mapped.values()):
            out["historical_groups"] += 1
            # text on the group row that no surviving round backs is history about an earlier member set: it goes
            if not row(c.execute("SELECT 1 FROM review_rounds WHERE group_id=? AND evidence_hash IS NOT NULL AND evidence_hash=?", (gid, g.get("evidence_hash")))):
                c.execute("UPDATE groups SET ai_rationale=NULL, evidence_json=NULL, updated_at=? WHERE id=?", (when, gid))
                c.execute("UPDATE group_apartments SET ai_reason=NULL WHERE group_id=?", (gid,))
            if not row(c.execute("SELECT 1 FROM group_briefs WHERE group_id=? AND kind='intro' AND evidence_hash IS NOT NULL AND evidence_hash=?", (gid, g.get("evidence_hash")))):
                c.execute("UPDATE groups SET intro_text=NULL, updated_at=? WHERE id=?", (when, gid))
            if g.get("brief_status") == "ok" and not row(c.execute("SELECT 1 FROM group_briefs WHERE group_id=? AND kind='group' AND status='ok' AND evidence_hash=?", (gid, g.get("evidence_hash")))):
                c.execute("UPDATE groups SET brief_status=NULL, ready_at=NULL, updated_at=? WHERE id=?", (when, gid))
    return out


def purge_expired(c, staff: str | None = None, today: date | None = None) -> dict:
    """T5: delete people who never leased 12 months after last contact. Leased members are exempt."""
    today = today or date.today()
    cutoff = date(today.year - 1, today.month, today.day).isoformat() if not (today.month == 2 and today.day == 29) else date(today.year - 1, 2, 28).isoformat()
    with retention_guard(c, label="retention purge"):   # cleanup + delete + audit: one guarded transaction (DB lock shared with every AI writer), committed at exit
        # Eligibility is decided INSIDE the guard, on current state: someone contacted or leased while the purge waited for the
        # lock is not a victim. (Selecting before entry would delete on a stale view.)
        victims = rows(c.execute("""SELECT id FROM enquiries e WHERE substr(last_contact_at,1,10) < ? AND status <> 'Leased'
                                     AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.enquiry_id=e.id AND g.status='Leased')""", (cutoff,)))
        ids = [v["id"] for v in victims]
        scrub = forget_generated_text(c, ids)
        for i in ids:
            c.execute("DELETE FROM enquiries WHERE id=?", (i,))
        # I19(f) / GP11: saved drafts and share links expire with retention too
        drafts = c.execute("DELETE FROM form_drafts WHERE expires_at < ?", (now_iso(),)).rowcount
        c.execute("DELETE FROM mate_links WHERE expires_at < ? OR enquiry_id NOT IN (SELECT id FROM enquiries)", (now_iso(),))
        submissions.tombstone(c)  # the submission keys of deleted people keep only an opaque tombstone (no reply, no name, no link)
        audit(c, staff, "purge_expired", "enquiries", None, f"{len(ids)} deleted (cutoff {cutoff}); {drafts} expired draft(s) deleted; "
                                                           f"generated text removed: {scrub['brief_rows']} brief row(s), {scrub['review_rows']} review row(s), {scrub['groups_cleared']} group(s) cleared; "
                                                           f"{scrub['run_errors_neutralised']} earlier run error(s) neutralised; "
                                                           f"{scrub['ai_failure_sinks_neutralised']} earlier AI failure text(s) neutralised; "
                                                           f"{scrub['audit_errors_neutralised']} earlier link/submission error note(s) neutralised")
    return {"deleted": len(ids), "cutoff": cutoff, "generated_text_removed": scrub}
