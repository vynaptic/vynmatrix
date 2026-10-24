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

A UI binding is scoped to the universe its strategy's release declares
(:func:`declared_universe`) and to the asset class its strategy declares
(:func:`declared_asset_classes`). A binding with no instrument list covers every
instrument in its asset classes (:class:`BindingScope`), so an equity strategy
with a membership universe can share an account with crypto strategies. The
owner may later patch the scope to another non-empty list, so two strategies
whose universes overlap can still split one account between them. A patch may
not drop an instrument the strategy still holds on that account
(:func:`reject_stranded_positions`): scoring routes signals by the scope, so the
position's close signals would no longer reach the binding.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Broker,
    Execution,
    Instrument,
    LinkedBrokerAccount,
    Order,
    OrderIntent,
    Strategy,
    StrategyVersion,
    UserStrategyBinding,
)
from lib_application.services.control_audit import append_audit, changed_fields
from lib_application.services.instrument_resolution import (
    InstrumentResolutionError,
    resolve_instrument,
)
from lib_common.asset_classes import CANONICAL_ASSET_CLASSES, normalize_asset_class


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

#: The binding's instrument scope. A patch may set it to a non-empty list only;
#: widening a binding to every instrument stays a CLI decision.
SCOPE_FIELD = "instruments_allowed"

#: What a UI patch may address. Everything else on the row stays where the CLI
#: and the catalogue put it.
PATCHABLE: frozenset[str] = frozenset({"mode", *NUMERIC_FIELDS, "max_open_positions", SCOPE_FIELD})


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


@dataclass(frozen=True)
class TradingState:
    """How far an installation is from trading, in the bind gate's own terms."""

    released: int
    """Strategies :func:`require_release` would accept."""
    bound: int
    """The owner's bindings that name a strategy."""
    switched_on: int
    """Those bindings that are active: close only, trading, or a custom active mix."""


def trading_state(session: Session, owner_id: str) -> TradingState:
    """Count what is released, bound and switched on for the deployment owner.

    "Released" is the condition :func:`require_release` applies -- the strategy
    row is active *and* one of its versions is active -- so a summary never
    calls a strategy released that the bind gate would refuse.
    """
    released = session.scalar(
        select(func.count(func.distinct(Strategy.strategy_id)))
        .join(StrategyVersion, StrategyVersion.strategy_id == Strategy.strategy_id)
        .where(Strategy.is_active.is_(True), StrategyVersion.status == "active")
    )
    rows = session.execute(
        select(UserStrategyBinding.is_active, func.count(UserStrategyBinding.binding_id))
        .where(
            UserStrategyBinding.user_id == owner_id,
            UserStrategyBinding.strategy_id.is_not(None),
        )
        .group_by(UserStrategyBinding.is_active)
    ).all()
    by_state = {bool(active): int(count) for active, count in rows}
    return TradingState(
        released=int(released or 0),
        bound=sum(by_state.values()),
        switched_on=by_state.get(True, 0),
    )


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


def declared_universe(session: Session, strategy_id: str) -> list[str] | None:
    """The instruments a strategy's release says it trades, as catalogue symbols.

    A binding made from the owner UI takes this as its scope, so two strategies
    with disjoint universes can share an account without either one claiming
    every instrument. The active release is read; a strategy bound before it is
    released falls back to its newest version.

    ``None`` -- no list -- is returned only where the release names no fixed
    list: a membership universe such as a point-in-time index
    (``universe_contract``), or a version with no ``universe`` at all. Such a
    binding covers every instrument in its asset classes
    (:func:`declared_asset_classes`), so it overlaps only strategies that can
    trade an instrument of the same class.
    """
    version = session.scalar(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(
            case((StrategyVersion.status == "active", 0), else_=1),
            StrategyVersion.strat_ver_id.desc(),
        )
        .limit(1)
    )
    params = version.default_params if version is not None else None
    if not isinstance(params, Mapping) or params.get("universe_contract"):
        return None
    raw = params.get("universe")
    if raw is None:
        return None
    if isinstance(raw, str):
        tokens = [token.strip() for token in raw.split(",") if token.strip()]
    elif isinstance(raw, (list, tuple)):
        tokens = [str(token).strip() for token in raw if str(token).strip()]
    else:
        msg = f"strategy {strategy_id} declares a universe that is not a list of instruments"
        raise BindingControlError(msg, status_code=422)
    if not tokens:
        return None
    try:
        return canonical_instruments(session, tokens)
    except BindingControlError as exc:
        msg = f"strategy {strategy_id} trades an instrument the catalogue cannot resolve: {exc}"
        raise BindingControlError(msg, status_code=exc.status_code) from exc


def declared_asset_classes(session: Session, strategy_id: str) -> list[str]:
    """The asset class a strategy declares, as a binding's ``asset_classes_allowed``.

    The catalogue row's ``asset_class`` is taken as declared: an ETF strategy is
    ``etf`` and a cash-index strategy is ``index``, never ``equity``. A binding
    with no instrument list is bounded by this value alone, so a strategy that
    declares none is refused rather than given a default.
    """
    strategy = session.get(Strategy, strategy_id)
    if strategy is None:
        msg = "strategy not found"
        raise BindingControlError(msg, status_code=404)
    declared = str(strategy.asset_class or "").strip()
    if not declared:
        msg = (
            f"strategy {strategy_id} declares no asset class; register its release "
            "through the catalogue first"
        )
        raise BindingControlError(msg, status_code=422)
    try:
        return [normalize_asset_class(declared, field_name="strategy asset_class")]
    except ValueError as exc:
        msg = f"strategy {strategy_id} declares an unsupported asset class: {exc}"
        raise BindingControlError(msg, status_code=422) from exc


@dataclass(frozen=True)
class BindingScope:
    """What one binding can trade, as scoring and execution enforce it.

    ``instruments`` is a fixed list of catalogue symbols. When it is set it is
    the whole scope: scoring and execution do not consult the asset classes.
    When it is ``None`` the binding covers every catalogued instrument whose
    asset class is in ``asset_classes``. ``asset_classes`` is ``None`` when it
    does not bound anything -- empty, or holding a value that is not a
    canonical asset class -- and then the scope is every instrument.

    The ``vm_binding_scopes_overlap`` SQL function (migration
    ``0110_asset_class_binding_scope``) applies the same reading.
    """

    instruments: tuple[str, ...] | None
    asset_classes: frozenset[str] | None

    @classmethod
    def of(
        cls, instruments: Sequence[Any] | None, asset_classes: Sequence[Any] | None
    ) -> BindingScope:
        """Read a binding's two stored columns; an empty instrument list is no list."""
        listed = tuple(str(item) for item in instruments or [])
        classes = frozenset(str(value) for value in asset_classes or [])
        bounded = bool(classes) and classes <= CANONICAL_ASSET_CLASSES
        return cls(listed or None, classes if bounded else None)

    def describe(self) -> str:
        """The scope in the owner's words, for a refusal."""
        if self.instruments is not None:
            return ", ".join(self.instruments)
        if self.asset_classes is None:
            return "every instrument"
        return f"every {' or '.join(sorted(self.asset_classes))} instrument"


def _classes_of(session: Session, symbols: Sequence[str]) -> dict[str, str]:
    """Each canonical symbol's catalogue asset class."""
    rows = session.execute(
        select(Instrument.canonical, Instrument.asset_class).where(
            Instrument.canonical.in_(list(symbols))
        )
    ).all()
    return {str(canonical): str(asset_class) for canonical, asset_class in rows}


def shared_instruments(
    session: Session, left: BindingScope, right: BindingScope
) -> list[str] | None:
    """The instruments both scopes authorize, sorted.

    ``None`` means the two share an open-ended set: neither has an instrument
    list and their asset classes meet (or one is not bounded by class). An
    instrument has exactly one catalogue asset class, so a list-free scope
    limited to ``equity`` shares nothing with one limited to ``crypto``, or
    with a list of crypto instruments.
    """
    if left.instruments is not None and right.instruments is not None:
        left_scope = set(canonical_instruments(session, list(left.instruments)) or [])
        right_scope = set(canonical_instruments(session, list(right.instruments)) or [])
        return sorted(left_scope & right_scope)
    if left.instruments is None and right.instruments is None:
        if left.asset_classes is None or right.asset_classes is None:
            return None
        return None if left.asset_classes & right.asset_classes else []
    listed, open_ended = (left, right) if left.instruments is not None else (right, left)
    symbols = canonical_instruments(session, list(listed.instruments or ())) or []
    if open_ended.asset_classes is None:
        return sorted(symbols)
    classes = _classes_of(session, symbols)
    return sorted(symbol for symbol in symbols if classes.get(symbol) in open_ended.asset_classes)


def scopes_overlap(session: Session, left: BindingScope, right: BindingScope) -> bool:
    """Whether two binding scopes can authorize the same instrument."""
    shared = shared_instruments(session, left, right)
    return shared is None or bool(shared)


def reject_conflicting_active(
    session: Session,
    *,
    broker_account_id: int,
    strategy_id: str | None,
    is_active: bool,
    instruments: list[str] | None,
    asset_classes: Sequence[str],
    existing_binding_id: int | None,
) -> None:
    """One active strategy per account and instrument.

    Only an activation can conflict: switching a binding off, or saving an
    inactive one, is always allowed. ``instruments`` and ``asset_classes`` are
    the binding's scope (:class:`BindingScope`). The refusal names the strategy
    in the way and what the two share, because that is what the owner has to
    change.
    """
    if not is_active:
        return
    scope = BindingScope.of(instruments, asset_classes)
    query = session.query(UserStrategyBinding).filter(
        UserStrategyBinding.broker_account_id == broker_account_id,
        UserStrategyBinding.is_active.is_(True),
    )
    if existing_binding_id is not None:
        query = query.filter(UserStrategyBinding.binding_id != existing_binding_id)
    for candidate in query.order_by(UserStrategyBinding.binding_id).all():
        if candidate.strategy_id == strategy_id:
            continue
        held = BindingScope.of(candidate.instruments_allowed, candidate.asset_classes_allowed)
        shared = shared_instruments(session, held, scope)
        if shared is not None and not shared:
            continue
        if shared:
            overlap = ", ".join(shared)
        else:
            overlap = held.describe()
            if scope.describe() != overlap:
                overlap = f"{overlap}, and this binding covers {scope.describe()}"
        msg = (
            f"overlapping instrument scope on broker_account_id={broker_account_id}: "
            f"{candidate.strategy_id} is already switched on for {overlap}. Two active "
            "strategies never share an instrument on one account; switch "
            f"{candidate.strategy_id} off, narrow one of the two scopes so they share "
            "none, or bind this strategy to another account"
        )
        raise BindingControlError(msg, status_code=409)


def held_instruments(
    session: Session, *, broker_account_id: int, strategy_id: str | None
) -> list[str]:
    """Instruments a strategy still holds on an account, as catalogue symbols, sorted.

    The source is the canonical execution ledger: exact ``executions`` joined to
    the ``orders`` and ``order_intents`` that own them. The signed sum of a
    strategy's fills is the net quantity its FIFO replay would leave, and it
    needs no price. The ``positions`` table is not used: it is a projection, and
    it is kept per account, so it cannot say which strategy holds a position.
    Every column read here is inside the backend's ``0107`` read grant.
    """
    if strategy_id is None:
        return []
    signed = case((OrderIntent.side == "BUY", Execution.qty), else_=-Execution.qty)
    rows = session.execute(
        select(Instrument.canonical)
        .select_from(Execution)
        .join(Order, Order.order_id == Execution.order_id)
        .join(OrderIntent, OrderIntent.intent_id == Order.intent_id)
        .join(Instrument, Instrument.instr_id == Execution.instr_id)
        .where(
            Order.account_id == broker_account_id,
            OrderIntent.strategy_id == strategy_id,
        )
        .group_by(Execution.instr_id, Instrument.canonical)
        .having(func.sum(signed) != 0)
    ).scalars()
    return sorted({str(symbol) for symbol in rows})


def reject_stranded_positions(
    session: Session,
    *,
    broker_account_id: int,
    strategy_id: str | None,
    current: frozenset[str] | None,
    proposed: Sequence[str],
) -> None:
    """Refuse a new scope that drops an instrument the strategy still holds.

    Scoring routes a strategy's signals only to bindings whose scope names the
    instrument, and execution re-checks the scope before every order, exits
    included. Dropping a held instrument would leave its position with no way
    to close. ``current`` is the scope as stored (``None`` is every
    instrument); a holding that the stored scope already excludes is not
    dropped by this change, so it does not block it.
    """
    kept = set(proposed)
    stranded = [
        symbol
        for symbol in held_instruments(
            session, broker_account_id=broker_account_id, strategy_id=strategy_id
        )
        if symbol not in kept and (current is None or symbol in current)
    ]
    if stranded:
        held = ", ".join(stranded)
        msg = (
            f"{strategy_id} still holds an open position in {held} on "
            f"broker_account_id={broker_account_id}. Removing it from the scope would stop "
            "that position's close signals from reaching this binding; keep it in the "
            "scope until the position is closed"
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


def _scope(session: Session, value: Any) -> frozenset[str] | None:
    """A stored or expected scope as a set of canonical symbols; ``None`` is everything.

    An empty list also means every instrument, the meaning scoring and the
    overlap rule already give it.
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        msg = f"{SCOPE_FIELD} must be a list of instruments or null"
        raise BindingControlError(msg, status_code=422)
    if not value:
        return None
    return frozenset(canonical_instruments(session, list(value)) or [])


def _requested_scope(session: Session, value: Any) -> list[str]:
    """The new scope: a non-empty list, every entry resolved through the catalogue."""
    if not isinstance(value, (list, tuple)) or not value:
        msg = (
            f"{SCOPE_FIELD} must be a non-empty list of instruments; "
            "a binding cannot be widened to every instrument from here"
        )
        raise BindingControlError(msg, status_code=422)
    if any(not str(item).strip() for item in value):
        msg = f"{SCOPE_FIELD} names a blank instrument"
        raise BindingControlError(msg, status_code=422)
    return canonical_instruments(session, list(value)) or []


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
    """Bind a strategy to one of the owner's connected accounts, scoped to what it declares.

    The binding takes the strategy's universe as ``instruments_allowed`` and its
    asset class as ``asset_classes_allowed``. Any mode but ``off`` needs a
    release.
    """
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
    # The scope is the strategy's own declared universe and asset class, not
    # "everything": an unbounded binding overlaps every other strategy, so it
    # would leave room for exactly one active strategy per account. A
    # membership universe has no list; its asset class still bounds it.
    asset_classes = declared_asset_classes(session, strategy_id)
    instruments = declared_universe(session, strategy_id)
    reject_conflicting_active(
        session,
        broker_account_id=broker_account_id,
        strategy_id=strategy_id,
        is_active=flags.is_active,
        instruments=instruments,
        asset_classes=asset_classes,
        existing_binding_id=None,
    )
    row = UserStrategyBinding(
        user_id=owner_id,
        strategy_id=strategy_id,
        broker_account_id=broker_account_id,
        instruments_allowed=instruments,
        asset_classes_allowed=asset_classes,
        **flags.as_fields(),
    )
    session.add(row)
    session.flush()
    fields = ["asset_classes_allowed", "mode"]
    if instruments is not None:
        fields.insert(1, "instruments_allowed")
    append_audit(
        session,
        user_id=owner_id,
        account_id=broker_account_id,
        action="binding.create",
        request_payload={**changed_fields(fields), "mode": mode},
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
    """Change only the supplied fields, and only if they still hold their expected value.

    A scope change is compared as a set of canonical symbols, so order and
    broker aliases do not make a current value look stale. Whenever the binding
    is or becomes active, the overlap rule runs against the scope it will have
    after this patch.
    """
    _fence(expected, changes)
    row = load_binding(session, owner_id=owner_id, binding_id=binding_id)
    account_id = int(row.broker_account_id)
    account = lock_account(session, owner_id=owner_id, account_id=account_id)
    require_broker_allowed(session, account=account, allowed_brokers=row.allowed_brokers)
    mode = changes.get("mode")
    if mode is not None and mode not in MODES:
        msg = f"unsupported mode: {mode}"
        raise BindingControlError(msg, status_code=422)
    scope = _requested_scope(session, changes[SCOPE_FIELD]) if SCOPE_FIELD in changes else None
    stored_scope = _scope(session, row.instruments_allowed) if scope is not None else None
    for key, supplied in expected.items():
        if key == SCOPE_FIELD:
            fresh = stored_scope in (_scope(session, supplied), frozenset(scope or []))
        else:
            current = _current(row, key)
            fresh = _matches(current, supplied) or _matches(current, changes[key])
        if not fresh:
            msg = f"stale expected value for {key}"
            raise BindingControlError(msg, status_code=409)
    if scope is not None:
        reject_stranded_positions(
            session,
            broker_account_id=account_id,
            strategy_id=row.strategy_id,
            current=stored_scope,
            proposed=scope,
        )
    flags = MODES[mode] if mode is not None else None
    if flags is not None and flags.is_active:
        require_release(session, row.strategy_id)
    active_after = flags.is_active if flags is not None else bool(row.is_active)
    if active_after and (scope is not None or flags is not None):
        scope_after = scope if scope is not None else list(row.instruments_allowed or []) or None
        reject_conflicting_active(
            session,
            broker_account_id=account_id,
            strategy_id=row.strategy_id,
            is_active=True,
            instruments=scope_after,
            asset_classes=list(row.asset_classes_allowed or []),
            existing_binding_id=int(row.binding_id),
        )
    if flags is not None:
        for field, value in flags.as_fields().items():
            setattr(row, field, value)
    if scope is not None:
        row.instruments_allowed = scope
    for key, value in changes.items():
        if key in {"mode", SCOPE_FIELD}:
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
    "SCOPE_FIELD",
    "BindingControlError",
    "BindingMode",
    "BindingScope",
    "TradingState",
    "canonical_instruments",
    "create_binding",
    "deactivate_binding",
    "declared_asset_classes",
    "declared_universe",
    "held_instruments",
    "load_binding",
    "lock_account",
    "mode_of",
    "patch_binding",
    "reject_conflicting_active",
    "reject_stranded_positions",
    "require_broker_allowed",
    "require_release",
    "scopes_overlap",
    "shared_instruments",
    "trading_state",
    "validate_numeric",
]
