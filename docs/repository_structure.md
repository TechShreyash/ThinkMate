# Repository Structure & Tech Stack

This document details the codebase layout, module responsibilities, and the core technologies powering the ThinkMate AI companion.

---

## 📂 File & Folder Structure

The map below outlines the repository's modules and documentation files:

```
ThinkMate/
├── .env.example                    # Environment configuration template
├── .gitignore                      # Git configuration
├── README.md                       # Core entrypoint & high-level brief
├── requirements.txt                # Python package requirements
├── main.py                         # Application entrypoint & worker lifecycle
├── persona.md                      # Hot-swappable bot personality definition
│
├── docs/                           # Architectural specs & design guides
│   ├── architecture.md             # Detailed data flows, mechanics & sequence guides
│   ├── setup_guide.md              # Step-by-step setup (MongoDB, BotFather, LLMs)
│   ├── project_plan.md             # Phase-by-phase implementation checklist
│   ├── repository_structure.md     # [THIS FILE] Codebase structure and tech stack reference
│   └── development/                # Subsystem implementation details
│       ├── telegram_bot.md         # aiogram handlers, routers & middleware
│       ├── database.md             # Async MongoDB models, indexes & connection hooks
│       ├── llm_integration.md      # LLM clients, retry policies & JSON parsing
│       ├── memory_engine.md        # Sliding window, extraction, & dreaming mechanics
│       ├── group_chat.md           # Group dynamics, ambient gating, & user affinity
│       ├── configuration.md        # Configuration variables tuning index
│       ├── testing_guide.md        # Pytest framework & mongomock configuration
│       ├── performance_and_scaling.md  # Latency, scaling ceilings, & scale-out path
│       ├── observability.md        # Telemetry metrics registry & logging sinks
│       └── hardening_plan.md       # Bounded state guides & security hardening plans
│
├── app/                            # Source code directory
│   ├── config.py                   # Environment validation & settings schemas
│   ├── handlers/                   # aiogram event dispatch routers
│   │   ├── commands.py             # Slash commands (/start, /guide, /health, /metrics)
│   │   ├── membership.py           # Group join transition greetings
│   │   ├── messages.py             # Chat-type router & ambient gate handoff
│   │   └── middlewares.py          # Rate limiting middleware & session injects
│   │
│   ├── services/                   # Business logic layers (Agent Services)
│   │   ├── llm_service.py          # Async LLM connector & call auditor
│   │   ├── chat_manager.py         # Response flow orchestrator
│   │   ├── memory_loader.py        # System prompt compilation
│   │   ├── memory_extractor.py     # Brain 2: JSON extraction pipeline
│   │   ├── memory_compressor.py    # Brain 2: Background character-budget compressor
│   │   ├── memory_consolidator.py  # Brain 2: Periodic "dreaming" pass
│   │   ├── group_gate.py           # Group ambient gate decision manager
│   │   ├── affinity.py             # Read/write-through group affinity cache
│   │   ├── user_task_manager.py    # Concurrency control & typing indicator manager
│   │   ├── metrics.py              # Telemetry registry (Counters, Gauges, Timers)
│   │   └── health.py               # Liveness checks & metrics logger
│   │
│   ├── database/                   # Storage connector layer
│   │   ├── connection.py           # Motor client singleton & index initializer
│   │   └── models.py               # MongoDB document CRUD interface
│   │
│   └── prompts/                    # Cognitive prompt templates
│       ├── system_prompt.py        # Conversational prompt assembler
│       ├── extraction_prompt.py    # Structured memory extraction prompt
│       ├── compression_prompt.py   # Memory compression prompt
│       ├── consolidation_prompt.py # Dreaming & insight synthesis prompt
│       └── checkin_prompt.py       # Proactive nudge generation prompt
│
└── tests/                          # Testing suite (Pytest + Mongomock)
```

---

## 🛠️ Tech Stack & Dependencies

ThinkMate is built on an async-first Python stack, optimized for single-process high-performance polling:

*   **Runtime**: Python 3.12+
*   **Telegram Handler**: `aiogram` (v3.x) with custom middleware for database injection, rate-limiting, and concurrent user locking.
*   **Database**: `MongoDB` utilizing the `motor` asynchronous driver.
*   **LLM Client**: `openai` SDK compatible with any provider (OpenAI, OpenRouter, Groq, LM Studio, Ollama).
*   **Data Validation**: `Pydantic` (v2.x) for strict validation of LLM outputs (JSON Mode).
*   **Logging**: `loguru` configured with a warning-level sink to forward issues to an admin Telegram channel.
*   **Performance Manager**: `uv` package manager for virtual environment isolation and deterministic installs.
*   **Testing Suite**: `pytest` and `pytest-asyncio` with in-memory `mongomock` databases.
