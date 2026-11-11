"""MMAI server package.

Start-up configuration: if `publish.env` exists in the app folder (next to `server/`), its KEY=VALUE lines are loaded into the
process environment *only where the variable is not already set*. The file is non-secret: it holds settings and the NAMES of
environment variables (for example MMAI_AI_URL_VAR=ANTHROPIC_BASE_URL_X), never a key or password. Secrets reach the process
only as real environment variables injected by the host (the publish credential proxy), never from a file in the repository.
"""
from __future__ import annotations

import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLISH_ENV = os.path.join(APP_DIR, "publish.env")


def load_publish_env(path: str = PUBLISH_ENV) -> dict:
    """Load KEY=VALUE lines (comments and blanks ignored) with os.environ.setdefault. Returns what was applied."""
    applied = {}
    if not os.path.isfile(path):
        return applied
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    applied[key] = value
    except OSError:
        pass
    return applied


load_publish_env()
