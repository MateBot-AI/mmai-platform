"""Stock lifecycle (9 Nov 2026): what happens to existing groups when an apartment stops being a valid offer.

Register basis (no new business rule): A3(f) known leased or unavailable apartments leave available suggestions promptly under
GP8; A3a an apartment ALM loses is deleted outright (the apartment, not the groups' own history); GP6(b) offers come from the
group's own ranked list, the next best when the top one goes; GP8(a)/(b) immediate rule-based re-ranking, AI refresh later; GP13
membership approval is about the people (an apartment-only change never resets it); H19 one internal staff alert when a previously
suggested apartment becomes unavailable; A17 the vacancy cap applies to the actual dates (both directions of a date change).

Shape:
  Transaction 1 (caller's retention-guard step, `record_transition`): the inventory change itself (lease claim / PATCH / relist /
  DELETE / import) + `apartments.lease_generation + 1` + one `stock_transitions` row + one `offer_reconcile_work` row per live
  group that listed the apartment (group id and the apartment's role captured BEFORE any option row or the apartment row goes).
  Transactions 2..n (`reconcile_work`, one guard step per group): re-read CURRENT state; the row is `superseded` when a newer
  transition exists for the same apartment (the newest judges validity; a stale cause never removes a now-valid offer); otherwise
  every option is checked against today's stock and rules, invalid ones removed, ranks renumbered, the first currently VALID
  remaining option becomes current or the pointer goes NULL (held, plain reason, offer copies cleared); the H19 alert
  (revision = transition id, deduplicated by the outbox's UNIQUE event id) is raised when the captured role shows the group held
  the apartment and it is not a valid offer now - whether or not this step had anything left to delete (a cascade may already have
  done it); the GP8(b) refresh marking is written in the same step; then the row is `done`. Any failure rolls the whole step back
  and leaves the row pending for the retry driver. No provider call happens here.
"""
from __future__ import annotations

import logging
import re

from . import db as dbm
from . import rules
from .db import audit, j, now_iso, pj, row, rows

log = logging.getLogger("mmai.stock")

EV_APARTMENT_UNAVAILABLE = "group_apartment_unavailable"   # alerts.EVENTS (record type group); one fixed cause-neutral body
CAUSES = ("leased", "withdrawn", "changed", "deleted", "replaced", "relisted")
LIVE_EXCLUDED = ("Leased", "Dissolved")
BOOTSTRAP_KEY = "stock_lifecycle.bootstrap"
INTRO_STAMP_STATUSES = ("Introduced", "Group confirmed")

# Apartment fields whose change can alter whether, or on what figures, an existing group's option stands (rules / displayed offer).
MATERIAL_FIELDS = frozenset({"available_from", "rent_12m", "rent_6m", "min_lease_months", "type_code", "bedroom1", "bedroom2", "bedroom3",
                             "bills_included", "address", "suburb", "lat", "lon"})


def is_material_field(name: str) -> bool:
    return name in MATERIAL_FIELDS or name.startswith("feat_")


class AlertOutboxUnavailable(RuntimeError):
    """The H19 alert row for a reconcile step could not be queued; the step is rolled back and retried as a whole."""


def _norm(v):
    if v in ("", None):
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    s = str(v)
    try:
        return round(float(s), 6)
    except ValueError:
        return s


def material_changes(before: dict, after: dict) -> dict:
    """{field: [old, new]} for material fields whose stored value changes; `after` holds only the fields being written."""
    out = {}
    for k, v in after.items():
        if is_material_field(k) and _norm(before.get(k)) != _norm(v):
            out[k] = [before.get(k), v]
    return out


# ------------------------------------------------------------------------------------------------------------ transaction 1
def record_transition(c, apartment_id: int, cause: str, staff: str | None, *, changed_fields: dict | list | None = None,
                      exclude_group_id: int | None = None, bump: bool = False) -> dict:
    """Inside the caller's guard. Writes the transition row (carrying the apartment's CURRENT lease_generation - that column stays a
    lease-only counter, bumped by the lease claim itself; `bump=True` is kept for a caller that owns a lease without having bumped)
    and one pending work row per live group that lists the apartment (current or alternative). Supersession between transitions of
    the same apartment is by transition id, never by generation. Must run BEFORE the apartment row is deleted.
    Returns {transition_id, generation, address, group_ids}."""
    if cause not in CAUSES:
        raise ValueError("unknown stock transition cause")
    if not dbm.guarded(c):
        raise RuntimeError("stock transition outside a retention guard")
    a = row(c.execute("SELECT id, address, suburb, city, lease_generation FROM apartments WHERE id=?", (apartment_id,)))
    if not a:
        raise ValueError("Apartment not found")
    when = now_iso()
    gen = int(a.get("lease_generation") or 0)
    if bump:
        c.execute("UPDATE apartments SET lease_generation=lease_generation+1, updated_at=? WHERE id=?", (when, apartment_id))
        gen += 1
    fields = sorted(changed_fields) if isinstance(changed_fields, dict) else (sorted(changed_fields) if changed_fields else None)
    c.execute("INSERT INTO stock_transitions(apartment_id, address, cause, generation, changed_fields, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
              (apartment_id, a["address"], cause, gen, j(fields) if fields else None, when, staff))
    tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    gids = []
    if cause != "relisted":
        ph = ",".join("?" * len(LIVE_EXCLUDED))
        cur = rows(c.execute(f"SELECT id FROM groups WHERE apartment_id=? AND status NOT IN ({ph}) ORDER BY id", (apartment_id, *LIVE_EXCLUDED)))
        alt = rows(c.execute(f"""SELECT DISTINCT g.id FROM group_apartments ga JOIN groups g ON g.id=ga.group_id
                                 WHERE ga.apartment_id=? AND g.status NOT IN ({ph}) AND (g.apartment_id IS NULL OR g.apartment_id<>?) ORDER BY g.id""",
                             (apartment_id, *LIVE_EXCLUDED, apartment_id)))
        for role, lst in (("current", cur), ("alternative", alt)):
            for r in lst:
                gid = int(r["id"])
                if exclude_group_id is not None and gid == int(exclude_group_id):
                    continue
                c.execute("INSERT OR IGNORE INTO offer_reconcile_work(transition_id, group_id, role, status, created_at) VALUES (?,?,?,'pending',?)", (tid, gid, role, when))
                gids.append(gid)
    if gids:
        # G-SL2 (11 Nov 2026): the unit's building identity is captured WITH the pending work it belongs to, so a follow-up that runs
        # after the apartment row is gone (delete route) can still answer "did a current member decline this building?" from the
        # members' records without inference. '' = captured, the unit has no building identity; NULL = not captured (no work, or a
        # row written before this change). Cleared atomically when the transition's last pending row closes (_clear_key_if_closed).
        from . import apartment_rejections
        c.execute("UPDATE stock_transitions SET building_key=? WHERE id=?", (apartment_rejections.building_identity(a), tid))
    return {"transition_id": int(tid), "generation": gen, "address": a["address"], "group_ids": gids}


def _clear_key_if_closed(c, transition_id: int) -> None:
    """G-SL2: the captured building identity is pending-decision evidence only - NULL it once no work row of this transition is
    pending. One predicate, idempotent, run in the same guard step as each terminal work write and inside delete_groups."""
    c.execute("""UPDATE stock_transitions SET building_key=NULL WHERE id=? AND building_key IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM offer_reconcile_work WHERE transition_id=? AND status='pending')""", (int(transition_id), int(transition_id)))


def delete_groups(c, group_ids) -> int:
    """G-SL2: the ONLY `DELETE FROM groups` in server/. Runs inside the caller's retention guard: the pending work rows of the
    deleted groups vanish by cascade, so the transitions they belonged to are checked for closure in the same step (delete and key
    clearing atomic; a rollback restores both). Empty input -> no statement, 0."""
    ids = sorted({int(g) for g in (group_ids or ())})
    if not ids:
        return 0
    if not dbm.guarded(c):
        raise RuntimeError("group deletion outside a retention guard")
    ph = ",".join("?" * len(ids))
    tids = [int(r["transition_id"]) for r in rows(c.execute(f"SELECT DISTINCT transition_id FROM offer_reconcile_work WHERE status='pending' AND group_id IN ({ph})", ids))]
    c.execute(f"DELETE FROM groups WHERE id IN ({ph})", ids)
    for tid in tids:
        _clear_key_if_closed(c, tid)
    return len(ids)


def latest_transition(c, apartment_id: int) -> dict | None:
    return row(c.execute("SELECT * FROM stock_transitions WHERE apartment_id=? ORDER BY id DESC LIMIT 1", (apartment_id,)))


# ------------------------------------------------------------------------------------------------------------ durable truth
def pending_work(c, *, transition_id: int | None = None, apartment_id: int | None = None, group_id: int | None = None) -> list[dict]:
    """Pending work rows joined with their transition (apartment_id, address, cause, generation)."""
    where, args = ["w.status='pending'"], []
    if transition_id is not None:
        where.append("w.transition_id=?"); args.append(int(transition_id))
    if apartment_id is not None:
        where.append("t.apartment_id=?"); args.append(int(apartment_id))
    if group_id is not None:
        where.append("w.group_id=?"); args.append(int(group_id))
    return rows(c.execute(f"""SELECT w.id, w.transition_id, w.group_id, w.role, w.status, w.legacy_event_id, t.apartment_id, t.address, t.cause, t.generation,
                                     t.created_at AS transition_at
                              FROM offer_reconcile_work w JOIN stock_transitions t ON t.id=w.transition_id
                              WHERE {' AND '.join(where)} ORDER BY w.transition_id, w.group_id""", args))


def group_has_pending_work(c, group_id: int) -> bool:
    return bool(row(c.execute("SELECT 1 FROM offer_reconcile_work WHERE group_id=? AND status='pending' LIMIT 1", (group_id,))))


# ------------------------------------------------------------------------------------------------------------ transactions 2..n
def run_pending(c, *, transition_id: int | None = None, apartment_id: int | None = None, staff: str | None = None,
                failures: list | None = None) -> list[dict]:
    """Complete pending work rows, each in its own guard step. Returns the list of per-group results whose step changed something
    (outcome moved / list_trimmed / no_apartment / refreshed). With `failures` given, a failing group's step is recorded there as
    {group_id, category} and the others proceed; without it the exception propagates. The refresh marking is written inside each
    step (durable, worker-only); nothing here calls a provider."""
    affected = []
    for w in pending_work(c, transition_id=transition_id, apartment_id=apartment_id):
        try:
            done = reconcile_work(c, w["id"], staff=staff)
        except Exception as e:
            if failures is None:
                raise
            if isinstance(e, dbm.ConnectionLostMidOperation):
                raise
            failures.append({"group_id": w["group_id"], "category": type(e).__name__})
            log.warning("stock reconcile for group %s (transition %s) failed: %s (left pending)", w["group_id"], w["transition_id"], type(e).__name__)
            continue
        if done and done.get("outcome") in ("moved", "list_trimmed", "no_apartment", "refreshed"):
            affected.append(done)
    return affected


def reconcile_work(c, work_id: int, staff: str | None = None) -> dict | None:
    """One group's follow-up as ONE retention-guard step (idempotent: re-reads everything inside).

    Succ3 (10 Nov 2026): (1) every option is JUDGED - an option whose rule check cannot be made (member projection unavailable, rule
    error) is not "ok": the step raises OptionsNotJudgeable, rolls back and stays pending for the retry driver (never a silent move
    onto an unjudged option, never a completed NULL hold); (2) the captured role's loss is decided by re-evaluating the transition
    apartment for this group today, whether or not an earlier step already removed its option row (job order never loses the
    original-loss alert); (3) a material 'changed' transition (MATERIAL_FIELDS / feat_*) whose figures happen to be equal still
    refreshes the ranking (GP8(b)) and, for the current offer of an introduced group, stamps the offer-change flag."""
    from . import ai_queue, alerts, matching
    w0 = row(c.execute("SELECT w.*, t.cause FROM offer_reconcile_work w JOIN stock_transitions t ON t.id=w.transition_id WHERE w.id=?", (work_id,)))
    label = "lease rerank" if (w0 or {}).get("cause") == "leased" else "stock reconcile"
    with dbm.retention_guard(c, label=label):
        w = row(c.execute("""SELECT w.*, t.apartment_id, t.address, t.cause, t.generation, t.changed_fields, t.created_at AS transition_at,
                                    t.building_key AS transition_building_key
                             FROM offer_reconcile_work w JOIN stock_transitions t ON t.id=w.transition_id WHERE w.id=?""", (work_id,)))
        if not w or w["status"] != "pending":
            return None
        gid, aid, tid = int(w["group_id"]), int(w["apartment_id"]), int(w["transition_id"])
        when = now_iso()
        # same-apartment ownership: the newest transition for this apartment judges validity; an older row is superseded untouched
        latest = c.execute("SELECT MAX(id) FROM stock_transitions WHERE apartment_id=?", (aid,)).fetchone()[0]
        if latest is not None and int(latest) != tid:
            c.execute("UPDATE offer_reconcile_work SET status='superseded', outcome='superseded', done_at=? WHERE id=?", (when, work_id))
            _clear_key_if_closed(c, tid)
            return {"group_id": gid, "outcome": "superseded", "transition_id": tid}
        g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
        if not g or g["status"] in LIVE_EXCLUDED:
            c.execute("UPDATE offer_reconcile_work SET status='done', outcome='skipped', done_at=? WHERE id=?", (when, work_id))
            _clear_key_if_closed(c, tid)
            return None
        day = str(w["transition_at"])[:10]
        units = _group_units(c, gid)
        options = rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,)))
        own_row = row(c.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
        current = g.get("apartment_id")
        # GP14 one lens (inc3, root decision 11 Nov 2026): a row a current member's active record excludes was never an effective
        # offer of this group. Such a NON-current row is history only: pruned here without a verdict (it is not a loss: no note,
        # flag, alert or refresh for it) and never a fallback for the pointer. The group's CURRENT pointer on an excluded row (legacy
        # state) is NOT exempt: it is judged like any offer, invalidated, and the pointer moves to an effective option or is held.
        excluded = matching.excluded_option_ids(matching.evidence_rows(c, gid))
        history_only = {int(o["apartment_id"]) for o in options
                        if int(o["apartment_id"]) in excluded and (current is None or int(o["apartment_id"]) != int(current))}
        removed, rewritten, verdicts = [], [], {}
        for o in options:
            if int(o["apartment_id"]) in history_only:
                c.execute("DELETE FROM group_apartments WHERE id=?", (o["id"],))   # silent prune of cached history (the decline record keeps the history)
                continue
            a = own_row if int(o["apartment_id"]) == aid else row(c.execute("SELECT * FROM apartments WHERE id=?", (o["apartment_id"],)))
            verdict, payload = _option_validity(c, a, o, units)   # raises OptionsNotJudgeable instead of guessing
            verdicts[int(o["apartment_id"])] = (verdict, payload)
            if verdict == "invalid":
                removed.append((o, payload))
            elif verdict == "rewrite":
                rewritten.append(int(o["apartment_id"]))
                _rewrite_option(c, o, a, units, payload)
        # the ORIGINAL loss (captured role current or alternative): is the transition apartment a valid offer for this group NOW?
        # Judged independently of its option row, so a loss an earlier step already pruned from the list is still this transition's.
        # A transition apartment a current member's active record excludes was never this group's offer: nothing lost - whether its
        # cached row is still present (history_only) or an earlier step / full run already pruned it (G-SL1: decided from the
        # members' records via the shared lens, not from the cached rows). The group's CURRENT pointer on it is still judged.
        # G-SL2 (root ruling 11 Nov 2026): when the apartment ROW is gone (delete route: cascade removed the option rows, the group
        # pointer was set NULL) the question is answered from the members' records and the transition's captured building identity
        # (no inference). A captured role 'current' with no row is still JUDGED (loss path); only an 'alternative' without a row can
        # be history. Row present: every conjunct is the inc3 one.
        history = (aid in history_only
                   or (aid not in verdicts
                       and (current is None or aid != int(current))
                       and (own_row is not None or w["role"] != "current")
                       and _declined_by_a_member(c, gid, aid, w)))
        if history:
            own_verdict, own_reason = "history", None
        elif aid in verdicts:
            own_verdict, own_reason = verdicts[aid]
        else:
            own_verdict, own_reason = _option_validity(c, own_row, {"group_id": gid, "apartment_id": aid, "allocation_json": None}, units)
        own_lost = own_verdict == "invalid"
        if not own_lost:
            own_reason = None
        removed_ids = {int(o["apartment_id"]) for o, _ in removed}
        old_rank = {int(o["apartment_id"]): int(o["rank"]) for o in options}
        for o, _ in removed:
            c.execute("DELETE FROM group_apartments WHERE id=?", (o["id"],))
        remaining = rows(c.execute("SELECT id, apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,)))
        for r, ga in enumerate(remaining, start=1):
            c.execute("UPDATE group_apartments SET rank=? WHERE id=?", (r, ga["id"]))
        remaining_ids = [int(x["apartment_id"]) for x in remaining if int(x["apartment_id"]) not in excluded]   # effective options only: never a declined fallback
        # a material change of a still-valid listed offer (equal figures included): the ranking is refreshed; the current offer of an
        # introduced group additionally carries the display flag (D4) - the public page withholds details until staff acknowledge
        material_own = w["cause"] == "changed" and not own_lost and aid in remaining_ids
        side_lost = None   # the CURRENT offer removed by this step although it is not the transition apartment (its own reason)
        if current is not None and int(current) in removed_ids and int(current) != aid:
            side_lost = (int(current), verdicts.get(int(current), ("invalid", None))[1])
        if current is None or int(current) in removed_ids or int(current) not in remaining_ids:
            if remaining_ids:
                matching._switch_current(c, gid, remaining_ids[0])   # only judged-valid options remain
                outcome = "moved"
            else:
                _clear_current_offer(c, gid, when, g)
                outcome = "no_apartment"
        elif int(current) in rewritten or (material_own and int(current) == aid):
            outcome = "refreshed"
            if g["status"] in INTRO_STAMP_STATUSES and not g.get("offer_changed_since_intro_at"):
                c.execute("UPDATE groups SET offer_changed_since_intro_at=? WHERE id=?", (when, gid))
        elif rewritten or material_own:
            outcome = "refreshed"   # an alternative's facts or figures moved: the ranking is refreshed (GP8(b)); the current offer itself is unchanged, no flag
        elif removed:
            outcome = "list_trimmed"
        else:
            outcome = "unchanged"
        note = None
        if own_lost or removed:
            note = _note(w, day, own_lost, own_reason, outcome, remaining_ids, old_rank.get(aid), c, removed, side_lost)
            flag = "lease" if w["cause"] == "leased" else "stock"
            c.execute("UPDATE groups SET rerank_note=?, reranked_at=?, flag_kind=?, updated_at=? WHERE id=?", (note, when, flag, when, gid))
        if own_lost:
            already = bool(w.get("legacy_event_id")) and bool(row(c.execute("SELECT 1 FROM internal_alert_outbox WHERE event_id=?", (w["legacy_event_id"],))))
            if not already:
                res = alerts.raise_attention(c, EV_APARTMENT_UNAVAILABLE, gid, is_test=matching._group_is_test(c, gid), revision=tid, in_transaction=True)
                if res.get("status") == "error":
                    raise AlertOutboxUnavailable("the internal attention alert for this re-check could not be queued; the step is left pending for retry")
        # GP8(b): the members did not change; the apartment ranking is refreshed in the background when the offered set or its facts moved
        # (a group held with NO apartment is not queued: there is nothing to rank until full matching regroups it - as before)
        if outcome in ("moved", "list_trimmed", "refreshed"):
            ai_queue.enqueue(c, [gid], refresh=True, commit=False)
        c.execute("UPDATE offer_reconcile_work SET status='done', outcome=?, done_at=? WHERE id=?", (outcome, when, work_id))
        _clear_key_if_closed(c, tid)
        return {"group_id": gid, "outcome": outcome, "note": note, "status": g["status"], "transition_id": tid}


def reoffer_held_groups(c, is_test: int, staff: str | None = None) -> list[int]:
    """Succ3 (L10): for every live Introduced / Group confirmed group of this data set held with NO current offer and no pending
    stock work, offer the first of ITS OWN stored options that is listed, unleased and passes every hard rule for its members today
    (figures refreshed first when they moved). One guard step per group; the switch stamps the offer-change flag (introduced
    statuses) and the GP8(b) refresh is queued. Options that cannot be judged leave the group as it is (no guess). Returns the ids
    re-offered. Proposed groups are re-formed by the run itself; Awaiting replacement / Leased / Dissolved are never touched."""
    from . import ai_queue, matching
    ph = ",".join("?" * len(INTRO_STAMP_STATUSES))
    held = rows(c.execute(f"""SELECT g.id FROM groups g WHERE g.apartment_id IS NULL AND g.status IN ({ph})
                              AND g.id IN (SELECT gm.group_id FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE e.is_test=?)
                              AND NOT EXISTS (SELECT 1 FROM offer_reconcile_work w WHERE w.group_id=g.id AND w.status='pending')
                              ORDER BY g.id""", (*INTRO_STAMP_STATUSES, int(is_test))))
    done = []
    for h in held:
        gid = int(h["id"])
        try:
            with dbm.retention_guard(c, label="held group re-offer"):
                g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
                if not g or g.get("apartment_id") is not None or g["status"] not in INTRO_STAMP_STATUSES or group_has_pending_work(c, gid):
                    continue
                units = _group_units(c, gid)
                chosen, invalid = None, []
                for o in rows(c.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,))):
                    a = row(c.execute("SELECT * FROM apartments WHERE id=?", (o["apartment_id"],)))
                    verdict, payload = _option_validity(c, a, o, units)
                    if verdict == "rewrite":
                        _rewrite_option(c, o, a, units, payload)
                    if verdict in ("ok", "rewrite"):
                        chosen = int(o["apartment_id"])
                        break
                    invalid.append(o["id"])
                if chosen is None:
                    for oid in invalid:   # every stored option judged invalid today: only judged-valid options are kept (as the re-check keeps them)
                        c.execute("DELETE FROM group_apartments WHERE id=?", (oid,))
                    # no stored option is usable (the hold pruned them, or they were lost since): the SAME member set is re-checked
                    # against today's available stock under every hard rule, and a valid apartment becomes a stored option of this
                    # group (GP6 ranking order; GP8: leased / unlisted never considered). Nothing about the members changes.
                    chosen = _restock_options(c, gid, units)
                if chosen is None:
                    continue
                matching._switch_current(c, gid, chosen)
                ai_queue.enqueue(c, [gid], refresh=True, commit=False)
                audit(c, staff, "group_reoffered", "group", gid, f"apartment {chosen} offered again from the group's own list after a stock change")
                done.append(gid)
        except OptionsNotJudgeable:
            log.info("group %s: held re-offer skipped, options could not be judged", gid)
            continue
    return done


def _restock_options(c, gid: int, units) -> int | None:
    """Succ3 (root, 10 Nov 2026): for a retained Introduced / Group confirmed group whose own option list holds nothing usable, find
    the apartments on today's available list (listed, unleased) where every member fits and the group passes every hard rule, store
    them as this group's ranked options (same figures the matching run would store; the caller has removed the invalid rows) and
    return the best one's id. Members, status, versions and clock untouched. None when nothing passes or the members cannot be judged."""
    from . import matching
    if units is None:
        raise OptionsNotJudgeable("the members' records needed to re-check available stock for this group could not be read")
    g = row(c.execute("SELECT id FROM groups WHERE id=?", (gid,)))
    if not g:
        return None
    ordered = matching.expand(units)
    have = {int(o["apartment_id"]) for o in rows(c.execute("SELECT apartment_id FROM group_apartments WHERE group_id=?", (gid,)))}
    found = []
    for a in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL ORDER BY available_from, address")):
        if int(a["id"]) in have:
            continue   # already judged by the caller
        try:
            apt = matching.apartment_from_row(a)
            if not all(matching._unit_fits_apartment(apt, u) for u in units):
                continue
            chk = rules.check_group(apt, ordered, exception_offers=matching.exception_offers_for(c, gid, a["id"]))
            if not chk.ok:
                continue
            sc, parts = matching.preference_score(apt, ordered, chk)
        except Exception as ex:  # noqa: BLE001
            log.warning("group %s apartment %s: stock re-check could not be made (%s)", gid, a["id"], type(ex).__name__)
            raise OptionsNotJudgeable("an available apartment could not be judged for this group; left as it is for retry") from None
        found.append((a, chk, sc, parts))
    if not found:
        return None
    found.sort(key=lambda t: (-t[2], t[1].total_weekly, int(t[0]["id"])))   # the run's deterministic order
    for i, (a, chk, sc, parts) in enumerate(found, start=1):
        c.execute("""INSERT INTO group_apartments(group_id, apartment_id, rank, rule_rank, rule_score, ai_reason, lease_start, lease_end, rate_kind, exit_window,
                     weekly_rent, extra_bed, total_weekly, allocation_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (gid, int(a["id"]), i, i, sc, None, chk.lease_start.isoformat(), chk.lease_end.isoformat(), chk.rate_kind, chk.exit_window,
                   chk.weekly_rent, chk.extra_bed, chk.total_weekly, matching._alloc_json(c, chk.allocation, ordered, score_parts=parts)))
        matching._store_exception_fields(c, gid, int(a["id"]), {"exception_ref": chk.exception_ref, "exception_total": chk.exception_total})
    return int(found[0][0]["id"])


def _clear_current_offer(c, gid: int, when: str, g: dict) -> None:
    """Held with no current offer: the pointer and the offer copies on the group row go NULL (never a placeholder apartment)."""
    stamp = when if g["status"] in INTRO_STAMP_STATUSES and not g.get("offer_changed_since_intro_at") else g.get("offer_changed_since_intro_at")
    c.execute("""UPDATE groups SET apartment_id=NULL, lease_start=NULL, lease_end=NULL, rate_kind=NULL, exit_window=NULL, weekly_rent=NULL, extra_bed=NULL,
                 total_weekly=NULL, allocation_json=NULL, rule_score=NULL, exception_ref=NULL, exception_total=NULL,
                 offer_changed_since_intro_at=?, updated_at=? WHERE id=?""", (stamp, when, gid))
    # the members stay (membership, GP13 versions, reviews and introduction history are untouched); only the per-member copies of
    # the offer that no longer exists (share, room, bed, arrangement) are cleared so nothing is shown as a CURRENT allocation
    c.execute("UPDATE group_members SET weekly_share=NULL, room=NULL, bed=NULL, arrangement=NULL WHERE group_id=?", (gid,))


def _note(w: dict, day: str, own_lost: bool, own_reason, outcome: str, remaining_ids: list[int], rank, c, removed, side_lost=None) -> str:
    """Plain staff note for this step. It names what actually happened to THIS transition's apartment and, when the group's current
    offer was removed by the same re-check although it is another apartment, names that apartment and its own reason - the move is
    never attributed to a still-valid apartment merely because its transition ran first (L11)."""
    address, cause = w["address"], w["cause"]
    if cause == "leased":
        head = f"{address} was leased on {day}."
    elif cause == "withdrawn":
        head = f"{address} was withdrawn from the available list on {day}."
    elif cause == "deleted":
        head = f"{address} was deleted from the apartment list on {day}."
    elif cause == "replaced":
        head = f"The apartment list was replaced on {day}; {address} is no longer listed."
    elif own_lost:
        head = f"{address} changed on {day} and no longer passes every rule for this group ({_plain(own_reason)})."
    else:
        head = f"{address} changed on {day} and still passes every rule for this group."
    if side_lost:
        cur_id, cur_reason = side_lost
        cur_addr = _address_of(c, cur_id)
        head += f" The group's current apartment, {cur_addr}, no longer passes every rule for this group ({_plain(cur_reason)}) and has been removed."
    others = [o for o, _ in removed if int(o["apartment_id"]) != int(w["apartment_id"]) and not (side_lost and int(o["apartment_id"]) == side_lost[0])]
    if others:
        head += f" {len(others)} other listed apartment(s) no longer pass every rule for this group and were removed."
    if outcome == "moved":
        new_addr = _address_of(c, remaining_ids[0])
        return head + f" This group now goes to its next best apartment that still passes every rule, {new_addr}."   # GP6 wording kept ("next best apartment")
    if outcome == "no_apartment":
        return head + " No other listed apartment passes every rule, so the group is held without an apartment. Staff can re-check the group's options when its requirements or the available apartments change."
    if own_lost and rank:
        return head + f" It was number {rank} on this group's list and has been removed."
    if own_lost:
        return head + " It is no longer on this group's list."
    return head


def _address_of(c, apartment_id: int) -> str:
    a = row(c.execute("SELECT address FROM apartments WHERE id=?", (apartment_id,)))
    return (a or {}).get("address") or f"apartment {apartment_id}"


_CODE_TAIL = re.compile(r",\s*((?:GP|[ABCHIPT])\d{1,2}(?:\([a-z]\))?)(?=\))")   # "(exact limit $1243, A16)" -> "(exact limit $1243, under A16)"


def _plain(reason) -> str:
    """T10: the rule's own reason in plain staff wording. Register codes the rule quotes become the requirement they stand for
    through the one staff rule-label map (staff_view.plain_text / RULE_LABELS); amounts, dates and the rest of the sentence are
    kept as the rule wrote them. (Succ3 follow-up, 10 Nov 2026: the note is rendered as-is by the staff page, so the code must
    not reach the stored text.)"""
    from . import staff_view
    s = str(reason or "no longer suitable").strip()
    s = staff_view.plain_text(_CODE_TAIL.sub(r", under \1", s), {}, {})
    return s[:240]


def _group_units(c, gid: int):
    """The group's members as rules.Unit objects, or None when they cannot be evaluated (then only stock availability is judged)."""
    from . import matching
    units = []
    for gm in rows(c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,))):
        e = row(c.execute("SELECT * FROM enquiries WHERE id=?", (gm["enquiry_id"],)))
        if not e or e.get("barred_at") or e.get("status") not in matching.ACTIVE_ENQUIRY_STATUSES:
            return None
        try:
            units.append(matching.unit_from_row(e, rows(c.execute("SELECT name, email, birth_country, birth_country_raw FROM party_members WHERE enquiry_id=? ORDER BY id", (e["id"],)))))
        except Exception as ex:  # noqa: BLE001
            log.warning("group %s: member record unreadable (%s)", gid, type(ex).__name__)
            return None
    return units or None


def _declined_by_a_member(c, gid: int, apartment_id: int, transition: dict | None = None) -> bool:
    """GP14 one lens for an apartment WITHOUT a cached option row: does any current member's active record exclude it (same decision
    as matching.unit_excludes_apartment / excluded_option_ids: apartment_rejections.excludes -> matching.exclusion_applies)?
    G-SL2: when the apartment ROW itself is gone, the decision uses immutable evidence only - each record's apartment key from its
    `recorded` event, the building key captured on the transition WITH its pending work (non-empty = the deleted unit's building;
    '' = the unit had none, so no building record applies; NULL = unknown -> not history). Allowances and current membership are the
    live ones; nothing is inferred from the address text."""
    from . import apartment_rejections
    if row(c.execute("SELECT 1 FROM apartments WHERE id=?", (int(apartment_id),))):
        return apartment_rejections.group_exclusion_reason(c, gid, apartment_id) is not None
    key = (transition or {}).get("transition_building_key", (transition or {}).get("building_key"))
    if key is None:
        key = ""   # legacy / cleared: the building question cannot be answered -> only an apartment-scope record can match
    return apartment_rejections.group_declined_absent_row(c, gid, int(apartment_id), str(key))


class OptionsNotJudgeable(RuntimeError):
    """An option's rule check could not be made (member projection unavailable or a rule error): the step is rolled back and left
    pending for retry - an unjudged option is never treated as valid and the group is never completed into a hold on a guess."""


def _option_validity(c, a: dict | None, o: dict, units):
    """('ok', None) | ('rewrite', check) | ('invalid', plain reason) for one stored option against today's stock and rules.
    Stock facts (deleted / leased / withdrawn) are judged without members; anything further needs the member projection and a
    clean rule check, otherwise OptionsNotJudgeable is raised (succ3: uncertainty is never a pass)."""
    from . import ai_queue, matching
    if not a:
        return "invalid", "no longer listed"
    if a.get("leased_at"):
        return "invalid", "was leased"
    if not a.get("available_from"):
        return "invalid", "was withdrawn"
    if units is None:
        raise OptionsNotJudgeable("the members' records needed to re-check this group's options could not be read; the step is left pending for retry")
    by_id = {str(u.id): u for u in units}
    # (replaying the stored occupant order is harmless legacy: rules.allocate_beds orders people canonically since 10 Nov 2026,
    # so the allocation depends on the member set only - never re-introduce order-dependent allocation)
    order = []
    for r in (pj(o.get("allocation_json"), {}) or {}).get("rooms") or []:
        for x in r.get("occupants") or []:
            uid = str(x.get("person_id") or "").split("m")[0]
            if uid in by_id and uid not in order:
                order.append(uid)
    ordered = [by_id[u] for u in order] + [u for u in units if str(u.id) not in order]
    try:
        apt = matching.apartment_from_row(a)
        if not all(matching._unit_fits_apartment(apt, u) for u in units):
            return "invalid", "the apartment no longer fits every member"
        chk = rules.check_group(apt, matching.expand(ordered),
                                exception_offers=matching.exception_offers_for(c, o["group_id"], a["id"]))
    except Exception as ex:  # noqa: BLE001
        log.warning("group %s apartment %s: rule check could not be made (%s); step left pending", o.get("group_id"), o.get("apartment_id"), type(ex).__name__)
        raise OptionsNotJudgeable("the rule check for one of this group's options could not be made; the step is left pending for retry") from None
    if not chk.ok:
        return "invalid", "; ".join(chk.reasons)
    if not ai_queue._offer_matches(o, chk):
        return "rewrite", chk
    return "ok", None


def _rewrite_option(c, o: dict, a: dict, units, chk) -> None:
    """The option still stands on different figures (rent, dates, allocation): store what the rules produce today."""
    from . import matching
    apt = matching.apartment_from_row(a)
    try:
        sc, parts = matching.preference_score(apt, units, chk)
    except Exception:  # noqa: BLE001
        sc, parts = o.get("rule_score"), None
    c.execute("""UPDATE group_apartments SET rule_score=?, lease_start=?, lease_end=?, rate_kind=?, exit_window=?, weekly_rent=?, extra_bed=?, total_weekly=?, allocation_json=?
                 WHERE id=?""", (sc, chk.lease_start.isoformat(), chk.lease_end.isoformat(), chk.rate_kind, chk.exit_window, chk.weekly_rent, chk.extra_bed, chk.total_weekly,
                                 matching._alloc_json(c, chk.allocation, units, score_parts=parts), o["id"]))
    matching._store_exception_fields(c, o["group_id"], o["apartment_id"],
                                    {"exception_ref": chk.exception_ref, "exception_total": chk.exception_total})
    g = row(c.execute("SELECT apartment_id FROM groups WHERE id=?", (o["group_id"],)))
    if g and g.get("apartment_id") is not None and int(g["apartment_id"]) == int(o["apartment_id"]):
        matching._switch_current(c, int(o["group_id"]), int(o["apartment_id"]))   # same pointer, refreshed copies (stamp handled by the caller)


# ------------------------------------------------------------------------------------------------------------ upgrade bootstrap
def bootstrap(c) -> dict:
    """Idempotent one-time carry-over for databases upgraded from the five-fix shape (marker row in `settings`):
    (1) disjoint revision namespace: the transition id sequence starts above every legacy alert revision of this event kind;
    (2) every apartment currently leased gets a transition row (cause leased, its generation); groups whose lease follow-up was
        still outstanding get a pending work row carrying the legacy event id, so the step alerts only if neither id exists;
    (3) groups left pointing at a leased apartment with no other option (the old 'no_apartment' outcome) go to the NULL pointer.
    Baseline databases (no alerts, no transitions) only get the marker. Runs outside any guard, commits itself."""
    done = row(c.execute("SELECT value FROM settings WHERE key=?", (BOOTSTRAP_KEY,)))
    if done:
        return {"bootstrapped": False}
    legacy = rows(c.execute("SELECT event_id, record_id FROM internal_alert_outbox WHERE kind=?", (EV_APARTMENT_UNAVAILABLE,)))
    floor = 0
    for r in legacy:
        try:
            floor = max(floor, int(str(r["event_id"]).rsplit(":", 1)[1]))
        except (ValueError, IndexError):
            continue
    if floor and not row(c.execute("SELECT 1 FROM stock_transitions LIMIT 1")):
        _set_sequence_floor(c, "stock_transitions", floor)
    carried = 0
    when = now_iso()
    for a in rows(c.execute("SELECT id, address, leased_at, leased_group_id, lease_generation FROM apartments WHERE leased_at IS NOT NULL")):
        if latest_transition(c, a["id"]):
            continue
        c.execute("INSERT INTO stock_transitions(apartment_id, address, cause, generation, changed_fields, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
                  (a["id"], a["address"], "leased", int(a.get("lease_generation") or 0), None, when, "upgrade"))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        ph = ",".join("?" * len(LIVE_EXCLUDED))
        outstanding = rows(c.execute(f"""SELECT DISTINCT g.id, CASE WHEN g.apartment_id=? THEN 'current' ELSE 'alternative' END AS role FROM groups g
                                         WHERE g.status NOT IN ({ph}) AND (? IS NULL OR g.id<>?)
                                           AND (g.id IN (SELECT group_id FROM group_apartments WHERE apartment_id=?)
                                                OR (g.apartment_id=? AND (g.reranked_at IS NULL OR g.reranked_at < ?)))""",
                                     (a["id"], *LIVE_EXCLUDED, a.get("leased_group_id"), a.get("leased_group_id"), a["id"], a["id"], str(a["leased_at"]))))
        for g in outstanding:
            legacy_id = f"{EV_APARTMENT_UNAVAILABLE}:group:{int(g['id'])}:{int(a['id']) * 10 ** 6 + int(a.get('lease_generation') or 0) % 10 ** 6}"
            c.execute("INSERT OR IGNORE INTO offer_reconcile_work(transition_id, group_id, role, status, legacy_event_id, created_at) VALUES (?,?,?,'pending',?,?)",
                      (tid, int(g["id"]), g["role"], legacy_id, when))
            carried += 1
        # the old final outcome "re-ranked for this lease and left without an apartment": uniform NULL pointer now
        for g in rows(c.execute(f"""SELECT id, status, offer_changed_since_intro_at FROM groups g WHERE g.apartment_id=? AND g.status NOT IN ({ph}) AND (? IS NULL OR g.id<>?)
                                    AND g.reranked_at IS NOT NULL AND g.reranked_at >= ? AND NOT EXISTS (SELECT 1 FROM group_apartments WHERE group_id=g.id)""",
                                (a["id"], *LIVE_EXCLUDED, a.get("leased_group_id"), a.get("leased_group_id"), str(a["leased_at"])))):
            _clear_current_offer(c, int(g["id"]), when, g)
    c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (BOOTSTRAP_KEY, when))
    c.commit()
    if carried or floor:
        log.info("stock lifecycle bootstrap: %s outstanding lease follow-up(s) carried over; legacy revision floor %s", carried, floor)
    return {"bootstrapped": True, "carried": carried, "floor": floor}


def _set_sequence_floor(c, table: str, floor: int) -> None:
    if getattr(c, "backend", "sqlite") == "postgres":
        c.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), ?, true)", (int(floor),))
    else:
        if c.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (int(floor), table)).rowcount == 0:
            c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, int(floor)))


def summarize_for_audit(staff: str | None, c, action: str, apartment_id: int, rec: dict, extra: str = "") -> None:
    audit(c, staff, action, "apartment", apartment_id, f"{rec.get('address')}; transition {rec.get('transition_id')}; {len(rec.get('group_ids') or [])} group(s) to re-check{extra}")
