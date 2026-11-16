"""Travel at the staff actions that present an apartment to a MateBot group (GP6(d), H28(d), T4/T6; 16 Nov 2026).

Travel is never stored, so nothing saved can say an apartment passed or failed it. Each action that presents an apartment
(offering it, drafting the introduction for it, marking the group introduced, introducing a revised group, acknowledging a
changed offer) checks that ONE apartment live first. A known failure refuses the action and changes nothing. Anything else
lets it go on and is worded as what it is: a pass only when every person was measured, a pass for part of the group, needs
confirmation, no destinations, or not checked. Staff still write and send everything themselves; nobody is moved and no
group is split. Synthetic journey replies; no network, no provider, no model.
"""
import json
from pathlib import Path

import pytest

from server import ai, homebot_shortlist as hs, matebot_live as ml, matebot_travel as mt, matebot_travel_reply as mr, routing
from server.db import j, row, rows
from tests.test_data_and_api import accept_all_members, appdb, client  # noqa: F401  (fixtures)
from tests.test_live_travel import _live_env
from tests.test_matebot_live import PT, WHEN, Provider, _answer, _audits, _members, _snapshot, _view, group  # noqa: F401  (group: fixture)
from tests.test_r7_intro_routes import _group, _intro_rows, _ready_party_with_two_offers
from tests.test_t6_staff_api import _introduce

STAFF_JS = (Path(__file__).resolve().parents[1] / "client" / "staff.js").read_text(encoding="utf-8")
ACTIONS = (ml.ACTION_OFFER, ml.ACTION_DRAFT, ml.ACTION_INTRODUCE, ml.ACTION_ACKNOWLEDGE)


def _act(db, gid, aid, provider, action=ml.ACTION_OFFER):
    return ml.action_check(db, gid, aid, action, "anna", now=WHEN, post=provider)


# ------------------------------------------------------------------------------------------------- the check itself
def test_one_apartment_is_checked_with_one_count_recorded_and_nothing_else_saved(group, monkeypatch):
    db, gid, ids = group
    _answer(db, gid)
    monkeypatch.setattr(mr, "travel_reply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AI call at a staff action")))
    before, p = _snapshot(db), Provider(minutes=20)
    r = _act(db, gid, ids[1], p)
    assert (r["outcome"], r["refuse"], r["state"], r["saved"]) == (ml.PASSED, False, "checked", False)
    assert p.origins() == ["2 Synthetic Road"]                                          # that apartment only; both people share one journey
    assert r["coverage"]["complete"] is True and r["coverage"]["people"] == 2
    assert r["plain"] == ("Travel was checked for this action. Within the doubled limit: the closest 2 of the people with a known destination "
                          "average 20.0 minutes (rounded) against 60 minutes, twice the stated 30. Journeys were measured for all 2 people in "
                          "this group. " + ml.ACTION_NOT_SAVED)
    (rec,) = _audits(db)
    assert json.loads(rec["detail"]) == {"requests": 1, "not_requested": 0} and _snapshot(db) == before
    stored = json.dumps(rows(db.execute("SELECT * FROM audit_log")), default=str)
    assert "Synthetic Road" not in rec["detail"] and "1200s" not in stored and "passed" not in rec["detail"]
    assert ml.still_current(db, gid, r["token"]) is True and ml.still_current(db, gid, None) is True


@pytest.mark.parametrize("action", ACTIONS)
def test_a_known_failure_refuses_the_action_in_its_own_words(group, action):
    db, gid, ids = group
    _answer(db, gid)
    before = _snapshot(db)
    r = _act(db, gid, ids[1], Provider(minutes=20, slow={"2 Synthetic Road": 100}), action)
    assert (r["outcome"], r["refuse"], r["saved"]) == (ml.FAILED, True, False)
    assert r["plain"].startswith(ml._REFUSED_LEAD[action] + " Over the doubled limit: the closest 2 of the people with a known destination average 100.0 minutes")
    assert r["plain"].endswith(ml._REFUSED_TAIL[action] + " Nothing was changed.") and _snapshot(db) == before
    assert r["plain"].startswith({"offer": "Not offered.", "draft": "No introduction was drafted.", "introduce": "Not marked introduced.",
                                  "acknowledge": "Not acknowledged."}[action])
    with pytest.raises(ValueError):
        _act(db, gid, ids[1], Provider(), "lease")


def test_differing_times_fail_or_pass_an_action_by_the_approved_calculation(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid, travel="15 minutes", only=[a])
    _answer(db, gid, travel="45 minutes", only=[b])
    assert _act(db, gid, ids[1], Provider(minutes=60))["outcome"] == ml.PASSED          # exactly twice the average of 15 and 45
    r = _act(db, gid, ids[1], Provider(minutes=61))
    assert (r["outcome"], r["refuse"]) == (ml.FAILED, True) and "twice the average of those same people's stated maximum travel times (15, 45 minutes)" in r["plain"]


def test_what_is_not_a_known_failure_goes_on_and_is_never_called_a_pass_or_a_failure(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)

    def provisional(r, outcome):
        assert (r["outcome"], r["refuse"], r["saved"]) == (outcome, False, False), r
        assert ml.PROVISIONAL_TAIL.strip() in r["plain"] and "Not offered" not in r["plain"]
        return r
    # a journey that could not be measured
    r = provisional(_act(db, gid, ids[1], Provider(fail=True)), ml.NEEDS_CONFIRMATION)
    assert "Travel time needs confirmation. This is not a pass and not a failure." in r["plain"] and "Secret Street" not in json.dumps(r, default=str)
    # a member who never stated a time: however far the journey, not a failure
    _answer(db, gid, travel=None, only=[b])
    provisional(_act(db, gid, ids[1], Provider(minutes=500)), ml.NEEDS_CONFIRMATION)
    _answer(db, gid)
    # a couple: the pass covers two of three people and is said to
    db.execute("UPDATE enquiries SET party='Couple', party_size=2 WHERE id=?", (a,))
    db.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?, 'Pat Partner', 'pat@example.com')", (a,))
    db.commit()
    r = provisional(_act(db, gid, ids[1], Provider(minutes=20)), ml.PASSED_PART)
    assert r["coverage"]["complete"] is False and r["coverage"]["destination_never_asked"] == [2]
    assert "never asked for a place of study or work, so their travel is unverified" in r["plain"] and "not for the whole group" in r["plain"]
    # but a failure for the people measured is still a known failure
    assert _act(db, gid, ids[1], Provider(minutes=100))["refuse"] is True
    # nobody with a destination: no request, no result
    db.execute(f"UPDATE enquiries SET doing='Other', work=NULL WHERE id IN (?, ?)", (a, b))
    db.commit()
    p = Provider()
    r = provisional(_act(db, gid, ids[1], p), ml.NO_DESTINATIONS)
    assert p.bodies == [] and "No travel result" in r["plain"]


def test_not_checked_when_switched_off_for_a_test_record_with_the_allowance_used_or_while_another_check_runs(group, monkeypatch):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)
    p = Provider(minutes=500)

    def not_checked(state):
        r = _act(db, gid, ids[1], p)
        assert (r["outcome"], r["refuse"], r["state"], r["token"], r["coverage"]) == (ml.NOT_CHECKED, False, state, None, None), r
        assert r["plain"].startswith("Travel was not checked for this action. ") and r["plain"].endswith(ml.PROVISIONAL_TAIL)
        assert p.bodies == []
        return r
    # another check is running: one lock for both bots
    class Held:
        def acquire(self, timeout=None):
            return False

        def release(self):
            raise AssertionError("released a lock that was never taken")
    real = hs._travel_lock
    monkeypatch.setattr(hs, "_travel_lock", Held())
    not_checked("busy")
    monkeypatch.setattr(hs, "_travel_lock", real)
    assert _audits(db) == []
    # the day's allowance is used
    monkeypatch.setenv("MMAI_ROUTING_LIVE_DAILY_REQUESTS", "2")
    db.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?, 'anna', ?, 'homebot_enquiry', '1', ?)",
               (WHEN.strftime("%Y-%m-%dT%H:%M:%S+00:00"), hs.TRAVEL_CHECK_ACTION, j({"requests": 2, "not_requested": 0})))
    db.commit()
    assert "allowance" in not_checked("limit")["plain"]
    monkeypatch.setenv("MMAI_ROUTING_LIVE_DAILY_REQUESTS", "1000")
    # a test record, with no controlled test switched on
    db.execute("UPDATE enquiries SET is_test=1 WHERE id=?", (b,))
    db.commit()
    monkeypatch.delenv(routing.LIVE_TEST_DATA_ENV, raising=False)
    assert routing.LIVE_TEST_OFF in not_checked("test_data")["plain"]
    db.execute("UPDATE enquiries SET is_test=0 WHERE id=?", (b,))
    db.commit()
    # switched off: nothing leaves, nothing is recorded
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    assert routing.LIVE_OFF in not_checked("off")["plain"]
    assert _audits(db) == []


def test_a_group_that_changes_during_or_after_the_check_is_caught(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)

    def rerank():
        db.execute("UPDATE group_apartments SET rank=rank+10 WHERE group_id=? AND apartment_id=?", (gid, ids[5]))
        db.commit()
    r = _act(db, gid, ids[1], Provider(during=rerank), ml.ACTION_INTRODUCE)
    assert (r["outcome"], r["refuse"], r["state"], r["token"]) == (ml.CHANGED, True, "replaced", None)
    assert r["plain"] == "Not marked introduced. The group or one of its apartments changed while travel was being checked. Reload the group and try again."
    # after the check and before the action's own step: the token no longer matches
    r = _act(db, gid, ids[1], Provider())
    assert ml.still_current(db, gid, r["token"]) is True
    _answer(db, gid, travel="5 minutes", only=[b])                                       # a travel answer was edited
    assert ml.still_current(db, gid, r["token"]) is False
    r = _act(db, gid, ids[1], Provider(minutes=4))
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (ids[2], gid))            # the current offer moved
    db.commit()
    assert ml.still_current(db, gid, r["token"]) is False
    r = _act(db, gid, ids[1], Provider(minutes=4))
    db.execute("UPDATE enquiries SET school='A school that is not on file' WHERE id=?", (a,))   # a destination changed
    db.commit()
    assert ml.still_current(db, gid, r["token"]) is False


APARTMENT_CHANGES = (("address", "UPDATE apartments SET address='9 / 1 Somewhere Else Street, Carlton VIC 3053' WHERE id=?"),
                     ("address as listed", "UPDATE apartments SET address_raw='9/1 Somewhere Else St' WHERE id=?"),
                     ("suburb", "UPDATE apartments SET suburb='Carlton' WHERE id=?"),
                     ("availability", "UPDATE apartments SET available_from='2031-01-01' WHERE id=?"),
                     ("leased", "UPDATE apartments SET leased_at='2026-11-16T00:00:00+00:00', leased_group_id=NULL WHERE id=?"),
                     ("lease generation", "UPDATE apartments SET lease_generation=lease_generation+1 WHERE id=?"))


@pytest.mark.parametrize("what,sql", APARTMENT_CHANGES, ids=[x[0] for x in APARTMENT_CHANGES])
def test_a_result_for_an_apartment_as_it_was_never_stands_for_the_apartment_as_it_is(group, what, sql):
    """The journey is measured from the apartment's address, for an apartment that can be offered. If where it is, or whether
    it is available or leased, changes after the check and before the action's own step, the check is not current."""
    db, gid, ids = group
    _answer(db, gid)
    r = _act(db, gid, ids[1], Provider(minutes=4))
    assert r["outcome"] != ml.CHANGED and ml.still_current(db, gid, r["token"]) is True
    key = ml.view_key(db, gid)
    db.execute(sql, (ids[1],))
    db.commit()
    assert ml.still_current(db, gid, r["token"]) is False, what
    assert ml.view_key(db, gid) != key, what                                             # and the page is told it is another state
    # the same change while the provider is being asked: refused as changed, not measured against the old apartment
    r = _act(db, gid, ids[2], Provider(minutes=4))
    assert ml.still_current(db, gid, r["token"]) is True

    def change():
        db.execute(sql.replace("lease_generation+1", "lease_generation+2").replace("2031-01-01", "2032-01-01").replace("Somewhere Else", "Another"), (ids[2],))
        db.commit()
    again = _act(db, gid, ids[2], Provider(minutes=4, during=change))
    assert (again["outcome"], again["refuse"], again["token"]) == (ml.CHANGED, True, None), what


def test_a_change_that_is_not_about_where_or_whether_the_apartment_is_does_not_refuse_the_action(group):
    db, gid, ids = group
    _answer(db, gid)
    r = _act(db, gid, ids[1], Provider(minutes=4))
    db.execute("UPDATE apartments SET parking_charge='$50 per week' WHERE id=?", (ids[1],))       # not part of a journey or of availability
    db.execute("UPDATE apartments SET address='1 Unrelated Street, Melbourne VIC 3000' WHERE id NOT IN (SELECT apartment_id FROM group_apartments WHERE group_id=?) "
               "AND id NOT IN (SELECT apartment_id FROM groups WHERE id=? AND apartment_id IS NOT NULL)", (gid, gid))   # another group's apartment
    db.commit()
    assert ml.still_current(db, gid, r["token"]) is True


def test_the_page_key_names_one_state_of_the_group_and_gives_nothing_away(group):
    db, gid, ids = group
    a, b = _members(db, gid)
    _answer(db, gid)
    key = ml.view_key(db, gid)
    assert key == ml.view_key(db, gid) and len(key) == 24 and all(ch in "0123456789abcdef" for ch in key)   # stable, short, one-way
    out = ml.travel_check(db, _view(db, gid), "anna", now=WHEN, post=Provider(minutes=4))
    assert out["ok"] and out["key"] == key                                              # the result says which state it is for
    assert ml.view_key(db, gid) == key                                                  # checking changes nothing
    seen = {key}
    _answer(db, gid, travel="5 minutes", only=[b])                                       # a stated travel time was edited
    seen.add(ml.view_key(db, gid)); assert len(seen) == 2
    _answer(db, gid, travel="5 minutes", modes=["Walk"], only=[b])                       # a travel choice was edited
    seen.add(ml.view_key(db, gid)); assert len(seen) == 3
    db.execute("UPDATE enquiries SET school='A school that is not on file' WHERE id=?", (a,))   # a destination was edited
    db.commit()
    seen.add(ml.view_key(db, gid)); assert len(seen) == 4
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (ids[2], gid))            # the current offer moved
    db.commit()
    seen.add(ml.view_key(db, gid)); assert len(seen) == 5
    db.execute("UPDATE apartments SET address='9 / 1 Somewhere Else Street, Carlton VIC 3053' WHERE id=?", (ids[3],))   # a listed apartment moved
    db.commit()
    seen.add(ml.view_key(db, gid)); assert len(seen) == 6
    # nothing about travel is stored by any of this
    assert all(set(json.loads(x["detail"])) == {"requests", "not_requested"} for x in _audits(db))


# ------------------------------------------------------------------------------------------------- through the API
def _on(monkeypatch, db, gid, provider):
    """Live checks on for this (test-record) group as a controlled test, with a stand-in journey service. The fixture's
    people study at a place that is not on file and never answered the travel questions; here the two enquirers have a
    verified place of study and their answers. The others in the party were never asked, as on the real form."""
    _live_env(monkeypatch)
    monkeypatch.setenv(routing.LIVE_TEST_DATA_ENV, "1")
    monkeypatch.setattr(routing, "_agent_tools_post", provider)
    monkeypatch.setattr(ml, "find_institution", lambda name: {"name": name, "verified": True, "address": "124 La Trobe Street, Melbourne VIC 3000"})
    real = ml.group_people

    def people(c, g):
        out = real(c, g)
        for p in out:
            p.update(stated_time="30 minutes", modes=(PT,))
        return out
    monkeypatch.setattr(ml, "group_people", people)
    return provider


def _street(db, aid):
    """The building's street address as the journey service is asked about it (the unit number is not sent)."""
    return row(db.execute("SELECT address FROM apartments WHERE id=?", (aid,)))["address"].split(",")[0].split(" / ")[-1].strip()


def _offers(db):
    return rows(db.execute("SELECT * FROM audit_log WHERE action='offer_apartment' ORDER BY id"))


def test_offering_an_apartment_that_fails_travel_is_refused_and_one_that_does_not_goes_on(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid, first = g["id"], g["apartment_id"]
    d = client.get(f"/api/groups/{gid}").json()
    assert d["ready"] is True and d["review_state"] == "Ready for reservations"
    assert d["travel_offer"]["state"] == "provisional" and d["travel_offer"]["why"] == "not_checked"      # ready on the review; travel unclaimed
    p = _on(monkeypatch, db, gid, Provider(minutes=20, slow={_street(db, other): 100}))
    before, offers = _group(db, gid), _offers(db)
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["travel_refused"] is True and body["travel_at_action"]["outcome"] == "failed" and body["travel_at_action"]["saved"] is False
    assert body["detail"].startswith("Not offered. This apartment failed the travel check just now") and "token" not in body["travel_at_action"]
    assert "not for the whole group" in body["detail"]                                   # two of the four people were measured, and it says so
    assert _group(db, gid) == before and _offers(db) == offers and len(p.bodies) == 1
    assert [json.loads(x["detail"]) for x in _audits(db)] == [{"requests": 1, "not_requested": 0}]      # a count; no result is recorded
    # the same apartment when it does not fail: offered, and the reply says what the check covered
    p.slow = {}
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 200, r.text
    at = r.json()["travel_at_action"]
    assert at["outcome"] == "passed_for_part_of_group" and at["coverage"]["measured"] == 2 and at["coverage"]["people"] == 4
    assert "provisional" in at["plain"] and _group(db, gid)["apartment_id"] == other and len(_offers(db)) == len(offers) + 1
    assert "travel_at_action" not in client.get(f"/api/groups/{gid}").json()             # for that reply only
    # a journey that cannot be measured does not stop the offer either, and is not called a pass
    p.fail = True
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": first})
    assert r.status_code == 200 and r.json()["travel_at_action"]["outcome"] == "needs_confirmation" and _group(db, gid)["apartment_id"] == first


def test_with_live_checks_off_the_actions_work_as_before_and_say_travel_was_not_checked(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    monkeypatch.setattr(routing, "_agent_tools_post", lambda body: (_ for _ in ()).throw(AssertionError("provider called while off")))
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 200 and r.json()["travel_at_action"]["outcome"] == "not_checked" and r.json()["travel_at_action"]["state"] == "off"
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 200 and r.json()["travel_at_action"]["outcome"] == "not_checked"
    r = client.patch(f"/api/groups/{gid}", json=_introduce(client, gid))
    assert r.status_code == 200, r.text
    assert r.json()["travel_at_action"]["outcome"] == "not_checked" and _group(db, gid)["status"] == "Introduced"
    assert _audits(db) == []


def test_no_introduction_is_drafted_or_recorded_for_a_current_offer_that_fails_travel(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid, first = g["id"], g["apartment_id"]
    p = _on(monkeypatch, db, gid, Provider(minutes=20, slow={_street(db, first): 100}))
    drafted = []
    real_draft = ai.draft_introduction
    monkeypatch.setattr(ai, "draft_introduction", lambda *a, **k: drafted.append(1) or real_draft(*a, **k))
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 409 and r.json()["travel_refused"] is True
    assert r.json()["detail"].startswith("No introduction was drafted. The apartment currently offered failed the travel check just now.")
    assert drafted == [] and _intro_rows(db, gid) == [] and _group(db, gid)["intro_text"] is None      # the AI was never asked
    # drafted while the offer did not fail; then it fails at the moment of marking the group introduced
    p.slow = {}
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 200 and r.json()["travel_at_action"]["outcome"] == "passed_for_part_of_group" and drafted == [1]
    body = _introduce(client, gid)
    p.slow = {_street(db, first): 100}
    r = client.patch(f"/api/groups/{gid}", json=body)
    assert r.status_code == 409 and r.json()["travel_refused"] is True and r.json()["detail"].startswith("Not marked introduced.")
    assert "If the introduction has already gone out, tell the group the apartment is being changed." in r.json()["detail"]
    assert _group(db, gid)["status"] == "Proposed"
    # the other apartment does not fail: staff offer it, draft for it and introduce the group with it. Nobody was moved.
    members = rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (gid,)))
    assert client.patch(f"/api/groups/{gid}", json={"apartment_id": other}).status_code == 200
    assert client.post(f"/api/groups/{gid}/introduction").status_code == 200
    r = client.patch(f"/api/groups/{gid}", json=_introduce(client, gid))
    assert r.status_code == 200, r.text
    assert r.json()["travel_at_action"]["outcome"] == "passed_for_part_of_group" and _group(db, gid)["status"] == "Introduced"
    assert rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (gid,))) == members
    assert row(db.execute("SELECT COUNT(*) AS n FROM groups"))["n"] == 1                 # no group was split or made
    # what was recorded about travel, in all of that: counts of requests, nothing else
    assert all(set(json.loads(x["detail"])) == {"requests", "not_requested"} for x in _audits(db))


def test_a_group_that_changes_while_travel_is_checked_is_not_offered(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]

    def rerank():
        db.execute("UPDATE group_apartments SET rank=rank+10 WHERE group_id=? AND apartment_id=?", (gid, other))
        db.commit()
    _on(monkeypatch, db, gid, Provider(minutes=20, during=rerank))
    before = _group(db, gid)
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 409 and r.json()["travel_refused"] is True and r.json()["travel_at_action"]["outcome"] == "changed"
    assert r.json()["detail"].startswith("Not offered. The group or one of its apartments changed while travel was being checked.") and _group(db, gid) == before


def test_a_change_between_the_check_and_the_actions_own_step_is_refused_there(client, monkeypatch):
    from server import app as application
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    _on(monkeypatch, db, gid, Provider(minutes=20))
    real = ml.action_check

    def then_the_group_changes(*args, **kw):
        out = real(*args, **kw)
        db.execute("UPDATE group_apartments SET rank=rank+10 WHERE group_id=? AND apartment_id=?", (gid, other))
        db.commit()
        return out
    monkeypatch.setattr(ml, "action_check", then_the_group_changes)
    before, offers = _group(db, gid), _offers(db)
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 409 and r.json()["detail"] == application.TRAVEL_CHANGED
    assert _group(db, gid) == before and _offers(db) == offers
    r = client.post(f"/api/groups/{gid}/introduction")
    assert r.status_code == 409 and _group(db, gid) == before                            # no draft is kept for a group that moved on


def test_an_apartment_whose_address_changes_after_the_check_is_not_offered_on_that_check(client, monkeypatch):
    from server import app as application
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    _on(monkeypatch, db, gid, Provider(minutes=20))
    real = ml.action_check

    def then_the_apartment_moves(*args, **kw):
        out = real(*args, **kw)
        db.execute("UPDATE apartments SET address='9 / 1 Somewhere Else Street, Carlton VIC 3053' WHERE id=?", (other,))
        db.commit()
        return out
    monkeypatch.setattr(ml, "action_check", then_the_apartment_moves)
    before, offers = _group(db, gid), _offers(db)
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": other})
    assert r.status_code == 409 and r.json()["detail"] == application.TRAVEL_CHANGED
    assert "one of its apartments changed" in application.TRAVEL_CHANGED
    assert _group(db, gid) == before and _offers(db) == offers                           # the offer did not move


def test_the_group_page_is_given_the_key_only_while_live_checks_can_run(client, monkeypatch):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    off = client.get(f"/api/groups/{gid}").json()
    assert off["travel_live"]["enabled"] is False and "key" not in off["travel_live"]
    assert all("key" not in (x.get("travel_live") or {}) for x in client.get("/api/groups?dataset=test").json()["items"])
    _on(monkeypatch, db, gid, Provider(minutes=20))
    on = client.get(f"/api/groups/{gid}").json()
    assert on["travel_live"]["enabled"] is True and on["travel_live"]["key"] == ml.view_key(db, gid)
    checked = client.post(f"/api/groups/{gid}/travel-check").json()
    assert checked["ok"] and checked["key"] == on["travel_live"]["key"]                  # the result is for the state the page shows
    db.execute("UPDATE apartments SET address='9 / 1 Somewhere Else Street, Carlton VIC 3053' WHERE id=?", (other,))
    db.commit()
    assert client.get(f"/api/groups/{gid}").json()["travel_live"]["key"] != on["travel_live"]["key"]
    assert all("key" not in (x.get("travel_live") or {}) for x in client.get("/api/groups?dataset=test").json()["items"])   # lists never work it out


def test_a_group_whose_travel_details_cannot_be_read_still_opens_and_is_never_confirmed(client, monkeypatch, caplog):
    db, a, lead, solo, g, other = _ready_party_with_two_offers(client, monkeypatch)
    gid = g["id"]
    _on(monkeypatch, db, gid, Provider(minutes=20))

    def unreadable(c, group_id):
        raise ValueError("12 Secret Street could not be read")
    monkeypatch.setattr(ml, "group_people", unreadable)
    with caplog.at_level("WARNING"):
        r = client.get(f"/api/groups/{gid}")
    assert r.status_code == 200                                                          # the page opens
    live = r.json()["travel_live"]
    assert live["enabled"] is True and live["key"] is None and live["key_unavailable"] is True
    assert r.json()["travel_offer"]["state"] == "provisional" and r.json()["members"]     # provisional, and the rest of the page is there
    said = [x.getMessage() for x in caplog.records if "travel key unavailable" in x.getMessage()]
    assert said == [f"matebot travel key unavailable group={gid} kind=ValueError"]       # the kind only: nothing from the stored answer
    assert "Secret" not in caplog.text
    # unit: the same, without the app
    assert ml.page_state(db, gid, {"enabled": True}) == {"enabled": True, "key": None, "key_unavailable": True}
    monkeypatch.undo()


def test_a_changed_offer_that_fails_travel_cannot_be_acknowledged(client, monkeypatch):
    from server import stock_lifecycle as sl
    from tests.test_gp13_group_responses import _introduced
    from tests.test_stock_lifecycle import _transition
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    aid = _group(db, gid)["apartment_id"]
    rec = _transition(db, aid, "changed", changed_fields=["rent_6m"])
    db.execute("UPDATE apartments SET rent_6m=rent_6m+100 WHERE id=?", (aid,))
    db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    from server import ai_queue
    if not ai_queue.intro_state(db, gid).get("current"):
        ai_queue.reassess_all(db, "test", "tester")
        ai_queue.process_pending(db)
        assert client.post(f"/api/groups/{gid}/introduction").status_code == 200
    flagged = row(db.execute("SELECT offer_changed_since_intro_at AS at FROM groups WHERE id=?", (gid,)))["at"]
    assert flagged
    p = _on(monkeypatch, db, gid, Provider(minutes=100))
    r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
    assert r.status_code == 409 and r.json()["travel_refused"] is True and r.json()["detail"].startswith("Not acknowledged.")
    assert row(db.execute("SELECT offer_changed_since_intro_at AS at FROM groups WHERE id=?", (gid,)))["at"] == flagged
    assert not row(db.execute("SELECT 1 FROM audit_log WHERE action='group_offer_acknowledged' AND entity_id=?", (str(gid),)))
    p.minutes = 20
    r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
    assert r.status_code == 200 and r.json()["travel_at_action"]["outcome"] == "passed_for_part_of_group"
    assert row(db.execute("SELECT offer_changed_since_intro_at AS at FROM groups WHERE id=?", (gid,)))["at"] is None
    # with nothing to acknowledge, no journey is requested
    asked = len(p.bodies)
    assert client.patch(f"/api/groups/{gid}", json={"offer_ack": True}).status_code == 200 and len(p.bodies) == asked


def test_recording_what_has_happened_is_not_gated_on_travel(client, monkeypatch):
    """Confirming the people and recording a lease record facts; they are not stopped by travel and ask for no journey."""
    from tests.test_gp13_group_responses import _accept_all, _introduced
    db, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    p = _on(monkeypatch, db, gid, Provider(minutes=500))
    r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
    assert r.status_code == 200, r.text
    assert _group(db, gid)["status"] == "Group confirmed" and p.bodies == [] and "travel_at_action" not in r.json()


# ---------------------------------------------------------------------------------------------------- the staff page
def test_the_page_never_shows_ready_in_green_without_a_confirmed_travel_check():
    assert 'class="badge badge--alt" data-role="ready-badge"' in STAFF_JS and ">Ready for reservations · travel not confirmed</span>" in STAFF_JS
    assert "<span>Ready for reservations (travel not confirmed)$" in STAFF_JS and ">Ready for reservations (travel not confirmed)</option>" in STAFF_JS
    assert "Ready for reservations$" not in STAFF_JS and "Ready for reservations</option>" not in STAFF_JS and "Ready for reservations</span>" not in STAFF_JS   # never the bare words as a label
    assert "<b>Ready for reservations</b>. Travel is checked last, on each group's page, and is not part of that." in STAFF_JS
    assert '<span class="badge badge--ready"' not in STAFF_JS                            # no green "ready" badge is drawn from stored data
    ready = STAFF_JS.split("function readinessHtml(g, travel) {", 1)[1].split("\n  }\n", 1)[0]
    assert ready.count('class="msg ok small"') == 1                                      # one green line only
    green = ready.split('class="msg ok small"', 1)[0].rsplit("\n", 1)[1]
    assert "t.state === 'confirmed'" in green and "g.intro && g.intro.current" in ready.split('class="msg ok small"', 1)[0]
    assert "<b>Provisionally ready: travel not confirmed.</b>" in ready and "data-travel=\"failed\"><b>Not ready to circulate.</b>" in ready
    assert ready.index("t.state === 'failed'") < ready.index("t.state === 'confirmed'")  # a failure is read first
    assert "{ state: 'provisional'" in ready                                             # nothing known reads provisional, never confirmed
    apply_ = STAFF_JS.split("const applyTravel = t => {", 1)[1].split("\n    };\n", 1)[0]
    assert "b.disabled = failed; b.textContent = failed ? 'Failed the travel check' : b.dataset.label;" in apply_
    for control in ("gate(p.querySelector('[data-status=\"Introduced\"]')", "gate(p.querySelector('[data-action=\"draft\"]')", "mailto();"):
        assert control in apply_, control
    assert "if (el.dataset.travelOff === 'set' && !offAnyway()) el.disabled = false;" in apply_   # never switches on what something else switched off
    assert "&& !travelFailed();" in STAFF_JS                                             # the email link follows the same check
    assert ".length} ready for reservations (travel not confirmed)$" in STAFF_JS and ".length} ready for reservations$" not in STAFF_JS   # the list's own count
    panel = STAFF_JS.split("function groupTravelPanel(box, g, onChange) {", 1)[1].split("\n  }\n", 1)[0]
    assert "const key = `${g.id}:${live.key || ''}:" in panel                            # what the page holds is held for one state of the group
    assert "if (reply && reply.ok && (live.key_unavailable || (live.key && reply.key !== live.key))) {" in panel   # a result for another state, or for a state the page could not name, is not used
    assert "if (kept.fail && kept.fail.offer) onChange({ state: 'failed'," in panel       # a known failure outlasts a re-check and the cleared figures
    assert panel.count("kept.fail = ") == 1 and "kept.fail = cur.state === 'failed' || ids.length ?" in panel   # set or lifted in ONE place: a completed check
    assert "kept.fail = null" not in panel and "kept.reply = null; kept.noteErr = true; kept.stale = true;" in panel   # a result that is not used lifts nothing
    assert "if (kept.at !== shownAt || !kept.reply) return;" in panel                      # the 30-minute clear leaves a reload or could-not-check note alone
    assert "kept.stale ? 'Reload the group to check travel for it as it stands.'" in panel   # and what it leaves switched off says to reload, not to check again
    assert "state: 'confirmed'" not in panel and "'confirmed'" not in panel               # a pass is never remembered or made up here
    assert STAFF_JS.count("ex.data.travel_refused") >= 3 and STAFF_JS.count("r.travel_at_action.plain") >= 4
    for code in ("H28", "GP6", "T6", "M4", "T4"):                                        # staff screens never quote register codes
        assert code not in ready and code not in apply_, code
