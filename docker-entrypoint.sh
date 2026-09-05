#!/bin/bash
# Runtime admission and launch. Schema migrations are an external migration-owner
# operation, never a runtime container action.

set -euo pipefail

if [ "${DATABASE_RUNTIME_ROLE_ENABLED:-false}" = "true" ] && [ -n "${MIGRATION_DATABASE_URL:-}" ]; then
    echo "limited runtime must not receive MIGRATION_DATABASE_URL" >&2
    exit 1
fi

python <<'PY'
import asyncio

from src.db.session import require_runtime_database_ready


asyncio.run(require_runtime_database_ready())
PY

if [ "$#" -gt 0 ]; then
    exec "$@"
else
    exec uvicorn src.main:app --host 0.0.0.0 --port 8000
fi
