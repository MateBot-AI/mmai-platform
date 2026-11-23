from pathlib import Path

from server.enquiries import QUESTION_TYPES


def test_question_classification_labels():
    assert QUESTION_TYPES["1.8"] == QUESTION_TYPES["1.9"] == "Information"
    assert QUESTION_TYPES["5.1"] == "Must-match" and QUESTION_TYPES["5.2"] == "Preference" and QUESTION_TYPES["5.4"] == "Preference"
    assert "5.3" not in QUESTION_TYPES   # replaced by the culture question (23 Nov 2026)
    assert not any("not used for matching" in v for v in QUESTION_TYPES.values())


def test_staff_copy_does_not_claim_fallback_or_connection_health():
    client = Path(__file__).parents[1] / "client"
    text = (client / "staff.js").read_text()
    for stale in ("AI unavailable: rules-only ranking", "AI matchmaking: on",
                  "they are never matched automatically", "No LLM credentials"):
        assert stale not in text
    assert "new groups wait for AI review" in text
    assert "Configuration alone does not confirm a successful AI connection" in text
    assert "staff.js?v=20261123a" in (client / "index.html").read_text()
