"""Fifth repair (9 Nov 2026, root actual-API control): H19 says to email reservations when a previously suggested apartment becomes
unavailable, so staff can communicate manually. The lease follow-up re-ranked every affected MateBot group but queued NO internal
alert (alerts.EVENTS had only HomeBot and GP13 kinds). Now stock_lifecycle.reconcile_work (via matching.rerank_after_lease) raises the fixed 'group_apartment_unavailable'
event inside the group's own durable rerank step: one row per affected live group and leased apartment (idempotent event id),
staff-only fixed body (no address, no names), TEST data flagged and held by default, a repeated claim or retry adds nothing, a group
whose step failed gets its alert exactly once when the pending rerank is retried, and a lease nobody listed raises nothing.
Fabricated doubles only; no provider, no mail transport (MMAI_ALERT_MODE off -> rows are 'held')."""
from server import alerts, db as dbmod, matching
from server.db import row, rows
from tests.test_data_and_api import client, appdb  # noqa: F401  (fixtures for the actual-route check)
from tests.test_launch_neutral import _apt, _pool

KIND = "group_apartment_unavailable"


def _outbox(c):
    return rows(c.execute("SELECT * FROM internal_alert_outbox ORDER BY id"))


def _rev(c, aid, gen):
    """The alert revision for one lease of one apartment. BEFORE (five-fix): matching._lease_event_revision(apartment id, generation)
    = apartment_id * 10**6 + generation % 10**6. AFTER (stock lifecycle, 9 Nov 2026, root F1 'collision-free transition IDs over
    modulo-million event revisions'): the revision is the id of the recorded 'leased' stock transition for that generation. The
    pinned property is unchanged - exactly one event per group and lease generation, a retry adds nothing, the next lease of the
    same apartment is a new event - only the number comes from the durable transition row."""
    t = row(c.execute("SELECT id FROM stock_transitions WHERE apartment_id=? AND cause='leased' AND generation=? ORDER BY id DESC LIMIT 1", (aid, gen)))
    assert t, f"no recorded lease transition for apartment {aid} generation {gen}"
    return int(t["id"])


def _lease(db, a, g):
    when = "2026-11-09T10:00:00"
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester', lease_generation=lease_generation+1 WHERE id=? AND leased_at IS NULL", (when, g["id"], a["id"])); db.commit()   # the route's durable claim (app._claim_apartment)
    return when, matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at=when)


def test_every_affected_group_gets_one_held_staff_alert_and_the_body_is_fixed(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    others = sorted(r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],))))
    assert len(others) >= 2 and _outbox(db) == []
    when, out = _lease(db, a, g)
    assert out["rerank"]["status"] == "complete" and sorted(x["group_id"] for x in out["affected"]) == others
    ob = _outbox(db)
    assert sorted(r["record_id"] for r in ob) == others and len(ob) == len(others)
    for r in ob:
        assert r["kind"] == KIND and r["record_type"] == "group" and r["to_email"] == alerts.RECIPIENT
        assert r["is_test"] == 1 and r["status"] == "held"   # test data, alerts off: queued for a person to see, never attempted
        assert r["event_id"] == alerts.event_id_for(KIND, "group", r["record_id"], _rev(db, a["id"], 1))   # first lease of this apartment
        msg = alerts.render(r)
        assert "A'Beckett" not in msg["text"] and "Neutral Street" not in msg["text"] and "@example.com" not in msg["text"]
        assert f"Group-{r['record_id']:05d}" in msg["text"] and "(TEST data)" in msg["text"] and "#/staff/groups/" in msg["text"]
    assert not db.in_transaction
    # a repeated claim is refused by the verify step and the retry path finds nothing: no second row for any group
    try:
        matching.mark_apartment_leased(db, a["id"], "tester", g["id"], claimed_at="2026-11-09T10:00:01")
        assert False, "a claim that did not land must be refused"
    except ValueError:
        pass
    assert matching.retry_pending_lease_reranks(db, "tester") == {"retried": 0, "affected": [], "failed": []}
    matching.rerank_after_lease(db, a["id"], a["address"], when, exclude_group_id=g["id"])   # direct re-run of the follow-up: idempotent
    assert len(_outbox(db)) == len(others)


def test_relist_and_second_lease_of_the_same_apartment_alerts_the_same_group_again(db):
    """dedupe is per lease generation, not per apartment: a retry of one lease adds nothing, the NEXT lease is a new event"""
    a1 = _apt(db)
    a2 = _apt(db, address="Unit 2 / 2 Neutral Street", rent12=1160.0, rent6=1235.0)
    _pool(db)
    matching.full_run(db, "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1 ORDER BY id LIMIT 1"))
    others = sorted(r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],))))
    listed_a1 = sorted(r["group_id"] for r in rows(db.execute("SELECT DISTINCT group_id FROM group_apartments WHERE apartment_id=? AND group_id<>?", (a1["id"], g["id"]))))
    assert listed_a1
    when, out = _lease(db, a1, g)
    first = {r["record_id"]: r["event_id"] for r in _outbox(db)}
    assert sorted(first) == listed_a1 and all(e.endswith(f":{_rev(db, a1['id'], 1)}") for e in first.values())
    assert row(db.execute("SELECT lease_generation FROM apartments WHERE id=?", (a1["id"],)))["lease_generation"] == 1
    # relist (generation unchanged), the next run offers it again, then the SAME apartment is leased a second time
    matching.relist_apartment(db, a1["id"], "2027-01-27", "tester")
    db.execute("UPDATE groups SET status='Proposed' WHERE id=?", (g["id"],)); db.commit()   # fabricated: the leasing group's lease fell through
    matching.full_run(db, "tester", dataset="test")
    again = sorted(r["group_id"] for r in rows(db.execute("SELECT DISTINCT group_id FROM group_apartments WHERE apartment_id=? AND group_id<>?", (a1["id"], g["id"]))))
    assert again
    when2 = "2026-11-10T10:00:00"
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester', lease_generation=lease_generation+1 WHERE id=?", (when2, g["id"], a1["id"])); db.commit()
    matching.mark_apartment_leased(db, a1["id"], "tester", g["id"], claimed_at=when2)
    second = [r for r in _outbox(db) if r["record_id"] in again and r["event_id"] not in first.values()]
    assert sorted(r["record_id"] for r in second) == again and all(r["event_id"].endswith(f":{_rev(db, a1['id'], 2)}") for r in second)
    # the first lease's rows are untouched history, and a retry of the second lease adds nothing
    n = len(_outbox(db))
    matching.rerank_after_lease(db, a1["id"], a1["address"], when2, exclude_group_id=g["id"])
    assert len(_outbox(db)) == n


def test_outbox_write_failure_leaves_the_step_pending_and_the_retry_recovers_rerank_and_alert(db, monkeypatch):
    """a durable rerank can never silently lose its alert: a failed outbox write fails the step (rolled back), the retry does both"""
    from server import alerts as alerts_mod
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    others = sorted(r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],))))
    victim = others[0]
    real = alerts_mod.raise_attention
    fired = []

    def flaky(c, kind, record_id, **kw):
        if record_id == victim and not fired:
            fired.append(1)
            return {"event_id": None, "status": "error", "created": False, "error": "OperationalError"}   # what raise_attention returns on a DB failure
        return real(c, kind, record_id, **kw)
    monkeypatch.setattr(alerts_mod, "raise_attention", flaky)
    when, out = _lease(db, a, g)
    assert fired and out["rerank"]["status"] == "partial" and out["rerank"]["pending_group_ids"] == [victim]
    assert out["rerank"]["failed"][0]["category"] == "AlertOutboxUnavailable"
    assert row(db.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (victim, a["id"])))   # the rewrite rolled back with the alert
    assert sorted(r["record_id"] for r in _outbox(db)) == others[1:]
    assert [p["group_id"] for p in matching.pending_lease_reranks(db)] == [victim]
    r1 = matching.retry_pending_lease_reranks(db, "tester")
    assert [x["group_id"] for x in r1["affected"]] == [victim] and not r1["failed"]
    assert sorted(r["record_id"] for r in _outbox(db)) == others and matching.pending_lease_reranks(db) == []


def test_lease_with_no_affected_group_raises_nothing(db):
    a1 = _apt(db)
    spare = _apt(db, address="Unit 9 / 9 Neutral Street", type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1900.0, rent6=2000.0)
    _pool(db)
    matching.run_match(db, a1["id"], "tester", dataset="test")
    assert not row(db.execute("SELECT 1 FROM group_apartments WHERE apartment_id=?", (spare["id"],)))
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    when, out = _lease(db, spare, g)
    assert out["affected"] == [] and out["rerank"]["status"] == "complete" and _outbox(db) == []


def test_partial_rerank_recovery_raises_the_pending_groups_alert_exactly_once(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    others = sorted(r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE id<>? AND status='Proposed'", (g["id"],))))
    victim = others[0]
    real = dbmod.retention_guard
    fired = []

    class FailOnce(real):
        def __init__(self, c, label=""):
            if label == "lease rerank" and not fired and row(c.execute("SELECT 1 FROM group_apartments WHERE group_id=? AND apartment_id=?", (victim, a["id"]))):
                fired.append(1)
                raise dbmod.RetentionLockTimeout("injected: another writer held the step")
            super().__init__(c, label=label)
    monkeypatch.setattr(dbmod, "retention_guard", FailOnce)
    when, out = _lease(db, a, g)
    assert out["rerank"]["pending_group_ids"] == [victim]
    assert sorted(r["record_id"] for r in _outbox(db)) == others[1:]   # the failed step queued nothing (its transaction did not land)
    monkeypatch.setattr(dbmod, "retention_guard", real)
    r1 = matching.retry_pending_lease_reranks(db, "tester")
    assert [x["group_id"] for x in r1["affected"]] == [victim]
    assert sorted(r["record_id"] for r in _outbox(db)) == others
    matching.retry_pending_lease_reranks(db, "tester")
    assert sorted(r["record_id"] for r in _outbox(db)) == others


def test_real_data_alert_is_not_flagged_test_and_stays_held_while_alerts_are_off(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a = _apt(db)
    ids = _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    db.execute("UPDATE enquiries SET is_test=0 WHERE id IN (%s)" % ",".join("?" * len(ids)), ids); db.commit()   # fabricated: members flagged as real
    when, out = _lease(db, a, g)
    ob = _outbox(db)
    assert ob and all(r["is_test"] == 0 and r["status"] == "held" and "disabled" in (r["last_error"] or "") for r in ob)
    assert "(TEST data)" not in alerts.render(ob[0])["text"]


def test_actual_lease_route_queues_the_alert_once_and_the_repeated_post_adds_nothing(client):
    """root's actual-API control shape: POST /api/apartments/{aid}/lease; the leasing group gets no alert, every other live group
    that listed the apartment gets exactly one, and the idempotent repeated POST (follow-up re-run) adds no row"""
    from tests.test_t6_staff_api import _db, _list_apartment, _plant_group, _second_apartment, _submit_public
    e1 = _submit_public(client, first_name="Lease", last_name="One", email="lease1@example.com")
    e2 = _submit_public(client, first_name="Lease", last_name="Two", email="lease2@example.com")
    c = _db(client)
    g1 = _plant_group(c, e1["id"])
    g2 = _plant_group(c, e2["id"])
    first1 = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (g1,)))["apartment_id"]
    other = _second_apartment(c)
    _list_apartment(c, g1, first1, 1, e1["id"])
    _list_apartment(c, g1, other, 2, e1["id"])
    first2 = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (g2,)))["apartment_id"]
    _list_apartment(c, g2, first2, 1, e2["id"])
    _list_apartment(c, g2, other, 2, e2["id"])
    c.commit()
    assert _outbox(c) == []
    r = client.post(f"/api/apartments/{other}/lease", json={"group_id": g1})
    assert r.status_code == 200, r.text
    ob = _outbox(c)
    assert [(x["kind"], x["record_type"], x["record_id"]) for x in ob] == [(KIND, "group", g2)]
    gen = row(c.execute("SELECT lease_generation FROM apartments WHERE id=?", (other,)))["lease_generation"]
    assert gen == 1 and ob[0]["event_id"] == alerts.event_id_for(KIND, "group", g2, _rev(c, other, 1))
    r2 = client.post(f"/api/apartments/{other}/lease", json={"group_id": g1})
    assert r2.status_code == 200 and r2.json()["repeated"] is True
    assert len(_outbox(c)) == 1
