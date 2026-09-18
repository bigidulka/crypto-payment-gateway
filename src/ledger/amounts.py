"""Exact atomic amount bounds shared by ledger runtime, ORM, and migration."""

from decimal import Decimal

ATOMIC_AMOUNT_UPPER_BOUND = 10**78
ATOMIC_AMOUNT_UPPER_BOUND_SQL = str(ATOMIC_AMOUNT_UPPER_BOUND)
ATOMIC_AMOUNT_CHECK_SQL = (
    "CAST(amount_atomic AS TEXT) <> 'NaN' "
    "AND amount_atomic > 0 "
    f"AND amount_atomic < {ATOMIC_AMOUNT_UPPER_BOUND_SQL} "
    "AND amount_atomic = trunc(amount_atomic)"
)


def decimal_to_atomic(value: object, decimals: object) -> int:
    """Convert a positive Decimal to atomic units without ambient-context rounding.

    ``Decimal * 10**decimals`` is unsafe because multiplication observes the
    current decimal context. This routine instead uses the Decimal coefficient
    and exponent directly and rejects non-integral precision before converting.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError("amount must be a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError("amount must be positive and finite")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError("atomic decimals must be an integer in range 0..255")
    sign, digits, exponent = value.as_tuple()
    if sign or not digits:
        raise ValueError("amount must be positive and finite")
    normalized_digits = list(digits)
    while len(normalized_digits) > 1 and normalized_digits[-1] == 0:
        normalized_digits.pop()
        exponent += 1
    if normalized_digits == [0]:
        raise ValueError("amount must be positive and finite")
    atomic_exponent = exponent + decimals
    if atomic_exponent < 0:
        # Trailing zeros were removed above. A remaining negative exponent
        # therefore represents fractional atomic precision exactly, not a
        # context-rounded value that could be silently truncated.
        raise ValueError("amount has unsupported atomic precision")
    atomic_digits = len(normalized_digits) + atomic_exponent
    if atomic_digits > 78:
        raise ValueError("atomic amount must be in range 1..10**78-1")
    coefficient = int("".join(str(digit) for digit in normalized_digits))
    atomic = coefficient * 10**atomic_exponent
    if not 0 < atomic < ATOMIC_AMOUNT_UPPER_BOUND:
        raise ValueError("atomic amount must be in range 1..10**78-1")
    return atomic
