# ThinkMate: in-depth audit and implementation backlog

**Reviewed:** 11 October 2026 (Asia/Calcutta). **Baseline:** [`28b78c5fcea69868debdd12fa38710b8e73ba939`](https://github.com/TechShreyash/ThinkMate/commit/28b78c5fcea69868debdd12fa38710b8e73ba939), matching the local checkout and GitHub `main` inspected for this audit.

This document is a working development guide for ThinkMate's current Telegram companion. It covers defects, failure-policy tradeoffs, improvements to existing features, essential capabilities that are absent, and optional future directions. Each detailed finding has a stable ID, evidence, impact, implementation direction, and acceptance checks. It complements the [short roadmap](roadmap.md) and [current behavior documentation](README.md); proposals here are not shipped features.

The most important next outcome is a bot whose personal memory stays in the right scope, survives ordinary failure correctly, respects current user controls, and has measurable quality and resource limits. More tools, richer media, and replicas are later choices.

## How to use this backlog

1. Start with the [first implementation queue](#first-implementation-queue), then the [release sequence](#recommended-release-sequence). Read the complete finding before coding; some require a privacy/product decision or storage migration.
2. Implement one coherent item or small dependency group per PR. Add the regression that demonstrates the actual scenario, preserve the existing safeguards, and update the affected subsystem guide.
3. Use the checkboxes and [completion record](#completion-record-template) to record the commit, validation, migrations, and operator steps. Completion means the acceptance criteria are met, not merely that a prompt or function was edited.
4. Revisit source evidence when the code changes. Line references describe the baseline commit; IDs remain stable even if files move. Reclassify resolved, disproved, and deliberately deferred items rather than leaving the list permanently unchecked.
5. Treat the [future options](#future-product-and-platform-options) as choices. They are not all essential, and building every option would create a much broader product. Finish the relevant foundations before selecting one.

### Priority and evidence conventions

| Label | Meaning |
| --- | --- |
| P0 | Immediate verified critical failure requiring an urgent response. No P0 incident was established in this audit. |
| P1 | Address before broadening a shared deployment, or before the applicable maintenance/support deadline. |
| P2 | Reliability, correctness under defined edge cases, validation, or development work to schedule next. |
| P3 | Conditional support, later quality improvements, or repository polish. |
| Confirmed defect | A specific source-supported behavior conflicts with the intended contract. Reproduction status is stated separately. |
| Existing tradeoff/policy gap | The code deliberately does this; decide the desired contract before changing it. |
| Validation gap | A property has not been demonstrated by the present tests or measurements; this is not proof that the property fails. |
| Missing essential | Needed for a trustworthy shared deployment or sustainable maintenance, even if a small personal bot can operate without it. |
| Future option | A new capability whose value, cost, permissions, and acceptance criteria need selection. |

Dependency lists identify supporting work and coordination as well as prerequisites; use the narrow first implementation when stated rather than waiting for every related capability. Shared contracts may be introduced in small coordinated PRs. IDs may have gaps where duplicate findings were consolidated.

Priorities are planning judgments, not vulnerability scores. Static findings are not represented as production incidents. A comprehensive review cannot prove that every possible bug has been found, and no estimate of 50,000-user capacity is justified by this repository.

## Audit scope and verification

The review examined application configuration/bootstrap, handlers and menus, group routing/gates, task ownership/batching, Telegram delivery, database models/indexes, all memory stages/prompts/schemas, proactive/consolidation scheduling, metrics/logging, tests, dependency declarations, Docker/Compose, contributor docs, and GitHub release/metadata surfaces.

The existing offline suite was executed in the repository's WSL virtual environment:

```text
python -m pytest -q
693 passed in 9.72s
```

The prior baseline GitHub [Python checks run](https://github.com/TechShreyash/ThinkMate/actions/runs/38150582904) also completed successfully. Passing tests establish their covered behaviors; they do not establish live MongoDB concurrency, production transport behavior, semantic accuracy of actual model outputs, or sustained capacity. This audit did not run paid LLM requests, send live Telegram messages, change repository settings, or exercise a production database.

Five additional synthetic offline cases used the existing mongomock adapters and mocked LLM calls. Network connections and dotenv loading were blocked in the reproduction harness. These were audit reproductions, not additions to the committed regression suite:

| Case | Setup and observed result | Backlog consequence |
| --- | --- | --- |
| Old group extraction after personal reset | Reset the participant after the group transcript read but before lifecycle capture; the participant profile was recreated with a fact from that pre-reset transcript. | Group lifecycle capture needs a consistent admission/snapshot boundary. |
| Unenforceable memory budget | A 2,000-character summary plus one fact under a 600-character budget; budget shedding removed the fact and the compiled block still exceeded budget. | Bound protected fields and avoid discarding all useful arrays without solving the request-size problem. |
| Complete empty replacement | A populated profile received all required compression-array keys with empty arrays; its fact array became empty. | Missing-key validation works, but semantic destructive-replacement checks are still needed. |
| Correction with an unknown original key | An `updated_facts` operation whose `old_content` never existed created `new_content` as a new fact. | Unknown updates need explicit rejection or separately supported insertion semantics. |
| Separate event occurrences | Two events with the same description but dates 10 January and 10 February 2026 became one stored event. | Occurrence identity must include more than description text. |

Detailed findings below distinguish these mocked reproductions from source inspection. They should become focused regression cases before implementation changes. The normal suite was not rerun after documentation-only edits because no application behavior changed.

## What already works and should be preserved

- The README and subsystem docs explain the architecture, setup, limits, and data sharing. ThinkMate updates stored memory/prompts; it does not train model weights.
- Runtime and developer dependency versions are locked; CI already runs Black, Ruff, and the full regression/property suite with SHA-pinned actions.
- The runtime container uses an unprivileged user and excludes optional test dependencies. Configuration/secrets are injected rather than intentionally baked into the image.
- Per-chat batching, maximum coalescing age, pending caps, input/output guards, sender-order preservation, and chat/memory locks already exist. A per-chat limit is not a global bound.
- Profile snapshot-conditional writes, message-ID-based snapshot trimming, and memory-generation guards prevent many stale overwrites and over-trim races. Personal reset owns/cancels prior DM work and defers new DM messages.
- Memory prompts distinguish user evidence from assistant/quoted text, preserve uncertain dates, and label context as data. Required replacement fields are validated; an omitted-field response is not the same problem as explicit empty arrays.
- Reply text is capped before normal assistant history is stored; text helpers split long output and preserve whitespace. Empty direct replies have a fallback; empty ambient replies stay silent.
- The OpenAI SDK's own retries are disabled so application retries do not silently stack with SDK retries. Existing tests include real SDK parsing through mocked HTTP, not only mocked service methods.
- Group participant-name collisions are skipped rather than guessed. Affinity updates have per-member in-process serialization and robust clamping.
- Proactive work already checks opt-out/lifecycle/committed activity after generation and protects a recreated profile after delivery. The intake, pre-attempt eligibility, and clock gaps remain in this backlog.
- Shutdown owns and drains tasks, preserves Telegram availability until cleanup, and runs after startup failure. The deployment deadline must still match the total cleanup sequence.
- Audits normally retain length metadata, not full prompts/completions; error/log/export paths need separate redaction and consent checks.

## First implementation queue

These are the first concrete tasks to take into small PRs. The detailed sections contain the larger backlog and dependencies.

- [ ] **PRIV-01:** Make personal `/profile` inspection private-only.
- [ ] **PRIV-02:** Omit global personal memory from public group prompts as the first privacy boundary; design scoped sharing afterward.
- [ ] **OPS-01:** Reject malformed operator allowlists; distinguish explicit public access from invalid configuration.
- [ ] **TEST-01:** Make test configuration independent of a contributor's `.env` and import side effects.
- [ ] **MEM-01 / MEM-02 / MEM-04 / MEM-07 / MEM-09:** Add regressions and fix or explicitly resolve the five reproduced memory cases.
- [ ] **RUN-01 / RUN-03 / RUN-06:** Track generated versus delivered replies and transport outcomes.
- [ ] **RUN-04 / RUN-05:** Suppress obsolete check-ins using intake activity and the current quiet-hour clock.
- [ ] **RUN-07:** Start gate/cache maintenance even when no reply has been enqueued.
- [ ] **GROUP-01:** Make group disable semantics effective during settings failures and in-flight work.
- [ ] **LLM-01 / LLM-02:** Bound global inference admission and the complete retry/deadline budget.
- [ ] **TEST-02 / TEST-04:** Add disposable MongoDB and shipping-artifact validation.
- [ ] **RUN-09:** Set a measured total shutdown budget and a compatible container grace period.
- [ ] **OPS-06:** Migrate off Motor with real-driver contracts and staged recovery checks.
- [ ] **TEST-06:** Establish semantic memory/reply quality baselines before changing retrieval or adding more model-driven behavior.

The short privacy, authorization, and test-isolation fixes can start before a durable queue is built. Do not wait for a full architecture rewrite to make those boundaries concrete.

## Essential capabilities ThinkMate is still missing

These are foundations for a shared, persistent companion. They summarize the detailed findings rather than introducing another implementation list. A private experimental deployment can deliberately defer some operator features; document that decision instead of implying that deployment scale or data guarantees have been demonstrated.

| Capability | What is missing today | Implement through | Completion signal |
| --- | --- | --- | --- |
| Enforced personal/group boundaries | Personal memory scope is largely a prompt rule, and a personal profile command can run publicly. | [PRIV-01](#priv-01), [PRIV-02](#priv-02), [MEM-08](#mem-08) | Disallowed data never enters a group prompt or public command response, including legacy records. |
| Direct memory ownership | Users cannot reliably inspect, correct, forget, pause, or restrict individual memories/categories. | [PRIV-05](#priv-05), [MEM-02](#mem-02), [MEM-03](#mem-03), [MEM-05](#mem-05) | A user can change a specific memory and its derived copies without resetting everything. |
| Durable reset and deletion | A reset is narrower than erasure, separate deletes can partially fail, and retained sources can be relearned. | [PRIV-04](#priv-04), [DATA-01](#data-01), [DATA-05](#data-05) | Idempotent deletion converges across restarts, old jobs are fenced, and retained/external copies have an explicit policy. |
| Portable data and recoverability | The existing reset export is partial, unversioned, and has no tested importer/consistent-snapshot contract. | [DATA-02](#data-02), [OPS-05](#ops-05) | A synthetic export and deployment backup both restore correctly into an isolated environment. |
| Reliable memory identity and evidence | Text keys, lossy metadata, stale derived memories, and weak replacement validation make correction unreliable. | [MEM-02](#mem-02) through [MEM-10](#mem-10), [DATA-03](#data-03) | Stable IDs, source time/scope, uncertainty, and validated operations survive maintenance and migrations. |
| Measured semantic quality | Passing unit tests do not demonstrate that real model outputs remember the right things or avoid invented ones. | [TEST-06](#test-06), [TEST-03](#test-03) | A versioned synthetic evaluation corpus reports memory precision, correction/forgetting success, scope violations, and reply quality. |
| Process-wide inference limits | Per-chat bounds do not cap all interactive/background requests, complete retries, or total prompt/output cost. | [LLM-01](#llm-01) through [LLM-04](#llm-04) | Controlled overload stays within defined concurrency, deadline, token, and cost budgets. |
| Accountable admission and delivery | Pending work is volatile, admission may silently fail, and generated text can become history before delivery. | [RUN-01](#run-01), [RUN-02](#run-02), [RUN-03](#run-03), [RUN-06](#run-06), [MEM-11](#mem-11) | Accepted work has an observable outcome; restart/retry and partial delivery cannot silently corrupt conversation history. |
| Consented, timely proactive contact | Default enrollment and broad availability settings lack sufficiently clear user control; intake and quiet-hour boundary gaps remain. | [PRIV-06](#priv-06), [RUN-04](#run-04), [RUN-05](#run-05) | Contact is explicitly permitted and rechecks current activity, lifecycle, preferences, and local time at delivery. |
| Reproducible developer and integration checks | Tests can depend on local configuration; real MongoDB and shipping-artifact contracts are absent. | [TEST-01](#test-01) through [TEST-04](#test-04) | A new contributor gets deterministic offline results; disposable integration and container checks validate actual dependencies. |
| Useful operational evidence | Health cannot expose the full queue-to-delivery path, maintenance lag, recent latency, or effective retention policy. | [RUN-11](#run-11), [OPS-04](#ops-04), [DATA-04](#data-04), [DATA-05](#data-05), [TEST-05](#test-05) | Fault injection is visible through bounded metrics and operator alerts; capacity claims follow measurements. |
| Safe long-term maintenance | No explicit schema/release compatibility system or restore drill exists, and Motor has a support deadline. | [DATA-03](#data-03), [DEV-01](#dev-01), [OPS-05](#ops-05), [OPS-06](#ops-06) | Versioned upgrades, rollback/recovery, driver migration, and dependency maintenance are exercised. |
| Clear operator/contributor boundaries | Invalid allowlists can become public access; configuration validation and private security intake are incomplete. | [OPS-01](#ops-01), [OPS-02](#ops-02), [DEV-03](#dev-03) | Invalid configuration fails clearly, access modes are explicit, and sensitive reports have a private route. |

The detailed sections below are the actionable backlog. Each checkbox is initially open; some policy items can be closed by a documented decision plus checks that enforce that decision.

## Detailed finding index

The 67 findings below are distinct work items. Priorities can be conditional on deployment scope; read each finding before scheduling it.

| ID | Priority | Finding |
| --- | --- | --- |
| PRIV-01 | P1 | [Keep personal profile inspection out of public chats](#priv-01) |
| PRIV-02 | P1 | [Enforce memory-sharing boundaries before constructing prompts](#priv-02) |
| PRIV-03 | P1 | [Separate operational diagnostics from personal content](#priv-03) |
| PRIV-04 | P1 | [Require a clear choice for reset backups and provide actual erasure/export](#priv-04) |
| PRIV-05 | P1 | [Give users direct control over what is remembered](#priv-05) |
| PRIV-06 | P1 | [Make proactive outreach a clear consent and availability setting](#priv-06) |
| MEM-01 | P1 | [Capture group owner lifecycles before accepting a transcript snapshot](#mem-01) |
| MEM-02 | P1 | [Validate correction/removal targets and conflicting operations before applying model deltas](#mem-02) |
| MEM-03 | P1 | [Invalidate derived summaries and insights when their evidence changes or is forgotten](#mem-03) |
| MEM-04 | P2 | [Give events stable identity independent of description text](#mem-04) |
| MEM-05 | P2 | [Support explicit clearing of nullable/profile memory fields](#mem-05) |
| MEM-06 | P2 | [Preserve evidence time and identity for mood observations, and reject stale/replayed moods](#mem-06) |
| MEM-07 | P1 | [Validate complete replacements semantically before destroying populated memory](#mem-07) |
| MEM-08 | P2 | [Preserve provenance, chronology, and uncertainty through extraction and maintenance](#mem-08) |
| MEM-09 | P2 | [Enforce a complete memory budget without discarding higher-value data first](#mem-09) |
| MEM-10 | P2 | [Make structured-output validation enforce the documented field constraints](#mem-10) |
| MEM-11 | P2 | [Make extraction admission, apply, and trim replayable and idempotent](#mem-11) |
| MEM-12 | P3 | [Address participants by trusted IDs rather than display names in extraction output](#mem-12) |
| MEM-13 | P2 | [Retain corrective intent when group memory context cannot fit](#mem-13) |
| MEM-14 | P2 | [Track maintenance outcomes and fairly retry skipped/conflicted work](#mem-14) |
| DATA-01 | P2 | [Make reset/delete lifecycle state durable and resilient to partial deletion](#data-01) |
| DATA-02 | P2 | [Define versioned, complete exports and a tested restoration format](#data-02) |
| DATA-03 | P2 | [Version persistent documents and provide explicit migrations/repair tooling](#data-03) |
| DATA-04 | P2 | [Index and bound due-work queries, including eligibility and fair scan progress](#data-04) |
| DATA-05 | P2 | [Reconcile and verify retention rather than treating index creation as success](#data-05) |
| RUN-01 | P1 | [Distinguish generated replies from delivered conversation history](#run-01) |
| RUN-02 | P1 | [Make restart losses an explicit admission and backlog policy](#run-02) |
| RUN-03 | P2 | [Return admission outcomes and handle a full chat queue explicitly](#run-03) |
| RUN-04 | P1 | [Revalidate activity and eligibility before proactive delivery](#run-04) |
| RUN-05 | P2 | [Recheck quiet hours using current time at delivery](#run-05) |
| RUN-06 | P2 | [Add typed delivery results, targeted retries, and chunk progress](#run-06) |
| RUN-07 | P2 | [Start cache/gate pruning even when no replies are enqueued](#run-07) |
| RUN-08 | P2 | [Keep optional reactions from delaying or contradicting a current preference](#run-08) |
| RUN-09 | P2 | [Align the complete shutdown budget with the deployment grace period](#run-09) |
| RUN-10 | P2 | [Make scheduler throughput, freshness, and fairness explicit](#run-10) |
| RUN-11 | P2 | [Measure user-visible health rather than only cumulative service averages](#run-11) |
| LLM-01 | P1 | [Enforce global admission and prioritize interactive work](#llm-01) |
| LLM-02 | P1 | [Share retry and elapsed-time budgets across fallback layers](#llm-02) |
| LLM-03 | P2 | [Budget the complete prompt, not only individual messages or memory](#llm-03) |
| LLM-04 | P2 | [Track actual usage and add provider-aware output budgets](#llm-04) |
| LLM-05 | P2 | [Separate plain-text recovery from malformed structured output](#llm-05) |
| LLM-06 | P2 | [Bound audit-write admission and summarize before scheduling](#llm-06) |
| GROUP-01 | P1 | [Make the group off switch hold during failures and in-flight work](#group-01) |
| GROUP-02 | P2 | [Preserve original order across passive and queued group messages](#group-02) |
| GROUP-03 | P2 | [Decide whether spam should ever qualify for ambient generation](#group-03) |
| GROUP-04 | P2 | [Improve multilingual routing and back-off interpretation](#group-04) |
| GROUP-05 | P2 | [Define forum-topic and group-migration support](#group-05) |
| UX-01 | P2 | [Make errors, overload, and unsupported requests actionable](#ux-01) |
| UX-02 | P3 | [Make group menus and callbacks appropriate to the invoking person](#ux-02) |
| UX-03 | P3 | [Preserve and expose useful reply formatting](#ux-03) |
| OPS-01 | P1 | [Reject malformed operator allowlists instead of opening reports](#ops-01) |
| OPS-02 | P2 | [Complete configuration validation and provide a local preflight](#ops-02) |
| OPS-04 | P2 | [Add health signals that a deployment platform can actually observe](#ops-04) |
| OPS-05 | P1 | [Turn backup advice into a tested disaster-recovery procedure](#ops-05) |
| OPS-06 | P1 | [Migrate the MongoDB driver deliberately](#ops-06) |
| TEST-01 | P2 | [Isolate automated tests from local environment and import side effects](#test-01) |
| TEST-02 | P1 | [Add actual MongoDB contracts beside fast mocked tests](#test-02) |
| TEST-03 | P2 | [Publish a bounded provider contract matrix and fix the live harness lifecycle](#test-03) |
| TEST-04 | P2 | [Test the artifacts that operators install and deploy](#test-04) |
| TEST-05 | P2 | [Make capacity and failure recovery measurable](#test-05) |
| TEST-06 | P1 | [Evaluate semantic memory and conversation behavior with a versioned corpus](#test-06) |
| DEV-01 | P2 | [Establish versioned releases and storage/config upgrade contracts](#dev-01) |
| DEV-02 | P2 | [Ship the repository license with distributed artifacts](#dev-02) |
| DEV-03 | P2 | [Provide a private security intake and useful contribution templates](#dev-03) |
| DEV-04 | P3 | [Improve GitHub discovery with evidence of the actual product](#dev-04) |
| DEV-05 | P2 | [Check documentation contracts automatically and update plans as work ships](#dev-05) |
| DEV-06 | P2 | [Reduce global coupling through small domain boundaries](#dev-06) |

## Privacy, consent, and user control

<a id="priv-01"></a>

### PRIV-01 — Keep personal profile inspection out of public chats

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Confirmed exposure path · Source inspection.**

- **Evidence:** [`cmd_profile`](../app/handlers/commands.py#L701) reads `user_profiles` using the invoking user's global ID and replies in the invoking chat. `register_commands` has no private-chat filter. Hiding `/profile` from the group command menu does not prevent typing it.
- **Scenario and impact:** Someone uses `/profile` in a group to find a setting and publishes facts, beliefs, mood, or insights originally learned in a DM. This is an own-profile disclosure, not evidence that anyone can request another user's profile.
- **Implementation:** Make inspection private-only. In a group, send a short link to the bot's private chat without loading or rendering the profile. Apply the same boundary to future exports, edits, deletion confirmations, and personalized settings. Keep group-specific inspection separate.
- **Done when:** A populated synthetic personal profile never appears in a group response; DM inspection still works; renamed commands and inline entry points obey the same scope.

<a id="priv-02"></a>

### PRIV-02 — Enforce memory-sharing boundaries before constructing prompts

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Existing design risk · Source inspection.**

- **Evidence:** [`handle_message`](../app/services/chat_manager.py#L302) loads the triggering group speaker's global personal profile and includes it in the group LLM prompt. [`build_system_prompt`](../app/prompts/system_prompt.py#L21) asks the model to avoid disclosure; this is a prompt instruction, not a data-access boundary.
- **Scenario and impact:** A group question can cause the model to use a private DM fact. Participant identity separation and JSON serialization already help attribution, but the private information is still available to generation.
- **Implementation:** Introduce explicit memory scope, source chat, owner, sensitivity, and sharing permission. Default legacy personal memory to private. Build group prompts from group-visible information and explicitly approved shared preferences. Treat group-to-personal extraction as a separate consent decision. Filter before provider calls rather than relying on output redaction alone.
- **Dependencies:** Memory provenance and a versioned storage migration. First release can simply omit global personal memory from group prompts.
- **Done when:** Adversarial group questions cannot access private markers because those markers are absent from the provider request. Shared facts appear only in their permitted scope; migration does not silently make old data public.

<a id="priv-03"></a>

### PRIV-03 — Separate operational diagnostics from personal content

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Privacy and retention gap · Source inspection.**

- **Evidence:** [`_trace_routing`](../app/handlers/messages.py#L91) logs names, identifiers, and message previews. [`app` logger setup](../app/__init__.py) writes DEBUG logs to disk. Extraction forwarding includes update counts/field types and actual mood/intensity values, formatted by [`_format_extraction_summary`](../app/services/memory_extractor.py#L85) and sent through [`log_forwarder`](../app/services/log_forwarder.py). LLM audits normally store lengths, but [`_structured_call`](../app/services/llm_service.py) can persist full exception tracebacks, including validation values, in its `error` field.
- **Scenario and impact:** A user shares sensitive text, or a model returns malformed sensitive JSON. That content can enter file logs, audit errors, or the operator's Telegram channel even though ordinary audits are length-only. MongoDB audit TTL does not delete Telegram copies or host backups.
- **Implementation:** Default to metadata-only logging. Add an explicit, short-lived diagnostic mode with redaction and clear operator documentation. Store structured error classes/codes instead of unsanitized provider or Pydantic tracebacks. Separate alert delivery from content export. Bound retention and restrict log-channel access.
- **Done when:** Synthetic secret/personal markers are absent from normal logs, audit failure records, and forwarded alerts, including schema-validation failures. Useful task IDs and error codes remain. Diagnostic mode expires or is visibly enabled.

<a id="priv-04"></a>

### PRIV-04 — Require a clear choice for reset backups and provide actual erasure/export

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Missing essential capability · Source inspection.**

- **Evidence:** [`cmd_reset`](../app/handlers/commands.py#L739) warns about retained shared history and may upload a JSON snapshot to an operator logs channel before deletion. [`reset_user`](../app/database/models.py#L302) deletes the personal profile and private buffer, not all personal references. There is no user-facing export or restore command.
- **Scenario and impact:** Reset is currently a restart of personal memory, not complete erasure. A person may want their data removed rather than an additional operator-held copy. The warning is present; do not describe the current command as secretly promising full deletion.
- **Implementation:** Keep reset, portable export, operator backup, and permanent deletion as distinct operations. Make backup consent explicit and independently configurable. Define how shared transcripts are redacted without deleting other participants' data, how in-flight jobs are invalidated, and which external copies an operator must remove. Add tombstones or durable suppression for erasure so retained group history cannot recreate deleted personal memories.
- **Dependencies:** Scoped provenance, data inventory, deletion lifecycle, and migration tooling. External-copy deletion needs an honest operator procedure; a database command alone cannot guarantee it.
- **Done when:** An authenticated user can export only their permitted data and request deletion. Tests cover shared messages, audits, retries, reset/recreation races, and retained work. Backup refusal does not prevent reset. Restore cannot reactivate a deleted lifecycle without a deliberate new consent step.

<a id="priv-05"></a>

### PRIV-05 — Give users direct control over what is remembered

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Missing essential capability · Source inspection.**

- **Evidence:** [`MemoryExtraction` and `ProfileUpdate`](../app/services/schemas.py) allow health, relationship, mood, beliefs, and inferred gender. Existing controls inspect/reset memory and opt out of check-ins/reactions, but there is no deterministic memory pause, per-category permission, or item-level edit/delete command.
- **Scenario and impact:** A user must rely on a later LLM extraction to understand a correction or forget request. They cannot have an ordinary temporary conversation without persistent memory capture, or reliably remove one mistaken detail without resetting everything.
- **Implementation:** Add stable memory IDs and private inspect/edit/forget actions, a memory pause, and category controls. Favor explicit self-identification over inferred sensitive traits. Make memory and outreach consent independent. Require a usable privacy-control route when capture/outreach is enabled even if operators customize or disable normal commands.
- **Dependencies:** Stable memory identity, provenance, and deletion-generation guards.
- **Done when:** Paused conversations do not modify persistent memory or get replayed into memory later; item deletion cannot be undone by stale background work; a selected category is excluded from capture and retrieval. Existing reset and opt-out commands remain discoverable.

<a id="priv-06"></a>

### PRIV-06 — Make proactive outreach a clear consent and availability setting

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Existing product-policy gap · Source inspection.**

- **Evidence:** The [current README](../README.md#deployment-and-data-behavior) documents default-enabled check-ins for eligible inactive users; [configuration](../app/config.py) enables the scheduler by default and uses one global UTC quiet window. [`cmd_checkins`](../app/handlers/commands.py#L624) supplies a working opt-out. The [check-in prompt](../app/prompts/checkin_prompt.py) already instructs the model to avoid sensitive callbacks and guilt.
- **Scenario and impact:** A person may not expect unsolicited messages based on stored memory. One operator-wide quiet window does not match every user's availability, and prompt instructions alone cannot exclude sensitive data from outreach input.
- **Implementation:** Offer explicit outreach enrollment in the private onboarding/settings flow, independent of memory capture. Store explicit timezone and quiet-window preferences rather than inferring them from location/name. Filter allowed benign memory hooks before generation and provide pause/snooze/frequency controls. Plan a compatibility choice for existing enabled users rather than silently rewriting all preferences.
- **Dependencies:** PRIV-02/05 provenance and category permissions; RUN-04/05 freshness and clock-boundary fixes.
- **Done when:** New users are not enrolled without the chosen clear consent flow, opt-out is honored before delivery, and a personalized quiet window handles DST and minute offsets. Sensitive excluded categories never appear in check-in prompts. Existing opt-outs survive migration and restoration.

## Memory correctness and quality

<a id="mem-01"></a>

### MEM-01 — Capture group owner lifecycles before accepting a transcript snapshot

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P1; confirmed defect; reproduced offline.
- **Evidence:** [app/services/memory_extractor.py:extract_and_trim_group](../app/services/memory_extractor.py), lines 538-554, reads the buffer at 540 and only then invokes `_bounded_group_memories`. That helper's `owner_memory`, lines 295-305, calls `models.prepare_memory_generation` for each owner at 298. [app/database/models.py:prepare_memory_generation](../app/database/models.py), lines 211-214, creates an absent owner's skeleton before returning its newly allocated generation. Contrast the fixed DM ordering at [memory_extractor.py](../app/services/memory_extractor.py), lines 423-435, and [tests/test_memory_concurrency.py:test_dm_generation_precedes_transcript_snapshot](../tests/test_memory_concurrency.py), lines 439-475.
- **Scenario/impact:** A group transcript is read; the participant runs `/reset`; owner generation capture observes the now-missing profile and creates a fresh one; the previously read transcript is then authorized against that fresh token. The synthetic reproduction returned `GROUP_RESET_REVIVAL True ['pre-reset synthetic fact']`. Existing tests cover reset during LLM calls and later retries, but not this initial group read/capture interval.
- **Implementation direction:** Capture trusted owner-generation/reset-epoch information with the source messages at ingestion or with a stable snapshot protocol. A group must identify owners before reading their lifecycles, so simply moving a function call is insufficient without a second validated transcript read, persisted epochs, or equivalent fencing. Keep epochs independent of profile deletion if they serve as reset barriers. Reject any segment/message whose owner epoch predates the current epoch. Preserve legitimate post-reset messages.
- **Dependencies:** PRIV-04 reset semantics; DATA-01 tombstones/epochs; source-message identity work in MEM-08. A narrow regression fix should precede broader storage redesign.
- **Acceptance:** Reset a participant and a shared group between initial raw-buffer read and generation capture, with/without recreation; no pre-reset facts or marker may appear. Exercise reset before first read, during generation initialization, during the LLM await, between participants, and during retries. Genuine fresh messages should still extract.

<a id="mem-02"></a>

### MEM-02 — Validate correction/removal targets and conflicting operations before applying model deltas

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P1; confirmed semantic-integrity defect; unknown-target insertion reproduced.
- **Evidence:** [app/database/models.py:_extracted_memory_fields](../app/database/models.py), lines 513-545, removes matching old facts and then appends every `updated_facts.new_content` even when `old_content` never matched. Beliefs do the same at 548-564; event updates use missing-target fallbacks at 587-617. [app/prompts/extraction_prompt.py](../app/prompts/extraction_prompt.py), lines 29, 43 and 46, asks for existing targets and nonconflicting operations, but code does not enforce those requirements. `save_extracted_memories`, lines 455-470, re-reads the current profile while applying a delta generated against earlier prompt context.
- **Scenario/impact:** A hallucinated correction to a nonexistent item becomes a new fact. A correct old target that another conversation has already changed can also disappear before apply; the stale delta is nevertheless inserted against the latest snapshot. Compare-and-set protects writes from overwriting each other, but does not make the **meaning** of a stale correction valid. Two updates to the same old target can create mutually contradictory replacement facts.
- **Implementation direction:** Introduce stable item IDs and per-item versions; include only authorized IDs in model context and require correction/removal preconditions. Until migration, enforce exact normalized target existence and one operation per target, with explicit conflict outcomes; do not reinterpret a failed correction as an addition. Match category/owner as well as text where relevant. Re-extract or retain the source job when an operation conflicts.
- **Dependencies:** MEM-08 item provenance/versioning; DATA-03 storage migrations; MEM-11 durable processing if partial groups must replay safely.
- **Acceptance:** Missing targets, stale targets, duplicate updates, simultaneous remove+update, and same text in multiple categories cannot create unsupported replacement facts or delete unrelated items. A concurrent valid change survives, and the conflicted source work remains retryable. Reproduction currently prints `UNKNOWN_UPDATE_ADDED ['Synthetic unsupported replacement']`.

<a id="mem-03"></a>

### MEM-03 — Invalidate derived summaries and insights when their evidence changes or is forgotten

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P1; confirmed representation gap by code inspection; distinct from complete account deletion.
- **Evidence:** [app/database/models.py:_extracted_memory_fields](../app/database/models.py), lines 488-623, edits style/gender/mood/facts/beliefs/events but never edits `profile_summary` or `insights`. [app/services/memory_loader.py:compile_memory_text](../app/services/memory_loader.py), lines 45 and 73-78, always emits the existing summary and insight content alongside the updated arrays. [app/services/schemas.py:MemoryExtraction](../app/services/schemas.py), lines 140-151, has no summary/insight invalidation field.
- **Scenario/impact:** A profile summary says “Works at Acme,” backed by a fact. The user says “I left Acme” or “forget where I work.” The fact can be updated/deleted, while the old employer remains in the summary or an insight and continues reaching reply/consolidation prompts. The prompt's statement that current corrections outrank memory reduces response risk but does not remove the retained stale content.
- **Implementation direction:** Derived summaries/insights should record source item IDs/versions. Invalidate or regenerate derived nodes when any cited source changes or is removed. A safe initial implementation can mark the summary/insights dirty, exclude dirty material from prompts immediately, and recompute asynchronously after a meaningful extraction delta. A targeted forgetting tombstone should prevent maintenance from recreating forgotten information from another derived copy.
- **Dependencies:** MEM-08 evidence graph; PRIV-04/05 forgetting/deletion policy; MEM-02 target validation. Do not rely on text substitution alone to remove semantically equivalent copies.
- **Acceptance:** Correct or forget a fact echoed in summary, insight, and event description; subsequent compiled context must contain no stale derived statement. Repeated compression/consolidation cannot resurrect forgotten content. Unrelated derived material remains when its provenance is independently valid.

<a id="mem-04"></a>

### MEM-04 — Give events stable identity independent of description text

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed defect; reproduced offline.
- **Evidence:** [app/database/models.py:_extracted_memory_fields](../app/database/models.py), lines 567-577, deduplicates and removes events only by normalized description. Lines 592-598 select the first old event with that description for updates. Date does not participate in identity. The prompt warns the model to invent distinct supported descriptions for distinct episodes at [app/prompts/extraction_prompt.py:45](../app/prompts/extraction_prompt.py#L45), shifting a storage concern onto generated wording.
- **Scenario/impact:** Two “Visited dentist” events on different dates collapse to one. A later correction/removal by description can affect every matching episode or attach the first episode's date/emotional context to the wrong one. Reproduction added January/February events and printed `DISTINCT_EVENT_DATES_COLLAPSED 1`.
- **Implementation direction:** Use UUID event IDs and source references; separate occurrence identity, display description, and event date. Deduplicate exact source occurrences rather than identical summaries. For legacy migration, retain separate `(description, date)` occurrences and report ambiguous corrections instead of choosing the first. Use provider output IDs only after allowlist validation.
- **Dependencies:** MEM-08 stable item IDs and DATA-03 migration. MEM-02 supplies version-aware correction checks.
- **Acceptance:** Same-description episodes on distinct dates both survive; explicit duplicate source messages do not duplicate an occurrence; updating one event changes only that ID. Unknown/partial dates and recurring episodes are represented without forced invented dates or descriptions.

<a id="mem-05"></a>

### MEM-05 — Support explicit clearing of nullable/profile memory fields

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; intentional schema limitation with a missing corrective capability; high confidence.
- **Evidence:** [app/services/schemas.py:EventUpdate](../app/services/schemas.py), lines 96-105, describes `None` as “keep original”; [models.py](../app/database/models.py), lines 603-606, implements that choice. [app/prompts/extraction_prompt.py:24](../app/prompts/extraction_prompt.py#L24) explicitly acknowledges that null cannot clear an existing date. `ProfileUpdate`, schemas lines 124-136, and models lines 489-496 use null/falsey values as no update, so gender/style cannot be explicitly unset by a model correction. `MemoryExtraction.emotional_state=None` is likewise no operation.
- **Scenario/impact:** The user corrects “That graduation date was a guess; I don't know it” or withdraws previously inferred gender. The existing extraction contract cannot express clearing the incorrect value; a full reset or removing/recreating an entire event is the workaround. This is documented behavior, not a parser regression.
- **Implementation direction:** Model mutation intent separately from the nullable value: typed `set`, `clear`, and `keep` operations or explicit clear-field lists tied to evidence/authorized item IDs. Do not silently change null semantics in place because existing callers/tests use null to preserve fields. Avoid inferring sensitive fields without the PRIV-05 consent/identity policy.
- **Dependencies:** MEM-02 operation validation; DATA-03 versioned schema rollout; PRIV-05 sensitive-memory preferences.
- **Acceptance:** Clear a known event date, communication preference, and gender without deleting unrelated data. Null used as `keep` retains legacy behavior; explicit `clear` removes the supplied field. Unauthorized/quoted requests cannot trigger clearing.

<a id="mem-06"></a>

### MEM-06 — Preserve evidence time and identity for mood observations, and reject stale/replayed moods

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed ordering/freshness gap by code inspection.
- **Evidence:** [app/services/memory_extractor.py](../app/services/memory_extractor.py), lines 440-441 and 545-546, extracts **older** messages while omitting the retained latest window. [app/database/models.py:_extracted_memory_fields](../app/database/models.py), lines 498-510, stamps any extracted mood with the save's `now` and appends it to history without source-message identity or temporal comparison. `EmotionLog`, schemas lines 115-120, contains no source timestamp/ID. Maintenance mood-preservation tests protect compression/consolidation, not the extraction path.
- **Scenario/impact:** A delayed group extraction of an older “I feel sad” message can overwrite a newer happy mood extracted in a DM; compare-and-set retries against the new profile and then assigns the old mood a fresh detection time. A crash or legacy conditional-trim deferral after save can replay the same observation, creating duplicate history and making it appear new. Mood history represents extraction passes rather than unique chronological observations.
- **Implementation direction:** Add validated source message IDs to mood output; resolve `observed_at` from trusted app metadata rather than a model-provided arbitrary timestamp. Store processing time separately. Only replace latest mood when the observation is newer; deduplicate history by source/observation ID and sort by observation time. Explicitly differentiate reported current mood from historic event emotion. Preserve unknown time instead of synthesizing a fresh observation.
- **Dependencies:** GROUP-02 original-message timestamp/batch preservation; MEM-08 provenance; MEM-11 idempotent processing. UTC insertion time is not equivalent to the original sender's local conversational date.
- **Acceptance:** Older group work cannot overwrite a newer DM mood; replay adds no duplicate and does not refresh `observed_at`; delayed batching retains the true supported observation time; historic event emotion stays in the event. Existing maintenance-preservation tests remain green.

<a id="mem-07"></a>

### MEM-07 — Validate complete replacements semantically before destroying populated memory

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P1; confirmed destructive accepted-output case; reproduced offline.
- **Evidence:** [app/services/llm_service.py](../app/services/llm_service.py), lines 602-606 and 629-634, require that all replacement arrays are supplied, but do not reject explicitly empty arrays. [tests/test_memory_prompt_contracts.py:test_explicit_complete_empty_replacement_remains_valid](../tests/test_memory_prompt_contracts.py), lines 166-180, deliberately accepts that result. [app/database/models.py:replace_user_memory](../app/database/models.py), lines 647-669, and `apply_consolidation`, lines 740-767, replace all corresponding arrays unconditionally. The runner's safety branch covers only `None`, at [memory_compressor.py:107-112](../app/services/memory_compressor.py#L107) and [memory_consolidator.py:44-49](../app/services/memory_consolidator.py#L44).
- **Scenario/impact:** A provider emits well-formed complete JSON with all arrays empty despite a populated input. Parsing succeeds and useful memory disappears. This is different from the already-fixed `{}`/missing-array case. The synthetic runner returned `COMPLETE_EMPTY_REPLACEMENT True` after compressing a populated profile.
- **Implementation direction:** Compare source and candidate before commit. Reject/quarantine all-empty replacements of meaningful populated input unless an explicit authorized clear operation explains them. Add reasonable nonblank/output-size/invariant checks and selected critical-item preservation rules; do not pretend a heuristic proves semantic equivalence. Keep a bounded versioned rollback snapshot and record deletion reasons/metrics. Legitimate no-retained-data outputs should remain possible through an explicit policy branch.
- **Dependencies:** MEM-08 item provenance and importance; MEM-10 schema constraints; PRIV-04/05 retention/undo storage policy; TEST-06 semantic evaluation.
- **Acceptance:** Populated input + complete-empty output preserves original memory and records a rejected candidate. Empty input can remain empty. Expected pruning retains critical identity/negation/date/subject information; unsupported added facts, empty content, and reversed meaning fail curated evaluation. Rollback restores an earlier version without reusing obsolete lifecycle tokens.

<a id="mem-08"></a>

### MEM-08 — Preserve provenance, chronology, and uncertainty through extraction and maintenance

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; essential data-model foundation; metadata loss confirmed by code.
- **Evidence:** Facts are assigned `confidence: 1.0` at [models.py:527,541,651,744](../app/database/models.py#L527); no confidence/evidence field exists in `FactExtract` ([schemas.py:24-30](../app/services/schemas.py#L24)). Compression recreates fact/belief/event `created_at` as maintenance time at models lines 647-669; consolidation does the same at 740-765. Both clear event `emotional_context` at 666/759 because replacement events lack an emotion field ([schemas.py:190-193](../app/services/schemas.py#L190)). [memory_loader.py](../app/services/memory_loader.py), lines 58-69 and 77-94, omits fact/belief/insight observation timestamps and source references from model context. Consolidation receives a compiled current profile, not independent original observations ([memory_consolidator.py:36-43](../app/services/memory_consolidator.py#L36)).
- **Scenario/impact:** After maintenance, an old fact appears newly created, all facts carry nominal certainty 1.0, and event emotional context survives only if a model chooses to rewrite it into prose. The consolidator is asked to use chronology and repeated independent evidence, but the representation often no longer contains that information. Original dates cannot support relevance ranking, meaningful undo, traceable corrections, safe sharing, or insight evidence checks.
- **Implementation direction:** Give every memory item an ID, source chat/message IDs, speaker/subject IDs, observation time, processing/revision time, visibility scope, assertion/inference type, and evidence references appropriate to its type. Preserve uncertainty explicitly; stop presenting a constant as calibrated confidence. Maintenance should return source-ID mappings or edits to existing IDs and carry metadata forward deterministically. Preserve event emotions as structured optional fields. Derived insights need multiple independent evidence references; repetitions of an old summary must not count as new evidence.
- **Dependencies:** DATA-03 migration strategy; PRIV-02/05 memory-sharing/consent policy. This enables MEM-02/03/04/06/07/09/13 rather than requiring embeddings first.
- **Acceptance:** Compression/consolidation retain original evidence time, owner/scope, supported date precision/emotion, and uncertainty. A renamed/merged item can be traced to all underlying sources; insights cannot cite themselves or two summaries of the same source as independent evidence. Prompt compilation exposes only authorized provenance needed for the current task.

<a id="mem-09"></a>

### MEM-09 — Enforce a complete memory budget without discarding higher-value data first

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed boundedness defect and documented pruning tradeoff; reproduced offline.
- **Evidence:** [app/services/memory_compressor.py:_enforce_budget](../app/services/memory_compressor.py), lines 51-74, can remove only events, beliefs, and facts, then breaks when those arrays are empty. Summary/style/insights/mood/history still contribute text in [memory_loader.py](../app/services/memory_loader.py), lines 45/52/73-78/100-120; their schema strings have no length limits. Events are removed by array position regardless of significance/date at compressor lines 65-72. The replacement model itself may reorder arrays. [chat_manager.py](../app/services/chat_manager.py), lines 296-315, uses only the group block's compression flag and ignores the sender profile's flag when compiling personal group context.
- **Scenario/impact:** A 2,000-character summary with a 600-character budget causes all facts to be removed while the profile remains oversized; reproduction printed `BUDGET_STILL_EXCEEDED True facts_left 0`. An old major milestone can be dropped before a recent routine/minor item merely because it appears first. A participant active only in groups can keep an oversized personal profile that this reply path never schedules for compression.
- **Implementation direction:** Define per-section and total compiled-budget limits, including fixed template overhead. Fit or safely omit/shorten irreducible fields before deleting every structured item. Return an explicit `fitted/unfittable/conflicted` outcome. Rank candidates deterministically using retained significance, evidence recency, explicit pinned user facts, and selected relevance; never use index 0 as an unsupported claim of oldest date. Trigger maintenance for oversized participant profiles or enforce a safe retrieval view on read. LLM-03 covers separate total request/token budgets including persona/history/schema; character thresholds are not that guarantee.
- **Dependencies:** MEM-08 retained metadata; MEM-10 field bounds; MEM-14 outcomes/retry admission; LLM-03 total-context controls.
- **Acceptance:** Oversized summary, style, insights, mood trigger/history, and combinations either fit the configured budget or produce a documented safe failure without dropping all useful facts. Preserve major/pinned items over less valuable ones. A group-only user cannot repeatedly send an oversized profile into requests. Conflict preserves newer content.

<a id="mem-10"></a>

### MEM-10 — Make structured-output validation enforce the documented field constraints

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed validation omissions; high confidence.
- **Evidence:** `EventExtract.date`, `EventUpdate.date`, and `CompressedEvent.date` are nullable strings at [schemas.py:74-84,96-102,192](../app/services/schemas.py#L74); no calendar validator exists. `EmotionLog.intensity`, line 119, has no `ge/le` or finite-number constraint. Memory text fields and operation arrays have no size/nonblank bounds throughout lines 24-151 and 179-233. Only `GroupMemoryExtraction` forbids extra top-level fields (165); other memory models default to ignoring extras. Prompts ask for ISO precision, bounded intensity, one short insight sentence, atomic facts, and English content, but `_structured_call` checks shape/Pydantic types rather than those semantic rules.
- **Scenario/impact:** A valid-looking result may persist `today`, impossible dates such as `2026-02-30`, out-of-range/NaN intensity, whitespace facts, oversized summaries, huge arrays, or unrecognized ignored fields. Bad values later pollute prompts, trigger ineffective compression, or conceal provider contract drift. Date preservation should support year/month/day precision rather than forcing a full day.
- **Implementation direction:** Add shared strict memory-model configuration, finite bounded intensity, trimmed nonblank bounded strings, bounded list lengths, and precision-aware real-calendar date validation. Use explicit enums/structured date precision where helpful. Validate owner IDs and mutually exclusive operation targets. Enforce only invariants machines can establish; translation, faithful meaning, evidence attribution, and diagnosis restraint require curated semantic evaluation, not a regex claiming to establish correctness.
- **Dependencies:** DATA-03 versioned rollout/legacy repair; MEM-05 explicit clear-vs-keep intent; MEM-07 candidate semantic checks.
- **Acceptance:** Reject invalid calendars/relative dates while accepting supported YYYY/YYYY-MM/YYYY-MM-DD and leap-day cases. Reject nonfinite/out-of-range intensity and empty/oversized content or arrays. Unknown fields cannot be silently mistaken for a successful known mutation. Public provider-compatible JSON/native schemas advertise the same rules. Malformed output preserves existing memory and is logged in redacted form.

<a id="mem-11"></a>

### MEM-11 — Make extraction admission, apply, and trim replayable and idempotent

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; documented lossy tradeoff plus missing recovery capability; high confidence.
- **Evidence:** [memory_extractor.py](../app/services/memory_extractor.py), lines 498-509 and 666-676, intentionally trims the final attempted segment after three `None` results. Group saves, marker write, and trimming are separate operations at 584-652. [models.py:add_message_to_buffer](../app/database/models.py), lines 335-347, evicts oldest messages at a count hard cap even without extraction. No persisted extraction job/attempt/application ID exists in these paths. Existing snapshot-ID trimming is correct and should be retained.
- **Scenario/impact:** A provider outage can discard unique unextracted facts although the buffer already has a count cap. A crash after saving one participant but before others/trim causes partial apply and re-extraction. Fact text dedup prevents some duplicates, but mood/history and generated corrections are not generally idempotent, and source progress is not auditable. The current product intentionally prefers bounded availability; stronger durability requires a retention/cost decision.
- **Implementation direction:** Before volatile history eviction, persist a bounded source segment/job with message IDs, owner epochs, attempt state, applied-owner receipts, and retry deadline. Mark immutable source IDs as applied; make per-owner mutation idempotent and commit progress before exact trim. Retry failed segments with capped backoff/dead-letter policy rather than unconditional trim. Bound durable backlog by an explicit age/size/privacy policy and distinguish clean no-memory extraction from unavailable/invalid output. A full distributed queue is unnecessary for a first single-process crash-safe implementation.
- **Dependencies:** MEM-01 epoch fencing; MEM-06 mood IDs; MEM-08 item provenance; DATA-05 retention; RUN-02/03 and LLM-01 admission design.
- **Acceptance:** Crash/fail between each group save, marker, and trim; restart applies each observation once and completes all authorized owners. An outage cannot silently erase a replayable pending segment. Reset invalidates old jobs, and poison jobs cannot block later conversations indefinitely. Hard-cap and snapshot-trim tests continue passing.

<a id="mem-12"></a>

### MEM-12 — Address participants by trusted IDs rather than display names in extraction output

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P3; sound current safety tradeoff with missing capability; high confidence.
- **Evidence:** [memory_extractor.py:_build_name_id_map](../app/services/memory_extractor.py), lines 218-242, permanently excludes a normalized name shared by multiple IDs. `_bounded_group_memories`, lines 317-335, supplies unique participants; returned names are resolved at 598-608. `GroupMemoryUpdate.participant`, schemas lines 155-160, contains only a display-name string despite input transcript IDs. [tests/test_group_extraction.py:test_duplicate_display_names_are_skipped_without_exposing_their_memories](../tests/test_group_extraction.py), lines 300-386, correctly verifies nonmisattribution.
- **Scenario/impact:** Two Alex participants with no unique aliases cannot receive extraction updates, even though every transcript entry has a trusted distinct sender ID. Their output is skipped and the successful segment is trimmed. Case/whitespace name collisions produce the same loss. This is preferable to assigning another person's data; do not weaken the collision guard.
- **Implementation direction:** Replace name-addressed output with participant ID fields validated against human IDs in the segment, or app-generated opaque owner keys mapped to IDs. Names remain presentation-only data. Preserve assistant exclusion, unknown-ID rejection, and cross-owner isolation. For migration, accept legacy names only when unambiguous.
- **Dependencies:** MEM-10 strict schemas; MEM-08 source IDs/owners; PRIV-02 scope rules.
- **Acceptance:** Identical display names both get their own valid facts without exposing the other's private context. Unknown IDs, bot IDs, forged content IDs, and mismatched owner keys are rejected. Rename/alias changes do not lose attribution. Existing collision-protection tests are expanded, not removed.

<a id="mem-13"></a>

### MEM-13 — Retain corrective intent when group memory context cannot fit

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; intentional safety rule with a functional/correction gap; high confidence.
- **Evidence:** `_bounded_group_memories`, [memory_extractor.py:293-335](../app/services/memory_extractor.py#L293), allocates the budget to the shared group first, then first-seen participant order; each profile is included whole or omitted. `_only_new_memory`, lines 349-364, drops all update/removal operations for omitted owners. Group path invokes it at 579-580 and 610-612, then trims the processed snapshot at 651-653. [tests/test_extraction_context.py:test_group_memory_budget_keeps_new_information_without_unseen_key_edits](../tests/test_extraction_context.py), lines 315-494, explicitly verifies that such corrections/removals are discarded.
- **Scenario/impact:** Later speakers or individually oversized profiles repeatedly lose correction/removal capability. A genuine “forget/change what you remember about my employer” request is processed as a successful extraction with its destructive operations suppressed, and its transcript can disappear. This is safe against guessing keys, but a budget-driven omission should not silently mean the user's correction is fulfilled.
- **Implementation direction:** Preserve app-identified corrective work as a deferred task and perform a targeted per-owner pass with scoped relevant items/IDs; include compact authorized item keys separately from verbose memory summaries. Rotate or reserve fair owner budgets so first speakers/group context do not always win. Separate omitted-context, invalid-owner, policy-denied, and reset outcomes. Do not remove the unseen-key protection merely to accept more model output.
- **Dependencies:** MEM-02 stable target validation; MEM-08 provenance/scoped retrieval; MEM-11 durable intent; PRIV-05 targeted forgetting semantics.
- **Acceptance:** A correction from the last participant in a large group is either safely applied against known targets or explicitly deferred with surviving evidence; it cannot be silently lost on trim. Oversized single owners still receive targeted corrections. Total supplied context stays within budget, and another owner's facts remain isolated.

<a id="mem-14"></a>

### MEM-14 — Track maintenance outcomes and fairly retry skipped/conflicted work

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; missing scheduling/result contract; high confidence.
- **Evidence:** [user_task_manager.py:run_extractor/run_compressor/run_consolidator](../app/services/user_task_manager.py), lines 601-639, returns immediately when memory locks are held; compressor cooldown advances at 623 even when the internally swallowed compression result is failure/conflict. [memory_consolidator.py](../app/services/memory_consolidator.py), lines 44-64, returns after failed/stale results without setting a durable retry outcome; [memory_compressor.py:107-119](../app/services/memory_compressor.py#L107) similarly returns. [health.py:_run_consolidation_scan](../app/services/health.py), lines 240-247, increments `processed` after every returned dispatch, including skips/failures. [models.py:find_users_due_for_consolidation](../app/database/models.py), lines 700-712, takes the first qualifying natural-order records with no fairness cursor or failure backoff.
- **Scenario/impact:** An extractor skipped while another maintenance job holds the lock may wait for a future message to be rescheduled. Group-only participant profiles do not necessarily get personal compression admission. Persistently failing/busy early profiles can consume the first scan batch repeatedly while later profiles wait. The log says processed even if nothing applied, obscuring this. Existing CAS skipping correctly preserves newer content; the gap is eventual follow-through, not stale-write protection.
- **Implementation direction:** Return typed outcomes (`applied`, `busy`, `conflicted`, `failed`, `no_work`, `reset`) from workers. Retain a bounded pending flag/job for work skipped during a lock and retry after the holder releases. Maintain separate success cooldown and failure retry/backoff. Use a due-work cursor/order plus capped retry_at to avoid starving later records. Record skipped/conflicted/fitted metrics and oldest pending age; persist only the minimal needed admission state for crash recovery.
- **Dependencies:** MEM-11 durable jobs; DATA-04 due query design; RUN-10 and LLM-01 concurrency controls. Avoid adding unbounded retry tasks around the existing lock.
- **Acceptance:** A lock conflict eventually executes the pending extractor/compressor without requiring a new user message. Failed compression retries on the failure policy rather than claiming success. Permanent failures at the head do not starve later due users. Logs/counters distinguish dispatched, applied, failed, and skipped. Reset cancels obsolete pending work.

## Persistence, lifecycle, and retention

<a id="data-01"></a>

### DATA-01 — Make reset/delete lifecycle state durable and resilient to partial deletion

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed non-atomic local reset and stronger-lifecycle capability gap.
- **Evidence:** [models.py:reset_user](../app/database/models.py), lines 302-305, deletes the profile and then the private buffer in separate operations. `db_session`, [connection.py:41-44](../app/database/connection.py#L41), is a shared-client context manager, not a transaction. Generation state resides only in the profile and is deleted with it (`ensure_user:111`, `prepare_memory_generation:212-226`). PRIV-04 covers the documented retained group/log/backup scope; docs already say later group extraction can create a profile again ([docs/development/database.md:132](../docs/development/database.md#L132)). `cmd_reset`, commands lines 754-797, serializes DM reset work but does not fence every group job touching that principal.
- **Scenario/impact:** If the second delete fails, the profile is gone but its private transcript remains. A subsequent memory initialization can replay it under a fresh lifecycle. Retained older group transcripts can also relearn forgotten information in a later extraction; the current reset warning discloses retained history, so this needs an explicit policy choice rather than calling the warning dishonest. Deleting the profile also discards opt-outs/preferences; recreation defaults them back to enabled.
- **Implementation direction:** Separate durable principal lifecycle/consent/preference metadata from the disposable memory profile. Persist a reset/delete epoch and cutoff before deleting content; fence all writes and pending source segments against that epoch. Make deletion idempotent with a progress record/retry and explicit incomplete outcome, or use a transaction where supported with equivalent standalone fallback. Decide whether local reset retains privacy preferences; do not silently reenroll a user in sensitive/proactive features. Tombstones need their own minimal retention policy.
- **Dependencies:** PRIV-04/05/06 deletion/reset/consent requirements; MEM-01 group race; MEM-11 jobs; DATA-05 retention. Group-message erasure must preserve other participants' records without leaking the requesting user's source facts.
- **Acceptance:** Inject failure before/after each collection deletion, restart, and rerun; deletion converges and obsolete content cannot be reintroduced. Reset during every group/DM maintenance phase invalidates old jobs. New explicitly authorized interaction starts a fresh lifecycle. Opt-outs behave according to the documented choice, and retained minimal tombstone fields contain no conversation content.

<a id="data-02"></a>

### DATA-02 — Define versioned, complete exports and a tested restoration format

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; essential portability/recovery capability; concrete export limitations confirmed.
- **Evidence:** [models.py:export_user_data](../app/database/models.py), lines 281-299, calls its result everything stored for a user but exports only one personal profile and one private buffer, returns `None` when no profile exists even if a buffer remains, and reads the two documents independently. Associated `chat_members` records include user IDs at models lines 1037-1095; group buffer sender records/audits are separate. `cmd_reset`, commands lines 760-775, serializes datetimes/ObjectIds with `default=str` and uploads a JSON backup. The user-facing note at 799 says an admin can help restore, but no importer or versioned restore contract is implemented in these paths.
- **Scenario/impact:** A partial-reset orphan buffer is omitted; group membership/preferences and attributed group source records are missing from a portable account snapshot. Concurrent group writes can change the profile between the profile and buffer reads despite the held DM reset lock. A manually imported JSON snapshot may restore datetime strings as strings and reuse a stale generation token, potentially authorizing obsolete jobs.
- **Implementation direction:** Specify export scope with a versioned manifest, typed timestamps/IDs, collection counts, lifecycle/version references, and explicit exclusions for shared or third-party data. Export associated principal data under the selected PRIV policy without copying others' private content. Take a consistent database snapshot or detect/retry revision changes. Build a controlled importer that validates/repairs types, resolves merge/replace choices, regenerates lifecycle tokens, and has a dry-run diff. Test actual recovery from the exported artifact.
- **Dependencies:** PRIV-04 self-service export/access rules; DATA-01 lifecycle fencing; DATA-03 schema versions; DATA-05 backup retention/consent.
- **Acceptance:** Orphan private buffers and chosen associated records appear in a versioned export. Round-trip restores datetime semantics, date precision, preferences, source IDs, and compiled behavior, with a new lifecycle token. Corrupt/foreign-owner/missing-version imports are rejected safely; concurrent changes are detected; export does not reveal other people's content.

<a id="data-03"></a>

### DATA-03 — Version persistent documents and provide explicit migrations/repair tooling

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; essential persistence-maintenance capability; high confidence.
- **Evidence:** [models.py:ensure_user/_ensure_memory_skeleton](../app/database/models.py), lines 97-197, create loosely structured dicts without a schema version; existing documents acquire `memory_generation` lazily at 217-225. New/legacy buffer entry handling differs at lines 339 and 423-438. [memory_loader.py:compile_memory_text](../app/services/memory_loader.py), lines 28-31/58-94/112-119, assumes list entries are mappings; `_extracted_memory_fields`, models lines 515/517/550/572, assumes required string keys. [connection.py:init_db](../app/database/connection.py), lines 53-72, configures indexes but no collection validators or migration mechanism. The repository inventory contains no versioned migration runner.
- **Scenario/impact:** Deploying new fields relies on `.get` defaults and scattered lazy repair; malformed or partly restored legacy entries can break prompt compilation, updates, or scheduling. A months-long sequence of stable IDs, observation times, scope changes, and renamed schemas will make untracked storage evolution harder to recover or roll back. Pydantic validates model output, not all already-stored documents.
- **Implementation direction:** Add per-document schema versions and a reviewed migration manifest with idempotent batches, progress, dry runs, backups, and compatibility windows. Validate/normalize documents at persistence boundaries; quarantine malformed entries with redacted diagnostics instead of crashing an entire user's reply. Migrate source IDs, UTC datetimes, stable item IDs and scope conservatively; avoid assigning invented observation dates or treating missing metadata as confirmed. Add MongoDB validators after backfill where practical. OPS-06 covers the Motor-to-supported-driver migration; coordinate rather than make it an unrelated change.
- **Dependencies:** DATA-02 export/restore; MEM-08 target schema; DEV-01 release/rollback and OPS-06 driver upgrade.
- **Acceptance:** Run migration twice against mixed old/new/corrupt data; it is idempotent, restartable, bounded, and reports unresolved records. Old and new app versions have a documented compatibility window. Invalid embedded arrays/datetimes cannot silently become invented content; known good records remain usable. Real MongoDB integration verifies the actual validators/update semantics.

<a id="data-04"></a>

### DATA-04 — Index and bound due-work queries, including eligibility and fair scan progress

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; confirmed scaling gap; high confidence without a production load estimate.
- **Evidence:** [connection.py:init_db](../app/database/connection.py), lines 58-68, creates only audit-log secondary indexes. [models.py:find_users_due_for_consolidation](../app/database/models.py), lines 692-713, queries `last_consolidated_at`, transfers facts/beliefs/events arrays, computes item count in Python, and stops only after `limit` qualifying profiles. `find_users_due_for_proactive`, lines 1001-1029, similarly queries engagement/preference timestamps then transfers arrays/counts/filters unanswered in Python. A return limit is not a cap on documents examined, bytes transferred, or work: many nonqualifying records can be read before a qualifying user is found.
- **Scenario/impact:** A large population of empty/low-item profiles or paused users makes each scan traverse many records/large arrays. Missing profile indexes aggravate the cost. Unsorted natural-order selection contributes to the maintenance starvation issue in MEM-14. Mongomock portability is a test convenience, not a reason to keep the production query inefficient.
- **Implementation direction:** Maintain trustworthy item_count/eligibility/next_due_at fields atomically with memory/preferences, or use appropriate aggregation predicates on the deployed MongoDB version. Add measured indexes matched to the final query/order, query only small projected fields, and cap scan pages with a resumable cursor. Use fair explicit ordering/retry deadlines instead of repeatedly taking the same natural-order head. Verify query plans on real seeded MongoDB before selecting an index design; don't blindly add one compound index for every optional field.
- **Dependencies:** MEM-14 outcomes/fairness; DATA-03 count/eligibility backfill; TEST-02/05 integration/load harness.
- **Acceptance:** Explain plans and load fixtures with many ineligible and large-profile records show bounded page work and low projected bytes; chosen indexes are used. Every eligible user is eventually considered despite head failures. Derived counts stay correct after extraction/replacement/deletion/import, and schema repairs do not admit invalid eligibility.

<a id="data-05"></a>

### DATA-05 — Reconcile and verify retention rather than treating index creation as success

- [ ] Implemented and verified, or resolved with a recorded decision.

- **Priority/classification/confidence:** P2; documented retention-policy/enforcement gap; high confidence.
- **Evidence:** [connection.py:init_db](../app/database/connection.py), lines 64-70, calls `create_index` with a fixed name and requested `expireAfterSeconds`, catches any failure, and still logs initialization success at 72. Changing TTL options on an existing same-name index can fail; the code does not inspect/reconcile actual options or expose retention readiness. [docs/development/database.md:16](../docs/development/database.md#L16) correctly acknowledges this. Other collections receive no age retention. Buffers are count-capped per chat, profiles/member/settings records persist indefinitely, and optional Telegram reset backups have no application-managed expiry in the inspected code.
- **Scenario/impact:** An operator reduces audit retention but the old live TTL policy remains, or TTL creation fails and audit records persist indefinitely while startup succeeds. Bounded entries per chat do not bound total inactive chats/principals or the age of sensitive raw snippets. Memory compression is not a deletion/retention policy. This needs an explicit product data-lifetime decision; no unsupported legal-compliance claim is implied.
- **Implementation direction:** Inspect live index options and reconcile retention changes through supported index modification, with controlled rebuild when needed. Expose the effective retention policy and unhealthy enforcement state in readiness/ops diagnostics. Add explicit collection/source/job/tombstone/backup age policies with safe ownership-aware purge jobs; consider simple idle-buffer TTL only after recovery/privacy semantics are decided. Record purge metrics and a last-success checkpoint. PRIV-04 defines user-facing disclosure and external-copy policy; PRIV-03 covers raw audit-error redaction ([llm_service.py:515-520,98-106](../app/services/llm_service.py#L515)).
- **Dependencies:** PRIV-04/05 retention/consent; MEM-11 durable sources; DATA-01 tombstone lifecycle; real MongoDB integration harness.
- **Acceptance:** Create an old TTL policy, change configuration, restart, and verify effective live options and eventual expiry in real MongoDB. Permission/conflict failures show a degraded retention state rather than a false success. Purging an inactive principal or source segment preserves other users' data and cannot resurrect deleted memory. Backup retention follows the documented controlled process.

**Index reconciliation detail:** Provide an operator migration/preflight command that compares the effective named TTL index to configured retention and supports a non-mutating dry-run. Do not silently drop/rebuild indexes during ordinary startup. Exercise first creation, matching/mismatched intervals, permission failures, and eventual expiry on real MongoDB. Document asynchronous expiry rather than promising deletion at an exact instant. This item consolidates the audit-index deployment and broader data-lifetime work.

## Conversation runtime and delivery

<a id="run-01"></a>

### RUN-01 — Distinguish generated replies from delivered conversation history

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P1, confirmed behavioral defect during delivery failure.

**Evidence:** `chat_manager.handle_message` appends the capped assistant text before returning ([app/services/chat_manager.py:376](../app/services/chat_manager.py#L376), `385`). `UserTaskManager._process_batch` then calls Telegram ([app/services/user_task_manager.py:489](../app/services/user_task_manager.py#L489), `492`); delivery errors are caught at `:510` and `:564`. The generated assistant entry is not marked failed or removed, while the generic error message sent at `:571` is not recorded.

**Trigger/impact:** Generation succeeds, but Telegram rejects or times out on delivery, or the process stops between the database append and send. Future prompts include a reply the user may never have seen; a delivered error apology may be absent from history. Memory extraction can incorporate the apparent unsent assistant turn.

**Implementation:** Introduce a reply record with generation/delivery status and Telegram message IDs. Treat only delivered assistant text as normal conversation history. Add a small persistent outbox or delivery ledger before send, then update it after acknowledgement; handle ambiguous transport outcomes explicitly. Simply moving the database append after send reduces phantom turns but still has a send-success/database-failure gap. Do not promise exactly-once Telegram delivery without a supported upstream idempotency contract.

**Dependencies:** Conversation/message identifiers; RUN-06 delivery result contract. Persistent recovery also depends on RUN-02.

**Acceptance/tests:** Simulate permanent send rejection, transport timeout, send-success/database-failure, process stop before send, and successful retry. Only acknowledged delivered content enters normal history. Error replies have an explicit history policy. A failed delivery never triggers a second expensive generation merely to retry sending.

<a id="run-02"></a>

### RUN-02 — Make restart losses an explicit admission and backlog policy

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P1, missing durability capability plus deliberate realtime tradeoff.

**Evidence:** Pending messages and tasks live in `UserState.pending_messages` and process dictionaries ([app/services/user_task_manager.py:65](../app/services/user_task_manager.py#L65), `89`, `306`). The batch is removed from the pending list before work starts (`:406`). User text is appended to MongoDB only inside `handle_message` ([app/services/chat_manager.py:230](../app/services/chat_manager.py#L230)), so queued accepted text can be lost before that point. Startup intentionally calls `delete_webhook(drop_pending_updates=True)` ([main.py:100](../main.py#L100)), independently of the configurable stale-message cutoff.

**Trigger/impact:** A crash/redeploy drops messages already received but still batching, and startup discards the entire Telegram pending backlog, including recent messages. Graceful shutdown reduces this risk but does not make admission durable. This is documented intentional behavior, not an accidental startup API misuse.

**Implementation:** Decide and document whether ThinkMate promises best-effort realtime handling or recovery of admitted messages. For a durable promise, persist update/message IDs and payload references before acknowledging admission, maintain per-chat ordering, and replay unfinished work with lifecycle/deletion guards. Expose backlog dropping as an explicit configuration choice; when replay is enabled, apply an age policy while processing rather than unconditionally deleting every pending update. Keep the existing default if realtime semantics are intentional until recovery is implemented.

**Dependencies:** RUN-01 delivery ledger; duplicate/update-ID handling; DATA-01 and PRIV-04 reset/deletion lifecycle semantics.

**Acceptance/tests:** Restart after enqueue, after user-buffer append, after generation, and during send. Demonstrate bounded duplicate effects and stable per-chat ordering. Test both configured backlog modes and distinguish their semantics from `STALE_MESSAGE_SECS`.

<a id="run-03"></a>

### RUN-03 — Return admission outcomes and handle a full chat queue explicitly

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, confirmed silent rejection/design gap.

**Evidence:** `enqueue_message` returns without a result when closing or full ([app/services/user_task_manager.py:282](../app/services/user_task_manager.py#L282), `299`). The full queue emits only a log and counter. Group routing marks ambient/implicit cooldowns before awaiting admission ([app/handlers/messages.py:233](../app/handlers/messages.py#L233), `568`), and the successful-gate path intentionally does not buffer the message itself (`:241`, `514`).

**Trigger/impact:** A slow generation holds a chat busy while its pending queue fills. New direct messages vanish without a busy notice; a gated group message rejected by admission is neither queued nor buffered, despite the intended single-write routing contract. Its routing cooldown can still be consumed.

**Implementation:** Return a typed admission result such as accepted/full/closing/reset-rejected. Define one bounded busy notice per private-chat congestion episode. For ambient group traffic, decide whether a rejected candidate should be buffered without generation; coordinate the exactly-one-buffer-write rule. Preserve the existing pre-admission cooldown reservation where it prevents concurrent spam, but make rejected admission handling explicit rather than blindly rolling it back.

**Dependencies:** RUN-02 if durable admission is adopted; LLM-01 global queue policy.

**Acceptance/tests:** Fill a queue during an active reply and verify the admission result, notice suppression, recovery notice policy, per-chat limit, and one buffer write for each accepted/retained group message. Closing must reject without starting new tasks.

<a id="run-04"></a>

### RUN-04 — Revalidate activity and eligibility before proactive delivery

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P1, confirmed remaining proactive freshness defect.

**Evidence:** Private messages only enqueue at [app/handlers/messages.py:321](../app/handlers/messages.py#L321); `last_interaction_at` changes later inside the batch worker ([app/services/chat_manager.py:329](../app/services/chat_manager.py#L329)). The command middleware resets `proactive_unanswered` but does not record interaction time ([app/handlers/middlewares.py:235](../app/handlers/middlewares.py#L235); [app/database/models.py:964](../app/database/models.py#L964)). The proactive pre-send check observes lifecycle, opt-out, and `last_interaction_at` ([app/services/health.py:324](../app/services/health.py#L324)), not pending message state or a command activity revision. Proactive tasks are owned for cancellation but do not take the ordinary `chat_lock`. The scan captures the due-user list once ([app/services/health.py:395](../app/services/health.py#L395)); each sender later reloads the profile ([app/services/health.py:306](../app/services/health.py#L306)) and checks opt-out, but does not recheck whether inactivity, minimum interval, or unanswered eligibility still holds at the actual attempt time.

**Trigger/impact:** A user sends a DM that remains queued behind another slow reply, or uses `/help` while a check-in is generating. The stored last-interaction value can remain unchanged, so an obsolete unsolicited check-in is sent despite current engagement. A separate stale-candidate case occurs when a later candidate interacts while an earlier candidate's check-in is generating. Its fresh profile timestamp is then adopted as the later sender's baseline; no subsequent change is needed for the obsolete check-in to pass the final equality check. Existing tests cover committed activity changes during generation, not all intake or pre-attempt eligibility cases.

**Implementation:** Revalidate all due/consent eligibility against the current profile and actual clock immediately before reserving an attempt, then record an activity revision/timestamp on accepted private-message and relevant command intake. Carry the previous interaction timestamp separately for temporal prompt context so moving activity accounting does not erase the last-talked gap. Check the revision and pending/running direct-message state before proactive send, and coordinate final delivery through the same conversation scheduling policy. A remote send already in flight cannot be recalled; define that boundary explicitly.

**Dependencies:** RUN-03 admission result; shared conversation scheduling/delivery contract. Preserve current snapshot-conditional streak updates and reset cancellation.

**Acceptance/tests:** Generate a check-in, enqueue a new DM without allowing its worker to run, then finish generation: no check-in should send. Repeat with `/help`, an active direct reply, opt-out, and reset. Hold candidate A's generation, commit activity for later candidate B, then release A: B must fail renewed inactivity eligibility without requiring another message during its own generation. Recheck minimum interval and unanswered limits as well. Confirm temporal prompts retain the correct prior gap and no command or message can overwrite a later streak reset.

<a id="run-05"></a>

### RUN-05 — Recheck quiet hours using current time at delivery

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, confirmed clock-boundary defect.

**Evidence:** `_run_proactive_scan` captures `now` and tests quiet hours once ([app/services/health.py:388](../app/services/health.py#L388), `389`), then sequentially awaits every candidate (`:404`, `415`). `_send_proactive_checkin` neither rechecks quiet hours nor refreshes that clock before sending (`:319`, `335`); the scan's same `now` is used for every attempted user's window timestamp.

**Trigger/impact:** A scan begins shortly before quiet hours; slow LLM calls carry delivery into the quiet interval. Later users get timestamps anchored to scan start, potentially hours before their attempts. The existing global UTC quiet-window design is deliberate, but the boundary-crossing behavior is separate from timezone personalization.

**Implementation:** Inject a clock, re-evaluate quiet hours immediately before generation/admission and again before delivery, and use each attempt's actual start time for rate-window claims. Stop/defer the remaining scan when the window closes. User-local quiet hours can be a later feature rather than a prerequisite for fixing this boundary.

**Dependencies:** RUN-10 scheduler job model; RUN-04 final-send activity checks.

**Acceptance/tests:** Advance a fake clock across quiet-start during generation and between candidates; no new delivery occurs in quiet hours. Test midnight-wrapping windows, equal start/end, and a later candidate's independently recorded attempt time.

<a id="run-06"></a>

### RUN-06 — Add typed delivery results, targeted retries, and chunk progress

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing transport recovery and confirmed partial-result blind spot.

**Evidence:** `send_message_text` retries every `TelegramBadRequest` from the first reply as an unthreaded answer ([app/services/telegram_text.py:103](../app/services/telegram_text.py#L103), `106`), not just a missing/deleted reply target. Both text helpers send chunks sequentially and return only the first result (`:99`, `110`, `129`, `135`). They do not expose completed chunk IDs or retry transient/rate-limit errors; `_process_batch` turns other failures into a generic apology ([app/services/user_task_manager.py:564](../app/services/user_task_manager.py#L564)).

**Trigger/impact:** A Telegram rate limit or transient send failure discards an already generated reply. With `MAX_RESPONSE_CHARS` increased/disabled or a long command output, chunk one may deliver and chunk two fail; the caller cannot distinguish zero from partial delivery. Retrying the entire response can duplicate chunks. An unrelated malformed-request error also incurs the same failing answer attempt.

**Implementation:** Define a delivery result containing message IDs, successful chunk count, and classified outcome. Retry only known transient failures within a bounded budget, honor Telegram retry delays, and resume from the failed chunk when acknowledgement is unambiguous. Restrict reply-to-answer fallback to errors indicating a missing/invalid reply target. Treat unknown timeout outcomes as ambiguous rather than silently assuming nothing sent.

**Dependencies:** RUN-01 delivery state and outbox; cancellation/reset ownership must remain intact.

**Acceptance/tests:** Missing original message falls back once; malformed content does not use reply fallback; forbidden fails permanently; retry-after delays are honored; later-chunk failure reports partial success; resume does not resend acknowledged chunks. Keep existing whitespace/code indentation and HTML fallback tests.

<a id="run-07"></a>

### RUN-07 — Start cache/gate pruning even when no replies are enqueued

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, confirmed maintenance startup defect.

**Evidence:** The sweeper starts lazily only on `enqueue_message` ([app/services/user_task_manager.py:205](../app/services/user_task_manager.py#L205), `295`). Its eviction hook is the code that prunes affinity and group gate maps (`:242`). Quiet/non-chiming group traffic loads the affinity cache and updates ambient/implicit/spam maps ([app/handlers/messages.py:169](../app/handlers/messages.py#L169), `193`; [app/services/group_gate.py:547](../app/services/group_gate.py#L547), `704`, `927`) but returns without enqueueing ([app/handlers/messages.py:219](../app/handlers/messages.py#L219), `229`). Gate `prune` methods are hooks, not automatically invoked from every decision.

**Trigger/impact:** Since process startup, the bot receives only non-addressed/quiet group traffic and never queues a reply. Member/chat state accumulates and stale entries are never swept. The code has working pruning logic, but its starting condition does not cover all producers.

**Implementation:** Start a maintenance sweeper explicitly during `main` initialization or ensure it starts whenever routing creates gate/cache state. Own it through the existing lifecycle manager and cancel it during shutdown. Add hard cache/state capacity bounds after measuring load; TTL alone still leaves all active entries resident.

**Dependencies:** Existing `UserTaskManager` lifecycle; LLM-01/global admission if hard active-state limits are added.

**Acceptance/tests:** Feed only quiet/non-chiming traffic from many distinct members, without any enqueue. Advance time, run the sweep, and verify all eligible idle maps are reclaimed. Confirm active locked affinity entries and active conversations remain safe, and startup/shutdown create no orphan sweeper.

<a id="run-08"></a>

### RUN-08 — Keep optional reactions from delaying or contradicting a current preference

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, deliberate sequencing tradeoff and remaining preference freshness gap.

**Evidence:** `_process_batch` awaits the optional Telegram reaction before sending the text ([app/services/user_task_manager.py:462](../app/services/user_task_manager.py#L462), `489`). Reaction errors are isolated correctly, but a slow call still delays the reply and holds `chat_lock`. The reaction opt-out is read from the profile before LLM generation ([app/services/chat_manager.py:304](../app/services/chat_manager.py#L304), `325`) and applied after generation without checking for a newer preference (`:404`).

**Trigger/impact:** A slow reaction endpoint delays an otherwise ready reply. A user issues `/reactions off` during a long generation, but the earlier preference snapshot can still permit its reaction. Current normalization and exception isolation are already useful and should remain.

**Implementation:** Deliver conversational text first, then perform the reaction with a short bounded deadline under owned conversation work. Revalidate reaction preference using an intake-updated preference revision/cache or a narrow read immediately before applying it. Avoid unowned fire-and-forget tasks that would escape reset/shutdown.

**Dependencies:** RUN-01 delivery acknowledgement; RUN-04 intake revisions/command coordination.

**Acceptance/tests:** A reaction call that stalls or fails must not delay text delivery. Toggling the preference during generation suppresses the later reaction. Reset/shutdown cancels pending reaction work. Existing FE0F and allowlist tests keep passing.

<a id="run-09"></a>

### RUN-09 — Align the complete shutdown budget with the deployment grace period

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing explicit deployment contract; not a claim that cleanup is absent.

**Evidence:** `main` has many separate 15-second cleanup windows ([main.py:34](../main.py#L34), `123`, `124`, `125`, `126`, `127`, `130`, `141`, `142`, `144`). Handler/conversation drains each allow 10 seconds and the audit drain allows 5 ([app/handlers/middlewares.py:43](../app/handlers/middlewares.py#L43); [app/services/user_task_manager.py:174](../app/services/user_task_manager.py#L174); [app/services/llm_service.py:59](../app/services/llm_service.py#L59)). Compose does not set `stop_grace_period` ([docker-compose.yml](../docker-compose.yml)). [docs/operations.md:105](../docs/operations.md#L105) already explains that these are separate windows and operators must configure grace.

**Trigger/impact:** The orchestrator kills the container before its multi-step cleanup sequence finishes. Pending replies, audit writes, or metrics can be lost even though the application attempts a bounded graceful drain. The [Docker Compose service reference](https://docs.docker.com/reference/compose-file/services/#stop_grace_period), checked on 11 October 2026, specifies a default stop grace of 10 seconds when `stop_grace_period` is omitted. Actual Coolify/service termination settings still need deployment verification; the repository does not establish them.

**Implementation:** Define a total configurable shutdown deadline and allocate remaining time across mandatory drains and optional notices/log uploads. Declare a compatible Compose termination grace period and deployment instructions. Prefer preserving essential reply/lifecycle work over a long shutdown announcement; record which work was cancelled for later durable recovery.

**Dependencies:** RUN-02 durable unfinished-work recovery; deployment owner must verify Coolify/service grace settings.

**Acceptance/tests:** Exercise stalled handlers, conversation work, audits, metrics, and logging together under a short fake total deadline. Verify clients close in the existing correct order, all cancellation-resistant tasks are accounted for, and the container-level termination budget exceeds the application budget with margin.

Run a container-level SIGTERM test as well as mocked application cleanup: stall each drain, verify that shutdown respects one overall deadline, and ensure the configured orchestrator grace leaves a measured margin. A guessed longer grace alone does not bound a sequence of independently timed cleanup steps.

<a id="run-10"></a>

### RUN-10 — Make scheduler throughput, freshness, and fairness explicit

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, deliberate low-concurrency tradeoff; P3 distributed extension.

**Evidence:** Consolidation processes candidates serially ([app/services/health.py:240](../app/services/health.py#L240), `242`) and proactive processing also awaits each candidate serially (`:404`, `415`). The loops sleep the interval and then run a scan (`:253`, `444`), so actual cadence is interval plus scan duration. Skipped consolidators caused by a held memory lock are counted as processed because `run_consolidator` returns no outcome ([app/services/user_task_manager.py:630](../app/services/user_task_manager.py#L630); [app/services/health.py:243](../app/services/health.py#L243)).

**Trigger/impact:** A few very slow upstream requests postpone every later candidate and the next scan. Scan limits cap candidates, not time. Repeated locked/failed users can consume slots, while operator summaries cannot separate truly completed, skipped, and failed maintenance. This is safer than unbounded fan-out, but does not give predictable maintenance lag.

**Implementation:** Consume MEM-14's typed per-profile outcomes and deferred-work contract rather than building a second result mechanism. Define a scan time budget, track last-success/due/attempt timestamps, and use a small bounded worker pool under LLM-01's background allocation. Revalidate eligibility at job execution. Provide stable due ordering/backoff so failed/skipped jobs do not monopolize early scan slots. If replicas ever arrive, add atomic job leases; today's process-local memory locks are not cross-process job ownership.

**Dependencies:** MEM-14 per-profile outcome/retry contract; LLM-01 admission and LLM-02 deadline policy; DATA-04 indexed due-work queries. Do not introduce parallel workers before enforcing global/provider limits.

**Acceptance/tests:** One slow candidate does not consume the entire scan budget; worker concurrency stays bounded; busy-user maintenance reports skipped rather than completed; later eligible users make progress; shutdown/reset cancels owned work appropriately; scheduler lag is observable. Lease tests belong to a later replica milestone.

<a id="run-11"></a>

### RUN-11 — Measure user-visible health rather than only cumulative service averages

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing operational measurements.

**Evidence:** `health._summary` shows cumulative LLM operation count and average/max reply latency, queue/throttle drops, and conversation count ([app/services/health.py:50](../app/services/health.py#L50), `57`, `59`). `liveness` always reports process-level success on a successful in-memory read (`:75`); readiness pings Mongo only (`:93`). `MetricsRegistry` stores count/sum/max timers ([app/services/metrics.py:90](../app/services/metrics.py#L90), `130`). Instrumentation currently records LLM latency, not enqueue-to-delivery or queue wait. Proactive generation failure and intentional model silence both return `None` ([app/services/llm_service.py:334](../app/services/llm_service.py#L334), `357`), which the scheduler counts as skipped ([app/services/health.py:320](../app/services/health.py#L320), `425`).

**Trigger/impact:** The process and Mongo are healthy while provider errors, old queued work, stalled scheduler progress, or failed sends make the bot unusable. Long historical averages conceal a recent regression. Provider requests retried within one service operation are counted as one `calls` event, so that field is not total HTTP/billable request volume.

**Implementation:** Add delivery success/failure/partial counters, admission-to-delivery latency and rolling percentiles, queue depth/oldest age, provider in-flight/attempt counts, scheduler heartbeat/lag/outcomes, audit backlog, and event-loop delay. Distinguish check-in decline from generation failure with a typed result. Keep basic liveness cheap and define readiness/degraded dependency states without routinely calling a billable model. Export bounded labels to an external sink when needed; never use arbitrary user/chat IDs as metric labels.

**Dependencies:** RUN-03/06/10 result types; LLM-01/02 instrumentation; optional collector/deployment work.

**Acceptance/tests:** Fault injection changes the correct recent metric and degraded signal while inexpensive liveness remains usable. Declines and provider failures have different counters. A simulated queued reply records queue wait plus generation/delivery time once; label cardinality remains fixed. A saved metrics snapshot is not represented as a per-minute request rate.

## LLM reliability, budgets, and compatibility

<a id="llm-01"></a>

### LLM-01 — Enforce global admission and prioritize interactive work

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P1, missing essential overload control.

**Evidence:** `LLMService` creates one client and an audit-task set, with no request semaphore ([app/services/llm_service.py:50](../app/services/llm_service.py#L50)). Every public operation goes directly to `_with_retries` (`:232`, `302`, `427`, `483`). `MAX_QUEUED_MESSAGES` is per chat ([app/services/user_task_manager.py:299](../app/services/user_task_manager.py#L299)); concurrent chats can each start processing (`:369`) and memory/proactive work uses the same provider.

**Trigger/impact:** Many distinct conversations plus extraction/maintenance compete for the provider and HTTP pool. Per-chat limits do not bound in-flight requests, all-process queued work, active conversation states, or total cost. Retrying overload can amplify contention.

**Implementation:** Add a shared admission controller covering all LLM task types, with configurable in-flight limit, bounded admission queue, queue deadline, and a reserved/background allocation so memory work cannot starve replies. Hold a request slot only for actual HTTP work unless the chosen policy intentionally retains it through backoff; otherwise retry sleepers consume scarce slots. Reject/defer work with an explicit outcome and instrument queue/in-flight gauges. Derive limits from measured endpoint capacity rather than asserting a user-count guarantee.

**Dependencies:** RUN-03 admission outcomes; LLM-02 retry/deadline policy; meaningful load harness.

**Acceptance/tests:** Start hundreds of distinct mocked conversations and maintenance jobs; observed HTTP concurrency never exceeds configuration, queues stay bounded, cancellation releases permits, foreground work makes progress, and no blocked producer creates an unbounded task list. Record degradation and recovery under a fake rate-limited provider.

<a id="llm-02"></a>

### LLM-02 — Share retry and elapsed-time budgets across fallback layers

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P1, deliberate generous-timeout policy with missing aggregate bounds.

**Evidence:** `_with_retries` applies deterministic exponential sleep without jitter or server retry hints ([app/services/llm_service.py:126](../app/services/llm_service.py#L126), `136`). Defaults are two retries and 610 seconds per request ([app/config.py:162](../app/config.py#L162), `174`). Native parsing catches every ordinary failure and starts a new JSON request policy ([app/services/llm_service.py:425](../app/services/llm_service.py#L425), `460`, `483`). Extraction has a further three-attempt pipeline loop ([app/services/memory_extractor.py:422](../app/services/memory_extractor.py#L422), `538`).

**Trigger/impact:** A direct reply can consume three long attempts before its chat lock is released. Native-mode memory operations can attempt both formats on an outage, and extraction can repeat the whole operation. With the default `LLM_MAX_RETRIES=2`, repeated failures can produce as many as 3 direct / 6 native+fallback per structured operation / 18 requests across a three-attempt extraction pipeline. Larger configured retry counts increase these totals; they are not universal hard maxima. These are possible request counts under repeated failures, not measured durations or proof that every upstream call lasts 610 seconds. Deterministic backoff aligns concurrent retries.

**Implementation:** Pass a single operation budget containing remaining attempts and deadline through request retries, structured-format fallback, and extraction retries. Use jitter and bounded server retry delays. Fallback only for format/capability/parsing problems rather than spending another full policy on exhausted transport/rate-limit failures. Configure per-task/model latency profiles and surface a conversational pending/failed outcome when the user-visible deadline is exceeded. Preserve the reason for the existing 610-second setting: blindly lowering client timeout below the proxy's limit can abandon still-running upstream work and cause duplicate computation. Coordinate cancellation/deadlines with the provider contract.

**Dependencies:** LLM-01 shared controller; OPS-02/TEST-03 provider-capability validation; RUN-10 background job retries.

**Acceptance/tests:** Native-mode transport exhaustion does not trigger a fresh full retry policy; format incompatibility can fallback within the same budget; HTTP request counts and elapsed fake-clock time are bounded at every layer; jitter is deterministic under an injected RNG for tests; cancellation propagates and frees capacity. Existing SDK `max_retries=0` remains enforced.

<a id="llm-03"></a>

### LLM-03 — Budget the complete prompt, not only individual messages or memory

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing request-size guard.

**Evidence:** The incoming character guard is per Telegram message ([app/handlers/messages.py:315](../app/handlers/messages.py#L315)), while consecutive messages are joined into a batch ([app/services/user_task_manager.py:431](../app/services/user_task_manager.py#L431)). `handle_message` sends every stored buffer message in active history ([app/services/chat_manager.py:239](../app/services/chat_manager.py#L239), `251`) and then combines persona, memory, time context, group/sender memory, and a format clause (`:311`; [app/services/llm_service.py:218](../app/services/llm_service.py#L218)). Exceeding extraction thresholds schedules background work rather than synchronously reducing the current request ([app/services/chat_manager.py:274](../app/services/chat_manager.py#L274), `281`). A count-based buffer cap is not a token limit.

**Trigger/impact:** Many allowed messages, a large persona/profile, group attribution JSON, or multilingual text can exceed a model's context window or create unnecessarily expensive requests. Background extraction may be busy/skipped, so it is not a current-request context guarantee. `CHARS_PER_TOKEN` is only a rough configurable estimate.

**Implementation:** Introduce model/provider context profiles and a budget for the assembled request, reserving room for output and reasoning when required. Preserve the latest user turn and needed speaker attribution while selecting older history/memory deterministically by relevance and recency. Define a long-batch/input rejection or summarization policy that does not drop accepted text silently. Use a tokenizer where available, conservative estimation otherwise, and avoid assuming one tokenizer fits every endpoint.

**Dependencies:** LLM-04 output reservation; memory relevance work can improve selection later but deterministic context limits should come first.

**Acceptance/tests:** Construct maximum-size same-sender batches, a near-cap buffer, oversized persona/memory, group metadata, emoji, and multiple scripts. Verify the complete request stays within configured bounds and always contains the latest accepted turn. A provider context error produces a clear bounded recovery path and metrics rather than repeating the identical oversized request.

<a id="llm-04"></a>

### LLM-04 — Track actual usage and add provider-aware output budgets

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing cost controls; preserve provider compatibility.

**Evidence:** Replies and check-ins explicitly omit a generation-token cap ([app/services/llm_service.py:222](../app/services/llm_service.py#L222), `297`). Structured calls also omit one (`:484`). Response character caps are applied after inference ([app/services/chat_manager.py:384](../app/services/chat_manager.py#L384); [app/services/health.py:333](../app/services/health.py#L333)). Audit records contain length metadata and status, but not provider usage/model/attempt breakdown ([app/services/llm_service.py:89](../app/services/llm_service.py#L89), `102`); response usage is never read.

**Trigger/impact:** A verbose model consumes output tokens that are later discarded, while reasoning tokens/provider charges may not correlate with visible characters. Operators cannot compare total foreground/background token consumption, estimate cost, or verify whether a change actually saves inference.

**Implementation:** Capture available input/output/reasoning/cached-token usage, model, finish reason, per-attempt status, and latency in bounded metadata; represent unavailable usage as unknown. Support provider/model-specific output limits rather than hard-coding one OpenAI argument across proxies. Choose adequate JSON/output headroom and handle truncation explicitly. Add optional rolling per-user/global budgets only after usage and pricing configuration are trustworthy; service-operation call counts alone are not cost.

**Dependencies:** LLM-02 attempts; LLM-03 prompt reservation; LLM-05 truncated-envelope handling; monitoring sink from RUN-11.

**Acceptance/tests:** SDK MockTransport fixtures include usage, missing usage, reasoning/cached usage variants, and length finish reasons. Verify metadata does not contain full user text, budget enforcement is deterministic, valid JSON is not silently truncated, and supported provider request parameters are validated. Cost estimates identify their configured prices/date and unknown usage rather than implying billing precision.

<a id="llm-05"></a>

### LLM-05 — Separate plain-text recovery from malformed structured output

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, deliberate compatibility fallback with a confirmed user-facing failure case.

**Evidence:** `_parse_reply_bundle` returns a cleaned raw string whenever JSON decoding fails or a valid value is not an object ([app/services/llm_service.py:368](../app/services/llm_service.py#L368), `395`, `399`). `generate_reply_bundle` reads content but not finish reason/refusal (`:241`). Missing/empty usable `reply` in a dictionary is already handled safely (`:392`). [tests/test_llm_sdk_compatibility.py](../tests/test_llm_sdk_compatibility.py) deliberately preserves ordinary non-JSON plaintext, so removing all fallback would break intended compatibility.

**Trigger/impact:** A truncated `{"reply":"hello` envelope, a JSON array, or other malformed structured response can be sent verbatim and stored as a normal assistant reply, exposing internal formatting such as `reaction`/`affinity_delta` instead of useful text. The caller and metrics label this successful generation despite a format failure.

**Implementation:** Return a typed parse outcome: structured-valid, plaintext-compatible, malformed-envelope, declined/refused, truncated, or empty. Preserve ordinary plaintext fallback, but do not publish recognizable broken JSON envelopes as conversational text. Validate a local reply schema, inspect finish/refusal fields, and use a bounded repair/fallback policy under LLM-02's shared budget. Record format degradation separately from provider success and delivery success.

**Dependencies:** LLM-02 budget; LLM-04 finish/usage metadata; RUN-01 history policy.

**Acceptance/tests:** Existing plaintext fallback remains. Add truncated JSON, array/scalar JSON, invalid reaction/delta, fenced malformed envelope, refusal, and length finish reason. Users receive valid text or the intended direct/ambient fallback, never raw broken metadata. Huge/nonfinite affinity values retain current robust clamping behavior.

<a id="llm-06"></a>

### LLM-06 — Bound audit-write admission and summarize before scheduling

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority/type:** P2, missing background memory/backpressure bound.

**Evidence:** `_fire_log` creates one asynchronous task per operation and holds it in `_bg_tasks` with no capacity limit ([app/services/llm_service.py:112](../app/services/llm_service.py#L112), `120`). Full `inputs`/`outputs` are passed into that task; lengths are computed only after the task begins (`:74`, `89`). Database server selection has a timeout, and audit tasks are correctly drained/cancelled at close (`:59`), but no queue/task bound exists during runtime.

**Trigger/impact:** When Mongo is slow/unreachable and completions continue, audit tasks and their referenced prompt/output objects accumulate. Audit logging is off the hot path but can become memory/DB pressure, and repeated failures also generate error logs. Shutdown draining does not limit the steady-state backlog.

**Implementation:** Compute the small metadata summary synchronously before scheduling, then submit it to a bounded audit queue serviced by a fixed worker count. Define overflow behavior such as sampling/dropping with counters, or durable metadata spooling if audits must be recoverable. Keep audit failure unable to break replies and avoid retaining full prompt objects merely to measure their lengths.

**Dependencies:** RUN-11 backlog/drop metrics; LLM-04 usage metadata. Coordinate ownership with existing `LLMService.close`.

**Acceptance/tests:** Hold Mongo inserts pending while completing many calls; queued records/tasks and retained metadata remain bounded. Overflow increments one appropriate counter without recursively flooding logs. Close drains available records, cancels stalled writes within budget, and always closes the HTTP client.

## Group routing and participation

<a id="group-01"></a>

### GROUP-01 — Make the group off switch hold during failures and in-flight work

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Confirmed control gap · Source inspection.**

- **Evidence:** [`_handle_group_message`](../app/handlers/messages.py#L396) and the media handler treat a failed enabled-state lookup as enabled. [`cmd_groupbot`](../app/handlers/commands.py#L871) updates settings without cancelling queued/in-flight replies or memory tasks. [`_process_batch`](../app/services/user_task_manager.py#L396) does not recheck group enablement before generation/delivery.
- **Scenario and impact:** A transient settings read failure can bypass a previously disabled group. A reply/extraction accepted before `/groupbot off` can continue after the bot says it will not reply or remember anything there.
- **Implementation:** Distinguish unknown/default state from a known disabled state. Use a safe last-known policy or fail closed when privacy settings cannot be read. Define immediate versus future-only disable semantics; for an immediate off switch, invalidate the group lifecycle, stop its jobs, and recheck the current policy before writes and sends.
- **Done when:** Queue a delayed reply/extraction, disable the group, then release the delay: no new public reply or memory write occurs. A simulated settings-read failure does not reactivate a known disabled group. Re-enabling works deliberately.

<a id="group-02"></a>

### GROUP-02 — Preserve original order across passive and queued group messages

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Confirmed ordering gap · Source inspection.**

- **Evidence:** Ambient drops write immediately in [`_maybe_ambient_chime`](../app/handlers/messages.py#L219); addressed/passing messages are written later by [`handle_message`](../app/services/chat_manager.py), after batching. The batch manager now correctly preserves consecutive A → B → A sender ordering; that fix does not order passive writes against delayed queued writes.
- **Scenario and impact:** A sends an addressed question, B sends a passive correction, then the batching timer processes A. The buffer can contain B before A. History, event dating, and extraction can misinterpret which statement corrected which.
- **Implementation:** Persist admitted raw messages once with original message/update ID, original timestamp, and conversation ordering metadata before routing. Have reply jobs refer to those records rather than appending a second combined user record. Coalesced prompt text can remain a derived view.
- **Dependencies:** Durable message identity/admission and MEM-06 source-time handling.
- **Done when:** Mixed passive/addressed/ambient traffic preserves source order without duplicate writes. A → B → A stays distinct, arrivals during extraction survive trimming, and retries do not create duplicate turns.

**Original timestamp detail:** Preserve Telegram `message.date` (and update/message identifiers) separately from processing time and the coalesced prompt view. Relative-date extraction around midnight and delayed mood observations need the original source time. A combined batch must not pretend all messages were authored at its flush timestamp. See MEM-06 for observation identity and stale-state handling.

<a id="group-03"></a>

### GROUP-03 — Decide whether spam should ever qualify for ambient generation

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Deliberate policy tradeoff · Source inspection.**

- **Evidence:** [`_maybe_ambient_chime`](../app/handlers/messages.py#L190) disables the cheap trigger for spam, but [`AmbientGate.decide`](../app/services/group_gate.py#L559) also passes candidates on periodic scan ticks. Direct replies to the bot deliberately survive spam classification.
- **Scenario and impact:** A mass-tag/repetition message classified as spam can still consume an LLM call and get an ambient reply on a scan tick. The current implementation is a participation heuristic, not a moderation system.
- **Implementation:** Write an explicit policy matrix for legitimate direct replies, spam mentions, ambient scan ticks, and retained context. If spam suppression should be strict, veto ambient admission before scan/probability and keep only a bounded sanitized context record. Do not indiscriminately block real support questions because they repeat text.
- **Done when:** A flagged spam message on the exact scan boundary cannot trigger ambient generation under the strict policy. Direct-reply exceptions and repeated legitimate messages have explicit regression tests.

<a id="group-04"></a>

### GROUP-04 — Improve multilingual routing and back-off interpretation

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Existing heuristic quality gap · Source inspection.**

- **Evidence:** [`_TRIGGER_PATTERNS` and `_NEGATIVE_PATTERNS`](../app/services/group_gate.py#L61) are largely English. [`handle_message`](../app/services/chat_manager.py) applies the negative affinity signal only after the message was selected for a generated reply. Recency-based implicit addressing is group-wide rather than semantic/speaker-aware.
- **Scenario and impact:** Hindi/Hinglish or other-language greetings/back-off requests behave differently from English. “Stop by tomorrow” can lower affinity; an unselected “please stop replying” may not. Untagged chatter near any bot reply can be treated as a follow-up.
- **Implementation:** Add a tested multilingual lexicon and conservative intent patterns. Honor explicit back-off/settings immediately, independent of ambient probability. Keep cheap gates, but improve follow-up attribution with prior addressed speaker/reply metadata and measured false-positive rates before adding another LLM classifier.
- **Done when:** Representative language/script tests cover invites, negation, quoted stop words, benign phrases, and real requests to stop. A group's unrelated conversation is not automatically attributed to the bot's latest interlocutor.

<a id="group-05"></a>

### GROUP-05 — Define forum-topic and group-migration support

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Conditional capability gap · Source inspection.**

- **Evidence:** Queues, buffers, locks, and gate state are keyed by chat ID in [`UserTaskManager`](../app/services/user_task_manager.py) and [`models`](../app/database/models.py); no `message_thread_id` or chat-migration handler is present in the application inventory. Telegram reply helpers do not create separate topic memory scopes.
- **Scenario and impact:** If the bot is used in forum groups, unrelated topics share context/cooldowns and serialize on one conversation lock. Moving a normal group to a supergroup can leave historical settings/memory under its previous ID. This is conditional on those Telegram features being used, not a demonstrated ordinary-DM failure.
- **Implementation:** Either document those modes as unsupported, or introduce a conversation key containing chat/topic and handle migration events with a deliberate settings/history mapping. Keep group-wide admin controls and participant-global consent distinct from topic-local context.
- **Dependencies:** Scoped storage migration and routing/message identity.
- **Done when:** Two topics cannot consume each other's private/local context or implicit-follow-up window. Migration preserves the chosen settings without duplicate group profiles. Existing non-topic groups remain compatible.

## Telegram interaction and reply experience

<a id="ux-01"></a>

### UX-01 — Make errors, overload, and unsupported requests actionable

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Existing experience improvement · Source inspection.**

- **Evidence:** [`handle_user_message`](../app/handlers/messages.py) silently ignores unknown slash commands; media gets a generic text-only response. Queue overflow is handled in [`enqueue_message`](../app/services/user_task_manager.py). The membership introduction says the bot will “always answer,” although throttle, queue, permission, and provider failure paths exist.
- **Implementation:** Provide concise DM help for unknown commands, state what a dropped request needs from the user, and distinguish temporary provider failure from unsupported input. Keep public-group feedback rate-limited. Align onboarding claims with actual text-only, best-effort capabilities. Explain capture timing without promising every message becomes memory.
- **Done when:** Disabled/renamed command behavior is intentional, guidance points to enabled controls, and failure messages do not claim delivery, saved memory, browsing, or scheduling that did not happen. Group flood tests do not turn feedback into spam.

<a id="ux-02"></a>

### UX-02 — Make group menus and callbacks appropriate to the invoking person

- [ ] Implemented and verified, or resolved with a recorded decision.

**P3 · Existing experience improvement · Source inspection.**

- **Evidence:** [`on_guide_nav`](../app/handlers/commands.py#L505) edits a shared Telegram guide message according to whichever user presses its buttons, and an onboarding callback can update that user's profile. Membership guidance points to `/start` even if that command is disabled. Navigation and command enablement are separate surfaces.
- **Implementation:** Keep shared group help generic, move personalized controls into a DM, and explicitly bind private state-changing callbacks to their owner/session. Build welcome/introduction/help affordances from enabled features and current capability settings.
- **Done when:** Another participant cannot replace a shared guide with misleading personalized state; stale buttons produce useful feedback; disabled controls are not advertised; authorization remains enforced in handlers, not merely hidden buttons.

<a id="ux-03"></a>

### UX-03 — Preserve and expose useful reply formatting

- [ ] Implemented and verified, or resolved with a recorded decision.

**P3 · Existing experience improvement · Source inspection.**

- **Evidence:** [`telegram_text`](../app/services/telegram_text.py) already safely chunks long plain text and strips oversized HTML command output. The response character cap in [`apply_response_cap`](../app/services/telegram_text.py#L22) can cut generated explanations/code, and ordinary LLM replies are plain text.
- **Implementation:** Add a deliberate “show more” or downloadable text path for requested depth, while retaining default conversation limits. If rich formatting is added, use a validated entity/escaping renderer and a chunk model that preserves code blocks and link boundaries. Do not send arbitrary model HTML/Markdown directly.
- **Done when:** Long requested answers remain useful, code indentation survives, formatting cannot cause rejected sends, and chunk-level failures integrate with delivery tracking. This is optional polish after reliable delivery.

## Configuration, deployment, and operations

<a id="ops-01"></a>

### OPS-01 — Reject malformed operator allowlists instead of opening reports

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P1; confirmed access-control configuration defect under malformed input.

**Evidence:** [app/config.py:_env_int_set](../app/config.py), lines 51–61, silently discards comma-separated values that cannot be parsed as integers and returns an empty set if none survive. `Config.ADMIN_USER_IDS`, lines 315–317, uses this helper. [app/handlers/commands.py:_is_admin](../app/handlers/commands.py), lines 92–103, permits any private-chat sender when the set is empty. [docs/setup_guide.md:76](../docs/setup_guide.md#L76) documents that invalid entries are ignored.

**Scenario and impact:** An operator enters `ADMIN_USER_IDS=alice,bob`, or mistypes every numeric ID. Startup succeeds and all private users can access `/health` and `/metrics`, despite the operator intending an allowlist. This exposes operational reports; it does not grant arbitrary shell or database administration. An explicitly blank allowlist is a separately documented product choice.

**Implementation:** Parse every nonblank entry strictly, validate the supported user-ID range, and raise a field-specific startup error for any malformed nonempty allowlist. Consider replacing implicit empty-set public access with an explicit `OPS_REPORT_ACCESS=admins|private` setting. Keep credentials and entered values out of error logs. Update setup/default behavior documentation.

**Dependencies:** None for strict parsing; an explicit access mode needs a compatibility decision for existing users.

**Acceptance:** Table-driven tests cover missing/blank, one valid ID, valid multiple IDs, mixed valid/invalid, all-invalid, whitespace, and unsupported IDs. Malformed settings stop startup; a non-operator cannot access either command after a valid allowlist. If an explicit public mode remains, test it independently from malformed input.

<a id="ops-02"></a>

### OPS-02 — Complete configuration validation and provide a local preflight

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; confirmed silent coercion plus missing operator validation.

**Evidence:** [app/config.py:_env_bool:47–48](../app/config.py#L47) treats every string outside four truthy spellings as false, including typos. `_env_int_or_none:33–40` treats a malformed logs-channel value as absent. `Config.LLM_STRUCTURED_MODE:159–161` is an unconstrained string; `LLMService._structured_call` at [app/services/llm_service.py:425](../app/services/llm_service.py#L425) selects native parsing only for an exact match and sends all other modes through JSON. `Config.validate_runtime_relationships:396–411` validates several important numeric relationships but none of these string/boolean cases. [main.py:63–68](../main.py#L63) pings/initializes MongoDB before the Telegram token is validated by `Bot`.

**Scenario and impact:** `TELEGRAM_PUBLISH_COMMANDS=tru` silently disables publication; a logs-channel typo silently disables forwarding; `LLM_STRUCTURED_MODE=native-parse` silently chooses a different request format. Wrong core connection/model/token settings can be discovered after unrelated service work.

**Implementation:** Use explicit accepted true/false values and reject nonblank invalid booleans/IDs. Model structured mode as a literal/enum. Validate nonblank model IDs and URL structure, while preserving the legitimate local-provider placeholder-key case. Add a local `check-config` command that reports selected feature states and redacted endpoints without creating network clients. Keep a network capability check explicit, bounded, and documented as potentially billable.

**Dependencies:** TEST-01 test/bootstrap isolation. Local validation/preflight can ship independently; coordinate any optional network capability check with TEST-03's provider matrix.

**Acceptance:** Invalid spelling produces a useful field-specific error; every documented disable value remains accepted. Preflight succeeds without MongoDB/Telegram/LLM access, redacts tokens and URI credentials, and makes no network request. Provider checks cannot run accidentally as part of config parsing or ordinary unit tests.

<a id="ops-04"></a>

### OPS-04 — Add health signals that a deployment platform can actually observe

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; missing essential operating capability for unattended deployments.

**Evidence:** [docker-compose.yml:9–34](../docker-compose.yml#L9) defines restart behavior but no healthcheck. [docs/operations.md:25](../docs/operations.md#L25) states that a stopped process is restarted but a responsive process behind a slow LLM is not detected. [app/services/health.py:liveness:75–90](../app/services/health.py#L75) reports the process that calls it; `readiness:93–112` only pings MongoDB. [docs/development/observability.md:19–26](../docs/development/observability.md#L19) documents that these do not establish polling, delivery, or provider availability.

**Scenario and impact:** Polling or the event loop can stop progressing while the container remains running. The operator discovers the problem only by manually invoking the Telegram report or hearing from users. Telegram-based reports also depend on the same channel that may be impaired.

**Implementation:** Emit a process heartbeat and last successful polling/update progress from the running worker; expose a minimal local CLI/file/socket probe or a deliberately secured HTTP probe. Define separate liveness, dependency readiness, and degraded-provider signals. A new Python process calling `liveness()` is not a valid healthcheck of the bot. Define the platform alert/watchdog policy separately: marking a Compose container unhealthy alone is not a complete recovery policy. Avoid restart loops on temporary provider failures because startup intentionally drops pending updates.

**Dependencies:** RUN-09; RUN-11 end-to-end measurements and LLM-01 overload controls.

**Acceptance:** Healthy idle polling remains healthy; deliberately freeze the main loop/polling and observe a failure; database outages and LLM slowdowns produce their intended distinct statuses. The probe requires no LLM generation or Telegram user message and exposes no sensitive prompts/settings.

<a id="ops-05"></a>

### OPS-05 — Turn backup advice into a tested disaster-recovery procedure

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P1 for a shared bot retaining personal data; missing essential operator procedure, not a request to make the bot implement its own MongoDB backup engine.

**Evidence:** [docker-compose.yml:23–25](../docker-compose.yml#L23) persists file logs only. [docs/operations.md:71–77](../docs/operations.md#L71) states that MongoDB backups are external and that the Telegram reset snapshot has no built-in restore command. [docs/architecture.md:75–82](../docs/architecture.md#L75) lists six stored record categories. No repository-owned restore drill, consistency checklist, RPO/RTO target, or versioned recovery fixture is present.

**Scenario and impact:** After database loss, a reset mistake, or a bad upgrade, a recent snapshot may be unavailable or cannot be restored confidently. Restoring only a personal profile leaves buffers, lifecycle tokens, member settings, and engagement state inconsistent. Backups/log copies can also restore information a user had asked to delete.

**Implementation:** Choose deployment-specific backups (Atlas or the operator's MongoDB tooling), encryption/access policy, retention, recovery-point and recovery-time targets. Write a step-by-step drill using a separate database and bot token; list every collection and index, lifecycle/consent implications, secrets exclusion, and post-restore checks. Treat deletion policy and backup retention together. Add a safe admin-only offline reset-export restoration utility only if supporting those exports is an explicit product promise.

**Dependencies:** PRIV-04/05 privacy/deletion decisions; DATA-05 index validation and DEV-01 release compatibility.

**Acceptance:** Restore a seeded disposable deployment to a new database; verify profiles, group/member records, buffers, audit TTL/indexes, metrics, and opt-out settings. Record achieved RPO/RTO and prove the test bot cannot send to production recipients. Repeat after meaningful storage changes.

<a id="ops-06"></a>

### OPS-06 — Migrate the MongoDB driver deliberately

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P1 due to the upstream lifecycle; dependency maintenance requirement, not evidence that current reads/writes fail.

**Evidence:** [pyproject.toml:10](../pyproject.toml#L10) depends on Motor. [app/database/connection.py:10–38](../app/database/connection.py#L10) constructs and types the Motor singleton; [app/database/models.py:18](../app/database/models.py#L18) and handlers/memory-loader/chat-manager import Motor types. [tests/conftest.py:28–111](../tests/conftest.py#L28) adapts synchronous mongomock through custom async wrappers rather than exercising the actual async driver.

**Scenario and impact:** Remaining on a driver beyond its upstream support horizon increases the maintenance burden. A blind rename to PyMongo Async could alter client lifecycle, close behavior, cursor/session usage, cancellation, codec options, and error propagation without current mocked tests catching it.

**Implementation:** Inventory actual Motor API use and the official supported migration path. Establish real-database contract tests first, then migrate behind a narrow database connection boundary; update types, async-close handling, lockfile/export, and docs together. Test deployment startup, timezone-aware codecs, atomic updates, iteration, cancellation, reconnect, and shutdown. Keep a rollout/rollback plan consistent with storage compatibility. Motor was deprecated on 14 May 2025; ordinary bug-fix support ended on 14 May 2026, with critical fixes scheduled through 14 May 2027. These dates were checked on 11 October 2026 against the [official Motor repository](https://github.com/mongodb/motor). Follow the [official PyMongo Async migration guide](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/reference/migration/) and validate the deployed versions. Package availability is not a support guarantee.

**Dependencies:** TEST-02, OPS-05, DEV-01.

**Acceptance:** Existing regression/property suite and real MongoDB integration tests pass with the new driver; no Motor runtime/type import remains; actual Docker startup and graceful close pass. A staging restore/upgrade drill demonstrates supported rollback behavior.

## Testing and measurable validation

<a id="test-01"></a>

### TEST-01 — Isolate automated tests from local environment and import side effects

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; confirmed reproducibility defect under a valid nondefault developer environment.

**Evidence:** [app/config.py:17–22](../app/config.py#L17) loads repository `.env` during import. [tests/test_group_config_smoke.py:29–39](../tests/test_group_config_smoke.py#L29) explicitly assumes unset group settings and asserts live defaults; at lines 63–67 it deletes a variable and reloads config, allowing dotenv to put it back. [docs/development/testing_guide.md:58–62](../docs/development/testing_guide.md#L58) already warns about this. [app/__init__.py:6–23](../app/__init__.py#L6) creates `logs/bot.log` and rewires global logging on import. Fixtures in [tests/conftest.py:8–25](../tests/conftest.py#L8) block TCP only after fixtures begin; test-module imports precede them.

**Scenario and impact:** A contributor follows the setup guide, edits supported settings in `.env`, and receives default-value test failures even though their changes are unrelated. Module imports create runtime artifacts and config-dependent clients before test isolation applies. The passing CI baseline uses a clean environment; it does not settle this scenario.

**Implementation:** Introduce an explicit configuration-loading boundary or test bootstrap that disables dotenv and seeds deterministic synthetic settings before collection/import. Default-value tests should construct settings from an isolated source, while separate tests exercise dotenv precedence using temporary files. Configure logging at runtime entry points, or direct test logs to a temporary location without replacing unrelated process handlers.

**Dependencies:** None; supports OPS-01/02 and TEST-02/03.

**Acceptance:** The same focused config suite passes with unset, deliberately nondefault, and invalid unrelated caller environment settings, without reading a real `.env` or writing repository logs. Temporary dotenv tests prove intended production precedence. An accidental network attempt during import/collection is rejected as well as one during a test body.

<a id="test-02"></a>

### TEST-02 — Add actual MongoDB contracts beside fast mocked tests

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P1 before relying on concurrency/deletion guarantees or changing drivers; validation gap.

**Evidence:** [tests/conftest.py:43–111](../tests/conftest.py#L43) forwards operations to mongomock and replaces `get_db`/`get_db_client` globally. [tests/test_database.py:18–25](../tests/test_database.py#L18) checks initialization against that mock. [docs/development/testing_guide.md:47–50](../docs/development/testing_guide.md#L47) correctly lists unverified real TTL, transaction, index, pooling, timeout, and concurrency behavior. The critical storage paths include `models.update_memory_if_unchanged:78–95`, `add_message_to_buffer:309–353`, generation helpers around `:200–277`, and snapshot trim around `:402–437`.

**Scenario and impact:** A passing mock test cannot establish that the real driver/server execute nested snapshot predicates, atomic `$push/$slice`, concurrent `$pull` trimming, timezone codec conversion, index changes, or reset-generation fences as expected under independent clients.

**Implementation:** Put integration tests in a separate marked suite with a disposable MongoDB service and explicitly scoped test database. Provide fixtures that opt out of the autouse DB mock and TCP block only for the authorized local test service. Test contested profile updates from independent clients, buffer append/trim races, reset/recreation generations, startup/reconnect/shutdown, and indexes. Run a supported server baseline in PR CI, with a scheduled broader server/driver matrix if necessary.

**Dependencies:** TEST-01 and a minimal disposable-MongoDB CI/container scaffold, which can be built with this item. Full TEST-04 artifact smoke checks are later validation; they do not block the first driver contracts. Do this before OPS-06.

**Acceptance:** Unit tests remain network-free; integration tests demonstrably use the real driver/server, cannot select a production database, and remove their fixture data. Assertions cover final persisted invariants rather than just successful calls. Run two independent clients to avoid inadvertently testing only one process-local lock.

<a id="test-03"></a>

### TEST-03 — Publish a bounded provider contract matrix and fix the live harness lifecycle

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; compatibility validation gap plus confirmed incomplete manual-script cleanup.

**Evidence:** [tests/test_llm_sdk_compatibility.py](../tests/test_llm_sdk_compatibility.py) exercises the real SDK with mocked HTTP (e.g. lines 117–134 for native parse), a useful existing check. [tests/run_llm_live.py:15–44](../tests/run_llm_live.py#L15) exercises only reply and personal extraction, under fixed synthetic ID 999, using import-time configured services; lines 46–50 finish without `llm_service.close()` or MongoDB close. `LLMService._fire_log` at [app/services/llm_service.py:112–123](../app/services/llm_service.py#L112) schedules audit tasks and `close:59–69` supplies the missing drain. [docs/development/llm_integration.md:3,35,58–66](../docs/development/llm_integration.md#L3) correctly warns about endpoint/model-format differences and native fallback.

**Scenario and impact:** A bot replies successfully but group extraction, compression, consolidation, or check-ins use an unsupported provider feature. The manual script can write synthetic audit rows to whatever database was configured and can exit before audit drain/client closure. Its successful output proves only two shallow operations.

**Implementation:** Make an explicit opt-in test CLI accept a named isolated endpoint/config and test database, print a redacted run identifier, and use `try/finally` to close services. Check all six operation families, both documented structured modes where supported, fallback, refusal/blank responses, rate-limit/timeout handling, and required schema shape. Record verified provider/model/endpoint-version/date combinations and mark unsupported/unverified ones. Keep paid live probes outside default pytest/PR CI; use mocked transport contract tests for every request branch.

**Dependencies:** TEST-01, OPS-02; semantic memory evaluation remains separate from API compatibility.

**Acceptance:** A provider row contains reproducible synthetic inputs and an actual run result for every claimed operation; one successful model is not generalized to others. The harness refuses production-looking destinations by default, drains/closes after success and assertion failure, and leaves no pending-task/resource warning. Document cost limits and the exact scope of each check.

<a id="test-04"></a>

### TEST-04 — Test the artifacts that operators install and deploy

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; build/distribution validation gap.

**Evidence:** [.github/workflows/python-checks.yml:24–37](../.github/workflows/python-checks.yml#L24) installs the pinned Python and locked dev dependencies, then checks Black, Ruff, and full pytest on Ubuntu. It does not build the image, inspect Compose, verify the generated export, or run an installed-artifact smoke check. [CONTRIBUTING.md:23–31](../CONTRIBUTING.md#L23) asks contributors to regenerate [requirements.txt](../requirements.txt) and build Docker manually. [Dockerfile:23–29](../Dockerfile#L23) uses a different runtime installation path (`--no-dev --no-install-project`). README/setup docs also offer PowerShell commands while CI covers one OS (`workflow:12`).

**Scenario and impact:** Application tests pass but an omitted runtime file, an unwritable mounted log directory, missing runtime package, broken non-root startup, export drift, or platform-specific setup path breaks the actual operator workflow. Ubuntu-only success does not establish Windows/macOS support.

**Implementation:** Add CI jobs for Docker build and a fake-config non-network import/package check as the runtime UID, Compose validation, and regenerated-export diff. Add a disposable-service startup/stop smoke test that reuses TEST-02 infrastructure. Verify no test-only packages or secrets land in the runtime image. Define explicitly which Python/OS/architecture combinations are supported; add the smallest matrix needed to match advertised support, rather than changing Python constraints without evidence. Store build revision and useful OCI metadata; release images should be referenced by immutable revision/digest.

**Dependencies:** TEST-01 for isolated startup configuration. Image build/export checks can ship first; full database smoke checks then use TEST-02, shutdown checks use RUN-09, and license checks use DEV-02.

**Acceptance:** CI fails when a required persona/runtime file is omitted, export differs, the runtime UID cannot write the intended volume, or Compose config is invalid. The supported platform smoke jobs install from the lockfile and import/run the documented entry point without contacting production services. A clean build produces a traceable image; rebuild/rollback instructions identify the exact artifact.

<a id="test-05"></a>

### TEST-05 — Make capacity and failure recovery measurable

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; validation gap, not proof of a current performance incident.

**Evidence:** [docs/development/performance_and_scaling.md:3–8](../docs/development/performance_and_scaling.md#L3) expressly says there is no sustained-load benchmark. Lines 127–141 list unmeasured end-to-end latency, queue waits, memory, event-loop delay, and recent percentiles. [docs/development/observability.md:79–81](../docs/development/observability.md#L79) documents missing queue/delivery timing, percentile histograms, and concurrency gauges. The current regression/property suite validates many timing/race branches using synthetic mocks but supplies no release capacity target.

**Scenario and impact:** Queue caps or extraction/model settings are changed with no evidence of their effect on legitimate message drops, user-visible latency, resident state, background backlog, or restart loss. A service-operation average masks slow users and queue delays.

**Implementation:** Define a realistic single-process workload and service objectives before promising a user capacity: private/group mix, sender bursts, profile sizes, synthetic model latency, rate limits, timeouts, and database latency. Build a reproducible harness with deterministic seeded data and an in-process/provider stub so routine runs cost no inference credits. Add fault scenarios for DB outages, Telegram send failures, provider 429/timeouts, forced termination, and scheduler overlap. Save machine/config/revision/results, and use relative regression budgets with justified absolute targets.

**Dependencies:** RUN-01/02/03/06/11 and LLM-01 admission/delivery/observability; TEST-02/04 for realistic IO and stop behavior.

**Acceptance:** Results include p50/p95/p99 update-to-delivery time, queue/drop rates, peak RSS/state/task counts, background completion lag, query costs, and recovery outcome. An overload case demonstrates bounded resource behavior; failure injection verifies documented loss/replay behavior. Run a short smoke in CI and a bounded longer benchmark on release candidates or scheduled runs.

<a id="test-06"></a>

### TEST-06 — Evaluate semantic memory and conversation behavior with a versioned corpus

- [ ] Implemented and verified, or resolved with a recorded decision.

**P1 · Missing essential quality evidence · Source inspection.**

- **Evidence:** [Automated tests](development/testing_guide.md) already cover prompt contracts, real SDK parsing through mocked HTTP, concurrency, and persistence behavior. The guide explicitly says they do not establish factual memory quality; the manual live script checks usable shapes, not accuracy. [Memory compilation](../app/services/memory_loader.py) includes stored facts, beliefs, events, and insights without a measured semantic-selection baseline.
- **Implementation:** Create synthetic, consent-safe conversations and expected facts/corrections/attribution/date ranges. Cover multilingual and mixed-script turns, repeated names, role-like text, quoted/third-party statements, uncertain plans versus completed events, explicit forgetting, mood observation timing, contradictory updates, compression, and long-run consolidation. Measure factual precision/recall, unsupported inference, correction/removal success, cross-person attribution, temporal accuracy, and retained key details. Add reply evaluations for relevant recall, disclosure, language, grounded check-ins, and capability honesty.
- **Dependencies:** Deterministic test settings, provider contract fixtures, provenance/scopes, and traceable prompt/model versions. Build the corpus before changing retrieval architecture.
- **Done when:** A baseline can be reproduced against a named model/prompt revision with recorded variance. Essential cases use deterministic validators or human-reviewed reference answers; automated LLM judges are calibrated against human examples. Runtime invariants remain ordinary offline regression tests; optional paid evaluations are explicitly selected and budgeted. A prompt/model upgrade compares against the baseline before release.

## Maintainability, documentation, and GitHub repository

<a id="dev-01"></a>

### DEV-01 — Establish versioned releases and storage/config upgrade contracts

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; missing release/upgrade system.

**Evidence:** [pyproject.toml:3](../pyproject.toml#L3) reports version 0.1.0. [docs/operations.md:79–98](../docs/operations.md#L79) provides a sensible source update checklist but says the roadmap is not a release log and delegates schema/config review to operators. No changelog, schema-version migration inventory, release workflow, or repository-owned rollback drill is present locally. Live GitHub inspection found no published releases as of 2026-10-11.

**Scenario and impact:** Operators cannot readily tell which behavior/default/storage changes shipped together, whether an older binary can read an upgraded document, or which revision can safely restore their deployment. Prompt/persona changes also alter behavior without a conventional migration.

**Implementation:** Define the version policy and supported update/rollback interval. Tag tested versions and write concise release notes covering user-visible behavior, breaking configuration/defaults, storage shape/index changes, dependency/model requirements, privacy semantics, and operator steps. Introduce versioned, idempotent offline migrations only where actual storage changes need them; do not add a generic migration framework merely for appearance. Record commit/image digest and prompt/persona revision in operator diagnostics.

**Dependencies:** The release/compatibility policy can be established immediately. Actual storage releases then require DATA-03/05 migration evidence, OPS-05 recovery, OPS-06 driver evidence when relevant, and TEST-04 artifact checks.

**Acceptance:** A release candidate upgrades a seeded previous-version database, passes behavior checks, and documents whether rollback is safe. Re-running a needed migration is safe. Notes and checked version match the tag/image, and operators can identify their deployed revision without disclosing configuration secrets.

<a id="dev-02"></a>

### DEV-02 — Ship the repository license with distributed artifacts

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2; confirmed image packaging omission; package metadata is an accompanying improvement.

**Evidence:** [LICENSE:12–13](../LICENSE#L12) says the copyright and permission notice must be included in copies or substantial portions. [Dockerfile:28–29](../Dockerfile#L28) copies application code plus [main.py](../main.py)/[persona.md](../persona.md), but never copies [LICENSE](../LICENSE); [.dockerignore](../.dockerignore) does not prevent it. [pyproject.toml:1–14](../pyproject.toml#L1) has no license/license-files/project URLs metadata. The source repository itself already has a valid MIT license; do not say it lacks a license.

**Scenario and impact:** An operator builds/shares the runtime image containing ThinkMate source without the repository's notice. Users inspecting a future installed package also lack the metadata linking it to the license/repository/support material.

**Implementation:** Copy [LICENSE](../LICENSE) to a known image location and add appropriate OCI/source/license metadata. If building a Python distribution becomes a supported delivery method, declare the correct project/license-files/URLs metadata and verify package contents. Inventory notices for any bundled third-party assets if those are added; do not create unnecessary notices for assets absent from this repository.

**Dependencies:** TEST-04; DEV-01 if publishing package/image releases.

**Acceptance:** A built runtime image contains the exact copyright/permission notice and a discoverable source revision. If a wheel/sdist is supported, inspect it for license/readme/runtime files and verify metadata. CI prevents notice omission in each shipped artifact.

<a id="dev-03"></a>

### DEV-03 — Provide a private security intake and useful contribution templates

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2 for security intake; P3 for template/community polish; missing contributor routing.

**Evidence:** `.github` contains only [.github/workflows/python-checks.yml](../.github/workflows/python-checks.yml). No `SECURITY.md`, issue form, PR template, or maintainership/support policy exists in the checked root inventory. [README.md:79–83](../README.md#L79) links public Telegram discussion and the MIT license; [CONTRIBUTING.md:70–72](../CONTRIBUTING.md#L70) asks for coherent reports and excludes credentials/private data, a good foundation.

**Scenario and impact:** A user discovering memory leakage or credential/log exposure has no documented private disclosure route and may post sensitive reproduction data in a public issue/community chat. Bug reports may omit deployment revision, structured mode, database/runtime version, or redacted logs, slowing diagnosis.

**Implementation:** Write `SECURITY.md` with a real monitored private contact or GitHub private vulnerability-reporting route, supported versions, and honest response expectations. Confirm the route works before publishing it. Add short bug/feature issue forms and a PR template requesting revision/environment, synthetic reproduction, expected/actual behavior, checks, and config/storage/privacy effects. Tell reporters to redact personal messages, tokens, MongoDB URIs, and provider keys. Clarify where support questions belong without claiming unsupported availability guarantees.

**Dependencies:** DEV-01 supported-version policy; privacy findings inform sensitive reproduction guidance.

**Acceptance:** A maintainer receives a test private report; security issues have a private path visible from the repo. A contributor can file a synthetic, diagnosable report and a PR without including secrets; templates link current setup/testing guides and do not require every unrelated check for every change.

<a id="dev-04"></a>

### DEV-04 — Improve GitHub discovery with evidence of the actual product

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P3; optional product/repository polish.

**Evidence:** Live GitHub inspection found absent About description/topics and releases. [README.md:3–16](../README.md#L3) now accurately explains the Telegram companion and clarifies that learning updates memory/prompts rather than model weights; [README.md:18–48](../README.md#L18) supplies a concrete setup path. This is already a strong text foundation. No screenshots/demo asset is in the tracked source inventory examined here.

**Scenario and impact:** Visitors see extensive implementation detail but cannot quickly judge the private-chat/group experience, memory inspection, opt-out, or deployment maturity. Discoverability and contribution conversion suffer; this does not affect runtime correctness.

**Implementation:** Fill the repository description/topics with the real stack/use case, add a short synthetic conversation screenshot or GIF/demo transcript, and show memory evolution/controls with fake participants. Add a current CI badge and a concise project-maturity statement. Keep external demo bots clearly separate from self-hosting guarantees and avoid capacity/privacy claims not established by the audit. Maintain the current limitation disclosures.

**Dependencies:** Address the highest privacy/default issues before recommending broader use; DEV-01 release status.

**Acceptance:** A new visitor can tell within a minute what the bot does, what is retained/shared, how to try it, what is supported, and where to contribute. Demo assets contain only synthetic data, work on GitHub, and match current behavior. Metadata and badges link to real current targets.

<a id="dev-05"></a>

### DEV-05 — Check documentation contracts automatically and update plans as work ships

- [ ] Implemented and verified, or resolved with a recorded decision.

**Priority / category:** P2 for the known behavior mismatch; P3 for broader automated documentation safeguards.

**Evidence:** [CONTRIBUTING.md:58–68](../CONTRIBUTING.md#L58) already requires executable setup commands, relative links, actual behavior/defaults, and link/anchor checks. [docs/README.md](../docs/README.md) indexes subsystem guides. The CI workflow at [.github/workflows/python-checks.yml:30–37](../.github/workflows/python-checks.yml#L30) checks Python only. [docs/roadmap.md:23–34](../docs/roadmap.md#L23) lists proposals that will become stale as the requested improvement work progresses. [tests/run_llm_live.py:1–4](../tests/run_llm_live.py#L1) still embeds a historical shell command while the testing guide gives better Bash/PowerShell commands.

**Scenario and impact:** Renamed guides or symbols break contributor navigation; copied defaults diverge; completed roadmap priorities remain listed as missing. Operators follow an old command even when the main guide is corrected.

**Implementation:** Add a fast local relative-link/anchor checker and lightweight Markdown lint to CI, avoiding flaky outbound link checks on every PR. Keep configuration defaults generated from or checked against the authoritative model where practical. Make setup snippets executable against a fake-config disposable environment. Tie roadmap entries to stable issue IDs/acceptance records, moving shipped work to release notes and recording intentional deferrals. Update architecture diagrams only when code/system boundaries change; they already correctly describe the single process and external services.

**Dependencies:** TEST-01/04 snippet checks; DEV-01 release notes and this document's stable backlog IDs.

**Acceptance:** CI catches a broken local doc link/anchor, missing indexed guide, and a deliberately stale configured default. Linux and advertised PowerShell setup snippets are validated without paid APIs. Each implemented audit item records its verification and clears or revises the corresponding proposed roadmap text.

**Concrete drift to correct:** [docs/development/group_chat.md:210-211](../docs/development/group_chat.md#L210) describes combining multi-sender batches in first-seen-sender order. The current task manager preserves consecutive A → B → A runs; the guide should describe that behavior and separately acknowledge GROUP-02's passive-versus-queued ordering gap. Check the example against the existing sender-order regressions before editing the guide.

<a id="dev-06"></a>

### DEV-06 — Reduce global coupling through small domain boundaries

- [ ] Implemented and verified, or resolved with a recorded decision.

**P2 · Existing maintainability improvement · Source inspection.**

- **Evidence:** [`models`](../app/database/models.py) mixes identity, message history, memory transforms, group settings, engagement, and metrics persistence; [`commands`](../app/handlers/commands.py) combines menus, HTML rendering, authorization, and all command actions. [Configuration](../app/config.py), [logger setup](../app/__init__.py), and [LLM service](../app/services/llm_service.py) create import-time globals; services often use local imports to reach the task-manager singleton. Tests patch those module references deliberately.
- **Implementation:** Introduce narrow interfaces for conversation storage, memory storage, delivery, provider calls, and jobs. Bootstrap configuration/logging/clients at `main` and inject explicit config/clock/client dependencies where changes are already required. Split large modules by responsibility in small behavior-preserving steps. Use typed admission/delivery/job outcomes and document whether an ID means user, group, or conversation. Add focused type checking for new interfaces before attempting a repository-wide strict conversion.
- **Dependencies:** TEST-01 isolated initialization and meaningful contract tests; RUN/LLM result types. This is a gradual refactor alongside fixes, not a prerequisite for every patch or a request to rewrite the application.
- **Done when:** Core memory transformations can run without creating clients/log files, domain interfaces have checked contracts, and tests can supply fake storage/providers/clocks without guessing module import paths. Existing behavior and public commands remain covered during each split.

## Future product and platform options

These options extend the product after the essential boundaries are reliable. Select a small number based on observed user needs. A new tool, memory store, or transport introduces additional data and permission boundaries; each option should earn its complexity through a small evaluated release.

### Memory and personalization

| ID / status | Option and value | Prerequisites | Small first release and acceptance |
| --- | --- | --- | --- |
| FUT-01 · [ ] | **Relevant-memory retrieval.** Select useful memories for the current turn instead of compiling the whole profile. Start with deterministic relevance, recency, scope, and confidence; use embeddings only if measured gains justify them. | MEM-08/09, PRIV-02/05, LLM-03, TEST-06 | Compare replies and memory recall on a fixed corpus under equal token budgets. Deleted, expired, low-confidence, and wrong-scope items are excluded. Preserve a fallback when retrieval fails. |
| FUT-02 · [ ] | **Retrieval over an optional conversation archive.** Recover details that were never extracted before buffer trimming. | PRIV-04/05, DATA-01/03/05, MEM-11, FUT-01 | An explicitly enabled, bounded archive shows source/date with recalled evidence. Forgetting removes both source and index entries; disabled users have no archive. |
| FUT-03 · [ ] | **Explainable memory view.** Show why an item was stored, when it was observed, where it may be used, and whether it was inferred. | MEM-08, PRIV-05, DATA-03 | A private paginated view supports inspect/correct/forget/pin with stable IDs and provenance. It never exposes another participant's private source. |
| FUT-04 · [ ] | **Separate project/task contexts.** Keep work projects, personal preferences, and temporary sessions distinct; allow explicit switching. | PRIV-02/05, MEM-08, DATA-03, LLM-03 | One user can maintain two contexts without cross-contaminating memories. Default scope is visible; context deletion is complete and migration is defined. |
| FUT-05 · [ ] | **User-selected conversation preferences.** Offer verbosity, language, tone, names/pronouns, and explicit accessibility settings without inferring sensitive identity. | PRIV-05, MEM-05, TEST-06 | Settings have clear precedence over inferred preferences, survive maintenance, and can be reset independently of memory. Test two languages and conflicting stored preferences. |
| FUT-06 · [ ] | **Private periodic reflection/digests.** Summarize chosen projects, conversations, or goals when the user requests it. | PRIV-06, RUN-04/05/10, MEM-08, TEST-06 | Opt-in cadence and source/category selection; reliable cancellation and no unwanted sensitive details. Summaries cite their source window and distinguish inferred patterns. |

### Input, output, and user interface

| ID / status | Option and value | Prerequisites | Small first release and acceptance |
| --- | --- | --- | --- |
| FUT-07 · [ ] | **Voice notes and optional spoken replies.** Make interaction easier when typing is inconvenient. | PRIV-03/05, LLM-01/04, RUN-06, DATA-05 | Bound duration/file size and handle unsupported formats. Show/edit the transcript before committing sensitive memory; disclose speech providers and retention; spoken output remains optional. |
| FUT-08 · [ ] | **Images, screenshots, and OCR.** Answer grounded questions about an image and extract user-approved useful details. | PRIV-02/03/05, LLM-01/03/04, TEST-06 | A small image-size/type allowlist, temporary storage cleanup, safe failure feedback, and explicit memory consent. Treat OCR/image text as untrusted data. |
| FUT-09 · [ ] | **Document understanding.** Discuss PDFs/text files with source locations and selectively save agreed facts. | PRIV-05, DATA-05, LLM-03, FUT-01 | Bound file size/pages and parser work; isolate document instructions; cite page/section evidence. Deleting a document removes its derived index/memories under a documented policy. |
| FUT-10 · [ ] | **A private Telegram Mini App for settings and memory.** Make editing a large profile easier than long command responses. | PRIV-01/04/05, MEM-08, DATA-02/03, OPS-01 | Verify Telegram identity server-side and enforce record ownership on every request. Ship inspection/correction first; expiration, replay, and foreign-owner requests fail safely. |
| FUT-11 · [ ] | **Localization and accessibility.** Translate onboarding/commands, improve readable output, and handle language-specific group intent. | GROUP-04, UX-01/03, TEST-06 | One additional language with contributor-reviewed terminology, localized errors and dates, Unicode/RTL checks where applicable, and equivalent privacy/control flows. |
| FUT-12 · [ ] | **Private group summaries and action items.** Summarize consenting shared conversation scope without borrowing personal profiles. | PRIV-02/05, GROUP-01/02/05, MEM-08, LLM-03 | Request a bounded topic/time window and include message links where available. Avoid exposing omitted/private content; distinguish an agreed action from a suggestion. |

### Tools and useful actions

| ID / status | Option and value | Prerequisites | Small first release and acceptance |
| --- | --- | --- | --- |
| FUT-13 · [ ] | **Actual reminders and recurring tasks.** A proactive check-in is not a durable user-requested reminder system. | PRIV-06, DATA-01/03/05, RUN-02/06/10, LLM-01 | Explicit create/list/edit/cancel commands, timezone and daylight-saving rules, durable job state, and duplicate-safe recovery. An acknowledged reminder has a clear delivery/failure outcome after restart. |
| FUT-14 · [ ] | **Calendar integration.** Read availability and create approved events from a user's request. | FUT-13, scoped credentials, PRIV-02/04/05, typed tool outcomes | Start with read-only availability. Show exact title/time/calendar before writes when the request has not already supplied clear authorization; revoke tokens and delete connector data independently. |
| FUT-15 · [ ] | **Current-information search with citations.** Answer questions that require facts newer than the model's knowledge. | LLM-01/02/03/04, TEST-06, PRIV-03 | Read-only search with source dates/links, bounded requests, and explicit unsupported-result behavior. Minimize personal data in queries; web content cannot authorize tools or change system instructions. |
| FUT-16 · [ ] | **Scoped connectors for notes and task systems.** Retrieve/update the user's authorized external records. | PRIV-02/04/05, DATA-01, tool permission model, RUN-06 | One connector, least-privilege scopes, per-user ownership, revocation, audit metadata, and bounded retries. A retrieved record's text cannot authorize an external write. |
| FUT-17 · [ ] | **Useful computation/code tools.** Support calculations and bounded analysis beyond model guessing. | LLM-01/02, isolated execution, explicit file/network limits | Start with a deterministic calculator. Any later code runner has resource/time limits, no host secrets, isolated temporary files, and a documented network policy. Verify cleanup and denied operations. |
| FUT-18 · [ ] | **Additional conversation transports.** A web UI or another chat service may make the companion more accessible. | PRIV-02/04/05, DATA-01/03, DEV-06, RUN-02/06 | Keep a single authoritative principal identity with explicit account linking. Prevent account takeover, cross-transport scope leakage, and duplicate processing; transport-specific delivery results stay observable. |

### Platform, cost, and deployment

| ID / status | Option and value | Prerequisites | Small first release and acceptance |
| --- | --- | --- | --- |
| FUT-19 · [ ] | **Self-hosted/local inference.** Let operators select a supported local endpoint for selected data policies and costs. | TEST-03, LLM-01/02/03/05, OPS-02, TEST-06 | Publish a tested endpoint/model combination and hardware/latency measurements. Preserve schema semantics or safely report unsupported modes. Telegram and database routing still need their own privacy policy. |
| FUT-20 · [ ] | **Model routing and controlled failover.** Use cheaper models for simple operations or a secondary provider during outages. | LLM-01/02/04/05, TEST-03/06, PRIV-02/03 | A small explicit operation/model matrix with per-provider consent boundaries, fallback budgets, and quality/cost measurements. Do not silently send private context to an unapproved provider. |
| FUT-21 · [ ] | **Prompt-prefix caching.** Reduce repeated persona/schema costs where the provider supports it. | LLM-03/04, TEST-03/06 | Measure actual cache benefit and latency at equal quality. Cache keys and invalidation preserve per-user/scope isolation; output/semantic cache reuse requires a separate design. |
| FUT-22 · [ ] | **Webhooks and multiple workers.** Expand beyond the present single-process polling design only when load requires it. | RUN-02/06/10, MEM-11, DATA-01/04, LLM-01, TEST-02/05 | First prove one webhook deployment's authentication and duplicate-safe update handling. Replicas then require shared job leases, ordering, reset fencing, cooldown/admission state, and fault-injection checks. |
| FUT-23 · [ ] | **Optional shared-service quotas and cost administration.** Allocate scarce provider capacity fairly across users/groups. | OPS-01, LLM-01/04, RUN-03/11, TEST-05 | Bounded per-principal quotas, a clear rejection/retry message, and private admin cost reports. Report estimates separately from actual provider usage; avoid exposing message content or raw user IDs in metrics. |
| FUT-24 · [ ] | **Stronger at-rest protection and key operations.** Reduce exposure from database/backups where the threat model requires it. | PRIV-03/04, DATA-02/03, OPS-05 | Inventory data/keys, select a supported encryption design, and prove backup restore/key rotation in isolation. Application encryption may constrain search; define that tradeoff before adding an encrypted index. |

Feature requests should be evaluated against the essential backlog. For example, embeddings do not repair wrong-scope prompts, stale correction targets, or forgotten facts preserved in a summary. More proactive output does not repair consent or quiet-hour timing.

## Recommended release sequence

Use these as milestones, not one enormous PR. Parallelize independent items, keep each change reviewable, and give storage/recovery work a staging rollout. The Motor lifecycle makes its migration time-sensitive even if other feature work is deferred.

| Milestone | Items to pull from this backlog | Exit criterion |
| --- | --- | --- |
| 1. Reproducible baseline and immediate boundaries | TEST-01; PRIV-01/02; OPS-01/02; GROUP-01; DEV-05's known documentation correction | Clean synthetic tests independent of `.env`; public commands/prompts cannot expose personal memory; malformed access config fails; group-off behavior is explicit and enforced. |
| 2. Contained memory correctness fixes | MEM-01/02/04/05/06/07/09/10; MEM-03's immediate dirty-derived-memory exclusion; seed TEST-06 with the five reproduced cases | Focused regressions protect lifecycle, correction targets, event identity, explicit clears, stale mood, destructive replacements, and bounded memory. Stale summaries/insights stop entering prompts after correction. Do not claim provenance is complete until milestone 3. |
| 3. Storage and evidence contracts | TEST-02 plus TEST-04's minimal disposable MongoDB/container CI scaffold; DATA-03; MEM-08/12; DEV-01's upgrade contract; DATA-02/OPS-05 recovery scaffolding | Stable IDs/scope/source time and versioned migrations work on real MongoDB; mixed legacy data is handled; a backup can restore before production migration. |
| 4. Request budgets and model failure semantics | LLM-01/02/03/04/05/06; TEST-03; RUN-08 | Every LLM operation shares global admission and a bounded overall budget; context/output policy preserves compatibility; failed/malformed/declined results are distinguishable. |
| 5. Admission, delivery, and recoverable work | RUN-01/02/03/06; GROUP-02; MEM-11/13/14 | Original messages have stable identity and order; accepted work and chunk delivery have typed outcomes; extraction apply/trim can recover without duplication or privacy regression. |
| 6. User ownership and durable erasure | PRIV-03/04/05/06; MEM-03; DATA-01/02/05 | Private inspect/edit/forget/export works; reset and erase have distinct honest contracts; deletion prevents replay of obsolete sources; consent/preferences survive as specified. |
| 7. Timely scheduling and operator contracts | RUN-04/05/07/09/10/11; DATA-04; OPS-04/05; TEST-04/05 | Freshness and quiet hours are checked at delivery; maintenance is fair and observable; scans and shutdown are bounded; container and recovery drills pass. |
| 8. Supported database driver | OPS-06 with TEST-02/04, DEV-01, OPS-05 | Supported async-driver rollout and rollback/recovery pass actual MongoDB/container contracts. Schedule before upstream critical-fix support ends; do not postpone solely to finish feature milestones. |
| 9. Product quality and release hygiene | GROUP-03/04/05 as needed; UX-01/02/03; TEST-06; DEV-01/02/03/04/05; gradual DEV-06 | Semantic evaluation and measured service checks back the release claims; guides/changelog/upgrade steps match behavior; supported Telegram contexts are explicit. |
| 10. Selected expansion | Pick one FUT item, define users/value/cost, and satisfy its prerequisites | A small release meets its stated quality and permission checks. Review whether it improves actual usage before adding the next capability. |

Some dependencies can be implemented incrementally: reject unknown correction targets before introducing a full evidence graph; enforce a safe group prompt boundary before offering a sharing UI; cap global requests before building a durable queue. Coordinate the shared ID/lifecycle/scope design before separate PRs create incompatible storage formats.

### Decisions to record before the affected implementation

- **Scope defaults:** Whether any personal facts may be shared with a group, how users authorize that sharing, how topic/group changes affect consent, and how legacy global facts are classified. Use the narrow safe boundary while this is unresolved.
- **Reset versus erase:** What each operation removes, how retained group records are handled without disclosing others' data, whether preferences survive, and what external/backed-up copies remain.
- **Memory policy:** Which categories are disabled by default, whether temporary/incidental/sensitive facts need confirmation, and how corrections, contradiction, expiry, and undo work.
- **Accepted work:** When a message is durably admitted, what overload feedback promises, what happens after a crash, and how partial Telegram delivery is represented.
- **Service objectives:** Maximum queue wait, request deadline, maintenance lag, shutdown duration, cost limits, and supported load measured on the actual deployment.
- **Compatibility:** Supported Telegram contexts, Python/OS/container combinations, MongoDB version/topology, and tested provider/model/schema modes. Mark untested combinations plainly.
- **Operations:** Effective retention, backup/restore ownership, recovery targets, alert destinations, and how deletion restrictions apply after restore.

## Validation workflow for implementation

Use the existing locked development workflow from [Contributing](../CONTRIBUTING.md) and [testing](development/testing_guide.md). In a clean development environment:

```sh
uv sync --locked --extra dev
uv run --locked --extra dev black --check .
uv run --locked --extra dev ruff check .
uv run --locked --extra dev python -m pytest -q
```

For a behavior fix, first write a focused failing regression for the named scenario, then run that regression and the relevant subsystem tests. Run the repository's required full checks before completing the PR. Pure documentation changes need source/default/link/anchor checks; they do not need a new behavior test that duplicates the prose.

Real MongoDB tests, artifact smoke checks, load benchmarks, and the semantic corpus are **backlog work**, not commands currently available in CI. Keep ordinary offline tests fast and network-free. Add separately labeled disposable integration jobs with synthetic data and isolated credentials; prove they cannot write to the developer's or production database. Live provider checks are explicit opt-in, bounded in calls/cost, and never required for every ordinary PR.

For changes to lifecycle or storage, test reset/deletion during intake, snapshot selection, model generation, apply, trim, delivery, retry, and restart. Include mixed legacy/corrupt records and a restore. For changes to global admission or scheduling, inject slow/failing dependencies and observe bounded tasks, queue age, fairness, and actual user outcomes. For semantic changes, retain fixed examples and report both gains and regressions at comparable budgets.

### Definition of done for one finding

- The concrete scenario has a regression or other meaningful evidence appropriate to the change.
- The documented acceptance checks pass, and previously implemented privacy/lifecycle/order safeguards remain covered.
- New data fields have a version/migration/default policy; no sensitive content is added to logs or arbitrary metric labels.
- Changed configuration, commands, operator procedures, and subsystem docs match the shipped behavior.
- Storage/deployment changes have a rollout and recovery/rollback procedure checked in a disposable environment.
- The audit item records its final status and evidence; related essential/roadmap entries are revised rather than leaving conflicting claims.

## Completion record template

Use this under a completed finding, or keep a separate progress file keyed by the stable ID. Check the finding's checkbox only after its acceptance criteria are met. A deliberate deferral should state its limits and revisit trigger.

```markdown
### <ID> completion record

Status: complete / partially complete / deferred / disproved
PR or commit:
Decision and resulting behavior:
Validation and measured result:
Migration / operator action:
Rollout and recovery:
Documentation updated:
Remaining limitation / revisit trigger:
Completed on:
```

Do not renumber findings as work ships. If two findings become one implementation, record that shared PR under both IDs. If new evidence disproves a source-inspection scenario, preserve the ID and explanation so the backlog remains trustworthy.
