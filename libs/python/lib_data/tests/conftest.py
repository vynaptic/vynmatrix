"""Pytest config for lib_data tests.

``lib_data`` is built/distributed as a wheel and is not pip-installed in the
dev tree, so add its source root (and its only dependency, ``lib_common``) to
``sys.path`` for local collection — without this, ``pytest libs/python/lib_data``
fails with ``ModuleNotFoundError: No module named 'lib_data'`` unless the caller
pre-sets PYTHONPATH. Mirrors the conftest in the app test suites.
"""

from __future__ import annotations

import sys
from pathlib import Path

LIB_DATA_ROOT = Path(__file__).resolve().parents[1]  # libs/python/lib_data
LIBS_DIR = LIB_DATA_ROOT.parent  # libs/python

for candidate in (LIB_DATA_ROOT, LIBS_DIR / "lib_common"):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
