"""3.7 first-choice room arrangement scoring (10 Nov 2026, reviewer finding): the hit test compared only the first word of the
first choice with the allocated arrangement, so "Share a room with one other person" counted as a full hit when the person was
allocated "Share with two others" (both start with "Share"). The comparison is now an exact arrangement label; the weight
(1.0) and the hit / miss values (1.0 / 0.4) are unchanged. Positive exact-choice controls, negative accepted-but-not-first-choice
controls and the couple variant; the rest of the score is held identical by scoring the SAME group with a modified allocation."""
import copy

from server import matching, rules
from tests.test_launch_neutral import _apt, _enq

TWIN = "Share a room with one other person"
TRIPLE = "Share a room with two others"


def _units(db):
    return {int(u.id): u for u in matching.load_pool(db, "test")}


def _parts(apt, units, check):
    sc, parts = matching.preference_score(apt, units, check)
    return sc, parts


def test_first_choice_mapping_is_exact():
    f = matching.first_choice_arrangement
    assert f("Own room") == "Own room" and f(TWIN) == "Share with one other" and f(TRIPLE) == "Share with two others"
    assert f("Couple sharing one bed") == "Couple sharing one bed"
    assert f("Share with one other") == "Share with one other" and f("Share with two others") == "Share with two others"   # label form
    assert f("") is None and f(None) is None and f("Anything") is None


def test_twin_first_choice_is_not_a_hit_when_allocated_a_triple(db):
    # two people who accept own room, twin or triple, FIRST choice twin, at a 2B2B: the real allocation gives each an own room
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    e1 = _enq(db, "Pia", "pia@example.invalid", rooms=["Own room", TWIN, TRIPLE], room_first=TWIN, budget_option="2B2B-single", mate_ideal="3", mate_max="5")
    e2 = _enq(db, "Quin", "quin@example.invalid", rooms=["Own room", TWIN, TRIPLE], room_first=TWIN, budget_option="2B2B-single", mate_ideal="3", mate_max="5")
    db.commit()
    apt = matching.apartment_from_row(a)
    u = _units(db)
    units = [u[e1["id"]], u[e2["id"]]]
    chk = rules.check_group(apt, units)
    assert chk.ok and chk.allocation
    assert all(o["arrangement"] == ["Own room"] for r in chk.allocation for o in r["occupants"])
    sc_own, parts_own = _parts(apt, units, chk)
    assert parts_own["room_choice"] == 0.4   # natural negative control: accepted arrangement, not the first choice

    def with_arrangement(label):
        c2 = copy.deepcopy(chk)
        for r in c2.allocation:
            for o in r["occupants"]:
                o["arrangement"] = [label]
        return c2
    sc_hit, parts_hit = _parts(apt, units, with_arrangement("Share with one other"))
    assert parts_hit["room_choice"] == 1.0   # positive control: exact first choice met
    sc_triple, parts_triple = _parts(apt, units, with_arrangement("Share with two others"))
    assert parts_triple["room_choice"] == 0.4, "a triple is not the twin first choice even though both labels start with 'Share'"
    for p in (parts_own, parts_triple):
        assert {k: v for k, v in p.items() if k != "room_choice"} == {k: v for k, v in parts_hit.items() if k != "room_choice"}
    assert sc_hit > sc_triple == sc_own
    # the opposite direction: first choice triple -> only "Share with two others" is a hit
    for x in units:
        x.extra["room_first"] = TRIPLE
    assert _parts(apt, units, with_arrangement("Share with one other"))[1]["room_choice"] == 0.4
    assert _parts(apt, units, with_arrangement("Share with two others"))[1]["room_choice"] == 1.0
    # own room first choice: exact as well
    for x in units:
        x.extra["room_first"] = "Own room"
    assert _parts(apt, units, chk)[1]["room_choice"] == 1.0
    assert _parts(apt, units, with_arrangement("Share with one other"))[1]["room_choice"] == 0.4


def test_couple_first_choice_variant(db):
    a = _apt(db, rent12=1300.0, rent6=1300.0)
    e1 = _enq(db, "Rae", "rae@example.invalid", rooms=["Own room"], room_first="Own room", budget_option="2B2B-single")
    e2 = _enq(db, "Sol", "sol@example.invalid", rooms=["Own room"], room_first="Own room", budget_option="2B2B-single")
    db.commit()
    apt = matching.apartment_from_row(a)
    u = _units(db)
    units = [u[e1["id"]], u[e2["id"]]]
    chk = rules.check_group(apt, units)
    assert chk.ok and _parts(apt, units, chk)[1]["room_choice"] == 1.0
    chk2 = copy.deepcopy(chk)
    for x in units:
        x.extra["room_first"] = "Couple sharing one bed"
    assert _parts(apt, units, chk2)[1]["room_choice"] == 0.4      # couple first choice, allocated own rooms: not met
    for r in chk2.allocation:
        for o in r["occupants"]:
            o["arrangement"] = ["Couple sharing one bed"]
    assert _parts(apt, units, chk2)[1]["room_choice"] == 1.0      # exact couple arrangement: met
    for r in chk2.allocation:
        for o in r["occupants"]:
            o["arrangement"] = ["Share with one other"]
    assert _parts(apt, units, chk2)[1]["room_choice"] == 0.4      # twin share is not the couple arrangement
