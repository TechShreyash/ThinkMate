# Group routing and memory

ThinkMate supports Telegram groups and supergroups with shared conversation history,
shared group memory, and personal memory for each participant. This guide explains the
current reply decision, controls, and storage behavior. See the
[documentation index](../README.md), [Telegram integration](telegram_bot.md), and
[configuration reference](configuration.md).

## Code map

| Responsibility | Source |
|---|---|
| Input guards, group enabled check, routing, traces | [`messages.py`](../../app/handlers/messages.py) |
| Addressing, spam detectors, implicit and ambient gates | [`group_gate.py`](../../app/services/group_gate.py) |
| Per-member affinity and mode cache | [`affinity.py`](../../app/services/affinity.py) |
| Personal and group command controls | [`commands.py`](../../app/handlers/commands.py) |
| Per-chat batching, reply delivery, recency tracking, pruning | [`user_task_manager.py`](../../app/services/user_task_manager.py) |
| Shared and personal memory in reply prompts | [`chat_manager.py`](../../app/services/chat_manager.py) |
| Group and participant extraction | [`memory_extractor.py`](../../app/services/memory_extractor.py) |
| Buffer, profile, member, and settings persistence | [`models.py`](../../app/database/models.py) |

## Which messages enter group routing?

The text router accepts human-authored text with a real sender. Middleware drops other
bots and rate-limited messages. The router ignores channel conversations, anonymous or
channel-authored `sender_chat` text, automatic forwards, forwarded text, and slash
commands handled elsewhere. Group text longer than `MAX_INPUT_CHARS` is silently dropped.

Next, `_handle_group_message()` reads the group's enabled flag. Disabled groups do not
continue to identity refresh, buffering, routing, or generation for newly arriving
conversational text. A settings-read failure treats the group as enabled.

For admitted group text, the router refreshes the sender's identity best-effort, resolves
the bot's identity, records spam observations, and applies these decisions in order:

1. **Explicit address:** a Telegram reply to the bot, a bot username mention, or the
   bot name as a standalone match. Spam changes this rule as described below.
2. **Implicit follow-up:** untagged text within the bot's recency window, not directed
   at another person or classified as spam, and outside the implicit cooldown.
3. **Ambient candidate:** remaining messages pass through cooldown, trigger or scan
   tick, and an affinity-weighted random decision.
4. **Context only:** messages that do not pass a reply gate are buffered without an
   LLM reply.

Private chats use the direct conversation path and do not run group spam, recency,
ambient, or affinity gates. See [Telegram integration](telegram_bot.md) for non-text
media notices and command handling.

All group behavior depends on messages Telegram actually delivers to the bot. Privacy
mode and membership permissions can limit that input; unseen group messages cannot
contribute to history, recency counters, or memory.

## Group controls

| Control | Who can change it | Effect |
|---|---|---|
| `/quiet` | Any member, for themselves in this group | Personal ambient probability becomes zero. |
| `/chatty` | Any member, for themselves in this group | Personal ambient probability gets a 1.5 multiplier. |
| `/groupmode quiet` | Telegram group administrator/creator or a listed global operator | Set ambient probability to zero for everyone. |
| `/groupmode chatty` | Same authorization | Use a 1.5 ambient multiplier for everyone, overriding personal quiet mode. |
| `/groupmode normal` | Same authorization | Store `auto`, so personal modes apply again. |
| `/groupbot off` | Same authorization | Stop admitting new conversational group text. |
| `/groupbot on` | Same authorization | Admit conversational group text again. |

A bare `/groupbot` or `/groupmode` reports status and is available to anyone. Global
operators are IDs in `ADMIN_USER_IDS`; otherwise the command verifies the caller with
`bot.get_chat_member()`. Lookup failure denies a change. Outside groups, group-wide
controls explain their scope; `/quiet` and `/chatty` are no-ops in DMs. Triggers may be
renamed or disabled, as described in [Telegram integration](telegram_bot.md).

**Ambient mode precedence:** group `quiet` or `chatty` overrides each member's mode.
Group `auto` uses the speaker's personal mode. These modes affect only ambient
participation; explicit and implicit replies still qualify. There is no personal
command to restore `auto` after `/quiet` or `/chatty`.

**Off-switch scope:** `/groupbot off` is checked in the text router, not by every
handler or worker. Commands remain usable, media that replies to the bot may still get
the text-only notice, and a membership join can still produce an introduction. Already
queued replies and background memory work are not canceled or rechecked when the flag
changes. The switch does not delete existing memory or history.

## Explicit addressing

`_get_bot_identity()` caches a successful `bot.get_me()` result for the process. The
addressing name uses nonblank `BOT_NAME`, otherwise the Telegram first name; username
and name checks are case-insensitive. A failed lookup returns a temporary empty
identity so later messages can retry.

A reply to one of the bot's own messages is the strongest explicit signal. Otherwise
`is_addressed()` checks the username mention and configured/fallback name. The name
match can occur anywhere in the text; it does not require that the sentence begins
with the name.

Explicitly addressed text is enqueued with `reason="reply"` and attempts a `+0.05`
affinity bump for its sender. It bypasses ambient probability and implicit cooldown,
but remains subject to input limits, group enablement, queue capacity, and delivery
errors. It is therefore a routing policy, not a guarantee that every mention produces
a delivered reply.

## Implicit follow-ups

`ImplicitAddressGate` holds in-memory state per chat. Untagged text qualifies when:

- the worker has recorded a successful conversational bot send in this group;
- elapsed time since that send is at most `GROUP_IMPLICIT_RECENCY_SECS`;
- the number of preceding human text messages since that send is at most
  `GROUP_IMPLICIT_RECENCY_MAX_MSGS`;
- it neither replies to another participant nor contains a Telegram mention entity
  targeting conversation elsewhere;
- it is not classified as spam.

Both recency bounds must hold. The current message is counted after the decision, so
an immediate follow-up has zero intervening messages. This is a recency heuristic;
it does not semantically determine whether an untagged message answers the bot.

A qualifying message is enqueued with `reason="reply"` only when the per-chat
`GROUP_IMPLICIT_COOLDOWN_SECS` has elapsed. The router commits that cooldown before
enqueueing, so a generation failure or queue drop still consumes the interval. Other
qualifying follow-ups during cooldown fall through to the ambient gate. Implicit
routing does not get the explicit `+0.05` affinity bump.

After successful conversational group text delivery, `_process_batch()` calls
`note_bot_spoke()`, resetting the recency time and human counter. Empty ambient output
does not reset recency. Command responses, membership introductions, and media notices
are not registered as bot activity by this mechanism.

## Spam detection and its limits

Two cheap detectors run before group address decisions:

- **Mass tags:** `count_distinct_mentions()` counts distinct handles from `mention`
  entities and user IDs from `text_mention` entities. A message is spam when that
  count is strictly greater than `GROUP_MASS_TAG_SPAM_THRESHOLD`; the bot's own
  mention also counts. Missing entities can leave mass tags undetected.
- **Repeated bursts:** `SpamBurstDetector` strips mention slices and remaining
  `@tokens`, normalizes case/whitespace, and compares against recent text from the
  same `(chat_id, user_id)`. Similarity uses `difflib.SequenceMatcher`; the current
  message is spam when enough near-identical messages fall within the configured
  window. History is capped per sender. Despite its greeting-burst name, the
  implementation does not require a greeting, mention, or low-content message;
  other repeated text can match too.

The combined spam flag blocks mention/name promotion to explicit addressing and
blocks implicit addressing. A direct Telegram reply to the bot remains explicit even
if classified as spam. Detector failures degrade to not-spam.

Spam also forces the ambient keyword trigger to false. **It does not veto a periodic
context-scan tick**, so a spam message can still pass the ambient gate on that tick
when its probability allows it. Spam text that is retained remains available as
conversation context. This feature is a reply-routing heuristic, not group moderation:
it does not delete messages or ban senders, and sender-local burst tracking does not
catch coordinated repetition from many accounts.

## Ambient participation and affinity

`AmbientGate.decide()` performs no DB or LLM call. The caller first loads the speaker's
affinity and effective mode, then the gate applies:

1. **Cooldown:** stop if less than `GROUP_AMBIENT_COOLDOWN_SECS` has elapsed since the
   last ambient dispatch.
2. **Trigger or scan tick:** advance the counter for candidates that survive cooldown.
   Continue on a cheap trigger or every `GROUP_CONTEXT_SCAN_EVERY` eligible candidates.
   A nonpositive scan interval disables periodic ticks. Cheap patterns cover English
   greetings, birthdays, congratulations, laughter, question openers, and sentiment,
   plus laughter emojis.
3. **Probability:** continue when a random draw is below
   `clamp(GROUP_AMBIENT_BASE_RATE * affinity * mode_factor, 0, 1)`.
   Factors are `auto=1`, `quiet=0`, and `chatty=1.5`.

With loader defaults (`base_rate=0.25`, `affinity=0.5`), an otherwise eligible candidate
has a 12.5% chance in auto mode and 18.75% in chatty mode. Cooldown and trigger checks
still apply.

Before enqueueing a passing candidate, the router calls `mark_chimed()`. Failed,
queued-but-dropped, or empty generation therefore still holds the ambient cooldown.
`reason="ambient"` uses the same group reply generation path; the worker uses the
reason for empty-output suppression and sender-batch precedence. It does not select a
separate ambient system prompt. Empty ambient output sends no text or reaction.

Affinity is stored per `(chat_id, user_id)` and clamped to `[0, 1]`. Its cache reads
through and writes through to MongoDB. Signals are the explicit routing bump, an
optional `affinity_delta` from the existing reply call, and a `-0.1` keyword bump for
`stop`, `quiet`, `spam`, `annoying`, or `shut up`. The negative scan runs during
generation, not on every passive message. No dedicated sentiment LLM call is made.

Gate decisions and caches are process-local; restarts reset cooldown/recency/burst
history. The task manager's lazy idle sweeper prunes those maps and cached members.
Persisted affinity, modes, profiles, and buffers survive restarts.

## Buffering, batching, and memory

| Data | Persistence |
|---|---|
| Conversation history | `chat_buffers[chat_id]`, including `sender_id`, `sender_name`, role, content, timestamp. |
| Shared group memory | `user_profiles[chat_id]`, using the same profile representation as personal memory. |
| Personal participant memory | `user_profiles[user_id]`, shared across that person's DMs and groups. |
| Member affinity and personal mode | `chat_members["{chat_id}:{user_id}"]`. |
| Enabled flag and ambient override | `group_settings[chat_id]`; defaults are enabled and `auto`. |

The buffer has one writer for each routing path:

- Explicit, implicit, and passing ambient candidates are enqueued without a handler
  buffer write. `handle_message()` appends their combined sender turn.
- Ambient drops, including defended affinity/gate-evaluation failures, are appended
  by `_maybe_ambient_chime()` and do not enter generation.

This prevents duplicate writes on passing ambient messages. It is not an exact
one-record-per-Telegram-message guarantee: batching combines each sender's queued text,
input/throttle/off/queue guards can drop messages, and the buffer hard cap trims older
entries. A multi-sender batch processes sender groups in first-seen sender order,
which can differ from the original interleaving.

Group reply history is rendered with speaker names. Prompts include both shared group
memory and the triggering sender's personal memory; profiles are separate documents,
but personal memory is available to group generation. Other participants' full
personal profiles are not loaded for that reply.

When generation observes the extraction threshold, it starts background extraction
with `is_group=True`. Passive buffering alone does not schedule extraction. The group
extractor makes one structured call over a segment, saves shared memory to the group
profile, and saves participant updates after mapping names back to IDs from the
segment. Unknown participant names are skipped. Duplicate display names are ambiguous:
the current map keeps the first sender ID for a normalized name.

Prompt rules ask extraction to normalize memory into English and ask replies to follow
the recent conversation's language and script. These are model instructions, not a
validator-enforced storage or response guarantee. See [memory engine](memory_engine.md),
[database](database.md), and [LLM integration](llm_integration.md).

## Tuning and verification

The authoritative defaults live in [`Config`](../../app/config.py); the
[configuration reference](configuration.md) covers:

- Ambient and affinity: `GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`,
  `GROUP_CONTEXT_SCAN_EVERY`, `AFFINITY_DEFAULT`.
- Implicit replies: `GROUP_IMPLICIT_RECENCY_SECS`, `GROUP_IMPLICIT_RECENCY_MAX_MSGS`,
  `GROUP_IMPLICIT_COOLDOWN_SECS`.
- Spam: `GROUP_MASS_TAG_SPAM_THRESHOLD`, `GROUP_SPAM_BURST_SIMILARITY`,
  `GROUP_SPAM_BURST_COUNT`, `GROUP_SPAM_BURST_WINDOW_SECS`, `GROUP_SPAM_BURST_TRACK_MAX`.
- Delivery and controls: `GROUP_INTRO_ON_JOIN`, `ENABLE_MESSAGE_REACTIONS`,
  `ADMIN_USER_IDS`, command overrides, and shared input/queue/rate limits.

Routing traces distinguish addressed replies, implicit decisions, spam detections,
and ambient drop stages (`cooldown`, `no_trigger`, `dice`) or passing candidates.
`FORWARD_DIAGNOSTICS` can mirror traces to the configured log channel; see
[observability](observability.md).

Representative coverage lives in [routing tests](../../tests/test_group_routing.py),
[implicit router tests](../../tests/test_group_router_implicit.py),
[ambient gate tests](../../tests/test_ambient_gate.py),
[affinity and command tests](../../tests/test_affinity_and_commands.py),
[recency commit tests](../../tests/test_recency_commit.py), and
[group extraction tests](../../tests/test_group_extraction.py), alongside the `test_prop_*`
property tests. See the [testing guide](testing_guide.md) for test setup and the
[roadmap](../roadmap.md) for remaining behavior gaps.
