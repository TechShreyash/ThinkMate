# Project Implementation Plan & Checklist

Use this step-by-step guide to implement, verify, and complete the ThinkMate project. It serves as a living roadmap and development tracker.

---

## 🗺️ Phase Roadmap Summary

```
┌───────────────────────────┐      ┌───────────────────────────┐      ┌───────────────────────────┐
│ Phase 1: Base Setup       │ ───> │ Phase 2: Async SQLite DB   │ ───> │ Phase 3: LLM & Pydantic   │
│ Init structure & Pydantic.│      │ CRUD, connection singleton│      │ SDK wrappers & fallbacks. │
└───────────────────────────┘      └───────────────────────────┘      └───────────────────────────┘
                                                                                    │
                                                                                    ▼
┌───────────────────────────┐      ┌───────────────────────────┐      ┌───────────────────────────┐
│ Phase 6: Consolidation    │ <─── │ Phase 5: Telegram Bot     │ <─── │ Phase 4: Memory Engine    │
│ Merge facts & cleanups.   │      │ Middlewares & handlers.   │      │ Sliding window & loaders. │
└───────────────────────────┘      └───────────────────────────┘      └───────────────────────────┘
              │
              ▼
┌───────────────────────────┐
│ Phase 7: Tests & Launch   │
│ pytest validation.        │
└───────────────────────────┘
```

---

## 📋 Phase-by-Phase Checklist

### Phase 1: Environment & Project Initialization
Set up the base directories, configuration loaders, typing schemas, and logging subsystems.

-   [x] **1.1 Directory Initialization**: Create the basic project folder structure.
    *   Create directories: `app/handlers`, `app/services`, `app/database`, `app/prompts`, `app/utils`, `data`, `tests`.
-   [x] **1.2 Dependencies Setup**: Create the [requirements.txt](file:///d:/ThinkMate/requirements.txt) file listing `aiogram`, `python-dotenv`, `aiosqlite`, `openai`, `pydantic`, and `loguru`.
-   [x] **1.3 Environment Variables Configuration**: Author the [.env.example](file:///d:/ThinkMate/.env.example) configuration template with Telegram token slots and local/cloud LLM endpoint variables.
-   [x] **1.4 Typed Configuration System**: Write [config.py](file:///d:/ThinkMate/app/config.py) to load environment variables via `python-dotenv`, execute type parsing, and validate configurations.
-   [x] **1.5 Pydantic Model Schemas**: Implement the structural validation models in `app/services/schemas.py`:
    *   `MemoryExtraction` (combines profile updates, new facts, updated facts, events, and emotional states)
    *   `MemoryConsolidation` (combines deactivations and update records)
-   [x] **1.6 Logging Subsystem**: Initialise `loguru` in [app/\_\_init\_\_.py](file:///d:/ThinkMate/app/__init__.py) to log outputs to standard output and rotating log files.

---

### Phase 2: Async Database Layer (SQLite)
Implement the SQL schema, async connection handlers, and transaction mutations.

-   [x] **2.1 Connection Singleton**: Write [connection.py](file:///d:/ThinkMate/app/database/connection.py) to handle database initialization and configure SQLite's WAL (Write-Ahead Logging) mode.
-   [x] **2.2 Schema Implementation**: Define SQL schemas in [connection.py](file:///d:/ThinkMate/app/database/connection.py) or an independent `.sql` file for the following tables:
    *   `user_profiles` (user settings, communication style, summary)
    *   `facts` (extracted user facts with classification categories and soft-delete states)
    *   `events` (chronological timeline items)
    *   `emotional_log` (mood tracking entries)
    *   `chat_buffer` (raw chat message timeline for sliding windows)
-   [x] **2.3 Dependency-Injected CRUD Models**: Write python database accessors in [models.py](file:///d:/ThinkMate/app/database/models.py) accepting `db: Connection` as their first parameter. Ensure transactional methods like `save_extracted_memories` take `MemoryExtraction` Pydantic models directly as inputs.

---

### Phase 3: LLM Integration Service
Wrap the OpenAI client, configure structured outputs, and build fallback parsing engines.

-   [x] **3.1 LLM Service Class**: Implement the core `LLMService` in [llm_service.py](file:///d:/ThinkMate/app/services/llm_service.py) wrapping `openai.AsyncOpenAI`.
-   [x] **3.2 Structured Output Handler**: Write `extract_memory` and `consolidate_memory` calls. Ensure they route through `client.beta.chat.completions.parse` for OpenAI connections and fallback to `response_format={"type": "json_object"}` + manual Pydantic validation (`model_validate_json()`) for local engines.
-   [x] **3.3 Base Prompts Definition**: Write basic prompts under `app/prompts/`:
    *   [system_prompt.py](file:///d:/ThinkMate/app/prompts/system_prompt.py): Combines core instructions, bot persona, and active memories.
    *   [extraction_prompt.py](file:///d:/ThinkMate/app/prompts/extraction_prompt.py): Standardizes rules for extracting JSON key-value updates from text.
    *   [consolidation_prompt.py](file:///d:/ThinkMate/app/prompts/consolidation_prompt.py): Explains how to merge, deduplicate, and clean old memories.

---

### Phase 4: Core Memory Engine
Develop the sliding window controllers and context formatters.

-   [x] **4.1 Memory Loader**: Implement [memory_loader.py](file:///d:/ThinkMate/app/services/memory_loader.py) to load details, active facts, recent events, and current moods from SQLite, formatting them into a structured text context.
-   [x] **4.2 Memory Extractor**: Implement [memory_extractor.py](file:///d:/ThinkMate/app/services/memory_extractor.py) to select the oldest $N$ messages from the chat buffer, feed them to the extraction prompt, write updates to database tables, and trim the buffer.
-   [x] **4.3 Chat Manager Orchestrator**: Write [chat_manager.py](file:///d:/ThinkMate/app/services/chat_manager.py) to coordinate:
    1. Append incoming user messages to buffer.
    2. Check buffer threshold.
    3. Run extraction and trim oldest messages.
    4. Compile prompt and fetch active history.
    5. Query chatbot response.
    6. Save bot response back to the buffer.

---

### Phase 5: Telegram Bot Layer (aiogram)
Hook up the Telegram network adapters, register command routers, and database injection middlewares.

-   [x] **5.1 Entrypoint Script**: Initialise the bot, async dispatcher, and database connection loop in [main.py](file:///d:/ThinkMate/main.py).
-   [x] **5.2 Session Injection Middleware**: Write `DbSessionMiddleware` in `app/handlers/middlewares.py` to auto-allocate database sessions and inject `db` references into handler contexts.
-   [x] **5.3 Auto-Typing Middleware**: Implement `AutoTypingMiddleware` detecting `long_operation` handler flags to automate typing visuals.
-   [ ] **5.4 Command Handlers**: Implement slash commands in [commands.py](file:///d:/ThinkMate/app/handlers/commands.py):
    *   [x] `/start`: Welcomes users and initializes their profile.
    *   [x] `/profile`: Compiles and displays their current memory card.
    *   [ ] `/remember`: Forces manual extraction on the current buffer state.
    *   [ ] `/forget <query>`: Soft-deletes matching facts.
    *   [ ] `/reset`: Offers full profile deletion with safety confirmations.
-   [x] **5.5 Text Routing Handler**: Write message interception in [messages.py](file:///d:/ThinkMate/app/handlers/messages.py) marked with `flags={"long_operation": True}`.

---

### Phase 6: Memory Compression & Input/Output Guards
Implement character-budget memory limits and early-return validation guards.

-   [x] **6.1 Memory Compression Service**: Create [memory_compressor.py](file:///d:/ThinkMate/app/services/memory_compressor.py) to run LLM-powered memory consolidation in background when budget is exceeded.
-   [x] **6.2 Input & Output Guards**: Implement `MAX_INPUT_CHARS` checks in handler and `MAX_RESPONSE_CHARS` limits in LLM completions.
-   [x] **6.3 Prompt and Persona Hardening**: Author `compression_prompt.py` and enforce conversational limits/anti-abuse boundaries in `persona.md`.

---

### Phase 7: Verification & Testing
Write automated unit tests and run end-to-end user checks.

-   [ ] **7.1 Unit Testing Framework**: Set up `pytest` configuration and test fixtures.
-   [ ] **7.2 Database Test Cases**: Create [test_database.py](file:///d:/ThinkMate/tests/test_database.py) to test SQLite transactions, WAL configurations, and per-user isolation.
-   [ ] **7.3 Memory Engine Test Cases**: Write mock LLM test suites in [test_memory_extractor.py](file:///d:/ThinkMate/tests/test_memory_extractor.py) verifying memory formatting.
-   [ ] **7.4 End-to-End Chat Verification**: Spin up the bot locally, verify state across two different Telegram users, and inspect the sqlite DB manually using a local editor (e.g. SQLite Viewer) to confirm correct facts are saved.
