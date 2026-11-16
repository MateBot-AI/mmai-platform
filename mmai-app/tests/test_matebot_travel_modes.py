"""The optional MateBot travel choices question (H31 / H28(c) / GP6(d), 16 November 2026): Walk, Bicycle, Public transport.

Optional like 4.7. Stored exactly as answered; no answer is NULL and nothing is ever inferred: an enquiry saved before the
question existed keeps no answer. The question wording is derived from the HomeBot form's (not Bill's own words); the three
choices are his. The answer is not yet used by any calculation: it is collected for the final travel check.
"""
import json
import re
from pathlib import Path

import pytest

from server import enquiries, homebot_travel, receipts
from server.db import row
from tests.test_data_and_api import client, good_answers  # noqa: F401  (fixture re-used)

CLIENT = Path(__file__).resolve().parents[1] / "client"


@pytest.fixture
def appdb(client):
    """The database the test client writes to."""
    import server.db as dbmod
    return dbmod.connect


def _saved(appdb, eid):
    return row(appdb().execute("SELECT travel_modes, travel, answers_json FROM enquiries WHERE id=?", (eid,)))


def _post(client, **over):
    return client.post("/api/enquiries", json={**good_answers(**over), "channel": "Phone"})


def test_the_three_choices_are_the_homebot_ones_and_public_transport_is_one_choice():
    assert enquiries.TRAVEL_MODES == ("Walk", "Bicycle", "Public transport") == homebot_travel.TRANSPORT[:3]
    html = (CLIENT / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'name="travel_modes" value="([^"]+)"', html) == list(enquiries.TRAVEL_MODES)
    assert 'How would you get to campus or work? <span class="hint-inline">(optional)</span>' in html
    block = html[html.index('name="travel_modes"') - 400:html.index('name="travel"')]
    assert "required" not in block and "checked" not in block                         # optional, and nothing pre-selected
    for gone in ("Tram", "Train", "Bus", "Car", "scooter"):
        assert gone not in block
    assert "'travel_modes'" in (CLIENT / "form.js").read_text(encoding="utf-8")       # collected as a list, like the other tick boxes


def test_the_value_is_the_answer_or_nothing_and_is_never_repaired():
    v = enquiries.travel_modes_value
    assert v(None) == v("") == v([]) == []
    assert v(["Public transport", "Walk"]) == ["Walk", "Public transport"]            # the form's order
    assert v(["Bicycle"]) == ["Bicycle"]
    for bad in ("Walk", ["Car"], ["Walk", "Walk"], ["walk"], ["Tram"], [1], {"Walk": True}, ["Walk", None], True, 3):
        assert v(bad) is None, bad


def test_an_answer_is_saved_as_given_and_no_answer_stays_empty(client, appdb):
    r = _post(client, travel_modes=["Public transport", "Walk"], travel="30 minutes")
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    s = _saved(appdb, eid)
    assert json.loads(s["travel_modes"]) == ["Walk", "Public transport"] and s["travel"] == "30 minutes"
    assert json.loads(s["answers_json"])["travel_modes"] == ["Public transport", "Walk"]   # the submitted payload, untouched
    assert client.get(f"/api/enquiries/{eid}").json()["travel_modes"] == ["Walk", "Public transport"]
    # left blank, or not sent at all: no answer. A travel time on its own implies no choice.
    for over in ({"travel_modes": [], "email": "b@example.com"}, {"email": "c@example.com", "travel": "5 minutes"}):
        r = _post(client, **over)
        assert r.status_code == 201, r.text
        s = _saved(appdb, r.json()["id"])
        assert s["travel_modes"] is None
        assert client.get(f"/api/enquiries/{r.json()['id']}").json()["travel_modes"] == []


@pytest.mark.parametrize("bad", ["Walk", ["Car"], ["Walk", "Walk"], ["Tram", "Walk"]])
def test_anything_but_the_three_choices_is_refused(client, appdb, bad):
    r = _post(client, travel_modes=bad)
    assert r.status_code in (400, 422) and "travel_modes" in r.text
    assert appdb().execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 0


def test_staff_can_record_or_clear_the_answer_and_nothing_else_moves(client, appdb):
    eid = _post(client).json()["id"]
    before = dict(row(appdb().execute("SELECT * FROM enquiries WHERE id=?", (eid,))))
    assert before["travel_modes"] is None
    assert client.patch(f"/api/enquiries/{eid}", json={"travel_modes": ["Bicycle", "Walk"]}).status_code == 200
    after = dict(row(appdb().execute("SELECT * FROM enquiries WHERE id=?", (eid,))))
    assert json.loads(after["travel_modes"]) == ["Walk", "Bicycle"]
    changed = {k for k in before if before[k] != after[k]}
    assert "travel_modes" in changed and changed <= {"travel_modes", "updated_at", "last_contact_at"}, changed
    assert client.patch(f"/api/enquiries/{eid}", json={"travel_modes": ["Car"]}).status_code in (400, 422)
    assert json.loads(_saved(appdb, eid)["travel_modes"]) == ["Walk", "Bicycle"]
    assert client.patch(f"/api/enquiries/{eid}", json={"travel_modes": []}).status_code == 200
    assert _saved(appdb, eid)["travel_modes"] is None


def test_an_enquiry_saved_before_the_question_has_no_answer_and_its_receipt_does_not_list_it(client, appdb):
    eid = _post(client, travel="15 minutes").json()["id"]                              # payload without the key = an older record
    e = dict(row(appdb().execute("SELECT * FROM enquiries WHERE id=?", (eid,))))
    assert "travel_modes" not in json.loads(e["answers_json"]) and e["travel_modes"] is None
    questions = [q for _, items in receipts.receipt_rows(e) for q, _ in items]
    assert "Maximum travel time to campus or work" in questions and "How would you get to campus or work?" not in questions
    # asked and left blank: listed as not provided; answered: listed as answered
    blank = dict(row(appdb().execute("SELECT * FROM enquiries WHERE id=?", (_post(client, travel_modes=[], email="b@example.com").json()["id"],))))
    assert ("How would you get to campus or work?", "Not provided") in [x for _, items in receipts.receipt_rows(blank) for x in items]
    full = dict(row(appdb().execute("SELECT * FROM enquiries WHERE id=?", (_post(client, travel_modes=["Walk", "Bicycle"], email="c@example.com").json()["id"],))))
    answer = dict(x for _, items in receipts.receipt_rows(full) for x in items)["How would you get to campus or work?"]
    assert "Walk" in answer and "Bicycle" in answer and "Public transport" not in answer


def test_the_answer_is_not_part_of_the_review_evidence_or_the_score_yet():
    """Collected only. It is not evidence of a review (adding it would make every saved review out of date) and no score reads
    it: the final travel check that will use it is not connected for MateBot yet."""
    import inspect
    from server import matching
    assert "travel_modes" not in matching.EVIDENCE_ENQUIRY_FIELDS
    assert "travel_modes" not in inspect.getsource(matching.preference_score)
