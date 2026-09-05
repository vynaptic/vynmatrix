"""Load canonical packaged strategy evidence for the narrow canary operation."""

from __future__ import annotations

import json
from pathlib import Path

from dev_cli.core.catalogue import load_strategy_releases
from lib_application.services.canary_activation import CanarySource
from lib_common.runner_utils import build_strategy_core_parameters


def load_canary(root: Path, *, strategy_id: str, version: str) -> CanarySource:
    releases = load_strategy_releases(root, strategy_id=strategy_id)
    if len(releases) != 1 or releases[0].semver != version:
        msg = "Canary requires the exact unambiguous packaged strategy version"
        raise ValueError(msg)
    release = releases[0]
    path = root / "strategies/indicator" / release.strategy_name / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if (
        config.get("strategy_id") != release.strategy_id
        or config.get("strategy_version") != release.semver
        or build_strategy_core_parameters(config) != release.default_params
    ):
        msg = "Canary source changed during validation; retry from stable packaged source"
        raise ValueError(msg)
    source = CanarySource(
        release=release,
        decision=config.get("metadata", {}).get("decision"),
        enabled=config.get("enabled") is True,
        environments=tuple(config.get("environments", [])),
    )
    source.validate()
    return source
