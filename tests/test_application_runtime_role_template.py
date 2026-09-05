"""Executable fail-closed provisioning template tests on disposable PostgreSQL."""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "provision_application_runtime_role.sql"


def template_target() -> tuple[str, str]:
    url = os.getenv("APP_ROLE_TEMPLATE_DATABASE_URL")
    container = os.getenv("APP_ROLE_TEMPLATE_CONTAINER")
    if not url or not container:
        pytest.fail("APP_ROLE_TEMPLATE_DATABASE_URL and APP_ROLE_TEMPLATE_CONTAINER are required")
    parsed = make_url(url)
    if parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "::1"}:
        pytest.fail("APP_ROLE_TEMPLATE_DATABASE_URL must be loopback postgresql+asyncpg")
    if not parsed.database or not parsed.database.startswith("test_"):
        pytest.fail("APP_ROLE_TEMPLATE_DATABASE_URL database must start with test_")
    return url.replace("postgresql+asyncpg://", "postgresql://", 1), container


async def owner_connection() -> asyncpg.Connection:
    dsn, _ = template_target()
    return await asyncpg.connect(dsn)


def apply_template(role: str, migration_owner: str = "ledger") -> subprocess.CompletedProcess[str]:
    database_url, container = template_target()
    database = make_url(database_url).database
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "ledger",
            "-d",
            database,
            "-v",
            f"application_runtime_role={role}",
            "-v",
            "application_runtime_password=template-local-password",
            "-v",
            f"migration_owner_role={migration_owner}",
        ],
        input=TEMPLATE.read_text(),
        text=True,
        capture_output=True,
        check=False,
    )


async def configure_existing_role(connection: asyncpg.Connection, role: str) -> None:
    await connection.execute(f'CREATE ROLE "{role}" LOGIN NOINHERIT')
    await connection.execute(f'ALTER ROLE "{role}" SET search_path = public, pg_catalog')


async def table_dml_granted(connection: asyncpg.Connection, role: str) -> bool:
    return bool(
        await connection.fetchval(
            "SELECT has_table_privilege($1, 'public.ledger_transactions', 'SELECT')", role
        )
    )


@pytest.mark.asyncio
async def test_template_source_uses_atomic_transaction_and_transaction_local_password_guc():
    source = TEMPLATE.read_text()
    assert "BEGIN;" in source and "COMMIT;" in source
    assert (
        "set_config('application.runtime_password', :'application_runtime_password', true)"
        in source
    )
    for clause in (
        "role memberships",
        "owns database, schema, relation, or function",
        "effective CREATE or TRUNCATE privilege",
        "security-definer function",
        "exact controlled search_path",
    ):
        assert clause in source
    assert "\nALTER DEFAULT PRIVILEGES" not in source


@pytest.mark.asyncio
async def test_template_happy_path_is_idempotent_and_does_not_persist_password_guc():
    connection = await owner_connection()
    role = f"happy_{uuid.uuid4().hex[:12]}"
    try:
        first = apply_template(role)
        second = apply_template(role)
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        row = await connection.fetchrow(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolinherit, rolreplication, rolbypassrls, rolcanlogin, rolconfig FROM pg_roles WHERE rolname=$1",
            role,
        )
        assert row is not None
        assert tuple(row[:6]) == (False, False, False, False, False, False)
        assert row[6] is True
        assert row[7] == ["search_path=public, pg_catalog"]
        assert await table_dml_granted(connection, role)
    finally:
        await connection.execute(f'DROP OWNED BY "{role}"')
        await connection.execute(f'DROP ROLE IF EXISTS "{role}"')
        await connection.close()


@pytest.mark.asyncio
async def test_template_missing_migration_owner_creates_no_candidate_role():
    connection = await owner_connection()
    role = f"absent_{uuid.uuid4().hex[:12]}"
    try:
        result = apply_template(role, migration_owner=f"missing_{uuid.uuid4().hex[:12]}")
        assert result.returncode != 0
        assert not await connection.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_template_rejects_privileged_existing_roles_without_new_grants():
    connection = await owner_connection()
    database = await connection.fetchval("SELECT current_database()")
    cleanup: list[str] = []
    try:
        for kind in (
            "superuser",
            "member",
            "database_owner",
            "schema_owner",
            "relation_owner",
            "function_owner",
            "truncate",
            "public_create",
        ):
            role = f"reject_{kind}_{uuid.uuid4().hex[:10]}"
            cleanup.append(role)
            await configure_existing_role(connection, role)
            if kind == "superuser":
                await connection.execute(f'ALTER ROLE "{role}" SUPERUSER')
            elif kind == "member":
                parent = f"parent_{uuid.uuid4().hex[:10]}"
                cleanup.append(parent)
                await connection.execute(f'CREATE ROLE "{parent}" NOLOGIN')
                await connection.execute(f'GRANT "{parent}" TO "{role}"')
            elif kind == "database_owner":
                await connection.execute(f'ALTER DATABASE "{database}" OWNER TO "{role}"')
            elif kind == "schema_owner":
                await connection.execute(f'CREATE SCHEMA "owned_{role}" AUTHORIZATION "{role}"')
            elif kind == "relation_owner":
                await connection.execute(f'CREATE TABLE public."owned_{role}" (id integer)')
                await connection.execute(f'ALTER TABLE public."owned_{role}" OWNER TO "{role}"')
            elif kind == "function_owner":
                await connection.execute(
                    f'CREATE FUNCTION public."owned_{role}"() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$'
                )
                await connection.execute(
                    f'ALTER FUNCTION public."owned_{role}"() OWNER TO "{role}"'
                )
            elif kind == "truncate":
                await connection.execute(f'GRANT TRUNCATE ON public.ledger_entries TO "{role}"')
            elif kind == "public_create":
                await connection.execute("GRANT CREATE ON SCHEMA public TO PUBLIC")

            before_dml = await table_dml_granted(connection, role)
            result = apply_template(role)
            assert result.returncode != 0, kind
            assert await table_dml_granted(connection, role) == before_dml, kind

            if kind == "database_owner":
                await connection.execute(f'ALTER DATABASE "{database}" OWNER TO ledger')
            if kind == "public_create":
                await connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    finally:
        for role in reversed(cleanup):
            await connection.execute(f'DROP OWNED BY "{role}"')
            await connection.execute(f'DROP ROLE IF EXISTS "{role}"')
        await connection.close()
