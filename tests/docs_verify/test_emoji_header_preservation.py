"""Property-based tests for emoji-header preservation.

These tests exercise :func:`tools.docs_verify.extract.extract_inventory` (which
populates the ``emoji_headers`` set) together with
:func:`tools.docs_verify.preservation.check_preservation` (which reports
``dropped_emoji_headers``). Together they enforce that the documentation
rewrite keeps every emoji-prefixed header exactly as it was: a rewrite that
preserves all emoji headers reports no drops, while one that strips an emoji
prefix or removes a header entirely is detected.

**Feature: docs-rewrite, Property 4: Emoji-header preservation**

For any header that was emoji-prefixed before the rewrite, the corresponding
header in the rewritten file is still prefixed with the same emoji.

_Validates: Requirements 3.2_
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.preservation import check_preservation


# --- generators --------------------------------------------------------------

# Emojis drawn from the code-point ranges the extractor recognises (see
# ``_EMOJI_RANGES`` in extract.py): a star, a check mark, a gear (with a
# variation selector), a file folder, a crescent moon, plus a few more from
# the supplemental pictograph / dingbat / misc-symbol ranges.
_EMOJIS = ["🌟", "✅", "⚙️", "📂", "🌙", "⭐", "✨", "🚀", "📊", "🔧", "☀", "⏰"]

# Plain word characters for the textual part of a header.
_WORD = st.text(
    alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyz")),
    min_size=1,
    max_size=6,
)


@st.composite
def emoji_header_lists(draw, min_headers: int = 1, max_headers: int = 6):
    """Produce a list of ``(level, text)`` emoji-prefixed headers.

    Each header text is made unique by embedding its index (``Section {i}``),
    so the emoji-header *set* never collapses two distinct entries — that keeps
    drop detection unambiguous.
    """
    count = draw(st.integers(min_value=min_headers, max_value=max_headers))
    headers: list[tuple[int, str]] = []
    for i in range(count):
        emoji = draw(st.sampled_from(_EMOJIS))
        word = draw(_WORD)
        level = draw(st.integers(min_value=2, max_value=4))
        headers.append((level, f"{emoji} Section {i} {word}"))
    return headers


def _build_doc(headers: list[tuple[int, str]], *, intro: str = "Intro prose.") -> str:
    """Render a minimal Markdown document from ``headers``.

    The doc has a fixed H1, an intro paragraph, and a section per header with a
    line of plain prose. Crucially it contains no code blocks, tables, links,
    or backticked tokens, so the only technical content under test is the set
    of emoji-prefixed headers.
    """
    lines = ["# Document Title", "", intro, ""]
    for level, text in headers:
        lines.append("#" * level + " " + text)
        lines.append("")
        lines.append("Some explanatory prose for this section.")
        lines.append("")
    return "\n".join(lines)


# --- Property 4: preservation holds when emoji headers are kept --------------

@settings(max_examples=200)
@given(emoji_header_lists())
def test_kept_emoji_headers_report_no_drops(headers: list[tuple[int, str]]) -> None:
    """A rewrite that keeps every emoji header reports empty drops.

    The rewrite freely adds intros and extra prose (and even a brand-new emoji
    header) but retains all original emoji-prefixed header texts. Preservation
    must hold: ``dropped_emoji_headers`` is empty and ``ok`` is ``True``.

    **Feature: docs-rewrite, Property 4: Emoji-header preservation**

    _Validates: Requirements 3.2_
    """
    original_text = _build_doc(headers)
    # The rewrite keeps all original headers, expands the intro, and appends a
    # fresh emoji header (additions are allowed; only drops are forbidden).
    rewritten_headers = list(headers) + [(2, "🌟 Section new extra")]
    rewritten_text = _build_doc(
        rewritten_headers, intro="A longer, clearer introduction paragraph."
    )

    original = extract_inventory(original_text, path="original.md")
    rewritten = extract_inventory(rewritten_text, path="rewritten.md")

    result = check_preservation(original, rewritten)

    assert result.dropped_emoji_headers == set()
    assert result.ok is True
    # Every original emoji header is recognised and carried through.
    assert original.emoji_headers <= rewritten.emoji_headers


# --- Property 4: dropping an emoji header is detected ------------------------

@settings(max_examples=200)
@given(emoji_header_lists(), st.data())
def test_removing_an_emoji_header_is_detected(
    headers: list[tuple[int, str]], data: st.DataObject
) -> None:
    """Omitting an emoji header entirely surfaces in ``dropped_emoji_headers``.

    **Feature: docs-rewrite, Property 4: Emoji-header preservation**

    _Validates: Requirements 3.2_
    """
    original_text = _build_doc(headers)
    idx = data.draw(st.integers(min_value=0, max_value=len(headers) - 1))
    dropped_level, dropped_text = headers[idx]
    rewritten_headers = [h for j, h in enumerate(headers) if j != idx]
    rewritten_text = _build_doc(rewritten_headers)

    original = extract_inventory(original_text, path="original.md")
    rewritten = extract_inventory(rewritten_text, path="rewritten.md")

    result = check_preservation(original, rewritten)

    assert dropped_text in result.dropped_emoji_headers
    assert result.ok is False


# --- Property 4: stripping the emoji prefix is detected ----------------------

@settings(max_examples=200)
@given(emoji_header_lists(), st.data())
def test_stripping_emoji_prefix_is_detected(
    headers: list[tuple[int, str]], data: st.DataObject
) -> None:
    """Removing the leading emoji from a header is detected as a drop.

    The rewrite keeps the header (and its words) but drops the emoji prefix, so
    the header is no longer emoji-prefixed and its original text disappears from
    the rewrite's emoji-header set.

    **Feature: docs-rewrite, Property 4: Emoji-header preservation**

    _Validates: Requirements 3.2_
    """
    original_text = _build_doc(headers)
    idx = data.draw(st.integers(min_value=0, max_value=len(headers) - 1))
    level, text = headers[idx]
    # Strip the leading emoji and the single space that followed it, leaving a
    # plain (non-emoji) header that keeps the descriptive words.
    _, _, remainder = text.partition(" ")
    stripped_text = remainder
    rewritten_headers = list(headers)
    rewritten_headers[idx] = (level, stripped_text)
    rewritten_text = _build_doc(rewritten_headers)

    original = extract_inventory(original_text, path="original.md")
    rewritten = extract_inventory(rewritten_text, path="rewritten.md")

    # Sanity: the stripped header is no longer considered emoji-prefixed.
    assert stripped_text not in rewritten.emoji_headers

    result = check_preservation(original, rewritten)

    assert text in result.dropped_emoji_headers
    assert result.ok is False
