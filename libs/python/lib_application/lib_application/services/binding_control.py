"""Authority rules for a strategy binding: who may trade what, on which account.

These rules previously lived in the ``POST /bindings`` route body, which meant a
second caller could only reimplement them. They are here so the HTTP surface and
the owner UI share one implementation of the account lock, the broker check, the
instrument canonicalization, the overlap rule and the release gate.

Two rules are new, and both close gaps the route had:

* **Release.** A binding for a strategy with no active release could be created
  and switched on. The scoring engine then refused to persist its signals
  (``strategy_authority.require_active_strategy_version``), so it was inert --
  armed-looking and silent. Binding now requires the release the owner UI has
  always claimed it required.
* **Modes.** Three CHECK constraints make most combinations of the four
  authority flags illegal. :data:`MODES` is the subset that is always legal, so a
  caller choosing a mode cannot construct a row the database will reject.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from lib_application.db.models import (
    Broker,
    Instrument,
    LinkedBrokerAccount,
    Strategy,
    StrategyVersion,
    UserStrategyBinding,
)
from lib_application.services.control_audit import append_audit, changed_fields
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)


class BindingControlError(ValueError):
    """A refused binding change, carrying the status the HTTP layer should use."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True)
class BindingMode:
    """One legal combination of the four authority flags."""

    is_active: bool
    entries_enabled: bool
    exits_enabled: bool
    autopilot: bool

    def as_fields(self) -> dict[str, bool]:
        return {
            "is_active": self.is_active,
            "entries_enabled": self.entries_enabled,
            "exits_enabled": self.exits_enabled,
            "autopilot": self.autopilot,
        }


#: Every mode satisfies all three authority CHECK constraints by construction:
#: ``is_active OR (NOT entries AND NOT exits)``, ``NOT is_active OR strategy_id``
#: and ``autopilot OR NOT entries`` (``control_plane.py`` ``__table_args__``).
MODES: dict[str, BindingMode] = {
    "off": BindingMode(
        is_active=False, entries_enabled=False, exits_enabled=False, autopilot=False
    ),
    "close_only": BindingMode(
        is_active=True, entries_enabled=False, exits_enabled=True, autopilot=False
    ),
    "trading": BindingMode(
        is_active=True, entries_enabled=True, exits_enabled=True, autopilot=True
    ),
}
CUSTOM_MODE = "custom"

#: Numeric parameters an owner may edit, with the CHECK each one must satisfy.
#: ``exclusive`` marks a lower bound the constraint rejects at equality -- the
#: asymmetry is real: two of these refuse zero and two accept it.
NUMERIC_FIELDS: dict[str, tuple[Decimal, Decimal, bool]] = {
    "asset_score_threshold": (Decimal("0"), Decimal("1"), False),
    "max_position_pct": (Decimal("0"), Decimal("1"), True),
    "max_total_exposure_pct": (Decimal("0"), Decimal("1"), True),
    "max_daily_loss_pct": (Decimal("0"), Decimal("1"), False),
}
MAX_OPEN_POSITIONS = (1, 1000)

#: What a UI patch may address. Everything else on the row stays where the CLI
#: and the catalogue put it.
PATCHABLE: frozenset[str] = frozenset({"mode", *NUMERIC_FIELDS, "max_open_positions"})


def mode_of(row: Any) -> str:
    """Name the row's current mode, or ``custom`` for a combination we do not offer."""
    current = BindingMode(
        is_active=bool(row.is_active),
        entries_enabled=bool(row.entries_enabled),
        exits_enabled=bool(row.exits_enabled),
        autopilot=bool(row.autopilot),
    )
    for name, mode in MODES.items():
        if mode == current:
            return name
    return CUSTOM_MODE


def require_release(session: Session, strategy_id: str | None) -> None:
    """Refuse a strategy that maintenance has not released for trading.

    A binding without a strategy cannot be activated at all (the
    ``ck_binding_active_strategy_required`` CHECK), so the caller must resolve
    that before asking about release.
    """
    if strategy_id is None:
        msg = "a binding without a strategy cannot be activated"
        raise BindingControlError(msg, status_code=409)
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        msg = "strategy not found"
        raise BindingControlError(msg, status_code=404)
    if strategy.is_active is not True:
        msg = "strategy is not released for trading"
        raise BindingControlError(msg, status_code=409)
    released = (
        session.query(StrategyVersion.strat_ver_id)
        .filter(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.status == "active",
        )
        .first()
    )
    if released is None:
        msg = "strategy has no active release"
        raise BindingControlError(msg, status_code=409)


def canonical_instruments(session: Session, values: Sequence[Any] | None) -> list[str] | None:
    """Resolve a binding scope to canonical catalogue symbols; ``None`` stays ``None``."""
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
            raise BindingControlError(str(exc), status_code=422) from exc
        if instrument is None:
            msg = f"unknown instrument {token!r}; provision it in the catalogue first"
            raise BindingControlError(msg, status_code=422)
        symbol = str(instrument.canonical)
        if symbol not in canonical:
            canonical.append(symbol)
    return canonical


def scopes_overlap(
    session: Session, left: Sequence[Any] | None, right: Sequence[Any] | None
) -> bool:
    """Whether two binding scopes can authorize the same instrument.

    An empty scope means "everything", so it overlaps anything.
    """
    if not left or not right:
        return True
    left_scope = set(canonical_instruments(session, [str(item) for item in left]) or [])
    right_scope = set(canonical_instruments(session, [str(item) for item in right]) or [])
    return bool(left_scope & right_scope)


def reject_conflicting_active(
    session: Session,
    *,
    broker_account_id: int,
    strategy_id: str | None,
    is_active: bool,
    instruments: list[str] | None,
    existing_binding_id: int | None,
) -> None:
    """One active strategy per account and instrument scope.

    Only an activation can conflict: switching a binding off, or saving an
    inactive one, is always allowed.
    """
    if not is_active:
        return
    query = session.query(UserStrategyBinding).filter(
        UserStrategyBinding.broker_account_id == broker_account_id,
        UserStrategyBinding.is_active.is_(True),
    )
    if existing_binding_id is not None:
        query = query.filter(UserStrategyBinding.binding_id != existing_binding_id)
    for candidate in query.all():
        if candidate.strategy_id == strategy_id:
            continue
        if scopes_overlap(session, list(candidate.instruments_allowed or []) or None, instruments):
            msg = (
                "another active strategy already owns an overlapping instrument "
                f"scope on broker_account_id={broker_account_id}"
            )
            raise BindingControlError(msg, status_code=409)


def lock_account(session: Session, *, owner_id: str, account_id: int) -> LinkedBrokerAccount:
    """Serialize authority changes for one account before reading competing bindings.

    Under READ COMMITTED this separate row-locking statement is what makes the
    subsequent conflict query see a binding committed by a writer that held the
    lock first.
    """
    account = (
        session.query(LinkedBrokerAccount)
        .filter(
            LinkedBrokerAccount.account_id == account_id,
            LinkedBrokerAccount.user_id == owner_id,
            LinkedBrokerAccount.status == "connected",
        )
        .with_for_update()
        .one_or_none()
    )
    if account is None:
        msg = "broker_account_id is not a connected account owned by this user"
        raise BindingControlError(msg, status_code=400)
    return account


def require_broker_allowed(
    session: Session, *, account: LinkedBrokerAccount, allowed_brokers: Sequence[Any] | None
) -> None:
    """A binding may not name an account whose broker its allowlist excludes."""
    broker = session.get(Broker, account.broker_id)
    if broker is None:
        msg = "broker_account_id references an unknown broker"
        raise BindingControlError(msg, status_code=400)
    allowed = {str(code).strip().lower() for code in allowed_brokers or [] if code}
    if allowed and str(broker.code).lower() not in allowed:
        msg = "broker_account_id is outside allowed_brokers"
        raise BindingControlError(msg, status_code=400)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        msg = f"{field} must be a number"
        raise BindingControlError(msg, status_code=422) from exc


def validate_numeric(field: str, value: Any) -> Decimal | int:
    """Check one parameter against the CHECK constraint it must satisfy."""
    if field == "max_open_positions":
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            msg = "max_open_positions must be a whole number"
            raise BindingControlError(msg, status_code=422) from exc
        low, high = MAX_OPEN_POSITIONS
        if not low <= count <= high:
            msg = f"max_open_positions must be between {low} and {high}"
            raise BindingControlError(msg, status_code=422)
        return count
    low_bound, high_bound, exclusive = NUMERIC_FIELDS[field]
    number = _decimal(value, field)
    too_low = number <= low_bound if exclusive else number < low_bound
    if too_low or number > high_bound:
        edge = "greater than" if exclusive else "at least"
        msg = f"{field} must be {edge} {low_bound} and at most {high_bound}"
        raise BindingControlError(msg, status_code=422)
    return number


def _fence(expected: Mapping[str, Any], changes: Mapping[str, Any]) -> None:
    """Every changed field must carry the value the caller believed it had.

    This is the account contract (exact key equality), not the owner contract
    (subset), chosen because a control form shows exactly what it is changing.
    """
    if not changes:
        msg = "no changes supplied"
        raise BindingControlError(msg, status_code=422)
    if set(changes) != set(expected):
        msg = "every changed field requires an expected current value"
        raise BindingControlError(msg, status_code=422)
    unsupported = sorted(set(changes) - PATCHABLE)
    if unsupported:
        msg = f"unsupported fields: {', '.join(unsupported)}"
        raise BindingControlError(msg, status_code=422)


def _current(row: Any, field: str) -> Any:
    if field == "mode":
        return mode_of(row)
    if field == "max_open_positions":
        return int(getattr(row, field))
    value = getattr(row, field)
    return None if value is None else Decimal(str(value))


def _matches(current: Any, supplied: Any) -> bool:
    if isinstance(current, Decimal):
        if supplied is None:
            return False
        try:
            return current == Decimal(str(supplied))
        except (InvalidOperation, ValueError):
            return False
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return current == int(supplied)
        except (TypeError, ValueError):
            return False
    return bool(current == supplied)


def load_binding(session: Session, *, owner_id: str, binding_id: int) -> UserStrategyBinding:
    row = (
        session.query(UserStrategyBinding)
        .filter(
            UserStrategyBinding.binding_id == binding_id,
            UserStrategyBinding.user_id == owner_id,
        )
        .one_or_none()
    )
    if row is None:
        msg = "binding not found"
        raise BindingControlError(msg, status_code=404)
    return row


def create_binding(
    session: Session,
    *,
    owner_id: str,
    strategy_id: str,
    broker_account_id: int,
    mode: str,
) -> UserStrategyBinding:
    """Bind a released strategy to one of the owner's connected accounts."""
    if mode not in MODES:
        msg = f"unsupported mode: {mode}"
        raise BindingControlError(msg, status_code=422)
    account = lock_account(session, owner_id=owner_id, account_id=broker_account_id)
    require_broker_allowed(session, account=account, allowed_brokers=None)
    flags = MODES[mode]
    # Only authority needs a release. Setting a strategy up while it is still
    # unreleased is a reasonable thing to do; it simply cannot be switched on.
    if flags.is_active:
        require_release(session, strategy_id)
    existing = (
        session.query(UserStrategyBinding)
        .filter(
            UserStrategyBinding.user_id == owner_id,
            UserStrategyBinding.strategy_id == strategy_id,
            UserStrategyBinding.broker_account_id == broker_account_id,
        )
        .one_or_none()
    )
    if existing is not None:
        msg = "this strategy is already bound to that account; change it instead"
        raise BindingControlError(msg, status_code=409)
    reject_conflicting_active(
        session,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        is_active=flags.is_active,
        instruments=None,
        existing_binding_id=None,
    )
    row = UserStrategyBinding(
        user_id=owner_id,
        strategy_id=strategy_id,
        broker_account_id=broker_account_id,
        **flags.as_fields(),
    )
    session.add(row)
    session.flush()
    append_audit(
        session,
        user_id=owner_id,
        account_id=broker_account_id,
        action="binding.create",
        request_payload={**changed_fields(["mode"]), "mode": mode},
        response_payload={"binding_id": int(row.binding_id)},
    )
    return row


def patch_binding(
    session: Session,
    *,
    owner_id: str,
    binding_id: int,
    expected: Mapping[str, Any],
    changes: Mapping[str, Any],
) -> UserStrategyBinding:
    """Change only the supplied fields, and only if they still hold their expected value."""
    _fence(expected, changes)
    row = load_binding(session, owner_id=owner_id, binding_id=binding_id)
    account = lock_account(session, owner_id=owner_id, account_id=int(row.broker_account_id))
    require_broker_allowed(session, account=account, allowed_brokers=row.allowed_brokers)
    for key, supplied in expected.items():
        current = _current(row, key)
        if not _matches(current, supplied) and not _matches(current, changes[key]):
            msg = f"stale expected value for {key}"
            raise BindingControlError(msg, status_code=409)
    mode = changes.get("mode")
    if mode is not None:
        if mode not in MODES:
            msg = f"unsupported mode: {mode}"
            raise BindingControlError(msg, status_code=422)
        flags = MODES[mode]
        if flags.is_active:
            require_release(session, row.strategy_id)
            reject_conflicting_active(
                session,
                broker_account_id=int(row.broker_account_id),
                strategy_id=row.strategy_id,
                is_active=True,
                instruments=list(row.instruments_allowed or []) or None,
                existing_binding_id=int(row.binding_id),
            )
        for field, value in flags.as_fields().items():
            setattr(row, field, value)
    for key, value in changes.items():
        if key == "mode":
            continue
        setattr(row, key, validate_numeric(key, value))
    session.flush()
    append_audit(
        session,
        user_id=owner_id,
        account_id=int(row.broker_account_id),
        action="binding.patch",
        request_payload={
            **changed_fields(sorted(changes)),
            **({"mode": mode} if mode is not None else {}),
        },
        response_payload={"binding_id": int(row.binding_id)},
    )
    return row


def deactivate_binding(session: Session, *, owner_id: str, binding_id: int) -> dict[str, Any]:
    """Switch a binding off. The backend role cannot delete, and history is kept."""
    row = load_binding(session, owner_id=owner_id, binding_id=binding_id)
    for field, value in MODES["off"].as_fields().items():
        setattr(row, field, value)
    append_audit(
        session,
        user_id=owner_id,
        account_id=int(row.broker_account_id),
        action="binding.deactivate",
        request_payload={"binding_id": binding_id},
        response_payload={"is_active": False},
    )
    return {
        "binding_id": binding_id,
        "is_active": False,
        "entries_enabled": False,
        "exits_enabled": False,
    }


__all__ = [
    "CUSTOM_MODE",
    "MAX_OPEN_POSITIONS",
    "MODES",
    "NUMERIC_FIELDS",
    "PATCHABLE",
    "BindingControlError",
    "BindingMode",
    "canonical_instruments",
    "create_binding",
    "deactivate_binding",
    "load_binding",
    "lock_account",
    "mode_of",
    "patch_binding",
    "reject_conflicting_active",
    "require_broker_allowed",
    "require_release",
    "scopes_overlap",
    "validate_numeric",
]
