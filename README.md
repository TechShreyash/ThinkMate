# ThinkMate

A Telegram companion that remembers a structured profile across conversations. ThinkMate uses an OpenAI-compatible language model for replies and background memory updates, with MongoDB for persistence.

**Start here:** [Setup](docs/setup_guide.md) · [Documentation index](docs/README.md) · [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md)

## What it does

- Replies to text in private chats and selectively participates in groups.
- Batches messages from the same sender, generates a reply and optional emoji reaction together, and keeps recent conversation context.
- Extracts facts, beliefs, events, communication preferences, and mood into a persistent profile. Compression and scheduled consolidation reduce repetition and add behavioral insights.
- Loads its voice from [persona.md](persona.md); edits are picked up when the file changes.
- Supports onboarding, memory inspection/reset, reaction preferences, group controls, and optional proactive check-ins.
- Provides Telegram health/metrics commands, rotating logs, and optional operational forwarding to a logs channel.

“Learning” here means updating stored memory and prompts; ThinkMate does not train model weights. It has no web browsing, file processing, embeddings, or vector search.

## Run locally

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), a Telegram bot token, a reachable MongoDB instance, and an LLM endpoint that supports the requests described in the [LLM guide](docs/development/llm_integration.md). The project supports Python 3.14 and pins 3.14.8 in [.python-version](.python-version).

```bash
git clone https://github.com/TechShreyash/ThinkMate.git
cd ThinkMate
uv python install
uv sync --locked
cp .env.example .env
```

On PowerShell, replace the last command with `Copy-Item .env.example .env`. Edit `.env` before starting:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-your-bot-token
MONGODB_URI=mongodb://localhost:27017
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=replace-with-your-provider-key-or-local-placeholder
LLM_MODEL=replace-with-a-model-served-by-your-endpoint
LLM_EXTRACTION_MODEL=
ADMIN_USER_IDS=replace-with-your-numeric-telegram-user-id
```

Leaving `LLM_EXTRACTION_MODEL` blank uses `LLM_MODEL`. The template contains example model IDs; they must match your provider. `ADMIN_USER_IDS` takes numeric IDs, not usernames. See [configuration](docs/development/configuration.md) for defaults and validation limits.

```bash
uv run --locked main.py
```

Send `/start` to check command delivery, then send an ordinary text message to test the LLM. `/start` itself does not make an LLM call. See [setup and troubleshooting](docs/setup_guide.md) for the full walkthrough and [operations](docs/operations.md) for Docker/Compose deployment.

## How it works

The **reply path** accepts and batches text, appends it to a chat buffer, assembles persona + memory + recent history, and asks the LLM for a reply. The **memory path** runs extraction, compression, and periodic consolidation in background tasks. Replies do not wait for extraction to finish, so newly extracted details may appear in a later turn.

Buffers and task locks are keyed by chat ID. Personal profiles are keyed by user ID; groups also have a shared profile keyed by their chat ID. Read the [architecture](docs/architecture.md) and [code map](docs/repository_structure.md) to follow the implementation.

## Deployment and data behavior

ThinkMate currently runs as one long-polling Python process per bot token. Queues, locks, caches, and routing cooldowns live in that process; multiple replicas are not supported by the current design. MongoDB and the LLM server are separate services. See [performance and scaling](docs/development/performance_and_scaling.md).

Review these behaviors before sharing a bot with other people:

- A group reply can include the triggering participant's personal memory from other chats. `/profile` can display personal memories in a group. These are current implementation behaviors, described in the [group guide](docs/development/group_chat.md).
- `/reset` deletes the caller's personal profile and DM buffer. It does not delete group history, affinity settings, audit entries, or external logs/backups, and it does not cancel work already running.
- Proactive check-ins are enabled by default for eligible inactive users. Users can use `/checkins off`; an operator can set `PROACTIVE_INTERVAL_SECS=0` to disable the scheduler.
- Empty `ADMIN_USER_IDS` permits health/metrics access to any private-chat user. Set an explicit operator allowlist for a shared deployment.
- Telegram processes messages, and the configured LLM provider receives prompts containing conversation and memory. MongoDB and diagnostic logs also retain data. Self-hosting the bot alone does not make the whole system local or guarantee confidentiality.

Details and safeguards: [database](docs/development/database.md), [observability](docs/development/observability.md), and [safeguards and limitations](docs/development/hardening_plan.md). The [roadmap](docs/roadmap.md) distinguishes current capabilities from proposed improvements.

## Development

```bash
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest -q
uv run --locked python scripts/check_docs.py
```

See [Contributing](CONTRIBUTING.md) for the workflow and [testing](docs/development/testing_guide.md) for targeted tests and the separate live LLM check.

## Community and license

Project bot: [@ThinkMate_AIBot](https://t.me/ThinkMate_AIBot). Updates: [@TechZBots](https://t.me/TechZBots). Discussion: [TechZBots Support](https://t.me/TechZBots_Support). These are project links; service availability is not checked by the documentation validator.

Licensed under the [MIT License](LICENSE).
