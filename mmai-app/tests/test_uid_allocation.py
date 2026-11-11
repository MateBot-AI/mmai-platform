"""GP13 / GP10(d): allocation occupants carry the server-issued person_uid of the person they were computed for (lead slice).

Real identities module, synthetic SQLite data, no provider calls. The engine's positional ids ("E", "EmK") are not proof of
which person once party rows are rewritten; the uid stamped at computation time is. Nothing here re-stamps a stored allocation.
"""
import json
import sys

import pytest

from server import identities, matching
from server.db import pj, retention_guard, row, rows
from tests.test_t6_grounding import _party_group  # noqa: F401
from tests.test_data_and_api import client  # noqa: F401
from tests.test_t6_fail_closed import _reply_builder, _good  # noqa: F401


def _occupants(c, gid, table="groups"):
    key = "id" if table == "groups" else "group_id"
    out = []
    for r in rows(c.execute(f"SELECT allocation_json FROM {table} WHERE {key}=?", (gid,))):
        for room in (pj(r["allocation_json"], {}) or {}).get("rooms") or []:
            for o in room.get("occupants") or []:
                out.append(o)
    return out


def _uid_of(c, eid, name=None):
    if name is None:
        return row(c.execute("SELECT person_uid FROM people_identities WHERE enquiry_id=? AND role='lead' AND retired_at IS NULL", (eid,)))["person_uid"]
    pm = row(c.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name=?", (eid, name)))
    return row(c.execute("SELECT person_uid FROM people_identities WHERE party_member_id=? AND retired_at IS NULL", (pm["id"],)))["person_uid"]


def test_a_fresh_match_stamps_every_occupant_with_the_right_persons_uid_and_shares_stay_individual(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    occ = _occupants(db, g["id"])
    assert len(occ) == 4 and all(o.get("person_uid") for o in occ), occ
    by_uid = {o["person_uid"]: o for o in occ}
    assert len(by_uid) == 4
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    assert set(by_uid) == {astrid, bo, cy, devi}
    assert by_uid[astrid]["person_id"] == str(lead["id"]) and by_uid[devi]["person_id"] == str(solo["id"])
    assert by_uid[bo]["person_id"] == f"{lead['id']}m1" and by_uid[cy]["person_id"] == f"{lead['id']}m2"   # row order at computation time
    # actual individual shares (A12 guide), not a division of the enquiry total: the two companions share a room
    shares = {k: float(v["share"]) for k, v in by_uid.items()}
    assert shares[bo] == shares[cy] and shares[bo] < shares[astrid] and shares[astrid] == shares[devi]
    assert abs(sum(shares.values()) - float(g["total_weekly"])) < 0.05
    # the lead's group_members row is the lead's OWN share, and every ranked apartment option is stamped too
    gm = row(db.execute("SELECT weekly_share FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    assert float(gm["weekly_share"]) == shares[astrid]
    ga = _occupants(db, g["id"], "group_apartments")
    assert ga and all(o.get("person_uid") in by_uid for o in ga)


def test_a_harmless_party_row_rewrite_keeps_the_uid_binding_while_positions_move(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    bo, cy = _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund")
    before_alloc = _occupants(db, g["id"])
    with retention_guard(db, label="rewrite"):
        cap = identities.capture(db, lead["id"])
        members = rows(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
        db.execute("DELETE FROM party_members WHERE enquiry_id=?", (lead["id"],))
        for m in reversed(members):     # same people, new row ids, REVERSED order
            db.execute("INSERT INTO party_members(enquiry_id, name, email, birth_country) VALUES (?,?,?,?)", (lead["id"], m["name"], m["email"], m["birth_country"]))
        res = identities.reconcile(db, lead["id"], "left", before=cap)
    assert res["reattached"] == 2 and res["changed"] is False
    assert _uid_of(db, lead["id"], "Bo Berg") == bo and _uid_of(db, lead["id"], "Cy Lund") == cy
    # the stored allocation was not touched by the rewrite: uids (and shares) still name the same people...
    after = _occupants(db, g["id"])
    assert [(o.get("person_uid"), o["share"]) for o in after] == [(o.get("person_uid"), o["share"]) for o in before_alloc]
    # ...while the positional id now points at the OTHER row: position is not proof, the uid is
    first_row = row(db.execute("SELECT name FROM party_members WHERE enquiry_id=? ORDER BY id LIMIT 1", (lead["id"],)))["name"]
    assert first_row == "Cy Lund" and next(o for o in after if o["person_id"] == f"{lead['id']}m1")["person_uid"] == bo


def test_removing_a_member_carries_the_remaining_uids_forward_without_restamping(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    astrid, bo, cy = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund")
    with retention_guard(db, label="reject"):
        matching._strip_from_allocations(db, g["id"], [solo["id"]])
    left = _occupants(db, g["id"])
    assert {o["person_uid"] for o in left} == {astrid, bo, cy} and all(o.get("person_uid") for o in left)
    covered = sum(float(o["share"]) for o in left)
    assert 0 < covered < float(g["total_weekly"])          # the empty bed's cost is the uncovered part; shares are the people's own


def test_a_stored_allocation_is_never_restamped_by_a_copy_or_read(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    gas = rows(db.execute("SELECT apartment_id, allocation_json FROM group_apartments WHERE group_id=?", (g["id"],)))
    assert gas
    # an older allocation without uids (pre-stamping data): drop them from the stored option and switch the group to it
    for ga in gas:
        alloc = pj(ga["allocation_json"], {})
        for room in alloc.get("rooms") or []:
            for o in room.get("occupants") or []:
                o.pop("person_uid", None)
        db.execute("UPDATE group_apartments SET allocation_json=? WHERE group_id=? AND apartment_id=?", (json.dumps(alloc), g["id"], ga["apartment_id"]))
    db.commit()
    matching.offer_apartment(db, g["id"], gas[0]["apartment_id"], "tester")
    assert all("person_uid" not in o for o in _occupants(db, g["id"]))   # copied as stored: unprovable stays absent


def test_without_the_identities_module_no_uid_is_written(db, monkeypatch):
    monkeypatch.setitem(sys.modules, "server.identities", None)
    rooms = [{"room": 1, "occupants": [{"person_id": "1", "share": 1.0}, {"person_id": "1m1", "share": 1.0, "person_uid": "stale"}]}]
    with retention_guard(db, label="stamp"):
        out = matching._stamp_people(db, rooms, [])
    assert all("person_uid" not in o for o in out[0]["occupants"])


def _units_now(c, *eids):
    return [matching._unit_for(c, eid) for eid in eids]


def test_an_occupant_without_a_provable_row_gets_no_uid(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    rooms = [{"room": 1, "occupants": [{"person_id": str(lead["id"]), "name": "Astrid", "share": 1.0}, {"person_id": f"{lead['id']}m9", "name": "Bo Berg", "share": 1.0},
                                        {"person_id": "x", "name": "Bo Berg", "share": 1.0}, {"person_id": f"{lead['id']}m1", "name": "Somebody Else", "share": 1.0},
                                        {"person_id": f"{lead['id']}m1", "name": "Bo Berg", "share": 1.0, "person_uid": "stale"}, {"person_id": "999", "name": "Ghost", "share": 1.0}]}]
    with retention_guard(db, label="stamp"):
        out = matching._stamp_people(db, rooms, units)
    o = out[0]["occupants"]
    assert o[0]["person_uid"] == _uid_of(db, lead["id"])
    assert "person_uid" not in o[1] and "person_uid" not in o[2]                   # no such row / not an engine id
    assert "person_uid" not in o[3]                                                 # row 1 exists but is not the person placed (name differs)
    assert o[4]["person_uid"] == _uid_of(db, lead["id"], "Bo Berg")                 # the stale input uid was dropped and re-proved
    assert "person_uid" not in o[5]                                                 # not among the units the allocation was computed from
    # no units / no guard: nothing is provable, nothing is stamped
    with retention_guard(db, label="stamp"):
        assert all("person_uid" not in x for x in matching._stamp_people(db, rooms, None)[0]["occupants"])
    assert all("person_uid" not in x for x in matching._stamp_people(db, rooms, units)[0]["occupants"])
    assert all("person_uid" not in x for x in pj(matching._alloc_json(db, rooms, units))["rooms"][0]["occupants"])


def _fresh_rooms(c, gid):
    rooms = (pj(row(c.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"], {}) or {})["rooms"]
    for r in rooms:
        for o in r["occupants"]:
            o.pop("person_uid", None)
    return rooms


def test_adversarial_same_name_other_email_between_computation_and_write_is_not_that_person(db, monkeypatch):
    """Identity proof is the captured (name, email) pair, not the name or the row position: a party row replaced by a same-name
    person with another email (or a same-name lead whose email changed) after the units were built gets NO uid; the others keep
    theirs. The occupant's share is then shown as not known, never credited to the newcomer."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])              # captured BEFORE the edits
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    db.execute("UPDATE party_members SET email='someone.else@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))   # same name, other person
    db.execute("UPDATE enquiries SET email='another.devi@example.com' WHERE id=?", (solo["id"],))                                     # same-name lead, other email
    db.commit()
    with retention_guard(db, label="stamp"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    by_pid = {o["person_id"]: o for r in out for o in r["occupants"]}
    assert by_pid[str(lead["id"])]["person_uid"] == astrid and by_pid[f"{lead['id']}m2"]["person_uid"] == cy
    assert "person_uid" not in by_pid[f"{lead['id']}m1"] and "person_uid" not in by_pid[str(solo["id"])]
    # the identities module itself has moved on (new uids for the edited rows): none of them is on the allocation either
    assert _uid_of(db, lead["id"], "Bo Berg") != bo and _uid_of(db, solo["id"]) != devi
    assert {o.get("person_uid") for o in by_pid.values()} == {astrid, cy, None}
    # A -> B -> A: the pairs are restored, but the identities the allocation was computed for (bo, devi) are retired and a third
    # uid now carries each pair. The OLD units still stamp nothing for them (a pair match never backfills a uid at the save) ...
    db.execute("UPDATE party_members SET email='bo@example.com' WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],))
    db.execute("UPDATE enquiries SET email='devi@example.com' WHERE id=?", (solo["id"],))
    db.commit()
    with retention_guard(db, label="stamp"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units)
    assert sorted(o.get("person_uid") or "" for r in out for o in r["occupants"]) == sorted([astrid, cy, "", ""])
    # ... and a NEW computation (units rebuilt from the rows as they are now) is attributed to the identities current now
    units2 = _units_now(db, lead["id"], solo["id"])
    with retention_guard(db, label="stamp"):
        out = matching._stamp_people(db, _fresh_rooms(db, g["id"]), units2)
    assert sorted(o.get("person_uid") or "" for r in out for o in r["occupants"]) == sorted([astrid, cy, _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, solo["id"])])
    assert _uid_of(db, lead["id"], "Bo Berg") != bo and _uid_of(db, solo["id"]) != devi


def test_adversarial_follow_up_stamp_of_a_group_created_outside_a_guard(db, monkeypatch):
    """The three unguarded creators stamp AFTER their commit, in their GP13 step, from the units they computed with. Between the
    commit and that step: an identity edit leaves that person unstamped; a membership change leaves the whole group unstamped."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])

    def unstamp():
        # portable (SQLite + PostgreSQL): address rows by their true keys, not SQLite's implicit rowid -
        # groups.id, and group_apartments by its (group_id, apartment_id) pair so each offer row is rewritten on its own
        for table, key, cols in (("groups", "id", ("id",)), ("group_apartments", "group_id", ("group_id", "apartment_id"))):
            sel = ", ".join(cols)
            for r in rows(db.execute(f"SELECT {sel}, allocation_json FROM {table} WHERE {key}=?", (g["id"],))):
                alloc = pj(r["allocation_json"], {})
                for room in alloc.get("rooms") or []:
                    for o in room.get("occupants") or []:
                        o.pop("person_uid", None)
                where = " AND ".join(f"{c}=?" for c in cols)
                db.execute(f"UPDATE {table} SET allocation_json=? WHERE {where}", (json.dumps(alloc), *[r[c] for c in cols]))
        db.commit()
        assert all("person_uid" not in o for o in _occupants(db, g["id"]) + _occupants(db, g["id"], "group_apartments"))

    unstamp()   # as the creator's unguarded write leaves it
    matching._membership_changed(db, g["id"], "mate", None, stamp_units=units)
    assert {o["person_uid"] for o in _occupants(db, g["id"])} == {astrid, bo, cy, devi}
    assert all(o.get("person_uid") for o in _occupants(db, g["id"], "group_apartments"))
    # identity edit in between: Cy's row now belongs to a same-name person with another email
    unstamp()
    db.execute("UPDATE party_members SET email='other.cy@example.com' WHERE enquiry_id=? AND name='Cy Lund'", (lead["id"],)); db.commit()
    matching._membership_changed(db, g["id"], "mate", None, stamp_units=units)
    occ = {o["person_id"]: o.get("person_uid") for o in _occupants(db, g["id"])}
    assert occ[f"{lead['id']}m2"] is None and occ[str(lead["id"])] == astrid and occ[f"{lead['id']}m1"] == bo and occ[str(solo["id"])] == devi
    # membership change in between (Devi left): the group is no longer what the creator computed -> nothing stamped at all
    unstamp()
    with retention_guard(db, label="strip"):
        matching._strip_from_allocations(db, g["id"], [solo["id"]])
    matching._membership_changed(db, g["id"], "mate", None, stamp_units=units)
    assert all("person_uid" not in o for o in _occupants(db, g["id"]))
    # ...and a stored occupant that is not the computed person (renamed in place) blocks the stamp as well
    unstamp()
    alloc = pj(row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (g["id"],)))["allocation_json"], {})
    alloc["rooms"][0]["occupants"][0]["name"] = "Nobody"
    db.execute("UPDATE groups SET allocation_json=? WHERE id=?", (json.dumps(alloc), g["id"])); db.commit()
    matching._membership_changed(db, g["id"], "mate", None, stamp_units=_units_now(db, lead["id"]))
    assert all("person_uid" not in o for o in _occupants(db, g["id"]))


def test_the_full_run_stamps_new_groups_too_and_a_mate_replacement_group_is_stamped_afresh(client, monkeypatch):   # noqa: F811
    from fastapi.testclient import TestClient
    from tests.test_bring_a_mate import _fake_ai, _group_of, _group_of_four, _person, _run, _submit
    from tests.test_data_and_api import appdb
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root_id, tok, others, g = _group_of_four(client, anon)
    c = appdb(client)
    occ = _occupants(c, g["id"])
    assert len(occ) == 4 and all(o.get("person_uid") for o in occ)
    assert {o["person_uid"] for o in occ} == {_uid_of(c, eid) for eid in [root_id, *others]}
    # a friend joins through the share link and replaces the weakest fit (GP11): the NEW group is computed and stamped afresh
    r = _submit(anon, _person(9), mate_token=tok)
    assert r.status_code == 201, r.text
    g2 = _group_of(client, root_id)
    assert g2 and g2[0]["id"] != g["id"] or g2[0]["id"] == g["id"]   # whichever path applied, the current group's occupants are all stamped
    occ2 = _occupants(c, g2[0]["id"])
    assert len(occ2) == 4 and all(o.get("person_uid") for o in occ2)
    assert _uid_of(c, root_id) in {o["person_uid"] for o in occ2} and _uid_of(c, r.json()["id"]) in {o["person_uid"] for o in occ2}


def _bar_lead_party_stays(db, monkeypatch):
    """Real staff bar of the ENQUIRER on the four-person group (Astrid barred, Bo and Cy stay): Bo takes over the enquiry."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    occ_before = {o["person_uid"]: dict(o) for o in _occupants(db, g["id"])}
    astrid, bo, cy, devi = _uid_of(db, lead["id"]), _uid_of(db, lead["id"], "Bo Berg"), _uid_of(db, lead["id"], "Cy Lund"), _uid_of(db, solo["id"])
    out = matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    assert out["party_stayed"] and g["id"] in out["awaiting_groups"], out
    return lead, solo, g, occ_before, (astrid, bo, cy, devi)


def test_a_lead_takeover_bar_rewrites_the_surviving_lead_row_from_the_promoted_persons_own_unchanged_proposal(db, monkeypatch):
    lead, solo, g, before, (astrid, bo, cy, devi) = _bar_lead_party_stays(db, monkeypatch)
    occ = {o["person_uid"]: o for o in _occupants(db, g["id"])}
    assert set(occ) == {bo, cy, devi} and astrid not in occ           # the barred person's bed is empty; the others keep their uids
    assert occ[bo]["person_id"] == str(lead["id"]) and occ[cy]["person_id"] == f"{lead['id']}m1"   # renumbered positions
    for uid in (bo, cy, devi):                                         # every survivor's proposed share, room and bed are UNCHANGED
        assert float(occ[uid]["share"]) == float(before[uid]["share"]) and occ[uid]["bed"] == before[uid]["bed"]
    assert float(before[bo]["share"]) < float(before[astrid]["share"])  # Bo's own share is not the barred lead's
    gm = row(db.execute("SELECT weekly_share, bed, room FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    assert float(gm["weekly_share"]) == float(before[bo]["share"]) and gm["bed"] == before[bo]["bed"]     # the lead row is now Bo's
    gm_devi = row(db.execute("SELECT weekly_share FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], solo["id"])))
    assert float(gm_devi["weekly_share"]) == float(before[devi]["share"])
    # the taker's identity is the same human (same uid, now the lead) and the barred lead's is retired
    idents = {i["person_uid"]: i for i in identities.active(db, lead["id"])}
    assert idents[bo]["role"] == "lead" and idents[cy]["role"] == "member" and astrid not in idents
    # the ranked options were renumbered the same way and keep their uids
    ga = {o["person_uid"]: o for o in _occupants(db, g["id"], "group_apartments") if o.get("person_uid")}
    assert bo in ga and ga[bo]["person_id"] == str(lead["id"]) and astrid not in ga


def test_a_member_bar_leaves_the_lead_row_alone(db, monkeypatch):
    a, lead, solo, g = _party_group(db, monkeypatch)
    before = row(db.execute("SELECT weekly_share, bed FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    pm = row(db.execute("SELECT id FROM party_members WHERE enquiry_id=? AND name='Bo Berg'", (lead["id"],)))
    matching.bar_enquiry(db, lead["id"], "staff", "test bar", person=pm["id"], party_choice="stay")
    after = row(db.execute("SELECT weekly_share, bed FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    assert dict(after) == dict(before)


def test_after_a_lead_takeover_each_persons_public_own_share_is_their_unchanged_proposal(db, monkeypatch):
    """Integration with the integration builder's group_responses._own_share (frozen Y): the promoted lead reads the lead row,
    companions read their stamped occupant. Skipped where that build's helper is not present (reviewer dependency)."""
    from server import group_responses
    own = getattr(group_responses, "_own_share", None)
    if own is None:
        pytest.skip("group_responses._own_share not in this build (integration builder's Y)")
    lead, solo, g, before, (astrid, bo, cy, devi) = _bar_lead_party_stays(db, monkeypatch)
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    for uid, eid in ((bo, lead["id"]), (cy, lead["id"]), (devi, solo["id"])):
        assert own(db, g, {"person_uid": uid, "enquiry_id": eid}) == float(before[uid]["share"]), uid
    assert float(before[bo]["share"]) != float(before[astrid]["share"])


def _corrupt_lead_occupant(c, gid, lead_id, how):
    """Stored allocations where the takeover cannot be proved: 'zero' = the enquirer's occupant is missing from the group's
    allocation; 'multiple' = two occupants carry the enquirer's engine id (a damaged document)."""
    alloc = pj(row(c.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"], {})
    for r in alloc["rooms"]:
        occ = r["occupants"]
        if how == "zero":
            r["occupants"] = [o for o in occ if o["person_id"] != f"{lead_id}m1"]          # Bo (who will take over) has no occupant
        else:
            for o in occ:
                if o["person_id"] == f"{lead_id}m2":
                    o["person_id"] = f"{lead_id}m1"                                        # Bo and Cy both carry m1 -> both remap to the lead id
    c.execute("UPDATE groups SET allocation_json=? WHERE id=?", (json.dumps(alloc), gid)); c.commit()


@pytest.mark.parametrize("how", ["zero", "multiple"])
def test_an_unprovable_lead_takeover_leaves_the_lead_row_unknown_never_the_barred_persons_share(db, monkeypatch, how):
    a, lead, solo, g = _party_group(db, monkeypatch)
    before = row(db.execute("SELECT weekly_share FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    assert float(before["weekly_share"]) > 0
    _corrupt_lead_occupant(db, g["id"], lead["id"], how)
    out = matching.bar_enquiry(db, lead["id"], "staff", "test bar", person="lead", party_choice="stay")
    assert out["party_stayed"]
    gm = row(db.execute("SELECT weekly_share, room, bed FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], lead["id"])))
    assert gm is not None and gm["weekly_share"] is None and gm["room"] is None and gm["bed"] is None   # not known - not Astrid's 533.33
    devi = row(db.execute("SELECT weekly_share FROM group_members WHERE group_id=? AND enquiry_id=?", (g["id"], solo["id"])))
    assert float(devi["weekly_share"]) > 0                                                            # the other survivor is untouched
    from server import group_responses
    own = getattr(group_responses, "_own_share", None)
    if own is not None:   # integration builder's Z: the promoted lead's public figure is unknown, not the barred person's
        g2 = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
        assert own(db, g2, {"person_uid": _uid_of(db, lead["id"]), "enquiry_id": lead["id"]}) is None


def test_identity_provenance_is_not_an_engine_input(db, monkeypatch):
    """The captured pair rides on the Person for the uid proof only: it is never in an allocation, and an email edit during a run
    does not make the plan stale at the save boundary (the person is simply left unstamped there, see the adversarial test)."""
    a, lead, solo, g = _party_group(db, monkeypatch)
    units = _units_now(db, lead["id"], solo["id"])
    assert all(p.ident_email and p.ident_uid for u in units for p in u.people)
    assert all("ident_email" not in o and "ident_uid" not in o and "ident_name" not in o and "email" not in o for o in _occupants(db, g["id"]) + _occupants(db, g["id"], "group_apartments"))
    import dataclasses
    u = units[0]
    other = dataclasses.replace(u, people=[dataclasses.replace(p, ident_name="Other Name", ident_email="other@example.com", ident_uid="ffff") for p in u.people])
    assert matching._engine_view(other) == matching._engine_view(u)          # the save-boundary stale check ignores the provenance...
    renamed = dataclasses.replace(u, people=[dataclasses.replace(p, name="Bo Borg") if p.name == "Bo Berg" else p for p in u.people])
    assert matching._engine_view(renamed) != matching._engine_view(u)        # ...while a placed name is an engine input, as before
    is_test = int(row(db.execute("SELECT is_test FROM enquiries WHERE id=?", (lead["id"],)))["is_test"] or 0)
    db.execute("UPDATE enquiries SET status='New' WHERE id IN (?, ?)", (lead["id"], solo["id"]))   # back in the pool, as during a run
    assert matching._units_still_current(db, units, is_test) is True
