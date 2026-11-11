"""Introduction-only sex and current age (Bill, 8 Nov 2026): restored SOLELY so introductions can describe each person; no role
in matching.

Principles under test
- Fresh, optional answers per person (enquirer and each party member), stored in dedicated intro_* columns; the retired legacy
  sex / dob / sex_pref / age_pref keys stay stripped and refused, and are never rehydrated into the intro fields.
- "Prefer not to say" is an answer; a blank answer is a staff flag (intro_profile_status 'incomplete'), never inferred, never a
  submission or matching failure.
- Editing the intro fields changes ONLY those columns: the adults declaration, its party fingerprint, the matching unit and the
  T6 compatibility evidence hash are byte-for-byte unchanged; the customer introduction draft is marked stale.
- Strict values: unknown sex options, non-integer / negative / boolean / list ages are rejected; 'Another term' needs the term.
- Display only: receipts and get_enquiry show them; AI compatibility packets and evidence whitelists do not carry them.
"""
import json

import pytest

from server import enquiries, intro_profile as ip, matching, receipts, rules
from server.db import now_iso, row, rows
from tests.test_data_and_api import appdb, client, good_answers, member  # noqa: F401  (fixtures re-used)

_n = [0]


def hdr():
    _n[0] += 1
    return {"X-Submission-Key": f"k_intro_{_n[0]:04d}" + "x" * 20}


def staff_get(client, eid):
    r = client.get(f"/api/enquiries/{eid}")
    assert r.status_code == 200, r.text
    return r.json()


def submit(client, **over):
    r = client.post("/api/public/enquiries", json=good_answers(**over), headers=hdr())
    assert r.status_code == 201, r.text
    return r.json()


# ---------------- validation module ----------------

def test_clean_accepts_only_the_agreed_values():
    assert ip.clean({"intro_sex": "Female", "intro_age": "17"}) == ({"intro_sex": "Female", "intro_sex_other": None, "intro_age": 17}, {})
    assert ip.clean({"intro_sex": "Prefer not to say", "intro_age": None})[0] == {"intro_sex": "Prefer not to say", "intro_sex_other": None, "intro_age": None}
    assert ip.clean({"intro_sex": "Another term", "intro_sex_other": " Non-binary ", "intro_age": 24})[0]["intro_sex_other"] == "Non-binary"
    assert "intro_sex_other" in ip.clean({"intro_sex": "Another term", "intro_age": 24})[1]           # the term is required with "Another term"
    assert "intro_sex_other" in ip.clean({"intro_sex": "Another term", "intro_sex_other": "x" * 41})[1]
    assert ip.clean({"intro_sex": "Male", "intro_sex_other": "ignored"})[0]["intro_sex_other"] is None   # only meaningful with "Another term"
    for bad in ("female", "F", "Woman", 1, True, ["Female"], {"v": "Female"}):
        assert "intro_sex" in ip.clean({"intro_sex": bad})[1], bad
    for bad in (True, False, -1, 1000, 24.5, "24.5", "twenty", "", [24], {"v": 24}, " 2 4 "):
        if bad == "":
            assert ip.clean({"intro_age": bad})[0]["intro_age"] is None   # blank = not given
        else:
            assert "intro_age" in ip.clean({"intro_age": bad})[1], bad
    assert ip.clean({"intro_age": 0})[0]["intro_age"] == 0 and ip.clean({"intro_age": 24.0})[0]["intro_age"] == 24
    assert ip.clean({"intro_age": 17})[1] == {}   # under 18 now is accepted: eligibility is the move-in declaration, not this number


def test_status_counts_prefer_not_to_say_as_an_answer_and_flags_blanks():
    assert ip.status({"first_name": "Ana", "intro_sex": "Prefer not to say", "intro_age": 30}, []) == ("complete", [])
    assert ip.status({"first_name": "Ana", "intro_sex": "Female"}, [{"name": "Bo", "intro_sex": "Male", "intro_age": 20}]) == ("incomplete", ["Ana (enquirer)"])
    assert ip.status({"first_name": "Ana", "intro_sex": "Female", "intro_age": 22}, [{"name": "Bo"}, {"name": "Cy", "intro_sex": "Prefer not to say", "intro_age": 0}]) == ("incomplete", ["Bo"])
    assert ip.describe({"intro_sex": "Another term", "intro_sex_other": "Agender", "intro_age": 24}) == "Agender, 24"
    assert ip.describe({"intro_sex": "Prefer not to say"}) == "Prefer not to say" and ip.describe({}) == "—"
    assert "not used for matching" in ip.PURPOSE


# ---------------- intake ----------------

def test_public_submission_stores_each_persons_own_answers(client):
    e = submit(client, party="With friends", party_size="3", intro_sex="Female", intro_age="24",
               party_members=[member(name="Bo Friend", email="bo@example.com", intro_sex="Another term", intro_sex_other="Non-binary", intro_age="19"),
                              member(name="Cy Friend", email="cy@example.com", intro_sex="Prefer not to say")])
    s = staff_get(client, e["id"])
    assert (s["intro_sex"], s["intro_sex_other"], s["intro_age"]) == ("Female", None, 24)
    bo, cy = s["party_members"]
    assert (bo["intro_sex"], bo["intro_sex_other"], bo["intro_age"]) == ("Another term", "Non-binary", 19)
    assert (cy["intro_sex"], cy["intro_sex_other"], cy["intro_age"]) == ("Prefer not to say", None, None)   # never copied from the enquirer
    assert s["intro_profile_status"] == "incomplete" and s["intro_missing"] == ["Cy Friend"]   # Cy gave no age; "Prefer not to say" itself is an answer
    aj = s["answers_json"]
    assert aj["intro_sex"] == "Female" and aj["intro_age"] == "24" and aj["party_members"][0]["intro_sex_other"] == "Non-binary"
    # the legacy columns stay "not collected": nothing is written to them from the intro answers
    r = row(appdb(client).execute("SELECT sex, dob, sex_other FROM enquiries WHERE id=?", (e["id"],)))
    assert r == {"sex": "", "dob": "", "sex_other": None}
    pm = rows(appdb(client).execute("SELECT sex, dob FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))
    assert all(p == {"sex": None, "dob": None} for p in pm)


def test_intro_answers_are_optional_and_missing_is_a_flag_not_a_failure(client):
    e = submit(client)   # no intro keys at all
    s = staff_get(client, e["id"])
    assert s["intro_sex"] is None and s["intro_age"] is None and s["intro_profile_status"] == "incomplete" and s["intro_missing"] == ["Test (enquirer)"]
    assert s["adults_status"] == "confirmed"   # eligibility is the declaration alone
    e2 = submit(client, email="p@example.com", intro_sex="Prefer not to say", intro_age="17")
    s2 = staff_get(client, e2["id"])
    assert s2["intro_profile_status"] == "complete" and s2["intro_missing"] == [] and s2["adults_status"] == "confirmed"   # 17 now, declared 18+ at move-in


def test_public_submission_rejects_invalid_intro_values_with_field_messages(client):
    r = client.post("/api/public/enquiries", json=good_answers(intro_sex="Woman", intro_age="24"), headers=hdr())
    assert r.status_code == 422 and "intro_sex" in r.json()["errors"], r.text
    r = client.post("/api/public/enquiries", json=good_answers(intro_sex="Female", intro_age=[24]), headers=hdr())
    assert r.status_code == 422 and "intro_age" in r.json()["errors"]
    r = client.post("/api/public/enquiries", json=good_answers(intro_sex="Another term", intro_age="24"), headers=hdr())
    assert r.status_code == 422 and "intro_sex_other" in r.json()["errors"]
    r = client.post("/api/public/enquiries", json=good_answers(party="With friends", party_size="2", party_members=[member(intro_age="-3")]), headers=hdr())
    assert r.status_code == 422 and "party_members.0.intro_age" in r.json()["errors"]


def test_legacy_sex_dob_keys_are_still_stripped_and_never_become_intro_values(client):
    e = submit(client, sex="Female", dob="2000-01-01", sex_pref="Same sex only", age_pref=["18-25"],
               party="With friends", party_size="2", party_members=[member(sex="Male", dob="2001-02-02")])
    s = staff_get(client, e["id"])
    assert s["intro_sex"] is None and s["intro_age"] is None and s["party_members"][0]["intro_sex"] is None
    assert not ({"sex", "dob", "sex_pref", "age_pref"} & set(s)) and "sex" not in s["answers_json"] and "dob" not in s["answers_json"]["party_members"][0]
    # a historical row with legacy values is not rehydrated either
    appdb(client).execute("UPDATE enquiries SET sex='Male', dob='1999-05-05' WHERE id=?", (e["id"],)); appdb(client).commit()
    s = staff_get(client, e["id"])
    assert s["intro_sex"] is None and s["intro_age"] is None and s["intro_profile_status"] == "incomplete"


# ---------------- staff edits ----------------

def _plant_group(c, eid):
    c.execute("""INSERT INTO apartments(city, address, address_raw, suburb, type_code, bathrooms, rent_12m, rent_6m, available_from, min_lease_months,
                                        bedroom1, bedroom2, bedroom3, bills_included, updated_at)
                 VALUES ('Melbourne', 'Unit 9 / 9 Intro Street', 'Unit 9/9 Intro St', 'Melbourne', '2B2B', 2, 1150.0, 1225.0, '2027-01-27', ?, '1 king or 2 singles', '1 king or 2 singles', NULL, 0, ?)""",
              (rules.DEFAULT_MIN_LEASE_MONTHS, now_iso()))
    aid = c.execute("SELECT id FROM apartments WHERE address='Unit 9 / 9 Intro Street'").fetchone()[0]
    c.execute("INSERT INTO groups(apartment_id, status, member_key, created_at, updated_at) VALUES (?, 'Proposed', ?, ?, ?)", (aid, str(eid), now_iso(), now_iso()))
    gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO group_members(group_id, enquiry_id, weekly_share) VALUES (?, ?, 575.0)", (gid, eid))
    c.commit()
    return gid


def test_staff_edit_changes_only_intro_columns_and_marks_the_intro_draft_stale(client):
    e = submit(client, party="With friends", party_size="2", party_members=[member(name="Bo Friend", email="bo@example.com")])
    c = appdb(client)
    gid = _plant_group(c, e["id"])
    before = row(c.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))
    unit_before = matching.unit_from_row(before, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],))))
    hash_before, _ = matching.group_evidence(c, gid)
    status_before = staff_get(client, e["id"])["adults_status"]

    r = client.patch(f"/api/enquiries/{e['id']}", json={"intro_sex": "Another term", "intro_sex_other": "Genderqueer", "intro_age": 27})
    assert r.status_code == 200, r.text
    mid = r.json()["party_members"][0]["id"]
    r2 = client.patch(f"/api/enquiries/{e['id']}/members/{mid}", json={"intro_sex": "Male", "intro_age": "17"})
    assert r2.status_code == 200, r2.text

    after = row(c.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {"intro_sex", "intro_sex_other", "intro_age"}, changed   # adults_*, fingerprint, identity, everything else untouched
    s = staff_get(client, e["id"])
    assert s["adults_status"] == status_before == "confirmed" and s["intro_profile_status"] == "complete"
    assert (s["party_members"][0]["intro_sex"], s["party_members"][0]["intro_age"]) == ("Male", 17)
    unit_after = matching.unit_from_row(after, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],))))
    strip = lambda u: {k: v for k, v in u.__dict__.items() if k not in ("people", "extra")}   # `extra` is the raw row itself (carries every column)
    assert strip(unit_after) == strip(unit_before) and len(unit_after.people) == len(unit_before.people)
    assert [p.__dict__ for p in unit_after.people] == [p.__dict__ for p in unit_before.people]   # rule-level people: identical
    hash_after, snap = matching.group_evidence(c, gid)
    assert hash_after == hash_before and "intro" not in json.dumps(snap)   # no rerank / re-review trigger
    # the customer introduction draft for the group is marked stale (append-only), twice: once per edit
    marks = rows(c.execute("SELECT kind, status, enquiry_id FROM group_briefs WHERE group_id=? ORDER BY id", (gid,)))
    assert len(marks) == 2 and all(m["kind"] == "intro" and m["status"] == "stale" and m["enquiry_id"] == e["id"] for m in marks)
    g = row(c.execute("SELECT status, ai_status, ai_queued_at FROM groups WHERE id=?", (gid,)))
    assert g["status"] == "Proposed" and g["ai_status"] is None and g["ai_queued_at"] is None


def test_staff_edit_validation_and_retired_keys_still_refused(client):
    e = submit(client)
    r = client.patch(f"/api/enquiries/{e['id']}", json={"intro_sex": "Another term"})
    assert r.status_code == 422 and "intro_sex_other" in r.json()["errors"]
    r = client.patch(f"/api/enquiries/{e['id']}", json={"intro_age": "abc"})
    assert r.status_code == 422 and "intro_age" in r.json()["errors"]
    r = client.patch(f"/api/enquiries/{e['id']}", json={"sex": "Female"})
    assert r.status_code == 422 and "sex" in r.json()["errors"]
    r = client.patch(f"/api/enquiries/{e['id']}", json={"dob": "2000-01-01"})
    assert r.status_code == 422
    # clearing is allowed (the person withdrew the detail): back to a staff flag, nothing inferred
    r = client.patch(f"/api/enquiries/{e['id']}", json={"intro_sex": "Female", "intro_age": 30})
    assert r.status_code == 200 and r.json()["intro_profile_status"] == "complete"
    r = client.patch(f"/api/enquiries/{e['id']}", json={"intro_sex": None, "intro_age": None})
    assert r.status_code == 200 and r.json()["intro_profile_status"] == "incomplete" and r.json()["intro_sex"] is None


# ---------------- display only: receipts, AI whitelists ----------------

def test_receipt_shows_intro_answers_only_when_given_and_never_legacy_values(client):
    e = submit(client, intro_sex="Another term", intro_sex_other="Non-binary", intro_age="24",
               party="With friends", party_size="2", party_members=[member(name="Bo Friend", email="bo@example.com", intro_sex="Male", intro_age="20")])
    c = appdb(client)
    c.execute("UPDATE enquiries SET sex='Female', dob='2000-01-01' WHERE id=?", (e["id"],)); c.commit()
    text = json.dumps(receipts.receipt_rows(row(c.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],)))))
    assert "Sex (shared in introductions to proposed housemates; not used for matching)\", \"Non-binary\"" in text
    assert "Current age (shared in introductions to proposed housemates; not used for matching)\", \"24\"" in text
    assert "for introductions only: Male, 20" in text and "2000-01-01" not in text and "Female" not in text
    e2 = submit(client, email="q@example.com")
    text2 = json.dumps(receipts.receipt_rows(row(c.execute("SELECT * FROM enquiries WHERE id=?", (e2["id"],)))))
    assert "introductions" not in text2   # not asked/answered → not shown (no "Not provided" nag)


def test_intro_fields_are_outside_every_matching_and_review_input():
    for k in ip.KEYS:
        assert k not in matching.AI_PACKET_FIELDS and k not in matching.EVIDENCE_ENQUIRY_FIELDS
        assert k in enquiries.ANSWER_KEYS and k in enquiries.MEMBER_KEYS and k in enquiries.EDITABLE and k in enquiries.MEMBER_EDITABLE
        assert k not in enquiries.RETIRED_FIELDS and k not in enquiries.RETIRED_MEMBER_FIELDS
    assert {"sex", "dob", "sex_pref", "age_pref"} <= enquiries.RETIRED_FIELDS   # the old collection stays retired
    import inspect
    src = inspect.getsource(rules)
    assert "intro_sex" not in src and "intro_age" not in src
