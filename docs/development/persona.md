# Persona customization

[Documentation index](../README.md) · [Configuration](configuration.md) · [LLM integration](llm_integration.md)

[persona.md](../../persona.md) is a runtime system-prompt fragment. It describes the companion's voice, identity, communication style, memory use, and conversational boundaries. Editing it changes prompts sent to the model; it is not merely editing project documentation.

## Loading and reload behavior

[chat_manager.py](../../app/services/chat_manager.py) reads the configured `PERSONA_FILE` as nonblank UTF-8 text, accepting an optional UTF-8 BOM. It caches valid content by absolute path, nanosecond modification time, and file size, and reloads when any of those change. A relative path is resolved against the process working directory; supplied workflows run from the repository root or `/app` in Docker.

Startup checks that the file can actually be read and decoded. Missing paths, directories, invalid UTF-8, and blank files trigger a warning once per path until a successful read. An unavailable persona uses a short default. If a previously valid file becomes unreadable or invalid, replies keep that same file's last good content; switching to a different invalid path uses the default rather than a persona from the previous path.

Saving an edit normally affects the next prompt load. It does not replace a prompt already submitted to the LLM. An edit that preserves both modification time and file size can escape cache detection until those change or the process restarts.

## Rules supplied by code

[system_prompt.py](../../app/prompts/system_prompt.py) adds application behavior around the editable persona: automatic language/script mirroring, explicit language/translation overrides, useful response depth, female self-reference, truthful identity and capability limits, and cautious memory use. It appends temporal context and group attribution instructions when applicable.

Memory text, display names, and group conversation attribution are serialized as labeled JSON data so embedded newlines remain within their original fields. The instructions treat that context as data, prioritize current corrections over stale memory, and prohibit disclosing private-chat details in public group replies. These are model instructions and formatting precautions, not a complete security or privacy boundary: the group caller still sends the current speaker's global personal memory to the LLM. See the [group guide](group_chat.md) for the data flow.

Editing `persona.md` alone does not remove application rules or grant browsing, media perception, code execution, reminders, or external actions. ThinkMate can analyze pasted text while remaining honest about media it cannot perceive. It keeps a female companion voice and answers truthfully about being an AI when asked.

`BOT_NAME` changes displayed names, onboarding/report text, group-name detection, and history attribution. It does not rewrite names already written in the persona. If you rebrand the companion, update both the environment setting and persona text and restart for the environment change.

[Check-in instructions](../../app/prompts/checkin_prompt.py) request a brief, low-pressure opener grounded in one appropriate remembered detail, or the exact `NOTHING` sentinel. They distinguish plans from completed events and avoid relative-day wording when the user's local timing is uncertain. They prefer a recorded language/script preference and otherwise use English; the check-in caller does not provide recent user messages for language mirroring.

## Make a focused edit

1. Read the persona and code-supplied response rules together.
2. Change one aspect, such as warmth, formality, or how memory is referenced.
3. Check that it does not conflict with actual capabilities, output requirements, or privacy instructions.
4. Verify representative short/long requests, explicit translations, serious topics, stale memory, and group attribution on a test bot.

[Persona tests](../../tests/test_persona_config.py) cover validation, caching/reload, file changes, and fallback isolation. [Prompt-language tests](../../tests/test_prompt_language.py) cover selected instructions. [Context tests](../../tests/test_system_prompt_context.py) check hostile-content serialization, DM/group context separation, and missing-mood rendering. [Check-in tests](../../tests/test_checkin_prompt.py) cover prompt requirements and request assembly.

These mocked and deterministic tests do not prove that a particular model follows the persona in every conversation. Prompt construction follows [OpenAI's prompting guidance](https://developers.openai.com/api/docs/guides/prompt-engineering); evaluate representative conversations against the configured endpoint when assessing response quality.

## Container deployments

The default image copies the persona at build time. For edits to appear in a running container, rebuild/recreate the container or mount the file at the configured path. See [operations](../operations.md#persona-edits-in-a-container).

Keep private notes and credentials out of the persona: its content is sent to the configured LLM endpoint on reply/check-in requests.
