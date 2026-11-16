"""MateBot groups: the live final travel check (GP6(d), H28, M4, T4/T6; 16 November 2026).

Run on request for one group, on the page a staff member is looking at, and shown once. It connects three things that
already exist and are tested on their own: the journey provider adapter (routing.LiveEstimates), the MateBot travel
arithmetic (matebot_travel) and the AI's transient assessment (matebot_travel_reply).

What is measured, and for whom (H28 counts PEOPLE, not enquiries):
  * every person in the group is counted once: each member enquiry's lead and each further person in that enquiry's party,
    exactly as matching counts them (matching.unit_from_row).
  * a journey is measured only to a destination that was actually supplied and is unambiguous: the verified address of the
    institution a person who is studying named (M4, I9), or, for a person who is working or on a working holiday, the
    "Where will you work?" answer when the whole answer is one full street address (a street number, a street, a suburb
    and a postcode, and nothing else: see work_address), sent exactly as typed. A suburb, an employer's name, an
    address with other words, a phone number or a second street beside it, "Not yet known", a blank answer or an institution without a verified
    address is a destination that exists and cannot be measured: that person reads "travel time needs confirmation" and
    so does the group result (H28(d)/(h)); nothing is guessed, geocoded from a name or filled in. The enquirer's place of
    study or work is the enquirer's alone; another person in the party was never asked for one and has no destination, and
    neither has someone whose answer is "Other". The form has no second destination, so nobody is counted twice.
  * each person's journeys are requested only by the travel choices given for them (Walk, Bicycle, Public transport). The
    party's answers are the lead's (I16(d)). Someone with no answer has no journey requested and none selected.
  * the maximum travel time is each enquiry's own optional answer. When the times differ or one is missing the group
    result is "needs confirmation" (not ruled); no default, average, minimum or maximum is used.

Travel last (GP6(d)): the group's stored apartments are taken in their stored, non-travel order (the one currently offered
first); the first three are checked; one that fails is replaced by the next stored candidate, which is checked in its turn.
Nothing is searched for beyond the stored candidates, and never more than three are shown as the shortlist.

Nothing from the journey provider is written to the database, kept between requests or logged. The only record is a count
of provider requests, made before they leave, so the shared daily and monthly allowance (one key, both bots) holds. The
group's stored apartments, order, current offer, saved AI review and decline history are not changed by a check.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from . import homebot_shortlist as hs, homebot_travel as ht, matebot_travel as mt, matebot_travel_reply as mr, matching, routing
from .db import audit, j, pj, row, rows
from .institutions import find_institution

TRAVEL_CHECK_ACTION = hs.MATEBOT_TRAVEL_CHECK_ACTION
TRAVEL_NOT_SAVED = ("Journey times are checked live and are not saved: they are shown on this page only, are not part of the group's "
                    "stored apartments or its saved AI review, and are checked again each time.")
NO_DESTINATION_WHY = {"not_asked": "no place of study or work was given",
                      "party_member": "travelling with the enquirer; no place of work or study was asked for them"}
# a destination that exists and cannot be measured: the person, and so the group, needs confirmation
UNMEASURABLE_WHY = {"no_institution": "the place of study is not one with a verified address on file",
                    "work_missing": "no place of work has been given",
                    "work_not_address": "the place of work given is not one full street address with its suburb and postcode, so no journey can be measured to it"}
WORKING = ("Working", "Working holiday")
WORK_ADDRESS_MAX = 160
_TYPES = (r"street|st|road|rd|avenue|ave|av|lane|ln|drive|dr|place|pl|parade|pde|highway|hwy|boulevard|blvd|court|ct|"
          r"crescent|cres|terrace|tce|way|square|sq|quay|esplanade|promenade|close|grove|walk|circuit|parkway")
_STATES = (r"vic|victoria|nsw|new\s+south\s+wales|qld|queensland|sa|south\s+australia|wa|western\s+australia|tas|tasmania|"
           r"nt|northern\s+territory|act|australian\s+capital\s+territory")
# words that make the answer a sentence, a second place or an employer rather than one address
_NOT_ADDRESS_WORDS = (r"in|on|at|near|by|of|and|or|from|to|for|the\s+corner|corner|cnr|opposite|opp|behind|next|then|also|plus|"
                      r"jobs?|days?|hours?|hrs?|minutes?|mins?|km|kms|weeks?|months?|shifts?|nights?|eleven|call|phone|mobile|tel|"
                      r"australia|territory")
_WORD = r"[A-Za-z][A-Za-z'-]*"
_PLAIN = rf"\b(?!(?:{_TYPES}|{_STATES}|{_NOT_ADDRESS_WORDS})\b){_WORD}"
# words that are a kind of business or place rather than part of a suburb's name. They are fine in a street name
# ("Station Street", "Centre Road"), so they are refused only where the suburb is written. This list is a help, not a proof.
_NOT_SUBURB_WORDS = (r"cafe|coffee|pizza|restaurant|bistro|kitchen|bakery|bar|pub|hotel|motel|gym|fitness|salon|shop|store|"
                     r"supermarket|market|mall|plaza|centre|center|office|offices|tower|building|warehouse|factory|depot|"
                     r"hospital|clinic|pharmacy|chemist|university|uni|school|college|campus|station|airport|"
                     r"pty|ltd|inc|co|company|group|services|westfield|coles|woolworths|aldi|kmart|bunnings|mcdonalds")
_LOCAL = rf"(?!(?:{_NOT_SUBURB_WORDS})\b){_PLAIN}"
# One address and nothing else, start to end: an optional unit or level, a street number, a street name, its type, a
# suburb, an optional state, and a postcode. Nothing may come before it or after it.
_FULL_ADDRESS = re.compile(
    rf"^(?:(?:unit|level|lvl|suite|shop|floor|apartment|apt)\s*\d{{1,4}}[A-Za-z]?\s*[,/]?\s*)?"
    rf"[1-9]\d{{0,4}}[A-Za-z]?(?:\s*[-/]\s*[1-9]\d{{0,4}}[A-Za-z]?)?\s+"
    rf"(?:(?:St|Mt)\b\.?\s+)?(?:{_PLAIN}\s+){{1,3}}"
    rf"(?:{_TYPES})\b\.?(?:\s+(?:north|south|east|west)\b)?\s*,?\s*"
    rf"(?:(?:St|Mt|North|South|East|West|Port|Mount|Point|Glen|Box|Upper|Lower)\b\.?\s+)?{_LOCAL}(?:\s+{_LOCAL})?\b"
    rf"(?:\s+(?:north|south|east|west|upper|lower)\b)?\s*,?\s*"
    rf"(?:(?P<state>{_STATES})\b\s*,?\s*)?\b(?P<postcode>\d{{4}})"
    rf"(?:\s*,?\s*Australia)?$", re.I)
# the postcodes used for street addresses in each state (post-office-box and bulk-mail ranges are not street addresses)
_STREET_POSTCODES = {"vic": ("3",), "nsw": ("2",), "act": ("26", "29", "02"), "qld": ("4",), "sa": ("5",), "wa": ("6",), "tas": ("7",), "nt": ("08",)}
_STATE_KEY = {"victoria": "vic", "new south wales": "nsw", "queensland": "qld", "south australia": "sa", "western australia": "wa",
              "tasmania": "tas", "northern territory": "nt", "australian capital territory": "act"}


def _postcode_fits(state: Optional[str], postcode: str) -> bool:
    """A postcode in a street-address range; and, when a state was written, in that state's range."""
    if state:
        key = " ".join(state.lower().split())
        return postcode.startswith(_STREET_POSTCODES[_STATE_KEY.get(key, key)])
    return any(postcode.startswith(starts) for starts in _STREET_POSTCODES.values())


def work_address(answer) -> Optional[str]:
    """The "Where will you work?" answer when it is one full street address and nothing else; otherwise None.

    Full means, from start to end: an optional unit or level, a street number, a street name with its type, a suburb, an
    optional state, and a postcode that is a street-address postcode (and belongs to the state when one is written). The
    whole answer must be that address: words before or after it, a phone number, an email address, a second street or a
    second address, and an address without a postcode are all refused. What is returned is exactly the answer as typed
    (spaces tidied), so nothing is completed, corrected or added. The stored answer is never changed. A refused answer is
    not a failure: the person reads "travel time needs confirmation" and no request is made for them.

    Limit, stated plainly: there is no list of suburbs here, so the one or two words where the suburb is written are not
    checked to be a real suburb, nor the postcode to be that suburb's. Common business words there are refused
    (_NOT_SUBURB_WORDS), but another business name written in place of the suburb ("12 Smith Street Acme 3121") would be
    accepted and sent with its street and postcode. The number, the street and the postcode are what place the address."""
    if not isinstance(answer, str):
        return None
    text = " ".join(answer.split())
    if not text or len(text) > WORK_ADDRESS_MAX:
        return None
    m = _FULL_ADDRESS.match(text)
    return text if m and _postcode_fits(m.group("state"), m.group("postcode")) else None
OPEN_POINT_PRACTICAL = "an essential practical need recorded for a member is not confirmed for this apartment"


REASON_PLAIN = {mt.TRAVEL_LIMIT_MISSING: "a member has not stated a maximum travel time",
                mt.TRAVEL_LIMITS_DIFFER: "the members' maximum travel times differ",
                mt.TRAVEL_MODE_NOT_ANSWERED: "a member has not said how they would travel, so no journey is selected for them",
                ht.TRAVEL_NEEDS_CONFIRMATION_INCOMPLETE_ESTIMATES: "a journey could not be measured",
                ht.FASTEST_MODE_UNCONFIRMED: "a member's fastest accepted way of travelling is not confirmed",
                ht.LONG_JOURNEY_WARNING: "a member has a long journey, over double the stated time"}
OWN_PLAIN = {mt.WITHIN_OWN_LIMIT: "within their own stated time ({t})", mt.OVER_OWN_LIMIT: "over their own stated time ({t})",
             mt.NOT_MEASURED: "not known against their own stated time ({t})", mt.NO_STATED_LIMIT: "no maximum travel time stated"}


def _result_plain(travel: dict) -> str:
    f = travel["facts"]
    m = f.get("majority") or {}
    if travel["status"] == mt.PASS:
        return (f"Within the doubled limit: the closest {m.get('size')} of the people with a known destination average "
                f"{m.get('average_display')} minutes (rounded) against {f['ceiling_minutes']} minutes, twice the stated {f['stated_limit_minutes']}.")
    if travel["status"] == mt.EXCEEDS_CEILING:
        return (f"Over the doubled limit: the closest {m.get('size')} of the people with a known destination average "
                f"{m.get('average_display')} minutes (rounded) against {f['ceiling_minutes']} minutes, twice the stated {f['stated_limit_minutes']}.")
    if travel["status"] == mt.NO_DESTINATIONS:
        return "No travel result: no member has a place of study or work recorded."
    return "Travel time needs confirmation. This is not a pass and not a failure."


def _journey_lines(travel: dict, people: list) -> list:
    """One plain sentence per person for the page. Written here; no code names."""
    by_pos = {p["position"]: p for p in people}
    lines = []
    for f in travel["facts"]["people"]:
        p = by_pos[f["index"]]
        who = f"Person {f['index']} ({p['name']})" if p.get("name") else f"Person {f['index']}"
        own = OWN_PLAIN[f["own_limit"]].format(t=p["stated_time"] or "")
        cannot = UNMEASURABLE_WHY.get(p["unmeasurable_why"])
        if f["status"] == mt.NO_DESTINATION:
            text = "no journey measured: " + NO_DESTINATION_WHY[p["no_destination_why"]]
        elif f["status"] == mt.NO_MODE_ANSWER:
            text = ("travel time needs confirmation: how they would travel has not been answered, so no journey is selected"
                    + (f"; and {cannot}" if cannot else ""))
        elif f["status"] == mt.MEASURED:
            text = f"{mr.tr._shown(f['fastest_minutes'])} minutes (rounded) by {' / '.join(f['fastest_modes'])}; {own}"
        elif f["status"] == mt.PARTIAL:
            text = (f"at most {mr.tr._shown(f['bound_minutes'])} minutes (rounded) by {' / '.join(f['best_known_modes'])}; "
                    f"{' / '.join(f['unknown_modes'])} could not be measured, so the fastest is not confirmed; {own}")
        else:
            text = ("travel time needs confirmation: " + (cannot or "no journey could be measured")
                    + ("" if f["own_limit"] == mt.NO_STATED_LIMIT else "; " + own))
        if f.get("warning"):
            text += "; long journey, discuss with the group"
        if f.get("in_majority"):
            text += " (one of the closest majority)"
        lines.append(f"{who}: {text}")
    return lines


def state(is_test: bool = False) -> dict:
    """For the group page: whether a live travel check can be run, and whether the page should run one when it opens.
    A test group gets neither unless a controlled test is switched on (routing.live_test_data)."""
    on = routing.live_enabled()
    if on and is_test and not routing.live_test_data():
        return {"enabled": False, "auto": False, "plain": routing.LIVE_TEST_OFF, "ai_reply": False}
    return {"enabled": on, "auto": on and routing.live_auto(), "plain": TRAVEL_NOT_SAVED if on else routing.LIVE_OFF,
            "ai_reply": on and mr.tr.enabled()}


def _refusal(state_: str, plain: str, **extra) -> dict:
    return {"ok": False, "state": state_, "saved": False, "plain": plain, **extra}


def _destination(e: dict) -> tuple:
    """(kind, address, shown as, why not) for the enquirer. kind is "study", "work" or None (no destination at all).
    With a kind and no address the destination exists and cannot be measured; `why not` says why."""
    doing = e.get("doing") or ""
    if doing == "Studying":
        campus = find_institution(e.get("school") or "")
        if not campus or not campus.get("verified") or not (campus.get("address") or "").strip():
            return "study", None, None, "no_institution"
        return "study", campus["address"].strip(), campus["name"], None
    if doing in WORKING:
        given = e.get("work")
        if not isinstance(given, str) or not given.strip():
            return "work", None, None, "work_missing"
        address = work_address(given)
        return ("work", address, "the work address given on the form", None) if address else ("work", None, None, "work_not_address")
    return None, None, None, "not_asked"


def _modes(e: dict) -> tuple:
    """The travel choices recorded for this enquiry, the form's three only. Anything else stored there is not used."""
    raw = e.get("travel_modes")
    value = pj(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        return ()
    return tuple(m for m in mt.MODES if m in value)


def group_people(c, gid: int) -> list:
    """Every person in the group, once, in the order the group page lists its members: the enquirer, then the others in
    that enquirer's party. Positions start at 1."""
    people = []
    members = rows(c.execute("SELECT e.* FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? ORDER BY gm.room, gm.id", (gid,)))
    for e in members:
        party = rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))
        unit = matching.unit_from_row(e, party)
        stated = e.get("travel") if isinstance(e.get("travel"), str) and e.get("travel") in mt.STATED_LIMIT else None
        modes = _modes(e)
        kind, address, shown, why = _destination(e)
        for n, person in enumerate(unit.people):
            lead = n == 0
            has = lead and kind is not None
            people.append({"position": len(people) + 1, "enquiry_id": e["id"], "lead": lead, "name": person.name,
                           "stated_time": stated, "modes": modes,   # I16(d): the enquirer's answers are the party's
                           "destination_kind": kind if lead else None, "destination": address if lead else None,
                           "destination_shown": shown if lead else None,
                           "no_destination_why": None if has else (why if lead else "party_member"),
                           "unmeasurable_why": why if (has and not address) else None})
    return people


class GroupEstimates(routing.LiveEstimates):
    """The journey provider adapter for a group whose people each have their own destination and travel choices. Requests,
    limits, deadline, attribution and the no-storage rule are LiveEstimates' own; only which journeys are wanted differs."""

    def __init__(self, people, inventory, *, max_requests, now=None, post=None):
        super().__init__(SimpleNamespace(people=[], transport=()), inventory, max_requests=max_requests, now=now, post=post)
        self.group = [p for p in people if p["destination"] and p["modes"]]

    def _keys(self, aid):
        apartment = self.inventory.get(int(aid))
        origin = self._origin(apartment) if apartment else None
        if not origin:
            return
        for p in self.group:
            for mode in p["modes"]:
                yield (origin, p["destination"], routing.AGENT_TOOLS_MODES[mode])

    def journeys(self, aid, person) -> Optional[dict]:
        """{choice: minutes | UNMEASURED} over the person's own choices; {} when they gave none; None with no destination.
        A destination that exists and cannot be measured gives UNMEASURED for every choice: never a pass, never left out."""
        if not person["destination_kind"]:
            return None
        if not person["destination"]:
            return {mode: ht.UNMEASURED for mode in person["modes"]}
        apartment = self.inventory.get(int(aid))
        origin = self._origin(apartment) if apartment else None
        return {mode: (self.cache.get((origin, person["destination"], routing.AGENT_TOOLS_MODES[mode]), ht.UNMEASURED) if origin else ht.UNMEASURED)
                for mode in person["modes"]}

    def __call__(self, aid, position, which):
        raise RuntimeError("group estimates are read per person with journeys()")


def _is_test(c, gid: int) -> bool:
    """Read from the records, not from the view passed in: any test member makes the group a test group."""
    return bool(row(c.execute("SELECT 1 AS t FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? AND e.is_test=1 LIMIT 1", (gid,))))


def _signature(c, gid: int) -> tuple:
    g = row(c.execute("SELECT status, apartment_id FROM groups WHERE id=?", (gid,))) or {}
    apts = rows(c.execute("SELECT apartment_id, rank FROM group_apartments WHERE group_id=? ORDER BY rank, apartment_id", (gid,)))
    members = rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))
    return (g.get("status"), g.get("apartment_id"), tuple((a["apartment_id"], a["rank"]) for a in apts), tuple(m["enquiry_id"] for m in members))


def _person_out(p: dict) -> dict:
    return {"position": p["position"], "name": p["name"], "enquiry_id": p["enquiry_id"], "lead": p["lead"],
            "stated_time": p["stated_time"], "travel_choices": list(p["modes"]),
            "destination_kind": p["destination_kind"], "destination_shown": p["destination_shown"],
            "needs_confirmation": UNMEASURABLE_WHY.get(p["unmeasurable_why"]),
            "no_destination": NO_DESTINATION_WHY.get(p["no_destination_why"])}


def travel_check(c, group: dict, staff: str, *, now: Optional[datetime] = None, post=None) -> dict:
    """One live travel check for a group. `group` is the staff view of the group (app._group_out): its stored apartments in
    order, with the role, current-offer, leased, declined and practical-need facts the page already shows."""
    gid = int(group["id"])
    if not routing.live_enabled():
        return _refusal("off", routing.LIVE_OFF)
    if not routing.live_allowed_for(_is_test(c, gid)):
        return _refusal("test_data", routing.LIVE_TEST_OFF)   # before the lock, the allowance and any request
    if group.get("status") in ("Leased", "Dissolved"):
        return _refusal("closed", "This group is closed, so travel was not checked.")
    apts = [a for a in group.get("apartments") or [] if a.get("shortlist_role") != mt.ROLE_UNAVAILABLE]
    if not apts:
        return _refusal("no_options", "There are no apartments to check travel for yet.")
    # the stored, non-travel order, with the apartment currently offered first (it always holds a place)
    order = [a for a in apts if a.get("is_current")] + [a for a in apts if not a.get("is_current")]
    by_id = {int(a["apartment_id"]): a for a in order}
    ids = list(by_id)
    first_three = set(ids[:mt.MAX_SHORTLIST])
    people = group_people(c, gid)
    if not (ht.MIN_PEOPLE <= len(people) <= ht.MAX_PEOPLE):
        return _refusal("size", "Travel was not checked: the group does not have between one and seven people.")
    when = (now or datetime.now(timezone.utc))
    when = (when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when).astimezone(timezone.utc)
    if not hs._travel_lock.acquire(timeout=10):
        return _refusal("busy", "Another travel check is running. Please try again in a moment.")
    try:
        before = _signature(c, gid)
        inv = rows(c.execute(f"SELECT id, address, suburb FROM apartments WHERE id IN ({','.join('?' * len(ids))})", ids))
        used_day = hs._travel_requests_used(c, when.strftime("%Y-%m-%d"))
        used_month = hs._travel_requests_used(c, when.strftime("%Y-%m"))
        allowance = min(routing.live_limit("MMAI_ROUTING_LIVE_MAX_REQUESTS"),
                        routing.live_limit("MMAI_ROUTING_LIVE_DAILY_REQUESTS") - used_day,
                        routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") - used_month)
        if allowance <= 0:
            return _refusal("limit", "The travel-check allowance for " + ("today" if routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") > used_month else "this month")
                            + " has been used, so travel was not checked. Travel time needs confirmation.")
        est = GroupEstimates(people, inv, max_requests=allowance, now=when, post=post)
        who = str(staff or "").strip()[:80] or "staff"

        def record(requests: int) -> None:
            # BEFORE the first request of each stage leaves. A count of our own requests and nothing else.
            audit(c, who, TRAVEL_CHECK_ACTION, "group", gid, j({"requests": requests, "not_requested": est.skipped}))
        est.before_send = record

        def assess(aid):
            est.prepare([aid])   # a replacement is measured only when it is reached; the first three were measured together
            return mt.assess_group([mt.Member(p["position"], p["stated_time"], p["modes"], est.journeys(aid, p)) for p in people])
        est.prepare(ids[:mt.MAX_SHORTLIST])
        res = mt.travel_last(ids, assess)
        check = est.display()
        if _signature(c, gid) != before:
            return _refusal("replaced", "The group changed while travel was being checked. Reload the group and check travel again.")
    finally:
        hs._travel_lock.release()

    def option(aid, r, role):
        a, travel = by_id[aid], r.as_dict()
        return {"apartment_id": aid, "address": a.get("address"), "suburb": a.get("suburb"), "type_label": a.get("type_label"),
                "total_weekly": a.get("total_weekly"), "is_current": bool(a.get("is_current")), "role": role,
                "replacement": role == "shortlist" and aid not in first_three,
                "practical_need_hold": bool(a.get("practical_need_hold")), "practical_need_reason": a.get("practical_need_reason"),
                "travel": travel, "result": travel["status"], "result_plain": _result_plain(travel),
                "reasons_plain": [REASON_PLAIN[x] for x in travel["reasons"] if x in REASON_PLAIN],
                "journey_lines": _journey_lines(travel, people),
                "measured": any(x["status"] in (mt.MEASURED, mt.PARTIAL) for x in travel["facts"]["people"]),
                "travel_sources": est.sources_for(aid)}
    shortlist = [option(aid, r, "shortlist") for aid, r in res["shortlist"]]
    failed = [option(aid, r, "failed") for aid, r in res["failed"]]
    # the AI's transient assessment of the shortlisted apartments: outside the lock and any database work; never stored
    reply = mr.travel_reply([{"apartment_id": x["apartment_id"], "travel": x["travel"],
                              "apartment": {"total_weekly_rent": x["total_weekly"], "replacement": x["replacement"],
                                            "open_points": [OPEN_POINT_PRACTICAL] if x["practical_need_hold"] else []}} for x in shortlist])
    snapshot = pj((row(c.execute("SELECT evidence_json FROM groups WHERE id=?", (gid,))) or {}).get("evidence_json"), {}) or {}
    reviewed = {int(a["apartment_id"]) for a in snapshot.get("apartments") or [] if isinstance(a, dict) and a.get("apartment_id") is not None}
    ai_gate = mr.gate(reply, bool(group.get("ready")), [x["apartment_id"] for x in shortlist], reviewed)
    notes = {o["apartment_id"]: o for o in reply.get("options") or []} if reply.get("state") == "ready" else {}
    for x in shortlist:
        x["ai_travel"] = notes.get(x["apartment_id"])
    unruled = any(x["travel"]["facts"].get("unruled") for x in shortlist + failed)
    return {"ok": True, "state": "checked", "saved": False, "plain": TRAVEL_NOT_SAVED, "group_id": gid, "check": check,
            "people": [_person_out(p) for p in people], "shortlist_max": mt.MAX_SHORTLIST,
            "ai_reply": reply, "ai_gate": ai_gate, "shortlist": shortlist, "failed": failed,
            "not_checked": [{"apartment_id": aid, "address": by_id[aid].get("address")} for aid in res["not_checked"]],
            "unruled_note": mt.UNRULED_NOTE if unruled else None}
