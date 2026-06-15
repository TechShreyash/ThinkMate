"""Typed application configuration.

Loads values from the environment (optionally a local ``.env``), parses/validates
types via Pydantic, and exposes a single importable ``config`` instance. See
``docs/development/configuration.md`` for what each variable does and how to tune it.
"""
import os
import re
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger
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


def _env_int_or_none(key: str) -> int | None:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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


# Canonical built-in command keys, in help-display order. The key is also the DEFAULT
# trigger name for that command.
_BUILTIN_COMMANDS: tuple[str, ...] = (
    "start", "onboard", "checkins",
    "profile", "reset", "reactions", "quiet", "chatty",
    "groupbot", "groupmode",
    "health", "metrics",
)

# Telegram command name rule: 1-32 chars, letters/digits/underscore. Used to reject
# invalid configured trigger names (e.g. containing spaces, "/", or punctuation).
_CMD_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")


def resolve_command_config() -> dict[str, tuple[str, bool]]:
    """Resolve {command_key: (trigger_name, enabled)} for every Built_In_Command.

    Reads CMD_<KEY>_NAME (trigger override, default = key) and CMD_<KEY>_ENABLED
    (bool, default True). Invalid trigger names fall back to the default; a trigger
    that duplicates another command's resolved trigger falls back to the default for
    BOTH colliding commands. Never raises: any unexpected parse error yields the
    all-defaults mapping (Req 7.5, 7.7).
    """
    try:
        raw: dict[str, tuple[str, bool]] = {}
        for key in _BUILTIN_COMMANDS:
            name = _env_str(f"CMD_{key.upper()}_NAME", key).strip().lstrip("/")
            enabled = _env_bool(f"CMD_{key.upper()}_ENABLED", True)
            if not _CMD_NAME_RE.match(name):
                logger.warning(
                    f"command config: invalid trigger {name!r} for {key!r}; "
                    f"falling back to default {key!r}"
                )
                name = key
            raw[key] = (name, enabled)

        # Duplicate detection among ENABLED commands' resolved triggers. Any command
        # whose trigger collides with another's falls back to its own default (the key).
        # Defaults are unique by construction, so fallback always resolves the collision.
        seen: dict[str, list[str]] = {}
        for key, (name, enabled) in raw.items():
            if enabled:
                seen.setdefault(name, []).append(key)
        for name, keys in seen.items():
            if len(keys) > 1:
                logger.warning(
                    f"command config: trigger {name!r} duplicated by {keys}; "
                    f"falling back to default names for those commands"
                )
                for key in keys:
                    enabled = raw[key][1]
                    raw[key] = (key, enabled)  # default trigger == key
        return raw
    except Exception as exc:  # never crash startup (Req 7.7)
        logger.warning(f"command config parse failed; using all defaults: {exc}")
        return {key: (key, True) for key in _BUILTIN_COMMANDS}


class Config(BaseModel):
    # --- Telegram ---
    TELEGRAM_BOT_TOKEN: str = Field(default_factory=lambda: _env_str("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_PUBLISH_COMMANDS: bool = Field(default_factory=lambda: _env_bool("TELEGRAM_PUBLISH_COMMANDS", True))

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
    # Per-request client timeout. Must sit ABOVE the proxy's own request ceiling
    # (currently 600s for non-streaming) so the proxy is always the first to give
    # up: the client then receives a definitive error instead of abandoning a
    # still-running generation and re-spawning it on retry (duplicate upstream
    # work). Generous by design — reasoning models can legitimately run for
    # minutes. See docs/development/llm_integration.md.
    LLM_REQUEST_TIMEOUT_SECS: float = Field(default_factory=lambda: _env_float("LLM_REQUEST_TIMEOUT_SECS", 610.0))

    # --- MongoDB ---
    MONGODB_URI: str = Field(default_factory=lambda: _env_str("MONGODB_URI", "mongodb://localhost:27017"))
    MONGODB_DB: str = Field(default_factory=lambda: _env_str("MONGODB_DB", "thinkmate_db"))
    AUDIT_LOG_RETENTION_DAYS: int = Field(default_factory=lambda: _env_int("AUDIT_LOG_RETENTION_DAYS", 30))

    # --- Memory tuning ---
    CHAT_BUFFER_MAX_CHARS: int = Field(default_factory=lambda: _env_int("CHAT_BUFFER_MAX_CHARS", 10000))
    # New/sparse users (few stored memory items) extract sooner so their profile builds
    # quickly: when a user's stored memory-item count is below NEW_USER_MEMORY_THRESHOLD,
    # extraction triggers at NEW_USER_EXTRACTION_CHARS instead of CHAT_BUFFER_MAX_CHARS.
    NEW_USER_EXTRACTION_CHARS: int = Field(default_factory=lambda: _env_int("NEW_USER_EXTRACTION_CHARS", 1000))
    NEW_USER_MEMORY_THRESHOLD: int = Field(default_factory=lambda: _env_int("NEW_USER_MEMORY_THRESHOLD", 5))
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
    # Ignore any message older than this many seconds at processing time. On (re)start or
    # catch-up, Telegram can deliver a burst of backlog messages at once; a real-time chat
    # bot must not reply to stale messages, and processing them floods the throttle (every
    # old message is stamped with the *current* time, so they all pile into one rate-limit
    # window and trip it en masse → a wave of "Slow down" warnings at startup). 0 disables.
    STALE_MESSAGE_SECS: float = Field(default_factory=lambda: _env_float("STALE_MESSAGE_SECS", 60.0))
    MAX_QUEUED_MESSAGES: int = Field(default_factory=lambda: _env_int("MAX_QUEUED_MESSAGES", 10))
    MAX_INPUT_CHARS: int = Field(default_factory=lambda: _env_int("MAX_INPUT_CHARS", 2500))
    MAX_RESPONSE_CHARS: int = Field(default_factory=lambda: _env_int("MAX_RESPONSE_CHARS", 2000))

    # --- Group chat / ambient replies ---
    GROUP_AMBIENT_COOLDOWN_SECS: float = Field(default_factory=lambda: _env_float("GROUP_AMBIENT_COOLDOWN_SECS", 90.0))
    GROUP_AMBIENT_BASE_RATE: float = Field(default_factory=lambda: _env_float("GROUP_AMBIENT_BASE_RATE", 0.25))
    GROUP_CONTEXT_SCAN_EVERY: int = Field(default_factory=lambda: _env_int("GROUP_CONTEXT_SCAN_EVERY", 12))
    AFFINITY_DEFAULT: float = Field(default_factory=lambda: _env_float("AFFINITY_DEFAULT", 0.5))

    # --- Group chat / implicit addressing & spam ---
    GROUP_IMPLICIT_RECENCY_SECS: float = Field(default_factory=lambda: _env_float("GROUP_IMPLICIT_RECENCY_SECS", 120.0))
    GROUP_IMPLICIT_RECENCY_MAX_MSGS: int = Field(default_factory=lambda: _env_int("GROUP_IMPLICIT_RECENCY_MAX_MSGS", 4))
    GROUP_IMPLICIT_COOLDOWN_SECS: float = Field(default_factory=lambda: _env_float("GROUP_IMPLICIT_COOLDOWN_SECS", 30.0))
    GROUP_MASS_TAG_SPAM_THRESHOLD: int = Field(default_factory=lambda: _env_int("GROUP_MASS_TAG_SPAM_THRESHOLD", 5))
    GROUP_SPAM_BURST_SIMILARITY: float = Field(default_factory=lambda: _env_float("GROUP_SPAM_BURST_SIMILARITY", 0.85))
    GROUP_SPAM_BURST_COUNT: int = Field(default_factory=lambda: _env_int("GROUP_SPAM_BURST_COUNT", 3))
    GROUP_SPAM_BURST_WINDOW_SECS: float = Field(default_factory=lambda: _env_float("GROUP_SPAM_BURST_WINDOW_SECS", 60.0))
    GROUP_SPAM_BURST_TRACK_MAX: int = Field(default_factory=lambda: _env_int("GROUP_SPAM_BURST_TRACK_MAX", 20))

    # --- Persona / features ---
    PERSONA_FILE: str = Field(default_factory=lambda: _env_str("PERSONA_FILE", "persona.md"))
    # Display name the bot answers to in group chats (standalone, word-boundary match,
    # case-insensitive). Blank -> fall back to the Telegram first name from get_me().
    BOT_NAME: str = Field(default_factory=lambda: _env_str("BOT_NAME", ""))
    ENABLE_MESSAGE_REACTIONS: bool = Field(default_factory=lambda: _env_bool("ENABLE_MESSAGE_REACTIONS", True))

    # --- Observability / ops ---
    ADMIN_USER_IDS: set[int] = Field(default_factory=lambda: _env_int_set("ADMIN_USER_IDS"))
    METRICS_LOG_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("METRICS_LOG_INTERVAL_SECS", 0.0))
    # Persist the metrics registry to MongoDB every N seconds so counters/timers survive a
    # restart or crash. <= 0 disables the periodic flush (startup-load and shutdown-flush
    # still run). Cheap single-document upsert, so on by default.
    METRICS_PERSIST_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("METRICS_PERSIST_INTERVAL_SECS", 300.0))
    LOGS_CHANNEL_ID: int | None = Field(default_factory=lambda: _env_int_or_none("LOGS_CHANNEL_ID"))
    # Early-phase verbose tracing: when True, per-message routing decisions (group
    # addressed/implicit/ambient/spam outcomes, throttling, blocked-user skips) are
    # forwarded to the Logs_Channel so internal behavior can be verified live. Noisy by
    # design — intended for early phase; turn off once behavior is trusted. Requires
    # LOGS_CHANNEL_ID to be set (otherwise it is a no-op).
    FORWARD_DIAGNOSTICS: bool = Field(default_factory=lambda: _env_bool("FORWARD_DIAGNOSTICS", False))

    # --- Configurable commands (trigger name + enabled state per built-in command) ---
    # Resolved once at import; a plain dict {key: (trigger, enabled)}.
    COMMANDS: dict[str, tuple[str, bool]] = Field(default_factory=resolve_command_config)

    # --- Consolidation (Phase 11) ---
    CONSOLIDATION_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("CONSOLIDATION_INTERVAL_SECS", 0.0))
    CONSOLIDATION_SCAN_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("CONSOLIDATION_SCAN_INTERVAL_SECS", 3600.0))
    CONSOLIDATION_MAX_USERS_PER_SCAN: int = Field(default_factory=lambda: _env_int("CONSOLIDATION_MAX_USERS_PER_SCAN", 50))
    CONSOLIDATION_MIN_ITEMS: int = Field(default_factory=lambda: _env_int("CONSOLIDATION_MIN_ITEMS", 8))
    MAX_INSIGHTS: int = Field(default_factory=lambda: _env_int("MAX_INSIGHTS", 5))

    # --- Engagement / mood history (Phase 12) ---
    MAX_MOOD_HISTORY: int = Field(default_factory=lambda: _env_int("MAX_MOOD_HISTORY", 10))

    # --- Proactive check-ins (Phase 12) ---
    PROACTIVE_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("PROACTIVE_INTERVAL_SECS", 0.0))
    PROACTIVE_INACTIVITY_SECS: float = Field(default_factory=lambda: _env_float("PROACTIVE_INACTIVITY_SECS", 172800.0))
    PROACTIVE_MIN_INTERVAL_SECS: float = Field(default_factory=lambda: _env_float("PROACTIVE_MIN_INTERVAL_SECS", 259200.0))
    PROACTIVE_MAX_PER_SCAN: int = Field(default_factory=lambda: _env_int("PROACTIVE_MAX_PER_SCAN", 20))
    PROACTIVE_MIN_ITEMS: int = Field(default_factory=lambda: _env_int("PROACTIVE_MIN_ITEMS", 3))
    # Auto-pause proactive DMs for an unresponsive user: after this many consecutive
    # delivered check-ins with no reply (no chat message or command in between), the user
    # is skipped by the scan until they engage again, which resets the streak. 0 disables
    # the auto-pause (check-ins keep going regardless of silence).
    PROACTIVE_MAX_UNANSWERED: int = Field(default_factory=lambda: _env_int("PROACTIVE_MAX_UNANSWERED", 3))
    PROACTIVE_QUIET_START_HOUR: int = Field(default_factory=lambda: _env_int("PROACTIVE_QUIET_START_HOUR", 22))
    PROACTIVE_QUIET_END_HOUR: int = Field(default_factory=lambda: _env_int("PROACTIVE_QUIET_END_HOUR", 7))

    @property
    def bot_display_name(self) -> str:
        """The bot's user-facing name: ``BOT_NAME`` when set, else ``"ThinkMate"``.

        Single source of truth for the name shown to users (greetings, onboarding,
        admin reports, and the assistant's attribution in group transcripts), so the
        bot can be rebranded entirely by setting ``BOT_NAME`` in the environment.
        """
        return self.BOT_NAME.strip() or "ThinkMate"


if "config" in globals() and globals()["config"].__class__.__name__ == "Config":
    _existing_config = globals()["config"]
    _new_config = Config()
    for _field in Config.model_fields:
        setattr(_existing_config, _field, getattr(_new_config, _field))
    config = _existing_config
else:
    config = Config()
