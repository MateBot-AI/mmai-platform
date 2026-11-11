"""GP11 "Bring a mate", I19(f) save-and-finish-later drafts, P5 rename and I19(j) no romantic imagery (27 Oct 2026)."""
import os
import re

import pytest
from fastapi.testclient import TestClient

from server import rules, ai, matching
from server.db import rows
from tests.conftest import fake_assess_groups  # noqa: F401
from tests.test_data_and_api import _fake_assess, _wait_run, appdb, client, confirm_group, good_answers, mark_introduced  # noqa: F401  (fixture re-used)

CLIENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")


def _fake_ai(monkeypatch):
    monkeypatch.setattr(ai, "assess_groups_and_apartments", _fake_assess)
    monkeypatch.setattr(ai, "assess_groups", fake_assess_groups)


def _person(i, **over):
    """A TEST enquirer who can share a 2-bedroom apartment with up to three others (own room or sharing with one other)."""
    base = dict(first_name=f"Mate{i}", last_name="Test", email=f"mate{i}@example.com", phone=f"04000000{i:02d}", is_test=True,
                rooms=["Own room", "Share with one other"], mate_max="3", mate_ideal="3")
    base.update(over)
    return good_answers(**base)


def _submit(anon, answers, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v)
    r = anon.post("/api/public/enquiries" + (f"?{qs}" if qs else ""), json=answers)
    return r


def _run(client):
    return _wait_run(client, client.post("/api/match/run-all", json={"dataset": "test"}).json()["run_id"])


def _group_of(client, eid, alternatives=False):
    items = client.get(f"/api/groups?dataset=test&alternatives={'true' if alternatives else 'false'}").json()["items"]
    return [g for g in items if any(m["enquiry_id"] == eid for m in g["members"]) and g["status"] not in ("Dissolved", "Leased")]


# ---------------- GP11(a)-(c): share link, linked form, limit ----------------
def test_share_link_links_friend_and_keeps_pair_together(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    r = _submit(anon, _person(1))
    assert r.status_code == 201
    a, tok = r.json()["id"], r.json()["mate_link"]
    assert tok and len(tok) > 15
    # the link tells the friend who shared it and how much room the party has left (GP11(c))
    meta = anon.get(f"/api/public/meta?mate_token={tok}").json()["mate_link"]
    assert meta["valid"] and meta["enquirer_first_name"] == "Mate1" and meta["people_so_far"] == 1 and meta["limit"] >= 2
    assert anon.get("/api/public/meta?mate_token=nope").json()["mate_link"]["valid"] is False
    # the friend fills in their own full form (GP11(b)); a bad form is still a bad form
    assert _submit(anon, _person(2, email="x"), mate_token=tok).status_code == 422
    r2 = _submit(anon, _person(2, is_test=False), mate_token=tok)  # inherits the enquirer's data set (TEST), never mixing data sets
    assert r2.status_code == 201 and r2.json()["is_test"] is True and r2.json()["mate"]["enquirer_first_name"] == "Mate1" and r2.json()["mate"]["outcome"] == "pool"
    b = r2.json()["id"]
    c = appdb(client)
    row = rows(c.execute("SELECT linked_to_enquiry_id, is_test FROM enquiries WHERE id=?", (b,)))[0]
    assert row["linked_to_enquiry_id"] == a and row["is_test"] == 1
    # the friend's link points at the same enquirer: one party, however the link is passed on
    assert r2.json()["mate_link"] == tok
    # in matching they are one keep-together unit
    pool = matching.load_pool(c, "test")
    party = [u for u in pool if isinstance(u, matching.LinkedParty)]
    assert len(party) == 1 and sorted(int(u.id) for u in party[0].units) == sorted([a, b]) and party[0].size == 2
    # staff see it in plain language
    e = client.get(f"/api/enquiries/{a}").json()
    assert [f["id"] for f in e["linked_friends"]] == [b] and e["linked_friends"][0]["role"] == "friend" and e["mate_link"]["token"] == tok
    e2 = client.get(f"/api/enquiries/{b}").json()
    assert e2["linked_friends"][0]["id"] == a and e2["linked_friends"][0]["role"] == "enquirer" and "share link" in e2["mate_note"]
    # after a run they are in the same group, never apart (GP11(b))
    for i in range(3, 6):
        assert _submit(anon, _person(i)).status_code == 201
    assert _run(client)["status"] == "done"
    ga, gb = _group_of(client, a, True), _group_of(client, b, True)
    assert ga and {g["id"] for g in ga} == {g["id"] for g in gb}
    g = client.get(f"/api/groups/{ga[0]['id']}").json()
    assert g["linked_parties"] and sorted(g["linked_parties"][0]["enquiry_ids"]) == sorted([a, b])
    m = next(m for m in g["members"] if m["enquiry_id"] == a)
    assert m["linked_with"] == ["Mate2"]


def test_mate_limit_is_one_less_than_beds_in_largest_available_apartment(client, monkeypatch):
    """GP11(c) (28 Oct 2026): the party must always be smaller than the beds, so the link limit is the largest apartment's beds minus one."""
    anon = TestClient(client.app)
    c = appdb(client)
    limit = matching.mate_limit(c, "test")
    best = max(min(rules.apartment_capacity(matching.apartment_from_row(a).bedrooms)[1], rules.MAX_GROUP)
               for a in rows(c.execute("SELECT * FROM apartments WHERE available_from IS NOT NULL AND leased_at IS NULL")))
    assert limit == best - 1 and 1 <= limit <= rules.MAX_GROUP - 1
    meta0 = anon.get("/api/public/meta").json()
    assert meta0["mate_limit"] == limit
    r = _submit(anon, _person(1))
    tok = r.json()["mate_link"]
    monkeypatch.setattr(matching, "mate_limit", lambda c_, ds: 3)
    assert _submit(anon, _person(2), mate_token=tok).status_code == 201  # 2 people
    # a friend with a party of two would make 4 > 3
    big = _person(3, party="With friends", party_size=2, party_members=[{"name": "Extra Person", "email": "e@example.com", "birth_country_raw": "Sweden"}],
                  rooms=["Share with one other"], room_first="Share with one other")
    r3 = _submit(anon, big, mate_token=tok)
    assert r3.status_code == 422 and "at least one more housemate" in r3.json()["errors"]["mate_link"] and "3 people" in r3.json()["errors"]["mate_link"]
    assert _submit(anon, _person(4), mate_token=tok).status_code == 201  # 3 people: exactly the limit
    assert _submit(anon, _person(5), mate_token=tok).status_code == 422
    meta = anon.get(f"/api/public/meta?mate_token={tok}").json()["mate_link"]
    assert meta["people_so_far"] == 3 and meta["room_left"] == 0


def test_link_stops_when_enquirer_is_leased_or_barred(client):
    anon = TestClient(client.app)
    a, tok = (lambda r: (r["id"], r["mate_link"]))(_submit(anon, _person(1)).json())
    client.post(f"/api/enquiries/{a}/bar", json={"reason": "duplicate TEST"})
    meta = anon.get(f"/api/public/meta?mate_token={tok}").json()["mate_link"]
    assert meta["valid"] is False and "no longer open" in meta["reason"]
    assert _submit(anon, _person(2), mate_token=tok).status_code == 422


# ---------------- GP11(d)-(h): joining an existing group ----------------
def _group_of_four(client, anon, root_smoke_ok="Yes"):
    """Mate1 (root) + Mate2..4 form one proposed group at a 2-bedroom apartment (two to a room)."""
    ids = [_submit(anon, _person(i, smoke_ok=(root_smoke_ok if i == 1 else "No"))).json() for i in range(1, 5)]
    assert _run(client)["status"] == "done"
    g = _group_of(client, ids[0]["id"])
    assert g and len(g[0]["members"]) == 4, "the four TEST people should form one proposed group"
    return ids[0]["id"], ids[0]["mate_link"], [x["id"] for x in ids[1:]], g[0]


def test_friend_joins_proposed_group_replacing_weakest_without_staff(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    c = appdb(client)
    r = _submit(anon, _person(9), mate_token=tok)
    assert r.status_code == 201 and r.json()["mate"]["outcome"] == "joined"
    friend = r.json()["id"]
    old = client.get(f"/api/groups/{g['id']}").json()
    assert old["status"] == "Dissolved" and old["flag_kind"] == "mate" and "brought a friend" in old["rerank_note"] and "GP" not in old["rerank_note"]
    new = _group_of(client, root)
    assert len(new) == 1 and new[0]["status"] == "Proposed"
    ids = {m["enquiry_id"] for m in new[0]["members"]}
    assert root in ids and friend in ids and len(ids) == 4
    replaced = set(others) - ids
    assert len(replaced) == 1  # the apartment was full, so the weakest-matched member made room (GP11(e))
    rep = replaced.pop()
    e = client.get(f"/api/enquiries/{rep}").json()
    assert e["status"] == "New" and "not rejected" in e["mate_note"]
    assert not rows(c.execute("SELECT 1 FROM member_rejections WHERE rejected_enquiry_id=? OR other_enquiry_id=?", (rep, rep)))  # GP11(h): no GP9 exclusion
    assert rows(c.execute("SELECT status FROM mate_requests")) == []  # Proposed: no staff approval needed (GP11(f))
    assert new[0]["review_state"] == "AI assessment pending"  # T6: members changed
    detail = client.get(f"/api/groups/{new[0]['id']}").json()
    assert detail["linked_parties"][0]["names"] == ["Mate1", "Mate9"] and "friend" in detail["awaiting_note"]
    # a later run keeps them together and does not resurrect the old grouping. T3(d) (10 Nov 2026): the run now chooses the
    # disjoint proposals covering the MOST people, so the replaced member is not left out while a rule-valid regrouping exists -
    # the linked pair stays whole in one proposal, the old four-person set does not come back, and every TEST person is proposed.
    assert _run(client)["status"] == "done"
    again = _group_of(client, root)
    assert again and friend in {m["enquiry_id"] for m in again[0]["members"]}
    assert {m["enquiry_id"] for m in again[0]["members"]} != {root, *others}
    primaries = [g for g in client.get("/api/groups?dataset=test&alternatives=false").json()["items"] if g["status"] == "Proposed"]
    proposed = [m["enquiry_id"] for g in primaries for m in g["members"]]
    assert len(proposed) == len(set(proposed)) and set(proposed) == {root, friend, *others}   # one proposal each, nobody stranded


def test_friend_fitting_shared_group_waits_for_staff_approval(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    assert mark_introduced(client, g["id"])["status"] == "Introduced"
    r = _submit(anon, _person(9), mate_token=tok)
    assert r.status_code == 201 and r.json()["mate"]["outcome"] == "queued"
    friend = r.json()["id"]
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Introduced"  # nothing changed yet (GP11(f))
    pend = client.get("/api/mate-requests?dataset=test").json()["items"]
    assert len(pend) == 1 and pend[0]["group_id"] == g["id"] and pend[0]["friend_name"] == "Mate9" and len(pend[0]["replace_ids"]) == 1 and "approval" in pend[0]["note"]
    assert client.get(f"/api/groups/{g['id']}").json()["mate_requests"][0]["id"] == pend[0]["id"]
    assert client.get("/api/stats?dataset=test").json()["mate_requests_pending"] == 1
    assert "Waiting for staff" in client.get(f"/api/enquiries/{friend}").json()["mate_note"]
    # approve: the friend joins; the replaced member returns to matching, not rejected
    d = client.post(f"/api/mate-requests/{pend[0]['id']}/decide", json={"approve": True}).json()
    assert d["outcome"] == "joined" and d["new_group_id"]
    new = client.get(f"/api/groups/{d['new_group_id']}").json()
    ids = {m["enquiry_id"] for m in new["members"]}
    assert root in ids and friend in ids and new["status"] == "Proposed"
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Dissolved"
    assert not rows(appdb(client).execute("SELECT 1 FROM member_rejections"))
    assert client.post(f"/api/mate-requests/{pend[0]['id']}/decide", json={"approve": True}).status_code == 409  # decided once


def test_staff_decline_means_pair_leaves_together(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    confirm_group(client, g["id"])   # GP13: introduced, every member's own yes, then confirmed (asserts 200)
    friend = _submit(anon, _person(9), mate_token=tok).json()["id"]
    rid = client.get("/api/mate-requests").json()["items"][0]["id"]
    d = client.post(f"/api/mate-requests/{rid}/decide", json={"approve": False, "reason": "group asked not to change"}).json()
    assert d["outcome"] == "left"
    c = appdb(client)
    for eid in (root, friend):
        e = client.get(f"/api/enquiries/{eid}").json()
        assert e["status"] == "New" and "together with a friend" in e["mate_note"]
    # the remaining three stay together awaiting a replacement (nothing else in the pool), nobody is rejected (GP10 applies, not GP9)
    old = client.get(f"/api/groups/{g['id']}").json()
    assert old["status"] == "Awaiting replacement" and {m["enquiry_id"] for m in old["members"]} == set(others) and old["flag_kind"] == "mate"
    assert not rows(c.execute("SELECT 1 FROM member_rejections"))
    assert rows(c.execute("SELECT status FROM mate_requests"))[0]["status"] == "declined"


def test_friend_who_does_not_fit_leaves_with_enquirer_as_pair(client, monkeypatch):
    """GP11(g): the friend smokes and nobody in the group but the enquirer accepts that."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon, root_smoke_ok="Yes")
    r = _submit(anon, _person(9, smoke="Yes"), mate_token=tok)
    assert r.status_code == 201 and r.json()["mate"]["outcome"] == "left"
    friend = r.json()["id"]
    c = appdb(client)
    old = client.get(f"/api/groups/{g['id']}").json()
    # Proposed group: the remaining members get a replacement as usual; with nobody else in the pool, GP10 keeps them together awaiting one
    assert old["status"] == "Awaiting replacement" and {m["enquiry_id"] for m in old["members"]} == set(others)
    assert "friend" in old["awaiting_note"] and "GP" not in old["awaiting_note"]
    assert not rows(c.execute("SELECT 1 FROM member_rejections"))
    for eid in (root, friend):
        assert client.get(f"/api/enquiries/{eid}").json()["status"] == "New"
    # GP11(c): a friend party is never a group on its own, so with nobody else in the pool the pair waits for a housemate
    assert _run(client)["status"] == "done"
    assert not _group_of(client, root)
    # once another compatible person enquires, the pair is grouped with them (together, never apart)
    assert _submit(anon, _person(10, smoke_ok="Yes")).status_code == 201
    assert _run(client)["status"] == "done"
    pair = _group_of(client, root)
    assert pair and {m["enquiry_id"] for m in pair[0]["members"]} >= {root, friend} and len(pair[0]["members"]) >= 3
    # the remaining members' group is still awaiting; a run never breaks it (GP10)
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Awaiting replacement"


def test_friend_with_no_group_yet_is_matched_together_on_next_run(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    assert b["mate"]["outcome"] == "pool"
    for i in range(3, 7):
        _submit(anon, _person(i))
    assert _run(client)["status"] == "done"
    ga = _group_of(client, a["id"])
    assert ga and b["id"] in {m["enquiry_id"] for m in ga[0]["members"]}


def test_rejecting_or_barring_one_linked_friend_moves_the_party_together(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    for i in range(3, 7):
        _submit(anon, _person(i))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])[0]
    assert len(g["members"]) == 4
    # GP11(k) default: "the housemates don't want this person": the whole friend party leaves together
    rep = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [b["id"]], "reason_kind": "housemates"}).json()
    assert sorted(rep["rejected"]) == sorted([a["id"], b["id"]]) and rep["friends"] == [a["id"]]  # linked friends leave together
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Dissolved"
    after = client.get(f"/api/groups/{rep['new_group_id']}").json()
    assert not ({a["id"], b["id"]} & {m["enquiry_id"] for m in after["members"]})
    c = appdb(client)
    rej = {r["rejected_enquiry_id"] for r in rows(c.execute("SELECT rejected_enquiry_id FROM member_rejections"))}
    assert rej == {a["id"], b["id"]}  # the housemates rejected the party, so neither is grouped with the stayers again
    assert "whole friend party" in client.get(f"/api/groups/{g['id']}").json()["rerank_note"]
    # they are grouped again together on a later run, with at least one housemate (GP11(c))
    for i in range(7, 9):
        _submit(anon, _person(i))
    assert _run(client)["status"] == "done"
    pair = _group_of(client, a["id"])
    assert pair and {m["enquiry_id"] for m in pair[0]["members"]} >= {a["id"], b["id"]} and len(pair[0]["members"]) >= 3
    # GP9(e): barring a never bars b; staff must record what b chose (leave with a, or stay)
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST bar"})
    assert r.status_code == 422 and "friends" in r.json()["detail"]
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST bar", "friends_choice": "leave"}).json()
    assert r["barred_at"] and r["friends_choice"] == "leave"
    e = client.get(f"/api/enquiries/{b['id']}").json()
    assert e["barred_at"] is None and e["status"] == "New"
    assert not _group_of(client, b["id"])
    assert e["linked_friends"] == [] and e["linked_to_enquiry_id"] is None  # the barred person left the friend party (GP11(n) hand-over)
    assert "not barred" in r["message"]


# ---------------- Bill's 28 Oct 2026 rulings: GP11(c), (f), (i), (k), (l) ----------------
def test_rules_friend_party_must_be_smaller_than_beds_and_never_alone():
    """GP11(c)/GP12: any party (linked friends or one form) fills at most beds-1 and always has a housemate from outside it."""
    from tests.conftest import apartment, check, unit
    apt = apartment("2B2B")  # 4 beds
    link = lambda uid, root: _linked_unit(unit, uid, root)
    root = unit("r")
    friends = [link(f"f{i}", "r") for i in range(1, 4)]
    assert not check(apt, [root] + friends).ok  # 4 linked people fill every bed
    assert any("party of 4" in x and "fill every bed" in x for x in check(apt, [root] + friends).reasons)
    assert not check(apt, [root] + friends[:2]).ok  # 3 linked people alone: no housemate
    assert any("whole group" in x for x in check(apt, [root] + friends[:2]).reasons)
    assert check(apt, [root] + friends[:2] + [unit("o")]).ok  # 3 linked + 1 housemate: fine
    assert check(apt, [root, friends[0], unit("o1"), unit("o2")]).ok
    # GP12: a party of friends on one form (I16) is a party too: four on one form may not fill the apartment, three plus an outsider may
    from tests.conftest import person
    form_party = unit("p", kind="party", people=[person(f"p{i}", unit="p") for i in range(4)])
    assert not check(apt, [form_party]).ok and any("GP12" in x for x in check(apt, [form_party]).reasons)
    trio = unit("p", kind="party", people=[person(f"p{i}", unit="p") for i in range(3)])
    assert not check(apt, [trio]).ok  # the whole group would be one party
    assert check(apt, [trio, unit("o")]).ok
    # a form party and the friends who used its link count together as one party
    duo = unit("d", kind="party", people=[person(f"d{i}", unit="d") for i in range(2)])
    assert not check(apt, [duo, link("f8", "d"), link("f7", "d")]).ok  # 2 on one form + 2 friends = 4 = every bed
    assert check(apt, [duo, link("f8", "d"), unit("o")]).ok  # 3 in the party + 1 outsider
    # two separate parties may share an apartment
    assert check(apt, [duo, unit("q", kind="party", people=[person(f"q{i}", unit="q") for i in range(2)])]).ok


def _linked_unit(unit, uid, root):
    u = unit(uid)
    u.extra = {**(u.extra or {}), "linked_to_enquiry_id": root}
    return u


def test_matching_never_forms_a_group_that_is_only_a_friend_party(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    for i in (2, 3):
        assert _submit(anon, _person(i), mate_token=a["mate_link"]).json()["mate"]["outcome"] == "pool"
    assert _run(client)["status"] == "done"
    assert not _group_of(client, a["id"]), "three linked friends must wait for a housemate"
    _submit(anon, _person(4))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])
    assert g and len(g[0]["members"]) == 4 and len(g[0]["linked_parties"][0]["enquiry_ids"]) == 3


def test_awaiting_replacement_group_takes_friend_without_approval(client, monkeypatch):
    """GP11(f) (28 Oct): a group awaiting a replacement member does not need staff approval for the swap."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    mark_introduced(client, g["id"])
    # a member is barred: the introduced group is kept together awaiting a replacement
    client.post(f"/api/enquiries/{others[-1]}/bar", json={"reason": "TEST"})
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Awaiting replacement"
    r = _submit(anon, _person(9), mate_token=tok)
    assert r.status_code == 201 and r.json()["mate"]["outcome"] == "joined", r.json()["mate"]
    assert client.get(f"/api/groups/{g['id']}").json()["status"] == "Dissolved"
    new = _group_of(client, root)[0]
    ids = {m["enquiry_id"] for m in new["members"]}
    assert ids == {root, r.json()["id"], *others[:-1]} and new["status"] == "Proposed"
    assert "Joined" in client.get(f"/api/enquiries/{r.json()['id']}").json()["mate_note"]
    assert "awaiting a replacement member" in (client.get(f"/api/groups/{g['id']}").json()["rerank_note"] or "")
    assert client.get("/api/stats?dataset=test").json()["mate_requests_pending"] == 0


def test_staff_issue_a_friend_the_same_party_link(client):
    """GP11(i): a friend may pass the link on; staff handing a friend 'their' link get the party's link, not a new party."""
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    assert client.post(f"/api/enquiries/{b['id']}/mate-link").json()["token"] == a["mate_link"]
    assert client.post(f"/api/enquiries/{a['id']}/mate-link").json()["token"] == a["mate_link"]
    # GP11(j): the link lasts one year
    exp = rows(appdb(client).execute("SELECT created_at, expires_at FROM mate_links WHERE token=?", (a["mate_link"],)))[0]
    assert exp["expires_at"][:4] == str(int(exp["created_at"][:4]) + 1) and exp["expires_at"][5:10] == exp["created_at"][5:10]


def _party_in_group(client, anon):
    """Mate1 (root) + Mate2 (friend) + Mate3 + Mate4 in one proposed group; Mate5..6 in the pool as possible replacements."""
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    for i in range(3, 7):
        _submit(anon, _person(i))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])[0]
    assert len(g["members"]) == 4 and b["id"] in {m["enquiry_id"] for m in g["members"]}
    return a["id"], b["id"], g


def test_alm_rejects_a_friend_and_the_friends_choose_to_stay(client, monkeypatch):
    """GP11(k): 'ALM rejects this person' + friends stay: only the rejected person leaves; they leave the party too."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a, b, g = _party_in_group(client, anon)
    # the choice is required
    assert client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [b], "reason_kind": "alm"}).status_code == 422
    rep = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [b], "reason_kind": "alm", "friends_choice": "stay"}).json()
    assert rep["rejected"] == [b] and rep["friends"] == [a] and rep["friends_choice"] == "stay"
    new = client.get(f"/api/groups/{rep['new_group_id']}").json()
    ids = {m["enquiry_id"] for m in new["members"]}
    assert a in ids and b not in ids and len(ids) == 4
    c = appdb(client)
    assert {r["rejected_enquiry_id"] for r in rows(c.execute("SELECT rejected_enquiry_id FROM member_rejections"))} == {b}
    e = client.get(f"/api/enquiries/{b}").json()
    assert e["status"] == "New" and e["linked_friends"] == [] and "chose to stay" in e["mate_note"]
    assert client.get(f"/api/enquiries/{a}").json()["linked_friends"] == []
    assert "chose to stay" in client.get(f"/api/groups/{g['id']}").json()["rerank_note"]


def test_alm_rejects_a_friend_and_the_friends_leave_with_them(client, monkeypatch):
    """GP11(k): 'ALM rejects this person' + friends leave: the party leaves together; only the rejected person is recorded as rejected."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a, b, g = _party_in_group(client, anon)
    rep = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [b], "reason_kind": "alm", "friends_choice": "leave"}).json()
    assert sorted(rep["rejected"]) == sorted([a, b]) and rep["friends_choice"] == "leave"
    new = client.get(f"/api/groups/{rep['new_group_id']}").json()
    assert not ({a, b} & {m["enquiry_id"] for m in new["members"]})
    c = appdb(client)
    assert {r["rejected_enquiry_id"] for r in rows(c.execute("SELECT rejected_enquiry_id FROM member_rejections"))} == {b}
    assert client.get(f"/api/enquiries/{b}").json()["linked_friends"][0]["id"] == a  # still one party
    assert "chose to leave with them" in client.get(f"/api/groups/{g['id']}").json()["rerank_note"]


def test_rejecting_the_root_when_friends_stay_rehomes_the_party_link(client, monkeypatch):
    """GP11(k) edge: ALM rejects the enquirer who sent the link and the friend stays: the friend becomes the party's root and keeps the link."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a, b, g = _party_in_group(client, anon)
    tok = client.get(f"/api/enquiries/{a}").json()["mate_link"]["token"]
    rep = client.post(f"/api/groups/{g['id']}/reject", json={"reject_enquiry_ids": [a], "reason_kind": "alm", "friends_choice": "stay"}).json()
    assert rep["rejected"] == [a]
    c = appdb(client)
    assert rows(c.execute("SELECT enquiry_id FROM mate_links WHERE token=?", (tok,)))[0]["enquiry_id"] == b
    assert rows(c.execute("SELECT linked_to_enquiry_id FROM enquiries WHERE id=?", (b,)))[0]["linked_to_enquiry_id"] is None
    assert b in {m["enquiry_id"] for m in client.get(f"/api/groups/{rep['new_group_id']}").json()["members"]}


def test_staff_decline_with_inviter_choosing_to_stay(client, monkeypatch):
    """GP11(l): declining a swap; the inviter chooses to stay, so the friend leaves the party and is matched alone."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    root, tok, others, g = _group_of_four(client, anon)
    confirm_group(client, g["id"])   # GP13: introduced, every member's own yes, then confirmed (asserts 200)
    friend = _submit(anon, _person(9), mate_token=tok).json()["id"]
    rid = client.get("/api/mate-requests").json()["items"][0]["id"]
    assert client.post(f"/api/mate-requests/{rid}/decide", json={"approve": False, "choice": "nonsense"}).status_code == 409  # the choice is required
    d = client.post(f"/api/mate-requests/{rid}/decide", json={"approve": False, "choice": "stay"}).json()
    assert d["outcome"] == "stayed" and not d["new_group_id"]
    g2 = client.get(f"/api/groups/{g['id']}").json()
    assert g2["status"] == "Group confirmed" and {m["enquiry_id"] for m in g2["members"]} == {root, *others} and g2["mate_requests"] == []
    e = client.get(f"/api/enquiries/{friend}").json()
    assert e["status"] == "New" and e["linked_friends"] == [] and "chose to stay" in e["mate_note"]
    assert client.get(f"/api/enquiries/{root}").json()["status"] == "Group confirmed"
    assert not rows(appdb(client).execute("SELECT 1 FROM member_rejections"))
    # a decline with the inviter leaving is unchanged (the pair leave together)
    friend2 = _submit(anon, _person(10), mate_token=tok).json()["id"]
    rid2 = client.get("/api/mate-requests").json()["items"][0]["id"]
    d2 = client.post(f"/api/mate-requests/{rid2}/decide", json={"approve": False, "choice": "leave"}).json()
    assert d2["outcome"] == "left"
    assert client.get(f"/api/enquiries/{friend2}").json()["linked_friends"][0]["id"] == root


# ---------------- I19(f): save and finish later ----------------
def test_drafts_save_resume_expire_and_clear_on_submit(client):
    anon = TestClient(client.app)
    r = anon.post("/api/public/drafts", json={"answers": {"first_name": "Drafty"}, "step": 3, "email": "d@example.com", "is_test": True})
    assert r.status_code == 201
    tok = r.json()["token"]
    assert len(tok) > 15 and r.json()["expires_at"] > r.json()["updated_at"]
    r = anon.put(f"/api/public/drafts/{tok}", json={"answers": {"first_name": "Drafty", "last_name": "Test"}, "step": 5})
    assert r.status_code == 200 and r.json()["step"] == 5
    got = anon.get(f"/api/public/drafts/{tok}").json()
    assert got["answers"]["last_name"] == "Test" and got["email"] == "d@example.com" and got["is_test"] is True
    assert anon.get("/api/public/drafts/unknown").status_code == 404
    # expiry in line with retention (T5): an expired draft is gone for the visitor and purged by the retention job
    c = appdb(client)
    c.execute("UPDATE form_drafts SET expires_at='2020-01-01T00:00:00+00:00' WHERE token=?", (tok,)); c.commit()
    assert anon.get(f"/api/public/drafts/{tok}").status_code == 410
    client.post("/api/retention/purge")
    assert not rows(c.execute("SELECT 1 FROM form_drafts WHERE token=?", (tok,)))
    # submitting with the draft token removes the draft
    tok2 = anon.post("/api/public/drafts", json={"answers": {}, "step": 1, "is_test": True}).json()["token"]
    assert _submit(anon, _person(1), draft_token=tok2).status_code == 201
    assert anon.get(f"/api/public/drafts/{tok2}").status_code == 404
    # deleting TEST data also clears TEST drafts and links
    client.delete("/api/test-data")
    assert not rows(c.execute("SELECT 1 FROM form_drafts WHERE is_test=1")) and not rows(c.execute("SELECT 1 FROM mate_links WHERE is_test=1"))


# ---------------- P5 rename, I19(j), T10 ----------------
def _client_text():
    out = {}
    for name in os.listdir(CLIENT_DIR):
        if name.endswith((".html", ".js", ".css")):
            with open(os.path.join(CLIENT_DIR, name), encoding="utf-8") as f:
                out[name] = f.read()
    return out


def test_rename_to_matebot_everywhere_users_see_it():
    files = _client_text()
    assert "<title>MateBot AI" in files["index.html"]
    for name, text in files.items():
        assert "Match Maker AI" not in text, name
        assert not re.search(r"\bMMAI\b(?!_)", re.sub(r"__[A-Z_0-9]+__", "", text)), name  # internal placeholders aside, no old name shown
    # I13: approved opening text kept word for word, only the product name changed
    assert "MateBot AI is AI matchmaking software that finds compatible flatmates to share a whole apartment" in files["index.html"]
    # the public form explains Bring a mate and save-and-finish-later
    assert "Bring a mate" in files["index.html"] and "finish later" in files["index.html"].lower()


def test_no_hearts_or_romantic_imagery_and_no_rule_codes_in_ui():
    files = _client_text()
    for name, text in files.items():
        low = text.lower()
        assert not re.search(r"[\u2764\u2665\u2661\U0001F495-\U0001F49F\U0001F48B\U0001F48F\U0001F491]", text), name  # no heart glyphs
        assert "heart" not in low.replace("hearth", ""), name
        assert not re.search(r"\b(romance|romantic|soulmate|date night|cupid|kiss)\b", low), name
        assert "svg" not in low or "m12 21" not in low  # no inline heart paths
    # T10: no register codes in the staff UI copy
    for name in ("staff.js", "index.html"):
        visible = re.sub(r"<!--.*?-->", "", re.sub(r"//.*", "", files[name]), flags=re.S)  # comments are never shown
        assert not re.search(r"\b(GP|T|I)\d{1,2}\b(?![-_:])", visible), name  # ':' excludes ISO times like T00:00


# ---------------- Bill's 28 Oct 2026 rulings: GP9(e), GP11(m), (n), GP12 ----------------
def test_gp12_form_party_limit_on_the_form_and_in_matching(client, monkeypatch):
    """GP12: a party on one form must be smaller than the beds in the apartment it is matched to, and can never be the whole group."""
    from tests.test_data_and_api import member
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    limit = anon.get("/api/public/meta").json()["mate_limit"]
    assert limit == matching.mate_limit(appdb(client), "test") and 2 <= limit <= 6
    mem = lambda n: [member(name=f"Pal {i}", email=f"pal{i}@example.com") for i in range(1, n)]
    too_big = _person(1, party="With friends", party_size=str(limit + 1), party_members=mem(limit + 1), mate_max="6", apt_sizes=["3 bedrooms"], budget_option="3B-twin", rooms=["Share with one other"], room_first="Share with one other")
    r = _submit(anon, too_big)
    assert r.status_code == 422 and "outside your own party" in r.json()["errors"]["party_size"]
    assert "party_size" in anon.post("/api/public/validate", json=too_big).json()["errors"]
    # four friends on one form want a 2-bedroom (4 beds): never grouped on their own; with an outsider they go into a bigger apartment only
    four = _person(2, party="With friends", party_size="4", party_members=mem(4), mate_max="3", rooms=["Share with one other"], room_first="Share with one other", budget_option="2B2B-twin")
    r = _submit(anon, four)
    assert r.status_code == 201
    assert _run(client)["status"] == "done"
    assert not _group_of(client, r.json()["id"], True), "a party of four fills every bed of a 2-bedroom apartment, so MateBot AI must not group it there"


def test_gp9e_bar_one_person_on_a_form_party_others_stay(client, monkeypatch):
    """GP9(e): barring one person on a form never bars the others. 'Stay': the person comes off the form, the rest keep their group
    with an empty bed awaiting a replacement (GP10)."""
    from tests.test_data_and_api import member
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    trio = _person(1, party="With friends", party_size="3", mate_max="3", rooms=["Share with one other", "Own room"],
                   party_members=[member(name="Bo Pal", email="bo@example.com"), member(name="Cy Pal", email="cy@example.com")])
    a = _submit(anon, trio).json()
    _submit(anon, _person(4))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])
    assert g and len(g[0]["members"]) == 2
    mark_introduced(client, g[0]["id"])
    e = client.get(f"/api/enquiries/{a['id']}").json()
    cy = next(m for m in e["party_members"] if m["name"] == "Cy Pal")
    # staff must record what the others chose
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST", "person": cy["id"]})
    assert r.status_code == 422 and "leave with them or stay" in r.json()["detail"]
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST abusive", "person": cy["id"], "party_choice": "stay"}).json()
    assert r["party_stayed"] and r["barred_person"] == "Cy Pal" and r["awaiting_groups"] == [g[0]["id"]]
    e = client.get(f"/api/enquiries/{a['id']}").json()
    assert e["barred_at"] is None and e["party_size"] == 2 and [m["name"] for m in e["party_members"]] == ["Bo Pal"]
    assert e["barred_people"][0]["name"] == "Cy Pal" and e["barred_people"][0]["reason"] == "TEST abusive" and not e["barred_people"][0]["was_lead"]
    gg = client.get(f"/api/groups/{g[0]['id']}").json()
    assert gg["status"] == "Awaiting replacement" and gg["awaiting_people_missing"] == 1 and "chose to stay" in gg["awaiting_note"]
    assert {m["enquiry_id"] for m in gg["members"]} == {a["id"], e["id"]} | {m["enquiry_id"] for m in g[0]["members"]}
    occupants = [o["person_id"] for room in gg["allocation"]["rooms"] for o in room["occupants"]]
    assert f"{a['id']}m2" not in occupants and sum(room.get("empty_beds", 0) for room in gg["allocation"]["rooms"]) == 1
    # bar the form-filler too: Bo takes over the enquiry; the original enquirer is kept in barred_people
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST", "person": "lead", "party_choice": "stay"}).json()
    assert r["party_stayed"] and "Bo has taken over" in r["message"]
    e = client.get(f"/api/enquiries/{a['id']}").json()
    assert e["first_name"] == "Bo" and e["last_name"] == "Pal" and e["email"] == "bo@example.com" and e["party"] == "Alone" and e["party_size"] == 1
    assert e["barred_at"] is None and [b["name"] for b in e["barred_people"]] == ["Cy Pal", "Mate1 Test"] and e["barred_people"][1]["was_lead"]
    gg = client.get(f"/api/groups/{g[0]['id']}").json()
    assert gg["status"] == "Awaiting replacement" and gg["awaiting_people_missing"] == 2
    assert str(a["id"]) in [o["person_id"] for room in gg["allocation"]["rooms"] for o in room["occupants"]]


def test_gp9e_bar_one_person_on_a_form_party_others_leave(client, monkeypatch):
    """GP9(e) 'leave': the form is out of matching, but only the named person is recorded as barred; lifting restores the form."""
    from tests.test_data_and_api import member
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    couple = _person(1, party="Couple sharing one bed", party_size="2", rooms=["Couple sharing one bed"], room_first="Couple sharing one bed",
                     party_members=[member(name="Bo Pal", email="bo@example.com")])
    a = _submit(anon, couple).json()
    e = client.get(f"/api/enquiries/{a['id']}").json()
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST", "person": e["party_members"][0]["id"], "party_choice": "leave"}).json()
    assert r["barred_at"] and r["barred_person"] == "Bo Pal" and "not barred" in r["message"]
    e = client.get(f"/api/enquiries/{a['id']}").json()
    assert e["barred_at"] and e["barred_person"] == "Bo Pal" and e["party_size"] == 2 and len(e["party_members"]) == 1
    lst = client.get("/api/enquiries?dataset=test&todo=barred").json()["items"]
    assert any(x["id"] == a["id"] and x["barred_person"] == "Bo Pal" for x in lst)
    r = client.post(f"/api/enquiries/{a['id']}/lift-bar", json={}).json()
    assert "Bo Pal" in r["message"]
    assert client.get(f"/api/enquiries/{a['id']}").json()["barred_person"] is None


def test_gp9e_bar_linked_friend_others_stay_and_link_hands_over_gp11n(client, monkeypatch):
    """GP9(e)/GP11(n): barring the person who sent the link never bars the friends; if they stay, the first friend takes over the link."""
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    c3 = _submit(anon, _person(3), mate_token=a["mate_link"]).json()
    _submit(anon, _person(4))
    assert _run(client)["status"] == "done"
    g = _group_of(client, a["id"])
    assert g and len(g[0]["members"]) == 4
    r = client.post(f"/api/enquiries/{a['id']}/bar", json={"reason": "TEST", "friends_choice": "stay"}).json()
    assert r["barred_at"] and sorted(r["friends"]) == sorted([b["id"], c3["id"]]) and "stay in the group" in r["message"]
    eb, ec = client.get(f"/api/enquiries/{b['id']}").json(), client.get(f"/api/enquiries/{c3['id']}").json()
    assert eb["barred_at"] is None and ec["barred_at"] is None
    assert eb["linked_to_enquiry_id"] is None and ec["linked_to_enquiry_id"] == b["id"]  # b took over the party and its link
    assert eb["mate_link"]["token"] == a["mate_link"] and ec["mate_link"]["token"] == a["mate_link"]
    assert anon.get(f"/api/public/meta?mate_token={a['mate_link']}").json()["mate_link"]["enquirer_first_name"] == "Mate2"
    assert client.get(f"/api/enquiries/{a['id']}").json()["linked_friends"] == []
    after = _group_of(client, b["id"])
    assert after and {m["enquiry_id"] for m in after[0]["members"]} >= {b["id"], c3["id"]} and a["id"] not in {m["enquiry_id"] for m in after[0]["members"]}
    assert after[0]["status"] in ("Proposed", "Awaiting replacement")


def test_gp11n_withdrawn_or_deleted_link_sender_hands_the_link_over(client, monkeypatch):
    _fake_ai(monkeypatch)
    anon = TestClient(client.app)
    a = _submit(anon, _person(1)).json()
    b = _submit(anon, _person(2), mate_token=a["mate_link"]).json()
    c3 = _submit(anon, _person(3), mate_token=a["mate_link"]).json()
    assert client.patch(f"/api/enquiries/{a['id']}", json={"status": "Withdrawn"}).status_code == 200
    eb = client.get(f"/api/enquiries/{b['id']}").json()
    assert eb["linked_to_enquiry_id"] is None and [f["id"] for f in eb["linked_friends"]] == [c3["id"]]
    assert anon.get(f"/api/public/meta?mate_token={a['mate_link']}").json()["mate_link"]["enquirer_first_name"] == "Mate2"
    assert client.delete(f"/api/enquiries/{b['id']}").status_code in (200, 204)
    ec = client.get(f"/api/enquiries/{c3['id']}").json()
    assert ec["linked_to_enquiry_id"] is None and ec["mate_link"]["token"] == a["mate_link"]
    assert anon.get(f"/api/public/meta?mate_token={a['mate_link']}").json()["mate_link"]["enquirer_first_name"] == "Mate3"
