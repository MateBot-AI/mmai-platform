"""Matching engine (T4): apartment-first candidate generation -> rule-based hard filters (rules.py)
-> preference scoring -> AI compatibility assessment -> ranked proposals saved as groups."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import re
from datetime import date, datetime
from typing import Optional

from . import access_facts, ai, ai_queue, db, rules
from . import homebot_match as hm   # GP14: building_key(address) - the one building parser both bots use
from .db import audit, j, maybe_commit, now_iso, pj, row, rows
from .institutions import find_institution

log = logging.getLogger("mmai.matching")

POOL_STATUSES = ("New", "Proposed in group")
AWAITING = "Awaiting replacement"  # GP10: group (and member enquiry) status while a rejected member has no rule-passing replacement
POOL_SQL = "is_test=? AND status IN (%s) AND barred_at IS NULL" % ",".join("?" * len(POOL_STATUSES))  # GP9(c): barred people are in no run and no replacement
SIZE_LABEL = {"Studio": "Studio", "1BR": "1 bedroom", "2B1B": "2 bedrooms", "2B2B": "2 bedrooms", "3B": "3 bedrooms"}


def d(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s[:10]) if s else None


# ---------------------------------------------------------------------------
# Row -> domain objects
# ---------------------------------------------------------------------------
def apartment_from_row(a: dict) -> rules.Apartment:
    beds = [rules.parse_bedding(a[k]) for k in ("bedroom1", "bedroom2", "bedroom3") if a.get(k)]
    return rules.Apartment(
        id=str(a["id"]), address=a["address"], type_code=a["type_code"], rent_12m=a["rent_12m"], rent_6m=a["rent_6m"],
        available_from=d(a.get("available_from")), bedrooms=beds, min_lease_months=a.get("min_lease_months") or rules.DEFAULT_MIN_LEASE_MONTHS,
        bills_included=bool(a.get("bills_included")), city=a.get("city") or "Melbourne", suburb=a.get("suburb") or "", lat=a.get("lat"), lon=a.get("lon"),
        building_key=building_identity(a))


def _lst(v) -> list:
    """JSON-list column, whether still a TEXT cell or already parsed (get_enquiry)."""
    return v if isinstance(v, list) else pj(v)


def unit_from_row(e: dict, members: list[dict]) -> rules.Unit:
    rooms = set(_lst(e["rooms"]))
    arrangements = set()
    for r in rooms:
        if r.startswith("Own"):
            arrangements.add("Own room")
        elif "one other" in r:
            arrangements.add("Share with one other")
        elif "two others" in r:
            arrangements.add("Share with two others")
        elif r.startswith("Couple"):
            arrangements.add("Couple sharing one bed")
    party = e["party"]
    kind = "single" if party == "Alone" else ("couple" if party.startswith("Couple") else "friends")
    # I16(f): only the enquirer creates a couple: 3.8 "Couple sharing one bed" (enquirer + Person 2), or an explicit 3.10 answer
    # for a "With friends" party naming the two people in each couple. Party size or 3.5 ticks alone never make a couple.
    from .enquiries import normalise_couples
    _, couple_pairs = normalise_couples({"party": party, "couples_answer": e.get("couples_answer"), "couples": _lst(e.get("couples_json"))})
    couple_of: dict[str, str] = {}
    for ci, pr in enumerate(couple_pairs, start=1):
        if len(pr) == 2:
            for ref in pr:
                couple_of[ref] = str(e["id"]) if kind == "couple" else f"{e['id']}c{ci}"
    if couple_of:
        arrangements.add("Couple sharing one bed")  # a stated couple shares one bed, whatever else was ticked in 3.5
    from .countries import region_of
    from .db import pj as _pj
    sex_pref = (e.get("sex_pref") or "").strip() or None
    age_pref = tuple(_lst(e.get("age_pref")))
    own_region_pref = (e.get("own_region") or "").strip() or None
    avoid_answer = (e.get("avoid_answer") or "").strip() or None
    avoid_regions = tuple(_lst(e.get("avoid_regions")))
    avoid_countries = tuple(_lst(e.get("avoid_countries")))
    languages = tuple(_lst(e.get("languages")))
    common = dict(
        unit_id=str(e["id"]), birth_country=None, budget_max=e["budget_max"], arrangements=arrangements,
        bunk_ok=bool(e["bunk_ok"]), smokes=(e["smoke"] != "No"), smoke_ok=(e["smoke_ok"] == "Yes"),
        sex_pref=sex_pref, age_pref=age_pref, own_region_pref=own_region_pref,
        avoid_answer=avoid_answer, avoid_regions=avoid_regions, avoid_countries=avoid_countries,
        languages=languages, live_country=e.get("live_country"),
    )
    lead = rules.Person(id=f"{e['id']}", name=e["first_name"], is_lead=True, couple_id=couple_of.get("lead"),
                        ident_name=f"{e.get('first_name') or ''} {e.get('last_name') or ''}", ident_email=e.get("email"), **common)
    lead.birth_country = e.get("birth_country")
    lead.own_sex = (e.get("intro_sex") or "").strip() or None
    lead.birth_region = region_of(lead.birth_country) if lead.birth_country else None
    people = [lead]
    size = int(e.get("party_size") or 1) if kind != "single" else 1
    if kind == "couple":
        size = 2
    others = members[: max(0, size - 1)]
    for i in range(max(0, size - 1)):
        m = others[i] if i < len(others) else {"name": f"{e['first_name']}'s {'partner' if kind == 'couple' else 'friend'} {i+1}"}
        # I16(b): each party member has their own country of birth; it is carried for identity (GP2 introductions, barred
        # records) only and is not a matching factor under the launch scope (8 Nov 2026). I16(d): the form-filler's practical
        # answers (budget, dates, 3.5, 5.5/5.6, lifestyle) apply to the whole party, inherited from the lead (`common`); members
        # are never asked separately. Sex/DOB are not collected for anyone (I8). I16(e): an unrecognised member country stays
        # None (staff confirm later); members saved before 26 Oct 2026 were never asked and take the lead's country.
        p = rules.Person(id=f"{e['id']}m{i+1}", name=m["name"], is_lead=False, couple_id=couple_of.get(f"m{i+1}"),
                         ident_name=m.get("name"), ident_email=m.get("email"), **common)
        p.birth_country = m.get("birth_country") or (None if m.get("birth_country_raw") else e.get("birth_country"))
        p.own_sex = (m.get("intro_sex") or "").strip() or None
        p.birth_region = region_of(p.birth_country) if p.birth_country else None
        people.append(p)
    flex_out = e["move_out_flex"]
    # I11(d): the declared move-in date carries only while the declaration is valid for the current party and date (see
    # enquiries.adults_status); 'renew'/'missing' → None → check_group holds the group for staff. Never derived from a DOB.
    from .enquiries import adults_status  # local import: enquiries imports rules/matching helpers at module level
    adults_for = d(e.get("adults_confirmed_move_in")) if adults_status(e, members) == "confirmed" else None
    return rules.Unit(
        apartment_exclusions=exclusions_from_json(e.get("apartment_exclusions_json")),   # GP14: absent column / NULL → none
        id=str(e["id"]), kind=kind, people=people, city=e.get("city") or "Melbourne", apt_sizes=set(_lst(e["apt_sizes"])),
        mate_max=int(e["mate_max"]), move_in=d(e["move_in"]), move_in_flex=rules.flex_days(e["move_in_flex"]),
        move_out=d(e["move_out"]), move_out_flex=rules.flex_days(flex_out), open_to_extending=(flex_out == "Open to extending"), extra=e,
        adults_confirmed_move_in=adults_for)


# ---------------------------------------------------------------------------
# Preference scoring (step 2). 0-100. Never excludes anyone.
# ---------------------------------------------------------------------------
def _km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def _campus(e: dict):
    inst = find_institution(e.get("school") or "")
    return inst if inst and inst.get("lat") else None


ORDINAL = {"routine": ["Early riser", "Varies", "Night owl"], "tidy": ["Very tidy", "Average", "Relaxed"],
           "social": ["Quiet and private", "Friendly but independent", "Very social"], "guests": ["Rarely", "Sometimes", "Often"]}


def _sim(key, a, b):
    order = ORDINAL[key]
    if not a or not b or a not in order or b not in order:
        return 0.6
    diff = abs(order.index(a) - order.index(b))
    return {0: 1.0, 1: 0.55, 2: 0.1}[diff]


# 3.7 first choice (form wording) -> the allocation's arrangement label (rules._arrangement_for). Exact label comparison: the former
# prefix test (`want.split()[0] in got`) counted "Share with two others" as a hit for a "Share a room with one other person" first
# choice because both start with "Share" (10 Nov 2026, reviewer finding). Weight and hit/miss values are unchanged (1.0 / 0.4).
_FIRST_CHOICE_ARRANGEMENT = {"Own room": "Own room", "Share a room with one other person": "Share with one other", "Share with one other": "Share with one other",
                             "Share a room with two others": "Share with two others", "Share with two others": "Share with two others",
                             "Couple sharing one bed": "Couple sharing one bed"}


def first_choice_arrangement(room_first) -> str | None:
    """The arrangement label a 3.7 first choice must match exactly in the allocation; None for an unknown / empty answer (counts
    as not met, as before: an unrecognised first choice never scores as a hit)."""
    w = str(room_first or "").strip()
    if w in _FIRST_CHOICE_ARRANGEMENT:
        return _FIRST_CHOICE_ARRANGEMENT[w]
    low = w.lower()
    if low.startswith("own"):
        return "Own room"
    if low.startswith("couple"):
        return "Couple sharing one bed"
    if low.startswith("share") and "two" in low:
        return "Share with two others"
    if low.startswith("share") and "one" in low:
        return "Share with one other"
    return None


def preference_score(apt: rules.Apartment, units: list[rules.Unit], check: rules.GroupCheck) -> tuple[float, dict]:
    leads = [u.extra for u in units]
    n_people = sum(u.size for u in units)
    parts: dict[str, float] = {}
    # 4.6 preferred areas only. M4 / GP6(d) (16 Nov 2026): distance to the place of study and the 4.7 travel time are NOT
    # part of this score. Travel is checked last, on the shortlisted apartments, from measured journeys (matebot_travel);
    # the former straight-line distance, its fixed distance per travel-time answer and its assumed 30 minutes for a blank
    # answer are gone. The weight of this part is unchanged, not re-tuned.
    area_fit = []
    for e in leads:
        areas = pj(e.get("areas"))
        if areas and "No preference" not in areas:
            area_fit.append(1.0 if apt.suburb in areas else 0.3)
    parts["areas"] = sum(area_fit) / len(area_fit) if area_fit else 0.6
    # lease-length alignment (2.4 spread)
    outs = [u.move_out for u in units]
    spread = (max(outs) - min(outs)).days if len(outs) > 1 else 0
    parts["dates"] = max(0.0, 1 - spread / 90)
    # vacancy gap: ALM wants apartments filled from their availability date (A9(d), GP4)
    gap = (check.lease_start - apt.available_from).days if check.lease_start and apt.available_from else 0
    parts["vacancy"] = max(0.0, 1 - gap / 60)
    # 3.3 ideal housemates
    ideal = [abs((n_people - 1) - int(e["mate_ideal"])) for e in leads if e.get("mate_ideal")]
    parts["group_size"] = max(0.0, 1 - (sum(ideal) / len(ideal)) / 3) if ideal else 0.7
    # 3.7 first-choice room arrangement
    hits = []
    if check.allocation:
        for r in check.allocation:
            for o in r["occupants"]:
                e = next((x for x in leads if str(x["id"]) == o["person_id"]), None)
                if e and e.get("room_first"):
                    want = first_choice_arrangement(e["room_first"])
                    hits.append(1.0 if want is not None and want in (o.get("arrangement") or []) else 0.4)
    parts["room_choice"] = sum(hits) / len(hits) if hits else 0.7
    # pairwise practical household habits + restored origin/language/age preferences (Bill, 17 Nov 2026 supersession)
    pair_scores = {"routine": [], "tidy": [], "social": [], "guests": [], "diet": []}
    for i, a in enumerate(leads):
        for b in leads[i + 1:]:
            for k in ("routine", "tidy", "social", "guests"):
                pair_scores[k].append(_sim(k, a.get(k), b.get(k)))
            da, db_ = a.get("diet") or "None", b.get("diet") or "None"
            pair_scores["diet"].append(1.0 if da == db_ or "None" in (da, db_) else 0.7)
    for k, v in pair_scores.items():
        parts[k] = sum(v) / len(v) if v else 0.7

    # --- 17 Nov 2026 preference components (soft only; sex is hard-filtered in pair_conflict) ---
    people = [p for u in units for p in u.people]
    # Country / region fit (own_region preference + avoid lists)
    country_scores = []
    for p in people:
        if not p.own_region_pref or p.own_region_pref == "Don’t mind":
            country_scores.append(0.7)
            continue
        others = [o for o in people if o.id != p.id]
        if not others:
            country_scores.append(0.7)
            continue
        same = 0
        for o in others:
            # avoid list: hard penalty if other is from an avoided country/region
            if p.avoid_answer and "happy to live with anyone" not in (p.avoid_answer or "").lower():
                if (o.birth_country and o.birth_country in p.avoid_countries) or \
                   (o.birth_region and o.birth_region in p.avoid_regions):
                    same -= 1.0
                    continue
            if p.own_region_pref == "Prefer to":
                if p.birth_country and o.birth_country and p.birth_country == o.birth_country:
                    same += 1.0
                elif p.birth_region and o.birth_region and p.birth_region == o.birth_region:
                    same += 0.7
                else:
                    same += 0.2
            elif p.own_region_pref == "Prefer a mix":
                if p.birth_country and o.birth_country and p.birth_country != o.birth_country:
                    same += 1.0
                else:
                    same += 0.4
        country_scores.append(max(0.0, min(1.0, same / len(others))))
    parts["country"] = sum(country_scores) / len(country_scores) if country_scores else 0.7

    # Language overlap
    lang_scores = []
    for i, a in enumerate(people):
        for b in people[i+1:]:
            la, lb = set(a.languages or ()), set(b.languages or ())
            if not la or not lb:
                lang_scores.append(0.6)
            else:
                overlap = len(la & lb) / max(len(la | lb), 1)
                lang_scores.append(0.5 + 0.5 * overlap)
    parts["languages"] = sum(lang_scores) / len(lang_scores) if lang_scores else 0.7

    # Age-band preference (soft)
    age_scores = []
    for p in people:
        bands = set(p.age_pref or ())
        if not bands or "No preference" in bands:
            age_scores.append(0.7)
            continue
        # We only have intro_age as free text on others; treat missing as neutral
        # Full numeric age is not collected; score is neutral when we cannot compare
        age_scores.append(0.7)
    parts["age"] = sum(age_scores) / len(age_scores) if age_scores else 0.7

    weights = {"areas": 2.0, "dates": 1.5, "vacancy": 1.5, "group_size": 1.0, "room_choice": 1.0,
               "routine": 1.0, "tidy": 1.2, "social": 1.2, "guests": 1.0, "diet": 0.5,
               "country": 1.5, "languages": 1.0, "age": 0.8}
    total = sum(parts[k] * w for k, w in weights.items()) / sum(weights.values())
    return round(total * 100, 1), {k: round(v, 2) for k, v in parts.items()}


# ---------------------------------------------------------------------------
# Candidate generation (apartment first)
# ---------------------------------------------------------------------------
_REJECTIONS: set[tuple[int, int]] = set()   # GP7: (rejected enquiry, other enquiry) pairs loaded by load_rejections()



# ---------------------------------------------------------------------------
# GP11 "Bring a mate": linked friends are one hard keep-together party in matching
# ---------------------------------------------------------------------------
class LinkedParty:
    """Several enquiries (an enquirer and the friends who used their share link) treated as one unit by the group builder.
    Quacks like rules.Unit for the builder (id, size, mate_max, people, extra, windows); `units` holds the real enquiries,
    which every group is expanded into before the rule check, scoring and saving."""
    kind = "linked"

    def __init__(self, units: list):
        self.units = list(units)
        lead = self.units[0]
        self.id = lead.id
        self.people = [p for u in self.units for p in u.people]
        self.city = lead.city
        self.apt_sizes = set.intersection(*[set(u.apt_sizes) for u in self.units])
        self.mate_max = min(u.mate_max for u in self.units)
        self.extra = dict(lead.extra)
        self.extra["linked_ids"] = [int(u.id) for u in self.units]

    @property
    def size(self) -> int:
        return len(self.people)

    def move_in_window(self):
        ws = [u.move_in_window() for u in self.units]
        return (max(w[0] for w in ws), min(w[1] for w in ws))

    def move_out_window(self):
        ws = [u.move_out_window() for u in self.units]
        return (max(w[0] for w in ws), min(w[1] for w in ws))


def _units_of(x) -> list:
    return x.units if isinstance(x, LinkedParty) else [x]


def expand(group: list) -> list:
    """Flatten linked parties into their real enquiries (rules.Unit)."""
    return [u for x in group for u in _units_of(x)]


def cluster_pool(units: list[rules.Unit]) -> list:
    """Replace linked friends (GP11(b)) with one LinkedParty per link root. A root that is not in the pool leaves its friends
    linked to each other; a friend who is not in the pool leaves the rest together."""
    by_id = {int(u.id): u for u in units}
    buckets: dict[int, list] = {}
    for u in units:
        root = u.extra.get("linked_to_enquiry_id")
        key = int(root) if root and int(root) in by_id else (int(root) if root else int(u.id))
        buckets.setdefault(key, []).append(u)
    out = []
    for key, members in buckets.items():
        if len(members) == 1:
            out.append(members[0])
        else:
            members.sort(key=lambda u: (0 if int(u.id) == key else 1, int(u.id)))  # root first, then friends in order of arrival
            out.append(LinkedParty(members))
    return out


def linked_party_ids(c, enquiry_id: int) -> list[int]:
    """Every enquiry linked with this one (root + friends), live or not, root first."""
    e = row(c.execute("SELECT id, linked_to_enquiry_id FROM enquiries WHERE id=?", (enquiry_id,)))
    if not e:
        return [enquiry_id]
    root = e["linked_to_enquiry_id"] or e["id"]
    ids = [r["id"] for r in rows(c.execute("SELECT id FROM enquiries WHERE id=? OR linked_to_enquiry_id=? ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END, id", (root, root, root)))]
    return ids or [enquiry_id]


def mate_limit(c, dataset: str) -> int:
    """GP11(c)/(e) and GP12 (Bill, 28 Oct 2026): the most people any party may hold (one form, a share link, or both together),
    enquirer included, is ONE LESS than the beds in the largest available apartment, so MateBot AI can always find the party at
    least one housemate from outside it. With no apartment listed the limit falls back to one less than the largest group (P3)."""
    best = 0
    for a in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL")):
        try:
            _, cap = rules.apartment_capacity(apartment_from_row(a).bedrooms)
        except Exception:
            continue
        best = max(best, min(cap, rules.MAX_GROUP))
    return max(1, (best or rules.MAX_GROUP) - 1)


MATE_LINK_DAYS = 365  # GP11(j): a Bring a mate link stays valid for one year


def party_people(c, ids: list[int]) -> int:
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    return len(ids) + c.execute(f"SELECT COUNT(*) FROM party_members WHERE enquiry_id IN ({q})", ids).fetchone()[0]


def load_rejections(c) -> set[tuple[int, int]]:
    """GP7: a person staff rejected from a group is never proposed again together with the members who stayed."""
    global _REJECTIONS
    _REJECTIONS = {(r["rejected_enquiry_id"], r["other_enquiry_id"]) for r in rows(c.execute("SELECT rejected_enquiry_id, other_enquiry_id FROM member_rejections"))}
    return _REJECTIONS


def _pair_ok(u: rules.Unit, v: rules.Unit) -> bool:
    if isinstance(u, LinkedParty) or isinstance(v, LinkedParty):
        return all(_pair_ok(a, b) for a in _units_of(u) for b in _units_of(v))
    try:
        a_id, b_id = int(u.id), int(v.id)
        if (a_id, b_id) in _REJECTIONS or (b_id, a_id) in _REJECTIONS:
            return False  # GP7: rejected together earlier
    except ValueError:
        pass
    for a in u.people:
        for b in v.people:
            if rules.pair_conflict(a, b) or rules.pair_conflict(b, a):
                return False
    if _date_problem(u) or _date_problem(v):   # an unusable date row never pairs (and never crashes the run); see _unit_fits_apartment
        return False
    # date windows must overlap pairwise
    if max(u.move_in_window()[0], v.move_in_window()[0]) > min(u.move_in_window()[1], v.move_in_window()[1]):
        return False
    if max(u.move_out_window()[0], v.move_out_window()[0]) > min(u.move_out_window()[1], v.move_out_window()[1]):
        return False
    return True


def _date_problem(u) -> Optional[str]:
    """rules.Unit.date_problem for a unit or a LinkedParty (any member's problem is the party's). Logged once per enquiry per
    process (operational id and a static category only: the reason prose, which carries the stored dates, stays in the
    engine result for staff); the enquiry is simply left out of candidate formation instead of aborting the run."""
    members = u.units if isinstance(u, LinkedParty) else [u]
    for m in members:
        problem = m.date_problem()
        if problem:
            if m.id not in _DATE_PROBLEMS_LOGGED:
                _DATE_PROBLEMS_LOGGED.add(m.id)
                log.warning("enquiry %s left out of matching: unusable stored dates (T5: details in the engine result, not logged)", m.id)
            return problem
    return None


_DATE_PROBLEMS_LOGGED: set = set()


def building_identity(a: dict | None) -> str:
    """GP14 building identity of an apartment ROW: the street address without the unit number (HomeBot's parser,
    homebot_match.building_key: 'Unit 11 / 35 Darling Street' -> '35 Darling Street') + the recorded suburb and city, each
    normalised the way access facts normalise location text. The street component is REQUIRED: no street text -> '' (never a
    building); the same street text in another recorded suburb/city is a different building; suburb/city alone never identify one.
    Immutable once stored on a record: a row re-addressed or moved later simply stops matching."""
    a = a or {}
    return hm.building_identity(a.get("address"), a.get("suburb"), a.get("city"))   # one identity for both bots (11 Nov 2026)


def exclusions_from_json(raw) -> tuple:
    """GP14: the enquiry row's derived `apartment_exclusions_json` → tuple of (scope, key, allowed) per ACTIVE record (see
    apartment_rejections.refresh_exclusions). Unknown shapes give no entries; a record without a key is dropped (never a wildcard)."""
    data = pj(raw, {}) or {}
    out = []
    for r in data.get("records") or []:
        scope, key = str(r.get("scope") or ""), str(r.get("key") or "").strip()
        if scope in ("apartment", "building") and key:
            out.append((scope, key, tuple(sorted(str(x) for x in (r.get("allowed") or [])))))
    return tuple(out)


def exclusion_applies(record: tuple, apartment_id, building_key: str) -> bool:
    """One (scope, key, allowed) record against one apartment: it applies when the scope matches (apartment id, or the building
    key - '' never matches) AND this apartment is not in the record's OWN allowance. Shared by the matcher and apartment_rejections."""
    scope, key, allowed = record
    aid = str(apartment_id)
    if aid in allowed:
        return False   # a re-offer permitted under THIS record only; any other active record still decides for itself
    if scope == "apartment":
        return key == aid
    return bool(building_key) and key == building_key


def unit_excludes_apartment(u, apt: rules.Apartment) -> bool:
    """GP14: True when this enquiry (or any enquiry of a linked party) has an active record that applies to this apartment (declined
    the apartment itself, or its building). Applied where the eligible option set is built (_unit_fits_apartment), so an excluded
    apartment never consumes coverage or top-N slots, and again as a fail-closed guard at save/offer time. Records are independent:
    ANY applicable record without a permission for this apartment excludes it."""
    if isinstance(u, LinkedParty):
        return any(unit_excludes_apartment(x, apt) for x in u.units)
    bk = getattr(apt, "building_key", "") or ""
    return any(exclusion_applies(r, apt.id, bk) for r in (getattr(u, "apartment_exclusions", None) or ()))


def _unit_fits_apartment(apt: rules.Apartment, u: rules.Unit) -> bool:
    if _date_problem(u):
        return False
    if isinstance(u, LinkedParty):  # GP11(b)/GP12: every linked enquiry must fit, and the party must leave at least one bed for a housemate
        _, cap = rules.apartment_capacity(apt.bedrooms)
        return u.size < min(cap, rules.MAX_GROUP) and all(_unit_fits_apartment(apt, x) for x in u.units)
    if apt.available_from is None or u.city != apt.city:
        return False
    if unit_excludes_apartment(u, apt):   # GP14: declined apartment/building is not an eligible option for this enquiry
        return False
    if SIZE_LABEL[rules.normalise_type(apt.type_code)] not in u.apt_sizes:
        return False
    lo, hi = u.move_in_window()
    if hi < apt.available_from:
        return False
    _, cap = rules.apartment_capacity(apt.bedrooms)
    if u.size > cap or u.size > rules.MAX_GROUP:
        return False
    if u.size >= 2 and u.size >= min(cap, rules.MAX_GROUP):
        return False  # GP12: a party on one form (couple or friends) must leave at least one bed for an outside housemate
    # No budget pre-filter here (A16, 8 Nov 2026): the budget test is the complete household's total against the SUM of every
    # member's maximum, which cannot be bounded per enquiry without knowing the partners (a low-budget enquirer may well be carried
    # by higher-budget housemates). check_group applies A16 to each complete candidate group.
    return True


SEARCH_CHECKS_PER_TARGET = 8        # bounded backtracking: completed-group rule checks per seed and target size (T3(d), 10 Nov 2026)
SEARCH_ALTERNATIVES_PER_SEAT = 4    # next-best units tried for one seat before backing up a seat


def candidate_groups(apt: rules.Apartment, units: list[rules.Unit], max_groups: int | None = 12, seeds: int | None = 40,
                     compat: dict | None = None, disjoint: bool = False, stats: dict | None = None,
                     exception_offers_by_members: dict | None = None) -> tuple[list[dict], int]:
    """Build candidate groups that pass every hard rule. Returns (groups, candidates_considered).
    T6: with `seeds=None` every eligible enquiry seeds its own groups and `max_groups=None` keeps every rule-passing group.
    `compat` is an optional shared pairwise-compatibility cache (unit id pair -> bool) reused across apartments.
    `disjoint=True` (coverage completion, 9 Nov 2026): one pass that places each person at most once - a seed already placed in a
    rule-passing group this pass is skipped and placed people leave the pool - so the result is a set of DISJOINT groups built by
    the same greedy affinity order and the same rule check; used only over people who have no proposal yet.
    `stats` (optional dict, T3(d) 10 Nov 2026) accumulates the search frontier the bounded part did NOT explore:
    `targets_truncated` (seed/target searches that ran out of budget with branches left and found nothing),
    `seat_options_untried` (options beyond the per-seat bound) and `targets_stopped_at_first_valid` (larger-group searches that
    accepted the first rule-passing group with branches left - so a zero in the first two counters never means the larger-group
    search was exhaustive). Pairs are always enumerated completely within the eligible pool."""
    if stats is not None:
        stats.setdefault("targets_truncated", 0)
        stats.setdefault("seat_options_untried", 0)
        stats.setdefault("seat_options_date_infeasible", 0)
        stats.setdefault("targets_stopped_at_first_valid", 0)
    eligible = [u for u in units if _unit_fits_apartment(apt, u)]
    eligible.sort(key=lambda u: u.extra.get("created_at") or "")  # first come, first served among equals
    _, cap = rules.apartment_capacity(apt.bedrooms)
    cap = min(cap, rules.APT_TYPES[rules.normalise_type(apt.type_code)]["a4b_max"], rules.MAX_GROUP)
    seen: set[frozenset] = set()
    results: list[dict] = []
    considered = 0
    # pairwise compatibility cache
    if compat is None:
        compat = {}

    def ok(u, v):
        k = (u.id, v.id) if u.id < v.id else (v.id, u.id)
        if k not in compat:
            compat[k] = _pair_ok(u, v)
        return compat[k]

    targets = list(range(cap, rules.MIN_GROUP - 1, -1))
    n_rooms = rules.APT_TYPES[rules.normalise_type(apt.type_code)]["bedrooms"]

    def triple_capable(u):  # people who can sit three to a room (A4(b)(iii)-(v), A5)
        return sum(1 for p in u.people if "Share with two others" in p.arrangements or p.is_couple_member)

    placed: set = set()   # disjoint mode: unit ids already in a rule-passing group this pass

    def fill_options(group, size, target, need_triple, pool):
        """The units that could take the next seat of `group` for `target`, best pairwise affinity first (the same order the
        single greedy pick used, so the first branch below IS the former greedy group)."""
        have_triple = sum(triple_capable(g) for g in group)
        opts = []
        for u in pool:
            if size + u.size > target or target - 1 > u.mate_max or not all(ok(u, g) for g in group):
                continue  # 3.4: a unit that will not accept target-1 housemates cannot join a group of that size
            if need_triple and have_triple < need_triple and (target - size) <= (need_triple - have_triple) and not triple_capable(u):
                continue  # remaining seats must go to people who accept a triple room, or the allocation (GP5/A4) will fail
            opts.append((-_affinity(u, group), u))
        opts.sort(key=lambda t: t[0])   # stable: ties keep first-come order
        return [u for _, u in opts]

    def partial_dates_ok(group) -> bool:
        """Feasibility pruning (T3(d) search repair, 10 Nov 2026): the accepted start / move-out windows of a group only SHRINK as
        members are added, so a partial group that already has no lawful start (windows ∩ availability ∩ every party's adult
        declaration, I11(d)) or no common move-out can never complete into a rule-passing group at this apartment. The check is
        the SAME rules._lease_bounds check_group applies - no new rule, no widening; it only stops the search spending its budget
        on branches that cannot succeed."""
        _in, _out, err = rules._lease_bounds(apt, expand(group))
        return not err

    def seat_options(group, size, target, need_triple, pool) -> list:
        """The affinity-ordered options for the next seat with the date-infeasible ones removed BEFORE the per-seat bound applies
        (T3(d) C1 repair, 10 Nov 2026, reviewer finding: a valid final-seat unit ranked below several units whose own declared dates
        already fail _lease_bounds was never reached because those options consumed the per-seat bound). The filter is the same
        monotone partial_dates_ok screen (existing rules._lease_bounds, nothing new), applied to every seat including the last;
        removed options are counted in `seat_options_date_infeasible`. Order among the survivors is unchanged (same affinity, same ties)."""
        opts = fill_options(group, size, target, need_triple, pool)
        if size >= target or not opts:
            return opts
        kept = [u for u in opts if partial_dates_ok(group + [u])]
        if stats is not None and len(kept) != len(opts):
            stats["seat_options_date_infeasible"] += len(opts) - len(kept)
        return kept

    for seed in (eligible if seeds is None else eligible[:seeds]):
        if disjoint and seed.id in placed:
            continue
        for target in targets:
            if seed.size > target or target - 1 > seed.mate_max:
                continue
            if isinstance(seed, LinkedParty) and seed.size >= target:
                continue  # GP11(c): a friend party is never a group on its own; it always gets at least one housemate
            # a target above two per bedroom needs a third bed in a room: at least 3 people per extra bed must accept it
            need_triple = max(0, target - 2 * n_rooms) * 3
            pool0 = [u for u in eligible if u.id != seed.id and not (disjoint and u.id in placed)]
            # Bounded depth-first search (T3(d) search repair, 10 Nov 2026). The former builder added the single best-affinity
            # unit per seat and gave up on the seed/target when that one completed group failed check_group - so a person whose
            # best-affinity partner fails a date or exit rule was stranded even when a rule-valid partner existed (reproduced
            # witness: P003/P133 at A69). Now the first branch is exactly that greedy group; when it fails, or a seat cannot be
            # filled, the deepest seat tries its next-best unit, bounded by SEARCH_CHECKS_PER_TARGET completed-group checks and
            # SEARCH_ALTERNATIVES_PER_SEAT options per seat. Same affinity order, same rule check, same accepted result shape (the
            # first rule-passing group for this seed and target); nothing about weights, eligibility or who may pair changes.
            # Two-unit groups are enumerated COMPLETELY (every compatible partner, every one rule-checked: the pair oracle is
            # cheap), so no valid pair is ever a blind spot. Larger targets use the bounded search; when its budget runs out
            # with options untried the frontier is recorded in `stats` (search_frontier) so the run can say what it did NOT
            # explore, rather than presenting the bounded result as complete or optimal.
            exhaustive = target == 2 or (seed.size >= 2 and target - seed.size == 1)   # one seat to fill: every option is one check
            checks_left = math.inf if exhaustive else SEARCH_CHECKS_PER_TARGET
            per_seat = math.inf if exhaustive else SEARCH_ALTERNATIVES_PER_SEAT
            found = None
            stack = [([seed], seed.size, pool0, seat_options([seed], seed.size, target, need_triple, pool0), 0)]
            while stack and checks_left > 0 and (found is None or (exhaustive and not disjoint)):
                group, size, pool, opts, tried = stack[-1]
                if size == target:
                    stack.pop()
                    key = frozenset(u.id for u in group)
                    if key in seen:
                        continue
                    seen.add(key)
                    considered += 1
                    checks_left -= 1
                    unit_ids = [u.id for u in group]
                    # GP11: linked parties become their real enquiries for the rule check and everything after. Canonical member
                    # order (enquiry id, the member_key order every retained group is re-checked in): the room allocation and
                    # therefore the option score of ONE member set must not depend on which seed discovered it (10 Nov 2026).
                    full = sorted(expand(group), key=lambda u: int(u.id))
                    exact = (exception_offers_by_members or {}).get(member_key(full), ())
                    check = rules.check_group(apt, full, exception_offers=exact) if exact else rules.check_group(apt, full)
                    if check.ok:
                        score, parts = preference_score(apt, full, check)
                        results.append({"units": full, "check": check, "rule_score": score, "score_parts": parts})
                        found = unit_ids
                    continue
                if tried >= min(len(opts), per_seat):
                    if len(opts) > per_seat and stats is not None:
                        stats["seat_options_untried"] += len(opts) - int(per_seat)
                    stack.pop()   # this seat has no (more) options within the bound: back up one seat
                    continue
                stack[-1] = (group, size, pool, opts, tried + 1)
                u = opts[tried]
                group2 = group + [u]
                size2 = size + u.size
                pool2 = [x for x in pool if x.id != u.id]   # options are already date-screened by seat_options (no lawful shared dates = never completes)
                stack.append((group2, size2, pool2, seat_options(group2, size2, target, need_triple, pool2) if size2 < target else [], 0))
            if stack and checks_left <= 0 and found is None and stats is not None:
                stats["targets_truncated"] += 1   # budget exhausted with branches left and nothing found: an unresolved frontier
            if stack and found is not None and not exhaustive and stats is not None:
                stats["targets_stopped_at_first_valid"] += 1   # a larger group was accepted with branches left: alternatives unexplored (bounded, not exhaustive)
            if found is not None and disjoint:
                placed.update(found)
                break   # this seed is placed; the larger target it could not fill was tried first
    results.sort(key=lambda g: (-g["rule_score"], -sum(u.size for u in g["units"])))
    return (results if max_groups is None else results[:max_groups]), considered


def pair_matrix(units: list[rules.Unit]) -> tuple[dict, int, int]:
    """T6: compare every enquiry with every other enquiry under the pairwise rules. Returns (cache, pairs, compatible)."""
    compat: dict[tuple[str, str], bool] = {}
    ok = 0
    for i, u in enumerate(units):
        for v in units[i + 1:]:
            k = (u.id, v.id) if u.id < v.id else (v.id, u.id)
            compat[k] = _pair_ok(u, v)
            ok += compat[k]
    return compat, len(compat), ok


def _affinity(u: rules.Unit, group: list[rules.Unit]) -> float:
    e = u.extra
    s = 0.0
    # launch scope (8 Nov 2026): practical habits, study place and dates only; no language, country or own-region affinity
    for g in group:
        f = g.extra
        s += 1.0 if e.get("tidy") == f.get("tidy") else 0
        s += 0.5 if e.get("social") == f.get("social") else 0
        s += 0.5 if e.get("school") and e.get("school") == f.get("school") else 0
        s -= abs((d(e["move_out"]) - d(f["move_out"])).days) / 120
    return s / len(group)


# ---------------------------------------------------------------------------
# Running a match and persisting proposals
# ---------------------------------------------------------------------------
def load_pool(c, dataset: str, exclude_enquiry_ids: set | None = None) -> list[rules.Unit]:
    is_test = 1 if dataset == "test" else 0
    enqs = rows(c.execute("SELECT * FROM enquiries WHERE " + POOL_SQL, (is_test, *POOL_STATUSES)))
    load_rejections(c)
    out = []
    for e in enqs:
        if exclude_enquiry_ids and e["id"] in exclude_enquiry_ids:
            continue
        members = rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))
        try:
            out.append(unit_from_row(e, members))
        except Exception:
            continue
    _identities_for_computation(c, out)   # GP13: the identity each person is being computed for, captured now
    return cluster_pool(out)  # GP11(b): linked friends are one keep-together party


# The compatibility ASSESSMENT packet (T6). Launch scope (Bill, 8 November 2026): structured, approved practical fields only.
# It carries no name, email, country of birth, region, nationality or language (identity and its proxies, which invite
# inference), no sex/age (I8), and no free text (intro, else_text, mate_ideal: kept in full for the staff/customer introduction
# under GP2, which is drafted from a separate, identity-bearing payload in app.group_introduction, never from this packet).
# People are referred to by positional refs (U1, U1-m1, ...) that are stable within one packet only.
AI_PACKET_FIELDS = ("doing", "school", "course", "level", "routine", "tidy", "social", "guests", "diet", "party", "party_size", "move_in", "move_out")
# T4(f) provenance: the answers on an enquiries row that are about the ENQUIRER as an individual (study/work, lifestyle self-
# description, free text), as opposed to the party-wide answers (budget, dates, rooms, housemate preferences, I16(d)). After a GP9(e)
# lead takeover the same row carries another person: these answers were written by the former enquirer and are NOT the promoted
# person's own until staff confirm them (lead_answers_unverified_at). Masked at read time; the stored answers are kept as history.
LEAD_INDIVIDUAL_FIELDS = ("doing", "school", "course", "level", "routine", "tidy", "social", "guests", "diet", "work", "intro", "else_text")
LEAD_ANSWERS_UNVERIFIED_TEXT = "Answers recorded by the former enquirer await confirmation by the person who took over the enquiry"


def lead_answers_unverified(e: dict | None) -> bool:
    return bool((e or {}).get("lead_answers_unverified_at"))
# r7 packet stage: the party's RAW accepted preferences and smoking answers, exactly as the enquirer recorded them for the whole
# party (I16(d)). A stated first choice (room_first) is carried separately from what the party said it would ACCEPT (rooms_accepted,
# apt_sizes_accepted, bunk_ok, mate_max); the allocator's working defaults (arrangements incl. a stated couple's shared bed, the
# group's intersected sizes / minimum mate_max) are never written here, and a NULL/blank stored value stays None (= not recorded).
AI_PACKET_PREFERENCE_FIELDS = ("room_first", "rooms_accepted", "apt_sizes_accepted", "bunk_ok", "mate_max", "smoke", "smoke_ok")


# r7 C2: these are CLOSED-choice fields. Only the form's options (and the two legacy room spellings the rules engine understands)
# may reach the packet; an unrecognised string - or a list containing one - makes the WHOLE field 'not recorded', so a stored
# pseudo-choice can never travel into the assessment as if it were a preference. Projection only: nothing is normalised or written back.
LEGACY_ROOM_ALIASES = ("Share with one other", "Share with two others")
LEGACY_SMOKE_ALIASES = ("Yes",)
SMOKE_OPTIONS = ("No", "Yes, outside only (balcony or courtyard)")
SMOKE_OK_OPTIONS = ("Yes", "No")


def _choice_sets() -> dict[str, frozenset]:
    from .enquiries import ROOM_OPTIONS, SIZE_OPTIONS   # local import: enquiries imports matching helpers at module level
    return {"room": frozenset(ROOM_OPTIONS) | frozenset(LEGACY_ROOM_ALIASES), "size": frozenset(SIZE_OPTIONS),
            "smoke": frozenset(SMOKE_OPTIONS) | frozenset(LEGACY_SMOKE_ALIASES), "smoke_ok": frozenset(SMOKE_OK_OPTIONS)}


def _raw_preferences(e: dict) -> dict:
    """The party's recorded choices, decoded but never coerced: anything that is not a valid stored answer (M3 yes/no as 0/1 or
    yes/no text; an integral mate_max; a decoded list of non-blank KNOWN choice strings) is None = not recorded. A stored 2 or -1
    for bunk_ok, 2.5 or a bool for mate_max (a bool IS a valid bunk_ok), a dict inside a list, a bare number or an unrecognised
    choice is a malformed row, never an acceptance."""
    allowed = _choice_sets()

    def text(v, kind):
        return v.strip() if isinstance(v, str) and v.strip() in allowed[kind] else None

    def yes_no(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, int) and v in (0, 1):
            return bool(v)
        if isinstance(v, str):
            t = v.strip().lower()
            return True if t in ("1", "yes", "true") else False if t in ("0", "no", "false") else None
        return None

    def integer(v):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
            return int(v)
        return None   # floats (2.5), blanks, containers: malformed -> not recorded

    def choices(v, kind):
        try:
            items = _lst(v)
        except Exception:
            return None
        if not isinstance(items, list) or not items or not all(isinstance(x, str) and x.strip() in allowed[kind] for x in items):
            return None   # one unknown token spoils the whole field: fail closed, no partial pass-through
        return [x.strip() for x in items]

    return {"room_first": text(e.get("room_first"), "room"), "rooms_accepted": choices(e.get("rooms"), "room"), "apt_sizes_accepted": choices(e.get("apt_sizes"), "size"),
            "bunk_ok": yes_no(e.get("bunk_ok")), "mate_max": integer(e.get("mate_max")), "smoke": text(e.get("smoke"), "smoke"), "smoke_ok": text(e.get("smoke_ok"), "smoke_ok")}
AI_PACKET_EXCLUDED = frozenset({"first_name", "last_name", "name", "email", "phone", "birth_country_raw",
                                "intro", "else_text", "mate_ideal", "sex", "dob",
                                "intro_sex", "intro_age", "work", "notes_packet_json", "notes_residual", "notes_input_key"})
# 17 Nov 2026: sex_pref, age_pref, own_region, avoid_*, languages, birth_country may travel for matching-aware assessment.
# intro_sex / intro_age remain introduction-only and stay excluded from assessment packets.


def person_refs(units: list[rules.Unit]) -> dict[str, str]:
    """person.id → positional ref ("U1" for the first unit's enquirer, "U1-m1" for their first party member, ...)."""
    refs: dict[str, str] = {}
    for i, u in enumerate(units, start=1):
        k = 0
        for p in u.people:
            if p.is_lead:
                refs[p.id] = f"U{i}"
            else:
                k += 1
                refs[p.id] = f"U{i}-m{k}"
    return refs


_NOTES_PACKET_KEYS = ("category", "strength", "value", "basis", "source_field")


def _notes_packet(raw) -> list[dict]:
    """The stored neutral projection, re-checked on read: only the five neutral keys, categories in the closed catalogue; anything
    else is dropped (fail closed), so a malformed row can never carry text into the packet."""
    from . import notes_facts as nf
    items = pj(raw, []) if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    out = []
    for it in items or []:
        if not isinstance(it, dict) or it.get("category") not in nf.CATEGORIES or it.get("strength") not in nf.STRENGTHS:
            continue
        ok, value = nf._clean_value(nf.CATEGORIES[it["category"]]["value"], it.get("value"))
        if not ok or it.get("source_field") not in nf.SOURCE_FIELDS:
            continue
        out.append({"category": it["category"], "strength": it["strength"], "value": value, "basis": nf.BASIS, "source_field": it["source_field"]})
    return out


def members_for_ai(units: list[rules.Unit]) -> list[dict]:
    refs = person_refs(units)
    out = []
    for i, u in enumerate(units, start=1):
        e = u.extra
        m = {"ref": f"U{i}"}
        m.update({k: e.get(k) for k in AI_PACKET_FIELDS})
        if lead_answers_unverified(e):   # takeover: the former enquirer's individual answers are not this person's (party-wide answers stay)
            m.update({k: None for k in AI_PACKET_FIELDS if k in LEAD_INDIVIDUAL_FIELDS})
        m.update(_raw_preferences(e))   # r7: raw accepted preferences / first choice / smoking, party-wide (source: this enquirer)
        m["party_members"] = [refs[p.id] for p in u.people if not p.is_lead]   # refs only: no names, no countries
        couples: dict[str, list[str]] = {}
        for p in u.people:
            if p.couple_id:
                couples.setdefault(p.couple_id, []).append(refs[p.id])
        m["couples"] = list(couples.values())  # I16(f): only couples the enquirer stated
        # GAP-T4c v2: validated NEUTRAL practical facts from the person's own notes (category / strength / typed value / basis /
        # source field; never a span, offset, residual marker or raw text). A blank note and an excluded-only note are both [].
        from . import notes_facts as _nf
        m["notes_facts"] = _nf.bind(_notes_packet(e.get("notes_packet_json")) if _nf.is_current(e) else [], e)   # + possible_ambiguity against the member's OWN current answers; a projection read under an earlier extraction contract binds NOTHING (fix 2 currentness, read-time)
        assert not (set(m) & AI_PACKET_EXCLUDED)
        out.append(m)
    return out


def allocation_for_ai(rooms: list[dict], refs: dict[str, str]) -> list[dict]:
    """A bed allocation for the assessment packet: occupants by positional ref, no names or ids."""
    out = []
    for j, r in enumerate(rooms or [], start=1):
        occ = []
        for k, o in enumerate(r.get("occupants") or [], start=1):
            occ.append({"ref": refs.get(str(o.get("person_id")), f"room{j}-occupant{k}"), "bed": o.get("bed"), "weekly_share": o.get("share")})
        out.append({"room": r.get("room"), "setup": r.get("setup"), "room_cost": r.get("room_cost"), "occupants": occ})
    return out


# T6 evidence snapshot. Everything a review or brief depends on, read from the CURRENT database rows (never from a caller's
# copy): the rule version and model, the group's state, each member's practical compatibility answers and party composition
# (ids only: identity, intro text, intro_sex/intro_age and other introduction-only fields are deliberately not part of the
# compatibility evidence, so editing them never re-ranks a group), the GP9 bar / rejection state, and every offered apartment's
# lease facts. The sha256 of the canonical JSON is stored with the assessment; ai_queue.is_ready recomputes it and a group is
# ready only while it still matches. Identity-only edits are handled separately (they invalidate the customer intro draft, GP2).
# Compatibility INPUT fingerprint (T6 F). Lifecycle outcomes of the review itself (an enquiry moving New -> Proposed in group ->
# Introduced -> Group confirmed, a group's status or its chosen #1 apartment) are NOT evidence and are outside the hash; a person
# leaving the pool (Withdrawn, Leased elsewhere), a bar, a rejection, any practical answer, party composition, dates, budget, the
# rule version and the model are inside it.
from .apartment_features import KEYS as _FEATURE_KEYS  # noqa: E402

ACTIVE_ENQUIRY_STATUSES = ("New", "Proposed in group", "Introduced", "Group confirmed", "Awaiting replacement")
EVIDENCE_ENQUIRY_FIELDS = ("barred_at", "city", "budget_max", "budget_option", "move_in", "move_in_flex", "move_out", "move_out_flex", "apt_sizes",
                           "rooms", "room_first", "bunk_ok", "mate_max", "mate_ideal", "party", "party_size", "couples_answer", "couples_json", "smoke", "smoke_ok",
                           "linked_to_enquiry_id", "adults_confirmed", "adults_confirmed_move_in", "features", "features_top", "travel", "areas", *AI_PACKET_FIELDS,
                           "notes_packet_json", "notes_status", "lead_answers_unverified_at")   # T4c: the validated notes projection feeds the packet, so an edited note makes the review stale; takeover provenance flag
# adults_confirmed_party (the declaration's party fingerprint) is identity-bearing, so the evidence carries only its digest.
EVIDENCE_APARTMENT_FIELDS = ("type_code", "available_from", "rent_12m", "rent_6m", "min_lease_months", "bedroom1", "bedroom2", "bedroom3", "bills_included", "leased_at",
                             "suburb", "lat", "lon", "parking_charge", *[f"feat_{k}" for k in _FEATURE_KEYS])   # feature flags feed the review/brief packets, so they are evidence


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def evidence_rows(c, group_id: int) -> dict:
    """ONE read of every row the compatibility evidence (and the AI packet) is built from: the group, each member's enquiry row,
    party_members rows (full rows: identity columns included, for the packet's provenance), rejections, the group's offered
    apartments and their apartment rows. The packet and its evidence hash are both derived from THIS capture, so an edit
    between two reads can never stamp old packet facts with a new hash (root's capture probe). Missing rows are captured as
    None/empty: they are part of the evidence (a deleted member or apartment changes the hash)."""
    g = row(c.execute("SELECT id, member_key, revision_round, status, apartment_id FROM groups WHERE id=?", (group_id,)))
    cap = {"group": g, "members": [], "group_apartments": [], "apartments": {}}
    if not g:
        return cap
    for gm in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (group_id,))):
        eid = gm["enquiry_id"]
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        pm = rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))
        rej = rows(c.execute("SELECT rejected_enquiry_id, other_enquiry_id FROM member_rejections WHERE rejected_enquiry_id=? OR other_enquiry_id=? ORDER BY id", (eid, eid)))
        cap["members"].append({"enquiry_id": eid, "enquiry": e, "party_members": pm, "rejections": [(r["rejected_enquiry_id"], r["other_enquiry_id"]) for r in rej]})
    cap["group_apartments"] = rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY apartment_id", (group_id,)))
    for ga in cap["group_apartments"]:
        cap["apartments"][ga["apartment_id"]] = row(c.execute("SELECT * FROM apartments WHERE id=?", (ga["apartment_id"],)))
    from . import exit_exceptions
    cap["exceptions"] = exit_exceptions.capture_for_group(c, group_id)
    for record in cap["exceptions"]:
        aid = record["apartment_id"]
        if aid not in cap["apartments"]:
            cap["apartments"][aid] = row(c.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    return cap


def excluded_option_ids(cap: dict) -> set:
    """GP14 (inc3, 11 Nov 2026): among a capture's group_apartments rows, the apartment ids that ANY current member's ACTIVE record
    excludes (per record, own allowance; matching.exclusion_applies - the same decision the generator and the offer guard make).
    Pure, from the capture only: the members' enquiry rows carry their records (apartment_exclusions_json) and the apartment rows
    give the building identity. Rows listed here are never an effective option: the AI packet, readiness and the evidence hash all
    work on the capture MINUS this set (effective_group_apartments). They are cached working-list rows, not the history record
    (that is apartment_rejections + offered_basis_json): a stock reconcile step or a full run prunes them without any effect on
    the evidence, and a lift purges them (root, 11 Nov 2026)."""
    recs: list = []
    for m in cap.get("members") or []:
        e = m.get("enquiry") or {}
        recs.extend(exclusions_from_json(e.get("apartment_exclusions_json")))
    if not recs:
        return set()
    out = set()
    for ga in cap.get("group_apartments") or []:
        a = (cap.get("apartments") or {}).get(ga["apartment_id"])
        bk = building_identity(a) if a else ""
        if any(exclusion_applies(r, ga["apartment_id"], bk) for r in recs):
            out.add(int(ga["apartment_id"]))
    return out


def effective_group_apartments(cap: dict) -> list[dict]:
    """The capture's group_apartments rows that are effective options now (GP14 exclusions removed). Same order as captured."""
    gone = excluded_option_ids(cap)
    return [ga for ga in (cap.get("group_apartments") or []) if int(ga["apartment_id"]) not in gone]


def effective_exception_records(cap: dict) -> list[dict]:
    """Declined history stays outside effective evidence, even after option-cache pruning."""
    exclusions = [r for m in cap.get("members") or []
                  for r in exclusions_from_json((m.get("enquiry") or {}).get("apartment_exclusions_json"))]
    return [x for x in cap.get("exceptions") or []
            if not any(exclusion_applies(r, x["apartment_id"],
                                         building_identity(cap["apartments"].get(x["apartment_id"]) or {}))
                       for r in exclusions)]


def eligible_offers(c, group_id: int) -> list[dict]:
    """The ONE eligibility lens for a persisted group (inc3): its group_apartments rows minus those any current member's active
    record excludes, from one evidence capture. Used by offer_apartment, the AI packet, readiness and the evidence hash."""
    return effective_group_apartments(evidence_rows(c, group_id))


def evidence_from_rows(cap: dict) -> tuple[str, dict]:
    """(sha256 hash, snapshot) of the compatibility evidence in a capture from evidence_rows. Pure: no database access.
    GP14 (inc3, root decision 11 Nov 2026): the apartments in the evidence are the EFFECTIVE offer set only (rows a current
    member's active record excludes are left out). The evidence never depends on whether such a history row happens to remain
    cached in group_apartments: recording a decline of an offered option shrinks the effective set (hash moves); a lifted option
    re-enters only through a validated run (hash moves then); a declined row being leased, pruned by a stock step or dropped by a
    full run changes nothing effective (hash unchanged). The lift writer makes the review non-current by queuing it, not by a
    changed fact."""
    import hashlib
    g = cap.get("group")
    if not g:
        return "", {}
    excluded = excluded_option_ids(cap)
    members = []
    for m in cap["members"]:
        e = m["enquiry"] or {}
        facts = {k: e.get(k) for k in EVIDENCE_ENQUIRY_FIELDS}
        facts["in_pool"] = bool(e) and e.get("status") in ACTIVE_ENQUIRY_STATUSES
        if e.get("travel_modes"):   # the travel choices (16 Nov 2026): in the evidence ONLY once answered, so older snapshots keep their hash
            facts["travel_modes"] = e["travel_modes"]   # and an edit of the answer makes the saved review out of date like any other answer
        facts["adults_confirmed_party_digest"] = hashlib.sha256((e.get("adults_confirmed_party") or "").encode("utf-8")).hexdigest()[:16] if e.get("adults_confirmed_party") else None
        members.append({"enquiry_id": m["enquiry_id"], "facts": facts, "party_member_ids": [x["id"] for x in m["party_members"]], "rejections": list(m["rejections"])})
    apartments = []
    for ga in cap["group_apartments"]:
        if int(ga["apartment_id"]) in excluded:
            continue   # GP14: a declined option is not evidence of the offer (effective set only; see excluded_option_ids)
        a = cap["apartments"].get(ga["apartment_id"]) or {}
        alloc = pj(ga.get("allocation_json"), {}) or {}
        beds = [{"room": r.get("room"), "setup": r.get("setup"), "occupants": [{"person_id": o.get("person_id"), "bed": o.get("bed"), "share": o.get("share")} for o in r.get("occupants") or []]}
                for r in alloc.get("rooms") or []]   # bed allocation by person id only: no names in the evidence
        facts = {k: a.get(k) for k in EVIDENCE_APARTMENT_FIELDS}
        sfa = access_facts.effective(a)   # A13(a) recorded access fact (C3): in the evidence ONLY once a record exists (older snapshots keep their hash);
        if sfa["record_id"] is not None:  # value AND record generation, so yes->unknown->yes is three different evidences, never one by coincidence
            facts["step_free_access"] = sfa["value"]
            facts["step_free_record"] = sfa["record_id"]
        apartments.append({"apartment_id": ga["apartment_id"], "lease": {k: ga.get(k) for k in ("lease_start", "lease_end", "rate_kind", "total_weekly", "extra_bed")}, "beds": beds,
                           "facts": facts})
        if ga.get("exception_ref") is not None:
            apartments[-1]["lease"].update(exception_ref=ga["exception_ref"], exception_total=ga.get("exception_total"))
    # The group's own workflow state (status, top apartment) is an OUTCOME of review and staff decisions, not evidence for it, so it
    # is outside the hash; members' statuses, rejections, party composition and every offered apartment's facts are inside.
    snap = {"rule_version": rules.RULE_VERSION, "model": ai.MODEL, "review_contract": ai.REVIEW_CONTRACT, "group": {"id": g["id"], "member_key": g["member_key"]},
            "members": members, "apartments": apartments}
    exceptions = effective_exception_records(cap)
    if exceptions:
        snap["exceptions"] = [{"id": x["id"], "digest": hashlib.sha256(_canonical(x).encode()).hexdigest()} for x in exceptions]
    return hashlib.sha256(_canonical(snap).encode("utf-8")).hexdigest(), snap


def group_evidence(c, group_id: int) -> tuple[str, dict]:
    """(sha256 hash, snapshot) of the current compatibility evidence for a persisted group (evidence_rows + evidence_from_rows)."""
    return evidence_from_rows(evidence_rows(c, group_id))


RATE_BAND_LABELS = {"6m": "six-month rate band (A6 pricing band for a January/February start ending in July; not the stay length)",
                    "12m": "standard rate band (A6 pricing band; not the stay length)"}


def rate_band_fact(rate_kind) -> Optional[str]:
    """The A6 pricing band as an explicit description for AI packets: the bare code '6m' read as 'a six-month lease' in a live
    review of a 1 Feb - 10 Jul stay (159 days). Presentation only; the stored rate_kind code is unchanged."""
    if rate_kind in (None, ""):
        return None
    return RATE_BAND_LABELS.get(str(rate_kind), f"{rate_kind} rate band (A6 pricing band; not the stay length)")


def stay_days_fact(lease_start, lease_end) -> Optional[int]:
    """Exact day count from lease start to lease end (date difference, e.g. 2027-02-01 -> 2027-07-10 = 159); None if either is missing."""
    try:
        a = lease_start if isinstance(lease_start, date) else date.fromisoformat(str(lease_start))
        b = lease_end if isinstance(lease_end, date) else date.fromisoformat(str(lease_end))
    except (TypeError, ValueError):
        return None
    return (b - a).days


DATE_WINDOW_BASIS = ("move_in / move_out are the person's stated dates; move_in_flex_answer / move_out_flex_answer their recorded flexibility choice as "
                     "worded on the form (null when none is recorded, 'unrecognised recorded answer' when the stored value is not a form choice) and "
                     "move_in_flex_days / move_out_flex_days that choice as +/- days; "
                     "open_to_extending is their 'Open to extending' answer, which widens the move-out window by up to "
                     f"{rules.OPEN_TO_EXTENDING_DAYS} days after the stated move-out (rules.Unit.move_out_window) instead of the +/- days; "
                     "effective_move_in_window / effective_move_out_window are the exact bounds the dates rule tested")


FLEX_ANSWER_CHOICES = frozenset(rules.FLEX_DAYS) | {"Open to extending"}   # the form's current and legacy spellings (enquiries.FLEX_OUT + rules.FLEX_DAYS)
FLEX_ANSWER_UNRECOGNISED = "unrecognised recorded answer"


def flex_answer_fact(raw) -> Optional[str]:
    """The recorded flexibility choice as a closed value: one of the form's choices verbatim, None when nothing is recorded, or the
    fixed marker FLEX_ANSWER_UNRECOGNISED for any other stored string. Free text never reaches the packet through this field."""
    if raw is None or raw == "":
        return None
    return raw if raw in FLEX_ANSWER_CHOICES else FLEX_ANSWER_UNRECOGNISED


def date_window_fact(u) -> dict:
    """One enquirer's date facts for the packet with their provenance: the stated dates and +/- answers as recorded, the recorded
    'Open to extending' answer, and the effective windows exactly as rules.Unit computes them for the dates rule. Before this
    the detail carried move_out_flex_days alone (0 for a person open to extending), so a pass could not be explained from the
    packet. Values are read from the rules object; nothing is recomputed or rounded here."""
    try:
        mi_lo, mi_hi = u.move_in_window()
        mo_lo, mo_hi = u.move_out_window()
        mi_w, mo_w = [mi_lo.isoformat(), mi_hi.isoformat()], [mo_lo.isoformat(), mo_hi.isoformat()]
    except (OverflowError, TypeError, AttributeError):
        mi_w = mo_w = None
    extra = u.extra if isinstance(getattr(u, "extra", None), dict) else {}
    return {"move_in": u.move_in.isoformat(), "move_in_flex_days": u.move_in_flex, "move_in_flex_answer": flex_answer_fact(extra.get("move_in_flex")),
            "move_out": u.move_out.isoformat(), "move_out_flex_days": u.move_out_flex, "move_out_flex_answer": flex_answer_fact(extra.get("move_out_flex")),
            "open_to_extending": bool(getattr(u, "open_to_extending", False)), "effective_move_in_window": mi_w, "effective_move_out_window": mo_w}


def rule_check_facts(apt, units, chk, refs: dict) -> list[dict]:
    """Deterministic hard-rule results for one apartment, as facts with provenance (the rules engine, this rule version). The model
    is asked to check the packet is consistent with them and to say so when it is not; it cannot change them."""
    src = f"rules.check_group, {rules.RULE_VERSION}"
    people = [p for u in units for p in u.people]
    shares = chk.shares or {}
    budget = {refs.get(p.id, p.id): per_person_budget_fact(p, shares.get(p.id), refs) for p in people}   # A12 proposed shares + I14 stated maxima, subject-attributed
    household = dict(chk.household_budget or {})
    household.pop("member_weekly_maximums", None)   # per-person figures are in per_person under packet refs (lossless, with provenance)
    n_people = len(people)
    windows = {refs.get(u.people[0].id, u.id): date_window_fact(u) for u in units}
    return [
        {"rule": "dates", "result": "pass", "source": src, "detail": {"lease_start": chk.lease_start.isoformat(), "lease_end": chk.lease_end.isoformat(),
                                                                       "stay_days": stay_days_fact(chk.lease_start, chk.lease_end), "rate_band": rate_band_fact(chk.rate_kind),
                                                                       "exit_window": chk.exit_window, "apartment_available_from": apt.available_from.isoformat() if apt.available_from else None,
                                                                       "members": windows, "members_basis": DATE_WINDOW_BASIS}},
        {"rule": "budget", "result": "pass", "source": src, "detail": {"whole_weekly_rent": chk.total_weekly, "extra_bed_charge": chk.extra_bed, "household": household,
                                                                        "per_person": budget, "per_person_note": "proposed A12 shares for the group's agreement; A16 tests only the household total"}},
        {"rule": "beds", "result": "pass", "source": src, "detail": {"rooms": allocation_for_ai(chk.allocation, refs)}},
        {"rule": "smoking", "result": "pass", "source": src,
         "detail": {"basis": "unit-level check derived from each enquirer's party answers (smoke / smoke_ok); not any companion's own statement",
                    "people": {refs.get(p.id, p.id): {"smokes": p.smokes, "ok_with_smoker": p.smoke_ok, "derived_from": refs.get(u.people[0].id, u.id)}
                               for u in units for p in u.people}}},
        {"rule": "adults", "result": "pass", "source": src, "detail": {refs.get(u.people[0].id, u.id): {"declared_18_plus_at_move_in": bool(u.adults_confirmed_move_in)} for u in units}},
        {"rule": "capacity", "result": "pass", "source": src, "detail": {"people": n_people, "apartment": rules.APT_TYPES[rules.normalise_type(apt.type_code)]["label"]}},
        # 3.4 as the code applies it (rules.check_group: n - 1 <= mate_max): the stated maximum counts the OTHER people who would live with
        # the person, never the person themselves and never the total number of occupants. Exposed so a reader cannot take "accepts at
        # most 6 housemates" in a seven-person group as a breach. This exposes the existing check only; it does not decide the open
        # register question of whether a party's own companions count towards that maximum.
        {"rule": "housemates", "result": "pass", "source": src,
         "detail": {"basis": "3.4: each enquirer's maximum counts the OTHER people living with them, not themselves and not the total number of occupants",
                    "people_in_group": n_people,
                    "enquirers": {refs.get(u.people[0].id, u.id): {"accepts_at_most_other_housemates": u.mate_max, "other_people_in_this_group": n_people - 1}
                                  for u in units}}},
    ]


def per_person_budget_fact(p, share, refs: dict) -> dict:
    """One member's budget evidence for the packet. weekly_share is the A12 guide share from the deterministic bed allocation, carried
    as stored (cents-rounded by the allocation) and repeated as text in weekly_share_text; stated_weekly_max is the I14 answer, with a
    lossless text of the same validated amount the A16 check used and the ref of the person who reported it (the enquirer answers I14
    for the whole party, I16(d)). No arithmetic happens here."""
    from . import budget as budget_mod
    lead_ref = refs.get(p.unit_id, p.unit_id)
    out = {"weekly_share": share, "weekly_share_text": (rules._lossless_decimal_text(budget_mod.money(share)) if share is not None else None),
           "weekly_share_basis": "A12 guide share from the bed allocation, a proposal for the group's agreement (not individually agreed, not a test)",
           "stated_weekly_max": p.budget_max, "stated_weekly_max_exact": None, "reported_by": lead_ref}
    if p.budget_max is not None:
        try:
            out["stated_weekly_max_exact"] = rules._lossless_decimal_text(budget_mod.money(p.budget_max, positive=True))
        except (ValueError, TypeError, ArithmeticError):
            out["stated_weekly_max_exact"] = None   # unusable amount: the check would have held the group; no exact fact is offered
    return out


def feature_facts(a: dict, enquiries: list[dict], refs: dict) -> list[dict]:
    """apartment_features.compare for each member's wishes, merged per feature: recorded status at this apartment and who asked
    for it (and for whom it matters most). Facts only: no weights, no exclusion."""
    from . import apartment_features as feat
    merged: dict[str, dict] = {}
    for e in enquiries:
        wanted, top = pj(e.get("features"), []) or [], pj(e.get("features_top"), []) or []
        if not wanted and not top:
            continue
        ref = refs.get(str(e["id"]), f"U?{e['id']}")
        for it in feat.compare(wanted, top, a)["items"]:
            m = merged.setdefault(it["key"], {"key": it["key"], "label": it["label"], "group": it["group"], "recorded": it["status"], "wanted_by": [], "matters_most_to": []})
            m["wanted_by"].append(ref)
            if it["matters_most"]:
                m["matters_most_to"].append(ref)
    return list(merged.values())


def _strict_reviews(res: list[dict], payloads: list[dict]) -> list[dict]:
    """A raw review list (e.g. a test double for ai.assess_groups) goes through the same strict parser as a real reply."""
    if res and any("valid" not in r for r in res):
        return ai.parse_review_batch(res, payloads)
    return res


def _apply_candidate_review(p: dict, r: dict) -> None:
    """Strict review result onto a candidate payload: only a VALID 'approve' review yields a rating; anything else is recorded as
    not rated with the reason (never a defaulted or clamped number)."""
    if r.get("valid") and r.get("verdict") == "approve":
        p["ai_rating"], p["ai_rationale"], p["ai_verdict"] = r["rating"], r["rationale"], "approve"
    elif r.get("valid"):
        p["ai_rating"], p["ai_verdict"] = None, r["verdict"]
        p["ai_rationale"] = f"AI verdict {r['verdict']}: " + (r.get("rationale") or "") + (f" (replace {', '.join(r['replace_refs'])})" if r.get("replace_refs") else "")
    else:
        p["ai_rating"], p["ai_verdict"] = None, None
        p["ai_rationale"] = "AI review unusable: " + (r.get("invalid_reason") or "no review")
    p["ai_findings"] = r.get("findings") or []


def candidate_apartment_packet(apt_row: dict, g: dict, refs: dict) -> dict:
    """One apartment entry in the strict review packet for a rule-formed candidate (same shape as ai_queue._payload builds)."""
    chk: rules.GroupCheck = g["check"]
    apt = apartment_from_row(apt_row)
    return {"apartment_id": int(apt_row["id"]), "address": apt_row.get("address"), "suburb": apt_row.get("suburb"),
            "type_label": rules.APT_TYPES[rules.normalise_type(apt_row["type_code"])]["label"], "bills_included": bool(apt_row.get("bills_included")),
            "available_from": apt_row.get("available_from"), "lease_start": chk.lease_start.isoformat(), "lease_end": chk.lease_end.isoformat(),
            "rate_kind": chk.rate_kind, "rate_band": rate_band_fact(chk.rate_kind), "stay_days": stay_days_fact(chk.lease_start, chk.lease_end),
            "total_weekly": chk.total_weekly, "extra_bed": chk.extra_bed, "allocation": chk.allocation,
            "allocation_for_ai": allocation_for_ai(chk.allocation, refs), "rule_score": g["rule_score"],
            "score_parts": g["score_parts"], "rank": 1, "rule_rank": 1, "rule_checks": rule_check_facts(apt, g["units"], chk, refs),
            "step_free_access": access_facts.effective(apt_row)["value"], "step_free_source": access_facts.effective(apt_row)["source"],
            "features": feature_facts(apt_row, [u.extra for u in g["units"]], refs)}


def _group_payload(apt_row: dict, g: dict) -> dict:
    chk: rules.GroupCheck = g["check"]
    refs = person_refs(g["units"])
    return {
        "lease_start": chk.lease_start.isoformat(), "lease_end": chk.lease_end.isoformat(), "rate_kind": chk.rate_kind, "exit_window": chk.exit_window,
        "weekly_rent": chk.weekly_rent, "extra_bed": chk.extra_bed, "total_weekly": chk.total_weekly, "allocation": chk.allocation,
        "liability_note": rules.liability_note(chk.total_weekly),
        "rule_score": g["rule_score"], "score_parts": g["score_parts"], "members_for_ai": members_for_ai(g["units"]),
        "person_refs": refs, "apartments": [candidate_apartment_packet(apt_row, g, refs)],
    }


class BookingChanged(ValueError):
    """A captured booking changed before the guarded save; the caller should retry."""


def run_match(c, apartment_id: int, staff: str | None, dataset: str = "real", use_ai: bool = True, max_groups: int = 6,
              required_units: list[rules.Unit] | None = None, pool: list[rules.Unit] | None = None) -> dict:
    """One apartment (staff "Find groups"): rules form the candidate groups; each new group is queued for the AI (T6), an existing
    Proposed group with the same members just gets this apartment added to its ranked list (GP6). `use_ai` is kept for older
    callers and ignored: under T6 the AI always assesses a new or changed group, through the queue, before it is ready."""
    from . import exit_exceptions
    with db.retention_guard(c, label="apartment run capture"):
        a = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
        if not a:
            raise ValueError("Apartment not found")
        if a.get("leased_at"):
            raise ValueError("This apartment is marked leased; it is not matched until staff relist it with a new date available")
        units = pool if pool is not None else load_pool(c, dataset)
        owners = {g["member_key"]: g for g in rows(c.execute(
            "SELECT id,member_key FROM groups WHERE status='Proposed' AND member_key IS NOT NULL ORDER BY id"))}
        authority = {key: (g["id"], exit_exceptions.capture_for_group(c, g["id"])) for key, g in owners.items()}
        authority = {key: value for key, value in authority.items() if value[1]}
    apt = apartment_from_row(a)
    started = now_iso()
    exact = {key: exit_exceptions.offers_from_capture(cap, apartment_id) for key, (_, cap) in authority.items()}
    kwargs = {"exception_offers_by_members": exact} if authority else {}
    cands, considered = candidate_groups(apt, units, max_groups=max_groups, **kwargs)
    payloads = [_group_payload(a, g) for g in cands]
    # T5 / GP13: the save is ONE retention-guard step (run row, groups, members, versions / identities, queue rows), as the full run's
    # save is; the GP13 hook and the occupant uid stamping run inside it (binding), the enqueue below commits on its own afterwards.
    with db.retention_guard(c, label="apartment run save"):
        c.execute("INSERT INTO match_runs(apartment_id, started_at, finished_at, staff_member, candidates_considered, groups_passing, ai_used, ai_error, summary_json) VALUES (?,?,?,?,?,?,?,?,?)",
                  (apartment_id, started, now_iso(), staff, considered, len(payloads), 0, None, j({"dataset": dataset, "pool_size": len(units)})))
        run_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        group_ids, queued, reused = [], [], 0
        for i, p in enumerate(payloads):
            key = member_key(cands[i]["units"])
            opt = _apt_option(a, apt, cands[i])
            existing = row(c.execute("SELECT id, ai_status, rule_version, ai_model, evidence_hash FROM groups WHERE status='Proposed' AND member_key=? ORDER BY id DESC LIMIT 1", (key,)))
            if key in authority:
                owner_id, capture = authority[key]
                entry = {"key": key, "units": cands[i]["units"], "apartments": [opt],
                         "_exception_owner": owner_id, "_exception_basis": capture}
                now_apt = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
                same_apts = {apartment_id} if now_apt == a else set()
                reason = _candidate_stale_reason(c, entry, 1 if dataset == "test" else 0, same_apts, 1)
                if reason:
                    raise BookingChanged("The booking changed during this apartment run: " + reason + ". Run it again.")
            if existing:
                gid = existing["id"]
                reused += 1
                previous = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, apartment_id)))
                if previous is None or key in authority:
                    nxt = previous["rank"] if previous else (c.execute("SELECT COALESCE(MAX(rank),0) FROM group_apartments WHERE group_id=?", (gid,)).fetchone()[0] or 0) + 1
                    c.execute("""INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window,
                                 weekly_rent, extra_bed, total_weekly, allocation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (gid, apartment_id, nxt, nxt, opt["rule_score"], None, opt["lease_start"], opt["lease_end"], opt["rate_kind"], opt["exit_window"],
                               opt["weekly_rent"], opt["extra_bed"], opt["total_weekly"], _alloc_json(c, opt["allocation"], cands[i]["units"], score_parts=opt["score_parts"])))
                    _store_exception_fields(c, gid, apartment_id, opt)
                    if previous:
                        c.execute("UPDATE group_apartments SET ai_reason=?,rule_rank=? WHERE group_id=? AND apartment_id=?",
                                  (previous["ai_reason"], previous["rule_rank"], gid, apartment_id))
                    if row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))["apartment_id"] == apartment_id:
                        _switch_current(c, gid, apartment_id)
                    if _assessment_stale(existing):
                        ai_queue.enqueue(c, [gid], priority=ai_queue.PRIORITY_NEW, commit=False)   # earlier rule set / model / no evidence: full review again
                    elif existing["ai_status"] == "ok":
                        ai_queue.enqueue(c, [gid], refresh=True, commit=False)  # GP8(b): members unchanged, apartment ranking refreshed in the background
            else:
                entry = {"key": key, "units": cands[i]["units"], "apartments": [opt], "ai_rating": None, "ai_rationale": None, "ai_status": "pending", "is_primary": False}
                # GP7(a): a group formed for one apartment is a proposal only if none of its members already has one
                busy = row(c.execute(f"SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE g.status='Proposed' AND g.is_primary=1 AND gm.enquiry_id IN ({','.join('?' * len(cands[i]['units']))})", [int(u.id) for u in cands[i]["units"]]))
                entry["is_primary"] = not busy
                gid = persist_group(c, run_id, entry, ai_queue.PENDING_BASIS, run_kind="apartment")
                queued.append(gid)
                if entry["is_primary"]:
                    for u in cands[i]["units"]:
                        c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=? AND status='New'", (int(u.id),))
            group_ids.append(gid)
    if queued:
        ai_queue.enqueue(c, queued, priority=ai_queue.PRIORITY_NEW, reason=f"apartment run {apartment_id}")
    basis = "Rules formed the groups; the AI assesses each new group before it is ready"
    audit(c, staff, "run_match", "apartment", apartment_id, f"{len(payloads)} groups ({len(queued)} new, queued for AI; {reused} existing); dataset={dataset}")
    return {"run_id": run_id, "apartment_id": apartment_id, "groups": group_ids, "candidates_considered": considered, "groups_passing": len(payloads),
            "ai_used": False, "ai_queued": len(queued), "ai_error": None, "ranking_basis": basis, "pool_size": len(units)}


def suggest_replacements(c, group_id: int, remove_enquiry_ids: list[int], staff: str | None, use_ai: bool = True, limit: int = 5) -> dict:
    """GP3: keep the remaining members, drop the named ones, and search for replacement units that pass every rule."""
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (group_id,)))
    if not g:
        raise ValueError("Group not found")
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    apt = apartment_from_row(a)
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (group_id,)))]
    keep_ids = [m for m in member_ids if m not in set(remove_enquiry_ids)]
    first = row(c.execute("SELECT is_test FROM enquiries WHERE id=?", (member_ids[0],))) if member_ids else {"is_test": 0}
    dataset = "test" if first and first["is_test"] else "real"
    all_units = load_pool(c, dataset)
    # also allow the kept members even if their status moved on
    kept = []
    for eid in keep_ids:
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        if e:
            kept.append(unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))))
    _identities_for_computation(c, kept)
    kept_people = sum(u.size for u in kept)
    excluded = set(member_ids)
    pool = [u for u in all_units if int(u.id) not in excluded and _unit_fits_apartment(apt, u)]
    _, cap = rules.apartment_capacity(apt.bedrooms)
    cap = min(cap, rules.APT_TYPES[rules.normalise_type(apt.type_code)]["a4b_max"], rules.MAX_GROUP)
    options = []
    # single replacement unit, or a pair of units, to refill the group
    for u in pool:
        if kept_people + u.size > cap or not all(_pair_ok(u, k) for k in kept):
            continue
        grp = kept + [u]
        chk = rules.check_group(apt, grp)
        if chk.ok:
            score, parts = preference_score(apt, grp, chk)
            options.append({"units": grp, "check": chk, "rule_score": score, "score_parts": parts, "added": [int(u.id)]})
    if len(remove_enquiry_ids) >= 2:
        for i, u in enumerate(pool):
            for v in pool[i + 1:]:
                if kept_people + u.size + v.size > cap or not _pair_ok(u, v) or not all(_pair_ok(u, k) and _pair_ok(v, k) for k in kept):
                    continue
                grp = kept + [u, v]
                chk = rules.check_group(apt, grp)
                if chk.ok:
                    score, parts = preference_score(apt, grp, chk)
                    options.append({"units": grp, "check": chk, "rule_score": score, "score_parts": parts, "added": [int(u.id), int(v.id)]})
    options.sort(key=lambda o: -o["rule_score"])
    options = options[:limit]
    payloads = [_group_payload(a, o) for o in options]
    basis, ai_error = "Rules only (AI unavailable)", None
    if use_ai and payloads:
        try:
            for r in _strict_reviews(ai.assess_groups(a, payloads), payloads):
                if 0 <= r["index"] < len(payloads):
                    _apply_candidate_review(payloads[r["index"]], r)
            basis = "AI"
        except ai.AIUnavailable as e:
            ai_error = ai.failure_text(e)   # Modified A: closed sentence, never the exception's words
    order = sorted(range(len(payloads)), key=lambda i: (-(payloads[i].get("ai_rating") or 0), -payloads[i]["rule_score"]))
    out = []
    for i in order:
        p = payloads[i]
        p["added_enquiry_ids"] = options[i]["added"]
        p["added_names"] = [u.people[0].name for u in options[i]["units"] if int(u.id) in options[i]["added"]]
        p.pop("members_for_ai", None)
        out.append(p)
    audit(c, staff, "suggest_replacements", "group", group_id, f"remove {remove_enquiry_ids}; {len(out)} options ({basis})")
    return {"group_id": group_id, "kept": keep_ids, "removed": remove_enquiry_ids, "options": out, "ranking_basis": basis, "ai_error": ai_error}


# ---------------------------------------------------------------------------
# T6 + GP6: full, group-first run. Every enquiry is compared with every other enquiry under the rules; every rule-passing
# group is kept; the AI assesses EVERY group (batched, in parallel, with retry) and ranks each group's suitable apartments.
# ---------------------------------------------------------------------------
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

RUNS: dict[int, dict] = {}        # live progress by run id (in this process)
_RUN_LOCK = threading.Lock()
AI_BATCH = int(__import__("os").environ.get("MMAI_AI_BATCH", "6"))        # groups per AI call
AI_WORKERS = int(__import__("os").environ.get("MMAI_AI_WORKERS", "4"))    # parallel AI calls
APARTMENTS_PER_GROUP = int(__import__("os").environ.get("MMAI_APARTMENTS_PER_GROUP", "6"))  # ranked apartments kept per group


def member_key(units: list[rules.Unit]) -> str:
    return ",".join(str(i) for i in sorted(int(u.id) for u in units))


def _apt_option(a_row: dict, apt: rules.Apartment, g: dict) -> dict:
    """One ranked-apartment entry for a group (GP6): whole weekly rent (B5), per-room shares (A12(e)), dates and reasons."""
    chk: rules.GroupCheck = g["check"]
    return {
        "apartment_id": int(a_row["id"]), "address": a_row["address"], "suburb": a_row.get("suburb"), "type_code": a_row["type_code"],
        "type_label": rules.APT_TYPES[rules.normalise_type(a_row["type_code"])]["label"], "bills_included": bool(a_row.get("bills_included")),
        "available_from": a_row.get("available_from"),
        "lease_start": chk.lease_start.isoformat(), "lease_end": chk.lease_end.isoformat(), "rate_kind": chk.rate_kind, "exit_window": chk.exit_window,
        "weekly_rent": chk.weekly_rent, "extra_bed": chk.extra_bed, "total_weekly": chk.total_weekly, "allocation": chk.allocation,
        "exception_ref": chk.exception_ref, "exception_total": chk.exception_total,
        "rule_score": g["rule_score"], "score_parts": g["score_parts"], "ai_reason": None,
    }


def _progress(c, run_id: int, **kw):
    with _RUN_LOCK:
        st = RUNS.setdefault(run_id, {})
        st.update(kw)
        snap = dict(st)
    try:
        c.execute("UPDATE match_runs SET progress_json=?, status=? WHERE id=?", (j(snap), snap.get("status", "running"), run_id))
        c.commit()
    except Exception:
        pass


def _assess_batch(batch: list[dict], attempts: int = 2) -> tuple[list[dict] | None, str | None, int]:
    """Call the AI for one batch, retrying once. Returns (results, error, calls_made)."""
    err = None
    for attempt in range(attempts):
        try:
            return ai.assess_groups_and_apartments(batch), None, attempt + 1
        except ai.AIUnavailable as e:
            err = ai.failure_text(e)   # Modified A (9 Nov 2026): closed sentence by kind / code, never the exception's words
            if e.kind in ("credentials", "setup"):   # decided by the trusted kind, never by words in a message
                return None, err, attempt + 1  # no point retrying: credentials missing or expired (restart the server to renew)
            time.sleep(1.5 * (attempt + 1))
    return None, err, attempts


def full_run(c, staff: str | None, dataset: str = "real", use_ai: bool = True, run_id: int | None = None,
             ai_batch: int = AI_BATCH, ai_workers: int = AI_WORKERS, apartments_per_group: int = APARTMENTS_PER_GROUP) -> dict:
    """T6/GP6 full run. Synchronous; `start_full_run` wraps it in a background thread with live progress."""
    t0 = time.time()
    started = now_iso()
    if run_id is None:
        c.execute("INSERT INTO match_runs(apartment_id, started_at, staff_member, ai_used, kind, status, dataset) VALUES (NULL,?,?,0,'full','running',?)", (started, staff, dataset))
        run_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    # Capture (T5). Order matters: the ownership snapshot of today's proposed groups and the T8 watermark are taken BEFORE the pool
    # and the apartments are read, so a proposal another writer creates or re-members from this instant on (whether or not its
    # members made it into the pool read) is "not ours" at the save, and any arrival from this instant on counts as unseen. The
    # computation runs outside any guard; the save re-validates every member, pair, apartment and ownership against current rows.
    is_test = 1 if dataset == "test" else 0
    snapshot = {"groups": _proposed_groups_snapshot(c, is_test)}   # id -> (member_key, member ids) of the proposals this run is entitled to judge
    # T8 watermark: the highest enquiry id of this data set before the pool is read (ids are AUTOINCREMENT, so anything that
    # arrives later has a higher id). The scheduler uses it to count new applicants without the one-second created_at race.
    watermark = c.execute("SELECT COALESCE(MAX(id), 0) FROM enquiries WHERE is_test=?", (is_test,)).fetchone()[0]
    c.execute("UPDATE match_runs SET pool_watermark_id=? WHERE id=?", (watermark, run_id))
    c.commit()
    units = load_pool(c, dataset)
    apts = rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL ORDER BY available_from, address"))  # GP8: leased = unavailable
    snapshot["apartments"] = {int(a["id"]): dict(a) for a in apts}   # the apartment facts every option below was computed from
    _progress(c, run_id, status="running", phase="rules", phase_label="Comparing every enquiry with every other enquiry", done=0, total=len(apts),
              pool_size=len(expand(units)), apartments=len(apts), ai_calls=0, ai_failed=0, groups_total=0, started_at=started)
    # 1. every enquiry vs every other enquiry (pairwise rules), shared with the group builder
    compat, pairs, pairs_ok = pair_matrix(units)
    # 2. rules at every apartment: keep every rule-passing group, aggregated by its members (group-first)
    groups: dict[str, dict] = {}
    considered = 0
    search_stats: dict = {}   # T3(d): the frontier the bounded larger-group search did NOT explore (pairs are always complete)
    for i, a in enumerate(apts):
        apt = apartment_from_row(a)
        cands, cons = candidate_groups(apt, units, max_groups=None, seeds=None, compat=compat, stats=search_stats)
        considered += cons
        for g in cands:
            key = member_key(g["units"])
            entry = groups.setdefault(key, {"key": key, "units": g["units"], "apartments": []})
            entry["apartments"].append(_apt_option(a, apt, g))
        if i % 10 == 9 or i == len(apts) - 1:
            _progress(c, run_id, done=i + 1, groups_total=len(groups))
    # T6 stability: an existing proposed group whose members are all still in the pool is kept if it still passes every rule at an
    # available apartment, even when the greedy builder did not regenerate that exact member set this hour (for example a group
    # the AI refilled after a rejection or a bar). Its apartments are re-checked under the rules like everyone else's.
    is_test = 1 if dataset == "test" else 0
    by_id = {int(u.id): u for u in expand(units)}
    party_of = {int(u.id): x for x in units if isinstance(x, LinkedParty) for u in x.units}  # GP11: a linked party stays whole
    apt_rows = [(a, apartment_from_row(a)) for a in apts]
    # (9 Nov 2026: alternatives are re-checked the same way, so a still-valid group found by an earlier run's coverage pass is kept
    # rather than deleted and recreated; a group that no longer passes still goes. Triage fix 4 follow-up, same day: a retained
    # alternative is re-checked over EVERY available apartment exactly like a retained proposal, not only its stored ones, so a
    # newly listed suitable apartment reaches its options too (GP6(a) consistency; GP8(b) refresh path unchanged). Nothing about
    # who is grouped, weights, primaries or guards moves; a reviewed alternative whose option set changed goes to the same
    # background ranking refresh a proposal already gets.)
    exception_owners = {g["member_key"]: g for g in rows(c.execute("""SELECT g.id, g.member_key, g.is_primary FROM groups g WHERE g.status='Proposed' AND g.member_key IS NOT NULL AND g.id IN
                               (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id""", (is_test,)))}
    # Match the save's latest-id owner; never transplant an older group's approval.
    for g in sorted(exception_owners.values(), key=lambda x: (-int(x["is_primary"] or 0), x["id"])):
        key = g["member_key"]
        from . import exit_exceptions
        captured_exceptions = exit_exceptions.capture_for_group(c, g["id"])
        exception_offers = {int(a["id"]): exit_exceptions.offers_from_capture(captured_exceptions, a["id"])
                            for a, _apt in apt_rows}
        if key in groups:
            groups[key].update(exception_offers=exception_offers, _exception_owner=g["id"],
                               _exception_basis=captured_exceptions)
            _complete_options(groups[key], apt_rows)
            continue
        g_apt_rows = apt_rows   # every available apartment is re-checked; newly suitable ones are added, unsuitable or leased ones go
        ids = [int(x) for x in key.split(",") if x.strip().isdigit()]
        if not ids or any(i not in by_id for i in ids):
            continue  # a member left the pool (barred, withdrawn, in another stage): the group goes
        if any(i in party_of and not all(int(u.id) in ids for u in party_of[i].units) for i in ids):
            continue  # GP11: a member has since brought a friend who is not in this group: the group is rebuilt around the party
        gunits = [by_id[i] for i in ids]
        if len(gunits) > 1 and not all(_pair_ok(gunits[i], gunits[k]) for i in range(len(gunits)) for k in range(i + 1, len(gunits))):
            continue
        entry = None
        for a, apt in g_apt_rows:
            if not all(_unit_fits_apartment(apt, u) for u in gunits):
                continue
            chk = rules.check_group(apt, gunits, exception_offers=exception_offers.get(int(a["id"]), ()))
            if chk.ok:
                sc, parts = preference_score(apt, gunits, chk)
                entry = entry or groups.setdefault(key, {"key": key, "units": gunits, "apartments": [], "_options_complete": True,
                                                        "exception_offers": exception_offers,
                                                        "_exception_owner": g["id"], "_exception_basis": captured_exceptions})
                entry["apartments"].append(_apt_option(a, apt, {"check": chk, "rule_score": sc, "score_parts": parts, "units": gunits}))
    # 2a. Option completion (9 Nov 2026, triage fix 4 - GP6(a) consistency). A member set the seed-and-greedy builder produced at
    #     some apartments is a candidate GROUP; which apartments happened to produce it is an artefact of the greedy per-apartment
    #     pass, not of the rules. Before any top-N trim, rule ranking or AI work, every candidate's options are completed over the
    #     SAME eligible stock every existing primary already gets: each remaining available apartment where every member fits and
    #     the group passes every hard rule is added with its rule score. Nothing here changes who is grouped with whom, the weights,
    #     the primary choice, the gates or the number of options kept (the trim below is unchanged). Retained existing groups
    #     (proposals and alternatives alike) already saw every apartment in the loop above.
    for entry in groups.values():
        if not entry.get("_options_complete"):
            _complete_options(entry, apt_rows)
    apt_by_id = {str(a["id"]): apt for a, apt in apt_rows}
    for entry in list(groups.keys()):
        _finalise_entry(groups[entry], apartments_per_group, apt_by_id)
        if not groups[entry]["apartments"]:   # GP14 guard left no option: not a candidate (cannot happen through the gate; fail closed)
            del groups[entry]
    # 2b. Coverage completion (9 Nov 2026). The seed-and-greedy builder above proposes, for every person, the group of the people
    #     it likes best for them - so in a pool of equals every candidate contains the same early arrival and GP7(a) can make only
    #     one of them the proposal, stranding people for whom a disjoint rule-passing group plainly exists. Remainder rounds: choose
    #     the primaries exactly as the save will (existing proposals first, then rule score), take everyone not yet in a primary,
    #     and run the SAME builder over that remainder at every apartment; repeat while it finds new groups. Every added group passes
    #     every hard rule at its apartment like any other; nothing here reorders, reserves or weights (GP8: a proposal holds nothing),
    #     and the binding selection is still made under the save guard from validated candidates.
    _progress(c, run_id, phase="rules", phase_label="Looking for groups among people not yet proposed", groups_total=len(groups))
    considered_box = [considered]
    # one stability-aware pass around the proposals this run will keep (GP7(a) existing-first): the remainder is the people the
    # kept proposals leave over, so a group found here never competes with a kept proposal for its members. (A pool-only pass was
    # tried and withdrawn: its groups are planned as if no proposal existed, so next to a kept proposal they can take a leftover
    # person into a smaller group and strand the others - e.g. a pair instead of a friend party plus one.) An unchanged pool keeps
    # every group because step 3 re-validates every existing Proposed group directly.
    coverage_rounds = _complete_coverage(groups, units, apt_rows, existing_primary_keys(c, is_test), compat, apartments_per_group, considered_box, stats=search_stats)
    considered = considered_box[0]
    _progress(c, run_id, groups_total=len(groups))
    glist = sorted(groups.values(), key=lambda e: -e["rule_score"])
    # 3. T6 (replaced 26 Oct 2026): the run forms groups with the RULES ONLY. Groups whose members are unchanged since the last run
    #    keep their existing AI assessment (and are not re-run); every new or changed group is queued for the AI, which assesses it
    #    and ranks its apartments before it is shown to reservations as ready. `use_ai` is kept for older callers and ignored.
    is_test = 1 if dataset == "test" else 0
    existing = {}
    for g in rows(c.execute("""SELECT g.* FROM groups g WHERE g.status='Proposed' AND g.member_key IS NOT NULL AND g.id IN
                               (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id""", (is_test,))):
        existing[g["member_key"]] = g  # latest id per member set wins
    annotate_ready(c, existing)
    # 4. GP7(a): one proposed group per enquiry. Rules decide (T3(d), 10 Nov 2026): an unchanged proposal that is genuinely ready
    #    keeps its place (conservative stability boundary, see _protected_primary); among everything else the disjoint set covering
    #    the most residents wins (ties: ready residents, rule scores best-first, then existing proposals retained). Provisional here (for progress);
    #    the binding selection is made again under the save guard from the validated candidates.
    _progress(c, run_id, phase="save", phase_label="Choosing one proposed group per person (rules)")
    _choose_primaries(glist, existing, stats=search_stats)
    # 5. persist: unchanged Proposed groups are updated in place (same id, AI assessment kept); new member sets become new groups
    #    queued for the AI; earlier Proposed groups whose members no longer form a group are deleted. Introduced / confirmed /
    #    awaiting / leased groups are never touched by a run.
    _progress(c, run_id, phase="save", phase_label="Saving groups")
    # T5: the save is ONE retention-guard step (the deletes of groups that no longer form, the in-place updates, the new groups
    # and the enqueue commit together; the AI queue's capture/apply steps and the enquiry deleter wait for it or precede it)
    with db.retention_guard(c, label="full run save"):
        # Everything above was computed from rows read BEFORE this step; another writer (a lease, a bar, an enquiry edit or deletion,
        # an introduction) may have committed meanwhile. So, under the lock: the existing-group map is re-read; every candidate's
        # members are re-validated against the CURRENT rows (same pool status, same facts, same party -> the planned allocation is
        # still about the people who exist now) and a candidate that no longer holds is discarded, its existing group left as it
        # is; deletions are compare-and-swap on status/member_key, so a group that became Introduced/Leased is never deleted.
        existing = {}
        for g in rows(c.execute("""SELECT g.* FROM groups g WHERE g.status='Proposed' AND g.member_key IS NOT NULL AND g.id IN
                                   (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id""", (is_test,))):
            existing[g["member_key"]] = g
        annotate_ready(c, existing)
        # groups another writer created or re-membered AFTER the capture (a per-apartment run, a refill, a replacement): not this
        # run's to judge, neither deleted nor re-proposed; their members are already spoken for when primaries are chosen below
        now_groups = _proposed_groups_snapshot(c, is_test)
        later = {key: g for key, g in existing.items() if now_groups.get(int(g["id"])) != snapshot["groups"].get(int(g["id"]))}
        valid, discarded_keys, discarded_enquiry_ids = [], set(), set()
        current_apts = _current_apartments(c, snapshot["apartments"])   # apartment ids whose row is byte-for-byte what the options were computed from
        load_rejections(c)   # GP7 pairs as of now (used by _pair_ok)
        for e in glist:
            why = _candidate_stale_reason(c, e, is_test, current_apts, apartments_per_group)
            if why is None:
                valid.append(e)
            else:
                discarded_keys.add(e["key"])
                discarded_enquiry_ids |= {int(u.id) for u in e["units"]}
                log.info("full run %s: candidate discarded at save (%s)", run_id, why)   # static category only
        if discarded_keys:
            log.info("full run %s: %d candidate group(s) discarded at save (inputs changed during the run)", run_id, len(discarded_keys))
            # Honest progress (T8): the discarded enquiries were NOT processed by this run. The pool watermark is pulled back below
            # the lowest of them so the scheduler's "new applicants since the last run" count includes them (and anything later)
            # instead of treating them as covered; the summary says the run was incomplete.
            c.execute("UPDATE match_runs SET pool_watermark_id=? WHERE id=? AND pool_watermark_id IS NOT NULL AND pool_watermark_id >= ?",
                      (min(discarded_enquiry_ids) - 1, run_id, min(discarded_enquiry_ids)))
        # untouched groups: a later writer's (whatever this run computed for the same member set) and the existing group behind a
        # discarded candidate. They are not rewritten here; when primary, their members are reserved so no validated alternative
        # can become a second proposal for the same enquiry (GP7(a)).
        untouched = dict(later)
        untouched.update({key: existing[key] for key in discarded_keys if key in existing})
        glist = [e for e in valid if e["key"] not in untouched]
        reserved = {int(m["enquiry_id"]) for g in untouched.values() if g["is_primary"]
                    for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))}
        search_stats.pop("selection_nodes", None); search_stats.pop("selection_truncated", None)   # report the binding selection only
        _choose_primaries(glist, existing, pre_taken=reserved, stats=search_stats)   # the binding GP7(a) selection, from validated candidates only
        new_keys = {e["key"] for e in glist} | set(untouched)   # an untouched key protects its existing group: the next run decides
        # G-SL2: every group deletion goes through stock_lifecycle.delete_groups (pending stock work closes with the group, in this step)
        from . import stock_lifecycle
        stale = [int(r["id"]) for key, g in existing.items() if key not in new_keys
                 for r in rows(c.execute("SELECT id FROM groups WHERE id=? AND status='Proposed' AND member_key=?", (g["id"], key)))]
        stock_lifecycle.delete_groups(c, stale)
        memberless = [int(r["id"]) for r in rows(c.execute("SELECT id FROM groups WHERE status='Proposed' AND id NOT IN (SELECT group_id FROM group_members) ORDER BY id"))]
        stock_lifecycle.delete_groups(c, memberless)
        c.execute("UPDATE enquiries SET status='New' WHERE is_test=? AND status='Proposed in group'", (is_test,))
        for key, g in untouched.items():   # untouched primary groups keep their members' pool status
            if g["is_primary"]:
                c.execute("UPDATE enquiries SET status='Proposed in group' WHERE status='New' AND id IN (SELECT enquiry_id FROM group_members WHERE group_id=?)", (g["id"],))
        group_ids, new_ids, kept_ids, refresh_ids = [], [], [], []
        for e in glist:
            old = existing.get(e["key"])
            if old:
                gid = old["id"]
                changed = _merge_apartments(c, gid, e["apartments"], e["units"])
                c.execute("UPDATE groups SET match_run_id=?, is_primary=?, run_kind='full', rule_score=?, updated_at=? WHERE id=?",
                          (run_id, 1 if e["is_primary"] else 0, e["rule_score"], now_iso(), gid))
                e["ai_status"] = old["ai_status"]
                kept_ids.append(gid)
                if _assessment_stale(old):   # built before the T6 queue, or reviewed under an earlier rule set / another model: review it again now
                    new_ids.append(gid)
                    ai_queue.enqueue(c, [gid], priority=ai_queue.PRIORITY_NEW if e["is_primary"] else ai_queue.PRIORITY_ALT, commit=False)
                    e["ai_status"] = "pending"
                elif changed and old["ai_status"] == "ok":
                    refresh_ids.append(gid)  # GP8(b): members unchanged, new apartments added: stays ready (is_ready carve-out) while the ranking is refreshed
            else:
                e["ai_rating"] = None; e["ai_rationale"] = None; e["ai_status"] = "pending"
                gid = persist_group(c, run_id, e, ai_queue.PENDING_BASIS, run_kind="full")
                c.execute("UPDATE groups SET ai_priority=?, ai_queued_at=? WHERE id=?", (ai_queue.PRIORITY_NEW if e["is_primary"] else ai_queue.PRIORITY_ALT, now_iso(), gid))
                new_ids.append(gid)
            group_ids.append(gid)
            if e["is_primary"]:
                for u in e["units"]:
                    c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=? AND status='New'", (int(u.id),))
        if refresh_ids:
            # succ3 (10 Nov 2026): the GP8(b) refresh marks are part of the save step (they describe groups this step changed), so
            # the group changes and their queue marks commit together at the step's own boundary - and the connection is clean
            # for the per-group re-offer steps below (a guard refuses a pending write at entry; before step 5a existed the next
            # progress commit absorbed these marks, which hid the ordering). Rollback of the step still discards them with it.
            ai_queue.enqueue(c, refresh_ids, refresh=True, commit=False)
    # 5a. Stock lifecycle (succ3, 10 Nov 2026, L10): an Introduced / Group confirmed group held with NO current offer after a stock
    #     loss keeps its identity and members (a run never touches it) - but when one of ITS OWN stored options is listed, unleased
    #     and passes every hard rule for its members today, that option is offered again within the same group (GP6(b): from the
    #     group's own ranked list), through the one switch that stamps the offer-change flag for introduced groups. Membership
    #     versions, responses and the clock are untouched; a group still being re-checked, or whose options cannot be judged, waits.
    from . import stock_lifecycle
    reoffered = stock_lifecycle.reoffer_held_groups(c, is_test, staff)
    refresh_ids += reoffered
    if new_ids or refresh_ids:
        ai_queue.kick()
    # 6. GP10: look again for replacements for groups awaiting one (rules only; flagged, never applied automatically)
    _progress(c, run_id, phase="save", phase_label="Checking groups awaiting a replacement")
    awaiting = scan_awaiting(c, dataset, use_ai=False)
    covered = len({int(u.id) for e in glist if e["is_primary"] for u in e["units"]})   # from the groups actually saved, never from a discarded plan
    in_any = len({int(u.id) for e in glist for u in e["units"]})
    residents_primary = len(_people_ids([u for e in glist if e["is_primary"] for u in e["units"]]))   # every actual person once (forms vs residents reported apart)
    residents_any = len(_people_ids([u for e in glist for u in e["units"]]))
    units = expand(units)  # counts below are per enquiry
    duration = round(time.time() - t0, 1)
    ready = sum(1 for gid in group_ids if ai_queue.is_ready(c, row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))["ready"])   # T6: the one readiness check
    summary = {"dataset": dataset, "awaiting_checked": awaiting["checked"], "awaiting_replacement_found": awaiting["found"], "pool_size": len(units), "apartments": len(apts), "pairs_compared": pairs, "pairs_compatible": pairs_ok,
               "candidates_considered": considered, "coverage_rounds": coverage_rounds, "groups_total": len(glist), "primary_groups": sum(1 for e in glist if e["is_primary"]),
               # T3(d) search frontier: >0 means the larger-group search was bounded and "no group" for a person is NOT proven infeasible
               "search_targets_truncated": int(search_stats.get("targets_truncated", 0)), "search_seat_options_untried": int(search_stats.get("seat_options_untried", 0)), "search_seat_options_date_infeasible": int(search_stats.get("seat_options_date_infeasible", 0)),
               "search_targets_stopped_at_first_valid": int(search_stats.get("targets_stopped_at_first_valid", 0)),   # larger groups accepted first-valid: never exhaustive
               "search_larger_groups_exhaustive": False,   # declared: only two-unit groups are enumerated completely; larger sizes are a bounded, first-valid search
               "selection_truncated": int(search_stats.get("selection_truncated", 0)),   # 1 = the binding coverage selection hit its node budget: best found, not a proven maximum
               "coverage_selection_truncated": int(search_stats.get("coverage_selection_truncated", 0)),   # remainder-round (step 2b) selections that hit the budget: those rounds' primaries were best found, not proven
               "enquiries_in_primary_group": covered, "enquiries_in_any_group": in_any, "enquiries_unmatched": len(units) - in_any,
               "residents_in_primary_group": residents_primary, "residents_in_any_group": residents_any,   # people, not forms (a couple form = 2 residents)
               "groups_unchanged": len(kept_ids), "groups_new": len(new_ids), "groups_ready": ready, "groups_ai_pending": len(new_ids) + sum(1 for e in glist if e["ai_status"] in ("pending", "running") and e["key"] in existing),
               "groups_ai_failed": sum(1 for e in glist if e["ai_status"] == "failed"), "apartment_refresh_queued": len(refresh_ids),
               "ai_calls": 0, "ai_failed_batches": 0, "ai_groups_ok": ready, "ai_error": None, "duration_s": duration, "use_ai": False, "groups_discarded_at_save": len(discarded_keys),
               "discarded_enquiry_ids": sorted(discarded_enquiry_ids), "complete": not discarded_keys,
               "primary_new": sum(1 for e in glist if e["is_primary"] and e["key"] not in existing)}
    c.execute("""UPDATE match_runs SET finished_at=?, candidates_considered=?, groups_passing=?, ai_used=?, ai_error=?, summary_json=?, status='done',
                 pairs_compared=?, groups_total=?, ai_calls=?, ai_failed=?, duration_s=? WHERE id=?""",
              (now_iso(), considered, len(glist), 0, None, j(summary), pairs, len(glist), 0, 0, duration, run_id))
    c.commit()
    _progress(c, run_id, status="done", phase="done", phase_label="Finished", summary=summary)
    audit(c, staff, "full_run", "match_run", run_id, f"{len(glist)} groups ({summary['primary_groups']} proposed; {len(new_ids)} new queued for AI, {len(kept_ids)} unchanged), {duration}s; dataset={dataset}"
          + (f"; INCOMPLETE: {len(discarded_keys)} candidate group(s) discarded at save because member facts changed during the run" if discarded_keys else ""))
    return {"run_id": run_id, **summary, "group_ids": group_ids, "new_group_ids": new_ids}


def _proposed_groups_snapshot(c, is_test: int) -> dict[int, tuple]:
    """Ownership snapshot of this data set's Proposed groups: id -> (member_key, frozenset of member enquiry ids). Captured with
    the pool and compared at the save boundary; a group absent from or different in the capture is another writer's."""
    out = {}
    for g in rows(c.execute("""SELECT g.id, g.member_key FROM groups g WHERE g.status='Proposed' AND g.id IN
                               (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?) ORDER BY g.id""", (is_test,))):
        members = frozenset(int(r["enquiry_id"]) for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],))))
        out[int(g["id"])] = (g["member_key"], members)
    return out


def _complete_options(entry: dict, apt_rows: list) -> None:
    """Add to a candidate group's options every other available apartment where each member fits and the group passes every hard
    rule (rules.check_group), scored exactly as the builder scores (preference_score). An already listed apartment is re-evaluated
    when exact booking authority supplies additional candidates. Ranking and top-N trim happen in _finalise_entry."""
    have = {int(o["apartment_id"]) for o in entry["apartments"]}
    gunits = entry["units"]
    for a2, apt2 in apt_rows:
        exact = entry.get("exception_offers", {}).get(int(a2["id"]), ())
        if (int(a2["id"]) in have and not exact) or not all(_unit_fits_apartment(apt2, u) for u in gunits):
            continue
        chk2 = rules.check_group(apt2, gunits, exception_offers=exact)
        if exact:
            entry["apartments"] = [o for o in entry["apartments"] if int(o["apartment_id"]) != int(a2["id"])]
        if chk2.ok:
            sc2, parts2 = preference_score(apt2, gunits, chk2)
            entry["apartments"].append(_apt_option(a2, apt2, {"check": chk2, "rule_score": sc2, "score_parts": parts2, "units": gunits}))
    entry["_options_complete"] = True


def _finalise_entry(entry: dict, apartments_per_group: int, apt_by_id: dict | None = None) -> None:
    """Rank one candidate group's apartment options deterministically and attach the packet fields the save and the AI queue use."""
    if apt_by_id:  # GP14 fail-closed guard: the eligibility gate already removed excluded options; this catches one built any other way
        entry["apartments"] = [o for o in entry["apartments"]
                               if str(o["apartment_id"]) not in apt_by_id or not any(unit_excludes_apartment(u, apt_by_id[str(o["apartment_id"])]) for u in entry["units"])]
    entry["apartments"].sort(key=lambda o: (-o["rule_score"], o["total_weekly"], o["apartment_id"]))   # deterministic; same key as ai_queue's refresh
    for r, o in enumerate(entry["apartments"], start=1):
        o["rule_rank"] = r
    entry["apartments_all"] = entry["apartments"]
    entry["apartments"] = entry["apartments"][:apartments_per_group]
    if not entry["apartments"]:   # GP14 guard left nothing: callers drop the entry (never saved, never scored)
        entry["rule_score"] = None
        return
    entry["rule_score"] = entry["apartments"][0]["rule_score"]
    entry["members_for_ai"] = members_for_ai(entry["units"])
    entry["person_refs"] = person_refs(entry["units"])
    entry["ai_rating"] = None; entry["ai_rationale"] = None; entry["ai_status"] = "skipped"


def existing_primary_keys(c, is_test: int) -> dict:
    """member_key -> the primary Proposed group row (with `_ready` stamped, see annotate_ready) for this data set; the stability
    input _choose_primaries reads during coverage completion; a read, taken before the save guard like the rest of the plan."""
    existing = {g["member_key"]: g for g in rows(c.execute("""SELECT g.* FROM groups g WHERE g.status='Proposed' AND g.is_primary=1
                               AND g.member_key IS NOT NULL AND g.id IN (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?)""", (is_test,)))}
    return annotate_ready(c, existing)


def uncovered_units(units: list, glist: list[dict]) -> list:
    """Pool units (linked parties kept whole) with no member in any primary candidate of `glist`."""
    taken = {int(u.id) for e in glist if e.get("is_primary") for u in e["units"]}
    return [x for x in units if all(int(u.id) not in taken for u in expand([x]))]


def _complete_coverage(groups: dict, units: list, apt_rows: list, existing: dict, compat: dict, apartments_per_group: int,
                       considered_box: list, max_rounds: int | None = None, stats: dict | None = None) -> int:
    """Remainder rounds (see full_run step 2b). Mutates `groups` (adds finalised candidate entries) and considered_box[0]; returns
    the number of rounds that added groups. Each round is ONE disjoint pass of the builder per apartment over the uncovered people
    (each person placed at most once per apartment pass), so a pool of equals is covered in one round; further rounds only pick
    up people whose greedy group failed the rule check. Terminates: a productive round adds at least one NEW candidate key, and the
    candidate space is finite, so rounds are bounded by the number of distinct candidate groups (callers may also pass
    max_rounds). Under the bounded selection a newly added disjoint group is normally made primary by the next selection (it raises
    resident coverage), but when that selection hits its node budget this is the best found, not proven - the per-round
    selection stats are accumulated in `stats` as coverage_selection_nodes / coverage_selection_truncated (reported in the run
    summary apart from the binding selection's selection_truncated)."""
    apt_by_id = {str(a["id"]): apt for a, apt in apt_rows}   # GP14 guard lookup, built once
    rounds = 0
    while max_rounds is None or rounds < max_rounds:
        glist = sorted(groups.values(), key=lambda e: -e["rule_score"])
        sel_stats: dict = {}
        _choose_primaries(glist, existing, stats=sel_stats)
        if stats is not None:
            stats["coverage_selection_nodes"] = stats.get("coverage_selection_nodes", 0) + int(sel_stats.get("selection_nodes", 0))
            stats["coverage_selection_truncated"] = stats.get("coverage_selection_truncated", 0) + int(sel_stats.get("selection_truncated", 0))
        remaining = uncovered_units(units, glist)
        if sum(x.size for x in remaining) < rules.MIN_GROUP:
            break
        added = False
        for a, apt in apt_rows:
            cands, cons = candidate_groups(apt, remaining, max_groups=None, seeds=None, compat=compat, disjoint=True, stats=stats)
            considered_box[0] += cons
            for g in cands:
                key = member_key(g["units"])
                if key in groups:
                    continue   # already a candidate (its apartments were ranked from the full pool): nothing new to add
                entry = {"key": key, "units": g["units"], "apartments": [_apt_option(a, apt, g)]}
                # the same member set may pass at other apartments too: collect them before finalising (same helper as step 2a)
                _complete_options(entry, apt_rows)
                _finalise_entry(entry, apartments_per_group, apt_by_id)
                if not entry["apartments"]:
                    continue   # GP14 guard (see run_matching): never a candidate without an eligible option
                groups[key] = entry
                added = True
        if not added:
            break
        rounds += 1
    return rounds


SELECTION_NODE_BUDGET = 20000   # bounded branch-and-bound nodes for the disjoint selection (T3(d), 10 Nov 2026); greedy incumbent always kept


def _selection_score(chosen: list[dict], existing: dict) -> tuple:
    """Lexicographic objective of a disjoint selection (no weights, no new rule), T3(d) 10 Nov 2026 - per-person outcome:
      1. RESIDENTS covered (every actual person once: a couple / friends form counts each person it brings; disjointness is still
         by enquiry / party ownership, so a party is never split);
      2. residents in a group that is GENUINELY ready now (ai_queue.is_ready: approved review, both briefs, evidence current) -
         three ready pairs (6) never beat one ready seven-person group (7) merely by group count; an 'ok' review with a
         pending / failed brief or an outstanding refresh counts for nothing here;
      3. the existing rule scores compared best-first (the selection holding the highest-scoring group wins, as the former
         "best rule score wins" greedy did; a sum would favour many small groups);
      4. existing proposals retained - a stability tie ONLY at equal coverage, readiness and quality (no quality-blind
         incumbency): a labelled engineering fallback, not a rule."""
    people = frozenset().union(*[e["_people"] for e in chosen]) if chosen else frozenset()
    ready_people = frozenset().union(*[e["_people"] for e in chosen if _ready_group(e, existing)]) if chosen else frozenset()
    return (len(people), len(ready_people), tuple(sorted((round(e["rule_score"], 6) for e in chosen), reverse=True)),
            sum(1 for e in chosen if _is_existing_primary(e, existing)))


def _people_ids(units) -> frozenset:
    """One id per actual resident behind the units (the lead's enquiry id and each party member's person id)."""
    out = set()
    for u in units:
        ppl = getattr(u, "people", None) or []
        if ppl:
            out.update(str(p.id) for p in ppl)
        else:
            out.update(f"{u.id}#{k}" for k in range(int(getattr(u, "size", 1) or 1)))
    return frozenset(out)


def _ready_group(e: dict, existing: dict) -> bool:
    """The candidate's existing group is genuinely ready NOW: the row's `_ready` annotation, stamped by annotate_ready from
    ai_queue.is_ready (the one product readiness predicate) by every production caller (full_run twice, existing_primary_keys).
    FAIL CLOSED: a row without the annotation is NOT ready and NOT protected - status fields (ai_status 'ok', an approved verdict,
    brief_status) are never approximated into readiness, so a stale approval cannot protect itself. Unit fixtures mark `_ready`
    explicitly."""
    g = existing.get(e["key"])
    if not g:
        return False
    return bool(g.get("_ready", False))


def _is_existing_primary(e: dict, existing: dict) -> bool:
    g = existing.get(e["key"])
    return bool(g) and bool(g.get("is_primary"))


def _ai_ok(e: dict, existing: dict) -> bool:   # kept for older callers: approved review on record (NOT readiness; see _ready_group)
    g = existing.get(e["key"])
    return bool(g) and g.get("ai_status") == "ok"


def _protected_primary(e: dict, existing: dict) -> bool:
    """Conservative stability boundary (T3(d) C2, labelled on purpose): a group that already IS the proposal AND is genuinely
    ready now (_ready_group: approved review, both staff briefs, evidence current - not ai_status 'ok' alone) keeps its place; a
    re-run must not dissolve ready work to re-pack people. Every other existing primary (pending, running, failed, held, revise,
    approved-but-brief-failed, stale, refresh outstanding) competes on coverage like every other candidate, with the retention
    tie of _selection_score as its only advantage. This is an engineering boundary, not GP7(a): GP7(a) only says one proposed
    group per person."""
    g = existing.get(e["key"])
    return bool(g) and bool(g.get("is_primary")) and _ready_group(e, existing)


def annotate_ready(c, existing: dict) -> dict:
    """Stamp every existing group row with `_ready` (ai_queue.is_ready, the one readiness check). Rows whose review is not
    approved or whose briefs are not ok are stamped False without the full check. Returns `existing` for chaining."""
    from . import ai_queue
    for g in existing.values():
        if g.get("ai_status") == "ok" and g.get("review_verdict") == "approve" and g.get("brief_status") == "ok":
            g["_ready"] = bool(ai_queue.is_ready(c, g)["ready"])
        else:
            g["_ready"] = False
    return existing


def select_disjoint(cands: list, ids_of, weight_ids, score, taken: set | None = None, budget: int = SELECTION_NODE_BUDGET,
                    stats: dict | None = None, extra_orders: list | None = None) -> list:
    """Bounded maximum-coverage selection of DISJOINT candidates (T3(d), 10 Nov 2026). `ids_of(e)` = the people e holds
    (frozenset); `weight_ids(e)` = the subset counted by the first objective component (e.g. all members, or members only when e
    is ready); `score(chosen)` = the full lexicographic tuple whose FIRST component is |union of weight_ids over chosen| (so the
    branch bound below is sound). `cands` must already be in the deterministic order the greedy incumbent should take.
    Seeded with that greedy incumbent, then an iterative unit-branching branch-and-bound (no recursion depth) with at most `budget` nodes: an
    exhausted budget returns the best found - never less than the greedy fallback - and `stats` records selection_nodes and
    selection_truncated (1 = not a proven maximum). Ties between equal scores keep the first found (deterministic). `extra_orders`
    (optional list of key functions) seed further greedy incumbents in other deterministic orders - e.g. the former
    "existing proposal first, then rule score" order - and the best incumbent is kept, so a budget-limited search never returns
    less than any of them merely because of traversal order."""
    taken = set(taken or ())
    cands = [e for e in cands if not (ids_of(e) & taken)]

    def greedy(seq):
        out, t = [], set(taken)
        for e in seq:
            if not (ids_of(e) & t):
                out.append(e)
                t |= ids_of(e)
        return out
    best = greedy(cands)
    best_score = score(best)
    for key in extra_orders or ():
        alt = greedy(sorted(cands, key=key))
        alt_score = score(alt)
        if alt_score > best_score:
            best, best_score = alt, alt_score
    n = len(cands)
    # Branching on the hardest-to-place UNIT, not on candidate index (T3(d) C2 repair, 10 Nov 2026, reviewer finding: the index
    # order DFS spent its whole node budget without reaching a selection the stored candidates already supported). At each node
    # the uncovered unit (enquiry / party, the disjointness key) with the FEWEST still-feasible candidates is chosen; each
    # candidate holding it is one branch (explored first, in `cands` order), leaving the unit uncovered is the last branch.
    # Every disjoint selection is still reachable (each unit is either covered by a chosen candidate or set aside), the bound is
    # the first objective component only (residents a branch could still cover: current weight + every feasible candidate's
    # weight), pruning only when strictly below the incumbent, so equal-coverage branches remain open for the later components.
    # Leaves are evaluated with the full `score`: adding a disjoint candidate never lowers any component of the objectives used
    # here (unions grow, the best-first score tuple only gains elements, retention only gains), so maximal selections suffice.
    ids_cache = {id(e): frozenset(ids_of(e)) for e in cands}
    w_cache = {id(e): frozenset(weight_ids(e)) for e in cands}
    index = {id(e): i for i, e in enumerate(cands)}
    nodes, truncated = 0, False
    root_feasible = tuple(range(n))
    stack: list[tuple[frozenset, frozenset, tuple, frozenset, tuple]] = [(frozenset(taken), frozenset(), (), frozenset(), root_feasible)]   # (covered, set aside, chosen, weighted, feasible idx)
    while stack:
        nodes += 1
        if nodes > budget:
            truncated = True
            break
        cov, aside, ch, w, feas = stack.pop()
        blocked = cov | aside
        feas = tuple(i for i in feas if not (ids_cache[id(cands[i])] & blocked))
        if not feas:
            sc = score(list(ch))
            if sc > best_score:
                best, best_score = list(ch), sc
            continue
        reach = set(w)
        per_unit: dict = {}
        for i in feas:
            e = cands[i]
            reach |= w_cache[id(e)]
            for uid in ids_cache[id(e)]:
                per_unit.setdefault(uid, []).append(i)
        if len(reach) < best_score[0]:
            continue   # cannot even equal the incumbent's first component: no need to look further down this branch
        unit = min(per_unit, key=lambda u: (len(per_unit[u]), str(u)))   # fewest options first; deterministic tie
        stack.append((cov, aside | {unit}, ch, w, feas))   # set this unit aside (explored last)
        for i in reversed(per_unit[unit]):                  # cover it with each feasible candidate, in `cands` order (first explored first)
            e = cands[i]
            stack.append((cov | ids_cache[id(e)], aside, ch + (e,), w | w_cache[id(e)], feas))
    if stats is not None:
        stats["selection_nodes"] = stats.get("selection_nodes", 0) + nodes
        stats["selection_truncated"] = stats.get("selection_truncated", 0) + (1 if truncated else 0)
    return best


def _choose_primaries(glist: list[dict], existing: dict, pre_taken: set | None = None, stats: dict | None = None) -> None:
    """GP7(a) in place: one proposed group per person, chosen from `glist` (rule-passing candidates) as a DISJOINT set.
    T3(d) (10 Nov 2026) replaced "existing primary first, then rule score" with coverage-first selection:
      1. protected tier - existing primaries that are genuinely ready now (see _protected_primary) keep their place;
      2. among the rest, the disjoint set that covers the MOST residents wins (ties, in order: more residents in genuinely ready
         groups, then the existing rule scores compared best-first, then existing proposals retained, then deterministic order) -
         select_disjoint: bounded branch-and-bound seeded with two greedy incumbents (coverage order and the former
         proposal-first/rule-score order), so an exhausted budget never returns less than either; `stats` gets selection_nodes /
         selection_truncated (truncated = best found, not a proven maximum).
    `pre_taken` are enquiries already proposed elsewhere (groups written by a later writer): no candidate holding one of them can
    be primary. Sets e["is_primary"] on every entry and leaves `glist` sorted primaries first, then by rule score."""
    taken: set[int] = set(pre_taken or ())
    for e in glist:
        e["_ids"] = frozenset(int(u.id) for u in e["units"])      # enquiry / party ownership: disjointness
        e["_people"] = _people_ids(e["units"])                     # residents: the objective
        e["is_primary"] = False
    order = lambda e: (-len(e["_people"]), 0 if _ready_group(e, existing) else 1, -e["rule_score"], 0 if _is_existing_primary(e, existing) else 1, e["key"])   # deterministic
    former = lambda e: (0 if _is_existing_primary(e, existing) else 1, -e["rule_score"], e["key"])   # the pre-T3(d) greedy order
    for e in sorted((e for e in glist if _protected_primary(e, existing)), key=order):
        if not (e["_ids"] & taken):
            e["is_primary"] = True
            taken |= e["_ids"]
    cands = sorted((e for e in glist if not e["is_primary"]), key=order)
    chosen = select_disjoint(cands, ids_of=lambda e: e["_ids"], weight_ids=lambda e: e["_people"], score=lambda ch: _selection_score(ch, existing),
                             taken=taken, stats=stats, extra_orders=[former])
    for e in chosen:
        e["is_primary"] = True
    for e in glist:
        e.pop("_ids", None)
        e.pop("_people", None)
    glist.sort(key=lambda e: (0 if e["is_primary"] else 1, -e["rule_score"]))


def _current_apartments(c, snapshot: dict) -> set[int]:
    """Ids of snapshot apartments whose row is unchanged NOW (still listed, not leased, same rent/dates/bedrooms/every column). An
    option computed from an apartment that changed is never written back from the old figures."""
    now = {int(a["id"]): dict(a) for a in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL"))}
    return {aid for aid, a in snapshot.items() if now.get(aid) == a}


def _candidate_stale_reason(c, e: dict, is_test: int, current_apts: set[int], apartments_per_group: int) -> str | None:
    """Guard-boundary re-validation of ONE planned group against the rows as they are now. None = still sound (its options
    trimmed to unchanged apartments, re-ranked); otherwise a static category naming what moved. Checked: every member's pool
    status/dataset/bar and rebuilt Unit (facts, party composition, link), GP11 party wholeness (a friend linked meanwhile must be in
    the group), GP7 pair rules with the rejections as of now, and every offered apartment's row."""
    if e.get("_exception_owner") is not None:
        from . import exit_exceptions
        owner = row(c.execute("""SELECT g.id FROM groups g WHERE g.status='Proposed' AND g.member_key=? AND g.id IN
                              (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?)
                              ORDER BY g.id DESC LIMIT 1""", (e["key"], is_test)))
        if not owner or int(owner["id"]) != int(e["_exception_owner"]):
            return "booking exception owner changed during the run"
        if exit_exceptions.capture_for_group(c, e["_exception_owner"]) != e["_exception_basis"]:
            return "booking exception authority changed during the run"
    if not _units_still_current(c, e["units"], is_test):
        return "member facts, party, pool status or link changed"
    ids = {int(u.id) for u in e["units"]}
    for u in e["units"]:
        for pid in _pool_party_ids(c, int(u.id), is_test):
            if pid not in ids:
                return "a linked friend joined the party during the run"
    us = list(e["units"])
    if len(us) > 1 and not all(_pair_ok(us[i], us[k]) for i in range(len(us)) for k in range(i + 1, len(us))):
        return "a pair rule or rejection changed during the run"
    opts = [o for o in e.get("apartments_all", e["apartments"]) if int(o["apartment_id"]) in current_apts]
    if not opts:
        return "no offered apartment is unchanged (leased, edited or delisted during the run)"
    for r, o in enumerate(opts, start=1):
        o["rule_rank"] = r
    e["apartments_all"] = opts
    e["apartments"] = opts[:apartments_per_group]
    e["rule_score"] = opts[0]["rule_score"]
    return None


def _pool_party_ids(c, enquiry_id: int, is_test: int) -> set[int]:
    """GP11(b): the enquiries this one must be grouped with NOW: its link root (if in the pool) and every pool friend linked to that
    root. Mirrors cluster_pool on current rows."""
    e = row(c.execute("SELECT id, linked_to_enquiry_id FROM enquiries WHERE id=?", (enquiry_id,)))
    if not e:
        return set()
    root = e["linked_to_enquiry_id"] or e["id"]
    ph = ",".join("?" * len(POOL_STATUSES))
    return {r["id"] for r in rows(c.execute(f"SELECT id FROM enquiries WHERE (id=? OR linked_to_enquiry_id=?) AND is_test=? AND status IN ({ph}) AND barred_at IS NULL",
                                              (root, root, is_test, *POOL_STATUSES)))}


def _units_still_current(c, units: list, is_test: int) -> bool:
    """Guard-boundary re-validation (T5): every real enquiry behind a planned group is re-read NOW and must still be in the pool
    (same dataset, pool status, not barred) and rebuild to exactly the same Unit (facts, party composition, links) the plan was
    computed from. Any difference discards the plan: a bed allocation or member set computed from older rows is never persisted."""
    for u in expand(units):
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (int(u.id),)))
        if not e or int(e.get("is_test") or 0) != int(is_test) or e.get("status") not in POOL_STATUSES or e.get("barred_at"):
            return False
        pm = rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (int(u.id),)))
        try:
            fresh = unit_from_row(e, pm)
        except Exception:
            return False
        if _engine_view(fresh) != _engine_view(u):
            return False
    return True


# The enquiry columns the engine reads through Unit.extra (plus every evidence field): a change in any of them is a fact change.
# Columns outside this projection (status moving between the two pool statuses, updated_at, staff notes) are not.
_UNIT_EXTRA_FIELDS = tuple(dict.fromkeys((*EVIDENCE_ENQUIRY_FIELDS, "id", "is_test", "created_at", "linked_to_enquiry_id")))


def _engine_view(u: rules.Unit) -> tuple:
    """A Unit with its raw row reduced to the engine-relevant projection, for equality at the guard boundary."""
    x = u.extra if isinstance(u.extra, dict) else {}
    # identity provenance (name/email pair for uid stamping) is not an engine input: it is proved separately at the write
    # (_stamp_people) and does not make a plan stale here
    people = [dataclasses.replace(p, ident_name=None, ident_email=None, ident_uid=None) if isinstance(p, rules.Person) else p for p in (u.people or [])]
    return (dataclasses.replace(u, extra=None, people=people), tuple((k, x.get(k)) for k in _UNIT_EXTRA_FIELDS))


def _merge_apartments(c, gid: int, new_opts: list[dict], units: list | None = None) -> bool:
    """Refresh an unchanged group's ranked apartment list from this run's rule results while keeping the AI's order and reasons for
    the apartments it already ranked; newly suitable apartments go after them in rule order, apartments no longer available go.
    Returns True when the set of apartments changed. The group's current apartment is kept when it is still on the list."""
    old = rows(c.execute("SELECT apartment_id, ai_reason FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,)))
    old_ids = [o["apartment_id"] for o in old]
    reasons = {o["apartment_id"]: o["ai_reason"] for o in old}
    by_id = {o["apartment_id"]: o for o in new_opts}
    order = [aid for aid in old_ids if aid in by_id] + [o["apartment_id"] for o in new_opts if o["apartment_id"] not in set(old_ids)]
    c.execute("DELETE FROM group_apartments WHERE group_id=?", (gid,))
    for rank, aid in enumerate(order, start=1):
        o = by_id[aid]
        c.execute("""INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window,
                     weekly_rent, extra_bed, total_weekly, allocation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (gid, aid, rank, o.get("rule_rank"), o["rule_score"], reasons.get(aid), o["lease_start"], o["lease_end"], o["rate_kind"], o["exit_window"],
                   o["weekly_rent"], o["extra_bed"], o["total_weekly"], _alloc_json(c, o["allocation"], units, score_parts=o.get("score_parts"))))
        _store_exception_fields(c, gid, aid, o)
    g = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))
    if order:
        _switch_current(c, gid, g["apartment_id"] if g["apartment_id"] in set(order) else order[0])
    return set(old_ids) != set(order)


def _assessment_stale(g: dict) -> bool:
    """A stored assessment that may NOT be reused (T6, 8 Nov 2026): no queue state at all (pre-queue group), or an 'ok' review
    made under another rule version or model or without an evidence snapshot. Pending/running/failed/revise/held/stale rows
    are already in the queue's hands and are left alone."""
    st = g.get("ai_status")
    if st not in ("ok", "pending", "running", "failed", "revise", "held", "stale"):
        return True
    if st == "ok":
        return g.get("rule_version") != rules.RULE_VERSION or g.get("ai_model") != ai.MODEL or not g.get("evidence_hash")
    return False


def persist_group(c, run_id: int | None, e: dict, basis: str, run_kind: str) -> int:
    """Insert one group with its ranked apartment list (GP6). groups.apartment_id = the top-ranked apartment, so the
    lease, rent, allocation and member rows describe that apartment (older app versions read them unchanged)."""
    if any(o.get("exception_ref") is not None or o.get("exception_total") is not None for o in e["apartments"]):
        raise ValueError("A new group cannot inherit another booking's exception authority")
    top = e["apartments"][0]
    ai_ok = e.get("ai_rating") is not None
    c.execute("""INSERT INTO groups(match_run_id, apartment_id, status, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly,
                 allocation_json, rule_score, ai_rating, ai_rationale, ai_model, ranking_basis, created_at, updated_at, member_key, is_primary, ai_status, run_kind)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (run_id, top["apartment_id"], "Proposed", top["lease_start"], top["lease_end"], top["rate_kind"], top["exit_window"], top["weekly_rent"], top["extra_bed"], top["total_weekly"],
               _alloc_json(c, top["allocation"], e["units"], score_parts=top["score_parts"], rank=1), top["rule_score"], e.get("ai_rating"), e.get("ai_rationale"),
               ai.MODEL if ai_ok else None, basis, now_iso(), now_iso(), e["key"], 1 if e.get("is_primary", True) else 0, e.get("ai_status"), run_kind))
    gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for rank, o in enumerate(e["apartments"], start=1):
        c.execute("""INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window,
                     weekly_rent, extra_bed, total_weekly, allocation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (gid, o["apartment_id"], rank, o.get("rule_rank"), o["rule_score"], o.get("ai_reason"), o["lease_start"], o["lease_end"], o["rate_kind"], o["exit_window"],
                   o["weekly_rent"], o["extra_bed"], o["total_weekly"], _alloc_json(c, o["allocation"], e["units"], score_parts=o.get("score_parts"))))
    _write_members(c, gid, top["allocation"])
    _membership_changed(c, gid, "proposed")   # inside the run's save guard: version 1 of this membership (not introduced)
    return gid


def _write_members(c, gid: int, allocation: list):
    # An apartment/terms switch is not a membership change. Keep the physical
    # member rows when the enquiry set is identical, updating only offer copies.
    # setdefault preserves the old INSERT OR IGNORE first-lead-occurrence rule.
    copies = {}
    for r in allocation:
        for o in r["occupants"]:
            if str(o["person_id"]).isdigit():
                copies.setdefault(int(o["person_id"]), (o["share"], r["room"], o["bed"], ", ".join(o.get("arrangement", []))))
    existing = {int(m["enquiry_id"]) for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))}
    if existing == set(copies):
        for eid, values in copies.items():
            c.execute("UPDATE group_members SET weekly_share=?, room=?, bed=?, arrangement=? WHERE group_id=? AND enquiry_id=?",
                      (*values, gid, eid))
        return
    # New/different membership retains the original replacement behaviour.
    # Its caller remains responsible for the existing GP13 versioning hook.
    c.execute("DELETE FROM group_members WHERE group_id=?", (gid,))
    for r in allocation:
        for o in r["occupants"]:
            if str(o["person_id"]).isdigit():  # lead rows only; party members are listed in allocation_json
                c.execute("INSERT OR IGNORE INTO group_members(group_id, enquiry_id, weekly_share, room, bed, arrangement) VALUES (?,?,?,?,?,?)",
                          (gid, int(o["person_id"]), o["share"], r["room"], o["bed"], ", ".join(o.get("arrangement", []))))


# ------------------------------------------------------------------------------------------------- GP13 hooks (lead call sites)
# GP13: a change of WHO is in a group invalidates every approval of the previous membership; the people's identities are server-
# issued, never positional. Both live in the integration builder's modules (server/identities.py, server/group_responses.py;
# their DDL goes through the sole db writer). Until they land the hooks are optional: an absent module is a no-op, a present one
# is binding. No status, GP9 or messaging effect comes from these calls - they only version the membership / retire identities.
GP13_REASONS = frozenset(("proposed", "replacement", "dissolved", "barred", "rejection", "mate", "left"))   # closed; stored by the hook


def _gp13(module: str, name: str):
    """Hook binding: ABSENT module (the requested module itself is not installed) -> None, the hook is optional in that build.
    PRESENT module -> binding: an import failure inside it (a dependency) or a missing hook function surfaces to the caller,
    never a silent no-op."""
    full = f"server.{module}" if __package__ in (None, "") else f"{__package__}.{module}"
    try:
        mod = __import__(full, fromlist=[name])
    except ModuleNotFoundError as e:
        if e.name == full:
            return None
        raise
    return getattr(mod, name)


def _membership_changed(c, gid: int, reason: str, staff: str | None = None, stamp_units: list | None = None):
    """Call after every write that changes WHO is in group `gid` (or dissolves it). Inside the caller's retention guard the hook
    runs in the SAME step (a hook failure aborts the mutation with it). Outside a guard the caller must have COMMITTED its mutation
    first; the hook then runs as one guarded step of its own. If that step cannot start (database busy / connection lost / a
    pending write at entry) the versioning is deferred, category-only log: the integration builder's read side recomputes the
    membership version under its own guard before any state, lookup or response (so a deferred hook never lets an old approval
    count), but the staff panel is a step behind until then. `stamp_units`: the caller created group `gid` in that unguarded
    mutation from these rules.Units, so its people could not be identified at the write (no guard, no reconcile); the own guarded
    step here stamps the new group's stored allocation where the captured identity pairs still prove the people (see
    _stamp_stored; a membership or identity change in between -> nothing / that person unstamped). Never used for a group that
    already existed. Returns the hook's result or None."""
    assert reason in GP13_REASONS, reason
    fn = _gp13("group_responses", "on_membership_changed")
    if fn is None:
        return None
    if db.guarded(c):
        return fn(c, gid, reason, staff)
    try:
        with db.retention_guard(c, label="group membership version"):
            out = fn(c, gid, reason, staff)
            if stamp_units is not None:
                _stamp_stored(c, gid, stamp_units)   # the group THIS mutator just created outside a guard, from these units
            return out
    except (db.RetentionLockTimeout, db.ConnectionLostMidOperation, db.DirtyConnectionAtGuardEntry):
        log.warning("gp13 membership version deferred (group %s, %s)", gid, reason)   # ids and closed reason only
        return None


def _identity(name: str, c, *args):
    """identities.promote_to_lead / member_left / reconcile when the module is present (inside the caller's guard; the module
    itself refuses to run outside one). Absent module: None."""
    fn = _gp13("identities", name)
    return None if fn is None else fn(c, *args)


def _identities_for_computation(c, units: list) -> None:
    """GP13: identities are issued BEFORE a computation and captured on the units (never backfilled at the save). Issuing is a
    write (identities.reconcile brings every person of these enquiries in line with their rows: missing -> issued, edited ->
    reset): inside the caller's guard it is part of that step; on a clean connection it is one guarded step of its own; on a
    connection with somebody else's pending write it is not attempted (the pending work is left to its owner, and whoever has
    no identity yet simply stays unstamped in this computation - never guessed). Then _attach_identities reads."""
    if _gp13("identities", "reconcile") is not None and units:
        eids = sorted({int(u.id) for u in expand(units) if str(u.id).isdigit()})
        if db.guarded(c):
            for eid in eids:
                _identity("reconcile", c, eid, "left")
        elif not bool(getattr(c, "_dirty", False) if getattr(c, "backend", "sqlite") == "postgres" else getattr(c, "in_transaction", False)):
            with db.retention_guard(c, label="identity issue before computation"):
                for eid in eids:
                    _identity("reconcile", c, eid, "left")
    _attach_identities(c, units)


def _attach_identities(c, units: list) -> None:
    """GP13: at COMPUTATION, record on every Person the server-issued person_uid that is active for the very row the Person was
    built from (`Person.ident_uid`), so that the allocation computed from these units can later be attributed to those
    identities and to nobody else. Read-only (identities.active). Per person, the uid is taken only when exactly one active
    identity of the SAME enquiry has the expected role and row binding (lead: the one lead identity; member K: the identity
    bound to the K-th party_members row in id order) AND its name/email pair equals the pair captured on the Person. Anything
    else (no module, no identity, several, a pair that differs) leaves ident_uid None: that person's share will show as not
    known rather than be attributed by position or by pair at save time. Never an engine input (_engine_view drops it)."""
    active = _gp13("identities", "active")
    if active is None or not units:
        return
    norm = _gp13("identities", "norm")
    for u in expand(units):
        if not str(u.id).isdigit():
            continue
        eid = int(u.id)
        idents = active(c, eid)
        pm_ids = [m["id"] for m in rows(c.execute("SELECT id FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))]
        for p in getattr(u, "people", []) or []:
            if not isinstance(p, rules.Person):
                continue
            p.ident_uid = None
            if not p.ident_email:
                continue
            captured = (norm(p.ident_name), norm(p.ident_email))
            eid_s, _, k = str(p.id).partition("m")
            if eid_s != str(eid):
                continue
            if k == "":
                hits = [i for i in idents if i["role"] == "lead"]
            else:
                idx = int(k) - 1 if k.isdigit() else -1
                if not (0 <= idx < len(pm_ids)):
                    continue
                hits = [i for i in idents if i["role"] == "member" and i["party_member_id"] == pm_ids[idx]]
            if len(hits) == 1 and (hits[0]["name_norm"], hits[0]["email_norm"]) == captured:
                p.ident_uid = hits[0]["person_uid"]


def _stamp_people(c, rooms: list | None, units: list | None) -> list | None:
    """GP13 / GP10(d): mark every occupant of a NEWLY computed allocation with the server-issued `person_uid` of the person it was
    computed for, so a share can later be shown for that person without any positional guess. `units` are the rules.Units the
    allocation was computed from; each Person carries the identity captured at computation (_attach_identities: the uid that
    was active for its row, plus the row's name/email pair). Proof, per occupant, ALL of: the caller is inside a retention guard
    (identities are first brought in line with the rows: reconcile, same step); the occupant's engine id names a Person of
    `units` with the same placed name; that Person captured a uid; that SAME uid is still an active identity of the SAME
    enquiry with the expected role (lead for `E`, member for `EmK`); and the row it is bound to exists NOW with the captured
    pair (the enquiry's first+last name/email for the lead; the party_members row the identity points at for a member - the
    row's position is irrelevant, the identity is the link). A retired or replaced identity (an edit A->B->A, a party re-saved
    so that the identity module had to reissue), a same-name person with another email, a missing row or identity, no uid
    captured at computation, no units, no guard -> no uid on that occupant (the share then shows as not known, never another
    person's, and a pair match alone never backfills one). Any uid on the input is dropped first. Stored allocations are
    never re-stamped from position by a read; strip / renumber paths carry occupant dicts (and so the uid) forward unchanged,
    which is how the guarded lead-takeover path keeps the promoted person's own uid. Absent identities module: no uids."""
    active = _gp13("identities", "active")
    for r in rooms or []:
        for o in r.get("occupants") or []:
            o.pop("person_uid", None)
    if active is None or not rooms or units is None or not db.guarded(c):
        return rooms
    norm = _gp13("identities", "norm")
    people = {p.id: p for u in expand(units) for p in getattr(u, "people", []) or []}
    cache: dict[int, tuple[list, dict, Optional[tuple]]] = {}
    for r in rooms:
        for o in r.get("occupants") or []:
            pid = str(o.get("person_id"))
            p = people.get(pid)
            if p is None or o.get("name") != p.name or not p.ident_email or not getattr(p, "ident_uid", None):
                continue
            captured = (norm(p.ident_name), norm(p.ident_email))
            eid_s, _, k = pid.partition("m")
            if not eid_s.isdigit():
                continue
            eid = int(eid_s)
            if eid not in cache:
                _identity("reconcile", c, eid, "left")     # identities follow the rows as they are now (same guarded step)
                e = row(c.execute("SELECT first_name, last_name, email FROM enquiries WHERE id=?", (eid,)))
                pm = {m["id"]: m for m in rows(c.execute("SELECT id, name, email FROM party_members WHERE enquiry_id=?", (eid,)))}
                lead_pair = (norm(f"{e.get('first_name') or ''} {e.get('last_name') or ''}"), norm(e.get("email"))) if e else None
                cache[eid] = (active(c, eid), pm, lead_pair)
            idents, pm, lead_pair = cache[eid]
            ident = next((i for i in idents if i["person_uid"] == p.ident_uid and i["enquiry_id"] == eid), None)
            if ident is None or (ident["name_norm"], ident["email_norm"]) != captured:
                continue
            if k == "":
                ok = ident["role"] == "lead" and lead_pair == captured
            else:
                m = pm.get(ident["party_member_id"]) if ident["role"] == "member" else None
                ok = m is not None and (norm(m["name"]), norm(m["email"])) == captured
            if ok:
                o["person_uid"] = ident["person_uid"]
    return rooms


def _stamp_stored(c, gid: int, units: list) -> None:
    """Inside a guard, for a group the calling mutator has JUST created outside one (bring-a-mate, auto replacement, staff refill)
    from `units`: stamp its stored allocation rows (groups + ranked options) with the uids provable NOW by the same proof as a
    guarded write (_stamp_people with the captured pairs). Before anything: the group's members must still be exactly the
    enquiries of `units` and every stored occupant must still be a Person of `units` by id and placed name - otherwise another
    mutation got in between the creator's commit and this step and nothing is stamped (unknown, never guessed). This is the
    creating mutation's own follow-up, not a read-time re-stamp of old data: it never runs for pre-existing groups."""
    g = row(c.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))
    if not g:
        return
    expected = {int(u.id) for u in expand(units)}
    present = {m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))}
    if present != expected:
        return
    people = {p.id: p.name for u in expand(units) for p in getattr(u, "people", []) or []}
    stored = [(gid, "groups", None, pj(g["allocation_json"], {}) or {})]
    stored += [(gid, "group_apartments", ga["apartment_id"], pj(ga["allocation_json"], {}) or {})
               for ga in rows(c.execute("SELECT apartment_id, allocation_json FROM group_apartments WHERE group_id=?", (gid,)))]
    for _, _, _, alloc in stored:
        for r in alloc.get("rooms") or []:
            for o in r.get("occupants") or []:
                if people.get(str(o.get("person_id"))) != o.get("name"):
                    return
    for _, table, aid, alloc in stored:
        _stamp_people(c, alloc.get("rooms"), units)
        if table == "groups":
            c.execute("UPDATE groups SET allocation_json=? WHERE id=?", (j(alloc), gid))
        else:
            c.execute("UPDATE group_apartments SET allocation_json=? WHERE group_id=? AND apartment_id=?", (j(alloc), gid, aid))


def _alloc_json(c, rooms: list | None, units: list | None, **extra) -> str:
    """allocation_json for a NEWLY computed allocation: occupants stamped with their person_uid where provable (see
    _stamp_people; `units` = what it was computed from), then the usual {"rooms": ..., ...} document. Copies of an already
    stored allocation (apartment switch) do not pass through here."""
    return j({"rooms": _stamp_people(c, rooms, units), **extra})


def offer_apartment(c, group_id: int, apartment_id: int, staff: str | None) -> None:
    """GP6(b): atomically select a currently valid offer from the group's own list.

    This is an internal pointer selection, not permission to introduce, send or lease.
    Those actions retain their own review/approval gates. Membership and clocks stay
    unchanged; current offer terms must still reproduce from today's rules.
    """
    with db.retention_guard(c, label="manual apartment offer"):
        _offer_apartment_guarded(c, group_id, apartment_id, staff)


def _select_listed_apartment_for_lease(c, group_id: int, apartment_id: int, staff: str | None) -> None:
    """Only the guarded lease-recording caller may use the pre-repair selection contract.

    A lease is recorded through _claim_apartment, which owns availability, competing
    claims and their refusal wording. This is not permission to issue a new offer or
    retain stale terms: the separate staff offer path keeps its full current-rule check.
    """
    if not db.guarded(c):
        raise db.RetentionGuardError("lease apartment selection requires the lease guard")
    from . import apartment_rejections
    ga = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (group_id, apartment_id)))
    if not ga:
        pending = apartment_rejections.pending_validation_reason(c, group_id, apartment_id)
        if pending:
            raise apartment_rejections.OfferRefused(pending)
        raise ValueError("That apartment is not on this group's list")
    if int(apartment_id) not in {int(x["apartment_id"]) for x in eligible_offers(c, group_id)}:
        why = apartment_rejections.group_exclusion_reason(c, group_id, apartment_id)
        raise apartment_rejections.OfferRefused(why or "A member of this group declined this apartment or its building; it cannot be offered")
    _switch_current(c, group_id, apartment_id)
    audit(c, staff, "offer_apartment", "group", group_id, f"apartment {apartment_id} (rank {ga['rank']})")


def _offer_apartment_guarded(c, group_id: int, apartment_id: int, staff: str | None) -> None:
    from . import apartment_rejections
    from . import stock_lifecycle
    if not db.guarded(c):
        raise db.RetentionGuardError("manual apartment offer outside retention guard")
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (group_id,)))
    if not g or g["status"] not in apartment_rejections.LIVE_GROUP_STATUSES:
        raise apartment_rejections.OfferRefused("This group is no longer available for an apartment offer; reload the page.")
    ga = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (group_id, apartment_id)))
    if not ga:
        pending = apartment_rejections.pending_validation_reason(c, group_id, apartment_id)   # GP14 (inc3): lifted, not yet re-validated
        if pending:
            raise apartment_rejections.OfferRefused(pending)
        raise ValueError("That apartment is not on this group's list")
    if int(apartment_id) not in {int(x["apartment_id"]) for x in eligible_offers(c, group_id)}:   # GP14: the same lens as packet/readiness
        why = apartment_rejections.group_exclusion_reason(c, group_id, apartment_id)              # wording (who declined) only
        raise apartment_rejections.OfferRefused(why or "A member of this group declined this apartment or its building; it cannot be offered")
    if stock_lifecycle.group_has_pending_work(c, group_id):
        raise apartment_rejections.OfferRefused("This group is still being re-checked after a stock change; reload the page and check its apartment before offering.")
    apartment = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
    if not apartment or apartment.get("leased_at") or not apartment.get("available_from"):
        raise apartment_rejections.OfferRefused("That apartment is no longer available; reload the page and choose from the current options.")
    try:
        units = stock_lifecycle._group_units(c, group_id)
        validity, _ = stock_lifecycle._option_validity(c, apartment, ga, units)
    except stock_lifecycle.OptionsNotJudgeable:
        raise apartment_rejections.OfferRefused("This offer could not be re-checked safely; refresh the group's options before offering.") from None
    if validity != "ok":
        raise apartment_rejections.OfferRefused("This offer no longer matches the current requirements and terms; refresh the group's options before offering.")
    _switch_current(c, group_id, apartment_id)
    audit(c, staff, "offer_apartment", "group", group_id, f"apartment {apartment_id} (rank {ga['rank']})")


# ---------------------------------------------------------------------------
# T5 run-error provenance (engineering implementation of the retention rule for match_runs; no new business ruling).
# A failed full run stores ONE fixed plain sentence chosen by exception TYPE (never by reading the message) plus small closed
# metadata; str(ex) and tracebacks are persisted nowhere and logged nowhere. Legacy rows (written before this) keep their raw
# bytes until the next delete / retention purge, when neutralise_run_errors() projects every match_runs row and live RUNS entry
# onto a closed, key-aware schema. run_status() applies the SAME projection to a copy for display (masking, flagged) - that is
# presentation only and is never counted as cleanup.
# ---------------------------------------------------------------------------
RUN_ERROR_TEXT = {
    "ai": "The AI review did not complete. Check the affected groups before retrying.",
    "database": "The run stopped because the database could not be used safely. Check the affected groups before retrying.",
    "conflict": "The run stopped because another run was already in progress.",
    "timeout": "The run stopped because a step took too long. Check the affected groups before retrying.",
    "data": "The run stopped because a stored enquiry or apartment value could not be used. Check recent edits before retrying.",
    "unexpected": "The run did not complete. Check the affected groups before retrying.",
}
RUN_FAILED_LEGACY_TEXT = "An earlier run did not complete. Its detailed error message is not shown."   # accurate both at rest and as a read mask
RUN_TEXT_REMOVED = "[earlier detail not shown]"
RUN_ERROR_VOCABULARY = frozenset(RUN_ERROR_TEXT.values()) | {RUN_FAILED_LEGACY_TEXT, RUN_TEXT_REMOVED}
RUN_ERROR_KINDS = frozenset(RUN_ERROR_TEXT) | {"unknown"}
RUN_ERROR_SCHEMA = 1
# finite exception-class tokens the writer may record (anything else is stored as "unknown" - a class name is never free text here)
RUN_ERROR_TYPE_TOKENS = frozenset((
    "AIUnavailable", "AINoRetry", "AIOutputTruncated", "AISchemaUnsupported", "RetentionGuardError", "DirtyConnectionAtGuardEntry", "CommitInsideRetentionGuard",
    "ConnectionLostMidOperation", "RetentionLockTimeout", "GuardRolledBack", "RunInProgress", "TimeoutError", "ValueError", "KeyError", "TypeError",
    "ZeroDivisionError", "OperationalError", "IntegrityError", "DatabaseError", "RuntimeError", "Exception", "unknown"))
RUN_STATUS_VOCAB = frozenset(("running", "done", "failed", "interrupted"))
RUN_PHASE_VOCAB = frozenset(("start", "rules", "save", "done", "failed"))
RUN_PHASE_LABELS = frozenset(("Starting", "Comparing every enquiry with every other enquiry", "Looking for groups among people not yet proposed", "Choosing one proposed group per person (rules)", "Saving groups",
                              "Checking groups awaiting a replacement", "Finished", "Run failed"))
RUN_DATASETS = frozenset(("real", "test"))
_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
# exact key sets written by the current code (full_run / start_full_run / run_match) plus this schema's own metadata
PROGRESS_COUNTER_KEYS = frozenset(("done", "total", "groups_total", "pool_size", "apartments", "ai_calls", "ai_failed", "error_schema", "removed_keys"))
PROGRESS_KEYS = PROGRESS_COUNTER_KEYS | {"status", "phase", "phase_label", "dataset", "staff_member", "started_at", "finished_at", "summary", "error",
                                         "error_kind", "error_type", "legacy_unreadable"}
SUMMARY_COUNTER_KEYS = frozenset(("awaiting_checked", "awaiting_replacement_found", "pool_size", "apartments", "pairs_compared", "pairs_compatible",
                                  "candidates_considered", "coverage_rounds", "search_targets_truncated", "search_seat_options_untried", "search_seat_options_date_infeasible", "search_targets_stopped_at_first_valid", "search_larger_groups_exhaustive", "selection_truncated", "coverage_selection_truncated", "groups_total", "primary_groups",
                                  "enquiries_in_primary_group", "enquiries_in_any_group", "residents_in_primary_group", "residents_in_any_group",
                                  "enquiries_unmatched", "groups_unchanged", "groups_new", "groups_ready", "groups_ai_pending", "groups_ai_failed",
                                  "apartment_refresh_queued", "ai_calls", "ai_failed_batches", "ai_groups_ok", "duration_s", "use_ai",
                                  "groups_discarded_at_save", "complete", "primary_new", "removed_keys"))
SUMMARY_KEYS = SUMMARY_COUNTER_KEYS | {"dataset", "ai_error", "discarded_enquiry_ids", "legacy_unreadable"}


def run_error_kind(ex: BaseException) -> str:
    """Closed error category by exception TYPE only; the message is never read."""
    if isinstance(ex, ai.AIUnavailable):
        return "ai"
    if isinstance(ex, db.RetentionGuardError):
        return "database"
    try:
        import sqlite3
        if isinstance(ex, sqlite3.Error):
            return "database"
    except Exception:  # pragma: no cover
        pass
    try:  # PostgreSQL driver, when installed
        import psycopg  # type: ignore
        if isinstance(ex, psycopg.Error):
            return "database"
    except Exception:
        pass
    if isinstance(ex, RunInProgress):
        return "conflict"
    if isinstance(ex, TimeoutError):
        return "timeout"
    if isinstance(ex, (ValueError, KeyError, TypeError, ArithmeticError)):
        return "data"
    return "unexpected"


def run_error_type_token(ex: BaseException) -> str:
    name = type(ex).__name__
    return name if name in RUN_ERROR_TYPE_TOKENS else "unknown"


def _is_number(v) -> bool:
    if v is None or isinstance(v, bool) or isinstance(v, int):
        return True
    return isinstance(v, float) and math.isfinite(v)


def _is_timestamp(v) -> bool:
    """Recognised operational timestamp value: ISO-8601 shape AND a real calendar date/time (2026-99-99 is not kept)."""
    if not (isinstance(v, str) and _ISO_TS.match(v)):
        return False
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _is_count(v) -> bool:
    """removed_keys: a finite non-negative int only (never a float/NaN/Infinity that could break int())."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _in(v, vocab) -> bool:
    """Closed-vocabulary test that never raises: only a str can match (lists/dicts/numbers are simply 'not in')."""
    return isinstance(v, str) and v in vocab


def _project_summary(summary, counter):
    """SUMMARY_KEYS projection of one summary dict (nested in progress or the summary_json column). Returns a new dict."""
    out = {}
    removed = 0
    for k, v in summary.items():
        if k == "dataset":
            if _in(v, RUN_DATASETS) or v == RUN_TEXT_REMOVED:
                out[k] = v
            else:
                out[k] = RUN_TEXT_REMOVED; counter[0] += 1
        elif k == "ai_error":
            if v is None or _in(v, RUN_ERROR_VOCABULARY):
                out[k] = v
            else:
                out[k] = RUN_FAILED_LEGACY_TEXT; counter[0] += 1
        elif k == "discarded_enquiry_ids":
            ints = [x for x in v if isinstance(x, int) and not isinstance(x, bool)] if isinstance(v, list) else []
            if not isinstance(v, list) or len(ints) != len(v):
                counter[0] += 1
            out[k] = ints
        elif k == "legacy_unreadable":
            if v is True:
                out[k] = True
            else:
                counter[0] += 1
        elif k == "removed_keys":
            if _is_count(v):
                out[k] = v
            else:
                removed += 1
        elif k in SUMMARY_COUNTER_KEYS:
            if _is_number(v):
                out[k] = v
            else:
                removed += 1
        else:
            removed += 1
    if removed:
        counter[0] += removed
        out["removed_keys"] = int(out.get("removed_keys") or 0) + removed
    return out


def _project_progress(progress, staff_member, counter):
    """PROGRESS_KEYS projection of one progress dict (progress_json column or a live RUNS entry). Returns a new dict."""
    out = {}
    removed = 0
    for k, v in progress.items():
        if k == "status":   # None is this projection's own neutral value for an unrecognised label (kept on later passes)
            ok = v is None or _in(v, RUN_STATUS_VOCAB)
        elif k == "phase":
            ok = v is None or _in(v, RUN_PHASE_VOCAB)
        elif k == "phase_label":
            ok = v is None or _in(v, RUN_PHASE_LABELS)
        elif k == "dataset":
            ok = _in(v, RUN_DATASETS) or v == RUN_TEXT_REMOVED
        elif k == "staff_member":
            ok = v is None or v == RUN_TEXT_REMOVED or (isinstance(v, str) and v == staff_member)
        elif k in ("started_at", "finished_at"):
            ok = v is None or _is_timestamp(v)
        elif k == "error_kind":
            ok = _in(v, RUN_ERROR_KINDS)
        elif k == "error_type":
            ok = _in(v, RUN_ERROR_TYPE_TOKENS)
        elif k == "legacy_unreadable":
            ok = v is True
        elif k == "removed_keys":
            ok = _is_count(v)
        elif k == "error":
            if v is None or _in(v, RUN_ERROR_VOCABULARY):
                out[k] = v
            else:
                out[k] = RUN_FAILED_LEGACY_TEXT; counter[0] += 1
            continue
        elif k == "summary":
            if isinstance(v, dict):
                before = counter[0]
                out[k] = _project_summary(v, counter)
                if counter[0] == before:
                    out[k] = v   # unchanged: keep the original object (byte-identical)
            else:
                removed += 1
            continue
        elif k in PROGRESS_COUNTER_KEYS:
            ok = _is_number(v)
        else:
            removed += 1
            continue
        if ok:
            out[k] = v
        elif k in ("status", "phase", "phase_label", "dataset", "staff_member", "error_kind", "error_type"):
            out[k] = RUN_TEXT_REMOVED if k in ("staff_member", "dataset") else ("unknown" if k in ("error_kind", "error_type") else None)
            counter[0] += 1
        else:
            removed += 1
    if removed:
        counter[0] += removed
        out["removed_keys"] = int(out.get("removed_keys") or 0) + removed
    if "error" in out and out["error"] is not None:   # an error carries the schema marker + a kind, so the record is self-describing
        if out.get("error_schema") != RUN_ERROR_SCHEMA:
            out["error_schema"] = RUN_ERROR_SCHEMA; counter[0] += 1
        if out.get("error_kind") not in RUN_ERROR_KINDS:
            out["error_kind"] = "unknown"; counter[0] += 1
    return out


UNREADABLE_PROGRESS = {"legacy_unreadable": True, "error": RUN_FAILED_LEGACY_TEXT, "error_schema": RUN_ERROR_SCHEMA, "error_kind": "unknown"}
UNREADABLE_SUMMARY = {"legacy_unreadable": True, "ai_error": RUN_FAILED_LEGACY_TEXT}


def _decode_json_column(raw):
    """(obj, state): state 'absent' for SQL NULL / empty, 'dict' when the column holds a JSON object, else 'unreadable'."""
    if raw is None or raw == "":
        return None, "absent"
    try:
        obj = json.loads(raw)
    except Exception:
        return None, "unreadable"
    return (obj, "dict") if isinstance(obj, dict) else (None, "unreadable")


def project_run_record(ai_error, progress_raw, summary_raw, staff_member) -> tuple:
    """The one projection shared by cleanup and display: (ai_error, progress_obj | None, summary_obj | None, changes).
    NULL columns stay None (absent). Unreadable / non-object JSON becomes the fixed replacement object."""
    counter = [0]
    if ai_error is not None and not _in(ai_error, RUN_ERROR_VOCABULARY):
        ai_error = RUN_FAILED_LEGACY_TEXT; counter[0] += 1
    prog, st = _decode_json_column(progress_raw)
    if st == "unreadable":
        prog = dict(UNREADABLE_PROGRESS); counter[0] += 1
    elif st == "dict":
        prog = _project_progress(prog, staff_member, counter)
    summ, st2 = _decode_json_column(summary_raw)
    if st2 == "unreadable":
        summ = dict(UNREADABLE_SUMMARY); counter[0] += 1
    elif st2 == "dict":
        summ = _project_summary(summ, counter)
    return ai_error, prog, summ, counter[0]


def neutralise_run_errors(c) -> dict:
    """T5 legacy cleanup for match_runs + live RUNS: project every row onto the closed schema above and rewrite only the rows
    that change. Commit-free (the caller holds the retention guard and commits); idempotent (a second call changes nothing);
    counts returned. Not keyed to a person: run errors were never scoped to one enquiry, so every row is covered."""
    out = {"match_runs_rows": 0, "process_entries": 0, "unreadable_json": 0}
    staff_by_run = {}
    for r in rows(c.execute("SELECT id, staff_member, ai_error, progress_json, summary_json FROM match_runs ORDER BY id")):
        staff_by_run[r["id"]] = r.get("staff_member")
        ai_err, prog, summ, changes = project_run_record(r.get("ai_error"), r.get("progress_json"), r.get("summary_json"), r.get("staff_member"))
        if prog is not None and prog.get("legacy_unreadable") and _decode_json_column(r.get("progress_json"))[1] == "unreadable":
            out["unreadable_json"] += 1
        if summ is not None and summ.get("legacy_unreadable") and _decode_json_column(r.get("summary_json"))[1] == "unreadable":
            out["unreadable_json"] += 1
        if not changes:
            continue
        c.execute("UPDATE match_runs SET ai_error=?, progress_json=?, summary_json=? WHERE id=?",
                  (ai_err, j(prog) if prog is not None else None, j(summ) if summ is not None else None, r["id"]))
        out["match_runs_rows"] += 1
    with _RUN_LOCK:   # no SQL under the lock: the staff mapping was read above
        for rid, st in list(RUNS.items()):
            if not isinstance(st, dict):
                RUNS[rid] = dict(UNREADABLE_PROGRESS); out["process_entries"] += 1
                continue
            counter = [0]
            projected = _project_progress(st, staff_by_run.get(rid), counter)
            if counter[0]:
                RUNS[rid] = projected
                out["process_entries"] += 1
    return out


def _record_run_failure(c, run_id: int, kind: str, token: str) -> None:
    """Worker failure path. Order matters: the safe in-process state is set first (cannot fail), then the two database writes,
    each guarded so a broken/closed connection can never surface an uncaught thread traceback (which would carry the raw exception
    context). The log line names only the run id and the closed category; when the row could not be updated it says so, so the
    database is never claimed to hold the failure record."""
    failed = {"status": "failed", "phase": "failed", "phase_label": "Run failed", "error": RUN_ERROR_TEXT[kind],
              "error_schema": RUN_ERROR_SCHEMA, "error_kind": kind, "error_type": token}
    try:
        with _RUN_LOCK:
            RUNS.setdefault(run_id, {}).update(failed)
    except BaseException:   # pragma: no cover - a plain dict update under a lock
        pass
    saved = False
    try:
        c.execute("UPDATE match_runs SET finished_at=?, status='failed', ai_error=? WHERE id=?", (now_iso(), RUN_ERROR_TEXT[kind], run_id))
        c.commit()
        saved = True
    except BaseException:
        pass
    try:
        _progress(c, run_id, **failed)   # swallows its own database errors; still the hook test wrappers observe
    except BaseException:
        pass
    try:
        if saved:
            log.warning("full run %s failed: %s", run_id, kind)
        else:
            log.warning("full run %s failed: %s (the failure record could not be written to the database; the run row may still say running)", run_id, kind)
    except BaseException:   # pragma: no cover
        pass


class RunInProgress(Exception):
    def __init__(self, run_id: int, dataset: str):
        super().__init__(f"A full matching run (#{run_id}, {dataset} data) is already in progress; MateBot AI never runs two at once")
        self.run_id, self.dataset = run_id, dataset


def running_run() -> tuple[int, str] | None:
    """(run_id, dataset) of the full run in progress in this process, if any."""
    with _RUN_LOCK:
        for rid, st in RUNS.items():
            if st.get("status") == "running":
                return rid, st.get("dataset")
    return None


def start_full_run(c, staff: str | None, dataset: str, use_ai: bool = True) -> int:
    """Run `full_run` in a background thread; returns the run id for progress polling.
    T8: never two runs at once. The same data set's running run is returned as is; another data set's run raises RunInProgress."""
    with _RUN_LOCK:
        for rid, st in RUNS.items():
            if st.get("status") == "running":
                if st.get("dataset") == dataset:
                    return rid
                raise RunInProgress(rid, st.get("dataset"))
    c.execute("INSERT INTO match_runs(apartment_id, started_at, staff_member, ai_used, kind, status, dataset) VALUES (NULL,?,?,0,'full','running',?)", (now_iso(), staff, dataset))
    run_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.commit()
    with _RUN_LOCK:
        RUNS[run_id] = {"status": "running", "phase": "start", "phase_label": "Starting", "dataset": dataset, "done": 0, "total": 0, "staff_member": staff}

    def worker():
        try:
            full_run(c, staff, dataset, use_ai=use_ai, run_id=run_id)
        except Exception as ex:  # keep the failure visible to staff - as a fixed sentence + closed metadata (T5): never str(ex), never a traceback
            kind, token = run_error_kind(ex), run_error_type_token(ex)
            del ex   # the raw exception is not needed (or wanted) past this point
            _record_run_failure(c, run_id, kind, token)

    threading.Thread(target=worker, daemon=True).start()
    return run_id


def run_status(c, run_id: int) -> dict:
    r = row(c.execute("SELECT * FROM match_runs WHERE id=?", (run_id,)))
    if not r:
        raise ValueError("Run not found")
    with _RUN_LOCK:
        live = dict(RUNS.get(run_id) or {})
    # T5 display masking: the SAME projection as neutralise_run_errors, applied to a COPY (nothing is written here). A legacy row
    # keeps its raw bytes until the next delete / purge; `error_masked` says the display differs from what is stored.
    ai_err, prog_db, summ, changes = project_run_record(r.get("ai_error"), r.get("progress_json"), r.get("summary_json"), r.get("staff_member"))
    if live:
        counter = [0]
        prog = _project_progress(live, r.get("staff_member"), counter)
        changes += counter[0]
    else:
        prog = prog_db if prog_db is not None else {}
    # top-level status comes from the PROJECTED live state, else the row column when it is a known value, else the fixed fallback
    status = prog.get("status") if live else None
    if not _in(status, RUN_STATUS_VOCAB):
        status = r.get("status") if _in(r.get("status"), RUN_STATUS_VOCAB) else ("done" if r.get("finished_at") else "running")
        if r.get("status") is not None and not _in(r.get("status"), RUN_STATUS_VOCAB):
            changes += 1
    if status == "running" and not live:
        status = "interrupted"  # the server restarted mid-run; results of this run were not saved
    out = {"run_id": run_id, "status": status, "kind": r.get("kind"), "dataset": r.get("dataset"), "started_at": r["started_at"], "finished_at": r.get("finished_at"),
           "staff_member": r.get("staff_member"), "progress": prog, "summary": summ if summ is not None else {}, "ai_error": ai_err,
           "error_kind": (prog or {}).get("error_kind"), "error_schema": (prog or {}).get("error_schema")}
    if changes:
        out["error_masked"] = True
    return out


# ---------------------------------------------------------------------------
# GP8: no holding. An apartment is taken only when reservations marks it Leased; every other group is re-ranked at once.
# ---------------------------------------------------------------------------
def mark_apartment_leased(c, apartment_id: int, staff: str | None, group_id: int | None = None, claimed_at: str | None = None) -> dict:
    """GP8: the apartment is Leased (unavailable for matching); then every other live group that listed it moves to its next
    best-fit available apartment, each flagged. Returns {apartment, affected: [...]}.
    T5 (D, 8 Nov 2026): the apartment CLAIM belongs to the caller's guarded step (app._set_group_status / the apartment lease route
    do a compare-and-swap `UPDATE apartments ... WHERE leased_at IS NULL` together with the group's status change). With `claimed_at`
    this helper only VERIFIES that exactly that claim landed (leased_at == claimed_at and leased_group_id is the given group), raises
    ValueError otherwise - a claim that did not land never triggers a rerank - and then reranks the other groups, each in its own
    retention-guard step (group_apartments/allocation writes are T5 scope). Without `claimed_at` the legacy claim-here path runs
    (kept only until both callers pass claimed_at)."""
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (apartment_id,)))
    if not a:
        raise ValueError("Apartment not found")
    if claimed_at is not None:
        if str(a.get("leased_at") or "") != str(claimed_at) or (a.get("leased_group_id") if a.get("leased_group_id") is not None else None) != (int(group_id) if group_id is not None else None):
            raise ValueError("The lease claim for this apartment did not land; nothing re-ranked")
        when = str(claimed_at)
    else:
        if a.get("leased_at"):
            raise ValueError("This apartment is already marked Leased")
        when = now_iso()
        from . import stock_lifecycle
        with db.retention_guard(c, label="apartment lease claim"):
            c.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by=?, updated_at=?, lease_generation=lease_generation+1 WHERE id=?", (when, group_id, staff, when, apartment_id))
            stock_lifecycle.record_transition(c, apartment_id, "leased", staff, exclude_group_id=group_id, bump=False)
    # Everything from here runs AFTER the lease is durable. A failure in the rerank (lock timeout, lost connection, a bad row) is
    # therefore reported as "lease landed, rerank pending for these groups", never as "nothing changed" and never by undoing the
    # claim. Each group's rewrite is its own idempotent step, so the pending ones are simply retried (retry_pending_lease_reranks).
    affected, failed = _rerank_after_lease_isolated(c, apartment_id, a["address"], when, exclude_group_id=group_id)
    rerank = {"status": "complete" if not failed else "partial", "failed": failed, "pending_group_ids": [f["group_id"] for f in failed]}
    try:
        audit(c, staff, "apartment_leased", "apartment", apartment_id, f"{a['address']}; group {group_id}; {len(affected)} group(s) re-ranked"
              + (f"; RERANK PENDING for {len(failed)} group(s): {', '.join(str(f['group_id']) for f in failed)}" if failed else ""))
    except Exception as e:   # the audit row is not the truth of the lease; the state below is
        log.warning("apartment %s lease audit not written: %s", apartment_id, type(e).__name__)
        rerank["audit_written"] = False
    return {"apartment_id": apartment_id, "address": a["address"], "leased_at": when, "leased_group_id": group_id, "affected": affected, "rerank": rerank}


def _rerank_after_lease_isolated(c, apartment_id: int, address: str, when: str, exclude_group_id: int | None = None) -> tuple[list[dict], list[dict]]:
    """rerank_after_lease with every group isolated: a group whose step fails is recorded (static category + group id, no prose)
    and the others still proceed. Returns (affected, failed)."""
    failed = []
    affected = rerank_after_lease(c, apartment_id, address, when, exclude_group_id=exclude_group_id, failures=failed)
    return affected, failed


def pending_lease_reranks(c) -> list[dict]:
    """Durable truth for the post-claim work: pending stock-lifecycle work rows of leased apartments (one row per group), plus any
    leased apartment whose follow-up was never captured (claimed outside the lifecycle) and that a live group still lists or sits
    on. Rows: {apartment_id, address, leased_at, leased_group_id, group_id}. Empty when every lease's follow-up completed."""
    from . import stock_lifecycle
    out = []
    for w in stock_lifecycle.pending_work(c):
        if w["cause"] != "leased":
            continue
        a = row(c.execute("SELECT leased_at, leased_group_id FROM apartments WHERE id=?", (w["apartment_id"],)))
        out.append({"apartment_id": w["apartment_id"], "address": w["address"], "leased_at": (a or {}).get("leased_at"),
                    "leased_group_id": (a or {}).get("leased_group_id"), "group_id": w["group_id"]})
    for r in rows(c.execute("""SELECT DISTINCT a.id AS apartment_id, a.address, a.leased_at, a.leased_group_id, g.id AS group_id
                               FROM apartments a JOIN groups g ON (g.id IN (SELECT group_id FROM group_apartments WHERE apartment_id=a.id)
                                                                OR (g.apartment_id=a.id AND (g.reranked_at IS NULL OR g.reranked_at < a.leased_at)))
                               WHERE a.leased_at IS NOT NULL AND g.status NOT IN ('Leased','Dissolved') AND (a.leased_group_id IS NULL OR g.id<>a.leased_group_id)
                                 AND NOT EXISTS (SELECT 1 FROM stock_transitions t WHERE t.apartment_id=a.id AND t.cause='leased' AND t.generation=a.lease_generation)
                               ORDER BY a.id, g.id""")):
        out.append(r)
    out.sort(key=lambda x: (int(x["apartment_id"]), int(x["group_id"])))
    return out


def retry_pending_lease_reranks(c, staff: str | None = None) -> dict:
    """Finish any stock-lifecycle follow-up left pending (lease, withdrawal, change, delete), each group in its own idempotent step.
    A lease claimed outside the lifecycle gets its follow-up captured first. Returns {retried: n, affected: [...], failed: [...]}."""
    from . import stock_lifecycle
    pending = pending_lease_reranks(c)
    for p in pending:
        if not row(c.execute("SELECT 1 FROM stock_transitions WHERE apartment_id=? AND cause='leased' AND generation=(SELECT lease_generation FROM apartments WHERE id=?)",
                             (p["apartment_id"], p["apartment_id"]))):
            _capture_lease_followup(c, p["apartment_id"], p.get("leased_group_id"), staff)
    work = stock_lifecycle.pending_work(c)
    affected, failed = [], []
    for apt_id in dict.fromkeys(w["apartment_id"] for w in work):
        got = stock_lifecycle.run_pending(c, apartment_id=apt_id, staff=staff, failures=failed)
        affected += got
        if got or [f for f in failed]:
            a = next(w for w in work if w["apartment_id"] == apt_id)
            audit(c, staff, "apartment_lease_rerank_retry", "apartment", apt_id, f"{a['address']}; {len(got)} group(s) re-ranked; {len([f for f in failed])} still pending")
    return {"retried": len(work), "affected": affected, "failed": failed}


def _capture_lease_followup(c, apartment_id: int, leased_group_id: int | None, staff: str | None) -> dict | None:
    """A lease that is already durable but whose follow-up work was never recorded (claimed by an older path or directly): record
    the transition and the affected groups now, in one guard step, BEFORE anything is removed. No generation bump (the claim did)."""
    from . import stock_lifecycle
    with db.retention_guard(c, label="lease follow-up capture"):
        a = row(c.execute("SELECT id, leased_at, lease_generation FROM apartments WHERE id=?", (apartment_id,)))
        if not a or not a.get("leased_at"):
            return None
        if row(c.execute("SELECT 1 FROM stock_transitions WHERE apartment_id=? AND cause='leased' AND generation=?", (apartment_id, a["lease_generation"]))):
            return None
        return stock_lifecycle.record_transition(c, apartment_id, "leased", staff, exclude_group_id=leased_group_id, bump=False)


def rerank_after_lease(c, apartment_id: int, address: str, when: str, exclude_group_id: int | None = None, failures: list | None = None) -> list[dict]:
    """GP8: complete the pending follow-up of the apartment's latest lease transition - remove it from every other live group's
    ranked list, renumber, move each group to its next currently valid option or hold it (NULL pointer), flag it, raise the H19
    alert and mark the GP8(b) refresh - one guard step per group (stock_lifecycle.reconcile_work). With `failures` given, a group
    whose step fails is appended there as {group_id, category} and the others proceed; without it the exception propagates.
    Idempotent: nothing pending -> nothing done."""
    from . import stock_lifecycle
    if not row(c.execute("SELECT 1 FROM stock_transitions WHERE apartment_id=? AND cause='leased' AND generation=(SELECT lease_generation FROM apartments WHERE id=?)",
                         (apartment_id, apartment_id))):
        _capture_lease_followup(c, apartment_id, exclude_group_id, None)
    return stock_lifecycle.run_pending(c, apartment_id=apartment_id, failures=failures)


EV_APARTMENT_UNAVAILABLE = "group_apartment_unavailable"   # alerts.EVENTS, record type group (stock_lifecycle.EV_APARTMENT_UNAVAILABLE)


def _group_is_test(c, gid: int) -> bool:
    r = row(c.execute("SELECT MAX(e.is_test) AS t FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=?", (gid,)))
    return bool(r and r["t"])


def _switch_current(c, group_id: int, apartment_id: int) -> None:
    """Point the group at one of its listed apartments (the offer copies on the group row follow). The ONE place a pointer moves:
    for a group already Introduced (or confirmed) a changed pointer stamps `offer_changed_since_intro_at` (display-only; the public
    page withholds apartment details until staff acknowledge the current introduction and offer). Membership version untouched."""
    ga = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (group_id, apartment_id)))
    g0 = row(c.execute("SELECT apartment_id, status, offer_changed_since_intro_at FROM groups WHERE id=?", (group_id,)))
    alloc = pj(ga["allocation_json"], {})
    when = now_iso()
    stamp = g0.get("offer_changed_since_intro_at") if g0 else None
    if g0 and g0["status"] in ("Introduced", "Group confirmed") and not stamp and (g0.get("apartment_id") is None or int(g0["apartment_id"]) != int(apartment_id)):
        stamp = when
    c.execute("""UPDATE groups SET apartment_id=?, lease_start=?, lease_end=?, rate_kind=?, exit_window=?, weekly_rent=?, extra_bed=?, total_weekly=?, allocation_json=?,
                 offer_changed_since_intro_at=?, updated_at=? WHERE id=?""",
              (apartment_id, ga["lease_start"], ga["lease_end"], ga["rate_kind"], ga["exit_window"], ga["weekly_rent"], ga["extra_bed"], ga["total_weekly"],
               j({"rooms": alloc.get("rooms"), "score_parts": alloc.get("score_parts"), "rank": ga["rank"]}), stamp, when, group_id))
    _write_members(c, group_id, alloc.get("rooms") or [])
    c.execute("UPDATE groups SET exception_ref=?, exception_total=? WHERE id=?",
              (ga.get("exception_ref"), ga.get("exception_total"), group_id))


def _store_exception_fields(c, gid: int, aid: int, option: dict) -> None:
    c.execute("UPDATE group_apartments SET exception_ref=?,exception_total=? WHERE group_id=? AND apartment_id=?",
              (option.get("exception_ref"), option.get("exception_total"), gid, aid))


def exception_offers_for(c, gid: int, apartment_id: int) -> tuple:
    """Only an existing exact group's approval may cross the ordinary-rule boundary."""
    from . import exit_exceptions
    return exit_exceptions.authorised_offers(c, group_id=gid, apartment_id=apartment_id)


def relist_apartment(c, apartment_id: int, available_from: str | None, staff: str | None) -> None:
    """GP8 / A3(c): the apartment comes back on the market with a new availability date; it is matched from the next run."""
    from . import stock_lifecycle
    with db.retention_guard(c, label="apartment relist"):
        c.execute("UPDATE apartments SET leased_at=NULL, leased_group_id=NULL, leased_by=NULL, available_from=COALESCE(?, available_from), updated_at=? WHERE id=?",
                  (available_from, now_iso(), apartment_id))
        # recorded as the apartment's newest transition (no generation bump: lease_generation counts leases, as before), so any
        # follow-up still pending from the previous lease is superseded by it - the apartment is available again, nothing stale is
        # removed - and the next lease or withdrawal is a distinct event (H19 dedupe by transition id)
        stock_lifecycle.record_transition(c, apartment_id, "relisted", staff, bump=False)
        audit(c, staff, "apartment_relisted", "apartment", apartment_id, available_from)


# ---------------------------------------------------------------------------
# GP7(b) + GP3: staff reject one or more suggested members; the AI replaces them. Staff never pick or rearrange members.
# ---------------------------------------------------------------------------
def _refill_options(c, apt: rules.Apartment, a: dict, kept: list[rules.Unit], excluded: set[int], dataset: str, limit: int = 5,
                    prefer_all: bool = False, pool_override: list | None = None) -> list[dict]:
    """Rule-passing ways to refill a group around the kept members at this apartment: one unit, two units, or a greedy fill."""
    all_units = load_pool(c, dataset)
    pool = [u for u in all_units if int(u.id) not in excluded and _unit_fits_apartment(apt, u) and all(_pair_ok(u, k) for k in kept)]
    # GP7(a): prefer people who are not yet in a proposed group, so refilling one group does not break another
    free = [u for u in pool if u.extra.get("status") == "New"]
    if free and not prefer_all:
        got = _refill_options(c, apt, a, kept, excluded, dataset, limit, prefer_all=True, pool_override=free)
        if got:
            return got
    if pool_override is not None:
        pool = pool_override
    _, cap = rules.apartment_capacity(apt.bedrooms)
    cap = min(cap, rules.APT_TYPES[rules.normalise_type(apt.type_code)]["a4b_max"], rules.MAX_GROUP)
    kept_people = sum(u.size for u in kept)
    options, seen = [], set()

    def consider(grp, added):
        key = frozenset(u.id for u in grp)
        if key in seen or sum(u.size for u in grp) > cap or sum(u.size for u in grp) < rules.MIN_GROUP:
            return False
        seen.add(key)
        grp = expand(grp)  # GP11: linked parties join as a whole
        added = [int(x.id) for x in grp if all(int(x.id) != int(k.id) for k in kept)]
        chk = rules.check_group(apt, grp)
        if chk.ok:
            score, parts = preference_score(apt, grp, chk)
            options.append({"units": grp, "check": chk, "rule_score": score, "score_parts": parts, "added": added})
            return True
        return False

    for u in pool:
        if kept_people + u.size > cap:
            continue
        if consider(kept + [u], [int(u.id)]):
            continue
        # greedy fill: keep adding the most compatible unit until the group passes or the apartment is full
        grp, rest = kept + [u], [v for v in pool if v.id != u.id]
        while sum(x.size for x in grp) < cap and rest:
            best, best_s = None, -1e9
            for v in rest:
                if sum(x.size for x in grp) + v.size > cap or not all(_pair_ok(v, g) for g in grp):
                    continue
                sc = _affinity(v, grp)
                if sc > best_s:
                    best, best_s = v, sc
            if best is None:
                break
            grp = grp + [best]
            rest = [v for v in rest if v.id != best.id]
            if consider(grp, [int(x.id) for x in grp if x not in kept]):
                break
    for i, u in enumerate(pool):  # explicit pairs as well (covers two rejected singles replaced by two singles)
        for v in pool[i + 1:]:
            if kept_people + u.size + v.size <= cap and _pair_ok(u, v):
                consider(kept + [u, v], [int(u.id), int(v.id)])
    options.sort(key=lambda o: -o["rule_score"])
    return options[:limit]


def reject_members(c, group_id: int, reject_ids: list[int], staff: str | None, use_ai: bool = True,
                   reason_kind: str = "housemates", friends_choice: str | None = None) -> dict:
    """GP7(b): staff reject one or more suggested members. The rejection is recorded (never re-proposed with the members who
    stayed), the AI ranks the rule-passing refills and the best one becomes the new proposed group; the old group is dissolved.
    If nothing passes, the group is NOT dissolved (GP10): the remaining members stay together as "Awaiting replacement" with an
    empty bed (GP5 exception), flagged for staff, and later runs keep looking. Other refills are returned view-only.

    GP11(k) (Bill, 28 Oct 2026), when a rejected person came through a "Bring a mate" link or sent one:
      reason_kind 'housemates' ("Housemates don't want this person"): the whole friend party leaves together and is grouped again
        together on the next run; the rejection is recorded for each of them against the members who stayed.
      reason_kind 'alm' ("ALM rejects this person"): staff have spoken with the remaining friends, who chose
        friends_choice 'leave' (they leave with their friend and are grouped again together; only the rejected person is recorded
        as rejected) or 'stay' (they stay without the friend, who leaves the party and the group alone)."""
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (group_id,)))
    if not g:
        raise ValueError("Group not found")
    if g["status"] in ("Leased", "Dissolved"):
        raise ValueError("This group is " + g["status"].lower())
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (group_id,)))]
    reject = [i for i in reject_ids if i in member_ids]
    if not reject:
        raise ValueError("Tick at least one member of this group")
    if reason_kind not in ("housemates", "alm"):
        raise ValueError("Say who rejected this person: the housemates or ALM")
    friends = [i for i in _with_linked(c, reject, member_ids) if i not in reject]  # GP11(a): linked friends normally leave together
    why = None
    names = lambda ids: [r["first_name"] for r in rows(c.execute(f"SELECT first_name FROM enquiries WHERE id IN ({','.join('?' * len(ids))})", ids))] if ids else []
    if friends and reason_kind == "alm":
        if friends_choice == "stay":  # GP11(k): the friends stay without the rejected person, who leaves the friend party
            for eid in reject:
                unlink_from_party(c, eid, f"ALM rejected them from Group {group_id} on {now_iso()[:10]}; their friends ({', '.join(names(friends))}) chose to stay in the group, so they are no longer linked and are matched on their own.")
            remove, record_for = list(reject), list(reject)
            why = f"ALM rejected {', '.join(names(reject))}; their friends ({', '.join(names(friends))}) chose to stay in the group"
        elif friends_choice == "leave":  # GP11(k): the friends leave with the rejected person and are grouped again together
            remove, record_for = list(reject) + friends, list(reject)
            why = f"ALM rejected {', '.join(names(reject))}; their friends ({', '.join(names(friends))}) chose to leave with them and are matched together again"
        else:
            raise ValueError("Speak with the friends first, then say whether they leave with this person or stay without them")
    else:
        remove, record_for = list(reject) + friends, list(reject) + friends
        if friends:
            why = f"the housemates did not want {', '.join(names(reject))}, so the whole friend party ({', '.join(names(remove))}) left together to be grouped again together"
    if len(remove) >= len(member_ids):
        raise ValueError("At least one member must stay; dissolve the group instead")
    rep = _lose_members(c, g, remove, staff, cause="rejection", auto_apply=True, use_ai=use_ai, record_for=record_for, why=why)
    rep["reason_kind"], rep["friends_choice"], rep["friends"] = reason_kind, (friends_choice if friends and reason_kind == "alm" else None), friends
    audit(c, staff, "reject_members", "group", group_id, f"rejected {reject} ({reason_kind}{', friends ' + friends_choice if friends and reason_kind == 'alm' else ''}); removed {remove}; {len(rep['options'])} refill(s) ({rep['ranking_basis']}); new group {rep['new_group_id']}" + ("" if rep["new_group_id"] else "; group kept, awaiting replacement"))
    return rep


def unlink_from_party(c, eid: int, note: str | None = None) -> None:
    """GP11(k)/(l): one person leaves their "Bring a mate" party (the others stay linked to each other). If they were the enquirer
    whose link the others used, the first friend becomes the party's root and the share link follows the party."""
    e = row(c.execute("SELECT id, linked_to_enquiry_id FROM enquiries WHERE id=?", (eid,)))
    if not e:
        return
    if e["linked_to_enquiry_id"]:
        c.execute("UPDATE enquiries SET linked_to_enquiry_id=NULL WHERE id=?", (eid,))
    else:
        friends = [r["id"] for r in rows(c.execute("SELECT id FROM enquiries WHERE linked_to_enquiry_id=? ORDER BY id", (eid,)))]
        if friends:
            new_root = friends[0]
            c.execute("UPDATE enquiries SET linked_to_enquiry_id=NULL WHERE id=?", (new_root,))
            c.execute("UPDATE enquiries SET linked_to_enquiry_id=? WHERE linked_to_enquiry_id=?", (new_root, eid))
            c.execute("UPDATE mate_links SET enquiry_id=? WHERE enquiry_id=?", (new_root, eid))
            c.execute("UPDATE mate_requests SET root_enquiry_id=? WHERE root_enquiry_id=? AND status='pending'", (new_root, eid))
    if note:
        c.execute("UPDATE enquiries SET mate_note=TRIM(COALESCE(mate_note, '') || ' ' || ?) WHERE id=?", (note, eid))
    maybe_commit(c)   # reached from the guarded delete route (atomic unlink + scrub + delete) as well as standalone


def _lose_members(c, g: dict, remove: list[int], staff: str | None, cause: str, auto_apply: bool, use_ai: bool = True,
                  record_for: list[int] | None = None, why: str | None = None) -> dict:
    """A group loses one or more members: `cause` 'rejection' (GP3/GP7/GP9(a), recorded as a permanent pair block) or 'bar'
    (GP9(d): the person was barred by staff; nothing is recorded against the members who stayed). MMAI looks for an AI-ranked
    replacement exactly the same way in both cases. `auto_apply`: the best replacement becomes the new Proposed group at once
    (rejections; bars on Proposed groups). Otherwise (bars on Introduced or later groups) the group is kept together awaiting the
    replacement and the find is flagged for staff to accept or decline, as in GP10. With no replacement, GP10 applies either way."""
    group_id = g["id"]
    when = now_iso()
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (group_id,)))]
    keep_ids = [m for m in member_ids if m not in set(remove)]
    if cause == "rejection":
        for r_id in (remove if record_for is None else record_for):  # GP11(k): friends leaving in sympathy are not themselves rejected
            for k_id in keep_ids:
                c.execute("INSERT OR IGNORE INTO member_rejections(group_id, rejected_enquiry_id, other_enquiry_id, at, staff_member) VALUES (?,?,?,?,?)", (group_id, r_id, k_id, when, staff))
        c.commit()
        load_rejections(c)
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    apt = apartment_from_row(a)
    first = row(c.execute("SELECT is_test FROM enquiries WHERE id=?", (member_ids[0],)))
    dataset = "test" if first and first["is_test"] else "real"
    kept = []
    for eid in keep_ids:
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        if e:
            kept.append(unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))))
    _identities_for_computation(c, kept)
    excluded = set(member_ids) | set(_lst_int(g.get("rejected_json")))
    out, options, order, basis, ai_error = _replacement_options(c, a, apt, kept, excluded, dataset, use_ai) if kept else ([], [], [], "Rules only", None)
    names = [r["first_name"] for r in rows(c.execute(f"SELECT first_name FROM enquiries WHERE id IN ({','.join('?' * len(remove))})", remove))]
    if why:
        pass  # the caller explained it (GP11(k) rejections)
    elif cause == "rejection":
        why = f"staff rejected {', '.join(names)}"
    elif cause == "mate":  # GP11(g): the enquirer (and anyone already linked) left with the friend they brought; nobody is marked rejected
        why = f"{', '.join(names)} left to be matched together with a friend they brought along"
    else:
        why = f"{', '.join(names)} {'was' if len(names) == 1 else 'were'} barred from matching by staff and removed from the group"
    rejected_all = sorted(set(_lst_int(g.get("rejected_json"))) | (set(remove) if cause == "rejection" else set()))
    new_id, awaiting, new_units = None, False, None
    if out and auto_apply:
        # the AI's top-rated refill becomes the new Proposed group (queued for its T6 assessment); the old group is dissolved
        c.execute("UPDATE groups SET status='Dissolved', updated_at=? WHERE id=?", (when, group_id))
        for eid in member_ids:
            c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', 'Introduced', 'Group confirmed', ?)", (eid, AWAITING))
        new_units = options[order[0]]["units"]
        new_id = _create_group_from_option(c, g, out[0], basis, replaced_from=group_id, rejected=rejected_all, units=new_units)
        note = f"Dissolved on {when[:10]}: {why}. The {'AI' if basis == 'AI' else 'rules'} refilled the group as Group {new_id}."
        c.execute("UPDATE groups SET rerank_note=?, reranked_at=?, rejected_json=?, flag_kind=? WHERE id=?", (note, when, j(rejected_all), cause, group_id))
        message = f"Group {group_id} replaced by group {new_id}." + ("" if cause == "rejection" else f" {', '.join(names)} removed; the AI chose the replacement.")
    else:
        # GP10 (no replacement) or GP9(d) on an Introduced / later group (replacement found, staff accept it): the group is NOT
        # dissolved. The remaining members stay together with status "Awaiting replacement" and an empty bed (GP5 exception); their
        # shares stay as they were and the uncovered share is shown (GP10(d)). Later runs keep looking.
        awaiting = True
        missing = sum(1 + c.execute("SELECT COUNT(*) FROM party_members WHERE enquiry_id=?", (eid,)).fetchone()[0] for eid in remove)
        _strip_from_allocations(c, group_id, remove)
        for eid in remove:
            c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', 'Introduced', 'Group confirmed', ?)", (eid, AWAITING))
        for eid in keep_ids:
            c.execute("UPDATE enquiries SET status=? WHERE id=?", (AWAITING, eid))
        if out:
            top = [{"added_enquiry_ids": o["added_enquiry_ids"], "added_names": o["added_names"], "rule_score": o["rule_score"], "ai_rating": o.get("ai_rating")} for o in out[:3]]
            note = (f"Awaiting replacement since {when[:10]}: {why}. The group ({g['status']}) was kept together with {missing} empty {'bed' if missing == 1 else 'beds'} "
                    f"(an empty bed is allowed while a replacement is awaited). The AI found and ranked a replacement; accept or decline it below (nothing is applied to an introduced group without you).")
            c.execute("UPDATE groups SET replacement_json=?, replacement_found_at=?, replacement_checked_at=? WHERE id=?", (j(top), when, when, group_id))
            message = (f"{', '.join(names)} removed from Group {group_id}. The group is kept together and awaits the replacement the AI found "
                       f"({out[0]['added_names'] and ' + '.join(out[0]['added_names'])}): accept or decline it on the group page.")
        else:
            note = (f"Awaiting replacement since {when[:10]}: {why} and no replacement passed every rule. "
                    f"The group was kept together with {missing} empty {'bed' if missing == 1 else 'beds'} (an empty bed is allowed while a replacement is awaited). "
                    "Each matching run keeps looking; a replacement is only applied when staff accept it.")
            c.execute("UPDATE groups SET replacement_json=NULL, replacement_found_at=NULL, replacement_checked_at=? WHERE id=?", (when, group_id))
            message = ("No replacement passes every rule right now. The group was kept together and is awaiting a replacement: "
                       "see the options on the group page. MateBot AI keeps looking on every run and will flag a replacement for you to accept.")
        c.execute("""UPDATE groups SET status=?, awaiting_note=?, awaiting_since=COALESCE(awaiting_since, ?), awaiting_people_missing=COALESCE(awaiting_people_missing,0)+?,
                     awaiting_reason=?, rejected_json=?, flag_kind=?, updated_at=? WHERE id=?""",
                  (AWAITING, note, when, missing, cause, j(rejected_all), cause, when, group_id))
    c.commit()
    # GP13: the people changed - the old group's version closes (dissolved) or is superseded (kept together with an empty bed);
    # a refilled group starts its own version. Nobody's earlier approval carries over.
    _membership_changed(c, group_id, "dissolved" if new_id else ("barred" if cause == "bar" else "rejection" if cause == "rejection" else "mate"), staff)
    if new_id:
        _membership_changed(c, new_id, "replacement", staff, stamp_units=new_units)
    return {"group_id": group_id, "new_group_id": new_id, "kept": keep_ids, "rejected": remove if cause == "rejection" else [], "removed": remove, "options": out, "ranking_basis": basis,
            "ai_error": ai_error, "awaiting_replacement": awaiting, "replacement_flagged": bool(awaiting and out), "message": message}


def _lst_int(v) -> list[int]:
    try:
        return [int(x) for x in (pj(v) or [])]
    except Exception:
        return []


def _replacement_options(c, a: dict, apt, kept: list, excluded: set, dataset: str, use_ai: bool):
    """Rule-passing refills around `kept` at apartment `a`, AI-ranked when available. Returns (payloads, options, order, basis, ai_error)."""
    options = _refill_options(c, apt, a, kept, excluded, dataset) if not a.get("leased_at") else []
    payloads = [_group_payload(a, o) for o in options]
    basis, ai_error = "Rules only (AI unavailable)", None
    if use_ai and payloads:
        try:
            for r in _strict_reviews(ai.assess_groups(a, payloads), payloads):
                if 0 <= r["index"] < len(payloads):
                    _apply_candidate_review(payloads[r["index"]], r)
            basis = "AI"
        except ai.AIUnavailable as e:
            ai_error = ai.failure_text(e)   # Modified A: closed sentence, never the exception's words
    elif payloads:
        basis = "Rules only"
    order = sorted(range(len(payloads)), key=lambda i: (-(payloads[i].get("ai_rating") or 0), -payloads[i]["rule_score"]))
    out = []
    for i in order:
        p = payloads[i]
        p["added_enquiry_ids"] = options[i]["added"]
        p["added_names"] = [u.people[0].name for u in options[i]["units"] if int(u.id) in options[i]["added"]]
        p.pop("members_for_ai", None)
        out.append(p)
    return out, options, order, basis, ai_error


def _strip_from_allocations(c, group_id: int, remove_ids: list[int]):
    """GP10 / GP5 exception: take the rejected enquiries (lead and party members) out of the group's stored allocations - the
    current one and every ranked apartment - leaving their beds empty, and rewrite the member list. Shares of the remaining
    people are left as they were (guide only, A12); the uncovered part of the rent is shown to staff."""
    rm = {str(i) for i in remove_ids}

    def strip(alloc):
        if not alloc:
            return alloc
        for r in alloc.get("rooms", []):
            kept = [o for o in r.get("occupants", []) if not (str(o.get("person_id")).split("m")[0] in rm)]
            r["empty_beds"] = r.get("empty_beds", 0) + (len(r.get("occupants", [])) - len(kept))
            r["occupants"] = kept
        alloc["gp5_exception"] = "Awaiting replacement: an empty bed is allowed until a replacement is accepted"
        return alloc

    g = row(c.execute("SELECT allocation_json FROM groups WHERE id=?", (group_id,)))
    alloc = strip(pj(g["allocation_json"]))
    c.execute("UPDATE groups SET allocation_json=? WHERE id=?", (j(alloc), group_id))
    for ga in rows(c.execute("SELECT apartment_id, allocation_json FROM group_apartments WHERE group_id=?", (group_id,))):
        c.execute("UPDATE group_apartments SET allocation_json=? WHERE group_id=? AND apartment_id=?", (j(strip(pj(ga["allocation_json"]))), group_id, ga["apartment_id"]))
    for eid in remove_ids:
        c.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (group_id, eid))
    eids = sorted({int(str(o["person_id"]).split("m")[0]) for r in (alloc or {}).get("rooms", []) for o in r["occupants"] if str(o["person_id"]).split("m")[0].isdigit()})
    c.execute("UPDATE groups SET member_key=? WHERE id=?", (",".join(map(str, eids)), group_id))


def _awaiting_context(c, g: dict):
    """Kept units, excluded ids (kept + everyone rejected from this group) and dataset for a group awaiting a replacement."""
    members = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],)))]
    kept = []
    for eid in members:
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
        if e:
            kept.append(unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))))
    _identities_for_computation(c, kept)
    first = row(c.execute("SELECT is_test FROM enquiries WHERE id=?", (members[0],))) if members else None
    dataset = "test" if first and first["is_test"] else "real"
    excluded = set(members) | set(_lst_int(g.get("rejected_json")))
    return kept, excluded, dataset, members


def scan_awaiting(c, dataset: str, use_ai: bool = False) -> dict:
    """GP10: on every full run, look again for a rule-passing replacement for each group awaiting one in this data set.
    A find is stored on the group and flagged for staff; it is never applied automatically. Rules-only ranking here; the AI
    rates the candidates when staff open the group / accept (T3 keeps AI calls to where they matter)."""
    is_test = 1 if dataset == "test" else 0
    found, checked = 0, 0
    for g in rows(c.execute("""SELECT g.* FROM groups g WHERE g.status=? AND EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id
                                WHERE gm.group_id=g.id AND e.is_test=?)""", (AWAITING, is_test))):
        checked += 1
        a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
        kept, excluded, _, _ = _awaiting_context(c, g)
        when = now_iso()
        if not a or a.get("leased_at") or not kept:
            c.execute("UPDATE groups SET replacement_checked_at=? WHERE id=?", (when, g["id"]))
            continue
        out, *_ = _replacement_options(c, a, apartment_from_row(a), kept, excluded, dataset, use_ai)
        if out:
            found += 1
            top = [{"added_enquiry_ids": o["added_enquiry_ids"], "added_names": o["added_names"], "rule_score": o["rule_score"], "ai_rating": o.get("ai_rating")} for o in out[:3]]
            c.execute("UPDATE groups SET replacement_json=?, replacement_found_at=COALESCE(replacement_found_at, ?), replacement_checked_at=?, updated_at=? WHERE id=?",
                      (j(top), when, when, when, g["id"]))
        else:
            c.execute("UPDATE groups SET replacement_json=NULL, replacement_found_at=NULL, replacement_checked_at=? WHERE id=?", (when, g["id"]))
    c.commit()
    return {"checked": checked, "found": found}


def fill_replacement(c, group_id: int, staff: str, use_ai: bool = True, added_enquiry_ids: list[int] | None = None) -> dict:
    """GP10: staff accept a replacement for a group awaiting one. The refill is recomputed now (the pool may have moved), the
    AI ranks it, and a new Proposed group replaces the awaiting one. If `added_enquiry_ids` is given, that option is used."""
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (group_id,)))
    if not g or g["status"] != AWAITING:
        raise ValueError("Only a group with status 'Awaiting replacement' can take a replacement.")
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    if not a or a.get("leased_at"):
        raise ValueError("This apartment is no longer available; the group must move to its next best apartment first.")
    kept, excluded, dataset, members = _awaiting_context(c, g)
    out, options, order, basis, ai_error = _replacement_options(c, a, apartment_from_row(a), kept, excluded, dataset, use_ai)
    if not out:
        c.execute("UPDATE groups SET replacement_json=NULL, replacement_found_at=NULL, replacement_checked_at=? WHERE id=?", (now_iso(), group_id)); c.commit()
        raise ValueError("No replacement passes every rule right now. The group stays as it is; MateBot AI keeps looking.")
    pick = 0
    if added_enquiry_ids:
        want = sorted(int(x) for x in added_enquiry_ids)
        pick = next((i for i, o in enumerate(out) if sorted(o["added_enquiry_ids"]) == want), None)
        if pick is None:
            raise ValueError("That replacement no longer passes every rule; choose another or let MateBot AI keep looking.")
    when = now_iso()
    for eid in members:
        c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status=?", (eid, AWAITING))
    c.execute("UPDATE groups SET status='Dissolved', updated_at=? WHERE id=?", (when, group_id))
    new_units = options[order[pick]]["units"]
    new_id = _create_group_from_option(c, g, out[pick], basis, replaced_from=group_id, rejected=_lst_int(g.get("rejected_json")), units=new_units)
    note = f"Replaced on {when[:10]}: staff accepted {', '.join(out[pick]['added_names'])} as the replacement. The group continues as Group {new_id}."
    c.execute("UPDATE groups SET rerank_note=?, reranked_at=?, replacement_json=NULL, flag_kind='replacement' WHERE id=?", (note, when, group_id))
    c.execute("UPDATE groups SET awaiting_note=?, discount_note=? WHERE id=?",
              (f"Filled from Group {group_id} (awaiting replacement since {(g.get('awaiting_since') or when)[:10]}).", g.get("discount_note"), new_id))
    c.commit()
    _membership_changed(c, group_id, "dissolved", staff)   # GP13: old version closed, the new group starts unapproved
    _membership_changed(c, new_id, "replacement", staff, stamp_units=new_units)
    audit(c, staff, "fill_replacement", "group", group_id, f"accepted replacement {out[pick]['added_enquiry_ids']} ({basis}); new group {new_id}")
    return {"group_id": group_id, "new_group_id": new_id, "added": out[pick]["added_enquiry_ids"], "ranking_basis": basis, "ai_error": ai_error,
            "message": f"Replacement accepted. Group {group_id} continues as Group {new_id} (Proposed)."}


def decline_replacement(c, group_id: int, staff: str, added_enquiry_ids: list[int], reason: str | None = None) -> dict:
    """GP10 / GP9(a): staff decline a found replacement. The declined people are recorded as rejected with the kept members,
    so they are never offered to this group again; the group stays awaiting and MMAI keeps looking."""
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (group_id,)))
    if not g or g["status"] != AWAITING:
        raise ValueError("Only a group with status 'Awaiting replacement' has a replacement to decline.")
    kept, excluded, dataset, members = _awaiting_context(c, g)
    when = now_iso()
    for cand in added_enquiry_ids:
        for k in members:
            c.execute("INSERT OR IGNORE INTO member_rejections(group_id, rejected_enquiry_id, other_enquiry_id, at, staff_member) VALUES (?,?,?,?,?)", (group_id, int(cand), k, when, staff))
    c.execute("UPDATE groups SET replacement_json=NULL, replacement_found_at=NULL, rejected_json=?, updated_at=? WHERE id=?",
              (j(sorted(set(_lst_int(g.get("rejected_json"))) | {int(x) for x in added_enquiry_ids})), when, group_id))
    c.commit()
    load_rejections(c)
    audit(c, staff, "decline_replacement", "group", group_id, f"declined {list(added_enquiry_ids)}: {reason or ''}")
    return {"group_id": group_id, "declined": list(added_enquiry_ids), "message": "Replacement declined and recorded; these people will not be offered to this group again. The group stays awaiting a replacement."}


def _remove_person_from_form(c, e: dict, member: dict | None, staff: str, reason: str, when: str) -> tuple[str, dict]:
    """GP9(e): take one person off a form party (a couple or friends on one enquiry) because they alone are barred while the
    others carry on. `member` is the party_members row, or None when the barred person is the enquirer who filled in the form:
    then the first other person takes over the enquiry (their own name, email and country replace the
    enquirer's; the shared answers - budget, dates, room and housemate preferences - apply to the whole party under I16(d), so
    they stay). The barred person's details are kept in `barred_people`. Couples (3.10) are re-pointed; a couple that loses one
    half is no couple. Returns (barred person's name, person-id mapping old -> new or None for the one who left)."""
    eid = e["id"]
    members = rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))
    n_old = len(members)
    if member is None:  # the enquirer is barred; the first other person takes over the form
        if not members:
            raise ValueError("Nobody else is on this form")
        taker = members[0]
        name = f"{e['first_name']} {e['last_name']}".strip()
        # I8: sex/dob are no longer recorded for barred people (columns stay, left NULL); the taker's row keeps '' (not collected)
        c.execute("""INSERT INTO barred_people(enquiry_id, name, email, birth_country, was_lead, reason, barred_at, barred_by)
                     VALUES (?,?,?,?,1,?,?,?)""", (eid, name, e.get("email"), e.get("birth_country") or e.get("birth_country_raw"), reason, when, staff))
        first, _, last = (taker["name"] or "").strip().partition(" ")
        _identity("promote_to_lead", c, eid, taker["id"], "barred")   # GP13: same human keeps their identity as the new lead; the barred lead's is retired
        # The taker's own recorded country (or their raw answer) only; a missing answer stays missing - the former enquirer's country
        # is never this person's. Their phone is not held either, so the former enquirer's dialling code goes with the number.
        c.execute("""UPDATE enquiries SET first_name=?, last_name=?, email=?, sex='', sex_other=NULL, dob='', birth_country=?, birth_country_raw=?, birth_country_confirmed=?,
                     phone='', phone_code=NULL, mate_note=TRIM(COALESCE(mate_note, '') || ' ' || ?) WHERE id=?""",
                  (first or taker["name"], last, taker["email"],
                   taker.get("birth_country") or taker.get("birth_country_raw") or None, taker.get("birth_country_raw") or None, 1 if taker.get("birth_country") else 0,
                   f"{name}, who filled in this form, was barred from matching on {when[:10]}; {taker['name']} took over the enquiry (please check their phone number). The shared answers were kept.", eid))
        c.execute("DELETE FROM party_members WHERE id=?", (taker["id"],))
        # Source identity (root GO, 9 Nov 2026): the row's individual answers (study/work, lifestyle, free text) and everything
        # derived from them (validated notes facts, extraction cache) were the former enquirer's. They are kept as history but
        # flagged unverified: masked from every packet/brief/introduction and the notes step fails closed until staff confirm them
        # with the person who took over (enquiries.update_enquiry lead_answers_confirmed). The taker's OWN fresh introduction
        # details (intro_sex / intro_age, recorded on their party row) move with them.
        c.execute("""UPDATE enquiries SET lead_answers_unverified_at=?, notes_status='unverified', notes_packet_json=NULL, notes_input_key=NULL, notes_residual=NULL,
                     notes_updated_at=?, intro_sex=?, intro_sex_other=?, intro_age=? WHERE id=?""",
                  (when, when, taker.get("intro_sex"), taker.get("intro_sex_other"), taker.get("intro_age"), eid))
        c.execute("DELETE FROM note_facts WHERE enquiry_id=?", (eid,))
        c.execute("DELETE FROM note_extraction_cache WHERE enquiry_id=?", (eid,))
        refs = {"lead": None, "m1": "lead", **{f"m{i}": f"m{i - 1}" for i in range(2, n_old + 1)}}
    else:
        name = member["name"]
        c.execute("""INSERT INTO barred_people(enquiry_id, name, email, birth_country, was_lead, reason, barred_at, barred_by)
                     VALUES (?,?,?,?,0,?,?,?)""", (eid, name, member.get("email"), member.get("birth_country") or member.get("birth_country_raw"), reason, when, staff))
        _identity("member_left", c, eid, member["id"], "barred")   # GP13: this person's identity is retired BEFORE the row goes
        c.execute("DELETE FROM party_members WHERE id=?", (member["id"],))
        idx = next(i for i, m in enumerate(members, start=1) if m["id"] == member["id"])
        refs = {"lead": "lead", **{f"m{i}": (None if i == idx else (f"m{i}" if i < idx else f"m{i - 1}")) for i in range(1, n_old + 1)}}
        c.execute("UPDATE enquiries SET mate_note=TRIM(COALESCE(mate_note, '') || ' ' || ?) WHERE id=?",
                  (f"{name}, who was on this form, was barred from matching on {when[:10]}; the rest of the party carried on without them.", eid))
    size = n_old  # one fewer than before (lead + n_old members)
    pairs = []
    for pr in (pj(e.get("couples_json")) or []):
        if isinstance(pr, list) and len(pr) == 2:
            x, y = refs.get(pr[0]), refs.get(pr[1])
            if x and y:
                pairs.append([x, y])
    if size == 1:
        c.execute("UPDATE enquiries SET party='Alone', party_size=1, couples_answer=NULL, couples_json=NULL WHERE id=?", (eid,))
    else:
        c.execute("UPDATE enquiries SET party_size=?, couples_answer=?, couples_json=? WHERE id=?", (size, "Yes" if pairs else ("No" if e.get("couples_answer") else None), j(pairs), eid))
    _identity("reconcile", c, eid, "barred")   # GP13: identities follow the rows as they are now (same guarded step)
    pid = lambda ref: str(eid) if ref == "lead" else (f"{eid}{ref}" if ref else None)
    return name, {pid(k): pid(v) for k, v in refs.items()}


def _lose_person(c, g: dict, mapping: dict, who: str, when: str) -> None:
    """GP9(e)/GP10: one person on a form party is barred and the rest of the party stays in the group. Their bed is emptied in the
    stored allocations (the others' person ids are renumbered), the group is kept together as "Awaiting replacement"; each run
    keeps looking and a find is flagged for staff to accept (GP10).
    When the barred person was the enquirer, the person who took over the form now carries the enquiry's id in the allocation
    (mapping "<eid>m1" -> "<eid>"); the group_members lead row (keyed by enquiry id, written by _write_members from the lead's
    occupant) is rewritten in the same step from THAT remapped occupant of the group's own allocation - their unchanged proposed
    share, room and bed - so the staff list and the person's own view show the promoted person's figure, not the barred
    person's. Nothing is divided or guessed: when the takeover cannot be proved from the allocation (no remapped lead occupant,
    or several) the row's share, room and bed are set to NULL = not known; the barred person's figure never stays against the
    person who took over."""
    lead_id = next((v for v in mapping.values() if v is not None and str(v).isdigit()), None)
    taken_over = []   # (room, occupant) of the group's own allocation now carrying the lead id after a renumber
    for table, key in (("groups", "id"), ("group_apartments", "group_id")):
        for r in rows(c.execute(f"SELECT id AS rid, allocation_json FROM {table} WHERE {key}=?", (g["id"],))):
            alloc = pj(r["allocation_json"])
            if not alloc:
                continue
            for room in alloc.get("rooms", []):
                kept = []
                for o in room.get("occupants", []):
                    old = str(o.get("person_id"))
                    if old in mapping:
                        if mapping[old] is None:
                            continue
                        o["person_id"] = mapping[old]
                        if table == "groups" and old != mapping[old] and str(mapping[old]) == lead_id:
                            taken_over.append((room, o))
                    kept.append(o)
                room["empty_beds"] = room.get("empty_beds", 0) + (len(room.get("occupants", [])) - len(kept))
                room["occupants"] = kept
            alloc["gp5_exception"] = "Awaiting replacement: an empty bed is allowed until a replacement is accepted"
            c.execute(f"UPDATE {table} SET allocation_json=? WHERE id=?", (j(alloc), r["rid"]))
    takeover = lead_id is not None and any(k != v and str(v) == lead_id for k, v in mapping.items())
    if takeover and len(taken_over) == 1:
        room, o = taken_over[0]
        c.execute("UPDATE group_members SET weekly_share=?, room=?, bed=?, arrangement=? WHERE group_id=? AND enquiry_id=?",
                  (o.get("share"), room.get("room"), o.get("bed"), ", ".join(o.get("arrangement", []) or []), g["id"], int(lead_id)))
    elif takeover:
        c.execute("UPDATE group_members SET weekly_share=NULL, room=NULL, bed=NULL, arrangement='' WHERE group_id=? AND enquiry_id=?", (g["id"], int(lead_id)))
    note = (f"Awaiting replacement since {when[:10]}: {who} was barred from matching by staff; the people who enquired with them chose to stay. "
            "The group was kept together with 1 empty bed (an empty bed is allowed while a replacement is awaited). "
            "Each matching run keeps looking; a replacement is only applied when staff accept it.")
    c.execute("""UPDATE groups SET status=?, awaiting_note=?, awaiting_since=COALESCE(awaiting_since, ?), awaiting_people_missing=COALESCE(awaiting_people_missing,0)+1,
                 awaiting_reason='bar', flag_kind='bar', replacement_json=NULL, replacement_found_at=NULL, replacement_checked_at=?, updated_at=? WHERE id=?""",
              (AWAITING, note, when, when, when, g["id"]))
    for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],))):
        c.execute("UPDATE enquiries SET status=? WHERE id=?", (AWAITING, m["enquiry_id"]))
    # Source integrity (root GO, 9 Nov 2026): the stored review and briefs were written about the people as they WERE. One of them
    # has gone (and on a lead takeover the same enquiries row now carries another person), so nothing of it is "current" any more:
    # the evidence hash is cleared (the writers' late-result protocol: a review/brief/introduction in flight for the old people
    # fails its hash check and is never written back; saved rounds no longer match the group), the approval and brief are marked
    # stale, readiness is withdrawn. History rows are kept as history (their own ref maps say who they were about).
    c.execute("""UPDATE groups SET evidence_hash=NULL, ready_at=NULL, brief_status=CASE WHEN brief_status='ok' THEN 'stale' ELSE brief_status END,
                 ai_status=CASE WHEN ai_status IS NULL THEN NULL ELSE 'stale' END, held_reason=? WHERE id=?""",
              ("A member left the group: the earlier review and brief describe a different set of people", g["id"]))


def bar_enquiry(c, enquiry_id: int, staff: str, reason: str, friends_choice: str | None = None, person: str | int | None = None,
                party_choice: str | None = None) -> dict:
    """GP9(c)/(d)/(e): bar ONE person from every matching run and every replacement. Barring never bars the people they came
    with (GP9(e)), whether they enquired together on one form or joined through "Bring a mate"; those people choose for themselves.

    Form party (a couple or friends on this one enquiry): `person` says who is barred ("lead" = the enquirer who filled in the
    form, or a party_members id) and `party_choice` what the others chose: 'leave' (they leave with the barred person: the whole
    form is out of matching, but only the named person is recorded as barred) or 'stay' (the barred person is taken off the
    form; the others carry on, in their group if they have one, with an empty bed awaiting a replacement, GP10).

    "Bring a mate" friends in the same group: `friends_choice` 'leave' (they leave the group with the barred person and are
    grouped again together; they are not barred) or 'stay' (they stay; only the barred person is removed). The barred person
    leaves the friend party either way, because they cannot be grouped; if they sent the share link, the first friend who joined
    takes it over so the rest stay together (GP11(n)).

    A group holding the barred person is NOT dissolved (GP9(d)): Proposed groups get the AI-chosen replacement at once;
    Introduced or later groups wait for one and the find is flagged for staff (GP10). A group left with nobody is dissolved."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to bar someone from matching.")
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    if not e:
        raise ValueError("Enquiry not found")
    if e.get("barred_at"):
        raise ValueError("This enquiry is already barred from matching.")
    when = now_iso()
    members = rows(c.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (enquiry_id,)))
    member = None
    if person not in (None, "", "lead"):
        member = next((m for m in members if str(m["id"]) == str(person)), None)
        if not member:
            raise ValueError("That person is not on this form")
    who = member["name"] if member else e["first_name"]
    if members and party_choice not in ("leave", "stay"):
        raise ValueError(f"Speak with the people who enquired with {who} first, then say whether they leave with them or stay without them")
    if not members:
        party_choice = None
    # "Bring a mate" friends (GP11): the barred person always leaves the friend party; the root link passes on (GP11(n))
    linked = [i for i in linked_party_ids(c, enquiry_id) if i != enquiry_id]
    linked_in_group = []
    for g in rows(c.execute("SELECT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status NOT IN ('Leased','Dissolved')", (enquiry_id,))):
        others_in = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
        linked_in_group += [i for i in _with_linked(c, [enquiry_id], others_in) if i not in (enquiry_id, *linked_in_group)]
    if linked_in_group and party_choice != "stay" and friends_choice not in ("leave", "stay"):
        raise ValueError(f"Speak with the friends {who} brought along (or came with) first, then say whether they leave the group with them or stay")
    names = lambda ids: [r["first_name"] for r in rows(c.execute(f"SELECT first_name FROM enquiries WHERE id IN ({','.join('?' * len(ids))})", ids))] if ids else []
    friend_names = names(linked_in_group)

    replaced, awaiting, flagged, dissolved = [], [], [], []
    if party_choice == "stay":
        # GP9(e): only the named person goes; the rest of the form party carries on (in their group, with an empty bed).
        # T5: removing the person from the form (party_members delete, allocation scrub) and the group changes are ONE guarded step.
        from . import stock_lifecycle
        with db.retention_guard(c, label="bar person from form"):
            name, mapping = _remove_person_from_form(c, e, member, staff, reason, when)
            for g in rows(c.execute("""SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status NOT IN ('Leased','Dissolved') ORDER BY g.is_primary DESC, g.id""", (enquiry_id,))):
                if g["status"] == "Proposed" and not g["is_primary"]:
                    stock_lifecycle.delete_groups(c, [g["id"]])   # G-SL2: one deletion helper (pending stock work closes with the group)
                    continue
                _lose_person(c, g, mapping, name, when)
                _membership_changed(c, g["id"], "barred", staff)   # GP13, same guarded step: one person left, everyone approves again
                awaiting.append(g["id"])
        audit(c, staff, "bar_from_matching", "enquiry", enquiry_id, f"barred {name} only (the rest of the party stayed, now {len(members)} people); reason: {reason}; awaiting {awaiting}")
        bits = [f"{name} is barred from matching. The people who enquired with them are not barred and carry on without them."]
        if member is None:
            bits.append(f"{e_first(c, enquiry_id)} has taken over this enquiry; please check their phone number.")
        if awaiting:
            bits.append(f"Group(s) {', '.join(map(str, awaiting))} kept together with an empty bed, awaiting a replacement (MateBot AI keeps looking; you accept the find).")
        return {"id": enquiry_id, "barred_at": None, "barred_person": name, "party_stayed": True, "replaced_groups": [], "flagged_groups": [], "awaiting_groups": awaiting,
                "dissolved_groups": [], "party_size": len(members), "message": " ".join(bits)}

    # the enquiry record itself is barred (a lone enquirer, a linked friend, or a form party that chose to leave together)
    c.execute("UPDATE enquiries SET barred_at=?, barred_by=?, barred_reason=?, barred_person=? WHERE id=?", (when, staff, reason, who if member else None, enquiry_id))
    maybe_commit(c)
    # T5: view-only alternative groupings holding the barred person simply go (the next run rebuilds alternatives). Deleting them is
    # one guarded step BEFORE the replacement work below, which may consult the model and so must not run under a guard.
    from . import stock_lifecycle
    with db.retention_guard(c, label="bar: drop alternatives"):
        for g in rows(c.execute("""SELECT g.id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status='Proposed' AND g.is_primary=0
                                   AND EXISTS (SELECT 1 FROM group_members o WHERE o.group_id=g.id AND o.enquiry_id<>?) ORDER BY g.id""", (enquiry_id, enquiry_id))):
            stock_lifecycle.delete_groups(c, [g["id"]])   # G-SL2: one deletion helper
    if linked:  # GP9(e)/GP11(n): the barred person leaves the friend party; the link passes to the first friend if they held it
        unlink_from_party(c, enquiry_id, f"Barred from matching on {when[:10]}; no longer linked to {', '.join(names(linked))}. If the bar is lifted they are matched on their own.")
    for g in rows(c.execute("""SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.status NOT IN ('Leased','Dissolved') ORDER BY g.is_primary DESC, g.id""", (enquiry_id,))):
        others = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? AND enquiry_id<>?", (g["id"], enquiry_id)))]
        if not others:
            c.execute("UPDATE groups SET status='Dissolved', rerank_note=?, reranked_at=?, flag_kind='bar', updated_at=? WHERE id=?",
                      (f"Dissolved on {when[:10]}: {who} was barred from matching by staff and nobody else was left in the group.", when, when, g["id"]))
            dissolved.append(g["id"])
            continue
        if g["status"] == "Proposed" and not g["is_primary"]:
            continue   # view-only alternatives were deleted in the guarded step above (a late one that appeared since is left to the next run)
        others_in = [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
        friends_here = [i for i in linked_in_group if i in others_in]
        leaving = [enquiry_id] + (friends_here if friends_choice == "leave" else [])
        if len(leaving) >= len(others_in):
            c.execute("UPDATE groups SET status='Dissolved', rerank_note=?, reranked_at=?, flag_kind='bar', updated_at=? WHERE id=?",
                      (f"Dissolved on {when[:10]}: {who} was barred from matching by staff and the rest of the group were friends of theirs who chose to leave with them (they are not barred).", when, when, g["id"]))
            for eid in others_in:
                c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', 'Introduced', 'Group confirmed', ?)", (eid, AWAITING))
            dissolved.append(g["id"])
            continue
        why = None
        if friends_here:
            why = (f"{who} was barred from matching by staff; their friends ({', '.join(names(friends_here))}) chose to "
                   + ("leave the group with them and are matched together again (they are not barred)" if friends_choice == "leave" else "stay in the group (they are not barred)"))
        rep = _lose_members(c, g, leaving, staff, cause="bar", auto_apply=(g["status"] == "Proposed"), why=why)
        if rep["new_group_id"]:
            replaced.append({"group_id": g["id"], "new_group_id": rep["new_group_id"]})
        elif rep["replacement_flagged"]:
            flagged.append(g["id"])
        else:
            awaiting.append(g["id"])
    c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', ?)", (enquiry_id, AWAITING))
    c.commit()
    for gid in dissolved:   # GP13: groups that ended here close their membership version (the _lose_members paths did their own)
        _membership_changed(c, gid, "dissolved", staff)
    party = len(members)
    audit(c, staff, "bar_from_matching", "enquiry", enquiry_id, f"barred {who}; reason: {reason}; party of {party + 1}{' left with them' if party else ''}; friends {friend_names} {friends_choice or ''}; replaced {replaced}; flagged {flagged}; awaiting {awaiting}; dissolved {dissolved}")
    bits = [f"{who} is barred from matching."]
    if party:
        bits.append(f"The {party} {'person' if party == 1 else 'people'} who enquired with them chose to leave with them: they are not barred, but this form is out of matching until the bar is lifted.")
    if friend_names:
        bits.append(f"Their friends ({', '.join(friend_names)}) are not barred and chose to {'leave the group with them; they stay together and are grouped again' if friends_choice == 'leave' else 'stay in the group'}.")
    elif linked:
        bits.append(f"They are no longer linked to {', '.join(names(linked))}, who stay together and are not barred.")
    if replaced:
        bits.append("Removed from proposed group(s) " + ", ".join(f"{r['group_id']} (now Group {r['new_group_id']}, AI-chosen replacement applied)" for r in replaced) + ".")
    if flagged:
        bits.append(f"Removed from group(s) {', '.join(map(str, flagged))}: the AI found a replacement for you to accept or decline.")
    if awaiting:
        bits.append(f"Removed from group(s) {', '.join(map(str, awaiting))}: no replacement passes every rule yet, so the group is awaiting one.")
    if dissolved:
        bits.append(f"Group(s) {', '.join(map(str, dissolved))} had nobody else left and were dissolved.")
    return {"id": enquiry_id, "barred_at": when, "barred_person": who if member else None, "party_stayed": False, "friends": linked_in_group, "friends_choice": friends_choice,
            "replaced_groups": replaced, "flagged_groups": flagged, "awaiting_groups": awaiting, "dissolved_groups": dissolved, "party_size": party + 1, "message": " ".join(bits)}


def e_first(c, eid: int) -> str:
    r = row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (eid,)))
    return r["first_name"] if r else ""


def lift_bar(c, enquiry_id: int, staff: str, note: str | None = None) -> dict:
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    if not e:
        raise ValueError("Enquiry not found")
    if not e.get("barred_at"):
        raise ValueError("This enquiry is not barred.")
    c.execute("UPDATE enquiries SET barred_at=NULL, barred_by=NULL, barred_reason=NULL, barred_person=NULL WHERE id=?", (enquiry_id,))
    c.commit()
    audit(c, staff, "lift_bar", "enquiry", enquiry_id, f"bar lifted (was: {e.get('barred_reason')}; person: {e.get('barred_person') or 'enquirer'}); note: {note or ''}")
    who = e.get("barred_person") or e["first_name"]
    return {"id": enquiry_id, "message": f"Bar on {who} lifted. {'The whole form is' if e.get('party_size', 1) and (e.get('party_size') or 1) > 1 else 'They are'} included in the next matching run (any earlier rejections still stand)."}


def _create_group_from_option(c, g: dict, opt: dict, basis: str, replaced_from: int, rejected: list[int], units: list | None = None) -> int:
    ai_ok = opt.get("ai_rating") is not None
    c.execute("""INSERT INTO groups(match_run_id, apartment_id, status, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly, allocation_json,
                 rule_score, ai_rating, ai_rationale, ai_model, ranking_basis, created_at, updated_at, is_primary, ai_status, run_kind, replaced_from_group_id, rejected_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,'replacement',?,?)""",
              (g["match_run_id"], g["apartment_id"], "Proposed", opt["lease_start"], opt["lease_end"], opt["rate_kind"], opt["exit_window"], opt["weekly_rent"], opt["extra_bed"], opt["total_weekly"],
               _alloc_json(c, opt["allocation"], units, score_parts=opt.get("score_parts"), replaced_from=replaced_from), opt["rule_score"], opt.get("ai_rating"), opt.get("ai_rationale"),
               ai.MODEL if ai_ok else None, basis if ai_ok else "Rules only (AI unavailable)", now_iso(), now_iso(), "ok" if ai_ok else "skipped", replaced_from, j(rejected)))
    new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    eids = sorted({int(o["person_id"]) for r in opt["allocation"] for o in r["occupants"] if str(o["person_id"]).isdigit()})
    c.execute("UPDATE groups SET member_key=? WHERE id=?", (",".join(map(str, eids)), new_id))
    c.execute("""INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly, allocation_json)
                 VALUES (?,?,1,1,?,?,?,?,?,?,?,?,?,?)""", (new_id, g["apartment_id"], opt["rule_score"], opt.get("ai_rationale"), opt["lease_start"], opt["lease_end"], opt["rate_kind"], opt["exit_window"],
                                                  opt["weekly_rent"], opt["extra_bed"], opt["total_weekly"], _alloc_json(c, opt["allocation"], units, score_parts=opt.get("score_parts"))))
    _write_members(c, new_id, opt["allocation"])
    if units:  # GP6 / GP7: the new group still lists every other available apartment it suits, ranked by rule score (rules only)
        extra = []
        for a2 in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL AND id<>?", (g["apartment_id"],))):
            apt2 = apartment_from_row(a2)
            if not all(_unit_fits_apartment(apt2, u) for u in units):
                continue
            chk = rules.check_group(apt2, units)
            if chk.ok:
                sc, parts = preference_score(apt2, units, chk)
                extra.append((sc, _apt_option(a2, apt2, {"check": chk, "rule_score": sc, "score_parts": parts, "units": units})))
        extra.sort(key=lambda x: -x[0])
        for rank, (sc, o) in enumerate(extra[:5], start=2):
            c.execute("""INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window, weekly_rent, extra_bed, total_weekly, allocation_json)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (new_id, o["apartment_id"], rank, rank, sc, None, o["lease_start"], o["lease_end"], o["rate_kind"], o["exit_window"],
                                                           o["weekly_rent"], o["extra_bed"], o["total_weekly"], _alloc_json(c, o["allocation"], units, score_parts=o["score_parts"])))
    for eid in eids:
        c.execute("UPDATE enquiries SET status='Proposed in group' WHERE id=? AND status='New'", (eid,))
        # GP7(a): the new group's members leave any other proposed group they were primary in
    c.execute(f"UPDATE groups SET is_primary=0, updated_at=? WHERE status='Proposed' AND id<>? AND id IN (SELECT group_id FROM group_members WHERE enquiry_id IN ({','.join('?' * len(eids))}))", [now_iso(), new_id, *eids])
    # T6: the members changed, so the AI assesses the new group and ranks its apartments before it is ready for reservations.
    # The refill rating above chose the replacement (GP3); the full assessment runs in the queue at top priority.
    ai_queue.enqueue(c, [new_id], priority=ai_queue.PRIORITY_CHANGE, reason=f"replacement for group {replaced_from}", commit=False)
    return new_id


# ---------------------------------------------------------------------------
# GP11 "Bring a mate"
# ---------------------------------------------------------------------------
def _with_linked(c, ids: list[int], within: list[int]) -> list[int]:
    """Add every enquiry linked with `ids` that is also in `within` (a group's member list)."""
    out = list(ids)
    for i in ids:
        for k in linked_party_ids(c, i):
            if k in within and k not in out:
                out.append(k)
    return out


def _unit_for(c, eid: int):
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    if not e:
        return None
    u = unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,))))
    _identities_for_computation(c, [u])
    return u


def _live_group_of(c, enquiry_id: int) -> dict | None:
    return row(c.execute("""SELECT g.* FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE gm.enquiry_id=? AND g.is_primary=1
                            AND g.status NOT IN ('Dissolved','Leased') ORDER BY g.id DESC LIMIT 1""", (enquiry_id,)))


def _mate_fit(c, g: dict, root_id: int, friend_ids: list[int]) -> dict | None:
    """GP11(d): can the friend(s) join the enquirer's group? Try everyone first; then drop the weakest-matched members who are
    not part of the enquirer's linked party, one at a time, until the group passes every rule at its apartment. At least one
    unaffiliated member must stay (otherwise it is no longer that group: GP11(g) applies). Returns {units, dropped, check, score, parts, apt, a}."""
    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    if not a or a.get("leased_at"):
        return None
    apt = apartment_from_row(a)
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (g["id"],)))]
    party = [i for i in linked_party_ids(c, root_id) if i in member_ids] or [root_id]
    party_units = [u for u in (_unit_for(c, i) for i in party) if u]
    friend_units = [u for u in (_unit_for(c, i) for i in friend_ids) if u]
    others = [u for u in (_unit_for(c, i) for i in member_ids if i not in party) if u]
    if not friend_units or not others:
        return None
    _, cap = rules.apartment_capacity(apt.bedrooms)
    cap = min(cap, rules.APT_TYPES[rules.normalise_type(apt.type_code)]["a4b_max"], rules.MAX_GROUP)
    if not all(_unit_fits_apartment(apt, f) for f in friend_units):
        return None
    if not all(_pair_ok(f, p) for f in friend_units for p in party_units):
        return None  # the friends do not pass the pairwise rules with the people they are linked to: nothing can hold them together
    others.sort(key=lambda u: _affinity(u, party_units + friend_units))  # weakest-matched first
    dropped: list = []
    while len(others) >= 1:
        grp = party_units + friend_units + others
        if sum(u.size for u in grp) <= cap and all(_pair_ok(f, o) for f in friend_units for o in others):
            chk = rules.check_group(apt, grp)
            if chk.ok:
                sc, parts = preference_score(apt, grp, chk)
                return {"units": grp, "dropped": [int(u.id) for u in dropped], "check": chk, "rule_score": sc, "score_parts": parts, "apt": apt, "a": a}
        dropped.append(others.pop(0))
    return None


def _apply_mate(c, g: dict, fit: dict, root_id: int, friend_ids: list[int], staff: str | None, how: str) -> int:
    """The friend joins: a new Proposed group replaces the old one (queued for its AI assessment); replaced members go back to
    matching as 'New' (GP11(h): not rejected, nothing recorded against them)."""
    when = now_iso()
    gid = g["id"]
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (gid,)))]
    opt = _group_payload(fit["a"], {"check": fit["check"], "rule_score": fit["rule_score"], "score_parts": fit["score_parts"], "units": fit["units"]})
    c.execute("UPDATE groups SET status='Dissolved', updated_at=? WHERE id=?", (when, gid))
    for eid in member_ids:
        c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', 'Introduced', 'Group confirmed', ?)", (eid, AWAITING))
    new_id = _create_group_from_option(c, g, opt, "Rules only", replaced_from=gid, rejected=_lst_int(g.get("rejected_json")), units=fit["units"])
    names = lambda ids: [r["first_name"] for r in rows(c.execute(f"SELECT first_name FROM enquiries WHERE id IN ({','.join('?' * len(ids))})", ids))] if ids else []
    fn, dn, rn = names(friend_ids), names(fit["dropped"]), names([root_id])
    repl = f" {', '.join(dn)} made room and {'is' if len(dn) == 1 else 'are'} back in matching (not rejected)." if dn else ""
    note = f"Dissolved on {when[:10]}: {rn[0] if rn else 'the enquirer'} brought a friend ({', '.join(fn)}){', ' + how if how else ''}. The group continues as Group {new_id}.{repl}"
    c.execute("UPDATE groups SET rerank_note=?, reranked_at=?, flag_kind='mate', updated_at=? WHERE id=?", (note, when, when, gid))
    c.execute("UPDATE groups SET awaiting_note=?, discount_note=? WHERE id=?",
              (f"Continues Group {gid}: {', '.join(fn)} joined as {rn[0] if rn else 'the enquirer'}'s friend{repl}", g.get("discount_note"), new_id))
    for eid in friend_ids:
        c.execute("UPDATE enquiries SET mate_note=? WHERE id=?", (f"Joined {rn[0] if rn else 'the enquirer'}'s group (Group {new_id}) on {when[:10]}.", eid))
    for eid in fit["dropped"]:
        c.execute("UPDATE enquiries SET mate_note=? WHERE id=?", (f"Left Group {gid} on {when[:10]} to make room for a friend another member brought along. Back in matching; not rejected.", eid))
    c.commit()
    _membership_changed(c, gid, "dissolved", staff)   # GP13: the friend's arrival is a new membership - nobody's earlier yes carries over
    _membership_changed(c, new_id, "mate", staff, stamp_units=fit["units"])
    audit(c, staff, "bring_mate_applied", "group", gid, f"friend {friend_ids} joined {root_id}'s group -> new group {new_id}; replaced {fit['dropped']}; {how}")
    return new_id


def _leave_with_friend(c, g: dict, root_id: int, friend_ids: list[int], staff: str | None, why: str) -> dict:
    """GP11(g): the enquirer (with anyone already linked) leaves the group together with the friend; both are matched as a pair
    on the next run. The remaining members get a replacement as usual (GP10 if none)."""
    member_ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (g["id"],)))]
    party = [i for i in linked_party_ids(c, root_id) if i in member_ids] or [root_id]
    when = now_iso()
    if len(party) >= len(member_ids):  # nobody else in the group: it simply ends; the party is rebuilt with the friend on the next run
        c.execute("UPDATE groups SET status='Dissolved', rerank_note=?, reranked_at=?, flag_kind='mate', updated_at=? WHERE id=?",
                  (f"Dissolved on {when[:10]}: {why}. Everyone in it is now matched together with the new friend on the next run.", when, when, g["id"]))
        for eid in member_ids:
            c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group', 'Introduced', 'Group confirmed', ?)", (eid, AWAITING))
        c.commit()
        _membership_changed(c, g["id"], "dissolved", staff)
        rep = {"group_id": g["id"], "new_group_id": None, "awaiting_replacement": False, "message": "Group dissolved; the party is matched together on the next run."}
    else:
        rep = _lose_members(c, g, party, staff, cause="mate", auto_apply=(g["status"] == "Proposed"))
    for eid in party + list(friend_ids):
        c.execute("UPDATE enquiries SET status='New', mate_note=? WHERE id=? AND status IN ('New', 'Proposed in group', 'Introduced', 'Group confirmed', ?)",
                  (f"Left Group {g['id']} on {when[:10]} to be matched together with a friend they brought along (the friend did not fit that group).", eid, AWAITING))
    c.commit()
    audit(c, staff, "bring_mate_left_group", "group", g["id"], f"{root_id} left with friend {friend_ids}: {why}; remaining members: {rep.get('message')}")
    return rep


def bring_mate(c, root_id: int, friend_id: int, staff: str | None = None) -> dict:
    """GP11(d)-(h): a friend has submitted their own form through the enquirer's share link and is now linked to them.
    Decide what happens to the enquirer's current group."""
    root = row(c.execute("SELECT * FROM enquiries WHERE id=?", (root_id,)))
    friend = row(c.execute("SELECT * FROM enquiries WHERE id=?", (friend_id,)))
    if not root or not friend:
        raise ValueError("Enquiry not found")
    g = _live_group_of(c, root_id)
    if not g:
        c.execute("UPDATE enquiries SET mate_note=TRIM(COALESCE(mate_note, '') || ?) WHERE id=?", (f" Linked to {root['first_name']}'s enquiry on {now_iso()[:10]}; they are matched together on the next run.", friend_id))
        c.commit()
        return {"outcome": "pool", "group_id": None, "message": f"{friend['first_name']} is linked to {root['first_name']}; they will be matched together on the next run."}
    fit = _mate_fit(c, g, root_id, [friend_id])
    if fit:
        if g["status"] in ("Proposed", AWAITING):  # GP11(f): not yet shared, or awaiting a replacement member: no staff approval needed
            how = "added before the group was shared with anyone" if g["status"] == "Proposed" else "added while the group was awaiting a replacement member"
            new_id = _apply_mate(c, g, fit, root_id, [friend_id], staff, how=how)
            return {"outcome": "joined", "group_id": g["id"], "new_group_id": new_id, "replaced": fit["dropped"], "message": f"{friend['first_name']} joined {root['first_name']}'s group (now Group {new_id})."}
        # shared with its members already (Introduced / Group confirmed): staff approve the change
        when = now_iso()
        c.execute("UPDATE mate_requests SET status='superseded', decided_at=? WHERE group_id=? AND status='pending' AND friend_enquiry_id=?", (when, g["id"], friend_id))
        c.execute("""INSERT INTO mate_requests(group_id, root_enquiry_id, friend_enquiry_id, replace_json, apartment_id, status, note, created_at, is_test)
                     VALUES (?,?,?,?,?,'pending',?,?,?)""",
                  (g["id"], root_id, friend_id, j(fit["dropped"]), g["apartment_id"],
                   f"{root['first_name']} brought a friend, {friend['first_name']}, who fits this group" + (f" if {len(fit['dropped'])} current member{'s' if len(fit['dropped']) != 1 else ''} make room" if fit["dropped"] else "") + ". The group has already been shared with its members, so this needs your approval.",
                   when, root.get("is_test") or 0))
        c.execute("UPDATE enquiries SET mate_note=? WHERE id=?", (f"Waiting for staff to approve joining {root['first_name']}'s group (Group {g['id']}).", friend_id))
        c.commit()
        audit(c, staff, "bring_mate_queued", "group", g["id"], f"friend {friend_id} fits {root_id}'s {g['status']} group; replace {fit['dropped']}; awaiting staff approval")
        return {"outcome": "queued", "group_id": g["id"], "replaced": fit["dropped"], "message": f"{friend['first_name']} fits {root['first_name']}'s group, which has already been shared; staff will approve the change."}
    rep = _leave_with_friend(c, g, root_id, [friend_id], staff, why=f"{root['first_name']} brought a friend ({friend['first_name']}) who did not fit this group")
    return {"outcome": "left", "group_id": g["id"], "new_group_id": rep.get("new_group_id"), "awaiting_replacement": rep.get("awaiting_replacement"),
            "message": f"{friend['first_name']} did not fit {root['first_name']}'s group; they left it together and will be matched as a pair. {rep.get('message') or ''}".strip()}


def decide_mate_request(c, req_id: int, staff: str, approve: bool, reason: str | None = None, choice: str = "leave") -> dict:
    """GP11(f): staff approve or decline a friend joining a group that was already shared with its members.
    GP11(l) (Bill, 28 Oct 2026): on a decline, staff have spoken with the person who invited the friend, who chose
    choice 'leave' (they leave the group with their friend and are grouped again together) or 'stay' (they stay in the group;
    the friend leaves the party and is matched on their own)."""
    r = row(c.execute("SELECT * FROM mate_requests WHERE id=?", (req_id,)))
    if not r or r["status"] != "pending":
        raise ValueError("This request has already been decided.")
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (r["group_id"],)))
    when = now_iso()
    friend = row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (r["friend_enquiry_id"],))) or {"first_name": "the friend"}
    root = row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (r["root_enquiry_id"],))) or {"first_name": "the enquirer"}
    if not g or g["status"] in ("Dissolved", "Leased"):
        c.execute("UPDATE mate_requests SET status='superseded', decided_at=?, decided_by=? WHERE id=?", (when, staff, req_id)); c.commit()
        raise ValueError("That group has since changed; nothing to approve.")
    if approve:
        fit = _mate_fit(c, g, r["root_enquiry_id"], [r["friend_enquiry_id"]])  # recomputed now: the group or pool may have moved
        c.execute("UPDATE mate_requests SET status='approved', decided_at=?, decided_by=? WHERE id=?", (when, staff, req_id))
        if fit:
            new_id = _apply_mate(c, g, fit, r["root_enquiry_id"], [r["friend_enquiry_id"]], staff, how=f"approved by {staff}")
            c.commit()
            return {"ok": True, "outcome": "joined", "new_group_id": new_id, "replaced": fit["dropped"],
                    "message": f"{friend['first_name']} joined the group, which continues as Group {new_id} (Proposed; the AI assesses it next)."}
        rep = _leave_with_friend(c, g, r["root_enquiry_id"], [r["friend_enquiry_id"]], staff, why=f"{root['first_name']}'s friend {friend['first_name']} no longer fits this group")
        return {"ok": True, "outcome": "left", "new_group_id": rep.get("new_group_id"), "message": f"{friend['first_name']} no longer fits, so {root['first_name']} and {friend['first_name']} left the group together and will be matched as a pair. {rep.get('message') or ''}".strip()}
    if choice not in ("leave", "stay"):
        raise ValueError(f"Speak with {root['first_name']} first, then say whether they leave with {friend['first_name']} or stay without them")
    c.execute("UPDATE mate_requests SET status='declined', decided_at=?, decided_by=?, note=? WHERE id=?",
              (when, staff, (r.get("note") or "") + (f" Declined: {reason}" if reason else " Declined.") + (f" {root['first_name']} chose to stay without {friend['first_name']}." if choice == "stay" else f" {root['first_name']} chose to leave with {friend['first_name']}."), req_id))
    if choice == "stay":  # GP11(l): the inviter stays; the friend leaves the party and is matched on their own
        c.commit()
        unlink_from_party(c, r["friend_enquiry_id"], f"Staff declined them joining {root['first_name']}'s group (Group {g['id']}) on {when[:10]}; {root['first_name']} chose to stay, so they are no longer linked and are matched on their own.")
        c.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('New', 'Proposed in group')", (r["friend_enquiry_id"],))
        c.commit()
        audit(c, staff, "bring_mate_declined_stay", "group", g["id"], f"friend {r['friend_enquiry_id']} declined; inviter {r['root_enquiry_id']} stays; friend unlinked")
        return {"ok": True, "outcome": "stayed", "new_group_id": None, "message": f"Declined. {root['first_name']} stays in the group; {friend['first_name']} is no longer linked to them and is matched on their own."}
    rep = _leave_with_friend(c, g, r["root_enquiry_id"], [r["friend_enquiry_id"]], staff, why=f"staff declined {friend['first_name']} joining, so {root['first_name']} left with them")
    return {"ok": True, "outcome": "left", "new_group_id": rep.get("new_group_id"), "message": f"Declined. {root['first_name']} and {friend['first_name']} left the group together and will be matched as a pair. {rep.get('message') or ''}".strip()}


def pending_mate_requests(c, group_id: int | None = None, dataset: str | None = None) -> list[dict]:
    where, args = ["r.status='pending'"], []
    if group_id is not None:
        where.append("r.group_id=?"); args.append(group_id)
    if dataset:
        where.append("r.is_test=?"); args.append(1 if dataset == "test" else 0)
    out = rows(c.execute(f"""SELECT r.*, f.first_name AS friend_name, f.last_name AS friend_last, e.first_name AS root_name FROM mate_requests r
                              JOIN enquiries f ON f.id=r.friend_enquiry_id JOIN enquiries e ON e.id=r.root_enquiry_id WHERE {' AND '.join(where)} ORDER BY r.id""", args))
    for o in out:
        ids = _lst_int(o.get("replace_json"))
        o["replace_ids"] = ids
        o["replace_names"] = [x["first_name"] for x in rows(c.execute(f"SELECT first_name FROM enquiries WHERE id IN ({','.join('?' * len(ids))})", ids))] if ids else []
    return out
