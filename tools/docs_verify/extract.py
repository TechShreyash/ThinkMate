"""Markdown extraction for the documentation verification layer.

This module turns the raw text of a Markdown file into the structured
:class:`~tools.docs_verify.models.FileInventory` used by the preservation and
link-integrity checks, mirroring the *Components and Interfaces* section of the
``docs-rewrite`` design document.

The two public entry points are:

- :func:`extract_inventory` — parse a whole file into a ``FileInventory``
  (H1 title, intro presence, headings + GitHub slugs, emoji headers, code and
  mermaid blocks, table rows, inline backticked tokens, and links).
- :func:`extract_technical_tokens` — return just the technical payload
  (code blocks, mermaid blocks, table rows, inline tokens) as a
  :class:`TechnicalContent`.

A small helper, :func:`github_slug`, implements the GitHub anchor-slug rule
(lowercase, punctuation/emoji stripped, spaces -> hyphens) so anchored
cross-links can be resolved against heading slugs.

Parsing is deliberately line-oriented and fence-aware: anything inside a fenced
code block (```` ``` ````- or ``~~~``-delimited) is treated as verbatim content
and is *not* scanned for headings, tables, inline tokens, or links. This keeps,
for example, a ``#`` comment inside a Python block from being mistaken for a
Markdown heading.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from tools.docs_verify.models import FileInventory, Heading, Link

__all__ = [
    "TechnicalContent",
    "github_slug",
    "extract_inventory",
    "extract_technical_tokens",
]


# --- regexes -----------------------------------------------------------------

# Opening/closing fence: optional indent, then >=3 backticks or >=3 tildes,
# followed by an optional info string (e.g. ``python`` or ``mermaid``).
_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# ATX heading: 1-6 '#', at least one space, then the heading text.
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<text>.+?)[ \t]*#*[ \t]*$")

# Horizontal rule made of ---, ***, or ___ (three or more).
_HR_RE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")

# A table delimiter row, e.g. ``| --- | :--: |`` or ``---|:--``.
_TABLE_DELIM_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$"
)

# Inline code span delimited by one or more backticks. The closing run must
# match the opening run length (handled in code, not the regex).
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")

# Inline Markdown link ``[text](target)``. A leading ``!`` (image) is excluded
# via a negative lookbehind so images are not treated as navigation links.
_LINK_RE = re.compile(r"(?<!\!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:[ \t]+\"[^\"]*\")?\)")

# Whitespace used when replacing spaces in slugs.
_WS_RE = re.compile(r"\s")

# Characters stripped by the GitHub slug rule: anything that is not a Unicode
# word character, whitespace, or hyphen (this removes emoji, em-dashes,
# variation selectors, quotes, parentheses, asterisks, etc.).
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]", re.UNICODE)


# Emoji code-point ranges used to decide whether a header is "emoji-prefixed".
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x2190, 0x21FF),   # arrows
    (0x2300, 0x23FF),   # misc technical (⏰ U+23F0, ⌚, ⏳ ...)
    (0x2460, 0x24FF),   # enclosed alphanumerics
    (0x2500, 0x25FF),   # box drawing / geometric shapes
    (0x2600, 0x26FF),   # misc symbols (☀, ⚙ ...)
    (0x2700, 0x27BF),   # dingbats (✅, ✨ ...)
    (0x2B00, 0x2BFF),   # misc symbols & arrows (⭐ U+2B50 ...)
    (0x1F000, 0x1FAFF), # emoticons, pictographs, transport, supplemental, etc.
    (0xFE00, 0xFE0F),   # variation selectors
    (0x1F1E6, 0x1F1FF), # regional indicators / flags
)


@dataclass
class TechnicalContent:
    """The technical payload of a document, used by preservation checks."""

    code_blocks: list[str] = field(default_factory=list)
    mermaid_blocks: list[str] = field(default_factory=list)
    table_rows: list[str] = field(default_factory=list)
    inline_tokens: Counter[str] = field(default_factory=Counter)


def _is_emoji_char(ch: str) -> bool:
    """Return ``True`` if ``ch`` is an emoji/pictograph code point."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def github_slug(text: str) -> str:
    """Compute the GitHub anchor slug for a heading ``text``.

    The rule (matching GitHub's renderer / ``github-slugger``) is:

    1. Lowercase the text.
    2. Remove every character that is not a Unicode word character, whitespace,
       or hyphen — this strips emoji, variation selectors, em-dashes, quotes,
       parentheses, asterisks, and other punctuation.
    3. Replace each whitespace character with a single hyphen (consecutive
       spaces therefore produce consecutive hyphens; they are not collapsed).

    Leading/trailing hyphens are intentionally preserved, because an
    emoji-prefixed header such as ``🌙 Phase 11`` keeps the space left behind by
    the removed emoji and slugifies to ``-phase-11``.

    Note:
        This function returns the *base* slug. Disambiguation of duplicate
        headings (GitHub appends ``-1``, ``-2`` ...) is applied by
        :func:`extract_inventory`, which has the whole-document context needed
        to count repeats.
    """
    lowered = text.lower()
    stripped = _SLUG_STRIP_RE.sub("", lowered)
    return _WS_RE.sub("-", stripped)


def _strip_inline_markup(text: str) -> str:
    """Remove emphasis/code markup so the leading glyph of a header is visible."""
    # Drop leading backticks/emphasis markers that would hide an emoji.
    return text.lstrip(" \t*_`~")


def _header_is_emoji_prefixed(text: str) -> bool:
    """Return ``True`` if the first visible glyph of a header is an emoji."""
    visible = _strip_inline_markup(text)
    if not visible:
        return False
    return _is_emoji_char(visible[0])


def _parse_target(raw_target: str) -> Link:
    """Split a raw link target into a :class:`Link`."""
    raw = raw_target.strip()
    lowered = raw.lower()
    if lowered.startswith(("http://", "https://", "mailto:", "tel:", "ftp://")):
        return Link(raw=raw, target_file=None, anchor=None, is_external=True)

    if "#" in raw:
        file_part, _, anchor_part = raw.partition("#")
        target_file = file_part or None
        anchor = anchor_part or None
    else:
        target_file = raw or None
        anchor = None

    return Link(raw=raw, target_file=target_file, anchor=anchor, is_external=False)


def _extract_links(line: str) -> list[Link]:
    """Extract all inline (non-image) Markdown links from a single line."""
    links: list[Link] = []
    for match in _LINK_RE.finditer(line):
        links.append(_parse_target(match.group("target")))
    return links


def _extract_inline_tokens(line: str) -> list[str]:
    """Extract backticked inline-code tokens from a single prose line."""
    tokens: list[str] = []
    for match in _INLINE_CODE_RE.finditer(line):
        body = match.group("body")
        # GitHub trims a single leading/trailing space inside a code span.
        if len(body) >= 2 and body.startswith(" ") and body.endswith(" "):
            body = body[1:-1]
        tokens.append(body)
    return tokens


def extract_inventory(text: str, path: str) -> FileInventory:
    """Parse Markdown ``text`` into a :class:`FileInventory`.

    Args:
        text: The full Markdown content of the file.
        path: The file's path, stored on the inventory for reporting.

    Returns:
        A populated :class:`FileInventory` describing the file's structure and
        technical content.
    """
    inventory = FileInventory(path=path)

    lines = text.splitlines()

    in_fence = False
    fence_marker = ""      # the run of backticks/tildes that opened the fence
    fence_is_mermaid = False
    fence_lines: list[str] = []

    seen_slugs: Counter[str] = Counter()
    h1_found = False
    # Tracks whether we are still in the region between the H1 and the first
    # subsequent section boundary ('## '+ heading or horizontal rule).
    in_intro_region = False

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        fence_match = _FENCE_RE.match(line)

        # --- fenced code block handling -------------------------------------
        if in_fence:
            fence_lines.append(line)
            # A closing fence is a fence of the same kind, at least as long,
            # with no info string.
            if (
                fence_match
                and fence_match.group("fence")[0] == fence_marker[0]
                and len(fence_match.group("fence")) >= len(fence_marker)
                and fence_match.group("info").strip() == ""
            ):
                block = "\n".join(fence_lines)
                if fence_is_mermaid:
                    inventory.mermaid_blocks.append(block)
                else:
                    inventory.code_blocks.append(block)
                in_fence = False
                fence_marker = ""
                fence_is_mermaid = False
                fence_lines = []
            i += 1
            continue

        if fence_match:
            # Opening fence (we are not currently inside one).
            in_fence = True
            fence_marker = fence_match.group("fence")
            info = fence_match.group("info").strip().lower()
            fence_is_mermaid = info.split() and info.split()[0] == "mermaid"
            fence_lines = [line]
            i += 1
            continue

        # --- headings -------------------------------------------------------
        heading_match = _HEADING_RE.match(line)
        if heading_match and not _HR_RE.match(line):
            level = len(heading_match.group("hashes"))
            htext = heading_match.group("text").strip()
            base_slug = github_slug(htext)
            occurrence = seen_slugs[base_slug]
            seen_slugs[base_slug] += 1
            slug = base_slug if occurrence == 0 else f"{base_slug}-{occurrence}"

            inventory.headings.append(Heading(level=level, text=htext, slug=slug))
            if _header_is_emoji_prefixed(htext):
                inventory.emoji_headers.add(htext)

            if level == 1 and not h1_found:
                h1_found = True
                inventory.h1_title = htext
                in_intro_region = True
            elif in_intro_region:
                # First heading after the H1 ends the intro region.
                in_intro_region = False

            # A heading line still contributes its links/inline tokens.
            inventory.links.extend(_extract_links(line))
            for tok in _extract_inline_tokens(line):
                inventory.inline_tokens[tok] += 1
            i += 1
            continue

        # --- horizontal rule (also ends the intro region) ------------------
        if _HR_RE.match(line):
            if in_intro_region:
                in_intro_region = False
            i += 1
            continue

        # --- tables ---------------------------------------------------------
        if _TABLE_DELIM_RE.match(line) and "|" in line:
            # The preceding non-blank line is the header row; include it and all
            # following consecutive lines that contain a pipe.
            table_block: list[str] = []
            header_idx = i - 1
            if header_idx >= 0 and "|" in lines[header_idx] and lines[header_idx].strip():
                table_block.append(lines[header_idx])
            table_block.append(line)
            j = i + 1
            while j < n and "|" in lines[j] and lines[j].strip() and not _FENCE_RE.match(lines[j]):
                table_block.append(lines[j])
                j += 1
            inventory.table_rows.extend(table_block)
            if in_intro_region:
                in_intro_region = False
            i = j
            continue

        # --- intro detection ------------------------------------------------
        if in_intro_region and line.strip():
            inventory.intro_present = True

        # --- inline tokens + links on prose lines --------------------------
        inventory.links.extend(_extract_links(line))
        for tok in _extract_inline_tokens(line):
            inventory.inline_tokens[tok] += 1

        i += 1

    # Flush an unterminated fence so its content is not silently lost.
    if in_fence and fence_lines:
        block = "\n".join(fence_lines)
        if fence_is_mermaid:
            inventory.mermaid_blocks.append(block)
        else:
            inventory.code_blocks.append(block)

    inventory.heading_count = len(inventory.headings)
    return inventory


def extract_technical_tokens(text: str) -> TechnicalContent:
    """Return the technical payload of ``text``.

    This is a convenience wrapper over :func:`extract_inventory` that exposes
    just the code blocks, mermaid blocks, table rows, and inline tokens.
    """
    inv = extract_inventory(text, path="<tokens>")
    return TechnicalContent(
        code_blocks=list(inv.code_blocks),
        mermaid_blocks=list(inv.mermaid_blocks),
        table_rows=list(inv.table_rows),
        inline_tokens=Counter(inv.inline_tokens),
    )
