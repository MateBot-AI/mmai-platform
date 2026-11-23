"""I19(q) automatic customer receipts (5 November 2026) — outbox, rendering and the save-after-success hook.

Every test runs against a temporary database (the `client` fixture) with a captured in-memory transport: no SMTP, no network,
no real email. Covers: disabled by default (held, never "sent"); one receipt per saved enquiry however often the submission is
replayed; the recipient comes from the saved record; mail failure never fails or duplicates the submission; retry with back-off
and a permanent failure cap; test enquiries are held unless explicitly allowed; deleting the enquiry removes the queued receipt;
the body contains the customer's questions and answers (escaped) and never staff notes or internal fields; the staff phone
path produces no automatic receipt.
"""
import importlib
import json

import pytest
from fastapi.testclient import TestClient

from server import receipts, submissions
from server.db import row, rows
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)

KEY = "k_" + "r" * 30


def _post(anon, answers, key=KEY):
    return anon.post("/api/public/enquiries", json=answers, headers={submissions.HEADER: key} if key else {})


def _outbox(c, eid=None, kind=receipts.KIND_RECEIPT):
    """Customer-receipt rows by default (reservations kind is asserted separately)."""
    if eid is None:
        return rows(c.execute("SELECT * FROM email_outbox WHERE kind=? ORDER BY id", (kind,)))
    return rows(c.execute("SELECT * FROM email_outbox WHERE enquiry_id=? AND kind=? ORDER BY id", (eid, kind)))


def _all_outbox(c, eid=None):
    if eid is None:
        return rows(c.execute("SELECT * FROM email_outbox ORDER BY id"))
    return rows(c.execute("SELECT * FROM email_outbox WHERE enquiry_id=? ORDER BY id", (eid,)))


@pytest.fixture
def mail_off(monkeypatch):
    monkeypatch.delenv("MMAI_MAIL_MODE", raising=False)
    monkeypatch.delenv("MMAI_MAIL_SEND_TEST", raising=False)
    monkeypatch.delenv("MMAI_MAIL_TEST_ALLOW", raising=False)


@pytest.fixture
def mail_dryrun(monkeypatch):
    """Delivery enabled for the tests, but the app's own background pass is replaced by a captured transport so nothing leaves."""
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun")
    monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    monkeypatch.delenv("MMAI_MAIL_SEND_TEST", raising=False)
    monkeypatch.delenv("MMAI_MAIL_TEST_ALLOW", raising=False)
    monkeypatch.setattr(receipts, "kick_delivery", lambda c: None)  # tests drive deliver_pending() themselves


# ---------------- disabled by default ----------------
def test_default_mode_is_off_and_records_held_without_claiming_sent(client, mail_off):
    anon = TestClient(client.app)
    c = appdb(client)
    r = _post(anon, good_answers(is_test=False))
    assert r.status_code == 201 and r.json()["receipt"] == "held"
    eid = r.json()["id"]
    ob = _outbox(c, eid)
    assert len(ob) == 1 and ob[0]["status"] == "held" and ob[0]["attempts"] == 0 and ob[0]["sent_at"] is None
    assert "disabled" in ob[0]["last_error"]
    reserved = _outbox(c, eid, receipts.KIND_RESERVATIONS)
    assert len(reserved) == 1 and reserved[0]["to_email"] == receipts.RESERVATIONS_TO and reserved[0]["status"] == "held"
    # a delivery pass while off moves nothing and sends nothing
    assert receipts.deliver_pending(c) == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "cancelled": 0, "skipped": 0}
    assert _outbox(c, eid)[0]["status"] == "held"
    # the outbox never stores a body: only the recipient and state
    assert set(ob[0].keys()) >= {"enquiry_id", "kind", "to_email", "status"} and "body" not in " ".join(ob[0].keys())


# ---------------- one receipt per saved enquiry ----------------
def test_replayed_submission_queues_exactly_one_receipt(client, mail_dryrun):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=False)
    r1 = _post(anon, a)
    r2 = _post(anon, a)  # double click / retry after a lost reply
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["replayed"] is True
    assert len(_outbox(c)) == 1 and _outbox(c)[0]["status"] == "queued"
    assert len(_all_outbox(c, r1.json()["id"])) == 2
    # enqueue called again directly (e.g. a replay after the side effects were lost) is a no-op
    again = receipts.enqueue_receipt(c, r1.json()["id"])
    assert again["created"] is False and len(_outbox(c)) == 1
    t = receipts.CapturedTransport()
    assert receipts.deliver_pending(c, t)["sent"] == 2  # customer + reservations
    assert receipts.deliver_pending(c, t)["sent"] == 0 and len(t.sent) == 2  # a second pass does not resend
    ob = _outbox(c)[0]
    assert ob["status"] == "sent" and ob["attempts"] == 1 and ob["message_id"] and ob["sent_at"]
    assert _outbox(c, r1.json()["id"], receipts.KIND_RESERVATIONS)[0]["status"] == "sent"
    assert t.sent[0]["To"] == "t@example.com"
    assert t.sent[1]["To"] == receipts.RESERVATIONS_TO


def test_receipt_row_is_written_in_the_same_transaction_as_the_enquiry(client, mail_dryrun, monkeypatch):
    anon = TestClient(client.app)
    c = appdb(client)
    # after-save repair disabled: the row must already exist from the atomic hook
    monkeypatch.setattr(receipts, "enqueue_receipt", lambda *a, **k: {"outbox_id": None, "status": "repair-disabled", "created": False})
    r = _post(anon, good_answers(is_test=False))
    assert r.status_code == 201
    ob = _outbox(c, r.json()["id"])
    assert len(ob) == 1 and ob[0]["status"] == "queued" and ob[0]["to_email"] == "t@example.com"
    assert len(_all_outbox(c, r.json()["id"])) == 2
    # a submission that is rolled back leaves no outbox row behind (fencing: Superseded raised inside the transaction)
    monkeypatch.undo()
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun"); monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    monkeypatch.setattr(receipts, "kick_delivery", lambda c: None)
    monkeypatch.setattr(submissions, "mark_saved", lambda c, sub, eid: (_ for _ in ()).throw(submissions.Superseded(sub.key)))
    n_enq = c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0]
    try:
        _post(anon, good_answers(is_test=False, email="rolled@example.com"), key="k_" + "x" * 30)
    except Exception:
        pass
    assert c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == n_enq
    assert c.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email='rolled@example.com'").fetchone()[0] == 0
    # and the stronger case: the outbox row IS inserted (real enqueue_in_transaction runs) and the transaction fails AFTERWARDS;
    # both the new enquiry and its outbox row must be rolled back together
    monkeypatch.undo()
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun"); monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    monkeypatch.setattr(receipts, "kick_delivery", lambda c: None)
    real_enqueue = receipts.enqueue_in_transaction
    seen = {}

    def enqueue_then_fail(c, eid, email, is_test, kind=receipts.KIND_RECEIPT):
        real_enqueue(c, eid, email, is_test, kind)
        seen["inside"] = c.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email='after@example.com'").fetchone()[0]
        raise RuntimeError("simulated failure after the outbox insert, inside the enquiry transaction")
    monkeypatch.setattr(receipts, "enqueue_submission_emails",
                        lambda c, eid, email, is_test: enqueue_then_fail(c, eid, email, is_test))
    n_enq = c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0]
    try:
        r = _post(anon, good_answers(is_test=False, email="after@example.com"), key="k_" + "y" * 30)
        assert r.status_code >= 500
    except Exception:
        pass
    assert seen.get("inside") == 1, "the real enqueue must have inserted the row before the fault"
    assert c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == n_enq
    assert c.execute("SELECT COUNT(*) FROM enquiries WHERE email='after@example.com'").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email='after@example.com'").fetchone()[0] == 0


def test_recipient_comes_from_the_saved_record_and_is_a_single_address(client, mail_dryrun):
    anon = TestClient(client.app)
    c = appdb(client)
    r = _post(anon, good_answers(is_test=False, email="Person@Example.com"))
    eid = r.json()["id"]
    t = receipts.CapturedTransport()
    receipts.deliver_pending(c, t)
    msg = t.sent[0]
    saved = row(c.execute("SELECT email FROM enquiries WHERE id=?", (eid,)))["email"]
    assert msg["To"] == saved == "person@example.com" and "," not in msg["To"] and msg["From"].endswith("<hello@example.com>")
    assert msg["Subject"] == receipts.SUBJECT and msg["Auto-Submitted"] == "auto-generated"
    # the record's address is changed by staff before delivery: the customer receipt is held — not sent to the old address
    c.execute("UPDATE enquiries SET email='staff-corrected@example.com', first_name='Someone Else' WHERE id=?", (eid,)); c.commit()
    c.execute("UPDATE email_outbox SET status='queued' WHERE enquiry_id=? AND kind=?", (eid, receipts.KIND_RECEIPT)); c.commit()
    before = len(t.sent)
    res = receipts.deliver_pending(c, t)
    ob = _outbox(c, eid)[0]
    assert res["sent"] == 0 and ob["status"] == "held" and "changed since submission" in ob["last_error"] and len(t.sent) == before
    # a corrupted snapshot is never sent to
    c.execute("UPDATE enquiries SET email='person@example.com' WHERE id=?", (eid,)); c.commit()
    c.execute("UPDATE email_outbox SET status='queued', to_email='bad address, two@example.com' WHERE enquiry_id=? AND kind=?",
              (eid, receipts.KIND_RECEIPT)); c.commit()
    res = receipts.deliver_pending(c, t)
    assert res["sent"] == 0 and _outbox(c, eid)[0]["status"] == "held" and len(t.sent) == before
    # staff edits to the columns never change the receipt content: it is rendered from the submitted payload
    c.execute("UPDATE enquiries SET last_name='Edited' WHERE id=?", (eid,)); c.commit()
    assert "Last name: Person" in receipts.render_receipt(row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,))))["text"]
    assert receipts.valid_email("a@b.co") and not receipts.valid_email("a@b.co\r\nBcc: x@y.z") and not receipts.valid_email("Name <a@b.co>")


# ---------------- failure never breaks the submission ----------------
def test_mail_failure_never_fails_or_duplicates_the_submission(client, mail_dryrun, monkeypatch):
    anon = TestClient(client.app)
    c = appdb(client)
    # 1) the enqueue itself blows up → the submit still succeeds, one enquiry, no exception
    monkeypatch.setattr(receipts, "enqueue_receipt", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _post(anon, good_answers(is_test=False))
    assert r.status_code == 201 and r.json()["id"] and r.json()["receipt"] == "error"
    assert c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM enquiry_submissions WHERE status='done'").fetchone()[0] == 1
    # the atomic hook still recorded the intent: the row exists and is queued although the after-save step blew up
    assert len(_outbox(c)) == 1 and _outbox(c)[0]["status"] == "queued"
    c.execute("UPDATE email_outbox SET status='sent'"); c.commit()  # park it so part 2 sees one row only
    monkeypatch.undo()
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun"); monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    monkeypatch.setattr(receipts, "kick_delivery", lambda c: None)
    # 2) the transport fails: the customer row retries with back-off, the enquiry is untouched, no duplicate row
    r = _post(anon, good_answers(is_test=False, email="second@example.com"), key="k_" + "s" * 30)
    eid = r.json()["id"]
    c.execute("UPDATE email_outbox SET status='sent' WHERE enquiry_id=? AND kind=?", (eid, receipts.KIND_RESERVATIONS)); c.commit()
    t = receipts.CapturedTransport(fail_times=2)
    res = receipts.deliver_pending(c, t)
    ob = _outbox(c, eid)[0]
    assert res["retry"] == 1 and ob["status"] == "queued" and ob["attempts"] == 1 and ob["last_error"] == "ConnectionError" and ob["next_attempt_at"] > ob["updated_at"][:10]
    # not due yet → skipped; due → tried again, fails again, then succeeds on the third attempt
    assert receipts.deliver_pending(c, t)["retry"] == 0
    assert receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")["retry"] == 1
    assert receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")["sent"] == 1
    assert len(t.sent) == 1 and len(_outbox(c, eid)) == 1 and _outbox(c, eid)[0]["attempts"] == 3
    assert c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 2


def test_permanent_failure_after_max_attempts(client, mail_dryrun):
    anon = TestClient(client.app)
    c = appdb(client)
    eid = _post(anon, good_answers(is_test=False)).json()["id"]
    c.execute("UPDATE email_outbox SET status='sent' WHERE enquiry_id=? AND kind=?", (eid, receipts.KIND_RESERVATIONS)); c.commit()
    t = receipts.CapturedTransport(fail_times=99)
    for _ in range(receipts.MAX_ATTEMPTS):
        receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")
    ob = _outbox(c, eid)[0]
    assert ob["status"] == "failed" and ob["attempts"] == receipts.MAX_ATTEMPTS and not t.sent
    assert receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")["failed"] == 0  # not retried for ever


# ---------------- test data is never emailed unless explicitly allowed ----------------
def test_test_enquiries_are_held_unless_explicitly_allowed(client, mail_dryrun, monkeypatch):
    anon = TestClient(client.app)
    c = appdb(client)
    r = _post(anon, good_answers(is_test=True))
    assert r.json()["receipt"] == "held" and "test enquiry" in _outbox(c, r.json()["id"])[0]["last_error"]
    # enabling test sends still requires the recipient to be on the allow list
    monkeypatch.setenv("MMAI_MAIL_SEND_TEST", "1"); monkeypatch.setenv("MMAI_MAIL_TEST_ALLOW", "@qa.example.org, bill@example.com")
    r2 = _post(anon, good_answers(is_test=True, email="someone@else.com"), key="k_" + "t" * 30)
    assert r2.json()["receipt"] == "held"
    r3 = _post(anon, good_answers(is_test=True, email="tester@qa.example.org"), key="k_" + "u" * 30)
    assert r3.json()["receipt"] == "queued"
    monkeypatch.delenv("MMAI_MAIL_SEND_TEST")
    assert not receipts.test_recipient_allowed("tester@qa.example.org")


# ---------------- deletion removes the queued receipt ----------------
def test_deleting_the_enquiry_removes_the_queued_receipt(client, mail_dryrun, monkeypatch):
    anon = TestClient(client.app)
    c = appdb(client)
    eid = _post(anon, good_answers(is_test=False)).json()["id"]
    assert len(_outbox(c, eid)) == 1
    assert client.delete(f"/api/enquiries/{eid}").status_code == 200
    assert _outbox(c, eid) == []
    t = receipts.CapturedTransport()
    assert receipts.deliver_pending(c, t)["sent"] == 0 and not t.sent
    # delete test data clears test receipts too
    eid2 = _post(anon, good_answers(is_test=True), key="k_" + "v" * 30).json()["id"]
    assert len(_outbox(c, eid2)) == 1
    client.delete("/api/test-data")
    assert _outbox(c, eid2) == []
    # a row claimed for sending whose enquiry vanished mid-way is cancelled, not sent
    eid3 = _post(anon, good_answers(is_test=False, email="third@example.com"), key="k_" + "w" * 30).json()["id"]
    c.execute("UPDATE email_outbox SET status='sent' WHERE enquiry_id=? AND kind=?", (eid3, receipts.KIND_RESERVATIONS)); c.commit()
    # (portable simulation for SQLite and Postgres: the enquiry row is removed while the outbox row is still present)
    real_row = receipts.row

    def row_without_enquiry(cur):
        r = real_row(cur)
        return None if (r and "answers_json" in r and r.get("id") == eid3) else r
    monkeypatch.setattr(receipts, "row", row_without_enquiry)
    assert receipts.deliver_pending(c, t)["cancelled"] == 1 and not t.sent
    monkeypatch.undo()
    ob3 = _outbox(c, eid3)
    assert ob3 and ob3[0]["status"] == "cancelled"


# ---------------- content ----------------
def test_receipt_contains_customer_questions_and_answers_but_nothing_internal(client, mail_dryrun):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=False, email="t@example.com")
    a["else_text"] = "Allergic to <cats> & \"dogs\""
    a["intro"] = ""
    a["areas"] = []
    eid = _post(anon, a).json()["id"]
    c.execute("UPDATE enquiries SET staff_notes='SECRET STAFF NOTE', status='Contacted' WHERE id=?", (eid,)); c.commit()
    e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    r = receipts.render_receipt(e)
    text, html_ = r["text"], r["html"]
    assert text.startswith("Hi Test,") and "Here is a copy of the answers you gave:" in text
    assert "Thank you for completing the MateBot form." in text
    assert "MateBot is working hard to find you compatible housemates" in text
    assert receipts.LOCKED_IN_LINE in text
    assert text.index(receipts.LOCKED_IN_LINE) < text.index("Kind Regards,")
    assert "Kind Regards," in text
    assert "Apartment Living Melbourne" in text and "Reservation Team" in text
    assert "Apartment Living Melbourne\nReservation Team" in text
    assert "DISCLAIMER:" in text and f"Reference: MateBot-{str(eid).zfill(5)}" in text
    assert receipts.LOGO_URL in html_
    assert "First name: Test" in text and "Email: t@example.com" in text and "Mobile number: +61 0400000000" in text
    assert "I confirm that everyone included in this enquiry, including me, will be 18 or older on the move-in date. If that is not the case, please contact the ALM team instead of continuing.: Yes" in text
    for gone in ("Date of birth", "Sex:", "I confirm I will be 18 or older", "Parent or guardian"):
        assert gone not in text, gone   # I8: nothing about sex or age is asked or echoed
    assert "Send me ALM offers and updates.: " in text  # unticked optional consent still shown (Yes/No)
    assert "A short introduction about yourself for your future housemates: Not provided" in text
    assert "Preferred areas: No preferences selected" in text
    # questions shown on this route (Studying) appear even when blank; questions from other branches do not
    for shown in ("Course name: Not provided", "Level: Not provided", "Course start date: Not provided", "Your daily routine: Not provided",
                  "How tidy are you?: Not provided", "Diet or kitchen needs: Not provided", "Maximum travel time to campus or work: Not provided",
                  "Where will you study?: RMIT University – City campus"):
        assert shown in text, shown
    for hidden in ("Where will you work?", "Specific countries", "Regions you would prefer", "Couples in your group", "Parent or guardian name", "People enquiring with you"):
        assert hidden not in text, hidden
    assert 'Anything else we should know?: Allergic to <cats> & "dogs"' in text
    assert "Allergic to &lt;cats&gt; &amp; &quot;dogs&quot;" in html_ and "<cats>" not in html_
    assert "2 bedroom / 2 bathroom" in text and "per week (6-month price guide)" in text  # A6: 1 Feb → 10 Jul same year
    for forbidden in ("SECRET STAFF NOTE", "staff_notes", "Contacted", "submission_key", "is_test", "budget_max", "answers_json", "birth_country_confirmed", KEY):
        assert forbidden not in text and forbidden not in html_
    # the message carries both parts
    msg = receipts.build_message("t@example.com", r)
    parts = {p.get_content_type() for p in msg.walk()}
    assert {"text/plain", "text/html"} <= parts


def test_branches_from_actual_saved_payloads(client, mail_dryrun):
    anon = TestClient(client.app)
    c = appdb(client)
    # working holiday → work question shown (blank → Not provided), study questions hidden
    eid = _post(anon, good_answers(is_test=False, doing="Working holiday", school=""), key="k_" + "b" * 30).json()["id"]
    t = receipts.render_receipt(row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,))))["text"]
    assert "Where will you work?: Not provided" in t and "Course name" not in t and "Where will you study?" not in t
    # English language school → the typed name is shown with the choice
    eid = _post(anon, good_answers(is_test=False, school="English language school (enter name)", school_other="Impact English",
                                   features=["Balcony", "Gym", "Views"], features_top=["Gym"]), key="k_" + "c" * 30).json()["id"]
    t = receipts.render_receipt(row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,))))["text"]
    assert "Where will you study?: Impact English (English language school (enter name))" in t
    # 3.11/3.12 feature wishes are echoed in catalogue order; the retired origin questions (legacy 5.3/5.4) never appear
    assert "Which apartment and building features would you particularly like?: Views, Balcony, Gym" in t
    assert "Which three matter most to you?: Gym" in t
    assert "prefer not to live with" not in t and "Specific countries" not in t and "own country" not in t.lower()
    # no features ticked → the "matter most" row is not asked, so it is not echoed either
    eid = _post(anon, good_answers(is_test=False, features=[]), key="k_" + "d" * 30).json()["id"]
    t = receipts.render_receipt(row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,))))["text"]
    assert "Which apartment and building features would you particularly like?: No preferences selected" in t and "matter most" not in t


def test_party_and_couples_are_rendered_by_name(client, mail_dryrun):
    c = appdb(client)
    e = {"id": 7, "first_name": "Ana", "email": "ana@example.com", "answers_json": json.dumps({
        "first_name": "Ana", "last_name": "Lee", "email": "ana@example.com", "phone_code": "+61", "phone": "0400", "party": "With friends", "party_size": 3,
        "party_members": [{"name": "Bea", "email": "b@example.com", "sex": "Female", "dob": "2000-01-01", "birth_country_raw": "Spain"},
                          {"name": "Carlos", "email": "c@example.com", "sex": "Male", "dob": "2000-01-02", "birth_country_raw": "Spain"}],
        "couples_answer": "Yes", "couples": [["m1", "m2"]], "languages": ["English", "Other"], "languages_other": "Catalan",
        "move_in": "2027-02-01", "move_out": "2027-07-20", "budget_option": "2B2B-single", "sex": "Other", "sex_other": "Non-binary", "doing": "Studying", "school": "Other", "school_other": "Example College"})}
    t = receipts.render_receipt(e)["text"]
    # a legacy payload (saved before 8 Nov 2026) still holds sex/dob: the receipt never echoes them
    assert "Person 2: Bea: email b@example.com; country of birth Spain" in t and "Female" not in t and "born 1 January" not in t
    assert "How many people in total, including you?: 3" in t
    assert "Couples in your group: Bea and Carlos" in t
    assert "Languages you speak: English, Other: Catalan" in t
    assert "Sex" not in t and "Non-binary" not in t and "Where will you study?: Example College (Other)" in t and "Where will you work?" not in t
    assert "18 or older" not in t   # the legacy record never answered the all-occupants question; it is not shown, let alone implied
    assert "(6-month price guide)" in t  # A6: Feb → July same year


# ---------------- staff phone path: no automatic receipt ----------------
def test_staff_phone_enquiry_gets_no_automatic_receipt(client, mail_dryrun):
    c = appdb(client)
    r = client.post("/api/enquiries", json=dict(good_answers(is_test=True), channel="Phone"))
    assert r.status_code == 201
    assert _outbox(c) == []  # I19(p) manual email copy remains the staff route


# ---------------- configuration guard ----------------
def test_smtp_mode_without_configuration_sends_nothing(client, monkeypatch):
    anon = TestClient(client.app)
    c = appdb(client)
    monkeypatch.setenv("MMAI_MAIL_MODE", "smtp")
    monkeypatch.delenv("MMAI_MAIL_FROM", raising=False); monkeypatch.delenv("MMAI_SMTP_HOST", raising=False)
    monkeypatch.setattr(receipts, "kick_delivery", lambda c: None)
    eid = _post(anon, good_answers(is_test=False)).json()["id"]
    assert receipts.config_problem() == "MMAI_MAIL_FROM missing or invalid"
    assert receipts.deliver_pending(c) == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "cancelled": 0, "skipped": 0}
    assert _outbox(c, eid)[0]["status"] == "queued"  # waits for configuration; nothing claimed, nothing lost
    monkeypatch.setenv("MMAI_MAIL_FROM", "not an address")
    assert receipts.config_problem() == "MMAI_MAIL_FROM missing or invalid"
    monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    assert receipts.config_problem() == "MMAI_SMTP_HOST missing"
    monkeypatch.setenv("MMAI_SMTP_HOST", "smtp.example.com"); monkeypatch.setenv("MMAI_SMTP_SECURITY", "none")
    assert "only allowed for a local relay" in receipts.config_problem()  # no plaintext to a remote host, ever
    monkeypatch.setenv("MMAI_SMTP_SECURITY", "plain")
    assert "must be starttls, ssl or none" in receipts.config_problem()
    monkeypatch.setenv("MMAI_SMTP_SECURITY", "starttls")
    assert receipts.config_problem() is None
    # a transport that cannot be configured refuses to send rather than downgrading
    monkeypatch.setenv("MMAI_SMTP_SECURITY", "none")
    with pytest.raises(RuntimeError):
        receipts.SMTPTransport().send(receipts.build_message("t@example.com", {"subject": "s", "text": "t", "html": "<p>t</p>"}))


# ---------------- scheduler hand-off ----------------
def test_hourly_scheduler_leaves_delivery_to_independent_dispatcher(client, monkeypatch):
    """Notification dispatch no longer depends on an hourly matching pass."""
    from server import scheduler
    c = appdb(client)

    class _Stop(BaseException):  # not Exception: passes the loop's try/except and ends the endless loop after one pass
        pass

    calls = {"check": 0, "deliver": [], "threads": []}

    def fake_check_all(conn, trigger):
        calls["check"] += 1  # 1 = start-up catch-up (last_check is None), 2 = first hourly check, 3 = stop after one full pass
        if calls["check"] >= 3:
            raise _Stop()
    monkeypatch.setattr(scheduler, "check_all", fake_check_all)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: None)
    monkeypatch.setattr(scheduler, "_setting", lambda conn, key, value=None: None)

    def spy_kick(conn):
        calls["threads"].append("unexpected hourly delivery")
    monkeypatch.setattr(receipts, "kick_delivery", spy_kick)

    # mode off: the hourly pass starts no delivery thread and never calls deliver_pending
    monkeypatch.setenv("MMAI_MAIL_MODE", "off")
    with pytest.raises(_Stop):
        scheduler._loop(c)
    assert calls["threads"] == [] and calls["deliver"] == []

    # Enabled delivery is also independent of the matching loop.
    calls.update(check=0, deliver=[], threads=[])
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun"); monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    with pytest.raises(_Stop):
        scheduler._loop(c)
    assert calls["threads"] == [] and calls["deliver"] == []
