"""Reloading the API must not accumulate daily T5 purge workers."""
import importlib
import threading
from datetime import datetime, timezone

from tests.test_data_and_api import client  # shared authenticated API fixture


def test_reload_preserves_one_live_purge_worker_and_its_schedule(client):
    import server.app as appmod

    worker = appmod._purge_thread
    schedule = appmod._next_purge_at
    assert worker.is_alive()
    assert worker.daemon
    assert worker.name == "mmai-retention-purge"
    for _ in range(8):
        importlib.reload(appmod)
        assert appmod._purge_thread is worker
        assert appmod._next_purge_at == schedule
    assert [t for t in threading.enumerate() if t.name == worker.name] == [worker]


def test_reload_replaces_a_dead_purge_worker_once(client, monkeypatch):
    import server.app as appmod

    class FakeThread:
        def __init__(self, *, target=None, name=None, daemon=None):
            self.target, self.name, self.daemon = target, name, daemon
            self.started = False
            self.start_calls = 0

        def is_alive(self):
            return self.started

        def start(self):
            # The startup pass must finish before a thread could run or requests
            # could be served. A retained timestamp from an old pass is not proof.
            assert appmod._last_purge_completed_at
            assert appmod._purge_failures == {}
            delay = (datetime.fromisoformat(appmod._next_purge_at)
                     - datetime.now(timezone.utc)).total_seconds()
            assert 86398 <= delay <= 86400
            self.start_calls += 1
            self.started = True

    # Keep the genuine sleeping worker untouched. The module's reference and
    # threading factory are restored by monkeypatch after the simulated restart.
    monkeypatch.setattr(appmod, "_purge_thread", FakeThread())
    monkeypatch.setattr(appmod, "_last_purge_completed_at", None)
    monkeypatch.setattr(appmod, "_next_purge_at", None)
    monkeypatch.setattr(threading, "Thread", FakeThread)
    importlib.reload(appmod)
    replacement = appmod._purge_thread
    assert replacement.target is appmod._purge_loop
    assert replacement.name == "mmai-retention-purge"
    assert replacement.daemon is True
    assert replacement.start_calls == 1
    importlib.reload(appmod)
    assert appmod._purge_thread is replacement
    assert replacement.start_calls == 1
