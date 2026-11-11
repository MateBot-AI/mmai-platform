"""H19 internal attention alerts — generic outbox core (prepared, not live). Temp SQLite only: DATABASE_URL and
MMAI_TEST_DATABASE_URL are cleared for every test here so no PostgreSQL store is touched."""
import threading

import pytest

from server import alerts, db
from server.db import row, rows

RECIP = "reservations@almelb.com.au"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MMAI_TEST_DATABASE_URL", raising=False)
    for k in ("MMAI_ALERT_MODE", "MMAI_ALERT_FROM", "MMAI_STAFF_URL", "MMAI_ALERT_SEND_TEST"):
        monkeypatch.delenv(k, raising=False)
    conn = db.connect(str(tmp_path / "alerts.db"))
    yield conn
    conn.close()


def _on(monkeypatch, mode="captured", send_test="0"):
    monkeypatch.setenv("MMAI_ALERT_MODE", mode)
    monkeypatch.setenv("MMAI_ALERT_FROM", "matebot@almelb.com.au")
    monkeypatch.setenv("MMAI_STAFF_URL", "https://matebot.example.com/")
    monkeypatch.setenv("MMAI_ALERT_SEND_TEST", send_test)


def _all(c):
    return rows(c.execute("SELECT * FROM internal_alert_outbox ORDER BY id"))


def test_default_off_holds_and_never_sends_even_with_a_transport(c):
    r = alerts.raise_attention(c, "synthetic_test", 7)
    assert r["created"] and r["status"] == "held"
    t = alerts.CapturedSimulation()
    assert alerts.deliver_pending(c, t) == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "held": 0, "skipped": 0}
    assert not t.sent and _all(c)[0]["status"] == "held"


def test_event_id_is_idempotent_and_built_only_from_validated_values(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    a = alerts.raise_attention(c, "synthetic_test", 7, is_test=True)
    b = alerts.raise_attention(c, "synthetic_test", 7, is_test=True)
    assert a["created"] and not b["created"] and a["event_id"] == b["event_id"] == "synthetic_test:enquiry:7:0"
    assert len(_all(c)) == 1
    # a numeric revision is the only way to intend a repeat
    assert alerts.raise_attention(c, "synthetic_test", 7, is_test=True, revision=1)["created"]
    # anything not in the fixed catalogue / not numeric is refused before it is stored or logged
    for bad in (("nope", 1, {}), ("synthetic_test", "7", {}), ("synthetic_test", 0, {}), ("synthetic_test", True, {}),
                ("synthetic_test", 7, {"revision": "2027-01-01 Jane <jane@x>"}), ("synthetic_test", 7, {"revision": -1})):
        r = alerts.raise_attention(c, bad[0], bad[1], is_test=True, **bad[2])
        assert r["status"] == "error" and r["event_id"] is None
    assert len(_all(c)) == 2
    assert all(x["to_email"] == RECIP for x in _all(c))


def test_captured_delivery_is_recorded_as_simulation_never_sent(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    alerts.raise_attention(c, "synthetic_test", 3, is_test=True)
    t = alerts.captured_transport(); t.sent.clear()
    out = alerts.deliver_pending(c)
    assert out["dryrun"] == 1 and out["sent"] == 0
    r = _all(c)[0]
    assert r["status"] == "dryrun" and r["sent_at"] is None and r["message_id"] and r["attempts"] == 1
    m = t.sent[-1]
    assert m["To"] == RECIP and m["From"] == "MateBot AI <matebot@almelb.com.au>" and m["Auto-Submitted"] == "auto-generated"
    assert m["Subject"] == "MateBot AI attention: Synthetic test alert (no action needed) — MateBot-00003"
    body = m.get_content()
    assert "Record: MateBot-00003 (TEST data)" in body and "Open in MateBot AI: https://matebot.example.com/#/staff" in body
    assert f"Raised: {r['created_at']}" in body and "MMAI" not in body and "not monitored" not in body
    # status reporting never says "sent"
    st = alerts.status(c)
    assert st["counts"] == {"dryrun": 1} and st["mode"] == "captured" and st["config_problem"] is None


def test_dryrun_mode_marks_dryrun(c, monkeypatch):
    _on(monkeypatch, mode="dryrun", send_test="1")
    alerts.raise_attention(c, "synthetic_test", 4, is_test=True)
    assert alerts.deliver_pending(c)["dryrun"] == 1 and _all(c)[0]["status"] == "dryrun"


def test_test_data_is_held_unless_explicitly_allowed_and_held_rows_stay_held_when_enabled(c, monkeypatch):
    _on(monkeypatch, send_test="0")
    r = alerts.raise_attention(c, "synthetic_test", 5, is_test=True)
    assert r["status"] == "held"
    monkeypatch.setenv("MMAI_ALERT_SEND_TEST", "1")
    t = alerts.captured_transport(); t.sent.clear()
    assert alerts.deliver_pending(c) == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "held": 0, "skipped": 0}
    assert _all(c)[0]["status"] == "held" and not t.sent  # only queued rows are ever picked up
    # a row queued while allowed but delivered after the flag is withdrawn is held at delivery time
    alerts.raise_attention(c, "synthetic_test", 6, is_test=True)
    monkeypatch.setenv("MMAI_ALERT_SEND_TEST", "0")
    assert alerts.deliver_pending(c)["held"] == 1


def test_recipient_is_fixed_and_a_changed_snapshot_is_held_not_redirected(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    alerts.raise_attention(c, "synthetic_test", 8, is_test=True)
    c.execute("UPDATE internal_alert_outbox SET to_email='someone-else@example.com'"); c.commit()
    t = alerts.captured_transport(); t.sent.clear()
    assert alerts.deliver_pending(c)["held"] == 1 and not t.sent
    r = _all(c)[0]
    assert r["status"] == "held" and "differs from the fixed internal recipient" in r["last_error"]
    assert alerts.recipient() == RECIP


def test_config_validation_holds_rows_and_rejects_untrusted_staff_url(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    for bad in ("http://matebot.example.com", "https://user:pw@host/", "https://host/?token=abc", "https://host/#/staff", "javascript:alert(1)", "https://ho st/", "https://[::1/", "https://[zz]:80/"):
        monkeypatch.setenv("MMAI_STAFF_URL", bad)
        assert alerts.staff_base_url() is None and alerts.config_problem()
    monkeypatch.setenv("MMAI_STAFF_URL", "http://localhost:8000")
    assert alerts.staff_base_url() == "http://localhost:8000" and alerts.config_problem() is None
    monkeypatch.setenv("MMAI_ALERT_FROM", "Reservations <reservations@almelb.com.au>")  # display name / injection shapes are refused
    assert alerts.config_problem() == "MMAI_ALERT_FROM missing or invalid"
    alerts.raise_attention(c, "synthetic_test", 9, is_test=True)
    t = alerts.captured_transport(); t.sent.clear()
    assert alerts.deliver_pending(c)["held"] == 1 and not t.sent and _all(c)[0]["status"] == "held"


def test_retry_backoff_and_permanent_failure_store_only_exception_types(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    alerts.raise_attention(c, "synthetic_test", 10, is_test=True)
    t = alerts.CapturedSimulation(fail_times=99, fail_with=ConnectionError)
    assert alerts.deliver_pending(c, t)["retry"] == 1
    r = _all(c)[0]
    assert r["status"] == "queued" and r["attempts"] == 1 and r["last_error"] == "ConnectionError" and r["next_attempt_at"] > r["updated_at"]
    assert alerts.deliver_pending(c, t)["skipped"] == 0 and _all(c)[0]["attempts"] == 1  # not due yet: not re-claimed
    far = "2099-01-01T00:00:00+00:00"
    for _ in range(3):
        assert alerts.deliver_pending(c, t, now=far)["retry"] == 1
    out = alerts.deliver_pending(c, t, now=far)
    r = _all(c)[0]
    assert out["failed"] == 1 and r["status"] == "failed" and r["attempts"] == 5 and r["last_error"] == "ConnectionError after 5 attempts"
    assert "simulated failure" not in (r["last_error"] or "")  # no raw exception text persisted
    # a non-transport error is not retried blindly: failed, type only
    alerts.raise_attention(c, "synthetic_test", 11, is_test=True)
    t2 = alerts.CapturedSimulation(fail_times=1, fail_with=ValueError)
    assert alerts.deliver_pending(c, t2)["failed"] == 1 and _all(c)[1]["last_error"] == "ValueError"


def test_sending_rows_are_left_for_manual_review_not_requeued(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    alerts.raise_attention(c, "synthetic_test", 12, is_test=True)
    c.execute("UPDATE internal_alert_outbox SET status='sending', attempts=1"); c.commit()
    t = alerts.captured_transport(); t.sent.clear()
    for _ in range(2):
        assert alerts.deliver_pending(c, now="2099-01-01T00:00:00+00:00") == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "held": 0, "skipped": 0}
    assert _all(c)[0]["status"] == "sending" and not t.sent
    assert [x["event_id"] for x in alerts.status(c)["sending_needs_review"]] == ["synthetic_test:enquiry:12:0"]


def test_concurrent_passes_deliver_each_alert_once(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    for i in range(1, 21):
        alerts.raise_attention(c, "synthetic_test", i, is_test=True)
    t = alerts.captured_transport(); t.sent.clear()
    results = []
    ths = [threading.Thread(target=lambda: results.append(alerts.deliver_pending(c))) for _ in range(4)]
    [x.start() for x in ths]; [x.join(20) for x in ths]
    statuses = rows(c.execute("SELECT status, COUNT(*) AS n FROM internal_alert_outbox GROUP BY status"))
    assert sum(r["dryrun"] for r in results) == 20 and len(t.sent) == 20, f"results={results} statuses={statuses}"
    assert len({m["Subject"] for m in t.sent}) == 20 and all(x["status"] == "dryrun" for x in _all(c)), statuses


def test_raise_inside_a_caller_transaction_rolls_back_with_it(c, monkeypatch):
    _on(monkeypatch, send_test="1")
    try:
        with db.transaction(c):
            r = alerts.raise_attention(c, "synthetic_test", 13, is_test=True, in_transaction=True)
            assert r["created"]
            raise RuntimeError("caller failed after raising the event")
    except RuntimeError:
        pass
    assert _all(c) == []


def test_kick_delivery_is_background_and_noop_when_off(c, monkeypatch):
    assert alerts.kick_delivery(c) is None
    _on(monkeypatch, send_test="1")
    alerts.raise_attention(c, "synthetic_test", 14, is_test=True)
    t = alerts.captured_transport(); t.sent.clear()
    th = alerts.kick_delivery(c)
    assert isinstance(th, threading.Thread) and th.name == "mmai-alerts"
    th.join(10)
    assert _all(c)[0]["status"] == "dryrun"


def test_only_the_permitted_producers_raise_attention():
    """The closed set of internal-attention producers under H19: the HomeBot options workflow (homebot_shortlist) and the group
    responses (group_responses: a decline, a change request, or 24 hours without everyone's answer - staff review only), and from
    9 Nov 2026 the MateBot stock follow-up (a previously suggested apartment was leased / withdrawn / changed / deleted, H19).
    BEFORE (five-fix): that producer was matching._rerank_one_group. AFTER (stock lifecycle, same day): the one producer is
    stock_lifecycle.reconcile_work (matching no longer raises anything itself). The explicit staff option re-check also records
    loss of the previously offered apartment under its distinct recheck event kind, with the same H19 body and no delivery.
    The set is still closed and deliberately named.
    If this fails, a new producer was added: name it here deliberately. The notification dispatcher consumes only existing
    outbox events; production transport still requires explicit configuration."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "server"
    callers = sorted(p.name for p in src.glob("*.py") if p.name != "alerts.py" and "raise_attention(" in p.read_text())
    assert callers == ["group_responses.py", "homebot_shortlist.py", "option_recheck.py", "stock_lifecycle.py"]
    assert "notifications.start(db)" in (src / "app.py").read_text()
    assert "raise_attention(" not in (src / "notifications.py").read_text()
