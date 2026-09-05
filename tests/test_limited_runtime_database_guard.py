"""Fail-closed limited runtime database guard tests."""

from __future__ import annotations

import pytest

import src.db.session as session_module


class Settings:
    database_runtime_role_enabled = True

    def validate_limited_runtime_environment(self):
        return None


class Result:
    def __init__(self, row):
        self.row = row

    def one(self):
        return self.row


class Connection:
    def __init__(self, row):
        self.row = row

    async def execute(self, _statement, _parameters=None):
        return Result(self.row)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Engine:
    def __init__(self, row):
        self.row = row

    def connect(self):
        return Connection(self.row)


def install_guard_engine(monkeypatch, row):
    monkeypatch.setattr(session_module, "get_settings", lambda: Settings())
    monkeypatch.setattr(session_module, "get_engine", lambda: Engine(row))


@pytest.mark.asyncio
async def test_limited_runtime_guard_rejects_bootstrap_without_database(monkeypatch):
    monkeypatch.setattr(session_module, "get_settings", lambda: Settings())
    with pytest.raises(RuntimeError, match="init_db is forbidden"):
        await session_module.init_db()


@pytest.mark.asyncio
async def test_limited_runtime_role_guard_rejects_non_public_search_path(monkeypatch):
    install_guard_engine(
        monkeypatch,
        (
            "runtime",
            '"$user", public',
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
            False,
        ),
    )
    with pytest.raises(RuntimeError, match="search_path"):
        await session_module.verify_limited_runtime_database_role()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag_index",
    [0, 6, 7, 8, 9, 10, 11, 12, 13, 14],
    ids=[
        "superuser",
        "membership",
        "database_owner",
        "schema_owner",
        "relation_owner",
        "function_owner",
        "database_create",
        "schema_create",
        "public_security_definer_execute",
        "protected_table_truncate",
    ],
)
async def test_limited_runtime_role_guard_rejects_effective_privilege_and_ownership(
    monkeypatch, flag_index
):
    flags = [False] * 15
    flags[flag_index] = True
    row = ("runtime", "public, pg_catalog", *flags)
    install_guard_engine(monkeypatch, row)
    with pytest.raises(RuntimeError, match="elevated ownership or DDL privilege"):
        await session_module.verify_limited_runtime_database_role()
