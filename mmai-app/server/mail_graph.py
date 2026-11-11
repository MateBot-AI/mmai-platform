"""Microsoft Graph `sendMail` transport for the two outboxes (customer receipts I19(q), internal attention alerts H19/H16).

Why it exists: the reservations mailbox is on Microsoft 365 (H16). The SMTP transport in server/receipts.py signs in with a
mailbox password (SMTP AUTH Basic). Microsoft's current Exchange Online timeline keeps that working unchanged until December
2026, disables it BY DEFAULT for existing tenants at the end of December 2026 (an administrator can re-enable it) and will
announce the final removal date in the second half of 2027. The attention-alert report (reports/20261105-attention-alerts)
recorded the intended Microsoft 365 path as "Application Mail.Send scoped to the reservations mailbox, then a Graph transport
adapter (not built)". This module is that adapter. It changes no matching, eligibility, queue or customer-message behaviour.

How it authenticates (OAuth 2.0 client credentials; no password, no permanent token):
  POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
       grant_type=client_credentials  client_id=…  client_secret=…  scope=https://graph.microsoft.com/.default
  → short-lived bearer token, cached in memory until shortly before `expires_in`, re-fetched on demand. The secret is read
  from the environment at send time, held in no object attribute, never logged, never included in any error text.
How it sends:
  POST https://graph.microsoft.com/v1.0/users/{sender}/sendMail   {"message": {...}, "saveToSentItems": true}
  → 202 Accepted, no body. 202 means Graph ACCEPTED the request for processing — not that the message was delivered, and
  Graph returns no message identifier. The transport therefore returns OUR OWN Message-ID header purely as the outbox's
  correlation value (the same value the SMTP transport returns); it never invents or claims a Graph identifier. NOTE: that
  value is NOT on the delivered message — the Graph payload carries subject, recipients, Reply-To and body only (Graph assigns
  its own internet Message-ID when it sends), so `email_outbox.message_id` correlates our outbox row and its attempts, not
  anything the provider or the recipient can see. Redirects are refused (never followed), so the bearer token and the message
  are only ever sent to the two fixed hosts above. The message is built from the same EmailMessage the SMTP path uses
  (subject, To, Reply-To, text body, optional HTML alternative); nothing else from the record is read.
Delivery semantics: never exactly-once. A network failure or timeout AFTER the sendMail request was dispatched
  is ambiguous (Graph may have accepted it) and is raised as GraphAmbiguous. The outboxes preserve sending with that error
  class for manual reconciliation instead of automatically retrying an uncertain send. Definite transient rejections retain
  bounded retries. The outbox row id is the stable identity across retries; the outbox stores a
  message id only on an accepted attempt. urllib honours HTTP_PROXY/HTTPS_PROXY from the environment (as every urllib client
  does): keep them unset or trusted on the host that sends.

Tenant-side prerequisites (NOT done by this code; external and unverified until root/Anna confirm them): an Entra app
registration in ALM's tenant given `Application Mail.Send` through Exchange RBAC for Applications scoped to the reservations
mailbox only — WITHOUT a parallel unscoped Entra `Mail.Send` application grant (Microsoft documents the two as additive, so an
unscoped Entra grant would not be narrowed by the scoped one). No tenant-wide mailbox grant, no Basic-authentication exception
and no new paid provider are implied or required by this module.

Configuration (environment; all optional until a module selects this transport by its own mode switch):
  MMAI_GRAPH_TENANT_ID      Entra tenant id (GUID) or verified domain
  MMAI_GRAPH_CLIENT_ID      application (client) id
  MMAI_GRAPH_CLIENT_SECRET  client secret (environment only; never persisted by this code)
  MMAI_GRAPH_SENDER         mailbox the message is sent from (user principal name). Defaults to the calling module's From
                            address (MMAI_MAIL_FROM for receipts, MMAI_ALERT_FROM for alerts).
  MMAI_GRAPH_TIMEOUT        seconds per HTTP call (default 20)

Failure classes (the outboxes decide retry vs stop; this module only classifies):
  GraphConfigError   configuration incomplete → not attempted (RuntimeError; outbox holds / fails, never retried blindly)
  GraphAuthError     401 / 403 from the token or send endpoint → stop and leave for a person (wrong grant, consent, scope)
  GraphRejected      any other 4xx (bad recipient, malformed message) → stop, not retried
  GraphUnavailable   429 / 5xx, or a network failure / timeout BEFORE anything was dispatched (token call) → retryable
                     (subclass of ConnectionError so alerts.RETRYABLE catches it); a sane Retry-After header is kept on the
                     exception for the caller's information
  GraphAmbiguous     network failure / timeout during the sendMail call itself → park: the send state is UNKNOWN
                     (subclass of GraphUnavailable; `ambiguous = True`); the outbox's recorded error class makes this visible
Authentication failures are never retried automatically by this module (no token refresh loop, no second attempt).
Error text carries the class name and HTTP status only. Response bodies and headers are never logged or stored.

Testing: the HTTP layer is one injectable callable `http(url, data: bytes, headers: dict, timeout) -> (status, headers, body)`,
so every path above is exercised offline without a network, a tenant or a secret.
"""
from __future__ import annotations

import json
import logging
import math
import os
import socket
import threading
import time
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Callable, Optional
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

log = logging.getLogger("mmai.mail_graph")

LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
TOKEN_SAFETY_SECONDS = 60          # refresh this long before the provider's expiry
MAX_RESPONSE_BYTES = 65536         # error bodies are discarded anyway; cap what is read


class GraphConfigError(RuntimeError):
    """Configuration incomplete or invalid; nothing was attempted."""


class GraphAuthError(RuntimeError):
    """The tenant refused the application (401/403). Not retryable: a person must fix the grant."""


class GraphRejected(RuntimeError):
    """The provider rejected this message (other 4xx). Not retryable as-is."""


class GraphUnavailable(ConnectionError):
    """Throttled (429), provider error (5xx), network failure or timeout. Retryable."""
    ambiguous = False

    def __init__(self, text: str, retry_after: Optional[int] = None):
        super().__init__(text)
        self.retry_after = retry_after


class GraphAmbiguous(GraphUnavailable):
    """The sendMail request was dispatched but no response arrived: the provider MAY have accepted it. Requires reconciliation;
    the send state of this attempt is unknown and stays unknown."""
    ambiguous = True


HttpCall = Callable[[str, bytes, dict, float], tuple[int, dict, bytes]]


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Fail closed on every 3xx: a redirect is never followed, so the bearer token and the message body can never be re-sent to
    another host or turned into a GET by urllib's default handler. The 3xx surfaces as HTTPError and is classified below."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


def build_opener() -> urlrequest.OpenerDirector:
    """The opener used for every Graph call: default verified-TLS HTTPS handler, redirects refused (see _NoRedirect)."""
    return urlrequest.build_opener(_NoRedirect())


_OPENER = build_opener()


def _default_http(url: str, data: bytes, headers: dict, timeout: float, opener: Optional[urlrequest.OpenerDirector] = None) -> tuple[int, dict, bytes]:
    """urllib POST with verified TLS (default context) and redirects refused. Returns (status, lower-cased headers, body) for
    every 2xx/4xx/5xx; raises GraphRejected for any 3xx (fail closed, not retried) and GraphUnavailable for network failures."""
    if not url.startswith("https://"):
        raise GraphConfigError("Graph endpoints must be https")
    if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0):
        raise GraphConfigError("MMAI_GRAPH_TIMEOUT must be a positive number of seconds")
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    try:
        with (opener or _OPENER).open(req, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts only
            if 300 <= resp.status < 400:  # a 3xx that a handler let through: still refused
                raise GraphRejected(f"redirect refused: HTTP {resp.status}")
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_RESPONSE_BYTES)
    except urlerror.HTTPError as e:
        if 300 <= e.code < 400:
            raise GraphRejected(f"redirect refused: HTTP {e.code}") from None
        body = b""
        try:
            body = e.read(MAX_RESPONSE_BYTES)
        except Exception:
            pass
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, body
    except (urlerror.URLError, socket.timeout, TimeoutError, OSError) as e:
        raise GraphUnavailable(f"network failure: {type(e).__name__}") from None


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def config_problem(sender: Optional[str]) -> Optional[str]:
    """Why Graph sending cannot proceed (None when complete). Names the missing variable, never its value."""
    if not _env("MMAI_GRAPH_TENANT_ID"):
        return "MMAI_GRAPH_TENANT_ID missing"
    if not _env("MMAI_GRAPH_CLIENT_ID"):
        return "MMAI_GRAPH_CLIENT_ID missing"
    if not _env("MMAI_GRAPH_CLIENT_SECRET"):
        return "MMAI_GRAPH_CLIENT_SECRET missing"
    s = _env("MMAI_GRAPH_SENDER") or (sender or "")
    if not s or "@" not in s or any(ch in s for ch in "/?# \r\n\t"):
        return "MMAI_GRAPH_SENDER missing or invalid"
    return _timeout_problem()


def _timeout_problem() -> Optional[str]:
    """Why MMAI_GRAPH_TIMEOUT is unusable (None when it is a finite positive number; default 20). Non-numeric text keeps the
    original message; 'nan', 'inf', '0' and negative values are refused with a message saying what is required."""
    raw = _env("MMAI_GRAPH_TIMEOUT") or "20"
    try:
        t = float(raw)
    except ValueError:
        return "MMAI_GRAPH_TIMEOUT must be a number"
    if not (math.isfinite(t) and t > 0):
        return "MMAI_GRAPH_TIMEOUT must be a positive number of seconds"
    return None


def _timeout_seconds() -> Optional[float]:
    """MMAI_GRAPH_TIMEOUT as a finite positive float (default 20); None when _timeout_problem() reports a problem."""
    if _timeout_problem():
        return None
    return float(_env("MMAI_GRAPH_TIMEOUT") or "20")


def _retry_after(headers: dict) -> Optional[int]:
    v = next((val for key, val in (headers or {}).items() if str(key).lower() == "retry-after"), None)
    if v is None:
        return None
    try:
        n = int(str(v).strip())
    except ValueError:
        return None
    return n if 0 <= n <= 86400 else None


def _recipients(value) -> list[dict]:
    out = []
    for _name, addr in getaddresses([str(value)] if value else []):
        if addr and "@" in addr:
            out.append({"emailAddress": {"address": addr}})
    return out


def message_payload(msg: EmailMessage, save_to_sent_items: bool = True) -> dict:
    """Graph JSON for an EmailMessage produced by receipts.build_message / alerts.build_message. HTML alternative wins when
    present (Graph carries one body); the plain text is used otherwise. Headers other than Subject/To/Reply-To are not copied."""
    to = _recipients(msg.get("To"))
    if not to:
        raise GraphRejected("message has no valid recipient")
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    if html_part is not None:
        body = {"contentType": "HTML", "content": html_part.get_content()}
    elif text_part is not None:
        body = {"contentType": "Text", "content": text_part.get_content()}
    else:
        raise GraphRejected("message has no body")
    message = {"subject": str(msg.get("Subject") or ""), "body": body, "toRecipients": to}
    reply = _recipients(msg.get("Reply-To"))
    if reply:
        message["replyTo"] = reply
    return {"message": message, "saveToSentItems": bool(save_to_sent_items)}


class GraphTransport:
    """Transport with the same contract as receipts.SMTPTransport: `send(EmailMessage) -> message id`, `simulated = False`."""
    simulated = False

    def __init__(self, sender: Optional[str] = None, *, http: Optional[HttpCall] = None, clock: Callable[[], float] = time.monotonic):
        self._sender_default = sender
        self._http = http or _default_http
        self._clock = clock
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    # -- configuration -------------------------------------------------------------------------------------------------
    @property
    def sender(self) -> str:
        return _env("MMAI_GRAPH_SENDER") or (self._sender_default or "")

    def config_problem(self) -> Optional[str]:
        return config_problem(self._sender_default)

    def _timeout(self) -> float:
        t = _timeout_seconds()
        if t is None:  # config_problem() already refused this before any token attempt; defensive for direct callers
            raise GraphConfigError(_timeout_problem() or "MMAI_GRAPH_TIMEOUT must be a positive number of seconds")
        return t

    # -- token ----------------------------------------------------------------------------------------------------------
    def _fetch_token(self) -> tuple[str, float]:
        tenant = urlparse.quote(_env("MMAI_GRAPH_TENANT_ID"), safe="")
        form = urlparse.urlencode({"grant_type": "client_credentials", "client_id": _env("MMAI_GRAPH_CLIENT_ID"),
                                   "client_secret": _env("MMAI_GRAPH_CLIENT_SECRET"), "scope": SCOPE}).encode()
        status, headers, body = self._http(f"{LOGIN_BASE}/{tenant}/oauth2/v2.0/token", form,
                                           {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                                           self._timeout())
        del form
        if status in (401, 403):
            raise GraphAuthError(f"token endpoint refused the application: HTTP {status}")
        if status == 429 or status >= 500:
            raise GraphUnavailable(f"token endpoint unavailable: HTTP {status}", _retry_after(headers))
        if status == 400:  # invalid_client / invalid_scope / unauthorized_client: a grant or registration problem, not transient
            raise GraphAuthError("token endpoint rejected the request: HTTP 400")
        if status != 200:
            raise GraphRejected(f"token endpoint: unexpected HTTP {status}")
        try:
            data = json.loads(body.decode("utf-8"))
            token = data["access_token"]
            expires_in = int(data.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise GraphRejected("token endpoint: unreadable response") from None
        if not isinstance(token, str) or not token:
            raise GraphRejected("token endpoint: empty token")
        return token, self._clock() + max(0, expires_in - TOKEN_SAFETY_SECONDS)

    def _bearer(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._token_expires_at:
                return self._token
            self._token, self._token_expires_at = self._fetch_token()
            return self._token

    def _forget_token(self) -> None:
        with self._lock:
            self._token, self._token_expires_at = None, 0.0

    # -- send -----------------------------------------------------------------------------------------------------------
    def send(self, msg: EmailMessage) -> str:
        problem = self.config_problem()
        if problem:
            raise GraphConfigError(problem)
        payload = json.dumps(message_payload(msg)).encode("utf-8")
        url = f"{GRAPH_BASE}/users/{urlparse.quote(self.sender, safe='@')}/sendMail"
        bearer = self._bearer()  # token failures surface as their own classes before anything is dispatched
        try:
            status, headers, _body = self._http(url, payload, {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                                                               "Accept": "application/json"}, self._timeout())
        except GraphAmbiguous:
            raise
        except GraphUnavailable as e:  # dispatched, no answer: the provider may have accepted it
            raise GraphAmbiguous(f"sendMail: no response ({e})", e.retry_after) from None
        if status == 202:
            return msg["Message-ID"]  # Graph gives no identifier; this is our correlation value only (see module docstring)
        if status == 401:
            self._forget_token()  # an expired/revoked token is fetched afresh on the next attempt; this attempt stops here
            raise GraphAuthError("sendMail refused: HTTP 401")
        if status == 403:
            raise GraphAuthError("sendMail forbidden for this mailbox: HTTP 403")
        if status == 429 or status >= 500:
            raise GraphUnavailable(f"sendMail unavailable: HTTP {status}", _retry_after(headers))
        raise GraphRejected(f"sendMail rejected: HTTP {status}")
