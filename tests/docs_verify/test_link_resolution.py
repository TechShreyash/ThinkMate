"""Property-based tests for cross-link resolution (docs-rewrite Property 6).

These tests exercise the cross-link *resolution* guarantee (R4.1) implemented by
:func:`tools.docs_verify.links.resolve_links`, together with the supporting
:func:`tools.docs_verify.extract.extract_inventory` /
:func:`tools.docs_verify.extract.github_slug` pipeline.

The strategy builds a small synthetic Markdown file tree in a temporary
directory and a "linking" document whose links fall into known-good and
known-bad categories:

- a link to an existing file (optionally to an existing ``#anchor``) must
  resolve to ``True``;
- a link to a missing file, or to an existing file with a missing anchor, or a
  pure-anchor link to a missing section in the linking file itself, must resolve
  to ``False``.

Each generated link records its expected outcome, and the test asserts that
``resolve_links`` classifies every link exactly as expected.

_Requirements: 4.1_
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tools.docs_verify.extract import extract_inventory, github_slug
from tools.docs_verify.links import resolve_links

# A heading "word": lowercase letters/digits, <= 10 chars. ``github_slug`` is the
# identity on these, so a list of distinct words yields a list of distinct slugs.
_WORD = st.from_regex(r"[a-z][a-z0-9]{0,9}", fullmatch=True)

# Sentinel anchors/files that can never collide with a generated word (which is
# at most 10 chars). The "ghost" prefix is itself 11 chars, so any anchor/file
# built from it is guaranteed absent from the generated slug/file sets.
def _ghost_anchor(idx: int) -> str:
    return f"ghostanchor{idx}missing"


def _ghost_file(idx: int) -> str:
    return f"ghostfile{idx}missing.md"


@st.composite
def _link_scenario(draw: st.DrawFn):
    """Generate a synthetic doc tree plus a list of (target, expected) links.

    Returns:
        files: mapping of target-file name -> list of heading words.
        own_headings: heading words for the linking file itself.
        links: list of ``(raw_target, expected_resolved)`` tuples, in order.
    """
    n_files = draw(st.integers(min_value=1, max_value=3))
    files: dict[str, list[str]] = {}
    for i in range(n_files):
        headings = draw(st.lists(_WORD, min_size=0, max_size=4, unique=True))
        files[f"doc{i}.md"] = headings

    own_headings = draw(st.lists(_WORD, min_size=0, max_size=4, unique=True))

    file_names = list(files.keys())
    files_with_headings = [name for name, hs in files.items() if hs]

    # Categories that are always constructible.
    categories = [
        "file_ok",
        "file_anchor_bad",
        "file_missing",
        "file_missing_anchor",
        "own_anchor_bad",
    ]
    # Categories that need an existing anchor target.
    if files_with_headings:
        categories.append("file_anchor_ok")
    if own_headings:
        categories.append("own_anchor_ok")

    n_links = draw(st.integers(min_value=1, max_value=6))
    links: list[tuple[str, bool]] = []
    for k in range(n_links):
        cat = draw(st.sampled_from(categories))

        if cat == "file_ok":
            name = draw(st.sampled_from(file_names))
            links.append((name, True))

        elif cat == "file_anchor_ok":
            name = draw(st.sampled_from(files_with_headings))
            heading = draw(st.sampled_from(files[name]))
            links.append((f"{name}#{github_slug(heading)}", True))

        elif cat == "file_anchor_bad":
            name = draw(st.sampled_from(file_names))
            links.append((f"{name}#{_ghost_anchor(k)}", False))

        elif cat == "file_missing":
            links.append((_ghost_file(k), False))

        elif cat == "file_missing_anchor":
            links.append((f"{_ghost_file(k)}#{_ghost_anchor(k)}", False))

        elif cat == "own_anchor_ok":
            heading = draw(st.sampled_from(own_headings))
            links.append((f"#{github_slug(heading)}", True))

        elif cat == "own_anchor_bad":
            links.append((f"#{_ghost_anchor(k)}", False))

    return files, own_headings, links


def _write_doc(path: Path, headings: list[str]) -> None:
    """Write a Markdown file whose only structure is ``## heading`` lines."""
    body = "\n\n".join(f"## {h}" for h in headings)
    path.write_text(body + "\n", encoding="utf-8")


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(scenario=_link_scenario())
def test_cross_link_resolution(scenario) -> None:
    """**Feature: docs-rewrite, Property 6: Cross-link resolution**

    For any cross-link in the rewritten Documentation_Set, the link resolves to
    an existing file and, when it includes an anchor, to an existing section
    within that file.

    _Validates: Requirements 4.1_
    """
    files, own_headings, links = scenario

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Materialise the target files referenced by "ok"/"missing" links.
        for name, headings in files.items():
            _write_doc(root / name, headings)

        # Build the linking document: its own headings plus one link per line.
        own_section = "\n\n".join(f"## {h}" for h in own_headings)
        link_lines = "\n\n".join(f"[link]({target})" for target, _ in links)
        content = f"# Linking Document\n\n{own_section}\n\n{link_lines}\n"

        index_path = root / "index.md"
        index_path.write_text(content, encoding="utf-8")

        inventory = extract_inventory(content, path=str(index_path))

        # Each generated link is on its own line, so inventory.links preserves
        # the generation order and lines up with our expectations.
        assert len(inventory.links) == len(links), (
            "extracted link count should match generated links"
        )

        results = resolve_links(inventory, repo_root=root)

        assert len(results) == len(links)
        for (target, expected), result in zip(links, results):
            assert result.resolved is expected, (
                f"link {target!r} expected resolved={expected}, "
                f"got {result.resolved} (reason: {result.reason!r})"
            )
