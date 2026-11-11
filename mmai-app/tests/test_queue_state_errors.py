"""T5 (root, 8 Nov 2026): the AI queue's process-global STATE is outside the database's retention/scrub provenance, so it must never
hold provider or model prose (which can quote a person's answers). It carries static categories only, in both the batch path
(process_pending) and the worker loop's catch-all."""
import json

import pytest

from server import ai, ai_queue
from server.db import row, rows
from tests.test_t6_late_result import _primary

MARKER = "ROOT_REVIEW_DELETION_PROBE Astrid said she hates mornings"


@pytest.fixture(autouse=True)
def _reset_state():
    with ai_queue._lock:
        before = dict(ai_queue.STATE)
        ai_queue.STATE.update({"last_error": None, "last_error_at": None, "busy": False, "in_flight": 0})
    yield
    with ai_queue._lock:
        ai_queue.STATE.clear(); ai_queue.STATE.update(before)


def _state_text():
    with ai_queue._lock:
        return json.dumps(ai_queue.STATE, default=str)


def test_provider_failure_leaves_only_a_static_category_in_state_and_direct_sql_group_removal_leaves_nothing_behind(db, monkeypatch):
    """Unit-level stand-in for the reviewer's staff DELETE: the group rows are removed by direct SQL here (the real route and
    its cascade are exercised by root's six-case PG pipeline harness, not by this test). What this proves is that once the
    database rows are gone, nothing in process state (STATE / status()) still carries the provider text."""
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)

    def failing(batch, meter):
        meter["asks"] += 1
        raise ai.AIUnavailable(f"provider said: {MARKER}")
    monkeypatch.setattr(ai_queue, "_call_review", failing)
    out = ai_queue.process_pending(db, attempts=1)
    assert out["failed"] >= 1
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER and ai_queue.STATE["last_error_at"]
    assert MARKER not in _state_text() and "Astrid" not in _state_text()
    # Modified A (9 Nov 2026): the provider's words reach NO row either - the group holds the closed sentence for its kind
    assert row(db.execute("SELECT ai_error FROM groups WHERE id=?", (g["id"],)))["ai_error"] == ai.FAILURE_TEXT["provider"]
    assert MARKER not in json.dumps(ai_queue.status(db), default=str)
    # direct-SQL removal of every group the person is in (all of them failed in this batch); not the staff DELETE route itself
    gids = [r["group_id"] for r in rows(db.execute("SELECT group_id FROM group_members WHERE enquiry_id=?", (members[0],)))]
    assert g["id"] in gids
    for gid in gids:
        db.execute("DELETE FROM group_members WHERE group_id=?", (gid,)); db.execute("DELETE FROM groups WHERE id=?", (gid,))
    db.commit()
    text = json.dumps(ai_queue.status(db), default=str)
    assert MARKER not in text and "Astrid" not in text, "provider prose survived the deletion through process state"
    # a later success keeps the category (it is history, not prose) and does not resurrect anything
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER


def test_credentials_failure_is_its_own_category(db, monkeypatch):
    _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)

    def no_creds(batch, meter):
        meter["asks"] += 1
        raise ai.AIUnavailable("No LLM credentials: set ANTHROPIC_API_KEY (" + MARKER + ")", kind="credentials")   # the trusted kind, not the words
    monkeypatch.setattr(ai_queue, "_call_review", no_creds)
    ai_queue.process_pending(db, attempts=1)
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER_CREDENTIALS and MARKER not in _state_text()


def test_credential_words_in_a_plain_message_do_not_make_a_credentials_category(db, monkeypatch):
    _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)

    def wordy(batch, meter):
        meter["asks"] += 1
        raise ai.AIUnavailable("No LLM credentials authentication_error Error code: 401 " + MARKER)
    monkeypatch.setattr(ai_queue, "_call_review", wordy)
    ai_queue.process_pending(db, attempts=1)
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER and MARKER not in _state_text()


def test_non_provider_exception_inside_a_batch_is_a_provider_error_category_not_its_text(db, monkeypatch):
    """_assess_batch turns any exception into bounded text ("KeyError: ...") for the group's own guarded history; STATE must
    not re-derive anything from that text, not even a type-looking prefix."""
    _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)

    def boom(batch, meter):
        meter["asks"] += 1
        raise KeyError(MARKER)
    monkeypatch.setattr(ai_queue, "_call_review", boom)
    ai_queue.process_pending(db, attempts=1)
    assert ai_queue.STATE["last_error"] == ai_queue.STATE_ERROR_PROVIDER and MARKER not in _state_text() and "KeyError" not in _state_text()


def test_worker_loop_failure_records_the_closed_type_token_only(caplog):
    ai_queue._record_worker_failure(RuntimeError(MARKER))
    assert ai_queue.STATE["last_error"] == "QUEUE_WORKER:RuntimeError" and ai_queue.STATE["last_error_at"]
    assert MARKER not in _state_text() and MARKER not in caplog.text
    ai_queue._record_worker_failure(type(MARKER, (RuntimeError,), {})("x"))   # an arbitrary class name is not safe metadata
    assert ai_queue.STATE["last_error"] == "QUEUE_WORKER:unknown" and MARKER not in _state_text() and MARKER not in caplog.text


STATIC = {None, "PROVIDER_CREDENTIALS", "PROVIDER_ERROR"}


def test_state_error_category_mapping_is_a_closed_set_for_text():
    f = ai_queue.state_error_category
    assert f(None) is None
    assert f("anything " + MARKER) == "PROVIDER_ERROR"
    assert f("Error code: 401 - " + MARKER) == "PROVIDER_ERROR"                       # words in a text never classify (Modified A)
    assert f("No LLM credentials: set ANTHROPIC_API_KEY") == "PROVIDER_ERROR"
    assert f("ValueError: " + MARKER) == "PROVIDER_ERROR"
    assert f("AINoRetry: " + MARKER) == "PROVIDER_ERROR"
    assert f(ai_queue._failure(ai.AIUnavailable("x", kind="credentials"))) == "PROVIDER_CREDENTIALS"   # only the trusted kind does
    # only a real exception object contributes anything beyond the two constants, and only the CLOSED type token
    assert f("x", exc=TimeoutError(MARKER)) == "UNEXPECTED:TimeoutError"
    assert f("x", exc=type(MARKER, (RuntimeError,), {})("y")) == "UNEXPECTED:unknown"
    assert MARKER not in f("x", exc=TimeoutError(MARKER))


@pytest.mark.parametrize("text", [
    "AstridSmithError: private provider text",            # person-shaped identifier prefix (root review, 8 Nov 2026)
    "Astrid_hates_mornings_Exception: more text",
    ("Astrid" * 60) + "Error: " + MARKER,                  # very long custom "Error" identifier
    "AstridSmithError",                                    # no colon at all
    "ValueError:AstridSmith",                              # real type name followed by text without a space
    "unit 12 Astrid said no: ValueError",
    "",
])
def test_untrusted_identifier_prefixes_never_reach_state(text):
    cat = ai_queue.state_error_category(text)
    assert cat in STATIC
    assert "Astrid" not in (cat or "") and MARKER not in (cat or "")
