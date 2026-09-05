#!/usr/bin/env python3
"""Validate an explicit limited-role Compose launch plan without applying it.

This helper never calls ``docker compose up``. It renders only the requested
approved service, validates its protected per-service raw runtime env file and
immutable image identity, and prints values-free JSON suitable for an approved
operator workflow. Apply remains a separately authorized explicit Compose
command documented in ``docs/runtime-deployment.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

ALLOWED_SERVICES = (
    "api",
    "worker-poller",
    "worker-webhook",
    "worker-sweeper",
    "worker-expirer",
)
FORBIDDEN_PREFIXES = ("POSTGRES_", "OWNER_")
FORBIDDEN_NAMES = {"PGPASSWORD", "DATABASE_OWNER_URL", "OWNER_DATABASE_URL"}

EXPECTED_COMMANDS = {
    "worker-poller": ["python", "-m", "src.workers.evm_log_poller"],
    "worker-webhook": ["python", "-m", "src.workers.webhook_dispatcher"],
    "worker-sweeper": ["python", "-m", "src.workers.unified_sweeper_runner"],
    "worker-expirer": ["python", "-m", "src.workers.invoice_expirer"],
}
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT = "arbitron-payment"


class PlanError(RuntimeError):
    """A values-free launch-plan validation failure."""


@dataclass(frozen=True)
class Plan:
    service: str
    image_ref: str
    image_id: str
    runtime_env: Path
    image_override: Path
    project: str
    base_compose: Path
    runtime_override: Path


class Runner:
    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            env=dict(env),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def _secure_parent(path: Path) -> None:
    parent = path.parent
    if not parent.is_absolute():
        raise PlanError("protected parent must be absolute")
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        current /= part
        item = current.lstat()
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            raise PlanError("protected parent symlink or non-directory")
    item = parent.lstat()
    if (
        stat.S_IMODE(item.st_mode) != 0o700
        or item.st_uid != os.getuid()
        or item.st_gid != os.getgid()
    ):
        raise PlanError("protected parent owner or mode mismatch")


def _secure_regular(path: Path, mode: int) -> None:
    if not path.is_absolute():
        raise PlanError("protected path must be absolute")
    _secure_parent(path)
    try:
        item = path.lstat()
    except FileNotFoundError as exc:
        raise PlanError("protected path missing") from exc
    if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode) or item.st_nlink != 1:
        raise PlanError("protected path is not a private regular file")
    if (
        stat.S_IMODE(item.st_mode) != mode
        or item.st_uid != os.getuid()
        or item.st_gid != os.getgid()
    ):
        raise PlanError("protected path owner or mode mismatch")


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or "\x00" in line
            or "\r" in line
            or key in values
        ):
            raise PlanError("runtime env syntax invalid")
        values[key] = value
    return values


def forbidden_env_names(environment: Mapping[str, object]) -> list[str]:
    names: list[str] = []
    for key, value in environment.items():
        if key == "MIGRATION_DATABASE_URL" and value in (None, ""):
            continue
        if (
            key in FORBIDDEN_NAMES
            or key.startswith(FORBIDDEN_PREFIXES)
            or key.startswith("MIGRATION_")
            or key.endswith("_OWNER_URL")
        ):
            names.append(key)
    return sorted(names)


def _compose_value(value: str) -> str:
    # Compose JSON representation of raw env_file literals doubles dollar signs.
    return value.replace("$", "$$")


def _matches_rendered_raw(value: str, rendered: object) -> bool:
    return rendered in (value, _compose_value(value))


def validate(plan: Plan, runner: Runner) -> dict[str, object]:
    if plan.service not in ALLOWED_SERVICES or plan.project != PROJECT:
        raise PlanError("service or project is not approved")
    if not IMAGE_ID_RE.fullmatch(plan.image_id):
        raise PlanError("image ID is not immutable sha256")
    _secure_regular(plan.runtime_env, 0o600)
    _secure_regular(plan.image_override, 0o600)
    if not plan.base_compose.is_absolute() or not plan.runtime_override.is_absolute():
        raise PlanError("compose paths must be absolute")
    for path in (plan.base_compose, plan.runtime_override):
        if not path.is_file() or path.is_symlink():
            raise PlanError("compose path missing or symlink")

    image = runner.run(
        ["docker", "image", "inspect", plan.image_ref, "--format", "{{.Id}}"],
        env={"PATH": os.environ["PATH"]},
        cwd=plan.base_compose.parent,
    )
    if image.returncode or image.stdout.strip() != plan.image_id:
        raise PlanError("immutable image ID mismatch")

    compose_env = {"PATH": os.environ["PATH"], "RUNTIME_ENV_FILE": str(plan.runtime_env)}
    rendered = runner.run(
        [
            "docker",
            "compose",
            "-p",
            plan.project,
            "-f",
            str(plan.base_compose),
            "-f",
            str(plan.runtime_override),
            "-f",
            str(plan.image_override),
            "config",
            "--format",
            "json",
        ],
        env=compose_env,
        cwd=plan.base_compose.parent,
    )
    if rendered.returncode:
        raise PlanError("compose render failed")
    try:
        target = json.loads(rendered.stdout)["services"][plan.service]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise PlanError("compose render missing target service") from exc

    environment = target.get("environment")
    if not isinstance(environment, dict):
        raise PlanError("target environment missing")
    runtime_values = _env_values(plan.runtime_env)
    for key, value in runtime_values.items():
        if not _matches_rendered_raw(value, environment.get(key)):
            raise PlanError("target runtime env differs from protected file")
    if environment.get("DATABASE_RUNTIME_ROLE_ENABLED") != "true":
        raise PlanError("target runtime role flag is not true")
    if not environment.get("DATABASE_URL", "").startswith("postgresql+asyncpg://arbitron_runtime:"):
        raise PlanError("target database user is not arbitron_runtime")
    if environment.get("MIGRATION_DATABASE_URL") not in (None, ""):
        raise PlanError("target migration URL is nonempty")
    if forbidden_env_names(environment):
        raise PlanError("target forbidden owner environment name")
    if target.get("entrypoint") is not None:
        raise PlanError("target overrides runtime image entrypoint")
    command = target.get("command")
    if plan.service == "api":
        if command not in (None, []) or target.get("healthcheck", {}).get("test") != [
            "CMD",
            "curl",
            "-f",
            "http://localhost:8000/ready",
        ]:
            raise PlanError("api command or readiness healthcheck mismatch")
    elif command != EXPECTED_COMMANDS[plan.service]:
        raise PlanError("worker command mismatch")
    if target.get("image") != plan.image_ref:
        raise PlanError("target image reference differs from explicit image")

    return {
        "mode": "plan-only",
        "project": plan.project,
        "service": plan.service,
        "image_id": plan.image_id,
        "runtime_env_path": str(plan.runtime_env),
        "image_override_path": str(plan.image_override),
    }


def parse_args(argv: Sequence[str] | None = None) -> Plan:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=ALLOWED_SERVICES)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--runtime-env", type=Path, required=True)
    parser.add_argument("--image-override", type=Path, required=True)
    parser.add_argument("--project", default="arbitron-payment")
    parser.add_argument("--base-compose", type=Path, required=True)
    parser.add_argument("--runtime-override", type=Path, required=True)
    args = parser.parse_args(argv)
    return Plan(
        args.service,
        args.image_ref,
        args.image_id,
        args.runtime_env,
        args.image_override,
        args.project,
        args.base_compose,
        args.runtime_override,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        result = validate(parse_args(argv), Runner())
    except PlanError as exc:
        print(f"RUNTIME_COMPOSE_PLAN_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
