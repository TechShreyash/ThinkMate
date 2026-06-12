---
trigger: always_on
description: Instructs the Antigravity agent to track all codebase changes, maintain open-source friendly and highly explanatory docs with proper linking/navigation, and update the root README.md when necessary.
---

# Documentation Tracker Rule

Whenever any file in this repository is modified, created, or deleted, you MUST update the corresponding documentation files in the [docs](file:///d:/ThinkMate/docs) directory or the root [README.md](file:///d:/ThinkMate/README.md) to reflect these changes accurately.

## Core Directives

1. **Monitor Repository Changes**:
   - Keep continuous track of every code modification, implementation detail, refactoring effort, database schema alteration, prompt adjustment, and package dependency change.

2. **Keep Docs In Sync & Open-Source Friendly**:
   - Do not defer documentation updates. Maintain all files as a real-time, accurate reflection of the current codebase state.
   - Ensure the documentation is highly explanatory, welcoming, and accessible. Because the project is open-source, any developer or contributor should be able to read the documentation and understand the system easily. Avoid assumptions, define terms clearly, and explain the "why" behind design decisions.

3. **Proper Linking & Navigation**:
   - Provide proper navigation headers or indexes in all guides (e.g., Table of Contents, Back to Top, Next/Previous section links).
   - Ensure thorough cross-linking between files. If a document references a database model, link directly to the [database.md](file:///d:/ThinkMate/docs/development/database.md) file.
   - Use correct file paths with the `file://` scheme when referencing files (e.g., `[README.md](file:///d:/ThinkMate/README.md)`).

4. **Update root README.md**:
   - When introducing high-level changes, features, new requirements, or config structures, update the root [README.md](file:///d:/ThinkMate/README.md) to keep it updated as the primary entry point for the project.

5. **Documentation Alignment Mapping**:
   - **Root Readme & Entry**: If changing features, project structure, or high-level details, update [README.md](file:///d:/ThinkMate/README.md).
   - **Architecture & System Flows**: If changing the sliding window memory logic, data flow, or system topology, update [architecture.md](file:///d:/ThinkMate/docs/architecture.md).
   - **Environment, Setup & Config**: If adding/editing environment variables or installation steps, update [setup_guide.md](file:///d:/ThinkMate/docs/setup_guide.md) and [.env.example](file:///d:/ThinkMate/.env.example).
   - **Project Checklist / Milestones**: If implementing a new feature or completing a plan item, mark off the task as done in [project_plan.md](file:///d:/ThinkMate/docs/project_plan.md).
   - **Telegram Handlers**: If modifying bots, routers, filters, or middleware, update [docs/development/telegram_bot.md](file:///d:/ThinkMate/docs/development/telegram_bot.md).
   - **Database Schema & Async Queries**: If adding tables, indexes, or CRUD logic, update [docs/development/database.md](file:///d:/ThinkMate/docs/development/database.md).
   - **LLM Integrations & System Prompts**: If changing templates, JSON validation schemas, or the OpenAI client wrapper, update [docs/development/llm_integration.md](file:///d:/ThinkMate/docs/development/llm_integration.md).
   - **Memory Engine Mechanics**: If updating consolidation prompts, facts extraction, or sliding window buffers, update [docs/development/memory_engine.md](file:///d:/ThinkMate/docs/development/memory_engine.md).

6. **Style and Detail**:
   - Document technical choices, class/method names, and API payloads clearly.
   - Use precise, professional language and format outputs with appropriate Markdown elements (fenced code blocks, Mermaid diagrams, lists).