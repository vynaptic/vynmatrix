"""Pytest config for lib_indicators tests.

``lib_indicators`` is built/distributed as a wheel and is not pip-installed in
the dev tree, so add its source root to ``sys.path`` for local collection.
Mirrors the conftest in the other lib test suites.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIB_ROOT = Path(__file__).resolve().parents[1]  # libs/python/lib_indicators
LIBS_DIR = LIB_ROOT.parent  # libs/python

for candidate in (LIB_ROOT, LIBS_DIR / "lib_common"):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
