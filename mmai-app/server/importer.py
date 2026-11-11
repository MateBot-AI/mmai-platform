"""Import ALM's apartment list (A13 columns) and run the brief's section 3 data quality checks."""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime

import openpyxl

from . import rules
from . import apartment_features as feat
from .db import audit, now_iso, retention_guard, rows

_GEO = json.load(open(os.path.join(os.path.dirname(__file__), "data", "geocode.json")))

STREET_WORDS = {
    r"\bSt\b\.?": "Street", r"\bStreet\b": "Street", r"\bPl\b\.?": "Place", r"\bLn\b\.?": "Lane", r"\bRd\b\.?": "Road",
    r"\bAve\b\.?": "Avenue", r"\bPde\b\.?": "Parade",
}


def normalise_address(raw: str) -> tuple[str, str, str]:
    """Return (display address, street for geocoding, suburb). The raw text is stored separately (brief 2.3)."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip()
    m = re.match(r"(?i)unit\s*(\S+)\s*/\s*(.+)$", s)
    if m:
        unit, street = m.group(1), m.group(2).strip()
    else:
        unit, street = "", s
    # expand the street-type abbreviation only on the final word ("St Andrews Pl" -> "St Andrews Place")
    parts = street.split(" ")
    last = parts[-1].rstrip(".")
    for pat, rep in STREET_WORDS.items():
        if re.fullmatch(pat.replace("\\b", ""), last, flags=re.I):
            parts[-1] = rep
            break
    street = " ".join(parts)
    # title-case words but keep apostrophes like A'Beckett and ordinal unit like 12a
    def tc(w):
        if "'" in w:
            a, b = w.split("'", 1)
            return a.capitalize() + "'" + b.capitalize()
        if re.match(r"^\d", w):
            return w
        return w.capitalize() if w.lower() not in ("st",) else w
    street = " ".join(tc(w) for w in street.split(" "))
    street = street.replace("Mackenzie", "MacKenzie")
    if "Darling Street" in street:
        suburb = "South Yarra"
    elif "St Andrews Place" in street:
        suburb = "East Melbourne"
    else:
        suburb = "Melbourne CBD"
    display = f"Unit {unit} / {street}" if unit else street
    return display, street, suburb


def _geo_for(street_display: str, suburb: str):
    # geocode.json keys use the short street form from the sheet, e.g. "35 Darling St, South Yarra VIC 3141"
    short = street_display.replace("Street", "St").replace("Place", "Pl").replace("Lane", "Lane")
    sub = {"South Yarra": "South Yarra VIC 3141", "East Melbourne": "East Melbourne VIC 3002"}.get(suburb, "Melbourne VIC 3000")
    for key in (f"{short}, {sub}", f"{street_display}, {sub}"):
        g = _GEO.get(key)
        if g:
            return g["lat"], g["lon"]
    # fuzzy: match by number + first word
    m = re.match(r"(\S+)\s+(\S+)", short)
    if m:
        pref = (m.group(1) + " " + m.group(2)).lower()
        for k, g in _GEO.items():
            if g and k.lower().startswith(pref):
                return g["lat"], g["lon"]
    return None, None


def parse_date_cell(v):
    if v is None or v == "":
        return None, None
    if isinstance(v, datetime):
        return v.date().isoformat(), None
    if isinstance(v, date):
        return v.isoformat(), None
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat(), None
        except ValueError:
            pass
    return None, s  # text instead of a date: imported as not yet available


class WorkbookError(ValueError):
    """The spreadsheet cannot be read safely (e.g. formulas without saved results)."""


def read_xlsx(path: str) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    header = [str(c).strip() if c else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        rec = dict(zip(header, r))
        out.append(rec)
    # Formula cells carry their last calculated value only if the file was saved by a spreadsheet program. A file written by a
    # script may hold formulas with no result: reading it would turn (for example) every 6-month rent into a blank. Refuse it
    # outright instead of importing silently; the fix is to open and save the file in Excel (or LibreOffice) first.
    wf = openpyxl.load_workbook(path, data_only=False).active
    missing = []
    for row in wf.iter_rows(min_row=2):
        if not row or row[0].value in (None, ""):
            continue
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("=") and ws.cell(row=c.row, column=c.column).value is None:
                missing.append(c.coordinate)
    if missing:
        raise WorkbookError(f"{len(missing)} formula cell(s) have no saved result (first: {', '.join(missing[:5])}). "
                            "Open the file in Excel, let it calculate, save it and load it again.")
    return out


# A15: Bill's corrections to the 23 October 2026 list, applied on import so a re-import never re-introduces the errors.
# (b) Units 604, 606 and 607/10 St Andrews Place sleep 5 (A4(b)(iv)): the third bed is in Bedroom 1 (confirmed by Bill, 26 Oct 2026),
#     Bedroom 2 is "1 king or 2 singles".
# (c) Unit 904/543 Elizabeth St: Bedroom 2 is "1 king or 2 singles".
A15B_BEDROOM1 = "1 king or 2 singles or 3 singles or 1 king+1 single"  # third bed in Bedroom 1 (A15(b)); allows A4(e) couple + one other
A15_CORRECTIONS: dict[str, dict] = {
    "Unit 604 / 10 St Andrews Place": {"bedroom1": A15B_BEDROOM1, "bedroom2": "1 king or 2 singles"},
    "Unit 606 / 10 St Andrews Place": {"bedroom1": A15B_BEDROOM1, "bedroom2": "1 king or 2 singles"},
    "Unit 607 / 10 St Andrews Place": {"bedroom1": A15B_BEDROOM1, "bedroom2": "1 king or 2 singles"},
    "Unit 904 / 543 Elizabeth Street": {"bedroom2": "1 king or 2 singles"},
}
A15_NOT_AVAILABLE = {"Unit 52 / 222 Victoria Street", "Unit 5 / 35 Darling Street", "Unit 8 / 35 Darling Street"}  # A15(a)


def apply_a15_corrections(c, staff: str | None = None) -> dict:
    """Idempotent data correction for the live table (A15). Only updates the named rows; never deletes."""
    changed = []
    for addr, fix in A15_CORRECTIONS.items():
        a = c.execute("SELECT id, bedroom1, bedroom2, bedroom3 FROM apartments WHERE address=?", (addr,)).fetchone()
        if not a:
            continue
        for col, val in fix.items():
            if a[col] != val:
                c.execute(f"UPDATE apartments SET {col}=?, updated_at=? WHERE id=?", (val, now_iso(), a["id"]))
                changed.append(f"{addr}: {col} -> {val}")
    # A9(a)/A15(d): the minimum lease is generally 2 months and was 2 months on every apartment at launch. The old default was 3, so
    # a table where every apartment still says 3 is moved to 2 once. A 3 that staff set on one apartment (A9(b)) is left alone.
    n = c.execute("SELECT COUNT(*) FROM apartments WHERE min_lease_months=3").fetchone()[0]
    others = c.execute("SELECT COUNT(*) FROM apartments WHERE min_lease_months<>3").fetchone()[0]
    if n and not others:
        c.execute("UPDATE apartments SET min_lease_months=?, updated_at=? WHERE min_lease_months=3", (rules.DEFAULT_MIN_LEASE_MONTHS, now_iso()))
        changed.append(f"min_lease_months 3 -> {rules.DEFAULT_MIN_LEASE_MONTHS} on {n} apartments")
    c.commit()
    if changed:
        audit(c, staff, "a15_corrections", "apartments", None, "; ".join(changed))
    return {"changed": changed}


# ---- Apartment and building features (8 Nov 2026): optional workbook columns headed by the 20 feature labels + "Car parking charge".
# Cells: Yes / No -> recorded; blank -> unknown, which never overwrites a value staff have already recorded; any other text is an
# ERROR for that cell (reported, nothing stored: never coerced to yes or no). A valid sheet value that differs from a recorded
# staff value is reported as a conflict and the staff value is kept unless the import is run with features_overwrite=True.
MIN_LEASE_HEADING = "Minimum lease (months)"


def _feature_cells(rec: dict) -> tuple[dict, list, bool, str | None, str | None, bool]:
    """Read the feature columns of one sheet row.

    Returns (values {key: 0|1} for valid non-blank cells, errors [{feature, text}], any_feature_column_present,
             parking_charge text or None, parking charge column present, min_lease value (unchecked) or None)."""
    lower = {str(k).strip().lower(): k for k in rec if k}
    values, errors, present = {}, [], False
    for key, label, _g in feat.FEATURES:
        col = lower.get(label.lower())
        if col is None:
            continue
        present = True
        cell = rec.get(col)
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            continue
        v = feat.cell_to_value(cell)
        if v is None:
            errors.append({"feature": label, "text": str(cell).strip()})
        else:
            values[key] = v
    pcol = lower.get(feat.PARKING_CHARGE_HEADING.lower())
    parking = None
    if pcol is not None and rec.get(pcol) not in (None, ""):
        parking = str(rec.get(pcol)).strip() or None
        if parking and len(parking) > feat.MAX_PARKING_CHARGE:   # too long to be a charge as stated: reported, not cut down or stored
            errors.append({"feature": feat.PARKING_CHARGE_HEADING, "text": parking[:40] + "…"})
            parking = None
    mcol = lower.get(MIN_LEASE_HEADING.lower())
    min_lease = rec.get(mcol) if mcol is not None else None
    return values, errors, present, parking, pcol is not None, min_lease


def _apply_features(c, existing, display: str, rec: dict, report: dict, overwrite: bool) -> dict:
    """Decide the feature / parking-charge / minimum-lease columns for one row. Returns {column: value} to write."""
    values, errors, present, parking, parking_col, min_lease = _feature_cells(rec)
    for e in errors:
        report["feature_errors"].append({"address": display, **e})
    if present:
        report["feature_rows"] += 1
    out = {}
    for key, v in values.items():
        col = f"feat_{key}"
        cur = existing[col] if existing is not None and col in existing.keys() else None
        if cur is None or cur == v:
            if cur != v:
                out[col] = v
        elif overwrite:
            out[col] = v
            report["feature_overwritten"].append({"address": display, "feature": feat.LABEL_OF[key], "was": feat.state_of(cur), "now": feat.state_of(v)})
        else:
            report["feature_conflicts"].append({"address": display, "feature": feat.LABEL_OF[key], "recorded": feat.state_of(cur), "sheet": feat.state_of(v)})
    if parking is not None:
        cur = existing["parking_charge"] if existing is not None and "parking_charge" in existing.keys() else None
        if cur in (None, "") or cur == parking or overwrite:
            if cur != parking:
                out["parking_charge"] = parking
        else:
            report["feature_conflicts"].append({"address": display, "feature": feat.PARKING_CHARGE_HEADING, "recorded": cur, "sheet": parking})
    if min_lease not in (None, ""):
        try:
            ml = float(str(min_lease).strip())
            if not (0 < ml <= 60):
                raise ValueError
        except ValueError:
            report["feature_errors"].append({"address": display, "feature": MIN_LEASE_HEADING, "text": str(min_lease).strip()})
        else:
            cur = existing["min_lease_months"] if existing is not None else None
            # the default (2 months, A2(h)) counts as "not yet set by staff": a sheet value may replace it; any other recorded value is kept unless overwriting
            if cur is None or float(cur) == float(rules.DEFAULT_MIN_LEASE_MONTHS) or float(cur) == ml or overwrite:
                if cur is None or float(cur) != ml:
                    out["min_lease_months"] = ml
            else:
                report["feature_conflicts"].append({"address": display, "feature": MIN_LEASE_HEADING, "recorded": cur, "sheet": ml})
    return out


def import_apartments(c, path: str, staff: str | None = None, replace: bool = False, features_overwrite: bool = False) -> dict:
    """Import the apartment list. Default (replace=False): update by address, so apartment ids, lease status, groups, the minimum
    lease and staff-entered dates survive; new addresses are added; addresses missing from the list are left untouched and reported.
    replace=True wipes the table first (first-time load and tests only). The agreed list corrections (A15) are applied in both cases."""
    from . import stock_lifecycle
    recs = read_xlsx(path)
    report = {"imported": 0, "added": 0, "updated": 0, "unchanged": 0, "kept_not_in_list": [], "dates_kept": [], "non_date_rows": [], "capacity_issues": [], "errors": [],
              "feature_rows": 0, "feature_errors": [], "feature_conflicts": [], "feature_overwritten": [], "stock_transitions": []}
    seen = set()
    # Stock lifecycle: the whole list update is ONE guarded step (the import already commits as one unit). Every material change
    # to an existing apartment records a transition and the follow-up work for the groups that list it; `replace=True` retires
    # each existing apartment through the same history-preserving path ('replaced', treated like a deletion) before the wipe.
    # The follow-up re-checks themselves run after the commit (stock_lifecycle.run_pending, one guarded step per group).
    from . import access_facts
    with retention_guard(c, label="apartment import"):
        if replace:
            ids = [a["id"] for a in rows(c.execute("SELECT id FROM apartments ORDER BY id"))]
            removed = access_facts.confirmations_at(c, ids)   # G2: report + audit what the wipe removes (A3(a): no archive)
            for aid in ids:
                report["stock_transitions"].append(stock_lifecycle.record_transition(c, aid, "replaced", staff)["transition_id"])
            c.execute("DELETE FROM apartments")
            report["access_facts_removed"] = access_facts.audit_removed(c, removed, staff, "table replaced")
        bound_before = access_facts.bound_snapshot(c)
        _import_rows(c, recs, staff, report, seen, features_overwrite, stock_lifecycle)
        report["access_facts_unbound"] = access_facts.unbound_since(c, bound_before, staff, "apartment list import")   # G4: never silent
    report["kept_not_in_list"] = [r["address"] for r in rows(c.execute("SELECT address FROM apartments ORDER BY address")) if r["address"] not in seen]
    audit(c, staff, "import_apartments", "apartments", None,
          f"{report['imported']} rows from {os.path.basename(path)}: {report['added']} added, {report['updated']} updated, {report['unchanged']} unchanged, "
          f"{len(report['kept_not_in_list'])} not in the list kept" + (" (table replaced first)" if replace else "")
          + (f"; features: {report['feature_rows']} row(s) with feature columns, {len(report['feature_errors'])} cell error(s), "
             f"{len(report['feature_conflicts'])} kept staff value(s), {len(report['feature_overwritten'])} overwritten" if report["feature_rows"] else "")
          + (f"; {len(report['stock_transitions'])} stock transition(s) recorded" if report["stock_transitions"] else ""))
    report["corrections"] = apply_a15_corrections(c, staff)["changed"]  # the agreed list corrections are re-applied after every import
    if report["stock_transitions"]:
        failed: list = []
        affected: list = []
        for tid in report["stock_transitions"]:
            affected.extend(stock_lifecycle.run_pending(c, transition_id=tid, staff=staff, failures=failed))
        report["groups_rechecked"] = {"affected": affected, "pending_group_ids": [f["group_id"] for f in failed]}
    report.update(data_quality(c))
    return report


def _import_rows(c, recs: list, staff: str | None, report: dict, seen: set, features_overwrite: bool, stock_lifecycle) -> None:
    for rec in recs:
        try:
            addr_raw = str(rec.get("Address", "")).strip()
            display, street, suburb = normalise_address(addr_raw)
            avail, avail_raw = parse_date_cell(rec.get("Vacate date"))
            code = rules.normalise_type(str(rec.get("Apartment type", "")))
            info = rules.APT_TYPES[code]
            beds = [str(rec.get(k) or "").strip() or None for k in ("Bedroom 1", "Bedroom 2", "Bedroom 3")]
            for col, val in A15_CORRECTIONS.get(display, {}).items():  # A15(b), A15(c)
                beds[int(col[-1]) - 1] = val
            parsed = [rules.parse_bedding(b) for b in beds if b]
            mn, mx = rules.apartment_capacity(parsed)
            if avail_raw:
                report["non_date_rows"].append({"address": display, "text": avail_raw})
            if not rules.capacity_fits_a4b(code, mx):
                report["capacity_issues"].append({"address": display, "type": code, "max_capacity": mx, "limit": info["a4b_max"]})
            if len(parsed) != info["bedrooms"]:
                report["capacity_issues"].append({"address": display, "type": code, "note": f"{len(parsed)} bedroom columns filled but type has {info['bedrooms']} bedrooms"})
            bills = str(rec.get("Bills/utilities", "")).strip().lower().startswith("incl")
            rent12, rent6 = float(rec.get("2026 12 months rate")), float(rec.get("2026 6 months rate"))
            seen.add(display)
            existing = c.execute("SELECT * FROM apartments WHERE address=?", (display,)).fetchone()
            if existing:
                new_avail, new_raw = avail, avail_raw
                if avail is None and existing["available_from"]:
                    # the list still has text instead of a date but staff have entered one: keep the staff date (A15(a))
                    new_avail, new_raw = existing["available_from"], existing["available_raw"]
                    report["dates_kept"].append({"address": display, "date": existing["available_from"]})
                vals = {"address_raw": addr_raw, "suburb": suburb, "type_code": code, "bathrooms": info["bathrooms"], "rent_12m": rent12, "rent_6m": rent6,
                        "available_from": new_avail, "available_raw": new_raw, "bedroom1": beds[0], "bedroom2": beds[1], "bedroom3": beds[2], "bills_included": 1 if bills else 0}
                changed = {k: v for k, v in vals.items() if existing[k] != v}
                changed.update(_apply_features(c, existing, display, rec, report, features_overwrite))
                if changed:
                    material = stock_lifecycle.material_changes(dict(existing), changed)
                    sets = ", ".join(f"{k}=?" for k in changed)
                    c.execute(f"UPDATE apartments SET {sets}, updated_at=? WHERE id=?", (*changed.values(), now_iso(), existing["id"]))
                    if material:
                        cause = "withdrawn" if ("available_from" in material and changed.get("available_from") is None) else "changed"
                        report["stock_transitions"].append(stock_lifecycle.record_transition(c, existing["id"], cause, staff, changed_fields=list(material))["transition_id"])
                    report["updated"] += 1
                else:
                    report["unchanged"] += 1
                if existing["lat"] is None:
                    lat, lon = _geo_for(street, suburb)
                    c.execute("UPDATE apartments SET lat=?, lon=? WHERE id=?", (lat, lon, existing["id"]))
            else:
                lat, lon = _geo_for(street, suburb)
                extra = _apply_features(c, None, display, rec, report, features_overwrite)   # features from the sheet, if any columns exist
                min_lease = extra.pop("min_lease_months", rules.DEFAULT_MIN_LEASE_MONTHS)
                cols = ["city", "address", "address_raw", "suburb", "type_code", "bathrooms", "rent_12m", "rent_6m", "available_from", "available_raw",
                        "min_lease_months", "bedroom1", "bedroom2", "bedroom3", "bills_included", "lat", "lon", "updated_at"] + list(extra)
                c.execute(f"INSERT INTO apartments({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                          ["Melbourne", display, addr_raw, suburb, code, info["bathrooms"], rent12, rent6,
                           avail, avail_raw, min_lease, beds[0], beds[1], beds[2], 1 if bills else 0, lat, lon, now_iso()] + list(extra.values()))
                report["added"] += 1
            report["imported"] += 1
        except Exception as e:  # report, don't guess
            report["errors"].append({"row": rec, "error": str(e)})


def data_quality(c) -> dict:
    """Section 3 checks, computed from the live table so they stay current."""
    apts = rows(c.execute("SELECT * FROM apartments"))
    count = len(apts)
    non_date = [{"address": a["address"], "text": a["available_raw"]} for a in apts if a["available_from"] is None]
    cap_issues = []
    for a in apts:
        parsed = [rules.parse_bedding(a[k]) for k in ("bedroom1", "bedroom2", "bedroom3") if a[k]]
        mn, mx = rules.apartment_capacity(parsed)
        if not rules.capacity_fits_a4b(a["type_code"], mx):
            cap_issues.append({"address": a["address"], "type": a["type_code"], "max_capacity": mx, "limit": rules.APT_TYPES[a["type_code"]]["a4b_max"]})
        if len(parsed) != rules.APT_TYPES[a["type_code"]]["bedrooms"]:
            cap_issues.append({"address": a["address"], "type": a["type_code"], "note": f"{len(parsed)} bedroom column(s) filled; type has {rules.APT_TYPES[a['type_code']]['bedrooms']} bedroom(s)"})
    # I14 price guide vs list
    derived = rules.compute_price_guide_from_list(apts)
    guide_diffs = []
    for kind in ("12m", "6m"):
        for o in rules.price_guide(kind):
            d = derived.get(f"{kind}:{o['id']}")
            if not d:
                guide_diffs.append({"guide": kind, "option": o["label"], "bill": [o["min"], o["max"]], "list": None, "note": "no apartments of this type/occupancy in the list"})
                continue
            lo, hi = round(d["min"]), round(d["max"])
            if lo != o["min"] or hi != o["max"]:
                rounding = abs(lo - o["min"]) <= 1 and abs(hi - o["max"]) <= 1
                guide_diffs.append({"guide": kind, "option": o["label"], "bill": [o["min"], o["max"]], "list": [lo, hi],
                                    "note": "rounding only" if rounding else "differs"})
    return {
        "apartment_count": count,
        "a1_expected": rules.A1_INVENTORY,
        "a1_matches": count == rules.A1_INVENTORY,
        "non_date_rows_live": non_date,
        "capacity_issues_live": cap_issues,
        "min_lease_default": rules.DEFAULT_MIN_LEASE_MONTHS,
        "min_lease_values": {str(r["m"]): r["n"] for r in rows(c.execute("SELECT min_lease_months m, COUNT(*) n FROM apartments GROUP BY 1"))},
        "price_guide_differences": guide_diffs,
        "by_type": {t: sum(1 for a in apts if a["type_code"] == t) for t in rules.APT_TYPES},
        # 8 Nov 2026 features: how much of the 20-feature record is still unknown (nothing is assumed for the blanks)
        "features_complete": sum(1 for a in apts if all(a.get(col) in (0, 1) for col in feat.COLUMNS)),
        "features_unknown_cells": sum(1 for a in apts for col in feat.COLUMNS if a.get(col) not in (0, 1)),
    }


# ---- Workbook export (8 Nov 2026): the nine baseline columns in their original order, then the 20 feature columns, the car-parking
# charge and the minimum lease, so staff can fill the columns in Excel and re-import the same file. Unknown features export blank.
BASELINE_HEADINGS = ["Address", "Vacate date", "2026 12 months rate", "2026 6 months rate", "Apartment type", "Bedroom 1", "Bedroom 2", "Bedroom 3", "Bills/utilities"]
EXPORT_HEADINGS = BASELINE_HEADINGS + feat.LABELS + [feat.PARKING_CHARGE_HEADING, MIN_LEASE_HEADING]


def _min_lease_cell(v):
    """Whole months as an integer, fractional months kept exactly as stored (text), so a 2.5 never becomes 2 or 3."""
    if v is None:
        return None
    f = float(v)
    return int(f) if f == int(f) else str(v)


def export_rows(c) -> list[list]:
    out = []
    for a in rows(c.execute("SELECT * FROM apartments ORDER BY address")):
        vac = a["available_from"] or a["available_raw"] or None
        if a["available_from"]:
            try:
                vac = datetime.fromisoformat(a["available_from"])
            except ValueError:
                vac = a["available_from"]
        base = [a["address_raw"] or a["address"], vac, a["rent_12m"], a["rent_6m"], a["type_code"], a["bedroom1"], a["bedroom2"], a["bedroom3"],
                "Included" if a["bills_included"] else "Excluded"]
        feats = [feat.value_to_cell(a.get(f"feat_{k}")) or None for k in feat.KEYS]
        out.append(base + feats + [a.get("parking_charge") or None, _min_lease_cell(a["min_lease_months"])])
    return out


def export_xlsx(c) -> bytes:
    import io
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Apartments"
    ws.append(EXPORT_HEADINGS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in export_rows(c):
        ws.append(r)
    for col in ws.iter_cols(min_row=2, max_row=ws.max_row, min_col=2, max_col=2):
        for cell in col:
            if isinstance(cell.value, datetime):
                cell.number_format = "DD/MM/YYYY"
    ws.freeze_panes = "B2"
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
