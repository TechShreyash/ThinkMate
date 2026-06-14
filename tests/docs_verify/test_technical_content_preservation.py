"""Property-based tests for Technical_Content preservation.

These tests exercise :func:`tools.docs_verify.extract.extract_inventory` and
:func:`tools.docs_verify.preservation.check_preservation`, which together
enforce that the documentation rewrite carries every piece of
``Technical_Content`` through untouched. The rewrite is allowed to wrap that
content in better prose, but it must never drop or alter a fenced code block,
an environment-variable name, a class name, a method name, a file path, or a
numeric configuration value.

**Feature: docs-rewrite, Property 2: Technical_Content preservation**

For any in-scope file, every item of ``Technical_Content`` present before the
rewrite — each fenced code block, environment-variable name, class name,
method name, file path, and numeric configuration value — appears verbatim in
the rewritten file.

_Validates: Requirements 2.1, 2.3, 2.4_
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.preservation import check_preservation


# --- token generators --------------------------------------------------------
#
# Each strategy models one category of inline Technical_Content. ``fullmatch``
# guarantees the generated string contains no backticks, whitespace, or
# newlines, so it survives backtick-wrapping and extraction verbatim (GitHub
# trims a single leading/trailing space inside a code span, which these tokens
# never have).

# Environment-variable names: UPPER_SNAKE_CASE, e.g. ``USER_MEMORY_BUDGET_CHARS``.
_env_var = st.from_regex(r"[A-Z][A-Z0-9_]{1,19}", fullmatch=True)

# Class names: PascalCase identifiers, e.g. ``UserTaskManager``.
_class_name = st.from_regex(r"[A-Z][A-Za-z0-9]{1,19}", fullmatch=True)

# Method/function names: snake_case identifiers, e.g. ``build_memory_block``.
_method_name = st.from_regex(r"[a-z][a-z0-9_]{1,19}", fullmatch=True)

# File paths: relative POSIX paths with an extension, e.g.
# ``app/services/chat_manager.py``.
_file_path = st.from_regex(
    r"[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*){0,3}\.[a-z]{1,4}", fullmatch=True
)

# Numeric configuration values, e.g. ``4,000``, ``80%``, ``0.95``.
_numeric = st.from_regex(r"[0-9]{1,4}(,[0-9]{3})?(\.[0-9]{1,2})?%?", fullmatch=True)

_token = st.one_of(_env_var, _class_name, _method_name, _file_path, _numeric)


# --- fenced code-block generator ---------------------------------------------

# Code-block body characters: printable ASCII without backticks or tildes, so a
# body line can never accidentally form a closing fence.
_code_char = st.characters(
    min_codepoint=32, max_codepoint=126, blacklist_characters="`~"
)
_code_line = st.text(_code_char, max_size=30)


@st.composite
def _code_block(draw) -> str:
    """Generate a verbatim fenced code block (non-mermaid) including fences."""
    # Mermaid blocks are Property 3's concern; keep this to plain code fences.
    lang = draw(st.sampled_from(["", "python", "bash", "json", "text", "yaml"]))
    body = draw(st.lists(_code_line, max_size=4))
    return "\n".join([f"```{lang}", *body, "```"])


# --- document builder --------------------------------------------------------

def _build_doc(
    code_blocks: list[str], tokens: list[str], *, wrap: bool = False
) -> str:
    """Assemble a Markdown document from ``code_blocks`` and inline ``tokens``.

    When ``wrap`` is ``True`` the same technical payload is surrounded by extra
    introductory and explanatory prose, modelling the rewrite. The prose is
    deliberately free of backticks, pipes, brackets, and leading ``#`` so it
    introduces no spurious tokens, tables, links, or headings.
    """
    lines: list[str] = ["# Document Title", ""]
    if wrap:
        lines += ["This added introduction explains what the document covers.", ""]

    for tok in tokens:
        prefix = "Here we describe the " if wrap else "The value "
        lines.append(f"{prefix}`{tok}` used by the system.")

    for block in code_blocks:
        if wrap:
            lines += ["", "The following example demonstrates the usage:"]
        lines += ["", block]

    return "\n".join(lines) + "\n"


# --- Property 2: wrapping prose preserves all technical content --------------

@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_code_block(), max_size=4), st.lists(_token, max_size=8))
def test_wrapping_prose_preserves_technical_content(
    code_blocks: list[str], tokens: list[str]
) -> None:
    """A rewrite that only adds prose preserves every code block and token.

    **Feature: docs-rewrite, Property 2: Technical_Content preservation**

    _Validates: Requirements 2.1, 2.3, 2.4_
    """
    original = _build_doc(code_blocks, tokens)
    rewritten = _build_doc(code_blocks, tokens, wrap=True)

    result = check_preservation(
        extract_inventory(original, "original.md"),
        extract_inventory(rewritten, "rewritten.md"),
    )

    assert result.ok is True
    assert not result.missing_tokens
    assert not result.missing_code_blocks


# --- Property 2: a dropped inline token is detected as missing ---------------

@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_token, min_size=1, max_size=8), st.data())
def test_dropping_an_inline_token_is_detected(
    tokens: list[str], data: st.DataObject
) -> None:
    """Dropping any backticked Technical_Content token flips ``ok`` to False.

    **Feature: docs-rewrite, Property 2: Technical_Content preservation**

    _Validates: Requirements 2.1, 2.4_
    """
    original = _build_doc([], tokens)

    idx = data.draw(st.integers(min_value=0, max_value=len(tokens) - 1))
    dropped = tokens[idx]
    remaining = tokens[:idx] + tokens[idx + 1 :]
    rewritten = _build_doc([], remaining, wrap=True)

    result = check_preservation(
        extract_inventory(original, "original.md"),
        extract_inventory(rewritten, "rewritten.md"),
    )

    assert result.ok is False
    # The dropped token is reported with a positive deficit count.
    assert result.missing_tokens[dropped] >= 1


# --- Property 2: a dropped fenced code block is detected as missing -----------

@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(_code_block(), min_size=1, max_size=4), st.lists(_token, max_size=4), st.data())
def test_dropping_a_code_block_is_detected(
    code_blocks: list[str], tokens: list[str], data: st.DataObject
) -> None:
    """Dropping a fenced code block flips ``ok`` to False and is reported.

    **Feature: docs-rewrite, Property 2: Technical_Content preservation**

    _Validates: Requirements 2.1, 2.3_
    """
    original = _build_doc(code_blocks, tokens)

    idx = data.draw(st.integers(min_value=0, max_value=len(code_blocks) - 1))
    remaining = code_blocks[:idx] + code_blocks[idx + 1 :]
    rewritten = _build_doc(remaining, tokens, wrap=True)

    result = check_preservation(
        extract_inventory(original, "original.md"),
        extract_inventory(rewritten, "rewritten.md"),
    )

    assert result.ok is False
    assert len(result.missing_code_blocks) >= 1
