"""Typed application configuration.

Loads values from the environment (optionally a local ``.env``), parses/validates
types via Pydantic, and exposes a single importable ``config`` instance. See
``docs/development/configuration.md`` for what each variable does and how to tune it.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Load environment variables from .env if present
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int_set(key: str, default: set[int] | None = None) -> set[int]:
    raw = os.getenv(key, "")
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return result or (default or set())


class Config(BaseModel):
    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str = Field(default_factory=lambda: _env_str("TELEGRAM_BOT_TOKEN", ""))

    # --- LLM connection ---
    LLM_BASE_URL: str = Field(default_factory=lambda: _env_str("LLM_BASE_URL", "http://localhost:1234/v1"))
    LLM_API_KEY: str = Field(default_factory=lambda: _env_str("LLM_API_KEY", "none"))
    LLM_MODEL: str = Field(default_factory=lambda: _env_str("LLM_MODEL", "gpt-4o"))
    LLM_EXTRACTION_MODEL: str = Field(default_factory=lambda: _env_str("LLM_EXTRACTION_MODEL", ""))
    # Structured-output strategy: "json_object" (works with Gemini/local proxies) or
    # "native_parse" (OpenAI structured outputs via beta.chat.completions.parse).
    LLM_STRUCTURED_MODE: str = Field(default_factory=lambda: _env_str("LLM_STRUCTURED_MODE", "json_object"))
    LLM_MAX_RETRIES: int = Field(default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2))
    LLM_RETRY_BASE_DELAY_SECS: float = Field(default_factory=lambda: _env_float("LLM_RETRY_BASE_DELAY_SECS", 0.5))
    REPLY_TEMPERATURE: float = Field(default_factory=lambda: _env_float("REPLY_TEMPERATURE", 0.7))
    EXTRACTION_TEMPERATURE: float = Field(default_factory=lambda: _env_float("EXTRACTION_TEMPERATURE", 0.1))

    # --- MongoDB ---
    MONGODB_URI: str = Field(default_factory=lambda: _env_str("MONGODB_URI", "mongodb://localhost:27017"))
    MONGODB_DB: str = Field(default_factory=lambda: _env_str("MONGODB_DB", "thinkmate_db"))
    AUDIT_LOG_RETENTION_DAYS: int = Field(default_factory=lambda: _env_int("AUDIT_LOG_RETENTION_DAYS", 30))

    # --- Memory tuning ---
    CHAT_BUFFER_MAX_CHARS: int = Field(default_factory=lambda: _env_int("CHAT_BUFFER_MAX_CHARS", 10000))
    CHAT_BUFFER_TRIM: int = Field(default_factory=lambda: _env_int("CHAT_BUFFER_TRIM", 10))
    CHAT_BUFFER_HARD_CAP: int = Field(default_factory=lambda: _env_int("CHAT_BUFFER_HARD_CAP", 200))
    USER_MEMORY_BUDGET_CHARS: int = Field(default_factory=lambda: _env_int("USER_MEMORY_BUDGET_CHARS", 4000))
    COMPRESSION_COOLDOWN_SECS: float = Field(default_factory=lambda: _env_float("COMPRESSION_COOLDOWN_SECS", 300.0))
    CHARS_PER_TOKEN: int = Field(default_factory=lambda: _env_int("CHARS_PER_TOKEN", 4))

    # --- Batching / responsiveness ---
    MESSAGE_BATCH_DELAY_SECS: float = Field(default_factory=lambda: _env_float("MESSAGE_BATCH_DELAY_SECS", 1.5))
    MAX_BATCH_DELAY_SECS: float = Field(default_factory=lambda: _env_float("MAX_BATCH_DELAY_SECS", 5.0))
    USER_STATE_TTL_SECS: float = Field(default_factory=lambda: _env_float("USER_STATE_TTL_SECS", 1800.0))

    # --- Input/Output guards ---
    RATE_LIMIT_MAX_REQUESTS: int = Field(default_factory=lambda: _env_int("RATE_LIMIT_MAX_REQUESTS", 5))
    RATE_LIMIT_WINDOW_SECS: float = Field(default_factory=lambda: _env_float("RATE_LIMIT_WINDOW_SECS", 10.0))
    MAX_QUEUED_MESSAGES: int = Field(default_factory=lambda: _env_int("MAX_QUEUED_MESSAGES", 10))
    MAX_INPUT_CHARS: int = Field(default_factory=lambda: _env_int("MAX_INPUT_CHARS", 2500))
    MAX_RESPONSE_CHARS: int = Field(default_factory=lambda: _env_int("MAX_RESPONSE_CHARS", 2000))

    # --- Group chat / ambient replies ---
    GROUP_AMBIENT_COOLDOWN_SECS: float = Field(default_factory=lambda: _env_float("GROUP_AMBIENT_COOLDOWN_SECS", 90.0))
    GROUP_AMBIENT_BASE_RATE: float = Field(default_factory=lambda: _env_float("GROUP_AMBIENT_BASE_RATE", 0.25))
    GROUP_CONTEXT_SCAN_EVERY: int = Field(default_factory=lambda: _env_int("GROUP_CONTEXT_SCAN_EVERY", 12))
    AFFINITY_DEFAULT: float = Field(default_factory=lambda: _env_float("AFFINITY_DEFAULT", 0.5))

    # --- Persona / features ---
    PERSONA_FILE: str = Field(default_factory=lambda: _env_str("PERSONA_FILE", "persona.md"))
    ENABLE_MESSAGE_REACTIONS: bool = Field(default_factory=lambda: _env_bool("ENABLE_MESSAGE_REACTIONS", True))

    # --- Observability / ops ---
    ADMIN_USER_IDS: set[int] = Field(default_factory=lambda: _env_int_set("ADMIN_USER_IDS"))
    METRICS_LOG_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("METRICS_LOG_INTERVAL_SECS", 0.0))

    # --- Consolidation (Phase 11) ---
    CONSOLIDATION_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("CONSOLIDATION_INTERVAL_SECS", 0.0))
    CONSOLIDATION_SCAN_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("CONSOLIDATION_SCAN_INTERVAL_SECS", 3600.0))
    CONSOLIDATION_MAX_USERS_PER_SCAN: int = Field(default_factory=lambda: _env_int("CONSOLIDATION_MAX_USERS_PER_SCAN", 50))
    CONSOLIDATION_MIN_ITEMS: int = Field(default_factory=lambda: _env_int("CONSOLIDATION_MIN_ITEMS", 8))
    MAX_INSIGHTS: int = Field(default_factory=lambda: _env_int("MAX_INSIGHTS", 5))


config = Config()
