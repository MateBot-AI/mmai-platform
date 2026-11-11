import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("MMAI_SCHEDULER", "0")  # T8 scheduler thread stays off in tests; scheduler.check() is tested directly
os.environ.setdefault("MMAI_AI_QUEUE", "0")   # T6 AI queue worker stays off in tests; ai_queue.process_pending() is called directly
# Test-only first passwords for the seeded accounts on a fresh database (real passwords are never in the repository)
TEST_PW = {"bill": "Test-Manager-0001", "anna": "Test-Anna-0002"}
for _u, _pw in TEST_PW.items():
    os.environ.setdefault(f"MMAI_INITIAL_PASSWORD_{_u.upper()}", _pw)
os.environ.setdefault("MMAI_TRUST_VISITOR_ID", "1")   # the preview fallback is exercised by the T7 test
os.environ.setdefault("MMAI_RATE_LIMITING", "0")     # per-IP limits are off for the suite; test_security.py turns them on explicitly

# Postgres run (T17): MMAI_TEST_DATABASE_URL=postgresql://... makes every test use that database instead of a SQLite file.
# The schema is dropped and recreated before each test so tests stay independent, as they are with one SQLite file per test.
if os.environ.get("MMAI_TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["MMAI_TEST_DATABASE_URL"]

from server import ai, rules  # noqa: E402


def fake_staff_briefs(packet):
    """Deterministic T4(f) brief for tests, in the strict evidence shape: every note cites fact ids from the packet's catalogue
    (server/facts.py); party members get party-wide facts only plus their personal facts as missing."""
    from server import facts
    cat = facts.catalogue(packet)
    members = packet.get("members_for_ai") or []
    refs = [m["ref"] for m in members] + [r for m in members for r in (m.get("party_members") or [])]

    def own(ref, status, scope=None):
        return [k for k, v in cat.items() if v.get("ref") == ref and v["status"] == status and (scope is None or v["scope"] == scope)]
    first_apt = next((f"A{a['apartment_id']}" for a in packet.get("apartments") or []), None)
    group = {"strengths": [{"fact_ids": own(members[0]["ref"], "recorded")[:2], "note": "fake group strength"}] if own(members[0]["ref"], "recorded") else [],
             "tensions": [], "missing_facts": [], "what_could_work": [], "what_could_cause_issues": [],
             "discussion_points": [{"fact_ids": [f"R.{first_apt}.budget"] if first_apt and f"R.{first_apt}.budget" in cat else own(members[0]["ref"], "recorded")[:1], "note": "fake discussion point"}]}
    out = []
    for r in refs:
        rec, miss = own(r, "recorded"), own(r, "missing", "personal")
        out.append({"ref": r, "strengths": [{"fact_ids": rec[:1], "note": f"{r} fake strength"}] if rec else [], "tensions": [],
                    "missing_facts": [{"fact_ids": miss, "note": f"{r}: personal facts not recorded"}] if miss else [], "discussion_points": []})
    return {"group": group, "members": out}


def fake_findings(g):
    """Evidence-cited findings for a fake review of one packet group."""
    from server import facts
    cat = facts.catalogue(g)
    out = []
    for m in g.get("members_for_ai") or []:
        ids = [k for k, v in cat.items() if v.get("ref") == m["ref"] and v["status"] == "recorded"][:2]
        if ids:
            out.append({"fact_ids": ids, "note": f"{m['ref']} fake finding"})
    return out


def fake_assess_groups(apartment, groups):
    """Strict-shape double for ai.assess_groups (candidate refills at one apartment): rating falls with position."""
    return [{"index": i, "rating": 9 - i * 0.5, "rationale": f"refill {i}", "verdict": "approve", "findings": fake_findings(g),
             "apartments": [{"apartment_id": a["apartment_id"], "reason": "r"} for a in g["apartments"]]} for i, g in enumerate(groups)]


@pytest.fixture(autouse=True)
def _staff_briefs_double(monkeypatch):
    """T6/T4(f): every approved review is followed by a staff-brief call before a group can be ready. The suite has no AI, so the
    brief call is replaced by a deterministic double for every test; tests about brief failure override it explicitly with
    monkeypatch.setattr(ai, "staff_briefs", ...). The review call itself is NOT doubled here."""
    monkeypatch.setattr(ai, "staff_briefs", fake_staff_briefs)


@pytest.fixture(autouse=True)
def _notes_extraction_double(monkeypatch):
    """GAP-T4c v2: a member's free text is read by one bounded extraction call before the group review. The suite has no AI, so the
    call is replaced by a deterministic double that returns no items (text present, nothing extracted); T4c tests supply controlled
    replies with monkeypatch.setattr(ai_queue, "_extract_notes", ...). The group-review call itself is NOT doubled here."""
    from server import ai_queue
    monkeypatch.setattr(ai_queue, "_extract_notes", lambda fields, party_size, meter: {"items": [], "residual": False})


def person(pid="p1", unit=None, name=None, country="Sweden", budget=600.0,
           arrangements=("Own room", "Share with one other"), bunk_ok=True, smokes=False,
           smoke_ok=True, is_lead=True, couple=False):
    # I8 (8 Nov 2026): people carry no sex, date of birth or sex preference. Launch scope (8 Nov 2026): no origin preferences
    # either (avoid_*/own_region are retired); `country` is identity only and never influences any engine result.
    return rules.Person(id=pid, unit_id=unit or pid, name=name or pid, birth_country=country, budget_max=budget,
                        arrangements=set(arrangements), bunk_ok=bunk_ok, smokes=smokes, smoke_ok=smoke_ok,
                        is_lead=is_lead, is_couple_member=couple)


ADULTS_DECLARED = object()   # sentinel: "declared for this unit's own move-in date"


def unit(uid, people=None, kind="single", sizes=("2 bedrooms", "3 bedrooms", "1 bedroom", "Studio"), mate_max=6,
         move_in=date(2027, 2, 1), in_flex=7, move_out=date(2028, 2, 1), out_flex=14, open_ext=False, adults_for=ADULTS_DECLARED, **pkw):
    """`adults_for` is the synthetic I11(d) fact for the fixture: the move-in date the enquirer's all-occupants 18+ declaration
    was made for. Default = the unit's own move-in date (what a fresh submission records). Pass None for "no declaration"."""
    ppl = people or [person(uid, unit=uid, **pkw)]
    return rules.Unit(id=uid, kind=kind, people=ppl, city="Melbourne", apt_sizes=set(sizes), mate_max=mate_max,
                      move_in=move_in, move_in_flex=in_flex, move_out=move_out, move_out_flex=out_flex, open_to_extending=open_ext,
                      adults_confirmed_move_in=(move_in if adults_for is ADULTS_DECLARED else adults_for))


def apartment(code="2B2B", rent12=1150.0, rent6=1225.0, avail=date(2027, 1, 20), bedrooms=None, min_lease=rules.DEFAULT_MIN_LEASE_MONTHS, **kw):
    if bedrooms is None:
        bedrooms = {"Studio": ["1 king or 2 singles"], "1BR": ["1 king or 2 singles"],
                    "2B1B": ["1 king or 2 singles", "1 king or 2 singles"], "2B2B": ["1 king or 2 singles", "1 king or 2 singles"],
                    "3B": ["1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"]}[code]
    return rules.Apartment(id="a1", address="Unit 1 / 1 Test Street", type_code=code, rent_12m=rent12, rent_6m=rent6, available_from=avail,
                           bedrooms=[rules.parse_bedding(b) for b in bedrooms], min_lease_months=min_lease, **kw)


def check(apt, units, today=None):
    return rules.check_group(apt, units, today=today)


# A17(a) counts future vacancy from the business date. The suite's fixtures live in 2027, so the business clock is pinned to a
# fixed date for determinism (a test input, not a live default: production never touches lease_dates.CLOCK).
TEST_TODAY = date(2026, 11, 8)


@pytest.fixture(autouse=True)
def _pinned_business_clock():
    from server import lease_dates
    before = lease_dates.CLOCK
    lease_dates.CLOCK = lambda: TEST_TODAY
    try:
        yield
    finally:
        lease_dates.CLOCK = before


@pytest.fixture(autouse=True)
def _fresh_postgres_schema():
    from server import db as dbmod
    if dbmod.is_postgres():
        import psycopg
        if dbmod._conn is not None:
            try:
                dbmod._conn.close()
            except Exception:
                pass
            dbmod._conn = None
        with psycopg.connect(dbmod.database_url(), autocommit=True) as c:
            # end any CLIENT session of OUR role that a previous test left open, then rebuild the schema (lock_timeout so a leak fails
            # loudly, never hangs). Only client backends owned by the test role are signalled: autovacuum / background workers on
            # the same database belong to the server, and terminating them needs privileges the test role does not (and should not) have.
            c.execute("""SELECT pg_terminate_backend(pid) FROM pg_stat_activity
                         WHERE datname=current_database() AND pid<>pg_backend_pid()
                           AND backend_type='client backend' AND usename=current_user""")
            c.execute("SET lock_timeout='15s'; DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    yield


@pytest.fixture
def db(tmp_path):
    os.environ["MMAI_DB"] = str(tmp_path / "test.db")
    from server import db as dbmod
    c = dbmod.connect(str(tmp_path / "test.db"))
    yield c
    c.close()
