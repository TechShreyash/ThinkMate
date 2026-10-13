# LLM integration

[LLMService](../../app/services/llm_service.py) is the shared interface to an OpenAI-compatible Chat Completions endpoint. One `AsyncOpenAI` client is created per process. The service handles retries, memory-output validation, reply parsing, audit summaries, and metrics. Endpoint compatibility is an operator responsibility; choosing a base URL does not guarantee that its server supports every response format.

See [configuration](configuration.md) for settings, [memory engine](memory_engine.md) for callers, [Telegram bot](telegram_bot.md) for delivery, and [database](database.md) for retained data.

## Operations

| Method | Model profile | Output / failure behavior |
| --- | --- | --- |
| `generate_reply_bundle` | Chat | DM: `(reply, reaction)`; group: `(reply, reaction, affinity_delta)`. API failures after model fallback propagate to the batch processor. |
| `generate_checkin` | Chat | Plain text or `None` for blank/declined output and errors after model fallback. |
| `extract_memory` | Extraction | `MemoryExtraction` or `None`. |
| `extract_group_memory` | Extraction | `GroupMemoryExtraction` or `None`; the audit key is a chat ID. |
| `compress_memory` | Extraction | `MemoryCompression` or `None`. |
| `consolidate_memory` | Extraction | `MemoryConsolidation` or `None`. |

The chat primary is `LLM_CHAT_MODEL`, or legacy `LLM_MODEL` when blank. The extraction
primary is `LLM_EXTRACTION_MODEL`, or the resolved chat model when blank. Each profile
has its own optional fallback model and independent reasoning settings:

| Profile | Fallback model | Primary reasoning | Fallback reasoning |
| --- | --- | --- | --- |
| Chat | `LLM_CHAT_FALLBACK_MODEL` | `LLM_CHAT_REASONING_EFFORT` | `LLM_CHAT_FALLBACK_REASONING_EFFORT` |
| Extraction | `LLM_EXTRACTION_FALLBACK_MODEL` | `LLM_EXTRACTION_REASONING_EFFORT` | `LLM_EXTRACTION_FALLBACK_REASONING_EFFORT` |

Model IDs are sent unchanged, including proxy family aliases such as
`gemini-flash-lite` or `gemini-flash`. The endpoint controls version selection and
rotation within an alias; ThinkMate's model fallback is activated by primary
failure. An explicit chat model takes precedence over an older `LLM_MODEL` version
pin once a revision containing the chat-profile settings has been deployed.

Blank fallback disables it. A fallback with the same resolved model ID as its primary
is skipped, even if its reasoning differs. All models share one `LLM_BASE_URL` and
`LLM_API_KEY`. Reasoning does not inherit between profiles or between primary and
fallback. Blank reasoning omits `reasoning_effort`; explicit `none` sends `"none"`.
Accepted values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`,
normalized for whitespace and case. Supported values depend on the endpoint and model;
see the [OpenAI Chat Completions reference](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).

All calls are non-streaming and pass `LLM_REQUEST_TIMEOUT_SECS` (default `610`). Requests do not set `max_tokens` or temperature. `MAX_RESPONSE_CHARS` is an application delivery cap, applied after generation and before saving reply history; it does not limit model generation cost or context size. Telegram delivery additionally chunks text to fit its message limit. Restart after changing model, reasoning, or structured-mode settings.

Set the client timeout with the actual provider/proxy ceiling in mind. A client timeout can abandon a generation that continues upstream, and a retry may start duplicate work. The repository does not define or prove the external server's timeout.

## Retries and call counts

The SDK client uses `max_retries=0`. `_with_retries` performs the explicit transport retry policy:

- Retry `APITimeoutError`, `APIConnectionError`, `RateLimitError`, and `InternalServerError`.
- Make at most `LLM_MAX_RETRIES + 1` attempts per model and structured mode (default `3`).
- Sleep `LLM_RETRY_BASE_DELAY_SECS * 2**attempt` between retries (default delays `0.5`, then `1.0` seconds).
- Propagate other errors immediately to the model-fallback layer. Invalid JSON/schema validation is not retried inside this helper.

A configured distinct fallback gets its own retry budget after the primary fails.
Transient errors exhaust the primary's retries first; other request errors proceed
to fallback immediately. Memory methods also use model fallback after invalid or
missing structured output. In `native_parse` mode, each model first tries native
parsing and then JSON-object mode before moving to the next model.

The extraction pipeline separately retries a `None` result up to three logical attempts, rereading the buffer each time. With default transport retries and no model fallback, one extraction run can make up to nine requests in `json_object` mode. A configured fallback raises this to eighteen; using `native_parse` as well can raise it to thirty-six. One reply/check-in operation can make up to six requests with a fallback; one compression/consolidation can make up to six in JSON mode or twelve with native parsing plus model fallback. These bounds describe failed attempts that exhaust all applicable retry budgets; ordinary successful operations make fewer calls.

## Conversational reply parsing

Reply generation uses `LLM_CHAT_STRUCTURED_MODE`, independently of the memory
setting `LLM_STRUCTURED_MODE`. The default `json_object` requests
`response_format={"type": "json_object"}`. Opt-in `json_schema` sends an explicit
strict JSON Schema requiring `reply` and `reaction` strings, rejecting extra fields.
The group schema additionally requires `affinity_delta` as a number in `[-0.2, 0.2]`
or `null`. Both modes use the same existing parser and failure policies; no SDK
native parsing or output-repair request is introduced. Endpoint support is required
to enforce the schema.

The prompt asks for:

```json
{"reply": "Hi!", "reaction": "", "affinity_delta": 0.05}
```

`affinity_delta` is requested and returned only when the group caller sets `with_affinity=True`. JSON-object mode permits its omission; schema mode uses `null` for no shift. The DM contract has two values. Persona examples and earlier assistant text describe the text inside `reply`; the prompt explicitly keeps the JSON envelope required.

Replies use a custom JSON parser, not Pydantic `ReplyBundle` validation. The parser strips optional Markdown fences and accepts a dictionary with a nonblank string `reply`:

- Reactions are normalized by [reactions.py](../../app/services/reactions.py), restricted to its allowed set, and dropped when globally disabled. The chat manager additionally honors the sender's `reactions_enabled` preference.
- A finite numeric, non-boolean affinity delta is bounded to the prompt's `[-0.2, 0.2]` range. Nonfinite values are ignored; the affinity cache also clamps the resulting score to `0..1`.
- A JSON object without usable reply text returns an empty reply, with no reaction or delta. The batch processor substitutes a fallback for empty DM/addressed replies and suppresses empty ambient replies. Non-object or invalid JSON falls back to the unfenced raw text with no reaction or delta.
- A request error triggers the configured chat fallback. If all configured models fail, the error is audited and re-raised; the batch processor attempts a short failure response. Successful Telegram delivery still depends on Telegram permissions and connectivity.

`ReplyBundle` in [schemas.py](../../app/services/schemas.py) describes the intended shape, but is not the runtime parser for this path.

Malformed/raw-text replies and empty reply objects keep these existing parsing policies; they do not trigger a second model call. Check-ins use a plain-text response. Blank output or the case-insensitive sentinels `NOTHING`, `none`, `n/a`, and `na` (with trailing `.`/`!`, quotes, or Markdown code fences tolerated) produce `None` without model fallback. Normal opener text is preserved. The caller stays silent for declined output and rechecks opt-out, profile lifecycle, and user activity after generation before sending. The supplied `memory_text` is used as a grounding guard; the caller must also put that memory into the system prompt, as the current scheduler does.

## Structured memory output

The memory methods share `_structured_call` and validate responses against Pydantic models from [schemas.py](../../app/services/schemas.py).

| `LLM_STRUCTURED_MODE` | Behavior |
| --- | --- |
| `json_object` (default) | Append the schema to the system prompt, request a JSON object, strip optional fences, then call `schema.model_validate_json`. |
| `native_parse` | For each model, first call `client.beta.chat.completions.parse` with the schema. On an exception or invalid/absent parsed result, fall back to the `json_object` path for that model. |
| Other string | Uses the `json_object` path; configuration does not validate this setting as an enum. |

The default JSON mode requests syntactically valid JSON; schema adherence still depends on the model and application validation. Native parsing requires endpoint support and can increase latency/call count when unsupported. After the primary model's applicable modes fail, a configured distinct extraction fallback repeats the same mode sequence with its own reasoning setting.

On the JSON path, blank output, including empty fenced output, fails validation.
The method returns `None` if the configured models cannot produce a valid result.
Extraction schemas default their lists to empty, so an explicit `{}` can still
validate as success and trim history. Compression and consolidation additionally
require every replacement array to be explicitly present in the actual response,
before schema defaults are applied; blank or incomplete responses fail validation,
and existing memory is preserved when all configured attempts fail. The same
presence check applies to native parsed responses. Explicit complete empty arrays
remain valid, so this guard does not detect a model intentionally returning an
unhelpful but complete replacement.

### Schema reference

| Model | Fields and constraints |
| --- | --- |
| `MemoryExtraction` | Optional `profile_updates`/`emotional_state`; new, updated, and removed lists for facts, beliefs, and events. Lists default empty. |
| `ProfileUpdate` | Optional communication style and gender (`male`, `female`, `non-binary`). |
| Fact models | Category is one of `personal`, `work`, `preference`, `health`, `hobby`, `relationship`. Addition uses `content`; update uses `old_content` and `new_content`; removal uses `content`. |
| Belief models | Addition/removal use `content`; update uses `old_content` and `new_content`. |
| Event models | Addition: description, nullable date/emotion, significance `major`, `minor`, or `routine`. Update: old/new description, optional date/significance. Removal: description. |
| `EmotionLog` | Mood string, intensity float defaulting to `0.5`, optional trigger. The described `0..1` intensity range is not constrained by validators. |
| `GroupMemoryUpdate` | Participant display-name string plus that person's `MemoryExtraction`. Identity resolution happens in the extractor. |
| `GroupMemoryExtraction` | Optional `group_extraction` and `updates` defaulting empty. Forbids unknown top-level fields, rejecting a plain single-person extraction envelope; it still accepts `{}`. |
| `MemoryCompression` | Optional summary/style plus `compressed_facts`, `compressed_beliefs`, `compressed_events`. Lists default empty. Legacy mood field should be null and is ignored during writes. |
| `MemoryConsolidation` | Optional summary/style plus `consolidated_facts`, `consolidated_beliefs`, `consolidated_events`, and `insights`. Lists default empty. Legacy mood field should be null and is ignored during writes. |
| `CompressedFact`, `CompressedBelief` | Category/content and content respectively. Shared by compression and consolidation. |
| `CompressedEvent` | Description, nullable date, significance restricted to `major` or `minor`; no emotional-context field. |
| `ConsolidatedInsight` | Content string; the storage helper caps the list at `MAX_INSIGHTS`. |

Most models use Pydantic's default handling of extra fields; only the group extraction envelope explicitly forbids them. Date fields are strings, not validated date objects. Text lengths, gender inference accuracy, mood accuracy, and factual truth are not schema guarantees. Array replacement and metadata conversion are documented in [database](database.md#memory-update-semantics).

## Prompt sources

| Source | Purpose |
| --- | --- |
| [system_prompt.py](../../app/prompts/system_prompt.py) | Persona, language/style behavior, memory blocks, group attribution, speaker anchor, and optional time context. |
| [extraction_prompt.py](../../app/prompts/extraction_prompt.py) | Memory CRUD instructions, English normalization, dates, and gender inference. The extractor adds group attribution instructions when needed. |
| [compression_prompt.py](../../app/prompts/compression_prompt.py) | Condense a current profile; the compressor appends the calculated character target. |
| [consolidation_prompt.py](../../app/prompts/consolidation_prompt.py) | Merge a current profile and infer durable insights; the consolidator appends the insight cap. |
| [checkin_prompt.py](../../app/prompts/checkin_prompt.py) | Generate a short grounded opener or decline. |

`PERSONA_FILE` is loaded by [chat_manager.py](../../app/services/chat_manager.py), cached by absolute path/modification time/file size, and reread on detected edits. Missing, blank, or invalid files use the default, or the last good content from that same path when available. Prompt instructions are requests to the model, not enforced behavioral guarantees.

## Audit and metrics

`_fire_log` schedules a best-effort MongoDB insert and tracks the task in the service's background task set. It records one audit row for the final logical result, not one per HTTP retry or native-parse failure. The six call types are `chat_reply`, `proactive_checkin`, `memory_extraction`, `group_memory_extraction`, `memory_compression`, and `memory_consolidation`.

Rows contain lengths of stringified input/output fields, success/failure status, failure traceback, an ID, and a UTC datetime for TTL expiry. Input lengths describe the original inputs; they do not include every appended schema/format instruction and are not token counts. A malformed reply handled as raw text counts as a successful call. A declined check-in also counts as a successful call.

[metrics.py](../../app/services/metrics.py) records success/failure and latency by call type. Timings include retries and fallback work for the logical operation. Audit writes do not delay generation, but may be lost on interruption or database failure. For storage/index details, see [database](database.md); for reports and checkpoints, see [observability](observability.md).

Conversation history, memory blocks, persona, and prompts are sent to `LLM_BASE_URL`. Length-only audit summaries reduce routine text retention in MongoDB, but error tracebacks may contain sensitive provider details, and the configured provider has its own retention behavior. See [reset and data retention](database.md#reset-and-data-retention) before describing reset as complete erasure.

## Verification

[test_llm_model_routing.py](../../tests/test_llm_model_routing.py) checks primary and
fallback routing, independent reasoning effort, retry ordering, native-to-JSON
format fallback, replacement-output validation, and cancellation through the real
SDK with mocked HTTP transport. [test_config_validation.py](../../tests/test_config_validation.py)
checks model resolution and reasoning-setting validation.
The routing suite verifies DM/group schemas on primary/fallback requests and
unchanged check-in and memory formats. It also loads the GeminiProxy chat/extraction environment settings
and verifies family aliases, reasoning efforts, and Gemma fallbacks at the SDK's
HTTP boundary when a legacy fixed-version `LLM_MODEL` remains configured.

[test_reactions.py](../../tests/test_reactions.py) exercises reply parsing, reaction normalization, and delivery behavior. [test_group_extraction.py](../../tests/test_group_extraction.py) checks group output envelopes. [test_consolidation_llm.py](../../tests/test_consolidation_llm.py) checks consolidation schema defaults and dispatch; [test_compression_safety.py](../../tests/test_compression_safety.py) checks `None` preservation in the caller. These mocked tests validate application contracts without proving real endpoint compatibility or model quality. See [testing guide](testing_guide.md) for the separate live harness.
