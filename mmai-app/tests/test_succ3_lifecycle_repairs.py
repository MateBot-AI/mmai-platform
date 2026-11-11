"""Succ3 repairs (10 Nov 2026; root SUCC3-REPAIR-ASSIGNMENT, eight consolidated areas). Behaviour under test, each with a control:

L3b  material change on the CURRENT apartment of an introduced group (address / feature, equal figures included) -> refreshed +
     offer-change flag + GP8(b) refresh; on an ALTERNATIVE -> refreshed only (pointer, no flag); non-material -> nothing; refresh
     failure -> whole step rolled back and pending.
L5b/L11  the ORIGINAL loss keeps its own alert identity for the current AND the alternative role, in both job orders, even when an
     earlier step already pruned the option row; the note names the apartment that actually lost (never the still-valid one).
L13  an option that cannot be judged (rule error / member projection unavailable) fails the step with a plain bounded category: no
     silent move, no completed hold, pointer unchanged, work pending, readiness and the public page withheld; recovery completes it.
L14  the public page withholds the apartment block at READ time while the current apartment's work is pending (not only after a
     successful step wrote the flag).
L10  an Introduced group held with a NULL pointer is offered one of ITS OWN valid options again by the next full run (same group id,
     same members, flag stamped); pending work / invalid-only options -> untouched; the manual PATCH stamps the flag the same way.
N4   staff review / brief are labelled current only while their evidence is current at read time (review contract, members' notes);
     reads never write; a legitimate staff hold does not make a current review historical.
Fabricated doubles only: no provider, no mail transport, no customer send (MMAI_AI_QUEUE off; alert rows 'held')."""
import json

import pytest

from server import ai, ai_queue, alerts, db as dbm, matching, rules, stock_lifecycle as sl
from server.db import now_iso, retention_guard, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401  (fixtures)
from tests.test_gp13_group_responses import _introduced, _lookup, _mint, _state
from tests.test_launch_neutral import _apt
from tests.test_stock_lifecycle import KIND, _g, _options, _outbox, _transition, _work, _world
from tests.test_t10_fact_labels import RULE_CODE

A3 = "Unit 3 / 3 Neutral Street"


# ------------------------------------------------------------------------------------------------------------------ helpers
def _add_option(db, gid, a, rank=2):
    """A second stored option for an introduced group, copied from its rank-1 row with the other apartment's own figures."""
    o = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND rank=1", (gid,)))
    cols = [k for k in o.keys() if k not in ("id", "apartment_id", "rank")]
    vals = [o[k] for k in cols]
    db.execute(f"INSERT INTO group_apartments(apartment_id, rank, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' * len(cols))})", (a["id"], rank, *vals))
    db.commit()
    return row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, a["id"])))


def _second_3b(db, a, address=A3):
    return _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=a["rent_12m"], rent6=a["rent_6m"], address=address)


def _introduced_with_alternative(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    g = _g(db, gid)
    a = row(db.execute("SELECT * FROM apartments WHERE id=?", (g["apartment_id"],)))
    alt = _second_3b(db, a)
    _add_option(db, gid, alt)
    db.execute("UPDATE groups SET ai_refresh=0 WHERE id=?", (gid,)); db.commit()
    assert _options(db, gid) == [a["id"], alt["id"]] and g["status"] == "Introduced" and g["offer_changed_since_intro_at"] is None
    return db, gid, a, alt


def _shelve_other_stock(db, keep):
    """The client fixture seeds the sample stock; a 'nothing suitable listed' phase shelves everything but the test's apartments."""
    ph = ",".join("?" * len(keep))
    shelved = rows(db.execute(f"SELECT id, available_from FROM apartments WHERE available_from IS NOT NULL AND id NOT IN ({ph})", tuple(keep)))
    db.execute(f"UPDATE apartments SET available_from=NULL WHERE id NOT IN ({ph})", tuple(keep)); db.commit()
    return shelved


def _unshelve(db, shelved):
    for r in shelved:
        db.execute("UPDATE apartments SET available_from=? WHERE id=?", (r["available_from"], r["id"]))
    db.commit()


def _queued(db, gid):
    g = _g(db, gid)
    return (g["ai_refresh"], g["ai_queued_at"])


# ------------------------------------------------------------------------------------------------------------------ L3b
@pytest.mark.parametrize("field,update", [("address", "UPDATE apartments SET address='Unit 1A / 1 Neutral Street', address_raw='Unit 1A / 1 Neutral Street' WHERE id=?"),
                                          ("feat_balcony", None), ("rent_6m", None)])
def test_material_change_on_the_current_apartment_flags_and_refreshes_even_with_equal_figures(client, monkeypatch, field, update):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    before = _queued(db, gid)
    rec = _transition(db, a["id"], "changed", changed_fields=[field])
    if update:
        db.execute(update, (a["id"],)); db.commit()
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    g = _g(db, gid)
    assert [x["outcome"] for x in out if x["group_id"] == gid] == ["refreshed"]
    assert g["apartment_id"] == a["id"] and g["status"] == "Introduced" and _options(db, gid) == [a["id"], alt["id"]]
    assert g["offer_changed_since_intro_at"], "the current offer's material change is stamped even when the stored figures are equal"
    assert _queued(db, gid) != before and g["ai_refresh"] == 1, "GP8(b) refresh queued"
    assert _outbox(db, gid) == [] and g["rerank_note"] is None   # nothing lost: no attention alert, no loss note
    assert _state(client, gid)["version"]["version"] == 1   # GP13 untouched


def test_material_change_on_an_alternative_refreshes_without_touching_the_offer(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    before = _queued(db, gid)
    rec = _transition(db, alt["id"], "changed", changed_fields=["feat_balcony"])
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    g = _g(db, gid)
    assert [x["outcome"] for x in out if x["group_id"] == gid] == ["refreshed"]
    assert g["apartment_id"] == a["id"] and g["offer_changed_since_intro_at"] is None, "an alternative's change never flags the current offer"
    assert _queued(db, gid) != before and g["ai_refresh"] == 1 and _options(db, gid) == [a["id"], alt["id"]]
    # a second alternative change while the refresh is still queued is idempotent on the queue marks (the group keeps its place)
    rec2 = _transition(db, alt["id"], "changed", changed_fields=["rent_6m"])
    out2 = sl.run_pending(db, transition_id=rec2["transition_id"])
    assert [x["outcome"] for x in out2 if x["group_id"] == gid] == ["refreshed"] and _g(db, gid)["ai_refresh"] == 1


def test_proposed_groups_are_refreshed_but_never_flagged_and_non_material_edits_record_nothing(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db = appdb(client)
    a1, a2, gids = _world(db)
    r = client.patch(f"/api/apartments/{a1['id']}", json={"address": "Unit 1B / 1 Neutral Street"})
    assert r.status_code == 200, r.text
    fu = r.json()["groups_rechecked"]
    assert fu["status"] == "complete" and {x["outcome"] for x in fu["affected"]} == {"refreshed"}
    for gid in gids:
        assert _g(db, gid)["offer_changed_since_intro_at"] is None and _g(db, gid)["apartment_id"] == a1["id"]   # Proposed: no display flag
    n = db.execute("SELECT COUNT(*) FROM stock_transitions").fetchone()[0]
    r = client.patch(f"/api/apartments/{a1['id']}", json={"parking_note": "Permit from the building manager"})
    assert r.status_code == 200 and "groups_rechecked" not in r.json()
    assert db.execute("SELECT COUNT(*) FROM stock_transitions").fetchone()[0] == n   # non-material: no transition, no work


def test_refresh_failure_rolls_the_whole_step_back_and_the_retry_completes_it(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    real = ai_queue.enqueue
    calls = []

    def flaky(c, ids, **kw):
        if not calls:
            calls.append(1)
            raise RuntimeError("queue mark unavailable")
        return real(c, ids, **kw)
    monkeypatch.setattr(ai_queue, "enqueue", flaky)
    rec = _transition(db, a["id"], "changed", changed_fields=["address"])
    failed = []
    out = sl.run_pending(db, transition_id=rec["transition_id"], failures=failed)
    assert out == [] and failed == [{"group_id": gid, "category": "RuntimeError"}]
    g = _g(db, gid)
    assert g["offer_changed_since_intro_at"] is None and g["ai_refresh"] == 0 and sl.group_has_pending_work(db, gid)   # rolled back whole
    assert not ai_queue.is_ready(db, g)["ready"]
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert [x["outcome"] for x in out] == ["refreshed"] and _g(db, gid)["offer_changed_since_intro_at"] and _g(db, gid)["ai_refresh"] == 1
    assert not sl.group_has_pending_work(db, gid)


# ------------------------------------------------------------------------------------------------------------------ L5b / L11
@pytest.mark.parametrize("order", ["AB", "BA"])
def test_current_withdrawn_and_alternative_rule_broken_each_keep_their_own_alert_in_both_orders(db, monkeypatch, order):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    rA = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    rB = _transition(db, a2["id"], "changed", changed_fields=["rent_12m", "rent_6m"]); db.execute("UPDATE apartments SET rent_12m=9000, rent_6m=9000 WHERE id=?", (a2["id"],)); db.commit()
    first, second = (rA, rB) if order == "AB" else (rB, rA)
    sl.run_pending(db, transition_id=first["transition_id"])
    for gid in gids:
        # the first step judges every option against today's stock and rules: both are gone, the group is held at once
        assert _g(db, gid)["apartment_id"] is None and _options(db, gid) == []
        assert [o["event_id"] for o in _outbox(db, gid)] == [alerts.event_id_for(KIND, "group", gid, first["transition_id"])]
    sl.run_pending(db, transition_id=second["transition_id"])
    for gid in gids:
        # the second transition's loss is its own, although its option row was already pruned: re-evaluated independently -> alert
        ev = sorted(o["event_id"] for o in _outbox(db, gid))
        assert ev == sorted([alerts.event_id_for(KIND, "group", gid, rA["transition_id"]), alerts.event_id_for(KIND, "group", gid, rB["transition_id"])])
        assert _g(db, gid)["apartment_id"] is None and _g(db, gid)["status"] == "Proposed"
    assert {w["status"] for w in _work(db)} == {"done"} and {w["outcome"] for w in _work(db, transition_id=second["transition_id"])} == {"no_apartment"}


def test_note_names_the_current_apartment_that_lost_not_the_valid_alternative_whose_step_ran_first(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    # a non-replace import shape: the alternative's rent changes (still valid), the current's availability is withdrawn in the same
    # batch; the alternative's step runs first
    rB = _transition(db, a2["id"], "changed", changed_fields=["rent_12m"]); db.execute("UPDATE apartments SET rent_12m=rent_12m+5 WHERE id=?", (a2["id"],)); db.commit()
    rA = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    out = sl.run_pending(db, transition_id=rB["transition_id"])
    for x in out:
        gid = x["group_id"]
        assert x["outcome"] == "moved" and _g(db, gid)["apartment_id"] == a2["id"]
        note = _g(db, gid)["rerank_note"]
        assert note.startswith(f"{a2['address']} changed on") and "still passes every rule" in note
        assert f"The group's current apartment, {a1['address']}, no longer passes every rule for this group (was withdrawn)" in note
        assert "next best apartment that still passes every rule, " + a2["address"] in note
        assert _outbox(db, gid) == []   # a2 is not lost: no loss alert from its step
    sl.run_pending(db, transition_id=rA["transition_id"])
    for gid in gids:
        # a1's own transition still records a1's loss with its own identity (option row already gone)
        assert [o["event_id"] for o in _outbox(db, gid)] == [alerts.event_id_for(KIND, "group", gid, rA["transition_id"])]
        assert _g(db, gid)["apartment_id"] == a2["id"]   # a later loss does not move the group off its valid new offer
        assert _g(db, gid)["rerank_note"].startswith(f"{a1['address']} was withdrawn") and "no longer on this group's list" in _g(db, gid)["rerank_note"]


def test_import_non_replace_update_keeps_the_lost_apartments_alert_identity(db, tmp_path, monkeypatch):
    """Import update (no replace) that withdraws the current and changes an alternative in one batch: both transitions keep their
    identity, work drains in id order, exactly one alert per lost apartment per group."""
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    with retention_guard(db, label="import batch"):
        rA = sl.record_transition(db, a1["id"], "withdrawn", "importer", changed_fields=["available_from"])
        db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],))
        rB = sl.record_transition(db, a2["id"], "changed", "importer", changed_fields=["rent_6m"])
        db.execute("UPDATE apartments SET rent_6m=rent_6m+1 WHERE id=?", (a2["id"],))
    assert len(sl.pending_work(db)) == 2 * len(gids)
    sl.run_pending(db)   # id order: a1's loss first
    for gid in gids:
        assert [o["event_id"] for o in _outbox(db, gid)] == [alerts.event_id_for(KIND, "group", gid, rA["transition_id"])]
        assert _g(db, gid)["apartment_id"] == a2["id"] and _options(db, gid) == [a2["id"]]
    assert {w["outcome"] for w in _work(db, transition_id=rB["transition_id"])} == {"refreshed"}


# ------------------------------------------------------------------------------------------------------------------ L13 / L14
def test_rule_error_leaves_the_step_pending_with_a_bounded_category_and_the_page_withheld(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    people = _state(client, gid)["people"]
    tok = _mint(client, gid, people[0]["person_uid"])["token"]
    assert _lookup(client, tok).json()["group"]["address"]
    real = rules.check_group

    def broken(*a_, **k_):
        raise ZeroDivisionError("fault injected: internal rule detail must not leak")
    monkeypatch.setattr(rules, "check_group", broken)
    rec = _transition(db, a["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a["id"],)); db.commit()
    failed = []
    out = sl.run_pending(db, transition_id=rec["transition_id"], failures=failed)
    assert out == [] and failed == [{"group_id": gid, "category": "OptionsNotJudgeable"}]
    g = _g(db, gid)
    assert g["apartment_id"] == a["id"] and _options(db, gid) == [a["id"], alt["id"]], "no silent move onto an unjudged option, no completed hold"
    assert g["status"] == "Introduced" and g["offer_changed_since_intro_at"] is None and _outbox(db, gid) == [] and g["rerank_note"] is None
    assert sl.group_has_pending_work(db, gid) and not ai_queue.is_ready(db, g)["ready"] and not ai_queue.can_circulate(db, g)["ok"]
    assert client.get(f"/api/groups/{gid}").json()["stock_recheck_pending"] is True
    assert client.patch(f"/api/groups/{gid}", json={"status": "Leased"}).status_code == 409
    # L14: the public page withholds the apartment block at READ time (the step wrote nothing), the answer is still taken
    view = _lookup(client, tok).json()["group"]
    assert view["apartment_changed"] is True and view["address"] is None and view["proposed_weekly_share"] is None
    # recovery: the rule check works again -> the pending step completes, moves to the judged-valid alternative, alerts once
    monkeypatch.setattr(rules, "check_group", real)
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert [x["outcome"] for x in out] == ["moved"] and _g(db, gid)["apartment_id"] == alt["id"] and _g(db, gid)["offer_changed_since_intro_at"]
    assert len(_outbox(db, gid)) == 1 and not sl.group_has_pending_work(db, gid)
    assert _state(client, gid)["version"]["version"] == 1


def test_unreadable_member_projection_is_not_a_pass_and_recovers(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    monkeypatch.setattr(sl, "_group_units", lambda c, g: None)
    rec = _transition(db, alt["id"], "changed", changed_fields=["rent_12m"]); db.execute("UPDATE apartments SET rent_12m=rent_12m+10 WHERE id=?", (alt["id"],)); db.commit()
    failed = []
    assert sl.run_pending(db, transition_id=rec["transition_id"], failures=failed) == [] and failed == [{"group_id": gid, "category": "OptionsNotJudgeable"}]
    assert sl.group_has_pending_work(db, gid) and _g(db, gid)["apartment_id"] == a["id"] and _options(db, gid) == [a["id"], alt["id"]]
    # stock-only facts are still judged without members: a withdrawn alternative is removed even while members cannot be read?
    # No - the step is one unit: it stays pending until every option can be judged (no partial judgement)
    monkeypatch.undo()
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert [x["outcome"] for x in out] == ["refreshed"] and not sl.group_has_pending_work(db, gid)


# ------------------------------------------------------------------------------------------------------------------ L10
def test_full_run_re_offers_a_held_introduced_groups_own_valid_option_and_stamps_the_flag(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    members = rows(db.execute("SELECT enquiry_id, room, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))
    # a NULL hold left with the group's own options still stored (the shape the retained Introduced groups are in)
    db.execute("UPDATE groups SET apartment_id=NULL, offer_changed_since_intro_at=NULL, updated_at=? WHERE id=?", (now_iso(), gid)); db.commit()
    assert _g(db, gid)["apartment_id"] is None and _g(db, gid)["status"] == "Introduced"
    res = matching.full_run(db, "tester", dataset="test")
    g = _g(db, gid)
    assert g["id"] == gid and g["status"] == "Introduced" and g["apartment_id"] == a["id"], "re-offered within the same group, best own option first"
    assert g["offer_changed_since_intro_at"], "the public page withholds the re-offered apartment until staff acknowledge"
    assert rows(db.execute("SELECT enquiry_id, room, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,))) == members
    assert _state(client, gid)["version"]["version"] == 1 and g["ai_refresh"] == 1
    assert row(db.execute("SELECT detail FROM audit_log WHERE action='group_reoffered' AND entity_id=?", (str(gid),)))


def test_full_run_leaves_a_held_group_alone_when_work_is_pending_or_no_own_option_is_valid(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    db.execute("UPDATE groups SET apartment_id=NULL WHERE id=?", (gid,)); db.commit()
    # (a) pending work on one of its apartments: not touched by the run
    rec = _transition(db, alt["id"], "changed", changed_fields=["rent_6m"])
    matching.full_run(db, "tester", dataset="test")
    assert _g(db, gid)["apartment_id"] is None and _g(db, gid)["offer_changed_since_intro_at"] is None
    # the pending step itself, once it runs, re-offers the best judged-valid own option (same switch, flag stamped)
    sl.run_pending(db, transition_id=rec["transition_id"])
    assert _g(db, gid)["apartment_id"] == a["id"] and _g(db, gid)["offer_changed_since_intro_at"]
    db.execute("UPDATE groups SET apartment_id=NULL, offer_changed_since_intro_at=NULL WHERE id=?", (gid,)); db.commit()
    # (b) every own option invalid (withdrawn / leased elsewhere) and nothing suitable listed anywhere: no forced re-offer, the
    #     hold stands. Before 10 Nov follow-up: the invalid own options stayed stored and the hold was perpetual whatever the
    #     stock; after: invalid options are pruned (only judged-valid options are kept) and the member set is re-checked against
    #     today's available stock - so the seeded stock is shelved here to show the hold itself, and (d) shows the restock.
    shelved = _shelve_other_stock(db, (a["id"], alt["id"]))
    db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a["id"],))
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=999999 WHERE id=?", (now_iso(), alt["id"])); db.commit()
    matching.full_run(db, "tester", dataset="test")
    assert _g(db, gid)["apartment_id"] is None and _g(db, gid)["status"] == "Introduced" and _options(db, gid) == []
    # (c) the alternative becomes available again -> offered from today's stock (the withdrawn one is skipped, not forced)
    db.execute("UPDATE apartments SET leased_at=NULL, leased_group_id=NULL WHERE id=?", (alt["id"],)); db.commit()
    matching.full_run(db, "tester", dataset="test")
    assert _g(db, gid)["apartment_id"] == alt["id"] and _g(db, gid)["offer_changed_since_intro_at"] and _options(db, gid) == [alt["id"]]
    # (d) held again with the alternative gone for good and the wider stock back: a listed 3-bedroom that passes every rule for
    #     these members today is offered within the same group (never a different member set)
    db.execute("UPDATE groups SET apartment_id=NULL, offer_changed_since_intro_at=NULL WHERE id=?", (gid,))
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=999999 WHERE id=?", (now_iso(), alt["id"])); db.commit()
    _unshelve(db, shelved)
    matching.full_run(db, "tester", dataset="test")
    g = _g(db, gid)
    assert g["apartment_id"] not in (None, a["id"], alt["id"]) and g["status"] == "Introduced" and g["offer_changed_since_intro_at"]
    assert row(db.execute("SELECT type_code, available_from, leased_at FROM apartments WHERE id=?", (g["apartment_id"],)))["type_code"] == "3B"


def test_manual_offer_on_a_held_introduced_group_goes_through_the_flagging_switch(client, monkeypatch):
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    db.execute("UPDATE groups SET apartment_id=NULL WHERE id=?", (gid,)); db.commit()
    r = client.patch(f"/api/groups/{gid}", json={"apartment_id": alt["id"]})
    assert r.status_code == 200, r.text
    g = _g(db, gid)
    assert g["apartment_id"] == alt["id"] and g["offer_changed_since_intro_at"] and g["status"] == "Introduced"
    assert _state(client, gid)["version"]["version"] == 1
    # an apartment not on the group's list is still refused
    other = _second_3b(db, a, address="Unit 9 / 9 Neutral Street")
    assert client.patch(f"/api/groups/{gid}", json={"apartment_id": other["id"]}).status_code in (400, 409, 422)


# ------------------------------------------------------------------------------------------------------------------ N4
def test_review_and_brief_currency_is_decided_at_read_time_without_writing(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    g0 = _g(db, gid)
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"] and d["review"]["current"] is True and d["review"]["label"].startswith("Current AI review") and d["review"]["currency_reasons"] == []
    if d.get("brief") and d["brief"]["status"] == "ok":
        assert d["brief"]["current"] is True and d["brief"]["label"] == "Current staff brief"
    cur = ai_queue.evidence_currency(db, g0)
    assert cur == {"current": True, "reasons": [], "carveout": False}
    # the review contract moves on (a later prompt/schema): the SAME stored review is history at read time, nothing is written
    contract0 = ai.REVIEW_CONTRACT
    monkeypatch.setattr(ai, "REVIEW_CONTRACT", contract0 + ".succ3-test")
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"]["current"] is False and d["review"]["label"].startswith("Earlier AI review") and any("earlier review contract" in r for r in d["review"]["currency_reasons"])
    if d.get("brief") and d["brief"]["status"] == "ok":
        assert d["brief"]["current"] is False and d["brief"]["label"].startswith("Earlier staff brief")
    assert _g(db, gid) == g0, "reads do not write"
    assert not ai_queue.is_ready(db, _g(db, gid))["ready"]   # the gate agrees (the label is not a substitute for it)
    monkeypatch.setattr(ai, "REVIEW_CONTRACT", contract0)
    dd = client.get(f"/api/groups/{gid}").json(); assert dd["review"]["current"] is True, (dd["review"]["currency_reasons"], dd["review"]["label"])
    # a member's notes awaiting reading: the review no longer rests on every member's current validated notes
    ns0 = row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (lead["id"],)))["notes_status"]
    db.execute("UPDATE enquiries SET notes_status='pending' WHERE id=?", (lead["id"],)); db.commit()
    d = client.get(f"/api/groups/{gid}").json()
    assert d["review"]["current"] is False and any("not yet read" in r for r in d["review"]["currency_reasons"])
    db.execute("UPDATE enquiries SET notes_status=? WHERE id=?", (ns0, lead["id"])); db.commit()
    dd = client.get(f"/api/groups/{gid}").json(); assert dd["review"]["current"] is True, (dd["review"]["currency_reasons"], dd["review"]["label"])


def test_a_staff_hold_does_not_make_a_current_review_historical(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    g = _g(db, gid)
    cur = ai_queue.evidence_currency(db, dict(g, held_reason="staff hold: awaiting a document"))
    assert cur["current"] is True and cur["reasons"] == []
    cur = ai_queue.evidence_currency(db, dict(g, status="Awaiting replacement"))
    assert cur["current"] is True   # status / ownership are for is_ready, not currency


# ------------------------------------------------------------------------------------------------------------------ N4 (root follow-up): carve-out
def test_gp8_carveout_keeps_readiness_but_labels_the_earlier_offer_prose_historical(db, monkeypatch):
    """Positive-backed control: the same group is Current before the carve-out and historical after it, while is_ready stands."""
    from tests.test_t6_fail_closed import _good, _reply_builder
    from tests.test_launch_neutral import _pool
    a1 = _apt(db, address="Unit 1 / 1 Neutral Street")
    a2 = _apt(db, address="Unit 2 / 2 Neutral Street", rent12=1160.0, rent6=1235.0)
    _pool(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    matching.full_run(db, "tester", dataset="test")
    ai_queue.process_pending(db)
    g = row(db.execute("SELECT * FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id LIMIT 1"))
    assert ai_queue.is_ready(db, g)["ready"]
    cur0 = ai_queue.evidence_currency(db, g)
    assert cur0 == {"current": True, "reasons": [], "carveout": False}
    from server import staff_view
    rv0 = staff_view.review_view(db, g["id"], g)
    assert rv0 and rv0["current"] is True
    other = row(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],)))
    matching.mark_apartment_leased(db, g["apartment_id"], "tester", group_id=other["id"] if other else None)
    g2 = _g(db, g["id"])
    rd = ai_queue.is_ready(db, g2)
    assert rd["ready"] and rd["carveout"] is True, rd           # readiness stands (GP8(a)/(b))
    cur = ai_queue.evidence_currency(db, g2)
    assert cur["current"] is False and cur["carveout"] is True and any("offered apartments changed" in r and "readiness still stands under the availability exception" in r for r in cur["reasons"])
    assert not any(RULE_CODE.search(r) for r in cur["reasons"]), cur["reasons"]   # T10: plain wording, no bare register code
    rv = staff_view.review_view(db, g["id"], g2)
    assert rv["current"] is False and rv["label"].startswith("Earlier AI review") and rv["currency_reasons"] == cur["reasons"]
    bv = staff_view.brief_view(db, g["id"], g2)
    if bv and bv["status"] == "ok":
        assert bv["current"] is False and bv["label"].startswith("Earlier staff brief")
    assert _g(db, g["id"]) == g2, "reads do not write"


# ------------------------------------------------------------------------------------------------------------------ L10 (root follow-up): empty own list
def test_full_run_re_offers_a_held_introduced_group_from_available_stock_when_its_own_list_is_empty(client, monkeypatch):
    """Sole offer lost and pruned (own list empty), then the apartment is relisted / another suitable one is listed: the next full
    run offers it to the SAME group (id, members, status, GP13 version, clock untouched) after every hard rule passes today."""
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    _shelve_other_stock(db, (a["id"], alt["id"]))
    members = rows(db.execute("SELECT enquiry_id, room, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))
    g0 = _g(db, gid)
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, alt["id"])); db.commit()
    rec = _transition(db, a["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a["id"],)); db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    assert _g(db, gid)["apartment_id"] is None and _options(db, gid) == [] and _g(db, gid)["status"] == "Introduced"
    # nothing suitable listed (alt is leased elsewhere): the hold stands, no forced offer
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=999999 WHERE id=?", (now_iso(), alt["id"])); db.commit()
    matching.full_run(db, "tester", dataset="test")
    assert _g(db, gid)["apartment_id"] is None and _options(db, gid) == []
    # the alternative comes back on the market: offered to this group from today's stock, as a stored option, flag stamped
    db.execute("UPDATE apartments SET leased_at=NULL, leased_group_id=NULL WHERE id=?", (alt["id"],)); db.commit()
    matching.full_run(db, "tester", dataset="test")
    g = _g(db, gid)
    assert g["id"] == gid and g["status"] == "Introduced" and g["apartment_id"] == alt["id"] and g["offer_changed_since_intro_at"]
    assert _options(db, gid) == [alt["id"]] and g["weekly_rent"] and g["total_weekly"] and g["lease_start"]
    assert rows(db.execute("SELECT enquiry_id, room, weekly_share FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,))) == members
    assert _state(client, gid)["version"]["version"] == 1 and g["member_key"] == g0["member_key"] and g["created_at"] == g0["created_at"]
    assert row(db.execute("SELECT detail FROM audit_log WHERE action='group_reoffered' AND entity_id=?", (str(gid),)))
    # a relist of the withdrawn original adds it as a further own option on the next run without moving the pointer
    matching.relist_apartment(db, a["id"], "2027-02-01", "tester")
    sl.run_pending(db, apartment_id=a["id"])
    matching.full_run(db, "tester", dataset="test")
    assert _g(db, gid)["apartment_id"] == alt["id"]   # a valid current offer is never switched by the run


def test_replace_import_hold_then_run_re_offers_within_the_member_set_only(client, monkeypatch):
    """The replacement-import shape: the whole list is replaced; the retained Introduced group is held NULL with an empty list;
    the run offers a replacement-list apartment that passes every rule for ITS members - Proposed primaries / GP10 untouched."""
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    db.execute("DELETE FROM group_apartments WHERE group_id=?", (gid,))
    db.execute("UPDATE groups SET apartment_id=NULL WHERE id=?", (gid,))
    db.execute("UPDATE apartments SET available_from=NULL WHERE id IN (?, ?)", (a["id"], alt["id"])); db.commit()
    _shelve_other_stock(db, (a["id"], alt["id"]))
    before_props = rows(db.execute("SELECT id, member_key, is_primary, apartment_id FROM groups WHERE status='Proposed' ORDER BY id"))
    new = _second_3b(db, a, address="Unit 7 / 7 Replacement Street")
    matching.full_run(db, "tester", dataset="test")
    g = _g(db, gid)
    assert g["apartment_id"] == new["id"] and g["status"] == "Introduced" and g["offer_changed_since_intro_at"] and _options(db, gid) == [new["id"]]
    # the new apartment's rule check for THIS member set was made (figures stored from today's rules)
    o = row(db.execute("SELECT * FROM group_apartments WHERE group_id=?", (gid,)))
    assert o["weekly_rent"] == new["rent_12m"] or o["weekly_rent"] == new["rent_6m"]
    kept = {p["id"]: p for p in before_props}
    for p in rows(db.execute("SELECT id, member_key, is_primary, apartment_id FROM groups WHERE status='Proposed' AND id IN (%s)" % ",".join("?" * max(1, len(kept))), list(kept) or [0])):
        assert p["member_key"] == kept[p["id"]]["member_key"]   # retained Proposed groups keep their member sets; the run decides their offers as before


# ------------------------------------------------------------------------------------------------------------------ T10: the stock note itself
def test_budget_loss_note_is_plain_staff_wording_with_the_amount_and_no_register_code(client, monkeypatch):
    """L13 shape: the current apartment's rent rises past the household's budget. The note keeps the cause and the figures but
    quotes no raw Register code (T10: the staff page renders rerank_note as-is). Before 10 Nov follow-up: '(exact limit $1243,
    A16)' reached the stored note; after: the rule label stands in for the code, nothing else in the reason changes."""
    import re
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    _shelve_other_stock(db, (a["id"], alt["id"]))
    db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (alt["id"],)); db.commit()   # only the current remains
    rec = _transition(db, a["id"], "changed", changed_fields=["rent_6m", "rent_12m"])
    db.execute("UPDATE apartments SET rent_6m=rent_6m+5000, rent_12m=rent_12m+5000 WHERE id=?", (a["id"],)); db.commit()
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert [x["outcome"] for x in out if x["group_id"] == gid] == ["no_apartment"]
    g = _g(db, gid)
    note = g["rerank_note"]
    assert note.startswith(f"{a['address']} changed on") and "no longer passes every rule for this group" in note and "held without an apartment" in note
    assert "$" in note and "exact limit" in note and "the total-rent allowance" in note, note
    assert not RULE_CODE.search(note), note
    assert not re.search(r"Traceback|Error\b|psycopg|sqlite3", note)
    d = client.get(f"/api/groups/{gid}").json()
    assert d["rerank_note"] == note and not RULE_CODE.search(d["rerank_note"])
    lst = client.get("/api/groups", params={"dataset": "test"}).json()["items"]
    mine = [x for x in lst if x["id"] == gid]
    assert mine and not RULE_CODE.search(mine[0].get("rerank_note") or "")


# ------------------------------------------------------------------------------------------------------------------ L10b: transaction ownership
def test_full_run_with_pending_refresh_marks_and_a_held_group_commits_the_save_step_before_the_re_offer_steps(db, monkeypatch):
    """Reviewer L10b (10 Nov 2026): a run that (a) changes a reviewed Proposed group's list (GP8(b) refresh mark) and (b) finds a
    held Introduced group entered the per-group re-offer guard with the refresh mark still pending -> DirtyConnectionAtGuardEntry
    aborted the run. Repair: the refresh marks are written inside the save step (they describe groups that step changed), which
    commits them with the group changes; the re-offer steps start clean. The guard itself is unchanged (still refuses a dirty
    entry), the caller contract is unchanged (full_run returns its summary), and both outcomes are coherent after one call."""
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    # (a) a reviewed Proposed group whose list will change on the next run: a third apartment is listed (its options grow)
    reviewed = gids[0]
    db.execute("UPDATE groups SET ai_status='ok', rule_version=?, ai_model=?, evidence_hash=COALESCE(evidence_hash, 'h0') WHERE id=?", (rules.RULE_VERSION, ai.MODEL, reviewed))
    # (b) a held Introduced group: raw status (the L10b shape), sole option withdrawn then relisted - own list empty, pointer NULL
    held = gids[-1]
    assert held != reviewed
    db.execute("UPDATE groups SET status='Introduced' WHERE id=?", (held,))
    db.execute("DELETE FROM group_apartments WHERE group_id=?", (held,))
    db.execute("UPDATE groups SET apartment_id=NULL WHERE id=?", (held,)); db.commit()
    held_members = rows(db.execute("SELECT enquiry_id, room FROM group_members WHERE group_id=? ORDER BY enquiry_id", (held,)))
    a3 = _apt(db, address="Unit 3 / 3 Neutral Street", rent12=1170.0, rent6=1245.0)
    before = _g(db, reviewed)
    out = matching.full_run(db, "tester", dataset="test")   # no DirtyConnectionAtGuardEntry
    assert out["run_id"] and out["complete"] and not getattr(db, "in_transaction", False)
    g = _g(db, reviewed)
    assert g["id"] == reviewed and g["status"] == "Proposed" and g["ai_status"] == "ok" and g["ai_refresh"] == 1 and g["ai_queued_at"]
    assert a3["id"] in _options(db, reviewed) and out["apartment_refresh_queued"] >= 2
    assert g["apartment_id"] == before["apartment_id"]   # GP8(b): the reviewed group's offer stands, the ranking refresh is queued
    h = _g(db, held)
    assert h["status"] == "Introduced" and h["apartment_id"] in (a1["id"], a2["id"], a3["id"]) and h["offer_changed_since_intro_at"]
    assert h["ai_status"] == "pending" and h["ai_queued_at"]   # never reviewed in this raw shape: queued as pending (a refresh mark is for reviewed groups)
    assert rows(db.execute("SELECT enquiry_id, room FROM group_members WHERE group_id=? ORDER BY enquiry_id", (held,))) == held_members
    assert row(db.execute("SELECT detail FROM audit_log WHERE action='group_reoffered' AND entity_id=?", (str(held),)))
    assert row(db.execute("SELECT status FROM match_runs WHERE id=?", (out["run_id"],)))["status"] == "done"


# ------------------------------------------------------------------------------------------------------------------ T10: held group header (source pin)
def test_staff_group_header_omits_the_apartment_link_when_there_is_no_current_offer():
    """Root browser QA on 1794235361: a held group's header rendered an anchor to #/staff/apartments/null. The anchor is emitted only
    with a real pointer; a NULL pointer shows the plain 'No current offer' words (the card below explains the hold)."""
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("client", "staff.js").read_text(encoding="utf-8")
    head = [ln for ln in src.splitlines() if "pane-head" in ln and "<h1>Group ${g.id}</h1>" in ln]
    assert len(head) == 1, head
    assert re.search(r"g\.apartment_id != null \? `<a class=\"small\" href=\"#/staff/apartments/\$\{g\.apartment_id\}\">", head[0]), head[0]
    assert "No current offer" in head[0]
    assert not re.search(r"<a class=\"small\" href=\"#/staff/apartments/\$\{g\.apartment_id\}\">[^`]*</a></div>$", head[0])   # the old unconditional anchor is gone
