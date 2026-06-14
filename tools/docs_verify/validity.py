"""Markdown validity diagnostics for the documentation rewrite.

The rewrite must keep every file rendering as valid Markdown/Mermaid
(**Property 5**, Requirement 3.4). This module provides the mechanical half of
that guarantee: :func:`check_markdown_validity` scans a document's text and
returns a list of :class:`Diagnostic` entries for the structural problems that
an accidental edit is most likely to introduce:

1. **Unbalanced code fences** — an opening ```` ``` ```` or ``~~~`` fence with no
   matching closing fence.
2. **Unparseable tables** — a table whose delimiter row does not have the same
   column count as its header row (or which has no header row at all).
3. **Unparseable Mermaid blocks** — a ```` ```mermaid ```` block that is empty or
   whose first content line does not begin with a recognised diagram type
   (``graph``, ``flowchart``, ``sequenceDiagram`` ...).

An empty list means the document is valid as far as these checks are concerned.

The parser is line-oriented and fence-aware, matching the conventions used by
:mod:`tools.docs_verify.extract`: anything inside a fenced code block is treated
as verbatim content and is not scanned for tables, so a pipe-laden line inside a
code sample is never mistaken for a malformed table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Diagnostic",
    "check_markdown_validity",
]


# --- regexes -----------------------------------------------------------------

# Opening/closing fence: optional indent, then >=3 backticks or >=3 tildes,
# followed by an optional info string (e.g. ``python`` or ``mermaid``).
_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# A table delimiter row, e.g. ``| --- | :--: |`` or ``---|:--``.
_TABLE_DELIM_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(?:\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$"
)

# Splits a table row on unescaped pipe characters.
_PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")


# Mermaid diagram-type keywords recognised on the first content line of a
# ``mermaid`` fence (compared case-insensitively against the line's first
# whitespace-delimited token).
_MERMAID_DIAGRAM_TYPES: frozenset[str] = frozenset(
    {
        "graph",
        "flowchart",
        "sequencediagram",
        "classdiagram",
        "statediagram",
        "statediagram-v2",
        "erdiagram",
        "journey",
        "gantt",
        "pie",
        "gitgraph",
        "mindmap",
        "timeline",
        "quadrantchart",
        "requirementdiagram",
        "c4context",
        "c4container",
        "c4component",
        "c4dynamic",
        "c4deployment",
        "sankey-beta",
        "xychart-beta",
        "block-beta",
        "packet-beta",
    }
)


@dataclass
class Diagnostic:
    """A single Markdown-validity problem found in a document.

    Attributes:
        line: The 1-based line number the problem is reported against. For an
            unbalanced fence this is the opening fence line; for a table it is
            the delimiter row; for a Mermaid block it is the offending line (or
            the fence line for an empty block).
        kind: A short machine-readable category — one of
            ``"unbalanced_fence"``, ``"unparseable_table"``, or
            ``"unparseable_mermaid"``.
        message: A human-readable description of the problem.
    """

    line: int
    kind: str
    message: str


def _count_columns(row: str) -> int:
    """Count the number of cells in a Markdown table ``row``.

    Leading and trailing pipes (which are optional in GitHub-flavoured Markdown)
    are ignored, and the remaining text is split on unescaped pipe characters.
    """
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        stripped = stripped[:-1]
    return len(_PIPE_SPLIT_RE.split(stripped))


def _check_mermaid_block(content_lines: list[str], fence_line: int) -> list[Diagnostic]:
    """Validate the body of a ``mermaid`` fence.

    Args:
        content_lines: The lines between the opening and closing fence.
        fence_line: The 1-based line number of the opening fence.

    Returns:
        A list with at most one diagnostic: empty when the block starts with a
        recognised diagram type, otherwise a single ``unparseable_mermaid``
        entry.
    """
    for offset, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Mermaid line comments start with ``%%`` and carry no diagram type.
        if stripped.startswith("%%"):
            continue

        first_token = stripped.split()[0].lower()
        if first_token in _MERMAID_DIAGRAM_TYPES:
            return []

        return [
            Diagnostic(
                line=fence_line + 1 + offset,
                kind="unparseable_mermaid",
                message=(
                    f"mermaid block does not start with a recognised diagram "
                    f"type (found {stripped.split()[0]!r})"
                ),
            )
        ]

    # No non-blank, non-comment content was found.
    return [
        Diagnostic(
            line=fence_line,
            kind="unparseable_mermaid",
            message="mermaid block is empty",
        )
    ]


def check_markdown_validity(text: str) -> list[Diagnostic]:
    """Return Markdown-validity diagnostics for ``text``.

    Checks balanced code fences, well-formed tables (delimiter row aligns with
    its header column count), and parseable Mermaid blocks (non-empty with a
    recognised diagram type on the first content line). An empty list means the
    document passes all three checks.

    Args:
        text: The full Markdown content of a file.

    Returns:
        A list of :class:`Diagnostic` entries, one per problem found, ordered by
        their appearance in the document.
    """
    diagnostics: list[Diagnostic] = []
    lines = text.splitlines()

    in_fence = False
    fence_marker = ""          # the run of backticks/tildes that opened the fence
    fence_is_mermaid = False
    fence_start_line = 0       # 1-based line number of the opening fence
    fence_content: list[str] = []

    for idx, line in enumerate(lines):
        lineno = idx + 1
        fence_match = _FENCE_RE.match(line)

        # --- inside a fenced block ------------------------------------------
        if in_fence:
            is_closing = (
                fence_match is not None
                and fence_match.group("fence")[0] == fence_marker[0]
                and len(fence_match.group("fence")) >= len(fence_marker)
                and fence_match.group("info").strip() == ""
            )
            if is_closing:
                if fence_is_mermaid:
                    diagnostics.extend(
                        _check_mermaid_block(fence_content, fence_start_line)
                    )
                in_fence = False
                fence_marker = ""
                fence_is_mermaid = False
                fence_content = []
            else:
                fence_content.append(line)
            continue

        # --- opening a fenced block -----------------------------------------
        if fence_match:
            in_fence = True
            fence_marker = fence_match.group("fence")
            info = fence_match.group("info").strip().lower().split()
            fence_is_mermaid = bool(info) and info[0] == "mermaid"
            fence_start_line = lineno
            fence_content = []
            continue

        # --- table delimiter rows (only outside fences) ---------------------
        if "|" in line and _TABLE_DELIM_RE.match(line):
            header_idx = idx - 1
            has_header = (
                header_idx >= 0
                and lines[header_idx].strip() != ""
                and "|" in lines[header_idx]
            )
            if not has_header:
                diagnostics.append(
                    Diagnostic(
                        line=lineno,
                        kind="unparseable_table",
                        message="table delimiter row has no header row above it",
                    )
                )
            else:
                header_cols = _count_columns(lines[header_idx])
                delim_cols = _count_columns(line)
                if header_cols != delim_cols:
                    diagnostics.append(
                        Diagnostic(
                            line=lineno,
                            kind="unparseable_table",
                            message=(
                                f"table delimiter row has {delim_cols} column(s) "
                                f"but its header row has {header_cols}"
                            ),
                        )
                    )

    # --- end of file: any still-open fence is unbalanced --------------------
    if in_fence:
        diagnostics.append(
            Diagnostic(
                line=fence_start_line,
                kind="unbalanced_fence",
                message=(
                    f"code fence opened with {fence_marker!r} is never closed"
                ),
            )
        )

    return diagnostics
