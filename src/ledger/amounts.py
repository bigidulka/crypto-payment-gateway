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


def decimal_to_atomic(value: object, decimals: object, *, allow_zero: bool = False) -> int:
    """Convert a positive Decimal to atomic units without ambient-context rounding.

    ``Decimal * 10**decimals`` is unsafe because multiplication observes the
    current decimal context. This routine instead uses the Decimal coefficient
    and exponent directly and rejects non-integral precision before converting.
    """
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError("amount must be a Decimal")
    if (
        allow_zero
        and value.is_finite()
        and value == 0
        and isinstance(decimals, int)
        and not isinstance(decimals, bool)
        and 0 <= decimals <= 255
    ):
        return 0
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


def atomic_to_decimal(value: int, decimals: int) -> Decimal:
    """Convert nonnegative atomic units without rounding in Decimal context."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value < ATOMIC_AMOUNT_UPPER_BOUND
        or isinstance(decimals, bool)
        or not isinstance(decimals, int)
        or not 0 <= decimals <= 255
    ):
        raise ValueError("invalid atomic amount or precision")
    return Decimal((0, tuple(int(digit) for digit in str(value)), -decimals))


def exact_decimal_sum(*values: Decimal) -> Decimal:
    """Add signed monetary facts in integer units, independent of Decimal context."""
    if not values or any(
        not isinstance(value, Decimal) or not value.is_finite() for value in values
    ):
        raise ValueError("finite Decimal monetary facts are required")
    scale = max(0, max(-value.as_tuple().exponent for value in values))
    if scale > 255:
        raise ValueError("monetary precision exceeds supported bounds")
    total = 0
    for value in values:
        sign, digits, exponent = value.as_tuple()
        if len(digits) + exponent + scale > 78:
            raise ValueError("monetary fact exceeds atomic bounds")
        coefficient = int("".join(str(digit) for digit in digits))
        total += (-1 if sign else 1) * coefficient * 10 ** (exponent + scale)
    if abs(total) >= ATOMIC_AMOUNT_UPPER_BOUND:
        raise ValueError("monetary total exceeds atomic bounds")
    return Decimal((int(total < 0), tuple(int(digit) for digit in str(abs(total))), -scale))
