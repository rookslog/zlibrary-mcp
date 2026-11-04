# Z-Library MCP Server Docker Image
# Multi-stage build for minimal runtime image
#
# Build from project root:
#   docker build -t zlibrary-mcp -f docker/Dockerfile .
#
# Based on PR #9 by @zaggash with fixes applied

# Default keeps source builds/Compose working. Release CI selects packed and
# supplies only release.tgz plus the matching checkout's package-lock.json.
ARG BUILD_INPUT=source

# --- Stage 1: Shared dependency tooling ---
FROM node:22-alpine AS tooling

# UV configuration for portable Python installation
# Keep Python on 3.14 until compiled dependencies ship 3.15 wheels (#230).
# Bump UV_PYTHON here when validating the next Python minor version.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/app/python-bin \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=3.14

WORKDIR /app

# Install UV package manager
RUN apk add --no-cache curl && \
    curl -LsSf https://astral.sh/uv/install.sh | sh && \
    mv /root/.local/bin/uv /usr/local/bin/

FROM tooling AS source

# Copy dependency files first (better layer caching)
COPY pyproject.toml uv.lock ./
COPY package.json package-lock.json ./

# Copy source files
COPY src/ ./src/
COPY lib/ ./lib/
COPY zlibrary/ ./zlibrary/
COPY tsconfig.json ./
COPY scripts/ ./scripts/

# Only acquisition dependencies ship in the runtime image.
RUN uv sync --frozen --no-dev

# Install Node dependencies and build TypeScript
RUN npm ci && npm run build

# No source compilation here: exercise exactly the dist/lib bytes in the npm
# release tarball, with locked production dependencies and no lifecycle hooks.
FROM tooling AS packed
COPY release.tgz package-lock.json ./
RUN tar -xzf release.tgz --strip-components=1 && rm release.tgz && \
    npm ci --omit=dev --ignore-scripts && \
    uv sync --frozen --no-dev

FROM ${BUILD_INPUT} AS builder

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

# Keep downloads in the mounted volume across container restarts
ENV ZLIBRARY_OUTPUT_ROOT=/app/downloads

# Create downloads directory
RUN mkdir -p /app/downloads

# Default command runs the MCP server via SuperGateway
# SuperGateway exposes the stdio MCP server over HTTP
CMD ["--stdio", "node /app/dist/index.js"]
