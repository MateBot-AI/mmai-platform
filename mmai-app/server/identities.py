"""Server-issued opaque identities for the real people on a MateBot enquiry (GP13 build, 9 Nov 2026).

Why: GP13 needs "the same person" to be a stable notion that survives harmless row rewrites (a birth-country confirmation, an
introduction detail) and resets on a real identity edit, is distinct for two companions who share an email (no uniqueness is
imposed on emails), and detects the GP9(e) lead takeover (the enquiry row keeps its id but names a different human).
Positional keys, hashes of the enquiry id or hashes of (enquiry id, email) all fail one of those; so the server issues an
opaque uid per person and keeps it in `people_identities`:
  * one identity per party_members ROW (role 'member', party_member_id set) and one per enquiry for the lead (role 'lead');
  * `name_norm` / `email_norm` are kept ONLY to detect an identity edit; they are personal data and retention deletes the rows;
  * a retired identity keeps its uid (historical membership versions refer to it) until the person is forgotten.

`reconcile(c, eid, reason, before=None)` is deterministic and idempotent and runs INSIDE the caller's retention guard (it raises
otherwise): it reads the enquiry row and its party rows and brings the table in line. Every GP13 read (lookup, respond,
current state) and every membership hash calls it first, so a mutator that forgot to call it can never let a stale identity
through. Explicit hooks exist for the two cases that must not be inferred: `promote_to_lead` (GP9(e): the caller names the
actual member row that takes over the form) and `member_left` (bar / leave: the caller names the row before deleting it).

Conservative by design (root ruling, 9 Nov): a changed name or email on a row is a new identity (so a typo fix resets that
person's group approval, and - through the membership hash - everyone else's on that group); a row that disappears and
re-appears is re-attached ONLY when the pre-write capture maps it unambiguously (exactly one old identity and exactly one new row
carry the same normalised name + email); any ambiguity retires the old identity instead of guessing. Nothing is ever inferred
from another enquiry's identities.
"""
from __future__ import annotations

import secrets
from typing import Optional

from .db import RetentionGuardError, guarded, now_iso, row, rows

RETIRED_REASONS = frozenset(("left", "barred", "identity_edit", "enquiry_deleted"))
LEAD = "lead"
MEMBER = "member"


def norm(v) -> str:
    return " ".join(str(v or "").split()).strip().lower()


def new_uid() -> str:
    return secrets.token_hex(16)


def _require_guard(c, what: str) -> None:
    if not guarded(c):
        raise RetentionGuardError(f"{what} outside retention guard")


def _lead_pair(e: dict) -> tuple[str, str]:
    return norm(f"{e.get('first_name') or ''} {e.get('last_name') or ''}"), norm(e.get("email"))


def _member_pair(m: dict) -> tuple[str, str]:
    return norm(m.get("name")), norm(m.get("email"))


def active(c, eid: int) -> list[dict]:
    """Active identities of an enquiry (lead first, then members in row order). Read-only; no guard needed."""
    return rows(c.execute("""SELECT * FROM people_identities WHERE enquiry_id=? AND retired_at IS NULL
                             ORDER BY CASE role WHEN 'lead' THEN 0 ELSE 1 END, party_member_id, id""", (eid,)))


def capture(c, eid: int) -> dict:
    """Pre-write snapshot for a caller that is about to rewrite party rows (delete + re-insert): {party_member_id: identity row}
    for the active member identities. Pass it to `reconcile(..., before=snapshot)` after the rewrite so rows that come back
    with the same person (same normalised name + email, unambiguous) keep their uid."""
    return {r["party_member_id"]: r for r in active(c, eid) if r["role"] == MEMBER}


def _issue(c, eid: int, role: str, pmid: Optional[int], pair: tuple[str, str], ts: str, superseding: Optional[dict] = None) -> str:
    uid = new_uid()
    c.execute("""INSERT INTO people_identities(person_uid, enquiry_id, role, party_member_id, name_norm, email_norm, created_at)
                 VALUES (?,?,?,?,?,?,?)""", (uid, eid, role, pmid, pair[0], pair[1], ts))
    if superseding is not None:
        c.execute("UPDATE people_identities SET superseded_by_uid=? WHERE id=?", (uid, superseding["id"]))
    return uid


def _retire(c, ident: dict, reason: str, ts: str) -> None:
    if reason not in RETIRED_REASONS:
        reason = "left"
    c.execute("UPDATE people_identities SET retired_at=?, retired_reason=? WHERE id=? AND retired_at IS NULL", (ts, reason, ident["id"]))


def reconcile(c, eid: int, reason: str = "left", before: Optional[dict] = None, now: Optional[str] = None) -> dict:
    """Bring the identities of one enquiry in line with its rows. Inside the caller's guard. Returns a summary with the active
    uids after the pass and counts of what changed (issued / retired / reattached / reset) - numbers only."""
    _require_guard(c, "identity reconcile")
    ts = now or now_iso()
    out = {"issued": 0, "retired": 0, "reattached": 0, "reset": 0, "changed": False}
    e = row(c.execute("SELECT id, first_name, last_name, email FROM enquiries WHERE id=?", (eid,)))
    if not e:
        # the enquiry is gone (cascade already removed the identities, or is about to): nothing to reconcile
        return {**out, "uids": []}
    members = rows(c.execute("SELECT id, name, email FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,)))
    idents = active(c, eid)
    lead_ident = next((i for i in idents if i["role"] == LEAD), None)
    by_pmid = {i["party_member_id"]: i for i in idents if i["role"] == MEMBER}
    present = {m["id"] for m in members}

    # 1. member identities whose row is gone: re-attach through the pre-write capture when unambiguous, otherwise retire
    orphans = [i for pmid, i in by_pmid.items() if pmid not in present]
    new_rows = [m for m in members if m["id"] not in by_pmid]
    if orphans and before:
        # candidates are only identities the caller captured before its rewrite; never another enquiry's, never a guess
        for ident in list(orphans):
            if ident["party_member_id"] not in before:
                continue
            pair = (ident["name_norm"], ident["email_norm"])
            same_old = [o for o in orphans if (o["name_norm"], o["email_norm"]) == pair]
            same_new = [m for m in new_rows if _member_pair(m) == pair]
            if len(same_old) == 1 and len(same_new) == 1:
                m = same_new[0]
                c.execute("UPDATE people_identities SET party_member_id=? WHERE id=?", (m["id"], ident["id"]))
                orphans.remove(ident)
                new_rows.remove(m)
                by_pmid[m["id"]] = {**ident, "party_member_id": m["id"]}
                out["reattached"] += 1
    for ident in orphans:
        _retire(c, ident, reason, ts)
        by_pmid.pop(ident["party_member_id"], None)
        out["retired"] += 1
        out["changed"] = True
    # 2. rows without an identity: issue
    for m in new_rows:
        _issue(c, eid, MEMBER, m["id"], _member_pair(m), ts)
        out["issued"] += 1
        out["changed"] = True
    # 3. identity edits on existing rows: conservative reset (retire + issue a new uid for the same row)
    for m in members:
        ident = by_pmid.get(m["id"])
        if ident is None or m["id"] in {x["id"] for x in new_rows}:
            continue
        pair = _member_pair(m)
        if (ident["name_norm"], ident["email_norm"]) != pair:
            _retire(c, ident, "identity_edit", ts)
            _issue(c, eid, MEMBER, m["id"], pair, ts, superseding=ident)
            out["reset"] += 1
            out["changed"] = True
    # 4. the lead: missing -> issue; name/email changed -> reset (a GP9(e) takeover is announced by promote_to_lead BEFORE this
    #    runs, so by then the lead identity already matches the row and nothing happens here)
    lp = _lead_pair(e)
    if lead_ident is None:
        _issue(c, eid, LEAD, None, lp, ts)
        out["issued"] += 1
        out["changed"] = True
    elif (lead_ident["name_norm"], lead_ident["email_norm"]) != lp:
        _retire(c, lead_ident, "identity_edit", ts)
        _issue(c, eid, LEAD, None, lp, ts, superseding=lead_ident)
        out["reset"] += 1
        out["changed"] = True
    out["uids"] = [i["person_uid"] for i in active(c, eid)]
    return out


def promote_to_lead(c, eid: int, member_id: int, old_lead_reason: str = "barred", now: Optional[str] = None) -> Optional[str]:
    """GP9(e) takeover: the caller names the ACTUAL party_members row whose person takes over the enquiry (the row is about to be
    deleted and the enquiry row rewritten with that person's name and email). The member's identity becomes the lead identity
    (same human, same uid); the old lead identity is retired. Call BEFORE the row delete / enquiry rewrite, inside the guard.
    Returns the promoted uid, or None when the member row has no identity yet (then the lead is simply re-issued by reconcile)."""
    _require_guard(c, "lead promotion")
    ts = now or now_iso()
    idents = active(c, eid)
    old_lead = next((i for i in idents if i["role"] == LEAD), None)
    taker = next((i for i in idents if i["role"] == MEMBER and i["party_member_id"] == int(member_id)), None)
    if old_lead is not None:
        _retire(c, old_lead, old_lead_reason, ts)
    if taker is None:
        return None
    c.execute("UPDATE people_identities SET role='lead', party_member_id=NULL WHERE id=?", (taker["id"],))
    return taker["person_uid"]


def member_left(c, eid: int, member_id: int, reason: str = "left", now: Optional[str] = None) -> Optional[str]:
    """A named party member leaves (barred, or leaves the party). Call BEFORE deleting the row, inside the guard; returns the
    retired uid (or None when the row had no identity). `reconcile` afterwards is then a no-op for that row."""
    _require_guard(c, "member retire")
    ts = now or now_iso()
    ident = row(c.execute("SELECT * FROM people_identities WHERE enquiry_id=? AND party_member_id=? AND retired_at IS NULL", (eid, int(member_id))))
    if not ident:
        return None
    _retire(c, ident, reason, ts)
    return ident["person_uid"]


def display_names(c, eid: int) -> dict:
    """{person_uid: display name} for the active identities of one enquiry, read from the rows (never stored in the identity
    table beyond the normalised comparison values). First names only for members' display, as the staff page shows them."""
    e = row(c.execute("SELECT first_name, last_name FROM enquiries WHERE id=?", (eid,)))
    out = {}
    if not e:
        return out
    for i in active(c, eid):
        if i["role"] == LEAD:
            out[i["person_uid"]] = f"{e.get('first_name') or ''} {e.get('last_name') or ''}".strip() or "Enquirer"
        else:
            m = row(c.execute("SELECT name FROM party_members WHERE id=?", (i["party_member_id"],)))
            out[i["person_uid"]] = (m or {}).get("name") or "Companion"
    return out


def forget(c, enquiry_ids: list[int]) -> int:
    """Retention: delete every identity row (active or retired) of these people. The cascade would do it too; explicit so it does
    not depend on FK enforcement. Returns the row count."""
    ids = [int(i) for i in enquiry_ids]
    if not ids:
        return 0
    q = ",".join("?" * len(ids))
    return c.execute(f"DELETE FROM people_identities WHERE enquiry_id IN ({q})", ids).rowcount
