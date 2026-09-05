"""DB-backed providers for user profiles and strategy configs."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from lib_application.db import models as app_models
from lib_common.asset_classes import normalize_asset_class
from lib_common.config_validation import normalize_broker_code
from lib_common.logging import get_logger
from lib_common.time_utils import now_utc

from .binding_utils import resolve_execution_mode

logger = get_logger(__name__)

# Type alias for session factory
SessionFactory = Callable[[], AbstractContextManager[Session]]

# A binding allow-list with exactly one broker pins (steers) routing to that
# broker instead of acting as a veto-only filter (D3).
_SINGLE_BROKER_STEER_COUNT = 1

# ``UserStrategyConfig.parameters`` is user-controlled JSON. Only explicitly
# reviewed execution-safety switches may cross the scoring -> execution trust
# boundary. Routing, sizing, broker selection, and risk limits remain derived
# from the active binding below; secrets and arbitrary top-level keys are never
# forwarded from this JSON blob.
_SAFE_USER_STRATEGY_BOOLEAN_PARAMETERS = frozenset(
    {
        "require_explicit_scoring_inputs",
        "require_stop_loss",
    }
)


def _normalize_broker_value(value: Any) -> str | None:
    if not value:
        return None
    return str(normalize_broker_code(value))


def _credential_is_usable(cred: Any) -> bool:
    """A credential is usable only if active AND not past its expiry (M-10).

    An expired-but-still-'active' credential must NOT be handed to a broker — it
    would fail at auth time or, worse, be silently used. ``expires_at`` is stored
    tz-naive (UTC); a null expiry means non-expiring.
    """
    if getattr(cred, "status", None) != "active":
        return False
    expires_at = getattr(cred, "expires_at", None)
    if expires_at is None:
        return True
    now_naive = now_utc().replace(tzinfo=None)
    exp = expires_at.replace(tzinfo=None) if getattr(expires_at, "tzinfo", None) else expires_at
    return bool(exp > now_naive)


def _active_credential_ref(account: Any) -> str | None:
    """Return the secret_ref of the account's first usable broker credential.

    The per-user credential pointer is what lets execution load *this* user's
    broker keys (instead of a shared default), so it must be carried into the
    profile. Returns None when the account has no usable (active + unexpired)
    credential.
    """
    credentials = getattr(account, "credentials", None) or []
    usable = next((cred for cred in credentials if _credential_is_usable(cred)), None)
    secret_ref = getattr(usable, "secret_ref", None) if usable is not None else None
    return str(secret_ref) if secret_ref else None


def _broker_capabilities(broker: Any) -> dict[str, Any]:
    caps = getattr(broker, "capabilities", None) or {}
    return dict(caps) if isinstance(caps, dict) else {}


def _supports_options(caps: dict[str, Any]) -> bool:
    return bool(
        caps.get("supports_options") or caps.get("options") or caps.get("options_supported")
    )


def _asset_classes(caps: dict[str, Any]) -> list[str]:
    raw = caps.get("asset_classes") or caps.get("assets") or caps.get("asset_class") or []
    if isinstance(raw, str):
        raw = [raw] if raw.strip() else []
    return list(
        dict.fromkeys(
            normalize_asset_class(str(item), field_name="broker capability asset class")
            for item in raw
            if str(item).strip()
        )
    )


def _select_primary_broker(
    accounts: list[tuple[Any, Any]],
    *,
    supports_options: bool,
    asset_class: str | None = None,
) -> str | None:
    target_asset = (
        normalize_asset_class(asset_class, field_name="broker selection asset class")
        if asset_class
        else None
    )
    for _account, broker in accounts:
        caps = _broker_capabilities(broker)
        if supports_options and not _supports_options(caps):
            continue
        if target_asset:
            allowed_assets = _asset_classes(caps)
            if allowed_assets and target_asset not in allowed_assets:
                continue
        code = _normalize_broker_value(getattr(broker, "code", None))
        if code:
            return code
    return None


class DBProfileProvider:
    """Fetch user profile from DB."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def __call__(self, user_id: str) -> dict[str, Any]:
        try:
            with self._session_factory() as s:
                return self._fetch_app_profile(s, user_id)
        except (SQLAlchemyError, OSError):
            # DB / connection-level failures only — programming errors propagate.
            logger.exception("Failed to fetch user profile for %s", user_id)
            return {}

    def _fetch_app_profile(self, session: Session, user_id: str) -> dict[str, Any]:
        user = session.get(app_models.User, user_id)
        if not user:
            return {}

        profile: dict[str, Any] = {
            "user_id": user.user_id,
            "email": user.email,
            "name": user.full_name,
            "tz": user.tz,
            "base_ccy": user.base_ccy,
            "status": user.status,
            "org_id": user.org_id,
        }

        risk_profile = self._resolve_risk_profile(session, user_id)
        if risk_profile:
            profile["risk_profile"] = risk_profile

        profile.update(self._resolve_broker_profile(session, user_id))
        return profile

    def _resolve_risk_profile(self, session: Session, user_id: str) -> str | None:
        policy = (
            session.query(app_models.UserTradingPolicy)
            .filter(app_models.UserTradingPolicy.user_id == user_id)
            .first()
        )
        if not policy or not policy.risk_overrides:
            return None
        value = policy.risk_overrides.get("risk_profile") or policy.risk_overrides.get("profile")
        return value if isinstance(value, str) else None

    def _resolve_broker_profile(self, session: Session, user_id: str) -> dict[str, Any]:
        accounts = (
            session.query(app_models.LinkedBrokerAccount, app_models.Broker)
            .join(
                app_models.Broker,
                app_models.LinkedBrokerAccount.broker_id == app_models.Broker.broker_id,
            )
            # Eager-load each account's credentials so the active secret_ref can be
            # threaded into the per-broker profile (per-user credential routing).
            .options(joinedload(app_models.LinkedBrokerAccount.credentials))
            .filter(app_models.LinkedBrokerAccount.user_id == user_id)
            .filter(app_models.LinkedBrokerAccount.status == "connected")
            .all()
        )
        if not accounts:
            return {}

        sorted_accounts: list[tuple[Any, Any]] = sorted(
            [(row[0], row[1]) for row in accounts],
            key=lambda pair: (
                pair[0].environment != "paper",
                _normalize_broker_value(getattr(pair[1], "code", "")) or "",
            ),
        )
        paper_accounts = [pair for pair in sorted_accounts if pair[0].environment == "paper"]
        live_accounts = [pair for pair in sorted_accounts if pair[0].environment == "live"]

        # The profile-level default is diagnostic context only; executable routing
        # is owned by each binding's explicit broker_account_id. Prefer a connected
        # live account here so the summary reflects deliberate live onboarding.
        default_acc, default_broker = (live_accounts or paper_accounts or sorted_accounts)[0]
        default_code = _normalize_broker_value(getattr(default_broker, "code", None)) or "paper"
        env_preference = "paper" if paper_accounts else default_acc.environment

        brokers: dict[str, dict[str, Any]] = {}
        account_profiles: dict[str, dict[str, Any]] = {}
        linked_brokers: list[str] = []
        for account, broker in sorted_accounts:
            code = _normalize_broker_value(getattr(broker, "code", None))
            if not code:
                continue
            account_profiles[str(account.account_id)] = {
                "account_id": account.account_id,
                "broker": code,
                "environment": account.environment,
                "status": account.status,
                "base_ccy": account.base_ccy,
                "paper_initial_equity": (
                    float(account.paper_initial_equity)
                    if account.paper_initial_equity is not None
                    else None
                ),
                "paper_initial_cash": (
                    float(account.paper_initial_cash)
                    if account.paper_initial_cash is not None
                    else None
                ),
                "credential_ref": _active_credential_ref(account),
            }
            linked_brokers.append(code)
            if code in brokers:
                continue
            caps = _broker_capabilities(broker)
            # Prefer this broker's connected live account in its summary entry;
            # binding-scoped execution still selects one explicit account.
            broker_accts = [
                acc
                for acc, bro in sorted_accounts
                if _normalize_broker_value(getattr(bro, "code", None)) == code
            ]
            selected = (
                next((a for a in broker_accts if a.environment == "live"), None)
                or next((a for a in broker_accts if a.environment == env_preference), None)
                or account
            )
            brokers[code] = {
                "account_id": selected.account_id,
                "environment": selected.environment,
                "status": selected.status,
                "base_ccy": selected.base_ccy,
                "paper_initial_equity": (
                    float(selected.paper_initial_equity)
                    if selected.paper_initial_equity is not None
                    else None
                ),
                "paper_initial_cash": (
                    float(selected.paper_initial_cash)
                    if selected.paper_initial_cash is not None
                    else None
                ),
                "credential_ref": _active_credential_ref(selected),
                "supports_options": _supports_options(caps),
                "asset_classes": _asset_classes(caps),
            }

        linked_brokers = sorted(set(linked_brokers))
        spot_broker = _select_primary_broker(sorted_accounts, supports_options=False)
        options_crypto = _select_primary_broker(
            sorted_accounts, supports_options=True, asset_class="crypto"
        )
        options_equity = _select_primary_broker(
            sorted_accounts, supports_options=True, asset_class="equity"
        )

        return {
            "broker": default_code,
            "spot_broker": spot_broker or default_code,
            "options_broker_crypto": options_crypto or default_code,
            "options_broker_equity": options_equity or default_code,
            "allowed_brokers": linked_brokers,
            "broker_account_id": default_acc.account_id,
            "broker_environment": default_acc.environment,
            "credential_ref": _active_credential_ref(default_acc),
            "sandbox": default_acc.environment != "live",
            "live_enabled": bool(live_accounts),
            "linked_brokers": linked_brokers,
            "brokers": brokers,
            "accounts": account_profiles,
        }


class DBStrategyConfigProvider:
    """Fetch strategy config for a user from DB."""

    _DEFAULT_STRATEGY_ID = "__all__"

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def __call__(
        self,
        user_id: str,
        strategy_id: str | None = None,
        binding_id: int | None = None,
        broker_account_id: int | None = None,
    ) -> dict[str, Any]:
        try:
            with self._session_factory() as s:
                return self._fetch_app_config(
                    s,
                    user_id,
                    strategy_id,
                    binding_id=binding_id,
                    broker_account_id=broker_account_id,
                )
        except (SQLAlchemyError, OSError):
            # DB / connection-level failures only — programming errors propagate.
            logger.exception("Failed to fetch strategy config for %s", user_id)
            return {}

    def _fetch_app_config(
        self,
        session: Session,
        user_id: str,
        strategy_id: str | None,
        *,
        binding_id: int | None,
        broker_account_id: int | None,
    ) -> dict[str, Any]:
        binding = self._find_binding(
            session,
            user_id,
            strategy_id,
            binding_id=binding_id,
            broker_account_id=broker_account_id,
        )
        if not binding:
            return {}

        resolved_strategy_id = binding.strategy_id or strategy_id
        safe_parameters = self._resolve_safe_parameters(
            session,
            user_id=user_id,
            strategy_id=resolved_strategy_id,
        )

        sizing = self._resolve_sizing_profile(session, binding.sizing_profile_id)
        execution_mode = resolve_execution_mode(
            preferred=binding.preferred_mode,
            allowed=binding.execution_modes_allowed,
            policy=binding.mode_selection_policy,
        )

        risk_caps = {
            "max_position_pct": (
                float(binding.max_position_pct) if binding.max_position_pct is not None else None
            ),
            "max_total_exposure_pct": (
                float(binding.max_total_exposure_pct)
                if binding.max_total_exposure_pct is not None
                else None
            ),
            "max_daily_loss_pct": (
                float(binding.max_daily_loss_pct)
                if binding.max_daily_loss_pct is not None
                else None
            ),
            "max_open_positions": binding.max_open_positions,
            "entry_cash_buffer_bps": (
                float(binding.entry_cash_buffer_bps)
                if binding.entry_cash_buffer_bps is not None
                else None
            ),
        }
        risk_caps = {k: v for k, v in risk_caps.items() if v is not None}

        # Precedence is deliberate: the allow-listed per-strategy safety flags
        # are merged first, then authoritative binding-derived routing, sizing,
        # and risk-cap fields are written over them.
        config: dict[str, Any] = {
            **safe_parameters,
            "user_id": binding.user_id,
            "binding_id": binding.binding_id,
            "strategy_id": resolved_strategy_id or self._DEFAULT_STRATEGY_ID,
            "broker_account_id": binding.broker_account_id,
            "execution_mode": execution_mode,
            "execution_modes_allowed": binding.execution_modes_allowed or [],
            "mode_selection_policy": binding.mode_selection_policy,
            "asset_classes_allowed": binding.asset_classes_allowed or [],
            "instruments_allowed": binding.instruments_allowed or [],
            "sectors_allowed": binding.sectors_allowed or [],
            "sizing": sizing,
            "risk_caps": risk_caps,
            "autopilot": binding.autopilot,
            "entries_enabled": binding.entries_enabled,
            "exits_enabled": binding.exits_enabled,
        }
        steered_broker = self._single_allowed_broker(binding.allowed_brokers)
        if steered_broker is not None:
            # A single-entry allowed_brokers is a routing STEER, not just a veto
            # (D3): without this, a binding restricted to e.g. ['paper'] resolves
            # the profile default broker (coinbase) and then gets BLOCKED at the
            # execution allowed-brokers gate instead of paper-trading. Both the
            # dispatcher route snapshot (config['broker'] first) and the execution
            # broker resolver (config broker override) honor this key. Multi-broker
            # or empty allow-lists keep the veto-only behavior.
            config["broker"] = steered_broker
        return config

    def _resolve_safe_parameters(
        self,
        session: Session,
        *,
        user_id: str,
        strategy_id: str | None,
    ) -> dict[str, bool]:
        """Return reviewed flags from an exact, active user/strategy config.

        A global binding may resolve a concrete signal strategy, but a config for
        another strategy must never bleed across that boundary. Invalid values,
        inactive rows, nested routing/risk overrides, and unrecognized or secret
        keys are ignored.
        """
        if not strategy_id:
            return {}
        strategy_config = (
            session.query(app_models.UserStrategyConfig)
            .filter(app_models.UserStrategyConfig.user_id == user_id)
            .filter(app_models.UserStrategyConfig.strategy_id == strategy_id)
            .filter(app_models.UserStrategyConfig.is_active.is_(True))
            .first()
        )
        if strategy_config is None or not isinstance(strategy_config.parameters, dict):
            return {}

        return {
            key: value
            for key, value in strategy_config.parameters.items()
            if key in _SAFE_USER_STRATEGY_BOOLEAN_PARAMETERS and type(value) is bool
        }

    @staticmethod
    def _single_allowed_broker(allowed_brokers: Any) -> str | None:
        """Return the broker code when the binding allows EXACTLY one broker."""
        normalized = [
            code
            for code in (_normalize_broker_value(item) for item in allowed_brokers or [])
            if code
        ]
        if len(normalized) == _SINGLE_BROKER_STEER_COUNT:
            return normalized[0]
        return None

    def _find_binding(
        self,
        session: Session,
        user_id: str,
        strategy_id: str | None,
        *,
        binding_id: int | None,
        broker_account_id: int | None,
    ) -> Any | None:
        query = session.query(app_models.UserStrategyBinding).filter(
            app_models.UserStrategyBinding.user_id == user_id
        )
        query = query.filter(app_models.UserStrategyBinding.is_active.is_(True))

        if binding_id is not None:
            binding = query.filter(
                app_models.UserStrategyBinding.binding_id == binding_id
            ).one_or_none()
            if binding is None:
                return None
            if broker_account_id is not None and binding.broker_account_id != broker_account_id:
                return None
            if strategy_id and binding.strategy_id not in {None, strategy_id}:
                return None
            return binding

        if broker_account_id is not None:
            query = query.filter(
                app_models.UserStrategyBinding.broker_account_id == broker_account_id
            )

        if strategy_id:
            binding = query.filter(
                app_models.UserStrategyBinding.strategy_id == strategy_id
            ).one_or_none()
            if binding:
                return binding

        return query.filter(app_models.UserStrategyBinding.strategy_id.is_(None)).one_or_none()

    def _resolve_sizing_profile(self, session: Session, profile_id: int | None) -> dict[str, Any]:
        if not profile_id:
            return {}
        sizing = session.get(app_models.SizingProfile, profile_id)
        if not sizing:
            return {}
        params = dict(sizing.params or {})
        params["method"] = sizing.method
        params["profile_name"] = sizing.name
        return params
