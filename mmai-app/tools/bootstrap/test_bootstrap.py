#!/usr/bin/env python3
"""Checks for the published-site bootstrap (tools/start_published.sh + bootstrap/staff_accounts.db).

Runs on its own (python3 tools/bootstrap/test_bootstrap.py) or under pytest. Uses only temporary stores and a spare port
(MMAI_BOOTSTRAP_TEST_PORT, default 8041); never touches server/data/mmai.db or a running server.

  1. seed: only staff_users (+ settings marker + default city) hold rows; every record table is empty; hashes equal the source
  2. absent store  -> the seed is copied byte-for-byte
  3. existing store (main file, or only a -wal sidecar) -> nothing is copied, file unchanged
  4. fresh start through the script -> server comes up, imports the apartment list (103 rows), 0 enquiries, staff hashes kept,
     log shows no generated/printed password
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import pytest

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(APP, "tools", "start_published.sh")
SEED = None  # generated test-only accounts, never inherited preview password hashes
PORT = int(os.environ.get("MMAI_BOOTSTRAP_TEST_PORT", "8041"))
EXPECTED_APARTMENTS = 103
RECORD_TABLES = ["enquiries", "party_members", "apartments", "match_runs", "groups", "group_members", "group_apartments", "member_rejections",
                 "audit_log", "sessions", "email_links", "scheduler_log", "mate_links", "form_drafts", "mate_requests", "barred_people",
                 "enquiry_submissions"]
STAFF_COLS = "id, username, display_name, pw_salt, pw_hash, role, disabled, created_at, pw_changed_at"


def make_synthetic_seed(directory):
    global SEED
    from server import accounts, db as dbmod
    SEED = os.path.join(str(directory), "synthetic-staff.db")
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("DATABASE_URL", raising=False)
        for username, _name, _role in accounts.STAFF_ACCOUNTS:
            patch.setenv("MMAI_INITIAL_PASSWORD_" + username.upper(), "Synthetic-Bootstrap-Only-2026!")
        c = dbmod.connect(SEED)
        accounts.ensure_staff_accounts(c, log=lambda text: None)
        c.execute("DELETE FROM settings WHERE key <> 'accounts.shared_disabled_at'")
        c.commit()
        c.execute("PRAGMA journal_mode=DELETE")
        c.close()


@pytest.fixture(scope="module", autouse=True)
def synthetic_seed(tmp_path_factory):
    make_synthetic_seed(tmp_path_factory.mktemp("bootstrap-seed"))


def sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def staff_rows(path: str) -> list[tuple]:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT {STAFF_COLS} FROM staff_users ORDER BY id").fetchall()
    finally:
        c.close()


def run_script(db_path: str, bootstrap_only: bool = True, extra_env: dict | None = None, log_path: str | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MMAI_")}
    # These checks exercise the script's SQLite bootstrap branch on purpose. Under the PostgreSQL test run the suite's conftest
    # exports DATABASE_URL, which the script correctly honours ("PostgreSQL store, no SQLite bootstrap"); the child must not inherit
    # it, otherwise the SQLite branch under test is never reached. Removing it here changes nothing about the script's behaviour.
    env.pop("DATABASE_URL", None)
    env.update({"MMAI_DB": db_path, "MMAI_TRUST_VISITOR_ID": "0", "PORT": str(PORT),
                "MMAI_BOOTSTRAP_SEED": SEED, "MMAI_SCHEDULER": "0", "MMAI_AI_QUEUE": "0",
                "MMAI_NOTIFICATIONS": "0", "MMAI_MAIL_MODE": "off", "MMAI_ALERT_MODE": "off"})
    if bootstrap_only:
        env["MMAI_BOOTSTRAP_ONLY"] = "1"
    env.update(extra_env or {})
    if bootstrap_only:
        return subprocess.run(["bash", SCRIPT], cwd=APP, env=env, capture_output=True, text=True, timeout=60)
    log = open(log_path, "w")
    return subprocess.Popen(["bash", SCRIPT], cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def test_seed_is_accounts_only():
    assert os.path.exists(SEED), "synthetic bootstrap fixture missing"
    assert not os.path.exists(SEED + "-wal") and not os.path.exists(SEED + "-shm")
    c = sqlite3.connect(f"file:{SEED}?mode=ro", uri=True)
    try:
        for t in RECORD_TABLES:
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, f"seed table {t} is not empty"
        users = c.execute("SELECT username, pw_hash, pw_salt FROM staff_users").fetchall()
        assert len(users) >= 6 and all(u[1] and u[2] for u in users)
        assert {u[0] for u in users} >= {"bill", "anna", "elaisa", "mylene", "keithley", "jordan"}
        assert c.execute("SELECT key FROM settings").fetchall() == [("accounts.shared_disabled_at",)]
    finally:
        c.close()
    assert "synthetic-staff.db" in SEED


def test_absent_store_gets_seed():
    d = tempfile.mkdtemp(prefix="mmai_boot_absent_")
    try:
        db = os.path.join(d, "sub", "mmai.db")
        r = run_script(db)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "fresh store" in r.stdout
        assert os.path.exists(db) and sha(db) == sha(SEED)
        assert not os.path.exists(db + ".bootstrap.tmp")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_read_only_seed_still_yields_a_private_writable_fresh_store_and_is_left_unchanged():
    """A frozen / read-only checkout has a read-only seed; cp would carry that mode into the new store and the server's first
    write (PRAGMA journal_mode=WAL) would fail. The script makes ONLY the new copy owner-writable and private; the seed's bytes and
    mode are untouched, and so is an existing store."""
    d = tempfile.mkdtemp(prefix="mmai_boot_roseed_")
    try:
        seed = os.path.join(d, "seed", "staff_accounts.db")
        os.makedirs(os.path.dirname(seed))
        shutil.copyfile(SEED, seed)
        os.chmod(seed, 0o444)
        seed_before = (sha(seed), os.stat(seed).st_mode)
        db = os.path.join(d, "store", "mmai.db")
        r = run_script(db, extra_env={"MMAI_BOOTSTRAP_SEED": seed})
        assert r.returncode == 0 and "fresh store" in r.stdout, r.stdout + r.stderr
        assert sha(db) == sha(SEED), "the fresh store starts as a byte-equal copy of the seed"
        assert (os.stat(db).st_mode & 0o777) == 0o600, "owner-writable and private, whatever the seed's mode"
        c = sqlite3.connect(db)
        try:
            assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"   # the server's first write succeeds
            c.execute("CREATE TABLE sentinel(x)"); c.execute("INSERT INTO sentinel VALUES (1)"); c.commit()
        finally:
            c.close()
        assert (sha(seed), os.stat(seed).st_mode) == seed_before, "the seed is never changed (bytes or mode)"
        # an existing store (here: read-only too) is neither copied over nor re-moded
        ex = os.path.join(d, "existing", "mmai.db")
        os.makedirs(os.path.dirname(ex))
        c = sqlite3.connect(ex); c.execute("CREATE TABLE live(x)"); c.commit(); c.close()
        os.chmod(ex, 0o444)
        ex_before = (sha(ex), os.stat(ex).st_mode)
        r = run_script(ex, extra_env={"MMAI_BOOTSTRAP_SEED": seed})
        assert r.returncode == 0 and "existing store kept" in r.stdout
        assert (sha(ex), os.stat(ex).st_mode) == ex_before
    finally:
        for root_, _, files in os.walk(d):
            for f in files:
                try:
                    os.chmod(os.path.join(root_, f), 0o600)
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)


def test_existing_store_untouched():
    d = tempfile.mkdtemp(prefix="mmai_boot_existing_")
    try:
        db = os.path.join(d, "mmai.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE sentinel(x)")
        c.execute("INSERT INTO sentinel VALUES ('live data')")
        c.commit()
        c.close()
        before = sha(db)
        r = run_script(db)
        assert r.returncode == 0 and "existing store kept" in r.stdout, r.stdout + r.stderr
        assert sha(db) == before
        # zero-byte main file counts as existing too
        z = os.path.join(d, "zero.db")
        open(z, "wb").close()
        r = run_script(z)
        assert "existing store kept" in r.stdout and os.path.getsize(z) == 0
        # only a -wal sidecar present (crashed store): still refuse to copy
        w = os.path.join(d, "walonly.db")
        open(w + "-wal", "wb").close()
        r = run_script(w)
        assert "existing store kept" in r.stdout and not os.path.exists(w)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_fresh_start_imports_apartments_and_keeps_staff():
    d = tempfile.mkdtemp(prefix="mmai_boot_start_")
    log_path = os.path.join(d, "server.log")
    db = os.path.join(d, "mmai.db")
    p = run_script(db, bootstrap_only=False, log_path=log_path)
    try:
        up = False
        for _ in range(120):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2) as resp:
                    if resp.status == 200:
                        up = True
                        break
            except Exception:
                time.sleep(0.5)
            if p.poll() is not None:
                break
        log = open(log_path).read()
        assert up, "server did not start:\n" + log[-3000:]
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            assert c.execute("SELECT COUNT(*) FROM apartments").fetchone()[0] == EXPECTED_APARTMENTS
            assert c.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0
            seeded = c.execute(f"SELECT {STAFF_COLS} FROM staff_users ORDER BY id").fetchall()
        finally:
            c.close()
        assert seeded == staff_rows(SEED), "staff rows changed at startup"
        assert "temporary password" not in log and "created staff account" not in log, "startup generated or printed a password"
        assert "fresh store" in log
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/public/meta", timeout=5) as resp:
            assert resp.status == 200
    finally:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            p.wait(timeout=15)
        except Exception:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                pass
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    _synthetic_temp = tempfile.TemporaryDirectory(prefix="bootstrap-synthetic-")
    make_synthetic_seed(_synthetic_temp.name)
    tests = [test_seed_is_accounts_only, test_absent_store_gets_seed, test_existing_store_untouched, test_fresh_start_imports_apartments_and_keeps_staff]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    sys.exit(1 if failed else 0)
