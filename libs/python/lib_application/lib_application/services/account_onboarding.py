"""Owner account operations shared by HTTP and CLI; callers own transactions."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from lib_application.db.models import (
    AccountExecutionGeneration,
    AccountRebalancePlan,
    ApiAuditLog,
    Broker,
    BrokerCredential,
    BrokerEnvironment,
    DailyNav,
    ExecutionDecisionLog,
    ExecutionLog,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    OutboxEvent,
    PendingOrder,
    Position,
)
from lib_application.db.session import tenant_scope
from lib_application.services.deployment_owner import require_deployment_owner_id
from lib_common.logging import get_logger

logger = get_logger(__name__)
_CONFIG_KEY_MAX_LENGTH = 100
_DISPLAY_NAME_MAX_LENGTH = 255


class AccountOnboardingError(ValueError):
    """A public, secret-free control-plane error."""

    def __init__(self, *, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class SecretsWriter(Protocol):
    def set_secret(
        self, secret_ref: str, plaintext: str, *, account_id: int, session: Any
    ) -> None: ...


class BrokerCredentialIn(BaseModel):
    """Broker-specific secret material accepted only on write surfaces."""

    model_config = ConfigDict(extra="forbid")

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
    model_config = ConfigDict(extra="forbid")
    config_key: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    external_ref: str | None = Field(default=None, min_length=1, max_length=255)
    broker_code: str = Field(min_length=1, max_length=50)
    environment: Literal["paper", "live"]
    credentials: BrokerCredentialIn = Field(default_factory=BrokerCredentialIn)
    base_ccy: str = Field(
        min_length=3,
        max_length=10,
        pattern=r"^[A-Z][A-Z0-9]{2,9}$",
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    paper_initial_equity: Decimal | None = Field(default=None, gt=0)
    paper_initial_cash: Decimal | None = Field(default=None, ge=0)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            message = "display_name must not be blank"
            raise ValueError(message)
        return value

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
    config_key: str | None
    display_name: str
    external_ref: str | None
    account_id: int
    broker_code: str
    environment: str
    status: str
    secret_ref: str  # pointer only — the key material is never returned
    base_ccy: str
    paper_initial_equity: Decimal | None
    paper_initial_cash: Decimal | None


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
        raise AccountOnboardingError(status_code=422, detail=detail)
    if len(rows) > 1 and not (str(broker.code) == "delta" and environment == "paper"):
        logger.error(
            "Broker catalogue has ambiguous environment rows",
            broker=str(broker.code),
            environment=environment,
        )
        raise AccountOnboardingError(status_code=503, detail="broker catalogue is ambiguous")


@contextmanager
def owner_scope(session: Session) -> Iterator[str]:
    """Resolve the designated owner in this transaction before setting RLS scope."""
    owner_id = require_deployment_owner_id(session)
    with tenant_scope(session, user_id=owner_id):
        yield owner_id


def _lock(session: Session, owner_id: str, resource: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"backend-control:{owner_id}:{resource}"},
        )


def _audit(
    session: Session, owner_id: str, action: str, account_id: int | None, fields: dict[str, Any]
) -> None:
    session.add(
        ApiAuditLog(
            user_id=owner_id,
            account_id=account_id,
            action=action,
            req=fields,
            resp={"account_id": account_id},
            status="ok",
        )
    )


def _writer(provider: Any) -> SecretsWriter:
    if provider is None or not callable(getattr(provider, "set_secret", None)):
        raise AccountOnboardingError(
            status_code=503,
            detail=(
                "credential writes are unavailable with the configured secrets backend; "
                "set SECRETS_BACKEND=db and configure SECRETS_MASTER_KEYS"
            ),
        )
    return cast(SecretsWriter, provider)


def _account(session: Session, owner_id: str, account_id: int) -> LinkedBrokerAccount:
    account = session.scalar(
        select(LinkedBrokerAccount)
        .where(
            LinkedBrokerAccount.account_id == account_id,
            LinkedBrokerAccount.user_id == owner_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if account is None:
        raise AccountOnboardingError(status_code=404, detail="broker account not found")
    return account


def account_output(session: Session, account: LinkedBrokerAccount) -> BrokerAccountOut:
    broker = session.get(Broker, account.broker_id)
    credential = session.scalar(
        select(BrokerCredential).where(
            BrokerCredential.account_id == account.account_id,
            BrokerCredential.status == "active",
        )
    )
    if broker is None:
        raise AccountOnboardingError(status_code=503, detail="broker catalogue is unavailable")
    return BrokerAccountOut(
        account_id=int(account.account_id),
        config_key=account.config_key,
        display_name=account.display_name,
        external_ref=account.external_ref,
        broker_code=broker.code,
        environment=account.environment,
        status=account.status,
        secret_ref=credential.secret_ref if credential else "",
        base_ccy=account.base_ccy,
        paper_initial_equity=account.paper_initial_equity,
        paper_initial_cash=account.paper_initial_cash,
    )


def onboard_account(
    session: Session, payload: BrokerAccountIn, secrets_provider: Any
) -> BrokerAccountOut:
    """Register a stable account key; retries never replace settings or credentials."""
    with owner_scope(session) as owner_id:
        _lock(session, owner_id, f"account-key:{payload.config_key}")
        broker = session.scalar(select(Broker).where(Broker.code == payload.broker_code))
        if broker is None:
            raise AccountOnboardingError(
                status_code=404, detail=f"unknown broker {payload.broker_code}"
            )
        local_paper = broker.code == "paper" and payload.environment == "paper"
        if local_paper and payload.credentials.model_dump(exclude_none=True):
            raise AccountOnboardingError(
                status_code=422, detail="local paper accounts must not carry broker credentials"
            )
        existing = session.scalar(
            select(LinkedBrokerAccount)
            .where(
                LinkedBrokerAccount.user_id == owner_id,
                LinkedBrokerAccount.config_key == payload.config_key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            fields = {
                "broker_id": broker.broker_id,
                "environment": payload.environment,
                "base_ccy": payload.base_ccy,
                "paper_initial_equity": payload.paper_initial_equity,
                "paper_initial_cash": payload.paper_initial_cash,
            }
            for optional in ("display_name", "external_ref"):
                if optional in payload.model_fields_set:
                    fields[optional] = getattr(payload, optional)
            if any(getattr(existing, field) != value for field, value in fields.items()):
                raise AccountOnboardingError(
                    status_code=409,
                    detail=(
                        "account key already has different settings; use an expected-value patch"
                    ),
                )
            return account_output(session, existing)
        writer = None if local_paper else _writer(secrets_provider)
        try:
            credentials, expiry = _credential_blob(
                broker_code=broker.code,
                environment=payload.environment,
                payload=payload.credentials,
            )
        except ValueError as exc:
            raise AccountOnboardingError(status_code=422, detail=str(exc)) from exc
        _require_supported_broker_environment(
            session, broker=broker, environment=payload.environment, credentials=payload.credentials
        )
        if payload.environment == "paper" and payload.external_ref is not None:
            raise AccountOnboardingError(
                status_code=422,
                detail=(
                    "local paper accounts use their database account identity; "
                    "external_ref must be omitted"
                ),
            )
        account = LinkedBrokerAccount(
            user_id=owner_id,
            broker_id=broker.broker_id,
            config_key=payload.config_key,
            environment=payload.environment,
            display_name=payload.display_name or f"{broker.name} {payload.environment}",
            external_ref=payload.external_ref,
            base_ccy=payload.base_ccy,
            paper_initial_equity=payload.paper_initial_equity,
            paper_initial_cash=payload.paper_initial_cash,
            status="connected",
        )
        session.add(account)
        session.flush()
        if writer is not None:
            secret_ref = f"users/{owner_id}/broker-accounts/{account.account_id}"
            session.add(
                BrokerCredential(
                    account_id=account.account_id,
                    secret_ref=secret_ref,
                    expires_at=expiry,
                    status="active",
                )
            )
            session.flush()
            writer.set_secret(
                secret_ref,
                json.dumps(credentials),
                account_id=int(account.account_id),
                session=session,
            )
        _audit(
            session,
            owner_id,
            "broker_account.create",
            int(account.account_id),
            {
                "config_key": payload.config_key,
                "broker_code": broker.code,
                "environment": payload.environment,
            },
        )
        session.flush()
        return account_output(session, account)


def rotate_credentials(
    session: Session, account_id: int, payload: BrokerCredentialIn, secrets_provider: Any
) -> BrokerCredentialRotationOut:
    """Replace one complete credential snapshot within the caller's transaction."""
    with owner_scope(session) as owner_id:
        account = _account(session, owner_id, account_id)
        broker = session.get(Broker, account.broker_id)
        if broker is not None and broker.code == "paper" and account.environment == "paper":
            raise AccountOnboardingError(
                status_code=409, detail="local paper accounts do not support credential rotation"
            )
        writer = _writer(secrets_provider)
        credential = session.scalar(
            select(BrokerCredential)
            .where(
                BrokerCredential.account_id == account_id,
                BrokerCredential.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if account.status != "connected" or broker is None or credential is None:
            raise AccountOnboardingError(status_code=404, detail="broker account not found")
        try:
            values, expiry = _credential_blob(
                broker_code=broker.code, environment=account.environment, payload=payload
            )
        except ValueError as exc:
            raise AccountOnboardingError(status_code=422, detail=str(exc)) from exc
        _require_supported_broker_environment(
            session, broker=broker, environment=account.environment, credentials=payload
        )
        credential.expires_at = expiry
        credential.last_rotated_at = datetime.now(tz=UTC).replace(tzinfo=None)
        writer.set_secret(
            credential.secret_ref, json.dumps(values), account_id=account_id, session=session
        )
        _audit(session, owner_id, "broker_account.rotate_credentials", account_id, {})
        session.flush()
        return BrokerCredentialRotationOut(
            account_id=account_id,
            secret_ref=credential.secret_ref,
            expires_at=expiry,
            status=credential.status,
        )


def adopt_account(session: Session, account_id: int, config_key: str) -> BrokerAccountOut:
    """Assign a key to an explicitly named owned legacy account without moving it."""
    if (
        len(config_key) > _CONFIG_KEY_MAX_LENGTH
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config_key) is None
    ):
        raise AccountOnboardingError(status_code=422, detail="config_key is invalid")
    with owner_scope(session) as owner_id:
        _lock(session, owner_id, f"account-key:{config_key}")
        account = _account(session, owner_id, account_id)
        existing = session.scalar(
            select(LinkedBrokerAccount).where(
                LinkedBrokerAccount.user_id == owner_id,
                LinkedBrokerAccount.config_key == config_key,
            )
        )
        if existing is not None and existing.account_id != account_id:
            raise AccountOnboardingError(
                status_code=409, detail="config_key belongs to another account"
            )
        if account.config_key not in (None, config_key):
            raise AccountOnboardingError(
                status_code=409, detail="account already has a different stable key"
            )
        if account.config_key is None:
            account.config_key = config_key
            _audit(
                session, owner_id, "broker_account.adopt", account_id, {"config_key": config_key}
            )
        session.flush()
        return account_output(session, account)


_FINANCIAL_FIELDS = frozenset(
    {"base_ccy", "external_ref", "paper_initial_equity", "paper_initial_cash"}
)
_ACCOUNT_FIELDS = _FINANCIAL_FIELDS | {"display_name", "status"}


def _expected_changes(
    row: Any, expected: dict[str, Any], changes: dict[str, Any], allowed: frozenset[str]
) -> None:
    if not changes or set(changes) != set(expected) or set(changes) - allowed:
        raise AccountOnboardingError(
            status_code=422,
            detail=(
                "every changed field requires an expected current value; "
                "unsupported fields are forbidden"
            ),
        )
    for key, value in expected.items():
        current = getattr(row, key)
        comparison = value
        if isinstance(current, Decimal) and value is not None:
            try:
                comparison = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise AccountOnboardingError(
                    status_code=422, detail=f"invalid expected {key}"
                ) from exc
        desired = changes[key]
        if isinstance(current, Decimal) and desired is not None:
            try:
                desired = Decimal(str(desired))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise AccountOnboardingError(
                    status_code=422, detail=f"invalid desired {key}"
                ) from exc
        if current not in (comparison, desired):
            raise AccountOnboardingError(status_code=409, detail=f"stale expected value for {key}")


def _has_activity(session: Session, account_id: int, owner_id: str) -> bool:
    if session.get_bind().dialect.name == "postgresql":
        try:
            activity = session.scalar(
                text("SELECT public.vm_owner_account_has_activity(:account_id)"),
                {"account_id": account_id},
            )
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
            if code == "55P03":
                raise AccountOnboardingError(
                    status_code=409,
                    detail="account has execution in progress; retry after it completes",
                ) from exc
            raise
        if not isinstance(activity, bool):
            raise AccountOnboardingError(status_code=503, detail="account activity is unavailable")
        return activity
    for model, column in (
        (OrderIntent, OrderIntent.account_id),
        (Order, Order.account_id),
        (PendingOrder, PendingOrder.broker_account_id),
        (ExecutionLog, ExecutionLog.account_id),
        (DailyNav, DailyNav.account_id),
        (Position, Position.account_id),
        (AccountRebalancePlan, AccountRebalancePlan.broker_account_id),
        (ExecutionDecisionLog, ExecutionDecisionLog.broker_account_id),
    ):
        if (
            session.scalar(select(column).select_from(model).where(column == account_id).limit(1))
            is not None
        ):
            return True
    if (
        session.scalar(
            select(AccountExecutionGeneration.broker_account_id).where(
                AccountExecutionGeneration.broker_account_id == account_id,
                AccountExecutionGeneration.active_owner.is_not(None),
            )
        )
        is not None
    ):
        return True
    for event in session.scalars(
        select(OutboxEvent).where(
            OutboxEvent.topic.in_(["execution.commands", "execution.rebalance.commands"]),
            OutboxEvent.status != "published",
        )
    ):
        payload = event.payload if isinstance(event.payload, dict) else {}
        route = payload.get("broker_route")
        route = route if isinstance(route, dict) else {}
        routed_id = route.get("broker_account_id")
        if str(routed_id) == str(account_id) or (
            payload.get("user_id") == owner_id and routed_id is None
        ):
            return True
    return False


def _flush_account_patch(session: Session) -> None:
    try:
        session.flush()
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if code == "55P03" or (code == "23514" and constraint == "guard_account_financial_terms"):
            raise AccountOnboardingError(
                status_code=409, detail="account execution activity prevents financial changes"
            ) from exc
        raise


def patch_account(
    session: Session, account_id: int, expected: dict[str, Any], changes: dict[str, Any]
) -> BrokerAccountOut:
    with owner_scope(session) as owner_id:
        account = _account(session, owner_id, account_id)
        _expected_changes(account, expected, changes, _ACCOUNT_FIELDS)
        merged = {key: getattr(account, key) for key in _ACCOUNT_FIELDS} | changes
        if (
            not isinstance(merged["display_name"], str)
            or not merged["display_name"].strip()
            or len(merged["display_name"]) > _DISPLAY_NAME_MAX_LENGTH
        ):
            raise AccountOnboardingError(status_code=422, detail="display_name is invalid")
        if merged["status"] not in ("connected", "revoked", "error"):
            raise AccountOnboardingError(status_code=422, detail="account status is invalid")
        normalized = dict(changes)
        if _FINANCIAL_FIELDS.intersection(changes):
            try:
                validated = BrokerAccountIn.model_validate(
                    {
                        "config_key": account.config_key or "legacy",
                        "broker_code": "paper",
                        "environment": account.environment,
                        **{key: merged[key] for key in _FINANCIAL_FIELDS},
                    }
                )
            except ValueError as exc:
                raise AccountOnboardingError(
                    status_code=422, detail="account financial settings are invalid"
                ) from exc
            normalized.update(
                {key: getattr(validated, key) for key in changes if key in _FINANCIAL_FIELDS}
            )
        normalized = {
            key: value for key, value in normalized.items() if getattr(account, key) != value
        }
        if not normalized:
            return account_output(session, account)
        financial = any(
            key in _FINANCIAL_FIELDS and getattr(account, key) != value
            for key, value in normalized.items()
        )
        if financial and _has_activity(session, account_id, owner_id):
            raise AccountOnboardingError(
                status_code=409,
                detail="account financial identity is immutable after execution activity",
            )
        if (
            "external_ref" in changes
            and account.environment == "paper"
            and merged["external_ref"] is not None
        ):
            raise AccountOnboardingError(
                status_code=422, detail="local paper external_ref must remain empty"
            )
        for key, value in normalized.items():
            setattr(account, key, value)
        _audit(session, owner_id, "broker_account.patch", account_id, {"fields": sorted(changes)})
        _flush_account_patch(session)
        return account_output(session, account)
