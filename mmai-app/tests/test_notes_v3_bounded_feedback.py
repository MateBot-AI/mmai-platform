"""NOTES-v3 bounded feedback (root assignment, 9 Nov 2026): notes_facts._ask may send at most ONE parser/validator-feedback
correction and at most TWO logical model requests in total, ai's compact retry included.

  * valid first reply            -> returned, no correction, 1 request;
  * malformed / multi-object / invalid-enum first reply (ordinary, not compact) -> ONE correction request, then the second reply
    is read; a valid second reply is returned carrying "_correction" provenance (never persisted);
  * second reply unusable or still rejected by the validator (any code but benign rejected_duplicate) -> terminal AINoRetry
    (the queue must not re-run the pair);
  * compact retry already used   -> never a correction: the compact reply is the last one read, so unusable / invalid -> terminal;
  * provider failure on EITHER request / truncation on the correction request -> terminal (AINoRetry / AIOutputTruncated);
  * the budget is enforced before any call (a spent meter never reaches the provider); whole-reply validator problems (>20 items,
    non-object entries) and non-scalar enum types are handled in the wrapper, the validator byte-unchanged;
  * meter['asks'] == meter['turns'] length == model calls actually made on every path; FEEDBACK_VERSION is in the cache input key.
The strict parser (ai._json_from) and validator (notes_facts.validate_reply) are unchanged and never select one object out of
several. All model traffic is a fabricated ai._ask; no live call, no real people, no database.
"""
import hashlib
import inspect
import json

import pytest

from server import ai, notes_facts

FIELDS = {"else_text": "I need my own room. Quiet after 10pm please.", "work": "I work variable shifts at a cafe.", "intro": ""}
PARTY = 1
GOOD_ITEM = {"category": "own_room_need", "polarity": "affirmed", "strength": "essential", "subject": "self", "field": "else_text",
             "start": 0, "end": 19, "span": "I need my own room.", "value": None}
GOOD = json.dumps({"items": [GOOD_ITEM], "residual": False})
BAD_ENUM = json.dumps({"items": [dict(GOOD_ITEM, strength="critical")], "residual": False})          # 'critical' is outside STRENGTHS
BAD_CATEGORY = json.dumps({"items": [dict(GOOD_ITEM, category="pet_policy_wish")], "residual": False})  # not in CATEGORIES
MULTI = GOOD + "\n" + json.dumps({"items": [dict(GOOD_ITEM, field="work", start=7, end=22, span="variable shifts", category="variable_or_night_hours", strength="stated")], "residual": False})
MALFORMED = '{"items": [{"category": "own_room_need", "polarity": "affirmed",}], "residual": false}'   # braces present, invalid JSON
TRUNCATED_TEXT = '{"items": [{"category": "own_room_need", "polarity": "affirmed",'                          # no closing brace: no JSON object at all


def scripted(replies, calls):
    """A fabricated ai._ask: pops the next scripted reply; an Exception instance is raised instead of returned."""
    def _ask(prompt, model=None, max_tokens=1500, system=""):
        calls.append({"prompt": prompt, "max_tokens": max_tokens})
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return _ask


def _run(monkeypatch, replies):
    calls: list = []
    monkeypatch.setattr(ai, "_ask", scripted(list(replies), calls))
    meter = {}
    try:
        out = notes_facts._ask(FIELDS, PARTY, meter=meter)
        err = None
    except ai.AIUnavailable as e:
        out, err = None, e
    return out, err, calls, meter


# ---------------------------------------------------------------- guards on the unchanged parser / validator
def test_strict_parser_rejects_multi_object_whole_and_stated_is_a_valid_strength():
    with pytest.raises(ai.AIUnusableReply):
        ai._json_from(MULTI)                       # never the first or the last object: the whole reply is unusable
    assert "stated" in notes_facts.STRENGTHS
    v = notes_facts.validate_reply(json.loads(GOOD), FIELDS, PARTY)
    assert v["rejected"] == [] and len(v["staff_items"]) == 1
    assert notes_facts.correction_feedback(json.loads(GOOD), FIELDS, PARTY) is None
    fb = notes_facts.correction_feedback(json.loads(BAD_ENUM), FIELDS, PARTY)
    assert fb and fb["codes"] == ["rejected_enum"] and fb["items"][0]["code"] == "rejected_enum" and fb["items"][0]["strength"] == "invalid" and "span" not in fb["items"][0]


# ---------------------------------------------------------------- valid first
def test_valid_first_reply_no_correction_one_request(monkeypatch):
    out, err, calls, meter = _run(monkeypatch, [GOOD])
    assert err is None and len(calls) == 1 and meter["asks"] == 1 == len(calls)
    assert meter["correction"] is False and "_correction" not in out
    assert notes_facts.validate_reply(out, FIELDS, PARTY)["rejected"] == []
    assert "CORRECTION" not in calls[0]["prompt"]


# ---------------------------------------------------------------- malformed -> valid
@pytest.mark.parametrize("first,problem", [(MALFORMED, "malformed"), (MULTI, "malformed"), ("no json here", "no_json"), (TRUNCATED_TEXT, "no_json"), ('[{"items": []}]', "not_object")])
def test_unparseable_first_reply_gets_one_correction_then_valid(monkeypatch, first, problem):
    out, err, calls, meter = _run(monkeypatch, [first, GOOD])
    assert err is None and len(calls) == 2 and meter["asks"] == 2 == len(calls)
    assert meter["correction"] is True
    assert out["_correction"]["first_reply"] == {"parse_problem": problem, "validator_codes": [], "global": [], "rejected_items": []} and out["_correction"]["requests"] == 2
    p2 = calls[1]["prompt"]
    assert p2.startswith(notes_facts.build_prompt(FIELDS, PARTY)) and "CORRECTION" in p2 and notes_facts.PARSE_FEEDBACK[problem] in p2
    assert "I need my own room" not in p2.split("CORRECTION", 1)[1], "no span / person text echoed in the feedback"
    assert calls[1]["max_tokens"] <= 1200
    assert notes_facts.validate_reply(out, FIELDS, PARTY)["rejected"] == []


# ---------------------------------------------------------------- bad enum -> valid
@pytest.mark.parametrize("first", [BAD_ENUM, BAD_CATEGORY])
def test_bad_enum_first_reply_gets_closed_feedback_then_valid(monkeypatch, first):
    out, err, calls, meter = _run(monkeypatch, [first, GOOD])
    assert err is None and len(calls) == 2 and meter["asks"] == 2
    assert out["_correction"]["first_reply"]["rejected_items"] == [{"item": 0, "code": "rejected_enum"}]
    fb = calls[1]["prompt"].split("CORRECTION", 1)[1]
    assert "rejected_enum" in fb and notes_facts.REJECTION_FEEDBACK["rejected_enum"] in fb
    assert '"item": 0' in fb and "I need my own room" not in fb and "critical" not in fb and "pet_policy_wish" not in fb, "structural projection only: invalid values become 'invalid'"
    assert '"strength": "invalid"' in fb or '"category": "invalid"' in fb
    assert notes_facts.validate_reply(out, FIELDS, PARTY)["staff_items"][0]["strength"] == "essential"


def test_duplicate_only_rejection_is_not_a_correction_trigger(monkeypatch):
    dup = json.dumps({"items": [GOOD_ITEM, dict(GOOD_ITEM)], "residual": False})   # the same fact twice: benign dedupe, not invalid
    assert [r["code"] for r in notes_facts.validate_reply(json.loads(dup), FIELDS, PARTY)["rejected"]] == ["rejected_duplicate"]
    out, err, calls, meter = _run(monkeypatch, [dup])
    assert err is None and len(calls) == 1 and meter["asks"] == 1 and "_correction" not in out


# ---------------------------------------------------------------- invalid second -> terminal
@pytest.mark.parametrize("second", [MALFORMED, MULTI, "still nothing"])
def test_unparseable_second_reply_is_terminal_no_third_request(monkeypatch, second):
    out, err, calls, meter = _run(monkeypatch, [BAD_ENUM, second])
    assert out is None and isinstance(err, ai.AINoRetry) and not isinstance(err, ai.AIOutputTruncated)
    assert err.kind == "validation" and len(calls) == 2 and meter["asks"] == 2


@pytest.mark.parametrize("second", [BAD_CATEGORY, json.dumps({"items": [dict(GOOD_ITEM, start=3, end=19, span="eed my own room.")], "residual": False})])
def test_second_reply_still_rejected_by_validator_is_terminal(monkeypatch, second):
    out, err, calls, meter = _run(monkeypatch, [BAD_ENUM, second])
    assert out is None and isinstance(err, ai.AINoRetry) and err.kind == "validation" and len(calls) == 2 and meter["asks"] == 2


def test_second_reply_with_staff_only_items_is_valid(monkeypatch):
    fields = dict(FIELDS, else_text="I do not need my own room. Quiet after 10pm please.")
    neg = {"items": [dict(GOOD_ITEM, polarity="negated", start=0, end=26, span="I do not need my own room.")], "residual": False}
    v = notes_facts.validate_reply(neg, fields, PARTY)
    assert v["rejected"] == [] and v["packet_items"] == [] and v["staff_items"][0]["polarity"] == "negated", "a valid negated item is staff-only, not invalid"
    calls: list = []
    monkeypatch.setattr(ai, "_ask", scripted([BAD_ENUM, json.dumps(neg)], calls))
    meter = {}
    out = notes_facts._ask(fields, PARTY, meter=meter)
    assert out["_correction"]["requests"] == 2 and meter["asks"] == 2 and notes_facts.validate_reply(out, fields, PARTY)["staff_items"]


# ---------------------------------------------------------------- compact already used -> never a correction
def test_compact_reply_never_gets_a_correction(monkeypatch):
    cut = ai.AIOutputTruncated("AI response was cut off at the 1200-token output budget", max_tokens=1200)
    # cut-off, then an unusable compact reply: terminal, two requests, no correction
    out, err, calls, meter = _run(monkeypatch, [cut, MALFORMED])
    assert out is None and isinstance(err, ai.AINoRetry) and err.code == "size_limit_compact_unusable"
    assert len(calls) == 2 and meter["asks"] == 2 and meter["compact"] is True and meter.get("correction") is not True
    # cut-off, then a compact reply with a bad enum: no correction (budget spent), the invalid compact reply is terminal
    out, err, calls, meter = _run(monkeypatch, [cut, BAD_ENUM])
    assert out is None and isinstance(err, ai.AINoRetry) and err.code == "size_limit_compact_unusable"
    assert len(calls) == 2 and meter["asks"] == 2 and meter["correction"] is False
    # cut-off, then a valid compact reply: returned, two requests
    out, err, calls, meter = _run(monkeypatch, [cut, GOOD])
    assert err is None and len(calls) == 2 and meter["asks"] == 2 and "_correction" not in out


# ---------------------------------------------------------------- unavailable / truncation
def test_provider_failure_on_correction_request_is_terminal(monkeypatch):
    out, err, calls, meter = _run(monkeypatch, [BAD_ENUM, ai.AIUnavailable("AI service call failed", kind="provider", code="provider")])
    assert out is None and isinstance(err, ai.AINoRetry) and err.kind == "provider" and err.code == "provider"
    assert len(calls) == 2 and meter["asks"] == 2


def test_truncated_correction_reply_is_terminal_without_compact_retry(monkeypatch):
    cut = ai.AIOutputTruncated("AI response was cut off at the 1200-token output budget", max_tokens=1200)
    out, err, calls, meter = _run(monkeypatch, [MALFORMED, cut])
    assert out is None and isinstance(err, ai.AIOutputTruncated) and len(calls) == 2 and meter["asks"] == 2
    assert "CORRECTION" in calls[1]["prompt"], "the second request was the correction, and nothing followed it"


def test_provider_failure_on_first_request_is_terminal_for_the_notes_component(monkeypatch):
    out, err, calls, meter = _run(monkeypatch, [ai.AIUnavailable("AI service call failed", kind="provider", code="provider")])
    assert out is None and isinstance(err, ai.AINoRetry) and err.kind == "provider" and err.code == "provider"
    assert len(calls) == 1 and meter["asks"] == 1 and meter["turns"] == ["first"]
    # the queue's own loop (retry on plain AIUnavailable, stop on AINoRetry) therefore cannot spend a third request
    out, err, calls, meter = _run(monkeypatch, [ai.AIUnavailable("x"), ai.AIOutputTruncated("cut", max_tokens=1200), GOOD])
    assert isinstance(err, ai.AINoRetry) and len(calls) == 1


def test_spent_or_partly_spent_meter_is_enforced_before_any_call(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai, "_ask", scripted([GOOD, GOOD], calls))
    with pytest.raises(ai.AINoRetry):
        notes_facts._ask(FIELDS, PARTY, meter={"asks": 2, "turns": ["first", "correction"]})
    with pytest.raises(ai.AINoRetry):
        notes_facts._ask(FIELDS, PARTY, meter={"asks": 0, "turns": ["first", "compact"]})
    assert calls == []
    # one call left: a single plain call, no compact retry and no correction
    meter = {"asks": 1, "turns": ["first"]}
    out = notes_facts._ask(FIELDS, PARTY, meter=meter)
    assert len(calls) == 1 and meter["asks"] == 2 and meter["turns"] == ["first", "first"] and "_correction" not in out
    calls.clear(); monkeypatch.setattr(ai, "_ask", scripted([BAD_ENUM, GOOD], calls))
    with pytest.raises(ai.AINoRetry):
        notes_facts._ask(FIELDS, PARTY, meter={"asks": 1, "turns": ["first"]})
    assert len(calls) == 1, "an invalid reply on the last permitted call is terminal, not corrected"


# ---------------------------------------------------------------- whole-reply validator problems (not only per item)
def test_more_than_20_items_is_a_whole_reply_problem_corrected_once_then_terminal(monkeypatch):
    many = json.dumps({"items": [GOOD_ITEM] * 21, "residual": False})
    assert any(r["code"] == "rejected_enum" and r["category"] is None for r in notes_facts.validate_reply(json.loads(many), FIELDS, PARTY)["rejected"])
    out, err, calls, meter = _run(monkeypatch, [many, GOOD])
    assert err is None and len(calls) == 2 and out["_correction"]["first_reply"]["global"] == ["too_many_items"]
    assert "rejected_enum" in out["_correction"]["first_reply"]["validator_codes"] and notes_facts.GLOBAL_FEEDBACK["too_many_items"] in calls[1]["prompt"]
    out, err, calls, meter = _run(monkeypatch, [MALFORMED, many])
    assert out is None and isinstance(err, ai.AINoRetry) and len(calls) == 2, "21 valid-looking items on the second reply do not bypass the whole-reply validator"


def test_non_object_entry_in_items_is_fed_back_closed(monkeypatch):
    first = json.dumps({"items": [GOOD_ITEM, "just a string"], "residual": False})
    out, err, calls, meter = _run(monkeypatch, [first, GOOD])
    assert err is None and out["_correction"]["first_reply"]["global"] == ["item_not_object"]
    assert "just a string" not in calls[1]["prompt"].split("CORRECTION", 1)[1]


@pytest.mark.parametrize("bad", [[], {}, ["own_room_need"], 7, {"x": 1}])
@pytest.mark.parametrize("key", ["category", "polarity", "strength", "subject", "field"])
def test_non_scalar_enum_values_are_closed_in_the_wrapper_not_a_TypeError(monkeypatch, bad, key):
    first = json.dumps({"items": [dict(GOOD_ITEM, **{key: bad})], "residual": False})
    out, err, calls, meter = _run(monkeypatch, [first, GOOD])
    assert err is None and len(calls) == 2 and notes_facts.validate_reply(out, FIELDS, PARTY)["packet_items"]
    fb = calls[1]["prompt"].split("CORRECTION", 1)[1]
    assert f'"{key}": "invalid"' in fb and "own_room_need" not in fb.replace('"category": "own_room_need"', "") or key != "category"
    # the same reply as the LAST one is terminal, still without a TypeError
    out, err, calls, meter = _run(monkeypatch, [MALFORMED, first])
    assert out is None and isinstance(err, ai.AINoRetry) and err.kind == "validation"


# ---------------------------------------------------------------- turn ledger
@pytest.mark.parametrize("replies,expected", [
    ([GOOD], ["first"]), ([MALFORMED, GOOD], ["first", "correction"]), ([BAD_ENUM, MALFORMED], ["first", "correction"]),
    ([ai.AIOutputTruncated("c", max_tokens=1200), GOOD], ["first", "compact"]), ([ai.AIOutputTruncated("c", max_tokens=1200), MALFORMED], ["first", "compact"]),
    ([ai.AIUnavailable("x")], ["first"]), ([MALFORMED, ai.AIUnavailable("x")], ["first", "correction"]),
    ([ai.AIOutputTruncated("c", max_tokens=1200), ai.AIOutputTruncated("c", max_tokens=1800)], ["first", "compact"]),
])
def test_turn_ledger_matches_calls_on_success_and_failure(monkeypatch, replies, expected):
    out, err, calls, meter = _run(monkeypatch, replies)
    assert meter["turns"] == expected and meter["asks"] == len(calls) == len(expected) <= notes_facts.MAX_LOGICAL_REQUESTS
    if out is not None and "_correction" in out:
        assert out["_correction"]["turns"] == expected


# ---------------------------------------------------------------- cache contract and unchanged strict core
def test_feedback_version_is_part_of_the_cache_input_key():
    tup = notes_facts.input_tuple(FIELDS, PARTY)
    assert tup["feedback_version"] == notes_facts.FEEDBACK_VERSION == "notes-feedback-3" and tup["prompt_version"] == "notes-extract-3"
    old = {k: v for k, v in tup.items() if k != "feedback_version"}
    old_key = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert notes_facts.cache_key(FIELDS, PARTY) != old_key, "v2 cache rows (partial accepted items under the old contract) are never reused"


def test_strict_parser_and_validator_source_unchanged_from_v2():
    """Textual proof: the source of the strict core is byte-identical to notes-v2 (1cdb24ce...) / its ai._json_from.
    build_prompt moved ONCE on 9 Nov 2026 (triage fix 2: rule 9 asks for the tidiness degree in the person's words; prompt
    version notes-extract-3); grounding_failure moved ONCE on 9 Nov 2026 (second round: a tidiness degree binds to the home
    context only - one added branch after the unchanged lifestyle support check, catalogue t4c-v2.3) and ONCE more on 10 Nov 2026
    (succ3 N2: the same tidiness branch also binds the degree to WHAT it is said of - the person or the home - so 'a spotless driving
    record' / 'my workshop is spotless' ground nothing; catalogue t4c-v2.4; the home/work and support checks themselves are unchanged);
    validate_reply, anchoring and value cleaning below are still the v2 bytes."""
    pinned = {
        "validate_reply": "d7167581dd51edd41831fc9c61557ef656b7e829c4dfcc4604640c69b59519d2",
        "anchor_items": "83c8131982fc454ac8161687dafedf4f2f0cc9e43c5902ce18e3fe78908171b3",
        "build_prompt": "e09428491a5c6f2eeb3931d26fbfeffa9acfb4b2bbe054216467a8dda2bac5a4",   # was 979bee1d... before fix 2
        "_clean_value": "a56a4ad9dd6b86618e3604022d3c276dff04bd2e19313040c8af9bec273b817f",
        "grounding_failure": "d03f41cf785535a6d80dfe56ef9ea747ed1d912a36008a3bd7962a236bfd6f1f",   # was 25cbf722... before the succ3 person/home binding, c889e6a8... before the tidy home-context binding
        "excluded_content": "0afdc98a2018b1d78bb1f097fc571a0a50838d26162429f67a8438f7d03bf17e",
    }
    for name, h in pinned.items():
        assert hashlib.sha256(inspect.getsource(getattr(notes_facts, name)).encode()).hexdigest() == h, name
    assert hashlib.sha256(inspect.getsource(ai._json_from).encode()).hexdigest() == "d8af97c3bdc0ebfa7a9a86942eda8e3bb53cfd5016e3b4196b0e0c802cf8e33e"
    assert notes_facts.STRENGTHS == ("essential", "matters", "preference", "stated")


def test_double_truncation_is_terminal_two_requests(monkeypatch):
    cut = ai.AIOutputTruncated("cut", max_tokens=1200)
    out, err, calls, meter = _run(monkeypatch, [cut, ai.AIOutputTruncated("cut again", max_tokens=1800)])
    assert out is None and isinstance(err, ai.AIOutputTruncated) and err.attempts == 2 and len(calls) == 2 and meter["asks"] == 2


# ---------------------------------------------------------------- asks == turns on every path, never more than two
@pytest.mark.parametrize("replies", [
    [GOOD], [MALFORMED, GOOD], [BAD_ENUM, GOOD], [BAD_ENUM, MALFORMED], [BAD_ENUM, BAD_ENUM],
    [ai.AIOutputTruncated("c", max_tokens=1200), MALFORMED], [ai.AIOutputTruncated("c", max_tokens=1200), GOOD],
    [MALFORMED, ai.AIUnavailable("x", kind="provider", code="provider")], [ai.AIUnavailable("x", kind="provider", code="provider")],
    [json.dumps({"items": [GOOD_ITEM] * 21, "residual": False}), GOOD], [json.dumps({"items": [dict(GOOD_ITEM, category=[])], "residual": False}), MALFORMED],
])
def test_meter_equals_actual_requests_and_never_exceeds_two(monkeypatch, replies):
    out, err, calls, meter = _run(monkeypatch, replies)
    assert meter["asks"] == len(calls) <= notes_facts.MAX_LOGICAL_REQUESTS
    if out is not None:
        assert "_anchoring" not in json.dumps(out.get("items")), "provenance keys stay at the top level of the returned reply only"
