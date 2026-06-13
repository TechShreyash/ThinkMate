# Hardening & Scaling Plan (Production)

> Living document. This is the source of truth for the production-hardening effort.
> Last updated: 2026-06-13. Boxes are checked as work lands. Read this before touching
> the LLM, memory, or concurrency code.

## Context & Goals

ThinkMate is a **live production** Telegram companion bot:

- **~50k active users, continuously growing.** Priorities (in order): responsiveness,
  robustness of every API call, minimizing LLM calls, reply quality, memory quality.
- **Runtime today:** single-instance long-polling, in-memory per-user state, MongoDB Atlas
  (`production-cluster`), an OpenAI-compatible **Gemini/Gemma proxy**
  (`gemini-3.1-flash-lite` for chat, `gemma-4-31b-it` for extraction).
- **Deployment is out of scope** for this effort — we test locally (mongomock / local
  Mongo, never the production cluster).

### Decisions locked in (2026-06-13)

| Topic | Decision |
|---|---|
| Framework | **Keep aiogram 3.x.** It is not the bottleneck; switching is pure risk. |
| Reactions | **Merge into one call** returning strict JSON `{reply, reaction}` (json_object mode). |
| Scaling | **Harden single-instance.** No Redis. Fix leaks/races, bound memory, cut calls. |
| Memory | **Improve the structured approach** (facts/beliefs/events). No vector DB. |
| Reply model | **Optimize within flash-lite** (prompt, memory, params). Keep model configurable. |

## LLM proxy capability findings (verified live, 2026-06-13)

| Mode | Result | Consequence |
|---|---|---|
| Plain chat completion | ✅ works (~1.4–2.4s) | Reaction leaks into text if unstructured. |
| `response_format={"type":"json_object"}` | ✅ **works, high quality** | **Primary structured-output mechanism for all calls.** |
| `client.beta.chat.completions.parse` (native) | ❌ 400 `additionalProperties` | Never use against this proxy — wastes a round-trip. |
| `response_format` `json_schema` strict | ❌ 400 `additionalProperties` | Gemini rejects `additionalProperties`. Avoid. |

**Implication:** the current `extract_memory`/`compress_memory` "native parse first, fall
back to JSON" pattern always fails the first attempt → JSON mode must be the default path,
gated by config so a future OpenAI deployment can re-enable native parse.

## Hot-path cost model (per user message batch)

Before → after this effort:

- LLM calls: **2 → 1** (reply + reaction merged) on the chat path; extraction/compression
  drop a wasted failed `.parse()` round-trip each.
- Mongo round-trips on chat path: ~5 → ~3 (combine buffer write+read; cache persona).
- Audit log writes moved **off** the chat hot path (fire-and-forget) + TTL retention.

---

## Phased checklist

### Phase A — LLM service: robustness + fewer calls
- [x] A1. Add `LLM_STRUCTURED_MODE` config (`json_object` default | `native_parse`). Skip the dead native-parse path for Gemini.
- [x] A2. Single shared `LLMService` instance reused everywhere (fix per-call instantiation in compressor).
- [x] A3. Bounded retry w/ backoff for transient errors (timeout, connection, 429, 5xx); short caps on the chat path.
- [x] A4. DRY the JSON-clean + Pydantic-validate logic into one helper.
- [x] A5. New `generate_reply_bundle()` → `(reply, reaction)` in one json_object call. Graceful fallback to plain reply if JSON parse fails.
- [x] A6. Audit logging: store `timestamp` as `datetime` (TTL-able), truncate large bodies, fire-and-forget on chat path.

### Phase B — Memory engine (structured, robust)
- [x] B1. **Atomic buffer trim** via `$pull` on a cutoff timestamp (fixes silent message loss race).
- [x] B2. Cap `chat_buffers.messages` growth with `$push`/`$slice` safety net.
- [x] B3. Normalize text (casefold/strip) for fact/belief/event CRUD matching → fewer stale duplicates.
- [x] B4. Dedup on write; enforce compression budget deterministically + per-user cooldown (fixes compression re-trigger loop).
- [x] B5. Tune extraction & compression prompts for quality; handle empty/garbage output gracefully.

### Phase C — Database layer
- [x] C1. `datetime.utcnow()` → `datetime.now(timezone.utc)` everywhere.
- [x] C2. Startup Mongo `ping` (fail fast); TTL index on `llm_audit_log`.
- [x] C3. Combine buffer write+read via `find_one_and_update` to cut round-trips.

### Phase D — Telegram layer
- [x] D1. Implement `/help` and `/reset` (reset wipes that user's profile + buffer).
- [x] D2. Remove dead `AutoTypingMiddleware` (manual typing loop is the real path).
- [x] D3. Null guards (`from_user`, null LLM content). Bound throttling dict (evict stale users).
- [x] D4. Use `generate_reply_bundle` in the batch processor (one call).

### Phase E — Concurrency / state
- [x] E1. Bound/evict idle `UserState` entries (fix unbounded `_states` growth at 50k).
- [x] E2. Review & tighten batch-cancellation races.

### Phase F — Standards, cleanup, docs
- [x] F1. Type hints, docstrings, module headers; remove dead code (stale `run_llm_live.py`, dead `/profile` empty-check).
- [x] F2. Fix stale "SQLite" strings/log lines (main.py, README). `.gitignore logs/`, untrack `bot.log`.
- [x] F3. Update all docs to match reality (architecture, llm_integration, database, configuration, README, project_plan).

### Phase G — Tests (local only)
- [x] G1. Tests for combined reply bundle, atomic trim race, compression cooldown, `/reset`, normalized dedup.
- [x] G2. Fix/replace stale `run_llm_live.py`. Keep mongomock; never hit production Atlas.
- [x] G3. Full suite green; zero deprecation warnings.
