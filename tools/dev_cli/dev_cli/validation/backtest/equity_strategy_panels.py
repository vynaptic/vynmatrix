"""Content-addressed synchronized-panel inputs for historical validation."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Final

from dev_cli.validation.backtest.equity_portfolio_types import EquityDataset
from dev_cli.validation.backtest.equity_snapshot import StrategyIdentity
from dev_cli.validation.evidence import (
    ContentAddressedArtifact,
    canonical_json_bytes,
    content_manifest_artifacts,
    evidence_sha256,
    load_content_addressed_manifest,
    sha256_digest,
    store_content_object,
    verified_content_path,
    write_content_addressed_manifest,
)
from lib_strategy.data_authority import DataUseScope
from lib_strategy.panels import (
    PanelReadyInput,
    SynchronizedPanelStrategy,
    evaluate_panel_readiness,
)

STRATEGY_PANEL_MANIFEST_SCHEMA: Final = "vynmatrix.equity-strategy-panels.v1"
STRATEGY_PANEL_ARTIFACT_ROLE: Final = "strategy_panel_inputs"
STRATEGY_PANEL_MEDIA_TYPE: Final = "application/x-ndjson"
STRATEGY_PANEL_DECISION_SESSION_OFFSET: Final = 0


class EquityStrategyPanelError(RuntimeError):
    """A historical strategy-panel artifact is incomplete or mismatched."""


@dataclass(frozen=True, slots=True)
class StrategyPanelManifestResult:
    """Exact immutable panel object and manifest identities written to disk."""

    manifest_path: Path
    manifest_sha256: str
    panel_artifact: ContentAddressedArtifact


def strategy_panel_artifact_context(
    *,
    historical_snapshot_manifest_sha256: str,
    strategy_identity: StrategyIdentity,
) -> dict[str, object]:
    """Return the exact provenance binding required on a panel-input object."""

    return {
        "decision_session_offset": STRATEGY_PANEL_DECISION_SESSION_OFFSET,
        "historical_snapshot_manifest_sha256": sha256_digest(
            historical_snapshot_manifest_sha256,
            field="historical_snapshot_manifest_sha256",
        ),
        "strategy_algorithm_type_name": strategy_identity.algorithm_type_name,
        "strategy_config_sha256": strategy_identity.config_sha256,
        "strategy_id": strategy_identity.strategy_id,
        "strategy_source_tree_sha256": strategy_identity.source_tree_sha256,
        "strategy_version": strategy_identity.strategy_version,
    }


def write_strategy_panel_manifest(
    root: Path,
    *,
    historical_snapshot_manifest_sha256: str,
    strategy_identity: StrategyIdentity,
    panel_payloads: Sequence[Mapping[str, Any]],
    lineage: Mapping[str, Any] | None = None,
) -> StrategyPanelManifestResult:
    """Write canonical NDJSON panel rows and their snapshot-bound manifest."""

    if not panel_payloads:
        message = "strategy-panel manifest requires at least one serialized panel input"
        raise EquityStrategyPanelError(message)
    snapshot_sha256 = sha256_digest(
        historical_snapshot_manifest_sha256,
        field="historical_snapshot_manifest_sha256",
    )
    try:
        content = b"".join(canonical_json_bytes(payload) + b"\n" for payload in panel_payloads)
        artifact = store_content_object(
            root,
            content,
            suffix=".ndjson",
            role=STRATEGY_PANEL_ARTIFACT_ROLE,
            media_type=STRATEGY_PANEL_MEDIA_TYPE,
            row_count=len(panel_payloads),
            context=strategy_panel_artifact_context(
                historical_snapshot_manifest_sha256=snapshot_sha256,
                strategy_identity=strategy_identity,
            ),
        )
        manifest_path, manifest_sha256 = write_content_addressed_manifest(
            root,
            {
                "schema": STRATEGY_PANEL_MANIFEST_SCHEMA,
                "historical_snapshot_manifest_sha256": snapshot_sha256,
                "strategy": asdict(strategy_identity),
                "lineage": dict(lineage or {}),
                "artifacts": [artifact.as_manifest()],
            },
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EquityStrategyPanelError(str(exc)) from exc
    return StrategyPanelManifestResult(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        panel_artifact=artifact,
    )


def _validated_panel_manifest(
    root: Path,
    manifest_path: Path,
    *,
    historical_snapshot_manifest_sha256: str,
    strategy_identity: StrategyIdentity,
) -> tuple[Path, int, str]:
    expected_snapshot_sha256 = sha256_digest(
        historical_snapshot_manifest_sha256,
        field="historical_snapshot_manifest_sha256",
    )
    try:
        manifest = load_content_addressed_manifest(
            root,
            manifest_path,
            schema=STRATEGY_PANEL_MANIFEST_SCHEMA,
        )
        observed_snapshot_sha256 = sha256_digest(
            manifest.get("historical_snapshot_manifest_sha256"),
            field="panel manifest historical_snapshot_manifest_sha256",
        )
        artifacts = content_manifest_artifacts(
            manifest,
            role=STRATEGY_PANEL_ARTIFACT_ROLE,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise EquityStrategyPanelError(str(exc)) from exc
    if observed_snapshot_sha256 != expected_snapshot_sha256:
        message = "strategy-panel manifest belongs to a different historical snapshot"
        raise EquityStrategyPanelError(message)
    if manifest.get("strategy") != asdict(strategy_identity):
        message = (
            "strategy-panel manifest source/config identity differs from the selected strategy"
        )
        raise EquityStrategyPanelError(message)
    if len(artifacts) != 1:
        message = (
            "strategy-panel manifest requires exactly one "
            f"{STRATEGY_PANEL_ARTIFACT_ROLE!r} artifact, found {len(artifacts)}"
        )
        raise EquityStrategyPanelError(message)
    artifact = artifacts[0]
    if artifact.media_type != STRATEGY_PANEL_MEDIA_TYPE or Path(artifact.path).suffix != ".ndjson":
        message = "strategy-panel inputs must be one application/x-ndjson content object"
        raise EquityStrategyPanelError(message)
    if artifact.row_count is None or artifact.row_count < 1:
        message = "strategy-panel input artifact must declare at least one row"
        raise EquityStrategyPanelError(message)
    expected_context = strategy_panel_artifact_context(
        historical_snapshot_manifest_sha256=expected_snapshot_sha256,
        strategy_identity=strategy_identity,
    )
    for field, expected in expected_context.items():
        if artifact.context.get(field) != expected:
            message = f"strategy-panel artifact context differs at {field!r}"
            raise EquityStrategyPanelError(message)
    try:
        path = verified_content_path(root, artifact)
    except (OSError, TypeError, ValueError) as exc:
        raise EquityStrategyPanelError(str(exc)) from exc
    return path, artifact.row_count, evidence_sha256(manifest)


def _artifact_lines(path: Path) -> Iterator[tuple[int, str]]:
    try:
        source = path.open("r", encoding="utf-8")
    except OSError as exc:
        message = f"cannot open strategy-panel input artifact {path}: {exc}"
        raise EquityStrategyPanelError(message) from exc
    with source:
        line_number = 0
        while True:
            try:
                line = source.readline()
            except OSError as exc:
                message = f"cannot read strategy-panel input artifact {path}: {exc}"
                raise EquityStrategyPanelError(message) from exc
            if line == "":
                return
            line_number += 1
            yield line_number, line


def _decode_panel_row(
    line: str,
    *,
    line_number: int,
    dataset: EquityDataset,
    strategy: SynchronizedPanelStrategy,
) -> tuple[date, object]:
    if not line.strip():
        message = f"strategy-panel artifact contains a blank row at line {line_number}"
        raise EquityStrategyPanelError(message)
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        message = f"strategy-panel row {line_number} is not valid JSON: {exc}"
        raise EquityStrategyPanelError(message) from exc
    if not isinstance(payload, Mapping):
        message = f"strategy-panel row {line_number} must be a JSON object"
        raise EquityStrategyPanelError(message)
    try:
        panel_input = strategy.deserialize_panel_input(payload)
        round_trip = strategy.serialize_panel_input(panel_input)
        canonical_input = canonical_json_bytes(payload)
        canonical_round_trip = canonical_json_bytes(round_trip)
        panel = strategy.panel_ready_input(panel_input)
        if isinstance(panel, PanelReadyInput):
            evaluate_panel_readiness(panel).require_complete()
    except (KeyError, RuntimeError, TypeError, ValueError) as exc:
        message = f"strategy-panel row {line_number} violates its strategy codec: {exc}"
        raise EquityStrategyPanelError(message) from exc
    if canonical_round_trip != canonical_input:
        message = f"strategy-panel row {line_number} is not codec-canonical"
        raise EquityStrategyPanelError(message)
    if not isinstance(panel, PanelReadyInput):
        message = f"strategy-panel row {line_number} has no PanelReadyInput"
        raise EquityStrategyPanelError(message)
    if panel.data_use_scope is not DataUseScope.HISTORICAL_VALIDATION:
        message = f"strategy-panel row {line_number} is not historical-validation data"
        raise EquityStrategyPanelError(message)
    decision_session = panel.session.session_date
    execution_session = panel.execution_session.session_date
    if dataset.official_sessions.get(decision_session) != panel.session:
        message = (
            f"strategy-panel decision session {decision_session} differs from "
            "the snapshot session authority"
        )
        raise EquityStrategyPanelError(message)
    if dataset.official_sessions.get(execution_session) != panel.execution_session:
        message = (
            f"strategy-panel execution session {execution_session} differs from "
            "the snapshot session authority"
        )
        raise EquityStrategyPanelError(message)
    decision_index = dataset.sessions.index(decision_session)
    if (
        decision_index + 1 >= len(dataset.sessions)
        or dataset.sessions[decision_index + 1] != execution_session
    ):
        message = (
            f"strategy-panel execution session {execution_session} is not the immediate "
            f"official session after decision {decision_session}"
        )
        raise EquityStrategyPanelError(message)
    return decision_session, panel_input


def compose_strategy_panel_dataset(
    dataset: EquityDataset,
    *,
    artifact_root: Path,
    manifest_path: Path,
    historical_snapshot_manifest_sha256: str,
    strategy_identity: StrategyIdentity,
    strategy: SynchronizedPanelStrategy,
) -> EquityDataset:
    """Deserialize one verified panel set and attach it to an equity dataset."""

    if not dataset.official_sessions or set(dataset.official_sessions) != set(dataset.sessions):
        message = "strategy-panel composition requires every official snapshot session cutoff"
        raise EquityStrategyPanelError(message)
    artifact_path, expected_rows, manifest_sha256 = _validated_panel_manifest(
        artifact_root,
        manifest_path,
        historical_snapshot_manifest_sha256=historical_snapshot_manifest_sha256,
        strategy_identity=strategy_identity,
    )
    panel_inputs: dict[date, object] = {}
    previous_session: date | None = None
    for line_number, line in _artifact_lines(artifact_path):
        decision_session, panel_input = _decode_panel_row(
            line,
            line_number=line_number,
            dataset=dataset,
            strategy=strategy,
        )
        if previous_session is not None and decision_session <= previous_session:
            message = "strategy-panel rows must use unique ascending decision sessions"
            raise EquityStrategyPanelError(message)
        panel_inputs[decision_session] = panel_input
        previous_session = decision_session
    if len(panel_inputs) != expected_rows:
        message = (
            "strategy-panel artifact row count differs: "
            f"declared {expected_rows}, loaded {len(panel_inputs)}"
        )
        raise EquityStrategyPanelError(message)
    return replace(
        dataset,
        panel_inputs=panel_inputs,
        panel_manifest_sha256=manifest_sha256,
    )


__all__ = (
    "STRATEGY_PANEL_ARTIFACT_ROLE",
    "STRATEGY_PANEL_DECISION_SESSION_OFFSET",
    "STRATEGY_PANEL_MANIFEST_SCHEMA",
    "STRATEGY_PANEL_MEDIA_TYPE",
    "EquityStrategyPanelError",
    "StrategyPanelManifestResult",
    "compose_strategy_panel_dataset",
    "strategy_panel_artifact_context",
    "write_strategy_panel_manifest",
)
