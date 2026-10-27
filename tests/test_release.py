from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nl2sql_agent.config import Settings
from nl2sql_agent.memory.experience import ExperienceStore
from nl2sql_agent.release import (
    create_release,
    list_releases,
    load_release,
    rollback_release,
)


def _settings(tmp_path: Path) -> tuple[Settings, Path]:
    schema = tmp_path / "schema.json"
    shutil.copy2(Settings(home=tmp_path).schema_path, schema)
    home = tmp_path / "home"
    (home / "var").mkdir(parents=True, exist_ok=True)
    return Settings(home=home, provider="mock", schema_override=schema), schema


def test_snapshot_records_all_three_versions(tmp_path: Path) -> None:
    settings, schema = _settings(tmp_path)
    store = ExperienceStore(settings.experience_path)
    store.add("6月销售额是多少？", "SELECT SUM(Amount) FROM SalesOrder")

    manifest = create_release(
        settings, prompt_version="agent-v5", prompt_text="system prompt", schema_hash="op-hash"
    )

    assert manifest["release_id"]
    assert manifest["prompt"]["version"] == "agent-v5"
    assert len(manifest["prompt"]["hash"]) == 16
    assert manifest["schema"]["schema_hash"] == "op-hash"
    assert manifest["schema"]["catalog_hash"]
    assert manifest["schema"]["file"] == "schema.json"
    assert manifest["experience"]["present"] is True
    assert manifest["experience"]["stats"]["total"] == 1
    release_dir = Path(settings.home) / "releases" / manifest["release_id"]
    assert (release_dir / "schema.json").is_file()
    assert (release_dir / "experience.db").is_file()
    assert list_releases(settings)[0]["release_id"] == manifest["release_id"]
    assert load_release(settings, manifest["release_id"])["prompt"]["hash"] == manifest["prompt"]["hash"]
    # Snapshot is read-only with respect to the live files.
    assert schema.read_bytes() == (release_dir / "schema.json").read_bytes()


def test_rollback_restores_schema_and_experience_and_keeps_a_backup(tmp_path: Path) -> None:
    settings, schema = _settings(tmp_path)
    store = ExperienceStore(settings.experience_path)
    store.add("6月销售额是多少？", "SELECT SUM(Amount) FROM SalesOrder")
    manifest = create_release(settings, prompt_version="agent-v5", prompt_text="system prompt")

    original_schema = schema.read_bytes()
    schema.write_bytes(original_schema + b"\n")
    store.add("7月销售额是多少？", "SELECT SUM(Amount) FROM SalesOrder")
    assert store.stats()["total"] == 2

    result = rollback_release(
        settings,
        manifest["release_id"],
        prompt_version="agent-v5",
        prompt_text="system prompt",
    )

    assert result["schema"]["restored"] is True
    assert result["experience"]["restored"] is True
    assert result["prompt"]["restored"] is True
    assert result["prompt"]["requires_code_checkout"] is False
    assert schema.read_bytes() == original_schema
    assert ExperienceStore(settings.experience_path).stats()["total"] == 1
    backup = Path(result["backup_dir"])
    assert (backup / "schema.json").is_file()
    assert (backup / "experience.db").is_file()


def test_prompt_rollback_is_reported_not_silently_claimed(tmp_path: Path) -> None:
    settings, _ = _settings(tmp_path)
    manifest = create_release(settings, prompt_version="agent-v5", prompt_text="system prompt")

    result = rollback_release(
        settings,
        manifest["release_id"],
        prompt_version="agent-v6",
        prompt_text="a newer prompt",
    )

    assert result["schema"]["restored"] is True
    assert result["prompt"]["matches"] is False
    assert result["prompt"]["restored"] is False
    assert result["prompt"]["requires_code_checkout"] is True
    assert "checkout" in result["prompt"]["note"]


def test_corrupted_snapshot_is_refused_before_overwriting(tmp_path: Path) -> None:
    settings, schema = _settings(tmp_path)
    manifest = create_release(settings, prompt_version="agent-v5", prompt_text="system prompt")
    snapshot = Path(settings.home) / "releases" / manifest["release_id"] / "schema.json"
    snapshot.write_text("{}", encoding="utf-8")
    current = schema.read_bytes()

    with pytest.raises(ValueError, match="校验失败"):
        rollback_release(
            settings,
            manifest["release_id"],
            prompt_version="agent-v5",
            prompt_text="system prompt",
        )
    assert schema.read_bytes() == current


def test_release_ids_cannot_escape_the_release_directory(tmp_path: Path) -> None:
    settings, _ = _settings(tmp_path)
    with pytest.raises(ValueError, match="非法的 release id"):
        load_release(settings, "../escape")


def test_missing_schema_is_a_clear_error(tmp_path: Path) -> None:
    settings, schema = _settings(tmp_path)
    schema.unlink()
    with pytest.raises(ValueError, match="schema 文件不存在"):
        create_release(settings, prompt_version="agent-v5", prompt_text="system prompt")


def test_rollback_can_skip_both_targets_and_says_so(tmp_path: Path) -> None:
    settings, _ = _settings(tmp_path)
    manifest = create_release(settings, prompt_version="agent-v5", prompt_text="system prompt")
    with pytest.raises(ValueError, match="没有可回滚的目标"):
        rollback_release(
            settings,
            manifest["release_id"],
            prompt_version="agent-v5",
            prompt_text="system prompt",
            restore_schema=False,
            restore_experience=False,
        )


def test_cli_snapshot_and_list(tmp_path: Path, capsys) -> None:
    from nl2sql_agent.cli import main

    assert main(["release", "snapshot", "--home", str(tmp_path)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["prompt"]["version"] == "agent-v5"
    assert created["schema"]["tables"] > 0

    assert main(["release", "list", "--home", str(tmp_path), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["releases"]
    assert rows[0]["release_id"] == created["release_id"]

    assert main(["release", "rollback", created["release_id"], "--home", str(tmp_path)]) == 2
    assert "--yes" in capsys.readouterr().err


def test_cli_rollback_says_when_the_prompt_still_needs_a_checkout(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from nl2sql_agent.cli import main

    settings, schema = _settings(tmp_path)
    manifest = create_release(
        settings, prompt_version="agent-v5", prompt_text="snapshot prompt"
    )
    # Keep the CLI on the temp schema; the running prompt is the real one, so the
    # snapshot (whose text was synthetic) cannot match it.
    monkeypatch.setenv("NL2SQL_SCHEMA_PATH", str(schema))
    code = main(
        [
            "release",
            "rollback",
            manifest["release_id"],
            "--yes",
            "--home",
            str(settings.home),
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    payload = json.loads(captured.out)
    assert payload["schema"]["restored"] is True
    assert payload["prompt"]["matches"] is False
    assert "checkout" in captured.err
