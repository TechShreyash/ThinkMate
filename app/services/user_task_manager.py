"""Per-conversation concurrency, batching, queues, and typing indicators.

Coalesces rapid-fire messages into batches, serializes response generation per conversation
(``chat_lock``) and background memory work (``memory_lock``), and bounds in-memory state
by evicting idle conversations — important at 50k+ users on a single instance.

State is keyed by ``chat_id``. In a DM ``chat_id == user_id`` (and ``sender_id == chat_id``),
so per-conversation batching collapses to exactly the original per-user behavior; for groups
a single conversation batches per chat regardless of how many members are speaking.
"""
import asyncio
import time
from loguru import logger
from aiogram import Bot
from aiogram.types import Message, ReactionTypeEmoji
from app.config import config
from app.database.connection import db_session
from app.services.chat_manager import handle_message

# Telegram chat.type values that map to the multi-party group path.
_GROUP_CHAT_TYPES = ("group", "supergroup")
_VALID_CHAT_TYPES = ("private", "group", "supergroup", "channel")


class UserState:
    """In-memory batching/coalescing state for a single conversation (keyed by chat_id)."""

    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.chat_lock = asyncio.Lock()
        self.memory_lock = asyncio.Lock()
        # Each entry: {"text", "message", "user_id", "sender_name", "reason"}.
        self.pending_messages: list[dict] = []
        self.batch_task: asyncio.Task | None = None
        self.typing_task: asyncio.Task | None = None
        self.typing_stop_event = asyncio.Event()
        self.first_message_time: float | None = None
        self.last_active: float = time.time()
        self.last_compression_time: float = 0.0

    def is_idle(self) -> bool:
        """True when nothing is queued, running, or locked for this conversation."""
        return (
            not self.pending_messages
            and not self.chat_lock.locked()
            and not self.memory_lock.locked()
            and (self.batch_task is None or self.batch_task.done())
            and (self.typing_task is None or self.typing_task.done())
        )


class UserTaskManager:
    def __init__(self):
        self._states: dict[int, UserState] = {}
        self._states_lock = asyncio.Lock()
        self._sweeper: asyncio.Task | None = None

    async def get_state(self, chat_id: int) -> UserState:
        async with self._states_lock:
            state = self._states.get(chat_id)
            if state is None:
                state = UserState(chat_id)
                self._states[chat_id] = state
            return state

    def _ensure_sweeper(self):
        """Lazily start the idle-state eviction loop on the running event loop."""
        if self._sweeper is None or self._sweeper.done():
            try:
                self._sweeper = asyncio.get_running_loop().create_task(self._sweep_loop())
            except RuntimeError:
                pass

    async def _sweep_loop(self):
        interval = max(60.0, config.USER_STATE_TTL_SECS / 2)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._evict_idle()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"State eviction sweep error: {e}")

    async def _evict_idle(self):
        now = time.time()
        async with self._states_lock:
            stale = [
                cid for cid, st in self._states.items()
                if st.is_idle() and now - st.last_active > config.USER_STATE_TTL_SECS
            ]
            for cid in stale:
                del self._states[cid]
        if stale:
            logger.debug(f"Evicted {len(stale)} idle conversation states (now {len(self._states)} active).")

    async def enqueue_message(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        message: Message,
        *,
        user_id: int | None = None,
        chat_type: str = "private",
        sender_name: str = "",
        reason: str = "reply",
    ):
        """Queue a message for batched processing, keyed by ``chat_id``.

        New parameters are keyword-only with DM-safe defaults so the existing positional
        call ``enqueue_message(bot, message.from_user.id, text, message)`` keeps working:
        in a DM ``message.chat.id == message.from_user.id``, so passing ``from_user.id`` as
        the (now) ``chat_id`` argument yields identical per-user batching. ``user_id``
        defaults to ``chat_id`` (the lone DM speaker); ``reason="ambient"`` selects the
        chime-in path downstream.
        """
        # In a DM the only speaker is the user, whose id equals the chat id.
        if user_id is None:
            user_id = chat_id

        self._ensure_sweeper()
        state = await self.get_state(chat_id)
        state.last_active = time.time()
        current_time = state.last_active

        if len(state.pending_messages) >= config.MAX_QUEUED_MESSAGES:
            logger.warning(f"Chat {chat_id} queue at limit ({config.MAX_QUEUED_MESSAGES}); dropping message.")
            return

        state.pending_messages.append({
            "text": text,
            "message": message,
            "user_id": user_id,
            "sender_name": sender_name,
            "reason": reason,
        })

        # Start the typing indicator if not already running.
        if not state.typing_task or state.typing_task.done():
            state.typing_stop_event.clear()
            state.typing_task = asyncio.create_task(
                self._typing_loop(bot, message.chat.id, state.typing_stop_event)
            )

        if state.first_message_time is None:
            state.first_message_time = current_time

        # Reset the coalescing timer, but never postpone past the hard deadline.
        if state.batch_task and not state.batch_task.done():
            state.batch_task.cancel()
        if current_time - state.first_message_time >= config.MAX_BATCH_DELAY_SECS:
            state.batch_task = asyncio.create_task(self._process_batch(chat_id))
        else:
            state.batch_task = asyncio.create_task(self._delayed_process_batch(chat_id))

    async def _typing_loop(self, bot: Bot, chat_id: int, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Failed to send typing action: {e}")
            try:
                await asyncio.sleep(4)
            except asyncio.CancelledError:
                break

    async def _delayed_process_batch(self, chat_id: int):
        try:
            await asyncio.sleep(config.MESSAGE_BATCH_DELAY_SECS)
            await self._process_batch(chat_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"Delayed batch error for chat {chat_id}: {e}")

    @staticmethod
    def _resolve_chat_type(message: Message) -> str:
        """Derive chat_type from the aiogram message, defaulting to ``private``.

        Robust against non-string values (e.g. test mocks): only a recognized Telegram
        chat-type string is honored; anything else falls back to ``private``.
        """
        chat_type = getattr(getattr(message, "chat", None), "type", None)
        if isinstance(chat_type, str) and chat_type in _VALID_CHAT_TYPES:
            return chat_type
        return "private"

    async def _process_batch(self, chat_id: int):
        state = await self.get_state(chat_id)

        async with state.chat_lock:
            if not state.pending_messages:
                self._stop_typing(state)
                state.first_message_time = None
                return

            batch = list(state.pending_messages)
            state.pending_messages.clear()
            state.first_message_time = None
            state.last_active = time.time()

            logger.info(f"Processing batch of {len(batch)} messages for chat {chat_id}")
            combined_text = "\n".join(m["text"] for m in batch)

            # Use the last message's metadata for the chat-context that handle_message needs.
            last = batch[-1]
            last_message = last["message"]
            last_user_id = last["user_id"]
            last_sender_name = last["sender_name"]
            last_reason = last["reason"]
            chat_type = self._resolve_chat_type(last_message)

            try:
                async with db_session() as db:
                    reply_text, reaction = await handle_message(
                        db,
                        chat_id,
                        combined_text,
                        chat_type=chat_type,
                        sender_id=last_user_id,
                        sender_name=last_sender_name,
                        reason=last_reason,
                    )

                if reaction:
                    try:
                        await last_message.react(reaction=[ReactionTypeEmoji(emoji=reaction)])
                    except Exception as react_err:  # noqa: BLE001
                        logger.warning(f"Failed to send reaction {reaction!r}: {react_err}")

                await last_message.answer(reply_text)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error processing batch for chat {chat_id}: {e}")
                await last_message.answer("Sorry, I ran into a problem just now — mind trying again?")
            finally:
                if not state.pending_messages:
                    self._stop_typing(state)

    @staticmethod
    def _stop_typing(state: UserState):
        state.typing_stop_event.set()
        if state.typing_task and not state.typing_task.done():
            state.typing_task.cancel()

    async def run_extractor(self, chat_id: int):
        """Run memory extraction in the background; at most one per conversation (shared memory_lock).

        Operates per buffer/profile id (``chat_id``; in a DM this equals the user id). Group
        extraction routing/branching is handled in task 6.1; here we only keep the id plumbing
        consistent.
        """
        state = await self.get_state(chat_id)
        if state.memory_lock.locked():
            logger.info(f"Memory lock held for chat {chat_id}; skipping extractor.")
            return
        async with state.memory_lock:
            from app.services.memory_extractor import extract_and_trim
            await extract_and_trim(chat_id)

    async def run_compressor(self, chat_id: int):
        """Run memory compression in the background; rate-limited and serialized per conversation."""
        state = await self.get_state(chat_id)
        if time.time() - state.last_compression_time < config.COMPRESSION_COOLDOWN_SECS:
            logger.debug(f"Compression cooldown active for chat {chat_id}; skipping.")
            return
        if state.memory_lock.locked():
            logger.info(f"Memory lock held for chat {chat_id}; skipping compressor.")
            return
        async with state.memory_lock:
            from app.services.memory_compressor import compress_user_memory
            await compress_user_memory(chat_id)
        state.last_compression_time = time.time()


user_task_manager = UserTaskManager()
