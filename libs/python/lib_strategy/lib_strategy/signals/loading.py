"""Load file-backed production strategy cores through one strict contract."""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from lib_strategy.signals.pure_strategy import PureSignalStrategy


def load_pure_strategy_core(
    strategy_path: Path,
    *,
    expected_class_name: str | None = None,
) -> type[PureSignalStrategy]:
    """Load the single production ``PureSignalStrategy`` from ``core.py``.

    The indicator worker and historical validator deliberately share this
    loader so research cannot select a different class or a translated rule.
    """

    resolved_strategy_path = strategy_path.resolve()
    core_path = resolved_strategy_path / "core.py"
    if not core_path.is_file():
        message = f"No core.py found for signal_worker strategy at {core_path}"
        raise FileNotFoundError(message)

    module_name = f"vynmatrix_strategy_{resolved_strategy_path.name}_{id(core_path)}"
    spec = importlib.util.spec_from_file_location(module_name, core_path)
    if spec is None or spec.loader is None:
        message = f"Could not load strategy core from {core_path}"
        raise RuntimeError(message)

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidates = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, PureSignalStrategy)
        and obj is not PureSignalStrategy
        and obj.__module__ == module.__name__
    ]
    if expected_class_name is not None:
        candidates = [
            candidate for candidate in candidates if candidate.__name__ == expected_class_name
        ]
        if not candidates:
            message = (
                f"Expected PureSignalStrategy class {expected_class_name!r} "
                f"was not found in {core_path}"
            )
            raise RuntimeError(message)
    if not candidates:
        message = f"No PureSignalStrategy subclass found in {core_path}"
        raise RuntimeError(message)
    if len(candidates) > 1:
        core_candidates = [
            candidate for candidate in candidates if candidate.__name__.endswith("Core")
        ]
        if len(core_candidates) == 1:
            return core_candidates[0]
        message = f"Multiple PureSignalStrategy subclasses found in {core_path}"
        raise RuntimeError(message)
    return candidates[0]


__all__ = ["load_pure_strategy_core"]
