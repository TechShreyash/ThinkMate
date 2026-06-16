# ThinkMate — Self-Learning Telegram AI Chatbot

> 🤖 **Try the live demo:** [@ThinkMate_AIBot](https://t.me/ThinkMate_AIBot) on Telegram.

ThinkMate is a self-learning, long-term memory Telegram AI companion — a chatbot that keeps remembering you across conversations instead of forgetting everything the moment a session ends. Inspired by concepts like Hermes Agent and Hindsight, it runs entirely on infrastructure you own: there are no third-party memory SaaS providers in the loop, so you keep full ownership and control over its database.

Most chatbots either drop their context when a session times out or lean on expensive vector databases to fake recall. ThinkMate takes a different route. It pairs a **Sliding Window Chat Buffer** — a rolling window that keeps only the most recent messages in active context — with a custom memory model stored in MongoDB. As older messages slide out of that window, the bot extracts the facts, events, and emotional states worth keeping and injects them back into the LLM's (Large Language Model's) system prompt as structured, long-term memory. The payoff is continuity: the bot recalls what matters without paying to store every message forever.

This README is the project's entry point. It summarizes the headline features, maps the repository layout, lists the tech stack, and links out to the detailed guides under `docs/`. If you're new here, read this page first, then follow the [Complete Documentation Index](#-complete-documentation-index) below to go deeper.

---

## 🌟 Key Features

Each feature below links to the guide that explains it in depth, so you can skim the highlights here and dive into specifics when you need them.

*   **Sliding Window Memory**: Keeps the last $N$ messages in active context. When the limit is reached, it automatically extracts facts and events.
*   **Character-Budget Memory Profile**: Consolidates user profiles, facts, subjective beliefs, events, and moods into a unified text block. If the text block size exceeds `USER_MEMORY_BUDGET_CHARS` (default 4,000 chars), a non-blocking background compression task is triggered to reduce memory usage to ≤ 80% of the budget.
*   **Input/Output Guards**: Early-return input guards ignore overly long user messages (preventing essays/code abuse), and output guards cap LLM response lengths at API level.
*   **Custom LLM Endpoint Compatibility**: Works with standard OpenAI models or any local/self-hosted LLM engines via OpenAI-compatible APIs (LM Studio, Ollama, vLLM, OpenRouter).
*   **Editable Persona**: Change the bot's tone, rules, and traits dynamically by editing the [persona.md](persona.md) markdown file—no service restart required.
*   **Dynamic Message Reactions**: The conversational reply and an optional Telegram emoji reaction are produced in a **single** LLM call (strict JSON `{reply, reaction}`), then the reaction is normalized to Telegram's accepted set and applied gracefully.
*   **Data Isolation**: Built-in support for multi-user chat with strict per-user database isolation.
*   **Group Chat & Affinity** *(supported; see [group_chat.md](docs/development/group_chat.md))*: In groups the bot always replies when addressed (mention, name, or reply-to-bot) and otherwise chimes in selectively through a no-LLM ambient gate (cooldown → keyword/scan-tick → affinity-weighted probability), keeping it engaging without spam or API abuse. Per-(chat, user) affinity and `/quiet` `/chatty` modes tune its chattiness, and group memory is extracted multi-party while staying attributed per user. DMs are unchanged.
*   **Built for load**: Single long-polling instance hardened for 50k+ users — one LLM call per reply, ~3 DB round-trips on the hot path, bounded in-memory state, and a documented scale-out path (see [performance_and_scaling.md](docs/development/performance_and_scaling.md)).
*   **Observability & ops** *(Phase 10)*: A dependency-free, in-process metrics registry tracks LLM volume/latency, throttle and queue drops, active conversations, and background-job runs. An admin `/health` command reports liveness, readiness, and a metrics summary, with an optional periodic metrics logger — all explained in the [Observability & Ops Runbook](docs/development/observability.md).
*   **Memory Consolidation ("dreaming" pass)** *(Phase 11)*: An optional periodic background pass reviews each user's whole profile to refresh the summary/style, merge duplicates, and synthesize a small bounded set of durable **behavioral insights** that localized extraction can't see. It runs entirely off the hot path under the shared memory lock, never wipes memory on failure, and is **disabled by default** — see [memory_engine.md](docs/development/memory_engine.md#-phase-11--periodic-consolidation-the-dreaming-pass-implemented).
*   **Engagement & UX** *(Phase 12)*: Small, additive touches that make the bot feel more present — **temporal context** ("now" and a coarse "last talked" gap in the prompt), **emotional continuity** (a bounded mood-history trend rather than just the latest mood), a static no-LLM **`/onboard`** intro, and optional **proactive check-ins** that occasionally send a memory-grounded nudge to inactive users (opt-out via `/pause`, quiet-hours aware, **disabled by default**). See [memory_engine.md](docs/development/memory_engine.md#-phase-12--temporal-context--emotional-continuity-implemented), [telegram_bot.md](docs/development/telegram_bot.md#-engagement-commands-phase-12-implemented), and [configuration.md](docs/development/configuration.md#-proactive-check-ins-phase-12).
*   **Interactive onboarding guide**: A beginner-friendly **`/guide`** command opens a tap-through tour built on Telegram **inline buttons** — memory, privacy, group chats, check-ins, and the full command list — so first-time users understand what the bot does without reading docs. The same buttons are surfaced from `/start`, `/onboard`, and `/help`. All copy is rename-safe (it follows the configured command triggers). See [telegram_bot.md](docs/development/telegram_bot.md#-interactive-guide-guide--inline-buttons).
*   **Pure Python & Async**: Powered by `aiogram 3.x` and `motor` (MongoDB async driver) for high performance and standard async workflow.

---

## 📂 File/Folder Structure

The tree below is the map of the repository. Source code lives under `app/`, the prose guides live under `docs/`, and the root holds the entry point and configuration templates.

```
ThinkMate/
├── .env.example                    # Environment variables template
├── .gitignore                      # Git ignore file
├── README.md                       # Main project introduction & directory
├── requirements.txt                # Python dependencies
├── main.py                         # Application entrypoint
├── persona.md                      # Bot personality definition
│
├── docs/                           # Documentation folder
│   ├── architecture.md             # High-level architecture, data flows & diagrams
│   ├── setup_guide.md              # Installation, BotFather & API setup
│   ├── project_plan.md             # Phase-by-phase implementation plan & checklist
│   │
│   └── development/                # Implementation detail guides
│       ├── telegram_bot.md         # aiogram handlers, routers & middleware
│       ├── database.md             # MongoDB schema & async document CRUD
│       ├── llm_integration.md      # LLM client, prompt engineering & JSON mode
│       ├── memory_engine.md        # Sliding window, extraction & consolidation
│       ├── group_chat.md           # Group behavior, ambient replies & affinity
│       ├── configuration.md        # Environment variables & tuning reference
│       ├── testing_guide.md        # Test suite structure & mongomock mocking
│       ├── performance_and_scaling.md  # Efficiency, ceiling & scale-out path
│       ├── observability.md        # Metrics, health checks & ops runbook (Phase 10)
│       └── hardening_plan.md       # Production hardening & scaling plan (living doc)
│
├── app/                            # Source code directory
│   ├── __init__.py
│   ├── config.py                   # Configuration and validation loading
│   │
│   ├── handlers/                   # Telegram event handlers (aiogram)
│   │   ├── __init__.py
│   │   ├── commands.py             # Slash commands (/start, /help, /guide, /profile, /reset, /quiet, /chatty, /health, /metrics, /onboard, /pause, /resume) + inline-button guide
│   │   └── messages.py             # Default message router, chat-type routing & ambient gate handoff
│   │
│   ├── services/                   # Core business logic
│   │   ├── __init__.py
│   │   ├── llm_service.py          # AsyncOpenAI wrapper: combined reply call, retries, audit
│   │   ├── schemas.py              # Pydantic schemas (ReplyBundle, extraction, compression)
│   │   ├── reactions.py            # Telegram reaction whitelist + normalization
│   │   ├── chat_manager.py         # Response flow orchestrator
│   │   ├── group_gate.py           # No-LLM group helpers + ambient gate (Phase 9)
│   │   ├── affinity.py             # Per-(chat, user) affinity/mode cache (Phase 9)
│   │   ├── memory_extractor.py     # Memory extraction LLM interface (DM + multi-party group)
│   │   ├── memory_loader.py        # System prompt memory compiler
│   │   ├── memory_compressor.py    # LLM-powered memory compressor + budget enforcement
│   │   ├── memory_consolidator.py  # Periodic "dreaming" consolidation pass + insights (Phase 11)
│   │   ├── metrics.py              # In-process metrics registry (counters, gauges, timers)
│   │   ├── health.py               # Liveness/readiness helpers + periodic metrics logger
│   │   └── user_task_manager.py    # Concurrency, batching, queues & typing indicators
│   │
│   ├── database/                   # Database interaction layers
│   │   ├── __init__.py
│   │   ├── connection.py           # Async client singleton, ping, indexes (incl. audit TTL)
│   │   └── models.py               # MongoDB document CRUD (atomic buffer trim)
│   │
│   ├── prompts/                    # LLM Prompt Templates
│   │   ├── __init__.py
│   │   ├── system_prompt.py        # Chat response prompt assembler
│   │   ├── extraction_prompt.py    # Structured JSON extraction template
│   │   ├── compression_prompt.py   # Memory compression instructions
│   │   └── consolidation_prompt.py # Consolidation ("dreaming") + insights synthesis prompt
│   │
│   └── __init__.py                 # Package init + loguru logging setup
│
└── tests/                          # Automated test suites (pytest + mongomock)
```

---

## 📖 Complete Documentation Index

To implement or contribute to this project, please consult the specialized guides in order:

1.  **[Architecture & Design](docs/architecture.md)**: Details how the sliding window functions and how components interact.
2.  **[Setup Guide](docs/setup_guide.md)**: Configures Telegram Bot tokens, local/remote LLM endpoints, and databases.
3.  **[Step-by-Step Project Plan](docs/project_plan.md)**: A complete, checkbox-driven roadmap from start to deployment.
4.  **[Development Guides](docs/development/telegram_bot.md)**:
    *   [Telegram Bot (`aiogram 3.x`) Integration](docs/development/telegram_bot.md)
    *   [Async MongoDB Schema Design](docs/development/database.md)
    *   [LLM Client & Prompt Engineering](docs/development/llm_integration.md)
    *   [Sliding Window & Memory Engine Mechanics](docs/development/memory_engine.md)
    *   [Group Chat, Ambient Replies & Affinity](docs/development/group_chat.md)
    *   [Performance, Efficiency & Scaling](docs/development/performance_and_scaling.md)
    *   [Observability & Ops Runbook](docs/development/observability.md)
    *   [Production Hardening & Scaling Plan](docs/development/hardening_plan.md)
    *   [Testing Infrastructure & Mocking Suite](docs/development/testing_guide.md)
    *   [Configuration & Tuning Parameters Reference](docs/development/configuration.md)

---

## 🛠️ Tech Stack Overview

ThinkMate is intentionally built on a small, async-first Python stack so the whole bot can run as a single process without external orchestration:

*   **Language**: Python 3.12+
*   **Telegram Framework**: `aiogram` (v3.x) with DB dependency injection & task-manager-driven typing indicators
*   **Database**: `MongoDB` (via `motor` async driver)
*   **LLM Client**: `openai` SDK against any OpenAI-compatible endpoint, JSON-mode structured outputs (with native-parse opt-in), transient-error retries, and centralized audit logging
*   **Data Validation**: `Pydantic` (v2.x) schemas for guaranteed JSON outputs
*   **Environment Config**: `python-dotenv`
*   **Logging**: `loguru`
*   **Testing**: `pytest` & `pytest-asyncio` (with `mongomock` in-memory mocks)

---

## 📄 License

This project is open-source and available under the MIT License.
