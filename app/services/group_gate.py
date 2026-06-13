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

import random
import re

from app.config import config

# ---------------------------------------------------------------------------
# Pre-compiled regexes (module-level for efficiency).
# ---------------------------------------------------------------------------

# Telegram entity types that represent a mention of a user/bot.
_MENTION_ENTITY_TYPES: frozenset[str] = frozenset({"mention", "text_mention"})

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
