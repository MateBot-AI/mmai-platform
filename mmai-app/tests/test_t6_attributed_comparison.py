"""T6 r5: attributed per-member comparisons (8 November 2026).

Background: on the frozen r4 core a real three-person run (lead + companion + solo) had its review approved and its brief
returned inside budget, but the brief was rejected because U1's strength "U1's early-riser, very tidy, friendly-but-independent
profile is identical to U2's" cited U2.routine / U2.tidy / U2.social inside U1's fact_ids (and U2's note did the mirror image).
That is a legitimate comparison under T4(f), not a lead's facts copied onto a companion, but the r4 validator could only see
"another person's personal fact in this person's item".

Contract pinned here (structural provenance validation, not a claim that the words are true):
  * fact_ids keeps the r4 ownership rule: a person's item cites that person's own facts (+ apartment / rule facts); another
    person's PERSONAL id in fact_ids is still rejected (the message now points at compared_with);
  * a person-scoped item may add compared_with: [{"ref": <housemate>, "fact_ids": [that housemate's ids]}]: each counterpart
    is another ref of the same group (not the subject, not repeated), each counterpart fact is RECORDED, is a personal or
    party-wide field (never apartment / rule), and the subject's OWN fact for the SAME field is recorded and cited in fact_ids;
  * counterpart facts never satisfy require_own or the minimum-content checks; compared_with on a group-level item is rejected,
    not ignored; over the caps (6 counterparts, 14 counterpart ids) the item is rejected, never trimmed;
  * the same contract applies to review replace_reasons (who conflicts with whom);
  * rendering keeps the attribution: counterpart facts appear in their own "compared with U2: ..." bracket, with the same
    party-wide "(reported by ... for the party)" provenance labels.
All model traffic is fabricated; no live call, no real people.
"""
import re
import json

import pytest

from server import ai, facts
from tests.conftest import fake_staff_briefs
from tests.test_t6_bounded_output import REAL_STAFF_BRIEFS
from tests.test_t6_grounding import _packet, _party_group, _run_and_approve


def _cat():
    """Two leads with full personal answers plus a companion (U1-m1) of U1: a synthetic catalogue, no database."""
    base = {"doing": "Studying", "school": "Uni", "course": "Eng", "level": "Postgraduate", "routine": "Early riser", "tidy": "Very tidy",
            "social": "Friendly but independent", "guests": "Sometimes", "diet": "No restrictions", "party": "With friends", "party_size": "2",
            "move_in": "2027-02-01", "move_out": "2027-07-10"}
    u1 = {"ref": "U1", **base, "party_members": ["U1-m1"]}
    u2 = {"ref": "U2", **base, "party": "Alone", "party_size": "1", "guests": None}
    u3 = {"ref": "U3", **base, "party": "Alone", "party_size": "1"}
    packet = {"members_for_ai": [u1, u2, u3], "apartments": [{"apartment_id": 1, "type_label": "2B2B", "suburb": "Carlton", "total_weekly": 1150}]}
    return facts.catalogue(packet)


def _check(items, ref="U1", **kw):
    kw.setdefault("allow_missing", False)
    kw.setdefault("require_own", True)
    return facts.check_items(items, _cat(), ref=ref, label=f"{ref}.strengths", **kw)


def test_contract_bumped_to_r5_and_prompts_explain_compared_with():
    assert ai.REVIEW_CONTRACT >= "2026-11-08.r5" and re.match(r"^\d{4}-\d{2}-\d{2}\.", ai.REVIEW_CONTRACT)   # r5 attributed comparison, carried into r6
    assert facts.COMPARE_REFS_MAX == 6 and facts.COMPARE_IDS_MAX == 14
    assert facts.COMPARABLE_FIELDS == facts.PERSONAL_FIELDS + facts.PARTY_FIELDS


def test_real3_item_shape_rejected_in_fact_ids_but_accepted_as_attributed_comparison():
    # the literal shape the real run produced: U2's ids inside U1's fact_ids -> still rejected, pointing at compared_with
    logged = [{"fact_ids": ["U1.routine", "U1.tidy", "U1.social", "U2.routine", "U2.tidy", "U2.social"],
               "note": "U1's early-riser, very tidy, friendly-but-independent profile is identical to U2's, so day-to-day friction looks unlikely."}]
    ok, pr = _check(logged)
    assert not ok and pr == ["U1.strengths[0]: U2.routine is U2's personal fact, not U1's (a comparison cites the housemate's same field under compared_with)",
                             "U1.strengths[0]: U2.tidy is U2's personal fact, not U1's (a comparison cites the housemate's same field under compared_with)",
                             "U1.strengths[0]: U2.social is U2's personal fact, not U1's (a comparison cites the housemate's same field under compared_with)"]
    # restructured: own facts in fact_ids, U2's same fields under compared_with -> accepted, counterpart kept separately
    good = [{"fact_ids": ["U1.routine", "U1.tidy", "U1.social"], "note": logged[0]["note"],
             "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine", "U2.tidy", "U2.social"]}]}]
    ok, pr = _check(good)
    assert not pr and ok == [{"fact_ids": ["U1.routine", "U1.tidy", "U1.social"], "note": logged[0]["note"], "kind": "evidence",
                              "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine", "U2.tidy", "U2.social"]}]}]
    # the mirror image for U2 works the same way
    ok, pr = _check([{"fact_ids": ["U2.routine"], "note": "same routine as U1", "compared_with": [{"ref": "U1", "fact_ids": ["U1.routine"]}]}], ref="U2")
    assert ok and not pr
    # an empty compared_with is simply no comparison
    ok, pr = _check([{"fact_ids": ["U1.routine"], "note": "early riser", "compared_with": []}])
    assert ok and not pr and "compared_with" not in ok[0]


@pytest.mark.parametrize("item, frag", [
    # counterpart-only: no own fact at all
    ({"fact_ids": [], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]}, "no fact_ids"),
    ({"fact_ids": ["A1.suburb"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]}, "cites no fact of U1"),
    # mismatched field: U2.tidy against U1.routine
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.tidy"]}]},
     "compared_with[U2] cites U2.tidy but U1's own recorded U1.tidy is not in fact_ids (a comparison needs both sides)"),
    # counterpart fact missing (U2 never answered guests)
    ({"fact_ids": ["U1.guests"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.guests"]}]},
     "compared_with[U2] cites U2.guests, which is not recorded for U2"),
    # a companion's personal field is never recorded, so it can never be compared
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U1-m1", "fact_ids": ["U1-m1.routine"]}]},
     "compared_with[U1-m1] cites U1-m1.routine, which is not recorded for U1-m1 (a companion's personal answers are never recorded)"),
    # self-comparison, unknown ref, repeated ref
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U1", "fact_ids": ["U1.routine"]}]}, "cannot compare U1 with themselves"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U9", "fact_ids": ["U9.routine"]}]}, "compared_with ref 'U9' is not a member of this group"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}, {"ref": "U2", "fact_ids": ["U2.routine"]}]}, "compared_with repeats U2"),
    # counterpart id that belongs to someone else / unknown / apartment fact
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U3.routine"]}]}, "compared_with[U2] cites U3.routine, which is not U2's fact"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.horoscope"]}]}, "compared_with cites unknown fact 'U2.horoscope'"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["A1.suburb"]}]}, "cites A1.suburb, which is not U2's fact"),
    # malformed
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": {"ref": "U2"}}, "compared_with is not a list of {ref, fact_ids} objects"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2"}]}, "compared_with[U2] has no fact_ids"),
    ({"fact_ids": ["U1.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": [3]}]}, "compared_with[U2] has no fact_ids"),
])
def test_bad_comparisons_reject_the_item(item, frag):
    ok, pr = _check([item])
    assert ok == [] and any(frag in p for p in pr), pr


def test_counterpart_facts_cannot_rescue_missing_own_fact_or_missing_subject():
    # own side cited but MISSING (U2 never answered guests): still rejected by the recorded-only rule, comparison or not
    ok, pr = _check([{"fact_ids": ["U2.guests"], "note": "x", "compared_with": [{"ref": "U1", "fact_ids": ["U1.guests"]}]}], ref="U2")
    assert not ok and any("U2.guests is not recorded" in p for p in pr)
    # own side recorded but not cited -> rejected even though U2 has routine recorded
    ok, pr = _check([{"fact_ids": ["U1.tidy"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]}])
    assert not ok and any("U1's own recorded U1.routine is not in fact_ids" in p for p in pr)


def test_compared_with_on_group_level_item_is_rejected_not_ignored():
    ok, pr = facts.check_items([{"fact_ids": ["U1.routine", "U2.routine"], "note": "x", "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}]}],
                               _cat(), ref=None, allow_missing=False, require_own=False, label="group.strengths")
    assert ok == [] and pr == ["group.strengths[0]: compared_with is only for a person's own brief items (group items already cite everyone)"]


def test_comparison_caps_reject_rather_than_trim():
    big = {"members_for_ai": [{"ref": f"U{i}", "routine": "Early riser", "tidy": "Tidy", "social": "Quiet", "party": "Alone", "party_size": "1"} for i in range(1, 9)]}
    cat = facts.catalogue(big)
    six = [{"ref": f"U{i}", "fact_ids": [f"U{i}.routine", f"U{i}.tidy"]} for i in range(2, 8)]   # 6 housemates, 12 ids
    ok, pr = facts.check_items([{"fact_ids": ["U1.routine", "U1.tidy"], "note": "x", "compared_with": six}], cat, ref="U1", allow_missing=False, require_own=True, label="U1.s")
    assert ok and not pr, pr
    seven = six + [{"ref": "U8", "fact_ids": ["U8.routine"]}]
    ok, pr = facts.check_items([{"fact_ids": ["U1.routine"], "note": "x", "compared_with": seven}], cat, ref="U1", allow_missing=False, require_own=True, label="U1.s")
    assert ok == [] and pr == ["U1.s[0]: compared_with names 7 housemates, over the response size limit of 6"]
    many = [{"ref": f"U{i}", "fact_ids": [f"U{i}.routine", f"U{i}.tidy", f"U{i}.social"]} for i in range(2, 7)]   # 5 housemates, 15 ids
    ok, pr = facts.check_items([{"fact_ids": ["U1.routine", "U1.tidy", "U1.social"], "note": "x", "compared_with": many}], cat, ref="U1", allow_missing=False, require_own=True, label="U1.s")
    assert ok == [] and pr == ["U1.s[0]: compared_with cites 15 counterpart facts, over the response size limit of 14"]


def test_render_keeps_counterpart_attribution_and_party_wide_provenance():
    cat = _cat()
    item = {"fact_ids": ["U1.routine", "U1.move_in"], "note": "same start and routine as U2", "kind": "evidence",
            "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine", "U2.move_in"]}]}
    out = facts.render(item, cat)
    assert out == "same start and routine as U2  [U1.routine=Early riser; U1.move_in=2027-02-01]  [compared with U2: U2.routine=Early riser; U2.move_in=2027-02-01]"
    # a companion's party-wide fact compared against a lead keeps its "reported by" label on the counterpart side too
    ok, pr = facts.check_items([{"fact_ids": ["U2.move_in"], "note": "same move-in as the party", "compared_with": [{"ref": "U1-m1", "fact_ids": ["U1-m1.move_in"]}]}],
                               cat, ref="U2", allow_missing=False, require_own=True, label="U2.s")
    assert ok and not pr, pr
    assert facts.render(ok[0], cat).endswith("[compared with U1-m1: U1-m1.move_in=2027-02-01 (reported by U1 for the party)]")


def test_staff_brief_end_to_end_with_real_party_packet(db, monkeypatch):
    """Through parse_staff_briefs on a real lead + two companions + solo packet: the restructured comparison is accepted and
    rendered with attribution; the companion cannot be a counterpart on personal fields; group-level compared_with fails."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    p = _packet(db, g)
    packet = {"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}}
    refs = [m["ref"] for m in p["members_for_ai"]] + [r for m in p["members_for_ai"] for r in (m.get("party_members") or [])]
    cat = facts.catalogue(packet)
    lead_ref = next(m["ref"] for m in p["members_for_ai"] if m.get("party_members"))
    solo_ref = next(m["ref"] for m in p["members_for_ai"] if not m.get("party_members"))
    pm_ref = next(r for r in refs if "-m" in r)
    # both leads answered the party-wide move_in; find a personal field both recorded (if any) else fall back to party-wide
    shared = [f for f in facts.COMPARABLE_FIELDS if cat[f"{lead_ref}.{f}"]["status"] == "recorded" and cat[f"{solo_ref}.{f}"]["status"] == "recorded"]
    assert shared, "fixture leads share at least one recorded comparable field"
    f = shared[0]
    good = fake_staff_briefs(packet)
    out = json.loads(json.dumps(good))
    me = next(m for m in out["members"] if m["ref"] == lead_ref)
    me["strengths"] = [{"fact_ids": [f"{lead_ref}.{f}"], "note": f"same {f} as the other lead", "compared_with": [{"ref": solo_ref, "fact_ids": [f"{solo_ref}.{f}"]}]}]
    res = ai.parse_staff_briefs(out, refs, packet)
    mine = next(m for m in res["members"] if m["ref"] == lead_ref)
    assert mine["strengths"][0]["compared_with"] == [{"ref": solo_ref, "fact_ids": [f"{solo_ref}.{f}"]}]
    assert f"[compared with {solo_ref}: {solo_ref}.{f}=" in mine["rendered"]["strengths"][0]
    # the companion has no personal facts to compare
    bad = json.loads(json.dumps(good))
    personal = next(x for x in facts.PERSONAL_FIELDS if cat[f"{solo_ref}.{x}"]["status"] == "recorded")
    next(m for m in bad["members"] if m["ref"] == solo_ref)["strengths"] = [
        {"fact_ids": [f"{solo_ref}.{personal}"], "note": "x", "compared_with": [{"ref": pm_ref, "fact_ids": [f"{pm_ref}.{personal}"]}]}]
    with pytest.raises(ai.AIUnavailable, match="companion's personal answers are never recorded"):
        ai.parse_staff_briefs(bad, refs, packet)
    # the old shape (other person's ids in fact_ids) is still unusable
    bad = json.loads(json.dumps(good))
    next(m for m in bad["members"] if m["ref"] == lead_ref)["strengths"] = [{"fact_ids": [f"{lead_ref}.{f}", f"{solo_ref}.{f}"], "note": "x"}]
    if cat[f"{solo_ref}.{f}"]["scope"] == "personal":
        with pytest.raises(ai.AIUnavailable, match="personal fact, not"):
            ai.parse_staff_briefs(bad, refs, packet)
    # group-level item carrying compared_with is rejected
    bad = json.loads(json.dumps(good))
    bad["group"]["strengths"] = [{"fact_ids": [f"{lead_ref}.{f}", f"{solo_ref}.{f}"], "note": "x", "compared_with": [{"ref": solo_ref, "fact_ids": [f"{solo_ref}.{f}"]}]}]
    with pytest.raises(ai.AIUnavailable, match="compared_with is only for a person's own brief items"):
        ai.parse_staff_briefs(bad, refs, packet)


def test_review_replace_reasons_accept_attributed_conflict_and_reject_other_only(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    desc = ai.review_packet([p])[0]
    cat = facts.catalogue(p)
    shared = [f for f in facts.PERSONAL_FIELDS if cat.get(f"U1.{f}", {}).get("status") == "recorded" and cat.get(f"U2.{f}", {}).get("status") == "recorded"]
    assert shared
    f = shared[0]
    apts = [{"apartment_id": x["apartment_id"], "reason": "r"} for x in desc["apartments_in_rule_order"]]
    rev = {"index": 0, "rating": 5, "rationale": "fine", "verdict": "revise", "apartments": apts, "findings": [{"fact_ids": [f"U1.{f}"], "note": "noted"}],
           "replace_refs": ["U2"], "replace_reasons": [{"ref": "U2", "fact_ids": [f"U2.{f}"], "note": "conflicts with U1 on this", "compared_with": [{"ref": "U1", "fact_ids": [f"U1.{f}"]}]}]}
    r = ai.parse_review_batch([rev], [p])[0]
    assert r["valid"], r["invalid_reason"]
    assert r["replace_reasons"][0]["compared_with"] == [{"ref": "U1", "fact_ids": [f"U1.{f}"]}]
    assert f"[compared with U1: U1.{f}=" in facts.render(r["replace_reasons"][0], cat)
    # the conflict described only through U1's facts is still no reason to replace U2
    bad = ai.parse_review_batch([{**rev, "replace_reasons": [{"ref": "U2", "fact_ids": [f"U1.{f}"], "note": "clash"}]}], [p])[0]
    assert not bad["valid"] and "personal fact, not U2's (a comparison cites the housemate's same field under compared_with)" in bad["invalid_reason"]
    # a mismatched counterpart field is rejected
    other = next((x for x in shared if x != f), None)
    if other:
        bad = ai.parse_review_batch([{**rev, "replace_reasons": [{"ref": "U2", "fact_ids": [f"U2.{f}"], "note": "clash", "compared_with": [{"ref": "U1", "fact_ids": [f"U1.{other}"]}]}]}], [p])[0]
        assert not bad["valid"] and "a comparison needs both sides" in bad["invalid_reason"]


def test_prompts_tell_the_model_how_to_compare(db, monkeypatch):
    """Both prompts (review replace_reasons and member briefs) explain compared_with and keep the fact_ids ownership rule."""
    a, ids, g = _run_and_approve(db, monkeypatch)
    monkeypatch.setattr(ai, "staff_briefs", REAL_STAFF_BRIEFS)
    seen: list = []

    def capture(prompt, model=None, max_tokens=1500, system=""):
        seen.append(prompt)
        raise ai.AIUnavailable("stop")
    monkeypatch.setattr(ai, "_ask", capture)
    p = _packet(db, g)
    with pytest.raises(ai.AIUnavailable):
        ai.assess_groups_and_apartments([p])
    with pytest.raises(ai.AIUnavailable):
        ai.staff_briefs({"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "apartments": p["apartments"][:3], "review": {}, "individually_recorded": {}})
    review, brief = seen[0], seen[1]
    assert "the other member's SAME fields under \"compared_with\"" in review and "never another person's ids in fact_ids" in review
    assert "the housemate's SAME fields under \"compared_with\"" in brief and "never put another person's ids in fact_ids" in brief
    assert "has no personal facts to compare" in brief
    for prompt in (review, brief):
        assert f"at most {facts.COMPARE_REFS_MAX} counterparts and {facts.COMPARE_IDS_MAX} counterpart ids per note" in prompt and f"separate from the {ai.FACT_IDS_MAX} own ids" in prompt
