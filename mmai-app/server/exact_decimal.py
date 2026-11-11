"""Exact, context-independent Decimal arithmetic for the pure rule helpers (budget, exception rent, lease dates).

Why: Python's Decimal arithmetic rounds to the AMBIENT context precision. With `decimal.getcontext().prec = 3` the sum
1100.01 + 0 becomes 1.10E+3 and 1000 × 1.10 becomes 1.10E+3, so an over-allowance rent passed A16. Verdicts must not depend on
whatever precision the calling thread happens to have, so every helper runs its arithmetic inside EXACT_CTX:

  * precision 40 digits — far above the bounded inputs (≤ MAX_INT_DIGITS integer + MAX_FRACTION_DIGITS decimal digits per
    amount, a handful of operands per calculation);
  * the Inexact signal is TRAPPED — if any operation would have to round, it raises instead of silently producing a rounded
    verdict (presentation rounding uses DISPLAY_CTX explicitly).

Magnitude guard: Decimal('1E+1000000') has one digit and no fraction yet formatting or quantizing it would allocate a
million-character string. `bounded()` checks the true magnitude from sign/exponent/adjusted() BEFORE any arithmetic or text
formatting and canonicalises zero (0E+1000000, -0 → 0). The digit bounds are engineering input limits, not business rules.

Three tiers, so every ACCEPTED input flows through the calculations and their serialisation without artificial rejection,
while nothing is ever rounded before a decision (out-of-bound input is rejected, not rounded):
  * RAW (default): one supplied amount — a rent, a member maximum, an evidence total — ≤ 12 integer and ≤ 6 decimal digits.
  * AGGREGATE (input bound for a whole-apartment maximum or a normal total): a MateBot baseline is the sum of ≤ 7 raw maxima
    (A4 / MAX_PEOPLE → < 7 × 10^12) and a normal total is raw rent + the A5 charge, so this input tier admits ≤ 13 integer /
    ≤ 6 decimal digits. Multiplying an aggregate by 1.10 or 1.20 therefore yields < 1.2 × 10^13 with ≤ 8 decimal places.
  * DERIVED (display/computed bound): closure of the tiers above under the helper operations — ≤ 15 integer / ≤ 8 decimal
    digits. Used only for computed figures (`cents`, `fmt_money`), so every value a helper can produce from accepted inputs
    serialises. It is never an input bound (a 10 dp "maximum" is rejected at the aggregate tier, not rounded later).
"""
from __future__ import annotations

from decimal import Context, Decimal, Inexact, InvalidOperation, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext

MAX_INT_DIGITS = 12        # raw: 12 integer digits is far beyond any ALM rent or stated maximum
MAX_FRACTION_DIGITS = 6    # raw: cents, plus room for fractional inputs that must not be rounded before a decision
MAX_AGGREGATE_INT_DIGITS = 13       # aggregate input: sum of ≤ 7 raw maxima / raw rent + A5 charge
MAX_AGGREGATE_FRACTION_DIGITS = 6   # aggregate input: sums do not add decimal places
MAX_DERIVED_INT_DIGITS = 15         # derived/display: 13 integer digits × 1.2 < 10^14, with headroom
MAX_DERIVED_FRACTION_DIGITS = 8     # derived/display: 6 dp × 1.10 / 1.20 → at most 8 dp

RAW, AGGREGATE, DERIVED = "raw", "aggregate", "derived"
_BOUNDS = {RAW: (MAX_INT_DIGITS, MAX_FRACTION_DIGITS),
           AGGREGATE: (MAX_AGGREGATE_INT_DIGITS, MAX_AGGREGATE_FRACTION_DIGITS),
           DERIVED: (MAX_DERIVED_INT_DIGITS, MAX_DERIVED_FRACTION_DIGITS)}

# Every setting explicit so nothing is inherited from the caller (localcontext() installs a COPY of these; caller flags untouched).
EXACT_CTX = Context(prec=40, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999, capitals=1, clamp=0,
                    traps=[InvalidOperation, Inexact])
DISPLAY_CTX = Context(prec=40, rounding=ROUND_HALF_UP, Emin=-999999, Emax=999999, capitals=1, clamp=0,
                      traps=[InvalidOperation])


def exact():
    """`with exact():` — run the enclosed Decimal arithmetic exactly, independent of the caller's context."""
    return localcontext(EXACT_CTX)


def display():
    """`with display():` — presentation rounding (quantize to cents etc.), never used for decisions."""
    return localcontext(DISPLAY_CTX)


def bounded(d: Decimal, *, tier: str = RAW) -> Decimal:
    """Finite Decimal → canonical, magnitude-checked Decimal. Raises ValueError (callers wrap into their own error types).
    Order matters: finiteness, zero canonicalisation, sign, fractional digits and leading-digit magnitude are all decided
    from the tuple representation before the value is ever expanded. Nothing is ever rounded: out of bounds → reject."""
    if not isinstance(d, Decimal):
        raise TypeError("bounded() needs a Decimal")
    if tier not in _BOUNDS:
        raise ValueError(f"unknown representation tier {tier!r}")
    int_digits, fraction_digits = _BOUNDS[tier]
    if not d.is_finite():
        raise ValueError("not a finite amount")
    sign, _digits, exp = d.as_tuple()
    if d.is_zero():
        return Decimal(0)
    if sign:
        raise ValueError("negative amount")
    frac = -exp if exp < 0 else 0
    if frac > fraction_digits:
        raise ValueError(f"more than {fraction_digits} decimal places ({tier} representation limit)")
    if d.adjusted() >= int_digits:
        raise ValueError(f"more than {int_digits} integer digits ({tier} representation limit)")
    with exact():
        return d.quantize(Decimal(1).scaleb(-frac)) if exp > 0 else +d   # no positive exponent left; value unchanged
