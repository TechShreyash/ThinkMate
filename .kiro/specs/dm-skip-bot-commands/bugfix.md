# Bugfix Requirements Document

## Introduction

In private (DM) chats, ThinkMate is intended to reply to every conversational message but must NOT treat bot commands (slash commands) as conversation. Registered commands such as `/start`, `/help`, `/profile`, and `/reset` are handled by their dedicated command handlers in `app/handlers/commands.py`. However, unregistered or unknown slash commands (e.g. `/foo`) are not matched by the command router and fall through to the catch-all text handler in `app/handlers/messages.py`, which is registered with `@router.message(F.text)`. As a result the bot replies to these unknown commands, sends them to the LLM, and saves them into the memory pipeline as if they were normal conversational input.

This bug causes unintended LLM replies, wasted API calls, and pollution of the user's memory with command-like text. The documentation in `docs/development/group_chat.md` currently states the DM behavior as "Reply to every message (unchanged)." without noting that bot commands are excluded; this should be corrected to reflect that commands are not treated as conversation.

## Bug Analysis

### Current Behavior (Defect)

When a user sends a slash command in a DM that is not matched by any registered command handler, the message falls through to the catch-all `F.text` handler and is processed as normal conversational input.

1.1 WHEN a user sends an unregistered/unknown slash command (e.g. `/foo`) in a DM THEN the system replies to it via the LLM as if it were conversational text
1.2 WHEN a user sends an unregistered/unknown slash command in a DM THEN the system enqueues the command text to the memory pipeline, saving command-like text into the user's memory
1.3 WHEN a message whose text begins with a bot command does not match a registered command handler THEN the catch-all `F.text` handler processes it instead of ignoring it

### Expected Behavior (Correct)

Bot commands must never be treated as conversation. Registered commands are handled by their command handlers; any command not handled by a command handler is ignored.

2.1 WHEN a user sends an unregistered/unknown slash command (e.g. `/foo`) in a DM THEN the system SHALL NOT reply to it via the LLM
2.2 WHEN a user sends an unregistered/unknown slash command in a DM THEN the system SHALL NOT enqueue the command text to the LLM or memory pipeline
2.3 WHEN a message text is a bot command that is not handled by a registered command handler THEN the catch-all `F.text` handler SHALL ignore it (no reply, no processing)

### Unchanged Behavior (Regression Prevention)

Normal conversational handling and registered command handling must continue to work exactly as before.

3.1 WHEN a user sends a normal conversational message (non-command text) in a DM THEN the system SHALL CONTINUE TO reply via the LLM and process it through the memory pipeline
3.2 WHEN a user sends a registered command (`/start`, `/help`, `/profile`, `/reset`) THEN the system SHALL CONTINUE TO handle it via its dedicated command handler
3.3 WHEN a user sends a message exceeding `MAX_INPUT_CHARS` of non-command text in a DM THEN the system SHALL CONTINUE TO reject it with the existing length-guard response
3.4 WHEN a message has no real sender (service/channel post) THEN the system SHALL CONTINUE TO be ignored

## Bug Condition and Properties

### Bug Condition Function

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type IncomingMessage in a DM
  OUTPUT: boolean

  // A message reaching the catch-all text handler whose text is a bot command.
  // (Registered commands are intercepted earlier; the bug is the command-like
  //  text being treated as conversation by the F.text catch-all.)
  RETURN hasSender(X) AND isBotCommand(X.text)
END FUNCTION
```

Where `isBotCommand(text)` is true when the text represents a slash command (e.g. begins with `/` following Telegram's bot-command convention).

### Property: Fix Checking

```pascal
// Property: Fix Checking - Commands are not treated as conversation
FOR ALL X WHERE isBugCondition(X) DO
  result ← handle_user_message'(X)
  ASSERT (no LLM reply sent) AND (not enqueued to memory pipeline)
END FOR
```

### Property: Preservation Checking

```pascal
// Property: Preservation Checking - Conversational handling unchanged
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT handle_user_message(X) = handle_user_message'(X)
END FOR
```

Where `handle_user_message` is the original (unfixed) catch-all handler and `handle_user_message'` is the fixed handler.
