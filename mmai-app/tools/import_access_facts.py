"""Import Bill's step-free access confirmations (A13(a), 10 November 2026) into ONE MateBot database - T3(d) C3.

Input: the confirmation file (JSON list; each row: apartment_id, address, step_free_access true|null, status, confirmation_source,
scope, explanation). The file describes a FIXED inventory: a row applies to an apartment only when BOTH the supplied inventory id
is that apartment's id AND the row's address matches the apartment's CURRENT canonical address (normalised `address`; a stale
address_raw is never a match). Anything
else is skipped and reported (`skipped`, with a plain reason): id not in stock, address differing from the row at that id, the
address found under another id only (renumbered stock), an unreadable value. Nothing is inferred for apartments that are not in
the file (no automatic extension to new stock); a null value is recorded as UNKNOWN with status 'excluded_from_confirmation' (the
source and scope stay attached); an existing identical bound record is left alone (`unchanged`); and the same confirmation is
never re-applied to an apartment whose location identity (address/suburb/city) changed after it was recorded - that row is
skipped as 'location changed since this confirmation; a fresh confirmation is needed'.

The batch is bound to the inventory's ORIGINAL location on its first application too (reviewer G6): a separately recorded trusted
inventory-location reference (JSON list of {apartment_id, address, suburb, city} taken from the fixed inventory the confirmation
was made for; `--export-inventory` writes one from a database) is REQUIRED, and a row applies only when the apartment's present
suburb/city equal the reference's for that id. Nothing is inferred about location or access.

Usage:
    MMAI_DB=/path/to/copy.db python -m tools.import_access_facts --export-inventory inventory-locations.json [--source-label "..."]
Every run prints the sha256 and row count of BOTH input files; --apply writes ONE audit event 'access_facts_import' holding those and
the recorded / unchanged / skipped counts (also when everything was skipped). The digests identify which files were used; they do not
make an arbitrary operator file trustworthy - the reference for a controlled import must come from the sealed original inventory, not
from an export of the database being changed.
    MMAI_DB=/path/to/copy.db python -m tools.import_access_facts --file apartment-access-facts.json --inventory inventory-locations.json [--apply] [--staff "name"]
Without --apply it is a dry run (prints what would change). With --apply, every recorded apartment's affected groups are re-queued
through access_facts.apply_to_groups and the totals printed are its own (checked / requeued / unchanged / skipped) - nothing is
re-stamped or released by hand. Never point it at the canonical database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

from server import access_facts
from server.db import audit, connect, maybe_commit, now_iso, row, rows

DEFAULT_STAFF = "import of the staff confirmation file"   # plain staff-facing attribution (what History shows as 'by')
IMPORT_AUDIT = "access_facts_import"                      # ONE audit event per --apply: provenance of both inputs + counts


def file_digest(path: str) -> str:
    """sha256 (hex) of the file's bytes - printed and audited for provenance. This identifies WHICH file was used; it does not
    authenticate an arbitrary operator file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


norm_address = access_facts.norm_address


LOCATION_CHANGED = "location changed since this confirmation; a fresh confirmation is needed"


def _skip(out, e, reason: str, found_id=None):
    out["skipped"].append({"apartment_id": e.get("apartment_id"), "address": e.get("address"), "reason": reason, **({"found_id": found_id} if found_id is not None else {})})


NO_INVENTORY = "no trusted inventory-location reference supplied"
NOT_IN_INVENTORY = "id not in the inventory-location reference"
INVENTORY_ADDRESS = "address differs from the inventory-location reference"
INVENTORY_LOCATION = "suburb/city differ from the inventory-location reference (moved before this confirmation was applied)"


def _loc(v) -> str:
    return re.sub(r"\s+", " ", (v or "").strip().lower())


def inventory_reference(c) -> list[dict]:
    """The trusted inventory-location reference as recorded from a database: [{apartment_id, address, suburb, city}] - read-only."""
    return [{"apartment_id": int(a["id"]), "address": a["address"], "suburb": a["suburb"], "city": a["city"]}
            for a in rows(c.execute("SELECT id, address, suburb, city FROM apartments ORDER BY id"))]


def load_inventory(obj) -> dict[int, dict]:
    """{apartment_id: {address, suburb, city}} from a reference list (or {"apartments": [...]}); rows without an id are ignored."""
    items = obj.get("apartments") if isinstance(obj, dict) else obj
    out = {}
    for r in items or []:
        try:
            out[int(r.get("apartment_id"))] = {"address": r.get("address"), "suburb": r.get("suburb"), "city": r.get("city")}
        except (TypeError, ValueError):
            continue
    return out


def plan(c, entries: list[dict], inventory: dict[int, dict] | None = None) -> dict:
    """{record: [...], unchanged: [apartment ids], skipped: [{apartment_id, address, reason[, found_id]}]} - read-only. `inventory`
    (load_inventory) is the trusted location reference the batch is bound to; without it every row is skipped (fail closed)."""
    # the CURRENT canonical address only (the row's `address`): a stale address_raw kept beside a changed address is not a match
    apts = rows(c.execute("SELECT id, address, suburb, city, " + ", ".join(access_facts.MIRROR_FIELDS) + " FROM apartments"))
    by_id = {int(a["id"]): a for a in apts}
    by_addr: dict[str, list[int]] = {}
    for a in apts:
        key = norm_address(a.get("address"))
        if key:
            by_addr.setdefault(key, []).append(int(a["id"]))
    out = {"record": [], "unchanged": [], "skipped": []}
    for e in entries:
        try:
            fid = int(e.get("apartment_id"))
        except (TypeError, ValueError):
            _skip(out, e, "no inventory id in the row")
            continue
        key = norm_address(e.get("address"))
        a = by_id.get(fid)
        elsewhere = [i for i in by_addr.get(key, []) if i != fid]
        if a is None:
            _skip(out, e, "id not in stock" + ("; address found under another id" if elsewhere else ""), found_id=(elsewhere[0] if len(elsewhere) == 1 else None))
            continue
        if not key or key != norm_address(a.get("address")):
            _skip(out, e, "address differs from the apartment at this id" + ("; address found under another id" if elsewhere else ""), found_id=(elsewhere[0] if len(elsewhere) == 1 else None))
            continue
        # G6: bound to the inventory's original location on the FIRST application too - the reference, not the row, says where the
        # confirmed apartment was; a row moved to another suburb/city before the batch is applied gets nothing
        if inventory is None:
            _skip(out, e, NO_INVENTORY)
            continue
        ref = inventory.get(fid)
        if ref is None:
            _skip(out, e, NOT_IN_INVENTORY)
            continue
        if norm_address(ref.get("address")) != key:   # the reference's address must be the row's present canonical address too
            _skip(out, e, INVENTORY_ADDRESS)
            continue
        if _loc(ref.get("suburb")) != _loc(a.get("suburb")) or _loc(ref.get("city")) != _loc(a.get("city")):
            _skip(out, e, INVENTORY_LOCATION)
            continue
        try:
            v = access_facts.value_from_text(e.get("step_free_access"))
        except ValueError:
            _skip(out, e, "unreadable value")
            continue
        status = e.get("status") or ("confirmed" if v == 1 else "not_step_free" if v == 0 else "unknown")
        src = e.get("confirmation_source") or "confirmation file"
        eff = access_facts.effective(a)
        if eff["record_id"] is not None:
            rec = row(c.execute("SELECT value, source FROM apartment_access_facts WHERE id=?", (eff["record_id"],))) or {}
            same = (rec.get("value") is None if v is None else (rec.get("value") is not None and int(rec["value"]) == v)) and rec.get("source") == src
            if same and eff["bound"]:
                out["unchanged"].append(a["id"])   # the same bound record already stands; re-recording would only invalidate current reviews for nothing
                continue
            if same and not eff["bound"]:
                _skip(out, e, LOCATION_CHANGED)   # the old confirmation must not travel to a moved / re-addressed row
                continue
        out["record"].append({"apartment_id": a["id"], "address": a["address"], "value": v, "status": status, "source": src,
                              "scope": e.get("scope"), "note": e.get("explanation")})
    return out


def main(argv=None, c=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", help="the confirmation file (JSON)")
    ap.add_argument("--inventory", help="the trusted inventory-location reference (JSON) the batch is bound to; required with --file")
    ap.add_argument("--export-inventory", metavar="OUT", help="write the inventory-location reference of the connected database to OUT and exit")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--staff", default=DEFAULT_STAFF, help="who is recorded as having made the import (plain words; shown in History)")
    ap.add_argument("--source-label", default=None, help="with --export-inventory: a label naming where this stock came from (metadata only)")
    args = ap.parse_args(argv)
    c = c if c is not None else connect()
    if args.export_inventory:
        ref = inventory_reference(c)
        out = {"exported_at": now_iso(), "stock_count": len(ref), "source_label": args.source_label or "database export (not a sealed authority)",
               "apartments": ref}
        with open(args.export_inventory, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"inventory-location reference: {len(ref)} apartment(s) written to {args.export_inventory} (sha256 {file_digest(args.export_inventory)}); "
              f"this records the connected database's stock as it is now - it is not evidence of the original inventory")
        return 0
    if not args.file or not args.inventory:
        ap.error("--file and --inventory are both required (or use --export-inventory)")
    with open(args.file, encoding="utf-8") as f:
        entries = json.load(f)
    if isinstance(entries, dict):
        entries = entries.get("apartments") or []
    with open(args.inventory, encoding="utf-8") as f:
        inventory = load_inventory(json.load(f))
    prov = {"file": os.path.basename(args.file), "file_sha256": file_digest(args.file), "file_rows": len(entries),
            "inventory": os.path.basename(args.inventory), "inventory_sha256": file_digest(args.inventory), "inventory_rows": len(inventory)}
    print(f"confirmation file {prov['file']}: sha256 {prov['file_sha256']}, {prov['file_rows']} row(s)")
    print(f"inventory reference {prov['inventory']}: sha256 {prov['inventory_sha256']}, {prov['inventory_rows']} apartment(s)")
    p = plan(c, entries, inventory)
    print(f"rows {len(entries)}: to record {len(p['record'])}, unchanged {len(p['unchanged'])}, skipped {len(p['skipped'])}")
    for sk in p["skipped"]:
        extra = f" (address found at id {sk['found_id']})" if sk.get("found_id") is not None else ""
        print(f"  skipped id {sk['apartment_id']} {sk['address']!r}: {sk['reason']}{extra}")
    if not args.apply:
        print("dry run: nothing written (use --apply)")
        return 0
    totals = {"checked": 0, "requeued": 0, "unchanged": 0, "skipped": 0}
    for r in p["record"]:
        access_facts.record(c, r["apartment_id"], r["value"], source=r["source"], staff=args.staff, scope=r["scope"], note=r["note"], status=r["status"])
        res = access_facts.apply_to_groups(c, r["apartment_id"], args.staff)
        totals["checked"] += int(res.get("checked") or 0)
        for k in ("requeued", "unchanged", "skipped"):
            totals[k] += len(res.get(k) or [])
    # ONE provenance event for the whole import (also when every row was skipped): which two files, their digests and row counts,
    # and what happened - recorded / unchanged / skipped rows, plus the group effects. Closed keys only; no row content.
    detail = dict(prov, recorded=len(p["record"]), unchanged=len(p["unchanged"]), skipped=len(p["skipped"]),
                  groups_checked=totals["checked"], groups_requeued=totals["requeued"], groups_unchanged=totals["unchanged"], groups_skipped=totals["skipped"])
    audit(c, args.staff, IMPORT_AUDIT, "apartment_access_facts", None, json.dumps(detail, sort_keys=True))
    maybe_commit(c)
    print(f"recorded {len(p['record'])} apartment(s); groups with stored evidence checked {totals['checked']}, re-queued for a current review {totals['requeued']}, "
          f"unchanged {totals['unchanged']}, skipped {totals['skipped']}; audit event {IMPORT_AUDIT} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
