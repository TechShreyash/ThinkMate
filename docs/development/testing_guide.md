# Testing Infrastructure & Test Suite Guide

This guide documents the ThinkMate testing infrastructure, explaining what each component in the `tests/` folder does, how MongoDB is mocked in-memory during testing, and how to execute the test suite.

---

## 📋 Overview

ThinkMate has a robust test suite powered by `pytest` and `pytest-asyncio`. To make execution fast and independent of external resources, **all tests are run against an in-memory database mock**. You do not need a running MongoDB server instance to execute the tests.

---

## 📂 Test Folder Structure

```
tests/
├── conftest.py                       # Session-wide test setup & MongoDB mocks
├── test_database.py                  # Validates MongoDB CRUD and model operations
├── test_guards_and_compression.py     # Tests input guards, prompt compiling, and memory compression
├── test_batching_and_concurrency.py  # Tests messaging queues, batching, throttling, and concurrency locks
├── test_reactions.py                 # Combined reply+reaction call & emoji normalization
├── test_hardening.py                 # Atomic trim race, dedup, cooldown, reset, budget enforcement, eviction, extraction retry/bounded-trim
└── run_llm_live.py                   # Manual live check against the configured LLM (not part of the suite)
```

> **Phase 9 (group chat)** adds `tests/test_group_chat.py`: chat-type routing, the ambient-gate
> funnel (cooldown → keyword scan → affinity probability), affinity updates, and multi-party
> per-user extraction. See [group_chat.md](group_chat.md).

---

## 🔍 Detailed Component Breakdown

### 1. `tests/conftest.py` (The Mocking Layer)
Because `mongomock` is a synchronous in-memory MongoDB mock library, it does not natively support the async `motor` driver. The `conftest.py` file defines a custom async wrapper to bridge this gap:

*   **`AsyncMockCursor`**: Simulates the behavior of motor's async cursors (supporting async iteration using `__aiter__` and `__anext__`).
*   **`AsyncMockCollection`**: Intercepts queries (like `find_one`, `update_one`, `find_one_and_update`, `insert_one`, `delete_one`, `delete_many`, `create_index`, and `find`) and maps them to synchronous calls on the underlying `mongomock` collection wrapper.
*   **`AsyncMockDatabase` / `AsyncMockClient`**: Wraps the database and client instances.
*   **`mock_mongodb` Fixture**: An autouse fixture that dynamically patches `app.database.connection.get_db` and `app.database.connection.get_db_client` globally for all tests. This ensures that every test gets a clean, isolated, in-memory MongoDB environment automatically.

---

### 2. `tests/test_database.py` (Database Model Operations)
Validates the database accessors inside `app/database/models.py`:

*   **`test_db_initialization`**: Verifies that the database initializes cleanly and correct indexes are configured on the collections.
*   **`test_ensure_user_and_buffer`**: Verifies that the user registration and chat buffer message arrays `$push` and trim operations execute correctly.
*   **`test_save_extracted_memories`**: Tests the surgical memory insertion logic, validating direct emotional state updating and array filtering (verifying that old records are hard-deleted when new versions are saved).

---

### 3. `tests/test_guards_and_compression.py` (Memory Constraints & Budgets)
Validates limits, formatted system prompts, and memory compression:

*   **`test_input_guard_config`**: Confirms all default limits are configured correctly (e.g. `USER_MEMORY_BUDGET_CHARS = 4000`).
*   **`test_build_memory_block_and_compression_flag`**: Ensures the prompt context block compiling structures Facts, Subjective Beliefs, Events, and Mood correctly, and sets the `needs_compression` flag if the compiled length breaches the character budget.
*   **`test_replace_user_memory`**: Validates the atomic memory replacement method, confirming that compression successfully overwrites facts, beliefs, events, summaries, and communication styles.

---

### 4. `tests/test_batching_and_concurrency.py` (Concurrency & Flow Controls)
Validates the message batching queue, rate limiting, and execution locks inside the `UserTaskManager`:

*   **`test_message_batching_delay`**: Verifies that rapid-fire messages sent within the delay window are coalesced into a single combined batch request.
*   **`test_character_count_extraction_trigger`**: Verifies that a background memory extraction task is spawned when the chat buffer character count breaches the threshold.
*   **`test_memory_extraction_excludes_latest_trim`**: Confirms that extraction only reads the older buffer slice, keeping the latest `CHAT_BUFFER_TRIM` messages untouched to maintain active context.
*   **`test_concurrent_compressor_lock`**: Tests that background tasks serialize and serialize execution safely via the unified `memory_lock`, preventing data write race conditions.
*   **`test_max_batch_delay_prevents_infinite_postponement`**: Assures that the batch deadline is enforced, forcing response generation even under a continuous flood of messages.
*   **`test_throttling_middleware`**: Verifies that the throttling middleware successfully intercepts flooding users and rate limits messages before database resource allocation.
*   **`test_user_task_manager_queue_limit_guard`**: Checks that the queue drops messages once the maximum queue threshold is exceeded to avoid memory overload.

---

### 5. `tests/test_hardening.py` (Hardening & Resilience Regressions)
Locks in the Phase 7–8 fixes so they can't regress:

*   **`test_atomic_trim_preserves_concurrent_appends`**: the atomic `$pull` trim keeps messages appended during a trim (the old read-slice-overwrite would have lost them).
*   **`test_buffer_hard_cap`**: the messages array never exceeds `CHAT_BUFFER_HARD_CAP`.
*   **`test_normalized_dedup_on_extraction`**: facts differing only by case/whitespace are not duplicated.
*   **Budget enforcement & eviction**: deterministic post-compression trimming fits the budget; idle `UserState` is evicted.
*   **`test_extraction_retries_and_folds_in_new_messages`**: a failed extraction is retried, and messages that arrive mid-call are folded into the next attempt's segment.
*   **`test_extraction_all_attempts_fail_still_trims`**: when every attempt fails, the oldest messages are trimmed anyway (buffer stays bounded) and memory is never written.

### 6. `tests/test_reactions.py` (Combined Reply + Reaction)
Validates that `generate_reply_bundle` parses the `{reply, reaction}` JSON, degrades to plain
text on bad JSON, and that `normalize_reaction` maps free-form emojis onto Telegram's accepted
set (tolerant of variation selectors).

---

## 🚀 Running the Tests

To run the test suite, always use `uv` from the repository root:

```bash
uv run python -m pytest
```

### Tips & Tricks
*   **Run a specific test file**:
    ```bash
    uv run python -m pytest tests/test_database.py
    ```
*   **Run a specific test case**:
    ```bash
    uv run python -m pytest -k test_ensure_user_and_buffer
    ```
*   **Show prints/logs during execution**:
    ```bash
    uv run python -m pytest -s
    ```
