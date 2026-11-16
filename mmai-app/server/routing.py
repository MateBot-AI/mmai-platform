"""Bounded HomeBot route adapter. Off unless documented field-level rights exist.

Google development access alone is NOT permission for persistent duration/mode
evidence or onward AI use. No default Google policy is supplied. Operator-supplied
rights must cover the complete persisted and AI-reviewed workflow, including
derived results and its retention period. No response bodies/addresses are logged.

A second, separate path at the end of this module (LiveEstimates) serves a staff
member's on-screen travel check (H28(i), 16 Nov 2026): journey times for walking,
bicycle and combined public transport are requested from Google Maps Experience
Agent Tools, used for that one reply and dropped. It stores nothing and never feeds
the saved AI review, so it does not use the stored-evidence rights above and never
satisfies them. It is off by default; the account, key, AI-retention and privacy
wording conditions for switching it on are recorded in RELEASE_OPERATIONS.md and
are not something this code settles.
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
DIRECT = {"Walk": "WALK", "Bike or scooter": "BICYCLE"}
# The only ways of travelling a journey is ever requested for: walking, bicycle, tram, train and bus. Car journeys are
# not measured. A person who accepted Car keeps that answer; it is never swapped for another way of travelling, and
# the car journey stays "not measured", so the travel result for that party reads as needing confirmation.
ROUTED_MODES = tuple(DIRECT) + tuple(TRANSIT)
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
    if mode not in TRANSIT:
        return False
    vehicles = []
    for step in steps:
        if step.get("travelMode") == "WALK":
            continue  # access/transfer walking within a transit journey
        if step.get("travelMode") != "TRANSIT":
            return False
        vehicles.append(((step.get("transitDetails") or {}).get("transitLine") or {}).get("vehicle", {}).get("type"))
    return bool(vehicles) and all(v in TRANSIT[mode][1] for v in vehicles)


def request_body(origin, destination, mode, departure):
    if mode not in ROUTED_MODES:
        raise ValueError("no journey is requested for this way of travelling")
    body = {"origin": {"address": origin}, "destination": {"address": destination},
            "travelMode": DIRECT.get(mode, "TRANSIT"), "computeAlternativeRoutes": True}
    if mode in TRANSIT:
        body["transitPreferences"] = {"allowedTravelModes": [TRANSIT[mode][0]]}
        body["departureTime"] = departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
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
            if mode not in ROUTED_MODES:
                continue   # never requested; the caller reads a missing mode as not measured
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
# A staff member's on-screen travel check (H28(i)). Journey times are requested for the options on the page, used for that
# one reply and then dropped: nothing from the provider is stored, cached between requests or logged. This path is separate
# from the stored-evidence path above (load_policy / Estimates), which stays off without documented rights and is not used.
#
# Provider: Google Maps Experience Agent Tools, computeRoutes over REST. What it returns for a route is a duration, a distance
# and an attribution (title + link). It does NOT return legs, steps or the vehicles used, so a public transport journey
# cannot be checked for tram, train or bus: "Public transport" is one combined choice and may include any vehicle the provider
# routes by (ferry included). Earlier Tram / Train / Bus / Car answers are therefore never requested and never measured here.
AGENT_TOOLS_ENV = "MMAI_TRAVEL_AGENT_TOOLS"
AGENT_TOOLS_KEY_ENV = "GOOGLE_MAPS_AGENT_TOOLS_API_KEY"
AGENT_TOOLS_ENDPOINT = "https://mapstools.googleapis.com/v1:computeRoutes"
AGENT_TOOLS_MODES = {"Walk": "WALK", "Bicycle": "BICYCLE", "Bike or scooter": "BICYCLE", "Public transport": "TRANSIT"}
# The only request fields this module ever sends. routingPreference (the traffic-aware option is the provider's dearer tier),
# includePolyline, intermediateWaypoints, locationBias and arrivalTime are never sent; _agent_tools_post refuses them.
AGENT_TOOLS_REQUEST_FIELDS = frozenset(("origin", "destination", "travelMode", "computeAlternativeRoutes", "departureTime"))
LIVE_ENV = AGENT_TOOLS_ENV
LIVE_BASIS = "Estimated weekday departure at 08:00 Melbourne; not a guaranteed journey time"
LIVE_OFF = "Live travel checks are switched off."
# The provider's own words, which it requires beside every walking route shown.
WALK_WARNING = "WALK routes are in beta and might sometimes be missing clear sidewalks or pedestrian paths."
SOURCE_TITLE_MAX = 200
SOURCE_URL_MAX = 2000
_SOURCE_HOST = re.compile(r"(?:[a-z0-9-]+\.)*google\.com(?:\.au)?|maps\.app\.goo\.gl|goo\.gl|g\.co")
# Request limits: (default, lowest, highest). The provider's published price for this service, as read on 16 November 2026,
# is one tier for every request this module makes (10,000 requests a month without charge, then US$7.00 per 1,000) unless a
# dearer option is asked for, and this module asks for none. That reading has not been checked against a bill. The monthly
# default is kept well under the no-charge allowance for that reason.
LIVE_LIMITS = {"MMAI_ROUTING_LIVE_MAX_REQUESTS": (200, 1, 500),          # one check
               "MMAI_ROUTING_LIVE_DAILY_REQUESTS": (1000, 1, 20000),     # all checks in one UTC day
               "MMAI_ROUTING_LIVE_MONTHLY_REQUESTS": (4000, 1, 50000)}   # all checks in one UTC month
LIVE_DEADLINE_SEC = 45
LIVE_WORKERS = 6


def live_enabled() -> bool:
    """On only with the exact switch value and the service's own key. The earlier MMAI_ROUTING_LIVE switch and
    GOOGLE_ROUTES_API_KEY no longer turn anything on."""
    return os.environ.get(AGENT_TOOLS_ENV, "0") == "1" and bool((os.environ.get(AGENT_TOOLS_KEY_ENV) or "").strip())


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


def agent_tools_request(origin, destination, mode, departure):
    """The request for one journey. `mode` is the provider's own name (WALK, BICYCLE or TRANSIT); never a car journey."""
    if mode not in AGENT_TOOLS_MODES.values():
        raise ValueError("no journey is requested for this way of travelling")
    return {"origin": {"address": origin}, "destination": {"address": destination}, "travelMode": mode,
            "computeAlternativeRoutes": True,
            "departureTime": departure.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}


def _agent_tools_post(body):
    import httpx
    key = (os.environ.get(AGENT_TOOLS_KEY_ENV) or "").strip()
    if not key:
        raise RuntimeError("journey service key missing")
    if set(body) - AGENT_TOOLS_REQUEST_FIELDS or body.get("travelMode") not in AGENT_TOOLS_MODES.values():
        raise ValueError("request field not allowed")
    # httpx is a pinned runtime dependency and honours the credential proxy.
    response = httpx.post(AGENT_TOOLS_ENDPOINT, json=body, headers={"X-Goog-Api-Key": key, "Content-Type": "application/json"},
                          timeout=httpx.Timeout(20, connect=5))
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise ValueError("oversize routing response")
    return response.json()


def route_source(route):
    """The attribution the provider sends with a route, as (title, link), or None when it sent none. Raises ValueError when
    it sent one that cannot be shown safely as a link (not a plain https address on the provider's own sites)."""
    a = route.get("attribution") if isinstance(route, dict) else None
    if a is None:
        return None
    title, url = (a.get("title"), a.get("url")) if isinstance(a, dict) else (None, None)
    if title is None and url is None:
        return None
    if not isinstance(title, str) or not isinstance(url, str) or not title.strip() or len(title) > SOURCE_TITLE_MAX \
            or len(url) > SOURCE_URL_MAX or any(ord(ch) < 32 or ord(ch) == 127 for ch in title + url):
        raise ValueError("unusable attribution")
    m = re.fullmatch(r"https://([^/?#@:\s\\]+)(?:[/?#][^\s\\\"'<>]*)?", url)
    if not m or not _SOURCE_HOST.fullmatch(m.group(1).lower()):
        raise ValueError("unusable attribution")
    return title.strip(), url


def _fastest_route(body):
    """(minutes, source) of the fastest usable route in a provider reply, or (UNMEASURED, None). A route whose duration cannot
    be read, or whose attribution cannot be shown, is not used."""
    best = None
    for route in (body.get("routes") if isinstance(body, dict) else None) or []:
        try:
            minutes, source = duration_minutes(route.get("duration")), route_source(route)
        except (ValueError, AttributeError):
            continue
        if best is None or minutes < best[0]:
            best = (minutes, source)
    return best or (tv.UNMEASURED, None)


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
        self.post = post or _agent_tools_post
        self.max_requests = int(max_requests)
        self.calls = self.measured = self.failures = self.skipped = 0
        self.cache = {}
        self.sources = {}
        self._todo = []

    def _origin(self, apartment):
        building = hm.building_key(apartment.get("address") or "")
        if not building:
            return None
        return ", ".join(x for x in (building, str(apartment.get("suburb") or "").strip()) if x) + ", Victoria, Australia"

    def _modes(self):
        """The provider modes to request for this party's accepted choices, each once. An earlier answer the provider cannot
        measure as given (Tram, Train, Bus, Car) is not here: it is never requested and never read as public transport."""
        return list(dict.fromkeys(AGENT_TOOLS_MODES[m] for m in self.req.transport if m in AGENT_TOOLS_MODES))

    def _keys(self, aid):
        apartment = self.inventory.get(int(aid))
        origin = self._origin(apartment) if apartment else None
        if not origin:
            return
        for person in self.req.people:
            for which in ("primary", "secondary"):
                destination = _destination(person, which)
                if destination:
                    for mode in self._modes():
                        yield (origin, destination, mode)

    def _measure(self, key):
        origin, destination, mode = key
        try:
            minutes, source = _fastest_route(self.post(agent_tools_request(origin, destination, mode, self.departure)))
            return minutes, source, False
        except Exception:   # noqa: BLE001 - counted only; never provider text or addresses
            return tv.UNMEASURED, None, True

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
                value, source, failed = f.result()
                self.cache[futures[f]] = value
                if value is not tv.UNMEASURED and source:
                    self.sources[futures[f]] = source
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
        return {mode: self.cache.get((origin, destination, AGENT_TOOLS_MODES[mode]), tv.UNMEASURED) if mode in AGENT_TOOLS_MODES
                else tv.UNMEASURED for mode in self.req.transport}

    def evidence(self):
        raise RuntimeError("live travel estimates are never stored")

    def sources_for(self, aid) -> dict:
        """What must be shown beside one apartment's journey figures: the provider's attribution for each measured journey
        (title and link exactly as sent, each once) and whether any of them is a walking route."""
        links, walking = {}, False
        for key in self._keys(aid):
            if self.cache.get(key, tv.UNMEASURED) is tv.UNMEASURED:
                continue
            walking = walking or key[2] == "WALK"
            if key in self.sources:
                links.setdefault(self.sources[key], None)
        return {"links": [{"title": t, "url": u} for t, u in links], "walking_warning": WALK_WARNING if walking else None}

    def display(self):
        """What the page says about this check. Counts and our own clock only; no journey content."""
        return {"attribution": "Google Maps", "basis": LIVE_BASIS, "checked_at": self.now.isoformat(),
                "departure": self.departure.isoformat(), "timezone": "Australia/Melbourne",
                "requests": self.calls, "measured": self.measured, "failures": self.failures, "not_requested": self.skipped}
