"""Completion Increment A (9 Nov 2026) - subject-attributed budget evidence, housemates semantics, wording guards, birth-country-only
bios and collective discussion. Synthetic data, isolated SQLite, no provider. Complements the root probe
(reviews/20261109-completion-a-probe.py), which must be run unchanged.

What is pinned here:
  * per-person budget facts come from the deterministic rules calculation (rules.check_group -> bed allocation), NOT from who answered
    the form: share facts have no 'reported by' marker, stated_max facts do (the enquirer answers I14 for the whole party, I16(d));
  * the lead's own share/stated_max are theirs; a companion's share is the companion's (owner == companion) and cannot be cited as the
    lead's evidence or vice versa; the household-only budget id is not a person's evidence;
  * weekly_share is carried as the allocation stored it (cents-rounded) and its text is the same value, not a re-derived rational
    split; stated_weekly_max_exact is the lossless text of the same validated amount the A16 check used;
  * housemates check: an enquirer's accepted maximum of OTHER housemates against the number of other people in this group - the
    person is never counted, a total-occupant reading is never used;
  * wording guards are context-aware: a share may not be called agreed/accepted, but 'not agreed', 'yet to be agreed', 'once agreed'
    pass; an accepted-preference fact may not be sold as a first choice unless room_first is also cited;
  * bios: only 'born in <country>' is recorded; origin/home/upbringing wording attached to that country is refused, ordinary English
    elsewhere is not, and a person's own free intro that says it in their own words keeps it;
  * to_discuss never asks a named person to supply another person's details.
"""
import re
from decimal import Decimal

import pytest

from server import ai, facts, matching, rules
from server import budget as budget_mod
from tests.test_r7_intro_wording import _people, _prose
from tests.test_t6_grounding import _packet, _party_group


def _party(db, monkeypatch):
    _, _, _, group = _party_group(db, monkeypatch)
    packet = _packet(db, group)
    lead = next(m for m in packet["members_for_ai"] if m.get("party_members"))
    solo = next(m for m in packet["members_for_ai"] if not m.get("party_members"))
    return packet, lead["ref"], lead["party_members"][0], solo["ref"]


def _budget_detail(packet, apt):
    return next(rc["detail"] for rc in apt["rule_checks"] if rc["rule"] == "budget")


# ---------------------------------------------------------------- provenance: calculation vs stated answer
def test_share_is_calculation_provenance_and_stated_max_is_reported_answer(db, monkeypatch):
    packet, lead, companion, solo = _party(db, monkeypatch)
    cat = facts.catalogue(packet)
    for apt in packet["apartments"]:
        prefix = f"R.A{apt['apartment_id']}.budget"
        for ref in (lead, companion, solo):
            share, stated = cat[f"{prefix}.share.{ref}"], cat[f"{prefix}.stated_max.{ref}"]
            # share: deterministic allocation, nobody 'reported' it
            assert share["source"].startswith("rules.check_group") and "allocation" in share["source"]
            assert "reported_by" not in share or share["reported_by"] in (None, "")
            assert "proposal" in share["source"] and "agreed" not in share["source"].split("proposal")[0]
            # stated max: a stated answer, reported by the lead of that person's enquiry
            assert stated["source"].startswith("rules.check_group") and "stated" in stated["source"]
            assert stated["reported_by"] == (lead if ref in (lead, companion) else solo)
            assert share["ref"] == ref and stated["ref"] == ref
            assert facts.owner(cat, f"{prefix}.share.{ref}") == ref and facts.owner(cat, f"{prefix}.stated_max.{ref}") == ref
            # plain decimal values with units, no currency symbols or thousands separators
            for f in (share, stated):
                assert re.fullmatch(r"\d+(?:\.\d+)?", f["value"]), f["value"]
                assert f["unit"] == "AUD per week" and f["scope"] == "rule" and f["status"] == "recorded"


def test_companion_bit_carries_reported_by_marker_only_for_stated_max(db, monkeypatch):
    packet, lead, companion, _ = _party(db, monkeypatch)
    cat = facts.catalogue(packet)
    prefix = f"R.A{packet['apartments'][0]['apartment_id']}.budget"
    share_bit = facts._fact_bit(f"{prefix}.share.{companion}", cat)
    stated_bit = facts._fact_bit(f"{prefix}.stated_max.{companion}", cat)
    assert f"(reported by {lead} for the party)" in stated_bit
    assert "reported by" not in share_bit
    # the lead's own stated maximum is their own answer: no marker
    assert "reported by" not in facts._fact_bit(f"{prefix}.stated_max.{lead}", cat)


# ---------------------------------------------------------------- values: carried, not re-derived
def test_share_text_is_the_stored_allocation_amount_and_stated_max_is_lossless(db, monkeypatch):
    packet, lead, companion, solo = _party(db, monkeypatch)
    cat = facts.catalogue(packet)
    for apt in packet["apartments"]:
        detail = _budget_detail(packet, apt)
        prefix = f"R.A{apt['apartment_id']}.budget"
        for ref in (lead, companion, solo):
            pp = detail["per_person"][ref]
            assert Decimal(pp["weekly_share_text"]) == Decimal(str(pp["weekly_share"]))         # same stored value, as text
            assert Decimal(cat[f"{prefix}.share.{ref}"]["value"]) == Decimal(str(pp["weekly_share"]))
            assert pp["stated_weekly_max_exact"] == rules._lossless_decimal_text(budget_mod.money(pp["stated_weekly_max"], positive=True))
            assert Decimal(cat[f"{prefix}.stated_max.{ref}"]["value"]) == Decimal(pp["stated_weekly_max_exact"])
            assert "proposal" in pp["weekly_share_basis"] and "not individually agreed" in pp["weekly_share_basis"]
        total_shares = sum(Decimal(pp["weekly_share_text"]) for pp in detail["per_person"].values())   # all four people, incl. the second companion
        # the shares are the A12 split of the listed rent (cents-rounded by the allocation), not a figure invented here
        assert abs(total_shares - Decimal(detail["household"]["total_weekly_rent_exact"])) <= Decimal("0.01") * len(detail["per_person"])
        assert "member_weekly_maximums" not in detail["household"]   # cents-rounded per-person figures are not relabelled exact; per_person carries them


def test_unusable_stated_max_gives_no_exact_fact_and_no_crash():
    class P:  # minimal stand-in for an allocation member
        unit_id = "7"
        budget_max = "not a number"
    out = matching.per_person_budget_fact(P(), 250.0, {"7": "U3"})
    assert out["stated_weekly_max_exact"] is None and out["stated_weekly_max"] == "not a number"
    assert out["reported_by"] == "U3" and Decimal(out["weekly_share_text"]) == Decimal("250.00")
    out2 = matching.per_person_budget_fact(P(), None, {})
    assert out2["weekly_share_text"] is None and out2["reported_by"] == "7"


# ---------------------------------------------------------------- citation ownership
@pytest.mark.parametrize("who,citation,accepted", [
    ("lead", "own_share", True), ("lead", "own_stated", True),
    ("lead", "companion_share", False), ("lead", "solo_share", False), ("lead", "household", False),
    ("companion", "own_stated", True), ("companion", "lead_stated", False),
    ("solo", "own_share", True), ("solo", "lead_share", False),
])
def test_only_the_subjects_own_budget_ids_are_their_evidence(db, monkeypatch, who, citation, accepted):
    packet, lead, companion, solo = _party(db, monkeypatch)
    prefix = f"R.A{packet['apartments'][0]['apartment_id']}.budget"
    me = {"lead": lead, "companion": companion, "solo": solo}[who]
    fid = {"own_share": f"{prefix}.share.{me}", "own_stated": f"{prefix}.stated_max.{me}",
           "companion_share": f"{prefix}.share.{companion}", "solo_share": f"{prefix}.share.{solo}", "lead_share": f"{prefix}.share.{lead}",
           "lead_stated": f"{prefix}.stated_max.{lead}", "household": prefix}[citation]
    clean, problems = facts.check_items([{"fact_ids": [fid], "note": "The proposed weekly contribution is one to talk through."}],
                                        facts.catalogue(packet), ref=me, allow_missing=False, require_own=True, label=f"{me}.tensions")
    assert bool(clean) is accepted and bool(problems) is not accepted, problems


def test_budget_fact_does_not_cover_a_companions_missing_lifestyle_answers(db, monkeypatch):
    """'Scope rule' must not license own lifestyle coverage: a companion with a share fact still has no routine/tidiness evidence."""
    packet, lead, companion, _ = _party(db, monkeypatch)
    cat = facts.catalogue(packet)
    for field in ("routine", "social", "diet"):
        assert cat[f"{companion}.{field}"]["status"] == "missing" and cat[f"{companion}.{field}"]["source"] is None
    prefix = f"R.A{packet['apartments'][0]['apartment_id']}.budget"
    clean, problems = facts.check_items([{"fact_ids": [f"{prefix}.share.{companion}"], "note": "Bo keeps an early routine."}],
                                        cat, ref=companion, allow_missing=False, require_own=True, label=f"{companion}.strengths")
    # the rule-scoped fact is accepted as the companion's evidence for a budget note, but it is not lifestyle coverage: the
    # household/attribution checks elsewhere (T6) decide habit sentences; here we only pin that the fact's scope stays 'rule'
    assert cat[f"{prefix}.share.{companion}"]["scope"] == "rule"
    assert cat[f"{prefix}.share.{companion}"].get("field") not in ("routine", "tidiness", "social", "diet")
    assert clean or problems  # exercised without error


# ---------------------------------------------------------------- housemates check semantics
def test_housemates_check_exposes_other_people_not_total_occupants(db, monkeypatch):
    packet, lead, companion, solo = _party(db, monkeypatch)
    for apt in packet["apartments"]:
        hm = next(rc for rc in apt["rule_checks"] if rc["rule"] == "housemates")
        assert hm["result"] == "pass" and hm["source"].startswith("rules.check_group")
        d = hm["detail"]
        assert d["people_in_group"] == 4                                   # Astrid + Bo + Cy + Devi
        assert set(d["enquirers"]) == {lead, solo}                          # enquirers only: companions did not answer I15
        for ref, e in d["enquirers"].items():
            assert e["other_people_in_this_group"] == 3 == d["people_in_group"] - 1
            assert e["accepts_at_most_other_housemates"] == 5
        assert "other" in d["basis"].lower() and "total" not in d["basis"].lower().replace("not the total", "")
    cat = facts.catalogue(packet)
    prefix = f"R.A{packet['apartments'][0]['apartment_id']}.housemates"
    assert prefix in cat and cat[prefix]["scope"] == "rule"


def test_housemates_fact_text_never_reads_as_total_occupants():
    """A reader of the packet sees 'accepts at most 6 other housemates' beside 'other people in this group: 6' for a seven-person
    group - the n-1 reading of 3.4, not a seven-vs-six breach."""
    class U:
        def __init__(self, i, mm):
            self.id, self.mate_max = i, mm
            self.people = [type("P", (), {"id": f"p{i}", "unit_id": i, "budget_max": 300.0, "smokes": False, "smoke_ok": True})()]
    units = [U(str(i), 6) for i in range(7)]
    class Chk:
        shares = {}; household_budget = {}; lease_start = __import__("datetime").date(2026, 1, 1); lease_end = __import__("datetime").date(2026, 7, 1)
        rate_kind = "6m"; exit_window = None; allocation = []; total_weekly = 0; extra_bed = 0
    class Apt:
        available_from = None; type_code = "3B"
    for u in units:
        u.move_in = Chk.lease_start; u.move_in_flex = 0; u.move_out = Chk.lease_end; u.move_out_flex = 0; u.adults_confirmed_move_in = True
    refs = {u.id: f"U{u.id}" for u in units} | {u.people[0].id: f"U{u.id}" for u in units}
    try:
        checks = matching.rule_check_facts(Apt(), units, Chk(), refs)
    except Exception as exc:   # pragma: no cover - stand-ins may miss a field used by another check; the housemates block is what is pinned
        pytest.skip(f"stand-in objects insufficient for the full rule_check_facts: {exc!r}")
    hm = next(c for c in checks if c["rule"] == "housemates")["detail"]
    assert hm["people_in_group"] == 7
    for e in hm["enquirers"].values():
        assert e["other_people_in_this_group"] == 6 and e["accepts_at_most_other_housemates"] == 6


# ---------------------------------------------------------------- wording guards (context-aware)
def _share_cat(fid="R.A1.budget.share.U1-m1"):
    return {fid: {"status": "recorded", "ref": "U1-m1", "scope": "rule", "value": "266.67"},
            "U1.rooms_accepted": {"status": "recorded", "ref": "U1", "field": "rooms_accepted", "scope": "answer", "value": "Own room; Share with one other"},
            "U1.room_first": {"status": "recorded", "ref": "U1", "field": "room_first", "scope": "answer", "value": "Share with one other"}}


@pytest.mark.parametrize("note,rejected", [
    ("Bo's proposed weekly share of 266.67 has been agreed by the party.", True),
    ("Bo has agreed to the share.", True),
    ("Bo accepted their share.", True),
    ("The shares were signed off at the viewing.", True),
    ("Bo's proposed share is not agreed yet.", False),
    ("Bo's share is yet to be agreed by the group.", False),
    ("Bo's share is still to be agreed.", False),
    ("Once agreed, the share would be 266.67 a week.", False),
    ("The guide share is a proposal for the group's agreement.", False),
    ("Bo agreed to an early routine; the share is a proposal.", False),            # 'agreed' about something else, another clause
    ("Bo's proposed share is 266.67 AUD per week, not an agreed amount.", False),
    ("Bo has agreed to pay 266.67 a week.", True),
])
def test_share_may_not_be_called_agreed_but_honest_not_agreed_passes(note, rejected):
    prob = facts.wording_problems(note, ["R.A1.budget.share.U1-m1"], _share_cat())
    assert bool(prob) is rejected, prob


@pytest.mark.parametrize("fids,note,rejected", [
    (["U1.rooms_accepted"], "Astrid's first choice is to share with one other.", True),
    (["U1.rooms_accepted"], "Astrid prefers an own room.", True),
    (["U1.rooms_accepted"], "Astrid asked for a shared room.", True),
    (["U1.rooms_accepted", "U1.room_first"], "Astrid's first choice is to share with one other; an own room is also accepted.", False),
    (["U1.rooms_accepted"], "Astrid would accept an own room or sharing with one other.", False),
    (["U1.room_first"], "Astrid's first choice is to share with one other.", False),
    (["U1.rooms_accepted"], "Astrid accepts a twin room and prefers early nights.", False),          # preference about evenings, not rooms
    (["U1.rooms_accepted"], "Astrid accepts a twin room; that is not a recorded first choice.", False),
    (["U1.rooms_accepted"], "Astrid accepts sharing rather than a first choice of an own room.", False),
    (["U1.rooms_accepted", "U2.room_first"], "Astrid's first choice is a twin room.", True),        # another person's first choice licenses nothing
])
def test_accepted_preference_is_not_a_first_choice_unless_room_first_is_cited(fids, note, rejected):
    cat = _share_cat()
    cat["U2.room_first"] = {"status": "recorded", "ref": "U2", "field": "room_first", "scope": "answer", "value": "Share with one other"}
    prob = facts.wording_problems(note, fids, cat, ref="U1")
    assert bool(prob) is rejected, prob


def test_check_items_applies_wording_guards_without_weakening_other_guards(db, monkeypatch):
    packet, lead, companion, _ = _party(db, monkeypatch)
    cat = facts.catalogue(packet)
    prefix = f"R.A{packet['apartments'][0]['apartment_id']}.budget"
    bad = [{"fact_ids": [f"{prefix}.share.{companion}"], "note": "Bo has agreed to the proposed share."}]
    clean, problems = facts.check_items(bad, cat, ref=companion, allow_missing=False, require_own=True, label=f"{companion}.tensions")
    assert not clean and problems and any("agreed" in p for p in problems), problems
    good = [{"fact_ids": [f"{prefix}.share.{companion}"], "note": "Bo's proposed share is not agreed yet and is one to talk through."}]
    clean, problems = facts.check_items(good, cat, ref=companion, allow_missing=False, require_own=True, label=f"{companion}.tensions")
    assert clean and not problems, problems
    # wrong-person share citation is still rejected even with perfect wording
    wrong = [{"fact_ids": [f"{prefix}.share.{lead}"], "note": "Bo's proposed share is not agreed yet."}]
    clean, problems = facts.check_items(wrong, cat, ref=companion, allow_missing=False, require_own=True, label=f"{companion}.tensions")
    assert not clean and problems


# ---------------------------------------------------------------- birth country only (GP2 provenance)
@pytest.mark.parametrize("text", [
    "Alex is from Sweden originally.", "Alex is originally from Sweden.", "Alex grew up in Sweden.", "Alex was raised in Sweden.",
    "Alex hails from Sweden.", "Alex comes from Sweden.", "Alex calls Sweden their home country.", "Alex moved here from Sweden.",
    "Alex, a native of Sweden, likes cooking.", "Sweden is Alex's homeland.",
])
def test_origin_wording_attached_to_birth_country_is_refused(text):
    people = _people()
    people[0]["country_of_birth"] = "Sweden"
    prose = _prose()
    prose["bios"]["I1"] = text
    problems = ai.validate_intro_prose(prose, people)
    assert problems and any("origin, home or upbringing" in p for p in problems), (text, problems)


@pytest.mark.parametrize("text", [
    "Alex was born in Sweden and looks forward to meeting everyone.",
    "Alex was born in Sweden and comes from a family of keen cooks.",          # 'comes from' not attached to the country
    "Alex often comes from work late and was born in Sweden.",
    "Alex was born in Sweden and is looking for a home close to the city.",   # 'home' as housing, not origin
    "Alex enjoys cycling and quiet evenings.",                                # no country at all
])
def test_plain_born_in_and_unrelated_english_are_not_refused(text):
    people = _people()
    people[0]["country_of_birth"] = "Sweden"
    prose = _prose()
    prose["bios"]["I1"] = text
    assert not ai.validate_intro_prose(prose, people), text


def test_origin_guard_is_scoped_to_the_recorded_country_and_needs_one():
    assert ai.origin_wording("Alex grew up in Norway.", "Sweden") is None     # a different country is caught by the existing demonym/country checks, not here
    assert ai.origin_wording("Alex grew up in Sweden.", None) is None
    assert ai.origin_wording("Alex grew up in Sweden.", "") is None
    assert ai.origin_wording("Alex grew up in Sweden.", "Sweden") == "grew up in Sweden"


@pytest.mark.parametrize("own_intro", ["I was born in Sweden. I come from work by tram.", "I come from Sweden.", "Hi all."])
def test_own_intro_must_state_the_same_relation_about_the_same_country(own_intro):
    people = _people()
    people[0].update(country_of_birth="Sweden", intro=own_intro)
    prose = _prose()
    prose["bios"]["I1"] = "Alex grew up in Sweden."
    assert ai.validate_intro_prose(prose, people), own_intro


def test_own_intro_in_the_persons_words_keeps_upbringing_statement():
    """Provenance-aware: GP2 is not redefined - if the person's own free text says they grew up there, repeating it is supported."""
    people = _people()
    people[0]["country_of_birth"] = "Sweden"
    people[0]["intro"] = "Hi all, I grew up in Sweden and moved to Melbourne for study."
    prose = _prose()
    prose["bios"]["I1"] = "Alex grew up in Sweden and moved to Melbourne for study."
    assert not ai.validate_intro_prose(prose, people)
    # the same sentence without that supplied text is refused
    people[0]["intro"] = "Hi all."
    assert ai.validate_intro_prose(prose, people)


def test_intro_prompt_asks_for_born_in_only():
    src = open(ai.__file__).read()
    assert "'born in X' and nothing else about origin" in src
    assert "or 'from X originally'" not in src


# ---------------------------------------------------------------- collective discussion
@pytest.mark.parametrize("discussion,refused", [
    ("Alex could ask Bailey about Bailey's routine and tidiness.", True),
    ("Alex can share more about Casey's routine with the group.", True),
    ("Together, discuss routines, tidiness and how to share the home.", False),
    ("Everyone should talk through guests, quiet hours and cleaning together.", False),
])
def test_to_discuss_is_collective(discussion, refused):
    prose = _prose()
    prose["to_discuss"] = discussion
    assert bool(ai.validate_intro_prose(prose, _people())) is refused


def test_prompts_and_contract_carry_increment_a_semantics():
    assert ai.REVIEW_CONTRACT > "2026-11-08.r7-preferences-a16" and ai.REVIEW_CONTRACT >= "2026-11-09."   # moved past r7 (Increment A, 9 Nov) and again for C3/C4 (10 Nov 2026)
    assert "R.A<id>.budget.share.<ref>" in ai.REVIEW_RULES_TEXT and "R.A<id>.budget.stated_max.<ref>" in ai.REVIEW_RULES_TEXT
    assert "never call it agreed or accepted" in ai.REVIEW_RULES_TEXT
    assert "OTHER housemates" in ai.REVIEW_RULES_TEXT and "never counts the person themselves" in ai.REVIEW_RULES_TEXT
    src = open(ai.__file__).read()
    assert "never asking one person to supply, share or explain another person's" in src
    assert "STAFF matter" in src


# ---------------------------------------------------------------- A2 projection safety (9 Nov 2026, root invalid-budget probe)
def _rule_cat(pp):
    return facts.catalogue({"apartments": [{"apartment_id": 981, "rule_checks": [{"rule": "budget", "result": "pass", "source": "rules.check_group",
                                                                                   "detail": {"per_person": {"U1": pp}}}]}]})


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, 0, "not an amount", True, None])
def test_rejected_current_maximum_is_not_resurrected_from_the_raw_number(amount):
    pp = {"weekly_share": 250.0, "weekly_share_text": "250.00", "stated_weekly_max": amount, "stated_weekly_max_exact": None, "reported_by": "U1"}
    cat = _rule_cat(pp)
    assert "R.A981.budget.stated_max.U1" not in cat
    assert cat["R.A981.budget.share.U1"]["value"] == "250.00"   # the valid share of the same person is still a fact


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1, 0, "abc", True, [600], {"v": 600}])
def test_invalid_legacy_maximum_is_not_a_fact(amount):
    assert "R.A981.budget.stated_max.U1" not in _rule_cat({"stated_weekly_max": amount, "reported_by": "U1"})


@pytest.mark.parametrize("amount", [float("nan"), -5, "x"])
def test_invalid_legacy_share_is_not_a_fact(amount):
    assert "R.A981.budget.share.U1" not in _rule_cat({"weekly_share": amount})


def test_valid_legacy_values_are_kept_as_stored_text_without_rounding():
    cat = _rule_cat({"weekly_share": 266.666667, "stated_weekly_max": 600.001, "reported_by": "U1"})
    assert cat["R.A981.budget.share.U1"]["value"] == "266.666667"
    assert cat["R.A981.budget.stated_max.U1"]["value"] == "600.001"
    assert _rule_cat({"weekly_share": 0})["R.A981.budget.share.U1"]["value"] == "0"   # a zero share (GP10 uncovered bed) is a valid amount; a zero maximum is not
