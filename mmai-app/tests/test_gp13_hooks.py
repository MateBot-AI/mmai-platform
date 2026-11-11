"""GP13 hook call sites in server/matching.py (lead slice, 9 Nov 2026).

What is tested here is the CONTRACT between the lead's group-membership mutators and the integration builder's two modules
(server/identities.py, server/group_responses.py - not landed; doubles stand in for them):
  * every write that changes WHO is in a group (or dissolves it) is followed by `group_responses.on_membership_changed`;
  * a person leaving a form party (GP9(e) bar) is announced to `identities` BEFORE their row is deleted, a lead takeover is
    announced as a promotion (same human, same identity), and the enquiry is reconciled afterwards;
  * the hook always runs inside a retention guard (the module refuses otherwise): in the caller's step when there is one, else
    in a guarded step of its own after the caller's commit; a step that cannot start is deferred without breaking the mutation;
  * a hook failure inside the caller's step aborts the whole mutation (nothing half-done);
  * no module present -> no calls, same behaviour as before (the rest of the suite runs that way);
  * the hooks never touch group status, GP9 rows or enquiries (the mutation's own effects are the only effects).
"""
import sys
import types

import pytest
from fastapi.testclient import TestClient

from server import db, matching
from server.db import rows
from tests.conftest import fake_assess_groups  # noqa: F401
from tests.test_bring_a_mate import _fake_ai, _group_of, _group_of_four, _person, _run, _submit
from tests.test_data_and_api import appdb, client, mark_introduced, member  # noqa: F401


class Recorder:
    """Stand-ins for the integration builder's modules. Every call records (name, args, guarded-at-call-time)."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on

    def _rec(self, name):
        def fn(c, *args, **kw):
            self.calls.append((name, args, db.guarded(c)))
            if name == self.fail_on:
                raise RuntimeError("hook double failure")
            return {"changed": True, "version": {"id": len(self.calls)}} if name == "on_membership_changed" else None
        return fn

    def modules(self):
        gr = types.ModuleType("server.group_responses")
        gr.on_membership_changed = self._rec("on_membership_changed")
        ids = types.ModuleType("server.identities")
        for n in ("promote_to_lead", "member_left", "reconcile"):
            setattr(ids, n, self._rec(n))
        ids.active = lambda c, eid: []          # read-only lookup used by the allocation uid stamping: the double knows nobody
        ids.norm = lambda v: " ".join(str(v or "").split()).strip().lower()
        return gr, ids

    def of(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def hooks(monkeypatch):
    rec = Recorder()
    gr, ids = rec.modules()
    monkeypatch.setitem(sys.modules, "server.group_responses", gr)
    monkeypatch.setitem(sys.modules, "server.identities", ids)
    return rec


def _install(monkeypatch, rec):
    gr, ids = rec.modules()
    monkeypatch.setitem(sys.modules, "server.group_responses", gr)
    monkeypatch.setitem(sys.modules, "server.identities", ids)


# ---------------------------------------------------------------------------------------------------- absent modules
def test_without_the_modules_nothing_is_called_and_the_adapter_is_a_no_op(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "server.group_responses", None)   # import -> ImportError
    monkeypatch.setitem(sys.modules, "server.identities", None)
    assert matching._gp13("group_responses", "on_membership_changed") is None
    assert matching._membership_changed(appdb(client), 1, "proposed") is None
    assert matching._identity("reconcile", appdb(client), 1, "left") is None


def test_a_present_module_is_binding_its_dependency_failure_or_missing_hook_surfaces(client, monkeypatch):
    import builtins, types
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "server.group_responses":
            raise ModuleNotFoundError("a dependency of the present module is missing", name="server.some_dependency")
        if name == "server.identities":
            return types.SimpleNamespace()                      # present, but without the hook
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(ModuleNotFoundError):
        matching._membership_changed(appdb(client), 1, "proposed")
    with pytest.raises(AttributeError):
        matching._identity("reconcile", appdb(client), 1, "left")


def test_reasons_are_a_closed_set(client, hooks):
    with pytest.raises(AssertionError):
        matching._membership_changed(appdb(client), 1, "whatever")
    assert hooks.calls == []


# ---------------------------------------------------------------------------------------------------- the run
def test_a_run_versions_every_new_group_inside_the_save_guard(client, monkeypatch, hooks):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    calls = hooks.of("on_membership_changed")
    assert calls, "the save step must version the new group"
    assert all(guarded for _, _, guarded in calls)
    assert all(args[1] == "proposed" for _, args, _ in calls)
    assert any(args[0] == g["id"] for _, args, _ in calls)
    # no second version for an unchanged group on the next run (the hook is not called from in-place updates)
    n = len(hooks.calls)
    assert _run(client)["status"] == "done"
    assert not [c for c in hooks.calls[n:] if c[0] == "on_membership_changed" and c[1][0] == g["id"]]
    # (identity reconciles DO happen on the second run: the refreshed apartment options are stamped with the people's uids)


# ---------------------------------------------------------------------------------------------------- bar, party stays
def _trio_in_group(client, anon):
    trio = _person(1, party="With friends", party_size="3", mate_max="3", rooms=["Share with one other", "Own room"],
                   party_members=[member(name="Bo Pal", email="bo@example.com"), member(name="Cy Pal", email="cy@example.com")])
    a = _submit(anon, trio).json()
    _submit(anon, _person(4))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])
    assert g and len(g[0]["members"]) == 2
    return a, g[0]


def test_barring_a_companion_retires_their_identity_before_the_row_goes_then_versions_the_group_in_the_same_step(client, monkeypatch, hooks):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a, g = _trio_in_group(client, anon)
    mark_introduced(client, g["id"])
    e = client.get(f"/api/enquiries/{a['id']}").json()
    cy = next(m for m in e["party_members"] if m["name"] == "Cy Pal")
    hooks.calls.clear()
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST abusive", "person": cy["id"], "party_choice": "stay"}).json()
    assert r["party_stayed"] and r["awaiting_groups"] == [g["id"]]
    names = [c[0] for c in hooks.calls]
    assert names == ["member_left", "reconcile", "on_membership_changed"]
    left, rec, ver = hooks.calls
    assert left[1][:2] == (a["id"], cy["id"]) and left[1][2] == "barred"
    assert rec[1] == (a["id"], "barred")
    assert ver[1][0] == g["id"] and ver[1][1] == "barred"
    assert all(guarded for _, _, guarded in hooks.calls), "GP9(e) stay path is one guarded step; the hooks run inside it"
    assert not hooks.of("promote_to_lead")
    # the mutation itself happened exactly as before (the hooks changed nothing else)
    gg = client.get(f"/api/groups/{g['id']}").json()
    assert gg["status"] == "Awaiting replacement" and gg["awaiting_people_missing"] == 1


def test_barring_the_form_filler_is_a_lead_promotion_for_the_taker(client, monkeypatch, hooks):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a, g = _trio_in_group(client, anon)
    e = client.get(f"/api/enquiries/{a['id']}").json()
    bo = next(m for m in e["party_members"] if m["name"] == "Bo Pal")
    hooks.calls.clear()
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST", "person": "lead", "party_choice": "stay"}).json()
    assert r["party_stayed"] and "Bo has taken over" in r["message"]
    names = [c[0] for c in hooks.calls]
    assert names == ["promote_to_lead", "reconcile", "on_membership_changed"]
    promo = hooks.calls[0]
    assert promo[1][:2] == (a["id"], bo["id"]) and promo[1][2] == "barred" and promo[2]
    assert not hooks.of("member_left")
    e2 = client.get(f"/api/enquiries/{a['id']}").json()
    assert e2["first_name"] == "Bo" and e2["email"] == "bo@example.com"


def test_a_hook_failure_inside_the_guarded_step_aborts_the_whole_bar(client, monkeypatch):
    _fake_ai(monkeypatch)
    rec = Recorder()
    _install(monkeypatch, rec)
    anon = TestClient(client.app)
    a, g = _trio_in_group(client, anon)
    rec.fail_on = "on_membership_changed"   # the double fails from now on (the run above had to succeed)
    e = client.get(f"/api/enquiries/{a['id']}").json()
    cy = next(m for m in e["party_members"] if m["name"] == "Cy Pal")
    before = rows(appdb(client).execute("SELECT id FROM party_members WHERE enquiry_id=? ORDER BY id", (a["id"],)))
    with pytest.raises(RuntimeError):
        matching.bar_enquiry(appdb(client), a["id"], "S", "TEST", person=cy["id"], party_choice="stay")
    c = appdb(client)
    assert rows(c.execute("SELECT id FROM party_members WHERE enquiry_id=? ORDER BY id", (a["id"],))) == before, "the row delete was rolled back with the step"
    assert not rows(c.execute("SELECT 1 FROM barred_people WHERE enquiry_id=?", (a["id"],)))
    assert c.execute("SELECT status FROM groups WHERE id=?", (g["id"],)).fetchone()[0] == "Proposed"
    assert not db.guarded(c)


# ---------------------------------------------------------------------------------------------------- bar, enquiry leaves
def test_barring_a_lone_member_versions_the_old_and_the_refilled_group_in_their_own_guarded_step_after_the_commit(client, monkeypatch, hooks):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    _submit(anon, _person(5)); _submit(anon, _person(6))
    hooks.calls.clear()
    r = client.post(f"/api/enquiries/{others[-1]}/bar", json={"reason": "TEST"}).json()
    assert r["barred_at"]
    vers = hooks.of("on_membership_changed")
    assert vers, "the membership changed; the old group must be versioned"
    assert all(guarded for _, _, guarded in vers), "outside a caller guard the adapter opens its own guarded step"
    by_gid = {args[0]: args[1] for _, args, _ in vers}
    if r["replaced_groups"]:
        assert by_gid[g["id"]] == "dissolved" and by_gid[r["replaced_groups"][0]["new_group_id"]] == "replacement"
    elif r["dissolved_groups"]:
        assert by_gid[g["id"]] == "dissolved"
    else:
        assert by_gid[g["id"]] == "barred"   # kept together, awaiting a replacement
    c = appdb(client)
    assert not db.guarded(c) and not getattr(c, "in_transaction", False)


def test_when_the_own_step_cannot_start_the_versioning_is_deferred_and_the_mutation_stands(client, monkeypatch, hooks, caplog):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    _submit(anon, _person(5)); _submit(anon, _person(6))
    hooks.calls.clear()
    real = db.retention_guard

    class Busy(real):
        def __enter__(self):
            if self.label == "group membership version":
                raise db.RetentionLockTimeout("database busy (test)")
            return super().__enter__()

    monkeypatch.setattr(db, "retention_guard", Busy)
    victim = others[-1]
    with caplog.at_level("WARNING", logger="mmai.matching"):
        r = client.post(f"/api/enquiries/{victim}/bar", json={"reason": "TEST"}).json()
    assert r["barred_at"], "the bar itself was committed before the hook step; a busy database defers only the versioning"
    assert not hooks.of("on_membership_changed")
    msgs = [m for m in caplog.messages if "gp13 membership version deferred" in m]
    assert msgs and all("TEST" not in m for m in msgs)   # category + ids only, no reason text or personal data
    assert client.get(f"/api/enquiries/{victim}").json()["barred_at"]


# ---------------------------------------------------------------------------------------------------- mate, replacement
def test_a_friend_joining_versions_the_dissolved_group_and_the_new_one(client, monkeypatch, hooks):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    hooks.calls.clear()
    f = _submit(anon, _person(9), mate_token=tok).json()
    assert f.get("id")
    vers = {args[0]: args[1] for _, args, _ in hooks.of("on_membership_changed")}
    assert vers.get(g["id"]) == "dissolved"
    new = _group_of(client, root)
    assert new and vers.get(new[0]["id"]) == "mate"
    assert all(guarded for _, _, guarded in hooks.of("on_membership_changed"))


def test_accepting_a_replacement_versions_both_groups(client, monkeypatch, hooks):
    from server import matching as mt
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    mark_introduced(client, g["id"])
    _submit(anon, _person(5))
    hooks.calls.clear()
    r = client.post(f"/api/enquiries/{others[-1]}/bar", json={"reason": "TEST"}).json()
    if not r["flagged_groups"]:
        pytest.skip("no replacement was found for this pool shape")
    assert {args[1] for _, args, _ in hooks.of("on_membership_changed")} == {"barred"}
    hooks.calls.clear()
    d = client.get(f"/api/groups/{g['id']}").json()
    out = mt.fill_replacement(appdb(client), g["id"], "S", added_enquiry_ids=d["replacement"][0]["added_enquiry_ids"])
    vers = {args[0]: args[1] for _, args, _ in hooks.of("on_membership_changed")}
    assert vers == {g["id"]: "dissolved", out["new_group_id"]: "replacement"}
