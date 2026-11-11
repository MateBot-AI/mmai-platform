"""Submission reliability (31 October 2026): one enquiry per form submission, however many times the request arrives.

Why: a form can send the same enquiry more than once without anyone meaning it to: a double click or double Enter, a reply
lost on a bad connection and a retry, a screen that showed an error after the server had already saved, or a reload while
"Saving…" was showing. Each would make another enquiry for the same person, and every copy would then be matched like a
separate applicant. This module makes a retry harmless.

How: the form makes one opaque random key per enquiry attempt (sent as the `X-Submission-Key` header; a saved draft keeps
the same key so a resumed form retries with it). The server keeps one `enquiry_submissions` row per key:

  reserve()  INSERT the key as `pending` (atomic: INSERT OR IGNORE / ON CONFLICT DO NOTHING, so two simultaneous requests
             can never both own it). If the key already exists:
               done   -> same answers: replay the stored response; different answers: refuse (already submitted, with the
                         reference), never a new enquiry and never an overwrite of the saved one.
               saved  -> the enquiry was committed but the side effects or the reply were lost: finish and reply again.
               pending-> another request is still working on it (reply "in progress", the form waits and retries); a
                         pending row older than STALE_SECONDS whose owner died is taken over, with a new `owner` token.
               deleted-> tombstone: the enquiry was later deleted (staff, T5 retention, test-data clear). Refused, with no
                         details, so a retry can never recreate a deleted enquiry.
  on_inserted  create_enquiry() calls back inside the enquiry's own transaction (connection lock held for the whole block) and
             the row becomes `saved` with the new enquiry id, so "enquiry written" and "key knows about it" are committed
             together. Fencing: the UPDATE only matches while `owner` is still this request's token; a request whose
             reservation was taken over while it stalled raises Superseded and its transaction is rolled back.
  backstop   The key is also written on enquiries.submission_key (UNIQUE index) in the same INSERT, so even if every check
             above were bypassed the database itself refuses a second enquiry for one key; the caller treats that
             violation as "already saved by the other attempt" and replays instead of failing.
  finish()   stores the response and marks `done`.
  release()  deletes a `pending` row that produced no enquiry (validation error, refused link, crash before the insert), so
             the person can fix their answers and send again with the same key.

Scope: `public` for the three public forms, `staff:<username>` for the phone form, so a key is never replayed across them;
a key seen from another scope is refused with a generic message and nothing about the other scope's enquiry.
Only the key, a hash of the answers, the enquiry id and the response the form already received are stored: no extra
personal data. Clients that send no key keep the old behaviour exactly (no reservation, no replay).
Both databases: SQLite (one shared connection, statements serialised) and PostgreSQL (row-level uniqueness). Schema additive.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from .db import j, now_iso, pj, row, transaction

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
STALE_SECONDS = 120  # a `pending` reservation older than this with no enquiry belongs to a request that died; a retry may take it over
HEADER = "X-Submission-Key"


def clean_key(value) -> str | None:
    """The key as sent by the form, or None when absent or malformed (malformed keys are ignored: legacy behaviour applies)."""
    v = (value or "").strip() if isinstance(value, str) else ""
    return v if KEY_RE.match(v) else None


def fingerprint(body: dict, extra: dict | None = None) -> str:
    """Stable hash of the answers (and the link tokens that shape the result). Same answers -> same hash, whatever the key order."""
    payload = {"body": body, "extra": {k: v for k, v in (extra or {}).items() if v}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


class Superseded(Exception):
    """This request's reservation was taken over by a later retry (or the database refused a second enquiry for the key):
    the enquiry transaction was rolled back; the caller re-reads the key and replays whatever the other attempt saved."""


class Submission:
    """The outcome of reserve(): either we own the key (`fresh`, with an `owner` fencing token) or an earlier attempt already got somewhere."""

    def __init__(self, key: str, state: str, existing: dict | None = None, owner: str | None = None):
        # state: fresh / replay / saved / in_progress / conflict / foreign (other scope) / deleted (tombstone)
        self.key, self.state, self.existing, self.owner = key, state, existing, owner

    @property
    def enquiry_id(self):
        return self.existing.get("enquiry_id") if self.existing else None

    @property
    def response(self):
        return pj(self.existing.get("response_json"), {}) if self.existing else {}


def _new_owner() -> str:
    return secrets.token_hex(12)


def reserve(c, key: str, scope: str, fp: str, is_test: bool) -> Submission:
    now = now_iso()
    owner = _new_owner()
    with transaction(c):
        cur = c.execute("INSERT OR IGNORE INTO enquiry_submissions(submit_key, scope, fingerprint, status, is_test, attempts, created_at, updated_at, owner) VALUES (?,?,?,'pending',?,1,?,?,?)",
                        (key, scope, fp, 1 if is_test else 0, now, now, owner))
        inserted = cur.rowcount == 1
    if inserted:
        return Submission(key, "fresh", owner=owner)
    ex = row(c.execute("SELECT * FROM enquiry_submissions WHERE submit_key=?", (key,)))
    if not ex:  # deleted between the INSERT and the SELECT (a release() elsewhere): try once more
        return reserve(c, key, scope, fp, is_test)
    if ex["scope"] != scope:
        return Submission(key, "foreign")  # a key is never shared between the public forms and a staff sign-in: nothing of the other scope is shown
    if ex["status"] == "deleted":
        return Submission(key, "deleted")  # tombstone: the enquiry this key made was removed; it is never recreated
    if ex["status"] == "done":
        with transaction(c):
            c.execute("UPDATE enquiry_submissions SET attempts=attempts+1, updated_at=? WHERE submit_key=?", (now, key))
        return Submission(key, "replay" if ex["fingerprint"] == fp else "conflict", ex)
    if ex["status"] == "saved" or ex.get("enquiry_id"):
        # the enquiry is committed; whatever came after (side effects, the reply) did not reach the form. Finish it now,
        # with the same answers only (different answers would otherwise look like an overwrite).
        with transaction(c):
            c.execute("UPDATE enquiry_submissions SET attempts=attempts+1, updated_at=? WHERE submit_key=?", (now, key))
        return Submission(key, "saved" if ex["fingerprint"] == fp else "conflict", ex)
    # pending: someone else is working on it right now, or the earlier owner died before writing the enquiry.
    # Takeover is fenced: the row gets a new owner token, so the earlier request can no longer mark it saved (Superseded),
    # and enquiries.submission_key UNIQUE stops a second row even if that request is already past the check.
    stale_before = (datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)).replace(microsecond=0).isoformat()
    with transaction(c):
        took = c.execute("UPDATE enquiry_submissions SET fingerprint=?, is_test=?, attempts=attempts+1, updated_at=?, owner=? "
                         "WHERE submit_key=? AND status='pending' AND enquiry_id IS NULL AND updated_at<?",
                         (fp, 1 if is_test else 0, now, owner, key, stale_before))
        taken = took.rowcount == 1
    if taken:
        return Submission(key, "fresh", owner=owner)
    return Submission(key, "in_progress", ex)


def mark_saved(c, sub: Submission, eid: int) -> None:
    """Called by create_enquiry() through on_inserted: runs inside the enquiry's transaction (no commit here). Fenced on the
    owner token: if a later retry took the reservation over while this request stalled, nothing matches and Superseded is
    raised, which rolls the whole enquiry transaction back (create_enquiry's transaction block)."""
    cur = c.execute("UPDATE enquiry_submissions SET status='saved', enquiry_id=?, updated_at=? WHERE submit_key=? AND status='pending' AND owner=?",
                    (eid, now_iso(), sub.key, sub.owner))
    if cur.rowcount != 1:
        raise Superseded(sub.key)


def finish(c, key: str, eid, response: dict) -> None:
    with transaction(c):
        c.execute("UPDATE enquiry_submissions SET status='done', response_json=?, updated_at=? WHERE submit_key=? AND enquiry_id=?", (j(response), now_iso(), key, eid))


def keep_partial(c, key: str, eid, response: dict) -> None:
    """The enquiry is saved but a follow-up step failed: keep the reply the form got, stay `saved` so a retry finishes the rest."""
    with transaction(c):
        c.execute("UPDATE enquiry_submissions SET status='saved', response_json=?, updated_at=? WHERE submit_key=? AND enquiry_id=?", (j(response), now_iso(), key, eid))


def release(c, sub: Submission) -> None:
    """Nothing was written for this key: let the same key try again (after a validation error, for example). Only this
    request's own pending reservation is removed, never one a later retry has taken over."""
    with transaction(c):
        c.execute("DELETE FROM enquiry_submissions WHERE submit_key=? AND status='pending' AND enquiry_id IS NULL AND owner=?", (sub.key, sub.owner))


HOMEBOT_SCOPE = "homebot"  # HomeBot apartment enquiries (server/homebot.py) share this engine but never this scope's ids


def tombstone(c, enquiry_ids=None, *, homebot: bool = False) -> int:
    """The enquiry behind a key was deleted (staff delete, T5 retention purge, test-data clear): clear everything the row
    kept about it (the reply with the first name and share link, the answer hash, the id) and keep only an opaque
    tombstone, so a retry with that key is refused rather than recreating a deleted enquiry. With no ids, every row whose
    enquiry no longer exists is tombstoned (a sweep for bulk deletes). Scope-aware: MateBot ids and HomeBot ids are
    separate number spaces, so a MateBot delete only touches MateBot-scope keys and a HomeBot delete (homebot=True) only
    HomeBot-scope keys. Runs inside the caller's transaction; no commit here."""
    scope_sql = "scope=?" if homebot else "scope<>?"
    table = "homebot_enquiries" if homebot else "enquiries"
    if enquiry_ids is not None:
        n = 0
        for eid in enquiry_ids:
            n += c.execute("UPDATE enquiry_submissions SET status='deleted', enquiry_id=NULL, response_json=NULL, fingerprint='', owner=NULL, updated_at=? "
                           f"WHERE enquiry_id=? AND {scope_sql}", (now_iso(), eid, HOMEBOT_SCOPE)).rowcount
        return n
    return c.execute("UPDATE enquiry_submissions SET status='deleted', enquiry_id=NULL, response_json=NULL, fingerprint='', owner=NULL, updated_at=? "
                     f"WHERE enquiry_id IS NOT NULL AND {scope_sql} AND enquiry_id NOT IN (SELECT id FROM {table})", (now_iso(), HOMEBOT_SCOPE)).rowcount


def reference(eid) -> str:
    return f"MateBot-{int(eid):05d}"  # P5


def already_submitted_message(eid) -> str:
    return (f"This enquiry has already been submitted (reference {reference(eid)}). The later changes were not saved and no second "
            "enquiry was made. If something needs changing, please contact the team and quote your reference.")


GENERIC_USED_MESSAGE = "This submission key has already been used. Please start a new enquiry."
DELETED_MESSAGE = "The enquiry sent earlier from this form is no longer on file, so it cannot be sent again. Please start a new enquiry if you still need one."
