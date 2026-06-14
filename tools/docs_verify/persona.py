"""Persona semantic-preservation checks for the documentation rewrite.

``persona.md`` influences the bot's runtime behaviour, so it is quarantined
from editorial rewriting: only typographical and formatting corrections are
allowed. To enforce that mechanically we reduce a persona document to its
*meaningful token sequence* and compare the before/after versions.

The normalization deliberately discards the things a typo/formatting fix is
allowed to change while keeping everything that defines tone, rules, and
traits:

- **Case** is folded, so capitalisation fixes (``i`` -> ``I``) are invisible.
- **Whitespace** is collapsed, so re-wrapping a paragraph or fixing double
  spaces does not register as a change.
- **Punctuation and Markdown formatting glyphs** (``*``, ``_``, ``#``, ``-``,
  quotes, commas, ...) are dropped, so emphasising a word (``rule`` ->
  ``**rule**``) or fixing a stray bullet marker is invisible.

What survives is the ordered stream of word tokens — letters and digits,
including non-ASCII letters — which is exactly the meaningful content. If two
versions produce the same token stream they are considered semantically
identical.

This mirrors the ``normalize_persona`` / ``check_persona_preserved`` interfaces
in the *Components and Interfaces* section of the ``docs-rewrite`` design and
backs **Property 8: Persona semantic preservation**.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_persona",
    "check_persona_preserved",
]

# A "word" token is a maximal run of Unicode word characters with underscores
# treated as separators. Underscores are excluded so that Markdown emphasis
# such as ``_tone_`` normalises to the same token as ``tone``. Everything that
# is not a letter or digit (whitespace, punctuation, formatting glyphs) acts as
# a delimiter and is therefore collapsed away.
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_persona(text: str) -> list[str]:
    """Reduce persona text to its lowercased, punctuation-free token stream.

    The returned list is the ordered sequence of word tokens (letters and
    digits, Unicode-aware) found in ``text``, lowercased. Whitespace,
    punctuation, and Markdown formatting characters are collapsed away because
    they sit between tokens and are never captured.

    Args:
        text: The raw persona document content.

    Returns:
        The ordered list of normalized word tokens. An empty or
        punctuation-only input yields an empty list.
    """
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def check_persona_preserved(original: str, rewritten: str) -> bool:
    """Return whether a persona rewrite preserved its meaningful content.

    Two persona versions are considered equivalent when their normalized token
    streams are identical, i.e. the rewrite only changed whitespace,
    punctuation, case, or Markdown formatting — never the wording that defines
    tone, rules, or traits.

    Args:
        original: The baseline persona content (before the rewrite).
        rewritten: The candidate persona content (after the rewrite).

    Returns:
        ``True`` if ``normalize_persona(original) == normalize_persona(rewritten)``,
        otherwise ``False``.
    """
    return normalize_persona(original) == normalize_persona(rewritten)
