"""Runtime credential isolation and generic readiness admission tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.main as main_module
from src.core.config import Settings

_RUNTIME_COMPOSE_SERVICES = (
    "api",
    "worker-poller",
    "worker-webhook",
    "worker-sweeper",
    "worker-expirer",
)


def test_limited_runtime_compose_override_replaces_base_env_file(monkeypatch, tmp_path):
    """Render the real base+override pair without consulting a repository dotenv."""

    root = Path(__file__).resolve().parent.parent
    owner_database_marker = "OWNER_DATABASE_URL_CANARY"
    owner_redis_marker = "OWNER_REDIS_PASSWORD_CANARY"
    owner_postgres_marker = "OWNER_POSTGRES_PASSWORD_CANARY"
    runtime_database_marker = "RUNTIME_DATABASE_URL_CANARY"
    runtime_redis_marker = "RUNTIME_REDIS_URL_CANARY"
    raw_literals = {
        "RAW_DOLLAR": "$foo",
        "RAW_BRACED": "${foo}",
        "RAW_HASH": "#value",
        "RAW_QUOTES": "'\"",
        "RAW_SPACES": " leading trailing ",
        "RAW_UNICODE": "ключ-ю",
        "RAW_BACKSLASH": r"a\b",
    }
    runtime_env = tmp_path / "curated-runtime.env"
    runtime_env.write_text(
        "\n".join(
            (
                "DATABASE_RUNTIME_ROLE_ENABLED=true",
                f"DATABASE_URL=postgresql+asyncpg://runtime:{runtime_database_marker}@postgres:5432/runtime",
                f"REDIS_URL=redis://:{runtime_redis_marker}@redis:6379/0",
                "SECRET_KEY=runtime-compose-test-secret-at-least-thirty-two-chars",
                "ENCRYPTION_KEY=hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
                "RUNTIME_ENV_CANARY=present",
                *(f"{key}={value}" for key, value in raw_literals.items()),
                "",
            )
        )
    )
    operator_env = tmp_path / "operator.env"
    operator_env.write_text(
        "\n".join(
            (
                f"RUNTIME_ENV_FILE={runtime_env}",
                f"DATABASE_URL=postgresql+asyncpg://owner:{owner_database_marker}@postgres:5432/owner",
                f"REDIS_PASSWORD={owner_redis_marker}",
                f"POSTGRES_PASSWORD={owner_postgres_marker}",
                "",
            )
        )
    )
    subprocess_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("COMPOSE_")
        and key
        not in {
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "REDIS_URL",
            "REDIS_PASSWORD",
            "POSTGRES_PASSWORD",
            "POSTGRES_USER",
            "POSTGRES_DB",
            "RUNTIME_ENV_FILE",
        }
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(operator_env),
            "-f",
            str(root / "docker-compose.yml"),
            "-f",
            str(root / "docker-compose.runtime-role.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=20,
        env=subprocess_env,
    )
    assert result.returncode == 0, "real Compose base+override render failed"
    rendered = json.loads(result.stdout)
    for service_name in _RUNTIME_COMPOSE_SERVICES:
        service = rendered["services"][service_name]
        environment = service["environment"]
        serialized = json.dumps(service, sort_keys=True)
        assert environment["DATABASE_URL"].endswith(
            f":{runtime_database_marker}@postgres:5432/runtime"
        )
        assert environment["REDIS_URL"] == f"redis://:{runtime_redis_marker}@redis:6379/0"
        assert environment["SECRET_KEY"].startswith("runtime-compose-test-secret-")
        assert environment["ENCRYPTION_KEY"] == "hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA="
        assert environment["RUNTIME_ENV_CANARY"] == "present"
        for key, value in raw_literals.items():
            rendered_value = value.replace("$", "$$")
            assert environment[key] == rendered_value
        assert environment["DATABASE_RUNTIME_ROLE_ENABLED"] == "true"
        assert environment.get("MIGRATION_DATABASE_URL") in (None, "")
        assert "POSTGRES_PASSWORD" not in environment
        assert service.get("entrypoint") is None
        assert owner_database_marker not in serialized
        assert owner_redis_marker not in serialized
        assert owner_postgres_marker not in serialized

    healthcheck = rendered["services"]["api"]["healthcheck"]
    assert healthcheck["test"] == ["CMD", "curl", "-f", "http://localhost:8000/ready"]


@pytest.mark.parametrize(
    "forbidden_key",
    ["MIGRATION_DATABASE_URL", "POSTGRES_PASSWORD", "DATABASE_OWNER_URL"],
)
def test_limited_settings_reject_effective_owner_environment(monkeypatch, forbidden_key):
    for name in ("MIGRATION_DATABASE_URL", "POSTGRES_PASSWORD", "DATABASE_OWNER_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(forbidden_key, "owner-marker")
    configured = Settings(
        _env_file=None,
        secret_key="runtime-container-secret-at-least-thirty-two-chars",
        encryption_key="hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
        database_runtime_role_enabled=True,
        database_url="postgresql+asyncpg://runtime",
        migration_database_url="postgresql+asyncpg://migration",
    )
    with pytest.raises(RuntimeError, match="forbidden owner key names"):
        configured.validate_limited_runtime_environment()


def test_settings_do_not_load_dotenv_by_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "SECRET_KEY=dotenv-owner-marker\n"
        "ENCRYPTION_KEY=dotenv-owner-marker\n"
        "DATABASE_URL=postgresql+asyncpg://dotenv-owner\n"
    )
    configured = Settings(
        _env_file=None,
        secret_key="explicit-runtime-secret-at-least-thirty-two-chars",
        encryption_key="hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
        database_url="postgresql+asyncpg://explicit-runtime",
    )
    assert configured.database_url == "postgresql+asyncpg://explicit-runtime"


@pytest.mark.asyncio
async def test_ready_returns_generic_503_when_database_or_redis_unavailable(monkeypatch):
    settings = SimpleNamespace(
        app_name="test",
        app_version="test",
        debug=False,
        cors_origins_list=["*"],
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    async def unavailable_database():
        raise RuntimeError("postgres password=must-not-leak")

    monkeypatch.setattr(main_module, "require_runtime_database_ready", unavailable_database)
    app = main_module.create_app()
    ready_endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/ready"
    )
    with pytest.raises(HTTPException) as exc:
        await ready_endpoint()
    assert exc.value.status_code == 503
    assert exc.value.detail == "service unavailable"

    async def ready_database():
        return None

    class Redis:
        async def ping(self):
            raise RuntimeError("redis password=must-not-leak")

    async def unavailable_redis():
        return Redis()

    monkeypatch.setattr(main_module, "require_runtime_database_ready", ready_database)
    monkeypatch.setattr(main_module, "get_redis", unavailable_redis)
    app = main_module.create_app()
    ready_endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/ready"
    )
    with pytest.raises(HTTPException) as exc:
        await ready_endpoint()
    assert exc.value.status_code == 503
    assert exc.value.detail == "service unavailable"

    health_endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/health"
    )
    assert (await health_endpoint())["status"] == "healthy"
