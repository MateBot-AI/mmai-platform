"""Internal reservations attention alerts (H19, H16) — generic outbox core, PREPARED, NOT LIVE.

What this is: a small, reusable mechanism that lets authorised application code raise an "attention event" about a record and
have one short internal email queued for the reservations team. Producers wired so far: HomeBot shortlist events
(server/homebot_shortlist.py), GP13 group responses (server/group_responses.py) and, from 9 Nov 2026, the H19 MateBot
"previously suggested apartment no longer available" event (server/stock_lifecycle.reconcile_work). No live transport is configured, and nothing
here changes matching, eligibility, ai_queue or customer messaging. Activation needs (a) Bill's ruling on the attention-event list and (b) Anna's Microsoft 365 sender
authorisation for reservations@almelb.com.au (an Exchange application RBAC "Application Mail.Send" scoped to that mailbox) —
neither exists yet, so `MMAI_ALERT_MODE` defaults to `off` and no row is ever attempted.

Design (same proven pattern as server/receipts.py, separate table, separate configuration):
  * EVENTS                — fixed catalogue of event kinds → fixed labels. Producers choose a kind; they cannot supply free text,
                            so no customer answer, name, nationality, AI rationale or token can reach the email or the table.
  * raise_attention(...)  — idempotent on a stable `event_id` (INSERT OR IGNORE, UNIQUE): raising twice queues once. Runs inside
                            the caller's transaction when asked, otherwise in its own short one. Never raises to the caller.
  * deliver_pending(...)  — conditional claim queued → sending in its own committed transaction, then the transport call with
                            no transaction open; known failures retry (5 attempts, 4 delays of 1/5/15/60 min) then `failed`.
                            A row left in `sending` (crash between claim and result) is NOT re-queued automatically: delivery
                            is ambiguous, so it waits for a person (`python -m server.alerts status` lists them). At-least-once:
                            a lost acknowledgement after the provider accepted the message can lead to one duplicate alert.
  * transports            — captured (tests; recorded as a simulation) and dryrun (simulated): nothing leaves the machine and
                            no row is marked `sent`. `graph` (added 11 Nov 2026) is the Microsoft 365 transport this report
                            recorded as "not built": Graph sendMail with OAuth client credentials (server/mail_graph.py);
                            `sent` then means Graph ACCEPTED the message (HTTP 202), not that it was delivered. No SMTP-password
                            transport is offered for alerts. The captured/dry-run classes come from server/receipts.py unchanged.
Configuration (all optional; defaults are safe):
  MMAI_ALERT_MODE       off (default: rows are `held`, nothing attempted) | dryrun | captured (tests only) | graph (live, needs
                        MMAI_GRAPH_TENANT_ID / MMAI_GRAPH_CLIENT_ID / MMAI_GRAPH_CLIENT_SECRET and the tenant-side scoped grant)
  (recipient)           hard-locked to RECIPIENT = reservations@almelb.com.au (H16); not configurable, snapshotted on each row,
                        and a row whose snapshot differs from the constant is held rather than redirected.
  MMAI_ALERT_FROM       sender address shown on the alert. No default.
  MMAI_STAFF_URL        trusted base URL of the staff workspace (https, or http on localhost only); the link is this base plus
                        a fixed path with the numeric record id — nothing else is interpolated.
  MMAI_ALERT_SEND_TEST  0 (default): events about TEST data are `held`; 1 lets them through (dry-run or captured only today).
CLI: python -m server.alerts status | deliver | synthetic   (synthetic = the only event source shipped: one TEST-flagged event)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional
from urllib.parse import urlsplit

from .db import now_iso, row, rows, transaction
from .receipts import CapturedTransport, DryRunTransport, valid_email, _ambiguous

log = logging.getLogger("mmai.alerts")

RECIPIENT = "reservations@almelb.com.au"  # H16; the only destination this release can address
MAX_ATTEMPTS = 5
BACKOFF_MIN = [1, 5, 15, 60]  # delays before attempts 2..5
RETRYABLE = (ConnectionError, TimeoutError, OSError)

# Fixed catalogue: kind → (label, record type, staff path). Producers may only pick a kind from this list; adding one is a code
# change that Bill's attention-event ruling (H19, open matter) must cover first. Paths are existing staff routes only
# (client/staff.js: #/staff and #/staff/enquiries/{id}); the synthetic event links to the plain staff page because its record
# is not guaranteed to exist.
EVENTS = {
    "synthetic_test": ("Synthetic test alert (no action needed)", "enquiry", "#/staff"),
    # 9 Nov 2026, T3(a) H2 HomeBot integrated matching (server/homebot_shortlist.py). Fixed kinds about one HomeBot enquiry.
    "homebot_shortlist_ready": ("HomeBot options prepared for staff review", "homebot_enquiry", "#/staff/homebot"),
    "homebot_shortlist_changed": ("HomeBot options changed after an apartment or requirement update", "homebot_enquiry", "#/staff/homebot"),
    "homebot_apartment_unavailable": ("An apartment on a HomeBot shortlist is no longer available", "homebot_enquiry", "#/staff/homebot"),
    "homebot_matching_failed": ("HomeBot options could not be prepared; retries have stopped", "homebot_enquiry", "#/staff/homebot"),
    "homebot_needs_review": ("HomeBot options need a staff check before anything is offered", "homebot_enquiry", "#/staff/homebot"),
    "homebot_review_unavailable": ("AI review did not complete; HomeBot options remain held. Retry the AI review from the enquiry page", "homebot_enquiry", "#/staff/homebot"),
    # 9 Nov 2026, GP13 individual group responses (server/group_responses.py). Fixed kinds about one MateBot group; staff review only.
    "group_response_declined": ("Someone in a proposed group said no to sharing; staff review needed", "group", "#/staff/groups/{id}"),
    "group_response_change_requested": ("Someone in a proposed group asked for a change; staff review needed", "group", "#/staff/groups/{id}"),
    "group_response_overdue": ("24 hours since the group was introduced and not everyone has answered; staff review of a possible replacement", "group", "#/staff/groups/{id}"),
    # 9 Nov 2026, H19 ("email reservations if a previously suggested apartment becomes unavailable, so staff can communicate
    # manually"): raised by stock_lifecycle.reconcile_work for EVERY live MateBot group whose own current offer (or the option it
    # had been introduced with) is no longer available - leased elsewhere, withdrawn from stock, deleted, or changed so that the
    # rules no longer pass - once per group and stock transition (revision = transition id). ONE fixed cause-neutral body: the
    # alert never claims a lease when the apartment was withdrawn or deleted.
    "group_apartment_unavailable": ("A previously suggested apartment for a MateBot group is no longer available; the group's options were re-checked and staff should review it", "group", "#/staff/groups/{id}"),
    # Same H19 event/body, distinct technical namespace: revision is the explicit
    # re-check's audit id, never a stock-transition id. No new recipient or send path.
    "group_apartment_unavailable_recheck": ("A previously suggested apartment for a MateBot group is no longer available; the group's options were re-checked and staff should review it", "group", "#/staff/groups/{id}"),
}
RECORD_TYPES = {"enquiry": "MateBot-{id:05d}", "homebot_enquiry": "HomeBot-{id:05d}", "group": "Group-{id:05d}"}
STAFF_ENQUIRY_PATH = "#/staff/enquiries/{id}"  # for future kinds whose record is an existing enquiry


# ---------------------------------------------------------------------------
# configuration


def alert_mode() -> str:
    m = (os.environ.get("MMAI_ALERT_MODE") or "off").strip().lower()
    return m if m in ("off", "dryrun", "captured", "graph") else "off"


def recipient() -> str:
    return RECIPIENT


def sender() -> Optional[str]:
    return valid_email(os.environ.get("MMAI_ALERT_FROM"))


def staff_base_url() -> Optional[str]:
    """Trusted configured base only: https, or http for localhost; no query, no fragment, no credentials."""
    u = (os.environ.get("MMAI_STAFF_URL") or "").strip()
    if not u or any(ch in u for ch in " \r\n\t<>\"'"):
        return None
    try:
        p = urlsplit(u)
        host = p.hostname  # a malformed IPv6 literal raises ValueError here: treat as invalid configuration, never as a crash
        if p.scheme not in ("http", "https") or not host or p.username or p.password or p.query or p.fragment:
            return None
    except ValueError:
        return None
    if p.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        return None
    return u.rstrip("/")


def send_test_allowed() -> bool:
    return (os.environ.get("MMAI_ALERT_SEND_TEST") or "0").strip() == "1"


def config_problem() -> Optional[str]:
    """Why delivery cannot proceed in the current mode (None when fine). `off` is never a problem: it is the safe default."""
    if alert_mode() == "off":
        return None
    if not sender():
        return "MMAI_ALERT_FROM missing or invalid"
    if not staff_base_url():
        return "MMAI_STAFF_URL missing or not a trusted https base"
    if alert_mode() == "graph":
        from . import mail_graph
        return mail_graph.config_problem(sender())
    return None


# ---------------------------------------------------------------------------
# transports — captured / dry-run only (no live provider is configured or authorised yet)

class CapturedSimulation(CapturedTransport):
    """Captured transport for tests and local QA: keeps messages in memory and is recorded as a SIMULATION (status `dryrun`),
    never as a live send."""

    def __init__(self, fail_times: int = 0, fail_with: Optional[type] = None):
        super().__init__(fail_times, fail_with)
        self.simulated = True  # the base class sets an instance attribute, so override it here


_captured: Optional[CapturedSimulation] = None


def captured_transport() -> CapturedSimulation:
    global _captured
    if _captured is None:
        _captured = CapturedSimulation()
    return _captured


def transport_from_env():
    mode = alert_mode()
    if mode == "dryrun":
        return DryRunTransport()
    if mode == "captured":
        return captured_transport()
    if mode == "graph":
        from . import mail_graph
        return mail_graph.GraphTransport(sender())
    return None


# ---------------------------------------------------------------------------
# rendering — fixed label + record reference + trusted link; nothing else


def reference(record_type: str, record_id: int) -> str:
    return RECORD_TYPES[record_type].format(id=int(record_id))


def render(a: dict) -> dict:
    label, _rt, path = EVENTS[a["kind"]]
    ref = reference(a["record_type"], a["record_id"])
    link = f"{staff_base_url() or ''}/{path.format(id=int(a['record_id']))}"
    when = a.get("created_at") or ""  # the time the event was raised (stored), not the time of this delivery attempt
    lines = [
        f"{label}",
        f"Record: {ref}" + (" (TEST data)" if a.get("is_test") else ""),
        f"Open in MateBot AI: {link}",
        f"Raised: {when}",
        "",
        "Internal alert from MateBot AI for the reservations team. Open MateBot AI to act; this message carries no customer details.",
    ]
    return {"subject": f"MateBot AI attention: {label} — {ref}", "text": "\n".join(lines)}


def build_message(a: dict) -> EmailMessage:
    r = render(a)
    frm = sender() or "no-reply@invalid.local"
    msg = EmailMessage()
    msg["Subject"] = r["subject"]
    msg["From"] = formataddr(("MateBot AI", frm))
    msg["To"] = a["to_email"]
    msg["Message-ID"] = make_msgid(domain=frm.split("@", 1)[1] if "@" in frm else None)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(r["text"])
    return msg


# ---------------------------------------------------------------------------
# outbox


def initial_status(is_test: bool) -> tuple[str, Optional[str]]:
    if alert_mode() == "off":
        return "held", "alerts disabled (MMAI_ALERT_MODE=off)"
    if is_test and not send_test_allowed():
        return "held", "test data: alert delivery not enabled (MMAI_ALERT_SEND_TEST)"
    return "queued", None


def _validate(kind: str, record_type: str, record_id) -> tuple[str, str, int]:
    if kind not in EVENTS:
        raise ValueError("unknown attention event kind")
    if record_type not in RECORD_TYPES or EVENTS[kind][1] != record_type:
        raise ValueError("record type does not match the event kind")
    if isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0:
        raise ValueError("record id must be a positive integer")
    return kind, record_type, record_id


def event_id_for(kind: str, record_type: str, record_id: int, revision: int = 0) -> str:
    """Stable idempotency key built only from validated values: kind, record type, numeric id and a non-negative numeric
    revision (0 = once per record; a producer that intends a repeat for the same record passes its own revision number)."""
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision must be a non-negative integer")
    return f"{kind}:{record_type}:{int(record_id)}:{int(revision)}"


def raise_attention(c, kind: str, record_id: int, *, is_test: bool = False, revision: int = 0, in_transaction: bool = False) -> dict:
    """Queue one internal alert for a fixed event kind about a record. Idempotent on the event id. Never raises to the caller:
    an alert problem must never fail the business operation that raised it. Returns {'event_id', 'status', 'created'}.
    Nothing unvalidated is logged or stored: on a bad argument only the exception type is logged."""
    try:
        kind, record_type, record_id = _validate(kind, EVENTS.get(kind, ("", "", ""))[1], record_id)
        eid = event_id_for(kind, record_type, record_id, revision)
        status, err = initial_status(bool(is_test))
        now = now_iso()
        sql = ("INSERT OR IGNORE INTO internal_alert_outbox(event_id, kind, record_type, record_id, to_email, is_test, status, attempts, "
               "last_error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,0,?,?,?)")
        params = (eid, kind, record_type, record_id, RECIPIENT, 1 if is_test else 0, status, err, now, now)
        if in_transaction:
            cur = c.execute(sql, params)
            created = cur.rowcount == 1
        else:
            with transaction(c):
                cur = c.execute(sql, params)
                created = cur.rowcount == 1
        with _read_lock(c):  # lock only: must not commit the caller's transaction when in_transaction=True
            r = row(c.execute("SELECT status FROM internal_alert_outbox WHERE event_id=?", (eid,)))
        log.info("attention event %s %s", eid, "queued" if created else "already present")
        return {"event_id": eid, "status": r["status"] if r else status, "created": created}
    except Exception as ex:
        log.warning("attention event not recorded error=%s", type(ex).__name__)
        return {"event_id": None, "status": "error", "created": False, "error": type(ex).__name__}


def _read_lock(c):
    """Connection lock only (re-entrant), for SELECT + fetch without starting or committing any transaction: safe inside a
    caller's open transaction, and it stops another thread's statement resetting this cursor between execute and fetch."""
    return c._mmai_lock


def _backoff(attempts: int, now: Optional[datetime] = None) -> str:
    mins = BACKOFF_MIN[min(max(attempts - 1, 0), len(BACKOFF_MIN) - 1)]
    return ((now or datetime.now(timezone.utc)) + timedelta(minutes=mins)).replace(microsecond=0).isoformat()


def _set(c, oid: int, **cols) -> None:
    cols["updated_at"] = now_iso()
    with transaction(c):
        c.execute(f"UPDATE internal_alert_outbox SET {', '.join(k + '=?' for k in cols)} WHERE id=?", [*cols.values(), oid])


def deliver_pending(c, transport=None, limit: int = 25, now: Optional[str] = None) -> dict:
    """Deliver queued alerts. Claim queued → sending in a committed transaction BEFORE the transport call (a concurrent pass
    skips the row; a crash mid-send leaves a visible `sending` row for manual review, never an automatic duplicate). The
    transport call runs with no transaction open and no connection lock held. Only `queued` rows are picked up: enabling alerts
    later never turns earlier `held` rows into sent ones (releasing them is a deliberate manual step, not built here)."""
    counts = {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "held": 0, "skipped": 0}
    if alert_mode() == "off":  # off wins even when a caller hands in a transport
        return counts
    transport = transport or transport_from_env()
    if transport is None:
        return counts
    now = now or now_iso()
    # every SELECT is executed AND fetched under the connection lock (transaction block): on the shared sqlite connection a
    # statement run by another thread between execute and fetch would reset this cursor and lose the row
    with _read_lock(c):
        due = rows(c.execute("SELECT id FROM internal_alert_outbox WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at<=?) "
                             "ORDER BY id LIMIT ?", (now, limit)))
    for d in due:
        oid = d["id"]
        with transaction(c):  # atomic claim + snapshot of the claimed row in the same locked block; no network call in here
            cur = c.execute("UPDATE internal_alert_outbox SET status='sending', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'", (now_iso(), oid))
            a = row(c.execute("SELECT * FROM internal_alert_outbox WHERE id=?", (oid,))) if cur.rowcount == 1 else None
        if a is None:
            counts["skipped"] += 1
            continue
        problem = config_problem()
        if a["to_email"] != RECIPIENT:
            problem = "queued recipient differs from the fixed internal recipient; not sent"
        if problem or (a["is_test"] and not send_test_allowed()):
            _set(c, oid, status="held", last_error=problem or "test data: alert delivery not enabled (MMAI_ALERT_SEND_TEST)")
            counts["held"] += 1
            continue
        try:
            mid = transport.send(build_message(a))  # outside any transaction
        except RETRYABLE as ex:
            if _ambiguous(ex):
                _set(c, oid, last_error=type(ex).__name__)  # keep sending for manual reconciliation
                counts["skipped"] += 1
            elif a["attempts"] >= MAX_ATTEMPTS:
                _set(c, oid, status="failed", last_error=f"{type(ex).__name__} after {a['attempts']} attempts")
                counts["failed"] += 1
            else:
                _set(c, oid, status="queued", last_error=type(ex).__name__, next_attempt_at=_backoff(a["attempts"]))
                counts["retry"] += 1
            continue
        except Exception as ex:  # configuration or rendering problem: not retried blindly, left for a person
            _set(c, oid, status="failed", last_error=f"{type(ex).__name__}")
            counts["failed"] += 1
            continue
        if getattr(transport, "simulated", False):
            _set(c, oid, status="dryrun", message_id=mid, last_error=None)
            counts["dryrun"] += 1
        else:
            _set(c, oid, status="sent", message_id=mid, sent_at=now_iso(), last_error=None)
            counts["sent"] += 1
    return counts


def kick_delivery(c) -> Optional[threading.Thread]:
    """Background pass; the caller never waits on a provider. No-op when alerts are off."""
    if alert_mode() == "off":
        return None

    def run():
        try:
            deliver_pending(c)
        except Exception as ex:
            log.warning("alert delivery pass failed: %s", type(ex).__name__)
    t = threading.Thread(target=run, daemon=True, name="mmai-alerts")
    t.start()
    return t


def status(c) -> dict:
    with _read_lock(c):
        by = {r["status"]: r["n"] for r in rows(c.execute("SELECT status, COUNT(*) AS n FROM internal_alert_outbox GROUP BY status"))}
        stuck = rows(c.execute("SELECT id, event_id, attempts, updated_at FROM internal_alert_outbox WHERE status='sending' ORDER BY id"))
    return {"mode": alert_mode(), "config_problem": config_problem(), "counts": by, "sending_needs_review": stuck}


def main(argv: list[str]) -> int:
    from . import db
    c = db.connect()
    db.init_schema(c)
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        print(status(c))
    elif cmd == "deliver":
        print(deliver_pending(c))
    elif cmd == "synthetic":
        print(raise_attention(c, "synthetic_test", 1, is_test=True, revision=int(datetime.now(timezone.utc).timestamp())))
    else:
        print("usage: python -m server.alerts status|deliver|synthetic")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
