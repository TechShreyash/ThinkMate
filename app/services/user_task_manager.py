"""Per-user concurrency, batching, queues, and typing indicators.

Coalesces rapid-fire messages into batches, serializes response generation per user
(``chat_lock``) and background memory work (``memory_lock``), and bounds in-memory state
by evicting idle users — important at 50k+ users on a single instance.
"""
import asyncio
import time
from loguru import logger
from aiogram import Bot
from aiogram.types import Message, ReactionTypeEmoji
from app.config import config
from app.database.connection import db_session
from app.services.chat_manager import handle_message


class UserState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.chat_lock = asyncio.Lock()
        self.memory_lock = asyncio.Lock()
        self.pending_messages: list[dict] = []  # {"text": str, "message": Message}
        self.batch_task: asyncio.Task | None = None
        self.typing_task: asyncio.Task | None = None
        self.typing_stop_event = asyncio.Event()
        self.first_message_time: float | None = None
        self.last_active: float = time.time()
        self.last_compression_time: float = 0.0

    def is_idle(self) -> bool:
        """True when nothing is queued, running, or locked for this user."""
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

    async def get_state(self, user_id: int) -> UserState:
        async with self._states_lock:
            state = self._states.get(user_id)
            if state is None:
                state = UserState(user_id)
                self._states[user_id] = state
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
                uid for uid, st in self._states.items()
                if st.is_idle() and now - st.last_active > config.USER_STATE_TTL_SECS
            ]
            for uid in stale:
                del self._states[uid]
        if stale:
            logger.debug(f"Evicted {len(stale)} idle user states (now {len(self._states)} active).")

    async def enqueue_message(self, bot: Bot, user_id: int, text: str, message: Message):
        self._ensure_sweeper()
        state = await self.get_state(user_id)
        state.last_active = time.time()
        current_time = state.last_active

        if len(state.pending_messages) >= config.MAX_QUEUED_MESSAGES:
            logger.warning(f"User {user_id} queue at limit ({config.MAX_QUEUED_MESSAGES}); dropping message.")
            return

        state.pending_messages.append({"text": text, "message": message})

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
            state.batch_task = asyncio.create_task(self._process_batch(user_id))
        else:
            state.batch_task = asyncio.create_task(self._delayed_process_batch(user_id))

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

    async def _delayed_process_batch(self, user_id: int):
        try:
            await asyncio.sleep(config.MESSAGE_BATCH_DELAY_SECS)
            await self._process_batch(user_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logger.error(f"Delayed batch error for user {user_id}: {e}")

    async def _process_batch(self, user_id: int):
        state = await self.get_state(user_id)

        async with state.chat_lock:
            if not state.pending_messages:
                self._stop_typing(state)
                state.first_message_time = None
                return

            batch = list(state.pending_messages)
            state.pending_messages.clear()
            state.first_message_time = None
            state.last_active = time.time()

            logger.info(f"Processing batch of {len(batch)} messages for user {user_id}")
            combined_text = "\n".join(m["text"] for m in batch)
            last_message = batch[-1]["message"]

            try:
                async with db_session() as db:
                    reply_text, reaction = await handle_message(db, user_id, combined_text)

                if reaction:
                    try:
                        await last_message.react(reaction=[ReactionTypeEmoji(emoji=reaction)])
                    except Exception as react_err:  # noqa: BLE001
                        logger.warning(f"Failed to send reaction {reaction!r}: {react_err}")

                await last_message.answer(reply_text)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Error processing batch for user {user_id}: {e}")
                await last_message.answer("Sorry, I ran into a problem just now — mind trying again?")
            finally:
                if not state.pending_messages:
                    self._stop_typing(state)

    @staticmethod
    def _stop_typing(state: UserState):
        state.typing_stop_event.set()
        if state.typing_task and not state.typing_task.done():
            state.typing_task.cancel()

    async def run_extractor(self, user_id: int):
        """Run memory extraction in the background; at most one per user (shared memory_lock)."""
        state = await self.get_state(user_id)
        if state.memory_lock.locked():
            logger.info(f"Memory lock held for user {user_id}; skipping extractor.")
            return
        async with state.memory_lock:
            from app.services.memory_extractor import extract_and_trim
            await extract_and_trim(user_id)

    async def run_compressor(self, user_id: int):
        """Run memory compression in the background; rate-limited and serialized per user."""
        state = await self.get_state(user_id)
        if time.time() - state.last_compression_time < config.COMPRESSION_COOLDOWN_SECS:
            logger.debug(f"Compression cooldown active for user {user_id}; skipping.")
            return
        if state.memory_lock.locked():
            logger.info(f"Memory lock held for user {user_id}; skipping compressor.")
            return
        async with state.memory_lock:
            from app.services.memory_compressor import compress_user_memory
            await compress_user_memory(user_id)
        state.last_compression_time = time.time()


user_task_manager = UserTaskManager()
