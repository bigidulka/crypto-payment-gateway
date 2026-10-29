"""TON receive/send using real wallet BOCs and completed canonical jetton traces."""

import asyncio
import time
import base64
from decimal import Decimal

import httpx
from tonsdk.boc import Cell
from tonsdk.contract.token.ft import JettonWallet
from tonsdk.utils import Address

from src.blockchain.base_adapter import (
    BaseAdapter,
    ChainType,
    TransactionResult,
    TransferEvent,
)
from src.blockchain.chains import ChainConfig, get_chain_config
from src.blockchain.prepared import (
    ChainEvidenceError,
    ChainRPCError,
    PreparedTransfer,
    TransferReceipt,
)
from src.blockchain.ton_evidence import complete_trace, hash_id, internal_transfer, successful, uint
from src.blockchain.ton_keys import normalize_ton_address, wallet_from_seed
from src.core.config import get_settings
from src.ledger.amounts import atomic_to_decimal, decimal_to_atomic



_pace_lock = asyncio.Lock()
_last_request_at = 0.0
_RATE_LIMIT_RETRIES = 3

class TonAdapter(BaseAdapter[ChainConfig]):
    def __init__(
        self,
        chain="ton",
        config: ChainConfig | None = None,
        rpc_url: str | None = None,
        api_key: str | None = None,
        index_url: str | None = None,
    ):
        config = config or get_chain_config(chain)
        super().__init__(chain, config)
        self.settings = get_settings()
        self.rpc_url = (rpc_url or self.settings.ton_rpc_url or config.rpc_url).rstrip("/")
        self.index_url = (index_url or self.settings.ton_index_url).rstrip("/")
        self.api_key = api_key if api_key is not None else self.settings.ton_api_key
        self._client: httpx.AsyncClient | None = None
        self._tokens = {
            normalize_ton_address(t.contract_address): t for t in config.tokens.values()
        }

    @property
    def chain_type(self):
        return ChainType.TON

    @property
    def native_symbol(self):
        return "TON"

    @property
    def address_length(self):
        return 66

    async def _paced(self, send):
        # One process-wide pace for the shared provider limit; a 429 is rejected
        # before processing, so retrying it never repeats an executed request.
        global _last_request_at
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            async with _pace_lock:
                wait = _last_request_at + self.settings.ton_min_request_interval_seconds - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                _last_request_at = time.monotonic()
            response = await send()
            if response.status_code != 429 or attempt == _RATE_LIMIT_RETRIES:
                return response
            await asyncio.sleep(self.settings.ton_min_request_interval_seconds * (attempt + 2))

    async def _request(self, method: str, path: str, params=None, *, index=False, body=None):
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        url = self.index_url if index else self.rpc_url
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        try:
            response = await self._paced(
                lambda: self._client.request(
                    method, f"{url}/{path}", params=params, json=body, headers=headers
                )
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise ChainRPCError(f"TON {path} response unavailable") from None
        if not isinstance(data, dict):
            raise ChainRPCError(f"TON {path} malformed response")
        if index:
            return data
        if data.get("ok") is not True or "result" not in data:
            raise ChainRPCError(f"TON {path} rejected or malformed")
        return data["result"]

    async def _api_call(self, method, params=None):
        return await self._request("GET", method, params)

    async def _run_get_method(self, address, method, stack):
        result = await self._request(
            "POST", "runGetMethod", body={"address": address, "method": method, "stack": stack}
        )
        if (
            not isinstance(result, dict)
            or result.get("exit_code") != 0
            or not isinstance(result.get("stack"), list)
        ):
            raise ChainEvidenceError("TON get-method proof unavailable")
        return result

    async def is_connected(self):
        try:
            return await self.get_latest_block() > 0
        except ChainRPCError:
            return False

    async def get_latest_block(self):
        result = await self._api_call("getMasterchainInfo")
        return uint(result["last"]["seqno"], "masterchain seqno")

    async def get_block_timestamp(self, block):
        result = await self._api_call(
            "getBlockHeader", {"workchain": -1, "shard": "-9223372036854775808", "seqno": block}
        )
        return uint(result["gen_utime"], "timestamp")

    async def get_native_balance(self, address):
        return atomic_to_decimal(
            uint(
                await self._api_call(
                    "getAddressBalance", {"address": self.normalize_address(address)}
                ),
                "balance",
            ),
            9,
        )

    @staticmethod
    def _stack_address(value):
        if value[0] not in ("cell", "slice") or not isinstance(value[1], dict):
            raise ChainEvidenceError("TON stack address missing")
        parsed = Cell.one_from_boc(base64.b64decode(value[1]["bytes"], validate=True)).begin_parse()
        address = parsed.read_msg_addr()
        if address is None:
            raise ChainEvidenceError("TON stack address is null")
        return normalize_ton_address(address.to_string(False))

    async def _get_jetton_wallet_address(self, jetton_master, owner):
        master, owner = self.normalize_address(jetton_master), self.normalize_address(owner)
        if master not in self._tokens:
            raise ChainEvidenceError("Jetton master is not canonical")
        cell = Cell()
        cell.bits.write_address(Address(owner))
        result = await self._run_get_method(
            master,
            "get_wallet_address",
            [["tvm.Slice", base64.b64encode(cell.to_boc(False)).decode()]],
        )
        return self._stack_address(result["stack"][0])

    async def get_token_balance(self, address, token_address):
        master, owner = self.normalize_address(token_address), self.normalize_address(address)
        wallet = await self._get_jetton_wallet_address(master, owner)
        info = await self._api_call("getAddressInformation", {"address": wallet})
        if info.get("state") in ("uninitialized", "uninit"):
            return Decimal(0)
        result = await self._run_get_method(wallet, "get_wallet_data", [])
        stack = result["stack"]
        if (
            len(stack) < 3
            or stack[0][0] != "num"
            or self._stack_address(stack[1]) != owner
            or self._stack_address(stack[2]) != master
        ):
            raise ChainEvidenceError("Jetton wallet owner/master mismatch")
        raw = int(stack[0][1], 16)
        return atomic_to_decimal(uint(raw, "jetton balance"), self._tokens[master].decimals)

    async def _pages(self, path, field, params):
        rows, seen = [], set()
        limit = self.config.scanner_page_limit
        for page in range(self.config.scanner_max_pages_per_address):
            result = await self._request(
                "GET",
                path,
                {**params, "limit": limit, "offset": page * limit, "sort": "asc"},
                index=True,
            )
            items = result.get(field)
            if not isinstance(items, list):
                raise ChainEvidenceError("TON indexed page is unavailable")
            for row in items:
                identity = row.get("transaction_hash") or row.get("hash")
                if not identity or identity in seen:
                    raise ChainEvidenceError("TON pagination is inconsistent")
                seen.add(identity)
                rows.append(row)
            if len(items) < limit:
                return rows
        raise ChainEvidenceError("TON page budget exhausted; checkpoint must not advance")

    async def _trace(self, *, tx_hash=None, msg_hash=None):
        query = {"tx_hash": tx_hash} if tx_hash else {"msg_hash": msg_hash}
        result = await self._request("GET", "traces", {**query, "limit": 2}, index=True)
        rows = result.get("traces")
        if not isinstance(rows, list) or len(rows) > 1:
            raise ChainEvidenceError("TON trace identity is ambiguous")
        return rows[0] if rows else None

    async def _jetton_event(self, row, owner, master, head):
        if (
            row.get("transaction_aborted") is not False
            or self.normalize_address(row["destination"]) != owner
        ):
            return None
        if (
            self.normalize_address(row["jetton_master"]) != master
            or self.normalize_address(row["source"]) == owner
        ):
            return None
        trace = await self._trace(tx_hash=row["transaction_hash"])
        if trace is None or not complete_trace(trace, head, self.config.confirmations):
            raise ChainEvidenceError("Jetton trace is not finalized and complete")
        wallet = await self._get_jetton_wallet_address(master, owner)
        source_wallet = self.normalize_address(row["source_wallet"])
        source = self.normalize_address(row["source"])
        # Verify both master-derived wallet identities. A fake notification cannot pass.
        if source_wallet != await self._get_jetton_wallet_address(master, source):
            raise ChainEvidenceError("Jetton source wallet is not master-derived")
        matches = []
        for tx in trace.get("transactions", {}).values():
            msg = tx.get("in_msg") or {}
            if self.normalize_address(tx["account"]) != wallet or not successful(
                tx, head, self.config.confirmations
            ):
                continue
            if not msg.get("source") or self.normalize_address(msg["source"]) != source_wallet:
                continue
            content = msg.get("message_content") or {}
            if not content.get("body"):
                raise ChainEvidenceError("Jetton receipt body is unavailable")
            raw, sender, query, memo = internal_transfer(content["body"])
            if (
                raw == uint(row["amount"], "jetton amount")
                and sender == source
                and query == uint(row["query_id"], "query id")
            ):
                matches.append((tx, raw, memo))
        if len(matches) != 1:
            raise ChainEvidenceError("Completed destination jetton receipt is unavailable")
        tx, raw, memo = matches[0]
        token = self._tokens[master]
        originating = [
            item
            for item in trace["transactions"].values()
            if hash_id(item["hash"]) == hash_id(row["transaction_hash"])
        ]
        if len(originating) != 1 or not successful(originating[0], head, self.config.confirmations):
            raise ChainEvidenceError("Jetton initiating transaction proof is unavailable")
        return TransferEvent(
            hash_id(tx["hash"]),
            tx["mc_block_seqno"],
            0,
            source,
            owner,
            master,
            token.symbol,
            atomic_to_decimal(raw, token.decimals),
            raw,
            self.chain,
            ChainType.TON,
            uint(tx["now"], "timestamp"),
            memo,
            originating[0]["mc_block_seqno"],
        )

    async def get_transfer_events(self, from_block, to_block, to_addresses, token_addresses=None):
        head = await self.get_latest_block()
        if (
            from_block < 0
            or to_block < from_block
            or to_block > head - self.config.confirmations + 1
        ):
            raise ChainEvidenceError("TON block range is not finalized")
        lower = await self.get_block_timestamp(from_block)
        upper = await self.get_block_timestamp(to_block)
        events = {}
        masters = [self.normalize_address(m) for m in (token_addresses or self._tokens)]
        for address in dict.fromkeys(to_addresses):
            owner = self.normalize_address(address)
            for master in masters:
                if master not in self._tokens:
                    raise ChainEvidenceError("Jetton master is not configured")
                rows = await self._pages(
                    "jetton/transfers",
                    "jetton_transfers",
                    {
                        "owner_address": owner,
                        "jetton_master": master,
                        "direction": "in",
                        "start_utime": lower,
                        "end_utime": upper,
                    },
                )
                for row in rows:
                    event = await self._jetton_event(row, owner, master, head)
                    if event and from_block <= event.scan_block_number <= to_block:
                        events[(event.tx_hash, event.log_index, owner)] = event
            rows = await self._pages(
                "transactions",
                "transactions",
                {"account": owner, "start_utime": lower, "end_utime": upper},
            )
            for tx in rows:
                msg = tx.get("in_msg") or {}
                if not msg.get("source") or not successful(tx, head, self.config.confirmations):
                    continue
                if (
                    not msg.get("destination")
                    or self.normalize_address(msg["destination"]) != owner
                    or not from_block <= tx["mc_block_seqno"] <= to_block
                ):
                    continue
                raw = uint(
                    (tx.get("description", {}).get("credit_ph") or {}).get("credit"),
                    "native credit",
                )
                if raw > 0:
                    event = TransferEvent(
                        hash_id(tx["hash"]),
                        tx["mc_block_seqno"],
                        1,
                        self.normalize_address(msg["source"]),
                        owner,
                        "",
                        "TON",
                        atomic_to_decimal(raw, 9),
                        raw,
                        self.chain,
                        ChainType.TON,
                        uint(tx["now"], "timestamp"),
                    )
                    events[(event.tx_hash, event.log_index, owner)] = event
        return sorted(events.values(), key=lambda e: (e.block_number, e.tx_hash, e.log_index))

    async def get_confirmations(self, tx_hash):
        result = await self._request(
            "GET", "transactions", {"hash": tx_hash, "limit": 2}, index=True
        )
        rows = result.get("transactions", [])
        if len(rows) != 1 or hash_id(rows[0]["hash"]) != hash_id(tx_hash):
            return None
        head = await self.get_latest_block()
        return head - rows[0]["mc_block_seqno"] + 1 if successful(rows[0], head, 1) else None

    async def is_tx_confirmed(self, tx_hash, required_confirmations=None):
        count = await self.get_confirmations(tx_hash)
        return count is not None and count >= (required_confirmations or self.config.confirmations)

    async def prepare_transfer(self, private_key, destination, amount, token=None, *, reference=""):
        wallet = wallet_from_seed(
            private_key, self.settings.ton_wallet_version, self.settings.ton_workchain
        )
        sender = self.normalize_address(wallet.address.to_string(False))
        destination = self.normalize_address(destination)
        if sender == destination or len(reference.encode()) > 120:
            raise ChainEvidenceError("TON transfer destination or reference is invalid")
        master = self.normalize_address(token) if token else ""
        if master and master not in self._tokens:
            raise ChainEvidenceError("Jetton master is not configured")
        decimals = self._tokens[master].decimals if master else 9
        raw = decimal_to_atomic(amount, decimals)
        info = await self._api_call("getWalletInformation", {"address": sender})
        if info.get("account_state") in ("uninitialized", "uninit"):
            seqno = 0
        else:
            seqno = uint(info.get("seqno"), "wallet seqno")
        payload, target, attached = reference or None, destination, raw
        if master:
            if decimal_to_atomic(await self.get_token_balance(sender, master), decimals) < raw:
                raise ChainEvidenceError("Insufficient canonical jetton balance")
            target = await self._get_jetton_wallet_address(master, sender)
            forward = decimal_to_atomic(Decimal(self.settings.ton_forward_amount), 9)
            attached = decimal_to_atomic(Decimal(self.settings.ton_jetton_attach_amount), 9)
            if forward <= 0 or attached <= forward:
                raise ChainEvidenceError("TON jetton attached/forward amount configuration invalid")
            payload = JettonWallet().create_transfer_body(
                Address(destination),
                raw,
                forward,
                b"\x00\x00\x00\x00" + reference.encode(),
                Address(sender),
            )
        message = wallet.create_transfer_message(
            target, attached, seqno, payload=payload, send_mode=1
        )
        signed = message["signing_message"].begin_parse()
        signed.read_uint(32)
        valid_until = signed.read_uint(32)

        def b64(cell):
            return base64.b64encode(cell.to_boc(False)).decode() if cell else ""

        estimate = await self._request(
            "POST",
            "estimateFee",
            body={
                "address": sender,
                "body": b64(message["body"]),
                "init_code": b64(message.get("code")),
                "init_data": b64(message.get("data")),
                "ignore_chksig": False,
            },
        )
        fees = estimate.get("source_fees") if isinstance(estimate, dict) else None
        if not isinstance(fees, dict):
            raise ChainEvidenceError("TON fee estimate is unavailable")
        fee = sum(uint(fees[k], k) for k in ("in_fwd_fee", "storage_fee", "gas_fee", "fwd_fee"))
        # Attached TON pays jetton forwarding/rent; keep it separate from jetton entitlement.
        budget = fee + (attached if master else 0)
        if decimal_to_atomic(await self.get_native_balance(sender), 9) < fee + attached:
            raise ChainEvidenceError("Insufficient TON for amount, fees and forwarding")
        return PreparedTransfer(
            self.chain,
            message["message"].bytes_hash().hex(),
            b64(message["message"]),
            sender,
            destination,
            master,
            raw,
            budget,
            {
                "seqno": seqno,
                "valid_until": valid_until,
                "body_hash": message["body"].bytes_hash().hex(),
                "reference": reference,
            },
        )

    async def estimate_transfer_fee(
        self, from_address, to_address, token_address=None, amount=None
    ):
        raise ChainEvidenceError(
            "TON fee estimation requires the exact signed body; use prepare_transfer"
        )

    async def broadcast_prepared(self, prepared):
        try:
            await self._request("POST", "sendBoc", body={"boc": prepared.payload})
        except ChainRPCError:
            return TransferReceipt("unknown", prepared.tx_id, reason="broadcast_outcome_unknown")
        return TransferReceipt("pending", prepared.tx_id)

    async def transfer_receipt(self, prepared):
        # BOC hash/body hash are looked up, never seqno advancement alone.
        result = await self._request(
            "GET",
            "transactionsByMessage",
            {"body_hash": prepared.context["body_hash"], "direction": "in", "limit": 2},
            index=True,
        )
        rows = result.get("transactions")
        if not isinstance(rows, list) or len(rows) > 1:
            return TransferReceipt("unknown", prepared.tx_id, reason="wallet_receipt_ambiguous")
        if not rows:
            info = await self._api_call("getWalletInformation", {"address": prepared.sender})
            head = await self.get_latest_block()
            stable_time = await self.get_block_timestamp(head - self.config.confirmations + 1)
            synced = info.get("sync_utime")
            if (
                isinstance(synced, int)
                and synced >= stable_time
                and stable_time > prepared.context.get("valid_until", 2**32)
            ):
                seqno = (
                    info.get("seqno", 0)
                    if info.get("account_state") in ("uninit", "uninitialized")
                    else info.get("seqno")
                )
                if seqno == prepared.context["seqno"]:
                    return TransferReceipt(
                        "failed", prepared.tx_id, reason="wallet_message_expired_unexecuted"
                    )
            return TransferReceipt("unknown", prepared.tx_id, reason="wallet_receipt_not_observed")
        root = rows[0]
        content = (root.get("in_msg") or {}).get("message_content") or {}
        if (
            self.normalize_address(root["account"]) != prepared.sender
            or hash_id(content["hash"]) != prepared.context["body_hash"]
        ):
            raise ChainEvidenceError("TON signed body binding differs")
        head = await self.get_latest_block()
        trace = await self._trace(tx_hash=root["hash"])
        if not trace or not complete_trace(trace, head, self.config.confirmations):
            return TransferReceipt("pending", prepared.tx_id)
        if not successful(root, head, self.config.confirmations):
            fee = self._native_cost(trace, prepared.sender, 0)
            return TransferReceipt(
                "failed", prepared.tx_id, fee, reason="wallet_execution_failed", native_cost_raw=fee
            )
        if prepared.token:
            rows = await self._pages(
                "jetton/transfers",
                "jetton_transfers",
                {
                    "owner_address": prepared.sender,
                    "jetton_master": prepared.token,
                    "direction": "out",
                    "start_lt": uint(root["lt"], "logical time"),
                    "end_lt": uint(trace["end_lt"], "logical time"),
                },
            )
            matches = []
            for row in rows:
                if row.get("trace_id") != root.get("trace_id"):
                    continue
                if (
                    self.normalize_address(row["destination"]) != prepared.destination
                    or uint(row["amount"], "amount") != prepared.amount_raw
                ):
                    continue
                try:
                    event = await self._jetton_event(
                        row, prepared.destination, prepared.token, head
                    )
                except ChainEvidenceError:
                    return TransferReceipt(
                        "reconciliation_required",
                        prepared.tx_id,
                        reason="jetton_destination_needs_reconciliation",
                        wallet_released=True,
                    )
                if event:
                    matches.append(event)
            if len(matches) != 1:
                return TransferReceipt(
                    "reconciliation_required",
                    prepared.tx_id,
                    reason="jetton_destination_receipt_unavailable",
                    wallet_released=True,
                )
        else:
            messages = [
                m
                for m in root.get("out_msgs", [])
                if m.get("destination")
                and self.normalize_address(m["destination"]) == prepared.destination
                and uint(m["value"], "native amount") == prepared.amount_raw
            ]
            if len(messages) != 1:
                return TransferReceipt(
                    "unknown", prepared.tx_id, reason="native_output_unavailable"
                )
            matching = [
                tx
                for tx in trace.get("transactions", {}).values()
                if (tx.get("in_msg") or {}).get("hash") == messages[0]["hash"]
                and successful(tx, head, self.config.confirmations)
            ]
            if len(matching) != 1:
                return TransferReceipt(
                    "unknown", prepared.tx_id, reason="native_destination_receipt_unavailable"
                )
        return TransferReceipt(
            "completed",
            prepared.tx_id,
            sum(uint(tx["total_fees"], "fee") for tx in trace["transactions"].values()),
            native_cost_raw=self._native_cost(
                trace, prepared.sender, 0 if prepared.token else prepared.amount_raw
            ),
        )

    def _native_cost(self, trace, sender, principal):
        spent = 0
        for tx in trace["transactions"].values():
            if self.normalize_address(tx["account"]) != sender:
                continue
            spent += uint(tx["total_fees"], "source fee")
            spent += sum(
                uint(msg.get("value", 0), "outgoing value") for msg in tx.get("out_msgs", [])
            )
            spent -= uint(
                (tx.get("description", {}).get("credit_ph") or {}).get("credit", 0),
                "incoming credit",
            )
        if spent < principal:
            raise ChainEvidenceError("TON source native cost differs")
        return spent - principal

    async def send_native_token(self, from_private_key, to_address, amount):
        prepared = await self.prepare_transfer(from_private_key, to_address, amount)
        result = await self.broadcast_prepared(prepared)
        return TransactionResult(result.tx_id, result.status == "pending", result.reason)

    async def send_token(self, from_private_key, to_address, token_address, amount):
        prepared = await self.prepare_transfer(from_private_key, to_address, amount, token_address)
        result = await self.broadcast_prepared(prepared)
        return TransactionResult(result.tx_id, result.status == "pending", result.reason)

    def normalize_address(self, address):
        return normalize_ton_address(address)

    def is_valid_address(self, address):
        try:
            self.normalize_address(address)
            return True
        except ChainEvidenceError:
            return False

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def get_ton_adapter():
    return TonAdapter()
