"""HomeBot apartment enquiries (H14; change request 4, 5 Nov 2026): saving into the shared backend, idempotent submission,
staff list/detail, strict validation, dataset isolation, and — the safety property — complete separation from the MateBot
matcher, scheduler, AI queue and groups. Temporary SQLite file per test; no live database, no mail, no AI calls."""
import importlib
import threading
from datetime import date, timedelta

import pytest

from tests.conftest import TEST_PW


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Temporary SQLite file by default; with MMAI_TEST_DATABASE_URL set, conftest's autouse fixture points the suite at that
    # (disposable) PostgreSQL database and rebuilds its schema per test, exactly as the other API fixtures do.
    monkeypatch.setenv("MMAI_DB", str(tmp_path / "hb.db"))
    from server import accounts
    for username, _name, _role in accounts.STAFF_ACCOUNTS:  # fixed test-only first passwords: no random password is ever printed in a test log
        monkeypatch.setenv(f"MMAI_INITIAL_PASSWORD_{username.upper()}", TEST_PW.get(username, f"Test-{username.title()}-0009"))
    from fastapi.testclient import TestClient
    import server.db as dbmod
    dbmod._conn = None
    import server.app as appmod
    importlib.reload(appmod)
    c = TestClient(appmod.app)
    r = c.post("/api/auth/login", json={"username": "anna", "password": TEST_PW["anna"]})
    assert r.status_code == 200
    c.headers.update({"Authorization": "Bearer " + r.json()["token"]})
    c.anon = TestClient(appmod.app)
    c.db = appmod.db
    return c


def _d(days):
    return (date.today() + timedelta(days=days)).isoformat()


def body(**over):
    b = {
        "is_test": False, "company_website": "",
        "need_more": "complete", "headcount": 3,
        "first_name": "Mei", "last_name": "Tan", "email": "Mei.Tan@example.com", "phone_code": "+61", "phone": "0400 000 111",
        "contact_pref": "WhatsApp",
        "people": [
            {"sleep": "Shares a bed with partner", "weekly_max": 300, "doing": "Studying", "institution": "University of Melbourne – Parkville"},
            {"name": "Jun", "sleep": "Shares a bed with partner", "weekly_max": 300, "doing": "Working", "place": "Collins St, CBD"},
            {"name": "Ana", "sleep": "Own bedroom", "weekly_max": 400, "doing": "Studying and working",
             "institution": "RMIT University – Melbourne City", "place": "Richmond", "primary": "Study"},
        ],
        "move_in": _d(40), "move_in_flex": "Within a week either way",
        "move_out": _d(400), "move_out_flex": "Exact date",
        "budget_total": 1000, "budget_types": ["2B2B", "3B"], "price_guide": "rent_12m",
        "bedrooms": ["2 bedrooms", "3 bedrooms"], "bathrooms": ["Prefer 2 bathrooms"], "area": "Either",
        "transport": ["Tram", "Walk"], "max_travel": "30 minutes", "extras": ["Balcony", "Dishwasher"],
        "apartment_notes": "", "viewing": "Video call", "heard": "Google",
        "consent_contact": True, "consent_lease": True, "consent_adults": True,
    }
    b.update(over)
    return b


def K(name="hb-key-0001"):
    """Submission keys must be 16–128 chars: semantic short names in the tests map to one stable padded key each."""
    return (name + "-" * 16)[:16] if len(name) < 16 else name


def post(c, b, key="hb-key-0001"):
    h = {"X-Submission-Key": K(key)} if key else {}
    return c.anon.post("/api/public/homebot/enquiries", json=b, headers=h)


def sub_row(c, key, cols="status"):
    return c.db.execute(f"SELECT {cols} FROM enquiry_submissions WHERE submit_key=?", (K(key),)).fetchone()


# ----------------------------------------------------------------------------------------------------------- persisting
def test_meta_has_types_without_counts(client):
    r = client.anon.get("/api/public/homebot/meta")
    assert r.status_code == 200
    m = r.json()
    assert [t["code"] for t in m["types"]] == ["Studio", "1BR", "2B1B", "2B2B", "3B"]
    assert m["extra_bed_charge_per_week"] == 70 and m["max_people"] == 7
    assert m["institutions"][-1]["name"] == "Other"
    text = r.text.lower()
    assert "count" not in text and "enquir" not in text  # H23: no counts of apartments, people or enquiries


def test_submit_persists_record_and_people(client):
    r = post(client, body())
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["reference"] == "HomeBot-%05d" % out["id"] and out["status"] == "New" and out["people"] == 3
    assert out["receipt"] == "held" and out["matching"] == "manual_review"  # mail off; never claims an email or a match
    e = client.get(f"/api/homebot/enquiries/{out['id']}").json()
    assert e["email"] == "mei.tan@example.com" and e["party_mode"] == "complete" and e["headcount_now"] == 3
    assert [p["name"] for p in e["people"]] == ["Mei", "Jun", "Ana"]
    assert e["people"][2]["primary_destination"] == "Study" and e["people"][2]["place"] == "Richmond"
    assert e["people"][0]["sleep"] == "Shares a bed with partner" and e["people"][2]["weekly_max"] == 400
    assert e["bunk_condition"] is None and e["budget_types"] == ["2B2B", "3B"] and e["is_test"] == 0
    assert e["matching"]["automatic"] is False
    assert not any("Budget check" in f for f in e["review_flags"])  # 300+300+400 = 1000: sums agree, no flag
    assert e["answers_json"]["price_guide"] == "rent_12m" and e["answers_json"]["guide_snapshot"][0]["code"] == "2B2B"
    titles = [s["title"] for s in e["summary"]]
    assert titles == ["Who is enquiring", "Dates", "Budget", "Apartment", "Viewing and contact", "Confirmed on the form"]
    flat_summary = str(e["summary"])
    assert "guide recorded at submission" in flat_summary and "18 or older" in flat_summary
    # nothing about sex, age, nationality or lifestyle is stored: no such field exists on the enquiry or on any person
    keys = set(e) | {k for p in e["people"] for k in p} | set(e["answers_json"])
    for word in ("sex", "dob", "age", "nationality", "country", "smoke", "sex_pref", "avoid"):
        assert not any(word in k.lower() for k in keys), (word, keys)
    from server.db import table_columns
    cols = set(table_columns(client.db, "homebot_enquiries")) | set(table_columns(client.db, "homebot_people"))
    assert not any(w in c for c in cols for w in ("sex", "dob", "country", "nationality", "smoke", "avoid"))


def test_budget_flag_only_when_sums_differ(client):
    a = post(client, body(budget_total=1000), key="k-eq").json()
    b = post(client, body(budget_total=900), key="k-ne").json()
    ea = client.get(f"/api/homebot/enquiries/{a['id']}").json()
    eb = client.get(f"/api/homebot/enquiries/{b['id']}").json()
    assert not any("Budget check" in f for f in ea["review_flags"])
    assert any("$1000/week" in f and "$900/week" in f for f in eb["review_flags"])


def test_rows_never_enter_mate_tables_or_matcher(client):
    post(client, body())
    db = client.db
    assert db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0  # no receipt queued
    # the matcher, scheduler check and AI queue run against the same database and find nothing to do
    from server import scheduler, ai_queue
    for ds in ("real", "test"):
        scheduler.check(db, ds, trigger="test", use_ai=False)  # the hourly matcher pass over the data set
    assert ai_queue.pending_count(db) == 0
    assert db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 0
    lst = client.get("/api/enquiries?dataset=real").json()
    assert (lst["total"] == 0) if isinstance(lst, dict) else (lst == [])


def test_legacy_routes_never_touch_homebot_rows(client):
    """A HomeBot id may equal a MateBot id (separate number spaces): legacy MateBot routes given that number must act on the
    MateBot row or 404 — and must never read or change the HomeBot row."""
    hid = post(client, body()).json()["id"]
    before = client.db.execute("SELECT * FROM homebot_enquiries WHERE id=?", (hid,)).fetchone()
    for method, path, payload in (("get", f"/api/enquiries/{hid}", None),
                                  ("patch", f"/api/enquiries/{hid}", {"status": "Matched"}),
                                  ("post", f"/api/enquiries/{hid}/contact", {}),
                                  ("delete", f"/api/enquiries/{hid}", None)):
        r = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert r.status_code in (200, 404, 422), (path, r.status_code)
        if r.status_code == 200 and method == "get":
            assert r.json().get("email") != "mei.tan@example.com"
    after = client.db.execute("SELECT * FROM homebot_enquiries WHERE id=?", (hid,)).fetchone()
    assert tuple(before) == tuple(after)
    assert client.db.execute("SELECT COUNT(*) FROM homebot_people WHERE enquiry_id=?", (hid,)).fetchone()[0] == 3
    # and the HomeBot key is still intact (a MateBot delete of the same number does not tombstone it)
    sub = sub_row(client, "hb-key-0001", "status, enquiry_id")
    assert sub[0] == "done" and sub[1] == hid


# --------------------------------------------------------------------------------------------------------- idempotency
def test_missing_key_is_refused_not_saved(client):
    r = post(client, body(), key=None)
    assert r.status_code == 422 and "submission key" in r.text.lower()
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


def test_replay_same_key_same_answers_returns_same_record(client):
    a = post(client, body())
    b = post(client, body())
    assert a.status_code == 201 and b.status_code == 200
    assert b.json()["id"] == a.json()["id"] and b.json()["replayed"] is True
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1


def test_same_key_changed_answers_is_refused(client):
    a = post(client, body())
    b = post(client, body(first_name="Other"))
    assert b.status_code == 409 and b.json()["already_submitted"] is True
    assert "result" in b.json() and b.json()["result"]["id"] == a.json()["id"]
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1


def test_key_from_matebot_scope_is_foreign_and_generic(client):
    """A MateBot public submission key presented to HomeBot (and the other way round) is refused with the generic message:
    nothing of the other scope (no id, reference or name) crosses."""
    from server import submissions
    client.db.execute("INSERT INTO enquiry_submissions(submit_key, scope, fingerprint, status, is_test, attempts, created_at, updated_at, enquiry_id, response_json) "
                      "VALUES (?,'public','x','done',0,1,'2026-01-01','2026-01-01',77,'{\"first_name\":\"Secret\"}')", (K("shared-key"),))
    client.db.commit()
    r = post(client, body(), key="shared-key")
    assert r.status_code == 409 and r.json()["errors"]["form"] == submissions.GENERIC_USED_MESSAGE
    assert "Secret" not in r.text and "77" not in r.text and "result" not in r.json()
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


def test_validation_error_releases_key_for_retry(client):
    bad = post(client, body(email="nope"))
    assert bad.status_code == 422 and "email" in bad.json()["errors"]
    good = post(client, body())
    assert good.status_code == 201


def test_concurrent_identical_submissions_make_one_record(client):
    """Eight identical submissions at once: one record, every worker answers 200/201/409. Worker exceptions are collected and
    re-raised (a crashed thread used to surface only as a PytestUnhandledThreadExceptionWarning while the test passed)."""
    from concurrent.futures import ThreadPoolExecutor
    b = body()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(lambda: post(client, b, key="race-key").status_code) for _ in range(8)]
    results = [f.result() for f in futures]  # re-raises the first worker exception with its traceback
    assert len(results) == 8
    assert sorted(results)[0] in (200, 201) and all(s in (200, 201, 409) for s in results)
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1
    assert client.db.execute("SELECT COUNT(*) FROM homebot_people").fetchone()[0] == 3


def test_people_saved_atomically_with_enquiry(client, monkeypatch):
    """If anything inside the saving transaction fails, neither the enquiry nor any of its people is left behind."""
    from server import homebot

    def boom(c, eid):
        raise RuntimeError("simulated failure after the people were written")
    with pytest.raises(RuntimeError):
        homebot.create(client.db, body(), is_test=False, submission_key="atomic-key", on_inserted=boom)
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0
    assert client.db.execute("SELECT COUNT(*) FROM homebot_people").fetchone()[0] == 0


def test_deleted_enquiry_key_is_tombstoned_and_not_recreated(client):
    hid = post(client, body()).json()["id"]
    assert client.delete(f"/api/homebot/enquiries/{hid}").status_code == 200
    assert client.get(f"/api/homebot/enquiries/{hid}").status_code == 404
    assert client.db.execute("SELECT COUNT(*) FROM homebot_people").fetchone()[0] == 0
    r = post(client, body())
    assert r.status_code == 409 and client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0
    assert tuple(sub_row(client, "hb-key-0001", "status, enquiry_id, response_json, fingerprint")) == ("deleted", None, None, "")


def test_homebot_delete_does_not_tombstone_matebot_key_with_same_number(client):
    """Scope-aware tombstones: MateBot enquiry #1 and HomeBot enquiry #1 are different records."""
    hid = post(client, body()).json()["id"]
    client.db.execute("INSERT INTO enquiry_submissions(submit_key, scope, fingerprint, status, is_test, attempts, created_at, updated_at, enquiry_id, response_json) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (K("mb-key"), "public", "fp", "done", 0, 1, "2026-01-01", "2026-01-01", hid, "{}"))
    client.db.commit()
    client.delete(f"/api/homebot/enquiries/{hid}")
    assert sub_row(client, "mb-key")[0] == "done"
    assert sub_row(client, "hb-key-0001")[0] == "deleted"


def test_matebot_bulk_sweep_does_not_tombstone_live_homebot_keys(client):
    from server import submissions
    hid = post(client, body()).json()["id"]
    from server.db import transaction
    with transaction(client.db):  # the MateBot sweep: HomeBot ids are not in `enquiries`, but the scope filter keeps their keys alive
        n = submissions.tombstone(client.db)
    assert n == 0
    assert sub_row(client, "hb-key-0001")[0] == "done"
    assert client.get(f"/api/homebot/enquiries/{hid}").status_code == 200
    # and the other way round: a HomeBot sweep leaves a MateBot key whose enquiry id happens to match alive
    client.db.execute("INSERT INTO enquiry_submissions(submit_key, scope, fingerprint, status, is_test, attempts, created_at, updated_at, enquiry_id, response_json) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (K("mb-live"), "public", "fp", "done", 0, 1, "2026-01-01", "2026-01-01", 999, "{}"))
    client.db.commit()
    with transaction(client.db):
        submissions.tombstone(client.db, homebot=True)
    assert sub_row(client, "mb-live")[0] == "done"


# ---------------------------------------------------------------------------------------------- dataset isolation
def test_test_flag_isolation_and_wipe(client):
    post(client, body(is_test=True), key="t-1")
    post(client, body(), key="r-1")
    assert client.get("/api/homebot/enquiries?dataset=test").json()["total"] == 1
    assert client.get("/api/homebot/enquiries?dataset=real").json()["total"] == 1
    st = client.get("/api/stats").json()
    assert st["homebot_new"] == 1 and st["homebot_new_test"] == 1
    r = client.delete("/api/test-data")
    assert r.status_code == 200
    assert client.get("/api/homebot/enquiries?dataset=test").json()["total"] == 0
    assert client.get("/api/homebot/enquiries?dataset=real").json()["total"] == 1
    assert sub_row(client, "t-1")[0] == "deleted"
    assert sub_row(client, "r-1")[0] == "done"
    assert post(client, body(is_test=True), key="t-1").status_code == 409  # a replay of the wiped test key is refused


def test_retention_purge_covers_homebot_and_exempts_leased(client):
    old = post(client, body(), key="old-key").json()["id"]
    leased = post(client, body(), key="leased-key").json()["id"]
    fresh = post(client, body(), key="fresh-key").json()["id"]
    client.db.execute("UPDATE homebot_enquiries SET last_contact_at='2024-01-01T00:00:00+00:00' WHERE id IN (?,?)", (old, leased))
    client.db.execute("UPDATE homebot_enquiries SET status='Leased' WHERE id=?", (leased,))
    client.db.commit()
    prev = client.get("/api/retention/preview").json()
    assert [d["id"] for d in prev["homebot_due"]] == [old]  # disclosed before the purge; Leased and recent rows are not due
    r = client.post("/api/retention/purge")
    assert r.status_code == 200 and r.json()["homebot_removed"] == 1
    assert client.get(f"/api/homebot/enquiries/{old}").status_code == 404
    assert client.get(f"/api/homebot/enquiries/{leased}").status_code == 200
    assert client.get(f"/api/homebot/enquiries/{fresh}").status_code == 200
    assert sub_row(client, "old-key")[0] == "deleted" and sub_row(client, "leased-key")[0] == "done"


# ----------------------------------------------------------------------------------------------------------- staff
def test_staff_routes_need_sign_in(client):
    hid = post(client, body()).json()["id"]
    for method, path in (("get", "/api/homebot/enquiries"), ("get", f"/api/homebot/enquiries/{hid}"),
                         ("patch", f"/api/homebot/enquiries/{hid}"), ("post", f"/api/homebot/enquiries/{hid}/contact"),
                         ("delete", f"/api/homebot/enquiries/{hid}")):
        r = getattr(client.anon, method)(path, json={}) if method in ("patch", "post") else getattr(client.anon, method)(path)
        assert r.status_code == 401, path


def test_staff_list_detail_patch_allowlist(client):
    hid = post(client, body(apartment_notes="Ground floor please, wheelchair user")).json()["id"]
    lst = client.get("/api/homebot/enquiries?dataset=real").json()
    assert lst["total"] == 1 and lst["items"][0]["reference"] == "HomeBot-%05d" % hid and lst["items"][0]["headcount_now"] == 3
    assert any("Notes to read" in f for f in lst["items"][0]["review_flags"])
    assert client.get("/api/homebot/enquiries?dataset=real&flag=flagged").json()["total"] == 1
    assert client.get("/api/homebot/enquiries?dataset=real&q=mei").json()["total"] == 1
    assert client.get("/api/homebot/enquiries?dataset=real&q=HomeBot-%05d" % hid).json()["total"] == 1
    assert client.get("/api/homebot/enquiries?dataset=real&q=HomeBot-%05d" % (hid + 50)).json()["total"] == 0
    assert client.get("/api/homebot/enquiries?dataset=real&q=zzz").json()["total"] == 0
    r = client.patch(f"/api/homebot/enquiries/{hid}", json={"status": "Staff reviewing", "staff_member": "Anna", "staff_notes": "Called"})
    assert r.status_code == 200 and r.json()["status"] == "Staff reviewing" and r.json()["staff_notes"] == "Called"
    # MateBot statuses and generic fields are refused: a HomeBot enquiry can never be made to look match-eligible
    for bad in ({"status": "Matched"}, {"status": "In group"}, {"email": "x@y.z"}, {"is_test": 1}, {"headcount_now": 2}, {"party_mode": "complete"}):
        r = client.patch(f"/api/homebot/enquiries/{hid}", json=bad)
        assert r.status_code == 422, bad
    e = client.get(f"/api/homebot/enquiries/{hid}").json()
    assert e["status"] == "Staff reviewing" and e["email"] == "mei.tan@example.com"
    r = client.post(f"/api/homebot/enquiries/{hid}/contact", json={"note": "WhatsApp sent"})
    assert r.status_code == 200 and r.json()["last_contact_at"] >= e["last_contact_at"]
    assert any(h["action"] == "homebot.contact" for h in r.json()["history"])
    assert client.db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


def test_invalid_status_rejected(client):
    hid = post(client, body()).json()["id"]
    assert client.patch(f"/api/homebot/enquiries/{hid}", json={"status": "<script>"}).status_code == 422
    assert client.patch(f"/api/homebot/enquiries/{hid}", json={"status": "Leased"}).status_code == 200


# ------------------------------------------------------------------------------------------------------- validation
@pytest.mark.parametrize("over,field", [
    ({"zzz": 1}, "form"),
    ({"headcount": 8}, "headcount"),
    ({"headcount": 0}, "headcount"),
    ({"first_name": "<b>x</b>"}, "first_name"),
    ({"first_name": "a" * 81}, "first_name"),
    ({"email": "not-an-email"}, "email"),
    ({"phone": "abc"}, "phone"),
    ({"phone_code": "+999"}, "phone_code"),
    ({"contact_pref": "Pigeon"}, "contact_pref"),
    ({"move_in": "2020-01-01"}, "move_in"),
    ({"move_in": "garbage"}, "move_in"),
    ({"move_out": ""}, "move_out"),
    ({"move_out_flex": "Open ended — may extend"}, "move_out_flex"),
    ({"move_out": _d(10)}, "move_out"),  # before move-in
    ({"budget_total": "unsure"}, "budget_total"),
    ({"budget_total": 0}, "budget_total"),
    ({"budget_total": 10 ** 9}, "budget_total"),
    ({"price_guide": "rent_6m"}, "price_guide"),  # A6: the dates in body() give the 12-month guide
    ({"is_test": "false"}, "is_test"),
    ({"phone": "+61 4"}, "phone"),
    ({"phone_code": "other", "phone": "0400 000 111"}, "phone"),  # "other" needs the full +international number
    ({"budget_types": ["Penthouse"]}, "budget_types"),
    ({"bedrooms": []}, "bedrooms"),
    ({"bathrooms": ["Must have 2 bathrooms", "1 bathroom is fine"]}, "bathrooms"),
    ({"transport": "Teleport"}, "transport"),
    ({"max_travel": "2 hours"}, "max_travel"),
    ({"extras": ["Helipad"]}, "extras"),
    ({"apartment_notes": "n" * 501}, "apartment_notes"),
    ({"viewing": ""}, "viewing"),
    ({"heard": "Carrier pigeon"}, "heard"),
    ({"consent_adults": False}, "consent_adults"),
    ({"bunk_condition": "Only if cheaper"}, "bunk_condition"),  # nobody chose a bunk
    ({"need_more": "more", "headcount_now": 3, "headcount_target": 3, "want_help": "no"}, "headcount_target"),
    ({"need_more": "more", "headcount_now": 3, "headcount_target": 5, "want_help": "yes"}, "want_help"),
])
def test_field_validation(client, over, field):
    r = post(client, body(**over))
    assert r.status_code == 422, (over, r.text)
    assert field in r.json()["errors"], (over, r.json())
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


def test_people_validation(client):
    b = body()
    b["people"] = b["people"][:2]
    assert "people" in post(client, b, key="p1").json()["errors"]  # count mismatch
    b = body()
    b["people"][2]["sleep"] = "Shares a bed with partner"  # three partners: no feasible pairs
    assert "people" in post(client, b, key="p2").json()["errors"]
    b = body()
    b["people"][0]["weekly_max"] = -5
    assert "people.1.weekly_max" in post(client, b, key="p3").json()["errors"]
    b = body()
    b["people"][0]["institution"] = "Hogwarts"
    assert "people.1.institution" in post(client, b, key="p4").json()["errors"]
    b = body()
    b["people"][1]["place"] = ""
    assert "people.2.place" in post(client, b, key="p5").json()["errors"]  # working → workplace required
    b = body()
    del b["people"][2]["primary"]
    assert "people.3.primary" in post(client, b, key="p6").json()["errors"]
    b = body()
    b["people"][1]["sex"] = "Male"  # a protected trait is not even a known field
    assert "people" in post(client, b, key="p7").json()["errors"]
    b = body(headcount=4)
    b["people"] = [{"sleep": "Own bedroom", "weekly_max": 300, "doing": "Not yet decided"} for _ in range(4)]
    assert "people" in post(client, b, key="p8").json()["errors"]  # four own bedrooms > any apartment
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


def test_price_guide_follows_dates_a6_and_other_phone_code(client):
    b = body(move_in="2027-01-15", move_in_flex="Exact date", move_out="2027-07-10", move_out_flex="Exact date",
             price_guide="rent_6m", phone_code="other", phone="+33 6 12 34 56 78")
    r = post(client, b, key="six-month")
    assert r.status_code == 201, r.text
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}").json()
    assert e["answers_json"]["price_guide"] == "rent_6m" and e["answers_json"]["guide_snapshot"][0]["kind"] == "rent_6m"
    phone_row = [r_ for sct in e["summary"] for r_ in sct["rows"] if r_[0] == "Phone"][0]
    assert phone_row[1] == "+33 6 12 34 56 78"  # never the literal "other"


def test_overlapping_flex_windows_are_allowed(client):
    """Earliest move-out before the likely move-in is fine when the earliest move-in is earlier still."""
    b = body(move_in=_d(60), move_in_flex="Flexible by a month or more", move_in_earliest=_d(5), move_in_latest=_d(100),
             move_out=_d(400), move_out_flex="Flexible by a month or more", move_out_earliest=_d(30), move_out_latest=_d(500))
    assert post(client, b, key="overlap-ok").status_code == 201
    b.update(move_out_earliest=_d(3))  # before the earliest possible move-in: not a feasible pair
    assert "move_out_earliest" in post(client, b, key="overlap-bad").json()["errors"]
    b = body(move_in=_d(60), move_in_flex="Flexible by a month or more", move_in_earliest=_d(5), move_in_latest=_d(1500))
    assert post(client, b, key="wide-span").status_code == 201  # finite limits, no invented span maximum


def test_single_person_and_bunks_and_wide_flex(client):
    b = body(headcount=1, budget_total=600, budget_types=["Studio"], bedrooms=["Studio"], bathrooms=["1 bathroom is fine"])
    b["people"] = [{"sleep": "Own bedroom", "weekly_max": 600, "doing": "Looking for work"}]
    r = post(client, b, key="one")
    assert r.status_code == 201, r.text
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}").json()
    assert e["people"][0]["sleep"] == "Whole apartment (alone)"
    # bunks need the bunk condition; wide flexibility needs finite limits
    b = body(headcount=4, budget_total=1200, move_in_flex="Flexible by a month or more")
    b["people"] = [{"sleep": "Own bedroom", "weekly_max": 300, "doing": "Not yet decided"},
                   {"sleep": "Twin room or bunk", "weekly_max": 300, "doing": "Not yet decided"},
                   {"sleep": "Bunk bed", "weekly_max": 300, "doing": "Not yet decided"},
                   {"sleep": "Twin room (two single beds)", "weekly_max": 300, "doing": "Not yet decided"}]
    r = post(client, b, key="bunk-1")
    errs = r.json()["errors"]
    assert "bunk_condition" in errs and "move_in_earliest" in errs and "move_in_latest" in errs
    b.update(bunk_condition="Only if cheaper", move_in_earliest=_d(10), move_in_latest=_d(60))
    r = post(client, b, key="bunk-2")
    assert r.status_code == 201, r.text
    b.update(move_in_earliest=_d(50), move_in_latest=_d(45))  # likely date outside the limits
    assert "move_in_latest" in post(client, b, key="bunk-3").json()["errors"]


def test_two_couples_flag_pairing_to_confirm(client):
    b = body(headcount=4, budget_total=1200)
    b["people"] = [{"sleep": "Shares a bed with partner", "weekly_max": 300, "doing": "Not yet decided"} for _ in range(4)]
    e = client.get(f"/api/homebot/enquiries/{post(client, b, key='cpl').json()['id']}").json()
    assert any("Partner pairing to confirm" in f and "2 couples" in f for f in e["review_flags"])


def test_incomplete_party_records_current_people_only(client):
    b = body(need_more="more", headcount_now=2, headcount_target=4, want_help="no", headcount=None)
    del b["headcount"]
    b["people"] = b["people"][:2]
    r = post(client, b, key="grow")
    assert r.status_code == 201, r.text
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}").json()
    assert e["party_mode"] == "more_declined" and e["headcount_now"] == 2 and e["headcount_target"] == 4 and len(e["people"]) == 2
    assert any("Party still growing" in f and "no MateBot matching" in f for f in e["review_flags"])


def test_escaping_round_trip_no_injection(client):
    b = body(apartment_notes="Robert'); DROP TABLE homebot_enquiries;-- & <i>x</i>", last_name="O'Neil")
    r = post(client, b)
    assert r.status_code == 201, r.text
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}").json()
    assert e["apartment_notes"].startswith("Robert'); DROP TABLE") and e["last_name"] == "O'Neil"
    assert "<i>x</i>" in str(e["summary"])  # stored and returned verbatim (JSON); the staff client escapes on render
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1


def test_honeypot_and_wrong_types(client):
    assert post(client, body(company_website="http://spam"), key="hp").status_code == 422
    assert post(client, body(people="three"), key="t1").status_code == 422
    assert post(client, body(budget_types={"a": 1}), key="t2").status_code == 422
    r = client.anon.post("/api/public/homebot/enquiries", data="[1,2]", headers={"Content-Type": "application/json", "X-Submission-Key": "arr"})
    assert r.status_code in (400, 422)
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


def test_static_homebot_mount_hides_docs_and_rewrites_meta(client):
    import os
    import server.app as appmod
    if not os.path.isdir(appmod.HOMEBOT_DIR):
        pytest.skip("homebot-app folder not beside mmai-app")
    r = client.anon.get("/homebot/")
    assert r.status_code == 200 and 'name="mmai-api-base" content=""' in r.text and "__PORT_" not in r.text
    assert 'name="matebot-url" content="/#/"' in r.text
    assert "https://matebot-ai.pplx.app/" not in r.text
    assert client.anon.get("/homebot/form.js").status_code == 200
    assert client.anon.get("/homebot/README.md").status_code == 404
    assert client.anon.get("/homebot/tools/").status_code == 404


def test_homebot_slash_redirect_preserves_test_query(client):
    r = client.anon.get("/homebot?test=1", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/homebot/?test=1"
    page = client.anon.get(r.headers["location"])
    assert page.status_code == 200 and 'name="matebot-url" content="/#/"' in page.text


@pytest.mark.parametrize("setting,expected", [(None, "on"), ("1", "on"), ("unexpected", "on"), ("0", "off")])
def test_homebot_review_mode_requires_explicit_host_setting(client, monkeypatch, setting, expected):
    monkeypatch.delenv("MMAI_HOMEBOT_REVIEW_BUILD", raising=False)
    if setting is not None:
        monkeypatch.setenv("MMAI_HOMEBOT_REVIEW_BUILD", setting)
    r = client.anon.get("/homebot/")
    assert r.status_code == 200
    assert f'name="homebot-review-build" content="{expected}"' in r.text
    assert r.headers["cache-control"] == "no-store"


def _audit_mentions(client, marker):
    return client.db.execute("SELECT COUNT(*) FROM audit_log WHERE detail LIKE ?", (f"%{marker}%",)).fetchone()[0]


def test_audit_details_scrubbed_on_every_delete_path(client):
    """T5: a staff contact note (possibly naming or quoting the customer) must not outlive the enquiry in the audit log."""
    # staff delete
    a = post(client, body(), key="aud-delete").json()["id"]
    client.post(f"/api/homebot/enquiries/{a}/contact", json={"note": "MARKER-DELETE spoke to Mei about the balcony"})
    assert _audit_mentions(client, "MARKER-DELETE") == 1
    client.delete(f"/api/homebot/enquiries/{a}")
    assert _audit_mentions(client, "MARKER-DELETE") == 0
    assert client.db.execute("SELECT COUNT(*) FROM audit_log WHERE entity='homebot_enquiry' AND entity_id=? AND action='homebot.contact'", (str(a),)).fetchone()[0] == 1  # action kept, detail gone
    # test-data clear
    b = post(client, body(is_test=True), key="aud-testclear").json()["id"]
    client.post(f"/api/homebot/enquiries/{b}/contact", json={"note": "MARKER-TESTCLEAR"})
    client.delete("/api/test-data")
    assert _audit_mentions(client, "MARKER-TESTCLEAR") == 0
    # retention purge
    c_ = post(client, body(), key="aud-purge").json()["id"]
    client.post(f"/api/homebot/enquiries/{c_}/contact", json={"note": "MARKER-PURGE"})
    client.db.execute("UPDATE homebot_enquiries SET last_contact_at='2024-01-01T00:00:00+00:00' WHERE id=?", (c_,))
    client.db.commit()
    client.post("/api/retention/purge")
    assert _audit_mentions(client, "MARKER-PURGE") == 0
    # a live enquiry's note is untouched
    d = post(client, body(), key="aud-live").json()["id"]
    client.post(f"/api/homebot/enquiries/{d}/contact", json={"note": "MARKER-LIVE"})
    assert _audit_mentions(client, "MARKER-LIVE") == 1


def test_form_options_match_server_enums():
    """UI/API parity: every value the HomeBot form can submit for an enumerated field is exactly one the server accepts,
    and vice versa (so a normal selection can never fail validation and no server option is unreachable from the form)."""
    import os
    import re
    import server.app as appmod
    from server import homebot as hb
    index = os.path.join(appmod.HOMEBOT_DIR, "index.html")
    if not os.path.isfile(index):
        pytest.skip("homebot-app folder not beside mmai-app")
    html = open(index, encoding="utf-8").read()

    def inputs(name):
        return re.findall(rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"', html)

    def options(name):
        m = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S)
        assert m, name
        out = []
        for attrs, text in re.findall(r"<option([^>]*)>(.*?)</option>", m.group(1), re.S):
            v = re.search(r'value="([^"]*)"', attrs)
            val = v.group(1) if v else text.strip()
            if val:
                out.append(val)
        return out

    assert inputs("bedrooms") == hb.BEDROOMS
    assert inputs("bathrooms") == hb.BATHROOMS
    assert inputs("area") == hb.AREAS
    assert inputs("transport") == hb.TRANSPORT
    assert inputs("extras") == hb.EXTRAS and len(hb.EXTRAS) == 20   # the 20 features Bill approved on 8 Nov 2026 (shared catalogue)
    assert inputs("viewing") == hb.VIEWING
    assert inputs("contact_pref") == hb.CONTACT_PREFS
    assert inputs("bunk_condition") == hb.BUNK_CONDITIONS
    assert inputs("need_more") == ["complete", "more"]
    assert options("max_travel") == hb.MAX_TRAVEL
    assert options("heard") == hb.HEARD
    assert options("move_in_flex") == hb.FLEX and options("move_out_flex") == hb.FLEX
    assert sorted(options("phone_code")) == sorted(hb.PHONE_CODES)
    assert options("headcount") == [str(i) for i in range(1, 8)] + ["8+"]
    assert options("headcount_now") == [str(i) for i in range(1, 8)]
    assert options("headcount_target") == [str(i) for i in range(2, 8)] + ["8+"]
    # per-person rows are rendered by form.js: its choice lists must match too
    js = open(os.path.join(appmod.HOMEBOT_DIR, "form.js"), encoding="utf-8").read()
    sleep = re.search(r"const SLEEP_CHOICES = \[(.*?)\];", js).group(1)
    assert re.findall(r"'([^']*)'", sleep) == hb.SLEEP
    bunk = re.search(r"const BUNK_CHOICES = \[(.*?)\];", js).group(1)
    assert set(re.findall(r"'([^']*)'", bunk)) == hb.BUNK_CHOICES
    doing = re.search(r'name="person_\$\{i\}_doing"[^>]*>(.*?)</select>', js, re.S).group(1)
    assert re.findall(r"<option>(.*?)</option>", doing) == hb.DOING


@pytest.mark.parametrize("field, value", [
    ("first_name", {"x": 1}), ("first_name", ["Ann"]), ("first_name", True), ("first_name", 12),
    ("phone", 400000000), ("phone", ["0400 000 000"]), ("email", {"a": "b"}),
    ("apartment_notes", ["quiet"]), ("apartment_notes", 7), ("area", 1), ("heard", False),
])
def test_text_fields_reject_non_string_types(client, field, value):
    """Strict payload contract: text fields take strings only; containers, booleans and numbers are a 422 with nothing saved."""
    before = client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0]
    r = post(client, body(**{field: value}), key=f"type-{field}-{abs(hash(repr(value)))}")
    assert r.status_code == 422, r.text
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == before


def test_text_fields_accept_ordinary_strings_and_null_optionals(client):
    r = post(client, body(first_name="  Anne-Marie ", apartment_notes=None, heard=None), key="type-ok-strings")
    assert r.status_code == 201, r.text
    e = client.db.execute("SELECT first_name, apartment_notes FROM homebot_enquiries WHERE id=?", (r.json()["id"],)).fetchone()
    assert e[0] == "Anne-Marie" and (e[1] or "") == ""
