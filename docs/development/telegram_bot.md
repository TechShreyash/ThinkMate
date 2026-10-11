# Telegram integration

ThinkMate uses aiogram 3 to receive commands, text messages, inline-button callbacks,
and bot membership updates through long polling. Start with the
[documentation index](../README.md); for response decisions in groups, see
[group routing](group_chat.md).

## Code map

| Responsibility | Source |
|---|---|
| Startup, background jobs, polling, shutdown | [`main.py`](../../main.py) |
| Router order: commands, messages, membership | [`app/handlers/__init__.py`](../../app/handlers/__init__.py) |
| Throttling and database injection | [`middlewares.py`](../../app/handlers/middlewares.py) |
| Commands, help, inline guide, command-menu publishing | [`commands.py`](../../app/handlers/commands.py) |
| Text guards, chat routing, media explanation | [`messages.py`](../../app/handlers/messages.py) |
| Introduction when the bot joins a group | [`membership.py`](../../app/handlers/membership.py) |
| Batching, conversation locks, typing, reply delivery | [`user_task_manager.py`](../../app/services/user_task_manager.py) |
| Memory-aware reply generation | [`chat_manager.py`](../../app/services/chat_manager.py) |
| Response caps, splitting, reply fallback | [`telegram_text.py`](../../app/services/telegram_text.py) |

## Startup and middleware

[`main()`](../../main.py) verifies MongoDB, creates indexes, and restores persisted metrics.
It creates the bot, installs log-channel forwarding and the warning/error sink, validates
the persona, and starts configured metrics, consolidation, and proactive jobs.
The dispatcher then installs middleware, includes
routers, publishes command menus when enabled, and polls for the update types returned
by `dp.resolve_used_update_types()`.

Before polling, startup explicitly calls `delete_webhook(drop_pending_updates=True)`
to remove a previous webhook and discard Telegram's pending backlog. Polling keeps
the bot's HTTP session open for cleanup.

Three outer middleware run on the update pipeline in this order:

1. `UpdateTaskMiddleware` tracks active update handlers, rejects new updates during
   shutdown, and drains or cancels active commands/handlers before clients close.
2. `ThrottlingMiddleware` drops bot-authored messages, sufficiently old pre-startup
   messages, and messages exceeding the per-user sliding-window limit. It uses
   Telegram send timestamps for rate-limit calculations and keeps one warning per
   throttling episode in DMs; groups drop over-limit messages silently. Callback
   queries and membership updates pass through this message-only limiter.
3. `DbSessionMiddleware` injects the shared Motor database handle as `db`. This is a
   pooled database handle, not a new client or transaction for each update.

The command router additionally uses `ProactiveResetMiddleware` to clear the caller's
unanswered check-in streak before a matched command runs. It does not change the
`proactive_enabled` preference. Typing indicators belong to `UserTaskManager`, not a
middleware. See [configuration](configuration.md) for limits and startup switches.

On shutdown or startup failure, the entry point cancels registered schedulers and drains
active update handlers for up to 10 seconds, owned conversation work for another
10 seconds, then LLM audits for up to 5 seconds. It
flushes metrics, drains the error sink for up to 5 seconds, and uploads buffered logs
before closing Telegram and MongoDB; the shared LLM client closes after its audit drain.
Asynchronous cleanup steps have separate 15-second outer timeouts. See
[shutdown and restarts](../operations.md#shutdown-and-restarts).

## Commands and their actual scope

These are the default triggers. Environment overrides can rename or disable them.
All command handlers are registered on the same message router; a Telegram menu scope
controls discoverability, not authorization or where a handler can run.

| Command | Behavior | Scope and authorization |
|---|---|---|
| `/start` | Ensure the caller's profile; show welcome and guide buttons. New users also see Quick start. | DMs and groups. |
| `/help` | Show enabled commands using their configured names; hide ops commands from unauthorized callers. | DMs and groups. |
| `/onboard` | Send static starter questions and set `onboarded=True`; no LLM call. | DMs and groups; onboarding is optional. |
| `/profile` | Show the caller's compiled memory block, or a no-memory message. | DMs and groups; output is visible in the invoking chat. |
| `/reset confirm` | Best-effort export, then delete the caller's profile and DM buffer. A bare `/reset` asks for confirmation. | DMs and groups; affects the caller's user ID. |
| `/checkins [on\|off]` | Report or set the caller's proactive-message preference. | DMs and groups; preference is global per user. |
| `/reactions [on\|off]` | Report or set reactions on the caller's messages. | DMs and groups; preference follows the user across chats. |
| `/quiet` | Set the caller's ambient mode to `quiet` in this chat. | Groups; in a DM, explain that the control is for groups. |
| `/chatty` | Set the caller's ambient mode to `chatty` in this chat. | Groups; in a DM, explain that the control is for groups. |
| `/groupbot [on\|off]` | Report or change whether conversational text and media explanations are processed in this group. | Group-only; anyone can read status, authorized admins can change it. |
| `/groupmode [quiet\|chatty\|normal]` | Report or set a group-wide ambient override. `normal` stores `auto`. | Group-only; anyone can read status, authorized admins can change it. |
| `/health` | Show liveness, MongoDB readiness, and a metrics summary. | Ops authorization below. Unauthorized calls are silent. |
| `/metrics` | Show counters, gauges, timers, and LLM calls by task. | Ops authorization below. Unauthorized calls are silent. |

Toggle arguments also accept `enable`, `enabled`, or `yes` for on, and `disable`,
`disabled`, or `no` for off. Missing or unrecognized arguments report status.
`/groupmode auto`, `reset`, and `clear` are aliases for `normal`.

**Group administration:** `/groupbot` and `/groupmode` mutations allow either an ID in
`ADMIN_USER_IDS` or a Telegram group member with status `administrator` or `creator`.
A failed membership lookup denies the mutation.

**Ops authorization:** when `ADMIN_USER_IDS` is non-empty, only listed IDs can run
`/health` and `/metrics`, including in groups. When it is empty, any caller in a
private chat is allowed; group calls are denied. Set this list for operator-only
reports. See [observability](observability.md).

**Memory visibility and reset limits:** `/profile` is not restricted to DMs. Personal
memory also enters group reply prompts for the triggering sender. `/reset` deletes
only `user_profiles[user_id]` and `chat_buffers[user_id]`; it does not remove group
history, group profiles, affinity, settings, or archived logs. The export contains
that profile and DM buffer and is sent to `LOGS_CHANNEL_ID` when forwarding is
configured. Backup delivery is best-effort; the success message mentions restoration
only after the backup upload succeeds. The confirmation explains that shared group
history and archived logs remain. There is no restore command. See
[database](database.md) and the [roadmap](../roadmap.md).

Confirmed reset stops prior owned DM work and holds new DM messages through backup
and deletion; a 10-second cancellation timeout prevents deletion. Shared group work
and text remain. Extraction results are accepted only while the owner's captured
profile-generation token still matches; deletion/recreation invalidates an older
token. Group owner tokens are captured after reading the group segment, and retained
history can be extracted again.

## Renaming, disabling, and Telegram menus

[`resolve_command_config()`](../../app/config.py) builds `config.COMMANDS` from
`CMD_<KEY>_NAME` and `CMD_<KEY>_ENABLED` at configuration creation. For example:

```dotenv
CMD_RESET_NAME=forget
CMD_METRICS_ENABLED=false
TELEGRAM_PUBLISH_COMMANDS=true
```

`commands.py` binds enabled handlers at import time. Restart after changing these
values. The loader strips a leading slash, normalizes to lowercase, and validates
1-32 ASCII lowercase letters, digits, or underscores. Invalid names fall back to the
built-in key. Duplicates among enabled triggers fall back to the keys for the colliding
commands; collision checks repeat until fallback names are unique.

Help lists and menus use resolved names and omit disabled commands. Some guide text,
buttons, and command descriptions still refer to disabled commands; disabling a slash
handler does not unregister `gd:` callbacks or the Quick start onboarding action.
Unknown, disabled, or old renamed slash triggers are ignored by the conversation router
rather than passed to the LLM.

`setup_bot_commands()` publishes these enabled commands before polling:

- `BotCommandScopeDefault`: start, help, onboard, checkins, profile, reset, reactions.
- `BotCommandScopeAllGroupChats`: start, help, quiet, chatty, groupbot, groupmode.

Health and metrics are omitted from public menus. Publication is best-effort.
`TELEGRAM_PUBLISH_COMMANDS=false` skips publication; it does not remove an already
published menu or disable local handlers.

## Text routing, batching, and delivery

[`messages.py`](../../app/handlers/messages.py) ignores text without a sender,
channel-authored or anonymous `sender_chat` messages, automatic forwards, forwarded
content, and slash commands not handled by the command router. Channel conversations
are ignored.

Private text within `MAX_INPUT_CHARS` is queued for a reply. Overlong private text gets
a short explanation and is not buffered. Groups silently drop overlong text and use
the enabled flag, explicit-address checks, implicit recency checks, and ambient gate
in [group routing](group_chat.md).

`UserTaskManager` queues by `chat_id`. It coalesces messages after
`MESSAGE_BATCH_DELAY_SECS`, limits postponement with `MAX_BATCH_DELAY_SECS`, caps pending
entries with `MAX_QUEUED_MESSAGES`, and serializes processing with the conversation's
`chat_lock`. The timer expires at the idle delay or maximum batch age, whichever comes
first; this does not bound lock waits, generation, or delivery. Within a batch it combines
only consecutive messages from the same sender and generates separately for each run,
preserving an A → B → A sequence. A direct-reply reason takes precedence over an ambient
reason within a sender's run. A new coalescing timer does not cancel ongoing reply work.

The typing loop spans batching and generation. `handle_message()` appends the combined
user turn, builds memory and context, makes one reply-bundle LLM call, and stores the
assistant turn. Extraction and compression may launch as separate background work;
"one call" describes reply plus reaction, not total memory work.

For delivery:

- Groups reply under the sender's last batched message. If Telegram rejects that
  reply, the text helper retries as a plain answer. DMs use a plain answer.
- LLM responses use `MAX_RESPONSE_CHARS` before splitting into safe chunks. Oversized
  formatted command output is converted to plain text before splitting.
- Empty ambient output sends neither text nor reaction. Empty direct/DM output gets
  a short fallback instead of sending an invalid empty Telegram message.
- Reaction failure does not block the text reply. Blocked or restricted delivery is
  logged; private blocked-user handling also disables proactive messages.

## Emoji reactions

The reply bundle may include a reaction from the same generation call. The model's
choice is normalized through the local whitelist in
[`reactions.py`](../../app/services/reactions.py). `ENABLE_MESSAGE_REACTIONS=false`
disables them globally. Otherwise the sender's `reactions_enabled` profile preference
is checked using the profile already loaded for generation; a missing preference
defaults to enabled.

The worker attempts one `ReactionTypeEmoji` on the sender's last batched message.
Telegram may reject a reaction because of chat settings, message restrictions, or
permissions; the attempt is caught and text delivery continues. The local whitelist
does not guarantee a particular chat accepts the emoji. `/reactions off` controls
message reactions, not emojis inside reply text.

## Guide, membership, and non-text limits

The `/start` and `/onboard` keyboards use `gd:` callback data. `on_guide_nav()` edits
one message in place, acknowledges the callback, and falls back to the home screen for
unknown topics. Topics are memory, privacy, groups, check-ins, and commands. Quick start
ensures the clicking user's profile and marks them onboarded. Guide edits are
best-effort; callbacks do not call the LLM.

The membership router listens to `my_chat_member` join transitions for groups and
supergroups. It posts an introduction when `GROUP_INTRO_ON_JOIN=true` and logs delivery
failures. It can introduce the bot again when it rejoins; there is no persisted
"introduced once" flag.

Photos, documents, video, voice, audio, stickers, animation, and video notes are not
analyzed or downloaded. A DM receives a text-only explanation. In a group this notice
is sent only for media that directly replies to a bot message; a caption mentioning the
bot is not sufficient. Channels receive no notice. Other update types, such as
location and polls, have no dedicated conversational handler.

The group enabled flag is checked for conversational text and media explanations.
Commands and membership introductions can still respond while
`/groupbot off` is set; see [group controls](group_chat.md#group-controls).

## Verification references

Behavior is covered by [command publication tests](../../tests/test_publish_commands.py),
[engagement command tests](../../tests/test_engagement_commands.py),
[command preservation tests](../../tests/test_command_preservation.py),
[reaction tests](../../tests/test_reactions.py),
[text-delivery tests](../../tests/test_telegram_text.py), and
[membership tests](../../tests/test_membership_intro.py).
See the [testing guide](testing_guide.md) for runnable commands and dependencies.
