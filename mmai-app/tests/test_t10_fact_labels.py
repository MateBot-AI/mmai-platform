"""T10: every fact id the review packet can cite is rendered to staff as plain words, never as a raw field id or a rule code.

Overlay J (8 Nov 2026). The packet catalogue lives in server.facts (every module attribute named *_FIELDS that is a tuple of
field names); the staff wording lives in server.staff_view.FACT_LABELS. The two must stay in step: a field that the queue can
put in front of the AI must have an explicit label here (or be a feat_<feature> id, which is spelled out from the feature name).
"""
import re

from server import facts, staff_view
from server.staff_view import FACT_LABELS, _label, describe_fact

# A register-style code: a section letter group followed by digits (A16, GP8, M3, I16, T10). Labels must never contain one.
RULE_CODE = re.compile(r"(?<![A-Za-z])(?:GP|[ABCHIMPT])\d+[a-z]?(?![A-Za-z])")

# Labels agreed with the queue owner for the stage C packet (exact ids and wording). A change here is a new overlay.
STAGE_C_LABELS = {
    "room_first": "first-choice room arrangement",
    "rooms_accepted": "room arrangements accepted",
    "apt_sizes_accepted": "apartment sizes accepted",
    "bunk_ok": "bunk bed accepted",
    "mate_max": "maximum number of other housemates accepted (not counting themselves)",
    "smoke": "smokes",
    "smoke_ok": "okay living with a smoker",
    "total_weekly_rent_exact": "whole-apartment weekly rent including the extra bed",
    "whole_apartment_max_exact": "household's combined weekly budget maximum",
    "allowance_fraction": "allowed rent above the household maximum (as a fraction, 0.1 = one tenth)",
    "limit_exact": "exact weekly rent limit for the household",
}


def catalogue_fields() -> set[str]:
    out: set[str] = set()
    for name in dir(facts):
        if not name.endswith("_FIELDS"):
            continue
        val = getattr(facts, name)
        if isinstance(val, (tuple, list)) and val and all(isinstance(f, str) for f in val):
            out.update(val)
    assert out, "facts catalogue not found"
    return out


def test_every_catalogue_field_has_an_explicit_label():
    unlabelled = sorted(f for f in catalogue_fields() if f not in FACT_LABELS and not f.startswith("feat_"))
    assert unlabelled == [], f"fields rendered from their raw id: {unlabelled}"


def test_stage_c_labels_present_verbatim():
    for fid, label in STAGE_C_LABELS.items():
        assert FACT_LABELS.get(fid) == label, fid


def test_labels_are_plain_words_without_ids_or_rule_codes():
    """T10 forbids register/internal codes and raw field keys, not meaningful numbers or units (0.1, 18+, km)."""
    for fid, label in FACT_LABELS.items():
        assert label and label == label.strip(), fid
        assert not label[0].isupper(), fid                    # rendered mid-sentence ("Ana's <label>"); proper nouns may follow
        assert "_" not in label, fid                          # a raw field id would carry underscores
        assert not RULE_CODE.search(label), (fid, label)
        assert label.count("(") == label.count(")"), (fid, label)


def test_label_fallback_never_reaches_staff_for_catalogue_fields():
    # _label's fallback (underscores -> spaces) exists for feature ids only; a catalogue field must resolve through FACT_LABELS
    for f in catalogue_fields():
        if f.startswith("feat_"):
            continue
        assert _label(f) == FACT_LABELS[f], f
    assert _label("feat_air_conditioning") == "air conditioning"


def test_describe_fact_renders_new_ids_as_name_label_value():
    names = {"U1": "Ana", "U2": "Ben"}
    apts = {"A12": "12 Smith St"}
    assert describe_fact("U1.bunk_ok", "Yes", names, apts) == "Ana's bunk bed accepted: Yes"
    assert describe_fact("U2.smoke_ok", "No", names, apts) == "Ben's okay living with a smoker: No"
    assert describe_fact("U1.mate_max", 4, names, apts, source="U2") == "Ana's maximum number of other housemates accepted (not counting themselves): 4 (answered by Ben for the whole party)"
    assert describe_fact("A12.total_weekly_rent_exact", "812.50", names, apts) == "12 Smith St: whole-apartment weekly rent including the extra bed: 812.50"
    assert describe_fact("R.A12.budget", "pass", names, apts) == "Budget check at 12 Smith St: pass"
    assert describe_fact("R.A12.budget.limit_exact", "830", names, apts) == "Exact weekly rent limit for the household (budget check) at 12 Smith St: 830"
    assert describe_fact("R.A12.budget.allowance_fraction", "0.1", names, apts) == (
        "Allowed rent above the household maximum (as a fraction, 0.1 = one tenth) (budget check) at 12 Smith St: 0.1")
    assert describe_fact("A12.stay_days", 159, names, apts) == "12 Smith St: length of stay (days): 159"
    assert describe_fact("U1.allowance_fraction", None, names, apts, missing=True) == (
        "Ana's allowed rent above the household maximum (as a fraction, 0.1 = one tenth) (not recorded)")
    for s in ("bunk_ok", "limit_exact", "U1.", "A12.", "_"):
        assert s not in describe_fact("U1.bunk_ok", "Yes", names, apts)


def test_fact_bit_regex_accepts_new_ids():
    for bit in ("U1.bunk_ok=Yes", "A12.total_weekly_rent_exact=812.50", "R.A12.budget.limit_exact=830.25",
                "U1-m1.smoke_ok=No (reported by U1 for the party)", "R.A12.budget.allowance_fraction: not recorded"):
        assert staff_view._BIT.match(bit), bit


def test_prose_substitution_spells_out_budget_detail_ids():
    names = {"U1": "Ana"}
    apts = {"A12": "12 Smith St"}
    out = staff_view.plain_text("R.A12.budget.limit_exact is 830 while U1.mate_max is 4 (A16).", names, apts)
    assert out == "exact weekly rent limit for the household (budget check) at 12 Smith St is 830 while Ana's maximum number of other housemates accepted (not counting themselves) is 4 (the total-rent allowance)."
    assert staff_view.plain_text("R.A12.budget fails", names, apts) == "budget check at 12 Smith St fails"
