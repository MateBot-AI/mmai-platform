"""8 November 2026 hardening: the public forms' agreements are affirmed by JSON true ONLY (as the 6.4 adults declaration
already was). A non-browser caller sending the strings "false" / "no" / "0" - or "true" / "yes" / 1 - never records an
agreement, and the optional marketing opt-in is stored only for an actual true. No new question or consent step; the shipped
forms send real booleans, so their behaviour is unchanged. Covers MateBot (consent_store / consent_share / consent_marketing)
and HomeBot (consent_contact / consent_lease / consent_adults)."""
import pytest
from fastapi.testclient import TestClient

from server.db import row
from tests.test_data_and_api import client, good_answers  # noqa: F401  (fixtures)
from tests import test_homebot as hb
from tests.test_homebot import client as hb_client  # noqa: F401  (fixture: HomeBot anon client + db)

NOT_AGREEMENT = [False, "false", "no", "0", "true", "yes", "on", 1, "1", None, 0, ""]


@pytest.mark.parametrize("field", ["consent_store", "consent_share"])
@pytest.mark.parametrize("value", NOT_AGREEMENT)
def test_matebot_required_agreements_accept_only_json_true(client, field, value):
    anon = TestClient(client.app)
    r = anon.post("/api/public/enquiries", json=good_answers(**{field: value}))
    assert r.status_code == 422, (field, value, r.text)
    assert r.json()["errors"][field] == "This agreement is required."
    import server.app as appmod
    assert appmod.db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 0


def test_matebot_true_is_accepted_and_marketing_strings_never_opt_in(client):
    anon = TestClient(client.app)
    import server.app as appmod
    for i, mk in enumerate(["yes", "true", "on", "1", 1, "false", False, None]):
        r = anon.post("/api/public/enquiries", json=good_answers(email=f"m{i}@example.com", consent_store=True, consent_share=True, consent_marketing=mk))
        assert r.status_code == 201, (mk, r.text)
        e = row(appmod.db.execute("SELECT consent_store, consent_share, consent_marketing FROM enquiries WHERE id=?", (r.json()["id"],)))
        assert (e["consent_store"], e["consent_share"], e["consent_marketing"]) == (1, 1, 0), (mk, e)
    r = anon.post("/api/public/enquiries", json=good_answers(email="opt@example.com", consent_marketing=True))
    assert r.status_code == 201
    assert row(appmod.db.execute("SELECT consent_marketing FROM enquiries WHERE id=?", (r.json()["id"],)))["consent_marketing"] == 1


@pytest.mark.parametrize("field", ["consent_contact", "consent_lease", "consent_adults"])
@pytest.mark.parametrize("value", NOT_AGREEMENT)
def test_homebot_tick_boxes_accept_only_json_true(hb_client, field, value):
    c = hb_client
    r = hb.post(c, hb.body(**{field: value}))
    assert r.status_code == 422, (field, value, r.text)
    assert field in r.json()["errors"]
    assert c.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 0
