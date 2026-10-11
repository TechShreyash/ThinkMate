# Setup guide

[Documentation index](README.md) · [Configuration reference](development/configuration.md) · [Operations](operations.md)

This guide runs ThinkMate from a local checkout. For containers, use the [Docker instructions](operations.md#docker-and-compose).

## Prerequisites

| Component | Requirement |
| --- | --- |
| Python | 3.14; [.python-version](../.python-version) pins 3.14.8 for development and Docker. |
| uv and Git | Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git. |
| Telegram | A bot account and token issued by [BotFather](https://t.me/BotFather). |
| MongoDB | A reachable database, local or hosted; this repository does not start one for you. |
| LLM | A reachable OpenAI-compatible endpoint, its credential if required, and a supported model ID. |

Run commands from the repository root. Use one polling process per Telegram bot token.

## 1. Get the code and dependencies

```bash
git clone https://github.com/TechShreyash/ThinkMate.git
cd ThinkMate
uv python install
uv sync --locked
```

uv uses the project's Python pin and lockfile. Docker installs runtime packages from the same `uv.lock`; `requirements.txt` is a generated locked export that also includes the test packages. You do not need to activate a virtual environment when using `uv run`. See [uv's project guide](https://docs.astral.sh/uv/guides/projects/) for the environment/lockfile workflow.

For development and tests, include the optional extra:

```bash
uv sync --locked --extra dev
```

If the pinned Python download fails, check uv's version and whether that Python release is available for your platform. Do not silently replace the project's pin while diagnosing setup.

## 2. Register a Telegram bot

Open [BotFather](https://t.me/BotFather), send `/newbot`, choose a display name and a username ending in `bot`, and save the token. Keep it out of commits, screenshots, and shared logs.

Create the local configuration:

```bash
cp .env.example .env
```

On PowerShell:

```powershell
Copy-Item .env.example .env
```

Set `TELEGRAM_BOT_TOKEN` in `.env`. The application loads `.env` from the repository root; environment variables already set in the process take precedence. See [app/config.py](../app/config.py).

## 3. Configure MongoDB and the LLM

Edit these settings, replacing the placeholders:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-botfather-token
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=thinkmate_db
LLM_BASE_URL=http://localhost:1234/v1
LLM_API_KEY=replace-with-your-provider-key-or-local-placeholder
LLM_MODEL=replace-with-a-model-your-server-serves
LLM_EXTRACTION_MODEL=
LLM_STRUCTURED_MODE=json_object
ADMIN_USER_IDS=replace-with-your-numeric-telegram-user-id
```

- MongoDB must be running and accessible from the bot process. For a hosted database, configure authentication, network access, and TLS as required by that service.
- Use the exact model ID exposed by your endpoint. Both the conversational model and extraction model need to support ThinkMate's structured requests. Blank `LLM_EXTRACTION_MODEL` falls back to `LLM_MODEL`; the template's `gpt-4o-mini` is an example override.
- `LLM_BASE_URL` is the API base, usually including `/v1`, not the complete `/chat/completions` URL. Local LM Studio commonly uses `http://localhost:1234/v1`; Ollama's documented compatible base is `http://localhost:11434/v1`. Check the endpoint's actual configuration.
- `json_object` is the default structured mode. An endpoint exposing a compatible URL is not proof that its model supports every request field; inspect [LLM integration](development/llm_integration.md) when choosing or troubleshooting a provider.
- `ADMIN_USER_IDS` is a comma-separated list of numeric Telegram user IDs. Blank means any private-chat user can access health/metrics. Invalid entries are ignored, so verify the value carefully.

[Ollama's compatibility reference](https://docs.ollama.com/api/openai-compatibility) describes its supported endpoint and JSON mode. A local LLM avoids sending prompts to a hosted inference provider, but Telegram remains part of the communication path.

## 4. Decide engagement and logging settings

The copied template enables consolidation and proactive check-ins. For a first test, you can disable proactive outreach:

```dotenv
PROACTIVE_INTERVAL_SECS=0
```

Users can also use `/checkins off`. Proactive quiet hours are UTC, with defaults of 16:00–02:00 UTC, corresponding to 21:30–07:30 in India.

If you configure `LOGS_CHANNEL_ID`, grant the bot permission to send messages and documents there. Operational forwarding can include extracted memory updates and reset backups; choose recipients accordingly. Leave `FORWARD_DIAGNOSTICS=False` unless you need routing traces. See [observability](development/observability.md).

All environment configuration is read at import/startup. Restart after changing `.env` or command settings. Editing the persona file is the exception; see [persona customization](development/persona.md).

## 5. Start and verify

```bash
uv run --locked main.py
```

Startup pings MongoDB, initializes audit indexes, restores persisted metrics, prepares schedulers and command menus, and begins Telegram long polling. A MongoDB failure stops startup. A persona path problem logs a warning and uses fallback behavior rather than failing startup.

Verify progressively:

1. Open your bot's private chat and send `/start`. This checks Telegram delivery and profile initialization; the welcome message is static.
2. Send a short ordinary text message. Wait for batching and model generation, and confirm a conversational reply.
3. Send a memorable detail, then continue chatting. Extraction runs only after its buffer threshold and enough older entries are available; memory need not appear after the first message.
4. Use `/profile` in the private chat to inspect memory. Use `/health` from your configured operator account to check MongoDB readiness; it does not probe the LLM endpoint.

Use `Ctrl+C` to stop. See [operations](operations.md#shutdown-and-restarts) for the shutdown limits.

## 6. Optional group setup

Add the bot to a test group and begin with an explicit `@username` mention or reply to its message. For ordinary group chatter and ambient participation, the bot must actually receive those messages. Telegram privacy mode limits visibility; use BotFather's `/setprivacy` if full group visibility is intended. Telegram documents that a privacy-mode change requires re-adding the bot, and group admin bots receive all messages. See [Telegram privacy mode](https://core.telegram.org/bots/features#privacy-mode).

Read [group behavior](development/group_chat.md) before enabling shared use: prompts include the current speaker's personal profile, and personal commands are not all restricted to private chats.

If something fails, use the [troubleshooting table](operations.md#troubleshooting). The full [configuration reference](development/configuration.md) covers tuning after the initial conversation works.
