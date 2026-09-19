FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-default-groups --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 pricehunter
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic ./alembic
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER pricehunter
EXPOSE 8000
CMD ["python", "-m", "pricehunter.apps.api"]
