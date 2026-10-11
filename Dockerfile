# syntax=docker/dockerfile:1

# ThinkMate — single-instance aiogram long-polling bot.
# MongoDB is external (Atlas), so this image runs the bot process only:
# no exposed ports, no bundled database.

FROM python:3.14.8-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

# - PYTHONUNBUFFERED: stream logs straight to stdout (Coolify/Docker capture them)
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Runtime dependencies use the same lockfile as local development.
# Test-only deps (pytest, hypothesis, mongomock) are intentionally excluded to
# keep the image small.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --python /usr/local/bin/python

# Application code + the persona file the bot loads at startup.
COPY app/ ./app/
COPY main.py persona.md ./

# Run as an unprivileged user; make the runtime log dir writable.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/logs \
    && chown -R appuser:appuser /app
USER appuser

# No .env is baked in — configuration is injected as environment variables
# (Coolify UI in production; env_file for local compose).
CMD ["python", "main.py"]
