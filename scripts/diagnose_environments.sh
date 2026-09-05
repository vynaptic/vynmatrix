#!/bin/bash
# Diagnose local Python/vmdev environment state.
#
# Usage:
#   ./scripts/diagnose_environments.sh

echo "=== Python Environments Diagnostic ==="
echo ""

echo "System Python:"
if command -v python3 >/dev/null 2>&1; then
    which python3
    python3 --version
else
    echo "ERROR: python3 not found on PATH"
fi
echo ""

echo "vmdev CLI:"
if command -v vmdev >/dev/null 2>&1; then
    which vmdev
    vmdev --version 2>&1 || echo "ERROR: vmdev not responding"
else
    echo "ERROR: vmdev not installed!"
fi
echo ""

echo "Project venvs:"
if [ -d "build/venvs" ]; then
    ls -l build/venvs/ 2>/dev/null || echo "ERROR: No venvs created yet. Run: vmdev build venvs"
else
    echo "ERROR: No venvs created yet. Run: vmdev build venvs"
fi
echo ""

echo "Custom libraries (wheels):"
if [ -d "build/wheels" ]; then
    ls -l build/wheels/ 2>/dev/null || echo "ERROR: No wheels built yet. Run: vmdev build libs"
else
    echo "ERROR: No wheels built yet. Run: vmdev build libs"
fi
echo ""
