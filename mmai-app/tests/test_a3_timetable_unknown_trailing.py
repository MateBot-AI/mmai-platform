"""9 Nov 2026 (C200 live-1 root replay: two FALSE POSITIVES, groups 14 and 21). A missing_facts note that says a timetable overlap
'is unknown' / 'are unknown' AFTER the asserting words was rejected because the negation window ended at the assertion. Narrow fix:
a negation / unknown marker that closes the SAME clause (after the assertion, before the clause ends) also counts. Adversarial mixed
clauses still reject: 'unknown school BUT timetables align' splits at 'but'; 'no doubt' / 'not only' idioms stay non-negations."""
import pytest

from server import facts
from tests.test_a3_timetable_inference import _cat, STUDY

MISSING = {"U2.school": {"status": "missing", "ref": "U2", "field": "school", "scope": "answer", "value": None},
           "U2.course": {"status": "missing", "ref": "U2", "field": "course", "scope": "answer", "value": None},
           "U2.level": {"status": "missing", "ref": "U2", "field": "level", "scope": "answer", "value": None}}


def _cat2():
    cat = _cat()
    cat.update(MISSING)
    return cat


@pytest.mark.parametrize("note,rejected", [
    # the two live captures, verbatim
    ("U2 has no recorded school, course or level, so study setting and any timetable overlap with others are unknown.", False),
    ("U2's school, course and level are unrecorded, so study or timetable overlap with U1 is unknown.", False),
    # trailing negation variants
    ("Whether their class times overlap is unknown.", False),
    ("Any timetable overlap with U1 is not recorded.", False),
    # adversarial: the unknown lives in another clause; the asserting clause stands alone
    ("U2's school is unknown but their timetables align with U1's.", True),
    ("U2's school is unrecorded, so their class times will overlap anyway.", True),
    ("No doubt their timetables line up.", True),
    ("Not only do their class times match, they are at the same campus.", True),
    ("Their timetables align, which is unknown to staff.", True),                      # 'unknown' beyond the clause boundary (', which')
    ("Their class times line up. Their diet is unknown.", True),                        # negation in a different sentence
    # root counterexamples (9 Nov 2026): an unknown about ANOTHER subject in the trailing words never negates the alignment claim
    ("Their timetables align despite an unknown school.", True),
    ("Their timetables align, with the campus unknown.", True),
    ("Their timetables overlap regardless of unrecorded course details.", True),
    ("Their timetables align even though the level is not recorded.", True),
    ("Their class times overlap; the school is unknown.", True),
    # root round-3 counterexamples: finite verb, positive certainty, plain 'not <word>' never negate
    ("Timetables align when school is unknown.", True),
    ("Their timetable overlap is undeniable.", True),
    ("Their timetable overlap is not disputed.", True),
    ("Their timetable overlap is unquestionable.", True),
    ("Their timetable overlap is unlikely to matter.", True),
    # the uncertainty predicate IS about the nominal overlap/alignment: honest gap
    ("Timetable overlap with U2 is unknown.", False),                                 # ("between U1 and U2" would be cut at "and" by the clause splitter and stay rejected: conservative)
    ("Whether class times align remains unclear.", False),
    ("Any timetable alignment is yet to be confirmed.", False),
])
def test_trailing_unknown_closes_the_clause(note, rejected):
    prob = facts.wording_problems(note, ["U2.school", "U2.course", "U2.level"], _cat2(), ref="U2")
    assert bool(prob) is rejected, prob


def test_original_table_unchanged():
    """The earlier positive and negative controls keep their verdicts."""
    from tests.test_a3_timetable_inference import test_study_facts_never_license_timetable_claims as t
    for fids, note, rejected in t.pytestmark[0].args[1]:
        t(fids, note, rejected)


# r4 (9 Nov 2026): the live group-21 capture with a COORDINATED ref suffix, which the clause splitter cut before the copula.
@pytest.mark.parametrize("note,rejected", [
    ("U2's school, course and level are not recorded, so any study or timetable overlap with U1 or U3 is unknown.", False),   # live capture, verbatim
    ("Timetable overlap between U1 and U2 is unknown.", False),
    ("Any timetable overlap with U1, U3 or U4 remains unclear.", False),
    # the bound span is masked, the REST of the sentence is still judged on its own
    ("Any timetable overlap with U1 or U3 is unknown but their class times align.", True),
    ("Any timetable overlap with U1 or U3 is unknown, so their timetables probably line up anyway.", True),
    ("Timetable overlap with U1 or U3 is unknown; their lecture times match.", True),
    # not fully bound: no copula / no closed predicate / finite verb / too long a suffix -> no masking, r3 verdicts stand
    ("Timetable overlap with U1 or U3 is undeniable.", True),
    ("Timetables align with U1 or U3 when school is unknown.", True),
    ("Timetable overlap with U1 or U3 or U4 or U5 and everyone else is unknown but class times match.", True),
])
def test_r4_coordinated_ref_suffix_is_masked_as_one_bound_span(note, rejected):
    prob = facts.wording_problems(note, ["U2.school", "U2.course", "U2.level"], _cat2(), ref="U2")
    assert bool(prob) is rejected, prob
