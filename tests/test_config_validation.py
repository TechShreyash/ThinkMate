"""Startup settings must reject values that break runtime resource bounds."""

import pytest
from pydantic import ValidationError

from app.config import _BUILTIN_COMMANDS, Config, resolve_command_config


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
