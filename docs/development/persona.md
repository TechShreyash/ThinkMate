# Persona customization

[Documentation index](../README.md) · [Configuration](configuration.md) · [LLM integration](llm_integration.md)

[persona.md](../../persona.md) is a runtime system-prompt fragment. It describes the companion's voice, identity, communication style, memory use, and conversational boundaries. Editing it changes prompts sent to the model; it is not merely editing project documentation.

## Loading and reload behavior

[chat_manager.py](../../app/services/chat_manager.py) reads the configured `PERSONA_FILE`, caches its contents with path/modification time, and reloads when either changes. A relative path is resolved against the process working directory; the supplied workflows run from the repository root or `/app` in Docker.

Startup checks that the file can be stat'ed and warns if it cannot. A missing path uses a short default persona. An open/read error after a successful stat returns cached content when available. This is fallback behavior, not a strict validation of the persona's content; an empty readable file is accepted.

Saving an edit normally affects the next prompt load. It does not replace a prompt already submitted to the LLM. If a tool preserves modification time, the cache may not detect the edit until the time changes or the process restarts.

## Rules supplied by code

[system_prompt.py](../../app/prompts/system_prompt.py) adds response behavior after the editable persona, including language/script mirroring, style/length guidance, text-only capability limits, and female self-reference. It also appends memory, temporal context, and group speaker/context instructions as appropriate.

Editing `persona.md` alone therefore does not remove these rules. They are instructions to the model, not guarantees enforced by output validation. Similarly, this persona cannot grant browsing, media perception, file access, or external actions that the application does not implement.

`BOT_NAME` changes displayed names, onboarding/report text, group-name detection, and history attribution. It does not rewrite names already written in the persona. If you rebrand the companion, update both the environment setting and persona text and restart for the environment change.

## Make a focused edit

1. Read the persona and code-supplied response rules together.
2. Change one aspect, such as warmth, formality, or how memory is referenced.
3. Check that it does not conflict with the capabilities or boundaries the application actually provides.
4. Verify with representative short/long messages, languages/scripts, serious topics, and group conversations on a test bot.

The [persona test](../../tests/test_persona_config.py) covers a missing-path warning emitted once. It does not exercise content/mtime reload. [Prompt-language tests](../../tests/test_prompt_language.py) cover selected prompt instructions. Those tests do not prove that a particular model follows the persona in every conversation.

## Container deployments

The default image copies the persona at build time. For edits to appear in a running container, rebuild/recreate the container or mount the file at the configured path. See [operations](../operations.md#persona-edits-in-a-container).

Keep private notes and credentials out of the persona: its content is sent to the configured LLM endpoint on reply/check-in requests.
