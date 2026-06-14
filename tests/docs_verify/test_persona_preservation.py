"""Property-based tests for persona semantic preservation.

These tests exercise :func:`tools.docs_verify.persona.normalize_persona` and
:func:`tools.docs_verify.persona.check_persona_preserved`, which together
enforce the review-only handling of ``persona.md``: only typographical,
whitespace, punctuation, and Markdown-formatting changes are allowed, and the
meaningful token sequence must stay identical across the rewrite.

**Feature: docs-rewrite, Property 8: Persona semantic preservation**

For the ``Persona_File``, the normalized meaningful token sequence (ignoring
whitespace, punctuation, and typo-level formatting) is identical before and
after the rewrite.

_Validates: Requirements 6.1, 6.2, 6.3, 7.4_
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st

from tools.docs_verify.persona import check_persona_preserved, normalize_persona


# --- generators --------------------------------------------------------------

# Word tokens are runs of letters/digits (the meaningful content the check
# keeps). We draw from a mix of ASCII letters, digits, and a couple of
# non-ASCII letters so the Unicode-aware tokenizer is exercised.
_WORD = st.text(
    alphabet=st.sampled_from(list("abcdefghijklmnopqrstuvwxyz0123456789éüñ")),
    min_size=1,
    max_size=8,
)

# Separators that the normalizer must treat as semantically invisible:
# whitespace, punctuation, and Markdown formatting glyphs (including the
# underscore, which the implementation treats as a token separator).
_SEPARATORS = [
    " ", "  ", "\t", "\n", "\n\n", " \n ",
    ".", ",", "!", "?", ";", ":", "-", "—",
    "*", "**", "_", "__", "#", "##", "`",
    "(", ")", "[", "]", '"', "'", "...",
]

_separator = st.sampled_from(_SEPARATORS)

# A persona document: a non-empty list of word tokens.
_token_lists = st.lists(_WORD, min_size=1, max_size=30)


def _join_with_random_separators(draw, tokens: list[str]) -> str:
    """Glue ``tokens`` together using randomly drawn separators.

    Optional leading/trailing separators are added too, so surrounding
    formatting noise is covered.
    """
    parts: list[str] = []
    if draw(st.booleans()):
        parts.append(draw(_separator))
    for i, tok in enumerate(tokens):
        if i:
            parts.append(draw(_separator))
        parts.append(tok)
    if draw(st.booleans()):
        parts.append(draw(_separator))
    return "".join(parts)


@st.composite
def persona_with_reformatting(draw):
    """Produce ``(original, rewritten)`` differing only by invisible changes.

    Both strings are built from the *same* ordered token list but use
    independently drawn whitespace/punctuation/formatting separators, and the
    rewritten side may additionally have each token's case flipped. None of
    these changes should affect the normalized token stream.
    """
    tokens = draw(_token_lists)
    original = _join_with_random_separators(draw, tokens)

    # Re-case tokens for the rewrite (case folding must be invisible).
    recased = [
        tok.upper() if draw(st.booleans()) else tok.lower()
        for tok in tokens
    ]
    rewritten = _join_with_random_separators(draw, recased)
    return original, rewritten


# --- Property 8: invariance under formatting-only changes --------------------

@settings(max_examples=200)
@given(persona_with_reformatting())
def test_formatting_only_changes_preserve_persona(pair: tuple[str, str]) -> None:
    """Whitespace/punctuation/case/Markdown changes keep persona preserved.

    **Feature: docs-rewrite, Property 8: Persona semantic preservation**

    _Validates: Requirements 6.1, 6.2, 6.3_
    """
    original, rewritten = pair
    assert check_persona_preserved(original, rewritten) is True


@settings(max_examples=200)
@given(_token_lists, st.data())
def test_normalize_is_invariant_to_separators(
    tokens: list[str], data: st.DataObject
) -> None:
    """``normalize_persona`` ignores the separators placed between tokens.

    Two renderings of the same token list with different whitespace and
    punctuation normalize to the identical token stream (lowercased).

    **Feature: docs-rewrite, Property 8: Persona semantic preservation**

    _Validates: Requirements 6.1, 6.2_
    """
    rendering_a = _join_with_random_separators(data.draw, tokens)
    rendering_b = _join_with_random_separators(data.draw, tokens)
    expected = [t.lower() for t in tokens]
    assert normalize_persona(rendering_a) == expected
    assert normalize_persona(rendering_b) == expected


# --- Property 8: meaningful word changes break preservation ------------------

@settings(max_examples=200)
@given(_token_lists, st.data())
def test_changing_a_word_breaks_preservation(
    tokens: list[str], data: st.DataObject
) -> None:
    """Altering, adding, or removing a word token flips the check to False.

    The mutated word is chosen so its normalized form genuinely differs from
    the original (e.g. appending a letter), guaranteeing a real semantic delta
    rather than a formatting-only one.

    **Feature: docs-rewrite, Property 8: Persona semantic preservation**

    _Validates: Requirements 6.2, 6.3, 7.4_
    """
    original = _join_with_random_separators(data.draw, tokens)
    mutation = data.draw(st.sampled_from(["change", "add", "remove"]))

    if mutation == "change":
        idx = data.draw(st.integers(min_value=0, max_value=len(tokens) - 1))
        mutated = list(tokens)
        # Append a guaranteed word char so the token's normalized form differs.
        mutated[idx] = mutated[idx] + "z9"
    elif mutation == "add":
        idx = data.draw(st.integers(min_value=0, max_value=len(tokens)))
        mutated = list(tokens)
        mutated.insert(idx, "extraword")
    else:  # remove
        idx = data.draw(st.integers(min_value=0, max_value=len(tokens) - 1))
        mutated = list(tokens)
        del mutated[idx]

    rewritten = _join_with_random_separators(data.draw, mutated)

    # Only assert the property when the token streams genuinely differ. For a
    # "remove" on a list whose tokens are not all identical this always holds,
    # but for pathological inputs (e.g. removing a duplicate) the streams could
    # still differ in length, which we confirm before asserting.
    if normalize_persona(original) != normalize_persona(rewritten):
        assert check_persona_preserved(original, rewritten) is False
    else:
        # Streams ended up identical (mutation was semantically invisible);
        # preservation must then report True for consistency.
        assert check_persona_preserved(original, rewritten) is True


# --- reflexivity on real persona-like content --------------------------------

@settings(max_examples=200)
@given(st.text(max_size=400))
def test_preservation_is_reflexive(text: str) -> None:
    """A document is always preserved against itself.

    **Feature: docs-rewrite, Property 8: Persona semantic preservation**

    _Validates: Requirements 6.1_
    """
    assert check_persona_preserved(text, text) is True
    # Idempotent normalization: re-joining the normalized tokens with single
    # spaces and normalizing again yields the same stream.
    once = normalize_persona(text)
    twice = normalize_persona(" ".join(once))
    assert once == twice
