"""Runtime/migration database URL selection regression tests."""

from __future__ import annotations

import pytest

from src.core.config import Settings


@pytest.fixture(autouse=True)
def _clear_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests assert configuration defaults, so ambient DATABASE_* values must not leak in."""

    for name in ("DATABASE_URL", "MIGRATION_DATABASE_URL", "DATABASE_RUNTIME_ROLE_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def settings(**overrides) -> Settings:
    return Settings(
        secret_key="runtime-config-secret-at-least-thirty-two-chars",
        encryption_key="hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
        **overrides,
    )


def test_default_mode_preserves_database_url_for_runtime_and_migration():
    configured = settings(database_url="postgresql+asyncpg://default")
    assert configured.get_runtime_database_url() == "postgresql+asyncpg://default"
    assert configured.get_migration_database_url() == "postgresql+asyncpg://default"


def test_limited_runtime_mode_requires_only_explicit_migration_owner_url():
    missing = settings(
        database_url="postgresql+asyncpg://runtime",
        database_runtime_role_enabled=True,
    )
    assert missing.get_runtime_database_url() == "postgresql+asyncpg://runtime"
    with pytest.raises(RuntimeError, match="MIGRATION_DATABASE_URL"):
        missing.get_migration_database_url()

    configured = settings(
        database_url="postgresql+asyncpg://runtime",
        database_runtime_role_enabled=True,
        migration_database_url="postgresql+asyncpg://migration-owner",
    )
    assert configured.get_runtime_database_url() == "postgresql+asyncpg://runtime"
    assert configured.get_migration_database_url() == "postgresql+asyncpg://migration-owner"
