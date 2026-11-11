"""T8: hourly matching scheduler.

Every hour, on the hour, MMAI checks whether at least one new applicant (a new enquiry in the live pool) has arrived since the
last completed full run for that data set. If so, a full matching run starts (T6, GP6); if not, the run is deferred to the next
hour. Every check is logged (`scheduler_log`) and the last/next check times are kept in `settings`, so the schedule survives a
server restart: a check missed while the server was down runs at start-up. Two runs never overlap (`matching.start_full_run`).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import matching
from .db import now_iso, row, rows

ENABLED = os.environ.get("MMAI_SCHEDULER", "1") == "1"
DATASETS = [d.strip() for d in os.environ.get("MMAI_SCHEDULER_DATASETS", "real").split(",") if d.strip()]
INTERVAL_S = int(os.environ.get("MMAI_SCHEDULER_INTERVAL_S", "3600"))
_LOCK = threading.Lock()
_thread: threading.Thread | None = None
_CATCHUP_LOCK = threading.Lock()
_catchup_thread: threading.Thread | None = None
_last_check_cache: str | None = None   # in-memory copy of settings.scheduler.last_check_at, so the per-request test is free


def _setting(c, key: str, value: str | None = None) -> str | None:
    if value is not None:
        c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        c.commit()
        return value
    r = row(c.execute("SELECT value FROM settings WHERE key=?", (key,)))
    return r["value"] if r else None


def next_boundary(now: datetime | None = None) -> datetime:
    """The next top-of-the-hour (or next INTERVAL_S boundary) in UTC."""
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    nxt = (epoch // INTERVAL_S + 1) * INTERVAL_S
    return datetime.fromtimestamp(nxt, tz=timezone.utc)


def last_completed_run(c, dataset: str) -> dict | None:
    return row(c.execute("SELECT id, started_at, finished_at, pool_watermark_id FROM match_runs WHERE kind='full' AND dataset=? AND status='done' ORDER BY id DESC LIMIT 1", (dataset,)))


def new_applicants_since(c, dataset: str, last: dict | None) -> int:
    """New enquiries in the live pool (New / Proposed in group) that the last completed run did not see.
    Robust to the same-second race: a run records `pool_watermark_id` (highest enquiry id when it read its pool; ids are
    AUTOINCREMENT and never reused), so "new" = id > watermark. Runs recorded before the watermark existed fall back to
    created_at > started_at. `last` may be a run row or None (no completed run: every pool enquiry is new)."""
    is_test = 1 if dataset == "test" else 0
    q = "SELECT COUNT(*) FROM enquiries WHERE " + matching.POOL_SQL  # GP9(c): a barred arrival is not a new applicant
    args: list = [is_test, *matching.POOL_STATUSES]
    if isinstance(last, str):  # backwards-compatible: a started_at timestamp
        last = {"started_at": last, "pool_watermark_id": None}
    if last:
        if last.get("pool_watermark_id") is not None:
            q += " AND id > ?"
            args.append(last["pool_watermark_id"])
        else:
            q += " AND created_at > ?"
            args.append(last["started_at"])
    return c.execute(q, args).fetchone()[0]


def check(c, dataset: str, trigger: str = "hourly", use_ai: bool = True) -> dict:
    """One scheduler check for one data set. Returns the log entry written."""
    with _LOCK:
        now = now_iso()
        nxt = next_boundary().replace(microsecond=0).isoformat()
        last = last_completed_run(c, dataset)
        n_new = new_applicants_since(c, dataset, last)
        entry = {"checked_at": now, "dataset": dataset, "outcome": None, "reason": None, "new_enquiries": n_new, "run_id": None, "next_check_at": nxt, "trigger_kind": trigger}
        running = matching.running_run()
        if running:
            entry.update(outcome="skipped", reason=f"a matching run (#{running[0]}, {running[1]} data) is already in progress; never two at once", run_id=running[0])
        elif n_new == 0:
            entry.update(outcome="deferred", reason=("no new applicant since the last completed run" + (f" (#{last['id']}, started {last['started_at'][:16].replace('T', ' ')} UTC)" if last else "; pool is empty")))
        else:
            try:
                rid = matching.start_full_run(c, "scheduler", dataset, use_ai=use_ai)
                entry.update(outcome="ran", run_id=rid, reason=f"{n_new} new applicant(s) since the last completed run" + ("" if last else " (first run for this data set)"))
            except matching.RunInProgress as ex:
                entry.update(outcome="skipped", reason=str(ex), run_id=ex.run_id)
            except Exception as ex:  # keep the failure visible
                entry.update(outcome="failed", reason=str(ex)[:300])
        c.execute("INSERT INTO scheduler_log(checked_at, dataset, outcome, reason, new_enquiries, run_id, next_check_at, trigger_kind) VALUES (?,?,?,?,?,?,?,?)",
                  (entry["checked_at"], dataset, entry["outcome"], entry["reason"], n_new, entry["run_id"], nxt, trigger))
        c.commit()
        _setting(c, "scheduler.last_check_at", now)
        _setting(c, "scheduler.next_check_at", nxt)
        global _last_check_cache
        _last_check_cache = now
        return entry


def check_all(c, trigger: str = "hourly") -> list[dict]:
    return [check(c, d, trigger) for d in DATASETS]


def status(c) -> dict:
    last_check = _setting(c, "scheduler.last_check_at")
    nxt = _setting(c, "scheduler.next_check_at") or next_boundary().replace(microsecond=0).isoformat()
    per = {}
    for d in DATASETS:
        last = last_completed_run(c, d)
        per[d] = {"last_completed_run": last, "new_applicants_since": new_applicants_since(c, d, last)}
    last_ran = row(c.execute("SELECT * FROM scheduler_log WHERE outcome='ran' ORDER BY id DESC LIMIT 1"))
    return {"enabled": ENABLED, "datasets": DATASETS, "interval_s": INTERVAL_S, "last_check_at": last_check, "next_check_at": nxt,
            "last_scheduled_run": last_ran, "running": matching.running_run(), "per_dataset": per,
            "log": rows(c.execute("SELECT * FROM scheduler_log ORDER BY id DESC LIMIT 48"))}


def is_overdue(c) -> bool:
    """True when no check has run since the last hourly boundary (the server was down or the sandbox was paused)."""
    global _last_check_cache
    prev_boundary = (next_boundary() - timedelta(seconds=INTERVAL_S)).isoformat()
    if _last_check_cache is not None and _last_check_cache >= prev_boundary:
        return False
    last_check = _setting(c, "scheduler.last_check_at")
    _last_check_cache = last_check
    return last_check is None or last_check < prev_boundary


def catch_up_if_overdue(c, trigger: str = "wakeup") -> bool:
    """Called on every API request (and usable at start-up). If the hourly check is overdue, run it now in a background thread;
    `check` itself only starts a matching run when at least one new applicant has arrived (T8), otherwise it logs 'deferred'.
    Returns True when a catch-up was started. Cheap when nothing is due (one in-memory comparison)."""
    global _catchup_thread
    if not ENABLED or not is_overdue(c):
        return False
    with _CATCHUP_LOCK:
        if _catchup_thread and _catchup_thread.is_alive():
            return False

        def _run():
            try:
                check_all(c, trigger=trigger)
            except Exception:
                pass

        _catchup_thread = threading.Thread(target=_run, daemon=True, name="mmai-scheduler-catchup")
        _catchup_thread.start()
        return True


def _loop(c):
    # a check missed while the server was down (last check older than the previous boundary) runs now
    last_check = _setting(c, "scheduler.last_check_at")
    prev_boundary = (next_boundary() - timedelta(seconds=INTERVAL_S)).isoformat()
    if last_check is None or last_check < prev_boundary:
        try:
            check_all(c, trigger="startup")
        except Exception:
            pass
    while True:
        target = next_boundary()
        _setting(c, "scheduler.next_check_at", target.replace(microsecond=0).isoformat())
        time.sleep(max(1.0, (target - datetime.now(timezone.utc)).total_seconds()))
        try:
            check_all(c, trigger="hourly")
        except Exception:
            pass
        # Customer receipts and internal alerts have their own bounded dispatcher
        # (notifications.py), so this matching pass cannot delay their retries.


def start(c):
    """Start the scheduler thread once per process (no-op when MMAI_SCHEDULER=0, for example in tests)."""
    global _thread
    if not ENABLED or (_thread and _thread.is_alive()):
        return None
    _thread = threading.Thread(target=_loop, args=(c,), daemon=True, name="mmai-scheduler")
    _thread.start()
    return _thread
