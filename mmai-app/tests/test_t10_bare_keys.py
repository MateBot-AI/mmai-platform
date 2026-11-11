"""T10: a bare machine key the AI copied into its prose never reaches staff as a key.

Overlay L (8 Nov 2026), from two live outputs on the C2/J/K build: "limit 1348.6 at allowance_fraction 0.1" and "recorded bunk_ok
of No". staff_view.plain_text already spelled out qualified ids (U1.bunk_ok, A12.total_weekly, R.A12.budget.limit_exact); L adds
the bare known keys to the SAME single-pass substitution. Exact lowercase whole tokens only, drawn from the underscore-bearing
FACT_LABELS keys; the value beside the key is left exactly as written; inserted names and addresses are never rescanned.
"""
import re

import pytest

from server import staff_view
from server.staff_view import FACT_LABELS, MACHINE_KEYS, plain_text

LIVE = [
    ("Total rent 850 ... limit 1348.6 at allowance_fraction 0.1",
     "Total rent 850 ... limit 1348.6 at allowed rent above the household maximum (as a fraction, 0.1 = one tenth) 0.1"),
    ("recorded bunk_ok of No", "recorded bunk bed accepted of No"),
]


def test_machine_keys_are_exactly_the_underscore_labels():
    assert set(MACHINE_KEYS) == {k for k in FACT_LABELS if "_" in k}
    assert all(k == k.lower() for k in MACHINE_KEYS)
    for plain in ("routine", "social", "diet", "budget", "smoke", "dates", "beds", "party", "course"):
        assert plain not in MACHINE_KEYS                 # ordinary English words are never substituted
    assert list(MACHINE_KEYS) == sorted(MACHINE_KEYS, key=len, reverse=True)


@pytest.mark.parametrize("text, expected", LIVE)
def test_exact_live_strings(text, expected):
    assert plain_text(text, {}, {}) == expected


@pytest.mark.parametrize("key", MACHINE_KEYS)
def test_every_known_key_is_spelled_out_and_its_value_untouched(key):
    label = FACT_LABELS[key]
    for template, tail in ((f"Recorded {key}=0.1", "=0.1"), (f"{key} is 1348.6", " is 1348.6"), (f"see ({key}).", ")."),
                           (f"{key}, then", ", then"), (f"the {key}: No", ": No"), (f"{key}", "")):
        out = plain_text(template, {}, {})
        assert key not in out and label in out and out.endswith(tail), (key, template, out)
        assert "_" not in out, (key, out)
        for n in re.findall(r"\d+(?:\.\d+)?", template):   # no numeric conversion of the displayed value
            assert n in out, (n, out)


@pytest.mark.parametrize("text", [
    "prefix_allowance_fraction and bunk_ok_suffix remain literal.",
    "Bunk_ok BUNK_OK bunk_okay bunk_ok2 xbunk_ok my-bunk_ok bunk_ok-ish",   # case or longer tokens: not the machine key
    "Their routine, social style, diet and budget line up; smoke and dates were discussed.",
    "Plain sentence with no tokens at all.",
])
def test_lowercase_whole_token_scope_only(text):
    assert plain_text(text, {}, {}) == text


def test_inserted_person_name_and_address_are_never_rescanned():
    assert plain_text("U1 at A12", {"U1": "allowance_fraction"}, {"A12": "12 bunk_ok Lane"}) == "allowance_fraction at 12 bunk_ok Lane"
    assert plain_text("U1.bunk_ok=Yes near A12", {"U1": "mate_max Smith"}, {"A12": "limit_exact Lane"}) == \
        "mate_max Smith's bunk bed accepted=Yes near limit_exact Lane"


def test_qualified_ids_keep_their_existing_branches():
    names, apts = {"U1": "Ana"}, {"A12": "12 Synthetic Street"}
    out = plain_text("R.A12.budget.allowance_fraction is 0.1", {}, apts)
    assert "allowance_fraction" not in out and "budget check" in out and "12 Synthetic Street" in out and out.endswith("is 0.1")
    assert plain_text("U1.bunk_ok", names, apts) == "Ana's bunk bed accepted"
    assert plain_text("U1-m1.smoke_ok", {"U1": "Ana", "U1-m1": "Ben"}, apts) == "Ben's okay living with a smoker"
    assert plain_text("A12.total_weekly", names, apts) == "12 Synthetic Street: whole-apartment weekly rent"
    assert plain_text("R.A12.budget fails", names, apts) == "budget check at 12 Synthetic Street fails"
    assert plain_text("under A16", names, apts) == "under the total-rent allowance"      # rule codes unchanged by L


def test_evidence_lines_and_notes_share_the_same_pass():
    # describe_fact (evidence bits) was already plain; the note path goes through plain_text and now spells bare keys too
    assert staff_view.describe_fact("U1.bunk_ok", "Yes", {"U1": "Ana"}, {}) == "Ana's bunk bed accepted: Yes"
    assert plain_text("Ana recorded bunk_ok of No while U1.mate_max=4", {"U1": "Ana"}, {}) == \
        "Ana recorded bunk bed accepted of No while Ana's maximum number of other housemates accepted (not counting themselves)=4"
