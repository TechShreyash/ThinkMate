# Testing guide

Tests use pytest, pytest-asyncio, mongomock, and Hypothesis. The supported Python
version is **3.14**, as declared in [`pyproject.toml`](../../pyproject.toml).
Run commands from the repository root. See [setup](../setup_guide.md) for installing `uv`.

## Install and run

The test packages are in the optional `dev` extra, so request that extra explicitly:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest
```

Useful focused commands:

```bash
uv run --locked --extra dev python -m pytest tests/test_database.py
uv run --locked --extra dev python -m pytest tests/test_group_extraction.py -q
uv run --locked --extra dev python -m pytest -k atomic_trim
uv run --locked --extra dev python -m pytest --collect-only -q
uv run --locked --extra dev python -m pytest -s
```

[`pyproject.toml`](../../pyproject.toml) adds the repository root to `pythonpath`,
sets `asyncio_mode = "auto"`, and restricts discovery to `tests/`. The manual
`run_llm_live.py` file is not a normal `test_*.py` module and is not collected.
[`requirements.txt`](../../requirements.txt) is a generated locked export of both runtime and test packages;
installing only the project without the `dev` extra does not install test tools.

Do not use a historical passing-test count as a guarantee. Collection, outcomes,
warnings, and duration come from the current checkout/environment and test command.

## Isolation and its limits

[`tests/conftest.py`](../../tests/conftest.py) provides autouse fixtures:

- Fresh mongomock database/client per test, patched through
  `app.database.connection.get_db` and `get_db_client`.
- Async adapters for the synchronous mongomock collection/cursor API.
- Reactions disabled by default, with the previous config restored afterward.
- A synthetic logs-channel ID when no target is configured, and forwarder-state resets.

Database tests normally need no MongoDB server. This mock does not implement all Motor
features or server behavior: real TTL expiry, transactions, indexes under load, network
timeouts, connection pooling, and distributed concurrency still require integration
validation.

**There is no global LLM/Telegram network blocker.** Tests mock the specific methods
or clients they exercise, usually with `AsyncMock`. Patch background jobs as well as
foreground calls when a test crosses extraction/compression thresholds. A new or
incorrectly patched test can contact the configured LLM, Telegram, or an unpatched
connection path.

Imports still load `.env` and process environment, create local logs, and construct the
LLM singleton. Some config tests reload the module and assume default settings. Run
with development settings and fake credentials, not a production `.env`. Existing
process variables override `.env`; deleting a variable in a test can cause dotenv to
reload it. Override/save/restore config carefully and cancel tasks you start.

## Where to look for coverage

This table groups current tests by behavior rather than old implementation phases.
The complete live inventory is the `tests/` directory and `--collect-only` output.

| Area | Representative files and coverage |
| --- | --- |
| Database and memory | [`test_database.py`](../../tests/test_database.py), [`test_guards_and_compression.py`](../../tests/test_guards_and_compression.py), [`test_hardening.py`](../../tests/test_hardening.py), [`test_compression_safety.py`](../../tests/test_compression_safety.py): CRUD, deduplication, atomic trim, retry/trim policy, budgets, cooldowns, and failure preservation. |
| Batching and delivery | [`test_batching_and_concurrency.py`](../../tests/test_batching_and_concurrency.py), [`test_telegram_text.py`](../../tests/test_telegram_text.py), [`test_reactions.py`](../../tests/test_reactions.py): coalescing, queues/locks, rate limits, chunking, reply fallback, and reactions. |
| Commands and persona | [`test_command_skip.py`](../../tests/test_command_skip.py), [`test_command_preservation.py`](../../tests/test_command_preservation.py), [`test_publish_commands.py`](../../tests/test_publish_commands.py), [`test_persona_config.py`](../../tests/test_persona_config.py), [`test_prompt_language.py`](../../tests/test_prompt_language.py): command catch-all exclusions, menus, persona validation/reload/fallback, and prompt-language expectations. [Context tests](../../tests/test_system_prompt_context.py) exercise hostile-context serialization and missing-mood handling; [check-in tests](../../tests/test_checkin_prompt.py) cover grounded instructions and language preferences; [memory prompt contracts](../../tests/test_memory_prompt_contracts.py) cover attribution/date guidance and incomplete replacement-response preservation through both real SDK parser paths with mocked HTTP. |
| Groups and member memory | `test_group_*.py`, [`test_ambient_gate.py`](../../tests/test_ambient_gate.py), [`test_affinity_and_commands.py`](../../tests/test_affinity_and_commands.py), [`test_membership_intro.py`](../../tests/test_membership_intro.py), [`test_dm_unchanged_regression.py`](../../tests/test_dm_unchanged_regression.py): group routing, attributed buffers, shared/member memory, affinity, ambient behavior, and private-chat regression coverage. |
| Implicit addressing and spam | `test_prop_*.py`, [`test_group_router_implicit.py`](../../tests/test_group_router_implicit.py), [`test_recency_commit.py`](../../tests/test_recency_commit.py): Hypothesis properties for recency/cooldown, mention/spam/burst interactions, degradation, state pruning, and successful-delivery tracking. |
| Metrics and operations | `test_metrics*.py`, [`test_health_and_command.py`](../../tests/test_health_and_command.py), [`test_logs_channel_config.py`](../../tests/test_logs_channel_config.py), [`test_observability_command_wiring.py`](../../tests/test_observability_command_wiring.py): registry behavior, instrumentation, probes, forwarding, and authorization/command wiring. |
| Consolidation | `test_consolidation_*.py`: schemas, due-user query, scheduler, apply logic, and failed-call preservation. |
| Engagement and proactive messages | `test_engagement_*.py`, [`test_proactive_scheduler.py`](../../tests/test_proactive_scheduler.py): temporal context, opt-out/onboarding commands, mood history, check-in decline/sending, quiet hours, and unanswered streaks. |

For behavior explanations, read [memory engine](memory_engine.md),
[Telegram bot](telegram_bot.md), [group chat](group_chat.md), and
[observability](observability.md) before updating expectations.

## Writing or changing a test

Patch the reference the caller actually uses. Several services import singleton
objects or functions at module scope; patching a different module's name may miss the
call. Use fresh state managers/registries where possible, and assert observable
behavior rather than relying on module-global state left by another test.

Choose checks that distinguish the bug from a working implementation. For async
flows, await the actual operation or bound the wait; restore timing settings and stop
spawned typing/sweeper/batch tasks. Property tests should cover the boundaries that
matter, such as exact recency/message-count limits and mention-stripped burst matching.
Mock tests establish Python behavior; they do not prove live provider compatibility,
Telegram permissions, sustained capacity, or semantic memory quality.

## Manual live LLM smoke check

[`tests/run_llm_live.py`](../../tests/run_llm_live.py) performs a reply-bundle request
and an extraction request using configured settings. It can spend provider credits
and writes best-effort audit rows to the configured MongoDB under synthetic user ID
`999`. Use a development endpoint/database; it is not an isolated automated test and
does not verify every memory operation or Telegram delivery.

Bash:

```bash
PYTHONPATH=. uv run --locked --extra dev python tests/run_llm_live.py
```

PowerShell:

```powershell
$env:PYTHONPATH = '.'
uv run --locked --extra dev python tests/run_llm_live.py
```

The script's small assertions check usable outputs, not factual accuracy. A compatible
endpoint must support the configured model and required structured-output mode. Do
not infer support for a different provider/model from one successful smoke check.

[Documentation index](../README.md)
