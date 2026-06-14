# Implementation Plan: docs-rewrite

## Overview

This plan delivers the ThinkMate documentation rewrite in two tracks. First, a small Python verification layer is built to mechanically enforce the preservation invariants (technical content, diagrams/tables, emoji headers, cross-links, Markdown validity, persona semantics). Second, each in-scope file is rewritten for clarity under the "clarify, don't cut" rule, with `persona.md` handled review-only. Every rewrite is validated against its `git` baseline using the verification layer, and the work closes with a whole-set README ↔ docs synchronization check.

The verification layer is implemented under `tools/docs_verify/` so it stays separate from the application code in `app/` (which is untouched). Tests live under `tests/docs_verify/`.

## Tasks

- [x] 1. Set up verification layer scaffolding and data models
  - [x] 1.1 Create package scaffolding and data models
    - Create `tools/docs_verify/__init__.py` and `tools/docs_verify/models.py`
    - Define the `FileInventory`, `Heading`, `Link`, and `PreservationResult` data structures (dataclasses) from the design Data Models section
    - Add a `git`-baseline helper that reads the committed version of a file for comparison
    - _Requirements: 2.1, 7.1, 7.2, 7.3_

- [x] 2. Implement Markdown extraction and parsing
  - [x] 2.1 Implement inventory extraction
    - In `tools/docs_verify/extract.py`, implement `extract_inventory(text, path)` parsing H1 title, intro presence, headings + GitHub slugs, emoji headers, code blocks, mermaid blocks, table rows, inline backticked tokens, and links
    - Implement `extract_technical_tokens(text)` returning code blocks, mermaid blocks, table rows, and inline tokens
    - Implement the GitHub anchor-slug rule (lowercase, spaces→hyphens, punctuation stripped, emoji handled per existing slugs)
    - _Requirements: 2.1, 2.2, 2.4, 3.2_

  - [x] 2.2 Write unit tests for slug generation edge cases
    - Test emoji-prefixed headers, duplicate headings, and punctuation in headings
    - _Requirements: 4.1_

- [x] 3. Implement technical-content preservation check
  - [x] 3.1 Implement `check_preservation`
    - In `tools/docs_verify/preservation.py`, implement `check_preservation(original, rewritten)` populating `missing_tokens`, `missing_code_blocks`, `missing_mermaid`, `missing_table_rows`, `dropped_emoji_headers`, `dropped_links`, and `ok`
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.2, 4.4_

  - [x] 3.2 Write property test for Technical_Content preservation
    - **Property 2: Technical_Content preservation**
    - **Validates: Requirements 2.1, 2.3, 2.4**

  - [x] 3.3 Write property test for diagram and table preservation
    - **Property 3: Diagram and table preservation**
    - **Validates: Requirements 2.2**

  - [x] 3.4 Write property test for emoji-header preservation
    - **Property 4: Emoji-header preservation**
    - **Validates: Requirements 3.2**

- [x] 4. Implement cross-link integrity and Markdown validity checks
  - [x] 4.1 Implement `resolve_links`
    - In `tools/docs_verify/links.py`, implement `resolve_links(inventory, repo_root)` confirming each non-external link target file exists and (when anchored) the anchor matches a heading slug in the target file
    - Implement the baseline-superset check so post-rewrite valid targets include all pre-rewrite valid targets
    - _Requirements: 4.1, 4.4_

  - [x] 4.2 Implement `check_markdown_validity`
    - In `tools/docs_verify/validity.py`, implement `check_markdown_validity(text)` returning diagnostics for unbalanced code fences, unparseable tables, and unparseable mermaid blocks
    - _Requirements: 3.4_

  - [x] 4.3 Write property test for cross-link resolution
    - **Property 6: Cross-link resolution**
    - **Validates: Requirements 4.1**

  - [x] 4.4 Write property test for cross-link preservation
    - **Property 7: Cross-link preservation**
    - **Validates: Requirements 4.4**

  - [x] 4.5 Write property test for valid Markdown rendering
    - **Property 5: Valid Markdown rendering**
    - **Validates: Requirements 3.4**

- [x] 5. Implement persona semantic-preservation check
  - [x] 5.1 Implement persona normalization and comparison
    - In `tools/docs_verify/persona.py`, implement `normalize_persona(text)` (lowercased token stream, whitespace/punctuation collapsed) and `check_persona_preserved(original, rewritten)`
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 5.2 Write property test for persona semantic preservation
    - **Property 8: Persona semantic preservation**
    - **Validates: Requirements 6.1, 6.2, 6.3, 7.4**

- [x] 6. Implement intro-presence and coverage checks and wire the verification runner
  - [x] 6.1 Implement intro/coverage checks and CLI runner
    - In `tools/docs_verify/runner.py`, assert `intro_present` for each non-persona file and overview presence for multi-section files
    - Implement a coverage check confirming every in-scope file was processed, and wire all checks (preservation, links, validity, persona, intro, coverage) into a single runnable entry point that compares each working file against its `git` baseline
    - _Requirements: 1.2, 1.3, 7.1, 7.2, 7.3, 7.4_

  - [x] 6.2 Write example tests for intro presence on real files
    - **Property 1: Intro presence**
    - **Validates: Requirements 1.2, 1.3**

- [x] 7. Checkpoint - Ensure verification layer passes
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Rewrite root entry files
  - [x] 8.1 Rewrite `README.md`
    - Sharpen the opening hook; add/clarify intro and overview; define terms on first use; explain rationale
    - Preserve the 🌟 Key Features list, the 📂 File/Folder Structure code block, all emoji headers, identifiers, and every cross-link; keep claims consistent with linked docs
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.4, 3.1, 3.2, 3.3, 4.3, 7.1_

  - [x] 8.2 Rewrite `changelog.md`
    - Improve section intros and phrasing; preserve version numbers, dates, and entry specifics verbatim
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 2.1, 2.4, 3.1, 3.3, 7.1_

- [x] 9. Rewrite top-level docs
  - [x] 9.1 Rewrite `docs/architecture.md`
    - Add/clarify document intro and per-section overviews; define terms; explain decisions
    - Preserve the ASCII system-prompt box, every mermaid pipeline diagram, and identifiers like `chat_buffers`, `CHAT_BUFFER_MAX_CHARS`, `UserTaskManager`
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.4, 3.1, 3.2, 3.4, 4.1, 4.4, 7.2_

  - [x] 9.2 Rewrite `docs/project_plan.md`
    - Improve surrounding narrative only; preserve checklist state (checked/unchecked) and phase numbering exactly
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 2.1, 2.3, 3.1, 3.3, 7.2_

  - [x] 9.3 Rewrite `docs/setup_guide.md`
    - Clarify rationale; preserve every command, env-var name, and step ordering; keep consistent with `.env.example`
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.5, 2.1, 2.4, 3.1, 5.1, 5.2, 7.2_

- [x] 10. Rewrite development guides (group A)
  - [x] 10.1 Rewrite `docs/development/configuration.md`
    - Add intro stating the subsystem covered and an orienting overview; define terms; preserve config tables, code blocks, env-var names, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 10.2 Rewrite `docs/development/database.md`
    - Add intro/overview; define terms; preserve schema snippets, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 10.3 Rewrite `docs/development/group_chat.md`
    - Add intro/overview; define terms; preserve diagrams, tables, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 10.4 Rewrite `docs/development/hardening_plan.md`
    - Add intro/overview; define terms; preserve checklist state, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 10.5 Rewrite `docs/development/llm_integration.md`
    - Add intro/overview; define terms; preserve code blocks, API payloads, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

- [x] 11. Rewrite development guides (group B)
  - [x] 11.1 Rewrite `docs/development/memory_engine.md`
    - Add intro/overview; define terms; preserve diagrams, code blocks, identifiers, and anchor targets (e.g., `memory_engine.md#-phase-11--periodic-consolidation-the-dreaming-pass-implemented`) referenced by the README
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 11.2 Rewrite `docs/development/observability.md`
    - Add intro/overview; define terms; preserve metrics tables, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 11.3 Rewrite `docs/development/performance_and_scaling.md`
    - Add intro/overview; define terms; explain rationale; preserve numeric config values, tables, code blocks, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 11.4 Rewrite `docs/development/telegram_bot.md`
    - Add intro/overview; define terms; preserve command lists, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

  - [x] 11.5 Rewrite `docs/development/testing_guide.md`
    - Add intro/overview; define terms; preserve test commands, code blocks, identifiers, and anchor targets
    - Run the verification layer against the baseline and resolve any failures
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.4, 3.1, 3.2, 4.1, 4.4, 7.3_

- [x] 12. Apply review-only handling to `persona.md`
  - [x] 12.1 Review and correct `persona.md`
    - Limit changes to typographical, spelling, and Markdown formatting corrections; preserve wording defining tone, rules, and traits; exclude any edit that would alter behavior
    - Run `check_persona_preserved` against the baseline and confirm it returns true
    - _Requirements: 6.1, 6.2, 6.3, 7.4_

- [x] 13. Checkpoint - Ensure all file rewrites pass verification
  - Ensure all tests pass, ask the user if questions arise.

- [x] 14. Whole-set synchronization and final verification
  - [x] 14.1 Run README ↔ docs synchronization check
    - Reconcile `README.md` Key Features and structure summaries with the rewritten guides per the Alignment_Mapping; align architecture/setup/plan/subsystem wording with their mapped docs and `.env.example`; comply with `.agents/rules/document_changes.md`
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 14.2 Run whole-set verification and coverage check
    - Execute the verification runner across the entire Documentation_Set, confirming global cross-link integrity and that every in-scope file was processed
    - _Requirements: 4.1, 4.4, 7.1, 7.2, 7.3, 7.4_

- [x] 15. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP.
- Each task references specific requirements for traceability.
- Checkpoints ensure incremental validation.
- Property tests validate universal correctness properties; property test tags use the format **Feature: docs-rewrite, Property {number}: {property_text}** with a minimum of 100 iterations each.
- File rewrites each run the verification layer against the file's `git` baseline; no application code under `app/` is changed.
- Each file rewrite writes to a distinct file, so rewrites in different tasks are independent.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "3.1", "4.1", "4.2", "5.2"] },
    { "id": 3, "tasks": ["3.2", "3.3", "3.4", "4.3", "4.4", "4.5", "6.1"] },
    { "id": 4, "tasks": ["6.2", "8.1", "8.2", "9.1", "9.2", "9.3", "10.1", "10.2", "10.3", "10.4", "10.5", "11.1", "11.2", "11.3", "11.4", "11.5", "12.1"] },
    { "id": 5, "tasks": ["14.1", "14.2"] }
  ]
}
```
