# Documentation

ThinkMate's documentation describes the current repository. Start with the guide for your task, then follow links to the implementation. [The project README](../README.md) gives the overview.

## Run and operate the bot

| Guide | Use it to |
| --- | --- |
| [Setup](setup_guide.md) | Prepare Python, MongoDB, Telegram, and an LLM; verify a first conversation. |
| [Operations](operations.md) | Deploy with Docker/Compose, monitor, troubleshoot, back up, and update. |
| [Configuration](development/configuration.md) | Look up every environment setting and distinguish code defaults from template values. |
| [Telegram commands](development/telegram_bot.md) | Understand command behavior, scope, delivery, and permissions. |
| [Group behavior](development/group_chat.md) | Configure participation and understand routing and memory sharing. |

## Understand and change the code

Recommended reading order: [architecture](architecture.md) → [repository map](repository_structure.md) → the subsystem you want to change → [testing](development/testing_guide.md).

| Reference | Covers |
| --- | --- |
| [Architecture](architecture.md) | Message lifecycle, background work, storage boundaries, and concurrency. |
| [Repository map](repository_structure.md) | Module responsibilities and common change locations. |
| [Memory engine](development/memory_engine.md) | Extraction, compilation, compression, consolidation, and check-ins. |
| [Database](development/database.md) | Collections, updates, indexes, retention, and reset scope. |
| [LLM integration](development/llm_integration.md) | Model requests, schemas, retries, parsing, and audit behavior. |
| [Persona customization](development/persona.md) | Edit the runtime prompt and understand rules supplied by code. |
| [Testing](development/testing_guide.md) | Automated fixtures, regression suites, and manual live checks. |
| [Contributing](../CONTRIBUTING.md) | Development workflow and documentation maintenance. |

## Assess limits and future work

| Reference | Covers |
| --- | --- |
| [Observability](development/observability.md) | Health, metrics, audit records, logs, and Telegram forwarding. |
| [Performance and scaling](development/performance_and_scaling.md) | Batching, latency, resource bounds, and the single-process deployment model. |
| [Safeguards and limitations](development/hardening_plan.md) | Implemented protections and remaining gaps. |
| [Project audit and implementation backlog](project-audit.md) | Detailed findings, essential gaps, future options, dependencies, and acceptance checks reviewed on 11 October 2026. |
| [Roadmap](roadmap.md) | Current capabilities and proposed next steps. |

## Sources of truth

- [app/config.py](../app/config.py) defines environment parsing and code defaults. [.env.example](../.env.example) is an editable sample with some intentional overrides.
- [pyproject.toml](../pyproject.toml) declares dependencies; [uv.lock](../uv.lock) pins local and [Dockerfile](../Dockerfile) runtime installs. [requirements.txt](../requirements.txt) exports those locked versions with the test packages.
- Source code defines runtime behavior. Tests cover selected cases and do not establish universal security, performance, or provider compatibility guarantees.
- [persona.md](../persona.md) is a runtime prompt loaded by the bot, not a developer guide. Read [persona customization](development/persona.md) before editing it.

Subsystem filenames in `development/` remain stable for existing references. The former phase-by-phase `project_plan.md` has been replaced by the [roadmap](roadmap.md); implemented behavior belongs in the subsystem guides.

When changing documentation, check local links and heading anchors, and verify behavior and defaults against their sources of truth.
