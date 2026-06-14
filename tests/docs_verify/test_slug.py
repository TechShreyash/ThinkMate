"""Unit tests for GitHub slug generation edge cases.

These tests pin down the behaviour of :func:`tools.docs_verify.extract.github_slug`
and the duplicate-heading disambiguation applied inside
:func:`tools.docs_verify.extract.extract_inventory`, which together back the
anchor cross-link resolution checks.

Covered edge cases:

- Emoji-prefixed headers (leading emoji stripped, leaving a leading hyphen).
- Duplicate headings (GitHub-style ``-1``, ``-2`` ... disambiguation).
- Punctuation in headings (em-dashes, parentheses, brackets, quotes, etc.).

_Requirements: 4.1_
"""

from __future__ import annotations

import pytest

from tools.docs_verify.extract import extract_inventory, github_slug


# --- emoji-prefixed headers --------------------------------------------------

class TestEmojiPrefixedHeaders:
    """A leading emoji is stripped, leaving the space-turned-hyphen prefix."""

    def test_real_world_emoji_punctuation_header(self) -> None:
        # The canonical example referenced by the README's anchor cross-link.
        heading = "🌙 Phase 11 — Periodic Consolidation (the Dreaming Pass) [implemented]"
        assert github_slug(heading) == (
            "-phase-11--periodic-consolidation-the-dreaming-pass-implemented"
        )

    def test_simple_emoji_prefix_keeps_leading_hyphen(self) -> None:
        # 🌟 is removed; the space it leaves behind becomes a leading hyphen.
        assert github_slug("🌟 Key Features") == "-key-features"

    def test_pictograph_emoji_prefix(self) -> None:
        assert github_slug("📂 File/Folder Structure") == "-filefolder-structure"

    def test_dingbat_emoji_prefix(self) -> None:
        # ✅ (U+2705) is in the dingbat range and is stripped like any emoji.
        assert github_slug("✅ Implemented") == "-implemented"

    def test_emoji_with_variation_selector(self) -> None:
        # ⚙ plus a variation selector (U+FE0F) should both be stripped.
        assert github_slug("⚙️ Configuration") == "-configuration"

    def test_emoji_only_header(self) -> None:
        # A header that is purely emoji slugifies to the empty string.
        assert github_slug("🌙") == ""


# --- punctuation in headings -------------------------------------------------

class TestPunctuationInHeadings:
    """Punctuation is stripped; whitespace becomes hyphens (not collapsed)."""

    def test_parentheses_and_brackets_removed(self) -> None:
        assert github_slug("Setup (local) [draft]") == "setup-local-draft"

    def test_em_dash_leaves_double_hyphen(self) -> None:
        # The em-dash itself is removed, but the surrounding spaces survive,
        # producing two adjacent hyphens.
        assert github_slug("Phase 11 — Consolidation") == "phase-11--consolidation"

    def test_assorted_punctuation_removed(self) -> None:
        assert github_slug("What's new? Bug-fixes, & more!") == "whats-new-bug-fixes--more"

    def test_colon_and_slash_removed(self) -> None:
        assert github_slug("LLM: client/service") == "llm-clientservice"

    def test_internal_hyphen_preserved(self) -> None:
        assert github_slug("memory-engine") == "memory-engine"

    def test_consecutive_spaces_not_collapsed(self) -> None:
        # Two spaces -> two hyphens (the rule does not collapse runs).
        assert github_slug("a  b") == "a--b"

    def test_dotted_identifier_loses_dots(self) -> None:
        assert github_slug("config.MAX_CHARS value") == "configmax_chars-value"

    def test_underscore_is_a_word_char(self) -> None:
        # Underscores are Unicode word characters, so they survive.
        assert github_slug("chat_buffers map") == "chat_buffers-map"


# --- duplicate-heading disambiguation ---------------------------------------

def _slugs_for(headings: list[str]) -> list[str]:
    """Build a tiny Markdown doc from ``headings`` and return computed slugs."""
    text = "\n\n".join(f"## {h}" for h in headings) + "\n"
    inventory = extract_inventory(text, path="<doc>")
    return [h.slug for h in inventory.headings]


class TestDuplicateHeadingDisambiguation:
    """Repeated heading slugs get GitHub-style numeric suffixes."""

    def test_repeated_identical_headings(self) -> None:
        slugs = _slugs_for(["Overview", "Overview", "Overview"])
        assert slugs == ["overview", "overview-1", "overview-2"]

    def test_distinct_headings_keep_base_slugs(self) -> None:
        slugs = _slugs_for(["Intro", "Details", "Summary"])
        assert slugs == ["intro", "details", "summary"]

    def test_interleaved_duplicates(self) -> None:
        slugs = _slugs_for(["Setup", "Notes", "Setup", "Notes", "Setup"])
        assert slugs == ["setup", "notes", "setup-1", "notes-1", "setup-2"]

    def test_headings_colliding_after_punctuation_strip(self) -> None:
        # "Notes:" and "Notes" both reduce to the base slug "notes".
        slugs = _slugs_for(["Notes:", "Notes"])
        assert slugs == ["notes", "notes-1"]

    def test_duplicate_emoji_headers_disambiguated(self) -> None:
        # Same emoji-prefixed text repeated -> same base slug, then suffixed.
        slugs = _slugs_for(["🌙 Phase", "🌙 Phase"])
        assert slugs == ["-phase", "-phase-1"]


# --- base-slug consistency between the two entry points ----------------------

@pytest.mark.parametrize(
    "heading, expected",
    [
        ("🌟 Key Features", "-key-features"),
        ("Setup (local)", "setup-local"),
        ("Phase 11 — Consolidation", "phase-11--consolidation"),
    ],
)
def test_first_occurrence_matches_github_slug(heading: str, expected: str) -> None:
    """The first occurrence's slug equals the bare ``github_slug`` output."""
    assert github_slug(heading) == expected
    inventory = extract_inventory(f"## {heading}\n", path="<doc>")
    assert inventory.headings[0].slug == expected
