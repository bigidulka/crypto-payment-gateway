#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR=""
if [[ "${1:-}" == "--output-dir" ]]; then
    [[ $# -eq 2 ]] || { echo "usage: $0 [--output-dir DIR]" >&2; exit 2; }
    OUTPUT_DIR="$2"
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--output-dir DIR]" >&2
    exit 2
fi

TMP_ROOT="$(mktemp -d /tmp/arbitron-sdk-install.XXXXXX)"
trap 'rm -rf -- "$TMP_ROOT"' EXIT
DIST_DIR="$TMP_ROOT/dist"
VENV_DIR="$TMP_ROOT/venv"
mkdir -p "$DIST_DIR"

"${SDK_TEST_PYTHON:-python3}" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --no-input --quiet --upgrade pip

uv build --project "$ROOT_DIR/sdk/python" --wheel --out-dir "$DIST_DIR" >/dev/null
WHEEL="$(printf '%s\n' "$DIST_DIR"/arbitron_sdk-*.whl | sed -n '1p')"
if [[ ! -f "$WHEEL" ]]; then
    echo "SDK wheel not found in temporary build directory" >&2
    exit 1
fi

"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --no-input --quiet "$WHEEL"

cd /tmp
env -u PYTHONPATH "$VENV_DIR/bin/python" -I - "$ROOT_DIR" <<'PY'
import json
import sys
import time
from importlib.metadata import version
from pathlib import Path

root_dir = Path(sys.argv[1]).resolve()
import arbitron_sdk
from arbitron_sdk import ArbitronClient, PaymentStatus, verify_webhook
from arbitron_sdk.models import InvoiceStatus
from arbitron_sdk.webhooks import compute_signature

module_path = Path(arbitron_sdk.__file__).resolve()
site_packages = Path(sys.prefix).resolve() / "lib"
if not module_path.is_relative_to(site_packages):
    raise SystemExit(f"module is outside venv site-packages: {module_path}")
if module_path.is_relative_to(root_dir):
    raise SystemExit(f"module unexpectedly resolved from repository: {module_path}")

assert version("arbitron-sdk") == arbitron_sdk.__version__
assert ArbitronClient.__name__ == "ArbitronClient"
assert PaymentStatus.__name__ == "PaymentStatus"
assert InvoiceStatus.CONFIRMED.value == "CONFIRMED"

payload = json.dumps(
    {"event": "invoice.confirmed", "invoice": {"id": "smoke"}},
    separators=(",", ":"),
).encode()
timestamp = int(time.time())
secret = "packaging-smoke-secret"
signature = compute_signature(payload, secret, timestamp)
event = verify_webhook(
    payload,
    {
        "X-Webhook-Signature": signature,
        "X-Webhook-Timestamp": str(timestamp),
        "X-Webhook-Event": "invoice.confirmed",
    },
    secret,
    now=timestamp,
)
assert event.event_type == "invoice.confirmed"

print("sdk_import=ok")
print("sdk_module=" + str(module_path))
print("metadata_version=" + version("arbitron-sdk"))
print("exports=ArbitronClient,PaymentStatus,verify_webhook")
print("verify_webhook=ok")
PY

if [[ -n "$OUTPUT_DIR" ]]; then
    mkdir -p "$OUTPUT_DIR"
    cp -- "$WHEEL" "$OUTPUT_DIR/"
    printf 'copied_wheel=%s\n' "$OUTPUT_DIR/$(basename "$WHEEL")"
fi

printf 'sdk_install_smoke=ok\n'
