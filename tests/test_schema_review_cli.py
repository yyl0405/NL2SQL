"""The review CLI is the gate between a model draft and the runtime overlay."""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.cli import main
from nl2sql_agent.schema_overlay import load_overlay
from nl2sql_agent.schema_suggest import load_suggestions


def make_files(tmp_path: Path, schema_hash: str = "current") -> tuple[Path, Path]:
    suggestions = tmp_path / "suggestions.json"
    suggestions.write_text(
        json.dumps(
            {
                "schema_hash": schema_hash,
                "suggestions": [
                    {
                        "id": "客户@customer",
                        "concept": "客户",
                        "table": "Customer",
                        "confidence": 0.7,
                        "field_aliases": {"CustomerName": ["客户名称"]},
                        "business_rule": "客户名称取 Customer.CustomerName",
                        "review_status": "pending",
                        "schema_hash": schema_hash,
                    }
                ],
                "observations": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return suggestions, tmp_path / "overlay.json"


def test_review_list_and_show_filter_status(tmp_path: Path, capsys) -> None:
    suggestions, _ = make_files(tmp_path)
    assert main(["schema-review", "list", "--suggestions", str(suggestions), "--home", str(tmp_path)]) == 0
    assert "客户@customer" in capsys.readouterr().out
    assert main(
        [
            "schema-review",
            "list",
            "--status",
            "approved",
            "--suggestions",
            str(suggestions),
            "--home",
            str(tmp_path),
        ]
    ) == 0
    assert "没有 approved" in capsys.readouterr().out
    assert main(["schema-review", "show", "客户@customer", "--suggestions", str(suggestions), "--home", str(tmp_path)]) == 0
    assert '"table": "Customer"' in capsys.readouterr().out


def test_approve_writes_overlay_and_reject_does_not(tmp_path: Path, capsys) -> None:
    suggestions, overlay = make_files(tmp_path)
    assert main(
        [
            "schema-review",
            "approve",
            "客户@customer",
            "--force",
            "--reviewer",
            "yyl",
            "--suggestions",
            str(suggestions),
            "--overlay",
            str(overlay),
            "--home",
            str(tmp_path),
        ]
    ) == 0
    saved_overlay = load_overlay(overlay)
    assert saved_overlay["table_aliases"]["Customer"] == ["客户"]
    assert saved_overlay["approved"][0]["reviewer"] == "yyl"
    assert load_suggestions(suggestions)["suggestions"][0]["review_status"] == "approved"

    rejected = tmp_path / "rejected.json"
    rejected.write_text(suggestions.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(
        [
            "schema-review",
            "reject",
            "客户@customer",
            "--note",
            "表不对",
            "--suggestions",
            str(rejected),
            "--home",
            str(tmp_path),
        ]
    ) == 0
    assert load_suggestions(rejected)["suggestions"][0]["review_status"] == "rejected"
    assert "overlay 未变化" in capsys.readouterr().out


def test_approve_refuses_stale_schema_hash_without_force(tmp_path: Path, capsys) -> None:
    suggestions, overlay = make_files(tmp_path, schema_hash="stale")
    rc = main(
        [
            "schema-review",
            "approve",
            "客户@customer",
            "--suggestions",
            str(suggestions),
            "--overlay",
            str(overlay),
            "--home",
            str(tmp_path),
        ]
    )
    assert rc == 2
    assert "schema hash" in capsys.readouterr().err
    assert not overlay.exists()
