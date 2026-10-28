"""Payment URI contract units and the actual hosted script's QR payload."""

import json
import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from src.api.hosted.router import _render_payment_page
from src.db.models import InvoiceStatus
from src.payments.checkout import checkout_uri


@pytest.mark.parametrize(
    ("chain", "token", "amount", "scheme", "parameters"),
    [
        ("base", "USDC", "40.123456", "ethereum", {"uint256": ["40123456"]}),
        ("bsc", "USDT", "40.123456", "ethereum", {"uint256": ["40123456000000000000"]}),
        ("base", "ETH", "0.000000001", "ethereum", {"value": ["1000000000"]}),
        (
            "solana",
            "USDC",
            "40.123456",
            "solana",
            {
                "amount": ["40.123456"],
                "spl-token": ["EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"],
            },
        ),
        ("solana", "SOL", "1E-9", "solana", {"amount": ["0.000000001"]}),
        (
            "ton",
            "USDT",
            "40.123456",
            "ton",
            {
                "amount": ["40123456"],
                "jetton": ["EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"],
            },
        ),
        ("ton", "TON", "1E-9", "ton", {"amount": ["1"]}),
    ],
)
def test_checkout_request_uses_protocol_units(chain, token, amount, scheme, parameters):
    address = "0x" + "11" * 20 if scheme == "ethereum" else "11111111111111111111111111111111"
    if scheme == "ton":
        address = "0:" + "11" * 32
    uri = urlsplit(checkout_uri(chain, token, Decimal(amount), address, "test memo + café"))
    query = parse_qs(uri.query)
    assert uri.scheme == scheme
    for key, value in parameters.items():
        assert query[key] == value
    if scheme == "ethereum":
        chain_id = 8453 if chain == "base" else 56
        if token == "ETH":
            assert uri.path == f"{address}@{chain_id}" and query == parameters
        else:
            contract = (
                "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
                if chain == "base"
                else "0x55d398326f99059fF775485246999027B3197955"
            )
            assert uri.path == f"{contract}@{chain_id}/transfer"
            assert query == {**parameters, "address": [address]}
    elif scheme == "solana":
        assert uri.path == address
        assert query == {**parameters, "memo": ["test memo + café"]}
    else:
        assert uri.netloc == "transfer" and uri.path == "/" + address
        assert query == {**parameters, "text": ["test memo + café"]}


def test_checkout_uri_rejects_fractional_atoms():
    with pytest.raises(ValueError, match="precision"):
        checkout_uri("base", "USDC", Decimal("0.0000001"), "0x" + "11" * 20, "test")


def test_hosted_script_draws_payment_uri_on_reload_and_selection():
    address = "0x" + "11" * 20
    expected = (
        "ethereum:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@8453/transfer"
        f"?address={address}&uint256=40123456"
    )
    invoice = SimpleNamespace(
        public_id="test-checkout",
        amount=Decimal("40.123456"),
        asset="USDC",
        status=InvoiceStatus.AWAITING_PAYMENT,
        allowed_chains=["base"],
        expires_at=datetime(2031, 1, 1, tzinfo=UTC),
        merchant=None,
        payment_sessions=[
            SimpleNamespace(
                chain="base", token="USDC", deposit_address=SimpleNamespace(address=address)
            )
        ],
    )
    page = _render_payment_page(invoice, False)
    assert 'data-token="USDC"' in page and 'data-token="USDT"' not in page
    script = re.findall(r"<script>(.*?)</script>", page, re.DOTALL)[-1]
    driver = r"""
const vm = require('node:vm');
let input = '';
process.stdin.on('data', data => input += data);
process.stdin.on('end', async () => {
  const {script, payload} = JSON.parse(input);
  const qr = [], elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      classList: {add() {}, remove() {}, toggle() {}}, style: {}
    });
    return elements.get(id);
  };
  let ready;
  const context = vm.createContext({
    document: {addEventListener: (event, callback) => ready = callback,
               querySelectorAll: () => [], getElementById: element},
    QRCode: {toCanvas: (canvas, value) => qr.push(value)},
    setInterval() {}, setTimeout() {}, alert: message => { throw Error(message); },
    fetch: async url => ({ok: true, json: async () => url.endsWith('/select') ? payload :
                         url.endsWith('/methods') ? {items: []} : {status: 'CONFIRMED'}})
  });
  vm.runInContext(script, context, {timeout: 1000});
  ready();
  await vm.runInContext('confirmSelection()', context, {timeout: 1000});
  process.stdout.write(JSON.stringify(qr));
});
"""
    result = subprocess.run(
        ["node", "-e", driver],
        input=json.dumps(
            {
                "script": script,
                "payload": {
                    "deposit_address": address,
                    "qr_data": expected,
                    "chain_name": "Base",
                    "token": "USDC",
                    "explorer_address_url": "https://basescan.org/address/" + address,
                },
            }
        ),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == [expected, expected]
