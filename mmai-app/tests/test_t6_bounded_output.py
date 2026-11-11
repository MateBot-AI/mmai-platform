"""T6 r4: bounded AI output, rate-band / stay-length facts and unverified features (8 November 2026).

Background: on the frozen checkpoint a real Sonnet run approved a two-person review but the staff brief failed with
'AI response was cut off (max_tokens)' at the old 1200 + 420*refs budget, and the review read the bare rate code '6m' as
"the apartment's 6-month lease dates" for a 1 Feb - 10 Jul stay (159 days) and cited a gym recorded 'unverified' as evidence.

Pinned here (engineering limits approved by root; NOT rules about what staff may be told):
  * every review / brief / introduction call has an explicit output budget, never above ai.OUTPUT_TOKEN_CEILING; a reply cut off
    at the budget is retried exactly ONCE with a compact instruction at 1.5x; a second cut-off is terminal (AIOutputTruncated),
    the queue does not run the pair again, the group holds (brief failed, never ready) and call counters count both attempts;
  * item caps (group lists <= 3, member lists <= 2, findings <= 10, fact ids <= 14 per note, notes <= 30 words) are enforced by
    the parsers as rejections - nothing is clamped or dropped silently; a compact reply that parses badly is terminal too;
  * brief strengths / tensions / what_could_work / what_could_cause_issues take recorded evidence only: a missing citation or
    a kind='gap' disguise is rejected there (group and member), so an unknown never counts as the required positive content;
  * apartment facts carry rate_band (an A6 pricing-band description) and stay_days (exact date difference) instead of the bare
    rate_kind code; the rule_checks 'dates' detail does the same;
  * a feature recorded 'unverified' is a missing fact (a gap), never evidence.
All model traffic is a fabricated `_ask`; no live call, no real people.
"""
import re
import json

import pytest

from server import ai, ai_queue, facts, matching
from server.db import rows, row
from tests.conftest import fake_staff_briefs
from tests.test_launch_neutral import _apt, _pool
from tests.test_t6_fail_closed import _reply_builder, _good
from tests.test_t6_grounding import _run_and_approve

REAL_STAFF_BRIEFS = ai.staff_briefs   # captured at import (collection), before the autouse conftest double is applied per test


def _truncating(record, cut_first=True, cut_second=True):
    """An `_ask` double that records (max_tokens, compact?) per call and raises AIOutputTruncated as instructed."""
    def _ask(prompt, model=None, max_tokens=1500, system=""):
        compact = "Your previous reply was cut off" in prompt
        record.append((max_tokens, compact))
        if (cut_first and not compact) or (cut_second and compact):
            raise ai.AIOutputTruncated(f"AI response was cut off at the {max_tokens}-token output budget", max_tokens=max_tokens)
        return '{"ok": true}'
    return _ask


# ---------------------------------------------------------------- _ask_bounded
def test_ask_bounded_retries_once_compactly_then_raises_terminal(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai, "_ask", _truncating(calls))
    meter = {"asks": 0}
    with pytest.raises(ai.AIOutputTruncated) as ei:
        ai._ask_bounded(lambda compact: "p" + (" Your previous reply was cut off" if compact else ""), 4000, "staff brief", meter=meter)
    assert calls == [(4000, False), (6000, True)], "one normal call, ONE compact retry at 1.5x, nothing more"
    assert meter["asks"] == 2
    msg = str(ei.value)
    assert "staff brief exceeded the response size limit after a compact retry (6000 output tokens)" in msg and "staff decide" in msg
    assert ei.value.attempts == 2 and ei.value.max_tokens == 6000


def test_ask_bounded_second_attempt_succeeds_and_ceiling_holds(monkeypatch):
    calls: list = []
    monkeypatch.setattr(ai, "_ask", _truncating(calls, cut_first=True, cut_second=False))
    meter = {"asks": 0}
    out = ai._ask_bounded(lambda compact: "p" + (" Your previous reply was cut off" if compact else ""), 7000, "AI review", meter=meter)
    assert meter.get("compact") is True
    assert out == '{"ok": true}' and meter["asks"] == 2
    assert calls == [(7000, False), (ai.OUTPUT_TOKEN_CEILING, True)], "the compact retry (1.5x = 10500) is capped at the explicit ceiling"
    # a budget request above the ceiling is itself capped
    calls.clear()
    monkeypatch.setattr(ai, "_ask", _truncating(calls, cut_first=False))
    ai._ask_bounded(lambda compact: "p", 50000, "x", meter=meter)
    assert calls == [(ai.OUTPUT_TOKEN_CEILING, False)] and meter["asks"] == 3


def test_brief_budget_sized_for_two_to_seven_people():
    assert ai.brief_token_budget(2) == 3600 and ai.brief_token_budget(3) == 4400 and ai.brief_token_budget(7) == 7600
    assert ai.OUTPUT_TOKEN_CEILING == 8000 and ai.brief_token_budget(7) <= ai.OUTPUT_TOKEN_CEILING, "root-approved ceiling; the 7-person budget fits under it"
    assert ai.brief_token_budget(100) == ai.OUTPUT_TOKEN_CEILING
    assert (ai.NOTE_WORDS_ASKED, ai.NOTE_WORDS_MAX, facts.NOTE_WORDS_MAX, ai.FACT_IDS_MAX, facts.FACT_IDS_MAX) == (25, 30, 30, 14, 14)


def test_ask_raises_truncated_on_max_tokens_stop(monkeypatch):
    class _Msg:
        stop_reason = "max_tokens"
        content = [type("B", (), {"text": "{"})()]

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] <= ai.OUTPUT_TOKEN_CEILING
                return _Msg()

    monkeypatch.setattr(ai, "_client", lambda: _Client())
    with pytest.raises(ai.AIOutputTruncated) as ei:
        ai._ask("p", max_tokens=99999)
    assert ei.value.max_tokens == ai.OUTPUT_TOKEN_CEILING and "cut off" in str(ei.value)
    assert isinstance(ei.value, ai.AIUnavailable)


# ---------------------------------------------------------------- queue paths
def test_brief_truncation_holds_group_without_further_retry(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    assert g["ai_status"] == "ok" and g["brief_status"] == "ok"
    # redo the brief with the real staff_briefs over a truncating model (the autouse double is undone for this group)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)   # the real brief call for this group (autouse double stays scoped to this test)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []
    monkeypatch.setattr(ai, "_ask", _truncating(calls))
    db.execute("UPDATE groups SET brief_status='pending', held_reason=NULL WHERE id=?", (g["id"],)); db.commit()
    db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],)); db.commit()
    out = ai_queue.process_briefs(db, attempts=3)
    assert out["ok"] == 0 and out["failed"] == 1
    assert len(calls) == 2 and out["calls"] == 2, "ONE compact retry inside ai; the queue's three attempts do not re-run the pair; counter = real calls"
    assert calls[0] == (ai.brief_token_budget(2), False) and calls[1] == (int(ai.brief_token_budget(2) * 1.5), True)
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g2["brief_status"] == "failed" and g2["ready_at"] is None
    assert "exceeded the response size limit after a compact retry" in g2["held_reason"] and "staff decide" in g2["held_reason"]
    assert not ai_queue.is_ready(db, g2)["ready"]
    fail = rows(db.execute("SELECT * FROM group_briefs WHERE group_id=? ORDER BY id", (g["id"],)))
    assert len(fail) == 1 and fail[0]["status"] == "failed" and "response size limit" in fail[0]["error"]
    # the hold is strict: nothing fell back to a template or partial brief, and a repeat pass does not touch a failed group
    assert ai_queue.process_briefs(db, attempts=3)["groups"] == 0 and len(calls) == 2


def test_review_truncation_fails_group_after_one_pair(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []
    monkeypatch.setattr(ai, "_ask", _truncating(calls))
    out = ai_queue.process_pending(db, attempts=3)
    assert out["ok"] == 0 and out["failed"] == res["ai_queued"]
    assert len(calls) == 2 and out["calls"] == 2, "one normal + one compact call for the batch, no third attempt"
    gs = rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],)))
    assert all(x["ai_status"] == "failed" and "response size limit" in x["ai_error"] and x["ready_at"] is None for x in gs)
    assert all(x["ai_attempts"] == 2 for x in gs), "ai_attempts counts the real calls made"


def test_brief_call_counter_counts_internal_retry_when_second_attempt_succeeds(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)   # the real brief call for this group (autouse double stays scoped to this test)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []
    good = fake_staff_briefs(ai_queue._payload(db, g) | {"apartments": ai_queue._payload(db, g)["apartments"][:3]})

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        compact = "Your previous reply was cut off" in prompt
        calls.append((max_tokens, compact))
        if not compact:
            raise ai.AIOutputTruncated("cut", max_tokens=max_tokens)
        return json.dumps({"group": {k: good["group"][k] for k in ai.BRIEF_GROUP_KEYS}, "members": [{k: m[k] for k in ("ref",) + ai.BRIEF_MEMBER_KEYS} for m in good["members"]]})

    monkeypatch.setattr(ai, "_ask", _ask)
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],)); db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],)); db.commit()
    out = ai_queue.process_briefs(db)
    assert out["ok"] == 1 and out["calls"] == 2 and calls[1][1] is True
    assert row(db.execute("SELECT brief_status FROM groups WHERE id=?", (g["id"],)))["brief_status"] == "ok"


# ---------------------------------------------------------------- parser caps (rejected, never clamped)
def _brief_packet(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = ai_queue._payload(db, g)
    packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}}
    good = fake_staff_briefs(packet)
    out = {"group": {k: good["group"][k] for k in ai.BRIEF_GROUP_KEYS}, "members": [{k: m[k] for k in ("ref",) + ai.BRIEF_MEMBER_KEYS} for m in good["members"]]}
    refs = [m["ref"] for m in packet["members_for_ai"]]
    return packet, out, refs


def test_brief_item_caps_rejected_not_clamped(db, monkeypatch):
    packet, out, refs = _brief_packet(db, monkeypatch)
    cat = facts.catalogue(packet)
    own = [k for k, v in cat.items() if v.get("ref") == refs[0] and v["status"] == "recorded"]
    assert ai.parse_staff_briefs(json.loads(json.dumps(out)), refs, packet)["group"]
    over = json.loads(json.dumps(out))
    over["group"]["strengths"] = [{"fact_ids": own[:1], "note": f"strength {i}"} for i in range(ai.BRIEF_GROUP_ITEMS_MAX + 1)]
    with pytest.raises(ai.AIUnavailable, match="group.strengths: 4 items exceed the response size limit of 3"):
        ai.parse_staff_briefs(over, refs, packet)
    over = json.loads(json.dumps(out))
    over["members"][0]["strengths"] = [{"fact_ids": own[:1], "note": f"s{i}"} for i in range(ai.BRIEF_MEMBER_ITEMS_MAX + 1)]
    with pytest.raises(ai.AIUnavailable, match=rf"{refs[0]}.strengths: 3 items exceed the response size limit of 2"):
        ai.parse_staff_briefs(over, refs, packet)
    # exactly at the cap is fine
    at = json.loads(json.dumps(out))
    at["group"]["strengths"] = [{"fact_ids": own[:1], "note": f"strength {i}"} for i in range(ai.BRIEF_GROUP_ITEMS_MAX)]
    at["members"][0]["strengths"] = [{"fact_ids": own[:1], "note": f"s{i}"} for i in range(ai.BRIEF_MEMBER_ITEMS_MAX)]
    res = ai.parse_staff_briefs(at, refs, packet)
    assert len(res["group"]["strengths"]) == 3 and len(res["members"][0]["strengths"]) == 2


def test_fact_id_and_note_length_caps(db, monkeypatch):
    packet, out, refs = _brief_packet(db, monkeypatch)
    cat = facts.catalogue(packet)
    recorded = sorted(k for k, v in cat.items() if v["status"] == "recorded")
    assert len(recorded) >= ai.FACT_IDS_MAX + 1
    ok, pr = facts.check_items([{"fact_ids": recorded[:ai.FACT_IDS_MAX], "note": "fourteen ids cited"}], cat, ref=None, allow_missing=False, require_own=False, label="x")
    assert ok and not pr, "14 fact ids (every member of a seven-person group) are allowed"
    ok, pr = facts.check_items([{"fact_ids": recorded[:ai.FACT_IDS_MAX + 1], "note": "too many"}], cat, ref=None, allow_missing=False, require_own=False, label="x")
    assert not ok and pr == ["x[0]: 15 fact ids exceed the response size limit of 14"]
    ok, pr = facts.check_items([{"fact_ids": recorded[:1], "note": " ".join(["word"] * facts.NOTE_WORDS_MAX)}], cat, ref=None, allow_missing=False, require_own=False, label="x")
    assert ok and not pr
    ok, pr = facts.check_items([{"fact_ids": recorded[:1], "note": " ".join(["word"] * (facts.NOTE_WORDS_MAX + 1))}], cat, ref=None, allow_missing=False, require_own=False, label="x")
    assert not ok and pr == ["x[0]: note of 31 words exceeds the response size limit of 30"]


def test_review_findings_cap_rejected(db, monkeypatch):
    def many(g):
        item = _good(g)
        item["findings"] = [{"fact_ids": g["fact_ids"][:1], "note": f"finding {i}"} for i in range(ai.REVIEW_FINDINGS_MAX + 1)]
        return item
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(many))
    seen, real_parse = [], ai.parse_review_batch
    monkeypatch.setattr(ai, "parse_review_batch", lambda out, groups: seen.extend(real_parse(out, groups)) or seen[-len(groups):])
    ai_queue.process_pending(db)
    gs = rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],)))
    assert all(x["ai_status"] == "failed" and x["ai_error"] == ai.FAILURE_TEXT["review_unusable"] + " (1 problem found)" for x in gs)   # Modified A: closed sentence + count
    assert seen and all("findings: 11 items exceed the response size limit of 10" in r["invalid_reason"] for r in seen)   # the validator's diagnosis, in process only


def test_prompts_state_limits_as_engineering_not_rules(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)   # the real brief call for this group (autouse double stays scoped to this test)
    seen: list = []

    def capture(prompt, model=None, max_tokens=1500, system=""):
        seen.append((prompt, max_tokens))
        raise ai.AIUnavailable("stop")
    monkeypatch.setattr(ai, "_ask", capture)
    p = ai_queue._payload(db, g)
    with pytest.raises(ai.AIUnavailable):
        ai.staff_briefs({"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}})
    prompt, budget = seen[-1]
    assert budget == ai.brief_token_budget(2)
    assert "Response size limit" in prompt and "engineering limit" in prompt and "not a rule" in prompt
    assert "do not manufacture a weakness" in prompt and "may be EMPTY" in prompt, "caps never pressure the model to invent tensions or gaps"
    assert f"at most {ai.NOTE_WORDS_ASKED} words" in prompt and f"over {ai.NOTE_WORDS_MAX} is rejected" in prompt
    assert "agreed size" not in prompt
    assert "rate_band is only ALM's A6 pricing band" in prompt and "never describe a stay as 'six months'" in prompt
    assert "'unverified' means ALM has NOT recorded it" in prompt


# ---------------------------------------------------------------- facts: rate band / stay length, unverified features
def test_packet_carries_rate_band_and_stay_days_not_bare_rate_kind(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = ai_queue._payload(db, g)
    apt = p["apartments"][0]
    assert apt["lease_start"] == "2027-02-01" and apt["lease_end"] == "2027-07-10" and apt["rate_kind"] == "6m"
    assert apt["stay_days"] == 159, "date difference 1 Feb -> 10 Jul 2027, not 160"
    assert "six-month rate band" in apt["rate_band"] and "A6" in apt["rate_band"] and "not the stay length" in apt["rate_band"]
    cat = facts.catalogue(p)
    assert cat["A1.stay_days"]["value"] == 159 and cat["A1.stay_days"]["status"] == "recorded"
    assert "not the stay length" in cat["A1.rate_band"]["value"]
    assert "A1.rate_kind" not in cat, "the bare code is no longer a citable fact"
    dates = next(rc for rc in apt["rule_checks"] if rc["rule"] == "dates")["detail"]
    assert "rate" not in dates and dates["stay_days"] == 159 and "not the stay length" in dates["rate_band"]
    desc = ai.review_packet([p])[0]
    lease = desc["apartments_in_rule_order"][0]["lease"]
    assert lease == {"start": "2027-02-01", "end": "2027-07-10", "stay_days": 159, "rate_band": apt["rate_band"]}
    assert matching.rate_band_fact("12m").startswith("standard rate band") and matching.rate_band_fact(None) is None
    assert matching.stay_days_fact("2027-02-01", "2027-07-10") == 159 and matching.stay_days_fact(None, "2027-07-10") is None


def test_unverified_feature_is_a_gap_never_evidence(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    # both members ask for a gym the apartment has not recorded (feat_gym NULL) and a balcony recorded yes
    db.execute("UPDATE apartments SET feat_gym=NULL, feat_balcony=1 WHERE id=?", (a["id"],))
    db.execute("UPDATE enquiries SET features=? WHERE id IN (1, 2)", (json.dumps(["Gym", "Balcony"]),)); db.commit()
    p = ai_queue._payload(db, g)
    feats = {f["key"]: f["recorded"] for f in p["apartments"][0]["features"]}
    assert feats == {"balcony": "available", "gym": "unverified"}
    cat = facts.catalogue(p)
    assert cat["A1.feature.gym"]["status"] == "missing" and cat["A1.feature.gym"]["value"] == "unverified"
    assert cat["A1.feature.balcony"]["status"] == "recorded"
    desc = ai.review_packet([p])[0]
    assert "A1.feature.gym" in desc["missing_fact_ids"] and "A1.feature.gym" not in desc["fact_ids"]
    # an evidence item citing the unverified gym is rejected (the live model's exact pattern); a gap item is kept and labelled
    bad = [{"fact_ids": ["A1.feature.balcony", "A1.feature.gym"], "note": "Both asked for a balcony (available) and a gym (unverified).", "kind": "evidence"}]
    clean, pr = facts.check_items(bad, cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert not clean and pr == ["findings[0]: A1.feature.gym is not recorded (cannot support an evidence finding; use kind 'gap' to flag it as unknown)"]
    gap = [{"fact_ids": ["A1.feature.gym"], "note": "Whether the building has a gym is not recorded; confirm before mentioning it.", "kind": "gap"}]
    clean, pr = facts.check_items(gap, cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert len(clean) == 1 and not pr
    assert facts.render(clean[0], cat) == "[gap - not recorded] Whether the building has a gym is not recorded; confirm before mentioning it.  [A1.feature.gym: not recorded (unverified)]"
    # in a staff brief: an unverified feature may appear under missing_facts / discussion points, never under strengths
    packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}}
    good = fake_staff_briefs(packet)
    out = {"group": {k: good["group"][k] for k in ai.BRIEF_GROUP_KEYS}, "members": [{k: m[k] for k in ("ref",) + ai.BRIEF_MEMBER_KEYS} for m in good["members"]]}
    refs = [m["ref"] for m in packet["members_for_ai"]]
    bad_out = json.loads(json.dumps(out))
    bad_out["group"]["strengths"].append({"fact_ids": ["A1.feature.gym"], "note": "The building gym suits both."})
    with pytest.raises(ai.AIUnavailable, match="A1.feature.gym is not recorded"):
        ai.parse_staff_briefs(bad_out, refs, packet)
    ok_out = json.loads(json.dumps(out))
    ok_out["group"]["missing_facts"].append({"fact_ids": ["A1.feature.gym"], "note": "Gym not recorded for this building; confirm with the landlord."})
    res = ai.parse_staff_briefs(ok_out, refs, packet)
    assert any("A1.feature.gym" in x["fact_ids"] for x in res["group"]["missing_facts"])


def test_review_contract_bumped_past_r4():
    assert ai.REVIEW_CONTRACT >= "2026-11-08.r4" and re.match(r"^\d{4}-\d{2}-\d{2}\.", ai.REVIEW_CONTRACT)   # r4 bounded output, superseded by r5 attributed comparison
    assert "rule-based fallback" not in ai.__doc__.splitlines()[-7:][0] or "Only the introduction" in ai.__doc__
    assert "Only the introduction has a" in ai.__doc__ and "HOLDS the group" in ai.__doc__


def test_gap_disguise_cannot_be_a_strength(db, monkeypatch):
    """Root review: check_items accepted kind='gap' anywhere, so a brief could satisfy 'group.strengths non-empty' with an unknown.
    Positive/negative evidence lists (strengths, tensions, what_could_work, what_could_cause_issues; member strengths/tensions)
    now reject both a missing citation and a kind='gap' item; missing_facts / discussion_points still take them."""
    packet, out, refs = _brief_packet(db, monkeypatch)
    cat = facts.catalogue(packet)
    missing = next(k for k, v in cat.items() if v["status"] == "missing" and v.get("ref") == refs[0])
    recorded = next(k for k, v in cat.items() if v["status"] == "recorded" and v.get("ref") == refs[0])
    for key in ("strengths", "what_could_work", "tensions", "what_could_cause_issues"):
        for item in ({"fact_ids": [missing], "note": "unknown dressed as evidence"},
                     {"fact_ids": [missing], "note": "unknown dressed as a gap", "kind": "gap"},
                     {"fact_ids": [recorded], "note": "recorded fact but flagged gap", "kind": "gap"}):
            bad = json.loads(json.dumps(out))
            bad["group"][key] = [item]
            with pytest.raises(ai.AIUnavailable, match=rf"group.{key}\[0\]: .*(is not recorded|gap item cannot stand as evidence)"):
                ai.parse_staff_briefs(bad, refs, packet)
    # the ONLY positive content being a gap never satisfies the required group strength / member strength
    bad = json.loads(json.dumps(out))
    bad["group"]["strengths"] = [{"fact_ids": [missing], "note": "we do not know yet", "kind": "gap"}]
    bad["group"]["what_could_work"] = []
    with pytest.raises(ai.AIUnavailable, match="gap item cannot stand as evidence"):
        ai.parse_staff_briefs(bad, refs, packet)
    for key in ("strengths", "tensions"):
        bad = json.loads(json.dumps(out))
        bad["members"][0][key] = [{"fact_ids": [missing], "note": "member unknown as a gap", "kind": "gap"}]
        with pytest.raises(ai.AIUnavailable, match=rf"{refs[0]}.{key}\[0\]: a gap item cannot stand as evidence"):
            ai.parse_staff_briefs(bad, refs, packet)
        bad["members"][0][key] = [{"fact_ids": [missing], "note": "member unknown as evidence"}]
        with pytest.raises(ai.AIUnavailable, match=rf"{refs[0]}.{key}\[0\]: {missing} is not recorded"):
            ai.parse_staff_briefs(bad, refs, packet)
    # gap items remain welcome where unknowns belong
    ok = json.loads(json.dumps(out))
    ok["group"]["discussion_points"].append({"fact_ids": [missing], "note": "confirm this before introducing", "kind": "gap"})
    ok["members"][0]["missing_facts"].append({"fact_ids": [missing], "note": "not recorded for this person", "kind": "gap"})
    res = ai.parse_staff_briefs(ok, refs, packet)
    assert any(x["kind"] == "gap" for x in res["group"]["discussion_points"]) and any(x["kind"] == "gap" for x in res["members"][0]["missing_facts"])


def test_unusable_compact_reply_is_terminal_for_brief_and_review(db, monkeypatch):
    """After a cut-off reply the compact retry may complete but be malformed or over-cap: the parser's AIUnavailable becomes
    AINoRetry, so the queue never re-runs the normal+compact pair (<= 2 model calls for the whole sequence)."""
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)   # the real brief call for this group (autouse double stays scoped to this test)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        compact = "Your previous reply was cut off" in prompt
        calls.append(compact)
        if not compact:
            raise ai.AIOutputTruncated("cut", max_tokens=max_tokens)
        return "this is not json at all"

    monkeypatch.setattr(ai, "_ask", _ask)
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],)); db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],)); db.commit()
    out = ai_queue.process_briefs(db, attempts=3)
    assert calls == [False, True] and out["calls"] == 2 and out["failed"] == 1
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g2["brief_status"] == "failed" and "compact reply after a cut-off reply was unusable" in g2["held_reason"] and "staff decide" in g2["held_reason"]
    assert not ai_queue.is_ready(db, g2)["ready"]


def test_unusable_compact_review_reply_is_terminal(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        compact = "Your previous reply was cut off" in prompt
        calls.append(compact)
        if not compact:
            raise ai.AIOutputTruncated("cut", max_tokens=max_tokens)
        return "this is not json at all"

    monkeypatch.setattr(ai, "_ask", _ask)
    out = ai_queue.process_pending(db, attempts=3)
    assert calls == [False, True] and out["calls"] == 2 and out["ok"] == 0
    gs = rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],)))
    assert gs and all(x["ai_status"] == "failed" and "compact reply after a cut-off reply was unusable" in x["ai_error"] and x["ai_attempts"] == 2 for x in gs)


def test_ordinary_unusable_first_reply_still_retried_within_bounded_attempts(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    calls: list = []
    monkeypatch.setattr(ai, "_ask", lambda prompt, model=None, max_tokens=1500, system="": calls.append(1) or "not json")
    out = ai_queue.process_pending(db, attempts=3)
    # C4 (10 Nov 2026): the first unusable-as-a-whole reply earns exactly ONE whole-batch repair call per call meter (the meter is
    # shared by the queue's attempts), so an always-unusable batch costs the 3 queue attempts + 1 repair call = 4 provider calls, never more
    assert res["ai_queued"] > 0 and out["calls"] == 4 and len(calls) == 4, "no cut-off: the queue's own bounded attempts + the one bounded repair turn"


def _cut_then(second):
    """`_ask` double: first (normal) call is cut off; the compact call does `second` (raise or return)."""
    calls: list = []

    def _ask(prompt, model=None, max_tokens=1500, system=""):
        compact = "Your previous reply was cut off" in prompt
        calls.append(compact)
        if not compact:
            raise ai.AIOutputTruncated("cut", max_tokens=max_tokens)
        if isinstance(second, BaseException):
            raise second
        return second
    return _ask, calls


@pytest.mark.parametrize("second, expect", [
    ('{"group": {"strengths": [}, "members": [', ai.FAILURE_TEXT["size_limit_compact_unusable"]),
    (ai.AIUnavailable("Connection error: simulated outage"), ai.FAILURE_TEXT["size_limit_compact_failed"]),   # Modified A: the outage's words never reach the row
])
def test_every_compact_attempt_failure_is_terminal_for_briefs(db, monkeypatch, second, expect):
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)   # the real brief call for this group (autouse double stays scoped to this test)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    _ask, calls = _cut_then(second)
    monkeypatch.setattr(ai, "_ask", _ask)
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],)); db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],)); db.commit()
    out = ai_queue.process_briefs(db, attempts=3)
    assert calls == [False, True] and out["calls"] == 2 and out["failed"] == 1, "exactly two model calls although the queue allows three attempts"
    g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert g2["brief_status"] == "failed" and g2["ready_at"] is None and expect in g2["held_reason"] and "staff decide" in g2["held_reason"]
    assert not ai_queue.is_ready(db, g2)["ready"]


@pytest.mark.parametrize("second, expect", [
    ('[{"index": 0, "findings": [}', ai.FAILURE_TEXT["size_limit_compact_unusable"]),
    (ai.AIUnavailable("Connection error: simulated outage"), ai.FAILURE_TEXT["size_limit_compact_failed"]),
])
def test_every_compact_attempt_failure_is_terminal_for_reviews(db, monkeypatch, second, expect):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    _ask, calls = _cut_then(second)
    monkeypatch.setattr(ai, "_ask", _ask)
    out = ai_queue.process_pending(db, attempts=3)
    assert calls == [False, True] and out["calls"] == 2 and out["ok"] == 0 and out["failed"] == res["ai_queued"]
    gs = rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],)))
    assert gs and all(x["ai_status"] == "failed" and expect in x["ai_error"] and x["ai_attempts"] == 2 and x["ready_at"] is None for x in gs)


def test_malformed_json_is_an_unusable_reply_not_a_decode_error():
    with pytest.raises(ai.AIUnavailable, match="malformed JSON"):
        ai._json_from('{"a": [1, 2}')
    with pytest.raises(ai.AIUnavailable, match="no JSON"):
        ai._json_from("plain words")
    assert ai._json_from('noise {"a": 1} noise') == {"a": 1}
