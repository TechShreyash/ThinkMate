# Changelog

All notable changes to the ThinkMate project will be documented in this file.

## [2026-06-12] - Implement Character-Budget Memory Compression & Input/Output Guards

### Added
- **Background Memory Compression**: Spawns a non-blocking `compress_user_memory()` background task to consolidate user memory when size exceeds limit.
- **Compression Prompt & Schemas**: Added `app/prompts/compression_prompt.py` and `MemoryCompression` schemas in `schemas.py` to route profile summary, style, facts, events, and emotional log.
- **Character Budget Config**: Added `USER_MEMORY_BUDGET_CHARS` (default 10,000) and `CHARS_PER_TOKEN` (default 4) configurations.
- **Input Guard**: Ignores incoming user messages exceeding `MAX_INPUT_CHARS` (default 1,000 chars) with a friendly deflection reply.
- **Output Guard**: Derives `max_tokens` based on `MAX_RESPONSE_CHARS` (default 1,000 chars) to restrict LLM response lengths at the API level.
- **Commit Rules Agent Guideline**: Created `.agents/rules/commit_rules.md` to automatically update changelog on commits.
- **Automated Tests**: Created `tests/test_guards_and_compression.py` covering loader, guards, and DB memory replacements.

### Modified
- **Database Models**: Implemented transactional memory replacement in `models.py` (`replace_user_memory()`).
- **Commands Handler**: Updated `/profile` command in `commands.py` to output the consolidated 4-part user card.
- **Message Router**: Updated `messages.py` to run input guard validation and invoke chat manager orchestrator.
- **Persona Guidelines**: Hardened anti-abuse boundaries (disallowing code generation, structured outputs, essay writing), added length limits, and prompt injection filters in `persona.md`.
- **System Documentation**: Synchronized the new configurations and structures in `architecture.md`, `memory_engine.md`, `setup_guide.md`, `database.md`, `llm_integration.md`, `telegram_bot.md`, `project_plan.md`, and `README.md`.
