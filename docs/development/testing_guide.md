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
├── test_group_models.py              # chat_id buffers with sender attribution & chat_members CRUD (Phase 9)
├── test_group_plumbing.py            # DM-unchanged + group multi-party handle_message / chat_id batching (Phase 9)
├── test_group_routing.py             # is_addressed + chat-type routing + ambient handoff (Phase 9)
├── test_ambient_gate.py              # AmbientGate funnel: cooldown, triggers, dice, prune, mark_chimed (Phase 9)
├── test_affinity_and_commands.py     # AffinityCache read/write-through + /quiet /chatty (Phase 9)
├── test_group_extraction.py          # Multi-party per-user extraction & attribution (Phase 9)
├── test_group_config_observability.py # Ambient config knobs honored + per-stage drop observability (Phase 9)
└── run_llm_live.py                   # Manual live check against the configured LLM (not part of the suite)
```

> **Phase 9 (group chat) is implemented and tested.** The seven `test_group_*` /
> `test_ambient_gate` / `test_affinity_and_commands` files below cover chat-type routing, the
> ambient-gate funnel, affinity, the group commands, and multi-party extraction. The full suite
> is **125 passing**. See [group_chat.md](group_chat.md).

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

### 7. Group Chat Suite *(Phase 9)*
Seven files lock in group behavior while proving the DM path is untouched:

*   **`tests/test_group_models.py`** — each buffered message persists `sender_id`/`sender_name`;
    `sender_id` defaults to `chat_id` when omitted; a DM-style call keeps `_id == chat_id`; the
    `$slice` hard cap bounds the array; and `chat_members` upsert applies defaults, clamps affinity
    to `[0, 1]`, coerces an invalid mode to `auto`, round-trips valid values, and `get_chat_member`
    returns `None` when absent.
*   **`tests/test_group_plumbing.py`** — `handle_message` in a DM is unchanged and renders a
    single-party history; the group path renders multi-party `"Name: content"` history; and
    `enqueue_message` batches by `chat_id` (not by sender).
*   **`tests/test_group_routing.py`** — `is_addressed` for mention / name-token / reply-to-bot
    (and the word-boundary non-match); addressed messages enqueue a reply with no extra buffer
    write and no chime; non-addressed messages are buffered once and handed to the gate; channel
    posts are ignored entirely; and bot commands in groups return early.
*   **`tests/test_ambient_gate.py`** — the `AmbientGate` funnel: cooldown blocks a second chime in
    the window, no-trigger/not-scan-tick drops, a scan tick passes without a keyword, the dice roll
    respects `p = base × affinity × mode_factor`, `quiet` mode forces no chime, at most one chime
    over a burst, `prune` drops stale state, and `mark_chimed` holds the window even on an empty reply.
*   **`tests/test_affinity_and_commands.py`** — `AffinityCache` read-through defaults on miss
    (creating the record), serves a warm read from cache without a DB hit, `bump` clamps and writes
    through, `set_mode` writes through, and `prune` evicts idle entries; plus the `/quiet` `/chatty`
    command behavior (group set vs. DM no-op).
*   **`tests/test_group_extraction.py`** — a two-speaker segment attributes each fact to the correct
    user profile in one extraction call; an update tagged to a non-participant is skipped (no crash,
    no profile); and the processed segment is trimmed afterward.
*   **`tests/test_group_config_observability.py`** — the `GROUP_AMBIENT_COOLDOWN_SECS`,
    `GROUP_AMBIENT_BASE_RATE`, and `GROUP_CONTEXT_SCAN_EVERY` knobs each change the outcome as
    expected, `decide` reports the correct drop `stage` for every funnel outcome, and the router
    emits the per-stage drop log.

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
