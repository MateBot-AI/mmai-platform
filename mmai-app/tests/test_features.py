"""Apartment and building features (Bill, 8 Nov 2026): one shared catalogue of 20 features behind the MateBot question 3.11/3.12,
the HomeBot "nice to have" list, the staff apartment editor, the spreadsheet import/export and the pure facts comparison.
Also: the legacy own-country / avoid-country questions (5.3/5.4) are retired everywhere.

Principles under test
- Wishes only: features are never a filter, a ranking or a weight; "matter most" is at most three of the ticked features.
- Unknown is never no: an apartment feature is unknown until staff record yes or no; blank cells never overwrite; unreadable
  cells are reported and leave the value unchanged; staff values survive a baseline refresh unless overwrite is explicit.
- Nothing invented: the supplied workbook has no feature columns, so every apartment starts unknown; parking charge is text copied
  as stated.
"""
import io
import json
import os
import re
import shutil

import openpyxl
import pytest
from fastapi.testclient import TestClient

from server import apartment_features as feat
from server import enquiries, homebot as hb, importer, receipts
from server.db import row
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)
from tests.test_homebot import body as hb_body  # noqa: F401

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(HERE, "data", "ALM_Apartments_2026-10-23.xlsx")
TEMPLATE = os.path.join(HERE, "data", "ALM_Apartments_2026-10-23_features_template.xlsx")
_n = [0]


def hdr():
    """Each public submission needs its own key (a repeated key is a duplicate submission, 409)."""
    _n[0] += 1
    return {"X-Submission-Key": f"k_feat_{_n[0]:04d}" + "x" * 20}

APPROVED = ["Higher floor", "Lower floor", "Views", "Balcony", "Courtyard", "Study desk", "Air conditioning", "Dishwasher", "Ensuite",
            "Pool", "Gym", "Sauna", "Theatre room", "Residents lounge", "BBQ facilities", "Tennis court", "Concierge",
            "Car parking available (extra charge)", "Bicycle storage", "Lift access"]


def staff(c):
    r = c.post("/api/auth/login", json={"username": "bill", "password": os.environ["MMAI_INITIAL_PASSWORD_BILL"]})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------------- catalogue ----------------

def test_catalogue_is_exactly_the_approved_twenty():
    assert sorted(feat.LABELS) == sorted(APPROVED) and len(feat.KEYS) == 20 == len(set(feat.KEYS))
    assert set(feat.GROUP_OF.values()) == set(feat.GROUPS) == {"Apartment", "Outdoor space", "Building facilities", "Parking/work facilities"}
    assert feat.MAX_TOP == 3 and all(re.fullmatch(r"[a-z_]+", k) for k in feat.KEYS)
    assert hb.EXTRAS == feat.LABELS and hb.EXTRAS_MAX_TOP == 3


def test_both_public_forms_offer_the_same_list_as_the_server():
    def inputs(path, name):
        html = open(path, encoding="utf-8").read()
        return re.findall(rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"', html)
    assert inputs(os.path.join(HERE, "client", "index.html"), "features") == feat.LABELS
    assert inputs(os.path.join(HERE, "..", "homebot-app", "index.html"), "extras") == feat.LABELS
    html = open(os.path.join(HERE, "client", "index.html"), encoding="utf-8").read()
    assert "Which apartment and building features would you particularly like?" in html and "Which three matter most to you?" in html
    # retired origin questions are gone from the public form (no own-country / avoid-country screens, no hidden inputs)
    assert 'name="own_region"' not in html and 'name="avoid_answer"' not in html and 'name="avoid_regions"' not in html and 'name="avoid_countries"' not in html


def test_values_are_strict_and_never_coerced():
    assert feat.value_of(None) is None and feat.value_of("unknown") is None and feat.value_of("") is None
    assert feat.value_of(True) == 1 and feat.value_of("yes") == 1 and feat.value_of("Yes") == 1
    assert feat.value_of(False) == 0 and feat.value_of("no") == 0
    for bad in (1, 0, 2, [], ["yes"], {}, "maybe", "x", "true"):
        with pytest.raises(ValueError):
            feat.value_of(bad)
    # workbook cells: only the agreed tokens; ticks, crosses and other text are "not understood" (None), reported by the importer
    assert feat.cell_to_value("Yes") == 1 and feat.cell_to_value(" y ") == 1 and feat.cell_to_value(1) == 1 and feat.cell_to_value(True) == 1
    assert feat.cell_to_value("No") == 0 and feat.cell_to_value("n") == 0 and feat.cell_to_value(0) == 0 and feat.cell_to_value(False) == 0
    for bad in ("x", "✓", "maybe", 2, "yes please", "-"):
        assert feat.cell_to_value(bad) is None
    assert feat.cell_to_value(None) is None and feat.cell_to_value("  ") is None


def test_clean_wanted_orders_dedupes_and_limits_top_three():
    errors = {}
    wanted, top = feat.clean_wanted(["Gym", "Balcony", "Gym", "Views"], top=["Gym", "Views"], errors=errors)
    assert wanted == ["Views", "Balcony", "Gym"] and top == ["Views", "Gym"] and errors == {}
    errors = {}
    feat.clean_wanted(["Helipad"], top=[], errors=errors)
    assert errors == {"features": "Please tick features from the list."}
    errors = {}
    feat.clean_wanted(["Gym"], top=["Pool"], errors=errors)
    assert errors == {"features_top": "Please choose from the features you ticked."}
    errors = {}
    feat.clean_wanted(["Gym", "Pool", "Sauna", "Views"], top=["Gym", "Pool", "Sauna", "Views"], errors=errors)
    assert errors == {"features_top": "Please choose up to 3."}
    errors = {}
    assert feat.clean_wanted(None, top=None, errors=errors) == ([], []) and errors == {}
    errors = {}
    feat.clean_wanted("Gym", top=None, errors=errors)   # a bare string is not a list
    assert "features" in errors


def test_compare_reports_facts_only():
    apt = {"feat_gym": 1, "feat_pool": 0, "feat_balcony": None, "parking_charge": "$60 per week"}
    for k in feat.KEYS:
        apt.setdefault("feat_" + k, None)
    out = feat.compare(["Gym", "Pool", "Balcony"], ["Pool"], apt)
    by = {i["key"]: i for i in out["items"]}
    assert by["gym"]["status"] == "available" and by["pool"]["status"] == "unavailable" and by["balcony"]["status"] == "unverified"
    assert by["pool"]["matters_most"] is True and by["gym"]["matters_most"] is False
    assert out["counts"] == {"available": 1, "unavailable": 1, "unverified": 1} and out["parking_charge"] == "$60 per week"
    # no score, weight or exclusion anywhere in the shape
    assert not any(k in json.dumps(out).lower() for k in ("score", "weight", "exclude", "penalt"))
    assert feat.compare([], [], apt) == {"items": [], "counts": {"available": 0, "unavailable": 0, "unverified": 0}, "parking_charge": "$60 per week"}


# ---------------- MateBot enquiry: 3.11 / 3.12 and retired origin questions ----------------

def test_enquiry_stores_features_and_strips_origin_answers(client):
    anon = TestClient(client.app)
    r = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Gym", "Balcony"], features_top=["Gym"],
                                                      own_region="Prefer to", avoid_answer="Yes", avoid_regions=["Europe"], avoid_countries=["Brazil"]),
                  headers=hdr())
    assert r.status_code == 201, r.text
    s = staff(client)
    e = client.get(f"/api/enquiries/{r.json()['id']}", headers=s).json()
    assert e["features"] == ["Balcony", "Gym"] and e["features_top"] == ["Gym"]
    assert not any(k in e for k in ("own_region", "avoid_answer", "avoid_regions", "avoid_countries"))
    c = appdb(client)
    db_row = row(c.execute("SELECT own_region, avoid_answer, avoid_regions, avoid_countries, features, features_top FROM enquiries WHERE id=?", (e["id"],)))
    assert db_row["own_region"] is None and db_row["avoid_regions"] is None and db_row["avoid_countries"] is None and db_row["avoid_answer"] in (None, "")
    assert json.loads(db_row["features"]) == ["Balcony", "Gym"]
    # optional: nothing ticked is valid and stored as empty lists
    r = anon.post("/api/public/enquiries", json=good_answers(is_test=False, email="t2@example.com"), headers=hdr())
    assert r.status_code == 201
    g = client.get(f"/api/enquiries/{r.json()['id']}", headers=s).json()
    assert g["features"] == [] and g["features_top"] == [] and "own_region" not in g and "avoid_answer" not in g


def test_enquiry_rejects_bad_feature_answers(client):
    anon = TestClient(client.app)
    r = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Helipad"]), headers=hdr())
    assert r.status_code == 422 and r.json()["errors"]["features"] == "Please tick features from the list."
    r = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Gym"], features_top=["Pool"]), headers=hdr())
    assert r.status_code == 422 and r.json()["errors"]["features_top"] == "Please choose from the features you ticked."
    r = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Gym", "Pool", "Sauna", "Views"], features_top=["Gym", "Pool", "Sauna", "Views"]), headers=hdr())
    assert r.status_code == 422 and r.json()["errors"]["features_top"] == "Please choose up to 3."


def test_staff_edit_features_and_retired_origin_keys(client):
    anon = TestClient(client.app)
    eid = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Gym", "Balcony"], features_top=["Gym"]), headers=hdr()).json()["id"]
    s = staff(client)
    r = client.patch(f"/api/enquiries/{eid}", json={"features": ["Pool", "Gym"], "features_top": ["Pool"]}, headers=s)
    assert r.status_code == 200, r.text
    g = client.get(f"/api/enquiries/{eid}", headers=s).json()
    assert g["features"] == ["Pool", "Gym"] and g["features_top"] == ["Pool"]
    # a top choice that is no longer ticked is refused, not silently dropped
    r = client.patch(f"/api/enquiries/{eid}", json={"features": ["Sauna"]}, headers=s)
    assert r.status_code == 422 and r.json()["errors"]["features_top"] == "Please choose from the features you ticked."
    # retired origin keys cannot be edited back in
    for k, v in (("own_region", "Prefer to"), ("avoid_answer", "Yes"), ("avoid_regions", ["Europe"]), ("avoid_countries", ["Brazil"])):
        r = client.patch(f"/api/enquiries/{eid}", json={k: v}, headers=s)
        assert r.status_code == 422, (k, r.text)
        assert r.json()["errors"] == {k: enquiries.RETIRED_EDIT_MESSAGE}


def test_receipt_rows_for_features(client):
    anon = TestClient(client.app)
    c = appdb(client)
    eid = anon.post("/api/public/enquiries", json=good_answers(is_test=False, features=["Gym", "Balcony", "Pool"], features_top=["Pool", "Gym"]), headers=hdr()).json()["id"]
    t = receipts.render_receipt(row(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,))))["text"]
    assert "Which apartment and building features would you particularly like?: Balcony, Pool, Gym" in t
    assert "Which three matter most to you?: Pool, Gym" in t
    assert "prefer not to live with" not in t and "Specific countries" not in t


# ---------------- HomeBot ----------------

def test_homebot_extras_top_optional_and_limited(client):
    anon = TestClient(client.app)
    r = anon.post("/api/public/homebot/enquiries", json=hb_body(extras=["Balcony", "Gym", "Pool"], extras_top=["Gym", "Balcony"]), headers=hdr())
    assert r.status_code == 201, r.text
    s = staff(client)
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}", headers=s).json()
    assert e["extras"] == ["Balcony", "Pool", "Gym"] and e["extras_top"] == ["Balcony", "Gym"]
    flat = str(e["summary"])
    assert "Features wanted (wishes, not filters)" in flat and "Matter most (up to three)" in flat
    r = anon.post("/api/public/homebot/enquiries", json=hb_body(extras=["Balcony"], extras_top=["Gym"]), headers=hdr())
    assert r.status_code == 422 and r.json()["errors"]["extras_top"] == "Please choose from the features you ticked."
    r = anon.post("/api/public/homebot/enquiries", json=hb_body(extras=APPROVED[:4], extras_top=APPROVED[:4]), headers=hdr())
    assert r.status_code == 422 and r.json()["errors"]["extras_top"] == "Please choose up to 3."
    r = anon.post("/api/public/homebot/enquiries", json=hb_body(extras=[], extras_top=[]), headers=hdr())
    assert r.status_code == 201
    e = client.get(f"/api/homebot/enquiries/{r.json()['id']}", headers=s).json()
    assert e["extras"] == [] and e["extras_top"] == []
    meta = anon.get("/api/public/homebot/meta").json()
    assert [f["label"] for f in meta["features"]] == feat.LABELS and meta["features_max_top"] == 3


# ---------------- apartments: API, import, export ----------------

def _apt(c, s, address="Unit 1 / 1 Test Street"):
    r = c.post("/api/apartments", json={"address": address, "suburb": "Melbourne", "type_code": "2B2B", "available_from": "2027-01-10",
                                        "rent_12m": 900, "rent_6m": 950, "bills_included": 1, "bedroom1": "1 king", "bedroom2": "1 king"}, headers=s)
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_apartment_features_api_default_unknown_and_strict(client):
    s = staff(client)
    a = _apt(client, s)
    assert set(a["features"]) == set(feat.KEYS) and set(a["features"].values()) == {"unknown"} and a["features_recorded"] == 0
    assert a["parking_charge"] is None and not any(k.startswith("feat_") for k in a)
    r = client.patch(f"/api/apartments/{a['id']}", json={"features": {"gym": "yes", "pool": "no"}, "parking_charge": "$60 per week"}, headers=s)
    assert r.status_code == 200, r.text
    a = client.get(f"/api/apartments/{a['id']}", headers=s).json()
    assert a["features"]["gym"] == "yes" and a["features"]["pool"] == "no" and a["features"]["sauna"] == "unknown" and a["features_recorded"] == 2
    assert a["parking_charge"] == "$60 per week"
    # back to unknown is allowed (a recorded value can be withdrawn); invalid values and unknown keys are refused
    assert client.patch(f"/api/apartments/{a['id']}", json={"features": {"gym": "unknown"}}, headers=s).status_code == 200
    assert client.get(f"/api/apartments/{a['id']}", headers=s).json()["features_recorded"] == 1
    for bad in ({"gym": "maybe"}, {"gym": 1}, {"helipad": "yes"}, ["gym"], {"gym": ["yes"]}):
        r = client.patch(f"/api/apartments/{a['id']}", json={"features": bad}, headers=s)
        assert r.status_code == 422, (bad, r.text)
    r = client.patch(f"/api/apartments/{a['id']}", json={"parking_charge": "x" * 81}, headers=s)
    assert r.status_code == 422
    ref = client.get("/api/reference", headers=s).json()
    assert [f["label"] for f in ref["features"]] == feat.LABELS and ref["feature_groups"] == feat.GROUPS and ref["features_max_top"] == 3
    pub = TestClient(client.app).get("/api/public/meta").json()
    assert [f["label"] for f in pub["features"]] == feat.LABELS


def _sheet(path, rows, headers):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    wb.save(path)


def test_template_matches_baseline_and_adds_blank_feature_columns(tmp_path):
    base = importer.read_xlsx(BASELINE)
    tpl = importer.read_xlsx(TEMPLATE)
    assert len(base) == len(tpl) == 103
    for b, t in zip(base, tpl):
        for k in importer.BASELINE_HEADINGS:
            assert b.get(k) == t.get(k), (k, b, t)   # original values, including the formula results in the 6-month column, are intact
    heads = list(tpl[0].keys())
    assert heads[:9] == importer.BASELINE_HEADINGS and heads[9:29] == feat.LABELS and heads[29:31] == [feat.PARKING_CHARGE_HEADING, importer.MIN_LEASE_HEADING]
    assert all(all(t.get(h) in (None, "") for h in feat.LABELS + [feat.PARKING_CHARGE_HEADING]) for t in tpl)   # nothing invented
    # the original workbook on disk is untouched: no feature headings
    assert list(base[0].keys()) == importer.BASELINE_HEADINGS


def test_workbook_with_uncached_formulas_is_refused(tmp_path):
    p = str(tmp_path / "f.xlsx")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(importer.BASELINE_HEADINGS)
    ws.append(["Unit 1 / 1 Test Street", "2027-01-10", 900, "=C2*1.05", "2B2B", "1 king", "1 king", None, "Included"])
    wb.save(p)
    with pytest.raises(importer.WorkbookError):
        importer.read_xlsx(p)


def test_import_feature_columns_unknown_blank_invalid_and_staff_precedence(client, tmp_path):
    c = appdb(client)
    s = staff(client)
    base = importer.read_xlsx(BASELINE)
    importer.import_apartments(c, BASELINE, "test", replace=True)
    c.commit()
    rec = base[0]
    a = row(c.execute("SELECT * FROM apartments WHERE address_raw=?", (str(rec["Address"]),)))
    assert a, rec["Address"]
    aid = a["id"]
    # staff record one value first
    assert client.patch(f"/api/apartments/{aid}", json={"features": {"gym": "yes", "pool": "yes"}}, headers=s).status_code == 200
    # sheet: gym=No (conflict), pool blank (kept), sauna=Yes (new), views="maybe" (invalid), parking charge text
    heads = importer.BASELINE_HEADINGS + ["Gym", "Pool", "Sauna", "Views", feat.PARKING_CHARGE_HEADING]
    rowvals = [rec[h] for h in importer.BASELINE_HEADINGS] + ["No", None, "Yes", "maybe", "$55 a week"]
    p = str(tmp_path / "feat.xlsx")
    _sheet(p, [rowvals], heads)
    rep = importer.import_apartments(c, p, "test")
    c.commit()
    a = client.get(f"/api/apartments/{aid}", headers=s).json()
    assert a["features"]["gym"] == "yes" and a["features"]["pool"] == "yes" and a["features"]["sauna"] == "yes" and a["features"]["views"] == "unknown"
    assert a["parking_charge"] == "$55 a week"
    assert rep["feature_rows"] == 1
    assert [(x["feature"], x["text"]) for x in rep["feature_errors"]] == [("Views", "maybe")]
    assert [(x["feature"], x["recorded"], x["sheet"]) for x in rep["feature_conflicts"]] == [("Gym", "yes", "no")]
    assert rep["feature_overwritten"] == []
    # explicit overwrite: the sheet's differing value replaces the staff value; blank and invalid still change nothing
    rep = importer.import_apartments(c, p, "test", features_overwrite=True)
    c.commit()
    a = client.get(f"/api/apartments/{aid}", headers=s).json()
    assert a["features"]["gym"] == "no" and a["features"]["pool"] == "yes" and a["features"]["views"] == "unknown"
    assert len(rep["feature_overwritten"]) == 1 and rep["feature_conflicts"] == []
    # a baseline refresh (no feature columns) leaves every staff value alone
    rep = importer.import_apartments(c, BASELINE, "test")
    c.commit()
    a = client.get(f"/api/apartments/{aid}", headers=s).json()
    assert a["features"]["gym"] == "no" and a["features"]["pool"] == "yes" and a["features"]["sauna"] == "yes" and a["parking_charge"] == "$55 a week"
    assert rep["feature_rows"] == 0
    # other apartments are still unknown throughout: nothing was inferred
    others = c.execute("SELECT COUNT(*) FROM apartments WHERE id<>? AND (" + " OR ".join(f"{col} IS NOT NULL" for col in feat.COLUMNS) + ")", (aid,)).fetchone()[0]
    assert others == 0
    dq = client.get("/api/apartments-data-quality", headers=s).json()
    assert dq["features_complete"] == 0 and dq["features_unknown_cells"] == 103 * 20 - 3


def test_export_round_trip(client, tmp_path):
    c = appdb(client)
    s = staff(client)
    importer.import_apartments(c, BASELINE, "test", replace=True)
    c.commit()
    a0 = row(c.execute("SELECT id FROM apartments ORDER BY id LIMIT 1"))
    client.patch(f"/api/apartments/{a0['id']}", json={"features": {"lift_access": "yes", "concierge": "no"}, "parking_charge": "$70/wk", "min_lease_months": 3}, headers=s)
    r = client.get("/api/apartments/export.xlsx", headers=s)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats") and "attachment" in r.headers["content-disposition"]
    p = str(tmp_path / "export.xlsx")
    open(p, "wb").write(r.content)
    wb = openpyxl.load_workbook(p)
    ws = wb["Apartments"]
    heads = [c_.value for c_ in ws[1]]
    assert heads == importer.EXPORT_HEADINGS and heads[:9] == importer.BASELINE_HEADINGS
    assert ws.max_row == 104
    recs = importer.read_xlsx(p)
    a0_full = client.get(f"/api/apartments/{a0['id']}", headers=s).json()
    first = next(x for x in recs if str(x["Address"]).strip() == (a0_full["address_raw"] or a0_full["address"]).strip())
    assert first["Lift access"] == "Yes" and first["Concierge"] == "No" and first["Gym"] in (None, "") and first[feat.PARKING_CHARGE_HEADING] == "$70/wk"
    assert first[importer.MIN_LEASE_HEADING] == 3
    # loading the export back changes nothing (idempotent) and keeps the recorded values
    rep = importer.import_apartments(c, p, "test")
    c.commit()
    assert rep["feature_errors"] == [] and rep["feature_conflicts"] == [] and rep["added"] == 0
    a = client.get(f"/api/apartments/{a0['id']}", headers=s).json()
    assert a["features"]["lift_access"] == "yes" and a["features"]["concierge"] == "no" and a["features_recorded"] == 2 and a["min_lease_months"] == 3
    # a 6-month rent read back from the export equals the baseline (no formula loss on export)
    assert all(x["2026 6 months rate"] not in (None, "") for x in recs)


def test_anonymous_cannot_export_or_edit_features(client):
    anon = TestClient(client.app)
    assert anon.get("/api/apartments/export.xlsx").status_code == 401
