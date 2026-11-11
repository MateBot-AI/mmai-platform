"""Run the real frontend TEST expression across review, hash and query modes."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("review,hash_value,search,expected", [
    (True, "", "", True), (True, "#/homebot?test=0", "?test=0", True),
    (False, "#/homebot?test=1", "", True), (False, "#/homebot", "?test=1", True),
    (False, "#/homebot", "?x=2&test=1&y=3", True),
    (False, "#/homebot?test=0", "?test=0", False),
    (False, "#/homebot", "?notest=1", False), (False, "", "", False),
])
def test_frontend_review_and_test_flags(review, hash_value, search, expected):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required to execute browser flag expression")
    script = (Path(__file__).resolve().parents[2] / "homebot-app" / "app.js").read_text()
    expression = next(line for line in script.splitlines() if line.startswith("  const TEST = "))
    code = (f"const REVIEW={json.dumps(review)}; const location={json.dumps({'hash': hash_value, 'search': search})};\n"
            + expression + "\nconsole.log(JSON.stringify(TEST));")
    result = subprocess.run([node, "-e", code], capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) is expected
