# ThinkMate — Self-Learning Telegram AI Companion

> 🚀 **Live Demo:** Chat with the bot on Telegram: [@ThinkMate_AIBot](https://t.me/ThinkMate_AIBot)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![Database: MongoDB](https://img.shields.io/badge/Database-MongoDB-47A248.svg?logo=mongodb&logoColor=white)](https://www.mongodb.com/)

ThinkMate is a self-learning, long-term memory Telegram AI companion. Instead of forgetting everything the moment a session ends, ThinkMate continuously tracks your life events, conversational vibe, and ongoing context. It is designed to feel less like a bot and more like chatting with a highly observant, long-term friend.

Designed for self-hosted deployment, ThinkMate runs entirely on infrastructure you own (Python 3.12, MongoDB, and any OpenAI-compatible API or local LLM like Llama-3/Gemini), guaranteeing absolute data ownership and multi-tenant isolation with zero reliance on third-party memory SaaS platforms.

---

## 🌟 Major Features

*   **Self-Learning Long-Term Memory**: Automatically extracts and tracks your core details, facts, subjective beliefs, and chronological life updates without storing raw chat logs forever.
*   **Asynchronous Dual-Brain Engine**: Splits operations into a fast conversational loop (Hot Path) and a background memory reflection pass (Cold Path) to keep responses near-instant.
*   **Self-Healing Memory Budget**: Automatically compresses and deduplicates your memory profile in the background once it exceeds context limits.
*   **Group Chat & Ambient Reply Gate**: Intelligently participates in group chats using user-specific affinity scores and a cooldown-based gateway, preventing API spam.
*   **Hot-Swappable Persona**: Allows you to adjust the bot's tone, rules, and rules of engagement dynamically by editing [persona.md](persona.md) (no reboot required).
*   **Observability & Telemetry**: Built-in admin dashboard tracking LLM response latency, token usage, queue depth, and service health stats.

---

## 🧠 Core Concept: The "Dual-Brain" Architecture

To achieve continuous learning without skyrocketing API costs or latency, the system splits operations into two independent, lock-guarded agentic pathways:

*   **Brain 1: The Talker (Hot Path)**
    *   **Latency & Low-Cost Focus**: The front-end agent that interacts directly with the user.
    *   **Sliding Window**: It only processes a small window of the last 5-10 messages, paired with a dynamically injected "Memory Profile."
    *   **Coalescing & Single-Turn Output**: Batches rapid-fire user messages and generates both the reply and an emoji reaction in a single LLM call.
*   **Brain 2: The Observer (Cold Path)**
    *   **Background Cognitive Worker**: Runs silently in the background.
    *   **Memory Extraction & Compression**: When the message buffer fills up, it extracts new facts, subjective beliefs, and emotional trends to update the user's permanent profile.
    *   **Consolidation ("Dreaming")**: Periodically reflects on the entire memory profile to synthesize deep behavioral insights and merge duplicate info.

---

## 📖 Documentation Index

All detailed explanations, architecture diagrams, installation runbooks, and implementation designs live in the `docs/` directory.

### 🏁 Getting Started
*   **[Quick Start & Setup Guide](docs/setup_guide.md)** — Step-by-step instructions to configure, run, and host the bot using `uv` and MongoDB.
*   **[Repository Structure & Tech Stack](docs/repository_structure.md)** — Layout of the codebase, module descriptions, and external dependencies.

### 📐 Architecture & Design
*   **[System Architecture Spec](docs/architecture.md)** — Deep dive into the data pipelines, locks, and the hot/cold cognitive paths.
*   **[Project Implementation Plan](docs/project_plan.md)** — Phase-by-phase implementation details and development checklists.

### 🛠️ Subsystem Development Guides
*   **[Telegram Bot Handler](docs/development/telegram_bot.md)** — Handlers, middlewares, custom rate limiters, and interactive guide configurations.
*   **[Database Layer](docs/development/database.md)** — MongoDB schema design, indexing, and motor async CRUD patterns.
*   **[LLM Integration](docs/development/llm_integration.md)** — Prompt designs, JSON mode parser fallbacks, and auditing.
*   **[Memory & Compression Engine](docs/development/memory_engine.md)** — Sliding-window extraction, memory budget compression, and the dreaming consolidator.
*   **[Group Chat Dynamics](docs/development/group_chat.md)** — Mentions, ambient reply gating, and user affinity matrices.
*   **[Performance & Scaling](docs/development/performance_and_scaling.md)** — Concurrency bounds, scale-out paths, and telemetry.
*   **[Observability & Telemetry](docs/development/observability.md)** — In-process metrics registry, health logs, and admin diagnostics.
*   **[Production Hardening Plan](docs/development/hardening_plan.md)** — Memory isolation, safety bounds, and defense-in-depth guidelines.
*   **[Testing Infrastructure](docs/development/testing_guide.md)** — Automated test suite with Pytest and database mocking.
*   **[Configuration Reference](docs/development/configuration.md)** — Index of all customizable environment variables and tuning parameters.

---

## 📞 Contact & Support

For updates, questions, or community discussion, join our official channels:
*   📢 **Updates Channel**: [@TechZBots](https://t.me/TechZBots)
*   💬 **Support Group**: [TechZBots Support](https://t.me/TechZBots_Support)

---

## 📄 License

This project is open-source and available under the [MIT License](LICENSE).
