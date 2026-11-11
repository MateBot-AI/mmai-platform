"""Microsoft Graph sendMail transport (server/mail_graph.py) and its `graph` mode in receipts / alerts.

Offline only: the HTTP layer is a fake callable; no network, no tenant, no real secret. Temp SQLite only (DATABASE_URL and
MMAI_TEST_DATABASE_URL cleared) so no PostgreSQL store is touched. Nothing here claims the tenant-side grant exists.
"""
import json
import logging
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import parse_qs

import pytest

from server import alerts, db, mail_graph, receipts
from server.db import rows

SECRET = "s3cr3t-value-that-must-never-appear"


@pytest.fixture
def graph_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MMAI_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("MMAI_GRAPH_TENANT_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("MMAI_GRAPH_CLIENT_ID", "app-client-id")
    monkeypatch.setenv("MMAI_GRAPH_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("MMAI_GRAPH_SENDER", raising=False)
    monkeypatch.delenv("MMAI_GRAPH_TIMEOUT", raising=False)


class FakeHttp:
    """Scripted responses per URL suffix: list of (status, headers, body) or an exception instance to raise."""

    def __init__(self, token=(200, {}, json.dumps({"access_token": "tok-1", "expires_in": 3600}).encode()), send=(202, {}, b"")):
        self.script = {"token": list(token if isinstance(token, list) else [token]), "send": list(send if isinstance(send, list) else [send])}
        self.calls = []

    def __call__(self, url, data, headers, timeout):
        kind = "token" if url.endswith("/oauth2/v2.0/token") else "send"
        self.calls.append((kind, url, data, dict(headers), timeout))
        q = self.script[kind]
        r = q.pop(0) if len(q) > 1 else q[0]
        if isinstance(r, Exception):
            raise r
        return r


def _msg(to="jane@example.org", html=True):
    m = EmailMessage()
    m["Subject"] = "Your enquiry"
    m["From"] = "Apartment Living Melbourne <reservations@almelb.com.au>"
    m["To"] = to
    m["Reply-To"] = "reservations@almelb.com.au"
    m["Message-ID"] = make_msgid(domain="almelb.com.au")
    m.set_content("Plain text body")
    if html:
        m.add_alternative("<p>HTML body</p>", subtype="html")
    return m


# ---------------------------------------------------------------------------------------------------------- configuration

def test_config_problem_names_each_missing_variable_without_its_value(graph_env, monkeypatch):
    assert mail_graph.config_problem("reservations@almelb.com.au") is None
    monkeypatch.delenv("MMAI_GRAPH_CLIENT_SECRET")
    assert mail_graph.config_problem("reservations@almelb.com.au") == "MMAI_GRAPH_CLIENT_SECRET missing"
    monkeypatch.setenv("MMAI_GRAPH_CLIENT_SECRET", SECRET)
    monkeypatch.delenv("MMAI_GRAPH_TENANT_ID")
    assert mail_graph.config_problem("reservations@almelb.com.au") == "MMAI_GRAPH_TENANT_ID missing"
    monkeypatch.setenv("MMAI_GRAPH_TENANT_ID", "t")
    assert mail_graph.config_problem(None) == "MMAI_GRAPH_SENDER missing or invalid"
    assert mail_graph.config_problem("not-an-address") == "MMAI_GRAPH_SENDER missing or invalid"
    assert mail_graph.config_problem("a@b.c/../x") == "MMAI_GRAPH_SENDER missing or invalid"
    monkeypatch.setenv("MMAI_GRAPH_SENDER", "reservations@almelb.com.au")
    assert mail_graph.config_problem(None) is None
    monkeypatch.setenv("MMAI_GRAPH_TIMEOUT", "soon")
    assert mail_graph.config_problem(None) == "MMAI_GRAPH_TIMEOUT must be a number"


def test_unconfigured_transport_raises_config_error_and_makes_no_http_call(graph_env, monkeypatch):
    monkeypatch.delenv("MMAI_GRAPH_CLIENT_ID")
    http = FakeHttp()
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    with pytest.raises(mail_graph.GraphConfigError):
        t.send(_msg())
    assert http.calls == []


# ------------------------------------------------------------------------------------------------------- token + send path

def test_successful_send_uses_client_credentials_then_sendmail_and_returns_own_message_id(graph_env):
    http = FakeHttp()
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    m = _msg()
    assert t.send(m) == m["Message-ID"]          # correlation value only: Graph's 202 carries no identifier
    assert t.simulated is False
    kinds = [c[0] for c in http.calls]
    assert kinds == ["token", "send"]
    tok_url, tok_body, tok_headers = http.calls[0][1], parse_qs(http.calls[0][2].decode()), http.calls[0][3]
    assert tok_url == "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/oauth2/v2.0/token"
    assert tok_body == {"grant_type": ["client_credentials"], "client_id": ["app-client-id"], "client_secret": [SECRET],
                        "scope": ["https://graph.microsoft.com/.default"]}
    assert tok_headers["Content-Type"] == "application/x-www-form-urlencoded"
    send_url, send_body, send_headers = http.calls[1][1], json.loads(http.calls[1][2]), http.calls[1][3]
    assert send_url == "https://graph.microsoft.com/v1.0/users/reservations@almelb.com.au/sendMail"
    assert send_headers["Authorization"] == "Bearer tok-1" and send_headers["Content-Type"] == "application/json"
    assert send_body["saveToSentItems"] is True
    msg = send_body["message"]
    assert msg["subject"] == "Your enquiry"
    assert msg["toRecipients"] == [{"emailAddress": {"address": "jane@example.org"}}]
    assert msg["replyTo"] == [{"emailAddress": {"address": "reservations@almelb.com.au"}}]
    assert msg["body"]["contentType"] == "HTML" and "HTML body" in msg["body"]["content"]
    assert set(msg) == {"subject", "body", "toRecipients", "replyTo"}   # nothing else from the message is copied
    assert SECRET not in json.dumps(send_body) and SECRET not in send_url


def test_plain_text_only_message_and_sender_override(graph_env, monkeypatch):
    monkeypatch.setenv("MMAI_GRAPH_SENDER", "shared-box@almelb.com.au")
    http = FakeHttp()
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    t.send(_msg(html=False))
    body = json.loads(http.calls[1][2])["message"]["body"]
    assert body == {"contentType": "Text", "content": "Plain text body\n"}
    assert http.calls[1][1].endswith("/users/shared-box@almelb.com.au/sendMail")


def test_token_is_cached_until_shortly_before_expiry_then_refetched(graph_env):
    now = [1000.0]
    http = FakeHttp(token=[(200, {}, json.dumps({"access_token": "tok-1", "expires_in": 600}).encode()),
                           (200, {}, json.dumps({"access_token": "tok-2", "expires_in": 600}).encode())])
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http, clock=lambda: now[0])
    t.send(_msg()); t.send(_msg())
    assert [c[0] for c in http.calls] == ["token", "send", "send"]
    now[0] = 1000.0 + 600 - mail_graph.TOKEN_SAFETY_SECONDS   # at the safety margin: refresh
    t.send(_msg())
    assert [c[0] for c in http.calls] == ["token", "send", "send", "token", "send"]
    assert http.calls[-1][3]["Authorization"] == "Bearer tok-2"


def test_message_without_recipient_or_body_is_rejected_before_any_call(graph_env):
    http = FakeHttp()
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    m = _msg(); del m["To"]; m["To"] = "not an address"
    with pytest.raises(mail_graph.GraphRejected):
        t.send(m)
    assert http.calls == []


# ----------------------------------------------------------------------------------------------------- failure classes

@pytest.mark.parametrize("status,exc", [(401, mail_graph.GraphAuthError), (403, mail_graph.GraphAuthError), (400, mail_graph.GraphAuthError),
                                        (429, mail_graph.GraphUnavailable), (503, mail_graph.GraphUnavailable), (418, mail_graph.GraphRejected)])
def test_token_endpoint_statuses_are_classified_and_send_is_never_attempted(graph_env, status, exc):
    http = FakeHttp(token=(status, {"Retry-After": "30"}, b'{"error":"x","error_description":"never stored"}'))
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    with pytest.raises(exc) as ei:
        t.send(_msg())
    assert [c[0] for c in http.calls] == ["token"]
    assert "never stored" not in str(ei.value) and SECRET not in str(ei.value)
    if exc is mail_graph.GraphUnavailable:
        assert ei.value.retry_after == 30 and ei.value.ambiguous is False


def test_unreadable_token_response_is_a_rejection_not_a_crash(graph_env):
    for body in (b"not json", b'{"expires_in": 10}', b'{"access_token": ""}'):
        t = mail_graph.GraphTransport("reservations@almelb.com.au", http=FakeHttp(token=(200, {}, body)))
        with pytest.raises(mail_graph.GraphRejected):
            t.send(_msg())


@pytest.mark.parametrize("status,exc", [(401, mail_graph.GraphAuthError), (403, mail_graph.GraphAuthError), (429, mail_graph.GraphUnavailable),
                                        (500, mail_graph.GraphUnavailable), (400, mail_graph.GraphRejected), (404, mail_graph.GraphRejected)])
def test_sendmail_statuses_are_classified_with_no_automatic_retry_and_no_body_in_the_error(graph_env, status, exc):
    http = FakeHttp(send=(status, {"retry-after": "7"}, b'{"error":{"code":"ErrorX","message":"recipient jane@example.org"}}'))
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    with pytest.raises(exc) as ei:
        t.send(_msg())
    assert [c[0] for c in http.calls] == ["token", "send"]          # exactly one attempt; the outbox decides what happens next
    text = str(ei.value)
    assert f"HTTP {status}" in text and "jane@example.org" not in text and "ErrorX" not in text
    assert isinstance(ei.value, ConnectionError) == (exc is mail_graph.GraphUnavailable)   # alerts.RETRYABLE compatibility
    if status == 401:  # the cached token is dropped so the NEXT outbox attempt starts with a fresh token (not retried now)
        http.script["send"] = [(202, {}, b"")]
        t.send(_msg())
        assert [c[0] for c in http.calls][-2:] == ["token", "send"]


def test_network_failure_during_sendmail_is_ambiguous_but_during_token_is_not(graph_env):
    http = FakeHttp(send=mail_graph.GraphUnavailable("network failure: timeout"))
    t = mail_graph.GraphTransport("reservations@almelb.com.au", http=http)
    with pytest.raises(mail_graph.GraphAmbiguous) as ei:
        t.send(_msg())
    assert ei.value.ambiguous is True and isinstance(ei.value, mail_graph.GraphUnavailable) and isinstance(ei.value, ConnectionError)
    http2 = FakeHttp(token=mail_graph.GraphUnavailable("network failure: timeout"))
    with pytest.raises(mail_graph.GraphUnavailable) as ei2:
        mail_graph.GraphTransport("reservations@almelb.com.au", http=http2).send(_msg())
    assert type(ei2.value) is mail_graph.GraphUnavailable and ei2.value.ambiguous is False


def test_default_http_refuses_non_https_without_connecting(graph_env):
    with pytest.raises(mail_graph.GraphConfigError):
        mail_graph._default_http("http://login.microsoftonline.com/x", b"", {}, 1)


def test_retry_after_header_parsing_is_bounded():
    assert mail_graph._retry_after({"retry-after": "120"}) == 120
    assert mail_graph._retry_after({"retry-after": "-1"}) is None
    assert mail_graph._retry_after({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) is None
    assert mail_graph._retry_after({"retry-after": "999999"}) is None
    assert mail_graph._retry_after({}) is None


# -------------------------------------------------------------------------------------------- receipts: `graph` mail mode

def test_receipts_graph_mode_is_recognised_and_configuration_is_checked(graph_env, monkeypatch):
    monkeypatch.setenv("MMAI_MAIL_MODE", "graph")
    monkeypatch.delenv("MMAI_MAIL_FROM", raising=False)
    assert receipts.mail_mode() == "graph"
    assert receipts.config_problem() == "MMAI_MAIL_FROM missing or invalid"
    monkeypatch.setenv("MMAI_MAIL_FROM", "reservations@almelb.com.au")
    assert receipts.config_problem() is None
    t = receipts.transport_from_env()
    assert isinstance(t, mail_graph.GraphTransport) and t.sender == "reservations@almelb.com.au" and t.simulated is False
    monkeypatch.delenv("MMAI_GRAPH_CLIENT_SECRET")
    assert receipts.config_problem() == "MMAI_GRAPH_CLIENT_SECRET missing"
    monkeypatch.setenv("MMAI_MAIL_MODE", "graphql")   # unknown values stay off
    assert receipts.mail_mode() == "off" and receipts.config_problem() is None and receipts.transport_from_env() is None


def test_receipts_graph_mode_unconfigured_moves_nothing(graph_env, monkeypatch, tmp_path):
    monkeypatch.setenv("MMAI_MAIL_MODE", "graph")
    monkeypatch.setenv("MMAI_MAIL_FROM", "reservations@almelb.com.au")
    monkeypatch.delenv("MMAI_GRAPH_TENANT_ID")
    c = db.connect(str(tmp_path / "r.db"))
    try:
        assert receipts.deliver_pending(c) == {"sent": 0, "dryrun": 0, "retry": 0, "failed": 0, "cancelled": 0, "skipped": 0}
    finally:
        c.close()


# ----------------------------------------------------------------------------------------------- alerts: `graph` mode

@pytest.fixture
def alerts_db(tmp_path, monkeypatch, graph_env):
    for k in ("MMAI_ALERT_MODE", "MMAI_ALERT_FROM", "MMAI_STAFF_URL", "MMAI_ALERT_SEND_TEST"):
        monkeypatch.delenv(k, raising=False)
    conn = db.connect(str(tmp_path / "alerts.db"))
    yield conn
    conn.close()


def _alerts_on(monkeypatch):
    monkeypatch.setenv("MMAI_ALERT_MODE", "graph")
    monkeypatch.setenv("MMAI_ALERT_FROM", "matebot@almelb.com.au")
    monkeypatch.setenv("MMAI_STAFF_URL", "https://matebot.example.com/")
    monkeypatch.setenv("MMAI_ALERT_SEND_TEST", "1")


def test_alerts_graph_mode_delivers_through_graph_and_records_sent_as_accepted(alerts_db, monkeypatch, caplog):
    _alerts_on(monkeypatch)
    assert alerts.alert_mode() == "graph" and alerts.config_problem() is None
    assert isinstance(alerts.transport_from_env(), mail_graph.GraphTransport)
    http = FakeHttp()
    t = mail_graph.GraphTransport(alerts.sender(), http=http)
    alerts.raise_attention(alerts_db, "synthetic_test", 7, is_test=True)
    with caplog.at_level(logging.DEBUG):
        counts = alerts.deliver_pending(alerts_db, t)
    assert counts["sent"] == 1 and counts["dryrun"] == 0
    r = rows(alerts_db.execute("SELECT * FROM internal_alert_outbox"))[0]
    assert r["status"] == "sent" and r["message_id"] and r["last_error"] is None
    assert json.loads(http.calls[1][2])["message"]["toRecipients"] == [{"emailAddress": {"address": alerts.RECIPIENT}}]
    assert SECRET not in caplog.text and "tok-1" not in caplog.text


def test_alerts_graph_ambiguous_state_is_parked_not_retried(alerts_db, monkeypatch):
    _alerts_on(monkeypatch)
    alerts.raise_attention(alerts_db, "synthetic_test", 8, is_test=True)
    t = mail_graph.GraphTransport(alerts.sender(), http=FakeHttp(send=mail_graph.GraphUnavailable("network failure: timeout")))
    counts = alerts.deliver_pending(alerts_db, t)
    r = rows(alerts_db.execute("SELECT * FROM internal_alert_outbox"))[0]
    assert counts["skipped"] == 1 and r["status"] == "sending" and r["last_error"] == "GraphAmbiguous"
    assert r["attempts"] == 1 and r["message_id"] is None   # stable row identity; no id recorded for an unknown-state attempt
    t2 = mail_graph.GraphTransport(alerts.sender(), http=FakeHttp(send=(503, {}, b"")))
    assert alerts.deliver_pending(alerts_db, t2, now="2999-01-01T00:00:00+00:00")["retry"] == 0
    assert rows(alerts_db.execute("SELECT last_error, attempts FROM internal_alert_outbox"))[0] == {"last_error": "GraphAmbiguous", "attempts": 1}


def test_alerts_graph_auth_failure_stops_without_retry_and_leaves_the_row_for_a_person(alerts_db, monkeypatch):
    _alerts_on(monkeypatch)
    alerts.raise_attention(alerts_db, "synthetic_test", 9, is_test=True)
    http = FakeHttp(send=(403, {}, b'{"error":{"message":"Access to OData is disabled for user X"}}'))
    counts = alerts.deliver_pending(alerts_db, mail_graph.GraphTransport(alerts.sender(), http=http))
    r = rows(alerts_db.execute("SELECT * FROM internal_alert_outbox"))[0]
    assert counts["failed"] == 1 and r["status"] == "failed" and r["last_error"] == "GraphAuthError" and r["next_attempt_at"] is None
    assert len(http.calls) == 2
    assert alerts.deliver_pending(alerts_db, mail_graph.GraphTransport(alerts.sender(), http=http))["skipped"] == 0 and len(http.calls) == 2


def test_alerts_graph_mode_with_missing_secret_holds_rows_and_calls_nothing(alerts_db, monkeypatch):
    _alerts_on(monkeypatch)
    monkeypatch.delenv("MMAI_GRAPH_CLIENT_SECRET")
    assert alerts.config_problem() == "MMAI_GRAPH_CLIENT_SECRET missing"
    alerts.raise_attention(alerts_db, "synthetic_test", 10, is_test=True)
    http = FakeHttp()
    counts = alerts.deliver_pending(alerts_db, mail_graph.GraphTransport(alerts.sender(), http=http))
    assert counts["held"] == 1 and http.calls == []
    assert rows(alerts_db.execute("SELECT status, last_error FROM internal_alert_outbox"))[0] == {"status": "held", "last_error": "MMAI_GRAPH_CLIENT_SECRET missing"}
