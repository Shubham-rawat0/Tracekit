"""Environment-driven configuration for Tracekit."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from tracekit.core.tenant import normalize_environment

VALID_ENVIRONMENTS = frozenset({"development", "staging", "production"})


def _load_dotenv(path: Path) -> None:
    """Load missing environment variables from a simple .env file."""

    if not path.is_file():
        return
    # Minimal dotenv loader; real process env vars take precedence over .env values.
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool_env(name: str, default: bool) -> bool:
    """Parse a boolean environment variable with a safe default."""

    value = os.getenv(name)
    if value is None:
        return default
    # Accept common truthy spellings; unknown values fall through to False.
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    """Parse a float environment variable, falling back on invalid values."""

    value = os.getenv(name)
    if value is None:
        return default
    # Invalid env values degrade to defaults rather than failing config load.
    try:
        return float(value)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    """Parse an integer environment variable, falling back on invalid values."""

    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _parse_headers(value: str | None) -> dict[str, str]:
    """Parse comma-separated KEY=VALUE OTLP headers into a mapping."""

    headers: dict[str, str] = {}
    for item in (value or "").split(","):
        if "=" not in item:
            continue
        key, header_value = item.split("=", 1)
        if key.strip():
            headers[key.strip()] = header_value.strip()
    return headers


@dataclass(frozen=True)
class TracekitConfig:
    db_path: Path
    log_level: str = "INFO"
    service_name: str = "tracekit"
    environment: str = "development"
    otlp_endpoint: str | None = None
    otlp_headers: dict[str, str] = field(default_factory=dict)
    auto_flush: bool = False
    flush_interval_seconds: float = 30.0
    max_spans_in_memory: int = 1000
    sampling_rate: float = 1.0
    token_estimation_jitter: float = 0.10

    def public_dict(self) -> dict[str, object]:
        """Return a serializable config view with secrets and headers omitted."""

        return {
            "db_path": str(self.db_path),
            "log_level": self.log_level,
            "service_name": self.service_name,
            "environment": self.environment,
            "otlp_endpoint": self.otlp_endpoint,
            "otlp_header_count": len(self.otlp_headers),
            "auto_flush": self.auto_flush,
            "flush_interval_seconds": self.flush_interval_seconds,
            "max_spans_in_memory": self.max_spans_in_memory,
            "sampling_rate": self.sampling_rate,
            "token_estimation_jitter": self.token_estimation_jitter,
        }


def load_config(dotenv_path: str | Path | None = ".env") -> TracekitConfig:
    """Build the runtime configuration from environment variables and dotenv."""

    if dotenv_path is not None:
        _load_dotenv(Path(dotenv_path))

    # Precedence: explicit TRACEKIT_DB_PATH, then legacy TRACELLM_HOME default location.
    db_path = os.getenv("TRACEKIT_DB_PATH") or (
        Path(os.getenv("TRACELLM_HOME", "~/.tracellm")).expanduser() / "traces.db"
    )
    environment = normalize_environment(
        os.getenv("TRACEKIT_ENVIRONMENT", "development").strip(),
        default="development",
    )

    token_jitter = _float_env("TRACEKIT_TOKEN_ESTIMATION_JITTER", 0.10)

    return TracekitConfig(
        db_path=Path(db_path).expanduser(),
        log_level=os.getenv("TRACEKIT_LOG_LEVEL", "INFO").upper(),
        service_name=os.getenv("TRACEKIT_SERVICE_NAME", "tracekit"),
        environment=environment,
        otlp_endpoint=os.getenv("OTLP_ENDPOINT") or None,
        otlp_headers=_parse_headers(os.getenv("OTLP_HEADERS")),
        auto_flush=_bool_env("TRACEKIT_AUTO_FLUSH", False),
        flush_interval_seconds=_float_env("TRACEKIT_FLUSH_INTERVAL_SECONDS", 30.0),
        max_spans_in_memory=_int_env("TRACEKIT_MAX_SPANS_IN_MEMORY", 1000),
        sampling_rate=_float_env("TRACEKIT_SAMPLING_RATE", 1.0),
        token_estimation_jitter=min(1.0, max(0.0, token_jitter)),
    )
