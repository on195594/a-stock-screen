# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

# Install uv for fast, frozen-lockfile package management
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install project dependencies in a standalone virtualenv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code
COPY app.py auth.py services.py workspace.py screen.py manage.py worker.py ./
COPY tests/fixtures ./tests/fixtures
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8550
VOLUME ["/app/data"]

ENTRYPOINT ["/entrypoint.sh"]
