"""Durable EVM transfers with nonce, receipt, chain and destination binding."""

import base64

from aiohttp import ClientError
from eth_account import Account
from httpx import HTTPError
from web3 import AsyncWeb3, Web3
from web3.exceptions import TransactionNotFound, Web3Exception

from src.blockchain.base_adapter import ChainType, TransferEvent
from src.blockchain.chains import ERC20_ABI, get_transfer_event_signature
from src.blockchain.prepared import (
    ChainEvidenceError,
    ChainRPCError,
    PreparedTransfer,
    TransferReceipt,
)
from src.ledger.amounts import atomic_to_decimal, decimal_to_atomic


class PreparedEvmAdapter:
    def __init__(self, chain, config):
        self.chain, self.config = chain, config
        self.w3 = AsyncWeb3(
            AsyncWeb3.AsyncHTTPProvider(
                config.rpc_url, request_kwargs={"timeout": 30}, exception_retry_configuration=None
            )
        )
        self.tokens = {t.contract_address.lower(): t for t in config.tokens.values()}

    def normalize_address(self, value):
        if not Web3.is_address(value):
            raise ChainEvidenceError("Invalid EVM address")
        return value.lower()

    async def _call(self, request):
        try:
            return await request
        except (Web3Exception, HTTPError, ClientError, TimeoutError, OSError, ValueError):
            raise ChainRPCError("EVM RPC response unavailable") from None

    async def get_latest_block(self):
        return await self._call(self.w3.eth.block_number)

    async def get_native_balance(self, address):
        raw = await self._call(self.w3.eth.get_balance(Web3.to_checksum_address(address), "latest"))
        return atomic_to_decimal(raw, self.config.native_decimals)

    async def get_token_balance(self, address, token):
        token = self.normalize_address(token)
        if token not in self.tokens:
            raise ChainEvidenceError("Canonical ERC20 is not configured")
        contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        raw = await self._call(
            contract.functions.balanceOf(Web3.to_checksum_address(address)).call()
        )
        return atomic_to_decimal(raw, self.tokens[token].decimals)

    async def prepare_transfer(self, private_key, destination, amount, token=None, *, reference=""):
        key = Account.from_key(private_key)
        sender, destination = (
            self.normalize_address(key.address),
            self.normalize_address(destination),
        )
        token = self.normalize_address(token) if token else ""
        if sender == destination or token and token not in self.tokens:
            raise ChainEvidenceError("Invalid EVM destination or token")
        raw = decimal_to_atomic(
            amount, self.tokens[token].decimals if token else self.config.native_decimals
        )
        chain_id = await self._call(self.w3.eth.chain_id)
        if chain_id != self.config.chain_id:
            raise ChainEvidenceError("RPC is connected to another chain")
        checksum = Web3.to_checksum_address(sender)
        nonce = await self._call(self.w3.eth.get_transaction_count(checksum, "pending"))
        if nonce != await self._call(self.w3.eth.get_transaction_count(checksum, "latest")):
            raise ChainEvidenceError("Wallet already has an unresolved transaction")
        tx = {
            "from": checksum,
            "chainId": chain_id,
            "nonce": nonce,
            "to": Web3.to_checksum_address(token or destination),
            "value": 0 if token else raw,
            "data": "0x",
        }
        if token:
            contract = self.w3.eth.contract(address=tx["to"], abi=ERC20_ABI)
            decimals = await self._call(contract.functions.decimals().call())
            if decimals != self.tokens[token].decimals:
                raise ChainEvidenceError("Canonical ERC20 decimals differ")
            if decimal_to_atomic(await self.get_token_balance(sender, token), decimals) < raw:
                raise ChainEvidenceError("Insufficient canonical ERC20 balance")
            tx["data"] = contract.encode_abi(
                "transfer", args=[Web3.to_checksum_address(destination), raw]
            )
        block = await self._call(self.w3.eth.get_block("latest"))
        if "baseFeePerGas" in block:
            tip = await self._call(self.w3.eth.max_priority_fee)
            tx.update(maxPriorityFeePerGas=tip, maxFeePerGas=block["baseFeePerGas"] * 2 + tip)
            gas_price = tx["maxFeePerGas"]
        else:
            gas_price = await self._call(self.w3.eth.gas_price)
            tx["gasPrice"] = gas_price
        estimate = await self._call(self.w3.eth.estimate_gas(tx))
        tx["gas"] = (estimate * 12 + 9) // 10
        fee = tx["gas"] * gas_price
        if fee > self.config.max_gas_cost_wei:
            raise ChainEvidenceError("Network fee exceeds configured cap")
        if (
            decimal_to_atomic(await self.get_native_balance(sender), self.config.native_decimals)
            < fee + tx["value"]
        ):
            raise ChainEvidenceError("Insufficient native coin for fees and value")
        signed = Account.sign_transaction(tx, private_key)
        return PreparedTransfer(
            self.chain,
            Web3.to_hex(signed.hash),
            base64.b64encode(signed.raw_transaction).decode(),
            sender,
            destination,
            token,
            raw,
            fee,
            {"nonce": nonce, "chain_id": chain_id, "data": tx["data"]},
        )

    async def broadcast_prepared(self, prepared):
        try:
            tx_hash = await self._call(
                self.w3.eth.send_raw_transaction(base64.b64decode(prepared.payload, validate=True))
            )
        except ChainRPCError:
            return TransferReceipt("unknown", prepared.tx_id, reason="broadcast_outcome_unknown")
        if Web3.to_hex(tx_hash) != prepared.tx_id:
            return TransferReceipt("unknown", prepared.tx_id, reason="transaction_hash_mismatch")
        return TransferReceipt("pending", prepared.tx_id)

    async def transfer_receipt(self, prepared):
        if await self._call(self.w3.eth.chain_id) != prepared.context["chain_id"]:
            raise ChainEvidenceError("Receipt RPC chain differs")
        try:
            receipt = await self.w3.eth.get_transaction_receipt(prepared.tx_id)
            tx = await self.w3.eth.get_transaction(prepared.tx_id)
        except TransactionNotFound:
            stable = (
                await self.get_latest_block()
                - max(self.config.confirmations, self.config.reorg_buffer)
                + 1
            )
            if (
                stable >= 0
                and await self._call(
                    self.w3.eth.get_transaction_count(
                        Web3.to_checksum_address(prepared.sender), stable
                    )
                )
                > prepared.context["nonce"]
            ):
                # Our transaction could be present in an unavailable history.
                # Preserve its money reserve while allowing use of the next nonce.
                return TransferReceipt(
                    "reconciliation_required",
                    prepared.tx_id,
                    reason="nonce_consumed_unreconciled",
                    wallet_released=True,
                )
            return TransferReceipt("unknown", prepared.tx_id, reason="receipt_not_observed")
        except (Web3Exception, ClientError, TimeoutError, OSError):
            raise ChainRPCError("EVM receipt is unavailable") from None
        head = await self.get_latest_block()
        block = await self._call(self.w3.eth.get_block(receipt["blockNumber"]))
        if block["hash"] != receipt["blockHash"] or head - receipt["blockNumber"] + 1 < max(
            self.config.confirmations, self.config.reorg_buffer
        ):
            return TransferReceipt("pending", prepared.tx_id)
        if (
            Web3.to_hex(tx["hash"]) != prepared.tx_id
            or tx["chainId"] != prepared.context["chain_id"]
            or self.normalize_address(tx["from"]) != prepared.sender
            or self.normalize_address(tx["to"]) != (prepared.token or prepared.destination)
            or tx["nonce"] != prepared.context["nonce"]
            or Web3.to_hex(tx["input"]) != prepared.context["data"]
        ):
            raise ChainEvidenceError("EVM signed transaction binding differs")
        if receipt["status"] != 1:
            fee = receipt["gasUsed"] * receipt["effectiveGasPrice"]
            return TransferReceipt(
                "failed", prepared.tx_id, fee, reason="chain_execution_failed", native_cost_raw=fee
            )
        if prepared.token:
            total = 0
            for row in receipt["logs"]:
                topics = row["topics"]
                if self.normalize_address(row["address"]) != prepared.token or len(topics) != 3:
                    continue
                if Web3.to_hex(topics[0]) != get_transfer_event_signature():
                    continue
                if (
                    "0x" + bytes(topics[1])[-20:].hex() == prepared.sender
                    and "0x" + bytes(topics[2])[-20:].hex() == prepared.destination
                ):
                    total += int.from_bytes(bytes(row["data"]), "big")
        else:
            total = tx["value"]
        if total != prepared.amount_raw:
            raise ChainEvidenceError("EVM actual transferred amount differs")
        return TransferReceipt(
            "completed", prepared.tx_id, receipt["gasUsed"] * receipt["effectiveGasPrice"]
        )

    async def get_transfer_events(self, lower, upper, addresses, token_addresses=None):
        watched = {self.normalize_address(a) for a in addresses}
        events = []
        tokens = token_addresses if token_addresses is not None else list(self.tokens)
        for owner in sorted(watched):
            topic = "0x" + owner[2:].rjust(64, "0")
            rows = await self._call(
                self.w3.eth.get_logs(
                    {
                        "fromBlock": lower,
                        "toBlock": upper,
                        "address": [Web3.to_checksum_address(t) for t in tokens],
                        "topics": [get_transfer_event_signature(), None, topic],
                    }
                )
            )
            for row in rows:
                token = self.tokens[self.normalize_address(row["address"])]
                if row.get("removed") or len(row["topics"]) != 3 or len(row["data"]) != 32:
                    raise ChainEvidenceError("Malformed canonical Transfer log")
                tx_hash = Web3.to_hex(row["transactionHash"])
                receipt = await self._call(self.w3.eth.get_transaction_receipt(tx_hash))
                block = await self._call(self.w3.eth.get_block(row["blockNumber"]))
                if (
                    receipt["status"] != 1
                    or receipt["blockHash"] != row["blockHash"]
                    or block["hash"] != row["blockHash"]
                ):
                    raise ChainEvidenceError("Transfer log lacks canonical successful receipt")
                raw = int.from_bytes(row["data"], "big")
                source = "0x" + bytes(row["topics"][1])[-20:].hex()
                if raw > 0 and source != owner:
                    events.append(
                        TransferEvent(
                            tx_hash,
                            row["blockNumber"],
                            row["logIndex"],
                            source,
                            owner,
                            token.contract_address.lower(),
                            token.symbol,
                            atomic_to_decimal(raw, token.decimals),
                            raw,
                            self.chain,
                            ChainType.EVM,
                            block["timestamp"],
                        )
                    )
        # Native transfers use the successful transaction's exact value, never current balance.
        for number in range(lower, upper + 1):
            block = await self._call(self.w3.eth.get_block(number, full_transactions=True))
            for tx in block["transactions"]:
                if (
                    not tx.get("to")
                    or self.normalize_address(tx["to"]) not in watched
                    or not tx["value"]
                ):
                    continue
                receipt = await self._call(self.w3.eth.get_transaction_receipt(tx["hash"]))
                if receipt["status"] != 1 or receipt["blockHash"] != block["hash"]:
                    continue
                events.append(
                    TransferEvent(
                        Web3.to_hex(tx["hash"]),
                        number,
                        -1,
                        self.normalize_address(tx["from"]),
                        self.normalize_address(tx["to"]),
                        "",
                        self.config.native_symbol,
                        atomic_to_decimal(tx["value"], self.config.native_decimals),
                        tx["value"],
                        self.chain,
                        ChainType.EVM,
                        block["timestamp"],
                    )
                )
        return sorted(events, key=lambda e: (e.block_number, e.tx_hash, e.log_index))

    async def close(self):
        await self.w3.provider.disconnect()
