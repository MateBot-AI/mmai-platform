"""M3 (3.6): the bunk-bed answer is a Yes or a No, and nothing else is ever recorded as either.

Overlay K (8 Nov 2026). Before it, a present-but-malformed answer ("maybe", 2, []) on CREATE and None / "" / "maybe" / 2 on a
staff PATCH were silently stored as No. Now every accepted representation is listed in enquiries.yes_no_value and every other
value is refused with one plain message before anything is written - no row, no partial column, no audit entry. No schema
change (the column stays INTEGER NOT NULL), no client change (the staff page already sends "1"/"0", the form "Yes"/"No").
"""
import pytest
from fastapi.testclient import TestClient

from server.db import row, rows
from server.enquiries import YES_NO_MESSAGE, ValidationError, create_enquiry, update_enquiry, yes_no_value
from tests.test_data_and_api import client, good_answers  # noqa: F401  (client fixture)

ACCEPTED_YES = ("Yes", "yes", "YES", " yes ", "1", "true", "True", True, 1)
ACCEPTED_NO = ("No", "no", "NO", " no ", "0", "false", "False", False, 0)
MALFORMED = (None, "", " ", "maybe", "y", "n", "on", "2", 2, -1, 0.0, 1.0, [], ["Yes"], {}, {"bunk_ok": "Yes"})


def _rows(db):
    return rows(db.execute("SELECT * FROM enquiries ORDER BY id"))


def _audit(db):
    return rows(db.execute("SELECT * FROM audit_log ORDER BY id"))


@pytest.mark.parametrize("value", ACCEPTED_YES)
def test_yes_representations(value):
    assert yes_no_value(value) == 1


@pytest.mark.parametrize("value", ACCEPTED_NO)
def test_no_representations(value):
    assert yes_no_value(value) == 0


@pytest.mark.parametrize("value", MALFORMED)
def test_malformed_representations_are_none_never_no(value):
    assert yes_no_value(value) is None


def test_message_is_plain_language():
    assert YES_NO_MESSAGE == "Please answer Yes or No."
    assert "M3" not in YES_NO_MESSAGE and "3.6" not in YES_NO_MESSAGE and "_" not in YES_NO_MESSAGE


# ---- CREATE -------------------------------------------------------------------------------------------------------------

def test_missing_create_answer_is_refused(db):
    answers = good_answers()
    answers.pop("bunk_ok")
    before = _rows(db)
    with pytest.raises(ValidationError) as exc:
        create_enquiry(db, answers, "Landing page", use_ai_countries=False)
    assert "bunk_ok" in exc.value.errors
    assert _rows(db) == before


@pytest.mark.parametrize("value", ["maybe", 2, [], "", "on", 0.0, {"bunk_ok": "Yes"}])
def test_malformed_create_answer_is_refused_and_nothing_written(db, value):
    before, audits = _rows(db), _audit(db)
    with pytest.raises(ValidationError) as exc:
        create_enquiry(db, good_answers(bunk_ok=value), "Landing page", use_ai_countries=False)
    assert exc.value.errors.get("bunk_ok") in (YES_NO_MESSAGE, "Please complete this.", "Invalid value.", "Too many entries.")
    assert _rows(db) == before and _audit(db) == audits


@pytest.mark.parametrize("value, stored", [("Yes", 1), ("1", 1), (True, 1), (1, 1), ("No", 0), ("0", 0), (False, 0), (0, 0), ("false", 0)])
def test_accepted_create_answers_store_one_or_zero(db, value, stored):
    e = create_enquiry(db, good_answers(bunk_ok=value), "Landing page", use_ai_countries=False)
    assert row(db.execute("SELECT bunk_ok FROM enquiries WHERE id=?", (e["id"],)))["bunk_ok"] == stored


# ---- STAFF PATCH --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "maybe", 2, {}, [], 0.0, "on"])
def test_malformed_staff_answer_does_not_replace_recorded_yes(db, value):
    e = create_enquiry(db, good_answers(bunk_ok="Yes"), "Landing page", use_ai_countries=False)
    eid = e["id"]
    before = row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    audits = _audit(db)
    with pytest.raises(ValidationError) as exc:
        update_enquiry(db, eid, {"bunk_ok": value}, "Synthetic reviewer")
    assert "bunk_ok" in exc.value.errors
    assert row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,))) == before
    assert _audit(db) == audits


def test_mixed_patch_with_malformed_answer_writes_nothing_at_all(db):
    e = create_enquiry(db, good_answers(bunk_ok="Yes", first_name="Original"), "Landing page", use_ai_countries=False)
    eid = e["id"]
    before = row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    audits = _audit(db)
    with pytest.raises(ValidationError) as exc:
        update_enquiry(db, eid, {"first_name": "Changed", "mate_max": 5, "staff_notes": "note", "bunk_ok": "maybe"}, "Synthetic reviewer")
    assert exc.value.errors == {"bunk_ok": YES_NO_MESSAGE}
    after = row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,)))
    assert after == before                      # first_name, mate_max and staff_notes all untouched, not just bunk_ok
    assert after["first_name"] == "Original" and after["bunk_ok"] == 1
    assert _audit(db) == audits                 # no "update_enquiry" audit entry for a refused edit


@pytest.mark.parametrize("value, stored", [("0", 0), ("No", 0), (False, 0), (0, 0), ("1", 1), ("Yes", 1), (True, 1), (1, 1)])
def test_accepted_staff_answers_are_recorded(db, value, stored):
    e = create_enquiry(db, good_answers(bunk_ok="Yes" if stored == 0 else "No"), "Landing page", use_ai_countries=False)
    out = update_enquiry(db, e["id"], {"bunk_ok": value}, "Synthetic reviewer")
    assert out["bunk_ok"] == stored
    assert row(db.execute("SELECT bunk_ok FROM enquiries WHERE id=?", (e["id"],)))["bunk_ok"] == stored


def test_birth_country_confirmed_coercion_is_unchanged(db):
    e = create_enquiry(db, good_answers(), "Landing page", use_ai_countries=False)
    out = update_enquiry(db, e["id"], {"birth_country_confirmed": "maybe"}, "Synthetic reviewer")   # out of K's scope: still coerced
    assert out["birth_country_confirmed"] == 0
    out = update_enquiry(db, e["id"], {"birth_country_confirmed": 1}, "Synthetic reviewer")
    assert out["birth_country_confirmed"] == 1


# ---- HTTP ---------------------------------------------------------------------------------------------------------------

def test_public_form_malformed_answer_gets_422_with_plain_message(client):  # noqa: F811
    anon = TestClient(client.app)
    r = anon.post("/api/public/enquiries", json=good_answers(bunk_ok="maybe"))
    assert r.status_code == 422 and r.json()["errors"]["bunk_ok"] == YES_NO_MESSAGE
    r = anon.post("/api/public/enquiries", json=good_answers(bunk_ok=2))
    assert r.status_code == 422 and r.json()["errors"]["bunk_ok"] == YES_NO_MESSAGE
    r = anon.post("/api/public/enquiries", json=good_answers(bunk_ok="Yes"))
    assert r.status_code == 201, r.text                                   # the public reply is a receipt, so read the row as staff
    listed = client.get("/api/enquiries").json()["items"]
    assert len(listed) == 1 and client.get(f"/api/enquiries/{listed[0]['id']}").json()["bunk_ok"] == 1


def test_staff_patch_malformed_answer_gets_422_and_keeps_the_row(client):  # noqa: F811
    r = client.post("/api/enquiries", json=good_answers(bunk_ok="Yes", first_name="Original"))
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    for bad in (None, "", "maybe", 2):
        r = client.patch(f"/api/enquiries/{eid}", json={"first_name": "Changed", "bunk_ok": bad})
        assert r.status_code == 422 and r.json()["errors"] == {"bunk_ok": YES_NO_MESSAGE}, (bad, r.text)
        e = client.get(f"/api/enquiries/{eid}").json()
        assert e["first_name"] == "Original" and e["bunk_ok"] == 1
    r = client.patch(f"/api/enquiries/{eid}", json={"bunk_ok": "0"})        # what the staff select sends
    assert r.status_code == 200 and r.json()["bunk_ok"] == 0
