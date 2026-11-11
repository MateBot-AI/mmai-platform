#!/usr/bin/env python3
"""Export reviewed source only, never preview accounts or runtime databases."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

ROOT_FILES = {".dockerignore", "mmai-app/Dockerfile", "mmai-app/start.sh", "mmai-app/publish.env",
              "mmai-app/requirements.txt", "mmai-app/README.md", "mmai-app/FLOWS.md",
              "mmai-app/DEPLOY_DIGITALOCEAN.md", "mmai-app/NOTIFICATION_OPERATIONS.md",
              "mmai-app/RELEASE_OPERATIONS.md", "mmai-app/.do/app.yaml",
              "mmai-app/data/ALM_Apartments_2026-10-23_features_template.xlsx"}
TREE_ROOTS = ("mmai-app/server/", "mmai-app/client/", "mmai-app/tests/",
              "mmai-app/tools/", "homebot-app/")
EXTENSIONS = {".py", ".sh", ".js", ".mjs", ".html", ".css", ".svg", ".webp",
              ".png", ".jpg", ".jpeg", ".ico", ".json", ".md", ".txt", ".woff2"}
INVENTORY = "mmai-app/data/ALM_Apartments_2026-10-23.xlsx"
SECRET = re.compile(rb"(?:sk-ant-[A-Za-z0-9_-]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)")


def export(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.exists() or root == destination or root in destination.parents:
        raise ValueError("destination must be new and outside the source package")
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if any(x in path.parts for x in ("__pycache__", "node_modules", ".git", "reports", "probes", "bootstrap")):
            # Test source under tools/bootstrap is allowed, but not a seed database.
            if not (rel.startswith("mmai-app/tools/bootstrap/") and path.suffix == ".py"):
                continue
        allowed = rel in ROOT_FILES or rel == INVENTORY or (
            rel.startswith(TREE_ROOTS) and path.suffix.lower() in EXTENSIONS
            and ".env" not in path.name and not path.name.endswith(".log"))
        if not allowed:
            continue
        body = path.read_bytes()
        if SECRET.search(body) or body.startswith(b"SQLite format 3"):
            raise ValueError("credential or database signature refused: " + rel)
        files.append((rel, body))
    required = {".dockerignore", "mmai-app/server/app.py", "mmai-app/client/index.html",
                "homebot-app/index.html", INVENTORY,
                "mmai-app/data/ALM_Apartments_2026-10-23_features_template.xlsx"}
    if not required.issubset({p for p, _ in files}):
        raise ValueError("required source or reference fixture missing")
    destination.mkdir(mode=0o700)
    manifest = {}
    for rel, body in files:
        out = destination / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(body)
        if out.suffix == ".sh":
            out.chmod(0o755)
        manifest[rel] = hashlib.sha256(body).hexdigest()
    (destination / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"files": len(files), "manifest_sha256": hashlib.sha256((destination / "SOURCE-MANIFEST.json").read_bytes()).hexdigest()}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True)
    p.add_argument("--destination", required=True)
    args = p.parse_args()
    print(json.dumps(export(args.root, args.destination), sort_keys=True))
