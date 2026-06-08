FROM python:3.12-slim-bookworm

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Copy application code
COPY connectors/ connectors/
COPY serving/ serving/
COPY scripts/ scripts/
COPY schemas/ schemas/
COPY domains/ domains/
COPY shared/ shared/
COPY scan-config.yaml .

# Default: run MCP server
CMD ["uv", "run", "canon", "serve"]
