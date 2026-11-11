"""Synthetic provider replies only. These tests confer no provider-use rights."""
import json
from datetime import datetime, timezone
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction

import pytest

from server import routing as r, homebot_travel as tv
from tests.test_homebot_match import req, apt

NOW = datetime(2026, 11, 11, 0, tzinfo=timezone.utc)


def policy():
    return {"provider": "google-routes", "evidence_reference": "synthetic-test-only",
            "valid_until": "2099-01-01T00:00:00Z", "verified": True,
            "derived_results": True, "persist_fields": sorted(r.REQUIRED_FIELDS),
            "ai_fields": sorted(r.REQUIRED_FIELDS), "history_retention": "enquiry-lifetime",
            "fresh_seconds": 3600, "max_requests_per_job": 10}


def install(tmp_path, monkeypatch, p=None):
    path = tmp_path / "rights.json"
    path.write_text(json.dumps(p or policy()))
    monkeypatch.setenv("MMAI_ROUTING", "1")
    monkeypatch.setenv("MMAI_ROUTING_RIGHTS_FILE", str(path))
    return r.load_policy(NOW)


def reply(vehicle="TRAM", duration="601s"):
    return {"routes": [{"duration": duration, "legs": [{"steps": [
        {"travelMode": "WALK"},
        {"travelMode": "TRANSIT", "transitDetails": {"transitLine": {"vehicle": {"type": vehicle}}}}
    ]}]}]}


@pytest.mark.parametrize("key,value", [
    ("verified", False), ("derived_results", False), ("persist_fields", ["duration"]),
    ("ai_fields", []), ("history_retention", "30-days"), ("evidence_reference", ""),
    ("valid_until", "2020-01-01T00:00:00Z"), ("fresh_seconds", True),
    ("max_requests_per_job", 0), ("provider", "unknown")])
def test_policy_fails_closed(tmp_path, monkeypatch, key, value):
    p = policy()
    p[key] = value
    assert install(tmp_path, monkeypatch, p) is None


def test_no_policy_or_switch_means_no_calls(monkeypatch):
    monkeypatch.setenv("MMAI_ROUTING", "0")
    assert r.configured_estimates(req(), [apt(1)]) is None


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-02T22:00:00+00:00", "2026-10-05T08:00:00+11:00"),
    ("2027-04-02T22:00:00+00:00", "2027-04-05T08:00:00+10:00"),
    ("2026-11-10T20:59:00+00:00", "2026-11-11T08:00:00+11:00"),
    ("2026-11-10T21:00:00+00:00", "2026-11-12T08:00:00+11:00")])
def test_weekday_melbourne_departure_and_dst(instant, expected):
    assert r.departure_at(datetime.fromisoformat(instant)).isoformat() == expected


def test_transit_preferences_never_authorise_a_different_returned_mode(tmp_path, monkeypatch):
    p = install(tmp_path, monkeypatch)
    seen = []
    def post(body):
        seen.append(body)
        return reply("BUS")
    adapter = r.Estimates(req(), [apt(1)], p, now=NOW, post=post)
    assert adapter(1, 1, "primary") == {"Tram": tv.UNMEASURED}
    assert seen[0]["transitPreferences"]["allowedTravelModes"] == ["LIGHT_RAIL"]
    assert seen[0]["departureTime"] == "2026-11-11T21:00:00Z"


def test_exact_seconds_deduplication_and_attribution(tmp_path, monkeypatch):
    p = install(tmp_path, monkeypatch)
    adapter = r.Estimates(req(), [apt(1)], p, now=NOW, post=lambda body: reply())
    with localcontext() as ctx:
        ctx.prec = 1
        ctx.traps[Inexact] = True
        assert adapter(1, 1, "primary")["Tram"] == Fraction(601, 60)
        assert adapter(1, 2, "primary")["Tram"] == Fraction(601, 60)
    assert adapter.calls == 1
    assert adapter.evidence()["attribution"] == "Google Maps"
    assert r.stale_evidence(adapter.evidence(), NOW) is None
    assert r.stale_evidence(adapter.evidence(), datetime(2026, 11, 12, tzinfo=timezone.utc))
    monkeypatch.setenv("MMAI_ROUTING", "0")
    assert r.stale_evidence(adapter.evidence(), NOW)


def test_fraction_seconds_do_not_round_a_ceiling_pass():
    pref = tv.TravelPreference("30 minutes", ["Tram"])
    people = [tv.PersonTravel(i, tv.Destination({"Tram": r.duration_minutes(s)}, pref))
              for i, s in enumerate(("3600s", "3600.000000001s"), 1)]
    out = tv.assess_travel(pref, people)
    assert out.status == tv.EXCEEDS_CEILING
    assert out.facts["within_ceiling"] is False
    assert Fraction(out.facts["majority"]["total_minutes"]) > 120


def test_unknown_secondary_destination_and_bounded_failures(tmp_path, monkeypatch):
    p = install(tmp_path, monkeypatch)
    p["max_requests_per_job"] = 1
    def fail(body):
        raise RuntimeError("sensitive provider detail must not be persisted")
    adapter = r.Estimates(req(), [apt(1), apt(2)], p, now=NOW, post=fail)
    assert adapter(1, 1, "secondary") == {}
    assert adapter(1, 1, "primary")["Tram"] == tv.UNMEASURED
    assert adapter(2, 1, "primary").get("Tram", tv.UNMEASURED) == tv.UNMEASURED
    assert adapter.calls == adapter.failures == 1
    assert "sensitive" not in json.dumps(adapter.evidence())


def test_institution_resolves_stored_address_not_inferred_campus():
    campus = r.INSTITUTIONS[0]
    assert r._destination({"doing": "Studying", "institution": campus["name"]}, "primary") == campus["address"]
    assert r._destination({"doing": "Studying", "institution": "unrecognised campus"}, "primary") is None
