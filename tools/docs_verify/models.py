"""Data models for the documentation verification layer.

These are the in-memory structures the verification layer builds per file,
mirroring the *Data Models* section of the ``docs-rewrite`` design document.

A few notes on the type choices:

- ``inline_tokens`` and ``missing_tokens`` are *multisets*: the same backticked
  identifier can appear several times in a document and each occurrence matters
  for preservation, so they are modelled with :class:`collections.Counter`.
- Verbatim blocks (code fences, mermaid diagrams, table rows) keep their order,
  so they are plain ``list`` fields.
- Emoji headers are a *set* because we only care about which emoji-prefixed
  header texts exist, not how many times each appears.

The :func:`read_git_baseline` helper reads the committed version of a file so
the rewrite can be compared against its pre-edit baseline.
"""

from __future__ import annotations

import subprocess
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Heading",
    "Link",
    "FileInventory",
    "PreservationResult",
    "GitBaselineError",
    "read_git_baseline",
]


@dataclass
class Heading:
    """A Markdown heading and its computed GitHub anchor slug."""

    level: int
    text: str
    slug: str  # GitHub anchor slug


@dataclass
class Link:
    """A Markdown link extracted from a document.

    ``target_file`` is the resolved relative path for an on-disk link, or
    ``None`` for a pure-anchor link (``#section``). ``anchor`` is the section
    fragment (without the leading ``#``) when present. ``is_external`` marks
    ``http(s)``/``mailto`` links, which are not resolution-checked on disk.
    """

    raw: str
    target_file: str | None = None  # resolved relative path, None for pure-anchor
    anchor: str | None = None
    is_external: bool = False  # http(s) / mailto — not resolution-checked on disk


@dataclass
class FileInventory:
    """A structural + technical-content inventory of a single Markdown file."""

    path: str
    h1_title: str = ""
    intro_present: bool = False  # non-empty prose between H1 and first '## '/'---'
    heading_count: int = 0
    headings: list[Heading] = field(default_factory=list)
    emoji_headers: set[str] = field(default_factory=set)  # headers whose first glyph is emoji
    code_blocks: list[str] = field(default_factory=list)  # verbatim fenced blocks, incl. fences
    mermaid_blocks: list[str] = field(default_factory=list)  # verbatim ```mermaid blocks
    table_rows: list[str] = field(default_factory=list)  # every table row line
    inline_tokens: Counter[str] = field(default_factory=Counter)  # backticked identifiers/paths/etc.
    links: list[Link] = field(default_factory=list)


@dataclass
class PreservationResult:
    """The outcome of comparing a rewritten file against its baseline."""

    file: str
    missing_tokens: Counter[str] = field(default_factory=Counter)
    missing_code_blocks: list[str] = field(default_factory=list)
    missing_mermaid: list[str] = field(default_factory=list)
    missing_table_rows: list[str] = field(default_factory=list)
    dropped_emoji_headers: set[str] = field(default_factory=set)
    dropped_links: list[Link] = field(default_factory=list)
    ok: bool = True


class GitBaselineError(RuntimeError):
    """Raised when the committed baseline of a file cannot be read."""


def read_git_baseline(
    file_path: str | Path,
    *,
    repo_root: str | Path | None = None,
    ref: str = "HEAD",
) -> str:
    """Read the committed version of ``file_path`` from ``git`` for comparison.

    The baseline is the reference for every preservation check: the working
    copy of a rewritten file is compared against the content returned here.

    Args:
        file_path: Path to the file whose committed content is wanted. May be
            absolute or relative to ``repo_root``.
        repo_root: Repository root used as the working directory for ``git`` and
            to compute the path relative to the repo. Defaults to the current
            working directory.
        ref: The git reference to read from. Defaults to ``"HEAD"`` (the last
            commit on the current branch).

    Returns:
        The file's content at ``ref`` as text.

    Raises:
        GitBaselineError: If ``git`` is unavailable or the file does not exist
            at the requested ref (e.g. a brand-new, never-committed file).
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    path = Path(file_path)

    # `git show` expects a repo-relative POSIX path. Normalise whichever form
    # we were given into one relative to the repository root.
    if path.is_absolute():
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise GitBaselineError(
                f"{file_path!s} is not inside repo root {root!s}"
            ) from exc
    else:
        rel = path

    rel_posix = rel.as_posix()
    spec = f"{ref}:{rel_posix}"

    try:
        completed = subprocess.run(
            ["git", "show", spec],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, FileNotFoundError) as exc:  # git binary missing
        raise GitBaselineError(f"failed to invoke git: {exc}") from exc

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise GitBaselineError(
            f"could not read baseline for {rel_posix!r} at {ref!r}: {message}"
        )

    return completed.stdout
