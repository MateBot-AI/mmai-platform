"""H5 / H14 — HomeBot → MateBot hand-off with reviewable, compatible prefill (server/homebot_handoff.py, 9 Nov 2026)."""
import json
import re
from pathlib import Path

import pytest

from server import homebot_handoff as hh
from tests.test_homebot import client  # noqa: F401  (API fixture: temp SQLite or the reserved PG test database)

ROOT = Path(__file__).resolve().parents[1]
HOMEBOT_APP = ROOT.parent / "homebot-app"


# ---------------------------------------------------------------------------------------------------------------- mapping
def test_one_person_now_is_alone_and_wanted_total_becomes_other_housemates():
    assert hh.prefill(1, 4) == {"party": "Alone", "mate_ideal": "3"}


def test_two_people_now_do_not_guess_couple_or_friends():
    """MateBot distinguishes a couple sharing one bed from two friends; HomeBot has not asked, so party stays blank (H5: no guessing)."""
    a = hh.prefill(2, 5)
    assert a == {"party_size": "2", "mate_ideal": "4"}
    assert "party" not in a


@pytest.mark.parametrize("now", [3, 4, 5, 6])
def test_three_or_more_now_are_friends_with_the_party_size(now):
    a = hh.prefill(now, 7)
    assert a["party"] == "With friends" and a["party_size"] == str(now) and a["mate_ideal"] == "6"


def test_mate_ideal_counts_the_other_people_as_matebot_does():
    """rules.check_group applies n - 1 <= mate_max: MateBot's housemate counts are the OTHER people. A wanted total of T is T - 1 others."""
    for now, target in ((1, 2), (2, 3), (3, 7)):
        assert hh.prefill(now, target)["mate_ideal"] == str(target - 1)


def test_mate_max_and_budget_are_never_prefilled():
    """HomeBot asked for the total wanted (not the most they would accept) and has not asked a budget; MateBot's budget is per person."""
    for now, target in ((1, 2), (2, 4), (5, 7)):
        a = hh.prefill(now, target)
        assert "mate_max" not in a and "budget_option" not in a and "budget_total" not in a


def test_no_identity_or_contact_key_is_ever_carried():
    for now, target in ((1, 2), (2, 3), (4, 6)):
        assert set(hh.prefill(now, target)) <= {"party", "party_size", "mate_ideal"}


@pytest.mark.parametrize("now,target,field", [(0, 3, "headcount_now"), (7, 8, "headcount_now"), (3, 3, "headcount_target"), (4, 2, "headcount_target"),
                                               (1, 8, "headcount_target"), ("x", 3, "headcount_now"), (2, None, "headcount_target")])
def test_invalid_counts_are_rejected_with_a_plain_sentence(now, target, field):
    with pytest.raises(hh.HandoffError) as ex:
        hh.prefill(now, target)
    assert field in ex.value.errors and ex.value.errors[field].endswith(".")


def test_carried_sentences_explain_now_total_and_additional_people_plainly():
    s = " ".join(hh.carried_sentences(3, 5))
    assert "3 of you" in s and "4 other housemates" in s and "the 2 more you want" in s
    assert "asked by MateBot" in s and "change the carried answers" in s
    assert not re.search(r"\b(GP|T|I|H)\d{1,2}\b", s) and "mate_ideal" not in s and "party_size" not in s   # T10: plain words only
    one = " ".join(hh.carried_sentences(1, 2))
    assert "on your own" in one and "1 other housemate living" in one


# ------------------------------------------------------------------------------------------------- travel choices (16 Nov 2026)
def test_travel_choices_already_ticked_are_carried_exactly_and_never_widened():
    base = {"party": "With friends", "party_size": "3", "mate_ideal": "4"}
    assert hh.prefill(3, 5) == base and hh.prefill(3, 5, None) == base and hh.prefill(3, 5, []) == base   # not answered: nothing carried
    assert hh.prefill(3, 5, ["Public transport", "Walk"]) == dict(base, travel_modes=["Walk", "Public transport"])
    assert hh.prefill(3, 5, ["Bicycle"])["travel_modes"] == ["Bicycle"]
    # an earlier answer is never read as one of today's choices, alone or beside one
    for old in (["Tram"], ["Train", "Bus"], ["Car"], ["Bike or scooter"], ["Tram", "Train", "Bus", "Car"], ["public transport"], ["Walking"]):
        assert "travel_modes" not in hh.prefill(3, 5, old), old
    assert hh.prefill(3, 5, ["Tram", "Walk", "Car"])["travel_modes"] == ["Walk"]
    # anything that is not a list of choices carries nothing (and is not an error: the question is asked by MateBot anyway)
    for odd in ("Walk", {"Walk": True}, 7, True, [["Walk"]], [None, 3, {"a": 1}]):
        assert "travel_modes" not in hh.prefill(3, 5, odd), odd
    assert hh.TRAVEL_CHOICES == ("Walk", "Bicycle", "Public transport")
    from server import homebot, matebot_travel
    assert list(hh.TRAVEL_CHOICES) == homebot.TRANSPORT == list(matebot_travel.MODES)
    # the longest-trip answer is never carried: MateBot's is each enquirer's own and optional
    assert "travel" not in hh.prefill(3, 5, ["Walk"]) and "max_travel" not in hh.prefill(3, 5, ["Walk"])


def test_the_carried_travel_choices_are_named_and_stay_open_to_change():
    assert len(hh.carried_sentences(3, 5)) == 3
    out = hh.carried_sentences(3, 5, ["Walk", "Public transport"])
    assert len(out) == 4 and out[2] == ("How you said you would get to campus or work: Walk, Public transport. "
                                        "MateBot asks this on its own screen, where you can change it.")


def test_handoff_route_carries_ticked_travel_choices_into_the_saved_form_for_review(client):  # noqa: F811
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 1, "headcount_target": 3, "transport": ["Public transport", "Tram", "Bicycle"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["answers"] == {"party": "Alone", "mate_ideal": "2", "travel_modes": ["Bicycle", "Public transport"]} and len(out["carried"]) == 4
    draft = client.anon.get("/api/public/drafts/" + out["token"]).json()
    assert draft["answers"] == out["answers"] and draft["step"] == 0 and draft["origin"] == "homebot"   # opened at screen one: reviewed before the summary
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 1, "headcount_target": 3, "transport": ["Car"]})
    assert r.status_code == 201 and "travel_modes" not in r.json()["answers"] and len(r.json()["carried"]) == 3


# ---------------------------------------------------------------------------------------------------------------- API
def test_handoff_route_saves_a_matebot_draft_that_resume_returns_from_screen_one(client):  # noqa: F811
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 3, "headcount_target": 5})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["answers"] == {"party": "With friends", "party_size": "3", "mate_ideal": "4"}
    assert out["resume"] == "#/resume?t=" + out["token"] and out["is_test"] is False and len(out["carried"]) == 3
    d = client.anon.get("/api/public/drafts/" + out["token"])
    assert d.status_code == 200
    draft = d.json()
    assert draft["answers"] == out["answers"] and draft["step"] == 0 and draft["origin"] == "homebot" and draft["is_test"] is False
    assert draft["email"] is None and draft["mate_token"] is None and draft["email_token"] is None and draft["submit_key"] is None
    row = client.db.execute("SELECT answers_json, origin, is_test FROM form_drafts WHERE token=?", (out["token"],)).fetchone()
    assert json.loads(row[0]) == out["answers"] and row[1] == "homebot" and row[2] == 0


def test_handoff_in_test_mode_marks_the_draft_test_and_keeps_test_in_the_link(client):  # noqa: F811
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 1, "headcount_target": 3, "is_test": True})
    assert r.status_code == 201
    out = r.json()
    assert out["resume"].endswith("&test=1") and out["is_test"] is True
    assert client.anon.get("/api/public/drafts/" + out["token"]).json()["is_test"] is True
    # staff test-data wipe removes it like any other test draft (T14)
    from server import seed
    seed.delete_test_data(client.db)
    client.db.commit()
    assert client.anon.get("/api/public/drafts/" + out["token"]).status_code == 404


def test_handoff_route_rejects_bad_counts_with_field_errors(client):  # noqa: F811
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 4, "headcount_target": 4})
    assert r.status_code == 422 and "headcount_target" in r.json()["errors"]
    r = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 2, "headcount_target": 9})
    assert r.status_code == 422 and "headcount_target" in r.json()["errors"]
    assert client.db.execute("SELECT COUNT(*) FROM form_drafts").fetchone()[0] == 0


def test_an_ordinary_draft_has_no_origin_and_the_old_banner_stays(client):  # noqa: F811
    r = client.anon.post("/api/public/drafts", json={"answers": {"first_name": "Mei"}, "step": 2})
    assert r.status_code == 201 and r.json()["origin"] is None


def test_handoff_draft_expires_with_retention_like_any_saved_form(client):  # noqa: F811
    out = client.anon.post("/api/public/homebot/handoff", json={"headcount_now": 2, "headcount_target": 4}).json()
    client.db.execute("UPDATE form_drafts SET expires_at='2000-01-01T00:00:00' WHERE token=?", (out["token"],)); client.db.commit()
    from server import enquiries
    enquiries.purge_expired(client.db, staff="Anna Chan")
    assert client.db.execute("SELECT COUNT(*) FROM form_drafts WHERE token=?", (out["token"],)).fetchone()[0] == 0


def test_homebot_enquiry_table_still_records_nothing_for_the_help_wanted_branch(client):  # noqa: F811
    """The hand-off does not create a HomeBot enquiry: the party continues as a MateBot enquiry (unchanged honest limit)."""
    from tests.test_homebot import body
    b = body(need_more="more", headcount_now=2, headcount_target=4, want_help="yes")
    b.pop("headcount", None)
    r = client.anon.post("/api/public/homebot/enquiries", json=b, headers={"X-Submission-Key": "k" * 32})
    assert r.status_code == 422 and "want_help" in r.json()["errors"]
    assert client.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0


# ---------------------------------------------------------------------------------------------------------------- client wiring
def test_homebot_handoff_screen_carries_and_falls_back_honestly():
    html = (HOMEBOT_APP / "index.html").read_text(encoding="utf-8")
    js = (HOMEBOT_APP / "form.js").read_text(encoding="utf-8")
    assert 'data-role="handoffCarried"' in html and 'data-role="handoffFallback"' in html
    assert "Nothing from this page could be carried across just now" in html   # the fallback is only shown when the server could not be reached
    assert "/api/public/homebot/handoff" in js and "#/resume?t=" in js and "prepareHandoff(scr)" in js
    assert "show('fallback')" in js   # a failed request never implies a transfer (H2)
    # Each changed script gets its own cache version, including self-hosted routing.
    assert 'src="form.js?v=20261116-handoff-travel"' in html
    # travel choices already ticked go across exactly as ticked, and only when there are any
    assert "const travel = [].concat(value('transport') || []);" in js and "travel.length ? { transport: travel } : {}" in js
    assert 'src="app.js?v=20261111-selfhost"' in html


def test_matebot_form_words_the_handoff_banner_and_cache_tag_is_bumped():
    js = (ROOT / "client" / "form.js").read_text(encoding="utf-8")
    html = (ROOT / "client" / "index.html").read_text(encoding="utf-8")
    assert "cfg.draft.origin === 'homebot'" in js and "You have come over from HomeBot" in js
    assert "Welcome back. Your saved answers are here" in js   # ordinary resumed drafts unchanged
    assert 'src="form.js?v=20261123a"' in html
