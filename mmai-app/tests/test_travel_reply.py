"""The AI's travel reply for a live travel check (H28(i), 16 Nov 2026).

Synthetic journey replies and a stand-in for the model; no network. Covers: off by default and only on ALM's own AI account
with the approved model; what the model is given (figures and positions only); every count and the group result is checked
against the code's own figures, with "not known" neither within nor over a limit; a wrong, unverifiable or unsafe reply is
withheld whole; the reply is returned once and nothing of it (or of the journeys) is stored or logged; the saved AI review
is unchanged; and the page shows the reply with its Google Maps sources straight after it.
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


def faithful(options, note=None):
    out = []
    for o in options:
        c = {"within_own_limit": sum(p.get("own_limit") == "within" for p in o["people"]),
             "over_own_limit": sum(p.get("own_limit") == "over" for p in o["people"]),
             "not_known": sum(p.get("own_limit") == "not_known" for p in o["people"])}
        text = note or f"{c['within_own_limit']} people are within their own limit and {c['over_own_limit']} are over it."
        out.append({"apartment_id": o["apartment_id"], **c, "group_result": o["group"]["result"], "note": text})
    return {"options": out}


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
    assert [o["apartment_id"] for o in reply["options"]] == [x["apartment_id"] for x in out["listed"] + out["alternatives"] + out["unresolved"]]
    for o in reply["options"]:
        # three people in the party: each journey by public transport is 10 minutes, inside the 30-minute limit
        assert (o["within_own_limit"], o["over_own_limit"], o["not_known"], o["no_destination"]) == (3, 0, 0, 0)
        assert o["group_result"] == tr.WITHIN and o["group_plain"] == "within the doubled limit"
        assert (o["stated_limit_minutes"], o["doubled_limit_minutes"]) == (30, 60)
        assert o["note"] == "3 people are within their own limit and 0 are over it."
    # each option still carries its sources, to sit straight after the note
    assert all(x["travel_sources"]["links"] == [SOURCE] for x in shown)
    # nothing was written anywhere but the one count-only audit record, and the saved AI review is as it was
    assert snapshot(c) == before and current(c, eid)["result_json"] == stored_before
    assert json.dumps(hr.packet(c.db, current(c, eid)), sort_keys=True, default=str) == packet_before
    (a,) = audits(c)
    assert json.loads(a["detail"]) == {"scenario": CUR, "shortlist_id": out["shortlist_id"], "requests": 16, "not_requested": 0}
    everywhere = re.sub(r"[0-9a-f]{16,}", "", json.dumps(c.db.execute("SELECT * FROM audit_log").fetchall(), default=str) + json.dumps(snapshot(c)))
    for leaked in ("within their own limit", "10.0", "synthetic", "homebot-travel-reply"):
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
        assert set(o) == {"apartment_id", "stated_limit_minutes", "doubled_limit_minutes", "people", "group"}   # the model counts; code checks
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
    given = _packet_from(model.prompts[0])
    unknown = [p for o in given["options"] for p in o["people"] if p["journey"] == "not_known"]
    assert unknown and all(set(p) == {"person", "journey", "own_limit"} and p["own_limit"] == "not_known" for p in unknown)

    # a model that counts the unknown person as within the limit (or as over it) is refused
    def optimistic(options):
        out = faithful(options)
        for o in out["options"]:
            o["within_own_limit"], o["not_known"] = o["within_own_limit"] + o["not_known"], 0
        return out
    monkeypatch.setattr(ai, "_ask", Model(optimistic))
    assert hs.live_travel_check(c.db, eid, CUR, "Anna", now=NOW, post=Patchy())["ai_reply"]["state"] == "withheld"

    def pessimistic(options):
        out = faithful(options)
        for o in out["options"]:
            o["over_own_limit"], o["not_known"] = o["not_known"], 0
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
def _reply(pk, **over):
    row = {"apartment_id": 7, "within_own_limit": 5, "over_own_limit": 1, "not_known": 0, "group_result": tr.WITHIN,
           "note": "5 people are within their own 30-minute limit and 1 person is over it."}
    row.update(over)
    return json.dumps({"options": [row]})


def test_a_faithful_reply_is_accepted_with_the_codes_own_figures():
    pk = tr.packet([figures()])
    (o,) = tr.parse_reply(_reply(pk), pk)
    assert o == {"apartment_id": 7, "within_own_limit": 5, "over_own_limit": 1, "not_known": 0, "no_destination": 0,
                 "stated_limit_minutes": 30, "doubled_limit_minutes": 60, "group_result": tr.WITHIN,
                 "group_plain": "within the doubled limit", "note": "5 people are within their own 30-minute limit and 1 person is over it."}
    for note in ("5 of 6 people met their own limit; 1 did not.", "Person 6 is over the limit at 45.5 minutes.",
                 "The closest 4 people average 21.8 minutes, within the doubled limit of 60 minutes.",
                 "5 met their own limit and 1 didn't. The group result is within the doubled limit."):
        assert tr.parse_reply(_reply(pk, note=note), pk)[0]["note"] == note


@pytest.mark.parametrize("over", [
    {"within_own_limit": 6}, {"within_own_limit": 4, "over_own_limit": 2}, {"over_own_limit": 0}, {"not_known": 1},
    {"within_own_limit": True}, {"within_own_limit": "5"}, {"within_own_limit": 5.0},
    {"group_result": tr.OVER}, {"group_result": tr.NEEDS}, {"group_result": "pass"}, {"apartment_id": 8}, {"apartment_id": "7"},
    {"note": ""}, {"note": "x" * 401}, {"note": None},
    {"note": "4 people met their own limit and 2 did not."},                 # wrong counts in the words
    {"note": "5 met their own limit and 2 did not."},                        # one right, one wrong
    {"note": "6 people are within the limit."},
    {"note": "2 people are not known."},
    {"note": "Five met the requirement, one did not."},                      # number words cannot be checked
    {"note": "All of them are within the limit."}, {"note": "Everyone is fine."}, {"note": "Most are within the limit."},
    {"note": "Person 6 takes 46 minutes."},                                  # a figure that is not in the data
    {"note": "Person 6 takes 45,5 minutes and person 1 takes 13 minutes."},
    {"note": "The group fails the doubled limit."}, {"note": "The option is ruled out on travel."},
    {"note": "See https://example.com for the route."}, {"note": "5 are <b>within</b> the limit."},
])
def test_a_reply_that_does_not_match_the_figures_is_refused_whole(over):
    pk = tr.packet([figures()])
    with pytest.raises(tr.ReplyMismatch):
        tr.parse_reply(_reply(pk, **over), pk)


def test_reply_shape_is_exact():
    pk = tr.packet([figures(), {**figures(), "apartment_id": 9}])
    one = json.loads(_reply(pk))["options"][0]
    good = {"options": [one, {**one, "apartment_id": 9}]}
    assert len(tr.parse_reply(json.dumps(good), pk)) == 2
    for bad in ("not json at all", "[]", json.dumps({"options": [one]}), json.dumps({"options": [one, one]}),
                json.dumps({"options": [{**one, "apartment_id": 9}, one]}), json.dumps({"options": good["options"], "extra": 1}),
                json.dumps({"options": [one, {**one, "apartment_id": 9, "verdict": "pass"}]}),
                json.dumps({"options": [one, {k: v for k, v in one.items() if k != "note"}]})):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(bad, pk)


def test_a_group_result_cannot_be_talked_up_or_down():
    pk = tr.packet([figures(status=tv.NEEDS_CONFIRMATION, extra=[{"index": 7, "status": tv.UNMEASURED_PERSON, "fastest_minutes": None}])])
    base = {"not_known": 1, "group_result": tr.NEEDS}
    for note in ("The group meets the doubled limit.", "Overall the option passes on travel.", "The party is within the doubled limit."):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, note=note, **base), pk)
    for note in ("The group does not meet the doubled limit.", "The group average is too long.", "The option is ruled out.",   # talked down
                 "Person 7 is not known, but the group passes.", "Person 7 is not known and the group is within the doubled limit.",
                 "The group passes although person 7 is not known."):         # a hedge elsewhere in the sentence does not soften the claim
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, note=note, **base), pk)
    for note in ("The group result needs confirmation because 1 person is not known.", "Whether the group meets the doubled limit is not known.",
                 "It is not yet known whether the group is within the doubled limit.", "The group result cannot be said to meet the doubled limit yet."):
        assert tr.parse_reply(_reply(pk, note=note, **base), pk)
    pk = tr.packet([figures(status=tv.EXCEEDS_CEILING)])
    for note in ("The group passes the doubled limit.", "The group is not over the doubled limit.", "The group average of 21.8 minutes is fine."):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, group_result=tr.OVER, note=note), pk)
    for note in ("The group result is over the doubled limit.", "The group does not meet the doubled limit."):
        assert tr.parse_reply(_reply(pk, group_result=tr.OVER, note=note), pk)
    pk = tr.packet([figures()])
    for note in ("The group does not meet the doubled limit.", "The group average of 21.8 minutes exceeds the doubled limit."):
        with pytest.raises(tr.ReplyMismatch):
            tr.parse_reply(_reply(pk, note=note), pk)
    assert tr.parse_reply(_reply(pk, note="The group is not over the doubled limit: the closest 4 average 21.8 minutes."), pk)


# ------------------------------------------------------------------------------------------------------------- the page
def test_page_shows_the_reply_with_its_sources_straight_after_it():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "client", "staff.js"), encoding="utf-8").read()
    css = open(os.path.join(root, "client", "styles.css"), encoding="utf-8").read()
    assert "const GMAPS_TEXT = '<span class=\"gmaps-text\" translate=\"no\">Google Maps</span>';" in js   # the name, unaltered, never translated
    block = js[js.index("const grounded = x =>"):js.index("const candRows =")]
    note, sources, warning = block.index('data-role="hb-travel-ai-note"'), block.index('data-role="hb-travel-sources"'), block.index('data-role="hb-walk-warning"')
    assert note < sources < warning                                          # the sources follow the reply directly; then the walking notice
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
    assert "localStorage" not in block + view and "sessionStorage" not in block + view
    assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", block)
    rule = css[css.index(".gmaps-text {"):].split("}")[0]
    for part in ("font-size: 14px", "font-weight: 400", "white-space: nowrap", "color: #5E5E5E", "font-style: normal", "Roboto", "text-transform: none"):
        assert part in rule, part
    for text in (tr.OFF_TEXT, tr.NOT_OWN_ACCOUNT_TEXT, tr.NOTHING_TEXT, tr.UNAVAILABLE_TEXT, tr.WITHHELD_TEXT, tr.READY_TEXT, *tr.GROUP_PLAIN.values()):
        assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", text)
