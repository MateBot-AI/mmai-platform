"""Google Ads tracking (25 Nov 2026): the click id and campaign names a visitor arrives with are saved with the enquiry, and
Google Tag Manager is told when an enquiry is saved.

Server: both public submit routes take one optional top-level `tracking` object, clean it (server/tracking.py: six keys, strings
only, control characters removed, trimmed, longer than 255 characters dropped, case untouched), keep it OUT of the answers, the
validation and the submission fingerprint, and save it in new nullable columns on `enquiries` and `homebot_enquiries`. It can never
fail a submission, never overwrite a first save, and is visible to staff only.
Client (run under node on the real source text, like the other client tests; skipped when node is absent): tracking.js capture and
storage rules, gtm.js and its staff-route exclusion, the extra `tracking` object on public submits only, and the completion event.
Page: the script tags, the Content-Security-Policy that lets the two pages load Google Tag Manager, and the unchanged API policy.
"""
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from server import db as dbmod
from server import tracking as trk
from server.db import row, rows
from tests.test_data_and_api import good_answers
from tests.test_homebot import body as hb_body, client  # noqa: F401  (client is the fixture)
from tests.test_pilot_gate import _login, gated, ungated  # noqa: F401  (fixtures)

APP = Path(__file__).resolve().parents[1]                  # mmai-app
CLIENT = APP / "client"
HOMEBOT = APP.parent / "homebot-app"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(not NODE, reason="node is not installed")
HTML = {"Accept": "text/html"}
LS, PS = chr(0x2028), chr(0x2029)   # the two Unicode line separators (written as code points so the file stays plain)

# A realistic click id: about 90 characters, upper and lower case, digits, '-' and '_' (it must come back exactly as sent).
GCLID = "Cj0KCQjwr4OwBhCbARIsAEcnnSxQ8mZ_kT1-hVdWuYpL3nBfRjO9sGcEaXyHiKzUwN7tAqMvPlD2eCbFgJoIrS0"
TRACKING = {
    "gclid": GCLID,
    "gbraid": "0AAAAAoJ_Qk-gBraidExample123",
    "wbraid": "CjkKCwiA_JWkBhCHARIoAK3zOVfy2b-wbraidExample",
    "utm_campaign": "AU_Search_Melbourne_Student_Apartments",
    "utm_ad_group": "Furnished-Apartments-CBD",
    "utm_keyword": "Furnished Apartment Melbourne CBD",
}
ABSENT = object()   # the request carries no `tracking` key at all


def _post(c, path, payload, key):
    """The JSON is written with ensure_ascii (as any browser-side escaper may), so a lone surrogate ("\\ud800") can be sent at all."""
    headers = {"Content-Type": "application/json", **({"X-Submission-Key": key} if key else {})}
    return c.anon.post(path, content=json.dumps(payload).encode("ascii"), headers=headers)


def _mate(c, tracking=ABSENT, key="k_" + "a" * 30, **over):
    answers = good_answers(is_test=True, **over)
    if tracking is not ABSENT:
        answers["tracking"] = tracking
    return _post(c, "/api/public/enquiries", answers, key)


def _hb(c, tracking=ABSENT, key="hb-gads-key-0001", **over):
    b = hb_body(is_test=True, **over)
    if tracking is not ABSENT:
        b["tracking"] = tracking
    return _post(c, "/api/public/homebot/enquiries", b, key)


SENDERS = {"matebot": (_mate, "enquiries"), "homebot": (_hb, "homebot_enquiries")}
BOTS = pytest.mark.parametrize("bot", ["matebot", "homebot"])
COLS = ", ".join(trk.COLUMNS)


def _saved(c, table, eid):
    return row(c.db.execute(f"SELECT {COLS} FROM {table} WHERE id=?", (eid,)))


def _all_null(r):
    return set(r) == set(trk.COLUMNS) and all(v is None for v in r.values())


def _count(c, table):
    return c.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------------------------------------ saved with the enquiry
@BOTS
def test_tracking_is_saved_with_the_enquiry(client, bot):
    send, table = SENDERS[bot]
    r = send(client, TRACKING)
    assert r.status_code == 201, r.text
    saved = _saved(client, table, r.json()["id"])
    assert {k: saved[k] for k in trk.KEYS} == TRACKING
    # server time, ISO, set because something was saved
    assert datetime.fromisoformat(saved["tracking_captured_at"])


@BOTS
def test_it_is_not_an_answer_and_not_in_the_reply(client, bot):
    send, table = SENDERS[bot]
    r = send(client, TRACKING)
    assert r.status_code == 201
    eid = r.json()["id"]
    answers = client.db.execute(f"SELECT answers_json FROM {table} WHERE id=?", (eid,)).fetchone()[0]
    assert "tracking" not in answers and GCLID not in answers and "utm_" not in answers
    assert GCLID not in r.text and not set(trk.COLUMNS) & set(r.json())


@BOTS
@pytest.mark.parametrize("tracking", [ABSENT, None, {}, {"gclid": ""}, {"gclid": "   "}, {"utm_campaign": "\x00\n"}],
                         ids=["absent", "null", "empty-object", "empty-string", "blank", "only-controls"])
def test_empty_or_absent_tracking_is_fine_and_leaves_the_columns_null(client, bot, tracking):
    send, table = SENDERS[bot]
    r = send(client, tracking)
    assert r.status_code == 201, r.text
    assert _all_null(_saved(client, table, r.json()["id"]))


@BOTS
def test_a_partial_object_saves_only_what_is_there(client, bot):
    send, table = SENDERS[bot]
    r = send(client, {"utm_campaign": "Brand-Melbourne", "gclid": None})
    saved = _saved(client, table, r.json()["id"])
    assert saved["utm_campaign"] == "Brand-Melbourne" and saved["gclid"] is None and saved["tracking_captured_at"]
    assert [k for k in trk.KEYS if saved[k] is not None] == ["utm_campaign"]


@BOTS
def test_unknown_keys_are_dropped(client, bot):
    send, table = SENDERS[bot]
    r = send(client, {"gclid": GCLID, "source": "alm-website", "email": "x@example.com", "is_test": False, "channel": "Phone",
                      "utm_source": "google", "utm_medium": "cpc", "evil": "x", "__proto__": {"gclid": "no"}})
    assert r.status_code == 201, r.text
    saved = _saved(client, table, r.json()["id"])
    assert saved["gclid"] == GCLID and [k for k in trk.KEYS if saved[k] is not None] == ["gclid"]
    row_ = row(client.db.execute(f"SELECT * FROM {table} WHERE id=?", (r.json()["id"],)))
    assert not {"source_x", "utm_source", "utm_medium", "evil"} & set(row_)   # no new columns appear from sent keys
    # the unknown keys did not leak into the enquiry itself: the TEST flag stayed what the form said, and the channel is the page's
    assert row_["is_test"] == 1 and row_["channel"] in ("Landing page", "HomeBot form")


@BOTS
def test_values_are_never_changed_case_included(client, bot):
    send, table = SENDERS[bot]
    assert 80 <= len(GCLID) <= 100 and GCLID != GCLID.lower() and GCLID != GCLID.upper()
    r = send(client, {**TRACKING, "gclid": "  " + GCLID + "\t"})   # only the ends are trimmed
    assert _saved(client, table, r.json()["id"]) == {**TRACKING, "tracking_captured_at": _saved(client, table, r.json()["id"])["tracking_captured_at"]}
    # characters that are not control characters stay exactly as sent, including inner spaces, quotes, markup and accented letters
    odd = "Été \"Melbourne\" <b>& more</b> + 100%"
    r = send(client, {"utm_campaign": odd}, key="k_" + "b" * 30 if bot == "matebot" else "hb-gads-key-0002")
    assert _saved(client, table, r.json()["id"])["utm_campaign"] == odd


@BOTS
def test_control_characters_are_stripped(client, bot):
    send, table = SENDERS[bot]
    r = send(client, {"gclid": "\x00Cj0K\nCQ\tjw\r\x1b[0mAB\x7f\x85" + LS + PS + " ", "utm_campaign": "Spring\x002027", "utm_keyword": "\x07\x08"})
    assert r.status_code == 201, r.text
    saved = _saved(client, table, r.json()["id"])
    assert saved["gclid"] == "Cj0KCQjw[0mAB" and saved["utm_campaign"] == "Spring2027"
    assert saved["utm_keyword"] is None   # nothing left once the controls are gone
    assert not re.search(r"[\x00-\x1f\x7f-\x9f" + LS + PS + "]", "".join(saved[k] or "" for k in trk.KEYS))


@BOTS
def test_over_long_values_are_dropped_not_cut_short(client, bot):
    send, table = SENDERS[bot]
    r = send(client, {"gclid": "G" * 255, "gbraid": "B" * 256, "utm_campaign": "c" * 5000, "utm_keyword": "é" * 255, "utm_ad_group": "é" * 256})
    assert r.status_code == 201, r.text
    saved = _saved(client, table, r.json()["id"])
    assert saved["gclid"] == "G" * 255 and saved["utm_keyword"] == "é" * 255          # 255 characters is allowed (characters, not bytes)
    assert saved["gbraid"] is None and saved["utm_campaign"] is None and saved["utm_ad_group"] is None   # longer: dropped whole


@BOTS
@pytest.mark.parametrize("tracking", [
    "gclid=abc", 123, 12.5, True, False, ["gclid", GCLID], [{"gclid": GCLID}], [], [[]],
    {"gclid": 123}, {"gclid": 1.5}, {"gclid": True}, {"gclid": ["x"]}, {"gclid": {"a": 1}}, {"gclid": None},
    {"gclid": "\ud800"},   # a lone surrogate is valid JSON but cannot be written as UTF-8
    {"gclid": "a\ud83d", "utm_campaign": "fine"},
    {"utm_campaign": {"$ne": ""}, "utm_keyword": {"nested": {"deeper": [1, 2, 3]}}},
], ids=lambda v: repr(v)[:28])
def test_malformed_tracking_never_fails_the_submission(client, bot, tracking):
    send, table = SENDERS[bot]
    r = send(client, tracking)
    assert r.status_code == 201, r.text
    assert _count(client, table) == 1
    saved = _saved(client, table, r.json()["id"])
    assert saved["gclid"] in (None, "") or isinstance(saved["gclid"], str)
    if isinstance(tracking, dict) and tracking.get("utm_campaign") == "fine":
        assert saved["utm_campaign"] == "fine" and saved["gclid"] is None   # valid keys beside a bad one are kept
    else:
        assert _all_null(saved)


@BOTS
def test_tracking_is_never_validated_as_an_answer(client, bot):
    """A bad answer is refused for its own reason; `tracking` (even a hostile one) adds no error and is not saved with the refusal."""
    send, table = SENDERS[bot]
    r = send(client, {**TRACKING, "gclid": {"nested": "object"}}, first_name="")
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert "first_name" in errors or any("first_name" in k for k in errors)
    assert "tracking" not in json.dumps(errors).lower() and "unexpected" not in json.dumps(errors).lower()
    assert _count(client, table) == 0


def test_the_other_unknown_top_level_keys_are_still_refused_by_homebot(client):
    """Taking `tracking` out first must not have loosened the strict HomeBot contract."""
    r = _hb(client, TRACKING, extra_field="x")
    assert r.status_code == 422 and "Unexpected fields: extra_field" in r.json()["errors"]["form"]
    assert _count(client, "homebot_enquiries") == 0


# ------------------------------------------------------------------------------------------- idempotent re-submits
@BOTS
def test_a_replay_with_other_tracking_is_the_same_submission_and_never_overwrites(client, bot):
    send, table = SENDERS[bot]
    first = send(client, TRACKING)
    assert first.status_code == 201
    eid = first.json()["id"]
    before = _saved(client, table, eid)
    for other in ({"gclid": "SOMETHING-ELSE", "utm_campaign": "other"}, {}, ABSENT, "junk"):
        again = send(client, other)
        assert again.status_code == 200 and again.json()["replayed"] is True and again.json()["id"] == eid, again.text
        assert _count(client, table) == 1
        assert _saved(client, table, eid) == before


@BOTS
def test_a_replay_cannot_add_tracking_to_an_enquiry_saved_without_it(client, bot):
    send, table = SENDERS[bot]
    first = send(client, ABSENT)
    again = send(client, TRACKING)
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert _all_null(_saved(client, table, first.json()["id"]))


@BOTS
def test_changed_answers_on_a_used_key_are_still_refused_whatever_the_tracking(client, bot):
    send, table = SENDERS[bot]
    first = send(client, TRACKING)
    changed = send(client, TRACKING, first_name="Someone")
    assert changed.status_code == 409 and changed.json()["already_submitted"] is True
    assert GCLID not in changed.text and _count(client, table) == 1
    assert _saved(client, table, first.json()["id"])["gclid"] == GCLID


# ------------------------------------------------------------------------------------------------- who can see it
@BOTS
def test_staff_see_the_fields_and_the_public_never_does(client, bot):
    send, table = SENDERS[bot]
    r = send(client, TRACKING)
    eid = r.json()["id"]
    path = f"/api/enquiries/{eid}" if bot == "matebot" else f"/api/homebot/enquiries/{eid}"
    staff = client.get(path)
    assert staff.status_code == 200
    seen = staff.json()
    assert {k: seen[k] for k in trk.KEYS} == TRACKING and datetime.fromisoformat(seen["tracking_captured_at"])
    assert client.anon.get(path).status_code == 401           # the record itself is staff only
    # no public answer carries it: the reply, the replay, a refused change, a saved draft
    public = [r, send(client, TRACKING), send(client, TRACKING, first_name="Someone")]
    if bot == "matebot":
        d = client.anon.post("/api/public/drafts", json={"answers": {**good_answers(), "tracking": TRACKING}, "tracking": TRACKING, "step": 1})
        assert d.status_code == 201
        public.append(d)
        assert GCLID not in client.db.execute("SELECT answers_json FROM form_drafts").fetchone()[0]
    for resp in public:
        assert GCLID not in resp.text and not any(k in resp.text for k in ("gclid", "gbraid", "wbraid", "utm_", "tracking_captured_at")), resp.text


@BOTS
def test_staff_records_without_tracking_show_null_fields(client, bot):
    send, _ = SENDERS[bot]
    eid = send(client, ABSENT).json()["id"]
    seen = client.get(f"/api/enquiries/{eid}" if bot == "matebot" else f"/api/homebot/enquiries/{eid}").json()
    assert all(seen[k] is None for k in trk.COLUMNS)


@BOTS
def test_the_click_id_is_stored_in_one_place_only(client, bot):
    """Not in the audit log, the answers, the receipt emails, the submission reply or any other table: only the enquiry's own column."""
    send, table = SENDERS[bot]
    send(client, TRACKING)
    send(client, TRACKING)   # the replay
    hits = []
    for t in dbmod.table_names(client.db):
        for col in dbmod.table_columns(client.db, t):
            n = client.db.execute(f"SELECT COUNT(*) FROM {t} WHERE CAST({col} AS TEXT) LIKE ?", (f"%{GCLID}%",)).fetchone()[0]
            if n:
                hits.append((t, col, n))
    assert hits == [(table, "gclid", 1)]


# ------------------------------------------------------------------------------- deletion, TEST clear, retention purge
def test_deleting_an_enquiry_removes_its_tracking_with_it(client):
    m = _mate(client, TRACKING).json()["id"]
    h = _hb(client, TRACKING).json()["id"]
    assert client.delete(f"/api/enquiries/{m}").status_code == 200
    assert client.delete(f"/api/homebot/enquiries/{h}").status_code == 200
    assert _count(client, "enquiries") == 0 and _count(client, "homebot_enquiries") == 0
    assert client.get(f"/api/enquiries/{m}").status_code == 404 and client.get(f"/api/homebot/enquiries/{h}").status_code == 404


def test_clearing_test_data_removes_enquiries_that_carry_tracking(client):
    from server import seed
    _mate(client, TRACKING)
    _hb(client, TRACKING)
    assert _count(client, "enquiries") == 1 and _count(client, "homebot_enquiries") == 1
    seed.delete_test_data(client.db)
    assert _count(client, "enquiries") == 0 and _count(client, "homebot_enquiries") == 0


def test_the_retention_purge_removes_enquiries_that_carry_tracking(client):
    from server import homebot
    from server.enquiries import purge_expired
    _mate(client, TRACKING)
    _hb(client, TRACKING)
    old = "2020-01-01T00:00:00+00:00"
    client.db.execute("UPDATE enquiries SET last_contact_at=?", (old,))
    client.db.execute("UPDATE homebot_enquiries SET last_contact_at=?", (old,))
    client.db.commit()
    assert purge_expired(client.db)["deleted"] == 1 and homebot.purge_expired(client.db) == 1
    assert _count(client, "enquiries") == 0 and _count(client, "homebot_enquiries") == 0


# ------------------------------------------------------------------------------------------------------ the columns
def test_the_columns_are_added_to_an_existing_database_once(db):
    """An older database (no tracking columns) gets them from the migration; running it again changes nothing and fails nowhere."""
    for table in ("enquiries", "homebot_enquiries"):
        for col in trk.COLUMNS:
            db.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    db.commit()
    assert not set(trk.COLUMNS) & dbmod.table_columns(db, "enquiries")
    assert not set(trk.COLUMNS) & dbmod.table_columns(db, "homebot_enquiries")
    dbmod.migrate(db)
    dbmod.migrate(db)
    for table in ("enquiries", "homebot_enquiries"):
        assert set(trk.COLUMNS) <= dbmod.table_columns(db, table)
    if not dbmod.is_postgres():   # nullable TEXT with no default, on both tables
        for table in ("enquiries", "homebot_enquiries"):
            info = {r[1]: (r[2], r[3], r[4]) for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
            assert all(info[c] == ("TEXT", 0, None) for c in trk.COLUMNS), table


def test_the_migration_list_names_exactly_the_columns_the_module_writes():
    declared = {(t, c): d for t, c, d in dbmod.MIGRATIONS if c in trk.COLUMNS}
    assert declared == {(t, c): "TEXT" for t in ("enquiries", "homebot_enquiries") for c in trk.COLUMNS}


# ------------------------------------------------------------------------------------------------------ the cleaning rules
def test_clean_is_total_and_keeps_only_the_six_keys():
    assert trk.KEYS == ("gclid", "gbraid", "wbraid", "utm_campaign", "utm_ad_group", "utm_keyword")
    for junk in (None, 0, "x", [], [1], (1,), {1, 2}, b"gclid", object()):
        assert trk.clean(junk) == {} and trk.columns(junk, "now") == {}
    assert trk.clean({"gclid": "A", "x": "y"}) == {"gclid": "A"}
    assert trk.columns({"gclid": "A"}, "2026-11-25T00:00:00+00:00") == {"gclid": "A", "tracking_captured_at": "2026-11-25T00:00:00+00:00"}
    assert trk.columns({"gclid": ""}, "now") == {}   # nothing saved, so no timestamp either


# ---------------------------------------------------------------------------------------- pages, scripts and the policy
def _directives(policy):
    out = {}
    for part in policy.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


GTM = "https://www.googletagmanager.com"
EXPECTED = {
    "script-src": ["'self'", GTM],
    "img-src": ["'self'", "data:", "blob:", GTM, "https://*.google-analytics.com", "https://*.g.doubleclick.net", "https://www.google.com", "https://www.google.com.au"],
    "connect-src": ["'self'", GTM, "https://*.google-analytics.com", "https://*.analytics.google.com", "https://*.g.doubleclick.net", "https://www.google.com"],
    "frame-src": ["'self'", GTM],
}
UNCHANGED = {"default-src": ["'self'"], "style-src": ["'self'", "'unsafe-inline'"], "font-src": ["'self'"], "form-action": ["'self'"],
             "base-uri": ["'self'"], "object-src": ["'none'"]}


def test_pages_get_a_policy_that_lets_them_load_google_tag_manager(ungated):
    c, _, sec = ungated
    for path in ("/", "/index.html", "/homebot/", "/tracking.js", "/gtm.js", "/terms"):
        policy = _directives(c.get(path, headers=HTML).headers["content-security-policy"])
        for name, sources in {**EXPECTED, **UNCHANGED}.items():
            assert policy[name] == sources, (path, name)
        assert policy["frame-ancestors"] == sec.FRAME_ANCESTORS.split(), path
        assert "'unsafe-inline'" not in policy["script-src"] and "'unsafe-eval'" not in policy["script-src"], path


def test_api_responses_keep_their_own_strict_policy(ungated):
    c, _, _ = ungated
    for path in ("/api/health", "/api/public/meta", "/api/public/homebot/meta"):
        r = c.get(path)
        assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'", path
        assert "googletagmanager" not in r.headers["content-security-policy"]
    posted = c.post("/api/public/enquiries", json={"tracking": TRACKING})   # even a refused request
    assert posted.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


def _meta_policy():
    html = (CLIENT / "index.html").read_text(encoding="utf-8")
    found = re.findall(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">', html)
    assert len(found) == 1
    return _directives(found[0])


def test_the_static_policy_matches_the_server_policy_and_keeps_the_preview_hash(ungated):
    c, _, _ = ungated
    server = _directives(c.get("/", headers=HTML).headers["content-security-policy"])
    meta = _meta_policy()
    hash_ = "'sha256-Qq/uoXbrEnjJ6L8KD8GicBffFCZgSQYA4VAEfHIXfGU='"
    assert hash_ in meta["script-src"] and hash_ not in server["script-src"]    # the preview helper's inline script: kept, meta only
    assert [s for s in meta["script-src"] if s != hash_] == server["script-src"]
    assert "frame-ancestors" not in meta                                         # a <meta> policy cannot carry it
    assert {k: v for k, v in meta.items()} == {**{k: v for k, v in server.items() if k != "frame-ancestors"}, "script-src": meta["script-src"]}
    # the HomeBot page has no <meta> policy: the server's header is the only one it gets
    assert "Content-Security-Policy" not in (HOMEBOT / "index.html").read_text(encoding="utf-8")


def _script_tags(html):
    return re.findall(r"<script\b([^>]*)>", html)


def test_both_pages_load_the_two_shared_scripts_first_and_use_no_inline_script(ungated):
    c, _, _ = ungated
    mate, home = c.get("/", headers=HTML).text, c.get("/homebot/", headers=HTML).text
    for page, srcs in ((mate, ["tracking.js", "gtm.js", "api.js", "form.js", "respond.js", "staff-exceptions.js", "staff.js", "app.js"]),
                       (home, ["/tracking.js", "/gtm.js", "form.js", "app.js"])):
        tags = _script_tags(page)
        assert all(re.fullmatch(r'\s*src="[^"]+"', t) for t in tags)                  # every script is external: no inline script
        names = [re.search(r'src="([^"?]+)\?v=([^"]+)"', t) for t in tags]
        assert [m[1] for m in names] == srcs and all(m[2] for m in names)             # each has a cache-busting ?v=
        assert "<noscript" not in page and "<iframe" not in page and "ns.html" not in page            # no noscript frame
        assert not re.search(r"<(?:script|link|img)\b[^>]*googletagmanager", page)                  # the container is loaded by gtm.js alone


def test_one_source_serves_both_pages(ungated):
    c, _, _ = ungated
    for name in ("tracking.js", "gtm.js"):
        served = c.get("/" + name)
        assert served.status_code == 200 and served.content == (CLIENT / name).read_bytes()
        assert not (HOMEBOT / name).exists()                                          # HomeBot reads the app-root copy, never its own
        assert c.get("/homebot/" + name).status_code == 404


def test_the_pilot_gate_covers_the_new_files_like_every_other_file(gated):
    c, _, _ = gated
    for name in ("/tracking.js", "/gtm.js"):
        assert c.get(name).status_code == 401
    assert _login(c).status_code == 200
    assert c.get("/tracking.js").status_code == 200 and c.get("/gtm.js").status_code == 200


# --------------------------------------------------------------------------------------------- tracking.js under node
TRACKING_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const NOW = 1790000000000, DAY = 86400000;
const run = sc => {
  const mk = init => { const m = new Map(Object.entries(init || {})); return { m, getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { if (sc.block) throw new Error('QuotaExceeded'); m.set(k, String(v)); }, removeItem: k => { m.delete(k); } }; };
  const session = mk(sc.session), local = mk(sc.local), win = {};
  const getter = s => () => { if (sc.block === 'throw') throw new Error('SecurityError'); return s; };
  Object.defineProperty(win, 'sessionStorage', { get: getter(session) });
  Object.defineProperty(win, 'localStorage', { get: getter(local) });
  const ctx = { window: win, location: { search: sc.search || '', hash: sc.hash || '' }, URLSearchParams, Date: { now: () => NOW } };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const rec = s => { const v = s.m.get('alm_gads_tracking'); if (v === undefined) return null; try { return JSON.parse(v); } catch (e) { return v; } };
  return { tracking: JSON.parse(JSON.stringify(win.ALM_TRACKING)), session: rec(session), local: rec(local), plain: vm.runInContext('Object.getPrototypeOf(window.ALM_TRACKING) === Object.prototype', ctx) };
};
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(sc => run(Object.assign({}, sc, {
  local: sc.local && Object.fromEntries(Object.entries(sc.local).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(Object.assign({}, v, { savedAt: NOW + (v.savedAtOffsetDays || 0) * DAY })) ])),
  session: sc.session && Object.fromEntries(Object.entries(sc.session).map(([k, v]) => [k, typeof v === 'string' ? v : JSON.stringify(Object.assign({}, v, { savedAt: NOW + (v.savedAtOffsetDays || 0) * DAY })) ])),
})))));
"""
NOW = 1790000000000


def _run_tracking(scenarios):
    out = subprocess.run([NODE, "-e", TRACKING_DRIVER, str(CLIENT / "tracking.js"), json.dumps(scenarios)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


def _stored(**values):
    """A kept record `days` old (negative days = in the past) for the driver, which turns savedAtOffsetDays into savedAt."""
    days = values.pop("days", 0)
    return {"alm_gads_tracking": {**values, "savedAtOffsetDays": -days}}


@needs_node
def test_tracking_js_captures_the_address_and_keeps_it_in_both_stores():
    q = "?source=alm-website&gclid=" + GCLID + "&gbraid=GB&wbraid=WB&utm_campaign=AU_Search%20Melbourne&utm_ad_group=ag&utm_keyword=cheap+flat&junk=1&utm_source=google"
    got, = _run_tracking([{"search": q}])
    want = {"gclid": GCLID, "gbraid": "GB", "wbraid": "WB", "utm_campaign": "AU_Search Melbourne", "utm_ad_group": "ag", "utm_keyword": "cheap flat"}
    assert got["tracking"] == want and got["plain"] is True                  # only the six keys; case kept; a plain object
    for store in ("session", "local"):
        assert got[store] == {"savedAt": NOW, **want}


@needs_node
def test_tracking_js_utm_gclid_counts_only_when_there_is_no_gclid():
    a, b, c, d = _run_tracking([{"search": "?utm_gclid=FromUtm"}, {"search": "?gclid=Real&utm_gclid=FromUtm"},
                                {"search": "?gclid=&utm_gclid=FromUtm"}, {"search": "?utm_gclid=" + "x" * 256}])
    assert a["tracking"] == {"gclid": "FromUtm"} and b["tracking"] == {"gclid": "Real"} and c["tracking"] == {"gclid": "FromUtm"}
    assert d["tracking"] == {}


@needs_node
def test_tracking_js_also_reads_a_query_inside_the_hash():
    a, b = _run_tracking([{"hash": "#/homebot/form?need=complete&gclid=InHash&utm_campaign=h"}, {"search": "?gclid=InSearch", "hash": "#/?gclid=InHash"}])
    assert a["tracking"] == {"gclid": "InHash", "utm_campaign": "h"} and b["tracking"] == {"gclid": "InSearch"}


@needs_node
def test_tracking_js_values_are_trimmed_cleaned_and_bounded():
    got, = _run_tracking([{"search": "?gclid=%00ab%0Acd%20&utm_campaign=%20%20x%20&utm_keyword=%20%09&gbraid=" + "y" * 255 + "&wbraid=" + "z" * 256}])
    assert got["tracking"] == {"gclid": "abcd", "utm_campaign": "x", "gbraid": "y" * 255}   # too long: left out, never shortened


@needs_node
def test_tracking_js_falls_back_to_what_was_kept_and_the_address_replaces_it():
    old = {"gclid": "OLD", "utm_campaign": "kept", "utm_keyword": "kw"}
    from_local, from_session, session_first, replaced, only_invalid = _run_tracking([
        {"local": _stored(**old, days=1)},
        {"session": _stored(**old, days=1)},
        {"session": _stored(gclid="S"), "local": _stored(**old)},
        {"search": "?utm_campaign=new", "session": _stored(**old), "local": _stored(**old)},
        {"search": "?gclid=" + "x" * 300, "local": _stored(**old)},
    ])
    assert from_local["tracking"] == from_session["tracking"] == old
    assert session_first["tracking"] == {"gclid": "S"}
    assert replaced["tracking"] == {"utm_campaign": "new"}                           # replaced, not merged: the old gclid is gone
    assert replaced["session"] == replaced["local"] == {"savedAt": NOW, "utm_campaign": "new"}
    assert only_invalid["tracking"] == old                                           # an address with nothing usable keeps what was kept
    # falling back does not rewrite the kept time (the 90 days run from the last ad click)
    assert from_local["local"]["savedAt"] == NOW - 86400000


@needs_node
def test_tracking_js_forgets_after_ninety_days():
    inside, edge, past, junk = _run_tracking([
        {"local": _stored(gclid="A", days=89)}, {"local": _stored(gclid="A", days=90)}, {"local": _stored(gclid="A", days=91)},
        {"local": {"alm_gads_tracking": "{not json"}, "session": {"alm_gads_tracking": "[]"}},
    ])
    assert inside["tracking"] == {"gclid": "A"} and edge["tracking"] == {"gclid": "A"}
    assert past["tracking"] == {} and past["local"] is None                          # expired: not used, and removed
    assert junk["tracking"] == {}
    nodate, = _run_tracking([{"local": {"alm_gads_tracking": json.dumps({"gclid": "A"})}}])
    assert nodate["tracking"] == {}                                                  # a record without its time is not trusted


@needs_node
def test_tracking_js_works_when_storage_is_blocked():
    blocked, throwing, nothing = _run_tracking([{"search": "?gclid=Z", "block": True}, {"search": "?gclid=Z", "block": "throw"}, {"block": "throw"}])
    assert blocked["tracking"] == {"gclid": "Z"} and throwing["tracking"] == {"gclid": "Z"} and nothing["tracking"] == {}


@needs_node
def test_tracking_js_leaves_an_empty_object_when_there_is_nothing():
    got, = _run_tracking([{"search": "?source=alm-website&utm_source=google"}])
    assert got["tracking"] == {} and got["plain"] is True and got["session"] is None and got["local"] is None


# ------------------------------------------------------------------------------------------------- gtm.js under node
GTM_DRIVER = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
console.log(JSON.stringify(JSON.parse(process.argv[2]).map(hash => {
  const inserted = [];
  const first = { parentNode: { insertBefore: (n, ref) => inserted.push({ n, before: ref === first }) } };
  const ctx = { window: {}, location: { hash }, Date: { now: () => 1234 },
    document: { createElement: t => ({ tag: t }), getElementsByTagName: () => [first], head: { appendChild: n => inserted.push({ n, head: true }) } } };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  return { hash, tags: inserted.map(i => ({ tag: i.n.tag, src: i.n.src, async: i.n.async })), dataLayer: ctx.window.dataLayer === undefined ? null : JSON.parse(JSON.stringify(ctx.window.dataLayer)) };
})));
"""
LOAD = {"tags": [{"tag": "script", "src": GTM + "/gtm.js?id=GTM-K729FCSB", "async": True}], "dataLayer": [{"gtm.start": 1234, "event": "gtm.js"}]}


def _run_gtm(hashes):
    out = subprocess.run([NODE, "-e", GTM_DRIVER, str(CLIENT / "gtm.js"), json.dumps(hashes)], capture_output=True, text=True, timeout=60, check=True)
    return {r["hash"]: {k: v for k, v in r.items() if k != "hash"} for r in json.loads(out.stdout)}


@needs_node
def test_gtm_js_loads_the_container_on_every_public_route():
    public = ["", "#/", "#/?test=1", "#/enquire?t=abc", "#/mate?t=abc", "#/resume?t=abc", "#/respond?t=abc", "#/homebot", "#/homebot/form?need=complete",
              "#/staffing", "#/x/staff", "#/Staff"]   # the staff test is app.js's own: the first part of the route, exactly "staff"
    for hash_, got in _run_gtm(public).items():
        assert got == LOAD, hash_


@needs_node
def test_gtm_js_never_loads_for_the_staff_workspace():
    for hash_, got in _run_gtm(["#/staff", "#/staff/", "#/staff/enquiries", "#/staff/enquiries/12", "#/staff?x=1", "#/staff/groups/3?t=1"]).items():
        assert got == {"tags": [], "dataLayer": None}, hash_       # nothing loaded, and not even a dataLayer created


@needs_node
def test_gtm_js_matches_how_app_js_reads_the_route():
    app_js = (CLIENT / "app.js").read_text(encoding="utf-8")
    gtm_js = (CLIENT / "gtm.js").read_text(encoding="utf-8")
    assert "const hash = location.hash || '#/';" in app_js and "const [path, query] = hash.slice(1).split('?');" in app_js
    assert "const parts = path.split('/').filter(Boolean);" in app_js and "if (parts[0] === 'staff') {" in app_js
    assert "(location.hash || '#/').slice(1).split('?')" in gtm_js and "route.split('/').filter(Boolean)[0] === 'staff'" in gtm_js


def test_gtm_js_is_external_and_names_the_container_once():
    text = (CLIENT / "gtm.js").read_text(encoding="utf-8")
    assert text.count("GTM-K729FCSB") == 1 and "https://www.googletagmanager.com/gtm.js?id=" in text
    assert "iframe" not in text.lower() and "ns.html" not in text and "document.write" not in text and "eval(" not in text


# ---------------------------------------------------------------- the forms: what is sent, and the completion event
def _snippet(source, pattern):
    m = re.search(pattern, source)
    assert m, pattern
    return m.group(0)


SNIPPET_DRIVER = r"""
const vm = require('vm');
const [snippet, scenarios] = [process.argv[1], JSON.parse(process.argv[2])];
console.log(JSON.stringify(scenarios.map(sc => {
  const ctx = { window: {}, staffMode: !!sc.staffMode, result: sc.result, body: sc.body, tracking: undefined };
  vm.createContext(ctx);
  if ('dataLayer' in sc) ctx.window.dataLayer = sc.dataLayer;
  if ('ALM_TRACKING' in sc) ctx.window.ALM_TRACKING = sc.ALM_TRACKING;
  if (sc.frozen) ctx.window.dataLayer = Object.freeze([]);
  if (sc.hostile) Object.defineProperty(ctx.window, 'dataLayer', { get() { throw new Error('blocked'); }, set() { throw new Error('blocked'); } });
  let error = null;
  try { vm.runInContext(sc.expression ? snippet + '\n;' + sc.expression : snippet, ctx); } catch (e) { error = String(e); }
  let layer = null; try { layer = ctx.window.dataLayer === undefined ? null : JSON.parse(JSON.stringify(ctx.window.dataLayer)); } catch (e) { layer = 'unreadable'; }
  return { error, dataLayer: layer,
           body: ctx.body === undefined ? null : JSON.parse(JSON.stringify(ctx.body)), returned: ctx.__out === undefined ? null : JSON.parse(JSON.stringify(ctx.__out)) };
})));
"""


def _run_snippet(snippet, scenarios):
    out = subprocess.run([NODE, "-e", SNIPPET_DRIVER, snippet, json.dumps(scenarios)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout)


EVENT_MATE = {"event": "alm_bot_complete", "helper": "matebot"}
EVENT_HOME = {"event": "alm_bot_complete", "helper": "homebot"}


@needs_node
def test_matebot_pushes_the_completion_event_for_a_saved_public_enquiry_only():
    form_js = (CLIENT / "form.js").read_text(encoding="utf-8")
    line = _snippet(form_js, r"if \(!staffMode && !\(result && result\.already_submitted\)\) \{ try \{.*\} \}\n").strip()
    saved, replayed, staff, already, existing, frozen, hostile = _run_snippet(line, [
        {"result": {"id": 7, "first_name": "Ann"}},
        {"result": {"id": 7, "replayed": True}},          # the reply to a retry: the first time this page sees the enquiry saved
        {"staffMode": True, "result": {"id": 7}},
        {"result": {"id": 7, "already_submitted": True}},
        {"result": {"id": 7}, "dataLayer": [{"event": "gtm.js"}]},
        {"result": {"id": 7}, "frozen": True},
        {"result": {"id": 7}, "hostile": True},
    ])
    assert saved["dataLayer"] == [EVENT_MATE] and replayed["dataLayer"] == [EVENT_MATE]
    assert staff["dataLayer"] is None and already["dataLayer"] is None                 # staff phone entry / nothing new saved: no event
    assert existing["dataLayer"] == [{"event": "gtm.js"}, EVENT_MATE]                  # appended to what Tag Manager already holds
    assert frozen["error"] is None and hostile["error"] is None                        # a broken dataLayer never touches the saved enquiry


def test_matebot_event_is_pushed_from_submit_once_and_carries_nothing_else():
    form_js = (CLIENT / "form.js").read_text(encoding="utf-8")
    assert form_js.count("alm_bot_complete") == 1 and form_js.count("dataLayer") == 2     # the push line: `window.dataLayer = window.dataLayer || []` and `.push`
    submit = form_js[form_js.index("    async function submit() {"):form_js.index("// ---------- I19(p): Email completed form")]
    assert submit.count("alm_bot_complete") == 1
    # after the server confirmed (savedResult set), before the confirmation is drawn; the confirmation itself can be drawn again
    # (a later "submit" while savedResult is set), so the event is not in showSaved / renderDone
    assert submit.index("savedResult = { answers, result };") < submit.index("alm_bot_complete") < submit.index("showSaved(answers, result);")
    assert submit.index("if (savedResult) { showSaved(savedResult.answers, savedResult.result); return; }") < submit.index("alm_bot_complete")
    show = form_js[form_js.index("    function renderDone("):form_js.index("    async function submit() {")]
    assert "alm_bot_complete" not in show and "dataLayer" not in show
    # nothing else goes in the event, and a TEST enquiry is not treated differently
    assert re.search(r"push\(\{ event: 'alm_bot_complete', helper: 'matebot' \}\)", form_js)


def test_matebot_staff_phone_entry_never_sends_tracking():
    staff_js = (CLIENT / "staff.js").read_text(encoding="utf-8")
    app_js = (CLIENT / "app.js").read_text(encoding="utf-8")
    assert "onSubmit: (answers, o) => api.post('/api/enquiries', { ...answers, channel: 'Phone' }, { headers: { 'X-Submission-Key': o.key } })," in staff_js
    assert "ALM_TRACKING" not in staff_js and "withTracking" not in staff_js          # staff.js only DISPLAYS saved tracking; it never reads the visitor's
    start = staff_js.index("views.new = async p =>")
    entry = staff_js[start:staff_js.index("\n  views.", start + 10)]                    # the phone-entry screen, up to the next view
    assert "api.post('/api/enquiries'" in entry and "tracking" not in entry.lower()
    assert app_js.count("withTracking(") == 1 and app_js.count("/api/public/enquiries") == 1   # one call, in the one public submit
    assert "withTracking(answers), { headers: { 'X-Submission-Key': o.key } }" in app_js
    assert "ALM_TRACKING" not in (CLIENT / "form.js").read_text(encoding="utf-8")         # the shared form component never reads it


@needs_node
def test_matebot_public_submit_adds_a_copy_of_the_tracking_only_when_there_is_some():
    app_js = (CLIENT / "app.js").read_text(encoding="utf-8")
    fn = _snippet(app_js, r"const withTracking = [^\n]+;")
    run = lambda sc: _run_snippet(fn, [{**sc, "expression": "this.__out = withTracking(body); this.__same = this.__out === body;"}])[0]
    body = {"first_name": "Ann"}
    for none in ({}, {"ALM_TRACKING": {}}, {"ALM_TRACKING": None}):
        got = run({**none, "body": body})
        assert got["returned"] == body and "tracking" not in got["returned"]
    got = run({"ALM_TRACKING": TRACKING, "body": body})
    assert got["returned"] == {**body, "tracking": TRACKING}
    assert got["body"] == body                                                       # the form's own answers are never changed


@needs_node
def test_homebot_sends_tracking_and_pushes_its_event_once_per_saved_enquiry():
    form_js = (HOMEBOT / "form.js").read_text(encoding="utf-8")
    send = _snippet(form_js, r"const tracking = window\.ALM_TRACKING; if \(tracking && Object\.keys\(tracking\)\.length\) body\.tracking = Object\.assign\(\{\}, tracking\);")
    none, empty, some = _run_snippet(send, [{"body": {"a": 1}}, {"body": {"a": 1}, "ALM_TRACKING": {}}, {"body": {"a": 1}, "ALM_TRACKING": TRACKING}])
    assert none["body"] == {"a": 1} and empty["body"] == {"a": 1} and some["body"] == {"a": 1, "tracking": TRACKING}
    push = _snippet(form_js, r"try \{ \(window\.dataLayer = window\.dataLayer \|\| \[\]\)\.push\(\{ event: 'alm_bot_complete', helper: 'homebot' \}\); \} catch \(e\) \{[^\n]*\}")
    first, again, frozen = _run_snippet(push, [{}, {"dataLayer": [{"event": "gtm.js"}]}, {"frozen": True}])
    assert first["dataLayer"] == [EVENT_HOME] and again["dataLayer"] == [{"event": "gtm.js"}, EVENT_HOME] and frozen["error"] is None
    # wiring: the body gets it just before the one request, the event is the first thing showDone does (it runs once per confirmed save,
    # review-build TEST enquiries included: nothing here looks at the TEST flag), and the payload() the server contract is checked against is untouched
    assert form_js.count("alm_bot_complete") == 1 and form_js.count("ALM_TRACKING") == 1
    assert form_js.index("const body = payload();") < form_js.index(send) < form_js.index("fetch(API.base + '/api/public/homebot/enquiries'")
    done = form_js[form_js.index("    function showDone(res, snap) {"):]
    assert done.index("alm_bot_complete") < done.index("form.hidden = true;") and form_js.count("showDone(") == 2   # its definition and the one call
    payload = form_js[form_js.index("    function payload() {"):form_js.index("    function showDone(")]
    assert "tracking" not in payload.lower() and not re.search(r"is_test|res\b|isTest", push)


@needs_node
def test_staff_detail_shows_the_fields_read_only_and_only_when_present():
    staff_js = (CLIENT / "staff.js").read_text(encoding="utf-8")
    helper = _snippet(staff_js, r"(?s)const AD_TRACKING = .*?\n  \};\n")
    driver = r"""
const vm = require('vm');
const ctx = { esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])), fmtDT: s => 'DT:' + s };
vm.createContext(ctx);
vm.runInContext(process.argv[1] + '\nthis.card = adTrackingCard;', ctx);
const t = JSON.parse(process.argv[2]);
console.log(JSON.stringify({ none: ctx.card({ gclid: null, tracking_captured_at: null, first_name: 'A' }), some: ctx.card(t, 'mt'), hostile: ctx.card({ gclid: '<img src=x onerror=alert(1)>' }) }));
"""
    out = json.loads(subprocess.run([NODE, "-e", driver, helper, json.dumps({**TRACKING, "tracking_captured_at": "2026-11-25T01:02:03+00:00"})], capture_output=True, text=True, timeout=60, check=True).stdout)
    assert out["none"] == ""
    for label in ("Google Ads tracking", "gclid", "gbraid", "wbraid", "Campaign", "Ad group", "Keyword", "Captured", GCLID, TRACKING["utm_keyword"], "DT:2026-11-25"):
        assert label in out["some"], label
    assert 'class="card card--tight mt"' in out["some"] and "<a " not in out["some"] and "<input" not in out["some"]   # plain text, not editable, no links
    assert "<img" not in out["hostile"] and "&lt;img" in out["hostile"]                                              # escaped like every other value
    # both detail pages use it
    assert "${adTrackingCard(e, 'mt')}" in staff_js and "${adTrackingCard(e)}" in staff_js and staff_js.count("adTrackingCard(") == 2
