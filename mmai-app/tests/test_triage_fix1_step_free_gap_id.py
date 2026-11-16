"""Triage fix 1 (9 Nov 2026): a member's ESSENTIAL step-free need puts an explicitly MISSING apartment-level fact id
A<id>.step_free_access into the catalogue, so a review / brief gap item has an unrecorded id to cite. Fabricated doubles only.
The id is never recorded (a lift is never proof), 'matters' never creates it, strict gap citation stays strict."""
from server import ai, facts

APT = {"apartment_id": 74, "address": "1 Test St", "type_label": "2 bedroom", "suburb": "CBD", "bills_included": 0, "available_from": "2027-01-01", "lease_start": "2027-02-01",
       "lease_end": "2027-08-01", "stay_days": 181, "rate_band": "6-month band", "total_weekly": 900, "extra_bed": 0, "campus_km": 1.2,
       "features": [{"key": "lift_access", "label": "Lift access", "recorded": "available", "wanted_by": ["U1"]}], "rule_checks": []}
MEMBER = {"ref": "U1", "party_members": [], "couples": None, "routine": "Night owl", "tidy": "Relaxed", "social": "Quiet", "guests": "Often", "diet": "Other",
          "doing": "Working", "notes_facts": [{"category": "step_free_access", "strength": "essential", "value": None, "basis": "self-report (free text), unverified", "source_field": "else_text"}]}


def _packet(strength="essential"):
    m = dict(MEMBER, notes_facts=[dict(MEMBER["notes_facts"][0], strength=strength)])
    return {"members_for_ai": [m], "person_refs": {"1": "U1"}, "apartments": [dict(APT)]}


def test_essential_need_adds_missing_apartment_fact_only():
    cat = facts.catalogue(_packet())
    f = cat["A74.step_free_access"]
    assert f["status"] == "missing" and f["value"] is None and f["scope"] == "apartment" and f["wanted_by"] == ["U1"]
    assert "A74.step_free_access" in ai.review_packet([_packet()])[0]["missing_fact_ids"]
    # a lift feature never records it (the id stays missing with lift 'available'), and 'matters' never creates it
    assert cat["A74.feature.lift_access"]["status"] == "recorded" and f["status"] == "missing"
    assert "A74.step_free_access" not in facts.catalogue(_packet("matters"))
    assert "A74.step_free_access" not in facts.catalogue({"members_for_ai": [dict(MEMBER, notes_facts=[])], "person_refs": {"1": "U1"}, "apartments": [dict(APT)]})


def test_gap_item_can_cite_the_missing_id_and_strictness_is_kept():
    p = _packet()
    cat = facts.catalogue(p)
    ok, pr = facts.check_items([{"fact_ids": ["U1.notes.step_free_access", "A74.step_free_access"], "note": "step-free access is not recorded; staff must confirm", "kind": "gap"}],
                               cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert not pr and ok and ok[0]["kind"] == "gap"
    # citing only the RECORDED notes id is still not a gap (the REQ 60 shape stays rejected)
    _, pr2 = facts.check_items([{"fact_ids": ["U1.notes.step_free_access"], "note": "apartment access isn't recorded", "kind": "gap"}],
                               cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert pr2 and "gap" in pr2[0]
    # the missing id used as plain evidence is rejected
    _, pr3 = facts.check_items([{"fact_ids": ["A74.step_free_access"], "note": "the apartment is step-free"}], cat, ref=None, allow_missing=False, require_own=False, label="findings")
    assert pr3


def test_contract_bumped_so_earlier_reviews_refresh():
    assert ai.REVIEW_CONTRACT == "2026-11-16.no-distance"   # Value/provenance delivered with the catalogue id.
    assert "A<id>.step_free_access" in ai._REVIEW_PROMPT_TEXT() if hasattr(ai, "_REVIEW_PROMPT_TEXT") else True
