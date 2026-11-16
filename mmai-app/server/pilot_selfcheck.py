"""Hosted self-check for the controlled pilot gate (MMAI_PILOT_SELFCHECK=1, only while MMAI_PILOT_GATE is on).

Once the server is listening, this process asks ITS OWN loopback address for a fixed list of pages and API addresses WITHOUT any
sign-in, and prints what came back, one line each, then a single PASS or FAIL line. It lets an operator confirm from the
hosting provider's runtime log that the gate is refusing visitors before any public address is opened. Every request is one
the gate refuses or one of the few open addresses, so nothing is written to the database and no background work is woken.
No secrets, cookies, tokens or reply bodies are printed.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PREFIX = "pilot self-check:"
HTML = {"Accept": "text/html"}
MINIMAL_HEALTH = {"ok": True}
_started = False

# (label, method, path, extra headers, JSON body or None, expected status, extra expectation name or None)
PROBES = (
    ("staff list without sign-in", "GET", "/api/enquiries", {}, None, 401, "no_store"),
    ("HomeBot list without sign-in", "GET", "/api/homebot/enquiries", {}, None, 401, None),
    ("AI queue without sign-in", "GET", "/api/ai-queue", {}, None, 401, None),
    ("made-up token", "GET", "/api/enquiries", {"Authorization": "Bearer not-a-real-token"}, None, 401, None),
    ("preview visitor header", "GET", "/api/enquiries", {"X-Visitor-Id": "self-check"}, None, 401, None),
    ("public enquiry form submit", "POST", "/api/public/enquiries", {}, {}, 401, None),
    ("public HomeBot form submit", "POST", "/api/public/homebot/enquiries", {}, {}, 401, None),
    ("session check without sign-in", "GET", "/api/auth/session", {}, None, 401, None),
    ("API description", "GET", "/openapi.json", {}, None, 401, None),
    ("page script without sign-in", "GET", "/app.js", {}, None, 401, None),
    ("front page in a browser", "GET", "/", HTML, None, 303, "to_sign_in"),
    ("HomeBot page in a browser", "GET", "/homebot/", HTML, None, 303, "to_sign_in_next"),
    ("off-site return address", "GET", "/.//evil.example", HTML, None, 303, "to_sign_in"),
    ("health", "GET", "/api/health", {}, None, 200, "minimal_health"),
    ("sign-in page", "GET", "/pilot/", HTML, None, 200, "page_headers"),
    ("sign-in page script", "GET", "/pilot/pilot.js", {}, None, 200, "fixed_script"),
    ("Terms of Use without sign-in", "GET", "/terms", HTML, None, 200, "terms_notice"),
    ("Privacy Policy without sign-in", "GET", "/privacy", HTML, None, 200, "privacy_notice"),
    ("a page beside the notices", "GET", "/terms/", HTML, None, 303, "to_sign_in_only"),
)


def _check(name, status, headers, body) -> bool:
    h = {k.lower(): v for k, v in headers.items()}
    if name == "no_store":
        return "no-store" in h.get("cache-control", "")
    if name == "to_sign_in":
        return h.get("location") == "/pilot/"
    if name == "to_sign_in_next":
        return h.get("location") == "/pilot/?next=%2Fhomebot%2F"
    if name == "minimal_health":
        try:
            return json.loads(body) == MINIMAL_HEALTH
        except ValueError:
            return False
    if name == "page_headers":
        return "no-store" in h.get("cache-control", "") and h.get("strict-transport-security") == "max-age=31536000"
    if name == "terms_notice":
        return b"<h1>Terms of Use</h1>" in body and b"Google Maps/Google Earth Additional Terms of Service" in body and "no-store" in h.get("cache-control", "")
    if name == "privacy_notice":
        return b"<h1>Privacy Policy</h1>" in body and b"Google Privacy Policy" in body and "no-store" in h.get("cache-control", "")
    if name == "to_sign_in_only":
        return (h.get("location") or "").startswith("/pilot/")
    if name == "fixed_script":
        return b"location.origin + path" in body and b"location.assign(next)" in body
    return True


def run(fetch, out=print) -> bool:
    """Run every probe through `fetch(method, path, headers, json_body) -> (status, headers, body_bytes)`; report with `out`."""
    passed = 0
    for label, method, path, headers, body, want, extra in PROBES:
        try:
            status, got_headers, got_body = fetch(method, path, dict(headers), body)
            ok = status == want and (extra is None or _check(extra, status, got_headers, got_body))
            detail = f"{status} (expected {want}{'' if extra is None else ' + ' + extra.replace('_', ' ')})"
        except Exception as ex:   # noqa: BLE001 - reported by type only
            ok, detail = False, f"no reply ({type(ex).__name__})"
        passed += ok
        out(f"{PREFIX} {'OK      ' if ok else 'MISMATCH'} {method} {path} [{label}] -> {detail}")
    script = Path(__file__).with_name("pilot") / "pilot.js"
    mark = hashlib.sha256(script.read_bytes()).hexdigest()[:12] if script.exists() else "missing"
    verdict = "PASS" if passed == len(PROBES) else "FAIL"
    out(f"{PREFIX} {verdict} {passed}/{len(PROBES)} probes as expected; sign-in script sha256 {mark}; requests made without sign-in from inside the container")
    return passed == len(PROBES)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _loopback_fetch(port: int):
    opener = urllib.request.build_opener(_NoRedirect)

    def fetch(method, path, headers, body):
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, dict(r.headers), r.read(200_000)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read(200_000)
    return fetch


def _wait_and_run(port: int, out=print, wait_sec: int = 180) -> None:
    fetch, deadline = _loopback_fetch(port), time.monotonic() + wait_sec
    while time.monotonic() < deadline:
        try:
            fetch("GET", "/api/health", {}, None)
            break
        except Exception:   # noqa: BLE001 - not listening yet
            time.sleep(2)
    else:
        out(f"{PREFIX} FAIL the server did not answer on its own address within {wait_sec} seconds")
        return
    try:
        run(fetch, out)
    except Exception as ex:   # noqa: BLE001
        out(f"{PREFIX} FAIL the self-check stopped early ({type(ex).__name__})")


def start(gate_on: bool) -> bool:
    """Start the self-check once per process when asked for. Never raises; returns whether it was started."""
    global _started
    if _started or (os.environ.get("MMAI_PILOT_SELFCHECK") or "").strip() != "1":
        return False
    _started = True
    if not gate_on:
        print(f"{PREFIX} FAIL MMAI_PILOT_SELFCHECK is set but the pilot gate is OFF (MMAI_PILOT_GATE is not on)", flush=True)
        return False
    try:
        port = int(os.environ.get("PORT") or 8000)
    except ValueError:
        port = 8000
    threading.Thread(target=_wait_and_run, args=(port, lambda line: print(line, flush=True)), name="mmai-pilot-selfcheck", daemon=True).start()
    return True
