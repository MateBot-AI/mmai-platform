#!/usr/bin/env python3
"""Build the accounts-only SQLite seed used by tools/start_published.sh on a FRESH published store.

What the seed contains
  * the full current schema (server/db.py SCHEMA + migrations, created through db.connect on a temporary path)
  * the staff_users rows of the source store, copied as they are (salted hashes only; no plain-text password exists anywhere)
  * the settings marker `accounts.shared_disabled_at` (so the retired shared sign-in is not re-processed)
  * the default city row that init_schema always writes
Nothing else: no enquiries, party members, apartments, groups, runs, sessions, drafts, audit rows or scheduler state.
The apartment list is imported by the server's normal first-run bootstrap from data/ALM_Apartments_*.xlsx.

Usage (from mmai-app):  python3 tools/bootstrap/make_staff_seed.py [--source server/data/mmai.db] [--out bootstrap/staff_accounts.db]
The source is read through SQLite's online-backup API, so a live WAL store is read consistently and never written.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, APP)

SETTINGS_KEEP = ("accounts.shared_disabled_at",)
# Every table that must be empty in the seed (everything except staff_users, settings and cities).
MUST_BE_EMPTY = ["enquiries", "party_members", "apartments", "match_runs", "groups", "group_members", "group_apartments", "member_rejections",
                 "audit_log", "sessions", "email_links", "scheduler_log", "mate_links", "form_drafts", "mate_requests", "barred_people",
                 "enquiry_submissions"]


def snapshot(source: str, dest: str) -> None:
    """Consistent read-only copy of the source store (WAL-safe) via the backup API."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=15)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def build(source: str, out: str) -> dict:
    if not os.path.exists(source):
        raise SystemExit(f"source store not found: {source}")
    work = tempfile.mkdtemp(prefix="mmai_seed_")
    try:
        snap = os.path.join(work, "source_snapshot.db")
        snapshot(source, snap)
        s = sqlite3.connect(snap)
        s.row_factory = sqlite3.Row
        users = [dict(r) for r in s.execute("SELECT * FROM staff_users ORDER BY id")]
        marks = [dict(r) for r in s.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(SETTINGS_KEEP))})", SETTINGS_KEEP)]
        s.close()
        if not users:
            raise SystemExit("source store has no staff_users rows; refusing to build an empty seed")

        # Fresh schema on a temporary path through the server's own code (schema + additive migrations + default city).
        seed_tmp = os.path.join(work, "seed.db")
        from server import db as dbmod  # noqa: E402  (after sys.path insert)
        c = dbmod.connect(seed_tmp)
        cols = [r[1] for r in c.execute("PRAGMA table_info(staff_users)")]
        for u in users:
            keys = [k for k in cols if k in u]
            c.execute(f"INSERT INTO staff_users({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [u[k] for k in keys])
        for m in marks:
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (m["key"], m["value"]))
        c.commit()
        for t in MUST_BE_EMPTY:
            n = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if n:
                raise SystemExit(f"seed table {t} unexpectedly has {n} rows")
        # Single-file seed: leave WAL mode so no -wal/-shm sidecars ship with it, then compact.
        c.execute("PRAGMA journal_mode=DELETE")
        c.commit()
        c.execute("VACUUM")
        c.close()
        for side in ("-wal", "-shm"):
            if os.path.exists(seed_tmp + side):
                os.remove(seed_tmp + side)
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        shutil.copyfile(seed_tmp, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"staff_users": len(users), "usernames": [u["username"] for u in users], "settings": [m["key"] for m in marks], "out": out,
            "bytes": os.path.getsize(out)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=os.path.join(APP, "server", "data", "mmai.db"))
    ap.add_argument("--out", default=os.path.join(APP, "bootstrap", "staff_accounts.db"))
    a = ap.parse_args()
    info = build(a.source, a.out)
    print(f"[mmai seed] wrote {info['out']} ({info['bytes']} bytes): {info['staff_users']} staff accounts {info['usernames']}, settings {info['settings']}")


if __name__ == "__main__":
    main()
