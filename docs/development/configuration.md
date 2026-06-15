# Configuration & Tuning Parameter Reference

ThinkMate reads its runtime configuration from **environment variables** — values supplied through the `.env` file in the project root and loaded once at startup by `app/config.py`. This guide is the reference for every one of those variables: what each one does, the value it falls back to when you leave it unset, and how to adjust it safely. Use it to tune conversational behavior, API budgets, batching speeds, memory limits, and security rate limits without changing any application code.

Every setting below is documented in a table with four columns: **Parameter** (the environment-variable name), **Type** (the value's shape — `String`, `Integer`, `Float`, `Bool`, `URL`, or `Path`), **Default** (the value used when the variable is unset), and **Description & How to Adjust** (what the setting controls and the trade-offs of changing it). A handful of terms recur throughout, so they are defined once here:

- **Environment variable** — a key/value pair read from the process environment (populated here from `.env`); a change takes effect on the next restart.
- **TTL (time-to-live)** — an expiry window after which a stored value is automatically dropped, bounding how much state can accumulate.
- **Backoff** — a deliberately growing wait between retries, so a struggling service is not hammered.
- **Master switch** — a single setting that, at its default, keeps an optional subsystem turned off entirely.

The variables are grouped by the subsystem they govern, in the order they appear below:

- **🔑 Credentials & Connection Settings** — Telegram and MongoDB connection details plus audit-log retention.
- **🧠 LLM Server Settings** — the inference endpoint, model selection, and retries.
- **📐 Memory Tuning & Budget Constraints** — buffer sizes and the character budgets that drive extraction and compression.
- **⏱️ Queue & Message Batching** — how the bot groups rapid-fire messages and evicts idle per-user state.
- **🛡️ Input & Output Security Guards** — rate limits and length caps that protect against spam and abuse.
- **👤 Persona Settings** — the persona-file path, the name the bot answers to, and the emoji-reaction switch.
- **👥 Group Chat & Ambient Replies** — how the bot behaves in groups and supergroups.
- **🗣️ Group Chat / Implicit Addressing & Spam** — implicit follow-up detection and mass-tag/greeting-burst defenses.
- **📊 Observability / ops** — in-process metrics and the admin `/health` and `/metrics` commands.
- **⌨️ Commands (rename / disable)** — remap any slash command to a custom trigger or turn it off.
- **🌙 Consolidation (Phase 11)** — the optional background "dreaming" pass over a user's whole profile.
- **💞 Engagement / Mood History (Phase 12)** — the bounded mood-trend history.
- **🔔 Proactive Check-ins (Phase 12)** — the optional, memory-grounded "thinking of you" scheduler.
- **🔌 Connection Pool (advanced)** — MongoDB driver connection-pool tuning.

---

## 🔑 Credentials & Connection Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`TELEGRAM_BOT_TOKEN`** | String | *Required* | **Purpose**: Authenticates your application connection with Telegram's Bot API.<br>**How to Tune**: Obtained from `@BotFather` on Telegram. Update if keys are regenerated or compromised. |
| **`TELEGRAM_PUBLISH_COMMANDS`** | Boolean | `True` | **Purpose**: Controls whether the bot registers its command menu with Telegram on startup.<br>**How to Tune**: Set to `True` (default) to publish the `/` command menu (containing the `/start` entry point) automatically. Set to `False` to prevent the bot from making startup `set_my_commands` API calls to Telegram. |
| **`MONGODB_URI`** | String | `mongodb://localhost:27017` | **Purpose**: The connection string pointing to your MongoDB database server.<br>**How to Tune**: Point to `mongodb://localhost:27017` for local execution. Use `mongodb+srv://...` cluster URIs for cloud databases. |
| **`MONGODB_DB`** | String | `thinkmate_db` | **Purpose**: The target database namespace where profiles and logs are stored.<br>**How to Tune**: Change to separate testing (`thinkmate_test_db`), development (`thinkmate_dev_db`), or production (`thinkmate_prod_db`) collections. |
| **`AUDIT_LOG_RETENTION_DAYS`** | Integer | `30` | **Purpose**: Audit-log entries in `llm_audit_log` auto-expire after this many days via a TTL index, bounding storage growth.<br>**How to Tune**: Lower (e.g. `7`) for tighter retention, raise for longer forensic history. |

---

## 🧠 LLM Server Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`LLM_BASE_URL`** | URL | `http://localhost:1234/v1` | **Purpose**: The root endpoint of the OpenAI-compatible HTTP inference server.<br>**How to Tune**: Point to local hosts (`http://localhost:11434/v1` for Ollama, `http://localhost:1234/v1` for LM Studio) or cloud providers (`https://api.openai.com/v1`, `https://openrouter.ai/api/v1`). |
| **`LLM_API_KEY`** | String | `none` | **Purpose**: The authorization bearer token passed in API headers.<br>**How to Tune**: Set to your provider API key. For local servers that do not require auth, set to mock values like `none` or `lm-studio`. |
| **`LLM_MODEL`** | String | `gpt-4o` | **Purpose**: Identifies the primary LLM model for conversational chat responses.<br>**How to Tune**: Set to a highly conversational, creative model (e.g. `gpt-4o`, `gemma-4-31b-it`). |
| **`LLM_EXTRACTION_MODEL`** | String | *(blank → uses `LLM_MODEL`)* | **Purpose**: Identifies the model for memory extraction and compression tasks.<br>**How to Tune**: Leave blank to reuse `LLM_MODEL`. A smaller, cheaper, faster model (e.g. `gpt-4o-mini`) keeps token costs minimal; `.env.example` ships `gpt-4o-mini` as a suggested value. |
| **`LLM_STRUCTURED_MODE`** | String | `json_object` | **Purpose**: Strategy for structured (JSON) outputs.<br>**How to Tune**: Keep `json_object` for Gemini proxies, Ollama, LM Studio, OpenRouter, and most non-OpenAI backends (they reject the `additionalProperties` field that native parsing emits). Use `native_parse` only on a true OpenAI endpoint to get strict schema validation via `beta.chat.completions.parse`. |
| **`LLM_MAX_RETRIES`** | Integer | `2` | **Purpose**: Retries for transient LLM errors (timeout, connection, 429, 5xx).<br>**How to Tune**: Raise for flaky endpoints; keep low to protect chat responsiveness. |
| **`LLM_RETRY_BASE_DELAY_SECS`** | Float | `0.5` | **Purpose**: Base delay for exponential backoff between retries (`delay = base * 2^attempt`). |

---

## 📐 Memory Tuning & Budget Constraints

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`CHAT_BUFFER_MAX_CHARS`** | Integer | `10000` | **Purpose**: The character threshold of the active conversation buffer before extraction runs.<br>**How to Tune**: Lowering this (e.g., `4000`) triggers extraction sooner, saving smaller chunks to memory. Raising this keeps a longer history in active context before background trim runs. |
| **`NEW_USER_EXTRACTION_CHARS`** | Integer | `1000` | **Purpose**: A lower buffer-char trigger used only for new/sparse users (see `NEW_USER_MEMORY_THRESHOLD`) so a fresh profile starts capturing memories quickly instead of waiting for the full `CHAT_BUFFER_MAX_CHARS`. Effective value is capped at `CHAT_BUFFER_MAX_CHARS`.<br>**How to Tune**: Lower for even faster first-profile building; raise toward `CHAT_BUFFER_MAX_CHARS` to reduce early extraction calls. |
| **`NEW_USER_MEMORY_THRESHOLD`** | Integer | `5` | **Purpose**: Defines "new/sparse": a user whose stored memory items (facts + beliefs + events) number fewer than this uses `NEW_USER_EXTRACTION_CHARS` as the extraction trigger; at or above it, the normal `CHAT_BUFFER_MAX_CHARS` applies.<br>**How to Tune**: Raise to keep the faster cadence for longer; set to `0` to disable the new-user fast path entirely. |
| **`CHAT_BUFFER_TRIM`** | Integer | `10` | **Purpose**: The count of latest messages preserved in active history when a buffer trim is executed.<br>**How to Tune**: Lower to keep prompts concise, or increase to retain a longer dialogue tail right after extraction. |
| **`CHAT_BUFFER_HARD_CAP`** | Integer | `200` | **Purpose**: Absolute maximum number of messages retained in the buffer array, enforced via `$slice`. A safety net so a stalled extractor (e.g. LLM outage) can never let the array grow without bound.<br>**How to Tune**: Keep comfortably above `CHAT_BUFFER_TRIM` and normal buffer sizes. |
| **`USER_MEMORY_BUDGET_CHARS`** | Integer | `4000` | **Purpose**: Caps the compiled memory profile text length. Exceeding this budget triggers compression.<br>**How to Tune**: Lowering forces high-level profiles earlier; raising retains more concrete detail. Keep **≥ ~600** — the empty section-header template alone is ~380 chars, so a budget near/below that can never be satisfied. |
| **`COMPRESSION_COOLDOWN_SECS`** | Float | `300` | **Purpose**: Minimum seconds between compression runs for a given user.<br>**How to Tune**: Prevents repeated compression if a run can't immediately fit the budget. Works alongside deterministic post-compression trimming. |
| **`CHARS_PER_TOKEN`** | Integer | `4` | **Purpose**: Character-to-token ratio. **No longer used to derive output limits** (the `max_tokens` cap was removed); retained for config compatibility.<br>**How to Tune**: Changing it no longer affects generation. |

---

## ⏱️ Queue & Message Batching

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`MESSAGE_BATCH_DELAY_SECS`** | Float | `1.5` | **Purpose**: Delay (in seconds) the bot waits after your last message before replying.<br>**How to Tune**: Set higher (e.g. `2.5`) if you type rapid-fire messages. Set lower (e.g. `0.5`) for instant replies. |
| **`MAX_BATCH_DELAY_SECS`** | Float | `5.0` | **Purpose**: Hard deadline from first message in a batch, forcing reply generation.<br>**How to Tune**: Prevents infinite postpone loops from spammers. Keep around `5.0` seconds to maintain conversational responsiveness. |
| **`USER_STATE_TTL_SECS`** | Float | `1800` | **Purpose**: Idle per-user in-memory state (locks, queues, batch timers) is evicted after this many seconds of inactivity. Bounds memory at large user counts (50k+) on a single instance.<br>**How to Tune**: Lower to reclaim memory faster; raise if you expect long conversational pauses. |

---

## 🛡️ Input & Output Security Guards

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`RATE_LIMIT_MAX_REQUESTS`** | Integer | `5` | **Purpose**: Maximum message requests allowed per user in the rate-limit window.<br>**How to Tune**: Keep at `5` to prevent automated spam loops or DDoS-like API billing charges. |
| **`RATE_LIMIT_WINDOW_SECS`** | Float | `10.0` | **Purpose**: Rate-limit cooling window duration in seconds.<br>**How to Tune**: Increase (e.g. `20.0` or `30.0`) to restrict flooding users further. |
| **`MAX_QUEUED_MESSAGES`** | Integer | `10` | **Purpose**: Caps the batch queue size. Incoming messages beyond this are ignored.<br>**How to Tune**: Restricting this protects your server from memory/concurrency exhaustion under spam attacks. |
| **`MAX_INPUT_CHARS`** | Integer | `2500` | **Purpose**: Inbound messages longer than this are ignored (anti-abuse — blocks pasted logs/essays), **not** a normal chat cap.<br>**How to Tune**: Keep high enough to allow a genuine long share (a story, an experience ≈ 500 words) but low enough to block copy-paste dumps. |
| **`MAX_RESPONSE_CHARS`** | Integer | `2000` | **Purpose**: Legacy soft reference for reply length. **No longer drives a `max_tokens` cap** — reply length is governed by the system-prompt "Length" rule. Retained for config compatibility.<br>**How to Tune**: Changing it no longer affects generation; length is matched to the user's message by the persona. |

---

## 👤 Persona Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`PERSONA_FILE`** | Path | `persona.md` | **Purpose**: Path to the Markdown file defining the bot's tone and traits.<br>**How to Tune**: Default is `persona.md`. Tune if you are hosting multiple bot instances with distinct personalities. |
| **`BOT_NAME`** | String | *(blank → `ThinkMate`)* | **Purpose**: The bot's **universal display name**. It is (a) the name the bot answers to in group chats, matched as a standalone, case-insensitive, word-boundary token (in addition to its auto-detected `@username` mention and reply-to-bot), and (b) the user-facing name shown everywhere else — greetings (`/start`), onboarding (`/onboard`), admin `/health` and `/metrics` headers, and the assistant's attribution in group transcripts. Resolved via `config.bot_display_name` (`BOT_NAME` if set, else `ThinkMate`).<br>**How to Tune**: Set to your bot's name (e.g. `Nova`) to rebrand it everywhere at once. For group **addressing only**, a blank value falls back to the Telegram first name from `get_me()`; the universal display name falls back to `ThinkMate`. The `@username` mention is always auto-detected regardless of this value. |
| **`ENABLE_MESSAGE_REACTIONS`** | Bool | `True` | **Purpose**: Master switch for Telegram emoji reactions on user messages. When `False`, the reaction field of the combined reply call is ignored.<br>**How to Tune**: Disable if a deployment's chats forbid reactions or to save nothing extra (reactions ride the existing reply call, so there is no LLM-cost saving — this is purely behavioral). |

---

## 👥 Group Chat & Ambient Replies

These tune ThinkMate's behavior in groups/supergroups. They have no effect in DMs. See
[group_chat.md](group_chat.md) for the full design.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`GROUP_AMBIENT_COOLDOWN_SECS`** | Float | `90` | **Purpose**: Minimum seconds between *ambient* (un-addressed) chime-ins per group. Caps how often the bot speaks up unprompted.<br>**How to Tune**: Raise to make the bot quieter in busy groups; lower for chattier behavior. The bot still always replies when directly addressed. |
| **`GROUP_AMBIENT_BASE_RATE`** | Float | `0.25` | **Purpose**: Base probability the bot chimes in when a cheap trigger fires, before affinity weighting. Final chance = `base × affinity × mode_factor`.<br>**How to Tune**: Lower for restraint; keep ≤ ~0.4 to avoid feeling spammy. |
| **`GROUP_CONTEXT_SCAN_EVERY`** | Integer | `12` | **Purpose**: Once per cooldown window, after this many group messages, a single affinity-gated context-scan call may run to catch subtler moments keywords miss.<br>**How to Tune**: Raise to reduce ambient LLM calls; lower to make the bot more contextually aware. |
| **`AFFINITY_DEFAULT`** | Float | `0.5` | **Purpose**: Starting affinity (0–1) for a new `{chat_id}:{user_id}` member, scaling how readily the bot engages that person.<br>**How to Tune**: Raise for a friendlier default; lower for a more reserved default. |

---

## 🗣️ Group Chat / Implicit Addressing & Spam

These tune how ThinkMate recognizes follow-up messages that are *implicitly* addressed to it (without
a fresh @mention) and how it defends against mass-tag and greeting-burst spam in groups. They have no
effect in DMs. See [group_chat.md](group_chat.md) for the full design.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`GROUP_IMPLICIT_RECENCY_SECS`** | Float | `120.0` | **Purpose**: Maximum seconds after the bot speaks for a follow-up to still count as implicitly addressed.<br>**How to Tune**: Raise to keep treating follow-ups as directed at the bot for longer; lower to require a fresh mention sooner. |
| **`GROUP_IMPLICIT_RECENCY_MAX_MSGS`** | Integer | `4` | **Purpose**: Maximum intervening human messages since the bot last spoke for an implicit follow-up to still count.<br>**How to Tune**: Raise to tolerate busier interleaving; lower to require a tighter back-and-forth. |
| **`GROUP_IMPLICIT_COOLDOWN_SECS`** | Float | `30.0` | **Purpose**: Minimum seconds between implicit direct replies per group (anti-noise throttle).<br>**How to Tune**: Raise to make implicit replies rarer; lower for snappier follow-ups. |
| **`GROUP_MASS_TAG_SPAM_THRESHOLD`** | Integer | `5` | **Purpose**: Distinct `@mentions` above which a single message is classified as mass-tag spam (strict `>`).<br>**How to Tune**: Lower to flag mass-tagging sooner; raise to allow more legitimate multi-mentions. |
| **`GROUP_SPAM_BURST_SIMILARITY`** | Float | `0.85` | **Purpose**: Mention-stripped similarity ratio (0–1) at/above which messages are treated as near-identical.<br>**How to Tune**: Raise to require closer matches before flagging; lower to catch looser repeats. |
| **`GROUP_SPAM_BURST_COUNT`** | Integer | `3` | **Purpose**: Near-identical messages within the window that classify a greeting burst.<br>**How to Tune**: Raise to tolerate more repeats before flagging; lower to react sooner. |
| **`GROUP_SPAM_BURST_WINDOW_SECS`** | Float | `60.0` | **Purpose**: Time window (seconds) for counting near-identical greeting-burst messages.<br>**How to Tune**: Raise to count repeats over a longer span; lower to require a tighter burst. |
| **`GROUP_SPAM_BURST_TRACK_MAX`** | Integer | `20` | **Purpose**: Hard cap on tracked recent messages per chat, bounding memory used for burst detection.<br>**How to Tune**: Raise to track more history per chat; lower to bound memory more tightly. |

---

## 📊 Observability / ops

These tune the Phase 10 observability layer (in-process metrics, the `/health` and `/metrics`
admin commands, and the optional periodic metrics logger). Both keys have safe defaults so the
bot runs unchanged when they are unset. See [observability.md](observability.md) for the full
metric catalog and runbook.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`ADMIN_USER_IDS`** | String (CSV) | *(blank → DM-only)* | **Purpose**: Comma-separated list of Telegram user ids allowed to use the `/health` and `/metrics` admin commands. Blanks are ignored and each id is coerced to `int`.<br>**How to Tune**: Leave empty to apply the safe default — the commands answer **only in private chats (DMs)** so a status report is never broadcast to a group. Set to specific ids (e.g. `123456789,987654321`) to restrict the commands to those operators in any chat. |
| **`METRICS_LOG_INTERVAL_SECS`** | Float | `0.0` | **Purpose**: Interval, in seconds, for the optional background task that logs the metrics-snapshot summary (no DB/LLM call).<br>**How to Tune**: Keep `0` (or any value ≤ 0) to disable the periodic logger entirely. Set a positive value (e.g. `60`) to emit one summary log line per interval for a lightweight time series in the logs. |
| **`METRICS_PERSIST_INTERVAL_SECS`** | Float | `300.0` | **Purpose**: Interval, in seconds, for flushing the metrics registry to the `metrics_state` MongoDB document so `/health` and `/metrics` survive restarts/crashes (a cheap single-document upsert).<br>**How to Tune**: Keep the default `300` for a 5-minute crash window. Lower it for tighter durability at the cost of more writes; set `0` to disable the *periodic* flush — the startup-load and shutdown-flush still run, so a graceful restart keeps its totals regardless. See [observability.md](observability.md#metrics-persistence-surviving-restarts). |
| **`LOGS_CHANNEL_ID`** | Integer | *None* | **Purpose**: The Telegram channel ID where operational events (startup, shutdown, memory extraction saves/skips, and user profile backups before `/reset`) are forwarded.<br>**How to Tune**: If unset/not present in the environment (default), logging/forwarding to the channel is disabled. Provide a valid integer channel ID (e.g. `-1003933328659`) to enable forwarding. |

---

## ⌨️ Commands (rename / disable)

Every built-in slash command can be **renamed** to a custom trigger or **disabled** entirely, all
from the environment — no code change. This is useful for white-labeling the bot (e.g. mapping
`/start` to `/chatbot`), avoiding command clashes with other bots in a group, or hiding capabilities
you don't want exposed. Both settings have safe defaults (the trigger equals the command key and the
command is enabled), so you only set the ones you want to change.

The live `/help` message is generated from this configuration, so a renamed command appears under its
new trigger and a disabled command is hidden and unregistered (it draws no response at all). The same
configuration drives the native Telegram **"/" command menu**, which is published at startup via
`set_my_commands` (see [telegram_bot.md](telegram_bot.md#️-published-command-menu-set_my_commands)):
personal commands are scoped to DMs and group moderation toggles to group chats, while admin-only
`/health` and `/metrics` are omitted from the public menu.

**The built-in command keys** (in help-display order) are: `start`, `onboard`, `guide`, `pause`,
`resume`, `help`, `profile`, `reset`, `quiet`, `chatty`, `groupon`, `groupoff`, `groupquiet`,
`groupchatty`, `groupnormal`, `health`, `metrics`.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`CMD_<KEY>_NAME`** | String | *(the key)* | **Purpose**: The trigger the command is bound under. `<KEY>` is the upper-cased command key (e.g. `CMD_START_NAME`). A leading `/` is stripped; the name must be 1–32 characters of letters, digits, or underscores (Telegram's command rule).<br>**How to Tune**: Set e.g. `CMD_START_NAME=chatbot` to expose `/start` as `/chatbot`. An invalid name, or one that **duplicates** another enabled command's trigger, safely falls back to the default key with a logged warning (startup never crashes). |
| **`CMD_<KEY>_ENABLED`** | Boolean | `True` | **Purpose**: Whether the command is registered at all.<br>**How to Tune**: Set e.g. `CMD_RESET_ENABLED=False` to remove `/reset` — it is left unregistered and omitted from `/help` and the "/" menu. Admin-only commands (`/health`, `/metrics`) remain admin-gated regardless of any rename. |

**Examples**

```dotenv
CMD_START_NAME=chatbot      # /start is now /chatbot (the bot's main entry point)
CMD_PROFILE_NAME=memories   # /profile is now /memories
CMD_RESET_ENABLED=False     # /reset is removed entirely
```

---

## 🌙 Consolidation (Phase 11)

These tune the periodic background **consolidation** ("dreaming") pass — a long-horizon review of a
user's whole profile that refreshes the summary/style, merges/de-duplicates items, and synthesizes a
small bounded set of durable behavioral **insights**. The pass runs entirely off the hot path under
the shared `memory_lock` and is **disabled by default**. See
[memory_engine.md](memory_engine.md#-phase-11--periodic-consolidation-the-dreaming-pass-implemented) for the full
design.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`CONSOLIDATION_INTERVAL_SECS`** | Float | `0.0` | **Purpose**: The minimum age of a user's last consolidation before they become "due" again — and the master switch for the whole feature.<br>**How to Tune**: Keep `0` (or any value ≤ 0) to **disable the dreaming pass entirely** (the scheduler is never started). Set a positive value to enable it and define the per-user cadence (e.g. `86400` for daily, `604800` for weekly). |
| **`CONSOLIDATION_SCAN_INTERVAL_SECS`** | Float | `3600` | **Purpose**: How often the scheduler wakes to scan for due users (one scan per interval).<br>**How to Tune**: Lower for tighter cadence/quicker pickup; raise to scan less often. Independent of the per-user interval above — a scan only consolidates users who are actually due. |
| **`CONSOLIDATION_MAX_USERS_PER_SCAN`** | Integer | `50` | **Purpose**: Upper bound on how many due users a single scan processes, keeping each scan's work (and LLM volume) bounded.<br>**How to Tune**: Raise to drain a large due backlog faster; lower to spread consolidation cost across more scans. |
| **`CONSOLIDATION_MIN_ITEMS`** | Integer | `8` | **Purpose**: Minimum stored items (`facts + beliefs + events`) a user must have before they are eligible, so the pass never "dreams" over a profile too thin to yield a durable pattern.<br>**How to Tune**: Raise to consolidate only richer profiles; lower to start synthesizing insights sooner. |
| **`MAX_INSIGHTS`** | Integer | `5` | **Purpose**: Hard cap on the dedicated `insights` list. Both the apply step and the prompt honor it, so the list can never grow unbounded.<br>**How to Tune**: Raise for more synthesized observations per user; lower to keep the behavioral-insights section terse. |

---

## 💞 Engagement / Mood History (Phase 12)

This single key tunes **emotional continuity** — the bounded `mood_history` list that lets ThinkMate
render a short mood *trend* (not just the latest mood) in the memory profile. The history is
appended whenever a new `emotional_state` is extracted and is exempt from budget-driven shedding.
See [memory_engine.md](memory_engine.md#-phase-12--temporal-context--emotional-continuity-implemented)
for the full design.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`MAX_MOOD_HISTORY`** | Integer | `10` | **Purpose**: Cap on the number of stored `mood_history` entries per user. Each new emotional state appends one entry and the oldest are dropped once the cap is reached, so the list (and the rendered trend line) stays small and bounded.<br>**How to Tune**: Raise for a longer mood trend, lower for a terser one. Safe at any positive value — it only ever bounds a tiny list. |

---

## 🔔 Proactive Check-ins (Phase 12)

These tune the optional background **proactive check-in** scheduler — a periodic pass that
occasionally sends a single, memory-grounded "thinking of you" nudge to users who have gone quiet
for a while. Like consolidation, it runs entirely **off the hot path**, costs at most one LLM call
per due user per cadence, never sends anything it can't ground in a real, known detail, and is
**disabled by default** (`PROACTIVE_INTERVAL_SECS = 0` is the master switch). Users can opt out at
any time with `/pause` (and back in with `/resume`); see
[telegram_bot.md](telegram_bot.md#-engagement-commands-phase-12-implemented). Full design in
[memory_engine.md](memory_engine.md#-phase-12--temporal-context--emotional-continuity-implemented).

> **Quiet hours are UTC-only.** `PROACTIVE_QUIET_START_HOUR` / `PROACTIVE_QUIET_END_HOUR` are
> interpreted against the **server's UTC clock**, not each user's local timezone — this is a
> documented limitation. Set the window to suit your audience's dominant timezone, or set
> `start == end` to disable the quiet window entirely.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`PROACTIVE_INTERVAL_SECS`** | Float | `0.0` | **Purpose**: How often the scheduler wakes to scan for due users — and the **master switch** for the whole feature.<br>**How to Tune**: Keep `0` (or any value ≤ 0) to **disable proactive check-ins entirely** (the scheduler is never started). Set a positive value (e.g. `3600` to scan hourly) to enable it. |
| **`PROACTIVE_INACTIVITY_SECS`** | Float | `172800` *(2 days)* | **Purpose**: How long a user must have been silent (no interaction) before they become eligible for a check-in.<br>**How to Tune**: Lower to reach out sooner after a lull; raise to wait longer before nudging. |
| **`PROACTIVE_MIN_INTERVAL_SECS`** | Float | `259200` *(3 days)* | **Purpose**: Minimum time between proactive nudges **per user**, so an inactive user is never pestered repeatedly.<br>**How to Tune**: Raise to space nudges further apart; keep comfortably above the scan interval. |
| **`PROACTIVE_MAX_PER_SCAN`** | Integer | `20` | **Purpose**: Upper bound on how many check-ins a single scan sends, keeping each scan's work (and LLM volume) bounded.<br>**How to Tune**: Raise to drain a large due backlog faster; lower to spread the cost across more scans. |
| **`PROACTIVE_MIN_ITEMS`** | Integer | `3` | **Purpose**: Minimum stored items (`facts + beliefs + events`) a user must have before a check-in is attempted, so a nudge is always grounded in something genuine rather than generic filler.<br>**How to Tune**: Raise to reach out only to richer profiles; lower to start nudging sooner. |
| **`PROACTIVE_QUIET_START_HOUR`** | Integer | `22` | **Purpose**: Start of the daily quiet window (**UTC hour**, 0–23) during which scans are skipped so no one is messaged late at night.<br>**How to Tune**: Set with `PROACTIVE_QUIET_END_HOUR` to bracket your audience's night hours in UTC. The window may wrap midnight (e.g. `22`→`7`). Set equal to the end hour to disable quiet hours. |
| **`PROACTIVE_QUIET_END_HOUR`** | Integer | `7` | **Purpose**: End of the daily quiet window (**UTC hour**, 0–23). Scans resume at this hour.<br>**How to Tune**: See `PROACTIVE_QUIET_START_HOUR`. |

---

## 🔌 Connection Pool (advanced)

The `motor` client uses a connection pool (driver default `maxPoolSize=100`). The *concurrently
active* user working set is far smaller than total users, so the default is typically sufficient
even at 50k+ users. If you observe connection saturation under extreme concurrency, raise
`maxPoolSize` in `connection.py` and document the change here. See
[performance_and_scaling.md](performance_and_scaling.md#database-access-patterns--indexes).
