"""Real SDK signatures and execution in Solana LiteSVM and TON sandbox (no live money)."""

import base64
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from nacl.signing import VerifyKey
from solders.account import Account
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.transaction_metadata import FailedTransactionMetadata

from src.blockchain.chains import get_chain_config
from src.blockchain.prepared import ChainEvidenceError, ChainRPCError
from src.blockchain.solana_adapter import SPL_TOKEN_PROGRAM_ID, SolanaAdapter
from src.blockchain.ton_adapter import TonAdapter
from src.blockchain.ton_evidence import successful
from src.blockchain.ton_keys import wallet_from_seed


class SolanaVMRPC:
    def __init__(self):
        self.vm = LiteSVM()
        self.sender = Keypair.from_seed(bytes(32))
        self.recipient = Keypair.from_seed(bytes([1]) * 32)
        self.mint = get_chain_config("solana").tokens["USDC"].contract_address
        self.program = Pubkey.from_string(SPL_TOKEN_PROGRAM_ID)
        mint = bytes(36) + (9000000).to_bytes(8, "little") + bytes([6, 1]) + bytes(36)
        self.vm.set_account(Pubkey.from_string(self.mint), Account(100000000, mint, self.program))
        self.accounts = [Keypair.from_seed(bytes([i]) * 32).pubkey() for i in (2, 3)]
        for account, raw in zip(self.accounts, (2000000, 3000000)):
            data = (
                bytes(Pubkey.from_string(self.mint))
                + bytes(self.sender.pubkey())
                + raw.to_bytes(8, "little")
                + bytes(36)
                + b"\x01"
                + bytes(56)
            )
            assert len(data) == 165
            self.vm.set_account(account, Account(100000000, data, self.program))
        self.vm.airdrop(self.sender.pubkey(), 1000000000)
        self.posts = 0

    def parsed(self, address):
        account = self.vm.get_account(Pubkey.from_string(address))
        if account is None:
            return None
        if address == self.mint:
            return {
                "owner": str(self.program),
                "data": {"parsed": {"type": "mint", "info": {"decimals": 6}}},
            }
        data = account.data
        return {
            "owner": str(account.owner),
            "data": {
                "parsed": {
                    "type": "account",
                    "info": {
                        "owner": str(Pubkey(data[32:64])),
                        "mint": str(Pubkey(data[:32])),
                        "state": "initialized",
                        "tokenAmount": {
                            "amount": str(int.from_bytes(data[64:72], "little")),
                            "decimals": 6,
                        },
                    },
                }
            },
        }

    async def call(self, method, params):
        if method == "getTokenAccountsByOwner":
            return {
                "value": [{"pubkey": str(a), "account": self.parsed(str(a))} for a in self.accounts]
            }
        if method == "getAccountInfo":
            return {"value": self.parsed(params[0])}
        if method == "getLatestBlockhash":
            return {
                "context": {"slot": 1},
                "value": {
                    "blockhash": str(self.vm.latest_blockhash()),
                    "lastValidBlockHeight": 1000,
                },
            }
        if method == "getFeeForMessage":
            return {"value": 5000}
        if method == "getMinimumBalanceForRentExemption":
            return self.vm.minimum_balance_for_rent_exemption(params[0])
        if method == "getBalance":
            return {"value": self.vm.get_balance(Pubkey.from_string(params[0])) or 0}
        if method == "simulateTransaction":
            result = self.vm.simulate_transaction(
                Transaction.from_bytes(base64.b64decode(params[0]))
            )
            return {
                "value": {
                    "err": "execution_failed"
                    if isinstance(result, FailedTransactionMetadata)
                    else None
                }
            }
        if method == "sendTransaction":
            self.posts += 1
            tx = Transaction.from_bytes(base64.b64decode(params[0]))
            result = self.vm.send_transaction(tx)
            if isinstance(result, FailedTransactionMetadata):
                raise ChainRPCError("VM rejected transaction")
            return str(tx.signatures[0])
        raise AssertionError(method)


async def test_real_spl_split_accounts_ata_signature_execution_and_replay():
    rpc = SolanaVMRPC()
    adapter = SolanaAdapter(config=get_chain_config("solana"))
    adapter._rpc_call = rpc.call
    prepared = await adapter.prepare_transfer(
        str(rpc.sender), str(rpc.recipient.pubkey()), Decimal("4"), rpc.mint
    )
    transaction = Transaction.from_bytes(base64.b64decode(prepared.payload))
    transaction.verify()
    assert (await adapter.broadcast_prepared(prepared)).status == "pending"
    ata = await adapter._get_associated_token_address(str(rpc.recipient.pubkey()), rpc.mint)
    assert (
        int.from_bytes(rpc.vm.get_account(Pubkey.from_string(ata)).data[64:72], "little") == 4000000
    )
    balances = [int.from_bytes(rpc.vm.get_account(a).data[64:72], "little") for a in rpc.accounts]
    assert sum(balances) == 1000000
    assert (await adapter.broadcast_prepared(prepared)).status == "unknown"
    assert (
        int.from_bytes(rpc.vm.get_account(Pubkey.from_string(ata)).data[64:72], "little") == 4000000
    )


async def test_solana_missing_fee_and_failed_simulation_cannot_broadcast():
    rpc = SolanaVMRPC()
    adapter = SolanaAdapter(config=get_chain_config("solana"))
    adapter._rpc_call = rpc.call
    with pytest.raises(ChainEvidenceError):
        await adapter.prepare_transfer(
            str(rpc.sender), str(rpc.recipient.pubkey()), Decimal("6"), rpc.mint
        )
    assert rpc.posts == 0


async def test_second_spl_sweep_needs_system_rent_even_with_existing_treasury_ata():
    rpc = SolanaVMRPC()
    adapter = SolanaAdapter(config=get_chain_config("solana"))
    adapter._rpc_call = rpc.call
    treasury = str(rpc.recipient.pubkey())
    first = await adapter.prepare_transfer(str(rpc.sender), treasury, Decimal("4"), rpc.mint)
    await adapter.broadcast_prepared(first)
    funder = rpc.sender
    deposit = Keypair.from_seed(bytes([12]) * 32)
    # LiteSVM permits system dust. Retain the RPC rent reserve so both sweeps
    # leave a funded system account across validator rent configurations.
    minimum = await adapter.native_rent_reserve()
    funding = await adapter.prepare_transfer(
        str(funder), str(deposit.pubkey()), Decimal(minimum + 5000) / Decimal(10**9)
    )
    assert (await adapter.broadcast_prepared(funding)).status == "pending"
    for account in rpc.accounts:
        old = rpc.vm.get_account(account)
        data = bytearray(old.data)
        data[32:64] = bytes(deposit.pubkey())
        rpc.vm.set_account(account, Account(old.lamports, bytes(data), old.owner))
    rpc.sender = deposit
    second = await adapter.prepare_transfer(str(deposit), treasury, Decimal("1"), rpc.mint)
    assert (await adapter.broadcast_prepared(second)).status == "pending"
    assert rpc.vm.get_balance(deposit.pubkey()) == minimum
    ata = await adapter._get_associated_token_address(treasury, rpc.mint)
    assert (
        int.from_bytes(rpc.vm.get_account(Pubkey.from_string(ata)).data[64:72], "little") == 5000000
    )


async def test_solana_expiry_releases_only_with_retained_finalized_history():
    from src.blockchain.prepared import PreparedTransfer

    prepared = PreparedTransfer(
        "solana",
        str(Keypair.from_seed(bytes(32)).sign_message(b"test")),
        "bytes",
        str(Keypair.from_seed(bytes(32)).pubkey()),
        str(Keypair.from_seed(bytes([1]) * 32).pubkey()),
        "",
        1,
        5000,
        {"last_valid_block_height": 100, "signing_slot": 10},
    )
    adapter = SolanaAdapter(config=get_chain_config("solana"))
    values = {
        "getSignatureStatuses": {"value": [None]},
        "getBlockHeight": 101,
        "minimumLedgerSlot": 1,
        "getTransaction": None,
    }

    async def rpc(method, params):
        return values[method]

    adapter._rpc_call = rpc
    assert (await adapter.transfer_receipt(prepared)).status == "failed"
    values["minimumLedgerSlot"] = 11
    assert (await adapter.transfer_receipt(prepared)).status == "unknown"
    await adapter.close()


@pytest.mark.parametrize("version", ["v3r2", "v4r2"])
def test_ton_state_init_signature_deploy_receive_and_replay(version, tmp_path):
    wallet = wallet_from_seed("00" * 32, version)
    destination = "0:" + "11" * 32
    message = wallet.create_transfer_message(destination, 100000000, 0, send_mode=1)
    VerifyKey(wallet.options["public_key"]).verify(
        message["signing_message"].bytes_hash(), message["signature"]
    )
    root = Path(__file__).parent / "ton_runtime"
    artifact = tmp_path / f"ton-sandbox-{version}.json"
    artifact.write_text(
        json.dumps(
            {
                "version": version,
                "address": wallet.address.to_string(False),
                "destination": destination,
                "public": wallet.options["public_key"].hex(),
                "boc": base64.b64encode(message["message"].to_boc(False)).decode(),
            }
        )
    )
    result = subprocess.run(
        ["node", str(root / "wallet-boc.mjs"), str(artifact)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert json.loads(result.stdout.splitlines()[-1])["replayRejected"]


async def test_ton_adapter_signed_jetton_transfer_executes_in_independent_vm(tmp_path):
    from dataclasses import replace

    from tonsdk.boc import Cell
    from tonsdk.contract.token.ft import JettonWallet
    from tonsdk.utils import Address

    owner = wallet_from_seed("00" * 32)
    destination = wallet_from_seed("01" * 32).address.to_string(False)
    master = "0:" + "33" * 32
    code = Cell.one_from_boc(JettonWallet.code)

    def wallet_data(address, balance):
        data = Cell()
        data.bits.write_grams(balance)
        data.bits.write_address(Address(address))
        data.bits.write_address(Address(master))
        data.refs.append(code)
        return data

    def wallet_address(data):
        state = Cell()
        for bit in (0, 0, 1, 1, 0):
            state.bits.write_bit(bit)
        state.refs.extend([code, data])
        return "0:" + state.bytes_hash().hex()

    def b64(cell):
        return base64.b64encode(cell.to_boc(False)).decode()

    initial, amount = 100000000, 40123456
    source_init = wallet_data(owner.address.to_string(False), 0)
    recipient_init = wallet_data(destination, 0)
    source = wallet_address(source_init)
    config = get_chain_config("ton")
    config = replace(
        config, tokens={"USDT": replace(config.tokens["USDT"], contract_address=master)}
    )
    adapter = TonAdapter(config=config)
    adapter._api_call = AsyncMock(return_value={"account_state": "uninit"})
    adapter._get_jetton_wallet_address = AsyncMock(return_value=source)
    adapter.get_token_balance = AsyncMock(return_value=Decimal("100"))
    adapter.get_native_balance = AsyncMock(return_value=Decimal("10"))
    adapter._request = AsyncMock(
        return_value={
            "source_fees": {
                "in_fwd_fee": 1000000,
                "storage_fee": 1000000,
                "gas_fee": 1000000,
                "fwd_fee": 1000000,
            }
        }
    )
    prepared = await adapter.prepare_transfer(
        "00" * 32, destination, Decimal("40.123456"), master, reference="invoice-test-reference"
    )
    artifact = tmp_path / "jetton-execution.json"
    artifact.write_text(
        json.dumps(
            {
                "owner": owner.address.to_string(False),
                "destination": destination,
                "public": owner.options["public_key"].hex(),
                "code": b64(code),
                "sourceInit": b64(source_init),
                "sourceData": b64(wallet_data(owner.address.to_string(False), initial)),
                "recipientInit": b64(recipient_init),
                "source": source,
                "recipient": wallet_address(recipient_init),
                "initial": str(initial),
                "amount": str(amount),
                "boc": prepared.payload,
            }
        )
    )
    result = subprocess.run(
        ["node", str(Path(__file__).parent / "ton_runtime/jetton-boc.mjs"), str(artifact)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    await adapter.close()
    assert result.returncode == 0, result.stderr[-3000:]
    proof = json.loads(result.stdout.splitlines()[-1])
    assert (
        proof["jettonAmount"] == str(amount)
        and proof["destinationExecuted"]
        and proof["replayRejected"]
    )


def test_ton_success_requires_execution_inclusion_and_no_bounce():
    tx = {
        "emulated": False,
        "finality": 2,
        "mc_block_seqno": 50,
        "end_status": "active",
        "description": {
            "aborted": False,
            "compute_ph": {"success": True, "exit_code": 0},
            "action": {"success": True, "result_code": 0, "no_funds": False},
        },
        "in_msg": {"bounced": False},
    }
    assert successful(tx, 70, 12)
    assert successful(dict(tx, finality="finalized"), 70, 12)
    for modified in [
        dict(tx, finality=0),
        dict(tx, mc_block_seqno=69),
        dict(tx, description={"aborted": True}),
        dict(tx, in_msg={"bounced": True}),
    ]:
        assert not successful(modified, 70, 12)


async def test_ton_jetton_requires_completed_destination_receipt():
    adapter = TonAdapter(config=get_chain_config("ton"))
    wallet = wallet_from_seed("00" * 32)
    master = next(iter(adapter._tokens))
    row = {
        "transaction_aborted": False,
        "destination": wallet.address.to_string(False),
        "source": "0:" + "11" * 32,
        "source_wallet": "0:" + "22" * 32,
        "jetton_master": master,
        "transaction_hash": "aa" * 32,
        "amount": "1000000",
        "query_id": "1",
    }
    adapter._trace = AsyncMock(
        return_value={
            "is_incomplete": False,
            "trace_info": {"trace_state": "complete", "pending_messages": 0},
            "mc_seqno_end": "50",
            "transactions": {},
        }
    )
    adapter._get_jetton_wallet_address = AsyncMock(side_effect=["0:" + "33" * 32, "0:" + "22" * 32])
    with pytest.raises(ChainEvidenceError, match="destination jetton receipt"):
        await adapter._jetton_event(row, wallet.address.to_string(False), master, 70)
    await adapter.close()


async def test_ton_async_receipt_across_scan_boundary_uses_initiating_block():
    from tonsdk.boc import Cell
    from tonsdk.utils import Address

    adapter = TonAdapter(config=get_chain_config("ton"))
    master = next(iter(adapter._tokens))
    source, destination = "0:" + "11" * 32, "0:" + "44" * 32
    source_wallet, destination_wallet = "0:" + "22" * 32, "0:" + "33" * 32
    body = Cell()
    body.bits.write_uint(0x178D4519, 32)
    body.bits.write_uint(1, 64)
    body.bits.write_coins(1000000)
    body.bits.write_address(Address(source))
    body.bits.write_address(Address(source))
    body.bits.write_coins(1)
    body.bits.write_bit(0)
    description = {
        "aborted": False,
        "compute_ph": {"success": True, "exit_code": 0},
        "action": {"success": True, "result_code": 0, "no_funds": False},
    }
    root = {
        "hash": "55" * 32,
        "account": source_wallet,
        "mc_block_seqno": 50,
        "now": 500,
        "finality": "finalized",
        "description": description,
        "in_msg": {"bounced": False},
    }
    receipt = {
        "hash": "66" * 32,
        "account": destination_wallet,
        "mc_block_seqno": 51,
        "now": 501,
        "finality": "finalized",
        "description": description,
        "in_msg": {
            "bounced": False,
            "source": source_wallet,
            "message_content": {"body": base64.b64encode(body.to_boc(False)).decode()},
        },
    }
    row = {
        "transaction_hash": root["hash"],
        "source_wallet": source_wallet,
        "source": source,
        "destination": destination,
        "jetton_master": master,
        "transaction_aborted": False,
        "amount": "1000000",
        "query_id": "1",
    }
    adapter._trace = AsyncMock(
        return_value={
            "is_incomplete": False,
            "trace_info": {"trace_state": "complete", "pending_messages": 0},
            "mc_seqno_end": 51,
            "transactions": {"root": root, "receipt": receipt},
        }
    )
    adapter._get_jetton_wallet_address = AsyncMock(side_effect=[destination_wallet, source_wallet])
    adapter.get_latest_block = AsyncMock(return_value=70)
    adapter.get_block_timestamp = AsyncMock(side_effect=lambda block: block * 10)

    async def pages(endpoint, key, query):
        return [row] if endpoint == "jetton/transfers" else []

    adapter._pages = pages
    events = await adapter.get_transfer_events(50, 50, [destination])
    assert len(events) == 1 and events[0].block_number == 51 and events[0].scan_block_number == 50
    assert events[0].raw_amount == 1000000
    await adapter.close()
