"""Exact incoming Solana instructions with account-index ownership evidence."""

from src.blockchain.base_adapter import ChainType, TransferEvent
from src.ledger.amounts import atomic_to_decimal

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


class SolanaEvidenceError(ValueError):
    pass


def uint(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SolanaEvidenceError(f"Invalid {name}")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or len(text) > 20:
        raise SolanaEvidenceError(f"Invalid {name}")
    result = int(text)
    if result >= 2**64:
        raise SolanaEvidenceError(f"Invalid {name}")
    return result


def token_accounts(result, owner, decimals_by_mint):
    if not isinstance(result, dict) or not isinstance(result.get("value"), list):
        raise SolanaEvidenceError("Token account response is incomplete")
    accounts = {}
    for row in result["value"]:
        try:
            key = row["pubkey"]
            account = row["account"]
            info = account["data"]["parsed"]["info"]
            mint = info["mint"]
            if mint not in decimals_by_mint:
                continue
            value = info["tokenAmount"]
            if (
                key in accounts
                or account["owner"] != TOKEN_PROGRAM
                or info["owner"] != owner
                or value["decimals"] != decimals_by_mint[mint]
                or info.get("state") != "initialized"
            ):
                raise SolanaEvidenceError("Token account ownership or precision mismatch")
            accounts[key] = (mint, uint(value["amount"], "token amount"))
        except (KeyError, TypeError) as exc:
            raise SolanaEvidenceError("Token account response is incomplete") from exc
    return accounts


def transfers(tx, signature, slot, timestamp, target, tokens, chain="solana", token_filter=None):
    meta = tx.get("meta")
    if not isinstance(meta, dict) or "err" not in meta:
        raise SolanaEvidenceError("Transaction execution evidence is incomplete")
    if meta["err"] is not None:
        return []
    transaction = tx.get("transaction", {})
    if (
        tx.get("slot") != slot
        or not transaction.get("signatures")
        or transaction["signatures"][0] != signature
    ):
        raise SolanaEvidenceError("Transaction signature/slot mismatch")
    message = transaction.get("message", {})
    keys = [
        key.get("pubkey") if isinstance(key, dict) else key
        for key in message.get("accountKeys", [])
    ]
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        raise SolanaEvidenceError("Transaction instructions are incomplete")
    balances = {}
    for row in [*(meta.get("preTokenBalances") or []), *(meta.get("postTokenBalances") or [])]:
        try:
            index = row["accountIndex"]
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(keys):
                raise SolanaEvidenceError("Token account index is invalid")
            identity = (row["mint"], row.get("owner"), row["uiTokenAmount"]["decimals"])
            if keys[index] in balances and balances[keys[index]] != identity:
                raise SolanaEvidenceError("Token account identity changed inside transaction")
            balances[keys[index]] = identity
        except (KeyError, TypeError) as exc:
            raise SolanaEvidenceError("Token balance evidence is incomplete") from exc
    inner = {}
    for row in meta.get("innerInstructions") or []:
        index = row.get("index")
        if index in inner or not isinstance(index, int) or not 0 <= index < len(instructions):
            raise SolanaEvidenceError("Inner instruction index is invalid")
        inner[index] = row.get("instructions", [])
    ordered = [
        item for index, outer in enumerate(instructions) for item in [outer, *inner.get(index, [])]
    ]
    events = []
    for index, instruction in enumerate(ordered):
        program = instruction.get("programId")
        if program not in {TOKEN_PROGRAM, SYSTEM_PROGRAM}:
            continue
        parsed = instruction.get("parsed")
        if not isinstance(parsed, dict):
            raise SolanaEvidenceError("Covered program returned unparsed instructions")
        info = parsed.get("info", {})
        if parsed.get("type") not in {"transfer", "transferChecked"}:
            continue
        destination = info.get("destination")
        source = info.get("source")
        if program == SYSTEM_PROGRAM:
            if destination != target or token_filter is not None or parsed["type"] != "transfer":
                continue
            mint, symbol, decimals, owner = "", "SOL", 9, target
            value = uint(info.get("lamports"), "lamports")
        else:
            bound = balances.get(destination)
            candidate_mint = info.get("mint") or (bound[0] if bound else None)
            if candidate_mint not in tokens or (
                token_filter is not None and candidate_mint not in token_filter
            ):
                continue
            if bound is None or bound[1] is None:
                raise SolanaEvidenceError("Token destination ownership evidence is missing")
            mint, owner, decimals = bound
            symbol, expected_decimals = tokens[mint]
            if owner != target:
                continue
            if decimals != expected_decimals or (info.get("mint") and info["mint"] != mint):
                raise SolanaEvidenceError("Canonical token precision or mint mismatch")
            origin = balances.get(source)
            if origin and (origin[0] != mint or origin[2] != decimals):
                raise SolanaEvidenceError("Source token identity mismatch")
            source = origin[1] if origin and origin[1] else source
            if parsed["type"] == "transferChecked":
                value_info = info.get("tokenAmount", {})
                if value_info.get("decimals") != decimals:
                    raise SolanaEvidenceError("Instruction precision mismatch")
                value = uint(value_info.get("amount"), "token amount")
            else:
                value = uint(info.get("amount"), "token amount")
        if not value or source == owner:
            continue
        events.append(
            TransferEvent(
                signature,
                slot,
                index,
                source,
                owner,
                mint,
                symbol,
                atomic_to_decimal(value, decimals),
                value,
                chain,
                ChainType.SOLANA,
                timestamp,
            )
        )
    return events
