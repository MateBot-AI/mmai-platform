"""23 Nov 2026 (Bill): sex multi-select + "Same sex only" hard filter, age range and culture interest as soft preferences,
culture validation (required question, optional lists, three choices in total), receipts / HomeBot email layout."""
import json

import pytest

from server import enquiries, homebot_receipts, matching, receipts, rules
from server import intro_profile as ip
from tests.conftest import person
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)
from server.enquiries import validate


def p(pid, sexes=(), pref=None, **kw):
    x = person(pid, **kw)
    x.own_sexes = tuple(sexes)
    x.sex_pref = pref
    return x


# ---------------- Same sex only (hard filter) ----------------
def test_same_sex_only_blocks_female_with_male():
    a, b = p("a", ["Female"], "Same sex only"), p("b", ["Male"])
    assert rules.pair_conflict(a, b) and rules.pair_conflict(b, a) is None   # only the person who asked is protected, checked both ways by the engine


def test_same_sex_only_allows_a_matching_pair():
    assert rules.pair_conflict(p("a", ["Female"], "Same sex only"), p("b", ["Female"])) is None


def test_same_sex_only_one_shared_option_is_enough_when_several_are_selected():
    assert rules.pair_conflict(p("a", ["Female", "Male"], "Same sex only"), p("b", ["Male"])) is None
    assert rules.pair_conflict(p("a", ["Female"], "Same sex only"), p("b", ["Male", "Female"])) is None


@pytest.mark.parametrize("other", [["Non-binary"], [ip.DIFFERENT_TERM], []])
def test_non_binary_other_term_or_no_answer_never_triggers_the_filter(other):
    assert rules.pair_conflict(p("a", ["Female"], "Same sex only"), p("b", other)) is None
    assert rules.pair_conflict(p("a", ["Non-binary"], "Same sex only"), p("b", ["Male"])) is None


def test_other_sex_preferences_do_not_filter():
    for pref in ("Mixed is fine", "No preference", None):
        assert rules.pair_conflict(p("a", ["Female"], pref), p("b", ["Male"])) is None


# ---------------- soft preferences: raise a score, never block ----------------
def test_age_range_soft_score():
    a, b = person("a"), person("b")
    a.age_pref = ("22–25",)
    b.own_age = 24
    assert matching._age_score([a, b]) > matching.NEUTRAL
    b.own_age = 40
    assert matching._age_score([a, b]) == matching.NEUTRAL   # outside the band: not rewarded, not penalised
    a.age_pref = ("No preference",)
    b.own_age = 24
    assert matching._age_score([a, b]) == matching.NEUTRAL


def test_culture_interest_soft_score_has_no_penalty():
    a, b = person("a"), person("b")
    a.culture_interest = "Yes — I’m interested in experiencing new cultures and languages"
    a.culture_languages = ("Hindi",)
    b.languages = ("Hindi",)
    assert matching._culture_score([a, b]) > matching.NEUTRAL
    b.languages = ("French",)
    assert matching._culture_score([a, b]) == matching.NEUTRAL
    a.culture_interest = "I’m happy to live with anyone"
    assert matching._culture_score([a, b]) == matching.NEUTRAL


def test_own_country_option_prefers_the_same_country():
    a, same, other = person("a", country="Sweden"), person("b", country="Sweden"), person("c", country="Brazil")
    for x in (a, same, other):
        x.birth_region = rules.__dict__.get("region_of", lambda c: None)(x.birth_country)
    a.culture_interest = "I prefer to live with people from my own country or region"
    assert matching._culture_score([a, same]) > matching._culture_score([a, other])


# ---------------- validation ----------------
YES = enquiries.CULTURE_YES


def test_culture_question_is_required():
    a = good_answers()
    a.pop("culture_interest")
    assert "culture_interest" in validate(a)


def test_culture_lists_are_optional_and_limited_to_three_in_total():
    assert validate(good_answers(culture_interest=YES, culture_languages=["Hindi", "Tamil"], culture_regions=["Southern Europe"])) == {}   # three in total
    over = validate(good_answers(culture_interest=YES, culture_languages=["Hindi", "Tamil"], culture_regions=["Southern Europe"], culture_countries=["Italy"]))
    assert "culture_interest" in over
    assert validate(good_answers(culture_interest=YES)) == {}   # none chosen: fine


def test_lists_are_dropped_unless_the_answer_is_yes(client):
    a = good_answers(culture_interest="I’m happy to live with anyone", culture_languages=["Hindi"])
    assert validate(a) == {}
    e = enquiries.create_enquiry(appdb(client), a, "Landing page", use_ai_countries=False)
    assert enquiries.get_enquiry(appdb(client), e["id"])["culture_languages"] == []


def test_old_own_region_and_avoid_answers_are_no_longer_required():
    a = good_answers()
    for k in ("own_region", "avoid_answer"):
        a.pop(k, None)
    assert validate(a) == {}


# ---------------- storage, receipt and matching input ----------------
def test_submission_stores_multi_sex_and_culture_and_feeds_matching(client):
    c = appdb(client)
    e = enquiries.create_enquiry(c, good_answers(sex_pref="Same sex only", intro_sex=["Female", "Male"], intro_age="24", age_pref=["22–25"],
                                                 culture_interest=YES, culture_languages=["Hindi"], culture_regions=["Southern Europe"]),
                                 "Landing page", use_ai_countries=False)
    full = enquiries.get_enquiry(c, e["id"])
    assert full["intro_sex"] == "Female, Male" and full["culture_languages"] == ["Hindi"] and full["culture_regions"] == ["Southern Europe"]
    assert full["age_pref"] == ["22–25"]
    u = matching.unit_from_row(full, [])
    lead = u.people[0]
    assert lead.own_sexes == ("Female", "Male") and lead.sex_pref == "Same sex only" and lead.own_age == 24
    assert lead.culture_interest == YES and lead.culture_languages == ("Hindi",)


def test_receipt_shows_the_new_answers_without_the_old_labels():
    e = {"id": 7, "answers_json": json.dumps(good_answers(sex_pref="Same sex only", intro_sex=["Female", ip.DIFFERENT_TERM], intro_sex_other="Agender",
                                                          intro_age="24", age_pref=["22–25"], culture_interest=YES,
                                                          culture_languages=["Hindi"]))}
    text = json.dumps(receipts.receipt_rows(e), ensure_ascii=False)
    for want in ("Housemate sex preference", "Same sex only", "Preferred housemate age range", "22–25", "Female, Agender", "Hindi",
                 "Languages I’m interested in"):
        assert want in text, want
    for gone in ("not used for matching", "for introductions only", "Information only"):
        assert gone not in text, gone


def test_old_single_sex_answers_stay_exactly_as_given():
    assert ip.sex_text("Prefer not to say", None) == "Prefer not to say"
    assert ip.sex_text("Another term", "Agender") == "Agender"
    assert ip.clean({"intro_sex": "Prefer not to say"})[0]["intro_sex"] == "Prefer not to say"


# ---------------- HomeBot emails use the MateBot layout ----------------
SAVED = {"first_name": "Ann", "last_name": "Lee", "email": "ann@example.com", "phone": "+61 400 000 000", "full_name": "Ann Lee",
         "sections": [["About you", [["Name", "Ann Lee"], ["Email", "ann@example.com"]]], ["Your stay", [["Move in", "1 Feb 2027"]]]]}


def test_homebot_customer_email_is_laid_out_like_matebots():
    r = homebot_receipts.render(SAVED, 5, False)
    for want in ("Hi Ann,", "HomeBot-00005", "About you\n---------", "Name: Ann Lee", receipts.LOCKED_IN_LINE, "Kind Regards,", "DISCLAIMER:"):
        assert want in r["text"], want
    assert "<h3" in r["html"] and "<table" in r["html"] and "DISCLAIMER" in r["html"] and "<dl>" not in r["html"]
    assert r["subject"] == homebot_receipts.SUBJECT


def test_homebot_reservations_email_is_laid_out_like_matebots():
    r = homebot_receipts.render_reservations(SAVED, 5, False)
    assert r["subject"] == "HomeBot enquiry from Ann Lee"
    assert "Name: Ann Lee" in r["text"] and "About you\n---------" in r["text"]
    assert "<h3" in r["html"] and "<table" in r["html"] and "<dl>" not in r["html"]
