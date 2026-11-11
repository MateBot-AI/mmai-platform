"""10 Nov 2026 (HC-v3 reviewer finding, narrow input-validation defect): the smoking answers `smoke` / `smoke_ok` are enum
strings from the form. Intake and the staff/customer update path used to accept a JSON boolean (true) for them; the stored
value then read as "does not smoke" / "not ok with a smoker" in matching.unit_from_row - a silent reinterpretation of a
malformed answer. Now a malformed type or value is refused with a field error; the form's own strings are unchanged and
no stored answer is rewritten. No new matching filter, no rule change."""
import pytest

from server import matching
from server.db import row
from server.enquiries import ValidationError, SMOKE_OPTIONS, SMOKE_OK_OPTIONS, create_enquiry, update_enquiry, validate
from tests.test_data_and_api import good_answers


def test_form_strings_pass_and_booleans_or_other_values_are_refused():
    for smoke in ("No", "Yes, outside only (balcony or courtyard)"):
        for ok in ("Yes", "No"):
            assert "smoke" not in validate(good_answers(smoke=smoke, smoke_ok=ok)) and "smoke_ok" not in validate(good_answers(smoke=smoke, smoke_ok=ok))
    for bad in (True, False, 1, 0, "true", "yes", "Sometimes", ["No"], {"v": "No"}):
        err = validate(good_answers(smoke=bad))
        assert "smoke" in err, bad
    for bad in (True, False, 1, "true", "YES", "ok", ["Yes"]):
        err = validate(good_answers(smoke_ok=bad))
        assert "smoke_ok" in err, bad
    assert set(SMOKE_OPTIONS) >= set(matching.SMOKE_OPTIONS) and set(SMOKE_OK_OPTIONS) == set(matching.SMOKE_OK_OPTIONS)


def test_create_and_update_refuse_booleans_and_keep_strings(db):
    with pytest.raises(ValidationError) as ex:
        create_enquiry(db, good_answers(smoke=True, smoke_ok=True), "Landing page", use_ai_countries=False, is_test=True)
    assert {"smoke", "smoke_ok"} <= set(ex.value.errors)
    e = create_enquiry(db, good_answers(smoke="Yes, outside only (balcony or courtyard)", smoke_ok="Yes"), "Landing page", use_ai_countries=False, is_test=True)
    u = matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (e["id"],))), [])
    assert u.people[0].smokes is True and u.people[0].smoke_ok is True
    for patch in ({"smoke": True}, {"smoke_ok": True}, {"smoke": "Sometimes"}, {"smoke_ok": 1}):
        with pytest.raises(ValidationError):
            update_enquiry(db, e["id"], patch, "tester")
    r = db.execute("SELECT smoke, smoke_ok FROM enquiries WHERE id=?", (e["id"],)).fetchone()
    assert (r["smoke"], r["smoke_ok"]) == ("Yes, outside only (balcony or courtyard)", "Yes")   # nothing rewritten by the refused patches
    update_enquiry(db, e["id"], {"smoke": "No", "smoke_ok": "No"}, "tester")
    r = db.execute("SELECT smoke, smoke_ok FROM enquiries WHERE id=?", (e["id"],)).fetchone()
    assert (r["smoke"], r["smoke_ok"]) == ("No", "No")
