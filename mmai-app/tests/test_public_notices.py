"""Public notices (16 Nov 2026): the Terms of Use and Privacy Policy pages.

Covers: both pages answer without sign-in while the pilot gate is on and while it is off; nothing else opens with them; the pages
carry the notice the journey provider asks for and the approved contact; they are fixed pages with no script and no draft notes;
they are linked from the sign-in page, both forms and the staff workspace; and reading them changes nothing.
"""
import os
import re

import pytest

from tests.test_pilot_gate import _counts, _login, gated, ungated  # noqa: F401  (fixtures + helpers)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGAL_DIR = os.path.join(ROOT, "server", "legal")
PAGES = {"/terms": ("terms.html", "Terms of Use", 7), "/privacy": ("privacy.html", "Privacy Policy", 11)}
LINKS = ('href="/terms"', 'href="/privacy"')
GOOGLE_TERMS = "https://maps.google.com/help/terms_maps/"
GOOGLE_PRIVACY = "https://policies.google.com/privacy"


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html))


def test_notices_are_readable_without_sign_in_behind_the_gate(gated):
    c, appmod, _ = gated
    before = _counts(appmod)
    for path, (name, title, _n) in PAGES.items():
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
        assert r.text == open(os.path.join(LEGAL_DIR, name), encoding="utf-8").read()
        assert f"<h1>{title}</h1>" in r.text
        assert r.headers["cache-control"] == "private, no-store, no-transform"   # not cached, and passed on by the edge as sent
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "set-cookie" not in r.headers
        assert c.head(path).status_code == 200
    assert _counts(appmod) == before                                      # no session, record or audit line from reading them


def test_nothing_else_opens_with_the_notices(gated):
    c, appmod, _ = gated
    before = _counts(appmod)
    for method, path in (("POST", "/terms"), ("PUT", "/privacy"), ("DELETE", "/terms"), ("GET", "/terms/"), ("GET", "/privacy/"),
                         ("GET", "/terms.html"), ("GET", "/privacy.html"), ("GET", "/legal/terms.html"), ("GET", "/legal/"),
                         ("GET", "/server/legal/terms.html"), ("GET", "/terms/x"), ("GET", "/privacy/../app.js"), ("GET", "/Terms"),
                         ("GET", "/terms%2F..%2Fapp.js"), ("GET", "/app.js"), ("GET", "/index.html"), ("GET", "/styles.css"),
                         ("GET", "/img/google_maps_logo.svg"), ("GET", "/homebot/"), ("GET", "/api/public/meta"), ("GET", "/api/apartments")):
        r = c.request(method, path)
        assert r.status_code == 401, (method, path, r.status_code)
    for path in ("/", "/homebot/", "/terms/", "/privacy/x"):
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 303 and r.headers["location"].startswith("/pilot/"), path
    assert _counts(appmod) == before


def test_open_list_gains_exactly_the_two_notices():
    from server import security as sec
    assert sec.PUBLIC_NOTICE_PATHS == ("/terms", "/privacy")
    for path in sec.PUBLIC_NOTICE_PATHS:
        assert sec.pilot_open_path("GET", path) and sec.pilot_open_path("HEAD", path)
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            assert not sec.pilot_open_path(method, path), (method, path)
        for near in (path + "/", path + ".html", path.upper(), path + "/x", "/legal" + path, path + "?"):
            assert not sec.pilot_open_path("GET", near), near


def test_notices_are_readable_with_the_gate_off_and_signed_in(ungated):
    c, _, _ = ungated
    for path, (_name, title, _n) in PAGES.items():
        r = c.get(path)
        assert r.status_code == 200 and f"<h1>{title}</h1>" in r.text
    assert c.post("/terms").status_code == 405
    assert _login(c, "anna").status_code == 200
    assert c.get("/privacy").status_code == 200


def test_notice_wording_and_page_safety():
    names = sorted(os.listdir(LEGAL_DIR))
    assert names == ["privacy.html", "terms.html"]
    for path, (name, title, clauses) in PAGES.items():
        html = open(os.path.join(LEGAL_DIR, name), encoding="utf-8").read()
        text, low = _text(html), html.lower()
        assert len(re.findall(r"<li>", html)) == clauses, name
        assert f"<title>{title} · MateBot AI</title>" in html and 'lang="en-AU"' in html and "noindex" in html
        assert "Matebot AI Pty Ltd" in text and "Last updated: 16 November 2026." in text
        # the contact address is plain text inside the markers that stop the host's edge swapping it for a placeholder
        marked = '<!--email_off--><a href="mailto:reservations@almelb.com.au">reservations@almelb.com.au</a><!--/email_off-->'
        assert html.count(marked) >= 1 and html.count("reservations@almelb.com.au") == 2 * html.count(marked), name
        # fixed pages: no script, no form, nothing loaded from elsewhere, no draft or approval notes left in
        for word in ("<script", "<form", "<input", "<iframe", "<img", "<link", "onclick", "javascript:", "http://", "@import", "url("):
            assert word not in low, (name, word)
        assert "[" not in text and "]" not in text, name
        for word in ("this draft", "draft for", "approval", "approve", "date of publication", "recommended contact",
                     "proposed process", "not legal advice"):
            assert word not in text.lower(), (name, word)
        assert not re.search(r"\bBill\b", text), name
        assert set(re.findall(r'href="(https://[^"]+)"', html)) <= {GOOGLE_TERMS, GOOGLE_PRIVACY}
        assert all('rel="noopener noreferrer"' in a for a in re.findall(r'<a href="https://[^>]*>', html))
        other = "/privacy" if path == "/terms" else "/terms"
        assert f'<a href="{other}">' in html and '<a href="/">Back to MateBot AI</a>' in html
        assert not re.search(r"\b(?:T|H|GP|I|O|M)\d+(?:\([a-z]\))?\b", text), name       # plain language, no register codes
    terms = open(os.path.join(LEGAL_DIR, "terms.html"), encoding="utf-8").read()
    # the notice the journey provider's terms ask for, with both of its links
    assert ("This service uses Google Maps features and content. Use of Google Maps features and content is subject to the "
            "then-current versions of the Google Maps/Google Earth Additional Terms of Service and the Google Privacy Policy.") in _text(terms)
    assert f'<a href="{GOOGLE_TERMS}" rel="noopener noreferrer">Google Maps/Google Earth Additional Terms of Service</a>' in terms
    assert f'<a href="{GOOGLE_PRIVACY}" rel="noopener noreferrer">Google Privacy Policy</a>' in terms
    privacy = _text(open(os.path.join(LEGAL_DIR, "privacy.html"), encoding="utf-8").read())
    for line in ("These are used only to introduce you to the people in a proposed group; they are not used to decide who is matched "
                 "with whom or to rank anyone.",
                 "Automatic sending is disabled during the current controlled pilot.",
                 "Journey estimates are shown to ALM staff when they look at your options and are not kept by MateBot AI.",
                 "hosted with DigitalOcean in its Sydney region",
                 "your details are kept for 12 months from our last contact with you and are then deleted",
                 "No advertising or analytics cookies are used.",
                 "staff member signed in; it can be read only by this site, is sent only over a secure connection, lasts up to 30 days"):
        assert line in privacy, line
    assert "__Host-mmai_session" in privacy


def test_notices_are_linked_from_the_sign_in_page_forms_and_staff_workspace():
    files = {"sign-in page": os.path.join(ROOT, "server", "pilot", "index.html"),
             "MateBot form and staff workspace": os.path.join(ROOT, "client", "index.html"),
             "HomeBot form": os.path.join(os.path.dirname(ROOT), "homebot-app", "index.html")}
    for label, path in files.items():
        html = open(path, encoding="utf-8").read()
        for link in LINKS:
            assert link in html, (label, link)
    client = open(files["MateBot form and staff workspace"], encoding="utf-8").read()
    public, staff = client.split('<div id="view-staff"', 1)
    for link in LINKS:
        assert link in public and link in staff.split("<template", 1)[0]      # under the form and under the staff workspace
    # a link from a form opens beside it, so a part-filled form is not lost
    for label in ("MateBot form and staff workspace", "HomeBot form"):
        html = open(files[label], encoding="utf-8").read()
        assert all('target="_blank" rel="noopener"' in a for a in re.findall(r'<a href="/(?:terms|privacy)"[^>]*>', html)), label


@pytest.mark.parametrize("path", ["/terms", "/privacy"])
def test_signed_in_pilot_user_reads_the_same_page(gated, path):
    c, _, _ = gated
    outside = c.get(path).text
    assert _login(c).status_code == 200
    assert c.get(path).text == outside
