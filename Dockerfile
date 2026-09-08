# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /build
RUN python -m venv /opt/venv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

FROM python:3.12-slim

LABEL org.opencontainers.image.title="nzbget-mcp" \
      org.opencontainers.image.description="FastMCP server wrapping the NZBGet JSON-RPC API" \
      org.opencontainers.image.source="https://nzbget.com/documentation/api/"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    NZBGET_HOST=localhost \
    NZBGET_PORT=6789

COPY --from=builder /opt/venv /opt/venv

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mcp
USER mcp
WORKDIR /home/mcp

EXPOSE 8000

# Verifies both that the MCP port is accepting connections and that NZBGet answers.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-m", "nzbget_mcp.healthcheck"]

ENTRYPOINT ["nzbget-mcp"]
