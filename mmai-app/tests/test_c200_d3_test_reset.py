"""D3 (C200, 9 Nov 2026): the TEST box of the public form must survive "Start again".

Found by root in a real browser on the fixed tree: open /#/?test=1, submit one enquiry (is_test true), click "Start again" -> the global
TEST DATA badge stays, app.js would still post is_test=true, but `form.elements.is_test.checked` was FALSE: the context ticked the box
only in script, and form.reset() returns every control to its DEFAULT state (defaultChecked, which was never set). The box must agree
with the badge and the posted flag at every moment.

The browser's reset semantics are simulated in node on the actual source text: the context-application statement is extracted from
client/form.js and run against a minimal form stub whose reset() behaves like a browser (checked := defaultChecked). The same stub shows
the pre-fix single statement losing the tick. Source checks pin the wiring (re-applied after the restart's form.reset(); staff mode and
real mode untouched; cache tag bumped). Node-free environments skip the simulation and keep the source checks.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import os
CLIENT = Path(os.environ.get("MMAI_D3_CLIENT_DIR") or Path(__file__).resolve().parents[1] / "client")   # override: before-evidence on another tree
FORM = (CLIENT / "form.js").read_text(encoding="utf-8")
INDEX = (CLIENT / "index.html").read_text(encoding="utf-8")
APP = (CLIENT / "app.js").read_text(encoding="utf-8")
STAFF = (CLIENT / "staff.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

APPLY_RE = re.compile(r"const applyTestContext = \(\) => \{(.*?)\};\n", re.S)
PRE_FIX = "if (cfg.isTest && form.elements.is_test) form.elements.is_test.checked = true;"

SIM = r"""
// minimal browser-like form stub: reset() returns every checkbox to its default state, exactly what the browser does
function makeForm() {
  const box = { type: 'checkbox', name: 'is_test', checked: false, defaultChecked: false };
  const form = { elements: { is_test: box }, reset() { box.checked = box.defaultChecked; } };
  return { form, box };
}
function run(body, cfg) {
  const { form, box } = makeForm();
  const apply = new Function('form', 'cfg', body);   // the statement(s) under test, as they appear in the source
  apply(form, cfg);
  const afterContext = box.checked;
  form.reset();                                      // Start again
  const afterReset = box.checked;
  apply(form, cfg);                                  // re-application after the reset (the fix wires this in the restart handler)
  return { afterContext, afterReset, afterResetAndReapply: box.checked, defaultChecked: box.defaultChecked };
}
const [body, prefix] = JSON.parse(process.argv[1]);
console.log(JSON.stringify({
  fixed_test: run(body, { isTest: true }),
  fixed_real: run(body, { isTest: false }),
  fixed_staff: run(body, { mode: 'staff' }),
  prefix_test: run(prefix, { isTest: true }),
}));
"""


def _apply_body() -> str:
    m = APPLY_RE.search(FORM)
    assert m, "applyTestContext must be defined in client/form.js"
    return m.group(1)


def _simulate() -> dict:
    r = subprocess.run([NODE, "-e", SIM, json.dumps([_apply_body(), PRE_FIX])], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_context_sets_the_default_so_the_tick_survives_a_browser_reset():
    body = _apply_body()
    assert "defaultChecked = true" in body and "checked = true" in body
    assert "cfg.isTest" in body, "only the page's TEST context applies; real mode and staff mode are untouched"


def test_restart_reapplies_the_test_context_after_form_reset():
    restart = [ln for ln in FORM.splitlines() if 'data-role="restart"' in ln]
    assert len(restart) == 1
    ln = restart[0]
    assert "form.reset(); applyTestContext();" in ln, "the TEST context must be re-applied right after Start again resets the form"
    code_lines = [ln for ln in FORM.splitlines() if ".reset()" in ln and not ln.strip().startswith("//")]
    assert len(code_lines) == 1, "every form reset in form.js is the Start again one and is covered"


def test_real_mode_and_staff_mode_are_not_broadened():
    # app.js alone decides the page context (URL ?test=1, a TEST saved form / HomeBot hand-over, a TEST enquirer's share link) and
    # always posts it; form.js only mirrors cfg.isTest into the box. Nothing else in form.js touches is_test.
    assert "const effTest = isTest || !!(draft && draft.is_test) || !!(m.mate_link && m.mate_link.valid && m.mate_link.is_test);" in APP
    assert "if (effTest) answers.is_test = true;" in APP
    touches = [ln for ln in FORM.splitlines() if "is_test" in ln and not ln.strip().startswith("//")]
    assert len(touches) == 2, touches   # applyTestContext + collect()
    # staff mode mounts the form without a page context: the box stays the staff member's explicit control
    staff_mount = STAFF[STAFF.index("views.new = async p =>"):][:1200]
    assert "isTest" not in staff_mount


def test_cache_tag_bumped_for_the_client_change():
    assert 'form.js?v=20261123a' in INDEX
    assert 'form.js?v=20261109ab' not in INDEX and 'form.js?v=20261109aa' not in INDEX


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_simulated_start_again_keeps_test_and_leaves_real_and_staff_alone():
    sim = _simulate()
    # the defect, reproduced on the pre-fix statement: ticked by the context, cleared by the reset
    assert sim["prefix_test"]["afterContext"] is True and sim["prefix_test"]["afterReset"] is False
    # fixed: ticked, default set, still ticked after the reset even before the re-application, and after it
    f = sim["fixed_test"]
    assert f == {"afterContext": True, "afterReset": True, "afterResetAndReapply": True, "defaultChecked": True}
    # real mode and staff mode: nothing is ticked, nothing becomes default
    for k in ("fixed_real", "fixed_staff"):
        assert sim[k] == {"afterContext": False, "afterReset": False, "afterResetAndReapply": False, "defaultChecked": False}, (k, sim[k])
