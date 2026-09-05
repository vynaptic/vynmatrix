"""Repository contract: every committed test module is discovered by pytest.

Guards against the silent-orphan regression where suites exist on disk but are
absent from ``[tool.pytest.ini_options] testpaths`` and therefore never run in
CI or ``vmdev test all``. ``libs/python/lib_application/tests`` and
``libs/python/lib_infrastructure/tests`` (RLS tenant scoping, encrypted
secrets, market-calendar and version-retirement fail-closed suites) were dark
this way from 2026-06-25 until 2026-07-29.
"""

import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_python_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def test_every_tracked_test_module_is_under_a_testpath() -> None:
    """Every tracked ``test_*.py`` must live under a configured testpaths entry."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths: list[str] = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]

    uncovered = sorted(
        {
            str(Path(tracked_path).parent)
            for tracked_path in _tracked_python_files()
            if Path(tracked_path).name.startswith("test_")
            and not any(
                tracked_path == testpath or tracked_path.startswith(f"{testpath}/")
                for testpath in testpaths
            )
        }
    )

    assert not uncovered, (
        "Tracked test modules exist outside [tool.pytest.ini_options] testpaths, "
        "so neither CI nor `vmdev test all` will ever run them. Add these "
        f"directories to testpaths in pyproject.toml: {uncovered}"
    )
