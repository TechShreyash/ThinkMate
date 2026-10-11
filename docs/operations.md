# Operations runbook

[Documentation index](README.md) · [Setup](setup_guide.md) · [Configuration](development/configuration.md) · [Observability](development/observability.md)

Run one ThinkMate process per bot token. It uses outbound Telegram polling, MongoDB, and an LLM endpoint; it does not serve HTTP or require an inbound port.

## Docker and Compose

The supplied [Dockerfile](../Dockerfile) uses Python 3.14.8 and uv 0.13.0, copies application code and the persona, and runs as an unprivileged user. It installs runtime packages from the checked-in `uv.lock` with `uv sync --locked --no-dev --no-install-project`, so local and Docker installs share the resolved dependency versions. The image excludes the optional `dev` extra.

The [Compose file](../docker-compose.yml) runs only the bot. It mounts a named volume for `/app/logs`, limits Docker's stdout log growth, and optionally reads the repository's `.env`. Its `env_file.required: false` syntax needs Docker Compose 2.24.0 or newer. See [Docker's environment-file documentation](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/).

Prepare `.env` using the [setup guide](setup_guide.md). Then:

```bash
docker compose version
docker compose up --build -d
docker compose logs -f thinkmate
```

MongoDB and the LLM must be reachable **from the container**. Container `localhost` refers to the container itself. Use your database service address, hosted connection string, or a host address supported by your Docker installation. Verify it rather than copying a host-local URL unchanged.

Configuration can also be provided by a deployment platform, but it must reach the container's environment. This Compose file has no `environment:` mapping forwarding arbitrary host-shell variables. Its local path is `env_file`; a platform needs its own environment-injection mechanism. Do not bake credentials into the image.

No HTTP healthcheck or published port is configured. The restart policy restarts a stopped process; it does not detect a responsive process stuck behind a slow LLM.

## Persona edits in a container

The default persona is copied into the image. Editing the host's `persona.md` does not modify a running container unless you mount it. Rebuild/recreate the service, or add a local Compose override:

```yaml
# docker-compose.override.yml
services:
  thinkmate:
    volumes:
      - ./persona.md:/app/persona.md:ro
```

The process checks the persona's file modification time on use. Environment changes still require container recreation. See [persona customization](development/persona.md).

## Basic checks

- Review startup logs for MongoDB connectivity and polling. Startup checks MongoDB; it does not validate model availability with a real LLM generation.
- Use `/health` from a configured operator account for process liveness and a MongoDB ping. Use `/metrics` to inspect reply/LLM latency, failures, throttle drops, queue drops, and background work.
- Send a normal private text message to test the LLM and Telegram delivery. A static `/start` welcome is not an inference test.
- Inspect extraction/compression/consolidation failures before assuming memory has been saved. A large buffer may also be trimmed after repeated extraction failures.

Set `ADMIN_USER_IDS` explicitly. Empty allows any private-chat user to read operations reports; listed operators can request them in groups too, so choose the invoking chat carefully.

The [observability guide](development/observability.md) explains metric names, persistence, log forwarding, and audit queries. It also distinguishes cumulative counters from process-local uptime.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| uv cannot install Python | uv version, platform support, network access, and availability of the pinned release. |
| Startup stops at MongoDB | URI/authentication, network allowlist, DNS/TLS, server availability, and container-relative addresses. |
| Telegram authentication fails | Correct token, no placeholder value or extra whitespace. |
| Polling reports a conflict | Stop the other process using this token; check that a previous deployment is gone. Startup explicitly removes the webhook and pending updates before polling; inspect failures of that request. |
| `/start` works but chat fails | LLM base URL, credential, exact model IDs, JSON-mode support, provider limits, and logged errors. |
| Responses take a long time | Configured batch delay, per-chat queueing, LLM latency/retries, and the 610-second code-default request timeout. The timeout is configurable and endpoint-specific. |
| Messages receive no response | Throttle/input/queue guards; startup-old messages; disabled command; groupbot/groupmode/member mode; spam or ambient gate decisions. |
| Group chatter is invisible | Telegram privacy mode and bot membership; use an explicit mention/reply first. See [setup](setup_guide.md#6-optional-group-setup). |
| Memories do not appear immediately | Extraction threshold, newest entries retained by `CHAT_BUFFER_TRIM`, failed extraction, and whether the group only buffered passive messages. |
| Memory stays over budget | Protected fields, compression cooldown, empty/failing compression, and budget-enforcement limits. See [memory](development/memory_engine.md). |
| Logs channel is silent | `LOGS_CHANNEL_ID`, bot permission to send messages/documents, and forwarding controls. Delivery is best-effort. |
| Check-ins stop | Opt-out, quiet UTC hours, insufficient memory/inactivity, unanswered streak cap, or permanent blocked/permission/not-found delivery failures. Transient send failures retain the opt-in preference and wait for the next eligible attempt. |
| TTL retention changed but old audit entries remain | Check the MongoDB TTL index and startup warnings. An existing index may need a deliberate administrative change; modifying the setting alone does not guarantee it was replaced. |
| Persona changes have no effect | `PERSONA_FILE`, process working directory, file modification time, container mount/image, and code-supplied behavior rules. |

## Data retention and backups

MongoDB stores profiles, recent raw chat text, group-member settings, audit records, and metrics. Buffer limits bound entry count, not retention time. Audit expiry depends on a working TTL index; file logs use separate rotation/retention settings. Telegram forwarding can send extracted memory, diagnostic previews, and reset exports outside MongoDB.

Use your MongoDB deployment's backup and restore procedures for the full database. The supplied Compose volume persists file logs only; it does not back up MongoDB. Validate restores against a separate database and test bot account before relying on them.

`/reset confirm` is a user-memory reset, not a database backup or full erasure workflow. It can forward the caller's profile and DM buffer as JSON to the configured logs channel before deletion. Export is optional and best-effort; success text mentions restoration only after the upload succeeds. There is no built-in restore command. See [database/reset scope](development/database.md).

## Updates

1. Record the deployed revision and back up MongoDB using your deployment's procedures.
2. Review changes to dependencies, schemas, settings, prompts, and data behavior; the [roadmap](roadmap.md) is not a release log.
3. Install and verify in a separate checkout/environment with a separate bot token/database.
4. Stop the previous polling process before starting the updated one.
5. Apply changed environment values and deploy the checked revision.

For a local uv checkout:

```bash
uv sync --locked
uv run --locked main.py
```

For Compose after updating the checkout/configuration:

```bash
docker compose up --build -d
docker compose logs -f thinkmate
```

## Shutdown and restarts

Use `Ctrl+C` locally or `docker compose stop` for containers. [main.py](../main.py) runs cleanup after polling exits or startup fails. It cancels registered schedulers and stops accepting updates, giving active commands/handlers up to 10 seconds to finish. It then stops admitting queued chat work and gives owned reply/memory tasks another 10 seconds. Pending batches dispatch immediately during this second drain; remaining tasks are cancelled.

LLM audits then receive a 5-second drain before the shared LLM client closes. Metrics flush, the shutdown notice, a separate 5-second warning/error-sink drain, and the final log upload run while Telegram is available. Telegram and MongoDB clients close afterward. Each asynchronous cleanup step has a 15-second outer timeout so one failure does not skip later cleanup. These are separate windows, not a total shutdown deadline; configure the container/service termination grace period to allow cleanup. Crashes, forced termination, and drain timeouts can still lose pending work; persisted buffers/profiles remain.

Startup calls `delete_webhook(drop_pending_updates=True)` before polling, intentionally discarding Telegram's pending backlog. The message-age guard separately rejects sufficiently old messages relative to process startup when enabled. See [Telegram integration](development/telegram_bot.md) and [safeguards](development/hardening_plan.md).
