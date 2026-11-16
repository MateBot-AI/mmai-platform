"""AI layer (T4(a), T4(c)): group compatibility assessment, introductions, free-text country reading.

Uses the Anthropic SDK. Two ways in, chosen at call time from the environment:
  1. A user-supplied Anthropic key delivered by the publish credential proxy: MMAI_AI_URL_VAR names the environment variable that
     holds the proxy base URL and MMAI_AI_TOKEN_VAR names the one holding the token. The platform's documented contract is that
     the token goes in the x-api-key header; the SDK (2.x) would send an api_key as "Authorization: Bearer", so _client sets
     x-api-key itself and removes the SDK's Authorization header for this client only. Default model: the public Claude Sonnet
     model id.
  2. A plain ANTHROPIC_API_KEY (MateBot AI's own Anthropic account, e.g. on DigitalOcean, T17) with no ANTHROPIC_BASE_URL set:
     the SDK talks to api.anthropic.com directly. Default model: the public Claude Sonnet model id.
  3. Otherwise the development sandbox LLM proxy (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN plus ANTHROPIC_BASE_URL injected by
     api_credentials), which uses its own underscore model ids.
  MMAI_AI_MODE=own-key|sandbox can force the choice between 2 and 3.
  Model (T13): every call uses Claude Sonnet 5.5 (claude-sonnet-5-5); there is no separate "fast" model. MMAI_AI_MODEL is honoured
  only on the development sandbox proxy (and is logged); with a real key (own-key / user-key) any value other than the approved
  id is ignored with a warning, so an environment change cannot silently switch the production model. MMAI_AI_FAST_MODEL is no
  longer read (8 Nov 2026 T13 review: the earlier Haiku fast default for free-text country reading had no approval).
Failure handling (T6, fail-closed): a review or staff brief that is unavailable, unusable or exceeds the response size limit
HOLDS the group (ai_status/brief_status failed, never ready); nothing rule-based stands in for it. Only the introduction has a
rule-based template (fallback_introduction), and that is visibly labelled and never counts as the AI draft (GP2).
Response size: the review, staff-brief and introduction calls have explicit output-token budgets (never above
OUTPUT_TOKEN_CEILING); a reply cut off at its budget is retried ONCE with a compact instruction and a larger budget, then raised
as AIOutputTruncated; a compact reply that comes back unusable is equally terminal (AINoRetry) so the queue never re-runs the
pair. These are engineering limits, not rules about how much staff may be told. normalise_country_ai is a single short call
with no retry.
Structured output (C4, 10 Nov 2026): the batch review asks for its reply in the provider's native JSON-schema output format
(messages.create(output_config={"format": {"type": "json_schema", "schema": ...}}), REVIEW_BATCH_SCHEMA). The schema fixes the
SHAPE only; every semantic validator (parse_review_batch, facts.check_items) still runs on the reply and nothing is relaxed. A
request in that format that the SDK or the service rejects raises AISchemaUnsupported: no other model is tried, the format is
never dropped for a free-text reply, and the call is not repeated. The SDK's own implicit retries are off (max_retries=0), so
every call made is one the callers count. A batch review gets at most ONE repair turn per call meter: for the groups of a
usable reply that fail the validator (those groups only, the validator's problems quoted), or for the whole batch when the reply
was unusable as a whole (nothing of that reply is kept or aligned by guess). The same validator judges the repair reply; whatever
is still invalid stays invalid, and a second reply that is again unusable as a whole leaves the original failure standing. If
the whole-batch repair call itself fails terminally (any AINoRetry: rejected output format, or a repair reply cut off at its
budget) or for missing credentials or SDK, that failure is raised instead, so the queue stops on it; the cut-off one is worded
by its own closed sentence, which does not claim a compact retry that was never made.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import json
import logging
import os
import re
from typing import Optional

from . import facts

log = logging.getLogger("mmai.ai")

# T13: the only approved model. Public Anthropic API id (platform.claude.com/docs/en/models/overview, checked 27 Oct 2026) for a
# direct or proxied key; the development sandbox proxy addresses the same model by its own underscore id.
APPROVED_MODEL = "claude-sonnet-5-5"
PUBLIC_MODEL = APPROVED_MODEL
SANDBOX_MODEL = "claude_sonnet_5_5"


def _proxy_env() -> tuple[str | None, str | None]:
    """(base_url, token) from the publish credential proxy, or (None, None). The two MMAI_* variables hold the NAMES of the
    variables that carry the values, so the names can live in a non-secret config file while the values stay host-injected."""
    url_var = os.environ.get("MMAI_AI_URL_VAR", "").strip()
    tok_var = os.environ.get("MMAI_AI_TOKEN_VAR", "").strip()
    if not url_var or not tok_var:
        return None, None
    url, tok = os.environ.get(url_var, "").strip(), os.environ.get(tok_var, "").strip()
    return (url or None), (tok or None)


def using_user_key() -> bool:
    url, tok = _proxy_env()
    return bool(url and tok)


def using_own_key() -> bool:
    """A plain Anthropic key for MateBot AI's own account (production hosting). Chosen when ANTHROPIC_API_KEY is set and no
    ANTHROPIC_BASE_URL points at a proxy, or when MMAI_AI_MODE=own-key says so explicitly."""
    if using_user_key():
        return False
    mode = os.environ.get("MMAI_AI_MODE", "").strip().lower()
    if mode in ("own-key", "own_key", "direct", "anthropic"):
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    if mode == "sandbox":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and not os.environ.get("ANTHROPIC_BASE_URL") and not os.environ.get("ANTHROPIC_AUTH_TOKEN")


def using_public_models() -> bool:
    return using_user_key() or using_own_key()


def model_override_ignored() -> str | None:
    """The MMAI_AI_MODEL value that is being ignored because a real key is in use and it is not the T13 approved id; else None."""
    env = os.environ.get("MMAI_AI_MODEL", "").strip()
    if env and using_public_models() and env != APPROVED_MODEL:
        return env
    return None


def model_name(fast: bool = False) -> str:
    """The model id for every call (T13: Claude Sonnet 5.5 only; `fast` is accepted for old call sites and makes no difference).
    With a real key (own-key / user-key) the approved public id is used and MMAI_AI_MODEL may only confirm it; on the development
    sandbox proxy MMAI_AI_MODEL may pick another sandbox id for testing, and that is logged."""
    env = os.environ.get("MMAI_AI_MODEL", "").strip()
    if using_public_models():
        if env and env != APPROVED_MODEL:
            log.warning("MMAI_AI_MODEL=%r ignored: T13 allows only %s with a real key", env, APPROVED_MODEL)
        return APPROVED_MODEL
    if env:
        if env not in (SANDBOX_MODEL, APPROVED_MODEL):
            log.warning("MMAI_AI_MODEL=%r in use on the development sandbox proxy (T13 approved model is %s)", env, APPROVED_MODEL)
        return env
    return SANDBOX_MODEL


def model_policy() -> dict:
    """For status screens: the model in use, whether it is the T13 approved model, and any ignored override."""
    m = model_name()
    return {"model": m, "approved_model": APPROVED_MODEL, "is_approved": m in (APPROVED_MODEL, SANDBOX_MODEL), "rule": "T13",
            "override_ignored": model_override_ignored()}


MODEL = model_name()
# T6 review contract version: bumped whenever the review/brief prompt, schema or validators are strengthened, so every earlier
# 'ok' review and brief becomes stale (part of the compatibility evidence; see matching.group_evidence / ai_queue.is_ready).
# Introduction-only wording/profile changes do NOT bump it.
REVIEW_CONTRACT = "2026-11-11.step-free-payload"   # Review and staff briefs receive the access value and provenance, not just its fact id.
# Response size limits (engineering limits so a reply fits its output budget; Bill has not ruled on note lengths). Enforced by
# the parsers: an over-cap reply is rejected, never clamped. fact_ids 14 lets a seven-person group cite every member's fact.
OUTPUT_TOKEN_CEILING = 8000                          # root-approved initial ceiling; a 7-person brief budget (7600) and its compact retry both cap here
FACT_IDS_MAX = facts.FACT_IDS_MAX                    # 14
NOTE_WORDS_ASKED, NOTE_WORDS_MAX = 25, facts.NOTE_WORDS_MAX   # asked of the model / rejected beyond (30 is the approved limit; 25 leaves margin)
COMPACT_NOTE_WORDS = 15
BRIEF_GROUP_ITEMS_MAX, BRIEF_MEMBER_ITEMS_MAX = 3, 2  # per list
REVIEW_FINDINGS_MAX = 10                             # per group
FAST_MODEL = MODEL  # kept for old imports; T13 has no separate fast model


def ai_mode() -> str:
    """For status screens: 'user-key' (publish credential proxy), 'own-key' (MateBot AI's own Anthropic account),
    'sandbox' (development proxy) or 'none'."""
    if using_user_key():
        return "user-key"
    if using_own_key():
        return "own-key"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "sandbox"
    return "none"


# Failure provenance (T5, 9 Nov 2026, root ruling "Modified A"). A FAILURE write to groups.ai_error, a failure held_reason,
# group_briefs.error or review_rounds.invalid_reason holds ONLY a closed plain sentence from FAILURE_TEXT, chosen by the exception's
# closed `kind` / `code`. Provider prose, Python exception text and our own validator sentences are never persisted or logged: a
# validator sentence quotes the model's words, which can carry a person's answer, name or country, so being code-authored does
# not make it safe. Exception messages stay descriptive IN PROCESS only (unit tests of the validators read them). Logs carry the
# fixed category and an operational id (group / run id) only. Valid model rationale (hold / revise reasons, apartment reasons,
# review_json) is intentional generated text kept under retention - it is not an error sink and is untouched here.
ERROR_KINDS = frozenset(("validation", "size_limit", "credentials", "setup", "provider", "unexpected", "schema"))
# Generic (service-side) failures are worded per STEP so a brief or introduction failure never denies a review that did complete
# (root read, 9 Nov 2026): the head names the cause, the step outcome names exactly what was not produced.
_GENERIC_HEAD = {
    "provider": "The AI service call failed (provider or network error)",
    "setup": "The AI SDK is not installed in this process",
    "credentials": "No AI credentials are configured in this process",
    "unexpected": "An unexpected error stopped the AI step",
}
STEP_OUTCOME = {
    "review": "the group was not assessed.",
    "brief": "the staff brief was not produced; the recorded review stands.",
    "introduction": "the introduction was not drafted; the recorded review and staff brief stand.",
}
FAILURE_STEPS = frozenset(STEP_OUTCOME)
STEP_FAILURE_TEXT = {step: {kind: f"{head}; {outcome}" for kind, head in _GENERIC_HEAD.items()} for step, outcome in STEP_OUTCOME.items()}
FAILURE_TEXT = {
    **STEP_FAILURE_TEXT["review"],   # provider / setup / credentials / unexpected: the review-step wording is the default
    "validation": "The AI reply failed the output validator and was not used.",
    "size_limit": "The AI reply exceeded the response size limit after a compact retry; no further automatic retry - staff decide.",
    # C4: says only what is known - a request in the structured reply format was rejected (the cause is not read from the provider's words)
    "schema": "The AI service rejected a request that used the structured reply format (the configured model or installed AI library may not support it); no automatic model change or free-text fallback was made.",
    # C4: a reply cut off where NO compact retry was made (the one whole-batch repair call); it must not claim one happened
    "size_limit_no_compact": "The AI response exceeded the output limit; the review did not complete.",
    "size_limit_compact_unusable": "The AI compact reply after a cut-off reply was unusable; no further automatic retry - staff decide.",
    "size_limit_compact_failed": "The AI compact retry after a cut-off reply failed; no further automatic retry - staff decide.",
    "review_unusable": "AI review unusable: the reply failed the output validator.",
    "review_missing": "AI review unusable: the reply carried no review object for this group.",
    "review_duplicate": "AI review unusable: the reply carried more than one review object for this group.",
    "review_shape": "AI review unusable: the reply was not a list of group reviews for this batch.",
    "brief_unusable": "Staff brief unusable: the reply failed the output validator.",
    "brief_unusable_after_repair": "Staff brief unusable: the reply failed the output validator and one corrected reply failed it again; staff decide.",
    "brief_repair_truncated": "Staff brief unusable: the reply failed the output validator and the corrected reply exceeded the response size limit; no further automatic retry - staff decide.",
    "brief_repair_failed": "Staff brief unusable: the reply failed the output validator and the corrected-reply call failed; no further automatic retry - staff decide.",
    "brief_packet": "Staff brief not attempted: the group packet has no members.",
    "intro_unusable": "Introduction draft unusable: the reply failed the output validator.",
    "intro_packet": "Introduction not attempted: the group has no members or no approved staff brief.",
    "packet": "The candidate packet was incomplete; the group was not assessed.",
    "no_review": "AI returned no review for this group.",
}
LEGACY_FAILURE_TEXT = "An earlier AI failure record was replaced by this fixed sentence (legacy cleanup)."
ALL_FAILURE_SENTENCES = frozenset(FAILURE_TEXT.values()) | frozenset(t for by_kind in STEP_FAILURE_TEXT.values() for t in by_kind.values())
NO_CREDENTIALS_TEXT = ("No AI credentials in this process (preview: start the server with api_credentials=['llm-api:website']; "
                       "published: set MMAI_AI_URL_VAR and MMAI_AI_TOKEN_VAR to the credential's variable names; own hosting: set ANTHROPIC_API_KEY)")


class AIUnavailable(Exception):
    """An AI step did not produce a usable result. `kind` (closed) says why in broad terms; `code` (closed, optional) picks the
    exact persisted sentence. The message is in-process diagnosis only. A bare AIUnavailable means the service did not answer
    (kind 'provider'); replies that answered but failed our validators are AIUnusableReply (kind 'validation')."""
    kind = "provider"

    def __init__(self, msg: str = "", kind: str | None = None, code: str | None = None):
        super().__init__(msg)
        if kind is not None:
            self.kind = kind if kind in ERROR_KINDS else "unexpected"
        self.code = code if code in FAILURE_TEXT else None


class AIUnusableReply(AIUnavailable):
    """The model answered but the reply failed our validators (shape, JSON, grounding, wording). Retrying the same prompt can help,
    but the queue treats a brief's unusable reply as final for the attempt loop (as before). `problems` (optional list of str) is
    the validator's exact, untruncated problem list, so a bounded feedback turn can quote it; the message stays the truncated
    in-process diagnosis as before."""
    kind = "validation"

    def __init__(self, msg: str = "", kind: str | None = None, code: str | None = None, problems: list[str] | None = None):
        super().__init__(msg, kind=kind, code=code)
        self.problems = list(problems) if problems else []


class AINoRetry(AIUnavailable):
    """Terminal for the queue: the bounded-output sequence (normal call + one compact retry) has been exhausted, or the compact
    reply came back unusable. Running the pair again would spend the same calls on the same facts; the group holds for staff."""
    kind = "size_limit"


class AIOutputTruncated(AINoRetry):
    """The model stopped at the output-token budget (stop_reason max_tokens). Callers retry once compactly via _ask_bounded; the
    second truncation is terminal: the queue must NOT retry it again (the same facts would overflow the same budget)."""

    def __init__(self, msg: str, max_tokens: int = 0, attempts: int = 1):
        super().__init__(msg, kind="size_limit", code="size_limit")
        self.max_tokens, self.attempts = max_tokens, attempts


class AISchemaUnsupported(AINoRetry):
    """A request that asked for the native JSON-schema output format was rejected: the installed SDK refused the argument
    (TypeError) or the service answered 400 (anthropic.BadRequestError). Closed kind and code 'schema'. Terminal like every
    AINoRetry: the same request would be rejected again, so nothing is retried - not with another model, not without the schema,
    not as free text. It takes no message: the label is fixed, so the provider's or SDK's words cannot be carried in it."""
    kind = "schema"

    def __init__(self):
        super().__init__("AI structured-output request rejected", kind="schema", code="schema")


def failure_kind(e: BaseException) -> str:
    """Closed kind for any exception an AI step raised: ours carry one; anything else is 'unexpected'."""
    return e.kind if isinstance(e, AIUnavailable) and e.kind in ERROR_KINDS else "unexpected"


def failure_text(e: BaseException, step: str = "review") -> str:
    """The ONLY text a database failure sink may hold for `e`: the closed sentence for its code, else for its kind - worded for
    `step` ('review' | 'brief' | 'introduction') when the kind is a generic service-side failure. Never the exception's own words."""
    if isinstance(e, AIUnavailable) and e.code:
        return FAILURE_TEXT[e.code]
    kind = failure_kind(e)
    if kind in _GENERIC_HEAD:
        return STEP_FAILURE_TEXT[step if step in FAILURE_STEPS else "review"][kind]
    return FAILURE_TEXT[kind]


def persisted_invalid_reason(r: dict) -> str | None:
    """Closed sentence for review_rounds.invalid_reason / groups.ai_error from a parse_review_batch result: fixed text by closed
    code plus a count, never the validator's sentences (r['invalid_reason'] stays in-process diagnosis)."""
    if r.get("valid") or not r.get("invalid_reason"):
        return None
    code = r.get("invalid_code") if r.get("invalid_code") in FAILURE_TEXT else "review_unusable"
    n = int(r.get("invalid_count") or 0)
    return FAILURE_TEXT[code] + (f" ({n} problem{'s' if n != 1 else ''} found)" if code == "review_unusable" and n else "")


# C4: the SDK retries a failed request twice by default without telling the caller. Every retry here is the callers' own bounded,
# counted attempt, so the SDK's implicit ones are switched off on every client.
SDK_MAX_RETRIES = 0


def _client():
    try:
        from anthropic import Anthropic
    except Exception:  # pragma: no cover
        log.warning("AI call not attempted: setup (SDK missing)")   # category only; never the import error's text
        raise AIUnavailable("anthropic SDK missing", kind="setup", code="setup")
    base_url, token = _proxy_env()
    if base_url and token:
        # Published site: the user's own Anthropic key, reached through the publish credential proxy. The token is sent as x-api-key
        # (the platform's documented contract) and the SDK's own "Authorization: Bearer <api_key>" is removed for this client
        # (anthropic.omit), so exactly one credential header goes out. api_key stays set: with an explicit key the SDK looks for
        # no other credential in the environment or on disk. An SDK without `omit` builds no client here (never a bearer instead).
        from anthropic import omit
        return Anthropic(base_url=base_url, api_key=token, default_headers={"anthropic-version": "2023-06-01", "x-api-key": token, "Authorization": omit}, max_retries=SDK_MAX_RETRIES)
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise AIUnavailable(NO_CREDENTIALS_TEXT, kind="credentials", code="credentials")
    if using_own_key():
        # Production hosting: MateBot AI's own Anthropic account, straight to api.anthropic.com (the SDK reads ANTHROPIC_API_KEY).
        return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=SDK_MAX_RETRIES)
    # the sandbox LLM proxy requires the classic API version header
    return Anthropic(default_headers={"anthropic-version": "2023-06-01"}, max_retries=SDK_MAX_RETRIES)


# C4: the JSON schema (or None) that _ask sends as the native output format. Set only by _output_schema for the duration of the
# calls it wraps, so _ask keeps its signature (tests and offline tools replace _ask with doubles of exactly that signature).
_SCHEMA: contextvars.ContextVar = contextvars.ContextVar("mmai_ai_output_schema", default=None)


@contextlib.contextmanager
def _output_schema(schema: dict | None):
    """Every _ask inside this block asks for `schema` as the native JSON-schema output format; None means plain text, also when
    an outer block had set one (nothing is inherited by accident). The API requires an object at the root."""
    if schema is not None and not (isinstance(schema, dict) and schema.get("type") == "object"):
        raise ValueError("output schema root must be a JSON object schema")
    token = _SCHEMA.set(schema)
    try:
        yield
    finally:
        _SCHEMA.reset(token)


def _schema_rejected(e: BaseException) -> bool:
    """Decided by trusted exception TYPE only, never by words: the SDK refusing the output_config argument (TypeError) or the
    service answering 400 Bad Request to the schema-bearing call."""
    if isinstance(e, TypeError):
        return True
    try:
        from anthropic import BadRequestError
    except Exception:  # pragma: no cover
        return False
    return isinstance(e, BadRequestError)


def _ask(prompt: str, model: str | None = None, max_tokens: int = 1500, system: str = "") -> str:
    """One model call. Raises AIOutputTruncated when the reply stopped at max_tokens (never above OUTPUT_TOKEN_CEILING). Inside
    _output_schema(schema) the call asks for the native JSON-schema output format; a rejected schema-bearing call raises
    AISchemaUnsupported and nothing else is tried."""
    model = model or model_name()
    max_tokens = min(int(max_tokens), OUTPUT_TOKEN_CEILING)
    schema = _SCHEMA.get()
    client = _client()
    fmt = {"output_config": {"format": {"type": "json_schema", "schema": copy.deepcopy(schema)}}} if schema is not None else {}
    try:
        msg = client.messages.create(model=model, max_tokens=max_tokens, thinking={"type": "between_tools"} if using_public_models() else {"type": "disabled"},
                                     system=system or "You are MateBot AI, Apartment Living Melbourne's matchmaking assistant.",
                                     messages=[{"role": "user", "content": prompt}], **fmt)
    except Exception as e:
        if schema is not None and _schema_rejected(e):
            log.warning("AI call failed: schema")   # T5: category only - the SDK's / provider's words go nowhere
            raise AISchemaUnsupported()
        log.warning("AI call failed: provider")   # T5: the provider's words go nowhere - not the log, not a row
        raise AIUnavailable("AI service call failed", kind="provider", code="provider")
    if getattr(msg, "stop_reason", "") == "max_tokens":
        raise AIOutputTruncated(f"AI response was cut off at the {max_tokens}-token output budget", max_tokens=max_tokens)
    return "".join(getattr(b, "text", "") for b in msg.content)


def _count(meter: dict | None) -> None:
    """`meter` (optional dict) counts every model call actually attempted ('asks'), so queue call counters stay truthful when a
    caller retries internally."""
    if meter is not None:
        meter["asks"] = int(meter.get("asks") or 0) + 1


def _terminal_if_compact(meter: dict, what: str, e: AIUnavailable) -> AIUnavailable:
    """The exception to raise when parsing a bounded reply failed: terminal (AINoRetry) if that reply was already the compact
    retry, otherwise unchanged (the queue may retry an ordinary unusable reply within its own bounded attempts)."""
    if isinstance(e, AINoRetry) or not meter.get("compact"):
        return e
    return AINoRetry(f"{what} compact reply after a cut-off reply was unusable; no further automatic retry - staff decide", code="size_limit_compact_unusable")


def _ask_bounded(build_prompt, max_tokens: int, what: str, meter: dict | None = None, schema: dict | None = None, system: str = "") -> str:
    """_bounded_attempts with `schema` (optional) as the native JSON-schema output format for the duration of its attempts (the
    normal call and the one compact retry alike). AISchemaUnsupported passes straight through: no compact retry, no retry at all.
    `system` (optional) is the caller's own system text, sent with both attempts; left empty, _ask is called exactly as before
    and sends its MateBot default (the HomeBot calls pass theirs: before 16 Nov 2026 they went out under the MateBot line)."""
    with _output_schema(schema):
        return _bounded_attempts(build_prompt, max_tokens, what, meter, system)


def _bounded_attempts(build_prompt, max_tokens: int, what: str, meter: dict | None = None, system: str = "") -> str:
    """Bounded output handling shared by the review, staff brief and introduction calls: one call at `max_tokens` (capped at
    OUTPUT_TOKEN_CEILING); if the reply is cut off, exactly ONE compact retry (`build_prompt(True)` asks for the compact
    variant) at 1.5x the budget, still capped; a second cut-off raises AIOutputTruncated, and ANY other failure of the compact
    attempt raises AINoRetry. Total calls are therefore at most two, and the exception tells the queue not to try again. `meter` (dict) also gets compact=True once the
    retry was used, so callers can make an unusable compact reply terminal as well (_terminal_if_compact)."""
    budget = min(int(max_tokens), OUTPUT_TOKEN_CEILING)
    own = {"system": system} if system else {}   # nothing extra is passed when the caller gave no system text
    _count(meter)
    try:
        return _ask(build_prompt(False), max_tokens=budget, **own)
    except AIOutputTruncated:
        log.warning("%s cut off at %s output tokens; one compact retry", what, budget)
    retry = min(int(budget * 1.5), OUTPUT_TOKEN_CEILING)
    _count(meter)
    if meter is not None:
        meter["compact"] = True   # tells the caller that whatever comes back is the last attempt
    try:
        return _ask(build_prompt(True), max_tokens=retry, **own)
    except AIOutputTruncated:
        raise AIOutputTruncated(f"{what} exceeded the response size limit after a compact retry ({retry} output tokens); no further automatic retry - staff decide",
                                max_tokens=retry, attempts=2)
    except AINoRetry:
        raise
    except AIUnavailable as e:
        # any other failure of the compact attempt (outage, credentials, transport) is terminal too: the pair is never re-run
        raise AINoRetry(f"{what} compact retry after a cut-off reply failed ({failure_kind(e)}); no further automatic retry - staff decide", code="size_limit_compact_failed")


def _json_from(text: str):
    """The first JSON object/array in a reply; a malformed one is an unusable reply (AIUnavailable), never a raw JSONDecodeError
    that would slip past the callers' handling."""
    m = re.search(r"\{.*\}|\[.*\]", text or "", re.S)
    if not m:
        raise AIUnusableReply("AI returned no JSON")
    try:
        return json.loads(m.group(0))
    except ValueError as e:   # json.JSONDecodeError is a ValueError
        raise AIUnusableReply(f"AI returned malformed JSON ({type(e).__name__} at position {getattr(e, 'pos', '?')})")   # position only, never the reply's text


def assess_groups(apartment: dict, groups: list[dict]) -> list[dict]:
    """Candidate ranking at ONE apartment (replacement / refill options, GP7/GP10). Same strict contract as the batch review:
    each packet must carry `members_for_ai`, `person_refs` and an `apartments` list with that apartment's lease facts and
    rule_checks (matching.candidate_packet builds it). Returns one dict per candidate: {index, valid, invalid_reason, verdict,
    rating (None unless valid), rationale, findings, replace_refs, apartments}. No clamping, no defaults. Raises AIUnavailable."""
    if not groups:
        return []
    for g in groups:
        if not g.get("apartments") or not g.get("members_for_ai") or not g.get("person_refs"):
            raise AIUnavailable("candidate packet lacks apartments/members/refs; built with the legacy payload", code="packet")
    return assess_groups_and_apartments(groups)


def review_packet(groups: list[dict]) -> list[dict]:
    """The exact JSON the review model sees for a batch (also used by tests). Per group: members (refs + practical facts), the
    fact-id catalogue it must cite, and each offered apartment with lease facts, bed allocation by ref, feature facts and the
    deterministic rule-check results with their provenance."""
    desc = []
    for i, g in enumerate(groups):
        refs = g.get("person_refs") or {}
        cat = facts.catalogue(g)
        desc.append({
            "index": i,
            "members": g["members_for_ai"],
            "people": facts.coverage(g),
            "fact_ids": sorted(k for k, v in cat.items() if v["status"] == "recorded"),
            "missing_fact_ids": sorted(k for k, v in cat.items() if v["status"] == "missing"),
            "apartments_in_rule_order": [
                {"apartment_id": a["apartment_id"], "address": a["address"], "type": a["type_label"], "suburb": a["suburb"],
                 "bills_included": a.get("bills_included"), "available_from": a.get("available_from"),
                 "step_free_access": a.get("step_free_access"), "step_free_source": a.get("step_free_source"),
                 "lease": {"start": a["lease_start"], "end": a["lease_end"], "stay_days": a.get("stay_days"), "rate_band": a.get("rate_band")},
                 "whole_weekly_rent": a["total_weekly"], "extra_bed_charge": a.get("extra_bed") or 0,
                 "rooms": a.get("allocation_for_ai") if a.get("allocation_for_ai") is not None else
                          [{"room": r["room"], "setup": r["setup"], "room_cost": r.get("room_cost"),
                            "occupants": [{"ref": refs.get(str(o.get("person_id")), f"room{j}-occupant{k}"), "bed": o.get("bed"), "weekly_share": o.get("share")}
                                          for k, o in enumerate(r.get("occupants") or [], start=1)]} for j, r in enumerate(a.get("allocation") or [], start=1)],
                 "campus_distance_km": a.get("campus_km"), "campus_distance": a.get("campus_distance"), "rule_score": a.get("rule_score"), "rule_rank": a.get("rule_rank"),
                 "rule_checks": a.get("rule_checks") or [], "features_asked_for": a.get("features") or []}
                for a in sorted(g["apartments"], key=lambda x: (x.get("rule_rank") is None, x.get("rule_rank") or 0))],
        })
    return desc


REVIEW_RULES_TEXT = (
    "Launch scope: ALM does not use anyone's country of birth, nationality, ethnicity, race, religion, regional origin, language, sex, gender or age in "
    "matching. The packet deliberately omits names, countries, languages and free text; people are referred to by refs such as U1 or U1-m1. "
    "Never infer, mention or weigh any of those attributes. Judge only the structured practical facts given.\n"
    "Rules and provenance: each apartment carries rule_checks - the deterministic results of ALM's hard rules (dates, budget, beds, smoking, adults, capacity) "
    "under the stated rule version, with the facts they were computed from. Those results are authoritative and you cannot waive or re-decide them. Your job "
    "with them is CONSISTENCY: if the facts in the packet contradict a rule result or each other (for example the household's combined guide shares exceeding the "
    "A16 allowance over the sum of everyone's stated maximums, a lease outside someone's window, a party member omitted from the group or split from their party "
    "across different groups or apartments), say so as a finding citing the fact ids and use verdict 'hold' or 'revise'. A single person's guide share above "
    "their own stated maximum is NOT by itself a failure: A16 has no per-person ceiling and shares are negotiable within the household allowance; party members "
    "in different bedrooms of the SAME apartment are together. "
    "Do not assume a rule passed because a flag says so; do not assert that it failed without citing the facts.\n"
    "Facts marked party-wide were answered by the enquirer for their whole party (I16(d)); refs like U2-m1 have NO personal study, work, lifestyle or contact "
    "facts of their own (listed under missing_fact_ids) and you must never copy the enquirer's personal facts onto them.\n"
    "Preferences: room_first is the party's stated FIRST CHOICE; rooms_accepted, apt_sizes_accepted, bunk_ok and mate_max are what the enquirer said the party "
    "would ACCEPT - treat them with discretion as acceptance, never as the first choice, and never read a proposed allocation or the group's size as if the "
    "party had asked for it. A value that is not recorded is unknown, not 'no'. The smoking rule_check is a unit-level derived check from each enquirer's "
    "party answers (smoke / smoke_ok, source shown as derived_from) - it is not a companion's own statement.\n"
    "Household budget: the budget rule_check carries R.A<id>.budget.total_weekly_rent_exact, whole_apartment_max_exact (the sum of every member's stated weekly "
    "maximum), allowance_fraction and limit_exact as exact strings with units; cite them rather than paraphrasing figures. allowance_fraction is a raw "
    "fraction - 0.1 means ten percent - quote it as given and never convert, round or re-derive it.\n"
    "Per-person budget evidence: R.A<id>.budget.share.<ref> is that person's A12 guide share from the bed allocation (a proposal for the group's agreement - "
    "never call it agreed or accepted) and R.A<id>.budget.stated_max.<ref> is their I14 stated weekly maximum (for a companion, reported by the enquirer "
    "for the party). A note about a person's share or maximum cites THAT person's ids, not another member's and not the household figures alone.\n"
    "Housemates: the housemates rule_check shows each enquirer's accepted maximum of OTHER housemates next to the number of other people in this group; "
    "that maximum never counts the person themselves or the total number of occupants (a maximum of 6 is met by a seven-person group).\n"
    "Counting people: party_size COUNTS THE ENQUIRER (party_size 6 with five companions listed is six people including the enquirer). The 'people' block is the "
    "code-derived count of who gave their own lifestyle / compatibility answers and who did not; if you state how many people have or lack those answers, use its "
    "numbers exactly - any other 'n of m people' coverage count is rejected. Companions do have introduction details outside this packet, so say they have no "
    "independently recorded lifestyle answers here, not that nothing is known about them.\n"
    "Evidence rule: every finding cites fact_ids from fact_ids (recorded); missing_fact_ids may only be cited in items marked kind 'gap', whose note says what "
    "is unknown and never asserts a requirement is met. A claim without a fact id is discarded. Feature facts (features_asked_for) are recorded at the apartment "
    "level against what people asked for as 'available' or 'unavailable'; 'unverified' means ALM has NOT recorded it (its id is in missing_fact_ids: a gap, never "
    "evidence that the facility exists). Feature facts explain trade-offs and never exclude an apartment or prove a particular bedroom has the facility. "
    "When a member's notes make step-free access essential, that apartment's step_free_access id (A<id>.step_free_access) is in the catalogue: when it is in "
    "missing_fact_ids ALM has not recorded step-free access for that apartment (a lift is never proof), so cite that id (with the member's notes id) in a kind "
    "'gap' item saying staff must confirm it; when it is a recorded fact (value true = the team recorded a completely step-free route, false = recorded as not "
    "step-free) cite it as a recorded fact in a kind 'evidence' item and leave the decision to staff; never treat the need as met or unmet yourself. "
    "The apartment's step_free_access value is 1/true for recorded step-free, 0/false for recorded not step-free, and null for unknown, "
    "even when step_free_source names the source of that unknown record.\n"
    "Lease length: the stay is lease start to lease end (stay_days gives the exact day count). rate_band is only ALM's A6 pricing band for that start/end "
    "combination; it is not the length of the lease, so never describe a stay as 'six months' or 'twelve months' from the rate band.\n"
    "Response size: this is an engineering limit so the reply fits its output budget, not a rule about what staff may be told. Keep every note to one "
    f"sentence of at most {NOTE_WORDS_ASKED} words (over {NOTE_WORDS_MAX} is rejected) with at most {FACT_IDS_MAX} fact ids. Lists may be short or, where the "
    "contract allows, empty: never invent a tension, risk or gap the facts do not show just to fill a list.\n"
)

# 11 Nov 2026 (prompt/validator parity): facts.wording_problems judges three wording families on EVERY validated note - review findings
# and replace reasons as well as staff-brief notes. Families (1) share-agreed and (2) accepted-versus-first-choice are stated in
# REVIEW_RULES_TEXT, which both prompts carry. Family (3), timetables, was stated only in the staff-brief prompt (9 Nov), so the review
# model was judged on a rule its prompt never gave it. The sentence is now ONE constant used by both prompts, so its words are the same
# in each; each prompt adds only its own closing words about where an honest "compare timetables" note belongs (the brief has
# discussion_points; the review has findings, where kind 'gap' needs an id from missing_fact_ids and nothing records a timetable).
# Prompt guidance only: the validator is unchanged. tests/test_review_prompt_wording_parity.py holds all three families in both prompts.
TIMETABLE_GUIDANCE = (
    "Course, school, level and campus labels alone do not "
    "establish timetable alignment: a shared course or campus is a recorded strength, but never infer that timetables, class times or attendance line up "
    "from them; quote supplied timing only as recorded, for the person who supplied it, without generalising to others")
BRIEF_TIMETABLE_PLACE = " (put 'compare timetables' under discussion_points if useful).\n"
REVIEW_TIMETABLE_PLACE = (
    " (a finding need not mention timetables at all; if it helps staff, say only that they are not recorded or are worth comparing, as kind 'gap' "
    "only when the item cites an id from missing_fact_ids - never, even hedged as 'likely' or 'similar', that timetables or study schedules line up).\n")

REVIEW_GAP_GUIDANCE = (
    "For kind 'gap', cite at least one RELEVANT id from this group's missing_fact_ids and say what that missing fact leaves unknown. "
    "A concern, discussion point or recommendation grounded solely in recorded facts uses kind 'evidence', not 'gap'; it must not claim an unlisted missing fact. "
    "If no relevant missing id or recorded evidence supports the item, omit it. Never add an unrelated missing id just to pass validation. "
    "A notes-derived id listed in fact_ids is recorded evidence of what the person stated, not independent verification, even when its source says "
    "'self-report (free text), unverified'. This differs from a feature fact at level 'unverified', which is missing and belongs in missing_fact_ids. "
    "A<id>.step_free_access with a recorded true or false value also remains recorded. If every cited id is in fact_ids, the item cannot be kind 'gap'.\n")


def assess_groups_and_apartments(groups: list[dict], meter: dict | None = None) -> list[dict]:
    """T6 + GP6(a): one call for a batch of matched groups (packets from ai_queue._payload). Returns parse_review_batch(...):
    one strict result per group. Raises AIUnavailable when the reply is unusable as a whole."""
    if not groups:
        return []
    desc = review_packet(groups)
    prompt = (
        "You are reviewing proposed groups of housemates for Apartment Living Melbourne (ALM) before staff see them.\n"
        + REVIEW_RULES_TEXT +
        "Timetables: " + TIMETABLE_GUIDANCE + REVIEW_TIMETABLE_PLACE + REVIEW_GAP_GUIDANCE +
        "For EACH group do four things.\n"
        "1. findings: a list of {\"fact_ids\": [...], \"note\": <one plain-English sentence for ALM staff>, \"kind\": \"evidence\"} - the practical compatibility "
        "points (routine, tidiness, social style, guests, diet, study or work pattern, dates, party composition) and any inconsistency with the rule_checks, each "
        "citing RECORDED fact ids only. Something unknown or unrecorded (an id from missing_fact_ids) goes in its own item with \"kind\": \"gap\" and a note that "
        "says what is missing and what to confirm; a gap item must not assert that a requirement is met. Say what is likely to cause complaints and whether the "
        "group is likely to sign one lease.\n"
        "2. rating: how well these people will get on living together, 1-10, with rationale: 2-3 sentences that only restate findings.\n"
        "3. verdict: \"approve\" (can go to staff as it stands), \"revise\" (would work better with one or more members replaced: list their refs in replace_refs and give "
        "replace_reasons: [{\"ref\": <ref>, \"fact_ids\": [...], \"note\": ...}] citing at least one recorded fact OF THAT PERSON; to say who they conflict with, "
        "cite that person's own facts in fact_ids and the other member's SAME fields under \"compared_with\": [{\"ref\": <other ref>, \"fact_ids\": [...]}], never "
        f"another person's ids in fact_ids; response size limit: at most {facts.COMPARE_REFS_MAX} counterparts and {facts.COMPARE_IDS_MAX} counterpart ids per note, "
        f"separate from the {FACT_IDS_MAX} own ids), or \"hold\" (facts too "
        "incomplete or contradictory to approve; staff must clarify - cite the missing or conflicting fact ids). The verdict is compulsory, never overrides a "
        "rule result and never asserts success the facts do not support.\n"
        "4. apartments: rank the listed apartments from best suited to least for this group with one short reason each for the reservations team (rent and each "
        "share against stated maxima, campus distance, room setup fit, bills, availability, size, the feature facts). Include EVERY apartment_id, each exactly once.\n\n"
        "Return ONLY JSON: one object {\"groups\": [...]} whose \"groups\" list has exactly one object per group index: {\"index\": <group index>, "
        "\"findings\": [...], \"rating\": <1-10 number>, "
        "\"rationale\": <string>, \"verdict\": \"approve\"|\"revise\"|\"hold\", \"replace_refs\": [...], \"replace_reasons\": [...], "
        "\"apartments\": [{\"apartment_id\": <id>, \"reason\": <string>} ... best first]}.\n"
    )

    def build(compact: bool, feedback: str = "", packet: list | None = None) -> str:
        words = COMPACT_NOTE_WORDS if compact else NOTE_WORDS_ASKED
        n_find = REVIEW_FINDINGS_MAX // 2 if compact else REVIEW_FINDINGS_MAX
        size = (f"Response size limit: at most {n_find} findings per group (the most material ones), notes and apartment reasons of at most {words} words, "
                f"rationale at most 3 sentences." + (" Your previous reply was cut off at the output budget: be compact." if compact else "") + "\n\n")
        # the packet descriptor stays the LAST blank-line-separated block (offline replay tools key on it); feedback sits before it
        return prompt + size + feedback + json.dumps(desc if packet is None else packet, ensure_ascii=False, default=str)

    meter = meter if meter is not None else {}
    text = _ask_bounded(build, review_token_budget(groups), "AI review", meter=meter, schema=REVIEW_BATCH_SCHEMA)
    try:
        results = parse_review_batch(_json_from(text), groups)
    except AIUnavailable as e:
        first = _terminal_if_compact(meter, "AI review", e)
        if not isinstance(e, AIUnusableReply) or isinstance(first, AINoRetry) or meter.get("compact") or meter.get("repair"):
            raise first
        results = None
    if results is None:
        # outside the handler above, so a failure raised by the repair is never implicitly chained to the first reply's diagnosis
        return _repair_unusable_batch(first, groups, build, meter)
    return _repair_invalid_reviews(results, groups, build, meter)


def review_token_estimate(groups: list[dict]) -> int:
    """UNCAPPED estimate of the output a batch review needs: 1200 + 400*G + 150*M + 90*A with G groups in the batch, M people
    across the batch (every person ref of every group: enquirers and their listed party members) and A offered apartments
    across the batch. Pure: no model call, no I/O. This is the one shared formula: the queue compares it with
    OUTPUT_TOKEN_CEILING to decide where to split a batch (the capped budget below can never exceed the ceiling, so it cannot
    answer that question), and review_token_budget caps it for the request itself.

    This is an ENGINEERING constraint, not a rule and not a business weight: the approved plan gives only the principle (raise
    the review budget within the ceiling, size it for group and member complexity, do not underbudget a large group at a single
    apartment). The conservative coefficients were raised after the 11 Nov synthetic cohort exhausted 6230 tokens for
    five groups / thirteen people / twelve apartments. They provide headroom and earlier splitting, not a guarantee
    against truncation. The 8000-token ceiling and single compact-or-repair allowance are unchanged."""
    people = sum(len(g.get("person_refs") or ()) or len(g.get("members_for_ai") or ()) for g in groups)
    return 1200 + 500 * len(groups) + 180 * people + 220 * sum(len(g["apartments"]) for g in groups)


def review_token_budget(groups: list[dict]) -> int:
    """The output budget actually asked for a batch review: min(OUTPUT_TOKEN_CEILING, review_token_estimate(groups)). Pure.
    It reaches the ceiling when a batch is too large for it or for one oversized group; a reply that is cut off there gets
    the usual one compact retry and no more."""
    return min(OUTPUT_TOKEN_CEILING, review_token_estimate(groups))


# 11 Nov 2026: one fixed sentence added - a correction is validated as a whole, so a repaired item that breaks another rule is rejected
# just as the first reply was. Fixed words only; nothing of the first reply is quoted and the turn count is unchanged.
REVIEW_REPAIR_HEAD = ("REPAIR TURN (one only). Your previous reply for the group(s) in THIS request was rejected by ALM's output validator. "
                      "Every rule in this request still applies to the whole corrected review, not only to the listed problems. "
                      "The groups are numbered again from 0 here. Exact problems, verbatim, by group index in this request:")
# Whole-batch repair: fixed words only. The first reply's own text and index values are never quoted (nothing of it is trusted).
REVIEW_BATCH_REPAIR_TEXT = (
    "REPAIR TURN (one only). Your previous reply to this request could not be used at all and none of it was kept: it was not valid JSON, or not one "
    "object {\"groups\": [...]} of group review objects, or an object in it carried an \"index\" that is not one of this request's group indices. "
    "Answer the whole request again: \"groups\" must hold exactly one review object for every group in this request and for no other group, each with "
    "\"index\" set to that group's index exactly as given below. This is the only correction turn.\n\n")


def review_repair_feedback(invalid: list[dict]) -> str:
    """The feedback block of the one review repair turn: for each re-asked group (in the order it is re-asked) the validator's own
    diagnosis of its first reply, verbatim apart from line breaks. In-process and prompt only - never a row, never the log."""
    lines = [f"Group index {k}: " + (re.sub(r"\s*\n\s*", " ", str(r.get("invalid_reason") or "")).strip() or "the review was unusable")
             for k, r in enumerate(invalid)]
    return (REVIEW_REPAIR_HEAD + "\n" + "\n".join(lines) + "\n" + REVIEW_GAP_GUIDANCE +
            "\nReturn one object {\"groups\": [...]} with a COMPLETE corrected review object for every group in this request and for no other group: "
            "fix every listed problem, cite only fact ids listed for that group, and add no claim the facts do not support. This is the only correction turn.\n\n")


def _repair_failure_is_final(e: BaseException) -> bool:
    """Whether a failure of the whole-batch repair call is terminal for the queue. Decided by trusted exception type and closed
    kind only, never by words: every AINoRetry (the type's contract - today AISchemaUnsupported and a cut-off repair reply,
    AIOutputTruncated), or missing credentials / SDK - the same two kinds the queue itself does not retry."""
    return isinstance(e, AINoRetry) or (isinstance(e, AIUnavailable) and e.kind in ("credentials", "setup"))


def _repair_unusable_batch(first: AIUnavailable, groups: list[dict], build, meter: dict) -> list[dict]:
    """C4 bounded whole-batch repair. The first reply was unusable as a whole (no JSON, not a list of group objects, or an index
    outside the batch): ask ONCE more for the WHOLE batch, in the same native output format and with no compact retry of its
    own, saying in fixed words that the reply could not be used. Nothing of the first reply is kept, quoted or lined up by
    guess. The second reply goes through the SAME parse_review_batch: usable as a whole, its per-group results are returned as
    they are (groups it leaves invalid stay invalid - this was the one repair turn).

    When the repair call fails there are no first results to fall back on, so what is raised decides whether the queue tries
    again, and it must not be told to retry something that cannot succeed (_repair_failure_is_final):
      * any AINoRetry - the structured-output request was rejected (AISchemaUnsupported), or the repair reply was cut off at
        its budget (AIOutputTruncated) - or the credentials / SDK are missing (AIUnavailable of kind 'credentials' / 'setup'):
        THAT exception is raised, type and kind as they are - the queue stops and staff see the closed sentence for that
        cause, not "unusable reply". A terminal cut-off is never dressed as the retryable parse failure. The repair has no
        compact retry of its own (none is added), so the cut-off exception is given the code 'size_limit_no_compact', whose
        sentence says only that the output limit was exceeded and the review did not complete - not that a compact retry ran;
      * anything else - an ordinary outage, a repair reply that is again unusable, an unexpected error: `first` is raised
        exactly as it would have been without the repair, and the queue's own bounded attempts continue as before.
    Shares the single repair allowance of the meter with _repair_invalid_reviews. Counted in meter['asks']; meter['repair']
    True, ['repair_scope'] 'batch', ['repair_groups'] the batch size, ['repair_fixed'] the groups valid after it,
    ['repair_error'] the closed kind of the repair failure in both cases."""
    _count(meter)
    meter["repair"], meter["repair_scope"] = True, "batch"
    meter["repair_groups"], meter["repair_fixed"] = len(groups), 0
    try:
        with _output_schema(REVIEW_BATCH_SCHEMA):
            text = _ask(build(False, feedback=REVIEW_BATCH_REPAIR_TEXT), max_tokens=review_token_budget(groups))
        results = parse_review_batch(_json_from(text), groups)
    except Exception as e:   # the category (never the words) is logged and metered
        meter["repair_error"] = failure_kind(e)
        log.warning("AI review repair turn not used: %s", meter["repair_error"])
        if _repair_failure_is_final(e):
            if isinstance(e, AIOutputTruncated):
                e.code = "size_limit_no_compact"   # same exception, type and kind; only the closed sentence: no compact retry was made here
            raise   # our own closed exception, as raised: the queue must stop on it rather than retry an "unusable reply"
        raise first from None   # the repair call's own exception (and its words) is not chained onto the original failure
    meter["repair_fixed"] = sum(1 for r in results if r.get("valid"))
    return results


def _repair_invalid_reviews(results: list[dict], groups: list[dict], build, meter: dict) -> list[dict]:
    """C4 bounded review repair. When the first reply was usable as a whole but left some groups failing the validator, ask ONCE
    more for those groups only, quoting the validator's problems, in the same native output format and with no compact retry of
    its own. A replacement is merged only if it passes the SAME validator (nothing is loosened; a replacement that is still
    invalid leaves the first result standing). Skipped after a compact retry (that reply is already the last attempt) and when
    this meter has already carried a repair turn (of either scope) - there is never a second one. Any failure of the repair
    call (outage, cut-off, rejected format, unusable reply, anything unexpected) keeps the first results: the valid ones are
    untouched and each still-invalid one additionally carries 'repair_error', the closed kind of that failure, so the queue can
    tell "the repair call failed" from "the repair reply was still invalid" even without the meter. Counted in meter['asks'];
    meter['repair'] is True, ['repair_scope'] 'groups', ['repair_groups'] / ['repair_fixed'] are counts and
    meter['repair_error'] the same closed kind (a member of ERROR_KINDS)."""
    bad = [i for i, r in enumerate(results) if not r.get("valid")]
    if not bad or meter.get("compact") or meter.get("repair"):
        return results
    sub = [groups[i] for i in bad]
    _count(meter)
    meter["repair"], meter["repair_scope"] = True, "groups"
    meter["repair_groups"], meter["repair_fixed"] = len(bad), 0
    try:
        with _output_schema(REVIEW_BATCH_SCHEMA):
            text = _ask(build(False, feedback=review_repair_feedback([results[i] for i in bad]), packet=review_packet(sub)),
                        max_tokens=review_token_budget(sub))
        second = parse_review_batch(_json_from(text), sub)
    except Exception as e:   # the first results stand; the category (never the words) is logged and metered
        meter["repair_error"] = failure_kind(e)
        log.warning("AI review repair turn not used: %s", meter["repair_error"])
        return [r if r.get("valid") else {**r, "repair_error": meter["repair_error"]} for r in results]
    merged = list(results)
    for k, i in enumerate(bad):
        if second[k].get("valid"):
            merged[i] = {**second[k], "index": i}   # back to this group's place in the original batch
            meter["repair_fixed"] += 1
    return merged


VERDICTS = ("approve", "revise", "hold")

# C4: the review reply's SHAPE as a JSON schema for the provider's native output format. Shape only: no schema keyword can state
# the rating range, that fact ids exist and belong to the right person, that every offered apartment is ranked once, or any wording
# rule - parse_review_batch checks all of that on every reply exactly as before. Only keywords the format supports are used
# (object / array / string / integer / number, enum, required, additionalProperties false); there are no numeric, length or item
# count constraints and no descriptions (the prompt, not the schema, carries the instructions). Property order is the order the
# prompt asks for: findings before the rating, rationale and verdict that restate them.
_FACT_IDS_SCHEMA = {"type": "array", "items": {"type": "string"}}
_KIND_SCHEMA = {"type": "string", "enum": ["evidence", "gap"]}
REVIEW_GROUP_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {"fact_ids": _FACT_IDS_SCHEMA, "note": {"type": "string"}, "kind": _KIND_SCHEMA},
            "required": ["fact_ids", "note", "kind"], "additionalProperties": False}},
        "rating": {"type": "number"},
        "rationale": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "replace_refs": {"type": "array", "items": {"type": "string"}},
        "replace_reasons": {"type": "array", "items": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "fact_ids": _FACT_IDS_SCHEMA, "note": {"type": "string"}, "kind": _KIND_SCHEMA,
                           "compared_with": {"type": "array", "items": {
                               "type": "object", "properties": {"ref": {"type": "string"}, "fact_ids": _FACT_IDS_SCHEMA},
                               "required": ["ref", "fact_ids"], "additionalProperties": False}}},
            "required": ["ref", "fact_ids", "note"], "additionalProperties": False}},   # kind / compared_with stay optional, as in the validator
        "apartments": {"type": "array", "items": {
            "type": "object", "properties": {"apartment_id": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["apartment_id", "reason"], "additionalProperties": False}},
    },
    "required": ["index", "findings", "rating", "rationale", "verdict", "replace_refs", "replace_reasons", "apartments"],
    "additionalProperties": False,
}
# The format needs an object at the root, so the list of group reviews is wrapped; parse_review_batch already reads {"groups": [...]}.
REVIEW_BATCH_SCHEMA = {"type": "object", "properties": {"groups": {"type": "array", "items": REVIEW_GROUP_SCHEMA}},
                       "required": ["groups"], "additionalProperties": False}


def _finite_number(v) -> Optional[float]:
    """A strict finite number: int/float (not bool, not NaN/Inf) or a plain numeric string; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            f = float(v)
        except OverflowError:   # int too large for a float (10**1000): not a usable number, never a crash
            return None
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except (ValueError, OverflowError):
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _refs_in_group(g: dict) -> set[str]:
    refs = set((g.get("person_refs") or {}).values())
    for m in g.get("members_for_ai") or []:
        refs.add(m.get("ref"))
        refs.update(m.get("party_members") or [])
    return {r for r in refs if r}


def parse_review_batch(out, groups: list[dict]) -> list[dict]:
    """T6 fail-closed parsing of a batch review. Never clamps, defaults or guesses. Returns one dict per group index:
    {index, valid: bool, invalid_reason, verdict, replace_refs, rating, rationale, apartments}. A group is valid only when it
    has exactly one review object, a finite numeric rating in 1..10, a non-empty rationale, an explicit verdict, replace_refs
    that are refs of this group (non-empty iff verdict is revise), and the offered apartment ids each exactly once and no other.
    Raises AIUnavailable when the reply is not a list of objects at all, or (r6) when any object carries an index that is not
    one of this batch's group indices - missing, boolean, fractional, negative or out of range: the batch does not line up with
    what was asked and nothing in it is trusted (the whole batch is unusable)."""
    if isinstance(out, dict):
        out = out.get("groups") or out.get("results")
    if not isinstance(out, list) or not out or not all(isinstance(x, dict) for x in out):
        raise AIUnusableReply("AI review was not a list of group objects", code="review_shape")
    by_index: dict[int, list[dict]] = {}
    stray: list[str] = []
    for item in out:
        raw = item.get("index")
        idx = _finite_number(raw)
        if idx is None or idx != int(idx) or not (0 <= int(idx) < len(groups)):
            stray.append(repr(raw))
            continue
        by_index.setdefault(int(idx), []).append(item)
    if stray:
        raise AIUnusableReply(f"AI review carried {len(stray)} object(s) with an index outside this batch of {len(groups)} group(s): {', '.join(stray[:5])}", code="review_shape")
    results = []
    for i, g in enumerate(groups):
        cov = facts.coverage(g)
        res = {"index": i, "valid": False, "invalid_reason": None, "invalid_code": None, "invalid_count": 0, "verdict": None, "replace_refs": [], "rating": None,
               "rationale": "", "apartments": [], "findings": [], "replace_reasons": [], "coverage": cov}
        items = by_index.get(i, [])
        if len(items) != 1:
            # invalid_reason is in-process diagnosis; invalid_code / invalid_count are the closed metadata the queue persists
            res["invalid_reason"] = "review missing for this group" if not items else f"{len(items)} review objects for one group"
            res["invalid_code"] = "review_missing" if not items else "review_duplicate"
            results.append(res)
            continue
        item = items[0]
        problems = []
        rating = _finite_number(item.get("rating"))
        if rating is None or not (1.0 <= rating <= 10.0):
            problems.append(f"rating {item.get('rating')!r} is not a number in 1-10")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            problems.append("rationale missing")
        else:
            hits = facts.policy_violations(rationale)
            if hits:
                problems.append(f"rationale reasons from a trait the packet never carried ({', '.join(hits[:4])})")
        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            problems.append(f"verdict {verdict!r} is not approve/revise/hold")
        refs = item.get("replace_refs")
        if refs is None:
            refs = []
        known = _refs_in_group(g)
        if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
            problems.append("replace_refs is not a list of refs")
            refs = []
        else:
            bad = [r for r in refs if r not in known]
            if bad:
                problems.append(f"replace_refs names unknown refs {bad}")
            if len(set(refs)) != len(refs):
                problems.append("replace_refs repeats a ref")
            if verdict == "revise" and not refs:
                problems.append("verdict revise without replace_refs")
            if verdict != "revise" and refs:
                problems.append(f"replace_refs given with verdict {verdict!r}")
            if known and set(refs) >= {m.get("ref") for m in g.get("members_for_ai") or []}:
                problems.append("replace_refs would replace every member")
        offered = [int(a["apartment_id"]) for a in g.get("apartments") or []]
        apts_in = item.get("apartments")
        apts: list[dict] = []
        if not isinstance(apts_in, list):
            problems.append("apartments is not a list")
        else:
            ids = []
            for a in apts_in:
                aid = _finite_number(a.get("apartment_id")) if isinstance(a, dict) else None
                if aid is None or aid != int(aid):
                    problems.append("an apartment entry has no numeric apartment_id")
                    break
                ids.append(int(aid))
                apts.append({"apartment_id": int(aid), "reason": str(a.get("reason") or "").strip()})
            else:
                if sorted(ids) != sorted(offered):
                    missing, extra = sorted(set(offered) - set(ids)), sorted(set(ids) - set(offered))
                    if len(ids) != len(set(ids)):
                        problems.append("an apartment_id is ranked twice")
                    if missing:
                        problems.append(f"apartments omitted: {missing}")
                    if extra:
                        problems.append(f"apartments not offered: {extra}")
        # evidence: findings cite catalogue fact ids (recorded ones; missing ones only when the verdict is hold/revise, i.e. the point is a gap)
        cat = facts.catalogue(g)
        findings, fp = facts.check_items(item.get("findings"), cat, ref=None, allow_missing=False, require_own=False, label="findings")
        problems += fp
        if isinstance(item.get("findings"), list) and len(item["findings"]) > REVIEW_FINDINGS_MAX:
            problems.append(f"findings: {len(item['findings'])} items exceed the response size limit of {REVIEW_FINDINGS_MAX}")
        if not any(x.get("kind", "evidence") == "evidence" for x in findings) and not fp:
            problems.append("findings empty: no evidence-cited finding (gap items alone cannot support a verdict)")
        reasons: list[dict] = []
        if verdict == "revise" and isinstance(refs, list) and refs:
            rr = item.get("replace_reasons")
            if not isinstance(rr, list):
                problems.append("replace_reasons missing")
            else:
                for r_ref in refs:
                    mine = [x for x in rr if isinstance(x, dict) and x.get("ref") == r_ref]
                    clean, rp = facts.check_items(mine, cat, ref=r_ref, allow_missing=True, require_own=True, label=f"replace_reasons[{r_ref}]")
                    problems += rp
                    if not clean:
                        problems.append(f"no evidence-cited reason to replace {r_ref}")
                    reasons += [{"ref": r_ref, **x} for x in clean]
        if not problems:   # r6: the one sentence shape about people coverage must agree with the code-derived count
            problems += facts.coverage_claim_problems(rationale, cov, "rationale")
            for k, x in enumerate(findings):
                problems += facts.coverage_claim_problems(x.get("note"), cov, f"findings[{k}]")
            for k, x in enumerate(reasons):
                problems += facts.coverage_claim_problems(x.get("note"), cov, f"replace_reasons[{k}]")
            for x in apts:
                problems += facts.coverage_claim_problems(x.get("reason"), cov, f"apartments[{x.get('apartment_id')}]")
        if problems:
            res["invalid_reason"] = "; ".join(problems)   # in-process diagnosis only (quotes the reply); persisted via persisted_invalid_reason
            res["invalid_code"], res["invalid_count"] = "review_unusable", len(problems)
        else:
            res.update(valid=True, verdict=verdict, replace_refs=list(refs), rating=rating, rationale=rationale.strip(), apartments=apts,
                       findings=findings, replace_reasons=reasons)
        results.append(res)
    return results


BRIEF_GROUP_KEYS = ("strengths", "tensions", "missing_facts", "discussion_points", "what_could_work", "what_could_cause_issues")
BRIEF_MEMBER_KEYS = ("strengths", "tensions", "missing_facts", "discussion_points")

# Explicit response schema shown to the model (9 Nov 2026, C200 live-1 / S1b: 14 of 24 captured brief replies failed the validator on
# shape and ownership). Built from the same key tuples the validator reads, so prompt and parser cannot drift apart; the validator
# (parse_staff_briefs / facts.check_items) is unchanged - this is guidance, not a relaxation.
BRIEF_NOTE_SCHEMA = {"fact_ids": ["<string: fact id exactly as listed in fact_ids or missing_fact_ids>"], "note": "<string: one plain-English sentence>",
                     "compared_with?": [{"ref": "<string: housemate ref>", "fact_ids": ["<string: that housemate's ids for the SAME fields>"]}]}
BRIEF_RESPONSE_SCHEMA = {"group": {k: ["<note>"] for k in BRIEF_GROUP_KEYS},
                         "members": [{"ref": "<string: one of the listed refs>"} | {k: ["<note>"] for k in BRIEF_MEMBER_KEYS}]}
BRIEF_SCHEMA_TEXT = ("Response schema (exact keys, no other keys, every list present even when empty, one <note> object per list item): "
                     + json.dumps(BRIEF_RESPONSE_SCHEMA) + " where <note> is " + json.dumps(BRIEF_NOTE_SCHEMA)
                     + " - the compared_with key (shown as compared_with?) is OPTIONAL and allowed in member notes only; group notes never carry it. "
                     "members holds exactly one object per listed ref, in any order, and no ref outside the list.\n")
BRIEF_REPAIR_HEAD = "REPAIR TURN (one only). Your previous reply was rejected by ALM's output validator. Exact problems, verbatim:"


def staff_briefs(packet: dict, meter: dict | None = None) -> dict:
    """T4(f): internal staff briefs for ONE approved group: one per person (by ref) plus one for the whole group. Every item is
    {fact_ids, note} and is validated against the fact catalogue built from the same packet (server/facts.py): facts exist,
    belong to the person described (a party member may cite the party-wide answers the enquirer gave for the party, labelled as
    such, never the enquirer's personal study/work/lifestyle facts), recorded facts for strengths/tensions, missing ones only
    under missing_facts / discussion points. Input: the launch-neutral packet (refs, structured practical facts, rule-check
    results, feature facts, the approved review). Returns {"group": {...}, "members": [{ref, ...}]} with both the structured
    items and a safe rendered string per item; raises AIUnavailable('staff brief unusable: ...') on any defect."""
    members = packet.get("members_for_ai") or []
    refs = [m["ref"] for m in members] + [r for m in members for r in (m.get("party_members") or [])]
    if not refs:
        raise AIUnavailable("staff brief: no members in packet", code="brief_packet")
    cat = facts.catalogue(packet)
    desc = {"members": members, "people": facts.coverage(packet), "individually_recorded": packet.get("individually_recorded") or {},
            "fact_ids": sorted(k for k, v in cat.items() if v["status"] == "recorded"), "missing_fact_ids": sorted(k for k, v in cat.items() if v["status"] == "missing"),
            "party_wide_fact_ids": sorted(k for k, v in cat.items() if v["scope"] == "party-wide" and v.get("source") != v.get("ref")),
            # r7-Q engineering hint: code-derived partition of the SAME missing ids - which belong to each person (their own unanswered
            # questions) and which are unrecorded apartment details (no person's) - so the model files gaps where the validator accepts them
            "missing_fact_ids_by_ref": {r: sorted(k for k, v in cat.items() if v["status"] == "missing" and v.get("ref") == r) for r in refs},
            "apartment_missing_fact_ids": sorted(k for k, v in cat.items() if v["status"] == "missing" and v.get("ref") is None),
            "approved_review": packet.get("review") or {},
            "apartments_best_first": [{k: a.get(k) for k in ("apartment_id", "type_label", "suburb", "lease_start", "lease_end", "total_weekly", "campus_km", "bills_included", "step_free_access", "step_free_source")}
                                      | {"rooms": a.get("allocation_for_ai"), "rule_checks": a.get("rule_checks") or [], "features_asked_for": a.get("features") or [],
                                         "campus_distance": a.get("campus_distance")}
                                      for a in (packet.get("apartments") or [])]}
    # S1b (9 Nov 2026, C200 live-1: 11 of 14 rejected briefs put a housemate's fact id into fact_ids): the item shape now SHOWS the
    # optional compared_with field and one worked example, states that group items never carry it and that ids are used exactly as
    # listed. Prompt/schema guidance only - the validator (facts.check_items) is unchanged.
    item = "{\"fact_ids\": [<this person's own ids>], \"note\": <one plain-English sentence>, \"compared_with\": [{\"ref\": <housemate ref>, \"fact_ids\": [<that housemate's ids for the SAME fields>]}] (optional, member items only)}"
    example = ("Worked example of a member comparison (U1's tensions): {\"fact_ids\": [\"U1.routine\"], \"compared_with\": [{\"ref\": \"U2\", \"fact_ids\": [\"U2.routine\"]}], "
               "\"note\": \"U1 is an early riser while U2 is a night owl, so quiet mornings in a shared room are worth agreeing.\"} - U2.routine appears ONLY under "
               "compared_with, never in U1's fact_ids. Group-level items (group.*) list everyone's ids in fact_ids and NEVER carry compared_with. Use fact ids exactly as "
               "listed in fact_ids / missing_fact_ids; do not invent or abbreviate ids (an id that is not listed makes the whole reply unusable).\n")
    prompt = (
        "You write INTERNAL briefing notes for Apartment Living Melbourne (ALM) staff about a proposed group of housemates whose AI review was approved. "
        "The notes help the team guide the people towards voluntarily accepting each other and signing one lease together.\n"
        + REVIEW_RULES_TEXT +
        "Each note is an object " + item + ". fact_ids must come from fact_ids (recorded facts) for strengths, tensions, what_could_work and "
        "what_could_cause_issues; missing_facts cite missing_fact_ids; discussion_points may cite either. A note about a person cites that person's own "
        "fact ids (party_wide_fact_ids are the enquirer's answers for the whole party and may be used for a party member, with that provenance); "
        "never describe one person with another person's personal facts. To COMPARE a person with a housemate, cite the person's own facts in fact_ids and "
        "the housemate's SAME fields under \"compared_with\": [{\"ref\": <housemate ref>, \"fact_ids\": [their ids for those same fields]}] - never put "
        "another person's ids in fact_ids, and a companion marked individually_recorded=false has no personal facts to compare. Response size limit for a "
        f"comparison (engineering limit, not a rule): at most {facts.COMPARE_REFS_MAX} counterparts and {facts.COMPARE_IDS_MAX} counterpart ids per note, separate "
        f"from the {FACT_IDS_MAX} own ids; compare the fields that matter rather than every housemate on every field. For refs marked "
        "individually_recorded=false, strengths/tensions may only rest "
        "on party-wide facts and missing_facts must list their unrecorded personal facts. Say what is likely to work and what could cause issues; do not "
        "promise or guarantee anything.\n"
        + example +
        "Where gaps go: an unknown is a MISSING FACT, never evidence - items marked kind 'gap' (or citing missing_fact_ids) may appear ONLY under missing_facts "
        "or discussion_points; strengths, tensions, what_could_work and what_could_cause_issues hold recorded facts only. A member's missing_facts list holds "
        "that person's own unanswered questions (missing_fact_ids_by_ref[ref]) and may be EMPTY; unrecorded apartment details (apartment_missing_fact_ids) "
        "belong under group.missing_facts or group.discussion_points, never in a member's lists.\n"
        "Wording of notes: refer to people by their PERSON REF (U1, U1-m1, U2 ...) in the sentence - never by name - so staff can map the note to the right "
        "person later. For FIELDS and FACTS use plain English in the sentence (say 'happy with a bunk bed' not bunk_ok; 'the allowed amount above the combined "
        "weekly budget, as a raw fraction' not allowance_fraction) and never paste field names or fact ids such as bunk_ok, allowance_fraction or U1.routine into "
        "the sentence - fact ids go only in fact_ids / compared_with. Quote recorded numbers exactly as given, with their stated unit (a fraction stays the same "
        "fraction, a weekly amount stays weekly): do not convert, round, add up or re-interpret them. "
        + TIMETABLE_GUIDANCE + BRIEF_TIMETABLE_PLACE +
        "Return ONLY JSON matching this schema. " + BRIEF_SCHEMA_TEXT +
        "Refs listed for members (exactly one member object each): " + ", ".join(refs) + ". group.strengths or "
        "group.what_could_work, and group.discussion_points, must not be empty; every member needs at least one strength/tension or one missing fact. "
        "Other lists (tensions, what_could_cause_issues, a member's tensions) may be EMPTY when the recorded facts show no material difference: "
        "do not manufacture a weakness to fill a list.\n"
    )

    def build(compact: bool, feedback: str = "") -> str:
        g_items = max(1, BRIEF_GROUP_ITEMS_MAX - 1) if compact else BRIEF_GROUP_ITEMS_MAX
        m_items = 1 if compact else BRIEF_MEMBER_ITEMS_MAX
        words = COMPACT_NOTE_WORDS if compact else NOTE_WORDS_ASKED
        size = (f"Response size limit: at most {g_items} notes in each group list and {m_items} in each member list (choose the most useful), "
                f"each note one sentence of at most {words} words with at most {FACT_IDS_MAX} fact ids."
                + (" Your previous reply was cut off at the output budget: be compact." if compact else "") + "\n\n")
        # the packet descriptor stays the LAST blank-line-separated block (offline replay tools key on it); feedback sits before it
        return prompt + size + feedback + json.dumps(desc, ensure_ascii=False, default=str)

    meter = meter if meter is not None else {}
    budget = brief_token_budget(len(refs))
    _turn(meter, "first")   # recorded as ATTEMPTED, before the call, so a failed first call still shows in the evidence
    try:
        text = _ask_bounded(build, budget, "staff brief", meter=meter)
    except AIUnavailable:
        if meter.get("compact"):
            _turn(meter, "compact")   # the compact retry was attempted (and failed or was cut off)
        raise
    if meter.get("compact"):
        _turn(meter, "compact")
    try:
        parsed = _json_from(text)
        return parse_staff_briefs(parsed, refs, packet)
    except AIUnavailable as e:
        if meter.get("compact") or not isinstance(e, AIUnusableReply) or not e.problems:
            # unusable compact reply: terminal as before (no repair after the size-limit pair); no JSON / malformed JSON: nothing
            # exact to feed back, unchanged behaviour
            raise _terminal_if_compact(meter, "staff brief", e)
        first = e
    # ONE exact-validator-feedback repair turn (9 Nov 2026): a single further call quoting the validator's own problem list and the
    # parsed first reply, same output budget, no compact retry of its own and no second repair. Counted in meter['asks'] and marked
    # separately (meter['repair'], meter['turns']) so call accounting distinguishes it from _ask_bounded's truncation retry.
    # Whatever comes back is final for this attempt: a second invalid reply, a cut-off, or a failed call all end the brief for staff.
    _count(meter)
    meter["repair"] = True
    _turn(meter, "repair")
    try:
        text2 = _ask(build(False, feedback=brief_repair_feedback(first.problems, parsed)), max_tokens=budget)
    except AIOutputTruncated:
        raise AINoRetry("staff brief repair reply exceeded the response size limit; no further automatic retry - staff decide", code="brief_repair_truncated")
    except AINoRetry:
        raise
    except AIUnavailable as e2:
        raise AINoRetry(f"staff brief repair call failed ({failure_kind(e2)}); no further automatic retry - staff decide", code="brief_repair_failed")
    try:
        return parse_staff_briefs(_json_from(text2), refs, packet)
    except AIUnavailable as e2:
        raise AIUnusableReply("staff brief unusable after one repair: " + str(e2)[:300], code="brief_unusable_after_repair",
                              problems=getattr(e2, "problems", None))


def _turn(meter: dict | None, kind: str) -> None:
    """Evidence of which kind of call each metered ask was: 'first', 'compact' (_ask_bounded's truncation retry) or 'repair'."""
    if meter is not None:
        meter.setdefault("turns", []).append(kind)


def brief_reply_projection(previous) -> dict:
    """The previous reply reduced to the schema's known keys only: group lists by BRIEF_GROUP_KEYS, members as ref + BRIEF_MEMBER_KEYS,
    each note as fact_ids (strings) / note (string) / compared_with ([{ref, fact_ids}]). Unknown keys at any level (e.g. extra
    top-level, member or note fields the model added) are dropped, so nothing outside the schema is echoed back. The note
    sentences themselves are kept as written (the validator's problems refer to them): this is a structural projection, not a
    proof that a note sentence contains no personal detail."""
    def note(x):
        if not isinstance(x, dict):
            return None
        out = {"fact_ids": [f for f in (x.get("fact_ids") or []) if isinstance(f, str)] if isinstance(x.get("fact_ids"), list) else x.get("fact_ids"),
               "note": x.get("note") if isinstance(x.get("note"), str) else None}
        if isinstance(x.get("compared_with"), list):
            out["compared_with"] = [{"ref": c.get("ref"), "fact_ids": [f for f in (c.get("fact_ids") or []) if isinstance(f, str)] if isinstance(c.get("fact_ids"), list) else c.get("fact_ids")}
                                    for c in x["compared_with"] if isinstance(c, dict)]
        if "kind" in x and isinstance(x.get("kind"), str):
            out["kind"] = x["kind"]   # the validator reads kind ('evidence' | 'gap'); keep it so a gap-placement problem can be fixed
        return out

    def lists(src, keys):
        return {k: [n for n in (note(x) for x in src.get(k)) if n] if isinstance(src.get(k), list) else src.get(k) for k in keys if k in src}
    prev = previous if isinstance(previous, dict) else {}
    group = prev.get("group") if isinstance(prev.get("group"), dict) else {}
    members = prev.get("members") if isinstance(prev.get("members"), list) else []
    return {"group": lists(group, BRIEF_GROUP_KEYS),
            "members": [{"ref": m.get("ref")} | lists(m, BRIEF_MEMBER_KEYS) for m in members if isinstance(m, dict)]}


def brief_repair_feedback(problems: list[str], previous) -> str:
    """The feedback block of the one repair turn: the validator's exact problems (numbered, verbatim, untruncated) and the previous
    reply projected onto the schema's known keys (brief_reply_projection) so no extra field the model added is echoed back and
    the descriptor block stays last. Instruction: return the complete corrected reply, fix every listed problem, add nothing new."""
    listed = "\n".join(f"{i}. {p}" for i, p in enumerate(problems, 1))
    prev = json.dumps(brief_reply_projection(previous), ensure_ascii=False, default=str)
    return (BRIEF_REPAIR_HEAD + "\n" + listed + "\nYour previous reply, as parsed: " + prev +
            "\nReturn the COMPLETE corrected JSON reply under the same schema: fix every listed problem (move or drop the offending items, cite only "
            "the listed ids for the right person), keep every other item as it was, and add no new claims. This is the only correction turn.\n\n")


def brief_token_budget(n_refs: int) -> int:
    """Output budget for a staff brief of n people under the item caps: about 90 tokens per note (25-30 words + up to 14 fact ids
    + JSON) across 18 group notes and 8 per person, with headroom; 3600 for 2 people, 7600 for 7; never above the ceiling."""
    return min(OUTPUT_TOKEN_CEILING, 2000 + 800 * max(1, int(n_refs)))


def parse_staff_briefs(out, refs: list[str], packet: dict | None = None) -> dict:
    """Strict: a group object with every list key, exactly one member object per ref (no unknown refs, no duplicates); every
    item {fact_ids, note} validated against the fact catalogue (existence, ownership, recorded vs missing, party-wide provenance);
    minimum content (group why-it-works + discussion points; per member at least one supported strength/tension or an explicit
    missing fact). Anything else raises AIUnavailable('staff brief unusable: ...'); nothing is defaulted. Validation is
    structural: it proves what each note rests on and who it is about, not that the words are a fair reading of it."""
    if not isinstance(out, dict) or not isinstance(out.get("group"), dict) or not isinstance(out.get("members"), list):
        raise AIUnusableReply("staff brief unusable: not an object with group and members", code="brief_unusable",
                              problems=["reply is not an object with a group object and a members list"])
    cat = facts.catalogue(packet or {})
    recorded_by_ref = {r: [k for k, v in cat.items() if v.get("ref") == r and v["status"] == "recorded"] for r in refs}
    problems: list[str] = []
    group: dict = {}
    for k in BRIEF_GROUP_KEYS:
        allow_missing = k in ("missing_facts", "discussion_points")
        if isinstance(out["group"].get(k), list) and len(out["group"][k]) > BRIEF_GROUP_ITEMS_MAX:
            problems.append(f"group.{k}: {len(out['group'][k])} items exceed the response size limit of {BRIEF_GROUP_ITEMS_MAX}")
        clean, pr = facts.check_items(out["group"].get(k), cat, ref=None, allow_missing=allow_missing, require_own=False, label=f"group.{k}",
                                      allow_gap=allow_missing)   # strengths / tensions / what could work or cause issues: recorded evidence only, no 'gap' disguise
        if k == "missing_facts":
            clean = [x for x in clean if any((cat.get(f) or {}).get("status") == "missing" for f in x["fact_ids"])] if not pr else clean
            if len(clean) != len(out["group"].get(k) or []) and not pr:
                problems.append("group.missing_facts cites only recorded facts")
        problems += pr
        group[k] = clean
    if not problems and not (group["strengths"] or group["what_could_work"]):
        problems.append("group: no supported strength / what could work")
    if not problems and not group["discussion_points"]:
        problems.append("group: no discussion points")
    seen: dict[str, dict] = {}
    members = []
    for m in out["members"]:
        if not isinstance(m, dict) or not isinstance(m.get("ref"), str):
            problems.append("a member object has no ref")
            continue
        ref = m["ref"]
        if ref not in refs:
            problems.append(f"unknown ref {ref!r}")
            continue
        if ref in seen:
            problems.append(f"ref {ref!r} briefed twice")
            continue
        body: dict = {"ref": ref}
        for k in BRIEF_MEMBER_KEYS:
            allow_missing = k in ("missing_facts", "discussion_points")
            if isinstance(m.get(k), list) and len(m[k]) > BRIEF_MEMBER_ITEMS_MAX:
                problems.append(f"{ref}.{k}: {len(m[k])} items exceed the response size limit of {BRIEF_MEMBER_ITEMS_MAX}")
            clean, pr = facts.check_items(m.get(k), cat, ref=ref, allow_missing=allow_missing, require_own=True, label=f"{ref}.{k}", allow_gap=allow_missing)
            if k == "missing_facts" and not pr:
                bad = [x for x in clean if not any((cat.get(f) or {}).get("status") == "missing" for f in x["fact_ids"])]
                if bad:
                    pr.append(f"{ref}.missing_facts cites only recorded facts")
            problems += pr
            body[k] = clean
        if not problems and not (body["strengths"] or body["tensions"] or body["missing_facts"]):
            problems.append(f"{ref}: empty brief (no supported strength/tension and no missing fact named)")
        seen[ref] = body
        members.append(body)
    missing = [r for r in refs if r not in seen]
    if missing:
        problems.append(f"no brief for {missing}")
    cov = facts.coverage(packet or {})
    if not problems:   # r6 coverage-count guard on every note
        for k in BRIEF_GROUP_KEYS:
            for i, x in enumerate(group[k]):
                problems += facts.coverage_claim_problems(x.get("note"), cov, f"group.{k}[{i}]")
        for b in members:
            for k in BRIEF_MEMBER_KEYS:
                for i, x in enumerate(b[k]):
                    problems += facts.coverage_claim_problems(x.get("note"), cov, f"{b['ref']}.{k}[{i}]")
    if problems:
        raise AIUnusableReply("staff brief unusable: " + "; ".join(problems)[:380], code="brief_unusable", problems=problems)
    group["coverage"] = cov   # code-derived, stored with every result (group and member bodies) so historic rows keep their own counts; the staff view shows cov["line"] (refs -> names there)
    for b in members:
        b["coverage"] = cov
    # safe factual rendering alongside the structured items
    def rendered(d: dict) -> dict:
        return {k: [facts.render(x, cat) for x in v] for k, v in d.items() if isinstance(v, list)}
    group["rendered"] = rendered(group)
    for b in members:
        b["rendered"] = rendered({k: b[k] for k in BRIEF_MEMBER_KEYS})
    return {"group": group, "members": [seen[r] for r in refs]}


INTRO_BANNED_PHRASES = ("perfect match", "perfect fit", "perfect group", "guarantee", "guaranteed", "no doubt", "will definitely", "certain to", "ideal match", "flawless")
_PRONOUNS = re.compile(r"\b(he|she|his|her|hers|him|himself|herself)\b", re.I)
_GENDERED = re.compile(r"\b(man|woman|male|female|guy|girl|lady|gentleman|boy|gal|bloke)\b", re.I)
INTRO_PROFILE_KEYS = ("ref", "first_name", "name", "country_of_birth", "doing", "school", "course", "diet", "social", "routine", "intro_sex", "intro_age", "email", "phone",
                      "contact_method", "whatsapp", "weekly_share", "room", "arrangement", "intro", "note", "is_lead", "lead_ref", "party_member_id", "enquiry_id", "intro_missing")
# What the model may see per person. r7-Q engineering choice implementing GP2: intro_sex / intro_age are NOT in the model payload - the
# supplied label is printed verbatim by intro_profile_block (deterministic), so the bio never writes sex or age.
INTRO_BIO_KEYS = ("first_name", "country_of_birth", "doing", "school", "course", "diet", "social", "routine", "intro")
def _sex_allows(sex_display) -> tuple[str, ...]:
    """r7 (Bill, 8 Nov 2026): NO supplied sex label licenses gendered words or pronouns in prose - for anyone. The supplied term
    is shown verbatim in that person's deterministic profile block only; bios use the name or neutral 'they'. Always ()."""
    return ()


LIFESTYLE_LABELS = {"diet": "diet", "social": "social style", "routine": "sleep pattern"}
# 4.1 "What will you be doing in Melbourne?" choices that are not Studying (handled with school/course) rendered as plain words
ACTIVITY_PLAIN = {"Other": "in Melbourne for other reasons"}
# an AI opening that already greets the group ("Hello Marco and Aiko, ...", "Hello from everyone at ALM!") must not be stacked under
# the fixed "Hello everyone," line (C200 real-draft review, 9 Nov 2026: every draft opened with two greetings)
GREETING_RE = re.compile(r"^\s*(?:hello|hi|hey|dear|welcome|g'day|greetings|good (?:morning|afternoon|evening))\b", re.I)


def intro_profile_block(m: dict) -> str:
    """Deterministic, code-built profile + contact block for ONE PERSON (GP2, Bill 8 Nov 2026): only values ALM actually holds
    for that person, verbatim; nothing inferred or inherited. Sex/age appear only when freshly supplied for introductions
    (intro_sex / intro_age); the country is the one given for them; study/work and diet / social / sleep pattern only from their
    own answers (a party member has none recorded). A party member's line shows their OWN email (never the lead's number)."""
    facts = []
    if m.get("intro_age") not in (None, ""):
        facts.append(f"{m['intro_age']}")
    if m.get("intro_sex"):
        facts.append(str(m["intro_sex"]))
    if m.get("country_of_birth"):
        facts.append(f"born in {m['country_of_birth']}")   # GP2: question 1.8 is "Country of birth" - stated as such, never as nationality or home country
    activity = None
    if m.get("doing") == "Studying" and m.get("school"):
        activity = f"studying{(' ' + m['course']) if m.get('course') else ''} at {m['school']}"
    elif m.get("doing") == "Studying":
        activity = "studying in Melbourne"
    elif m.get("doing"):
        # plain language for the customer email (T10): the 4.1 choice "Other" is a form code, never printed as "other in Melbourne"
        activity = ACTIVITY_PLAIN.get(str(m["doing"]), str(m["doing"]).lower() + " in Melbourne")
    if activity:
        facts.append(activity)
    name = (m.get("first_name") or m.get("name")) if m.get("is_lead", True) else (m.get("name") or m.get("first_name"))   # party members: the full name ALM holds
    head = f"{name}" + (f" ({', '.join(facts)})" if facts else "")
    if m.get("is_lead") is False and m.get("lead_ref"):
        head += "  [party member]"
    lines = [head]
    life = [f"{LIFESTYLE_LABELS[k]}: {m[k]}" for k in ("diet", "social", "routine") if m.get(k) not in (None, "")]
    if life:
        lines.append("  " + "; ".join(life))
    contact = []
    if m.get("email"):
        contact.append(f"email {m['email']}")
    if m.get("phone"):
        # "WhatsApp" only when the person said WhatsApp is their best contact method (question 1.5); otherwise a plain mobile
        contact.append(f"WhatsApp {m['phone']}" if (m.get("contact_method") == "WhatsApp" or m.get("whatsapp") is True) else f"mobile {m['phone']}")
    if contact:
        lines.append("  Contact: " + ", ".join(contact))
    elif m.get("note"):
        lines.append(f"  Contact: {m['note']}")
    if m.get("weekly_share") is not None:
        lines.append(f"  Guide share: about ${float(m['weekly_share']):,.0f}/week")
    return "\n".join(lines)


def _rent_block(apartment: dict, group: dict) -> str:
    total = group.get("total_weekly") or 0
    return "\n".join([f"Proposed apartment: {apartment.get('address')} ({apartment.get('type_label', apartment.get('type_code'))}), {group.get('lease_start')} to {group.get('lease_end')}"
                      + (f", {group['rate']}" if group.get("rate") else "") + ".",
                      group.get("liability_note") or f"The whole apartment is ${total:,.0f} per week on one lease; everyone on the lease is jointly and severally liable for the full rent, and the shares below are a guide only."])


def _ref_of(m: dict, i: int) -> str:
    return str(m.get("ref") or f"I{i}")


_DEMONYM = re.compile(r"\b[A-Z][a-z]{2,}(?:ish|ese|ian|ean|ani)\b")   # Swedish, Vietnamese, Indian, Korean, Pakistani ... (shape only; see _NOT_DEMONYMS)
_NOT_DEMONYMS = {"vegetarian", "pescatarian", "pescetarian", "flexitarian", "librarian", "musician", "physician", "historian", "electrician", "technician",
                 "civilian", "guardian", "median", "comedian", "dietician", "dietitian", "clean", "mean", "lean", "ocean", "bean", "jean", "sean", "dean",
                 "ian", "christian", "finish", "publish", "establish", "relish", "cherish", "fish", "dish", "wish",
                 "these", "cheese", "obese", "diocese"}
_NATIONALITY_WORDS = re.compile(r"\b(nationality|citizen(?:ship)?|national of|passport|expat|foreigner|immigrant|native of)\b", re.I)


# GP2 provenance (Increment A, 9 Nov 2026): the only recorded origin fact is question 1.8 'Country of birth'. Each RELATION a bio
# might attach to that country is a separate claim: 'from' (origin / moved from / native of), 'upbringing' (grew up / raised) and
# 'home' (home country, homeland, calls X home). None follows from a birthplace. Patterns are built around the recorded country so
# ordinary English elsewhere ('comes from work', 'home country' in a staff flag) is untouched. First-person forms are included so a
# person's OWN free introduction can be checked for the SAME relation about the SAME country.
_ORIGIN_RELATIONS = {
    "from": (r"\b(?:originally from|from|hails? from|comes? from|coming from|moved (?:here |to australia |to melbourne )?from|native of)\b[^.;,]{0,30}?\b{C}\b"
             r"|\b{C}\b[^.;,]{0,25}?\boriginally\b"),
    "upbringing": (r"\b(?:grew up in|was raised in|were raised in|raised in|brought up in)\b[^.;,]{0,30}?\b{C}\b"
                   r"|\b{C}\b[^.;,]{0,25}?\bwhere (?:they|she|he|I|we|the family) grew up\b"),
    "home": (r"\b(?:calls?|call|back home in|home country of|home country is|homeland is|homeland of)\b[^.;,]{0,30}?\b{C}\b"
             r"|\b{C}\b[^.;,]{0,25}?\b(?:their home|my home|our home|home country|homeland|as home)\b"),
}


def origin_wording(bio: str, country, own_intro=None) -> str | None:
    """Returns the offending phrase when `bio` attaches origin / upbringing / home wording to the recorded country of birth, else
    None. A plain 'born in <country>' passes. Provenance-aware without redefining GP2: the phrase is allowed only when the person's
    OWN free introduction states the SAME relation about the SAME country in their own words ('I grew up in Sweden' supports
    'grew up in Sweden'; 'I come from Sweden' or 'I was born in Sweden' does not support 'grew up in Sweden')."""
    if not country or not isinstance(bio, str):
        return None
    c = re.escape(str(country).strip())
    own = str(own_intro or "")
    for relation, template in _ORIGIN_RELATIONS.items():
        rx = re.compile(template.replace("{C}", c), re.I)
        for m in rx.finditer(bio):
            phrase = m.group(0)
            if re.search(r"\bborn\b", phrase, re.I):
                continue   # 'was born in Sweden' spanned by the gap: the birthplace statement itself is the recorded fact
            if own and rx.search(own):
                continue   # the same relation about the same country, supplied by the person themselves
            return phrase.strip()
    return None


def validate_intro_prose(prose: dict, members: list[dict]) -> list[str]:
    """Code checks on the AI prose, STRICTLY PER PERSON: every person (lead or party member) has a bio naming them; a bio uses no
    pronoun / gendered word unless THAT person freshly supplied a standard sex term, no number unless it is THAT person's supplied
    age, no other person's country, study place or course; group-level discussion text names nobody; no certainty promises.
    These are guards over wording, not proof that every sentence is true; the facts themselves come from the deterministic blocks."""
    problems = []
    if not isinstance(prose, dict):
        return ["prose is not an object"]
    for k in ("opening", "group_fit", "to_discuss", "closing"):
        if not isinstance(prose.get(k), str) or not prose[k].strip():
            problems.append(f"{k} missing")
    bios = prose.get("bios")
    if not isinstance(bios, dict):
        return problems + ["bios missing"]
    refs = [_ref_of(m, i) for i, m in enumerate(members, start=1)]
    extra = set(bios) - set(refs)
    if extra:
        problems.append(f"bios for unknown refs {sorted(extra)}")
    others_terms = {}
    for i, m in enumerate(members, start=1):
        terms = [str(m[k]) for k in ("country_of_birth", "school", "course") if m.get(k)]
        others_terms[_ref_of(m, i)] = terms
    for i, m in enumerate(members, start=1):
        ref = _ref_of(m, i)
        bio = bios.get(ref)
        name = m.get("first_name") or m.get("name") or ""
        if not isinstance(bio, str) or not bio.strip():
            problems.append(f"no bio for {ref}")
            continue
        if name and name.lower() not in bio.lower():
            problems.append(f"bio {ref} does not name {name}")
        allowed_sex = _sex_allows(m.get("intro_sex"))
        for w in (*_PRONOUNS.findall(bio), *_GENDERED.findall(bio)):
            if w.lower() not in allowed_sex:
                problems.append(f"bio {ref} uses a sex label, gendered word or pronoun ({w}); the supplied term appears only in the profile line")
                break
        allowed_nums = {str(m.get("intro_age"))} if m.get("intro_age") not in (None, "") else set()
        for n in re.findall(r"\b\d+\b", bio):   # any standalone number of any length (a birth year or phone number is not the supplied age); digits inside a word (Companion1, 3D) are not numbers
            if n not in allowed_nums:
                problems.append(f"bio {ref} contains a number ({n}) that is not this person's supplied age")
                break
        # GP2 provenance: the only origin fact is question 1.8 "Country of birth"; a bio may not restate it as a nationality,
        # citizenship or demonym (conservative: capitalised demonym-shaped words not present in this person's own facts/words)
        own_text = " ".join(str(x.get(k) or "") for x in members for k in ("name", "first_name")).lower() + " " + \
            " ".join(str(m.get(k) or "") for k in ("intro", "school", "course", "diet", "social", "routine")).lower()
        for w in _DEMONYM.findall(bio):
            if w.lower() not in _NOT_DEMONYMS and w.lower() not in own_text:
                problems.append(f"bio {ref} states a nationality/demonym ({w}); only 'born in <country of birth>' is a recorded fact")
                break
        if _NATIONALITY_WORDS.search(bio):
            problems.append(f"bio {ref} uses nationality/citizenship wording")
        hit = origin_wording(bio, m.get("country_of_birth"), m.get("intro"))
        if hit:
            problems.append(f"bio {ref} turns the recorded country of birth into origin, home or upbringing wording ({hit}); only 'born in {m.get('country_of_birth')}' is recorded")
        own = {t.lower() for t in others_terms[ref]}
        for oref, terms in others_terms.items():
            if oref == ref:
                continue
            for t in terms:
                if t.lower() not in own and re.search(r"\b" + re.escape(t) + r"\b", bio, re.I):
                    problems.append(f"bio {ref} mentions another person's fact ({t})")
                    break
    text_all = " ".join(str(v) for v in (prose.get("opening"), prose.get("group_fit"), prose.get("to_discuss"), prose.get("closing"), *bios.values()) if v)
    low = text_all.lower()
    for ph in INTRO_BANNED_PHRASES:
        if ph in low:
            problems.append(f"certainty wording: {ph!r}")
            break
    td = str(prose.get("to_discuss") or "")
    for m in members:
        n = m.get("first_name") or m.get("name")
        if n and re.search(r"\b" + re.escape(n) + r"\b", td, re.I):
            problems.append(f"to_discuss singles out {n}")
            break
    return problems


def _identity_terms(members: list[dict]) -> list[str]:
    """Words that may appear only in a person's own bio / profile block, never in group-level prose: every supplied country, sex
    term and age."""
    out = []
    for m in members:
        for k in ("country_of_birth", "intro_sex"):
            if m.get(k):
                out.append(str(m[k]))
        if m.get("intro_age") not in (None, ""):
            out.append(str(m["intro_age"]))
    return out


EXCLUDED_TRAIT_REASONING = ("same age", "similar age", "same sex", "same gender", "same country", "same background", "same nationality", "same culture",
                            "similar background", "similar culture", "all girls", "all boys", "all women", "all men", "all female", "all male", "fellow countrymen",
                            "both from", "all from", "age group", "age range", "young women", "young men")


# Household-habit domains a collective claim can be about -> the recorded answer field that would have to support it. A claim
# ("the household's routines line up", "everyone keeps a tidy home") is licensed only when EVERY person in the introduction has
# their OWN recorded answer for that field in the basis coverage (status 'recorded', basis 'own'); a lead-reported party-wide fact,
# a missing answer, or study/work facts never license a habit claim. Sentences that INVITE discussion are exempt.
HABIT_DOMAINS = {
    "routine": re.compile(r"\b(routines?|sleep(?:ing)?|bed ?times?|night owls?|early (?:risers?|birds?)|(?:similar|same|regular|late|early) hours|schedules?|mornings?|evenings?)\b", re.I),
    "tidy": re.compile(r"\b(tidy|tidiness|untidy|clean(?:liness|ing)?|neat(?:ness)?|messy|chores?|housework)\b", re.I),
    "social": re.compile(r"\b(social(?:ly|ising|izing)?|sociable|outgoing|quiet|introverts?|extroverts?|homebod(?:y|ies)|company)\b", re.I),
    "guests": re.compile(r"\b(guests?|visitors?|friends over|parties|hosting)\b", re.I),
    "diet": re.compile(r"\b(diets?|dietary|vegetarian|vegan|halal|kosher|cook(?:s|ing)?|meals?|food)\b", re.I),
}
_INVITATION = re.compile(r"\b(discuss|talk|agree|chat|decide|compare|work out|sort out|ask|check|share how|let each other know|worth (?:raising|covering|agreeing))\b", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# succ3 (10 Nov 2026, N1): "so" splits a clause only as a conjunction ("so that", "so everyone's expectations ..."), never as the
# intensifier of a habit degree ("everyone is so social anyway" stays one clause); causal subordinators (since / because / given that /
# now that / seeing that) start a clause of their own, because what follows them is asserted as a settled fact, whatever the main
# clause was doing ("Agree quiet hours together, which is easy since everyone is quiet" asserts that everyone is quiet).
_CLAUSE = re.compile(r"\s*[,;:]\s*|\s+-\s+|\s+\u2014\s+|\s+\b(?:which|but|and|while|although|though|since|because|given that|now that|seeing that|"
                     r"so(?=\s+(?:that|everyone|everybody|you|we|they|it|the|this|these|those|there|each|all|please|nobody|no one)\b))\b\s+", re.I)
# Who a clause is about (r7-Q engineering implementation of the neutral-prose / GP2 grounding rules - subject scoping only; the
# coverage requirement itself - own recorded answers - is unchanged and no new register ruling is made here):
#   _ALL_PEOPLE  - everyone / the household / the group / you all ...: the claim is about EVERY person in the introduction;
#   named people - whole-word first names of current members ("Alex and Bailey recorded matching routines"): the claim is about
#                  exactly those people (either order; a list keeps all its names), each of whom needs their OWN recorded answer;
#   _SAMENESS    - similar / matching / line up ... with no names: about everyone (sameness across the household).
# A name shared by two members, or a single name with a sameness word, resolves to nobody in particular -> everyone.
# Statements about the LISTING ("a separate kitchen for cooking", "a quiet street") have no people subject and are not habit claims,
# but apartment words never narrow a clause that does have a people subject.
_ALL_PEOPLE = re.compile(r"\b(everyone|everybody|all of you|you all|you both|both of you|the household|household[\u2019']s|the group|group[\u2019']s|housemates|flatmates|"
                         r"each of you|all (?:three|four|five|six) of you|the (?:two|three|four|five|six) of you|you (?:each|share|keep|like|prefer|tend|are all|have)|"
                         r"they (?:all|both|each|share|keep))\b", re.I)
_SAMENESS = re.compile(r"\b(similar|same|matching|match(?:es|ing)?|align(?:s|ed)?|line up|lines up|compatible|in common|shared|alike)\b", re.I)
_LISTING = re.compile(r"\b(apartment|listing|flat|unit|building|balcony|kitchen|bathroom|bedrooms?|rooms?|living (?:area|room)|street|location|suburb|features?|property|lease)\b", re.I)
# 9 Nov 2026 (triage fix 3): inside a sentence already in invitation mode ("Chat together about tidiness, chores, cooking and how
# social you each like the home to be"), a list item that is an indirect question (how / what / whether / when ... ) is a TOPIC of
# that invitation, never an assertion, even when it carries a whole-household marker ("you each"). Any other clause with a
# household / named / sameness marker still re-decides for itself, so "Everyone should discuss routines, and everyone is very tidy"
# keeps rejecting its second clause.
_TOPIC_CLAUSE = re.compile(r"^\s*(?:and\s+|or\s+|also\s+|plus\s+)?(?:how|what|whether|when|where|which|who|whose|why|if)\b", re.I)
# ... unless the "question" presupposes the very habit it names (second-round repair, 9 Nov 2026): a factive wh-item that
# predicates a graded habit of its subject ("how everyone is very tidy", "why everyone is very social") asserts that habit as
# settled - the invitation only invites talk about it. Two shapes, nothing broader: (a) "why ..." (always presupposes its
# complement); (b) another wh-word whose subject is followed by a copula / habit verb and an intensifier ("is very", "are all so",
# "keep everything really"). "how social you each like the home to be" and "how tidy everyone is" stay open questions (no copula
# + intensifier), "whether / if ..." stay open always.
# succ3 (10 Nov 2026, N1) adds two more factive shapes, nothing broader: (c) an exclamative "how" + intensifier ("how very tidy everyone
# already is" - a degree stated, not asked); (d) a wh-item whose subject is followed by "already / always" and a copula or habit verb
# ("how tidy everyone already is") - the adverb presupposes the habit as settled. "how tidy you each like the home" stays an open question.
_PRESUPPOSED_ITEM = re.compile(r"^\s*(?:and\s+|or\s+|also\s+|plus\s+)?(?:why\b|how\s+(?:very|really|extremely|so|incredibly|super|truly|remarkably)\b|"
                               r"(?:how|what|when|where|which|who|whose)\b[^.;]*?\b(?:already|always)\s+(?:is|are|was|were|keeps?|stays?|likes?|loves?|prefers?|tends?)\b|"
                               r"(?:how|what|when|where|which|who|whose)\b[^.;]*?\b"
                               r"(?:is|are|'s|'re|was|were|keeps?|stays?|likes?|loves?|prefers?|tends?)\s+(?:all\s+|both\s+|everything\s+|things\s+|the\s+(?:home|place|flat|apartment|house)\s+)?"
                               r"(?:very|really|extremely|so|quite|super|incredibly|pretty|highly|particularly|always|rather|truly)\b)", re.I)
_ASSERTIVE_PARTS = ("opening", "group_fit", "closing")   # to_discuss is a list of topics; a bare habit word there is not an assertion


def _own_coverage(basis) -> dict[str, set[str]]:
    """current intro ref -> fields that person's OWN recorded answer covers, read from structured basis items (ai_queue.intro_basis_items).
    Legacy string bases carry no coverage -> {}."""
    out: dict[str, set[str]] = {}
    for item in basis or []:
        if not isinstance(item, dict):
            continue
        cov = item.get("coverage") or {}
        for sj in item.get("subjects") or []:
            pref, ref = sj.get("packet_ref"), sj.get("ref")
            if not pref or not ref:
                continue
            for field, c in (cov.get(pref) or {}).items():
                if isinstance(c, dict) and c.get("status") == "recorded" and c.get("basis") == "own":
                    out.setdefault(ref, set()).add(field)
    return out


def _basis_subject_sets(basis) -> list[frozenset]:
    """the set of current intro refs each structured basis note is about (legacy strings -> nothing)"""
    out = []
    for item in basis or []:
        if isinstance(item, dict):
            out.append(frozenset(sj.get("ref") for sj in (item.get("subjects") or []) if isinstance(sj, dict) and sj.get("ref")))
    return out


def _member_names(members: list[dict]) -> dict[str, set[str]]:
    """lower-cased first name -> refs of the current members carrying it (two people may share one)"""
    names: dict[str, set[str]] = {}
    for i, m in enumerate(members, start=1):
        n = str(m.get("first_name") or m.get("name") or "").strip()
        if n:
            names.setdefault(n.lower(), set()).add(_ref_of(m, i))
    return names


def _protect_name_lists(sent: str, names: dict[str, set[str]]) -> str:
    """'Alex and Bailey ...' / 'Alex, Bailey and Casey ...' must stay ONE subject through the clause split: the connectors inside a
    list of member names become '&' (not a clause separator). Names are matched whole-word, case-insensitively, in any order."""
    if not names:
        return sent
    name_rx = "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")"
    # a list is a name followed by one or more ", Name" / " and Name" / ", and Name" links in any combination ("Casey and Alex and Bailey",
    # "Alex, Bailey and Casey", "Alex, Bailey, and Casey"); every connector inside it becomes '&'
    lst = re.compile(r"\b" + name_rx + r"(?:\s*,?\s+and\s+" + name_rx + r"|\s*,\s*" + name_rx + r")+\b", re.I)
    def fix(m):
        return re.sub(r"\s*,?\s+and\s+|\s*,\s*", " & ", m.group(0))
    return lst.sub(fix, sent)


def _named_refs(clause: str, names: dict[str, set[str]]) -> tuple[set[str], bool]:
    """(refs of members named in the clause, ambiguous) - ambiguous when a name in the clause belongs to two or more members"""
    refs: set[str] = set()
    ambiguous = False
    for n, owners in names.items():
        if re.search(r"\b" + re.escape(n) + r"\b", clause, re.I):
            if len(owners) > 1:
                ambiguous = True
            refs |= owners
    return refs, ambiguous


def _study_literals(members: list[dict]) -> list[str]:
    """Recorded study answers of the current members (a course called 'Timetable Design' is the fact's own words). Free-text intros are
    NOT included: group_fit has a no-bios basis and one person's bio must not license a claim about everyone's timetable."""
    return [str(m.get(k)) for m in members for k in ("course", "school", "doing") if m.get(k) not in (None, "")]


# succ4 (10 Nov 2026, degree fidelity): an asserted collective habit must not only be COVERED by each person's own recorded answer, the
# DEGREE it states must be one those answers can stand for. Live capture REQ140 (cycle1-post, group 3) wrote "each described themselves
# as tidy and social" while all three recorded 'Relaxed'; the coverage check passed (tidy covered for everyone) and the draft was refused
# only because its to_discuss named people. Scope of the lexicon, deliberately: TIDINESS and SOCIAL STYLE only, and only the recorded
# options' own words or their plain intensified / negated forms - no personality inference (no 'outgoing', 'introvert', bare 'private'
# or 'independent', no copula + 'quiet': "everyone has a private bedroom" is about rooms, a quiet night is a context). Words that state
# no degree ('clean', 'tidiness', 'quiet hours', 'friendly') are never read as one, and neither is a degree word directly after 'how'
# when that how-item is an OPEN degree question ("how tidy you each like the home to be" asks the degree instead of stating it); the
# how-item is read with the existing _PRESUPPOSED_ITEM shapes, so "how very tidy ..." and "how tidy everyone already is" still state
# a degree (ckpt5: no blanket 'how' exemption). Quoted text gets NO exemption here: the group parts
# carry the drafter's claims, and a person's own words belong in that person's bio, which these checks do not read.
_INTENSIFIER = r"(?:very|really|extremely|super|incredibly|highly|particularly|so|truly|remarkably|exceptionally|especially)"
HABIT_DEGREES: dict[str, tuple[tuple[str, frozenset], ...]] = {   # field -> ((pattern, options the wording can stand for), ...); first match on a span wins
    "tidy": ((r"\b(?:" + _INTENSIFIER + r"\s+(?:tidy|clean|neat)|spotless|immaculate|meticulously tidy|neat freaks?|clean freaks?)\b", frozenset({"Very tidy"})),
             (r"\b(?:average|middle[- ]of[- ]the[- ]road|moderately|reasonably|fairly)\s+(?:(?:about|on|with|when it comes to)\s+)?(?:tidy|tidiness|clean|cleaning|neat|neatness|housework|chores)\b", frozenset({"Average"})),
             (r"\b(?:(?:relaxed|laid-?back|easy-?going|easygoing|not (?:too |very |especially |particularly )?(?:fussy|bothered|strict))\s+(?:about|on|with|around|regarding|when it comes to)\s+"
              r"(?:the\s+)?(?:tidiness|tidying|tidy|cleaning|cleanliness|neatness|mess|housework|chores|clutter)|messy|untidy)\b", frozenset({"Relaxed"})),
             (r"\b(?:tidy|neat)\b(?!\s+(?:up|away|or|hours?))", frozenset({"Very tidy", "Average"}))),   # bare 'tidy' states a positive degree: 'Relaxed' is the scale's other end
    "social": ((r"\b" + _INTENSIFIER + r"\s+(?:social|sociable)\b", frozenset({"Very social"})),
               (r"\bfriendly (?:but|&) independent\b", frozenset({"Friendly but independent"})),   # '&' = the option phrase protected through the clause split
               (r"\bquiet (?:and|&) private\b", frozenset({"Quiet and private"})),
               (r"\b(?:social|sociable)\b(?!\s+(?:hours?|media|life|styles?|habits?|calendar|events?|side|butterfl\w*))", frozenset({"Very social", "Friendly but independent"}))),   # bare 'social' excludes 'Quiet and private'
}
_HABIT_DEGREE_RX = {f: tuple((re.compile(p, re.I), opts) for p, opts in pats) for f, pats in HABIT_DEGREES.items()}
# the two social options whose own wording contains a conjunction must stay ONE phrase through the clause split (the splitter breaks on
# 'and' / 'but'): the conjunction becomes '&', exactly as _protect_name_lists does for a list of member names
_OPTION_PHRASES = re.compile(r"\b(quiet) and (private)\b|\b(friendly) but (independent)\b", re.I)


def _protect_option_phrases(sent: str) -> str:
    return _OPTION_PHRASES.sub(lambda m: f"{m.group(1) or m.group(3)} & {m.group(2) or m.group(4)}", sent)
_HABIT_LABEL = {"tidy": "tidiness", "social": "social style"}
# How a stated degree is READ (never a reason to skip it):
#   plain      - "everyone is very tidy", "Alex & Bailey are both relaxed about tidiness": every person in scope must have recorded it;
#   partitive  - "two very tidy people", "some of you are very tidy", "one relaxed about tidiness": at least that many (a number) or at
#                least one (some / most / another / the rest ...) of the people in scope recorded it; never "the two of you" / "all three of you";
#   negated    - "nobody is very tidy", "none of you are tidy", "not very social": no person in scope recorded it;
#   range      - a degree that IS an endpoint of an explicit range / mix construction ("from quiet and private to very social", "ranging
#                from very tidy to relaxed about tidiness", "a mix of very tidy and relaxed ...") is an existence claim: at least one
#                person in scope recorded it. The construction is bound to the degree spans themselves: 'from' / 'of' must be followed
#                directly by a degree and 'to' directly by another, so a temporal "from day one to the end of the lease" (before OR
#                after the degree) leaves "everyone is very tidy" a plain claim.
# Nothing else downgrades a plain degree: a difference word in the clause or sentence ("despite different tidiness answers", "rather than
# relaxed about tidiness") changes nothing, and two plain degrees of one field for the same people in one clause contradict each other.
_DIFFERENCE = re.compile(r"\b(differ|differs|differing|different|differently|differences?|a mix|the mix|mixture|mixed|range from|ranges from|a range of|whereas|versus|contrast|"
                         r"respectively|not everyone|not all)\b", re.I)
_NUMBER = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_PARTITIVE = re.compile(r"(?<!the )(?<!all )(?<!both )\b(?P<q>one|two|three|four|five|six|some|most|many|several|a couple|a few|half|another|the other|the others|the rest|others)\b"
                        r"(?:\s+of\s+(?:you|them|the\s+\w+))?\s+(?:\w+\s+){0,2}$", re.I)
_RANGE_FROM = re.compile(r"\b(?:rang(?:e|es|ing)\s+)?from\s+(?:being\s+)?", re.I)   # ... <degree> to <degree>: both endpoints must be degree spans
_RANGE_TO = re.compile(r"\s+to\s+(?:being\s+)?", re.I)
_RANGE_MIX = re.compile(r"\ba (?:mix|mixture|spread) of\s+(?:being\s+)?", re.I)   # ... <degree>[, <degree>][ and|&|or <degree>]
_RANGE_SEP = re.compile(r"\s*(?:,|and|&|or)\s+", re.I)
_HOW_BEFORE = re.compile(r"\bhow\s+$", re.I)   # the degree word directly follows 'how': its how-item is a question unless _PRESUPPOSED_ITEM says otherwise
# the additive "not only X but also Y" affirms X, so 'not' followed by 'only' / 'just' / 'merely' is not a denial
_NEGATION = re.compile(r"\b(?:not(?!\s+(?:only|just|merely)\b)|no one|nobody|none|never|neither|nor|isn't|aren't|wasn't|weren't|hasn't|haven't|doesn't|don't|didn't|without)\b(?:\s+\w+){0,6}\s+$", re.I)


def _range_endpoints(clause: str, spans: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """The degree spans that are endpoints of an explicit range / mix construction in `clause`: 'from <degree> to <degree>' (optionally
    'ranges / ranging from'), or 'a mix / mixture / spread of <degree>[, <degree>][ and <degree>]'. A degree span must begin exactly where
    the keyword ends, so words of time or place after 'from' / 'to' never make a later degree an existence claim."""
    starts = {a: (a, b) for a, b in spans}
    out: set[tuple[int, int]] = set()
    for m in _RANGE_FROM.finditer(clause):
        first = starts.get(m.end())
        if not first:
            continue
        to = _RANGE_TO.match(clause, first[1])
        second = starts.get(to.end()) if to else None
        if second:
            out.update((first, second))
    for m in _RANGE_MIX.finditer(clause):
        span = starts.get(m.end())
        while span:
            out.add(span)
            sep = _RANGE_SEP.match(clause, span[1])
            span = starts.get(sep.end()) if sep else None
    return out


def habit_degree_terms(clause: str) -> dict[str, list[tuple[str, frozenset, str, int]]]:
    """field -> [(wording as written, recorded options it can stand for, reading, minimum)] for the habit DEGREES this clause states.
    Each span is read once (the most specific pattern of a field wins); wordings that state no degree are absent. `reading` is 'plain',
    'partitive' (minimum = the number stated, else 1), 'negated' or 'range' (an endpoint of an explicit range / mix construction, minimum 1)."""
    found: list[tuple[str, re.Match, frozenset]] = []
    for field, pats in _HABIT_DEGREE_RX.items():
        taken: list[tuple[int, int]] = []
        for rx, opts in pats:
            for m in rx.finditer(clause):
                if any(a < m.end() and m.start() < b for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                found.append((field, m, opts))
    endpoints = _range_endpoints(clause, [(m.start(), m.end()) for _, m, _ in found])
    out: dict[str, list[tuple[str, frozenset, str, int]]] = {}
    for field, m, opts in found:
        before = clause[:m.start()]
        how = _HOW_BEFORE.search(before)
        if how and not _PRESUPPOSED_ITEM.match(clause[how.start():]):
            continue   # "how tidy you each like the home to be": an open degree question states no degree
        part = _PARTITIVE.search(before)
        if _NEGATION.search(before):
            reading, minimum = "negated", 0
        elif part:
            reading, minimum = "partitive", _NUMBER.get(part.group("q").lower(), 1)
        elif (m.start(), m.end()) in endpoints:
            reading, minimum = "range", 1
        else:
            reading, minimum = "plain", 0
        out.setdefault(field, []).append((m.group(0), opts, reading, minimum))
    return out


def _degree_problem(k: str, field: str, word: str, allowed: frozenset, reading: str, minimum: int, scope: list[str], habit_answers: dict) -> str | None:
    """None when the recorded answers of `scope` can stand for the stated degree under its reading; otherwise the problem sentence (refs
    and wording only - never a recorded value). A person in scope with no value to compare fails closed."""
    label = _HABIT_LABEL.get(field, field)
    values = {r: (habit_answers.get(r) or {}).get(field) for r in scope}
    unknown = [r for r, v in values.items() if v in (None, "")]
    if unknown:
        return f"{k} states a {label} degree ('{word}') that cannot be checked against a recorded answer for {', '.join(unknown)}"
    holders = [r for r, v in values.items() if v in allowed]
    if reading == "plain":
        wrong = [r for r in scope if r not in holders]
        return None if not wrong else f"{k} states a {label} degree ('{word}') that does not match the recorded answer of {', '.join(wrong)}"
    if reading == "negated":
        return None if not holders else f"{k} denies a {label} degree ('{word}') that is the recorded answer of {', '.join(holders)}"
    if reading == "range":
        return None if holders else f"{k} names a {label} degree ('{word}') in a range that no recorded answer of the people described holds"
    if len(holders) < minimum:   # partitive
        return f"{k} states a {label} degree ('{word}') for {minimum} of the people described, but fewer recorded answers say so"
    return None

# Succ3 (10 Nov 2026): the version of the customer-introduction WORDING checks (validate_intro_prose / validate_group_prose /
# habit_claim_problems and their clause grammar). Stored with every introduction draft (ai_queue.record_intro_draft) and compared at
# read time (ai_queue.intro_state): a draft accepted under an earlier wording check is history, never the current introduction, even
# when the compatibility evidence is unchanged (members with notes_status 'none' get no notes bump). No provider call follows by
# itself - staff draft again when they need a current introduction. Bump whenever these checks tighten or loosen.
INTRO_PROSE_CONTRACT = "2026-11-10.n4-habit-degree-fidelity"   # succ4: asserted habit degrees must match each person's recorded answer (habit_answers); n1 shapes retained


def habit_claim_problems(prose: dict, members: list[dict], basis, habit_answers: dict | None = None) -> list[str]:
    """Household-habit assertions in the group prose must be covered by the OWN recorded answers (basis coverage) of the people
    the clause is about: every person for everyone/household/sameness wording, exactly the named people for a named comparison
    (which must also be the subject of a basis note about them), nobody narrower than that. A habit asserted with no people
    subject at all (and not about the listing) is refused as unclear. Discussion invitations are allowed regardless.
    Wording guard only (it cannot prove a sentence true).
    succ4: with `habit_answers` given (intro ref -> recorded lifestyle answer values, server-side only - see ai_queue.intro_habit_answers;
    None = frozen checks only), a covered tidiness / social-style assertion must also state a degree the recorded answers can stand for
    (HABIT_DEGREES); a covered person without a value to check against fails closed."""
    refs = [_ref_of(m, i) for i, m in enumerate(members, start=1)]
    check_degrees = habit_answers is not None   # succ4: None = the frozen checks only; a dict (even empty) = the route's validation context is active
    own = _own_coverage(basis)
    names = _member_names(members)
    subject_sets = _basis_subject_sets(basis)
    problems = []
    for k in ("opening", "group_fit", "to_discuss", "closing"):
        for sent in _SENTENCE.split(str(prose.get(k) or "")):
            scope: list[str] | None = None   # None = no people subject so far in this sentence
            invited = False
            clauses = [cl for cl in _CLAUSE.split(_protect_name_lists(_protect_option_phrases(sent), names)) if cl and cl.strip()]
            for clause in clauses:   # clause by clause: "everyone has matching routines, which you can discuss" still asserts
                degrees = habit_degree_terms(clause) if check_degrees else {}
                named, ambiguous = _named_refs(clause, names)
                topic = invited and bool(_TOPIC_CLAUSE.match(clause)) and not _PRESUPPOSED_ITEM.match(clause)   # an indirect-question item of an invitation already under way (fix 3), not a presupposed habit
                if _ALL_PEOPLE.search(clause):        # a whole-household marker wins over any names in the same clause
                    scope, invited = list(refs), topic or bool(_INVITATION.search(clause))
                elif named and not ambiguous and (len(named) >= 2 or not _SAMENESS.search(clause)):
                    scope, invited = sorted(named), topic or bool(_INVITATION.search(clause))   # named comparison: those people only
                elif named:                           # an ambiguous name, or one name 'matching' nobody in particular -> everyone
                    scope, invited = list(refs), topic or bool(_INVITATION.search(clause))
                elif _SAMENESS.search(clause):
                    if scope is None:                 # nameless sameness opens a sentence: about everyone
                        scope = list(refs)
                    invited = topic or bool(_INVITATION.search(clause))   # inside a named sentence ("Alex & Bailey's routines, tidiness and social styles line up") the named scope carries
                elif _INVITATION.search(clause):
                    invited = True
                # ... so "The household's routines, tidiness and social styles line up" checks all three fields, while
                # "Everyone should discuss routines, tidiness and cooking" stays an invitation through its list
                if invited:
                    continue
                if k in _ASSERTIVE_PARTS:
                    tt = facts.timetable_inference(clause, routine_cited=True, literal=_study_literals(members))   # 9 Nov 2026: labels alone never establish timetable alignment
                    if tt:
                        problems.append(f"{k} asserts that study timetables or class schedules line up ('{tt}'); the recorded facts do not support this timetable alignment")
                        return problems
                asserted = [field for field, rx in HABIT_DOMAINS.items() if rx.search(clause)]
                asserted += [f for f in degrees if f not in asserted]   # a degree word ('spotless', 'introverted') names its habit
                if not asserted:
                    continue
                if scope is None:
                    if k in _ASSERTIVE_PARTS and not _LISTING.search(clause):
                        problems.append(f"{k} asserts a household habit ({asserted[0]}) without saying who it is about")
                        return problems
                    continue   # a statement about the listing, or a bare topic in to_discuss
                for field in asserted:
                    lacking = [r for r in scope if field not in own.get(r, set())]
                    if len(scope) == len(refs):
                        if lacking:
                            problems.append(f"{k} asserts a shared household habit ({field}) that is not covered by every person's own recorded answer")
                            return problems
                    else:
                        if lacking:
                            problems.append(f"{k} asserts a habit ({field}) for named people that is not covered by their own recorded answers ({', '.join(lacking)})")
                            return problems
                        if not any(set(scope) <= ss for ss in subject_sets):
                            problems.append(f"{k} compares named people ({', '.join(scope)}) who are not together the subject of any basis note")
                            return problems
                    # succ4: covered, so every DEGREE stated must be one the recorded answers of these people can stand for, under its reading
                    # (plain / partitive / negated / range - see habit_degree_terms). Nothing is skipped for difference words or mixes: a plain
                    # degree is compared with every person in scope whatever else the sentence says; two plain degrees of one field for the same
                    # people in one clause contradict each other; only an explicit range / mix construction reads as existence claims.
                    stated = list(degrees.get(field) or [])
                    if len({opts for _, opts, reading, _ in stated if reading == "plain"}) >= 2:
                        problems.append(f"{k} states more than one {_HABIT_LABEL.get(field, field)} degree for the same people in one clause")
                        return problems
                    for word, allowed, reading, minimum in stated:
                        problem = _degree_problem(k, field, word, allowed, reading, minimum, scope, habit_answers)
                        if problem:
                            problems.append(problem)
                            return problems
    return problems


def validate_group_prose(prose: dict, members: list[dict], basis=None, habit_answers: dict | None = None) -> list[str]:
    """group_fit / to_discuss / opening / closing must rest on the neutral basis only: no member's country, sex term or age, no
    gendered words, no 'same age/background' style reasoning (those were never selection criteria). With `basis` given (always
    from draft_introduction), collective habit claims must be covered by every person's own recorded answers (habit_claim_problems)."""
    problems = []
    text = " ".join(str(prose.get(k) or "") for k in ("opening", "group_fit", "to_discuss", "closing"))
    for t in _identity_terms(members):
        if re.search(r"\b" + re.escape(t) + r"\b", text, re.I):
            problems.append(f"group prose mentions an identity detail ({t})")
            break
    if _GENDERED.search(text) or _PRONOUNS.search(text):
        problems.append("group prose uses gendered words")
    low = text.lower()
    for ph in EXCLUDED_TRAIT_REASONING:
        if ph in low:
            problems.append(f"group prose reasons from an excluded trait: {ph!r}")
            break
    if basis is not None:
        problems += habit_claim_problems(prose, members, basis, habit_answers)
    return problems


def _basis_for_prompt(basis) -> list:
    """What the model sees of the basis: the note text, WHO it is about (current intro ref + display name, so two people with one
    first name stay distinct) and field-level provenance indicators keyed by ref - which answer fields each person answered
    themselves ('own_answers') and which the lead reported for the party ('lead_reported') - never fact ids, coverage objects,
    hashes or stored answer VALUES. Legacy strings pass through."""
    out = []
    for item in basis or []:
        if isinstance(item, dict):
            d = {"note": str(item.get("text") or "")}
            about, own, lead = [], {}, {}
            cov = item.get("coverage") or {}
            for sj in item.get("subjects") or []:
                ref = sj.get("ref")   # people are keyed by their CURRENT intro ref (two people may share a first name)
                if not ref:
                    continue
                about.append({"ref": ref, "name": sj.get("name") or ref})
                for field, c in (cov.get(sj.get("packet_ref")) or {}).items():
                    if isinstance(c, dict) and c.get("status") == "recorded":
                        (own if c.get("basis") == "own" else lead).setdefault(ref, []).append(field)
            if about:
                d["about"] = about
            if own:
                d["own_answers"] = own
            if lead:
                d["lead_reported"] = lead
            out.append(d)
        else:
            out.append(str(item))
    return out


def household_claims_allowed(members: list[dict], basis) -> list[str]:
    """Habit fields every person in the introduction answered themselves (per the basis coverage): the only fields a collective
    'the household ...' statement may assert. Shown to the model so it writes usable prose first time."""
    refs = [_ref_of(m, i) for i, m in enumerate(members, start=1)]
    own = _own_coverage(basis)
    return sorted(f for f in HABIT_DOMAINS if all(f in own.get(r, set()) for r in refs))


# succ4: the route hands the private degree-validation context (ai_queue.intro_habit_answers) to draft_introduction through this scoped
# context instead of a new argument, so the drafter's callable contract (apartment, group, members, basis[, meter]) and every existing
# caller / test double stay exactly as they are. Read only by draft_introduction, only for validation; never serialised, never in the prompt.
# Absent context is NOT a bypass: draft_introduction then validates against an empty context (every stated degree fails closed).
_HABIT_ANSWERS: contextvars.ContextVar = contextvars.ContextVar("mmai_intro_habit_answers", default=None)


@contextlib.contextmanager
def intro_validation_context(habit_answers: dict | None):
    token = _HABIT_ANSWERS.set(dict(habit_answers or {}))
    try:
        yield
    finally:
        _HABIT_ANSWERS.reset(token)


def draft_introduction(apartment: dict, group: dict, members: list[dict], basis: list | None = None, meter: dict | None = None) -> str:
    """GP2 customer introduction email draft (Bill, 8 Nov 2026) for staff to copy, paste and send. `members` is the FLAT
    per-person list from ai_queue.intro_members (one entry per real person, refs I1, I1-m1, I2 ...).
    Two separate AI tasks, one call: (1) a short positive bio per PERSON from that person's OWN supplied facts (name, own words,
    country of birth, Australia activity, diet / social style / sleep pattern, sex/age only when freshly supplied for introductions);
    (2) 'why this group fits' and 'worth discussing' written ONLY from `basis` - the approved neutral staff brief notes - with no
    identity detail at all. Contact, rent and profile facts are DETERMINISTIC blocks built in code. Raises AIUnavailable when
    there is no approved basis or the prose fails validation (the caller then labels a template).
    succ4: the recorded habit answers set by intro_validation_context() are validation context only - never prompt or bio input. With no
    context set, the degree check runs against NOTHING and so fails closed on any stated tidiness / social-style degree: production never has
    a bypass; only the pure helpers keep `None` = coverage-only for the existing fixtures."""
    habit_answers = _HABIT_ANSWERS.get()
    if habit_answers is None:
        habit_answers = {}
    if not members:
        raise AIUnavailable("introduction: no members", code="intro_packet")
    if not basis:
        raise AIUnavailable("introduction: no approved staff brief to base the group fit on", code="intro_packet")
    people = []
    for i, m in enumerate(members, start=1):
        d = {"ref": _ref_of(m, i), **{k: m.get(k) for k in INTRO_BIO_KEYS if m.get(k) not in (None, "", [])}}
        if not m.get("first_name") and m.get("name"):
            d["first_name"] = m["name"]
        if m.get("is_lead") is False and m.get("lead_ref"):
            d["party_of"] = m["lead_ref"]
        people.append(d)
    # r7: the EXACT apartment and offer objects the caller fingerprinted (ai_queue.intro_offer_projection apt / ginfo) are
    # serialised whole - nothing selected out - so the stored offer_fingerprint binds what the model actually read.
    desc = {"apartment": apartment, "offer": group, "people": people, "group_basis": _basis_for_prompt(basis),
            "household_claims_allowed_for": household_claims_allowed(members, basis)}
    prompt = (
        "Write the PROSE parts of a warm, short introduction email from Apartment Living Melbourne (ALM) to a proposed group of housemates. ALM staff will "
        "assemble your text around factual blocks (each person's contact details, country of birth, study/work, guide share, the apartment, dates, rent and the "
        "lease liability note) that are inserted by the system, so do not repeat contact details, rents or dates.\n"
        "Task 1 - bios: for EACH person (refs I1, I1-m1, I2 ...; a 'party_of' entry is a companion coming with that lead) write 1-2 positive sentences "
        "using their first name and ONLY the facts given for THAT person (own introduction text, country_of_birth, what they do in Australia, diet, social style, "
        "sleep pattern). Sex and age are never written by you: the system prints each person's supplied label in their profile line, so a bio contains no sex, gender "
        "or age words and no number standing for an age. country_of_birth is the answer to ALM's question 'Country of birth': "
        "say 'born in X' and nothing else about origin: never 'from X', 'originally from X', 'grew up in X', 'raised in X', 'hails/comes from X', 'home country' "
        "or 'homeland', and never a nationality, citizenship, demonym ('Swedish', 'an Indian student') or current home - the form asked only where the person was born. "
        "Never guess or imply sex, gender, age, nationality or ethnicity. Use the person's name or neutral 'they'; never 'he'/'she' or gendered nouns. "
        "Never copy one person's facts into another person's bio; a companion with few facts gets a short, factual bio.\n"
        "Task 2 - group_fit and to_discuss: write ONLY from group_basis (ALM's approved practical notes; each note says who it is about, which answer fields those people "
        "gave themselves ('own_answers') and which the lead reported for a companion ('lead_reported')). Assert a habit of the WHOLE household (routines, tidiness, social style, "
        "guests, diet) - 'everyone', 'the household', 'you all', or sameness words with no names - ONLY for fields listed in household_claims_allowed_for. "
        "You may compare NAMED people (two or more, by first name, e.g. 'Alex and Bailey both recorded early routines') only on fields each named person answered "
        "themselves (own_answers) in a note about them; never widen a named comparison to the household, and a habit sentence must always say who it is about. "
        "For anything else, invite the group to discuss it rather than stating it. "
        "The 'apartment' and 'offer' objects are the exact listing and offer being proposed: you may refer to listed features, but do not repeat rents or dates. "
        "Do not use anything from the bios here: no countries, sex, age or 'similar background' reasoning - those were not how the group was formed; names are "
        "attribution of who a note is about, never a reason. "
        "Phrase to_discuss about the group as a whole, never singling anyone out by name and never asking one person to supply, share or explain another person's "
        "details: a companion whose habits are not recorded is a STAFF matter (already flagged to the team), not a task or a topic for the customer email - invite "
        "everyone to talk through routines, tidiness, guests and social preferences together instead. No guarantees, no 'perfect'. Do not mention screening, matching criteria or any AI.\n"
        "Return ONLY JSON: {\"opening\": <1-2 sentences>, \"bios\": {\"I1\": <bio>, \"I1-m1\": <bio>, ...}, \"group_fit\": <2-3 sentences>, \"to_discuss\": <2-3 sentences>, "
        "\"closing\": <1 sentence inviting them to say hello to each other and reply to ALM>}.\n"
    )

    def build(compact: bool) -> str:
        size = ("Response size limit (so the reply fits its output budget): bios of at most 2 sentences (1 if compact), group_fit and to_discuss of at most "
                "3 sentences each; keep every fact given, just say it briefly."
                + (" Your previous reply was cut off at the output budget: write each part in one short sentence." if compact else "") + "\n\n")
        return prompt + size + json.dumps(desc, ensure_ascii=False, default=str)

    meter = meter if meter is not None else {}
    text = _ask_bounded(build, 700 + 220 * len(members), "introduction draft", meter=meter)
    try:
        prose = _json_from(text)
        problems = validate_intro_prose(prose, members) + (validate_group_prose(prose, members, basis, habit_answers) if isinstance(prose, dict) else [])
        if problems:
            raise AIUnusableReply("intro draft unusable: " + "; ".join(problems)[:300], code="intro_unusable")
    except AIUnavailable as e:
        raise _terminal_if_compact(meter, "introduction draft", e)
    opening = prose["opening"].strip()
    parts = ([] if GREETING_RE.match(opening) else ["Hello everyone,", ""]) + [opening, "", _rent_block(apartment, group), ""]
    for i, m in enumerate(members, start=1):
        parts += [prose["bios"][_ref_of(m, i)].strip(), intro_profile_block(m), ""]
    parts += [prose["group_fit"].strip(), "", prose["to_discuss"].strip(), "", prose["closing"].strip(), "", "Apartment Living Melbourne"]
    return "\n".join(parts)


FALLBACK_LABEL = "[TEMPLATE DRAFT - not written or checked by the AI; staff must review and edit before sending]"


def fallback_introduction(apartment: dict, group: dict, members: list[dict]) -> str:
    """Rule-based template used when the AI draft is unavailable or failed validation. Visibly labelled: it never counts as the
    required AI draft (GP2) and the send step must not treat it as approved. One block per real person."""
    lines = [FALLBACK_LABEL, "", "Hello everyone,", "",
             "MateBot AI has put you together as a proposed group.", "", _rent_block(apartment, group), ""]
    for m in members:
        lines.append(intro_profile_block(m))
        if m.get("intro"):
            lines.append(f"  In their own words: {m['intro']}")
        lines.append("")
    lines += ["Please say hello to each other and let us know how you'd like to proceed.", "", "Apartment Living Melbourne"]
    return "\n".join(lines)


def normalise_country_ai(text: str) -> Optional[str]:
    """T4(c): read a typed country and return a standard English country name, or None."""
    prompt = (f"A person typed this as their country: {text!r}. Reply with ONLY the standard English short name of the country "
              f"(for example 'United Kingdom', 'South Korea', 'Vietnam'). If it is not a recognisable country, reply with the single word NONE.")
    out = _ask(prompt, max_tokens=20).strip().strip(".")  # T13: same approved model as every other call
    if not out or out.upper() == "NONE":
        return None
    return out
