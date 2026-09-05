"""Sprint B / Sprint C consolidation regression tests.

Pins the post-consolidation contract so future changes don't silently undo
the mechanical work. The modules migrated off ``logging.getLogger(__name__)`` use
  ``lib_common.logging.get_logger``. The wrapper returns a
  ``_StructuredLoggerAdapter`` that supports keyword-style structured logs
  (``logger.info("msg", user_id=123)``); reverting any of the migrated
  modules to a plain stdlib logger would silently drop that capability.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (
    REPO_ROOT / "libs" / "python" / "lib_common",
    REPO_ROOT / "libs" / "python" / "lib_strategy",
    REPO_ROOT / "libs" / "python" / "lib_indicators",
    REPO_ROOT / "libs" / "python" / "lib_data",
    REPO_ROOT / "libs" / "python" / "lib_application",
    REPO_ROOT / "libs" / "python" / "lib_infrastructure",
    REPO_ROOT / "apps" / "execution_engine",
    REPO_ROOT / "apps" / "scoring_engine",
):
    candidate_str = str(candidate)
    if candidate_str not in sys.path:
        sys.path.insert(0, candidate_str)


def test_deprecated_basestrategy_chain_is_gone() -> None:
    """Sprint C: the entire deprecated chain was removed (no DeprecationWarning cycle)."""
    import importlib

    for module_path in (
        "lib_strategy.base",
        "lib_strategy.full_strategy",
        "lib_strategy.indicator_strategy",
        "lib_strategy.registry",
    ):
        try:
            importlib.import_module(module_path)
        except ModuleNotFoundError:
            continue  # expected — module was deleted
        msg = f"{module_path} should be gone but is still importable"
        raise AssertionError(msg)


def test_deprecated_user_bindings_module_is_gone() -> None:
    """``UserBindingsService`` and friends were ripped out in Sprint C."""
    import importlib

    try:
        importlib.import_module("lib_application.services.user_bindings")
    except ModuleNotFoundError:
        return  # expected
    msg = "lib_application.services.user_bindings should be gone"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Logger canonicalization (migrated modules)
# ---------------------------------------------------------------------------


# (module dotted-path, attribute-name-of-logger). Pin every site we migrated
# in Sprint B so a future revert surfaces as a test failure, not as silent
# logger-API drift.
_MIGRATED_LOGGER_SITES: tuple[tuple[str, str], ...] = (
    # lib_common
    ("lib_common.retries", "logger"),
    ("lib_common.config_validation", "logger"),
    ("lib_common.shutdown", "logger"),
    # lib_strategy
    ("lib_strategy.signals.emitter", "logger"),
    # apps/scoring_engine
    ("scoring_engine.providers_db", "logger"),
    ("scoring_engine.pipeline", "logger"),
    ("scoring_engine.storage", "logger"),
    ("scoring_engine.services.meta_label_service", "logger"),
    ("scoring_engine.services.ensemble_service", "logger"),
    # apps/execution_engine
    ("execution_engine.options_single_builder", "logger"),
    ("execution_engine.futures_builder", "logger"),
    ("execution_engine.position_sizer", "logger"),
    ("execution_engine.order_builder", "logger"),
    ("execution_engine.brokers.paper", "logger"),
)


@pytest.mark.parametrize(("dotted_path", "attr"), _MIGRATED_LOGGER_SITES)
def test_migrated_modules_use_canonical_logger(dotted_path: str, attr: str) -> None:
    """Each migrated module's module-level ``logger`` is the structured wrapper.

    The canonical ``lib_common.logging.get_logger`` returns a
    ``_StructuredLoggerAdapter``. A plain ``logging.Logger`` would NOT support
    the keyword-style structured-logging API the rest of the codebase relies
    on (``logger.info("msg", user_id=123)``). So we type-check the wrapper.
    """
    module = importlib.import_module(dotted_path)
    logger_obj = getattr(module, attr)

    from lib_common.logging import _StructuredLoggerAdapter, get_logger

    # The canonical wrapper uses a private adapter class; pin against that
    # exact type so a revert to ``logging.getLogger`` shows up as a failure.
    assert isinstance(logger_obj, _StructuredLoggerAdapter), (
        f"{dotted_path}.{attr} is not the canonical structured logger. "
        f"It must come from `lib_common.logging.get_logger`, not "
        f"`logging.getLogger`."
    )

    # Sanity: round-tripping through get_logger returns the same kind.
    assert isinstance(get_logger(dotted_path), _StructuredLoggerAdapter)
