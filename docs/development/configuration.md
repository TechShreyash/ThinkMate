# Configuration reference

Runtime settings are defined in [`app/config.py`](../../app/config.py). Start with
[setup](../setup_guide.md), then use this page to explain or change a setting.

## Loading and validation

The module loads the repository-root `.env` when present, otherwise calls
`python-dotenv` discovery. Existing process environment variables take precedence over
`.env` values. A single `config` object is created at import; restart the bot after
changing environment settings. Command handlers and menus also resolve at startup.

Tables below show **code defaults when a variable is unset**, not values promised by
[`.env.example`](../../.env.example). Copying the example changes these values:

| Variable | Code default | Example value |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Empty string | Placeholder that must be replaced |
| `LLM_EXTRACTION_MODEL` | Empty; reuses the resolved chat model | `gpt-4o-mini` |
| `BOT_NAME` | Empty | `ThinkMate` |
| `RATE_LIMIT_MAX_REQUESTS` | `10` | `5` |
| `RATE_LIMIT_WINDOW_SECS` | `5.0` | `10` |

Boolean true values are `1`, `true`, `yes`, and `on`, case-insensitive; any other
provided string becomes false. Integer/float parse errors can stop import. Invalid
`ADMIN_USER_IDS` entries are ignored; invalid or blank `LOGS_CHANNEL_ID` becomes
`None`. Numeric settings reject NaN/infinity. Resource caps and timeouts must be
positive; retry counts, retained-buffer counts, and cooldowns allow zero. Probability
and similarity settings must be in `[0, 1]`, and quiet-hour settings in `0`–`23`.
Startup also checks that `CHAT_BUFFER_TRIM < CHAT_BUFFER_HARD_CAP` and that the burst
tracking cap can hold `GROUP_SPAM_BURST_COUNT` messages. Documented `<= 0` feature
switches remain valid. Model IDs are stripped of surrounding whitespace; either
`LLM_CHAT_MODEL` or `LLM_MODEL` must provide a nonblank chat model. Reasoning
settings are stripped and lowercased; unsupported values stop startup. URL/model
strings and accepted reasoning levels are not checked for endpoint support at startup.

## Telegram, identity, and persona

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | String | Empty | Required by aiogram to construct the bot; use a real BotFather token. |
| `TELEGRAM_PUBLISH_COMMANDS` | Boolean | `true` | Publish command menus to Telegram at startup; publication errors are best-effort. |
| `PERSONA_FILE` | Path string | `persona.md` | Relative to the process working directory. Cached content reloads when its modification time changes; missing files use a fallback persona. |
| `BOT_NAME` | String | Empty | Display name falls back to `ThinkMate`. Group name addressing uses this value when set, otherwise Telegram's first name. Username addressing is resolved separately. |
| `ENABLE_MESSAGE_REACTIONS` | Boolean | `true` | Global reaction permission; users can separately opt out via `/reactions off`. Disabling this does not remove the reply LLM call. |
| `GROUP_INTRO_ON_JOIN` | Boolean | `true` | Send the introduction when the bot joins a group. |

`BOT_NAME` changes UI labels and group addressing; it does not rewrite a name embedded
in the persona file. See [Telegram behavior](telegram_bot.md).

## LLM connection and retries

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `LLM_BASE_URL` | String | `http://localhost:1234/v1` | OpenAI-compatible API base URL. |
| `LLM_API_KEY` | String | `none` | API key supplied to the client; local endpoints may accept a placeholder. |
| `LLM_MODEL` | String | `gpt-4o` | Legacy chat-model setting, used when `LLM_CHAT_MODEL` is empty. |
| `LLM_CHAT_MODEL` | String | Empty | Replies and proactive check-ins model; empty uses `LLM_MODEL`. Must exist at the configured endpoint. |
| `LLM_EXTRACTION_MODEL` | String | Empty | Extraction, group extraction, compression, and consolidation model; empty reuses the resolved chat model. |
| `LLM_CHAT_FALLBACK_MODEL` | String | Empty | Optional alternate model for replies/check-ins after primary request failure. Empty disables it; identical to the resolved primary is skipped. |
| `LLM_EXTRACTION_FALLBACK_MODEL` | String | Empty | Optional alternate model for memory work after primary request or output-validation failure. Empty disables it; identical to the resolved primary is skipped. |
| `LLM_CHAT_REASONING_EFFORT` | String | Empty | Reasoning effort for the primary chat model. |
| `LLM_EXTRACTION_REASONING_EFFORT` | String | Empty | Reasoning effort for the primary extraction model, even when its model ID inherits from chat. |
| `LLM_CHAT_FALLBACK_REASONING_EFFORT` | String | Empty | Reasoning effort for the chat fallback model. |
| `LLM_EXTRACTION_FALLBACK_REASONING_EFFORT` | String | Empty | Reasoning effort for the extraction fallback model. |
| `LLM_STRUCTURED_MODE` | String | `json_object` | Memory calls use JSON-object mode. `native_parse` attempts SDK schema parsing first, then JSON-object fallback. Reply bundles always use JSON-object mode. |
| `LLM_MAX_RETRIES` | Integer | `2` | Retries after the first request for transient connection, timeout, 429, and server errors, per model and structured mode; SDK retries are disabled. Use a non-negative value. |
| `LLM_RETRY_BASE_DELAY_SECS` | Float | `0.5` | Exponential retry delay: base multiplied by `2 ** attempt`. |
| `LLM_REQUEST_TIMEOUT_SECS` | Float | `610.0` | Timeout per request, not an entire reply deadline. Choose for your server; the code default does not establish a universal provider limit. |

Each reasoning setting accepts blank, `none`, `minimal`, `low`, `medium`, `high`,
`xhigh`, or `max`. Blank omits `reasoning_effort` and uses the provider default;
`none` is sent literally. Each primary/fallback reasoning setting is independent,
with no inheritance. The endpoint and model must support the selected value; accepted
configuration does not guarantee provider support. See the
[OpenAI Chat Completions reference](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
and your endpoint's own documentation.

All primary and fallback models share `LLM_BASE_URL` and `LLM_API_KEY`. Transient
errors exhaust a model's retry budget before fallback; other request errors move to
fallback immediately. Memory validation failure can also trigger the extraction
fallback, after the configured structured modes have failed for the primary model.

No `max_tokens` limit is sent. `MAX_RESPONSE_CHARS` truncates delivery after generation
and does not cap inference cost. Retries, structured-mode fallback, and model fallback
can multiply requests and latency. See [LLM integration](llm_integration.md) for call
counts and output policies. Restart the bot after changing these settings.

## MongoDB and audit retention

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `MONGODB_URI` | String | `mongodb://localhost:27017` | Connection URI; driver options such as `maxPoolSize` can be included in the URI. |
| `MONGODB_DB` | String | `thinkmate_db` | Database for runtime profiles, buffers, settings, audits, and metrics. |
| `AUDIT_LOG_RETENTION_DAYS` | Integer | `30` | TTL expiry in days for LLM audit rows. Index creation is best-effort; verify the index exists. |

[`connection.py`](../../app/database/connection.py) sets `serverSelectionTimeoutMS=10000`.
There is no separate connection-pool environment variable. Audit retention does not
apply to profiles, local log files, or Telegram log-channel content. See
[database](database.md) and [observability](observability.md).

## Memory

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `CHAT_BUFFER_MAX_CHARS` | Integer | `4000` | Buffer character threshold checked after a user append to schedule extraction. |
| `NEW_USER_EXTRACTION_CHARS` | Integer | `1000` | Lower threshold for sparse profiles, capped by `CHAT_BUFFER_MAX_CHARS`. |
| `NEW_USER_MEMORY_THRESHOLD` | Integer | `10` | Facts + beliefs + events below this count use the lower threshold; `0` disables it. Check uses the conversation/profile ID, including group IDs. |
| `CHAT_BUFFER_TRIM` | Integer | `8` | Newest buffer messages kept outside extraction. |
| `CHAT_BUFFER_HARD_CAP` | Integer | `200` | Maximum retained messages via MongoDB `$slice`; not a character or token limit. |
| `USER_MEMORY_BUDGET_CHARS` | Integer | `10000` | Compiled profile length triggering compression; not a bound on the full prompt or database document. |
| `COMPRESSION_COOLDOWN_SECS` | Float | `300.0` | Minimum compression gap while a conversation's state remains in memory. |
| `CHARS_PER_TOKEN` | Integer | `4` | Retained field; current application code does not use it. |
| `MAX_MOOD_HISTORY` | Integer | `10` | Stored mood-history entry cap. |

Use positive thresholds/caps and keep `CHAT_BUFFER_HARD_CAP` greater than
`CHAT_BUFFER_TRIM`. A budget must fit fixed memory sections. Deterministic trimming
drops events, beliefs, then facts; retained metadata can still exceed an unrealistically
small budget. Extraction exhaustion deliberately discards older unextracted messages.
See [memory engine](memory_engine.md).

## Batching and input/output guards

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `MESSAGE_BATCH_DELAY_SECS` | Float | `2.0` | Idle delay after the latest queued message before processing. |
| `MAX_BATCH_DELAY_SECS` | Float | `6.0` | Coalescing timer's maximum age from the first pending message. Each timer uses the smaller of the idle delay and remaining maximum age. It does not bound lock wait, generation, or delivery. |
| `USER_STATE_TTL_SECS` | Float | `1800.0` | Idle state eviction threshold; busy/locked/queued state is retained. Sweeps run at `max(60, TTL / 2)` seconds. |
| `RATE_LIMIT_MAX_REQUESTS` | Integer | `10` | Maximum per-user messages accepted in the sliding window, across chats. |
| `RATE_LIMIT_WINDOW_SECS` | Float | `5.0` | Rate-limit window using Telegram send timestamps; idle entries are pruned on later access. |
| `STALE_MESSAGE_SECS` | Float | `60.0` | Drop messages sent before middleware startup minus this allowance; `<= 0` disables it. Startup cutoff, not a rolling message-age limit. |
| `MAX_QUEUED_MESSAGES` | Integer | `10` | Pending queue cap per conversation; excess messages are dropped. |
| `MAX_INPUT_CHARS` | Integer | `4000` | Reject longer inbound text. DMs receive a deflection; groups stay silent. |
| `MAX_RESPONSE_CHARS` | Integer | `4000` | Delivery cap for generated replies/check-ins; `<= 0` disables it. Telegram-safe chunking still applies. |

A group batch is split by sender and can generate multiple replies. There is no global
queue/concurrency cap. See [performance and scaling](performance_and_scaling.md).

## Group reply gates

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `GROUP_AMBIENT_COOLDOWN_SECS` | Float | `30.0` | Window between ambient dispatches per group, including declined/empty replies. |
| `GROUP_AMBIENT_BASE_RATE` | Float | `0.25` | Ambient probability before affinity and mode weighting. |
| `GROUP_CONTEXT_SCAN_EVERY` | Integer | `12` | Periodic message-count trigger in the same ambient gate; not an independent LLM context-scanner call. |
| `AFFINITY_DEFAULT` | Float | `0.5` | Initial per-group member affinity. |
| `GROUP_IMPLICIT_RECENCY_SECS` | Float | `120.0` | Time window after a bot response for possible implicit addressing. |
| `GROUP_IMPLICIT_RECENCY_MAX_MSGS` | Integer | `5` | Maximum intervening human messages for implicit addressing. |
| `GROUP_IMPLICIT_COOLDOWN_SECS` | Float | `15.0` | Minimum gap between implicit replies per group. |
| `GROUP_MASS_TAG_SPAM_THRESHOLD` | Integer | `5` | Distinct mentions strictly greater than this count mark mass-tag spam. |
| `GROUP_SPAM_BURST_SIMILARITY` | Float | `0.85` | Similarity threshold for mention-stripped repeated-message detection. |
| `GROUP_SPAM_BURST_COUNT` | Integer | `3` | Repeat count marking a burst within the window. |
| `GROUP_SPAM_BURST_WINDOW_SECS` | Float | `60.0` | Repeat-detection window. |
| `GROUP_SPAM_BURST_TRACK_MAX` | Integer | `20` | Recent-message tracking cap per sender within each group. |

Use probability/similarity values in `[0, 1]` and positive windows/counts. Detailed
routing, commands, and spam interactions are in [group chat](group_chat.md).

## Observability and log forwarding

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `ADMIN_USER_IDS` | CSV of integers | Empty set | Empty permits ops commands to any private-chat user. Non-empty restricts them to listed IDs, including requests in groups. Invalid entries are ignored. |
| `METRICS_LOG_INTERVAL_SECS` | Float | `0.0` | Summary log cadence; `<= 0` disables it. |
| `METRICS_PERSIST_INTERVAL_SECS` | Float | `300.0` | Metrics checkpoint cadence; `<= 0` disables periodic writes only. Startup restore and shutdown flush still occur. |
| `LOGS_CHANNEL_ID` | Integer or blank | `None` | Telegram destination for operational events, WARNING+ records, extraction summaries, and reset backups. Empty/invalid disables forwarding. |
| `FORWARD_DIAGNOSTICS` | Boolean | `false` | Also forward verbose routing/drop traces when a logs destination is configured. |

Local logging always uses console INFO and `logs/bot.log` DEBUG, 10 MB rotation,
10-day retention. These are code settings, not environment fields. Extraction
summaries and reset backups contain user data. See [observability](observability.md).

## Consolidation and proactive check-ins

Both schedulers are **enabled by code default and by the example**, and perform
background LLM work. Disable with `CONSOLIDATION_INTERVAL_SECS=0` and
`PROACTIVE_INTERVAL_SECS=0`.

| Variable | Type | Code default | Behavior |
| --- | --- | --- | --- |
| `CONSOLIDATION_INTERVAL_SECS` | Float | `86400.0` | Minimum per-profile consolidation age; `<= 0` disables scheduler. |
| `CONSOLIDATION_SCAN_INTERVAL_SECS` | Float | `3600.0` | Time between scans; keep positive when enabled. |
| `CONSOLIDATION_MAX_USERS_PER_SCAN` | Integer | `50` | Maximum eligible profiles processed per scan. |
| `CONSOLIDATION_MIN_ITEMS` | Integer | `8` | Minimum facts + beliefs + events for eligibility. |
| `MAX_INSIGHTS` | Integer | `5` | Maximum stored consolidation insights. |
| `PROACTIVE_INTERVAL_SECS` | Float | `3600.0` | Proactive scan interval; `<= 0` disables scheduler. |
| `PROACTIVE_INACTIVITY_SECS` | Float | `86400.0` | Required age of a user's last recorded interaction. |
| `PROACTIVE_MIN_INTERVAL_SECS` | Float | `172800.0` | Minimum attempt gap per user; skip/failure also records attempt time. |
| `PROACTIVE_MAX_PER_SCAN` | Integer | `50` | Maximum qualifying users attempted per scan, not guaranteed deliveries. |
| `PROACTIVE_MIN_ITEMS` | Integer | `3` | Minimum facts + beliefs + events for grounding eligibility. |
| `PROACTIVE_MAX_UNANSWERED` | Integer | `3` | Pause after this many delivered unanswered check-ins; interaction resets streak. `0` disables auto-pause. |
| `PROACTIVE_QUIET_START_HOUR` | Integer | `16` | Quiet-window start UTC hour, inclusive. |
| `PROACTIVE_QUIET_END_HOUR` | Integer | `2` | Quiet-window end UTC hour, exclusive; equal start/end disables it. |

Quiet hours use one UTC window for all users, including midnight wrapping. Default
`16` to `2` corresponds to 21:30–07:30 in Asia/Calcutta. Hours should be `0`–`23`;
per-user timezones are not stored. Users are eligible unless they opt out via
`/checkins off`; generation can still decline. See [memory engine](memory_engine.md).

## Command environment variables

Each built-in key has `CMD_<KEY>_NAME` (default: key in lowercase) and
`CMD_<KEY>_ENABLED` (default: `true`). The `COMMANDS` field is derived from these;
an environment variable called `COMMANDS` has no effect.

| Command key | Name variable | Enable variable |
| --- | --- | --- |
| `start` | `CMD_START_NAME` | `CMD_START_ENABLED` |
| `help` | `CMD_HELP_NAME` | `CMD_HELP_ENABLED` |
| `onboard` | `CMD_ONBOARD_NAME` | `CMD_ONBOARD_ENABLED` |
| `checkins` | `CMD_CHECKINS_NAME` | `CMD_CHECKINS_ENABLED` |
| `profile` | `CMD_PROFILE_NAME` | `CMD_PROFILE_ENABLED` |
| `reset` | `CMD_RESET_NAME` | `CMD_RESET_ENABLED` |
| `reactions` | `CMD_REACTIONS_NAME` | `CMD_REACTIONS_ENABLED` |
| `quiet` | `CMD_QUIET_NAME` | `CMD_QUIET_ENABLED` |
| `chatty` | `CMD_CHATTY_NAME` | `CMD_CHATTY_ENABLED` |
| `groupbot` | `CMD_GROUPBOT_NAME` | `CMD_GROUPBOT_ENABLED` |
| `groupmode` | `CMD_GROUPMODE_NAME` | `CMD_GROUPMODE_ENABLED` |
| `health` | `CMD_HEALTH_NAME` | `CMD_HEALTH_ENABLED` |
| `metrics` | `CMD_METRICS_NAME` | `CMD_METRICS_ENABLED` |

Names lose leading `/` and surrounding whitespace and normalize to lowercase. The
resolver accepts 1–32 ASCII lowercase letters, digits, and underscores; invalid names
fall back to the built-in key. Identical enabled triggers reset colliding commands to
defaults, repeating if a fallback collides with another override. Disabled commands
disappear from handlers/help/menu;
catch-all command messages are ignored. Rename preserves authorization rules.

```dotenv
CMD_PROFILE_NAME=memories
CMD_METRICS_NAME=stats
CMD_RESET_ENABLED=false
```

See [`commands.py`](../../app/handlers/commands.py) for menu scopes and handler wiring.

[Documentation index](../README.md)
