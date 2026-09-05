#!/usr/bin/env python3
"""Prepare additive ledger schema, a new runtime role, and raw runtime env files.

Default ``check`` is read-only. ``prepare --confirm-prepare`` has no Compose
service switch command: it may migrate, create only the configured new role,
and publish external 0600 runtime env files. It never starts API/workers,
performs financial writes, resumes automatically, or downgrades.

Every subprocess is an argv list with shell=False. Parent shell/argv/output and
the durable journal never contain a secret. Owner credentials expand only
inside the preconfigured PostgreSQL container; migration credentials exist only
in an owned ephemeral 0600 env file. DB and filesystem changes cannot be
atomic, so a cumulative durable journal preserves the last successful phase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote

SCRIPT_PATH = Path(__file__).resolve()

ROLE = "arbitron_runtime"
SERVICES = ("api", "worker-poller", "worker-webhook", "worker-sweeper", "worker-expirer")
FORBIDDEN_PREFIXES = ("POSTGRES_", "MIGRATION_", "OWNER_")
FORBIDDEN_NAMES = {"PGPASSWORD", "DATABASE_OWNER_URL", "OWNER_DATABASE_URL"}


class PreparationError(RuntimeError):
    """A gate failed without relaying child stderr or secret-bearing output."""


class Phase(StrEnum):
    CHECKED = "checked"
    MIGRATED = "migrated"
    PENDING_PASSWORD = "pending_password"
    ROLE_CREATED = "role_created"
    RUNTIME_ENV_PUBLISHED = "runtime_env_published"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class Config:
    root: Path
    expected_sha: str
    source_manifest: Path
    migration_image: str
    migration_image_id: str
    runtime_image: str
    runtime_image_id: str
    postgres_container: str
    postgres_network: str
    external_dir: Path
    backup_path: Path
    backup_sha256: str
    service_containers: tuple[tuple[str, str], ...]
    migration_owner: str = "arbitron"
    role_name: str = ROLE
    lock_timeout_ms: int = 5_000
    statement_timeout_ms: int = 120_000

    def __post_init__(self) -> None:
        if self.role_name != ROLE:
            raise PreparationError("only new role arbitron_runtime is accepted")
        if tuple(name for name, _ in self.service_containers) != SERVICES:
            raise PreparationError(
                "service mapping must contain the five approved services in order"
            )
        if (
            not 1_000 <= self.lock_timeout_ms <= 300_000
            or not 1_000 <= self.statement_timeout_ms <= 300_000
        ):
            raise PreparationError("migration timeout outside reviewed bounds")

    @property
    def journal(self) -> Path:
        return self.external_dir / "runtime-schema-role-preparation.manifest.json"

    @property
    def operation_lock(self) -> Path:
        return self.external_dir / ".runtime-schema-role.lock"

    @property
    def pending(self) -> Path:
        return self.external_dir / f"{self.role_name}.pending"

    def runtime_env(self, service: str) -> Path:
        return self.external_dir / f"runtime-{service}.env"

    def container_for(self, service: str) -> str:
        return dict(self.service_containers)[service]


class Runner:
    """Injectable argv-only adapter. Tests can inject failures without Docker."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        cwd: Path | None = None,
        timeout: int = 180,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            input=input_text,
            cwd=cwd,
            timeout=timeout,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def run_checked(
    runner: Runner,
    argv: Sequence[str],
    label: str,
    *,
    input_text: str | None = None,
    cwd: Path | None = None,
    timeout: int = 180,
) -> str:
    try:
        result = runner.run(argv, input_text=input_text, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PreparationError(f"{label}: timed out") from exc
    if result.returncode:
        raise PreparationError(f"{label}: exit {result.returncode}")
    return result.stdout.strip()


def assert_secure(path: Path, mode: int, *, directory: bool) -> None:
    try:
        value = path.lstat()
    except FileNotFoundError as exc:
        raise PreparationError(f"{path}: missing") from exc
    type_ok = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if stat.S_ISLNK(value.st_mode) or not type_ok:
        raise PreparationError(f"{path}: type/symlink refusal")
    if (
        value.st_uid != os.getuid()
        or value.st_gid != os.getgid()
        or stat.S_IMODE(value.st_mode) != mode
        or (not directory and value.st_nlink != 1)
    ):
        raise PreparationError(f"{path}: ownership/mode/link refusal")


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        count = os.write(fd, content[offset:])
        if count <= 0:
            raise PreparationError("secure write failed")
        offset += count


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_exclusive(path: Path, content: str) -> tuple[int, int]:
    assert_secure(path.parent, 0o700, directory=True)
    fd: int | None = None
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        _write_all(fd, content.encode())
        os.fsync(fd)
        identity = os.fstat(fd)
    except FileExistsError as exc:
        raise PreparationError(f"{path}: exists") from exc
    finally:
        if fd is not None:
            os.close(fd)
    _fsync_directory(path.parent)
    assert_secure(path, 0o600, directory=False)
    return identity.st_dev, identity.st_ino


def unlink_owned(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        raise PreparationError(f"{path}: cleanup ownership refusal")
    path.unlink()
    _fsync_directory(path.parent)


def publish_exclusive(path: Path, content: str) -> None:
    assert_secure(path.parent, 0o700, directory=True)
    if path.exists() or path.is_symlink():
        raise PreparationError(f"{path}: refusing overwrite")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, content.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(temp, path)
        _fsync_directory(path.parent)
    except FileExistsError as exc:
        raise PreparationError(f"{path}: appeared during publish") from exc
    finally:
        Path(temp).unlink(missing_ok=True)
    _fsync_directory(path.parent)
    assert_secure(path, 0o600, directory=False)


def journal(config: Config, phase: Phase, details: Mapping[str, Any]) -> None:
    prior: dict[str, Any] = {}
    if config.journal.exists():
        assert_secure(config.journal, 0o600, directory=False)
        try:
            prior = json.loads(config.journal.read_text())
        except json.JSONDecodeError as exc:
            raise PreparationError("journal invalid") from exc
    last_completed = prior.get("last_completed_phase") if phase == Phase.FAILED else phase
    state = {
        **prior,
        "phase": phase,
        "last_completed_phase": last_completed,
        "phases": [*prior.get("phases", []), phase],
        **details,
    }
    body = json.dumps(state, sort_keys=True, indent=2) + "\n"
    if not config.journal.exists():
        write_exclusive(config.journal, body)
        return
    temporary = config.external_dir / f".{config.journal.name}.{secrets.token_hex(6)}"
    identity = write_exclusive(temporary, body)
    try:
        os.replace(temporary, config.journal)
        _fsync_directory(config.external_dir)
    finally:
        unlink_owned(temporary, identity)


def env_file(values: Mapping[str, str]) -> str:
    """Literal Compose ``format: raw`` and Docker --env-file representation."""
    for key, value in values.items():
        if not key or "=" in key or "\r" in key or "\n" in key:
            raise PreparationError("invalid environment key")
        if "\r" in value or "\n" in value:
            raise PreparationError(f"{key}: newline environment value refused")
    return "\n".join(f"{key}={value}" for key, value in sorted(values.items())) + "\n"


def _image_id(runner: Runner, image: str) -> str:
    return run_checked(
        runner, ["docker", "image", "inspect", "--format", "{{.Id}}", image], "image provenance"
    )


def _container_network(runner: Runner, container: str) -> str:
    raw = run_checked(
        runner,
        ["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", container],
        "postgres network inspection",
    )
    networks = json.loads(raw)
    if len(networks) != 1:
        raise PreparationError("postgres network mapping ambiguous")
    network, attachment = next(iter(networks.items()))
    if "postgres" not in attachment.get("Aliases", []):
        raise PreparationError("postgres network alias missing")
    return network


def docker_env(runner: Runner, container: str) -> dict[str, str]:
    raw = run_checked(
        runner,
        ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container],
        "container environment inspection",
    )
    return {
        line.partition("=")[0]: line.partition("=")[2] for line in raw.splitlines() if "=" in line
    }


def pg_env(runner: Runner, config: Config) -> dict[str, str]:
    values = docker_env(runner, config.postgres_container)
    if any(not values.get(key) for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")):
        raise PreparationError("postgres connection components missing")
    return values


def psql(runner: Runner, config: Config, sql: str, *, timeout: int = 180) -> Any:
    # Existing owner credential expands only inside this already-configured container.
    command = [
        "docker",
        "exec",
        "-i",
        config.postgres_container,
        "sh",
        "-c",
        'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ]
    return json.loads(run_checked(runner, command, "psql", input_text=sql, timeout=timeout))


def database_url(pg: Mapping[str, str], user: str, password: str) -> str:
    return "postgresql+asyncpg://{}:{}@postgres:5432/{}".format(
        quote(user, safe=""), quote(password, safe=""), quote(pg["POSTGRES_DB"], safe="")
    )


def forbidden(key: str) -> bool:
    return (
        key in {"PGPASSWORD", "DATABASE_OWNER_URL", "OWNER_DATABASE_URL"}
        or key.startswith(("POSTGRES_", "MIGRATION_", "OWNER_"))
        or key.endswith("_OWNER_URL")
    )


def settings_names(runner: Runner, config: Config) -> set[str]:
    raw = run_checked(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            config.runtime_image,
            "-c",
            "from src.core.config import Settings; print('\\n'.join(sorted(k.upper() for k in Settings.model_fields)))",
        ],
        "runtime settings inventory",
    )
    return set(raw.splitlines()) | {"TZ"}


def source_runtime_config(runner: Runner, config: Config) -> tuple[dict[str, str], dict[str, Any]]:
    allowed = settings_names(runner, config)
    values: dict[str, dict[str, str]] = {}
    for service in SERVICES:
        candidate = {
            key: value
            for key, value in docker_env(runner, config.container_for(service)).items()
            if key in allowed and not forbidden(key)
        }
        if any(not candidate.get(key) for key in ("SECRET_KEY", "ENCRYPTION_KEY", "REDIS_URL")):
            raise PreparationError(f"{service}: required runtime setting missing")
        env_file(candidate)  # enforce literal line-safe source now, before mutation
        values[service] = candidate
    first = values[SERVICES[0]]
    if any(value != first for value in values.values()):
        raise PreparationError("per-service source configuration differs; refusing loss")
    safe_meta = {
        "keys": sorted(first),
        "fingerprints": {
            key: hashlib.sha256(value.encode()).hexdigest()
            for key, value in first.items()
            if key not in {"DATABASE_URL", "DATABASE_RUNTIME_ROLE_ENABLED"}
        },
    }
    return first, safe_meta


def verify_backup(config: Config) -> dict[str, str]:
    assert_secure(config.external_dir, 0o700, directory=True)
    assert_secure(config.backup_path.parent, 0o700, directory=True)
    assert_secure(config.backup_path, 0o600, directory=False)
    if config.backup_path.stat().st_size <= 0 or not config.backup_sha256:
        raise PreparationError("backup empty or checksum absent")
    actual = hashlib.sha256(config.backup_path.read_bytes()).hexdigest()
    if actual != config.backup_sha256:
        raise PreparationError("backup checksum mismatch")
    return {"path": str(config.backup_path), "sha256": actual}


def gate(runner: Runner, config: Config) -> dict[str, Any]:
    return psql(
        runner,
        config,
        """
SELECT json_build_object($$revision$$,(SELECT version_num FROM alembic_version LIMIT 1),$$role_absent$$,NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='arbitron_runtime'),$$ledger_absent$$,NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='ledger_transactions'),$$public_create$$,EXISTS(SELECT 1 FROM pg_namespace n CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a WHERE n.nspname='public' AND a.grantee=0 AND a.privilege_type='CREATE'),$$public_temp$$,EXISTS(SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a WHERE d.datname=current_database() AND a.grantee=0 AND a.privilege_type='TEMPORARY'),$$public_secdef_count$$,(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef),$$sequence_count$$,(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='S'));
""",
    )


def verify_source_manifest(config: Config, runner: Runner) -> None:
    assert_secure(config.source_manifest, 0o600, directory=False)
    try:
        manifest = json.loads(config.source_manifest.read_text())
    except json.JSONDecodeError as exc:
        raise PreparationError("source manifest invalid") from exc
    patch = run_checked(
        runner, ["git", "diff", "--binary", "HEAD"], "worktree patch", cwd=config.root
    )
    expected = {
        "source_sha": config.expected_sha,
        "migration_image_id": config.migration_image_id,
        "runtime_image_id": config.runtime_image_id,
        "worktree_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "script_sha256": hashlib.sha256(SCRIPT_PATH.read_bytes()).hexdigest(),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise PreparationError("source manifest provenance mismatch")


def check(runner: Runner, config: Config) -> dict[str, Any]:
    if not config.root.is_dir() or not (config.root / ".git").exists():
        raise PreparationError("invalid repository root")
    assert_secure(config.external_dir, 0o700, directory=True)
    if (
        config.operation_lock.exists()
        or config.journal.exists()
        or config.pending.exists()
        or any(config.runtime_env(s).exists() for s in SERVICES)
    ):
        raise PreparationError("operation/recovery/runtime path already exists")
    sha = run_checked(runner, ["git", "rev-parse", "HEAD"], "source provenance", cwd=config.root)
    if sha != config.expected_sha:
        raise PreparationError("source SHA mismatch")
    verify_source_manifest(config, runner)
    if (
        _image_id(runner, config.migration_image) != config.migration_image_id
        or _image_id(runner, config.runtime_image) != config.runtime_image_id
    ):
        raise PreparationError("immutable image ID mismatch")
    if _container_network(runner, config.postgres_container) != config.postgres_network:
        raise PreparationError("postgres network mapping mismatch")
    expected = {
        "revision": "0009_merchant_rails",
        "role_absent": True,
        "ledger_absent": True,
        "public_create": False,
        "public_temp": True,
        "public_secdef_count": 0,
        "sequence_count": 0,
    }
    current_gate = gate(runner, config)
    if current_gate != expected:
        raise PreparationError("database gate drift")
    source, meta = source_runtime_config(runner, config)
    return {
        "source_sha": sha,
        "backup": verify_backup(config),
        "gate": current_gate,
        "source": meta,
        "source_values": source,
        "phase": Phase.CHECKED,
    }


def owner_env(config: Config, pg: Mapping[str, str], source: Mapping[str, str]) -> str:
    url = database_url(pg, pg["POSTGRES_USER"], pg["POSTGRES_PASSWORD"])
    return env_file(
        {
            "DATABASE_URL": url,
            "MIGRATION_DATABASE_URL": url,
            "DATABASE_RUNTIME_ROLE_ENABLED": "false",
            "SECRET_KEY": source["SECRET_KEY"],
            "ENCRYPTION_KEY": source["ENCRYPTION_KEY"],
            "MIGRATION_LOCK_TIMEOUT_MS": str(config.lock_timeout_ms),
            "MIGRATION_STATEMENT_TIMEOUT_MS": str(config.statement_timeout_ms),
        }
    )


def migrate(runner: Runner, config: Config, owner_file: Path) -> None:
    run_checked(
        runner,
        [
            "docker",
            "run",
            "--rm",
            "--network",
            config.postgres_network,
            "--env-file",
            str(owner_file),
            config.migration_image,
            "alembic",
            "upgrade",
            "0010_ledger_foundation",
        ],
        "owner-only migration",
        timeout=config.statement_timeout_ms // 1000 + 60,
    )


def migration_snapshot(runner: Runner, config: Config) -> dict[str, Any]:
    return psql(
        runner,
        config,
        """
SELECT json_build_object(
 $$revision$$,(SELECT version_num FROM alembic_version LIMIT 1),
 $$tables$$,COALESCE((SELECT json_agg(json_build_object($$name$$,c.relname,$$kind$$,c.relkind) ORDER BY c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$$public$$ AND c.relname=ANY(ARRAY[$$ledger_assets$$,$$ledger_accounts$$,$$ledger_transactions$$,$$ledger_entries$$])),$$[]$$::json),
 $$rows$$,json_build_object($$assets$$,(SELECT count(*) FROM ledger_assets),$$accounts$$,(SELECT count(*) FROM ledger_accounts),$$transactions$$,(SELECT count(*) FROM ledger_transactions),$$entries$$,(SELECT count(*) FROM ledger_entries)),
 $$functions$$,COALESCE((SELECT json_agg(json_build_object($$name$$,p.proname,$$return_type$$,p.prorettype::regtype::text,$$language$$,l.lanname,$$config$$,p.proconfig,$$security_definer$$,p.prosecdef) ORDER BY p.proname) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang WHERE n.nspname=$$public$$ AND p.proname=ANY(ARRAY[$$ledger_assert_final_state$$,$$ledger_guard_row$$,$$ledger_prevent_truncate$$])),$$[]$$::json),
 $$triggers$$,COALESCE((SELECT json_agg(json_build_object($$table$$,c.relname,$$name$$,t.tgname,$$function$$,p.proname,$$constraint$$,(t.tgconstraint<>0),$$deferrable$$,t.tgdeferrable,$$initially_deferred$$,t.tginitdeferred,$$enabled$$,t.tgenabled) ORDER BY c.relname,t.tgname) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid WHERE n.nspname=$$public$$ AND c.relname=ANY(ARRAY[$$ledger_assets$$,$$ledger_accounts$$,$$ledger_transactions$$,$$ledger_entries$$]) AND NOT t.tgisinternal),$$[]$$::json),
 $$constraints$$,COALESCE((SELECT json_agg(json_build_object($$table$$,c.relname,$$name$$,co.conname,$$type$$,co.contype,$$deferrable$$,co.condeferrable,$$initially_deferred$$,co.condeferred) ORDER BY c.relname,co.conname) FROM pg_constraint co JOIN pg_class c ON c.oid=co.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$$public$$ AND c.relname=ANY(ARRAY[$$ledger_assets$$,$$ledger_accounts$$,$$ledger_transactions$$,$$ledger_entries$$])),$$[]$$::json)
);
""",
    )


def migration_problems(state: Mapping[str, Any]) -> list[str]:
    expected_tables = {"ledger_assets", "ledger_accounts", "ledger_transactions", "ledger_entries"}
    problems: list[str] = []
    if state["revision"] != "0010_ledger_foundation":
        problems.append("revision")
    if {(row["name"], row["kind"]) for row in state["tables"]} != {
        (name, "r") for name in expected_tables
    }:
        problems.append("tables")
    if any(state["rows"].values()):
        problems.append("nonzero_rows")
    functions = {row["name"]: row for row in state["functions"]}
    expected_functions = {
        "ledger_assert_final_state",
        "ledger_guard_row",
        "ledger_prevent_truncate",
    }
    if set(functions) != expected_functions:
        problems.append("function_names")
    for name in expected_functions:
        row = functions.get(name, {})
        if (
            row.get("return_type") != "trigger"
            or row.get("language") != "plpgsql"
            or row.get("config") != ["search_path=pg_catalog, pg_temp"]
            or row.get("security_definer") is not False
        ):
            problems.append(f"function_properties:{name}")
    expected_triggers = {
        ("ledger_assets", "ledger_assets_guard", "ledger_guard_row", False, False, False),
        (
            "ledger_assets",
            "ledger_assets_no_truncate",
            "ledger_prevent_truncate",
            False,
            False,
            False,
        ),
        ("ledger_accounts", "ledger_accounts_guard", "ledger_guard_row", False, False, False),
        (
            "ledger_accounts",
            "ledger_accounts_no_truncate",
            "ledger_prevent_truncate",
            False,
            False,
            False,
        ),
        (
            "ledger_transactions",
            "ledger_final_header",
            "ledger_assert_final_state",
            True,
            True,
            True,
        ),
        (
            "ledger_transactions",
            "ledger_transactions_guard",
            "ledger_guard_row",
            False,
            False,
            False,
        ),
        (
            "ledger_transactions",
            "ledger_transactions_no_truncate",
            "ledger_prevent_truncate",
            False,
            False,
            False,
        ),
        ("ledger_entries", "ledger_final_entry", "ledger_assert_final_state", True, True, True),
        ("ledger_entries", "ledger_entries_guard", "ledger_guard_row", False, False, False),
        (
            "ledger_entries",
            "ledger_entries_no_truncate",
            "ledger_prevent_truncate",
            False,
            False,
            False,
        ),
    }
    actual_triggers = {
        (
            row["table"],
            row["name"],
            row["function"],
            row["constraint"],
            row["deferrable"],
            row["initially_deferred"],
        )
        for row in state["triggers"]
        if row["enabled"] == "O"
    }
    if actual_triggers != expected_triggers:
        problems.append("trigger_mappings")
    required_constraints = {
        "ck_ledger_asset_decimals",
        "uq_ledger_asset_canonical",
        "ck_ledger_account_type",
        "ck_ledger_custody_type",
        "uq_ledger_account_tenant",
        "ck_ledger_transaction_status",
        "ck_ledger_transaction_posted_at",
        "ck_ledger_source_digest",
        "ck_ledger_posting_digest",
        "uq_ledger_transaction_tenant",
        "uq_ledger_transaction_idempotency",
        "uq_ledger_transaction_source",
        "ck_ledger_entry_direction",
        "ck_ledger_entry_atomic",
    }
    if not required_constraints.issubset({row["name"] for row in state["constraints"]}):
        problems.append("named_constraints")
    deferred = {
        (row["table"], row["name"], row["type"], row["deferrable"], row["initially_deferred"])
        for row in state["constraints"]
        if row["deferrable"] or row["initially_deferred"]
    }
    if deferred != {
        ("ledger_transactions", "ledger_final_header", "t", True, True),
        ("ledger_entries", "ledger_final_entry", "t", True, True),
    }:
        problems.append("deferred_constraints")
    return problems


def verify_migration(runner: Runner, config: Config) -> dict[str, Any]:
    state = migration_snapshot(runner, config)
    state["problems"] = migration_problems(state)
    return state


def meta_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def create_role(runner: Runner, config: Config, password: str) -> None:
    template = (config.root / "scripts" / "provision_application_runtime_role.sql").read_text()
    input_sql = "\n".join(
        (
            f"\\set application_runtime_role {meta_quote(config.role_name)}",
            f"\\set migration_owner_role {meta_quote(config.migration_owner)}",
            f"\\set application_runtime_password {meta_quote(password)}",
            template,
        )
    )
    run_checked(
        runner,
        [
            "docker",
            "exec",
            "-i",
            config.postgres_container,
            "sh",
            "-c",
            'PGPASSWORD="$POSTGRES_PASSWORD" exec psql -h 127.0.0.1 -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ],
        "new runtime role provisioning",
        input_text=input_sql,
    )


def verify_role(runner: Runner, config: Config) -> dict[str, Any]:
    state = psql(
        runner,
        config,
        """
SELECT json_build_object($$attrs$$,(SELECT json_build_object($$super$$,rolsuper,$$createrole$$,rolcreaterole,$$createdb$$,rolcreatedb,$$inherit$$,rolinherit,$$replication$$,rolreplication,$$bypassrls$$,rolbypassrls,$$login$$,rolcanlogin,$$config$$,rolconfig) FROM pg_roles WHERE rolname='arbitron_runtime'),$$memberships$$,(SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname='arbitron_runtime'),$$ownership$$,(SELECT count(*) FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE r.rolname='arbitron_runtime')+(SELECT count(*) FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner WHERE r.rolname='arbitron_runtime')+(SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname='arbitron_runtime')+(SELECT count(*) FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname='arbitron_runtime'),$$db_create$$,has_database_privilege('arbitron_runtime',current_database(),'CREATE'),$$schema_create$$,has_schema_privilege('arbitron_runtime','public','CREATE'),$$truncate_count$$,(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND has_table_privilege('arbitron_runtime',c.oid,'TRUNCATE')),$$secdef_execute_count$$,(SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef AND has_function_privilege('arbitron_runtime',p.oid,'EXECUTE')),$$dml_ok_count$$,(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname<>'alembic_version' AND has_table_privilege('arbitron_runtime',c.oid,'SELECT,INSERT,UPDATE,DELETE')),$$dml_expected_count$$,(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname<>'alembic_version'));
""",
    )
    attrs = state["attrs"]
    if (
        attrs is None
        or any(
            attrs[key]
            for key in ("super", "createrole", "createdb", "inherit", "replication", "bypassrls")
        )
        or not attrs["login"]
        or attrs["config"] != ["search_path=public, pg_catalog"]
    ):
        raise PreparationError("role attribute/search path verification failed")
    if (
        any(
            state[key]
            for key in (
                "memberships",
                "ownership",
                "db_create",
                "schema_create",
                "truncate_count",
                "secdef_execute_count",
            )
        )
        or state["dml_ok_count"] != state["dml_expected_count"]
    ):
        raise PreparationError("role effective grant verification failed")
    return state


def curated(source: Mapping[str, str], pg: Mapping[str, str], password: str) -> str:
    values = dict(source)
    values["DATABASE_URL"] = database_url(pg, ROLE, password)
    values["DATABASE_RUNTIME_ROLE_ENABLED"] = "true"
    values.pop("MIGRATION_DATABASE_URL", None)
    if any(forbidden(key) for key in values):
        raise PreparationError("curated environment has owner key")
    return env_file(values)


def prepare(runner: Runner, config: Config) -> dict[str, Any]:
    checked = check(runner, config)
    lock_identity = write_exclusive(config.operation_lock, "local-review operation lock\n")
    owner_path = config.external_dir / f".migration-{secrets.token_hex(12)}.env"
    owner_identity: tuple[int, int] | None = None
    try:
        journal(
            config,
            Phase.CHECKED,
            {key: value for key, value in checked.items() if key != "source_values"},
        )
        pg = pg_env(runner, config)
        owner_identity = write_exclusive(
            owner_path, owner_env(config, pg, checked["source_values"])
        )
        migrate(runner, config, owner_path)
        migration = verify_migration(runner, config)
        journal(config, Phase.MIGRATED, {"migration": migration})
        if migration["problems"]:
            raise PreparationError(
                "post-migration verification: " + ",".join(migration["problems"])
            )
        password = secrets.token_urlsafe(32)
        write_exclusive(config.pending, f"ARBITRON_RUNTIME_PASSWORD={password}\n")
        journal(config, Phase.PENDING_PASSWORD, {"pending_path": str(config.pending)})
        create_role(runner, config, password)
        role = verify_role(runner, config)
        journal(config, Phase.ROLE_CREATED, {"role": role, "pending_path": str(config.pending)})
        content = curated(checked["source_values"], pg, password)
        for service in SERVICES:
            publish_exclusive(config.runtime_env(service), content)
        journal(
            config,
            Phase.RUNTIME_ENV_PUBLISHED,
            {"runtime_paths": [str(config.runtime_env(s)) for s in SERVICES]},
        )
        config.pending.unlink()
        _fsync_directory(config.external_dir)
        journal(
            config,
            Phase.COMPLETE,
            {"runtime_paths": [str(config.runtime_env(s)) for s in SERVICES], "role": role},
        )
        return {"phase": Phase.COMPLETE}
    except Exception as exc:
        journal(
            config,
            Phase.FAILED,
            {"failure_class": type(exc).__name__, "pending_exists": config.pending.exists()},
        )
        raise
    finally:
        unlink_owned(owner_path, owner_identity)
        unlink_owned(config.operation_lock, lock_identity)


def arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "prepare"), nargs="?", default="check")
    parser.add_argument("--confirm-prepare", action="store_true")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--migration-image", required=True)
    parser.add_argument("--migration-image-id", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--runtime-image-id", required=True)
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--postgres-network", required=True)
    parser.add_argument("--external-dir", type=Path, required=True)
    parser.add_argument("--backup-path", type=Path, required=True)
    parser.add_argument("--backup-sha256", required=True)
    parser.add_argument("--migration-owner", default="arbitron")
    parser.add_argument("--service-container", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = arguments(argv)
    mapping = tuple(item.split("=", 1) for item in args.service_container)
    if args.mode == "prepare" and not args.confirm_prepare:
        print("PREPARATION_FAILED: prepare requires --confirm-prepare", file=sys.stderr)
        return 1
    try:
        config = Config(
            args.root.resolve(),
            args.expected_sha,
            args.source_manifest.resolve(),
            args.migration_image,
            args.migration_image_id,
            args.runtime_image,
            args.runtime_image_id,
            args.postgres_container,
            args.postgres_network,
            args.external_dir.resolve(),
            args.backup_path.resolve(),
            args.backup_sha256,
            mapping,
            migration_owner=args.migration_owner,
        )
        result = check(Runner(), config) if args.mode == "check" else prepare(Runner(), config)
    except PreparationError as exc:
        print(f"PREPARATION_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"phase": result["phase"], "source_sha": args.expected_sha}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
