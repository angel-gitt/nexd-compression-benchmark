#!/usr/bin/env bash
# setup_venv.sh — Create and populate the project virtual environment.
#
# Usage:
#   bash setup_venv.sh          # create .venv and install everything
#   source .venv/bin/activate   # activate afterwards

set -euo pipefail

VENV_DIR=".venv"
PYTHON="${PYTHON:-python3}"

# ── 1. Require Python 3.9+ ────────────────────────────────────────────────────
PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
    echo "ERROR: Python 3.9+ required (found $PY_VERSION)"
    exit 1
fi
echo "Using Python $PY_VERSION at $("$PYTHON" -c 'import sys; print(sys.executable)')"

# ── 2. Create venv ────────────────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment in $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
else
    echo "Virtual environment already exists at $VENV_DIR"
fi

# ── 3. Activate ───────────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── 4. Upgrade pip silently ───────────────────────────────────────────────────
pip install --upgrade pip --quiet

# ── 5. Install dependencies ───────────────────────────────────────────────────
echo "Installing dependencies from requirements.txt ..."
pip install -r requirements.txt

echo ""
echo "✓ Virtual environment ready."
echo ""
echo "To activate it in your shell run:"
echo "    source $VENV_DIR/bin/activate"
