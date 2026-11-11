"""Independent ownership-confirmation boundary checks, no provider calls."""
import pytest

from server import enquiries, matching, ai_queue
from server.db import row, retention_guard
from tests.test_takeover_source_integrity import _took_over
from tests.test_t6_grounding import _party_group


@pytest.mark.parametrize("value", ["false", "0", "yes", 1, {"confirmed": True}])
def test_non_boolean_confirmation_cannot_release_owner_flag(db, monkeypatch, value):
    lead, _, _, _ = _took_over(db, monkeypatch)
    try:
        enquiries.update_enquiry(db, lead["id"], {"lead_answers_confirmed": value}, "staff")
    except enquiries.ValidationError:
        pass
    e = row(db.execute("SELECT lead_answers_unverified_at,notes_status FROM enquiries WHERE id=?",
                       (lead["id"],)))
    assert e["lead_answers_unverified_at"]
    assert e["notes_status"] == "unverified"


def test_missing_promoted_country_does_not_inherit_former_lead_country(db, monkeypatch):
    _, lead, _, group = _party_group(db, monkeypatch)
    before_consent = row(db.execute("SELECT consent_share FROM enquiries WHERE id=?",
                                   (lead["id"],)))["consent_share"]
    with retention_guard(db, label="independent absent companion country"):
        db.execute("UPDATE enquiries SET birth_country='Sweden' WHERE id=?", (lead["id"],))
        db.execute("""UPDATE party_members SET birth_country=NULL,birth_country_raw=NULL
                      WHERE enquiry_id=? AND name='Bo Berg'""", (lead["id"],))
    original = next(p for p in ai_queue.intro_members(db, group["id"])
                    if p["is_lead"] and p["enquiry_id"] == lead["id"])
    assert original["country_of_birth"] == "Sweden"
    matching.bar_enquiry(db, lead["id"], "staff", "test", person="lead", party_choice="stay")
    person = next(p for p in ai_queue.intro_members(db, group["id"])
                  if p["is_lead"] and p["enquiry_id"] == lead["id"])
    assert not person["country_of_birth"], "The former lead's country is not the promoted companion's answer"
    assert row(db.execute("SELECT consent_share FROM enquiries WHERE id=?",
                          (lead["id"],)))["consent_share"] == before_consent
