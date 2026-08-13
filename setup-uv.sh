#!/bin/bash
# UV-based setup script for Z-Library MCP server (v2.0.0)
#
# This script sets up the Python environment using UV (modern Python package manager)
# Replaces the old setup_venv.sh which used cache venv

set -e

# Check Python version.
# Ask Python for its own version rather than parsing `python3 --version` with
# `grep -oP`: PCRE mode is a GNU extension that BSD grep on macOS rejects with
# "invalid option -- P" (issue #14).
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found on PATH. Install Python 3.10+ and retry."
    exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
REQUIRED="3.10"
if [ "$(printf '%s\n' "$REQUIRED" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED" ]; then
    echo "Error: Python $REQUIRED+ required, found $PYTHON_VERSION"
    exit 1
fi
echo "Python $PYTHON_VERSION detected (>= $REQUIRED required)"

echo "🚀 Z-Library MCP - UV Setup (v2.0.0)"
echo "====================================="
echo ""

# Check if UV is installed
if ! command -v uv &> /dev/null; then
    echo "❌ UV not found"
    echo ""
    echo "UV is required for Python dependency management."
    echo ""
    echo "📥 Install UV:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  # Or: pip install uv"
    echo ""
    echo "Then run this script again."
    echo ""
    echo "See: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

UV_VERSION=$(uv --version)
echo "✅ UV found: $UV_VERSION"

# Dev dependencies live in [dependency-groups] (PEP 735), which uv gained in
# 0.4.27. An older uv does not read that table and installs no dev group, so
# `uv run pytest` fails with "not found" — a symptom that points nowhere near
# the cause. Check it here rather than letting it surface three steps later.
# Same `sort -V` comparison used for the Python version above.
UV_NUMBER=$(printf '%s' "$UV_VERSION" | awk '{print $2}')
UV_REQUIRED="0.4.27"
if [ -n "$UV_NUMBER" ] && \
   [ "$(printf '%s\n' "$UV_REQUIRED" "$UV_NUMBER" | sort -V | head -n1)" != "$UV_REQUIRED" ]; then
    echo "Error: UV $UV_REQUIRED+ required (dev dependencies use PEP 735 [dependency-groups]), found $UV_NUMBER"
    echo "Upgrade with: uv self update    (or reinstall: curl -LsSf https://astral.sh/uv/install.sh | sh)"
    exit 1
fi
echo ""

# npm/end-user setup passes --no-dev. Source contributors use the default,
# which explicitly names the PEP 735 development group instead of relying on
# uv's implicit default-group behavior.
case "${1:-}" in
    "")
        SYNC_ARGS=(sync --group dev)
        SETUP_TIER="core plus contributor development tools"
        ;;
    --no-dev)
        SYNC_ARGS=(sync --no-dev)
        SETUP_TIER="lightweight end-user core"
        ;;
    *)
        echo "Usage: $0 [--no-dev]"
        echo "  no argument  Source/contributor setup with the PEP 735 dev group"
        echo "  --no-dev     End-user core setup without development tools"
        exit 2
        ;;
esac

# Initialize UV project (creates .venv and installs the selected tier)
echo "📦 Installing Python dependencies with UV..."
echo "   This will:"
echo "   - Create .venv/ directory"
echo "   - Install $SETUP_TIER from pyproject.toml"
echo "   - Install vendored zlibrary as editable"
echo "   - Generate uv.lock for reproducibility"
echo ""

uv "${SYNC_ARGS[@]}"

echo ""
echo "✅ Dependencies installed"
echo ""

# Verify zlibrary import
echo "🔍 Verifying zlibrary installation..."
if .venv/bin/python -c "from zlibrary import Extension, Language; print('✅ zlibrary ready')" 2>&1; then
    echo ""
    echo "🎉 Python environment setup complete!"
    echo ""
    echo "📋 Next steps:"
    echo "  1. npm install         # Install Node.js dependencies"
    echo "  2. npm run build       # Build TypeScript"
    echo "  3. Configure in your MCP client"
    echo ""
    echo "💡 Tips:"
    echo "  - uv.lock file tracks exact dependencies (commit this!)"
    echo "  - .venv/ is gitignored (local to your machine)"
    echo "  - To add deps: uv add package-name"
    echo "  - To update deps: uv sync --upgrade"
else
    echo ""
    echo "❌ zlibrary import failed"
    echo "   Check the error messages above"
    exit 1
fi
