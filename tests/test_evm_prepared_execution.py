"""Signed gateway transactions executed by an isolated local Anvil, without real funds."""

import json
import subprocess
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from web3 import Web3

from src.blockchain.chains import TokenConfig, get_chain_config
from src.blockchain.evm_prepared import PreparedEvmAdapter
from src.blockchain.prepared import ChainEvidenceError

TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


@pytest.fixture
def evm_rpc():
    image = subprocess.run(
        ["docker", "image", "inspect", "ghcr.io/foundry-rs/foundry:latest", "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
    )
    if image.returncode:
        pytest.skip("Explicitly cached local Anvil image is required")
    name = "gateway-execution-" + uuid4().hex
    started = subprocess.run(
        [
            "docker",
            "run",
            "--pull",
            "never",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            "codex.task=arbitron-gateway-completion",
            "--read-only",
            "--cap-drop",
            "ALL",
            "-p",
            "127.0.0.1::8545",
            image.stdout.strip(),
            "anvil --silent --host 0.0.0.0",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = started.stdout.strip()
    try:
        ports = json.loads(
            subprocess.check_output(
                ["docker", "inspect", container_id, "--format", "{{json .NetworkSettings.Ports}}"],
                text=True,
            )
        )
        url = "http://127.0.0.1:" + ports["8545/tcp"][0]["HostPort"]
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 1}))
        deadline = time.monotonic() + 15
        while not w3.is_connected():
            if time.monotonic() > deadline:
                pytest.fail("Task-owned Anvil did not start")
            time.sleep(0.1)
        yield w3, url
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=True)


async def test_real_erc20_and_native_signed_execution_receipts_and_replay(evm_rpc):
    w3, url = evm_rpc
    payer = w3.eth.account.from_key(TEST_KEY)
    recipient = w3.eth.account.create().address
    root = Path(__file__).parent / "contracts"
    abi = json.loads((root / "mock_erc20.abi").read_text())
    contract = w3.eth.contract(
        abi=abi, bytecode="0x" + (root / "mock_erc20.hex").read_text().strip()
    )
    deployed = w3.eth.wait_for_transaction_receipt(
        contract.constructor("USDT", "USDT").transact({"from": payer.address})
    )
    token = w3.eth.contract(address=deployed.contractAddress, abi=abi)
    w3.eth.wait_for_transaction_receipt(
        token.functions.mint(payer.address, 100 * 10**18).transact({"from": payer.address})
    )
    config = replace(
        get_chain_config("bsc"),
        rpc_url=url,
        rpc_urls=[url],
        chain_id=w3.eth.chain_id,
        confirmations=1,
        reorg_buffer=1,
        native_symbol="ETH",
        tokens={"USDT": TokenConfig("USDT", deployed.contractAddress, 18)},
    )
    adapter = PreparedEvmAdapter("bsc", config)
    try:
        prepared = await adapter.prepare_transfer(
            TEST_KEY, recipient, Decimal("12.345678901234567890"), deployed.contractAddress
        )
        assert (await adapter.broadcast_prepared(prepared)).status == "pending"
        w3.eth.wait_for_transaction_receipt(prepared.tx_id)
        proof = await adapter.transfer_receipt(prepared)
        assert proof.status == "completed" and proof.fee_raw > 0
        assert token.functions.balanceOf(recipient).call() == 12345678901234567890
        # Replay may be returned as known or ambiguous by the RPC, but cannot execute twice.
        await adapter.broadcast_prepared(prepared)
        assert token.functions.balanceOf(recipient).call() == 12345678901234567890
        with pytest.raises(ChainEvidenceError):
            await adapter.transfer_receipt(replace(prepared, destination=payer.address.lower()))
        observed = await adapter.get_transfer_events(
            deployed.blockNumber, w3.eth.block_number, [recipient]
        )
        assert [event.raw_amount for event in observed] == [12345678901234567890]
        native = await adapter.prepare_transfer(TEST_KEY, recipient, Decimal("0.1"))
        assert (await adapter.broadcast_prepared(native)).status == "pending"
        w3.eth.wait_for_transaction_receipt(native.tx_id)
        assert (await adapter.transfer_receipt(native)).status == "completed"
        assert w3.eth.get_balance(recipient) == 10**17
    finally:
        await adapter.close()
