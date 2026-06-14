# Implementation Plan: Group User Memory

## Overview

This plan implements per-person group memory plus three cross-cutting operational concerns
(centralized log forwarding, per-task LLM metrics, environment-configurable commands) in an
additive, dependency-ordered way. Foundations come first (config settings, metrics map,
Log_Forwarder, Error_Log_Sink, identity accessors, prompt parameter), then the group reply
composition and handler/extractor wiring, then the command registry and metrics reporting,
and finally the property, regression, and wiring test suites. Every new operation sits on or
near a hot path and follows the project's degrade-never-raise contract, and the DM path is
held byte-for-byte unchanged. Each task references the requirements it satisfies; property
tasks also reference their design property number for traceability.

## Tasks

- [x] 1. Add LOGS_CHANNEL_ID configuration
  - [x] 1.1 Add `LOGS_CHANNEL_ID` setting to `app/config.py`
    - Add `LOGS_CHANNEL_ID: int = Field(default_factory=lambda: _env_int("LOGS_CHANNEL_ID", -1003933328659))` following the existing `_env_int` config pattern
    - Confirm it loads through the existing `config` object used elsewhere in the app
    - _Requirements: 4.1_

  - [ ]* 1.2 Write config default test
    - Assert `config.LOGS_CHANNEL_ID` equals `-1003933328659` when the env var is unset
    - Assert an env override is parsed as an int
    - _Requirements: 4.1_

- [x] 2. Add Command_Config to configuration
  - [x] 2.1 Add Command_Config accessor to `app/config.py`
    - Add `_BUILTIN_COMMANDS` tuple (`start, onboard, pause, resume, help, profile, reset, quiet, chatty, health, metrics`) in help-display order; the key is also the default trigger
    - Add `_CMD_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")` for Telegram trigger-name validation
    - Implement `resolve_command_config() -> dict[str, tuple[str, bool]]` reading `CMD_<KEY>_NAME` (default = key, stripped of leading `/`) via `_env_str` and `CMD_<KEY>_ENABLED` (default `True`) via `_env_bool` (Req 7.1, 7.2)
    - Invalid trigger (fails `_CMD_NAME_RE`) → fall back to default key with a logged warning (Req 7.5)
    - Duplicate trigger among ENABLED commands → fall back BOTH colliding commands to their default keys with a logged warning (Req 7.5); honor a clean name-swap as-is
    - Wrap the whole body so any unexpected parse error returns the all-defaults, all-enabled mapping and startup never raises (Req 7.7)
    - Add `COMMANDS: dict[str, tuple[str, bool]] = Field(default_factory=resolve_command_config)` to the `Config` model
    - _Requirements: 7.1, 7.2, 7.5, 7.7_

  - [ ]* 2.2 Write property test for command config defaults
    - **Property 16: Command config defaults when env is unset**
    - For any built-in command with `CMD_<KEY>_NAME`/`CMD_<KEY>_ENABLED` unset, assert the resolved trigger equals its key and enabled is `True`
    - **Validates: Requirements 7.1, 7.2**

  - [ ]* 2.3 Write property test for invalid/duplicate trigger fallback
    - **Property 19: Invalid or duplicate triggers fall back to defaults without crashing**
    - For any config with invalid trigger names, triggers duplicating another enabled command's trigger, or an unparseable env, assert every affected command resolves to its default key, the enabled-trigger set has no duplicates, and `resolve_command_config` returns without raising
    - **Validates: Requirements 7.5, 7.7**

- [x] 3. Complete per-task LLM metrics mapping
  - [x] 3.1 Complete `_LLM_TYPE_PREFIX` and export `LLM_TASK_TYPES` in `app/services/metrics.py`
    - Add explicit entries for all six LLM_Task_Types, including `memory_consolidation -> "consolidation"` and `proactive_checkin -> "checkin"` (Req 6.3)
    - Export `LLM_TASK_TYPES: tuple[tuple[str, str], ...] = tuple(_LLM_TYPE_PREFIX.items())` as the single ordered source of truth consumed by both `record_llm` and the reporter
    - Keep `record_llm` shape unchanged (`_LLM_TYPE_PREFIX.get(call_type, call_type)`) and confirm its body is wrapped so a metrics failure never raises into the call site (Req 6.1, 6.2, 6.7)
    - _Requirements: 6.1, 6.2, 6.3, 6.7_

  - [ ]* 3.2 Write property test for per-task LLM call counting
    - **Property 13: Per-task LLM call counting is exact**
    - For any finite sequence of `record_llm(task_type, ok, latency)` across the six task types, assert the snapshot's `llm.<prefix>.calls` equals the number of calls for that type, with `success + failure == calls` and the latency timer `count == calls`
    - **Validates: Requirements 6.1, 6.2, 6.3**

  - [ ]* 3.3 Write property test for metric recording never raising
    - **Property 15: Metric recording never raises into the call site**
    - For any `record_llm` invocation, including when an internal registry mutation fails, assert the call returns without propagating an exception
    - **Validates: Requirements 6.7**

  - [ ]* 3.4 Write edge test for consolidation/checkin canonical prefixes
    - Assert `record_llm("memory_consolidation", ...)` records under `llm.consolidation.*` and `record_llm("proactive_checkin", ...)` records under `llm.checkin.*`
    - _Requirements: 6.3_

- [x] 4. Implement Log_Forwarder module
  - [x] 4.1 Create `app/services/log_forwarder.py`
    - Implement module-level `_bot` reference, `set_bot(bot)`, and `async def send(bot, source_chat_id, text)`
    - Forward only the three explicit group-memory events (identity, extraction-saved, extraction-skipped); NO per-reply event
    - `send` reads `config.LOGS_CHANNEL_ID`; no-op when target falsy; use `bot or _bot`; no-op when no bot is available
    - Anti-recursion: no-op when `source_chat_id == target`, dropping events whose source chat is the Logs_Channel (Req 4.10)
    - Wrap the entire body so any send failure is logged at debug and swallowed (Req 4.8)
    - Bind a `no_forward=True` marker on the forwarder's own logs (`logger.bind(no_forward=True)`) so the Error_Log_Sink never re-forwards them (Req 4.9)
    - _Requirements: 4.8, 4.9, 4.10_

  - [x] 4.2 Wire `log_forwarder.set_bot(bot)` in `main.py`
    - Call `log_forwarder.set_bot(bot)` immediately after `bot = Bot(token=...)` so the background extractor has a process-wide bot reference
    - _Requirements: 4.3, 4.4_

  - [ ]* 4.3 Write property test for Log_Forwarder source anti-recursion
    - **Property 8: Log_Forwarder anti-recursion on source chat**
    - For any text, when `source_chat_id == LOGS_CHANNEL_ID`, assert the mock bot's `send_message` is never called
    - **Validates: Requirements 4.10**

  - [ ]* 4.4 Write property test for Log_Forwarder never raises
    - **Property 9: Log_Forwarder never raises**
    - For any text, with a transport whose `send_message` raises, assert `send` returns without raising
    - **Validates: Requirements 4.8**

- [x] 5. Implement Error_Log_Sink module
  - [x] 5.1 Create `app/services/error_log_sink.py`
    - Implement `make_error_log_sink(bot, loop)` returning a synchronous loguru `sink(message)` closing over the captured `bot` and main event `loop`
    - Re-entry guard: `_in_sink` `contextvars.ContextVar` (default `False`); return immediately when already set (Req 4.9)
    - Skip records bound with `no_forward` in `record["extra"]` (Req 4.9)
    - Level-guard as defense-in-depth: return when `record["level"].no < 30` (WARNING) (Req 4.5)
    - Format a concise one-line message (level name, `name`:`function`, message text)
    - Dispatch via `loop.call_soon_threadsafe(_dispatch)`; inside `_dispatch` schedule the send with `asyncio.create_task(bot.send_message(...))`, setting/resetting `_in_sink` around the send
    - Swallow exceptions in every layer (outer sink body, dispatch, inner send) so the sink never blocks and never raises back into the originating logging call (Req 4.6, 4.7)
    - _Requirements: 4.5, 4.6, 4.7, 4.9_

  - [x] 5.2 Register the Error_Log_Sink in `main.py`
    - Inside the running async context, capture `loop = asyncio.get_running_loop()`
    - Add `logger.add(make_error_log_sink(bot, loop), level="WARNING", filter=lambda r: not r["extra"].get("no_forward"), enqueue=False)` alongside the existing console + `logs/bot.log` sinks without replacing them
    - _Requirements: 4.5, 4.9_

  - [ ]* 5.3 Write property test for Error_Log_Sink WARNING+ forwarding
    - **Property 10: Error_Log_Sink forwards exactly the WARNING+ records**
    - Using a mock bot and a loop double whose `call_soon_threadsafe` runs the callback inline, assert a `WARNING`+ record yields exactly one `bot.send_message` and any sub-`WARNING` record yields none
    - **Validates: Requirements 4.5**

  - [ ]* 5.4 Write property test for Error_Log_Sink re-entry / self-forward safety
    - **Property 11: Error_Log_Sink never re-forwards its own or the Log_Forwarder's records**
    - With the loop double, assert a `no_forward`-marked record, or a record emitted while `_in_sink` is set, produces no forward (no infinite recursion)
    - **Validates: Requirements 4.9**

  - [ ]* 5.5 Write property test for Error_Log_Sink non-blocking + never raises
    - **Property 12: Error_Log_Sink is non-blocking and never raises into the logging call**
    - With a loop double running callbacks inline and a `bot.send_message` that raises, assert invoking the sink returns without raising and does not block/await the originating logging call
    - **Validates: Requirements 4.6, 4.7**

- [x] 6. Implement identity-safe accessors in models
  - [x] 6.1 Add `refresh_identity_if_changed` to `app/database/models.py`
    - Read only `{"username":1,"display_name":1}` before deciding to write (Req 1.1)
    - When no profile exists, create via `ensure_user` carrying incoming identity and return `{"created": True, ...}` (Req 1.2)
    - Update `username`/`display_name` only when the incoming value is non-empty and absent or different (Req 1.3, 1.4)
    - Guard empty incoming values so they never blank a populated stored value
    - Return `None` (no write) when already current (Req 1.5)
    - The `$set` must never include any Memory_Field key (Req 1.6)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

  - [ ]* 6.2 Write property test for identity write only on absent or changed
    - **Property 1: Identity write only on absent or changed**
    - For any stored profile and non-empty incoming values, assert stored identity equals incoming when absent or differing, and the profile is created with incoming identity when none existed
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4**

  - [ ]* 6.3 Write property test for no identity write when already current
    - **Property 2: No identity write when already current**
    - For any profile whose stored identity already equals incoming values, assert `refresh_identity_if_changed` returns no change and leaves the document unchanged
    - **Validates: Requirements 1.5**

  - [ ]* 6.4 Write property test for identity writes never altering Memory_Fields
    - **Property 3: Identity writes never alter Memory_Fields**
    - For any profile carrying arbitrary Memory_Fields, assert every Memory_Field is byte-for-byte unchanged after an identity refresh
    - **Validates: Requirements 1.6**

  - [ ]* 6.5 Write edge test for empty incoming identity not blanking stored values
    - Assert empty/None incoming `username`/`display_name` does not blank an existing populated identity in `refresh_identity_if_changed`
    - _Requirements: 1.3, 1.4_

  - [x] 6.6 Add `_ensure_memory_skeleton` and fix `save_extracted_memories` fallback in `app/database/models.py`
    - Add `_ensure_memory_skeleton(db, user_id)` that `$setOnInsert` the memory skeleton only, leaving `username`/`display_name` unset
    - Replace the `ensure_user(db, user_id, "", "")` fallback in `save_extracted_memories` with `_ensure_memory_skeleton`
    - Keep the `$set` payload carrying only Memory_Fields (no identity keys) (Req 2.4)
    - _Requirements: 2.1, 2.4_

  - [ ]* 6.7 Write property test for memory writes never altering Identity_Fields
    - **Property 4: Memory writes never alter Identity_Fields**
    - For any profile with set Identity_Fields and any extraction, assert `save_extracted_memories` leaves `username`/`display_name` unchanged
    - **Validates: Requirements 2.4**

  - [ ]* 6.8 Write edge test for identity-safe memory skeleton creation
    - Assert `_ensure_memory_skeleton` on an absent profile creates the memory skeleton with `username`/`display_name` unset
    - _Requirements: 2.1, 2.4_

- [x] 7. Extend system prompt composition
  - [x] 7.1 Add `user_memory_text` parameter to `build_system_prompt` in `app/prompts/system_prompt.py`
    - Add an optional `user_memory_text=""` keyword parameter
    - When present and non-blank, append a distinctly-labeled per-user memory section after the group block (Req 3.3, 3.5)
    - When empty, render output identical to today so the DM path is byte-for-byte unchanged (Req 3.6, 5.2)
    - _Requirements: 3.3, 3.5, 3.6, 5.2_

  - [ ]* 7.2 Write property test for group reply prompt composition
    - **Property 7: Group reply prompt contains both memory blocks**
    - For any non-empty per-user and group blocks, assert the assembled prompt contains both; when the per-user block is empty, assert the prompt equals the group-only prompt verbatim
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.5, 3.7**

- [x] 8. Checkpoint - foundations
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Wire per-user memory into the group reply path
  - [x] 9.1 Compose group prompt with per-user and group blocks in `handle_message` (`app/services/chat_manager.py`)
    - In the group branch, load `group_block` keyed by `chat_id` (Req 3.2) and `user_block` keyed by `sender_id` (Req 3.1, 3.4)
    - Wrap the per-user load so a failure degrades to group-only without raising (Req 3.7)
    - Call `build_system_prompt(persona, group_block, time_context="", user_memory_text=user_block)` (Req 3.3, 3.5)
    - Keep `needs_compression` tracking the group (`chat_id`) block
    - Perform NO Logs_Channel forwarding here; bot threading stays optional and DM-safe (`bot: Bot | None = None`) and triggers no forwarding
    - Leave the DM branch byte-for-byte unchanged (Req 3.6, 5.2)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 5.2_

  - [ ]* 9.2 Write unit test for per-user load failure degradation
    - Stub `build_memory_block` to raise for the `sender_id` and assert the reply is still generated with the group block only and no exception propagates
    - _Requirements: 3.7, 5.4_

- [x] 10. Wire identity capture into the group handler
  - [x] 10.1 Add best-effort identity refresh to `_handle_group_message` in `app/handlers/messages.py`
    - Early (before routing), compute `sender_name` via `_display_name(message)` and call `models.refresh_identity_if_changed(db, message.from_user.id, message.from_user.username or "", sender_name)`
    - On a non-None change, forward an `identity` event via `log_forwarder.send(message.bot, message.chat.id, …)` (Req 4.2)
    - Wrap in try/except so any failure is logged at debug and does not raise on the hot path (Req 1.7, 5.4)
    - Do not write the chat buffer here, preserving the Single_Write_Invariant (Req 5.1)
    - Ensure this runs only on the group path; DM branch untouched (Req 5.3)
    - _Requirements: 1.1, 1.7, 4.2, 5.1, 5.3, 5.4_

  - [ ]* 10.2 Write unit test for identity refresh hot-path safety
    - Stub `refresh_identity_if_changed` to raise and assert `_handle_group_message` continues without raising and the buffer write still happens exactly once
    - _Requirements: 1.7, 5.1, 5.4_

- [x] 11. Wire extractor logging
  - [x] 11.1 Forward saved/skipped events in `app/services/memory_extractor.py`
    - On each successful `save_extracted_memories`, forward a `memory-extraction-saved` event via the process-wide bot (Req 4.3)
    - On each unresolved-participant skip, forward a `memory-extraction-skipped` event (Req 4.4)
    - Confirm unresolved names are skipped without creating misattributed/empty-identity profiles (Req 2.3) and resolved memory persists against the identity-bearing `sender_id` (Req 2.1, 2.2)
    - _Requirements: 2.1, 2.2, 2.3, 4.3, 4.4_

  - [ ]* 11.2 Write property test for extracted memory landing on identity-bearing user_id
    - **Property 5: Extracted memory lands on the identity-bearing user_id**
    - For any group segment whose participants were identity-captured, assert each resolved participant's Memory_Fields persist against the same `sender_id` holding their non-empty Identity_Fields
    - **Validates: Requirements 2.1, 2.2**

  - [ ]* 11.3 Write property test for unresolved participants skipped without profile creation
    - **Property 6: Unresolved participants are skipped without profile creation**
    - For any update whose participant name does not resolve in the name→id map, assert no profile is created and no memory is written for that name
    - **Validates: Requirements 2.3**

- [x] 12. Implement Command_Registry and metrics reporting in commands
  - [x] 12.1 Convert command decorators to a registry in `app/handlers/commands.py`
    - Convert each hardcoded `@router.message(Command(...))` handler to a plain coroutine with an unchanged body
    - Add the `_COMMANDS` map (`command_key -> (handler, help description)`) ordered to match `_BUILTIN_COMMANDS`
    - Add `register_commands(router)` driven by `config.COMMANDS`: bind enabled commands to their resolved trigger; skip disabled commands so they stay unregistered and draw no response (Req 7.3); bind renamed commands' existing handler under the new trigger (Req 7.4); guard each binding to fall back to the default trigger on unexpected failure
    - Keep the Admin_Gate (`if not _admin_allowed(message): return`) INSIDE `cmd_health` and `cmd_metrics` so authorization survives any rename (Req 7.6)
    - Make `cmd_help` dynamic: render one line per enabled command using the resolved trigger and `_COMMANDS` description, omitting disabled and (for non-admins) admin-only commands (Req 7.3, 7.4)
    - Call `register_commands(router)` at import time (bottom of `commands.py`)
    - _Requirements: 7.3, 7.4, 7.6_

  - [x] 12.2 Enhance `_render_metrics` with an "LLM calls by task" section in `app/handlers/commands.py`
    - Add `_render_llm_by_task(snap)` that iterates the canonical `LLM_TASK_TYPES` and, per task type, reads `llm.<prefix>.calls/.success/.failure` from counters and `avg`/`max` from the `llm.<prefix>.latency` timer, rendering missing values as `0` (Req 6.4, 6.5, 6.8)
    - Prepend the "LLM calls by task" lines in `_render_metrics`, retaining the existing counters/gauges/timers dump unchanged
    - Keep the report downstream of the existing Admin_Gate in `cmd_metrics` (Req 6.6)
    - _Requirements: 6.4, 6.5, 6.6, 6.8_

  - [ ]* 12.3 Write property test for disabled commands not registered
    - **Property 17: Disabled commands are not registered**
    - For any subset of commands configured disabled, assert `register_commands` against a fresh `Router` binds exactly the enabled commands and registers no handler for any disabled command
    - **Validates: Requirements 7.3**

  - [ ]* 12.4 Write property test for renamed command binding
    - **Property 18: Renamed command binds unchanged behavior to the configured trigger**
    - For any valid override trigger, assert `register_commands` binds the command's original handler callable under the configured trigger
    - **Validates: Requirements 7.4**

  - [ ]* 12.5 Write property test for metrics report listing every task type
    - **Property 14: Metrics report lists every task type with count and available aggregates**
    - For any snapshot, assert the rendered report has exactly one "LLM calls by task" line per canonical LLM_Task_Type showing its count (rendering `0` when absent) and includes success/failure/latency aggregates when present, never raising for an absent task type
    - **Validates: Requirements 6.4, 6.5, 6.8**

- [x] 13. DM-unchanged regression and wiring tests
  - [x]* 13.1 Write DM-unchanged regression suite
    - Assert the DM `handle_message` path produces the same system prompt (no per-user block), same buffer writes, and same return contract as before
    - Assert the DM path performs no identity capture, no per-user/group combination, and no Logs_Channel forwarding
    - _Requirements: 5.2, 5.3_

  - [x]* 13.2 Write Log_Forwarder, Error_Log_Sink, and command wiring tests
    - With a mock bot, assert `log_forwarder.send` is invoked at the three explicit points — identity, extraction-saved, extraction-skipped — and that a send failure at any point is swallowed and does not interrupt processing (Req 4.2, 4.3, 4.4, 4.8)
    - With a mock bot and a loop double whose `call_soon_threadsafe` runs the callback inline, assert Error_Log_Sink dispatch: a `WARNING`+ log yields exactly one `bot.send_message`, a sub-`WARNING` log yields none, a `no_forward`-bound log yields none, and a raising `bot.send_message` does not propagate (Req 4.5, 4.6, 4.7, 4.9)
    - Assert `resolve_command_config` env cases: unset → all-defaults/all-enabled; `CMD_HELP_ENABLED=false` disables `help`; `CMD_START_NAME=hello` remaps `start`; invalid `CMD_START_NAME=" bad/name"` and duplicate `CMD_PROFILE_NAME=help` each fall back to defaults; a malformed env yields all-defaults (Req 7.1, 7.2, 7.3, 7.4, 7.5, 7.7)
    - Assert `register_commands` against a fresh `Router` registers exactly the enabled commands under their resolved triggers and leaves disabled commands unmatched; confirm `cmd_health`/`cmd_metrics` stay admin-gated after a rename (Req 7.3, 7.4, 7.6)
    - Not asserting Telegram delivery
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

- [x] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Each task references specific requirements for traceability; property tasks also reference the design property number.
- This plan covers all 7 requirements and the 19 correctness properties: Properties 1-3 (identity refresh), 4 (memory/identity separation), 5-6 (group extraction), 7 (group prompt composition), 8-9 (Log_Forwarder), 10-12 (Error_Log_Sink), 13-15 (per-task metrics), 16-19 (command config & registry).
- The Log_Forwarder forwards exactly three explicit events (identity, extraction-saved, extraction-skipped); bot-wide `WARNING`+ visibility is handled by the Error_Log_Sink, not by threading `bot` through the Chat_Manager.
- The DM-unchanged regression suite (13.1) is the primary guard for Req 5.2/5.3.
- Tests use `pytest` + `hypothesis` with a `mongomock`-backed async DB; each property test runs ≥100 iterations. Error_Log_Sink tests use a loop double whose `call_soon_threadsafe` runs callbacks inline.
- Checkpoints ensure incremental validation after foundations and before completion.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1", "4.1", "5.1", "6.1", "7.1"] },
    { "id": 1, "tasks": ["2.1", "4.2", "6.6", "1.2", "3.2", "3.3", "3.4", "4.3", "4.4", "5.3", "5.4", "5.5", "6.2", "6.3", "6.4", "6.5", "7.2"] },
    { "id": 2, "tasks": ["5.2", "9.1", "10.1", "11.1", "12.1", "2.2", "2.3", "6.7", "6.8"] },
    { "id": 3, "tasks": ["12.2", "9.2", "10.2", "11.2", "11.3", "12.3", "12.4"] },
    { "id": 4, "tasks": ["12.5", "13.1", "13.2"] }
  ]
}
```
