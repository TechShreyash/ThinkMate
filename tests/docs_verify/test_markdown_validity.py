"""Property-based tests for Markdown-validity diagnostics (docs-rewrite Property 5).

These tests exercise :func:`tools.docs_verify.validity.check_markdown_validity`,
which is the mechanical half of the "valid Markdown rendering" guarantee
(R3.4): every rewritten in-scope file must keep balanced code fences and
parseable tables and Mermaid blocks.

**Feature: docs-rewrite, Property 5: Valid Markdown rendering**

For any rewritten in-scope file, all fenced code blocks are balanced and all
Mermaid diagrams and tables parse as valid Markdown/Mermaid syntax.

The strategy generates two families of synthetic documents:

- *valid* documents — balanced code fences, well-formed tables (header column
  count matches the delimiter row), and Mermaid blocks that open with a
  recognised diagram type — for which the validator must return ``[]``; and
- *defective* documents — each carrying exactly one injected fault (an
  unbalanced fence, a column-mismatched / header-less table, or an empty /
  bad-header Mermaid block) — for which the validator must report the matching
  diagnostic ``kind``.

The real in-scope files (current working copies) are also fed as fixed cases
and asserted to be valid Markdown.

_Validates: Requirements 3.4_
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tools.docs_verify.validity import check_markdown_validity


# --- shared generators -------------------------------------------------------

# Alphanumeric word tokens. Restricted to alphanumerics so a generated line can
# never accidentally look like a fence (```), a heading (#), a horizontal rule
# (---), or a table delimiter row — keeping "valid" documents truly valid.
_ALNUM = st.text(
    alphabet=st.sampled_from(
        list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    ),
    min_size=1,
    max_size=6,
)

# Diagram types the validator recognises on a mermaid block's first content
# line (a representative subset of ``_MERMAID_DIAGRAM_TYPES``).
_MERMAID_TYPES = st.sampled_from(
    ["graph TD", "graph LR", "flowchart TB", "sequenceDiagram", "classDiagram"]
)

# Code-fence markers the validator balances (>=3 backticks or >=3 tildes).
_FENCE_MARKER = st.sampled_from(["```", "~~~", "````"])


@st.composite
def _prose_block(draw) -> list[str]:
    """A short prose paragraph (alphanumeric words only)."""
    n_lines = draw(st.integers(min_value=1, max_value=2))
    return [" ".join(draw(st.lists(_ALNUM, min_size=1, max_size=6))) for _ in range(n_lines)]


@st.composite
def _balanced_code_block(draw) -> list[str]:
    """A balanced fenced code block as a list of lines.

    The body is alphanumeric word runs, which can never themselves form a
    closing fence, so the block opens and closes exactly once.
    """
    marker = draw(_FENCE_MARKER)
    info = draw(st.sampled_from(["", "python", "bash", "text", "json"]))
    n_body = draw(st.integers(min_value=0, max_value=3))
    body = [" ".join(draw(st.lists(_ALNUM, min_size=1, max_size=4))) for _ in range(n_body)]
    return [marker + info, *body, marker]


@st.composite
def _valid_table(draw) -> list[str]:
    """A well-formed Markdown table (header column count == delimiter count)."""
    cols = draw(st.integers(min_value=2, max_value=4))
    n_body = draw(st.integers(min_value=1, max_value=3))

    def _row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = _row(draw(st.lists(_ALNUM, min_size=cols, max_size=cols)))
    delim = "| " + " | ".join(["---"] * cols) + " |"
    body = [_row(draw(st.lists(_ALNUM, min_size=cols, max_size=cols))) for _ in range(n_body)]
    return [header, delim, *body]


@st.composite
def _valid_mermaid(draw) -> list[str]:
    """A parseable ```mermaid block opening with a recognised diagram type."""
    diagram = draw(_MERMAID_TYPES)
    n_lines = draw(st.integers(min_value=0, max_value=3))
    edges = [" --> ".join(draw(st.lists(_ALNUM, min_size=1, max_size=3))) for _ in range(n_lines)]
    return ["```mermaid", diagram, *edges, "```"]


def _render(blocks: list[list[str]]) -> str:
    """Render line-blocks into a Markdown document, blank-line separated."""
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


# --- Property 5: valid documents produce no diagnostics ----------------------

@st.composite
def _valid_document(draw) -> str:
    """A document mixing prose, balanced fences, valid tables, and mermaid."""
    n_elements = draw(st.integers(min_value=1, max_value=5))
    blocks: list[list[str]] = []
    for _ in range(n_elements):
        choice = draw(st.integers(min_value=0, max_value=3))
        if choice == 0:
            blocks.append(draw(_prose_block()))
        elif choice == 1:
            blocks.append(draw(_balanced_code_block()))
        elif choice == 2:
            blocks.append(draw(_valid_table()))
        else:
            blocks.append(draw(_valid_mermaid()))
    return _render(blocks)


@settings(max_examples=200)
@given(_valid_document())
def test_valid_documents_report_no_diagnostics(text: str) -> None:
    """Balanced fences + well-formed tables + parseable mermaid => no problems.

    **Feature: docs-rewrite, Property 5: Valid Markdown rendering**

    _Validates: Requirements 3.4_
    """
    assert check_markdown_validity(text) == []


# --- Property 5: unbalanced fences are detected ------------------------------

@st.composite
def _doc_with_unbalanced_fence(draw) -> str:
    """A document whose trailing fence is opened but never closed."""
    # A valid prefix (optionally containing balanced blocks) keeps the test
    # honest: the only defect is the dangling fence appended at the end.
    prefix_blocks: list[list[str]] = [draw(_prose_block())]
    if draw(st.booleans()):
        prefix_blocks.append(draw(_balanced_code_block()))

    marker = draw(_FENCE_MARKER)
    info = draw(st.sampled_from(["", "python", "text"]))
    n_body = draw(st.integers(min_value=0, max_value=3))
    dangling = [marker + info] + [
        " ".join(draw(st.lists(_ALNUM, min_size=1, max_size=4))) for _ in range(n_body)
    ]
    return _render([*prefix_blocks, dangling])


@settings(max_examples=150)
@given(_doc_with_unbalanced_fence())
def test_unbalanced_fence_is_detected(text: str) -> None:
    """An unclosed code fence is reported as an ``unbalanced_fence`` diagnostic.

    **Feature: docs-rewrite, Property 5: Valid Markdown rendering**

    _Validates: Requirements 3.4_
    """
    diagnostics = check_markdown_validity(text)
    kinds = {d.kind for d in diagnostics}
    assert "unbalanced_fence" in kinds


# --- Property 5: malformed tables are detected -------------------------------

@st.composite
def _doc_with_bad_table(draw) -> str:
    """A document containing one structurally invalid table.

    Two fault flavours are produced:

    - *column mismatch* — the delimiter row's column count differs from its
      header row; and
    - *missing header* — a delimiter row with a blank line directly above it.
    """
    flavour = draw(st.sampled_from(["column_mismatch", "missing_header"]))

    if flavour == "column_mismatch":
        header_cols = draw(st.integers(min_value=2, max_value=4))
        # A different (still >= 2) delimiter column count guarantees a mismatch.
        delim_cols = draw(
            st.integers(min_value=2, max_value=5).filter(lambda c: c != header_cols)
        )

        def _row(cells: list[str]) -> str:
            return "| " + " | ".join(cells) + " |"

        header = _row(draw(st.lists(_ALNUM, min_size=header_cols, max_size=header_cols)))
        delim = "| " + " | ".join(["---"] * delim_cols) + " |"
        body = _row(draw(st.lists(_ALNUM, min_size=header_cols, max_size=header_cols)))
        table = [header, delim, body]
    else:
        cols = draw(st.integers(min_value=2, max_value=4))
        delim = "| " + " | ".join(["---"] * cols) + " |"
        body = "| " + " | ".join(draw(st.lists(_ALNUM, min_size=cols, max_size=cols))) + " |"
        # A blank line sits directly above the delimiter row, so it has no
        # header: the leading prose block is separated by the blank-line render.
        table = [delim, body]

    return _render([draw(_prose_block()), table])


@settings(max_examples=150)
@given(_doc_with_bad_table())
def test_malformed_table_is_detected(text: str) -> None:
    """A column-mismatched or header-less table reports ``unparseable_table``.

    **Feature: docs-rewrite, Property 5: Valid Markdown rendering**

    _Validates: Requirements 3.4_
    """
    diagnostics = check_markdown_validity(text)
    kinds = {d.kind for d in diagnostics}
    assert "unparseable_table" in kinds


# --- Property 5: malformed mermaid blocks are detected -----------------------

@st.composite
def _doc_with_bad_mermaid(draw) -> str:
    """A document containing one unparseable ```mermaid block.

    Either the block is empty (no content lines), or its first content line
    starts with an unrecognised diagram-type token.
    """
    if draw(st.booleans()):
        # Empty mermaid block.
        mermaid = ["```mermaid", "```"]
    else:
        # Bad first token: prefix a non-keyword so it can never collide with a
        # recognised diagram type.
        bad = "notadiagram" + draw(_ALNUM)
        mermaid = ["```mermaid", bad + " content here", "```"]
    return _render([draw(_prose_block()), mermaid])


@settings(max_examples=150)
@given(_doc_with_bad_mermaid())
def test_malformed_mermaid_is_detected(text: str) -> None:
    """An empty or bad-header mermaid block reports ``unparseable_mermaid``.

    **Feature: docs-rewrite, Property 5: Valid Markdown rendering**

    _Validates: Requirements 3.4_
    """
    diagnostics = check_markdown_validity(text)
    kinds = {d.kind for d in diagnostics}
    assert "unparseable_mermaid" in kinds


# --- Property 5: real in-scope files are valid Markdown ----------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The in-scope Documentation_Set from the design's "In-Scope Files" table.
_IN_SCOPE_FILES = [
    "README.md",
    "changelog.md",
    "persona.md",
    "docs/architecture.md",
    "docs/project_plan.md",
    "docs/setup_guide.md",
    "docs/development/configuration.md",
    "docs/development/database.md",
    "docs/development/group_chat.md",
    "docs/development/hardening_plan.md",
    "docs/development/llm_integration.md",
    "docs/development/memory_engine.md",
    "docs/development/observability.md",
    "docs/development/performance_and_scaling.md",
    "docs/development/telegram_bot.md",
    "docs/development/testing_guide.md",
]


@pytest.mark.parametrize("rel_path", _IN_SCOPE_FILES)
def test_real_in_scope_files_are_valid_markdown(rel_path: str) -> None:
    """Every real in-scope working copy passes the Markdown-validity checks.

    **Feature: docs-rewrite, Property 5: Valid Markdown rendering**

    _Validates: Requirements 3.4_
    """
    path = _REPO_ROOT / rel_path
    assert path.is_file(), f"in-scope file is missing: {rel_path}"

    diagnostics = check_markdown_validity(path.read_text(encoding="utf-8"))

    assert diagnostics == [], (
        f"{rel_path} has Markdown-validity problems: "
        + "; ".join(f"L{d.line} {d.kind}: {d.message}" for d in diagnostics)
    )
