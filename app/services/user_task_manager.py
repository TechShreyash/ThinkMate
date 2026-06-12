import asyncio
import time
from loguru import logger
from aiogram import Bot
from aiogram.types import Message
from app.config import config
from app.database.connection import db_session
from app.services.chat_manager import handle_message

class UserState:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.chat_lock = asyncio.Lock()
        self.compressor_lock = asyncio.Lock()
        self.pending_messages = []  # list of dict: {"text": str, "message": Message}
        self.batch_task = None
        self.typing_task = None
        self.typing_stop_event = asyncio.Event()
        self.first_message_time = None

class UserTaskManager:
    def __init__(self):
        self._states = {}
        self._states_lock = asyncio.Lock()

    async def get_state(self, user_id: int) -> UserState:
        async with self._states_lock:
            if user_id not in self._states:
                self._states[user_id] = UserState(user_id)
            return self._states[user_id]

    async def enqueue_message(self, bot: Bot, user_id: int, text: str, message: Message):
        state = await self.get_state(user_id)
        current_time = time.time()
        
        # Max queued messages safety guard
        if len(state.pending_messages) >= config.MAX_QUEUED_MESSAGES:
            logger.warning(f"User {user_id} message queue exceeded maximum limit ({config.MAX_QUEUED_MESSAGES}). Dropping message.")
            return

        # Add to pending queue
        state.pending_messages.append({"text": text, "message": message})
        
        # Start typing indicator if not already running
        if not state.typing_task or state.typing_task.done():
            state.typing_stop_event.clear()
            state.typing_task = asyncio.create_task(
                self._typing_loop(bot, message.chat.id, state.typing_stop_event)
            )
            
        # Set first message time if it's the start of a batch
        if state.first_message_time is None:
            state.first_message_time = current_time
            
        # Check if the maximum batch delay has been reached
        max_delay = config.MAX_BATCH_DELAY_SECS
        time_elapsed = current_time - state.first_message_time
        
        if time_elapsed >= max_delay:
            # Force immediate batch processing
            if state.batch_task and not state.batch_task.done():
                state.batch_task.cancel()
            state.batch_task = asyncio.create_task(self._process_batch(user_id))
        else:
            # Cancel any pending batch task to reset the delay timer
            if state.batch_task and not state.batch_task.done():
                state.batch_task.cancel()
                
            state.batch_task = asyncio.create_task(
                self._delayed_process_batch(user_id)
            )

    async def _typing_loop(self, bot: Bot, chat_id: int, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception as e:
                logger.debug(f"Failed to send typing chat action: {e}")
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
        except Exception as e:
            logger.error(f"Error in delayed batch processing for user {user_id}: {e}")

    async def _process_batch(self, user_id: int):
        state = await self.get_state(user_id)
        
        # Acquire chat lock to ensure max 1 chat response request is running for the user
        async with state.chat_lock:
            if not state.pending_messages:
                # Stop typing when queue is empty
                state.typing_stop_event.set()
                if state.typing_task:
                    state.typing_task.cancel()
                state.first_message_time = None
                return

            # Drain all pending messages
            batch = list(state.pending_messages)
            state.pending_messages.clear()
            state.first_message_time = None
            
            logger.info(f"Processing batch of {len(batch)} messages for user {user_id}")
            
            # Combine message texts
            combined_text = "\n".join(msg["text"] for msg in batch)
            last_message = batch[-1]["message"]
            
            try:
                # Establish database session for processing
                async with db_session() as db:
                    reply_text = await handle_message(db, user_id, combined_text)
                    await last_message.answer(reply_text)
            except Exception as e:
                logger.error(f"Error processing message batch for user {user_id}: {e}")
                await last_message.answer("Sorry, I encountered an error while processing your messages.")
            finally:
                # If no new messages were enqueued during processing, stop typing
                if not state.pending_messages:
                    state.typing_stop_event.set()
                    if state.typing_task:
                        state.typing_task.cancel()

    async def run_compressor(self, user_id: int):
        state = await self.get_state(user_id)
        if state.compressor_lock.locked():
            logger.info(f"Memory compressor already running for user {user_id}, skipping new launch.")
            return
            
        async with state.compressor_lock:
            from app.services.memory_compressor import compress_user_memory
            await compress_user_memory(user_id)

user_task_manager = UserTaskManager()
