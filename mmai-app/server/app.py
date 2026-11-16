"""MMAI Stage 1 API (FastAPI). Run: python -m server.app  (port 8000).

Public routes (no login): form metadata + enquiry submission for the landing page and emailed form (I4 b, c).
Staff routes (Bearer token): everything that touches personal data (brief 2.3).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import access_facts, accounts, ai, ai_queue, apartment_rejections, group_responses, homebot, homebot_handoff, homebot_shortlist, importer, matching, matebot_live, matebot_travel, outcome_ledger, receipts, rules, scheduler, seed, staff_view, stock_lifecycle, submissions
from . import apartment_features as feat
from . import security as sec
from . import homebot_receipts, notifications
from .countries import ALL_COUNTRIES, REGIONS, normalise_country
from .db import ConnectionLostMidOperation, RetentionLockTimeout, audit, connect, is_unique_violation, j, now_iso, pj, retention_guard, row, rows
from . import exit_exceptions
from .db import guarded as dbm_guarded
from .enquiries import CHANNELS, QUESTION_TYPES, STATUSES, ConcurrentEdit, ValidationError, create_enquiry, forget_generated_text, get_enquiry, log_contact, purge_expired, update_enquiry, update_party_member, strip_staff_only
from .institutions import institution_list

HERE = os.path.dirname(__file__)
CLIENT_DIR = os.path.join(os.path.dirname(HERE), "client")
# The 23 Oct 2026 apartment list: a copy inside the app folder (so the published bundle is self-contained), else the Project copy.
_XLSX_CANDIDATES = [os.path.join(os.path.dirname(HERE), "data", "ALM_Apartments_2026-10-23.xlsx"),
                    os.path.join(os.path.dirname(os.path.dirname(HERE)), "data", "ALM_Apartments_2026-10-23.xlsx")]
XLSX_DEFAULT = next((p for p in _XLSX_CANDIDATES if os.path.exists(p)), _XLSX_CANDIDATES[0])
# T7: a staff sign-in is remembered on that device for 30 days (absolute expiry; sign-out revokes it earlier).
SESSION_DAYS = int(os.environ.get("MMAI_SESSION_DAYS", "30"))
# Published hosting only forwards cookies whose name carries the __Host- prefix (Secure, Path=/, no Domain).
SESSION_COOKIE = "__Host-mmai_session"
# T7 fallback inside the sandboxed preview iframe: the hosting proxy injects X-Visitor-Id per browser. Trust it ONLY when the
# preview start command sets MMAI_TRUST_VISITOR_ID=1. Published and self-hosted deployments keep the default (0): cookie + token.
TRUST_VISITOR_ID = os.environ.get("MMAI_TRUST_VISITOR_ID", "0") == "1"

app = FastAPI(title="MateBot AI Stage 1", version="0.3", docs_url=None, redoc_url=None, openapi_url=None)
# Review M1: the client is served from the same origin (self-hosted) or through the hosting proxy's same-origin path, so no
# cross-origin access is needed. Only origins listed in MMAI_ALLOWED_ORIGINS get CORS headers (never a reflected wildcard).
if sec.ALLOWED_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=sec.ALLOWED_ORIGINS, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
db = connect()
sec.hash_legacy_sessions(db)  # review L5: session tokens are stored hashed


# T5 retention guard outcomes for every guarded route that has no more specific wording of its own (delete / intro routes do):
# the step was rolled back or its outcome is unknown; staff get plain language and a retryable status, never a stack trace.
@app.exception_handler(RetentionLockTimeout)
async def _retention_busy(_req, _exc):
    return JSONResponse({"detail": "The system is busy finishing another update. Nothing was changed - try again in a moment."}, status_code=503)


@app.exception_handler(ConnectionLostMidOperation)
async def _retention_lost(_req, exc):
    msg = ("The database connection was lost while saving; reload and check what was recorded before trying again." if getattr(exc, "outcome_unknown", False)
           else "The database connection was lost. Nothing was changed - try again in a moment.")
    return JSONResponse({"detail": msg}, status_code=503)


# ---------------------------------------------------------------------------
# Staff auth
# ---------------------------------------------------------------------------
_hash = accounts.hash_password  # kept for older callers


def ensure_default_staff():
    """Idempotent: adds any missing individual staff account, never resets a password, closes the shared account once."""
    return accounts.ensure_staff_accounts(db)


ensure_default_staff()


@app.middleware("http")
async def _wake_up_scheduler(request: Request, call_next):
    """T8 in a sandbox that pauses when idle: any API request wakes the process, so if the hourly check is overdue it runs now,
    in the background (it only starts a matching run when a new applicant has arrived)."""
    is_api = request.url.path.startswith("/api/")
    too_big = sec.body_too_large(request)  # review M2: body-size limit before any handler runs
    if too_big is not None:
        sec.apply_security_headers(too_big, is_api)
        return too_big
    if sec.pilot_gate_enabled():
        # Controlled pilot: decided here, before the wake-ups below and before any handler, so a visitor without an allowed
        # sign-in triggers no scheduler check, no sweep and no handler code at all.
        refused = await run_in_threadpool(_pilot_gate, request)
        if refused is not None:
            sec.apply_security_headers(refused, is_api)
            return refused
        # The few addresses open without sign-in (sign-in call, session check, health) do not wake background work either.
        wake = is_api and bool(getattr(request.state, "pilot_user", None))
    else:
        wake = is_api
    if wake:
        try:
            scheduler.catch_up_if_overdue(db)
        except Exception:
            pass
        try:
            group_responses.sweep_if_due(db)   # GP13: hourly 24 h review sweep (staff alert only), same wake-up pattern, background thread
        except Exception:
            pass
    resp = await call_next(request)
    sec.apply_security_headers(resp, is_api)  # review L1
    return resp


def _visitor_hash(req: Request) -> Optional[str]:
    # The preview-only header fallback is never honoured while the pilot gate is on: a header is not proof of sign-in.
    vid = req.headers.get("x-visitor-id") if (TRUST_VISITOR_ID and not sec.pilot_gate_enabled()) else None
    return hashlib.sha256(vid.encode()).hexdigest() if vid else None


_SESSION_SQL = "SELECT s.token, s.expires_at, s.kind, u.id AS user_id, u.username, u.display_name, u.role FROM sessions s JOIN staff_users u ON u.id=s.user_id WHERE COALESCE(u.disabled,0)=0"


def _session_row(token: str) -> Optional[dict]:
    """Look a clear-text token up by its hash. The returned dict carries the clear token (for the client) and `token_hash` (for SQL)."""
    if not token:
        return None
    h = sec.token_hash(token)
    s = row(db.execute(_SESSION_SQL + " AND s.token=?", (h,)))
    if not s or s["expires_at"] < now_iso():
        return None
    s["token"], s["token_hash"] = token, h
    return s


def _touch(token_hash: str):
    db.execute("UPDATE sessions SET last_seen_at=? WHERE token=?", (now_iso(), token_hash))
    db.commit()


def _new_session(req: Request, user_id: int, expires_at: Optional[str] = None, kind: str = "remembered") -> tuple[str, str]:
    """Insert a session row (hashed token) and return (clear token, expiry)."""
    token = secrets.token_urlsafe(32)
    exp = expires_at or (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).replace(microsecond=0).isoformat()
    db.execute("INSERT INTO sessions(token, user_id, created_at, expires_at, kind, visitor_hash, last_seen_at, user_agent) VALUES (?,?,?,?,?,?,?,?)",
               (sec.token_hash(token), user_id, now_iso(), exp, kind, _visitor_hash(req), now_iso(), (req.headers.get("user-agent") or "")[:200]))
    db.commit()
    return token, exp


def resolve_session(req: Request, authorization: Optional[str] = None) -> tuple[Optional[dict], Optional[str]]:
    """T7: find the signed-in staff member by (1) the HttpOnly session cookie, (2) a Bearer token the client kept,
    (3) the hosting proxy's per-browser X-Visitor-Id (sandboxed preview fallback, only with MMAI_TRUST_VISITOR_ID=1). Returns (session, method)."""
    tok = req.cookies.get(SESSION_COOKIE)
    if tok:
        s = _session_row(tok)
        if s:
            return s, "cookie"
    if authorization and authorization.lower().startswith("bearer "):
        s = _session_row(authorization.split(" ", 1)[1].strip())
        if s:
            return s, "token"
    vh = _visitor_hash(req)
    if vh:
        s = row(db.execute(_SESSION_SQL + " AND s.visitor_hash=? AND s.expires_at>? ORDER BY s.created_at DESC LIMIT 1", (vh, now_iso())))
        if s:
            # the stored token is a hash, so hand this device a fresh clear token with the same expiry (preview fallback only);
            # device rows idle for an hour are dropped so cookie-less reloads do not pile up (open tabs touch theirs on every call)
            db.execute("DELETE FROM sessions WHERE kind='device' AND visitor_hash=? AND last_seen_at < ?",
                       (vh, (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()))
            token, _ = _new_session(req, s["user_id"], s["expires_at"], kind="device")
            s["token"], s["token_hash"] = token, sec.token_hash(token)
            return s, "device"
    return None, None


def _pilot_session(req: Request) -> Optional[dict]:
    """Read-only lookup of the allowed pilot user behind this request. Same order as resolve_session (session cookie, then
    Bearer token) and the FIRST valid session decides, so the gate and the handlers always judge the same sign-in. Writes nothing."""
    tokens = [req.cookies.get(SESSION_COOKIE)]
    auth = req.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        tokens.append(auth.split(" ", 1)[1].strip())
    for tok in tokens:
        s = _session_row(tok) if tok else None
        if s:
            return s if sec.pilot_user_allowed(s) else None
    return None


def _pilot_gate(request: Request) -> Optional[Response]:
    """Controlled pilot gate. Returns None to let the request through, or the refusal to send instead. Open without a session:
    only the exact paths in security.pilot_open_path (sign-in page and its two files, the two public notices, the sign-in call,
    session check, health)."""
    path, method = request.scope.get("path") or "", request.method.upper()   # the exact path the router will match
    try:
        s = _pilot_session(request)
    except Exception:
        # No detail: a database fault must not open the gate or describe itself to a visitor.
        return JSONResponse({"detail": "The service is temporarily unavailable. Please try again shortly."}, status_code=503)
    request.state.pilot_user = s
    if s or sec.pilot_open_path(method, path):
        return None
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Sign-in is required. This site is in private testing."}, status_code=401)
    if method in ("GET", "HEAD") and "text/html" in (request.headers.get("accept") or ""):
        nxt = sec.pilot_next_target(path, request.url.query)
        return RedirectResponse("/pilot/" + (("?next=" + quote(nxt, safe="")) if nxt and nxt != "/" else ""), status_code=303)
    return Response("Sign-in is required.", status_code=401, media_type="text/plain")


def _pilot_force_test(body: dict) -> dict:
    """While the pilot gate is on, every new enquiry or saved form is TEST, whatever the page sent."""
    if sec.pilot_gate_enabled():
        body["is_test"] = True
    return body


def current_staff(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    s, _ = resolve_session(request, authorization)
    if s and sec.pilot_gate_enabled() and not sec.pilot_user_allowed(s):
        s = None   # second check behind the middleware gate: only an allowed pilot user is staff while the gate is on
    if not s:
        raise HTTPException(401, "Staff login required" if not (authorization or request.cookies.get(SESSION_COOKIE)) else "Session expired, please sign in again")
    return s


def current_manager(staff=Depends(current_staff)) -> dict:
    if staff["role"] != "owner":
        raise HTTPException(403, "Only the manager can do this")
    return staff


def _set_cookie(resp: Response, token: str, max_age: int):
    # __Host- prefix rules: Secure, Path=/, no Domain. HttpOnly keeps it away from scripts; SameSite=Lax is right for a top-level site.
    resp.headers.append("set-cookie", f"{SESSION_COOKIE}={token}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax")


def _clear_cookie(resp: Response):
    resp.headers.append("set-cookie", f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax")


def _prune_sessions():
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
    db.commit()


def _login_payload(s: dict, method: str) -> dict:
    return {"token": s["token"], "username": s.get("username"), "display_name": s["display_name"], "role": s["role"], "expires_at": s["expires_at"],
            "method": method, "session_days": SESSION_DAYS, "staff_list": accounts.active_display_names(db)}


# Review L4: unknown usernames cost the same PBKDF2 work as a wrong password, so timing does not reveal which usernames exist.
_DUMMY_USER = {"pw_salt": "0123456789abcdef", "pw_hash": accounts.hash_password("not-a-real-password", "0123456789abcdef")}


async def _slow_fail():
    """Review H2: the failure delay must not block the event loop (time.sleep stalled every other request)."""
    await asyncio.sleep(0.4)


@app.post("/api/auth/login")
async def login(req: Request, resp: Response):
    body = await sec.json_object(req)
    sec.rate_limit(req, "login", sec.RATE_LOGINS_PER_15MIN, 15 * 60)
    username = str(body.get("username") or "").strip().lower()[:80]
    password = str(body.get("password") or "")[:200]
    left = sec.login_guard.locked_for(username) if username else 0
    if left:
        await _slow_fail()
        raise HTTPException(423, f"Too many failed sign-ins for this username. Please wait {max(1, (left + 59) // 60)} minute(s) and try again.")
    u = accounts.get_user(db, username)
    ok = await run_in_threadpool(accounts.verify_password, u or _DUMMY_USER, password)  # PBKDF2 off the event loop
    if not u or not ok:
        if username and sec.login_guard.failed(username):
            audit(db, None, "login_locked", "staff_user", username, f"{sec.LOGIN_LOCK_FAILURES} failed sign-ins; locked for {sec.LOGIN_LOCK_MINUTES} min")
        await _slow_fail()
        raise HTTPException(401, "Incorrect username or password")
    if u.get("disabled"):
        await _slow_fail()
        raise HTTPException(401, "This shared sign-in has been closed. Please use your own username.")
    sec.login_guard.succeeded(username)
    if sec.pilot_gate_enabled() and not sec.pilot_user_allowed(u):
        audit(db, u["display_name"], "pilot_login_refused")
        await _slow_fail()
        raise HTTPException(403, "This sign-in is not part of the private testing. Please contact the manager.")
    _prune_sessions()
    token, exp = _new_session(req, u["id"])
    audit(db, u["display_name"], "login")
    _set_cookie(resp, token, SESSION_DAYS * 86400)
    return _login_payload({"token": token, "username": u["username"], "display_name": u["display_name"], "role": u["role"], "expires_at": exp}, "password")


@app.get("/api/auth/session")
def session_resume(req: Request, resp: Response, authorization: Optional[str] = Header(None)):
    """T7: called when the app opens. Returns the remembered sign-in for this device (cookie, kept token or visitor id) or 401."""
    s, method = resolve_session(req, authorization)
    if not s or (sec.pilot_gate_enabled() and not sec.pilot_user_allowed(s)):
        raise HTTPException(401, "Not signed in on this device")
    _touch(s["token_hash"])
    if method != "cookie":
        _set_cookie(resp, s["token"], max(60, int((datetime.fromisoformat(s["expires_at"]) - datetime.now(timezone.utc)).total_seconds())))
    return _login_payload(s, method)


@app.post("/api/auth/logout")
def logout(req: Request, resp: Response, staff=Depends(current_staff)):
    """Sign out: revoke this session and every remembered session for this device."""
    db.execute("DELETE FROM sessions WHERE token=?", (staff["token_hash"],))
    vh = _visitor_hash(req)
    if vh:
        db.execute("DELETE FROM sessions WHERE visitor_hash=?", (vh,))
    db.commit()
    _clear_cookie(resp)
    audit(db, staff["display_name"], "logout")
    return {"ok": True}


@app.get("/api/auth/me")
def me(staff=Depends(current_staff)):
    return {"username": staff.get("username"), "display_name": staff["display_name"], "role": staff["role"], "expires_at": staff["expires_at"]}


@app.post("/api/auth/password")
async def change_password(req: Request, staff=Depends(current_staff)):
    """Change my password (any signed-in user). Needs the current password; the new one must be at least 10 characters.
    Other remembered sign-ins of this user are ended; the device making the change stays signed in."""
    body = await sec.json_object(req)
    u = accounts.get_user(db, staff["username"])
    ok = u and await run_in_threadpool(accounts.verify_password, u, str(body.get("current_password") or "")[:200])
    if not ok:
        await _slow_fail()
        raise HTTPException(422, "The current password is not right.")
    new = str(body.get("new_password") or "")[:200]
    problem = accounts.validate_new_password(new)
    if problem:
        raise HTTPException(422, problem)
    if new == (body.get("current_password") or ""):
        raise HTTPException(422, "The new password must be different from the current one.")
    accounts.set_password(db, u["id"], new)
    db.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (u["id"], staff["token_hash"]))
    db.commit()
    audit(db, staff["display_name"], "change_password", "staff_user", u["username"])
    return {"ok": True, "message": "Your password has been changed."}


@app.get("/api/auth/users")
def list_staff_users(staff=Depends(current_manager)):
    """Manager only: the staff accounts (no password material)."""
    return accounts.list_users(db)


@app.post("/api/auth/users/{username}/reset-password")
async def reset_staff_password(username: str, req: Request, staff=Depends(current_manager)):
    """Manager only: set a new temporary password for a team member who has forgotten theirs. The new password is returned once
    (to be passed on in person) and is never stored in clear text or written to the audit log."""
    u = accounts.get_user(db, username)
    if not u or u.get("disabled"):
        raise HTTPException(404, "No active account with that username")
    body = await sec.json_object(req, empty_ok=True)
    new = str(body.get("new_password") or "")[:200] or accounts.generate_password()
    problem = accounts.validate_new_password(new)
    if problem:
        raise HTTPException(422, problem)
    accounts.set_password(db, u["id"], new)
    db.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
    db.commit()
    audit(db, staff["display_name"], "reset_password", "staff_user", u["username"])
    return {"ok": True, "username": u["username"], "display_name": u["display_name"], "temporary_password": new}


# ---------------------------------------------------------------------------
# Public: form metadata and submission
# ---------------------------------------------------------------------------
def _mate_link_info(token: str) -> dict:
    """GP11(a)/(c): who shared this link and how many more people the party can take."""
    ml = row(db.execute("SELECT m.*, e.first_name, e.status, e.is_test, e.barred_at FROM mate_links m JOIN enquiries e ON e.id=m.enquiry_id WHERE m.token=?", (token,)))
    if not ml:
        return {"valid": False, "reason": "This link is not one of ours, or the enquiry it belonged to has been removed."}
    if ml["expires_at"] < now_iso():
        return {"valid": False, "reason": "This link has expired."}
    if ml["status"] in ("Leased", "Withdrawn") or ml["barred_at"]:
        return {"valid": False, "reason": f"{ml['first_name']}'s enquiry is no longer open, so this link no longer works."}
    ids = matching.linked_party_ids(db, ml["enquiry_id"])
    people = matching.party_people(db, [i for i in ids if row(db.execute("SELECT 1 FROM enquiries WHERE id=? AND status<>'Withdrawn' AND barred_at IS NULL", (i,)))])
    limit = matching.mate_limit(db, "test" if ml["is_test"] else "real")
    return {"valid": True, "enquirer_first_name": ml["first_name"], "enquiry_id": ml["enquiry_id"], "is_test": bool(ml["is_test"]),
            "people_so_far": people, "limit": limit, "room_left": max(0, limit - people)}


def _iso_in(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _issue_mate_link(enquiry_id: int, is_test: bool) -> str:
    existing = row(db.execute("SELECT token FROM mate_links WHERE enquiry_id=? AND expires_at>? ORDER BY created_at DESC LIMIT 1", (enquiry_id, now_iso())))
    if existing:
        return existing["token"]
    token = secrets.token_urlsafe(18)
    db.execute("INSERT INTO mate_links(token, enquiry_id, created_at, expires_at, is_test) VALUES (?,?,?,?,?)",
               (token, enquiry_id, now_iso(), _iso_in(matching.MATE_LINK_DAYS), 1 if is_test else 0))  # GP11(j): one year
    db.commit()
    return token


@app.get("/api/public/meta")
def public_meta(email_token: Optional[str] = None, mate_token: Optional[str] = None):
    link = None
    if email_token:
        link = row(db.execute("SELECT id, recipient_name, recipient_email, used_at FROM email_links WHERE token=?", (email_token,)))
    mate_info = _mate_link_info(mate_token[:200]) if mate_token else None
    if mate_info:
        mate_info.pop("enquiry_id", None)  # review M5: internal ids stay off the public route
    return {
        "mate_link": mate_info,
        "mate_limit": matching.mate_limit(db, "real"),  # GP11(c)/(e): people per share link, enquirer included
        "price_guide": {"12m": rules.price_guide("12m"), "6m": rules.price_guide("6m")},
        "institutions": institution_list(),
        "regions": list(REGIONS.keys()),
        "countries": ALL_COUNTRIES,
        "cities": [c["name"] for c in rows(db.execute("SELECT name FROM cities WHERE active=1"))],
        "email_link": {"valid": bool(link), "recipient_name": link["recipient_name"] if link else None, "recipient_email": link["recipient_email"] if link else None, "used": bool(link and link["used_at"])} if email_token else None,
        "question_types": QUESTION_TYPES,
        "features": feat.catalogue(), "features_max_top": feat.MAX_TOP,   # 3.11 / 3.12: the one catalogue (server/apartment_features.py)
        "public_base_url": sec.PUBLIC_BASE_URL or None,  # links made inside the ALM website iframe point here (I20(g))
    }


def _public_finalise(e: dict, channel: str, is_test: bool, mate: Optional[dict], draft_token: Optional[str]) -> dict:
    """Everything that follows a saved public enquiry. Safe to run again for the same enquiry (a retry after a lost reply):
    the mate link is linked once, the share token is reused and the draft delete is a no-op the second time."""
    out = {"id": e["id"], "first_name": e["first_name"], "move_in": e["move_in"], "channel": channel, "is_test": is_test}
    if mate:
        root_id = mate["enquiry_id"]
        cur = row(db.execute("SELECT linked_to_enquiry_id FROM enquiries WHERE id=?", (e["id"],)))
        if cur and cur.get("linked_to_enquiry_id"):
            res = {"outcome": "linked"}  # linked on an earlier attempt of this same submission; never link or regroup twice
        else:
            db.execute("UPDATE enquiries SET linked_to_enquiry_id=?, mate_note=? WHERE id=?", (root_id, f"Came through {mate['enquirer_first_name']}'s share link.", e["id"]))
            db.execute("UPDATE enquiries SET mate_note=COALESCE(mate_note, '') || ? WHERE id=?", (f" {e['first_name']} joined through their share link on {now_iso()[:10]}.", root_id))
            db.commit()
            audit(db, None, "bring_mate_linked", "enquiry", e["id"], f"linked to {root_id} via share link")
            try:
                res = matching.bring_mate(db, root_id, e["id"])
            except Exception as ex:  # the link itself must never fail because of the group step
                res = {"outcome": "error"}
                # T5: the audit detail is a closed token (exception TYPE from a fixed set), never the exception text (which could carry
                # applicant text or provider words); the client only ever received the outcome
                audit(db, None, "bring_mate_error", "enquiry", e["id"], "UNEXPECTED:" + matching.run_error_type_token(ex))
        out["mate"] = {"enquirer_first_name": mate["enquirer_first_name"], "outcome": res.get("outcome")}
        root_token = _issue_mate_link(root_id, is_test)
        out["mate_link"] = root_token  # the friend can pass the same link on; everyone links to the same enquirer (one party)
    else:
        out["mate_link"] = _issue_mate_link(e["id"], is_test)
    if draft_token:  # I19(f): the saved draft is finished with
        db.execute("DELETE FROM form_drafts WHERE token=?", (draft_token,)); db.commit()
    # I19(q): the receipt row was written atomically with the enquiry (hook in _create_guarded). This repairs a missing row
    # (idempotent), then starts background delivery when mail is enabled. Never raises: a mail problem can never turn the
    # saved submission into an error. Nothing is sent unless MMAI_MAIL_MODE is set.
    rc = receipts.after_saved(db, e["id"])
    out["receipt"] = rc.get("status")
    return out


def _submission_replies(sub: Optional[submissions.Submission]):
    """Replies for a submission key that already got somewhere on an earlier attempt, or None when the request should go ahead."""
    if sub is None or sub.state == "fresh":
        return None
    if sub.state == "in_progress":
        return JSONResponse({"status": "in_progress", "errors": {"form": "Your enquiry is still being saved. Please wait a moment."}}, status_code=409)
    if sub.state == "foreign":
        # the key exists in another scope (public form vs a staff sign-in): refused, and nothing about that other enquiry (no id,
        # no reference, no reply) crosses the boundary
        return JSONResponse({"already_submitted": True, "errors": {"form": submissions.GENERIC_USED_MESSAGE}}, status_code=409)
    if sub.state == "deleted":
        return JSONResponse({"already_submitted": True, "errors": {"form": submissions.DELETED_MESSAGE}}, status_code=409)
    if sub.state == "conflict":
        eid = sub.enquiry_id
        body = {"already_submitted": True, "errors": {"form": submissions.already_submitted_message(eid) if eid else submissions.GENERIC_USED_MESSAGE}}
        if eid:
            body["result"] = dict(sub.response, id=eid, replayed=True)
        return JSONResponse(body, status_code=409)
    return None  # replay / saved are handled by the caller (they need the channel-specific response)


def _submission_from(req: Request, scope: str, body: dict, extra: dict, is_test: bool) -> Optional[submissions.Submission]:
    key = submissions.clean_key(req.headers.get(submissions.HEADER))
    if not key:
        return None  # legacy client: no key, no reservation, behaviour unchanged
    return submissions.reserve(db, key, scope, submissions.fingerprint(body, extra), is_test)


def _create_guarded(sub: Optional[submissions.Submission], receipt: bool = False, **kw) -> dict:
    """create_enquiry() with the submission row marked saved in the same (locked) transaction and the key on the enquiry row.
    Raises submissions.Superseded when this request lost the key to a later retry (fencing) or the database refused a second
    enquiry for the key (UNIQUE backstop): the transaction has been rolled back and the caller replays the other attempt's result."""
    def hook(c, eid):
        if sub:
            submissions.mark_saved(c, sub, eid)
        if receipt:  # I19(q), public form only: the receipt intent is written in the SAME transaction as the enquiry row
            saved = row(c.execute("SELECT email, is_test FROM enquiries WHERE id=?", (eid,)))  # the address as saved (normalised)
            receipts.enqueue_in_transaction(c, eid, saved and saved["email"], bool(saved and saved["is_test"]))
    try:
        return create_enquiry(db, on_inserted=hook, submission_key=sub.key if sub else None, **kw)
    except ValidationError:
        if sub:
            submissions.release(db, sub)
        raise
    except submissions.Superseded:
        raise  # rolled back inside create_enquiry; the key now belongs to the other attempt, so it is not released here
    except Exception as ex:
        try:
            db.rollback()  # never leave a half-written enquiry for a later commit elsewhere to pick up
        except Exception:
            pass
        if sub and is_unique_violation(ex):
            raise submissions.Superseded(sub.key) from ex  # another attempt already wrote the enquiry for this key
        if sub:
            submissions.release(db, sub)
        raise


def _reread(sub: submissions.Submission, scope: str, body: dict, extra: dict, is_test: bool) -> submissions.Submission:
    """After Superseded: look the key up again (never a fresh reservation of our own; the other attempt owns it now)."""
    again = submissions.reserve(db, sub.key, scope, submissions.fingerprint(body, extra), is_test)
    if again.state == "fresh":
        # the other attempt released the key without writing (its own validation error): nothing was saved on either side
        submissions.release(db, again)
        return submissions.Submission(sub.key, "in_progress")
    return again


def _public_replay(sub: submissions.Submission, mate: Optional[dict], draft_token: Optional[str]):
    """The key already produced an enquiry: return its result (finishing the side effects first if they were lost)."""
    try:
        e = get_enquiry(db, sub.enquiry_id)
    except KeyError:  # the saved enquiry was deleted by staff since: nothing to replay, and nothing new is made
        return JSONResponse({"already_submitted": True, "errors": {"form": submissions.DELETED_MESSAGE}}, status_code=409)
    if sub.state == "replay":
        return JSONResponse(dict(sub.response, replayed=True), status_code=200)
    is_test = bool(e["is_test"])
    out = _public_finalise(e, e["channel"], is_test, mate if (mate and mate.get("valid")) else None, draft_token)
    submissions.finish(db, sub.key, e["id"], out)
    return JSONResponse(dict(out, replayed=True), status_code=200)


@app.post("/api/public/enquiries", status_code=201)
async def submit_public(req: Request, email_token: Optional[str] = None, mate_token: Optional[str] = None, draft_token: Optional[str] = None):
    body = _pilot_force_test(await sec.json_object(req))
    if str(body.get("company_website") or "").strip():  # review M2: honeypot field, hidden from people, filled by bots
        return JSONResponse({"errors": {"form": "Sorry, this form could not be sent. Please try again."}}, status_code=422)
    sec.rate_limit(req, "public_enquiries", sec.RATE_ENQUIRIES_PER_HOUR, 3600)
    channel, link_id = "Landing page", None
    if email_token:
        link = row(db.execute("SELECT id FROM email_links WHERE token=?", (email_token,)))
        if link:
            channel, link_id = "Emailed form", link["id"]
    is_test = bool(body.get("is_test"))
    mate = None
    if mate_token:  # GP11: a friend filling in their own full form through the enquirer's share link
        mate = _mate_link_info(mate_token)
    # Submission key (31 Oct 2026): reserve it first, so a retry of this exact request can never make a second enquiry.
    sub_extra = {"email_token": email_token, "mate_token": mate_token, "draft_token": draft_token}
    sub_test = is_test or bool(mate and mate.get("is_test"))
    sub = _submission_from(req, "public", body, sub_extra, sub_test)
    early = _submission_replies(sub)
    if early is not None:
        return early
    if sub and sub.state in ("replay", "saved"):
        return _public_replay(sub, mate, draft_token)
    if mate:
        if not mate["valid"]:
            if sub:
                submissions.release(db, sub)
            return JSONResponse({"errors": {"mate_link": mate["reason"]}}, status_code=422)
        is_test = is_test or mate["is_test"]  # a friend inherits the enquirer's data set, so test data never mixes with real data
        joining = 1 + len([m for m in (body.get("party_members") or []) if isinstance(m, dict) and (m.get("name") or "").strip()])
        if mate["people_so_far"] + joining > mate["limit"]:
            if sub:
                submissions.release(db, sub)
            return JSONResponse({"errors": {"mate_link": f"Sorry, {mate['enquirer_first_name']}'s group is already as big as a shared link allows ({mate['limit']} people, leaving room for at least one more housemate in our largest available apartment), so it cannot take {joining} more."}}, status_code=422)
    try:
        # D1: the public form never sets the Staff-only section; drop those keys (hidden inputs carry DOM defaults such as channel "Phone")
        e = _create_guarded(sub, receipt=True, a=strip_staff_only(body), channel=channel, is_test=is_test, email_link_id=link_id, party_max=matching.mate_limit(db, "test" if is_test else "real"))
    except ValidationError as ve:
        return JSONResponse({"errors": ve.errors}, status_code=422)
    except submissions.Superseded:
        # this request stalled and a retry with the same key took over (or the database refused a second row for the key):
        # nothing of ours was written; answer with whatever the other attempt saved
        again = _reread(sub, "public", body, sub_extra, sub_test)
        early = _submission_replies(again)
        return early if early is not None else _public_replay(again, mate, draft_token)
    try:
        out = _public_finalise(e, channel, is_test, mate, draft_token)
    except Exception as ex:
        # The enquiry is saved (and the key knows its id). A failure in what follows must not read as "not sent": reply with
        # the reference; a retry with the same key finishes the side effects without a second enquiry.
        audit(db, None, "submit_side_effect_error", "enquiry", e["id"], "UNEXPECTED:" + matching.run_error_type_token(ex))   # T5: closed token, never str(ex)
        out = {"id": e["id"], "first_name": e["first_name"], "move_in": e["move_in"], "channel": channel, "is_test": is_test, "mate_link": None,
               "warning": "Your enquiry was saved, but one of the follow-up steps did not finish. Our team will complete it."}
        if sub:
            try:
                submissions.keep_partial(db, sub.key, e["id"], out)  # stays 'saved': a retry with the same key finishes the follow-up steps
            except Exception:
                pass
        return JSONResponse(out, status_code=201)
    if sub:
        submissions.finish(db, sub.key, e["id"], out)
    return out


# I19(f): save and finish later. The token lives only on the server; drafts expire in line with retention (T5, 12 months).
DRAFT_DAYS = 365


def _draft_out(d: dict) -> dict:
    from .enquiries import clean_answers
    answers = pj(d.get("answers_json"), {})
    # I8: a draft saved before 8 November 2026 may hold dob/sex/age/sex-preference/guardian/consent_age answers; only current
    # question keys are ever sent back to the form, so nothing retired can be rehydrated or read as the new declaration
    return {"token": d["token"], "answers": clean_answers(answers) if isinstance(answers, dict) else {}, "step": d.get("step"), "email": d.get("email"), "is_test": bool(d.get("is_test")),
            "mate_token": d.get("mate_token"), "email_token": d.get("email_token"), "updated_at": d.get("updated_at"), "expires_at": d.get("expires_at"),
            "submit_key": d.get("submit_key"),  # the form's opaque submission key, so a resumed form retries with the same key (never a duplicate)
            "origin": d.get("origin")}  # H5: 'homebot' when the saved form carries HomeBot hand-off answers (the form words its banner accordingly)


def _draft_fields(body: dict) -> tuple[str, int, Optional[str]]:
    """Review M2/M4: a draft is a JSON object of answers (≤ 64 KB), a screen number and an optional email."""
    from .enquiries import clean_answers
    answers = body.get("answers") or {}
    if not isinstance(answers, dict):
        raise HTTPException(422, "The saved answers must be a JSON object.")
    answers_json = j(clean_answers(answers))   # I8: only current question keys are kept, even in a draft
    if len(answers_json.encode("utf-8")) > sec.MAX_DRAFT_BYTES:
        raise HTTPException(422, "This saved form is too large to keep.")
    email = str(body.get("email") or "").strip()[:200] or None
    return answers_json, sec.as_int(body.get("step"), 0, 0, 999), email


def _opt_token(v) -> Optional[str]:
    return (str(v).strip()[:200] or None) if v else None


@app.post("/api/public/drafts", status_code=201)
async def create_draft(req: Request):
    body = _pilot_force_test(await sec.json_object(req))
    sec.rate_limit(req, "public_drafts", sec.RATE_DRAFTS_PER_HOUR, 3600)
    answers_json, step, email = _draft_fields(body)
    token = secrets.token_urlsafe(18)
    db.execute("INSERT INTO form_drafts(token, answers_json, step, email, is_test, mate_token, email_token, created_at, updated_at, expires_at, submit_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (token, answers_json, step, email, 1 if body.get("is_test") else 0,
                _opt_token(body.get("mate_token")), _opt_token(body.get("email_token")), now_iso(), now_iso(), _iso_in(DRAFT_DAYS), submissions.clean_key(body.get("submit_key"))))
    db.commit()
    return _draft_out(row(db.execute("SELECT * FROM form_drafts WHERE token=?", (token,))))


@app.put("/api/public/drafts/{token}")
async def update_draft(token: str, req: Request):
    d = row(db.execute("SELECT * FROM form_drafts WHERE token=?", (token,)))
    if not d:
        raise HTTPException(404, "This saved form was not found. It may have been sent already.")
    if d["expires_at"] < now_iso():
        raise HTTPException(410, "This saved form has expired.")
    body = await sec.json_object(req)
    sec.rate_limit(req, "public_drafts", sec.RATE_DRAFTS_PER_HOUR, 3600)
    answers_json, step, email = _draft_fields(body)
    db.execute("UPDATE form_drafts SET answers_json=?, step=?, email=COALESCE(?, email), updated_at=?, submit_key=COALESCE(submit_key, ?) WHERE token=?",
               (answers_json, step, email, now_iso(), submissions.clean_key(body.get("submit_key")), token))  # the first key sticks to the draft
    db.commit()
    return _draft_out(row(db.execute("SELECT * FROM form_drafts WHERE token=?", (token,))))


@app.get("/api/public/drafts/{token}")
def get_draft(token: str):
    d = row(db.execute("SELECT * FROM form_drafts WHERE token=?", (token,)))
    if not d:
        raise HTTPException(404, "This saved form was not found. It may have been sent already.")
    if d["expires_at"] < now_iso():
        raise HTTPException(410, "This saved form has expired.")
    return _draft_out(d)


@app.post("/api/public/validate")
async def validate_public(req: Request):
    from .enquiries import validate
    body = _pilot_force_test(await sec.json_object(req))
    sec.rate_limit(req, "public_drafts", sec.RATE_DRAFTS_PER_HOUR, 3600)
    return {"errors": validate(body, party_max=matching.mate_limit(db, "test" if body.get("is_test") else "real"))}


@app.get("/api/public/countries/normalise")
def country_normalise(q: str):
    q = (q or "")[:120]
    name, ok = normalise_country(q)
    return {"input": q, "country": name, "confident": ok}


# ---------------------------------------------------------------------------
# Staff: enquiries
# ---------------------------------------------------------------------------
@app.post("/api/enquiries/{eid}/bar")
async def enquiry_bar(eid: int, req: Request, staff=Depends(current_staff)):
    """GP9(c)/(e): bar ONE person from matching (reason required). Body: `reason`; for a form party `person` ("lead" or a party
    member id) and `party_choice` ("leave" | "stay"); for "Bring a mate" friends in the same group `friends_choice` ("leave" | "stay").
    The people they came with are never barred (GP9(e)). Barred people are in no run and no replacement until lifted."""
    body = await sec.json_object(req)
    try:
        return matching.bar_enquiry(db, eid, staff["display_name"], body.get("reason", ""), friends_choice=body.get("friends_choice"),
                                    person=body.get("person"), party_choice=body.get("party_choice"))
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e).lower() else 422, str(e))


@app.post("/api/enquiries/{eid}/lift-bar")
async def enquiry_lift_bar(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req, empty_ok=True)
    try:
        return matching.lift_bar(db, eid, staff["display_name"], body.get("note"))
    except ValueError as e:
        raise HTTPException(422 if "not barred" in str(e) else 404, str(e))


def _with_adults_status(items: list[dict]) -> list[dict]:
    """I11(d): adults_status (confirmed | renew | missing) for each listed enquiry, from the stored declaration and the current
    party only. One query for all parties."""
    from .enquiries import adults_status
    ids = [it["id"] for it in items]
    members: dict[int, list] = {}
    if ids:
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start: chunk_start + 500]
            for m in rows(db.execute(f"SELECT enquiry_id, name, email FROM party_members WHERE enquiry_id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk)):
                members.setdefault(m["enquiry_id"], []).append(m)
    for it in items:
        it["adults_status"] = adults_status(it, members.get(it["id"], []))
        it["adults_confirmed"] = bool(it.get("adults_confirmed"))
    return items


ADULTS_COLS = "id, first_name, last_name, email, move_in, party, party_size, adults_confirmed, adults_confirmed_by, adults_confirmed_at, adults_confirmed_move_in, adults_confirmed_party"


def _adults_todo_ids(is_test: int) -> list[int]:
    """Ids of open enquiries whose I11(d) declaration is missing or needs renewing; scanned in bounded batches."""
    out: list[int] = []
    last = 0
    while True:
        batch = rows(db.execute(f"SELECT {ADULTS_COLS} FROM enquiries WHERE is_test=? AND status NOT IN ('Withdrawn', 'Leased') AND id>? ORDER BY id LIMIT 500", (is_test, last)))
        if not batch:
            return out
        out += [it["id"] for it in _with_adults_status(batch) if it["adults_status"] != "confirmed"]
        last = batch[-1]["id"]


def _adults_todo_count(is_test: int) -> int:
    return len(_adults_todo_ids(is_test))


@app.get("/api/enquiries")
def list_enquiries(staff=Depends(current_staff), q: Optional[str] = None, status: Optional[str] = None, dataset: str = "real",
                   move_in_from: Optional[str] = None, move_in_to: Optional[str] = None, budget_min: Optional[float] = None, budget_max: Optional[float] = None,
                   school: Optional[str] = None, country: Optional[str] = None, channel: Optional[str] = None, todo: Optional[str] = None,
                   limit: int = 200, offset: int = 0):
    where, args = ["is_test=?"], [1 if dataset == "test" else 0]
    if todo == "adults":  # I11(d): the all-occupants 18+ at move-in declaration is missing or needs renewing (status computed per row, bounded scan)
        ids = _adults_todo_ids(1 if dataset == "test" else 0)
        where.append(f"id IN ({','.join('?' * len(ids)) or 'NULL'})")
        args += ids
    elif todo == "country":  # I15 / I16(b): a country of birth to confirm (enquirer or party member)
        where.append("(birth_country_confirmed=0 OR EXISTS (SELECT 1 FROM party_members pm WHERE pm.enquiry_id=enquiries.id AND pm.birth_country_raw IS NOT NULL AND pm.birth_country_confirmed=0))")
    elif todo == "barred":  # GP9(c): people (and their parties) staff have barred from matching
        where.append("barred_at IS NOT NULL")
    if q:
        where.append("(lower(first_name||' '||last_name) LIKE ? OR lower(email) LIKE ? OR phone LIKE ?)")
        args += [f"%{q.lower()}%", f"%{q.lower()}%", f"%{q}%"]
    if status:
        where.append("status=?"); args.append(status)
    if move_in_from:
        where.append("move_in>=?"); args.append(move_in_from)
    if move_in_to:
        where.append("move_in<=?"); args.append(move_in_to)
    if budget_min is not None:
        where.append("budget_max>=?"); args.append(budget_min)
    if budget_max is not None:
        where.append("budget_max<=?"); args.append(budget_max)
    if school:
        where.append("lower(coalesce(school,'')||' '||coalesce(school_other,'')) LIKE ?"); args.append(f"%{school.lower()}%")
    if country:  # I16(b): the enquirer's or any party member's country of birth
        where.append("""(lower(coalesce(birth_country,'')) LIKE ? OR lower(coalesce(birth_country_raw,'')) LIKE ?
                         OR EXISTS (SELECT 1 FROM party_members pm WHERE pm.enquiry_id=enquiries.id AND lower(coalesce(pm.birth_country,'')) LIKE ?))"""); args += [f"%{country.lower()}%"] * 3
    if channel:
        where.append("channel=?"); args.append(channel)
    sql = f"""SELECT id, is_test, first_name, last_name, email, status, channel, created_at, last_contact_at, move_in, move_out, budget_option, budget_max, budget_guide,
              birth_country, birth_country_raw, birth_country_confirmed, school, party, party_size, apt_sizes, barred_at, barred_reason, barred_person,
              adults_confirmed, adults_confirmed_by, adults_confirmed_at, adults_confirmed_move_in, adults_confirmed_party FROM enquiries WHERE {' AND '.join(where)}
              ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"""
    total = db.execute(f"SELECT COUNT(*) FROM enquiries WHERE {' AND '.join(where)}", args).fetchone()[0]
    items = _with_adults_status(rows(db.execute(sql, args + [limit, offset])))   # status computed for the page only
    for it in items:
        it["apt_sizes"] = pj(it["apt_sizes"])
        it.pop("adults_confirmed_party", None)
    return {"items": items, "total": total, "counts": {"real": db.execute("SELECT COUNT(*) FROM enquiries WHERE is_test=0").fetchone()[0],
                                                    "test": db.execute("SELECT COUNT(*) FROM enquiries WHERE is_test=1").fetchone()[0]}}


@app.get("/api/enquiries/{eid}")
def enquiry_detail(eid: int, staff=Depends(current_staff)):
    try:
        return get_enquiry(db, eid)
    except KeyError:
        raise HTTPException(404, "Not found")


@app.post("/api/enquiries", status_code=201)
async def staff_create_enquiry(req: Request, staff=Depends(current_staff)):
    body = _pilot_force_test(await sec.json_object(req))
    channel = body.get("channel") or "Phone"
    if channel not in CHANNELS:
        channel = "Phone"
    # review L7: a phone enquiry is attributed to the signed-in person; only the manager may record it under another name
    taken_by = staff["display_name"] if staff["role"] != "owner" else (body.get("staff_member") or staff["display_name"])
    # Submission key (31 Oct 2026): a double click or a retry after a lost reply replays the saved enquiry instead of making another
    scope, sub_extra, sub_test = f"staff:{staff['username']}", {"channel": channel}, bool(body.get("is_test"))
    sub = _submission_from(req, scope, body, sub_extra, sub_test)

    def staff_replay(s_):
        try:
            e_ = get_enquiry(db, s_.enquiry_id)
        except KeyError:
            return JSONResponse({"already_submitted": True, "errors": {"form": submissions.DELETED_MESSAGE}}, status_code=409)
        if s_.state == "saved":
            submissions.finish(db, s_.key, e_["id"], {"id": e_["id"], "first_name": e_["first_name"], "channel": e_["channel"]})
        return JSONResponse(dict(e_, replayed=True), status_code=200)

    early = _submission_replies(sub)
    if early is not None:
        return early
    if sub and sub.state in ("replay", "saved"):
        return staff_replay(sub)
    try:
        e = _create_guarded(sub, a=body, channel=channel, staff=taken_by, is_test=sub_test, party_max=matching.mate_limit(db, "test" if body.get("is_test") else "real"))
    except ValidationError as ve:
        return JSONResponse({"errors": ve.errors}, status_code=422)
    except submissions.Superseded:  # a retry with the same key took over while this request stalled: replay its result, never a second enquiry
        again = _reread(sub, scope, body, sub_extra, sub_test)
        early = _submission_replies(again)
        return early if early is not None else staff_replay(again)
    if sub:
        submissions.finish(db, sub.key, e["id"], {"id": e["id"], "first_name": e["first_name"], "channel": e["channel"]})
    return e


@app.patch("/api/enquiries/{eid}")
async def staff_update_enquiry(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        out = update_enquiry(db, eid, body, staff["display_name"])
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValidationError as ve:
        return JSONResponse({"errors": ve.errors}, status_code=422)
    except ConcurrentEdit as ce:   # the status changed underneath the edit (purge, group transition, another staff member)
        raise HTTPException(409, str(ce))
    if body.get("status") == "Withdrawn" and matching.linked_party_ids(db, eid) != [eid]:  # GP11(n): a withdrawn person leaves the friend party; the link passes on
        matching.unlink_from_party(db, eid, f"Withdrawn on {now_iso()[:10]}; no longer linked to their friends, who stay together.")
        out = get_enquiry(db, eid)
    return out


@app.patch("/api/enquiries/{eid}/members/{mid}")
async def staff_update_member(eid: int, mid: int, req: Request, staff=Depends(current_staff)):
    """I16(b): staff confirm or correct a party member's details (for example a country of birth MMAI could not recognise, I15)."""
    try:
        return update_party_member(db, eid, mid, await sec.json_object(req), staff["display_name"])
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValidationError as ve:   # I8: a retired field (sex, sex_other, dob) in the patch is refused with a clear 422, like the enquiry edit
        return JSONResponse({"errors": ve.errors}, status_code=422)
    except ConcurrentEdit as ce:   # the member row changed or went underneath the edit (guarded step, like the enquiry edit)
        raise HTTPException(409, str(ce))


@app.post("/api/enquiries/{eid}/contact")
async def staff_log_contact(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req, empty_ok=True)
    try:
        return log_contact(db, eid, staff["display_name"], body.get("note"))
    except KeyError:
        raise HTTPException(404, "Not found")  # an unknown MateBot id (for example a HomeBot number) is simply not found


@app.delete("/api/enquiries/{eid}")
def staff_delete_enquiry(eid: int, staff=Depends(current_staff)):
    # T5 retention guard: unlink + cleanup + delete + tombstone + audit are ONE guarded transaction (DB lock shared with every AI
    # writer, on every connection), committed once at guard exit; nothing in here commits on its own.
    try:
        with retention_guard(db, label="delete enquiry"):
            matching.unlink_from_party(db, eid)  # GP11(n): if they sent the share link, the first friend who joined takes it over
            scrub = forget_generated_text(db, [eid], include_leased=True)  # T5/T15: generated text about them (briefs, reviews, introduction drafts) goes too, leased group or not
            db.execute("DELETE FROM enquiries WHERE id=?", (eid,))
            submissions.tombstone(db, [eid])  # the form's submission key keeps only an opaque tombstone: a retry cannot recreate the deleted enquiry
            audit(db, staff["display_name"], "delete_enquiry", "enquiry", eid, f"generated text removed: {scrub['brief_rows']} brief row(s), {scrub['review_rows']} review row(s), {scrub['groups_cleared']} group(s) cleared, {scrub['allocations_scrubbed']} allocation(s) scrubbed, {scrub['audit_notes_cleared']} audit note(s) cleared, {scrub['email_links_deleted']} invitation link(s) deleted; {scrub['ai_failure_sinks_neutralised']} earlier AI failure text(s) neutralised; {scrub['audit_errors_neutralised']} earlier link/submission error note(s) neutralised")
    except RetentionLockTimeout:
        raise HTTPException(503, "The system is busy finishing another update. Nothing was deleted - try again in a moment.")
    except ConnectionLostMidOperation as e:
        raise HTTPException(503, "The database connection was lost" + (" while saving; check whether the enquiry is still listed before trying again." if e.outcome_unknown else ". Nothing was deleted - try again in a moment."))
    return {"ok": True, "generated_text_removed": scrub}


@app.post("/api/retention/purge")
def retention_purge(staff=Depends(current_staff)):
    out = purge_expired(db, staff["display_name"])
    out["homebot_removed"] = homebot.purge_expired(db)  # H14 HomeBot enquiries follow the same 12-month rule (separate tables)
    return out


@app.get("/api/retention/preview")
def retention_preview(staff=Depends(current_staff)):
    today = date.today()
    cutoff = homebot.retention_cutoff(today)
    due = rows(db.execute("""SELECT id, first_name, last_name, is_test, status, last_contact_at FROM enquiries e WHERE substr(last_contact_at,1,10) < ? AND status <> 'Leased'
                              AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN groups g ON g.id=gm.group_id WHERE gm.enquiry_id=e.id AND g.status='Leased')""", (cutoff,)))
    return {"cutoff": cutoff, "due": due, "homebot_due": homebot.retention_due(db, today),
            "next_scheduled_run": _next_purge_at,
            "last_completed_at": _last_purge_completed_at,
            "failures": dict(_purge_failures)}


# ---------------------------------------------------------------------------
# Staff: emailed form links (I4(b))
# ---------------------------------------------------------------------------
@app.post("/api/email-links", status_code=201)
async def create_email_link(req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    token = secrets.token_urlsafe(12)
    db.execute("INSERT INTO email_links(token, created_at, staff_member, recipient_email, recipient_name) VALUES (?,?,?,?,?)",
               (token, now_iso(), staff["display_name"], body.get("recipient_email"), body.get("recipient_name")))
    db.commit()
    audit(db, staff["display_name"], "create_email_link", "email_link", token, body.get("recipient_email"))
    return {"token": token}


@app.get("/api/email-links")
def list_email_links(staff=Depends(current_staff)):
    return rows(db.execute("SELECT * FROM email_links ORDER BY id DESC LIMIT 100"))


# ---------------------------------------------------------------------------
# Staff: apartments (A3)
# ---------------------------------------------------------------------------
DS_GROUP_SQL = "EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=groups.id AND e.is_test=?)"


def _is_test(dataset: Optional[str]) -> int:
    return 1 if dataset == "test" else 0


def _apt_out(a: dict, dataset: Optional[str] = None) -> dict:
    beds = [rules.parse_bedding(a[k]) for k in ("bedroom1", "bedroom2", "bedroom3") if a.get(k)]
    mn, mx = rules.apartment_capacity(beds)
    a["capacity_min"], a["capacity_max"] = mn, mx
    a["type_label"] = rules.APT_TYPES[a["type_code"]]["label"]
    a["monthly_12m"], a["monthly_6m"] = rules.monthly(a["rent_12m"]), rules.monthly(a["rent_6m"])
    a["a4b_ok"] = rules.capacity_fits_a4b(a["type_code"], mx)
    # every count follows the data set staff are looking at (Real or Test); without a data set, all groups are counted
    if dataset:
        a["proposed_groups"] = db.execute(f"SELECT COUNT(*) FROM groups WHERE apartment_id=? AND status<>'Dissolved' AND {DS_GROUP_SQL}", (a["id"], _is_test(dataset))).fetchone()[0]
    else:
        a["proposed_groups"] = db.execute("SELECT COUNT(*) FROM groups WHERE apartment_id=? AND status<>'Dissolved'", (a["id"],)).fetchone()[0]
    a["is_leased"] = bool(a.get("leased_at"))  # GP8: taken only when reservations marked it Leased
    a["is_available"] = bool(a.get("available_from")) and not a["is_leased"]
    # 8 Nov 2026 features: {key: 'unknown'|'yes'|'no'}; the raw feat_* columns stay off the wire, unknown is never shown as no
    a["features"] = feat.apartment_features(a)
    a["features_recorded"] = sum(1 for v in a["features"].values() if v != "unknown")
    for col in feat.COLUMNS:
        a.pop(col, None)
    # 10 Nov 2026 (T3(d) C3, A13(a)): the recorded step-free fact with its provenance; unknown is shown as unknown, never as no
    eff = access_facts.effective(a)
    v = eff["value"]
    a["step_free_access"] = "yes" if v == 1 else "no" if v == 0 else "unknown"
    a["step_free_label"] = access_facts.LABELS[v] if eff["bound"] else access_facts.MOVED_LABEL
    a["step_free_source"] = eff["source"]
    return a


@app.get("/api/apartments/{aid}/access-facts")
def get_access_facts(aid: int, staff=Depends(current_staff)):
    """The apartment's current step-free record (value / source / scope / note / who / when) and its append-only history."""
    if not row(db.execute("SELECT id FROM apartments WHERE id=?", (aid,))):
        raise HTTPException(404, "Not found")
    return {"current": access_facts.current(db, aid), "history": access_facts.history(db, aid)}


@app.post("/api/apartments/{aid}/access-facts")
async def post_access_fact(aid: int, req: Request, staff=Depends(current_staff)):
    """Record the step-free fact for ONE apartment: {"step_free_access": "yes"|"no"|"unknown" (required, explicit), "source"?: str,
    "scope"?: str, "note"?: str}. Audited under the authenticated staff account (the display name is only the human-facing
    provenance); the previous record is superseded, never deleted. Then every stored review that offers this apartment and no longer
    matches today's facts is queued for a CURRENT AI review (access_facts.apply_to_groups) - nothing is re-stamped or released by
    hand; the result says what was queued."""
    body = await sec.json_object(req)
    if "step_free_access" not in body:
        raise HTTPException(422, "step_free_access is required: yes, no or unknown")
    if isinstance(body.get("step_free_access"), (list, dict)):
        raise HTTPException(422, "step-free access must be yes, no or unknown")
    try:
        value = access_facts.value_from_text(body.get("step_free_access"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    texts = {}
    for k, limit in (("source", 200), ("scope", 200), ("note", 1000)):
        v = body.get(k)
        if v is None:
            texts[k] = None
            continue
        if not isinstance(v, str):
            raise HTTPException(422, f"{k} must be text")
        v = v.strip()
        if len(v) > limit:
            raise HTTPException(422, f"{k} is limited to {limit} characters")
        texts[k] = v or None
    source = texts["source"] or f"{staff['display_name']}, recorded in MateBot"
    actor = staff.get("username") or staff["display_name"]
    try:
        with retention_guard(db, label="access fact"):
            cur = access_facts.record(db, aid, value, source=source, staff=actor, scope=texts["scope"], note=texts["note"], commit=False)
    except ValueError as e:
        raise HTTPException(404 if "unknown apartment" in str(e) else 422, str(e))
    applied = access_facts.apply_to_groups(db, aid, actor)
    return {"current": cur, "groups": applied}


@app.get("/api/apartments")
def list_apartments(staff=Depends(current_staff), q: Optional[str] = None, type_code: Optional[str] = None, available_only: bool = False, dataset: Optional[str] = None):
    where, args = ["1=1"], []
    if q:
        where.append("lower(address) LIKE ?"); args.append(f"%{q.lower()}%")
    if type_code:
        where.append("type_code=?"); args.append(type_code)
    if available_only:
        where.append("available_from IS NOT NULL AND leased_at IS NULL")  # GP8
    items = [_apt_out(a, dataset) for a in rows(db.execute(f"SELECT * FROM apartments WHERE {' AND '.join(where)} ORDER BY leased_at IS NOT NULL, available_from IS NULL, available_from, address", args))]
    return {"items": items, "total": len(items)}


@app.get("/api/apartments/export.xlsx")
def export_apartments(staff=Depends(current_staff)):
    """The apartment list as a workbook: the nine baseline columns in their original order, then the 20 feature columns
    (Yes / No / blank = unknown), the car-parking charge and the minimum lease, so staff can fill the columns in Excel and
    re-import the same file (Settings). Nothing is inferred for blanks."""
    data = importer.export_xlsx(db)
    audit(db, staff["display_name"], "export_apartments", "apartments", None, f"{len(data)} bytes")
    name = f"ALM_Apartments_{date.today().isoformat()}.xlsx"
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/apartments/{aid}")
def apartment_detail(aid: int, staff=Depends(current_staff), dataset: Optional[str] = None):
    a = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    if not a:
        raise HTTPException(404, "Not found")
    a = _apt_out(a, dataset)
    a["groups"] = _groups_for_apartment(aid, dataset)
    a["runs"] = rows(db.execute("SELECT * FROM match_runs WHERE apartment_id=? ORDER BY id DESC LIMIT 5", (aid,)))
    # GP8 / D: a lease's follow-up (re-ranking the other groups) may not have finished; the truth comes from the data (builder's
    # pending_lease_reranks), and staff finish it with the idempotent "Finish updating other groups" action (same POST, same binding)
    # groups whose follow-up after a stock change (lease, withdrawal, change) of THIS apartment has not finished yet
    a["rerank_pending_group_ids"] = sorted({int(p["group_id"]) for p in stock_lifecycle.pending_work(db, apartment_id=aid)}
                                           | ({p["group_id"] for p in matching.pending_lease_reranks(db) if p["apartment_id"] == aid} if a["is_leased"] else set()))
    return a


APT_FIELDS = {"address", "suburb", "type_code", "rent_12m", "rent_6m", "available_from", "min_lease_months", "bedroom1", "bedroom2", "bedroom3", "bills_included", "city", "lat", "lon",
              "parking_charge"} | set(feat.COLUMNS)


def _validate_apt(body: dict, partial: bool = False) -> dict:
    out = {}
    feats = body.get("features")
    if feats is not None:   # {key: 'unknown'|'yes'|'no'} (or true/false/null); unknown keys and other values are refused, nothing is guessed
        if not isinstance(feats, dict):
            raise HTTPException(422, "features must be an object of feature keys")
        for k, v in feats.items():
            if k not in feat.KEY_OF.values():
                raise HTTPException(422, f"Unknown feature {k!r}")
            try:
                out[f"feat_{k}"] = feat.value_of(v)
            except ValueError:
                raise HTTPException(422, f"{feat.LABEL_OF[k]}: use yes, no or unknown")
    for k, v in body.items():
        if k not in APT_FIELDS:
            continue
        if k.startswith("feat_"):
            try:
                v = feat.value_of(v)
            except ValueError:
                raise HTTPException(422, f"{k}: use yes, no or unknown")
        if k == "parking_charge":
            if v is not None and not isinstance(v, str):
                raise HTTPException(422, "Car parking charge: enter it as text, as stated (e.g. “$60 per week”)")
            v = v.strip() if v else None
            if v and len(v) > feat.MAX_PARKING_CHARGE:
                raise HTTPException(422, f"Car parking charge: {feat.MAX_PARKING_CHARGE} characters at most")
        if k == "type_code":
            v = rules.normalise_type(v)
        if k in ("bedroom1", "bedroom2", "bedroom3") and v:
            rules.parse_bedding(v)  # raises on bad value (A4(d))
        if k in ("rent_12m", "rent_6m", "min_lease_months", "lat", "lon") and v not in (None, ""):
            v = float(v)
        if k == "available_from" and v in ("",):
            v = None
        if k == "bills_included":
            v = 1 if v in (True, 1, "1", "Included", "true", "Yes") else 0
        out[k] = v
    if not partial:
        for k in ("address", "type_code", "rent_12m", "rent_6m", "bedroom1"):
            if out.get(k) in (None, ""):
                raise HTTPException(422, f"{k} is required")
    return out


@app.post("/api/apartments", status_code=201)
async def create_apartment(req: Request, staff=Depends(current_staff)):
    try:
        f = _validate_apt(await sec.json_object(req))
    except ValueError as e:
        raise HTTPException(422, str(e))
    f.setdefault("city", "Melbourne"); f.setdefault("suburb", "Melbourne CBD"); f.setdefault("min_lease_months", rules.DEFAULT_MIN_LEASE_MONTHS)
    f.setdefault("bills_included", 0); f.setdefault("available_from", None)
    f["bathrooms"] = rules.APT_TYPES[f["type_code"]]["bathrooms"]
    f["address_raw"] = f["address"]; f["updated_at"] = now_iso()
    keys = list(f)
    db.execute(f"INSERT INTO apartments({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [f[k] for k in keys])
    aid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    audit(db, staff["display_name"], "create_apartment", "apartment", aid, f["address"])
    _homebot_inventory_changed("apartment added", [aid])
    return _apt_out(row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,))))


@app.patch("/api/apartments/{aid}")
async def update_apartment(aid: int, req: Request, staff=Depends(current_staff)):
    try:
        f = _validate_apt(await sec.json_object(req), partial=True)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if "type_code" in f:
        f["bathrooms"] = rules.APT_TYPES[f["type_code"]]["bathrooms"]
    f["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in f)
    # Stock lifecycle (A3(c)/(d)/(f), GP8): the change and the follow-up work for the groups that list this apartment are ONE
    # guarded step. Clearing the available date withdraws the apartment from available stock (every group loses that option);
    # any other material change (dates either way, rents, type, bedding, bills, address, features) is re-checked per group with
    # the actual rules - the stock itself stays listed. Non-material text (parking charge wording) triggers nothing.
    rec = None
    with retention_guard(db, label="apartment update"):
        before = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
        if not before:
            raise HTTPException(404, "Not found")
        changes = stock_lifecycle.material_changes(before, {k: v for k, v in f.items() if k != "updated_at"})
        bound_before = access_facts.bound_snapshot(db) if set(f) & set(access_facts.LOCATION_FIELDS) else {}
        db.execute(f"UPDATE apartments SET {sets} WHERE id=?", list(f.values()) + [aid])
        unbound = access_facts.unbound_since(db, {aid: bound_before[aid]} if aid in bound_before else {}, staff["display_name"], "staff edit")
        if changes:
            cause = "withdrawn" if ("available_from" in changes and f.get("available_from") is None) else "changed"
            rec = stock_lifecycle.record_transition(db, aid, cause, staff["display_name"], changed_fields=list(changes))
        audit(db, staff["display_name"], "update_apartment", "apartment", aid, ", ".join(k for k in f if k != "updated_at")
              + (f"; {cause} -> {len(rec['group_ids'])} group(s) to re-check" if rec else ""))
    followup = _stock_followup(rec, staff["display_name"]) if rec else None
    a = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    if not a:
        raise HTTPException(404, "Not found")
    _homebot_inventory_changed("apartment changed", [aid])
    out = _apt_out(a)
    if followup:
        out["groups_rechecked"] = followup
    if unbound:
        out["access_facts_unbound"] = unbound   # G4: a moved row's confirmation no longer applies - reported, never silent
    return out


@app.delete("/api/apartments/{aid}")
def delete_apartment(aid: int, staff=Depends(current_staff)):
    """A3a: permanent deletion of the APARTMENT, no archive - not of the groups that listed it. In ONE guarded step the follow-up
    work for every live group that lists it is captured (group id + role), then the row is deleted (groups.apartment_id goes NULL,
    the option rows go); each group is then re-checked in its own step: next valid option or held with no offer, H19 alert, GP8(b)
    refresh. Group identity, members, reviews, briefs and GP13 history stay (stale where they named this apartment)."""
    rec = None
    with retention_guard(db, label="apartment delete"):
        a = row(db.execute("SELECT id, address FROM apartments WHERE id=?", (aid,)))
        removed = None
        if a:
            removed = access_facts.confirmations_at(db, [aid])   # G2: the confirmation goes with the apartment (A3(a)); say so
            rec = stock_lifecycle.record_transition(db, aid, "deleted", staff["display_name"])
            db.execute("DELETE FROM apartments WHERE id=?", (aid,))
            removed = access_facts.audit_removed(db, removed, staff["display_name"], "apartment deleted")
        audit(db, staff["display_name"], "delete_apartment", "apartment", aid, (a["address"] if a else None) if not rec else f"{a['address']}; {len(rec['group_ids'])} group(s) to re-check")
    followup = _stock_followup(rec, staff["display_name"]) if rec else None
    _homebot_inventory_changed("apartment deleted", [aid])
    out = {"ok": True}
    if followup:
        out["groups_rechecked"] = followup
    if removed and removed["count"]:
        out["access_facts_removed"] = removed
    return out


def _stock_followup(rec: dict, staff_name: str) -> dict:
    """Transactions 2..n of a stock transition, right after transaction 1 committed (GP8 immediate re-rank). A group whose step
    fails is reported pending (durable work row); the inventory change itself is never undone or hidden."""
    failed: list = []
    try:
        affected = stock_lifecycle.run_pending(db, transition_id=rec["transition_id"], staff=staff_name, failures=failed)
    except (RetentionLockTimeout, ConnectionLostMidOperation) as e:
        return {"transition_id": rec["transition_id"], "status": "pending", "affected": [], "pending_group_ids": list(rec.get("group_ids") or []),
                "message": f"The apartment change is recorded, but the follow-up re-check of {len(rec.get('group_ids') or [])} group(s) did not finish ({type(e).__name__}); it is retried automatically and from the group pages."}
    pending = [f["group_id"] for f in failed]
    return {"transition_id": rec["transition_id"], "status": "complete" if not pending else "partial", "affected": affected, "pending_group_ids": pending}


@app.post("/api/apartments/{aid}/lease")
async def lease_apartment(aid: int, req: Request, staff=Depends(current_staff)):
    """GP8: reservations marks the apartment Leased (the only way an apartment becomes taken). Optional body {group_id} records
    which group leased it and marks that group Leased; every other group is re-ranked to its next best fit immediately.
    The whole claim (optional offer switch, apartment claim, group status, members) is ONE guarded step in _set_group_status, so a
    refusal can no longer arrive after the group was already marked Leased. Without a group, the apartment claim alone is the
    guarded step. Repeating the request for the SAME binding (this group, or no group) never claims twice: it runs the follow-up
    re-ranking again (idempotent), which is how a follow-up that did not finish is completed. A group already Leased whose own
    apartment was never claimed gets that claim completed for that same group and apartment only; another claimant is refused."""
    body = await sec.json_object(req, empty_ok=True)
    gid = int(body["group_id"]) if body.get("group_id") else None
    if gid:
        rep = _set_group_status(gid, "Leased", staff["display_name"], apartment_id=aid)
        if rep is not None:
            rep["apartment"] = _apt_out(row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,))))
            return rep
    with retention_guard(db, label="apartment lease"):
        if gid:   # group already Leased with THIS apartment (checked above, re-checked here) and the apartment not claimed for it
            g = row(db.execute("SELECT status, apartment_id FROM groups WHERE id=?", (gid,)))
            if not g or g["status"] != "Leased" or g["apartment_id"] != aid:
                raise HTTPException(409, "The group changed while you were working. Reload the page and check it again.")
        claim = _claim_apartment(aid, gid, staff["display_name"], missing=404, repeat_ok=True)
        if not claim["repeated"]:
            audit(db, staff["display_name"], "apartment_claim", "apartment", aid, f"{claim['address']}; group {gid}; claimed at {claim['leased_at']}")
    rep = _after_claim(claim, staff["display_name"], gid)
    rep["apartment"] = _apt_out(row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,))))
    _homebot_inventory_changed("apartment leased", [aid])
    return rep


@app.post("/api/apartments/{aid}/relist")
async def relist_apartment(aid: int, req: Request, staff=Depends(current_staff)):
    """GP8 / A3(c): a leased apartment comes back on the market with a new availability date."""
    body = await sec.json_object(req, empty_ok=True)
    if not row(db.execute("SELECT id FROM apartments WHERE id=?", (aid,))):
        raise HTTPException(404, "Not found")
    matching.relist_apartment(db, aid, body.get("available_from") or None, staff["display_name"])
    _homebot_inventory_changed("apartment relisted", [aid])
    return _apt_out(row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,))))


IMPORT_CONFIRM_WORD = "IMPORT"


@app.post("/api/apartments/import")
async def import_apartments(req: Request, staff=Depends(current_staff), confirm: str = "", replace: bool = False, features_overwrite: bool = False):
    """Body: raw .xlsx bytes (Content-Type application/octet-stream), or empty to re-import the 23 Oct 2026 list.
    Lives under Settings. The staff member must type the confirmation word. The import updates apartments by address (ids, lease
    status, groups and the minimum lease are kept; a date staff entered is kept when the list has no date), adds new addresses and
    leaves addresses missing from the list untouched; the agreed list corrections (A15) are re-applied afterwards.
    `replace=true` (not offered in the staff screens) wipes the table first.
    Feature columns (8 Nov 2026): Yes/No cells are recorded, blanks leave recorded values alone, other text is reported as an error for
    that cell; a sheet value that differs from a value staff recorded is reported and kept unless `features_overwrite=true`."""
    if confirm.strip().upper() != IMPORT_CONFIRM_WORD:
        raise HTTPException(422, f"Type {IMPORT_CONFIRM_WORD} to confirm the import")
    path, uploaded = XLSX_DEFAULT, None
    data = await req.body()
    if len(data) > sec.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "That spreadsheet is too large.")
    if data and len(data) > 100:
        path = uploaded = os.path.join(HERE, "data", "upload_" + secrets.token_hex(4) + ".xlsx")
        with open(path, "wb") as fh:
            fh.write(data)
    if not os.path.exists(path):
        raise HTTPException(404, "No apartment list found")
    try:
        rep = importer.import_apartments(db, path, staff["display_name"], replace=replace, features_overwrite=features_overwrite)
    except importer.WorkbookError as ex:
        raise HTTPException(422, f"The list was not loaded: {ex}")
    finally:
        if uploaded:  # review L8: the uploaded file is not kept after the import
            try:
                os.remove(uploaded)
            except OSError:
                pass
    _homebot_inventory_changed("apartment list imported", None)
    return rep


def _homebot_inventory_changed(reason: str, apartment_ids):
    """T3(a) H2: after any apartment change, queue a fresh HomeBot options run for every open HomeBot enquiry (no-op while
    MMAI_HOMEBOT_MATCHING is off). Never fails the apartment operation that triggered it."""
    try:
        return homebot_shortlist.on_inventory_changed(db, reason, apartment_ids)
    except Exception as ex:  # noqa: BLE001
        homebot_shortlist.log.warning("homebot inventory trigger not recorded kind=%s", type(ex).__name__)
        return None


@app.get("/api/apartments-data-quality")
def apartments_dq(staff=Depends(current_staff)):
    return importer.data_quality(db)


# ---------------------------------------------------------------------------
# Staff: matching and groups
# ---------------------------------------------------------------------------
def _groups_for_apartment(aid: int, dataset: Optional[str] = None) -> list[dict]:
    """Groups for which this apartment is on the ranked list (GP6), current-apartment groups first."""
    gs = rows(db.execute("""SELECT g.*, ga.rank AS apt_rank FROM groups g JOIN group_apartments ga ON ga.group_id=g.id AND ga.apartment_id=?
                            WHERE g.status<>'Dissolved' ORDER BY CASE g.status WHEN 'Leased' THEN 0 WHEN 'Awaiting replacement' THEN 1 WHEN 'Group confirmed' THEN 2 WHEN 'Introduced' THEN 3 ELSE 4 END,
                            (g.apartment_id=?) DESC, g.is_primary DESC, g.ai_rating DESC, g.rule_score DESC""", (aid, aid)))
    legacy = rows(db.execute("SELECT g.*, 1 AS apt_rank FROM groups g WHERE g.apartment_id=? AND g.status<>'Dissolved' AND NOT EXISTS (SELECT 1 FROM group_apartments ga WHERE ga.group_id=g.id)", (aid,)))
    occ = _occupancy_map()
    out = [_group_out(g, occ) for g in gs + legacy]
    if dataset:
        out = [g for g in out if g["is_test"] == (dataset == "test")]
    return out


def _occupancy_map() -> dict:
    """GP8: apartment_id -> {'leased': {...} | None, 'first_choice': [group ids]}. An apartment is taken ONLY when reservations marked it
    Leased (apartments.leased_at). Proposed groups that share a first-choice apartment are merely noted; nothing is held."""
    m: dict = {}
    for r in rows(db.execute("SELECT id, address, leased_at, leased_group_id, leased_by FROM apartments WHERE leased_at IS NOT NULL")):
        m.setdefault(r["id"], {"leased": None, "first_choice": []})["leased"] = {"group_id": r["leased_group_id"], "status": "Leased", "leased_at": r["leased_at"], "leased_by": r["leased_by"]}
    for r in rows(db.execute("SELECT id, apartment_id FROM groups WHERE status='Proposed' AND is_primary=1")):
        m.setdefault(r["apartment_id"], {"leased": None, "first_choice": []})["first_choice"].append(r["id"])
    return m


def _group_out(g: dict, occ: dict | None = None, circ: dict | None = None) -> dict:
    if occ is None:
        occ = _occupancy_map()
    if circ is None and g.get("status") not in ("Leased", "Dissolved") and (g.get("ai_status") == "ok" or (g.get("ai_status") is None and g.get("ai_rating") is not None)):
        circ = _circulation(g)   # T6: readiness comes from the central predicate, never from the ai_status flag alone
    g["allocation"] = pj(g.pop("allocation_json"), {})
    g["members"] = rows(db.execute("""SELECT gm.enquiry_id, gm.weekly_share, gm.room, gm.bed, gm.arrangement, e.first_name, e.last_name, e.email, e.phone_code, e.phone, e.status AS enquiry_status,
                                      e.party, e.party_size, e.consent_share, e.is_test, e.birth_country, e.intro, e.linked_to_enquiry_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? ORDER BY gm.room, gm.id""", (g["id"],)))
    g["is_test"] = any(m["is_test"] for m in g["members"])
    g["people"] = sum((m["party_size"] or 1) for m in g["members"])
    # GP11: linked friends in this group, in plain language ("Keep together: Ana + Ben"), and any friend waiting for approval
    mids = [m["enquiry_id"] for m in g["members"]]
    parties: dict[int, list] = {}
    if mids:
        for r in rows(db.execute(f"SELECT id, first_name, linked_to_enquiry_id FROM enquiries WHERE id IN ({','.join('?' * len(mids))})", mids)):
            parties.setdefault(r["linked_to_enquiry_id"] or r["id"], []).append(r)
    g["linked_parties"] = [{"root_id": k, "enquiry_ids": [x["id"] for x in v], "names": [x["first_name"] for x in v]} for k, v in parties.items() if len(v) > 1]
    for m in g["members"]:
        party = next((p for p in g["linked_parties"] if m["enquiry_id"] in p["enquiry_ids"]), None)
        m["linked_with"] = [n for n, i in zip(party["names"], party["enquiry_ids"]) if i != m["enquiry_id"]] if party else []
    g["mate_requests"] = matching.pending_mate_requests(db, group_id=g["id"]) if g["status"] not in ("Leased", "Dissolved") else []
    g["liability_note"] = rules.liability_note(g["total_weekly"] or 0)  # B5
    g["is_primary"] = bool(g.get("is_primary", 1))
    # stock lifecycle: a group can sit without a current offer (apartment deleted / withdrawn / leased with nothing else valid):
    # visibly held, never a placeholder; work still pending after a stock change is shown and blocks readiness (ai_queue.is_ready)
    g["no_current_offer"] = g.get("apartment_id") is None
    g["stock_recheck_pending"] = stock_lifecycle.group_has_pending_work(db, g["id"]) if g.get("status") not in ("Leased", "Dissolved") else False
    g["offer_changed_since_intro"] = bool(g.get("offer_changed_since_intro_at"))
    # GP6: ranked apartment list with whole weekly rent, per-room shares and the AI's reason; "taken" flags for GP6(b)
    apts = rows(db.execute("""SELECT ga.*, a.address, a.suburb, a.type_code, a.available_from, a.bills_included FROM group_apartments ga JOIN apartments a ON a.id=ga.apartment_id
                              WHERE ga.group_id=? ORDER BY ga.rank""", (g["id"],)))
    practical_members = ai_queue.essential_need_members(db, g["id"]) if apts else []
    for a in apts:
        al = pj(a.pop("allocation_json"), {})
        a["rooms"] = al.get("rooms") or []
        a["type_label"] = rules.APT_TYPES[a["type_code"]]["label"]
        a["is_current"] = a["apartment_id"] == g["apartment_id"]
        a["liability_note"] = rules.liability_note(a["total_weekly"] or 0)
        # The current-offer gate also labels every alternative. A ranked candidate
        # is not a confirmed suitable option merely because the group is ready.
        practical = ai_queue.essential_need_gate(db, g["id"], a["apartment_id"], members=practical_members)
        a["practical_need_hold"] = practical["hold"]
        a["practical_need_reason"] = practical["reason"]
        info = occ.get(a["apartment_id"], {"leased": None, "first_choice": []})
        leased = info["leased"]
        a["taken_by"] = [leased] if leased and leased.get("group_id") != g["id"] else []       # GP8: taken only when marked Leased
        a["also_first_choice_of"] = [{"group_id": x} for x in info["first_choice"] if x != g["id"]]  # information only; nothing is held
    staff_view.apartment_reasons(db, g, apts)   # T10: the AI's apartment reasons in plain words through their own saved basis
    # GP6(d): at most three apartments are a group's shortlist. Every stored candidate stays listed, in its stored order (GP14
    # history included); the ones beyond three are reserves that move up only when a place opens. Travel is NOT part of this
    # stored view: the final travel check is run live from the group page (matebot_live) and nothing from it is stored.
    # GP14: listed apartments a current member has declined. Read once here and carried on the group, so the list views, the
    # detail view and the roles below all use the same answer.
    # A closed group (leased or dissolved) is not read here, as before: its roles ignore declines, and only its own detail
    # view reads them (group_detail), so the list views make no query for closed groups.
    closed = g.get("status") in ("Leased", "Dissolved")
    declined = apartment_rejections.declined_in_group(db, g["id"]) if apts and not closed else {}
    if not closed:
        g["declined_apartments"] = {str(k): v for k, v in declined.items()}
    declined_ids = {int(k) for k in declined}
    for a, role in zip(apts, matebot_travel.shortlist_roles([{"id": a["apartment_id"], "current": a["is_current"],
                                                              "unavailable": bool(a["taken_by"]) or a["apartment_id"] in declined_ids} for a in apts])):
        a["shortlist_role"] = role
    g["shortlist_max"] = matebot_travel.MAX_SHORTLIST
    # the final travel check is live and never stored (matebot_live): the view carries only whether one can be run. Until
    # one has been run on the page, and its AI assessment accepted, the apartments are provisional and are shown as such.
    g["travel_live"] = matebot_live.state(is_test=bool(g.get("is_test")))
    g["travel_check"] = "live" if g["travel_live"]["enabled"] else "off"
    g["apartments"] = apts
    # the "next best" named when the current offer is leased is one staff can actually offer: shortlisted (so never a reserve,
    # a declined or a leased apartment) and not held for an essential need
    g["next_best"] = next((a for a in apts if not a["is_current"] and a["shortlist_role"] == matebot_travel.ROLE_SHORTLIST and not a["practical_need_hold"]), None)
    g["rejected"] = pj(g.get("rejected_json"), [])
    # T6: review state. Reservations only treat a group as ready once the AI has assessed it and ranked its apartments.
    st = g.get("ai_status")
    g["circulation"] = {"ok": bool(circ and circ.get("ok")), "reasons": list((circ or {}).get("reasons") or [])}
    g["ready"] = g["circulation"]["ok"]
    if g["status"] in ("Leased", "Dissolved"):
        g["review_state"] = g["status"]
    elif st == "ok" or (st is None and g.get("ai_rating") is not None):
        g["review_state"] = "Ready for reservations" if g["ready"] else "AI assessed; not ready"
    elif st == "failed":
        g["review_state"] = "AI assessment failed"
    elif st == "held" and g.get("ranking_basis") == ai_queue.NOTES_HELD_BASIS:
        g["review_state"] = "AI assessment held"   # C4: the automatic re-reads of a member's notes are used up; staff retry restarts them
    else:
        g["review_state"] = "AI assessment pending"
    g["ready_for_reservations"] = g["review_state"] == "Ready for reservations"
    # the staff retry control applies to processing failures only: a failed review, or the exhausted-notes hold. Other holds
    # (practical need to confirm, AI review hold) are staff decisions, not retries
    g["ai_retry_available"] = g["status"] not in ("Leased", "Dissolved") and (st == "failed" or (st == "held" and g.get("ranking_basis") == ai_queue.NOTES_HELD_BASIS))
    g["ai_refresh"] = bool(g.get("ai_refresh"))
    # T10 staff presentation: the AI's internal refs become names through the round's own immutable map (never today's names
    # for other people's text); failure detail stays on the database row (nothing is logged), staff see a plain actionable label
    g["ai_rationale"] = staff_view.rationale_for_group(db, g)
    g["ai_error"] = staff_view.ai_error_label(g)
    g["hold"] = staff_view.hold_view(db, g)
    g["held_reason"] = (g["hold"] or {}).get("label")
    # GP10: awaiting-replacement details for staff: empty beds, the uncovered part of the whole rent (B5; shares stay as a guide),
    # any replacement a later run found (never applied automatically) and the staff options as guidance
    g["awaiting_replacement"] = g["status"] == matching.AWAITING
    g["replacement"] = pj(g.get("replacement_json"), None) or None
    if g["awaiting_replacement"] or g.get("awaiting_note"):
        g["empty_beds"] = group_responses.empty_space_count(g["allocation"])
        # GP10(d): every remaining person's own stored share from the bed allocation (lead rows alone under-count the party); None = unknown
        g["uncovered_weekly"] = group_responses.uncovered_weekly(g["total_weekly"], g["allocation"], g["people"])
    if g["awaiting_replacement"]:
        g["staff_options"] = [
            {"key": "find", "label": "Ask the group to find one more person", "detail": "The members may know someone; a person they bring is added as a new enquiry and MateBot AI checks every rule before they join."},
            {"key": "lease", "label": "They can lease now and fill the last space later", "detail": "The group may lease the whole apartment with the empty bed and remain liable for the full rent: " + (f"about ${g['uncovered_weekly']:,.0f}/wk is not covered by the current shares." if g.get("uncovered_weekly") is not None else "the amount not covered by the current shares cannot be read from the stored bed allocation, so check the allocation before quoting a figure.")},
            {"key": "discount", "label": "Consider a discount (staff discretion)", "detail": "Record what you offer in the discount note below. MateBot AI never changes a price; the note is free text for staff only."},
        ]
    return g


@app.post("/api/match/run")
async def match_run(req: Request, staff=Depends(current_staff)):
    """One apartment: rules for every candidate group, AI on every passing group (T4). Full runs use /api/match/run-all (T6)."""
    body = await sec.json_object(req)
    aid = body.get("apartment_id")
    dataset = body.get("dataset", "real")
    use_ai = body.get("use_ai", True)
    if not aid:
        raise HTTPException(422, "apartment_id is required; use /api/match/run-all for the full run")
    try:
        return matching.run_match(db, int(aid), staff["display_name"], dataset=dataset, use_ai=use_ai, max_groups=int(body.get("max_groups", 12)))
    except matching.BookingChanged as e:
        raise HTTPException(409, str(e)) from None
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/match/run-all", status_code=202)
async def match_run_all(req: Request, staff=Depends(current_staff)):
    """T6 + GP6: full group-first run in the background (rules form the groups; new or changed groups are queued for the AI).
    Poll GET /api/match/runs/{run_id} for the run and GET /api/ai-queue for the AI queue."""
    body = await sec.json_object(req, empty_ok=True)
    dataset = body.get("dataset", "real")
    try:
        run_id = matching.start_full_run(db, staff["display_name"], dataset, use_ai=body.get("use_ai", True))
    except matching.RunInProgress as e:
        raise HTTPException(409, str(e))
    return matching.run_status(db, run_id)


@app.get("/api/match/runs")
def match_runs(staff=Depends(current_staff), dataset: str = "real", limit: int = 5):
    ids = [r["id"] for r in rows(db.execute("SELECT id FROM match_runs WHERE kind='full' AND dataset=? ORDER BY id DESC LIMIT ?", (dataset, limit)))]
    return {"items": [matching.run_status(db, i) for i in ids], "ai_batch": matching.AI_BATCH, "ai_workers": matching.AI_WORKERS, "apartments_per_group": matching.APARTMENTS_PER_GROUP}


@app.get("/api/match/runs/{run_id}")
def match_run_status(run_id: int, staff=Depends(current_staff)):
    try:
        return matching.run_status(db, run_id)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/outcome-ledger")
def outcome_ledger_view(staff=Depends(current_staff), dataset: str = "real", people: bool = True):
    """T3(d) C5: staff-only, read-only - every person's current outcome, reason and next step (a ready proposal is never a
    placement). Changes nothing. `consistency` says whether the figures were read at one moment (checked on SQLite; NOT VERIFIED on
    PostgreSQL); the response is not cached and is not a live view."""
    if dataset not in ("real", "test"):
        raise HTTPException(400, "dataset must be real or test")
    return outcome_ledger.get_ledger(db, dataset) if people else outcome_ledger.summary(db, dataset)


@app.get("/api/scheduler")
def scheduler_status(staff=Depends(current_staff)):
    """T8: last and next hourly check, the last scheduled run, and the check log (ran / deferred / skipped)."""
    return scheduler.status(db)


@app.post("/api/scheduler/check")
async def scheduler_check_now(req: Request, staff=Depends(current_staff)):
    """T8: run the hourly check now (same logic: runs only if a new applicant arrived since the last completed run)."""
    body = await sec.json_object(req, empty_ok=True)
    ds = [body["dataset"]] if body.get("dataset") else scheduler.DATASETS
    return {"checks": [scheduler.check(db, d, trigger="manual", use_ai=body.get("use_ai", True)) for d in ds], "status": scheduler.status(db)}


@app.get("/api/ai-queue")
def ai_queue_status(staff=Depends(current_staff)):
    """T6: the AI assessment queue (pending / running / ready / failed, calls, last error, failed groups)."""
    return ai_queue.status(db)


@app.post("/api/ai-queue/retry")
async def ai_queue_retry(req: Request, staff=Depends(current_staff)):
    """T6: staff retry failed AI assessments (all, or the given group ids)."""
    body = await sec.json_object(req, empty_ok=True)
    n = ai_queue.retry_failed(db, staff["display_name"], body.get("group_ids"))
    return {"requeued": n, "status": ai_queue.status(db)}


@app.post("/api/ai-queue/reassess")
async def ai_queue_reassess(req: Request, staff=Depends(current_staff)):
    """Staff 'Refresh every AI assessment' for one data set (deliberate; T6 never re-runs an unchanged group on its own)."""
    body = await sec.json_object(req, empty_ok=True)
    n = ai_queue.reassess_all(db, body.get("dataset", "real"), staff["display_name"])
    return {"requeued": n, "status": ai_queue.status(db)}


@app.post("/api/ai-queue/process")
def ai_queue_process(staff=Depends(current_staff), max_batches: Optional[int] = None):
    """Process the queue now in this request (used by tests and by a deployment without the worker thread); normally the worker does it."""
    return {"result": ai_queue.process_pending(db, max_batches=max_batches), "status": ai_queue.status(db)}


@app.get("/api/groups")
def list_groups(staff=Depends(current_staff), status: Optional[str] = None, dataset: Optional[str] = None, alternatives: bool = False, enquiry_id: Optional[int] = None,
                ai_state: Optional[str] = None):
    """Group-first list. By default only each enquiry's best (primary) proposed group is shown; alternatives=true adds the rest.
    T6: `ai_state=ready` (AI-assessed, ready for reservations), `pending` (queued or running) or `failed`."""
    where, args = ["g.status<>'Dissolved'"], []
    if status:
        where.append("g.status=?"); args.append(status)
    if ai_state == "ready":
        where.append("g.ai_status='ok'")   # a non-ok review can never be ready; the real test is can_circulate() on each row below
    elif ai_state == "pending":
        where.append("(g.ai_status IS NULL OR g.ai_status IN ('pending','running','skipped'))")
    elif ai_state == "failed":
        where.append("(g.ai_status='failed' OR (g.ai_status='held' AND g.ranking_basis=?))"); args.append(ai_queue.NOTES_HELD_BASIS)   # C4: the exhausted-notes hold is a processing failure staff can retry
    if not alternatives:
        where.append("(g.is_primary=1 OR g.status<>'Proposed')")
    if enquiry_id:
        where.append("EXISTS (SELECT 1 FROM group_members gm WHERE gm.group_id=g.id AND gm.enquiry_id=?)"); args.append(enquiry_id)
    gs = rows(db.execute(f"""SELECT g.*, a.address, a.type_code, a.suburb FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE {' AND '.join(where)}
                             ORDER BY CASE g.status WHEN 'Leased' THEN 0 WHEN 'Awaiting replacement' THEN 1 WHEN 'Group confirmed' THEN 2 WHEN 'Introduced' THEN 3 ELSE 4 END, g.is_primary DESC, g.ai_rating DESC, g.rule_score DESC LIMIT 600""", args))
    occ = _occupancy_map()
    out = []
    for g in gs:
        o = _group_out(g, occ)   # carries ready / circulation from the central predicate
        if ai_state == "ready" and not o["ready"]:
            continue
        out.append(o)
    if dataset:
        out = [g for g in out if g["is_test"] == (dataset == "test")]
    return {"items": out}


@app.get("/api/groups/{gid}")
def group_detail(gid: int, staff=Depends(current_staff)):
    g = row(db.execute("SELECT g.*, a.address, a.type_code, a.suburb, a.bills_included FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (gid,)))
    if not g:
        raise HTTPException(404, "Not found")
    out = _group_out(g)
    # alternative groupings that share a member with this group (shown to staff as alternatives only)
    ids = [m["enquiry_id"] for m in out["members"]]
    if ids:
        alts = rows(db.execute(f"""SELECT DISTINCT g2.id, g2.ai_rating, g2.rule_score, g2.is_primary, g2.status, a.address FROM groups g2 JOIN group_members gm ON gm.group_id=g2.id
                                   LEFT JOIN apartments a ON a.id=g2.apartment_id WHERE g2.id<>? AND g2.status='Proposed' AND gm.enquiry_id IN ({','.join('?' * len(ids))}) ORDER BY g2.ai_rating DESC, g2.rule_score DESC LIMIT 12""", [gid, *ids]))
        for a in alts:
            a["members"] = [f"{m['first_name']} {m['last_name']}" for m in rows(db.execute("SELECT e.first_name, e.last_name FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=? ORDER BY gm.room", (a["id"],)))]
        out["alternatives"] = alts
    else:
        out["alternatives"] = []
    # GP14: listed apartments a current member has declined (shown as "declined by <name> on <date>"; never offerable while
    # active) are already on an open group from _group_out; a closed group's are read here, once, for its own page only
    if "declined_apartments" not in out:
        out["declined_apartments"] = {str(k): v for k, v in apartment_rejections.declined_in_group(db, gid).items()}
    _attach_review_state(out, g)
    return out


def _circulation(g: dict) -> dict:
    """The ONE readiness + ownership predicate (ai_queue.can_circulate: valid approved review under the current rules and model,
    staff brief ok, evidence unchanged or within the GP8 carve-out, hard rules still passing, primary group, nobody owned by
    another circulating group). The UI never re-derives it and nothing here widens it."""
    base = {k: g.get(k) for k in g if not isinstance(g.get(k), (list, dict))}
    return ai_queue.can_circulate(db, base)


TEMPLATE_AI_UNAVAILABLE = "Template (AI unavailable)"   # r7 / T5: static basis label; the error category goes to the audit only
TEMPLATE_BASIS_INCOMPLETE = "Template (brief provenance incomplete)"   # r7: the staff brief predates note provenance; never an AI draft
INTRO_CAPTURE_TEXT = {   # ai_queue.IntroNotCapturable.code -> fixed staff sentence (codes are a closed set; nothing else is passed through)
    "no_offer": "this group has no current apartment offer to introduce. Check its apartment list first.",
    "no_members": "this group has no members to introduce.",
    "brief_not_ok": "the staff brief for this group is not complete yet; the introduction waits for it.",
    "legacy_basis": "the staff brief for this group predates note provenance; only a labelled template can be produced.",
}


def _intro_capture_refusal(e) -> HTTPException:
    if e.code == "group_gone":
        return HTTPException(404, "Not found")
    if e.code == "not_circulable":
        return HTTPException(409, "Not ready to introduce: " + "; ".join(e.details or ["the group is not ready"]))
    return HTTPException(409, "Not ready to introduce: " + INTRO_CAPTURE_TEXT.get(e.code, "the introduction cannot be drafted for this group right now."))


def _recheck_circulation(gid: int, prefix: str, conn=None) -> dict:
    """r7: inside an application guard, re-read the group row and apply the central readiness predicate again. The CAS predicates
    on the UPDATE cover the columns they name; this covers everything else can_circulate looks at (review validity under the
    current rules/model, ownership by another live group, primary flag). 404 when the group is gone, 409 when not ready."""
    c = conn or db
    fresh = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    if not fresh:
        raise HTTPException(404, "Not found")
    circ = ai_queue.can_circulate(c, {k: fresh.get(k) for k in fresh if not isinstance(fresh.get(k), (list, dict))})
    if not circ.get("ok"):
        raise HTTPException(409, f"{prefix}: the group is no longer ready to circulate (" + "; ".join(circ.get("reasons") or ["not ready"]) + "). Reload the page and check it again.")
    return fresh


def _attach_review_state(out: dict, g: dict) -> None:
    """T6 staff view: the current readiness check, the latest review / brief / introduction with 'current' vs history labels,
    and the GP8(b) note when readiness rests on the leased-apartment carve-out."""
    circ = _circulation(g) if g.get("status") not in ("Leased", "Dissolved") else {"ok": False, "reasons": [f"group is {g.get('status', '').lower()}"]}
    st = ai_queue.intro_state(db, g["id"])
    out["circulation"] = {"ok": circ.get("ok", False), "reasons": circ.get("reasons") or []}
    out["ready_check"] = circ.get("ready_check") or {"ready": False, "reasons": circ.get("reasons") or []}
    out["ready"] = bool(circ.get("ok"))
    out["review"] = staff_view.review_view(db, g["id"], g)
    out["brief"] = staff_view.brief_view(db, g["id"], g)
    out["intro_history"] = staff_view.intro_history(db, g["id"])
    current = bool(st.get("current")) and out["ready"] and bool(g.get("intro_text"))
    out["intro"] = {"text": g.get("intro_text"), "current": current, "basis": st.get("basis"), "drafted_at": st.get("drafted_at"),
                    "draft_id": st.get("draft_id"), "context_hash": st.get("context_hash"),   # r7: the edit names the draft it is based on
                    "offer_current": bool(st.get("offer_current")), "profile_current": bool(st.get("profile_current")), "apartment_id": st.get("apartment_id"),
                    "stale_reason": None if current else (st.get("stale_reason") if not st.get("current") else "; ".join(out["circulation"]["reasons"]) or "group not ready to circulate"),
                    "label": "Current introduction draft" if current else ("Earlier introduction draft (history, not for sending)" if g.get("intro_text") else "No introduction drafted")}
    if out["ready_check"].get("carveout"):
        out["facts_note"] = ("An apartment on this group's list was leased and the group moved to its next apartment. The apartment facts shown on this page are "
                             "current; the AI brief and introduction were written before the change and may describe the earlier apartment, so check them "
                             "before use. The AI refresh is pending.")
    out["intro_missing"], out["intro_recipients"], out["intro_without_email"] = [], [], []
    if circ.get("ok"):
        try:
            people = _intro_people(ai_queue.intro_members(db, g["id"]))
            out["intro_missing"] = [x for p in people for x in (p.get("intro_missing") or [])]
            out["intro_recipients"] = [{"name": p["name"], "email": p["email"]} for p in people if p.get("email")]
            out["intro_without_email"] = [p["name"] for p in people if not p.get("email")]
        except Exception:  # pragma: no cover - never let a display helper break the detail page
            pass


GROUP_STATUSES = ["Proposed", "Introduced", "Group confirmed", "Leased", "Dissolved", matching.AWAITING]  # GP10: Awaiting replacement is set by MMAI only
ENQ_STATUS_FOR_GROUP = {"Proposed": "Proposed in group", "Introduced": "Introduced", "Group confirmed": "Group confirmed", "Leased": "Leased",
                        matching.AWAITING: matching.AWAITING}
GROUP_PATCH_OPS = {"status": "status change", "intro_text": "introduction edit", "apartment_id": "apartment offer", "rerank_ack": "re-rank acknowledgement",
                   "discount_note": "discount note", "offer_ack": "offer acknowledgement"}   # the operations PATCH /api/groups/{gid} accepts, exactly one per request (E)
POOL_RETURN_STATUSES = ("Proposed in group", matching.AWAITING)  # enquiry statuses that go back to New when their group is dissolved


def _claim_apartment(aid: int, gid: int | None, staff_name: str, *, missing: int = 404, conn=None, repeat_ok: bool = False) -> dict:
    """GP8: the ONLY place an apartment becomes Leased. Must be called inside a retention guard: the read, the refusal and the
    conditional write are one guarded step, so two staff claiming the same apartment (two requests, two connections) can never
    both succeed; the second sees rowcount 0 and is refused. Never reassigns: a claimed apartment stays with its claimant until
    it is relisted. `missing` is the HTTP status when the apartment row is gone (404 for a named apartment, 409 for a group's
    stored apartment that is no longer listed). With `repeat_ok`, an apartment ALREADY claimed for exactly this group (or for no
    group, when gid is None) is not refused: the existing claim is returned with repeated=True and nothing is written, so the
    caller can finish the follow-up (re-ranking) that may not have completed the first time. Any other claimant: refused."""
    c = conn if conn is not None else db
    if not dbm_guarded(c):
        raise RuntimeError("apartment claim outside a retention guard")
    a = row(c.execute("SELECT id, address, leased_at, leased_group_id FROM apartments WHERE id=?", (aid,)))
    if not a:
        raise HTTPException(missing, "Apartment not found" if missing == 404 else "This group's apartment is no longer listed; consult the ALM property management record")
    if a.get("leased_at"):
        if repeat_ok and (a.get("leased_group_id") if a.get("leased_group_id") is not None else None) == gid:
            return {"apartment_id": aid, "address": a["address"], "leased_at": str(a["leased_at"]), "leased_group_id": gid, "repeated": True}
        raise HTTPException(409, "This apartment is already marked Leased")
    when = now_iso()
    n = c.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by=?, updated_at=?, lease_generation=lease_generation+1 WHERE id=? AND leased_at IS NULL",
                   (when, gid, staff_name, when, aid)).rowcount
    if n != 1:
        raise HTTPException(409, "This apartment is already marked Leased")
    # stock lifecycle: the follow-up work (which other live groups listed this apartment, and how) is captured in this same durable
    # step, before any option row is touched; the groups are then re-checked one guard step each (_after_claim)
    stock_lifecycle.record_transition(c, aid, "leased", staff_name, exclude_group_id=gid, bump=False)
    return {"apartment_id": aid, "address": a["address"], "leased_at": when, "leased_group_id": gid, "repeated": False}


def _introduction_ready(c, g: dict, intro_token: dict | None) -> None:
    """T6/GP2/r7 readiness for the Introduced action, inside the caller's guard: ready to circulate, a CURRENT introduction draft, and the
    request names THE DRAFT THE STAFF PAGE SHOWED (stable id + its context), which must still be the latest current row. A newer draft (same
    offer re-drafted or edited, or a new apartment after a GP8 switch) refuses the old page - never substituted with today's draft.
    Raises HTTPException(409) with the reason; returns None when ready. Shared by the first introduction and the GP13 re-introduction."""
    gid = g["id"]
    circ = _circulation(g)
    if not circ.get("ok"):
        raise HTTPException(409, "Not ready to introduce: " + "; ".join(circ.get("reasons") or ["the group is not ready"]))
    ist = ai_queue.intro_state(c, gid)
    if not ist.get("current") or not (g.get("intro_text") or "").strip():
        raise HTTPException(409, "There is no current introduction draft for this group (" + (ist.get("stale_reason") or "draft it first") + "). Draft it again before marking the group introduced.")
    tok = intro_token or {}
    try:
        want_id = int(tok.get("draft_id")) if tok.get("draft_id") not in (None, "") else None
    except (TypeError, ValueError):
        want_id = None
    if want_id is None or not tok.get("context_hash"):
        raise HTTPException(409, "Reload the page and check the current introduction draft before marking the group introduced: this request does not say which draft was sent.")
    if ist.get("draft_id") != want_id or ist.get("context_hash") != tok.get("context_hash"):
        raise HTTPException(409, "A newer introduction draft exists for this group since you opened the page. Reload the page, check the current draft is the one you sent, then mark it introduced.")


def _set_group_status(gid: int, st: str, staff_name: str, apartment_id: int | None = None, conn=None, intro_token: dict | None = None) -> dict | None:
    """Status chain for a group; members' enquiry status follows (7.5). Leased (GP4) also claims the apartment (GP8) in the same
    guarded step and, after it, re-ranks every other group; returns the GP8 report when that happens.
    `apartment_id` (POST /api/apartments/{aid}/lease) names the apartment being leased: the group's current one, or another from
    its own ranked list (GP6(b) offer switch, inside the same step). A group whose status is final is never switched.
    Every read that decides the outcome happens INSIDE the guard (the route's earlier read is not authoritative): a purge or
    another staff member may have dissolved, leased or deleted the group in between, so the status write is conditional on the
    status that was read under the guard and the apartment claim is conditional on the apartment being unclaimed."""
    claim = None
    c = conn if conn is not None else db   # `conn` only for tests that drive two real connections; the app always uses `db`
    # T5: the status chain changes purge eligibility (Leased exempts; members' last_contact_at moves), which purge_expired reads
    # under the same guard. One guarded transaction, committed at exit; refusals raise and the guard rolls back. No model or
    # network work inside; the GP8 re-rank after a lease runs AFTER the guard (builder's helper, its own per-group guards).
    with retention_guard(c, label="group status"):
        g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g:
            raise HTTPException(404, "Group not found")
        was = g["status"]
        if was in ("Dissolved", "Leased") and st != was:
            raise HTTPException(409, f"Group {gid} is {was.lower()}; its status is final")
        if st == was:
            if st == "Leased" and apartment_id is not None and apartment_id != g["apartment_id"]:
                raise HTTPException(409, "This group has already leased a different apartment; a leased group is never switched to another one")
            if st == "Leased" and g["apartment_id"] is not None:
                # unchanged status, same apartment: never a second claim, but the follow-up (re-ranking the other groups) is run
                # again if the apartment is indeed claimed for THIS group - it may not have finished the first time (see _after_claim).
                # Any other binding (unclaimed, or claimed for someone else) is left to the caller: nothing is written here.
                a = row(c.execute("SELECT address, leased_at, leased_group_id FROM apartments WHERE id=?", (g["apartment_id"],)))
                if a and a.get("leased_at") and a.get("leased_group_id") == gid:
                    claim = {"apartment_id": g["apartment_id"], "address": a["address"], "leased_at": str(a["leased_at"]), "leased_group_id": gid, "repeated": True}
            if st == "Introduced" and group_responses.needs_reintroduction(c, gid):
                # GP13: same status, but the membership changed since the last introduction and the revised group has not been introduced
                # yet. Marking it introduced again anchors the 24 h clock of the CURRENT version - after the same readiness checks as the
                # first time (ready to circulate, the current draft the page showed). Nothing else changes; no links are minted.
                _introduction_ready(c, g, intro_token)
                group_responses.anchor_introduction(c, gid, staff_name)
                audit(c, staff_name, "group_reintroduced", "group", gid, "revised membership introduced; everyone answers again")
                return None
            if claim is None:
                return None   # unchanged: nothing written, no second claim
        else:
            if st == matching.AWAITING:
                raise HTTPException(409, "Only MateBot AI sets 'Awaiting replacement': it happens when a group loses a member and no replacement passes every rule")
            if was == matching.AWAITING and st == "Proposed":
                raise HTTPException(409, "A group awaiting a replacement cannot go back to Proposed by hand. Accept a replacement, lease with the empty space, or dissolve it.")
            if st == "Introduced":
                # T6/GP2: only a group that is ready to circulate, with a CURRENT introduction draft, can be marked introduced; the status
                # change is one conditional update so a change underneath (facts, review, brief, ownership) makes it fail, never half-apply.
                if was != "Proposed":
                    raise HTTPException(409, f"Group {gid} is {was.lower()}; only a proposed group can be marked introduced")
                if g.get("apartment_id") is None:
                    raise HTTPException(409, "This group has no current apartment; it cannot be introduced until it has a valid offer")
                _introduction_ready(c, g, intro_token)
                n = c.execute("""UPDATE groups SET status='Introduced', updated_at=? WHERE id=? AND status='Proposed' AND is_primary=1 AND evidence_hash=?
                                 AND review_verdict='approve' AND brief_status='ok' AND ai_status='ok' AND intro_text IS NOT NULL AND apartment_id=?""",
                              (now_iso(), gid, g.get("evidence_hash"), g["apartment_id"])).rowcount   # r7: the selected apartment is part of the transition
                if n != 1:
                    raise HTTPException(409, "The group changed while you were working (facts, review, brief or membership). Reload the page and check it again.")
                # GP13: the staff Introduced action is the 24 h anchor of the CURRENT membership version (people as they are now, inside this
                # same guard). It mints no links: staff hand each person their own link afterwards from the group page.
                group_responses.anchor_introduction(c, gid, staff_name)
            elif st == "Group confirmed":
                # GP13: the group is confirmed only when EVERY person in the current membership version has said yes through their own link
                # (acceptance of the housemates, not of the proposed rent / share). Derived now, under this guard, never from an older version.
                group_responses.on_membership_changed(c, gid, "confirm", staff_name)
                block = group_responses.confirmation_block(c, gid)
                if block:
                    raise HTTPException(409, block)
                n = c.execute("UPDATE groups SET status=?, updated_at=? WHERE id=? AND status=?", (st, now_iso(), gid, was)).rowcount
                if n != 1:
                    raise HTTPException(409, "The group changed while you were working. Reload the page and check it again.")
            else:
                if st == "Leased":
                    aid = apartment_id if apartment_id is not None else g["apartment_id"]
                    if aid is None:
                        raise HTTPException(409, "This group has no apartment to lease; consult the ALM property management record")
                    if stock_lifecycle.group_has_pending_work(c, gid):
                        raise HTTPException(409, "This group is still being re-checked after a stock change; reload the page and check its apartment before leasing.")
                    if aid != g["apartment_id"]:   # GP6(b): offered from the group's own list only; refused otherwise; same guarded step
                        try:
                            matching._select_listed_apartment_for_lease(c, gid, aid, staff_name)
                        except apartment_rejections.OfferRefused as e:
                            raise HTTPException(409, str(e))   # GP14 (inc3): declined / lifted-pending-validation
                        except ValueError as e:
                            raise HTTPException(422, str(e))
                    claim = _claim_apartment(aid, gid, staff_name, missing=404 if apartment_id is not None else 409, conn=c)
                n = c.execute("UPDATE groups SET status=?, updated_at=? WHERE id=? AND status=?", (st, now_iso(), gid, was)).rowcount
                if n != 1:
                    raise HTTPException(409, "The group changed while you were working. Reload the page and check it again.")
            ids = [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))]
            if st in ENQ_STATUS_FOR_GROUP:
                for eid in ids:
                    c.execute("UPDATE enquiries SET status=?, last_contact_at=? WHERE id=?", (ENQ_STATUS_FOR_GROUP[st], now_iso(), eid))
            if st == "Dissolved":
                for eid in ids:
                    c.execute(f"UPDATE enquiries SET status='New' WHERE id=? AND status IN ({','.join('?' * len(POOL_RETURN_STATUSES))})", (eid, *POOL_RETURN_STATUSES))
            audit(c, staff_name, "group_status", "group", gid, st + (" (was awaiting replacement)" if was == matching.AWAITING else "")
                  + (f"; claimed apartment {claim['apartment_id']} ({claim['address']}) at {claim['leased_at']}" if claim else ""))
    return _after_claim(claim, staff_name, gid, conn=c) if claim else None


def _when_melbourne(iso):
    """Staff-facing time for a lease message: '8 Nov 2026, 9:38 pm (Melbourne)'. Falls back to the stored text when the value
    cannot be parsed or the time-zone data is missing - the message never guesses."""
    try:
        from server import lease_dates
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(lease_dates.business_tz()).strftime("%-d %b %Y, %-I:%M %p").replace("AM", "am").replace("PM", "pm") + " (Melbourne)"
    except Exception:
        return str(iso)


def _acknowledge_offer(gid: int, staff_name: str) -> None:
    with retention_guard(db, label="offer acknowledgement"):
        g = row(db.execute("SELECT g.*, a.available_from, a.leased_at FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (gid,)))
        if not g:
            raise HTTPException(404, "Not found")
        if not g.get("offer_changed_since_intro_at"):
            return
        if g.get("apartment_id") is None or not g.get("available_from") or g.get("leased_at"):
            raise HTTPException(409, "This group has no valid current apartment to acknowledge; offer it the next option or run full matching first.")
        if stock_lifecycle.group_has_pending_work(db, gid):
            raise HTTPException(409, "This group is still being re-checked after a stock change. Try again in a moment.")
        if not row(db.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, g["apartment_id"]))):
            raise HTTPException(409, "The group's current apartment is not on its own list; offer one from the list first.")
        ist = ai_queue.intro_state(db, gid)
        if not ist.get("current"):
            raise HTTPException(409, "There is no current introduction draft for this group (" + (ist.get("stale_reason") or "draft it first") + "). Draft it before acknowledging the offer.")
        db.execute("UPDATE groups SET offer_changed_since_intro_at=NULL, updated_at=? WHERE id=?", (now_iso(), gid))
        audit(db, staff_name, "group_offer_acknowledged", "group", gid, f"current introduction and offer (apartment {g['apartment_id']}) acknowledged")


def _after_claim(claim: dict, staff_name: str, gid: int | None, conn=None) -> dict:
    """After the guarded claim has COMMITTED: GP8 re-rank of every other group (builder's helper verifies that the claim it was
    handed is the one on the apartment, then re-ranks each group in its own guarded step, then writes the apartment_leased audit).
    Runs OUTSIDE the claim's guard, so its failure is a different thing from a refused claim: the lease IS recorded. The answer
    says so honestly (never "nothing was changed"), and the recovery path is to mark the same apartment leased again for the same
    group: the claim is not repeated (repeat_ok / unchanged status), only this follow-up runs again - the re-rank skips groups
    that no longer list the apartment, so finishing it twice is harmless. Nothing here ever unclaims a committed lease."""
    c = conn if conn is not None else db
    recorded = f"The lease is recorded: {claim.get('address') or 'apartment ' + str(claim['apartment_id'])} marked Leased at {_when_melbourne(claim['leased_at'])}" \
               + (f" for group {gid}" if gid else "") + "."
    retry = " Mark the apartment leased again (same group) to finish re-ranking the other groups; the lease itself will not be repeated."
    try:
        rep = matching.mark_apartment_leased(c, claim["apartment_id"], staff_name, gid, claimed_at=claim["leased_at"])
    except ValueError as e:
        raise HTTPException(409, f"{recorded} But the apartment changed before the other groups could be re-ranked ({e}). Reload the page and check the apartment.")
    except RetentionLockTimeout:
        raise HTTPException(503, f"{recorded} The follow-up re-ranking of the other groups did not finish: the system is busy finishing another update.{retry}")
    except ConnectionLostMidOperation as e:
        raise HTTPException(503, f"{recorded} The follow-up re-ranking of the other groups {'may not have finished' if getattr(e, 'outcome_unknown', False) else 'did not finish'}: the database connection was lost.{retry}")
    except HTTPException:
        raise
    except Exception as e:   # pragma: no cover - unexpected follow-up failure must still tell the truth about the recorded lease
        raise HTTPException(500, f"{recorded} The follow-up re-ranking of the other groups failed ({type(e).__name__}).{retry}")
    rep["repeated"] = bool(claim.get("repeated"))
    rr = rep.get("rerank") or {}
    pending = list(rr.get("pending_group_ids") or [])
    moved = len(rep.get("affected") or [])
    if pending:   # the builder's helper isolates each group's re-rank step: the lease is durable, these groups are still to do
        rep["message"] = (f"{recorded} {moved} other group(s) re-ranked; re-ranking is still pending for {len(pending)} group(s)."
                          + retry)
    elif rep["repeated"]:
        rep["message"] = (f"This apartment was already marked Leased at {_when_melbourne(claim['leased_at'])}" + (f" for group {gid}" if gid else "")
                          + f"; the lease was not repeated. The re-ranking of the other groups was run again: {moved} group(s) moved or trimmed.")
    else:
        rep["message"] = f"Leased. {moved} other group(s) moved to their next best apartment and flagged."
    return rep


@app.patch("/api/groups/{gid}")
async def update_group(gid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    # E (8 Nov 2026): ONE operation per request, decided before ANY write. A status change is its own guarded step (a lease
    # claims the apartment); the introduction edit, the offer switch and the notes below work from the group as read before it
    # and commit on their own, so a mixed body would let a later part act on stale state (or switch the offer of a group just
    # leased), and the forbidden is_primary used to be refused only after the other parts had committed. The staff page sends
    # exactly one operation per request (intro_drafted_at is the edit's version stamp, not an operation).
    if "is_primary" in body:   # GP7(b): staff do not promote or rearrange groups by hand - refused first, nothing else in the body runs
        raise HTTPException(403, "Staff cannot promote or rearrange groups by hand. Reject one or more members and the AI replaces them.")
    ops = [k for k in GROUP_PATCH_OPS if k in body]
    if len(ops) != 1:
        raise HTTPException(422, "One change per request: " + ("nothing to change was sent." if not ops else
                            "send the " + " and the ".join(GROUP_PATCH_OPS[k] for k in ops) + " as separate requests."))
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    if not g:
        raise HTTPException(404, "Not found")
    gp8 = None
    if "status" in body:
        st = body["status"]
        if st not in GROUP_STATUSES:
            raise HTTPException(422, "Invalid status")
        # the Introduced action carries the draft row the page showed (metadata of the one status operation, like the edit's)
        gp8 = _set_group_status(gid, st, staff["display_name"], intro_token={"draft_id": body.get("draft_id"), "context_hash": body.get("context_hash")} if st == "Introduced" else None)
    if "intro_text" in body:
        # T6 + r7: staff edit the CURRENT draft only (a stale or template draft must be drafted again); the edit is kept as history.
        # The client names the draft row it showed (draft_id = stable row id; context_hash = the context that draft was written
        # for) - second-precision drafted_at is display only. Inside ONE guarded step: readiness on the fresh row, the named row
        # is still the latest and unchanged, the draft is still current for the exact offer / people, the conditional write, and
        # the record step which compares THAT row's stored context with a fresh projection. Stale text is never re-stamped.
        text = str(body["intro_text"] or "").strip()
        if not text:
            raise HTTPException(422, "The introduction text cannot be empty")
        if body.get("draft_id") in (None, "") or not body.get("context_hash"):
            raise HTTPException(422, "Reload the page before editing: this edit does not say which draft it is based on.")
        try:
            draft_id = int(body["draft_id"])
        except (TypeError, ValueError):
            raise HTTPException(422, "Reload the page before editing: this edit does not say which draft it is based on.")
        ist = ai_queue.intro_state(db, gid)
        circ = _circulation(g)
        if not ist.get("current") or not circ.get("ok"):
            raise HTTPException(409, "This introduction draft is not current (" + "; ".join(([ist.get("stale_reason")] if not ist.get("current") else []) + (circ.get("reasons") or [])) + "). Draft it again with the AI before editing.")
        try:
            with retention_guard(db, label="intro edit"):
                fresh = _recheck_circulation(gid, "The edit was not kept")   # r7: readiness re-checked on the fresh row inside the step
                last = row(db.execute("SELECT id, body_json, refs_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (gid,)))
                stored = (pj(last.get("body_json"), {}) or {}) if last else {}
                if not last or last["id"] != draft_id or not stored.get("context_hash") or stored.get("context_hash") != body.get("context_hash"):
                    raise HTTPException(409, "Someone changed this draft since you opened it. Reload the page to see the latest text before editing.")
                # the people the draft describes: the immutable ref -> identity map the drafter stored with THAT row, carried onto the
                # edit row unchanged (never re-derived from today's members) and re-checked by the record step (deleted person or
                # changed membership -> refused). An AI row always has it; without it the edit is refused rather than left unattributed.
                refs = pj(last.get("refs_json"), None) or {}
                if not refs:
                    raise HTTPException(409, "This introduction draft is not current (drafted before its people were recorded with it). Draft it again with the AI before editing.")
                described = [{"ref": ref, "enquiry_id": who.get("enquiry_id"), "party_member_id": who.get("party_member_id")} for ref, who in refs.items()]
                ist = ai_queue.intro_state(db, gid)   # re-evaluated inside the step: exact offer, people, facts, basis
                if not ist.get("current"):
                    raise HTTPException(409, "This introduction draft is not current (" + (ist.get("stale_reason") or "draft it again") + "). Draft it again with the AI before editing.")
                n = db.execute("UPDATE groups SET intro_text=?, updated_at=? WHERE id=? AND evidence_hash=? AND status=? AND member_key=? AND apartment_id=?",
                               (text, now_iso(), gid, fresh.get("evidence_hash"), fresh["status"], fresh.get("member_key"), stored.get("apartment_id"))).rowcount
                if n != 1:
                    raise HTTPException(409, "The group changed while you were editing. Reload the page and check it again.")
                try:   # the capture is the STORED context of the row the staff member saw - never a fresh capture around their text
                    ai_queue.record_intro_draft(db, gid, text, "staff", staff["display_name"], members=described,
                                                capture={k: stored.get(k) for k in ("apartment_id", "profile_hash", "offer_fingerprint", "basis_scope", "context_hash")} | {"basis": stored.get("basis_items")})
                except ValueError as e:
                    raise HTTPException(409, f"The edit was not kept: {e}. Reload the page and check the draft again.")
                audit(db, staff["display_name"], "group_intro_edit", "group", gid)
        except (RetentionLockTimeout, ConnectionLostMidOperation):
            raise HTTPException(503, "The system is busy finishing another update. Reload the page and try the edit again.")
    if "apartment_id" in body:  # GP6(b): offer another apartment from the group's own ranked list (never a hand-picked one)
        try:
            matching.offer_apartment(db, gid, int(body["apartment_id"]), staff["display_name"])
        except RetentionLockTimeout:
            raise HTTPException(503, "The system is busy finishing another update. Reload the page and try the apartment offer again.")
        except apartment_rejections.OfferRefused as e:
            raise HTTPException(409, str(e))   # GP14 (inc3): declined / lifted-pending-validation
        except ValueError as e:
            raise HTTPException(422, str(e))
    if "rerank_ack" in body:
        db.execute("UPDATE groups SET rerank_note=NULL, flag_kind=NULL, updated_at=? WHERE id=?", (now_iso(), gid))
    if "offer_ack" in body:
        # Stock lifecycle: the explicit, audited staff acknowledgement that the CURRENT introduction and the CURRENT offer have been
        # handled with the group. Only a group that has a valid listed current offer and a current introduction draft can be
        # acknowledged; nothing else changes (membership version, approvals and the 24 h clock are untouched - GP13 is about people).
        _acknowledge_offer(gid, staff["display_name"])
    if "discount_note" in body:  # GP10(b): staff discretion, recorded as free text only; MMAI never changes a price
        db.execute("UPDATE groups SET discount_note=?, updated_at=? WHERE id=?", ((body["discount_note"] or "").strip() or None, now_iso(), gid))
        audit(db, staff["display_name"], "group_discount_note", "group", gid, (body["discount_note"] or "")[:200])
    db.commit()
    out = group_detail(gid, staff)
    if gp8:
        out["gp8"] = gp8
    return out


@app.post("/api/groups/{gid}/recheck-options")
def group_recheck_options(gid: int, staff=Depends(current_staff)):
    from . import option_recheck
    try:
        return option_recheck.recheck(db, gid, staff["display_name"])
    except option_recheck.GroupNotFound:
        raise HTTPException(404, "Group not found")
    except option_recheck.RecheckRefused as e:
        raise HTTPException(409, str(e))
    except stock_lifecycle.OptionsNotJudgeable:
        raise HTTPException(409, "The group's options could not be checked safely. Nothing was changed; check the member and apartment records before trying again.")
    except stock_lifecycle.AlertOutboxUnavailable as e:
        raise HTTPException(503, str(e))


@app.post("/api/groups/{gid}/reject")
async def group_reject_members(gid: int, req: Request, staff=Depends(current_staff)):
    """GP7(b) + GP3: staff reject one or more suggested members; MMAI records the rejection, the AI ranks the rule-passing
    refills and the best one becomes the new proposed group (the old one is dissolved). Staff never pick members by hand."""
    body = await sec.json_object(req)
    reject = [int(x) for x in body.get("reject_enquiry_ids", body.get("remove_enquiry_ids", []))]
    if not reject:
        raise HTTPException(422, "Tick at least one member to reject")
    try:
        rep = matching.reject_members(db, gid, reject, staff["display_name"], use_ai=body.get("use_ai", True),
                                      reason_kind=body.get("reason_kind") or "housemates", friends_choice=body.get("friends_choice"))  # GP11(k)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if rep["new_group_id"]:
        rep["group"] = group_detail(rep["new_group_id"], staff)
    return rep


@app.post("/api/groups/{gid}/fill-replacement")
async def group_fill_replacement(gid: int, req: Request, staff=Depends(current_staff)):
    """GP10: staff accept a replacement for a group awaiting one. Never automatic: the refill is recomputed, AI-ranked and a new
    Proposed group continues the old one."""
    body = await sec.json_object(req, empty_ok=True)
    try:
        rep = matching.fill_replacement(db, gid, staff["display_name"], use_ai=body.get("use_ai", True), added_enquiry_ids=body.get("added_enquiry_ids"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    rep["group"] = group_detail(rep["new_group_id"], staff)
    return rep


@app.post("/api/groups/{gid}/decline-replacement")
async def group_decline_replacement(gid: int, req: Request, staff=Depends(current_staff)):
    """GP10 / GP9(a): staff decline a found replacement; the declined people are never offered to these members again."""
    body = await sec.json_object(req)
    ids = [int(x) for x in body.get("added_enquiry_ids", [])]
    if not ids:
        raise HTTPException(422, "No replacement to decline")
    try:
        return matching.decline_replacement(db, gid, staff["display_name"], ids, body.get("reason"))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/mate-requests")
def list_mate_requests(staff=Depends(current_staff), dataset: Optional[str] = None):
    """GP11(f): friends waiting for staff approval to join a group that has already been shared with its members."""
    return {"items": matching.pending_mate_requests(db, dataset=dataset)}


@app.post("/api/mate-requests/{rid}/decide")
async def decide_mate_request(rid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        return matching.decide_mate_request(db, rid, staff["display_name"], approve=bool(body.get("approve")), reason=body.get("reason"),
                                            choice=body.get("choice") or "leave")  # GP11(l)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/enquiries/{eid}/mate-link")
def staff_mate_link(eid: int, staff=Depends(current_staff)):
    """Staff can hand an enquirer their 'Bring a mate' link (for example after a phone enquiry)."""
    e = row(db.execute("SELECT id, is_test, linked_to_enquiry_id FROM enquiries WHERE id=?", (eid,)))
    if not e:
        raise HTTPException(404, "Not found")
    # GP11(i): a friend who joined through a link passes on the same party's link, so everyone stays in one party
    return {"token": _issue_mate_link(e["linked_to_enquiry_id"] or eid, bool(e["is_test"]))}


@app.post("/api/groups/{gid}/dissolve")
async def group_dissolve(gid: int, req: Request, staff=Depends(current_staff)):
    """GP10: staff 'Dissolve and return to pool'. The group ends; every member goes back to the pool for the next run."""
    body = await sec.json_object(req, empty_ok=True)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    if not g:
        raise HTTPException(404, "Not found")
    was = g["status"]
    ids = [m["enquiry_id"] for m in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))]
    _set_group_status(gid, "Dissolved", staff["display_name"])
    for eid in ids:  # "return to pool" means exactly that: whatever stage the group had reached, its members are New again
        db.execute("UPDATE enquiries SET status='New' WHERE id=? AND status IN ('Proposed in group','Introduced','Group confirmed',?)", (eid, matching.AWAITING))
    note = f"Dissolved on {now_iso()[:10]} by staff" + (" (was awaiting a replacement)" if was == matching.AWAITING else "") + \
           (f": {body['note'].strip()}" if body.get("note") else ".") + " The members will be included in the next matching run."
    db.execute("UPDATE groups SET rerank_note=?, reranked_at=?, replacement_json=NULL, flag_kind='dissolved' WHERE id=?", (note, now_iso(), gid))
    db.commit()
    audit(db, staff["display_name"], "dissolve_return_to_pool", "group", gid, body.get("note") or "")
    return {"group_id": gid, "message": "Group dissolved; the members will be included in the next matching run.", "group": group_detail(gid, staff)}


@app.post("/api/groups/{gid}/replacements")
async def group_replacements(gid: int, req: Request, staff=Depends(current_staff)):
    """Superseded by /reject (GP7): staff no longer choose replacements. Kept so older clients get a clear message."""
    raise HTTPException(410, "Staff do not pick replacements. Use POST /api/groups/{id}/reject: the AI replaces the rejected members.")


@app.post("/api/groups/{gid}/introduction")
async def group_introduction(gid: int, req: Request, staff=Depends(current_staff)):
    """GP2 + T4(c) + T6: AI-drafted introduction for staff to copy into their own email. Allowed only for a group that is ready to
    circulate (central predicate). One block per real person (lead and each party member) built from that person's own recorded
    facts; recipients are the people whose own email ALM may share (consent 6.3). The draft is recorded as history; a template
    fallback is labelled and never counts as the AI draft. Nothing is sent."""
    # r7: ONE guarded read gives the exact apartment / offer / people the model will see (and the fingerprint over those very
    # objects), the current basis notes and central readiness; nothing is written in it
    try:
        cap, template_reason = ai_queue.intro_capture(db, gid), None
    except ai_queue.IntroNotCapturable as e:
        if e.code != "legacy_basis":
            raise _intro_capture_refusal(e)
        try:   # the brief predates note provenance: a labelled template is still possible, an attributable AI draft is not
            cap, template_reason = ai_queue.intro_capture(db, gid, for_ai=False), TEMPLATE_BASIS_INCOMPLETE
        except ai_queue.IntroNotCapturable as e2:
            raise _intro_capture_refusal(e2)
    except (RetentionLockTimeout, ConnectionLostMidOperation):
        raise HTTPException(503, "The system is busy finishing another update. Reload the page and draft the introduction again.")
    apt, ginfo, members = cap["apt"], cap["ginfo"], cap["members"]
    basis = list(cap.get("basis") or [])   # the structured basis items as captured (section, text, fact ids, subjects, coverage): the drafter uses the provenance, not just the sentence
    basis_label, audit_detail = "AI", "AI"
    if template_reason:
        text, basis_label, audit_detail = ai.fallback_introduction(apt, ginfo, members), template_reason, template_reason
    else:
        try:
            with ai.intro_validation_context(cap.get("habit_answers")):   # succ4: private degree-validation context from the capture; not a prompt input
                text = ai.draft_introduction(apt, ginfo, members, basis)   # OUTSIDE any guard
        except ai.AIUnavailable as e:
            text = ai.fallback_introduction(apt, ginfo, members)
            # r7 / T5: the label is static and the audit carries the closed error category only - provider prose never reaches the
            # response, the draft history (basis) or the audit trail
            basis_label = TEMPLATE_AI_UNAVAILABLE
            audit_detail = f"{basis_label}; {ai_queue.failure_category(e)}"   # Modified A: category from the trusted exception kind, never from its words
    # a late result must never land on a group that changed while the AI was writing: readiness is re-checked on the fresh row,
    # the conditional update binds everything the capture saw (evidence, status, brief, membership, apartment) and
    # record_intro_draft compares the exact offer / people fingerprints with a fresh projection. One guarded step: any refusal
    # leaves it with an exception and nothing is kept (no partial intro_text, no history row).
    try:
        with retention_guard(db, label="intro draft"):   # the model call above is OUTSIDE the guard; only the checks and writes are inside
            _recheck_circulation(gid, "The introduction was not kept")
            n = db.execute("UPDATE groups SET intro_text=?, updated_at=? WHERE id=? AND evidence_hash=? AND status=? AND brief_status='ok' AND member_key=? AND apartment_id=?",
                           (text, now_iso(), gid, cap.get("evidence_hash"), cap["status"], cap.get("member_key"), cap["apartment_id"])).rowcount
            if n != 1:
                raise HTTPException(409, "The group changed while the introduction was being drafted. Reload the page and check it again.")
            try:
                ai_queue.record_intro_draft(db, gid, text, "AI" if basis_label == "AI" else basis_label, staff["display_name"], capture=cap)
            except ValueError as e:
                raise HTTPException(409, f"The introduction was not kept: {e}. Reload the page and draft it again.")
            audit(db, staff["display_name"], "group_intro_draft", "group", gid, audit_detail)
    except (RetentionLockTimeout, ConnectionLostMidOperation):
        raise HTTPException(503, "The system is busy finishing another update. Reload the page and draft the introduction again.")
    people = _intro_people(members)
    recipients = [{"name": p["name"], "email": p["email"]} for p in people if p.get("email")]
    without = [p["name"] for p in people if not p.get("email")]
    st = ai_queue.intro_state(db, gid)
    return {"intro_text": text, "basis": basis_label, "current": basis_label == "AI" and bool(st.get("current")), "intro_state": st,
            "recipients": recipients, "without_email": without, "intro_missing": [x for p in people for x in (p.get("intro_missing") or [])]}


def _intro_people(members: list[dict]) -> list[dict]:
    """Every real person in the introduction payload as a flat list (lead and each party member), whichever shape the identity
    payload uses: flat entries (ref/is_lead) or a lead with nested party_members. Each person keeps only their own fields."""
    people = []
    for m in members or []:
        name = m.get("name") or m.get("first_name")
        people.append({"name": name, "email": m.get("email"), "is_lead": m.get("is_lead", m.get("party_member_id") is None), "ref": m.get("ref"),
                       "intro_missing": list(m.get("intro_missing") or [])})
        for pm in m.get("party_members") or []:
            if isinstance(pm, dict):
                people.append({"name": pm.get("name"), "email": pm.get("email"), "is_lead": False, "ref": pm.get("ref"), "intro_missing": []})
    return people


# ---------------------------------------------------------------------------
# GP13: individual group responses (server/group_responses.py) - staff side and the public link
# ---------------------------------------------------------------------------
def _group_responses_call(fn, *a, **k):
    """Plain refusals: ValueError -> 409 (a sentence staff can act on), KeyError -> 404, lock problems -> 503 (nothing changed)."""
    try:
        return fn(*a, **k)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'"))
    except RetentionLockTimeout:
        raise HTTPException(503, "The system is busy finishing another update. Nothing was changed - try again in a moment.")
    except ConnectionLostMidOperation:
        raise HTTPException(503, "The database connection was lost. Nothing was changed - try again in a moment.")


@app.get("/api/groups/{gid}/responses")
def group_responses_state(gid: int, staff=Depends(current_staff)):
    """Who has answered through their own link, the 24 h clock of the current membership version, why the group cannot be confirmed yet,
    and earlier versions as history (never counted as approval)."""
    return _group_responses_call(group_responses.state, db, gid, staff["display_name"])


@app.post("/api/groups/{gid}/responses/introduce")
async def group_responses_introduce(gid: int, req: Request, staff=Depends(current_staff)):
    """The explicit 'this revised group has been introduced' action: the membership changed since the last introduction, so the current
    version has no 24 h anchor. Same readiness checks as the first introduction (ready to circulate; the current draft the page showed,
    named by draft_id + context_hash). Anchors the clock; mints nothing; changes no status."""
    body = await sec.json_object(req)
    with retention_guard(db, label="group re-introduced"):
        g = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g:
            raise HTTPException(404, "Group not found")
        if g["status"] not in group_responses.RESPOND_STATUSES:
            raise HTTPException(409, "Only a group that has been introduced can be introduced again with a revised membership.")
        if not group_responses.needs_reintroduction(db, gid):
            raise HTTPException(409, "The current membership of this group has already been introduced; nothing to do.")
        _introduction_ready(db, g, {"draft_id": body.get("draft_id"), "context_hash": body.get("context_hash")})
        group_responses.anchor_introduction(db, gid, staff["display_name"])
        audit(db, staff["display_name"], "group_reintroduced", "group", gid, "revised membership introduced; everyone answers again")
    return _group_responses_call(group_responses.state, db, gid, staff["display_name"])


@app.post("/api/groups/{gid}/responses/introduce-remaining")
async def group_responses_introduce_remaining(gid: int, req: Request, staff=Depends(current_staff)):
    """GP10(b), staff-led: 'I have explained the remaining group and the empty space to everyone'. Only for a group awaiting a replacement
    whose current membership version is not yet anchored. The body names the membership and the full disclosed context the page showed
    (membership_hash + disclosure_hash from GET .../responses); both are recomputed and compared under the guard, so a change underneath
    refuses with a plain sentence and writes nothing. Anchors the 24 h clock with the remaining-group anchor kind; mints nothing; changes
    no status; sends nothing. Everyone then answers through their own link about the housemates, not the rent."""
    body = await sec.json_object(req)
    token = {"membership_hash": body.get("membership_hash"), "disclosure_hash": body.get("disclosure_hash")}

    def _anchor():
        with retention_guard(db, label="group remaining introduced"):
            return group_responses.anchor_remaining_group(db, gid, staff["display_name"], token)
    _group_responses_call(_anchor)
    return _group_responses_call(group_responses.state, db, gid, staff["display_name"])


@app.post("/api/groups/{gid}/responses/links")
async def group_responses_mint_link(gid: int, req: Request, staff=Depends(current_staff)):
    """Mint (or rotate) ONE person's individual link. The clear link is in this reply only - it is never stored, logged or emailed by the
    system; staff paste it into their own message to that person (H17). Any earlier link of that person on this version stops working."""
    body = await sec.json_object(req)
    uid = str(body.get("person_uid") or "").strip()
    if not uid:
        raise HTTPException(422, "Say which person the link is for.")

    def _mint():
        with retention_guard(db, label="group response link"):
            return group_responses.mint_link(db, gid, uid, staff["display_name"])
    return _group_responses_call(_mint)


@app.post("/api/groups/{gid}/responses/{rid}/reviewed")
def group_response_reviewed(gid: int, rid: int, staff=Depends(current_staff)):
    return _group_responses_call(group_responses.mark_reviewed, db, gid, rid, staff["display_name"])


@app.post("/api/public/group-responses/lookup")
async def public_group_response_lookup(req: Request):
    """The link page. The token travels in the POST body (the browser keeps it in the URL fragment, which never reaches a server log).
    Every refusal is the same sentence (410) whatever the reason: no detail is shown before the person and the group are verified."""
    body = await sec.json_object(req)
    sec.rate_limit(req, "public_group_responses", sec.RATE_DRAFTS_PER_HOUR, 3600)
    try:
        return group_responses.lookup(db, body.get("token"))
    except group_responses.LinkInvalid:
        raise HTTPException(410, group_responses.INVALID)
    except RetentionLockTimeout:
        raise HTTPException(503, "We are busy for a moment. Please try again shortly.")


@app.post("/api/public/group-responses/respond")
async def public_group_response_respond(req: Request):
    body = await sec.json_object(req)
    sec.rate_limit(req, "public_group_responses", sec.RATE_DRAFTS_PER_HOUR, 3600)
    try:
        return group_responses.respond(db, body.get("token"), str(body.get("response") or ""), body.get("note") or "")
    except group_responses.LinkInvalid:
        raise HTTPException(410, group_responses.INVALID)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RetentionLockTimeout:
        raise HTTPException(503, "We are busy for a moment. Your answer was not saved - please try again shortly.")


# ---------------------------------------------------------------------------
# Staff: test data, audit, settings, reference data
# ---------------------------------------------------------------------------
@app.post("/api/test-data/seed")
def seed_test(staff=Depends(current_staff), n: int = 180):
    rep = seed.seed_test_data(db, n=n)
    audit(db, staff["display_name"], "seed_test_data", None, None, f"{rep['created']} created")
    return rep


@app.delete("/api/test-data")
def delete_test(staff=Depends(current_staff)):
    # G-SL2: one guarded step; the audit row (actor, counts) is written inside it. A flag/ownership contradiction refuses the clear
    # before any write (409, counts only); guard failures surface as elsewhere (503).
    try:
        n = seed.delete_test_data(db, staff["display_name"])
    except seed.TestDataClearRefused as ex:
        audit(db, staff["display_name"], "delete_test_data_refused", None, None, str(ex))
        raise HTTPException(409, str(ex))
    return {"deleted": n}


@app.get("/api/audit")
def audit_list(staff=Depends(current_staff), limit: int = 200):
    return rows(db.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)))


@app.get("/api/reference")
def reference(staff=Depends(current_staff)):
    return {"question_types": QUESTION_TYPES, "statuses": STATUSES, "channels": CHANNELS, "institutions": institution_list(), "apt_types": rules.APT_TYPES,
            "features": feat.catalogue(), "feature_groups": feat.GROUPS, "features_max_top": feat.MAX_TOP,
            "price_guide": {"12m": rules.price_guide("12m"), "6m": rules.price_guide("6m")}, "ai_model": ai.MODEL,
            "ai_configured": ai.ai_mode() != "none", "ai_mode": ai.ai_mode(),
            "staff_list": accounts.active_display_names(db)}


def _ready_to_circulate_count(dataset: Optional[str]) -> int:
    """Proposed primary groups that can_circulate() accepts (valid approved review + brief + evidence + ownership)."""
    where = "status='Proposed' AND is_primary=1 AND ai_status='ok'"
    args: list = []
    if dataset:
        where += f" AND {DS_GROUP_SQL}"; args.append(_is_test(dataset))
    return sum(1 for g in rows(db.execute(f"SELECT * FROM groups WHERE {where}", args)) if _circulation(g).get("ok"))


@app.get("/api/stats")
def stats(staff=Depends(current_staff), dataset: Optional[str] = None):
    """Overview figures. With `dataset` (real / test) every group and enquiry count follows that data set, as the staff screens do;
    without it (older clients, tests) the counts cover all data."""
    def one(sql, *a):
        return db.execute(sql, a).fetchone()[0]
    if dataset:
        ds_sql, ds_args = f" AND {DS_GROUP_SQL}", (_is_test(dataset),)
    else:
        ds_sql, ds_args = "", ()

    def grp(where):
        return one(f"SELECT COUNT(*) FROM groups WHERE {where}{ds_sql}", *ds_args)
    return {
        "dataset": dataset,
        **homebot.stats(db),  # H14: homebot_new / homebot_open / homebot_flagged (+ _test); HomeBot rows live in their own tables
        "enquiries_real": one("SELECT COUNT(*) FROM enquiries WHERE is_test=0"), "enquiries_test": one("SELECT COUNT(*) FROM enquiries WHERE is_test=1"),
        "apartments": one("SELECT COUNT(*) FROM apartments"), "apartments_available": one("SELECT COUNT(*) FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL"),
        "apartments_leased": one("SELECT COUNT(*) FROM apartments WHERE leased_at IS NOT NULL"),  # GP8
        "groups_reranked": grp("rerank_note IS NOT NULL AND status NOT IN ('Leased','Dissolved')"),  # GP8 flags awaiting staff
        "couples_unanswered": one("SELECT COUNT(*) FROM enquiries WHERE party='With friends' AND couples_answer IS NULL AND is_test=0"),  # 3.10 not asked (pre-26 Oct real rows)
        "scheduler": {k: v for k, v in scheduler.status(db).items() if k in ("enabled", "last_check_at", "next_check_at", "last_scheduled_run", "running", "per_dataset")},
        "groups_proposed": grp("status='Proposed' AND is_primary=1"), "groups_alternative": grp("status='Proposed' AND is_primary=0"), "groups_active": grp("status IN ('Introduced','Group confirmed')"),
        "groups_leased": grp("status='Leased'"),
        # T6: AI queue over live groups (proposed groups only for the 'ready' count shown to reservations)
        "groups_ready": _ready_to_circulate_count(dataset),   # T6: the central predicate, not the ai_status flag
        "groups_ai_pending": grp("status IN ('Proposed','Introduced','Group confirmed','Awaiting replacement') AND ((ai_status IS NULL AND ai_rating IS NULL) OR ai_status IN ('pending','running','skipped'))"),
        "groups_ai_pending_primary": grp("status='Proposed' AND is_primary=1 AND ((ai_status IS NULL AND ai_rating IS NULL) OR ai_status IN ('pending','running','skipped'))"),
        "groups_ai_failed": grp("status IN ('Proposed','Introduced','Group confirmed','Awaiting replacement') AND ai_status='failed'"),
        "ai_queue": {k: v for k, v in ai_queue.status(db).items() if k in ("pending", "running", "ready", "failed", "refresh", "busy", "calls", "last_error", "last_error_at", "last_finished_at", "thread_alive", "per_dataset")},
        # GP10: groups kept together with an empty bed, and those where a later run found a replacement for staff to accept
        "groups_awaiting": grp(f"status='{matching.AWAITING}'"),
        "groups_replacement_found": grp(f"status='{matching.AWAITING}' AND replacement_json IS NOT NULL"),
        # GP11(f): friends waiting for staff approval to join a shared group
        "mate_requests_pending": one("SELECT COUNT(*) FROM mate_requests WHERE status='pending'" + (" AND is_test=?" if dataset else ""), *ds_args),
        "linked_parties": one("SELECT COUNT(DISTINCT linked_to_enquiry_id) FROM enquiries WHERE linked_to_enquiry_id IS NOT NULL" + (" AND is_test=?" if dataset else ""), *ds_args),
        # GP9(c): people barred from matching by staff (real / test)
        "enquiries_barred": one("SELECT COUNT(*) FROM enquiries WHERE barred_at IS NOT NULL AND is_test=0"),
        "enquiries_barred_test": one("SELECT COUNT(*) FROM enquiries WHERE barred_at IS NOT NULL AND is_test=1"),
        "unconfirmed_countries": one("SELECT COUNT(*) FROM enquiries WHERE birth_country_confirmed=0 AND is_test=0"),
        "unconfirmed_countries_test": one("SELECT COUNT(*) FROM enquiries WHERE birth_country_confirmed=0 AND is_test=1"),
        # I16(b): party members whose typed country of birth still needs staff confirmation (I15)
        "unconfirmed_member_countries": one("SELECT COUNT(*) FROM party_members pm JOIN enquiries e ON e.id=pm.enquiry_id WHERE pm.birth_country_raw IS NOT NULL AND pm.birth_country_confirmed=0 AND e.is_test=0"),
        "unconfirmed_member_countries_test": one("SELECT COUNT(*) FROM party_members pm JOIN enquiries e ON e.id=pm.enquiry_id WHERE pm.birth_country_raw IS NOT NULL AND pm.birth_country_confirmed=0 AND e.is_test=1"),
        # I11(d): staff to-do, open enquiries whose all-occupants 18+ at move-in declaration is missing or needs renewing
        "adults_todo": _adults_todo_count(0),
        "adults_todo_test": _adults_todo_count(1),
        "by_status": {r["status"]: r["n"] for r in rows(db.execute("SELECT status, COUNT(*) n FROM enquiries WHERE is_test=0 GROUP BY status"))},
        "by_status_test": {r["status"]: r["n"] for r in rows(db.execute("SELECT status, COUNT(*) n FROM enquiries WHERE is_test=1 GROUP BY status"))},
        "ai_configured": ai.ai_mode() != "none", "ai_mode": ai.ai_mode(),
    }


@app.get("/api/health")
def health(request: Request):
    if sec.pilot_gate_enabled() and not getattr(request.state, "pilot_user", None):
        # Minimal reply for the host's health check and any visitor without a pilot sign-in: alive and database reachable, no detail.
        db.execute("SELECT 1").fetchone()
        return {"ok": True}
    return {"ok": True, "time": now_iso(), "database": getattr(db, "backend", "sqlite"),
            "apartments": db.execute("SELECT COUNT(*) FROM apartments").fetchone()[0]}


# ---------------------------------------------------------------------------
# HomeBot apartment enquiries (H14; change request 4, 5 Nov 2026) — server/homebot.py
# Separate tables, separate ids ("HomeBot-00012"), separate statuses. Manual staff review only: nothing here matches,
# shortlists, routes, emails or messages. HomeBot rows never enter the MateBot matcher, scheduler, AI queue or groups.
# ---------------------------------------------------------------------------
@app.get("/api/public/homebot/meta")
def homebot_meta():
    return homebot.meta(db)


def _homebot_reply(e: dict, is_test: bool, receipt_status: str) -> dict:
    """Saved-state acknowledgement, not a promise of delivery or a completed match."""
    return {"id": e["id"], "reference": e["reference"], "first_name": e["first_name"], "is_test": is_test, "status": e["status"],
            "people": len(e.get("people", [])), "receipt": receipt_status, "matching": "manual_review"}


@app.post("/api/public/homebot/handoff", status_code=201)
async def create_homebot_handoff(req: Request):
    """H5/H14: the HomeBot hand-off screen saves the party's size answers as a MateBot saved form (origin 'homebot') and
    returns the resume link. Only compatible answers are carried (server/homebot_handoff.py); no identity exists yet."""
    body = _pilot_force_test(await sec.json_object(req))
    sec.rate_limit(req, "public_drafts", sec.RATE_DRAFTS_PER_HOUR, 3600)
    try:
        out = homebot_handoff.create(db, body.get("headcount_now"), body.get("headcount_target"), is_test=bool(body.get("is_test")),
                                     now_iso=now_iso(), expires_iso=_iso_in(DRAFT_DAYS), transport=body.get("transport"))
    except homebot_handoff.HandoffError as ex:
        return JSONResponse({"errors": ex.errors}, status_code=422)
    db.commit()
    return out


@app.post("/api/public/homebot/enquiries", status_code=201)
async def submit_homebot(req: Request):
    body = _pilot_force_test(await sec.json_object(req))
    if str(body.get("company_website") or "").strip():  # honeypot, as the MateBot form
        return JSONResponse({"errors": {"form": "Sorry, this form could not be sent. Please try again."}}, status_code=422)
    sec.rate_limit(req, "public_enquiries", sec.RATE_ENQUIRIES_PER_HOUR, 3600)
    is_test = body.get("is_test") is True  # strict boolean (validation refuses anything but true/false)
    # The submission key is REQUIRED here (unlike the older MateBot clients): a HomeBot request without a usable key is refused
    # rather than saved non-idempotently.
    key = submissions.clean_key(req.headers.get(submissions.HEADER))
    if not key:
        return JSONResponse({"errors": {"form": "This form needs a submission key. Please reload the page and try again."}}, status_code=422)
    sub = submissions.reserve(db, key, submissions.HOMEBOT_SCOPE, submissions.fingerprint(body, {}), is_test)
    early = _homebot_early(sub)
    if early is not None:
        return early
    if sub.state in ("replay", "saved"):
        return _homebot_replay(sub)

    def hook(c, eid):
        submissions.mark_saved(c, sub, eid)  # same transaction as the enquiry and its people: all rows or none
        homebot_receipts.enqueue_in_transaction(c, eid)
        homebot_shortlist.on_enquiry_created(c, eid)  # T3(a) H2: first requirement version + options job, same transaction (no-op when off)

    try:
        eid = homebot.create(db, body, is_test=is_test, submission_key=key, on_inserted=hook)
    except ValidationError as ve:
        submissions.release(db, sub)
        return JSONResponse({"errors": ve.errors}, status_code=422)
    except submissions.Superseded:
        again = _reread(sub, submissions.HOMEBOT_SCOPE, body, {}, is_test)
        early = _homebot_early(again)
        return early if early is not None else _homebot_replay(again)
    except Exception as ex:
        try:
            db.rollback()
        except Exception:
            pass
        if is_unique_violation(ex):
            again = _reread(sub, submissions.HOMEBOT_SCOPE, body, {}, is_test)
            early = _homebot_early(again)
            return early if early is not None else _homebot_replay(again)
        submissions.release(db, sub)
        raise
    e = homebot.get(db, eid)
    out = _homebot_reply(e, is_test, homebot_receipts.after_saved(db, eid)["status"])
    submissions.finish(db, key, eid, out)
    return out


def _homebot_early(sub: submissions.Submission):
    """As _submission_replies, but a conflict quotes the HomeBot reference (never a MateBot-prefixed one)."""
    if sub.state == "conflict":
        eid = sub.enquiry_id
        body = {"already_submitted": True, "errors": {"form": homebot.already_submitted_message(eid) if eid else submissions.GENERIC_USED_MESSAGE}}
        if eid:
            body["result"] = dict(sub.response, id=eid, replayed=True)
        return JSONResponse(body, status_code=409)
    return _submission_replies(sub)


def _homebot_replay(sub: submissions.Submission):
    try:
        e = homebot.get(db, sub.enquiry_id)
    except KeyError:
        return JSONResponse({"already_submitted": True, "errors": {"form": submissions.DELETED_MESSAGE}}, status_code=409)
    if sub.state == "replay":
        homebot_receipts.after_saved(db, e["id"])  # repair/kick only; retain the original submission reply
        return JSONResponse(dict(sub.response, replayed=True), status_code=200)
    out = _homebot_reply(e, bool(e["is_test"]), homebot_receipts.after_saved(db, e["id"])["status"])
    submissions.finish(db, sub.key, e["id"], out)
    return JSONResponse(dict(out, replayed=True), status_code=200)


@app.get("/api/homebot/enquiries")
def homebot_list(staff=Depends(current_staff), dataset: Optional[str] = "real", q: Optional[str] = "", status: Optional[str] = "", flag: Optional[str] = ""):
    return homebot.list_(db, dataset or "real", (q or "")[:80], status or "", flag or "")


@app.get("/api/homebot/enquiries/{eid}")
def homebot_detail(eid: int, staff=Depends(current_staff)):
    try:
        return homebot.get(db, eid)
    except KeyError:
        raise HTTPException(404, "Not found")


@app.patch("/api/homebot/enquiries/{eid}")
async def homebot_update(eid: int, req: Request, staff=Depends(current_staff)):
    try:
        return homebot.update(db, eid, await sec.json_object(req), staff["display_name"])
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValidationError as ve:
        return JSONResponse({"errors": ve.errors}, status_code=422)


@app.post("/api/homebot/enquiries/{eid}/contact")
async def homebot_contact(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req, empty_ok=True)
    try:
        return homebot.contact(db, eid, staff["display_name"], str(body.get("note") or ""))
    except KeyError:
        raise HTTPException(404, "Not found")


@app.delete("/api/homebot/enquiries/{eid}")
def homebot_delete(eid: int, staff=Depends(current_staff)):
    try:
        homebot.delete(db, eid, staff["display_name"])
    except KeyError:
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# T3(a) H2: HomeBot integrated matching — options (shortlists), requirement edits, planning budget, rejections (server/homebot_shortlist.py)
# Switched off unless MMAI_HOMEBOT_MATCHING is set; every route then answers 409 with a plain sentence.
# ---------------------------------------------------------------------------
def _homebot_options_call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except homebot_shortlist.SwitchedOff as ex:
        raise HTTPException(409, str(ex))
    except KeyError:
        raise HTTPException(404, "Not found")
    except ValidationError as ve:
        return JSONResponse({"errors": ve.errors}, status_code=422)


@app.get("/api/homebot/matching-status")
def homebot_matching_status(staff=Depends(current_staff)):
    on = homebot_shortlist.enabled()
    out = {"enabled": on, "plain": "HomeBot apartment matching is on. Open each enquiry to check its travel evidence and AI review status." if on else homebot_shortlist.SWITCHED_OFF,
           "algorithm_version": homebot_shortlist.ALGORITHM_VERSION}
    if on:
        out["jobs"] = {r["status"]: r["n"] for r in rows(db.execute("SELECT status, COUNT(*) AS n FROM homebot_jobs GROUP BY status"))}
    return out


@app.get("/api/homebot/enquiries/{eid}/options")
def homebot_options(eid: int, staff=Depends(current_staff)):
    return _homebot_options_call(homebot_shortlist.overview, db, eid)


@app.post("/api/homebot/enquiries/{eid}/options/rerun")
async def homebot_options_rerun(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req, empty_ok=True)
    return _homebot_options_call(homebot_shortlist.request_rerun, db, eid, staff["display_name"], str(body.get("scenario") or homebot_shortlist.hm.SCENARIO_CURRENT))


@app.post("/api/groups/{gid}/travel-check")
async def group_travel_check(gid: int, staff=Depends(current_staff)):
    """The live final travel check for a MateBot group (staff only). Shown once and not saved; see matebot_live. It waits on
    the journey provider, so it runs in the worker pool and never holds up other requests."""
    def run():
        g = row(db.execute("SELECT g.*, a.address, a.type_code, a.suburb, a.bills_included FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (gid,)))
        if not g:
            raise HTTPException(404, "Not found")
        return matebot_live.travel_check(db, _group_out(g), staff["display_name"])
    return await run_in_threadpool(run)


@app.post("/api/homebot/enquiries/{eid}/options/travel-check")
async def homebot_options_travel_check(eid: int, req: Request, staff=Depends(current_staff)):
    """Live journey estimates for the options on the page (staff only). Shown once and not saved; see homebot_shortlist.
    The check waits on the journey provider, so it runs in the worker pool and never holds up other requests."""
    body = await sec.json_object(req, empty_ok=True)
    scenario = str(body.get("scenario") or homebot_shortlist.hm.SCENARIO_CURRENT)
    return await run_in_threadpool(_homebot_options_call, homebot_shortlist.live_travel_check, db, eid, scenario, staff["display_name"])


@app.post("/api/homebot/enquiries/{eid}/options/review-retry")
async def homebot_options_review_retry(eid: int, req: Request, staff=Depends(current_staff)):
    """Explicit staff retry of the AI review hand-over for the current options (held until the review completes)."""
    body = await sec.json_object(req, empty_ok=True)
    return _homebot_options_call(homebot_shortlist.retry_review, db, eid, str(body.get("scenario") or homebot_shortlist.hm.SCENARIO_CURRENT), staff["display_name"])


@app.patch("/api/homebot/enquiries/{eid}/requirements")
async def homebot_requirements(eid: int, req: Request, staff=Depends(current_staff)):
    return _homebot_options_call(homebot_shortlist.update_requirements, db, eid, await sec.json_object(req), staff["display_name"])


@app.post("/api/homebot/enquiries/{eid}/planning")
async def homebot_planning(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    return _homebot_options_call(homebot_shortlist.confirm_planning, db, eid, body.get("budget_total"), staff["display_name"])


@app.post("/api/homebot/enquiries/{eid}/rejections", status_code=201)
async def homebot_rejection_create(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        aid = int(body.get("apartment_id"))
    except (TypeError, ValueError):
        return JSONResponse({"errors": {"apartment_id": "Choose the apartment."}}, status_code=422)
    sid = body.get("shortlist_id")
    return _homebot_options_call(homebot_shortlist.record_rejection, db, eid, scope=str(body.get("scope") or "apartment"), apartment_id=aid,
                                 staff=staff["display_name"], customer_reason=str(body.get("customer_reason") or ""), shortlist_id=int(sid) if sid else None)


@app.get("/api/homebot/rejections/{rid}/material-change")
def homebot_rejection_diff(rid: int, apartment_id: int, staff=Depends(current_staff)):
    return _homebot_options_call(lambda: {"changes": homebot_shortlist.material_change(db, rid, apartment_id)})


@app.post("/api/homebot/rejections/{rid}/reopen")
async def homebot_rejection_reopen(rid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    return _homebot_options_call(homebot_shortlist.reopen_by_customer, db, rid, staff["display_name"], str(body.get("note") or ""))


@app.post("/api/homebot/rejections/{rid}/reoffer")
async def homebot_rejection_reoffer(rid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        aid = int(body.get("apartment_id"))
    except (TypeError, ValueError):
        return JSONResponse({"errors": {"apartment_id": "Choose the apartment."}}, status_code=422)
    return _homebot_options_call(homebot_shortlist.allow_reoffer, db, rid, aid, staff["display_name"], str(body.get("note") or ""))


# ---------------------------------------------------------------------------
# GP14 (MateBot): a room/bed enquirer declines an apartment or a building - staff record it (server/apartment_rejections.py)
# ---------------------------------------------------------------------------
def _rejection_call(fn, *args, _write: bool = True, **kw):
    try:
        if not _write:
            return fn(*args, **kw)
        with retention_guard(db, label="apartment rejection"):
            return fn(*args, **kw)
    except apartment_rejections.RejectionError as ex:
        raise HTTPException(404 if str(ex).startswith("No such") else 409, str(ex))
    except (RetentionLockTimeout, ConnectionLostMidOperation):
        raise HTTPException(503, "The system is busy finishing another update. Try again in a moment.")


@app.get("/api/enquiries/{eid}/apartment-rejections")
def apartment_rejections_list(eid: int, staff=Depends(current_staff)):
    if not row(db.execute("SELECT 1 FROM enquiries WHERE id=?", (eid,))):
        raise HTTPException(404, "Not found")
    # the apartments this person's live groups currently list (what staff can record a decline against) - listed options only
    offered = rows(db.execute(f"""SELECT ga.apartment_id, a.address, a.type_code, ga.group_id, g.status AS group_status,
                                         CASE WHEN g.apartment_id=ga.apartment_id THEN 1 ELSE 0 END AS is_current
                                  FROM group_members gm JOIN groups g ON g.id=gm.group_id JOIN group_apartments ga ON ga.group_id=g.id
                                  JOIN apartments a ON a.id=ga.apartment_id
                                  WHERE gm.enquiry_id=? AND g.status IN ({','.join('?' * len(apartment_rejections.LIVE_GROUP_STATUSES))})
                                  ORDER BY g.id DESC, ga.rank""", (eid, *apartment_rejections.LIVE_GROUP_STATUSES)))
    return {"items": apartment_rejections.for_enquiry(db, eid), "exclusions": apartment_rejections.exclusions_for(db, eid), "offered": offered}


@app.post("/api/enquiries/{eid}/apartment-rejections", status_code=201)
async def apartment_rejection_create(eid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        aid = int(body.get("apartment_id"))
    except (TypeError, ValueError):
        return JSONResponse({"errors": {"apartment_id": "Choose the apartment."}}, status_code=422)
    gid = body.get("group_id")
    return _rejection_call(apartment_rejections.record, db, eid, str(body.get("scope") or "apartment"), aid, staff["display_name"],
                           customer_reason=str(body.get("customer_reason") or ""), group_id=int(gid) if gid else None)


@app.post("/api/apartment-rejections/{rid}/reopen")
async def apartment_rejection_reopen(rid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    return _rejection_call(apartment_rejections.reopen_by_customer, db, rid, staff["display_name"], str(body.get("note") or ""))


@app.get("/api/apartment-rejections/{rid}/material-change")
def apartment_rejection_diff(rid: int, apartment_id: int | None = None, staff=Depends(current_staff)):
    return _rejection_call(apartment_rejections.material_change, db, rid, apartment_id, _write=False)


@app.post("/api/apartment-rejections/{rid}/reoffer")
async def apartment_rejection_reoffer(rid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    try:
        aid = int(body.get("apartment_id"))
    except (TypeError, ValueError):
        return JSONResponse({"errors": {"apartment_id": "Choose the apartment."}}, status_code=422)
    return _rejection_call(apartment_rejections.allow_reoffer, db, rid, aid, staff["display_name"], str(body.get("note") or ""))


# ---------------------------------------------------------------------------
# A10 exact-booking authority. Staff-only writes and recheck requests commit together.
# ---------------------------------------------------------------------------
def _exception_call(fn, *args, _prepare_homebot=None, **kwargs):
    from . import homebot_match, homebot_shortlist
    try:
        with retention_guard(db, label="exit exception"):
            if _prepare_homebot is not None:
                eid, scenario, actor = _prepare_homebot
                scenario = scenario or homebot_match.SCENARIO_CURRENT
                try:
                    e = homebot_shortlist._capture(db, eid)
                except KeyError:
                    raise HTTPException(404, "No such HomeBot enquiry") from None
                homebot_shortlist._ensure_version(db, e, scenario, created_by=actor,
                                                note=None, now=now_iso())
            result = fn(*args, **kwargs)
            if result["bot"] == exit_exceptions.BOT_MATEBOT:
                ai_queue.enqueue(db, [result["group_id"]], reason="exit exception", refresh=True, commit=False)
            elif homebot_shortlist.enabled():
                owner = row(db.execute("SELECT status FROM homebot_enquiries WHERE id=?", (result["homebot_enquiry_id"],)))
                if owner and owner["status"] in homebot_shortlist.OPEN_STATUSES:
                    homebot_shortlist._ensure_job(db, result["homebot_enquiry_id"], result["scenario"],
                                                 "exit_exception", now_iso())
                    homebot_shortlist.kick()
            return result
    except exit_exceptions.ExitExceptionError as ex:
        raise HTTPException(ex.status, str(ex)) from None
    # Lock timeout and connection-loss outcomes retain the global handlers' truthful wording.


def _exception_body(body: dict) -> dict:
    errors = {}
    try:
        raw_aid = body.get("apartment_id")
        if (not isinstance(raw_aid, (int, str)) or isinstance(raw_aid, bool)
                or not re.fullmatch(r"[1-9]\d*", str(raw_aid))):
            raise ValueError
        aid = int(raw_aid)
    except (TypeError, ValueError):
        aid = None
        errors["apartment_id"] = "Choose the apartment."
    for key in ("move_in", "move_out"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(body.get(key) or "")):
            errors[key] = "Enter the date as YYYY-MM-DD."
    total = str(body.get("exception_total_weekly_rent") or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", total):
        errors["exception_total_weekly_rent"] = "Enter the whole-apartment weekly total, including any extra-bed charge."
    return {"errors": errors, "apartment_id": aid, "move_in": body.get("move_in"),
            "move_out": body.get("move_out"), "total": total}


@app.get("/api/groups/{gid}/exit-exceptions")
def group_exit_exceptions(gid: int, staff=Depends(current_staff)):
    if not row(db.execute("SELECT 1 FROM groups WHERE id=?", (gid,))):
        raise HTTPException(404, "Not found")
    return {"items": exit_exceptions.for_group(db, gid)}


@app.post("/api/groups/{gid}/exit-exception", status_code=201)
async def group_exit_exception_propose(gid: int, req: Request, staff=Depends(current_staff)):
    b = _exception_body(await sec.json_object(req))
    if b["errors"]:
        return JSONResponse({"errors": b["errors"]}, status_code=422)
    return _exception_call(exit_exceptions.propose, db, exit_exceptions.BOT_MATEBOT, gid, b["apartment_id"],
                           b["move_in"], b["move_out"], b["total"], staff["display_name"])


@app.get("/api/homebot/enquiries/{eid}/exit-exceptions")
def homebot_exit_exceptions(eid: int, staff=Depends(current_staff)):
    if not row(db.execute("SELECT 1 FROM homebot_enquiries WHERE id=?", (eid,))):
        raise HTTPException(404, "Not found")
    return {"items": exit_exceptions.for_homebot_enquiry(db, eid)}


@app.post("/api/homebot/enquiries/{eid}/exit-exception", status_code=201)
async def homebot_exit_exception_propose(eid: int, req: Request, staff=Depends(current_staff)):
    from . import homebot_match, homebot_shortlist
    body = await sec.json_object(req)
    b = _exception_body(body)
    scenario = str(body.get("scenario") or homebot_match.SCENARIO_CURRENT)
    if scenario not in homebot_shortlist.SCENARIOS:
        b["errors"]["scenario"] = "Unknown scenario."
    if b["errors"]:
        return JSONResponse({"errors": b["errors"]}, status_code=422)
    return _exception_call(exit_exceptions.propose, db, exit_exceptions.BOT_HOMEBOT, eid, b["apartment_id"],
                           b["move_in"], b["move_out"], b["total"], staff["display_name"], scenario=scenario,
                           _prepare_homebot=(eid, scenario, staff["display_name"]))


@app.post("/api/exit-exceptions/{xid}/agreement")
async def exit_exception_agreement(xid: int, req: Request, staff=Depends(current_staff)):
    body = await sec.json_object(req)
    total = str(body.get("total") or "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", total):
        return JSONResponse({"errors": {"total": "Enter the agreed whole-apartment weekly total as a number."}}, status_code=422)
    return _exception_call(exit_exceptions.record_agreement, db, xid, staff["display_name"],
                           str(body.get("agreement_scope") or ""), total)


@app.post("/api/exit-exceptions/{xid}/withdraw")
def exit_exception_withdraw(xid: int, staff=Depends(current_staff)):
    return _exception_call(exit_exceptions.withdraw, db, xid, staff["display_name"])


# ---------------------------------------------------------------------------
# T5 scheduled purge (daily) in a background thread
# ---------------------------------------------------------------------------
# importlib.reload retains this module's globals. Preserve the existing worker's
# schedule rather than spawning another sleeping daily worker on every reload.
_next_purge_at = globals().get("_next_purge_at")
_purge_failures = globals().get("_purge_failures", {})
_last_purge_completed_at = globals().get("_last_purge_completed_at")


def _purge_pass():
    """Attempt both retention domains; log categories only, never exception text."""
    global _purge_failures, _last_purge_completed_at
    failures = {}
    for name, action in (
        ("matebot", lambda: purge_expired(db, "scheduler")),
        ("homebot", lambda: homebot.purge_expired(db)),
    ):
        try:
            action()
        except Exception as error:
            failures[name] = type(error).__name__
            # No traceback or error message: drivers can include private record data.
            print(f"[mmai retention] {name} purge failed ({type(error).__name__}); retry scheduled",
                  file=sys.stderr, flush=True)
    _purge_failures = failures
    if not failures:
        _last_purge_completed_at = now_iso()
    return not failures


def _schedule_purge():
    global _next_purge_at
    seconds = 60 if _purge_failures else 24 * 3600
    _next_purge_at = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()
    return seconds


def _purge_loop():
    while True:
        time.sleep(_schedule_purge())
        _purge_pass()


if globals().get("_purge_thread") is None or not _purge_thread.is_alive():
    # Complete the first attempt before accepting requests. A daily restart must
    # not postpone deletion forever, and import/reload must not race the first
    # request against a newly started purge using the shared connection.
    _purge_pass()
    _schedule_purge()
    _purge_thread = threading.Thread(
        target=_purge_loop, name="mmai-retention-purge", daemon=True
    )
    _purge_thread.start()


# ---------------------------------------------------------------------------
# Static client (for local use; the preview serves client/ via deploy_website)
# ---------------------------------------------------------------------------
HOMEBOT_DIR = os.environ.get("MMAI_HOMEBOT_DIR") or os.path.join(os.path.dirname(os.path.dirname(HERE)), "homebot-app")


class _HomeBotStatic(StaticFiles):
    """The HomeBot form served from the same origin at /homebot/ (self-host). Only the client files: notes (`.md`), the `tools/`
    folder and dot-files are never served. The API-base placeholder in index.html is rewritten to "" (same origin), so a
    self-hosted copy needs no build step; a CDN copy is rewritten at deploy time instead (see homebot-app/README.md)."""

    async def get_response(self, path: str, scope):
        if path in ("", ".", "index.html"):
            return self._index()
        parts = [p for p in path.split("/") if p]
        if any(p.startswith(".") or p == "tools" or p.lower().endswith(".md") for p in parts):
            raise HTTPException(404, "Not found")
        return await super().get_response(path, scope)

    @staticmethod
    def _index():
        try:
            with open(os.path.join(HOMEBOT_DIR, "index.html"), encoding="utf-8") as f:
                html = re.sub(r'(<meta name="mmai-api-base" content=")__PORT_\d+__(")', r"\1\2", f.read())
        except OSError:
            raise HTTPException(404, "Not found")
        # Bundled forms share this backend and its saved-form tokens, never the old hosted preview.
        html = html.replace("https://matebot-ai.pplx.app/#/", "/#/")
        review = "off" if os.environ.get("MMAI_HOMEBOT_REVIEW_BUILD") == "0" else "on"
        html = re.sub(r'(<meta name="homebot-review-build" content=")[^"]*(")',
                      lambda m: m[1] + review + m[2], html)
        return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


LEGAL_DIR = os.path.join(HERE, "legal")


@app.api_route("/terms", methods=["GET", "HEAD"], include_in_schema=False)
def terms_of_use_page():
    """Public notice: Terms of Use. A fixed page, readable without sign-in whether the pilot gate is on or off."""
    return FileResponse(os.path.join(LEGAL_DIR, "terms.html"), media_type="text/html")


@app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
def privacy_policy_page():
    """Public notice: Privacy Policy. A fixed page, readable without sign-in whether the pilot gate is on or off."""
    return FileResponse(os.path.join(LEGAL_DIR, "privacy.html"), media_type="text/html")


PILOT_DIR = os.path.join(HERE, "pilot")
_PILOT_FILES = {"": ("index.html", "text/html"), "pilot.js": ("pilot.js", "text/javascript"), "pilot.css": ("pilot.css", "text/css")}


@app.api_route("/pilot", methods=["GET", "HEAD"], include_in_schema=False)
def pilot_root(request: Request):
    if not sec.pilot_gate_enabled():
        raise HTTPException(404, "Not found")
    return RedirectResponse("/pilot/" + ("?" + request.url.query if request.url.query else ""), status_code=307)


@app.api_route("/pilot/{name:path}", methods=["GET", "HEAD"], include_in_schema=False)
def pilot_page(name: str):
    """The pilot sign-in page and its two files. Only while the gate is on; otherwise these addresses do not exist."""
    entry = _PILOT_FILES.get(name) if sec.pilot_gate_enabled() else None
    if not entry:
        raise HTTPException(404, "Not found")
    return FileResponse(os.path.join(PILOT_DIR, entry[0]), media_type=entry[1])


if os.path.isdir(HOMEBOT_DIR):
    @app.get("/homebot", include_in_schema=False)
    def homebot_root(request: Request):
        # A trailing slash is required for relative HomeBot scripts/styles, including query-mode TEST links.
        return RedirectResponse("/homebot/" + ("?" + request.url.query if request.url.query else ""), status_code=307)

    app.mount("/homebot", _HomeBotStatic(directory=HOMEBOT_DIR, html=True), name="homebot")
if os.path.isdir(CLIENT_DIR):
    app.mount("/", StaticFiles(directory=CLIENT_DIR, html=True), name="client")


def bootstrap():
    """First run: import the apartment list if the table is empty. Every run: apply the A15 corrections (idempotent)."""
    if db.execute("SELECT COUNT(*) FROM apartments").fetchone()[0] == 0 and os.path.exists(XLSX_DEFAULT):
        importer.import_apartments(db, XLSX_DEFAULT, "bootstrap")
    importer.apply_a15_corrections(db, "bootstrap")
    # I16(f) migration, 26 Oct 2026 (fourth update). 3.8 "Couple sharing one bed" rows: the couple is the enquirer + Person 2 (derived
    # from Bill's 3.8 wording, all data sets). TEST "With friends" rows were synthetic and never held a couple, so they get 3.10 = "No"
    # (the old build default "enquirer + Person 2 are a couple" is dropped; no TEST friends row relied on it). REAL "With friends" rows
    # saved before 3.10 existed keep NULL = not asked: treated as no couple and flagged for staff to confirm by phone.
    db.execute("""UPDATE enquiries SET couples_answer='Yes', couples_json='[["lead","m1"]]' WHERE party LIKE 'Couple%' AND couples_answer IS NULL""")
    db.execute("""UPDATE enquiries SET couples_answer='No', couples_json='[]' WHERE party='With friends' AND couples_answer IS NULL AND is_test=1""")
    # T10 (26 Oct 2026, eighth update): notes stored before this update carried register codes. One-off tidy: derive `flag_kind`
    # from the old wording and rewrite those notes in plain English. Only rows that still contain a code are touched.
    code_re = re.compile(r"\s*\((?:GP|A|B|I|M|T)\d{1,2}(?:\([a-z]\))*(?:[;,/ ]+(?:(?:GP|A|B|I|M|T)\d{1,2}(?:\([a-z]\))*|GP5 exception|exception|applied))*\)|\b(?:GP|A|B|I|M|T)\d{1,2}(?:\([a-z]\))*\b:?\s*")
    for g in rows(db.execute("SELECT id, rerank_note, awaiting_note, flag_kind, status FROM groups WHERE (rerank_note LIKE '%GP%' OR awaiting_note LIKE '%GP%' OR rerank_note LIKE '%(T%' OR awaiting_note LIKE '%(A%' OR rerank_note LIKE '%the pool%')")):
        kind = g["flag_kind"]
        note = g["rerank_note"] or ""
        if not kind:
            kind = "bar" if "GP9" in note else "rejection" if "GP7" in note else "replacement" if "accepted" in note else "dissolved" if "by staff" in note else "lease" if note else None
        def tidy(t):
            if not t:
                return t
            t = t.replace("(GP10; GP5 exception)", "(an empty bed is allowed while a replacement is awaited)")
            t = t.replace("returned to the pool for the next matching run", "will be included in the next matching run").replace("The other members returned to the pool.", "The other members will be included in the next matching run.")
            t = code_re.sub("", t)
            t = re.sub(r",\s*\)", ")", t)
            return re.sub(r"\s+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", t)).strip()
        db.execute("UPDATE groups SET rerank_note=?, awaiting_note=?, flag_kind=? WHERE id=?", (tidy(g["rerank_note"]), tidy(g["awaiting_note"]), kind, g["id"]))
    for tbl in ("groups", "group_apartments"):  # exit-window labels stored by earlier runs carried register codes
        db.execute(f"UPDATE {tbl} SET exit_window=REPLACE(exit_window, ', A6(b))', ')') WHERE exit_window LIKE '%, A6(b))'")
        db.execute(f"UPDATE {tbl} SET exit_window=REPLACE(exit_window, ' (A10(e))', '') WHERE exit_window LIKE '% (A10(e))'")
    db.execute("UPDATE groups SET flag_kind='lease' WHERE flag_kind IS NULL AND rerank_note LIKE 'Re-ranked:%'")
    db.execute("UPDATE groups SET rerank_note=REPLACE(rerank_note, 'Re-ranked: ', '') WHERE rerank_note LIKE 'Re-ranked:%'")
    db.commit()
    # T8: full runs left 'running' by a previous process were interrupted by the restart
    db.execute("UPDATE match_runs SET status='interrupted', finished_at=COALESCE(finished_at, ?) WHERE kind='full' AND status='running'", (now_iso(),))
    # I16(b): synthetic TEST party members saved before 26 Oct 2026 get the lead's country as their country of birth (test rows only)
    db.execute("""UPDATE party_members SET birth_country=(SELECT birth_country FROM enquiries e WHERE e.id=party_members.enquiry_id),
                  birth_country_raw=(SELECT birth_country FROM enquiries e WHERE e.id=party_members.enquiry_id), birth_country_confirmed=1
                  WHERE birth_country IS NULL AND enquiry_id IN (SELECT id FROM enquiries WHERE is_test=1 AND birth_country IS NOT NULL)""")
    db.commit()


bootstrap()
scheduler.start(db)  # T8 (disabled with MMAI_SCHEDULER=0)
notifications.start(db)  # Independent of the hourly matching loop; all delivery modes remain off by default.
homebot_shortlist.start_worker(db)  # T3(a) H2 HomeBot options worker (only when MMAI_HOMEBOT_MATCHING is set; off by default)
ai_queue.start(db)   # T6 AI assessment queue (disabled with MMAI_AI_QUEUE=0, e.g. tests)
from . import pilot_selfcheck  # noqa: E402
pilot_selfcheck.start(sec.pilot_gate_enabled())  # opt-in (MMAI_PILOT_SELFCHECK=1): prints the gate's own refusals to the runtime log

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
