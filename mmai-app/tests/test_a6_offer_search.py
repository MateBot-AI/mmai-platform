"""A6 / A9(c) feasible-date search (8 November 2026): the engine must put forward the LONGEST offer inside the accepted windows
that passes every existing rule (A9 minimum, A10 exit windows, A17 vacancy, A16 household budget) at the tariff that offer
actually attracts, instead of only trying the earliest start + latest exit. Windows are never widened or capped.

The pruned tariff-boundary search in rules.choose_lease_offer is proved against an exhaustive finite-window oracle here: every
(start, end) pair in the accepted windows is assessed with the SAME primitive checks and the best one (longest stay, ties to the
earliest start) must equal the engine's choice, for both price directions (6m dearer, 6m cheaper, equal)."""
from datetime import date, timedelta
import random

import pytest

from server import lease_dates, rules
from tests.conftest import apartment, check, unit

TODAY = date(2026, 11, 8)


def _oracle(apt, units, today, people):
    """Exhaustive: every start in the verified start window × every end in the move-out window, same primitive checks."""
    win_in, win_out, err = rules._lease_bounds(apt, units)
    if err:
        return None
    code = rules.normalise_type(apt.type_code)
    best = None
    s = win_in[0]
    while s <= win_in[1]:
        e = win_out[0]
        while e <= win_out[1]:
            ok, _ = rules.exit_ok(s, e, apt.min_lease_months)
            if ok and lease_dates.check_vacancy(today, apt.available_from, s, e).passed:
                kind = rules.rate_kind(s, e)
                rent = apt.rent_6m if kind == "6m" else apt.rent_12m
                if not rules.household_budget(code, rent, people)[0]:
                    key = ((e - s).days, -s.toordinal())
                    if best is None or key > best[0]:
                        best = (key, s, e, kind)
            e += timedelta(days=1)
        s += timedelta(days=1)
    return None if best is None else (best[1], best[2], best[3])


def _people(units):
    return [p for u in units for p in u.people]


def _engine(apt, units, today):
    o = rules.choose_lease_offer(apt, units, today, _people(units))
    return (o["start"], o["end"], o["kind"]) if o["feasible"] else None


# ---------------- the two recorded false negatives (root probe, 8 Nov 2026) ----------------
def test_feb_start_window_crossing_1_march_takes_cheaper_12m_rate_when_6m_is_dearer():
    """Window 27 Feb .. 6 Mar, exit July: a 27 Feb start attracts the (dearer) 6m rate and breaks A16; 1 March at the 12m rate is
    within budget and still passes A9/A10/A17. The old engine held the group; the longest FEASIBLE offer is 1 Mar -> 20 Jul."""
    apt = apartment("2B2B", rent12=1150, rent6=1300, avail=date(2027, 2, 26))   # 3 empty days to 1 March: within A17 for a 141-day stay
    units = [unit("a", move_in=date(2027, 2, 27), in_flex=7, move_out=date(2027, 7, 15), out_flex=10, budget=575),
             unit("b", move_in=date(2027, 2, 27), in_flex=7, move_out=date(2027, 7, 15), out_flex=10, budget=575)]
    r = check(apt, units, today=TODAY)
    assert r.ok, r.reasons
    assert (r.lease_start, r.lease_end, r.rate_kind, r.weekly_rent) == (date(2027, 3, 1), date(2027, 7, 20), "12m", 1150)
    assert r.lease_choice and r.lease_choice["earliest_start"] == "2027-02-27" and r.lease_choice["chosen_start"] == "2027-03-01"
    assert _engine(apt, units, TODAY) == _oracle(apt, units, TODAY, _people(units))


def test_dec_start_window_crossing_1_january_takes_cheaper_6m_rate():
    """Window 28 Dec .. 4 Jan, exit July: a 28 Dec start is a 12m-rate lease; from 1 Jan the same exit is the (here cheaper) 6m
    rate and the only one within budget. Longest feasible offer: 1 Jan -> 20 Jul at the 6m rate (reversed price order)."""
    apt = apartment("2B2B", rent12=1300, rent6=1150, avail=date(2026, 12, 27))   # 5 empty days to 1 January: within A17 for a 200-day stay
    units = [unit("a", move_in=date(2026, 12, 28), in_flex=7, move_out=date(2027, 7, 15), out_flex=10, budget=575),
             unit("b", move_in=date(2026, 12, 28), in_flex=7, move_out=date(2027, 7, 15), out_flex=10, budget=575)]
    r = check(apt, units, today=TODAY)
    assert r.ok, r.reasons
    assert (r.lease_start, r.lease_end, r.rate_kind, r.weekly_rent) == (date(2027, 1, 1), date(2027, 7, 20), "6m", 1150)
    assert r.lease_choice["rate_kind"] == "6m"
    assert _engine(apt, units, TODAY) == _oracle(apt, units, TODAY, _people(units))


def test_longer_feasible_stay_beats_cheaper_shorter_one_a9c():
    """A9(c): a 6m exit in July is never preferred over a feasible longer 12m exit later, whatever the price order."""
    for r12, r6 in ((1150, 1225), (1225, 1150), (1150, 1150)):
        apt = apartment("2B2B", rent12=r12, rent6=r6, avail=date(2027, 1, 20))
        units = [unit("a", move_in=date(2027, 2, 1), move_out=date(2027, 11, 1), out_flex=40, budget=700),
                 unit("b", move_in=date(2027, 2, 1), move_out=date(2027, 11, 1), out_flex=40, budget=700)]
        r = check(apt, units, today=TODAY)
        assert r.ok and r.lease_end == date(2027, 12, 7) and r.rate_kind == "12m", (r12, r6, r.reasons)
        assert r.lease_choice is None   # the baseline (earliest start, latest exit) was already the longest feasible offer


def test_no_window_widening_and_baseline_reasons_when_nothing_feasible():
    """When no candidate passes, the group is held with the baseline offer's reasons (as before); no date outside the accepted
    windows is ever proposed."""
    apt = apartment("2B2B", rent12=1150, rent6=1300, avail=date(2027, 2, 26))
    units = [unit("a", move_in=date(2027, 2, 27), in_flex=1, move_out=date(2027, 7, 15), out_flex=10, budget=575),
             unit("b", move_in=date(2027, 2, 27), in_flex=1, move_out=date(2027, 7, 15), out_flex=10, budget=575)]
    r = check(apt, units, today=TODAY)
    assert not r.ok and any("A16" in x for x in r.reasons) and r.lease_start == date(2027, 2, 27) and r.rate_kind == "6m"
    assert r.lease_choice is None and _oracle(apt, units, TODAY, _people(units)) is None


def test_fact_present_only_when_offer_differs_from_baseline():
    apt = apartment("2B2B", rent12=1150, rent6=1225, avail=date(2027, 1, 25))
    units = [unit("a", move_in=date(2027, 2, 1), move_out=date(2027, 7, 10), budget=700), unit("b", move_in=date(2027, 2, 1), move_out=date(2027, 7, 10), budget=700)]
    r = check(apt, units, today=TODAY)
    assert r.ok and r.lease_choice is None


# ---------------- oracle equivalence over randomised finite windows ----------------
def _scenario(rng):
    anchor = date(2026, 11, 20) + timedelta(days=rng.randint(0, 420))
    in_flex = rng.choice((0, 3, 7, 14, 21, 45))
    out_flex = rng.choice((0, 7, 14, 30, 60, 120))
    stay = rng.choice((60, 120, 150, 170, 200, 300, 365, 500))
    avail = anchor - timedelta(days=rng.randint(0, 25))
    r12, r6 = rng.choice(((1150, 1225), (1225, 1150), (1150, 1150), (900, 1400), (1400, 900)))
    budget = rng.choice((500, 560, 575, 600, 640, 700))
    min_lease = rng.choice((2, 2, 2, 3, 6))
    apt = apartment("2B2B", rent12=r12, rent6=r6, avail=avail, min_lease=min_lease)
    units = [unit("a", move_in=anchor, in_flex=in_flex, move_out=anchor + timedelta(days=stay), out_flex=out_flex, budget=budget),
             unit("b", move_in=anchor + timedelta(days=rng.randint(-3, 3)), in_flex=in_flex, move_out=anchor + timedelta(days=stay + rng.randint(-5, 5)), out_flex=out_flex, budget=budget)]
    today = min(TODAY, avail - timedelta(days=rng.randint(0, 40)))
    return apt, units, today


@pytest.mark.parametrize("seed", range(6))
def test_pruned_search_equals_exhaustive_oracle(seed):
    rng = random.Random(seed)
    feasible_seen = diverged = 0
    for _ in range(40):
        apt, units, today = _scenario(rng)
        want, got = _oracle(apt, units, today, _people(units)), _engine(apt, units, today)
        assert got == want, (apt.available_from, apt.rent_12m, apt.rent_6m, apt.min_lease_months, [(u.move_in, u.move_in_flex, u.move_out, u.move_out_flex) for u in units], today, want, got)
        if want:
            feasible_seen += 1
            b_start, b_end, _, _ = rules.choose_lease_dates(apt, units)
            if (want[0], want[1]) != (b_start, b_end):
                diverged += 1
    assert feasible_seen >= 5   # the scenarios exercise the feasible path, not only holds


# ---------------- A6 literal parity: a January/February start ending in a LATER July is a 6-month-rate lease ----------------

def test_jan_start_ending_july_next_year_is_6m_rate_and_feasible():
    """Root ruling 8 Nov 2026: the same-year condition was a code/register gap. A January 2027 start exiting in July 2028 attracts
    the 6-month rate (here the cheaper one), and the engine's longest feasible offer uses it."""
    apt = apartment("2B2B", rent12=1300, rent6=1150, avail=date(2027, 1, 10), min_lease=6)
    units = [unit("a", move_in=date(2027, 1, 15), in_flex=3, move_out=date(2028, 7, 10), out_flex=10, budget=575),
             unit("b", move_in=date(2027, 1, 15), in_flex=3, move_out=date(2028, 7, 10), out_flex=10, budget=575)]
    r = check(apt, units, today=TODAY)
    assert r.ok, r.reasons
    assert (r.lease_start, r.lease_end, r.rate_kind, r.weekly_rent) == (date(2027, 1, 15), date(2028, 7, 20), "6m", 1150)
    assert _engine(apt, units, TODAY) == _oracle(apt, units, TODAY, _people(units))


def test_jan_start_ending_july_next_year_dearer_6m_breaks_budget_so_12m_boundary_start_is_offered():
    """Same window, reversed price order: at the 6m rate the household is over budget, so the only feasible candidates are the
    12m-rate ones - the 1 March tariff boundary start with the same July 2028 exit (shorter, but feasible)."""
    apt = apartment("2B2B", rent12=1150, rent6=1300, avail=date(2027, 2, 20), min_lease=6)
    units = [unit("a", move_in=date(2027, 2, 25), in_flex=7, move_out=date(2028, 7, 10), out_flex=10, budget=575),
             unit("b", move_in=date(2027, 2, 25), in_flex=7, move_out=date(2028, 7, 10), out_flex=10, budget=575)]
    r = check(apt, units, today=TODAY)
    assert r.ok, r.reasons
    assert (r.lease_start, r.lease_end, r.rate_kind, r.weekly_rent) == (date(2027, 3, 1), date(2028, 7, 20), "12m", 1150)
    assert r.lease_choice and r.lease_choice["earliest_start"] == "2027-02-25" and r.lease_choice["rate_kind"] == "12m"
    assert _engine(apt, units, TODAY) == _oracle(apt, units, TODAY, _people(units))


def test_leap_day_start_to_later_july_is_6m_rate():
    apt = apartment("2B2B", rent12=1300, rent6=1150, avail=date(2028, 2, 20), min_lease=6)
    units = [unit("a", move_in=date(2028, 2, 29), move_out=date(2029, 7, 5), out_flex=7, budget=575),
             unit("b", move_in=date(2028, 2, 29), move_out=date(2029, 7, 5), out_flex=7, budget=575)]
    r = check(apt, units, today=date(2028, 1, 15))
    assert r.ok and (r.lease_start, r.lease_end, r.rate_kind) == (date(2028, 2, 29), date(2029, 7, 12), "6m"), r.reasons
    assert _engine(apt, units, date(2028, 1, 15)) == _oracle(apt, units, date(2028, 1, 15), _people(units))


def _multi_year_scenario(rng):
    """Jan/Feb/Dec/Mar anchored starts with stays reaching the July (and later windows) of the FOLLOWING years."""
    year = rng.choice((2026, 2027, 2028))
    month = rng.choice((12, 1, 1, 2, 2, 3))
    ay = year + (1 if month != 12 else 0)
    day = rng.choice((1, 15, 28, 29)) if (month == 2 and ay % 4 == 0) else rng.randint(1, 28)   # leap-day starts included
    anchor = date(ay, month, day)
    if anchor < date(2026, 11, 20):
        anchor = date(2026, 12, rng.randint(1, 28))
    in_flex = rng.choice((0, 7, 14, 31))
    out_flex = rng.choice((0, 7, 14, 31, 60))
    stay = rng.choice((500, 520, 540, 560, 600, 880, 900))
    avail = anchor - timedelta(days=rng.randint(0, 25))
    r12, r6 = rng.choice(((1150, 1225), (1225, 1150), (1150, 1150), (900, 1400), (1400, 900)))
    budget = rng.choice((500, 560, 575, 600, 640, 700))
    min_lease = rng.choice((2, 6, 12))
    apt = apartment("2B2B", rent12=r12, rent6=r6, avail=avail, min_lease=min_lease)
    units = [unit("a", move_in=anchor, in_flex=in_flex, move_out=anchor + timedelta(days=stay), out_flex=out_flex, budget=budget),
             unit("b", move_in=anchor + timedelta(days=rng.randint(-3, 3)), in_flex=in_flex, move_out=anchor + timedelta(days=stay + rng.randint(-5, 5)), out_flex=out_flex, budget=budget)]
    today = min(TODAY, avail - timedelta(days=rng.randint(0, 40)))
    return apt, units, today


@pytest.mark.parametrize("seed", range(100, 106))
def test_pruned_search_equals_exhaustive_oracle_multi_year(seed):
    rng = random.Random(seed)
    feasible_seen = six_month_later_july = 0
    for _ in range(40):
        apt, units, today = _multi_year_scenario(rng)
        want, got = _oracle(apt, units, today, _people(units)), _engine(apt, units, today)
        assert got == want, (apt.available_from, apt.rent_12m, apt.rent_6m, apt.min_lease_months, [(u.move_in, u.move_in_flex, u.move_out, u.move_out_flex) for u in units], today, want, got)
        if want:
            feasible_seen += 1
            if want[2] == "6m" and want[1].year > want[0].year:
                six_month_later_july += 1
    assert feasible_seen >= 5
    assert six_month_later_july >= 1, "the multi-year scenarios must exercise a 6m-rate exit in a later July"


def test_candidate_starts_are_window_start_plus_tariff_boundaries_inside_window():
    assert rules._tariff_starts(date(2026, 12, 28), date(2027, 1, 4)) == [date(2026, 12, 28), date(2027, 1, 1)]
    assert rules._tariff_starts(date(2027, 2, 20), date(2027, 3, 6)) == [date(2027, 2, 20), date(2027, 3, 1)]
    assert rules._tariff_starts(date(2027, 3, 1), date(2027, 3, 1)) == [date(2027, 3, 1)]
    assert rules._tariff_starts(date(2026, 12, 1), date(2027, 3, 15)) == [date(2026, 12, 1), date(2027, 1, 1), date(2027, 3, 1)]


def test_a10e_exceptions_not_consulted_by_the_search():
    """A10(e) staff exceptions are a separate record (lease_dates.check_exit) and are deliberately NOT wired into the offer search:
    a blackout exit is never a candidate."""
    apt = apartment("2B2B", avail=date(2027, 1, 20))
    units = [unit("a", move_in=date(2027, 2, 1), move_out=date(2027, 8, 15), out_flex=3, budget=700), unit("b", move_in=date(2027, 2, 1), move_out=date(2027, 8, 15), out_flex=3, budget=700)]
    r = check(apt, units, today=TODAY)
    assert not r.ok and any("A10(c)" in x for x in r.reasons) and r.lease_end is None
