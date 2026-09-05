"""Explicit owner initialization and guarded profile edits; callers own transactions."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from lib_application.db.models import (
    AccountExecutionGeneration,
    AccountRebalancePlan,
    ApiAuditLog,
    Execution,
    ExecutionDecisionLog,
    ExecutionLog,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    OutboxEvent,
    PendingOrder,
    User,
)
from lib_application.db.session import tenant_scope
from lib_application.services.database_authority import require_maintenance_database_role
from lib_application.services.deployment_owner import require_deployment_owner_id

_USER_ID_MAX_LENGTH = 50
_PROFILE_FIELDS = frozenset({"email", "full_name", "tz", "base_ccy"})
_REQUIRED_FIELDS = frozenset({"email", "tz", "base_ccy"})
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OwnerOnboardingError(ValueError):
    """A public owner-operation validation or conflict error."""

    def __init__(self, detail: str, *, status_code: int = 409) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _validate_profile(
    profile: Mapping[str, object], *, require_complete: bool = False
) -> dict[str, object]:
    values = dict(profile)
    if set(values) - _PROFILE_FIELDS:
        msg = "Unsupported owner profile fields"
        raise OwnerOnboardingError(msg, status_code=422)
    if require_complete and not values.keys() >= _REQUIRED_FIELDS:
        msg = "New owner requires explicit email, base_ccy and tz"
        raise OwnerOnboardingError(msg, status_code=422)
    for key, value in values.items():
        if key == "full_name" and value is None:
            continue
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            msg = f"Owner {key} must be a nonblank string"
            raise OwnerOnboardingError(msg, status_code=422)
        limit = 50 if key == "tz" else 255
        if len(value) > limit:
            msg = f"Owner {key} is too long"
            raise OwnerOnboardingError(msg, status_code=422)
        if key == "base_ccy" and not _CURRENCY.fullmatch(value):
            msg = "Owner currency must be an uppercase 3-10 character code"
            raise OwnerOnboardingError(msg, status_code=422)
        if key == "email" and not _EMAIL.fullmatch(value):
            msg = "Owner email is invalid"
            raise OwnerOnboardingError(msg, status_code=422)
        if key == "tz":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                msg = "Owner tz must be a known IANA timezone"
                raise OwnerOnboardingError(msg, status_code=422) from exc
    return values


def _profile(user: User) -> dict[str, Any]:
    return {
        key: getattr(user, key)
        for key in ("user_id", "email", "full_name", "tz", "base_ccy", "status")
    }


def get_owner_profile(session: Session) -> dict[str, Any]:
    owner_id = require_deployment_owner_id(session)
    with tenant_scope(session, user_id=owner_id):
        owner = session.get(User, owner_id, populate_existing=True)
        if owner is None:
            msg = "Deployment owner disappeared during profile lookup"
            raise OwnerOnboardingError(msg)
        return _profile(owner)


def _payload_owners(value: object) -> set[str]:
    owners: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"user_id", "entitlement_owner_user_id", "panel_entitlement_owner_user_id"}:
                if not isinstance(item, str) or not item.strip():
                    msg = (
                        "Pending outbox has ambiguous owner identity; reconcile it before adoption"
                    )
                    raise OwnerOnboardingError(msg)
                owners.add(item)
            elif isinstance(item, (dict, list)):
                owners.update(_payload_owners(item))
    elif isinstance(value, list):
        for item in value:
            owners.update(_payload_owners(item))
    return owners


def _payload_accounts(value: object) -> set[int]:
    accounts: set[int] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"account_id", "broker_account_id"}:
                if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
                    msg = (
                        "Pending outbox has ambiguous account identity; "
                        "reconcile it before adoption"
                    )
                    raise OwnerOnboardingError(msg)
                accounts.add(item)
            elif isinstance(item, (dict, list)):
                accounts.update(_payload_accounts(item))
    elif isinstance(value, list):
        for item in value:
            accounts.update(_payload_accounts(item))
    return accounts


def _require_adoption_quiescence(session: Session, owner_id: str) -> None:
    for event in session.scalars(select(OutboxEvent).where(OutboxEvent.status != "published")):
        if _payload_owners(event.payload) != {owner_id}:
            msg = (
                "Foreign or ambiguous pending outbox requires explicit disposition before adoption"
            )
            raise OwnerOnboardingError(msg)
        for account_id in _payload_accounts(event.payload):
            account = session.get(LinkedBrokerAccount, account_id)
            if account is None or account.user_id != owner_id:
                msg = (
                    "Pending outbox references a foreign or missing account; "
                    "reconcile it before adoption"
                )
                raise OwnerOnboardingError(msg)
    checks = (
        (
            ExecutionDecisionLog,
            ExecutionDecisionLog.status.in_(("pending", "executing")),
            "decisions",
        ),
        (
            ExecutionLog,
            ExecutionLog.status.in_(
                ("pending", "executing", "submitted", "partially_filled", "accepted", "working")
            ),
            "execution logs",
        ),
        (
            PendingOrder,
            PendingOrder.status.not_in(("filled", "cancelled", "expired", "rejected")),
            "pending orders",
        ),
        (
            AccountRebalancePlan,
            AccountRebalancePlan.status.in_(("pending", "running", "blocked")),
            "rebalances",
        ),
        (
            AccountExecutionGeneration,
            AccountExecutionGeneration.active_owner.is_not(None),
            "account writers",
        ),
    )
    for model, condition, label in checks:
        if (
            session.scalar(
                select(model.user_id).where(model.user_id != owner_id, condition).limit(1)
            )
            is not None
        ):
            msg = f"Foreign pending {label} require explicit disposition before adoption"
            raise OwnerOnboardingError(msg)
    active_orders = (
        select(Order.order_id)
        .join(OrderIntent, Order.intent_id == OrderIntent.intent_id)
        .where(
            OrderIntent.user_id != owner_id,
            Order.state.not_in(("filled", "canceled", "rejected")),
        )
    )
    if session.scalar(active_orders.limit(1)) is not None:
        msg = "Foreign nonterminal orders require reconciliation before adoption"
        raise OwnerOnboardingError(msg)
    orphan_intents = select(OrderIntent.intent_id).where(
        OrderIntent.user_id != owner_id,
        OrderIntent.status.in_(("created", "routed")),
        ~select(Order.order_id).where(Order.intent_id == OrderIntent.intent_id).exists(),
    )
    if session.scalar(orphan_intents.limit(1)) is not None:
        msg = "Foreign unresolved order intents require reconciliation before adoption"
        raise OwnerOnboardingError(msg)
    quantities: dict[tuple[int, int, str], Decimal] = defaultdict(Decimal)
    rows = session.execute(
        select(Execution, Order, OrderIntent, LinkedBrokerAccount, Instrument)
        .join(Order, Execution.order_id == Order.order_id)
        .join(OrderIntent, Order.intent_id == OrderIntent.intent_id)
        .outerjoin(LinkedBrokerAccount, Order.account_id == LinkedBrokerAccount.account_id)
        .outerjoin(Instrument, Execution.instr_id == Instrument.instr_id)
    )
    for fill, order, intent, account, instrument in rows:
        if intent.user_id == owner_id and account is not None and account.user_id == owner_id:
            continue
        if (
            account is None
            or instrument is None
            or account.user_id != intent.user_id
            or order.account_id != intent.account_id
            or order.broker_id != account.broker_id
            or intent.broker_environment not in {"paper", "live"}
            or account.environment != intent.broker_environment
            or intent.side not in {"BUY", "SELL"}
            or not isinstance(fill.qty, Decimal)
            or not fill.qty.is_finite()
            or fill.qty <= 0
        ):
            msg = "Ambiguous canonical execution identity requires reconciliation before adoption"
            raise OwnerOnboardingError(msg)
        if intent.method != "SPOT":
            msg = (
                "Foreign non-SPOT ledger history requires explicit contract-aware "
                "reconciliation and disposition before adoption"
            )
            raise OwnerOnboardingError(msg)
        key = (account.account_id, instrument.instr_id, account.environment)
        quantities[key] += fill.qty if intent.side == "BUY" else -fill.qty
    if any(quantity != 0 for quantity in quantities.values()):
        msg = (
            "Foreign canonical ledger exposure requires closure and reconciliation before adoption"
        )
        raise OwnerOnboardingError(msg)


def initialize_owner(
    session: Session,
    *,
    profile: Mapping[str, object],
    existing_user_id: str | None = None,
) -> dict[str, Any]:
    """Create or explicitly adopt one owner, without changing historical attribution."""
    values = _validate_profile(profile)
    if existing_user_id is not None and (
        not existing_user_id.strip() or len(existing_user_id) > _USER_ID_MAX_LENGTH
    ):
        msg = "existing-user-id is invalid"
        raise OwnerOnboardingError(msg, status_code=422)
    if session.get_bind().dialect.name == "postgresql":
        require_maintenance_database_role(session)
        # Refuse RLS-filtered preflight reads rather than silently missing foreign history.
        session.execute(text("SET LOCAL row_security = off"))
        session.execute(text("SET LOCAL lock_timeout = '5s'"))
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('deployment-owner-initialization', 0))"
            )
        )
        session.execute(
            text(
                "LOCK TABLE public.users, "
                "public.outbox_events, "
                "public.execution_decision_logs, "
                "public.execution_logs, "
                "public.pending_orders, "
                "public.account_rebalance_plans, "
                "public.account_execution_generations, "
                "public.order_intents, "
                "public.orders, "
                "public.executions, "
                "public.linked_broker_accounts, "
                "public.instruments IN SHARE ROW EXCLUSIVE MODE"
            )
        )
    owners = session.scalars(
        select(User).where(User.is_deployment_owner.is_(True)).with_for_update()
    ).all()
    if len(owners) > 1:
        msg = "Multiple deployment owners require explicit maintenance disposition"
        raise OwnerOnboardingError(msg)
    owner = owners[0] if owners else None
    if owner is not None and existing_user_id is not None and owner.user_id != existing_user_id:
        msg = "A different owner is already designated"
        raise OwnerOnboardingError(msg)
    if owner is None and existing_user_id is not None:
        owner = session.get(User, existing_user_id, with_for_update=True)
        if owner is None:
            msg = "Explicit existing-user-id does not exist"
            raise OwnerOnboardingError(msg, status_code=404)
    created = owner is None
    if owner is None:
        if session.scalar(select(User.user_id).limit(1)) is not None:
            msg = "Existing users require explicit --existing-user-id adoption"
            raise OwnerOnboardingError(msg)
        values = _validate_profile(values, require_complete=True)
        owner = User(**values, user_id=str(uuid4()), is_deployment_owner=False, status="active")
    else:
        if owner.status != "active":
            msg = "Only an active existing user may be designated; resolve its status explicitly"
            raise OwnerOnboardingError(msg)
        if any(getattr(owner, key) != value for key, value in values.items()):
            msg = (
                "Existing owner has different supplied profile values; use an expected-value patch"
            )
            raise OwnerOnboardingError(msg)
    _require_adoption_quiescence(session, owner.user_id)
    was_designated = bool(owner.is_deployment_owner)
    owner.is_deployment_owner = True
    session.add(owner)
    session.flush()
    if not was_designated:
        session.add(
            ApiAuditLog(
                user_id=owner.user_id,
                action="owner.init" if created else "owner.adopt",
                req={"fields": sorted(values)},
                resp={"user_id": owner.user_id},
                status="ok",
            )
        )
    return _profile(owner)


def apply_owner_patch(
    session: Session,
    *,
    expected: Mapping[str, object],
    changes: Mapping[str, object],
) -> dict[str, Any]:
    """Change supplied profile fields only, with a current-value conflict fence."""
    values = _validate_profile(changes)
    if not values or set(expected) - _PROFILE_FIELDS or not values.keys() <= expected.keys():
        msg = "Every changed owner field requires an allowed expected value"
        raise OwnerOnboardingError(msg, status_code=422)
    owner_id = require_deployment_owner_id(session)
    with tenant_scope(session, user_id=owner_id):
        owner = session.get(User, owner_id, with_for_update=True, populate_existing=True)
        if owner is None:
            msg = "Deployment owner disappeared during profile patch"
            raise OwnerOnboardingError(msg)
        for key, old_value in expected.items():
            current = getattr(owner, key)
            if current != old_value and (key not in values or current != values[key]):
                msg = f"Owner {key} does not match its expected value"
                raise OwnerOnboardingError(msg)
        if (
            "base_ccy" in values
            and values["base_ccy"] != owner.base_ccy
            and (
                session.scalar(
                    select(LinkedBrokerAccount.account_id)
                    .where(LinkedBrokerAccount.user_id == owner_id)
                    .limit(1)
                )
                is not None
            )
        ):
            msg = "Owner currency cannot change after account or execution authority exists"
            raise OwnerOnboardingError(msg)
        changed = {key: value for key, value in values.items() if getattr(owner, key) != value}
        for key, value in changed.items():
            setattr(owner, key, value)
        if changed:
            session.add(
                ApiAuditLog(
                    user_id=owner_id,
                    action="owner.patch",
                    req={"fields": sorted(changed)},
                    resp={"user_id": owner_id},
                    status="ok",
                )
            )
            session.flush()
        return _profile(owner)
