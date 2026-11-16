"""MateBot groups: the live final travel check (GP6(d), H28, M4, T4/T6; 16 Nov 2026).

Synthetic journey replies and a stand-in for the model; no network. Covers: off by default; PEOPLE are counted, not
enquiries, each once, with only the destination that was actually supplied (a verified place of study) and only the travel
choices given; travel last for at most three, a failure replaced by the next stored candidate; differing or missing stated
times are never combined; the request allowance is shared with HomeBot and recorded as a count before anything leaves;
nothing from the journey provider or the model is stored; and a shortlist is "AI-assessed" only when the saved review is
current, covered every apartment shown and this check's assessment was accepted.
"""
import json
from pathlib import Path

import pytest

from server import ai, app, homebot_shortlist as hs, homebot_travel_reply as tr, matching, matebot_live as ml, matebot_travel as mt
from server import matebot_travel_reply as mr, routing
from server.db import j, row, rows
from tests.test_data_and_api import client  # noqa: F401  (fixture)
from tests.test_live_travel import NOW, SOURCE, _live_env
from tests.test_matebot_shortlist_three import _six, _view
from tests.test_matebot_travel_reply import Model

STAFF_JS = (Path(__file__).resolve().parents[1] / "client" / "staff.js").read_text(encoding="utf-8")
W, B, PT = mt.MODES
RMIT = "RMIT University – Melbourne City"
WHEN = __import__("datetime").datetime.fromisoformat(NOW)
WORK = "100 Collins Street, Melbourne VIC 3000"


class Provider:
    """Stand-in for the journey service: records every request and answers by building and travel mode."""

    def __init__(self, minutes=20, slow=(), fail=False, during=None):
        self.minutes, self.slow, self.fail, self.during, self.bodies = minutes, dict(slow), fail, during, []

    def __call__(self, body):
        self.bodies.append(body)
        if self.during:
            self.during()
        if self.fail:
            raise RuntimeError("provider said: 12 Secret Street is unreachable")
        origin = body["origin"]["address"]
        minutes = next((v for k, v in self.slow.items() if origin.startswith(k)), self.minutes)
        return {"routes": [{"duration": f"{int(minutes * 60)}s", "distanceMeters": 4321, "attribution": dict(SOURCE)}]}

    def origins(self):
        return [b["origin"]["address"].split(",")[0] for b in self.bodies]


def _members(db, gid):
    return [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY room, id", (gid,)))]


def _answer(db, gid, travel="30 minutes", modes=(PT,), only=None):
    for eid in _members(db, gid) if only is None else only:
        db.execute("UPDATE enquiries SET travel=?, travel_modes=? WHERE id=?", (travel, j(list(modes)) if modes else None, eid))
    db.commit()


def _school(db, gid, name=RMIT):
    """The shared fixture's place of study is not one of the institutions on file; these people study at one that is."""
    db.execute(f"UPDATE enquiries SET school=? WHERE id IN ({','.join('?' * len(_members(db, gid)))})", [name, *_members(db, gid)])
    db.commit()


def _audits(db):
    return rows(db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (ml.TRAVEL_CHECK_ACTION,)))


def _snapshot(db):
    names = [r["name"] for r in rows(db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))]
    return {n: json.dumps(rows(db.execute(f"SELECT * FROM {n} ORDER BY 1")), sort_keys=True, default=str) for n in names if n != "audit_log"}


@pytest.fixture
def group(db, monkeypatch):
    gid, ids = _six(db, monkeypatch)
    _live_env(monkeypatch)
    _school(db, gid)
    # the shared fixture makes its people as test records; these checks are about real records, which is what the provider
    # may be asked about. Test records have their own test below.
    db.execute(f"UPDATE enquiries SET is_test=0 WHERE id IN ({','.join('?' * len(_members(db, gid)))})", _members(db, gid))
    db.commit()
    return db, gid, ids


def _check(db, gid, provider, **over):
    return ml.travel_check(db, dict(_view(db, gid), **over), "anna", now=WHEN, post=provider)


# ----------------------------------------------------------------------------------------------------------- off
def test_off_by_default_nothing_is_requested_or_recorded(db, monkeypatch):
    gid, ids = _six(db, monkeypatch)
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    _school(db, gid)
    _answer(db, gid)
    p = Provider()
    out = _view(db, gid)
    assert out["travel_check"] == "off" and out["travel_live"] == {"enabled": False, "auto": False, "plain": routing.LIVE_OFF, "ai_reply": False}
    r = ml.travel_check(db, out, "anna", post=p)
    assert r == {"ok": False, "state": "off", "saved": False, "plain": routing.LIVE_OFF} and p.bodies == [] and _audits(db) == []


def test_the_view_says_when_a_check_can_be_run(group, monkeypatch):
    db, gid, ids = group
    out = _view(db, gid)
    assert out["travel_check"] == "live" and out["travel_live"]["enabled"] is True and out["travel_live"]["plain"] == ml.TRAVEL_NOT_SAVED
    assert out["travel_live"]["auto"] is routing.live_auto() and out["travel_live"]["ai_reply"] is False
    monkeypatch.setenv("MMAI_ROUTING_LIVE_AUTO", "0")
    assert _view(db, gid)["travel_live"]["auto"] is False


# ------------------------------------------------------------------------------------------------- people, not enquiries
def test_every_person_is_counted_once_with_only_what_was_supplied(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    people = ml.group_people(db, gid)
    assert [(p["position"], p["enquiry_id"], p["lead"], p["destination_kind"], p["destination_shown"], p["modes"], p["stated_time"]) for p in people] == [
        (1, a, True, "study", RMIT, (), None), (2, b, True, "study", RMIT, (), None)]     # nothing answered: nothing assumed
    assert all(p["destination"] and p["no_destination_why"] is None and p["unmeasurable_why"] is None for p in people)
    # a couple: the second person is counted, shares the enquirer's time and choices, and has no destination of their own
    db.execute("UPDATE enquiries SET party='Couple', party_size=2, travel='45 minutes', travel_modes=? WHERE id=?", (j([W, PT]), a))
    db.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?, 'Pat Partner', 'pat@example.com')", (a,))
    # someone working whose answer is a suburb: a destination that exists and cannot be measured. Not left out, not guessed
    db.execute("UPDATE enquiries SET doing='Working', work='Richmond', school=NULL, travel='15 minutes', travel_modes=? WHERE id=?", (j([B]), b))
    db.commit()
    people = ml.group_people(db, gid)
    assert [(p["position"], p["enquiry_id"], p["lead"], p["name"]) for p in people][1] == (2, a, False, "Pat Partner")
    assert [(p["stated_time"], p["modes"], p["destination_kind"], bool(p["destination"]), p["no_destination_why"], p["unmeasurable_why"]) for p in people] == [
        ("45 minutes", (W, PT), "study", True, None, None), ("45 minutes", (W, PT), None, False, "party_member", None),
        ("15 minutes", (B,), "work", False, None, "work_not_address")]

    def third(**answers):
        db.execute(f"UPDATE enquiries SET {', '.join(k + '=?' for k in answers)} WHERE id=?", [*answers.values(), b])
        db.commit()
        q = ml.group_people(db, gid)[2]
        return q["destination_kind"], q["destination"], q["no_destination_why"], q["unmeasurable_why"]
    # a full street address given as the place of work is used as typed; for a working holiday too
    assert third(work="  100 Collins Street,   Melbourne  VIC 3000 ") == ("work", WORK, None, None)
    assert db.execute("SELECT work FROM enquiries WHERE id=?", (b,)).fetchone()[0] == "  100 Collins Street,   Melbourne  VIC 3000 "   # the answer itself is untouched
    assert third(doing="Working holiday") == ("work", WORK, None, None)
    assert ml.group_people(db, gid)[2]["destination_shown"] == "the work address given on the form"
    # no answer, "Not yet known", an employer's name and a street without a suburb all need confirmation; none is completed
    assert third(work=None) == ("work", None, None, "work_missing") and third(work="   ") == ("work", None, None, "work_missing")
    for vague in ("Not yet known", "Coles", "Coles Richmond", "200 Queen St", "CBD", "3 days at Chapel Street South Yarra", "100 Collins Street, Melbourne"):
        assert third(work=vague) == ("work", None, None, "work_not_address"), vague
    # "Other": no place of study or work was asked for, so there is no destination at all
    assert third(doing="Other", work=WORK) == (None, None, "not_asked", None)
    # a place of study that is not one with a verified address on file is not guessed at: it needs confirmation
    assert third(doing="Studying", school="Somewhere Else College") == ("study", None, None, "no_institution")
    assert [(p["no_destination_why"], p["unmeasurable_why"]) for p in ml.group_people(db, gid)] == [(None, None), ("party_member", None), (None, "no_institution")]
    # an answer outside the form's three choices is not used, and an unreadable one is no answer
    db.execute("UPDATE enquiries SET travel_modes=?, travel='about an hour' WHERE id=?", (j(["Car", "Tram", PT]), a))
    db.execute("UPDATE enquiries SET travel_modes='not json' WHERE id=?", (b,))
    db.commit()
    people = ml.group_people(db, gid)
    assert people[0]["modes"] == (PT,) and people[0]["stated_time"] is None and people[2]["modes"] == ()


def test_a_work_answer_that_is_not_an_address_is_never_measured_and_never_left_out(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid, modes=(W, PT), only=[a])
    db.execute("UPDATE enquiries SET doing='Working', work='Richmond', travel='30 minutes', travel_modes=? WHERE id=?", (j([B]), b))
    db.commit()
    p = Provider()
    r = _check(db, gid, p)
    assert r["ok"] and sorted({x["travelMode"] for x in p.bodies}) == ["TRANSIT", "WALK"]            # never the other person's bicycle
    assert len(p.bodies) == 6 and len({x["destination"]["address"] for x in p.bodies}) == 1           # 3 apartments x 2 choices, one destination
    assert all("Richmond" not in json.dumps(x) for x in p.bodies)                                     # the suburb is never sent
    first = r["shortlist"][0]
    # the person who works is counted as someone with a destination whose journey is not known: no pass on the other alone
    assert first["result"] == mt.NEEDS_CONFIRMATION and first["travel"]["facts"]["known_destinations"] == 2
    assert first["travel"]["facts"]["no_destination"] == [] and first["travel"]["facts"]["unmeasured"] == [2]
    assert first["journey_lines"][1].startswith("Person 2") and "travel time needs confirmation: the place of work given is not one full street address with its suburb and state or postcode" in first["journey_lines"][1]
    assert [x["apartment_id"] for x in r["shortlist"]] == ids[:3] and r["failed"] == []               # kept in place, not failed
    assert [(q["destination_kind"], q["needs_confirmation"], q["no_destination"]) for q in r["people"]] == [
        ("study", None, None), ("work", ml.UNMEASURABLE_WHY["work_not_address"], None)]


@pytest.mark.parametrize("typed", [
    "100 Collins Street, Melbourne VIC 3000", "100 Collins St Melbourne 3000", "100 Collins Street, Melbourne, Victoria",
    "Level 3, 100 Collins Street, Melbourne VIC 3000", "3/100 Collins St, Melbourne VIC", "12 Acland Street, St Kilda VIC 3182",
    "450 St Kilda Road, Melbourne VIC 3004", "1-5 Swanston St, Carlton 3053", "22 Chapel Street South Yarra VIC 3141",
    "5 Toorak Rd, South Yarra, VIC, 3141, Australia", "300 Burwood Hwy, Burwood East VIC 3151", "12 The Avenue, Parkville VIC 3052",
    "100 collins street melbourne vic 3000", "  100   Collins Street,\n Melbourne   VIC 3000 "])
def test_one_full_street_address_is_used_exactly_as_typed(typed):
    assert ml.work_address(typed) == " ".join(typed.split())                             # spaces tidied, nothing else changed


@pytest.mark.parametrize("typed", [
    # no state or postcode, or not a street address at all: nothing is completed
    "100 Collins Street, Melbourne", "100 Collins Street", "200 Queen St", "Collins Street, Melbourne VIC 3000", "Melbourne VIC 3000",
    "Richmond", "CBD", "Coles", "Coles Richmond", "Not yet known", "", "   ", None, 12, "12 Smith Richmond VIC 3121",
    "100 Collins Street, Victoria 3000", "100 Collins Street, South Australia 5000", "100 Collins Street, Australia 3000", "100 Collins Street, Melbourne 30000",
    # words before or after the address, or a business name with it
    "Cafe at 12 Smith Street, Richmond VIC 3121", "Cafe at 12 Smith Street, near the station and Richmond VIC 3121", "Coles, 12 Smith Street, Richmond VIC 3121",
    "12 Smith Street, Richmond VIC 3121 (Coles)", "12 Smith Street near Richmond station VIC", "3 days at 22 Chapel Street South Yarra VIC 3141",
    "2 jobs in Smith Road Richmond VIC 3121", "100 Collins Street Melbourne VIC 3000 Australia please", "7 Eleven Walk Brunswick VIC 3056", "7 Eleven Walk Brunswick",
    # a phone number, an email address
    "call me 0412 345 678 Smith St Richmond", "0412 345 678 Smith St Richmond VIC 3121", "12 Smith Street, Richmond VIC 3121 0412345678",
    "12 Smith Street\nRichmond VIC 3121\nphone 0400 000 000", "12 Smith St, Richmond VIC 3121 anna@example.com",
    # a second street or a second address
    "2 Smith Road and 3 Brown Road Richmond", "2 Smith Road and 3 Brown Road Richmond VIC 3121", "2 Smith Road Brown Road, Richmond VIC 3121",
    "2 Smith Road, 3 Brown Road, Richmond VIC 3121", "100 Collins Street, Melbourne VIC 3000; 5 Queen St Melbourne 3000", "12 Smith Street, Richmond VIC 3121 or Coles Kew",
    "12 Smith Street Richmond VIC 3121 3122", "x" * 200])
def test_anything_but_one_full_street_address_is_refused_not_trimmed_or_completed(typed):
    assert ml.work_address(typed) is None


def test_a_refused_work_answer_sends_nothing_for_that_person(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)
    typed = "Cafe at 12 Smith Street, Richmond VIC 3121, call 0412 345 678"
    db.execute("UPDATE enquiries SET doing='Working', work=? WHERE id=?", (typed, b))
    db.commit()
    p = Provider()
    r = _check(db, gid, p)
    sent = json.dumps(p.bodies)
    assert len(p.bodies) == 3 and "Smith" not in sent and "0412" not in sent and "Cafe" not in sent      # only the other person's journeys
    assert r["shortlist"][0]["result"] == mt.NEEDS_CONFIRMATION and r["people"][1]["needs_confirmation"] == ml.UNMEASURABLE_WHY["work_not_address"]
    assert db.execute("SELECT work FROM enquiries WHERE id=?", (b,)).fetchone()[0] == typed              # kept as the customer wrote it


def test_a_full_work_address_is_measured_by_that_persons_own_choices(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid, modes=(W, PT), only=[a])
    db.execute("UPDATE enquiries SET doing='Working', work=?, travel='30 minutes', travel_modes=? WHERE id=?", (WORK, j([B]), b))
    db.commit()
    p = Provider(minutes=20)
    r = _check(db, gid, p)
    assert db.execute("SELECT work FROM enquiries WHERE id=?", (b,)).fetchone()[0] == WORK
    asked = sorted({(x["destination"]["address"] == WORK, x["travelMode"]) for x in p.bodies})
    assert asked == [(False, "TRANSIT"), (False, "WALK"), (True, "BICYCLE")] and len(p.bodies) == 9   # 3 apartments x (2 + 1)
    first = r["shortlist"][0]
    assert first["result"] == mt.PASS and first["travel"]["facts"]["known_destinations"] == 2 and first["travel"]["facts"]["measured"] == [1, 2]
    assert "by Bicycle" in first["journey_lines"][1] and r["people"][1]["destination_shown"] == "the work address given on the form"
    assert "Collins" not in json.dumps(r["ai_reply"]) and "Collins" not in json.dumps(_audits(db))    # the address goes to the journey service only


def test_someone_whose_answer_is_other_has_no_destination_and_is_not_counted(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)
    db.execute("UPDATE enquiries SET doing='Other', work=NULL WHERE id=?", (b,))
    db.commit()
    r = _check(db, gid, Provider(minutes=20))
    first = r["shortlist"][0]
    assert first["result"] == mt.PASS and first["travel"]["facts"]["known_destinations"] == 1 and first["travel"]["facts"]["no_destination"] == [2]
    assert "no journey measured: no place of study or work was given" in first["journey_lines"][1]


def test_a_place_of_study_that_is_not_on_file_is_never_guessed_at(group):
    db, gid, ids = group
    _school(db, gid, "RMIT University – City campus")                                    # close to a name on file, and still not it
    _answer(db, gid)
    p = Provider()
    r = _check(db, gid, p)
    assert p.bodies == [] and _audits(db) == []
    assert [q["needs_confirmation"] for q in r["people"]] == [ml.UNMEASURABLE_WHY["no_institution"]] * 2
    x = r["shortlist"][0]
    assert x["result"] == mt.NEEDS_CONFIRMATION and x["result_plain"].startswith("Travel time needs confirmation") and x["measured"] is False
    assert "the place of study is not one with a verified address on file" in x["journey_lines"][0]
    assert r["ai_reply"]["state"] == "off" and r["ai_gate"]["held"] is True
    # nobody with a place of study or work at all: no travel result, and still nothing requested
    db.execute("UPDATE enquiries SET doing='Other'")
    db.commit()
    x = _check(db, gid, p)["shortlist"][0]
    assert x["result"] == mt.NO_DESTINATIONS and x["result_plain"] == "No travel result: no member has a place of study or work recorded." and p.bodies == []


# ------------------------------------------------------------------------------------------------------- test records
def test_a_test_group_reaches_the_provider_only_while_a_controlled_test_is_switched_on(group, monkeypatch):
    db, gid, ids = group
    _answer(db, gid)
    db.execute("UPDATE enquiries SET is_test=1 WHERE id=?", (_members(db, gid)[0],))     # one test member makes it a test group
    db.commit()
    monkeypatch.delenv(routing.LIVE_TEST_DATA_ENV, raising=False)
    p, out = Provider(), _view(db, gid)
    assert out["is_test"] and out["travel_check"] == "off"
    assert out["travel_live"] == {"enabled": False, "auto": False, "plain": routing.LIVE_TEST_OFF, "ai_reply": False}
    # the address refuses on what the records say, whatever view it is handed, before the lock, the allowance and any request
    for view in (out, dict(out, is_test=False, travel_live={"enabled": True, "auto": True})):
        assert ml.travel_check(db, view, "anna", now=WHEN, post=p) == {"ok": False, "state": "test_data", "saved": False, "plain": routing.LIVE_TEST_OFF}
    assert p.bodies == [] and _audits(db) == []
    for not_on in ("0", "true", "yes", " 1", ""):
        monkeypatch.setenv(routing.LIVE_TEST_DATA_ENV, not_on)
        assert routing.live_test_data() is False and ml.travel_check(db, out, "anna", now=WHEN, post=p)["state"] == "test_data"
    monkeypatch.setenv(routing.LIVE_TEST_DATA_ENV, "1")
    assert _view(db, gid)["travel_live"]["enabled"] is True
    assert _check(db, gid, p)["state"] == "checked" and len(p.bodies) == 3 and len(_audits(db)) == 1   # one shared journey per apartment
    # the test switch alone turns nothing on
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    assert routing.live_test_data() is False and _check(db, gid, p)["state"] == "off" and len(p.bodies) == 3


# ----------------------------------------------------------------------------------------------- travel last, three at most
def test_the_first_three_are_checked_together_and_nothing_is_stored(group):
    db, gid, ids = group
    _answer(db, gid)
    before, p = _snapshot(db), Provider(minutes=20)
    r = _check(db, gid, p)
    assert r["ok"] and r["state"] == "checked" and r["saved"] is False and r["plain"] == ml.TRAVEL_NOT_SAVED and r["shortlist_max"] == 3
    assert [x["apartment_id"] for x in r["shortlist"]] == ids[:3] and r["failed"] == []
    assert [x["apartment_id"] for x in r["not_checked"]] == ids[3:]
    assert len(p.bodies) == 3 and sorted(p.origins()) == ["1 Neutral Street", "2 Synthetic Road", "3 Synthetic Road"]   # both people share one journey
    assert all(x["travelMode"] == "TRANSIT" and set(x) == {"origin", "destination", "travelMode", "computeAlternativeRoutes", "departureTime"} for x in p.bodies)
    assert r["check"]["requests"] == 3 and r["check"]["measured"] == 3 and r["check"]["failures"] == 0 and r["check"]["stages"] == 1
    for x in r["shortlist"]:
        assert x["result"] == mt.PASS and x["role"] == "shortlist" and x["replacement"] is False and x["measured"] is True
        assert x["result_plain"].startswith("Within the doubled limit: the closest 2 of the people with a known destination average 20.0 minutes")
        assert x["journey_lines"][0].endswith("20 minutes (rounded) by Public transport; within their own stated time (30 minutes) (one of the closest majority)")
        assert x["travel_sources"] == {"links": [{"title": SOURCE["title"], "url": SOURCE["url"]}], "walking_warning": None}
        assert x["ai_travel"] is None
    assert r["shortlist"][0]["is_current"] is True and r["unruled_note"] is None
    assert r["ai_reply"]["state"] == "off" and r["ai_gate"]["held"] is True and r["ai_gate"]["reason"] == "off"
    # one count-only record, written before the request left; no other table changed
    (rec,) = _audits(db)
    assert json.loads(rec["detail"]) == {"requests": 3, "not_requested": 0} and rec["entity"] == "group" and rec["entity_id"] == str(gid)
    assert _snapshot(db) == before
    stored = json.dumps(rows(db.execute("SELECT * FROM audit_log")), default=str)
    assert SOURCE["url"] not in stored and "Synthetic Road" not in rec["detail"] and "1200s" not in stored


def test_a_failed_apartment_is_replaced_by_the_next_stored_candidate_which_is_then_checked(group):
    db, gid, ids = group
    _answer(db, gid)
    p = Provider(minutes=20, slow={"2 Synthetic Road": 100, "4 Synthetic Road": 70})
    r = _check(db, gid, p)
    assert [x["apartment_id"] for x in r["shortlist"]] == [ids[0], ids[2], ids[4]]                   # never more than three
    assert [x["apartment_id"] for x in r["failed"]] == [ids[1], ids[3]] and [x["apartment_id"] for x in r["not_checked"]] == [ids[5]]
    assert [x["replacement"] for x in r["shortlist"]] == [False, False, True]
    assert r["failed"][0]["result"] == mt.EXCEEDS_CEILING and r["failed"][0]["result_plain"].startswith("Over the doubled limit")
    assert "long journey" in r["failed"][0]["journey_lines"][0]
    assert p.origins()[3:] == ["4 Synthetic Road", "5 Synthetic Road"]                               # one at a time, only when reached
    assert "6 Synthetic Road" not in p.origins() and r["check"]["stages"] == 3
    assert [json.loads(x["detail"])["requests"] for x in _audits(db)] == [3, 1, 1]
    assert rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))) == [{"apartment_id": i} for i in ids]


def test_a_failed_request_is_neither_a_pass_nor_a_failure(group):
    db, gid, ids = group
    _answer(db, gid)
    r = _check(db, gid, Provider(fail=True))
    assert [x["apartment_id"] for x in r["shortlist"]] == ids[:3] and r["failed"] == [] and r["check"]["failures"] == 3
    for x in r["shortlist"]:
        assert x["result"] == mt.NEEDS_CONFIRMATION and x["measured"] is False and x["travel_sources"]["links"] == []
        assert x["result_plain"] == "Travel time needs confirmation. This is not a pass and not a failure."
        assert x["reasons_plain"] == ["a journey could not be measured"]
    assert "Secret Street" not in json.dumps(r) and r["ai_reply"]["state"] == "off"


# ---------------------------------------------------------------------------------------- the part that is not ruled
@pytest.mark.parametrize("second,why", [("45 minutes", "the members' maximum travel times differ"), (None, "a member has not stated a maximum travel time")])
def test_differing_or_missing_times_are_never_combined_and_never_fail_an_apartment(group, second, why):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid, travel="15 minutes", only=[a])
    _answer(db, gid, travel=second, only=[b])
    r = _check(db, gid, Provider(minutes=100))                                           # far over anyone's time, yet not a failure
    assert [x["apartment_id"] for x in r["shortlist"]] == ids[:3] and r["failed"] == [] and r["unruled_note"] == mt.UNRULED_NOTE
    x = r["shortlist"][0]
    assert x["result"] == mt.NEEDS_CONFIRMATION and x["reasons_plain"] == [why]
    f = x["travel"]["facts"]
    assert f["majority"] is None and f["ceiling_minutes"] is None and f["stated_limit_minutes"] is None and f["within_ceiling"] is None
    assert x["journey_lines"][0].endswith("100 minutes (rounded) by Public transport; over their own stated time (15 minutes)")
    assert x["journey_lines"][1].endswith("over their own stated time (45 minutes)" if second else "no maximum travel time stated")


def test_no_travel_choice_answered_means_no_request_and_no_journey_selected(group):
    db, gid, ids = group
    _answer(db, gid, modes=())
    p = Provider()
    r = _check(db, gid, p)
    assert p.bodies == [] and _audits(db) == [] and r["check"]["requests"] == 0
    x = r["shortlist"][0]
    assert x["result"] == mt.NEEDS_CONFIRMATION and x["measured"] is False
    assert x["reasons_plain"] == ["a member has not said how they would travel, so no journey is selected for them"]
    assert "how they would travel has not been answered" in x["journey_lines"][0]
    assert r["ai_reply"]["state"] == "off"


# ------------------------------------------------------------------------------------------------------- allowance
def test_the_allowance_is_shared_with_homebot_and_refuses_before_any_request(group, monkeypatch):
    db, gid, ids = group
    _answer(db, gid)
    monkeypatch.setenv("MMAI_ROUTING_LIVE_DAILY_REQUESTS", "7")
    p = Provider()
    assert _check(db, gid, p)["check"]["requests"] == 3
    db.execute("UPDATE audit_log SET at=? WHERE action=?", (WHEN.strftime("%Y-%m-%dT%H:%M:%S+00:00"), ml.TRAVEL_CHECK_ACTION))
    db.commit()
    assert hs._travel_requests_used(db, WHEN.strftime("%Y-%m-%d")) == 3                # MateBot's requests count for HomeBot too
    db.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?, 'anna', ?, 'homebot_enquiry', '1', ?)",
               (WHEN.strftime("%Y-%m-%dT%H:%M:%S+00:00"), hs.TRAVEL_CHECK_ACTION, j({"requests": 2, "not_requested": 0})))
    db.commit()
    p = Provider()
    r = _check(db, gid, p)                                                               # 7 a day, 5 used: two requests left
    assert r["check"]["requests"] == 2 and r["check"]["not_requested"] == 1 and len(p.bodies) == 2
    assert r["shortlist"][2]["result"] == mt.NEEDS_CONFIRMATION
    db.execute("UPDATE audit_log SET at=? WHERE action=?", (WHEN.strftime("%Y-%m-%dT%H:%M:%S+00:00"), ml.TRAVEL_CHECK_ACTION))
    db.commit()
    p = Provider()
    r = _check(db, gid, p)
    assert r["ok"] is False and r["state"] == "limit" and "today" in r["plain"] and p.bodies == []


def test_refusals_closed_empty_busy_and_changed_during_the_check(group, monkeypatch):
    db, gid, ids = group
    _answer(db, gid)
    p = Provider()
    assert _check(db, gid, p, status="Leased")["state"] == "closed" and _check(db, gid, p, status="Dissolved")["state"] == "closed"
    assert _check(db, gid, p, apartments=[])["state"] == "no_options" and p.bodies == []
    waited = []

    class Held:                                                                           # HomeBot's check is running: one lock for both bots
        def acquire(self, timeout=None):
            waited.append(timeout)
            return False

        def release(self):
            raise AssertionError("released a lock that was never taken")
    real = hs._travel_lock
    monkeypatch.setattr(hs, "_travel_lock", Held())
    r = _check(db, gid, p)
    assert r["ok"] is False and r["state"] == "busy" and waited == [10] and p.bodies == [] and _audits(db) == []
    monkeypatch.setattr(hs, "_travel_lock", real)

    def change():
        db.execute("UPDATE group_apartments SET rank=rank+10 WHERE group_id=? AND apartment_id=?", (gid, ids[5]))
        db.commit()
    r = _check(db, gid, Provider(during=change))
    assert r["ok"] is False and r["state"] == "replaced" and "shortlist" not in r


# ---------------------------------------------------------------------------------------------- the AI and the gate
@pytest.fixture
def with_ai(group, monkeypatch):
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR", "MMAI_AI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key-not-real")
    monkeypatch.setenv("MMAI_AI_MODE", "own-key")
    monkeypatch.setenv(tr.REPLY_ENV, "1")
    monkeypatch.setattr(ai, "_client", lambda: (_ for _ in ()).throw(AssertionError("real model call in a test")))
    model = Model()
    monkeypatch.setattr(ai, "_ask", model)
    return (*group, model)


def _reviewed(db, gid, ids):
    snap = json.loads(row(db.execute("SELECT evidence_json FROM groups WHERE id=?", (gid,)))["evidence_json"] or "{}")
    snap["apartments"] = [{"apartment_id": i} for i in ids]
    db.execute("UPDATE groups SET evidence_json=? WHERE id=?", (j(snap), gid))
    db.commit()


def test_a_shortlist_is_ai_assessed_only_with_a_current_review_that_covered_it_and_an_accepted_assessment(with_ai):
    db, gid, ids, model = with_ai
    _answer(db, gid)
    before = _snapshot(db)
    # as the group really is: held, so not ready; the journeys are shown and the shortlist stays held
    r = _check(db, gid, Provider())
    assert r["ai_reply"]["state"] == "ready" and r["ai_gate"]["held"] is True and r["ai_gate"]["reason"] == "review"
    assert len(model.prompts) == 1 and all(x["ai_travel"]["group_result"] == "within_doubled_limit" for x in r["shortlist"])
    assert [x["ai_travel"]["preference"] for x in r["shortlist"]] == [1, 2, 3] and r["shortlist"][0]["ai_travel"]["advice"] == "confirm_first"
    # the model saw positions and figures only: no name, address, institution or destination
    prompt = model.prompts[0]
    for banned in ("Astrid", "Bo", "Chandra", "Devi", "RMIT", "Neutral Street", "Synthetic Road", "example.com", "Swanston"):
        assert banned not in prompt, banned
    # a ready group whose saved review never saw the apartments added later: held, staff decide
    r = _check(db, gid, Provider(), ready=True)
    assert r["ai_gate"]["held"] is True and r["ai_gate"]["reason"] == "not_reviewed"
    # ready, and the saved review covered all three: AI-assessed
    _reviewed(db, gid, ids[:3])
    before = _snapshot(db)
    r = _check(db, gid, Provider(), ready=True)
    assert r["ai_gate"] == {"state": "assessed", "held": False, "plain": mr.ASSESSED_TEXT}
    # a replacement the saved review never saw is not AI-assessed by this check's travel assessment alone
    r = _check(db, gid, Provider(slow={"2 Synthetic Road": 100}), ready=True)
    assert [x["apartment_id"] for x in r["shortlist"]] == [ids[0], ids[2], ids[3]] and r["shortlist"][2]["replacement"] is True
    assert r["ai_gate"]["reason"] == "not_reviewed" and r["shortlist"][2]["ai_travel"] is not None
    assert all("ai_travel" not in x for x in r["failed"])                                # the failed one is not assessed as an option
    # a reply that does not match the figures is withheld whole, and the shortlist is held
    model.make = lambda options: {"options": [], "preferred_order": []}
    r = _check(db, gid, Provider(), ready=True)
    assert r["ai_reply"]["state"] == "withheld" and r["ai_gate"]["reason"] == "withheld" and all(x["ai_travel"] is None for x in r["shortlist"])
    assert r["shortlist"][0]["result"] == mt.PASS                                         # the journey figures stay visible
    assert _snapshot(db) == before                                                         # nothing of the reply or the journeys is stored


def test_the_practical_hold_reaches_the_model_as_an_open_point(with_ai):
    db, gid, ids, model = with_ai
    _answer(db, gid)
    view = _view(db, gid)
    assert view["apartments"][0]["practical_need_hold"] is True                           # the step-free hold this group carries
    r = ml.travel_check(db, view, "anna", now=WHEN, post=Provider())
    given = json.loads(model.prompts[0].split("GOOGLE MAPS JOURNEY ESTIMATES", 1)[1].split(")\n\n", 1)[1])["options"]
    assert given[0]["apartment"]["open_points"] == [ml.OPEN_POINT_PRACTICAL]
    assert r["shortlist"][0]["ai_travel"]["advice"] == "confirm_first"
    assert mr.POINTS["other_points_to_confirm"][1] in r["shortlist"][0]["ai_travel"]["weaknesses"]


# ---------------------------------------------------------------------------------------------------- evidence
def test_a_travel_choice_answer_is_evidence_once_given_so_an_edit_makes_the_saved_review_out_of_date(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    h0, snap0 = matching.group_evidence(db, gid)
    assert all("travel_modes" not in m["facts"] for m in snap0["members"])               # never answered: older evidence is unchanged
    _answer(db, gid, travel=None, modes=(PT,), only=[a])
    h1, snap1 = matching.group_evidence(db, gid)
    assert h1 != h0 and [m["facts"].get("travel_modes") for m in snap1["members"] if m["enquiry_id"] == a] == [j([PT])]
    _answer(db, gid, travel=None, modes=(W, PT), only=[a])
    assert matching.group_evidence(db, gid)[0] not in (h0, h1)
    _answer(db, gid, travel=None, modes=(), only=[a])
    assert matching.group_evidence(db, gid)[0] == h0


# ------------------------------------------------------------------------------------------------------ the page
def test_the_group_page_runs_the_check_on_open_labels_candidates_provisional_and_keeps_nothing():
    panel = STAFF_JS.split("function groupTravelPanel(box, g) {", 1)[1].split("\n  }\n", 1)[0]
    assert "\n    if (live.auto && !kept.tried) run(false);" in panel                         # automatic on open, not staff selection
    assert "if (kept.busy) return;" in panel and "kept.busy = true; kept.tried = true;" in panel   # one request at a time, one automatic attempt
    assert panel.count("run(false)") == 1 and panel.count("api.post(") == 1
    assert "} finally { kept.busy = false; kept.show(); }" in panel                       # a redraw during a check still ends with its result
    assert "api.post(`/api/groups/${g.id}/travel-check`, {})" in panel and "HB_TRAVEL_KEEP_MS" in panel
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert banned not in panel, banned
    view = STAFF_JS.split("function groupTravelView(r) {", 1)[1].split("\n  }\n", 1)[0]
    assert "GMAPS_ATTR" in view and "GMAPS_TEXT" in view and 'data-role="mb-walk-warning"' in view
    assert "'Provisional shortlist (not confirmed)'" in view and "gate.held ? 'Held.' : 'AI-assessed.'" in view
    assert "const provisional = gate.held !== false;" in view                             # anything but an explicit pass reads provisional
    assert 'data-role="mb-travel-unruled"' in view and "Failed the travel check (not on the shortlist)" in view
    card = STAFF_JS.split("function travelProvisionalHtml(g, opts = {}) {", 1)[1].split("\n  }\n", 1)[0]
    assert "<b>Provisional.</b>" in card and "they are candidates and not a confirmed shortlist" in card
    assert "${travelProvisionalHtml(g, opts)}" in STAFF_JS and "groupTravelPanel(p.querySelector('[data-role=\"mb-travel-panel\"]'), g);" in STAFF_JS
    for code in ("H28", "GP6", "T6", "M4"):                                                # staff screens never quote register codes
        assert code not in panel and code not in view and code not in card, code
    assert "Travel to campus or work has not been checked for these apartments." not in STAFF_JS


def test_the_address_is_staff_only_and_answers_plainly(client):  # noqa: F811
    from fastapi.testclient import TestClient
    assert TestClient(client.app).post("/api/groups/1/travel-check").status_code in (401, 403)
    assert client.post("/api/groups/999999/travel-check").status_code == 404
