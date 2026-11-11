"""Field validators for staff/customer evidence records (A10(e) exit exceptions, A6(d) booking-specific rent, the customer's
recorded agreement to an exception rent). No dependencies; fail closed: anything blank, malformed or of the wrong type is not
evidence. No pricing policy lives here."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from . import exact_decimal as xd


def clean_text(v, name: str, *, max_len: int = 200) -> str:
    """Non-blank string (whitespace is not a name or a reference)."""
    if not isinstance(v, str):
        raise TypeError(f"{name} must be a string")
    s = v.strip()
    if not s or len(s) > max_len:
        raise ValueError(f"{name} must be a non-blank string of at most {max_len} characters")
    return s


def clean_timestamp(v, name: str) -> str:
    """ISO-8601 date-time WITH a timezone, returned normalised. A bare date or a naive time is not a record timestamp."""
    s = clean_text(v, name, max_len=40)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name} must be an ISO-8601 date-time: {v!r}")
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must carry a timezone: {v!r}")
    if len(s) <= 10:  # defensive: fromisoformat accepts 'YYYY-MM-DD' alone in 3.11+, which the tz check already rejects
        raise ValueError(f"{name} must be a date-time, not a date: {v!r}")
    return dt.isoformat()


def clean_positive_money(v, name: str, *, tier: str = xd.RAW) -> Decimal:
    """Finite, strictly positive exact amount (booleans, NaN/Infinity, zero, negatives and other types rejected). Raw evidence
    amounts use the RAW tier; a helper-computed normal total (rent + A5 charge) uses the AGGREGATE tier (see exact_decimal)."""
    if isinstance(v, bool) or v is None or not isinstance(v, (int, float, str, Decimal)):
        raise TypeError(f"{name} must be an amount")
    try:
        with xd.exact():   # parse with InvalidOperation trapped even if the caller disabled it (no silent NaN)
            d = v if isinstance(v, Decimal) else Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} is not an amount: {v!r}")
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be a finite positive amount: {v!r}")
    try:
        return xd.bounded(d, tier=tier)   # representation limits: reject, never round
    except ValueError as e:
        raise ValueError(f"{name}: {e}")


def fmt_money(d: Decimal) -> str:
    """Presentation only: exact (possibly derived, 7–8 dp) value rounded half-up to the cent as text; comparisons always use
    the exact Decimal and happen before any formatting."""
    if not isinstance(d, Decimal) or not d.is_finite() or d < 0:
        raise ValueError("fmt_money needs a finite non-negative Decimal")
    xd.bounded(d, tier=xd.DERIVED)   # magnitude checked before the text expansion below
    with xd.display():
        return str(d.quantize(Decimal("0.01")))
