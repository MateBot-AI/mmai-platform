"""Live travel check (16 Nov 2026): journey times for the options on the page, worked out on request and shown once.

Synthetic provider replies only; no network. Covers: off by default; nothing from the provider is stored (every table is unchanged
apart from one count-only audit record); the AI review packet is unchanged; the travel rules (over the limit, missing journeys,
wrong vehicle) are applied to the live view; the request allowances; out-of-date options are refused before any request; and the
staff-only address.
"""
import json
from datetime import date, timedelta

import pytest

from server import homebot_match as hm, homebot_review as hr, homebot_shortlist as hs, homebot_travel as tv, routing
from server.db import rows
from tests.test_homebot_shortlist import CUR, client, current, inventory, on, submit  # noqa: F401  (fixtures + helpers)

NOW = "2026-11-10T02:00:00+00:00"          # Tuesday 13:00 Melbourne -> departure Wednesday 11 Nov 08:00 (+11:00)
URL = "/api/homebot/enquiries/{}/options/travel-check"


class Provider:
    """Stand-in for the journey provider: records every request body and answers by travel mode."""

    def __init__(self, tram="601s", walk="1500s", vehicle="TRAM", fail=False):
        self.tram, self.walk, self.vehicle, self.fail, self.bodies = tram, walk, vehicle, fail, []

    def __call__(self, body):
        self.bodies.append(body)
        if self.fail:
            raise RuntimeError("provider said: 12 Secret Street is unreachable")
        if body["travelMode"] == "WALK":
            return {"routes": [{"duration": self.walk, "legs": [{"steps": [{"travelMode": "WALK"}]}]}]}
        return {"routes": [{"duration": self.tram, "legs": [{"steps": [
            {"travelMode": "WALK"}, {"travelMode": "TRANSIT", "transitDetails": {"transitLine": {"vehicle": {"type": self.vehicle}}}}]}]}]}


@pytest.fixture
def live(on, monkeypatch):
    monkeypatch.setenv("MMAI_ROUTING_LIVE", "1")
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "test-key-not-real")
    monkeypatch.setenv("MMAI_ROUTING", "0")                      # the stored-evidence path stays off throughout
    monkeypatch.delenv("MMAI_ROUTING_RIGHTS_FILE", raising=False)
    for name in routing.LIVE_LIMITS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(routing, "_post", lambda body: (_ for _ in ()).throw(AssertionError("real provider call in a test")))
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
    monkeypatch.delenv("MMAI_ROUTING_LIVE", raising=False)
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "test-key-not-real")
    monkeypatch.setattr(routing, "_post", lambda body: (_ for _ in ()).throw(AssertionError("provider called while off")))
    stock(on)
    eid = submit(on)
    hs.run_pending(on.db)
    r = on.post(URL.format(eid), json={})
    assert r.status_code == 200 and r.json() == {"ok": False, "state": "off", "saved": False, "plain": "Live travel checks are switched off."}
    assert on.get(f"/api/homebot/enquiries/{eid}/options").json()["live_travel"]["enabled"] is False
    assert audits(on) == []
    # the switch alone is not enough: without a provider key the check stays off
    monkeypatch.setenv("MMAI_ROUTING_LIVE", "1")
    monkeypatch.delenv("GOOGLE_ROUTES_API_KEY")
    assert on.post(URL.format(eid), json={}).json()["state"] == "off"
    # and anything other than exactly "1" is off
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "test-key-not-real")
    for value in ("0", "true", "yes", "on", " 1", ""):
        monkeypatch.setenv("MMAI_ROUTING_LIVE", value)
        assert not routing.live_enabled(), value


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
    tram = [b for b in p.bodies if b["travelMode"] == "TRANSIT"]
    assert tram and all(b["departureTime"] == "2026-11-10T21:00:00Z" and b["transitPreferences"] == {"allowedTravelModes": ["LIGHT_RAIL"]} for b in tram)
    shown = everything(out)
    assert shown and all(x["travel"]["status"] == tv.PASS for x in shown)
    person = shown[0]["travel"]["facts"]["people"][0]
    assert person["fastest_modes"] == ["Tram"] and person["fastest_minutes"] is not None      # the faster accepted way is named
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
    for leaked in ("601", "10.0166", "1500s", "fastest_minutes\": \"", "google-routes"):
        assert leaked not in everywhere, leaked


def test_stored_options_still_say_travel_needs_confirmation(live):
    c, eid = live
    hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    cur = c.get(f"/api/homebot/enquiries/{eid}/options").json()["scenarios"][CUR]["current"]
    assert cur["route_evidence"] is None
    assert cur["listed"] and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in cur["listed"])


def test_over_the_travel_limit_is_ruled_out_in_the_live_view_only(live):
    c, eid = live
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(tram="7200s", walk="9000s"))   # two hours each way
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


def test_a_journey_by_the_wrong_vehicle_is_not_counted_as_measured(live):
    c, eid = live
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(vehicle="BUS"))   # the party accepts tram and walking only
    assert out["check"]["measured"] == 8                                  # the walking journeys only
    for x in everything(out):
        for person in x["travel"]["facts"]["people"]:
            assert person.get("fastest_modes") in (None, [], ["Walk"])


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
        assert routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") == 10000, bad
    monkeypatch.setenv("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS", "2500")
    assert routing.live_limit("MMAI_ROUTING_LIVE_MONTHLY_REQUESTS") == 2500


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
    monkeypatch.setenv("MMAI_ROUTING_LIVE", "1")
    monkeypatch.setenv("GOOGLE_ROUTES_API_KEY", "test-key-not-real")
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
    monkeypatch.setattr(routing.LiveEstimates, "prefetch", lambda self, ids: req_est(self, ids, deadline_sec=0.2))
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
    assert est(1, 1, "primary") == {"Tram": tv.UNMEASURED, "Walk": tv.UNMEASURED}     # nothing is requested outside prefetch
    assert est.calls == 0


def test_address_is_staff_only_and_validates(live, monkeypatch):
    c, eid = live
    monkeypatch.setattr(routing, "_post", Provider())
    assert c.anon.post(URL.format(eid), json={}).status_code == 401
    assert audits(c) == []
    assert c.post(URL.format(eid), json={"scenario": "nonsense"}).status_code == 422
    assert c.post(URL.format(999999), json={}).status_code == 404
    r = c.post(URL.format(eid), json={"scenario": CUR})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["check"]["requests"] == 16
    assert "no-store" in r.headers.get("cache-control", "")
    ov = c.get(f"/api/homebot/enquiries/{eid}/options").json()
    assert ov["live_travel"] == {"enabled": True, "auto": True, "plain": hs.TRAVEL_NOT_SAVED}
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
    attr = '<span class="gmaps-attr" translate="no">Google Maps</span>'
    assert f"const GMAPS_ATTR = '{attr}';" in js                           # the provider's name, untranslated and unaltered
    assert "Journey estimates: ${GMAPS_ATTR}." in block                    # at the head of the live options
    figures = js[js.index("const journeys = x =>"):js.index("const candRows =")]
    assert "${shown ? '<br>' + GMAPS_ATTR : ''}" in figures                # and beside each apartment's journey figures
    assert "person.fastest_minutes != null" in figures.split("const shown =")[1] and "f.majority" in figures.split("const shown =")[1]
    assert "localStorage" not in block and "sessionStorage" not in block and "indexedDB" not in block
    assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", block)       # plain staff language, no register codes
    css = open(os.path.join(root, "client", "styles.css"), encoding="utf-8").read()
    rule = css[css.index(".gmaps-attr {"):].split("}")[0]
    assert "font-size: 14px" in rule and "color: #1F1F1F" in rule and "Roboto" in rule
    assert "localStorage" not in js[js.index("const hbTravelShown"):js.index("const hbTravelShown") + 400]
    for text in (hs.TRAVEL_NOT_SAVED, routing.LIVE_BASIS, routing.LIVE_OFF):
        assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", text)
