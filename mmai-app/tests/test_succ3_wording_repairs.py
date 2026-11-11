"""Succ3 wording repairs (10 Nov 2026; root SUCC3-REPAIR-ASSIGNMENT areas N1 / N2). Exact reviewer fixtures (frozen
lifecycle_probes.py N1_CASES / N2_CASES) plus controls. Wording guards only - no provider, no new business rule.

N1  a presupposed household habit inside an invitation sentence is rejected in every supplied shape: "and how everyone is very
    tidy", "and why everyone is very social", "which is easy since everyone is quiet", "when everyone is so social anyway", "and how
    very tidy everyone already is"; REQ 66, the "if anyone prefers ..." hypothetical and the indirect-question list stay accepted.
N2  a tidiness degree grounds the form answer only when it is said of the person or the home: "a spotless driving record", "my
    workshop is spotless and very clean", "keep my uniform clean and spotless at work" ground nothing; "I keep our home spotless",
    "I work in an office, but keep my home very tidy" stay grounded; bare 'clean' is never remapped to Average / Relaxed."""
import pytest

from server import ai, notes_facts
from tests.test_data_and_api import appdb, client  # noqa: F401  (fixtures)
from tests.test_triage_fix3_invitation_topics import BASIS, MEMBERS, PROSE, REQ66_TO_DISCUSS

N1_CASES = [("REQ66 exact", None, False),
            ("assertion next sentence", "Chat together about tidiness and chores. Everyone is very tidy.", True),
            ("assertion same sentence", "Chat together about tidiness, and everyone is very tidy.", True),
            ("how-everyone", "Chat together about chores, and how everyone is very tidy.", True),
            ("why-everyone", "Discuss routines, and why everyone is very social.", True),
            ("which-clause", "Agree quiet hours together, which is easy since everyone is quiet.", True),
            ("when-clause", "Talk through guests together, when everyone is so social anyway.", True),
            ("how-very-tidy-already", "Chat together about chores and how very tidy everyone already is.", True),
            ("if hypothetical (legit)", "Talk through guests together, and if anyone prefers quiet weeknights, say so early.", False),
            ("indirect-question list (legit)", "Chat together about how tidy you each like the home, how often guests visit and when you each get up.", False),
            ("so-that purpose clause (legit)", "Agree quiet hours together, so that everyone's expectations are clear.", False),
            ("because clause with no habit (legit)", "Agree quiet hours together, because routines differ.", False),
            ("named assertion", "Chat together about tidiness; Yuki and Leila are both very tidy.", True)]


@pytest.mark.parametrize("name,to_discuss,expect_problem", N1_CASES, ids=[c[0] for c in N1_CASES])
def test_presupposed_household_habits_inside_invitations(name, to_discuss, expect_problem):
    pr = ai.habit_claim_problems(dict(PROSE, to_discuss=to_discuss or REQ66_TO_DISCUSS), MEMBERS, BASIS)
    assert bool(pr) == expect_problem, (name, pr)
    if expect_problem:
        assert "tidy" in pr[0] or "social" in pr[0], pr


def test_so_is_split_only_as_a_conjunction():
    assert [c for c in ai._CLAUSE.split("everyone is so social anyway") if c] == ["everyone is so social anyway"]
    assert [c for c in ai._CLAUSE.split("agree quiet hours so everyone's expectations are clear") if c] == ["agree quiet hours", "everyone's expectations are clear"]


N2_CASES = [("I am very tidy.", "Very tidy", "affirmed", True), ("I am extremely clean.", "Very tidy", "affirmed", True), ("I am a neat freak.", "Very tidy", "affirmed", True),
            ("I am fairly tidy.", "Average", "affirmed", True), ("I am relaxed about mess.", "Relaxed", "affirmed", True),
            ("I keep shared spaces clean.", "Very tidy", "affirmed", False), ("I keep shared spaces clean.", "Average", "affirmed", False), ("I keep shared spaces clean.", "Relaxed", "affirmed", False),
            ("I am clean and tidy.", "Very tidy", "affirmed", False), ("I am not very tidy.", "Very tidy", "affirmed", False), ("I am not very tidy.", "Relaxed", "affirmed", False),
            ("I am meticulous at work.", "Very tidy", "affirmed", False), ("I have a spotless driving record.", "Very tidy", "affirmed", False),
            ("I keep my uniform clean and spotless at work.", "Very tidy", "affirmed", False), ("I am very neat at work.", "Very tidy", "affirmed", False),
            ("I am very tidy at my desk at work.", "Very tidy", "affirmed", False), ("My workshop is spotless and very clean.", "Very tidy", "affirmed", False),
            ("I am very clean-cut in my career.", "Very tidy", "affirmed", False),
            ("I am very tidy at work, but relaxed at home.", "Very tidy", "affirmed", False),
            ("I work in an office, but keep my home very tidy.", "Very tidy", "affirmed", True),
            # controls beyond the reviewer set
            ("I keep our home spotless.", "Very tidy", "affirmed", True), ("I am a very tidy person.", "Very tidy", "affirmed", True),
            ("We keep the kitchen very clean.", "Very tidy", "affirmed", True), ("My room is always spotless.", "Very tidy", "affirmed", True),
            ("I'd describe myself as very tidy.", "Very tidy", "affirmed", True), ("Very tidy and organised, I keep everything spotless.", "Very tidy", "affirmed", True),
            ("I have a spotless reputation.", "Very tidy", "affirmed", False), ("My desk is very tidy.", "Very tidy", "affirmed", False),
            ("I keep my car very clean.", "Very tidy", "affirmed", False), ("I am very tidy at home.", "Very tidy", "affirmed", True)]


def _validate(text, answer, pol):
    span = text.rstrip(".")
    reply = {"items": [{"category": "lifestyle_self_description", "polarity": pol, "strength": "stated", "subject": "self", "field": "else_text", "start": 0, "end": len(span),
                        "span": span, "value": {"field": "tidy", "answer": answer}}], "residual": False}
    return notes_facts.validate_reply(reply, {"else_text": text, "work": "", "intro": ""}, 0)


@pytest.mark.parametrize("text,answer,pol,expect_packet", N2_CASES, ids=[f"{c[0]}->{c[1]}" for c in N2_CASES])
def test_tidiness_degree_binds_to_the_person_or_the_home(text, answer, pol, expect_packet):
    out = _validate(text, answer, pol)
    assert bool(out.get("packet_items")) == expect_packet, (text, answer, out.get("rejected"))
    if not expect_packet and pol == "affirmed" and "not" not in text:
        assert all(r["code"] in ("rejected_grounding", "rejected_category") for r in out.get("rejected", [])), out.get("rejected")


def test_object_binding_reasons_are_plain_and_bounded():
    assert notes_facts._tidy_not_about_person_or_home("I have a spotless driving record", "I have a spotless driving record", "Very tidy") is True
    assert notes_facts._tidy_not_about_person_or_home("My workshop is spotless and very clean", "My workshop is spotless and very clean", "Very tidy") is True
    assert notes_facts._tidy_not_about_person_or_home("I keep our home spotless", "I keep our home spotless", "Very tidy") is False
    assert notes_facts._tidy_not_about_person_or_home("Spotless", "Spotless", "Very tidy") is False   # no readable subject: unchanged behaviour
    assert notes_facts._tidy_not_about_person_or_home("I am very tidy", "I am very tidy", "Early riser") is False   # other answers untouched
    reasons = {r.get("reason") for r in _validate("My workshop is spotless and very clean.", "Very tidy", "affirmed").get("rejected", [])}
    assert reasons <= {"tidiness is described for something other than the person or the home", "tidiness is described for a work or study context, not the home", None, ""}


def test_catalogue_version_moved_with_the_binding():
    # BEFORE: t4c-v2.3 (home-context binding). AFTER: t4c-v2.4 (person/home object binding) - earlier accepted projections are
    # no longer current and are re-read through the existing currentness path; prompt version unchanged.
    assert notes_facts.CATALOGUE_VERSION == "t4c-v2.4" and notes_facts.PROMPT_VERSION == "notes-extract-3"


# ------------------------------------------------------------------------------------------------------------------ N1: stored introductions
def test_stored_introduction_drafts_carry_the_wording_contract_and_earlier_ones_are_history(client, monkeypatch):  # noqa: C901
    """A draft accepted under an earlier wording check is never current, even with identical evidence (notes_status 'none' members
    get no notes bump). Existing context machinery only: the body records ai.INTRO_PROSE_CONTRACT, intro_state compares at read
    time; nothing is written, no provider call follows, history is kept."""
    from server import ai_queue
    from server.db import j, pj, row
    from tests.test_gp13_group_responses import _introduced
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    st = ai_queue.intro_state(db, gid)
    assert st["current"] is True and st["stale_reason"] is None
    last = row(db.execute("SELECT id, body_json, evidence_hash FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (gid,)))
    body = pj(last["body_json"], {})
    assert body["prose_contract"] == ai.INTRO_PROSE_CONTRACT
    g = row(db.execute("SELECT evidence_hash FROM groups WHERE id=?", (gid,)))
    assert last["evidence_hash"] == g["evidence_hash"]
    # the wording check tightens (contract moves): same evidence, same offer, same people -> the stored draft is history
    monkeypatch.setattr(ai, "INTRO_PROSE_CONTRACT", ai.INTRO_PROSE_CONTRACT + ".later")
    st2 = ai_queue.intro_state(db, gid)
    assert st2["current"] is False and st2["stale_reason"] == ai_queue.INTRO_STALE_CONTRACT and st2["draft_id"] == last["id"]
    assert row(db.execute("SELECT COUNT(*) AS n FROM group_briefs WHERE group_id=? AND kind='intro'", (gid,)))["n"] >= 1   # history kept, nothing deleted
    d = client.get(f"/api/groups/{gid}").json()
    assert d["intro"]["current"] is False and "earlier introduction wording check" in (d["intro"].get("stale_reason") or "")
    assert client.patch(f"/api/groups/{gid}", json={"intro_text": "x", "draft_id": last["id"], "context_hash": body["context_hash"]}).status_code == 409   # edits need a current draft
    # a legacy draft without the key (written before succ3) is history too, whatever else is current
    body0 = dict(body); body0.pop("prose_contract")
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (j(body0), last["id"])); db.commit()
    monkeypatch.setattr(ai, "INTRO_PROSE_CONTRACT", body["prose_contract"])
    assert ai_queue.intro_state(db, gid)["stale_reason"] == ai_queue.INTRO_STALE_CONTRACT
    db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (j(body), last["id"])); db.commit()
    assert ai_queue.intro_state(db, gid)["current"] is True
