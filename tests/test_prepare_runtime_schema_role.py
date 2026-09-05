"""Local-only tests for schema-role preparation phase-core safeguards."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "prepare_runtime_schema_role.py"
spec = importlib.util.spec_from_file_location("schema_prepare", SCRIPT)
assert spec and spec.loader
prepare = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = prepare
spec.loader.exec_module(prepare)


class FailingRunner:
    def run(self, _argv, **_kwargs):
        return subprocess.CompletedProcess([], 99, "", "secret-would-be-here")


def config(tmp_path: Path) -> prepare.Config:
    os.chmod(tmp_path, 0o700)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(mode=0o700)
    backup = backup_dir / "backup.dump"
    backup.write_bytes(b"local-only-backup")
    os.chmod(backup, 0o600)
    source_manifest = tmp_path / "source.json"
    source_manifest.write_text(
        json.dumps(
            {
                "source_sha": "x" * 40,
                "migration_image_id": "sha256:" + "a" * 64,
                "runtime_image_id": "sha256:" + "b" * 64,
            }
        )
    )
    os.chmod(source_manifest, 0o600)
    return prepare.Config(
        root=ROOT,
        expected_sha="x" * 40,
        source_manifest=source_manifest,
        migration_image="test/migration:local",
        migration_image_id="sha256:" + "a" * 64,
        runtime_image="test/runtime:local",
        runtime_image_id="sha256:" + "b" * 64,
        postgres_container="test-postgres",
        postgres_network="test-network",
        external_dir=tmp_path,
        backup_path=backup,
        backup_sha256=prepare.hashlib.sha256(backup.read_bytes()).hexdigest(),
        service_containers=tuple((service, f"test-{service}") for service in prepare.SERVICES),
    )


def test_secure_publish_refuses_existing_symlink_and_does_not_chmod_target(tmp_path):
    os.chmod(tmp_path, 0o700)
    target = tmp_path / "target"
    target.write_text("keep")
    path = tmp_path / "runtime-api.env"
    path.symlink_to(target)
    original_mode = stat.S_IMODE(target.stat().st_mode)
    with pytest.raises(prepare.PreparationError, match="refusing overwrite"):
        prepare.publish_exclusive(path, "SECRET_KEY=not-printed\n")
    assert target.read_text() == "keep"
    assert stat.S_IMODE(target.stat().st_mode) == original_mode


def test_secure_file_refuses_fifo(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "not-a-file"
    os.mkfifo(path, 0o600)
    with pytest.raises(prepare.PreparationError, match="type/symlink"):
        prepare.assert_secure(path, 0o600, directory=False)


def test_raw_env_roundtrip_preserves_compose_sensitive_literals_and_rejects_newlines():
    values = {
        "DOLLAR": "$foo",
        "BRACED": "${foo}",
        "HASH": "#value",
        "QUOTES": "'\"",
        "SPACE": " leading trailing ",
        "UNICODE": "ключ-ю",
        "BACKSLASH": r"a\b",
    }
    rendered = prepare.env_file(values)
    assert {
        line.split("=", 1)[0]: line.split("=", 1)[1] for line in rendered.splitlines()
    } == values
    with pytest.raises(prepare.PreparationError, match="newline"):
        prepare.env_file({"SECRET_KEY": "line1\nline2"})


def test_failure_runner_error_is_secret_free():
    with pytest.raises(prepare.PreparationError) as exc:
        prepare.run_checked(FailingRunner(), ["docker", "run"], "migration")
    assert str(exc.value) == "migration: exit 99"
    assert "secret-would-be-here" not in str(exc.value)


def test_journal_is_cumulative_and_failure_preserves_last_completed_phase(tmp_path):
    configured = config(tmp_path)
    prepare.journal(configured, prepare.Phase.CHECKED, {"backup": {"sha256": "safe"}})
    prepare.journal(configured, prepare.Phase.MIGRATED, {"migration": {"revision": "0010"}})
    prepare.journal(configured, prepare.Phase.FAILED, {"failure_class": "Injected"})
    content = json.loads(configured.journal.read_text())
    assert content["phase"] == "failed"
    assert content["last_completed_phase"] == "migrated"
    assert content["backup"] == {"sha256": "safe"}
    assert content["migration"] == {"revision": "0010"}
    assert content["phases"] == ["checked", "migrated", "failed"]
    assert stat.S_IMODE(configured.journal.stat().st_mode) == 0o600


def test_owned_ephemeral_cleanup_never_deletes_replaced_path(tmp_path):
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "ephemeral"
    identity = prepare.write_exclusive(path, "owner-env")
    path.unlink()
    path.write_text("replacement")
    os.chmod(path, 0o600)
    with pytest.raises(prepare.PreparationError, match="cleanup ownership"):
        prepare.unlink_owned(path, identity)
    assert path.read_text() == "replacement"


def test_script_has_no_service_switch_or_shell_true_surface():
    source = SCRIPT.read_text()
    for fragment in (
        '"compose", "up"',
        '"compose", "restart"',
        '"compose", "stop"',
        '"compose", "down"',
    ):
        assert fragment not in source
    assert "shell=True" not in source


def test_migration_timeout_source_is_bounded_and_transaction_local():
    source = (ROOT / "alembic" / "env.py").read_text()
    assert "MIGRATION_LOCK_TIMEOUT_MS" in source
    assert "MIGRATION_STATEMENT_TIMEOUT_MS" in source
    assert "SET LOCAL" in source
    assert "1_000 <= value <= 300_000" in source
    assert 'get_migration_database_url().replace("%", "%%")' in source


def test_check_refuses_mutable_tag_when_image_id_does_not_match(monkeypatch, tmp_path):
    configured = config(tmp_path)
    monkeypatch.setattr(prepare, "run_checked", lambda *_args, **_kwargs: "x" * 40)
    monkeypatch.setattr(prepare, "verify_source_manifest", lambda *_args: None)
    monkeypatch.setattr(prepare, "_image_id", lambda *_args: "sha256:" + "c" * 64)
    with pytest.raises(prepare.PreparationError, match="immutable image ID mismatch"):
        prepare.check(prepare.Runner(), configured)


def test_check_refuses_existing_recovery_path_before_any_database_probe(monkeypatch, tmp_path):
    configured = config(tmp_path)
    configured.pending.write_text("not-a-secret")
    os.chmod(configured.pending, 0o600)
    calls: list[str] = []
    monkeypatch.setattr(
        prepare, "run_checked", lambda *_args, **_kwargs: calls.append("run") or "x" * 40
    )
    with pytest.raises(prepare.PreparationError, match="recovery/runtime path already exists"):
        prepare.check(prepare.Runner(), configured)
    assert calls == []


def valid_migration_snapshot():
    functions = [
        {
            "name": name,
            "return_type": "trigger",
            "language": "plpgsql",
            "config": ["search_path=pg_catalog, pg_temp"],
            "security_definer": False,
        }
        for name in (
            "ledger_assert_final_state",
            "ledger_guard_row",
            "ledger_prevent_truncate",
        )
    ]
    triggers = [
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
    ]
    names = {
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
    return {
        "revision": "0010_ledger_foundation",
        "tables": [
            {"name": name, "kind": "r"}
            for name in (
                "ledger_assets",
                "ledger_accounts",
                "ledger_transactions",
                "ledger_entries",
            )
        ],
        "rows": {"assets": 0, "accounts": 0, "transactions": 0, "entries": 0},
        "functions": functions,
        "triggers": [
            {
                "table": table,
                "name": name,
                "function": function,
                "constraint": constraint,
                "deferrable": deferrable,
                "initially_deferred": deferred,
                "enabled": "O",
            }
            for table, name, function, constraint, deferrable, deferred in triggers
        ],
        "constraints": [
            {
                "table": "ledger_assets",
                "name": name,
                "type": "c",
                "deferrable": False,
                "initially_deferred": False,
            }
            for name in names
        ]
        + [
            {
                "table": "ledger_transactions",
                "name": "ledger_final_header",
                "type": "t",
                "deferrable": True,
                "initially_deferred": True,
            },
            {
                "table": "ledger_entries",
                "name": "ledger_final_entry",
                "type": "t",
                "deferrable": True,
                "initially_deferred": True,
            },
        ],
    }


def test_structured_catalog_verifier_accepts_exact_ledger_metadata():
    assert prepare.migration_problems(valid_migration_snapshot()) == []


def test_structured_catalog_verifier_rejects_wrong_guard_search_path():
    state = valid_migration_snapshot()
    state["functions"][0]["config"] = ["search_path=public"]
    assert "function_properties:ledger_assert_final_state" in prepare.migration_problems(state)


def test_main_check_safe_output_uses_real_check_return_shape(monkeypatch, tmp_path, capsys):
    configured = config(tmp_path)
    monkeypatch.setattr(prepare, "verify_source_manifest", lambda *_args: None)
    monkeypatch.setattr(
        prepare,
        "_image_id",
        lambda _runner, image: configured.migration_image_id
        if image == configured.migration_image
        else configured.runtime_image_id,
    )
    monkeypatch.setattr(prepare, "_container_network", lambda *_args: configured.postgres_network)
    monkeypatch.setattr(
        prepare,
        "gate",
        lambda *_args: {
            "revision": "0009_merchant_rails",
            "role_absent": True,
            "ledger_absent": True,
            "public_create": False,
            "public_temp": True,
            "public_secdef_count": 0,
            "sequence_count": 0,
        },
    )
    monkeypatch.setattr(
        prepare,
        "source_runtime_config",
        lambda *_args: (
            {"SECRET_KEY": "secret", "ENCRYPTION_KEY": "key", "REDIS_URL": "redis://x"},
            {"keys": ["SECRET_KEY", "ENCRYPTION_KEY", "REDIS_URL"]},
        ),
    )
    monkeypatch.setattr(prepare, "verify_backup", lambda *_args: {"path": "safe", "sha256": "safe"})
    monkeypatch.setattr(prepare, "run_checked", lambda *_args, **_kwargs: configured.expected_sha)
    real_result = prepare.check(prepare.Runner(), configured)
    assert real_result["phase"] == prepare.Phase.CHECKED
    assert "source_values" in real_result

    monkeypatch.setattr(
        prepare,
        "arguments",
        lambda _argv: type(
            "Args",
            (),
            {
                "mode": "check",
                "confirm_prepare": False,
                "root": configured.root,
                "expected_sha": configured.expected_sha,
                "source_manifest": configured.source_manifest,
                "migration_image": configured.migration_image,
                "migration_image_id": configured.migration_image_id,
                "runtime_image": configured.runtime_image,
                "runtime_image_id": configured.runtime_image_id,
                "postgres_container": configured.postgres_container,
                "postgres_network": configured.postgres_network,
                "external_dir": configured.external_dir,
                "backup_path": configured.backup_path,
                "backup_sha256": configured.backup_sha256,
                "migration_owner": configured.migration_owner,
                "service_container": [
                    f"{name}={container}" for name, container in configured.service_containers
                ],
            },
        )(),
    )
    assert prepare.main([]) == 0
    emitted = capsys.readouterr().out
    assert json.loads(emitted) == {"phase": "checked", "source_sha": configured.expected_sha}
    assert "secret" not in emitted
