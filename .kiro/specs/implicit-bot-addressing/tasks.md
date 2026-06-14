# Implementation Plan: Implicit Bot Addressing

## Overview

This plan implements two independent enhancements described in the design:

- **Part A / A-spam / A-burst** — a no-LLM `ImplicitAddressGate`, mass-tag and greeting-burst spam
  detectors, and their router integration that lets the bot reply to implicitly-addressed group
  messages while suppressing spam and preserving every existing invariant (single-write buffer,
  ambient cooldown, affinity signals, byte-for-byte DM behavior).
- **Part B** — prompt-only changes for English memory normalization (extraction prompt) and
  reply language/script matching (system prompt).

The work is incremental and test-driven, in Python with pytest + Hypothesis, mirroring the existing
pure/deterministic `tests/test_ambient_gate.py` style (fresh instance per test, injected `now`,
config knobs overridden with set/restore in `try/finally`). Each of the 13 correctness properties
becomes exactly one Hypothesis property test (min 100 iterations) tagged with its property
reference. Tasks build bottom-up: config → pure helpers → stateful gates → router wiring → recency
commit point → Part B prompts → docs.

## Tasks

- [x] 1. Add group implicit/spam configuration knobs
  - [x] 1.1 Add the eight new config fields to `app/config.py`
    - In the `Config` model add: `GROUP_IMPLICIT_RECENCY_SECS` (float, default `120.0`),
      `GROUP_IMPLICIT_RECENCY_MAX_MSGS` (int, default `4`), `GROUP_IMPLICIT_COOLDOWN_SECS` (float,
      default `30.0`), `GROUP_MASS_TAG_SPAM_THRESHOLD` (int, default `5`),
      `GROUP_SPAM_BURST_SIMILARITY` (float, default `0.85`), `GROUP_SPAM_BURST_COUNT` (int,
      default `3`), `GROUP_SPAM_BURST_WINDOW_SECS` (float, default `60.0`),
      `GROUP_SPAM_BURST_TRACK_MAX` (int, default `20`)
    - Use the existing `_env_int` / `_env_float` `Field(default_factory=...)` pattern so each value
      is env-overridable and falls back to its default when unset (defaults supplied by the loader)
    - Group them under a new "Group chat / implicit addressing & spam" comment section near the
      existing "Group chat / ambient replies" block
    - _Design: Data Models (config knob table). Requirements: 4.2, 4.3, 9.7, 10.11, 10.12_

  - [ ]* 1.2 Write configuration smoke test
    - New file `tests/test_group_config_smoke.py`
    - Assert all eight knobs exist on `config` with their documented default values
    - Assert each is read live (override via env or monkeypatching `config` attribute and confirm a
      consumer reflects the new value) — sensible-default coverage for Req 10.12
    - _Design: Testing Strategy (Configuration smoke tests). Requirements: 4.2, 4.3, 9.7, 10.11, 10.12_

- [x] 2. Implement pure no-LLM scan helpers in `app/services/group_gate.py`
  - [x] 2.1 Implement `count_distinct_mentions` and `is_mass_tag_spam`
    - `count_distinct_mentions(text, entities) -> int`: count distinct @mentioned participants —
      `mention` entities distinct by case-folded sliced handle text, `text_mention` entities
      distinct by carried `user.id`; de-duplicate so tagging the same person twice counts once;
      tolerant of `None`/empty/malformed entities; never raises (return 0 on total failure)
    - `is_mass_tag_spam(text, entities, *, threshold: int) -> bool`: `True` when
      `count_distinct_mentions(...) > threshold` (strict `>`); fully defensive — any internal error
      degrades to `False` ("not spam"); does NOT exclude the bot's own mention from the count
    - Reuse the existing `_MENTION_ENTITY_TYPES` set and entity-reading style from `is_addressed`
    - _Design: Components A0 (count_distinct_mentions / is_mass_tag_spam). Requirements: 9.1, 9.6_

  - [x] 2.2 Implement `is_directed_at_other`
    - `is_directed_at_other(*, entities, reply_to_other: bool) -> bool`: `True` when a
      non-explicitly-addressed message replies to a non-bot message (`reply_to_other`) OR carries a
      `mention`/`text_mention` entity (which must reference another participant, since the bot's own
      mention is caught earlier by `is_addressed`)
    - Reuse the existing `_has_mention_entity` scan for the @mention signal; fully defensive —
      malformed input degrades to `False`, never raises
    - _Design: Components A1 (is_directed_at_other). Requirements: 2.1, 2.2, 2.3_

  - [ ]* 2.3 Write unit tests for the pure scan helpers
    - New file `tests/test_group_gate_helpers.py`
    - `count_distinct_mentions`: dedup by handle and by user id, mixed `mention`/`text_mention`,
      bot-mention counted, malformed/None entities → 0
    - `is_mass_tag_spam`: strict `>` boundary (threshold N → N mentions not spam, N+1 spam)
    - `is_directed_at_other`: reply-to-other true, mention-other true, neither false, malformed → False
    - _Design: Testing Strategy (Example / unit tests). Requirements: 2.1, 2.2, 2.3, 9.1, 9.6_

- [x] 3. Implement `ImplicitAddressGate` in `app/services/group_gate.py`
  - [x] 3.1 Implement the `ImplicitAddressGate` class and `implicit_gate` singleton
    - Per-chat in-memory maps keyed by `chat_id`: `_bot_last_spoke: dict[int, float]`,
      `_human_since_bot: dict[int, int]`, `_last_implicit_reply: dict[int, float]`,
      `_last_seen: dict[int, float]`
    - `note_bot_spoke(chat_id, now)`: set last-spoke time, reset human counter to 0, update last_seen
    - `note_human_message(chat_id, now) -> int`: increment & return since-bot counter (only once the
      bot has spoken), update last_seen
    - `decide(chat_id, *, directed_at_other, is_spam, now) -> tuple[bool, str]`: pure predicate (no
      mutation, never raises). Reject `is_spam` first → `(False, "spam")`; then `no_bot_activity`
      (never spoke) → `(False, ...)`; then `directed_at_other` → `(False, "directed_at_other")`;
      then window check using AND of both bounds — `elapsed = now - _bot_last_spoke <=
      GROUP_IMPLICIT_RECENCY_SECS` AND `intervening = _human_since_bot.get(chat_id, 0) <=
      GROUP_IMPLICIT_RECENCY_MAX_MSGS` → `(True, "implicit")` else `(False, "out_of_window")`. The
      current message is NOT yet counted
    - `cooldown_elapsed(chat_id, now) -> bool`: `True` when `GROUP_IMPLICIT_COOLDOWN_SECS` has
      elapsed since `_last_implicit_reply` (or never replied)
    - `mark_implicit_reply(chat_id, now)`: record implicit-reply time (resets Implicit_Cooldown),
      update last_seen
    - `prune(now, max_idle=None) -> int`: drop per-chat state idle beyond `max_idle` (default mirrors
      `AmbientGate`); return count pruned
    - Read all knobs live from `config`; expose module-level `implicit_gate = ImplicitAddressGate()`
    - _Design: Components A2 (ImplicitAddressGate). Requirements: 1.2, 1.3, 1.4, 1.5, 3.1, 3.3, 4.1, 4.4, 6.1, 6.2, 6.3, 6.4_

  - [-]* 3.2 Write property test: recency-window implicit classification
    - New file `tests/test_prop_recency_window.py`
    - **Property 1: Recency-window implicit classification**
    - **Validates: Requirements 1.2, 1.3, 1.4, 6.1, 6.2**

  - [-]* 3.3 Write property test: directed-at-other suppression
    - New file `tests/test_prop_directed_at_other.py`
    - **Property 2: Directed-at-other suppression**
    - **Validates: Requirements 2.1, 2.2, 2.3**

  - [-]* 3.4 Write property test: implicit cooldown bounds direct replies
    - New file `tests/test_prop_implicit_cooldown_bounds.py`
    - **Property 3: Implicit cooldown bounds direct replies** (assert at-most-one via
      `cooldown_elapsed`/`mark_implicit_reply` over a burst within one window; new candidate after
      full elapse may reply again)
    - **Validates: Requirements 3.1, 4.1, 4.4**

  - [-]* 3.5 Write property test: cooldown reset on implicit reply
    - New file `tests/test_prop_cooldown_reset.py`
    - **Property 4: Cooldown reset on implicit reply** (after `mark_implicit_reply(t)`,
      `cooldown_elapsed(t')` is `False` for all `t <= t' < t + GROUP_IMPLICIT_COOLDOWN_SECS`)
    - **Validates: Requirements 3.3**

  - [-]* 3.6 Write property test: mass-tag-spam classification and implicit suppression
    - New file `tests/test_prop_mass_tag_spam.py`
    - **Property 5: Mass-tag-spam classification and implicit suppression** (count > threshold →
      `is_mass_tag_spam` True; `decide(..., is_spam=True)` not implicit even inside the recency window)
    - **Validates: Requirements 9.1, 9.2**

- [x] 4. Implement `SpamBurstDetector` in `app/services/group_gate.py`
  - [x] 4.1 Implement the `SpamBurstDetector` class and `spam_burst_detector` singleton
    - Per-chat state: `_recent: dict[int, deque[tuple[float, str]]]` (window + hard-cap bounded),
      `_last_seen: dict[int, float]`
    - `observe(chat_id, text, entities, now) -> bool`: (1) strip `@mention`/`text_mention` entity
      slices from `text`, then case-fold + whitespace-collapse → `content` (regex `@\w+` fallback
      when entities absent/malformed); (2) evict entries older than `GROUP_SPAM_BURST_WINDOW_SECS`;
      (3) count retained entries with `difflib.SequenceMatcher(None, a, b).ratio() >=
      GROUP_SPAM_BURST_SIMILARITY`; (4) append `(now, content)` (hard-capped at
      `GROUP_SPAM_BURST_TRACK_MAX`); (5) including the just-added message, return `True` when the
      near-identical count reaches `GROUP_SPAM_BURST_COUNT` else `False`. Fully defensive — any
      internal error degrades to `False`; update `_last_seen`
    - `prune(now, max_idle=None) -> int`: drop per-chat history idle beyond `max_idle`; return count
    - Read knobs live from `config`; expose `spam_burst_detector = SpamBurstDetector()`
    - _Design: Components A0b (SpamBurstDetector). Requirements: 10.1, 10.2, 10.3, 10.8, 10.10, 10.13_

  - [-]* 4.2 Write property test: burst similarity excludes @mention tokens
    - New file `tests/test_prop_burst_mention_exclusion.py`
    - **Property 10: Burst similarity excludes @mention tokens** (same base content with different
      mention sets reduces to identical mention-stripped content → maximal similarity → near-identical)
    - **Validates: Requirements 10.1, 10.2**

  - [-]* 4.3 Write property test: greeting-burst classification threshold within the window
    - New file `tests/test_prop_burst_threshold.py`
    - **Property 11: Greeting-burst classification threshold within the window** (classified as
      burst iff near-identical count within `GROUP_SPAM_BURST_WINDOW_SECS` reaches
      `GROUP_SPAM_BURST_COUNT`; lone/sub-threshold/spaced-beyond-window → not burst)
    - **Validates: Requirements 10.3, 10.8**

- [ ] 5. Cross-cutting gate property tests (pruning & defensive degradation)
  - [-]* 5.1 Write property test: recency and burst state pruning
    - New file `tests/test_prop_state_pruning.py`
    - **Property 8: Recency and burst state pruning** (`prune(now, max_idle)` on both
      `ImplicitAddressGate` and `SpamBurstDetector` removes exactly the idle-beyond-`max_idle` chats
      and keeps recently-active ones)
    - **Validates: Requirements 6.4, 10.13**

  - [-]* 5.2 Write property test: defensive degradation (never raise)
    - New file `tests/test_prop_defensive_degradation.py`
    - **Property 9: Defensive degradation (never raise)** (fuzz malformed entities, non-string text,
      `None` across `is_mass_tag_spam`, `SpamBurstDetector.observe`, `ImplicitAddressGate.decide`;
      assert no exception escapes and the verdict is the safe default)
    - **Validates: Requirements 1.6, 9.6, 10.14**

- [~] 6. Checkpoint - pure gate/detector logic complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Wire the detectors into the group router (`app/handlers/messages.py`)
  - [x] 7.1 Add spam-awareness to `_maybe_ambient_chime`
    - Add keyword-only `is_spam: bool = False` parameter
    - Change the cheap-trigger computation to `triggered = scan_cheap_triggers(user_text) and not
      is_spam` so greeting/laughter keywords cannot fire the ambient gate for a spam message
    - Preserve the existing single-write invariant exactly (gate remains the sole buffer writer on
      its drop branch); no new buffer-write paths
    - _Design: Components A6 (Ambient trigger suppression for spam). Requirements: 9.3, 10.5_

  - [x] 7.2 Integrate spam classification, spam-aware explicit check, and the implicit detector into `_handle_group_message`
    - Import `is_mass_tag_spam`, `is_directed_at_other`, `implicit_gate`, `spam_burst_detector`
    - Compute `now = time.time()` once; classify both spam shapes up front, each in its own
      defensive `try/except` (`is_burst = spam_burst_detector.observe(...)` on EVERY group message;
      `is_mass = is_mass_tag_spam(..., threshold=config.GROUP_MASS_TAG_SPAM_THRESHOLD)`); combine
      `spam = is_mass or is_burst`
    - Spam-aware explicit decision replacing the bare `is_addressed` call:
      `reply_to_bot → explicit`; `elif spam → not explicit`; `else is_addressed(...)`. Explicit path
      keeps the `+0.05` affinity bump and `enqueue_message(reason="reply")`, then calls
      `implicit_gate.note_human_message(chat_id, now)`, then returns (no buffer write — single-write)
    - Not-addressed path: compute `reply_to_other` and
      `directed_at_other = is_directed_at_other(entities=message.entities, reply_to_other=...)`;
      call `implicit_gate.decide(...)` inside a `try/except` (failure → `is_implicit = False`)
    - If `is_implicit and implicit_gate.cooldown_elapsed(chat_id, now)`:
      `mark_implicit_reply` (before enqueue) → log the implicit-reply decision with the chat id →
      `note_human_message` → `enqueue_message(reason="reply")` (no buffer write) → return
    - Otherwise: `note_human_message` then `await _maybe_ambient_chime(..., is_spam=spam)`
    - Keep `note_human_message` AFTER `decide` on every path so the current message is never counted
      as its own intervening predecessor
    - _Design: Components A3 (Router integration). Requirements: 1.1, 3.1, 3.2, 3.3, 3.4, 4.1, 5.2, 5.3, 5.4, 5.5, 9.2, 9.4, 9.5, 10.4, 10.6, 10.7, 10.9_

  - [ ]* 7.3 Write property test: spam suppresses cheap-trigger ambient firing
    - New file `tests/test_prop_spam_trigger_suppression.py`
    - **Property 6: Spam suppresses cheap-trigger ambient firing** (`scan_cheap_triggers(text) and
      not is_mass_tag_spam` is `False` for any mass-tag-spam message)
    - **Validates: Requirements 9.3**

  - [ ]* 7.4 Write property test: spam-aware explicit address
    - New file `tests/test_prop_spam_aware_explicit.py`
    - **Property 7: Spam-aware explicit address** (spam message is explicit iff reply-to-bot; a bare
      bot @mention buried in a bulk list, no reply-to-bot, is not explicit)
    - **Validates: Requirements 9.4, 9.5**

  - [ ]* 7.5 Write property test: greeting-burst suppresses implicit classification and ambient triggers
    - New file `tests/test_prop_burst_suppression.py`
    - **Property 12: Greeting-burst suppresses implicit classification and ambient triggers**
      (`decide(..., is_spam=True)` not implicit even inside recency window; `scan_cheap_triggers and
      not is_spam` is `False`)
    - **Validates: Requirements 10.4, 10.5**

  - [ ]* 7.6 Write property test: burst-aware explicit address
    - New file `tests/test_prop_burst_aware_explicit.py`
    - **Property 13: Burst-aware explicit address** (burst message is explicit iff reply-to-bot; a
      bare bot @mention inside a burst with no reply-to-bot is not explicit; a non-burst genuine
      explicit address uses the existing addressed path)
    - **Validates: Requirements 10.6, 10.7, 10.9**

  - [ ]* 7.7 Write router example/wiring tests
    - New file `tests/test_group_router_implicit.py`
    - Decision order: non-explicit message consults the implicit detector before the ambient gate
      (spy/fake gate) — Req 1.1
    - Single-write invariant: implicit-reply and explicit-reply paths do NOT call
      `add_message_to_buffer`; ambient drop writes exactly once; ambient pass writes via enqueue —
      Req 3.4, 5.4
    - DM unchanged (no detector invoked) — Req 5.1; explicit path unchanged — Req 5.2; ambient
      fallthrough — Req 5.3
    - Implicit-reply logging includes the chat id — Req 3.2
    - Defensive fallthrough: a `decide`/classifier stub that raises → message reaches the ambient
      path — Req 1.6, 9.6, 10.14
    - `spam_burst_detector.observe` called on every group path (explicit/implicit/ambient) — Req 10.3
    - Burst-classified reply-to-bot still routes through the explicit path — Req 10.7
    - _Design: Testing Strategy (Example / unit tests). Requirements: 1.1, 1.6, 3.2, 3.4, 5.1, 5.2, 5.3, 5.4, 9.6, 10.3, 10.7, 10.14_

- [x] 8. Record the recency commit point and wire pruning (`app/services/user_task_manager.py`)
  - [x] 8.1 Call `note_bot_spoke` when a group reply is actually sent
    - Import `implicit_gate` (alongside the existing `ambient_gate`, `affinity_cache` imports)
    - In `_process_batch`, in the branch that performs `await last_message.answer(reply_text)` (the
      genuinely-sent case, not the suppressed ambient-empty case), when `chat_type in
      _GROUP_CHAT_TYPES` call `implicit_gate.note_bot_spoke(chat_id, time.time())` wrapped in a
      defensive `try/except` so a tracking failure never breaks delivery
    - _Design: Components A4 (Recency commit point). Requirements: 6.1_

  - [x] 8.2 Extend the idle sweep to prune the new trackers
    - In `_evict_idle`, inside the existing defensive prune `try/except` block, also call
      `implicit_gate.prune(now)` and `spam_burst_detector.prune(now)` (import the latter)
    - _Design: Components A5 (Pruning hook). Requirements: 6.4, 10.13_

  - [ ]* 8.3 Write tests for the commit point and prune hook
    - New file `tests/test_recency_commit.py`
    - A sent group reply calls `note_bot_spoke`; a suppressed ambient empty-reply does NOT — Req 6.1
    - `_evict_idle` invokes `implicit_gate.prune` and `spam_burst_detector.prune` — Req 6.4, 10.13
    - _Design: Testing Strategy (Example / unit tests). Requirements: 6.1, 6.4, 10.13_

- [~] 9. Checkpoint - Part A fully wired
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Part B: prompt changes
  - [x] 10.1 Add English memory normalization to the extraction prompt
    - In `app/prompts/extraction_prompt.py`, add a top-level **LANGUAGE NORMALIZATION** rule to
      `SYSTEM_EXTRACTION_PROMPT`: store every fact/belief/event in English regardless of
      conversation language; translate (not transliterate) non-English content to natural English;
      preserve proper nouns, personal/place/brand names, and quoted identifiers in their original
      form (example guidance: "मुझे पुणे में नौकरी मिली" → `"Got a job in Pune"`)
    - Reinforce in `_GROUP_EXTRACTION_NOTE`: each participant's stored memory is English while names
      stay original
    - _Design: Components B1 (Extraction prompt). Requirements: 7.1, 7.2, 7.3, 7.4_

  - [x] 10.2 Strengthen the language/script rule in the system prompt
    - In `app/prompts/system_prompt.py`, strengthen the **Language** bullet of
      `DEFAULT_SYSTEM_PROMPT_TEMPLATE`: reply in the user's current language; for Hindi, match the
      user's script — Hinglish → reply in Hinglish, Devanagari Hindi → reply in Devanagari; judge
      current language/script from recent conversation context (not one isolated message) and switch
      when recent usage shifts; add an explicit independence note that language/script matching
      affects the reply only and not how memories are stored
    - _Design: Components B2 (System prompt). Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

  - [ ]* 10.3 Write prompt-assertion tests for Part B
    - New file `tests/test_prompt_language.py`
    - Assert `SYSTEM_EXTRACTION_PROMPT` contains the English-normalization rule and the
      proper-noun-preservation clause, and `_GROUP_EXTRACTION_NOTE` reinforces English per
      participant — Req 7.1–7.4
    - Assert `DEFAULT_SYSTEM_PROMPT_TEMPLATE` contains the language-and-script matching rule, names
      the Hinglish vs Devanagari distinction, the "judge from recent context" clause, and the
      independence-from-memory note — Req 8.1–8.5
    - _Design: Testing Strategy (Prompt-assertion tests). Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5_

- [ ] 11. Documentation
  - [~] 11.1 Update `docs/development/group_chat.md` for the new behavior
    - Document the implicit-addressing flow (recency window + intervening-message bound, implicit
      cooldown), the mass-tag and greeting-burst spam protections, the spam-aware explicit-address
      rule (reply-to-bot survives spam), the recency commit point (`note_bot_spoke` on actual group
      send) and pruning, the preserved single-write invariant and unchanged DM behavior, and the new
      config knobs with their defaults
    - _Design: Overview, Components A2–A6, Data Models (config table). Requirements: 1, 2, 3, 4, 5, 6, 9, 10_

- [~] 12. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation sub-tasks are never optional.
- Each task references specific requirement clauses and design sections for traceability.
- Each of the 13 correctness properties is implemented as exactly one Hypothesis property test
  (min 100 iterations, e.g. `@settings(max_examples=100)`), in its own test file, tagged with a
  comment `# Feature: implicit-bot-addressing, Property {n}: {property text}`, placed close to the
  code it validates so errors surface early.
- Part B is LLM-driven and has no deterministic property to assert, so it is covered by
  prompt-assertion tests rather than property tests (optional live smoke tests are out of scope for
  the default suite).
- Tests are pure/synchronous and deterministic: a fresh `ImplicitAddressGate()` / `SpamBurstDetector()`
  per test, an injected `now`, and config overrides via set/restore in `try/finally`.
- The full existing suite (group routing, ambient gate, affinity, multi-party extraction) must stay
  green — no existing public contract (`is_addressed`, `AmbientGate`, `enqueue_message`,
  `handle_message`) changes.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1", "10.1", "10.2"] },
    { "id": 1, "tasks": ["1.2", "2.2", "10.3"] },
    { "id": 2, "tasks": ["2.3", "3.1"] },
    { "id": 3, "tasks": ["4.1", "3.2", "3.3", "3.4", "3.5", "3.6"] },
    { "id": 4, "tasks": ["4.2", "4.3", "5.1", "5.2", "7.1"] },
    { "id": 5, "tasks": ["7.2", "8.1"] },
    { "id": 6, "tasks": ["8.2", "7.3", "7.4", "7.5", "7.6", "7.7"] },
    { "id": 7, "tasks": ["8.3", "11.1"] }
  ]
}
```
