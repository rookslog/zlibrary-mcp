# Z-Library MCP Server Docker Image
# Multi-stage build for minimal runtime image
#
# Build from project root:
#   docker build -t zlibrary-mcp -f docker/Dockerfile .
#
# Based on PR #9 by @zaggash with fixes applied

# --- Stage 1: Build ---
FROM node:22-alpine AS builder

# UV configuration for portable Python installation
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/app/python-bin \
    UV_PYTHON_PREFERENCE=only-managed

WORKDIR /app

# Install UV package manager
RUN apk add --no-cache curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/

# Copy dependency files first (better layer caching)
COPY pyproject.toml uv.lock ./
COPY package.json package-lock.json ./

# Copy source files
COPY src/ ./src/
COPY lib/ ./lib/
COPY zlibrary/ ./zlibrary/
COPY tsconfig.json ./
COPY scripts/ ./scripts/

# The image is a RAG-capable distribution: PDF and EPUB extraction work without
# an interactive install step. Keep scholar dependencies out so source and npm
# users retain the lightweight core default, and Alpine need not install OpenCV.
RUN uv sync --frozen --no-dev --extra rag

# Install Node dependencies and build TypeScript
RUN npm ci && npm run build

# --- Stage 2: Runtime ---
FROM ghcr.io/supercorp-ai/supergateway:3.4.3 AS runtime

WORKDIR /app

# Set up Python environment paths
ENV PATH="/app/.venv/bin:/app/python-bin/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Copy built artifacts from builder
COPY --from=builder /app/python-bin ./python-bin/
COPY --from=builder /app/.venv ./.venv/
COPY --from=builder /app/zlibrary ./zlibrary/
COPY --from=builder /app/dist ./dist/
COPY --from=builder /app/node_modules ./node_modules/
COPY --from=builder /app/lib ./lib/
COPY --from=builder /app/package.json ./

# Create downloads directory
RUN mkdir -p /app/downloads

# Default command runs the MCP server via SuperGateway
# SuperGateway exposes the stdio MCP server over HTTP
CMD ["--stdio", "node /app/dist/index.js"]
