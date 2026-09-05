"""Pytest config for dev_cli tests."""

import sys
from pathlib import Path

DEV_CLI_ROOT = Path(__file__).resolve().parents[1]
if str(DEV_CLI_ROOT) not in sys.path:
    sys.path.insert(0, str(DEV_CLI_ROOT))

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
