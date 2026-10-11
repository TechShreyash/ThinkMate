# Repository map

[Documentation index](README.md) · [Architecture](architecture.md) · [Contributing](../CONTRIBUTING.md)

The application separates Telegram handling, conversation/memory services, prompt templates, and persistence. This map lists maintained source files; generated environments, logs, and caches are excluded.

## Root files

| File | Responsibility |
| --- | --- |
| [main.py](../main.py) | Startup, middleware/router registration, long polling, scheduler lifecycle, and shutdown. |
| [pyproject.toml](../pyproject.toml) | Project metadata, runtime dependencies, optional `dev` extra, and pytest configuration. |
| [uv.lock](../uv.lock) | Resolved dependency versions for uv installs. |
| [.python-version](../.python-version) | Development interpreter pin. |
| [requirements.txt](../requirements.txt) | Generated locked dependency export including runtime and test packages. |
| [.env.example](../.env.example) | Sample environment configuration; some values override code defaults. |
| [persona.md](../persona.md) | Runtime persona prompt. |
| [Dockerfile](../Dockerfile) | Bot-only image, unprivileged user, and runtime dependency install from `uv.lock`. |
| [docker-compose.yml](../docker-compose.yml) | Single bot service, optional local env file, and persistent log volume. |
| [README.md](../README.md) | Project entry point and quick start. |
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Workflow and documentation maintenance. |

## Application modules

```text
app/
├── __init__.py                 # Console and rotating file logging
├── config.py                   # Environment parsing and Config instance
├── handlers/
│   ├── __init__.py             # Router composition and command engagement middleware
│   ├── commands.py             # Commands, inline help, configuration-driven menus
│   ├── membership.py           # Group membership transition introductions
│   ├── messages.py             # Text/media routing and group engagement decisions
│   └── middlewares.py          # Throttle, DB injection, proactive streak reset
├── database/
│   ├── connection.py           # Motor singleton, UTC codecs, startup ping/indexes
│   └── models.py               # Profile, buffer, member, audit and metrics CRUD
├── services/
│   ├── chat_manager.py         # Reply flow, persona cache and time context
│   ├── user_task_manager.py    # Per-chat batching, locks, typing and delivery
│   ├── llm_service.py          # OpenAI-compatible client, retries, parsing, audit
│   ├── schemas.py              # Pydantic response/memory schemas
│   ├── memory_loader.py        # Compile stored profile into prompt text
│   ├── memory_extractor.py     # Private/group extraction and buffer trim
│   ├── memory_compressor.py    # Compression, cooldown and budget shedding
│   ├── memory_consolidator.py  # Scheduled whole-profile consolidation
│   ├── affinity.py             # Per-group member affinity/mode cache
│   ├── group_gate.py           # Addressing, spam, implicit and ambient helpers
│   ├── reactions.py            # Normalize emoji to supported reactions
│   ├── telegram_text.py        # Response caps, safe chunks and reply fallback
│   ├── metrics.py              # In-process counters, gauges and timers
│   ├── health.py               # Health helpers and background schedulers
│   ├── log_forwarder.py        # Optional Telegram operational forwarding
│   └── error_log_sink.py       # WARNING+ sink and filtering for Telegram forwarding
└── prompts/
    ├── system_prompt.py        # Persona, behavior rules, memory and context
    ├── extraction_prompt.py    # Structured private/group extraction
    ├── compression_prompt.py   # Condense memories to a target size
    ├── consolidation_prompt.py # Synthesize insights and revise memory
    └── checkin_prompt.py       # Proactive opener generation
```

Each package also contains an `__init__.py`. [tests/](../tests/) contains pytest unit/regression/property tests and the manual [run_llm_live.py](../tests/run_llm_live.py) script.

## Where to make a change

| Task | Start in | Read alongside |
| --- | --- | --- |
| Add/rename a command | [commands.py](../app/handlers/commands.py), [config.py](../app/config.py) | [Telegram guide](development/telegram_bot.md) |
| Change which group messages get a reply | [messages.py](../app/handlers/messages.py), [group_gate.py](../app/services/group_gate.py) | [Group guide](development/group_chat.md) |
| Change batching or delivery | [user_task_manager.py](../app/services/user_task_manager.py), [telegram_text.py](../app/services/telegram_text.py) | [Performance](development/performance_and_scaling.md) |
| Change tone | [persona.md](../persona.md), [system_prompt.py](../app/prompts/system_prompt.py) | [Persona customization](development/persona.md) |
| Change memory behavior | [memory_extractor.py](../app/services/memory_extractor.py), [memory_loader.py](../app/services/memory_loader.py), compressor/consolidator | [Memory guide](development/memory_engine.md) |
| Change structured LLM output | [schemas.py](../app/services/schemas.py), [llm_service.py](../app/services/llm_service.py), relevant prompt | [LLM guide](development/llm_integration.md) |
| Change storage/retention | [models.py](../app/database/models.py), [connection.py](../app/database/connection.py) | [Database guide](development/database.md) |
| Change check-ins or scheduler cadence | [health.py](../app/services/health.py), [models.py](../app/database/models.py), [checkin_prompt.py](../app/prompts/checkin_prompt.py) | [Configuration](development/configuration.md) |
| Add a diagnostic | [metrics.py](../app/services/metrics.py), [health.py](../app/services/health.py), relevant caller | [Observability](development/observability.md) |

## Dependencies

Runtime dependencies are aiogram (Telegram), Motor/MongoDB (persistence), the OpenAI SDK (compatible client), Pydantic (typed schemas), python-dotenv (local settings), and Loguru (logging). The `dev` extra adds pytest, pytest-asyncio, mongomock, and Hypothesis.

Dependency ranges are declared in `pyproject.toml`; use `uv sync --locked` for the checked-in versions. Docker installs runtime packages from the same `uv.lock`, while `requirements.txt` exports locked runtime and test packages for pip-based environments. Regenerate the export when changing dependencies; see [Contributing](../CONTRIBUTING.md#refresh-dependencies), [operations](operations.md), and [testing](development/testing_guide.md).

Documentation is organized by task in the [documentation index](README.md). Current behavior belongs in subsystem references; future work belongs in the [roadmap](roadmap.md).
