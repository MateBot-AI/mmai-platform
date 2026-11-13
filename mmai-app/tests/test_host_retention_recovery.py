"""Host handover regressions: prompt purge/retry and explicit backup TLS settings."""
from datetime import date, datetime, timezone

import pytest

from tests.test_data_and_api import client
from tools import recovery_drill


def test_staff_retention_preview_exposes_healthy_completion(client):
    import server.app as appmod
    assert appmod._purge_pass() is True
    response = client.get("/api/retention/preview")
    assert response.status_code == 200
    body = response.json()
    assert body["last_completed_at"]
    assert body["failures"] == {}


@pytest.mark.parametrize("today,expected", [
    (date(2026, 1, 29), "2025-01-29"),
    (date(2026, 4, 30), "2025-04-30"),
    (date(2026, 1, 31), "2025-01-31"),
    (date(2028, 2, 29), "2027-02-28"),
])
def test_retention_preview_uses_same_calendar_cutoff_as_purge(client, monkeypatch, today, expected):
    import server.app as appmod

    class FixedDate(date):
        @classmethod
        def today(cls):
            return today

    monkeypatch.setattr(appmod, "date", FixedDate)
    response = client.get("/api/retention/preview")
    assert response.status_code == 200
    assert response.json()["cutoff"] == expected


def test_recovery_passes_ca_and_clears_inherited_connection_settings(monkeypatch):
    for key in ("PGHOST", "PGPASSWORD", "PGSERVICE", "PGOPTIONS", "PGSSLMODE", "PGSSLROOTCERT"):
        monkeypatch.setenv(key, "inherited-value-must-not-win")
    monkeypatch.setenv("PGPASSFILE", "/unrelated/password/file")
    env = recovery_drill.env_for(
        "postgresql://test:synthetic@db.invalid:25060/mmai?"
        "sslmode=verify-full&sslrootcert=/tmp/mmai-pg-ca.crt"
    )
    assert env["PGHOST"] == "db.invalid"
    assert env["PGPORT"] == "25060"
    assert env["PGUSER"] == "test"
    assert env["PGPASSWORD"] == "synthetic"
    assert env["PGDATABASE"] == "mmai"
    assert env["PGSSLMODE"] == "verify-full"
    assert env["PGSSLROOTCERT"] == "/tmp/mmai-pg-ca.crt"
    assert not ({"PGSERVICE", "PGOPTIONS", "PGPASSFILE"} & env.keys())


def test_recovery_does_not_silently_drop_unsupported_parameters():
    with pytest.raises(ValueError, match="unsupported recovery connection parameter"):
        recovery_drill.env_for("postgresql://test@db.invalid/mmai?options=-csearch_path%3Dother")


def test_purge_failure_is_visible_redacted_and_does_not_skip_other_bot(client, monkeypatch, capsys):
    import server.app as appmod
    called = []

    def broken(*args):
        raise RuntimeError("PRIVATE_CUSTOMER_TEXT_MUST_NOT_APPEAR")

    monkeypatch.setattr(appmod, "purge_expired", broken)
    monkeypatch.setattr(appmod.homebot, "purge_expired", lambda c: called.append("homebot"))
    monkeypatch.setattr(appmod, "_purge_failures", {})
    monkeypatch.setattr(appmod, "_last_purge_completed_at", "previous-success")
    monkeypatch.setattr(appmod, "_next_purge_at", None)
    assert appmod._purge_pass() is False
    assert called == ["homebot"]
    assert appmod._purge_failures == {"matebot": "RuntimeError"}
    assert appmod._last_purge_completed_at == "previous-success"
    assert appmod._schedule_purge() == 60
    delay = (datetime.fromisoformat(appmod._next_purge_at) - datetime.now(timezone.utc)).total_seconds()
    assert 58 <= delay <= 60
    captured = capsys.readouterr()
    assert "matebot purge failed (RuntimeError)" in captured.err
    assert "PRIVATE_CUSTOMER_TEXT" not in captured.out + captured.err


def test_success_clears_retry_and_records_completion(client, monkeypatch):
    import server.app as appmod
    called = []
    monkeypatch.setattr(appmod, "purge_expired", lambda c, staff: called.append(("matebot", staff)))
    monkeypatch.setattr(appmod.homebot, "purge_expired", lambda c: called.append(("homebot", None)))
    monkeypatch.setattr(appmod, "_purge_failures", {"homebot": "RuntimeError"})
    monkeypatch.setattr(appmod, "_last_purge_completed_at", None)
    monkeypatch.setattr(appmod, "_next_purge_at", None)
    assert appmod._purge_pass() is True
    assert called == [("matebot", "scheduler"), ("homebot", None)]
    assert appmod._purge_failures == {}
    assert appmod._last_purge_completed_at
    assert appmod._schedule_purge() == 24 * 3600


def test_worker_retries_after_wait_then_reschedules(client, monkeypatch):
    import server.app as appmod
    events = []

    class StopLoop(BaseException):
        pass

    def wait(seconds):
        events.append(("wait", seconds))
        if len(events) > 2:
            raise StopLoop

    def purge():
        events.append(("purge",))
        appmod._purge_failures = {}

    monkeypatch.setattr(appmod, "_purge_failures", {"matebot": "RuntimeError"})
    monkeypatch.setattr(appmod, "_next_purge_at", None)
    monkeypatch.setattr(appmod.time, "sleep", wait)
    monkeypatch.setattr(appmod, "_purge_pass", purge)
    with pytest.raises(StopLoop):
        appmod._purge_loop()
    assert events == [("wait", 60), ("purge",), ("wait", 86400)]
