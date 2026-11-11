from pathlib import Path

from server.enquiries import QUESTION_TYPES


def test_information_fields_are_not_matching_preferences():
    for key in ("1.8", "1.9", "1.10"):
        assert QUESTION_TYPES[key] == "Information only (not used for matching)"


def test_staff_copy_does_not_claim_fallback_or_connection_health():
    client = Path(__file__).parents[1] / "client"
    text = (client / "staff.js").read_text()
    for stale in ("AI unavailable: rules-only ranking", "AI matchmaking: on",
                  "they are never matched automatically", "No LLM credentials"):
        assert stale not in text
    assert "new groups wait for AI review" in text
    assert "Configuration alone does not confirm a successful AI connection" in text
    assert "staff.js?v=20261111h" in (client / "index.html").read_text()
