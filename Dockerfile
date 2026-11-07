FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY uv.lock pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-install-project --no-dev

COPY README.md LICENSE ./
COPY tarr ./tarr
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev

FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 999 nonroot && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app
COPY --from=builder --chown=nonroot:nonroot /app .

ENV PATH="/app/.venv/bin:$PATH"

USER nonroot

ENV CONFIG_FILE=/config/config.yaml

CMD ["tarr", "--daemon"]

FROM builder AS test-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-default-groups --group test

FROM runtime AS test
COPY --from=test-dependencies --chown=nonroot:nonroot /app/.venv /app/.venv
COPY --chown=nonroot:nonroot e2e ./e2e
ENV PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest_cache"
CMD ["python", "-m", "pytest", "-v", "-s", "e2e"]

FROM runtime AS production
