"""Apartment and building features (approved by Bill, 8 November 2026): the ONE catalogue shared by the MateBot form,
the HomeBot form, the staff apartment screen, the inventory importer and the workbook export.

Exactly these 20 features exist, in four groups. Nothing is added here without a ruling. For an apartment every feature is
recorded as unknown (NULL, the default for every apartment until someone enters it), yes (1) or no (0): "unknown" is never
shown or exported as "no". The car-parking charge is free text copied from the inventory sheet or typed by staff; it is never
derived from a "yes".

Enquirers may tick any number of wanted features (optional) and may then say which up to three matter most to them. These
are wishes recorded for staff and for the people themselves: there are no hard exclusions, no rigid ranking and no numeric
weights attached to them here.
"""
from __future__ import annotations

GROUPS = ["Apartment", "Outdoor space", "Building facilities", "Parking/work facilities"]

# (key, label shown to people and used as the workbook column heading, group)
FEATURES: list[tuple[str, str, str]] = [
    ("higher_floor", "Higher floor", "Apartment"),
    ("lower_floor", "Lower floor", "Apartment"),
    ("views", "Views", "Apartment"),
    ("air_conditioning", "Air conditioning", "Apartment"),
    ("dishwasher", "Dishwasher", "Apartment"),
    ("ensuite", "Ensuite", "Apartment"),
    ("balcony", "Balcony", "Outdoor space"),
    ("courtyard", "Courtyard", "Outdoor space"),
    ("pool", "Pool", "Building facilities"),
    ("gym", "Gym", "Building facilities"),
    ("sauna", "Sauna", "Building facilities"),
    ("theatre_room", "Theatre room", "Building facilities"),
    ("residents_lounge", "Residents lounge", "Building facilities"),
    ("bbq", "BBQ facilities", "Building facilities"),
    ("tennis_court", "Tennis court", "Building facilities"),
    ("concierge", "Concierge", "Building facilities"),
    ("lift_access", "Lift access", "Building facilities"),
    ("car_parking", "Car parking available (extra charge)", "Parking/work facilities"),
    ("bicycle_storage", "Bicycle storage", "Parking/work facilities"),
    ("study_desk", "Study desk", "Parking/work facilities"),
]
assert len(FEATURES) == 20 and len({k for k, _, _ in FEATURES}) == 20 and len({l for _, l, _ in FEATURES}) == 20

KEYS = [k for k, _, _ in FEATURES]
LABELS = [l for _, l, _ in FEATURES]
LABEL_OF = {k: l for k, l, _ in FEATURES}
KEY_OF = {l: k for k, l, _ in FEATURES}
GROUP_OF = {k: g for k, _, g in FEATURES}
COLUMNS = [f"feat_{k}" for k in KEYS]           # apartments.feat_<key>: NULL unknown, 1 yes, 0 no
PARKING_CHARGE_HEADING = "Car parking charge"   # workbook column; free text, blank = not stated
MAX_TOP = 3
MAX_PARKING_CHARGE = 80

STATES = ("unknown", "yes", "no")


def catalogue() -> list[dict]:
    """For /api/meta and the public metas: the list in display order with its group."""
    return [{"key": k, "label": l, "group": g} for k, l, g in FEATURES]


def grouped() -> list[dict]:
    return [{"group": g, "features": [{"key": k, "label": l} for k, l, gg in FEATURES if gg == g]} for g in GROUPS]


def state_of(v) -> str:
    """Database value -> 'unknown' | 'yes' | 'no'. Anything that is not exactly 0/1 is unknown (never guessed)."""
    if v in (1, True):
        return "yes"
    if v in (0, False):
        return "no"
    return "unknown"


def value_of(state) -> int | None:
    """API value -> database value. Accepts true/false/null or the words yes/no/unknown (the staff UI sends the words);
    numbers, lists and any other text raise ValueError so nothing is ever coerced."""
    if state is None:
        return None
    if state is True:
        return 1
    if state is False:
        return 0
    if isinstance(state, str):
        s = state.strip().lower()
        if s in ("unknown", "", "null"):
            return None
        if s == "yes":
            return 1
        if s == "no":
            return 0
    raise ValueError("Use yes, no or unknown.")


def cell_to_value(cell) -> int | None:
    """Workbook cell -> database value. Strict agreed tokens only: Yes/Y/1/True -> 1, No/N/0/False -> 0, blank -> None.
    Anything else (ticks, crosses, "x", "maybe", 2 ...) also returns None; the importer reports those cells as errors and stores
    nothing, so a mark whose meaning is ambiguous is never read as yes or no."""
    if cell is None:
        return None
    if cell is True:
        return 1
    if cell is False:
        return 0
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return 1 if cell == 1 else 0 if cell == 0 else None
    s = str(cell).strip().lower()
    if s in ("yes", "y", "1", "true"):
        return 1
    if s in ("no", "n", "0", "false"):
        return 0
    return None


def value_to_cell(v) -> str:
    return {"yes": "Yes", "no": "No"}.get(state_of(v), "")


def apartment_features(a: dict) -> dict:
    """{key: 'unknown'|'yes'|'no'} for an apartment row (missing columns count as unknown)."""
    return {k: state_of(a.get(f"feat_{k}")) for k in KEYS}


def clean_wanted(values, *, top=None, errors: dict | None = None, key: str = "features", top_key: str = "features_top") -> tuple[list, list]:
    """Validate an enquirer's wanted features and the up-to-three that matter most.

    Both are optional. Unknown labels are an error (never silently dropped, so a mistyped client cannot pass junk); duplicates
    are collapsed; the "matter most" list must be a subset of the wanted list and hold at most MAX_TOP items.
    Returns (features, features_top) as lists of labels in catalogue order."""
    errors = errors if errors is not None else {}
    if values in (None, ""):
        values = []
    if top in (None, ""):
        top = []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        errors[key] = "Please tick features from the list."
        values = []
    if not isinstance(top, list) or not all(isinstance(v, str) for v in top):
        errors[top_key] = "Please choose from the features you ticked."
        top = []
    bad = [v for v in values if v not in KEY_OF]
    if bad:
        errors[key] = "Please tick features from the list."
    wanted = [l for l in LABELS if l in values]
    bad_top = [v for v in top if v not in wanted]
    if bad_top:
        errors[top_key] = "Please choose from the features you ticked."
    most = [l for l in LABELS if l in top and l in wanted]
    if len(most) > MAX_TOP:
        errors[top_key] = f"Please choose up to {MAX_TOP}."
    return wanted, most


def compare(wanted: list, top: list, apartment: dict) -> dict:
    """Pure facts for an internal briefing: for each feature an enquirer (or group) asked for, is it recorded as available
    ('available'), recorded as absent ('unavailable') or not yet entered ('unverified') at this apartment?

    No weights, no score, no exclusion: this only lines up stated wishes against recorded facts so people can read them.
    `wanted` and `top` are lists of feature labels (as stored on an enquiry); unknown labels are ignored. Each item carries
    `matters_most` so the up-to-three can be shown first without re-ordering the catalogue."""
    states = apartment_features(apartment)
    word = {"yes": "available", "no": "unavailable", "unknown": "unverified"}
    items = []
    for label in LABELS:
        if label not in wanted and label not in top:
            continue
        key = KEY_OF[label]
        items.append({"key": key, "label": label, "group": GROUP_OF[key], "matters_most": label in top, "status": word[states[key]]})
    counts = {"available": 0, "unavailable": 0, "unverified": 0}
    for it in items:
        counts[it["status"]] += 1
    return {"items": items, "counts": counts, "parking_charge": (apartment.get("parking_charge") or None)}
