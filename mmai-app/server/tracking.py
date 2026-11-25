"""Google Ads details saved with an enquiry (25 November 2026).

A visitor who arrives from a Google ad carries a click id and campaign names in the web address
(?gclid=...&utm_campaign=...). client/tracking.js keeps them for the visit and the public MateBot and HomeBot forms send them with
the enquiry as ONE optional top-level `tracking` object. They are stored on the enquiry row (same names on `enquiries` and
`homebot_enquiries`) so staff can see which ad an enquiry came from and the click can be matched to the lead later. Nothing here
is personal data, and nothing here is read by matching, scoring, the AI review, introductions or any email.

Contract:
  * Only the six keys in KEYS are ever read; every other key inside `tracking` is ignored.
  * A value must be a string. Control characters are removed, the ends are trimmed, and what is left must be 1-255 characters of
    valid text. Anything else is dropped, never repaired: a longer value is NOT cut short (a click id cut short looks genuine and
    matches nothing), and case and every other character stay exactly as sent.
  * Tracking is optional and informational, so it can never make a submission fail. A missing, malformed or hostile `tracking`
    (not an object, numbers, nested objects, characters the database cannot hold) just means fewer or no values are saved.
  * The route takes `tracking` out of the request body first (app.submit_public / app.submit_homebot), so it never reaches the
    submission fingerprint, answer validation, the stored answers, drafts or the replayed reply: a retry that carries different
    tracking is the same submission, and the first save is never overwritten.
  * `tracking_captured_at` (server time) is set only when at least one value was saved. Columns are otherwise NULL.
"""
from __future__ import annotations

import re

KEYS = ("gclid", "gbraid", "wbraid", "utm_campaign", "utm_ad_group", "utm_keyword")
CAPTURED_AT = "tracking_captured_at"
COLUMNS = KEYS + (CAPTURED_AT,)   # the same nullable TEXT columns on enquiries and homebot_enquiries (db.MIGRATIONS)
MAX_LEN = 255
# C0 and C1 controls (NUL included: PostgreSQL text cannot hold it), DEL, and the two Unicode line separators.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def clean(raw) -> dict:
    """The usable part of a submitted `tracking` value: {key: str} for the keys that pass the contract above, in KEYS order.
    Never raises; anything unusable (including a `raw` that is not a dict) gives {}."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for k in KEYS:
        try:
            v = raw.get(k)
            if not isinstance(v, str):
                continue
            v = _CONTROL.sub("", v).strip()
            if not v or len(v) > MAX_LEN:
                continue
            v.encode("utf-8")   # a lone surrogate (valid in JSON, not in UTF-8) would fail the database write
            out[k] = v
        except Exception:   # noqa: BLE001 - nothing about tracking may ever fail a submission
            continue
    return out


def columns(raw, captured_at: str) -> dict:
    """Column values to store for a submitted `tracking` value: the saved keys plus `tracking_captured_at`, or {} when nothing
    usable was sent (the columns then stay NULL)."""
    kept = clean(raw)
    return {**kept, CAPTURED_AT: captured_at} if kept else {}
