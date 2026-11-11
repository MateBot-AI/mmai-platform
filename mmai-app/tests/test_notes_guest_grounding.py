"""Guest facts must establish overnight stays and the reported frequency."""
import pytest
from server import notes_facts


def assessed(text):
    return notes_facts.validate_reply({"items": [{
        "category": "overnight_guests", "polarity": "affirmed",
        "strength": "stated", "subject": "self", "field": "else_text",
        "span": text, "start": 0, "end": len(text),
        "value": {"frequency": "frequent"},
    }], "residual": False}, {"else_text": text})


@pytest.mark.parametrize("text", [
    "My partner stays over 0 nights a week.",
    "My partner stays over 1 night a week.",
    "I often have guests over for lunch.",
    "I often have guests.",
    "I have visitors weekly.",
])
def test_frequency_and_topic_words_do_not_establish_frequent_overnight_stays(text):
    assert not assessed(text)["packet_items"]


@pytest.mark.parametrize("text", [
    "My partner stays over three or four nights a week.",
    "I often have guests overnight.",
])
def test_explicit_frequent_overnight_stays_remain_supported(text):
    assert assessed(text)["packet_items"]
