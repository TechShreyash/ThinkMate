# ThinkMate — Self-Learning Telegram AI Chatbot

ThinkMate is a self-learning, long-term memory Telegram AI companion. Inspired by concepts like Hermes Agent and Hindsight, it operates without third-party memory SaaS providers, maintaining full ownership and control over its database.

Rather than relying on simple session timeouts or expensive vector databases, ThinkMate implements a **Sliding Window Chat Buffer** combined with a custom memory model in MongoDB. This allows the bot to continuously extract facts, events, and emotional states from conversational overflows and inject them back into the LLM's system prompt as structured memory.

---

## 🌟 Key Features

*   **Sliding Window Memory**: Keeps the last $N$ messages in active context. When the limit is reached, it automatically extracts facts and events.
*   **Character-Budget Memory Profile**: Consolidates user profiles, facts, subjective beliefs, events, and moods into a unified text block. If the text block size exceeds `USER_MEMORY_BUDGET_CHARS` (default 4,000 chars), a non-blocking background compression task is triggered to reduce memory usage to ≤ 80% of the budget.
*   **Input/Output Guards**: Early-return input guards ignore overly long user messages (preventing essays/code abuse), and output guards cap LLM response lengths at API level.
*   **Custom LLM Endpoint Compatibility**: Works with standard OpenAI models or any local/self-hosted LLM engines via OpenAI-compatible APIs (LM Studio, Ollama, vLLM, OpenRouter).
*   **Editable Persona**: Change the bot's tone, rules, and traits dynamically by editing the [persona.md](persona.md) markdown file—no service restart required.
*   **Dynamic Message Reactions**: The conversational reply and an optional Telegram emoji reaction are produced in a **single** LLM call (strict JSON `{reply, reaction}`), then the reaction is normalized to Telegram's accepted set and applied gracefully.
*   **Data Isolation**: Built-in support for multi-user chat with strict per-user database isolation.
*   **Pure Python & Async**: Powered by `aiogram 3.x` and `motor` (MongoDB async driver) for high performance and standard async workflow.

---

## 📂 File/Folder Structure

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
│       └── hardening_plan.md       # Production hardening & scaling plan (living doc)
│
├── app/                            # Source code directory
│   ├── __init__.py
│   ├── config.py                   # Configuration and validation loading
│   │
│   ├── handlers/                   # Telegram event handlers (aiogram)
│   │   ├── __init__.py
│   │   ├── commands.py             # Slash commands (/start, /profile, /reset)
│   │   └── messages.py             # Default message router & main handler
│   │
│   ├── services/                   # Core business logic
│   │   ├── __init__.py
│   │   ├── llm_service.py          # AsyncOpenAI wrapper: combined reply call, retries, audit
│   │   ├── schemas.py              # Pydantic schemas (ReplyBundle, extraction, compression)
│   │   ├── reactions.py            # Telegram reaction whitelist + normalization
│   │   ├── chat_manager.py         # Response flow orchestrator
│   │   ├── memory_extractor.py     # Memory extraction LLM interface
│   │   ├── memory_loader.py        # System prompt memory compiler
│   │   ├── memory_compressor.py    # LLM-powered memory compressor + budget enforcement
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
│   │   └── compression_prompt.py   # Memory compression instructions
│   │
│   └── utils/                      # Helper modules
│       ├── __init__.py
│       └── helpers.py              # Parsing, formatting, and time helpers
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
    *   [Testing Infrastructure & Mocking Suite](docs/development/testing_guide.md)
    *   [Configuration & Tuning Parameters Reference](docs/development/configuration.md)

---

## 🛠️ Tech Stack Overview

*   **Language**: Python 3.10+
*   **Telegram Framework**: `aiogram` (v3.x) with DB dependency injection & auto-typing indicators
*   **Database**: `MongoDB` (via `motor` async driver)
*   **LLM Client**: `openai` SDK against any OpenAI-compatible endpoint, JSON-mode structured outputs (with native-parse opt-in), transient-error retries, and centralized audit logging
*   **Data Validation**: `Pydantic` (v2.x) schemas for guaranteed JSON outputs
*   **Environment Config**: `python-dotenv`
*   **Logging**: `loguru`
*   **Testing**: `pytest` & `pytest-asyncio` (with `mongomock` in-memory mocks)

---

## 📄 License

This project is open-source and available under the MIT License.
