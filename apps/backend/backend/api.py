"""Tenant self-service config API (gap #3).

A FastAPI surface for a user (or an admin/web BFF on their behalf) to manage their
own trading config: strategy bindings, reviewed strategy safety switches,
append-only drawdown mandates, and broker-account onboarding (link an account +
store its API keys in the DB-encrypted secret store).

Multi-tenant by construction: every DB unit-of-work is wrapped in
``tenant_scope(session, user_id=...)``, so when the service connects through
``vm_backend_login`` Postgres RLS enforces that a request can only ever read/write
that user's rows — a hard backstop behind the application logic. Broker
API keys are written via the pluggable secrets backend (``DbSecretsProvider`` in
prod) and are never returned or logged.

Auth here is an admin API key (operator / web BFF); per-end-user JWT auth that
derives ``user_id`` from a verified token is a web-layer follow-up.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import text

from lib_application.db.models import (
    ApiAuditLog,
    Broker,
    BrokerCredential,
    BrokerEnvironment,
    Instrument,
    LinkedBrokerAccount,
    RiskMandate,
    Strategy,
    StrategyVersion,
    UserStrategyBinding,
    UserStrategyConfig,
)
from lib_application.db.session import tenant_scope
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)
from lib_application.services.market_calendars import (
    MarketSessionWindow,
    replace_market_calendar,
)
from lib_common.api_security import is_production_environment, secret_matches
from lib_common.app import create_service_app
from lib_common.asset_classes import normalize_asset_class
from lib_common.config_validation import BrokerType, ExecutionMode, normalize_broker_code
from lib_common.env_utils import parse_bool_env
from lib_common.logging import get_logger

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]
_WRITABLE_SECRETS_UNAVAILABLE = (
    "credential writes are unavailable with the configured secrets backend; "
    "set SECRETS_BACKEND=db and configure SECRETS_MASTER_KEYS"
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class BindingIn(BaseModel):
    strategy_id: str | None = Field(default=None, min_length=1, max_length=50)
    broker_account_id: int = Field(gt=0)
    asset_score_threshold: float = Field(default=0.60, ge=0, le=1)
    sector_score_threshold: float | None = Field(default=None, ge=0, le=1)
    market_score_threshold: float | None = Field(default=None, ge=0, le=1)
    execution_modes_allowed: list[str] = Field(
        default_factory=lambda: ["spot"],
        min_length=1,
    )
    preferred_mode: str | None = None
    mode_selection_policy: Literal[
        "fixed",
        "best_return",
        "lowest_risk",
        "highest_sharpe",
        "user_rotating",
    ] = "fixed"
    asset_classes_allowed: list[str] = Field(
        default_factory=lambda: ["crypto"],
        min_length=1,
    )
    instruments_allowed: list[str] | None = None
    max_position_pct: float = Field(default=0.10, gt=0, le=1)
    max_total_exposure_pct: float = Field(default=0.50, gt=0, le=1)
    max_daily_loss_pct: float = Field(default=0.05, ge=0, le=1)
    max_open_positions: int = Field(default=10, gt=0, le=1000)
    entry_cash_buffer_bps: float | None = Field(default=None, gt=0, le=1000)
    allowed_brokers: list[str] | None = None
    autopilot: bool = False
    is_active: bool = False
    entries_enabled: bool = False
    exits_enabled: bool = False

    @field_validator("strategy_id")
    @classmethod
    def normalize_strategy_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            msg = "strategy_id must be null or non-empty"
            raise ValueError(msg)
        return normalized

    @field_validator("asset_classes_allowed")
    @classmethod
    def normalize_asset_classes(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                normalize_asset_class(value, field_name="binding asset class") for value in values
            )
        )

    @field_validator("execution_modes_allowed", mode="before")
    @classmethod
    def normalize_execution_modes(cls, values: Any) -> list[str]:
        if not isinstance(values, list):
            msg = "execution_modes_allowed must be a non-empty list"
            raise ValueError(msg)  # noqa: TRY004
        normalized: list[str] = []
        for value in values:
            token = str(value or "").strip().lower()
            try:
                mode = ExecutionMode(token)
            except ValueError as exc:
                msg = f"Unsupported execution mode {value!r}"
                raise ValueError(msg) from exc
            if mode.value not in normalized:
                normalized.append(mode.value)
        return normalized

    @field_validator("preferred_mode")
    @classmethod
    def normalize_preferred_mode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return ExecutionMode(value.strip().lower()).value
        except ValueError as exc:
            msg = f"Unsupported preferred execution mode {value!r}"
            raise ValueError(msg) from exc

    @field_validator("allowed_brokers", mode="before")
    @classmethod
    def normalize_allowed_brokers(cls, values: Any) -> list[str] | None:
        if values is None:
            return None
        if not isinstance(values, list):
            msg = "allowed_brokers must be a list"
            raise ValueError(msg)  # noqa: TRY004
        if not values:
            msg = "allowed_brokers must be null or a non-empty list"
            raise ValueError(msg)
        normalized: list[str] = []
        for value in values:
            broker_code = normalize_broker_code(value, default="")
            try:
                broker = BrokerType(broker_code)
            except ValueError as exc:
                msg = f"Unsupported broker {value!r}"
                raise ValueError(msg) from exc
            if broker.value not in normalized:
                normalized.append(broker.value)
        if not normalized:
            msg = "allowed_brokers must be null or a non-empty list"
            raise ValueError(msg)
        return normalized

    @field_validator("instruments_allowed")
    @classmethod
    def normalize_instrument_tokens(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        normalized = list(
            dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip())
        )
        if not normalized:
            msg = "instruments_allowed must be null or a non-empty list"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_execution_authority(self) -> BindingIn:
        if self.is_active and self.strategy_id is None:
            msg = "active bindings require an explicit strategy_id"
            raise ValueError(msg)
        if not self.is_active and (self.entries_enabled or self.exits_enabled):
            msg = "inactive bindings cannot grant entry or exit authority"
            raise ValueError(msg)
        if self.entries_enabled and not self.autopilot:
            msg = "entry authority requires autopilot=true"
            raise ValueError(msg)
        if (
            self.preferred_mode is not None
            and self.preferred_mode not in self.execution_modes_allowed
        ):
            msg = "preferred_mode must be included in execution_modes_allowed"
            raise ValueError(msg)
        return self


class BindingOut(BaseModel):
    binding_id: int
    user_id: str
    strategy_id: str | None
    broker_account_id: int
    asset_score_threshold: float
    sector_score_threshold: float | None
    market_score_threshold: float | None
    execution_modes_allowed: list[str]
    preferred_mode: str | None
    mode_selection_policy: str
    asset_classes_allowed: list[str]
    instruments_allowed: list[str] | None
    max_position_pct: float
    max_total_exposure_pct: float
    max_daily_loss_pct: float
    max_open_positions: int
    entry_cash_buffer_bps: float | None
    allowed_brokers: list[str] | None
    autopilot: bool
    is_active: bool
    entries_enabled: bool
    exits_enabled: bool


class StrategySafetyParameters(BaseModel):
    """Reviewed switches allowed to cross the scoring/execution trust boundary."""

    model_config = ConfigDict(extra="forbid")

    require_stop_loss: bool = Field(default=True, strict=True)
    require_explicit_scoring_inputs: bool = Field(default=True, strict=True)


class StrategyConfigIn(BaseModel):
    """Tenant-owned strategy policy; routing and risk caps remain binding-owned."""

    model_config = ConfigDict(extra="forbid")

    execution_mode: str = "spot"
    is_active: bool = Field(default=False, strict=True)
    parameters: StrategySafetyParameters = Field(default_factory=StrategySafetyParameters)

    @field_validator("execution_mode")
    @classmethod
    def normalize_execution_mode(cls, value: str) -> str:
        try:
            return ExecutionMode(value.strip().lower()).value
        except ValueError as exc:
            msg = f"Unsupported execution mode {value!r}"
            raise ValueError(msg) from exc


class StrategyConfigOut(BaseModel):
    config_id: str
    user_id: str
    strategy_id: str
    execution_mode: str
    is_active: bool
    parameters: StrategySafetyParameters
    created_at: datetime
    updated_at: datetime


class DrawdownMandateIn(BaseModel):
    """Append-only user drawdown ceiling used by portfolio target strategies."""

    model_config = ConfigDict(extra="forbid")

    max_drawdown_pct: Decimal = Field(
        ge=Decimal("0.05"),
        le=Decimal("0.50"),
        decimal_places=4,
    )


class DrawdownMandateOut(BaseModel):
    mandate_id: int
    user_id: str
    max_drawdown_pct: Decimal
    effective_at: datetime


class BrokerCredentialIn(BaseModel):
    """Broker-specific secret material accepted only on write surfaces."""

    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=16384)
    api_secret: SecretStr | None = Field(default=None, min_length=1, max_length=16384)
    passphrase: SecretStr | None = Field(default=None, min_length=1, max_length=4096)
    subaccount: str | None = Field(default=None, min_length=1, max_length=200)
    access_token: SecretStr | None = Field(default=None, min_length=1, max_length=16384)
    access_token_expires_at: datetime | None = None
    refresh_token: SecretStr | None = Field(default=None, min_length=1, max_length=16384)
    refresh_token_expires_at: datetime | None = None
    account_key: str | None = Field(default=None, min_length=1, max_length=200)
    client_key: str | None = Field(default=None, min_length=1, max_length=200)
    region: Literal["global", "india"] | None = None
    gateway_url: AnyHttpUrl | None = None
    ca_cert: str | None = Field(default=None, min_length=1, max_length=4096)


class BrokerAccountIn(BaseModel):
    broker_code: str = Field(min_length=1, max_length=50)
    environment: Literal["paper", "live"]
    credentials: BrokerCredentialIn = Field(default_factory=BrokerCredentialIn)
    base_ccy: str = Field(
        min_length=3,
        max_length=10,
        pattern=r"^[A-Z][A-Z0-9]{2,9}$",
    )
    display_name: str | None = None
    paper_initial_equity: Decimal | None = Field(default=None, gt=0)
    paper_initial_cash: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_paper_capital(self) -> BrokerAccountIn:
        """Require explicit, internally consistent capital for paper routes."""
        if self.environment == "paper":
            if self.paper_initial_equity is None or self.paper_initial_cash is None:
                msg = "paper accounts require paper_initial_equity and paper_initial_cash"
                raise ValueError(msg)
            if self.paper_initial_cash > self.paper_initial_equity:
                msg = "paper_initial_cash cannot exceed paper_initial_equity"
                raise ValueError(msg)
        elif self.paper_initial_equity is not None or self.paper_initial_cash is not None:
            msg = "paper capital fields are only valid for paper accounts"
            raise ValueError(msg)
        return self


class BrokerCredentialRotationOut(BaseModel):
    account_id: int
    secret_ref: str
    expires_at: datetime | None
    status: str


class BrokerAccountOut(BaseModel):
    account_id: int
    broker_code: str
    environment: str
    status: str
    secret_ref: str  # pointer only — the key material is never returned
    base_ccy: str
    paper_initial_equity: Decimal | None
    paper_initial_cash: Decimal | None


class MarketSessionIn(BaseModel):
    opens_at: datetime
    closes_at: datetime


class MarketCalendarIn(BaseModel):
    source_kind: Literal["broker", "exchange"]
    provider: str = Field(min_length=1, max_length=100)
    source_reference: AnyHttpUrl
    observed_at: datetime
    coverage_start: datetime
    coverage_end: datetime
    sessions: list[MarketSessionIn]
    instrument_ids: list[int] = Field(min_length=1)


class MarketCalendarOut(BaseModel):
    calendar_id: int
    code: str
    provider: str
    coverage_start: datetime
    coverage_end: datetime
    observed_at: datetime
    session_count: int
    instrument_ids: list[int]


_CREDENTIAL_FIELDS = frozenset(BrokerCredentialIn.model_fields)
_BROKER_CREDENTIAL_RULES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "paper": (frozenset(), frozenset()),
    "coinbase": (
        frozenset({"api_key", "api_secret"}),
        frozenset({"api_key", "api_secret", "passphrase"}),
    ),
    "deribit": (
        frozenset({"api_key", "api_secret"}),
        frozenset({"api_key", "api_secret", "subaccount"}),
    ),
    "delta": (
        frozenset({"api_key", "api_secret", "region"}),
        frozenset({"api_key", "api_secret", "region"}),
    ),
    "ibkr": (
        frozenset({"subaccount", "gateway_url"}),
        frozenset({"subaccount", "gateway_url", "ca_cert"}),
    ),
    "zerodha": (
        frozenset(
            {
                "api_key",
                "api_secret",
                "access_token",
                "access_token_expires_at",
            }
        ),
        frozenset(
            {
                "api_key",
                "api_secret",
                "access_token",
                "access_token_expires_at",
            }
        ),
    ),
    "saxo": (
        frozenset(
            {
                "api_key",
                "api_secret",
                "access_token",
                "access_token_expires_at",
                "refresh_token",
                "refresh_token_expires_at",
                "account_key",
                "client_key",
            }
        ),
        frozenset(
            {
                "api_key",
                "api_secret",
                "access_token",
                "access_token_expires_at",
                "refresh_token",
                "refresh_token_expires_at",
                "account_key",
                "client_key",
            }
        ),
    ),
}


def _credential_values(payload: BrokerCredentialIn) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_name in _CREDENTIAL_FIELDS:
        raw = getattr(payload, field_name)
        if raw is None:
            continue
        if isinstance(raw, SecretStr):
            value = raw.get_secret_value()
        elif isinstance(raw, datetime):
            value = raw.isoformat()
        else:
            value = str(raw)
        normalized = value.strip()
        if normalized:
            values[field_name] = normalized
    return values


def _credential_blob(
    *,
    broker_code: str,
    environment: str,
    payload: BrokerCredentialIn,
) -> tuple[dict[str, str], datetime | None]:
    """Validate and serialize one complete broker credential snapshot."""
    code = broker_code.strip().lower()
    try:
        required, allowed = _BROKER_CREDENTIAL_RULES[code]
    except KeyError as exc:
        msg = f"broker {broker_code!r} has no credential contract"
        raise ValueError(msg) from exc

    values = _credential_values(payload)
    if environment == "paper":
        # Normal tenant paper execution is always the deterministic in-process
        # broker. Remote sandboxes are isolated certification workflows and do
        # not share linked-account credential state.
        required = frozenset()
        allowed = frozenset()
    present = frozenset(values)
    missing = sorted(required - present)
    unexpected = sorted(present - allowed)
    if missing:
        msg = f"{code} credentials are missing required fields: {', '.join(missing)}"
        raise ValueError(msg)
    if unexpected:
        msg = f"{code} credentials contain unsupported fields: {', '.join(unexpected)}"
        raise ValueError(msg)
    if code == "ibkr" and environment == "live" and "ca_cert" not in present:
        msg = "ibkr live credentials require ca_cert for gateway TLS verification"
        raise ValueError(msg)
    if (
        code == "ibkr"
        and environment == "live"
        and not values["gateway_url"].startswith("https://")
    ):
        msg = "ibkr gateway_url must use HTTPS"
        raise ValueError(msg)

    access_expiry = payload.access_token_expires_at
    refresh_expiry = payload.refresh_token_expires_at
    now = datetime.now(tz=UTC)
    for name, expiry in (
        ("access_token_expires_at", access_expiry),
        ("refresh_token_expires_at", refresh_expiry),
    ):
        if expiry is None:
            continue
        if expiry.tzinfo is None or expiry.utcoffset() is None:
            msg = f"{code} {name} must be timezone-aware"
            raise ValueError(msg)
        if expiry.astimezone(UTC) <= now:
            msg = f"{code} {name} must be in the future"
            raise ValueError(msg)
    if (
        access_expiry is not None
        and refresh_expiry is not None
        and refresh_expiry.astimezone(UTC) <= access_expiry.astimezone(UTC)
    ):
        msg = f"{code} refresh_token_expires_at must be after access_token_expires_at"
        raise ValueError(msg)

    expires_at = (
        access_expiry.astimezone(UTC).replace(tzinfo=None) if access_expiry is not None else None
    )
    return values, expires_at


def _require_supported_broker_environment(
    session: Any,
    *,
    broker: Broker,
    environment: str,
    credentials: BrokerCredentialIn,
) -> None:
    query = session.query(BrokerEnvironment).filter(
        BrokerEnvironment.broker_id == broker.broker_id,
        BrokerEnvironment.environment == environment,
    )
    if str(broker.code) == "delta" and environment == "live":
        query = query.filter(BrokerEnvironment.region == credentials.region)
    rows = query.all()
    if not rows:
        detail = f"{broker.code} does not support environment {environment!r}"
        if str(broker.code) == "delta" and credentials.region:
            detail += f" in region {credentials.region!r}"
        raise HTTPException(status_code=422, detail=detail)
    if len(rows) > 1 and not (str(broker.code) == "delta" and environment == "paper"):
        logger.error(
            "Broker catalogue has ambiguous environment rows",
            broker=str(broker.code),
            environment=environment,
        )
        raise HTTPException(status_code=503, detail="broker catalogue is ambiguous")


def _binding_out(row: UserStrategyBinding) -> BindingOut:
    return BindingOut(
        binding_id=int(row.binding_id),
        user_id=str(row.user_id),
        strategy_id=row.strategy_id,
        broker_account_id=int(row.broker_account_id),
        asset_score_threshold=float(row.asset_score_threshold),
        sector_score_threshold=(
            float(row.sector_score_threshold) if row.sector_score_threshold is not None else None
        ),
        market_score_threshold=(
            float(row.market_score_threshold) if row.market_score_threshold is not None else None
        ),
        execution_modes_allowed=list(row.execution_modes_allowed or []),
        preferred_mode=row.preferred_mode,
        mode_selection_policy=str(row.mode_selection_policy),
        asset_classes_allowed=list(row.asset_classes_allowed or []),
        instruments_allowed=(
            list(row.instruments_allowed) if row.instruments_allowed is not None else None
        ),
        max_position_pct=float(row.max_position_pct),
        max_total_exposure_pct=float(row.max_total_exposure_pct),
        max_daily_loss_pct=float(row.max_daily_loss_pct),
        max_open_positions=int(row.max_open_positions),
        entry_cash_buffer_bps=(
            float(row.entry_cash_buffer_bps) if row.entry_cash_buffer_bps is not None else None
        ),
        allowed_brokers=list(row.allowed_brokers) if row.allowed_brokers else None,
        autopilot=bool(row.autopilot),
        is_active=bool(row.is_active),
        entries_enabled=bool(row.entries_enabled),
        exits_enabled=bool(row.exits_enabled),
    )


def _strategy_config_out(row: UserStrategyConfig) -> StrategyConfigOut:
    """Render only the reviewed strategy-policy schema, never arbitrary JSON."""

    parameters = StrategySafetyParameters.model_validate(dict(row.parameters or {}))
    return StrategyConfigOut(
        config_id=str(row.config_id),
        user_id=str(row.user_id),
        strategy_id=str(row.strategy_id),
        execution_mode=str(row.execution_mode),
        is_active=bool(row.is_active),
        parameters=parameters,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _drawdown_mandate_out(row: RiskMandate) -> DrawdownMandateOut:
    rules = dict(row.rules or {})
    if set(rules) != {"max_drawdown_pct"}:
        msg = f"Risk mandate {row.mandate_id} is not an exact drawdown mandate"
        raise RuntimeError(msg)
    try:
        validated = DrawdownMandateIn(max_drawdown_pct=rules["max_drawdown_pct"])
    except ValueError as exc:
        msg = f"Risk mandate {row.mandate_id} has an invalid drawdown ceiling"
        raise RuntimeError(msg) from exc
    if row.user_id is None:
        msg = f"Risk mandate {row.mandate_id} is not user-owned"
        raise RuntimeError(msg)
    return DrawdownMandateOut(
        mandate_id=int(row.mandate_id),
        user_id=str(row.user_id),
        max_drawdown_pct=validated.max_drawdown_pct,
        effective_at=row.effective_at,
    )


def _lock_tenant_control(session: Any, *, user_id: str, resource_key: str) -> None:
    """Serialize absent-row upserts without taking a cross-tenant table lock."""

    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"backend-control:{user_id}:{resource_key}"},
    )


def _append_control_audit(
    session: Any,
    *,
    user_id: str,
    action: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> None:
    """Append an immutable tenant-owned audit row in the mutation transaction."""

    session.add(
        ApiAuditLog(
            user_id=user_id,
            account_id=None,
            action=action,
            req=request_payload,
            resp=response_payload,
            status="ok",
        )
    )


def _require_strategy_release(
    session: Any,
    *,
    strategy_id: str,
    active_required: bool,
) -> None:
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if not active_required:
        return
    if strategy.is_active is not True:
        raise HTTPException(status_code=409, detail="strategy is not active")
    active_version = (
        session.query(StrategyVersion.strat_ver_id)
        .filter(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.status == "active",
        )
        .first()
    )
    if active_version is None:
        raise HTTPException(status_code=409, detail="strategy has no active release")


def _canonical_binding_instruments(
    session: Any,
    values: list[str] | None,
) -> list[str] | None:
    """Resolve an executable binding scope to canonical catalogue symbols."""
    if values is None:
        return None
    canonical: list[str] = []
    for value in values:
        token = str(value).strip()
        try:
            if token.isascii() and token.isdigit() and not token.startswith("0"):
                instrument = session.get(Instrument, int(token))
            else:
                instrument = resolve_instrument(session, token)
        except InstrumentResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if instrument is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown instrument {token!r}; provision it in the catalogue first",
            )
        symbol = str(instrument.canonical)
        if symbol not in canonical:
            canonical.append(symbol)
    return canonical


def _binding_scopes_overlap(
    session: Any,
    left: list[Any] | None,
    right: list[Any] | None,
) -> bool:
    """Return whether two binding scopes can authorize the same instrument."""
    if not left or not right:
        return True
    left_scope = set(_canonical_binding_instruments(session, [str(item) for item in left]) or [])
    right_scope = set(_canonical_binding_instruments(session, [str(item) for item in right]) or [])
    return bool(left_scope & right_scope)


def _reject_conflicting_active_binding(
    session: Any,
    *,
    payload: BindingIn,
    canonical_instruments: list[str] | None,
    existing_binding_id: int | None,
) -> None:
    """Enforce one active strategy per account/instrument before authority changes."""
    if not payload.is_active:
        return
    query = session.query(UserStrategyBinding).filter(
        UserStrategyBinding.broker_account_id == payload.broker_account_id,
        UserStrategyBinding.is_active.is_(True),
    )
    if existing_binding_id is not None:
        query = query.filter(UserStrategyBinding.binding_id != existing_binding_id)
    for candidate in query.all():
        if candidate.strategy_id == payload.strategy_id:
            continue
        if _binding_scopes_overlap(
            session,
            list(candidate.instruments_allowed or []) or None,
            canonical_instruments,
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "another active strategy already owns an overlapping instrument "
                    f"scope on broker_account_id={payload.broker_account_id}"
                ),
            )


def _resolve_admin_auth(
    admin_api_key: str | None,
    allow_anon: bool | None,
) -> tuple[str | None, bool]:
    resolved_key = (
        admin_api_key if admin_api_key is not None else os.environ.get("BACKEND_ADMIN_API_KEY")
    )
    resolved_anon = (
        allow_anon
        if allow_anon is not None
        else parse_bool_env("BACKEND_ALLOW_ANON", default=False)
    )
    if not resolved_key and not resolved_anon:
        msg = (
            "BACKEND_ADMIN_API_KEY is not set and BACKEND_ALLOW_ANON is not true; "
            "refusing to start the config API without an auth gate. Set "
            "BACKEND_ADMIN_API_KEY (production) or BACKEND_ALLOW_ANON=true (local dev only)."
        )
        raise RuntimeError(msg)
    if resolved_anon and is_production_environment():
        msg = (
            "BACKEND_ALLOW_ANON=true is a local-development escape hatch and is "
            "refused in production; set BACKEND_ADMIN_API_KEY instead."
        )
        raise RuntimeError(msg)
    return resolved_key, resolved_anon


def _build_admin_dependency(
    admin_api_key: str | None,
    allow_anon: bool,
) -> Callable[..., None]:
    def _require_admin(x_admin_key: str | None = Header(default=None)) -> None:
        if not admin_api_key:
            if allow_anon:
                return
            raise HTTPException(status_code=401, detail="admin key not configured")
        if not secret_matches(x_admin_key, admin_api_key):
            raise HTTPException(status_code=401, detail="invalid or missing admin key")

    return _require_admin


def _require_writable_secrets_provider(secrets_provider: Any | None) -> Any:
    """Return a credential writer or fail without entering a DB unit-of-work."""
    if secrets_provider is None or not callable(getattr(secrets_provider, "set_secret", None)):
        raise HTTPException(status_code=503, detail=_WRITABLE_SECRETS_UNAVAILABLE)
    return secrets_provider


def _rotate_broker_credentials(
    *,
    session_factory: SessionFactory,
    secrets_provider: Any,
    user_id: str,
    account_id: int,
    payload: BrokerCredentialIn,
) -> BrokerCredentialRotationOut:
    if account_id <= 0:
        raise HTTPException(status_code=404, detail="broker account not found")

    with session_factory() as s, tenant_scope(s, user_id=user_id):
        row = (
            s.query(LinkedBrokerAccount, Broker, BrokerCredential)
            .join(Broker, Broker.broker_id == LinkedBrokerAccount.broker_id)
            .join(
                BrokerCredential,
                BrokerCredential.account_id == LinkedBrokerAccount.account_id,
            )
            .filter(
                LinkedBrokerAccount.account_id == account_id,
                LinkedBrokerAccount.user_id == user_id,
                LinkedBrokerAccount.status == "connected",
                BrokerCredential.status == "active",
            )
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="broker account not found")

        account, broker, credential = row
        try:
            credential_blob, expires_at = _credential_blob(
                broker_code=str(broker.code),
                environment=str(account.environment),
                payload=payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _require_supported_broker_environment(
            s,
            broker=broker,
            environment=str(account.environment),
            credentials=payload,
        )

        credential.expires_at = expires_at
        credential.last_rotated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        credential.status = "active"
        secrets_provider.set_secret(
            str(credential.secret_ref),
            json.dumps(credential_blob),
            account_id=int(account.account_id),
            session=s,
        )
        s.commit()

        return BrokerCredentialRotationOut(
            account_id=int(account.account_id),
            secret_ref=str(credential.secret_ref),
            expires_at=credential.expires_at,
            status=str(credential.status),
        )


def _replace_market_calendar(
    *,
    session_factory: SessionFactory,
    code: str,
    payload: MarketCalendarIn,
) -> MarketCalendarOut:
    with session_factory() as session:
        try:
            calendar = replace_market_calendar(
                session,
                code=code,
                source_kind=payload.source_kind,
                provider=payload.provider,
                source_reference=str(payload.source_reference),
                observed_at=payload.observed_at,
                coverage_start=payload.coverage_start,
                coverage_end=payload.coverage_end,
                windows=[
                    MarketSessionWindow(
                        opens_at=window.opens_at,
                        closes_at=window.closes_at,
                    )
                    for window in payload.sessions
                ],
                instrument_ids=payload.instrument_ids,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        coverage_start = calendar.coverage_start
        coverage_end = calendar.coverage_end
        observed_at = calendar.observed_at
        if coverage_start is None or coverage_end is None or observed_at is None:
            msg = "Replaced market calendar is missing authoritative coverage"
            raise RuntimeError(msg)
        session.commit()
        return MarketCalendarOut(
            calendar_id=int(calendar.calendar_id),
            code=str(calendar.code),
            provider=str(calendar.provider),
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            observed_at=observed_at,
            session_count=len(payload.sessions),
            instrument_ids=sorted(set(payload.instrument_ids)),
        )


def _register_market_calendar_route(
    app: FastAPI,
    *,
    session_factory: SessionFactory,
    require_admin: Callable[..., None],
) -> None:
    @app.put("/market-calendars/{code}", dependencies=[Depends(require_admin)])
    def sync_market_calendar(code: str, payload: MarketCalendarIn) -> MarketCalendarOut:
        """Atomically replace official session coverage for exact instruments."""
        return _replace_market_calendar(
            session_factory=session_factory,
            code=code,
            payload=payload,
        )


def create_app(  # noqa: PLR0915
    *,
    session_factory: SessionFactory,
    secrets_provider: Any | None = None,
    admin_api_key: str | None = None,
    allow_anon: bool | None = None,
) -> FastAPI:
    """Build the config API. Dependencies are injected for testability."""
    app = create_service_app(
        title="vynmatrix Config API",
        version="0.1.0",
        service_auth=False,
    )
    _require_admin = _build_admin_dependency(*_resolve_admin_auth(admin_api_key, allow_anon))
    _register_market_calendar_route(
        app,
        session_factory=session_factory,
        require_admin=_require_admin,
    )

    @app.get("/users/{user_id}/bindings", dependencies=[Depends(_require_admin)])
    def list_bindings(user_id: str) -> list[BindingOut]:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            rows = s.query(UserStrategyBinding).filter(UserStrategyBinding.user_id == user_id).all()
            return [_binding_out(r) for r in rows]

    @app.post("/users/{user_id}/bindings", dependencies=[Depends(_require_admin)])
    def upsert_binding(user_id: str, payload: BindingIn) -> BindingOut:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            # Serialize authority changes for one broker account before reading
            # competing bindings. Under PostgreSQL READ COMMITTED this separate
            # row-locking statement ensures the subsequent conflict query sees
            # any binding committed by a writer that held the lock first.
            account = (
                s.query(LinkedBrokerAccount)
                .filter(
                    LinkedBrokerAccount.account_id == payload.broker_account_id,
                    LinkedBrokerAccount.user_id == user_id,
                    LinkedBrokerAccount.status == "connected",
                )
                .with_for_update()
                .one_or_none()
            )
            if account is None:
                raise HTTPException(
                    status_code=400,
                    detail="broker_account_id is not a connected account owned by this user",
                )
            account_broker = s.get(Broker, account.broker_id)
            if account_broker is None:
                raise HTTPException(
                    status_code=400,
                    detail="broker_account_id references an unknown broker",
                )
            allowed_brokers = {
                str(code).strip().lower() for code in payload.allowed_brokers or [] if code
            }
            if allowed_brokers and str(account_broker.code).lower() not in allowed_brokers:
                raise HTTPException(
                    status_code=400,
                    detail="broker_account_id is outside allowed_brokers",
                )
            row = (
                s.query(UserStrategyBinding)
                .filter(
                    UserStrategyBinding.user_id == user_id,
                    UserStrategyBinding.strategy_id == payload.strategy_id,
                    UserStrategyBinding.broker_account_id == payload.broker_account_id,
                )
                .one_or_none()
            )
            fields = payload.model_dump()
            canonical_instruments = _canonical_binding_instruments(
                s,
                payload.instruments_allowed,
            )
            fields["instruments_allowed"] = canonical_instruments
            _reject_conflicting_active_binding(
                s,
                payload=payload,
                canonical_instruments=canonical_instruments,
                existing_binding_id=int(row.binding_id) if row is not None else None,
            )
            if row is None:
                row = UserStrategyBinding(user_id=user_id, **fields)
                s.add(row)
            else:
                for key, value in fields.items():
                    setattr(row, key, value)
            s.commit()
            s.refresh(row)
            return _binding_out(row)

    @app.delete("/users/{user_id}/bindings/{binding_id}", dependencies=[Depends(_require_admin)])
    def deactivate_binding(user_id: str, binding_id: int) -> dict[str, Any]:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            row = (
                s.query(UserStrategyBinding)
                .filter(
                    UserStrategyBinding.binding_id == binding_id,
                    UserStrategyBinding.user_id == user_id,
                )
                .one_or_none()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="binding not found")
            row.is_active = False
            row.autopilot = False
            row.entries_enabled = False
            row.exits_enabled = False
            s.commit()
            return {
                "binding_id": binding_id,
                "is_active": False,
                "entries_enabled": False,
                "exits_enabled": False,
            }

    @app.get(
        "/users/{user_id}/strategy-configs",
        dependencies=[Depends(_require_admin)],
    )
    def list_strategy_configs(user_id: str) -> list[StrategyConfigOut]:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            rows = (
                s.query(UserStrategyConfig)
                .filter(UserStrategyConfig.user_id == user_id)
                .order_by(UserStrategyConfig.strategy_id.asc())
                .all()
            )
            return [_strategy_config_out(row) for row in rows]

    @app.put(
        "/users/{user_id}/strategy-configs/{strategy_id}",
        dependencies=[Depends(_require_admin)],
    )
    def upsert_strategy_config(
        user_id: str,
        strategy_id: str,
        payload: StrategyConfigIn,
    ) -> StrategyConfigOut:
        normalized_strategy_id = strategy_id.strip()
        if not normalized_strategy_id:
            raise HTTPException(status_code=404, detail="strategy not found")
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            _lock_tenant_control(
                s,
                user_id=user_id,
                resource_key=f"strategy-config:{normalized_strategy_id}",
            )
            _require_strategy_release(
                s,
                strategy_id=normalized_strategy_id,
                active_required=payload.is_active,
            )
            row = (
                s.query(UserStrategyConfig)
                .filter(
                    UserStrategyConfig.user_id == user_id,
                    UserStrategyConfig.strategy_id == normalized_strategy_id,
                )
                .one_or_none()
            )
            parameters = payload.parameters.model_dump(mode="python")
            outcome = "created"
            if row is None:
                row = UserStrategyConfig(
                    user_id=user_id,
                    strategy_id=normalized_strategy_id,
                    execution_mode=payload.execution_mode,
                    is_active=payload.is_active,
                    parameters=parameters,
                )
                s.add(row)
            else:
                outcome = "updated"
                row.execution_mode = payload.execution_mode
                row.is_active = payload.is_active
                row.parameters = parameters
                row.updated_at = datetime.now(tz=UTC)
            s.flush()
            response = _strategy_config_out(row)
            _append_control_audit(
                s,
                user_id=user_id,
                action="strategy_config.upsert",
                request_payload={
                    "strategy_id": normalized_strategy_id,
                    "execution_mode": payload.execution_mode,
                    "is_active": payload.is_active,
                    "parameters": parameters,
                },
                response_payload={
                    "config_id": response.config_id,
                    "outcome": outcome,
                },
            )
            s.commit()
            return response

    @app.delete(
        "/users/{user_id}/strategy-configs/{strategy_id}",
        dependencies=[Depends(_require_admin)],
    )
    def deactivate_strategy_config(user_id: str, strategy_id: str) -> StrategyConfigOut:
        normalized_strategy_id = strategy_id.strip()
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            _lock_tenant_control(
                s,
                user_id=user_id,
                resource_key=f"strategy-config:{normalized_strategy_id}",
            )
            row = (
                s.query(UserStrategyConfig)
                .filter(
                    UserStrategyConfig.user_id == user_id,
                    UserStrategyConfig.strategy_id == normalized_strategy_id,
                )
                .one_or_none()
            )
            if row is None:
                raise HTTPException(status_code=404, detail="strategy config not found")
            outcome = "deactivated" if row.is_active else "unchanged"
            row.is_active = False
            row.updated_at = datetime.now(tz=UTC)
            s.flush()
            response = _strategy_config_out(row)
            _append_control_audit(
                s,
                user_id=user_id,
                action="strategy_config.deactivate",
                request_payload={"strategy_id": normalized_strategy_id},
                response_payload={
                    "config_id": response.config_id,
                    "outcome": outcome,
                    "is_active": False,
                },
            )
            s.commit()
            return response

    @app.get(
        "/users/{user_id}/risk-mandates/drawdown",
        dependencies=[Depends(_require_admin)],
    )
    def list_drawdown_mandates(user_id: str) -> list[DrawdownMandateOut]:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            rows = (
                s.query(RiskMandate)
                .filter(RiskMandate.user_id == user_id)
                .order_by(RiskMandate.effective_at.desc(), RiskMandate.mandate_id.desc())
                .all()
            )
            return [
                _drawdown_mandate_out(row)
                for row in rows
                if set(dict(row.rules or {})) == {"max_drawdown_pct"}
            ]

    @app.put(
        "/users/{user_id}/risk-mandates/drawdown",
        dependencies=[Depends(_require_admin)],
    )
    def upsert_drawdown_mandate(
        user_id: str,
        payload: DrawdownMandateIn,
    ) -> DrawdownMandateOut:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            _lock_tenant_control(s, user_id=user_id, resource_key="drawdown-mandate")
            rows = (
                s.query(RiskMandate)
                .filter(RiskMandate.user_id == user_id)
                .order_by(RiskMandate.effective_at.desc(), RiskMandate.mandate_id.desc())
                .all()
            )
            existing_values: list[Decimal] = []
            exact_matches: list[RiskMandate] = []
            for row in rows:
                rules = dict(row.rules or {})
                if "max_drawdown_pct" not in rules:
                    continue
                try:
                    value = DrawdownMandateIn(
                        max_drawdown_pct=rules["max_drawdown_pct"]
                    ).max_drawdown_pct
                except ValueError as exc:
                    msg = f"Risk mandate {row.mandate_id} has an invalid drawdown ceiling"
                    raise RuntimeError(msg) from exc
                existing_values.append(value)
                if set(rules) == {"max_drawdown_pct"} and value == payload.max_drawdown_pct:
                    exact_matches.append(row)

            if existing_values and payload.max_drawdown_pct > min(existing_values):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "append-only drawdown mandates may tighten but cannot loosen the "
                        "existing user ceiling"
                    ),
                )

            outcome = "unchanged"
            if exact_matches:
                row = exact_matches[0]
            else:
                outcome = "created"
                row = RiskMandate(
                    user_id=user_id,
                    rules={"max_drawdown_pct": float(payload.max_drawdown_pct)},
                    effective_at=datetime.now(tz=UTC),
                )
                s.add(row)
                s.flush()

            response = _drawdown_mandate_out(row)
            _append_control_audit(
                s,
                user_id=user_id,
                action="risk_mandate.drawdown_upsert",
                request_payload={
                    "max_drawdown_pct": str(payload.max_drawdown_pct),
                },
                response_payload={
                    "mandate_id": response.mandate_id,
                    "outcome": outcome,
                },
            )
            s.commit()
            return response

    @app.get("/users/{user_id}/broker-accounts", dependencies=[Depends(_require_admin)])
    def list_broker_accounts(user_id: str) -> list[BrokerAccountOut]:
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            rows = (
                s.query(LinkedBrokerAccount, Broker, BrokerCredential)
                .join(Broker, Broker.broker_id == LinkedBrokerAccount.broker_id)
                .outerjoin(
                    BrokerCredential,
                    BrokerCredential.account_id == LinkedBrokerAccount.account_id,
                )
                .filter(LinkedBrokerAccount.user_id == user_id)
                .all()
            )
            return [
                BrokerAccountOut(
                    account_id=int(acc.account_id),
                    broker_code=str(broker.code),
                    environment=str(acc.environment),
                    status=str(acc.status),
                    secret_ref=str(cred.secret_ref) if cred else "",
                    base_ccy=str(acc.base_ccy),
                    paper_initial_equity=acc.paper_initial_equity,
                    paper_initial_cash=acc.paper_initial_cash,
                )
                for acc, broker, cred in rows
            ]

    @app.post("/users/{user_id}/broker-accounts", dependencies=[Depends(_require_admin)])
    def onboard_broker_account(user_id: str, payload: BrokerAccountIn) -> BrokerAccountOut:
        writable_secrets = _require_writable_secrets_provider(secrets_provider)
        with session_factory() as s, tenant_scope(s, user_id=user_id):
            broker = s.query(Broker).filter(Broker.code == payload.broker_code).one_or_none()
            if broker is None:
                raise HTTPException(status_code=404, detail=f"unknown broker {payload.broker_code}")
            try:
                credential_blob, expires_at = _credential_blob(
                    broker_code=str(broker.code),
                    environment=payload.environment,
                    payload=payload.credentials,
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            _require_supported_broker_environment(
                s,
                broker=broker,
                environment=payload.environment,
                credentials=payload.credentials,
            )
            account = LinkedBrokerAccount(
                user_id=user_id,
                broker_id=broker.broker_id,
                environment=payload.environment,
                display_name=payload.display_name or f"{broker.name} {payload.environment}",
                base_ccy=payload.base_ccy,
                paper_initial_equity=payload.paper_initial_equity,
                paper_initial_cash=payload.paper_initial_cash,
                status="connected",
            )
            s.add(account)
            s.flush()  # assign account_id
            secret_ref = f"users/{user_id}/broker-accounts/{int(account.account_id)}"
            s.add(
                BrokerCredential(
                    account_id=account.account_id,
                    secret_ref=secret_ref,
                    expires_at=expires_at,
                    status="active",
                )
            )
            # The account, credential pointer, and ciphertext are one atomic
            # unit-of-work. A partial onboarding must never leave a connected
            # account whose credential reference cannot be resolved.
            writable_secrets.set_secret(
                secret_ref,
                json.dumps(credential_blob),
                account_id=int(account.account_id),
                session=s,
            )
            s.commit()
            account_id = int(account.account_id)
        logger.info("Onboarded broker account", user_id=user_id, broker=payload.broker_code)
        return BrokerAccountOut(
            account_id=account_id,
            broker_code=payload.broker_code,
            environment=payload.environment,
            status="connected",
            secret_ref=secret_ref,
            base_ccy=payload.base_ccy,
            paper_initial_equity=payload.paper_initial_equity,
            paper_initial_cash=payload.paper_initial_cash,
        )

    @app.put(
        "/users/{user_id}/broker-accounts/{account_id}/credentials",
        dependencies=[Depends(_require_admin)],
    )
    def rotate_broker_credentials(
        user_id: str,
        account_id: int,
        payload: BrokerCredentialIn,
    ) -> BrokerCredentialRotationOut:
        """Atomically replace one complete broker credential snapshot.

        OAuth refresh-token rotation invalidates the previous token document at
        several brokers. Partial field patches are therefore deliberately not
        supported: callers must persist the full replacement document in one
        transaction.
        """
        return _rotate_broker_credentials(
            session_factory=session_factory,
            secrets_provider=_require_writable_secrets_provider(secrets_provider),
            user_id=user_id,
            account_id=account_id,
            payload=payload,
        )

    return app
