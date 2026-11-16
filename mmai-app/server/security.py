"""Pre-publish hardening (27 Oct 2026 security review): request-shape checks, size limits, per-IP rate limits, sign-in lockout,
session-token hashing and response security headers. Everything is in-process (one uvicorn worker) and configurable by
environment variable; nothing here changes a matching rule or a staff screen.

Environment variables (all optional):
  MMAI_RATE_LIMITING=0          turn the per-IP public limits off (tests)
  MMAI_RATE_ENQUIRIES_PER_HOUR  public enquiry submissions per IP per hour (default 20)
  MMAI_RATE_DRAFTS_PER_HOUR     public draft saves per IP per hour (default 300; the form autosaves on each Next)
  MMAI_RATE_LOGINS_PER_15MIN    sign-in attempts per IP per 15 minutes (default 60)
  MMAI_LOGIN_LOCK_FAILURES      failed sign-ins for one username before a lock (default 10)
  MMAI_LOGIN_LOCK_MINUTES       length of the lock (default 15)
  MMAI_MAX_BODY_BYTES           JSON body limit (default 262144 = 256 KB); MMAI_MAX_UPLOAD_BYTES for the .xlsx import (default 10 MB)
  MMAI_ALLOWED_ORIGINS          comma-separated list of origins allowed to call the API cross-origin with credentials (default none:
                                the client is served from the same origin, so no CORS is needed)
  MMAI_FRAME_ANCESTORS          CSP frame-ancestors value (default allows the app's own origin and the Perplexity preview/app hosts)
  MMAI_PUBLIC_BASE_URL          public address of the app for generated links (e.g. https://app.matebot.com.au); empty = page address
  MMAI_TRUST_PROXY_IP=0         ignore X-Forwarded-For (default 1: the hosting proxy sets it; the first address is the visitor)
  MMAI_PILOT_GATE=1             controlled pilot: every page, file and API call needs an allowed signed-in pilot user, except the
                                pilot sign-in page, the sign-in call itself, a minimal health reply and the two public notices
                                (/terms and /privacy); all new enquiries are
                                saved as TEST. Unset/0/off/false/no = off (normal behaviour). Any other value turns it ON, so a
                                mistyped value fails closed.
  MMAI_PILOT_USERS              comma-separated usernames allowed through the pilot gate (default: the manager account only)
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


RATE_LIMITING = os.environ.get("MMAI_RATE_LIMITING", "1") != "0"
RATE_ENQUIRIES_PER_HOUR = _int_env("MMAI_RATE_ENQUIRIES_PER_HOUR", 20)
RATE_DRAFTS_PER_HOUR = _int_env("MMAI_RATE_DRAFTS_PER_HOUR", 300)
RATE_LOGINS_PER_15MIN = _int_env("MMAI_RATE_LOGINS_PER_15MIN", 60)
LOGIN_LOCK_FAILURES = _int_env("MMAI_LOGIN_LOCK_FAILURES", 10)
LOGIN_LOCK_MINUTES = _int_env("MMAI_LOGIN_LOCK_MINUTES", 15)
MAX_BODY_BYTES = _int_env("MMAI_MAX_BODY_BYTES", 256 * 1024)
MAX_UPLOAD_BYTES = _int_env("MMAI_MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
MAX_DRAFT_BYTES = _int_env("MMAI_MAX_DRAFT_BYTES", 64 * 1024)
TRUST_PROXY_IP = os.environ.get("MMAI_TRUST_PROXY_IP", "1") != "0"
UPLOAD_PATHS = ("/api/apartments/import",)

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("MMAI_ALLOWED_ORIGINS", "").split(",") if o.strip()]
FRAME_ANCESTORS = os.environ.get("MMAI_FRAME_ANCESTORS", "'self' https://*.perplexity.ai https://*.pplx.app")
# I20(g): production sets MMAI_FRAME_ANCESTORS to the ALM website origins only, e.g.
#   'self' https://www.apartmentlivingmelbourne.com.au https://apartmentlivingmelbourne.com.au   (see .do/app.yaml)
# The staff dashboard shares index.html with the form (hash routing), so the staff screens refuse to render inside a frame
# client-side (app.js) and send the visitor to a full tab instead.
# Public address used in every link the app hands out (resume, Bring a mate, emailed form): production sets
# MMAI_PUBLIC_BASE_URL=https://app.matebot.com.au so links made inside the ALM website iframe point at the app, not the website.
PUBLIC_BASE_URL = os.environ.get("MMAI_PUBLIC_BASE_URL", "").strip().rstrip("/")

# Content Security Policy for responses the backend serves itself (self-hosted client). The static preview host serves the
# client from its own CDN, so client/index.html carries the same policy in a <meta> tag (minus frame-ancestors, which a meta tag
# cannot express). No inline scripts, no third-party scripts, fonts self-hosted (review L1, L2).
def csp() -> str:
    return ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; "
            "connect-src 'self'; form-action 'self'; base-uri 'self'; object-src 'none'; frame-ancestors " + FRAME_ANCESTORS)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
}


# ---------------------------------------------------------------------------
# Controlled pilot gate (16 Nov 2026). Read from the environment on every call so one process setting governs every request and
# a test can switch it without re-importing this module.
# ---------------------------------------------------------------------------
_PILOT_OFF = ("", "0", "off", "false", "no")
PILOT_PAGE_PATHS = ("/pilot", "/pilot/", "/pilot/pilot.js", "/pilot/pilot.css")
# The two public notices (Terms of Use and Privacy Policy). They are fixed pages holding no enquiry, staff or apartment
# information, and anyone may read them whether the gate is on or off.
PUBLIC_NOTICE_PATHS = ("/terms", "/privacy")
# (method, exact path) pairs that answer without a pilot session while the gate is on. Nothing else does.
PILOT_OPEN_API = (("POST", "/api/auth/login"), ("GET", "/api/auth/session"), ("GET", "/api/health"))
PILOT_HSTS = "max-age=31536000"


def pilot_gate_enabled() -> bool:
    return (os.environ.get("MMAI_PILOT_GATE") or "").strip().lower() not in _PILOT_OFF


def pilot_usernames() -> frozenset:
    """Usernames named in MMAI_PILOT_USERS (lower-case). Empty = only the manager role is allowed."""
    return frozenset(u.strip().lower() for u in (os.environ.get("MMAI_PILOT_USERS") or "").split(",") if u.strip())


def pilot_user_allowed(user: dict | None) -> bool:
    """True when this staff record (needs `username` and `role`) may pass the pilot gate. Never true for a disabled account."""
    if not user or user.get("disabled"):
        return False
    named = pilot_usernames()
    if named:
        return str(user.get("username") or "").strip().lower() in named
    return user.get("role") == "owner"


def pilot_open_path(method: str, path: str) -> bool:
    """Exact-match allow-list for requests that need no pilot session (sign-in page and its two files, the two public
    notices, sign-in, health)."""
    m = (method or "").upper()
    if path in PILOT_PAGE_PATHS or path in PUBLIC_NOTICE_PATHS:
        return m in ("GET", "HEAD")
    return (m, path) in PILOT_OPEN_API


def pilot_next_target(path: str, query: str = "") -> str:
    """A same-site path (+query) to return to after sign-in, or "" when it is not a plain local path."""
    if not path.startswith("/") or "//" in path or "\\" in path or "%" in path or any(ord(ch) < 32 for ch in path + query):
        return ""
    # A browser folds "." and ".." segments before it navigates, which can turn a harmless-looking path into "//other-site";
    # any path that is not already in its plain form is refused rather than tidied.
    if any(seg in (".", "..") for seg in path.split("/")):
        return ""
    if path in PILOT_PAGE_PATHS or path.startswith("/pilot/"):
        return ""
    return path + (("?" + query) if query else "")


# ---------------------------------------------------------------------------
# Client address
# ---------------------------------------------------------------------------
def client_ip(req: Request) -> str:
    if TRUST_PROXY_IP:
        fwd = req.headers.get("x-forwarded-for")
        if fwd:
            first = fwd.split(",")[0].strip()
            if first:
                return first
    return (req.client.host if req.client else None) or "unknown"


# ---------------------------------------------------------------------------
# JSON body helper (review M4): 422 instead of 500 for a non-object or non-JSON body
# ---------------------------------------------------------------------------
async def json_object(req: Request, empty_ok: bool = False, limit: int | None = None) -> dict:
    raw = await req.body()
    if len(raw) > (limit or MAX_BODY_BYTES):
        raise HTTPException(413, "The request is too large.")
    if not raw.strip():
        if empty_ok:
            return {}
        raise HTTPException(422, "The request body must be a JSON object.")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "The request body must be valid JSON.")
    if not isinstance(data, dict):
        raise HTTPException(422, "The request body must be a JSON object.")
    return data


def as_int(value, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    try:
        n = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


# ---------------------------------------------------------------------------
# Per-IP sliding-window rate limits (review M2)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self):
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, limit: int, window_s: int) -> bool:
        """Record one hit and return True when it is within the limit."""
        if not RATE_LIMITING or limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            q = self._hits[(bucket, key)]
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:  # keep memory bounded under a flood from many addresses
                for k in [k for k, v in self._hits.items() if not v or v[-1] < now - window_s]:
                    self._hits.pop(k, None)
            return True

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(req: Request, bucket: str, limit: int, window_s: int):
    if not limiter.check(bucket, client_ip(req), limit, window_s):
        raise HTTPException(429, "Too many requests from your connection. Please wait a little while and try again.")


# ---------------------------------------------------------------------------
# Sign-in lockout (review H2): per-username counter with a time-limited lock
# ---------------------------------------------------------------------------
class LoginGuard:
    def __init__(self):
        self._fails: dict[str, list] = {}  # username -> [count, locked_until_monotonic]
        self._lock = threading.Lock()

    def locked_for(self, username: str) -> int:
        """Seconds left on the lock for this username (0 when not locked)."""
        with self._lock:
            rec = self._fails.get(username)
            if not rec:
                return 0
            left = rec[1] - time.monotonic()
            if left <= 0:
                if rec[1]:
                    self._fails.pop(username, None)
                return 0
            return int(left) + 1

    def failed(self, username: str) -> bool:
        """Record a failure; True when this failure triggered a lock."""
        with self._lock:
            rec = self._fails.setdefault(username, [0, 0.0])
            rec[0] += 1
            if rec[0] >= LOGIN_LOCK_FAILURES:
                rec[0] = 0
                rec[1] = time.monotonic() + LOGIN_LOCK_MINUTES * 60
                return True
            return False

    def succeeded(self, username: str):
        with self._lock:
            self._fails.pop(username, None)

    def reset(self):
        with self._lock:
            self._fails.clear()


login_guard = LoginGuard()


# ---------------------------------------------------------------------------
# Session tokens are stored hashed (review L5); the clear token lives only in the cookie / the client's memory
# ---------------------------------------------------------------------------
def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def hash_legacy_sessions(c):
    """One-off: sessions saved in clear text by earlier builds are replaced by their hash, so staff stay signed in."""
    legacy = [r[0] for r in c.execute("SELECT token FROM sessions WHERE length(token) <> 64").fetchall()]
    for t in legacy:
        c.execute("UPDATE sessions SET token=? WHERE token=?", (token_hash(t), t))
    if legacy:
        c.commit()
    return len(legacy)


# ---------------------------------------------------------------------------
# Middleware helpers
# ---------------------------------------------------------------------------
def body_too_large(req: Request) -> JSONResponse | None:
    """Reject oversized requests before the handler runs (Content-Length check; JSON handlers also check the real size)."""
    cl = req.headers.get("content-length")
    if not cl:
        return None
    try:
        n = int(cl)
    except ValueError:
        return JSONResponse({"detail": "Bad Content-Length"}, status_code=400)
    limit = MAX_UPLOAD_BYTES if req.url.path in UPLOAD_PATHS else MAX_BODY_BYTES
    if n > limit:
        return JSONResponse({"detail": "The request is too large."}, status_code=413)
    return None


def apply_security_headers(resp, is_api: bool):
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    if pilot_gate_enabled():
        # Nothing served during the controlled pilot may be kept by a shared cache (the host's edge cache included): a cached
        # copy would be handed to a visitor who never passed the gate.
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers.setdefault("Strict-Transport-Security", PILOT_HSTS)
    elif is_api:
        resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("Content-Security-Policy", csp() if not is_api else "default-src 'none'; frame-ancestors 'none'")
