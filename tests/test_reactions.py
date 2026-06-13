import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from app.config import config
from app.database import connection
from app.services.llm_service import LLMService
from app.services.reactions import normalize_reaction
from app.services.user_task_manager import user_task_manager


@pytest_asyncio.fixture
async def temp_db():
    await connection.init_db()
    yield


# --- Reaction normalization (pure unit) ---
def test_normalize_reaction_passthrough():
    assert normalize_reaction("🔥") == "🔥"


def test_normalize_reaction_strips_variation_selector():
    # Model returns the FE0F-decorated heart; Telegram wants the bare code point.
    assert normalize_reaction("❤️") == "❤"


def test_normalize_reaction_rejects_unknown():
    assert normalize_reaction("🦖") is None
    assert normalize_reaction("None") is None
    assert normalize_reaction("") is None
    assert normalize_reaction(None) is None


# --- Combined reply+reaction call ---
@pytest.mark.asyncio
async def test_generate_reply_bundle_parses_json():
    llm = LLMService()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"reply": "yay, congrats!", "reaction": "🎉"}'
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    original = config.ENABLE_MESSAGE_REACTIONS
    config.ENABLE_MESSAGE_REACTIONS = True
    try:
        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create, \
             patch.object(llm, "_fire_log"):
            mock_create.return_value = mock_response
            reply, reaction = await llm.generate_reply_bundle(123, "sys", [{"role": "user", "content": "hi"}])
            assert reply == "yay, congrats!"
            assert reaction == "🎉"
    finally:
        config.ENABLE_MESSAGE_REACTIONS = original


@pytest.mark.asyncio
async def test_generate_reply_bundle_keeps_reply_emojis():
    """Emojis in the reply text are preserved; the reaction is an independent channel."""
    llm = LLMService()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"reply": "haha that is wild 😂 tell me more", "reaction": "😁"}'
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    original = config.ENABLE_MESSAGE_REACTIONS
    config.ENABLE_MESSAGE_REACTIONS = True
    try:
        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create, \
             patch.object(llm, "_fire_log"):
            mock_create.return_value = mock_response
            reply, reaction = await llm.generate_reply_bundle(123, "sys", [{"role": "user", "content": "hi"}])
            assert reply == "haha that is wild 😂 tell me more"  # reply emoji kept
            assert reaction == "😁"                              # independent reaction
    finally:
        config.ENABLE_MESSAGE_REACTIONS = original


@pytest.mark.asyncio
async def test_generate_reply_bundle_falls_back_to_plain_text():
    llm = LLMService()
    mock_choice = MagicMock()
    mock_choice.message.content = "just plain text, not json"
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create, \
         patch.object(llm, "_fire_log"):
        mock_create.return_value = mock_response
        reply, reaction = await llm.generate_reply_bundle(123, "sys", [{"role": "user", "content": "hi"}])
        assert reply == "just plain text, not json"
        assert reaction is None


@pytest.mark.asyncio
async def test_generate_reply_bundle_reactions_disabled_yields_none():
    llm = LLMService()
    mock_choice = MagicMock()
    mock_choice.message.content = '{"reply": "hello there", "reaction": "🔥"}'
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    original = config.ENABLE_MESSAGE_REACTIONS
    config.ENABLE_MESSAGE_REACTIONS = False
    try:
        with patch.object(llm.client.chat.completions, "create", new_callable=AsyncMock) as mock_create, \
             patch.object(llm, "_fire_log"):
            mock_create.return_value = mock_response
            reply, reaction = await llm.generate_reply_bundle(123, "sys", [{"role": "user", "content": "hi"}])
            assert reply == "hello there"
            assert reaction is None
    finally:
        config.ENABLE_MESSAGE_REACTIONS = original


# --- Batch processing applies the reaction returned by handle_message ---
@pytest.mark.asyncio
async def test_process_batch_applies_reaction(temp_db):
    user_id = 11111
    mock_bot = MagicMock()
    mock_message = MagicMock()
    mock_message.chat.id = 123
    mock_message.react = AsyncMock()
    mock_message.answer = AsyncMock()

    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.1
    try:
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = ("Mocked Response", "🎉")
            await user_task_manager.enqueue_message(mock_bot, user_id, "Hurray!", mock_message)
            await asyncio.sleep(0.25)

            mock_message.react.assert_called_once()
            assert mock_message.react.call_args[1]["reaction"][0].emoji == "🎉"
            mock_message.answer.assert_called_once_with("Mocked Response")
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay


@pytest.mark.asyncio
async def test_process_batch_no_reaction(temp_db):
    user_id = 22222
    mock_bot = MagicMock()
    mock_message = MagicMock()
    mock_message.chat.id = 123
    mock_message.react = AsyncMock()
    mock_message.answer = AsyncMock()

    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.1
    try:
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = ("Mocked Response", None)
            await user_task_manager.enqueue_message(mock_bot, user_id, "Hi", mock_message)
            await asyncio.sleep(0.25)

            mock_message.react.assert_not_called()
            mock_message.answer.assert_called_once_with("Mocked Response")
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay


@pytest.mark.asyncio
async def test_process_batch_reaction_failure_still_answers(temp_db):
    user_id = 33333
    mock_bot = MagicMock()
    mock_message = MagicMock()
    mock_message.chat.id = 123
    mock_message.react = AsyncMock(side_effect=Exception("Failed to react"))
    mock_message.answer = AsyncMock()

    original_delay = config.MESSAGE_BATCH_DELAY_SECS
    config.MESSAGE_BATCH_DELAY_SECS = 0.1
    try:
        with patch("app.services.user_task_manager.handle_message", new_callable=AsyncMock) as mock_handle:
            mock_handle.return_value = ("Mocked Response", "🔥")
            await user_task_manager.enqueue_message(mock_bot, user_id, "Fire!", mock_message)
            await asyncio.sleep(0.25)

            mock_message.react.assert_called_once()
            mock_message.answer.assert_called_once_with("Mocked Response")
    finally:
        config.MESSAGE_BATCH_DELAY_SECS = original_delay
