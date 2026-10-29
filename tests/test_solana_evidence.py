"""Financial Solana read paths, without an SDK, RPC network or validator claim."""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.blockchain.chains import ChainType, TokenConfig, get_chain_config
from src.blockchain.solana_adapter import SolanaAdapter
from src.blockchain.solana_evidence import TOKEN_PROGRAM, SolanaEvidenceError, transfers

MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


def config():
    return replace(
        get_chain_config("base"),
        chain_type=ChainType.SOLANA,
        native_symbol="SOL",
        native_decimals=9,
        scanner_max_pages_per_address=2,
        tokens={"USDT": TokenConfig("USDT", MINT, 6)},
    )


def transaction():
    def balance(index, owner):
        return {
            "accountIndex": index,
            "owner": owner,
            "mint": MINT,
            "uiTokenAmount": {"amount": "0", "decimals": 6},
        }
    return {
        "slot": 50,
        "transaction": {
            "signatures": ["sig1"],
            "message": {
                "accountKeys": ["source", "destination", "other"],
                "instructions": [
                    {
                        "programId": TOKEN_PROGRAM,
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": "source",
                                "destination": "destination",
                                "mint": MINT,
                                "tokenAmount": {"amount": "1234567", "decimals": 6},
                            },
                        },
                    }
                ],
            },
        },
        "meta": {
            "err": None,
            "preTokenBalances": [balance(0, "sender")],
            "postTokenBalances": [balance(1, "owner"), balance(2, "other-owner")],
            "innerInstructions": [],
        },
    }


def parse(tx, owner="owner"):
    return transfers(tx, "sig1", 50, 100, owner, {MINT: ("USDT", 6)})


def test_outer_and_inner_transfers_have_exact_owned_destination_and_unique_indices():
    tx = transaction()
    outer = tx["transaction"]["message"]["instructions"][0]
    tx["meta"]["innerInstructions"] = [{"index": 0, "instructions": [deepcopy(outer)]}]
    result = parse(tx)
    assert len(result) == 2 and len({e.log_index for e in result}) == 2
    assert all(e.to_address == "owner" and e.from_address == "sender" for e in result)
    assert all(e.amount == Decimal("1.234567") and e.raw_amount == 1234567 for e in result)
    assert parse(tx, "other-owner") == []


def test_unrelated_owner_in_post_balances_never_overrides_destination():
    tx = transaction()
    tx["meta"]["postTokenBalances"][0]["owner"] = "someone-else"
    tx["meta"]["postTokenBalances"][1]["owner"] = "owner"
    assert parse(tx) == []


def test_failed_transaction_and_spoofed_program_never_credit():
    tx = transaction()
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert parse(tx) == []
    tx = transaction()
    tx["transaction"]["message"]["instructions"][0]["programId"] = "spoofed"
    assert parse(tx) == []


@pytest.mark.parametrize("change", ["signature", "precision", "owner"])
def test_incomplete_or_conflicting_finalized_evidence_is_a_failure(change):
    tx = transaction()
    if change == "signature":
        tx["transaction"]["signatures"] = ["unrelated"]
    elif change == "precision":
        tx["transaction"]["message"]["instructions"][0]["parsed"]["info"]["tokenAmount"][
            "decimals"
        ] = 9
    else:
        tx["meta"]["postTokenBalances"][0].pop("owner")
    with pytest.raises(SolanaEvidenceError):
        parse(tx)


def account(key, amount):
    return {
        "pubkey": key,
        "account": {
            "owner": TOKEN_PROGRAM,
            "data": {
                "parsed": {
                    "info": {
                        "owner": "owner",
                        "mint": MINT,
                        "state": "initialized",
                        "tokenAmount": {"amount": amount, "decimals": 6},
                    }
                }
            },
        },
    }


@pytest.mark.asyncio
async def test_balance_sums_all_token_accounts_and_null_is_unknown():
    adapter = SolanaAdapter(config=config(), rpc_url="http://unused.invalid")
    adapter._rpc_call = AsyncMock(
        return_value={"value": [account("a1", "1000000"), account("a2", "234567")]}
    )
    assert await adapter.get_token_balance("owner", MINT) == Decimal("1.234567")
    adapter._rpc_call = AsyncMock(return_value=None)
    with pytest.raises(SolanaEvidenceError):
        await adapter.get_token_balance("owner", MINT)
    with pytest.raises(SolanaEvidenceError):
        await adapter.get_native_balance("owner")


@pytest.mark.asyncio
async def test_signature_pages_include_token_accounts_and_use_before_cursor():
    adapter = SolanaAdapter(config=config(), rpc_url="http://unused.invalid")
    adapter._get_associated_token_address = AsyncMock(return_value="known-ata")
    calls = []

    async def rpc(method, params):
        calls.append((method, params))
        if method == "getSlot":
            return 100
        if method == "getTokenAccountsByOwner":
            return {"value": [account("a1", "0"), account("a2", "0")]}
        if method == "getSignaturesForAddress":
            address, options = params
            assert options["commitment"] == "finalized"
            if address != "a2":
                return []
            if "before" not in options:
                return [
                    {"signature": f"later-{i}", "slot": 90, "err": {"failed": True}}
                    for i in range(1000)
                ]
            assert options["before"] == "later-999"
            return [{"signature": "sig1", "slot": 50, "err": None}]
        if method == "getTransaction":
            assert params[1]["commitment"] == "finalized"
            return transaction()
        raise AssertionError(method)

    adapter._rpc_call = rpc
    events = await adapter.get_transfer_events(40, 100, ["owner"], [MINT])
    assert len(events) == 1 and events[0].tx_hash == "sig1"
    addresses = {params[0] for method, params in calls if method == "getSignaturesForAddress"}
    assert addresses == {"owner", "a1", "a2", "known-ata"}


@pytest.mark.asyncio
async def test_unavailable_transaction_or_incomplete_pagination_cannot_advance():
    adapter = SolanaAdapter(
        config=replace(config(), scanner_max_pages_per_address=1), rpc_url="http://unused.invalid"
    )
    adapter._get_associated_token_address = AsyncMock(return_value="known-ata")

    async def rpc(method, params):
        if method == "getSlot":
            return 100
        if method == "getTokenAccountsByOwner":
            return {"value": []}
        if method == "getSignaturesForAddress":
            return [
                {"signature": f"s{i}", "slot": 90, "err": {"failed": True}} for i in range(1000)
            ]
        raise AssertionError(method)

    adapter._rpc_call = rpc
    with pytest.raises(SolanaEvidenceError, match="checkpoint must not advance"):
        await adapter.get_transfer_events(40, 100, ["owner"], [MINT])


@pytest.mark.asyncio
async def test_failed_finalized_signature_never_confirms_a_send():
    adapter = SolanaAdapter(config=config(), rpc_url="http://unused.invalid")
    adapter._rpc_call = AsyncMock(
        return_value={"value": [{"confirmationStatus": "finalized", "err": {"failed": True}}]}
    )
    assert not await adapter.is_tx_confirmed("sig1")
