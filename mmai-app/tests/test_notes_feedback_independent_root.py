"""Independent notes-feedback boundary probes; fabricated provider, no DB."""
import hashlib
import json
import pytest
from server import ai, notes_facts as n

TEXT = "Step-free access is essential for me."
FIELDS = {"else_text": TEXT, "work": "", "intro": ""}
ITEM = dict(category="step_free_access", polarity="affirmed", strength="essential",
            subject="self", field="else_text", start=0, end=len(TEXT), span=TEXT, value=None)
GOOD = json.dumps({"items": [ITEM], "residual": False})
MANY = json.dumps({"items": [ITEM] * 21, "residual": False})


def script(monkeypatch, replies):
    calls = []
    replies = iter(replies)

    def ask(prompt, **kwargs):
        calls.append(prompt)
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(ai, "_ask", ask)
    return calls


def test_global_item_limit_triggers_correction(monkeypatch):
    assert any(r["code"] == "rejected_enum" for r in n.validate_reply(json.loads(MANY), FIELDS, 1)["rejected"])
    calls = script(monkeypatch, [MANY, GOOD])
    out = n._ask(FIELDS, 1, meter={})
    assert len(calls) == 2
    assert not n.validate_reply(out, FIELDS, 1)["rejected"]


def test_second_global_item_limit_terminal(monkeypatch):
    calls = script(monkeypatch, ["not json", MANY])
    with pytest.raises(ai.AINoRetry):
        n._ask(FIELDS, 1, meter={})
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [[], {}, ["step_free_access"]])
def test_non_scalar_category_corrected_not_unexpected_exception(monkeypatch, bad):
    calls = script(monkeypatch, [json.dumps({"items": [dict(ITEM, category=bad)], "residual": False}), GOOD])
    result = n._ask(FIELDS, 1, meter={})
    assert len(calls) == 2
    assert n.validate_reply(result, FIELDS, 1)["packet_items"]


def test_first_provider_failure_terminal(monkeypatch):
    calls = script(monkeypatch, [ai.AIUnavailable("fabricated unavailable")])
    with pytest.raises(ai.AINoRetry):
        n._ask(FIELDS, 1, meter={})
    assert len(calls) == 1


def test_outer_retry_cannot_spend_third_request(monkeypatch):
    calls = script(monkeypatch, [
        ai.AIUnavailable("fabricated unavailable"),
        ai.AIOutputTruncated("fabricated cutoff", max_tokens=1200), GOOD,
    ])
    meter = {}
    # Same distinction used by the queue: terminal exceptions stop immediately.
    for _ in range(3):
        try:
            n._ask(FIELDS, 1, meter=meter)
            break
        except ai.AINoRetry:
            break
        except ai.AIUnavailable:
            continue
    assert len(calls) <= 2
    assert meter["asks"] == len(calls)


def test_spent_meter_refuses_before_provider(monkeypatch):
    calls = script(monkeypatch, [GOOD])
    with pytest.raises(ai.AINoRetry):
        n._ask(FIELDS, 1, meter={"asks": 2, "turns": ["first", "correction"]})
    assert not calls


@pytest.mark.parametrize("path,expected", [
    ("valid", ["first"]),
    ("correction", ["first", "correction"]),
    ("compact", ["first", "compact"]),
    ("provider", ["first"]),
])
def test_turn_ledger_on_success_and_failure(monkeypatch, path, expected):
    replies = {
        "valid": [GOOD],
        "correction": ["not json", GOOD],
        "compact": [ai.AIOutputTruncated("fabricated cutoff", max_tokens=1200), GOOD],
        "provider": [ai.AIUnavailable("fabricated unavailable")],
    }
    calls = script(monkeypatch, replies[path])
    meter = {}
    try:
        n._ask(FIELDS, 1, meter=meter)
    except ai.AIUnavailable:
        pass
    assert meter.get("turns") == expected
    assert meter["asks"] == len(calls) == len(meter["turns"])


def test_feedback_contract_changes_cache_key():
    old = {"fields": FIELDS, "party_size": 1, "model": ai.MODEL,
           "prompt_version": "notes-extract-2", "catalogue_version": "t4c-v2.1"}
    old_key = hashlib.sha256(json.dumps(old, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    assert n.cache_key(FIELDS, 1) != old_key
