"""Fixed HomeBot submission copies, separate from matching and customer match messages.

The immutable, allowlisted original answers are captured in the save transaction.
Only queued rows can be dispatched. Off/TEST suppression and transport configuration
are shared with MateBot; no credentials or message bodies are logged.
"""
from __future__ import annotations

import html
import logging
import threading

from . import homebot, receipts
from .db import assert_not_guarded, j, now_iso, pj, row, rows, transaction

log = logging.getLogger(__name__)
KIND = "homebot_submission"
SUBJECT = "Your HomeBot enquiry: a copy of your answers"
ANSWER_KEYS = {
    "first_name", "last_name", "email", "phone_code", "phone", "contact_pref",
    "party_mode", "headcount_now", "headcount_target", "want_help", "people", "bunk_condition",
    "move_in", "move_in_flex", "move_in_earliest", "move_in_latest",
    "move_out", "move_out_flex", "move_out_earliest", "move_out_latest",
    "budget_total_weekly", "budget_types", "price_guide", "guide_snapshot",
    "bedrooms", "bathrooms", "area", "transport", "max_travel", "extras", "extras_top",
    "apartment_notes", "viewing", "heard", "consent_contact", "consent_lease", "consent_adults",
}
PERSON_KEYS = {"position", "name", "sleep", "weekly_max", "doing", "institution",
               "institution_other", "place", "primary_destination"}
_deliver_lock = threading.Lock()


def _text(value):
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "None chosen"
    return str(value) if value is not None and value != "" else "Not provided"


def snapshot(a: dict) -> dict:
    """Explicit customer fields only, never arbitrary record/dictionary serialization."""
    sections = []

    def section(title, items):
        sections.append([title, [[q, _text(v)] for q, v in items]])

    code = a.get("phone_code")
    section("About you", [
        ("Name", f"{a['first_name']} {a['last_name']}"), ("Email", a["email"]),
        ("Phone", a["phone"] if code == "other" else f"{code} {a['phone']}"),
        ("Preferred contact", a["contact_pref"]),
        ("Do you have everyone in your group?", "Yes" if a["party_mode"] == "complete" else "No"),
        ("People moving in now", a["headcount_now"]), ("People wanted in total", a["headcount_target"]),
    ])
    if a["party_mode"] != "complete":
        sections[0][1].append(["Would you like MateBot to find more housemates?",
                               "No, we will find them ourselves" if a.get("want_help") == "no" else _text(a.get("want_help"))])
    section("Your stay", [
        (f"{label} {suffix}".strip(), a.get(f"{key}{field}"))
        for key, label in (("move_in", "Move in"), ("move_out", "Move out"))
        for field, suffix in (("", ""), ("_flex", "flexibility"),
                              ("_earliest", "earliest acceptable date"), ("_latest", "latest acceptable date"))
        if field in ("", "_flex") or a.get(f"{key}{field}")
    ])
    guide = "; ".join(f"{g.get('label', g.get('code', ''))}: {homebot._range(g)}"
                      for g in a.get("guide_snapshot") or [])
    section("Budget and apartment", [
        ("Whole-apartment maximum per week", f"${a['budget_total_weekly']:,}"),
        ("Selected apartment price-guide options", guide or a["budget_types"]),
        ("Price guide shown", homebot.RENT_KINDS.get(a.get("price_guide"), "Not recorded")),
        ("Bedrooms", a["bedrooms"]), ("Bathrooms", a["bathrooms"]), ("Area", a["area"]),
        ("Getting around", a["transport"]), ("Longest acceptable trip", a["max_travel"]),
        ("Features you would like", a["extras"]), ("Which three matter most?", a.get("extras_top")),
        ("Bunks", a.get("bunk_condition") or "No bunks chosen"),
        ("Anything else about the apartment?", a.get("apartment_notes")),
    ])
    for p in a["people"]:
        section(f"Person {p['position']}", [
            ("Name", p.get("name")), ("Sleeping arrangement", p["sleep"]),
            ("Weekly maximum per person", f"${p['weekly_max']:,}"), ("Study or work", p["doing"]),
            ("Institution", p.get("institution")), ("Other institution", p.get("institution_other")),
            ("Work or possible work location", p.get("place")),
            ("Primary destination", p.get("primary_destination")),
        ])
    section("Viewing and consent", [
        ("Preferred viewing", a["viewing"]), ("How did you hear about us?", a.get("heard")),
        *[(label, "Yes" if a.get(key) else "No") for key, label in zip(
            ("consent_contact", "consent_lease", "consent_adults"), homebot.CONSENT_TEXT)],
    ])
    return {"first_name": a["first_name"], "party_mode": a["party_mode"],
            "headcount_now": a["headcount_now"], "headcount_target": a["headcount_target"], "sections": sections}


def render(saved: dict, enquiry_id: int, is_test: bool) -> dict:
    first = saved["first_name"] or "there"
    next_step = ("Our team will review your apartment requirements and contact you about suitable options and viewings."
                 if saved["party_mode"] == "complete" else
                 f"We have noted that you have {saved['headcount_now']} people and want {saved['headcount_target']} in total. "
                 "You are finding the remaining housemates yourselves. Our team will review your apartment requirements "
                 "and discuss the next steps with you. Please let us know when your group is complete.")
    paragraphs = [f"Hi {first},", "Thanks for telling us what you are looking for. Here is a copy of your enquiry.",
                  next_step, "If an answer needs changing, please contact the Apartment Living Melbourne team.",
                  f"Reference: {homebot.reference(enquiry_id)}"]
    if is_test:
        paragraphs.insert(1, receipts.TEST_NOTE)
    text = "\n\n".join(paragraphs) + "\n\nYour answers\n"
    body = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs)
    for title, items in saved["sections"]:
        text += "\n" + title + "\n" + "\n".join(f"{q}: {v}" for q, v in items) + "\n"
        body += f"<h2>{html.escape(title)}</h2><dl>"
        body += "".join(f"<dt>{html.escape(q)}</dt><dd>{html.escape(v)}</dd>" for q, v in items) + "</dl>"
    return {"subject": (receipts.TEST_SUBJECT_PREFIX if is_test else "") + SUBJECT,
            "text": text, "html": f"<!doctype html><html><body>{body}</body></html>"}


def enqueue_in_transaction(c, enquiry_id: int) -> None:
    """Called only by the original submission hook, before commit."""
    e = row(c.execute("SELECT email, is_test, answers_json FROM homebot_enquiries WHERE id=?", (enquiry_id,)))
    to = receipts.valid_email(e["email"])
    status, error = receipts.initial_status(to, bool(e["is_test"]))
    saved = snapshot(pj(e["answers_json"]))
    now = now_iso()
    c.execute("""INSERT OR IGNORE INTO homebot_email_outbox
        (enquiry_id, kind, to_email, snapshot_json, status, attempts, last_error, created_at, updated_at)
        VALUES (?,?,?,?,?,0,?,?,?)""", (enquiry_id, KIND, to or "", j(saved), status, error, now, now))


def outbox_status(c, enquiry_id: int):
    with c._mmai_lock:
        return row(c.execute("""SELECT id, status, attempts, last_error FROM homebot_email_outbox
                                WHERE enquiry_id=? AND kind=?""", (enquiry_id, KIND)))


def after_saved(c, enquiry_id: int) -> dict:
    """Missing historical snapshot is held, never invented from later staff edits."""
    try:
        with transaction(c):
            e = row(c.execute("SELECT email FROM homebot_enquiries WHERE id=?", (enquiry_id,)))
            if not e:
                return {"status": "cancelled"}
            now = now_iso()
            c.execute("""INSERT OR IGNORE INTO homebot_email_outbox
                (enquiry_id, kind, to_email, snapshot_json, status, attempts, last_error, created_at, updated_at)
                VALUES (?,?,?,NULL,'held',0,?,?,?)""",
                (enquiry_id, KIND, receipts.valid_email(e["email"]) or "",
                 "original submission snapshot unavailable; not sent", now, now))
            result = outbox_status(c, enquiry_id)
        if result["status"] == "queued":
            kick_delivery(c)
        return result
    except Exception as ex:
        log.warning("HomeBot receipt preparation failed enquiry=%s error=%s", enquiry_id, type(ex).__name__)
        return {"status": "error"}


def _set(c, oid, **cols):
    cols["updated_at"] = now_iso()
    with transaction(c):
        c.execute(f"UPDATE homebot_email_outbox SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                  [*cols.values(), oid])


def deliver_pending(c, transport=None, limit=25, now=None):
    """Committed atomic claims, bounded retries, no automatic replay of ambiguous sends.

    Deletion prevents unclaimed sends and removes the snapshot. A deletion after the
    final existence check cannot recall an in-flight message; no stronger guarantee.
    """
    counts = dict(sent=0, dryrun=0, retry=0, failed=0, cancelled=0, skipped=0)
    if receipts.mail_mode() == "off":
        return counts
    assert_not_guarded(c, "HomeBot receipt delivery")
    transport = transport or receipts.transport_from_env()
    if transport is None:
        return counts
    if (receipts.config_problem() and not getattr(transport, "simulated", False)
            and not isinstance(transport, receipts.CapturedTransport)):
        return counts
    with _deliver_lock:
        with c._mmai_lock:
            if c.in_transaction:
                raise RuntimeError("HomeBot receipt delivery requires an idle connection")
            pending = rows(c.execute("""SELECT id FROM homebot_email_outbox WHERE status='queued'
                AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id LIMIT ?""", (now or now_iso(), limit)))
        for p in pending:
            oid = p["id"]
            with transaction(c):
                cur = c.execute("""UPDATE homebot_email_outbox SET status='sending', attempts=attempts+1,
                    updated_at=? WHERE id=? AND status='queued'""", (now_iso(), oid))
                ob = row(c.execute("SELECT * FROM homebot_email_outbox WHERE id=?", (oid,))) if cur.rowcount == 1 else None
            if not ob:
                counts["skipped"] += 1
                continue
            with c._mmai_lock:
                e = row(c.execute("SELECT email, is_test FROM homebot_enquiries WHERE id=?", (ob["enquiry_id"],)))
            if not e:
                _set(c, oid, status="cancelled", last_error="enquiry no longer exists")
                counts["cancelled"] += 1
                continue
            to = receipts.valid_email(ob["to_email"])
            if (not to or e["email"].strip().lower() != to.lower() or not ob["snapshot_json"]
                    or (e["is_test"] and not receipts.test_recipient_allowed(to)
                        and not isinstance(transport, receipts.CapturedTransport))):
                _set(c, oid, status="held", last_error="recipient, TEST permission or original snapshot requires review")
                counts["skipped"] += 1
                continue
            try:
                msg = receipts.build_message(to, render(pj(ob["snapshot_json"]), ob["enquiry_id"], bool(e["is_test"])))
                with c._mmai_lock:
                    current = row(c.execute("""SELECT e.email FROM homebot_email_outbox o
                        JOIN homebot_enquiries e ON e.id=o.enquiry_id WHERE o.id=? AND o.status='sending'""", (oid,)))
                if not current or current["email"].strip().lower() != to.lower():
                    _set(c, oid, status="cancelled", last_error="enquiry removed or recipient changed")
                    counts["cancelled"] += 1
                    continue
                assert_not_guarded(c, "HomeBot receipt send")
                mid = transport.send(msg)
            except Exception as ex:
                if receipts._ambiguous(ex):
                    _set(c, oid, last_error=type(ex).__name__)
                    counts["skipped"] += 1
                elif receipts._permanent_stop(ex) or ob["attempts"] >= receipts.MAX_ATTEMPTS:
                    _set(c, oid, status="failed", last_error=type(ex).__name__)
                    counts["failed"] += 1
                else:
                    _set(c, oid, status="queued", last_error=type(ex).__name__,
                         next_attempt_at=receipts._backoff(ob["attempts"]))
                    counts["retry"] += 1
                continue
            # A failed acknowledgement write after provider acceptance must not
            # put the message back into the retry queue. Leave sending for review.
            status = "dryrun" if getattr(transport, "simulated", False) else "sent"
            _set(c, oid, status=status, message_id=mid, sent_at=now_iso(), last_error=None)
            counts[status] += 1
    return counts


def status(c):
    """Operational view without customer addresses, answers or staff text."""
    with c._mmai_lock:
        counts = {r["status"]: r["n"] for r in rows(c.execute(
            "SELECT status, COUNT(*) n FROM homebot_email_outbox GROUP BY status"))}
        attention = rows(c.execute("""SELECT id, enquiry_id, status, attempts, last_error, updated_at
            FROM homebot_email_outbox WHERE status IN ('sending','held','failed') ORDER BY id"""))
    return {"mode": receipts.mail_mode(), "counts": counts, "needs_review": attention}


def kick_delivery(c):
    if receipts.mail_mode() == "off":
        return None

    def run():
        try:
            deliver_pending(c)
        except Exception as ex:
            log.warning("HomeBot receipt pass failed error=%s", type(ex).__name__)
    worker = threading.Thread(target=run, daemon=True, name="homebot-receipts")
    worker.start()
    return worker


if __name__ == "__main__":
    import sys
    from . import db
    if sys.argv[1:] not in ([], ["status"]):
        raise SystemExit("usage: python -m server.homebot_receipts status")
    connection = db.connect()
    db.init_schema(connection)
    print(status(connection))
