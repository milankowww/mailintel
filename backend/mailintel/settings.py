from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _preset_cooldown_recovery_interval_seconds() -> int:
    return int(os.environ.get("MAILINTEL_PRESET_COOLDOWN_RECOVERY_INTERVAL_SECONDS", "5"))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _config_dir() -> Path:
    return Path(os.environ.get("MAILINTEL_CONFIG_DIR", str(Path.home() / ".config" / "mailintel"))).expanduser()


def _split_csv_env(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "")
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(slots=True)
class AppSettings:
    deployment_profile: str = field(default_factory=lambda: os.environ.get("MAILINTEL_PROFILE", "local-python"))
    database_url: str = field(
        default_factory=lambda: os.environ.get("MAILINTEL_DATABASE_URL", "postgresql://localhost/mailintel")
    )
    host: str = field(default_factory=lambda: os.environ.get("MAILINTEL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("MAILINTEL_PORT", "8000")))
    body_segment_chars: int = field(default_factory=lambda: int(os.environ.get("MAILINTEL_BODY_SEGMENT_CHARS", "6000")))
    job_stale_after_minutes: int = field(default_factory=lambda: int(os.environ.get("MAILINTEL_JOB_STALE_AFTER_MINUTES", "60")))
    llm_provider_timeout_seconds: float = field(
        default_factory=lambda: float(os.environ.get("MAILINTEL_LLM_TIMEOUT_SECONDS", "1800"))
    )
    llm_paused: bool = field(default_factory=lambda: _env_bool("MAILINTEL_LLM_PAUSED"))
    sync_interval_seconds: int = field(default_factory=lambda: int(os.environ.get("MAILINTEL_SYNC_INTERVAL_SECONDS", "120")))
    worker_poll_seconds: float = field(default_factory=lambda: float(os.environ.get("MAILINTEL_WORKER_POLL_SECONDS", "2")))
    cluster_worker_count: int = field(default_factory=lambda: int(os.environ.get("MAILINTEL_CLUSTER_WORKER_COUNT", "4")))
    smart_search_question_max_bytes: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_QUESTION_MAX_BYTES", "12000"))
    )
    smart_search_owner_timezone: str = field(
        default_factory=lambda: (
            os.environ.get(
                "MAILINTEL_SMART_SEARCH_OWNER_TIMEZONE",
                os.environ.get("MAILINTEL_SUMMARY_TIMEZONE", os.environ.get("TZ", "UTC")),
            ).strip()
            or "UTC"
        )
    )
    smart_search_candidate_limit: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_CANDIDATE_LIMIT", "40"))
    )
    smart_search_complete_limit: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_COMPLETE_LIMIT", "500"))
    )
    smart_search_max_materialized_bytes: int = field(
        default_factory=lambda: int(
            os.environ.get("MAILINTEL_SMART_SEARCH_MAX_MATERIALIZED_BYTES", str(16 * 1024 * 1024))
        )
    )
    smart_search_statement_timeout_ms: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_STATEMENT_TIMEOUT_MS", "60000"))
    )
    smart_search_wall_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_WALL_TIMEOUT_SECONDS", "1800"))
    )
    smart_search_max_compression_calls: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_MAX_COMPRESSION_CALLS", "24"))
    )
    smart_search_max_compression_depth: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_MAX_COMPRESSION_DEPTH", "4"))
    )
    smart_search_max_evidence_nodes: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_MAX_EVIDENCE_NODES", "4096"))
    )
    smart_search_safety_margin_tokens: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_SAFETY_MARGIN_TOKENS", "256"))
    )
    smart_search_result_retention_hours: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_SMART_SEARCH_RESULT_RETENTION_HOURS", "24"))
    )
    preset_cooldown_recovery_interval_seconds: int = field(
        default_factory=_preset_cooldown_recovery_interval_seconds
    )
    interval_summary_retry_seconds: int = field(
        default_factory=lambda: int(os.environ.get("MAILINTEL_INTERVAL_SUMMARY_RETRY_SECONDS", "30"))
    )
    summary_timezone: str = field(
        default_factory=lambda: os.environ.get("MAILINTEL_SUMMARY_TIMEZONE", os.environ.get("TZ", "UTC"))
    )
    config_dir: Path = field(default_factory=_config_dir)
    static_dir: Path = field(default_factory=lambda: Path(__file__).resolve().parent / "web")
    api_token: str | None = field(default_factory=lambda: os.environ.get("MAILINTEL_API_TOKEN"))
    api_token_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MAILINTEL_API_TOKEN_FILE", str(_config_dir() / "api-token"))
        ).expanduser()
    )
    cors_origins: tuple[str, ...] = field(default_factory=lambda: _split_csv_env("MAILINTEL_CORS_ORIGINS"))
    encryption_key_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MAILINTEL_ENCRYPTION_KEY_FILE", str(_config_dir() / "email-password.key"))
        ).expanduser()
    )

    def __post_init__(self) -> None:
        if not 1 <= self.cluster_worker_count <= 20:
            raise ValueError("MAILINTEL_CLUSTER_WORKER_COUNT must be between 1 and 20")


def get_settings() -> AppSettings:
    return AppSettings()
