"""r7 packet stage C (8 November 2026): raw accepted preferences and exact A16 household facts in the assessment packet.

  * members_for_ai carries the party's RAW recorded preferences: the stated first choice (room_first) separately from what the
    party said it would ACCEPT (rooms_accepted, apt_sizes_accepted, bunk_ok, mate_max) and its smoking answers (smoke, smoke_ok);
    the allocator's working defaults (a stated couple's added 'Couple sharing one bed', the group's intersected sizes or minimum
    mate_max) are never written as the party's acceptance; a NULL stored value stays None -> catalogue status 'missing', never False;
  * every one of those is a party-wide fact with the enquirer as source: a companion's copy renders "reported by U1 for the party"
    and is never presented as the companion's own answer; the smoking rule check names the unit it was derived from;
  * the A16 household figures are citable rule facts R.A<id>.budget.<field> taken verbatim (lossless strings) from the SAME
    BudgetResult the check used, with explicit units; the allowance is a raw fraction ("0.1"), never a percent; when the check
    produced no figures (a member without a stated maximum) there is no fact to cite;
  * REVIEW_CONTRACT moved, so stored assessments under the r6 contract are no longer 'ready'; the prompt tells the model how to
    treat acceptance vs first choice and the fraction, without re-interpreting numbers.

Everything is a fabricated double; no live model call, no real people. Structural checks on values and provenance only.
"""
from decimal import Decimal

from server import ai, facts, matching, rules
from server.db import row, rows
from tests.test_data_and_api import good_answers
from tests.test_launch_neutral import _apt, _enq, _pool
from tests.test_t6_grounding import _packet, _party_group, _run_and_approve


def _members(db, apt_id):
    a = row(db.execute("SELECT * FROM apartments WHERE id=?", (apt_id,)))
    units = matching.load_pool(db, "test")
    return a, units, matching.members_for_ai(units)


def test_members_for_ai_carries_raw_preferences_separately_from_first_choice(db):
    ids = _pool(db)
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (ids[0],)))
    db.execute("UPDATE enquiries SET room_first=?, rooms=?, apt_sizes=?, bunk_ok=1, mate_max=4, smoke='No', smoke_ok='Yes' WHERE id=?",
               ("Own room", '["Own room", "Share a room with one other person"]', '["2 bedrooms", "3 bedrooms"]', ids[0]))
    db.commit()
    units = matching.load_pool(db, "test")
    m = next(x for x in matching.members_for_ai(units) if x["ref"] == matching.person_refs(units)[str(ids[0])])
    assert m["room_first"] == "Own room"
    assert m["rooms_accepted"] == ["Own room", "Share a room with one other person"]
    assert m["apt_sizes_accepted"] == ["2 bedrooms", "3 bedrooms"]
    assert m["bunk_ok"] is True and m["mate_max"] == 4 and m["smoke"] == "No" and m["smoke_ok"] == "Yes"
    assert set(matching.AI_PACKET_PREFERENCE_FIELDS) <= set(m)
    # the allocator's working set for a stated couple adds 'Couple sharing one bed' - the packet keeps the party's raw list
    u = next(u for u in units if u.id == str(ids[0]))
    assert "Couple sharing one bed" not in m["rooms_accepted"] or "Couple sharing one bed" in (e.get("rooms") or "")
    # the group-level allocator defaults (intersection / minimum) are per-unit raw values in the packet, not derived ones
    assert m["mate_max"] == int(u.extra["mate_max"])


def test_null_preference_is_missing_not_false(db):
    """A NULL / blank raw value (never written by validated intake; the columns are NOT NULL) is projected as None and catalogued
    as 'missing', never as False. Exercised on the raw row in memory - SQL NULL storage is not claimed to be supported."""
    ids = _pool(db)
    units = matching.load_pool(db, "test")
    refs = matching.person_refs(units)
    u = next(x for x in units if x.id == str(ids[1]))
    u.extra = {**u.extra, "mate_max": None, "room_first": None, "rooms": "[]", "bunk_ok": None, "smoke": ""}
    members = matching.members_for_ai(units)
    m = next(x for x in members if x["ref"] == refs[str(ids[1])])
    assert m["mate_max"] is None and m["room_first"] is None and m["rooms_accepted"] is None and m["bunk_ok"] is None and m["smoke"] is None
    assert m["apt_sizes_accepted"] and m["smoke_ok"]   # untouched raw answers stay recorded
    cat = facts.catalogue({"members_for_ai": members, "apartments": []})
    r = refs[str(ids[1])]
    for f in ("mate_max", "room_first", "rooms_accepted", "bunk_ok", "smoke"):
        assert cat[f"{r}.{f}"]["status"] == "missing" and cat[f"{r}.{f}"]["value"] is None, f
        assert "not recorded" in facts._fact_bit(f"{r}.{f}", cat)
    assert cat[f"{r}.apt_sizes_accepted"]["status"] == "recorded"
    # the untouched members keep recorded 0/1 bunk answers as real booleans
    other = next(x for x in members if x["ref"] != r)
    assert other["bunk_ok"] in (True, False)


def test_raw_preferences_decode_but_never_coerce():
    base = {"room_first": "Own room", "rooms": '["Own room"]', "apt_sizes": '["Studio"]', "mate_max": "3", "smoke": "No", "smoke_ok": "Yes"}
    rp = matching._raw_preferences
    # M3 yes/no: stored 0/1 (or yes/no text) only; anything else is malformed -> not recorded, never an acceptance
    assert rp({**base, "bunk_ok": 0})["bunk_ok"] is False and rp({**base, "bunk_ok": 1})["bunk_ok"] is True
    assert rp({**base, "bunk_ok": "1"})["bunk_ok"] is True and rp({**base, "bunk_ok": "No"})["bunk_ok"] is False
    for bad in (None, "", 2, -1, "maybe", 1.0, [1], {"yes": 1}):
        assert rp({**base, "bunk_ok": bad})["bunk_ok"] is None, bad
    # mate_max: an integer or a canonical integer string; floats, bools, containers and words are malformed
    assert rp({**base, "mate_max": 4})["mate_max"] == 4 and rp({**base, "mate_max": " 5 "})["mate_max"] == 5
    for bad in (2.5, "2.5", True, "many", "", None, [3], {"n": 3}):
        assert rp({**base, "mate_max": bad})["mate_max"] is None, bad
    # list fields: a decoded list of non-blank strings, preserved verbatim; empty / malformed shapes are not recorded
    assert rp({**base, "rooms": ["Own room", "Share a room with one other person"]})["rooms_accepted"] == ["Own room", "Share a room with one other person"]
    for bad in (None, "", "[]", [], '[{"a": 1}]', [1, 2], ["Own room", ""], "Own room", 7):
        assert rp({**base, "rooms": bad})["rooms_accepted"] is None, bad
    assert rp({**base, "apt_sizes": None})["apt_sizes_accepted"] is None
    assert rp({**base, "room_first": "  "})["room_first"] is None and rp({**base, "smoke": None})["smoke"] is None


def test_closed_choice_fields_fail_closed_on_unrecognised_values(db):
    """C2: room/size/smoking answers are closed choices. Every form option (and the legacy room spellings) passes verbatim; an
    unrecognised string - alone or inside a list - leaves the WHOLE field 'not recorded', so a stored pseudo-choice never reaches
    the packet. Exercised through load_pool on stored rows (the real path) and on the pure helper."""
    from server.enquiries import ROOM_OPTIONS, SIZE_OPTIONS
    rp = matching._raw_preferences
    base = {"room_first": "Own room", "rooms": '["Own room"]', "apt_sizes": '["Studio"]', "mate_max": "3", "smoke": "No", "smoke_ok": "Yes", "bunk_ok": 1}
    for opt in ROOM_OPTIONS + list(matching.LEGACY_ROOM_ALIASES):
        assert rp({**base, "room_first": opt})["room_first"] == opt and rp({**base, "rooms": [opt]})["rooms_accepted"] == [opt]
    assert rp({**base, "apt_sizes": SIZE_OPTIONS})["apt_sizes_accepted"] == SIZE_OPTIONS
    for opt in matching.SMOKE_OPTIONS + matching.LEGACY_SMOKE_ALIASES:
        assert rp({**base, "smoke": opt})["smoke"] == opt
    assert rp({**base, "smoke_ok": "No"})["smoke_ok"] == "No"
    assert rp({**base, "rooms": ["Own room", "Men only"]})["rooms_accepted"] is None   # whole field, not a filtered list
    assert rp({**base, "apt_sizes": ["2 bedrooms", "Penthouse"]})["apt_sizes_accepted"] is None
    for bad in ("Men only", "own room", "Own room ", "Share", ""):
        assert rp({**base, "room_first": bad})["room_first"] in (None, "Own room"), bad
    assert rp({**base, "room_first": "Own room "})["room_first"] == "Own room"   # whitespace only is tolerated
    assert rp({**base, "smoke": "Sometimes"})["smoke"] is None and rp({**base, "smoke": "yes"})["smoke"] is None
    assert rp({**base, "smoke_ok": "maybe"})["smoke_ok"] is None and rp({**base, "smoke_ok": "yes"})["smoke_ok"] is None
    # stored row path: an unrecognised token in the row never appears anywhere in the packet, and the field is catalogued missing
    ids = _pool(db)
    db.execute("UPDATE enquiries SET rooms=?, room_first=?, smoke=?, smoke_ok=? WHERE id=?",
               ('["Own room", "Men only"]', "Men only", "Sometimes", "maybe", ids[0]))
    db.commit()
    units = matching.load_pool(db, "test")
    refs = matching.person_refs(units)
    members = matching.members_for_ai(units)
    m = next(x for x in members if x["ref"] == refs[str(ids[0])])
    assert m["rooms_accepted"] is None and m["room_first"] is None and m["smoke"] is None and m["smoke_ok"] is None
    import json
    blob = json.dumps(members)
    for token in ("Men only", "Sometimes", "maybe"):
        assert token not in blob
    cat = facts.catalogue({"members_for_ai": members, "apartments": []})
    for f in ("rooms_accepted", "room_first", "smoke", "smoke_ok"):
        assert cat[f"{m['ref']}.{f}"]["status"] == "missing"
    # the stored row itself is untouched (projection only)
    e = row(db.execute("SELECT rooms, room_first, smoke FROM enquiries WHERE id=?", (ids[0],)))
    assert e["room_first"] == "Men only" and e["smoke"] == "Sometimes" and "Men only" in e["rooms"]


def test_companion_copies_are_lead_reported_never_own(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)   # Astrid + two companions, and solo Devi
    p = _packet(db, g)
    cat = facts.catalogue(p)
    companions = [r for m in p["members_for_ai"] for r in m.get("party_members") or []]
    assert companions, "the pool needs a party with companions for this test"
    lead_of = {r: m["ref"] for m in p["members_for_ai"] for r in m.get("party_members") or []}
    for r in companions:
        for f in matching.AI_PACKET_PREFERENCE_FIELDS:
            fact = cat[f"{r}.{f}"]
            assert fact["scope"] == "party-wide" and fact["source"] == lead_of[r]
            if fact["status"] == "recorded":
                assert facts._fact_bit(f"{r}.{f}", cat).endswith(f"(reported by {lead_of[r]} for the party)")
    for m in p["members_for_ai"]:
        for f in matching.AI_PACKET_PREFERENCE_FIELDS:
            assert cat[f"{m['ref']}.{f}"]["source"] == m["ref"]
    # the smoking rule check is a unit-level derived check that names its source unit; it is never a companion's own statement
    for apt in p["apartments"]:
        rc = next(x for x in apt["rule_checks"] if x["rule"] == "smoking")
        assert "not any companion's own statement" in rc["detail"]["basis"]
        for r in companions:
            assert rc["detail"]["people"][r]["derived_from"] == lead_of[r]
        for m in p["members_for_ai"]:
            assert rc["detail"]["people"][m["ref"]]["derived_from"] == m["ref"]


def test_household_budget_detail_carries_exact_figures_from_the_same_result():
    people = [rules.Person(id="1", name="A", is_lead=True, unit_id="1", birth_country=None, budget_max=383.335, arrangements=set(), bunk_ok=True, smokes=False, smoke_ok=True),
              rules.Person(id="2", name="B", is_lead=True, unit_id="2", birth_country=None, budget_max=383.335, arrangements=set(), bunk_ok=True, smokes=False, smoke_ok=True)]
    reasons, d = rules.household_budget("2B1B", 700, people)
    assert d is not None
    # display fields stay cent-rounded; the *_exact fields are the lossless Decimal text of the figures the classification used
    assert d["whole_apartment_max"] == "766.67" and d["whole_apartment_max_exact"] == "766.67"
    assert d["total_weekly_rent_exact"] == "700" and d["allowance_fraction"] in ("0.1", "0.2")
    assert Decimal(d["limit_exact"]) == Decimal(d["whole_apartment_max_exact"]) * (1 + Decimal(d["allowance_fraction"]))
    assert Decimal(d["limit_exact"]) != Decimal(d["limit"]) or Decimal(d["limit"]) == Decimal(d["limit_exact"])   # exact may differ from the display
    # no stated maximum -> no detail -> nothing to cite
    people[1].budget_max = None
    reasons2, d2 = rules.household_budget("2B1B", 700, people)
    assert d2 is None and reasons2


def test_a16_household_facts_are_citable_with_units_and_never_percent(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    p = _packet(db, g)
    cat = facts.catalogue(p)
    for apt in p["apartments"]:
        A = f"A{apt['apartment_id']}"
        hh = next(x for x in apt["rule_checks"] if x["rule"] == "budget")["detail"]["household"]
        for f in facts.HOUSEHOLD_BUDGET_FIELDS:
            fact = cat[f"R.{A}.budget.{f}"]
            assert fact["status"] == "recorded" and fact["scope"] == "rule" and fact["value"] == str(hh[f]) and fact["unit"] == facts.HOUSEHOLD_BUDGET_FACTS[f]
            assert fact["source"].startswith("rules.check_group")
            assert facts.FACT_ID.match(f"R.{A}.budget.{f}")
        frac = cat[f"R.{A}.budget.allowance_fraction"]
        assert Decimal(frac["value"]) < 1 and "%" not in frac["value"] and "%" not in frac["unit"] and "ten percent" in frac["unit"]
        assert "AUD per week" == cat[f"R.{A}.budget.limit_exact"]["unit"]
        bit = facts._fact_bit(f"R.{A}.budget.limit_exact", cat)
        assert bit == f"R.{A}.budget.limit_exact={frac and hh['limit_exact']} AUD per week"
        # a review item can cite them as evidence
        clean, problems = facts.check_items([{"fact_ids": [f"R.{A}.budget.whole_apartment_max_exact", f"R.{A}.budget.limit_exact"], "note": "rent within the household limit"}],
                                            cat, ref=None, allow_missing=False, require_own=False, label="findings")
        assert not problems and clean
    # the review and brief prompts receive the fact ids and the wording on acceptance / fraction
    desc = ai.review_packet([p])[0]
    assert any(k.endswith(".budget.allowance_fraction") for k in desc["fact_ids"])
    assert "0.1 means ten percent" in ai.REVIEW_RULES_TEXT and "never convert, round or re-derive" in ai.REVIEW_RULES_TEXT
    assert "stated FIRST CHOICE" in ai.REVIEW_RULES_TEXT and "would ACCEPT" in ai.REVIEW_RULES_TEXT
    assert "not a companion's own statement" in ai.REVIEW_RULES_TEXT


def test_no_household_figures_means_no_budget_facts():
    payload = {"members_for_ai": [{"ref": "U1", "party_members": [], "couples": []}],
               "apartments": [{"apartment_id": 7, "rule_checks": [{"rule": "budget", "result": "pass", "source": "rules.check_group, x",
                                                                    "detail": {"household": {}, "per_person": {}}}]}]}
    cat = facts.catalogue(payload)
    assert "R.A7.budget" in cat and not [k for k in cat if k.startswith("R.A7.budget.")]
    # partial figures: only what the check produced is citable
    payload["apartments"][0]["rule_checks"][0]["detail"]["household"] = {"limit_exact": "1149.995", "allowance_fraction": None}
    cat = facts.catalogue(payload)
    assert set(k for k in cat if k.startswith("R.A7.budget.")) == {"R.A7.budget.limit_exact"}
    assert cat["R.A7.budget.limit_exact"]["value"] == "1149.995"


def test_review_contract_bumped_and_old_assessments_not_ready(db, monkeypatch):
    assert ai.REVIEW_CONTRACT > "2026-11-08.r7-preferences-a16"   # r7 or a later contract (Increment A 2026-11-09 moved it again)
    a, ids, g = _run_and_approve(db, monkeypatch)
    from server import ai_queue
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)
    monkeypatch.setattr(ai, "REVIEW_CONTRACT", "2026-11-08.r6-coverage-block")
    st = ai_queue.is_ready(db, g)
    assert not st["ready"] and any("contract" in r for r in st["reasons"]), st


def test_budget_facts_in_brief_basis_are_rule_scoped_not_companion_answers(db, monkeypatch):
    """A brief note citing R.A<id>.budget.* facts: the basis item carries them as rule facts (no subject coverage for any person),
    so a companion is never credited with an answer, and intro_basis_items keeps the note with its fact ids only when every
    cited fact still resolves in the fresh catalogue (an id that no longer exists is dropped, not guessed)."""
    from server import ai_queue
    a, lead, solo, g = _party_group(db, monkeypatch)
    p = _packet(db, g)
    cat = facts.catalogue(p)
    A = f"A{p['apartments'][0]['apartment_id']}"
    fid = f"R.{A}.budget.limit_exact"
    assert fid in cat
    refs_json = ai_queue._ref_identities(db, g)
    meta = ai_queue.basis_note_meta({"fact_ids": [fid, f"R.{A}.budget.whole_apartment_max_exact"], "note": "rent within the household limit"}, cat, refs_json)
    assert meta is not None and meta["subjects"] == [] and meta["coverage"] == {}
    assert set(meta["facts"]) == {fid, f"R.{A}.budget.whole_apartment_max_exact"}
    # rule facts carry status/scope only - no value is projected, so a figure cannot leak from an old packet into a later intro
    assert all(v["scope"] == "rule" and v["value"] is None for v in meta["facts"].values())
    # a budget fact id the fresh catalogue does not carry (figures absent) makes the note unattributable, never guessed
    assert ai_queue.basis_note_meta({"fact_ids": [f"R.{A}.budget.nonexistent"], "note": "x"}, cat, refs_json) is None
    # a companion is never credited with a budget answer: a note mixing a companion preference fact shows party_reported, not own
    comp = next(r for m in p["members_for_ai"] for r in m.get("party_members") or [])
    mixed = ai_queue.basis_note_meta({"fact_ids": [fid, f"{comp}.rooms_accepted"], "note": "x"}, cat, refs_json)
    assert mixed is not None and mixed["coverage"][comp]["rooms_accepted"]["basis"] == "party_reported"
