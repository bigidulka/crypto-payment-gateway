"""Strict TON Center v3/TEP-74 evidence; an emitted request is not a receipt."""

import base64

from tonsdk.boc import Cell

from src.blockchain.prepared import ChainEvidenceError
from src.blockchain.ton_keys import normalize_ton_address


def uint(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ChainEvidenceError(f"Invalid TON {name}")
    if isinstance(value, str) and (not value or not value.isdecimal()):
        raise ChainEvidenceError(f"Invalid TON {name}")
    raw = int(value)
    if not 0 <= raw < 2**120:
        raise ChainEvidenceError(f"Invalid TON {name}")
    return raw


def hash_id(value: str) -> str:
    try:
        raw = (
            bytes.fromhex(value)
            if len(value) == 64
            else base64.b64decode(
                value.replace("-", "+").replace("_", "/") + "=" * (-len(value) % 4), validate=True
            )
        )
    except (ValueError, TypeError):
        raise ChainEvidenceError("Invalid TON hash") from None
    if len(raw) != 32:
        raise ChainEvidenceError("Invalid TON hash length")
    return raw.hex()


def successful(tx: dict, head: int, depth: int) -> bool:
    if (
        tx.get("emulated") is True
        or tx.get("finality") not in (None, 2, "finalized")
        or not isinstance(tx.get("mc_block_seqno"), int)
        or head - tx["mc_block_seqno"] + 1 < depth
    ):
        return False
    description = tx.get("description", {})
    message = tx.get("in_msg") or {}
    compute = description.get("compute_ph", {})
    # Native transfers to an uninitialized deposit wallet have no VM phase; the
    # node then reports the transaction as aborted although the credit landed.
    credited_uninit = (
        compute.get("skipped") is True
        and compute.get("reason") == "no_state"
        and tx.get("end_status") in ("uninit", "nonexist")
    )
    if message.get("bounced") is True or (
        description.get("aborted") is not False and not credited_uninit
    ):
        return False
    if compute and not credited_uninit and not (
        compute.get("success") is True and compute.get("exit_code") in (0, 1)
    ):
        return False
    action = description.get("action")
    if action and (
        action.get("success") is not True
        or action.get("result_code") != 0
        or action.get("no_funds") is True
        or action.get("skipped_actions", 0) != 0
    ):
        return False
    return True


def internal_transfer(body: str) -> tuple[int, str, int, str | None]:
    try:
        parsed = Cell.one_from_boc(base64.b64decode(body, validate=True)).begin_parse()
        if parsed.read_uint(32) != 0x178D4519:
            raise ChainEvidenceError("Not a jetton internal_transfer")
        query_id = parsed.read_uint(64)
        amount = parsed.read_coins()
        sender = parsed.read_msg_addr()
        parsed.read_msg_addr()  # response_destination
        parsed.read_coins()  # forward_ton_amount
        payload = parsed.read_ref().begin_parse() if parsed.read_bit() else parsed
        memo = None
        if len(payload.bits) >= 32 and payload.read_uint(32) == 0:
            chunks = bytearray()
            while True:
                while len(payload.bits) >= 8:
                    chunks.append(payload.read_uint(8))
                if not payload.refs:
                    break
                if len(payload.refs) != 1 or len(chunks) > 1024:
                    raise ChainEvidenceError("Invalid TON comment snake")
                payload = payload.read_ref().begin_parse()
            memo = bytes(chunks).decode("utf-8")
        if sender is None or amount <= 0:
            raise ChainEvidenceError("Invalid jetton sender or amount")
        return amount, normalize_ton_address(sender.to_string(False)), query_id, memo
    except (ValueError, TypeError, IndexError, UnicodeError):
        raise ChainEvidenceError("Malformed TON transfer body") from None


def complete_trace(trace: dict, head: int, depth: int) -> bool:
    info = trace.get("trace_info", {})
    return (
        trace.get("is_incomplete") is False
        and info.get("trace_state") == "complete"
        and info.get("pending_messages") == 0
        and uint(trace.get("mc_seqno_end"), "trace block") <= head - depth + 1
    )
