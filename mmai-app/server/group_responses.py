"""GP13 — individual group responses (build 9 Nov 2026; root Revision 2 + corrections).

Rule basis: GP13 (each person in a proposed group responds through an individual link; a membership change after acceptance
means everyone approves the revised group again; 24 elapsed hours from the group introduction without a response → staff review
of a possible replacement, never automatic removal or rejection; silence is neither acceptance nor a GP9 pairing), GP9(o), H17
(no automated customer messaging), H29, T5, A12 (a share is a proposal, never an agreed amount).

What this module keeps (schema in db.py):
  * group_membership_versions — the immutable set of PEOPLE (server-issued person_uids, see identities.py) a group had; exactly
    one current version per group (partial unique index); `introduced_at` is the 24 h anchor and belongs to THAT version only:
    it is set by the staff Introduced action (or the explicit "this revised group has been introduced" action), never by a
    draft, never automatically;
  * group_response_links — one ACTIVE link per person per version (partial unique index); the clear token is shown to staff ONCE
    when minted and only its digest is stored; rotation inserts a new row and revokes the old one (append-only); expiry and
    revocation are security only - never a closure, decline or GP9 effect;
  * group_responses — append-only history; the current response of a person on a version is the latest row without
    superseded_at; a new version supersedes every response of the old one.

What this module derives, never stores: `all_current_members_accepted` (every person in the CURRENT version has a current
`accept` on THAT version). Responses on earlier versions are history only and are never shown as approval.

Every write and every token check runs inside a retention guard (the DB lock shared with every writer) and starts by
reconciling the identities of the enquiries involved, so a stale identity or a membership change that a mutator forgot to
announce can never let an old link see or approve anything: lookup/respond verify, in this order, digest → not revoked / not
expired → version is current → group status allows responses → identity still active and in the current members → only then
any group detail. Nothing here sends anything to a customer; declines, change requests and the 24 h overdue state raise internal
staff alerts only (alerts.py). Nothing here changes a group's status.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import alerts, identities
from .db import RetentionGuardError, audit, guarded, j, now_iso, pj, retention_guard, row, rows
from .security import token_hash

log = logging.getLogger("mmai.group_responses")

LINK_DAYS = 30                    # security expiry of a link; expiry never closes or rejects anything (staff mint a fresh one)
REVIEW_HOURS = 24                 # GP13: elapsed hours from the version's introduction before silence goes to staff review
NOTE_MAX = 1000
RESPONSES = ("accept", "decline", "change_request")
RESPONSE_LABEL = {"accept": "Yes, happy to share with this group", "decline": "No", "change_request": "Asked for something to change"}
LIVE_STATUSES = ("Proposed", "Introduced", "Group confirmed", "Awaiting replacement")
RESPOND_STATUSES = ("Introduced", "Group confirmed")   # the statuses in which a link shows anything or takes a response ...
AWAITING = "Awaiting replacement"                       # ... plus this one ONLY when its current version carries the explicit
                                                        # remaining-group anchor (GP10(b), see anchor_remaining_group); matching.AWAITING
ANCHOR_INTRODUCTION = "introduction"                    # closed anchor kinds: how the current version's 24 h clock was anchored
ANCHOR_REMAINING_GROUP = "remaining_group"
ANCHOR_KINDS = (ANCHOR_INTRODUCTION, ANCHOR_REMAINING_GROUP)
INVALID = "This link is no longer valid. Ask the ALM team for a fresh link."
MEANING = ("You are saying whether you are happy to share this apartment with these housemates. "
           "The rent and your share shown here are a proposal from ALM, not an agreed amount; nothing is agreed until the lease.")
APARTMENT_CHANGED = ("Apartment details have changed. Please contact the ALM team about the current apartment options. "
                     "Your response here is about these housemates, not an agreement to rent.")   # stock lifecycle (D4): fixed factual text
MEANING_REMAINING = ("You are saying whether you are happy to share this apartment with these housemates while one space stays empty "
                     "until another person is found. You are not agreeing to the rent, to any share, or to covering the empty space: "
                     "the amounts shown are a proposal from ALM, not an agreed amount; nothing is agreed until the lease.")
NOT_AWAITING = "Only a group that is awaiting a replacement can have the remaining group and the empty space recorded this way."
STALE_REMAINING = "The group or its details changed since the page was loaded. Reload the page and check it again."
EV_DECLINED = "group_response_declined"
EV_CHANGE = "group_response_change_requested"
EV_OVERDUE = "group_response_overdue"
SWEEP_EVERY_S = 3600
_sweep_lock = threading.Lock()
_last_sweep: Optional[datetime] = None


class LinkInvalid(Exception):
    """The fixed, detail-free refusal: the caller answers 410 with INVALID."""


def _require_guard(c, what: str) -> None:
    if not guarded(c):
        raise RetentionGuardError(f"{what} outside retention guard")


def _dt(s: str) -> datetime:
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _plus(iso: str, **kw) -> str:
    return (_dt(iso) + timedelta(**kw)).replace(microsecond=0).isoformat()


def membership_hash(members: list[dict]) -> str:
    return hashlib.sha256(json.dumps(sorted(m["person_uid"] for m in members)).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------------ versions
def _member_enquiries(c, gid: int) -> list[int]:
    return [r["enquiry_id"] for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (gid,)))]


def _members_now(c, gid: int, reason: str = "left") -> list[dict]:
    """Reconcile every member enquiry (inside the guard) and return the people of the group as they are NOW:
    [{person_uid, enquiry_id, role, display_name}] sorted by uid."""
    _require_guard(c, "group membership read")
    out = []
    for eid in _member_enquiries(c, gid):
        identities.reconcile(c, eid, reason)
        names = identities.display_names(c, eid)
        for i in identities.active(c, eid):
            out.append({"person_uid": i["person_uid"], "enquiry_id": eid, "role": i["role"], "display_name": names.get(i["person_uid"], "")})
    return sorted(out, key=lambda m: m["person_uid"])


def current_version(c, gid: int) -> Optional[dict]:
    v = row(c.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    if v:
        v["members"] = pj(v.pop("members_json"), []) or []
    return v


def _supersede(c, v: dict, ts: str, reason: str) -> None:
    c.execute("UPDATE group_membership_versions SET superseded_at=? WHERE id=? AND superseded_at IS NULL", (ts, v["id"]))
    c.execute("UPDATE group_response_links SET revoked_at=?, revoked_reason=? WHERE version_id=? AND revoked_at IS NULL", (ts, reason, v["id"]))
    c.execute("UPDATE group_responses SET superseded_at=? WHERE version_id=? AND superseded_at IS NULL", (ts, v["id"]))


def on_membership_changed(c, gid: int, reason: str, staff: Optional[str] = None, now: Optional[str] = None) -> dict:
    """Hook for every group-membership mutator (lead's matching.py call sites, the enquiry identity hooks in enquiries.py, and
    this module's own reads). Inside the caller's guard. Reconciles the members' identities, hashes the people, and:
    unchanged → no-op; changed → supersedes the current version (revokes its links, supersedes its responses - nothing deleted)
    and writes a new, UN-introduced version (fresh approvals from everyone, GP13). A dissolved group only closes its version.
    Returns {'changed', 'version'} (the current version row or None)."""
    _require_guard(c, "membership version")
    ts = now or now_iso()
    g = row(c.execute("SELECT id, status FROM groups WHERE id=?", (gid,)))
    if not g:
        return {"changed": False, "version": None}
    cur = current_version(c, gid)
    if g["status"] in ("Dissolved",):
        if cur:
            _supersede(c, cur, ts, "dissolved")
            return {"changed": True, "version": None}
        return {"changed": False, "version": None}
    members = _members_now(c, gid, "left" if reason != "barred" else "barred")
    h = membership_hash(members)
    if cur and cur["membership_hash"] == h:
        return {"changed": False, "version": cur}
    if cur:
        _supersede(c, cur, ts, "membership_changed")
    n = (row(c.execute("SELECT MAX(version) AS v FROM group_membership_versions WHERE group_id=?", (gid,))) or {}).get("v") or 0
    c.execute("""INSERT INTO group_membership_versions(group_id, version, membership_hash, members_json, reason, created_at, created_by)
                 VALUES (?,?,?,?,?,?,?)""", (gid, int(n) + 1, h, j(members), str(reason or "edit")[:40], ts, staff))
    audit(c, staff, "group_membership_version", "group", gid, j({"version": int(n) + 1, "people": len(members), "reason": str(reason or "edit")[:40]}))
    return {"changed": True, "version": current_version(c, gid)}


def anchor_introduction(c, gid: int, staff: str, now: Optional[str] = None) -> dict:
    """Set the 24 h anchor on the CURRENT version (once). Inside the caller's guard, AFTER the caller's readiness checks (ready to
    circulate, current introduction draft named by the page) - this function performs none of them and never mints links.
    An already anchored current version is left alone. Returns the current version."""
    _require_guard(c, "introduction anchor")
    ts = now or now_iso()
    on_membership_changed(c, gid, "introduced", staff, now=ts)   # the version the clock is anchored on is the people as they are NOW
    v = current_version(c, gid)
    if v and not v.get("introduced_at"):
        c.execute("UPDATE group_membership_versions SET introduced_at=?, introduced_by=?, anchor_kind=? WHERE id=? AND introduced_at IS NULL",
                  (ts, staff, ANCHOR_INTRODUCTION, v["id"]))
        audit(c, staff, "group_introduced_version", "group", gid, j({"version": v["version"]}))
        v = current_version(c, gid)
    return v


def respond_allowed(status: Optional[str], v: Optional[dict]) -> bool:
    """Whether links may be minted / shown / answered for a group in this status with this CURRENT version:
    Introduced or Group confirmed (the version still needs its anchor for anything to be shown), or Awaiting replacement ONLY when the
    current version carries the explicit remaining-group anchor (GP10(b)). An ordinary introduction anchor never authorises the
    awaiting exception; a bare introduced_at is not enough."""
    if status in RESPOND_STATUSES:
        return True
    if status == AWAITING:
        return bool(v and v.get("introduced_at") and v.get("anchor_kind") == ANCHOR_REMAINING_GROUP)
    return False


def _remaining_anchored(status: Optional[str], v: Optional[dict]) -> bool:
    return status == AWAITING and respond_allowed(status, v)


def _empty_space_count(g: dict) -> int:
    return empty_space_count(pj(g.get("allocation_json"), {}) or {})


def empty_space_count(allocation) -> int:
    """Empty beds recorded in a stored bed allocation; a malformed allocation counts none (the uncovered figure is then unknown)."""
    rooms = allocation.get("rooms") if isinstance(allocation, dict) else None
    return sum(int(r.get("empty_beds") or 0) for r in (rooms if isinstance(rooms, list) else []) if isinstance(r, dict))


def uncovered_weekly(total_weekly, allocation, people_count: Optional[int]) -> Optional[float]:
    """GP10(d): the part of the whole weekly rent that the REMAINING people's actual stored shares do not cover (B5: the group is
    liable for the whole rent; shares stay the guide from before the removal). Covered = the sum of every occupant's stored share
    over the rooms of the group's bed allocation - lead enquirers AND companions, each person's own share, never group_members alone
    (that table holds lead rows only, so it would count a lead's own share as if it were the whole party's).
    None = unknown, shown as such, whenever the figure cannot be read honestly: no / malformed allocation, an occupant without a
    numeric share, or an occupant count that does not match the remaining people - nothing is invented (no zero, no other person's
    contribution, no division guesses)."""
    total = _money_amount(total_weekly)
    if total is None:
        return None
    if not isinstance(allocation, dict) or not isinstance(allocation.get("rooms"), list):
        return None
    covered, seen = 0.0, 0
    for r in allocation["rooms"]:
        if not isinstance(r, dict) or not isinstance(r.get("occupants"), list):
            return None
        for o in r["occupants"]:
            share = _money_amount(o.get("share")) if isinstance(o, dict) else None
            if share is None:
                return None
            covered += share
            if not math.isfinite(covered):          # an overflowing sum is not a figure either
                return None
            seen += 1
    if isinstance(people_count, bool) or not isinstance(people_count, int) or seen != people_count:
        return None
    left = total - covered
    if not math.isfinite(left):
        return None
    return round(max(0.0, left), 2)


def _money_amount(v) -> Optional[float]:
    """A stored money amount usable in arithmetic: a real, finite, non-negative number (not a bool, not a string, not NaN/inf);
    otherwise None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _uncovered_weekly(g: dict, v: Optional[dict]) -> Optional[float]:
    """The staff / respond figure for one group: its stored allocation against the current membership version's people."""
    return uncovered_weekly(g.get("total_weekly"), pj(g.get("allocation_json"), {}) or {}, len(v["members"]) if v else None)


def remaining_group_context(c, gid: int) -> Optional[dict]:
    """GP10(b): the FULL context staff must have explained to everyone before the remaining group can answer through their links -
    apartment identity, lease dates, total rent, the actual remaining people (lead enquirers AND companions) with their provable
    proposed shares, the number of empty spaces, the uncovered amount and the discount note - and its digest. Inside the caller's
    guard, after on_membership_changed, so the people are the current version's. None when the group does not exist."""
    _require_guard(c, "remaining group context")
    g = row(c.execute("SELECT g.*, a.address, a.suburb, a.type_code FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (gid,)))
    if not g:
        return None
    v = current_version(c, gid)
    people = [{"person_uid": m["person_uid"], "enquiry_id": m["enquiry_id"], "role": m["role"], "display_name": m["display_name"],
               "proposed_weekly_share": _own_share(c, g, m)} for m in (v["members"] if v else [])]
    ctx = {"group_id": gid, "apartment_id": g.get("apartment_id"), "address": g.get("address"), "suburb": g.get("suburb"), "type_code": g.get("type_code"),
           "lease_start": g.get("lease_start"), "lease_end": g.get("lease_end"), "total_weekly": g.get("total_weekly"),
           "people": people, "empty_space_count": _empty_space_count(g), "uncovered_weekly": _uncovered_weekly(g, v),
           "discount_note": g.get("discount_note") or None, "membership_version": v["version"] if v else None}
    return {"context": ctx, "membership_hash": v["membership_hash"] if v else None,
            "disclosure_hash": hashlib.sha256(json.dumps(ctx, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()}


def anchor_remaining_group(c, gid: int, staff: str, token: dict, now: Optional[str] = None) -> dict:
    """GP10(b), staff-led: record that the REMAINING group and the empty space were explained to everyone, anchoring the 24 h clock of
    the current membership version with the remaining-group anchor kind. Inside the caller's guard. Refused (ValueError, plain
    sentence) unless the group is awaiting a replacement, the current version is not yet anchored, and the token names exactly the
    membership and the full disclosed context as they are NOW (both digests recomputed and compared under this guard). Changes no
    status, mints no link, sends nothing; a later membership change supersedes the anchor like any other version."""
    _require_guard(c, "remaining group anchor")
    ts = now or now_iso()
    g = row(c.execute("SELECT id, status FROM groups WHERE id=?", (gid,)))
    if not g:
        raise KeyError("Group not found")
    if g["status"] != AWAITING:
        raise ValueError(NOT_AWAITING)
    on_membership_changed(c, gid, "remaining_group", staff, now=ts)
    v = current_version(c, gid)
    if not v or not v["members"]:
        raise ValueError("Nobody is left in this group to ask.")
    if v.get("introduced_at"):
        raise ValueError("The remaining group has already been recorded as explained to everyone; make or rotate the links instead.")
    ctx = remaining_group_context(c, gid)
    want = {"membership_hash": str((token or {}).get("membership_hash") or ""), "disclosure_hash": str((token or {}).get("disclosure_hash") or "")}
    if want["membership_hash"] != ctx["membership_hash"] or want["disclosure_hash"] != ctx["disclosure_hash"]:
        raise ValueError(STALE_REMAINING)
    c.execute("""UPDATE group_membership_versions SET introduced_at=?, introduced_by=?, anchor_kind=?, disclosure_hash=?
                 WHERE id=? AND introduced_at IS NULL AND superseded_at IS NULL""", (ts, staff, ANCHOR_REMAINING_GROUP, ctx["disclosure_hash"], v["id"]))
    audit(c, staff, "group_remaining_introduced", "group", gid, j({"version": v["version"], "people": len(v["members"])}))
    return current_version(c, gid)


def needs_reintroduction(c, gid: int) -> bool:
    """True when the group's status already says Introduced (or confirmed) but the CURRENT membership version has no anchor:
    the membership changed since the last introduction and the revised group has not been introduced yet."""
    g = row(c.execute("SELECT status FROM groups WHERE id=?", (gid,)))
    v = current_version(c, gid)
    return bool(g and g["status"] in RESPOND_STATUSES and (v is None or not v.get("introduced_at")))


# ------------------------------------------------------------------------------------------------------ links
def _group_is_test(c, gid: int) -> bool:
    r = row(c.execute("SELECT MAX(e.is_test) AS t FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=?", (gid,)))
    return bool(r and r["t"])


def mint_link(c, gid: int, person_uid: str, staff: str, now: Optional[str] = None) -> dict:
    """Staff mint (or rotate) ONE person's link for the current, introduced version. Inside the caller's guard. The clear token is
    returned once and never stored, logged or written into any text; any earlier active link of that person on this version is
    revoked ('rotated') - its response history stays. Raises ValueError with a plain sentence when it cannot be done."""
    _require_guard(c, "link mint")
    ts = now or now_iso()
    g = row(c.execute("SELECT id, status FROM groups WHERE id=?", (gid,)))
    if not g:
        raise KeyError("Group not found")
    if g["status"] not in RESPOND_STATUSES and g["status"] != AWAITING:
        raise ValueError("Links can only be made for a group that has been introduced.")
    on_membership_changed(c, gid, "link", staff, now=ts)
    v = current_version(c, gid)
    if g["status"] == AWAITING and not respond_allowed(g["status"], v):
        raise ValueError("This group is awaiting a replacement. Record that the remaining group and the empty space were explained to "
                         "everyone first, then make the links.")
    if not v or not v.get("introduced_at"):
        raise ValueError("This group's membership changed since it was introduced. Mark the revised group introduced first, then make the links.")
    m = next((x for x in v["members"] if x["person_uid"] == person_uid), None)
    if not m:
        raise ValueError("That person is not in the current group.")
    c.execute("UPDATE group_response_links SET revoked_at=?, revoked_reason='rotated' WHERE version_id=? AND person_uid=? AND revoked_at IS NULL",
              (ts, v["id"], person_uid))
    token = secrets.token_urlsafe(24)
    c.execute("""INSERT INTO group_response_links(version_id, group_id, enquiry_id, person_uid, token_hash, is_test, created_at, created_by, expires_at)
                 VALUES (?,?,?,?,?,?,?,?,?)""", (v["id"], gid, m["enquiry_id"], person_uid, token_hash(token), 1 if _group_is_test(c, gid) else 0, ts, staff,
                                                _plus(ts, days=LINK_DAYS)))
    lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    audit(c, staff, "group_response_link", "group", gid, j({"version": v["version"], "link_id": lid}))   # ids only, never the token
    return {"link_id": lid, "person_uid": person_uid, "display_name": m["display_name"], "expires_at": _plus(ts, days=LINK_DAYS),
            "token": token, "path": "#/respond?t=" + token}


# ------------------------------------------------------------------------------------------------------ public side
def _verify(c, token: str, now: str) -> tuple[dict, dict, dict, dict]:
    """Inside the guard. Digest → link live → version current → status allows → identity active and in the version. Any failure is
    LinkInvalid (one fixed sentence, no detail). Returns (link, version, group, member entry)."""
    _require_guard(c, "link verify")
    if not isinstance(token, str) or not (16 <= len(token) <= 128):
        raise LinkInvalid()
    link = row(c.execute("SELECT * FROM group_response_links WHERE token_hash=?", (token_hash(token),)))
    if not link or link.get("revoked_at") or (link.get("expires_at") or "") <= now:
        raise LinkInvalid()
    v = row(c.execute("SELECT * FROM group_membership_versions WHERE id=?", (link["version_id"],)))
    if not v or v.get("superseded_at") or not v.get("introduced_at"):
        raise LinkInvalid()
    g = row(c.execute("SELECT g.*, a.address, a.suburb, a.type_code FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (link["group_id"],)))
    if not g or not respond_allowed(g["status"], v):
        raise LinkInvalid()
    # the people as they are NOW (identities reconciled under this guard): a change a mutator forgot to announce is caught here
    res = on_membership_changed(c, g["id"], "lookup", None, now=now)
    if res["changed"] or not res["version"] or res["version"]["id"] != v["id"]:
        raise LinkInvalid()
    v = res["version"]
    if not respond_allowed(g["status"], v):
        raise LinkInvalid()
    m = next((x for x in v["members"] if x["person_uid"] == link["person_uid"]), None)
    if not m:
        raise LinkInvalid()
    ident = row(c.execute("SELECT retired_at FROM people_identities WHERE person_uid=?", (link["person_uid"],)))
    if not ident or ident["retired_at"]:
        raise LinkInvalid()
    return link, v, g, m


def _current_response(c, version_id: int, person_uid: str) -> Optional[dict]:
    r = row(c.execute("SELECT id, response, note, responded_at FROM group_responses WHERE version_id=? AND person_uid=? AND superseded_at IS NULL ORDER BY id DESC LIMIT 1",
                      (version_id, person_uid)))
    return r


def _own_share(c, g: dict, m: dict) -> Optional[float]:
    """THIS person's proposed weekly share, only where the binding allocation -> person is PROVABLE; otherwise None (the page then
    says the share is not set yet - never another person's figure).
    - The lead enquirer: group_members holds exactly their row, keyed by enquiry_id (matching._write_members writes lead rows only).
    - A companion: the bed allocation (groups.allocation_json) names occupants positionally ("<enquiry id>m<k>", the k-th party row
      at the time of the run); a position is not proof after party rows are rewritten or reordered, so a companion's share is read
      ONLY from an occupant that carries this person's own person_uid (stamped by matching when it writes the allocation; until the
      lead's matching module does so, companions see "not set yet")."""
    ident = row(c.execute("SELECT role FROM people_identities WHERE person_uid=?", (m["person_uid"],)))
    if not ident:
        return None
    if ident["role"] == identities.LEAD:
        r = row(c.execute("SELECT weekly_share FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], m["enquiry_id"])))
        return r["weekly_share"] if r else None
    alloc = pj(g.get("allocation_json"), {}) or {}
    rooms = alloc.get("rooms") if isinstance(alloc, dict) else None
    for r in (rooms if isinstance(rooms, list) else []):
        for o in (r.get("occupants") or []) if isinstance(r, dict) else []:
            if isinstance(o, dict) and o.get("person_uid") and o.get("person_uid") == m["person_uid"]:
                return _money_amount(o.get("share"))
    return None


def _offer_withheld(c, g: dict) -> bool:
    """Read-time decision (succ3, L14): the public page withholds the apartment block when the group has no current offer, when the
    offer-change flag is set (cleared only by staff acknowledgement), when follow-up work for a stock transition of the CURRENT
    apartment is still pending (transaction 1 committed, the group's own step not yet - e.g. after an enqueue or outbox failure), or
    when the current apartment itself is no longer listed / is leased elsewhere. Not merely the flag a successful step writes."""
    aid = g.get("apartment_id")
    if aid is None or g.get("offer_changed_since_intro_at"):
        return True
    if row(c.execute("""SELECT 1 FROM offer_reconcile_work w JOIN stock_transitions t ON t.id=w.transition_id
                        WHERE w.group_id=? AND w.status='pending' AND t.apartment_id=? LIMIT 1""", (g["id"], int(aid)))):
        return True
    a = row(c.execute("SELECT available_from, leased_at, leased_group_id FROM apartments WHERE id=?", (int(aid),)))
    if not a or not a.get("available_from"):
        return True
    if a.get("leased_at") and (a.get("leased_group_id") is None or int(a["leased_group_id"]) != int(g["id"])):
        return True
    return False


def _group_view(c, g: dict, v: dict, m: dict) -> dict:
    """What a person may see: the apartment, the lease dates, the first names of everyone in this version and THEIR OWN proposed
    share (labelled a proposal). No contact details, no answers of other people. Called inside the lookup / respond guard, after
    _verify, so every read is of the verified group as it is in that same guarded step."""
    share = _own_share(c, g, m)
    try:
        from . import rules
        type_label = rules.APT_TYPES[g["type_code"]]["label"]
    except Exception:   # noqa: BLE001
        type_label = g.get("type_code")
    out = {"address": g["address"], "suburb": g.get("suburb"), "type_label": type_label, "lease_start": g.get("lease_start"), "lease_end": g.get("lease_end"),
           "housemates": [x["display_name"].split(" ")[0] for x in v["members"] if x["person_uid"] != m["person_uid"]],
           "your_name": m["display_name"], "proposed_weekly_share": share, "proposed_total_weekly": g.get("total_weekly"),
           "empty_space": False, "apartment_changed": False, "meaning": MEANING}
    if _offer_withheld(c, g):
        # Stock lifecycle (D4): the apartment this version was introduced with has changed, gone or been replaced since the current
        # introduction - or a stock change affecting it is still being re-checked. The page withholds the apartment details rather
        # than silently showing new ones under the old introduction, states that fact in fixed words, and still accepts the
        # response - which is about the housemates (GP13 unchanged).
        out.update({"address": None, "suburb": None, "type_label": None, "lease_start": None, "lease_end": None,
                    "proposed_weekly_share": None, "proposed_total_weekly": None, "apartment_changed": True, "apartment_changed_text": APARTMENT_CHANGED})
    if _remaining_anchored(g.get("status"), v):
        # GP10(b): the page says honestly that a space is empty, what part of the rent that leaves uncovered, and any discount staff
        # recorded; the answer is still about the housemates only
        out.update({"empty_space": True, "empty_space_count": _empty_space_count(g), "uncovered_weekly": _uncovered_weekly(g, v),
                    "discount_note": g.get("discount_note") or None, "meaning": MEANING_REMAINING})
    return out


def lookup(c, token: str, now: Optional[str] = None) -> dict:
    ts = now or now_iso()
    with retention_guard(c, "group response lookup"):
        link, v, g, m = _verify(c, token, ts)
        cur = _current_response(c, v["id"], link["person_uid"])
        view = _group_view(c, g, v, m)
    return {"group": view, "response": cur and {"response": cur["response"], "label": RESPONSE_LABEL[cur["response"]], "note": cur["note"], "responded_at": cur["responded_at"]},
            "choices": [{"value": k, "label": RESPONSE_LABEL[k]} for k in RESPONSES], "is_test": bool(link["is_test"])}


def respond(c, token: str, response: str, note: str = "", now: Optional[str] = None) -> dict:
    """Record one person's response on the current version (append-only; an identical repeat adds nothing). Decline and change
    requests raise a staff alert; nothing else happens automatically (no status change, no GP9 row, no message)."""
    ts = now or now_iso()
    if response not in RESPONSES:
        raise ValueError("Choose one of the answers shown.")
    note = str(note or "").strip()
    if len(note) > NOTE_MAX:
        raise ValueError(f"Please keep your note to {NOTE_MAX} characters.")
    with retention_guard(c, "group response record"):
        link, v, g, m = _verify(c, token, ts)
        cur = _current_response(c, v["id"], link["person_uid"])
        if cur and cur["response"] == response and (cur["note"] or "") == note:
            created = False
        else:
            c.execute("UPDATE group_responses SET superseded_at=? WHERE version_id=? AND person_uid=? AND superseded_at IS NULL", (ts, v["id"], link["person_uid"]))
            c.execute("""INSERT INTO group_responses(link_id, version_id, group_id, enquiry_id, person_uid, response, note, responded_at)
                         VALUES (?,?,?,?,?,?,?,?)""", (link["id"], v["id"], g["id"], link["enquiry_id"], link["person_uid"], response, note or None, ts))
            rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            audit(c, None, "group_response", "group", g["id"], j({"version": v["version"], "response": response}))
            if response == "decline":
                alerts.raise_attention(c, EV_DECLINED, g["id"], is_test=bool(link["is_test"]), revision=rid, in_transaction=True)
            elif response == "change_request":
                alerts.raise_attention(c, EV_CHANGE, g["id"], is_test=bool(link["is_test"]), revision=rid, in_transaction=True)
            created = True
        cur = _current_response(c, v["id"], link["person_uid"])
        view = _group_view(c, g, v, m)
    return {"recorded": created, "response": {"response": cur["response"], "label": RESPONSE_LABEL[cur["response"]], "note": cur["note"], "responded_at": cur["responded_at"]},
            "group": view}


# ------------------------------------------------------------------------------------------------------ staff side
def _deadline(v: dict) -> Optional[str]:
    return _plus(v["introduced_at"], hours=REVIEW_HOURS) if v and v.get("introduced_at") else None


def _people_state(c, v: dict, now: str) -> list[dict]:
    out = []
    deadline = _deadline(v)
    for m in v["members"]:
        cur = _current_response(c, v["id"], m["person_uid"])
        link = row(c.execute("SELECT id, created_at, expires_at FROM group_response_links WHERE version_id=? AND person_uid=? AND revoked_at IS NULL", (v["id"], m["person_uid"])))
        links_made = row(c.execute("SELECT COUNT(*) AS n FROM group_response_links WHERE version_id=? AND person_uid=?", (v["id"], m["person_uid"])))["n"]
        out.append({"person_uid": m["person_uid"], "enquiry_id": m["enquiry_id"], "role": m["role"], "display_name": m["display_name"],
                    "response": cur and {"id": cur["id"], "response": cur["response"], "label": RESPONSE_LABEL[cur["response"]], "note": cur["note"], "responded_at": cur["responded_at"]},
                    "link": link and {"id": link["id"], "created_at": link["created_at"], "expires_at": link["expires_at"], "expired": link["expires_at"] <= now},
                    "links_made": links_made,
                    "overdue": bool(deadline and deadline <= now and not cur)})
    return out


def all_current_members_accepted(c, gid: int) -> bool:
    """Derived: the CURRENT version is introduced and every person in it has a current 'accept' on it. Never from old versions."""
    v = current_version(c, gid)
    if not v or not v.get("introduced_at") or not v["members"]:
        return False
    for m in v["members"]:
        cur = _current_response(c, v["id"], m["person_uid"])
        if not cur or cur["response"] != "accept":
            return False
    return True


def confirmation_block(c, gid: int) -> Optional[str]:
    """Why the group cannot be marked 'Group confirmed' yet (plain sentence), or None when everyone in the current version has
    said yes. Read-only (no reconcile): the status step calls on_membership_changed first, inside its guard."""
    v = current_version(c, gid)
    g = row(c.execute("SELECT status FROM groups WHERE id=?", (gid,)))
    if g and g["status"] == AWAITING and not respond_allowed(g["status"], v):
        return ("This group is awaiting a replacement. Record that the remaining group and the empty space were explained to everyone, "
                "send each person their own link, and everyone must say yes.")
    if not v:
        return "Nobody has been asked to confirm this group yet: mark it introduced and send each person their own link first."
    if not v.get("introduced_at"):
        return "This group's membership changed since it was introduced. Mark the revised group introduced and send each person a new link; everyone must say yes again."
    waiting, no, change = [], [], []
    for m in v["members"]:
        cur = _current_response(c, v["id"], m["person_uid"])
        if not cur:
            waiting.append(m["display_name"])
        elif cur["response"] == "decline":
            no.append(m["display_name"])
        elif cur["response"] == "change_request":
            change.append(m["display_name"])
    if not (waiting or no or change):
        return None
    parts = []
    if waiting:
        parts.append("still to answer: " + ", ".join(waiting))
    if no:
        parts.append("said no: " + ", ".join(no))
    if change:
        parts.append("asked for a change: " + ", ".join(change))
    return "Not everyone has said yes to this group yet (" + "; ".join(parts) + ")."


def state(c, gid: int, staff: Optional[str] = None, now: Optional[str] = None) -> dict:
    """The staff panel's view: current version (people, each one's response and link state, the 24 h clock), the reasons the
    group cannot be confirmed yet, and the earlier versions as history. Reconciles identities under its own guard first."""
    ts = now or now_iso()
    with retention_guard(c, "group response state"):
        g = row(c.execute("SELECT id, status FROM groups WHERE id=?", (gid,)))
        if not g:
            raise KeyError("Group not found")
        on_membership_changed(c, gid, "state", staff, now=ts)
        v = current_version(c, gid)
        people = _people_state(c, v, ts) if v else []
        hist = rows(c.execute("SELECT * FROM group_membership_versions WHERE group_id=? AND superseded_at IS NOT NULL ORDER BY version DESC", (gid,)))
        history = []
        for h in hist:
            members = pj(h.pop("members_json"), []) or []
            acc = row(c.execute("SELECT COUNT(DISTINCT person_uid) AS n FROM group_responses WHERE version_id=? AND response='accept'", (h["id"],)))["n"]
            history.append({"version": h["version"], "created_at": h["created_at"], "superseded_at": h["superseded_at"], "introduced_at": h.get("introduced_at"),
                            "people": [m.get("display_name") or "Former member" for m in members], "accepted_count": acc, "people_count": len(members),
                            "note": "Earlier membership; those answers do not count for the current group."})
        block = confirmation_block(c, gid) if (g["status"] in RESPOND_STATUSES or g["status"] == AWAITING) else None
        remaining = None
        if g["status"] == AWAITING:
            # GP10(b): what staff need to record the explicit remaining-group anchor (both digests are re-checked under the anchor's guard)
            ctx = remaining_group_context(c, gid)
            anchored = _remaining_anchored(g["status"], v)
            remaining = {"applies": True, "anchored": anchored, "membership_hash": ctx["membership_hash"],
                         # the context moved on after it was recorded (e.g. the discount note): shown to staff, resets nobody's answer
                         "disclosure_changed": bool(anchored and (v.get("disclosure_hash") or "") != ctx["disclosure_hash"]),
                         "context": ctx["context"],
                         "disclosure_hash": ctx["disclosure_hash"], "people_count": len(ctx["context"]["people"]),
                         "empty_space_count": ctx["context"]["empty_space_count"], "uncovered_weekly": ctx["context"]["uncovered_weekly"],
                         "discount_note": ctx["context"]["discount_note"], "shares_unproven": [p["display_name"] for p in ctx["context"]["people"] if p["proposed_weekly_share"] is None]}
    deadline = _deadline(v) if v else None
    return {"group_id": gid, "status": g["status"], "applies": respond_allowed(g["status"], v),
            "remaining_group": remaining,
            "version": v and {"id": v["id"], "version": v["version"], "created_at": v["created_at"], "reason": v["reason"], "introduced_at": v.get("introduced_at"),
                              "introduced_by": v.get("introduced_by"), "anchor_kind": v.get("anchor_kind"), "review_due_at": deadline, "review_due": bool(deadline and deadline <= ts)},
            "needs_reintroduction": bool(v and not v.get("introduced_at") and g["status"] in RESPOND_STATUSES),
            "people": people, "all_accepted": all_current_members_accepted(c, gid), "confirmation_block": block,
            "meaning": MEANING_REMAINING if _remaining_anchored(g["status"], v) else MEANING, "history": history}


def mark_reviewed(c, gid: int, response_id: int, staff: str, now: Optional[str] = None) -> dict:
    """Staff acknowledgement of a decline / change request (clears nothing else; the response stays as the person gave it)."""
    ts = now or now_iso()
    with retention_guard(c, "group response reviewed"):
        n = c.execute("UPDATE group_responses SET reviewed_at=?, reviewed_by=? WHERE id=? AND group_id=? AND reviewed_at IS NULL", (ts, staff, int(response_id), gid)).rowcount
        if n != 1:
            raise KeyError("Response not found or already reviewed")
        audit(c, staff, "group_response_reviewed", "group", gid, j({"response_id": int(response_id)}))
    return {"ok": True}


# ------------------------------------------------------------------------------------------------------ 24 h review
def sweep_overdue(c, now: Optional[str] = None) -> int:
    """GP13: every current, introduced version older than REVIEW_HOURS with someone still to answer raises ONE staff alert
    (idempotent per version). Review only: nothing is removed, rejected or re-queued. Returns alerts newly raised."""
    ts = now or now_iso()
    raised = 0
    with retention_guard(c, "group response sweep"):
        due = rows(c.execute("""SELECT v.*, g.status FROM group_membership_versions v JOIN groups g ON g.id=v.group_id
                                WHERE v.superseded_at IS NULL AND v.introduced_at IS NOT NULL AND v.introduced_at <= ? AND g.status IN (?,?,?)""",
                             ((_dt(ts) - timedelta(hours=REVIEW_HOURS)).replace(microsecond=0).isoformat(), *RESPOND_STATUSES, AWAITING)))
        for v in due:
            v["members"] = pj(v.pop("members_json"), []) or []
            if not respond_allowed(v["status"], v):   # an awaiting group only counts under the explicit remaining-group record
                continue
            if all(_current_response(c, v["id"], m["person_uid"]) for m in v["members"]):
                continue
            r = alerts.raise_attention(c, EV_OVERDUE, v["group_id"], is_test=_group_is_test(c, v["group_id"]), revision=v["id"], in_transaction=True)
            raised += 1 if r.get("created") else 0
    return raised


def sweep_if_due(c, now: Optional[str] = None, background: bool = True) -> bool:
    """Called from the app's request-time wake-up hook (like scheduler.catch_up_if_overdue): at most once an hour per process, and in a
    daemon thread so the request path only pays one in-memory comparison. Failures are logged by exception type only."""
    global _last_sweep
    t = _dt(now or now_iso())
    with _sweep_lock:
        if _last_sweep is not None and (t - _last_sweep).total_seconds() < SWEEP_EVERY_S:
            return False
        _last_sweep = t

    def _run():
        try:
            sweep_overdue(c, now)
        except Exception as ex:   # noqa: BLE001
            log.warning("group response sweep skipped kind=%s", type(ex).__name__)
    if background:
        threading.Thread(target=_run, name="mmai-group-response-sweep", daemon=True).start()
    else:
        _run()
    return True


# ------------------------------------------------------------------------------------------------------ retention (T5)
def forget(c, enquiry_ids: list[int]) -> dict:
    """Everything kept about these people here, BEFORE their enquiry rows go (inside the caller's guard, no commit of its own):
    their identities (name/email comparison values included), their links and responses, and - because members_json and other
    people's notes on the same versions can name them - every historical version entry naming them (replaced by an anonymous
    'removed' marker so counts stay honest) and the notes of ALL responses on those versions. Response choices and timestamps
    are kept. Counts only."""
    ids = [int(i) for i in enquiry_ids]
    out = {"identities": 0, "links": 0, "responses": 0, "versions_scrubbed": 0, "notes_cleared": 0}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    uids = {r["person_uid"] for r in rows(c.execute(f"SELECT person_uid FROM people_identities WHERE enquiry_id IN ({q})", ids))}
    uids |= {r["person_uid"] for r in rows(c.execute(f"SELECT DISTINCT person_uid FROM group_response_links WHERE enquiry_id IN ({q})", ids))}
    uids |= {r["person_uid"] for r in rows(c.execute(f"SELECT DISTINCT person_uid FROM group_responses WHERE enquiry_id IN ({q})", ids))}
    if uids:
        for v in rows(c.execute("SELECT id, members_json FROM group_membership_versions")):
            members = pj(v["members_json"], []) or []
            if not any(m.get("person_uid") in uids for m in members):
                continue
            scrubbed = [({"person_uid": None, "enquiry_id": None, "role": m.get("role"), "display_name": None, "removed": True} if m.get("person_uid") in uids else m) for m in members]
            c.execute("UPDATE group_membership_versions SET members_json=? WHERE id=?", (j(scrubbed), v["id"]))
            out["versions_scrubbed"] += 1
            out["notes_cleared"] += c.execute("UPDATE group_responses SET note=NULL WHERE version_id=? AND note IS NOT NULL", (v["id"],)).rowcount
    out["responses"] += c.execute(f"DELETE FROM group_responses WHERE enquiry_id IN ({q})", ids).rowcount
    out["links"] += c.execute(f"DELETE FROM group_response_links WHERE enquiry_id IN ({q})", ids).rowcount
    out["identities"] += identities.forget(c, ids)
    return out
