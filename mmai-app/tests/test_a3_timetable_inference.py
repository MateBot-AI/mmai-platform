"""9 Nov 2026 (root live-replay finding): course / school / campus labels alone do not establish timetable alignment. A brief note may
not assert that timetables or class schedules line up unless the recorded facts say so (a supplied timing answer quoted as recorded, for
its own person), and the introduction may not carry such a claim; legitimate "same course / same campus" statements, honest negatives,
questions and invitations to compare timetables are preserved. Narrow evidence boundary, no new rule."""
import pytest

from server import ai, facts
from tests.test_r7_intro_wording import _basis, _people, _prose

STUDY = ["U1.course", "U2.course", "U1.school", "U2.school"]


def _cat():
    cat = {}
    for ref in ("U1", "U2"):
        for field, val in (("course", "Engineering"), ("school", "RMIT"), ("level", "Undergraduate"), ("doing", "Studying"), ("routine", "Early riser")):
            cat[f"{ref}.{field}"] = {"status": "recorded", "ref": ref, "field": field, "scope": "answer", "value": val}
    cat["A1.campus_km"] = {"status": "recorded", "ref": None, "field": "campus_km", "scope": "apartment", "value": "1.2"}
    return cat


@pytest.mark.parametrize("fids,note,rejected", [
    (STUDY, "U1 and U2 are both studying Engineering at the same RMIT campus, so their study timetables are likely to line up.", True),
    (STUDY, "Both are at RMIT, so their class times will probably overlap.", True),
    (STUDY, "U1 and U2 are on campus at the same time most days.", True),
    (STUDY, "Their schedules should match since both study at RMIT.", True),                       # 'schedules' with study facts only
    (STUDY, "U1 and U2 are both studying Engineering at the same RMIT campus.", False),               # what is recorded
    (STUDY, "Both are undergraduates at RMIT, which gives them common ground.", False),
    (STUDY, "Worth comparing timetables once introduced, since both study at RMIT.", False),           # invitation
    (STUDY, "They could discuss whether their class times line up.", False),
    (["U1.routine", "U2.routine"], "Both are early risers, so their morning schedules match.", False),  # routine fact cited: a sleep-pattern claim
    (STUDY + ["U1.routine", "U2.routine"], "Both are early risers at RMIT, so their lecture timetables line up.", True),  # routine never licenses timetables
    (["A1.campus_km"], "The apartment is 1.2 km from campus, so attendance should be easy to keep up.", True),
    (["A1.campus_km"], "The apartment is 1.2 km from the RMIT campus.", False),
    (["U1.routine"], "U1 keeps regular hours and a steady schedule.", False),                        # no study fact cited: not judged
    (["U1.routine", "U2.routine"], "Both are early risers, so their class times will line up.", True),  # routine-only citation never licenses a timetable claim
    (STUDY, "Discuss cooking. Their class times will match.", True),                                   # an invitation elsewhere does not launder another sentence
    (STUDY, "Their study timetables line up, and they can compare cooking preferences.", True),
    (STUDY, "Shared campus does not establish that their class times overlap.", False),                # honest negative
    (STUDY, "Neither has reported class times.", False),
    (STUDY, "Timetables could be compared once they have spoken.", False),                             # passive invitation
    (STUDY, "Their class times match and they have not discussed cooking.", True),                    # invitation / negation elsewhere in the sentence
    (STUDY, "Do their class times match?", False),                                                     # a question asks, it does not assert
])
def test_study_facts_never_license_timetable_claims(fids, note, rejected):
    prob = facts.wording_problems(note, fids, _cat(), ref=None)
    assert bool(prob) is rejected, prob
    if rejected:
        assert "timetable" in prob and "labels alone do not establish timetable alignment" in prob


def test_recorded_wording_is_not_an_inference():
    """A course literally called 'Timetable Design', or a freely typed course answer carrying class times, may be quoted as recorded."""
    cat = _cat()
    cat["U1.course"]["value"] = "Timetable Design"
    assert facts.wording_problems("U1 studies Timetable Design and would suit a planning-minded household.", ["U1.course"], cat) is None
    value = "Engineering; class times: Monday at 9 am"
    cat["U1.course"]["value"] = value
    assert facts.wording_problems(f'U1 recorded the course answer "{value}".', ["U1.course", "U2.course"], cat) is None   # whole value quoted
    assert facts.wording_problems(f'U1 recorded "{value}", so their class times will match.', ["U1.course", "U2.course"], cat)   # quote + separate claim
    assert facts.wording_problems("U1's class times will suit U2.", ["U1.course", "U2.course"], cat)   # the noun phrase alone is not exempt
    assert facts.wording_problems("Not only do both study Engineering, their class times will line up.", ["U1.course", "U2.course"], cat)   # idiom is no negation
    assert facts.wording_problems("U2 has no car, so their class times will line up.", ["U1.course", "U2.course"], cat)   # negation about something else
    assert facts.wording_problems("It is unclear whether their class times overlap.", ["U1.course", "U2.course"], cat) is None


def test_check_items_rejects_the_live_seven_person_wording():
    cat = _cat()
    items = [{"fact_ids": STUDY, "note": "U1 and U2 are both studying Engineering at the same RMIT campus, so their study timetables are likely to line up."},
             {"fact_ids": STUDY, "note": "U1 and U2 are both studying Engineering at the same RMIT campus."}]
    clean, problems = facts.check_items(items, cat, ref=None, allow_missing=False, require_own=False, label="group.what_could_work")
    assert len(clean) == 1 and "timetable" in " ".join(problems)
    assert clean[0]["note"].endswith("same RMIT campus.")


def test_introduction_may_not_assert_timetable_alignment_even_with_routines_recorded():
    basis = _basis(("routine",))
    bad = _prose(group_fit="You are all studying Engineering at RMIT, so your study timetables are likely to line up.")
    probs = ai.habit_claim_problems(bad, _people(), basis)
    assert probs and "timetable" in probs[0]
    ok = _prose(group_fit="You are all studying Engineering at the same RMIT campus.")
    assert ai.habit_claim_problems(ok, _people(), basis) == []
    invite = _prose(group_fit="You are all at RMIT.", to_discuss="Compare timetables and see whether your class times line up.")
    assert ai.habit_claim_problems(invite, _people(), basis) == []
    topic = _prose(to_discuss="Timetables; cooking; guests.")
    assert ai.habit_claim_problems(topic, _people(), basis) == []
    people = _people()
    people[0]["course"] = "Timetable Design"
    named = _prose(group_fit=f"{people[0]['first_name']} studies Timetable Design.")
    assert ai.habit_claim_problems(named, people, basis) == []                              # the course's own name
    people[0]["intro"] = "My class times suit anyone."
    bio = _prose(group_fit="Their class times will suit everyone.")
    assert ai.habit_claim_problems(bio, people, basis)                                      # one person's bio licenses nobody's alignment


def test_brief_prompt_and_contract_carry_the_boundary():
    import inspect
    src = inspect.getsource(ai)
    assert "labels alone do not" in src and "never infer that timetables, class times or attendance line up" in src
    assert "quote supplied timing only as recorded" in src and "without generalising to others" in src
    assert ai.REVIEW_CONTRACT == "2026-11-11.step-free-payload"   # Access value delivery; timetable boundaries remain.
