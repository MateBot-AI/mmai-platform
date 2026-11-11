"""T14 provenance and fail-closed TEST deletion for both bots' authority rows."""
import hashlib

import pytest

from server import db as dbm, exit_exceptions as xx, homebot, seed
from server.db import retention_guard, row, rows
from tests.test_astra_exit_authority import _world, _propose
from tests.test_gsl2_test_data_clear import world, _snapshot, _real_digests, TABLES  # noqa: F401
from tests.test_homebot import client  # noqa: F401


def _records(w):
    c, aid = w["db"], w["a"]["id"]
    result = {}
    with retention_guard(c):
        for key, bot, owner in (
            ("mt", "matebot", w["tg"][0]), ("mr", "matebot", w["rg"][0]),
            ("ht", "homebot", w["hb_test"]), ("hr", "homebot", w["hb_real"]),
        ):
            result[key] = xx.propose(c, bot, owner, aid, "2027-02-01", "2027-08-15", "5000", "tester")
    return result


def _real_authorities(c):
    records = (
        rows(c.execute("SELECT * FROM exit_exceptions WHERE is_test=0 ORDER BY id")),
        rows(c.execute("""SELECT m.* FROM exit_exception_members m JOIN exit_exceptions x ON x.id=m.exception_id
                          WHERE x.is_test=0 ORDER BY m.exception_id,m.enquiry_id""")),
    )
    return hashlib.sha256(repr(records).encode()).hexdigest(), records


def _all(c):
    return _snapshot(c, TABLES + ("exit_exceptions", "exit_exception_members"))


def test_test_clear_cascades_both_bots_and_keeps_every_real_authority_byte_identical(world):
    c = world["db"]
    recs = _records(world)
    assert {key: r["is_test"] for key, r in recs.items()} == {"mt": 1, "mr": 0, "ht": 1, "hr": 0}
    real = _real_authorities(c)
    digests = _real_digests(c, world)
    n_tg = c.execute("""SELECT COUNT(*) FROM groups g
                       WHERE EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=1)
                         AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test<>1)""").fetchone()[0]
    assert n_tg >= len(world["tg"]) >= 1
    assert seed.delete_test_data(c, "tester") == len(world["test"])
    assert _real_authorities(c) == real and _real_digests(c, world) == digests
    assert rows(c.execute("SELECT id FROM exit_exceptions ORDER BY id")) == [{"id": r["id"]} for r in sorted((recs["mr"], recs["hr"]), key=lambda r: r["id"])]
    assert rows(c.execute("""SELECT m.* FROM exit_exception_members m
                            LEFT JOIN exit_exceptions x ON x.id=m.exception_id WHERE x.id IS NULL""")) == []
    audit = row(c.execute("SELECT detail FROM audit_log WHERE action='delete_test_data' ORDER BY id DESC LIMIT 1"))
    assert audit["detail"] == (
        f"2 MateBot and 1 HomeBot test enquiries, {n_tg} groups cleared; "
        "2 exit exceptions cleared; 4 unrelated real orphan rows left unchanged")
    assert seed.delete_test_data(c, "tester") == 0
    assert _real_authorities(c) == real and _real_digests(c, world) == digests


@pytest.mark.parametrize("key,bad_flag", [("mt", 0), ("mr", 1), ("ht", 0), ("hr", 1), ("mt", None)])
def test_provenance_mismatch_or_unknown_refuses_before_any_write(world, key, bad_flag):
    c = world["db"]
    recs = _records(world)
    c.execute("UPDATE exit_exceptions SET is_test=? WHERE id=?", (bad_flag, recs[key]["id"]))
    c.commit()
    before = _all(c)
    with pytest.raises(seed.TestDataClearRefused) as exc:
        seed.delete_test_data(c, "tester")
    assert str(exc.value) == (
        "test data not cleared: 1 contradiction(s) between test flags and ownership "
        "(groups 0, links 0, requests 0, submission keys 0, alerts 0, exceptions 1, receipt owners 0); resolve them first")
    assert not c.in_transaction and _all(c) == before


def test_original_member_link_mismatch_is_detected_even_when_current_owner_matches(world):
    c = world["db"]
    recs = _records(world)
    c.execute("INSERT INTO exit_exception_members(exception_id,enquiry_id) VALUES (?,?)",
              (recs["mr"]["id"], world["test"][0]))
    c.commit()
    before = _all(c)
    with pytest.raises(seed.TestDataClearRefused, match="exceptions 1"):
        seed.delete_test_data(c, "tester")
    assert _all(c) == before


def test_cross_bot_member_link_is_not_treated_as_homebot_provenance(world):
    c = world["db"]
    recs = _records(world)
    c.execute("INSERT INTO exit_exception_members(exception_id,enquiry_id) VALUES (?,?)",
              (recs["hr"]["id"], world["real"][0]))
    c.commit()
    before = _all(c)
    with pytest.raises(seed.TestDataClearRefused, match="exceptions 1"):
        seed.delete_test_data(c, "tester")
    assert _all(c) == before


def test_mixed_dataset_proposal_refuses_without_creating_authority(client, monkeypatch):
    c = client.db
    aid, ids, gid = _world(c, monkeypatch)
    c.execute("UPDATE enquiries SET is_test=0 WHERE id=?", (ids[0],))
    c.commit()
    before = rows(c.execute("SELECT * FROM audit_log ORDER BY id"))
    with pytest.raises(xx.ExitExceptionError, match="real/test ownership"):
        _propose(c, gid, aid)
    assert rows(c.execute("SELECT * FROM exit_exceptions")) == []
    assert rows(c.execute("SELECT * FROM audit_log ORDER BY id")) == before


def test_additive_provenance_migration_is_idempotent_and_does_not_guess_legacy_flags(client, monkeypatch):
    c = client.db
    aid, ids, gid = _world(c, monkeypatch)
    rec = _propose(c, gid, aid)
    assert rec["is_test"] == 1
    c.execute("ALTER TABLE exit_exceptions DROP COLUMN is_test")
    c.commit()
    dbm.migrate(c)
    legacy = row(c.execute("SELECT * FROM exit_exceptions WHERE id=?", (rec["id"],)))
    assert legacy["is_test"] is None
    dbm.migrate(c)
    assert row(c.execute("SELECT * FROM exit_exceptions WHERE id=?", (rec["id"],))) == legacy
    before = _all(c)
    with pytest.raises(seed.TestDataClearRefused, match="exceptions 1"):
        seed.delete_test_data(c, "tester")
    assert _all(c) == before


@pytest.mark.parametrize("kind", ["real_authority", "member_link"])
def test_sqlite_legacy_orphan_is_counted_without_silently_deleting_real_data(world, kind):
    c = world["db"]
    if getattr(c, "backend", "sqlite") != "sqlite":
        pytest.skip("SQLite FK-off legacy corruption fixture; normal cascades run on both backends")
    recs = _records(world)
    # Labelled legacy corruption only: production deletion still runs with FKs ON.
    c.execute("PRAGMA foreign_keys=OFF")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    try:
        if kind == "real_authority":
            c.execute("UPDATE exit_exceptions SET group_id=999999 WHERE id=?", (recs["mr"]["id"],))
        else:
            c.execute("INSERT INTO exit_exception_members(exception_id,enquiry_id) VALUES (?,?)",
                      (999999, world["real"][0]))
        c.commit()
    finally:
        c.execute("PRAGMA foreign_keys=ON")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    if kind == "member_link":
        before = _all(c)
        with pytest.raises(seed.TestDataClearRefused, match="exceptions 1"):
            seed.delete_test_data(c, "tester")
        assert _all(c) == before
    else:
        real = _real_authorities(c)
        assert seed.delete_test_data(c, "tester") == len(world["test"])
        assert _real_authorities(c) == real
        audit = row(c.execute("SELECT detail FROM audit_log WHERE action='delete_test_data' ORDER BY id DESC LIMIT 1"))
        assert audit["detail"].endswith("5 unrelated real orphan rows left unchanged")


def test_fault_after_both_cascades_rolls_back_all_authorities_and_original_links(world, monkeypatch):
    c = world["db"]
    _records(world)
    before = _all(c)
    original = homebot.delete_test_data

    def fail_after_deletion(conn):
        original(conn)
        raise RuntimeError("synthetic failure after exception cascades")

    monkeypatch.setattr(homebot, "delete_test_data", fail_after_deletion)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        seed.delete_test_data(c, "tester")
    assert _all(c) == before


def test_sqlite_disabled_cascade_is_detected_and_the_entire_clear_rolls_back(world):
    c = world["db"]
    if getattr(c, "backend", "sqlite") != "sqlite":
        pytest.skip("SQLite FK-disabled caller fixture; normal cascades run on both backends")
    _records(world)
    before = _all(c)
    c.execute("PRAGMA foreign_keys=OFF")
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    try:
        with pytest.raises(seed.TestDataClearRefused, match="cascade was incomplete"):
            seed.delete_test_data(c, "tester")
        assert _all(c) == before
    finally:
        c.execute("PRAGMA foreign_keys=ON")
