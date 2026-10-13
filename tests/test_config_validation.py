"""Startup settings must reject values that break runtime resource bounds."""

import pytest
from pydantic import ValidationError

from app.config import _BUILTIN_COMMANDS, Config, resolve_command_config

_LLM_MODEL_FIELDS = (
    "LLM_MODEL",
    "LLM_CHAT_MODEL",
    "LLM_EXTRACTION_MODEL",
    "LLM_CHAT_FALLBACK_MODEL",
    "LLM_EXTRACTION_FALLBACK_MODEL",
)
_LLM_REASONING_FIELDS = (
    "LLM_CHAT_REASONING_EFFORT",
    "LLM_EXTRACTION_REASONING_EFFORT",
    "LLM_CHAT_FALLBACK_REASONING_EFFORT",
    "LLM_EXTRACTION_FALLBACK_REASONING_EFFORT",
)


@pytest.fixture
def clear_llm_model_environment(monkeypatch):
    for field in (*_LLM_MODEL_FIELDS, *_LLM_REASONING_FIELDS):
        monkeypatch.delenv(field, raising=False)


def test_legacy_model_config_remains_shared(clear_llm_model_environment):
    settings = Config(LLM_MODEL="legacy-model")
    assert settings.chat_model == "legacy-model"
    assert settings.extraction_model == "legacy-model"
    assert settings.LLM_CHAT_FALLBACK_MODEL == ""
    assert settings.LLM_EXTRACTION_FALLBACK_MODEL == ""
    assert all(getattr(settings, field) == "" for field in _LLM_REASONING_FIELDS)


def test_chat_and_extraction_models_can_be_set_independently(
    clear_llm_model_environment,
):
    settings = Config(
        LLM_MODEL="legacy-model",
        LLM_CHAT_MODEL="chat-model",
        LLM_EXTRACTION_MODEL="extraction-model",
        LLM_CHAT_FALLBACK_MODEL="chat-backup",
        LLM_EXTRACTION_FALLBACK_MODEL="extraction-backup",
    )
    assert settings.chat_model == "chat-model"
    assert settings.extraction_model == "extraction-model"
    assert settings.LLM_CHAT_FALLBACK_MODEL == "chat-backup"
    assert settings.LLM_EXTRACTION_FALLBACK_MODEL == "extraction-backup"


def test_extraction_defaults_to_explicit_chat_model(clear_llm_model_environment):
    settings = Config(LLM_MODEL="legacy-model", LLM_CHAT_MODEL="chat-model")
    assert settings.extraction_model == "chat-model"


@pytest.mark.parametrize("field", _LLM_MODEL_FIELDS)
def test_model_names_are_trimmed_from_environment(
    monkeypatch, clear_llm_model_environment, field
):
    monkeypatch.setenv(field, "  provider/model-name  ")
    settings = Config()
    assert getattr(settings, field) == "provider/model-name"


def test_whitespace_optional_models_use_the_documented_defaults(
    clear_llm_model_environment,
):
    settings = Config(
        LLM_MODEL="  legacy-model  ",
        LLM_CHAT_MODEL="  ",
        LLM_EXTRACTION_MODEL="\t",
        LLM_CHAT_FALLBACK_MODEL="\n",
        LLM_EXTRACTION_FALLBACK_MODEL="  ",
    )
    assert settings.chat_model == "legacy-model"
    assert settings.extraction_model == "legacy-model"
    assert settings.LLM_CHAT_FALLBACK_MODEL == ""
    assert settings.LLM_EXTRACTION_FALLBACK_MODEL == ""


def test_blank_legacy_model_is_allowed_when_chat_is_explicit(
    clear_llm_model_environment,
):
    settings = Config(LLM_MODEL="  ", LLM_CHAT_MODEL="chat-model")
    assert settings.chat_model == "chat-model"


def test_missing_chat_model_is_rejected(clear_llm_model_environment):
    with pytest.raises(ValidationError, match="LLM_CHAT_MODEL or LLM_MODEL"):
        Config(LLM_MODEL="  ", LLM_CHAT_MODEL=" ", LLM_EXTRACTION_MODEL="extraction")


@pytest.mark.parametrize("field", _LLM_REASONING_FIELDS)
@pytest.mark.parametrize(
    "effort", ["", "none", "minimal", "low", "medium", "high", "xhigh", "max"]
)
def test_reasoning_efforts_are_normalized_and_accepted(
    monkeypatch, clear_llm_model_environment, field, effort
):
    monkeypatch.setenv(field, f"  {effort.upper()}  ")
    assert getattr(Config(), field) == effort


@pytest.mark.parametrize("field", _LLM_REASONING_FIELDS)
def test_unknown_reasoning_efforts_are_rejected_at_startup(
    monkeypatch, clear_llm_model_environment, field
):
    monkeypatch.setenv(field, "extreme")
    with pytest.raises(ValidationError, match=field):
        Config()


def test_reasoning_efforts_do_not_inherit_between_models(clear_llm_model_environment):
    settings = Config(
        LLM_CHAT_REASONING_EFFORT="high",
        LLM_EXTRACTION_REASONING_EFFORT="low",
        LLM_CHAT_FALLBACK_MODEL="chat-backup",
        LLM_EXTRACTION_FALLBACK_MODEL="extraction-backup",
    )
    assert settings.LLM_CHAT_REASONING_EFFORT == "high"
    assert settings.LLM_EXTRACTION_REASONING_EFFORT == "low"
    assert settings.LLM_CHAT_FALLBACK_REASONING_EFFORT == ""
    assert settings.LLM_EXTRACTION_FALLBACK_REASONING_EFFORT == ""


@pytest.mark.parametrize(
    "field",
    [name for name, field in Config.model_fields.items() if field.annotation is float],
)
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_environment_values_are_rejected(monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match=field):
        Config()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("LLM_MAX_RETRIES", "-1"),
        ("LLM_REQUEST_TIMEOUT_SECS", "0"),
        ("RATE_LIMIT_MAX_REQUESTS", "0"),
        ("RATE_LIMIT_WINDOW_SECS", "0"),
        ("CHAT_BUFFER_HARD_CAP", "0"),
        ("CHAT_BUFFER_TRIM", "-1"),
        ("MAX_QUEUED_MESSAGES", "-1"),
        ("MAX_MOOD_HISTORY", "0"),
        ("GROUP_AMBIENT_BASE_RATE", "1.1"),
        ("AFFINITY_DEFAULT", "-0.1"),
        ("GROUP_SPAM_BURST_SIMILARITY", "1.1"),
        ("PROACTIVE_QUIET_START_HOUR", "24"),
        ("PROACTIVE_QUIET_END_HOUR", "-1"),
    ],
)
def test_invalid_environment_bounds_are_rejected(monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError, match=field):
        Config()


@pytest.mark.parametrize(
    "settings",
    [
        {"CHAT_BUFFER_TRIM": 8, "CHAT_BUFFER_HARD_CAP": 8},
        {"GROUP_SPAM_BURST_COUNT": 4, "GROUP_SPAM_BURST_TRACK_MAX": 3},
    ],
)
def test_unreachable_memory_windows_are_rejected(settings):
    with pytest.raises(ValidationError):
        Config(**settings)


def test_documented_disable_values_are_preserved():
    settings = Config(
        LLM_MAX_RETRIES=0,
        NEW_USER_MEMORY_THRESHOLD=0,
        STALE_MESSAGE_SECS=-1,
        MAX_RESPONSE_CHARS=0,
        GROUP_CONTEXT_SCAN_EVERY=0,
        METRICS_LOG_INTERVAL_SECS=-1,
        METRICS_PERSIST_INTERVAL_SECS=0,
        CONSOLIDATION_INTERVAL_SECS=0,
        PROACTIVE_INTERVAL_SECS=-1,
        PROACTIVE_MAX_UNANSWERED=0,
    )
    assert settings.PROACTIVE_MAX_UNANSWERED == 0
    assert settings.PROACTIVE_INTERVAL_SECS == -1


@pytest.mark.parametrize("scan_interval", [0.0, -1.0])
def test_consolidation_scan_interval_is_validated_only_when_enabled(scan_interval):
    settings = Config(
        CONSOLIDATION_INTERVAL_SECS=0,
        CONSOLIDATION_SCAN_INTERVAL_SECS=scan_interval,
    )
    assert settings.CONSOLIDATION_SCAN_INTERVAL_SECS == scan_interval
    with pytest.raises(ValidationError, match="CONSOLIDATION_SCAN_INTERVAL_SECS"):
        Config(
            CONSOLIDATION_INTERVAL_SECS=60,
            CONSOLIDATION_SCAN_INTERVAL_SECS=scan_interval,
        )


def _clear_command_environment(monkeypatch):
    for key in _BUILTIN_COMMANDS:
        monkeypatch.delenv(f"CMD_{key.upper()}_NAME", raising=False)
        monkeypatch.delenv(f"CMD_{key.upper()}_ENABLED", raising=False)


def test_command_names_are_lowercase_for_telegram_publication(monkeypatch):
    _clear_command_environment(monkeypatch)
    monkeypatch.setenv("CMD_START_NAME", " /Hello_2 ")
    assert resolve_command_config()["start"] == ("hello_2", True)


def test_command_fallback_resolves_cascading_collisions(monkeypatch):
    _clear_command_environment(monkeypatch)
    monkeypatch.setenv("CMD_START_NAME", "hello")
    monkeypatch.setenv("CMD_HELP_NAME", "HELLO")
    monkeypatch.setenv("CMD_PROFILE_NAME", "start")
    monkeypatch.setenv("CMD_RESET_NAME", "profile")
    resolved = resolve_command_config()
    enabled_names = [name for name, enabled in resolved.values() if enabled]
    assert len(enabled_names) == len(set(enabled_names))
    assert resolved["start"] == ("start", True)
    assert resolved["profile"] == ("profile", True)
    assert resolved["reset"] == ("reset", True)


def test_disabled_command_does_not_reset_an_enabled_override(monkeypatch):
    _clear_command_environment(monkeypatch)
    monkeypatch.setenv("CMD_START_NAME", "help")
    monkeypatch.setenv("CMD_HELP_ENABLED", "false")
    assert resolve_command_config()["start"] == ("help", True)
