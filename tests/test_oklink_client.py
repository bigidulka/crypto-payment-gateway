import base64
import json
from decimal import Decimal

import httpx
import pytest

from src.blockchain.oklink_client import (
    OKLinkClientConfig,
    OKLinkExplorerClient,
    OKLinkTransferLogFetcher,
    generate_oklink_web_api_key,
)


@pytest.mark.asyncio
async def test_oklink_incoming_scan_converts_logs() -> None:
    address = "0x" + "0" * 37 + "abc"
    sender = "0x" + "2" * 40
    token = "0x" + "1" * 40
    tx_hash = "0x" + "3" * 64
    transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith(f"/v2/bsc/addresses/{address}/transfers/condition/token"):
            payload = {
                "code": 0,
                "data": {
                    "total": 1,
                    "hits": [
                        {
                            "txhash": tx_hash,
                            "blockHeight": 100,
                            "from": sender,
                            "to": address,
                            "tokenContractAddress": token,
                            "value": 1.25,
                        }
                    ],
                },
            }
            return httpx.Response(200, json=payload)
        if request.url.path.endswith(f"/v1/bsc/transactions/{tx_hash}/logs"):
            payload = {
                "code": 0,
                "data": [
                    {
                        "logIndex": 7,
                        "txhash": tx_hash,
                        "blockHeight": 100,
                        "address": token,
                        "addressEvm": token,
                        "data": ["0x" + hex(1250000000000000000)[2:].zfill(64)],
                        "topics": [
                            transfer_sig,
                            "0x" + sender[2:].zfill(64),
                            "0x" + address[2:].zfill(64),
                        ],
                    },
                    {
                        "logIndex": 8,
                        "txhash": tx_hash,
                        "blockHeight": 100,
                        "address": token,
                        "data": ["0x1"],
                        "topics": [
                            transfer_sig,
                            "0x" + sender[2:].zfill(64),
                            "0x" + ("4" * 40).zfill(64),
                        ],
                    },
                ],
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url="https://oklink.test",
            api_prefix="/api/explorer/",
            referer="https://oklink.test/bsc",
            user_agent="pytest",
            web_key="abcdefgh12345678",
            transfer_event_signature=transfer_sig,
            page_limit=10,
            request_timeout_seconds=5,
            request_delay_seconds=0,
            max_pages_per_address=2,
            max_log_pages_per_tx=2,
            api_key_time_shift_ms=1111111111111,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://oklink.test"),
    )

    logs = await client.fetch_incoming_token_transfer_logs(
        "bsc",
        [address],
        [token],
        90,
        110,
    )
    result = await OKLinkTransferLogFetcher("bsc", client).fetch_transfer_logs(
        90,
        110,
        [address],
        [token],
    )
    await client.aclose()

    assert len(logs) == 1
    assert result.is_complete is True
    assert result.failed_address_count == 0
    assert result.method_used.value == "oklink_address_token_transfers"
    assert len(result.logs) == 1
    assert logs[0]["transactionHash"] == tx_hash
    assert logs[0]["logIndex"] == 7
    assert logs[0]["blockNumber"] == 100
    assert logs[0]["address"] == token
    assert logs[0]["data"] == "0x" + hex(1250000000000000000)[2:].zfill(64)
    assert all(request.url.params.get("t") for request in requests)
    assert all(request.headers.get("x-apiKey") for request in requests)
    address_requests = [
        request
        for request in requests
        if request.url.path.endswith(f"/v2/bsc/addresses/{address}/transfers/condition/token")
    ]
    log_requests = [
        request
        for request in requests
        if request.url.path.endswith(f"/v1/bsc/transactions/{tx_hash}/logs")
    ]
    assert all(request.method == "POST" for request in address_requests)
    assert all(json.loads(request.content)["address"] == address for request in address_requests)
    assert all(request.method == "GET" for request in log_requests)


@pytest.mark.asyncio
async def test_oklink_address_scan_stops_at_older_block() -> None:
    address = "0x" + "0" * 37 + "abc"
    token = "0x" + "1" * 40
    transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = {
            "code": 0,
            "data": {
                "total": 1,
                "hits": [
                    {
                        "txhash": "0x" + "5" * 64,
                        "blockHeight": 80,
                        "from": "0x" + "2" * 40,
                        "to": address,
                        "tokenContractAddress": token,
                        "value": 1,
                    }
                ],
            },
        }
        return httpx.Response(200, json=payload)

    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url="https://oklink.test",
            api_prefix="/api/explorer/",
            referer="https://oklink.test/bsc",
            user_agent="pytest",
            web_key="abcdefgh12345678",
            transfer_event_signature=transfer_sig,
            page_limit=1,
            request_timeout_seconds=5,
            request_delay_seconds=0,
            max_pages_per_address=3,
            max_log_pages_per_tx=1,
            api_key_time_shift_ms=1111111111111,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://oklink.test"),
    )

    logs = await client.fetch_incoming_token_transfer_logs("bsc", [address], [token], 90, 100)
    await client.aclose()

    assert logs == []
    assert calls == 1


def test_generate_oklink_web_api_key_matches_browser_shape() -> None:
    key = generate_oklink_web_api_key(
        "abcdefgh12345678",
        1111111111111,
        now_ms=1000,
        nonce=7,
    )
    decoded = base64.b64decode(key.encode()).decode()

    assert decoded == "12345678abcdefgh|1111111112111007"


def test_oklink_json_parser_keeps_decimal_precision() -> None:
    raw = '{"code":0,"data":{"hits":[{"value":1.234567890123456789}]}}'
    payload = json.loads(raw, parse_float=Decimal)

    assert payload["data"]["hits"][0]["value"] == Decimal("1.234567890123456789")


@pytest.mark.asyncio
async def test_oklink_address_scan_marks_incomplete_when_page_limit_reached() -> None:
    address = "0x" + "0" * 37 + "abc"
    token = "0x" + "1" * 40
    transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "code": 0,
            "data": {
                "total": 2,
                "hits": [
                    {
                        "txhash": "0x" + "6" * 64,
                        "blockHeight": 100,
                        "from": "0x" + "2" * 40,
                        "to": address,
                        "tokenContractAddress": "0x" + "9" * 40,
                        "value": 1,
                    }
                ],
            },
        }
        return httpx.Response(200, json=payload)

    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url="https://oklink.test",
            api_prefix="/api/explorer/",
            referer="https://oklink.test/bsc",
            user_agent="pytest",
            web_key="abcdefgh12345678",
            transfer_event_signature=transfer_sig,
            page_limit=1,
            request_timeout_seconds=5,
            request_delay_seconds=0,
            max_pages_per_address=1,
            max_log_pages_per_tx=1,
            api_key_time_shift_ms=1111111111111,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://oklink.test"),
    )

    result = await OKLinkTransferLogFetcher("bsc", client).fetch_transfer_logs(
        90,
        110,
        [address],
        [token],
    )
    await client.aclose()

    assert result.is_complete is False
    assert result.failed_address_count == 1


@pytest.mark.asyncio
async def test_oklink_tx_log_scan_marks_incomplete_when_page_limit_reached() -> None:
    address = "0x" + "0" * 37 + "abc"
    token = "0x" + "1" * 40
    tx_hash = "0x" + "7" * 64
    transfer_sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/v2/bsc/addresses/{address}/transfers/condition/token"):
            payload = {
                "code": 0,
                "data": {
                    "total": 1,
                    "hits": [
                        {
                            "txhash": tx_hash,
                            "blockHeight": 100,
                            "from": "0x" + "2" * 40,
                            "to": address,
                            "tokenContractAddress": token,
                            "value": 1,
                        }
                    ],
                },
            }
            return httpx.Response(200, json=payload)
        if request.url.path.endswith(f"/v1/bsc/transactions/{tx_hash}/logs"):
            payload = {
                "code": 0,
                "data": [
                    {
                        "logIndex": 1,
                        "txhash": tx_hash,
                        "blockHeight": 100,
                        "address": token,
                        "data": ["0x1"],
                        "topics": [
                            transfer_sig,
                            "0x" + ("2" * 40).zfill(64),
                            "0x" + address[2:].zfill(64),
                        ],
                    }
                ],
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"code": 404, "msg": "not found"})

    client = OKLinkExplorerClient(
        OKLinkClientConfig(
            base_url="https://oklink.test",
            api_prefix="/api/explorer/",
            referer="https://oklink.test/bsc",
            user_agent="pytest",
            web_key="abcdefgh12345678",
            transfer_event_signature=transfer_sig,
            page_limit=1,
            request_timeout_seconds=5,
            request_delay_seconds=0,
            max_pages_per_address=2,
            max_log_pages_per_tx=1,
            api_key_time_shift_ms=1111111111111,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://oklink.test"),
    )

    result = await OKLinkTransferLogFetcher("bsc", client).fetch_transfer_logs(
        90,
        110,
        [address],
        [token],
    )
    await client.aclose()

    assert result.is_complete is False
    assert result.failed_address_count == 1
    assert len(result.logs) == 1
