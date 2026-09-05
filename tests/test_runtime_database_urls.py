"""Runtime/migration database URL selection regression tests."""

from __future__ import annotations

import pytest

from src.core.config import Settings


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
