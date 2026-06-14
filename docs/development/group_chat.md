# Group Chat, Ambient Replies & Affinity

> **Status: ✅ implemented (Phase 9 in [project_plan.md](../project_plan.md); extended by the
> implicit-bot-addressing spec).**
> The configuration knobs live in [configuration.md](configuration.md#-group-chat--ambient-replies);
> the behavior below reflects the shipped code. DMs are unaffected — the private-chat path is
> byte-for-byte unchanged.

This guide covers ThinkMate's group-chat subsystem: how the bot behaves in multi-user group and
supergroup chats versus one-on-one direct messages (DMs), how it decides when to join a
conversation on its own without being spammy or wasting LLM API calls, and how a per-user
*affinity* score — a learned value between 0 and 1 that measures how welcome the bot is to a given
person — tunes how chatty it is. Two ideas recur throughout. The *ambient gate* is a cheap,
no-LLM funnel that filters unaddressed messages before any model call. *Affinity* biases that
funnel per person, so the bot leans into people who engage and backs off those who do not. On top
of these, an *implicit-address gate* lets the bot recognize when someone is answering it without a
tag (the message right after it spoke is usually a direct reply), and two *spam detectors* keep
automated mass-tagging and greeting-burst floods from tricking the bot into replying.

This guide is organized as a tour of the moving parts followed by the rules that drive them:
behavior by chat type, the data model behind it, implicit addressing, spam protection, recency
tracking, the ambient gate, the affinity signals that feed it, the single-write buffer invariant,
memory/reply language handling, the configuration knobs, and an implementation checklist.
The pieces, at a glance:

| Concern | Where it lives |
|---|---|
| Pure no-LLM helpers + ambient funnel | [`app/services/group_gate.py`](../../app/services/group_gate.py) (`is_addressed`, `is_directed_at_other`, `scan_cheap_triggers`, `scan_negative_signal`, `AmbientGate`) |
| Implicit-address gate (recency + cooldown) | [`app/services/group_gate.py`](../../app/services/group_gate.py) (`ImplicitAddressGate`, singleton `implicit_gate`) |
| Mass-tag + greeting-burst spam detection | [`app/services/group_gate.py`](../../app/services/group_gate.py) (`count_distinct_mentions`, `is_mass_tag_spam`, `SpamBurstDetector`, singleton `spam_burst_detector`) |
| Per-(chat, user) affinity/mode cache | [`app/services/affinity.py`](../../app/services/affinity.py) (`AffinityCache`, singleton `affinity_cache`) |
| Chat-type routing + addressed/implicit/spam detection | [`app/handlers/messages.py`](../../app/handlers/messages.py) (`_handle_group_message`, `_maybe_ambient_chime`) |
| Recency commit point (`note_bot_spoke`) + idle pruning | [`app/services/user_task_manager.py`](../../app/services/user_task_manager.py) (`_process_batch`, `_evict_idle`) |
| `/quiet` `/chatty` commands | [`app/handlers/commands.py`](../../app/handlers/commands.py) |
| Multi-party reply + affinity fold | [`app/services/chat_manager.py`](../../app/services/chat_manager.py) |
| Multi-party memory extraction | [`app/services/memory_extractor.py`](../../app/services/memory_extractor.py) (`extract_and_trim_group`) |
| English-memory + reply language/script prompts | [`app/prompts/extraction_prompt.py`](../../app/prompts/extraction_prompt.py), [`app/prompts/system_prompt.py`](../../app/prompts/system_prompt.py) |
| `chat_members` / buffer attribution | [`app/database/models.py`](../../app/database/models.py) (see [database.md](database.md)) |

## Behavior by chat type

The first decision for any incoming message is what kind of chat it arrived in, because the reply
policy differs sharply between a private DM and a busy group. *Addressed* means the message
targets the bot directly — an @mention, the bot's name, or a reply to one of the bot's messages.
The rules by chat type:

| Chat type | Behavior |
|---|---|
| **Private (DM)** | Reply to every *conversational* message. **Bot (slash) commands are excluded** — they are not treated as conversation and are not replied to. |
| **Group / supergroup** | **Always** reply when *addressed* (bot @mentioned, bot's name used, or a reply to the bot's message). Otherwise, if the bot spoke recently and the message is not aimed at someone else, treat it as an **implicit address** and reply directly (throttled by a per-chat cooldown). Otherwise run the **ambient gate** — sometimes chime in on the wider conversation (birthdays, jokes, questions…), modulated by affinity. Mass-tag and greeting-burst **spam** never trigger any of these. |
| **Channel** | Ignored. |

> **Bot commands in DMs are never treated as conversation.** Registered commands
> (`/start`, `/help`, `/profile`, `/reset`) are handled by their dedicated command
> handlers. Any unregistered slash command (e.g. `/foo`) falls through to the catch-all
> text handler, which ignores it — no LLM reply and no memory enqueue. Normal
> conversational messages are still replied to as before.

## Data model

The group features rest on three storage decisions: where messages are buffered, where extracted
memory lives, and where affinity is stored. Keeping these separate is what lets a single
group-aware path serve DMs unchanged.

* **Buffers are keyed by `chat_id`** (in a DM, `chat_id == user_id`, so DMs are unchanged).
  Each buffered message carries `sender_id` + `sender_name`, so group context is multi-party
  ("Alice: …", "Bob: …") and memory can be attributed to the right person.
* **Memory (facts/beliefs/events) stays per `user_id`.** In groups, extraction is multi-party:
  one LLM call over the segment returns updates tagged by participant name, which are mapped
  back to each `sender_id` (using the segment's own name→id map) and saved to each profile.
* **Affinity** lives in `chat_members` (`_id = "{chat_id}:{user_id}"`): `affinity` (0–1,
  default 0.5), `mode` (`auto` / `quiet` / `chatty`). Cached in memory, written through on change.

## Implicit addressing (replying without being tagged)

In real conversations people answer the bot without tagging it — the message right after the bot
speaks is usually a direct reply to it. The **implicit-address gate** (`ImplicitAddressGate` in
[`group_gate.py`](../../app/services/group_gate.py), singleton `implicit_gate`) recognizes those
moments. It is a pure, no-LLM decision helper that mirrors `AmbientGate`'s shape: bounded in-memory
per-chat maps keyed by `chat_id`, an injectable `now` for deterministic tests, a decision/commit
split, and a `prune` hook wired into the idle sweep.

The router consults it **only** on non-explicitly-addressed group messages, strictly between the
explicit-address check and the ambient gate. A message is an *implicit address* when **all** hold:

* the bot has spoken in this chat before (`note_bot_spoke` has recorded a last-spoke time);
* the message falls inside the **Bot_Recency_Window** — meaning **both** bounds hold (logical AND,
  the conservative reading): the bot's last message is within `GROUP_IMPLICIT_RECENCY_SECS` of now,
  **and** at most `GROUP_IMPLICIT_RECENCY_MAX_MSGS` human messages have arrived since the bot spoke
  (the follow-up must be both *soon* and *not buried* under other people's chatter);
* the message is not **directed at another participant** (`is_directed_at_other`: it replies to a
  non-bot message, or it @mentions another user);
* the message is not spam (see below).

`decide(...)` returns `(is_implicit, reason)` where `reason` is one of `"spam"`, `"no_bot_activity"`,
`"directed_at_other"`, `"out_of_window"`, or `"implicit"`, so the router can log *why* a message was
or wasn't treated as a direct reply. It is a pure predicate — no state mutation, never raises
(malformed input degrades to not-implicit so the message simply falls through to the ambient gate,
Req 1.6).

**Throttle (Implicit_Cooldown).** To stop the bot dominating a burst of follow-ups, at most one
implicit reply fires per `GROUP_IMPLICIT_COOLDOWN_SECS` per chat. When a message is implicit *and*
`cooldown_elapsed(chat_id, now)` is true, the router commits the throttle with
`mark_implicit_reply` **before** enqueueing (so even an empty/failed reply still holds the window,
Req 3.3), logs the decision with the chat id (Req 3.2), and enqueues with `reason="reply"`. If more
than one participant sends an implicit address inside one cooldown window, only the first is
answered directly; the rest fall through to the ambient gate (Req 4.4).

**Counter ordering.** `implicit_gate.note_human_message(chat_id, now)` is called *after* `decide`
on every path, so the current message is never counted among its own "intervening" predecessors.
The immediate follow-up to a bot message therefore evaluates with `intervening == 0` — the strongest
implicit candidate. `note_human_message` only advances the counter once the bot has spoken (there is
no window to fill before the bot's first message).

**Affinity.** The explicit `+0.05` mention/reply-to-bot bump stays on the addressed path only — an
implicit address is neither a mention nor a reply-to-bot, so it earns no routing bump. The
sentiment-based `affinity_delta` from the reply call still folds in (implicit replies go through the
same `reason="reply"` generation path), so affinity signals keep applying without new semantics.

## Spam protection (mass-tagging and greeting bursts)

Group chats attract userbots that farm attention by tagging members with short greetings. Two
no-LLM detectors keep the bot from replying to or amplifying that noise. Both feed a single combined
`spam` flag (`spam = is_mass or is_burst`) computed once, up front, that drives three downstream
decisions: the spam-aware explicit check, the implicit detector, and ambient trigger suppression.

**Mass-tag spam (single message).** `is_mass_tag_spam(text, entities, *, threshold)` counts the
*distinct* participants a message @mentions (`count_distinct_mentions`: `mention` entities by
case-folded handle, `text_mention` entities by carried `user.id`, de-duplicated) and flags the
message as spam when the count is strictly greater than `GROUP_MASS_TAG_SPAM_THRESHOLD`. The bot's
own mention is **not** excluded from the count — a bulk tag that happens to sweep up the bot is
exactly the spam case. Fully defensive: any internal error degrades to `False` ("not spam") so a
classification bug can never suppress a legitimate reply (Req 9.6).

**Greeting-burst spam (over time).** A userbot can also tag members one-by-one in rapid succession
with near-identical low-content greetings ("hi @a", "hi @b", "hi @c"…) — no single message trips the
mention-count threshold. `SpamBurstDetector` (singleton `spam_burst_detector`) is a **stateful**
no-LLM detector that catches this. Per chat it keeps a `deque` of recent
`(arrival_time, mention_stripped_content)` pairs, bounded by both the time window
`GROUP_SPAM_BURST_WINDOW_SECS` and a hard cap `GROUP_SPAM_BURST_TRACK_MAX`. Its single entry point,
`observe(chat_id, text, entities, now)`:

1. strips `@mention`/`text_mention` entity slices from the text (regex `@\w+` fallback when entities
   are absent or malformed), then case-folds and whitespace-collapses the remainder — so "hi @alice"
   and "hi @bob" both reduce to "hi" and compare as identical (Req 10.1);
2. evicts this chat's history entries older than the window;
3. counts retained entries that are *near-identical* — `difflib.SequenceMatcher(None, a, b).ratio()
   >= GROUP_SPAM_BURST_SIMILARITY` (standard library, deterministic, dependency-free);
4. appends `(now, content)` (hard-capped at `GROUP_SPAM_BURST_TRACK_MAX`);
5. including the just-added message, returns `True` when the near-identical count reaches
   `GROUP_SPAM_BURST_COUNT`, else `False`.

A lone or sub-threshold greeting is never flagged (Req 10.8). `observe` must run on **every** group
message so its time-windowed history is complete regardless of which path the message takes; it is
fully defensive (errors degrade to "not burst", Req 10.14) and is deterministic given `now`, so it
unit- and property-tests with a fresh instance and an injected clock.

**Spam-aware explicit address.** Spam-awareness is layered in the router rather than folded into the
pure `is_addressed` (which several tests pin), so the explicit decision reads:

```
reply_to_bot      -> Explicit_Address      # a deliberate reply-to-bot survives spam (Req 9.5, 10.7)
elif spam         -> NOT Explicit_Address   # a bare bot @mention buried in bulk/burst spam (Req 9.4, 10.6)
else              -> is_addressed(...)       # unchanged addressed path (Req 10.9)
```

A deliberate Telegram *reply* to one of the bot's own messages is the one signal automated
mass-tagging cannot fake, so it still counts as explicit even under spam — a real person quoting the
bot is never silenced. But a bare bot @mention swept into a bulk tag list or a greeting-burst
message does **not** promote the message to explicit. Spam also suppresses cheap-trigger ambient
firing (see the ambient gate section).

## Recency tracking and the commit point

The implicit gate needs to know when the bot last spoke. That is recorded at the one place that
knows a bot message actually went out: `UserTaskManager._process_batch`, in the branch that performs
`await last_message.answer(reply_text)`. For a group chat it calls
`implicit_gate.note_bot_spoke(chat_id, time.time())` (wrapped defensively so a tracking failure never
breaks delivery). `note_bot_spoke` sets the last-spoke time **and** resets the since-bot human
counter to 0, so the recency window reopens from the bot's latest message.

It is deliberately **not** called when an ambient chime-in produced an empty/declined reply (nothing
was sent) — the recency window only opens when the bot genuinely spoke. Idle per-chat state for both
`implicit_gate` and `spam_burst_detector` is pruned by the existing idle sweep in
`UserTaskManager._evict_idle` (alongside `ambient_gate` and `affinity_cache`), keeping memory bounded
(Req 6.4, 10.13).

## The ambient gate (never abuses the LLM)


*Ambient* replies are the messages the bot volunteers when nobody addressed it directly. The hard
part is picking those moments without spamming the chat or burning an LLM call on every line, so
the decision runs through a staged funnel that only reaches the model at the very end.

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
   trigger and not a scan tick → stop. Stage: `"no_trigger"`. **Spam suppresses triggers:**
   `_maybe_ambient_chime` takes `is_spam` and computes `triggered = scan_cheap_triggers(user_text)
   and not is_spam`, so greeting/laughter keywords in a mass-tag or greeting-burst message can never
   fire the gate (Req 9.3, 10.5). The message still flows through `AmbientGate.decide` and drops at
   `"no_trigger"`, so the single-write invariant is untouched.
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

Affinity is the bot's running sense of how welcome it is to each person, and it moves only on
cheap signals — never a dedicated LLM call. It lives in
[`affinity.py`](../../app/services/affinity.py)'s `AffinityCache` (write-through
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

Group context is only trustworthy if every message lands in the buffer exactly once — no drops,
no duplicates. Every group message must be buffered exactly once. The router enforces this by
splitting the write by path:

* **Addressed** messages are enqueued and the normal `enqueue_message → handle_message` path
  appends the user message itself (exactly like a DM), so the handler does **not** write them.
* **Implicit replies** take the same path as addressed messages: the router enqueues them with
  `reason="reply"` and does **not** write the buffer itself, so the enqueue path is their sole writer
  (Req 3.4).
* **Non-addressed** messages never reach `handle_message`, so the handler is their only writer —
  it appends them (with `sender_id`/`sender_name`) before handing off to the ambient gate.

Net result: each group message is buffered once — addressed and implicit-reply by the enqueue path,
non-addressed by the handler/ambient path. DM behavior is byte-for-byte unchanged: DMs never reach
the implicit detector or the spam scans (Req 5.1).

## Config knobs

These knobs tune the ambient gate's pacing, the implicit-address gate, the spam detectors, and the
affinity defaults; each is documented with its default in the configuration guide.

**Ambient + affinity:** `GROUP_AMBIENT_COOLDOWN_SECS`, `GROUP_AMBIENT_BASE_RATE`,
`GROUP_CONTEXT_SCAN_EVERY`, `AFFINITY_DEFAULT`.

**Implicit addressing + spam** (all read live from `config`, env-overridable, each with a loader
default):

| Knob | Default | What it tunes |
|---|---|---|
| `GROUP_IMPLICIT_RECENCY_SECS` | `120.0` | Bot_Recency_Window elapsed-time bound — max seconds since the bot spoke for a follow-up to be implicit. |
| `GROUP_IMPLICIT_RECENCY_MAX_MSGS` | `4` | Bot_Recency_Window intervening-message bound — max human messages since the bot spoke. |
| `GROUP_IMPLICIT_COOLDOWN_SECS` | `30.0` | Implicit_Cooldown — at most one implicit direct reply per chat per window. |
| `GROUP_MASS_TAG_SPAM_THRESHOLD` | `5` | Distinct-@mention count above which a single message is mass-tag spam (strict `>`). |
| `GROUP_SPAM_BURST_SIMILARITY` | `0.85` | `difflib` ratio at/above which two mention-stripped contents are near-identical. |
| `GROUP_SPAM_BURST_COUNT` | `3` | Near-identical messages within the window that classify a greeting burst. |
| `GROUP_SPAM_BURST_WINDOW_SECS` | `60.0` | Time window over which burst near-identical messages are counted. |
| `GROUP_SPAM_BURST_TRACK_MAX` | `20` | Hard cap on tracked recent messages per chat (bounds memory). |

See `docs/development/configuration.md`.

## Memory language & reply language/script (Part B)

Two prompt-only behaviors round out the natural-conversation goal. They are **independent** of each
other and of the routing logic above.

* **Memory is always stored in English.** A top-level *LANGUAGE NORMALIZATION* rule in
  `SYSTEM_EXTRACTION_PROMPT` ([`extraction_prompt.py`](../../app/prompts/extraction_prompt.py))
  directs the `Memory_Extractor` to store every fact/belief/event in English regardless of the
  conversation language — translating (not transliterating) non-English content to natural English,
  while preserving proper nouns, names, and quoted identifiers in their original form (e.g. Hindi
  "मुझे पुणे में नौकरी मिली" → fact `"Got a job in Pune"`). The `_GROUP_EXTRACTION_NOTE` reinforces
  this per participant for multi-party segments.
* **Replies match the user's language and script.** The *Language & script* bullet in
  `DEFAULT_SYSTEM_PROMPT_TEMPLATE` ([`system_prompt.py`](../../app/prompts/system_prompt.py)) directs
  the `Reply_Generator` to reply in the user's current language, judged from the recent flow of the
  conversation (not one isolated message) and switching when recent usage shifts. For Hindi it
  matches the *script*: Hinglish (Hindi in Latin letters) → reply in Hinglish; Devanagari Hindi →
  reply in Devanagari.

The two are explicitly independent: language/script matching applies to the **reply only** and does
not change how memories are stored (always English).

## Implementation checklist

The lettered items (L1–L6) track the shipped layers of the group-chat feature; every box is
checked, matching the Phase 9 status noted at the top of this guide.

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
  [testing_guide.md](testing_guide.md).
- [x] L7. Implicit-address gate (Bot_Recency_Window + Implicit_Cooldown, recency commit at
  `note_bot_spoke`) — `ImplicitAddressGate`/`is_directed_at_other`
  ([`group_gate.py`](../../app/services/group_gate.py)), commit + prune in
  [`user_task_manager.py`](../../app/services/user_task_manager.py).
- [x] L8. Mass-tag + greeting-burst spam protection and the spam-aware explicit-address rule —
  `is_mass_tag_spam`/`SpamBurstDetector` ([`group_gate.py`](../../app/services/group_gate.py)) wired
  into `_handle_group_message`/`_maybe_ambient_chime`
  ([`messages.py`](../../app/handlers/messages.py)).
- [x] L9. English-memory normalization + reply language/script matching (Part B) — prompt rules in
  [`extraction_prompt.py`](../../app/prompts/extraction_prompt.py) and
  [`system_prompt.py`](../../app/prompts/system_prompt.py).
