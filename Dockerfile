FROM python:3.12-slim-bookworm

# Install ODBC Driver 18 (required for Fabric SQL connector)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg2 apt-transport-https ca-certificates unixodbc \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
        | sed 's|signed-by=.*]|signed-by=/usr/share/keyrings/microsoft-prod.gpg]|' \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev

COPY canon/ canon/
COPY connectors/ connectors/
COPY serving/ serving/
COPY scripts/ scripts/
COPY schemas/ schemas/
COPY domains/ domains/
COPY shared/ shared/
COPY scan-config.yaml .

EXPOSE 8000

ENV CANON_MCP_TRANSPORT=streamable-http
ENV CANON_MCP_PORT=8000
ENV CANON_REPO_ROOT=/app

CMD ["uv", "run", "canon", "serve"]
