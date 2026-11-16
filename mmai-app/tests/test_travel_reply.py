"""The AI's travel reply for a live travel check (H28(i), 16 Nov 2026).

Synthetic journey replies and a stand-in for the model; no network. Covers: off by default and only on ALM's own AI account
with the approved model; what the model is given (figures and positions only); every count, every person the model names
and the group result is checked against the code's own figures, with "not known" neither within nor over a limit; a wrong
reply is withheld whole; no sentence written by the model is ever shown (the note is written by code from the checked
fields); the reply is returned once and nothing of it (or of the journeys) is stored or logged; the saved AI review is
unchanged; and the page shows the reply with its Google Maps sources straight after it.
"""
import json
import logging
import os
import re

import pytest

from server import ai, homebot_review as hr, homebot_shortlist as hs, homebot_travel as tv, homebot_travel_reply as tr, routing
from tests.test_homebot_shortlist import CUR, client, current, inventory, on  # noqa: F401  (fixtures + helpers)
from tests.test_live_travel import NOW, SOURCE, Provider, _party, audits, everything, live, snapshot  # noqa: F401


def _ai_env(monkeypatch):
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR", "MMAI_AI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key-not-real")
    monkeypatch.setenv("MMAI_AI_MODE", "own-key")
    monkeypatch.setenv(tr.REPLY_ENV, "1")
    monkeypatch.setattr(ai, "_client", lambda: (_ for _ in ()).throw(AssertionError("real model call in a test")))


def _packet_from(prompt):
    return json.loads(prompt.split("GOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps;", 1)[1].split(")\n\n", 1)[1])


def _counted(o):
    """The option as the model sees it, with the counts a careful reader would make."""
    who = lambda kind: [p["person"] for p in o["people"] if p.get("own_limit") == kind]   # noqa: E731
    return dict(o, counts={"within_own_limit": len(who("within")), "over_own_limit": len(who("over")), "not_known": len(who("not_known"))})


def cautious(o):
    """A plain, valid judgement for a stand-in model: the first rating and advice that fit, and only what must be flagged."""
    f = _counted(o)
    weak = tr._must_flag(f)
    return {"travel_rating": tr._ratings_allowed(f)[0], "strengths": [], "weaknesses": weak, "advice": tr._advice_allowed(f, weak)[0]}


def _order(options):
    return [o["apartment_id"] for o in sorted(options, key=lambda o: (o.get("apartment") or {}).get("kind", tr.FULL) != tr.FULL)]


def faithful(options):
    out = []
    for o in options:
        who = lambda kind: [p["person"] for p in o["people"] if p.get("own_limit") == kind]   # noqa: E731
        out.append({"apartment_id": o["apartment_id"], "within_own_limit": len(who("within")), "over_own_limit": len(who("over")),
                    "not_known": len(who("not_known")), "people_over_own_limit": who("over"), "people_not_known": who("not_known"),
                    "group_result": o["group"]["result"], **cautious(o)})
    return {"options": out, "preferred_order": _order(options)}


class Model:
    """Stand-in for ai._ask: records every prompt and answers with make(options given)."""

    def __init__(self, make=faithful):
        self.make, self.prompts = make, []

    def __call__(self, prompt, model=None, max_tokens=1500, system=""):
        self.prompts.append(prompt)
        out = self.make(_packet_from(prompt)["options"])
        return out if isinstance(out, str) else json.dumps(out)


@pytest.fixture
def grounded(live, monkeypatch):
    _ai_env(monkeypatch)
    model = Model()
    monkeypatch.setattr(ai, "_ask", model)
    c, eid = live
    return c, eid, model


def figures(limit=30, minutes=("12", "22.3", "25", "28", "30", "45.5"), status=tv.PASS, extra=()):
    """One option's travel assessment as the unchanged travel maths reports it."""
    people = [{"index": i + 1, "status": tv.MEASURED, "fastest_minutes": m, "fastest_modes": ["Public transport"]} for i, m in enumerate(minutes)]
    people += list(extra)
    return {"apartment_id": 7, "travel": {"status": status, "facts": {
        "stated_limit_minutes": limit, "ceiling_minutes": 2 * limit, "people": people, "meets_stated_limit": True,
        "majority": {"size": 4, "average_display": "21.8"} if status in (tv.PASS, tv.EXCEEDS_CEILING) else None}}}


# ------------------------------------------------------------------------------------------------------------ switches
def test_off_by_default_and_only_on_alms_own_account_with_the_approved_model(live, monkeypatch):
    c, eid = live
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called while off")))
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR", "MMAI_AI_MODEL", "MMAI_AI_MODE", tr.REPLY_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key-not-real")
    assert ai.using_own_key() and not tr.enabled()                           # the account alone does not switch it on
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert out["ok"] and out["ai_reply"] == {"state": "off", "saved": False, "plain": tr.OFF_TEXT, "options": []}
    assert c.get(f"/api/homebot/enquiries/{eid}/options").json()["live_travel"]["ai_reply"] is False
    for value in ("0", "true", "yes", "on", " 1", ""):
        monkeypatch.setenv(tr.REPLY_ENV, value)
        assert not tr.switch_on() and not tr.enabled(), value
    monkeypatch.setenv(tr.REPLY_ENV, "1")
    assert tr.enabled() and c.get(f"/api/homebot/enquiries/{eid}/options").json()["live_travel"]["ai_reply"] is True
    # never through the development proxy, the publish proxy or without a key: journey content goes to ALM's own account only
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.invalid")
    assert not tr.enabled()
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert out["ai_reply"] == {"state": "off", "saved": False, "plain": tr.NOT_OWN_ACCOUNT_TEXT, "options": []}
    monkeypatch.delenv("ANTHROPIC_BASE_URL")
    monkeypatch.setenv("MMAI_AI_URL_VAR", "X_URL")
    monkeypatch.setenv("MMAI_AI_TOKEN_VAR", "X_TOK")
    monkeypatch.setenv("X_URL", "https://publish.example.invalid")
    monkeypatch.setenv("X_TOK", "t")
    assert ai.using_user_key() and not tr.enabled()
    for var in ("MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR"):
        monkeypatch.delenv(var)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not tr.enabled()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key-not-real")
    monkeypatch.setenv("MMAI_AI_MODEL", "some-other-model")                   # an override cannot change the model on a real key
    assert tr.enabled() and ai.model_name() == ai.APPROVED_MODEL == "claude-sonnet-5-5"
    # and never when live travel checks themselves are off
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV)
    assert not tr.enabled()
    assert tr.REPLY_ENV == "MMAI_TRAVEL_AI_REPLY"


# -------------------------------------------------------------------------------------------------- the whole check
def test_reply_is_returned_once_and_nothing_is_stored(grounded, caplog):
    c, eid, model = grounded
    before, packet_before = snapshot(c), json.dumps(hr.packet(c.db, current(c, eid)), sort_keys=True, default=str)
    stored_before = current(c, eid)["result_json"]
    with caplog.at_level(logging.DEBUG):
        out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    reply, shown = out["ai_reply"], everything(out)
    assert reply["state"] == "ready" and reply["saved"] is False and reply["model"] == "claude-sonnet-5-5" and reply["plain"] == tr.READY_TEXT
    assert len(model.prompts) == 1                                           # one model call for the whole check
    assert [o["apartment_id"] for o in reply["options"]] == [x["apartment_id"] for x in out["listed"] + out["alternatives"]]
    for o in reply["options"]:
        # three people in the party: each journey by public transport is 10 minutes, inside the 30-minute limit
        assert (o["within_own_limit"], o["over_own_limit"], o["not_known"], o["no_destination"]) == (3, 0, 0, 0)
        assert o["group_result"] == tr.WITHIN and o["group_plain"] == "within the doubled limit"
        assert (o["stated_limit_minutes"], o["doubled_limit_minutes"]) == (30, 60)
        assert o["note"] == "No one is over their own limit and no journey is unknown."
    # each option still carries its sources, to sit straight after the note
    assert all(x["travel_sources"]["links"] == [SOURCE] for x in shown)
    # nothing was written anywhere but the one count-only audit record, and the saved AI review is as it was
    assert snapshot(c) == before and current(c, eid)["result_json"] == stored_before
    assert json.dumps(hr.packet(c.db, current(c, eid)), sort_keys=True, default=str) == packet_before
    (a,) = audits(c)
    assert json.loads(a["detail"]) == {"scenario": CUR, "shortlist_id": out["shortlist_id"], "requests": 16, "not_requested": 0}
    everywhere = re.sub(r"[0-9a-f]{16,}", "", json.dumps(c.db.execute("SELECT * FROM audit_log").fetchall(), default=str) + json.dumps(snapshot(c)))
    for leaked in ("over their own limit", "10.0", "synthetic", "homebot-travel-reply", "people_not_known"):
        assert leaked not in everywhere and leaked not in caplog.text, leaked
    assert c.db.execute("SELECT COUNT(*) AS n FROM homebot_reviews").fetchone()["n"] == json.loads(before["homebot_reviews"]).__len__()


def test_what_the_model_is_given(grounded):
    c, eid, model = grounded
    hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    (prompt,) = model.prompts
    assert "GOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps;" in prompt   # journey content sits in its own labelled section
    given = _packet_from(prompt)
    assert set(given) == {"options"} and given["options"]
    for o in given["options"]:
        assert set(o) == {"apartment_id", "stated_limit_minutes", "doubled_limit_minutes", "people", "group", "long_journey", "apartment"}   # the model counts; code checks
        assert set(o["apartment"]) == {"kind", "total_weekly_rent", "days_from_preferred_dates", "future_empty_days", "open_points", "notes",
                                       "took_the_place_of_one_that_failed_travel"}
        for person in o["people"]:
            assert set(person) <= {"person", "journey", "minutes", "by", "own_limit"}
            assert person["by"] == ["Public transport"] and person["minutes"] == "10" and person["own_limit"] == "within"
    e = c.get(f"/api/homebot/enquiries/{eid}").json()
    for private in [e["first_name"], e["last_name"], e["email"]] + [p.get("place") or "" for p in e["people"]] + ["Street", "Lane", "Unit", "@"]:
        assert not private or private not in prompt, private


def test_ai_failure_or_mismatch_leaves_the_journey_figures_and_withholds_the_reply(grounded, monkeypatch, caplog):
    c, eid, model = grounded

    def one_wrong(options):
        out = faithful(options)
        out["options"][0]["within_own_limit"] += 1
        return out
    monkeypatch.setattr(ai, "_ask", Model(one_wrong))
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert out["ok"] and out["ai_reply"] == {"state": "withheld", "saved": False, "plain": tr.WITHHELD_TEXT, "options": []}
    assert all(x["travel"]["status"] == tv.PASS for x in everything(out))    # the code's own result is unaffected

    def down(*a, **k):
        raise ai.AIUnavailable("provider said: 12 Secret Street", kind="provider", code="provider")
    monkeypatch.setattr(ai, "_ask", down)
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert out["ok"] and out["ai_reply"] == {"state": "unavailable", "saved": False, "plain": tr.UNAVAILABLE_TEXT, "options": []}
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(ValueError("12 Secret Street")))
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())["ai_reply"]["state"] == "unavailable"
    assert "Secret Street" not in caplog.text
    # nothing measured: the model is not asked at all
    asked = Model()
    monkeypatch.setattr(ai, "_ask", asked)
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider(fail=True))
    assert out["ai_reply"] == {"state": "nothing", "saved": False, "plain": tr.NOTHING_TEXT, "options": []} and asked.prompts == []


def test_an_earlier_answer_is_never_given_to_the_model_as_a_journey(on, monkeypatch):
    eid = _party(on, monkeypatch, ["Walk", "Tram"])
    _ai_env(monkeypatch)
    model = Model()
    monkeypatch.setattr(ai, "_ask", model)
    # walking is measured (two minutes), but the tram answer keeps every person "not known": the walking time alone is not
    # their fastest accepted journey, so there is nothing the model may assess and it is not asked
    out = hs.live_travel_check(on.db, eid, CUR, "Anna", now=NOW, post=Provider(walk="120s"))
    assert out["ok"] and out["check"]["measured"] > 0 and all(x["travel"]["status"] == tv.NEEDS_CONFIRMATION for x in everything(out))
    assert out["ai_reply"] == {"state": "nothing", "saved": False, "plain": tr.NOTHING_TEXT, "options": []} and model.prompts == []
    for x in everything(out):
        f = tr.option_figures(x)
        assert f["counts"]["within_own_limit"] == f["counts"]["over_own_limit"] == 0 and f["counts"]["not_known"] == 3
        assert all(p == {"person": p["person"], "journey": "not_known", "own_limit": "not_known"} for p in f["people"])


def test_a_person_whose_journey_failed_is_counted_as_not_known(grounded, monkeypatch):
    c, eid, model = grounded

    class Patchy(Provider):
        """Every request to one person's destination fails; the others answer."""
        def __call__(self, body):
            self.first = getattr(self, "first", body["destination"])
            if body["destination"] == self.first:
                self.bodies.append(body)
                raise RuntimeError("no route")
            return super().__call__(body)
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Patchy())
    reply = out["ai_reply"]
    assert reply["state"] == "ready" and len(model.prompts) == 1
    by_id = {x["apartment_id"]: x for x in everything(out)}
    for o in reply["options"]:
        assert o["not_known"] >= 1 and o["over_own_limit"] == 0 and o["within_own_limit"] + o["not_known"] == 3
        assert o["within_own_limit"] == len(by_id[o["apartment_id"]]["travel"]["facts"]["measured"])
        assert o["group_result"] == tr.GROUP_RESULT[by_id[o["apartment_id"]]["travel"]["status"]]
        assert o["note"].startswith("Not known, so neither within nor over a limit: person ") and "Over their own limit" not in o["note"]
    given = _packet_from(model.prompts[0])
    unknown = [p for o in given["options"] for p in o["people"] if p["journey"] == "not_known"]
    assert unknown and all(set(p) == {"person", "journey", "own_limit"} and p["own_limit"] == "not_known" for p in unknown)

    # a model that counts the unknown person as within the limit (or as over it) is refused
    def optimistic(options):
        out = faithful(options)
        for o in out["options"]:
            o["within_own_limit"], o["not_known"], o["people_not_known"] = o["within_own_limit"] + o["not_known"], 0, []
        return out
    monkeypatch.setattr(ai, "_ask", Model(optimistic))
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Patchy())["ai_reply"]["state"] == "withheld"

    def pessimistic(options):
        out = faithful(options)
        for o in out["options"]:
            o["over_own_limit"], o["not_known"], o["people_over_own_limit"], o["people_not_known"] = o["not_known"], 0, o["people_not_known"], []
        return out
    monkeypatch.setattr(ai, "_ask", Model(pessimistic))
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Patchy())["ai_reply"]["state"] == "withheld"


# ------------------------------------------------------------------------------------------------- the code's figures
def test_own_limit_and_group_result_are_separate_counts():
    """Five people within their own 30-minute limit, one over it; the group result is a different question."""
    f = tr.option_figures(figures())
    assert f["counts"] == {"within_own_limit": 5, "over_own_limit": 1, "not_known": 0, "no_destination": 0}
    assert f["group"]["result"] == tr.WITHIN
    assert [p["own_limit"] for p in f["people"]] == ["within"] * 5 + ["over"]          # exactly at the limit is within
    # most people over their own limit, yet the group result (doubled limit) is still within: both are reported as they are
    f = tr.option_figures(figures(minutes=("10", "40", "45", "50", "55", "59")))
    assert f["counts"]["within_own_limit"] == 1 and f["counts"]["over_own_limit"] == 5 and f["group"]["result"] == tr.WITHIN
    # the comparison is on the exact value, not the rounded one: 30 minutes and one second is over a 30-minute limit
    f = tr.option_figures(figures(minutes=("1801/60", "30.0000001", "29.99")))
    assert [p["own_limit"] for p in f["people"]] == ["over", "over", "within"] and [p["minutes"] for p in f["people"]] == ["30", "30", "30"]


def test_not_known_is_neither_within_nor_over():
    extra = [{"index": 7, "status": tv.PARTIAL, "fastest_minutes": None, "bound_minutes": "3", "best_known_modes": ["Walk"], "unknown_modes": ["Tram"]},
             {"index": 8, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}, {"index": 9, "status": tv.NO_DEST_PERSON, "fastest_minutes": None}]
    f = tr.option_figures(figures(status=tv.NEEDS_CONFIRMATION, extra=extra))
    assert f["counts"] == {"within_own_limit": 5, "over_own_limit": 1, "not_known": 2, "no_destination": 1}
    assert f["group"]["result"] == tr.NEEDS and f["group"]["closest_majority_size"] is None
    # a best-known time for a partly measured person is never passed on, however short
    assert f["people"][6] == {"person": 7, "journey": "not_known", "own_limit": "not_known"}
    assert f["people"][8] == {"person": 9, "journey": "no_destination"}
    assert tr.option_figures({"apartment_id": 1, "travel": None}) is None
    assert len(tr.packet([{**figures(), "apartment_id": i} for i in range(40)])["options"]) == tr.MAX_OPTIONS
    assert len(tr.packet([figures(), figures()])["options"]) == 1


# ------------------------------------------------------------------------------------------------------------ validator
def _reply(pk, order=None, **over):
    row = {"apartment_id": 7, "within_own_limit": 5, "over_own_limit": 1, "not_known": 0, "people_over_own_limit": [6],
           "people_not_known": [], "group_result": tr.WITHIN, **cautious(pk["options"][0])}
    row.update(over)
    return json.dumps({"options": [{k: v for k, v in row.items() if v is not ...}], "preferred_order": [7] if order is None else order})


def test_a_faithful_reply_is_accepted_and_the_sentence_is_written_by_code():
    pk = tr.packet([figures()])
    (o,) = tr.parse_reply(_reply(pk), pk)
    assert o == {"apartment_id": 7, "within_own_limit": 5, "over_own_limit": 1, "not_known": 0, "no_destination": 0,
                 "stated_limit_minutes": 30, "doubled_limit_minutes": 60, "group_result": tr.WITHIN,
                 "group_plain": "within the doubled limit", "note": "Over their own limit: person 6.",
                 "travel_rating": "good", "travel_rating_plain": "good", "strengths": [], "weaknesses": [], "strength_codes": [],
                 "weakness_codes": [], "advice": "offer", "advice_plain": "Suitable to offer.", "preference": 1, "of": 1}
    # five within their own limit and one over it, while the group result is a separate question
    extra = [{"index": 7, "status": tv.PARTIAL, "fastest_minutes": None, "unknown_modes": ["Tram"]},
             {"index": 8, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]
    pk = tr.packet([figures(status=tv.NEEDS_CONFIRMATION, extra=extra)])
    (o,) = tr.parse_reply(_reply(pk, not_known=2, people_not_known=[8, 7], group_result=tr.NEEDS), pk)   # any order, same people
    assert o["note"] == "Over their own limit: person 6. Not known, so neither within nor over a limit: person 7, person 8."
    assert (o["within_own_limit"], o["over_own_limit"], o["not_known"], o["group_plain"]) == (5, 1, 2, "needs confirmation")


@pytest.mark.parametrize("over", [
    {"within_own_limit": 6}, {"within_own_limit": 4, "over_own_limit": 2}, {"over_own_limit": 0}, {"not_known": 1},
    {"within_own_limit": True}, {"within_own_limit": "5"}, {"within_own_limit": 5.0},
    {"group_result": tr.OVER}, {"group_result": tr.NEEDS}, {"group_result": "pass"}, {"group_result": None},
    {"apartment_id": 8}, {"apartment_id": "7"},
    {"people_over_own_limit": []}, {"people_over_own_limit": [5]}, {"people_over_own_limit": [5, 6]},   # the wrong people
    {"people_over_own_limit": [6, 6]}, {"people_over_own_limit": ["6"]}, {"people_over_own_limit": [6.0]},
    {"people_over_own_limit": [True]}, {"people_over_own_limit": 6}, {"people_over_own_limit": None},
    {"people_not_known": [4]}, {"people_not_known": [6]}, {"people_not_known": None},
    {"people_over_own_limit": ...}, {"people_not_known": ...}, {"group_result": ...},                    # a field left out
    # no sentence from the model is accepted at all, however reasonable it reads
    {"note": "5 people are within their own 30-minute limit and 1 person is over it."},
    {"note": "Person 4 is within their limit."}, {"note": "The group is OK on travel."}, {"note": ""},
    {"comment": "Travel is acceptable."}, {"summary": "Within: 3 people"}, {"verdict": "pass"},
])
def test_a_reply_that_does_not_match_the_figures_is_refused_whole(over):
    pk = tr.packet([figures()])
    with pytest.raises(tr.ReplyMismatch):
        tr.parse_reply(_reply(pk, **over), pk)


def test_an_unknown_person_cannot_be_reported_as_within_or_over():
    extra = [{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]
    pk = tr.packet([figures(status=tv.NEEDS_CONFIRMATION, extra=extra)])
    good = {"not_known": 1, "people_not_known": [7], "group_result": tr.NEEDS}
    assert tr.parse_reply(_reply(pk, **good), pk)
    for wrong in ({**good, "within_own_limit": 6, "not_known": 0, "people_not_known": []},        # counted as within
                  {**good, "over_own_limit": 2, "not_known": 0, "people_over_own_limit": [6, 7], "people_not_known": []},   # counted as over
                  {**good, "people_over_own_limit": [6, 7]},                                       # named as over while still counted unknown
                  {**good, "people_not_known": []}, {**good, "people_not_known": [6]},
                  {**good, "group_result": tr.WITHIN}, {**good, "group_result": tr.OVER}):         # the group result talked up or down
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, **wrong), pk)
    pk = tr.packet([figures(status=tv.EXCEEDS_CEILING)])
    assert tr.parse_reply(_reply(pk, group_result=tr.OVER), pk)[0]["group_plain"] == "over the doubled limit"
    with pytest.raises(tr.ReplyMismatch):
        tr.parse_reply(_reply(pk), pk)


def test_reply_shape_is_exact():
    pk = tr.packet([figures(), {**figures(), "apartment_id": 9}])
    one = json.loads(_reply(pk))["options"][0]
    good = {"options": [one, {**one, "apartment_id": 9}], "preferred_order": [9, 7]}
    assert [o["preference"] for o in tr.parse_reply(json.dumps(good), pk)] == [2, 1]
    both = lambda options, **more: json.dumps({"options": options, "preferred_order": [7, 9], **more})   # noqa: E731
    for bad in ("not json at all", "[]", both([one]), both([one, one]), json.dumps({"options": good["options"]}),
                both([{**one, "apartment_id": 9}, one]), both(good["options"], extra=1),
                both([one, {**one, "apartment_id": 9, "verdict": "pass"}]),
                both([one, {k: v for k, v in one.items() if k != "people_not_known"}]),
                both([one, {k: v for k, v in one.items() if k != "advice"}]),
                both([one, "9"]), both(None),
                json.dumps({**good, "preferred_order": [7]}), json.dumps({**good, "preferred_order": [7, 7]}),
                json.dumps({**good, "preferred_order": [7, 9, 9]}), json.dumps({**good, "preferred_order": [7, 8]}),
                json.dumps({**good, "preferred_order": ["7", "9"]}), json.dumps({**good, "preferred_order": None})):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(bad, pk)


def test_the_model_is_not_asked_for_a_sentence_and_none_of_its_words_can_reach_the_page():
    pk = tr.packet([figures()])
    prompt = tr.build_prompt(pk)
    assert '"note"' not in prompt and "no other fields" in prompt
    assert set(tr.REPLY_KEYS) == {"apartment_id", "within_own_limit", "over_own_limit", "not_known", "people_over_own_limit", "people_not_known",
                                  "group_result", "travel_rating", "strengths", "weaknesses", "advice"}
    assert "Do not write sentences of your own anywhere" in prompt
    # a sentence where a code belongs is refused; the page only ever carries the server's own wording for a chosen code
    for wordy in ({"strengths": ["Everyone is close to work."]}, {"weaknesses": ["A bit far for person 6."]}, {"advice": "Offer it today."},
                  {"travel_rating": "good, with one long journey"}):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, **wordy), pk)
    # every value shown for an option comes from the code's figures: change every model-supplied field's type and nothing passes
    (o,) = tr.parse_reply(_reply(pk), pk)
    f = pk["options"][0]
    assert o["note"] == tr._note(f) and all(o[k] == f["counts"][k] for k in f["counts"]) and o["group_result"] == f["group"]["result"]
    for text in (tr._note(f), tr._note(tr.packet([figures(minutes=("10", "12"))])["options"][0])):
        assert not re.search(r"\d+(?:\.\d+)? minutes", text)             # the sentence carries no journey time


# ------------------------------------------------------------------------------------------------------- the judgement
def option(aid=7, klass=None, rent="900.00", distance=0, empty=0, flags=(), reasons=(), **kw):
    """One shortlisted apartment as the travel check returns it: its journeys and its other points."""
    from server import homebot_match as hm
    return {**figures(**kw), "apartment_id": aid, "class": klass or hm.SUITABLE, "flags": list(flags), "confirmation_reasons": list(reasons),
            "offer": {"total_weekly_rent": rent, "date_distance_days": distance, "future_empty_days": empty}}


def _judged(pk, judgements, order=None):
    rows_ = [{k: v for k, v in faithful([{k: v for k, v in o.items() if k != "counts"}])["options"][0].items()} for o in pk["options"]]
    for row, j in zip(rows_, judgements):
        row.update(j)
    return json.dumps({"options": rows_, "preferred_order": order or [o["apartment_id"] for o in pk["options"]]})


def test_the_judgement_is_the_models_own_and_it_is_what_the_page_shows():
    """Same journeys, same apartments: two different assessments are both accepted, and each is shown as given. The code
    does not choose the rating, the points, the advice or the order; it only refuses what is untrue."""
    pk = tr.packet([option(7, rent="900.00"), option(9, rent="950.00", distance=3)])
    one = tr.parse_reply(_judged(pk, [
        {"travel_rating": "good", "strengths": ["lowest_rent_of_these", "most_within_own_limit"], "weaknesses": [], "advice": "offer"},
        {"travel_rating": "acceptable", "strengths": [], "weaknesses": ["dates_differ", "some_over_own_limit"], "advice": "offer_with_caveats"}]), pk)
    two = tr.parse_reply(_judged(pk, [
        {"travel_rating": "poor", "strengths": [], "weaknesses": ["some_over_own_limit"], "advice": "offer_with_caveats"},
        {"travel_rating": "good", "strengths": ["majority_average_within_stated_limit"], "weaknesses": [], "advice": "offer"}], order=[9, 7]), pk)
    assert [(o["travel_rating"], o["advice"], o["preference"]) for o in one] == [("good", "offer", 1), ("acceptable", "offer_with_caveats", 2)]
    assert [(o["travel_rating"], o["advice"], o["preference"]) for o in two] == [("poor", "offer_with_caveats", 2), ("good", "offer", 1)]
    assert one[0]["strengths"] == ["The lowest whole-apartment rent of these options.", "Most people's journeys are within their own stated travel time."]
    assert one[1]["weaknesses"] == ["The dates differ from the ones the party asked for.", "At least one person's journey is over their own stated travel time."]
    assert one[1]["advice_plain"] == "Can be offered, with the weaknesses explained to the customer." and one[0]["of"] == 2
    assert two[1]["strengths"] == ["The closest majority's average journey is within the stated travel time itself, not only double it."]
    # the figures the two replies report are identical and exact, whatever the judgement
    for a, b in zip(one, two):
        assert all(a[k] == b[k] for k in ("within_own_limit", "over_own_limit", "not_known", "group_result", "note"))
    # no sentence shown states a journey time
    for o in one + two:
        assert not re.search(r"\d", " ".join(o["strengths"] + o["weaknesses"] + [o["advice_plain"], o["travel_rating_plain"]]))


LONG = dict(minutes=("12", "22.3", "25", "28", "30", "75"))


@pytest.mark.parametrize("candidate,judgement", [
    # a rating that contradicts the result
    (option(), {"travel_rating": "not_known"}), (option(), {"travel_rating": "fails"}), (option(), {"travel_rating": "excellent"}),
    (option(), {"travel_rating": None}), (option(minutes=("10", "12")), {"travel_rating": "poor"}),          # everyone within, average within
    (option(flags=["LONG_JOURNEY"], **LONG), {"travel_rating": "good", "weaknesses": ["long_journey_for_some"], "advice": "offer_with_caveats"}),
    (option(status=tv.NEEDS_CONFIRMATION, extra=[{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]), {"travel_rating": "good"}),
    (option(status=tv.NEEDS_CONFIRMATION, extra=[{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]), {"travel_rating": "acceptable"}),
    # a point that is not true for this option, is on the wrong side, is not on the list, or is given twice
    (option(), {"strengths": ["everyone_within_own_limit"]}), (option(), {"strengths": ["lowest_rent_of_these"]}),     # only one option
    (option(distance=2), {"strengths": ["dates_as_requested"]}), (option(empty=4), {"strengths": ["no_empty_days"]}),
    (option(), {"weaknesses": ["journeys_not_known"], "advice": "offer_with_caveats"}),
    (option(), {"weaknesses": ["long_journey_for_some"], "advice": "offer_with_caveats"}),
    (option(), {"weaknesses": ["alternative_not_full_match"], "advice": "offer_with_caveats"}),
    (option(), {"weaknesses": ["other_points_to_confirm"], "advice": "offer_with_caveats"}),
    (option(), {"strengths": ["some_over_own_limit"]}), (option(), {"weaknesses": ["most_within_own_limit"], "advice": "offer_with_caveats"}),
    (option(), {"strengths": ["close_to_the_tram"]}), (option(), {"strengths": ["most_within_own_limit", "most_within_own_limit"]}),
    (option(), {"strengths": "most_within_own_limit"}), (option(), {"strengths": None}), (option(), {"weaknesses": [7]}),
    (option(), {"strengths": ["most_within_own_limit", "majority_average_within_stated_limit", "dates_as_requested", "no_empty_days", "x"]}),
    # something that must be flagged is left out
    (option(flags=["LONG_JOURNEY"], **LONG), {"travel_rating": "acceptable", "weaknesses": ["some_over_own_limit"], "advice": "offer_with_caveats"}),
    (option(status=tv.NEEDS_CONFIRMATION, extra=[{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]), {"weaknesses": []}),
    (option(klass="alternative_area"), {"weaknesses": [], "advice": "discuss_as_alternative"}),
    # advice that contradicts the option or the model's own weaknesses
    (option(), {"advice": "offer_with_caveats"}), (option(), {"advice": "confirm_first"}), (option(), {"advice": "discuss_as_alternative"}),
    (option(), {"weaknesses": ["some_over_own_limit"], "advice": "offer"}), (option(), {"advice": "Offer it."}),
    (option(klass="alternative_area"), {"advice": "offer_with_caveats"}), (option(klass="alternative_bathroom"), {"advice": "offer"}),
    (option(reasons=["BEDDING_NEEDS_CONFIRMATION"]), {"advice": "offer"}), (option(reasons=["BEDDING_NEEDS_CONFIRMATION"]), {"advice": "offer_with_caveats"}),
    (option(status=tv.NEEDS_CONFIRMATION, extra=[{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]), {"advice": "offer_with_caveats"}),
])
def test_a_judgement_that_is_untrue_hides_a_flag_or_contradicts_itself_is_refused_whole(candidate, judgement):
    pk = tr.packet([candidate])
    assert tr.parse_reply(_judged(pk, [{}]), pk)                 # the plain valid judgement for this option is accepted
    with pytest.raises(tr.ReplyMismatch):
        tr.parse_reply(_judged(pk, [judgement]), pk)


def test_what_must_be_flagged_and_what_may_be_chosen():
    from server import homebot_match as hm
    assert hm.ALTERNATIVE_AREA == "alternative_area" and hm.ALTERNATIVE_BATHROOM == "alternative_bathroom" and hm.LONG_JOURNEY == "LONG_JOURNEY"
    unknown = [{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}]
    f = tr.packet([option()])["options"][0]
    assert tr._ratings_allowed(f) == ("good", "acceptable", "poor") and tr._must_flag(f) == [] and tr._advice_allowed(f, []) == ("offer",)
    assert tr._advice_allowed(f, ["some_over_own_limit"]) == ("offer_with_caveats",)
    f = tr.packet([option(minutes=("10", "12"))])["options"][0]
    assert tr._ratings_allowed(f) == ("good", "acceptable")
    f = tr.packet([option(flags=["LONG_JOURNEY"], **LONG)])["options"][0]
    assert tr._ratings_allowed(f) == ("acceptable", "poor") and tr._must_flag(f) == ["long_journey_for_some"]
    f = tr.packet([option(status=tv.NEEDS_CONFIRMATION, extra=unknown)])["options"][0]
    assert tr._ratings_allowed(f) == ("not_known",) and tr._must_flag(f) == ["journeys_not_known"] and tr._advice_allowed(f, []) == ("confirm_first",)
    f = tr.packet([option(status=tv.NO_DESTINATIONS, minutes=())])["options"][0]
    assert tr._ratings_allowed(f) == ("not_applicable",)
    f = tr.packet([option(status=tv.EXCEEDS_CEILING)])["options"][0]
    assert tr._ratings_allowed(f) == ("fails",) and tr._advice_allowed(f, []) == ("confirm_first",)       # never "offer"
    f = tr.packet([option(klass=hm.ALTERNATIVE_BATHROOM)])["options"][0]
    assert tr._must_flag(f) == ["alternative_not_full_match"] and tr._advice_allowed(f, []) == ("discuss_as_alternative",)
    assert f["apartment"]["kind"] == "alternative_one_bathroom"
    # travel's own needs-confirmation reason is not repeated as an "other point"; anything else open is
    f = tr.packet([option(reasons=[hm.TRAVEL_NEEDS_CONFIRMATION])])["options"][0]
    assert f["apartment"]["open_points"] == []
    f = tr.packet([option(reasons=[hm.BEDDING_NEEDS_CONFIRMATION])])["options"][0]
    assert f["apartment"]["open_points"] == [hm.PLAIN[hm.BEDDING_NEEDS_CONFIRMATION]] and tr._advice_allowed(f, []) == ("confirm_first",)
    # every statement has a side and a sentence with no figure in it
    assert all(side in ("strength", "weakness") and not re.search(r"\d", text) for side, text, _ in tr.POINTS.values())
    # rent points need a real comparison: at least two options and one clear lowest or highest
    pk = tr.packet([option(7, rent="900.00"), option(9, rent="900.00"), option(11, rent="990.00")])
    assert [tr.POINTS["lowest_rent_of_these"][2](o, pk) for o in pk["options"]] == [False, False, False]
    assert [tr.POINTS["highest_rent_of_these"][2](o, pk) for o in pk["options"]] == [False, False, True]


def test_the_order_of_preference_never_puts_an_alternative_before_a_full_match():
    pk = tr.packet([option(7), option(9, klass="alternative_area"), option(11)])
    assert [o["preference"] for o in tr.parse_reply(_judged(pk, [{}, {}, {}], order=[11, 7, 9]), pk)] == [2, 3, 1]
    for order in ([9, 7, 11], [7, 9, 11], [7, 11], [7, 11, 9, 9], [7, 11, 12]):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_judged(pk, [{}, {}, {}], order=order), pk)


def test_only_a_valid_ai_assessment_of_every_apartment_makes_the_travel_checked_shortlist_ai_assessed():
    """T6: no rules-only way through. Journey figures without an accepted AI assessment are held, with the reason."""
    shown = [{"apartment_id": 7}, {"apartment_id": 9}]
    ready = {"state": "ready", "options": [{"apartment_id": 9}, {"apartment_id": 7}]}
    assert tr.gate(ready, "reviewed", shown) == {"state": "assessed", "held": False, "plain": tr.ASSESSED_TEXT}
    held = lambda reply, review, options=shown: tr.gate(reply, review, options)   # noqa: E731
    for reply, review, options, why in (
            ({"state": "off", "options": []}, "reviewed", shown, "off"), ({"state": "nothing", "options": []}, "reviewed", shown, "nothing"),
            ({"state": "unavailable", "options": []}, "reviewed", shown, "unavailable"), ({"state": "withheld", "options": []}, "reviewed", shown, "withheld"),
            ({"state": "something new", "options": []}, "reviewed", shown, "unavailable"), ({}, "reviewed", shown, "unavailable"),
            ({"state": "ready", "options": [{"apartment_id": 7}]}, "reviewed", shown, "incomplete"),      # a replacement not covered
            (ready, "queued", shown, "review"), (ready, "held", shown, "review"), (ready, "unreviewed", shown, "review"), (ready, None, shown, "review"),
            (ready, "reviewed", [], "empty")):
        out = held(reply, review, options)
        assert out["state"] == "held" and out["held"] is True and out["reason"] == why and out["plain"] == tr.HELD_PREFIX + tr.HELD_REASON[why]
    for text in (tr.ASSESSED_TEXT, tr.HELD_PREFIX, *tr.HELD_REASON.values(), *tr.ADVICE_PLAIN.values(), *tr.RATING_PLAIN.values()):
        assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", text)


def test_a_live_check_with_the_assessment_is_still_held_until_the_saved_ai_review_is_complete(grounded):
    c, eid, model = grounded
    out = hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Provider())
    assert out["ai_reply"]["state"] == "ready" and len(model.prompts) == 1
    for o in out["ai_reply"]["options"]:
        assert o["travel_rating"] == "good" and o["advice"] in ("offer", "confirm_first")
        assert 1 <= o["preference"] <= o["of"] == len(out["ai_reply"]["options"])
    assert sorted(o["preference"] for o in out["ai_reply"]["options"]) == list(range(1, len(out["ai_reply"]["options"]) + 1))
    assert hs._review_state(c.db, current(c, eid))["state"] != "reviewed"
    assert out["ai_gate"] == {"state": "held", "held": True, "reason": "review", "plain": tr.HELD_PREFIX + tr.HELD_REASON["review"]}


# ------------------------------------------------------------------------------------------------------------- the page
def test_page_shows_the_reply_with_its_sources_straight_after_it():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "client", "staff.js"), encoding="utf-8").read()
    css = open(os.path.join(root, "client", "styles.css"), encoding="utf-8").read()
    assert "const GMAPS_TEXT = '<span class=\"gmaps-text\" translate=\"no\">Google Maps</span>';" in js   # the name, unaltered, never translated
    block = js[js.index("const grounded = x =>"):js.index("const candRows =")]
    note, sources, warning = block.index('data-role="hb-travel-ai-note"'), block.index('data-role="hb-travel-sources"'), block.index('data-role="hb-walk-warning"')
    assert block.index('data-role="hb-travel-ai-view"') < note < sources < warning   # assessment, its figures, then directly the sources, then the walking notice
    for words in ("Travel: ${esc(ai.travel_rating_plain)}", "Strengths: ${ai.strengths.map(esc).join(' ')}", "Weaknesses: ${ai.weaknesses.map(esc).join(' ')}",
                  "Advice: ${esc(ai.advice_plain)}", "its choice ${ai.preference} of ${ai.of}"):
        assert words in block
    assert "Sources: ${GMAPS_TEXT}" in block and 'target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>' in block
    assert "/^https:\\/\\//.test(s.url || '')" in block and 'href="${esc(s.url)}"' in block
    assert "<details" not in block                                           # visible without a further click
    assert "${esc(src.walking_warning)}" in block and "${esc(ai.note)}" in block
    for words in ("Each person's own limit (${ai.stated_limit_minutes} minutes): ${ai.within_own_limit} within, ${ai.over_own_limit} over, ${ai.not_known} not known",
                  "Group result (closest majority against the doubled limit of ${ai.doubled_limit_minutes} minutes): ${esc(ai.group_plain)}"):
        assert words in block
    figures_block = js[js.index("const journeys = x =>"):js.index("const grounded = x =>")]
    assert "return `${grounded(x)}<details" in figures_block                 # in the same cell as that option's journey figures
    view = js[js.index("const travelView = r =>"):js.index("const travelBlock =")]
    assert "ai.state === 'ready' ? ai.options || [] : []" in view and 'data-role="hb-travel-ai"' in view
    assert 'data-role="hb-travel-gate"' in view and "gate.held ? 'Held.' : 'AI-assessed.'" in view and "${esc(gate.plain)}" in view
    assert "fourth" not in view.lower() and "Never more than three." in view
    assert "localStorage" not in block + view and "sessionStorage" not in block + view
    assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", block)
    rule = css[css.index(".gmaps-text {"):].split("}")[0]
    for part in ("font-size: 14px", "font-weight: 400", "white-space: nowrap", "color: #5E5E5E", "font-style: normal", "Roboto", "text-transform: none"):
        assert part in rule, part
    for text in (tr.OFF_TEXT, tr.NOT_OWN_ACCOUNT_TEXT, tr.NOTHING_TEXT, tr.UNAVAILABLE_TEXT, tr.WITHHELD_TEXT, tr.READY_TEXT, *tr.GROUP_PLAIN.values()):
        assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", text)
