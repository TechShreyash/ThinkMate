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
| **`LLM_EXTRACTION_MODEL`** | String | `gpt-4o-mini` | **Purpose**: Identifies the model for memory extraction and compression tasks.<br>**How to Tune**: Leave blank to reuse `LLM_MODEL`. Recommending a smaller, cheaper, and faster model (like `gpt-4o-mini`) keeps token execution costs minimal. |
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
| **`MAX_INPUT_CHARS`** | Integer | `1000` | **Purpose**: Caps incoming message lengths. Longer messages are ignored immediately.<br>**How to Tune**: Set to `1000` (approx. 250 words) to prevent injection exploits or massive copy-pasted logs. |
| **`MAX_RESPONSE_CHARS`** | Integer | `1000` | **Purpose**: Caps the maximum character length generated by the conversational LLM.<br>**How to Tune**: Decrease (e.g. `500`) to force very brief replies, or increase (e.g. `2000`) if you want long, elaborate answers. |

---

## 👤 Persona Settings

| Parameter | Type | Default | Description & How to Adjust |
| :--- | :--- | :--- | :--- |
| **`PERSONA_FILE`** | Path | `persona.md` | **Purpose**: Path to the Markdown file defining the bot's tone and traits.<br>**How to Tune**: Default is `persona.md`. Tune if you are hosting multiple bot instances with distinct personalities. |
