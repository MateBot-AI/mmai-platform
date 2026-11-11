"""A10 exact candidates compete with ordinary leases; prices never add A5 twice."""
from datetime import date
from decimal import Decimal

from server import exception_rent as er, lease_dates, rules
from tests.conftest import apartment, unit

START = date(2027, 2, 1)
END = date(2027, 8, 15)
STAMP = "2026-11-11T00:00:00+00:00"


def _exception(total="1200", *, start=START, end=END, ref="exact-booking", agreed=False):
    value = Decimal(total)
    staff = lease_dates.StaffExitException(ref, end, value, "tester", STAMP)
    agreement = er.CustomerAgreement(ref, value, "tester", STAMP) if agreed else None
    return er.ExceptionOffer(1, ref, start, end, value, staff, agreement, STAMP)


def _units(n=2, budget=600, out_flex=30):
    return [unit(str(i), move_in=START, in_flex=0, move_out=END, out_flex=out_flex,
                 budget=budget, arrangements=("Own room", "Share with one other", "Share with two others"))
            for i in range(n)]


def test_longer_authorised_exact_candidate_beats_feasible_ordinary_offer():
    apt, units = apartment(rent12=1000, rent6=1050, avail=START), _units()
    ordinary = rules.check_group(apt, units, today=START)
    assert ordinary.ok and ordinary.lease_end == date(2027, 7, 20)
    revised = rules.check_group(apt, units, today=START, exception_offers=(_exception(),))
    assert revised.ok and revised.lease_end == END
    assert revised.exception_ref == "exact-booking" and revised.exception_total == "1200"
    assert revised.total_weekly == 1200
    assert revised.weekly_rent == 1000  # inventory tariff remains inventory tariff
    assert revised.household_budget["total_weekly_rent_exact"] == "1200"


def test_exception_only_group_and_each_record_considered():
    apt, units = apartment(rent12=1000, avail=START), _units(out_flex=0)
    assert not rules.check_group(apt, units, today=START).ok
    unusable = _exception("999", ref="not-an-increase")
    valid = _exception(ref="usable")
    chk = rules.check_group(apt, units, today=START, exception_offers=(unusable, valid))
    assert chk.ok and chk.exception_ref == "usable"


def test_a5_is_included_once_and_room_costs_allocate_actual_total():
    apt = apartment("2B1B", rent12=1000, avail=START,
                    bedrooms=["1 king or 2 singles or 3 singles", "1 king or 2 singles"])
    chk = rules.check_group(apt, _units(5, out_flex=0), today=START, exception_offers=(_exception(),))
    assert chk.ok
    assert (chk.extra_bed, chk.total_weekly, chk.exception_total) == (70, 1200, "1200")
    assert sum(r["room_cost"] for r in chk.allocation) == 1200
    assert sum(r["extra_bed"] for r in chk.allocation) == 70
    assert chk.household_budget["total_weekly_rent_exact"] == "1200"


def test_above_allowance_requires_exact_customer_agreement():
    apt, units = apartment(rent12=1000, avail=START), _units(budget=500, out_flex=0)
    assert not rules.check_group(apt, units, today=START, exception_offers=(_exception(),)).ok
    chk = rules.check_group(apt, units, today=START, exception_offers=(_exception(agreed=True),))
    assert chk.ok and chk.total_weekly == 1200


def test_exception_does_not_expand_customer_dates_or_bypass_bedding():
    apt, units = apartment(rent12=1000, avail=START), _units(out_flex=0)
    outside = _exception(end=date(2027, 9, 1))
    assert not rules.check_group(apt, units, today=START, exception_offers=(outside,)).ok
    units[0].people[0].arrangements = set()
    assert not rules.check_group(apt, units, today=START, exception_offers=(_exception(),)).ok


def test_exception_does_not_bypass_minimum_vacancy_or_declaration():
    units = _units(out_flex=0)
    too_long = apartment(rent12=1000, avail=START, min_lease=12)
    assert not rules.check_group(too_long, units, today=START, exception_offers=(_exception(),)).ok
    empty = apartment(rent12=1000, avail=date(2027, 1, 1))
    assert not rules.check_group(empty, units, today=date(2027, 1, 1), exception_offers=(_exception(),)).ok
    apt = apartment(rent12=1000, avail=START)
    units[0].adults_confirmed_move_in = None
    assert not rules.check_group(apt, units, today=START, exception_offers=(_exception(),)).ok
