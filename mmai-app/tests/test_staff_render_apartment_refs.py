"""Staff-render repair (9 Nov 2026, neutral review builder; proposal, not integrated): two T10 presentation defects seen on the
G2 work3 snapshot.

(1) group_apartments.ai_reason reached the staff card as raw AI prose ("U3's 537.5 share exceeds their 483 maximum"): the
    payload now puts it into plain words through the SAVED review round that wrote it (its immutable refs map and apartment
    basis). Stale membership -> "a former member"; a ref the map never had -> neutral words; no matching round -> neutral
    words and only the group's present apartments named. Today's identities are never assigned to older text.
(2) plain_text turned a real apartment reference into "the agreed requirements" whenever a weak cue preceded it ("In A54",
    "cheaper in A50") or the apartment had left the group's list. A bare A<id> that is not a register code is always an
    apartment: the address when it is in the text's basis, otherwise an explicit neutral phrase. Register codes A4-A17 keep
    their rule reading under strong cues (under, per, rule, see, clause, breach, violat, an opening bracket) and sub-letters.
"""
import json
import re

from server import staff_view as sv
from server.db import now_iso, row
from tests.test_data_and_api import appdb, client, good_answers, member  # noqa: F401  (fixtures re-used)
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _add_member, _brief, _review, _submit

RAW_REF = re.compile(r"(?<![A-Za-z0-9_-])(?:[UI]\d+(?:-m\d+)?|R\.A\d+|A\d+)(?![A-Za-z0-9_-])")
NAMES = {"U1": "Elin", "U2": "Hana", "U3": "Kenji"}
G2_APTS = {"A50": "Unit 3308 / 80 A'Beckett Street", "A54": "Unit 1015 / 218-228 A'Beckett Street"}
# verbatim captured texts from the G2 work3 snapshot (review_rounds id 8 / group_briefs id 2)
G2_TENSION = "In A54 U3's proposed share of 537.5 weekly is above their stated maximum of 483, though the household total rent of 1075 passes the household rule."
G2_RATIONALE = ("Tidiness and social styles are compatible, dates align exactly, and room preferences match the allocation. Shares are cheaper in A50 and within "
                "every stated maximum except U2's guide share exceeding nothing, while A54's U3 share exceeds their stated maximum.")
G2_REASON_54 = "Rent 1075 is within household limit but U3's 537.5 share exceeds their 483 maximum."


# ---------------------------------------------------------------- unit: plain_text apartment references


def test_g2_snapshot_texts_name_the_apartment_not_the_agreed_requirements():
    out = sv.plain_text(G2_TENSION, NAMES, G2_APTS)
    assert out == ("In Unit 1015 / 218-228 A'Beckett Street Kenji's proposed share of 537.5 weekly is above their stated maximum of 483, "
                   "though the household total rent of 1075 passes the household rule.")
    rat = sv.plain_text(G2_RATIONALE, NAMES, G2_APTS)
    assert "cheaper in Unit 3308 / 80 A'Beckett Street" in rat and "Unit 1015 / 218-228 A'Beckett Street's Kenji share" in rat
    assert "agreed requirements" not in out + rat and not RAW_REF.search(out + rat)
    assert sv.plain_text(G2_REASON_54, NAMES, G2_APTS) == "Rent 1075 is within household limit but Kenji's 537.5 share exceeds their 483 maximum."


def test_non_code_apartment_outside_the_basis_is_an_explicit_neutral_phrase_never_a_rule_label():
    # apartment 54 has left the group's list and is not in the saved basis: say so, do not call it a requirement
    out = sv.plain_text(G2_TENSION, NAMES, {"A50": G2_APTS["A50"]})
    assert out.startswith(f"In {sv.APT_UNKNOWN} Kenji's proposed share")
    assert "agreed requirements" not in out and not RAW_REF.search(out)
    assert sv.plain_text("A99 rent is high", {}, {}) == f"{sv.APT_UNKNOWN} rent is high"


def test_register_codes_keep_their_rule_reading_and_weak_cues_keep_the_address():
    apts = {"A16": "16 Smith St", "A12": "12 Smith St"}
    names = {"U1": "Oscar", "U2": "Nia", "U1-m1": "Priya"}
    # unchanged behaviour (existing T10 expectations): strong cues, brackets, sub-letters, no such apartment
    assert sv.plain_text("the rent is negotiable under A16 only if U1-m1 accepts a single bed.", names, apts) == \
        "the rent is negotiable under the total-rent allowance only if Priya accepts a single bed."
    assert sv.plain_text("see A16(a); budget (A16) ok; A16 suits U2", names, apts) == "see the total-rent allowance; budget (the total-rent allowance) ok; 16 Smith St suits Nia"
    assert sv.plain_text("Fails GP99 and A16.", names, {}) == "Fails the agreed requirements and the total-rent allowance."
    assert sv.plain_text("A9 and A10 pass", names, apts) == "the minimum lease period and the lease cycle and accepted exit windows pass"
    # repaired: a weak cue before an apartment that IS in the basis keeps the address (register code or not)
    assert sv.plain_text("Shares are cheaper in A12 than in A16; within A16 the split works", names, apts) == \
        "Shares are cheaper in 12 Smith St than in 16 Smith St; within 16 Smith St the split works"
    assert sv.plain_text("In A54 the split works", names, {"A54": "54 High St"}) == "In 54 High St the split works"
    # a register code quoted under a weak cue with NO such apartment is still the rule
    assert sv.plain_text("within A16 the rent fits", names, {}) == "within the total-rent allowance the rent fits"


# ---------------------------------------------------------------- unit: the saved apartment basis


def _apartment(c, address):
    c.execute("""INSERT INTO apartments(city, address, address_raw, suburb, type_code, bathrooms, rent_12m, rent_6m, available_from, min_lease_months,
                                        bedroom1, bedroom2, bedroom3, bills_included, updated_at)
                 VALUES ('Melbourne', ?, ?, 'Melbourne', '2B1B', 1, 940.0, 1000.0, '2027-01-19', 6, '1 king', '2 singles', NULL, 0, ?)""", (address, address, now_iso()))
    return c.execute("SELECT id FROM apartments WHERE address=?", (address,)).fetchone()[0]


def _rank(c, gid, aid, rank, reason):
    c.execute("INSERT OR REPLACE INTO group_apartments(group_id, apartment_id, rank, rule_score, ai_reason, total_weekly, allocation_json) VALUES (?,?,?,70,?,940,'{}')",
              (gid, aid, rank, reason))


def _round(c, gid, rationale, reasons: dict, refs, h="h1", valid=1, at=None):
    """A review round as ai_queue writes it: rationale + ranked apartments with reasons in review_json, immutable refs map."""
    rj = {"verdict": "approve", "rating": 7.0, "rationale": rationale, "apartments": [{"apartment_id": a, "reason": t} for a, t in reasons.items()], "findings": []}
    at = at or now_iso()
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at, refs_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?)""", (gid, 0, "approve", 7.0, rationale, json.dumps(rj), valid, h, at, json.dumps(refs) if refs else None))
    return at


def _plant(client):
    """G2-shaped group: three leads, two apartments, one valid review round that wrote the rationale and both apartment reasons."""
    e1 = _submit(client, first_name="Elin", last_name="One", email="elin@example.com")
    e2 = _submit(client, first_name="Hana", last_name="Two", email="hana@example.com")
    e3 = _submit(client, first_name="Kenji", last_name="Three", email="kenji@example.com")
    c = appdb(client)
    gid = _plant_group(c, e1["id"])
    _add_member(c, gid, e2["id"]); _add_member(c, gid, e3["id"])
    a50 = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))["apartment_id"]
    a54 = _apartment(c, "Unit 1015 / 218-228 A'Beckett Street")
    reasons = {a50: "Rent 940; all shares (470, 235, 235) are within stated maximums 483, 483, 348; better value.", a54: G2_REASON_54}
    _rank(c, gid, a50, 1, reasons[a50]); _rank(c, gid, a54, 2, reasons[a54])
    rationale = G2_RATIONALE.replace("A50", f"A{a50}").replace("A54", f"A{a54}")
    refs = {"U1": {"enquiry_id": e1["id"], "party_member_id": None}, "U2": {"enquiry_id": e2["id"], "party_member_id": None}, "U3": {"enquiry_id": e3["id"], "party_member_id": None}}
    at = _round(c, gid, rationale, reasons, refs, at="2026-11-09T05:00:00+00:00")
    # the applied markers ai_queue.apply_review persists in the same write: ai_assessed_at = the round's created_at, the rationale, the hash
    c.execute("UPDATE groups SET evidence_hash='h1', ai_status='ok', review_verdict='approve', ai_rationale=?, ai_rating=7, ai_assessed_at=? WHERE id=?", (rationale, at, gid))
    c.commit()
    return c, gid, (e1, e2, e3), (a50, a54), rationale


def test_valid_but_discarded_round_with_the_same_words_never_lends_its_refs(client):
    """apply_review records a round valid=1 BEFORE it rejects stale evidence, so a later valid row can exist that was never applied.
    Probe: Kenji (U3) is replaced by Noor; the discarded re-review has the SAME apartment reasons and rationale text but refs
    U3 -> Noor. The card must attribute the stored reasons through the APPLIED round (Kenji), or stay neutral - never Noor."""
    c, gid, (e1, e2, e3), (a50, a54), rationale = _plant(client)
    n = _submit(client, first_name="Noor", last_name="New", email="noor@example.com")
    c.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, e3["id"])); _add_member(c, gid, n["id"])
    reasons = {a50: "Rent 940; all shares (470, 235, 235) are within stated maximums 483, 483, 348; better value.", a54: G2_REASON_54}
    stale_refs = {"U1": {"enquiry_id": e1["id"], "party_member_id": None}, "U2": {"enquiry_id": e2["id"], "party_member_id": None}, "U3": {"enquiry_id": n["id"], "party_member_id": None}}
    _round(c, gid, rationale, reasons, stale_refs, h="h2", at="2026-11-09T06:00:00+00:00")   # recorded valid, discarded: facts moved on to h3
    c.execute("UPDATE groups SET evidence_hash='h3' WHERE id=?", (gid,)); c.commit()
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[1]["ai_reason"] == "Rent 1075 is within household limit but Kenji's 537.5 share exceeds their 483 maximum."   # the applied round (ai_assessed_at marker)
    assert "Noor" not in apts[1]["ai_reason"] and "Noor" not in sv.rationale_for_group(c, g) and "Kenji share" in sv.rationale_for_group(c, g)
    # no persisted applied marker at all (legacy row): neutral words, still never Noor
    c.execute("UPDATE groups SET ai_assessed_at=NULL WHERE id=?", (gid,)); c.commit()
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[1]["ai_reason"] == "Rent 1075 is within household limit but one member's 537.5 share exceeds their 483 maximum."
    assert "Noor" not in sv.rationale_for_group(c, g) and "one member" in sv.rationale_for_group(c, g)
    # a marker that matches neither round (both created after the last apply): nothing applied matches, neutral, never Noor
    c.execute("UPDATE groups SET ai_assessed_at='2026-11-09T04:00:00+00:00' WHERE id=?", (gid,)); c.commit()
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert "one member's 537.5 share" in apts[1]["ai_reason"] and "Noor" not in apts[1]["ai_reason"] and "Kenji" not in apts[1]["ai_reason"]
    assert not any(nm in sv.rationale_for_group(c, g) for nm in ("Noor", "Kenji", "Hana"))
    # same-second tie (db.now_iso has no microseconds): a discarded round created in the SAME second as the applied one, with
    # different refs, makes the marker ambiguous -> neutral, never Noor and never a guess at Kenji
    c.execute("UPDATE review_rounds SET created_at='2026-11-09T05:00:00+00:00' WHERE group_id=? AND created_at='2026-11-09T06:00:00+00:00'", (gid,))
    c.execute("UPDATE groups SET ai_assessed_at='2026-11-09T05:00:00+00:00' WHERE id=?", (gid,)); c.commit()
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert "one member's 537.5 share" in apts[1]["ai_reason"] and not any(nm in apts[1]["ai_reason"] for nm in ("Noor", "Kenji"))
    assert not any(nm in sv.rationale_for_group(c, g) for nm in ("Noor", "Kenji", "Hana"))
    # same-second tie with the SAME refs map (a duplicate write of one reply) is unambiguous: Kenji
    kenji_refs = row(c.execute("SELECT refs_json FROM review_rounds WHERE group_id=? ORDER BY id LIMIT 1", (gid,)))["refs_json"]
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=?", (kenji_refs, gid)); c.commit()
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert "Kenji's 537.5 share" in apts[1]["ai_reason"] and "Kenji share" in sv.rationale_for_group(c, g)
    c.execute("UPDATE review_rounds SET refs_json=?, created_at='2026-11-09T06:00:00+00:00' WHERE group_id=? AND id=(SELECT MAX(id) FROM review_rounds WHERE group_id=?)", (json.dumps(stale_refs), gid, gid)); c.commit()
    # the discarded round alone (the applied one lost its saved reasons, e.g. a legacy row) is still not a source: it postdates the apply
    c.execute("UPDATE groups SET ai_assessed_at='2026-11-09T05:00:00+00:00' WHERE id=?", (gid,))
    c.execute("UPDATE review_rounds SET review_json='{}' WHERE group_id=? AND created_at='2026-11-09T05:00:00+00:00'", (gid,)); c.commit()
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert "one member's 537.5 share" in apts[1]["ai_reason"] and "Noor" not in apts[1]["ai_reason"]


def test_apartment_names_adds_the_saved_basis_and_skips_deleted_apartments(client):
    c, gid, _, (a50, a54), _ = _plant(client)
    c.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, a54)); c.commit()   # apartment left the group's list
    assert f"A{a54}" not in sv.apartment_names(c, gid)
    assert sv.apartment_names(c, gid, [a54])[f"A{a54}"] == "Unit 1015 / 218-228 A'Beckett Street"       # identity kept through the basis
    c.execute("DELETE FROM apartments WHERE id=?", (a54,)); c.commit()
    assert f"A{a54}" not in sv.apartment_names(c, gid, [a54])                                             # gone: neutral fallback applies
    assert sv.basis_apartment_ids(json.dumps({"apartments": [{"apartment_id": a50, "reason": "x"}, {"apartment_id": "7"}, "junk"]})) == [a50]
    assert sv.basis_apartment_ids(None) == [] and sv.basis_apartment_ids("not json") == []


# ---------------------------------------------------------------- apartment reasons through their own basis


def test_apartment_reasons_use_the_rounds_own_map_and_apartment_basis(client):
    c, gid, (e1, e2, e3), (a50, a54), rationale = _plant(client)
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[1]["ai_reason"] == "Rent 1075 is within household limit but Kenji's 537.5 share exceeds their 483 maximum."
    assert apts[0]["ai_reason"].startswith("Rent 940; all shares")
    assert sv.rationale_for_group(c, g) == ("Tidiness and social styles are compatible, dates align exactly, and room preferences match the allocation. Shares are cheaper in "
                                            "Unit 9 / 9 Intro Street and within every stated maximum except Hana's guide share exceeding nothing, while "
                                            "Unit 1015 / 218-228 A'Beckett Street's Kenji share exceeds their stated maximum.")
    # the apartment leaves the group's list but stays in the saved basis: its identity is preserved in the rationale
    c.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, a54)); c.commit()
    assert "Unit 1015 / 218-228 A'Beckett Street's Kenji share" in sv.rationale_for_group(c, g)
    # deleted outright: explicit neutral phrase, never a rule label
    c.execute("DELETE FROM apartments WHERE id=?", (a54,)); c.commit()
    out = sv.rationale_for_group(c, g)
    assert f"{sv.APT_UNKNOWN}'s Kenji share" in out and "agreed requirements" not in out


def test_stale_membership_retired_ref_and_legacy_rows_never_borrow_todays_names(client):
    c, gid, (e1, e2, e3), (a50, a54), rationale = _plant(client)
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    # (a) stale membership: U3 (Kenji) was deleted and Noor joined; the reasons still describe the old set
    n = _submit(client, first_name="Noor", last_name="New", email="noor@example.com")
    c.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, e3["id"]))
    c.execute("DELETE FROM enquiries WHERE id=?", (e3["id"],))
    _add_member(c, gid, n["id"]); c.commit()
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[1]["ai_reason"] == "Rent 1075 is within household limit but a former member's 537.5 share exceeds their 483 maximum."
    assert "Noor" not in apts[1]["ai_reason"]
    # (b) a retired ref the saved map never had (U4) is neutral, not anyone in the group today
    c.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=? AND apartment_id=?", ("U4 pays the most; U1 the least", gid, a50))
    rj = json.loads(row(c.execute("SELECT review_json FROM review_rounds WHERE group_id=?", (gid,)))["review_json"])
    rj["apartments"][0]["reason"] = "U4 pays the most; U1 the least"
    c.execute("UPDATE review_rounds SET review_json=? WHERE group_id=?", (json.dumps(rj), gid)); c.commit()
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[0]["ai_reason"] == "one member pays the most; Elin the least"
    # (c) no round matches the stored reasons (legacy / overwritten): neutral words, no raw refs, no present-day names
    c.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=? AND apartment_id=?", ("U1 and U2 both fit A%d well" % a54, gid, a54)); c.commit()
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    legacy = apts[1]["ai_reason"]
    # one neutral labelling is shared across the card's reasons (U4 was met first in the rank-1 reason), so labels stay consistent
    assert apts[0]["ai_reason"] == "one member pays the most; another member the least"
    assert legacy == "another member and a third member both fit Unit 1015 / 218-228 A'Beckett Street well"
    assert not RAW_REF.search(legacy) and not any(nm in legacy for nm in ("Elin", "Hana", "Noor"))
    # (d) a round that matches by evidence hash but has no refs map: names only while the group's evidence is still that hash
    c.execute("UPDATE review_rounds SET refs_json=NULL WHERE group_id=?", (gid,))
    c.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=? AND apartment_id=?", ("U4 pays the most; U1 the least", gid, a50))
    c.execute("UPDATE groups SET evidence_hash='h2' WHERE id=?", (gid,)); c.commit()          # facts moved on: the round is history
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[0]["ai_reason"] == "one member pays the most; another member the least"


def test_apartment_reasons_do_not_collide_with_register_codes(client):
    c, gid, (e1, e2, e3), (a50, a54), rationale = _plant(client)
    g = dict(row(c.execute("SELECT * FROM groups WHERE id=?", (gid,))))
    text = f"Rent fits under A16 and within A{a54}; A{a50} suits U1; see GP8"
    c.execute("UPDATE group_apartments SET ai_reason=? WHERE group_id=? AND apartment_id=?", (text, gid, a54))
    rj = json.loads(row(c.execute("SELECT review_json FROM review_rounds WHERE group_id=?", (gid,)))["review_json"])
    rj["apartments"][1]["reason"] = text
    c.execute("UPDATE review_rounds SET review_json=? WHERE group_id=?", (json.dumps(rj), gid)); c.commit()
    apts = [dict(r) for r in c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))]
    sv.apartment_reasons(c, g, apts)
    assert apts[1]["ai_reason"] == ("Rent fits under the total-rent allowance and within Unit 1015 / 218-228 A'Beckett Street; Unit 9 / 9 Intro Street suits Elin; "
                                    "see no holding of apartments")
    assert not re.search(r"\b(?:GP|[ABT])\d+\b", apts[1]["ai_reason"])


# ---------------------------------------------------------------- API: the staff payload carries the plain reason


def test_group_payloads_carry_plain_apartment_reasons(client):
    c, gid, (e1, e2, e3), (a50, a54), rationale = _plant(client)
    d = client.get(f"/api/groups/{gid}").json()
    by_id = {a["apartment_id"]: a for a in d["apartments"]}
    assert by_id[a54]["ai_reason"] == "Rent 1075 is within household limit but Kenji's 537.5 share exceeds their 483 maximum."
    assert "Unit 1015 / 218-228 A'Beckett Street's Kenji share" in d["ai_rationale"] and "agreed requirements" not in d["ai_rationale"]
    assert not any(RAW_REF.search(a["ai_reason"] or "") for a in d["apartments"])
    lst = client.get("/api/groups", params={"dataset": "real"}).json()["items"] + client.get("/api/groups", params={"dataset": "test"}).json()["items"]
    item = next(x for x in lst if x["id"] == gid)
    assert {a["apartment_id"]: a["ai_reason"] for a in item["apartments"]} == {a["apartment_id"]: a["ai_reason"] for a in d["apartments"]}
    # the stored row is untouched: presentation only
    assert row(c.execute("SELECT ai_reason FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, a54)))["ai_reason"] == G2_REASON_54
