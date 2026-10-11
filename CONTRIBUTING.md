# Contributing to ThinkMate

Start with the [architecture](docs/architecture.md) and [repository map](docs/repository_structure.md). Use the [documentation index](docs/README.md) to find the subsystem involved in your change.

## Prepare a development environment

From the repository root:

```bash
uv python install
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest -q
```

The `dev` extra supplies the test dependencies. Automated tests use an in-memory database fixture; they are not a production MongoDB integration test. LLM/Telegram mocks are supplied by individual tests, not globally by the fixture. See [testing](docs/development/testing_guide.md).

Running the bot separately requires the [.env.example](.env.example) settings and external services described in [setup](docs/setup_guide.md). Use a separate bot token and database for live development.

## Refresh dependencies

Update dependency constraints in [pyproject.toml](pyproject.toml) when needed, then resolve the newest compatible stable releases and regenerate the pip export:

```bash
uv lock --upgrade
uv export --locked --extra dev --no-emit-project --format requirements.txt --output-file requirements.txt
uv sync --locked --extra dev
uv run --locked --extra dev python -m pytest -q
uv run --locked python scripts/check_docs.py
docker build -t thinkmate .
```

Commit `pyproject.toml`, `uv.lock`, and the generated `requirements.txt` together when their contents change. Do not edit the export by hand. Docker installs runtime packages from `uv.lock` without the `dev` extra; the export includes test packages for pip-based development. Review release notes and relevant integration behavior when upgrading major versions.

## Keep changes coherent

- Put Telegram parsing/authorization in handlers, conversation/memory work in services, persistence in database models, and LLM instructions in prompts.
- When changing a structured response, update its schema, prompt, parser/caller assumptions, and relevant regression coverage together.
- When changing settings, update [app/config.py](app/config.py), [.env.example](.env.example), and the [configuration reference](docs/development/configuration.md). Distinguish a sample override from a code default.
- When changing storage, inspect private and group flows. Personal profiles can have writers from multiple chats, and per-chat locks do not cover all of those writers.
- Update the relevant guide with the actual behavior and limitations. Future ideas belong in the [roadmap](docs/roadmap.md).

## Verify a change

Run focused tests for the behavior you changed; run the broader suite when shared paths are affected. Commands in the [testing guide](docs/development/testing_guide.md) map subsystems to coverage.

```bash
uv run --locked --extra dev python -m pytest -q
uv run --locked python scripts/check_docs.py
git diff --check
```

The manual `tests/run_llm_live.py` script makes real requests and can incur provider usage. It is separate from pytest and should only be run against an intentionally configured test endpoint.

## Maintain documentation

- Write current behavior in present tense. Mark proposed features explicitly; do not check off unmeasured acceptance targets.
- Use relative links to source, tests, and neighboring guides so they work in a clone and on GitHub.
- Give every document one top-level heading. Leave blank lines after headings and before lists/fenced blocks.
- Keep setup commands executable from the stated directory. Describe what each verification step actually proves.
- Keep default values in the configuration reference instead of repeating full tables in multiple guides.
- Add new human-facing guides to [docs/README.md](docs/README.md). The checker also requires authored Markdown to be reachable from the project README.
- Treat [persona.md](persona.md) as runtime input: changing its text can change bot behavior. Use the [persona guide](docs/development/persona.md) and appropriate prompt regression coverage.

The documentation checker uses only Python's standard library. It validates local links/Markdown anchors, basic heading/fence structure, index reachability, and environment-reference coverage. It skips generated environments, caches, logs, and external URL availability; semantic claims still need code review.

## Describe the result

Explain the user-visible behavior, relevant limits or tradeoffs, and how you verified it. Mention any database/configuration changes and whether operators need to restart or adjust settings. Keep credentials and personal conversation data out of patches and examples.
