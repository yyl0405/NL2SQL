"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    provider: str = "mock"
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    home: Path = field(default_factory=lambda: Path.cwd())
    db_path: Path | None = None
    experience_path: Path | None = None
    max_steps: int = 12
    max_rows: int = 200
    query_timeout_ms: int = 5000
    max_tokens: int = 4096
    disable_thinking: bool = True
    allow_sensitive: bool = False
    demo_today: str = "2026-07-01"
    request_timeout_sec: float = 120.0
    judge_enabled: bool = False
    judge_provider: str = "openai"
    judge_model: str = "qwen-max"
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_max_tokens: int = 800
    judge_max_rows: int = 20
    judge_cache_path: Path | None = None
    judge_prompt_version: str = "judge-v2"
    judge_max_calls: int = 100
    few_shot_enabled: bool = True
    few_shot_max: int = 3
    few_shot_max_chars: int = 1200
    capture_failures: bool = True
    max_repairs: int = 2
    category_boost: float = 0.12

    def __post_init__(self) -> None:
        self.home = Path(self.home).resolve()
        if self.db_path is None:
            self.db_path = self.home / "var" / "demo_erp.db"
        if self.experience_path is None:
            self.experience_path = self.home / "var" / "experience.db"
        if self.judge_cache_path is None:
            self.judge_cache_path = self.home / "var" / "judge_cache.sqlite"
        self.db_path = Path(self.db_path)
        self.experience_path = Path(self.experience_path)
        self.judge_cache_path = Path(self.judge_cache_path)

    @classmethod
    def load(cls, home: str | Path | None = None, provider: str | None = None) -> "Settings":
        base = Path(home) if home is not None else Path(os.getenv("NL2SQL_HOME", Path.cwd()))
        _load_dotenv(base / ".env")
        package_root = Path(__file__).resolve().parents[2]
        _load_dotenv(package_root / ".env")

        selected_provider = (provider or os.getenv("NL2SQL_PROVIDER") or "").strip().lower()
        api_key = (
            os.getenv("NL2SQL_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or ""
        ).strip()
        if not selected_provider:
            selected_provider = "openai" if api_key else "mock"

        base_url = (
            os.getenv("NL2SQL_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
        settings = cls(
            provider=selected_provider,
            model=(os.getenv("NL2SQL_MODEL") or os.getenv("OPENAI_MODEL") or "qwen-plus").strip(),
            base_url=base_url,
            api_key=api_key,
            home=base,
            db_path=(os.getenv("NL2SQL_DB_PATH") or None),
            experience_path=(os.getenv("NL2SQL_EXPERIENCE_PATH") or None),
            max_steps=_as_int(os.getenv("NL2SQL_MAX_STEPS"), 12),
            max_rows=_as_int(os.getenv("NL2SQL_MAX_ROWS"), 200),
            query_timeout_ms=_as_int(os.getenv("NL2SQL_QUERY_TIMEOUT_MS"), 5000),
            max_tokens=_as_int(os.getenv("NL2SQL_MAX_TOKENS"), 4096),
            disable_thinking=_as_bool(os.getenv("NL2SQL_DISABLE_THINKING"), True),
            allow_sensitive=_as_bool(os.getenv("NL2SQL_ALLOW_SENSITIVE"), False),
            demo_today=os.getenv("NL2SQL_DEMO_TODAY", "2026-07-01").strip(),
            request_timeout_sec=float(os.getenv("NL2SQL_REQUEST_TIMEOUT_SEC", "120")),
            judge_enabled=_as_bool(os.getenv("NL2SQL_JUDGE_ENABLED"), False),
            judge_provider=(os.getenv("NL2SQL_JUDGE_PROVIDER") or "openai").strip().lower(),
            judge_model=(os.getenv("NL2SQL_JUDGE_MODEL") or "qwen-max").strip(),
            judge_base_url=(os.getenv("NL2SQL_JUDGE_BASE_URL") or base_url).strip(),
            judge_api_key=(os.getenv("NL2SQL_JUDGE_API_KEY") or api_key).strip(),
            judge_max_tokens=_as_int(os.getenv("NL2SQL_JUDGE_MAX_TOKENS"), 800),
            judge_max_rows=_as_int(os.getenv("NL2SQL_JUDGE_MAX_ROWS"), 20),
            judge_cache_path=(os.getenv("NL2SQL_JUDGE_CACHE_PATH") or None),
            judge_prompt_version=(os.getenv("NL2SQL_JUDGE_PROMPT_VERSION") or "judge-v2").strip(),
            judge_max_calls=_as_int(os.getenv("NL2SQL_JUDGE_MAX_CALLS"), 100),
            few_shot_enabled=_as_bool(os.getenv("NL2SQL_FEWSHOT_ENABLED"), True),
            few_shot_max=_as_int(os.getenv("NL2SQL_FEWSHOT_MAX"), 3),
            few_shot_max_chars=_as_int(os.getenv("NL2SQL_FEWSHOT_MAX_CHARS"), 1200),
            capture_failures=_as_bool(os.getenv("NL2SQL_CAPTURE_FAILURES"), True),
            max_repairs=_as_int(os.getenv("NL2SQL_MAX_REPAIRS"), 2),
            category_boost=_as_float(os.getenv("NL2SQL_CATEGORY_BOOST"), 0.12),
        )
        return settings

    @property
    def schema_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "demo_schema.json"

    @property
    def eval_cases_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "demo_cases.jsonl"

    @property
    def calibration_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "judge_calibration.jsonl"

    def ensure_dirs(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.experience_path.parent.mkdir(parents=True, exist_ok=True)
