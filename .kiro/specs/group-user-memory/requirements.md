# Requirements Document

## Introduction

This feature deepens ThinkMate's group-chat behavior by giving the bot durable, per-person
memory that is captured, extracted, and used inside multi-party groups, plus a centralized
operational log feed.

Today the group path records buffer messages, extracts per-participant memories, and replies
using only the group-level (chat-keyed) memory block. Three gaps remain:

1. Group messages do not refresh the sender's stored identity (username / display name), so
   per-user profiles created during group extraction can carry empty-string identity.
2. Group replies ignore the triggering sender's own per-user memory and use only the
   group-level block.
3. There is no centralized, real-time operational feed for key group-memory events.

This feature closes those gaps while preserving three hard invariants: the chat-buffer
single-write invariant, byte-for-byte-unchanged DM behavior, and defensive (degrade, never
raise) error handling on every hot path.

This refinement also broadens operational visibility and control: bot-wide warnings and
errors (all modules, DM and group) are forwarded to the logs channel through a non-recursive
`loguru` sink, LLM-call volume is recorded and reported per task type through an admin
command, and each built-in command's trigger name and enabled state become configurable via
environment variables without changing command behavior or relaxing admin authorization.

## Glossary

- **Group_Message_Handler**: The group/supergroup routing function `_handle_group_message`
  in `app/handlers/messages.py`.
- **Identity_Updater**: The user-profile upsert accessor `ensure_user` in
  `app/database/models.py`, together with any new identity-refresh accessor it gains.
- **Chat_Manager**: The response-flow orchestrator `handle_message` in
  `app/services/chat_manager.py`.
- **Memory_Loader**: The memory assembly functions `build_memory_block` /
  `compile_memory_text` in `app/services/memory_loader.py`.
- **Group_Extractor**: The multi-party extraction function `extract_and_trim_group` in
  `app/services/memory_extractor.py`.
- **Log_Forwarder**: The new best-effort component that forwards explicit operational events
  (identity, extraction-saved, extraction-skipped) to the configured Telegram logs channel
  via `bot.send_message`.
- **Error_Log_Sink**: The new `loguru` sink that forwards every log record at severity
  `WARNING` or higher, emitted anywhere in the bot, to the Logs_Channel. It runs in logging
  context (often synchronous, outside an async event loop and without a Message), so it
  dispatches the send safely (for example, by scheduling onto the bot's event loop) and
  guards against re-entry.
- **Logs_Channel**: The Telegram channel identified by the configurable `LOGS_CHANNEL_ID`
  setting (default `-1003933328659`) defined in `app/config.py`.
- **User_Profile**: A document in the `user_profiles` collection keyed by Telegram
  `user_id`, holding identity fields (`username`, `display_name`) and memory fields
  (`profile_summary`, `facts`, `beliefs`, `events`, `insights`, `mood_history`, etc.).
- **Identity_Fields**: The `username` and `display_name` fields of a User_Profile.
- **Memory_Fields**: All non-identity content fields of a User_Profile, including
  `profile_summary`, `communication_style`, `emotional_state`, `facts`, `beliefs`,
  `events`, `insights`, and `mood_history`.
- **Per_User_Memory_Block**: The compiled memory text produced by Memory_Loader for a single
  participant, keyed by that participant's `user_id`.
- **Group_Memory_Block**: The compiled memory text produced by Memory_Loader keyed by the
  group's `chat_id`.
- **Triggering_Sender**: The participant whose message caused the bot to reply in a group
  (the addressed / implicitly-addressed / ambient-chime speaker), identified by `sender_id`.
- **Hot_Path**: A code path executed synchronously while handling an inbound message, where
  an unhandled exception would interrupt message processing.
- **Single_Write_Invariant**: The rule that each inbound group message is appended to the
  chat buffer exactly once.
- **Metrics_Registry**: The process-wide in-memory metrics singleton `metrics`
  (`MetricsRegistry`) in `app/services/metrics.py`, exposing `record_llm`, `incr`,
  `observe`, and `snapshot`.
- **LLM_Task_Type**: One of the named LLM call types passed to `metrics.record_llm`:
  `chat_reply`, `memory_extraction`, `group_memory_extraction`, `memory_compression`,
  `memory_consolidation`, and `proactive_checkin`.
- **Metrics_Reporter**: The admin-gated metrics command handler `cmd_metrics` in
  `app/handlers/commands.py`, which renders `metrics.snapshot()` into an operator-readable
  report grouped by LLM_Task_Type.
- **Admin_Gate**: The authorization predicate `_admin_allowed` in
  `app/handlers/commands.py`, which restricts a command to the user ids in
  `ADMIN_USER_IDS` (or to private chats when that set is empty).
- **Built_In_Command**: One of the bot's slash commands defined in
  `app/handlers/commands.py`: `start`, `onboard`, `pause`, `resume`, `help`, `profile`,
  `reset`, `quiet`, `chatty`, `health`, and `metrics`.
- **Admin_Command**: The subset of Built_In_Commands protected by the Admin_Gate, namely
  `health` and `metrics`.
- **Command_Config**: The configuration accessor in `app/config.py` that resolves, for each
  Built_In_Command, its trigger name and its enabled/disabled state from environment
  variables using the existing `_env_str` / `_env_bool` helpers.
- **Command_Registry**: The command registration logic in `app/handlers/commands.py` that
  binds each enabled Built_In_Command to its configured trigger name.

## Requirements

### Requirement 1: Identity capture and refresh from group messages

**User Story:** As a group participant, I want the bot to learn my current username and
display name from my messages, so that the bot remembers me by my real identity rather than
a blank placeholder.

#### Acceptance Criteria

1. WHEN the Group_Message_Handler processes a group message from a sender, THE Identity_Updater SHALL read the sender's existing User_Profile Identity_Fields before deciding whether to write.
2. IF the sender has no existing User_Profile, THEN THE Identity_Updater SHALL create a User_Profile carrying the sender's incoming Telegram `username` and `display_name`.
3. WHEN the sender's stored `username` is absent or differs from the incoming Telegram `username`, THE Identity_Updater SHALL update the stored `username` to the incoming value.
4. WHEN the sender's stored `display_name` is absent or differs from the incoming Telegram `display_name`, THE Identity_Updater SHALL update the stored `display_name` to the incoming value.
5. WHILE the sender's stored Identity_Fields already equal the incoming Telegram values, THE Identity_Updater SHALL perform no identity write for that message.
6. WHEN the Identity_Updater writes Identity_Fields, THE Identity_Updater SHALL leave the sender's existing Memory_Fields unchanged.
7. IF an identity read or write fails, THEN THE Group_Message_Handler SHALL continue processing the message without raising on the Hot_Path.

### Requirement 2: Per-user group memory carries real identity

**User Story:** As a group participant, I want memories the bot extracts about me to be tied
to my real identity, so that my per-user profile is recognizable rather than empty.

#### Acceptance Criteria

1. WHEN the Group_Extractor creates a User_Profile for a participant during extraction, THE Group_Extractor SHALL populate that profile's Identity_Fields with the participant's captured identity rather than empty-string fallbacks.
2. WHERE a participant's identity was captured by the Identity_Updater from a prior group message, THE Group_Extractor SHALL persist extracted Memory_Fields against the same `user_id` that holds those Identity_Fields.
3. IF a participant's name cannot be resolved to a `sender_id`, THEN THE Group_Extractor SHALL skip that participant's update without creating a misattributed or empty-identity profile.
4. WHEN the Group_Extractor saves a participant's extracted Memory_Fields, THE Group_Extractor SHALL leave that participant's existing Identity_Fields unchanged.

### Requirement 3: Per-user memory used in group replies

**User Story:** As a group participant addressing the bot, I want the bot to use what it
remembers about me along with the shared group context, so that its replies are personalized
to me without losing group awareness.

#### Acceptance Criteria

1. WHEN the Chat_Manager generates a reply on the group path, THE Chat_Manager SHALL load the Per_User_Memory_Block for the Triggering_Sender keyed by that sender's `user_id`.
2. WHEN the Chat_Manager generates a reply on the group path, THE Chat_Manager SHALL load the Group_Memory_Block keyed by the group's `chat_id`.
3. WHEN the Chat_Manager assembles the system prompt on the group path, THE Chat_Manager SHALL include both the Triggering_Sender's Per_User_Memory_Block and the Group_Memory_Block.
4. THE Chat_Manager SHALL load the Per_User_Memory_Block only for the Triggering_Sender and SHALL NOT load per-user memory for non-triggering participants.
5. WHEN the Chat_Manager includes the Triggering_Sender's Per_User_Memory_Block, THE Chat_Manager SHALL retain the Group_Memory_Block rather than replacing it.
6. WHILE the Chat_Manager processes a DM (`chat_type` private), THE Chat_Manager SHALL preserve its existing single-party memory assembly with no per-user/group block combination.
7. IF loading the Triggering_Sender's Per_User_Memory_Block fails, THEN THE Chat_Manager SHALL generate the reply using the Group_Memory_Block without raising on the Hot_Path.

### Requirement 4: Centralized operational logging to the logs channel

**User Story:** As an operator, I want explicit group-memory events and every bot-wide
warning and error forwarded to a configurable logs channel in real time, so that I can
observe the bot's behavior and catch failures without reading server logs.

#### Acceptance Criteria

1. THE Logs_Channel SHALL be identified by a configurable `LOGS_CHANNEL_ID` setting with default value `-1003933328659`, loaded through the existing configuration pattern in `app/config.py`.
2. WHEN the Identity_Updater captures or refreshes a sender's Identity_Fields, THE Log_Forwarder SHALL send an identity event message to the Logs_Channel.
3. WHEN the Group_Extractor saves participant Memory_Fields, THE Log_Forwarder SHALL send a memory-extraction-saved event message to the Logs_Channel.
4. WHEN the Group_Extractor skips a participant update, THE Log_Forwarder SHALL send a memory-extraction-skipped event message to the Logs_Channel.
5. WHEN a log record at severity `WARNING` or higher is emitted anywhere in the bot, THE Error_Log_Sink SHALL forward that log record to the Logs_Channel.
6. WHILE the Error_Log_Sink executes in a logging context that is synchronous or lacks an active asyncio event loop, THE Error_Log_Sink SHALL dispatch the forward onto the bot's event loop without blocking the originating logging call.
7. IF forwarding a log record fails, THEN THE Error_Log_Sink SHALL discard the failure without propagating an exception back into the originating logging call.
8. IF a Log_Forwarder send to the Logs_Channel fails, THEN THE Log_Forwarder SHALL discard the failure without raising on the Hot_Path.
9. THE Error_Log_Sink SHALL exclude log records produced by the Log_Forwarder or by the Error_Log_Sink from forwarding so that forwarding does not trigger further forwarding.
10. WHERE the source chat of an event is the Logs_Channel, THE Log_Forwarder SHALL send no event message for that chat.

### Requirement 5: Invariant preservation

**User Story:** As a maintainer, I want the existing buffer, DM, and error-handling
guarantees to remain intact, so that this feature adds capability without regressing proven
behavior.

#### Acceptance Criteria

1. THE Group_Message_Handler SHALL append each inbound group message to the chat buffer exactly once in accordance with the Single_Write_Invariant.
2. WHILE processing a DM, THE Chat_Manager SHALL produce byte-for-byte-unchanged behavior relative to its pre-feature DM path.
3. WHILE processing a DM, THE Group_Message_Handler SHALL perform no identity capture, per-user group memory combination, or Logs_Channel forwarding.
4. IF any group-memory operation added by this feature fails on a Hot_Path, THEN the affected component SHALL degrade gracefully and SHALL continue message processing without raising.

### Requirement 6: Per-task LLM-call metrics and admin reporting

**User Story:** As an operator, I want the bot to count LLM calls broken down by task type
and let me view that breakdown through an admin command, so that I can see which tasks
consume LLM capacity without reading server logs.

#### Acceptance Criteria

1. WHEN an LLM call completes for any LLM_Task_Type, THE Metrics_Registry SHALL increment the call count recorded for that LLM_Task_Type.
2. WHEN an LLM call completes for any LLM_Task_Type, THE Metrics_Registry SHALL record the call's success-or-failure outcome and its latency for that LLM_Task_Type.
3. THE Metrics_Registry SHALL record call counts for every LLM_Task_Type, including `memory_consolidation` and `proactive_checkin`, with no LLM_Task_Type omitted.
4. WHEN an admin invokes the metrics command, THE Metrics_Reporter SHALL render a report that groups LLM calls by LLM_Task_Type and shows the call count for each LLM_Task_Type.
5. WHERE success, failure, or latency aggregates exist for an LLM_Task_Type, THE Metrics_Reporter SHALL include those aggregate values alongside that LLM_Task_Type's call count in the report.
6. WHILE `ADMIN_USER_IDS` is non-empty, THE Metrics_Reporter SHALL render the report only for a requester whose user id is in `ADMIN_USER_IDS`, in accordance with the Admin_Gate.
7. IF recording an LLM-call metric fails, THEN THE Metrics_Registry SHALL discard the failure without raising into the LLM call site.
8. IF the Metrics_Registry holds no recorded calls for an LLM_Task_Type, THEN THE Metrics_Reporter SHALL render that LLM_Task_Type's count as zero rather than raising.

### Requirement 7: Environment-configurable bot commands

**User Story:** As an operator, I want to remap each command's trigger name and enable or
disable individual commands through environment variables, so that I can tailor the bot's
command surface per deployment without code changes.

#### Acceptance Criteria

1. THE Command_Config SHALL resolve a trigger name for each Built_In_Command from an environment variable, defaulting to that command's current name when the variable is unset.
2. THE Command_Config SHALL resolve an enabled-or-disabled state for each Built_In_Command from an environment variable, defaulting to enabled when the variable is unset.
3. WHILE a Built_In_Command is configured disabled, THE Command_Registry SHALL leave that command unregistered, and the bot SHALL send no response to that command's trigger.
4. WHERE a Built_In_Command's trigger name is remapped, THE Command_Registry SHALL bind that command's existing behavior to the configured trigger name without altering the command's behavior.
5. IF a configured trigger name is invalid or duplicates another Built_In_Command's resolved trigger, THEN THE Command_Config SHALL fall back to the affected command's default trigger name and startup SHALL continue.
6. WHILE an Admin_Command is remapped to a configured trigger name, THE Command_Registry SHALL retain the Admin_Gate authorization on that Admin_Command.
7. IF the command-name configuration cannot be parsed, THEN THE Command_Config SHALL apply default trigger names and the enabled default for the affected commands, and startup SHALL continue without raising.
