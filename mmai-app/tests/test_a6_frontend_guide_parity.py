"""A6(b) parity across the three rate-kind implementations (8 Nov 2026 root ruling: literal register wording, no same-year
condition; it was a code/register gap, not a rule):

  * server/rules.rate_kind                      (engine, HomeBot server guide via homebot.py)
  * client/form.js guideKind                    (MateBot price guide, reviewer-owned file; patched in coordination)
  * homebot-app/form.js rateKind                (HomeBot price guide)

The two browser functions are extracted from the real source files and run under Node with TZ=UTC, Australia/Melbourne and
America/Los_Angeles: the month of a civil date must come from the ISO text, never from Date (root finding: getMonth() shifted
1 January to December west of UTC). Skipped when Node is absent; the Python side still runs."""
import json
import re
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from server import rules

APP = Path(__file__).resolve().parents[1]
CLIENT_JS = APP / "client" / "form.js"
HOMEBOT_JS = APP.parent / "homebot-app" / "form.js"
NODE = shutil.which("node")
TIMEZONES = ("UTC", "Australia/Melbourne", "America/Los_Angeles")

# every (start, end) that matters for the tariff test, with the register's answer computed independently of rules.rate_kind
CASES = [
    ("2027-01-01", "2027-07-01"), ("2027-01-10", "2027-07-20"), ("2027-02-20", "2027-07-01"), ("2027-02-28", "2027-07-31"),
    ("2027-01-15", "2028-07-10"), ("2028-02-29", "2029-07-01"), ("2027-02-01", "2030-07-20"),          # later Julys
    ("2026-12-31", "2027-07-10"), ("2027-03-01", "2027-07-10"), ("2027-01-15", "2028-06-30"), ("2027-01-15", "2028-08-01"),
    ("2027-07-01", "2028-01-15"), ("2027-01-31", "2027-07-31"), ("2027-02-01", "2027-07-01"), ("2027-01-01", "2027-12-31"),
    ("2027-12-01", "2028-07-01"), ("2027-01-01", "2027-06-30"), ("2027-01-01", "2027-08-01"),
]


def register_kind(s: str, e: str) -> str:
    sm, em = int(s[5:7]), int(e[5:7])
    return "6m" if sm in (1, 2) and em == 7 else "12m"


def _extract(src: str, name: str) -> str:
    """The single-line arrow/function definition and its helper from the real file, by name."""
    lines = [l for l in src.splitlines() if re.match(rf"\s*(const {name} = |function {name}\()", l)]
    assert len(lines) == 1, f"{name} must be defined exactly once"
    return lines[0]


def _helpers(src: str, names) -> str:
    out = []
    for n in names:
        found = [l for l in src.splitlines() if re.match(rf"\s*const {n} = ", l)]
        assert len(found) == 1, n
        out.append(found[0])
    return "\n".join(out)


def _run_node(script: str, tz: str) -> dict:
    env = {"TZ": tz, "PATH": "/usr/bin:/bin:/usr/local/bin"}
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_server_rate_kind_matches_register_wording():
    for s, e in CASES:
        assert rules.rate_kind(date.fromisoformat(s), date.fromisoformat(e)) == register_kind(s, e), (s, e)


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("tz", TIMEZONES)
def test_matebot_client_guide_kind_matches_server_in_every_timezone(tz):
    src = CLIENT_JS.read_text()
    script = _helpers(src, ["ymdParts"]) + "\n" + _extract(src, "guideKind") + "\n" + \
        f"const cases = {json.dumps(CASES)}; console.log(JSON.stringify(Object.fromEntries(cases.map(([a,b]) => [a+'|'+b, guideKind(a,b)]))));"
    got = _run_node(script, tz)
    for s, e in CASES:
        assert got[f"{s}|{e}"] == register_kind(s, e) == rules.rate_kind(date.fromisoformat(s), date.fromisoformat(e)), (tz, s, e)
    assert got["2027-01-01|2027-07-01"] == "6m"        # root's west-of-UTC reproduction: shifted to December under LA before the fix
    assert _run_node(_helpers(src, ["ymdParts"]) + "\n" + _extract(src, "guideKind") + "\nconsole.log(JSON.stringify([guideKind('', '2027-07-01'), guideKind('2027-01-01', null), guideKind('01/01/2027', '2027-07-01')]))", tz) == ["12m", "12m", "12m"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("tz", TIMEZONES)
def test_homebot_guide_rate_kind_matches_server_in_every_timezone(tz):
    src = HOMEBOT_JS.read_text()
    script = _helpers(src, ["ymd"]) + "\n" + _extract(src, "rateKind") + "\n" + \
        f"const cases = {json.dumps(CASES)}; console.log(JSON.stringify(Object.fromEntries(cases.map(([a,b]) => [a+'|'+b, rateKind(a,b)]))));"
    got = _run_node(script, tz)
    for s, e in CASES:
        assert got[f"{s}|{e}"] == register_kind(s, e) == rules.rate_kind(date.fromisoformat(s), date.fromisoformat(e)), (tz, s, e)


def test_no_date_object_month_or_year_reads_in_either_guide():
    """The guide must not go through Date for the tariff decision (civil-date months shift west of UTC)."""
    for path, name in ((CLIENT_JS, "guideKind"), (HOMEBOT_JS, "rateKind")):
        line = _extract(path.read_text(), name)
        assert "new Date" not in line and "getMonth" not in line and "getFullYear" not in line, (path.name, name)
        assert "getFullYear" not in line and ".y ===" not in line and ".y==" not in line, "same-year condition must be gone"
