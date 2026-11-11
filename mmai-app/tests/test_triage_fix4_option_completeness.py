"""Triage fix 4 (9 Nov 2026, GP6(a) consistency): a candidate group's apartment options are evaluated over the whole available
stock before the top-N trim, not only at the apartments where the greedy per-apartment builder happened to produce that member set.
Invariant checked after a rules-only full run over the seeded test pool and the real apartment list: for EVERY Proposed group the
stored options are exactly the first APARTMENTS_PER_GROUP of all rule-passing available apartments in the builder's own order
(rule score desc, whole weekly rent asc, apartment id). Nothing about grouping, weights, primaries or gates is asserted to change."""
import os

import pytest

from server import importer, matching, rules, seed
from server.db import rows

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = next((p for p in (os.path.join(_APP, "data", "ALM_Apartments_2026-10-23.xlsx"), os.path.join(os.path.dirname(_APP), "data", "ALM_Apartments_2026-10-23.xlsx")) if os.path.exists(p)), "")


def _expected_options(db, units_by_id, member_key, apt_rows):
    ids = [int(x) for x in member_key.split(",")]
    gunits = [units_by_id[i] for i in ids]
    opts = []
    for a, apt in apt_rows:
        if not all(matching._unit_fits_apartment(apt, u) for u in gunits):
            continue
        chk = rules.check_group(apt, gunits)
        if chk.ok:
            sc, _ = matching.preference_score(apt, gunits, chk)
            opts.append((-sc, chk.total_weekly, int(a["id"])))
    return [(aid, -negsc) for negsc, _, aid in sorted(opts)]


@pytest.mark.skipif(not os.path.exists(XLSX), reason="apartment list not present")
def test_every_proposed_group_offers_the_top_n_of_all_rule_passing_stock(db):
    importer.import_apartments(db, XLSX, "test")
    seed.seed_test_data(db, n=120)
    n_keep = 3
    res = matching.full_run(db, "tester", "test", apartments_per_group=n_keep)
    assert res["groups_total"] > 0 and res["ai_calls"] == 0
    units_by_id = {int(u.id): u for u in matching.expand(matching.load_pool(db, "test"))}
    apts = rows(db.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL ORDER BY available_from, address"))
    apt_rows = [(a, matching.apartment_from_row(a)) for a in apts]
    matching.load_rejections(db)
    checked = trimmed = 0
    for g in rows(db.execute("SELECT id, member_key, rule_score FROM groups WHERE status='Proposed' AND match_run_id=?", (res["run_id"],))):
        stored = [(int(r["apartment_id"]), float(r["rule_score"])) for r in rows(db.execute("SELECT apartment_id, rule_score FROM group_apartments WHERE group_id=? ORDER BY rule_rank", (g["id"],)))]
        expected = _expected_options(db, units_by_id, g["member_key"], apt_rows)
        assert [a for a, _ in stored] == [a for a, _ in expected[:n_keep]], (g["id"], stored, expected[:n_keep + 2])
        assert all(abs(s - e) < 1e-9 for (_, s), (_, e) in zip(stored, expected))
        assert abs(float(g["rule_score"]) - expected[0][1]) < 1e-9
        checked += 1
        trimmed += len(expected) > n_keep
    assert checked == res["groups_total"]
    assert trimmed > 0, "the fixture never exercised the trim; make the pool produce groups with more than n_keep passing apartments"
