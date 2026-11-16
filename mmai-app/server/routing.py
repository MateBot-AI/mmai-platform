"""Bounded HomeBot route adapter. Off unless documented field-level rights exist.

Google development access alone is NOT permission for persistent duration/mode
evidence or onward AI use. No default Google policy is supplied. Operator-supplied
rights must cover the complete persisted and AI-reviewed workflow, including
derived results and its retention period. No response bodies/addresses are logged.

A second, separate path at the end of this module (LiveEstimates) serves a staff
member's on-screen travel check: journey times are requested, used for that one
reply and dropped. It stores nothing and never feeds the AI review, so it does not
use the stored-evidence rights above and never satisfies them. It is off by default;
whether the provider's standard terms allow even this in-request calculation is an
open question recorded in RELEASE_OPERATIONS.md, not something this code settles.
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
        origin = self._origin(apartment)
        if not destination or not origin:
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

    def _origin(self, apartment):
        if not apartment.get("address"):
            return None
        return ", ".join(str(apartment.get(k) or "").strip() for k in ("address", "suburb")) + ", Victoria, Australia"

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


# ----------------------------------------------------------------------------------------------- live travel check
# A staff member's on-screen travel check. Journey times are requested for the options on the page, used for that one
# reply and then dropped: nothing from the provider is stored, cached, logged or passed to the AI review. This path is
# separate from the stored-evidence path above (load_policy / Estimates), which stays off without documented rights.
LIVE_ENV = "MMAI_ROUTING_LIVE"
LIVE_BASIS = "Estimated weekday departure at 08:00 Melbourne; not a guaranteed journey time"
LIVE_OFF = "Live travel checks are switched off."
# Request limits: (default, lowest, highest). The provider bills each successful request under one price tier chosen by
# the features asked for. What this module asks for (request_body + FIELD_MASK) is in the lowest tier for walking, cycling
# and public transport, and in the middle tier for car journeys (traffic-aware routing); it asks for nothing in the highest
# tier (no two-wheeler mode, tolls, traffic on polylines or large-vehicle routing). The monthly default is kept below the
# middle tier's monthly free allowance (5,000 requests), so the default cannot run past it whatever mix of modes is used.
LIVE_LIMITS = {"MMAI_ROUTING_LIVE_MAX_REQUESTS": (200, 1, 500),          # one check
               "MMAI_ROUTING_LIVE_DAILY_REQUESTS": (1000, 1, 20000),     # all checks in one UTC day
               "MMAI_ROUTING_LIVE_MONTHLY_REQUESTS": (4000, 1, 50000)}   # all checks in one UTC month
LIVE_DEADLINE_SEC = 45
LIVE_WORKERS = 6


def live_enabled() -> bool:
    return os.environ.get(LIVE_ENV, "0") == "1" and bool((os.environ.get("GOOGLE_ROUTES_API_KEY") or "").strip())


def live_auto() -> bool:
    return live_enabled() and os.environ.get("MMAI_ROUTING_LIVE_AUTO", "1") != "0"


def live_limit(name: str) -> int:
    """A configured request limit, clamped to its allowed range; anything unreadable falls back to the default."""
    default, low, high = LIVE_LIMITS[name]
    try:
        value = int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default
    return value if low <= value <= high else default


class LiveEstimates(Estimates):
    """In-memory journey estimates for one staff travel check. `prefetch` makes every provider request (bounded by the
    request allowance and a deadline, no retries); afterwards the object only answers from what was measured. `plan`
    settles how many requests will be made without contacting the provider, so that number can be recorded before the
    first request leaves. Anything not measured stays UNMEASURED and the
    option reads 'Travel time needs confirmation'. Origins are the building, so apartments in one building share a journey."""

    def __init__(self, req, inventory, *, max_requests, now=None, post=None):
        self.req = req
        self.inventory = {int(a["id"]): a for a in inventory}
        self.people = {p["position"]: p for p in req.people}
        self.now = now or datetime.now(timezone.utc)
        self.departure = departure_at(self.now)
        self.post = post or _post
        self.max_requests = int(max_requests)
        self.calls = self.measured = self.failures = self.skipped = 0
        self.cache = {}
        self._todo = []

    def _origin(self, apartment):
        building = hm.building_key(apartment.get("address") or "")
        if not building:
            return None
        return ", ".join(x for x in (building, str(apartment.get("suburb") or "").strip()) if x) + ", Victoria, Australia"

    def _keys(self, aid):
        apartment = self.inventory.get(int(aid))
        origin = self._origin(apartment) if apartment else None
        if not origin:
            return
        for person in self.req.people:
            for which in ("primary", "secondary"):
                destination = _destination(person, which)
                if destination:
                    for mode in self.req.transport:
                        yield (origin, destination, mode)

    def _measure(self, key):
        origin, destination, mode = key
        try:
            body = self.post(request_body(origin, destination, mode, self.departure))
            times = [duration_minutes(r.get("duration")) for r in body.get("routes", []) if _valid_route(r, mode)]
            return min(times) if times else tv.UNMEASURED, False
        except Exception:   # noqa: BLE001 - counted only; never provider text or addresses
            return tv.UNMEASURED, True

    def plan(self, apartment_ids) -> int:
        """Settle which journeys will be requested for these apartments, in the order given (the options staff see first
        come first), and return how many requests that is. Nothing is sent to the provider here."""
        wanted = []
        for aid in apartment_ids:
            for key in self._keys(aid):
                if key not in self.cache:
                    self.cache[key] = tv.UNMEASURED
                    wanted.append(key)
        room = max(0, self.max_requests - self.calls)
        self._todo += wanted[:room]
        self.skipped += max(0, len(wanted) - room)
        self.calls += len(wanted[:room])   # requests that will be attempted, including any that fail; no retry
        return self.calls

    def prefetch(self, apartment_ids=None, *, deadline_sec=LIVE_DEADLINE_SEC):
        """Make the planned requests (planning these apartments first when they are given)."""
        from concurrent.futures import ThreadPoolExecutor, wait
        if apartment_ids is not None:
            self.plan(apartment_ids)
        todo, self._todo = self._todo, []
        if not todo:
            return
        pool = ThreadPoolExecutor(max_workers=LIVE_WORKERS, thread_name_prefix="mmai-travel")
        try:
            futures = {pool.submit(self._measure, key): key for key in todo}
            done, late = wait(futures, timeout=deadline_sec)
            for f in done:
                value, failed = f.result()
                self.cache[futures[f]] = value
                self.measured += value is not tv.UNMEASURED
                self.failures += bool(failed)
            self.failures += len(late)   # still running at the deadline: treated as not measured
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def __call__(self, aid, position, which):
        apartment = self.inventory.get(int(aid))
        person = self.people.get(position)
        origin = self._origin(apartment) if apartment else None
        destination = _destination(person, which) if person else None
        if not origin or not destination:
            return {}
        return {mode: self.cache.get((origin, destination, mode), tv.UNMEASURED) for mode in self.req.transport}

    def evidence(self):
        raise RuntimeError("live travel estimates are never stored")

    def display(self):
        """What the page says about this check. Counts and our own clock only; no journey content."""
        return {"attribution": "Google Maps", "basis": LIVE_BASIS, "checked_at": self.now.isoformat(),
                "departure": self.departure.isoformat(), "timezone": "Australia/Melbourne",
                "requests": self.calls, "measured": self.measured, "failures": self.failures, "not_requested": self.skipped}
