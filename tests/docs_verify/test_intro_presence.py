"""Example tests for intro-presence detection on real documentation files.

Feature: docs-rewrite, Property 1: Intro presence

Property 1 states: *For all in-scope non-persona files in the
``Documentation_Set``, after the rewrite there exists a non-empty introductory
prose block between the top-level H1 and the first subsequent section
boundary.*

Per the design's *Testing Strategy*, Property 1 is verified with concrete
example tests rather than property-based generation, because "multi-section"
membership and intro placement are structural one-shot checks. These tests do
two complementary things:

1. **Real-file examples** — exercise the intro/overview detection machinery
   (:func:`tools.docs_verify.runner.check_intro_present`,
   :func:`~tools.docs_verify.runner.check_overview_present`, and
   :func:`~tools.docs_verify.runner.is_multi_section`) against the actual
   in-scope files, asserting the detector's verdict is consistent with each
   file's real structure (parsed by
   :func:`tools.docs_verify.extract.extract_inventory`). Because the editorial
   rewrites (tasks 8-12) added the intros, every in-scope non-persona file is
   expected to be detected as having one — and Requirement 1.3's overview is
   present for multi-section files. These assertions are derived from the file's
   own parsed inventory, so they lock down the detection behaviour without
   hard-coding assumptions that would break before the rewrite.

2. **Synthetic structural examples** — pin down the one-shot rules the design
   describes: an intro is non-empty prose directly under the H1 and above the
   first ``##``/``---`` boundary; an overview is required only for
   multi-section files.

_Validates: Requirements 1.2, 1.3_
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.models import FileInventory
from tools.docs_verify.runner import (
    NON_PERSONA_FILES,
    check_intro_present,
    check_overview_present,
    is_multi_section,
)

# Repo root: tests/docs_verify/ -> tests/ -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _inventory_for(rel_path: str) -> FileInventory:
    """Parse the real on-disk in-scope file into its inventory."""
    text = (_REPO_ROOT / rel_path).read_text(encoding="utf-8")
    return extract_inventory(text, path=rel_path)


# --- real-file examples ------------------------------------------------------


class TestRealFileIntroPresence:
    """The detection machinery agrees with the real files' structure (R1.2/R1.3)."""

    @pytest.mark.parametrize("rel_path", NON_PERSONA_FILES)
    def test_real_file_exists_and_has_h1(self, rel_path: str) -> None:
        # Sanity guard: every in-scope non-persona file is present and titled,
        # which is the precondition for intro placement "under the H1".
        path = _REPO_ROOT / rel_path
        assert path.is_file(), f"in-scope file missing: {rel_path}"
        inv = _inventory_for(rel_path)
        assert inv.h1_title, f"{rel_path} has no top-level H1 title"

    @pytest.mark.parametrize("rel_path", NON_PERSONA_FILES)
    def test_intro_detector_matches_parsed_intro_flag(self, rel_path: str) -> None:
        # The detector's verdict must be exactly the structural fact parsed from
        # the file (non-empty prose between the H1 and the first boundary).
        inv = _inventory_for(rel_path)
        result = check_intro_present(inv)
        assert result.ok == inv.intro_present
        assert result.name == "intro"

    @pytest.mark.parametrize("rel_path", NON_PERSONA_FILES)
    def test_in_scope_files_have_intro_after_rewrite(self, rel_path: str) -> None:
        # Property 1: after the editorial rewrite, every in-scope non-persona
        # file carries an intro block under its H1.
        inv = _inventory_for(rel_path)
        result = check_intro_present(inv)
        assert result.ok, (
            f"{rel_path} is missing an intro under its H1 "
            f"(Property 1 / R1.2): {result.details}"
        )

    @pytest.mark.parametrize("rel_path", NON_PERSONA_FILES)
    def test_multi_section_files_have_overview(self, rel_path: str) -> None:
        # Requirement 1.3: multi-section files additionally carry an orienting
        # overview. Single-section files pass the overview check trivially.
        inv = _inventory_for(rel_path)
        result = check_overview_present(inv)
        assert result.ok, (
            f"{rel_path} is multi-section but lacks an overview "
            f"(R1.3): {result.details}"
        )
        # Cross-check the overview verdict is consistent with section structure.
        if not is_multi_section(inv):
            assert "single-section" in " ".join(result.details).lower()


# --- synthetic structural examples ------------------------------------------


class TestSyntheticIntroPlacement:
    """One-shot structural rules for intro detection (R1.2)."""

    def test_intro_prose_directly_under_h1_is_detected(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "This short paragraph states the file's purpose.\n"
            "\n"
            "## First Section\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-with-intro>")
        assert inv.intro_present is True
        assert check_intro_present(inv).ok is True

    def test_section_immediately_after_h1_has_no_intro(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "## First Section\n"
            "\n"
            "Body prose lives under a section, not under the H1.\n"
        )
        inv = extract_inventory(text, path="<synthetic-no-intro>")
        assert inv.intro_present is False
        result = check_intro_present(inv)
        assert result.ok is False
        assert result.details  # explains the missing intro

    def test_horizontal_rule_after_h1_has_no_intro(self) -> None:
        # A '---' boundary directly after the H1 ends the intro region with no
        # prose in between, so no intro is present.
        text = (
            "# Title\n"
            "\n"
            "---\n"
            "\n"
            "## Section\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-hr-no-intro>")
        assert inv.intro_present is False
        assert check_intro_present(inv).ok is False

    def test_prose_before_horizontal_rule_is_an_intro(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "Intro prose appears before the rule boundary.\n"
            "\n"
            "---\n"
            "\n"
            "## Section\n"
        )
        inv = extract_inventory(text, path="<synthetic-intro-then-hr>")
        assert inv.intro_present is True
        assert check_intro_present(inv).ok is True

    def test_code_comment_under_h1_is_not_treated_as_intro(self) -> None:
        # Content inside a fenced code block is verbatim, not prose, so a '#'
        # line in it must not be mistaken for an intro paragraph or heading.
        text = (
            "# Title\n"
            "\n"
            "```python\n"
            "# this is a code comment, not intro prose\n"
            "x = 1\n"
            "```\n"
            "\n"
            "## Section\n"
        )
        inv = extract_inventory(text, path="<synthetic-code-only>")
        # The fenced block is not prose; there is no intro paragraph.
        assert inv.intro_present is False
        assert check_intro_present(inv).ok is False


class TestSyntheticOverviewRules:
    """One-shot structural rules for overview presence (R1.3)."""

    def test_single_section_file_does_not_require_overview(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "Intro prose.\n"
            "\n"
            "## Only Section\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-single-section>")
        assert is_multi_section(inv) is False
        result = check_overview_present(inv)
        assert result.ok is True
        assert "single-section" in " ".join(result.details).lower()

    def test_multi_section_with_intro_has_overview(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "An orienting intro paragraph counts as the overview.\n"
            "\n"
            "## Section One\n"
            "\n"
            "Body.\n"
            "\n"
            "## Section Two\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-multi-intro>")
        assert is_multi_section(inv) is True
        assert check_overview_present(inv).ok is True

    def test_multi_section_with_overview_heading_but_no_intro(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "## Overview\n"
            "\n"
            "What's in this doc.\n"
            "\n"
            "## Details\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-overview-heading>")
        assert is_multi_section(inv) is True
        assert inv.intro_present is False
        # An explicit overview-style heading satisfies the overview check.
        assert check_overview_present(inv).ok is True

    def test_multi_section_without_intro_or_overview_fails(self) -> None:
        text = (
            "# Title\n"
            "\n"
            "## Section One\n"
            "\n"
            "Body.\n"
            "\n"
            "## Section Two\n"
            "\n"
            "Body.\n"
        )
        inv = extract_inventory(text, path="<synthetic-multi-no-overview>")
        assert is_multi_section(inv) is True
        assert inv.intro_present is False
        result = check_overview_present(inv)
        assert result.ok is False
        assert result.details
