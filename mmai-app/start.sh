#!/usr/bin/env bash
# MMAI backend start command (preview and published). Runs from any working directory.
# Settings and the NAMES of credential variables can also be put in publish.env (non-secret; loaded by server/__init__.py
# only for variables not already set). Secrets are never read from files.
set -euo pipefail
cd "$(dirname "$0")"
export MMAI_TRUST_VISITOR_ID="${MMAI_TRUST_VISITOR_ID:-0}"
export MMAI_SCHEDULER_DATASETS="${MMAI_SCHEDULER_DATASETS:-real,test}"
exec python3 -m uvicorn server.app:app --host 0.0.0.0 --port "${PORT:-8000}"
