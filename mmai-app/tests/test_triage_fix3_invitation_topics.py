"""Triage fix 3 (9 Nov 2026): an invitation sentence keeps its invitation meaning through an indirect-question list item that
carries a whole-household marker ("... and how social you each like the home to be"), while a later ACTUAL assertion in the same
sentence is still rejected. The captured REQ 66 to_discuss (fresh run) is the positive case. Fabricated doubles only; wording guard."""
from server import ai

MEMBERS = [{"ref": "I1", "first_name": "Yuki", "name": "Yuki"}, {"ref": "I2", "first_name": "Leila", "name": "Leila"}, {"ref": "I3", "first_name": "Tariq", "name": "Tariq"}]
# basis as the drafter receives it: coverage says every person answered routine and guests themselves (not tidy / social)
def _cov(*fields):
    return {f: {"status": "recorded", "basis": "own"} for f in fields}


BASIS = [{"section": "discussion_points", "text": "Agree guest etiquette and quiet hours up front, given the mix of early, late and varying routines.",
          "subjects": [{"packet_ref": "U1", "ref": "I1"}, {"packet_ref": "U2", "ref": "I2"}, {"packet_ref": "U3", "ref": "I3"}],
          "coverage": {"U1": _cov("routine", "guests"), "U2": _cov("routine", "guests"), "U3": _cov("routine", "guests")}}]
REQ66_TO_DISCUSS = ("It would be great for you all to agree guest etiquette and quiet hours early on, given the mix of routines and how often guests may visit. "
                    "Chat together about tidiness, chores, cooking and how social you each like the home to be, so everyone's expectations are clear from the start.")
PROSE = {"opening": "Hello from Apartment Living Melbourne! We're pleased to introduce you as a proposed household for the apartment below.",
         "bios": {"I1": "Yuki was born in South Africa.", "I2": "Leila was born in Brazil.", "I3": "Tariq was born in Singapore."},
         "group_fit": "Everyone in the household has shared their routine and their guest habits.",
         "to_discuss": REQ66_TO_DISCUSS, "closing": "Please say hello to each other and reply to us at ALM with any questions."}


def _own(basis):
    return ai._own_coverage(basis)


def test_basis_fixture_gives_routine_and_guests_only():
    assert _own(BASIS) == {"I1": {"routine", "guests"}, "I2": {"routine", "guests"}, "I3": {"routine", "guests"}}


def test_captured_req66_to_discuss_passes():
    assert ai.habit_claim_problems(PROSE, MEMBERS, BASIS) == []


def test_actual_assertion_after_an_invitation_is_still_rejected():
    for bad in ("Everyone should discuss routines, and everyone is very tidy.",
                "Chat together about tidiness and how social you each like the home to be, and you all keep things very tidy.",
                "Talk through guests together; the household is very social."):
        pr = ai.habit_claim_problems(dict(PROSE, to_discuss=bad), MEMBERS, BASIS)
        assert pr and ("tidy" in pr[0] or "social" in pr[0]), (bad, pr)


def test_topic_rule_needs_an_invitation_already_under_way():
    # the same indirect-question clause WITHOUT a preceding invitation verb is still a household assertion
    pr = ai.habit_claim_problems(dict(PROSE, to_discuss="How social you each like the home to be matters, and everyone is quiet."), MEMBERS, BASIS)
    assert pr and "social" in pr[0]
    # and the household-coverage requirement itself is unchanged: a to_discuss invitation about a COVERED field never trips
    assert ai.habit_claim_problems(dict(PROSE, to_discuss="Agree together how often you each have guests."), MEMBERS, BASIS) == []


def test_presupposed_habit_inside_a_wh_item_is_still_an_assertion():
    """Second round (root regression, 9 Nov 2026): a wh-word alone must not launder an assertion. 'how everyone is very tidy' / 'why
    everyone is very social' presuppose the habit; neither tidy nor social is covered by anyone's own answer here -> rejected.
    The captured REQ 66 wording (an open degree question, 'how social you each like the home to be') keeps passing."""
    for bad in ("Chat together about chores, and how everyone is very tidy.",
                "Discuss routines, and why everyone is very social.",
                "Talk about cooking, and how you all keep things really tidy."):
        pr = ai.habit_claim_problems(dict(PROSE, to_discuss=bad), MEMBERS, BASIS)
        assert pr and ("tidy" in pr[0] or "social" in pr[0]), (bad, pr)
    for ok in (REQ66_TO_DISCUSS,
               "Chat together about chores, and how tidy everyone likes the home to be.",
               "Discuss routines, and whether everyone is very social.",
               "Agree together how often you each have guests, and why guests matter to you each."):   # guests: covered by everyone's own answer
        assert ai.habit_claim_problems(dict(PROSE, to_discuss=ok), MEMBERS, BASIS) == [], ok
