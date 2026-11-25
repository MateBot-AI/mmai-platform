"""I19(p)/GP6(d): phone-copy prose must not promise exactly three apartments."""
from pathlib import Path


def test_phone_copy_describes_suitable_shortlist_without_fixed_count():
    script = (Path(__file__).resolve().parents[1] / "client" / "form.js").read_text()
    copy = script.split("function emailBody(saved, groups) {", 1)[1].split("async function prepareEmail()", 1)[0]
    assert "suggest suitable apartments for your group" in copy
    assert "arrange viewings of the shortlisted options" in copy
    assert "find three apartments" not in copy
    assert "groups.forEach" in copy  # supplied customer answer rows remain included
    page = (Path(__file__).resolve().parents[1] / "client" / "index.html").read_text()
    assert 'form.js?v=20261125a' in page
