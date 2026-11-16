"""HomeBot apartment enquiries (H14; change request 4, 5 Nov 2026).

HomeBot is the whole-apartment form: a complete party (or a party that is still growing and finds its own people) asks for
an apartment. It asks no sex, age, nationality, smoking or lifestyle questions, so these records live in their OWN tables
(`homebot_enquiries`, `homebot_people`) and never in `enquiries`. Everything that groups, screens or AI-assesses people
(matching.py, scheduler.py, ai_queue.py, groups, Bring-a-mate, the bar) reads `enquiries` only, so no build — old or new —
can pull a HomeBot enquiry into housemate matching. HomeBot enquiries go to MANUAL staff review: nothing here shortlists
apartments, estimates routes, sends email or messages anyone.

Validation is strict and server-side: an allow-list of fields, typed values, finite limits, and the same cross-checks the
form applies (people count, feasible partner pairs, dates, budgets). Unknown keys are refused.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from .db import retention_guard, audit, j, now_iso, pj, row, rows, transaction
from .enquiries import EMAIL_RE, MAX_NAME, MAX_TEXT, NAME_BAD_RE, ValidationError
from .institutions import institution_list
from . import rules, submissions
from . import security as sec
from . import apartment_features as feat

SOURCE = "homebot"
CHANNEL = "HomeBot form"
STATUSES = ["New", "Staff reviewing", "Options sent", "Viewing arranged", "Leased", "Closed"]
MAX_PEOPLE = 7            # 8+ is a terminal screen on the form: contact the team directly, nothing is recorded
MAX_WEEKLY_PERSON = 1_000_000   # safe numeric bound only (a positive whole dollar amount), not a product rule
MAX_WEEKLY_TOTAL = 1_000_000    # idem
MAX_NOTES = 500
MAX_PHONE = 30
MAX_OPTIONAL_TEXT = 120

PARTY_MODES = {"complete": "complete", "more": "more_declined"}
FLEX = ["Exact date", "Within a week either way", "Within two weeks either way", "Flexible by a month or more"]
WIDE_FLEX = "Flexible by a month or more"
CONTACT_PREFS = ["WhatsApp", "Phone call", "Text message", "Email"]
SLEEP_ALONE = "Whole apartment (alone)"
SLEEP = ["Own bedroom", "Shares a bed with partner", "Twin room (two single beds)", "Twin room or bunk", "Bunk bed"]
BUNK_CHOICES = {"Twin room or bunk", "Bunk bed"}
BUNK_CONDITIONS = ["Any price", "Only if cheaper"]
DOING = ["Studying", "Working", "Studying and working", "Looking for work", "Not yet decided"]
STUDY = {"Studying", "Studying and working"}
WORK = {"Working", "Studying and working"}
MAY_WORK = {"Looking for work"}
PRIMARY = ["Study", "Work"]
BEDROOMS = ["Studio", "1 bedroom", "2 bedrooms", "3 bedrooms"]
BATHROOMS = ["1 bathroom is fine", "Prefer 2 bathrooms", "Must have 2 bathrooms"]
AREAS = ["Melbourne CBD", "South Yarra", "Either"]
# H28(c) / H31 (16 Nov 2026): the travel choices are walking, bicycle and public transport. Public transport is ONE combined
# choice: the journey service cannot tell a tram from a train, bus or ferry, so nothing narrower can be measured.
TRANSPORT = ["Walk", "Bicycle", "Public transport"]
# No longer offered. An enquiry sent while these were offered keeps its answer exactly as given. Tram, Train, Bus and Car are
# never read as agreement to "Public transport" or to any other choice: those journeys are not measured and travel reads
# "needs confirmation" until the applicant has told staff how they will travel. Staff may then untick the old answer and tick
# what the applicant said; an old answer can never be added. "Bike or scooter" was always measured as a bicycle journey and
# still is (it is not widened).
TRANSPORT_RETIRED = ("Tram", "Train", "Bus", "Bike or scooter", "Car")
MAX_TRAVEL = ["5 minutes — walking distance only", "15 minutes", "30 minutes", "45 minutes", "60 minutes or more"]
EXTRAS = list(feat.LABELS)   # the 20 approved apartment and building features (8 Nov 2026), one catalogue shared with MateBot and the apartment list
EXTRAS_MAX_TOP = feat.MAX_TOP   # "Which three matter most to you?": up to three of the ticked extras, optional
VIEWING = ["In person", "Video call", "Photos and floor plan", "Either"]
HEARD = ["Friend or family", "University or college", "Education agent", "Facebook", "Google", "Instagram", "TikTok",
         "YouTube", "Snapchat", "Reddit", "Other search engine", "Realestate.com", "Education Expo", "Already an ALM tenant", "Other"]  # H13/I10
PHONE_CODES = ["+61", "+1", "+44", "+60", "+62", "+64", "+65", "+84", "+86", "+91", "other"]
RENT_KINDS = {"rent_12m": "12-month lease", "rent_6m": "6-month lease"}

ALLOWED_KEYS = {
    "is_test", "company_website", "need_more", "headcount", "headcount_now", "headcount_target", "want_help",
    "first_name", "last_name", "email", "phone_code", "phone", "contact_pref",
    "people", "bunk_condition",
    "move_in", "move_in_flex", "move_in_earliest", "move_in_latest",
    "move_out", "move_out_flex", "move_out_earliest", "move_out_latest",
    "budget_total", "budget_types", "price_guide", "bedrooms", "bathrooms", "area", "transport", "max_travel", "extras", "extras_top",
    "apartment_notes", "viewing", "heard", "consent_contact", "consent_lease", "consent_adults",
}
PERSON_KEYS = {"name", "sleep", "weekly_max", "doing", "institution", "institution_other", "place", "primary"}
JSON_FIELDS = ("budget_types", "bedrooms", "bathrooms", "transport", "extras", "extras_top", "review_flags", "answers_json")
PHONE_RE = re.compile(r"^[0-9 +().-]{6,30}$")
INTL_PHONE_RE = re.compile(r"^\+[1-9][0-9 ().-]{6,29}$")  # "other" country code: the full international number, starting with +
REF_RE = re.compile(r"^\s*homebot[-\s]?0*(\d+)\s*$", re.I)


def reference(eid) -> str:
    return f"HomeBot-{int(eid):05d}"


# ----------------------------------------------------------------------------------------------------------------- meta
def _num(v):
    """Whole dollars as int when the stored rent is a whole number (SQLite/PG may hand back floats or Decimals)."""
    if v is None:
        return None
    f = float(v)
    return int(f) if f.is_integer() else round(f, 2)


def meta(c) -> dict:
    """What the form needs from the shared data: apartment types with whole-apartment weekly ranges taken from the apartments
    table (lowest and highest rent per type and lease length), the provider list and the extra-bed charge. No counts of
    apartments, enquiries or people are disclosed (H23)."""
    types = []
    price = {}
    for r in rows(c.execute("SELECT type_code, MIN(rent_12m) lo12, MAX(rent_12m) hi12, MIN(rent_6m) lo6, MAX(rent_6m) hi6 FROM apartments GROUP BY type_code")):
        price[r["type_code"]] = r
    for code, t in rules.APT_TYPES.items():
        p = price.get(code) or {}
        types.append({
            "code": code, "label": t["label"], "bedrooms": t["bedrooms"], "bathrooms": t["bathrooms"],
            "standard_capacity": t["standard_capacity"], "max_capacity": t["standard_capacity"] + 1,
            "rent_12m": {"min": _num(p.get("lo12")), "max": _num(p.get("hi12"))} if p.get("lo12") is not None else None,
            "rent_6m": {"min": _num(p.get("lo6")), "max": _num(p.get("hi6"))} if p.get("lo6") is not None else None,
        })
    return {
        "types": types, "rent_kinds": RENT_KINDS, "extra_bed_charge_per_week": rules.EXTRA_BED_CHARGE,
        "institutions": institution_list(), "max_people": MAX_PEOPLE,
        "statuses": STATUSES, "sleep_choices": SLEEP,
        "features": feat.catalogue(), "features_max_top": feat.MAX_TOP,   # the "nice to have" list (8 Nov 2026) and the up-to-three that matter most
        # the fixed answer lists, so the staff requirement editor offers exactly the public form's choices (the validator stays authoritative)
        "choices": {"flex": FLEX, "bedrooms": BEDROOMS, "bathrooms": BATHROOMS, "areas": AREAS, "transport": TRANSPORT, "max_travel": MAX_TRAVEL,
                    "bunk_conditions": BUNK_CONDITIONS},
    }


# ----------------------------------------------------------------------------------------------------------- validation
def _s(v, limit=MAX_TEXT) -> str:
    """Text fields take strings only (None = not given). Numbers, booleans, lists and objects are rejected as a whole rather
    than quietly stringified: the form never sends them, so they are a broken or hand-made payload (strict contract)."""
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError("text expected")
    return v.strip()[: limit + 1]


def _choice(errors, a, key, options, required=True):
    v = _s(a.get(key))
    if not v:
        if required:
            errors[key] = "Please choose one."
        return None
    if v not in options:
        errors[key] = "Not one of the offered choices."
        return None
    return v


def _multi(errors, a, key, options, required=True, max_items=None):
    v = a.get(key)
    if v is None or v == "":
        v = []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        errors[key] = "A list of choices is expected."
        return []
    out = []
    for x in v:
        if not isinstance(x, str) or x not in options:
            errors[key] = "Not one of the offered choices."
            return []
        if x not in out:
            out.append(x)
    if required and not out:
        errors[key] = "Please choose at least one."
    if max_items and len(out) > max_items:
        errors[key] = f"Choose up to {max_items}."
    return out


def _int(errors, a, key, lo, hi, required=True):
    v = a.get(key)
    if v is None or v == "":
        if required:
            errors[key] = "Please enter a number."
        return None
    try:
        if isinstance(v, bool) or isinstance(v, float) and not float(v).is_integer():
            raise ValueError
        n = int(str(v).strip())
    except (TypeError, ValueError):
        errors[key] = "Whole numbers only."
        return None
    if n < lo or n > hi:
        errors[key] = f"Between {lo} and {hi}."
        return None
    return n


def _date(errors, a, key, today, required=True):
    v = _s(a.get(key), 20)
    if not v:
        if required:
            errors[key] = "Please choose a date."
        return None
    try:
        d = date.fromisoformat(v)
    except ValueError:
        errors[key] = "Use a real date (YYYY-MM-DD)."
        return None
    if d < today:
        errors[key] = "That date has already passed."
        return None
    return d


def _name(errors, a, key, required=True, limit=MAX_NAME):
    v = _s(a.get(key), limit)
    if not v:
        if required:
            errors[key] = "Please enter this."
        return None
    if len(v) > limit:
        errors[key] = f"Up to {limit} characters."
        return None
    if NAME_BAD_RE.search(v):
        errors[key] = "Unexpected characters."
        return None
    return v


def _flag(errors, a, key):
    """A required tick box: JSON true is the only affirmative (8 Nov 2026 hardening, same as MateBot's 6.1/6.3/6.4). The HomeBot
    form sends real booleans; strings such as "yes"/"true"/"on" or "false"/"no" from another caller are not an agreement."""
    if a.get(key) is True:
        return 1
    errors[key] = "Please tick this box."
    return 0


def validate(a: dict, *, today: date | None = None, keep_transport=()) -> dict:
    """Validate a HomeBot submission. Returns the clean record (top-level fields, `people` list, `flags`, `answers`) or raises
    ValidationError({field: message}). Nothing about sex, age, nationality or lifestyle is read, derived or stored.
    `keep_transport` is the transport answer already on the record when staff change an existing enquiry: a way of travelling
    that is no longer offered stays valid only if it is already there."""
    if not isinstance(a, dict):
        raise ValidationError({"form": "A JSON object is expected."})
    today = today or datetime.now(timezone.utc).astimezone().date()
    errors: dict[str, str] = {}
    unknown = sorted(k for k in a if k not in ALLOWED_KEYS)
    if unknown:
        raise ValidationError({"form": "Unexpected fields: " + ", ".join(str(k)[:40] for k in unknown[:5])})
    if "is_test" in a and not isinstance(a.get("is_test"), bool):
        raise ValidationError({"is_test": "true or false only."})  # strict: the string "false" is not a test flag
    try:
        return _validate(a, errors, today, keep_transport)
    except ValueError:
        raise ValidationError({"form": "Unexpected value types."})


def _validate(a, errors, today, keep_transport=()) -> dict:
    out: dict = {}
    # ---- party
    need = _choice(errors, a, "need_more", list(PARTY_MODES))
    want_help = None
    if need == "complete":
        n_now = _int(errors, a, "headcount", 1, MAX_PEOPLE)
        n_target = n_now
    elif need == "more":
        n_now = _int(errors, a, "headcount_now", 1, MAX_PEOPLE)
        n_target = _int(errors, a, "headcount_target", 2, MAX_PEOPLE)
        if n_now and n_target and n_target <= n_now:
            errors["headcount_target"] = "The total must be more than the people you have now."
        want_help = _choice(errors, a, "want_help", ["no"])
        if a.get("want_help") == "yes" and "want_help" in errors:
            # the "yes, help us find people" branch is a hand-off to the MateBot form: nothing is recorded here (honest limit)
            errors["want_help"] = "People who want help finding housemates use the MateBot form; HomeBot records nothing for them."
    else:
        n_now = n_target = None
    out.update(party_mode=PARTY_MODES.get(need or "", ""), headcount_now=n_now, headcount_target=n_target, want_help=want_help)

    # ---- contact
    out["first_name"] = _name(errors, a, "first_name")
    out["last_name"] = _name(errors, a, "last_name")
    email = _s(a.get("email"), 254).lower()
    if not email or len(email) > 254 or not EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid email address."
    out["email"] = email
    out["phone_code"] = _choice(errors, a, "phone_code", PHONE_CODES)
    phone = _s(a.get("phone"), MAX_PHONE)
    digits = sum(ch.isdigit() for ch in phone)
    if not phone or not PHONE_RE.match(phone) or digits < 6:
        errors["phone"] = "Digits, spaces and + only (at least 6 digits)."
    elif out["phone_code"] == "other" and (not INTL_PHONE_RE.match(phone) or digits < 7):
        errors["phone"] = "With “other” country code, type the full international number starting with + (for example +33 6 12 34 56 78)."
    out["phone"] = phone
    out["contact_pref"] = _choice(errors, a, "contact_pref", CONTACT_PREFS)

    # ---- people (H9 per-person weekly maximum, H21 per-person bed choice, H10/H11 destinations)
    people_in = a.get("people")
    people: list[dict] = []
    if not isinstance(people_in, list):
        errors["people"] = "Each person's details are expected."
    elif n_now is not None and len(people_in) != n_now:
        errors["people"] = f"Details are expected for exactly {n_now} {'person' if n_now == 1 else 'people'}."
    else:
        for i, p in enumerate(people_in, start=1):
            if not isinstance(p, dict):
                errors["people"] = "Each person's details are expected."
                break
            bad = sorted(k for k in p if k not in PERSON_KEYS)
            if bad:
                errors["people"] = f"Person {i}: unexpected fields."
                break
            pe: dict[str, str] = {}
            q = {}
            q["position"] = i
            q["name"] = out["first_name"] if i == 1 else _name(pe, p, "name", required=False)
            if len(people_in) == 1:
                q["sleep"] = SLEEP_ALONE
            else:
                q["sleep"] = _choice(pe, p, "sleep", SLEEP)
            q["weekly_max"] = _int(pe, p, "weekly_max", 1, MAX_WEEKLY_PERSON)
            q["doing"] = _choice(pe, p, "doing", DOING)
            d = q["doing"]
            inst = _choice(pe, p, "institution", [x["name"] for x in institution_list()], required=d in STUDY) if d in STUDY else None
            q["institution"] = inst
            q["institution_other"] = _name(pe, p, "institution_other", required=(inst == "Other"), limit=MAX_OPTIONAL_TEXT) if inst == "Other" else None
            # workplace / campus detail: captured as typed where given (address, suburb, building, campus); required for work,
            # optional for study (the provider list already names the campus) and job-seeking; never looked up or fabricated
            q["place"] = _name(pe, p, "place", required=d in WORK, limit=MAX_OPTIONAL_TEXT) if d != "Not yet decided" else None
            q["primary_destination"] = _choice(pe, p, "primary", PRIMARY) if d == "Studying and working" else None
            for k, v in pe.items():
                errors[f"people.{i}.{k}"] = v
            people.append(q)
    out["people"] = people

    # sleeping feasibility: partners come in pairs (and only pairs); no inference about who the partners are
    if people and len(people) > 1 and not any(k.startswith("people.") for k in errors):
        partners = sum(1 for p in people if p["sleep"] == "Shares a bed with partner")
        if partners % 2 == 1:
            errors["people"] = "“Shares a bed with partner” needs both partners to choose it (an even number of people)."
        own = sum(1 for p in people if p["sleep"] == "Own bedroom")
        if own > 3:
            errors["people"] = "No apartment has more than three bedrooms; up to three people can have their own bedroom."
        if own + partners // 2 > 3:
            errors["people"] = "Those sleeping choices need more bedrooms than any apartment has (three at most)."
    any_bunk = any(p.get("sleep") in BUNK_CHOICES for p in people)
    out["bunk_condition"] = _choice(errors, a, "bunk_condition", BUNK_CONDITIONS) if any_bunk else None
    if not any_bunk and _s(a.get("bunk_condition")):
        errors["bunk_condition"] = "Only applies when someone chose a bunk."

    # ---- dates (H28(f): move-out required; wide flexibility needs finite limits)
    mi = _date(errors, a, "move_in", today)
    out["move_in"] = mi.isoformat() if mi else None
    out["move_in_flex"] = _choice(errors, a, "move_in_flex", FLEX)
    mi_lo = mi_hi = None
    if out["move_in_flex"] == WIDE_FLEX:  # H28(f): wide flexibility still has finite earliest/latest limits
        mi_lo = _date(errors, a, "move_in_earliest", today)
        mi_hi = _date(errors, a, "move_in_latest", today)
        if mi and mi_lo and mi_hi and not (mi_lo <= mi <= mi_hi):
            errors["move_in_latest"] = "Earliest ≤ likely date ≤ latest."
    elif a.get("move_in_earliest") or a.get("move_in_latest"):
        errors["move_in_flex"] = "Earliest/latest limits only apply to wide flexibility."
    mo = _date(errors, a, "move_out", today)
    out["move_out"] = mo.isoformat() if mo else None
    out["move_out_flex"] = _choice(errors, a, "move_out_flex", FLEX)
    mo_lo = mo_hi = None
    if out["move_out_flex"] == WIDE_FLEX:
        mo_lo = _date(errors, a, "move_out_earliest", today)
        mo_hi = _date(errors, a, "move_out_latest", today)
        if mo and mo_lo and mo_hi and not (mo_lo <= mo <= mo_hi):
            errors["move_out_latest"] = "Earliest ≤ likely date ≤ latest."
    elif a.get("move_out_earliest") or a.get("move_out_latest"):
        errors["move_out_flex"] = "Earliest/latest limits only apply to wide flexibility."
    # feasible chronological pairs: the likely dates in order, and the earliest possible move-out after the earliest possible
    # move-in (windows may overlap; every combination need not be in order)
    if mi and mo and mo <= mi:
        errors["move_out"] = "Move-out must be after move-in."
    if mi and mo and (mo_lo or mo) <= (mi_lo or mi):
        errors["move_out_earliest"] = "The earliest move-out must be after the earliest move-in."
    # A6 price guide: derived from the dates (rules.rate_kind); the form sends the kind it showed, which must agree
    guide_kind = None
    if mi and mo:
        guide_kind = "rent_6m" if rules.rate_kind(mi, mo) == "6m" else "rent_12m"
        sent = _s(a.get("price_guide"), 20)
        if sent and sent != guide_kind:
            errors["price_guide"] = "The price guide shown does not match the dates (reload the page and try again)."
    out["price_guide"] = guide_kind
    out.update(move_in_earliest=mi_lo and mi_lo.isoformat(), move_in_latest=mi_hi and mi_hi.isoformat(),
               move_out_earliest=mo_lo and mo_lo.isoformat(), move_out_latest=mo_hi and mo_hi.isoformat())

    # ---- budget and apartment
    out["budget_total_weekly"] = _int(errors, a, "budget_total", 1, MAX_WEEKLY_TOTAL)
    out["budget_types"] = _multi(errors, a, "budget_types", list(rules.APT_TYPES), required=True)
    out["bedrooms"] = _multi(errors, a, "bedrooms", BEDROOMS, required=True)
    baths = _multi(errors, a, "bathrooms", BATHROOMS, required=True)
    if "Must have 2 bathrooms" in baths and len(baths) > 1:
        errors["bathrooms"] = "“Must have 2 bathrooms” cannot be combined with the other choices."
    out["bathrooms"] = baths
    out["area"] = _choice(errors, a, "area", AREAS)
    kept = [m for m in TRANSPORT_RETIRED if isinstance(keep_transport, (list, tuple)) and m in keep_transport]
    out["transport"] = _multi(errors, a, "transport", TRANSPORT + kept, required=True)
    given = a.get("transport")
    if "transport" in errors and any(m in ([given] if isinstance(given, str) else given if isinstance(given, list) else [])
                                     for m in TRANSPORT_RETIRED if m not in kept):
        errors["transport"] = "The travel choices are now walk, bicycle and public transport. Please choose from those."
    out["max_travel"] = _choice(errors, a, "max_travel", MAX_TRAVEL)
    out["extras"], out["extras_top"] = feat.clean_wanted(a.get("extras"), top=a.get("extras_top"), errors=errors, key="extras", top_key="extras_top")
    notes = _s(a.get("apartment_notes"), MAX_NOTES)
    if len(notes) > MAX_NOTES:
        errors["apartment_notes"] = f"Up to {MAX_NOTES} characters."
    out["apartment_notes"] = notes or None
    out["viewing"] = _choice(errors, a, "viewing", VIEWING)
    out["heard"] = _choice(errors, a, "heard", HEARD, required=False)
    out["consent_contact"] = _flag(errors, a, "consent_contact")
    out["consent_lease"] = _flag(errors, a, "consent_lease")
    out["consent_adults"] = _flag(errors, a, "consent_adults")
    if errors:
        raise ValidationError(errors)

    # ---- plain-language staff flags (facts from the answers, not judgements; none of them selects or excludes anything)
    flags = []
    if out["party_mode"] == "more_declined":
        flags.append(f"Party still growing: {n_now} now, {n_target} wanted — they are finding their own people; no MateBot matching is involved.")
    if out["apartment_notes"]:
        flags.append("Notes to read — the applicant wrote something about the apartment; it may include an essential need. Read before suggesting anything.")
    partners = sum(1 for p in people if p["sleep"] == "Shares a bed with partner")
    if partners >= 4:
        flags.append(f"Partner pairing to confirm: {partners} people chose “shares a bed with partner”, so there are {partners // 2} couples, but the form does not record who is with whom. Confirm the pairs with the applicant; nothing has been assumed.")
    per_person = sum(p["weekly_max"] for p in people)
    if per_person != out["budget_total_weekly"]:
        flags.append(f"Budget check: the individual maximums add up to ${per_person}/week; the whole-apartment maximum given is ${out['budget_total_weekly']}/week.")
    out["flags"] = flags
    return out


# ---------------------------------------------------------------------------------------------------------------- store
def create(c, a: dict, *, is_test: bool, submission_key: str | None = None, on_inserted=None) -> int:
    """Validate and store one HomeBot enquiry and its people in ONE transaction (all rows or none). `on_inserted(c, eid)` runs
    inside the same transaction (the submission-key engine marks the key saved there; if it raises, everything rolls back).
    Returns the new id. Raises ValidationError."""
    is_test = bool(is_test) or sec.pilot_gate_enabled()   # controlled pilot: nothing new is ever stored as real data
    clean = validate(a)
    now = now_iso()
    answers = {k: v for k, v in clean.items() if k not in ("flags",)}
    answers["guide_snapshot"] = guide_snapshot(c, clean["budget_types"], clean["price_guide"])  # what the applicant saw, as of now
    with transaction(c):
        cur = c.execute(
            "INSERT INTO homebot_enquiries(is_test, source, channel, status, created_at, updated_at, last_contact_at, submission_key, "
            "first_name, last_name, email, phone_code, phone, contact_pref, party_mode, headcount_now, headcount_target, "
            "move_in, move_in_flex, move_in_earliest, move_in_latest, move_out, move_out_flex, move_out_earliest, move_out_latest, "
            "budget_total_weekly, budget_types, bedrooms, bathrooms, area, transport, max_travel, extras, extras_top, apartment_notes, bunk_condition, "
            "viewing, heard, consent_contact, consent_lease, consent_adults, review_flags, answers_json) "
            "VALUES (?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?)",
            (1 if is_test else 0, SOURCE, CHANNEL, "New", now, now, now, submission_key,
             clean["first_name"], clean["last_name"], clean["email"], clean["phone_code"], clean["phone"], clean["contact_pref"],
             clean["party_mode"], clean["headcount_now"], clean["headcount_target"],
             clean["move_in"], clean["move_in_flex"], clean["move_in_earliest"], clean["move_in_latest"],
             clean["move_out"], clean["move_out_flex"], clean["move_out_earliest"], clean["move_out_latest"],
             clean["budget_total_weekly"], j(clean["budget_types"]), j(clean["bedrooms"]), j(clean["bathrooms"]), clean["area"],
             j(clean["transport"]), clean["max_travel"], j(clean["extras"]), j(clean["extras_top"]), clean["apartment_notes"], clean["bunk_condition"],
             clean["viewing"], clean["heard"], clean["consent_contact"], clean["consent_lease"], clean["consent_adults"],
             j(clean["flags"]), j(answers)))
        eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        for p in clean["people"]:
            c.execute("INSERT INTO homebot_people(enquiry_id, position, name, sleep, weekly_max, doing, institution, institution_other, place, primary_destination) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (eid, p["position"], p["name"], p["sleep"], p["weekly_max"], p["doing"], p["institution"], p["institution_other"], p["place"], p["primary_destination"]))
        if on_inserted:
            on_inserted(c, eid)
    audit(c, "HomeBot form", "homebot.create", "homebot_enquiry", eid, j({"is_test": bool(is_test), "people": len(clean["people"])}))
    return eid


def _hydrate(e: dict) -> dict:
    for k in JSON_FIELDS:
        e[k] = pj(e.get(k))
    e["reference"] = reference(e["id"])
    e["review_flags"] = e["review_flags"] or []
    return e


def get_record(c, eid: int) -> dict:
    """The enquiry record itself (fields, people, history, summary) WITHOUT the matching note. The shortlist module's basis
    reads (capture under the guard, freshness check) use this, so that a freshness check never re-enters the matching note
    (get -> matching_note -> review state -> freshness -> get ...; lead's H7/H14 finding, 9 Nov 2026)."""
    e = row(c.execute("SELECT * FROM homebot_enquiries WHERE id=?", (eid,)))
    if not e:
        raise KeyError("HomeBot enquiry not found")
    e = _hydrate(e)
    e["people"] = rows(c.execute("SELECT position, name, sleep, weekly_max, doing, institution, institution_other, place, primary_destination "
                                 "FROM homebot_people WHERE enquiry_id=? ORDER BY position", (eid,)))
    e["history"] = rows(c.execute("SELECT at, staff_member, action, detail FROM audit_log WHERE entity='homebot_enquiry' AND entity_id=? ORDER BY id DESC LIMIT 50", (str(eid),)))
    e["summary"] = summary(e)
    return e


def get(c, eid: int) -> dict:
    e = get_record(c, eid)
    from . import homebot_shortlist  # lazy: homebot_shortlist imports this module
    e["matching"] = homebot_shortlist.matching_note(c, eid)   # T3(a) H2: honest state (switched off / being prepared / ready for review)
    return e


def list_(c, dataset: str = "real", q: str = "", status: str = "", flag: str = "") -> dict:
    where = ["is_test=?"]
    args: list = [1 if dataset == "test" else 0]
    if status and status in STATUSES:
        where.append("status=?")
        args.append(status)
    if q:
        m = REF_RE.match(q)
        if m:  # "HomeBot-00012", "homebot 12"
            where.append("id=?")
            args.append(int(m.group(1)))
        else:
            like = f"%{q.strip().lower()}%"
            where.append("(LOWER(first_name) LIKE ? OR LOWER(last_name) LIKE ? OR LOWER(email) LIKE ? OR phone LIKE ? OR CAST(id AS TEXT) LIKE ?)")
            args += [like, like, like, like, like]
    if flag == "flagged":
        where.append("review_flags<>'[]'")
    sql = "SELECT id, is_test, status, staff_member, created_at, last_contact_at, first_name, last_name, email, party_mode, headcount_now, headcount_target, " \
          "move_in, move_out, budget_total_weekly, bedrooms, area, review_flags FROM homebot_enquiries WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 500"
    items = [_hydrate_list(r) for r in rows(c.execute(sql, args))]
    total = row(c.execute("SELECT COUNT(*) n FROM homebot_enquiries WHERE " + " AND ".join(where), args))["n"]
    return {"total": total, "items": items}


def _hydrate_list(r: dict) -> dict:
    r["bedrooms"] = pj(r.get("bedrooms")) or []
    r["review_flags"] = pj(r.get("review_flags")) or []
    r["reference"] = reference(r["id"])
    return r


def update(c, eid: int, patch: dict, staff: str) -> dict:
    """Staff may change the status, the staff member and the notes — nothing else. HomeBot statuses only: the MateBot statuses
    (Matched, In group, Bring-a-mate…) do not exist here, so a HomeBot enquiry can never be marked as auto-eligible."""
    bad = sorted(k for k in patch if k not in ("status", "staff_member", "staff_notes"))
    if bad:
        raise ValidationError({"form": "Only status, staff_member and staff_notes can be changed."})
    e = get(c, eid)
    sets, args = [], []
    if "status" in patch:
        st = str(patch.get("status") or "").strip()
        if st not in STATUSES:
            raise ValidationError({"status": "Not a HomeBot status."})
        sets.append("status=?"); args.append(st)
    if "staff_member" in patch:
        sm = str(patch.get("staff_member") or "").strip()[:MAX_NAME]
        if NAME_BAD_RE.search(sm):
            raise ValidationError({"staff_member": "Unexpected characters."})
        sets.append("staff_member=?"); args.append(sm or None)
    if "staff_notes" in patch:
        sn = str(patch.get("staff_notes") or "").strip()
        if len(sn) > 5000:
            raise ValidationError({"staff_notes": "Up to 5000 characters."})
        sets.append("staff_notes=?"); args.append(sn or None)
    if not sets:
        return e
    sets.append("updated_at=?"); args.append(now_iso())
    args.append(eid)
    # under the retention guard (not a bare transaction): a status change to Leased/Closed is matching basis — on PostgreSQL it
    # must serialise with a HomeBot options publication running on another connection (homebot_shortlist._store)
    with retention_guard(c, "homebot enquiry update"):
        c.execute("UPDATE homebot_enquiries SET " + ", ".join(sets) + " WHERE id=?", args)
        audit(c, staff, "homebot.update", "homebot_enquiry", eid, j({k: patch[k] for k in patch if k != "staff_notes"}))
    return get(c, eid)


def contact(c, eid: int, staff: str, note: str = "") -> dict:
    get(c, eid)
    now = now_iso()
    with transaction(c):
        c.execute("UPDATE homebot_enquiries SET last_contact_at=?, updated_at=? WHERE id=?", (now, now, eid))
    audit(c, staff, "homebot.contact", "homebot_enquiry", eid, (note or "")[:MAX_TEXT] or None)
    return get(c, eid)


def _scrub_audit(c, ids: list) -> None:
    """When a HomeBot enquiry is removed, the free-text details of its audit rows (contact notes may name or quote the customer)
    go too; the fixed action names and timestamps stay as non-personal history. Runs inside the caller's transaction."""
    for eid in ids:
        c.execute("UPDATE audit_log SET detail=NULL WHERE entity='homebot_enquiry' AND entity_id=? AND detail IS NOT NULL", (str(eid),))


def delete(c, eid: int, staff: str) -> None:
    from . import homebot_shortlist
    with retention_guard(c, "homebot delete"):
        get(c, eid)
        homebot_shortlist.forget(c, [eid])   # H2 versions, jobs, shortlists, rejections and their attention alerts go with it
        c.execute("DELETE FROM homebot_email_outbox WHERE enquiry_id=?", (eid,))
        c.execute("DELETE FROM homebot_people WHERE enquiry_id=?", (eid,))
        c.execute("DELETE FROM homebot_enquiries WHERE id=?", (eid,))
        submissions.tombstone(c, [eid], homebot=True)  # a retry of the form's key cannot recreate the deleted enquiry
        _scrub_audit(c, [eid])
        audit(c, staff, "homebot.delete", "homebot_enquiry", eid, None)


def delete_test_data(c) -> list[int]:
    """Called from seed.delete_test_data (inside its guard): remove TEST HomeBot enquiries (and their people) and tombstone THEIR keys.
    G-SL2: the tombstone is explicit to the ids captured here (no orphan sweep: a REAL key whose enquiry is gone is not test data).
    Returns the ids removed so the caller can check the set it captured is the set that went."""
    from . import homebot_shortlist
    ids = [int(r["id"]) for r in rows(c.execute("SELECT id FROM homebot_enquiries WHERE is_test=1 ORDER BY id"))]
    if ids:
        homebot_shortlist.forget(c, ids)
        ph = ",".join("?" * len(ids))
        c.execute(f"DELETE FROM homebot_email_outbox WHERE enquiry_id IN ({ph})", ids)
        c.execute(f"DELETE FROM homebot_people WHERE enquiry_id IN ({ph})", ids)
        c.execute(f"DELETE FROM homebot_enquiries WHERE id IN ({ph})", ids)
        _scrub_audit(c, ids)
    submissions.tombstone(c, ids, homebot=True)
    return ids


def retention_cutoff(today: date | None = None) -> str:
    """T5: the same calendar-year cutoff as enquiries.purge_expired (one year before today; 29 Feb → 28 Feb)."""
    today = today or date.today()
    if today.month == 2 and today.day == 29:
        return date(today.year - 1, 2, 28).isoformat()
    return date(today.year - 1, today.month, today.day).isoformat()


def retention_due(c, today: date | None = None) -> list[dict]:
    """HomeBot enquiries the T5 purge would remove: last contact over a year ago and never leased (Leased is exempt)."""
    return rows(c.execute("SELECT id, first_name, last_name, is_test, status, last_contact_at FROM homebot_enquiries "
                          "WHERE substr(last_contact_at,1,10) < ? AND status <> 'Leased' ORDER BY id", (retention_cutoff(today),)))


def purge_expired(c, today: date | None = None) -> int:
    """T5 retention, same convention as the MateBot purge: enquiries whose last contact is over a calendar year ago are removed,
    Leased enquiries are exempt, keys are tombstoned. Selection, deletion and audit share one retention guard."""
    from . import homebot_shortlist
    with retention_guard(c, "homebot purge"):
        ids = [r["id"] for r in retention_due(c, today)]
        if ids:
            homebot_shortlist.forget(c, ids)
        for eid in ids:
            c.execute("DELETE FROM homebot_email_outbox WHERE enquiry_id=?", (eid,))
            c.execute("DELETE FROM homebot_people WHERE enquiry_id=?", (eid,))
            c.execute("DELETE FROM homebot_enquiries WHERE id=?", (eid,))
        n = len(ids)
        if n:
            submissions.tombstone(c, ids, homebot=True)
            _scrub_audit(c, ids)
        if n:
            audit(c, "system", "homebot.purge", "homebot_enquiry", None, j({"removed": n}))
    return n


def stats(c) -> dict:
    out = {}
    for ds, t in (("", 0), ("_test", 1)):
        out["homebot_new" + ds] = row(c.execute("SELECT COUNT(*) n FROM homebot_enquiries WHERE is_test=? AND status='New'", (t,)))["n"]
        out["homebot_open" + ds] = row(c.execute("SELECT COUNT(*) n FROM homebot_enquiries WHERE is_test=? AND status NOT IN ('Leased','Closed')", (t,)))["n"]
        out["homebot_flagged" + ds] = row(c.execute("SELECT COUNT(*) n FROM homebot_enquiries WHERE is_test=? AND review_flags<>'[]'", (t,)))["n"]
    return out


# -------------------------------------------------------------------------------------------------------------- summary
def _dates(d, flex, lo, hi):
    s = f"{d} ({flex})"
    if lo or hi:
        s += f" — earliest {lo or '?'}, latest {hi or '?'}"
    return s


def summary(e: dict) -> list[dict]:
    """The complete typed Q&A as plain sections for the staff detail (labels match the form; nothing is inferred)."""
    a = e.get("answers_json") or {}
    pm = "Complete — everyone is known" if e["party_mode"] == "complete" else f"Still growing — {e['headcount_now']} now, {e['headcount_target']} in total; they are finding their own people (declined MateBot help)"
    snap = a.get("guide_snapshot") or []
    if snap:
        guide = "; ".join(f"{g['label']}: {_range(g)}" for g in snap)
    else:
        guide = ", ".join(rules.APT_TYPES.get(t, {}).get("label", t) for t in (e.get("budget_types") or [])) + " (ranges not recorded)"
    code = e.get("phone_code") or ""
    phone = e["phone"] if code == "other" else f"{code} {e['phone']}".strip()
    return [
        {"title": "Who is enquiring", "rows": [
            ["Name", f"{e['first_name']} {e['last_name']}"], ["Email", e["email"]],
            ["Phone", phone], ["Preferred contact", e["contact_pref"]],
            ["Party", pm], ["People now", e["headcount_now"]], ["People in total", e["headcount_target"]]]},
        {"title": "Dates", "rows": [
            ["Move in", _dates(e["move_in"], e["move_in_flex"], e.get("move_in_earliest"), e.get("move_in_latest"))],
            ["Move out", _dates(e["move_out"], e["move_out_flex"], e.get("move_out_earliest"), e.get("move_out_latest"))]]},
        {"title": "Budget", "rows": [
            ["Whole-apartment maximum", f"${e['budget_total_weekly']:,} per week"],
            ["Price guide shown (from the dates)", RENT_KINDS.get(a.get("price_guide"), a.get("price_guide") or "—")],
            ["Selected price-guide options (guide recorded at submission, not a quote or an eligibility check)", guide or "—"],
            ["Sum of individual maximums", f"${sum(p['weekly_max'] for p in e.get('people', [])):,} per week"]]},
        {"title": "Apartment", "rows": [
            ["Bedrooms", ", ".join(e.get("bedrooms") or [])], ["Bathrooms", ", ".join(e.get("bathrooms") or [])],
            ["Area", e["area"]], ["Getting around", ", ".join(e.get("transport") or [])], ["Longest acceptable trip", e["max_travel"]],
            ["Features wanted (wishes, not filters)", ", ".join(e.get("extras") or []) or "None chosen"],
            ["Matter most (up to three)", ", ".join(e.get("extras_top") or []) or "None chosen"],
            ["Bunk condition", e.get("bunk_condition") or "No bunks chosen"],
            ["Notes about the apartment", e.get("apartment_notes") or "—"]]},
        {"title": "Viewing and contact", "rows": [
            ["Viewing", e["viewing"]], ["Heard about us", e.get("heard") or "—"]]},
        {"title": "Confirmed on the form", "rows": [[t, "Yes"] for t in CONSENT_TEXT]},
    ]


CONSENT_TEXT = [
    "The ALM team may contact me about apartments using the details above.",
    "I understand ALM leases are for the whole apartment, every adult living there is named on the lease, apartments are non-smoking indoors, no pets, and internet is not included in the rent.",
    "Everyone who will live in the apartment will be 18 or older at move-in.",
]


def _range(g: dict) -> str:
    if g.get("min") is None:
        return "no price listed"
    return f"${g['min']:,}–${g['max']:,} per week ({RENT_KINDS.get(g.get('kind'), g.get('kind'))})"


def guide_snapshot(c, type_codes: list, kind: str | None) -> list[dict]:
    """The whole-apartment weekly ranges for the chosen types as the shared data has them right now — kept with the enquiry so
    staff see the guide the applicant saw. A guide only: never a rent quote and never used to qualify or exclude anyone."""
    kind = kind or "rent_12m"
    by_code = {t["code"]: t for t in meta(c)["types"]}
    out = []
    for code in type_codes:
        t = by_code.get(code) or {}
        r = t.get(kind) or {}
        out.append({"code": code, "label": t.get("label", code), "kind": kind, "min": r.get("min"), "max": r.get("max")})
    return out


def already_submitted_message(eid) -> str:
    return (f"This enquiry has already been submitted (reference {reference(eid)}). The later changes were not saved and no second "
            "enquiry was made. If something needs changing, please contact the team and quote your reference.")
