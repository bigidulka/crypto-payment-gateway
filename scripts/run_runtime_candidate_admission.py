#!/usr/bin/env python3
"""Run the final LOCAL-only runtime-image admission gate.

This harness creates a uniquely named disposable Compose project on an internal
network. It builds distinct runtime and migration images from the checked-out
source, proves a unique dummy secret value is absent from the runtime artifact,
then replaces every service command with a short marker command while retaining
the image ENTRYPOINT. It never starts a real worker loop or contacts an
external RPC/provider endpoint.

It is intentionally not a production procedure: it creates only disposable
Docker resources, generates only local dummy credentials, and always removes
only resources named by its own unique Compose project in ``finally``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_SERVICES = {
    "api": None,
    "poller": ["python", "-m", "src.workers.evm_log_poller"],
    "webhook": ["python", "-m", "src.workers.webhook_dispatcher"],
    "sweeper": ["python", "-m", "src.workers.unified_sweeper_runner"],
    "expirer": ["python", "-m", "src.workers.invoice_expirer"],
}
CANARY_TIMEOUT_SECONDS = 20


class AdmissionFailure(RuntimeError):
    """A local admission assertion failed without exposing process output."""


def command(
    args: list[str], *, input_text: str | None = None, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    """Run a bounded command and retain output only for local assertions."""
    return subprocess.run(
        args,
        cwd=ROOT,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode:
        raise AdmissionFailure(f"{label} failed with exit {result.returncode}")


def compose(
    project: str, compose_file: Path, *args: str, timeout: int = 20
) -> subprocess.CompletedProcess[str]:
    return command(
        ["docker", "compose", "-p", project, "-f", str(compose_file), *args],
        timeout=timeout,
    )


def runtime_environment(
    database: str, runtime_password: str, redis_password: str, marker: str
) -> str:
    return "\n".join(
        (
            "DATABASE_RUNTIME_ROLE_ENABLED=true",
            f"DATABASE_URL=postgresql+asyncpg://app_runtime:{runtime_password}@postgres:5432/{database}",
            f"REDIS_URL=redis://:{redis_password}@redis:6379/0",
            "SECRET_KEY=local-runtime-admission-secret-at-least-thirty-two-chars",
            "ENCRYPTION_KEY=hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
            f"ADMISSION_RUNTIME_ENV_CANARY={marker}",
            "MIGRATION_DATABASE_URL=",
            "",
        )
    )


def migration_environment(database: str, owner_password: str) -> str:
    owner_url = f"postgresql+asyncpg://owner:{owner_password}@postgres:5432/{database}"
    return "\n".join(
        (
            "DATABASE_RUNTIME_ROLE_ENABLED=false",
            f"DATABASE_URL={owner_url}",
            f"MIGRATION_DATABASE_URL={owner_url}",
            "SECRET_KEY=local-migration-admission-secret-at-least-thirty-two-chars",
            "ENCRYPTION_KEY=hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
            "",
        )
    )


def compose_document(
    runtime_image: str,
    migration_image: str,
    runtime_env: Path,
    migration_env: Path,
    database: str,
    owner_password: str,
    redis_password: str,
) -> dict[str, Any]:
    services: dict[str, Any] = {
        "postgres": {
            "image": "postgres:16-alpine",
            "environment": {
                "POSTGRES_USER": "owner",
                "POSTGRES_PASSWORD": owner_password,
                "POSTGRES_DB": database,
            },
            "networks": ["admission"],
        },
        "redis": {
            "image": "redis:7-alpine",
            "command": [
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
                "--requirepass",
                redis_password,
            ],
            "networks": ["admission"],
        },
        "migration": {
            "image": migration_image,
            "env_file": [str(migration_env)],
            "networks": ["admission"],
            "restart": "no",
        },
    }
    for service, default_command in RUNTIME_SERVICES.items():
        value: dict[str, Any] = {
            "image": runtime_image,
            "env_file": [str(runtime_env)],
            "networks": ["admission"],
            "restart": "no",
        }
        if default_command:
            value["command"] = default_command
        services[service] = value
    return {"services": services, "networks": {"admission": {"internal": True}}}


def write_compose(path: Path, document: dict[str, Any]) -> None:
    # JSON is valid YAML and avoids a PyYAML runtime dependency.
    path.write_text(json.dumps(document))


def wait_for_postgres(project: str, compose_file: Path) -> None:
    deadline = time.monotonic() + CANARY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = compose(
            project,
            compose_file,
            "exec",
            "-T",
            "postgres",
            "pg_isready",
            "-U",
            "owner",
            timeout=5,
        )
        if result.returncode == 0:
            return
        time.sleep(0.5)
    raise AdmissionFailure("disposable PostgreSQL did not become ready within 20 seconds")


def render_runtime_env_assertions(
    project: str, compose_file: Path, runtime_image: str, marker: str, owner_marker: str
) -> None:
    rendered = compose(project, compose_file, "config", "--format", "json")
    require_ok(rendered, "compose render")
    configuration = json.loads(rendered.stdout)
    for service in RUNTIME_SERVICES:
        rendered_service = configuration["services"][service]
        if rendered_service.get("image") != runtime_image:
            raise AdmissionFailure(f"{service} does not render the candidate runtime image")
        if rendered_service.get("entrypoint") is not None:
            raise AdmissionFailure(f"{service} overrides the runtime image entrypoint")
        if "admission" not in rendered_service.get("networks", {}):
            raise AdmissionFailure(f"{service} is not on the internal admission network")
        environment = rendered_service.get("environment", {})
        if environment.get("ADMISSION_RUNTIME_ENV_CANARY") != marker:
            raise AdmissionFailure(f"{service} lacks its curated runtime environment canary")
        serialized = json.dumps(rendered_service, sort_keys=True)
        if owner_marker in serialized:
            raise AdmissionFailure(f"{service} rendered an owner marker")
        if environment.get("MIGRATION_DATABASE_URL") not in (None, ""):
            raise AdmissionFailure(f"{service} rendered a migration owner URL")


def assert_runtime_image_excludes_secret(image: str, secret_value: str, project: str) -> None:
    inspected = command(["docker", "image", "inspect", image], timeout=CANARY_TIMEOUT_SECONDS)
    require_ok(inspected, "runtime image inspect")
    if secret_value in inspected.stdout:
        raise AdmissionFailure("dummy secret value present in runtime image configuration")
    history = command(
        [
            "docker",
            "image",
            "history",
            "--no-trunc",
            "--format",
            "{{.CreatedBy}}",
            image,
        ],
        timeout=CANARY_TIMEOUT_SECONDS,
    )
    require_ok(history, "runtime image history")
    if secret_value in history.stdout:
        raise AdmissionFailure("dummy secret value present in runtime image history")
    container_name = f"{project}-image-scan"
    created = command(
        ["docker", "create", "--name", container_name, image],
        timeout=CANARY_TIMEOUT_SECONDS,
    )
    require_ok(created, "runtime image scan container creation")
    try:
        exported = subprocess.run(
            ["docker", "export", container_name],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=CANARY_TIMEOUT_SECONDS,
        )
        if exported.returncode:
            raise AdmissionFailure("runtime image filesystem export failed")
        if secret_value.encode() in exported.stdout:
            raise AdmissionFailure("dummy secret value present in runtime image filesystem")
    finally:
        command(["docker", "rm", "-f", container_name], timeout=CANARY_TIMEOUT_SECONDS)


def run_marker(
    project: str, compose_file: Path, service: str, marker: str, extra_env: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    invocation = ["run", "--rm", "--no-deps"]
    for value in extra_env or []:
        invocation.extend(("-e", value))
    invocation.extend((service, "python", "-c", f"print({marker!r})"))
    return compose(project, compose_file, *invocation, timeout=CANARY_TIMEOUT_SECONDS)


def assert_marker(
    result: subprocess.CompletedProcess[str], marker: str, label: str, expected: bool
) -> None:
    marker_count = result.stdout.count(marker) + result.stderr.count(marker)
    if expected:
        if result.returncode != 0 or marker_count != 1:
            raise AdmissionFailure(f"{label} did not complete its valid entrypoint canary")
    elif result.returncode == 0 or marker_count:
        raise AdmissionFailure(f"{label} executed its forbidden-role/migration/drift marker")


def provision_runtime_role(
    project: str, compose_file: Path, database: str, runtime_password: str
) -> None:
    template = (ROOT / "scripts" / "provision_application_runtime_role.sql").read_text()
    result = command(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(compose_file),
            "exec",
            "-T",
            "postgres",
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "owner",
            "-d",
            database,
            "-v",
            "application_runtime_role=app_runtime",
            "-v",
            f"application_runtime_password={runtime_password}",
            "-v",
            "migration_owner_role=owner",
        ],
        input_text=template,
        timeout=CANARY_TIMEOUT_SECONDS,
    )
    require_ok(result, "disposable application role provisioning")


def sql(project: str, compose_file: Path, database: str, statement: str) -> None:
    result = compose(
        project,
        compose_file,
        "exec",
        "-T",
        "postgres",
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        "owner",
        "-d",
        database,
        "-c",
        statement,
        timeout=CANARY_TIMEOUT_SECONDS,
    )
    require_ok(result, "disposable privilege-drift setup")


def build_images(runtime_image: str, migration_image: str) -> None:
    require_ok(
        command(
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
            timeout=900,
        ),
        "runtime image build",
    )
    require_ok(
        command(
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
            timeout=900,
        ),
        "migration image build",
    )
    runtime_id = command(["docker", "image", "inspect", runtime_image, "--format", "{{.Id}}"])
    migration_id = command(["docker", "image", "inspect", migration_image, "--format", "{{.Id}}"])
    require_ok(runtime_id, "runtime image ID inspection")
    require_ok(migration_id, "migration image ID inspection")
    if runtime_id.stdout.strip() == migration_id.stdout.strip():
        raise AdmissionFailure("runtime and migration images are not distinct")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="retain only this run's uniquely tagged local images",
    )
    args = parser.parse_args()
    token = uuid.uuid4().hex[:12]
    project = f"runtimeadmit{token}"
    runtime_image = f"arbitron-runtime-admission-{token}:local"
    migration_image = f"arbitron-migration-admission-{token}:local"
    database = f"test_runtime_admission_{token}"
    owner_password = f"ADMISSION_OWNER_URL_{token}_{secrets.token_urlsafe(24)}"
    runtime_password = secrets.token_urlsafe(24)
    redis_password = secrets.token_urlsafe(24)
    runtime_marker = f"ADMISSION_RUNTIME_ENV_CANARY_{token}"
    secret_value = f"ADMISSION_DUMMY_SECRET_VALUE_{token}_{secrets.token_hex(16)}"
    secret_dir = ROOT / "secrets"
    secret_file = secret_dir / f"admission-{token}.env"
    workspace = Path(tempfile.mkdtemp(prefix=f"{project}-"))
    compose_file = workspace / "compose.json"
    runtime_env = workspace / "runtime.env"
    migration_env = workspace / "migration.env"
    stage = "initialization"
    try:
        secret_dir.mkdir(exist_ok=True)
        secret_file.write_text(f"OWNER_SECRET={secret_value}\n")
        runtime_env.write_text(
            runtime_environment(database, runtime_password, redis_password, runtime_marker)
        )
        migration_env.write_text(migration_environment(database, owner_password))
        stage = "image build"
        build_images(runtime_image, migration_image)
        stage = "runtime image secret-value scan"
        assert_runtime_image_excludes_secret(runtime_image, secret_value, project)
        assert_runtime_image_excludes_secret(runtime_image, owner_password, project)
        stage = "compose render"
        write_compose(
            compose_file,
            compose_document(
                runtime_image,
                migration_image,
                runtime_env,
                migration_env,
                database,
                owner_password,
                redis_password,
            ),
        )
        render_runtime_env_assertions(
            project, compose_file, runtime_image, runtime_marker, owner_password
        )
        stage = "internal dependencies"
        require_ok(
            compose(project, compose_file, "up", "-d", "postgres", "redis"),
            "disposable internal dependency startup",
        )
        wait_for_postgres(project, compose_file)
        stage = "migration-owner image"
        require_ok(
            compose(project, compose_file, "run", "--rm", "migration"),
            "disposable migration-owner image",
        )
        stage = "non-owner role provisioning"
        provision_runtime_role(project, compose_file, database, runtime_password)
        stage = "five-service canaries"
        for service in RUNTIME_SERVICES:
            marker = f"ADMISSION_CANARY_{service.upper()}"
            assert_marker(
                run_marker(project, compose_file, service, marker),
                marker,
                service,
                expected=True,
            )
            assert_marker(
                run_marker(
                    project,
                    compose_file,
                    service,
                    marker,
                    [
                        f"DATABASE_URL=postgresql+asyncpg://owner:{owner_password}@postgres:5432/{database}"
                    ],
                ),
                marker,
                f"{service} forbidden owner role",
                expected=False,
            )
            assert_marker(
                run_marker(
                    project,
                    compose_file,
                    service,
                    marker,
                    [
                        f"MIGRATION_DATABASE_URL=postgresql+asyncpg://owner:{owner_password}@postgres:5432/{database}"
                    ],
                ),
                marker,
                f"{service} forbidden migration environment",
                expected=False,
            )
        stage = "truncate drift rejection"
        sql(project, compose_file, database, "GRANT TRUNCATE ON public.webhooks TO app_runtime")
        drift_marker = "ADMISSION_CANARY_DRIFT_TRUNCATE"
        assert_marker(
            run_marker(project, compose_file, "api", drift_marker),
            drift_marker,
            "protected-table truncate drift",
            expected=False,
        )
        sql(project, compose_file, database, "REVOKE TRUNCATE ON public.webhooks FROM app_runtime")
        stage = "security-definer drift rejection"
        sql(
            project,
            compose_file,
            database,
            "CREATE FUNCTION public.runtime_admission_definer() RETURNS integer "
            "LANGUAGE sql SECURITY DEFINER AS $$ SELECT 1 $$; "
            "GRANT EXECUTE ON FUNCTION public.runtime_admission_definer() TO PUBLIC",
        )
        drift_marker = "ADMISSION_CANARY_DRIFT_SECURITY_DEFINER"
        assert_marker(
            run_marker(project, compose_file, "api", drift_marker),
            drift_marker,
            "PUBLIC security-definer drift",
            expected=False,
        )
        print("RUNTIME_CANDIDATE_IMAGE_CONTEXT_SECRET_VALUE_EXIT=0")
        print("RUNTIME_CANDIDATE_COMPOSE_FIVE_SERVICE_RENDER_EXIT=0")
        print("RUNTIME_CANDIDATE_FIVE_SERVICE_ENTRYPOINT_CANARIES_EXIT=0")
        print("RUNTIME_CANDIDATE_OWNER_AND_MIGRATION_REJECTS_EXIT=0")
        print("RUNTIME_CANDIDATE_DYNAMIC_PRIVILEGE_DRIFT_REJECTS_EXIT=0")
        return 0
    except (AdmissionFailure, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        print(
            "RUNTIME_CANDIDATE_ADMISSION_EXIT=1 "
            f"({type(error).__name__}; stage={stage}; reason={error})",
            file=sys.stderr,
        )
        return 1
    finally:
        compose(
            project,
            compose_file,
            "down",
            "--volumes",
            "--remove-orphans",
            timeout=CANARY_TIMEOUT_SECONDS,
        )
        if not args.keep_images:
            command(
                ["docker", "image", "rm", "-f", runtime_image, migration_image],
                timeout=CANARY_TIMEOUT_SECONDS,
            )
        secret_file.unlink(missing_ok=True)
        if secret_dir.exists() and not any(secret_dir.iterdir()):
            secret_dir.rmdir()
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
