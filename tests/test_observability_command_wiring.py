"""Task 13.2 — Log_Forwarder, Error_Log_Sink, and command-config/registry wiring tests.

These are behavior-at-the-seams tests (not Telegram delivery): with a mock bot we assert
the forwarder/sink dispatch contract and the command config/registry wiring.

* Log_Forwarder: forwards to ``LOGS_CHANNEL_ID``; a send failure is swallowed; an event
  whose SOURCE chat is the logs channel is dropped (Req 4.8, 4.10).
* Error_Log_Sink: registered exactly as ``main.py`` does (``level="WARNING"`` + a
  ``no_forward`` filter, ``enqueue=False``) with a loop double whose
  ``call_soon_threadsafe`` runs the callback inline — a WARNING+ record yields exactly one
  ``bot.send_message``, a sub-WARNING and a ``no_forward``-bound record yield none, and a
  raising ``bot.send_message`` never propagates into the logging call (Req 4.5, 4.6, 4.7,
  4.9).
* Command config: ``resolve_command_config`` env cases — unset → all-defaults/all-enabled;
  ``CMD_REACTIONS_ENABLED=false`` disables ``reactions``; ``CMD_START_NAME=hello`` remaps ``start``;
  invalid and duplicate triggers fall back to defaults (Req 7.1, 7.2, 7.3, 7.4, 7.5, 7.7).
* Command registry: ``register_commands`` against a fresh ``Router`` binds exactly the
  enabled commands under their resolved triggers and leaves disabled commands unmatched;
  ``cmd_health`` / ``cmd_metrics`` stay admin-gated after a rename (Req 7.3, 7.4, 7.6).
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Router
from aiogram.filters.command import Command
from loguru import logger

from app.config import config, resolve_command_config, _BUILTIN_COMMANDS
from app.handlers import commands as commands_module
from app.services import log_forwarder
from app.services.error_log_sink import make_error_log_sink


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _registered_triggers(router: Router) -> dict[str, object]:
    """Map every registered ``/trigger`` to its bound handler callback for a router."""
    out: dict[str, object] = {}
    for ho in router.message.handlers:
        for f in ho.filters:
            cmd = getattr(f, "callback", None)
            cmds = getattr(cmd, "commands", None)
            if isinstance(cmd, Command) and cmds:
                for c in cmds:
                    out[c] = ho.callback
    return out


class _InlineLoop:
    """Loop double whose ``call_soon_threadsafe`` runs the callback inline."""

    def call_soon_threadsafe(self, callback, *args):
        callback(*args)


@pytest.fixture
def clean_cmd_env():
    """Remove all CMD_* env vars for the run, restoring them afterward."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("CMD_")}
    for k in saved:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k in list(os.environ):
            if k.startswith("CMD_"):
                os.environ.pop(k, None)
        os.environ.update(saved)


# --------------------------------------------------------------------------- #
# 1. Log_Forwarder dispatch contract (Req 4.8, 4.10)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_log_forwarder_sends_to_logs_channel():
    """A normal event is forwarded to ``LOGS_CHANNEL_ID`` via the given bot."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await log_forwarder.send(bot, source_chat_id=-100555, text="identity captured: Bob")
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == config.LOGS_CHANNEL_ID
    assert bot.send_message.await_args.kwargs["text"] == "identity captured: Bob"


@pytest.mark.asyncio
async def test_log_forwarder_swallows_send_failure():
    """A transport that raises must not propagate out of ``send`` (Req 4.8)."""
    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("network down"))
    # Must not raise.
    await log_forwarder.send(bot, source_chat_id=-100555, text="extraction-saved")


@pytest.mark.asyncio
async def test_log_forwarder_drops_events_from_logs_channel():
    """An event whose SOURCE chat is the logs channel is never forwarded (Req 4.10)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    await log_forwarder.send(
        bot, source_chat_id=config.LOGS_CHANNEL_ID, text="should be dropped"
    )
    bot.send_message.assert_not_called()


# --------------------------------------------------------------------------- #
# 2. Error_Log_Sink dispatch (Req 4.5, 4.6, 4.7, 4.9)
# --------------------------------------------------------------------------- #
def _add_sink(bot):
    """Register the sink exactly as ``main.py`` does; return the loguru sink id."""
    return logger.add(
        make_error_log_sink(bot, _InlineLoop()),
        level="WARNING",
        filter=lambda r: not r["extra"].get("no_forward"),
        enqueue=False,
    )


@pytest.mark.asyncio
async def test_error_log_sink_forwards_warning_and_above():
    """A WARNING (and an ERROR) record yields exactly one ``bot.send_message`` each."""
    import asyncio

    bot = MagicMock()
    bot.send_message = AsyncMock()
    sink_id = _add_sink(bot)
    try:
        logger.warning("a warning that should forward")
        logger.error("an error that should forward")
        await asyncio.sleep(0)  # let the scheduled send tasks run
    finally:
        logger.remove(sink_id)

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_error_log_sink_ignores_sub_warning_and_no_forward():
    """Sub-WARNING records and ``no_forward``-bound records produce no forward (Req 4.9)."""
    import asyncio

    bot = MagicMock()
    bot.send_message = AsyncMock()
    sink_id = _add_sink(bot)
    try:
        logger.info("an info line — below threshold")
        logger.debug("a debug line — below threshold")
        logger.bind(no_forward=True).error("the forwarder's own error must not loop")
        await asyncio.sleep(0)
    finally:
        logger.remove(sink_id)

    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_error_log_sink_swallows_send_failure():
    """A raising ``bot.send_message`` never propagates into the logging call (Req 4.7)."""
    import asyncio

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    sink_id = _add_sink(bot)
    try:
        # The logging call itself must return normally despite the failing transport.
        logger.warning("this will fail to forward but must not raise")
        await asyncio.sleep(0)
    finally:
        logger.remove(sink_id)

    bot.send_message.assert_awaited_once()


# --------------------------------------------------------------------------- #
# 3. resolve_command_config env cases (Req 7.1, 7.2, 7.3, 7.4, 7.5, 7.7)
# --------------------------------------------------------------------------- #
def test_command_config_unset_is_all_defaults_enabled(clean_cmd_env):
    """With no CMD_* env, every command resolves to its key, enabled (Req 7.1, 7.2)."""
    resolved = resolve_command_config()
    assert set(resolved) == set(_BUILTIN_COMMANDS)
    for key in _BUILTIN_COMMANDS:
        assert resolved[key] == (key, True)


def test_command_config_disable_reactions(clean_cmd_env):
    """``CMD_REACTIONS_ENABLED=false`` disables reactions; others stay default-enabled (Req 7.3)."""
    os.environ["CMD_REACTIONS_ENABLED"] = "false"
    resolved = resolve_command_config()
    assert resolved["reactions"] == ("reactions", False)
    assert resolved["start"] == ("start", True)


def test_command_config_rename_start(clean_cmd_env):
    """``CMD_START_NAME=hello`` remaps the start trigger, still enabled (Req 7.4)."""
    os.environ["CMD_START_NAME"] = "hello"
    resolved = resolve_command_config()
    assert resolved["start"] == ("hello", True)


def test_command_config_invalid_trigger_falls_back(clean_cmd_env):
    """An invalid trigger (spaces/slash) falls back to the default key (Req 7.5)."""
    os.environ["CMD_START_NAME"] = " bad/name"
    resolved = resolve_command_config()
    assert resolved["start"] == ("start", True)


def test_command_config_duplicate_trigger_both_fall_back(clean_cmd_env):
    """A trigger duplicating another enabled command's trigger falls back for BOTH (Req 7.5)."""
    os.environ["CMD_PROFILE_NAME"] = "reset"  # collides with reset's default trigger
    resolved = resolve_command_config()
    assert resolved["profile"] == ("profile", True)
    assert resolved["reset"] == ("reset", True)
    # No duplicate triggers remain among enabled commands.
    enabled_triggers = [t for t, en in resolved.values() if en]
    assert len(enabled_triggers) == len(set(enabled_triggers))


def test_command_config_never_raises_on_bad_env(clean_cmd_env):
    """A malformed env that breaks parsing yields the all-defaults mapping (Req 7.7)."""
    # Force the internal parse to blow up; the wrapper must catch and return defaults.
    with patch("app.config._env_str", side_effect=RuntimeError("boom")):
        resolved = resolve_command_config()
    assert resolved == {key: (key, True) for key in _BUILTIN_COMMANDS}


# --------------------------------------------------------------------------- #
# 4. register_commands wiring (Req 7.3, 7.4, 7.6)
# --------------------------------------------------------------------------- #
def test_register_commands_binds_enabled_only_under_resolved_triggers():
    """Exactly the enabled commands are bound under their triggers; disabled are absent."""
    resolved = {key: (key, True) for key in _BUILTIN_COMMANDS}
    resolved["reactions"] = ("reactions", False)  # disabled
    resolved["start"] = ("hello", True)           # renamed

    router = Router(name="test")
    with patch.object(config, "COMMANDS", resolved):
        commands_module.register_commands(router)

    triggers = _registered_triggers(router)
    # Renamed trigger is bound; the original key is not.
    assert "hello" in triggers
    assert "start" not in triggers
    # Disabled command leaves no trigger at all.
    assert "reactions" not in triggers
    # Every other enabled command is bound under its default key.
    for key in _BUILTIN_COMMANDS:
        if key in ("reactions", "start"):
            continue
        assert key in triggers


def test_renamed_admin_commands_stay_admin_gated():
    """A renamed ``health``/``metrics`` keeps its in-handler Admin_Gate (Req 7.6)."""
    resolved = {key: (key, True) for key in _BUILTIN_COMMANDS}
    resolved["health"] = ("status", True)
    resolved["metrics"] = ("stats", True)

    router = Router(name="test")
    with patch.object(config, "COMMANDS", resolved):
        commands_module.register_commands(router)

    triggers = _registered_triggers(router)
    # The configured triggers map back to the original, still-admin-gated handlers.
    assert triggers["status"] is commands_module.cmd_health
    assert triggers["stats"] is commands_module.cmd_metrics


@pytest.mark.asyncio
async def test_renamed_health_handler_still_fails_closed_for_non_admin():
    """Calling the renamed health handler as a non-admin still produces no report (Req 7.6)."""
    message = MagicMock()
    message.from_user = MagicMock()
    message.from_user.id = 999
    message.chat.type = "group"  # not a DM, and not in ADMIN_USER_IDS
    message.answer = AsyncMock()
    db = MagicMock()

    with patch.object(config, "ADMIN_USER_IDS", {12345}):
        await commands_module.cmd_health(message, db)

    message.answer.assert_not_called()
