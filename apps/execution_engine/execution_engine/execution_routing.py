"""Execution route identity, environment, and settlement resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lib_application.services.instrument_resolution import (
    BrokerInstrumentIdentity,
    resolve_broker_instrument_identity,
)
from lib_common.logging import get_logger
from lib_common.time_utils import now_utc
from lib_infrastructure.brokers.capabilities import get_broker_capabilities
from lib_strategy.signals.signal import Signal

from .canonical_execution_store import (
    CanonicalExecutionStore,
    validate_broker_account_identity,
)
from .config import BrokerType

logger = get_logger(__name__)
_MAX_CURRENCY_CODE_LENGTH = 10


class CurrentAuthorityError(ValueError):
    """The durable control-plane state no longer authorizes broker I/O."""


@dataclass(frozen=True)
class CurrentExecutionAuthority:
    """Current credential generation after a complete authority revalidation."""

    credential_ref: str
    credential_version: str


def _require_current_instrument_id(instrument_id: int | str | None) -> int:
    """Normalize the persisted instrument identity required at the I/O boundary."""
    if isinstance(instrument_id, bool):
        msg = "Execution command has no durable instrument identity"
        raise CurrentAuthorityError(msg)
    try:
        current_instrument_id = int(instrument_id) if instrument_id is not None else 0
    except (TypeError, ValueError) as exc:
        msg = "Execution command has no durable instrument identity"
        raise CurrentAuthorityError(msg) from exc
    if current_instrument_id <= 0:
        msg = "Execution command has no durable instrument identity"
        raise CurrentAuthorityError(msg)
    return current_instrument_id


def _validate_current_binding_instrument_scope(
    session: Any,
    *,
    binding: Any,
    binding_id: int,
    instrument_id: int,
    enforce_allowlist: bool = True,
) -> None:
    """Require a current instrument and, normally, its binding allowlist scope."""
    from lib_application.db.models import Instrument  # noqa: PLC0415

    instrument = session.get(Instrument, instrument_id)
    if instrument is None:
        msg = f"Instrument {instrument_id} no longer exists"
        raise CurrentAuthorityError(msg)
    if not enforce_allowlist:
        return
    raw_instrument_scope = binding.instruments_allowed
    if raw_instrument_scope is None:
        return
    if not isinstance(raw_instrument_scope, (list, tuple)):
        msg = f"Binding {binding_id} has an invalid current instrument scope"
        raise CurrentAuthorityError(msg)
    current_scope = {str(item).strip() for item in raw_instrument_scope if str(item).strip()}
    if str(instrument.canonical) not in current_scope:
        msg = f"Binding {binding_id} no longer authorizes instrument {instrument.canonical}"
        raise CurrentAuthorityError(msg)


class ExecutionRouteResolver:
    """Resolve immutable routing facts before any broker submission."""

    def __init__(
        self,
        *,
        default_mode: str,
        session_factory: Any | None,
        canonical_execution_store: CanonicalExecutionStore | None,
    ) -> None:
        self._default_mode = default_mode
        self._session_factory = session_factory
        self._canonical_execution_store = canonical_execution_store

    def normalize_mode(
        self,
        mode_value: Any,
        trace_ctx: dict[str, Any] | None = None,
    ) -> str:
        """Normalize an engine mode to backtest, paper, or live.

        The configured default is selected by the caller only when the request
        omits a mode. An explicit malformed value is rejected because silently
        changing it can route an order into a different environment.
        """
        normalized = mode_value.strip().lower() if isinstance(mode_value, str) else ""
        if normalized in {"backtest", "paper", "live"}:
            return normalized
        msg = f"Unknown execution mode {mode_value!r}"
        logger.warning(msg, **(trace_ctx or {}))
        raise ValueError(msg)

    @staticmethod
    def resolve_environment(*, mode: str, profile: dict[str, Any]) -> str:
        """Resolve the broker environment from the immutable route snapshot."""
        route_snapshot = profile.get("_broker_route_snapshot") or {}
        for candidate in (
            route_snapshot.get("broker_environment"),
            profile.get("broker_environment"),
        ):
            normalized = str(candidate or "").strip().lower()
            if normalized in {"paper", "live"}:
                return normalized

        if "sandbox" in route_snapshot:
            return "paper" if bool(route_snapshot.get("sandbox")) else "live"
        if mode != "live":
            return "paper"
        return "paper" if bool(profile.get("sandbox")) else "live"

    def resolve_account_id(
        self,
        *,
        user_id: str,
        broker_type: BrokerType,
        environment: str,
        profile: dict[str, Any],
    ) -> int:
        """Resolve and validate one explicitly routed account without inference."""
        route = dict(profile.get("_broker_route_snapshot") or {})
        route_account_id = route.get("broker_account_id")
        profile_account_id = profile.get("broker_account_id")
        if (
            route_account_id is not None
            and profile_account_id is not None
            and str(route_account_id) != str(profile_account_id)
        ):
            msg = "Broker account identity differs between route snapshot and profile"
            raise ValueError(msg)
        raw_account_id = route_account_id if route_account_id is not None else profile_account_id
        if raw_account_id is None:
            msg = "Execution route requires an explicit broker_account_id"
            raise ValueError(msg)
        if isinstance(raw_account_id, bool):
            msg = "Execution route broker_account_id must be a positive integer"
            raise TypeError(msg)
        try:
            account_id = int(raw_account_id)
        except (TypeError, ValueError) as exc:
            msg = "Execution route broker_account_id must be a positive integer"
            raise ValueError(msg) from exc
        if account_id <= 0:
            msg = "Execution route broker_account_id must be a positive integer"
            raise ValueError(msg)

        route_broker = str(route.get("broker") or "").strip().lower()
        if route_broker and route_broker != broker_type.value:
            msg = (
                f"Broker account route selects {route_broker}, "
                f"but execution resolved {broker_type.value}"
            )
            raise ValueError(msg)
        route_environment = str(route.get("broker_environment") or "").strip().lower()
        if route_environment and route_environment != environment:
            msg = (
                f"Broker account route selects {route_environment}, "
                f"but execution resolved {environment}"
            )
            raise ValueError(msg)

        account_snapshot = dict((profile.get("accounts") or {}).get(str(account_id)) or {})
        if account_snapshot:
            snapshot_broker = str(account_snapshot.get("broker") or "").strip().lower()
            snapshot_environment = str(account_snapshot.get("environment") or "").strip().lower()
            if account_snapshot.get("status") != "connected":
                msg = f"Linked broker account {account_id} is not connected"
                raise ValueError(msg)
            if snapshot_broker and snapshot_broker != broker_type.value:
                msg = (
                    f"Linked broker account {account_id} belongs to {snapshot_broker}, "
                    f"not {broker_type.value}"
                )
                raise ValueError(msg)
            if snapshot_environment and snapshot_environment != environment:
                msg = (
                    f"Linked broker account {account_id} is {snapshot_environment}, "
                    f"not {environment}"
                )
                raise ValueError(msg)

        if self._session_factory is None:
            msg = "Broker account ownership validation requires database persistence"
            raise ValueError(msg)
        validate_broker_account_identity(
            self._session_factory,
            user_id=user_id,
            account_id=account_id,
            broker_code=broker_type.value,
            broker_environment=environment,
        )
        return account_id

    def resolve_settlement_currency(self, signal: Signal) -> str | None:
        """Resolve immutable instrument settlement currency for persisted orders."""
        if self._canonical_execution_store is None:
            return None
        instrument_id = self._instrument_id(signal)
        return self._canonical_execution_store.resolve_settlement_currency(instrument_id)

    def resolve_broker_instrument(
        self,
        *,
        user_id: str,
        account_id: int,
        broker_type: BrokerType,
        signal: Signal,
    ) -> BrokerInstrumentIdentity | None:
        """Resolve exact venue identity through the selected account's broker.

        Text-symbol brokers may continue without an explicit mapping. Adapters
        whose capability contract requires an opaque ID or type are blocked
        here, before credentials are loaded or any broker connection is made.
        """
        capabilities = get_broker_capabilities(broker_type.value)
        requires_id = bool(capabilities is not None and capabilities.requires_broker_instrument_id)
        requires_type = bool(
            capabilities is not None and capabilities.requires_broker_instrument_type
        )
        if self._session_factory is None:
            if requires_id or requires_type:
                msg = (
                    f"{broker_type.value} instrument identity resolution requires "
                    "database persistence"
                )
                raise ValueError(msg)
            return None

        instrument_id = self._instrument_id(signal)
        if instrument_id is None:
            if requires_id or requires_type:
                msg = f"{broker_type.value} execution requires a persisted instrument_id"
                raise ValueError(msg)
            return None

        from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415

        with self._session_factory() as session:
            account = session.get(LinkedBrokerAccount, account_id)
            if account is None or str(account.user_id) != str(user_id):
                msg = f"Linked broker account {account_id} is unavailable for user {user_id}"
                raise ValueError(msg)
            identity = resolve_broker_instrument_identity(
                session,
                instrument_id=instrument_id,
                broker_code=broker_type.value,
            )
            if identity is not None and int(account.broker_id) != identity.broker_id:
                msg = (
                    f"Instrument {instrument_id} mapping does not belong to linked "
                    f"broker account {account_id}"
                )
                raise ValueError(msg)

        if identity is None:
            if requires_id or requires_type:
                msg = f"Instrument {instrument_id} has no {broker_type.value} catalogue mapping"
                raise ValueError(msg)
            return None
        if requires_id and identity.broker_instrument_id is None:
            msg = f"Instrument {instrument_id} has no {broker_type.value} broker_instrument_id"
            raise ValueError(msg)
        if (
            identity.broker_instrument_id is not None
            and capabilities is not None
            and capabilities.broker_instrument_id_format == "positive_integer"
        ):
            venue_id = identity.broker_instrument_id
            if (
                not venue_id.isascii()
                or not venue_id.isdigit()
                or venue_id.startswith("0")
                or int(venue_id) <= 0
            ):
                msg = (
                    f"Instrument {instrument_id} has a noncanonical "
                    f"{broker_type.value} broker_instrument_id"
                )
                raise ValueError(msg)
        if requires_type and identity.broker_instrument_type is None:
            msg = f"Instrument {instrument_id} has no {broker_type.value} broker_instrument_type"
            raise ValueError(msg)
        return identity

    @staticmethod
    def _instrument_id(signal: Signal) -> int | None:
        if signal.instrument_id is None:
            return None
        if isinstance(signal.instrument_id, bool):
            msg = "Signal instrument_id must identify the persisted instrument"
            raise TypeError(msg)
        try:
            instrument_id = int(signal.instrument_id)
        except (TypeError, ValueError) as exc:
            msg = "Signal instrument_id must identify the persisted instrument"
            raise ValueError(msg) from exc
        if instrument_id <= 0:
            msg = "Signal instrument_id must identify the persisted instrument"
            raise ValueError(msg)
        return instrument_id

    def resolve_account_currency(
        self,
        *,
        user_id: str,
        account_id: int | None,
    ) -> str:
        """Resolve the database-authoritative reporting currency for an account."""
        if account_id is None:
            msg = "Broker account currency resolution requires broker_account_id"
            raise ValueError(msg)
        if self._session_factory is None:
            msg = "Broker account currency resolution requires database persistence"
            raise ValueError(msg)
        from lib_application.db.models import LinkedBrokerAccount  # noqa: PLC0415

        with self._session_factory() as session:
            account = session.get(LinkedBrokerAccount, account_id)
            if account is None or str(account.user_id) != str(user_id):
                msg = f"Linked broker account {account_id} is unavailable for user {user_id}"
                raise ValueError(msg)
            currency = str(account.base_ccy or "").strip().upper()
            if not currency or len(currency) > _MAX_CURRENCY_CODE_LENGTH or not currency.isalnum():
                msg = f"Linked broker account {account_id} has invalid base currency"
                raise ValueError(msg)
            return currency

    def validate_current_route(
        self,
        *,
        user_id: str,
        binding_id: int | None,
        strategy_id: str,
        account_id: int,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        instrument_id: int | str | None,
    ) -> CurrentExecutionAuthority:
        """Re-read the revocable route before read-only broker account I/O.

        This deliberately validates the active user, binding, instrument
        scope, account, environment, and credential generation without
        treating a read-only account refresh as an entry or exit request.
        Order submission must use :meth:`validate_current_authority` with its
        actual action immediately before broker I/O.
        """
        return self._validate_current_route(
            user_id=user_id,
            binding_id=binding_id,
            strategy_id=strategy_id,
            account_id=account_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            instrument_id=instrument_id,
            action=None,
            enforce_instrument_allowlist=True,
        )

    def validate_current_rebalance_account_route(
        self,
        *,
        user_id: str,
        binding_id: int | None,
        strategy_id: str,
        account_id: int,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        instrument_id: int | str | None,
    ) -> CurrentExecutionAuthority:
        """Authorize read-only account state needed to evaluate an incumbent.

        A symbol removed from a binding allowlist may still have strategy-owned
        exposure that must be measured before it can be reduced. This method
        does not authorize an order: it retains user, binding, strategy,
        account, instrument-existence, environment, and credential checks and
        deliberately omits only the allowlist membership test.
        """
        return self._validate_current_route(
            user_id=user_id,
            binding_id=binding_id,
            strategy_id=strategy_id,
            account_id=account_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            instrument_id=instrument_id,
            action=None,
            enforce_instrument_allowlist=False,
        )

    def validate_current_rebalance_reduction_authority(
        self,
        *,
        user_id: str,
        binding_id: int | None,
        strategy_id: str,
        account_id: int,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        action: str,
        instrument_id: int | str | None,
    ) -> CurrentExecutionAuthority:
        """Authorize a previously proven internal, strategy-owned reduction."""
        normalized_action = action.strip().lower()
        if normalized_action not in {"close", "flat", "close_spread"}:
            msg = "Rebalance allowlist exception requires exit execution semantics"
            raise CurrentAuthorityError(msg)
        return self._validate_current_route(
            user_id=user_id,
            binding_id=binding_id,
            strategy_id=strategy_id,
            account_id=account_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            instrument_id=instrument_id,
            action=action,
            enforce_instrument_allowlist=False,
        )

    def validate_current_authority(
        self,
        *,
        user_id: str,
        binding_id: int | None,
        strategy_id: str,
        account_id: int,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        action: str,
        instrument_id: int | str | None,
    ) -> CurrentExecutionAuthority:
        """Re-read route and action authority immediately before order I/O."""
        return self._validate_current_route(
            user_id=user_id,
            binding_id=binding_id,
            strategy_id=strategy_id,
            account_id=account_id,
            broker_type=broker_type,
            environment=environment,
            credential_ref=credential_ref,
            instrument_id=instrument_id,
            action=action,
            enforce_instrument_allowlist=True,
        )

    def _validate_current_route(
        self,
        *,
        user_id: str,
        binding_id: int | None,
        strategy_id: str,
        account_id: int,
        broker_type: BrokerType,
        environment: str,
        credential_ref: str,
        instrument_id: int | str | None,
        action: str | None,
        enforce_instrument_allowlist: bool,
    ) -> CurrentExecutionAuthority:
        if self._session_factory is None:
            msg = "Current execution authorization requires database persistence"
            raise CurrentAuthorityError(msg)
        if isinstance(binding_id, bool) or not isinstance(binding_id, int) or binding_id <= 0:
            msg = "Execution command has no durable binding identity"
            raise CurrentAuthorityError(msg)
        current_instrument_id = _require_current_instrument_id(instrument_id)

        from lib_application.db.models import (  # noqa: PLC0415
            Broker,
            BrokerCredential,
            LinkedBrokerAccount,
            User,
            UserStrategyBinding,
        )

        with self._session_factory() as session:
            user = session.get(User, user_id)
            if user is None or str(user.status) != "active":
                msg = f"User {user_id} is not currently active"
                raise CurrentAuthorityError(msg)

            binding = session.get(UserStrategyBinding, binding_id)
            if (
                binding is None
                or str(binding.user_id) != str(user_id)
                or int(binding.broker_account_id) != account_id
                or str(binding.strategy_id or "") != str(strategy_id)
                or not bool(binding.is_active)
            ):
                msg = f"Binding {binding_id} no longer authorizes this strategy/account route"
                raise CurrentAuthorityError(msg)

            _validate_current_binding_instrument_scope(
                session,
                binding=binding,
                binding_id=binding_id,
                instrument_id=current_instrument_id,
                enforce_allowlist=enforce_instrument_allowlist,
            )

            if action is not None:
                is_exit = action.strip().lower() in {"close", "flat", "close_spread"}
                if is_exit:
                    if not bool(binding.exits_enabled):
                        msg = f"Binding {binding_id} has exit authority disabled"
                        raise CurrentAuthorityError(msg)
                elif not (bool(binding.autopilot) and bool(binding.entries_enabled)):
                    msg = f"Binding {binding_id} has entry authority disabled"
                    raise CurrentAuthorityError(msg)

            account_row = (
                session.query(LinkedBrokerAccount, Broker)
                .join(Broker, Broker.broker_id == LinkedBrokerAccount.broker_id)
                .filter(LinkedBrokerAccount.account_id == account_id)
                .one_or_none()
            )
            if account_row is None:
                msg = f"Broker account {account_id} no longer exists"
                raise CurrentAuthorityError(msg)
            account, broker = account_row
            if (
                str(account.user_id) != str(user_id)
                or str(account.status) != "connected"
                or str(broker.code).strip().lower() != broker_type.value
                or str(account.environment).strip().lower() != environment
            ):
                msg = f"Broker account {account_id} no longer matches the authorized route"
                raise CurrentAuthorityError(msg)

            credentials = (
                session.query(BrokerCredential)
                .filter(BrokerCredential.account_id == account_id)
                .all()
            )
            now_naive = now_utc().replace(tzinfo=None)
            usable = [
                item
                for item in credentials
                if str(item.status) == "active"
                and (item.expires_at is None or item.expires_at.replace(tzinfo=None) > now_naive)
            ]
            if len(usable) > 1:
                msg = f"Broker account {account_id} has ambiguous active credentials"
                raise CurrentAuthorityError(msg)
            if usable:
                current_credential = usable[0]
                current_ref = str(current_credential.secret_ref or "").strip()
                if not current_ref or current_ref != credential_ref:
                    msg = f"Broker account {account_id} credential route changed"
                    raise CurrentAuthorityError(msg)
                rotation = current_credential.last_rotated_at
                version = (
                    rotation.replace(tzinfo=None).isoformat(timespec="microseconds")
                    if rotation is not None
                    else "unrotated"
                )
                return CurrentExecutionAuthority(
                    credential_ref=current_ref,
                    credential_version=f"{current_credential.cred_id}:{current_ref}:{version}",
                )

            synthetic_ref = f"{broker_type.value}-{environment}"
            if broker_type != BrokerType.PAPER or credentials or credential_ref != synthetic_ref:
                msg = f"Broker account {account_id} has no usable current credential"
                raise CurrentAuthorityError(msg)
            return CurrentExecutionAuthority(
                credential_ref=synthetic_ref,
                credential_version=f"local-paper:{account_id}:{environment}",
            )


__all__ = [
    "CurrentAuthorityError",
    "CurrentExecutionAuthority",
    "ExecutionRouteResolver",
]
