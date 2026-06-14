# syntax=docker/dockerfile:1

# ThinkMate — single-instance aiogram long-polling bot.
# MongoDB is external (Atlas), so this image runs the bot process only:
# no exposed ports, no bundled database.

FROM python:3.12-slim

# - PYTHONUNBUFFERED: stream logs straight to stdout (Coolify/Docker capture them)
# - PYTHONDONTWRITEBYTECODE: no .pyc clutter
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies only — mirrors [project].dependencies in pyproject.toml.
# Test-only deps (pytest, hypothesis, mongomock) are intentionally excluded to
# keep the image small.
RUN pip install \
    "aiogram>=3.15.0" \
    "python-dotenv>=1.0.1" \
    "motor>=3.6.0" \
    "openai>=1.55.0" \
    "pydantic>=2.0.0" \
    "loguru>=0.7.2"

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
