"""Load the repository's existing strategy and broker reference contracts."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft7Validator

from lib_application.db.models import ApiAuditLog
from lib_application.db.session import tenant_scope
from lib_application.services.catalogue import (
    InstrumentReference,
    StrategyRelease,
    check_catalogue,
    lock_catalogue,
    register_brokers,
    register_strategies,
    upsert_instruments,
)
from lib_application.services.catalogue_changes import (
    register_instrument_links,
    validate_environment_metadata,
    validate_instrument_links,
)
from lib_application.services.database_authority import (
    require_backend_database_role,
    require_maintenance_database_role,
)
from lib_application.services.deployment_owner import require_deployment_owner_id
from lib_common.logging import get_logger
from lib_common.runner_utils import (
    build_config_validator,
    build_strategy_core_parameters,
    iter_strategy_dirs,
    validate_strategy_config,
)
from lib_infrastructure.brokers.capabilities import BROKER_SPECS

logger = get_logger(__name__)


def load_strategy_releases(root: Path, *, strategy_id: str | None = None) -> list[StrategyRelease]:
    """Validate source configs even when disabled; registration is independent of selection."""
    schema_path = root / "config/schemas/indicator_strategy_config.schema.json"
    validator = build_config_validator(schema_path, "indicator strategy", logger)
    parameter_schema = deepcopy(validator.schema["properties"]["parameters"])
    parameter_schema["definitions"] = deepcopy(validator.schema["definitions"])
    # Source parameters cannot spoof strategy identity. The established builder
    # injects that immutable top-level identity into the resulting core parameters.
    parameter_schema.pop("not", None)
    parameter_schema["properties"]["strategy_version"] = deepcopy(
        validator.schema["properties"]["strategy_version"]
    )
    parameter_validator = Draft7Validator(parameter_schema)
    releases = []
    for directory in sorted(iter_strategy_dirs(root / "strategies/indicator")):
        if not (directory / "config.json").is_file():
            # The packaged indicator namespace is a sibling of source strategies.
            # A strategy core with a missing contract is still an error.
            if (directory / "core.py").is_file():
                msg = f"Missing strategy configuration: {directory.name}"
                raise ValueError(msg)
            continue
        config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
        if not validate_strategy_config(config, directory.name, validator, logger):
            msg = f"Invalid strategy configuration: {directory.name}"
            raise ValueError(msg)
        if not (directory / "core.py").is_file() or config["runner_kind"] != "signal_worker":
            msg = f"Strategy {directory.name} requires its canonical signal_worker core"
            raise ValueError(msg)
        parameters = build_strategy_core_parameters(config)
        parameter_validator.validate(parameters)
        release = StrategyRelease(
            strategy_id=config["strategy_id"],
            strategy_name=directory.name,
            asset_class=config["parameters"]["asset_class"],
            semver=config["strategy_version"],
            description=config.get("description"),
            param_schema=deepcopy(parameter_schema),
            default_params=parameters,
        )
        release.validate()
        releases.append(release)
    if strategy_id is not None:
        releases = [release for release in releases if release.strategy_id == strategy_id]
        if not releases:
            msg = f"Unknown strategy: {strategy_id}"
            raise ValueError(msg)
    return releases


def load_broker_references(root: Path, *, broker_code: str | None = None) -> list[dict[str, Any]]:
    """Attach authoritative adapter capabilities to explicit non-secret environment routes."""
    data = yaml.safe_load((root / "config/brokers.yaml").read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or set(data) != {"brokers"}
        or not isinstance(data["brokers"], list)
    ):
        msg = "Broker reference file requires only a brokers list"
        raise ValueError(msg)
    capabilities = {spec.code: spec.capabilities.catalogue_payload() for spec in BROKER_SPECS}
    references = []
    seen: set[str] = set()
    for item in data["brokers"]:
        if not isinstance(item, dict):
            msg = "Broker references must be objects"
            raise TypeError(msg)
        fields = {"code", "name", "environments"}
        if item.get("code") == "paper":
            fields.add("capabilities")
        if set(item) != fields:
            msg = "Broker references require code, name and environments only"
            raise ValueError(msg)
        code = item["code"]
        if code == "paper":
            # The execution engine's in-process PaperBroker has no remote
            # adapter spec. Retain its existing, explicit reference capabilities.
            capabilities[code] = item["capabilities"]
            if not isinstance(capabilities[code], dict):
                msg = "Local paper capabilities must be an object"
                raise TypeError(msg)
        if code not in capabilities or code in seen:
            msg = f"Unsupported or duplicate broker: {code}"
            raise ValueError(msg)
        seen.add(code)
        if not isinstance(item["name"], str) or not item["name"].strip():
            msg = f"Broker {code} requires a display name"
            raise ValueError(msg)
        _validate_environments(code, item["environments"])
        references.append({**item, "capabilities": capabilities[code]})
    if broker_code is not None:
        references = [item for item in references if item["code"] == broker_code]
        if not references:
            msg = f"Unknown broker: {broker_code}"
            raise ValueError(msg)
    return references


def _validate_environments(code: str, environments: Any) -> None:
    if not isinstance(environments, list) or not environments:
        msg = f"Broker {code} requires explicit environment routes"
        raise ValueError(msg)
    seen = set()
    for route in environments:
        if not isinstance(route, dict) or set(route) != {
            "environment",
            "region",
            "base_urls",
            "rate_limits",
        }:
            msg = f"Broker {code} has invalid environment fields"
            raise ValueError(msg)
        region = route["region"]
        if (
            route["environment"] not in {"paper", "live"}
            or not isinstance(region, str)
            or not region.strip()
        ):
            msg = f"Broker {code} requires an explicit paper/live environment and region"
            raise ValueError(msg)
        key = (route["environment"], region)
        if key in seen:
            msg = f"Duplicate broker environment route: {code}/{key}"
            raise ValueError(msg)
        seen.add(key)
        if not isinstance(route["base_urls"], dict) or not isinstance(route["rate_limits"], dict):
            msg = f"Broker {code} endpoints and limits must be objects"
            raise TypeError(msg)

        for field in ("base_urls", "rate_limits"):
            validate_environment_metadata(broker_code=code, field=field, value=route[field])
        json.dumps(route, allow_nan=False)


def load_catalogue(
    root: Path,
    *,
    strategy_id: str | None = None,
    broker_code: str | None = None,
) -> dict[str, Any]:
    """Select one source family explicitly, or the complete reviewed install catalogue."""

    if strategy_id is not None and broker_code is not None:
        msg = "Select either a strategy or a broker"
        raise ValueError(msg)
    instruments = []
    if strategy_id is None and broker_code is None:
        data = yaml.safe_load((root / "config/instruments.yaml").read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or set(data) != {"instruments"}
            or not isinstance(data["instruments"], list)
        ):
            msg = "Instrument reference file requires only an instruments list"
            raise ValueError(msg)
        instruments = data["instruments"]
        for item in instruments:
            InstrumentReference.parse(item)
    brokers = load_broker_references(root, broker_code=broker_code) if strategy_id is None else []
    releases = load_strategy_releases(root, strategy_id=strategy_id) if broker_code is None else []
    validate_instrument_links(instruments, broker_references=brokers)
    return {"instruments": instruments, "brokers": brokers, "releases": releases}


def reconcile_catalogue(
    session: Any,
    sources: dict[str, Any],
    *,
    apply: bool,
    maintenance: bool = False,
) -> dict[str, Any]:
    """One caller-owned transaction; dry-run never allocates IDs or appends audits."""

    if session.new or session.dirty or session.deleted:
        msg = "Catalogue reconciliation requires a clean caller session"
        raise ValueError(msg)
    if maintenance:
        require_maintenance_database_role(session)
        owner_id = None
    else:
        require_backend_database_role(session)
        owner_id = require_deployment_owner_id(session)
    if apply:
        lock_catalogue(session)
    with session.no_autoflush:
        counts = check_catalogue(session, **sources)
        links = register_instrument_links(
            session,
            sources["instruments"],
            broker_references=sources["brokers"],
            dry_run=True,
            maintenance=maintenance,
        )
    if apply:
        register_brokers(session, sources["brokers"])
        upsert_instruments(session, sources["instruments"])
        register_strategies(session, sources["releases"])
        links = register_instrument_links(
            session,
            sources["instruments"],
            broker_references=sources["brokers"],
            maintenance=maintenance,
        )
        if any(counts.values()):
            with tenant_scope(session, user_id=owner_id):
                session.add(
                    ApiAuditLog(
                        user_id=owner_id,
                        action="catalogue.register",
                        req={
                            "strategy_ids": [
                                release.strategy_id for release in sources["releases"]
                            ],
                            "broker_codes": [broker["code"] for broker in sources["brokers"]],
                        },
                        resp={"created": counts},
                        status="ok",
                    )
                )
                session.flush()
    return {
        "missing" if not apply else "created": counts,
        "links": [asdict(link) for link in links],
    }
