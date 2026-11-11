"""Outcome ledger (server/outcome_ledger.py), 10 November 2026: focused tests.

Fabricated people only, one disposable database per test (conftest `db`), the AI review replaced by the suite's `_ask`
double, the business date pinned by conftest, and every Python socket connection refused. No provider and no network; the
only link out of the process is the PostgreSQL client library's own connection to the suite's database, when it has one.

The suite's database is SQLite by default and PostgreSQL when the suite is pointed at one. The "ledger must not write" check
in `_ledger` is made on whichever it is: by the change counter on SQLite, by a read-only session on PostgreSQL. The ledger
cannot confirm one-moment consistency on PostgreSQL and must say so (NOT VERIFIED); that is asserted, not worked around.
The two tests about SQLite's own behaviour (the change marker, a frozen file) always run, on an isolated SQLite file.
"""
import json
import re
import socket
import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest

from server import ai, ai_queue, lease_dates, matching, rules
from server import db as dbmod
from server import outcome_ledger as ol
from server.db import row, rows, table_names
from tests.conftest import TEST_TODAY
from tests.test_launch_neutral import SWE, _apt, _enq, _pool
from tests.test_t6_fail_closed import _good, _reply_builder

CODE = re.compile(r"\b(?:GP|[ABCGHIMPT])\d{1,2}(?:\([a-z]\))?\b")   # Register codes must never reach staff text


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network use in an outcome-ledger test")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)


def _pg(db):
    return getattr(db, "backend", None) == "postgres"


def _snapshot(db):
    """Every row of every table. On PostgreSQL each row also carries its physical version (ctid, xmin), so a rewrite that
    changes no value still shows, as SQLite's change counter shows it. On PostgreSQL this is supplementary to the read-only
    guard: a write the server refused changes no row, so the snapshot does not show that one was attempted."""
    if _pg(db):
        return {t: rows(db.execute(f"SELECT ctid::text AS _row_place, xmin::text AS _row_version, * FROM {t} ORDER BY ctid")) for t in table_names(db)}
    return {t: rows(db.execute(f"SELECT * FROM {t}")) for t in table_names(db)}


def _pg_asks_read_only(db):
    """The helper's own question to the server: is a transaction begun on this session read-only? Asked on the raw session,
    outside the adapter, only when no transaction is open, and closed again by the helper itself - so it can neither leave
    open, nor end, a transaction that belongs to the code under test."""
    assert not db.in_transaction
    raw = db._pg
    try:
        return raw.execute("SHOW transaction_read_only").fetchone()[0]
    finally:
        raw.rollback()


def _pg_read_only_call(db, call):
    """PostgreSQL: run `call` with the server itself refusing every write. The session is switched to read-only for the call,
    so an INSERT, UPDATE, DELETE, schema change, sequence step or row lock fails in the database, whatever sent it. Afterwards
    the session must be the same one, with nothing left open: the adapter keeps a transaction open after any statement it does
    not take for a plain read, including one the server refused. What the code under test left is read BEFORE the helper sends
    anything of its own and is never tidied away unreported; the helper then restores the session as it found it."""
    clean = {"same_session": True, "transaction_left_open": False, "write_statement_seen": False}
    raw, prior = db._pg, db._pg.read_only
    assert not db.in_transaction and not db._dirty, "a change was left uncommitted before the ledger was read"
    raw.read_only = True
    left = None
    try:
        assert _pg_asks_read_only(db) == "on", "the read-only guard was not in force before the ledger was read"
        out = call()
        left = {"same_session": db._pg is raw, "transaction_left_open": bool(db.in_transaction), "write_statement_seen": bool(db._dirty)}
        if left == clean:
            assert _pg_asks_read_only(db) == "on", "the read-only guard did not hold to the end of the read"
    finally:
        if db._pg is raw and not raw.closed:
            if db.in_transaction or db._dirty:
                db.rollback()
            raw.read_only = prior
    assert left == clean, f"the ledger must not write: {left}"
    return out


def _ledger(db, **kw):
    """get_ledger, asserting on every call that it wrote nothing and that the states add up."""
    if _pg(db):
        before = _snapshot(db)
        out = _pg_read_only_call(db, lambda: ol.get_ledger(db, "test", **kw))
        assert _snapshot(db) == before, "the ledger must not write"
    else:
        before, changes = _snapshot(db), db.total_changes
        out = ol.get_ledger(db, "test", **kw)
        assert db.total_changes == changes and _snapshot(db) == before, "the ledger must not write"
    assert sum(s["forms"] for s in out["states"].values()) == out["totals"]["enquiry_forms"]
    assert sum(s["residents"] for s in out["states"].values()) == out["totals"]["residents"]
    json.dumps(out)   # the endpoint returns it as JSON
    con = out["consistency"]
    if _pg(db):
        # PostgreSQL has no change marker, so the ledger cannot confirm it read one moment. It must say exactly that, at the
        # first read, and list it as a limitation - never report one moment
        assert con["single_instant"] == ol.NOT_VERIFIED and con["checked"] is False and con["attempts"] == 1
        assert "not a picture of one instant" in con["text"] and con["text"] in out["limitations"]
    else:
        # nothing else is writing in these tests, so the ledger must have checked, and found, that it read one moment at the first try
        assert con["single_instant"] is True and con["checked"] is True and con["attempts"] == 1
    _no_placement_words(out)
    assert not any("compared completely" in t.lower() or "complete search" in t.lower() for t in _strings(out))   # v3: never asserted
    return out


def _strings(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(x, (list, tuple)):
        for v in x:
            yield from _strings(v)
    elif isinstance(x, str):
        yield x


def _no_placement_words(out):
    """A proposal is never called a placement, and a listed-apartment reference is never called apartment support. The only
    sentences allowed to use the word are the ones saying a ready proposal is NOT a placement."""
    for t in _strings(out):
        low = t.lower()
        assert "apartment_supported" not in low and "apartment-supported" not in low and "apartment-backed" not in low and "own_apartment" not in low, t
        if re.search(r"\bplaced\b|\bplacements?\b", low):
            assert "not a placement" in low or low == "not_a_placement", t


def _ready_pairs(db, monkeypatch):
    """One 2B2B, four compatible solo enquirers, a full run and an approving review: (1,2) and (3,4) are ready proposals, the
    other four pairs are alternatives."""
    a = _apt(db)
    ids = _pool(db)
    matching.full_run(db, "tester", dataset="test")
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    return a, ids


def _sqlite_shape(c):
    return {"class": type(c).__name__, "backend": c.backend, "rows_by_name": c.row_factory is sqlite3.Row,
            "journal_mode": c.execute("PRAGMA journal_mode").fetchone()[0], "foreign_keys": c.execute("PRAGMA foreign_keys").fetchone()[0],
            "schema": [tuple(r) for r in c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()]}


@pytest.fixture
def sqlite_db(tmp_path):
    """An isolated SQLite database for the tests about SQLite's own behaviour (the change marker, a frozen file), whatever
    database the rest of the suite is using. It is opened the way server.db.connect() opens its SQLite branch; it does not read
    or change DATABASE_URL and is never the app's shared connection. When the suite itself is on SQLite, the database is
    compared with one opened by connect(), so this copy of the opening steps cannot drift from the product unnoticed."""
    home = tmp_path / "isolated-sqlite"
    home.mkdir()
    c = sqlite3.connect(str(home / "test.db"), check_same_thread=False, timeout=15, factory=dbmod.Connection)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        dbmod.init_schema(c)
        assert c.backend == "sqlite" and not c.in_transaction
        if not dbmod.is_postgres():
            ref = dbmod.connect(str(home / "as-connect-opens-it.db"))
            try:
                assert _sqlite_shape(c) == _sqlite_shape(ref) and len(_sqlite_shape(c)["schema"]) > 10
            finally:
                ref.close()
        yield c
    finally:
        c.close()


def _people(out):
    return {p["resident_id"]: p for p in out["people"]}


def _form(out, eid):
    return next(f for f in out["forms"] if f["form_id"] == eid)


# ------------------------------------------------------------------------------------------------ date check = rule engine
def _unit(i, move_in, flex_in, move_out, flex_out, declared="same", extend=False):
    p = rules.Person(id=str(i), unit_id=str(i), name=f"P{i}", birth_country=None, budget_max=400.0, arrangements={"Own room"}, bunk_ok=False, smokes=False, smoke_ok=True)
    return rules.Unit(id=str(i), kind="single", people=[p], city="Melbourne", apt_sizes={"2 bedrooms"}, mate_max=3, move_in=move_in, move_in_flex=flex_in,
                      move_out=move_out, move_out_flex=flex_out, open_to_extending=extend, adults_confirmed_move_in=move_in if declared == "same" else declared)


def _engine_dates_ok(apt, units, today):
    cands, err = rules.lease_offer_candidates(apt, units, today, [p for u in units for p in u.people])
    return not err and any(not c["date_reasons"] for c in cands)


def test_date_check_agrees_with_the_rule_engine_on_a_grid():
    """The ledger's one-pair date check must say 'dates work' exactly when the engine has an offer with no date reason - with
    the declaration, and without it. Budget reasons are ignored on both sides (the group total is the only budget test)."""
    today = TEST_TODAY
    ins = [date(2026, 12, 20), date(2027, 1, 10), date(2027, 2, 1), date(2027, 3, 5), date(2027, 6, 25)]
    outs = [date(2027, 2, 10), date(2027, 3, 15), date(2027, 7, 10), date(2027, 8, 20), date(2027, 11, 30), date(2026, 12, 1)]
    units = [_unit(1, mi, fi, mo, fo, declared=dec, extend=ext)
             for mi in ins for fi in (0, 14) for mo in outs for fo, ext in ((0, False), (30, False), (0, True))
             for dec in ("same", None, date(2027, 2, 10))]
    apts = [rules.Apartment(id=str(k), address="x", type_code="2B2B", rent_12m=1150.0, rent_6m=1225.0, available_from=av, bedrooms=[], min_lease_months=ml)
            for k, (av, ml) in enumerate([(date(2026, 10, 1), 2), (date(2027, 1, 27), 2), (date(2027, 2, 20), 6), (date(2027, 7, 1), 2), (date(2027, 1, 27), 2.5), (date(2027, 1, 5), 12)])]
    partner = _unit(2, date(2027, 2, 3), 7, date(2027, 7, 12), 14)
    checked = agree_ok = 0
    memo = {}
    for u in units:
        for apt in apts:
            for group in ([u], [u, partner]):
                want = _engine_dates_ok(apt, group, today)
                got = ol.date_outcome(apt.available_from, apt.min_lease_months, group, today, with_declaration=True, memo=memo)
                assert (got == ol.DATE_OK) == want, (u.move_in, u.move_in_flex, u.move_out, u.move_out_flex, u.adults_confirmed_move_in, apt.available_from, apt.min_lease_months, got)
                bare = [replace(x, adults_confirmed_move_in=None) for x in group]
                got_bare = ol.date_outcome(apt.available_from, apt.min_lease_months, group, today, with_declaration=False, memo=memo)
                assert (got_bare == ol.DATE_OK) == _engine_dates_ok(apt, bare, today)
                assert got == ol.DATE_OK or got in ol.DATE_FAILURE_TEXT
                checked += 1
                agree_ok += want
    assert checked > 3000 and 0 < agree_ok < checked, "the grid must exercise both feasible and infeasible cases"


def test_availability_past_the_move_out_window_is_stock_timing_not_bad_dates():
    u = _unit(1, date(2027, 2, 1), 30, date(2027, 2, 15), 0)
    assert ol.date_outcome(date(2027, 2, 20), 2, [u], TEST_TODAY, with_declaration=True) == "availability_past_move_out"
    assert "not an error in the enquiry's dates" in ol.DATE_FAILURE_TEXT["availability_past_move_out"]
    assert ol.date_outcome(date(2027, 3, 20), 2, [u], TEST_TODAY, with_declaration=True) == "available_too_late"
    # the declaration is told apart from the dates: feasible without it, not with a declaration made for a later move-in
    v = _unit(2, date(2027, 2, 1), 7, date(2027, 7, 10), 0, declared=date(2027, 3, 1))
    assert ol.date_outcome(date(2027, 1, 27), 2, [v], TEST_TODAY, with_declaration=False) == ol.DATE_OK
    assert ol.date_outcome(date(2027, 1, 27), 2, [v], TEST_TODAY, with_declaration=True) == "declaration_date"


# ------------------------------------------------------------------------------------------------ ready / primary / alternative
def test_ready_primaries_are_the_only_coverage_and_alternatives_are_a_separate_union(db, monkeypatch):
    a, ids = _ready_pairs(db, monkeypatch)
    out = _ledger(db)
    assert out["totals"] == {"enquiry_forms": 4, "residents": 4, "linked_parties": 0, "forms_in_linked_parties": 0}
    assert out["states"]["ready_primary"]["residents"] == 4 and out["coverage"]["ready_proposal_residents"] == 4
    assert all(out["states"][s]["residents"] == 0 for s in ol.ALL_STATES if s != "ready_primary")
    # exactly the engine's own predicate, group by group
    for g in out["groups"]:
        chk = ai_queue.can_circulate(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["group_id"],))))
        assert g["can_introduce"] == chk["ok"] and g["ready"] == chk["ready_check"]["ready"]
    prim = [g for g in out["groups"] if g["is_primary"]]
    alts = [g for g in out["groups"] if not g["is_primary"]]
    assert len(prim) == 2 and all(g["kind"] == "reviewed ready proposal" for g in prim) and len(alts) == 4
    assert all(not g["can_introduce"] for g in alts), "an alternative can never be introduced as it is"
    cov = out["coverage"]
    assert cov["residents_in_any_primary_proposal"] == 4 and cov["residents_in_any_alternative"] == 4 and cov["residents_in_primary_or_alternative"] == 4
    assert cov["planning_only_residents"] == 0 and out["states"]["planning_only"] == {"forms": 0, "residents": 0, "label": ol.CATEGORY_LABEL["planning_only"]}
    assert all(p["counts_as_ready_proposal"] and p["state"] == "ready_primary" for p in out["people"])
    assert "not a placement" in cov["not_a_placement"] and "placed_residents" not in cov and "counts_as_placed" not in out["people"][0]
    assert "not a placement" in out["people"][0]["outcome"]
    # stock: four people's groups point at a still-listed apartment record. That is a reference, not a re-checked offer and not
    # an apartment for anyone; both groups are offered the same single apartment and nothing is reserved
    st = out["stock"]
    ref = st["listed_apartment_references"]
    assert ref["unique_residents"] == {"ready_primary": 4, "any_primary": 4, "primary_or_alternative": 4}
    assert ref["offer_validity"] == ol.NOT_VERIFIED and ref["nobody_has_an_apartment_from_this"] is True and "NOT VERIFIED" in ref["note"]
    assert all(g["stored_offer_validity"] == ol.NOT_VERIFIED and g["current_apartment_listed"] is True for g in out["groups"])
    d = st["hypothetical_listed_offer_assignment"]
    assert d["nothing_reserved"] is True and "No apartment is reserved" in d["note"] and d["offer_validity"] == ol.NOT_VERIFIED
    assert d["ready_primary_groups"] == {"groups": 2, "groups_assigned_a_distinct_listed_offer": 1, "residents": 4, "residents_in_those_groups": 2}
    assert row(db.execute("SELECT leased_at FROM apartments WHERE id=?", (a["id"],)))["leased_at"] is None
    # the search is declared bounded, so the ledger never calls it complete
    assert out["search"]["uncertain"] is True and out["search"]["flags"]["search_larger_groups_exhaustive"] is False
    assert out["search"]["exhaustive"] is False and "not an exhaustive search" in out["search"]["text"]
    assert out["search"]["flags"]["coverage_selection_truncated"] in (ol.NOT_VERIFIED, 0, 1)   # a key this build may not write


def test_pending_failed_revision_and_hold_are_never_ready_and_keep_distinct_reasons(db, monkeypatch):
    a = _apt(db)
    _pool(db)
    matching.full_run(db, "tester", dataset="test")
    out = _ledger(db)   # before any review
    assert out["states"]["primary_failed_or_pending"]["residents"] == 4 and out["coverage"]["ready_proposal_residents"] == 0
    assert {p["reason_code"] for p in out["people"]} == {"ai_pending"}
    assert all(_form(out, p["form_id"])["ai_processing_incomplete"] for p in out["people"])

    def down(prompt, model=None, max_tokens=1500, system=""):
        raise ai.AIUnavailable("simulated outage")
    monkeypatch.setattr(ai, "_ask", down)
    ai_queue.process_pending(db)
    out = _ledger(db)   # the review failed: rules alone never make a proposal ready
    assert out["coverage"]["ready_proposal_residents"] == 0 and out["states"]["primary_failed_or_pending"]["residents"] == 4
    assert {p["reason_code"] for p in out["people"]} == {"ai_failed"} and "rules alone never make a group ready" in out["people"][0]["outcome"]

    prim = rows(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id"))
    db.execute("UPDATE groups SET ai_status='revise', held_reason='AI review: revise U2. routine clash' WHERE id=?", (prim[0]["id"],))
    db.execute("UPDATE groups SET ai_status='held', ranking_basis='Held for staff (practical need to confirm)', "
               "held_reason='Step-free access needs confirmation from the team before introduction' WHERE id=?", (prim[1]["id"],))
    db.commit()
    out = _ledger(db)
    by_group = {}
    for p in out["people"]:
        by_group.setdefault(p["group_id"], set()).add((p["state"], p["reason_code"]))
    assert by_group[prim[0]["id"]] == {("primary_failed_or_pending", "revision_requested")}
    assert by_group[prim[1]["id"]] == {("primary_held", "essential_fact_unverified")}
    assert out["coverage"]["ready_proposal_residents"] == 0
    held = next(f for f in out["forms"] if f["group_id"] == prim[1]["id"])
    assert "has not passed" in held["outcome"] and held["hold_detail"] == "Step-free access needs confirmation from the team before introduction"
    assert "step_free_access" in held   # shown beside a practical-need hold; its values are tested on their own below
    # an approval written straight onto the row is still not ready: readiness is the engine's full check, not a status column
    db.execute("UPDATE groups SET ai_status='ok', review_verdict='approve', brief_status='ok', held_reason=NULL WHERE id=?", (prim[0]["id"],))
    db.commit()
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (prim[0]["id"],)))
    assert ai_queue.can_circulate(db, g)["ok"] is False
    assert all(p["state"] != "ready_primary" for p in _ledger(db)["people"])


def test_access_fact_is_only_what_the_build_reports_as_current_and_otherwise_not_verified(db, monkeypatch):
    """The ledger asks the build's own access-facts module for the apartment's current record and repeats it. It never reads a
    column, a fact table or a note for this itself; no module, an error or an unknown answer is NOT VERIFIED, never a pass."""
    a, _ = _ready_pairs(db, monkeypatch)
    gid = row(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id LIMIT 1"))["id"]

    def shown():
        out = _ledger(db)
        return next(g for g in out["groups"] if g["group_id"] == gid)["step_free_access"], out

    monkeypatch.setattr(ol, "_access_module", lambda: None)   # a build with no access-facts module (the frozen base)
    label, out = shown()
    assert label == ol.NOT_VERIFIED and any("NOT VERIFIED" in x and "step-free" in x for x in out["limitations"])
    try:   # a raw column with a "yes" in it is not the build's answer: still NOT VERIFIED
        db.execute("ALTER TABLE apartments ADD COLUMN step_free_access INTEGER")
    except Exception:
        pass   # the build already has the column
    db.execute("UPDATE apartments SET step_free_access=1 WHERE id=?", (a["id"],))
    db.commit()
    assert shown()[0] == ol.NOT_VERIFIED

    asked = []

    class Facts:
        answer = {"value": None}

        @classmethod
        def current(cls, c, apartment_id):
            asked.append(apartment_id)
            if isinstance(cls.answer, Exception):
                raise cls.answer
            return cls.answer
    monkeypatch.setattr(ol, "_access_module", lambda: Facts)
    for answer, want in (({"value": 1}, "recorded: step-free"), ({"value": 0}, "recorded: not step-free"), ({"value": None}, "not recorded"),
                         ({"value": True}, ol.NOT_VERIFIED), ({"value": "yes"}, ol.NOT_VERIFIED), ({"value": 2}, ol.NOT_VERIFIED),
                         ({"value": None, "bound": False}, "not recorded (an earlier record was for a different address and no longer applies)"),
                         ({"value": 1, "bound": False}, ol.NOT_VERIFIED), ({"value": 1, "bound": True}, "recorded: step-free"),
                         ({}, ol.NOT_VERIFIED), (None, ol.NOT_VERIFIED), ({"label": "Completely step-free route confirmed"}, ol.NOT_VERIFIED),
                         (RuntimeError("no such table"), ol.NOT_VERIFIED)):
        Facts.answer = answer
        label, out = shown()
        assert label == want, (answer, label)
        assert not any("no record of apartment access facts" in x for x in out["limitations"])
    assert set(asked) == {a["id"]}
    # a recorded step-free fact is reported, and changes nobody's state: readiness stays the engine's own check
    Facts.answer = {"value": 1}
    with_fact = _ledger(db)
    monkeypatch.setattr(ol, "_access_module", lambda: None)
    assert with_fact["states"] == _ledger(db)["states"]


def test_a_stale_listed_offer_is_never_ready_and_a_listed_reference_claims_nothing_about_it(db, monkeypatch):
    """Control: the apartment record stays listed while its rent, date or bedding changes under a stored offer. The proposal
    must stop being ready (the engine's own check), whatever the listed-apartment reference says - that figure does not move,
    which is exactly why it is labelled NOT VERIFIED and never called apartment support."""
    a, ids = _ready_pairs(db, monkeypatch)
    assert _ledger(db)["coverage"]["ready_proposal_residents"] == 4
    prim = [g["id"] for g in rows(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id"))]
    stamped = {g: row(db.execute("SELECT evidence_hash FROM groups WHERE id=?", (g,)))["evidence_hash"] for g in prim}
    bands = {r["rate_kind"] for r in rows(db.execute("SELECT rate_kind FROM group_apartments WHERE group_id IN (?, ?)", prim))}
    assert len(prim) == 2 and len(bands) == 1
    unused_rent = "rent_12m" if bands == {"6m"} else "rent_6m"   # the rate band this stay does not use is not part of its stored offer
    for column, stale in (("rent_12m", a["rent_12m"] + 400.0), ("rent_6m", a["rent_6m"] + 400.0), ("available_from", "2027-02-20"),
                          ("bedroom2", "1 single"), ("min_lease_months", 12)):
        db.execute(f"UPDATE apartments SET {column}=? WHERE id=?", (stale, a["id"]))
        db.commit()
        out = _ledger(db)
        assert out["coverage"]["ready_proposal_residents"] == 0 and out["states"]["primary_failed_or_pending"]["residents"] == 4, column
        assert {p["reason_code"] for p in out["people"]} == {"facts_changed"}, column
        assert not any(p["counts_as_ready_proposal"] for p in out["people"])
        ref = out["stock"]["listed_apartment_references"]   # still listed: the reference count does not notice a stale offer
        assert ref["unique_residents"] == {"ready_primary": 0, "any_primary": 4, "primary_or_alternative": 4} and ref["offer_validity"] == ol.NOT_VERIFIED
        hyp = out["stock"]["hypothetical_listed_offer_assignment"]
        assert hyp["all_primary_groups"]["groups_assigned_a_distinct_listed_offer"] == 1 and hyp["ready_primary_groups"]["groups"] == 0
        assert hyp["offer_validity"] == ol.NOT_VERIFIED
        # even with the review record re-stamped to today's facts, the stored offer is still the old one: not ready
        for g in prim:
            db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (matching.group_evidence(db, g)[0], g))
        db.commit()
        out = _ledger(db)
        if column == unused_rent:   # the stored offer is still exactly what the rules produce today: the engine's check passes
            assert out["coverage"]["ready_proposal_residents"] == 4, column
        else:
            assert out["coverage"]["ready_proposal_residents"] == 0, column
            assert {p["reason_code"] for p in out["people"]} == {"requirements_no_longer_met"}, column
        for g in prim:
            db.execute("UPDATE groups SET evidence_hash=? WHERE id=?", (stamped[g], g))
        db.execute(f"UPDATE apartments SET {column}=? WHERE id=?", (a[column], a["id"]))
        db.commit()
        assert _ledger(db)["coverage"]["ready_proposal_residents"] == 4, f"{column} restored: ready again"


def test_alternative_only_and_no_current_apartment(db, monkeypatch):
    a, ids = _ready_pairs(db, monkeypatch)
    g34 = row(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 AND member_key=?", (f"{ids[2]},{ids[3]}",)))["id"]
    db.execute("UPDATE groups SET is_primary=0 WHERE id=?", (g34,))
    db.commit()
    out = _ledger(db)
    p = _people(out)
    assert out["states"]["alternative_only"]["residents"] == 2 and out["coverage"]["ready_proposal_residents"] == 2
    for i in ids[2:]:
        assert p[str(i)]["state"] == "alternative_only" and p[str(i)]["counts_as_ready_proposal"] is False
        assert p[str(i)]["reason_code"] == "alternative_approved_not_selected" and "cannot be introduced" in p[str(i)]["outcome"]
    assert out["coverage"]["residents_in_primary_or_alternative"] == 4 and "not a number of people with a ready proposal" in out["coverage"]["union_note"]
    # a primary whose apartment has gone is held: no listed-apartment reference, and not a ready proposal
    g12 = row(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1"))["id"]
    db.execute("UPDATE groups SET apartment_id=NULL WHERE id=?", (g12,))
    db.commit()
    out = _ledger(db)
    assert out["coverage"]["ready_proposal_residents"] == 0 and out["states"]["primary_held"]["residents"] == 2
    assert {x["reason_code"] for x in out["people"] if x["group_id"] == g12 and x["state"] == "primary_held"} == {"no_current_apartment"}
    assert out["stock"]["primary_groups_without_a_listed_apartment"] == 1 and out["stock"]["listed_apartment_references"]["unique_residents"]["any_primary"] == 0


# ------------------------------------------------------------------------------------------------ forms are not people
def test_a_couple_form_is_two_residents_and_linked_forms_stay_one_party(db):
    _apt(db)
    couple = _enq(db, "Cara", "cara@example.com", party="Couple sharing one bed", party_size="2", rooms=["Couple sharing one bed"], room_first="Couple sharing one bed",
                  party_members=[{"name": "Dan Partner", "email": "dan@example.com", "birth_country_raw": "Sweden"}], **SWE)
    solo = _enq(db, "Eve", "eve@example.com", **SWE)
    mate = _enq(db, "Finn", "finn@example.com", **SWE)
    db.execute("UPDATE enquiries SET linked_to_enquiry_id=? WHERE id=?", (solo["id"], mate["id"]))
    db.commit()
    out = _ledger(db)
    assert out["totals"] == {"enquiry_forms": 3, "residents": 4, "linked_parties": 1, "forms_in_linked_parties": 2}
    p = _people(out)
    assert set(p) == {str(couple["id"]), f"{couple['id']}m1", str(solo["id"]), str(mate["id"])}
    assert set(p) == set(matching._people_ids([matching.unit_from_row(e, rows(db.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],))))
                                               for e in rows(db.execute("SELECT * FROM enquiries"))]))
    assert p[f"{couple['id']}m1"]["role"] == "party member" and p[f"{couple['id']}m1"]["name"] == "Dan Partner"
    assert out["states"]["no_proposal"] == {"forms": 3, "residents": 4, "label": ol.CATEGORY_LABEL["no_proposal"]}
    assert _form(out, solo["id"])["linked_forms"] == [mate["id"]] and _form(out, mate["id"])["linked_forms"] == [solo["id"]]
    # the linked pair is one party for the partner check: the only OTHER party in the pool is the couple
    assert _form(out, solo["id"])["checks"]["partners"]["other_parties_in_pool"] == 1


# ------------------------------------------------------------------------------------------------ why no proposal
def test_solo_only_answers_are_not_an_individual_enquiry_or_a_private_bedroom(db):
    _apt(db)
    _apt(db, type_code="Studio", beds=("1 queen", None, None), rent12=500.0, rent6=550.0, address="Unit 2 / 1 Neutral Street")
    solo = _enq(db, "Solo", "solo@example.com", apt_sizes=["Studio"], budget_option="Studio-single", **SWE)   # own room, studio only
    private = _enq(db, "Priv", "priv@example.com", **SWE)                 # an individual wanting a private bedroom in a shared 2-bedroom
    sharer = _enq(db, "Shar", "shar@example.com", apt_sizes=["Studio"], budget_option="Studio-single", rooms=["Share a room with one other person"],
                  room_first="Share a room with one other person", **SWE)   # studio only, but willing to share: not solo-only
    out = _ledger(db)
    f = _form(out, solo["id"])
    assert f["state"] == "no_proposal" and f["reason_code"] == "solo_only_answers" and "apartment to themselves" in f["outcome"]
    for other in (private, sharer):
        assert "solo_only_answers" not in {x["code"] for x in _form(out, other["id"])["proven"]}
        assert _form(out, other["id"])["residents"] == 1
    # accepting no housemates is solo-only too
    db.execute("UPDATE enquiries SET mate_max=0 WHERE id=?", (private["id"],))
    db.commit()
    assert _form(_ledger(db), private["id"])["reason_code"] == "solo_only_answers"


def test_date_reasons_are_proven_from_stored_facts_and_bad_input_is_kept_apart(db):
    _apt(db)                                                              # available 27 Jan 2027
    early = _enq(db, "Early", "early@example.com", move_in="2026-12-01", move_in_flex="Exact date", **SWE)      # must start before the stock is free
    clipped = _enq(db, "Clip", "clip@example.com", move_in="2027-01-20", move_in_flex="± 2 weeks", move_out="2027-01-25", move_out_flex="Exact date", **SWE)
    broken = _enq(db, "Broke", "broke@example.com", **SWE)
    db.execute("UPDATE enquiries SET move_in='' WHERE id=?", (broken["id"],))
    db.commit()
    out = _ledger(db)
    e = _form(out, early["id"])
    assert e["reason_code"] == "no_date_feasible_stock" and "only after the latest move-in date accepted" in e["outcome"]
    assert e["checks"]["apartments_right_city_size_and_room"] == 1 and e["checks"]["apartments_where_their_dates_work"] == 0
    cl = _form(out, clipped["id"])
    assert cl["reason_code"] == "no_date_feasible_stock" and "not an error in the enquiry's dates" in cl["outcome"]
    b = _form(out, broken["id"])
    assert b["reason_code"] == "enquiry_needs_correcting" and "problem with the stored enquiry itself" in b["outcome"]
    assert e["reason_code"] != b["reason_code"]


def test_no_date_compatible_partner_is_proven_only_when_each_has_stock(db):
    _apt(db)                                                                                     # 27 Jan 2027
    _apt(db, avail="2027-03-08", address="Unit 3 / 1 Neutral Street")
    feb = _enq(db, "Feb", "feb@example.com", **SWE)                                              # 1 Feb ± 1 week
    mar = _enq(db, "Mar", "mar@example.com", move_in="2027-03-10", move_in_flex="Exact date", **SWE)
    out = _ledger(db)
    for x in (feb, mar):
        f = _form(out, x["id"])
        assert f["checks"]["apartments_where_their_dates_work"] == 1, f["checks"]
        assert f["reason_code"] == "no_date_compatible_partner"
        assert f["checks"]["partners"] == {"other_parties_in_pool": 1, "pass_basic_conditions": 1, "accepted_dates_overlap": 0, "share_an_apartment_on_dates": 0,
                                           "share_an_apartment_on_dates_under_current_declarations": 0}


def test_budget_is_never_an_individual_reason_and_unfound_groups_are_search_uncertainty(db):
    _apt(db)
    a = _enq(db, "Lo", "lo@example.com", **SWE)
    b = _enq(db, "Low", "low@example.com", **SWE)
    db.execute("UPDATE enquiries SET budget_max=50")   # the pair's COMBINED total fails; neither person is ruled out alone
    db.commit()
    matching.full_run(db, "tester", dataset="test")
    assert rows(db.execute("SELECT id FROM groups")) == []
    out = _ledger(db)
    for x in (a, b):
        f = _form(out, x["id"])
        assert f["state"] == "no_proposal" and f["reason_code"] == "not_found_by_search" and f["proven"] == []
        assert "not proof that none exists" in f["outcome"] and f["proof"] == "none: remaining search uncertainty"
        assert f["checks"]["budget"] == ol.BUDGET_NOTE and f["checks"]["partners"]["share_an_apartment_on_dates_under_current_declarations"] == 1
    assert not any("budget" in r["reason_code"] for r in out["unresolved_reasons"])


def test_adult_declaration_missing_and_renewal_have_their_own_labels(db):
    _apt(db)
    miss = _enq(db, "Miss", "miss@example.com", **SWE)
    renew = _enq(db, "Renew", "renew@example.com", **SWE)
    db.execute("UPDATE enquiries SET adults_confirmed=0 WHERE id=?", (miss["id"],))
    db.execute("UPDATE enquiries SET adults_confirmed_move_in='2027-03-01' WHERE id=?", (renew["id"],))   # declared for a later move-in than 1 Feb
    db.commit()
    out = _ledger(db)
    m, r = _form(out, miss["id"]), _form(out, renew["id"])
    assert m["reason_code"] == "declaration_missing" and m["adult_declaration"] == "missing"
    assert r["reason_code"] == "declaration_renewal" and r["adult_declaration"] == "needs renewing"
    assert "no_date_feasible_stock" not in {x["code"] for x in m["proven"] + r["proven"]}, "a declaration problem is not a date problem"


def test_one_person_checks_never_rule_out_someone_the_engine_did_group(db):
    """Soundness of the necessary-condition checks: every enquiry the rule engine itself put in a group must come out of the
    'why no group' diagnosis with nothing proven against it. A mixed pool, so most of the reasons are exercised on the others."""
    for k, (code, beds, avail) in enumerate([("2B2B", ("1 king or 2 singles", "1 king or 2 singles", None), "2027-01-27"), ("3B", ("1 queen", "1 king or 2 singles", "2 singles"), "2027-01-10"),
                                             ("2B2B", ("1 queen", "2 singles", None), "2027-03-08"), ("1BR", ("2 singles", None, None), "2027-02-01")]):
        _apt(db, type_code=code, beds=beds, avail=avail, address=f"Unit {k} / 7 Sound Street")
    ins = ["2027-01-15", "2027-02-01", "2027-02-01", "2027-02-10", "2027-03-10", "2027-03-12", "2026-12-01", "2027-02-05"]
    flex = ["Exact date", "± 1 week", "± 2 weeks", "± 1 month"]
    sizes = [["2 bedrooms"], ["2 bedrooms", "3 bedrooms"], ["3 bedrooms"], ["1 bedroom"], ["1 bedroom", "2 bedrooms"]]
    for i in range(24):
        _enq(db, f"S{i}", f"s{i}@example.com", move_in=ins[i % 8], move_in_flex=flex[i % 4], move_out=["2027-07-10", "2027-11-20", "2027-07-15"][i % 3],
             move_out_flex=flex[(i + 1) % 4], apt_sizes=sizes[i % 5], smoke="No" if i % 6 else "Yes, outside only (balcony or courtyard)", smoke_ok="Yes" if i % 4 else "No",
             rooms=["Own room"] if i % 3 else ["Own room", "Share a room with one other person"], **SWE)
    matching.full_run(db, "tester", dataset="test")
    grouped = {r["enquiry_id"] for r in rows(db.execute("SELECT DISTINCT enquiry_id FROM group_members"))}
    assert 4 <= len(grouped) < 24, "the pool must leave some people grouped and some not"
    forms, (apartments, unreadable) = ol._load_forms(db, 1), ol._load_apartments(db)
    clusters = {i: [i] for i in forms}
    memo = {}
    wrongly_ruled_out = {i: [p["code"] for p in d["proven"]] for i in grouped
                         for d in [ol._why_no_proposal([i], forms, clusters, apartments, set(), TEST_TODAY, memo)] if d["proven"]}
    assert not wrongly_ruled_out and unreadable == 0
    out = _ledger(db)
    assert out["coverage"]["residents_in_primary_or_alternative"] == len(grouped)
    assert out["states"]["no_proposal"]["forms"] == 24 - len(grouped) and out["coverage"]["ready_proposal_residents"] == 0   # nothing reviewed: no ready proposal
    assert {f["reason_code"] for f in out["forms"] if f["state"] == "no_proposal"} - {"not_found_by_search"}, "some unresolved people carry a proven reason"


# ------------------------------------------------------------------------------------------------ history is separate
def test_history_is_separate_and_nobody_is_counted_twice(db, monkeypatch):
    a, ids = _ready_pairs(db, monkeypatch)
    g12 = row(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 AND member_key=?", (f"{ids[0]},{ids[1]}",)))["id"]
    db.execute("UPDATE groups SET status='Introduced' WHERE id=?", (g12,))
    db.execute("UPDATE enquiries SET status='Introduced' WHERE id IN (?,?)", (ids[0], ids[1]))
    uid = row(db.execute("SELECT person_uid FROM people_identities WHERE enquiry_id=? AND role='lead' AND retired_at IS NULL", (ids[0],)))["person_uid"]   # issued by the engine
    db.execute("UPDATE group_membership_versions SET introduced_at='2026-11-08T10:00:00+11:00' WHERE group_id=? AND superseded_at IS NULL", (g12,))   # the engine's own version row
    vid = row(db.execute("SELECT id FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (g12,)))["id"]
    db.execute("INSERT INTO group_response_links(version_id, group_id, enquiry_id, person_uid, token_hash, is_test, created_at, expires_at) VALUES (?,?,?,?,'t',1,'x','x')", (vid, g12, ids[0], uid))
    lid = row(db.execute("SELECT id FROM group_response_links"))["id"]
    db.execute("INSERT INTO group_responses(link_id, version_id, group_id, enquiry_id, person_uid, response, responded_at) VALUES (?,?,?,?,?,'accept','x')", (lid, vid, g12, ids[0], uid))
    db.commit()
    out = _ledger(db)
    p = _people(out)
    assert out["states"]["introduced_or_later"]["residents"] == 2 and out["states"]["ready_primary"]["residents"] == 2
    assert out["coverage"]["ready_proposal_residents"] == 2, "introduced people are history, not a second count of current coverage"
    assert p[str(ids[0])]["state"] == "introduced_or_later" and p[str(ids[0])]["counts_as_ready_proposal"] is False
    assert p[str(ids[0])]["history"] == {"introduced": True, "responded": True, "accepted": True, "leased": False}
    assert p[str(ids[1])]["history"] == {"introduced": True, "responded": False, "accepted": False, "leased": False}
    assert p[str(ids[2])]["history"] == {"introduced": False, "responded": False, "accepted": False, "leased": False}
    assert out["history"]["residents"] == {"introduced": 2, "responded": 1, "accepted": 1, "leased": 0}
    # the alternatives that contain an introduced person cannot be introduced, and the ledger says why in plain words
    blocked = [g for g in out["groups"] if not g["is_primary"] and set(g["form_ids"]) & {ids[0], ids[1]}]
    assert blocked and all("member_committed_elsewhere" in {r["code"] for r in g["reasons"]} for g in blocked)
    db.execute("UPDATE enquiries SET status='Withdrawn' WHERE id=?", (ids[3],))
    db.commit()
    out = _ledger(db)
    assert _people(out)[str(ids[3])]["state"] == "not_in_pool" and out["coverage"]["ready_proposal_residents"] == 0   # the (3,4) group no longer passes


# ------------------------------------------------------------------------------------------------ wording and the summary
def test_staff_text_carries_no_register_codes_or_internal_refs(db, monkeypatch):
    fixed = list(ol.CATEGORY_LABEL.values()) + list(ol.DATE_FAILURE_TEXT.values()) + [ol.BUDGET_NOTE, ol.SEARCH_NOT_PROOF, ol.STOCK_DIAGNOSTIC_NOTE]
    fixed += [t for _k, _c, text, act in ol._REASON_MAP for t in (text, act)] + [t for v in ol._HELD_BASIS.values() for t in v[1:]] + list(ol._GENERIC_REASON[1:])
    assert not [t for t in fixed if CODE.search(t)]
    # an internal reason the ledger does not know is replaced by the generic sentence, never passed through
    got = ol._plain_reasons(["hard rules no longer pass: Lease shorter than the apartment's minimum (A9)", "some new internal reason (GP7) for U2"])
    assert [r["code"] for r in got] == ["requirements_no_longer_met", "other"] and not any(CODE.search(r["text"]) or "U2" in r["text"] for r in got)
    assert ol._safe_text("Step-free access needs confirmation from the team before introduction") and ol._safe_text("replace U2 under GP7") is None
    _ready_pairs(db, monkeypatch)
    db.execute("UPDATE groups SET ai_status='held', ranking_basis='Held for staff (AI review)', held_reason='AI review: hold. U1 and U2 clash (M3)' WHERE is_primary=1")
    db.commit()
    out = _ledger(db)

    def texts(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k not in ("name", "apartment"):
                    yield from texts(v)
        elif isinstance(x, list):
            for v in x:
                yield from texts(v)
        elif isinstance(x, str):
            yield x
    leaked = [t for t in texts(out) if CODE.search(t) or re.search(r"\bU\d\b", t)]
    assert not leaked, leaked
    assert out["states"]["primary_held"]["residents"] == 4 and {p["reason_code"] for p in out["people"]} == {"ai_review_hold"}
    assert all("hold_detail" not in f or not re.search(r"\bU\d\b|M3", f["hold_detail"]) for f in out["forms"])


def test_summary_is_counts_only_and_matches_the_full_ledger(db, monkeypatch):
    _ready_pairs(db, monkeypatch)
    _enq(db, "Late", "late@example.com", **SWE)
    full, before = _ledger(db), _snapshot(db)
    s = ol.summary(db, "test")
    assert _snapshot(db) == before
    assert "people" not in s and "forms" not in s and "groups" not in s
    assert s["states"] == full["states"] and s["coverage"] == full["coverage"] and s["totals"] == full["totals"] and s["stock"] == full["stock"]
    assert ol.get_ledger(db, "real")["totals"]["enquiry_forms"] == 0   # the other dataset is never mixed in
    with pytest.raises(ValueError):
        ol.get_ledger(db, "everything")


def test_consistency_is_checked_not_assumed(sqlite_db, monkeypatch):
    """The ledger is many reads with no lock and no transaction. It must say, from an actual check, whether anything changed
    while it read: one moment only when nothing did; otherwise it reads again, and if it still cannot, it says so plainly.
    The check is SQLite's, so this runs on an isolated SQLite database whatever the suite's own database is."""
    db = sqlite_db
    _ready_pairs(db, monkeypatch)
    path = [r[2] for r in db.execute("PRAGMA database_list").fetchall() if r[1] == "main"][0]
    quiet = _ledger(db)   # nothing else writing: one moment at the first try (asserted in _ledger), and no transaction left open
    assert not db.in_transaction and "same moment" in quiet["consistency"]["text"]
    real_load = ol._load_groups
    tick = [0]

    def stamp():   # a real change to a column no rule or review reads (an update that changes nothing is not a change)
        tick[0] += 1
        return f"2026-11-08T00:00:{tick[0]:02d}+00:00"

    def same_connection_write():   # another thread of the app writing through the shared connection
        db.execute("UPDATE apartments SET updated_at=?", (stamp(),))
        db.commit()

    def other_connection_write():   # another process committing through its own connection
        other = sqlite3.connect(path, timeout=15)
        try:
            other.execute("UPDATE apartments SET updated_at=?", (stamp(),))
            other.commit()
        finally:
            other.close()

    for write in (same_connection_write, other_connection_write):
        for busy_passes, attempts, want_single, want_tried in ((1, 3, True, 2), (2, 3, True, 3), (9, 3, False, 3), (1, 1, False, 1)):
            left = [busy_passes]

            def load(c, is_test):
                out = real_load(c, is_test)
                if left[0] > 0:
                    left[0] -= 1
                    write()   # lands in the middle of the ledger's reads
                return out
            monkeypatch.setattr(ol, "_load_groups", load)
            out = ol.get_ledger(db, "test", attempts=attempts)
            monkeypatch.setattr(ol, "_load_groups", real_load)
            con = out["consistency"]
            assert (con["single_instant"], con["attempts"], con["checked"]) == (want_single, want_tried, True), (write.__name__, busy_passes, attempts, con)
            if want_single:
                assert out["states"] == quiet["states"] and con["text"] not in out["limitations"]
            else:   # still reported, never dressed up as one instant
                assert "slightly different moments" in con["text"] and con["text"] in out["limitations"] and "same moment" not in con["text"]
            assert not db.in_transaction

    # a write left open on the shared connection (someone mid-change): what is read may include unsaved rows - not one moment
    db.execute("UPDATE apartments SET updated_at=?", (stamp(),))
    assert db.in_transaction
    con = ol.get_ledger(db, "test", attempts=2)["consistency"]
    assert con["single_instant"] is False and con["attempts"] == 2
    assert db.in_transaction, "the ledger must not commit or roll back someone else's open write"
    db.rollback()
    assert ol.summary(db, "test")["consistency"]["single_instant"] is True

    # a database the ledger has no change marker for: NOT VERIFIED, said once, no pretence and no retry loop
    monkeypatch.setattr(ol, "_change_marker", lambda c: None)
    out = ol.get_ledger(db, "test")
    con = out["consistency"]
    assert con["single_instant"] == ol.NOT_VERIFIED and con["checked"] is False and con["attempts"] == 1
    assert "not a picture of one instant" in con["text"] and con["text"] in out["limitations"]
    monkeypatch.undo()

    class NotSqlite:   # the marker itself refuses any backend it was not written for
        backend = "postgres"
    assert ol._change_marker(NotSqlite()) is None and ol._change_marker(object()) is None
    assert ol._change_marker(db) is not None


def test_a_frozen_copy_is_measured_without_touching_it(sqlite_db, monkeypatch, tmp_path):
    """Measurement mode: a frozen copy of a database is read through a connection that cannot write, runs no migration and
    leaves no file beside the copy; the ledger from it equals the ledger from the database it was copied from.
    A frozen copy is a SQLite file, so this runs on an isolated SQLite database whatever the suite's own database is."""
    import hashlib
    import os
    import shutil
    db = sqlite_db
    _ready_pairs(db, monkeypatch)
    db.execute("UPDATE groups SET ai_status='failed' WHERE id=(SELECT MAX(id) FROM groups WHERE status='Proposed' AND is_primary=1)")
    db.commit()
    source = _ledger(db)
    src = [r[2] for r in db.execute("PRAGMA database_list").fetchall() if r[1] == "main"][0]
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # the copy must hold everything: no write-ahead data left outside the file
    froze = tmp_path / "frozen"
    froze.mkdir()
    copy = str(froze / "copy.db")
    shutil.copyfile(src, copy)
    os.chmod(copy, 0o444)
    digest = hashlib.sha256(open(copy, "rb").read()).hexdigest()
    c = ol.open_frozen(copy)
    try:
        out = ol.get_ledger(c, "test")
        for k in ("totals", "states", "coverage", "stock", "people", "forms", "groups", "history", "unresolved_reasons", "search"):
            assert out[k] == source[k], k
        assert out["consistency"]["single_instant"] is True and out["consistency"]["attempts"] == 1
        assert out["coverage"]["ready_proposal_residents"] == 2 and out["states"]["primary_failed_or_pending"]["residents"] == 2
        with pytest.raises(sqlite3.OperationalError):
            c.execute("UPDATE apartments SET address='changed'")
    finally:
        c.close()
    assert hashlib.sha256(open(copy, "rb").read()).hexdigest() == digest and sorted(os.listdir(froze)) == ["copy.db"]
    # a file with unsaved write-ahead data beside it is not a frozen copy; a missing file is an error, not an empty ledger
    open(copy + "-wal", "wb").write(b"x" * 64)
    with pytest.raises(ValueError):
        ol.open_frozen(copy)
    with pytest.raises(FileNotFoundError):
        ol.open_frozen(str(froze / "absent.db"))


def test_the_no_write_check_fails_when_the_ledger_is_made_to_write(db, monkeypatch):
    """Control for `_ledger`'s own guarantee, on whichever database the suite is using. A real write is slipped into the middle
    of the ledger's reads: the helper must fail. On PostgreSQL the server itself must have refused the write, the data must be
    unchanged and the session must be handed back as it was. A rewrite that changes no value must also be seen."""
    _apt(db)
    _enq(db, "Solo", "solo@example.com", **SWE)
    quiet = _ledger(db)   # nothing slipped in: the helper passes
    session_mode = db._pg.read_only if _pg(db) else None
    real_load, tried = ol._load_groups, []

    def load(c, is_test):
        out = real_load(c, is_test)
        if not tried:   # once, in the middle of the ledger's reads
            try:
                c.execute("UPDATE apartments SET updated_at=?", ("2026-11-08T00:00:01+00:00",))
                c.commit()
                tried.append("written")
            except Exception as e:   # swallowed, as a careless caller might: the helper must still notice
                tried.append(getattr(e, "sqlstate", None) or type(e).__name__)
        return out

    before = _snapshot(db)
    monkeypatch.setattr(ol, "_load_groups", load)
    with pytest.raises(AssertionError, match="the ledger must not write"):
        _ledger(db)
    monkeypatch.setattr(ol, "_load_groups", real_load)
    if _pg(db):
        assert tried == ["25006"], tried   # read_only_sql_transaction: refused by the server, not by anything in this file
        assert _snapshot(db) == before     # nothing was changed. (The snapshot shows that; it is not what caught the attempt.)
        assert not db.in_transaction and not db._dirty and db._pg.read_only == session_mode   # handed back as it was found
    else:
        assert tried == ["written"] and _snapshot(db) != before   # SQLite has no such guard: the write landed, and was counted

    # a rewrite that changes no value: seen through the row versions on PostgreSQL, through the change counter on SQLite
    def values(snap):
        return {t: sorted(json.dumps({k: v for k, v in r.items() if k not in ("_row_place", "_row_version")}, sort_keys=True, default=str) for r in rs)
                for t, rs in snap.items()}
    before, changes = _snapshot(db), getattr(db, "total_changes", None)
    db.execute("UPDATE apartments SET address=address")
    db.commit()
    after = _snapshot(db)
    assert values(after) == values(before)
    if _pg(db):
        assert {t for t in after if after[t] != before[t]} == {"apartments"}
    else:
        assert after == before and db.total_changes > changes

    # and the session is still an ordinary one: a normal write goes through, and the ledger reads cleanly again
    db.execute("UPDATE apartments SET updated_at=?", ("2026-11-08T00:00:02+00:00",))
    db.commit()
    assert row(db.execute("SELECT updated_at FROM apartments"))["updated_at"] == "2026-11-08T00:00:02+00:00"
    again = _ledger(db)
    assert again["states"] == quiet["states"] and again["totals"] == quiet["totals"]



# ------------------------------------------------------------------------------------------------ version 3 amendments
def test_search_wording_claims_only_what_the_recorded_counters_establish(db):
    """Root, 10 Nov 2026: a historical run summary without the search counters must not be described as complete in any part
    (an older build did not compare every rule-valid pair). Absent or unreadable counters are NOT VERIFIED and the prose says
    only that the recorded counters do not establish an exhaustive search."""
    assert ol._search(db, "test")["exhaustive"] == ol.NOT_VERIFIED and "NOT VERIFIED" in ol._search(db, "test")["text"]   # no run at all
    _apt(db)
    _pool(db)
    matching.full_run(db, "tester", dataset="test")
    run = row(db.execute("SELECT id, summary_json FROM match_runs WHERE dataset='test' AND kind='full' AND status='done' ORDER BY id DESC LIMIT 1"))
    recorded = json.loads(run["summary_json"])
    limits = ("search_targets_truncated", "search_seat_options_untried", "search_targets_stopped_at_first_valid", "selection_truncated",
              "coverage_selection_truncated")
    full = {**{k: 0 for k in limits}, "search_larger_groups_exhaustive": True, "complete": True}

    def with_summary(summary):
        db.execute("UPDATE match_runs SET summary_json=? WHERE id=?", (json.dumps(summary), run["id"]))
        db.commit()
        out = _ledger(db)   # also asserts that no string claims pairs were "compared completely" or a "complete search"
        assert out["search"] == ol._search(db, "test")
        return out["search"], out

    neutral = "the recorded counters do not establish an exhaustive search"
    strip = {k: v for k, v in recorded.items() if not k.startswith("search_") and "selection_truncated" not in k and k != "complete"}
    for absent in ({}, strip, {"complete": True}, {"search_larger_groups_exhaustive": True, "complete": True},
                   {k: v for k, v in full.items() if k != "coverage_selection_truncated"},      # one counter this build may not write
                   {**full, "search_targets_truncated": "many"}, {**full, "complete": "yes"}, {**full, "selection_truncated": True}):
        s, out = with_summary(absent)
        assert s["exhaustive"] == ol.NOT_VERIFIED and s["uncertain"] is True and s["flags_not_recorded"], absent
        assert neutral in s["text"] and "NOT VERIFIED" in s["text"] and "bounded" not in s["text"] and "groups of two" not in s["text"], absent
        assert neutral in " ".join(out["limitations"])
        for f in out["forms"]:
            if f.get("reason_code") == "not_found_by_search":
                assert neutral in f["outcome"]
    assert set(with_summary({})[0]["flags"].values()) == {ol.NOT_VERIFIED}

    s, _ = with_summary(full)   # every counter recorded and none reached: the only case reported as not uncertain
    assert s["exhaustive"] is True and s["uncertain"] is False and s["flags_not_recorded"] == [] and s["text"] == "The last run's recorded counters show no search limit was reached."

    s, _ = with_summary({"search_targets_truncated": 2})   # a recorded limit is a recorded fact even when the rest is absent
    assert s["exhaustive"] is False and s["bounded_by"] == ["search_targets_truncated"] and s["flags_not_recorded"]
    assert "stopped at its limits" in s["text"] and "not an exhaustive search" in s["text"] and "were not recorded" in s["text"]
    s, _ = with_summary({**full, "search_larger_groups_exhaustive": False})
    assert s["exhaustive"] is False and s["bounded_by"] == [] and "not an exhaustive search" in s["text"] and "not recorded" not in s["text"]
    for k in limits:
        s, _ = with_summary({**full, k: 1})
        assert s["exhaustive"] is False and s["bounded_by"] == [k] and ol._SEARCH_LIMIT_TEXT[k] in s["text"] and not CODE.search(s["text"])


def test_notes_reading_hold_and_scheduled_retry_use_the_engines_own_constants(db, monkeypatch):
    """The two labels come from ai_queue (imported, never copied). On a build that does not define them the ledger has no such
    entry; either way a held or pending group is never ready and never counted."""
    import importlib
    held_const = getattr(ai_queue, "NOTES_HELD_BASIS", None)
    retry_const = getattr(ai_queue, "NOTES_RETRY_SCHEDULED_TEXT", None)
    src = open(ol.__file__, encoding="utf-8").read()
    assert "a member's notes could not be read)" not in src and "will be read again shortly)" not in src   # the strings are not duplicated
    if held_const is None:
        assert ol._NOTES_HELD_BASIS is None and all(v[0] != "notes_not_read" for v in ol._HELD_BASIS.values())
        monkeypatch.setattr(ai_queue, "NOTES_HELD_BASIS", "Held for staff (fabricated constant for this test)", raising=False)
        monkeypatch.setattr(ai_queue, "NOTES_RETRY_SCHEDULED_TEXT", "AI assessment pending (fabricated constant for this test)", raising=False)
    try:
        importlib.reload(ol)
        held_const, retry_const = ai_queue.NOTES_HELD_BASIS, ai_queue.NOTES_RETRY_SCHEDULED_TEXT
        assert ol._HELD_BASIS[held_const][0] == "notes_not_read"
        _apt(db)
        _pool(db)
        matching.full_run(db, "tester", dataset="test")
        prim = rows(db.execute("SELECT id FROM groups WHERE status='Proposed' AND is_primary=1 ORDER BY id"))
        assert len(prim) == 2
        db.execute("UPDATE groups SET ai_status='held', ranking_basis=?, held_reason=NULL WHERE id=?", (held_const, prim[0]["id"]))
        db.execute("UPDATE groups SET ai_status='pending', ranking_basis=? WHERE id=?", (retry_const, prim[1]["id"]))
        db.commit()
        out = _ledger(db)
        held = [f for f in out["forms"] if f["group_id"] == prim[0]["id"]]
        waiting = [f for f in out["forms"] if f["group_id"] == prim[1]["id"]]
        assert held and all(f["state"] == "primary_held" and f["reason_code"] == "notes_not_read" and f["ai_processing_incomplete"] is True for f in held)
        assert all("could not be read after repeated attempts" in f["outcome"] and "has not been reviewed or approved" in f["outcome"] for f in held)
        assert waiting and all(f["state"] == "primary_failed_or_pending" and f["reason_code"] == "notes_retry_scheduled" and f["ai_processing_incomplete"] is True
                               for f in waiting)
        assert all("Another attempt is scheduled" in f["outcome"] and "has not run" in f["outcome"] for f in waiting)
        assert out["coverage"]["ready_proposal_residents"] == 0 and not any(p["counts_as_ready_proposal"] for p in out["people"])
        assert out["states"]["primary_held"]["residents"] == 2 and out["states"]["primary_failed_or_pending"]["residents"] == 2
        for t in _strings(out):
            assert not CODE.search(t) and "fabricated constant" not in t and held_const not in t and retry_const not in t, t
        # a held basis the ledger does not know stays a plain generic hold - never guessed at
        db.execute("UPDATE groups SET ranking_basis='Held for staff (something new)' WHERE id=?", (prim[0]["id"],))
        db.commit()
        assert {f["reason_code"] for f in _ledger(db)["forms"] if f["group_id"] == prim[0]["id"]} == {"held_other"}
    finally:
        monkeypatch.undo()
        importlib.reload(ol)
    assert ol._NOTES_HELD_BASIS == getattr(ai_queue, "NOTES_HELD_BASIS", None)
