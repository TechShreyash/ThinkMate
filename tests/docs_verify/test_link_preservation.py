"""Property-based tests for cross-link preservation (Property 7).

These tests exercise :func:`tools.docs_verify.links.check_links_preserved`
(and, by extension, :func:`tools.docs_verify.extract.extract_inventory`), which
together enforce the *Cross-Link Integrity Approach* of the ``docs-rewrite``
design: the set of *valid* link targets present before a rewrite must be a
subset of the valid targets after it. No working navigation path may be dropped.

**Feature: docs-rewrite, Property 7: Cross-link preservation**

For any cross-link that was valid before the rewrite, that link target is still
present and valid after the rewrite (the baseline valid-target set is a subset
of the rewritten valid-target set).

_Validates: Requirements 4.4_
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Hashable

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.links import check_links_preserved
from tools.docs_verify.models import Link


# --- key helper (mirrors the module's notion of a link target identity) ------

def _key(link: Link) -> tuple[Hashable, ...]:
    """Identity of a link's *target*, ignoring its display text.

    Matches the keying used inside ``check_links_preserved``: external links are
    keyed by their raw target, on-disk links by ``target_file`` + ``anchor``.
    """
    if link.is_external:
        return ("external", link.raw)
    return ("internal", link.target_file, link.anchor)


# --- generators --------------------------------------------------------------

# Targets are constrained to the slice of the input space the link regex
# accepts: no spaces and no closing paren in the target. Files end in an
# extension; anchors are already-valid GitHub slugs (lowercase / digits /
# hyphens) so they double as on-disk heading text when we need a real file.
_FILES = [
    "a.md", "b.md", "c.md", "guide.md", "readme.md",
    "docs/setup.md", "sub/c.md", "x.txt", "y.py",
]
_ANCHORS = ["intro", "setup", "details", "section-1", "key-features"]
_URLS = [
    "https://example.com",
    "https://x.org/a",
    "http://t.io/p",
    "mailto:a@b.com",
]


@st.composite
def _link_target(draw) -> tuple[str, tuple[Hashable, ...]]:
    """Draw one ``(raw_target, target_key)`` pair spanning the link kinds."""
    kind = draw(st.sampled_from(["file", "file_anchor", "anchor", "external"]))
    if kind == "file":
        f = draw(st.sampled_from(_FILES))
        return f, ("internal", f, None)
    if kind == "file_anchor":
        f = draw(st.sampled_from(_FILES))
        a = draw(st.sampled_from(_ANCHORS))
        return f"{f}#{a}", ("internal", f, a)
    if kind == "anchor":
        a = draw(st.sampled_from(_ANCHORS))
        return f"#{a}", ("internal", None, a)
    url = draw(st.sampled_from(_URLS))
    return url, ("external", url)


@st.composite
def _distinct_targets(draw, *, min_size: int = 1, max_size: int = 12):
    """Draw a list of ``(raw, key)`` pairs deduplicated by target key.

    Distinct keys make the preservation arithmetic exact: a removed target can
    never be re-supplied by another link, so dropped == removed.
    """
    drawn = draw(st.lists(_link_target(), min_size=min_size, max_size=max_size))
    by_key: dict[tuple[Hashable, ...], str] = {}
    for raw, key in drawn:
        by_key.setdefault(key, raw)
    assume(by_key)  # at least one distinct target
    return [(raw, key) for key, raw in by_key.items()]


def _doc(raws: list[str], *, lead_prose: bool = False) -> str:
    """Render a Markdown document embedding each raw target as a link."""
    lines = ["# Title", "", "An introductory paragraph for context.", ""]
    if lead_prose:
        lines += ["Some extra clarifying prose that wraps the links.", ""]
    for i, raw in enumerate(raws):
        lines.append(f"- item {i}: see [link {i}]({raw}) for more.")
    lines.append("")
    return "\n".join(lines) + "\n"


# --- Property 7: a link-preserving rewrite drops nothing ---------------------

@settings(max_examples=200)
@given(_distinct_targets(), st.data())
def test_preserving_rewrite_drops_no_links(
    targets: list[tuple[str, tuple[Hashable, ...]]], data: st.DataObject
) -> None:
    """A rewrite that keeps every target (reordered, extra prose, extra links)
    reports no dropped baseline links.

    **Feature: docs-rewrite, Property 7: Cross-link preservation**

    _Validates: Requirements 4.4_
    """
    original_raws = [raw for raw, _ in targets]
    original = extract_inventory(_doc(original_raws), path="index.md")

    # Rewrite: same targets in a shuffled order, plus optional brand-new links.
    permuted = data.draw(st.permutations(original_raws))
    extra = data.draw(st.lists(_link_target(), max_size=5))
    rewritten_raws = list(permuted) + [raw for raw, _ in extra]
    rewritten = extract_inventory(_doc(rewritten_raws, lead_prose=True), path="index.md")

    dropped = check_links_preserved(original, rewritten)
    assert dropped == []


# --- Property 7: removing a valid link is reported as dropped ----------------

@settings(max_examples=200)
@given(_distinct_targets(min_size=1), st.data())
def test_removing_links_is_reported_as_dropped(
    targets: list[tuple[str, tuple[Hashable, ...]]], data: st.DataObject
) -> None:
    """Removing one or more baseline targets reports exactly those as dropped,
    in first-seen baseline order.

    **Feature: docs-rewrite, Property 7: Cross-link preservation**

    _Validates: Requirements 4.4_
    """
    original_raws = [raw for raw, _ in targets]
    original = extract_inventory(_doc(original_raws), path="index.md")

    n = len(targets)
    remove_flags = data.draw(
        st.lists(st.booleans(), min_size=n, max_size=n)
    )
    # Guarantee at least one removal so there is something to detect.
    assume(any(remove_flags))

    kept_raws = [raw for (raw, _), drop in zip(targets, remove_flags) if not drop]
    expected_dropped_keys = [
        key for (_, key), drop in zip(targets, remove_flags) if drop
    ]

    rewritten = extract_inventory(_doc(kept_raws), path="index.md")
    dropped = check_links_preserved(original, rewritten)

    assert [_key(link) for link in dropped] == expected_dropped_keys


# --- Property 7: reflexivity --------------------------------------------------

@settings(max_examples=200)
@given(_distinct_targets())
def test_identity_rewrite_preserves_all_links(
    targets: list[tuple[str, tuple[Hashable, ...]]]
) -> None:
    """A file compared against itself drops nothing.

    **Feature: docs-rewrite, Property 7: Cross-link preservation**

    _Validates: Requirements 4.4_
    """
    raws = [raw for raw, _ in targets]
    inv = extract_inventory(_doc(raws), path="index.md")
    assert check_links_preserved(inv, inv) == []


# --- Property 7: repo_root variant — only *valid* baseline links must survive -

@st.composite
def _on_disk_links(draw):
    """Draw distinct on-disk file links, each tagged valid/invalid.

    Each link targets a unique file, so its validity is independent: a *valid*
    link's file is created on disk (with the anchor heading when anchored); an
    *invalid* link's file is never created, so it fails resolution.
    """
    descriptors = draw(
        st.lists(
            st.tuples(
                st.sampled_from(_FILES),
                st.sampled_from([None, *_ANCHORS]),
                st.booleans(),  # valid?
            ),
            min_size=1,
            max_size=8,
            unique_by=lambda d: d[0],  # one link per file -> independent validity
        )
    )
    return descriptors


@settings(max_examples=150, deadline=None)
@given(_on_disk_links(), st.data())
def test_repo_root_requires_only_valid_links_to_be_preserved(
    descriptors: list[tuple[str, str | None, bool]], data: st.DataObject
) -> None:
    """With ``repo_root`` given, dropping an *invalid* baseline link is fine;
    only dropped links that genuinely resolved on disk are reported.

    **Feature: docs-rewrite, Property 7: Cross-link preservation**

    _Validates: Requirements 4.4_
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        raws: list[str] = []
        valid_keys: list[tuple[Hashable, ...]] = []
        for f, anchor, valid in descriptors:
            raw = f if anchor is None else f"{f}#{anchor}"
            raws.append(raw)
            if valid:
                target = root / f
                target.parent.mkdir(parents=True, exist_ok=True)
                body = "# Title\n\nIntro prose.\n"
                if anchor is not None:
                    body += f"\n## {anchor}\n\nSection body.\n"
                target.write_text(body, encoding="utf-8")
                valid_keys.append(("internal", f, anchor))

        # The linking file lives at the repo root so relative targets resolve
        # against it; its inventory path is repo-relative.
        original = extract_inventory(_doc(raws), path="index.md")

        # Rewrite keeps a random subset of the links.
        keep_flags = data.draw(
            st.lists(st.booleans(), min_size=len(raws), max_size=len(raws))
        )
        kept_raws = [raw for raw, keep in zip(raws, keep_flags) if keep]
        kept_keys = {
            ("internal", f, anchor)
            for (f, anchor, _), keep in zip(descriptors, keep_flags)
            if keep
        }
        rewritten = extract_inventory(_doc(kept_raws), path="index.md")

        dropped = check_links_preserved(original, rewritten, repo_root=root)
        dropped_keys = {_key(link) for link in dropped}

        # Exactly the valid baseline links that were not kept must be reported;
        # invalid (non-resolving) baseline links are never required to survive.
        expected = {k for k in valid_keys if k not in kept_keys}
        assert dropped_keys == expected
