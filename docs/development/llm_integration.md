# LLM integration

[LLMService](../../app/services/llm_service.py) is the shared interface to an OpenAI-compatible Chat Completions endpoint. One `AsyncOpenAI` client is created per process. The service handles retries, memory-output validation, reply parsing, audit summaries, and metrics. Endpoint compatibility is an operator responsibility; choosing a base URL does not guarantee that its server supports every response format.

See [configuration](configuration.md) for settings, [memory engine](memory_engine.md) for callers, [Telegram bot](telegram_bot.md) for delivery, and [database](database.md) for retained data.

## Operations

| Method | Model | Output / failure behavior |
| --- | --- | --- |
| `generate_reply_bundle` | `LLM_MODEL` | DM: `(reply, reaction)`; group: `(reply, reaction, affinity_delta)`. API failures propagate to the batch processor. |
| `generate_checkin` | `LLM_MODEL` | Plain text or `None` for blank/declined output and errors. |
| `extract_memory` | `LLM_EXTRACTION_MODEL` or `LLM_MODEL` | `MemoryExtraction` or `None`. |
| `extract_group_memory` | `LLM_EXTRACTION_MODEL` or `LLM_MODEL` | `GroupMemoryExtraction` or `None`; the audit key is a chat ID. |
| `compress_memory` | `LLM_EXTRACTION_MODEL` or `LLM_MODEL` | `MemoryCompression` or `None`. |
| `consolidate_memory` | `LLM_EXTRACTION_MODEL` or `LLM_MODEL` | `MemoryConsolidation` or `None`. |

All calls are non-streaming and pass `LLM_REQUEST_TIMEOUT_SECS` (default `610`). Requests do not set `max_tokens`, temperature, or a provider-specific reasoning budget. `MAX_RESPONSE_CHARS` is an application delivery cap, applied after generation; it does not limit model generation cost or context size. Telegram delivery additionally chunks text to fit its message limit.

Set the client timeout with the actual provider/proxy ceiling in mind. A client timeout can abandon a generation that continues upstream, and a retry may start duplicate work. The repository does not define or prove the external server's timeout.

## Retries and call counts

The SDK client uses `max_retries=0`. `_with_retries` performs the explicit transport retry policy:

- Retry `APITimeoutError`, `APIConnectionError`, `RateLimitError`, and `InternalServerError`.
- Make at most `LLM_MAX_RETRIES + 1` attempts (default `3`).
- Sleep `LLM_RETRY_BASE_DELAY_SECS * 2**attempt` between retries (default delays `0.5`, then `1.0` seconds).
- Propagate other errors immediately. Invalid JSON/schema validation is not retried inside this helper.

The extraction pipeline separately retries a `None` result up to three logical attempts, rereading the buffer each time. With default transport retries, one extraction run can make up to nine requests in `json_object` mode. `native_parse` fallback can add another set of requests per logical attempt. A reply, compression, consolidation, or check-in normally starts one logical operation, but can also issue several transport requests.

## Conversational reply parsing

Reply generation always requests `response_format={"type": "json_object"}`, independently of `LLM_STRUCTURED_MODE`. The prompt asks for:

```json
{"reply": "Hi!", "reaction": "", "affinity_delta": 0.05}
```

`affinity_delta` is requested and returned only when the group caller sets `with_affinity=True`. The DM contract has two values.

Replies use a custom JSON parser, not Pydantic `ReplyBundle` validation. The parser strips optional Markdown fences and accepts a dictionary with a nonblank string `reply`:

- Reactions are normalized by [reactions.py](../../app/services/reactions.py), restricted to its allowed set, and dropped when globally disabled. The chat manager additionally honors the sender's `reactions_enabled` preference.
- A numeric, non-boolean affinity delta is accepted. The prompt asks for `[-0.2, 0.2]`, but the parser does not enforce that range; the affinity cache clamps the resulting score to `0..1`.
- Malformed/unusable reply JSON falls back to the unfenced raw text with no reaction or delta. This can expose a malformed JSON envelope as the delivered text. The batch processor substitutes a fallback for empty DM/addressed replies and suppresses empty ambient replies.
- An API error is audited and re-raised; the batch processor attempts a short failure response. Successful Telegram delivery still depends on Telegram permissions and connectivity.

`ReplyBundle` in [schemas.py](../../app/services/schemas.py) describes the intended shape, but is not the runtime parser for this path.

Check-ins use a plain-text response. Blank output or the case-insensitive sentinels `NOTHING`, `none`, `n/a`, and `na` (with trailing `.`/`!` tolerated) produce `None`. The caller stays silent and does not send a check-in. The supplied `memory_text` is used as a grounding guard; the caller must also put that memory into the system prompt, as the current scheduler does.

## Structured memory output

The memory methods share `_structured_call` and validate responses against Pydantic models from [schemas.py](../../app/services/schemas.py).

| `LLM_STRUCTURED_MODE` | Behavior |
| --- | --- |
| `json_object` (default) | Append the schema to the system prompt, request a JSON object, strip optional fences, then call `schema.model_validate_json`. |
| `native_parse` | First call `client.beta.chat.completions.parse` with the schema. On an exception or absent parsed result, fall back to the `json_object` path. |
| Other string | Uses the `json_object` path; configuration does not validate this setting as an enum. |

The default JSON mode requests syntactically valid JSON; schema adherence still depends on the model and application validation. Native parsing requires endpoint support and can increase latency/call count when unsupported.

On the JSON path, `_strip_fences` turns blank output into `{}`. The memory schemas default their lists to empty, so `{}` can validate as a successful empty result. This is different from `None`, which signals an API/parsing/validation failure. Extraction treats an empty result as success and trims history. Compression/consolidation apply the empty lists and can clear stored memory arrays. The failure-preservation contract applies specifically to `None`, not every unhelpful or empty model output.

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
| `MemoryCompression` | Optional summary/style/mood plus `compressed_facts`, `compressed_beliefs`, `compressed_events`. Lists default empty. |
| `MemoryConsolidation` | Optional summary/style/mood plus `consolidated_facts`, `consolidated_beliefs`, `consolidated_events`, and `insights`. Lists default empty. |
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

`PERSONA_FILE` is loaded by [chat_manager.py](../../app/services/chat_manager.py), cached by path/mtime, and reread on edits. A missing file uses a default persona; a read failure can preserve the cached content. Prompt instructions are requests to the model, not enforced behavioral guarantees.

## Audit and metrics

`_fire_log` schedules a best-effort MongoDB insert and tracks the task in the service's background task set. It records one audit row for the final logical result, not one per HTTP retry or native-parse failure. The six call types are `chat_reply`, `proactive_checkin`, `memory_extraction`, `group_memory_extraction`, `memory_compression`, and `memory_consolidation`.

Rows contain lengths of stringified input/output fields, success/failure status, failure traceback, an ID, and a UTC datetime for TTL expiry. Input lengths describe the original inputs; they do not include every appended schema/format instruction and are not token counts. A malformed reply handled as raw text counts as a successful call. A declined check-in also counts as a successful call.

[metrics.py](../../app/services/metrics.py) records success/failure and latency by call type. Timings include retries and fallback work for the logical operation. Audit writes do not delay generation, but may be lost on interruption or database failure. For storage/index details, see [database](database.md); for reports and checkpoints, see [observability](observability.md).

Conversation history, memory blocks, persona, and prompts are sent to `LLM_BASE_URL`. Length-only audit summaries reduce routine text retention in MongoDB, but error tracebacks may contain sensitive provider details, and the configured provider has its own retention behavior. See [reset and data retention](database.md#reset-and-data-retention) before describing reset as complete erasure.

## Verification

[test_reactions.py](../../tests/test_reactions.py) exercises reply parsing, reaction normalization, and delivery behavior. [test_group_extraction.py](../../tests/test_group_extraction.py) checks group output envelopes. [test_consolidation_llm.py](../../tests/test_consolidation_llm.py) checks consolidation schema defaults and dispatch; [test_compression_safety.py](../../tests/test_compression_safety.py) checks `None` preservation in the caller. These mocked tests validate application contracts without proving real endpoint compatibility or model quality. See [testing guide](testing_guide.md) for the separate live harness.
