#!/usr/bin/env python3
"""Backup and isolated restore, never overwrite a nonempty destination.

SOURCE_DATABASE_URL and RESTORE_DATABASE_URL must be provided through the
environment. The destination database must already exist, be empty, have a name
starting mmai_restore_, and differ from the source. This is not a production
rollback command. Backups contain private data: keep the output directory private.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def env_for(url):
    c = conninfo_to_dict(url)
    env = os.environ.copy()
    for key in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGOPTIONS", "PGSERVICE", "PGSERVICEFILE"):
        env.pop(key, None)
    for key, dest in (("host", "PGHOST"), ("port", "PGPORT"), ("user", "PGUSER"),
                      ("password", "PGPASSWORD"), ("dbname", "PGDATABASE"), ("sslmode", "PGSSLMODE")):
        if key in c:
            env[dest] = c[key]
    return env


def snapshot(url):
    result = {}
    with psycopg.connect(url) as c:
        c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        tables = c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetchall()
        for (table,) in tables:
            records = c.execute(sql.SQL("SELECT row_to_json(t)::text FROM {} t").format(sql.Identifier(table))).fetchall()
            # Canonical sorted rows include every field, not only row counts.
            canon = sorted(json.dumps(json.loads(v), sort_keys=True, separators=(",", ":")) for (v,) in records)
            result[table] = {"rows": len(canon), "sha256": hashlib.sha256("\n".join(canon).encode()).hexdigest()}
    return result


def run(out):
    source, restore = os.environ["SOURCE_DATABASE_URL"], os.environ["RESTORE_DATABASE_URL"]
    src, dst = conninfo_to_dict(source), conninfo_to_dict(restore)
    identity = lambda c: tuple(c.get(k, "") for k in ("host", "port", "dbname"))
    if identity(src) == identity(dst) or not dst.get("dbname", "").startswith("mmai_restore_"):
        raise ValueError("refusing unsafe restore destination")
    if snapshot(restore):
        raise ValueError("restore destination is not empty")
    out = Path(out)
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    dump = out / "backup.dump"
    before = snapshot(source)
    subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--file", str(dump)],
                   env=env_for(source), check=True, capture_output=True)
    os.chmod(dump, 0o600)
    subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--dbname", dst["dbname"], str(dump)],
                   env=env_for(restore), check=True, capture_output=True)
    after = snapshot(restore)
    stable = snapshot(source)
    report = {"source_stable_during_drill": before == stable, "restored_rows_and_fields_equal": before == after,
              "tables": after, "backup_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
              "outgoing_messages": "not attempted; restart must keep all delivery switches off"}
    (out / "verification.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if before != stable or before != after:
        raise RuntimeError("recovery comparison failed; preserve all evidence")
    return {"tables": len(after), "rows": sum(t["rows"] for t in after.values()), "verified": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.output), sort_keys=True))
    except Exception as ex:
        # Driver/category only; PostgreSQL errors can contain connection details.
        print(json.dumps({"verified": False, "failure_type": type(ex).__name__}))
        raise SystemExit(1)
