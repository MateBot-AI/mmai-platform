"""Live travel check (16 Nov 2026): journey times for the options on the page, worked out on request and shown once.

Synthetic provider replies only; no network. Covers: off by default; nothing from the provider is stored (every table is unchanged
apart from one count-only audit record); the saved AI review packet is unchanged; the travel rules (over the limit, missing
journeys) are applied to the live view; the three travel choices (walk, bicycle, one combined public transport choice) and
earlier answers that are kept as given and never widened; what each request asks for; the provider's attribution and walking
notice; the request allowances; out-of-date options are refused before any request; and the staff-only address.
"""
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from server import homebot_match as hm, homebot_review as hr, homebot_shortlist as hs, homebot_travel as tv, routing
from server.db import rows
from tests.test_homebot_shortlist import CUR, client, current, inventory, on, submit  # noqa: F401  (fixtures + helpers)

NOW = "2026-11-10T02:00:00+00:00"          # Tuesday 13:00 Melbourne -> departure Wednesday 11 Nov 08:00 (+11:00)
URL = "/api/homebot/enquiries/{}/options/travel-check"


SOURCE = {"title": "Route on Google Maps", "url": "https://www.google.com/maps/dir/?api=1&x=synthetic"}


class Provider:
    """Stand-in for the journey service: records every request body and answers by travel mode, in the service's own shape
    (a duration, a distance and an attribution; no legs, steps or vehicles)."""

    def __init__(self, transit="601s", walk="1500s", bicycle="900s", fail=False, source=SOURCE):
        self.transit, self.walk, self.bicycle, self.fail, self.source, self.bodies = transit, walk, bicycle, fail, source, []

    def __call__(self, body):
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("provider said: 12 Secret Street is unreachable")
        seconds = {"WALK": self.walk, "BICYCLE": self.bicycle, "TRANSIT": self.transit}[body["travelMode"]]
        route = {"duration": seconds, "distanceMeters": 4321}
        if self.source is not None:
            route["attribution"] = dict(self.source)
        return {"routes": [route]}


class StoredPathProvider:
    """The older reply shape read by the switched-off stored-evidence path only."""

    def __init__(self):
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(body)
        return {"routes": [{"duration": "1500s", "legs": [{"steps": [{"travelMode": "WALK"}]}]}]}


def _live_env(monkeypatch):
    monkeypatch.setenv(routing.AGENT_TOOLS_ENV, "1")
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    monkeypatch.delenv("MMAI_TRAVEL_AI_REPLY", raising=False)    # the AI travel reply has its own switch and its own tests
    monkeypatch.setenv("MMAI_ROUTING", "0")                      # the stored-evidence path stays off throughout
    monkeypatch.delenv("MMAI_ROUTING_RIGHTS_FILE", raising=False)
    for name in routing.LIVE_LIMITS:
        monkeypatch.delenv(name, raising=False)
    for name in ("_post", "_agent_tools_post"):
        monkeypatch.setattr(routing, name, lambda body: (_ for _ in ()).throw(AssertionError("real provider call in a test")))


@pytest.fixture
def live(on, monkeypatch):
    _live_env(monkeypatch)
    stock(on)
    eid = submit(on)
    assert hs.run_pending(on.db)
    assert json.loads(current(on, eid)["result_json"])["listed"]
    return on, eid


def stock(c):
    """The shared four-apartment stock, free from a date that suits the party whatever day the tests run (the party moves in
    40 days from today, give or take a week)."""
    inventory(c.db)
    c.db.execute("UPDATE apartments SET available_from=?", ((date.today() + timedelta(days=33)).isoformat(),))
    c.db.commit()


def snapshot(c, skip=("audit_log",)):
    """Every row of every table except the audit log, as text."""
    names = [r["name"] for r in rows(c.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))]
    return {n: json.dumps(rows(c.db.execute(f"SELECT * FROM {n} ORDER BY 1")), sort_keys=True, default=str) for n in names if n not in skip}


def audits(c):
    return rows(c.db.execute("SELECT * FROM audit_log WHERE action=? ORDER BY id", (hs.TRAVEL_CHECK_ACTION,)))


def everything(reply):
    return [x for key in ("listed", "alternatives", "unresolved") for x in reply[key]] + ([reply["fourth_option"]] if reply["fourth_option"] else [])


def test_off_by_default_no_request_and_no_record(on, monkeypatch):
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    for name in ("_post", "_agent_tools_post"):
        monkeypatch.setattr(routing, name, lambda body: (_ for _ in ()).throw(AssertionError("provider called while off")))
    stock(on)
    eid = submit(on)
    hs.run_pending(on.db)
    r = on.post(URL.format(eid), json={})
    assert r.status_code == 200 and r.json() == {"ok": False, "state": "off", "saved": False, "plain": "Live travel checks are switched off."}
    assert on.get(f"/api/homebot/enquiries/{eid}/options").json()["live_travel"]["enabled"] is False
    assert audits(on) == []
    # the switch alone is not enough: without the service's own key the check stays off
    monkeypatch.setenv(routing.AGENT_TOOLS_ENV, "1")
    monkeypatch.delenv(routing.AGENT_TOOLS_KEY_ENV)
    assert on.post(URL.format(eid), json={}).json()["state"] == "off"
    # and anything other than exactly "1" is off
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    for value in ("0", "true", "yes", "on", " 1", ""):
        monkeypatch.setenv(routing.AGENT_TOOLS_ENV, value)
        assert not routing.live_enabled(), value
    # the earlier switch and the earlier key turn nothing on, alone or together, and the earlier key is never used as this one
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV)
    monkeypatch.delenv(routing.AGENT_TOOLS_KEY_ENV)
    monkeypatch.setenv("MMAI_ROUTING_LIVE", "1")
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "an-older-key")
    assert not routing.live_enabled() and on.post(URL.format(eid), json={}).json()["state"] == "off"
    monkeypatch.setenv(routing.AGENT_TOOLS_ENV, "1")
    assert not routing.live_enabled()
    assert (routing.AGENT_TOOLS_ENV, routing.AGENT_TOOLS_KEY_ENV) == ("MMAI_TRAVEL_AGENT_TOOLS", "GOOGLE_MAPS_AGENT_TOOLS_API_KEY")
    assert audits(on) == []


def test_check_shows_journeys_and_stores_nothing_from_the_provider(live):
    c, eid = live
    before, packet_before = snapshot(c), json.dumps(hr.packet(c.db, current(c, eid)), sort_keys=True, default=str)
    stored_before = current(c, eid)["result_json"]
    assert "route_evidence" not in json.loads(stored_before)
    p = Provider()
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p)
    assert out["ok"] and out["state"] == "checked" and out["saved"] is False and out["shortlist_id"] == current(c, eid)["id"]
    # two buildings x four destinations x two accepted ways of travelling; the over-budget apartment is never measured
    assert len(p.bodies) == 16 and out["check"]["requests"] == 16 and out["check"]["measured"] == 16 and out["check"]["failures"] == 0
    assert not any("30 Pricey Lane" in b["origin"]["address"] for b in p.bodies)
    assert not any("Unit" in b["origin"]["address"] for b in p.bodies)            # the building, not each apartment
    assert out["check"]["attribution"] == "Google Maps"
    assert out["check"]["basis"] == "Estimated weekday departure at 08:00 Melbourne; not a guaranteed journey time"
    assert out["check"]["departure"] == "2026-11-11T08:00:00+11:00"
    assert sorted({b["travelMode"] for b in p.bodies}) == ["TRANSIT", "WALK"]
    # every request: the weekday 08:00 departure and nothing beyond the five plain fields (no vehicle filter exists to send)
    assert all(b["departureTime"] == "2026-11-10T21:00:00Z" and set(b) == routing.AGENT_TOOLS_REQUEST_FIELDS for b in p.bodies)
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.PASS for x in shown)
    person = shown[0]["travel"]["facts"]["people"][0]
    assert person["fastest_modes"] == ["Public transport"] and person["fastest_minutes"] is not None   # the faster accepted way is named
    # the provider's attribution, exactly as sent and once, and its walking notice, travel with each option's figures
    for x in shown:
        assert x["travel_sources"] == {"links": [SOURCE], "walking_warning": routing.WALK_WARNING}
    assert routing.WALK_WARNING == "WALK routes are in beta and might sometimes be missing clear sidewalks or pedestrian paths."
    assert out["ai_reply"]["state"] == "off" and out["ai_reply"]["options"] == []           # the AI travel reply has its own switch
    assert all("Travel time needs confirmation" not in " ".join(x["reasons"]) for x in shown)
    # nothing but one count-only audit record was written
    assert snapshot(c) == before
    assert current(c, eid)["result_json"] == stored_before
    assert json.dumps(hr.packet(c.db, current(c, eid)), sort_keys=True, default=str) == packet_before
    (a,) = audits(c)
    detail = json.loads(a["detail"])
    # our own request counts only: nothing that depends on what the provider answered
    assert detail == {"scenario": CUR, "shortlist_id": out["shortlist_id"], "requests": 16, "not_requested": 0}
    assert a["staff_member"] == "Anna" and a["entity"] == "homebot_enquiry" and a["entity_id"] == str(eid)
    everywhere = json.dumps(rows(c.db.execute("SELECT * FROM audit_log")), default=str) + json.dumps(snapshot(c))
    # random password and session digests can spell any short number by chance; they are not provider content
    everywhere = re.sub(r"[0-9a-f]{16,}", "", everywhere)
    for leaked in ("601", "10.0166", "1500s", "4321", "fastest_minutes\": \"", "google-routes", "synthetic", "Route on Google Maps"):
        assert leaked not in everywhere, leaked


def test_stored_options_still_say_travel_needs_confirmation(live):
    c, eid = live
    hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    cur = c.get(f"/api/homebot/enquiries/{eid}/options").json()["scenarios"][CUR]["current"]
    assert cur["route_evidence"] is None
    assert cur["listed"] and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in cur["listed"])


def test_over_the_travel_limit_is_ruled_out_in_the_live_view_only(live):
    c, eid = live
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(transit="7200s", walk="9000s"))   # two hours each way
    assert out["ok"] and everything(out) == []
    assert len(out["why_not"]) == 4 and all(x["class"] == hm.EXCLUDED for x in out["why_not"])
    assert sum(any("travel" in reason.lower() or "journey" in reason.lower() for reason in x["reasons"]) for x in out["why_not"]) >= 3
    assert json.loads(current(c, eid)["result_json"])["listed"]          # the stored options are untouched


def test_missing_or_failed_journeys_read_needs_confirmation(live, caplog):
    c, eid = live
    p = Provider(fail=True)
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p)
    assert out["ok"] and out["check"]["requests"] == 16 and out["check"]["measured"] == 0 and out["check"]["failures"] == 16
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in shown)
    assert all(any("ravel" in reason for reason in x["reasons"]) for x in shown)
    assert "Secret Street" not in json.dumps(out) and "Secret Street" not in caplog.text
    assert "Secret Street" not in json.dumps(rows(c.db.execute("SELECT * FROM audit_log")), default=str)
    assert json.loads(audits(c)[0]["detail"])["requests"] == 16           # failed requests still count against the allowance


def test_public_transport_is_one_combined_choice_whose_vehicle_cannot_be_checked(live):
    """The service returns no legs or vehicles, so a public transport journey is accepted as measured without knowing what
    it travels by. That is why the choice is the combined one and why an earlier Tram, Train or Bus answer is never measured."""
    c, eid = live
    p = Provider()
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p)
    assert out["check"]["measured"] == 16
    assert not any(key in json.dumps(p.bodies) for key in ("transitPreferences", "allowedTravelModes", "LIGHT_RAIL", "routingPreference", "Polyline"))
    assert set(routing.AGENT_TOOLS_MODES.values()) == {"WALK", "BICYCLE", "TRANSIT"}
    assert not any(old in routing.AGENT_TOOLS_MODES for old in ("Tram", "Train", "Bus", "Car"))


def test_a_route_whose_attribution_cannot_be_shown_is_not_used(live):
    c, eid = live
    for bad in ({"title": "Route", "url": "http://www.google.com/maps"}, {"title": "Route", "url": "https://example.com/maps"},
                {"title": "Route", "url": "https://www.google.com.example.com/"}, {"title": "", "url": "https://www.google.com/maps"},
                {"title": "Route", "url": "javascript:alert(1)"}, {"title": "Route", "url": "https://www.google.com/maps\"><script>"},
                {"title": "x" * 201, "url": "https://www.google.com/maps"}, {"title": "Route"}, "Google Maps"):
        out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(source=bad))
        assert out["check"]["requests"] == 16 and out["check"]["measured"] == 0, bad
        assert all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION and x["travel_sources"]["links"] == [] for x in everything(out)), bad
    # a route that came with no attribution at all is still measured; the page's own "Google Maps" wording covers it
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(source=None))
    assert out["check"]["measured"] == 16 and all(x["travel_sources"]["links"] == [] for x in everything(out))
    for good in ("https://maps.google.com/?cid=1", "https://www.google.com/maps/dir/a/b", "https://maps.app.goo.gl/abc", "https://www.google.com.au/maps"):
        assert routing.route_source({"attribution": {"title": " A ", "url": good}}) == ("A", good)
    # the fastest usable route is the one counted, and its own attribution is the one shown
    fast, slow = {"title": "Fast", "url": "https://maps.google.com/f"}, {"title": "Slow", "url": "https://maps.google.com/s"}
    minutes, source = routing._fastest_route({"routes": [{"duration": "900s", "attribution": slow}, {"duration": "bad"},
                                                         {"duration": "600s", "attribution": fast}]})
    assert minutes == 10 and source == ("Fast", "https://maps.google.com/f")
    assert routing._fastest_route({"routes": []}) == (tv.UNMEASURED, None) and routing._fastest_route({}) == (tv.UNMEASURED, None)


def test_request_allowances(live, monkeypatch):
    c, eid = live
    monkeypatch.setenv("MMAI_ROUTING_LIVE_MAX_REQUESTS", "5")
    p = Provider()
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p)
    assert len(p.bodies) == 5 and out["check"]["requests"] == 5 and out["check"]["not_requested"] == 11
    assert any(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in everything(out))
    # daily allowance: 5 used, 7 allowed -> only 2 more requests today, then none
    monkeypatch.setenv("MMAI_ROUTING_LIVE_DAILY_REQUESTS", "7")
    monkeypatch.setattr(hs, "now_iso", lambda: NOW, raising=True)
    c.db.execute("UPDATE audit_log SET at=? WHERE action=?", (NOW, hs.TRAVEL_CHECK_ACTION))
    c.db.commit()
    p2 = Provider()
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p2)["check"]["requests"] == 2 and len(p2.bodies) == 2
    c.db.execute("UPDATE audit_log SET at=? WHERE action=?", (NOW, hs.TRAVEL_CHECK_ACTION))
    c.db.commit()
    p3 = Provider()
    refused = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p3)
    assert refused["ok"] is False and refused["state"] == "limit" and "today" in refused["plain"] and p3.bodies == []
    assert len(audits(c)) == 2                                            # a refusal makes no request and no record
    # the next day is allowed again, until the monthly allowance is reached
    tomorrow = "2026-11-11T02:00:00+00:00"
    monkeypatch.setenv("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS", "7")
    p4 = Provider()
    refused = hs.live_travel_check(c.db, eid, CUR, "Anna", now=tomorrow, post=p4)
    assert refused["state"] == "limit" and "this month" in refused["plain"] and p4.bodies == []
    monkeypatch.setenv("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS", "9")
    p5 = Provider()
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=tomorrow, post=p5)["check"]["requests"] == 2
    # a new month starts a new allowance
    p6 = Provider()
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now="2026-12-01T02:00:00+00:00", post=p6)["check"]["requests"] == 5


def test_limit_settings_fall_back_to_safe_defaults(monkeypatch):
    for bad in ("0", "-5", "abc", "999999999", "1.5"):
        monkeypatch.setenv("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS", bad)
        assert routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") == 4000, bad
    monkeypatch.setenv("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS", "2500")
    assert routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") == 2500


def test_default_limits_and_what_each_request_asks_for(live, monkeypatch):
    """The default monthly limit stays under the service's published no-charge monthly allowance, and no request asks for
    the service's dearer option (traffic-aware routing) or anything else beyond the five plain fields."""
    assert {k: v[0] for k, v in routing.LIVE_LIMITS.items()} == {
        "MMAI_ROUTING_LIVE_MAX_REQUESTS": 200, "MMAI_ROUTING_LIVE_DAILY_REQUESTS": 1000, "MMAI_ROUTING_LIVE_MONTHLY_REQUESTS": 4000}
    assert routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") < 10000
    assert routing.AGENT_TOOLS_ENDPOINT == "https://mapstools.googleapis.com/v1:computeRoutes"
    assert routing.AGENT_TOOLS_MODES == {"Walk": "WALK", "Bicycle": "BICYCLE", "Bike or scooter": "BICYCLE", "Public transport": "TRANSIT"}
    assert routing.AGENT_TOOLS_REQUEST_FIELDS == {"origin", "destination", "travelMode", "computeAlternativeRoutes", "departureTime"}
    when = routing.departure_at(datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc))
    for mode in ("WALK", "BICYCLE", "TRANSIT"):
        body = routing.agent_tools_request("1 Test Street, Melbourne", "2 Test Street, Melbourne", mode, when)
        assert set(body) == routing.AGENT_TOOLS_REQUEST_FIELDS and body["travelMode"] == mode
        assert body["origin"] == {"address": "1 Test Street, Melbourne"} and body["departureTime"] == "2026-11-10T21:00:00Z"
    for mode in ("Car", "DRIVE", "TWO_WHEELER", "Tram", "Walk", "walk", ""):
        with pytest.raises(ValueError):
            routing.agent_tools_request("1 Test Street, Melbourne", "2 Test Street, Melbourne", mode, when)
    # the sender itself refuses a dearer or unlisted field and a car journey before anything leaves, and needs the key
    import httpx
    monkeypatch.undo()
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("a request left the process")))
    good = routing.agent_tools_request("1 Test Street, Melbourne", "2 Test Street, Melbourne", "WALK", when)
    for extra in ({"routingPreference": "ROUTING_PREFERENCE_TRAFFIC_AWARE_OPTIMAL"}, {"includePolyline": True},
                  {"intermediateWaypoints": []}, {"travelMode": "DRIVE"}, {"transitPreferences": {}}):
        with pytest.raises(ValueError):
            routing._agent_tools_post({**good, **extra})
    monkeypatch.delenv(routing.AGENT_TOOLS_KEY_ENV)
    with pytest.raises(RuntimeError):
        routing._agent_tools_post(good)


def _party(on, monkeypatch, transport):
    """An enquiry with this transport answer. Tram, Train, Bus, Bike or scooter and Car are no longer offered, so an enquiry
    holding one is made the way an old one was: with them among the offered choices at the moment it was sent."""
    from server import homebot
    _live_env(monkeypatch)
    stock(on)
    with monkeypatch.context() as then:
        then.setattr(homebot, "TRANSPORT", homebot.TRANSPORT + list(homebot.TRANSPORT_RETIRED))
        eid = submit(on, transport=transport)
    assert homebot.TRANSPORT == ["Walk", "Bicycle", "Public transport"]
    assert hs.run_pending(on.db)
    return eid


NEW_CHOICES_TEXT = "The travel choices are now walk, bicycle and public transport. Please choose from those."


def test_the_form_offers_walk_bicycle_and_public_transport_only(on):
    from server import homebot
    from tests.test_homebot import body as form_body
    assert homebot.TRANSPORT == ["Walk", "Bicycle", "Public transport"]
    assert homebot.TRANSPORT_RETIRED == ("Tram", "Train", "Bus", "Bike or scooter", "Car")
    assert all(choice in routing.AGENT_TOOLS_MODES for choice in homebot.TRANSPORT)        # what is offered is what is measured
    assert list(tv.TRANSPORT) == homebot.TRANSPORT + list(homebot.TRANSPORT_RETIRED)       # old answers can still be read
    assert on.get("/api/public/homebot/meta").json()["choices"]["transport"] == homebot.TRANSPORT
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    page = open(os.path.join(root, "homebot-app", "index.html"), encoding="utf-8").read()
    import re
    assert re.findall(r'name="transport" value="([^"]+)">([^<]+)<', page) == [(x, x) for x in homebot.TRANSPORT]
    before = on.db.execute("SELECT COUNT(*) AS n FROM homebot_enquiries").fetchone()["n"]
    old_answers = (["Car"], ["Tram", "Car"], "Car", ["Tram"], ["Train"], ["Bus"], ["Bike or scooter"], ["Walk", "Bus"], ["Public transport", "Tram"])
    for answer in old_answers:
        with pytest.raises(homebot.ValidationError) as err:
            homebot.validate(form_body(transport=answer, move_out="2027-11-30"))
        assert err.value.errors == {"transport": NEW_CHOICES_TEXT}, answer
    with pytest.raises(homebot.ValidationError) as err:                                    # any other unknown answer keeps the usual message
        homebot.validate(form_body(transport=["Teleport"], move_out="2027-11-30"))
    assert err.value.errors == {"transport": "Not one of the offered choices."}
    # the public form's own address refuses them too, in plain words, and stores nothing; each offered choice is stored
    from tests.test_homebot import post as form_post
    count = lambda: on.db.execute("SELECT COUNT(*) AS n FROM homebot_enquiries").fetchone()["n"]
    for i, answer in enumerate(a for a in old_answers if isinstance(a, list)):
        r = form_post(on, form_body(transport=answer, move_out="2027-11-30"), f"hb-old-key-{i:04d}")
        assert r.status_code == 422 and NEW_CHOICES_TEXT in r.text, r.text
        assert count() == before
    r = form_post(on, form_body(transport=["Walk", "Bicycle", "Public transport"], move_out="2027-11-30"), "hb-new-key-0009")
    assert r.status_code == 201 and count() == before + 1, r.text
    assert homebot.get(on.db, r.json()["id"])["transport"] == ["Walk", "Bicycle", "Public transport"]


def test_an_earlier_answer_stays_until_staff_untick_it_and_can_never_be_added(on, monkeypatch):
    from server import homebot
    eid = _party(on, monkeypatch, ["Tram", "Car"])
    # an unrelated change keeps the answer exactly as given
    hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna")
    assert homebot.get(on.db, eid)["transport"] == ["Tram", "Car"]
    # an earlier answer that was never on this enquiry cannot be added beside it
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Tram", "Car", "Bus"]})
    assert r.status_code == 422 and NEW_CHOICES_TEXT in r.text
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Tram", "Car", "Walk"]})   # an offered choice can be
    assert r.status_code == 200 and r.json()["transport"] == ["Tram", "Car", "Walk"], r.text
    # staff untick an earlier answer once the applicant has said how they will travel
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Tram", "Walk"]})
    assert r.status_code == 200 and r.json()["transport"] == ["Tram", "Walk"], r.text
    # once gone it cannot come back, and it cannot be given to an enquiry that never had it
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Tram", "Car"]})
    assert r.status_code == 422 and NEW_CHOICES_TEXT in r.text
    assert homebot.get(on.db, eid)["transport"] == ["Tram", "Walk"]
    # Tram becomes Public transport only when staff make that change themselves; then Tram cannot come back either
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Public transport", "Walk"]})
    assert r.status_code == 200 and r.json()["transport"] == ["Public transport", "Walk"], r.text
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Tram"]})
    assert r.status_code == 422 and NEW_CHOICES_TEXT in r.text
    other = submit(on, key="hb-key-0002")
    for answer in (["Car"], ["Tram"], ["Bike or scooter"]):
        r = on.patch(f"/api/homebot/enquiries/{other}/requirements", json={"transport": answer})
        assert r.status_code == 422 and NEW_CHOICES_TEXT in r.text, answer
    js = open(__import__("os").path.join(__import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))),
                                         "client", "staff.js"), encoding="utf-8").read()
    assert "(ch.transport || []).concat((e.transport || []).filter(o => !(ch.transport || []).includes(o)))" in js   # the box still shows it
    assert "Leave the earlier answer ticked unless the applicant has told you how they will travel" in js
    assert "They are not treated as “Public transport”" in js


@pytest.mark.parametrize("old", ["Tram", "Train", "Bus"])
def test_an_earlier_tram_train_or_bus_answer_is_never_widened_to_public_transport(on, monkeypatch, old):
    """A party that answered Walk and Tram (or Train, or Bus) before the choices changed: only walking is requested, never
    a public transport journey, and the result is neither a confirmed pass nor a rejection, however short the walk."""
    eid = _party(on, monkeypatch, ["Walk", old])
    p = Provider(walk="120s", transit="60s")
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert out["ok"] and len(p.bodies) == 8 and out["check"]["requests"] == 8             # two buildings x four destinations x walking
    assert {b["travelMode"] for b in p.bodies} == {"WALK"}
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in shown)
    for x in shown:
        assert x["travel"]["facts"]["accepted_modes"] == ["Walk", old]
        for person in x["travel"]["facts"]["people"]:
            if person["status"] != tv.NO_DESTINATION:
                assert person["unknown_modes"] == [old] and person["fastest_modes"] == [], person
    # nothing is ruled out on travel: the only travel wording anywhere is the needs-confirmation line
    for x in out["why_not"] + shown:
        assert all(r.startswith("Travel time needs confirmation") for r in x["reasons"] if "ravel" in r or "ourney" in r), x["reasons"]


@pytest.mark.parametrize("answer", [["Tram"], ["Train", "Bus"], ["Tram", "Train", "Bus"], ["Car"], ["Tram", "Car"]])
def test_a_party_with_only_earlier_answers_gets_no_request_and_needs_confirmation(on, monkeypatch, answer):
    """All three of tram, train and bus together is still not the combined choice: nobody was asked about it."""
    eid = _party(on, monkeypatch, answer)
    p = Provider()
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert p.bodies == []                                                                 # nothing is asked of the provider
    assert out["ok"] and out["check"]["requests"] == 0 and out["check"]["measured"] == 0 and out["check"]["not_requested"] == 0
    assert audits(on) == []                                                               # no request, so no usage on record
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in shown)
    assert all(person["unknown_modes"] == answer for x in shown for person in x["travel"]["facts"]["people"]
               if person["status"] != tv.NO_DESTINATION)
    assert all(x["travel_sources"] == {"links": [], "walking_warning": None} for x in shown)
    stored = json.loads(current(on, eid)["result_json"])
    assert stored["listed"] and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in stored["listed"])


def test_bicycle_and_the_earlier_bike_or_scooter_answer_are_one_bicycle_journey(on, monkeypatch):
    eid = _party(on, monkeypatch, ["Bike or scooter"])
    p = Provider()
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert len(p.bodies) == 8 and {b["travelMode"] for b in p.bodies} == {"BICYCLE"}
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.PASS for x in shown)
    assert all(x["travel_sources"]["walking_warning"] is None for x in shown)             # no walking route was shown
    # staff tick the offered name beside the earlier one: still one request per journey, not two
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"transport": ["Bike or scooter", "Bicycle"]})
    assert r.status_code == 200, r.text
    assert hs.run_pending(on.db)
    p2 = Provider()
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p2)
    assert out["ok"] and len(p2.bodies) == 8 and {b["travelMode"] for b in p2.bodies} == {"BICYCLE"}
    assert all(x["travel"]["status"] == tv.PASS for x in everything(out))


def test_stored_journey_path_never_requests_car(monkeypatch):
    """The stored-evidence path (switched off, and not the service used for live checks) never requests a car journey."""
    from types import SimpleNamespace
    req = SimpleNamespace(transport=("Walk", "Car"), people=[{"position": 1, "doing": "Working", "place": "1 Work Street, Melbourne"}])
    inv = [{"id": 7, "address": "10 Test Street", "suburb": "Carlton"}]
    policy = {"max_requests_per_job": 50, "fresh_seconds": 3600, "valid_until": "2099-01-01T00:00:00Z", "digest": "x"}
    p = StoredPathProvider()
    est = routing.Estimates(req, inv, policy, now=datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc), post=p)
    got = est(7, 1, "primary")
    assert [b["travelMode"] for b in p.bodies] == ["WALK"] and est.calls == 1
    assert "Car" not in got and got["Walk"] is not tv.UNMEASURED


def test_the_request_count_is_on_record_before_the_first_request_leaves(live, monkeypatch):
    c, eid = live
    seen = {}

    def stopped(self, *a, **k):
        seen["rows"] = [json.loads(r["detail"]) for r in audits(c)]
        raise RuntimeError("the process stopped while journeys were being requested")

    monkeypatch.setattr(routing.LiveEstimates, "prefetch", stopped)
    with pytest.raises(RuntimeError):
        hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert [r["requests"] for r in seen["rows"]] == [16]                  # already recorded when the requests were about to start
    assert [json.loads(r["detail"])["requests"] for r in audits(c)] == [16]   # and still held afterwards


def test_an_unreadable_usage_record_tightens_the_allowance(live):
    c, eid = live
    c.db.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
                 (NOW, "x", hs.TRAVEL_CHECK_ACTION, "homebot_enquiry", str(eid), "not json"))
    c.db.commit()
    assert hs._travel_requests_used(c.db, "2026-11-10") == 500


def test_out_of_date_options_are_refused_before_any_request(live):
    c, eid = live
    c.db.execute("UPDATE apartments SET rent_12m=rent_12m+1 WHERE id=1")
    c.db.commit()
    p = Provider()
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=p)
    assert out["ok"] is False and out["state"] == "out_of_date" and "apartment list changed" in out["plain"]
    assert p.bodies == [] and audits(c) == []


def test_no_prepared_options_and_busy(on, monkeypatch):
    monkeypatch.setenv(routing.AGENT_TOOLS_ENV, "1")
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    stock(on)
    eid = submit(on)                                                      # queued, not yet run
    p = Provider()
    assert hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)["state"] == "no_options"

    class Held:
        def acquire(self, timeout=None):
            return False

        def release(self):
            raise AssertionError("released a lock that was never taken")
    monkeypatch.setattr(hs, "_travel_lock", Held())
    assert hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)["state"] == "busy"
    assert p.bodies == []


def test_a_slow_provider_is_cut_off_and_counted(live, monkeypatch):
    import threading
    c, eid = live
    release = threading.Event()

    def slow(body):
        release.wait(5)
        raise RuntimeError("late")
    monkeypatch.setattr(routing, "LIVE_DEADLINE_SEC", 0.2)
    req_est = routing.LiveEstimates.prefetch
    monkeypatch.setattr(routing.LiveEstimates, "prefetch", lambda self, ids=None, **kw: req_est(self, ids, deadline_sec=0.2))
    try:
        out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=slow)
    finally:
        release.set()
    assert out["ok"] and out["check"]["requests"] == 16 and out["check"]["measured"] == 0 and out["check"]["failures"] == 16
    assert json.loads(audits(c)[0]["detail"])["requests"] == 16


def test_live_estimates_can_never_be_stored(live):
    c, eid = live
    est = routing.LiveEstimates(hs._requirements(c.db, hs._capture(c.db, eid), CUR), hs.inventory_rows(c.db), max_requests=3, post=Provider())
    with pytest.raises(RuntimeError):
        est.evidence()
    assert est(1, 1, "primary") == {"Public transport": tv.UNMEASURED, "Walk": tv.UNMEASURED}     # nothing is requested outside prefetch
    assert est.calls == 0


def test_address_is_staff_only_and_validates(live, monkeypatch):
    c, eid = live
    monkeypatch.setattr(routing, "_agent_tools_post", Provider())
    assert c.anon.post(URL.format(eid), json={}).status_code == 401
    assert audits(c) == []
    assert c.post(URL.format(eid), json={"scenario": "nonsense"}).status_code == 422
    assert c.post(URL.format(999999), json={}).status_code == 404
    r = c.post(URL.format(eid), json={"scenario": CUR})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["check"]["requests"] == 16
    assert "no-store" in r.headers.get("cache-control", "")
    ov = c.get(f"/api/homebot/enquiries/{eid}/options").json()
    assert ov["live_travel"] == {"enabled": True, "auto": True, "plain": hs.TRAVEL_NOT_SAVED, "ai_reply": False}
    monkeypatch.setenv("MMAI_ROUTING_LIVE_AUTO", "0")
    assert c.get(f"/api/homebot/enquiries/{eid}/options").json()["live_travel"]["auto"] is False
    monkeypatch.setenv(hs.GATE_ENV, "0")                                  # matching switched off: the address answers as the others do
    assert c.post(URL.format(eid), json={}).status_code == 409


def test_page_wording_and_attribution():
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "client", "staff.js"), encoding="utf-8").read()
    start = js.index("const travelView = r =>")
    block = js[start:js.index("const scenario = (key, sc) =>", start)]
    attr = '<img class="gmaps-attr" src="/img/google_maps_logo.svg" alt="Google Maps" translate="no" width="98" height="18">'
    assert f"const GMAPS_ATTR = '{attr}';" in js                           # the provider's logo, labelled with its untranslated name
    assert "is not measured; confirm with the applicant how they will travel" in js   # why an earlier answer reads needs confirmation
    assert "const HB_TRANSPORT_EARLIER = ['Tram', 'Train', 'Bus', 'Car'];" in js
    assert '<p class="small">${GMAPS_ATTR}<br>Journey estimates.' in block  # at the head of the live options
    import hashlib
    logo = open(os.path.join(root, "client", "img", "google_maps_logo.svg"), "rb").read()   # the provider's own file, byte for byte
    assert hashlib.sha256(logo).hexdigest() == "62c52df68d8f450c860d5d65068fc96d63c2dfaec849b91ac64b3256a898936e"
    figures = js[js.index("const journeys = x =>"):js.index("const candRows =")]
    assert "${shown ? '<br>' + GMAPS_ATTR : ''}" in figures                # and beside each apartment's journey figures
    assert "person.fastest_minutes != null" in figures.split("const shown =")[1] and "f.majority" in figures.split("const shown =")[1]
    assert "localStorage" not in block and "sessionStorage" not in block and "indexedDB" not in block
    assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", block)       # plain staff language, no register codes
    css = open(os.path.join(root, "client", "styles.css"), encoding="utf-8").read()
    rule = css[css.index(".gmaps-attr {"):].split("}")[0]
    assert "font-size: 14px" in rule and "color: #1F1F1F" in rule and "Roboto" in rule and "font-weight: 400" in rule
    assert "height: 18px" in rule and "margin: 10px 10px 5px" in rule       # logo size and the clear space around it
    # journey times are dropped from the page's memory after 30 minutes whether or not the page is opened again
    assert "hbTravelShown.forEach(kept => { if (kept.reply && Date.now() - kept.at > HB_TRAVEL_KEEP_MS) kept.reply = null; });" in js
    assert "setTimeout(() => {\n          if (kept.at !== shownAt) return;\n          kept.reply = null;" in js
    assert "recorded with the saved details for that apartment, where travel time still reads as needing confirmation" in js
    assert "localStorage" not in js[js.index("const hbTravelShown"):js.index("const hbTravelShown") + 400]
    for text in (hs.TRAVEL_NOT_SAVED, routing.LIVE_BASIS, routing.LIVE_OFF):
        assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", text)


# ------------------------------------------------------------------------------------------------- travel checked last
class ByBuilding(Provider):
    """Journeys from the named buildings are far too long (over double the 30-minute limit); the rest are as usual."""

    def __init__(self, far=()):
        super().__init__()
        self.far = tuple(far)

    def __call__(self, body):
        out = super().__call__(body)
        if any(b in body["origin"]["address"] for b in self.far):
            out["routes"][0]["duration"] = "7300s"
        return out


def _five_buildings(on, monkeypatch):
    from tests.test_homebot_shortlist import _apt
    _live_env(monkeypatch)
    on.db.execute("DELETE FROM apartments")
    for i in (1, 2, 3, 4, 5):
        _apt(on.db, i, rent12=900 + 10 * i, building=f"{i}0 Stage Street")
    on.db.commit()
    eid = submit(on)
    assert hs.run_pending(on.db)
    return eid


def _buildings(p):
    return [b for b in dict.fromkeys(body["origin"]["address"].split(",")[0] for body in p.bodies)]


def test_travel_is_measured_last_and_only_for_the_three_shortlisted(on, monkeypatch):
    eid = _five_buildings(on, monkeypatch)
    stored = json.loads(current(on, eid)["result_json"])
    assert [x["apartment_id"] for x in stored["listed"]] == [1, 2, 3] and stored["fourth_option"] is None
    p = Provider()
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert [x["apartment_id"] for x in out["listed"]] == [1, 2, 3] and out["fourth_option"] is None and out["alternatives"] == []
    assert sorted(_buildings(p)) == ["10 Stage Street", "20 Stage Street", "30 Stage Street"]      # the other two are never sent
    assert out["check"]["requests"] == len(p.bodies) == 24 and out["check"]["stages"] == 1
    (a,) = audits(on)
    assert json.loads(a["detail"]) == {"scenario": CUR, "shortlist_id": out["shortlist_id"], "requests": 24, "not_requested": 0}
    left = {x["apartment_id"]: x for x in out["why_not"]}
    assert set(left) == {4, 5} and out["summary"]["travel_not_checked"] == 2 and out["unresolved_not_shown"] == 0
    for x in left.values():   # not shortlisted, so not measured: says so, and is never shown as meeting the travel limit
        assert x["class"] == hm.NEEDS_CONFIRMATION and hm.PLAIN[hm.TRAVEL_NOT_CHECKED] in x["notes"]
    # no AI assessment was made (its switch is off), so this is held: journey figures alone are not a ready shortlist
    assert out["ai_gate"]["state"] == "held" and out["ai_gate"]["held"] is True and out["ai_gate"]["reason"] == "off"
    assert out["ai_gate"]["plain"].startswith("Held, not a ready shortlist: ")


def test_a_shortlisted_apartment_that_fails_travel_is_replaced_and_the_replacement_is_measured(on, monkeypatch):
    eid = _five_buildings(on, monkeypatch)
    stored_before = current(on, eid)["result_json"]
    p = ByBuilding(far=["20 Stage Street"])
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert [x["apartment_id"] for x in out["listed"]] == [1, 3, 4]                       # 1 and 3 are kept; 4 steps in
    assert all(x["travel"]["status"] == tv.PASS for x in out["listed"])
    assert _buildings(p)[-1] == "40 Stage Street" and "50 Stage Street" not in _buildings(p)   # the replacement is measured, last
    assert out["check"]["requests"] == len(p.bodies) == 32 and out["check"]["stages"] == 2
    # the allowance is held before each stage leaves: counts only, nothing about which apartment or why
    assert [json.loads(a["detail"])["requests"] for a in audits(on)] == [24, 8]
    assert all(set(json.loads(a["detail"])) == {"scenario", "shortlist_id", "requests", "not_requested"} for a in audits(on))
    left = {x["apartment_id"]: x for x in out["why_not"]}
    assert left[2]["class"] == hm.EXCLUDED and hm.PLAIN[hm.TRAVEL_EXCEEDS_CEILING] in left[2]["reasons"]
    assert hm.PLAIN[hm.REPLACED_AFTER_TRAVEL] in left[2]["notes"] and hm.PLAIN[hm.TRAVEL_NOT_CHECKED] in left[5]["notes"]
    assert hm.PLAIN[hm.REPLACEMENT_AFTER_TRAVEL] in out["listed"][2]["notes"]
    assert current(on, eid)["result_json"] == stored_before                                # the saved options are untouched
    # every shortlisted apartment failing leaves a shorter list, never a padded one
    p = ByBuilding(far=["10 Stage Street", "20 Stage Street", "30 Stage Street", "40 Stage Street"])
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=p)
    assert [x["apartment_id"] for x in out["listed"]] == [5] and out["check"]["stages"] == 2 and len(p.bodies) == 40


def test_never_more_than_three_rows_are_shown_whichever_heading_they_sit_under():
    """Options first, then permitted alternatives, then apartments that could not be checked automatically, into the room
    left. The rest stay in the list of other apartments; nothing leaves the saved result."""
    row = lambda i: {"apartment_id": i}   # noqa: E731
    ids = lambda parts: [[x["apartment_id"] for x in part] for part in parts]   # noqa: E731
    res = {"listed": [row(1), row(2), row(3)], "alternatives": [row(4)], "unresolved": [row(5), row(6)]}
    assert ids(hs._three_rows(res)) == [[1, 2, 3], [], []]
    assert ids(hs._three_rows({"listed": [row(1)], "alternatives": [row(4)], "unresolved": [row(5), row(6)]})) == [[1], [4], [5]]
    assert ids(hs._three_rows({"listed": [], "alternatives": [], "unresolved": [row(5), row(6), row(7), row(8)]})) == [[], [], [5, 6, 7]]
    assert ids(hs._three_rows({"listed": [row(1), row(2), row(3), row(9)], "alternatives": [], "unresolved": []})) == [[1, 2, 3], [], []]
    assert ids(hs._three_rows({})) == [[], [], []]
    assert len(res["unresolved"]) == 2 and len(res["alternatives"]) == 1          # the result itself is not changed


def test_the_time_budget_covers_every_stage_of_one_check(on, monkeypatch):
    eid = _five_buildings(on, monkeypatch)
    del eid
    inv = hs.inventory_rows(on.db)
    e = hs._capture(on.db, 1)
    est = routing.LiveEstimates(hs._requirements(on.db, e, CUR), inv, max_requests=100, now=None, post=Provider())
    sent = []
    est.before_send = sent.append
    assert est.prepare([1]) == 8 and sent == [8] and est.stages == 1
    assert est.prepare([1]) == 0 and sent == [8]                    # already measured: nothing is requested twice
    est.deadline_sec = 0                                            # the budget is spent
    assert est.prepare([2]) == 0 and sent == [8] and est.calls == 8 and est.skipped == 8
    assert all(v is tv.UNMEASURED for v in est(2, 1, "primary").values())   # not measured: reads as needing confirmation
