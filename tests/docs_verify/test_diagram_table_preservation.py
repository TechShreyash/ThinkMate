"""Property-based tests for Mermaid-diagram and table preservation.

These tests exercise :func:`tools.docs_verify.extract.extract_inventory` and
:func:`tools.docs_verify.preservation.check_preservation`, focusing on the
``missing_mermaid`` and ``missing_table_rows`` fields of the resulting
:class:`~tools.docs_verify.models.PreservationResult`.

**Feature: docs-rewrite, Property 3: Diagram and table preservation**

For any in-scope file, every Mermaid diagram block and every table row present
before the rewrite appears verbatim in the rewritten file.

The strategy generates synthetic Markdown documents containing random
```mermaid blocks and Markdown tables (header + delimiter + body rows). A
prose-wrapping rewrite that keeps every diagram/table verbatim must report no
missing diagrams or table rows, while a lossy rewrite that drops a diagram or a
row must be detected.

_Validates: Requirements 2.2_
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.preservation import check_preservation


# --- generators --------------------------------------------------------------

# Tokens used for prose words, mermaid node names, and table cells. Restricted
# to alphanumerics so a generated line can never accidentally look like a
# fence (```), a heading (#), a horizontal rule (---), or a table delimiter.
_ALNUM = st.text(
    alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")),
    min_size=1,
    max_size=6,
)


@st.composite
def mermaid_block(draw) -> list[str]:
    """A verbatim ```mermaid fenced block as a list of lines.

    The content lines are alphanumeric word runs (e.g. ``A --> B`` style),
    which can never form a closing fence, so the block stays intact.
    """
    n_lines = draw(st.integers(min_value=1, max_value=3))
    lines = ["```mermaid"]
    for _ in range(n_lines):
        toks = draw(st.lists(_ALNUM, min_size=1, max_size=4))
        lines.append(" --> ".join(toks))
    lines.append("```")
    return lines


@st.composite
def table_spec(draw) -> dict:
    """A Markdown table as its header, delimiter, and body-row lines.

    Columns are constrained to >= 2: the extractor's table-delimiter pattern
    requires at least two pipe-separated columns, so a single-column table is
    intentionally outside the recognised table grammar.
    """
    cols = draw(st.integers(min_value=2, max_value=4))
    n_body = draw(st.integers(min_value=1, max_value=4))

    def _row(cells: list[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    header = _row(draw(st.lists(_ALNUM, min_size=cols, max_size=cols)))
    delim = "| " + " | ".join(["---"] * cols) + " |"
    body = [_row(draw(st.lists(_ALNUM, min_size=cols, max_size=cols))) for _ in range(n_body)]
    return {"header": header, "delim": delim, "body": body}


def _table_lines(spec: dict) -> list[str]:
    return [spec["header"], spec["delim"], *spec["body"]]


@st.composite
def prose_block(draw) -> list[str]:
    """A short prose paragraph (alphanumeric words only)."""
    n_lines = draw(st.integers(min_value=1, max_value=2))
    return [" ".join(draw(st.lists(_ALNUM, min_size=1, max_size=6))) for _ in range(n_lines)]


def _render(blocks: list[list[str]]) -> str:
    """Render a list of line-blocks into Markdown, blank-line separated."""
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


# --- Property 3: a prose-wrapping rewrite preserves everything ---------------

@st.composite
def doc_with_wrapping(draw) -> tuple[str, str, int, int]:
    """``(baseline, wrapped_rewrite, n_mermaid, n_table_rows)``.

    Both documents contain the *same* diagrams and tables; the rewrite merely
    wraps them with extra prose paragraphs (blank-line separated), exactly the
    transformation the rewrite is allowed to perform.
    """
    n_elements = draw(st.integers(min_value=1, max_value=4))
    elements: list[tuple[str, list[str]]] = []
    for _ in range(n_elements):
        if draw(st.booleans()):
            elements.append(("mermaid", draw(mermaid_block())))
        else:
            elements.append(("table", _table_lines(draw(table_spec()))))

    baseline_blocks = [lines for _, lines in elements]

    wrapped_blocks: list[list[str]] = []
    if draw(st.booleans()):
        wrapped_blocks.append(draw(prose_block()))
    for _, lines in elements:
        wrapped_blocks.append(lines)
        if draw(st.booleans()):
            wrapped_blocks.append(draw(prose_block()))

    n_mermaid = sum(1 for kind, _ in elements if kind == "mermaid")
    n_table_rows = sum(len(lines) for kind, lines in elements if kind == "table")
    return _render(baseline_blocks), _render(wrapped_blocks), n_mermaid, n_table_rows


@settings(max_examples=150)
@given(doc_with_wrapping())
def test_prose_wrapping_preserves_diagrams_and_tables(
    case: tuple[str, str, int, int],
) -> None:
    """A prose-wrapping rewrite drops no Mermaid block and no table row.

    **Feature: docs-rewrite, Property 3: Diagram and table preservation**

    _Validates: Requirements 2.2_
    """
    baseline, rewritten, n_mermaid, n_table_rows = case

    original = extract_inventory(baseline, "baseline.md")
    revised = extract_inventory(rewritten, "rewrite.md")

    # Sanity: the generator and extractor agree on what is present, so the
    # preservation assertion below is non-vacuous.
    assert len(original.mermaid_blocks) == n_mermaid
    assert len(original.table_rows) == n_table_rows

    result = check_preservation(original, revised)

    assert result.missing_mermaid == []
    assert result.missing_table_rows == []


# --- Property 3: dropping a diagram is detected ------------------------------

@st.composite
def doc_dropping_mermaid(draw) -> tuple[str, str, str]:
    """``(baseline, lossy_rewrite, dropped_block)`` — one diagram removed."""
    n_mermaid = draw(st.integers(min_value=1, max_value=3))
    mermaids = [draw(mermaid_block()) for _ in range(n_mermaid)]
    tables = [_table_lines(draw(table_spec())) for _ in range(draw(st.integers(0, 2)))]

    baseline_blocks = [*mermaids, *tables]

    drop_idx = draw(st.integers(min_value=0, max_value=n_mermaid - 1))
    kept_mermaids = [m for i, m in enumerate(mermaids) if i != drop_idx]
    rewritten_blocks: list[list[str]] = [draw(prose_block()), *kept_mermaids, *tables]

    dropped_block = "\n".join(mermaids[drop_idx])
    return _render(baseline_blocks), _render(rewritten_blocks), dropped_block


@settings(max_examples=150)
@given(doc_dropping_mermaid())
def test_dropping_a_diagram_is_detected(case: tuple[str, str, str]) -> None:
    """Removing a Mermaid block surfaces it in ``missing_mermaid``.

    **Feature: docs-rewrite, Property 3: Diagram and table preservation**

    _Validates: Requirements 2.2_
    """
    baseline, rewritten, dropped_block = case

    original = extract_inventory(baseline, "baseline.md")
    revised = extract_inventory(rewritten, "rewrite.md")

    result = check_preservation(original, revised)

    assert result.missing_mermaid != []
    assert dropped_block in result.missing_mermaid
    assert result.ok is False


# --- Property 3: dropping a table row is detected ----------------------------

@st.composite
def doc_dropping_table_row(draw) -> tuple[str, str, str]:
    """``(baseline, lossy_rewrite, dropped_row)`` — one body row removed."""
    n_tables = draw(st.integers(min_value=1, max_value=3))
    specs = [draw(table_spec()) for _ in range(n_tables)]
    mermaids = [draw(mermaid_block()) for _ in range(draw(st.integers(0, 2)))]

    baseline_blocks = [*( _table_lines(s) for s in specs), *mermaids]

    table_idx = draw(st.integers(min_value=0, max_value=n_tables - 1))
    row_idx = draw(st.integers(min_value=0, max_value=len(specs[table_idx]["body"]) - 1))
    dropped_row = specs[table_idx]["body"][row_idx]

    rewritten_table_blocks: list[list[str]] = []
    for i, spec in enumerate(specs):
        body = list(spec["body"])
        if i == table_idx:
            del body[row_idx]
        rewritten_table_blocks.append([spec["header"], spec["delim"], *body])

    rewritten_blocks = [*rewritten_table_blocks, *mermaids]
    return _render(baseline_blocks), _render(rewritten_blocks), dropped_row


@settings(max_examples=150)
@given(doc_dropping_table_row())
def test_dropping_a_table_row_is_detected(case: tuple[str, str, str]) -> None:
    """Removing a table body row surfaces it in ``missing_table_rows``.

    **Feature: docs-rewrite, Property 3: Diagram and table preservation**

    _Validates: Requirements 2.2_
    """
    baseline, rewritten, dropped_row = case

    original = extract_inventory(baseline, "baseline.md")
    revised = extract_inventory(rewritten, "rewrite.md")

    result = check_preservation(original, revised)

    assert result.missing_table_rows != []
    assert dropped_row in result.missing_table_rows
    assert result.ok is False
