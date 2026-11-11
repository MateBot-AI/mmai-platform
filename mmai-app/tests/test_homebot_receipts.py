"""Submission-copy and dispatcher regression checks. No real transports."""
import json
from unittest.mock import Mock

import pytest

from server import homebot, homebot_receipts as hr, notifications, receipts
from server.db import pj, row, transaction
from tests.test_homebot import body, client, post


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("MMAI_MAIL_MODE", "off")
    monkeypatch.setenv("MMAI_ALERT_MODE", "off")
    monkeypatch.setenv("MMAI_NOTIFICATIONS", "0")
    monkeypatch.setattr(hr, "kick_delivery", lambda c: None)
    monkeypatch.delenv("MMAI_MAIL_SEND_TEST", raising=False)
    monkeypatch.delenv("MMAI_MAIL_TEST_ALLOW", raising=False)


def saved(c, **over):
    response = post(c, body(**over))
    assert response.status_code == 201, response.text
    return response.json()


def outbox(c):
    return row(c.db.execute("SELECT * FROM homebot_email_outbox"))


def captured():
    transport = receipts.CapturedTransport()
    transport.simulated = True
    return transport


def queue(c, monkeypatch):
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun")
    monkeypatch.setenv("MMAI_MAIL_FROM", "hello@example.com")
    with transaction(c.db):
        c.db.execute("UPDATE homebot_email_outbox SET status='queued'")


def test_original_snapshot_atomic_and_replay_singleton(client):
    result = saved(client)
    assert result["receipt"] == "held"
    first = outbox(client)
    replay = post(client, body()).json()
    assert replay == {**result, "replayed": True}
    assert client.db.execute("SELECT COUNT(*) FROM homebot_email_outbox").fetchone()[0] == 1
    assert client.db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0
    assert outbox(client)["snapshot_json"] == first["snapshot_json"]


def test_snapshot_covers_normalized_customer_answers_without_private_data():
    clean = homebot.validate(body())
    assert not set(clean) - hr.ANSWER_KEYS - {"flags"}
    assert all(not set(p) - hr.PERSON_KEYS for p in clean["people"])
    clean.update(staff_notes="PRIVATE STAFF SECRET", matching={"score": 82}, arbitrary="PRIVATE UNKNOWN")
    clean["people"][0]["private"] = "PRIVATE PERSON SECRET"
    rendered = hr.render(hr.snapshot(clean), 19, True)
    assert "HomeBot-00019" in rendered["text"]
    assert "Person 3" in rendered["text"] and "Richmond" in rendered["text"]
    assert "PRIVATE" not in json.dumps(rendered)
    assert "TEST" in rendered["subject"]


def test_snapshot_survives_actual_staff_requirement_edit(client, monkeypatch):
    from server import homebot_shortlist
    monkeypatch.setenv(homebot_shortlist.GATE_ENV, "1")
    saved(client)
    original = outbox(client)
    homebot_shortlist.update_requirements(client.db, original["enquiry_id"], {"budget_total_weekly": 1100}, "Anna")
    assert outbox(client)["snapshot_json"] == original["snapshot_json"]
    assert "$1,000" in hr.render(pj(original["snapshot_json"]), original["enquiry_id"], False)["text"]
    assert "$1,100" not in hr.render(pj(outbox(client)["snapshot_json"]), original["enquiry_id"], False)["text"]


def test_html_escapes_customer_text():
    clean = homebot.validate(body(apartment_notes="<script>alert('x')</script>"))
    rendered = hr.render(hr.snapshot(clean), 1, False)
    assert "<script>" not in rendered["html"] and "&lt;script&gt;" in rendered["html"]
    assert "<script>" in rendered["text"]


def test_snapshot_failure_rolls_back_enquiry_and_children(client, monkeypatch):
    monkeypatch.setattr(hr, "snapshot", Mock(side_effect=RuntimeError("synthetic snapshot fault")))
    try:
        post(client, body())
    except RuntimeError:
        pass
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0
    assert client.db.execute("SELECT COUNT(*) FROM homebot_people").fetchone()[0] == 0
    assert client.db.execute("SELECT COUNT(*) FROM homebot_email_outbox").fetchone()[0] == 0


def test_historical_missing_snapshot_is_held(client, monkeypatch):
    saved(client)
    oid = outbox(client)["enquiry_id"]
    with transaction(client.db):
        client.db.execute("DELETE FROM homebot_email_outbox")
    queue(client, monkeypatch)
    assert hr.after_saved(client.db, oid)["status"] == "held"
    assert outbox(client)["snapshot_json"] is None
    assert hr.deliver_pending(client.db, captured())["dryrun"] == 0


def test_mode_off_wins_and_held_backlog_never_released(client, monkeypatch):
    saved(client)
    transport = captured()
    assert hr.deliver_pending(client.db, transport)["sent"] == 0
    monkeypatch.setenv("MMAI_MAIL_MODE", "dryrun")
    assert hr.deliver_pending(client.db, transport)["dryrun"] == 0
    assert outbox(client)["status"] == "held"


def test_delivery_is_outside_transaction_and_dryrun_is_not_sent(client, monkeypatch):
    saved(client)
    queue(client, monkeypatch)
    transport = captured()
    original_send = transport.send
    def send(msg):
        assert not client.db.in_transaction
        return original_send(msg)
    transport.send = send
    assert hr.deliver_pending(client.db, transport)["dryrun"] == 1
    assert outbox(client)["status"] == "dryrun"
    assert hr.deliver_pending(client.db, transport)["dryrun"] == 0
    assert outbox(client)["attempts"] == 1


def test_ambiguous_sending_row_is_never_retried(client, monkeypatch):
    saved(client)
    queue(client, monkeypatch)
    with transaction(client.db):
        client.db.execute("UPDATE homebot_email_outbox SET status='sending', attempts=1")
    transport = captured()
    assert hr.deliver_pending(client.db, transport)["dryrun"] == 0
    assert outbox(client)["status"] == "sending" and outbox(client)["attempts"] == 1


def test_failure_retry_bound_and_private_error_not_logged(client, monkeypatch):
    saved(client)
    queue(client, monkeypatch)
    transport = captured()
    transport.send = Mock(side_effect=TimeoutError("PRIVATE recipient/provider detail"))
    for attempt in range(1, receipts.MAX_ATTEMPTS + 1):
        hr.deliver_pending(client.db, transport, now="9999-12-31T23:59:59")
        assert outbox(client)["attempts"] == attempt
        assert outbox(client)["last_error"] == "TimeoutError"
    assert outbox(client)["status"] == "failed"
    hr.deliver_pending(client.db, transport, now="9999-12-31T23:59:59")
    assert transport.send.call_count == receipts.MAX_ATTEMPTS


def test_recipient_change_holds_original_copy(client, monkeypatch):
    saved(client)
    queue(client, monkeypatch)
    with transaction(client.db):
        client.db.execute("UPDATE homebot_enquiries SET email='changed@example.com'")
    assert hr.deliver_pending(client.db, captured())["dryrun"] == 0
    assert outbox(client)["status"] == "held"


def test_post_acceptance_write_failure_cannot_trigger_resend(client, monkeypatch):
    saved(client)
    queue(client, monkeypatch)
    transport = captured()
    monkeypatch.setattr(hr, "_set", Mock(side_effect=RuntimeError("synthetic database fault")))
    with pytest.raises(RuntimeError):
        hr.deliver_pending(client.db, transport)
    assert outbox(client)["status"] == "sending"
    assert hr.deliver_pending(client.db, transport)["dryrun"] == 0
    assert len(transport.sent) == 1


def test_graph_ambiguous_is_parked_and_status_contains_no_customer_content(client, monkeypatch):
    from server.mail_graph import GraphAmbiguous
    saved(client)
    queue(client, monkeypatch)
    transport = captured()
    transport.send = Mock(side_effect=GraphAmbiguous("PRIVATE provider detail"))
    assert hr.deliver_pending(client.db, transport)["skipped"] == 1
    assert outbox(client)["status"] == "sending"
    assert outbox(client)["last_error"] == "GraphAmbiguous"
    hr.deliver_pending(client.db, transport, now="9999-12-31T23:59:59")
    assert transport.send.call_count == 1
    summary = hr.status(client.db)
    assert summary["counts"]["sending"] == 1 and summary["needs_review"][0]["attempts"] == 1
    assert "PRIVATE" not in json.dumps(summary) and "@" not in json.dumps(summary)


def test_test_recipient_denied_even_with_dryrun_transport(client, monkeypatch):
    saved(client, is_test=True)
    queue(client, monkeypatch)
    assert hr.deliver_pending(client.db, receipts.DryRunTransport())["dryrun"] == 0
    assert outbox(client)["status"] == "held"


def test_test_clear_preserves_real_snapshot(client):
    saved(client)
    real = outbox(client)
    response = post(client, body(is_test=True), key="different-test-key")
    assert response.status_code == 201
    homebot.delete_test_data(client.db)
    assert outbox(client) == real
    assert client.db.execute("SELECT COUNT(*) FROM homebot_email_outbox").fetchone()[0] == 1


def test_delete_removes_snapshot(client):
    saved(client)
    eid = outbox(client)["enquiry_id"]
    homebot.delete(client.db, eid, "Anna")
    assert outbox(client) is None


def test_retention_purge_removes_snapshot(client):
    saved(client)
    with transaction(client.db):
        client.db.execute("UPDATE homebot_enquiries SET last_contact_at='2000-01-01T00:00:00+00:00'")
    assert homebot.purge_expired(client.db) == 1
    assert outbox(client) is None


def test_dispatch_is_disabled_without_database_or_transport(monkeypatch):
    sentinel = Mock()
    assert notifications.dispatch_once(sentinel, "homebot_receipts") is None
    sentinel.assert_not_called()
    assert not sentinel.method_calls
    assert notifications.start(sentinel) == []


def test_dispatch_all_existing_queues_bounded_and_independent(monkeypatch):
    monkeypatch.setenv("MMAI_NOTIFICATIONS", "1")
    monkeypatch.setattr(notifications, "assert_not_guarded", lambda *args: None)
    calls = {}
    for name in notifications.queues():
        calls[name] = Mock(return_value={"sent": 0})
    monkeypatch.setattr(notifications, "queues", lambda: {name: (lambda: "dryrun", call) for name, call in calls.items()})
    connection = object()
    for name in calls:
        notifications.dispatch_once(connection, name)
        calls[name].assert_called_once_with(connection, limit=25)


def test_slow_queue_does_not_block_other_workers_and_start_is_idempotent(monkeypatch):
    import threading
    stop, slow_entered, fast_entered, release = (threading.Event() for _ in range(4))
    monkeypatch.setenv("MMAI_NOTIFICATIONS", "1")
    monkeypatch.setattr(notifications, "_stop", stop)
    monkeypatch.setattr(notifications, "_threads", {})
    monkeypatch.setattr(notifications, "assert_not_guarded", lambda *args: None)
    def slow(*args, **kwargs):
        slow_entered.set()
        assert release.wait(5)
    def fast(*args, **kwargs):
        fast_entered.set()
    monkeypatch.setattr(notifications, "queues", lambda: {
        "slow": (lambda: "dryrun", slow), "fast": (lambda: "dryrun", fast)})
    workers = notifications.start(object())
    try:
        assert slow_entered.wait(5) and fast_entered.wait(5)
        assert notifications.start(object()) == workers
        assert len(workers) == 2
    finally:
        stop.set()
        release.set()
        for worker in workers:
            worker.join(5)
    assert all(not worker.is_alive() for worker in workers)


def test_public_copy_never_claims_matching_has_not_run():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    html = (root / "homebot-app/index.html").read_text()
    script = (root / "homebot-app/form.js").read_text()
    assert "data-role=\"receiptNotice\"" in html
    assert "no automatic apartment matching has run" not in script
    assert "no apartment matching has run" not in html
    staff = (root / "mmai-app/client/staff.js").read_text()
    assert "nothing has been emailed to the applicant" not in staff
    for state in ("queued", "sending", "sent", "held", "dryrun", "failed", "error", "cancelled"):
        assert f"{state}:" in script
