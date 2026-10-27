"""The pre-upload privacy gate: it must bite on real shapes and stay quiet on ours.

A gate that cries wolf gets bypassed, so the "no findings" half matters as much as
the detection half: demo catalog names, short test fixtures and documentation
placeholder hosts are all things this repository legitimately contains.

# privacy-scan: allow-file - every fixture below is leak-shaped on purpose, and the
# scanner is one of the things being tested. The marker is the auditable opt-out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nl2sql_agent.privacy_scan import (
    build_rules,
    load_extras,
    public_files,
    scan_paths,
    scan_repo,
)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _codes(root: Path, text: str, extras=()) -> dict[str, int]:
    path = _write(root, "docs/sample.md", text)
    report = scan_paths([path], build_rules(extras), root=root)
    return report.codes


def test_erp_table_shape_is_caught(tmp_path: Path) -> None:
    assert "erp_table_name" in _codes(tmp_path, "字段来自 CRM_Contract.HTDate\n")
    assert "erp_table_name" in _codes(tmp_path, "join MES_Order on id\n")


def test_account_set_catalog_and_internal_host_shapes(tmp_path: Path) -> None:
    assert "catalog_name" in _codes(tmp_path, "账套库 AC2025100001 里没有业务表\n")
    extras = [r"192\.168\.\d{1,3}\.\d{1,3}"]
    assert "extras:1" in _codes(tmp_path, "server=192.168.102.199,51125\n", extras)


def test_credential_shapes_are_caught_and_masked(tmp_path: Path) -> None:
    secret_key = "sk-abcdefghijklmnopqrstuvwx1234"
    path = _write(
        tmp_path,
        "config/sample.txt",
        f'api_key = "{secret_key}"\n;DRIVER=x;SERVER=y;UID=ro;PWD=Sup3rSecretPassword99\n',
    )
    report = scan_paths([path], build_rules(), root=tmp_path)
    codes = set(report.codes)
    assert {"api_key_literal", "credential_literal", "dsn_credential"} <= codes
    dumped = str(report.to_dict())
    assert secret_key not in dumped
    assert "Sup3rSecretPassword99" not in dumped


def test_private_key_block_is_caught(tmp_path: Path) -> None:
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n"
    assert "private_key_material" in _codes(tmp_path, text)


def test_demo_names_and_test_doubles_stay_clean(tmp_path: Path) -> None:
    benign = """\
    SELECT ROUND(SUM(oi.qty * oi.unit_price), 2) FROM sales_order o
    JOIN order_item oi ON oi.order_id = o.order_id
    NL2SQL_MSSQL_SERVER=10.0.0.5
    NL2SQL_PG_HOST=127.0.0.1
    password="secret"
    password=self.mssql_password,
    PWD=hunter2
    api_key=settings.api_key
    "Login timeout for SERVER=10.0.0.7;DATABASE=erp;UID=svc;PWD=s3cr3t"
    assert "PWD=secret" in target.dsn()
    sk-abcdef123456
    """
    assert _codes(tmp_path, benign) == {}


def test_tracked_dotenv_is_reported_by_name_only(tmp_path: Path) -> None:
    secret_key = "sk-abcdefghijklmnopqrstuvwx1234"
    path = _write(tmp_path, ".env", f"NL2SQL_API_KEY={secret_key}\n")
    report = scan_paths([path], build_rules(), root=tmp_path)
    assert [item.code for item in report.findings] == ["sensitive_filename"]
    assert secret_key not in str(report.to_dict())


def test_extra_patterns_carry_the_customers_own_literals(tmp_path: Path) -> None:
    text = "reviewer leigg 说先看合同表\n"
    assert _codes(tmp_path, text) == {}
    assert "extras:1" in _codes(tmp_path, text, [r"\bleigg\b", r"合同表"])


def test_broken_extra_regex_raises_instead_of_silently_passing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不是合法正则"):
        build_rules([r"CRM_(["])


def test_load_extras_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_extras(tmp_path / "nope.txt")
    assert load_extras(None) == []


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


def _make_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _write(root, "README.md", "# demo catalog only\nsales_order\n")
    _write(root, "notes.md", "CRM_Contract 是真实表\n")
    _write(root, "var/private.md", "CRM_Customer 副本表\n")
    _write(root, ".gitignore", "var/\n.env\n")
    _write(root, ".env", "NL2SQL_API_KEY=sk-abcdefghijklmnopqrstuvwx1234\n")
    _git(root, "add", "README.md", "notes.md", ".gitignore")
    _git(root, "commit", "-qm", "init")


def test_public_files_exclude_ignored_paths(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    names = {item.name for item in public_files(tmp_path)}
    assert {"README.md", "notes.md", ".gitignore"} <= names
    # var/ and .env are ignored, so they never enter the public file set at all.
    assert "private.md" not in names
    assert ".env" not in names


def test_scan_repo_fails_on_the_tracked_leak_and_passes_when_scrubbed(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    report = scan_repo(tmp_path)
    assert any(item.path == "notes.md" and item.code == "erp_table_name" for item in report.findings)

    (tmp_path / "notes.md").write_text("合同表在演示目录里叫 sales_order\n", encoding="utf-8")
    assert scan_repo(tmp_path).findings == []


def test_line_and_file_allow_markers_are_the_auditable_opt_out(tmp_path: Path) -> None:
    line_path = _write(
        tmp_path,
        "tests/fake_fixture.py",
        'value = "CRM_Contract"  # privacy-scan: allow\nother = "CRM_Order"\n',
    )
    codes = {item.line: item.code for item in scan_paths([line_path], build_rules(), root=tmp_path).findings}
    assert 1 not in codes and codes.get(2) == "erp_table_name"

    file_path = _write(
        tmp_path,
        "tests/fake_scanner_test.py",
        "# privacy-scan: allow-file\nvalue = \"CRM_Contract\"\n",
    )
    assert scan_paths([file_path], build_rules(), root=tmp_path).findings == []
