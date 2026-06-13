# Group Chat, Ambient Replies & Affinity

> **Status: designed, not yet implemented (Phase 9 in [project_plan.md](../project_plan.md)).**
> The configuration knobs already exist (see [configuration.md](configuration.md#-group-chat--ambient-replies));
> the behavior below is the build target. DMs are unaffected.

How ThinkMate behaves in group chats vs. DMs, how it decides when to chime in without being
spammy or abusing the LLM API, and how per-user affinity tunes its chattiness.

## Behavior by chat type

| Chat type | Behavior |
|---|---|
| **Private (DM)** | Reply to every *conversational* message. **Bot (slash) commands are excluded** — they are not treated as conversation and are not replied to. |
| **Group / supergroup** | **Always** reply when *addressed* (bot @mentioned, bot's name used, or a reply to the bot's message). Otherwise run the **ambient gate** — sometimes chime in on the wider conversation (birthdays, jokes, questions…), modulated by affinity. |
| **Channel** | Ignored. |

> **Bot commands in DMs are never treated as conversation.** Registered commands
> (`/start`, `/help`, `/profile`, `/reset`) are handled by their dedicated command
> handlers. Any unregistered slash command (e.g. `/foo`) falls through to the catch-all
> text handler, which ignores it — no LLM reply and no memory enqueue. Normal
> conversational messages are still replied to as before.

## Data model

* **Buffers are keyed by `chat_id`** (in a DM, `chat_id == user_id`, so DMs are unchanged).
  Each buffered message carries `sender_id` + `sender_name`, so group context is multi-party
  ("Alice: …", "Bob: …") and memory can be attributed to the right person.
* **Memory (facts/beliefs/events) stays per `user_id`.** In groups, extraction is multi-party:
  one LLM call over the segment returns updates tagged by participant name, which are mapped
  back to each `sender_id` (using the segment's own name→id map) and saved to each profile.
* **Affinity** lives in `chat_members` (`_id = "{chat_id}:{user_id}"`): `affinity` (0–1,
  default 0.5), `mode` (`auto` / `quiet` / `chatty`). Cached in memory, written through on change.

## The ambient gate (never abuses the LLM)

Every group message is recorded to the buffer (cheap, for context + learning). Whether to
*reply* runs through a funnel; the LLM is touched only at the end, for the few that survive:

1. **Per-chat cooldown** (in-memory) — at most one ambient chime-in per `GROUP_AMBIENT_COOLDOWN_SECS`.
2. **Cheap trigger scan** (regex/keywords, no LLM) — birthdays, congrats, laughter, group
   questions, greetings, strong sentiment. No trigger and not a periodic-scan tick → stop.
3. **Affinity-weighted probability** — chime-in chance = `GROUP_AMBIENT_BASE_RATE × affinity ×
   mode_factor`. A dice roll keeps it organic, not robotic. `quiet` mode → 0.
4. **One LLM call** — craft a short chime-in from recent context; it may decline (empty → send nothing).

**Hybrid:** step 2 catches obvious moments instantly; additionally, once per cooldown window of
activity, a single context-scan call may run (affinity-gated) to catch subtler moments keywords miss.
Either way it's ≤ ~1 ambient LLM call per active group per window.

## Affinity signals (no extra LLM calls)

* **Up**: user replies to / mentions the bot; engages after a chime-in.
* **Down**: cheap keyword detection of "stop / quiet / spam / annoying / shut up"; the bot is
  ignored after chiming in.
* **Sentiment**: the reply call the bot is *already* making returns an optional `affinity_delta`
  field — folded into the existing JSON, costing nothing extra.
* **Explicit**: `/quiet` (mode→quiet, suppress ambient) and `/chatty` (mode→chatty, boost).

## Config knobs

`GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`, `GROUP_CONTEXT_SCAN_EVERY`,
`AFFINITY_DEFAULT`. See `docs/development/configuration.md`.

## Implementation checklist

- [ ] L1. Bot identity (mention/reply-to-bot detection) + chat-type routing.
- [ ] L2. `chat_id`-keyed buffers with sender attribution; multi-party history rendering.
- [ ] L3. Affinity store (`chat_members`) + `/quiet` `/chatty`.
- [ ] L4. Ambient hybrid gate (triggers + cooldown + affinity probability + context scan).
- [ ] L5. Multi-party per-user memory extraction in groups.
- [ ] L6. Tests for routing, gate funnel, affinity, multi-party extraction.
