"""The AI's assessment of a MateBot group's travel-checked shortlist (H28(i), GP6(d), T4/T6; 16 Nov 2026).

Synthetic figures and a stand-in for the model; no network, no provider, no database. Covers: off by default and only on
ALM's own AI account with the approved model; the model is given positions and figures only; every count, every person it
names and the group result must equal the code's own; differing stated times are combined only by the code, as approved
(H28(j)), and never by the model; a missing stated time, or members level for a place among the closest whose times would
change the result, can only be rated "not known"; what must be flagged cannot be left out; a wrong reply is withheld whole; no sentence from the model is
shown; and the gate holds the shortlist unless the saved review is current, covered every apartment shown and this check's
assessment was accepted.
"""
import inspect
import json
import logging

import pytest

from server import ai, homebot_travel as ht, homebot_travel_reply as tr, matebot_travel as mt, matebot_travel_reply as mr, routing

W, B, PT = mt.MODES
U = ht.UNMEASURED


def M(i, time, modes, journeys):
    return mt.Member(i, time, modes, journeys)


def option(aid, members, rent=900, open_points=(), replacement=False):
    return {"apartment_id": aid, "travel": mt.assess_group(members).as_dict(),
            "apartment": {"total_weekly_rent": rent, "open_points": list(open_points), "replacement": replacement}}


SAME_PASS = [M(1, "30 minutes", (PT,), {PT: 20}), M(2, "30 minutes", (W, PT), {W: 45, PT: 25})]
SAME_OVER_OWN = [M(1, "30 minutes", (PT,), {PT: 20}), M(2, "30 minutes", (PT,), {PT: 50}), M(3, "30 minutes", (PT,), {PT: 28})]
DIFFER = [M(1, "15 minutes", (PT,), {PT: 20}), M(2, "45 minutes", (PT,), {PT: 20})]
TIED = [M(1, "15 minutes", (PT,), {PT: 10}), M(2, "5 minutes", (PT,), {PT: 50}), M(3, "60+ minutes", (PT,), {PT: 50})]   # result depends on who is counted
TIED_SAME = [M(1, "30 minutes", (PT,), {PT: 10}), M(2, "15 minutes", (PT,), {PT: 20}), M(3, "45 minutes", (PT,), {PT: 20})]   # within either way
MISSING = [M(1, None, (PT,), {PT: 20}), M(2, "30 minutes", (PT,), {PT: 20}), M(3, "30 minutes", (), None)]
NOT_ANSWERED = [M(1, "30 minutes", (), {}), M(2, "30 minutes", (PT,), {PT: 20})]
PARTLY = [M(1, "30 minutes", (W, PT), {W: U, PT: 40}), M(2, "30 minutes", (PT,), {PT: U})]
FAILS = [M(1, "15 minutes", (PT,), {PT: 40}), M(2, "15 minutes", (PT,), {PT: 45})]
NOBODY = [M(1, "30 minutes", (PT,), None), M(2, None, (), None)]


def _packet_from(prompt):
    return json.loads(prompt.split("GOOGLE MAPS JOURNEY ESTIMATES (source: Google Maps;", 1)[1].split(")\n\n", 1)[1])


def _figures(o, pk):
    """The option as the model sees it (no counts), with the counts a careful reader would make."""
    who = lambda kind: [p["person"] for p in o["people"] if p.get("own_limit") == kind]   # noqa: E731
    counts = {"within_own_limit": len(who("within")), "over_own_limit": len(who("over")), "not_known": len(who("not_known")),
              "no_stated_limit": len(who("no_stated_time")), "no_destination": sum(p["journey"] == "no_destination" for p in o["people"])}
    return dict(o, counts=counts), who


def faithful(options):
    out, pk = [], {"options": options}
    for o in options:
        f, who = _figures(o, pk)
        weak = mr._must_flag(f)
        out.append({"apartment_id": o["apartment_id"], **{k: f["counts"][k] for k in mr.COUNT_KEYS},
                    "people_over_own_limit": who("over"), "people_not_known": who("not_known"), "group_result": o["group"]["result"],
                    "travel_rating": mr._ratings_allowed(f)[0], "strengths": [], "weaknesses": weak, "advice": mr._advice_allowed(f, weak)[0]})
    return {"options": out, "preferred_order": [o["apartment_id"] for o in options]}


class Model:
    """Stand-in for ai._ask: records every prompt and answers with make(options given)."""

    def __init__(self, make=faithful):
        self.make, self.prompts, self.systems, self.tokens = make, [], [], []

    def __call__(self, prompt, model=None, max_tokens=1500, system=""):
        self.prompts.append(prompt)
        self.systems.append(system)
        self.tokens.append(max_tokens)
        out = self.make(_packet_from(prompt)["options"])
        return out if isinstance(out, str) else json.dumps(out)


@pytest.fixture
def on(monkeypatch):
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR", "MMAI_AI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key-not-real")
    monkeypatch.setenv("MMAI_AI_MODE", "own-key")
    monkeypatch.setenv(tr.REPLY_ENV, "1")
    monkeypatch.setenv(routing.AGENT_TOOLS_ENV, "1")
    monkeypatch.setenv(routing.AGENT_TOOLS_KEY_ENV, "test-key-not-real")
    monkeypatch.setattr(ai, "_client", lambda: (_ for _ in ()).throw(AssertionError("real model call in a test")))
    model = Model()
    monkeypatch.setattr(ai, "_ask", model)
    return model


def _edit(change):
    def make(options):
        out = faithful(options)
        change(out)
        return out
    return make


# --------------------------------------------------------------------------------------------------- off unless all four
def test_off_by_default_and_never_on_a_borrowed_account_or_another_model(monkeypatch):
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model asked while off")))
    monkeypatch.delenv(tr.REPLY_ENV, raising=False)
    r = mr.travel_reply([option(1, SAME_PASS)])
    assert r == {"state": "off", "saved": False, "plain": mr.OFF_TEXT, "options": []}
    monkeypatch.setenv(tr.REPLY_ENV, "1")                                   # the switch alone is not enough
    monkeypatch.delenv(routing.AGENT_TOOLS_ENV, raising=False)
    r = mr.travel_reply([option(1, SAME_PASS)])
    assert r["state"] == "off" and r["plain"] == mr.NOT_OWN_ACCOUNT_TEXT and r["options"] == []
    assert mr.gate(r, True, [1], [1]) == {"state": "held", "held": True, "reason": "off", "plain": mr.HELD_PREFIX + mr.HELD_REASON["off"]}


def test_another_model_is_never_asked(on, monkeypatch):
    monkeypatch.setenv("MMAI_AI_MODEL", "some-other-model")                 # with ALM's own key the setting cannot pick another model
    assert ai.model_name() == ai.APPROVED_MODEL == "claude-sonnet-5-5"
    assert mr.travel_reply([option(1, SAME_PASS)])["model"] == "claude-sonnet-5-5"
    monkeypatch.setattr(ai, "model_name", lambda fast=False: "some-other-model")
    assert mr.travel_reply([option(1, SAME_PASS)])["state"] == "off" and len(on.prompts) == 1


# ------------------------------------------------------------------------------------------------ what the model is given
def test_the_model_is_given_positions_and_figures_only(on):
    r = mr.travel_reply([option(11, SAME_OVER_OWN, rent=950), option(12, DIFFER, rent=900, open_points=["an open point"], replacement=True)])
    assert r["state"] == "ready" and r["model"] == "claude-sonnet-5-5" and r["saved"] is False and len(on.prompts) == 1
    assert on.systems == [mr.SYSTEM] and on.tokens == [mr.OUTPUT_TOKENS]
    given = _packet_from(on.prompts[0])["options"]
    assert [o["apartment_id"] for o in given] == [11, 12] and all("counts" not in o for o in given)       # the model counts; code checks
    a, b = given
    assert a["people"] == [
        {"person": 1, "journey": "measured", "own_stated_time_minutes": 30, "own_limit": "within", "minutes": "20", "by": [PT]},
        {"person": 2, "journey": "measured", "own_stated_time_minutes": 30, "own_limit": "over", "minutes": "50", "by": [PT]},
        {"person": 3, "journey": "measured", "own_stated_time_minutes": 30, "own_limit": "within", "minutes": "28", "by": [PT]}]
    assert a["group"] == {"result": "within_doubled_limit", "same_stated_limit": True, "stated_limit_minutes": 30, "doubled_limit_minutes": 60,
                          "closest_majority_average_stated_time_minutes": "30.0", "doubled_limit_minutes_range": None,
                          "why_needs_confirmation": [], "closest_majority_size": 2, "closest_majority_average_minutes": "24.0",
                          "closest_majority_average_within_stated_limit": True}
    # differing stated times (15 and 45): the code's own figures as approved, already worked out; the model is given them, not asked
    assert b["group"] == {"result": "within_doubled_limit", "same_stated_limit": False, "stated_limit_minutes": None, "doubled_limit_minutes": "60.0",
                          "closest_majority_average_stated_time_minutes": "30.0", "doubled_limit_minutes_range": None,
                          "why_needs_confirmation": [], "closest_majority_size": 2, "closest_majority_average_minutes": "20.0",
                          "closest_majority_average_within_stated_limit": True}
    assert b["apartment"] == {"total_weekly_rent": 900, "open_points": ["an open point"], "took_the_place_of_one_that_failed_travel": True}
    keys = {k for o in given for k in o} | {k for o in given for p in o["people"] for k in p}
    assert keys == {"apartment_id", "people", "long_journey", "apartment", "group", "person", "journey", "own_stated_time_minutes", "own_limit", "minutes", "by"}
    for banned in ("name", "address", "school", "institution", "destination\"", "email", "enquiry"):
        assert banned not in on.prompts[0].split("GOOGLE MAPS JOURNEY ESTIMATES", 1)[1], banned
    assert "Do not work out a group result of your own" in on.prompts[0] and "Do not write sentences of your own" in on.prompts[0]


def test_at_most_three_apartments_reach_the_model_each_once(on):
    r = mr.travel_reply([option(i, SAME_PASS) for i in (1, 2, 2, 3, 4)])
    assert [o["apartment_id"] for o in _packet_from(on.prompts[0])["options"]] == [1, 2, 3] and mr.MAX_OPTIONS == 3
    assert [o["apartment_id"] for o in r["options"]] == [1, 2, 3]


# ----------------------------------------------------------------------------------------------------- the figures
def test_own_time_statuses_and_counts_for_each_kind_of_person():
    f = mr.option_figures(option(1, MISSING))
    assert [(p["person"], p["journey"], p.get("own_limit")) for p in f["people"]] == [
        (1, "measured", "no_stated_time"), (2, "measured", "within"), (3, "no_destination", None)]
    assert f["counts"] == {"within_own_limit": 1, "over_own_limit": 0, "not_known": 0, "no_stated_limit": 1, "no_destination": 1}
    assert f["group"]["result"] == "needs_confirmation" and f["group"]["why_needs_confirmation"] == ["stated_time_missing"]
    f = mr.option_figures(option(1, NOT_ANSWERED))
    assert f["people"][0] == {"person": 1, "journey": "travel_choice_not_answered", "own_stated_time_minutes": 30, "own_limit": "not_known"}
    assert f["counts"]["not_known"] == 1 and f["group"]["why_needs_confirmation"] == ["travel_choice_not_answered"]
    f = mr.option_figures(option(1, PARTLY))
    assert f["people"][0] == {"person": 1, "journey": "partly_measured", "own_stated_time_minutes": 30, "own_limit": "not_known",
                              "minutes_at_most": "40", "by": [PT], "choices_not_measured": [W]}
    assert f["people"][1]["journey"] == "not_known" and f["counts"]["not_known"] == 2
    assert f["group"]["why_needs_confirmation"] == ["journeys_not_known", "fastest_choice_not_confirmed"]
    f = mr.option_figures(option(1, NOBODY))
    assert f["group"]["result"] == "no_destinations" and f["counts"]["no_destination"] == 2
    assert mr.option_figures({"apartment_id": 1, "travel": None}) is None and mr.option_figures({"apartment_id": 1, "travel": {"status": "PASS"}}) is None


@pytest.mark.parametrize("members,rating,advice,flagged", [
    (SAME_PASS, ("good", "acceptable"), ("offer",), []),
    (SAME_OVER_OWN, ("good", "acceptable", "poor"), ("offer_with_caveats",), ["some_over_own_limit"]),
    (DIFFER, ("good", "acceptable", "poor"), ("offer_with_caveats",), ["some_over_own_limit"]),
    (MISSING, ("not_known",), ("confirm_first",), ["stated_time_missing"]),
    (TIED, ("not_known",), ("confirm_first",), ["some_over_own_limit", "closest_majority_tied"]),
    (TIED_SAME, ("good", "acceptable", "poor"), ("offer_with_caveats",), ["some_over_own_limit"]),
    (NOT_ANSWERED, ("not_known",), ("confirm_first",), ["journeys_not_known"]),
    (PARTLY, ("not_known",), ("confirm_first",), ["journeys_not_known"]),
    (FAILS, ("fails",), ("confirm_first",), ["some_over_own_limit", "long_journey_for_some"]),
])
def test_what_the_model_may_say_follows_the_figures(members, rating, advice, flagged):
    f = mr.option_figures(option(1, members))
    assert mr._ratings_allowed(f) == rating and mr._must_flag(f) == flagged and mr._advice_allowed(f, flagged) == advice


def test_a_missing_time_or_a_deciding_tie_is_never_rated_or_offered_without_confirmation(on):
    """The model cannot turn "needs confirmation" into a result, a rating or a go-ahead, whatever the journeys are."""
    for members, point in ((MISSING, "stated_time_missing"), (TIED, "closest_majority_tied")):
        for change in (lambda out: out["options"][0].update(travel_rating="good"),
                       lambda out: out["options"][0].update(travel_rating="acceptable"),
                       lambda out: out["options"][0].update(travel_rating="poor"),
                       lambda out: out["options"][0].update(advice="offer"),
                       lambda out: out["options"][0].update(advice="offer_with_caveats"),
                       lambda out: out["options"][0].update(group_result="within_doubled_limit"),
                       lambda out: out["options"][0].update(group_result="over_doubled_limit"),
                       lambda out: out["options"][0]["weaknesses"].remove(point)):
            on.make = _edit(change)
            r = mr.travel_reply([option(1, members)])
            assert r == {"state": "withheld", "saved": False, "plain": mr.WITHHELD_TEXT, "options": []}
    on.make = faithful
    r = mr.travel_reply([option(1, TIED)])
    o = r["options"][0]
    assert r["state"] == "ready" and o["group_result"] == "needs_confirmation" and o["group_plain"] == "needs confirmation"
    assert o["travel_rating"] == "not_known" and o["advice"] == "confirm_first" and o["same_stated_limit"] is False
    assert o["stated_limit_minutes"] is None and o["doubled_limit_minutes"] is None
    assert o["weaknesses"] == [mr.POINTS["some_over_own_limit"][1], mr.POINTS["closest_majority_tied"][1]]
    assert o["note"] == "Over their own stated time: person 2."
    given = _packet_from(on.prompts[-1])["options"][0]["group"]
    assert given["why_needs_confirmation"] == ["closest_majority_tied"] and given["closest_majority_average_minutes"] is None
    assert given["doubled_limit_minutes"] is None and given["doubled_limit_minutes_range"] is None       # neither way of counting is given


def test_differing_times_follow_the_codes_result_and_the_model_cannot_change_it(on):
    """H28(j): the code compared the closest majority with twice the average of its own stated times; the model reports that."""
    r = mr.travel_reply([option(1, DIFFER)])
    o = r["options"][0]
    assert r["state"] == "ready" and o["group_result"] == "within_doubled_limit" and o["same_stated_limit"] is False
    assert o["stated_limit_minutes"] is None and o["doubled_limit_minutes"] == "60.0"
    assert o["weaknesses"] == [mr.POINTS["some_over_own_limit"][1]] and o["note"] == "Over their own stated time: person 1."
    assert o["advice"] == "offer_with_caveats" and o["travel_rating"] in ("good", "acceptable", "poor")
    for change in (lambda out: out["options"][0].update(group_result="needs_confirmation"),
                   lambda out: out["options"][0].update(group_result="over_doubled_limit"),
                   lambda out: out["options"][0].update(travel_rating="not_known"),
                   lambda out: out["options"][0].update(travel_rating="fails"),
                   lambda out: out["options"][0].update(advice="offer"),
                   lambda out: out["options"][0]["weaknesses"].remove("some_over_own_limit")):
        on.make = _edit(change)
        assert mr.travel_reply([option(1, DIFFER)])["state"] == "withheld"
    # members level for a place whose stated times do not change the result: within, and the doubled figure is given as its range
    on.make = faithful
    r = mr.travel_reply([option(1, TIED_SAME)])
    given = _packet_from(on.prompts[-1])["options"][0]["group"]
    assert r["state"] == "ready" and given["result"] == "within_doubled_limit"
    assert given["doubled_limit_minutes"] is None and given["doubled_limit_minutes_range"] == ["45.0", "75.0"]
    assert given["closest_majority_average_stated_time_minutes"] is None and given["closest_majority_average_minutes"] == "15.0"
    over = [M(1, "5 minutes", (PT,), {PT: 10}), M(2, "5 minutes", (PT,), {PT: 11}), M(3, "5 minutes", (PT,), {PT: 12}),
            M(4, "60+ minutes", (PT,), {PT: 100}), M(5, "60+ minutes", (PT,), {PT: 101})]
    f = mr.option_figures(option(1, over))
    assert f["group"]["result"] == "over_doubled_limit" and f["group"]["doubled_limit_minutes"] == "10.0" and mr._ratings_allowed(f) == ("fails",)


@pytest.mark.parametrize("change", [
    lambda out: out["options"][0].update(within_own_limit=3),
    lambda out: out["options"][0].update(over_own_limit=0),
    lambda out: out["options"][0].update(not_known=1),
    lambda out: out["options"][0].update(no_stated_limit=1),
    lambda out: out["options"][0].update(people_over_own_limit=[1]),
    lambda out: out["options"][0].update(people_over_own_limit=[2, 2]),
    lambda out: out["options"][0].update(people_not_known=[3]),
    lambda out: out["options"][0].update(within_own_limit=True),
    lambda out: out["options"][0].update(within_own_limit="2"),
    lambda out: out["options"][0].update(group_result="over_doubled_limit"),
    lambda out: out["options"][0].update(apartment_id=99),
    lambda out: out["options"][0].update(comment="Person 2 lives 50 minutes away"),
    lambda out: out["options"][0].pop("advice"),
    lambda out: out["options"][0].update(weaknesses=[]),
    lambda out: out["options"][0].update(weaknesses=["some_over_own_limit", "journeys_not_known"]),
    lambda out: out["options"][0].update(strengths=["everyone_within_own_limit"]),
    lambda out: out["options"][0].update(strengths=["some_over_own_limit"]),
    lambda out: out["options"][0].update(strengths=["a sentence of the model's own"]),
    lambda out: out["options"][0].update(advice="offer"),
    lambda out: out["options"][0].update(travel_rating="not_known"),
    lambda out: out.update(preferred_order=[1, 1]),
    lambda out: out.update(preferred_order=[]),
    lambda out: out.update(note="extra"),
    lambda out: out["options"].append(dict(out["options"][0])),
    lambda out: out["options"].clear(),
])
def test_any_mismatch_withholds_the_whole_reply(on, change, caplog):
    on.make = _edit(change)
    with caplog.at_level(logging.DEBUG):
        r = mr.travel_reply([option(1, SAME_OVER_OWN)])
    assert r == {"state": "withheld", "saved": False, "plain": mr.WITHHELD_TEXT, "options": []}
    said = " | ".join(caplog.messages)
    assert "Person 2" not in said and "minutes" not in said and "own_limit" not in said and "travel reply withheld: validation" in said
    assert mr.gate(r, True, [1], [1])["reason"] == "withheld"


def test_text_around_the_object_or_not_json_is_withheld_or_unavailable(on):
    on.make = lambda options: "not json at all"
    assert mr.travel_reply([option(1, SAME_PASS)])["state"] in ("withheld", "unavailable")
    on.make = lambda options: (_ for _ in ()).throw(ai.AIUnavailable("provider said something", kind="transient"))
    r = mr.travel_reply([option(1, SAME_PASS)])
    assert r == {"state": "unavailable", "saved": False, "plain": mr.UNAVAILABLE_TEXT, "options": []}
    on.make = lambda options: (_ for _ in ()).throw(RuntimeError("unexpected"))
    assert mr.travel_reply([option(1, SAME_PASS)])["state"] == "unavailable"


def test_the_models_judgement_is_shown_only_in_sentences_written_here(on):
    def make(options):
        out = faithful(options)
        out["options"][0].update(travel_rating="acceptable", strengths=["majority_average_within_stated_limit", "lowest_rent_of_these"],
                                 weaknesses=["some_over_own_limit"])
        out["options"][1].update(strengths=["everyone_within_own_limit"], weaknesses=["highest_rent_of_these"], advice="offer_with_caveats")
        out["preferred_order"] = [2, 1]
        return out
    on.make = make
    r = mr.travel_reply([option(1, SAME_OVER_OWN, rent=800), option(2, SAME_PASS, rent=950)])
    assert r["state"] == "ready" and r["plain"] == mr.READY_TEXT
    a, b = r["options"]
    assert (a["preference"], a["of"], b["preference"]) == (2, 2, 1)
    assert a["travel_rating_plain"] == "acceptable" and a["advice_plain"] == mr.ADVICE_PLAIN["offer_with_caveats"]
    assert a["strengths"] == [mr.POINTS["majority_average_within_stated_limit"][1], mr.POINTS["lowest_rent_of_these"][1]]
    assert a["weaknesses"] == [mr.POINTS["some_over_own_limit"][1]] and a["note"] == "Over their own stated time: person 2."
    assert (a["within_own_limit"], a["over_own_limit"], a["not_known"], a["no_stated_limit"], a["no_destination"]) == (2, 1, 0, 0, 0)
    assert b["strengths"] == [mr.POINTS["everyone_within_own_limit"][1]] and b["weaknesses"] == [mr.POINTS["highest_rent_of_these"][1]]
    assert b["note"] == "No one is over their own stated time and no journey is unknown." and b["group_plain"] == "within the doubled limit"
    shown = {s for o in r["options"] for s in o["strengths"] + o["weaknesses"]} | {o["advice_plain"] for o in r["options"]}
    assert shown <= {v[1] for v in mr.POINTS.values()} | set(mr.ADVICE_PLAIN.values())


def test_nothing_measured_means_nothing_is_asked(on):
    r = mr.travel_reply([option(1, NOBODY), option(2, [M(1, "30 minutes", (), {}), M(2, "30 minutes", (PT,), {PT: U})])])
    assert r == {"state": "nothing", "saved": False, "plain": mr.NOTHING_TEXT, "options": []} and on.prompts == []
    assert mr.gate(r, True, [1, 2], [1, 2])["reason"] == "nothing"


def test_an_open_point_must_be_flagged_and_confirmed_first(on):
    r = mr.travel_reply([option(1, SAME_PASS, open_points=["a practical need is not confirmed"])])
    o = r["options"][0]
    assert o["advice"] == "confirm_first" and o["weaknesses"] == [mr.POINTS["other_points_to_confirm"][1]]
    on.make = _edit(lambda out: out["options"][0].update(weaknesses=[], advice="offer"))
    assert mr.travel_reply([option(1, SAME_PASS, open_points=["a practical need is not confirmed"])])["state"] == "withheld"


# ------------------------------------------------------------------------------------------------------------ the gate
def test_the_gate_is_assessed_only_when_review_coverage_and_reply_all_hold(on):
    reply = mr.travel_reply([option(1, SAME_PASS), option(2, SAME_OVER_OWN)])
    assert mr.gate(reply, True, [1, 2], [1, 2, 3]) == {"state": "assessed", "held": False, "plain": mr.ASSESSED_TEXT}
    assert mr.gate(reply, False, [1, 2], [1, 2])["reason"] == "review"
    assert mr.gate(reply, None, [1, 2], [1, 2])["reason"] == "review" and mr.gate(reply, 1, [1, 2], [1, 2])["reason"] == "review"
    assert mr.gate(reply, True, [1, 2], [1])["reason"] == "not_reviewed"               # a replacement the saved review never saw
    assert mr.gate(reply, True, [1, 2], None)["reason"] == "not_reviewed"
    assert mr.gate(reply, True, [1, 2, 3], [1, 2, 3])["reason"] == "incomplete"
    assert mr.gate(reply, True, [1], [1])["reason"] == "incomplete"
    assert mr.gate(reply, True, [], [1])["reason"] == "empty"
    for state in ("off", "nothing", "unavailable", "withheld", "something-else", None):
        g = mr.gate({"state": state, "options": []}, True, [1], [1])
        assert g["held"] is True and g["state"] == "held" and g["plain"].startswith(mr.HELD_PREFIX)
    assert set(mr.HELD_REASON) == {"off", "nothing", "unavailable", "withheld", "incomplete", "review", "not_reviewed", "empty"}


def test_nothing_is_stored_and_the_module_touches_no_database_or_file():
    src = inspect.getsource(mr)
    for banned in ("from .db", "import sqlite", "open(", "execute(", "audit(", "os.environ"):
        assert banned not in src, banned
    assert mr.OUTPUT_TOKENS <= ai.OUTPUT_TOKEN_CEILING
