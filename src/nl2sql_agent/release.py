"""Release snapshots for the three things that change a deployed answer.

Prompt text lives in code, the schema is a file, experience is a SQLite database.
A release ties their versions together so "roll back to how it answered yesterday"
can be answered with files instead of memory. The prompt itself is *not* restorable
from a snapshot: reverting it requires checking out that code version, so the
manifest records its hash and ``rollback`` says out loud when the running code is
still the newer one.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import Catalog
from .config import Settings
from .memory.experience import ExperienceStore
from .observability import fingerprint

_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_copy(source: Path, target: Path) -> None:
    """Consistent copy even while the source is in WAL mode and being written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source), timeout=10)
    try:
        dst = sqlite3.connect(str(target))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _check_id(release_id: str) -> str:
    value = str(release_id or "").strip()
    if not value or not _ID.match(value):
        raise ValueError(f"非法的 release id: {release_id!r}")
    return value


def releases_root(settings: Settings) -> Path:
    return Path(settings.home) / "releases"


def _release_dir(settings: Settings, release_id: str) -> Path:
    return releases_root(settings) / _check_id(release_id)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 release manifest: {path} ({exc})") from exc
    if not isinstance(value, dict) or not value.get("release_id"):
        raise ValueError(f"release manifest 缺字段: {path}")
    return value


def create_release(
    settings: Settings,
    *,
    prompt_version: str,
    prompt_text: str,
    schema_hash: str = "",
    release_id: str = "",
) -> dict[str, Any]:
    """Snapshot schema + experience + prompt metadata into ``<home>/releases/<id>``."""
    schema_source = Path(settings.schema_path)
    if not schema_source.is_file():
        raise ValueError(f"schema 文件不存在: {schema_source}")
    catalog = Catalog.from_path(schema_source)
    root = releases_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    base_id = _check_id(release_id) if release_id else datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    rid = base_id
    suffix = 1
    while (root / rid).exists():
        suffix += 1
        rid = f"{base_id}-{suffix}"
    final = root / rid
    temp = root / f".tmp-{rid}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)

    schema_file = f"schema{schema_source.suffix or '.json'}"
    shutil.copy2(schema_source, temp / schema_file)
    experience_source = Path(settings.experience_path)
    experience: dict[str, Any] = {
        "source": str(experience_source),
        "present": experience_source.is_file(),
        "file": None,
        "sha256": None,
        "stats": {},
    }
    if experience_source.is_file():
        experience_file = "experience.db"
        _sqlite_copy(experience_source, temp / experience_file)
        experience.update(
            {
                "file": experience_file,
                "sha256": _sha256(temp / experience_file),
                "stats": ExperienceStore(experience_source).stats(),
            }
        )

    manifest: dict[str, Any] = {
        "release_id": rid,
        "created_at": _now(),
        "prompt": {
            "version": str(prompt_version or ""),
            "hash": fingerprint(prompt_text),
            "chars": len(prompt_text or ""),
        },
        "schema": {
            "source": str(schema_source),
            "file": schema_file,
            "sha256": _sha256(temp / schema_file),
            "schema_hash": schema_hash or catalog.schema_hash(),
            "catalog_hash": catalog.schema_hash(),
            "dialect": catalog.dialect,
            "tables": len(catalog.tables),
        },
        "experience": experience,
        "config": {
            "provider": settings.provider,
            "model": settings.model,
            "principal": settings.principal_id,
            "gray_percent": settings.gray_percent,
            "gray_prompt_version": settings.gray_prompt_version,
            "dialect": catalog.dialect,
            "database": settings.database_label,
        },
    }
    (temp / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temp.rename(final)
    return manifest


def list_releases(settings: Settings) -> list[dict[str, Any]]:
    root = releases_root(settings)
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            rows.append(_read_manifest(manifest_path))
        except ValueError:
            continue
    return sorted(rows, key=lambda row: str(row.get("created_at") or ""), reverse=True)


def load_release(settings: Settings, release_id: str) -> dict[str, Any]:
    path = _release_dir(settings, release_id) / "manifest.json"
    if not path.is_file():
        raise ValueError(f"release 不存在: {release_id}")
    return _read_manifest(path)


def _backup_current(settings: Settings, release_id: str, *, schema: bool, experience: bool) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(settings.home) / "var" / "release_backups" / f"{release_id}-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    if schema:
        source = Path(settings.schema_path)
        if source.is_file():
            shutil.copy2(source, backup / f"schema{source.suffix or '.json'}")
    if experience:
        source = Path(settings.experience_path)
        if source.is_file():
            _sqlite_copy(source, backup / "experience.db")
    return backup


def rollback_release(
    settings: Settings,
    release_id: str,
    *,
    prompt_version: str,
    prompt_text: str,
    restore_schema: bool = True,
    restore_experience: bool = True,
) -> dict[str, Any]:
    """Restore schema and experience; report honestly whether the prompt matches.

    Files are verified against the manifest before anything is overwritten, and the
    current files are copied aside first. Prompt rollback is a code checkout, not a
    file copy, so it is reported as pending rather than silently claimed.
    """
    manifest = load_release(settings, release_id)
    root = _release_dir(settings, release_id)
    schema = manifest.get("schema") or {}
    experience = manifest.get("experience") or {}
    result: dict[str, Any] = {
        "release_id": release_id,
        "created_at": manifest.get("created_at"),
        "schema": {"restored": False},
        "experience": {"restored": False},
        "prompt": {},
    }

    if restore_schema:
        snapshot_file = root / str(schema.get("file") or "")
        if not snapshot_file.is_file():
            raise ValueError(f"release 缺少 schema 快照: {snapshot_file}")
        actual = _sha256(snapshot_file)
        if actual != schema.get("sha256"):
            raise ValueError(
                f"release schema 快照校验失败: {release_id}（期望 {schema.get('sha256')}，实际 {actual}）"
            )
        target = Path(settings.schema_path)
        result["schema"] = {
            "restored": True,
            "source": schema.get("source"),
            "target": str(target),
            "sha256": actual,
            "sha256_matches_manifest": True,
        }

    if restore_experience and experience.get("present"):
        snapshot_file = root / str(experience.get("file") or "")
        if not snapshot_file.is_file():
            raise ValueError(f"release 缺少 experience 快照: {snapshot_file}")
        actual = _sha256(snapshot_file)
        if actual != experience.get("sha256"):
            raise ValueError(
                f"release experience 快照校验失败: {release_id}"
                f"（期望 {experience.get('sha256')}，实际 {actual}）"
            )
        result["experience"] = {
            "restored": True,
            "target": str(settings.experience_path),
            "sha256": actual,
            "stats": experience.get("stats") or {},
        }

    if not result["schema"]["restored"] and not result["experience"]["restored"]:
        raise ValueError("没有可回滚的目标：schema 与 experience 都被跳过或快照不存在")

    backup = _backup_current(
        settings,
        release_id,
        schema=result["schema"]["restored"],
        experience=result["experience"]["restored"],
    )
    if result["schema"]["restored"]:
        shutil.copy2(root / str(schema["file"]), Path(settings.schema_path))
    if result["experience"]["restored"]:
        _sqlite_copy(root / str(experience["file"]), Path(settings.experience_path))

    target_prompt = manifest.get("prompt") or {}
    current_hash = fingerprint(prompt_text)
    matches = (
        str(target_prompt.get("version") or "") == str(prompt_version or "")
        and str(target_prompt.get("hash") or "") == current_hash
    )
    result["prompt"] = {
        "target_version": target_prompt.get("version"),
        "target_hash": target_prompt.get("hash"),
        "current_version": prompt_version,
        "current_hash": current_hash,
        "matches": matches,
        "restored": matches,
        # A prompt snapshot cannot rewind Python code; saying so is the point.
        "requires_code_checkout": not matches,
        "note": (
            "当前代码中的 prompt 与 release 一致。"
            if matches
            else "prompt 未回滚：快照只记录版本与 hash，需要 checkout 对应代码版本后重启。"
        ),
    }
    result["backup_dir"] = str(backup)
    return result
