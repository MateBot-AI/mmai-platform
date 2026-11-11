"""C4 (10 November 2026): native JSON-schema output for the batch review, no implicit SDK retries, a closed failure for a rejected
structured-output request, and ONE bounded repair turn for the groups of a review reply that fail the validator.

Pinned here:
  * every Anthropic client is built with max_retries=0 (the callers' own bounded attempts are the only retries, and they are counted);
  * `_ask` keeps its signature; the review call sends output_config={"format": {"type": "json_schema", "schema": <object root>}}
    and nothing else does; the schema states SHAPE only, in keywords the format supports - parse_review_batch still decides;
  * a TypeError (SDK refuses the argument) or anthropic.BadRequestError on a schema-bearing call is AISchemaUnsupported (closed
    kind/code 'schema', closed sentence): one call, same model, the schema still attached, no free-text reply accepted, no retry
    in ai or in the queue, and the SDK's / provider's words reach neither a row nor the log;
  * a review reply that is usable as a whole but leaves some groups invalid gets exactly one repair call for those groups only,
    quoting the validator's problems; a replacement counts only if it passes the same validator; any failure of the repair call
    keeps the first results (the valid ones untouched, the invalid ones marked with the closed kind of that failure); a compact
    attempt never gets a repair; there is never a second repair;
  * v2: a reply that is unusable as a whole (no JSON, wrong shape, an index outside the batch) gets the same single allowance as
    ONE whole-batch repair call in fixed words - nothing of the first reply is kept, quoted or lined up by guess; a second
    unusable reply (or any failure of that call) re-raises the original failure unchanged;
  * v2: the prompt asks for the object {"groups": [...]} the schema describes (REVIEW_CONTRACT moved with it), on the first turn
    and on both repair turns;
  * v2: review_token_budget is a public pure function, min(ceiling, 1200 + 400*G + 150*M + 90*A): member-aware, equal to the
    earlier formula for two-person groups, never above OUTPUT_TOKEN_CEILING. An engineering constraint, not a rule;
  * v2: the closed repair telemetry the queue reads: meter repair / repair_scope / repair_groups / repair_fixed / repair_error;
  * v3: review_token_estimate is the one shared UNCAPPED formula (the queue splits batches on it); review_token_budget is
    min(ceiling, estimate) and stays the budget actually requested;
  * v3: when the whole-batch repair call fails for a cause no retry can cure - the output format rejected, credentials or the
    SDK missing - THAT closed failure is raised and the queue stops on it; every other repair failure (outage, an unusable
    repair reply, a defect) still re-raises the original unusable-reply failure for the queue's own attempts;
  * v4 (root ruling): the AINoRetry type contract holds for the whole-batch repair too - a repair reply cut off at its budget
    surfaces as AIOutputTruncated (terminal, never dressed as the retryable parse failure), with its own closed sentence that
    does not claim a compact retry, because none was made; no compact or repair turn is added for it;
  * v5 (root ruling): on the published-site credential proxy route the token goes out as x-api-key (the platform's documented
    contract) and the SDK's own Authorization header is removed, so the request carries exactly one credential header; the
    own-key and sandbox constructors are untouched. Checked on the real SDK's built request, which is never sent. This pins
    what the app builds; it says nothing about how the live proxy behaves.
Fabricated people, one disposable SQLite file per test, the model replaced by doubles at `_ask` or at the SDK client; every
outbound connection is refused. No provider traffic.
"""
import copy
import inspect
import json
import logging
import re
import socket

import pytest

from server import ai, ai_queue
from server.db import rows
from tests.test_t6_fail_closed import _good, _groups, _reply_builder, _run

MARKER = "PRIVATE_SCHEMA_TOKEN_Qx"
REPAIR_SKIPPED = object()


@pytest.fixture(autouse=True)
def _no_network_no_sleep(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network use in a C4 test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "MMAI_AI_URL_VAR", "MMAI_AI_TOKEN_VAR", "MMAI_AI_MODE", "MMAI_AI_MODEL"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- helpers
def _packets(db, monkeypatch):
    """The review packets of one fabricated run (six two-person groups at one apartment), exactly as the queue builds them."""
    _run(db)
    seen = []
    real = ai.assess_groups_and_apartments

    def record(groups, meter=None):
        seen.append(copy.deepcopy(groups))
        return real(groups, meter=meter)
    with monkeypatch.context() as m:
        m.setattr(ai, "assess_groups_and_apartments", record)
        m.setattr(ai, "_ask", _reply_builder(_good))
        ai_queue.process_pending(db, batch_size=100, workers=1, attempts=1)
    assert len(seen) == 1 and len(seen[0]) >= 3
    return seen[0]


def _descriptor(prompt):
    return json.loads(prompt[prompt.rindex("\n\n") + 2:])


def _full(g):
    """A reply item that also satisfies the native schema (every key present)."""
    return {"index": g["index"], "findings": [{"fact_ids": g["fact_ids"][:2], "note": "fine", "kind": "evidence"}], "rating": 7, "rationale": "fine",
            "verdict": "approve", "replace_refs": [], "replace_reasons": [],
            "apartments": [{"apartment_id": x["apartment_id"], "reason": "r"} for x in g["apartments_in_rule_order"]]}


def _bad_rating(g):
    return {**_full(g), "rating": 42}


def _scripted_ask(calls, *turns):
    """An `_ask` double with the real signature. Turn n answers through turns[n] (a per-group item maker, an exception to raise,
    or a literal string); the last turn repeats. Records (prompt, max_tokens, schema in force) per call."""
    def _ask(prompt, model=None, max_tokens=1500, system=""):
        calls.append({"prompt": prompt, "max_tokens": max_tokens, "schema": ai._SCHEMA.get(), "model": model})
        turn = turns[min(len(calls), len(turns)) - 1]
        if isinstance(turn, BaseException):
            raise turn
        if isinstance(turn, str):
            return turn
        return json.dumps({"groups": [x for x in (turn(g) for g in _descriptor(prompt)) if x is not None]})
    return _ask


class _Block:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, text, stop_reason="end_turn"):
        self.content, self.stop_reason = [_Block(text)], stop_reason


def _sdk_client(calls, answer):
    """A double for the SDK client (the pattern of tests/test_t5_ai_failure_sinks.py): records every create(**kw); `answer(kw)`
    returns the reply text or raises."""
    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            return _Msg(answer(kw))

    class _Client:
        messages = _Messages()
    return lambda: _Client()


def _answer_with(make_item):
    return lambda kw: json.dumps({"groups": [make_item(g) for g in _descriptor(kw["messages"][0]["content"])]})


def _sinks(db) -> str:
    return json.dumps({"groups": rows(db.execute("SELECT ai_error, held_reason, ranking_basis FROM groups")),
                       "briefs": rows(db.execute("SELECT error FROM group_briefs")),
                       "reviews": rows(db.execute("SELECT invalid_reason, review_json FROM review_rounds")),
                       "state": dict(ai_queue.STATE)}, default=str)


def _bad_request(text):
    import anthropic
    import httpx2
    return anthropic.BadRequestError(text, response=httpx2.Response(400, request=httpx2.Request("POST", "https://example.invalid/v1/messages")),
                                     body={"error": {"type": "invalid_request_error", "message": text}})


# ---------------------------------------------------------------- (1) no implicit SDK retries
def test_every_client_is_built_without_implicit_sdk_retries(monkeypatch):
    import anthropic
    built = []

    class _Recorder:
        def __init__(self, **kw):
            built.append(kw)
    monkeypatch.setattr(anthropic, "Anthropic", _Recorder)
    # the published-site credential proxy
    monkeypatch.setenv("MMAI_AI_URL_VAR", "C4_PROXY_URL")
    monkeypatch.setenv("MMAI_AI_TOKEN_VAR", "C4_PROXY_TOKEN")
    monkeypatch.setenv("C4_PROXY_URL", "https://proxy.invalid")
    monkeypatch.setenv("C4_PROXY_TOKEN", "fabricated-token")
    ai._client()
    monkeypatch.delenv("MMAI_AI_URL_VAR")
    monkeypatch.delenv("MMAI_AI_TOKEN_VAR")
    # ALM's own key, straight to the provider
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key")
    assert ai.using_own_key()
    ai._client()
    # the development sandbox proxy
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://sandbox.invalid")
    assert not ai.using_own_key()
    ai._client()
    assert len(built) == 3 and ai.SDK_MAX_RETRIES == 0
    assert all(kw.get("max_retries") == 0 for kw in built), built
    assert "base_url" in built[0] and built[1].get("api_key") == "fabricated-key" and "api_key" not in built[2]   # the three distinct paths
    # no constructor call in the source is left without it, including any added later
    src = inspect.getsource(ai)
    ctor_calls = re.findall(r"\bAnthropic\((.*)\)", src)
    assert len(ctor_calls) == 3 and all("max_retries=SDK_MAX_RETRIES" in c for c in ctor_calls), ctor_calls


def test_the_real_sdk_client_reports_zero_retries(monkeypatch):
    """Not a double: the installed SDK's own client object (constructed, never used) carries max_retries 0."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key")
    assert ai._client().max_retries == 0


# ---------------------------------------------------------------- (1b) v5: the credential header each route builds
CREDENTIAL_HEADERS = ("authorization", "x-api-key")
SDK_ENV_EXTRAS = ("ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR")


def _built_request(client):
    """The request the real SDK client would send for one message call - built in memory, never sent (no transport is touched)."""
    from anthropic._models import FinalRequestOptions
    return client._build_request(FinalRequestOptions(method="post", url="/v1/messages", json_data={"model": ai.model_name()}))


def _publish_route(monkeypatch, token="fabricated-token"):
    for var in SDK_ENV_EXTRAS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MMAI_AI_URL_VAR", "C4_PROXY_URL")
    monkeypatch.setenv("MMAI_AI_TOKEN_VAR", "C4_PROXY_TOKEN")
    monkeypatch.setenv("C4_PROXY_URL", "https://proxy.invalid")
    monkeypatch.setenv("C4_PROXY_TOKEN", token)
    assert ai.using_user_key() and not ai.using_own_key() and ai.ai_mode() == "user-key"


def test_the_publish_route_builds_the_token_as_x_api_key_and_no_authorization_header(monkeypatch):
    """The real SDK (not a double), the EFFECTIVE headers of the built request - not the client's auth_headers property, which
    still reports the bearer the SDK would have sent. Nothing is sent."""
    import anthropic
    _publish_route(monkeypatch)
    client = ai._client()
    assert isinstance(client, anthropic.Anthropic) and client.max_retries == 0 == ai.SDK_MAX_RETRIES
    assert client.api_key == "fabricated-token", "the key stays explicit, so the SDK looks for no other credential"
    assert client.custom_auth is None, "nothing is left that could add a credential header when a request is sent"
    request = _built_request(client)
    assert request.headers.get("x-api-key") == "fabricated-token"
    assert "authorization" not in request.headers, "exactly one credential header: no bearer beside x-api-key"
    assert sorted(k.lower() for k, v in request.headers.items() if "fabricated-token" in v) == ["x-api-key"], "the token is in no other header"
    assert request.headers.get("anthropic-version") == "2023-06-01", "the version header the route already sent is kept"
    assert request.method == "POST" and str(request.url) == "https://proxy.invalid/v1/messages"
    assert request.headers.get("x-stainless-retry-count") == "0"
    assert "fabricated-token" not in repr(client) and "fabricated-token" not in str(client), "T5: the token is not in the client's text form"
    assert ai.model_name() == ai.APPROVED_MODEL == "claude-sonnet-5-5", "T13: the model on this route is untouched"


def test_stray_sdk_credentials_in_the_environment_add_no_authorization_header_on_the_publish_route(monkeypatch):
    """A leftover key, bearer token or custom-header variable on the host must not put a second credential beside x-api-key."""
    _publish_route(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "stray-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray-bearer")
    # the SDK reads this variable itself; the names are cased differently from the app's own on purpose (the harder case)
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "authorization: Bearer stray-header\nX-API-KEY: stray-header-key\nX-C4-Env-Probe: env-probe-value")
    assert ai.using_user_key() and not ai.using_own_key(), "the publish route still wins"
    request = _built_request(ai._client())
    assert request.headers.get("x-c4-env-probe") == "env-probe-value", "the variable WAS read by the SDK, so the next lines are not vacuous"
    assert request.headers.get("x-api-key") == "fabricated-token" and "authorization" not in request.headers
    assert request.headers.get_list("x-api-key") == ["fabricated-token"], "one x-api-key value, the app's"
    assert not [k for k, v in request.headers.items() if "stray" in v], "no stray value reaches any header"


def test_the_publish_constructor_gets_exactly_the_agreed_arguments(monkeypatch):
    import anthropic
    built = []

    class _Recorder:
        def __init__(self, **kw):
            built.append(kw)
    monkeypatch.setattr(anthropic, "Anthropic", _Recorder)
    _publish_route(monkeypatch)
    ai._client()
    assert len(built) == 1 and set(built[0]) == {"base_url", "api_key", "default_headers", "max_retries"}
    assert built[0]["base_url"] == "https://proxy.invalid" and built[0]["api_key"] == "fabricated-token" and built[0]["max_retries"] == 0
    headers = built[0]["default_headers"]
    assert set(headers) == {"anthropic-version", "x-api-key", "Authorization"}
    assert headers["anthropic-version"] == "2023-06-01" and headers["x-api-key"] == "fabricated-token"
    assert headers["Authorization"] is anthropic.omit and isinstance(headers["Authorization"], anthropic.Omit), "removed, not blanked"


def test_an_sdk_that_cannot_remove_the_bearer_builds_no_publish_client_and_sends_nothing(monkeypatch):
    """Fail closed: without `omit` there is no client at all on this route - never a quiet return to the bearer header."""
    import anthropic
    built = []
    real = anthropic.Anthropic

    class _Counting(real):
        def __init__(self, **kw):
            built.append(sorted(kw))
            super().__init__(**kw)
    monkeypatch.setattr(anthropic, "Anthropic", _Counting)
    monkeypatch.delattr(anthropic, "omit")
    _publish_route(monkeypatch)
    with pytest.raises(ImportError) as caught:
        ai._client()
    assert built == [] and ai.failure_kind(caught.value) == "unexpected", "a closed kind; no new sentence"
    with pytest.raises(ImportError):
        ai._ask("x")
    assert built == []
    # the other two routes do not need `omit` and still build
    monkeypatch.delenv("MMAI_AI_URL_VAR"); monkeypatch.delenv("MMAI_AI_TOKEN_VAR")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key")
    assert ai.using_own_key() and ai._client().api_key == "fabricated-key"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://sandbox.invalid")
    assert not ai.using_own_key() and ai._client().max_retries == 0
    assert built == [["api_key", "max_retries"], ["default_headers", "max_retries"]]


def test_the_own_key_and_sandbox_routes_are_left_exactly_as_they_were(monkeypatch):
    """v5 touches the publish constructor only. The other two constructors get the same arguments as before, add and remove no
    header of their own beyond what they already did, and their source lines are byte for byte the v4 ones."""
    import anthropic
    for var in SDK_ENV_EXTRAS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fabricated-key")
    assert ai.using_own_key() and ai.ai_mode() == "own-key"
    own = ai._client()
    assert isinstance(own, anthropic.Anthropic) and own.api_key == "fabricated-key" and own.max_retries == 0
    assert dict(own._custom_headers) == {}, "the app adds or removes no header on the own-key route"
    request = _built_request(own)
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    carrying = sorted(k.lower() for k, v in request.headers.items() if "fabricated-key" in v)
    assert len(carrying) == 1 and carrying[0] in CREDENTIAL_HEADERS, "whichever credential header the SDK itself chooses, and only that one"
    assert request.headers.get("anthropic-version") == anthropic.Anthropic(api_key="fabricated-other", max_retries=0).default_headers["anthropic-version"], \
        "the SDK's own version header, not one of ours"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://sandbox.invalid")
    assert not ai.using_own_key() and ai.ai_mode() == "sandbox"
    sandbox = ai._client()
    assert dict(sandbox._custom_headers) == {"anthropic-version": "2023-06-01"} and sandbox.max_retries == 0
    request = _built_request(sandbox)
    assert str(request.url) == "https://sandbox.invalid/v1/messages" and request.headers.get("anthropic-version") == "2023-06-01"
    src = inspect.getsource(ai._client)
    assert '        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=SDK_MAX_RETRIES)\n' in src
    assert '    return Anthropic(default_headers={"anthropic-version": "2023-06-01"}, max_retries=SDK_MAX_RETRIES)\n' in src
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert code.count("omit") == 2 and code.count("x-api-key") == 1, "one import and one use of omit, one x-api-key: the publish branch only"


# ---------------------------------------------------------------- (2) the native schema path
def test_ask_signature_is_unchanged_and_the_limits_stand():
    params = inspect.signature(ai._ask).parameters
    assert list(params) == ["prompt", "model", "max_tokens", "system"]
    assert [p.default for p in params.values()] == [inspect.Parameter.empty, None, 1500, ""]
    assert ai.OUTPUT_TOKEN_CEILING == 8000
    assert ai.REVIEW_CONTRACT == "2026-11-11.step-free-payload"   # Recorded access value/provenance delivered to both model descriptors.
    assert inspect.signature(ai._ask_bounded).parameters["schema"].default is None
    assert ai._SCHEMA.get() is None


def test_review_call_sends_the_schema_as_native_output_config_and_the_validator_still_decides(db, monkeypatch):
    _, run = _run(db)
    calls = []
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, _answer_with(_full)))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=1)
    assert len(calls) == 1 and out["calls"] == 1 and out["failed"] == 0
    kw = calls[0]
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": ai.REVIEW_BATCH_SCHEMA}}
    schema = kw["output_config"]["format"]["schema"]
    assert schema is not ai.REVIEW_BATCH_SCHEMA, "the module constant is never handed to the SDK itself"
    assert schema["type"] == "object" and schema["required"] == ["groups"] and schema["additionalProperties"] is False
    assert schema["properties"]["groups"] == {"type": "array", "items": ai.REVIEW_GROUP_SCHEMA}
    assert set(kw) == {"model", "max_tokens", "thinking", "system", "messages", "output_config"}, "no beta header, tool or other route"
    assert kw["model"] == ai.model_name() and kw["model"] in (ai.APPROVED_MODEL, ai.SANDBOX_MODEL)
    assert kw["max_tokens"] <= ai.OUTPUT_TOKEN_CEILING
    # the wrapped object reply ({"groups": [...]}) went through the unchanged validator and the groups were reviewed
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "ok" for g in gs)
    assert ai._SCHEMA.get() is None, "the schema is in force only for the duration of the bounded call"


def test_first_turn_prompt_has_no_repair_text_and_ends_with_the_packet(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, _full))
    res = ai.assess_groups_and_apartments(groups, meter := {"asks": 0})
    assert len(calls) == 1 and meter == {"asks": 1} and all(r["valid"] for r in res)
    p = calls[0]["prompt"]
    assert ai.REVIEW_REPAIR_HEAD not in p and "REPAIR TURN" not in p
    assert p.startswith("You are reviewing proposed groups of housemates") and ai.REVIEW_RULES_TEXT in p
    assert _descriptor(p) == json.loads(json.dumps(ai.review_packet(groups), default=str))
    assert calls[0]["schema"] == ai.REVIEW_BATCH_SCHEMA and calls[0]["max_tokens"] == min(ai.review_token_budget(groups), ai.OUTPUT_TOKEN_CEILING)
    # v2: the prompt asks for the same wrapper the schema describes, and no longer for a bare list
    assert 'Return ONLY JSON: one object {"groups": [...]} whose "groups" list has exactly one object per group index' in p
    assert "Return ONLY JSON: a list" not in p and p.count("Return ONLY JSON") == 1


def test_review_token_budget_is_pure_member_aware_and_never_above_the_ceiling(db, monkeypatch):
    def g(people, apartments):
        return {"person_refs": {str(i): f"P{i}" for i in range(people)}, "apartments": [{}] * apartments}
    budget = ai.review_token_budget
    assert budget([]) == 1200
    assert budget([g(2, 3)]) == 1200 + 500 + 360 + 660
    # The higher estimate may now split shapes that exhausted the old allowance.
    for n_groups, n_apts in ((1, 1), (3, 2), (6, 1), (6, 3)):
        assert budget([g(2, n_apts)] * n_groups) == min(8000, 1200 + 860 * n_groups + 220 * n_groups * n_apts)
    # more people at the same single apartment get more room (a large group at one apartment is not underbudgeted) ...
    assert [budget([g(n, 1)]) for n in (1, 2, 5, 7)] == [2100, 2280, 2820, 3180]
    # Nothing is ever asked above the ceiling.
    assert ai.review_token_estimate([g(5, 1)] * 6) == 10920 and budget([g(5, 1)] * 6) == ai.OUTPUT_TOKEN_CEILING == 8000
    assert budget([g(7, 9)] * 50) == ai.OUTPUT_TOKEN_CEILING
    # people are the person refs (enquirers and listed party members alike); a packet without refs falls back to its member list
    assert budget([{"members_for_ai": [{}] * 4, "apartments": [{}]}]) == budget([g(4, 1)])
    assert budget([{"person_refs": {"7": "A", "7m1": "B", "7m2": "C"}, "members_for_ai": [{}], "apartments": []}]) == 1200 + 500 + 540
    # pure: the same answer every time, the packets untouched, and no model call
    groups = _packets(db, monkeypatch)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("budget made a model call")))
    monkeypatch.setattr(ai, "_client", lambda: (_ for _ in ()).throw(AssertionError("budget built a client")))
    before = copy.deepcopy(groups)
    assert budget(groups) == budget(groups) == 1200 + 500 * len(groups) + 180 * sum(len(x["person_refs"]) for x in groups) + 220 * sum(len(x["apartments"]) for x in groups)
    assert groups == before and list(inspect.signature(budget).parameters) == ["groups"]


def test_review_token_estimate_is_the_shared_uncapped_formula_and_the_budget_is_its_cap(db, monkeypatch):
    def g(people, apartments):
        return {"person_refs": {str(i): f"P{i}" for i in range(people)}, "apartments": [{}] * apartments}
    estimate, budget = ai.review_token_estimate, ai.review_token_budget
    # Only the uncapped estimate can tell the queue to split.
    six = [g(5, 1)] * 6
    assert estimate(six) == 10920 > ai.OUTPUT_TOKEN_CEILING and budget(six) == 8000 == ai.OUTPUT_TOKEN_CEILING
    assert estimate(six[:4]) == 1200 + 4 * (500 + 900 + 220) == 7680 == budget(six[:4])
    assert estimate([g(7, 9)] * 50) == 1200 + 50 * (500 + 1260 + 1980) == 188200, "the estimate is never capped"
    assert estimate([]) == 1200 and estimate([g(2, 3)]) == 2720
    # budget == min(ceiling, estimate) for every shape, below, at and above the ceiling; same people / apartment definitions
    shapes = [[g(p, a)] * n for p in (1, 2, 3, 5, 7) for a in (0, 1, 3, 9) for n in (1, 2, 6, 12)]
    shapes += [[{"members_for_ai": [{}] * 4, "apartments": [{}]}], [{"person_refs": {"7": "A", "7m1": "B"}, "members_for_ai": [{}], "apartments": []}]]
    assert all(budget(x) == min(ai.OUTPUT_TOKEN_CEILING, estimate(x)) for x in shapes)
    assert any(estimate(x) > ai.OUTPUT_TOKEN_CEILING for x in shapes) and any(estimate(x) < ai.OUTPUT_TOKEN_CEILING for x in shapes)
    assert estimate([{"members_for_ai": [{}] * 4, "apartments": [{}]}]) == estimate([g(4, 1)])
    # pure, and the real packets give the same number through both
    groups = _packets(db, monkeypatch)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("estimate made a model call")))
    monkeypatch.setattr(ai, "_client", lambda: (_ for _ in ()).throw(AssertionError("estimate built a client")))
    before = copy.deepcopy(groups)
    assert estimate(groups) == estimate(groups) == budget(groups) < ai.OUTPUT_TOKEN_CEILING and groups == before
    assert list(inspect.signature(estimate).parameters) == ["groups"]
    # the request itself is made at the capped budget, never at the raw estimate
    calls = []
    big = [copy.deepcopy(x) for x in groups] * 12
    assert estimate(big) > ai.OUTPUT_TOKEN_CEILING
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, ai.AIUnavailable("stop here")))
    with pytest.raises(ai.AIUnavailable):
        ai.assess_groups_and_apartments(big, {"asks": 0})
    assert len(calls) == 1 and calls[0]["max_tokens"] == ai.OUTPUT_TOKEN_CEILING


def test_only_the_review_asks_for_a_schema_and_plain_calls_send_none(monkeypatch):
    calls = []
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, lambda kw: "Australia"))
    assert ai._ask("x") == "Australia"
    assert ai._ask_bounded(lambda compact: "p", 500, "introduction draft") == "Australia"
    assert len(calls) == 2 and all("output_config" not in kw for kw in calls)
    # a schema applies to the calls inside its block only, and None inside an outer block means none (nothing is inherited)
    with ai._output_schema(ai.REVIEW_BATCH_SCHEMA):
        ai._ask("x")
        with ai._output_schema(None):
            ai._ask("x")
        assert ai._ask_bounded(lambda compact: "p", 500, "plain") == "Australia"
        ai._ask("x")
    ai._ask("x")
    assert ["output_config" in kw for kw in calls[2:]] == [True, False, False, True, False]
    # the API needs an object at the root: anything else is refused before any call is made
    n = len(calls)
    for bad in ({"type": "array", "items": ai.REVIEW_GROUP_SCHEMA}, [], "object", {"properties": {}}):
        with pytest.raises(ValueError):
            ai._ask_bounded(lambda compact: "p", 500, "x", schema=bad)
    assert len(calls) == n and ai._SCHEMA.get() is None


def test_the_schema_is_reset_after_a_failure_too(monkeypatch):
    def boom(prompt, model=None, max_tokens=1500, system=""):
        assert ai._SCHEMA.get() == ai.REVIEW_BATCH_SCHEMA
        raise ai.AIUnavailable("simulated outage")
    monkeypatch.setattr(ai, "_ask", boom)
    with pytest.raises(ai.AIUnavailable):
        ai._ask_bounded(lambda compact: "p", 500, "AI review", schema=ai.REVIEW_BATCH_SCHEMA)
    assert ai._SCHEMA.get() is None


# keywords the native format supports (Anthropic structured-outputs documentation, read 10 Nov 2026); numeric, length and item-count
# constraints are not supported and must not appear
_SUPPORTED = {"type", "properties", "required", "additionalProperties", "items", "enum"}


def _walk(schema, path="$"):
    yield path, schema
    for k, v in (schema.get("properties") or {}).items():
        yield from _walk(v, f"{path}.{k}")
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"], path + "[]")


def _conforms(schema, value) -> bool:
    """A small checker for exactly the keywords the schema uses (the suite has no JSON-schema library)."""
    t = schema["type"]
    if "enum" in schema and value not in schema["enum"]:
        return False
    if t == "object":
        if not isinstance(value, dict) or set(schema.get("required", [])) - set(value) or set(value) - set(schema["properties"]):
            return False
        return all(_conforms(schema["properties"][k], v) for k, v in value.items())
    if t == "array":
        return isinstance(value, list) and all(_conforms(schema["items"], v) for v in value)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise AssertionError(f"unexpected type {t}")


def test_schema_uses_only_supported_keywords_and_states_shape_not_meaning(db, monkeypatch):
    optional = 0
    for path, node in _walk(ai.REVIEW_BATCH_SCHEMA):
        assert isinstance(node, dict) and set(node) <= _SUPPORTED, path
        assert node["type"] in ("object", "array", "string", "integer", "number"), path
        if node["type"] == "object":
            assert node["additionalProperties"] is False and set(node["required"]) <= set(node["properties"]) and node["properties"], path
            optional += len(set(node["properties"]) - set(node["required"]))
        if node["type"] == "array":
            assert isinstance(node["items"], dict), path
        if "enum" in node:
            assert node["type"] == "string" and all(isinstance(x, str) for x in node["enum"]), path
    assert optional == 2 <= 24, "the format's limit on optional properties is 24; only replace_reasons' kind and compared_with are optional"
    g = ai.REVIEW_GROUP_SCHEMA
    assert g["properties"]["verdict"]["enum"] == list(ai.VERDICTS)
    assert g["required"] == list(g["properties"]) == ["index", "findings", "rating", "rationale", "verdict", "replace_refs", "replace_reasons", "apartments"]
    json.dumps(ai.REVIEW_BATCH_SCHEMA)

    groups = _packets(db, monkeypatch)
    desc = ai.review_packet(groups)
    good = {"groups": [_full(d) for d in desc]}
    assert _conforms(ai.REVIEW_BATCH_SCHEMA, good) and all(r["valid"] for r in ai.parse_review_batch(good, groups))
    # shape the schema refuses
    assert not _conforms(ai.REVIEW_BATCH_SCHEMA, good["groups"])                                            # a bare list at the root
    assert not _conforms(ai.REVIEW_BATCH_SCHEMA, {"groups": [{**good["groups"][0], "confidence": "high"}]})  # an extra key
    assert not _conforms(ai.REVIEW_BATCH_SCHEMA, {"groups": [{**good["groups"][0], "verdict": "accept"}]})   # a verdict outside the three
    assert not _conforms(ai.REVIEW_BATCH_SCHEMA, {"groups": [{k: v for k, v in good["groups"][0].items() if k != "verdict"}]})
    # MEANING the schema cannot state: each reply below has a perfectly valid shape and is still refused by the unchanged validator
    d0 = desc[0]
    for label, item in {
        "rating out of range": {**_full(d0), "rating": 42},
        "fabricated fact id": {**_full(d0), "findings": [{"fact_ids": ["U9.made_up"], "note": "fine", "kind": "evidence"}]},
        "apartment not ranked": {**_full(d0), "apartments": []},
        "apartment never offered": {**_full(d0), "apartments": _full(d0)["apartments"] + [{"apartment_id": 987654, "reason": "r"}]},
        "revise without anyone to replace": {**_full(d0), "verdict": "revise"},
        "approve with someone to replace": {**_full(d0), "replace_refs": [groups[0]["members_for_ai"][0]["ref"]]},
        "no evidence at all": {**_full(d0), "findings": []},
        "empty rationale": {**_full(d0), "rationale": " "},
    }.items():
        reply = {"groups": [item] + [_full(d) for d in desc[1:]]}
        assert _conforms(ai.REVIEW_BATCH_SCHEMA, reply), label
        res = ai.parse_review_batch(reply, groups)
        assert res[0]["valid"] is False and res[0]["verdict"] is None and res[0]["rating"] is None, label
        assert all(r["valid"] for r in res[1:]), label


# ---------------------------------------------------------------- (3) a rejected structured-output request
def test_schema_failure_is_a_closed_kind_with_a_closed_sentence():
    e = ai.AISchemaUnsupported()
    assert isinstance(e, ai.AINoRetry) and isinstance(e, ai.AIUnavailable)
    assert e.kind == "schema" and e.code == "schema" and "schema" in ai.ERROR_KINDS and ai.failure_kind(e) == "schema"
    text = ai.FAILURE_TEXT["schema"]
    assert ai.failure_text(e) == ai.failure_text(e, "brief") == ai.failure_text(e, "introduction") == text
    assert text in ai.ALL_FAILURE_SENTENCES and ai_queue.is_closed_failure_text(text)
    assert text[0].isupper() and text.endswith(".") and "{" not in text and "%" not in text
    assert "no automatic model change or free-text fallback was made" in text
    assert not re.search(r"\b(?:GP|[ABCGHIMPT])\d{1,2}\b", text), "staff text carries no Register code"
    with pytest.raises(TypeError):
        ai.AISchemaUnsupported("the SDK said " + MARKER)   # takes no message, so nobody can put the provider's words in it
    assert str(e) == "AI structured-output request rejected"
    # the queue treats it as terminal and words it from the closed code; it is not the credentials category
    f = ai_queue._failure(e)
    assert f == text and f.kind == "schema" and f.code == "schema"
    assert ai_queue.failure_category(e) == ai_queue.STATE_ERROR_PROVIDER


@pytest.mark.parametrize("make", [lambda: TypeError("create() got an unexpected keyword argument 'output_config' " + MARKER),
                                  lambda: _bad_request("output_config.format: schema is not supported for this model " + MARKER)],
                         ids=["TypeError", "BadRequestError"])
def test_rejected_schema_call_is_one_call_same_model_no_fallback(db, monkeypatch, caplog, make):
    caplog.set_level(logging.DEBUG)
    groups = _packets(db, monkeypatch)
    calls = []

    def reject(kw):
        raise make()
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, reject))
    meter = {"asks": 0}
    with pytest.raises(ai.AISchemaUnsupported) as ei:
        ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 1, "no second create(): not without the schema, not with another model, not compactly"
    assert calls[0]["model"] == ai.model_name() and "output_config" in calls[0]
    assert meter == {"asks": 1}, "no compact retry and no repair turn was spent"
    e = ei.value
    assert e.kind == "schema" and e.code == "schema" and ai.failure_text(e) == ai.FAILURE_TEXT["schema"]
    assert MARKER not in str(e) and MARKER not in repr(e.args) and MARKER not in caplog.text and "output_config" not in caplog.text
    assert "AI call failed: schema" in caplog.text and "AI call failed: provider" not in caplog.text
    assert ai._SCHEMA.get() is None


def test_the_same_errors_without_a_schema_and_other_errors_with_one_stay_provider_failures(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    calls = []
    for make in (lambda: TypeError("x " + MARKER), lambda: _bad_request("x " + MARKER)):
        def reject(kw, make=make):
            raise make()
        monkeypatch.setattr(ai, "_client", _sdk_client(calls, reject))
        with pytest.raises(ai.AIUnavailable) as ei:
            ai._ask("no schema here")
        assert not isinstance(ei.value, ai.AISchemaUnsupported) and ei.value.kind == "provider" and "output_config" not in calls[-1]

    def outage(kw):
        raise RuntimeError("Connection error " + MARKER)
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, outage))
    with pytest.raises(ai.AIUnavailable) as ei:
        ai._ask_bounded(lambda compact: "p", 500, "AI review", schema=ai.REVIEW_BATCH_SCHEMA)
    assert not isinstance(ei.value, ai.AINoRetry) and ei.value.kind == "provider" and "output_config" in calls[-1]
    assert MARKER not in caplog.text and "AI call failed: schema" not in caplog.text


def test_rejected_schema_holds_the_groups_with_the_closed_sentence_and_the_queue_does_not_retry(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []

    def reject(kw):
        raise _bad_request("structured outputs are not available " + MARKER)
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, reject))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 1 and out["calls"] == 1, "three queue attempts were allowed; the terminal failure used one"
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "failed" and g["ai_error"] == ai.FAILURE_TEXT["schema"] for g in gs)
    assert not any(g["ai_status"] == "ok" or g.get("review_verdict") == "approve" for g in gs), "nothing was approved without a review"
    for g in gs:
        assert ai_queue.is_ready(db, g)["ready"] is False
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_a_free_text_reply_is_never_accepted_in_place_of_the_structured_one(db, monkeypatch):
    """No permissive parsing came with the schema: prose around nothing, or a status word alone, is unusable exactly as before."""
    groups = _packets(db, monkeypatch)
    for reply in ("All six groups look fine to me. Approve.", '{"groups": "approve"}', '{"status": "approved"}', '{"groups": []}'):
        calls = []
        monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, reply))
        with pytest.raises(ai.AIUnusableReply):
            ai.assess_groups_and_apartments(groups, meter := {"asks": 0})
        assert len(calls) == 2, "v2: one whole-batch repair call, answered the same way, and then nothing more"
        assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": 0, "repair_error": "validation"}


# ---------------------------------------------------------------- (4) one bounded repair turn
def test_one_repair_turn_reasks_only_the_invalid_groups_with_the_validator_problems(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    bad_at = {1, 3}
    calls = []
    first = lambda g: _bad_rating(g) if g["index"] in bad_at else _full(g)   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and meter == {"asks": 2, "repair": True, "repair_scope": "groups", "repair_groups": 2, "repair_fixed": 2}
    assert not any("repair_error" in r for r in res)
    assert [r["index"] for r in res] == list(range(len(groups))) and all(r["valid"] and r["verdict"] == "approve" for r in res)
    # the repair call: the two invalid groups only, re-numbered from 0, the same native format, the same budget formula
    p2 = calls[1]["prompt"]
    sub = [groups[i] for i in sorted(bad_at)]
    assert _descriptor(p2) == json.loads(json.dumps(ai.review_packet(sub), default=str))
    assert [d["index"] for d in _descriptor(p2)] == [0, 1]
    assert calls[1]["schema"] == ai.REVIEW_BATCH_SCHEMA and calls[1]["max_tokens"] == min(ai.review_token_budget(sub), ai.OUTPUT_TOKEN_CEILING)
    assert calls[1]["max_tokens"] < calls[0]["max_tokens"]
    assert ai.REVIEW_REPAIR_HEAD in p2 and "This is the only correction turn." in p2 and "Your previous reply was cut off" not in p2
    assert 'Return one object {"groups": [...]} with a COMPLETE corrected review object' in p2 and ai.REVIEW_BATCH_REPAIR_TEXT not in p2
    assert "Group index 0: rating 42 is not a number in 1-10" in p2 and "Group index 1: rating 42 is not a number in 1-10" in p2
    assert "Group index 2:" not in p2
    assert p2.index(ai.REVIEW_REPAIR_HEAD) > p2.index(ai.REVIEW_RULES_TEXT), "the rules and the full instructions are sent again, unchanged"
    assert p2.startswith(calls[0]["prompt"][:calls[0]["prompt"].index("Response size limit: at most")])
    # replacements sit at their original places and describe the right group; the groups that were valid are untouched
    for i in sorted(bad_at):
        assert [a["apartment_id"] for a in res[i]["apartments"]] == [int(a["apartment_id"]) for a in groups[i]["apartments"]]
        assert res[i]["coverage"] == ai.parse_review_batch({"groups": [_full(d) for d in ai.review_packet(groups)]}, groups)[i]["coverage"]


def test_repair_replacement_must_pass_the_same_validator_and_there_is_no_second_repair(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []
    first = lambda g: _bad_rating(g) if g["index"] in (0, 2) else _full(g)   # noqa: E731
    # the repair reply fixes sub-index 0 and is still wrong for sub-index 1 (an approval resting on a fabricated fact)
    second = lambda g: _full(g) if g["index"] == 0 else {**_full(g), "findings": [{"fact_ids": ["U9.made_up"], "note": "fine", "kind": "evidence"}]}   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first, second, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2, "exactly one repair call; the still-invalid group is not asked a third time"
    assert meter == {"asks": 2, "repair": True, "repair_scope": "groups", "repair_groups": 2, "repair_fixed": 1}
    assert res[0]["valid"] and res[0]["index"] == 0
    assert "repair_error" not in res[2], "the repair call itself worked; its replacement was simply still invalid"
    assert res[2]["valid"] is False and res[2]["verdict"] is None and res[2]["rating"] is None and res[2]["index"] == 2
    assert "rating 42" in res[2]["invalid_reason"], "the first result stands as it was; the failed replacement is discarded whole"
    assert res[2]["invalid_code"] == "review_unusable" and ai.persisted_invalid_reason(res[2]).startswith(ai.FAILURE_TEXT["review_unusable"])
    # the same meter never carries a second repair, whatever the next reply looks like
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, _bad_rating, _full))
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 1 and meter["asks"] == 3 and not any(r["valid"] for r in res)


def test_missing_and_duplicate_reviews_are_repaired_too_and_a_wholly_invalid_batch_is_reasked_once(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []

    def first(prompt, model=None, max_tokens=1500, system=""):
        calls.append(prompt)
        desc = _descriptor(prompt)
        if len(calls) == 1:   # group 0 left out, group 1 answered twice
            return json.dumps({"groups": [_full(desc[1])] + [_full(d) for d in desc[1:]]})
        return json.dumps({"groups": [_full(d) for d in desc]})
    monkeypatch.setattr(ai, "_ask", first)
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and meter["repair_groups"] == 2 and meter["repair_fixed"] == 2 and all(r["valid"] for r in res)
    assert "Group index 0: review missing for this group" in calls[1] and "Group index 1: 2 review objects for one group" in calls[1]
    # every group invalid: still one repair call (for all of them) and no more
    calls2 = []
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls2, _bad_rating))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls2) == 2 and meter == {"asks": 2, "repair": True, "repair_scope": "groups", "repair_groups": len(groups), "repair_fixed": 0}
    assert not any(r["valid"] for r in res) and len(_descriptor(calls2[1]["prompt"])) == len(groups)


@pytest.mark.parametrize("failure, kind", [
    (ai.AIUnavailable("simulated outage " + MARKER), "provider"),
    (ai.AIOutputTruncated("cut off", max_tokens=100), "size_limit"),
    (ai.AISchemaUnsupported(), "schema"),
    (KeyError(MARKER), "unexpected"),
    ("no JSON in this reply at all", "validation"),
    ('{"groups": [{"index": 99}]}', "validation"),
], ids=["outage", "cut-off", "schema", "unexpected", "no-json", "wrong-index"])
def test_any_failure_of_the_repair_call_keeps_the_first_results(db, monkeypatch, caplog, failure, kind):
    caplog.set_level(logging.DEBUG)
    groups = _packets(db, monkeypatch)
    calls = []
    first = lambda g: _bad_rating(g) if g["index"] == 1 else _full(g)   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first))
    expected = ai.assess_groups_and_apartments(groups, {"asks": 0, "repair": True})   # the first results alone (repair suppressed)
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first, failure, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)   # does not raise
    assert len(calls) == 2, "one repair call, no compact retry of its own, nothing after it"
    # the valid subset is exactly the first results; the invalid one is the first result plus the closed kind of the repair failure
    assert res[1]["valid"] is False and sum(r["valid"] for r in res) == len(groups) - 1
    assert [r for r in res if r["valid"]] == [r for r in expected if r["valid"]] and not any("repair_error" in r for r in res if r["valid"])
    assert res[1] == {**expected[1], "repair_error": kind} and "repair_error" not in expected[1]
    assert meter == {"asks": 2, "repair": True, "repair_scope": "groups", "repair_groups": 1, "repair_fixed": 0, "repair_error": kind}
    assert kind in ai.ERROR_KINDS and MARKER not in caplog.text and f"AI review repair turn not used: {kind}" in caplog.text


# ---------------------------------------------------------------- v2: one bounded WHOLE-batch repair
def _five_good_and_a_stray(prompt_groups):
    """A reply that would be five usable reviews and one object whose index is outside the batch: unusable as a whole."""
    return json.dumps({"groups": [_full(d) for d in prompt_groups[:-1]] + [{**_full(prompt_groups[-1]), "index": 99, "rationale": MARKER}]})


UNUSABLE_WHOLE = {
    "no-json": lambda desc: "I cannot produce that. " + MARKER,
    "malformed-json": lambda desc: '{"groups": [{"index": 0, "rationale": "' + MARKER,
    "not-a-list": lambda desc: json.dumps({"groups": MARKER}),
    "empty": lambda desc: json.dumps({"groups": []}),
    "stray-index": _five_good_and_a_stray,
    "fractional-index": lambda desc: json.dumps({"groups": [{**_full(d), "index": d["index"] + 0.5} for d in desc]}),
}


def _whole_then(calls, first_kind, *later):
    """An `_ask` double: the first reply is unusable as a whole (by kind); later turns follow _scripted_ask's rules."""
    scripted = _scripted_ask(calls, None, *later) if later else None

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        if not calls:
            calls.append({"prompt": prompt, "max_tokens": max_tokens, "schema": ai._SCHEMA.get(), "model": model})
            return UNUSABLE_WHOLE[first_kind](_descriptor(prompt))
        return scripted(prompt, model, max_tokens, system)
    return _ask


@pytest.mark.parametrize("first_kind", sorted(UNUSABLE_WHOLE))
def test_a_reply_unusable_as_a_whole_gets_one_whole_batch_repair_in_fixed_words(db, monkeypatch, caplog, first_kind):
    caplog.set_level(logging.DEBUG)
    groups = _packets(db, monkeypatch)
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, first_kind, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and all(r["valid"] and r["verdict"] == "approve" for r in res) and [r["index"] for r in res] == list(range(len(groups)))
    assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": len(groups)}
    assert not any("repair_error" in r for r in res)
    p1, p2 = calls[0]["prompt"], calls[1]["prompt"]
    # the WHOLE batch again, with its own indices, the same format, the same budget, no compact wording
    assert _descriptor(p2) == _descriptor(p1) == json.loads(json.dumps(ai.review_packet(groups), default=str))
    assert calls[1]["schema"] == ai.REVIEW_BATCH_SCHEMA and calls[1]["max_tokens"] == calls[0]["max_tokens"] == ai.review_token_budget(groups)
    assert "Your previous reply was cut off" not in p2
    assert calls[0]["model"] is None and calls[1]["model"] is None, "no model is named on either call: both use the configured one"
    # fixed words only: the prompt is the first prompt plus the constant block, so nothing of the first reply can be in it
    head, packet = p1[:p1.rindex("\n\n") + 2], p1[p1.rindex("\n\n") + 2:]
    assert p2 == head + ai.REVIEW_BATCH_REPAIR_TEXT + packet
    assert MARKER not in p2 and "99" not in ai.REVIEW_BATCH_REPAIR_TEXT and ai.REVIEW_REPAIR_HEAD not in p2 and "Group index" not in p2
    assert 'one object {"groups": [...]}' in ai.REVIEW_BATCH_REPAIR_TEXT and "none of it was kept" in ai.REVIEW_BATCH_REPAIR_TEXT
    assert ai.REVIEW_BATCH_REPAIR_TEXT.endswith("This is the only correction turn.\n\n") and "\n\n" not in ai.REVIEW_BATCH_REPAIR_TEXT[:-2]
    assert MARKER not in caplog.text


def test_whole_batch_repair_reply_is_judged_by_the_same_validator_and_its_valid_subset_is_kept(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []
    # the repair reply lines up as a whole but two of its reviews are wrong (a rating out of range, an invented fact id)
    second = lambda g: (_bad_rating(g) if g["index"] == 1 else                                                                    # noqa: E731
                        {**_full(g), "findings": [{"fact_ids": ["U9.made_up"], "note": "fine", "kind": "evidence"}]} if g["index"] == 4 else _full(g))
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "stray-index", second, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2, "the two still-invalid groups are not asked again: the one repair turn has been used"
    assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": len(groups) - 2}
    assert [r["valid"] for r in res] == [i not in (1, 4) for i in range(len(groups))]
    assert "rating 42" in res[1]["invalid_reason"] and res[1]["verdict"] is None and res[4]["rating"] is None
    assert res == ai.parse_review_batch({"groups": [second(d) for d in ai.review_packet(groups)]}, groups), "exactly what the unchanged validator makes of the second reply"
    assert not any("repair_error" in r for r in res)


def test_nothing_of_an_unusable_first_reply_is_trusted_even_when_most_of_it_looked_fine(db, monkeypatch):
    """Five of six objects in the first reply were complete approvals with good indices; the sixth had index 99. The batch does
    not line up, so none of the five is kept: every result comes from the repair reply (here: all 'hold')."""
    groups = _packets(db, monkeypatch)
    calls = []
    hold = lambda g: {**_full(g), "verdict": "hold", "rating": 3}   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "stray-index", hold))
    res = ai.assess_groups_and_apartments(groups, {"asks": 0})
    assert len(calls) == 2 and all(r["valid"] and r["verdict"] == "hold" and r["rating"] == 3 for r in res)
    # and when the repair fails there is no result at all - not the five that looked fine, not a guess for the sixth
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "stray-index", ai.AIUnavailable("outage")))
    with pytest.raises(ai.AIUnusableReply) as ei:
        ai.assess_groups_and_apartments(groups, {"asks": 0})
    assert ei.value.code == "review_shape" and len(calls) == 2


@pytest.mark.parametrize("failure, kind", [
    (ai.AIUnavailable("simulated outage " + MARKER), "provider"),
    (ai.AIUnavailable("mentions credentials and setup " + MARKER, kind="provider", code="provider"), "provider"),   # words never decide
    (KeyError(MARKER), "unexpected"),
    ("still no JSON " + MARKER, "validation"),
    ('{"groups": [{"index": 99}]}', "validation"),
], ids=["outage", "outage-worded-like-credentials", "unexpected", "no-json", "wrong-index"])
@pytest.mark.parametrize("first_kind, code, message", [
    ("no-json", None, "AI returned no JSON"),
    ("not-a-list", "review_shape", "AI review was not a list of group objects"),
])
def test_a_failed_whole_batch_repair_reraises_the_original_failure_unchanged(db, monkeypatch, caplog, first_kind, code, message, failure, kind):
    caplog.set_level(logging.DEBUG)
    groups = _packets(db, monkeypatch)
    # what the same first reply raises with the repair suppressed (the pre-repair behaviour)
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, first_kind, _full))
    with pytest.raises(ai.AIUnusableReply) as before:
        ai.assess_groups_and_apartments(groups, {"asks": 0, "repair": True})
    assert len(calls) == 1
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, first_kind, failure, _full))
    meter = {"asks": 0}
    with pytest.raises(ai.AIUnavailable) as ei:
        ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2, "one repair call, no compact retry of its own, nothing after it"
    e = ei.value
    assert type(e) is type(before.value) is ai.AIUnusableReply and not isinstance(e, ai.AINoRetry), "still an ordinary unusable reply for the queue's own bounded attempts"
    assert str(e) == str(before.value) == message and e.code == before.value.code == code and e.kind == "validation"
    assert ai.failure_text(e) == ai.failure_text(before.value)
    assert e.__cause__ is None and e.__suppress_context__, "the repair call's exception is not chained onto the original"
    assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": 0, "repair_error": kind}
    assert kind in ai.ERROR_KINDS and MARKER not in caplog.text and f"AI review repair turn not used: {kind}" in caplog.text


def _final_failures():
    return [
        (lambda: ai.AISchemaUnsupported(), "schema", ai.FAILURE_TEXT["schema"]),
        (lambda: ai.AIUnavailable(ai.NO_CREDENTIALS_TEXT, kind="credentials", code="credentials"), "credentials", None),
        (lambda: ai.AIUnavailable("anthropic SDK missing", kind="setup", code="setup"), "setup", None),
        (lambda: ai.AINoRetry("compact reply unusable " + MARKER, code="size_limit_compact_unusable"), "size_limit", ai.FAILURE_TEXT["size_limit_compact_unusable"]),
        # v4: a repair reply cut off at its budget, as `_ask` raises it (code 'size_limit', attempts 1)
        (lambda: ai.AIOutputTruncated("AI response was cut off " + MARKER, max_tokens=100), "size_limit", ai.FAILURE_TEXT["size_limit_no_compact"]),
    ]


@pytest.mark.parametrize("n", range(5), ids=["schema", "credentials", "setup", "other-no-retry", "cut-off"])
@pytest.mark.parametrize("first_kind", ["no-json", "stray-index"])
def test_a_whole_batch_repair_that_fails_for_good_raises_that_failure_not_the_unusable_reply(db, monkeypatch, caplog, first_kind, n):
    """v3. There are no first results to fall back on, so the queue acts on what is raised: a rejected output format, missing
    credentials or a missing SDK must stop it, not be dressed as an ordinary unusable reply that it would retry."""
    caplog.set_level(logging.DEBUG)
    make, kind, sentence = _final_failures()[n]
    failure = make()
    groups = _packets(db, monkeypatch)
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, first_kind, failure, _full))
    meter = {"asks": 0}
    with pytest.raises(ai.AIUnavailable) as ei:
        ai.assess_groups_and_apartments(groups, meter)
    e = ei.value
    assert e is failure and len(calls) == 2, "the repair call's own closed exception, as raised; nothing after it"
    assert not isinstance(e, ai.AIUnusableReply) and e.kind == kind == ai.failure_kind(e) and kind in ai.ERROR_KINDS
    assert isinstance(e, ai.AINoRetry) or ai_queue._no_point_retrying(e), "either way the queue does not try again"
    assert sentence is None or ai.failure_text(e) == sentence
    assert e.__context__ is None and e.__cause__ is None, "the first reply's diagnosis (which may quote the reply) is not chained onto it"
    assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": 0, "repair_error": kind}
    assert MARKER not in caplog.text and f"AI review repair turn not used: {kind}" in caplog.text


def test_which_whole_batch_repair_failures_are_final_is_decided_by_type_and_closed_kind_only():
    final = ai._repair_failure_is_final
    assert all(final(make()) for make, _, _ in _final_failures())
    assert final(ai.AIOutputTruncated("cut", max_tokens=1)), "v4: the AINoRetry type contract - a cut-off is terminal here too"
    assert final(ai.AINoRetry("x")) and issubclass(ai.AIOutputTruncated, ai.AINoRetry) and issubclass(ai.AISchemaUnsupported, ai.AINoRetry)
    assert not final(ai.AIUnavailable("credentials missing, setup broken, schema unsupported"))   # generic outage whose words mislead
    assert not final(ai.AIUnusableReply("AI returned no JSON")) and not final(ai.AIUnusableReply("x", code="review_shape"))
    assert not final(KeyError("credentials")) and not final(TypeError("schema")) and not final(ValueError("setup"))


@pytest.mark.parametrize("error", ["TypeError", "BadRequestError"])
def test_through_the_queue_a_schema_rejected_whole_batch_repair_stops_after_one_attempt_with_the_schema_sentence(db, monkeypatch, caplog, error):
    """The real `_ask` at the SDK double: the first call answers with no JSON, the repair call is rejected for its output format."""
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []

    def answer(kw):
        if len(calls) == 1:
            return "no JSON in the first reply " + MARKER
        raise (TypeError("create() got an unexpected keyword argument 'output_config' " + MARKER) if error == "TypeError"
               else _bad_request("output_config.format is not supported for this model " + MARKER))
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, answer))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 2 and out["calls"] == 2 and out["batches"] == 1, "the call and its one repair call; the queue did not start a second attempt"
    assert all("output_config" in kw for kw in calls) and calls[0]["model"] == calls[1]["model"] == ai.model_name()
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "failed" and g["ai_error"] == ai.FAILURE_TEXT["schema"] for g in gs)
    assert all(ai_queue.is_ready(db, g)["ready"] is False for g in gs)
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_the_sentence_for_a_cut_off_repair_reply_is_closed_true_and_separate_from_the_compact_retry_one():
    text = ai.FAILURE_TEXT["size_limit_no_compact"]
    assert text == "The AI response exceeded the output limit; the review did not complete."
    assert "compact" not in text and "retry" not in text, "it claims nothing that did not happen"
    assert text in ai.ALL_FAILURE_SENTENCES and ai_queue.is_closed_failure_text(text) and text != ai.FAILURE_TEXT["size_limit"]
    # the existing sentence and what an ordinary cut-off exception says are untouched
    assert ai.FAILURE_TEXT["size_limit"] == "The AI reply exceeded the response size limit after a compact retry; no further automatic retry - staff decide."
    e = ai.AIOutputTruncated("cut " + MARKER, max_tokens=5, attempts=2)
    assert e.code == "size_limit" and e.kind == "size_limit" and ai.failure_text(e) == ai.FAILURE_TEXT["size_limit"]
    assert ai.AIUnavailable("x", kind="size_limit", code="size_limit_no_compact").code == "size_limit_no_compact", "a registered closed code"


def test_a_cut_off_whole_batch_repair_reply_is_terminal_as_itself_with_no_compact_turn_and_the_truthful_sentence(db, monkeypatch, caplog):
    """v4, the real `_ask` at an SDK double: the first reply has no JSON, the one repair reply stops at max_tokens."""
    caplog.set_level(logging.DEBUG)
    groups = _packets(db, monkeypatch)
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            return _Msg("no JSON in the first reply " + MARKER) if len(calls) == 1 else _Msg('{"groups": [{"index": 0, ' + MARKER, stop_reason="max_tokens")

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(ai, "_client", lambda: _Client())
    meter = {"asks": 0}
    with pytest.raises(ai.AINoRetry) as ei:
        ai.assess_groups_and_apartments(groups, meter)
    e = ei.value
    assert type(e) is ai.AIOutputTruncated and not isinstance(e, ai.AIUnusableReply), "the cut-off itself, not the first reply's parse failure"
    assert e.kind == "size_limit" == ai.failure_kind(e) and e.code == "size_limit_no_compact" and e.attempts == 1
    assert e.max_tokens == calls[1]["max_tokens"] == calls[0]["max_tokens"] == ai.review_token_budget(groups)
    assert ai.failure_text(e) == ai.FAILURE_TEXT["size_limit_no_compact"] != ai.FAILURE_TEXT["size_limit"]
    assert e.__context__ is None and e.__cause__ is None
    # exactly the call and its one repair: no compact turn, no third call, same model and output format on both
    assert len(calls) == 2 and "compact" not in meter
    assert ai.REVIEW_BATCH_REPAIR_TEXT in calls[1]["messages"][0]["content"] and ai.REVIEW_BATCH_REPAIR_TEXT not in calls[0]["messages"][0]["content"]
    assert calls[0]["model"] == calls[1]["model"] == ai.model_name() and calls[0]["output_config"] == calls[1]["output_config"]
    assert meter == {"asks": 2, "repair": True, "repair_scope": "batch", "repair_groups": len(groups), "repair_fixed": 0, "repair_error": "size_limit"}
    assert MARKER not in caplog.text
    # the queue: one attempt, that sentence, nothing retried
    del calls[:]
    res, err, made = ai_queue._assess_batch(groups, attempts=3)
    assert res is None and err == ai.FAILURE_TEXT["size_limit_no_compact"] and len(calls) == 2 and made == 2


def test_through_the_queue_a_cut_off_whole_batch_repair_holds_the_groups_with_the_truthful_sentence_and_no_retry(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            return _Msg("no JSON in the first reply " + MARKER) if len(calls) == 1 else _Msg("[" + MARKER, stop_reason="max_tokens")

    class _Client:
        messages = _Messages()
    monkeypatch.setattr(ai, "_client", lambda: _Client())
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 2 and out["calls"] == 2 and out["batches"] == 1, "the call and its one repair call; no compact turn, no second attempt"
    gs = _groups(db, run["run_id"])
    sinks = _sinks(db)
    assert gs and all(g["ai_status"] != "ready" and ai_queue.is_ready(db, g)["ready"] is False for g in gs), "nothing circulates"
    assert ai.FAILURE_TEXT["size_limit_no_compact"] in sinks and "after a compact retry" not in sinks and ai.FAILURE_TEXT["validation"] not in sinks
    assert MARKER not in sinks and MARKER not in caplog.text


def test_assess_batch_returns_the_final_repair_failure_after_one_attempt_and_retries_the_ordinary_ones(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    for make, kind, sentence in _final_failures():
        calls = []
        monkeypatch.setattr(ai, "_ask", _whole_then(calls, "no-json", make(), _full))
        res, err, made = ai_queue._assess_batch(groups, attempts=3)
        assert res is None and len(calls) == 2 and made == 2, f"{kind}: no second queue attempt"
        assert ai_queue.is_closed_failure_text(err) and (sentence is None or err == sentence) and err != ai.FAILURE_TEXT["validation"]
    # an ordinary outage on the repair call: the original unusable-reply failure, and the queue's own attempts go on (here to success)
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "no-json", ai.AIUnavailable("outage"), _full))
    res, err, made = ai_queue._assess_batch(groups, attempts=3)
    assert err is None and all(r["valid"] for r in res) and len(calls) == 3 and made == 3, "call, failed repair, then the second attempt's call"
    # the same with every later reply unusable: three attempts plus the one repair call, and the unusable-reply sentence
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "no-json", ai.AIUnavailable("outage"), "still no JSON"))
    res, err, made = ai_queue._assess_batch(groups, attempts=3)
    assert res is None and len(calls) == 4 and made == 4 and ai_queue.is_closed_failure_text(err)
    assert err not in (ai.FAILURE_TEXT["schema"],) and sum(ai.REVIEW_BATCH_REPAIR_TEXT in c["prompt"] for c in calls) == 1


def test_the_repair_allowance_is_one_per_meter_whatever_its_scope(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    # a whole-batch repair was used: a later reply on the same meter with one invalid group gets no per-group repair ...
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "no-json", _full))
    meter = {"asks": 0}
    assert all(r["valid"] for r in ai.assess_groups_and_apartments(groups, meter)) and meter["repair_scope"] == "batch"
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, _bad_rating, _full))
    assert not any(r["valid"] for r in ai.assess_groups_and_apartments(groups, meter)) and len(calls) == 1 and meter["asks"] == 3
    # ... and a later unusable reply on that meter gets no second whole-batch repair
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "no-json", _full))
    with pytest.raises(ai.AIUnusableReply):
        ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 1 and meter["asks"] == 4 and meter["repair_scope"] == "batch"
    # a per-group repair was used: a later unusable reply on the same meter gets no whole-batch repair
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, lambda g: _bad_rating(g) if g["index"] == 0 else _full(g), _full))
    meter = {"asks": 0}
    assert all(r["valid"] for r in ai.assess_groups_and_apartments(groups, meter)) and meter["repair_scope"] == "groups" and len(calls) == 2
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "stray-index", _full))
    with pytest.raises(ai.AIUnusableReply):
        ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 1 and meter["asks"] == 3 and meter["repair_scope"] == "groups"


@pytest.mark.parametrize("failure", [ai.AIUnavailable("outage"), ai.AISchemaUnsupported(), ai.AIOutputTruncated("cut", max_tokens=1), KeyError("x")],
                         ids=["outage", "schema", "cut-off-twice", "unexpected"])
def test_only_an_unusable_reply_is_a_whole_batch_repair_case(db, monkeypatch, failure):
    """A call that failed (outage, rejected format, cut off twice, a defect) produced no reply to repair: no repair call."""
    groups = _packets(db, monkeypatch)
    calls = []
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, failure))
    meter = {"asks": 0}
    with pytest.raises(Exception) as ei:
        ai.assess_groups_and_apartments(groups, meter)
    assert type(ei.value) is type(failure) or isinstance(ei.value, ai.AINoRetry)
    assert "repair" not in meter and "repair_scope" not in meter
    assert len(calls) == (2 if isinstance(failure, ai.AIOutputTruncated) else 1), "only the existing compact retry after a cut-off"


def test_through_the_queue_a_whole_batch_repair_is_counted_and_bounded(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []
    monkeypatch.setattr(ai, "_ask", _whole_then(calls, "stray-index", _full))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 2 and out["calls"] == 2 and out["batches"] == 1 and out["failed"] == 0
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "ok" for g in gs)
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_through_the_queue_an_always_unusable_reply_costs_one_repair_call_and_holds_every_group(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, "no JSON here " + MARKER))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    # attempt 1 = the call + its one repair call; attempts 2 and 3 = one call each (the meter's repair allowance is spent)
    assert len(calls) == 4 and out["calls"] == 4, "three bounded queue attempts plus exactly one repair call, all counted"
    assert sum(ai.REVIEW_BATCH_REPAIR_TEXT in c["prompt"] for c in calls) == 1 and ai.REVIEW_BATCH_REPAIR_TEXT in calls[1]["prompt"]
    gs = _groups(db, run["run_id"])
    assert gs and all(g["ai_status"] == "failed" and ai_queue.is_closed_failure_text(g["ai_error"]) for g in gs)
    assert all(ai_queue.is_ready(db, g)["ready"] is False for g in gs)
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_through_the_queue_a_failed_group_repair_keeps_the_valid_groups_and_holds_the_rest(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []
    first = lambda g: _bad_rating(g) if g["index"] in (0, 3) else _full(g)   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first, ai.AIUnavailable("outage " + MARKER), _full))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 2 and out["calls"] == 2 and out["batches"] == 1, "the batch is not re-run: its valid reviews stand"
    gs = _groups(db, run["run_id"])
    assert sum(g["ai_status"] == "ok" for g in gs) == len(gs) - 2 and sum(g["ai_status"] == "failed" for g in gs) == 2
    for g in gs:
        if g["ai_status"] == "failed":
            assert ai_queue.is_closed_failure_text(g["ai_error"]) and ai_queue.is_ready(db, g)["ready"] is False
    assert MARKER not in _sinks(db) and MARKER not in caplog.text


def test_a_compact_attempt_never_gets_a_repair(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []
    cut = ai.AIOutputTruncated("cut off", max_tokens=100)
    first = lambda g: _bad_rating(g) if g["index"] == 1 else _full(g)   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, cut, first, _full))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and meter == {"asks": 2, "compact": True}, "first call + the one compact retry; no repair call"
    assert "Your previous reply was cut off" in calls[1]["prompt"] and ai.REVIEW_REPAIR_HEAD not in calls[1]["prompt"]
    assert res[1]["valid"] is False and sum(r["valid"] for r in res) == len(groups) - 1
    # an unusable compact reply stays terminal, as before
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, cut, "not json"))
    with pytest.raises(ai.AINoRetry) as ei:
        ai.assess_groups_and_apartments(groups, {"asks": 0})
    assert ei.value.code == "size_limit_compact_unusable" and len(calls) == 2


def test_no_repair_when_every_group_is_valid(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, _full))
    meter = {"asks": 0}
    assert all(r["valid"] for r in ai.assess_groups_and_apartments(groups, meter))
    assert len(calls) == 1 and meter == {"asks": 1}
    assert ai.assess_groups_and_apartments([], meter) == [] and len(calls) == 1


def test_repair_call_uses_the_same_model_and_native_format_at_the_client(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []

    def answer(kw):
        desc = _descriptor(kw["messages"][0]["content"])
        return json.dumps({"groups": [(_bad_rating(d) if len(calls) == 1 and d["index"] == 0 else _full(d)) for d in desc]})
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, answer))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and all(r["valid"] for r in res) and meter["repair_fixed"] == 1
    assert calls[0]["model"] == calls[1]["model"] == ai.model_name()
    assert calls[0]["output_config"] == calls[1]["output_config"] == {"format": {"type": "json_schema", "schema": ai.REVIEW_BATCH_SCHEMA}}
    assert calls[1]["max_tokens"] <= calls[0]["max_tokens"] <= ai.OUTPUT_TOKEN_CEILING
    assert calls[0]["thinking"] == calls[1]["thinking"] and calls[0]["system"] == calls[1]["system"]


def test_whole_batch_repair_call_uses_the_same_model_and_native_format_at_the_client(db, monkeypatch):
    groups = _packets(db, monkeypatch)
    calls = []

    def answer(kw):
        if len(calls) == 1:
            return "no JSON in the first reply"
        return json.dumps({"groups": [_full(d) for d in _descriptor(kw["messages"][0]["content"])]})
    monkeypatch.setattr(ai, "_client", _sdk_client(calls, answer))
    meter = {"asks": 0}
    res = ai.assess_groups_and_apartments(groups, meter)
    assert len(calls) == 2 and all(r["valid"] for r in res) and meter["repair_scope"] == "batch" and meter["repair_fixed"] == len(groups)
    assert calls[0]["model"] == calls[1]["model"] == ai.model_name()
    assert calls[0]["output_config"] == calls[1]["output_config"] == {"format": {"type": "json_schema", "schema": ai.REVIEW_BATCH_SCHEMA}}
    assert calls[1]["max_tokens"] == calls[0]["max_tokens"] <= ai.OUTPUT_TOKEN_CEILING
    assert calls[0]["thinking"] == calls[1]["thinking"] and calls[0]["system"] == calls[1]["system"]
    assert set(calls[1]) == set(calls[0]) == {"model", "max_tokens", "thinking", "system", "messages", "output_config"}


def test_through_the_queue_the_repair_is_counted_and_the_validator_words_stay_out_of_rows_and_logs(db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _, run = _run(db)
    calls = []
    # every group cites a fabricated fact id first (the validator quotes it); the repair fixes all but the group re-asked first
    first = lambda g: {**_full(g), "findings": [{"fact_ids": [MARKER], "note": "A supplied fact.", "kind": "evidence"}]}   # noqa: E731
    second = lambda g: first(g) if g["index"] == 0 else _full(g)   # noqa: E731
    monkeypatch.setattr(ai, "_ask", _scripted_ask(calls, first, second, _full))
    out = ai_queue.process_pending(db, batch_size=100, workers=1, attempts=3)
    assert len(calls) == 2 and out["calls"] == 2 and out["batches"] == 1, "first call + one repair, both counted; the queue did not re-run the batch"
    assert MARKER in calls[1]["prompt"], "the validator's exact problem went back to the model in the repair prompt"
    gs = _groups(db, run["run_id"])
    assert sum(g["ai_status"] == "ok" for g in gs) == len(gs) - 1 and sum(g["ai_status"] == "failed" for g in gs) == 1
    failed = next(g for g in gs if g["ai_status"] == "failed")
    assert failed["ai_error"].startswith(ai.FAILURE_TEXT["review_unusable"]) and ai_queue.is_closed_failure_text(failed["ai_error"])
    assert ai_queue.is_ready(db, failed)["ready"] is False
    assert MARKER not in _sinks(db) and MARKER not in caplog.text
