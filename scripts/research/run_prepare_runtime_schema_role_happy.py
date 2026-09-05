#!/usr/bin/env python3
"""One REAL LOCAL happy run for prepare_runtime_schema_role.py.

Creates only uniquely named internal Docker resources. Five inert source
containers run `/usr/bin/env` so their effective env values can be inspected;
no application/worker module or financial action runs.  A special-character
PostgreSQL owner password exercises actual Alembic ConfigParser URL handling.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_runtime_schema_role.py"
SERVICES = ("api", "worker-poller", "worker-webhook", "worker-sweeper", "worker-expirer")

PREPARE_SPEC = importlib.util.spec_from_file_location("schema_prepare_harness", SCRIPT)
assert PREPARE_SPEC and PREPARE_SPEC.loader
prepare_module = importlib.util.module_from_spec(PREPARE_SPEC)
sys.modules[PREPARE_SPEC.name] = prepare_module
PREPARE_SPEC.loader.exec_module(prepare_module)


def run(
    argv: list[str], *, input_text: str | None = None, timeout: int = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=ROOT,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def ok(argv: list[str], label: str, *, input_text: str | None = None, timeout: int = 180) -> str:
    result = run(argv, input_text=input_text, timeout=timeout)
    if result.returncode:
        sqlstate = ""
        for token in result.stderr.split():
            if len(token) == 5 and token.isalnum() and token.upper() == token:
                sqlstate = token
                break
        suffix = f" sqlstate={sqlstate}" if sqlstate else ""
        raise RuntimeError(f"{label}: exit={result.returncode}{suffix}")
    return result.stdout.strip()


def main(inject_publish_failure: bool = False) -> int:
    token = uuid.uuid4().hex[:12]
    network = f"runtime-schema-role-{token}"
    postgres = f"runtime-schema-role-postgres-{token}"
    owner_password = "Own@:/%+$'\\ю" + token
    runtime_image = f"arbitron-schema-role-runtime-{token}:local"
    migration_image = f"arbitron-schema-role-migration-{token}:local"
    external = Path(tempfile.mkdtemp(prefix=f"runtime-schema-role-{token}-"))
    os.chmod(external, 0o700)
    backups = external / "backups"
    diagnostic = ROOT / ".ralph" / f"schema-role-happy-diagnostic-{token}.json"
    source_ids: dict[str, str] = {}
    backups.mkdir(mode=0o700)
    backup = backups / "fixture.dump"
    backup.write_bytes(b"local-only-fixture")
    os.chmod(backup, 0o600)
    source_manifest = external / "source.json"
    created_sources: list[str] = []
    stage = "image build"
    try:
        ok(
            [
                "docker",
                "build",
                "--network=host",
                "--target",
                "production",
                "-t",
                runtime_image,
                ".",
            ],
            "runtime build",
            timeout=900,
        )
        ok(
            [
                "docker",
                "build",
                "--network=host",
                "--target",
                "migration",
                "-t",
                migration_image,
                ".",
            ],
            "migration build",
            timeout=900,
        )
        runtime_id = ok(
            ["docker", "image", "inspect", runtime_image, "--format", "{{.Id}}"], "runtime ID"
        )
        migration_id = ok(
            ["docker", "image", "inspect", migration_image, "--format", "{{.Id}}"], "migration ID"
        )
        source_manifest.write_text(
            json.dumps(
                {
                    "source_sha": ok(["git", "rev-parse", "HEAD"], "source SHA"),
                    "migration_image_id": migration_id,
                    "runtime_image_id": runtime_id,
                    "worktree_patch_sha256": hashlib.sha256(
                        ok(["git", "diff", "--binary", "HEAD"], "worktree patch").encode()
                    ).hexdigest(),
                    "script_sha256": hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                }
            )
        )
        os.chmod(source_manifest, 0o600)
        stage = "internal dependencies"
        ok(["docker", "network", "create", "--internal", network], "network create")
        ok(
            [
                "docker",
                "run",
                "-d",
                "--name",
                postgres,
                "--network",
                network,
                "--network-alias",
                "postgres",
                "-e",
                "POSTGRES_USER=owner",
                "-e",
                f"POSTGRES_PASSWORD={owner_password}",
                "-e",
                "POSTGRES_DB=test_schema_role",
                "postgres:16-alpine",
            ],
            "postgres create",
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (
                run(
                    [
                        "docker",
                        "exec",
                        postgres,
                        "pg_isready",
                        "-U",
                        "owner",
                        "-d",
                        "test_schema_role",
                    ],
                    timeout=10,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError("postgres readiness")
        ok(
            [
                "docker",
                "exec",
                "-i",
                postgres,
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE ROLE arbitron NOLOGIN"',
            ],
            "migration owner fixture",
        )
        bootstrap_env = external / "bootstrap-migration.env"
        owner_url = "postgresql+asyncpg://owner:{}@postgres:5432/test_schema_role".format(
            quote(owner_password, safe="")
        )
        fd = os.open(bootstrap_env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(
                fd,
                "\n".join(
                    (
                        f"DATABASE_URL={owner_url}",
                        f"MIGRATION_DATABASE_URL={owner_url}",
                        "DATABASE_RUNTIME_ROLE_ENABLED=false",
                        "SECRET_KEY=schema-role-local-secret-at-least-thirty-two-chars",
                        "ENCRYPTION_KEY=hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
                        "",
                    )
                ).encode(),
            )
            os.fsync(fd)
        finally:
            os.close(fd)
        if stat.S_IMODE(bootstrap_env.stat().st_mode) != 0o600:
            raise RuntimeError("bootstrap env mode")
        ok(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env-file",
                str(bootstrap_env),
                migration_image,
                "alembic",
                "upgrade",
                "0009_merchant_rails",
            ],
            "bootstrap 0009 migration",
            timeout=180,
        )
        bootstrap_env.unlink()
        stage = "inert source containers"
        old_env = {
            "SECRET_KEY": "schema-role-local-secret-at-least-thirty-two-chars",
            "ENCRYPTION_KEY": "hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
            "REDIS_URL": "redis://:dollar$literal@redis:6379/0",
            "HD_WALLET_SEED": "literal-${seed} # quote' space unicode-ю\\path",
            "TZ": "UTC",
        }
        for service in SERVICES:
            name = f"runtime-schema-role-source-{service}-{token}"
            argv = ["docker", "run", "-d", "--name", name, "--network", network]
            argv.extend(
                item
                for pair in (("-e", f"{key}={value}") for key, value in old_env.items())
                for item in pair
            )
            argv.extend(
                ["--entrypoint", "python", runtime_image, "-c", "import time; time.sleep(600)"]
            )
            ok(argv, f"source container {service}")
            created_sources.append(name)
            source_ids[service] = ok(
                ["docker", "inspect", "--format", "{{.Id}}", name], "source identity before prepare"
            )
        stage = "mutable preparation"
        sha = ok(["git", "rev-parse", "HEAD"], "source SHA")
        service_args = [
            item
            for service, container in zip(SERVICES, created_sources, strict=True)
            for item in ("--service-container", f"{service}={container}")
        ]
        args = [
            sys.executable,
            str(SCRIPT),
            "prepare",
            "--confirm-prepare",
            "--root",
            str(ROOT),
            "--expected-sha",
            sha,
            "--source-manifest",
            str(source_manifest),
            "--migration-image",
            migration_image,
            "--migration-image-id",
            migration_id,
            "--runtime-image",
            runtime_image,
            "--runtime-image-id",
            runtime_id,
            "--postgres-container",
            postgres,
            "--postgres-network",
            network,
            "--external-dir",
            str(external),
            "--backup-path",
            str(backup),
            "--backup-sha256",
            hashlib.sha256(backup.read_bytes()).hexdigest(),
            "--migration-owner",
            "owner",
            *service_args,
        ]
        if inject_publish_failure:
            config = prepare_module.Config(
                ROOT,
                sha,
                source_manifest,
                migration_image,
                migration_id,
                runtime_image,
                runtime_id,
                postgres,
                network,
                external,
                backup,
                hashlib.sha256(backup.read_bytes()).hexdigest(),
                tuple(zip(SERVICES, created_sources, strict=True)),
                migration_owner="owner",
            )
            original_publish = prepare_module.publish_exclusive
            published: list[str] = []

            def injected_publish(path, content):
                if published:
                    raise OSError("injected publish fault")
                original_publish(path, content)
                published.append(path.name)

            prepare_module.publish_exclusive = injected_publish
            try:
                try:
                    prepare_module.prepare(prepare_module.Runner(), config)
                except OSError:
                    pass
                else:
                    raise RuntimeError("injected publish did not fail")
            finally:
                prepare_module.publish_exclusive = original_publish
            manifest_state = json.loads(
                (external / "runtime-schema-role-preparation.manifest.json").read_text()
            )
            pending = external / "arbitron_runtime.pending"
            if (
                manifest_state.get("phase") != "failed"
                or manifest_state.get("last_completed_phase") != "role_created"
                or not pending.is_file()
                or stat.S_IMODE(pending.stat().st_mode) != 0o600
                or not (external / "runtime-api.env").is_file()
                or len(list(external.glob("runtime-*.env"))) != 1
                or list(external.glob(".migration-*.env"))
                or (external / ".runtime-schema-role.lock").exists()
            ):
                raise RuntimeError("publish failure recovery state")
            auth_code = """import asyncio
from sqlalchemy import text
from src.db.session import get_engine

async def main():
    async with get_engine().connect() as connection:
        print((await connection.execute(text("SELECT current_user"))).scalar())
    await get_engine().dispose()

asyncio.run(main())
"""
            compile(auth_code, "<fault-auth>", "exec")
            auth = ok(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    network,
                    "--env-file",
                    str(external / "runtime-api.env"),
                    runtime_image,
                    "python",
                    "-c",
                    auth_code,
                ],
                "publish failure role auth",
            )
            if auth != "arbitron_runtime":
                raise RuntimeError("publish failure role auth")
            if any(
                source_ids[service]
                != ok(
                    ["docker", "inspect", "--format", "{{.Id}}", container],
                    "source identity after injected failure",
                )
                for service, container in zip(SERVICES, created_sources, strict=True)
            ):
                raise RuntimeError("source changed after injected failure")
            try:
                prepare_module.check(prepare_module.Runner(), config)
            except prepare_module.PreparationError:
                pass
            else:
                raise RuntimeError("recovery check unexpectedly passed")
            print("SCHEMA_ROLE_REAL_PUBLISH_FAILURE_EXIT=0")
            return 0

        result = run(args, timeout=300)
        if result.returncode or "complete" not in result.stdout:
            safe_lines = [
                line
                for line in (result.stdout + "\n" + result.stderr).splitlines()
                if line.startswith("PREPARATION_FAILED:")
            ]
            snapshot = ok(
                [
                    "docker",
                    "exec",
                    "-i",
                    postgres,
                    "sh",
                    "-c",
                    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -q -t -A -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT json_build_object(\'revision\',(SELECT version_num FROM alembic_version LIMIT 1),\'role_exists\',EXISTS(SELECT 1 FROM pg_roles WHERE rolname=\'arbitron_runtime\'))"',
                ],
                "sanitized failure snapshot",
            )
            manifest = external / "runtime-schema-role-preparation.manifest.json"
            manifest_state = json.loads(manifest.read_text()) if manifest.exists() else {}
            diagnostic.write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "exit_code": result.returncode,
                        "preparation_messages": safe_lines,
                        "manifest_phase": manifest_state.get("phase"),
                        "last_completed_phase": manifest_state.get("last_completed_phase"),
                        "failure_class": manifest_state.get("failure_class"),
                        "db_snapshot": json.loads(snapshot),
                        "pending_exists": (external / "arbitron_runtime.pending").exists(),
                        "runtime_file_count": len(list(external.glob("runtime-*.env"))),
                        "source_ids_unchanged": {
                            service: source_ids[service]
                            == ok(
                                ["docker", "inspect", "--format", "{{.Id}}", container],
                                "source identity after failure",
                            )
                            for service, container in zip(SERVICES, created_sources, strict=True)
                        },
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            os.chmod(diagnostic, 0o600)
            raise RuntimeError("prepare happy")
        runtime_env = external / "runtime-api.env"
        stage = "runtime canary"
        canary_code = """import asyncio
import os
from sqlalchemy import text
from src.db.session import get_engine

EXPECTED = __EXPECTED__

def safe_error(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        state = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
        if state:
            return type(exc).__name__, str(state)
        exc = exc.__cause__ or exc.__context__
    return type(exc).__name__, "none"

async def main():
    try:
        async with get_engine().connect() as connection:
            row = (await connection.execute(text("SELECT current_user, current_database(), current_setting('search_path'), (SELECT count(*) FROM public.ledger_transactions), (SELECT count(*) FROM public.ledger_entries)"))).one()
        print(f"CANARY_OK|{row[0]}|{row[1]}|{row[2] == 'public, pg_catalog'}|{row[3] == 0}|{row[4] == 0}|{os.environ['HD_WALLET_SEED'] == EXPECTED}")
    except BaseException as exc:
        name, state = safe_error(exc)
        print(f"CANARY_FAILURE|{name}|{state}")
    finally:
        await get_engine().dispose()

asyncio.run(main())
""".replace("__EXPECTED__", json.dumps(old_env["HD_WALLET_SEED"]))
        compile(canary_code, "<canary>", "exec")
        canary_result = run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                network,
                "--env-file",
                str(runtime_env),
                runtime_image,
                "python",
                "-c",
                canary_code,
            ],
            timeout=60,
        )
        marker = next(
            (line for line in canary_result.stdout.splitlines() if line.startswith("CANARY_")),
            "CANARY_FAILURE|entrypoint_or_process|none",
        )
        if marker != "CANARY_OK|arbitron_runtime|test_schema_role|True|True|True|True":
            diagnostic.write_text(
                json.dumps(
                    {
                        "stage": stage,
                        "exit_code": canary_result.returncode,
                        "canary_marker": marker,
                        "manifest_phase": json.loads(
                            (external / "runtime-schema-role-preparation.manifest.json").read_text()
                        ).get("phase"),
                        "last_completed_phase": json.loads(
                            (external / "runtime-schema-role-preparation.manifest.json").read_text()
                        ).get("last_completed_phase"),
                        "source_ids_unchanged": {
                            service: source_ids[service]
                            == ok(
                                ["docker", "inspect", "--format", "{{.Id}}", container],
                                "source identity after canary",
                            )
                            for service, container in zip(SERVICES, created_sources, strict=True)
                        },
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            os.chmod(diagnostic, 0o600)
            raise RuntimeError("runtime canary marker")
        stage = "ledger verification"
        verify = ok(
            [
                "docker",
                "exec",
                "-i",
                postgres,
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -q -t -A -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT (SELECT count(*) FROM ledger_assets)+(SELECT count(*) FROM ledger_accounts)+(SELECT count(*) FROM ledger_transactions)+(SELECT count(*) FROM ledger_entries)"',
            ],
            "zero ledger rows",
        )
        if verify != "0":
            raise RuntimeError("ledger rows")
        stage = "runtime file verification"
        for service, container in zip(SERVICES, created_sources, strict=True):
            before = ok(["docker", "inspect", "--format", "{{.Id}}", container], "source identity")
            if not before:
                raise RuntimeError(f"source changed {service}")
            path = external / f"runtime-{service}.env"
            if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise RuntimeError("runtime env mode")
        if (external / "arbitron_runtime.pending").exists() or list(
            external.glob(".migration-*.env")
        ):
            raise RuntimeError("ephemeral cleanup")
        print("SCHEMA_ROLE_REAL_HAPPY_EXIT=0")
        return 0
    except Exception as exc:
        print(
            f"SCHEMA_ROLE_REAL_HAPPY_EXIT=1 ({type(exc).__name__}; stage={stage}; reason={exc})",
            file=sys.stderr,
        )
        return 1
    finally:
        for name in created_sources:
            run(["docker", "rm", "-f", name])
        run(["docker", "rm", "-f", postgres])
        run(["docker", "image", "rm", "-f", runtime_image, migration_image])
        shutil.rmtree(external, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inject-publish-failure", action="store_true")
    raise SystemExit(main(parser.parse_args().inject_publish_failure))
