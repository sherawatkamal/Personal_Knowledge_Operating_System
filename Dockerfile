# Pinned. Verified running natively on linux/arm64; all three images also publish linux/amd64.
FROM python:3.12.15-slim-trixie

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

# The venv lives outside /app so the dev bind mount of the repo does not hide it.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, for layer caching
COPY pyproject.toml uv.lock ./
# The uv cache lives in a BuildKit cache mount: faster rebuilds, and not baked into the image.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen

CMD ["pkos", "health"]
