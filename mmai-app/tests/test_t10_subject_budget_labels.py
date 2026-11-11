"""Increment A (9 Nov 2026) staff rendering of the lead builder's subject-attributed budget evidence (R.A<id>.budget.share.<ref>,
R.A<id>.budget.stated_max.<ref>) and the housemates check (R.A<id>.housemates). Pure renderer tests: whole-token consumption in
one pass, correct person attribution, lead-reported provenance named separately, no leaked ref / suffix / machine key, and
neutral labels when a ref is unknown or belongs to an earlier member set."""
import re

import pytest

from server import staff_view as sv

NAMES = {"U1": "Alex", "U1-m1": "Bailey", "U2": "Casey"}
APTS = {"A12": "12 Synthetic Street"}
LEAK = re.compile(r"\bU\d+(?:-m\d+)?\b|\.share\b|\.stated_max\b|R\.A\d+|\bA12\b")


@pytest.mark.parametrize("field,must", [("share", "proposed weekly share"), ("stated_max", "stated weekly maximum")])
def test_plain_text_consumes_the_whole_five_segment_token(field, must):
    out = sv.plain_text(f"Discuss R.A12.budget.{field}.U1-m1 before the viewing.", NAMES, APTS)
    assert out.startswith("Discuss Bailey's " + must) and "12 Synthetic Street" in out
    assert not LEAK.search(out), out
    assert "Alex" not in out   # the companion's own fact is never attributed to the lead


def test_share_is_described_as_a_proposal_not_an_agreed_amount():
    out = sv.describe_fact("R.A12.budget.share.U1-m1", "266.67", NAMES, APTS)
    assert out == "Bailey's proposed weekly share at 12 Synthetic Street (a proposal for the group's agreement, not an agreed amount): 266.67"


def test_stated_max_names_the_lead_who_reported_it_separately():
    out = sv.describe_fact("R.A12.budget.stated_max.U1-m1", "300", NAMES, APTS, source="U1")
    assert out == "Bailey's stated weekly maximum (budget check at 12 Synthetic Street): 300 (reported by Alex for the party)"
    own = sv.describe_fact("R.A12.budget.stated_max.U1", "450", NAMES, APTS, source="U1")
    assert own.endswith(": 450") and "reported by" not in own   # the lead's own answer carries no report suffix


def test_missing_subject_fact_is_marked_not_recorded_without_a_value():
    out = sv.describe_fact("R.A12.budget.stated_max.U2", None, NAMES, APTS, missing=True)
    assert out == "Casey's stated weekly maximum (budget check at 12 Synthetic Street) (not recorded)"


def test_unknown_ref_gets_a_neutral_label_not_the_raw_ref():
    for fn in (lambda: sv.describe_fact("R.A12.budget.share.U7", "1", NAMES, APTS),
               lambda: sv.plain_text("R.A12.budget.share.U7", NAMES, APTS),
               lambda: sv._fact_line("R.A12.budget.share.U7", NAMES, APTS)):
        out = fn()
        assert "one member" in out.lower() and "U7" not in out, out


def test_earlier_member_set_is_never_attributed_to_todays_names():
    out = sv.plain_text("R.A12.budget.share.U1-m1=266.67", sv.EarlierMembers(), APTS)
    assert out.startswith("one member's proposed weekly share") and "Bailey" not in out and "U1-m1" not in out


def test_inserted_names_and_addresses_are_not_rescanned():
    out = sv.plain_text("R.A12.budget.share.U1-m1", {"U1-m1": "allowance_fraction"}, {"A12": "12 bunk_ok Lane"})
    assert out.startswith("allowance_fraction's proposed weekly share at 12 bunk_ok Lane")
    assert "U1-m1" not in out and ".share" not in out


def test_four_segment_detail_still_renders_as_before():
    assert sv.plain_text("R.A12.budget.limit_exact", NAMES, APTS) == "exact weekly rent limit for the household (budget check) at 12 Synthetic Street"
    assert sv.describe_fact("R.A12.budget", "pass", NAMES, APTS) == "Budget check at 12 Synthetic Street: pass"


def test_housemates_check_and_detail_keys_have_plain_labels():
    out = sv.plain_text("R.A12.housemates: accepts_at_most_other_housemates 6, other_people_in_this_group 6", NAMES, APTS)
    assert out.startswith("housemates check at 12 Synthetic Street")
    assert "accepts_at_most_other_housemates" not in out and "other_people_in_this_group" not in out
    assert "not counting themselves" in out
    assert sv.FACT_LABELS["mate_max"] == "maximum number of other housemates accepted (not counting themselves)"
    assert "total occupants" not in sv.plain_text("U1.mate_max", NAMES, APTS)


def test_friendly_item_renders_bracketed_subject_evidence_with_provenance():
    item = "Budget to discuss  [R.A12.budget.stated_max.U1-m1=300 (reported by U1 for the party); R.A12.budget.share.U1-m1=266.67]"
    out = sv.friendly_item(item, NAMES, APTS)
    assert out["note"] == "Budget to discuss"
    assert out["evidence"] == ["Bailey's stated weekly maximum (budget check at 12 Synthetic Street): 300 (reported by Alex for the party)",
                               "Bailey's proposed weekly share at 12 Synthetic Street (a proposal for the group's agreement, not an agreed amount): 266.67"]
    structured = sv.friendly_item({"note": "Shares", "fact_ids": ["R.A12.budget.share.U1-m1", "R.A12.budget.share.U1"]}, NAMES, APTS)
    assert structured["evidence"][0].startswith("Bailey's proposed weekly share at 12 Synthetic Street")
    assert structured["evidence"][1].startswith("Alex's proposed weekly share at 12 Synthetic Street")


def test_non_budget_five_segment_fact_is_still_person_attributed():
    out = sv.plain_text("R.A12.beds.room_first.U2", NAMES, APTS)
    assert out == "Casey's first-choice room arrangement (beds check at 12 Synthetic Street)"
