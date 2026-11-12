"""Triage fix 2 (9 Nov 2026): 'keep shared spaces clean' names no tidiness degree, so it must not ground the strongest form option
'Very tidy' (that accepted fact reached a staff brief as a false contradiction with a structured 'Relaxed' answer). Exercised through
the REAL bounded path notes_facts._ask with a scripted ai._ask (no provider): the first reply's over-read item is rejected with
rejected_grounding, the ONE correction turn carries that feedback, a corrected reply without the item is accepted, and a correction
that insists fails closed (2-request policy, exact anchoring, strict validator all unchanged). Fabricated doubles only."""
import json

from server import ai, notes_facts
from tests.test_notes_v3_bounded_feedback import scripted

INTRO = "Leila here - I cook a lot, keep shared spaces clean and like a quiet weeknight. Happy to share a room if the people are easy-going."
FIELDS = {"else_text": "", "work": "", "intro": INTRO}


def _item(span, category, value, strength="stated"):
    s = INTRO.index(span)
    return {"category": category, "polarity": "affirmed", "strength": strength, "subject": "self", "field": "intro", "start": s, "end": s + len(span), "span": span, "value": value}


TIDY = _item("keep shared spaces clean", "lifestyle_self_description", {"field": "tidy", "answer": "Very tidy"})
QUIET = _item("like a quiet weeknight", "quiet_at_night", None, "preference")
OVER_READ = json.dumps({"items": [TIDY, QUIET], "residual": False})
CORRECTED = json.dumps({"items": [QUIET], "residual": False})


def _run(monkeypatch, replies):
    calls: list = []
    monkeypatch.setattr(ai, "_ask", scripted(list(replies), calls))
    meter = {}
    try:
        return notes_facts._ask(FIELDS, 1, meter=meter), None, calls, meter
    except ai.AIUnavailable as e:
        return None, e, calls, meter


def test_bare_clean_supports_no_tidiness_option():
    for answer in ("Very tidy", "Average", "Relaxed"):
        v = notes_facts.validate_reply({"items": [dict(TIDY, value={"field": "tidy", "answer": answer})], "residual": False}, FIELDS, 1)
        assert v["packet_items"] == [] and v["rejected"] and v["rejected"][0]["code"] == "rejected_grounding", answer
    # the degree written -> the option is grounded (strict validator otherwise unchanged)
    txt = "I am very tidy and like a quiet weeknight."
    f = {"else_text": txt, "work": "", "intro": ""}
    it = {"category": "lifestyle_self_description", "polarity": "affirmed", "strength": "stated", "subject": "self", "field": "else_text",
          "start": 5, "end": 14, "span": "very tidy", "value": {"field": "tidy", "answer": "Very tidy"}}
    v = notes_facts.validate_reply({"items": [it], "residual": False}, f, 1)
    assert [x["value"]["answer"] for x in v["packet_items"]] == ["Very tidy"]


def test_real_bounded_path_corrects_the_over_read_and_keeps_two_request_policy(monkeypatch):
    out, err, calls, meter = _run(monkeypatch, [OVER_READ, CORRECTED])
    assert err is None and len(calls) == 2 and meter["turns"] == ["first", "correction"]
    fb = out["_correction"]["first_reply"]
    assert fb["validator_codes"] == ["rejected_grounding"] and fb["rejected_items"] == [{"item": 0, "code": "rejected_grounding"}]
    assert "CORRECTION" in calls[1]["prompt"] and "rejected_grounding" in calls[1]["prompt"] and "keep shared spaces clean" not in calls[1]["prompt"].split("CORRECTION")[1]
    v = notes_facts.validate_reply(out, FIELDS, 1)
    assert [x["category"] for x in v["packet_items"]] == ["quiet_at_night"]   # no tidy fact: P160 'Relaxed' can no longer be contradicted by it


def test_insisting_correction_fails_closed(monkeypatch):
    out, err, calls, meter = _run(monkeypatch, [OVER_READ, OVER_READ, CORRECTED])
    assert out is None and isinstance(err, ai.AINoRetry) and len(calls) == 2 and meter["asks"] == 2


def test_prompt_and_versions_carry_the_change():
    p = notes_facts.build_prompt(FIELDS, 1)
    assert "9. lifestyle_self_description" in p and "names no degree" in p
    # BEFORE (fix 2): catalogue t4c-v2.2. AFTER (second round, 9 Nov 2026): t4c-v2.3 - the validator now binds a tidiness degree to
    # the home context and reads 'spotless' as tidiness wording, so accepted projections made under v2.2 are no longer current and
    # withdraw_outdated_notes re-queues them (the same currentness path fix 2 introduced). Prompt version unchanged.
    # BEFORE (succ3): t4c-v2.3. AFTER (10 Nov 2026, succ3 N2): t4c-v2.4 - the degree must also be said of the person or the home
    # ('a spotless driving record', 'my workshop is spotless' ground nothing), so v2.3 projections are no longer current either.
    # The later no-inference guidance advances the prompt, preserving the validator.
    assert notes_facts.PROMPT_VERSION == "notes-extract-5" and notes_facts.CATALOGUE_VERSION == "t4c-v2.4"
    assert notes_facts.FEEDBACK_VERSION == "notes-feedback-3" and notes_facts.MAX_LOGICAL_REQUESTS == 2


def test_vocabulary_stays_degree_faithful_no_standalone_meticulous_or_immaculate():
    """'meticulous at work' / 'immaculate references' are not tidiness degrees: only tidy/clean wording with its degree grounds Very tidy"""
    for txt in ("I am meticulous at work and like a quiet weeknight.", "I have immaculate references from my last landlord."):
        word = "meticulous at work" if "meticulous" in txt else "immaculate references"
        s = txt.index(word)
        it = {"category": "lifestyle_self_description", "polarity": "affirmed", "strength": "stated", "subject": "self", "field": "else_text",
              "start": s, "end": s + len(word), "span": word, "value": {"field": "tidy", "answer": "Very tidy"}}
        v = notes_facts.validate_reply({"items": [it], "residual": False}, {"else_text": txt, "work": "", "intro": ""}, 1)
        assert v["packet_items"] == [] and v["rejected"][0]["code"] in ("rejected_grounding", "rejected_category"), txt   # fails closed either way
    pat = notes_facts._LIFESTYLE_SUPPORT["Very tidy"].pattern
    assert "meticulous" not in pat and "immaculate" not in pat


def _tidy_item(txt, span, answer, field="else_text"):
    s = txt.index(span)
    return {"category": "lifestyle_self_description", "polarity": "affirmed", "strength": "stated", "subject": "self", "field": field,
            "start": s, "end": s + len(span), "span": span, "value": {"field": "tidy", "answer": answer}}


def _validate(txt, span, answer, field="else_text"):
    fields = {"else_text": "", "work": "", "intro": ""}
    fields[field] = txt
    return notes_facts.validate_reply({"items": [_tidy_item(txt, span, answer, field)], "residual": False}, fields, 1)


def test_tidiness_degree_binds_to_the_home_context_only():
    """Second round (root, 9 Nov 2026): 'very neat at work' is not a home tidiness answer; a home word elsewhere in the sentence does
    not rescue a work-bound degree, and a work word elsewhere does not block a home-bound one. 'spotless' is tidiness wording."""
    # work-bound degree: grounds nothing (rejected_grounding, fails closed)
    for txt, span in (("I am very neat at work.", "very neat at work"), ("I am very neat at work.", "very neat"),
                      ("I am very tidy at work, but relaxed at home.", "very tidy at work"), ("I am very tidy at work, but relaxed at home.", "very tidy"),
                      ("I am extremely tidy in the office and have a dog at home.", "extremely tidy in the office")):
        v = _validate(txt, span, "Very tidy")
        assert v["packet_items"] == [] and v["rejected"] and v["rejected"][0]["code"] == "rejected_grounding", (txt, span, v["rejected"])
    # the same sentence's home-bound clause still grounds its own answer
    v = _validate("I am very tidy at work, but relaxed at home.", "relaxed at home", "Relaxed")
    assert [x["value"]["answer"] for x in v["packet_items"]] == ["Relaxed"]
    # home-bound degree beside a work word elsewhere: grounded
    for txt, span in (("I work in an office, but keep my home very tidy.", "keep my home very tidy"), ("I work in an office, but keep my home very tidy.", "very tidy"),
                      ("I keep our home spotless.", "keep our home spotless"), ("I keep our home spotless.", "spotless"), ("I am very tidy.", "very tidy")):
        v = _validate(txt, span, "Very tidy")
        assert [x["value"]["answer"] for x in v["packet_items"]] == ["Very tidy"], (txt, span, v["rejected"])
    # the 'work' FIELD is still a source: a home-bound degree written there grounds, a work-bound one does not
    assert [x["value"]["answer"] for x in _validate("Nurse. At home I am very tidy.", "very tidy", "Very tidy", field="work")["packet_items"]] == ["Very tidy"]
    assert _validate("Nurse; very tidy at work.", "very tidy at work", "Very tidy", field="work")["packet_items"] == []
