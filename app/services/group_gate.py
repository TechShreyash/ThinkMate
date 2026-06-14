"""Pure, no-LLM helpers for group-chat routing and the ambient gate (Phase 9).

This module holds the cheap, deterministic decision helpers that run on every
group message *before* any LLM call is considered:

- :func:`is_addressed` — does a message clearly talk to the bot (mention, name,
  or reply-to-bot)?
- :func:`scan_cheap_triggers` — regex/keyword scan for ambient-worthy moments
  (birthdays, congrats, laughter, group questions, greetings, strong sentiment).
- :func:`scan_negative_signal` — cheap "back off" detector (stop / quiet / spam /
  annoying / shut up) used as an affinity-down signal.

Everything here accepts plain values (``str``, ``bool``, simple objects) and has
**no aiogram or DB imports**, so it is directly unit-testable and side-effect
free. The stateful ambient funnel (``AmbientGate``) and the affinity cache
(``AffinityCache``) live in later tasks (4.1 and 5.1) and are intentionally not
implemented here.

All matching is case-insensitive and uses word boundaries where appropriate. The
helpers are defensive: malformed input never raises — it degrades to ``False``.
"""

from __future__ import annotations

import difflib
import random
import re
from collections import deque

from app.config import config

# ---------------------------------------------------------------------------
# Pre-compiled regexes (module-level for efficiency).
# ---------------------------------------------------------------------------

# Telegram entity types that represent a mention of a user/bot.
_MENTION_ENTITY_TYPES: frozenset[str] = frozenset({"mention", "text_mention"})

# Fallback @mention token matcher, used when entity offsets are absent or
# malformed so similarity comparison still excludes obvious @mentions.
_MENTION_TOKEN_RE: re.Pattern[str] = re.compile(r"@\w+")

# Cheap-trigger patterns. Each entry is a pre-compiled, case-insensitive regex.
# Kept conservative but reasonable; word boundaries avoid matching inside words.
_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Birthdays
    re.compile(r"\bhappy\s+birthday\b", re.IGNORECASE),
    re.compile(r"\bbday\b", re.IGNORECASE),
    # Congratulations
    re.compile(r"\bcongrats\b", re.IGNORECASE),
    re.compile(r"\bcongratulations\b", re.IGNORECASE),
    re.compile(r"\bwell\s+done\b", re.IGNORECASE),
    # Laughter (text forms)
    re.compile(r"\blol\b", re.IGNORECASE),
    re.compile(r"\blmao\b", re.IGNORECASE),
    re.compile(r"\bhaha\b", re.IGNORECASE),
    # Greetings
    re.compile(r"\bhi\b", re.IGNORECASE),
    re.compile(r"\bhello\b", re.IGNORECASE),
    re.compile(r"\bhey\b", re.IGNORECASE),
    re.compile(r"\bgood\s+(?:morning|evening)\b", re.IGNORECASE),
    re.compile(r"\bgm\b", re.IGNORECASE),
    # Group-question openers
    re.compile(
        r"^\s*(?:who|what|when|where|why|how|anyone|does\s+anyone)\b",
        re.IGNORECASE,
    ),
    # Strong sentiment
    re.compile(r"\bamazing\b", re.IGNORECASE),
    re.compile(r"\bawful\b", re.IGNORECASE),
    re.compile(r"\blove\s+it\b", re.IGNORECASE),
    re.compile(r"\bhate\b", re.IGNORECASE),
)

# Laughter emojis (matched as substrings, not word-bounded).
_LAUGH_EMOJIS: tuple[str, ...] = ("😂", "🤣")

# Negative / affinity-down keywords.
_NEGATIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bstop\b", re.IGNORECASE),
    re.compile(r"\bquiet\b", re.IGNORECASE),
    re.compile(r"\bspam\b", re.IGNORECASE),
    re.compile(r"\bannoying\b", re.IGNORECASE),
    # "shut up" or "shutup"
    re.compile(r"\bshut\s*up\b", re.IGNORECASE),
)


def _word_token_present(token: str, text: str) -> bool:
    """Return True if ``token`` appears as a standalone, case-insensitive word in ``text``.

    Uses a word-boundary match around the escaped token. Returns False on empty
    inputs. Never raises.
    """
    if not token or not text:
        return False
    try:
        pattern = re.compile(r"\b" + re.escape(token) + r"\b", re.IGNORECASE)
        return pattern.search(text) is not None
    except (re.error, TypeError):
        return False


def _mention_present(handle: str, text: str) -> bool:
    """Return True if ``@handle`` appears as a standalone @mention in ``text``.

    A leading ``\\b`` cannot be used because ``@`` is a non-word character, so we
    require that the ``@`` is not preceded by a word character or another ``@``,
    followed by the handle and a trailing word boundary. Case-insensitive. Never
    raises.
    """
    if not handle or not text:
        return False
    try:
        pattern = re.compile(
            r"(?<![\w@])@" + re.escape(handle) + r"\b", re.IGNORECASE
        )
        return pattern.search(text) is not None
    except (re.error, TypeError):
        return False


def is_addressed(
    *,
    text: str,
    entities,
    reply_to_bot: bool,
    bot_username: str,
    bot_name: str,
) -> bool:
    """Decide whether a group message addresses the bot.

    A message is *addressed* when any of the following hold:

    - ``reply_to_bot`` is True (the message replies to one of the bot's messages);
    - ``text`` contains an ``@mention`` of ``bot_username`` (case-insensitive);
    - ``bot_name`` appears as a standalone, word-boundary token in ``text``
      (case-insensitive).

    ``entities`` may be a list of Telegram ``MessageEntity``-like objects (each
    exposing ``.type``, ``.offset``, ``.length``) or ``None``. A ``mention`` /
    ``text_mention`` entity is used as an *additional* signal but is never
    required. Empty/None ``bot_username`` or ``bot_name`` simply skip that check.

    The function is fully defensive: any malformed input degrades to ``False``
    rather than raising.

    Args:
        text: The message text (may be empty or None).
        entities: Optional iterable of Telegram entity-like objects, or None.
        reply_to_bot: Whether the message replies to one of the bot's messages.
        bot_username: The bot's @username (without the leading ``@``).
        bot_name: The bot's configured display name.

    Returns:
        True if the message addresses the bot, otherwise False.
    """
    try:
        # (a) Reply to the bot is the strongest, cheapest signal.
        if reply_to_bot:
            return True

        safe_text = text if isinstance(text, str) else ""

        # (b) @mention of the bot's username (case-insensitive).
        if bot_username:
            handle = bot_username.lstrip("@")
            if handle and _mention_present(handle, safe_text):
                return True
            # Additional signal: a mention/text_mention entity present in the
            # message. This is supplementary and never required.
            if _has_mention_entity(entities, safe_text, handle):
                return True

        # (c) Bot name as a standalone token (word-boundary, case-insensitive).
        if bot_name and _word_token_present(bot_name, safe_text):
            return True

        return False
    except Exception:
        # Never raise on malformed input.
        return False


def _has_mention_entity(entities, text: str, handle: str) -> bool:
    """Return True if ``entities`` contains a mention of the bot.

    Tolerant of ``None``/empty and of objects missing the expected attributes.
    For a plain ``mention`` entity, the referenced slice of ``text`` is compared
    against ``@handle``; ``text_mention`` entities (which carry a user object) are
    treated as a positive signal only when their slice matches as well. Never
    raises.
    """
    if not entities:
        return False
    try:
        iterator = iter(entities)
    except TypeError:
        return False

    for entity in iterator:
        etype = getattr(entity, "type", None)
        if etype not in _MENTION_ENTITY_TYPES:
            continue
        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if not isinstance(offset, int) or not isinstance(length, int):
            continue
        if offset < 0 or length <= 0:
            continue
        fragment = text[offset:offset + length]
        if not handle:
            # No username to compare against; presence of a mention entity is
            # itself a weak positive signal.
            return True
        if fragment.lstrip("@").lower() == handle.lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Mass-tagging / spam scan helpers (Task 2.1 / 2.2): pure, no-LLM, defensive.
# ---------------------------------------------------------------------------


def count_distinct_mentions(text, entities) -> int:
    """Count the number of *distinct* participants a message @mentions.

    Two entity kinds contribute (Requirement 9.1):

    - ``mention`` entities (``@username``): distinct by the case-folded handle
      text sliced from ``text`` (the ``@`` stripped).
    - ``text_mention`` entities (inline name links carrying a ``user`` object):
      distinct by the carried ``user.id``.

    Tagging the same person twice counts once. The function is tolerant of
    ``None``/empty/malformed entities (it skips anything it cannot read) and
    never raises — returning ``0`` on total failure.

    Args:
        text: The message text (may be empty, None, or non-str).
        entities: Optional iterable of Telegram entity-like objects, or None.

    Returns:
        The count of distinct @mentioned participants.
    """
    try:
        if not entities:
            return 0
        safe_text = text if isinstance(text, str) else ""
        try:
            iterator = iter(entities)
        except TypeError:
            return 0

        handles: set[str] = set()
        user_ids: set[object] = set()
        for entity in iterator:
            etype = getattr(entity, "type", None)
            if etype not in _MENTION_ENTITY_TYPES:
                continue
            if etype == "text_mention":
                user = getattr(entity, "user", None)
                uid = getattr(user, "id", None)
                if uid is not None:
                    user_ids.add(uid)
                continue
            # Plain ``mention`` entity: distinct by sliced, case-folded handle.
            offset = getattr(entity, "offset", None)
            length = getattr(entity, "length", None)
            if not isinstance(offset, int) or not isinstance(length, int):
                continue
            if offset < 0 or length <= 0:
                continue
            fragment = safe_text[offset:offset + length].lstrip("@").casefold()
            if fragment:
                handles.add(fragment)
        return len(handles) + len(user_ids)
    except Exception:
        return 0


def is_mass_tag_spam(text, entities, *, threshold: int) -> bool:
    """Return True when a message @mentions *more than* ``threshold`` participants.

    Uses a strict ``>`` comparison (Requirement 9.1: "more than a configurable
    threshold"), so a threshold of 5 marks the 6th-and-beyond distinct mention as
    spam. The bot's own mention is **not** excluded from the count — a bulk tag
    that happens to sweep up the bot is exactly the spam case Requirement 9.4
    targets.

    Fully defensive: any internal error degrades to ``False`` ("not spam") so a
    classification bug can never suppress a legitimate reply (Requirement 9.6).

    Args:
        text: The message text (may be empty, None, or non-str).
        entities: Optional iterable of Telegram entity-like objects, or None.
        threshold: The distinct-mention count above which the message is spam.

    Returns:
        True if the message is mass-tag spam, otherwise False.
    """
    try:
        return count_distinct_mentions(text, entities) > threshold
    except Exception:
        return False


def is_directed_at_other(*, entities, reply_to_other: bool) -> bool:
    """Return True when a non-explicitly-addressed message targets another participant.

    Because this predicate is only consulted for messages that already failed
    :func:`is_addressed`, any mention entity present must reference a *non-bot*
    participant, and any reply present must be to a non-bot message. The message
    is therefore Directed_At_Other when either holds (Requirement 2.1):

    - ``reply_to_other`` is True — the message replies to a non-bot message
      (Requirement 2.2);
    - the message carries a ``mention`` / ``text_mention`` entity, i.e. an
      @mention of another user (Requirement 2.3).

    Fully defensive: malformed input degrades to ``False`` (never raises).

    Args:
        entities: Optional iterable of Telegram entity-like objects, or None.
        reply_to_other: Whether the message replies to a non-bot message.

    Returns:
        True if the message is clearly aimed at another participant.
    """
    try:
        if reply_to_other:
            return True
        # An empty handle makes ``_has_mention_entity`` a pure "is any mention
        # entity present?" scan, which is exactly what we need here.
        return _has_mention_entity(entities, "", "")
    except Exception:
        return False


def scan_cheap_triggers(text: str) -> bool:
    """Cheap, no-LLM scan for ambient-worthy moments.

    Returns True when ``text`` looks like a moment worth (maybe) chiming in on:
    birthdays, congratulations, laughter, group questions, greetings, or strong
    sentiment. Matching is case-insensitive and word-bounded where appropriate.
    Conservative but reasonable; returns False for anything that doesn't clearly
    match. Never raises.

    Args:
        text: The message text (may be empty or None).

    Returns:
        True if an ambient trigger is detected, otherwise False.
    """
    if not text or not isinstance(text, str):
        return False
    try:
        # Laughter emojis (substring match).
        for emoji in _LAUGH_EMOJIS:
            if emoji in text:
                return True

        # Keyword / pattern triggers.
        for pattern in _TRIGGER_PATTERNS:
            if pattern.search(text):
                return True

        # Group question: ends with a question mark.
        if text.rstrip().endswith("?"):
            return True

        # Exclamation-heavy strong sentiment.
        if "!!" in text or text.count("!") >= 2:
            return True

        return False
    except Exception:
        return False


def scan_negative_signal(text: str) -> bool:
    """Cheap, no-LLM detector for "back off" / affinity-down moments.

    Returns True when ``text`` matches any of the negative keywords: ``stop``,
    ``quiet``, ``spam``, ``annoying``, or ``shut up`` (also ``shutup``). Matching
    is case-insensitive and word-bounded. Never raises.

    Args:
        text: The message text (may be empty or None).

    Returns:
        True if a negative signal is detected, otherwise False.
    """
    if not text or not isinstance(text, str):
        return False
    try:
        return any(pattern.search(text) for pattern in _NEGATIVE_PATTERNS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ambient gate (Task 4.1): the no-LLM funnel that decides whether to chime in
# on a non-addressed group message.
# ---------------------------------------------------------------------------

# mode -> probability multiplier. ``quiet`` hard-zeroes the ambient probability
# (Requirement 3.5); ``chatty`` boosts it above 1 (Requirement 6.2).
_MODE_FACTORS: dict[str, float] = {
    "quiet": 0.0,
    "auto": 1.0,
    "chatty": 1.5,
}


class AmbientGate:
    """Per-chat, no-LLM funnel for ambient group chime-ins.

    The gate is a pure, deterministic decision helper holding a small amount of
    in-memory per-chat state. It performs three cheap checks — **cooldown →
    trigger/scan-tick → affinity-weighted dice roll** — none of which touch the
    LLM or the database. Only candidates that pass all three are eligible for a
    single chime-in LLM call (made by the caller).

    State (all keyed by ``chat_id``):

    - ``_last_chime_time``: the ``now`` value passed to :meth:`mark_chimed` when
      the last ambient chime-in was dispatched, used to enforce the per-chat
      cooldown window (``GROUP_AMBIENT_COOLDOWN_SECS``). A chat with **no** entry
      has never chimed and is therefore treated as "cooldown elapsed".
    - ``_msg_counter``: a per-chat counter advanced once per candidate that
      survives the cooldown check; used to fire a periodic context-scan tick
      every ``GROUP_CONTEXT_SCAN_EVERY`` messages.
    - ``_last_seen``: the most recent ``now`` observed for the chat (updated on
      every :meth:`should_chime` call), used by :meth:`prune` to bound the maps.

    Important contract: :meth:`should_chime` does **not** reset the cooldown. The
    caller is responsible for calling :meth:`mark_chimed` *after* actually
    dispatching a chime-in, so that even a failed/empty model reply still resets
    the cooldown (Requirement 3.7) at the call site (Task 4.2). This keeps
    :meth:`should_chime` a side-effect-light predicate (it only advances the
    message counter and activity clock) and leaves the budget-enforcing reset to
    the dispatch path.

    All tuning knobs are read live from :data:`config` (Requirement 7.1). ``now``
    and ``rng`` are injectable so tests are fully deterministic.
    """

    def __init__(self) -> None:
        self._last_chime_time: dict[int, float] = {}
        self._msg_counter: dict[int, int] = {}
        self._last_seen: dict[int, float] = {}
        self._last_prune: float = 0.0

    def decide(
        self,
        chat_id: int,
        *,
        affinity: float,
        mode: str,
        triggered: bool,
        now: float,
        rng=None,
    ) -> tuple[bool, str]:
        """Run the ambient funnel and return ``(should_chime, stage)``.

        ``stage`` names the funnel step that produced the outcome, so the caller
        can emit per-stage drop logging (Requirement 7.2) without re-deriving the
        decision. It is one of:

        - ``"cooldown"`` — dropped because the per-chat cooldown has not elapsed;
        - ``"no_trigger"`` — dropped because neither a cheap trigger nor the
          periodic context-scan tick matched;
        - ``"dice"`` — dropped at the affinity-weighted dice roll (this also
          covers ``quiet`` mode, whose probability is hard-zeroed);
        - ``"pass"`` — survived all three checks and is eligible for a single
          chime-in LLM call.

        The funnel, in order (each step is no-LLM):

        1. **Cooldown.** If the chat has chimed before and ``now`` is still
           within ``GROUP_AMBIENT_COOLDOWN_SECS`` of the last chime, stop at
           ``"cooldown"``. A chat with no recorded chime (first ever message) is
           treated as cooldown-elapsed.
        2. **Trigger / scan tick.** Advance the per-chat message counter and
           compute ``scan_tick = counter % GROUP_CONTEXT_SCAN_EVERY == 0``. The
           candidate passes this step if the caller-supplied ``triggered`` (a
           cheap-trigger hit) is True OR ``scan_tick`` is True; otherwise stop at
           ``"no_trigger"``.
        3. **Affinity-weighted dice roll.** Compute
           ``p = GROUP_AMBIENT_BASE_RATE * affinity * mode_factor`` (mode_factor:
           ``quiet`` → 0, ``auto`` → 1, ``chatty`` → 1.5), clamped to [0, 1]. If
           ``p <= 0`` (covers ``quiet``), stop at ``"dice"``. Otherwise draw a
           single ``r`` from ``rng.random()`` (defaulting to the module's
           :func:`random.random`); return ``(True, "pass")`` when ``r < p`` and
           ``(False, "dice")`` otherwise.

        This method never resets the cooldown — see the class docstring and call
        :meth:`mark_chimed` after dispatch.

        The decision logic and ordering are identical to the historical
        :meth:`should_chime`; ``decide`` only additionally surfaces the stage.

        Args:
            chat_id: The group chat id.
            affinity: The speaker's affinity in [0, 1].
            mode: The speaker's mode (``auto`` / ``quiet`` / ``chatty``).
            triggered: Whether a cheap trigger already matched (from the caller).
            now: The current time (seconds); injectable for deterministic tests.
            rng: Optional object exposing ``.random() -> float`` in [0, 1);
                defaults to the module's :func:`random.random`.

        Returns:
            A ``(should_chime, stage)`` tuple.
        """
        # Record activity for prune() bookkeeping (even when we ultimately stop).
        self._last_seen[chat_id] = now

        # 1. Cooldown — cheapest check, runs first. Absence = never chimed = elapsed.
        last = self._last_chime_time.get(chat_id)
        if last is not None and (now - last) < config.GROUP_AMBIENT_COOLDOWN_SECS:
            return False, "cooldown"

        # 2. Trigger / periodic context-scan tick.
        counter = self._msg_counter.get(chat_id, 0) + 1
        self._msg_counter[chat_id] = counter
        scan_every = config.GROUP_CONTEXT_SCAN_EVERY
        scan_tick = scan_every > 0 and (counter % scan_every == 0)
        if not (triggered or scan_tick):
            return False, "no_trigger"

        # 3. Affinity-weighted dice roll.
        mode_factor = _MODE_FACTORS.get(mode, 1.0)
        p = config.GROUP_AMBIENT_BASE_RATE * affinity * mode_factor
        # Clamp to [0, 1].
        p = max(0.0, min(1.0, p))
        if p <= 0.0:
            return False, "dice"

        draw = rng.random() if rng is not None else random.random()
        if draw < p:
            return True, "pass"
        return False, "dice"

    def should_chime(
        self,
        chat_id: int,
        *,
        affinity: float,
        mode: str,
        triggered: bool,
        now: float,
        rng=None,
    ) -> bool:
        """Return True iff this non-addressed message should trigger a chime-in.

        Thin wrapper around :meth:`decide` that discards the stage, preserved so
        existing callers and tests that only need the boolean keep working. See
        :meth:`decide` for the full funnel semantics.
        """
        chime, _stage = self.decide(
            chat_id,
            affinity=affinity,
            mode=mode,
            triggered=triggered,
            now=now,
            rng=rng,
        )
        return chime

    def mark_chimed(self, chat_id: int, now: float) -> None:
        """Record that an ambient chime-in was dispatched for ``chat_id`` at ``now``.

        Called by the dispatch path after an ambient chime-in is sent (or
        attempted) so the per-chat cooldown holds for at least
        ``GROUP_AMBIENT_COOLDOWN_SECS`` — even when the model returned nothing
        (Requirement 3.7).
        """
        self._last_chime_time[chat_id] = now
        # Keep the activity clock consistent with the chime.
        self._last_seen[chat_id] = now

    def prune(self, now: float, max_idle: float | None = None) -> int:
        """Drop stale per-chat state so the maps stay bounded (Requirement 3.10).

        An entry is considered stale when the chat has had no activity (no
        :meth:`should_chime` / :meth:`mark_chimed` call) within ``max_idle``
        seconds. Because activity is recorded on every call and a chime always
        updates the activity clock, this covers both the "last chime is old" and
        "counter activity is stale" conditions in one idle check — mirroring the
        throttle-map pruning philosophy in ``middlewares.py``.

        Args:
            now: The current time (seconds).
            max_idle: Idle horizon; defaults to ``10 × GROUP_AMBIENT_COOLDOWN_SECS``.

        Returns:
            The number of chats pruned.
        """
        if max_idle is None:
            max_idle = 10.0 * config.GROUP_AMBIENT_COOLDOWN_SECS
        cutoff = now - max_idle
        stale = [cid for cid, seen in self._last_seen.items() if seen <= cutoff]
        for cid in stale:
            self._last_seen.pop(cid, None)
            self._last_chime_time.pop(cid, None)
            self._msg_counter.pop(cid, None)
        self._last_prune = now
        return len(stale)


# Module-level singleton for the application hot path. Tests should instantiate
# ``AmbientGate()`` directly to keep state isolated and deterministic.
ambient_gate = AmbientGate()


# ---------------------------------------------------------------------------
# Implicit-address gate (Task 3.1): per-chat recent-activity tracking and the
# implicit-reply throttle. Mirrors AmbientGate's shape (bounded in-memory maps
# keyed by chat_id, injectable now, decision/commit split, prune hook).
# ---------------------------------------------------------------------------


class ImplicitAddressGate:
    """Per-chat, no-LLM gate that judges whether a non-addressed message is
    implicitly directed at the bot.

    It holds the bot's recent-activity tracking and the implicit-reply throttle.
    Like :class:`AmbientGate` it keeps bounded in-memory per-chat state keyed by
    ``chat_id``, takes an injectable ``now`` for deterministic tests, separates
    the pure :meth:`decide` predicate from the :meth:`mark_implicit_reply`
    commit, and exposes :meth:`prune` for the idle sweep.

    State (all keyed by ``chat_id``):

    - ``_bot_last_spoke``: ``now`` when the bot last sent a message in the chat;
      absent means the bot has never spoken there.
    - ``_human_since_bot``: count of human messages observed since the bot's most
      recent message (reset to 0 by :meth:`note_bot_spoke`).
    - ``_last_implicit_reply``: ``now`` when the bot last issued an implicit
      direct reply (for the Implicit_Cooldown); absent means never.
    - ``_last_seen``: most recent ``now`` observed for the chat; used by
      :meth:`prune`.

    All tuning knobs are read live from :data:`config` (Requirement 1.5/4.2/4.3).
    """

    def __init__(self) -> None:
        self._bot_last_spoke: dict[int, float] = {}
        self._human_since_bot: dict[int, int] = {}
        self._last_implicit_reply: dict[int, float] = {}
        self._last_seen: dict[int, float] = {}
        self._last_prune: float = 0.0

    def note_bot_spoke(self, chat_id: int, now: float) -> None:
        """Record that the bot just spoke in ``chat_id`` at ``now``.

        Sets the last-spoke time and resets the since-bot human counter to 0, so
        the Bot_Recency_Window reopens from the bot's latest message
        (Requirement 6.1).
        """
        self._bot_last_spoke[chat_id] = now
        self._human_since_bot[chat_id] = 0
        self._last_seen[chat_id] = now

    def note_human_message(self, chat_id: int, now: float) -> int:
        """Record a human message and return the since-bot counter.

        The counter only advances once the bot has spoken in the chat (there is
        no window to fill before the bot's first message — Requirement 6.2).
        Always updates the activity clock for :meth:`prune`.
        """
        self._last_seen[chat_id] = now
        if chat_id not in self._bot_last_spoke:
            return 0
        count = self._human_since_bot.get(chat_id, 0) + 1
        self._human_since_bot[chat_id] = count
        return count

    def decide(
        self,
        chat_id: int,
        *,
        directed_at_other: bool,
        is_spam: bool,
        now: float,
    ) -> tuple[bool, str]:
        """Classify a non-explicitly-addressed message; return ``(is_implicit, reason)``.

        Pure predicate: it does **not** mutate state and never raises (malformed
        input degrades to ``(False, "out_of_window")``). The current message is
        NOT yet counted among the intervening human messages — the router calls
        :meth:`note_human_message` *after* this.

        Decision order:

        1. ``is_spam`` → ``(False, "spam")`` — checked first so neither spam
           shape is ever implicit, even inside the recency window (Req 9.2, 10.4).
        2. bot has never spoken → ``(False, "no_bot_activity")`` (Req 1.4).
        3. ``directed_at_other`` → ``(False, "directed_at_other")`` (Req 2.1).
        4. window check — within the Bot_Recency_Window when **both** bounds hold
           (Req 1.2/1.3):
           ``elapsed = now - _bot_last_spoke <= GROUP_IMPLICIT_RECENCY_SECS`` AND
           ``intervening = _human_since_bot <= GROUP_IMPLICIT_RECENCY_MAX_MSGS``
           → ``(True, "implicit")`` else ``(False, "out_of_window")``.
        """
        try:
            if is_spam:
                return False, "spam"
            last_spoke = self._bot_last_spoke.get(chat_id)
            if last_spoke is None:
                return False, "no_bot_activity"
            if directed_at_other:
                return False, "directed_at_other"
            elapsed = now - last_spoke
            intervening = self._human_since_bot.get(chat_id, 0)
            within = (
                elapsed <= config.GROUP_IMPLICIT_RECENCY_SECS
                and intervening <= config.GROUP_IMPLICIT_RECENCY_MAX_MSGS
            )
            if within:
                return True, "implicit"
            return False, "out_of_window"
        except Exception:
            # Never raise: degrade to the safe default (not implicit → ambient).
            return False, "out_of_window"

    def cooldown_elapsed(self, chat_id: int, now: float) -> bool:
        """Return True when the Implicit_Cooldown has elapsed for ``chat_id``.

        A chat that has never issued an implicit reply is treated as
        cooldown-elapsed. Otherwise the cooldown holds for
        ``GROUP_IMPLICIT_COOLDOWN_SECS`` after the last implicit reply
        (Requirement 4.1).
        """
        last = self._last_implicit_reply.get(chat_id)
        if last is None:
            return True
        return (now - last) >= config.GROUP_IMPLICIT_COOLDOWN_SECS

    def mark_implicit_reply(self, chat_id: int, now: float) -> None:
        """Record that an implicit direct reply was dispatched (resets the cooldown).

        Called by the router *before* enqueueing so the Implicit_Cooldown holds
        even if the eventual reply is empty/fails (Requirement 3.3).
        """
        self._last_implicit_reply[chat_id] = now
        self._last_seen[chat_id] = now

    def prune(self, now: float, max_idle: float | None = None) -> int:
        """Drop stale per-chat state so the maps stay bounded (Requirement 6.4).

        An entry is stale when the chat has had no activity within ``max_idle``
        seconds. Defaults to ``10 × GROUP_IMPLICIT_RECENCY_SECS`` (mirroring the
        idle-horizon philosophy of :meth:`AmbientGate.prune`).

        Returns the number of chats pruned.
        """
        if max_idle is None:
            max_idle = 10.0 * config.GROUP_IMPLICIT_RECENCY_SECS
        cutoff = now - max_idle
        stale = [cid for cid, seen in self._last_seen.items() if seen <= cutoff]
        for cid in stale:
            self._last_seen.pop(cid, None)
            self._bot_last_spoke.pop(cid, None)
            self._human_since_bot.pop(cid, None)
            self._last_implicit_reply.pop(cid, None)
        self._last_prune = now
        return len(stale)


# Module-level singleton for the hot path. Tests instantiate ImplicitAddressGate()
# directly for isolated, deterministic state.
implicit_gate = ImplicitAddressGate()


# ---------------------------------------------------------------------------
# Greeting-burst spam detector (Task 4.1): stateful, no-LLM. Tracks a short,
# bounded, time-windowed history of mention-stripped message contents per chat
# and flags near-identical greeting bursts (Requirement 10).
# ---------------------------------------------------------------------------


class SpamBurstDetector:
    """Per-chat, no-LLM, **stateful** detector for time-distributed greeting bursts.

    It remembers a short, bounded, time-windowed history of recent
    mention-stripped message contents **per sender within a chat** and flags a
    message as Greeting_Burst_Spam once enough near-identical messages from the
    *same* sender have arrived within the window. Keying by ``(chat_id,
    user_id)`` means one member's repetition can never cause another member's
    message to be classified as spam — which also matches the real threat: a
    single userbot tagging members one-by-one. It mirrors :class:`AmbientGate`'s
    shape: per-key ``dict`` state, injectable ``now``, a :meth:`prune` method for
    the idle sweep, and a fully defensive contract.

    State (keyed by the ``(chat_id, user_id)`` pair):

    - ``_recent``: a :class:`collections.deque` of
      ``(arrival_time, mention_stripped_content)`` pairs, evicted when older than
      ``GROUP_SPAM_BURST_WINDOW_SECS`` and hard-capped at
      ``GROUP_SPAM_BURST_TRACK_MAX`` entries.
    - ``_last_seen``: most recent ``now`` observed for the sender; used by
      :meth:`prune`.

    Similarity uses the standard-library
    ``difflib.SequenceMatcher(None, a, b).ratio()`` (deterministic, dependency
    free, in ``[0, 1]``); two contents are *near-identical* when the ratio meets
    or exceeds ``GROUP_SPAM_BURST_SIMILARITY`` (Requirement 10.2). All knobs are
    read live from :data:`config` (Requirement 10.11/10.12).
    """

    def __init__(self) -> None:
        self._recent: dict[tuple[int, int | None], deque[tuple[float, str]]] = {}
        self._last_seen: dict[tuple[int, int | None], float] = {}
        self._last_prune: float = 0.0

    @staticmethod
    def _strip_and_normalize(text, entities) -> str:
        """Return the mention-stripped, case-folded, whitespace-collapsed content.

        Removes ``mention``/``text_mention`` entity slices from ``text`` (so
        "hi @alice" and "hi @bob" both reduce to "hi" — Requirement 10.1), then
        applies an ``@\\w+`` regex fallback to strip any leftover @tokens (covers
        absent/malformed entities), then casefolds and collapses whitespace.
        """
        safe_text = text if isinstance(text, str) else ""
        # Collect valid entity slices to remove.
        ranges: list[tuple[int, int]] = []
        if entities:
            try:
                iterator = iter(entities)
            except TypeError:
                iterator = iter(())
            for entity in iterator:
                etype = getattr(entity, "type", None)
                if etype not in _MENTION_ENTITY_TYPES:
                    continue
                offset = getattr(entity, "offset", None)
                length = getattr(entity, "length", None)
                if not isinstance(offset, int) or not isinstance(length, int):
                    continue
                if offset < 0 or length <= 0:
                    continue
                ranges.append((offset, offset + length))
        if ranges:
            ranges.sort()
            kept: list[str] = []
            cursor = 0
            for start, end in ranges:
                if start > cursor:
                    kept.append(safe_text[cursor:start])
                cursor = max(cursor, end)
            if cursor < len(safe_text):
                kept.append(safe_text[cursor:])
            stripped = " ".join(kept)
        else:
            stripped = safe_text
        # Fallback / cleanup: drop any remaining @mention tokens.
        stripped = _MENTION_TOKEN_RE.sub(" ", stripped)
        # Casefold + collapse whitespace.
        return " ".join(stripped.casefold().split())

    def observe(self, chat_id: int, text, entities, now: float, user_id: int | None = None) -> bool:
        """Record a message and classify it as Greeting_Burst_Spam (True) or not.

        History is tracked per ``(chat_id, user_id)`` so a burst is only flagged
        when the **same sender** repeats near-identical messages; one member's
        repetition never suppresses another member's message. ``user_id`` is
        optional (defaults to ``None``) for callers/tests that don't track a
        sender — those share a single per-chat bucket as before.

        Steps (no LLM — Requirement 10.10):

        1. Strip @mention tokens and normalize → ``content`` (Req 10.1).
        2. Evict this sender's history entries older than
           ``GROUP_SPAM_BURST_WINDOW_SECS`` (now-anchored).
        3. Count retained entries near-identical to ``content`` (ratio ≥
           ``GROUP_SPAM_BURST_SIMILARITY`` — Req 10.2).
        4. Append ``(now, content)`` (hard-capped at
           ``GROUP_SPAM_BURST_TRACK_MAX``).
        5. Including the just-added message, return ``True`` when the
           near-identical count reaches ``GROUP_SPAM_BURST_COUNT`` (Req 10.3);
           a lone/sub-threshold greeting returns ``False`` (Req 10.8).

        Fully defensive: any internal error degrades to ``False`` ("not burst")
        so a classification bug can never suppress a legitimate reply
        (Requirement 10.14). Updates ``_last_seen``.
        """
        try:
            key = (chat_id, user_id)
            self._last_seen[key] = now
            content = self._strip_and_normalize(text, entities)

            window = config.GROUP_SPAM_BURST_WINDOW_SECS
            track_max = config.GROUP_SPAM_BURST_TRACK_MAX
            similarity = config.GROUP_SPAM_BURST_SIMILARITY
            burst_count = config.GROUP_SPAM_BURST_COUNT

            maxlen = track_max if isinstance(track_max, int) and track_max > 0 else None
            history = self._recent.get(key)
            if history is None:
                history = deque(maxlen=maxlen)
                self._recent[key] = history

            # 2. Evict entries older than the window (now-anchored).
            cutoff = now - window
            retained = [(t, c) for (t, c) in history if t >= cutoff]

            # 3. Count retained entries near-identical to the current content.
            near = 0
            for _t, prior in retained:
                ratio = difflib.SequenceMatcher(None, content, prior).ratio()
                if ratio >= similarity:
                    near += 1

            # 4. Append the current message and re-bound the history.
            retained.append((now, content))
            self._recent[key] = deque(retained, maxlen=maxlen)

            # 5. Including the just-added message, threshold the burst count.
            return (near + 1) >= burst_count
        except Exception:
            return False

    def prune(self, now: float, max_idle: float | None = None) -> int:
        """Drop per-sender history idle beyond ``max_idle``; return count pruned (Req 10.13).

        Defaults to ``10 × GROUP_SPAM_BURST_WINDOW_SECS``.
        """
        if max_idle is None:
            max_idle = 10.0 * config.GROUP_SPAM_BURST_WINDOW_SECS
        cutoff = now - max_idle
        stale = [key for key, seen in self._last_seen.items() if seen <= cutoff]
        for key in stale:
            self._last_seen.pop(key, None)
            self._recent.pop(key, None)
        self._last_prune = now
        return len(stale)


# Module-level singleton for the hot path. Tests instantiate SpamBurstDetector()
# directly for isolated, deterministic state.
spam_burst_detector = SpamBurstDetector()
