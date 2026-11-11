"""H7/H14 grounded AI review of a HomeBot shortlist (server/homebot_review.py) behind the two ai_queue hooks the H2 shortlist
module calls. Temporary SQLite per test, synthetic data, no provider: every model reply is a double installed on ai._ask.
Boundaries under test: hooks are idempotent and never mark anything ready; the review cannot upgrade an open point (travel,
essential note, unresolved dates) into verified suitability; a basis that moves before, during or after the call makes the
review STALE, never 'reviewed'; failure sinks hold closed sentences only; retention removes the rows."""
import json

import pytest

from server import ai, ai_queue, homebot_match as hm, homebot_review as hr, homebot_shortlist as hs
from server.db import row, rows
from tests.test_homebot import client  # noqa: F401  (fixture)
from tests.test_homebot_shortlist import CUR, current, inventory, run_all, submit

MARKER = "ZQX-PROVIDER-WORDS-7731"


@pytest.fixture
def on(client, monkeypatch):
    monkeypatch.setenv(hs.GATE_ENV, "1")
    assert hs.enabled() and hr.table_present(client.db)
    return client


def _reviews(c, sid=None):
    return rows(c.db.execute("SELECT * FROM homebot_reviews" + (" WHERE shortlist_id=?" if sid else "") + " ORDER BY id", (sid,) if sid else ()))


def _good_reply(pk, verdict="approve", note_for=None):
    """A grounded reply: one finding citing real fact ids, one note per option; `note_for` overrides a note by option ref."""
    notes = []
    for o in pk["options"]:
        txt = (note_for or {}).get(o["ref"], "Consistent with the recorded offer and requirements; open points stay with staff.")
        notes.append({"ref": o["ref"], "agrees": True, "note": txt})
    return json.dumps({"verdict": verdict, "rationale": "The listed options match the recorded requirements; nothing conflicts.",
                       "findings": [{"fact_ids": [pk["fact_ids"][0], pk["options"][0]["ref"] + ".offer"], "note": "The offer sits inside the recorded budget."}],
                       "option_notes": notes})


def _ask_double(make):
    """ai._ask replacement: parses the packet out of the prompt and answers with make(packet)."""
    def _ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        return make(pk)
    return _ask


def _prepared(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sl = current(on, eid)
    assert sl and sl["review_enqueued_at"], "the shortlist module handed the shortlist to the review hook"
    return eid, sl


# hooks -----------------------------------------------------------------------------------------------------------------------
def test_hook_records_one_pending_row_per_shortlist_version_and_is_idempotent(on):
    eid, sl = _prepared(on)
    rs = _reviews(on, sl["id"])
    assert len(rs) == 1 and rs[0]["state"] == hr.PENDING and rs[0]["version"] == hr.shortlist_version(sl) and rs[0]["attempts"] == 0
    again = ai_queue.enqueue_homebot_review(on.db, sl["id"], commit=True)
    assert again["outcome"] == "already_queued" and len(_reviews(on, sl["id"])) == 1
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["eligible"] is True and st["reviewed"] is None and st["state"] == hr.PENDING
    out = hs.current_shortlist(on.db, eid, CUR)
    assert out["ready"] is False and out["review"]["state"] == "queued"      # the shortlist module's own words until a review exists


def test_review_state_asked_from_inside_the_freshness_check_answers_not_reviewed_instead_of_recursing(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1
    seen = {}

    def probe(c, s):            # stands in for today's _stale -> homebot.get -> matching_note -> _review_state path
        seen["nested"] = ai_queue.homebot_review_state(c, s["id"])
        return None
    monkeypatch.setattr(hs, "_stale", probe)
    monkeypatch.delattr(hs, "freshness", raising=False)
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"]["verdict"] == hr.APPROVE                      # the outer call still answers from the stored review
    assert seen["nested"]["reviewed"] is None and seen["nested"]["state"] == "checking"
    # and the real path (enquiry read inside the freshness check) terminates
    from server import homebot
    assert homebot.get(on.db, eid)["matching"]["review_state"] == "reviewed"


def test_hook_on_a_missing_shortlist_raises_so_the_callers_step_fails_visibly(on):
    with pytest.raises(LookupError):
        ai_queue.enqueue_homebot_review(on.db, 999999)


# the review -----------------------------------------------------------------------------------------------------------------
def test_packet_carries_cited_facts_open_points_and_no_names(on):
    eid, sl = _prepared(on)
    pk = hr.packet(on.db, sl)
    assert pk["options"] and all(o["ref"] == f"O{i + 1}" for i, o in enumerate(pk["options"]))
    assert "R.whole_apartment_max" in pk["fact_ids"] and "O1.offer" in pk["fact_ids"] and "O1.open_points" in pk["fact_ids"]
    assert "name" not in json.dumps(pk["requirements"]).lower() or all("name" not in (p or {}) for k, p in pk["requirements"].items() if k.startswith("R.person"))
    for o in pk["options"]:
        for op in o["open_points"]:
            assert op["plain"] == hm.PLAIN[op["code"]]                        # the rules' plain words, never codes alone


def test_valid_reply_is_stored_bound_to_the_version_and_read_back_without_ready(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    out = hr.process_pending(on.db)
    assert out == {"reviewed": 1, "invalid": 0, "failed": 0, "stale": 0, "calls": 1, "guard_failures": 0, "not_claimed": 0, "lost_claim": 0}
    r = _reviews(on, sl["id"])[0]
    assert r["state"] == hr.REVIEWED and r["verdict"] == hr.APPROVE and r["error"] is None and r["reviewed_at"] and r["contract"] == hr.REVIEW_CONTRACT
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"]["verdict"] == hr.APPROVE and "ready" not in st["reviewed"] and "ready" not in st
    assert set(st["reviewed"]["ready_conditions"]) == set(hr.READY_CONDITIONS)
    assert st["reviewed"]["ready_conditions"]["review_state_reviewed"] and st["reviewed"]["ready_conditions"]["fresh_now"]
    # open points come from the rules result, never from the model
    assert st["reviewed"]["open_points"] == hr._open_points(current(on, eid))
    cur = hs.current_shortlist(on.db, eid, CUR)
    assert cur["ready"] is False and cur["review"]["state"] == "reviewed" and cur["review"]["detail"]["verdict"] == hr.APPROVE


@pytest.mark.parametrize("bad", [
    lambda pk: "no json here",
    lambda pk: json.dumps({"verdict": "approve"}),
    lambda pk: json.dumps({"verdict": "ready", "rationale": "x", "findings": [], "option_notes": []}),
    lambda pk: json.dumps({"verdict": "approve", "rationale": "x", "findings": [{"fact_ids": ["R.not_a_fact"], "note": "n"}],
                           "option_notes": [{"ref": o["ref"], "agrees": True, "note": ""} for o in pk["options"]]}),
    lambda pk: json.dumps({"verdict": "approve", "rationale": "x", "findings": [], "option_notes": [],
                           "class": "suitable"}),                                   # no field may carry a class
    lambda pk: json.dumps({"verdict": "approve", "rationale": "x", "findings": [], "option_notes": []}),   # a note per option is required
    lambda pk: json.dumps({"verdict": "approve", "rationale": "She will like it.", "findings": [],
                           "option_notes": [{"ref": o["ref"], "agrees": True, "note": ""} for o in pk["options"]]}),
])
def test_unusable_replies_are_invalid_after_bounded_attempts_with_a_closed_sentence(on, monkeypatch, bad):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(bad))
    out = hr.process_pending(on.db, attempts=2)
    assert out["reviewed"] == 0 and _reviews(on, sl["id"])[0]["state"] == hr.PENDING and _reviews(on, sl["id"])[0]["attempts"] == 1
    assert hr.due_count(on.db) == 0 and hr.process_pending(on.db, attempts=2)["calls"] == 0      # not retried before RETRY_AFTER_S
    monkeypatch.setattr(hr, "RETRY_AFTER_S", 0)
    out = hr.process_pending(on.db, attempts=2)
    r = _reviews(on, sl["id"])[0]
    assert out["invalid"] == 1 and r["state"] == hr.INVALID and r["error"] == hr.FAILURE_TEXT["unusable"] and r["review_json"] is None
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"] is None and st["plain"] == hr.FAILURE_TEXT["unusable"]
    assert hs.current_shortlist(on.db, eid, CUR)["ready"] is False


def _with_open_point(sl, code, on):
    """Plant an open point on the first listed option of the stored result (rules-side), as the matcher would have recorded it."""
    res = json.loads(sl["result_json"])
    res["listed"][0]["confirmation_reasons"] = list(res["listed"][0].get("confirmation_reasons") or []) + [code]
    on.db.execute("UPDATE homebot_shortlists SET result_json=? WHERE id=?", (json.dumps(res), sl["id"]))
    on.db.commit()
    sl2 = current(on, sl["enquiry_id"])
    # the stored result changed, so the pending row is re-bound to the new version by a fresh hand-over
    ai_queue.enqueue_homebot_review(on.db, sl2["id"], commit=True)
    return sl2


@pytest.mark.parametrize("code, note", [
    ("TRAVEL_NEEDS_CONFIRMATION", "The journey to campus is short and well within the stated travel time."),
    ("TRAVEL_NEEDS_CONFIRMATION", "Travel is fine for everyone."),
    ("ESSENTIAL_NOTE_REVIEW", "The essential step-free access requirement is met by this building."),
    ("DATE_SEARCH_UNRESOLVED", "The dates and cost are confirmed inside the party's accepted ranges."),
    ("BEDDING_NEEDS_CONFIRMATION", "The sleeping arrangement works for the party."),
])
def test_a_reply_that_upgrades_an_open_point_into_verified_suitability_is_invalid(on, monkeypatch, code, note):
    eid, sl = _prepared(on)
    sl = _with_open_point(sl, code, on)
    pk = hr.packet(on.db, sl)
    with pytest.raises(hr.UnusableReview) as ei:
        hr.parse_review(_good_reply(pk, note_for={"O1": note}), pk)
    assert code in str(ei.value)


def test_the_upgrade_guard_is_scoped_to_the_option_that_carries_the_open_point(on):
    eid, sl = _prepared(on)
    sl = _with_open_point(sl, "ESSENTIAL_NOTE_REVIEW", on)
    pk = hr.packet(on.db, sl)
    note = "The essential step-free access requirement is met by this building."
    assert [o["code"] for o in pk["options"][1]["open_points"]] == ["TRAVEL_NEEDS_CONFIRMATION"]   # O2 has no essential-note point
    with pytest.raises(hr.UnusableReview):
        hr.parse_review(_good_reply(pk, note_for={"O1": note}), pk)
    hr.parse_review(_good_reply(pk, note_for={"O2": note}), pk)       # about O2 it is an ordinary (if bold) remark, not an upgrade


@pytest.mark.parametrize("code, note", [
    ("TRAVEL_NEEDS_CONFIRMATION", "Travel time is not measured yet and still needs confirming with the party."),
    ("TRAVEL_NEEDS_CONFIRMATION", "Whether the journey is within the stated travel time is unknown; confirm before offering."),
    ("ESSENTIAL_NOTE_REVIEW", "The notes state an essential need; staff must read and confirm it before this counts as suitable."),
    ("DATE_SEARCH_UNRESOLVED", "Dates and cost are unresolved for this apartment and need a manual check."),
])
def test_a_reply_that_keeps_the_open_point_open_is_valid(on, monkeypatch, code, note):
    eid, sl = _prepared(on)
    sl = _with_open_point(sl, code, on)
    pk = hr.packet(on.db, sl)
    rv = hr.parse_review(_good_reply(pk, note_for={"O1": note}), pk)
    assert rv["option_notes"][0]["note"] == note


def test_open_points_in_the_read_state_come_from_the_rules_even_when_the_model_agrees(on, monkeypatch):
    eid, sl = _prepared(on)
    sl = _with_open_point(sl, "TRAVEL_NEEDS_CONFIRMATION", on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk, note_for={"O1": "Travel still needs confirming."})))
    assert hr.process_pending(on.db)["reviewed"] == 1
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert any(p["code"] == "TRAVEL_NEEDS_CONFIRMATION" for p in st["reviewed"]["open_points"])
    assert st["reviewed"]["ready_conditions"]["no_open_points"] is False and st["reviewed"]["all_ready_conditions_hold"] is False


def test_hold_verdict_is_stored_and_read_as_hold_for_staff(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk, verdict="hold")))
    assert hr.process_pending(on.db)["reviewed"] == 1
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"]["verdict"] == hr.HOLD and "hold for staff" in st["reviewed"]["plain"] and st["reviewed"]["ready_conditions"]["verdict_approve"] is False


# freshness -------------------------------------------------------------------------------------------------------------------
def test_basis_moved_before_the_call_marks_stale_without_a_model_call(on, monkeypatch):
    eid, sl = _prepared(on)
    calls = {"n": 0}

    def never(*a, **k):
        calls["n"] += 1
        raise AssertionError("no model call for a stale shortlist")
    monkeypatch.setattr(ai, "_ask", never)
    monkeypatch.setattr(hs, "_stale", lambda c, s: "Out of date: the apartment list changed after these options were prepared; a fresh run is queued.")
    out = hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert out["stale"] == 1 and calls["n"] == 0 and r["state"] == hr.STALE and r["error"] == hr.FAILURE_TEXT["stale"]
    assert ai_queue.homebot_review_state(on.db, sl["id"])["reviewed"] is None


def test_basis_moved_while_the_model_was_writing_discards_the_result(on, monkeypatch):
    eid, sl = _prepared(on)

    def ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        # an apartment edit lands while the model is writing: the inventory snapshot no longer matches
        on.db.execute("UPDATE apartments SET rent_12m = rent_12m + 1 WHERE id=1"); on.db.commit()
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    out = hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert out["stale"] == 1 and out["reviewed"] == 0 and r["state"] == hr.STALE and r["review_json"] is None
    assert ai_queue.homebot_review_state(on.db, sl["id"])["reviewed"] is None


def test_a_stored_review_is_not_reported_once_the_basis_moves_after_it(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1
    assert ai_queue.homebot_review_state(on.db, sl["id"])["reviewed"]
    on.db.execute("UPDATE apartments SET rent_12m = rent_12m + 1 WHERE id=2"); on.db.commit()   # inventory changed on another path
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"] is None and st["state"] == hr.STALE and st["plain"].startswith("Out of date")
    assert hs.current_shortlist(on.db, eid, CUR)["ready"] is False


def test_superseded_shortlist_is_never_reviewed_and_the_newer_one_gets_its_own_row(on, monkeypatch):
    eid, sl = _prepared(on)
    on.db.execute("UPDATE apartments SET rent_12m = rent_12m + 10 WHERE id=1"); on.db.commit()
    hs.on_inventory_changed(on.db, "test edit", [1])
    run_all(on)
    sl2 = current(on, eid)
    assert sl2["id"] != sl["id"]
    assert ai_queue.homebot_review_state(on.db, sl["id"])["eligible"] is False
    assert [r["shortlist_id"] for r in _reviews(on)].count(sl2["id"]) == 1


def test_without_any_freshness_check_nothing_is_ever_reported_as_reviewed(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    monkeypatch.delattr(hs, "_stale")
    monkeypatch.delattr(hs, "freshness", raising=False)
    out = hr.process_pending(on.db)
    assert out["stale"] == 1 and out["reviewed"] == 0
    assert ai_queue.homebot_review_state(on.db, sl["id"])["reviewed"] is None


def test_public_freshness_name_is_preferred_when_the_shortlist_module_offers_it(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(hs, "freshness", lambda c, s: "Out of date: public check", raising=False)
    assert hr.freshness(on.db, sl) == "Out of date: public check"


# failure sinks ---------------------------------------------------------------------------------------------------------------
def test_provider_words_reach_neither_the_row_nor_the_log(on, monkeypatch, caplog):
    eid, sl = _prepared(on)

    def down(*a, **k):
        raise ai.AIUnavailable("Error code: 401 - {'type': 'authentication_error', 'message': '" + MARKER + "'}")
    monkeypatch.setattr(ai, "_ask", down)
    caplog.set_level("DEBUG")
    out = hr.process_pending(on.db, attempts=1)
    r = _reviews(on, sl["id"])[0]
    assert out["failed"] == 1 and r["state"] == hr.FAILED and r["error"] == hr.FAILURE_TEXT["provider"]
    assert MARKER not in json.dumps(dict(r)) and MARKER not in caplog.text and "authentication_error" not in caplog.text
    assert ai_queue.homebot_review_state(on.db, sl["id"])["plain"] == hr.FAILURE_TEXT["provider"]


def test_cut_off_reply_is_terminal_with_the_size_limit_sentence(on, monkeypatch):
    eid, sl = _prepared(on)

    def cut(*a, **k):
        raise ai.AIOutputTruncated("cut " + MARKER, max_tokens=100)
    monkeypatch.setattr(ai, "_ask", cut)
    out = hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert out["failed"] == 1 and out["calls"] == 2 and r["state"] == hr.FAILED and r["error"] == hr.FAILURE_TEXT["size_limit"] and MARKER not in (r["error"] or "")


def test_unexpected_exception_stores_the_fixed_sentence_only(on, monkeypatch, caplog):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(KeyError(MARKER)))
    caplog.set_level("DEBUG")
    out = hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert out["failed"] == 1 and r["state"] == hr.FAILED and r["error"] == hr.FAILURE_TEXT["unexpected"] and MARKER not in caplog.text


def test_every_persisted_error_is_a_closed_sentence_and_states_are_closed(on):
    assert hr.STATES == {"pending", "running", "reviewed", "invalid", "failed", "stale"} and hr.VERDICTS == {"approve", "hold"}
    for k, t in hr.FAILURE_TEXT.items():
        assert t in hr.CLOSED_FAILURE_SENTENCES and t.endswith(".") and "{" not in t and "Error code" not in t


# automatic consumer (AI queue worker) --------------------------------------------------------------------------------------
def test_worker_iteration_consumes_due_reviews_only_when_the_homebot_gate_is_on(on, monkeypatch):
    eid, sl = _prepared(on)
    calls = {"n": 0}

    def ask(prompt, model=None, max_tokens=1500, system=""):
        calls["n"] += 1
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    monkeypatch.setenv(hs.GATE_ENV, "0")
    out = ai_queue._iteration(on.db)["homebot"]           # MateBot has no work; HomeBot gate off: nothing read, no call
    assert out["skipped"] == "homebot_gate_off" and calls["n"] == 0 and _reviews(on, sl["id"])[0]["state"] == hr.PENDING
    monkeypatch.setenv(hs.GATE_ENV, "1")
    out = ai_queue._iteration(on.db)["homebot"]
    assert out["skipped"] is None and out["batches"] == 1 and out["reviewed"] == 1 and calls["n"] == 1
    assert ai_queue.homebot_review_state(on.db, sl["id"])["summary"]["approved"] is True
    assert ai_queue._iteration(on.db)["homebot"]["batches"] == 0     # nothing due: no second call


def test_the_worker_serves_both_queues_in_bounded_slices_so_homebot_never_waits_for_matebot_to_drain(on, monkeypatch):
    order, kicked = [], []
    monkeypatch.setattr(ai_queue, "pending_count", lambda c: 1)                       # MateBot work never runs out
    monkeypatch.setattr(ai_queue, "briefs_pending_count", lambda c: 0)                # D2 (9 Nov 2026): the second remaining-work signal; the DB here is a bare object
    monkeypatch.setattr(ai_queue, "notes_pending_count", lambda c: 0)                 # T4c (9 Nov 2026): the third remaining-work signal (unread member notes)
    monkeypatch.setattr(ai_queue, "process_pending", lambda c, **kw: (order.append("M") or {"groups": 1}))   # T4c: the worker passes notes_limit
    monkeypatch.setattr(ai_queue, "_consume_homebot_reviews", lambda c: (order.append("H") or {"reviewed": 0}))
    monkeypatch.setattr(ai_queue, "kick", lambda: kicked.append(1))
    out = ai_queue._iteration(object())
    assert order[0] == "H" and order[-1] == "H" and order.count("M") == ai_queue.MATEBOT_BATCHES_PER_WAKE
    assert out["more"] is True and kicked == [1]                                       # the rest is served on the next wake, HomeBot first again
    order.clear(); kicked.clear()
    monkeypatch.setattr(ai_queue, "pending_count", lambda c: 0)
    out = ai_queue._iteration(object())
    assert order == ["H"] and out["matebot_batches"] == 0 and out["more"] is False and not kicked


def test_ai_queue_off_means_no_worker_and_therefore_no_automatic_review(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setenv("MMAI_AI_QUEUE", "0")
    monkeypatch.setattr(ai_queue, "_thread", None)
    assert ai_queue.start(on.db) is None and _reviews(on, sl["id"])[0]["state"] == hr.PENDING


def test_the_provider_call_runs_outside_any_retention_guard_and_writes_run_inside_one(on, monkeypatch):
    from server import db
    eid, sl = _prepared(on)
    seen = {"guarded_at_call": None, "labels": []}

    def ask(prompt, model=None, max_tokens=1500, system=""):
        seen["guarded_at_call"] = db.guarded(on.db)
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    hook = lambda ev, c, label: seen["labels"].append((ev, label)) if label.startswith("homebot review") else None
    db.GUARD_EVENTS.append(hook)
    try:
        assert hr.process_pending(on.db)["reviewed"] == 1
    finally:
        db.GUARD_EVENTS.remove(hook)
    assert seen["guarded_at_call"] is False
    assert [l for ev, l in seen["labels"] if ev == "committed"] == ["homebot review capture", "homebot review apply"]


def test_a_busy_database_leaves_the_row_pending_with_the_attempt_not_counted(on, monkeypatch):
    from server import db
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call when capture fails")))

    class Busy(db.retention_guard):
        def __enter__(self):
            raise db.RetentionLockTimeout("database busy (test)")
    monkeypatch.setattr(db, "retention_guard", Busy)
    out = hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert out["guard_failures"] == 1 and out["calls"] == 0 and r["state"] == hr.PENDING and r["attempts"] == 0
    assert hr.consume(on.db)["batches"] == 1               # the consumer stops after a guard failure instead of spinning


def test_consume_is_bounded_per_wake(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    for i in range(2, 5):      # three more pending rows for the same shortlist under synthetic versions
        on.db.execute(f"INSERT INTO homebot_reviews(shortlist_id, enquiry_id, state, version, attempts, created_at, updated_at) VALUES (?,?,?,?,0,?,?)",
                      (sl["id"], eid, hr.PENDING, f"v{i}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    on.db.commit()
    out = hr.consume(on.db, max_batches=1, batch=2)
    assert out["batches"] == 1 and out["reviewed"] + out["stale"] == 2 and hr.due_count(on.db) == 2
    out = hr.consume(on.db, max_batches=5, batch=2)
    assert out["batches"] == 1 and hr.due_count(on.db) == 0
    assert sum(1 for r in _reviews(on, sl["id"]) if r["state"] == hr.REVIEWED) == 1   # only the row bound to the real version


def test_summary_for_the_staff_screen_exists_in_every_state_and_never_hides_open_points(on, monkeypatch):
    eid, sl = _prepared(on)
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["summary"] == {"approved": False, "fresh": True, "has_listed_options": True, "open_points": hr._open_points(sl), "all_ready_conditions_hold": False}
    sl = _with_open_point(sl, "ESSENTIAL_NOTE_REVIEW", on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["summary"]["approved"] is True and st["summary"]["fresh"] is True and st["summary"]["all_ready_conditions_hold"] is False
    assert "ESSENTIAL_NOTE_REVIEW" in [p["code"] for p in st["summary"]["open_points"]]
    on.db.execute("UPDATE apartments SET rent_12m = rent_12m + 1 WHERE id=2"); on.db.commit()
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["summary"]["approved"] is False and st["summary"]["fresh"] is False


def test_an_unresolved_only_result_has_open_points_and_no_listed_option(on, monkeypatch):
    eid, sl = _prepared(on)
    res = json.loads(sl["result_json"])
    res["unresolved"] = res["listed"]; res["listed"] = []; res["fourth_option"] = None
    for x in res["unresolved"]:
        x["date_search_unresolved"] = True
    on.db.execute("UPDATE homebot_shortlists SET result_json=? WHERE id=?", (json.dumps(res), sl["id"])); on.db.commit()
    sl = current(on, eid)
    pts = hr._open_points(sl)
    assert pts and all(p["code"] in hm.PLAIN for p in pts) and any(p["code"] == "DATE_SEARCH_UNRESOLVED" for p in pts)
    assert hr._has_listed(sl) is False
    ai_queue.enqueue_homebot_review(on.db, sl["id"], commit=True)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["summary"]["approved"] is True and st["summary"]["has_listed_options"] is False and st["summary"]["all_ready_conditions_hold"] is False
    assert st["reviewed"]["ready_conditions"]["has_listed_options"] is False and st["reviewed"]["ready_conditions"]["no_open_points"] is False


# claim / owner fence --------------------------------------------------------------------------------------------------------
def test_second_consumer_cannot_claim_an_in_flight_row_and_a_running_row_reads_as_in_progress(on, monkeypatch):
    eid, sl = _prepared(on)
    calls = []

    def ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        calls.append(1)
        if len(calls) == 1:
            r = _reviews(on, sl["id"])[0]
            assert r["state"] == hr.RUNNING and r["owner"] and r["claimed_at"]
            st = ai_queue.homebot_review_state(on.db, sl["id"])
            assert st["reviewed"] is None and st["state"] == hr.RUNNING and st["plain"] == hr.RUNNING_TEXT
            assert hr.process_pending(on.db) == {"reviewed": 0, "invalid": 0, "failed": 0, "stale": 0, "calls": 0, "guard_failures": 0, "not_claimed": 0, "lost_claim": 0}
            assert hr.due_count(on.db) == 0        # a fresh claim is not due for anyone else
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    assert hr.process_pending(on.db)["reviewed"] == 1 and len(calls) == 1
    r = _reviews(on, sl["id"])[0]
    assert r["owner"] is None and r["claimed_at"] is None


def test_an_expired_claim_is_taken_over_and_the_late_result_of_the_first_consumer_is_dropped(on, monkeypatch):
    eid, sl = _prepared(on)
    calls = []

    def ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        calls.append(1)
        if len(calls) == 1:
            # the first consumer stalls past its lease: age the claim, then a second consumer takes the row over and finishes
            on.db.execute("UPDATE homebot_reviews SET claimed_at='2020-01-01T00:00:00+00:00' WHERE shortlist_id=?", (sl["id"],)); on.db.commit()
            assert hr.due_count(on.db) == 1
            out = hr.process_pending(on.db)
            assert out["reviewed"] == 1 and len(calls) == 2
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    out = hr.process_pending(on.db)
    assert out["lost_claim"] == 1 and out["reviewed"] == 0
    rs = _reviews(on, sl["id"])
    assert len(rs) == 1 and rs[0]["state"] == hr.REVIEWED and rs[0]["attempts"] == 2   # one stored review, written by the second consumer only


def test_closed_or_leased_enquiry_is_never_reviewed_and_a_stored_review_is_not_reported_for_it(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1
    on.db.execute("UPDATE homebot_enquiries SET status='Closed' WHERE id=?", (eid,)); on.db.commit()
    st = ai_queue.homebot_review_state(on.db, sl["id"])
    assert st["reviewed"] is None and st["plain"] == hr.FAILURE_TEXT["closed"] and st["summary"]["approved"] is False


# retention -------------------------------------------------------------------------------------------------------------------
def test_deleting_the_enquiry_removes_its_review_rows(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(ai, "_ask", _ask_double(lambda pk: _good_reply(pk)))
    assert hr.process_pending(on.db)["reviewed"] == 1 and _reviews(on)
    hs.forget(on.db, [eid]); on.db.commit()
    assert _reviews(on) == []


def test_schema_proposal_matches_the_table_the_module_uses(on):
    import server.db as sdb
    if sdb.is_postgres():
        schema = rows(on.db.execute("""
            SELECT column_name AS name, data_type AS type, is_nullable
            FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='homebot_reviews'
        """))
        actual = {r["name"]: (r["type"].upper(), r["is_nullable"] == "NO") for r in schema}
    else:
        schema = rows(on.db.execute("PRAGMA table_info(homebot_reviews)"))
        # SQLite reports notnull=0 for an INTEGER PRIMARY KEY even though its rowid cannot be null.
        actual = {r["name"]: (r["type"].upper(), bool(r["notnull"] or r["pk"])) for r in schema}
    cols = set(actual)
    assert {"shortlist_id", "enquiry_id", "state", "version", "attempts", "verdict", "review_json", "error", "model", "rule_version", "contract",
            "created_at", "updated_at", "reviewed_at"} <= cols
    assert "homebot_reviews" in hr.PROPOSED_DDL and "UNIQUE(shortlist_id, version)" in hr.PROPOSED_DDL
    import re, sqlite3
    probe = sqlite3.connect(":memory:")
    probe.executescript("CREATE TABLE homebot_shortlists(id INTEGER PRIMARY KEY); CREATE TABLE homebot_enquiries(id INTEGER PRIMARY KEY);")
    probe.executescript(hr.PROPOSED_DDL)
    proposed = {r[1]: (r[2].upper(), bool(r[3] or r[5])) for r in probe.execute("PRAGMA table_info(homebot_reviews)")}
    probe.close()
    assert proposed == actual, "the schema the reviewer lands must equal PROPOSED_DDL column for column"


# lease, completion time, configuration failures (H7c, root worker probe) ------------------------------------------------------
def test_a_healthy_slow_call_keeps_its_lease_through_renewal_and_is_sent_once(on, monkeypatch):
    import threading
    eid, sl = _prepared(on)
    # test TTL 2 s (not 1): db.now_iso() has second resolution, so with TTL 1 a lease renewed milliseconds ago can already
    # read as expired (claimed_at <= now-1s) right after a second boundary - a fixture artefact below the clock's resolution,
    # not lease behaviour. The call still outlasts the TTL (2.3 s > 2 s) and renewal must still prevent any second claim.
    monkeypatch.setattr(hr, "CLAIM_TTL_S", 2)
    monkeypatch.setattr(hr, "HEARTBEAT_SEC", 0.05)
    calls = []

    def ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        calls.append(1)
        if len(calls) == 1:
            threading.Event().wait(2.3)                     # longer than the (test) TTL; the lease is renewed meanwhile
            assert hr.due_count(on.db) == 0, "a renewed lease is not claimable"
            assert hr.process_pending(on.db)["not_claimed"] == 0 and len(calls) == 1
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    out = hr.process_pending(on.db)
    assert out["reviewed"] == 1 and out["lost_claim"] == 0 and len(calls) == 1
    r = _reviews(on, sl["id"])[0]
    assert r["state"] == hr.REVIEWED and r["owner"] is None and r["claimed_at"] is None


def test_a_renewal_that_finds_the_claim_gone_makes_the_consumer_drop_its_result(on, monkeypatch):
    eid, sl = _prepared(on)
    monkeypatch.setattr(hr, "HEARTBEAT_SEC", 0.02)

    def ask(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        # an operator reset the row under the running consumer (owner changed): the next heartbeat must notice
        on.db.execute("UPDATE homebot_reviews SET owner='someone-else' WHERE shortlist_id=?", (sl["id"],)); on.db.commit()
        import time; time.sleep(0.2)
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", ask)
    out = hr.process_pending(on.db)
    assert out["lost_claim"] == 1 and out["reviewed"] == 0
    r = _reviews(on, sl["id"])[0]
    assert r["owner"] == "someone-else" and r["review_json"] is None and r["state"] == hr.RUNNING


def test_publication_and_retry_backoff_count_from_completion_not_from_capture(on, monkeypatch):
    from datetime import datetime, timedelta
    eid, sl = _prepared(on)
    start = datetime.fromisoformat(hr.now_iso())
    finish = start + timedelta(seconds=hr.RETRY_AFTER_S + 30)
    clock = [start]
    monkeypatch.setattr(hr, "now_iso", lambda: clock[0].isoformat())

    def fail(prompt, model=None, max_tokens=1500, system=""):
        clock[0] = finish
        raise ai.AIUnavailable("provider down", kind="provider")
    monkeypatch.setattr(ai, "_ask", fail)
    hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert r["state"] == hr.PENDING and r["updated_at"] == finish.isoformat()
    assert hr.due_count(on.db, now=finish.isoformat()) == 0                        # backoff starts when the failed call ENDED
    assert hr.due_count(on.db, now=(finish + timedelta(seconds=hr.RETRY_AFTER_S)).isoformat()) == 1
    later = finish + timedelta(seconds=hr.RETRY_AFTER_S + 5)
    clock[0] = later
    done = later + timedelta(seconds=40)

    def slow_ok(prompt, model=None, max_tokens=1500, system=""):
        pk = json.loads(prompt[prompt.index("\n\nPacket:\n\n") + len("\n\nPacket:\n\n"):])
        clock[0] = done
        return _good_reply(pk)
    monkeypatch.setattr(ai, "_ask", slow_ok)
    assert hr.process_pending(on.db)["reviewed"] == 1
    r = _reviews(on, sl["id"])[0]
    assert r["reviewed_at"] == done.isoformat() and r["updated_at"] == done.isoformat()


@pytest.mark.parametrize("kind, word", [("credentials", "credentials"), ("setup", "SDK")])
def test_a_configuration_failure_is_named_terminal_and_never_an_automatic_provider_retry(on, monkeypatch, kind, word):
    eid, sl = _prepared(on)
    calls = []

    def ask(prompt, model=None, max_tokens=1500, system=""):
        calls.append(1)
        raise ai.AIUnavailable("SECRET-DETAIL-xyz", kind=kind)
    monkeypatch.setattr(ai, "_ask", ask)
    hr.process_pending(on.db)
    r = _reviews(on, sl["id"])[0]
    assert r["state"] == hr.FAILED and r["error"] == hr.FAILURE_TEXT[kind] and word in r["error"] and "SECRET" not in r["error"]
    assert hr.due_count(on.db, now="2099-01-01T00:00:00+00:00") == 0 and len(calls) == 1      # no retry, ever, until re-enqueued
    assert ai_queue.homebot_review_state(on.db, sl["id"])["state"] == hr.FAILED


# lease renewal never owns unknown work and never waits unbounded (H7d, root renewal probe) --------------------------------------
def _claimed_lease(on, sl):
    import server.db as sdb
    with sdb.retention_guard(on.db, label="test claim"):
        r = row(on.db.execute("SELECT * FROM homebot_reviews WHERE shortlist_id=?", (sl["id"],)))
        assert hr._claim(on.db, r, "lease-test-owner", hr.now_iso())
    return hr._Lease(on.db, r["id"], "lease-test-owner")


def test_a_beat_on_a_connection_with_someone_elses_pending_write_is_skipped_without_commit_or_rollback(on):
    eid, sl = _prepared(on)
    lease = _claimed_lease(on, sl)
    before = row(on.db.execute("SELECT claimed_at FROM homebot_reviews WHERE id=?", (lease.rid,)))["claimed_at"]
    on.db.execute("UPDATE homebot_enquiries SET updated_at='pending-marker' WHERE id=?", (eid,))
    try:
        assert lease.renew() is True and lease.skipped == 1 and lease.lost is False
        assert on.db.in_transaction, "the beat must not commit another operation's pending write"
        assert row(on.db.execute("SELECT updated_at FROM homebot_enquiries WHERE id=?", (eid,)))["updated_at"] == "pending-marker"
    finally:
        on.db.rollback()
    assert row(on.db.execute("SELECT claimed_at FROM homebot_reviews WHERE id=?", (lease.rid,)))["claimed_at"] == before
    on.db.execute("UPDATE homebot_reviews SET claimed_at='2020-01-01T00:00:00+00:00' WHERE id=?", (lease.rid,)); on.db.commit()
    assert lease.renew() is True and lease.skipped == 1            # a clean connection: the beat lands and commits only itself
    assert row(on.db.execute("SELECT claimed_at FROM homebot_reviews WHERE id=?", (lease.rid,)))["claimed_at"] > "2020-01-02"
    assert not on.db.in_transaction


def test_a_beat_waits_a_bounded_time_for_the_connection_and_then_skips(on, monkeypatch):
    import threading, time
    eid, sl = _prepared(on)
    lease = _claimed_lease(on, sl)
    monkeypatch.setattr(hr, "HEARTBEAT_DB_TIMEOUT_MS", 100)
    held, release = threading.Event(), threading.Event()

    def holder():                       # another thread holds the connection lock (as a long write or guard would)
        with on.db._mmai_lock:
            held.set()
            release.wait(5)
    t = threading.Thread(target=holder, daemon=True)
    t.start(); held.wait(1)
    t0 = time.monotonic()
    try:
        assert lease.renew() is True and lease.skipped == 1 and lease.lost is False
        assert time.monotonic() - t0 < 1.0, "the beat queued behind the other thread instead of giving up within the bound"
    finally:
        release.set(); t.join(2)


def test_a_beat_under_another_threads_retention_guard_is_skipped_and_the_guard_commits_nothing_extra(on):
    import threading
    import server.db as sdb
    eid, sl = _prepared(on)
    lease = _claimed_lease(on, sl)
    before = row(on.db.execute("SELECT claimed_at FROM homebot_reviews WHERE id=?", (lease.rid,)))["claimed_at"]
    inside, go, result = threading.Event(), threading.Event(), {}

    def guarded_step():
        with sdb.retention_guard(on.db, label="other step"):
            inside.set(); go.wait(5)
    t = threading.Thread(target=guarded_step, daemon=True); t.start(); inside.wait(1)
    # the guard owns the connection lock for its whole step; the beat's bounded wait ends in a skip, not in a nested commit
    import time; t0 = time.monotonic()
    assert lease.renew() is True and lease.skipped == 1 and time.monotonic() - t0 < hr.HEARTBEAT_DB_TIMEOUT_MS / 1000.0 + 1.0
    go.set(); t.join(2)
    assert row(on.db.execute("SELECT claimed_at FROM homebot_reviews WHERE id=?", (lease.rid,)))["claimed_at"] == before


def test_a_beat_waits_a_bounded_time_for_a_rival_sqlite_writer_and_restores_the_busy_timeout(on, monkeypatch):
    import threading, time
    import server.db as sdb
    if sdb.is_postgres():
        pytest.skip("SQLite busy_timeout/write-lock behaviour; PostgreSQL row-lock renewal has separate probes")
    eid, sl = _prepared(on)
    lease = _claimed_lease(on, sl)
    monkeypatch.setattr(hr, "HEARTBEAT_DB_TIMEOUT_MS", 100)
    busy_before = on.db.execute("PRAGMA busy_timeout").fetchone()[0]
    rival = sdb.connect(row(on.db.execute("PRAGMA database_list"))["file"])
    rival.execute("BEGIN IMMEDIATE")                        # an independent writer holds the SQLite write lock
    try:
        t0 = time.monotonic()
        assert lease.renew() is True and lease.skipped == 1 and lease.lost is False
        assert time.monotonic() - t0 < 1.0
    finally:
        rival.rollback(); rival.close()
    assert on.db.execute("PRAGMA busy_timeout").fetchone()[0] == busy_before and not on.db.in_transaction
