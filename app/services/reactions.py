"""Telegram emoji-reaction whitelist and tolerant normalization.

Telegram only accepts reactions from a fixed set of emojis, and is strict about exact
code points (e.g. it wants ``❤`` without the U+FE0F variation selector, but an LLM will
happily return ``❤️``). ``normalize_reaction`` maps a model's free-form guess onto a
canonical accepted emoji, or returns ``None`` when nothing valid was produced.

Note: this is only about the *reaction* applied to the user's message. The bot's reply text
may itself contain emojis (per the persona) — that is a separate, independent choice.
"""

# Canonical set of emojis Telegram accepts as message reactions.
ALLOWED_REACTIONS: frozenset[str] = frozenset({
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩",
    "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴",
    "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡",
    "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎",
    "👾", "🤷‍♂", "🤷", "🤷‍♀", "😡",
})

_VARIATION_SELECTOR = "️"

# FE0F-insensitive lookup: stripped form -> canonical accepted form.
_NORMALIZED_LOOKUP: dict[str, str] = {
    r.replace(_VARIATION_SELECTOR, ""): r for r in ALLOWED_REACTIONS
}


def normalize_reaction(candidate: str | None) -> str | None:
    """Return a Telegram-accepted emoji for ``candidate``, or ``None`` if there's no match."""
    if not candidate:
        return None
    cleaned = candidate.strip().strip("\"'").strip()
    if not cleaned or cleaned.lower() == "none":
        return None
    if cleaned in ALLOWED_REACTIONS:
        return cleaned
    stripped = cleaned.replace(_VARIATION_SELECTOR, "")
    return _NORMALIZED_LOOKUP.get(stripped)
