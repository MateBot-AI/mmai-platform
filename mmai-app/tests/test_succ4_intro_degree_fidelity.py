"""succ4 (10 Nov 2026): collective habit DEGREE fidelity in the customer introduction.

Defect (live capture REQ140, cycle1-post group 3): group_fit said the three people "each described themselves as tidy and social"
while every recorded tidiness answer was 'Relaxed'. The coverage check passed (tidy covered by everyone's own answer) and the draft
was refused only because its to_discuss named people - the degree misstatement itself escaped. Repair: habit_claim_problems gets a
PRIVATE validation context `habit_answers` (intro ref -> recorded lifestyle values, read in ai_queue.intro_capture from the group's
captured evidence_json) and an asserted, covered TIDINESS / SOCIAL-STYLE habit must state a degree those answers can stand for.
Pinned here: the exact REQ140 negative, the exact REQ141 / REQ142 positives, the REQ66 open-discussion positive, mixed degrees
(field- and clause-scoped, never sentence-wide), named vs collective scope, no quotation exemption in group prose, fail-closed on a
missing value, 'clean' / 'quiet hours' with no inferred degree, the unchanged drafter contract and the prose-contract bump.
No prompt, bio, INTRO_BIO_KEYS, INTRO_LIFESTYLE, matching, scoring or notes-extraction change."""
import json
import pytest

from server import ai, ai_queue
from tests.test_triage_fix3_invitation_topics import BASIS as REQ66_BASIS, MEMBERS as REQ66_MEMBERS, PROSE as REQ66_PROSE, REQ66_TO_DISCUSS, _cov

# ---- group 3 as captured (Yuki I1 / Leila I2 / Tariq I3; all tidy=Relaxed, social=Very social; routines early / night / varies) ----
G3_MEMBERS = [{"ref": "I1", "first_name": "Yuki", "name": "Yuki", "enquiry_id": 51, "is_lead": True},
              {"ref": "I2", "first_name": "Leila", "name": "Leila", "enquiry_id": 160, "is_lead": True},
              {"ref": "I3", "first_name": "Tariq", "name": "Tariq", "enquiry_id": 161, "is_lead": True}]
_ALL = ("tidy", "social", "routine", "guests")
G3_BASIS = [{"section": "compatibility", "text": "All three record relaxed tidiness and a very social style.",
             "subjects": [{"packet_ref": "U1", "ref": "I1"}, {"packet_ref": "U2", "ref": "I2"}, {"packet_ref": "U3", "ref": "I3"}],
             "coverage": {"U1": _cov(*_ALL), "U2": _cov(*_ALL), "U3": _cov(*_ALL)}},
            {"section": "compatibility", "text": "Leila and Tariq both put a shared room first.",
             "subjects": [{"packet_ref": "U2", "ref": "I2"}, {"packet_ref": "U3", "ref": "I3"}],
             "coverage": {"U2": _cov("room_first"), "U3": _cov("room_first")}}]
G3_ANSWERS = {"I1": {"tidy": "Relaxed", "social": "Very social", "routine": "Early riser", "guests": "Sometimes"},
              "I2": {"tidy": "Relaxed", "social": "Very social", "routine": "Night owl", "guests": "Often"},
              "I3": {"tidy": "Relaxed", "social": "Very social", "routine": "Varies", "guests": "Often"}}
OPENING = "Hello everyone, we are pleased to introduce you as a proposed household for the apartment below."
CLOSING = "Please say hello to each other and reply to us at ALM with any questions."
BIOS3 = {"I1": "Yuki was born in Japan.", "I2": "Leila was born in Brazil.", "I3": "Tariq was born in Singapore."}

REQ140_GROUP_FIT = ("Yuki, Leila and Tariq each described themselves as tidy and social, so the household shares a similar outlook on those two "
                    "points. Leila and Tariq both named sharing a room with one other person as their first choice, which fits the two-bed room "
                    "in this apartment's layout.")
REQ142_GROUP_FIT = ("Yuki, Leila and Tariq each described themselves as relaxed about tidiness and very social, so you share a similar energy at "
                    "home. Leila and Tariq both named sharing a bedroom with one other person as their first choice, which matches the room "
                    "arrangement proposed here.")
REQ142_TO_DISCUSS = ("Your sleep routines differ, with an early riser, a night owl and a varied schedule in the mix, so it's worth talking through "
                     "quiet hours together. It would also help to agree how you'd like to handle guests, since hosting habits vary across the group, "
                     "and to talk about the shared bedroom's expectations as well as tidiness and chores.")

# ---- group 4 as captured (Marco I1 / Aiko I2; both tidy=Relaxed, social=Friendly but independent) -----------------------------
G4_MEMBERS = [{"ref": "I1", "first_name": "Marco", "name": "Marco", "enquiry_id": 71, "is_lead": True},
              {"ref": "I2", "first_name": "Aiko", "name": "Aiko", "enquiry_id": 150, "is_lead": True}]
G4_BASIS = [{"section": "compatibility", "text": "Both record relaxed tidiness and a friendly but independent style.",
             "subjects": [{"packet_ref": "U1", "ref": "I1"}, {"packet_ref": "U2", "ref": "I2"}],
             "coverage": {"U1": _cov(*_ALL), "U2": _cov(*_ALL)}}]
G4_ANSWERS = {"I1": {"tidy": "Relaxed", "social": "Friendly but independent", "routine": "Varies", "guests": "Sometimes"},
              "I2": {"tidy": "Relaxed", "social": "Friendly but independent", "routine": "Early riser", "guests": "Rarely"}}
REQ141_GROUP_FIT = ("Marco and Aiko both described themselves as relaxed about tidiness and friendly but independent, which points to similar "
                    "expectations for shared living. Since you each have your own king bedroom, there is a sensible base for a low-conflict household.")
REQ141_TO_DISCUSS = ("It would help to talk early about kitchen habits, how often you each like to have guests, overnight visitors and quiet hours. "
                     "Everyone's day-to-day rhythm is a little different, so agreeing on routines and noise expectations together should help the "
                     "household run smoothly.")

# ---- group 32 shape (two 'Very tidy', one 'Relaxed'): everyone covered, degrees differ ------------------------------------------
G32_MEMBERS = [{"ref": "I1", "first_name": "Chiara", "name": "Chiara", "enquiry_id": 72, "is_lead": True},
               {"ref": "I2", "first_name": "Bruno", "name": "Bruno", "enquiry_id": 83, "is_lead": True},
               {"ref": "I3", "first_name": "Amara", "name": "Amara", "enquiry_id": 152, "is_lead": True}]
G32_BASIS = [{"section": "compatibility", "text": "Tidiness answers are mixed: two very tidy, one relaxed.",
              "subjects": [{"packet_ref": "U1", "ref": "I1"}, {"packet_ref": "U2", "ref": "I2"}, {"packet_ref": "U3", "ref": "I3"}],
              "coverage": {"U1": _cov(*_ALL), "U2": _cov(*_ALL), "U3": _cov(*_ALL)}},
             {"section": "compatibility", "text": "Chiara and Bruno both record very tidy.",
              "subjects": [{"packet_ref": "U1", "ref": "I1"}, {"packet_ref": "U2", "ref": "I2"}],
              "coverage": {"U1": _cov("tidy"), "U2": _cov("tidy")}}]
G32_ANSWERS = {"I1": {"tidy": "Very tidy", "social": "Quiet and private", "routine": "Early riser", "guests": "Rarely"},
               "I2": {"tidy": "Very tidy", "social": "Friendly but independent", "routine": "Early riser", "guests": "Rarely"},
               "I3": {"tidy": "Relaxed", "social": "Very social", "routine": "Night owl", "guests": "Often"}}
BIOS32 = {"I1": "Chiara was born in Italy.", "I2": "Bruno was born in Portugal.", "I3": "Amara was born in Nigeria."}


def _prose(bios, group_fit, to_discuss):
    return {"opening": OPENING, "bios": bios, "group_fit": group_fit, "to_discuss": to_discuss, "closing": CLOSING}


def _check(prose, members, basis, answers):
    return ai.validate_intro_prose(prose, members) + ai.validate_group_prose(prose, members, basis, answers)


# ---- the defect, pinned --------------------------------------------------------------------------------------------------------
def test_req140_degree_misstatement_escaped_without_habit_answers():
    """documents the gap: with the naming problem removed from to_discuss, the frozen checks accept 'tidy' for three 'Relaxed' answers"""
    prose = _prose(BIOS3, REQ140_GROUP_FIT, REQ142_TO_DISCUSS)
    assert ai.validate_intro_prose(prose, G3_MEMBERS) + ai.validate_group_prose(prose, G3_MEMBERS, G3_BASIS) == []


def test_req140_exact_group_fit_now_refused_on_tidiness_degree():
    prose = _prose(BIOS3, REQ140_GROUP_FIT, REQ142_TO_DISCUSS)
    problems = _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert len(problems) == 1 and problems[0].startswith("group_fit states a tidiness degree ('tidy')")
    assert "I1, I2, I3" in problems[0]
    for v in ("Relaxed", "Very social", "Early riser", "Night owl"):   # recorded values never appear in the problem text
        assert v not in problems[0]


def test_req140_with_its_original_naming_to_discuss_reports_both_problems():
    prose = _prose(BIOS3, REQ140_GROUP_FIT, "Yuki, Leila and Tariq's routines differ (early starts, late nights and varying hours), so agree quiet hours together.")
    problems = _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert "to_discuss singles out Yuki" in problems
    assert any(p.startswith("group_fit states a tidiness degree") for p in problems)


# ---- the correct live drafts stay accepted ----------------------------------------------------------------------------------------
def test_req142_exact_prose_accepted():
    assert _check(_prose(BIOS3, REQ142_GROUP_FIT, REQ142_TO_DISCUSS), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


def test_req141_exact_prose_accepted():
    bios = {"I1": "Marco was born in Italy.", "I2": "Aiko was born in Japan."}
    assert _check(_prose(bios, REQ141_GROUP_FIT, REQ141_TO_DISCUSS), G4_MEMBERS, G4_BASIS, G4_ANSWERS) == []


def test_req66_open_discussion_accepted_with_answers():
    """the REQ66 invitation (topics only) stays accepted whatever the recorded degrees are"""
    answers = {"I1": {"routine": "Early riser", "guests": "Rarely"}, "I2": {"routine": "Night owl", "guests": "Often"}, "I3": {"routine": "Varies", "guests": "Sometimes"}}
    assert ai.validate_group_prose(REQ66_PROSE, REQ66_MEMBERS, REQ66_BASIS, answers) == []
    assert ai.validate_group_prose(dict(REQ66_PROSE, to_discuss=REQ66_TO_DISCUSS), REQ66_MEMBERS, REQ66_BASIS, answers) == []


def test_no_context_keeps_frozen_behaviour_and_an_active_empty_context_fails_closed():
    """habit_answers None = exactly the pre-succ4 checks (fixtures without values keep passing); a dict - even empty - means the
    route's validation context is active, and a stated degree with nothing to check against is refused, never silently passed"""
    prose = _prose(BIOS3, "Everyone described themselves as very tidy.", REQ142_TO_DISCUSS)
    assert ai.validate_group_prose(prose, G3_MEMBERS, G3_BASIS) == []
    closed = ai.validate_group_prose(prose, G3_MEMBERS, G3_BASIS, {})
    assert len(closed) == 1 and "cannot be checked against a recorded answer for I1, I2, I3" in closed[0]
    assert _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS) != []


# ---- mixed degrees: all covered, different values ------------------------------------------------------------------------------
@pytest.mark.parametrize("group_fit", [
    "Everyone in the household is very tidy, which should make sharing the kitchen easy.",
    "Everyone described themselves as relaxed about tidiness, so chores should not be a sore point.",
    "The household keeps things tidy.",
    "Chiara, Bruno and Amara are all spotless at home.",
    "Chiara and Amara are both very tidy.",      # named pair: Amara's recorded answer is not 'Very tidy'
])
def test_mixed_tidiness_degrees_refuse_a_single_shared_degree(group_fit):
    problems = _check(_prose(BIOS32, group_fit, "Agree chores and quiet hours together early on."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)
    assert len(problems) == 1 and "tidiness degree" in problems[0], problems


def test_named_pair_with_matching_degree_accepted_when_basis_note_covers_them():
    prose = _prose(BIOS32, "Chiara and Bruno both described themselves as very tidy.", "Agree chores and quiet hours together early on.")
    assert _check(prose, G32_MEMBERS, G32_BASIS, G32_ANSWERS) == []


@pytest.mark.parametrize("group_fit", [
    "Tidiness answers differ across the group, with two very tidy people and one relaxed about tidiness, which is worth knowing up front.",
    "The household's tidiness answers are a mix: two of you are very tidy and one is relaxed about tidiness.",
    "Chiara and Bruno are both very tidy, whereas Amara is relaxed about tidiness, so it is worth agreeing standards early.",
    "Social styles differ across the group, from quiet and private to very social, which is useful to know going in.",
])
def test_genuine_mixed_level_descriptions_are_preserved(group_fit):
    assert _check(_prose(BIOS32, group_fit, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS) == [], group_fit


def test_mixedness_is_field_and_clause_scoped_not_sentence_wide():
    """root control: a difference stated about ONE field never licenses a wrong shared degree of ANOTHER field in the same sentence"""
    gf = "Everyone is very tidy, although your routines differ."
    problems = _check(_prose(BIOS3, gf, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert len(problems) == 1 and "tidiness degree ('very tidy')" in problems[0], problems
    gf_same_clause = "Everyone is very tidy despite different routines."   # the difference word sits in the SAME clause as the plain degree
    problems = _check(_prose(BIOS3, gf_same_clause, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert len(problems) == 1 and "tidiness degree ('very tidy')" in problems[0], problems
    gf2 = "Everyone is very tidy, and Amara is relaxed about tidiness."   # two degrees, but no difference stated about tidiness: the shared claim is checked
    assert any("tidiness degree ('very tidy')" in p for p in _check(_prose(BIOS32, gf2, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))
    gf3 = "Tidiness differs across the group, and everyone is very tidy and very social."   # a difference clause does not launder the next clause
    assert any("tidiness degree ('very tidy')" in p for p in _check(_prose(BIOS32, gf3, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))


def test_social_style_degrees_checked_with_option_wording_only():
    bad_social = _prose(BIOS32, "Everyone is very social, so the common areas will be lively.", "Agree chores together.")
    assert any("social style degree" in p for p in _check(bad_social, G32_MEMBERS, G32_BASIS, G32_ANSWERS))
    bad_quiet = _prose(BIOS3, "Everyone is quiet and private at home.", "Agree chores together.")
    assert any("social style degree" in p for p in _check(bad_quiet, G3_MEMBERS, G3_BASIS, G3_ANSWERS))
    bad_indep = _prose(BIOS3, "Yuki, Leila and Tariq each described themselves as friendly but independent.", "Agree chores together.")
    assert any("social style degree" in p for p in _check(bad_indep, G3_MEMBERS, G3_BASIS, G3_ANSWERS))
    ok_social = _prose(BIOS3, "Everyone described themselves as very social.", "Agree chores together.")
    assert _check(ok_social, G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


def test_no_personality_inference_from_outgoing_introvert_private_independent_or_quiet():
    """root ruling: only the social options' own wording is a degree; traits and room words are not"""
    for word in ("outgoing", "extroverted", "introverted", "reserved", "independent", "private", "quiet"):
        assert "social" not in ai.habit_degree_terms(f"everyone is {word}"), word
    # "private" / "quiet" in context stay what they are: statements about rooms or hours, never the 'Quiet and private' category
    for gf in ("Everyone has a private bedroom, so there is space to retreat to.",
               "Since you each have your own private room, there is a sensible base for a low-conflict household.",
               "Everyone is quiet at night and keen to keep weeknights calm."):
        assert _check(_prose(BIOS3, gf, "Agree quiet hours together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == [], gf


@pytest.mark.parametrize("group_fit,members,basis,answers", [
    ("Tidiness answers differ across the group, with two very tidy people and one relaxed about tidiness.", G3_MEMBERS, G3_BASIS, G3_ANSWERS),   # nobody is very tidy
    ("The household's tidiness is a mix: some of you are very tidy and the rest are relaxed about tidiness.", G3_MEMBERS, G3_BASIS, G3_ANSWERS),
    ("Social styles differ across the group, with two of you very social and one friendly but independent.", G32_MEMBERS, G32_BASIS, G32_ANSWERS),   # only one is very social
    ("Social styles differ across the group, from quiet and private to friendly but independent.", G3_MEMBERS, G3_BASIS, G3_ANSWERS),   # neither exists: all very social
    ("Tidiness differs across the group, with one relaxed about tidiness and everyone very social.", G32_MEMBERS, G32_BASIS, G32_ANSWERS),   # Relaxed exists, but 'everyone very social' is false
])
def test_false_mix_or_partitive_descriptions_are_refused(group_fit, members, basis, answers):
    problems = _check(_prose(BIOS3 if members is G3_MEMBERS else BIOS32, group_fit, "Agree chores together."), members, basis, answers)
    assert len(problems) == 1 and "degree" in problems[0], (group_fit, problems)


def test_universal_assertion_is_not_laundered_by_a_second_degree_or_a_difference_word():
    """root control (ckpt2 probe): an explicit universal assertion compares every scoped person even when another degree is mentioned"""
    gf = "Everyone is very tidy rather than relaxed about tidiness, despite different tidiness answers."
    problems = _check(_prose(BIOS32, gf, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)
    assert len(problems) == 1 and "tidiness degree" in problems[0], problems
    # the named 'whereas' control and its inverted false variant are unchanged
    assert _check(_prose(BIOS32, "Chiara and Bruno are both very tidy, whereas Amara is relaxed about tidiness.", "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS) == []
    assert any("tidiness degree" in p for p in _check(_prose(BIOS32, "Chiara and Amara are both very tidy, whereas Bruno is relaxed about tidiness.", "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))


def test_only_an_explicit_range_construction_reads_as_existence_claims():
    ok = "Social styles differ across the group, from quiet and private to very social, which is useful to know going in."   # genuine range, G32
    assert _check(_prose(BIOS32, ok, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS) == []
    ok2 = "Tidiness in the household ranges from very tidy to relaxed about tidiness."
    assert _check(_prose(BIOS32, ok2, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS) == []
    bad = "Social styles differ across the group, from quiet and private to very social."   # G3: everyone is very social, nobody quiet and private
    problems = _check(_prose(BIOS3, bad, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert len(problems) == 1 and "in a range that no recorded answer" in problems[0], problems
    plain_before_from = "Everyone is very tidy from day one to the end of the lease."   # 'very tidy' precedes the construction: a plain claim
    assert any("tidiness degree ('very tidy')" in p for p in _check(_prose(BIOS32, plain_before_from, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))
    # ckpt4 (root probe): a temporal 'from ... to ...' BEFORE the degree is not a range of degrees either - the existence reading is bound
    # to degree spans that are the construction's own endpoints, not to every degree word after a 'from'
    plain_after_from = "From day one to the end of the lease everyone is very tidy."
    assert any("tidiness degree ('very tidy')" in p for p in _check(_prose(BIOS32, plain_after_from, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))
    assert ai.habit_degree_terms("From day one to the end of the lease everyone is very tidy") == {"tidy": [("very tidy", frozenset({"Very tidy"}), "plain", 0)]}
    assert ai.habit_degree_terms("Everyone is very tidy from day one to the end of the lease") == {"tidy": [("very tidy", frozenset({"Very tidy"}), "plain", 0)]}
    assert ai.habit_degree_terms("Tidiness in the household ranges from very tidy to relaxed about tidiness") == {
        "tidy": [("very tidy", frozenset({"Very tidy"}), "range", 1), ("relaxed about tidiness", frozenset({"Relaxed"}), "range", 1)]}
    assert ai.habit_degree_terms("a spread of quiet & private, friendly & independent or very social") == {   # mix construction: the listed endpoints only
        "social": [("very social", frozenset({"Very social"}), "range", 1), ("friendly & independent", frozenset({"Friendly but independent"}), "range", 1),
                   ("quiet & private", frozenset({"Quiet and private"}), "range", 1)]}
    assert ai.habit_degree_terms("a mix of people from Perth to Hobart who are all very tidy") == {"tidy": [("very tidy", frozenset({"Very tidy"}), "plain", 0)]}


def test_open_degree_question_states_no_degree_and_keeps_the_frozen_coverage_gate():
    """ckpt4 (reviewer D3): 'Worth chatting about how tidy you each like the home to be.' asks the degree rather than stating one ('chatting'
    is not in the frozen invitation lexicon, so this clause is checked as an assertion about everyone). 6145 accepted it on coverage alone;
    the degree check must not turn the question into a claim of 'tidy'. The frozen own-coverage gate is untouched: without I3's own
    tidiness coverage the sentence is refused exactly as on 6145, with or without answers."""
    q = "Worth chatting about how tidy you each like the home to be."
    for part in ("group_fit", "to_discuss"):
        p = _prose(BIOS32, q if part == "group_fit" else "Agree chores together.", q if part == "to_discuss" else "Agree chores together.")
        assert _check(p, G32_MEMBERS, G32_BASIS, G32_ANSWERS) == [], part
        assert ai.validate_group_prose(p, G32_MEMBERS, G32_BASIS) == [], part   # frozen checks only: same acceptance
    assert ai.habit_degree_terms("Worth chatting about how tidy you each like the home to be") == {}
    assert ai.habit_degree_terms("how social you each like the home to be") == {}
    thin_basis = [G32_BASIS[1]]   # only Chiara and Bruno's own tidiness coverage: refused on coverage, as on 6145, answers or not
    p = _prose(BIOS32, q, "Agree chores together.")
    assert any("not covered by every person's own recorded answer" in x for x in ai.validate_group_prose(p, G32_MEMBERS, thin_basis))
    assert any("not covered by every person's own recorded answer" in x for x in ai.validate_group_prose(p, G32_MEMBERS, thin_basis, G32_ANSWERS))
    # presupposed degrees inside a 'chatting' sentence are still degree statements and still checked (G32: I3 is Relaxed)
    for bad in ("Worth chatting about how very tidy everyone keeps the home.",
                "Worth chatting about how tidy you each like the home to be, since everyone is very tidy anyway.",
                "Worth chatting about how tidy you each like the home to be, given everyone is spotless."):
        problems = _check(_prose(BIOS32, bad, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)
        assert any("tidiness degree" in x and "I3" in x for x in problems), (bad, problems)
    assert ai.habit_degree_terms("how very tidy everyone keeps the home") == {"tidy": [("very tidy", frozenset({"Very tidy"}), "plain", 0)]}
    # ckpt5 (root review): the existing presupposed 'already / always' shape keeps its degree - no blanket 'how' exemption
    already = "Worth chatting about how tidy everyone already is."
    assert ai.habit_degree_terms("Worth chatting about how tidy everyone already is") == {"tidy": [("tidy", frozenset({"Very tidy", "Average"}), "plain", 0)]}
    for part in ("group_fit", "to_discuss"):
        false_g3 = _prose(BIOS3, already if part == "group_fit" else "Agree chores together.", already if part == "to_discuss" else "Agree chores together.")
        problems = _check(false_g3, G3_MEMBERS, G3_BASIS, G3_ANSWERS)   # G3: everyone Relaxed
        assert any(f"{part} states a tidiness degree ('tidy')" in x and "I1, I2, I3" in x for x in problems), (part, problems)
        all_tidy = {r: dict(G3_ANSWERS[r], tidy="Very tidy") for r in G3_ANSWERS}
        assert _check(false_g3, G3_MEMBERS, G3_BASIS, all_tidy) == [], part   # true counterpart
    assert any("tidiness degree ('tidy')" in x for x in _check(_prose(BIOS32, "Worth chatting about how tidy you each always are.", "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS))
    # (the REQ66 open degree question 'how social you each like the home to be' is pinned above in test_req66_open_discussion_accepted_with_answers)


def test_contradictory_degrees_for_the_same_people_without_a_stated_difference_are_refused():
    problems = _check(_prose(BIOS32, "Everyone is very tidy or relaxed about tidiness.", "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)
    assert len(problems) == 1 and "more than one tidiness degree" in problems[0], problems
    problems = _check(_prose(BIOS32, "Everyone is very tidy and relaxed about tidiness.", "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)
    assert len(problems) == 1 and "tidiness degree ('very tidy')" in problems[0], problems   # split at 'and': the first plain degree already fails for I3


def test_negated_degree_is_read_as_a_denial_and_checked_as_one():
    ok = "Nobody in the household described themselves as very tidy, so a relaxed standard is the likely starting point."   # true for group 3
    assert _check(_prose(BIOS3, ok, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []
    ok2 = "Everyone is relaxed about tidiness and nobody is tidy in the strict sense, while everyone is very social."
    assert _check(_prose(BIOS3, ok2, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []
    bad = _check(_prose(BIOS32, ok, "Agree chores together."), G32_MEMBERS, G32_BASIS, G32_ANSWERS)   # two of group 32 recorded 'Very tidy'
    assert len(bad) == 1 and "denies a tidiness degree ('very tidy')" in bad[0] and "I1, I2" in bad[0], bad


def test_additive_not_only_is_an_affirmation_not_a_denial():
    """root control (ckpt2 probe): 'not only very tidy but also very social' asserts both degrees"""
    gf = "Everyone is not only very tidy but also very social."
    problems = _check(_prose(BIOS3, gf, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)   # group 3: all Relaxed -> refused on 'very tidy'
    assert len(problems) == 1 and "tidiness degree ('very tidy')" in problems[0] and "denies" not in problems[0], problems
    tidy_social = {"I1": {"tidy": "Very tidy", "social": "Very social"}, "I2": {"tidy": "Very tidy", "social": "Very social"}, "I3": {"tidy": "Very tidy", "social": "Very social"}}
    assert _check(_prose(BIOS3, gf, "Agree chores together."), G3_MEMBERS, G3_BASIS, tidy_social) == []   # matching answers: accepted
    assert ai.habit_degree_terms("not only very tidy but also very social")["tidy"][0][2] == "plain"
    assert ai.habit_degree_terms("not very tidy")["tidy"][0][2] == "negated"


def test_lexicon_is_tidiness_and_social_style_only():
    """routine / guest wording is not degree-checked in this assignment (no inferred equivalences such as 'early to bed' or 'shift work')"""
    assert set(ai.HABIT_DEGREES) == {"tidy", "social"}
    for gf in ("You are all early risers.", "Everyone rarely has guests over.", "Everyone is a night owl who hosts often."):
        problems = _check(_prose(BIOS3, gf, "Agree quiet hours together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)
        assert not any("degree" in p for p in problems), (gf, problems)


# ---- what must NOT be inferred ------------------------------------------------------------------------------------------------------
def test_bare_clean_states_no_degree():
    prose = _prose(BIOS3, "Everyone keeps shared spaces clean and takes cleaning seriously.", "Agree chores together.")
    assert _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


def test_tidiness_topic_words_state_no_degree():
    prose = _prose(BIOS3, "Everyone answered the tidiness and cleaning questions, so expectations are on the table.", "Agree chores together.")
    assert _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


def test_quiet_hours_and_friendly_are_not_social_degrees():
    prose = _prose(BIOS3, "Everyone is friendly and keen on sensible quiet hours.", "Agree quiet hours together.")
    assert _check(prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


@pytest.mark.parametrize("group_fit", [
    'Everyone described themselves as "very tidy", which should make sharing the kitchen easy.',
    "Yuki, Leila and Tariq each said they are \u201cvery tidy\u201d at home.",
])
def test_quoted_group_assertions_get_no_exemption(group_fit):
    """root control: quotation marks in group prose can be invented; the claim is checked like any other wording"""
    problems = _check(_prose(BIOS3, group_fit, "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS)
    assert any("tidiness degree ('very tidy')" in p for p in problems), problems


def test_individual_bio_quotations_are_preserved_untouched():
    """a person's own words belong in that person's bio; the group checks never read bios, so a verbatim quote is kept as supplied"""
    bios = dict(BIOS3, I1="Yuki was born in Japan and wrote: \"I'm very tidy and love a quiet night in.\"")
    assert _check(_prose(bios, REQ142_GROUP_FIT, REQ142_TO_DISCUSS), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []


def test_since_clause_degree_is_asserted():
    """an invitation's since/because clause states a settled fact (succ3 N1) and so is degree-checked"""
    ok = _prose(BIOS3, "Yuki, Leila and Tariq all put a shared room first.", "Agree together how you keep the place, which should be easy since everyone is relaxed about tidiness.")
    assert _check(ok, G3_MEMBERS, G3_BASIS, G3_ANSWERS) == []
    bad = _prose(BIOS32, "Chiara and Bruno both record very tidy.", "Agree together how you keep the place, which should be easy since everyone is relaxed about tidiness.")
    assert any("tidiness degree" in p for p in _check(bad, G32_MEMBERS, G32_BASIS, G32_ANSWERS))


def test_covered_person_without_a_value_in_the_context_fails_closed():
    """root control: coverage says I3 answered, but the validation context has no value to check -> the stated degree is not accepted"""
    answers = {"I1": {"tidy": "Very tidy"}, "I2": {"tidy": "Very tidy"}}
    prose = _prose(BIOS32, "Everyone is very tidy.", "Agree chores together.")
    problems = _check(prose, G32_MEMBERS, G32_BASIS, answers)
    assert len(problems) == 1 and "cannot be checked against a recorded answer for I3" in problems[0]
    # a named pair whose values ARE present is still fine
    assert _check(_prose(BIOS32, "Chiara and Bruno are both very tidy.", "Agree chores together."), G32_MEMBERS, G32_BASIS, answers) == []


# ---- lexicon helpers ---------------------------------------------------------------------------------------------------------------
def test_habit_degree_terms_most_specific_wins_and_bare_words_absent():
    t = ai.habit_degree_terms("very tidy and social, relaxed about tidiness, clean kitchen")
    assert [(w.lower(), set(o), r, n) for w, o, r, n in t["tidy"]] == [("very tidy", {"Very tidy"}, "plain", 0), ("relaxed about tidiness", {"Relaxed"}, "plain", 0)]
    assert [(w.lower(), set(o), r, n) for w, o, r, n in t["social"]] == [("social", {"Very social", "Friendly but independent"}, "plain", 0)]
    assert ai.habit_degree_terms("clean, tidiness, cleaning, quiet hours, friendly, early riser, night owl, guests often, outgoing, private bedroom") == {}


def test_partitive_and_negated_readings_but_whole_group_counts_stay_plain():
    assert ai.habit_degree_terms("with two very tidy people")["tidy"][0][2:] == ("partitive", 2)
    assert ai.habit_degree_terms("some of you are very tidy")["tidy"][0][2:] == ("partitive", 1)
    assert ai.habit_degree_terms("one relaxed about tidiness")["tidy"][0][2:] == ("partitive", 1)
    assert ai.habit_degree_terms("the two of you are very tidy")["tidy"][0][2:] == ("plain", 0)
    assert ai.habit_degree_terms("all three of you are very tidy")["tidy"][0][2:] == ("plain", 0)
    assert ai.habit_degree_terms("nobody is very tidy")["tidy"][0][2:] == ("negated", 0)
    assert ai.habit_degree_terms("none of you are tidy")["tidy"][0][2:] == ("negated", 0)


# ---- server-side context: bound to the captured evidence, never to the prompt --------------------------------------------------------
def test_intro_habit_answers_reads_captured_evidence_for_leads_only():
    snap = {"members": [{"enquiry_id": 51, "facts": {"tidy": "Relaxed", "social": "Very social", "routine": "Early riser", "guests": None, "budget_max": 300}},
                        {"enquiry_id": 160, "facts": {"tidy": "Average", "social": None, "lead_answers_unverified_at": "2026-11-01T00:00:00Z"}}]}
    members = [{"ref": "I1", "enquiry_id": 51, "is_lead": True}, {"ref": "I1-m1", "enquiry_id": 51, "is_lead": False},
               {"ref": "I2", "enquiry_id": 160, "is_lead": True}, {"ref": "I3", "enquiry_id": 999, "is_lead": True}]
    out = ai_queue.intro_habit_answers({"evidence_json": json.dumps(snap)}, members)
    assert out == {"I1": {"tidy": "Relaxed", "social": "Very social"}}   # routine/guests not carried; party member, takeover lead, unknown enquiry: absent
    assert ai_queue.intro_habit_answers({"evidence_json": None}, members) == {}


def test_habit_answers_never_enter_the_intro_payload_whitelists():
    assert set(ai_queue.INTRO_HABIT_FIELDS) == set(ai.HABIT_DEGREES) == {"tidy", "social"}   # exactly the fields degree-checked, nothing more is carried
    assert "tidy" not in ai.INTRO_BIO_KEYS and "guests" not in ai.INTRO_BIO_KEYS
    assert "tidy" not in ai_queue.INTRO_LIFESTYLE and "guests" not in ai_queue.INTRO_LIFESTYLE


def test_prose_contract_bumped_so_older_drafts_go_stale():
    assert ai.INTRO_PROSE_CONTRACT == "2026-11-10.n4-habit-degree-fidelity"
    assert ai.INTRO_PROSE_CONTRACT != "2026-11-10.n1-presupposed-habit-shapes"


# ---- draft_introduction: context applied, prompt untouched, callable contract unchanged --------------------------------------------
from tests.test_r7_intro_wording import APT as _APT, OFFER as _OFFER


def _draft_with(monkeypatch, prose, members, basis, answers, prompts):
    def reply(build, *a, **k):
        prompts.append(build(False))
        return json.dumps(prose)
    monkeypatch.setattr(ai, "_ask_bounded", reply)
    with ai.intro_validation_context(answers):
        return ai.draft_introduction(_APT, _OFFER, members, basis)   # the drafter's contract (apartment, group, members, basis) is unchanged


def test_draft_refuses_req140_degree_under_validation_context_and_prompt_never_carries_the_answers(monkeypatch):
    prompts: list[str] = []
    prose = _prose(BIOS3, REQ140_GROUP_FIT, REQ142_TO_DISCUSS)
    with pytest.raises(ai.AIUnavailable, match="tidiness degree") as ei:
        _draft_with(monkeypatch, prose, G3_MEMBERS, G3_BASIS, G3_ANSWERS, prompts)
    assert ei.value.code == "intro_unusable"
    assert prompts and all("Relaxed" not in p and "Night owl" not in p and "habit_answers" not in p and "Often" not in p for p in prompts)


def test_draft_accepts_req142_under_validation_context_and_context_is_scoped(monkeypatch):
    prompts: list[str] = []
    text = _draft_with(monkeypatch, _prose(BIOS3, REQ142_GROUP_FIT, REQ142_TO_DISCUSS), G3_MEMBERS, G3_BASIS, G3_ANSWERS, prompts)
    assert REQ142_GROUP_FIT in text and "Relaxed" not in text
    assert ai._HABIT_ANSWERS.get() is None   # nothing leaks past the with-block


def test_draft_without_any_context_fails_closed_on_a_stated_degree_but_keeps_degree_free_drafts(monkeypatch):
    """root control: production draft_introduction has no bypass - absent context = empty context, so REQ140 cannot pass a direct call"""
    monkeypatch.setattr(ai, "_ask_bounded", lambda build, *a, **k: json.dumps(_prose(BIOS3, REQ140_GROUP_FIT, REQ142_TO_DISCUSS)))
    with pytest.raises(ai.AIUnavailable, match="cannot be checked against a recorded answer"):
        ai.draft_introduction(_APT, _OFFER, G3_MEMBERS, G3_BASIS)
    monkeypatch.setattr(ai, "_ask_bounded", lambda build, *a, **k: json.dumps(_prose(BIOS3, REQ142_GROUP_FIT, REQ142_TO_DISCUSS)))
    with pytest.raises(ai.AIUnavailable, match="cannot be checked against a recorded answer"):   # even a TRUE degree needs the context to be checked
        ai.draft_introduction(_APT, _OFFER, G3_MEMBERS, G3_BASIS)
    degree_free = _prose(BIOS3, "Everyone in the household has shared their routine and their guest habits.", REQ66_TO_DISCUSS)   # question-only / no degree
    monkeypatch.setattr(ai, "_ask_bounded", lambda build, *a, **k: json.dumps(degree_free))
    assert REQ66_TO_DISCUSS in ai.draft_introduction(_APT, _OFFER, G3_MEMBERS, G3_BASIS)


def test_quiet_at_night_or_quiet_hours_preference_is_not_a_quiet_and_private_social_category():
    """root caution: a quiet-night discussion or preference is contextual, not the 'Quiet and private' social answer"""
    for gf in ("Everyone is quiet at night and keen to keep weeknights calm.",
               "Everyone is quiet after ten on weeknights, which should suit the shared wall.",
               "You all like a quiet night in now and then, and everyone is very social otherwise."):
        assert _check(_prose(BIOS3, gf, "Agree quiet hours together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS) == [], gf
    # whereas the social category's own wording, stated of very social people, is refused
    assert any("social style degree" in p for p in _check(_prose(BIOS3, "Everyone is quiet and private at home.", "Agree chores together."), G3_MEMBERS, G3_BASIS, G3_ANSWERS))
