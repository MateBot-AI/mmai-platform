"""The MateBot page (public form and staff workspace) as this server hands it out at /.

client/index.html carries a placeholder for the API address. The hosted preview's publisher replaced it; on our own host
nothing did, and client/api.js stops on an unreplaced placeholder on any host that is not local. Signed-in staff then saw
"The MateBot AI service is not reachable right now (Cannot read properties of undefined (reading 'get'))" (first seen in a
browser on 17 November 2026). Local addresses are exempt from that stop, so no local run showed it. These tests read the
page as served and run the page's own first script under a real host name.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

from tests.test_pilot_gate import _login, gated, ungated  # noqa: F401  (fixtures)

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(os.path.dirname(HERE), "client")
META = re.compile(r'<meta name="mmai-api-base" content="([^"]*)">')
HTML = {"Accept": "text/html"}

# Runs client/api.js as a browser on the given host would: the only page fact it reads is the meta tag's content.
NODE_CHECK = r"""
const fs = require('fs'), vm = require('vm');
const [apiJs, host, content] = process.argv.slice(2);
const ctx = { window: {}, location: { hostname: host, protocol: 'https:' }, URLSearchParams,
  document: { querySelector: s => s === 'meta[name="mmai-api-base"]' ? { content } : null }, fetch: () => new Promise(() => {}) };
vm.createContext(ctx);
let loadError = null, firstCallError = null;
try { vm.runInContext(fs.readFileSync(apiJs, 'utf8'), ctx); } catch (e) { loadError = e.message; }
try { vm.runInContext("window.MMAI_API.get('/api/public/meta')", ctx); } catch (e) { firstCallError = e.message; }
console.log(JSON.stringify({ loadError, firstCallError, defined: !!ctx.window.MMAI_API, base: ctx.window.MMAI_API ? ctx.window.MMAI_API.base : null }));
"""


def _run_api_js(tmp_path, host, content):
    script = tmp_path / "check.js"
    script.write_text(NODE_CHECK, encoding="utf-8")
    out = subprocess.run(["node", str(script), os.path.join(CLIENT, "api.js"), host, content], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_the_source_file_still_carries_the_placeholder_for_the_preview_publisher():
    with open(os.path.join(CLIENT, "index.html"), encoding="utf-8") as f:
        assert re.fullmatch(r"__PORT_\d+__", META.search(f.read())[1])


def test_page_is_served_with_the_placeholder_rewritten(ungated):
    c, _, _ = ungated
    with open(os.path.join(CLIENT, "index.html"), encoding="utf-8") as f:
        source = f.read()
    for path in ("/", "/index.html", "/?test=1"):
        r = c.get(path, headers=HTML)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
        assert META.findall(r.text) == [""] and "__PORT_" not in r.text, path
        assert r.headers["cache-control"] == "no-store", path
        # nothing else about the page changes
        assert r.text == META.sub('<meta name="mmai-api-base" content="">', source), path
    h = c.head("/")
    assert h.status_code == 200 and h.headers["cache-control"] == "no-store" and not h.content
    assert c.post("/", json={}).status_code == 405 and c.post("/index.html", json={}).status_code == 405


def test_other_client_files_are_served_unchanged_and_nothing_falls_back_to_the_page(ungated):
    c, _, _ = ungated
    for name in ("api.js", "app.js", "staff.js", "form.js", "styles.css"):
        with open(os.path.join(CLIENT, name), "rb") as f:
            assert c.get("/" + name).content == f.read(), name
    for missing in ("/no-such-page", "/staff", "/index.htm", "/img/", "/index.html/extra", "/no/such/file.js"):
        r = c.get(missing, headers=HTML)
        assert r.status_code == 404 and "mmai-api-base" not in r.text, missing


def test_pilot_gate_still_decides_who_gets_the_page(gated):
    c, _, _ = gated
    for path in ("/", "/index.html", "/?test=1"):
        r = c.get(path, headers=HTML)
        assert r.status_code == 303 and r.headers["location"].startswith("/pilot/") and "mmai-api-base" not in r.text, path
        r = c.get(path)   # not a page request
        assert r.status_code == 401 and "mmai-api-base" not in r.text, path
        assert c.head(path).status_code == 401, path
    assert _login(c).status_code == 200
    r = c.get("/", headers=HTML)
    # behind the gate every reply carries the gate's own, stricter, no-store header
    assert r.status_code == 200 and META.findall(r.text) == [""] and "no-store" in r.headers["cache-control"]
    assert r.headers.get("content-security-policy") and r.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_pages_first_script_loads_on_a_real_host_name(ungated, tmp_path):
    c, _, _ = ungated
    served = META.search(c.get("/", headers=HTML).text)[1]
    for host in ("app.matebot.com.au", "matebot-ai-example.ondigitalocean.app", "localhost"):
        got = _run_api_js(tmp_path, host, served)
        assert got == {"loadError": None, "firstCallError": None, "defined": True, "base": ""}, host
    # the fault this guards against: the file as stored, on a host that is not local
    with open(os.path.join(CLIENT, "index.html"), encoding="utf-8") as f:
        stored = META.search(f.read())[1]
    bad = _run_api_js(tmp_path, "app.matebot.com.au", stored)
    assert bad["defined"] is False and bad["loadError"] == "Backend proxy placeholder was not rewritten"
    assert bad["firstCallError"] == "Cannot read properties of undefined (reading 'get')"
