"""I8 / I11(d), 8 November 2026 (Bill): MMAI asks nothing about sex, gender, date of birth or age, and uses none of it. The only
age-related fact is the enquirer's explicit declaration that EVERY occupant will be 18 or older on the move-in date.

Covered here: intake without the retired fields; false / missing / malformed declaration rejected; legacy payloads and staff
edits sanitised; the declaration is bound to the move-in date and the party (earlier date, renamed member, changed lead, missing
member row -> renewed declaration needed); receipt / staff summary / AI packet carry nothing about sex or age; pre-8-Nov rows
stay unconfirmed and are held for staff, never auto-confirmed from consent_age or anything else; drafts cannot rehydrate.
"""
import json
from datetime import date

import pytest

from server import matching, receipts, rules
from server.db import rows
from server.enquiries import (ADULTS_MESSAGE, RETIRED_EDIT_MESSAGE, ValidationError, adults_confirm_value, adults_status, clean_answers,
                              create_enquiry, get_enquiry, update_enquiry, update_party_member, validate)
from tests.test_data_and_api import appdb, client, good_answers, member  # noqa: F401  (fixture re-used)

T = date(2026, 11, 8)
RETIRED = {"dob": "2000-05-05", "sex": "Female", "sex_other": "x", "sex_pref": "Same sex only", "age_pref": ["18–21"], "consent_age": True,
           "guardian_name": "P", "guardian_relationship": "Mother", "guardian_email": "p@example.com", "guardian_phone": "+61 400",
           # origin preferences (legacy 5.3/5.4) retired 8 Nov 2026: never stored, never scored
           "own_region": "Prefer to", "avoid_answer": "Yes", "avoid_regions": ["Europe"], "avoid_countries": ["Brazil"]}


# ---------------- 1. intake without sex / DOB; the declaration is mandatory and strict ----------------
def test_new_intake_has_no_sex_or_dob_and_passes():
    a = good_answers()
    assert not ({"dob", "sex", "sex_pref", "age_pref", "consent_age"} & set(a))
    assert validate(a, today=T) == {}


@pytest.mark.parametrize("bad", [False, None, "true", "True", "yes", "on", 1, 0, "1", [True], {"v": True}, 1.0, "", " "])
def test_declaration_false_missing_or_malformed_is_rejected(bad):
    a = good_answers(adults_confirm=bad)
    err = validate(a, today=T)
    assert set(err) == {"adults_confirm"}, (bad, err)   # the only problem is the declaration (a dict trips the generic shape guard first)
    assert err["adults_confirm"] in (ADULTS_MESSAGE, "Invalid value.")
    assert adults_confirm_value(bad) is not True


def test_declaration_missing_key_is_rejected():
    a = good_answers()
    del a["adults_confirm"]
    assert validate(a, today=T) == {"adults_confirm": ADULTS_MESSAGE}


def test_declaration_is_recorded_with_what_it_covered(db):
    e = create_enquiry(db, good_answers(party="With friends", party_size="2", party_members=[member()]), "Landing page", use_ai_countries=False)
    assert e["adults_confirmed"] is True and e["adults_status"] == "confirmed" and e["adults_confirmed_by"] == "enquirer"
    assert e["adults_confirmed_move_in"] == "2027-02-01" and e["adults_confirmed_at"]
    fp = json.loads(rows(db.execute("SELECT adults_confirmed_party FROM enquiries WHERE id=?", (e["id"],)))[0]["adults_confirmed_party"])
    assert fp == {"lead": ["test", "person", "t@example.com"], "size": 2, "n": 1, "members": [["a friend", "a@example.com"]]}
    assert "consent_age" not in e   # the old lead-only tick is never shown next to the all-occupants declaration


# ---------------- 2. legacy payload / edit sanitisation ----------------
def test_legacy_public_payload_keys_are_dropped_not_stored(db):
    a = good_answers(**RETIRED, party="Couple sharing one bed", party_members=[member(sex="Male", dob="2001-01-01", needs_guardian=1)])
    assert validate(a, today=T) == {}
    e = create_enquiry(db, a, "Landing page", use_ai_countries=False)
    stored = json.loads(rows(db.execute("SELECT answers_json FROM enquiries WHERE id=?", (e["id"],)))[0]["answers_json"])
    assert not (set(RETIRED) & set(stored)) and set(stored["party_members"][0]) == {"name", "email", "birth_country_raw"}
    assert stored["adults_confirm"] is True
    raw = rows(db.execute("SELECT dob, sex, sex_pref, age_pref, guardian_name FROM enquiries WHERE id=?", (e["id"],)))[0]
    assert raw == {"dob": "", "sex": "", "sex_pref": "", "age_pref": None, "guardian_name": None}   # '' = not collected, never a value
    assert rows(db.execute("SELECT sex, dob, needs_guardian FROM party_members WHERE enquiry_id=?", (e["id"],))) == [{"sex": None, "dob": None, "needs_guardian": 0}]


def test_legacy_consent_age_alone_is_not_a_declaration():
    a = good_answers(consent_age=True)
    del a["adults_confirm"]
    assert "adults_confirm" in validate(a, today=T)


def test_staff_edit_of_retired_field_is_refused(db):
    e = create_enquiry(db, good_answers(party="With friends", party_size="2", party_members=[member()]), "Phone", staff="S", use_ai_countries=False)
    for k in ("dob", "sex", "sex_other", "sex_pref", "age_pref", "guardian_name"):
        with pytest.raises(ValidationError) as ex:
            update_enquiry(db, e["id"], {k: "x", "staff_notes": "n"}, "S")
        assert ex.value.errors == {k: RETIRED_EDIT_MESSAGE}
    mid = e["party_members"][0]["id"]
    for k in ("sex", "sex_other", "dob", "needs_guardian"):
        with pytest.raises(ValidationError):
            update_party_member(db, e["id"], mid, {k: "x"}, "S")
    assert rows(db.execute("SELECT staff_notes FROM enquiries WHERE id=?", (e["id"],)))[0]["staff_notes"] is None   # nothing partially applied


def test_staff_member_patch_api_returns_422_for_retired_field(client):
    e = client.post("/api/enquiries", json=good_answers(party="With friends", party_size="2", party_members=[member()])).json()
    mid = e["party_members"][0]["id"]
    r = client.patch(f"/api/enquiries/{e['id']}/members/{mid}", json={"sex": "Female"})
    assert r.status_code == 422 and r.json()["errors"] == {"sex": RETIRED_EDIT_MESSAGE}
    r = client.patch(f"/api/enquiries/{e['id']}", json={"dob": "2000-01-01"})
    assert r.status_code == 422 and "dob" in r.json()["errors"]


def test_staff_adults_confirmed_patch_is_strict_bool(client):
    e = client.post("/api/enquiries", json=good_answers()).json()
    for bad in ("true", "yes", 1, "1", None, [True]):
        r = client.patch(f"/api/enquiries/{e['id']}", json={"adults_confirmed": bad})
        assert r.status_code == 422 and "adults_confirmed" in r.json()["errors"], bad
    r = client.patch(f"/api/enquiries/{e['id']}", json={"adults_confirmed": False})
    assert r.status_code == 200 and r.json()["adults_confirmed"] is False and r.json()["adults_status"] == "missing"
    r = client.patch(f"/api/enquiries/{e['id']}", json={"adults_confirmed": True})
    j = r.json()
    assert r.status_code == 200 and j["adults_status"] == "confirmed" and j["adults_confirmed_by"].startswith("staff:") and j["adults_confirmed_move_in"] == "2027-02-01"


def test_get_and_list_omit_retired_fields_and_expose_status(client):
    e = client.post("/api/enquiries", json=good_answers(**RETIRED)).json()
    assert not (set(RETIRED) & set(e)) and e["adults_status"] == "confirmed"
    lst = client.get("/api/enquiries?dataset=real").json()
    it = next(x for x in lst["items"] if x["id"] == e["id"])
    assert not ({"sex", "dob", "guardian_todo", "adults_confirmed_party"} & set(it)) and it["adults_status"] == "confirmed"
    assert lst["total"] == 1


# ---------------- 3. the declaration is bound to the move-in date and the party ----------------
def _legacy_row(db, **over):
    """A pre-8-Nov enquiry: written through the current code, then its declaration columns cleared as the migration leaves them."""
    e = create_enquiry(db, good_answers(**over), "Landing page", use_ai_countries=False)
    db.execute("UPDATE enquiries SET adults_confirmed=0, adults_confirmed_by=NULL, adults_confirmed_at=NULL, adults_confirmed_move_in=NULL, adults_confirmed_party=NULL, "
               "consent_age=1, dob='2000-05-05', sex='Female', sex_pref='No preference' WHERE id=?", (e["id"],))
    db.commit()
    return get_enquiry(db, e["id"])


def test_legacy_row_is_unconfirmed_and_held_not_fabricated(db):
    e = _legacy_row(db)
    assert e["adults_confirmed"] is False and e["adults_status"] == "missing"
    u = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0], [])
    assert u.adults_confirmed_move_in is None
    other = create_enquiry(db, good_answers(first_name="Other", email="o@example.com"), "Landing page", use_ai_countries=False)
    u2 = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (other["id"],)))[0], [])
    apt = rules.Apartment(id="a1", address="x", type_code="2B2B", rent_12m=1150.0, rent_6m=1225.0, available_from=date(2027, 1, 27),
                          bedrooms=[rules.parse_bedding("1 king or 2 singles")] * 2, min_lease_months=rules.DEFAULT_MIN_LEASE_MONTHS)
    r = rules.check_group(apt, [u, u2])
    assert not r.ok and any("staff to confirm" in x and "I11(d)" in x for x in r.reasons)
    # staff record the declaration -> matchable; the record says who and for what
    e2 = update_enquiry(db, e["id"], {"adults_confirmed": True}, "Anna")
    assert e2["adults_status"] == "confirmed" and e2["adults_confirmed_by"] == "staff:Anna"
    u3 = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0], [])
    assert rules.check_group(apt, [u3, u2]).ok


def test_earlier_move_in_needs_renewal_later_is_covered(db):
    e = create_enquiry(db, good_answers(), "Landing page", use_ai_countries=False)
    assert update_enquiry(db, e["id"], {"move_in": "2027-02-15"}, "S")["adults_status"] == "confirmed"      # later: same people, older
    assert update_enquiry(db, e["id"], {"move_in": "2027-01-20"}, "S")["adults_status"] == "renew"          # earlier: not inherited
    u = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0], [])
    assert u.adults_confirmed_move_in is None
    # renewed by staff together with the date: bound to the new date
    e2 = update_enquiry(db, e["id"], {"move_in": "2027-01-15", "adults_confirmed": True}, "Anna")
    assert e2["adults_status"] == "confirmed" and e2["adults_confirmed_move_in"] == "2027-01-15"


def test_renamed_member_same_email_and_changed_lead_need_renewal(db):
    e = create_enquiry(db, good_answers(party="With friends", party_size="3", party_members=[member(), member(name="B", email="b@example.com")]),
                       "Phone", staff="S", use_ai_countries=False)
    mid = e["party_members"][0]["id"]
    assert update_party_member(db, e["id"], mid, {"name": "A Friend"}, "S")["adults_status"] == "confirmed"      # unchanged (case/space-insensitive)
    assert update_party_member(db, e["id"], mid, {"name": "Somebody Else"}, "S")["adults_status"] == "renew"      # same email, different person
    update_party_member(db, e["id"], mid, {"name": "A Friend"}, "S")
    assert update_enquiry(db, e["id"], {"first_name": "Another"}, "S")["adults_status"] == "renew"                 # changed lead identity
    update_enquiry(db, e["id"], {"first_name": "Test"}, "S")
    assert update_enquiry(db, e["id"], {"email": "new@example.com"}, "S")["adults_status"] == "renew"
    # renewal in the same authenticated edit binds to the resulting state, not the old one
    e2 = update_enquiry(db, e["id"], {"last_name": "Changed", "adults_confirmed": True}, "Anna")
    assert e2["adults_status"] == "confirmed" and e2["last_name"] == "Changed"
    assert update_enquiry(db, e["id"], {"last_name": "Person"}, "S")["adults_status"] == "renew"


def test_missing_member_row_or_changed_size_fails_closed(db):
    e = create_enquiry(db, good_answers(party="With friends", party_size="3", party_members=[member(), member(name="B", email="b@example.com")]),
                       "Phone", staff="S", use_ai_countries=False)
    db.execute("DELETE FROM party_members WHERE enquiry_id=? AND name='B'", (e["id"],)); db.commit()
    g = get_enquiry(db, e["id"])
    assert g["adults_status"] == "renew"
    u = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0],
                               rows(db.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=?", (e["id"],))))
    assert len(u.people) == 3 and u.adults_confirmed_move_in is None       # the placeholder occupant is not covered by anyone's declaration
    e2 = create_enquiry(db, good_answers(email="x@example.com", party="With friends", party_size="2", party_members=[member()]), "Phone", staff="S", use_ai_countries=False)
    db.execute("UPDATE enquiries SET party_size=3 WHERE id=?", (e2["id"],)); db.commit()
    assert get_enquiry(db, e2["id"])["adults_status"] == "renew"
    assert adults_status({"adults_confirmed": 1, "adults_confirmed_move_in": "2027-02-01", "move_in": "2027-02-01", "party_size": "abc",
                          "adults_confirmed_party": "", "first_name": "a", "last_name": "b", "email": "c"}, []) == "renew"


def test_party_change_through_gp9e_bar_needs_renewal(db):
    """GP9(e): taking one person off a form party changes the party; the declaration no longer covers it until renewed."""
    e = create_enquiry(db, good_answers(party="With friends", party_size="3", party_members=[member(), member(name="B", email="b@example.com")]),
                       "Phone", staff="S", is_test=True, use_ai_countries=False)
    matching.bar_enquiry(db, e["id"], "S", "test reason", person=e["party_members"][1]["id"], party_choice="stay")
    g = get_enquiry(db, e["id"])
    assert g["party_size"] == 2 and len(g["party_members"]) == 1 and g["adults_status"] == "renew"
    assert rows(db.execute("SELECT sex, dob FROM barred_people WHERE enquiry_id=?", (e["id"],))) == [{"sex": None, "dob": None}]   # not copied
    # the enquirer barred, first member takes over: identity changed -> renew; legacy columns get '' not a copied value
    e2 = create_enquiry(db, good_answers(email="lead2@example.com", party="With friends", party_size="2", party_members=[member(name="Taker", email="t@example.com")]),
                        "Phone", staff="S", is_test=True, use_ai_countries=False)
    matching.bar_enquiry(db, e2["id"], "S", "test reason", person="lead", party_choice="stay")
    g2 = get_enquiry(db, e2["id"])
    assert g2["first_name"] == "Taker" and g2["adults_status"] == "renew"
    assert rows(db.execute("SELECT sex, dob FROM enquiries WHERE id=?", (e2["id"],)))[0] == {"sex": "", "dob": ""}


def test_todo_filter_and_stats_count_unconfirmed(client):
    e1 = client.post("/api/enquiries", json=good_answers()).json()
    e2 = client.post("/api/enquiries", json=good_answers(email="b@example.com", first_name="Bee")).json()
    c = appdb(client)
    c.execute("UPDATE enquiries SET adults_confirmed=0, adults_confirmed_party=NULL, adults_confirmed_move_in=NULL WHERE id=?", (e2["id"],)); c.commit()
    lst = client.get("/api/enquiries?dataset=real&todo=adults").json()
    assert [x["id"] for x in lst["items"]] == [e2["id"]] and lst["total"] == 1 and lst["items"][0]["adults_status"] == "missing"
    s = client.get("/api/stats").json()
    assert s["adults_todo"] == 1 and s["adults_todo_test"] == 0
    assert client.get("/api/enquiries?dataset=real").json()["total"] == 2


# ---------------- 4. nothing about sex or age reaches the receipt, the AI, the staff summary or matching ----------------
def test_receipt_ai_packet_and_summary_are_free_of_sex_and_age(db):
    e = create_enquiry(db, good_answers(**RETIRED, party="With friends", party_size="2", party_members=[member(sex="Male", dob="2001-01-01")]),
                       "Landing page", use_ai_countries=False)
    text = receipts.render_receipt(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0])["text"]
    for gone in ("Date of birth", "Sex", "sex preference", "age range", "born ", "Parent or guardian", "I confirm I will be 18"):
        assert gone not in text, gone
    assert "everyone included in this enquiry, including me, will be 18 or older on the move-in date. If that is not the case, please contact the ALM team instead of continuing.: Yes" in text
    u = matching.unit_from_row(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))[0],
                               rows(db.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=?", (e["id"],))))
    packet = matching.members_for_ai([u])[0]
    flat = json.dumps(packet).lower()
    for word in ('"sex"', '"age"', "age_pref", "sex_pref", "dob", "gender", "guardian"):
        assert word not in flat, word
    assert packet["party_members"] == ["U1-m1"]   # launch scope: positional refs only, no names or countries
    assert not any(hasattr(p, k) for p in u.people for k in ("sex", "dob", "sex_pref"))


def test_preference_score_has_no_age_component(db):
    import inspect
    src = inspect.getsource(matching.preference_score)
    code = "\n".join(line.split("#")[0] for line in src.splitlines())   # comments aside
    assert '"age"' not in code and "age_pref" not in code and "AGE_BANDS" not in code and "dob" not in code
    assert not hasattr(matching, "AGE_BANDS")


def test_ai_prompts_do_not_ask_about_sex_or_age():
    import inspect
    from server import ai
    src = inspect.getsource(ai)
    assert "age mix" not in src and "sex preference, smoking" not in src
    assert "sex, gender or age in" in src and "never infer, mention or weigh any of those attributes" in src.lower()
    # introduction prompt (r7-Q engineering implementation of GP2 + the neutral-prose ruling): the model never writes sex/age - the supplied label is printed deterministically in the
    # profile line (GP2) and the model payload carries no intro_sex / intro_age; no contradictory "only if supplied" permission remains
    low = src.lower()
    assert "only if intro_sex/intro_age is present" not in low and "never guess or imply sex, gender, age" in low
    assert "sex and age are never written by you" in low and "the system prints each person's supplied label in their profile line" in low
    assert "use the person's name or neutral 'they'; never 'he'/'she' or gendered nouns" in low   # r7: no label licenses pronouns
    assert "intro_sex" not in ai.INTRO_BIO_KEYS and "intro_age" not in ai.INTRO_BIO_KEYS


def test_matching_result_is_invariant_to_legacy_sex_and_age_values(db):
    """Two identical neutral enquiries whose legacy columns differ wildly produce identical units and checks."""
    a = create_enquiry(db, good_answers(email="a@example.com"), "Landing page", use_ai_countries=False)
    b = create_enquiry(db, good_answers(email="b@example.com", first_name="Test"), "Landing page", use_ai_countries=False)
    db.execute("UPDATE enquiries SET sex='Male', dob='2009-12-31', sex_pref='Same sex only', age_pref='[\"31+\"]' WHERE id=?", (a["id"],)); db.commit()
    ra, rb = (rows(db.execute("SELECT * FROM enquiries WHERE id=?", (i,)))[0] for i in (a["id"], b["id"]))
    ua, ub = matching.unit_from_row(ra, []), matching.unit_from_row(rb, [])
    strip = lambda u: {k: v for k, v in u.__dict__.items() if k not in ("id", "extra", "people")}
    assert strip(ua) == strip(ub)
    pa, pb = ua.people[0], ub.people[0]
    skip = ("id", "unit_id", "ident_name", "ident_email")   # identity provenance (uid stamping) differs by design: a@ vs b@; not an engine input
    assert {k: v for k, v in pa.__dict__.items() if k not in skip} == {k: v for k, v in pb.__dict__.items() if k not in skip}
    assert rules.pair_conflict(pa, pb) is None and rules.pair_conflict(pb, pa) is None


# ---------------- 5. drafts and public validate cannot rehydrate retired answers ----------------
def test_draft_round_trip_drops_retired_keys(client):
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    body = {"answers": {**good_answers(), **RETIRED, "party_members": [member(sex="Female", dob="2000-01-01")], "unknown_thing": 1}, "step": 5}
    r = anon.post("/api/public/drafts", json=body)
    assert r.status_code == 201
    got = anon.get(f"/api/public/drafts/{r.json()['token']}").json()["answers"]
    assert not ((set(RETIRED) | {"unknown_thing"}) & set(got)) and set(got["party_members"][0]) == {"name", "email", "birth_country_raw"}
    assert got["adults_confirm"] is True
    # a draft stored before 8 Nov with the old keys is cleaned on read too
    c = appdb(client)
    c.execute("UPDATE form_drafts SET answers_json=? WHERE token=?", (json.dumps({**good_answers(), **RETIRED}), r.json()["token"])); c.commit()
    got2 = anon.get(f"/api/public/drafts/{r.json()['token']}").json()["answers"]
    assert not (set(RETIRED) & set(got2))


def test_clean_answers_is_an_allowlist():
    a = {**good_answers(), **RETIRED, "evil": 1, "party_members": [{"name": "n", "email": "e", "birth_country_raw": "c", "dob": "d", "x": 1}]}
    c = clean_answers(a)
    assert not ((set(RETIRED) | {"evil"}) & set(c)) and c["party_members"] == [{"name": "n", "email": "e", "birth_country_raw": "c"}]
    assert set(good_answers()) - set(RETIRED) <= set(c)


def test_public_validate_endpoint_reports_declaration(client):
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    a = good_answers(adults_confirm="true")
    r = anon.post("/api/public/validate", json=a)
    assert r.status_code in (200, 422)
    errs = r.json().get("errors", r.json())
    assert "adults_confirm" in errs
