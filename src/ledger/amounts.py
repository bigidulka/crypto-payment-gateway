"""Exact atomic amount bounds shared by ledger runtime, ORM, and migration."""

ATOMIC_AMOUNT_UPPER_BOUND = 10**78
ATOMIC_AMOUNT_UPPER_BOUND_SQL = str(ATOMIC_AMOUNT_UPPER_BOUND)
ATOMIC_AMOUNT_CHECK_SQL = (
    "CAST(amount_atomic AS TEXT) <> 'NaN' "
    "AND amount_atomic > 0 "
    f"AND amount_atomic < {ATOMIC_AMOUNT_UPPER_BOUND_SQL} "
    "AND amount_atomic = trunc(amount_atomic)"
)
