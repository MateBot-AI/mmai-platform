"""Honest nominal timetable-fit uncertainty is not a positive schedule claim."""
import pytest

from server import facts


@pytest.mark.parametrize("note", [
    "U3 and U4 have no recorded school or course, so their campus distance and study-timetable fit with U1 and U2 are unknown.",
    "Their timetable fit with U1 is not recorded.",
    "Timetable fit between U1 and U2 remains unconfirmed.",
])
def test_bound_nominal_fit_unknown_is_allowed(note):
    assert facts.timetable_inference(note, routine_cited=False) is None


@pytest.mark.parametrize("note", [
    "Their timetables fit well.",
    "Timetable fit with U1 is likely good.",
    "Timetable fit is unknown, though likely fine.",
    "Timetable fit is unknown but probably good.",
    "Their timetables fit despite an unknown campus.",
    "Their timetable fit is not disputed.",
    "Their timetable fit with U1 is undeniable.",
    "Timetable fit with U1 is unknown; their class times align.",
    "Their class times align. Timetable fit with U1 is unknown.",
    "Their timetables fit well, while timetable fit with U1 is unknown.",
])
def test_uncertainty_does_not_license_positive_schedule_claims(note):
    assert facts.timetable_inference(note, routine_cited=False) is not None
