# Requirements Document

Phase 12: Engagement & UX — temporal context, emotional continuity, guided onboarding, and proactive re-engagement.

## Introduction

ThinkMate today is purely **reactive**: it answers when spoken to, builds memory from conversation overflow, and renders a single *current* mood. It has no sense of *when* things happened, no memory of how a user's mood has *trended*, no guided first-run, and no way to gently re-engage someone who has gone quiet. Phase 12 adds four small, additive UX features plus two documented-but-deferred roadmap items, all built strictly on top of the existing hot-path, memory, scheduler, and command patterns already in the codebase.

The four implemented features are:

- **Feature A — Temporal context.** Inject a concise UTC time context (today's date/time and the gap since the user last talked) into the system prompt so the bot can reason about "when". Backed by a new `last_interaction_at` timestamp on the profile, updated cheaply on the DM hot path.
- **Feature B — Emotional continuity.** Track a bounded `mood_history` list (capped at `MAX_MOOD_HISTORY`) alongside the existing single `emotional_state`, so the bot can notice mood *trends* across recent chats.
- **Feature C — Onboarding command.** A static, persona-consistent `/onboard` first-run that introduces ThinkMate and invites a few starter shares so memory builds quickly, plus an `onboarded` flag and a `/start` nudge.
- **Feature D — Proactive check-ins.** A scheduled background job that occasionally sends a contextual, memory-grounded nudge to inactive users — safe, opt-outable (`/pause` / `/resume`), quiet-hours aware, rate-limited, bounded per scan, and **disabled by default**.

The two **future/forward-looking** items (documented in `docs/project_plan.md`, **not** implemented in this phase) are:
- **#3 Relevance-ranked memory selection** — score facts by recency/relevance instead of dumping the whole profile (avoids "lost in the middle").
- **#6 Semantic retrieval over trimmed conversation history** — an embedding store of past segments for recall beyond extracted memory.

Phase 12 mirrors the codebase's established patterns:
- The **proactive scheduler** follows `app/services/health.py::start_consolidation_scheduler` — a single background loop, no-op when its interval ≤ 0, self-healing, bounded per scan, started from `main.py` — but takes the aiogram `bot` so it can **send** messages, and is therefore started **after** `bot` is created.
- The **per-user check-in** uses **one** LLM call grounded in memory (`llm_service.generate_checkin`, mirroring `generate_reply_bundle`'s audit/metrics shape), and returns nothing when there is nothing genuine to say.
- The **due-user query** follows `models.find_users_due_for_consolidation` — a bounded, mongomock-friendly scan.
- The **commands** follow `app/handlers/commands.py` (aiogram `Command` handlers, DB via DI, DM/group awareness like `/quiet` `/chatty`).
- The **config knobs** follow `app/config.py` `_env_*` helpers — all optional, safe defaults, **no new required config**, feature OFF by default.
- The **system-prompt** and **memory-loader** edits are additive and default to current behavior.

## Glossary

- **Hot path**: everything between "a user's batch is ready" and "the reply is sent" in `chat_manager.handle_message`. New work here must be at most one cheap, combined round-trip.
- **`last_interaction_at`**: a UTC timestamp on `user_profiles` recording when the user last sent a DM message; the basis for both the temporal "last talked" gap and proactive inactivity.
- **`mood_history`**: a bounded list of recent mood entries (`{mood, intensity, trigger, detected_at}`), capped at `MAX_MOOD_HISTORY`, appended whenever a new `emotional_state` is written.
- **Check-in / proactive nudge**: a single short, memory-grounded opener the bot sends to an inactive user from the background scheduler (never on the hot path).
- **Due for a check-in**: a user who is inactive long enough, not nagged recently, has enough memory to ground a message, has not opted out, and is not currently within quiet hours.
- **Quiet hours**: a UTC hour window (`PROACTIVE_QUIET_START_HOUR`..`PROACTIVE_QUIET_END_HOUR`) during which no check-ins are sent; `start == end` means no quiet window.
- **Master switch**: `PROACTIVE_INTERVAL_SECS <= 0` disables the entire proactive feature — the scheduler never starts.
- **Defensive read**: reading a new field with `doc.get(field)` / `doc.get(field) or default` so profiles written before Phase 12 work without migration.

---

## Requirements

### Requirement 1: Temporal context in the system prompt (Feature A)

**User Story:** As a user, I want ThinkMate to know what day it is and how long it's been since we talked, so that it can reference time naturally ("been a few days!") instead of being timeless.

#### Acceptance Criteria

1.1 WHERE the system prompt is assembled THE SYSTEM SHALL add an optional `time_context: str = ""` parameter to `build_system_prompt(persona_content, active_memory_text, time_context="")`, additive and keyword-defaulted so every existing two-argument call is unaffected.

1.2 WHEN `time_context` is a non-empty string THEN the system SHALL render it in a clearly-labelled `## ⏰ TIME CONTEXT` section of the prompt; WHEN `time_context` is empty (the default) THEN the system SHALL render no such section and produce byte-for-byte the prior prompt.

1.3 WHERE the DM hot path runs (`chat_manager.handle_message` with `chat_type == "private"`) THE SYSTEM SHALL compute a concise `time_context` string containing today's UTC date/time and the gap since the user's last interaction (e.g. `Last talked: 3 days ago`), and pass it to `build_system_prompt`.

1.4 WHEN the user has no recorded prior interaction (first-ever message, `last_interaction_at` absent) THEN the system SHALL render only the current date/time and SHALL NOT fabricate a gap.

1.5 WHERE the group path runs (`chat_type` in `group`/`supergroup`) THE SYSTEM SHALL pass an empty `time_context` (default) so multi-party reply behavior is unchanged by this feature.

1.6 WHERE the temporal gap is rendered THE SYSTEM SHALL express it in coarse, human units (minutes / hours / days) rather than raw seconds, and SHALL be a pure function of `now` and `last_interaction_at` so it is unit-testable.

---

### Requirement 2: Last-interaction tracking on the hot path (Feature A)

**User Story:** As a maintainer, I want the user's last-interaction time recorded cheaply on the DM path, so that both temporal context and proactive inactivity work without slowing replies.

#### Acceptance Criteria

2.1 WHERE the profile shape changes THE SYSTEM SHALL support a `last_interaction_at` UTC timestamp on `user_profiles`, optional and read defensively so pre-Phase-12 profiles need no migration.

2.2 WHEN a DM message is handled THEN the system SHALL record the user's `last_interaction_at = now` using at most **one** additional Mongo round-trip, achieved by a combined read-then-set helper (`touch_and_get_last_interaction`) that returns the *previous* value (for the gap) and writes the new value in a single `find_one_and_update`.

2.3 WHERE the helper writes THE SYSTEM SHALL NOT upsert a new profile (no `last_interaction_at` write creates a partial document); on the DM path the profile already exists from `/start`, and a user without a profile is simply a no-op.

2.4 WHERE the hot path is concerned THE SYSTEM SHALL add no LLM call and no more than the single combined round-trip described in 2.2, and SHALL keep the existing reply/reaction return contract unchanged.

2.5 WHERE `last_interaction_at` is updated THE SYSTEM SHALL update it only on the DM path (keyed by `user_id == chat_id`), so group activity does not mark a per-user DM interaction.

---

### Requirement 3: Emotional continuity via bounded mood history (Feature B)

**User Story:** As a user, I want ThinkMate to notice how my mood has been trending lately, so that it can say things like "you've seemed stressed the last few chats" instead of only reacting to right now.

#### Acceptance Criteria

3.1 WHERE the profile shape changes THE SYSTEM SHALL support a bounded `mood_history` list on `user_profiles`, each entry shaped `{mood, intensity, trigger, detected_at}`, initialized to `[]` by `ensure_user` and read defensively elsewhere.

3.2 WHEN `save_extracted_memories` writes a new `emotional_state` THEN the system SHALL also append that mood as a new `mood_history` entry in the same write, preserving the existing `emotional_state` overwrite behavior.

3.3 WHERE `mood_history` is appended THE SYSTEM SHALL bound it to at most `MAX_MOOD_HISTORY` entries (default 10), dropping the oldest entries so the list cannot grow without limit.

3.4 WHEN the compiled memory block is built (`compile_memory_text`) THEN the system SHALL render a short "mood trend" (the recent moods, most-recent-last) within or adjacent to the `=== CURRENT MOOD ===` section so the bot can reference trends.

3.5 WHERE a profile has no `mood_history` (legacy profile or none yet) THE SYSTEM SHALL render the mood section exactly as before (no trend line) and SHALL NOT raise.

3.6 WHERE budget enforcement runs THE SYSTEM SHALL treat `mood_history` as its own small, capped list: `memory_compressor._enforce_budget` (which sheds events → beliefs → facts) SHALL NOT need to drop `mood_history`, since the `MAX_MOOD_HISTORY` cap keeps its rendered contribution tiny and bounded.

---

### Requirement 4: Guided onboarding command (Feature C)

**User Story:** As a new user, I want a warm first-run that explains ThinkMate and invites me to share a few things, so that it starts remembering me quickly without feeling like a form.

#### Acceptance Criteria

4.1 WHERE onboarding lives THE SYSTEM SHALL add an `/onboard` command to `app/handlers/commands.py` that sends a single warm, persona-consistent, **plain-text** message introducing ThinkMate and inviting 2–4 light starter shares (e.g. what to call them, what they do, what they're into).

4.2 WHERE the onboarding copy is written THE SYSTEM SHALL keep it conversational per `persona.md` — no bullet lists, numbered lists, markdown, or code formatting — and SHALL require **no** extra LLM call (the message is static).

4.3 WHEN `/onboard` runs THEN the system SHALL set an `onboarded` flag on the user's profile (via a small setter / `ensure_user`) so the state is idempotent-ish, and SHALL NOT block, gate, or alter normal chat handling.

4.4 WHERE the user's onboarding answers arrive as ordinary messages THE SYSTEM SHALL rely on the normal extraction pipeline to capture them and SHALL NOT perform any bespoke extraction in the command.

4.5 WHEN `/start` runs THEN the system SHALL mention `/onboard`, and SHALL auto-suggest onboarding **only** when the profile is not yet `onboarded` (a returning, onboarded user gets no nudge).

4.6 WHERE `/help` lists commands THE SYSTEM SHALL include `/onboard` (and the new `/pause` / `/resume`) so they are discoverable.

---

### Requirement 5: Proactive check-in scheduler (Feature D, off the hot path, disabled by default)

**User Story:** As an operator, I want an opt-in background job that occasionally re-engages inactive users, so that ThinkMate can feel like a friend who reaches out — without ever touching the reply path or running unless I enable it.

#### Acceptance Criteria

5.1 WHERE the scheduler lives THE SYSTEM SHALL provide `start_proactive_scheduler(bot)` in `app/services/health.py`, mirroring `start_consolidation_scheduler` but taking the aiogram `bot` (needed to send), starting at most one background task and returning it.

5.2 WHEN `PROACTIVE_INTERVAL_SECS <= 0` THEN the system SHALL NOT start the scheduler task and SHALL return `None` — this is the master switch and the default, so the entire feature is inert unless explicitly enabled.

5.3 WHERE the scheduler is started THE SYSTEM SHALL start it in `main.py` **after** `bot = Bot(...)` is created (passing `bot`), and SHALL log that it started when enabled and start nothing when disabled.

5.4 WHEN the scheduler is enabled THEN it SHALL wake every `PROACTIVE_INTERVAL_SECS` seconds, find due users, and dispatch at most `PROACTIVE_MAX_PER_SCAN` check-ins per wake (bounded work).

5.5 WHEN a single user's check-in raises THEN the scheduler SHALL log it and continue with the remaining due users (one failure must not abort the scan).

5.6 WHEN any scan iteration encounters an unexpected error THEN the loop SHALL log and continue on its next interval and SHALL NEVER crash the process (self-healing, mirroring `_consolidation_loop`); cancellation SHALL break the loop cleanly.

5.7 WHERE a send could throw THE SYSTEM SHALL ensure a failed `bot.send_message` (e.g. the user blocked the bot / `Forbidden`) never crashes the scheduler.

---

### Requirement 6: Due-for-check-in selection & safety gating (Feature D)

**User Story:** As a user, I want any proactive message to be rare, well-timed, and never spammy, so that ThinkMate reaching out feels caring rather than annoying.

#### Acceptance Criteria

6.1 WHERE due users are found THE SYSTEM SHALL add `find_users_due_for_proactive(db, *, inactivity_secs, min_interval_secs, limit, now)` returning at most `limit` user ids, bounding its own work (stops once `limit` qualifying users are collected) and remaining mongomock-friendly (no array-length query operators).

6.2 WHERE inactivity is required THE SYSTEM SHALL select a user only when `last_interaction_at` is present AND older than `now - inactivity_secs` (a user who never interacted is never selected).

6.3 WHERE the rate limit is enforced THE SYSTEM SHALL select a user only when `last_proactive_at` is null/absent OR older than `now - min_interval_secs`, so a user is never nudged more often than `PROACTIVE_MIN_INTERVAL_SECS`.

6.4 WHERE grounding is required THE SYSTEM SHALL select a user only when they have at least `PROACTIVE_MIN_ITEMS` total facts+beliefs+events (applied in Python), so a check-in can be grounded in real memory.

6.5 WHERE per-user opt-out is honored THE SYSTEM SHALL exclude any user whose `proactive_enabled` is explicitly `False`, while treating an absent/true flag as eligible.

6.6 WHERE quiet hours apply THE SYSTEM SHALL NOT send check-ins when the current UTC hour is within `[PROACTIVE_QUIET_START_HOUR, PROACTIVE_QUIET_END_HOUR)` (supporting a window that wraps midnight); WHEN `PROACTIVE_QUIET_START_HOUR == PROACTIVE_QUIET_END_HOUR` THEN there is no quiet window. The quiet-hours check is a pure function of the current hour and the two bounds, and is evaluated per scan so quieted users are simply picked up on a later scan.

6.7 WHERE the timezone model is concerned THE SYSTEM SHALL evaluate quiet hours in UTC only and SHALL document this limitation (no per-user timezone in this phase).

---

### Requirement 7: Check-in generation & delivery (Feature D)

**User Story:** As a user, I want a proactive message to reference something real about me and to never be a hollow or made-up greeting, so that it feels genuine.

#### Acceptance Criteria

7.1 WHERE the check-in prompt lives THE SYSTEM SHALL add `app/prompts/checkin_prompt.py` with a persona-consistent instruction that asks for a short, natural opener grounded in a real memory (a known upcoming/recent event, insight, current mood, or profile detail), and that explicitly permits returning **nothing** when there is no genuine detail to reference.

7.2 WHERE the generation method lives THE SYSTEM SHALL add `llm_service.generate_checkin(user_id, system_prompt, memory_text) -> str | None`, making exactly **one** LLM call, returning the opener text on success and `None`/empty when the model declines or there is nothing genuine to say.

7.3 WHERE fabrication is forbidden THE SYSTEM SHALL treat an empty result, a decline sentinel, or an ungroundable profile as "send nothing" and SHALL never send a fabricated or empty message.

7.4 WHERE auditing and metrics apply THE SYSTEM SHALL route `generate_checkin` through the existing `_fire_log` audit path with a distinct `call_type` (e.g. `proactive_checkin`) so per-type LLM metrics (`llm.proactive_checkin.*`) are derived for free, and SHALL never raise into the scheduler (it returns `None` on failure).

7.5 WHEN a check-in opener is produced THEN the system SHALL deliver it via `bot.send_message(chat_id=user_id, text=...)` (in a DM `chat_id == user_id`).

7.6 WHEN a check-in is delivered successfully THEN the system SHALL append it to the user's chat buffer as an assistant message via `add_message_to_buffer(chat_id, "assistant", text, sender_id=0, sender_name="ThinkMate")`, so the conversation context includes it and a user reply flows normally; this buffer write is done without `memory_lock` (it is not memory work, matching how replies are appended).

7.7 WHEN a check-in is attempted THEN the system SHALL set `last_proactive_at = now` regardless of whether a message was actually sent (an empty/declined result still holds the rate-limit window so a due user is not re-selected next scan).

7.8 WHEN `bot.send_message` fails (e.g. `Forbidden` because the user blocked the bot) THEN the system SHALL catch and log the error, SHALL set `proactive_enabled = False` for that user so a blocked user is never nagged again, and SHALL still respect the rate-limit window.

---

### Requirement 8: Opt-out commands /pause and /resume (Feature D)

**User Story:** As a user, I want to turn proactive check-ins off and on, so that I stay in control of whether ThinkMate reaches out to me.

#### Acceptance Criteria

8.1 WHERE the opt-out commands live THE SYSTEM SHALL add `/pause` and `/resume` to `app/handlers/commands.py`, toggling `proactive_enabled` to `False` and `True` respectively on the user's profile via a small setter.

8.2 WHEN `/pause` runs in a DM THEN the system SHALL set `proactive_enabled = False` and confirm in a warm, persona-consistent line; WHEN `/resume` runs in a DM THEN it SHALL set `proactive_enabled = True` and confirm.

8.3 WHERE these commands are used in a group THE SYSTEM SHALL either no-op with a short explanation or apply harmlessly, consistent with how `/quiet` `/chatty` explain themselves in DMs — proactive check-ins are a DM-oriented feature.

8.4 WHERE discoverability is required THE SYSTEM SHALL list `/pause` and `/resume` in `/help`.

8.5 WHERE opt-out interacts with selection THE SYSTEM SHALL ensure a `proactive_enabled = False` user is excluded from `find_users_due_for_proactive` (Requirement 6.5), so pausing reliably stops nudges.

---

### Requirement 9: Configuration knobs (optional, safe defaults, feature OFF by default)

**User Story:** As an operator, I want every new behavior to be configurable with safe defaults and off by default, so that upgrading to Phase 12 changes nothing until I opt in.

#### Acceptance Criteria

9.1 WHERE engagement config lives THE SYSTEM SHALL add the following optional keys to `app/config.py` via the existing `_env_*` helpers, each with a safe default and introducing no new *required* config:
- `MAX_MOOD_HISTORY: int` — cap on stored mood entries; default `10` (Feature B).
- `PROACTIVE_INTERVAL_SECS: float` — scheduler scan period; **default `0.0` = disabled (master switch)**.
- `PROACTIVE_INACTIVITY_SECS: float` — how long a user must be inactive to be due; default `172800` (2 days).
- `PROACTIVE_MIN_INTERVAL_SECS: float` — minimum time between nudges per user; default `259200` (3 days).
- `PROACTIVE_MAX_PER_SCAN: int` — max check-ins per scan; default `20`.
- `PROACTIVE_MIN_ITEMS: int` — minimum facts+beliefs+events to ground a check-in; default `3`.
- `PROACTIVE_QUIET_START_HOUR: int` — quiet window start hour (UTC); default `22`.
- `PROACTIVE_QUIET_END_HOUR: int` — quiet window end hour (UTC); default `7`.

9.2 WHERE the feature is off by default THE SYSTEM SHALL ensure that with `PROACTIVE_INTERVAL_SECS = 0.0` (the default) no scheduler runs, no check-in LLM call is made, and no message is ever sent — behavior is identical to Phase 11.

9.3 WHEN any new knob is unset in the environment THEN the system SHALL fall back to the documented default rather than failing to start.

9.4 WHERE the new keys are introduced THE SYSTEM SHALL document them in `docs/development/configuration.md` and mirror them in `.env.example`, consistent with `.agents/rules/document_changes.md`.

---

### Requirement 10: Data model & CRUD

**User Story:** As a developer, I want the persistence helpers these features need, so that hot-path touches, mood appends, opt-out toggles, and due-user selection are atomic, single-write, and test-friendly.

#### Acceptance Criteria

10.1 WHERE the profile shape changes THE SYSTEM SHALL support `last_interaction_at`, `mood_history`, `onboarded`, `last_proactive_at`, and `proactive_enabled` on `user_profiles`, all additive and read defensively so pre-Phase-12 documents need no migration; `ensure_user` SHALL initialize `mood_history: []` and `onboarded: False` on insert (`$setOnInsert`).

10.2 WHERE the hot-path touch lives THE SYSTEM SHALL add `touch_and_get_last_interaction(db, user_id, *, now=None) -> datetime | None` that, in a single `find_one_and_update` (no upsert), returns the previous `last_interaction_at` and sets it to `now`.

10.3 WHERE mood history is written THE SYSTEM SHALL append the new mood entry inside the existing `save_extracted_memories` `$set` write and truncate the list to the last `config.MAX_MOOD_HISTORY` entries.

10.4 WHERE opt-out and onboarding state are written THE SYSTEM SHALL add small setters `set_proactive_enabled(db, user_id, enabled)`, `set_onboarded(db, user_id, value=True)`, and `set_last_proactive(db, user_id, *, now=None)`, each a single `$set` write that does not clobber unrelated fields.

10.5 WHERE due users are found THE SYSTEM SHALL implement `find_users_due_for_proactive(db, *, inactivity_secs, min_interval_secs, limit, now)` per Requirement 6 (inactivity, rate-limit, opt-out via query; min-items grounding applied in Python using `config.PROACTIVE_MIN_ITEMS`), returning at most `limit` ids and bounding its own iteration.

---

### Requirement 11: Metrics & observability

**User Story:** As an operator, I want proactive activity surfaced through the existing metrics registry and logs, so that I can see how often check-ins run, send, skip, and fail.

#### Acceptance Criteria

11.1 WHEN a proactive scan runs THEN the system SHALL `metrics.incr("proactive.runs")` once per wake.

11.2 WHEN a check-in is sent THEN the system SHALL `metrics.incr("proactive.sent")`; WHEN a check-in is skipped (empty/declined result) THEN it SHALL `metrics.incr("proactive.skipped")`; WHEN a send fails THEN it SHALL `metrics.incr("proactive.failed")`.

11.3 WHEN a scan completes THEN it SHALL log a single summary line of due/sent/skipped/failed counts.

11.4 WHERE LLM metrics apply THE SYSTEM SHALL surface check-in LLM volume/latency as `llm.proactive_checkin.*` for free via `metrics.record_llm` / `_structured_call`-style accounting, and SHALL never let a metrics failure break a scan (mirrors Phase 10).

---

### Requirement 12: Backward-compatibility & safety

**User Story:** As a maintainer, I want Phase 12 to be additive and safe, so that all existing behavior and tests are preserved and the new behaviors cannot harm a running instance.

#### Acceptance Criteria

12.1 WHERE the proactive feature is disabled by default THE SYSTEM SHALL leave all existing behavior unchanged when `PROACTIVE_INTERVAL_SECS = 0.0`, and the existing test suite SHALL pass unmodified.

12.2 WHERE the DM hot path is concerned THE SYSTEM SHALL change reply behavior only by (a) the single combined `last_interaction_at` round-trip (Requirement 2.2) and (b) an additive, default-empty `time_context` — no extra LLM call, same reply/reaction contract.

12.3 WHERE proactive sending happens THE SYSTEM SHALL use the `bot` instance only inside the scheduler (off the hot path), and a send failure SHALL never crash the scheduler (Requirements 5.7, 7.8).

12.4 WHERE nagging is prevented THE SYSTEM SHALL combine opt-out, per-user rate limit, quiet hours, bounded per-scan dispatch, and the never-send-empty rule so a user can never be spammed.

12.5 WHERE new fields and arrays are introduced THE SYSTEM SHALL read them defensively (`doc.get(...)` / `or []`) so existing profiles continue to work without a migration, and all new config SHALL be optional with safe defaults.

12.6 WHERE the buffer append after a check-in happens THE SYSTEM SHALL perform it without `memory_lock` (matching how replies are appended), and SHALL ensure it never blocks or races the hot path.

---

### Requirement 13: Test coverage & documentation (incl. deferred roadmap)

**User Story:** As a maintainer, I want hermetic tests for every new surface and clear docs (including the two deferred items), so that the engagement layer is verified and the roadmap is honest about what is and isn't built.

#### Acceptance Criteria

13.1 WHERE tests are written THE SYSTEM SHALL use **mongomock + pytest-asyncio** per `tests/conftest.py` (async mock wrappers, autouse DB patch), with no real LLM or network; the LLM SHALL be patched with `AsyncMock`, config SHALL be saved/restored (as in `tests/test_hardening.py`), `metrics.reset()` SHALL isolate metric state, and background tasks SHALL be cancelled/awaited in `finally` so none leak (as in `tests/test_metrics_logger.py`).

13.2 WHEN the temporal feature is tested THEN the tests SHALL assert `build_system_prompt` is unchanged when `time_context=""` and renders the `## ⏰ TIME CONTEXT` section when non-empty, that the gap function is correct/coarse and absent for first-ever interactions, and that the DM hot path records `last_interaction_at` in a single combined round-trip without adding an LLM call.

13.3 WHEN the mood-history feature is tested THEN the tests SHALL assert `save_extracted_memories` appends a `mood_history` entry bounded to `MAX_MOOD_HISTORY`, that `compile_memory_text` renders the trend (and behaves as before with no history), and that `ensure_user` initializes `mood_history` to `[]`.

13.4 WHEN onboarding and opt-out commands are tested THEN the tests SHALL use a mocked aiogram `Message` (as in `tests/test_command_skip.py`) to assert `/onboard` sets `onboarded`, sends static plain-text copy, and does not block chat; that `/start` nudges only when not onboarded; and that `/pause`/`/resume` toggle `proactive_enabled`.

13.5 WHEN the proactive scheduler/sender is tested THEN the tests SHALL assert `start_proactive_scheduler(bot)` returns `None` when disabled; that `find_users_due_for_proactive` honors inactivity, rate-limit, opt-out, min-items, and `limit`; that quiet hours suppress sending; that a sent check-in calls `bot.send_message` (mocked `AsyncMock`) and appends to the buffer and sets `last_proactive_at`; that an empty/declined result sends nothing but still sets `last_proactive_at`; that a `Forbidden` send sets `proactive_enabled=False`; and that one user's failure and a raising scan iteration are both survived (self-healing).

13.6 WHEN the full suite is run after Phase 12 THEN every previously passing test SHALL still pass with no warnings and no external services, with proactive **disabled by default**.

13.7 WHERE the roadmap is documented THE SYSTEM SHALL record the two deferred items in `docs/project_plan.md` as explicit future/forward-looking entries — **#3 Relevance-ranked memory selection** and **#6 Semantic retrieval over trimmed conversation history** — clearly marked as not yet implemented.
