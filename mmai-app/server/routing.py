"""Bounded HomeBot route adapter. Off unless documented field-level rights exist.

Google development access alone is NOT permission for persistent duration/mode
evidence or onward AI use. No default Google policy is supplied. Operator-supplied
rights must cover the complete persisted and AI-reviewed workflow, including
derived results and its retention period. No response bodies/addresses are logged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time as clock
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

from . import homebot_match as hm, homebot_travel as tv
from .institutions import INSTITUTIONS

MELBOURNE = ZoneInfo("Australia/Melbourne")
REQUIRED_FIELDS = frozenset(("duration", "mode", "departure", "attribution"))
TRANSIT = {"Tram": ("LIGHT_RAIL", {"TRAM", "LIGHT_RAIL"}),
           "Train": ("TRAIN", {"HEAVY_RAIL", "COMMUTER_TRAIN", "HIGH_SPEED_TRAIN", "SUBWAY", "RAIL"}),
           "Bus": ("BUS", {"BUS", "INTERCITY_BUS", "TROLLEYBUS"})}
DIRECT = {"Walk": "WALK", "Car": "DRIVE", "Bike or scooter": "BICYCLE"}
ENDPOINT = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.legs.steps.travelMode,routes.legs.steps.transitDetails.transitLine.vehicle.type"
UNAVAILABLE = "Travel time needs confirmation"


def _utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone required")
    return dt.astimezone(timezone.utc)


def departure_at(now: datetime) -> datetime:
    """Next weekday at 08:00 Melbourne, with the zone's date-specific DST offset."""
    if now.tzinfo is None:
        raise ValueError("timezone required")
    local = now.astimezone(MELBOURNE)
    day = local.date()
    if local.time().replace(tzinfo=None) >= time(8):
        day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, time(8), MELBOURNE)


def load_policy(now=None):
    """No raw provider response may enter persistence without this entire gate."""
    if os.environ.get("MMAI_ROUTING", "0") != "1":
        return None
    try:
        path = Path(os.environ["MMAI_ROUTING_RIGHTS_FILE"])
        if path.stat().st_size > 32768:
            return None
        p = json.loads(path.read_text())
        now = now or datetime.now(timezone.utc)
        if p["provider"] != "google-routes" or not p["evidence_reference"].strip():
            return None
        if _utc(p["valid_until"]) <= now:
            return None
        # These are verified rights, not an application user's checkbox/consent.
        if p.get("verified") is not True or p.get("derived_results") is not True:
            return None
        if set(p["persist_fields"]) != REQUIRED_FIELDS or set(p["ai_fields"]) != REQUIRED_FIELDS:
            return None
        # Existing history follows enquiry retention; narrower provider rights
        # cannot be silently stretched by an expiry on the current route cache.
        if p.get("history_retention") != "enquiry-lifetime":
            return None
        if type(p["fresh_seconds"]) is not int or not 60 <= p["fresh_seconds"] <= 86400:
            return None
        if type(p["max_requests_per_job"]) is not int or not 1 <= p["max_requests_per_job"] <= 1000:
            return None
        p["digest"] = hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return p
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        return None


def duration_minutes(value):
    """Exact protobuf duration seconds; never rounded before H28 comparisons."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,9}(?:\.\d{1,9})?s", value):
        raise ValueError("invalid duration")
    return tv._minutes(Fraction(Decimal(value[:-1])) / 60)


def _valid_route(route, mode):
    steps = [step for leg in route.get("legs", []) for step in leg.get("steps", [])]
    if not steps:
        return False
    if mode in DIRECT:
        return all(s.get("travelMode") == DIRECT[mode] for s in steps)
    vehicles = []
    for step in steps:
        if step.get("travelMode") == "WALK":
            continue  # access/transfer walking within a transit journey
        if step.get("travelMode") != "TRANSIT":
            return False
        vehicles.append(((step.get("transitDetails") or {}).get("transitLine") or {}).get("vehicle", {}).get("type"))
    return bool(vehicles) and all(v in TRANSIT[mode][1] for v in vehicles)


def request_body(origin, destination, mode, departure):
    body = {"origin": {"address": origin}, "destination": {"address": destination},
            "travelMode": DIRECT.get(mode, "TRANSIT"), "computeAlternativeRoutes": True}
    if mode in TRANSIT:
        body["transitPreferences"] = {"allowedTravelModes": [TRANSIT[mode][0]]}
    if mode in TRANSIT or mode == "Car":
        body["departureTime"] = departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if mode == "Car":
        body["routingPreference"] = "TRAFFIC_AWARE"
    return body


def _post(body):
    import httpx
    headers = {"X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}
    key = os.environ.get("GOOGLE_ROUTES_API_KEY")
    if key:
        headers["X-Goog-Api-Key"] = key
    # httpx is a pinned runtime dependency and honours the credential proxy.
    response = httpx.post(ENDPOINT, json=body, headers=headers,
                          timeout=httpx.Timeout(20, connect=5))
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("oversize routing response")
    return response.json()


def _destination(person, which):
    primary, secondary = hm._destinations(person)
    kind = primary if which == "primary" else secondary
    if kind == "work":
        return (person.get("place") or "").strip() or None
    if kind == "study":
        name = person.get("institution")
        campus = next((c for c in INSTITUTIONS if c["name"] == name and c.get("verified")), None)
        return campus["address"] if campus else None
    return None


class Estimates:
    def __init__(self, req, inventory, policy, *, now=None, post=None):
        self.req = req
        self.inventory = {int(a["id"]): a for a in inventory}
        self.people = {p["position"]: p for p in req.people}
        self.policy = policy
        self.now = now or datetime.now(timezone.utc)
        self.departure = departure_at(self.now)
        self.post = post or _post
        self.calls = 0
        self.cache = {}
        self.measured = 0
        self.failures = 0
        self.started = clock.monotonic()
        self.expires = min(self.now + timedelta(seconds=policy["fresh_seconds"]), _utc(policy["valid_until"]))

    def __call__(self, aid, position, which):
        apartment = self.inventory.get(aid)
        person = self.people.get(position)
        if not apartment or not person:
            return {}
        destination = _destination(person, which)
        origin = ", ".join(str(apartment.get(k) or "").strip() for k in ("address", "suburb")) + ", Victoria, Australia"
        if not destination or not apartment.get("address"):
            return {}
        result = {}
        for mode in self.req.transport:
            key = (origin, destination, mode)
            if key not in self.cache:
                self.cache[key] = tv.UNMEASURED
                if self.calls >= self.policy["max_requests_per_job"] or clock.monotonic() - self.started >= 60:
                    continue
                self.calls += 1  # attempted calls counted, including failures; no retry
                try:
                    body = self.post(request_body(origin, destination, mode, self.departure))
                    times = [duration_minutes(r.get("duration")) for r in body.get("routes", []) if _valid_route(r, mode)]
                    if times:
                        self.cache[key] = min(times)
                        self.measured += 1
                except Exception:
                    self.failures += 1  # category/count only, never provider text or addresses
            result[mode] = self.cache[key]
        return result

    def evidence(self):
        return {"provider": "google-routes", "policy_digest": self.policy["digest"],
                "measured_at": self.now.isoformat(), "expires_at": self.expires.isoformat(),
                "departure": self.departure.isoformat(), "timezone": "Australia/Melbourne",
                "basis": "Estimated weekday departure at 08:00 Melbourne; not a guaranteed journey time",
                "attribution": "Google Maps", "requests_attempted": self.calls,
                "routes_measured": self.measured, "failures": self.failures,
                "persist_fields": sorted(REQUIRED_FIELDS), "ai_fields": sorted(REQUIRED_FIELDS)}


def configured_estimates(req, inventory, now=None):
    p = load_policy(now)
    return Estimates(req, inventory, p, now=now) if p else None


def stale_evidence(evidence, now=None):
    if not evidence:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        policy = load_policy(now)
        if not policy or policy["digest"] != evidence["policy_digest"]:
            return "Routing permission or configuration changed; fresh confirmation is required."
        if _utc(evidence["expires_at"]) <= now:
            return "Travel estimates have expired; fresh journeys are required."
    except (KeyError, TypeError, ValueError):
        return "Travel estimate evidence is incomplete; fresh confirmation is required."
    return None
