#!/usr/bin/env bash
# Published-site start command: bootstrap a FRESH store, then hand over to start.sh.
#
#   run command:  bash tools/start_published.sh      (same environment variables as start.sh)
#
# Rule: an existing store is never touched. Only when the store path does not exist at all (no main file, no -wal, no -shm)
# is bootstrap/staff_accounts.db copied into place, so the first start of a fresh published backend already has the staff
# sign-ins (salted hashes carried over from the preview store; no plain-text passwords, no customer records). Apartments are
# imported by the server's own first-run bootstrap from data/. If the seed is missing, the server simply creates a new store.
# The fresh copy is made owner-writable and private (0600) whatever the seed's mode; the seed itself is never changed.
#
# MMAI_BOOTSTRAP_ONLY=1 runs the bootstrap step and exits without starting the server (tests).
set -euo pipefail
cd "$(dirname "$0")/.."

DB="${MMAI_DB:-server/data/mmai.db}"
SEED="${MMAI_BOOTSTRAP_SEED:-bootstrap/staff_accounts.db}"   # the override exists for the bootstrap tests only

if [ -n "${DATABASE_URL:-}" ]; then
  echo "[mmai bootstrap] DATABASE_URL set: PostgreSQL store, no SQLite bootstrap"
elif [ -e "$DB" ] || [ -e "$DB-wal" ] || [ -e "$DB-shm" ] || [ -e "$DB-journal" ]; then
  echo "[mmai bootstrap] existing store kept at $DB (nothing copied)"
elif [ -f "$SEED" ]; then
  mkdir -p "$(dirname "$DB")"
  # copy to a temporary name first, then rename, so a half-written file can never be mistaken for a store
  cp "$SEED" "$DB.bootstrap.tmp"
  # the NEW copy must be the server's own writable, private store even when the seed is read-only (a frozen or read-only
  # checkout): cp carries the seed's mode, and a read-only store makes the first "PRAGMA journal_mode=WAL" fail. Only the new
  # temporary copy is changed - never the seed, never an existing store.
  chmod 0600 "$DB.bootstrap.tmp"
  mv -n "$DB.bootstrap.tmp" "$DB"
  if [ -e "$DB.bootstrap.tmp" ]; then rm -f "$DB.bootstrap.tmp"; echo "[mmai bootstrap] store appeared meanwhile; kept it"; fi
  echo "[mmai bootstrap] fresh store at $DB from $SEED (staff accounts only)"
else
  echo "[mmai bootstrap] no store and no seed ($SEED): the server will create a new store"
fi

if [ "${MMAI_BOOTSTRAP_ONLY:-0}" = "1" ]; then
  exit 0
fi
exec bash start.sh
