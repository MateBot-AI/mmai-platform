"""I19(q) — automatic customer receipt emails (MateBot public form).

What this module does
  * `enqueue_in_transaction(c, eid, ...)` — called by the public submit path through create_enquiry's `on_inserted` hook, so the
                                         outbox row is written in the SAME transaction as the enquiry: the intent to send is as
                                         durable as the enquiry itself (committed together or rolled back together). ONE row per
                                         enquiry and kind (UNIQUE), so a replayed submission can never queue twice.
  * `enqueue_receipt(c, enquiry_id)`   — idempotent repair used after the save (no-op when the row exists). Never raises.
  * `deliver_pending(c, transport)`    — sends queued rows OUTSIDE any database transaction, one at a time, claiming each row
                                         atomically first (status queued → sending) so two delivery passes cannot both send it.
                                         Transient failures retry with back-off; after MAX_ATTEMPTS the row is `failed`.
  * `render_receipt(enquiry_row)`      — plain text + escaped HTML built ONLY from the saved canonical record (answers_json and
                                         the customer columns) through an allow-list of customer questions. Staff notes, status,
                                         derived nationality classification, scores, keys and internal fields are never read.

Configuration (environment, provider-neutral; nothing is sent by default)
  MMAI_MAIL_MODE        off (default) | dryrun | smtp | graph
                        off    → rows are recorded as `held` (nothing rendered, nothing sent, no claim of sending)
                        dryrun → rows are rendered and marked `dryrun` (no network; for local QA)
                        smtp   → real delivery through MMAI_SMTP_* (SMTP AUTH with a mailbox password; Microsoft 365 disables
                                 this by default for existing tenants from the end of December 2026)
                        graph  → real delivery through Microsoft Graph sendMail with OAuth client credentials (server/mail_graph.py,
                                 MMAI_GRAPH_*; the Microsoft 365 path recorded in reports/20261105-attention-alerts)
  MMAI_MAIL_FROM        sender address, required for smtp and graph (validated); MMAI_MAIL_FROM_NAME optional display name
  MMAI_MAIL_REPLY_TO    optional reply-to address (validated)
  MMAI_SMTP_HOST / MMAI_SMTP_PORT (587) / MMAI_SMTP_USER / MMAI_SMTP_PASSWORD / MMAI_SMTP_SECURITY (starttls | ssl | none)
  MMAI_MAIL_SEND_TEST   0 (default): receipts for TEST enquiries are `held`. 1: sent only when the recipient matches
  MMAI_MAIL_TEST_ALLOW  comma-separated addresses or @domains that test receipts may go to (required when SEND_TEST=1).

Privacy: the outbox stores no message body — only the enquiry id, kind, the recipient as supplied on the form (snapshot taken
when the enquiry was created) and delivery state. The body is rendered at send time from the enquiry's `answers_json`, the payload
exactly as submitted (staff edits change the columns, never that payload). If the record's email no longer matches the queued
recipient when delivery runs, the row is `held` (never sent to the old address, never retargeted automatically). Deleting the enquiry (staff delete, delete test data, retention purge) removes the
queued receipt with it (ON DELETE CASCADE) and a pending delivery finds nothing to send. Logs carry ids, states and error classes only.
Transport: provider-neutral SMTP with certificate verification (STARTTLS or implicit TLS) — any SMTP-compatible provider or relay
works once its own authentication requirements are met — or Microsoft Graph sendMail (mode `graph`, OAuth 2.0 client credentials,
see server/mail_graph.py). No Google OAuth flow. `sent` means the provider accepted the message for processing, not delivery.
Enabling mail later never back-fills: rows recorded as `held` while mail was off stay held unless an operator releases them.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import ssl
import threading
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Optional

from . import rules
from . import intro_profile as ip
from .db import now_iso, row, rows, transaction

log = logging.getLogger("mmai.receipts")

KIND_RECEIPT = "customer_receipt"
KIND_RESERVATIONS = "reservations_new_enquiry"
RESERVATIONS_TO = "reservations@almelb.com.au"
MAX_ATTEMPTS = 5
BACKOFF_MIN = [1, 5, 15, 60, 240]
SUBJECT = "Your MateBot enquiry – we’re finding your perfect housemates"
# R2 (9 Nov 2026): receipts for TEST-mode records are marked so a test copy can never be mistaken for a real enquiry receipt.
TEST_SUBJECT_PREFIX = "[TEST] "
TEST_NOTE = "This is a TEST record made in MateBot's test mode. It is not a real enquiry and nobody will act on it."
# doc 2: after answers, before Kind Regards
LOCKED_IN_LINE = "We're locked in on this—update incoming soon"
# doc 2 sign-off: two lines under Kind Regards (not a single combined phrase)
TEAM_LINES = ("Apartment Living Melbourne", "Reservation Team")
TEAM = "\n".join(TEAM_LINES)
LOGO_URL = "https://app.matebot.com.au/img/alm-logo.jpg"
DISCLAIMER = (
    "The information contained in this electronic message (e-mail), and any files transmitted with it, is intended for "
    "the named recipients only. It may contain privileged and/or confidential information and if you are not an intended "
    "recipient, you must not copy, distribute or take any action in reliance on it. Please notify us immediately if you "
    "have received this electronic message (e-mail) and any files transmitted with it, in error. Then, please destroy your copy."
)

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


# ---------------------------------------------------------------------------
# configuration


def mail_mode() -> str:
    m = (os.environ.get("MMAI_MAIL_MODE") or "off").strip().lower()
    return m if m in ("off", "dryrun", "smtp", "graph") else "off"


def valid_email(v) -> Optional[str]:
    """One plain address, no display name, no header injection. Returns the normalised address or None."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or len(s) > 254 or any(ch in s for ch in ",;<>\r\n \t"):
        return None
    return s if _EMAIL_RE.match(s) else None


def _allow_list() -> list[str]:
    return [x.strip().lower() for x in (os.environ.get("MMAI_MAIL_TEST_ALLOW") or "").split(",") if x.strip()]


def test_recipient_allowed(to_email: str) -> bool:
    """Test-data receipts go nowhere unless explicitly enabled AND the recipient is on the allow list."""
    if (os.environ.get("MMAI_MAIL_SEND_TEST") or "0").strip() != "1":
        return False
    to = to_email.lower()
    for item in _allow_list():
        if item.startswith("@") and to.endswith(item):
            return True
        if item == to:
            return True
    return False


def config_problem() -> Optional[str]:
    """Why real sending cannot proceed (None when the smtp / graph configuration is complete and valid)."""
    mode = mail_mode()
    if mode not in ("smtp", "graph"):
        return None
    if not valid_email(os.environ.get("MMAI_MAIL_FROM")):
        return "MMAI_MAIL_FROM missing or invalid"
    if os.environ.get("MMAI_MAIL_REPLY_TO") and not valid_email(os.environ.get("MMAI_MAIL_REPLY_TO")):
        return "MMAI_MAIL_REPLY_TO invalid"
    if mode == "graph":
        from . import mail_graph
        return mail_graph.config_problem(valid_email(os.environ.get("MMAI_MAIL_FROM")))
    host = (os.environ.get("MMAI_SMTP_HOST") or "").strip()
    if not host:
        return "MMAI_SMTP_HOST missing"
    sec = (os.environ.get("MMAI_SMTP_SECURITY") or "starttls").strip().lower()
    if sec not in ("starttls", "ssl", "none"):
        return "MMAI_SMTP_SECURITY must be starttls, ssl or none"
    if sec == "none" and host not in ("localhost", "127.0.0.1", "::1"):
        return "MMAI_SMTP_SECURITY=none is only allowed for a local relay"
    return None


# ---------------------------------------------------------------------------
# rendering — allow-listed customer questions only


def _fmt_date(v) -> str:
    if not v:
        return ""
    try:
        return date.fromisoformat(str(v)[:10]).strftime("%-d %B %Y")
    except Exception:
        return str(v)


def _fmt_list(v) -> str:
    if isinstance(v, str):
        v = [v]
    return ", ".join(str(x) for x in (v or []) if str(x).strip())


def _fmt_bool(v) -> str:
    return "Yes" if v in (True, 1, "1", "true", "Yes", "yes", "on") else "No"


def _budget_label(a: dict, e: dict) -> str:
    opt = a.get("budget_option") or e.get("budget_option")
    if not opt:
        return ""
    try:
        mi = date.fromisoformat(str(a.get("move_in") or e.get("move_in"))[:10]) if (a.get("move_in") or e.get("move_in")) else None
        mo = date.fromisoformat(str(a.get("move_out") or e.get("move_out"))[:10]) if (a.get("move_out") or e.get("move_out")) else None
        kind = rules.guide_kind_for(mi, mo)
    except Exception:
        kind = "12m"
    for o in rules.price_guide(kind):
        if o["id"] == opt:
            return f"{o['label']} per week ({'6-month' if kind == '6m' else '12-month'} price guide)"
    return str(opt)


def _phone(a: dict, e: dict) -> str:
    code = str(a.get("phone_code") or e.get("phone_code") or "").strip()
    num = str(a.get("phone") or e.get("phone") or "").strip()
    if code.lower() == "other":   # R1: the form's "Other" country-code option is a select value, not part of the number
        code = ""
    return f"{code} {num}".strip()


def _people_names(a: dict) -> dict:
    names = {"lead": (a.get("first_name") or "You").strip() or "You"}
    for i, m in enumerate(a.get("party_members") or [], start=1):
        names[f"m{i}"] = (m.get("name") or f"Person {i + 1}").strip() if isinstance(m, dict) else f"Person {i + 1}"
    return names


def _couples(a: dict) -> str:
    pairs = a.get("couples") or []
    names = _people_names(a)
    out = []
    for p in pairs:
        if isinstance(p, (list, tuple)) and len(p) == 2:
            out.append(f"{names.get(p[0], p[0])} and {names.get(p[1], p[1])}")
    return "; ".join(out)


def _party_members(a: dict) -> list[tuple[str, str]]:
    out = []
    for i, m in enumerate(a.get("party_members") or [], start=2):
        if not isinstance(m, dict):
            continue
        bits = []
        if m.get("email"):
            bits.append(f"email {m['email']}")
        if m.get("birth_country_raw"):   # I8: legacy sex / date of birth are never shown, even for a pre-8 November 2026 record that stored them
            bits.append(f"country of birth {m['birth_country_raw']}")
        if ip.given(m) or m.get("intro_sex") or m.get("intro_age") not in (None, ""):   # introduction-only details, exactly as this person gave them
            bits.append(f"for introductions only: {ip.describe(m)}")
        out.append((f"Person {i}: {m.get('name') or 'Not provided'}", "; ".join(bits) or "Not provided"))
    return out


# (field, question as shown to the customer, kind). Only what is listed here can appear in a receipt.
# kinds: text | date | list | bool | special
SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("About you", [
        ("first_name", "First name", "text"), ("last_name", "Last name", "text"), ("email", "Email", "text"),
        ("phone", "Mobile number", "phone"), ("contact_method", "Best way to contact you", "text"),
        ("birth_country_raw", "Country of birth", "text"), ("live_country_raw", "Country you currently live in", "text"),
        ("languages", "Languages you speak", "languages"),
        ("intro_sex", "Sex (shared in introductions to proposed housemates; not used for matching)", "intro_sex"),
        ("intro_age", "Current age (shared in introductions to proposed housemates; not used for matching)", "text"),
    ]),
    ("Your stay", [
        ("city", "City", "text"), ("move_in", "Preferred move-in date", "date"), ("move_in_flex", "How flexible is your move-in date?", "text"),
        ("move_out", "Preferred move-out date", "date"), ("move_out_flex", "How flexible is your move-out date?", "text"),
    ]),
    ("Budget and room", [
        ("budget_option", "The most you can pay per week, per person", "budget"),
        ("apt_sizes", "Which apartment sizes would you consider?", "list"),
        ("mate_ideal", "Ideally, how many housemates would you like?", "text"), ("mate_max", "What's the most housemates you'd accept?", "text"),
        ("rooms", "Which room arrangements would you accept?", "list"), ("room_first", "Which is your first choice?", "text"),
        ("bunk_ok", "Are you OK sleeping in a bunk bed?", "text"),
        ("features", "Which apartment and building features would you particularly like?", "list"),
        ("features_top", "Which three matter most to you?", "list"),
        ("party", "Are you enquiring alone, as a couple sharing one bed, or with friends?", "text"),
        ("party_size", "How many people in total, including you?", "text"),
        ("party_members", "People enquiring with you", "party"),
        ("couples_answer", "Is anyone in your group a couple who will share one bed?", "text"),
        ("couples", "Couples in your group", "couples"),
    ]),
    ("Study or work", [
        ("doing", "What will you be doing in Melbourne?", "text"), ("school", "Where will you study?", "school"),
        ("course", "Course name", "text"), ("level", "Level", "text"), ("course_start", "Course start date", "date"), ("course_end", "Course end date", "date"),
        ("work", "Where will you work?", "text"), ("areas", "Preferred areas", "list"),
        ("travel_modes", "How would you get to campus or work?", "list"), ("travel", "Maximum travel time to campus or work", "text"),
    ]),
    ("Ideal housemates", [   # 8 Nov 2026: the own-country/region and avoid-country/region questions are no longer asked or shown, even for older records
        ("smoke", "Do you smoke or vape?", "text"), ("smoke_ok", "Are you OK living with someone who smokes or vapes outside?", "text"),
        ("routine", "Your daily routine", "text"), ("tidy", "How tidy are you?", "text"), ("social", "Social style at home", "text"),
        ("guests", "Guests and parties", "text"), ("diet", "Diet or kitchen needs", "text"),
        ("else_text", "Anything else we should know?", "text"), ("intro", "A short introduction about yourself for your future housemates", "text"),
    ]),
    ("Consent", [
        ("source", "How did you hear about us?", "text"),
        ("consent_store", "I agree that Apartment Living Melbourne may store my details to match me with housemates.", "bool"),
        ("consent_share", "I agree that ALM may share my name, contact details and introduction with the people in my proposed group.", "bool"),
        ("adults_confirm", "I confirm that everyone included in this enquiry, including me, will be 18 or older on the move-in date. If that is not the case, please contact the ALM team instead of continuing.", "bool"),
        ("consent_marketing", "Send me ALM offers and updates.", "bool"),
    ]),
]

LIST_FIELDS = {"apt_sizes", "rooms", "areas", "travel_modes", "features", "features_top", "languages"}
SCHOOL_NAMED = ("Other", "English language school (enter name)")  # the form asks for the name in both cases


def applicable(field: str, a: dict) -> bool:
    """Mirror of the form's branches (data-show-when in client/index.html): a question that was never shown on the customer's
    route is left out; every question that WAS shown is listed, blank ones as "Not provided" / "No preferences selected"."""
    doing, party = a.get("doing"), a.get("party")
    if field in ("school", "course", "level", "course_start", "course_end"):
        return doing == "Studying"
    if field == "work":
        return doing in ("Working", "Working holiday")
    if field in ("party_size", "party_members"):
        return party in ("Couple sharing one bed", "With friends")
    if field == "couples_answer":
        return party == "With friends"
    if field == "couples":
        return a.get("couples_answer") == "Yes"
    if field in ("features", "features_top"):   # asked since 8 Nov 2026; older records never saw the question
        return "features" in a and (field == "features" or bool(a.get("features")))
    if field == "travel_modes":   # asked since 16 Nov 2026 (H31): a record saved before then never saw the question, so it is not listed
        return "travel_modes" in a
    if field == "adults_confirm":   # pre-8 November 2026 records never answered this question; it is not shown for them
        return "adults_confirm" in a
    if field in ("intro_sex", "intro_age"):   # optional introduction-only questions asked since 8 Nov 2026; shown only when answered
        return a.get(field) not in (None, "")
    return True


def _answers(e: dict) -> dict:
    a = {}
    try:
        a = json.loads(e.get("answers_json") or "{}") or {}
    except Exception:
        a = {}
    return a if isinstance(a, dict) else {}


def receipt_rows(e: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(section title, [(question, answer), ...]), ...] from the saved record, customer questions only."""
    a = _answers(e)

    def get(f):
        v = a.get(f)
        if v in (None, "") and f in e and not a:  # column fallback only for records without a submitted payload (legacy rows)
            v = e.get(f)
        if isinstance(v, str) and f in LIST_FIELDS and v.startswith("["):
            try:
                v = json.loads(v)
            except Exception:
                pass
        return v

    out = []
    for title, fields in SECTIONS:
        items: list[tuple[str, str]] = []
        for f, q, kind in fields:
            if not applicable(f, a):
                continue
            v = get(f)
            if kind == "phone":
                text = _phone(a, e)
            elif kind == "budget":
                text = _budget_label(a, e)
            elif kind == "school":
                text = (f"{get('school_other')} ({v})" if get("school_other") else v) if v in SCHOOL_NAMED else (v or "")
            elif kind == "languages":
                lst = v if isinstance(v, list) else ([v] if v else [])
                other = get("languages_other")
                text = ", ".join([x for x in lst if x != "Other"] + ([f"Other: {other}"] if other else (["Other"] if "Other" in lst else [])))
            elif kind == "party":
                for q2, t2 in _party_members(a):
                    items.append((q2, t2))
                continue
            elif kind == "couples":
                text = _couples(a)
            elif kind == "intro_sex":   # the person's own term when "Another term"; never a legacy column value
                text = (get("intro_sex_other") or v) if v == ip.ANOTHER_TERM else (v or "")
            elif kind == "date":
                text = _fmt_date(v)
            elif kind == "list":
                text = _fmt_list(v)
            elif kind == "bool":
                text = _fmt_bool(v)
            else:
                text = "" if v is None else str(v)
            text = text.strip() if isinstance(text, str) else str(text)
            if not text:
                text = "No preferences selected" if f in LIST_FIELDS and f != "travel_modes" else "Not provided"
            items.append((q, text))
        if items:
            out.append((title, items))
    return out


def reference(enquiry_id) -> str:
    return f"MateBot-{str(enquiry_id).zfill(5)}"


def acknowledgement_intro(bot: str) -> list[str]:
    """Customer acknowledgement copy before the answers block (MateBot or HomeBot)."""
    form = "MateBot" if bot == "matebot" else "HomeBot"
    working = (
        "MateBot is working hard to find you compatible housemates and the right furnished apartment for your group."
        if bot == "matebot"
        else "HomeBot is working hard to find you the perfect furnished apartment."
    )
    return [
        f"Thank you for completing the {form} form.",
        "",
        working,
        "",
        "Here is a copy of the answers you gave:",
    ]


def signature_text_lines() -> list[str]:
    return [
        "Kind Regards,",
        *TEAM_LINES,
        "",
        "Ground, 10 St Andrews Place, East Melbourne, VIC 3002",
        "M: +61 405 780 780",
        "T: +61 3 8637 0220",
        f"E: {RESERVATIONS_TO}",
        "W: www.apartmentlivingmelbourne.com.au",
        "",
        "Consider the environment and think before you print.",
        "",
        "DISCLAIMER:",
        DISCLAIMER,
    ]


def signature_html() -> str:
    h = html.escape
    team_br = "<br>".join(h(line) for line in TEAM_LINES)
    return (
        f"<p>Kind Regards,<br>{team_br}</p>"
        f"<p><img src=\"{h(LOGO_URL)}\" alt=\"Apartment Living Melbourne\" "
        f"style=\"max-width:220px;height:auto;display:block;margin:12px 0\"></p>"
        f"<p>Ground, 10 St Andrews Place, East Melbourne, VIC 3002<br>"
        f"M: +61 405 780 780<br>T: +61 3 8637 0220<br>"
        f"E: {h(RESERVATIONS_TO)}<br>W: www.apartmentlivingmelbourne.com.au</p>"
        f"<p>Consider the environment and think before you print.</p>"
        f"<p><strong>DISCLAIMER:</strong><br>{h(DISCLAIMER)}</p>"
    )


def customer_full_name(e: dict) -> str:
    a = _answers(e)
    first = ((a.get("first_name") if a else None) or e.get("first_name") or "").strip()
    last = ((a.get("last_name") if a else None) or e.get("last_name") or "").strip()
    return f"{first} {last}".strip() or "Unknown"


def _sections_text(sections: list[tuple[str, list[tuple[str, str]]]]) -> list[str]:
    lines: list[str] = []
    for title, items in sections:
        lines += ["", title, "-" * len(title)]
        for q, v in items:
            lines.append(f"{q}: {v}")
    return lines


def _sections_html(sections: list[tuple[str, list[tuple[str, str]]]]) -> list[str]:
    h = html.escape
    parts: list[str] = []
    for title, items in sections:
        parts.append(
            f"<h3 style=\"font-size:1em;margin:1.2em 0 .3em\">{h(title)}</h3>"
            f"<table cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse\">"
        )
        for q, v in items:
            parts.append(
                f"<tr><td style=\"padding:2px 12px 2px 0;vertical-align:top;color:#555\">{h(q)}</td>"
                f"<td style=\"padding:2px 0;vertical-align:top\">{h(v)}</td></tr>"
            )
        parts.append("</table>")
    return parts


def render_receipt(e: dict) -> dict:
    """{'subject', 'text', 'html'} for a saved enquiry row (dict). Rendered from `answers_json` — the payload as submitted, which
    staff edits never change — through the allow-list above; nothing internal is read. Pure function of the record."""
    a = _answers(e)
    first = ((a.get("first_name") if a else None) or e.get("first_name") or "").strip() or "there"
    sections = receipt_rows(e)
    ref = reference(e.get("id"))
    is_test = bool(e.get("is_test"))
    subject = (TEST_SUBJECT_PREFIX + SUBJECT) if is_test else SUBJECT   # R2: TEST records are marked; real receipts unchanged
    test_note = [TEST_NOTE, ""] if is_test else []
    lines = [f"Hi {first},", ""] + test_note + acknowledgement_intro("matebot") + ["", f"Reference: {ref}"]
    lines += _sections_text(sections) + ["", LOCKED_IN_LINE, ""] + signature_text_lines()
    text = "\n".join(lines).rstrip() + "\n"

    h = html.escape
    parts = [f"<p>Hi {h(first)},</p>"] + ([f"<p><strong>{h(TEST_NOTE)}</strong></p>"] if is_test else [])
    for para in [p for p in acknowledgement_intro("matebot") if p]:
        parts.append(f"<p>{h(para)}</p>")
    parts.append(f"<p style=\"margin-top:0\">Reference: {h(ref)}</p>")
    parts += _sections_html(sections)
    parts.append(f"<p>{h(LOCKED_IN_LINE)}</p>")
    parts.append(signature_html())
    body = "\n".join(parts)
    html_doc = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{h(subject)}</title></head>"
                f"<body style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.45;color:#222;max-width:640px;margin:0 auto;padding:16px\">{body}</body></html>")
    return {"subject": subject, "text": text, "html": html_doc}


def render_reservations_notification(e: dict) -> dict:
    """Internal new-enquiry notice for reservations@ — name, contact and allow-listed answers only."""
    a = _answers(e)
    name = customer_full_name(e)
    email = ((a.get("email") if a else None) or e.get("email") or "").strip() or "Not provided"
    phone = _phone(a, e) or "Not provided"
    sections = receipt_rows(e)
    is_test = bool(e.get("is_test"))
    subject = f"MateBot enquiry from {name}"
    if is_test:
        subject = TEST_SUBJECT_PREFIX + subject
    lines = [
        "New MateBot enquiry received.",
        "",
        f"Name: {name}",
        f"Email: {email}",
        f"Phone: {phone}",
        "",
        "Answers:",
    ] + _sections_text(sections)
    text = "\n".join(lines).rstrip() + "\n"
    h = html.escape
    parts = [
        "<p>New MateBot enquiry received.</p>",
        f"<p>Name: {h(name)}<br>Email: {h(email)}<br>Phone: {h(phone)}</p>",
        "<h2 style=\"font-size:1.1em;margin:1.4em 0 .2em\">Answers</h2>",
    ] + _sections_html(sections)
    html_doc = (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{h(subject)}</title></head>"
                f"<body style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.45;color:#222;max-width:640px;margin:0 auto;padding:16px\">{''.join(parts)}</body></html>")
    return {"subject": subject, "text": text, "html": html_doc}


# ---------------------------------------------------------------------------
# transports


class CapturedTransport:
    """Local QA only: keeps the messages in memory, sends nothing."""

    def __init__(self, fail_times: int = 0, fail_with: Optional[type] = None):
        self.sent: list[EmailMessage] = []
        self.fail_times = fail_times
        self.fail_with = fail_with or ConnectionError
        self.calls = 0
        self.simulated = False

    def send(self, msg: EmailMessage) -> str:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with("captured transport: simulated failure")
        self.sent.append(msg)
        return msg["Message-ID"]


class DryRunTransport:
    """Renders and logs (ids only); nothing leaves the machine."""
    simulated = True

    def send(self, msg: EmailMessage) -> str:
        log.info("receipt dry-run: message %s prepared (not sent)", msg["Message-ID"])
        return msg["Message-ID"]


class SMTPTransport:
    simulated = False

    def __init__(self):
        self.host = (os.environ.get("MMAI_SMTP_HOST") or "").strip()
        self.port = int(os.environ.get("MMAI_SMTP_PORT") or 587)
        self.user = os.environ.get("MMAI_SMTP_USER") or ""
        self.password = os.environ.get("MMAI_SMTP_PASSWORD") or ""
        self.security = (os.environ.get("MMAI_SMTP_SECURITY") or "starttls").strip().lower()
        self.timeout = float(os.environ.get("MMAI_SMTP_TIMEOUT") or 20)

    def send(self, msg: EmailMessage) -> str:
        problem = config_problem()
        if problem:
            raise RuntimeError(problem)
        ctx = ssl.create_default_context()  # verified certificates and hostname; never a downgrade
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=ctx)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            server.ehlo()
            if self.security == "starttls":
                if not server.has_extn("starttls"):
                    raise RuntimeError("server does not offer STARTTLS")
                server.starttls(context=ctx)
                server.ehlo()
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return msg["Message-ID"]


def transport_from_env():
    mode = mail_mode()
    if mode == "smtp":
        return SMTPTransport()
    if mode == "graph":
        from . import mail_graph
        return mail_graph.GraphTransport(valid_email(os.environ.get("MMAI_MAIL_FROM")))
    if mode == "dryrun":
        return DryRunTransport()
    return None


def build_message(to_email: str, rendered: dict) -> EmailMessage:
    frm = valid_email(os.environ.get("MMAI_MAIL_FROM")) or "no-reply@invalid.local"
    name = (os.environ.get("MMAI_MAIL_FROM_NAME") or "Apartment Living Melbourne Reservations").replace("\r", " ").replace("\n", " ").strip()
    msg = EmailMessage()
    msg["Subject"] = rendered["subject"]
    msg["From"] = formataddr((name, frm))
    msg["To"] = to_email
    reply = valid_email(os.environ.get("MMAI_MAIL_REPLY_TO"))
    if reply:
        msg["Reply-To"] = reply
    msg["Message-ID"] = make_msgid(domain=frm.split("@", 1)[1] if "@" in frm else None)
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(rendered["text"])
    msg.add_alternative(rendered["html"], subtype="html")
    return msg


# ---------------------------------------------------------------------------
# outbox


def initial_status(to_email: Optional[str], is_test: bool) -> tuple[str, Optional[str]]:
    """Decided when the row is created (nothing is sent by default)."""
    if not to_email:
        return "failed", "recipient address invalid"
    if mail_mode() == "off":
        return "held", "mail disabled (MMAI_MAIL_MODE=off)"
    if is_test and not test_recipient_allowed(to_email):
        return "held", "test enquiry: sending to test data is not enabled for this recipient"
    return "queued", None


def enqueue_in_transaction(c, enquiry_id: int, email, is_test: bool, kind: str = KIND_RECEIPT) -> None:
    """Runs INSIDE create_enquiry's transaction (through on_inserted): no commit, no rollback here. The recipient is the address
    on the form as saved (snapshot), or the fixed reservations mailbox for KIND_RESERVATIONS. INSERT OR IGNORE keeps one row
    per enquiry/kind if the hook ever runs twice."""
    to = valid_email(email)
    status, err = initial_status(to, bool(is_test))
    now = now_iso()
    c.execute("INSERT OR IGNORE INTO email_outbox(enquiry_id, kind, to_email, status, attempts, last_error, created_at, updated_at) VALUES (?,?,?,?,0,?,?,?)",
              (enquiry_id, kind, to or str(email or "")[:254], status, err, now, now))


def enqueue_submission_emails(c, enquiry_id: int, email, is_test: bool) -> None:
    """Customer acknowledgement + reservations notification in the same enquiry transaction."""
    enqueue_in_transaction(c, enquiry_id, email, is_test, KIND_RECEIPT)
    enqueue_in_transaction(c, enquiry_id, RESERVATIONS_TO, is_test, KIND_RESERVATIONS)


def enqueue_receipt(c, enquiry_id: int, kind: str = KIND_RECEIPT) -> dict:
    """Idempotent repair/enqueue after the save. Returns {'outbox_id', 'status', 'created': bool}. Never raises."""
    try:
        existing = row(c.execute("SELECT id, status FROM email_outbox WHERE enquiry_id=? AND kind=?", (enquiry_id, kind)))
        if existing:
            return {"outbox_id": existing["id"], "status": existing["status"], "created": False}
        e = row(c.execute("SELECT id, email, is_test FROM enquiries WHERE id=?", (enquiry_id,)))
        if not e:
            return {"outbox_id": None, "status": "no_enquiry", "created": False}
        dest = RESERVATIONS_TO if kind == KIND_RESERVATIONS else e.get("email")
        with transaction(c):  # own short write block under the connection lock (commit on exit, rollback only of this block on error)
            enqueue_in_transaction(c, enquiry_id, dest, bool(e.get("is_test")), kind)
        r = row(c.execute("SELECT id, status FROM email_outbox WHERE enquiry_id=? AND kind=?", (enquiry_id, kind)))
        log.info("receipt enqueue enquiry=%s kind=%s outbox=%s status=%s", enquiry_id, kind, r and r["id"], r and r["status"])
        return {"outbox_id": r and r["id"], "status": r and r["status"], "created": True}
    except Exception as ex:  # a receipt problem must never turn a saved submission into an error
        log.warning("receipt enqueue failed enquiry=%s kind=%s error=%s", enquiry_id, kind, type(ex).__name__)
        return {"outbox_id": None, "status": "error", "created": False, "error": type(ex).__name__}


def _backoff(attempts: int) -> str:
    mins = BACKOFF_MIN[min(max(attempts - 1, 0), len(BACKOFF_MIN) - 1)]
    return (datetime.now(timezone.utc) + timedelta(minutes=mins)).replace(microsecond=0).isoformat()


_deliver_lock = threading.Lock()


def _set(c, oid: int, **cols) -> None:
    cols["updated_at"] = now_iso()
    with transaction(c):
        c.execute(f"UPDATE email_outbox SET {', '.join(k + '=?' for k in cols)} WHERE id=?", [*cols.values(), oid])


def deliver_pending(c, transport=None, limit: int = 25, now: Optional[str] = None) -> dict:
    """Send queued receipts. Each row is claimed with a conditional UPDATE (queued → sending) in its own committed transaction
    BEFORE the network call, so a concurrent pass skips it and a crash mid-send leaves a visible `sending` row rather than a
    duplicate. The SMTP call itself runs with no transaction open and no connection lock held.
    Returns counts. Safe to call from a request thread, the scheduler or the CLI."""
    counts = {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "cancelled": 0, "skipped": 0}
    transport = transport or transport_from_env()
    if transport is None:
        return counts  # mode off: nothing moves
    problem = config_problem()
    if problem and not getattr(transport, "simulated", False) and not isinstance(transport, CapturedTransport):
        log.warning("receipt delivery skipped: %s", problem)
        return counts
    now = now or now_iso()
    with _deliver_lock:
        with c._mmai_lock:
            pending = rows(c.execute("SELECT id FROM email_outbox WHERE status='queued' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?", (now, limit)))
        for p in pending:
            oid = p["id"]
            with transaction(c):  # atomic claim: only one pass can move queued → sending
                cur = c.execute("UPDATE email_outbox SET status='sending', attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'", (now_iso(), oid))
                claimed = cur.rowcount == 1
            if not claimed:
                counts["skipped"] += 1
                continue
            with c._mmai_lock:
                ob = row(c.execute("SELECT * FROM email_outbox WHERE id=?", (oid,)))
            if not ob:  # the enquiry (and, by cascade, this row) was deleted between the claim and now
                counts["cancelled"] += 1
                continue
            with c._mmai_lock:
                e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (ob["enquiry_id"],)))
            if not e:
                _set(c, oid, status="cancelled", last_error="enquiry no longer exists")
                counts["cancelled"] += 1
                continue
            to = valid_email(ob["to_email"])  # immutable snapshot of the address supplied on the form; never from the request
            kind = ob.get("kind") or KIND_RECEIPT
            is_reservations = kind == KIND_RESERVATIONS
            if not to or (e.get("is_test") and not test_recipient_allowed(to) and not isinstance(transport, CapturedTransport)):
                _set(c, oid, status="held", last_error="recipient invalid" if not to else "test enquiry: sending not enabled for this recipient")
                counts["skipped"] += 1
                continue
            if not is_reservations and (e.get("email") or "").strip().lower() != to.lower():
                # the record's address was changed after submission (staff correction, or a different person on the record now):
                # never send the original answers to the old address, and never retarget automatically — staff decide
                _set(c, oid, status="held", last_error="record email changed since submission; not sent")
                counts["skipped"] += 1
                continue
            if is_reservations and to.lower() != RESERVATIONS_TO:
                _set(c, oid, status="held", last_error="reservations recipient mismatch; not sent")
                counts["skipped"] += 1
                continue
            attempts = ob["attempts"]  # already counts this attempt (incremented by the claim)
            try:
                rendered = render_reservations_notification(e) if is_reservations else render_receipt(e)
                msg = build_message(to, rendered)
                mid = transport.send(msg)  # no transaction open, no lock held
            except Exception as ex:
                err = f"{type(ex).__name__}"[:120]
                if _ambiguous(ex):
                    _set(c, oid, last_error=err)  # remains sending; manual reconciliation, not an automatic duplicate
                    counts["skipped"] += 1
                elif _permanent_stop(ex):
                    # Graph said no (configuration, authorisation 401/403, or a definitive 4xx rejection): retrying the same request
                    # cannot succeed and must not happen automatically - the row is left `failed` for a person to look at.
                    # SMTP and network failures keep the bounded retry below, exactly as before.
                    _set(c, oid, status="failed", last_error=err)
                    counts["failed"] += 1
                    log.warning("receipt stopped, not retried outbox=%s enquiry=%s error=%s", oid, e["id"], err)
                elif attempts >= MAX_ATTEMPTS:
                    _set(c, oid, status="failed", last_error=err)
                    counts["failed"] += 1
                    log.warning("receipt failed permanently outbox=%s enquiry=%s error=%s", oid, e["id"], err)
                else:
                    _set(c, oid, status="queued", last_error=err, next_attempt_at=_backoff(attempts))
                    counts["retry"] += 1
                    log.info("receipt retry scheduled outbox=%s enquiry=%s attempt=%s error=%s", oid, e["id"], attempts, err)
                continue
            # Provider acceptance followed by a database fault is not a send failure.
            status = "dryrun" if getattr(transport, "simulated", False) else "sent"
            _set(c, oid, status=status, message_id=mid, last_error=None, sent_at=now_iso())
            counts[status] += 1
            log.info("receipt %s outbox=%s enquiry=%s", status, oid, e["id"])
            if row(c.execute("SELECT 1 FROM email_outbox WHERE id=?", (oid,))) is None:
                pass  # deleted during send: nothing more to record (cascade already removed the row)
    return counts


def _permanent_stop(ex: BaseException) -> bool:
    """True for the Graph transport's definitive answers (GraphConfigError, GraphAuthError, GraphRejected): never retried
    automatically. GraphAmbiguous is parked separately; definite GraphUnavailable, smtplib and OS errors stay retryable."""
    from . import mail_graph
    return isinstance(ex, (mail_graph.GraphConfigError, mail_graph.GraphAuthError, mail_graph.GraphRejected))


def _ambiguous(ex: BaseException) -> bool:
    from . import mail_graph
    return isinstance(ex, mail_graph.GraphAmbiguous)


def kick_delivery(c) -> Optional[threading.Thread]:
    """Start a background delivery pass when mail is enabled. Never raises; returns the thread (None when mode is off)."""
    if mail_mode() == "off":
        return None

    def run():
        try:
            deliver_pending(c)
        except Exception as ex:
            log.warning("receipt delivery pass error=%s", type(ex).__name__)

    t = threading.Thread(target=run, daemon=True, name="mmai-receipts")
    t.start()
    return t


def after_saved(c, enquiry_id: int) -> dict:
    """After the enquiry transaction committed: repair-enqueue customer + reservations kinds if bypassed (idempotent), then,
    if mail is enabled and any row is queued, deliver in the background. Never raises. Returns the customer-receipt status
    (API/staff still key off that kind)."""
    try:
        res = enqueue_receipt(c, enquiry_id, KIND_RECEIPT)
        res_r = enqueue_receipt(c, enquiry_id, KIND_RESERVATIONS)
        if res.get("status") == "queued" or res_r.get("status") == "queued":
            kick_delivery(c)
        return res
    except Exception as ex:
        log.warning("receipt hook error enquiry=%s error=%s", enquiry_id, type(ex).__name__)
        return {"outbox_id": None, "status": "error", "created": False}


def outbox_status(c, enquiry_id: int) -> Optional[dict]:
    r = row(c.execute("SELECT id, kind, status, attempts, last_error, created_at, updated_at, sent_at FROM email_outbox WHERE enquiry_id=? AND kind=?", (enquiry_id, KIND_RECEIPT)))
    return r


if __name__ == "__main__":  # ops: python -m server.receipts deliver | status
    import sys
    from . import db as _db
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    conn = _db.connect()
    if cmd == "deliver":
        print(json.dumps(deliver_pending(conn)))
    else:
        print(json.dumps({"mode": mail_mode(), "config_problem": config_problem(),
                          "counts": {r["status"]: r["n"] for r in rows(conn.execute("SELECT status, COUNT(*) n FROM email_outbox GROUP BY status"))}}))
