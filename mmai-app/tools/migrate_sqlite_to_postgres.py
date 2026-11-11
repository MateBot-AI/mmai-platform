#!/usr/bin/env python3
"""One-off copy of a MateBot AI SQLite database into an EMPTY PostgreSQL database (T17 move to DigitalOcean).

Copies every table (apartments, enquiries, party members, groups, match runs, staff accounts, sessions, settings,
audit and scheduler logs, mate links, drafts, barred people ... real and TEST datasets alike), preserving all ids so
foreign keys, share links and audit history stay intact, then moves each identity sequence past the highest copied id.

Usage:
    python3 tools/migrate_sqlite_to_postgres.py --sqlite server/data/mmai.db --postgres "postgresql://user:pass@host:25060/db?sslmode=require"
    add --force to copy into a Postgres database that already has rows (its rows are kept; duplicates by id are skipped)
    add --dry-run to only print the row counts of both sides

Checks: row counts of every table are printed before and after, and the script exits non-zero if any table's
Postgres count after copying is not SQLite count (+ whatever was already there). Stop the app before running so no
rows change during the copy.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Parent tables first so foreign keys are satisfied while copying.
TABLE_ORDER = ["cities", "staff_users", "apartments", "enquiries", "party_members", "match_runs", "groups", "group_members",
               "group_apartments", "member_rejections", "audit_log", "sessions", "email_links", "settings", "scheduler_log",
               "mate_links", "form_drafts", "mate_requests", "barred_people", "enquiry_submissions"]
BATCH = 500


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sqlite", required=True, help="path to the SQLite file (e.g. server/data/mmai.db)")
    ap.add_argument("--postgres", default=os.environ.get("DATABASE_URL"), help="PostgreSQL URL (default: DATABASE_URL env var)")
    ap.add_argument("--force", action="store_true", help="allow a target database that already contains rows")
    ap.add_argument("--dry-run", action="store_true", help="only print counts; copy nothing")
    a = ap.parse_args()
    if not a.postgres or not a.postgres.startswith(("postgres://", "postgresql://")):
        print("ERROR: give --postgres postgresql://... (or set DATABASE_URL)", file=sys.stderr)
        return 2
    if not os.path.exists(a.sqlite):
        print(f"ERROR: SQLite file not found: {a.sqlite}", file=sys.stderr)
        return 2

    os.environ["DATABASE_URL"] = a.postgres
    from server import db as dbmod

    src = sqlite3.connect(a.sqlite)
    src.row_factory = sqlite3.Row
    src_tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    order = [t for t in TABLE_ORDER if t in src_tables] + sorted(t for t in src_tables if t not in TABLE_ORDER)

    dst = dbmod.PgConnection(a.postgres)
    dbmod.init_schema(dst)  # creates the tables and applies the column migrations on the target
    dst_tables = set(dst.table_names())

    def counts(conn, tables):
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}

    before_src = counts(src, order)
    before_dst = counts(dst, [t for t in order if t in dst_tables])
    print(f"{'table':20} {'sqlite':>8} {'postgres (before)':>18}")
    for t in order:
        print(f"{t:20} {before_src[t]:8} {before_dst.get(t, '-'):>18}")
    seeded_only = {"cities"}  # init_schema seeds Melbourne (C1); that row is expected on the target
    if any(n for t, n in before_dst.items() if t not in seeded_only) and not a.force and not a.dry_run:
        print("\nERROR: the Postgres database is not empty. Use --force to copy into it anyway (existing rows are kept).", file=sys.stderr)
        return 3
    if a.dry_run:
        return 0

    problems = []
    for t in order:
        if t not in dst_tables:
            problems.append(f"{t}: table missing on Postgres (not part of the current schema), {before_src[t]} rows NOT copied")
            continue
        src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
        dst_cols = dst.table_columns(t)
        cols = [c for c in src_cols if c in dst_cols]
        skipped = [c for c in src_cols if c not in dst_cols]
        if skipped:
            print(f"  note: {t}: columns not in the Postgres schema are skipped: {', '.join(skipped)}")
        key = dbmod.UNIQUE_KEYS.get(t, ("id",)) if "id" not in cols else ("id",)
        conflict = f" ON CONFLICT ({', '.join(key)}) DO NOTHING" if all(k in cols for k in key) else ""
        sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}){conflict}"
        rows = src.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
        copied = 0
        for i in range(0, len(rows), BATCH):
            batch = [tuple(r) for r in rows[i:i + BATCH]]
            with dst._pg.cursor() as cur:  # direct psycopg: this SQL is already Postgres-flavoured
                cur.executemany(sql, batch)
            copied += len(batch)
        if "id" in cols and rows:
            # move the identity sequence past the highest copied id so new rows do not collide
            with dst._pg.cursor() as cur:
                cur.execute(f"SELECT pg_get_serial_sequence('{t}', 'id')")
                seq = cur.fetchone()[0]
                if seq:
                    cur.execute(f"SELECT setval(%s, (SELECT COALESCE(MAX(id), 1) FROM {t}), true)", (seq,))
        dst.commit()
        print(f"  copied {copied:6} rows -> {t}")

    after_dst = counts(dst, [t for t in order if t in dst_tables])
    print(f"\n{'table':20} {'sqlite':>8} {'postgres (after)':>17} {'expected':>9}")
    for t in order:
        if t not in dst_tables:
            continue
        expected = before_src[t] + before_dst.get(t, 0)
        if t in seeded_only:  # Melbourne exists on both sides; the upsert skips the duplicate
            expected = max(before_src[t], before_dst.get(t, 0))
        flag = "" if after_dst[t] >= expected and (a.force or after_dst[t] == expected) else "  <-- MISMATCH"
        print(f"{t:20} {before_src[t]:8} {after_dst[t]:17} {expected:9}{flag}")
        if flag:
            problems.append(f"{t}: expected {expected}, found {after_dst[t]}")
    dst.close()
    src.close()
    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print("\nDone. All tables copied and counts match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
