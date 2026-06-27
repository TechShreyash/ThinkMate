"""Configuration smoke tests for the implicit-addressing & spam knobs — Task 1.2.

Asserts the eight new group knobs exist on ``config`` with their documented
default values, and that each is read live from the environment (override via
env + module reload → the new value is reflected; unset → the documented
default is applied, covering Req 10.12).

Covers Requirements 4.2, 4.3, 9.7, 10.11, 10.12.
"""
import importlib
import os

from app.config import config

# (attribute name, documented default, caster, sample env-override value)
_KNOBS = [
    ("GROUP_IMPLICIT_RECENCY_SECS", 120.0, float, "200.5"),
    ("GROUP_IMPLICIT_RECENCY_MAX_MSGS", 5, int, "7"),
    ("GROUP_IMPLICIT_COOLDOWN_SECS", 15.0, float, "45.0"),
    ("GROUP_MASS_TAG_SPAM_THRESHOLD", 5, int, "8"),
    ("GROUP_SPAM_BURST_SIMILARITY", 0.85, float, "0.9"),
    ("GROUP_SPAM_BURST_COUNT", 3, int, "6"),
    ("GROUP_SPAM_BURST_WINDOW_SECS", 60.0, float, "90.0"),
    ("GROUP_SPAM_BURST_TRACK_MAX", 20, int, "50"),
]


def test_all_knobs_exist_with_documented_defaults():
    """All eight knobs exist on the live config with their documented defaults.

    This assumes the surrounding environment does not set these variables
    (the default, unset case — Req 10.12 default coverage).
    """
    for name, default, caster, _override in _KNOBS:
        assert hasattr(config, name), f"missing config knob: {name}"
        value = getattr(config, name)
        assert isinstance(value, caster), f"{name} should be {caster.__name__}"
        assert value == default, f"{name} default should be {default}, got {value}"


def test_knobs_read_live_from_environment():
    """Each knob is env-overridable and falls back to its default when unset.

    Reloading the config module with the env var set proves the value is read
    live via the ``Field(default_factory=...)`` pattern; reloading again with
    the var removed proves the documented default is applied (Req 10.12).
    """
    import app.config as config_module

    for name, default, caster, override in _KNOBS:
        had_var = name in os.environ
        prior = os.environ.get(name)
        try:
            # Override → reloaded config reflects the new value (read live).
            os.environ[name] = override
            reloaded = importlib.reload(config_module)
            assert getattr(reloaded.config, name) == caster(override), (
                f"{name} did not reflect env override {override!r}"
            )

            # Unset → reloaded config falls back to the documented default.
            del os.environ[name]
            reloaded = importlib.reload(config_module)
            assert getattr(reloaded.config, name) == default, (
                f"{name} did not fall back to default {default}"
            )
        finally:
            if had_var:
                os.environ[name] = prior  # type: ignore[assignment]
            else:
                os.environ.pop(name, None)

    # Restore the module's module-level ``config`` to a clean reload so other
    # tests importing ``app.config.config`` see a pristine instance.
    importlib.reload(config_module)
