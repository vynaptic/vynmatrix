from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_DIR = PROJECT_ROOT / "apps" / "feedback_loop_engine"
LIBS_DIR = PROJECT_ROOT / "libs" / "python"

for candidate in (
    APP_DIR,
    LIBS_DIR / "lib_common",
    LIBS_DIR / "lib_strategy",
    LIBS_DIR / "lib_application",
    LIBS_DIR / "lib_infrastructure",
):
    path_str = str(candidate)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
