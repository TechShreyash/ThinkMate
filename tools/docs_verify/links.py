"""Cross-link integrity checks for the documentation rewrite.

The rewrite must keep navigation intact. The design's *Cross-Link Integrity
Approach* names two distinct guarantees, and this module implements both:

1. **Resolution (R4.1):** every Markdown link in a rewritten file points at a
   real target. For a relative file link the target file must exist on disk;
   for an anchored link (``file.md#section``) the section anchor must match a
   heading slug in the target file. Anchors are computed with the standard
   GitHub slug rule via :func:`tools.docs_verify.extract.github_slug`.
2. **Preservation (R4.4):** the set of *valid* link targets present before the
   rewrite is a subset of the valid targets after it — no existing, working
   navigation path is dropped.

The public entry points are:

- :func:`resolve_links` — resolve every non-external link in a
  :class:`~tools.docs_verify.models.FileInventory` against the repository,
  returning a :class:`LinkResult` per link.
- :func:`check_links_preserved` — the baseline-superset check; returns the
  baseline links whose valid targets are missing from the rewrite.

External links (``http(s)``/``mailto``/...) are intentionally not checked on
disk: they are reported as resolved with an explanatory reason so callers can
see they were considered and skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Hashable

from tools.docs_verify.extract import extract_inventory, github_slug
from tools.docs_verify.models import FileInventory, Link

__all__ = [
    "LinkResult",
    "resolve_links",
    "check_links_preserved",
]


@dataclass
class LinkResult:
    """The outcome of resolving a single :class:`Link`.

    Attributes:
        link: The link that was checked.
        resolved: ``True`` when the link points at an existing target (and,
            when anchored, an existing section). External links are reported as
            resolved because they are out of scope for on-disk checking.
        reason: A short human-readable explanation of the outcome — empty when
            an on-disk target resolved cleanly, otherwise describing why it did
            (external/anchor-only) or did not (missing file, missing anchor).
    """

    link: Link
    resolved: bool
    reason: str = ""


def _linking_file_dir(inventory_path: str, repo_root: Path) -> Path:
    """Return the directory of the linking file as an absolute path.

    ``inventory.path`` may be absolute or relative to ``repo_root``; either way
    the link's relative target is resolved against the linking file's own
    directory, matching how Markdown renderers resolve relative links.
    """
    linking = Path(inventory_path)
    if not linking.is_absolute():
        linking = repo_root / linking
    return linking.parent


def _heading_slugs_for_file(path: Path, cache: dict[Path, set[str]]) -> set[str]:
    """Return the set of heading slugs for the Markdown file at ``path``.

    Results are cached per resolved path so a file linked to many times is only
    read and parsed once. A file that cannot be read yields an empty set.
    """
    key = path.resolve()
    if key in cache:
        return cache[key]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        slugs: set[str] = set()
    else:
        inventory = extract_inventory(text, path=str(path))
        slugs = {heading.slug for heading in inventory.headings}
    cache[key] = slugs
    return slugs


def _anchor_matches(anchor: str, slugs: set[str]) -> bool:
    """Return whether ``anchor`` corresponds to one of the heading ``slugs``.

    The comparison is tried as-written first and then against the GitHub slug
    form of the anchor, so links written either as the raw slug
    (``#-key-features``) or as readable text are both accepted.
    """
    if anchor in slugs:
        return True
    lowered = anchor.lower()
    if lowered in slugs:
        return True
    return github_slug(anchor) in slugs


def resolve_links(inventory: FileInventory, repo_root: str | Path) -> list[LinkResult]:
    """Resolve every link in ``inventory`` against the repository.

    For each non-external link:

    - The ``target_file`` (resolved relative to the linking file's directory)
      must exist on disk. A pure-anchor link (``#section``) targets the linking
      file itself.
    - When the link carries an ``anchor`` and the target is a Markdown file, the
      anchor must match a heading slug in that target file.

    External links are reported as resolved without an on-disk check.

    Args:
        inventory: The inventory of the file whose links are checked. Its
            ``path`` is used as the base directory for relative targets and as
            the heading source for pure-anchor links.
        repo_root: The repository root used to resolve relative inventory paths.

    Returns:
        One :class:`LinkResult` per link in ``inventory.links``, in order.
    """
    root = Path(repo_root)
    base_dir = _linking_file_dir(inventory.path, root)
    slug_cache: dict[Path, set[str]] = {}

    # The linking file's own heading slugs back pure-anchor (#section) links.
    own_slugs = {heading.slug for heading in inventory.headings}

    results: list[LinkResult] = []
    for link in inventory.links:
        if link.is_external:
            results.append(
                LinkResult(link=link, resolved=True, reason="external link not checked on disk")
            )
            continue

        # Pure-anchor link: resolve against this file's own headings.
        if link.target_file is None:
            if link.anchor is None:
                # Neither a file nor an anchor — nothing meaningful to resolve.
                results.append(
                    LinkResult(link=link, resolved=False, reason="empty link target")
                )
                continue
            if _anchor_matches(link.anchor, own_slugs):
                results.append(LinkResult(link=link, resolved=True))
            else:
                results.append(
                    LinkResult(
                        link=link,
                        resolved=False,
                        reason=f"anchor '#{link.anchor}' not found in {inventory.path}",
                    )
                )
            continue

        # File link (optionally anchored): confirm the file exists first.
        target_path = (base_dir / link.target_file)
        if not target_path.is_file():
            results.append(
                LinkResult(
                    link=link,
                    resolved=False,
                    reason=f"target file '{link.target_file}' does not exist",
                )
            )
            continue

        if link.anchor is None:
            results.append(LinkResult(link=link, resolved=True))
            continue

        # Anchored file link: anchors are only meaningful for Markdown targets.
        if target_path.suffix.lower() not in (".md", ".markdown"):
            results.append(
                LinkResult(
                    link=link,
                    resolved=True,
                    reason="non-Markdown target; anchor not checked",
                )
            )
            continue

        slugs = _heading_slugs_for_file(target_path, slug_cache)
        if _anchor_matches(link.anchor, slugs):
            results.append(LinkResult(link=link, resolved=True))
        else:
            results.append(
                LinkResult(
                    link=link,
                    resolved=False,
                    reason=f"anchor '#{link.anchor}' not found in '{link.target_file}'",
                )
            )

    return results


def _link_target_key(link: Link) -> tuple[Hashable, ...]:
    """Compute an identity key for a link's *target* (ignoring link text).

    External links are keyed by their raw target; on-disk links by their
    ``target_file`` plus optional ``anchor`` so two links pointing at the same
    destination compare equal.
    """
    if link.is_external:
        return ("external", link.raw)
    return ("internal", link.target_file, link.anchor)


def check_links_preserved(
    original_inventory: FileInventory,
    rewritten_inventory: FileInventory,
    repo_root: str | Path | None = None,
) -> list[Link]:
    """Return baseline links whose valid targets were dropped by the rewrite.

    This is the baseline-superset check (R4.4): the set of valid link targets in
    the rewrite must be a superset of the baseline's valid targets. Any baseline
    target that is valid but missing from the rewrite is a dropped link.

    When ``repo_root`` is provided, "valid" is determined by resolving the
    baseline links on disk via :func:`resolve_links`, so only genuinely working
    navigation paths are required to survive. When ``repo_root`` is ``None``,
    every baseline link target is treated as something that must be preserved.

    Args:
        original_inventory: Inventory of the baseline (pre-rewrite) file.
        rewritten_inventory: Inventory of the rewritten (working-copy) file.
        repo_root: Optional repository root. When given, only links that
            resolved successfully in the baseline are required to be preserved.

    Returns:
        The baseline :class:`Link` objects whose target keys are absent from the
        rewrite, each reported once in first-seen baseline order.
    """
    # Decide which baseline links count as "must be preserved".
    if repo_root is not None:
        resolved = resolve_links(original_inventory, repo_root)
        baseline_links = [r.link for r in resolved if r.resolved]
    else:
        baseline_links = list(original_inventory.links)

    rewritten_keys = {_link_target_key(link) for link in rewritten_inventory.links}

    dropped: list[Link] = []
    seen: set[tuple[Hashable, ...]] = set()
    for link in baseline_links:
        key = _link_target_key(link)
        if key in rewritten_keys or key in seen:
            continue
        seen.add(key)
        dropped.append(link)

    return dropped
