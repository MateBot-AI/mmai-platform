"""Submission reliability (31 October 2026): one enquiry per form submission, however many times the request arrives.

Reproduces the vulnerable paths (double click / double Enter, a reply lost after the server saved, an error after the save,
a reload while saving, concurrent retries) and checks that the submission key makes every retry land on the one saved
enquiry, that side effects (draft, Bring a mate link and grouping, emailed-form link) happen once, that a changed payload on
a used key is refused without overwriting, and that legacy clients (no key) behave exactly as before.
"""
import threading

import pytest
from fastapi.testclient import TestClient

from server import submissions
from server.db import rows, row
from tests.test_bring_a_mate import _fake_ai, _person  # noqa: F401
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixture re-used)

KEY = "k_" + "a" * 30
H = {submissions.HEADER: KEY}


def _count(c, sql="SELECT COUNT(*) FROM enquiries", params=()):
    return c.execute(sql, params).fetchone()[0]


def _post(anon, answers, key=KEY, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    return anon.post("/api/public/enquiries" + (f"?{qs}" if qs else ""), json=answers, headers={submissions.HEADER: key} if key else {})


# ---------------- the original failure path: the same request arrives twice ----------------
def test_double_submit_with_one_key_makes_one_enquiry_and_replays_the_same_result(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    r1 = _post(anon, a)
    assert r1.status_code == 201 and r1.json()["id"]
    r2 = _post(anon, a)  # double click / double Enter / retry after a lost reply
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert r2.json()["id"] == r1.json()["id"] and r2.json()["mate_link"] == r1.json()["mate_link"]
    assert _count(c) == 1
    sub = row(c.execute("SELECT * FROM enquiry_submissions WHERE submit_key=?", (KEY,)))
    assert sub["status"] == "done" and sub["enquiry_id"] == r1.json()["id"] and sub["attempts"] == 2 and sub["scope"] == "public"
    # only the key, a hash and the reply the form already got are stored: no answers
    assert "example.com" not in (sub["response_json"] or "") and len(sub["fingerprint"]) == 64
    # the mate link was issued once
    assert _count(c, "SELECT COUNT(*) FROM mate_links") == 1


def test_legacy_client_without_a_key_keeps_the_old_behaviour(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    assert _post(anon, a, key=None).status_code == 201
    assert _post(anon, a, key=None).status_code == 201  # no key: no reservation, exactly as before (not our dedup business)
    assert _count(c) == 2 and _count(c, "SELECT COUNT(*) FROM enquiry_submissions") == 0
    # a malformed key is ignored rather than refused
    assert anon.post("/api/public/enquiries", json=a, headers={submissions.HEADER: "short"}).status_code == 201


def test_new_deliberate_enquiry_with_a_new_key_is_saved(client):
    anon = TestClient(client.app)
    c = appdb(client)
    assert _post(anon, good_answers(is_test=True)).status_code == 201
    assert _post(anon, good_answers(is_test=True, first_name="Second"), key="k_" + "b" * 30).status_code == 201
    assert _count(c) == 2


# ---------------- changed answers on a key that already produced an enquiry ----------------
def test_changed_payload_on_a_committed_key_is_refused_without_a_new_enquiry_or_overwrite(client):
    anon = TestClient(client.app)
    c = appdb(client)
    r1 = _post(anon, good_answers(is_test=True, first_name="Original"))
    eid = r1.json()["id"]
    r2 = _post(anon, good_answers(is_test=True, first_name="Changed"))
    assert r2.status_code == 409 and r2.json()["already_submitted"] is True
    assert "MateBot-%05d" % eid in r2.json()["errors"]["form"] and r2.json()["result"]["id"] == eid
    assert _count(c) == 1 and row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (eid,)))["first_name"] == "Original"


def test_validation_error_releases_the_key_so_the_fixed_form_can_be_sent(client):
    anon = TestClient(client.app)
    c = appdb(client)
    bad = good_answers(is_test=True, email="not-an-email")
    assert _post(anon, bad).status_code == 422
    assert _count(c, "SELECT COUNT(*) FROM enquiry_submissions") == 0
    assert _post(anon, good_answers(is_test=True)).status_code == 201
    assert _count(c) == 1


def test_honeypot_and_refused_share_link_do_not_consume_the_key(client):
    anon = TestClient(client.app)
    c = appdb(client)
    assert _post(anon, good_answers(is_test=True), mate_token="not-a-link").status_code == 422
    assert _count(c, "SELECT COUNT(*) FROM enquiry_submissions") == 0
    assert _post(anon, good_answers(is_test=True)).status_code == 201


# ---------------- concurrency: several retries of the same request at once ----------------
def test_concurrent_retries_with_one_key_make_exactly_one_enquiry(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    results = []

    def go():
        results.append(_post(anon, a).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results) <= {201, 200, 409} and 201 in results
    assert _count(c) == 1
    # anything that answered 409 said "in progress"; a later retry gets the saved result
    r = _post(anon, a)
    assert r.status_code == 200 and r.json()["replayed"] is True


def test_in_progress_and_stale_pending_reservations(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    # another request holds the key right now: this one is told to wait, nothing is written
    c.execute("INSERT INTO enquiry_submissions(submit_key, scope, fingerprint, status, is_test, attempts, created_at, updated_at) VALUES (?,?,?,?,1,1,?,?)",
              (KEY, "public", "x", "pending", "2026-10-31T00:00:00+00:00", submissions.now_iso()))
    c.commit()
    r = _post(anon, a)
    assert r.status_code == 409 and r.json()["status"] == "in_progress" and _count(c) == 0
    # the earlier owner died before writing anything (old reservation, no enquiry): the retry takes it over
    c.execute("UPDATE enquiry_submissions SET updated_at=? WHERE submit_key=?", ("2026-10-31T00:00:00+00:00", KEY))
    c.commit()
    r = _post(anon, a)
    assert r.status_code == 201 and _count(c) == 1
    assert row(c.execute("SELECT status, enquiry_id FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["status"] == "done"


# ---------------- the enquiry is saved but what follows fails or is lost ----------------
def test_side_effect_failure_after_the_save_still_reports_success_and_a_retry_completes_it(client, monkeypatch):
    import server.app as appmod
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    calls = {"n": 0}
    real = appmod._issue_mate_link

    def flaky(eid, is_test):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup after the save")
        return real(eid, is_test)

    monkeypatch.setattr(appmod, "_issue_mate_link", flaky)
    r1 = _post(anon, a)
    assert r1.status_code == 201 and r1.json()["id"] and r1.json()["mate_link"] is None and "saved" in r1.json()["warning"]
    assert _count(c) == 1
    assert row(c.execute("SELECT action FROM audit_log WHERE action='submit_side_effect_error'"))
    r2 = _post(anon, a)  # the form retries with the same key: the saved enquiry is finished, not duplicated
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"] and r2.json()["mate_link"]
    assert _count(c) == 1 and _count(c, "SELECT COUNT(*) FROM mate_links") == 1


def test_request_lost_after_the_save_replays_from_the_saved_state(client):
    """The server committed the enquiry (key row 'saved') but never got to store the reply: the retry finishes and replays."""
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    r1 = _post(anon, a)
    eid = r1.json()["id"]
    c.execute("UPDATE enquiry_submissions SET status='saved', response_json=NULL WHERE submit_key=?", (KEY,))
    c.commit()
    r2 = _post(anon, a)
    assert r2.status_code == 200 and r2.json()["id"] == eid and r2.json()["mate_link"] == r1.json()["mate_link"] and r2.json()["replayed"] is True
    assert _count(c) == 1 and _count(c, "SELECT COUNT(*) FROM mate_links") == 1
    assert row(c.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["status"] == "done"


def test_failure_inside_the_insert_leaves_no_enquiry_and_frees_the_key(client, monkeypatch):
    from server import enquiries as enq
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True, party="With friends", party_size="2", mate_ideal="2", mate_max="3",
                     party_members=[{"name": "Friend One", "email": "f1@example.com", "birth_country_raw": "Boom"}])
    real = enq.resolve_country

    def explode(raw, use_ai):
        if raw == "Boom":
            raise RuntimeError("country service down")  # after the enquiry INSERT, before the commit
        return real(raw, use_ai)

    monkeypatch.setattr(enq, "resolve_country", explode)
    with pytest.raises(RuntimeError):
        _post(anon, a)
    assert _count(c) == 0 and _count(c, "SELECT COUNT(*) FROM party_members") == 0
    assert _count(c, "SELECT COUNT(*) FROM enquiry_submissions") == 0  # released: the same key may try again
    monkeypatch.setattr(enq, "resolve_country", real)
    assert _post(anon, a).status_code == 201 and _count(c) == 1


def test_saved_enquiry_deleted_by_staff_is_not_recreated_by_a_replay(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    eid = _post(anon, a).json()["id"]
    assert client.delete(f"/api/enquiries/{eid}").status_code in (200, 204)
    r = _post(anon, a)
    assert r.status_code == 409 and r.json()["already_submitted"] is True and _count(c) == 0


# ---------------- drafts, emailed forms and Bring a mate: side effects once ----------------
def test_draft_keeps_the_key_and_a_resumed_retry_replays(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    d = anon.post("/api/public/drafts", json={"answers": {"first_name": "Test"}, "step": 3, "is_test": True, "submit_key": KEY}).json()
    assert d["submit_key"] == KEY
    # a later autosave with a different (or missing) key never changes the draft's key
    assert anon.put(f"/api/public/drafts/{d['token']}", json={"answers": {"first_name": "Test"}, "step": 4, "submit_key": "k_" + "z" * 30}).json()["submit_key"] == KEY
    assert anon.get(f"/api/public/drafts/{d['token']}").json()["submit_key"] == KEY
    r1 = _post(anon, a, draft_token=d["token"])
    assert r1.status_code == 201 and _count(c, "SELECT COUNT(*) FROM form_drafts") == 0
    r2 = _post(anon, a, draft_token=d["token"])  # retry after the draft is already gone: no 404, no second enquiry
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"] and _count(c) == 1


def test_emailed_form_link_is_used_once_and_the_retry_replays(client):
    anon = TestClient(client.app)
    c = appdb(client)
    link = client.post("/api/email-links", json={"recipient_name": "Em", "recipient_email": "em@example.com"}).json()
    tok = link["token"]
    a = good_answers(is_test=True)
    r1 = _post(anon, a, email_token=tok)
    assert r1.status_code == 201 and r1.json()["channel"] == "Emailed form"
    r2 = _post(anon, a, email_token=tok)
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]
    assert _count(c) == 1 and _count(c, "SELECT COUNT(*) FROM enquiries WHERE email_link_id IS NOT NULL") == 1


def test_bring_a_mate_friend_is_linked_and_grouped_once(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    c = appdb(client)
    root = _post(anon, _person(1), key="k_" + "r" * 30)
    assert root.status_code == 201
    tok = root.json()["mate_link"]
    f = _person(2)
    r1 = _post(anon, f, mate_token=tok)
    assert r1.status_code == 201 and r1.json()["mate"]["outcome"] == "pool" and r1.json()["mate_link"] == tok
    r2 = _post(anon, f, mate_token=tok)  # double click on the friend's form
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"] and r2.json()["mate_link"] == tok
    assert _count(c) == 2
    assert _count(c, "SELECT COUNT(*) FROM audit_log WHERE action='bring_mate_linked'") == 1
    assert _count(c, "SELECT COUNT(*) FROM mate_links") == 1  # the party's one link, never a second
    assert row(c.execute("SELECT mate_note FROM enquiries WHERE id=?", (root.json()["id"],)))["mate_note"].count("joined through their share link") == 1
    # the friend's link must not be "saved" twice either when only the side effects are redone
    c.execute("UPDATE enquiry_submissions SET status='saved', response_json=NULL WHERE submit_key=?", (KEY,))
    c.commit()
    r3 = _post(anon, f, mate_token=tok)
    assert r3.status_code == 200 and r3.json()["mate"]["outcome"] == "linked"
    assert _count(c, "SELECT COUNT(*) FROM audit_log WHERE action='bring_mate_linked'") == 1 and _count(c) == 2


# ---------------- staff phone form ----------------
def test_staff_form_double_save_makes_one_enquiry_and_keys_are_scoped(client):
    c = appdb(client)
    a = good_answers(is_test=True, channel="Phone")
    r1 = client.post("/api/enquiries", json=a, headers=H)
    assert r1.status_code == 201
    r2 = client.post("/api/enquiries", json=a, headers=H)
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"] and r2.json()["replayed"] is True
    assert _count(c) == 1
    assert row(c.execute("SELECT scope FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["scope"] == "staff:anna"
    # the same key from the public form is a different scope: refused, never replayed across the boundary
    anon = TestClient(client.app)
    r3 = _post(anon, good_answers(is_test=True))
    assert r3.status_code == 409 and r3.json()["already_submitted"] is True and _count(c) == 1
    # changed answers on the staff key after the save: refused with the reference, nothing overwritten
    r4 = client.post("/api/enquiries", json=good_answers(is_test=True, channel="Phone", first_name="Other"), headers=H)
    assert r4.status_code == 409 and "MateBot-" in r4.json()["errors"]["form"]
    assert row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (r1.json()["id"],)))["first_name"] == "Test"
    # staff without a key: unchanged behaviour
    assert client.post("/api/enquiries", json=a).status_code == 201 and _count(c) == 2


def test_fingerprint_ignores_key_order_and_sees_changes():
    a = good_answers()
    b = dict(reversed(list(a.items())))
    assert submissions.fingerprint(a) == submissions.fingerprint(b)
    assert submissions.fingerprint(a) != submissions.fingerprint(dict(a, first_name="X"))
    assert submissions.fingerprint(a, {"mate_token": "t"}) != submissions.fingerprint(a, {"mate_token": None})
    assert submissions.clean_key("k_" + "a" * 30) and submissions.clean_key("short") is None and submissions.clean_key("bad key!" * 4) is None


# =====================================================================================================================
# Review hardening (31 October 2026, second pass): fencing, UNIQUE backstop, one locked transaction, scope isolation, tombstones
# =====================================================================================================================
OLD = "2026-10-31T00:00:00+00:00"  # older than STALE_SECONDS: a reservation with this updated_at may be taken over


def _age(c, key=KEY):
    c.execute("UPDATE enquiry_submissions SET updated_at=? WHERE submit_key=?", (OLD, key))
    c.commit()


def test_delayed_original_resumes_after_a_takeover_and_still_only_one_enquiry_exists(client, monkeypatch):
    """The original request stalls (here: inside country normalisation, before any write) for longer than STALE_SECONDS, a
    retry with the same key takes the reservation over and saves the enquiry, then the original wakes up and carries on.
    It must not make a second enquiry: the UNIQUE key on enquiries.submission_key (and the owner fence) stop it, and it
    answers with the retry's saved result instead of an error."""
    from server import enquiries as enq
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    real = enq.resolve_country
    stalled, resume = threading.Event(), threading.Event()
    first = {"done": False}

    def slow(raw, use_ai):
        if not first["done"]:
            first["done"] = True
            stalled.set()
            assert resume.wait(10)
        return real(raw, use_ai)

    monkeypatch.setattr(enq, "resolve_country", slow)
    out = {}

    def original():
        out["r"] = _post(anon, a)

    t = threading.Thread(target=original)
    t.start()
    assert stalled.wait(10)
    _age(c)  # 120 s pass while the original is stuck
    retry = _post(anon, a)  # the form's retry takes the key over and completes
    assert retry.status_code == 201 and _count(c) == 1
    eid = retry.json()["id"]
    resume.set()
    t.join(10)
    r = out["r"]
    assert r.status_code == 200 and r.json()["id"] == eid and r.json()["replayed"] is True
    assert _count(c) == 1 and _count(c, "SELECT COUNT(*) FROM mate_links") == 1
    sub = row(c.execute("SELECT * FROM enquiry_submissions WHERE submit_key=?", (KEY,)))
    assert sub["status"] == "done" and sub["enquiry_id"] == eid
    assert row(c.execute("SELECT submission_key FROM enquiries WHERE id=?", (eid,)))["submission_key"] == KEY


def test_owner_fence_rolls_back_an_original_whose_reservation_was_taken_over(client):
    """Fencing on its own (before the UNIQUE backstop can bite): the retry has taken the key over but has not written yet.
    The original's mark_saved matches no row (wrong owner), the whole transaction rolls back, nothing is left behind, and
    the original's release() does not remove the retry's reservation."""
    from server.enquiries import create_enquiry
    c = appdb(client)
    a = good_answers(is_test=True, party="With friends", party_size="2", mate_ideal="2", mate_max="3",
                     party_members=[{"name": "Friend One", "email": "f1@example.com", "birth_country_raw": "Sweden"}])
    fp = submissions.fingerprint(a)
    original = submissions.reserve(c, KEY, "public", fp, True)
    assert original.state == "fresh" and original.owner
    _age(c)
    retry = submissions.reserve(c, KEY, "public", fp, True)
    assert retry.state == "fresh" and retry.owner != original.owner
    with pytest.raises(submissions.Superseded):
        create_enquiry(c, a, "Landing page", is_test=True, on_inserted=lambda cc, eid: submissions.mark_saved(cc, original, eid), submission_key=KEY)
    assert _count(c) == 0 and _count(c, "SELECT COUNT(*) FROM party_members") == 0
    submissions.release(c, original)  # the loser must not free the winner's reservation
    sub = row(c.execute("SELECT * FROM enquiry_submissions WHERE submit_key=?", (KEY,)))
    assert sub and sub["status"] == "pending" and sub["owner"] == retry.owner
    e = create_enquiry(c, a, "Landing page", is_test=True, on_inserted=lambda cc, eid: submissions.mark_saved(cc, retry, eid), submission_key=KEY)
    assert _count(c) == 1 and _count(c, "SELECT COUNT(*) FROM party_members") == 1
    assert row(c.execute("SELECT status, enquiry_id FROM enquiry_submissions WHERE submit_key=?", (KEY,))) == {"status": "saved", "enquiry_id": e["id"]}


def test_committed_enquiry_carries_its_key_and_the_database_refuses_a_second_one(client):
    """The key is written on the enquiry row in the same INSERT and is UNIQUE, so even a direct write that bypasses every
    application check cannot make a second enquiry for one key. Enquiries without a key (staff tools, seed, legacy) are untouched."""
    from server.enquiries import create_enquiry
    anon = TestClient(client.app)
    c = appdb(client)
    eid = _post(anon, good_answers(is_test=True)).json()["id"]
    e = row(c.execute("SELECT submission_key FROM enquiries WHERE id=?", (eid,)))
    assert e["submission_key"] == KEY
    assert row(c.execute("SELECT enquiry_id, status FROM enquiry_submissions WHERE submit_key=?", (KEY,))) == {"enquiry_id": eid, "status": "saved"} or \
        row(c.execute("SELECT enquiry_id, status FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["status"] == "done"
    with pytest.raises(Exception) as ei:
        create_enquiry(c, good_answers(is_test=True, first_name="Dup"), "Landing page", is_test=True, submission_key=KEY)
    from server.db import is_unique_violation
    assert is_unique_violation(ei.value)
    assert _count(c) == 1
    # the failed attempt left no open transaction behind: unrelated writes still work, and NULL keys never collide
    assert create_enquiry(c, good_answers(is_test=True, first_name="NoKey1"), "Phone", staff="Anna")["id"]
    assert create_enquiry(c, good_answers(is_test=True, first_name="NoKey2"), "Phone", staff="Anna")["id"]
    assert _count(c) == 3 and _count(c, "SELECT COUNT(*) FROM enquiries WHERE submission_key IS NULL") == 2


def test_failure_after_the_key_mark_rolls_the_whole_block_back_and_frees_the_key(client, monkeypatch):
    """A crash inside the write block, after the enquiry INSERT and the key mark (party-member insert fails): nothing is
    committed, the key is released, and the same key then succeeds."""
    from server import enquiries as enq
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True)
    real_mark = submissions.mark_saved
    boom = {"on": True}

    def mark_then_fail(cc, sub, eid):
        real_mark(cc, sub, eid)
        if boom["on"]:
            raise RuntimeError("disk full after the key mark")

    monkeypatch.setattr(submissions, "mark_saved", mark_then_fail)
    with pytest.raises(RuntimeError):
        _post(anon, a)
    assert _count(c) == 0 and _count(c, "SELECT COUNT(*) FROM enquiry_submissions") == 0
    boom["on"] = False
    assert _post(anon, a).status_code == 201 and _count(c) == 1
    assert row(c.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["status"] == "done"


def test_a_background_thread_commit_cannot_split_the_enquiry_transaction(client):
    """Another thread (the T8 scheduler, the AI queue) calling commit() while an enquiry is half-written must wait for the
    block to finish. Here the block fails after the enquiry row and key mark are written; if the other thread's commit had
    slipped in between, a half enquiry would survive. It must not."""
    from server.enquiries import create_enquiry
    c = appdb(client)
    a = good_answers(is_test=True)
    sub = submissions.reserve(c, KEY, "public", submissions.fingerprint(a), True)
    inside, bg_committed = threading.Event(), threading.Event()
    timeline = []

    def background():
        inside.wait(10)
        c.commit()  # the T8 scheduler's habit: commit from its own thread
        timeline.append("bg_commit_returned")
        bg_committed.set()

    def hook(cc, eid):
        submissions.mark_saved(cc, sub, eid)
        inside.set()
        bg_committed.wait(0.8)  # the other thread's commit must still be waiting on the lock here
        timeline.append("hook_raising")
        raise RuntimeError("fails after the other thread tried to commit")

    t = threading.Thread(target=background)
    t.start()
    with pytest.raises(RuntimeError):
        create_enquiry(c, a, "Landing page", is_test=True, on_inserted=hook, submission_key=KEY)
    t.join(10)
    assert timeline == ["hook_raising", "bg_commit_returned"]
    assert _count(c) == 0
    assert row(c.execute("SELECT status, enquiry_id FROM enquiry_submissions WHERE submit_key=?", (KEY,))) == {"status": "pending", "enquiry_id": None}


def test_cross_scope_key_is_refused_with_no_data_from_the_other_scope(client):
    """A key seen from another scope (staff key used on the public form, or the other way round) is refused with a generic
    message: no id, no reference, no first name, no share link, and the other scope's row is not touched."""
    c = appdb(client)
    anon = TestClient(client.app)
    r1 = client.post("/api/enquiries", json=good_answers(is_test=True, channel="Phone", first_name="Staffname"), headers=H)
    assert r1.status_code == 201
    before = row(c.execute("SELECT attempts, updated_at FROM enquiry_submissions WHERE submit_key=?", (KEY,)))
    r = _post(anon, good_answers(is_test=True))
    assert r.status_code == 409 and r.json()["already_submitted"] is True
    text = r.text
    assert "result" not in r.json() and "MateBot-" not in text and "Staffname" not in text and str(r1.json()["id"]) not in r.json()["errors"]["form"]
    assert r.json()["errors"]["form"] == submissions.GENERIC_USED_MESSAGE
    assert row(c.execute("SELECT attempts, updated_at FROM enquiry_submissions WHERE submit_key=?", (KEY,))) == before
    assert _count(c) == 1
    # the other direction: a public key used from the staff form
    pk = "k_" + "p" * 30
    p1 = _post(anon, good_answers(is_test=True, first_name="Publicname"), key=pk)
    assert p1.status_code == 201
    r2 = client.post("/api/enquiries", json=good_answers(is_test=True, channel="Phone"), headers={submissions.HEADER: pk})
    assert r2.status_code == 409 and "result" not in r2.json() and "Publicname" not in r2.text and "MateBot-" not in r2.text and p1.json()["mate_link"] not in r2.text
    assert _count(c) == 2


def _assert_tombstone(c, key=KEY):
    sub = row(c.execute("SELECT * FROM enquiry_submissions WHERE submit_key=?", (key,)))
    assert sub is not None, "the tombstone must stay so the key can never be reused"
    assert sub["status"] == "deleted" and sub["enquiry_id"] is None and sub["response_json"] is None and sub["fingerprint"] == "" and sub["owner"] is None
    return sub


def test_staff_delete_leaves_an_opaque_tombstone_and_a_retry_cannot_recreate_the_enquiry(client):
    anon = TestClient(client.app)
    c = appdb(client)
    a = good_answers(is_test=True, first_name="Gone")
    r1 = _post(anon, a)
    eid, link = r1.json()["id"], r1.json()["mate_link"]
    assert "Gone" in (row(c.execute("SELECT response_json FROM enquiry_submissions WHERE submit_key=?", (KEY,)))["response_json"] or "")
    assert client.delete(f"/api/enquiries/{eid}").status_code in (200, 204)
    _assert_tombstone(c)
    r = _post(anon, a)
    assert r.status_code == 409 and r.json()["already_submitted"] is True and "result" not in r.json()
    assert "Gone" not in r.text and link not in r.text and "MateBot-" not in r.text
    assert _count(c) == 0
    _age(c)  # even a very old tombstone is never taken over as if it were a dead reservation
    assert _post(anon, a).status_code == 409 and _count(c) == 0
    assert _post(anon, good_answers(is_test=True, first_name="Changed")).status_code == 409 and _count(c) == 0
    # a deliberate new enquiry with a new key still works
    assert _post(anon, good_answers(is_test=True), key="k_" + "n" * 30).status_code == 201 and _count(c) == 1


def test_retention_purge_and_test_data_clear_tombstone_their_keys_and_keep_unrelated_rows(client):
    from server.enquiries import purge_expired
    anon = TestClient(client.app)
    c = appdb(client)
    k_old, k_live, k_test = "k_" + "o" * 30, "k_" + "l" * 30, "k_" + "t" * 30
    old = _post(anon, good_answers(is_test=False, first_name="Oldie", email="old@example.com"), key=k_old).json()
    live = _post(anon, good_answers(is_test=False, first_name="Livia", email="live@example.com"), key=k_live).json()
    test = _post(anon, good_answers(is_test=True, first_name="Tessa"), key=k_test).json()
    c.execute("UPDATE enquiries SET last_contact_at=? WHERE id=?", ("2024-01-01T00:00:00+00:00", old["id"]))
    c.commit()
    assert purge_expired(c, "Anna")["deleted"] == 1
    _assert_tombstone(c, k_old)
    kept = row(c.execute("SELECT status, enquiry_id, response_json FROM enquiry_submissions WHERE submit_key=?", (k_live,)))
    assert kept["status"] == "done" and kept["enquiry_id"] == live["id"] and "Livia" in kept["response_json"]
    assert _post(anon, good_answers(is_test=False, first_name="Oldie", email="old@example.com"), key=k_old).status_code == 409
    assert "Oldie" not in _post(anon, good_answers(is_test=False, first_name="Oldie", email="old@example.com"), key=k_old).text
    # test-data clear (T14/T15): the test key is tombstoned, the real one untouched
    assert client.delete("/api/test-data").status_code == 200
    _assert_tombstone(c, k_test)
    assert row(c.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (k_live,)))["status"] == "done"
    assert _count(c) == 1 and row(c.execute("SELECT id FROM enquiries"))["id"] == live["id"]
    r = _post(anon, good_answers(is_test=True, first_name="Tessa"), key=k_test)
    assert r.status_code == 409 and "Tessa" not in r.text and _count(c) == 1
    # the live key still replays normally
    assert _post(anon, good_answers(is_test=False, first_name="Livia", email="live@example.com"), key=k_live).status_code == 200
