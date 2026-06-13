# DM Skip Bot Commands Bugfix Design

## Overview

In private (DM) chats, ThinkMate should reply to every conversational message but must never treat bot commands as conversation. Registered commands (`/start`, `/help`, `/profile`, `/reset`) are intercepted by the command router in `app/handlers/commands.py`. Any slash command that is *not* registered (e.g. `/foo`) is not matched by the command router and falls through to the catch-all text handler `handle_user_message` in `app/handlers/messages.py`, which is registered with `@router.message(F.text)`. The catch-all then replies via the LLM and enqueues the command text into the memory pipeline.

The fix excludes bot-command text from the catch-all handler so that any command not consumed by a dedicated command handler is ignored: no reply, no enqueue. The fix is minimal and targeted at the entry point of `handle_user_message`, leaving conversational handling, the length guard, and the empty-sender guard untouched.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — a message reaching the catch-all `F.text` handler whose text is a bot command (slash command) and which has a real sender.
- **Property (P)**: The desired behavior for buggy inputs — the catch-all handler ignores the message (sends no reply and does not enqueue it to the memory/LLM pipeline).
- **Preservation**: Existing behavior that must remain unchanged — conversational replies + enqueue for non-command text, registered command handling, the `MAX_INPUT_CHARS` length guard for non-command text, and ignoring messages with no sender.
- **handle_user_message**: The catch-all text handler in `app/handlers/messages.py`, registered with `@router.message(F.text)`. The original (`F`) version enqueues command-like text; the fixed (`F'`) version ignores it.
- **isBotCommand(text)**: Predicate that is true when the message text represents a Telegram bot command — text whose first message entity is of type `bot_command` at offset 0, or, as a fallback, text beginning with `/`.
- **user_task_manager.enqueue_message**: The function the catch-all calls to push a message into the batched LLM/memory pipeline. "Enqueued" means this was invoked.

## Bug Details

### Bug Condition

The bug manifests when a user sends a slash command in a DM that is not matched by any registered command handler. Because the command router does not consume it, the message falls through to the `@router.message(F.text)` catch-all, which has no check for command text and therefore treats the command as conversational input — replying via the LLM and enqueuing it to the memory pipeline.

**Formal Specification:**
```
FUNCTION isBugCondition(message)
  INPUT: message of type IncomingMessage in a DM that reached the F.text catch-all
  OUTPUT: boolean

  // A message reaching the catch-all text handler whose text is a bot command.
  // (Registered commands are intercepted earlier by the command router; the bug
  //  is the command-like text being treated as conversation by the F.text catch-all.)
  RETURN hasSender(message) AND isBotCommand(message.text)
END FUNCTION

FUNCTION isBotCommand(text)
  INPUT: text string
  OUTPUT: boolean

  // Telegram bot-command convention: a command entity at offset 0, e.g. "/foo" or "/foo@Bot".
  RETURN text is non-empty AND text starts with "/"
END FUNCTION
```

### Examples

- User sends `/foo` in a DM → expected: ignored (no reply, no enqueue); actual: bot replies via LLM and saves `/foo` to memory.
- User sends `/foo@ThinkMateBot` in a DM → expected: ignored; actual: treated as conversation.
- User sends `/help` (registered) → handled by `cmd_help` in `commands.py` (never reaches catch-all) — not the bug.
- User sends `hello, how are you?` → expected and actual: replied + enqueued — not the bug.
- Edge case: user sends `2/3 of the way there` → not a command (does not start with `/`) — must remain conversational.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Normal conversational (non-command) text must continue to reply via the LLM and be enqueued to the memory pipeline.
- Registered commands (`/start`, `/help`, `/profile`, `/reset`) must continue to be handled by their dedicated handlers in `commands.py`.
- The `MAX_INPUT_CHARS` length guard for non-command text must continue to reject over-long messages with the existing response.
- Messages with no real sender (`message.from_user` is `None`) must continue to be ignored.

**Scope:**
All inputs where `isBugCondition` returns false should be completely unaffected by this fix. This includes:
- Normal conversational text (does not start with `/`).
- Over-long non-command text (handled by the length guard).
- Messages with no sender.
- Registered commands (these never reach the catch-all; their routing is unchanged).

## Hypothesized Root Cause

Based on the bug analysis, the most likely cause is:

1. **Missing command guard in the catch-all handler**: `handle_user_message` is registered with `@router.message(F.text)`, which matches *any* text message. It checks only for an empty sender and for input length, with no check for command text. Unknown commands that the command router does not consume therefore fall through and are processed as conversation.

2. **`F.text` filter is too broad**: The `F.text` magic filter does not distinguish between conversational text and command text. Telegram marks commands with a `bot_command` message entity at offset 0; the current handler ignores entities entirely.

3. **Routing order does not help for unknown commands**: The command router only intercepts *registered* commands. There is no fallback handler for *unregistered* commands, so they reach the broad catch-all.

The primary cause is (1)/(2): the catch-all needs an explicit early-return when the text is a bot command.

## Correctness Properties

Property 1: Bug Condition - Bot commands are not treated as conversation

_For any_ DM message reaching the catch-all handler where the bug condition holds (`isBugCondition` returns true — a message with a real sender whose text is a bot command), the fixed `handle_user_message` SHALL ignore the message: it SHALL NOT send an LLM reply and SHALL NOT enqueue the text to the memory/LLM pipeline.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-command handling unchanged

_For any_ message where the bug condition does NOT hold (`isBugCondition` returns false — non-command conversational text, over-long non-command text, or a message with no sender), the fixed `handle_user_message` SHALL produce the same result as the original handler, preserving conversational reply + enqueue, the `MAX_INPUT_CHARS` length-guard response, and the empty-sender early return.

**Validates: Requirements 3.1, 3.3, 3.4**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct:

**File**: `app/handlers/messages.py`

**Function**: `handle_user_message`

**Specific Changes**:
1. **Add a command guard early-return**: After the empty-sender guard and before the length guard / enqueue, detect whether the message text is a bot command and return early if so (no reply, no enqueue).

2. **Use the most robust aiogram-idiomatic detection**: Prefer inspecting message entities — if `message.entities` has a first entity of type `bot_command` at `offset == 0`, treat it as a command. Fall back to `message.text.startswith("/")` to cover cases where entities are absent. This correctly handles `/foo` and `/foo@BotName` and avoids misclassifying text like `2/3` (which does not start with `/`).

3. **Keep ordering correct**: The empty-sender guard stays first (preserves Requirement 3.4). The command guard goes next so that command text is ignored before the length guard runs. The length guard and enqueue for non-command text remain unchanged (preserves 3.1, 3.3).

4. **No change to the command router**: Registered commands continue to be handled by `commands.py`; this fix only makes the catch-all ignore command-like text instead of conversing with it.

**Documentation correction**:

**File**: `docs/development/group_chat.md`

The "Behavior by chat type" table's Private (DM) row must clearly state that bot commands are excluded from conversational handling (not treated as conversation / not replied to), rather than implying every message is answered.

## Testing Strategy

### Validation Approach

Two-phase approach: first surface a counterexample that demonstrates the bug on the UNFIXED catch-all handler, then verify the fix ignores commands while preserving conversational, length-guard, and empty-sender behavior. Tests call `handle_user_message` directly with a mocked `Message` and patch `user_task_manager.enqueue_message`, asserting whether it was invoked and whether `message.answer` was called — no real LLM or DB is exercised (consistent with existing test conventions in `tests/`).

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix, and confirm the root-cause hypothesis (the broad `F.text` catch-all enqueues command text).

**Test Plan**: Construct mocked DM messages whose text is a bot command (with a real `from_user`), patch `user_task_manager.enqueue_message` (and `message.answer`), invoke `handle_user_message`, and assert the command was NOT enqueued and NOT answered. Run on UNFIXED code to observe the failure.

**Test Cases**:
1. **Unknown command enqueued**: `text = "/foo"` → assert `enqueue_message` not called (will FAIL on unfixed code — it IS called).
2. **Command with bot mention**: `text = "/foo@ThinkMateBot"` → assert not enqueued (will FAIL on unfixed code).
3. **Scoped property over generated commands**: iterate a generated set of command-like strings (e.g. `/` + random ascii word, with/without `@bot` suffix) and assert none are enqueued (will FAIL on unfixed code).

**Expected Counterexamples**:
- `handle_user_message` calls `user_task_manager.enqueue_message("/foo", ...)` instead of returning early.
- Root cause confirmed: the catch-all has no command guard.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed handler ignores the message.

**Pseudocode:**
```
FOR ALL message WHERE isBugCondition(message) DO
  result := handle_user_message_fixed(message)
  ASSERT (enqueue_message NOT called) AND (message.answer NOT called)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed handler behaves identically to the original.

**Pseudocode:**
```
FOR ALL message WHERE NOT isBugCondition(message) DO
  ASSERT handle_user_message_original(message) = handle_user_message_fixed(message)
END FOR
```

**Testing Approach**: Property-style testing (looping/parametrizing over generated non-command inputs) is recommended for preservation because the property is universal ("for all non-command inputs"). Note: Hypothesis is not currently a project dependency, so the property is expressed as a loop/parametrize over a generated input set, consistent with the existing test style (`unittest.mock` + `pytest`). Hypothesis MAY be added later if stronger generation is desired.

**Test Plan**: Observe behavior on UNFIXED code for non-command inputs first, then write tests asserting that behavior is preserved.

**Test Cases**:
1. **Conversational text preserved**: for generated non-command strings (do not start with `/`), assert `enqueue_message` IS called with the text — passes on unfixed code, must still pass after fix.
2. **Length-guard preserved**: non-command text longer than `MAX_INPUT_CHARS` → `message.answer` called with the length-guard text and NOT enqueued.
3. **Empty-sender preserved**: `message.from_user = None` → returns early, neither answers nor enqueues.

### Unit Tests

- Unknown command (`/foo`) is ignored by the catch-all (no enqueue, no answer).
- Command with `@bot` suffix is ignored.
- Conversational text is enqueued.
- Over-long non-command text triggers the length guard.
- Message with no sender is ignored.

### Property-Based Tests

- Scoped property: for a generated set of command-like strings, the fixed catch-all never enqueues and never answers (Property 1).
- Preservation property: for a generated set of non-command strings (within length limits), the fixed catch-all always enqueues exactly as the original did (Property 2).

### Integration Tests

- Optional: route a mocked unknown-command `Update`/`Message` through the configured routers and assert the command handlers and catch-all together neither reply nor enqueue, while a registered command still reaches its handler and conversational text still reaches the catch-all.
