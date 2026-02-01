#!/bin/bash
set -Eeuo pipefail

echo "Setting up pip development environment..."

# Get the workspace directory
WORKSPACE_DIR="${WORKSPACE_DIR:-/workspaces/pip}"
cd "$WORKSPACE_DIR"

echo "Working directory: $(pwd)"
echo "Python version: $(python3 --version)"

# Upgrade pip
python -m pip install --upgrade pip

# Install nox and test dependencies
python -m pip install nox --group test

# Create common wheels cache directory if it doesn't exist
mkdir -p tests/data/common_wheels

# Build common wheels needed by tests
python -m pip wheel -w tests/data/common_wheels --group test-common-wheels

# Install pip in editable mode
python -m pip install -e .


echo ""
echo "=========================================="
echo "Development environment setup complete!"
echo "=========================================="
echo "Python: $(which python)"
echo "pytest: $(which pytest)"
echo "You can now run pytest directly with: pytest -n auto"
echo ""
