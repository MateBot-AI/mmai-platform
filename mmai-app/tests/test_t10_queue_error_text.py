"""T10 presentation of the AI queue's last_error (STATE fix, 8 Nov 2026): the server stores a fixed category
(PROVIDER_CREDENTIALS / PROVIDER_ERROR / UNEXPECTED:<Type> / QUEUE_WORKER:<Type>) and the staff page maps it to one fixed plain
sentence. Staff never see the category code, an exception type name, provider prose or any unknown raw string.

The mapping is extracted from the real client/staff.js and run under Node; the source-level checks run without Node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import ai_queue

APP = Path(__file__).resolve().parents[1]
STAFF_JS = (APP / "client" / "staff.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

SERVER_CATEGORIES = [ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS, ai_queue.STATE_ERROR_PROVIDER,
                     f"{ai_queue.STATE_ERROR_UNEXPECTED}:TimeoutError", f"{ai_queue.STATE_ERROR_WORKER}:ProgrammingError"]
HOSTILE = ["Error code: 401 - {'type': 'authentication_error', 'message': 'invalid x-api-key'}", "<script>alert(1)</script>",
           "SOMETHING_NEW:ValueError", "", None, 42]


def _mapping_source():
    m = re.search(r"\n  const AI_ERROR_TEXT = \{.*?\n  \};\n  const aiErrorText = [^\n]+\n", STAFF_JS, re.S)
    assert m, "AI_ERROR_TEXT / aiErrorText not found in client/staff.js"
    return m.group(0)


def test_staff_page_never_renders_the_raw_last_error():
    assert "esc(q.last_error)" not in STAFF_JS, "the raw category/string must not reach the page"
    assert "aiErrorText(q.last_error)" in STAFF_JS
    src = _mapping_source()
    for cat in (ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS, ai_queue.STATE_ERROR_PROVIDER, ai_queue.STATE_ERROR_UNEXPECTED, ai_queue.STATE_ERROR_WORKER):
        assert re.search(rf"\n    {cat}: '", src), f"no fixed sentence for server category {cat}"
    assert "|| '" in src, "a generic fallback sentence is required for anything unrecognised"


def test_server_status_exposes_last_error_at_for_the_page():
    src = (APP / "server" / "app.py").read_text(encoding="utf-8")
    assert re.search(r'"ai_queue": \{k: v for k, v in ai_queue.status\(db\).items\(\) if k in \([^)]*"last_error", "last_error_at"', src), \
        "the staff status whitelist must pass last_error_at alongside last_error"


@pytest.mark.skipif(NODE is None, reason="Node is not installed; the source-level checks above still ran")
def test_every_category_maps_to_a_fixed_plain_sentence_under_node():
    script = _mapping_source() + "\nconst cases = JSON.parse(process.argv[1]);\n" \
             "process.stdout.write(JSON.stringify(cases.map(c => aiErrorText(c))));\n"
    cases = SERVER_CATEGORIES + HOSTILE
    out = subprocess.run([NODE, "-e", script, json.dumps(cases)], capture_output=True, text=True, check=True).stdout
    texts = json.loads(out)
    fixed = set(re.findall(r"(?:: |\|\| )'([^']+)'", _mapping_source()))   # the four category sentences + the `|| '...'` fallback
    assert len(fixed) == 5, fixed   # four categories + the fallback
    for case, text in zip(cases, texts):
        assert text in fixed, (case, text)
        assert text[0].isupper() and text.endswith("."), text
        for word in ("PROVIDER", "QUEUE_WORKER", "UNEXPECTED", "Error", "401", "<", "x-api-key"):
            assert word not in text, (case, text)
        if isinstance(case, str) and case and case not in fixed:
            assert case not in text, "the raw input never appears in the staff text"
    by = dict(zip(cases, texts))
    assert by[ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS] != by[ai_queue.STATE_ERROR_PROVIDER], "credentials are distinguished (retrying is pointless)"
    assert by["UNEXPECTED:TimeoutError"] == by.get("UNEXPECTED:TimeoutError") and "Type" not in by["UNEXPECTED:TimeoutError"]
    assert by["SOMETHING_NEW:ValueError"] == by[""] == texts[cases.index(None)], "unknown, empty and missing all get the same generic line"
