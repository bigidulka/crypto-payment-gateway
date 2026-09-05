"""Dry-run planner safety tests; no Docker daemon or production state."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "runtime_compose.py"
spec = importlib.util.spec_from_file_location("runtime_compose", SCRIPT)
assert spec and spec.loader
planner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = planner
spec.loader.exec_module(planner)

IMAGE_ID = "sha256:" + "a" * 64
IMAGE_REF = "gateway:test"
COMMANDS = {
    "worker-poller": ["python", "-m", "src.workers.evm_log_poller"],
    "worker-webhook": ["python", "-m", "src.workers.webhook_dispatcher"],
    "worker-sweeper": ["python", "-m", "src.workers.unified_sweeper_runner"],
    "worker-expirer": ["python", "-m", "src.workers.invoice_expirer"],
}


class FakeRunner:
    def __init__(self, service, environment, command=None):
        self.service = service
        self.environment = environment
        self.command = command
        self.calls = []

    def run(self, argv, *, env, cwd):
        self.calls.append(argv)
        if argv[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, IMAGE_ID + "\n", "")
        target = {
            "image": IMAGE_REF,
            "entrypoint": None,
            "environment": self.environment,
        }
        if self.service == "api":
            target["command"] = None
            target["healthcheck"] = {"test": ["CMD", "curl", "-f", "http://localhost:8000/ready"]}
        else:
            target["command"] = self.command
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"services": {self.service: target}}), ""
        )


def protected(path, content, mode=0o600):
    path.write_text(content)
    os.chmod(path, mode)


def plan(tmp_path, service="worker-webhook"):
    os.chmod(tmp_path, 0o700)
    runtime_env = tmp_path / "runtime.env"
    override = tmp_path / "image.yml"
    protected(
        runtime_env,
        "\n".join(
            (
                "DATABASE_RUNTIME_ROLE_ENABLED=true",
                "DATABASE_URL=postgresql+asyncpg://arbitron_runtime:dummy@postgres:5432/db",
                "REDIS_URL=redis://:dummy@redis:6379/0",
                "SECRET_KEY=dummy-secret-at-least-thirty-two-chars",
                "ENCRYPTION_KEY=hqFLu+kFxLrHJ0GvR5eAWT0DcxSr5FqxJcXmV9GZqMA=",
                "RAW=$foo/${bar}",
                "",
            )
        ),
    )
    protected(override, "services: {}\n")
    base = tmp_path / "base.yml"
    runtime = tmp_path / "runtime.yml"
    base.write_text("services: {}\n")
    runtime.write_text("services: {}\n")
    return planner.Plan(
        service, IMAGE_REF, IMAGE_ID, runtime_env, override, "arbitron-payment", base, runtime
    )


def rendered_environment(planned):
    values = planner._env_values(planned.runtime_env)
    environment = {key: planner._compose_value(value) for key, value in values.items()}
    environment["MIGRATION_DATABASE_URL"] = ""
    return environment


def test_allowlist_is_exact_five_runtime_services():
    assert planner.ALLOWED_SERVICES == (
        "api",
        "worker-poller",
        "worker-webhook",
        "worker-sweeper",
        "worker-expirer",
    )


@pytest.mark.parametrize("service", planner.ALLOWED_SERVICES)
def test_validate_accepts_exact_runtime_plan_for_each_service(tmp_path, service):
    planned = plan(tmp_path, service)
    command = None if service == "api" else COMMANDS[service]
    result = planner.validate(planned, FakeRunner(service, rendered_environment(planned), command))
    assert result["service"] == service
    assert result["image_id"] == IMAGE_ID


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (
            lambda plan, env, command: env.update({"DATABASE_RUNTIME_ROLE_ENABLED": "false"}),
            "target runtime env differs",
        ),
        (
            lambda plan, env, command: env.update(
                {"DATABASE_URL": "postgresql+asyncpg://owner:dummy@postgres/db"}
            ),
            "target runtime env differs",
        ),
        (
            lambda plan, env, command: env.update({"MIGRATION_DATABASE_URL": "owner-url"}),
            "migration URL",
        ),
        (lambda plan, env, command: env.update({"OWNER_URL": ""}), "forbidden owner"),
        (lambda plan, env, command: command.__setitem__(2, "wrong.module"), "worker command"),
    ],
)
def test_validate_rejects_strict_runtime_invariants(tmp_path, mutate, error):
    planned = plan(tmp_path, "worker-webhook")
    environment = rendered_environment(planned)
    command = list(COMMANDS["worker-webhook"])
    mutate(planned, environment, command)
    with pytest.raises(planner.PlanError, match=error):
        planner.validate(planned, FakeRunner(planned.service, environment, command))


def test_only_empty_migration_database_url_is_allowed():
    assert planner.forbidden_env_names({"MIGRATION_DATABASE_URL": ""}) == []
    assert planner.forbidden_env_names({"MIGRATION_DATABASE_URL": "owner-url"}) == [
        "MIGRATION_DATABASE_URL"
    ]
    assert planner.forbidden_env_names(
        {
            "MIGRATION_OTHER": "",
            "POSTGRES_PASSWORD": "",
            "OWNER_URL": "",
            "PGPASSWORD": "",
            "EXTRA_OWNER_URL": "",
        }
    ) == ["EXTRA_OWNER_URL", "MIGRATION_OTHER", "OWNER_URL", "PGPASSWORD", "POSTGRES_PASSWORD"]


def test_validate_rejects_nonimmutable_image_id_and_unsafe_parent(tmp_path):
    planned = plan(tmp_path)
    weak = planner.Plan(**{**planned.__dict__, "image_id": "latest"})
    with pytest.raises(planner.PlanError, match="immutable sha256"):
        planner.validate(
            weak,
            FakeRunner(planned.service, rendered_environment(planned), COMMANDS[planned.service]),
        )
    os.chmod(tmp_path, 0o755)
    with pytest.raises(planner.PlanError, match="parent owner or mode"):
        planner.validate(
            planned,
            FakeRunner(planned.service, rendered_environment(planned), COMMANDS[planned.service]),
        )


def test_env_parser_rejects_duplicate_invalid_and_nul_keys(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "runtime.env"
    protected(path, "GOOD=x\nGOOD=y\n")
    with pytest.raises(planner.PlanError, match="syntax"):
        planner._env_values(path)
    protected(path, "bad-key=x\n")
    with pytest.raises(planner.PlanError, match="syntax"):
        planner._env_values(path)
    protected(path, "GOOD=x\x00\n")
    with pytest.raises(planner.PlanError, match="syntax"):
        planner._env_values(path)


def test_raw_config_comparison_accounts_for_compose_json_dollar_escaping():
    assert planner._compose_value("$foo/${bar}") == "$$foo/$${bar}"
    assert planner._matches_rendered_raw("$foo", "$$foo")
    assert planner._matches_rendered_raw("$foo", "$foo")


def test_planner_has_no_compose_apply_surface():
    source = SCRIPT.read_text()
    for fragment in ('"up"', '"restart"', '"stop"', '"down"'):
        assert fragment not in source
    assert "shell=True" not in source
