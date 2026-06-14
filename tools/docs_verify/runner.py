"""End-to-end verification runner for the documentation rewrite.

This module ties the individual verification checks together into a single,
repeatable entry point. For every in-scope file it compares the working copy
against the file's committed ``git`` baseline and runs the full battery of
preservation and structural checks named in the design's *Verification
Approach* section:

- **Preservation** (R2, R3.2, R4.4) via
  :func:`tools.docs_verify.preservation.check_preservation`.
- **Cross-link resolution** (R4.1) via
  :func:`tools.docs_verify.links.resolve_links`.
- **Cross-link preservation** (R4.4) via
  :func:`tools.docs_verify.links.check_links_preserved`.
- **Markdown validity** (R3.4) via
  :func:`tools.docs_verify.validity.check_markdown_validity`.
- **Intro presence** (R1.2) and **overview presence** (R1.3) — implemented
  here over the :class:`~tools.docs_verify.models.FileInventory`.
- **Persona semantic preservation** (R6) via
  :func:`tools.docs_verify.persona.check_persona_preserved`, applied only to the
  review-only ``persona.md``.
- **Coverage** (R7) — confirming every in-scope file was processed.

The two intro/overview heuristics this module owns:

- *Intro presence* reuses the ``intro_present`` flag the extractor sets when
  there is non-empty prose between the H1 and the first section boundary.
- *Overview presence* is required only for **multi-section** files. A file is
  considered multi-section when it has at least two section headings below the
  H1. An overview counts as present when the file carries an orienting intro
  paragraph (``intro_present``) or an explicit overview-style heading
  (``overview`` / ``summary`` / ``contents`` / ``what's in this`` / ``in this
  doc``).

Running the module against an unchanged checkout is meaningful: the working
copy equals its baseline, so every preservation check passes. Intro/overview
checks may legitimately fail before the editorial rewrite has added those
sections — that is the signal the rewrite tasks are still outstanding.

Usage::

    python -m tools.docs_verify.runner

The process exits ``0`` when every check passes and ``1`` when any check fails
or an in-scope file is missing.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools.docs_verify.extract import extract_inventory
from tools.docs_verify.links import check_links_preserved, resolve_links
from tools.docs_verify.models import (
    FileInventory,
    GitBaselineError,
    read_git_baseline,
)
from tools.docs_verify.persona import check_persona_preserved
from tools.docs_verify.preservation import check_preservation
from tools.docs_verify.validity import check_markdown_validity

__all__ = [
    "CheckResult",
    "FileReport",
    "RunReport",
    "NON_PERSONA_FILES",
    "PERSONA_FILE",
    "IN_SCOPE_FILES",
    "is_multi_section",
    "check_intro_present",
    "check_overview_present",
    "check_coverage",
    "verify_non_persona_file",
    "verify_persona_file",
    "run_all",
    "format_report",
    "main",
]


# --- in-scope file set -------------------------------------------------------
# Repo-root-relative POSIX paths. ``persona.md`` is review-only and handled
# separately from the editorial (non-persona) set.

NON_PERSONA_FILES: tuple[str, ...] = (
    "README.md",
    "changelog.md",
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
)

PERSONA_FILE: str = "persona.md"

IN_SCOPE_FILES: tuple[str, ...] = NON_PERSONA_FILES + (PERSONA_FILE,)


# Heading texts (lowercased, substring match) that signal an explicit overview.
_OVERVIEW_HEADING_HINTS: tuple[str, ...] = (
    "overview",
    "summary",
    "contents",
    "what's in this",
    "whats in this",
    "in this doc",
    "in this guide",
    "at a glance",
)


# --- result containers -------------------------------------------------------


@dataclass
class CheckResult:
    """The outcome of a single named check on a single file.

    Attributes:
        name: Short identifier of the check (e.g. ``"preservation"``).
        ok: Whether the check passed.
        details: Human-readable lines describing failures (or notes). Empty when
            the check passed cleanly.
        skipped: ``True`` when the check could not run (e.g. no git baseline);
            a skipped check does not count as a failure.
    """

    name: str
    ok: bool
    details: list[str] = field(default_factory=list)
    skipped: bool = False


@dataclass
class FileReport:
    """All check results for one in-scope file."""

    path: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` when no non-skipped check failed for this file."""
        return all(r.ok for r in self.results)


@dataclass
class RunReport:
    """The aggregate result of a full verification run."""

    file_reports: list[FileReport] = field(default_factory=list)
    coverage: CheckResult | None = None

    @property
    def ok(self) -> bool:
        """``True`` when every file passed and coverage is satisfied."""
        files_ok = all(fr.ok for fr in self.file_reports)
        coverage_ok = self.coverage is None or self.coverage.ok
        return files_ok and coverage_ok


# --- intro / overview heuristics --------------------------------------------


def is_multi_section(inventory: FileInventory) -> bool:
    """Return whether a file presents a multi-section explanation.

    A file is treated as multi-section when it has at least two section headings
    below the top-level H1 (i.e. two or more ``##``-or-deeper headings).

    Args:
        inventory: The parsed inventory of the file.

    Returns:
        ``True`` if the file has two or more sub-H1 section headings.
    """
    section_headings = [h for h in inventory.headings if h.level >= 2]
    return len(section_headings) >= 2


def check_intro_present(inventory: FileInventory) -> CheckResult:
    """Assert the file has an introductory prose block under its H1 (R1.2).

    Args:
        inventory: The parsed inventory of a non-persona file.

    Returns:
        A passing :class:`CheckResult` when ``intro_present`` is set, otherwise a
        failing result explaining that no intro was found.
    """
    if inventory.intro_present:
        return CheckResult(name="intro", ok=True)
    return CheckResult(
        name="intro",
        ok=False,
        details=[
            "no introductory prose found between the H1 and the first section "
            "boundary"
        ],
    )


def check_overview_present(inventory: FileInventory) -> CheckResult:
    """Assert multi-section files carry an orienting overview (R1.3).

    Single-section files pass trivially. For multi-section files, an overview is
    considered present when the file either has an orienting intro paragraph
    (``intro_present``) or an explicit overview-style heading.

    Args:
        inventory: The parsed inventory of a non-persona file.

    Returns:
        A :class:`CheckResult` describing whether an overview is present.
    """
    if not is_multi_section(inventory):
        return CheckResult(
            name="overview",
            ok=True,
            details=["single-section file; overview not required"],
        )

    if inventory.intro_present:
        return CheckResult(name="overview", ok=True)

    for heading in inventory.headings:
        lowered = heading.text.lower()
        if any(hint in lowered for hint in _OVERVIEW_HEADING_HINTS):
            return CheckResult(name="overview", ok=True)

    return CheckResult(
        name="overview",
        ok=False,
        details=[
            "multi-section file has neither an orienting intro paragraph nor an "
            "overview/summary/contents heading"
        ],
    )


def check_coverage(
    processed: set[str], in_scope: tuple[str, ...] = IN_SCOPE_FILES
) -> CheckResult:
    """Confirm every in-scope file was processed by the run (R7).

    Args:
        processed: The repo-relative POSIX paths that the run actually handled.
        in_scope: The full in-scope file set to check against.

    Returns:
        A passing :class:`CheckResult` when every in-scope file is present in
        ``processed``, otherwise a failing result naming the gaps.
    """
    missing = [p for p in in_scope if p not in processed]
    if not missing:
        return CheckResult(
            name="coverage",
            ok=True,
            details=[f"all {len(in_scope)} in-scope files processed"],
        )
    return CheckResult(
        name="coverage",
        ok=False,
        details=[f"in-scope file not processed: {p}" for p in missing],
    )


# --- per-file verification ---------------------------------------------------


def _read_working_copy(rel_path: str, repo_root: Path) -> str:
    """Read the on-disk working copy of an in-scope file."""
    return (repo_root / rel_path).read_text(encoding="utf-8")


def verify_non_persona_file(rel_path: str, repo_root: Path, ref: str = "HEAD") -> FileReport:
    """Run the full non-persona check battery for ``rel_path``.

    Reads the working copy and (when available) the committed baseline, then
    runs preservation, link resolution, link preservation, Markdown validity,
    intro presence, and overview presence. When the file has no git baseline
    (new/uncommitted), the baseline-dependent checks are reported as skipped and
    the working-copy-only checks still run.

    Args:
        rel_path: Repo-root-relative POSIX path of the file.
        repo_root: Absolute repository root.
        ref: Git ref to use as the baseline (default ``HEAD``).

    Returns:
        A :class:`FileReport` aggregating every check result for the file.
    """
    report = FileReport(path=rel_path)

    # Working copy must exist; a missing in-scope file is a hard failure.
    try:
        working_text = _read_working_copy(rel_path, repo_root)
    except OSError as exc:
        report.results.append(
            CheckResult(
                name="working-copy",
                ok=False,
                details=[f"cannot read working copy: {exc}"],
            )
        )
        return report

    working_inv = extract_inventory(working_text, path=rel_path)

    # Baseline (may be absent for never-committed files).
    baseline_inv: FileInventory | None = None
    baseline_error: str | None = None
    try:
        baseline_text = read_git_baseline(rel_path, repo_root=repo_root, ref=ref)
        baseline_inv = extract_inventory(baseline_text, path=rel_path)
    except GitBaselineError as exc:
        baseline_error = str(exc)

    # --- preservation (needs a baseline) --------------------------------
    if baseline_inv is not None:
        pres = check_preservation(baseline_inv, working_inv)
        if pres.ok:
            report.results.append(CheckResult(name="preservation", ok=True))
        else:
            details: list[str] = []
            if pres.missing_tokens:
                details.append(
                    "missing inline tokens: "
                    + ", ".join(f"`{tok}`×{cnt}" for tok, cnt in pres.missing_tokens.items())
                )
            if pres.missing_code_blocks:
                details.append(f"missing code blocks: {len(pres.missing_code_blocks)}")
            if pres.missing_mermaid:
                details.append(f"missing mermaid blocks: {len(pres.missing_mermaid)}")
            if pres.missing_table_rows:
                details.append(f"missing table rows: {len(pres.missing_table_rows)}")
            if pres.dropped_emoji_headers:
                details.append(
                    "dropped emoji headers: "
                    + ", ".join(sorted(pres.dropped_emoji_headers))
                )
            if pres.dropped_links:
                details.append(
                    "dropped links: "
                    + ", ".join(link.raw for link in pres.dropped_links)
                )
            report.results.append(
                CheckResult(name="preservation", ok=False, details=details)
            )

        # --- cross-link preservation (baseline-superset) ---------------
        dropped = check_links_preserved(baseline_inv, working_inv, repo_root=repo_root)
        if dropped:
            report.results.append(
                CheckResult(
                    name="link-preservation",
                    ok=False,
                    details=[f"dropped valid baseline link: {link.raw}" for link in dropped],
                )
            )
        else:
            report.results.append(CheckResult(name="link-preservation", ok=True))
    else:
        skip_note = baseline_error or "no git baseline available"
        report.results.append(
            CheckResult(name="preservation", ok=True, skipped=True, details=[skip_note])
        )
        report.results.append(
            CheckResult(name="link-preservation", ok=True, skipped=True, details=[skip_note])
        )

    # --- cross-link resolution (working copy) ---------------------------
    link_results = resolve_links(working_inv, repo_root)
    unresolved = [lr for lr in link_results if not lr.resolved]
    if unresolved:
        report.results.append(
            CheckResult(
                name="link-resolution",
                ok=False,
                details=[f"{lr.link.raw}: {lr.reason}" for lr in unresolved],
            )
        )
    else:
        report.results.append(CheckResult(name="link-resolution", ok=True))

    # --- markdown validity (working copy) -------------------------------
    diagnostics = check_markdown_validity(working_text)
    if diagnostics:
        report.results.append(
            CheckResult(
                name="validity",
                ok=False,
                details=[f"line {d.line} [{d.kind}]: {d.message}" for d in diagnostics],
            )
        )
    else:
        report.results.append(CheckResult(name="validity", ok=True))

    # --- intro + overview presence (working copy) -----------------------
    report.results.append(check_intro_present(working_inv))
    report.results.append(check_overview_present(working_inv))

    return report


def verify_persona_file(rel_path: str, repo_root: Path, ref: str = "HEAD") -> FileReport:
    """Run review-only verification for ``persona.md`` (R6).

    Compares the working copy against the baseline using the persona
    semantic-preservation check. Markdown validity is also checked on the
    working copy. Preservation/intro checks do not apply to the persona file.

    Args:
        rel_path: Repo-root-relative path of the persona file.
        repo_root: Absolute repository root.
        ref: Git ref to use as the baseline (default ``HEAD``).

    Returns:
        A :class:`FileReport` for the persona file.
    """
    report = FileReport(path=rel_path)

    try:
        working_text = _read_working_copy(rel_path, repo_root)
    except OSError as exc:
        report.results.append(
            CheckResult(
                name="working-copy",
                ok=False,
                details=[f"cannot read working copy: {exc}"],
            )
        )
        return report

    try:
        baseline_text = read_git_baseline(rel_path, repo_root=repo_root, ref=ref)
    except GitBaselineError as exc:
        report.results.append(
            CheckResult(
                name="persona-preservation",
                ok=True,
                skipped=True,
                details=[str(exc)],
            )
        )
    else:
        if check_persona_preserved(baseline_text, working_text):
            report.results.append(CheckResult(name="persona-preservation", ok=True))
        else:
            report.results.append(
                CheckResult(
                    name="persona-preservation",
                    ok=False,
                    details=[
                        "normalized persona token stream changed; review-only "
                        "handling was violated"
                    ],
                )
            )

    diagnostics = check_markdown_validity(working_text)
    if diagnostics:
        report.results.append(
            CheckResult(
                name="validity",
                ok=False,
                details=[f"line {d.line} [{d.kind}]: {d.message}" for d in diagnostics],
            )
        )
    else:
        report.results.append(CheckResult(name="validity", ok=True))

    return report


# --- orchestration -----------------------------------------------------------


def run_all(repo_root: str | Path | None = None, ref: str = "HEAD") -> RunReport:
    """Run every verification check across the whole in-scope file set.

    Args:
        repo_root: Repository root. Defaults to the current working directory.
        ref: Git ref to use as the baseline for every file (default ``HEAD``).

    Returns:
        A :class:`RunReport` aggregating per-file reports and the coverage check.
    """
    root = Path(repo_root) if repo_root is not None else Path.cwd()
    report = RunReport()
    processed: set[str] = set()

    for rel_path in NON_PERSONA_FILES:
        report.file_reports.append(verify_non_persona_file(rel_path, root, ref=ref))
        processed.add(rel_path)

    report.file_reports.append(verify_persona_file(PERSONA_FILE, root, ref=ref))
    processed.add(PERSONA_FILE)

    report.coverage = check_coverage(processed)
    return report


def format_report(report: RunReport) -> str:
    """Render a :class:`RunReport` as a human-readable text summary."""
    lines: list[str] = []
    lines.append("docs-rewrite verification")
    lines.append("=" * 40)

    for fr in report.file_reports:
        status = "PASS" if fr.ok else "FAIL"
        lines.append(f"[{status}] {fr.path}")
        for result in fr.results:
            if result.skipped:
                marker = "skip"
            elif result.ok:
                marker = "ok"
            else:
                marker = "FAIL"
            lines.append(f"    - {result.name}: {marker}")
            for detail in result.details:
                # Only surface details for failures and skips to keep noise low.
                if not result.ok or result.skipped:
                    lines.append(f"        · {detail}")

    if report.coverage is not None:
        cov = report.coverage
        status = "PASS" if cov.ok else "FAIL"
        lines.append(f"[{status}] coverage")
        for detail in cov.details:
            lines.append(f"    · {detail}")

    lines.append("=" * 40)
    lines.append("RESULT: " + ("PASS" if report.ok else "FAIL"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: run all checks and return a process exit code.

    Args:
        argv: Optional argument list (unused beyond an optional repo-root path).

    Returns:
        ``0`` when every check passed, ``1`` otherwise.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = args[0] if args else None

    # Doc content (and therefore failure details) can contain emoji / non-ASCII
    # text. Make stdout UTF-8 so printing the report never hits a console codec
    # error on platforms that default to a legacy encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

    report = run_all(repo_root)
    print(format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
