# System Architecture & Design

This document describes the high-level architecture, processing pipelines, and data flow of the ThinkMate self-learning Telegram bot.

---

## 🧠 System Context & Core Mechanics

Unlike traditional vector-search RAG (Retrieval-Augmented Generation) systems that fetch arbitrary text chunks based on semantic similarity, ThinkMate builds a **structured memory profile** of the user over time. The core philosophy is to keep the LLM's context relevant, concise, and reflective of a real human friendship.

The LLM receives exactly **three components** to generate its responses:

```
┌──────────────────────────────────────────────────────────┐
│                   LLM SYSTEM PROMPT                      │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │ 1. Persona (loaded from persona.md)              │    │
│  │    Tone, style, humor, guidelines.              │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │ 2. Memory Profile (compiled from MongoDB)        │    │
│  │    ├─ User Details (name, occupation, style)     │    │
│  │    ├─ Core Facts (objective preferences)         │    │
│  │    ├─ Subjective Beliefs (values & thoughts)     │    │
│  │    ├─ Events (chronological life milestones)     │    │
│  │    └─ Current Mood (direct emotional state)      │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────────────────────────────────────────┐    │
│  │ 3. Active Chat History (from chat_buffers)        │    │
│  │    [User]: I started learning piano today!       │    │
│  │    [Bot]: That's amazing! What song first?       │    │
│  │    ... (up to last N messages)                   │    │
│  └──────────────────────────────────────────────────┘    │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 🔄 The Sliding Window Memory Engine

The bot maintains a sliding window buffer of the latest messages in MongoDB's `chat_buffers` collection. Once the buffer's character count exceeds `CHAT_BUFFER_MAX_CHARS`, a background memory extraction process is triggered. Incoming messages are batched using `UserTaskManager` to handle rapid-fire messages and avoid redundant LLM calls.

```mermaid
graph TD
    A[Incoming Message] --> B[Enqueue to UserTaskManager]
    B --> C[Wait for MESSAGE_BATCH_DELAY_SECS Inactivity]
    C --> D[Acquire Chat Lock & Combine Batch]
    D --> E[Insert Message Batch to chat_buffers Collection]
    E --> F{Buffer Chars >= CHAT_BUFFER_MAX_CHARS?}
    F -->|No| G[Load persona.md & Compile Memory Profile]
    F -->|Yes| H[Trigger Memory Extraction Job]
    
    subgraph Memory Extraction Pipeline
        H --> H1[Fetch buffer messages except latest CHAT_BUFFER_TRIM]
        H1 --> H2[Fetch current Memory Profile]
        H2 --> H3[Send to LLM with Extraction Prompt]
        H3 --> H4[LLM outputs structured JSON adjustments]
        H4 --> H5[Apply adjustments atomically to user_profiles]
        H5 --> H6[Trim processed messages from chat_buffers]
    end
    
    H6 --> G
    G --> I[Send Persona + Memory + Active History to LLM]
    I --> J[Generate Response & Send to User]
    J --> K[Insert Bot Response into chat_buffers Collection]
    K --> L[Release Chat Lock]
```

### Step-by-Step Processing Flow

1.  **Enqueue & Coalescing**: When the user sends a message, it is enqueued. A batching timer (`MESSAGE_BATCH_DELAY_SECS`, default 1.5 seconds) runs. If the user sends another message before it expires, the timer resets. To prevent infinite postponement from spammers, a hard deadline (`MAX_BATCH_DELAY_SECS`, default 5.0 seconds) is enforced from the first message in the batch. Once this deadline is crossed, the batch is immediately forced to process. The bot sends a Telegram "typing..." action during this delay and the subsequent generation.
2.  **Lock Acquisition**: Once the batch delay expires or the deadline is hit, the system acquires the user's `chat_lock`. This serialized lock ensures that only one response pipeline runs at a time per user. Any messages sent by the user during LLM processing accumulate in the queue and are processed in the next batch.
3.  **Buffer Append**: The combined messages are written to the database buffer in the `chat_buffers` collection using `$push`.
4.  **Threshold Check**: The system computes the total character length of the messages in `chat_buffers` for that user.
5.  **Extraction Trigger**: If the character count matches or exceeds `CHAT_BUFFER_MAX_CHARS` (default 10,000 characters):
    *   All messages in the buffer **except** the latest `CHAT_BUFFER_TRIM` (default 10) messages are read (these constitute the older "queued" messages).
    *   The current facts, events, beliefs, and profile summary are retrieved.
    *   The system calls the extraction model (`LLM_EXTRACTION_MODEL`) requesting updates.
    *   The returned JSON conforms to the `MemoryExtraction` schema and contains:
        *   `profile_updates`: Dynamic updates to the user's `communication_style` preference.
        *   `new_facts` / `updated_facts` / `removed_facts`: Full CRUD operations to keep user facts synchronized.
        *   `new_beliefs` / `updated_beliefs` / `removed_beliefs`: Full CRUD operations to keep subjective beliefs synchronized.
        *   `new_events` / `updated_events` / `removed_events`: Full CRUD operations to keep timeline milestones updated.
        *   `emotional_state`: Shifts in user mood, intensity, and triggers.
    *   The changes are transactionally written to the user's profile document inside `user_profiles` using atomic `$set` operations, applying **hard deletes** for removals.
    *   The processed segment (older messages) is trimmed from `chat_buffers`.
6.  **Memory Compression (Background Task)**: When compiling the memory profile, if its length exceeds `USER_MEMORY_BUDGET_CHARS` (default 4,000 characters), a non-blocking background task is spawned. The `UserTaskManager` ensures a shared sequential lock (`memory_lock`) is acquired per user; concurrent extraction/compression tasks are skipped. This task sends all memory components to the LLM to compress them to ≤ 80% of the budget. It is the only phase where the high-level `profile_summary` is rewritten, as synthesizing a summary requires a bird's-eye view of all memories, which is not available to the localized extraction steps. The compressed profile, facts, beliefs, and events then atomically replace the old records in the user profile document.
7.  **Prompt Assembly**: The chat manager loads the personality from `persona.md` and reads the memory blocks from `user_profiles` to build a comprehensive system prompt.
8.  **Input/Output Guards**:
9.  **Generation & Send**: The main chatbot model (`LLM_MODEL`) generates a response, which is saved to the buffer, sent back to Telegram, and the `chat_lock` is released.

---

## 🧱 Component Breakdown

```mermaid
graph TB
    subgraph "Telegram Layer (aiogram)"
        U[Telegram App User] <-->|HTTPS API / Webhooks| TG[Telegram API]
        TG <-->|Async Loop| Main[main.py Entrypoint]
        Main --> Handlers[app/handlers/]
        Handlers -->|Commands| Cmds[commands.py]
        Handlers -->|Messages| Msgs[messages.py]
    end

    subgraph "Core Orchestration Engine"
        Msgs -->|user_id, text| Manager[chat_manager.py]
        Manager -->|Load Persona| Persona[persona.md]
        Manager -->|Get Compiled Profile| Loader[memory_loader.py]
        Manager -->|Trigger Extraction| Extractor[memory_extractor.py]
        Manager -->|Query LLM API| LLM[llm_service.py]
        Compressor[memory_compressor.py] -.->|Run Background Compression| DB[(MongoDB Database)]
    end

    subgraph "Data Storage Layer"
        Loader -->|Read Memory| Models[app/database/models.py]
        Extractor -->|Write Memory| Models
        Models <-->|Async Queries| DB
        Conn[connection.py] -->|Client Singleton & Indexes| DB
    end

    style U fill:#4f46e5,color:#fff
    style TG fill:#0284c7,color:#fff
    style LLM fill:#0d9488,color:#fff
    style DB fill:#b91c1c,color:#fff
```

### 1. Presentation & Telegram Router (`app/handlers/`)
Built with `aiogram 3.x`, this layer registers routers and filters. It extracts Telegram message information, ensures async operation, and manages bot-side interactions (like displaying the typing state to users while waiting for the LLM).

### 2. Business Logic & Services (`app/services/`)
*   **[chat_manager.py](../app/services/chat_manager.py)**: The central transaction pipeline orchestrating the buffer checks, memory compilation, calling the LLM wrapper, and updating history.
*   **[memory_loader.py](../app/services/memory_loader.py)**: Compiles raw database documents (Facts, Beliefs, Events, Moods) into a human-readable text block formatted specifically for LLM context ingestion.
*   **[memory_extractor.py](../app/services/memory_extractor.py)**: Handles the structured parsing of past conversations, transforming text history into database modifications.
*   **[memory_compressor.py](../app/services/memory_compressor.py)**: Runs non-blocking background compression. When the total characters of compiled user memories exceed `USER_MEMORY_BUDGET_CHARS`, it triggers an LLM compression job to condense the user details, facts, beliefs, and events.
*   **[llm_service.py](../app/services/llm_service.py)**: Low-level API connector. Handles structured parsing with local fallback functionality and records centralized LLM call details to the `llm_audit_log` collection.

### 3. Database Layer (`app/database/`)
Powered by `motor` async MongoDB client. It manages connections, initializes indexes, and executes transactional updates.

---

## 🔒 Data Security & Multi-User Isolation

To support hundreds of concurrent users without data leakage, the database schema is strictly keyed.

*   Every collection (`user_profiles`, `chat_buffers`, `llm_audit_log`) uses the unique, system-level `user_id` provided by Telegram as the primary key (`_id`) or an indexed filter key.
*   All queries executed by the backend are strictly parameterized and filtered by `user_id`.
*   No global variables hold memory context, eliminating state bleeding between concurrent requests.
