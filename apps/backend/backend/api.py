"""Authenticated owner control plane with durable account-scoped configuration."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import text

from lib_application.db.models import (
    ApiAuditLog,
    Broker,
    Instrument,
    LinkedBrokerAccount,
    RiskMandate,
    Strategy,
    StrategyVersion,
    UserStrategyBinding,
    UserStrategyConfig,
)
from lib_application.services.account_onboarding import (
    AccountOnboardingError,
    BrokerAccountIn,
    BrokerAccountOut,
    BrokerCredentialIn,
    BrokerCredentialRotationOut,
    account_output,
    adopt_account,
    onboard_account,
    owner_scope,
    patch_account,
    rotate_credentials,
)
from lib_application.services.deployment_owner import DeploymentOwnerError
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)
from lib_application.services.market_calendars import (
    MarketSessionWindow,
    replace_market_calendar,
)
from lib_application.services.owner_onboarding import (
    OwnerOnboardingError,
    apply_owner_patch,
    get_owner_profile,
)
from lib_common.api_security import is_production_environment, secret_matches
from lib_common.app import create_service_app
from lib_common.asset_classes import normalize_asset_class
from lib_common.config_validation import BrokerType, ExecutionMode, normalize_broker_code
from lib_common.env_utils import parse_bool_env
from lib_common.logging import get_logger

logger = get_logger(__name__)

SessionFactory = Callable[[], Any]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class BindingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class MarketSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opens_at: datetime
    closes_at: datetime


class MarketCalendarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
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


class ExpectedPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected: dict[str, Any]
    changes: dict[str, Any]


class AccountAdoptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_key: str = Field(min_length=1, max_length=100)


@contextmanager
def _owner_session(session_factory: SessionFactory) -> Iterator[tuple[Any, str]]:
    with session_factory() as session, owner_scope(session) as user_id:
        yield session, user_id


def _reject_owner_query(request: Request) -> None:
    if "user_id" in request.query_params or any(
        name in request.headers for name in ("user_id", "user-id", "x-user-id")
    ):
        raise HTTPException(status_code=422, detail="caller-supplied user_id is not accepted")


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
    app.router.dependencies.append(Depends(_reject_owner_query))

    @app.exception_handler(OwnerOnboardingError)
    @app.exception_handler(AccountOnboardingError)
    async def account_error(
        _request: Request, exc: AccountOnboardingError | OwnerOnboardingError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(DeploymentOwnerError)
    async def owner_error(_request: Request, exc: DeploymentOwnerError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    _require_admin = _build_admin_dependency(*_resolve_admin_auth(admin_api_key, allow_anon))
    _register_market_calendar_route(
        app,
        session_factory=session_factory,
        require_admin=_require_admin,
    )

    @app.get("/bindings", dependencies=[Depends(_require_admin)])
    def list_bindings() -> list[BindingOut]:
        with _owner_session(session_factory) as (s, user_id):
            rows = s.query(UserStrategyBinding).filter(UserStrategyBinding.user_id == user_id).all()
            return [_binding_out(r) for r in rows]

    @app.post("/bindings", dependencies=[Depends(_require_admin)])
    def upsert_binding(payload: BindingIn) -> BindingOut:
        with _owner_session(session_factory) as (s, user_id):
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
            s.flush()
            response = _binding_out(row)
            _append_control_audit(
                s,
                user_id=user_id,
                action="binding.upsert",
                request_payload=fields,
                response_payload={"binding_id": response.binding_id},
            )
            s.commit()
            return response

    @app.delete("/bindings/{binding_id}", dependencies=[Depends(_require_admin)])
    def deactivate_binding(binding_id: int) -> dict[str, Any]:
        with _owner_session(session_factory) as (s, user_id):
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
            _append_control_audit(
                s,
                user_id=user_id,
                action="binding.deactivate",
                request_payload={"binding_id": binding_id},
                response_payload={"is_active": False},
            )
            s.commit()
            return {
                "binding_id": binding_id,
                "is_active": False,
                "entries_enabled": False,
                "exits_enabled": False,
            }

    @app.get(
        "/strategy-configs",
        dependencies=[Depends(_require_admin)],
    )
    def list_strategy_configs() -> list[StrategyConfigOut]:
        with _owner_session(session_factory) as (s, user_id):
            rows = (
                s.query(UserStrategyConfig)
                .filter(UserStrategyConfig.user_id == user_id)
                .order_by(UserStrategyConfig.strategy_id.asc())
                .all()
            )
            return [_strategy_config_out(row) for row in rows]

    @app.put(
        "/strategy-configs/{strategy_id}",
        dependencies=[Depends(_require_admin)],
    )
    def upsert_strategy_config(
        strategy_id: str,
        payload: StrategyConfigIn,
    ) -> StrategyConfigOut:
        normalized_strategy_id = strategy_id.strip()
        if not normalized_strategy_id:
            raise HTTPException(status_code=404, detail="strategy not found")
        with _owner_session(session_factory) as (s, user_id):
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
        "/strategy-configs/{strategy_id}",
        dependencies=[Depends(_require_admin)],
    )
    def deactivate_strategy_config(strategy_id: str) -> StrategyConfigOut:
        normalized_strategy_id = strategy_id.strip()
        with _owner_session(session_factory) as (s, user_id):
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
        "/risk-mandates/drawdown",
        dependencies=[Depends(_require_admin)],
    )
    def list_drawdown_mandates() -> list[DrawdownMandateOut]:
        with _owner_session(session_factory) as (s, user_id):
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
        "/risk-mandates/drawdown",
        dependencies=[Depends(_require_admin)],
    )
    def upsert_drawdown_mandate(
        payload: DrawdownMandateIn,
    ) -> DrawdownMandateOut:
        with _owner_session(session_factory) as (s, user_id):
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

    @app.get("/owner", dependencies=[Depends(_require_admin)])
    def get_owner() -> dict[str, Any]:
        with session_factory() as session:
            return get_owner_profile(session)

    @app.patch("/owner", dependencies=[Depends(_require_admin)])
    def update_owner(payload: ExpectedPatchIn) -> dict[str, Any]:
        with session_factory() as session:
            response = apply_owner_patch(
                session, expected=payload.expected, changes=payload.changes
            )
            session.commit()
            return response

    @app.get("/broker-accounts", dependencies=[Depends(_require_admin)])
    def list_broker_accounts() -> list[BrokerAccountOut]:
        with _owner_session(session_factory) as (session, user_id):
            accounts = (
                session.query(LinkedBrokerAccount)
                .filter(LinkedBrokerAccount.user_id == user_id)
                .order_by(LinkedBrokerAccount.account_id)
                .all()
            )
            return [account_output(session, account) for account in accounts]

    @app.post("/broker-accounts", dependencies=[Depends(_require_admin)])
    def onboard_broker_account(payload: BrokerAccountIn) -> BrokerAccountOut:
        with session_factory() as session:
            response = onboard_account(session, payload, secrets_provider)
            session.commit()
            return response

    @app.post("/broker-accounts/{account_id}/adopt", dependencies=[Depends(_require_admin)])
    def adopt_broker_account(account_id: int, payload: AccountAdoptionIn) -> BrokerAccountOut:
        with session_factory() as session:
            response = adopt_account(session, account_id, payload.config_key)
            session.commit()
            return response

    @app.patch("/broker-accounts/{account_id}", dependencies=[Depends(_require_admin)])
    def update_broker_account(account_id: int, payload: ExpectedPatchIn) -> BrokerAccountOut:
        with session_factory() as session:
            response = patch_account(session, account_id, payload.expected, payload.changes)
            session.commit()
            return response

    @app.put("/broker-accounts/{account_id}/credentials", dependencies=[Depends(_require_admin)])
    def rotate_broker_credentials(
        account_id: int, payload: BrokerCredentialIn
    ) -> BrokerCredentialRotationOut:
        with session_factory() as session:
            response = rotate_credentials(session, account_id, payload, secrets_provider)
            session.commit()
            return response

    return app
