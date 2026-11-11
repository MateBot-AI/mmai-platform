"""T5 retention applied to the generated-text tables (8 Nov 2026).

Deleting a person (staff delete or the 12-month purge) must not leave their profile, contact details or AI-written text about
them in group_briefs (member briefs, group briefs, introduction drafts), review_rounds or groups.intro_text / ai_rationale, nor
their name in bed allocations, free-text audit notes about them, the cleared groups' free-text notes, or their emailed-form
invitation row. Lease terms (rents, shares, beds) and non-personal audit metadata stay.
The automatic 12-month expiry never reaches a leased person (T5 exemption), so it leaves leased groups alone; an authorised
manual deletion (T15) cleans that person's text from a leased group as well. History is normal revision history, not an
exemption from deletion.
"""
import json

from server import enquiries
from server.db import now_iso, row, rows
from tests.test_data_and_api import accept_all_members, appdb, client, good_answers, mark_introduced, member  # noqa: F401  (fixtures re-used)
from tests.test_intro_profile import _plant_group, hdr


def _submit(client, **over):
    r = client.post("/api/public/enquiries", json=good_answers(**over), headers=hdr())
    assert r.status_code == 201, r.text
    return r.json()


def _add_member(c, gid, eid):
    c.execute("INSERT INTO group_members(group_id, enquiry_id, weekly_share) VALUES (?, ?, 575.0)", (gid, eid))
    c.execute("UPDATE groups SET member_key=? WHERE id=?", (",".join(str(r["enquiry_id"]) for r in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))), gid))


def _brief(c, gid, kind, body, eid=None, pmid=None, status="ok", ref=None, rnd=0, h="h", at=None):
    c.execute("""INSERT INTO group_briefs(group_id, round, kind, person_ref, enquiry_id, party_member_id, body_json, status, model, rule_version, evidence_hash, created_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (gid, rnd, kind, ref, eid, pmid, json.dumps(body), status, "m", "v", h, at or now_iso()))


def _review(c, gid, text, rnd=0, h="h", at=None):
    c.execute("""INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
              (gid, rnd, "approve", 8.0, text, json.dumps({"findings": [text]}), 1, h, at or now_iso()))


def _plant_history(c, gid, victim, victim_pm, other):
    """A reviewed, briefed, introduced-draft group with the victim (lead + one party member) and another lead."""
    _brief(c, gid, "member", {"text": "Victoria Victim keeps a tidy kitchen"}, eid=victim, ref="U1")
    _brief(c, gid, "member", {"text": "Pat Party (party member) - no own answers"}, eid=victim, pmid=victim_pm, ref="U1-m1")
    _brief(c, gid, "member", {"text": "Oscar Other studies"}, eid=other, ref="U2")
    _brief(c, gid, "group", {"rendered": {"strengths": ["Victoria and Oscar both wake early"]}})
    _brief(c, gid, "intro", {"text": "Hello Victoria, Pat and Oscar... victim@example.com", "basis": "AI"})
    _review(c, gid, "Victoria Victim and Oscar Other: compatible routines")
    c.execute("UPDATE groups SET intro_text=?, ai_rationale=?, ai_status='ok', review_verdict='approve', brief_status='ok', evidence_hash='h' WHERE id=?",
              ("Hello Victoria, Pat and Oscar", "Victoria Victim is tidy", gid))
    c.commit()


def _plant_snapshots(c, gid, victim, victim_pm, other, staff_client=None):
    """The other persisted snapshots root's T5 trace found: bed allocation names, free-text audit notes, discount note, invite."""
    alloc = {"rooms": [{"room": 1, "setup": "1 king or 2 singles", "room_cost": 575.0, "occupants": [{"person_id": str(victim), "name": "Victoria Victim", "bed": "king", "share": 575.0}]},
                       {"room": 2, "setup": "1 king or 2 singles", "room_cost": 575.0, "occupants": [{"person_id": f"{victim}m1", "name": "Pat Party", "bed": "single", "share": 287.5},
                                                                                                       {"person_id": str(other), "name": "Oscar Other", "bed": "single", "share": 287.5}]}]}
    evidence = {"rule_version": 1, "people": {"U1": {"name_hint": "Victoria Victim", "budget_max": 575, "routine": "Early riser"}, "U2": {"budget_max": 300}}}
    c.execute("""UPDATE groups SET allocation_json=?, discount_note='Victoria asked for $20 off because of the small room', total_weekly=1150, evidence_json=?,
                 rerank_note='Victoria Victim asked to move rooms', awaiting_note=NULL WHERE id=?""", (json.dumps(alloc), json.dumps(evidence), gid))
    c.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, total_weekly, allocation_json, ai_reason) SELECT id, apartment_id, 1, 1150, ?, 'Victoria Victim prefers the quiet side' FROM groups WHERE id=?", (json.dumps(alloc), gid))
    # Victoria was also the AI's suggested replacement for some other group
    c.execute("INSERT INTO groups(match_run_id, apartment_id, status, total_weekly, created_at, updated_at, member_key, replacement_json) SELECT match_run_id, apartment_id, 'Awaiting replacement', 900, ?, ?, '999', ? FROM groups WHERE id=?",
              (now_iso(), now_iso(), json.dumps([{"added_enquiry_ids": [victim], "added_names": ["Victoria Victim"], "rule_score": 1}, {"added_enquiry_ids": [other], "added_names": ["Oscar Other"], "rule_score": 0.9}]), gid))
    c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?, 'Anna', 'log_contact', 'enquiry', ?, 'Victoria called about her visa dates')", (now_iso(), str(victim)))
    c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?, 'Anna', 'group_discount_note', 'group', ?, 'Victoria asked for $20 off')", (now_iso(), str(gid)))
    c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?, 'Anna', 'group_status', 'group', ?, 'Proposed')", (now_iso(), str(gid)))
    c.execute("INSERT INTO email_links(token, created_at, staff_member, recipient_email, recipient_name, used_at) VALUES ('tok-victim', ?, 'Anna', 'victim@example.com', 'Victoria Victim', ?)", (now_iso(), now_iso()))
    lid = c.execute("SELECT id FROM email_links WHERE token='tok-victim'").fetchone()[0]
    c.execute("UPDATE enquiries SET email_link_id=? WHERE id=?", (lid, victim))
    c.commit()
    return lid


def _mentions(c, needle):
    hits = []
    for t, col in (("group_briefs", "body_json"), ("review_rounds", "rationale"), ("review_rounds", "review_json"), ("groups", "intro_text"), ("groups", "ai_rationale"),
                   ("groups", "allocation_json"), ("group_apartments", "allocation_json"), ("groups", "discount_note"), ("groups", "ai_error"), ("groups", "held_reason"),
                   ("group_apartments", "ai_reason"), ("review_rounds", "invalid_reason"), ("audit_log", "detail"), ("email_links", "recipient_name"), ("email_links", "recipient_email")):
        n = c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} LIKE ?", (f"%{needle}%",)).fetchone()[0]
        if n:
            hits.append(f"{t}.{col}")
    return hits


def test_staff_delete_removes_generated_text_about_the_person_and_keeps_the_other_member(client):
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com", party="With friends", party_size="2",
                party_members=[member(name="Pat Party", email="pat@example.com")])
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    c = appdb(client)
    gid = _plant_group(c, v["id"])
    _add_member(c, gid, o["id"])
    pm = row(c.execute("SELECT id FROM party_members WHERE enquiry_id=?", (v["id"],)))["id"]
    _plant_history(c, gid, v["id"], pm, o["id"])
    lid = _plant_snapshots(c, gid, v["id"], pm, o["id"])
    assert _mentions(c, "Victoria")  # precondition: the text is there

    r = client.delete(f"/api/enquiries/{v['id']}")
    assert r.status_code == 200, r.text
    rep = r.json()["generated_text_removed"]
    assert rep["brief_rows"] == 5 and rep["review_rows"] == 1 and rep["groups_cleared"] == 1 and rep["groups_dissolved"] == 0, rep
    assert rep["allocations_scrubbed"] == 2 and rep["audit_notes_cleared"] == 2 and rep["email_links_deleted"] == 1 and rep["replacement_entries"] == 1, rep

    assert _mentions(c, "Victoria") == [] and _mentions(c, "Pat Party") == [] and _mentions(c, "victim@example.com") == []
    # lease terms survive in the allocation; only the deleted people's names are gone; Oscar's name stays
    al = json.loads(row(c.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"])
    occ = [o for r_ in al["rooms"] for o in r_["occupants"]]
    assert [o["name"] for o in occ] == ["Former member", "Former member", "Oscar Other"] and [o["share"] for o in occ] == [575.0, 287.5, 287.5] and al["rooms"][0]["room_cost"] == 575.0
    assert json.loads(row(c.execute("SELECT allocation_json FROM group_apartments WHERE group_id=?", (gid,)))["allocation_json"]) == al
    # audit rows keep who/when/what; only free text about the person or the cleared group is gone
    notes = {(r_["action"], r_["detail"]) for r_ in rows(c.execute("SELECT action, detail FROM audit_log WHERE action IN ('log_contact','group_discount_note','group_status','create_enquiry')"))}
    assert notes == {("log_contact", None), ("group_discount_note", None), ("group_status", "Proposed"), ("create_enquiry", "channel=Landing page; test=False")}
    gr = row(c.execute("SELECT discount_note, evidence_json, rerank_note, total_weekly, lease_start, apartment_id FROM groups WHERE id=?", (gid,)))
    assert gr["discount_note"] is None and gr["evidence_json"] is None and gr["rerank_note"] is None and gr["total_weekly"] == 1150 and gr["apartment_id"]
    assert row(c.execute("SELECT ai_reason, total_weekly FROM group_apartments WHERE group_id=?", (gid,))) == {"ai_reason": None, "total_weekly": 1150}
    rep_json = json.loads(row(c.execute("SELECT replacement_json FROM groups WHERE member_key='999'"))["replacement_json"])
    assert rep_json == [{"added_enquiry_ids": [o["id"]], "added_names": ["Oscar Other"], "rule_score": 0.9}]
    assert c.execute("SELECT COUNT(*) FROM email_links WHERE id=?", (lid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (gid,)).fetchone()[0] == 0   # the group brief embedded the member set, and so did the other member's brief round
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert g["status"] == "Proposed" and g["intro_text"] is None and g["ai_rationale"] is None and g["review_verdict"] is None and g["brief_status"] is None
    assert g["ai_status"] == "stale" and "deleted (retention)" in g["held_reason"]
    assert [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gid,)))] == [o["id"]]
    # Oscar's own record is untouched
    assert client.get(f"/api/enquiries/{o['id']}").status_code == 200
    # the group detail no longer presents the old introduction as current
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro_text"] is None and d["ai_rationale"] is None
    note = row(c.execute("SELECT detail FROM audit_log WHERE action='delete_enquiry' ORDER BY id DESC LIMIT 1"))["detail"]
    assert "5 brief row(s)" in note and "1 review row(s)" in note


def test_purge_removes_generated_text_but_leaves_leased_groups_alone(client):
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com", party="With friends", party_size="2",
                party_members=[member(name="Pat Party", email="pat@example.com")])
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    lessee = _submit(client, first_name="Lena", last_name="Lessee", email="lena@example.com")
    c = appdb(client)
    pm = row(c.execute("SELECT id FROM party_members WHERE enquiry_id=?", (v["id"],)))["id"]
    # group A: the victim alone with Oscar, never leased -> cleared and, once empty of others, dissolved
    ga = _plant_group(c, v["id"])
    _plant_history(c, ga, v["id"], pm, o["id"])
    # group B: a LEASED group with Lena; her text stays (the register exempts leased people)
    gb = _plant_group(c, lessee["id"])
    _brief(c, gb, "member", {"text": "Lena Lessee likes quiet evenings"}, eid=lessee["id"], ref="U1")
    _brief(c, gb, "group", {"rendered": {"strengths": ["Lena keeps regular hours"]}})
    _review(c, gb, "Lena Lessee: approved")
    c.execute("UPDATE groups SET status='Leased', intro_text='Hello Lena', ai_rationale='Lena is steady' WHERE id=?", (gb,))
    c.execute("UPDATE enquiries SET status='Leased' WHERE id=?", (lessee["id"],))
    # the victim is 13 months stale; Oscar and Lena are fresh
    c.execute("UPDATE enquiries SET last_contact_at='2025-09-01T00:00:00+00:00' WHERE id=?", (v["id"],))
    c.commit()

    rep = enquiries.purge_expired(c, "test")
    assert rep["deleted"] == 1
    scrub = rep["generated_text_removed"]
    assert scrub["people"] == 1 and scrub["groups_cleared"] == 1 and scrub["groups_dissolved"] == 1, scrub

    assert _mentions(c, "Victoria") == [] and _mentions(c, "Pat Party") == []
    assert row(c.execute("SELECT status, intro_text FROM groups WHERE id=?", (ga,))) == {"status": "Dissolved", "intro_text": None}
    # the leased group and Lena are intact, text included
    gbr = row(c.execute("SELECT status, intro_text, ai_rationale FROM groups WHERE id=?", (gb,)))
    assert gbr == {"status": "Leased", "intro_text": "Hello Lena", "ai_rationale": "Lena is steady"}
    assert c.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=?", (gb,)).fetchone()[0] == 2
    assert c.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (gb,)).fetchone()[0] == 1
    assert _mentions(c, "Lena") == ["group_briefs.body_json", "review_rounds.rationale", "review_rounds.review_json", "groups.intro_text", "groups.ai_rationale"]
    assert client.get(f"/api/enquiries/{o['id']}").status_code == 200 and client.get(f"/api/enquiries/{lessee['id']}").status_code == 200


def test_deleting_a_leased_person_by_hand_also_clears_the_leased_groups_text_but_keeps_the_lease(client):
    """T15 authorised manual deletion: the leased-person exemption belongs to the AUTOMATIC expiry only. By hand, the text about
    the person goes from the leased group too; the group stays Leased with its lease facts and the other member."""
    lessee = _submit(client, first_name="Lena", last_name="Lessee", email="lena@example.com")
    other = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    c = appdb(client)
    gb = _plant_group(c, lessee["id"])
    _add_member(c, gb, other["id"])
    _brief(c, gb, "member", {"text": "Lena Lessee likes quiet evenings"}, eid=lessee["id"], ref="U1")
    _brief(c, gb, "member", {"text": "Oscar Other studies"}, eid=other["id"], ref="U2")
    _brief(c, gb, "group", {"rendered": {"strengths": ["Lena and Oscar keep regular hours"]}})
    _review(c, gb, "Lena Lessee and Oscar: approved")
    c.execute("UPDATE groups SET status='Leased', intro_text='Hello Lena and Oscar', ai_rationale='Lena is steady', total_weekly=1150 WHERE id=?", (gb,))
    c.commit()
    r = client.delete(f"/api/enquiries/{lessee['id']}")
    assert r.status_code == 200
    rep = r.json()["generated_text_removed"]
    assert rep["people"] == 1 and rep["brief_rows"] == 3 and rep["review_rows"] == 1 and rep["groups_cleared"] == 1 and rep["groups_dissolved"] == 0, rep
    assert _mentions(c, "Lena") == []
    g = row(c.execute("SELECT status, intro_text, ai_rationale, total_weekly FROM groups WHERE id=?", (gb,)))
    assert g == {"status": "Leased", "intro_text": None, "ai_rationale": None, "total_weekly": 1150}
    assert [m["enquiry_id"] for m in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (gb,)))] == [other["id"]]


def test_forget_generated_text_with_nobody_is_a_no_op(client):
    c = appdb(client)
    assert enquiries.forget_generated_text(c, []) == {"people": 0, "brief_rows": 0, "review_rows": 0, "groups_cleared": 0, "groups_dissolved": 0, "historical_groups": 0, "allocations_scrubbed": 0, "audit_notes_cleared": 0, "email_links_deleted": 0, "drafts_deleted": 0, "replacement_entries": 0, "mate_requests_deleted": 0,
                                                         "run_errors_neutralised": 0, "ai_failure_sinks_neutralised": 0, "audit_errors_neutralised": 0,
                                                         "exception_records_scrubbed": 0}   # T5: the run-error, AI-failure-sink and audit-error steps still run with nobody eligible; 0 = nothing legacy found


# ---------------- late AI result vs deletion: the shared protocol (root, 8 Nov) ----------------
def test_in_flight_review_for_a_deleted_person_is_never_written_back(client, monkeypatch):
    """A review (or brief) that the AI was writing while a member's record was deleted must not reinsert text about that person
    into review_rounds / group_briefs / groups.ai_rationale. Delete side: the surviving group's member_key becomes the survivors'
    key and its evidence hash is cleared in the deletion transaction; writer side: member_key / evidence hash are checked before
    anything is inserted. Deterministic: the deletion happens inside the AI call itself."""
    from server import ai, ai_queue
    from tests.test_t6_grounding import _party_group
    from tests.test_t6_fail_closed import _reply_builder, _good
    c = appdb(client)
    a, lead, solo, g = _party_group(c, monkeypatch)
    gid = g["id"]
    # queue a fresh review of the same group; the deletion lands while the AI is "thinking"
    c.execute("UPDATE groups SET ai_status='pending', ai_queued_at=? WHERE id=?", (now_iso(), gid)); c.commit()
    good = _reply_builder(_good)

    def deleting_ask(prompt, model=None, max_tokens=1500, system=""):
        r = client.delete(f"/api/enquiries/{solo['id']}")
        assert r.status_code == 200, r.text
        return good(prompt, model, max_tokens, system)
    monkeypatch.setattr(ai, "_ask", deleting_ask)
    ai_queue.process_pending(c)

    live = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert live["member_key"] == str(lead["id"]) and live["evidence_hash"] is None
    assert live["ai_status"] != "ok" and live["ai_rationale"] is None and live["review_verdict"] is None and live["brief_status"] is None
    assert rows(c.execute("SELECT * FROM review_rounds WHERE group_id=?", (gid,))) == []
    assert rows(c.execute("SELECT * FROM group_briefs WHERE group_id=?", (gid,))) == []
    assert _mentions(c, "Devi") == [] and _mentions(c, "devi@example.com") == []
    assert ai_queue.is_ready(c, live)["ready"] is False


# ---------------- historical membership: replaced out earlier, deleted later ----------------
def test_person_replaced_out_earlier_is_forgotten_from_the_old_rounds_only(client):
    """Victoria was in group G (round 0: review, briefs, intro), then rejected and replaced by Nia while G kept its id (round 1 was
    written about the NEW member set). Deleting Victoria later must remove round 0 (found through her keyed brief and the
    rejection record, not current membership) and leave round 1 and the group's current text untouched."""
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    n = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    gid = _plant_group(c, v["id"])
    _add_member(c, gid, o["id"])
    pm = None
    _plant_history(c, gid, v["id"], pm, o["id"])                      # round 0, hash 'h', Victoria + Oscar
    # staff rejected Victoria; Nia took her place in the same group row (the reject flow records the rejection and rewrites the key)
    c.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, v["id"]))
    c.execute("INSERT INTO member_rejections(group_id, rejected_enquiry_id, other_enquiry_id, at, staff_member) VALUES (?,?,?,?,?)", (gid, v["id"], o["id"], now_iso(), "Anna"))
    _add_member(c, gid, n["id"])
    later = "2099-01-01T00:00:00+11:00"
    _brief(c, gid, "member", {"text": "Oscar Other studies"}, eid=o["id"], ref="U1", rnd=1, h="h2", at=later)
    _brief(c, gid, "member", {"text": "Nia New works in a cafe"}, eid=n["id"], ref="U2", rnd=1, h="h2", at=later)
    _brief(c, gid, "group", {"rendered": {"strengths": ["Oscar and Nia both like a quiet flat"]}}, rnd=1, h="h2", at=later)
    _brief(c, gid, "intro", {"text": "Hello Oscar and Nia", "basis": "AI"}, rnd=1, h="h2", at=later)
    _review(c, gid, "Oscar Other and Nia New: compatible", rnd=1, h="h2", at=later)
    c.execute("UPDATE groups SET intro_text='Hello Oscar and Nia', ai_rationale='Oscar and Nia are tidy', evidence_hash='h2', ai_status='ok', review_verdict='approve', brief_status='ok' WHERE id=?", (gid,))
    # an OLD bed allocation that still lists Victoria survived the replacement (group row and a stored apartment candidate)
    old_alloc = {"rooms": [{"room": 1, "setup": "1 king or 2 singles", "room_cost": 575.0, "occupants": [{"person_id": str(v["id"]), "name": "Victoria Victim", "bed": "king", "share": 575.0}]},
                           {"room": 2, "setup": "1 king or 2 singles", "room_cost": 575.0, "occupants": [{"person_id": str(o["id"]), "name": "Oscar Other", "bed": "king", "share": 575.0}]}]}
    c.execute("UPDATE groups SET allocation_json=?, total_weekly=1150 WHERE id=?", (json.dumps(old_alloc), gid))
    c.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, total_weekly, allocation_json, ai_reason) SELECT id, apartment_id, 1, 1150, ?, 'ranked by rules' FROM groups WHERE id=?", (json.dumps(old_alloc), gid))
    c.commit()
    assert _mentions(c, "Victoria")

    r = client.delete(f"/api/enquiries/{v['id']}")
    assert r.status_code == 200, r.text
    rep = r.json()["generated_text_removed"]
    assert rep["groups_cleared"] == 0 and rep["historical_groups"] == 1 and rep["review_rows"] == 1 and rep["allocations_scrubbed"] == 2, rep
    assert _mentions(c, "Victoria") == [] and _mentions(c, "victim@example.com") == []
    # both allocations: Victoria's name gone, her bed/share/room-cost accounting and Oscar's entry unchanged
    for table, key in (("groups", "id"), ("group_apartments", "group_id")):
        al = json.loads(row(c.execute(f"SELECT allocation_json FROM {table} WHERE {key}=?", (gid,)))["allocation_json"])
        occ = [o_ for rm in al["rooms"] for o_ in rm["occupants"]]
        assert [o_["name"] for o_ in occ] == ["Former member", "Oscar Other"] and [o_["share"] for o_ in occ] == [575.0, 575.0], (table, occ)
        assert [o_["person_id"] for o_ in occ] == [str(v["id"]), str(o["id"])] and all(rm["room_cost"] == 575.0 for rm in al["rooms"])
    assert row(c.execute("SELECT ai_reason FROM group_apartments WHERE group_id=?", (gid,)))["ai_reason"] == "ranked by rules"   # unrelated apartment reason kept
    # the current member set's round and text are intact
    assert [r_["round"] for r_ in rows(c.execute("SELECT round FROM review_rounds WHERE group_id=?", (gid,)))] == [1]
    assert {r_["kind"] for r_ in rows(c.execute("SELECT kind FROM group_briefs WHERE group_id=? AND round=1", (gid,)))} == {"member", "group", "intro"}
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert g["intro_text"] == "Hello Oscar and Nia" and g["ai_rationale"] == "Oscar and Nia are tidy" and g["evidence_hash"] == "h2" and g["ai_status"] == "ok" and g["member_key"] == f"{min(o['id'], n['id'])},{max(o['id'], n['id'])}"
    assert _mentions(c, "Nia")


# ---------------- immutable ref maps (refs_json, 8 Nov): exact history, no time heuristics ----------------
def test_rows_with_an_immutable_ref_map_are_deleted_exactly_and_history_names_come_from_the_map(client):
    """When the writer stored refs_json with a round, deletion finds the person's rounds through that map (exact), not through
    member_rejections or round/time, and the staff history names people from the map rather than from today's members."""
    from server import staff_view
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    n = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    gid = _plant_group(c, o["id"])
    _add_member(c, gid, n["id"])            # CURRENT members: Oscar (U1) and Nia (U2)
    # an OLD round written about Oscar (U1) + Victoria (U2) with its own map; no rejection record, same timestamps as the new one
    old_map = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": v["id"], "party_member_id": None}})
    new_map = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": n["id"], "party_member_id": None}})
    at = now_iso()
    _review(c, gid, "U1 and U2 wake early", rnd=3, h="old", at=at)
    _brief(c, gid, "group", {"rendered": {"strengths": ["U1 and U2 wake early"]}}, rnd=3, h="old", at=at)
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=? AND round=3", (old_map, gid))
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=? AND round=3", (old_map, gid))
    _review(c, gid, "U1 and U2 both like quiet", rnd=4, h="new", at=at)
    _brief(c, gid, "group", {"rendered": {"strengths": ["U1 and U2 both like quiet"]}}, rnd=4, h="new", at=at)
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=? AND round=4", (new_map, gid))
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=? AND round=4", (new_map, gid))
    c.execute("UPDATE groups SET evidence_hash='new', ai_status='ok' WHERE id=?", (gid,))
    c.commit()

    # history display: the old round names Oscar and Victoria from ITS map, even though Nia is U2 today
    old_names = staff_view.ref_names(c, gid, "old", old_map)
    assert old_names == {"U1": "Oscar", "U2": "Victoria"}
    assert staff_view.ref_names(c, gid, "new", new_map) == {"U1": "Oscar", "U2": "Nia"}
    assert staff_view.ref_names(c, gid, "old", None).get("U2") == "an earlier member"      # legacy row without a map

    out = enquiries.forget_generated_text(c, [v["id"]])
    c.commit()
    assert out["review_rows"] == 1 and out["brief_rows"] == 1 and out["historical_groups"] == 1 and out["groups_cleared"] == 0
    assert [r["round"] for r in rows(c.execute("SELECT round FROM review_rounds WHERE group_id=?", (gid,)))] == [4]
    assert [r["round"] for r in rows(c.execute("SELECT round FROM group_briefs WHERE group_id=?", (gid,)))] == [4]
    live = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert live["evidence_hash"] == "new" and live["ai_status"] == "ok"      # the current round survives untouched
    # a deleted person in a surviving map shows as a former member, never by a guessed current name
    c.execute("DELETE FROM enquiries WHERE id=?", (v["id"],)); c.commit()
    assert staff_view.ref_names(c, gid, "old", old_map) == {"U1": "Oscar", "U2": "a former member"}


def test_another_persons_member_brief_that_compares_with_the_deleted_person_is_deleted_too(client):
    """r5 briefs may carry attributed comparisons (compared_with: [{ref: 'U2', fact_ids: ['U2.routine']}]) inside ANOTHER
    person's member brief. The writer stores the whole group's immutable ref map on every row of a round, so deleting U2
    (Victoria) must remove Oscar's member brief from that round as well - in a group she has since left - while Oscar's member
    brief from a later round written about the new member set (map without her) survives."""
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    n = _submit(client, first_name="Nia", last_name="New", email="nia@example.com")
    c = appdb(client)
    gid = _plant_group(c, o["id"])
    _add_member(c, gid, n["id"])            # current members: Oscar (U1) and Nia (U2); Victoria was U2 in round 3
    old_map = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": v["id"], "party_member_id": None}})
    new_map = json.dumps({"U1": {"enquiry_id": o["id"], "party_member_id": None}, "U2": {"enquiry_id": n["id"], "party_member_id": None}})
    at = now_iso()
    # round 3 (with Victoria): Oscar's own brief compares him with U2; Victoria's brief; group brief; review
    _brief(c, gid, "member", {"ref": "U1", "strengths": [{"fact_ids": ["U1.routine"], "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}], "note": "U1 and U2 both rise early"}]},
           eid=o["id"], ref="U1", rnd=3, h="old", at=at)
    _brief(c, gid, "member", {"ref": "U2", "strengths": [{"fact_ids": ["U2.routine"], "note": "U2 rises early"}]}, eid=v["id"], ref="U2", rnd=3, h="old", at=at)
    _brief(c, gid, "group", {"rendered": {"strengths": ["U1 and U2 both rise early"]}}, rnd=3, h="old", at=at)
    _review(c, gid, "U1 and U2 compatible", rnd=3, h="old", at=at)
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=? AND round=3", (old_map, gid))
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=? AND round=3", (old_map, gid))
    # round 4 (Nia instead): Oscar's brief again compares with U2 - now Nia - and must survive
    _brief(c, gid, "member", {"ref": "U1", "strengths": [{"fact_ids": ["U1.routine"], "compared_with": [{"ref": "U2", "fact_ids": ["U2.routine"]}], "note": "U1 and U2 both rise early"}]},
           eid=o["id"], ref="U1", rnd=4, h="new", at=at)
    _brief(c, gid, "member", {"ref": "U2", "strengths": []}, eid=n["id"], ref="U2", rnd=4, h="new", at=at)
    _brief(c, gid, "group", {"rendered": {"strengths": ["U1 and U2 both rise early"]}}, rnd=4, h="new", at=at)
    _review(c, gid, "U1 and U2 compatible", rnd=4, h="new", at=at)
    c.execute("UPDATE group_briefs SET refs_json=? WHERE group_id=? AND round=4", (new_map, gid))
    c.execute("UPDATE review_rounds SET refs_json=? WHERE group_id=? AND round=4", (new_map, gid))
    c.execute("UPDATE groups SET evidence_hash='new', ai_status='ok', review_verdict='approve', brief_status='ok' WHERE id=?", (gid,))
    c.commit()
    assert row(c.execute("SELECT COUNT(*) AS n FROM group_briefs WHERE group_id=? AND round=3 AND enquiry_id=?", (gid, o["id"])))["n"] == 1

    out = enquiries.forget_generated_text(c, [v["id"]])
    c.commit()
    assert out["brief_rows"] == 3 and out["review_rows"] == 1 and out["historical_groups"] == 1 and out["groups_cleared"] == 0, out
    left = rows(c.execute("SELECT round, kind, enquiry_id FROM group_briefs WHERE group_id=? ORDER BY id", (gid,)))
    assert all(r["round"] == 4 for r in left) and {(r["kind"], r["enquiry_id"]) for r in left} == {("member", o["id"]), ("member", n["id"]), ("group", None)}, left
    assert [r["round"] for r in rows(c.execute("SELECT round FROM review_rounds WHERE group_id=?", (gid,)))] == [4]
    # Oscar's surviving brief still compares with U2 - who is Nia by that round's own map, never Victoria
    body = json.loads(row(c.execute("SELECT body_json FROM group_briefs WHERE group_id=? AND round=4 AND enquiry_id=?", (gid, o["id"])))["body_json"])
    assert body["strengths"][0]["compared_with"] == [{"ref": "U2", "fact_ids": ["U2.routine"]}]
    live = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    assert live["evidence_hash"] == "new" and live["ai_status"] == "ok" and live["brief_status"] == "ok"
    assert _mentions(c, "Victoria") == []


# ---------------------------------------------------------------- root's read-only findings (8 Nov, evening)

def _plant_error_text(c, gid, name):
    c.execute("UPDATE groups SET ai_error=?, held_reason=? WHERE id=?", (f"Model reply unusable: it discussed {name} without evidence", f"Held while {name} confirms", gid))
    c.execute("INSERT OR IGNORE INTO group_apartments(group_id, apartment_id, rank, ai_reason) SELECT id, apartment_id, 1, ? FROM groups WHERE id=?", (f"{name} fits this flat", gid))
    c.execute("UPDATE review_rounds SET valid=0, invalid_reason=? WHERE group_id=?", (f"reply named {name} twice", gid))
    c.commit()


def test_staff_delete_also_clears_error_hold_and_apartment_reason_text_about_the_person(client):
    """groups.ai_error (and held_reason / group_apartments.ai_reason / review_rounds.invalid_reason) can quote a person even when no
    late result ever arrives: the current-group cleanup clears them."""
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    c = appdb(client)
    gid = _plant_group(c, v["id"])
    _add_member(c, gid, o["id"])
    _review(c, gid, "Victoria Victim and Oscar Other")
    _plant_error_text(c, gid, "Victoria")
    assert set(_mentions(c, "Victoria")) >= {"groups.ai_error", "groups.held_reason", "group_apartments.ai_reason", "review_rounds.invalid_reason"}
    r = client.delete(f"/api/enquiries/{v['id']}")
    assert r.status_code == 200, r.text
    assert _mentions(c, "Victoria") == [], _mentions(c, "Victoria")
    g = row(c.execute("SELECT ai_error, held_reason, status FROM groups WHERE id=?", (gid,)))
    assert g["ai_error"] is None and "deleted (retention)" in g["held_reason"] and g["status"] == "Proposed"


def test_historical_group_error_text_without_provenance_is_cleared_with_the_erased_history(client):
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    o = _submit(client, first_name="Oscar", last_name="Other", email="oscar@example.com")
    c = appdb(client)
    gid = _plant_group(c, o["id"])                                   # Oscar's group; Victoria was in it EARLIER (keyed brief + rejection)
    _brief(c, gid, "member", {"text": "Victoria Victim is tidy"}, eid=v["id"], ref="U2", rnd=0, at="2026-11-01T00:00:00+00:00")
    c.execute("INSERT INTO member_rejections(group_id, rejected_enquiry_id, other_enquiry_id, at) VALUES (?,?,?,?)", (gid, v["id"], o["id"], "2026-11-02T00:00:00+00:00"))
    c.execute("UPDATE groups SET ai_error='Model reply unusable: it discussed Victoria without evidence' WHERE id=?", (gid,))
    c.commit()
    r = client.delete(f"/api/enquiries/{v['id']}")
    assert r.status_code == 200, r.text
    assert row(c.execute("SELECT ai_error FROM groups WHERE id=?", (gid,)))["ai_error"] is None
    assert _mentions(c, "Victoria") == []
    assert client.get(f"/api/enquiries/{o['id']}").status_code == 200


def _stale(c, eid):
    c.execute("UPDATE enquiries SET last_contact_at='2025-09-01T00:00:00+00:00' WHERE id=?", (eid,))
    c.commit()


def test_purge_decides_eligibility_inside_the_guard_fresh_contact_while_waiting_is_spared(client):
    """Someone contacted after the purge started but before it held the lock must not be deleted: selection runs under the guard."""
    from server import db as dbm
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    w = _submit(client, first_name="Walter", last_name="Waiting", email="walter@example.com")
    c = appdb(client)
    _stale(c, v["id"]); _stale(c, w["id"])

    def contact_walter_on_acquire(ev, conn, label):      # same connection, same thread, inside the guard: models the state the lock-holder committed
        if ev == "acquired" and label == "retention purge":
            conn.execute("UPDATE enquiries SET last_contact_at=? WHERE id=?", (now_iso(), w["id"]))
    dbm.GUARD_EVENTS.append(contact_walter_on_acquire)
    try:
        rep = enquiries.purge_expired(c, "test")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert rep["deleted"] == 1
    assert client.get(f"/api/enquiries/{v['id']}").status_code == 404 and client.get(f"/api/enquiries/{w['id']}").status_code == 200


def test_purge_decides_eligibility_inside_the_guard_fresh_lease_while_waiting_is_spared(client):
    from server import db as dbm
    v = _submit(client, first_name="Victoria", last_name="Victim", email="victim@example.com")
    w = _submit(client, first_name="Walter", last_name="Waiting", email="walter@example.com")
    c = appdb(client)
    gid = _plant_group(c, w["id"])
    _stale(c, v["id"]); _stale(c, w["id"])

    def lease_walter_on_acquire(ev, conn, label):
        if ev == "acquired" and label == "retention purge":
            conn.execute("UPDATE groups SET status='Leased' WHERE id=?", (gid,))
    dbm.GUARD_EVENTS.append(lease_walter_on_acquire)
    try:
        rep = enquiries.purge_expired(c, "test")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert rep["deleted"] == 1
    assert client.get(f"/api/enquiries/{w['id']}").status_code == 200 and client.get(f"/api/enquiries/{v['id']}").status_code == 404


def test_contact_and_group_status_writers_run_under_the_retention_guard(client):
    """The eligibility writers (log contact; the group status chain incl. Leased) take the same guard purge_expired selects under,
    so on any connection a contact or lease cannot land between the purge's eligibility check and its delete."""
    from server import db as dbm
    w = _submit(client, first_name="Walter", last_name="Waiting", email="walter@example.com")
    c = appdb(client)
    gid = _plant_group(c, w["id"])
    mark_introduced(client, gid); accept_all_members(client, gid)   # GP13 setup (introduced + every member's own yes), before the hook
    seen = []
    dbm.GUARD_EVENTS.append(lambda ev, conn, label: seen.append((ev, label)))
    try:
        assert client.post(f"/api/enquiries/{w['id']}/contact", json={"note": "called"}).status_code == 200
        assert client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"}).status_code == 200
    finally:
        dbm.GUARD_EVENTS.clear()
    assert [l for e, l in seen if e == "committed"] == ["log contact", "group status"], seen
    assert not c.in_transaction if getattr(c, "backend", "sqlite") == "sqlite" else c._dirty is False
    assert row(c.execute("SELECT status FROM enquiries WHERE id=?", (w["id"],)))["status"] == "Group confirmed"
