"""GP9(e) lead takeover vs staff display (neutral review builder, 9 Nov 2026; successor of the frozen probe
/tmp/mmai-c200-probe-takeover-names). When the enquirer is barred and the party stays, the first companion takes over the SAME
enquiries row. Text written before that about the old lead must never be shown under the promoted person's name, and the
promoted person's own earlier words should follow them (same immutable uid) or go neutral when unprovable. Display only:
no packet, selection, saved-map or rule change; no U-codes. Real matching.bar_enquiry route; synthetic SQLite; scripted _ask; no provider.
Terms: "a former member" = proven to have left (retired identity / deleted row); "an earlier member" = cannot be attributed.
r3 (test-only, for the combined tree): the writer (ai_queue._ref_identities) may now save person_uid per ref and _lose_person
invalidates the group's evidence hash on a takeover. Legacy-path assertions therefore use _legacy() (uid stripped) and the
uid-path expectations branch on whether the saved map carries uids. No renderer change."""
import json

from server import identities, matching, staff_view
from server.db import retention_guard, row, rows
from tests.test_t6_grounding import _party_group
from tests.test_uid_allocation import _uid_of

T0 = "2026-11-09T06:00:00+00:00"   # takeover moment used by the legacy-map tests
BEFORE, SAME, AFTER = "2026-11-09T05:59:59+00:00", "2026-11-09T06:00:00+00:00", "2026-11-09T06:00:01+00:00"
R1, R1_AFTER = "2099-01-01T00:00:00+00:00", "2099-01-01T00:00:01+00:00"   # identity resets stamped after any fixture row
R2, R2_AFTER = "2099-01-02T00:00:00+00:00", "2099-01-02T00:00:01+00:00"


def _takeover(db, monkeypatch):
    """The real route: Astrid (enquirer) barred, Bo and Cy stay -> Bo takes over enquiry row 'lead'. Returns the saved map of the
    pre-takeover round exactly as the writer stored it (with person_uid per ref on a uid-aware writer, without on the T4c base)."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    rr = row(db.execute("SELECT refs_json, created_at, evidence_hash FROM review_rounds WHERE group_id=? ORDER BY id DESC LIMIT 1", (g["id"],)))
    before = staff_view.names_from_refs(db, rr["refs_json"], rr["created_at"])
    assert before == {"U1": "Astrid", "U1-m1": "Bo Berg", "U1-m2": "Cy Lund", "U2": "Devi"}
    out = matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    assert out["party_stayed"] and g["id"] in out["awaiting_groups"]
    assert row(db.execute("SELECT first_name FROM enquiries WHERE id=?", (lead["id"],)))["first_name"] == "Bo"
    return lead, solo, g, rr, (astrid, bo, cy, devi)


def _legacy(refs_json):
    """The same saved map as a pre-uid writer would have stored it (person_uid removed) - exercises the timestamp path."""
    m = json.loads(refs_json)
    for ident in m.values():
        ident.pop("person_uid", None)
    return json.dumps(m)


def _saved_uid(refs_json, ref):
    return (json.loads(refs_json).get(ref) or {}).get("person_uid")


def _with_uids(refs_json, **uid_by_ref):
    m = json.loads(refs_json)
    for ref, uid in uid_by_ref.items():
        m[ref]["person_uid"] = uid
    return json.dumps(m)


# ---------------------------------------------------------------- the defect (failing on the G23 tree, see evidence/before-fix.log)
def test_pre_takeover_round_never_shows_the_old_lead_under_the_promoted_persons_name(db, monkeypatch):
    lead, solo, g, rr, (astrid, bo, cy, devi) = _takeover(db, monkeypatch)
    saved_uid_writer = _saved_uid(rr["refs_json"], "U1") is not None
    if saved_uid_writer:
        assert _saved_uid(rr["refs_json"], "U1") == astrid and _saved_uid(rr["refs_json"], "U1-m1") == bo
    # (a) the map as stored: Astrid's words (U1) are never shown under Bo's name
    names = staff_view.names_from_refs(db, rr["refs_json"], rr["created_at"])
    assert names["U1"] in ("a former member", "an earlier member")
    if saved_uid_writer:
        assert names["U1"] == "a former member"          # Astrid's uid retired 'barred' -> proven gone
        assert names["U1-m1"] == "Bo"                     # Bo's own earlier words follow Bo (same live uid, now the lead) - root GO
    else:
        assert names["U1-m1"].endswith("earlier member")  # no uid: Bo's old row is gone and the enquiry had a takeover -> not provable
        assert "Bo" not in json.dumps(names) and "Berg" not in json.dumps(names)
    assert names["U1-m2"] == "Cy Lund" and names["U2"] == "Devi"
    assert staff_view.plain_text("U1 keeps a tidy kitchen.", names) in ("a former member keeps a tidy kitchen.", "an earlier member keeps a tidy kitchen.")
    # (b) the same map on the legacy (no uid) path: the fixture writes the round and bars within the same second -> cannot be ordered
    # -> neutral, never "Bo" (deterministic before/same/after cases are in test_legacy_map_timestamp_order_is_conservative)
    legacy = staff_view.names_from_refs(db, _legacy(rr["refs_json"]), rr["created_at"])
    assert legacy["U1"] in ("a former member", "an earlier member") and legacy["U1-m1"].endswith("earlier member")
    assert legacy["U1-m2"] == "Cy Lund" and legacy["U2"] == "Devi"
    assert "Bo" not in json.dumps(legacy) and "Berg" not in json.dumps(legacy) and "Astrid" not in json.dumps(legacy)
    # (c) the staff brief / rationale built from the stored round: U1 is never Bo or Astrid; Bo may appear only as U1-m1's own name
    gd = dict(row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    bv = staff_view.brief_view(db, g["id"], gd)
    m = {x["ref"]: x["name"] for x in bv["members"]}
    assert m["U1"].endswith("member") and "Astrid" not in json.dumps(bv)
    if saved_uid_writer:
        assert m.get("U1-m1") == "Bo"
    else:
        assert "Bo" not in json.dumps(bv)
    assert "U1" not in json.dumps(bv["sections"])
    rat = staff_view.rationale_for_group(db, gd)
    assert rat is not None and "Astrid" not in rat and "U1" not in rat   # still renders (neutral words), never U-codes
    if not saved_uid_writer:
        assert "Bo" not in rat


def test_no_map_fallback_after_takeover_is_withheld_not_renamed(db, monkeypatch):
    lead, solo, g, rr, _ = _takeover(db, monkeypatch)
    gh = row(db.execute("SELECT evidence_hash FROM groups WHERE id=?", (g["id"],)))["evidence_hash"]
    # writer behaviour differs by tree: the T4c base left the hash unchanged (probe finding - the hash alone would let today's
    # members through); the combined tree's _lose_person invalidates it (evidence_hash NULL, root GO). Either way the renderer
    # must withhold a no-map fallback for text written before (or not provably after) the takeover.
    assert gh is None or gh == rr["evidence_hash"]
    fb = staff_view.ref_names(db, g["id"], rr["evidence_hash"], None, rr["created_at"])
    assert isinstance(fb, staff_view.EarlierMembers)
    assert isinstance(staff_view.ref_names(db, g["id"], rr["evidence_hash"], None, None), staff_view.EarlierMembers)   # no timestamp: withheld
    assert isinstance(staff_view.ref_names(db, g["id"], None, None, rr["created_at"]), staff_view.EarlierMembers)      # no hash check either: still withheld
    assert isinstance(staff_view.ref_names(db, g["id"], None, None, None), staff_view.EarlierMembers)
    # a round provably written AFTER the takeover may use today's members (hash check skipped: the group hash may have been invalidated)
    later = staff_view.ref_names(db, g["id"], None, None, "2099-01-01T00:00:00+00:00")
    assert not isinstance(later, staff_view.EarlierMembers) and later["U1"] == "Bo"
    if gh is not None:   # unchanged-hash writer: the hash-checked variant must agree
        later_h = staff_view.ref_names(db, g["id"], rr["evidence_hash"], None, "2099-01-01T00:00:00+00:00")
        assert not isinstance(later_h, staff_view.EarlierMembers) and later_h["U1"] == "Bo"


# ---------------------------------------------------------------- saved uid (writer-side addition the lead is adding)
def test_saved_uids_follow_the_same_human_through_the_promotion(db, monkeypatch):
    lead, solo, g, rr, (astrid, bo, cy, devi) = _takeover(db, monkeypatch)
    m = _with_uids(rr["refs_json"], U1=astrid, **{"U1-m1": bo, "U1-m2": cy}, U2=devi)
    names = staff_view.names_from_refs(db, m, rr["created_at"])
    assert names == {"U1": "a former member", "U1-m1": "Bo", "U1-m2": "Cy Lund", "U2": "Devi"}   # Bo's own words follow Bo, now the lead
    assert staff_view.plain_text("U1 keeps a tidy kitchen; U1-m1 has no own answers.", names) == "a former member keeps a tidy kitchen; Bo has no own answers."
    # the uid wins over the row ids even without a timestamp, and even if the stored ids are stale
    assert staff_view.names_from_refs(db, m, None)["U1"] == "a former member"
    stale = json.loads(m); stale["U1-m1"]["party_member_id"] = 999999
    assert staff_view.names_from_refs(db, json.dumps(stale), None)["U1-m1"] == "Bo"


def test_saved_uid_edge_cases_unknown_deleted_reset(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    rr = row(db.execute("SELECT refs_json, created_at FROM review_rounds WHERE group_id=? ORDER BY id DESC LIMIT 1", (g["id"],)))
    astrid, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    # unknown uid -> neutral (nothing proven either way; never the row's current name)
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], U1="no-such-uid"), rr["created_at"])["U1"] == "an earlier member"
    # identity reset on the SAME row (name AND email changed: identities.reconcile's conservative reset may be a replacement, not a
    # rename) -> the old uid is neutralised; succession (superseded_by_uid) is never followed as proof of the same human
    with retention_guard(db, label="reset"):
        db.execute("UPDATE enquiries SET first_name='Zed', last_name='Other', email='zed@example.com' WHERE id=?", (lead["id"],))
        identities.reconcile(db, lead["id"], "left", now=R1)
    old = row(db.execute("SELECT retired_reason, superseded_by_uid FROM people_identities WHERE person_uid=?", (astrid,)))
    assert old["retired_reason"] == "identity_edit" and old["superseded_by_uid"]
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], U1=astrid), rr["created_at"])["U1"] == "an earlier member"
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], U1=astrid), "2099-01-01T00:00:00+00:00")["U1"] == "an earlier member"   # uid path ignores timestamps
    # legacy path (uid stripped): the reset is a change event like a takeover - before/unknown -> neutral or former, after -> current name
    leg = _legacy(rr["refs_json"])
    assert staff_view.names_from_refs(db, leg, rr["created_at"])["U1"] == "a former member"   # written before the reset
    assert staff_view.names_from_refs(db, leg, None)["U1"] == "an earlier member"
    assert staff_view.names_from_refs(db, leg, R1_AFTER)["U1"] == "Zed"
    assert "Zed" not in json.dumps(list(staff_view.names_from_refs(db, leg, rr["created_at"]).values()))
    if _saved_uid(rr["refs_json"], "U1"):   # uid-aware writer: the saved (now retired by identity_edit) uid stays neutral whatever the timestamp
        assert staff_view.names_from_refs(db, rr["refs_json"], R1_AFTER)["U1"] == "an earlier member"
    # the new uid resolves to the row as it is now
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], U1=old["superseded_by_uid"]), None)["U1"] == "Zed"
    # a party row reset (same row id, different person details) is a change event for THAT slot only
    cy_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Cy Lund'", (lead["id"],)))["id"]
    with retention_guard(db, label="party reset"):
        db.execute("UPDATE party_members SET name='Quinn New', email='quinn@example.com' WHERE id=?", (cy_pm,))
        identities.reconcile(db, lead["id"], "left", now=R2)
    leg = _legacy(rr["refs_json"])
    n = staff_view.names_from_refs(db, leg, R1_AFTER)   # after the lead reset, before the party reset
    assert n["U1-m2"] == "an earlier member" and n["U1"] == "Zed" and n["U1-m1"] == "Bo Berg"
    assert staff_view.names_from_refs(db, leg, R2_AFTER)["U1-m2"] == "Quinn New"
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], **{"U1-m2": cy}), None)["U1-m2"].endswith("earlier member")   # retired by the reset: neutral
    # deleted enquiry -> former member on both paths
    db.execute("DELETE FROM enquiries WHERE id=?", (solo["id"],)); db.commit()
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], U2=devi), rr["created_at"])["U2"].endswith("former member")   # uid cascaded away with the row: proven gone
    assert staff_view.names_from_refs(db, _legacy(rr["refs_json"]), rr["created_at"])["U2"].endswith("former member")   # legacy: row gone
    # a companion who left (member_left retires the uid as 'left') -> former member
    bo_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))["id"]
    bo = _uid_of(db, lead["id"], "Bo Berg")
    with retention_guard(db, label="leave"):
        identities.member_left(db, lead["id"], bo_pm, "left")
        db.execute("DELETE FROM party_members WHERE id=?", (bo_pm,))
    assert staff_view.names_from_refs(db, _with_uids(rr["refs_json"], **{"U1-m1": bo}), rr["created_at"])["U1-m1"].endswith("former member")


def test_timestamp_order_uses_validated_utc_datetimes_not_strings(db):
    wa = staff_view._written_after
    assert wa("2026-11-09T06:00:01+00:00", "2026-11-09T06:00:00+00:00") is True
    assert wa("2026-11-09T05:59:59+00:00", "2026-11-09T06:00:00+00:00") is False
    assert wa("2026-11-09T06:00:00+00:00", "2026-11-09T06:00:00+00:00") is None            # same second
    assert wa("2026-11-09T06:00:00.900+00:00", "2026-11-09T06:00:00+00:00") is None        # sub-second difference is not an order
    assert wa("2026-11-09T17:00:01+11:00", "2026-11-09T06:00:00+00:00") is True            # same instant basis across offsets
    assert wa("2026-11-09T17:00:00+11:00", "2026-11-09T06:00:00+00:00") is None            # equal instants in different offsets
    assert wa("2026-11-09T06:00:01Z", "2026-11-09T06:00:00+00:00") is True
    assert wa("2026-11-09T06:00:01", "2026-11-09T06:00:00+00:00") is True                  # naive = UTC (db.now_iso writes UTC)
    for bad in (None, "", "   ", "garbage", "2026-13-45T00:00:00+00:00", "9", 12345):
        assert wa(bad, "2026-11-09T06:00:00+00:00") is None and wa("2026-11-09T06:00:01+00:00", bad) is None
    # lexical order would say otherwise for a differently-formatted later stamp
    assert wa("2026-11-09 06:00:01+00:00", "2026-11-09T06:00:00+00:00") is True


def test_duplicate_names_widen_only_through_resolved_slots(db, monkeypatch):
    """Two 'Mateo' leads; a saved map whose row ids are stale/conflicting but whose uids resolve. Widening must read the slots the
    uids resolved to, never the saved ids - and a neutral label is never widened."""
    from tests.test_launch_neutral import _enq
    a, lead, solo, g = _party_group(db, monkeypatch)
    m1 = _enq(db, "Mateo", "m1@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    m2 = _enq(db, "Mateo", "m2@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
    db.execute("UPDATE enquiries SET last_name='Rossi' WHERE id=?", (m1["id"],)); db.execute("UPDATE enquiries SET last_name='Silva' WHERE id=?", (m2["id"],)); db.commit()
    with retention_guard(db, label="ids"):
        identities.reconcile(db, m1["id"], "left"); identities.reconcile(db, m2["id"], "left")
    u1, u2 = _uid_of(db, m1["id"]), _uid_of(db, m2["id"])
    # saved ids point at the WRONG rows (swapped / stale) while the uids are right
    refs = json.dumps({"U1": {"enquiry_id": m2["id"], "party_member_id": None, "person_uid": u1},
                       "U2": {"enquiry_id": solo["id"], "party_member_id": None, "person_uid": u2}})
    assert staff_view.names_from_refs(db, refs, None) == {"U1": "Mateo Rossi", "U2": "Mateo Silva"}
    # one of the two uids retired -> neutral, and the remaining single 'Mateo' is not widened (no collision)
    with retention_guard(db, label="bar"):
        db.execute("UPDATE people_identities SET retired_at='2026-11-09T06:50:00+00:00', retired_reason='barred' WHERE person_uid=?", (u2,))
    assert staff_view.names_from_refs(db, refs, None) == {"U1": "Mateo", "U2": "a former member"}
    # legacy map (no uid) with a collision: widening reads the saved slot only because it was proven unchanged
    legacy = json.dumps({"U1": {"enquiry_id": m1["id"], "party_member_id": None}, "U2": {"enquiry_id": m2["id"], "party_member_id": None}})
    assert staff_view.names_from_refs(db, legacy, None) == {"U1": "Mateo", "U2": "an earlier member"}   # m2's lead slot changed (retired) -> neutral; no collision left, nothing widened
    # two neutral labels never widen into anyone's full name
    neutral = json.dumps({"U1": {"enquiry_id": m2["id"], "party_member_id": None}, "U2": {"enquiry_id": m2["id"], "party_member_id": None}})
    assert staff_view.names_from_refs(db, neutral, None) == {"U1": "an earlier member", "U2": "another earlier member"}


# ---------------------------------------------------------------- legacy map (no uid) ordered against the takeover timestamp
def test_legacy_map_timestamp_order_is_conservative(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    rr = row(db.execute("SELECT refs_json FROM review_rounds WHERE group_id=? ORDER BY id DESC LIMIT 1", (g["id"],)))
    saved = rr["refs_json"]
    rr = {"refs_json": _legacy(saved)}   # legacy (no uid) map: this test is about timestamp ordering only
    bo_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))["id"]
    with retention_guard(db, label="takeover at a known second"):
        identities.promote_to_lead(db, lead["id"], bo_pm, "barred", now=T0)
        db.execute("UPDATE enquiries SET first_name='Bo', last_name='Berg', email='bo@example.com' WHERE id=?", (lead["id"],))
        db.execute("DELETE FROM party_members WHERE id=?", (bo_pm,))
        identities.reconcile(db, lead["id"], "barred", now=T0)
    assert staff_view.names_from_refs(db, rr["refs_json"], BEFORE)["U1"] == "a former member"   # written before the takeover
    assert staff_view.names_from_refs(db, rr["refs_json"], SAME)["U1"] == "an earlier member"   # same second: not provable
    assert staff_view.names_from_refs(db, rr["refs_json"], None)["U1"] == "an earlier member"   # no timestamp with takeover history
    assert staff_view.names_from_refs(db, rr["refs_json"], AFTER)["U1"] == "Bo"                 # written after: today's lead is right
    if _saved_uid(saved, "U1"):   # uid-aware writer: uids answer regardless of timestamp (Astrid retired 'barred' -> former; Bo live -> Bo)
        for when in (BEFORE, SAME, None, AFTER):
            nu = staff_view.names_from_refs(db, saved, when)
            assert nu["U1"] == "a former member" and nu["U1-m1"] == "Bo" and nu["U1-m2"] == "Cy Lund" and nu["U2"] == "Devi"
    for when in (BEFORE, SAME, None, AFTER):
        n = staff_view.names_from_refs(db, rr["refs_json"], when)
        assert n["U1-m1"].endswith("earlier member") and n["U1-m2"] == "Cy Lund" and n["U2"] == "Devi"   # Bo's old row: neutral, never Cy
        assert "U1" not in json.dumps(list(n.values()))
    # two takeovers: the latest one is the bar
    cy_pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Cy Lund'", (lead["id"],)))["id"]
    with retention_guard(db, label="second takeover"):
        identities.promote_to_lead(db, lead["id"], cy_pm, "barred", now="2026-11-09T07:00:00+00:00")
        db.execute("UPDATE enquiries SET first_name='Cy', last_name='Lund', email='cy@example.com' WHERE id=?", (lead["id"],))
        db.execute("DELETE FROM party_members WHERE id=?", (cy_pm,))
        identities.reconcile(db, lead["id"], "barred", now="2026-11-09T07:00:00+00:00")
    assert staff_view.names_from_refs(db, rr["refs_json"], AFTER)["U1"] == "a former member"    # after the first, before the second
    assert staff_view.names_from_refs(db, rr["refs_json"], "2026-11-09T07:00:01+00:00")["U1"] == "Cy"


def test_source_selection_ties_and_untouched_groups(db, monkeypatch):
    """A group whose rows never changed person renders exactly as before (names, duplicates, rationale source selection)."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    gd = dict(row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    rr = row(db.execute("SELECT refs_json, created_at FROM review_rounds WHERE group_id=? ORDER BY id DESC LIMIT 1", (g["id"],)))
    assert staff_view.names_from_refs(db, rr["refs_json"], rr["created_at"]) == {"U1": "Astrid", "U1-m1": "Bo Berg", "U1-m2": "Cy Lund", "U2": "Devi"}
    assert staff_view.names_from_refs(db, rr["refs_json"]) == staff_view.names_from_refs(db, rr["refs_json"], None)
    bv = staff_view.brief_view(db, g["id"], gd)
    assert bv["current"] and {x["ref"]: x["name"] for x in bv["members"]}["U1"] == "Astrid"
    assert staff_view.rationale_for_group(db, gd) == "fine"
    # a tie between two same-worded rounds with DIFFERENT maps still resolves to neutral words (unchanged _source_round rule)
    other = json.dumps({"U1": {"enquiry_id": solo["id"], "party_member_id": None}, "U2": {"enquiry_id": lead["id"], "party_member_id": None}})
    db.execute("INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at, refs_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (g["id"], 7, "approve", 8.0, "U1 and U2 both like quiet", json.dumps({"findings": []}), 1, gd["evidence_hash"], gd.get("ai_assessed_at") or rr["created_at"], other))
    db.execute("INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at, refs_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
               (g["id"], 8, "approve", 8.0, "U1 and U2 both like quiet", json.dumps({"findings": []}), 1, gd["evidence_hash"], gd.get("ai_assessed_at") or rr["created_at"], rr["refs_json"]))
    db.execute("UPDATE groups SET ai_rationale='U1 and U2 both like quiet' WHERE id=?", (g["id"],)); db.commit()
    gd = dict(row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    txt = staff_view.rationale_for_group(db, gd)
    assert txt == "one member and another member both like quiet"
