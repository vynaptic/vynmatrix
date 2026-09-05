"""Exact, evidence-backed paper authority for one strategy deployment scope."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from lib_common.hashing import canonical_json_hash, sha256_file
from lib_common.paper_promotion_evidence import (
    PAPER_PROMOTION_EVIDENCE_NAMES,
    PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION,
    parse_promotion_timestamp,
    validate_evidence_document,
    validate_evidence_documents_for_build,
)
from lib_common.paper_promotion_instruments import (
    load_instrument_set_document as _load_instrument_set_document,
)
from lib_common.paper_promotion_instruments import (
    paper_promotion_instrument_set_sha256,
)
from lib_common.paper_promotion_instruments import (
    resolve_promotion_artifact as _resolved_artifact,
)
from lib_common.paper_promotion_instruments import (
    validate_instrument_authority as _validate_instrument_authority,
)

PAPER_PROMOTION_SCHEMA_VERSION = "3"
PAPER_PROMOTION_IMAGE = "vynmatrix/platform"
PAPER_PROMOTION_MODEL_SCOPES = frozenset({"single_instrument", "synchronized_portfolio"})

_STRATEGY_SCOPE_KEYS = frozenset(
    {
        "strategy_id",
        "strategy_version",
        "strategy_universe",
        "universe_contract",
        "model_scope",
        "canonical_instrument",
        "asset_class",
        "market_data_source",
        "market_data_timeframe",
        "consolidation_minutes",
        "broker_code",
        "data_use_scope",
        "model_configuration_sha256",
        "instrument_set_sha256",
        "scoring_semantics",
        "order_evidence_profile",
    }
)
_AUTHORITY_SCOPE_KEYS = frozenset(
    {
        "broker_environment",
        "capital_mode",
        "live_authority",
        "dedicated_account",
        "user_id",
        "broker_account_id",
        "strategy_binding_id",
        "image_repository",
        "image_tag",
        "config_sha256",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "status",
        *_STRATEGY_SCOPE_KEYS,
        *_AUTHORITY_SCOPE_KEYS,
        "instrument_set_artifact",
        "authorized_instruments",
        "evidence_run_id",
        "evidence",
        "created_at",
        "operator",
    }
)
_EVIDENCE_KEYS = frozenset({"path", "sha256"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BROKER_ALIASES = MappingProxyType({"local-paper": "paper"})
_SCORING_SEMANTICS = frozenset({"calibrated_forecast", "rank_model"})
_ORDER_EVIDENCE_PROFILES = frozenset({"bracket_oco", "synchronized_targets"})


@dataclass(frozen=True, slots=True)
class PaperPromotionScope:
    """Exact model and tenant route authorized by one validated manifest."""

    user_id: str
    broker_account_id: int
    strategy_binding_id: int
    strategy_id: str
    strategy_version: str
    strategy_universe: str
    model_scope: str
    canonical_instrument: str | None
    asset_class: str
    broker_code: str
    data_use_scope: str | None
    model_configuration_sha256: str | None
    instrument_set_sha256: str
    instruments: tuple[tuple[int, str], ...]
    scoring_semantics: str
    order_evidence_profile: str

    @property
    def is_synchronized_portfolio(self) -> bool:
        """Return whether authority applies only to one atomic model batch."""

        return self.model_scope == "synchronized_portfolio"


@dataclass(frozen=True, slots=True)
class PaperPromotionModelContext:
    """Exact immutable portfolio identity supplied to scoring."""

    asset_class: str
    data_use_scope: str
    model_configuration_sha256: str
    instrument_set_sha256: str


def canonical_paper_broker_code(value: str) -> str:
    """Return the persisted canonical paper broker code."""

    normalized = _require_nonempty(value, field="broker_code").lower()
    return _BROKER_ALIASES.get(normalized, normalized)


def _single_instrument_sha256(canonical: str) -> str:
    return canonical_json_hash(
        {
            "schema": "paper-promotion-single-instrument-v1",
            "canonical_instrument": canonical,
        }
    )


def _require_positive_int(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        msg = f"{field} must be a positive integer"
        raise ValueError(msg)
    return value


def _require_nonempty(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        msg = f"{field} must be a string"
        raise TypeError(msg)
    normalized = value.strip()
    if not normalized:
        msg = f"{field} must be non-empty"
        raise ValueError(msg)
    return normalized


def _digest(value: object, *, field: str) -> str:
    normalized = _require_nonempty(value, field=field)
    if _SHA256_RE.fullmatch(normalized) is None:
        msg = f"{field} must be a lowercase SHA-256 digest"
        raise ValueError(msg)
    return normalized


def _load_strategy_config(config_path: Path) -> dict[str, Any]:
    if config_path.name != "config.json":
        msg = "paper promotion config must be a strategy config.json"
        raise ValueError(msg)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"strategy config is unreadable: {config_path}"
        raise ValueError(msg) from exc
    if not isinstance(config, dict):
        msg = "strategy config must be a JSON object"
        raise TypeError(msg)
    return config


def _config_values(config: Mapping[str, Any]) -> dict[str, Any]:
    parameters = config.get("parameters")
    market_data = config.get("market_data")
    deployment = config.get("deployment")
    execution = config.get("execution")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    market_data = market_data if isinstance(market_data, Mapping) else {}
    deployment = deployment if isinstance(deployment, Mapping) else {}
    execution = execution if isinstance(execution, Mapping) else {}
    paper_candidate = deployment.get("paper_candidate")
    paper_candidate = paper_candidate if isinstance(paper_candidate, Mapping) else {}
    broker_value = paper_candidate.get("broker_code")
    return {
        "strategy_id": config.get("strategy_id"),
        "strategy_version": config.get("strategy_version"),
        "strategy_universe": parameters.get("universe"),
        "universe_contract": parameters.get("universe_contract"),
        "canonical_instrument": paper_candidate.get("canonical_instrument"),
        "asset_class": parameters.get("asset_class"),
        "market_data_source": market_data.get("source"),
        "market_data_timeframe": market_data.get("timeframe"),
        "consolidation_minutes": market_data.get("consolidation_minutes"),
        "configured_broker_code": (
            canonical_paper_broker_code(str(broker_value)) if broker_value else None
        ),
        "require_explicit_scoring_inputs": execution.get("require_explicit_scoring_inputs"),
    }


def _strategy_contract(  # noqa: PLR0915 - explicit packaged-scope contract ledger
    config: Mapping[str, Any],
    *,
    model_scope: str | None,
    broker_code: str | None,
    model_configuration_sha256: str | None,
    instruments: Mapping[int, str],
) -> dict[str, Any]:
    values = _config_values(config)
    inferred_scope = (
        "synchronized_portfolio" if values.get("universe_contract") else "single_instrument"
    )
    resolved_scope = model_scope or inferred_scope
    if resolved_scope not in PAPER_PROMOTION_MODEL_SCOPES:
        msg = f"model_scope must be one of {sorted(PAPER_PROMOTION_MODEL_SCOPES)}"
        raise ValueError(msg)
    if model_scope is not None and model_scope != inferred_scope:
        msg = "model_scope differs from the packaged strategy config"
        raise ValueError(msg)
    for field in (
        "strategy_id",
        "strategy_version",
        "strategy_universe",
        "asset_class",
        "market_data_source",
        "market_data_timeframe",
    ):
        values[field] = _require_nonempty(values.get(field), field=field)
    consolidation = values.get("consolidation_minutes")
    if isinstance(consolidation, bool) or not isinstance(consolidation, int) or consolidation < 0:
        msg = "consolidation_minutes must be a non-negative integer"
        raise ValueError(msg)
    values["asset_class"] = str(values["asset_class"]).lower()
    configured_broker = values.pop("configured_broker_code")
    resolved_broker = broker_code or configured_broker
    if resolved_broker is None:
        msg = "synchronized portfolio authority requires broker_code"
        raise ValueError(msg)
    normalized_broker = canonical_paper_broker_code(resolved_broker)
    if configured_broker is not None and configured_broker != normalized_broker:
        msg = "broker_code differs from the packaged strategy config"
        raise ValueError(msg)

    canonical = values.get("canonical_instrument")
    universe_contract = values.get("universe_contract")
    explicit_scoring = values.pop("require_explicit_scoring_inputs")
    if explicit_scoring is not None and not isinstance(explicit_scoring, bool):
        msg = "require_explicit_scoring_inputs must be boolean when configured"
        raise ValueError(msg)
    if resolved_scope == "single_instrument":
        canonical = _require_nonempty(canonical, field="canonical_instrument")
        if instruments:
            msg = "single-instrument authority does not accept a portfolio artifact"
            raise ValueError(msg)
        if model_configuration_sha256 is not None:
            msg = "single-instrument authority cannot claim a model configuration digest"
            raise ValueError(msg)
        model_configuration = None
        data_use_scope = None
        instrument_set_sha256 = _single_instrument_sha256(canonical)
        scoring_semantics = "rank_model" if explicit_scoring is False else "calibrated_forecast"
        order_evidence_profile = "bracket_oco"
    else:
        if canonical is not None:
            msg = "synchronized portfolio config cannot declare one canonical instrument"
            raise ValueError(msg)
        universe_contract = _require_nonempty(universe_contract, field="universe_contract")
        if not instruments:
            msg = "synchronized portfolio authority requires an instrument allowlist"
            raise ValueError(msg)
        if model_configuration_sha256 is None:
            msg = "synchronized portfolio authority requires model_configuration_sha256"
            raise ValueError(msg)
        if explicit_scoring is None:
            msg = "synchronized portfolio config must declare require_explicit_scoring_inputs"
            raise ValueError(msg)
        model_configuration = _digest(
            model_configuration_sha256,
            field="model_configuration_sha256",
        )
        data_use_scope = "paper_forward"
        instrument_set_sha256 = paper_promotion_instrument_set_sha256(instruments)
        scoring_semantics = "calibrated_forecast" if explicit_scoring else "rank_model"
        order_evidence_profile = "synchronized_targets"

    return {
        "strategy_id": values["strategy_id"],
        "strategy_version": values["strategy_version"],
        "strategy_universe": values["strategy_universe"],
        "universe_contract": universe_contract,
        "model_scope": resolved_scope,
        "canonical_instrument": canonical,
        "asset_class": values["asset_class"],
        "market_data_source": values["market_data_source"],
        "market_data_timeframe": values["market_data_timeframe"],
        "consolidation_minutes": consolidation,
        "broker_code": normalized_broker,
        "data_use_scope": data_use_scope,
        "model_configuration_sha256": model_configuration,
        "instrument_set_sha256": instrument_set_sha256,
        "scoring_semantics": scoring_semantics,
        "order_evidence_profile": order_evidence_profile,
    }


def _evidence_scope(
    contract: Mapping[str, Any],
    *,
    user_id: Any,
    broker_account_id: Any,
    strategy_binding_id: Any,
    image_tag: Any,
    config_sha256: Any,
) -> dict[str, Any]:
    return {
        **contract,
        "broker_environment": "paper",
        "capital_mode": "paper",
        "live_authority": False,
        "dedicated_account": True,
        "user_id": user_id,
        "broker_account_id": broker_account_id,
        "strategy_binding_id": strategy_binding_id,
        "image_repository": PAPER_PROMOTION_IMAGE,
        "image_tag": image_tag,
        "config_sha256": config_sha256,
    }


def _load_evidence_document(path: Path, *, name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"evidence.{name} must be a readable JSON object"
        raise ValueError(msg) from exc
    if not isinstance(payload, Mapping):
        msg = f"evidence.{name} must be a JSON object"
        raise TypeError(msg)
    return payload


def build_paper_promotion_manifest(
    *,
    config_path: Path,
    artifact_root: Path,
    evidence_paths: Mapping[str, Path],
    user_id: str,
    broker_account_id: int,
    strategy_binding_id: int,
    image_tag: str,
    operator: str,
    model_scope: str | None = None,
    broker_code: str | None = None,
    model_configuration_sha256: str | None = None,
    instrument_set_artifact: Path | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build one manifest from an exact config, model scope and evidence set."""

    config = _load_strategy_config(config_path)
    values = _config_values(config)
    inferred_scope = (
        "synchronized_portfolio" if values.get("universe_contract") else "single_instrument"
    )
    instruments: dict[int, str] = {}
    instrument_reference: dict[str, str] | None = None
    instrument_path: Path | None = None
    if inferred_scope == "synchronized_portfolio":
        if instrument_set_artifact is None:
            msg = "synchronized portfolio authority requires instrument_set_artifact"
            raise ValueError(msg)
        configuration_digest = _digest(
            model_configuration_sha256,
            field="model_configuration_sha256",
        )
        instrument_path, relative_path = _resolved_artifact(
            artifact_root,
            instrument_set_artifact,
            field="instrument_set_artifact",
        )
        instruments = _load_instrument_set_document(
            instrument_path,
            strategy_id=_require_nonempty(values.get("strategy_id"), field="strategy_id"),
            strategy_version=_require_nonempty(
                values.get("strategy_version"), field="strategy_version"
            ),
            model_configuration_sha256=configuration_digest,
        )
        instrument_reference = {
            "path": relative_path,
            "sha256": sha256_file(instrument_path),
        }
    elif instrument_set_artifact is not None:
        msg = "single-instrument authority cannot claim an instrument-set artifact"
        raise ValueError(msg)
    contract = _strategy_contract(
        config,
        model_scope=model_scope,
        broker_code=broker_code,
        model_configuration_sha256=model_configuration_sha256,
        instruments=instruments,
    )
    if set(evidence_paths) != PAPER_PROMOTION_EVIDENCE_NAMES:
        missing = sorted(PAPER_PROMOTION_EVIDENCE_NAMES - set(evidence_paths))
        unexpected = sorted(set(evidence_paths) - PAPER_PROMOTION_EVIDENCE_NAMES)
        msg = (
            f"paper promotion evidence set is not exact; missing={missing}, unexpected={unexpected}"
        )
        raise ValueError(msg)
    timestamp = created_at or datetime.now(tz=UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        msg = "created_at must be timezone-aware"
        raise ValueError(msg)
    normalized_user_id = _require_nonempty(user_id, field="user_id")
    normalized_account_id = _require_positive_int(broker_account_id, field="broker_account_id")
    normalized_binding_id = _require_positive_int(strategy_binding_id, field="strategy_binding_id")
    normalized_image_tag = _require_nonempty(image_tag, field="image_tag")
    config_digest = sha256_file(config_path)
    expected_scope = _evidence_scope(
        contract,
        user_id=normalized_user_id,
        broker_account_id=normalized_account_id,
        strategy_binding_id=normalized_binding_id,
        image_tag=normalized_image_tag,
        config_sha256=config_digest,
    )
    evidence: dict[str, dict[str, str]] = {}
    documents: dict[str, Mapping[str, Any]] = {}
    resolved_paths: set[Path] = set()
    if instrument_path is not None:
        resolved_paths.add(instrument_path)
    for name in sorted(PAPER_PROMOTION_EVIDENCE_NAMES):
        evidence_path, relative_path = _resolved_artifact(
            artifact_root,
            evidence_paths[name],
            field=f"evidence.{name}",
        )
        if evidence_path in resolved_paths:
            msg = "every promotion evidence type must reference a distinct artifact"
            raise ValueError(msg)
        resolved_paths.add(evidence_path)
        documents[name] = _load_evidence_document(evidence_path, name=name)
        evidence[name] = {"path": relative_path, "sha256": sha256_file(evidence_path)}
    evidence_run_id = validate_evidence_documents_for_build(
        documents,
        expected_scope=expected_scope,
    )
    return {
        "schema_version": PAPER_PROMOTION_SCHEMA_VERSION,
        "status": "passed",
        **contract,
        "broker_environment": "paper",
        "capital_mode": "paper",
        "live_authority": False,
        "dedicated_account": True,
        "user_id": normalized_user_id,
        "broker_account_id": normalized_account_id,
        "strategy_binding_id": normalized_binding_id,
        "image_repository": PAPER_PROMOTION_IMAGE,
        "image_tag": normalized_image_tag,
        "config_sha256": config_digest,
        "instrument_set_artifact": instrument_reference,
        "authorized_instruments": [
            {"instrument_id": instrument_id, "canonical_symbol": symbol}
            for instrument_id, symbol in sorted(instruments.items())
        ],
        "evidence_run_id": evidence_run_id,
        "evidence": evidence,
        "created_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "operator": _require_nonempty(operator, field="operator"),
    }


def _validate_evidence(
    evidence: Any,
    *,
    manifest_path: Path,
    expected_scope: Mapping[str, Any],
    expected_run_id: Any,
) -> list[str]:
    if not isinstance(evidence, Mapping):
        return ["evidence must be an object"]
    if set(evidence) != PAPER_PROMOTION_EVIDENCE_NAMES:
        return ["evidence names do not match the required promotion set"]
    errors: list[str] = []
    manifest_root = manifest_path.resolve().parent
    resolved_paths: set[Path] = set()
    for name in sorted(PAPER_PROMOTION_EVIDENCE_NAMES):
        item = evidence.get(name)
        if not isinstance(item, Mapping) or set(item) != _EVIDENCE_KEYS:
            errors.append(f"evidence.{name} must contain only path and sha256")
            continue
        path_value = item.get("path")
        digest = item.get("sha256")
        if not isinstance(path_value, str) or not path_value.strip():
            errors.append(f"evidence.{name}.path must be non-empty")
            continue
        relative_path = PurePosixPath(path_value)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"evidence.{name}.path must remain below the manifest directory")
            continue
        try:
            evidence_path = (manifest_root / Path(*relative_path.parts)).resolve(strict=True)
            evidence_path.relative_to(manifest_root)
        except (FileNotFoundError, OSError, ValueError):
            errors.append(f"evidence.{name}.path is missing or escapes the artifact root")
            continue
        if not evidence_path.is_file():
            errors.append(f"evidence.{name}.path is not a regular file")
            continue
        if evidence_path in resolved_paths:
            errors.append(f"evidence.{name}.path duplicates another evidence artifact")
            continue
        resolved_paths.add(evidence_path)
        try:
            observed_digest = sha256_file(evidence_path)
        except OSError:
            errors.append(f"evidence.{name}.path cannot be hashed")
            continue
        if not isinstance(digest, str) or digest != observed_digest:
            errors.append(f"evidence.{name}.sha256 does not match the artifact")
        try:
            document = _load_evidence_document(evidence_path, name=name)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        errors.extend(validate_evidence_document(name, document, expected_scope=expected_scope))
        if document.get("run_id") != expected_run_id:
            errors.append(f"evidence.{name}.run_id does not match evidence_run_id")
    return errors


def _validate_contract(  # noqa: PLR0912, PLR0915 - accumulate every fail-closed mismatch
    payload: Mapping[str, Any], config_path: Path | None
) -> list[str]:
    errors: list[str] = []
    model_scope = payload.get("model_scope")
    if model_scope not in PAPER_PROMOTION_MODEL_SCOPES:
        errors.append("model_scope is unsupported")
    for field in (
        "strategy_id",
        "strategy_version",
        "strategy_universe",
        "asset_class",
        "market_data_source",
        "market_data_timeframe",
        "broker_code",
    ):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            errors.append(f"{field} must be non-empty")
    consolidation = payload.get("consolidation_minutes")
    if isinstance(consolidation, bool) or not isinstance(consolidation, int) or consolidation < 0:
        errors.append("consolidation_minutes must be a non-negative integer")
    broker_code = payload.get("broker_code")
    if isinstance(broker_code, str) and broker_code.strip():
        try:
            if canonical_paper_broker_code(broker_code) != broker_code:
                errors.append("broker_code must use its canonical persisted value")
        except (TypeError, ValueError):
            pass
    if (
        not isinstance(payload.get("instrument_set_sha256"), str)
        or _SHA256_RE.fullmatch(str(payload.get("instrument_set_sha256"))) is None
    ):
        errors.append("instrument_set_sha256 must be a lowercase SHA-256 digest")
    if payload.get("scoring_semantics") not in _SCORING_SEMANTICS:
        errors.append("scoring_semantics is unsupported")
    if payload.get("order_evidence_profile") not in _ORDER_EVIDENCE_PROFILES:
        errors.append("order_evidence_profile is unsupported")
    if model_scope == "single_instrument":
        if (
            not isinstance(payload.get("canonical_instrument"), str)
            or not str(payload["canonical_instrument"]).strip()
        ):
            errors.append("single_instrument scope requires canonical_instrument")
        if payload.get("universe_contract") is not None:
            errors.append("single_instrument scope cannot claim universe_contract")
        if payload.get("data_use_scope") is not None:
            errors.append("single_instrument scope cannot claim data_use_scope")
        if payload.get("model_configuration_sha256") is not None:
            errors.append("single_instrument scope cannot claim model_configuration_sha256")
        if payload.get("order_evidence_profile") != "bracket_oco":
            errors.append("single_instrument scope requires bracket_oco evidence")
        canonical = payload.get("canonical_instrument")
        if (
            isinstance(canonical, str)
            and canonical.strip()
            and payload.get("instrument_set_sha256") != _single_instrument_sha256(canonical)
        ):
            errors.append("instrument_set_sha256 does not match canonical_instrument")
    elif model_scope == "synchronized_portfolio":
        if payload.get("canonical_instrument") is not None:
            errors.append("synchronized_portfolio scope cannot claim canonical_instrument")
        if (
            not isinstance(payload.get("universe_contract"), str)
            or not str(payload["universe_contract"]).strip()
        ):
            errors.append("synchronized_portfolio scope requires universe_contract")
        if payload.get("data_use_scope") != "paper_forward":
            errors.append("synchronized_portfolio data_use_scope must be paper_forward")
        if (
            not isinstance(payload.get("model_configuration_sha256"), str)
            or _SHA256_RE.fullmatch(str(payload.get("model_configuration_sha256"))) is None
        ):
            errors.append("synchronized_portfolio scope requires model_configuration_sha256")
        if payload.get("order_evidence_profile") != "synchronized_targets":
            errors.append("synchronized_portfolio scope requires synchronized_targets evidence")

    if config_path is not None:
        try:
            config = _load_strategy_config(config_path)
            values = _config_values(config)
        except (TypeError, ValueError) as exc:
            errors.append(str(exc))
        else:
            expected = {
                "strategy_id": values["strategy_id"],
                "strategy_version": values["strategy_version"],
                "strategy_universe": values["strategy_universe"],
                "universe_contract": values["universe_contract"],
                "canonical_instrument": values["canonical_instrument"],
                "asset_class": (
                    str(values["asset_class"]).lower()
                    if isinstance(values["asset_class"], str)
                    else values["asset_class"]
                ),
                "market_data_source": values["market_data_source"],
                "market_data_timeframe": values["market_data_timeframe"],
                "consolidation_minutes": values["consolidation_minutes"],
            }
            mismatches = sorted(
                field for field, value in expected.items() if payload.get(field) != value
            )
            configured_broker = values["configured_broker_code"]
            if configured_broker is not None and configured_broker != payload.get("broker_code"):
                mismatches.append("broker_code")
            inferred_scope = (
                "synchronized_portfolio" if values.get("universe_contract") else "single_instrument"
            )
            if payload.get("model_scope") != inferred_scope:
                mismatches.append("model_scope")
            explicit_scoring = values.get("require_explicit_scoring_inputs")
            if explicit_scoring is not None and not isinstance(explicit_scoring, bool):
                errors.append("require_explicit_scoring_inputs must be boolean when configured")
            elif inferred_scope == "synchronized_portfolio" and explicit_scoring is None:
                errors.append(
                    "synchronized portfolio config must declare require_explicit_scoring_inputs"
                )
            else:
                expected_semantics = (
                    "rank_model" if explicit_scoring is False else "calibrated_forecast"
                )
                if payload.get("scoring_semantics") != expected_semantics:
                    mismatches.append("scoring_semantics")
            if mismatches:
                errors.append(f"manifest scope differs from packaged strategy config: {mismatches}")
    return errors


def validate_paper_promotion_manifest(
    payload: Any,
    *,
    manifest_path: Path,
    deploy_image_tag: str,
    config_path: Path | None = None,
) -> list[str]:
    """Return every reason a manifest cannot authorize its exact paper scope."""

    if not isinstance(payload, Mapping):
        return ["manifest must be a JSON object"]
    errors: list[str] = []
    if set(payload) != _MANIFEST_KEYS:
        errors.append(
            f"manifest fields do not match schema version {PAPER_PROMOTION_SCHEMA_VERSION}"
        )
    expected = {
        "schema_version": PAPER_PROMOTION_SCHEMA_VERSION,
        "status": "passed",
        "broker_environment": "paper",
        "capital_mode": "paper",
        "live_authority": False,
        "dedicated_account": True,
        "image_repository": PAPER_PROMOTION_IMAGE,
        "image_tag": deploy_image_tag,
    }
    mismatches = sorted(field for field, value in expected.items() if payload.get(field) != value)
    if mismatches:
        errors.append(f"manifest values mismatch exact paper authority: {mismatches}")
    errors.extend(_validate_contract(payload, config_path))
    _, instrument_errors = _validate_instrument_authority(
        payload,
        manifest_path=manifest_path,
    )
    errors.extend(instrument_errors)
    if not isinstance(payload.get("user_id"), str) or not payload["user_id"].strip():
        errors.append("user_id must be non-empty")
    for field in ("broker_account_id", "strategy_binding_id"):
        value = payload.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            errors.append(f"{field} must be a positive integer")
    if not isinstance(payload.get("operator"), str) or not payload["operator"].strip():
        errors.append("operator must be non-empty")
    run_id = payload.get("evidence_run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append("evidence_run_id must be non-empty")
    _, created_error = parse_promotion_timestamp(payload.get("created_at"), field="created_at")
    if created_error is not None:
        errors.append(created_error)
    config_digest = payload.get("config_sha256")
    if not isinstance(config_digest, str) or _SHA256_RE.fullmatch(config_digest) is None:
        errors.append("config_sha256 must be a lowercase SHA-256 digest")
    if config_path is not None:
        try:
            expected_digest = sha256_file(config_path)
        except OSError:
            errors.append("strategy config cannot be hashed")
        else:
            if config_digest != expected_digest:
                errors.append("config_sha256 does not match the packaged strategy config")

    contract = {field: payload.get(field) for field in _STRATEGY_SCOPE_KEYS}
    expected_scope = _evidence_scope(
        contract,
        user_id=payload.get("user_id"),
        broker_account_id=payload.get("broker_account_id"),
        strategy_binding_id=payload.get("strategy_binding_id"),
        image_tag=payload.get("image_tag"),
        config_sha256=payload.get("config_sha256"),
    )
    errors.extend(
        _validate_evidence(
            payload.get("evidence"),
            manifest_path=manifest_path,
            expected_scope=expected_scope,
            expected_run_id=run_id,
        )
    )
    return errors


def load_paper_promotion_scope(
    *,
    manifest_path: Path,
    deploy_image_tag: str,
    config_path: Path | None = None,
) -> tuple[PaperPromotionScope | None, tuple[str, ...]]:
    """Read and validate a manifest without mutating it or its evidence."""

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ("paper promotion manifest is unreadable",)
    errors = validate_paper_promotion_manifest(
        payload,
        manifest_path=manifest_path,
        deploy_image_tag=deploy_image_tag,
        config_path=config_path,
    )
    if errors:
        return None, tuple(errors)
    if not isinstance(payload, Mapping):
        return None, ("manifest must be a JSON object",)
    instruments, instrument_errors = _validate_instrument_authority(
        payload,
        manifest_path=manifest_path,
    )
    if instrument_errors:
        return None, tuple(instrument_errors)
    return (
        PaperPromotionScope(
            user_id=str(payload["user_id"]),
            broker_account_id=int(payload["broker_account_id"]),
            strategy_binding_id=int(payload["strategy_binding_id"]),
            strategy_id=str(payload["strategy_id"]),
            strategy_version=str(payload["strategy_version"]),
            strategy_universe=str(payload["strategy_universe"]),
            model_scope=str(payload["model_scope"]),
            canonical_instrument=(
                str(payload["canonical_instrument"])
                if payload["canonical_instrument"] is not None
                else None
            ),
            asset_class=str(payload["asset_class"]),
            broker_code=str(payload["broker_code"]),
            data_use_scope=(
                str(payload["data_use_scope"]) if payload["data_use_scope"] is not None else None
            ),
            model_configuration_sha256=(
                str(payload["model_configuration_sha256"])
                if payload["model_configuration_sha256"] is not None
                else None
            ),
            instrument_set_sha256=str(payload["instrument_set_sha256"]),
            instruments=tuple(sorted(instruments.items())),
            scoring_semantics=str(payload["scoring_semantics"]),
            order_evidence_profile=str(payload["order_evidence_profile"]),
        ),
        (),
    )


__all__ = [
    "PAPER_PROMOTION_EVIDENCE_NAMES",
    "PAPER_PROMOTION_EVIDENCE_SCHEMA_VERSION",
    "PAPER_PROMOTION_IMAGE",
    "PAPER_PROMOTION_MODEL_SCOPES",
    "PAPER_PROMOTION_SCHEMA_VERSION",
    "PaperPromotionModelContext",
    "PaperPromotionScope",
    "build_paper_promotion_manifest",
    "canonical_paper_broker_code",
    "load_paper_promotion_scope",
    "paper_promotion_instrument_set_sha256",
    "validate_paper_promotion_manifest",
]
