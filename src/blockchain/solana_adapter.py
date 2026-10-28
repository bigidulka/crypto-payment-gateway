"""
Solana Adapter - адаптер для работы с Solana blockchain.

Поддерживает:
- Получение балансов SOL и SPL токенов
- Отслеживание Transfer событий
- Отправка транзакций (sweep)

Использует solana-py для взаимодействия с RPC.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import base58
import httpx

from src.blockchain.base_adapter import (
    BaseAdapter,
    ChainType,
    FeeEstimate,
    TransactionResult,
    TransferEvent,
)
from src.blockchain.chains import ChainConfig, get_chain_config
from src.core.config import get_settings

logger = logging.getLogger(__name__)

# SPL Token Program ID
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# Token-2022 Program ID (для USDC и новых токенов)
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@dataclass
class SolanaTransferInfo:
    """Информация о Solana transfer."""

    signature: str
    slot: int
    block_time: int | None
    from_address: str
    to_address: str
    mint: str  # Token mint address
    amount: int  # Raw amount
    decimals: int


class SolanaAdapter(BaseAdapter[ChainConfig]):
    """
    Адаптер для работы с Solana blockchain.

    Использует JSON-RPC API для взаимодействия с нодой.
    """

    def __init__(
        self,
        chain: str = "solana",
        config: ChainConfig | None = None,
        rpc_url: str | None = None,
    ) -> None:
        if config is None:
            config = get_chain_config(chain)

        super().__init__(chain, config)

        # Получаем RPC URL
        if rpc_url is None:
            settings = get_settings()
            rpc_url = getattr(settings, "solana_rpc_url", None) or config.rpc_url

        self.rpc_url = rpc_url
        self._client: httpx.AsyncClient | None = None

        # Кэш для mint -> decimals
        self._decimals_cache: dict[str, int] = {
            token.contract_address: token.decimals for token in config.tokens.values()
        }

        logger.info("Initialized SolanaAdapter")

    @property
    def chain_type(self) -> ChainType:
        return ChainType.SOLANA

    @property
    def native_symbol(self) -> str:
        return "SOL"

    @property
    def address_length(self) -> int:
        return 44  # Base58 encoded

    async def _get_client(self) -> httpx.AsyncClient:
        """Получить HTTP клиент."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def _rpc_call(
        self,
        method: str,
        params: list[Any] | None = None,
    ) -> Any:
        """
        Выполнить JSON-RPC вызов.

        Args:
            method: RPC метод
            params: Параметры

        Returns:
            Результат вызова
        """
        client = await self._get_client()

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or [],
        }

        from src.blockchain.prepared import ChainRPCError

        try:
            response = await client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ChainRPCError(f"Solana {method} response unavailable") from None
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise ChainRPCError(f"Solana {method} rejected or malformed")
        return data["result"]

    # === Connection ===

    async def is_connected(self) -> bool:
        """Проверить подключение к ноде."""
        try:
            result = await self._rpc_call("getHealth")
            return result == "ok"
        except Exception:
            return False

    # === Block / Slot ===

    async def get_latest_block(self) -> int:
        return int(await self._rpc_call("getSlot", [{"commitment": "finalized"}]))

    async def get_block_timestamp(self, slot: int) -> int | None:
        """Получить timestamp слота."""
        try:
            result = await self._rpc_call("getBlockTime", [slot])
            return result
        except Exception:
            return None

    # === Balance ===

    async def get_native_balance(self, address: str) -> Decimal:
        from src.blockchain.solana_evidence import SolanaEvidenceError, uint
        from src.ledger.amounts import atomic_to_decimal

        result = await self._rpc_call("getBalance", [address, {"commitment": "finalized"}])
        if not isinstance(result, dict) or "value" not in result:
            raise SolanaEvidenceError("Native balance is unavailable")
        return atomic_to_decimal(uint(result["value"], "lamports"), 9)

    async def get_token_balance(self, address: str, token_address: str) -> Decimal:
        return (await self.get_all_token_balances(address, [token_address]))[token_address]

    async def _get_associated_token_address(self, owner: str, mint: str) -> str:
        from solders.pubkey import Pubkey

        associated_program = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
        address, _ = Pubkey.find_program_address(
            [
                bytes(Pubkey.from_string(owner)),
                bytes(Pubkey.from_string(SPL_TOKEN_PROGRAM_ID)),
                bytes(Pubkey.from_string(mint)),
            ],
            associated_program,
        )
        return str(address)

    async def get_all_token_balances(
        self, address: str, token_addresses: list[str]
    ) -> dict[str, Decimal]:
        from src.blockchain.solana_evidence import SolanaEvidenceError, token_accounts
        from src.ledger.amounts import atomic_to_decimal

        if any(mint not in self._decimals_cache for mint in token_addresses):
            raise SolanaEvidenceError("Mint is not in the canonical token registry")
        result = await self._rpc_call(
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": SPL_TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
        accounts = token_accounts(result, address, self._decimals_cache)
        totals = {mint: 0 for mint in token_addresses}
        for mint, value in accounts.values():
            if mint in totals:
                totals[mint] += value
        return {
            mint: atomic_to_decimal(value, self._decimals_cache[mint])
            for mint, value in totals.items()
        }

    # === Transfer Events ===

    async def get_transfer_events(
        self,
        from_slot: int,
        to_slot: int,
        to_addresses: list[str],
        token_addresses: list[str] | None = None,
    ) -> list[TransferEvent]:
        from src.blockchain.solana_evidence import SolanaEvidenceError, token_accounts

        if from_slot < 0 or to_slot < from_slot or to_slot > await self.get_latest_block():
            raise SolanaEvidenceError("Requested slot range is not finalized")
        events, transactions = {}, {}
        mints = token_addresses if token_addresses is not None else list(self._decimals_cache)
        if any(mint not in self._decimals_cache for mint in mints):
            raise SolanaEvidenceError("Mint is not in the canonical token registry")
        for owner in dict.fromkeys(to_addresses):
            raw_accounts = await self._rpc_call(
                "getTokenAccountsByOwner",
                [
                    owner,
                    {"programId": SPL_TOKEN_PROGRAM_ID},
                    {"encoding": "jsonParsed", "commitment": "finalized"},
                ],
            )
            accounts = token_accounts(raw_accounts, owner, self._decimals_cache)
            watched = {owner, *(key for key, (mint, _) in accounts.items() if mint in mints)}
            # Query deterministic ATAs even when closed after the payment.
            for mint in mints:
                watched.add(await self._get_associated_token_address(owner, mint))
            for address in sorted(watched):
                before, seen = None, set()
                for _ in range(self.config.scanner_max_pages_per_address):
                    options = {"commitment": "finalized", "limit": 1000}
                    if before:
                        options["before"] = before
                    page = await self._rpc_call("getSignaturesForAddress", [address, options])
                    if not isinstance(page, list):
                        raise SolanaEvidenceError("Signature page is unavailable")
                    if not page:
                        break
                    previous_slot = None
                    for row in page:
                        signature, slot = row.get("signature"), row.get("slot")
                        if (
                            not isinstance(signature, str)
                            or not signature
                            or signature in seen
                            or isinstance(slot, bool)
                            or not isinstance(slot, int)
                            or previous_slot is not None
                            and slot > previous_slot
                        ):
                            raise SolanaEvidenceError("Signature pagination is inconsistent")
                        seen.add(signature)
                        previous_slot = slot
                        if not from_slot <= slot <= to_slot or row.get("err") is not None:
                            continue
                        if signature not in transactions:
                            transactions[signature] = await self._rpc_call(
                                "getTransaction",
                                [
                                    signature,
                                    {
                                        "encoding": "jsonParsed",
                                        "commitment": "finalized",
                                        "maxSupportedTransactionVersion": 0,
                                    },
                                ],
                            )
                        tx = transactions[signature]
                        if not isinstance(tx, dict):
                            raise SolanaEvidenceError(
                                "Finalized transaction details are unavailable"
                            )
                        for event in self._parse_token_transfers(
                            tx, signature, slot, row.get("blockTime"), owner, token_addresses
                        ):
                            events[(event.tx_hash, event.log_index, event.to_address)] = event
                    if len(page) < 1000 or page[-1]["slot"] < from_slot:
                        break
                    before = page[-1]["signature"]
                else:
                    raise SolanaEvidenceError(
                        "Signature page budget exhausted; checkpoint must not advance"
                    )
        return sorted(
            events.values(), key=lambda event: (event.block_number, event.tx_hash, event.log_index)
        )

    def _parse_token_transfers(
        self,
        tx: dict,
        signature: str,
        slot: int,
        block_time: int | None,
        target_address: str,
        token_filter: list[str] | None,
    ) -> list[TransferEvent]:
        from src.blockchain.solana_evidence import transfers

        tokens = {
            token.contract_address: (token.symbol, token.decimals)
            for token in self.config.tokens.values()
        }
        return transfers(
            tx, signature, slot, block_time, target_address, tokens, self.chain, token_filter
        )

    def _get_token_symbol(self, mint: str) -> str:
        """Получить символ токена по mint address."""
        for token in self.config.tokens.values():
            if token.contract_address == mint:
                return token.symbol
        return "UNKNOWN"

    # === Transaction Status ===

    async def get_confirmations(self, signature: str) -> int | None:
        result = await self._rpc_call(
            "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("value"), list)
            or len(result["value"]) != 1
        ):
            return None
        status = result["value"][0]
        if not isinstance(status, dict) or "err" not in status or status["err"] is not None:
            return None
        if status.get("confirmationStatus") == "finalized":
            return self.config.confirmations
        return 0

    async def is_tx_confirmed(
        self, signature: str, required_confirmations: int | None = None
    ) -> bool:
        confirmations = await self.get_confirmations(signature)
        required = (
            self.config.confirmations if required_confirmations is None else required_confirmations
        )
        return confirmations is not None and confirmations >= required

    # === Signed transactions ===

    async def _instructions(self, sender: str, destination: str, token: str | None, raw: int):
        from solders.instruction import AccountMeta, Instruction
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer

        from src.blockchain.prepared import ChainEvidenceError
        from src.blockchain.solana_evidence import token_accounts, uint

        source, target = Pubkey.from_string(sender), Pubkey.from_string(destination)
        if raw <= 0 or raw >= 2**64 or source == target:
            raise ChainEvidenceError("Transfer amount or destination is invalid")
        if not token:
            return [transfer(TransferParams(from_pubkey=source, to_pubkey=target, lamports=raw))], 0
        if token not in self._decimals_cache:
            raise ChainEvidenceError("Mint is not configured")
        mint = Pubkey.from_string(token)
        mint_info = await self._rpc_call(
            "getAccountInfo", [token, {"encoding": "jsonParsed", "commitment": "finalized"}]
        )
        value = mint_info.get("value") if isinstance(mint_info, dict) else None
        if (
            not isinstance(value, dict)
            or value.get("owner") != SPL_TOKEN_PROGRAM_ID
            or value.get("data", {}).get("parsed", {}).get("type") != "mint"
            or value["data"]["parsed"]["info"].get("decimals") != self._decimals_cache[token]
        ):
            raise ChainEvidenceError("Canonical classic SPL mint proof is unavailable")
        result = await self._rpc_call(
            "getTokenAccountsByOwner",
            [
                sender,
                {"programId": SPL_TOKEN_PROGRAM_ID},
                {"encoding": "jsonParsed", "commitment": "finalized"},
            ],
        )
        accounts = token_accounts(result, sender, self._decimals_cache)
        ata = await self._get_associated_token_address(destination, token)
        target_info = await self._rpc_call(
            "getAccountInfo", [ata, {"encoding": "jsonParsed", "commitment": "finalized"}]
        )
        if not isinstance(target_info, dict) or "value" not in target_info:
            raise ChainEvidenceError("Destination token account is unavailable")
        instructions, rent = [], 0
        if target_info["value"] is None:
            rent = uint(await self._rpc_call("getMinimumBalanceForRentExemption", [165]), "rent")
            instructions.append(
                Instruction(
                    Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
                    b"\x01",
                    [
                        AccountMeta(source, True, True),
                        AccountMeta(Pubkey.from_string(ata), False, True),
                        AccountMeta(target, False, False),
                        AccountMeta(mint, False, False),
                        AccountMeta(Pubkey.default(), False, False),
                        AccountMeta(Pubkey.from_string(SPL_TOKEN_PROGRAM_ID), False, False),
                    ],
                )
            )
        else:
            verified = token_accounts(
                {"value": [{"pubkey": ata, "account": target_info["value"]}]},
                destination,
                self._decimals_cache,
            )
            if verified.get(ata, (None,))[0] != token:
                raise ChainEvidenceError("Destination token account has a different mint")
        remaining = raw
        for account, (mint_address, balance) in sorted(accounts.items()):
            if mint_address != token or balance == 0:
                continue
            part = min(remaining, balance)
            instructions.append(
                Instruction(
                    Pubkey.from_string(SPL_TOKEN_PROGRAM_ID),
                    bytes([12]) + part.to_bytes(8, "little") + bytes([self._decimals_cache[token]]),
                    [
                        AccountMeta(Pubkey.from_string(account), False, True),
                        AccountMeta(mint, False, False),
                        AccountMeta(Pubkey.from_string(ata), False, True),
                        AccountMeta(source, True, False),
                    ],
                )
            )
            remaining -= part
            if remaining == 0:
                break
        if remaining:
            raise ChainEvidenceError("Insufficient canonical SPL balance")
        return instructions, rent

    async def _message(self, sender: str, destination: str, token: str | None, raw: int):
        import base64

        from solders.hash import Hash
        from solders.message import Message
        from solders.pubkey import Pubkey

        from src.blockchain.prepared import ChainRPCError
        from src.blockchain.solana_evidence import uint

        instructions, rent = await self._instructions(sender, destination, token, raw)
        block = await self._rpc_call("getLatestBlockhash", [{"commitment": "finalized"}])
        if not isinstance(block, dict) or not isinstance(block.get("value"), dict):
            raise ChainRPCError("Recent finalized blockhash is unavailable")
        message = Message.new_with_blockhash(
            instructions, Pubkey.from_string(sender), Hash.from_string(block["value"]["blockhash"])
        )
        fee = await self._rpc_call(
            "getFeeForMessage",
            [base64.b64encode(bytes(message)).decode(), {"commitment": "finalized"}],
        )
        if not isinstance(fee, dict) or fee.get("value") is None:
            raise ChainRPCError("Transaction fee is unavailable")
        return (
            message,
            uint(fee["value"], "fee") + rent,
            uint(block["value"]["lastValidBlockHeight"], "expiry"),
            uint(block.get("context", {}).get("slot"), "signing slot"),
        )

    async def estimate_transfer_fee(
        self,
        from_address: str,
        to_address: str,
        token_address: str | None = None,
        amount: Decimal | None = None,
    ) -> FeeEstimate:
        from src.ledger.amounts import atomic_to_decimal, decimal_to_atomic

        decimals = self._decimals_cache[token_address] if token_address else 9
        raw = decimal_to_atomic(amount, decimals) if amount is not None else 1
        _, fee, _, _ = await self._message(from_address, to_address, token_address, raw)
        return FeeEstimate(atomic_to_decimal(fee, 9), fee)

    async def prepare_transfer(
        self,
        private_key: str,
        destination: str,
        amount: Decimal,
        token: str | None = None,
        *,
        reference: str = "",
    ):
        import base64

        from solders.keypair import Keypair
        from solders.transaction import Transaction

        from src.blockchain.prepared import ChainEvidenceError, PreparedTransfer
        from src.ledger.amounts import decimal_to_atomic

        key = Keypair.from_base58_string(private_key)
        decimals = self._decimals_cache[token] if token else 9
        raw = decimal_to_atomic(amount, decimals)
        message, fee, expiry, slot = await self._message(str(key.pubkey()), destination, token, raw)
        balance = decimal_to_atomic(await self.get_native_balance(str(key.pubkey())), 9)
        if balance < fee + (0 if token else raw):
            raise ChainEvidenceError("Insufficient SOL for amount, fees and ATA rent")
        tx = Transaction([key], message, message.recent_blockhash)
        payload = base64.b64encode(bytes(tx)).decode()
        if len(bytes(tx)) > 1232:
            raise ChainEvidenceError(
                "Token accounts require more than one transaction; consolidate first"
            )
        simulation = await self._rpc_call(
            "simulateTransaction",
            [payload, {"encoding": "base64", "sigVerify": True, "commitment": "finalized"}],
        )
        if not isinstance(simulation, dict) or not isinstance(simulation.get("value"), dict):
            raise ChainEvidenceError("Transaction simulation is unavailable")
        if "err" not in simulation["value"] or simulation["value"]["err"] is not None:
            raise ChainEvidenceError("Transaction simulation failed")
        return PreparedTransfer(
            self.chain,
            str(tx.signatures[0]),
            payload,
            str(key.pubkey()),
            destination,
            token or "",
            raw,
            fee,
            {"last_valid_block_height": expiry, "signing_slot": slot},
        )

    async def broadcast_prepared(self, prepared):
        from src.blockchain.prepared import ChainRPCError, TransferReceipt

        try:
            signature = await self._rpc_call(
                "sendTransaction",
                [
                    prepared.payload,
                    {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "maxRetries": 0,
                        "preflightCommitment": "finalized",
                    },
                ],
            )
        except (ChainRPCError, httpx.HTTPError):
            return TransferReceipt("unknown", prepared.tx_id, reason="broadcast_outcome_unknown")
        if signature != prepared.tx_id:
            return TransferReceipt("unknown", prepared.tx_id, reason="signature_mismatch")
        return TransferReceipt("pending", prepared.tx_id)

    async def transfer_receipt(self, prepared):
        from src.blockchain.prepared import ChainEvidenceError, TransferReceipt
        from src.blockchain.solana_evidence import uint

        status = await self._rpc_call(
            "getSignatureStatuses", [[prepared.tx_id], {"searchTransactionHistory": True}]
        )
        if (
            not isinstance(status, dict)
            or not isinstance(status.get("value"), list)
            or len(status["value"]) != 1
        ):
            return TransferReceipt("unknown", prepared.tx_id)
        row = status["value"][0]
        if row is None:
            height = uint(
                await self._rpc_call("getBlockHeight", [{"commitment": "finalized"}]),
                "block height",
            )
            retained = uint(await self._rpc_call("minimumLedgerSlot", []), "retained slot")
            if height > prepared.context[
                "last_valid_block_height"
            ] and retained <= prepared.context.get("signing_slot", -1):
                tx = await self._rpc_call(
                    "getTransaction",
                    [
                        prepared.tx_id,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "finalized",
                            "maxSupportedTransactionVersion": 0,
                        },
                    ],
                )
                if tx is None:
                    return TransferReceipt(
                        "failed", prepared.tx_id, reason="signature_expired_unexecuted"
                    )
            return TransferReceipt("unknown", prepared.tx_id, reason="signature_not_observed")
        if "err" not in row:
            return TransferReceipt("unknown", prepared.tx_id)
        if row.get("confirmationStatus") != "finalized":
            return TransferReceipt("pending", prepared.tx_id)
        tx = await self._rpc_call(
            "getTransaction",
            [
                prepared.tx_id,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if not isinstance(tx, dict) or "err" not in tx.get("meta", {}):
            return TransferReceipt("unknown", prepared.tx_id, reason="receipt_unavailable")
        signatures = tx.get("transaction", {}).get("signatures", [])
        if not signatures or signatures[0] != prepared.tx_id:
            raise ChainEvidenceError("Finalized transaction signature binding differs")
        fee = uint(tx["meta"]["fee"], "fee")
        if row["err"] is not None and tx["meta"]["err"] is not None:
            return TransferReceipt(
                "failed", prepared.tx_id, fee, "chain_execution_failed", native_cost_raw=fee
            )
        if row["err"] is not None or tx["meta"]["err"] is not None:
            raise ChainEvidenceError("Solana execution status differs")
        if prepared.token:
            events = self._parse_token_transfers(
                tx,
                prepared.tx_id,
                tx["slot"],
                tx.get("blockTime"),
                prepared.destination,
                [prepared.token],
            )
            total = sum(e.raw_amount for e in events if e.token_address == prepared.token)
        else:
            instructions = tx.get("transaction", {}).get("message", {}).get("instructions", [])
            total = sum(
                uint(i["parsed"]["info"]["lamports"], "lamports")
                for i in instructions
                if i.get("programId") == "11111111111111111111111111111111"
                and i.get("parsed", {}).get("type") == "transfer"
                and i["parsed"]["info"].get("source") == prepared.sender
                and i["parsed"]["info"].get("destination") == prepared.destination
            )
        if total != prepared.amount_raw:
            raise ChainEvidenceError("Finalized destination or amount differs")
        keys = tx["transaction"]["message"]["accountKeys"]
        owners = [key.get("pubkey") if isinstance(key, dict) else key for key in keys]
        if owners.count(prepared.sender) != 1:
            raise ChainEvidenceError("Solana fee payer balance binding differs")
        index = owners.index(prepared.sender)
        cost = uint(tx["meta"]["preBalances"][index], "pre balance") - uint(
            tx["meta"]["postBalances"][index], "post balance"
        )
        if not prepared.token:
            cost -= prepared.amount_raw
        if cost < fee:
            raise ChainEvidenceError("Solana native cost binding differs")
        return TransferReceipt("completed", prepared.tx_id, fee, native_cost_raw=cost)

    async def native_rent_reserve(self):
        from src.blockchain.solana_evidence import uint

        return uint(await self._rpc_call("getMinimumBalanceForRentExemption", [0]), "system rent")

    async def send_native_token(
        self, from_private_key: str, to_address: str, amount: Decimal
    ) -> TransactionResult:
        prepared = await self.prepare_transfer(from_private_key, to_address, amount)
        receipt = await self.broadcast_prepared(prepared)
        return TransactionResult(receipt.tx_id, receipt.status == "pending", receipt.reason)

    async def send_token(
        self, from_private_key: str, to_address: str, token_address: str, amount: Decimal
    ) -> TransactionResult:
        prepared = await self.prepare_transfer(from_private_key, to_address, amount, token_address)
        receipt = await self.broadcast_prepared(prepared)
        return TransactionResult(receipt.tx_id, receipt.status == "pending", receipt.reason)

    # === Address Validation ===

    def is_valid_address(self, address: str) -> bool:
        """Проверить валидность Solana адреса (Base58)."""
        try:
            decoded = base58.b58decode(address)
            return len(decoded) == 32
        except Exception:
            return False

    def normalize_address(self, address: str) -> str:
        """Нормализовать Solana адрес."""
        # Solana адреса case-sensitive, просто проверяем валидность
        if not self.is_valid_address(address):
            raise ValueError(f"Invalid Solana address: {address}")
        return address

    async def close(self) -> None:
        """Закрыть HTTP клиент."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Singleton адаптер
_solana_adapter: SolanaAdapter | None = None


def get_solana_adapter() -> SolanaAdapter:
    """Получить singleton Solana адаптер."""
    global _solana_adapter
    if _solana_adapter is None:
        _solana_adapter = SolanaAdapter()
    return _solana_adapter
