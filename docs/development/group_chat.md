# Group Chat, Ambient Replies & Affinity

> **Status: ✅ implemented (Phase 9 in [project_plan.md](../project_plan.md)).**
> The configuration knobs live in [configuration.md](configuration.md#-group-chat--ambient-replies);
> the behavior below reflects the shipped code. DMs are unaffected — the private-chat path is
> byte-for-byte unchanged.

How ThinkMate behaves in group chats vs. DMs, how it decides when to chime in without being
spammy or abusing the LLM API, and how per-user affinity tunes its chattiness.

The pieces, at a glance:

| Concern | Where it lives |
|---|---|
| Pure no-LLM helpers + ambient funnel | [`app/services/group_gate.py`](../../app/services/group_gate.py) (`is_addressed`, `scan_cheap_triggers`, `scan_negative_signal`, `AmbientGate`) |
| Per-(chat, user) affinity/mode cache | [`app/services/affinity.py`](../../app/services/affinity.py) (`AffinityCache`, singleton `affinity_cache`) |
| Chat-type routing + addressed detection | [`app/handlers/messages.py`](../../app/handlers/messages.py) |
| `/quiet` `/chatty` commands | [`app/handlers/commands.py`](../../app/handlers/commands.py) |
| Multi-party reply + affinity fold | [`app/services/chat_manager.py`](../../app/services/chat_manager.py) |
| Multi-party memory extraction | [`app/services/memory_extractor.py`](../../app/services/memory_extractor.py) (`extract_and_trim_group`) |
| `chat_members` / buffer attribution | [`app/database/models.py`](../../app/database/models.py) (see [database.md](database.md)) |

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
*reply* runs through `AmbientGate` in [`group_gate.py`](../../app/services/group_gate.py) — a
pure, no-LLM funnel holding only a little in-memory per-chat state. The LLM is touched only at
the end, for the few candidates that survive. `AmbientGate.decide()` returns
`(should_chime, stage)` so the caller can log *which* stage dropped a message (observability,
Req 7.2); `should_chime()` is a thin boolean wrapper for callers that only need the verdict.

1. **Per-chat cooldown** (in-memory) — at most one ambient chime-in per `GROUP_AMBIENT_COOLDOWN_SECS`.
   A chat that has never chimed is treated as "cooldown elapsed". Stage: `"cooldown"`.
2. **Cheap trigger scan** (`scan_cheap_triggers`, regex/keywords, no LLM) — birthdays, congrats,
   laughter, group questions, greetings, strong sentiment. The gate advances a per-chat counter
   and also fires a periodic context-scan *tick* every `GROUP_CONTEXT_SCAN_EVERY` messages. No
   trigger and not a scan tick → stop. Stage: `"no_trigger"`.
3. **Affinity-weighted probability** — chime-in chance = `GROUP_AMBIENT_BASE_RATE × affinity ×
   mode_factor` (mode factor: `auto` → 1, `quiet` → 0, `chatty` → 1.5), clamped to `[0, 1]`. A
   dice roll keeps it organic, not robotic. `quiet` mode → 0. Stage: `"dice"` (drop) / `"pass"`.
4. **One LLM call** — craft a short chime-in from recent context; it may decline (empty reply →
   send nothing, suppressed in `user_task_manager._process_batch`).

**Cooldown reset on dispatch.** `decide()`/`should_chime()` never reset the cooldown. The router
([`messages.py`](../../app/handlers/messages.py)) calls `ambient_gate.mark_chimed(chat_id, now)`
*before* enqueueing the chime-in, so even a failed or empty model reply still holds the window
(Req 3.7). Idle per-chat state is bounded by `AmbientGate.prune`. The module exposes a singleton
`ambient_gate` for the hot path.

**Hybrid:** step 2 catches obvious moments instantly; additionally, once per
`GROUP_CONTEXT_SCAN_EVERY` messages, the scan tick lets a candidate through (still affinity-gated)
to catch subtler moments keywords miss. Either way it's ≤ ~1 ambient LLM call per active group
per cooldown window.

## Affinity signals (no extra LLM calls)

Affinity lives in [`affinity.py`](../../app/services/affinity.py)'s `AffinityCache` (write-through
to `chat_members`, clamped to `[0, 1]`). Signals:

* **Up**: a mention / reply-to-bot is a routing-level engagement signal — the router bumps the
  speaker `+0.05` in [`messages.py`](../../app/handlers/messages.py).
* **Down**: cheap keyword detection (`scan_negative_signal`: "stop / quiet / spam / annoying /
  shut up") applies a small down-bump (`-0.1`) to the speaker in
  [`chat_manager.py`](../../app/services/chat_manager.py).
* **Sentiment**: the reply call the bot is *already* making returns an optional `affinity_delta`
  field (groups only, via `generate_reply_bundle(..., with_affinity=True)`) — folded into the
  speaker's affinity at no extra LLM cost. Ignored in DMs. See [llm_integration.md](llm_integration.md).
* **Explicit**: `/quiet` (mode→quiet, suppress ambient) and `/chatty` (mode→chatty, boost), set
  via `affinity_cache.set_mode` in [`commands.py`](../../app/handlers/commands.py). In a DM both
  commands reply with a graceful no-op explanation (affinity has no meaning when the bot always
  replies).

## The single-write buffer invariant

Every group message must be buffered exactly once. The router enforces this by splitting the
write by path:

* **Addressed** messages are enqueued and the normal `enqueue_message → handle_message` path
  appends the user message itself (exactly like a DM), so the handler does **not** write them.
* **Non-addressed** messages never reach `handle_message`, so the handler is their only writer —
  it appends them (with `sender_id`/`sender_name`) before handing off to the ambient gate.

Net result: each group message is buffered once — addressed by the enqueue path, non-addressed by
the handler.

## Config knobs

`GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`, `GROUP_CONTEXT_SCAN_EVERY`,
`AFFINITY_DEFAULT`. See `docs/development/configuration.md`.

## Implementation checklist

- [x] L1. Bot identity (mention/reply-to-bot detection) + chat-type routing — `is_addressed`
  in [`group_gate.py`](../../app/services/group_gate.py); routing + cached `get_me()` identity in
  [`messages.py`](../../app/handlers/messages.py).
- [x] L2. `chat_id`-keyed buffers with sender attribution; multi-party history rendering —
  `add_message_to_buffer(..., sender_id, sender_name)` ([database.md](database.md)) and the
  multi-party `"Name: content"` render in [`chat_manager.py`](../../app/services/chat_manager.py).
- [x] L3. Affinity store (`chat_members`) + `/quiet` `/chatty` — `AffinityCache`
  ([`affinity.py`](../../app/services/affinity.py)) and the commands in
  [`commands.py`](../../app/handlers/commands.py).
- [x] L4. Ambient hybrid gate (triggers + cooldown + affinity probability + context scan) —
  `AmbientGate.decide` ([`group_gate.py`](../../app/services/group_gate.py)).
- [x] L5. Multi-party per-user memory extraction in groups — `extract_and_trim_group`
  ([`memory_extractor.py`](../../app/services/memory_extractor.py)).
- [x] L6. Tests for routing, gate funnel, affinity, multi-party extraction — see
  [testing_guide.md](testing_guide.md). Full suite: **158 passing**.
