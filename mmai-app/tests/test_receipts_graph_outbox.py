"""inc1b: how the customer-receipt outbox classifies the Graph transport's answers, redirect refusal through the real HTTP adapter,
and timeout validation. The fix-targeted tests fail on increment 1 (8204978b…) and pass on inc1b; the control tests (503 bounded
retry, non-Graph retry, ambiguous-then-accepted history, valid timeouts, escaping) pass on both — they pin unchanged behaviour:
  * on inc1 receipts.deliver_pending retried GraphAuthError/GraphRejected/GraphConfigError with back-off like a network failure;
  * on inc1 _default_http used urllib's default opener, which follows 3xx (POST→GET, headers carried to the new host);
  * on inc1 MMAI_GRAPH_TIMEOUT accepted 'nan', 'inf', '0' and negative values.
Nothing here opens a socket: the HTTPS handler is a fake installed in a real urllib opener.
Bounded at-least-once semantics are unchanged in this increment and stated, not improved (no exactly-once, no delivery claim).
"""
from __future__ import annotations

import io
import json
import logging
from email.message import Message
from urllib import request as urlrequest
from urllib import response as urlresponse

import pytest
from fastapi.testclient import TestClient

from server import db, mail_graph, receipts
from server.db import rows
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)
from tests.test_mail_graph import FakeHttp, graph_env  # noqa: F401
from tests.test_receipts import KEY, _post, mail_dryrun  # noqa: F401

SENDER = "reservations@almelb.com.au"


def _outbox(c, eid):
    return rows(c.execute("SELECT * FROM email_outbox WHERE enquiry_id=? ORDER BY id", (eid,)))[0]


def _enquiry(client):
    anon = TestClient(client.app)
    r = _post(anon, good_answers(is_test=False))
    assert r.status_code == 201
    return anon, r.json()["id"]


# --------------------------------------------------------------------------- outbox classification of Graph answers (F1)

@pytest.mark.parametrize("status,klass", [(401, "GraphAuthError"), (403, "GraphAuthError"), (400, "GraphRejected"), (404, "GraphRejected")])
def test_definitive_graph_answers_stop_the_receipt_without_automatic_retry(client, mail_dryrun, graph_env, status, klass):
    c = appdb(client)
    _, eid = _enquiry(client)
    http = FakeHttp(send=(status, {}, b'{"error":{"code":"X","message":"jane@example.org"}}'))
    t = mail_graph.GraphTransport(SENDER, http=http)
    res = receipts.deliver_pending(c, t)
    ob = _outbox(c, eid)
    assert res["failed"] == 1 and res["retry"] == 0 and res["sent"] == 0
    assert ob["status"] == "failed" and ob["attempts"] == 1 and ob["last_error"] == klass and ob["next_attempt_at"] is None
    assert "jane@example.org" not in (ob["last_error"] or "")
    sends_before = len(http.calls)
    assert receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")["failed"] == 0   # nothing re-tries it
    assert len(http.calls) == sends_before                                                     # and no further provider call


def test_configuration_problem_discovered_at_send_time_stops_without_retry(client, mail_dryrun, graph_env, monkeypatch):
    c = appdb(client)
    _, eid = _enquiry(client)
    http = FakeHttp()
    t = mail_graph.GraphTransport(SENDER, http=http)
    monkeypatch.delenv("MMAI_GRAPH_CLIENT_SECRET")
    receipts.deliver_pending(c, t)
    ob = _outbox(c, eid)
    assert ob["status"] == "failed" and ob["last_error"] == "GraphConfigError" and http.calls == []
    assert "s3cr3t" not in json.dumps(dict(ob))


def test_graph_unavailable_keeps_the_bounded_retry_exactly_as_before(client, mail_dryrun, graph_env):
    c = appdb(client)
    _, eid = _enquiry(client)
    http = FakeHttp(send=(503, {"Retry-After": "7"}, b""))
    t = mail_graph.GraphTransport(SENDER, http=http)
    res = receipts.deliver_pending(c, t)
    ob = _outbox(c, eid)
    assert res["retry"] == 1 and ob["status"] == "queued" and ob["attempts"] == 1 and ob["last_error"] == "GraphUnavailable"
    assert ob["next_attempt_at"] > ob["updated_at"][:10]
    for _ in range(receipts.MAX_ATTEMPTS - 1):
        receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")
    ob = _outbox(c, eid)
    assert ob["status"] == "failed" and ob["attempts"] == receipts.MAX_ATTEMPTS   # bounded, as before


def test_non_graph_failures_keep_the_existing_retry_behaviour(client, mail_dryrun):
    """The permanent stop is Graph-specific: an unknown transport failure is still retried with back-off (SMTP path unchanged)."""
    c = appdb(client)
    _, eid = _enquiry(client)

    class Boom:
        simulated = False

        def send(self, msg):
            raise RuntimeError("boom")

    receipts.deliver_pending(c, Boom())
    ob = _outbox(c, eid)
    assert ob["status"] == "queued" and ob["attempts"] == 1 and ob["last_error"] == "RuntimeError"
    assert receipts.deliver_pending(c, receipts.CapturedTransport(fail_times=1), now="2999-01-01T00:00:00+00:00")["retry"] == 1


def test_accepted_receipt_with_failed_acknowledgement_is_not_requeued(client, mail_dryrun, monkeypatch):
    c = appdb(client)
    _, eid = _enquiry(client)
    transport = receipts.CapturedTransport()
    def fail_write(*args, **kwargs):
        raise RuntimeError("synthetic acknowledgement fault")
    monkeypatch.setattr(receipts, "_set", fail_write)
    with pytest.raises(RuntimeError):
        receipts.deliver_pending(c, transport)
    assert _outbox(c, eid)["status"] == "sending"
    assert receipts.deliver_pending(c, transport)["sent"] == 0
    assert len(transport.sent) == 1


def test_ambiguous_attempt_is_parked_without_automatic_duplicate(client, mail_dryrun, graph_env, caplog):
    """An unanswered sendMail may already be accepted; preserve its unknown state for a person."""
    c = appdb(client)
    _, eid = _enquiry(client)
    http = FakeHttp(send=[mail_graph.GraphUnavailable("network failure: timeout"), (202, {}, b"")])
    t = mail_graph.GraphTransport(SENDER, http=http)
    with caplog.at_level(logging.INFO, logger="mmai.receipts"):
        res1 = receipts.deliver_pending(c, t)
        ob1 = _outbox(c, eid)
        assert res1["skipped"] == 1 and ob1["status"] == "sending" and ob1["last_error"] == "GraphAmbiguous" and ob1["attempts"] == 1
        res2 = receipts.deliver_pending(c, t, now="2999-01-01T00:00:00+00:00")
    ob2 = _outbox(c, eid)
    assert res2["sent"] == 0 and ob2["status"] == "sending" and ob2["attempts"] == 1 and not ob2["message_id"]
    assert ob2["last_error"] == "GraphAmbiguous"
    assert [k for k, *_ in http.calls] == ["token", "send"]


# ------------------------------------------------------------------------------------ redirects through the real adapter (F2)

class FakeHTTPS(urlrequest.HTTPSHandler):
    """Stands in for the real HTTPS handler inside a real urllib opener: first request → 302 to another host; any later
    request → 202 with an empty body. Records every request urllib actually issues."""
    handler_order = 1

    def __init__(self):
        super().__init__()
        self.requests = []

    def https_open(self, req):
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        hdrs = Message()
        if len(self.requests) == 1:
            hdrs["Location"] = "https://evil.example.net/steal"
            return self._response(hdrs, req.full_url, 302, "Found")
        return self._response(hdrs, req.full_url, 202, "Accepted")

    @staticmethod
    def _response(hdrs, url, code, reason):
        r = urlresponse.addinfourl(io.BytesIO(b""), hdrs, url, code)
        r.msg = reason   # urllib's HTTPErrorProcessor reads response.msg (http.client sets it on real responses)
        return r


def test_default_http_refuses_redirects_fail_closed_through_the_real_opener(graph_env):
    fake = FakeHTTPS()
    opener = mail_graph.build_opener()
    opener.add_handler(fake)
    with pytest.raises(mail_graph.GraphRejected) as ei:
        mail_graph._default_http(f"{mail_graph.GRAPH_BASE}/users/{SENDER}/sendMail", b'{"message":{}}',
                                 {"Authorization": "Bearer tok-1", "Content-Type": "application/json"}, 5.0, opener=opener)
    assert "redirect refused: HTTP 302" in str(ei.value) and "evil.example.net" not in str(ei.value)
    assert len(fake.requests) == 1 and fake.requests[0][0] == "POST" and fake.requests[0][1].startswith(mail_graph.GRAPH_BASE)


def test_without_the_refusal_urllib_would_follow_the_redirect_and_report_202(graph_env):
    """Documents the hazard the fix closes: the default redirect handler turns the POST into a GET on the other host, carries the
    Authorization header there and a 202 from that host would have been recorded as accepted."""
    fake = FakeHTTPS()
    opener = urlrequest.build_opener(fake)  # default handlers incl. HTTPRedirectHandler, our FakeHTTPS replaces HTTPSHandler
    resp = opener.open(urlrequest.Request(f"{mail_graph.GRAPH_BASE}/users/{SENDER}/sendMail", data=b"{}",
                                          headers={"Authorization": "Bearer tok-1"}, method="POST"), timeout=5)
    assert resp.status == 202 and len(fake.requests) == 2
    assert fake.requests[1][0] == "GET" and fake.requests[1][1].startswith("https://evil.example.net/") and "Authorization" in fake.requests[1][2]


def test_transport_classifies_a_redirect_as_rejected_and_the_outbox_does_not_retry_it(client, mail_dryrun, graph_env, monkeypatch):
    fake = FakeHTTPS()
    opener = mail_graph.build_opener()
    opener.add_handler(fake)
    monkeypatch.setattr(mail_graph, "_OPENER", opener)
    token = (200, {}, json.dumps({"access_token": "tok-1", "expires_in": 3600}).encode())

    def http(url, data, headers, timeout):
        if url.endswith("/oauth2/v2.0/token"):
            return token
        return mail_graph._default_http(url, data, headers, timeout)   # module opener → the fake HTTPS handler

    c = appdb(client)
    _, eid = _enquiry(client)
    receipts.deliver_pending(c, mail_graph.GraphTransport(SENDER, http=http))
    ob = _outbox(c, eid)
    assert ob["status"] == "failed" and ob["last_error"] == "GraphRejected" and len(fake.requests) == 1


# ------------------------------------------------------------------------------------------------- timeout validation (F3)

@pytest.mark.parametrize("raw,message", [("nan", "positive"), ("inf", "positive"), ("-inf", "positive"), ("0", "positive"), ("-5", "positive"),
                                         ("abc", "number"), ("1e999", "positive")])
def test_non_finite_or_non_positive_timeout_is_refused_before_any_token_attempt(graph_env, monkeypatch, raw, message):
    monkeypatch.setenv("MMAI_GRAPH_TIMEOUT", raw)
    expected = "MMAI_GRAPH_TIMEOUT must be a number" if message == "number" else "MMAI_GRAPH_TIMEOUT must be a positive number of seconds"
    assert mail_graph.config_problem(SENDER) == expected   # the inc1 wording for non-numeric text is unchanged
    http = FakeHttp()
    with pytest.raises(mail_graph.GraphConfigError):
        mail_graph.GraphTransport(SENDER, http=http).send(__import__("tests.test_mail_graph", fromlist=["_msg"])._msg())
    assert http.calls == []
    with pytest.raises(mail_graph.GraphConfigError):
        mail_graph._default_http(f"{mail_graph.GRAPH_BASE}/x", b"", {}, float(raw) if raw not in ("abc",) else 0.0)


@pytest.mark.parametrize("raw,expected", [("", 20.0), ("2.5", 2.5), ("45", 45.0)])
def test_valid_timeouts_are_accepted(graph_env, monkeypatch, raw, expected):
    if raw:
        monkeypatch.setenv("MMAI_GRAPH_TIMEOUT", raw)
    assert mail_graph.config_problem(SENDER) is None
    http = FakeHttp()
    mail_graph.GraphTransport(SENDER, http=http).send(__import__("tests.test_mail_graph", fromlist=["_msg"])._msg())
    assert http.calls[-1][4] == expected


# --------------------------------------------------------------------------------------------- correlation id wording (F4)

def test_returned_message_id_is_local_correlation_only_and_not_in_the_graph_payload(graph_env):
    from tests.test_mail_graph import _msg
    http = FakeHttp()
    m = _msg()
    mid = mail_graph.GraphTransport(SENDER, http=http).send(m)
    assert mid == m["Message-ID"]
    body = json.loads(http.calls[-1][2].decode())
    assert mid not in json.dumps(body) and "internetMessageHeaders" not in body["message"]   # not on the wire: documented, not claimed
    assert "NOT on the delivered message" in mail_graph.__doc__ and "manual reconciliation" in mail_graph.__doc__
