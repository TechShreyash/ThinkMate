# Configuration & Tuning Parameter Reference

This guide provides a detailed description of all environment variables configured in the `.env` file of the ThinkMate system. Use this reference to tune conversational behavior, API budgets, batching speeds, and security rate limits.

---

## 🔑 Credentials & Connection Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`TELEGRAM_BOT_TOKEN`** | String | *Required* | **Purpose**: Authenticates your application connection with Telegram's Bot API.<br>**How to Tune**: Obtained from `@BotFather` on Telegram. Update if keys are regenerated or compromised. |
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
| **`REPLY_TEMPERATURE`** | Float | `0.7` | **Purpose**: Sampling temperature for conversational replies.<br>**How to Tune**: Lower (e.g. `0.5`) for steadier replies, higher (e.g. `0.9`) for more variety. |
| **`EXTRACTION_TEMPERATURE`** | Float | `0.1` | **Purpose**: Sampling temperature for memory extraction/compression. Kept low for deterministic, faithful structured output. |

---

## 📐 Memory Tuning & Budget Constraints

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`CHAT_BUFFER_MAX_CHARS`** | Integer | `10000` | **Purpose**: The character threshold of the active conversation buffer before extraction runs.<br>**How to Tune**: Lowering this (e.g., `4000`) triggers extraction sooner, saving smaller chunks to memory. Raising this keeps a longer history in active context before background trim runs. |
| **`CHAT_BUFFER_TRIM`** | Integer | `10` | **Purpose**: The count of latest messages preserved in active history when a buffer trim is executed.<br>**How to Tune**: Lower to keep prompts concise, or increase to retain a longer dialogue tail right after extraction. |
| **`CHAT_BUFFER_HARD_CAP`** | Integer | `200` | **Purpose**: Absolute maximum number of messages retained in the buffer array, enforced via `$slice`. A safety net so a stalled extractor (e.g. LLM outage) can never let the array grow without bound.<br>**How to Tune**: Keep comfortably above `CHAT_BUFFER_TRIM` and normal buffer sizes. |
| **`USER_MEMORY_BUDGET_CHARS`** | Integer | `4000` | **Purpose**: Caps the compiled memory profile text length. Exceeding this budget triggers compression.<br>**How to Tune**: Lowering forces high-level profiles earlier; raising retains more concrete detail. Keep **≥ ~600** — the empty section-header template alone is ~380 chars, so a budget near/below that can never be satisfied. |
| **`COMPRESSION_COOLDOWN_SECS`** | Float | `300` | **Purpose**: Minimum seconds between compression runs for a given user.<br>**How to Tune**: Prevents repeated compression if a run can't immediately fit the budget. Works alongside deterministic post-compression trimming. |
| **`CHARS_PER_TOKEN`** | Integer | `4` | **Purpose**: Character-to-token ratio used to derive output limits.<br>**How to Tune**: Default is `4`. Increase if you converse in languages that require higher token sizes (e.g. Cyrillic or East Asian). |

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
| **`MAX_RESPONSE_CHARS`** | Integer | `2000` | **Purpose**: A generous safety ceiling for reply length (drives `max_tokens`). It is **not** a per-message target — actual length is matched to the user's message (see the system-prompt "Length" rule).<br>**How to Tune**: Raise if long replies get truncated; lower only if you want a firmer ceiling. |

---

## 👤 Persona Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`PERSONA_FILE`** | Path | `persona.md` | **Purpose**: Path to the Markdown file defining the bot's tone and traits.<br>**How to Tune**: Default is `persona.md`. Tune if you are hosting multiple bot instances with distinct personalities. |
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

## 📊 Observability / ops

These tune the Phase 10 observability layer (in-process metrics, the `/health` and `/metrics`
admin commands, and the optional periodic metrics logger). Both keys have safe defaults so the
bot runs unchanged when they are unset. See [observability.md](observability.md) for the full
metric catalog and runbook.

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`ADMIN_USER_IDS`** | String (CSV) | *(blank → DM-only)* | **Purpose**: Comma-separated list of Telegram user ids allowed to use the `/health` and `/metrics` admin commands. Blanks are ignored and each id is coerced to `int`.<br>**How to Tune**: Leave empty to apply the safe default — the commands answer **only in private chats (DMs)** so a status report is never broadcast to a group. Set to specific ids (e.g. `123456789,987654321`) to restrict the commands to those operators in any chat. |
| **`METRICS_LOG_INTERVAL_SECS`** | Float | `0.0` | **Purpose**: Interval, in seconds, for the optional background task that logs the metrics-snapshot summary (no DB/LLM call).<br>**How to Tune**: Keep `0` (or any value ≤ 0) to disable the periodic logger entirely. Set a positive value (e.g. `60`) to emit one summary log line per interval for a lightweight time series in the logs. |

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
