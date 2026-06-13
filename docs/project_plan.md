# Project Implementation Plan & Checklist

Use this step-by-step guide to implement, verify, and complete the ThinkMate project. It serves as a living roadmap and development tracker.

---

## 🗺️ Phase Roadmap Summary

```
┌───────────────────────────┐      ┌───────────────────────────┐      ┌───────────────────────────┐
│ Phase 1: Base Setup       │ ───> │ Phase 2: Async MongoDB DB │ ───> │ Phase 3: LLM & Pydantic   │
│ Init structure & Pydantic.│      │ Document arrays & schemas │      │ SDK wrappers & fallbacks. │
└───────────────────────────┘      └───────────────────────────┘      └───────────────────────────┘
                                                                                    │
                                                                                    ▼
┌───────────────────────────┐      ┌───────────────────────────┐      ┌───────────────────────────┐
│ Phase 6: Memory Budget    │ <─── │ Phase 5: Telegram Bot     │ <─── │ Phase 4: Memory Engine    │
│ Compressions & Guards.    │      │ Middlewares & handlers.   │      │ Sliding window & loaders. │
└───────────────────────────┘      └───────────────────────────┘      └───────────────────────────┘
              │
              ▼
┌───────────────────────────┐
│ Phase 7: Tests & Audit    │
│ pytest, mock, log audits  │
└───────────────────────────┘
```

---

## 📋 Phase-by-Phase Checklist

### Phase 1: Environment & Project Initialization
Set up the base directories, configuration loaders, typing schemas, and logging subsystems.

-   [x] **1.1 Directory Initialization**: Create the basic project folder structure.
-   [x] **1.2 Dependencies Setup**: Configure the `requirements.txt` file, listing `aiogram`, `python-dotenv`, `motor`, `mongomock`, `openai`, `pydantic`, and `loguru`.
-   [x] **1.3 Environment Variables Configuration**: Author the `.env.example` configuration template with MongoDB connection URI/DB variables, Telegram tokens, and default memory budgets.
-   [x] **1.4 Typed Configuration System**: Write `config.py` to load environment variables, execute type parsing, and validate configurations.
-   [x] **1.5 Configuration Reference Documentation**: Document environment variables and tuning parameters in [configuration.md](development/configuration.md).
-   [x] **1.6 Pydantic Model Schemas**: Implement the structural validation models in `app/services/schemas.py`:
    *   `MemoryExtraction` (combines profile updates, new facts, updated facts, subjective beliefs, events, and emotional states)
    *   `MemoryCompression` (combines compressed facts, beliefs, events, summaries, and communication styles)
-   [x] **1.7 Logging Subsystem**: Initialise `loguru` in `app/__init__.py` to log outputs to standard output and rotating log files.

---

### Phase 2: Async Database Layer (MongoDB)
Implement the connection managers, indexing tasks, and atomic document-per-user CRUD mutations.

-   [x] **2.1 Connection Singleton**: Write `connection.py` using `motor` to handle asynchronous MongoDB database client setup and database sessions.
-   [x] **2.2 Schema & Indexing Implementation**: Define index constraints in `connection.py` for collections:
    *   `user_profiles` (consolidates summaries, communication styles, emotional states, facts, beliefs, and events arrays)
    *   `chat_buffers` (holds active messages arrays for sliding windows)
    *   `llm_audit_log` (logs prompts, replies, and error metrics, indexed on `("user_id", 1), ("timestamp", -1)`)
-   [x] **2.3 CRUD Models**: Write NoSQL accessors in `models.py` accepting `db: AsyncIOMotorDatabase` as their first parameter. Ensure transactional methods like `save_extracted_memories` take Pydantic models directly as inputs and implement hard deletes.

---

### Phase 3: LLM Integration Service & Audit Trails
Wrap the OpenAI client, configure structured outputs, parse responses, and establish database audit logs.

-   [x] **3.1 LLM Service Class**: Implement the core `LLMService` in `llm_service.py` wrapping `openai.AsyncOpenAI`.
-   [x] **3.2 Structured Output Handler & Fallback**: Write `extract_memory` and `compress_memory` calls. Ensure they route through `client.beta.chat.completions.parse` for OpenAI connections and fallback to JSON mode + manual Pydantic validation for custom local engines.
-   [x] **3.3 Centralized Audit Logging**: Wrap LLM operations to insert calling parameters, system prompts, raw outputs, parsed JSON structures, success flags, and traceback strings into `llm_audit_log`.
-   [x] **3.4 Base Prompts Definition**: Write basic prompts under `app/prompts/` (system, extraction, and compression prompts).

---

### Phase 4: Core Memory Engine
Develop the sliding window controllers and context loaders.

-   [x] **4.1 Memory Loader**: Implement `memory_loader.py` to compile facts, subjective beliefs, timeline events, and current moods from the unified user profile document into a clean structured text context block.
-   [x] **4.2 Memory Extractor**: Implement `memory_extractor.py` to select the oldest buffer messages, feed them to the extraction prompt, write updates to the user profile document using hard deletes, and trim the active chat buffer.
-   [x] **4.3 Chat Manager Orchestrator**: Write `chat_manager.py` to coordinate incoming message buffer storage, threshold checks, prompt composition, response query, and non-blocking background extractions.

---

### Phase 5: Telegram Bot Layer (aiogram)
Hook up the Telegram network adapters, register command routers, and database injection middlewares.

-   [x] **5.1 Entrypoint Script**: Initialise the bot, async dispatcher, and database connection loop in `main.py`.
-   [x] **5.2 Session Injection Middleware**: Write `DbSessionMiddleware` in `app/handlers/middlewares.py` to auto-allocate database sessions and inject `db` references into handler contexts.
-   [x] **5.3 Auto-Typing Middleware**: Implement `AutoTypingMiddleware` detecting `long_operation` handler flags to automate typing visuals.
-   [x] **5.4 Command Handlers**: Implement slash commands in `commands.py`:
    *   `/start`: Welcomes users and initializes their profile.
    *   `/profile`: Compiles and displays their current memory card.
-   [x] **5.5 Text Routing Handler**: Write message interception in `messages.py` marked with `flags={"long_operation": True}`.
-   [x] **5.6 Dynamic Message Reactions**: Query LLM concurrently for emoji reactions during message batch processing and send them gracefully to Telegram.

---

### Phase 6: Memory Compression & Input/Output Guards
Implement character-budget memory limits and early-return validation guards.

-   [x] **6.1 Memory Compression Service**: Create `memory_compressor.py` to run LLM-powered memory compression in the background when the 4,000-character budget is exceeded.
-   [x] **6.2 Input & Output Guards**: Implement `MAX_INPUT_CHARS` checks in handler and `MAX_RESPONSE_CHARS` limits in LLM completions.
-   [x] **6.3 Prompt and Persona Hardening**: Author `compression_prompt.py` and enforce conversational limits/anti-abuse boundaries in `persona.md`.
-   [x] **6.4 Throttling, Queue, and Concurrency Guards**: Implement `ThrottlingMiddleware` for early rate limiting, `MAX_QUEUED_MESSAGES` to prevent queue bloat, and a shared sequential `memory_lock` inside `UserState` to serialize background extractor & compressor tasks.

---

### Phase 7: Verification & Testing
Write automated unit tests and run end-to-end user checks.

-   [x] **7.1 Unit Testing Framework**: Set up `pytest` configuration, mock libraries, and test fixtures.
-   [x] **7.2 MongoDB Test Cases**: Create `test_database.py` and `test_guards_and_compression.py` to test MongoDB CRUD transactions, hard-deletion, direct mood writing, and budget triggers using `mongomock` in-memory clients.
-   [x] **7.3 Memory Engine Test Cases**: Write mock LLM test suites in `test_batching_and_concurrency.py` verifying message batching, throttling, queue overflows, and sequential background locks.
-   [x] **7.4 Testing Documentation**: Document the testing suite structure and database mocks in [testing_guide.md](development/testing_guide.md).

---

### Phase 8: Production Hardening & Scaling (2026-06)
Single-instance hardening for 50k+ users. Full detail and rationale in
[hardening_plan.md](development/hardening_plan.md).

-   [x] **8.1 One combined LLM call**: reply + reaction returned as strict JSON in a single `generate_reply_bundle` call (was two calls). Verified live on the Gemini proxy.
-   [x] **8.2 Structured-output strategy**: default to `json_object` (the only mode the Gemini proxy accepts) and drop the always-failing native-parse round-trip; `native_parse` kept as an opt-in for OpenAI. Retries with backoff on transient errors.
-   [x] **8.3 Atomic buffer trim**: `$pull`-on-cutoff with monotonic millisecond timestamps — fixes the silent message-loss race; buffer hard-capped.
-   [x] **8.4 Memory robustness**: normalized/deduped fact-belief-event CRUD; deterministic post-compression budget enforcement + per-user cooldown (fixes the compression re-trigger loop).
-   [x] **8.5 Bounded memory & responsiveness**: idle per-user state eviction, throttle-map pruning, persona cached by mtime, hot-path Mongo round-trips reduced.
-   [x] **8.6 Audit lifecycle**: timestamps stored as `datetime`, off-hot-path writes, field truncation, TTL retention index. Startup Mongo ping.
-   [x] **8.7 Commands & cleanup**: `/help` and `/reset` added; dead `AutoTypingMiddleware` removed; null guards; stale SQLite strings fixed; `logs/` git-ignored.
-   [x] **8.8 Tests**: regression tests for the trim race, cooldown, dedup, reset, budget enforcement, state eviction, and the combined call. Zero deprecation warnings.

---

## 🔮 Future Roadmap (Honcho Inspiration)

-   [ ] **Periodic Consolidation & Dreaming Pass**:
    *   *Concept*: Implement a periodic background task (executed daily or weekly) that performs a "dreaming" consolidation phase.
    *   *Purpose*: Analyze standing facts, timeline events, and subjective beliefs across days/weeks of interactions to extract long-term behavioral trends, conversational habits, and complex user relationship insights.
    *   *Refinement*: Synthesize recurring moods or triggers into a behavioral profile and identify deep-seated beliefs that the user has implicitly expressed.
