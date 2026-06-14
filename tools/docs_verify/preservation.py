"""Technical-content preservation check for the documentation rewrite.

The rewrite treats ``Technical_Content`` as an immutable payload that better
prose merely wraps. This module enforces that contract mechanically by
comparing the :class:`~tools.docs_verify.models.FileInventory` of a file's
``git`` baseline against the inventory of its rewritten working copy.

The single public entry point, :func:`check_preservation`, returns a
:class:`~tools.docs_verify.models.PreservationResult` whose ``missing_*`` /
``dropped_*`` fields enumerate everything the rewrite failed to carry through.
A file passes (``ok`` is ``True``) iff every one of those fields is empty.

The comparison honours the multiplicity rules from the design's *Data Models*
section:

- **Inline tokens** are a multiset (:class:`collections.Counter`). A token that
  appears twice in the baseline but once in the rewrite is missing once, which
  is exactly Counter subtraction.
- **Code blocks, mermaid blocks, and table rows** are ordered lists, but
  preservation is still multiset-based: each baseline occurrence must be
  matched by a distinct occurrence in the rewrite. Missing items are reported
  in baseline order.
- **Emoji headers** form a set; a header is dropped when its exact text is
  absent from the rewrite's emoji-header set.
- **Links** are compared by their resolved target. Only *valid cross-links*
  (non-external, on-disk links identified by ``target_file`` + ``anchor``) are
  subject to the preservation check; external ``http(s)``/``mailto`` links are
  not ``Cross_Link``s and are ignored here. Preservation means the baseline's
  cross-link-target set is a subset of the rewrite's; any baseline target not
  present afterwards is reported once as a dropped link.

This mirrors the ``check_preservation`` interface in the *Components and
Interfaces* section of the ``docs-rewrite`` design and backs **Property 2**
(technical content), **Property 3** (diagrams/tables), **Property 4** (emoji
headers), and **Property 7** (cross-link preservation).
"""

from __future__ import annotations

from collections import Counter
from typing import Hashable

from tools.docs_verify.models import FileInventory, Link, PreservationResult

__all__ = [
    "check_preservation",
]


def _missing_in_order(baseline: list[str], rewritten: list[str]) -> list[str]:
    """Return baseline items not matched by the rewrite, in baseline order.

    Comparison is multiset-based: each rewrite occurrence cancels at most one
    baseline occurrence. An item present twice in ``baseline`` but once in
    ``rewritten`` is reported once.

    Args:
        baseline: The ordered items extracted from the baseline file.
        rewritten: The ordered items extracted from the rewritten file.

    Returns:
        The unmatched baseline items, preserving their original order and
        multiplicity.
    """
    available: Counter[str] = Counter(rewritten)
    missing: list[str] = []
    for item in baseline:
        if available[item] > 0:
            available[item] -= 1
        else:
            missing.append(item)
    return missing


def _link_key(link: Link) -> tuple[Hashable, ...]:
    """Compute an identity key for an on-disk (cross-link) target.

    Cross-links are identified by their resolved ``target_file`` plus optional
    ``anchor`` so that two links pointing at the same destination compare equal
    regardless of surrounding link text. External links are not cross-links and
    are filtered out before this is called.
    """
    return (link.target_file, link.anchor)


def check_preservation(
    original: FileInventory, rewritten: FileInventory
) -> PreservationResult:
    """Check that ``rewritten`` preserves the technical content of ``original``.

    Every fenced code block, mermaid diagram, table row, backticked inline
    token, emoji-prefixed header, and valid link present in ``original`` must
    also be present in ``rewritten``. Anything absent is recorded on the
    returned result.

    Args:
        original: Inventory of the baseline (pre-rewrite) file.
        rewritten: Inventory of the rewritten (working-copy) file.

    Returns:
        A :class:`PreservationResult` whose ``missing_*`` / ``dropped_*`` fields
        enumerate the lost content. ``ok`` is ``True`` iff all of those fields
        are empty, meaning preservation held.
    """
    result = PreservationResult(file=original.path)

    # Inline tokens: multiset subtraction keeps only tokens missing in the
    # rewrite, with the right deficit count (Counter drops zero/negative).
    result.missing_tokens = original.inline_tokens - rewritten.inline_tokens

    # Verbatim blocks and rows: multiset difference reported in baseline order.
    result.missing_code_blocks = _missing_in_order(
        original.code_blocks, rewritten.code_blocks
    )
    result.missing_mermaid = _missing_in_order(
        original.mermaid_blocks, rewritten.mermaid_blocks
    )
    result.missing_table_rows = _missing_in_order(
        original.table_rows, rewritten.table_rows
    )

    # Emoji headers: a set-difference — every baseline emoji header text must
    # still be an emoji header in the rewrite.
    result.dropped_emoji_headers = set(original.emoji_headers) - set(
        rewritten.emoji_headers
    )

    # Links: the baseline's valid cross-link target set must be a subset of the
    # rewrite's. Only non-external links are Cross_Links; external links are not
    # resolution-checked and are excluded here. Report each lost target once,
    # preserving the first baseline occurrence.
    rewritten_keys = {
        _link_key(link) for link in rewritten.links if not link.is_external
    }
    seen: set[tuple[Hashable, ...]] = set()
    dropped_links: list[Link] = []
    for link in original.links:
        if link.is_external:
            continue
        key = _link_key(link)
        if key in rewritten_keys or key in seen:
            continue
        seen.add(key)
        dropped_links.append(link)
    result.dropped_links = dropped_links

    result.ok = (
        not result.missing_tokens
        and not result.missing_code_blocks
        and not result.missing_mermaid
        and not result.missing_table_rows
        and not result.dropped_emoji_headers
        and not result.dropped_links
    )

    return result
