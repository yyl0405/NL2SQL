"""Pre-upload privacy gate: scan exactly the files that would go public.

The repository is a portfolio piece while the working catalog, frozen cases and
experience store live in the gitignored ``var/`` of a real deployment. That split
holds only as long as nobody pastes a real table name into a docstring or a key
into a sample config, and neither is visible in a diff review once it is pushed.

Rules are **shapes**, not lists of the customer's identifiers: a scanner that
embeds the names it protects would itself be the leak. Deployment-specific
literals go in an extras file (``var/privacy_patterns.txt``, one regex per line,
gitignored) passed with ``--extra``.

Secret-shaped matches are masked in the report, so running the scan cannot itself
echo a key into a terminal log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

#: Default shape rules. ``secret=True`` masks the matched text in the report.
#:
#: Nothing here names one customer: private IP ranges and account names belong to the
#: extras file, because a public scanner that lists them would be the leak it guards
#: against. The defaults only catch shapes that are wrong in *any* public repository.
DEFAULT_RULES: tuple[tuple[str, str, bool], ...] = (
    # Module-prefixed ERP table names (MODULE_Name style). The synthetic demo catalog
    # uses lowercase snake_case names, so any match here came from a real schema.
    (
        "erp_table_name",
        r"\b(?:CRM|MES|K3|CCM|OA|HR|RDS|WMS|FM|Sp)_[A-Za-z][A-Za-z0-9_]{2,}\b",
        False,
    ),
    # Account-set catalogue names: AC followed by a 10-digit book code.
    ("catalog_name", r"\bAC\d{10}\b", False),
    ("api_key_literal", r"\bsk-[A-Za-z0-9]{20,}\b", True),
    # A quoted credential long enough to be real. Short fixtures ("secret", "hunter2")
    # are deliberately below the bar: a gate that flags test doubles gets ignored, and
    # an online provider keys are 30+ characters.
    (
        "credential_literal",
        r"(?i)\b(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|password|passwd|token|secret)\b"
        r"\s*[=:]\s*[\"'](?!\s*(?:secret|passwd|password|changeme|placeholder|example|dummy|"
        r"none|fake|test|your[_-]?key|[x*]+)\b)[^\"']{16,}[\"']",
        True,
    ),
    # An unquoted DSN credential (`;PWD=...`) with a real-looking value.
    (
        "dsn_credential",
        r"(?i)[;\s](?:pwd|password)\s*=\s*(?![{\$])(?!self\.)(?!settings\.)[A-Za-z0-9+/_\-]{16,}",
        True,
    ),
    ("private_key_material", r"-----BEGIN (?:[A-Z ]*)PRIVATE KEY-----", False),
    ("email_personal", r"\b[A-Za-z0-9._%+-]+@(?:126|163|qq|gmail|outlook)\.com\b", False),
)

#: Files that must never be tracked, whatever their content.
SENSITIVE_FILENAMES = (".env", ".pem", ".key", "id_rsa", "credentials.json", "service-account.json")

#: Opt-out markers, mirroring how `# noqa` keeps a linter usable. Without an escape
#: hatch the honest response to a false positive is to delete the gate, so the tests
#: that *have* to contain a fake key mark the line instead - and the marker is
#: greppable, which a silenced rule is not.
LINE_ALLOW = "privacy-scan: allow"
FILE_ALLOW = "privacy-scan: allow-file"


@dataclass(frozen=True)
class Rule:
    code: str
    pattern: re.Pattern
    secret: bool = False


@dataclass
class Finding:
    path: str
    line: int
    code: str
    match: str
    excerpt: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "code": self.code,
            "match": self.match,
            "excerpt": self.excerpt,
        }


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    rules_used: int = 0
    skipped_binary: int = 0
    extras_file: str = ""

    @property
    def codes(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.findings:
            counts[item.code] = counts.get(item.code, 0) + 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "files_scanned": self.files_scanned,
            "rules_used": self.rules_used,
            "skipped_binary": self.skipped_binary,
            "extras_file": self.extras_file,
            "findings": [item.to_dict() for item in self.findings],
            "codes": self.codes,
        }


def build_rules(extras: Sequence[str] = ()) -> list[Rule]:
    """Default shapes plus the deployment's own literals."""
    rules = [Rule(code, re.compile(pattern), secret) for code, pattern, secret in DEFAULT_RULES]
    for index, raw in enumerate(extras):
        line = str(raw).strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(Rule(f"extras:{index + 1}", re.compile(line)))
        except re.error:
            # A broken pattern would silently disable the check it was added for.
            raise ValueError(f"extras 第 {index + 1} 行不是合法正则: {line!r}") from None
    return rules


def load_extras(path: str | Path | None) -> list[str]:
    if not path:
        return []
    candidate = Path(path)
    if not candidate.is_file():
        raise FileNotFoundError(f"privacy patterns 文件不存在: {candidate}")
    return candidate.read_text(encoding="utf-8").splitlines()


def public_files(root: Path) -> list[Path]:
    """Tracked files plus untracked-and-not-ignored ones: exactly what a push would send."""
    import subprocess

    def git(*args: str) -> list[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "git failed").strip())
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    try:
        names = git("ls-files") + git("ls-files", "--others", "--exclude-standard")
    except (RuntimeError, OSError) as exc:
        raise RuntimeError(f"privacy-scan 需要 git 仓库（{exc}）；非仓库环境请直接传文件列表") from None
    seen: list[Path] = []
    for name in dict.fromkeys(names):
        path = root / name
        if path.is_file():
            seen.append(path)
    return seen


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 7)}{value[-4:]}"


def _excerpt(line: str, match: str, secret: bool) -> str:
    text = line.strip()
    if secret and match:
        text = text.replace(match, _mask(match))
    return text[:160]


def scan_paths(paths: Iterable[Path], rules: Sequence[Rule], *, root: Path | None = None) -> ScanReport:
    report = ScanReport(rules_used=len(rules))
    base = Path(root).resolve() if root is not None else None
    for path in paths:
        report.files_scanned += 1
        resolved = path.resolve()
        if base is not None:
            try:
                label = resolved.relative_to(base).as_posix()
            except ValueError:
                # A path outside the repository still needs a readable label, and it
                # is exactly the case where a real absolute path must not be printed.
                label = path.as_posix()
        else:
            label = path.as_posix()
        lowered = label.lower()
        if any(lowered.endswith(name) or lowered == name for name in SENSITIVE_FILENAMES):
            report.findings.append(Finding(label, 0, "sensitive_filename", path.name))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # A binary or non-UTF-8 file cannot be reviewed line by line; the
            # filename rule above is the only check that still applies.
            report.skipped_binary += 1
            continue
        if FILE_ALLOW in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if LINE_ALLOW in line:
                continue
            for rule in rules:
                found = rule.pattern.search(line)
                if not found:
                    continue
                match = found.group(0)
                report.findings.append(
                    Finding(
                        label,
                        number,
                        rule.code,
                        _mask(match) if rule.secret else match,
                        _excerpt(line, match, rule.secret),
                    )
                )
    return report


def scan_repo(
    home: str | Path,
    *,
    extras_path: str | Path | None = None,
    extras: Sequence[str] = (),
) -> ScanReport:
    root = Path(home)
    rules = build_rules([*load_extras(extras_path), *extras])
    report = scan_paths(public_files(root), rules, root=root)
    report.extras_file = str(extras_path) if extras_path else ""
    return report
