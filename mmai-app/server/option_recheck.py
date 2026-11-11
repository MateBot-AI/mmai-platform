"""Explicit staff repair of stored options, not an automatic member-edit or lease policy.

Uses the existing stock-lifecycle rule checks. No membership, response, approval or
introduction-clock writes; no model or mail call. Every persisted effect is one step.
"""
from . import ai_queue, alerts, db, matching, stock_lifecycle as sl
from .db import audit, now_iso, row, rows

ALERT_KIND = "group_apartment_unavailable_recheck"


class RecheckRefused(ValueError):
    pass


class GroupNotFound(KeyError):
    pass


def recheck(c, gid: int, staff: str) -> dict:
    with db.retention_guard(c, label="staff option recheck"):
        g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g:
            raise GroupNotFound(gid)
        if g["status"] not in ("Proposed", "Introduced", "Group confirmed"):
            raise RecheckRefused("Re-check options is available for proposed, introduced or confirmed groups. Complete any replacement decision first.")
        if sl.group_has_pending_work(c, gid):
            raise RecheckRefused("This group is still being re-checked after a stock change; reload the page and check its apartment before offering.")
        units = sl._group_units(c, gid)
        if units is None:
            raise RecheckRefused("The group's current member records could not be checked safely. Nothing was changed; check those records before trying again.")
        current, when = g.get("apartment_id"), now_iso()
        options = rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,)))
        excluded = matching.excluded_option_ids(matching.evidence_rows(c, gid))
        removed, rewritten = [], []
        for o in options:
            aid = int(o["apartment_id"])
            a = row(c.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
            if aid in excluded:
                verdict, payload = "invalid", None
            else:
                verdict, payload = sl._option_validity(c, a, o, units)
            if verdict == "invalid":
                c.execute("DELETE FROM group_apartments WHERE id=?", (o["id"],))
                removed.append(aid)
            elif verdict == "rewrite":
                sl._rewrite_option(c, o, a, units, payload)
                rewritten.append(aid)
        remaining = rows(c.execute("SELECT id, apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,)))
        for rank, o in enumerate(remaining, start=1):
            c.execute("UPDATE group_apartments SET rank=? WHERE id=?", (rank, o["id"]))
        remaining_ids = [int(o["apartment_id"]) for o in remaining]
        restocked = False
        if not remaining_ids:
            # Existing retained-group recovery, never a hand-picked apartment or people.
            sl._restock_options(c, gid, units)
            remaining_ids = [int(o["apartment_id"]) for o in rows(c.execute(
                "SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank,id", (gid,)))]
            restocked = bool(remaining_ids)
        lost = current is not None and int(current) not in remaining_ids
        chosen = int(current) if current is not None and int(current) in remaining_ids else (remaining_ids[0] if remaining_ids else None)
        if chosen is None:
            sl._clear_current_offer(c, gid, when, g)
        elif current != chosen or restocked:
            matching._switch_current(c, gid, chosen)
        if (current in rewritten or (restocked and current == chosen and chosen is not None)) and g["status"] in sl.INTRO_STAMP_STATUSES and not g.get("offer_changed_since_intro_at"):
            c.execute("UPDATE groups SET offer_changed_since_intro_at=? WHERE id=?", (when, gid))
        # Member facts may have changed even when the resulting prices did not.
        # Full reassessment deliberately clears verdict, brief, ready_at and evidence.
        ai_queue.enqueue(c, [gid], refresh=False, commit=False)
        if chosen is None:
            # No model work can repair a missing rule-passing offer. Hold truthfully,
            # rather than sending an empty packet or labelling it an AI failure.
            c.execute("""UPDATE groups SET ai_status='failed', ai_queued_at=NULL,
                         ranking_basis='Rules only (no suitable apartment)', ai_error=?, held_reason=?
                         WHERE id=?""", (ai_queue.NO_SUITABLE_OFFER_TEXT, ai_queue.NO_SUITABLE_OFFER_TEXT, gid))
        note = ("The previously offered apartment no longer suits the group under its current requirements; "
                if lost else "The group's options were re-checked against its current requirements; ")
        note += ("a full AI review is required before sharing." if chosen is not None
                 else "no listed apartment currently passes every requirement, so the group remains held without an apartment.")
        c.execute("UPDATE groups SET rerank_note=?, reranked_at=?, flag_kind='stock', updated_at=? WHERE id=?",
                  (note, when, when, gid))
        audit(c, staff, "group_options_rechecked", "group", gid,
              f"checked {len(options)}; removed {len(removed)}; rewritten {len(rewritten)}; remaining {len(remaining_ids)}; full review {int(chosen is not None)}")
        if lost:
            revision = c.execute("SELECT MAX(id) FROM audit_log WHERE action='group_options_rechecked' AND entity='group' AND entity_id=?",
                                 (str(gid),)).fetchone()[0]
            result = alerts.raise_attention(c, ALERT_KIND, gid, is_test=matching._group_is_test(c, gid),
                                            revision=int(revision), in_transaction=True)
            if result.get("status") == "error":
                raise sl.AlertOutboxUnavailable("The staff attention record could not be saved. Nothing was changed; try the option re-check again.")
        result = {"group_id": gid, "checked": len(options), "removed": len(removed), "rewritten": len(rewritten),
                  "remaining": len(remaining_ids), "restocked": restocked, "apartment_id": chosen,
                  "full_review_queued": chosen is not None, "message": note}
    ai_queue.kick()  # after commit as well as enqueue's wake: no missed early wake
    return result
