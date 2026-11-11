"""Launch-neutral engine paths (Bill, 8 November 2026).

The initial launch uses no protected-trait group filter or ranking: no country/region/nationality defaults, no sex, no age
preference, no language weight. These tests pin that end to end, on real database rows rather than on hand-built units:

  1. candidate formation (`candidate_groups`, `run_match`) is invariant to legacy origin / language / region answers;
  2. replacement (`_refill_options`) is invariant to them too;
  3. a rerun of `full_run` after those legacy answers change keeps every group unchanged (nothing is re-formed or re-queued);
  4. the assessment packets (`matching._group_payload`, `ai_queue._payload`) carry positional refs and approved practical
     fields only: no name, email, phone, country, language, region, free text, sex or age; and the prompt the AI model sees
     carries none of those values either;
  5. identity is preserved where it belongs: the enquiry record and the introduction payload still carry names and countries.

"Legacy answers" are written straight into the still-existing columns (own_region, avoid_answer, avoid_countries,
avoid_regions, languages, birth_country) as a pre-8-Nov row would have them; the engine must not read them.
"""
import json
from datetime import date

import pytest

from tests.conftest import fake_findings

from server import ai, ai_queue, matching, rules
from server.db import now_iso, rows
from server.enquiries import create_enquiry, get_enquiry
from tests.test_data_and_api import good_answers, member  # noqa: F401

SWE = dict(birth_country_raw="Sweden", live_country_raw="Sweden")
IND = dict(birth_country_raw="India", live_country_raw="India")


# synthetic availability: 5 empty days before the default 1 Feb 2027 move-in, inside the A17 5% cap for the default 159-day stay
def _apt(db, type_code="2B2B", avail="2027-01-27", beds=("1 king or 2 singles", "1 king or 2 singles", None), rent12=1150.0, rent6=1225.0, address="Unit 1 / 1 Neutral Street"):
    db.execute("""INSERT INTO apartments(city, address, address_raw, suburb, type_code, bathrooms, rent_12m, rent_6m, available_from, min_lease_months,
                                         bedroom1, bedroom2, bedroom3, bills_included, updated_at)
                  VALUES ('Melbourne', ?, ?, 'Melbourne', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
               (address, address, type_code, rules.APT_TYPES[type_code]["bathrooms"], rent12, rent6, avail, rules.DEFAULT_MIN_LEASE_MONTHS, beds[0], beds[1], beds[2], now_iso()))
    db.commit()
    return rows(db.execute("SELECT * FROM apartments WHERE address=?", (address,)))[0]


def _enq(db, first_name, email, **over):
    a = good_answers(first_name=first_name, email=email, **over)
    return create_enquiry(db, a, "Landing page", use_ai_countries=False, is_test=True)


def _pool(db):
    """Four compatible solo enquirers for a 2B2B (own rooms), all legally identical except identity; created in a fixed order."""
    created = []
    for i, (name, origin) in enumerate([("Astrid", SWE), ("Bo", SWE), ("Chandra", IND), ("Devi", IND)]):
        e = _enq(db, name, f"{name.lower()}@example.com", intro=f"Hi, I am {name} and I love cooking {origin['birth_country_raw']} food", **origin)
        db.execute("UPDATE enquiries SET created_at=? WHERE id=?", (f"2026-11-0{i + 1}T10:00:00+11:00", e["id"]))
        created.append(e["id"])
    db.commit()
    return created


def _write_legacy(db, ids, variant):
    """Pre-8-Nov answers that would have blocked or weighted pairs under the retired M8/M10-M12/M9 logic."""
    if variant == "none":
        db.execute("UPDATE enquiries SET own_region=NULL, avoid_answer='No', avoid_countries=NULL, avoid_regions=NULL, languages='[\"English\"]' WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
    elif variant == "hostile":
        # everyone wants their own region only, avoids the other pair's country/region, speaks a different language
        for eid, (reg, avoid_c, avoid_r, lang) in zip(ids, [("Prefer to", ["India"], ["South Asia"], ["Swedish"]), ("Prefer to", ["India"], ["South Asia"], ["Swedish"]),
                                                          ("Prefer to", ["Sweden"], ["Western"], ["Hindi"]), ("Prefer to", ["Sweden"], ["Western"], ["Tamil"])]):
            db.execute("UPDATE enquiries SET own_region=?, avoid_answer='Yes', avoid_countries=?, avoid_regions=?, languages=? WHERE id=?",
                       (reg, json.dumps(avoid_c), json.dumps(avoid_r), json.dumps(lang), eid))
    db.commit()


def _group_shapes(groups):
    return sorted((tuple(sorted(int(u.id) for u in g["units"])), round(g["rule_score"], 6)) for g in groups)


IDENTITY_WORDS = ("Astrid", "Bo", "Chandra", "Devi", "@example.com", "Sweden", "Swedish", "India", "Hindi", "Tamil", "South Asia", "Western", "cooking", "0400000000")


def _assert_identity_free(obj):
    blob = json.dumps(obj, default=str)
    for w in IDENTITY_WORDS:
        assert w not in blob, f"{w!r} leaked into the assessment packet"
    for k in matching.AI_PACKET_EXCLUDED:
        assert f'"{k}"' not in blob, f"excluded key {k!r} in packet"


# ---------------------------------------------------------------- 1. candidate formation ignores legacy answers
def test_candidate_groups_invariant_to_legacy_origin_and_language_answers(db):
    a = _apt(db)
    ids = _pool(db)
    apt = matching.apartment_from_row(a)
    _write_legacy(db, ids, "none")
    base, considered0 = matching.candidate_groups(apt, matching.load_pool(db, "test"), max_groups=None, seeds=None)
    assert base, "the four compatible solo enquirers must form at least one rule-passing group"
    _write_legacy(db, ids, "hostile")
    hostile, considered1 = matching.candidate_groups(apt, matching.load_pool(db, "test"), max_groups=None, seeds=None)
    assert _group_shapes(hostile) == _group_shapes(base) and considered1 == considered0
    # the mixed-origin pair is formed and scored exactly as the same-origin pair is
    shapes = {k for k, _ in _group_shapes(hostile)}
    assert (ids[0], ids[2]) in shapes or (ids[0], ids[3]) in shapes or (ids[1], ids[2]) in shapes or (ids[1], ids[3]) in shapes
    for g in hostile:
        assert "country" not in g["score_parts"] and "languages" not in g["score_parts"]
        u = g["units"][0]
        assert not hasattr(u.people[0], "avoid_countries") and not hasattr(u.people[0], "avoid_regions") and not hasattr(u.people[0], "avoid_answer")


def test_run_match_queues_same_groups_whatever_legacy_answers_say(db):
    a = _apt(db)
    ids = _pool(db)
    _write_legacy(db, ids, "hostile")
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    keys_hostile = sorted(g["member_key"] for g in rows(db.execute("SELECT member_key FROM groups WHERE match_run_id=?", (res["run_id"],))))
    assert keys_hostile and any("," in k for k in keys_hostile)
    # a fresh database state with neutral legacy columns forms the same member keys
    db.execute("DELETE FROM group_apartments"); db.execute("DELETE FROM group_members"); db.execute("DELETE FROM groups"); db.execute("DELETE FROM match_runs")
    db.execute("UPDATE enquiries SET status='New' WHERE is_test=1"); db.commit()
    _write_legacy(db, ids, "none")
    res2 = matching.run_match(db, a["id"], "tester", dataset="test")
    keys_none = sorted(g["member_key"] for g in rows(db.execute("SELECT member_key FROM groups WHERE match_run_id=?", (res2["run_id"],))))
    assert keys_none == keys_hostile


# ---------------------------------------------------------------- 2. replacement ignores legacy answers
def test_refill_options_invariant_to_legacy_answers(db):
    a = _apt(db)
    ids = _pool(db)
    apt = matching.apartment_from_row(a)
    kept_row = rows(db.execute("SELECT * FROM enquiries WHERE id=?", (ids[0],)))[0]
    kept = [matching.unit_from_row(kept_row, [])]
    _write_legacy(db, ids, "none")
    base = matching._refill_options(db, apt, a, kept, excluded={ids[1]}, dataset="test")
    assert base and all(ids[1] not in [int(u.id) for u in o["units"]] for o in base)
    _write_legacy(db, ids, "hostile")
    hostile = matching._refill_options(db, apt, a, kept, excluded={ids[1]}, dataset="test")
    assert _group_shapes(hostile) == _group_shapes(base)
    # with Bo excluded the only replacements are the two enquirers the legacy answers "avoided": they are offered, unpenalised
    added = {tuple(sorted(o["added"])) for o in hostile}
    assert added & {(ids[2],), (ids[3],), (ids[2], ids[3])}


# ---------------------------------------------------------------- 3. rerun after legacy answers change: nothing re-formed
def _fake_assess(groups):
    return [{"index": g.get("index", i), "rating": 7, "rationale": "fake", "verdict": "approve", "findings": fake_findings(g), "apartments": [{"apartment_id": x["apartment_id"], "reason": "r"} for x in g.get("apartments", [])]}
            for i, g in enumerate(groups)]


def test_full_run_rerun_ignores_legacy_answer_changes(db, monkeypatch):
    _apt(db)
    _apt(db, address="Unit 2 / 1 Neutral Street", avail="2027-01-25")
    ids = _pool(db)
    _write_legacy(db, ids, "none")
    monkeypatch.setattr(ai, "assess_groups_and_apartments", _fake_assess)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    r1 = matching.full_run(db, "tester", dataset="test")
    assert r1["groups_total"] > 0
    ai_queue.process_pending(db)
    before = rows(db.execute("SELECT id, member_key, ai_status, ai_rating FROM groups ORDER BY id"))
    assert before and all(g["ai_status"] == "ok" for g in before)
    _write_legacy(db, ids, "hostile")   # a legacy row's retired answers "change": the engine must not notice
    r2 = matching.full_run(db, "tester", dataset="test")
    after = rows(db.execute("SELECT id, member_key, ai_status, ai_rating FROM groups ORDER BY id"))
    assert [(g["id"], g["member_key"]) for g in after] == [(g["id"], g["member_key"]) for g in before]
    assert r2["groups_total"] == r1["groups_total"] and r2.get("groups_unchanged", r2["groups_total"]) == r1["groups_total"] and ai_queue.pending_count(db) == 0


# ---------------------------------------------------------------- 4. assessment packets are identity-free, by ref
def test_group_payload_and_queue_payload_are_identity_free_and_use_refs(db):
    a = _apt(db)
    ids = _pool(db)
    # a friends party so a non-lead person is in the packet too (I16(b) member country must not leak either)
    party = _enq(db, "Erik", "erik@example.com", party="With friends", party_size="2", party_members=[member(name="Fatima", email="fatima@example.com", birth_country_raw="Pakistan")],
                 rooms=["Share a room with one other person"], room_first="Share a room with one other person", budget_option="2B2B-twin", **SWE)
    _write_legacy(db, ids + [party["id"]], "hostile")
    apt = matching.apartment_from_row(a)
    cands, _ = matching.candidate_groups(apt, matching.load_pool(db, "test"), max_groups=None, seeds=None)
    with_party = [g for g in cands if any(int(u.id) == party["id"] for u in g["units"])]
    assert with_party, "the friends party must be able to join a group"
    for g in cands:
        p = matching._group_payload(a, g)
        # the AI-facing parts of the payload (what ai.assess_groups puts in its prompt) are identity-free; the rules allocation
        # (`allocation`, names for the staff screen and group_apartments.allocation_json) is NOT part of the AI packet
        _assert_identity_free({"members_for_ai": p["members_for_ai"], "person_refs": p["person_refs"], "lease": (p["lease_start"], p["lease_end"], p["rate_kind"])})
        refs = p["person_refs"]
        assert set(refs.values()) == {m["ref"] for m in p["members_for_ai"]} | {r for m in p["members_for_ai"] for r in m["party_members"]}
        for i, m in enumerate(p["members_for_ai"], start=1):
            assert m["ref"] == f"U{i}" and set(m) == {"ref", "party_members", "couples", "notes_facts", *matching.AI_PACKET_FIELDS, *matching.AI_PACKET_PREFERENCE_FIELDS}   # T4c: validated neutral notes projection
        assert all(isinstance(refs[k], str) and refs[k].startswith("U") for k in refs) and all(k.isdigit() or "-" in k or k for k in refs)
    # the replacement path sends _group_payload entries straight to ai.assess_groups: capture that prompt
    seen = {}

    def capture(prompt, model=None, max_tokens=1500, system=""):
        seen["prompt"] = prompt
        return json.dumps([{"index": i, "rating": 5, "rationale": "ok"} for i in range(len(cands))])

    import pytest as _pt
    mp = _pt.MonkeyPatch()
    mp.setattr(ai, "_ask", capture)
    try:
        out = ai.assess_groups(a, [matching._group_payload(a, g) for g in cands])
    finally:
        mp.undo()
    assert len(out) == len(cands)
    for w in IDENTITY_WORDS + ("Erik", "Fatima", "Pakistan"):
        assert w not in seen["prompt"], f"{w!r} reached the model through assess_groups"
    assert '"U1"' in seen["prompt"] and ("U1-m1" in seen["prompt"] or "U2-m1" in seen["prompt"]), "the party member appears by ref"
    # the queue's payload (what the AI batch actually receives) for a persisted group
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    for g in rows(db.execute("SELECT * FROM groups WHERE match_run_id=?", (res["run_id"],))):
        qp = ai_queue._payload(db, g)
        assert qp is not None
        _assert_identity_free({k: v for k, v in qp.items() if k != "apartments"})
        assert set(qp) == {"group_id", "key", "members_for_ai", "person_refs", "refs_json", "apartments", "evidence_hash", "round", "rule_version"}  # hash + round + refs_json (ref -> row ids, history provenance): bookkeeping for the writers, never in review_packet
        assert "refs_json" not in json.dumps(ai.review_packet([qp]), default=str) and "party_member_id" not in json.dumps(ai.review_packet([qp]), default=str)
        assert all(m["ref"].startswith("U") for m in qp["members_for_ai"])
        # apartments[].allocation is the stored rules allocation (person ids + names for staff); ai.assess_groups_and_apartments
        # maps occupants to refs and drops names before the prompt (pinned by test_prompt_sent_to_the_model_carries_refs_not_identity)
        for apt_entry in qp["apartments"]:
            for room in apt_entry["allocation"]:
                for o in room.get("occupants", []):
                    assert str(o["person_id"]) in qp["person_refs"], "every occupant must be mappable to a ref"


def test_prompt_sent_to_the_model_carries_refs_not_identity(db, monkeypatch):
    a = _apt(db)
    ids = _pool(db)
    _write_legacy(db, ids, "hostile")
    seen = {}

    def capture(prompt, model=None, max_tokens=1500, system=""):
        seen["prompt"], seen["system"] = prompt, system
        groups = json.loads(prompt[prompt.rindex("\n\n") + 2:])  # the packet is the trailing JSON list
        return json.dumps([{"index": g["index"], "rating": 6, "rationale": "ok", "verdict": "approve", "findings": [{"fact_ids": g["fact_ids"][:1], "note": "ok"}],
                            "apartments": [{"apartment_id": x["apartment_id"], "reason": "r"} for x in g["apartments_in_rule_order"]]} for g in groups])

    monkeypatch.setattr(ai, "_ask", capture)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    out = ai_queue.process_pending(db)
    assert out["ok"] == res["ai_queued"] > 0
    text = seen["prompt"] + seen["system"]
    for w in IDENTITY_WORDS:
        assert w not in text, f"{w!r} reached the model"
    assert "U1" in text and "nationality" not in text.lower().replace("country of birth, nationality", "")   # refs in; no nationality preference talk
    assert "does not use anyone's country of birth" in text and "Never infer, mention or weigh" in text
    for g in rows(db.execute("SELECT ai_status, ai_model FROM groups WHERE match_run_id=?", (res["run_id"],))):
        assert g["ai_status"] == "ok" and g["ai_model"] == ai.MODEL


# ---------------------------------------------------------------- 5. identity is kept where it belongs
def test_identity_preserved_for_record_and_introduction(db):
    ids = _pool(db)
    e = get_enquiry(db, ids[2])
    assert e["first_name"] == "Chandra" and e["birth_country_raw"] == "India" and "cooking" in e["intro"]
    u = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (ids[2],)))[0], [])
    assert u.people[0].name == "Chandra" and u.extra.get("intro")   # the unit still knows who the person is (GP2 introductions, history)
    assert "intro" in matching.AI_PACKET_EXCLUDED and "intro" not in matching.AI_PACKET_FIELDS   # ... but the assessment packet does not carry it
