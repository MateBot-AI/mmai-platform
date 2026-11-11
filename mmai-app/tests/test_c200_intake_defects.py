"""C200 intake defects found by the 200-person browser acceptance run (9 November 2026) — regression tests.

D1  A public submission carried the hidden Staff-only section's DOM default (channel "Phone") into answers_json while the
    enquiry's channel column correctly said "Landing page". Public submissions now drop the staff-only keys before validation and
    storage; the staff Phone route still stores them (channel boundary kept both ways); the submission fingerprint is taken from the
    raw body, so a replay of the same raw body still replays.
R1  The customer receipt printed the phone-code select's literal value when the code is not listed: "Mobile number: other 049…".
R2  Receipts for TEST-mode records carry a TEST marker in subject and body; real receipts are unchanged (same subject, no note).
"""
import html
import json

import pytest
from fastapi.testclient import TestClient

from server import enquiries, receipts, submissions
from server.db import row
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)

KEY = "k_" + "c" * 30


def _anon(client):
    return TestClient(client.app)


def _answers(c, eid):
    return json.loads(row(c.execute("SELECT answers_json FROM enquiries WHERE id=?", (eid,)))["answers_json"])


# ---------------------------------------------------------------- D1
def test_public_submission_drops_staff_only_keys(client):
    anon, c = _anon(client), appdb(client)
    body = dict(good_answers(), channel="Phone", campaign="spring", staff_member="Anna Chan", staff_notes="x", enq_date="9 November 2026")
    r = anon.post("/api/public/enquiries", json=body, headers={submissions.HEADER: KEY})
    assert r.status_code == 201 and r.json()["channel"] == "Landing page"
    e = row(c.execute("SELECT channel, campaign, staff_member, staff_notes FROM enquiries WHERE id=?", (r.json()["id"],)))
    assert e["channel"] == "Landing page" and not e["campaign"] and not e["staff_member"] and not e["staff_notes"]
    stored = _answers(c, r.json()["id"])
    assert not (set(stored) & enquiries.STAFF_ONLY_KEYS), stored.keys() & enquiries.STAFF_ONLY_KEYS
    assert stored["first_name"] == "Test" and stored["budget_option"] == body["budget_option"]   # the person's answers are untouched


def test_public_replay_of_same_raw_body_still_replays(client):
    anon = _anon(client)
    body = dict(good_answers(), channel="Phone")
    r1 = anon.post("/api/public/enquiries", json=body, headers={submissions.HEADER: KEY})
    r2 = anon.post("/api/public/enquiries", json=body, headers={submissions.HEADER: KEY})
    assert r1.status_code == 201 and r2.status_code == 200 and r2.json()["replayed"] is True and r2.json()["id"] == r1.json()["id"]


def test_staff_phone_route_still_stores_its_channel_fields(client):
    c = appdb(client)
    r = client.post("/api/enquiries", json=dict(good_answers(is_test=True), channel="Phone", campaign="walk-in"), headers={submissions.HEADER: KEY})
    assert r.status_code == 201 and r.json()["channel"] == "Phone"
    e = row(c.execute("SELECT channel, campaign FROM enquiries WHERE id=?", (r.json()["id"],)))
    assert e["channel"] == "Phone" and e["campaign"] == "walk-in"
    assert _answers(c, r.json()["id"]).get("channel") == "Phone"   # staff-entered metadata stays with the staff record


def test_strip_staff_only_is_a_copy_and_keeps_answers():
    a = dict(good_answers(), channel="Phone", referrer="x")
    b = enquiries.strip_staff_only(a)
    assert "channel" in a and "channel" not in b and "referrer" not in b and b["email"] == a["email"]
    assert enquiries.strip_staff_only("not a dict") == "not a dict"


# ---------------------------------------------------------------- R1
@pytest.mark.parametrize("code,expect", [("other", "0490220814"), ("Other", "0490220814"), ("+61", "+61 0490220814"), ("", "0490220814")])
def test_receipt_phone_never_prints_the_other_option(code, expect):
    assert receipts._phone({"phone_code": code, "phone": "0490220814"}, {}) == expect


def test_receipt_for_other_code_shows_full_number_only(client, monkeypatch):
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun")
    anon, c = _anon(client), appdb(client)
    r = anon.post("/api/public/enquiries", json=good_answers(phone_code="other", phone="0490220814"), headers={submissions.HEADER: KEY})
    assert r.status_code == 201
    e = enquiries.get_enquiry(c, r.json()["id"])
    text = receipts.render_receipt(e)["text"]
    assert "Mobile number: 0490220814" in text and "other 0490220814" not in text


# ---------------------------------------------------------------- R2
def test_test_mode_receipt_is_marked_and_real_receipt_unchanged(client):
    anon, c = _anon(client), appdb(client)
    rt = anon.post("/api/public/enquiries", json=good_answers(is_test=True, email="t1@example.test"), headers={submissions.HEADER: KEY})
    rr = anon.post("/api/public/enquiries", json=good_answers(email="r1@example.com"), headers={submissions.HEADER: "k_" + "d" * 30})
    assert rt.status_code == 201 and rr.status_code == 201
    test_r = receipts.render_receipt(enquiries.get_enquiry(c, rt.json()["id"]))
    real_r = receipts.render_receipt(enquiries.get_enquiry(c, rr.json()["id"]))
    assert test_r["subject"] == "[TEST] " + receipts.SUBJECT and receipts.TEST_NOTE in test_r["text"] and html.escape(receipts.TEST_NOTE) in test_r["html"]
    assert real_r["subject"] == receipts.SUBJECT and "TEST" not in real_r["text"] and html.escape(receipts.TEST_NOTE) not in real_r["html"]
    # the marked receipt still carries the person's own answers
    assert "Hi Test," in test_r["text"] and "Reference: MateBot-" in test_r["text"]
