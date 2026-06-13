# Requirements Document

Phase 9: Group chat, ambient replies & affinity.

## Introduction

ThinkMate currently behaves as a DM-only companion: every conversational message in a private chat is replied to, batched per user, and folded into a per-user memory pipeline. Phase 9 extends ThinkMate into Telegram groups and supergroups without spamming members or abusing the LLM, while keeping DM behavior byte-for-byte identical.

The build target is fully specified in `docs/development/group_chat.md` and the Phase 9 section of `docs/project_plan.md`. The configuration knobs (`GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`, `GROUP_CONTEXT_SCAN_EVERY`, `AFFINITY_DEFAULT`) already exist in `app/config.py`.

Core ideas:
- Buffers become keyed by `chat_id` (in a DM, `chat_id == user_id`, so DMs are unchanged on disk). Each buffered message gains `sender_id` + `sender_name` for multi-party context.
- In a group, ThinkMate replies when **addressed** (mentioned, named, or replied-to); otherwise it runs the **ambient gate** — a no-LLM funnel (per-chat cooldown → cheap trigger scan → affinity-weighted dice roll) that admits at most one ambient LLM call per active group per cooldown window.
- Per-(chat, user) **affinity** and **mode** live in a new `chat_members` collection, cached in memory and written through on change. `/quiet` and `/chatty` set the mode.
- Memory (facts/beliefs/events) stays per `user_id`; group extraction is multi-party (one LLM call, updates tagged by participant name, mapped back to each `sender_id`).
- Channels are ignored.

## Glossary

- **Addressed message**: a group message that @mentions the bot, uses the bot's name, or is a reply to one of the bot's own messages.
- **Ambient gate**: the no-LLM funnel that decides whether to chime in on a non-addressed group message.
- **Member record**: a `chat_members` document keyed `"{chat_id}:{user_id}"` holding `affinity` (0–1) and `mode` (`auto`/`quiet`/`chatty`).
- **Cooldown window**: the per-chat interval (`GROUP_AMBIENT_COOLDOWN_SECS`) during which at most one ambient chime-in is allowed.
- **mode_factor**: the multiplier applied to ambient probability by a member's mode (`quiet` → 0, `auto` → 1, `chatty` → > 1).

---

## Requirements

### Requirement 1: DM preservation (backward compatibility)

**User Story:** As an existing DM user, I want ThinkMate to behave exactly as it does today, so that the group-chat work introduces no regressions.

#### Acceptance Criteria

1.1 WHEN a conversational (non-command) message is received in a private chat THEN the system SHALL reply exactly as it does today (one reply call, optional reaction, batching, length guard, memory pipeline) with no behavioral change.

1.2 WHERE the chat is a private chat THE SYSTEM SHALL treat `chat_id` as equal to `user_id` so that the on-disk `chat_buffers` document (`_id`) and the `user_profiles` document are unchanged from current behavior.

1.3 WHEN a bot (slash) command is received in a private chat THEN the system SHALL continue to exclude it from conversation handling (no reply, no enqueue), preserving the existing command-skip behavior.

1.4 WHEN a message in a private chat exceeds `MAX_INPUT_CHARS` THEN the system SHALL apply the existing length guard unchanged (deflect, do not enqueue, do not call the LLM).

1.5 WHERE the chat is a private chat THE SYSTEM SHALL NOT run the ambient gate, the addressed-detection branch, or any affinity logic — the DM path SHALL reply to every conversational message as before.

1.6 WHEN the existing DM test suite is run after Phase 9 changes THEN every previously passing test SHALL still pass without modification.

1.7 WHERE a buffered message originates in a private chat THE SYSTEM SHALL still attach `sender_id` and `sender_name`, both equal to the single DM user, without changing the rendered single-party history seen by the reply call.

---

### Requirement 2: Group routing & identity

**User Story:** As a group member, I want ThinkMate to reply when I clearly talk to it and to record the conversation for context, so that it is useful without being intrusive.

#### Acceptance Criteria

2.1 WHEN any message is received in a group or supergroup THEN the system SHALL record it to the `chat_id`-keyed buffer with `role`, `sender_id`, `sender_name`, and `content` before deciding whether to reply.

2.2 WHEN a group message @mentions the bot's username THEN the system SHALL classify it as addressed and SHALL generate a reply.

2.3 WHEN a group message is a reply to one of the bot's own previous messages THEN the system SHALL classify it as addressed and SHALL generate a reply.

2.4 WHEN a group message contains the bot's configured name as a standalone token THEN the system SHALL classify it as addressed and SHALL generate a reply.

2.5 IF a group message is not addressed THEN the system SHALL NOT reply directly and SHALL instead pass the message to the ambient gate.

2.6 WHERE the update originates from a channel THE SYSTEM SHALL ignore it entirely (no buffer write, no reply, no memory work).

2.7 WHEN rendering group history for an LLM call THE SYSTEM SHALL present messages as multi-party (attributed by `sender_name`, e.g. "Alice: …", "Bob: …") so the model can distinguish speakers.

2.8 WHEN a registered command (`/start`, `/help`, `/profile`, `/reset`, `/quiet`, `/chatty`) is received in a group THEN the system SHALL route it to its command handler and SHALL NOT treat it as conversation or an ambient trigger.

---

### Requirement 3: Ambient gate (never abuses the LLM)

**User Story:** As a group member, I want ThinkMate to occasionally and organically chime in on the wider conversation, so that it feels alive without being spammy or expensive.

#### Acceptance Criteria

3.1 WHEN a non-addressed group message enters the ambient gate AND the per-chat cooldown window (`GROUP_AMBIENT_COOLDOWN_SECS`) has not elapsed since the last ambient chime-in THEN the system SHALL stop without any LLM call.

3.2 WHEN a non-addressed group message passes the cooldown check THEN the system SHALL run a cheap keyword/regex trigger scan (no LLM) for moments such as birthdays, congratulations, laughter, group questions, greetings, or strong sentiment.

3.3 IF the trigger scan finds no match AND the current message is not a periodic context-scan tick (per `GROUP_CONTEXT_SCAN_EVERY`) THEN the system SHALL stop without any LLM call.

3.4 WHEN a candidate survives the trigger/scan step THEN the system SHALL compute a chime-in probability of `GROUP_AMBIENT_BASE_RATE × affinity × mode_factor` and SHALL perform a single random dice roll against it; on failure it SHALL stop without any LLM call.

3.5 WHERE the member's `mode` is `quiet` THE SYSTEM SHALL treat the ambient probability as 0 (mode_factor = 0) and SHALL never chime in ambiently for that member.

3.6 WHEN a candidate passes the dice roll THEN the system SHALL make at most one LLM call to craft a short chime-in, and IF the model returns empty THEN the system SHALL send nothing.

3.7 WHEN an ambient chime-in LLM call is made (whether or not it produces text) THEN the system SHALL reset the per-chat cooldown so that subsequent messages are gated for at least `GROUP_AMBIENT_COOLDOWN_SECS`.

3.8 WHILE a group is active THE SYSTEM SHALL ensure the ambient path costs at most approximately one ambient LLM call per cooldown window for that group, regardless of message volume.

3.9 WHERE the periodic context-scan is used THE SYSTEM SHALL gate it by the same cooldown and by affinity, so the hybrid path does not exceed the one-call-per-window budget.

3.10 WHEN the in-memory per-chat cooldown state grows THEN the system SHALL keep it bounded (self-pruning / eviction) so it cannot grow without limit across many groups.

---

### Requirement 4: Affinity store & signals

**User Story:** As a group member, I want ThinkMate to learn how much I welcome its participation, so that it chats more with people who engage and backs off from people who do not.

#### Acceptance Criteria

4.1 WHERE per-(chat, user) affinity is needed THE SYSTEM SHALL store it in a `chat_members` collection keyed `"{chat_id}:{user_id}"` with fields `affinity` (0–1, default `AFFINITY_DEFAULT`) and `mode` (`auto`/`quiet`/`chatty`).

4.2 WHEN a member record is read on the ambient path THEN the system SHALL serve it from an in-memory read-through cache, falling back to a single DB read (and default creation) on a cache miss, so the hot path adds no per-message DB read after warm-up.

4.3 WHEN an affinity or mode value changes THEN the system SHALL write the change through to the `chat_members` document (write-through) and update the cache.

4.4 WHEN a member mentions or replies to the bot, or engages immediately after an ambient chime-in THEN the system SHALL increase that member's affinity (bounded at 1.0).

4.5 WHEN a member's message matches the cheap "stop / quiet / spam / annoying / shut up" keyword detector THEN the system SHALL decrease that member's affinity (bounded at 0.0), using no extra LLM call.

4.6 WHEN a reply or chime-in LLM call returns an optional `affinity_delta` field THEN the system SHALL fold that delta into the member's affinity (bounded to 0–1) at no additional LLM cost.

4.7 WHERE affinity is clamped THE SYSTEM SHALL keep every stored affinity value within the inclusive range 0.0 to 1.0.

4.8 WHERE the chat is a private chat THE SYSTEM SHALL NOT create or consult `chat_members` records (affinity has no effect on DMs).

---

### Requirement 5: Multi-party group memory extraction

**User Story:** As a group member, I want ThinkMate to remember facts about each person correctly, so that what Alice said is attributed to Alice and not to Bob.

#### Acceptance Criteria

5.1 WHEN a group buffer segment is extracted THEN the system SHALL make a single LLM extraction call over the multi-party segment (one call, not one per participant).

5.2 WHEN the multi-party extraction returns updates tagged by participant name THEN the system SHALL map each tagged update back to the correct `sender_id` using the segment's own name→id map.

5.3 WHEN extracted updates are mapped to `sender_id`s THEN the system SHALL save each participant's facts/beliefs/events to that participant's per-`user_id` profile using the existing memory-write path.

5.4 IF a tagged participant name cannot be resolved to a `sender_id` in the segment's map THEN the system SHALL skip that update rather than misattribute it.

5.5 WHERE the chat is a private chat THE SYSTEM SHALL continue to use the existing single-party extraction path unchanged.

5.6 WHEN group extraction completes THEN the system SHALL trim the processed buffer segment using the existing atomic, bounded trim behavior (no clobbering of concurrently appended messages).

---

### Requirement 6: Commands (/quiet, /chatty)

**User Story:** As a group member, I want explicit control over how chatty ThinkMate is with me, so that I can quiet it down or invite more participation.

#### Acceptance Criteria

6.1 WHEN a member sends `/quiet` in a group THEN the system SHALL set that member's `mode` to `quiet`, write it through to `chat_members`, and suppress ambient chime-ins for that member.

6.2 WHEN a member sends `/chatty` in a group THEN the system SHALL set that member's `mode` to `chatty`, write it through, and boost that member's ambient probability (mode_factor > 1).

6.3 WHEN `/quiet` or `/chatty` is sent in a private chat THEN the system SHALL respond gracefully (e.g. an explanatory message) without creating group affinity state, since these commands govern group behavior.

6.4 WHEN `/quiet` or `/chatty` is handled THEN the system SHALL acknowledge the mode change to the member.

6.5 WHERE `/quiet` and `/chatty` are registered THE SYSTEM SHALL ensure they are routed as commands and never treated as conversational text or ambient triggers.

---

### Requirement 7: Configuration & observability

**User Story:** As an operator, I want the group behavior to be tunable and observable, so that I can keep ThinkMate within its LLM budget and diagnose its decisions.

#### Acceptance Criteria

7.1 WHERE group behavior is tuned THE SYSTEM SHALL read `GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`, `GROUP_CONTEXT_SCAN_EVERY`, and `AFFINITY_DEFAULT` from `config` rather than hardcoded literals.

7.2 WHEN an ambient candidate is dropped at any funnel stage (cooldown, trigger scan, dice roll, empty reply) THEN the system SHALL emit a log record identifying the stage, so the funnel is observable.

7.3 WHEN an ambient chime-in LLM call is made THEN the system SHALL audit it via the existing `llm_audit_log` path (off the hot path, fire-and-forget) just like other LLM calls.

7.4 WHERE the ambient and affinity LLM calls are made THE SYSTEM SHALL respect the hot-path invariants in `performance_and_scaling.md`: cheap scans before any LLM call, and at most ~1 ambient LLM call per active group per cooldown window.

7.5 WHEN affinity changes via any signal THEN the system SHALL log the signal type and resulting affinity at debug level for traceability.
