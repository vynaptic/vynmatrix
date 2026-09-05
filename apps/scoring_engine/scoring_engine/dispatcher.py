from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from lib_common.config_validation import parse_run_mode
from lib_common.internal_events import (
    BrokerRouteSnapshot,
    ExecutionCommandEvent,
    ExecutionPolicySnapshot,
    RebalanceExecutionCommandEvent,
    ScoreContextSnapshot,
)
from lib_common.logging import get_logger
from lib_common.observability import build_trace_context
from lib_strategy.signals.signal import Signal

from .models import TriggerDecision, trigger_to_execution_decision
from .snapshot_utils import signal_snapshot
from .storage import ScoreStore

logger = get_logger(__name__)

_PROVIDER_WITH_STRATEGY_PARAM_COUNT = 2
_PROVIDER_WITH_BINDING_ROUTE_PARAM_COUNT = 4

# Execution commands must survive a multi-hour execution-engine outage instead
# of dead-lettering after ~23 minutes (the outbox default of 10 attempts): a
# dead-lettered CLOSE strands a live position and there is no requeue tooling
# (D5). With the relay backoff min(300s, 30s*attempts), 60 attempts covers
# roughly 4.7 hours of downtime. Other topics keep the library default.
_EXECUTION_COMMAND_MAX_ATTEMPTS = 60


def _require_external_signal_id(signal: Signal) -> str:
    identity = signal.external_signal_id
    if identity:
        return identity
    msg = (
        f"Signal {signal.signal_id} has no canonical "
        "external_signal_id; refusing execution dispatch"
    )
    raise ValueError(msg)


def _provider_args(
    provider: Callable[..., Any],
    user_id: str,
    strategy_id: str | None,
    binding_id: int | None,
    broker_account_id: int | None,
) -> tuple[Any, ...]:
    """Match the provider's positional arity (user-only, +strategy, +route)."""
    params = inspect.signature(provider).parameters
    accepts_strategy = (
        strategy_id is not None and len(params) >= _PROVIDER_WITH_STRATEGY_PARAM_COUNT
    )
    accepts_binding_route = len(params) >= _PROVIDER_WITH_BINDING_ROUTE_PARAM_COUNT
    if accepts_binding_route:
        return (user_id, strategy_id, binding_id, broker_account_id)
    if accepts_strategy:
        return (user_id, strategy_id)
    return (user_id,)


async def _resolve_provider(
    provider: Callable[..., Any] | None,
    user_id: str,
    strategy_id: str | None,
    *,
    binding_id: int | None = None,
    broker_account_id: int | None = None,
) -> dict[str, Any]:
    if provider is None:
        return {}
    try:
        args = _provider_args(provider, user_id, strategy_id, binding_id, broker_account_id)
        if inspect.iscoroutinefunction(provider):
            result = await provider(*args)
        else:
            result = provider(*args)
            if inspect.isawaitable(result):
                result = await result
        return dict(result) if result else {}
    except Exception:
        # Broad catch is intentional: the provider is supplied by callers
        # (broker registry, user-context resolver) and may raise any exception
        # type. We surface the traceback via ``logger.exception`` and fall back
        # to an empty context so downstream guards can decide whether to
        # proceed or reject the trigger.
        logger.exception("Provider resolution failed", user_id=user_id, strategy_id=strategy_id)
        return {}


def _resolve_provider_in_worker(
    provider: Callable[..., Any] | None,
    user_id: str,
    strategy_id: str | None,
    *,
    binding_id: int | None = None,
    broker_account_id: int | None = None,
) -> dict[str, Any]:
    """Synchronously resolve a provider inside the worker-thread dispatch.

    Fallback for a decision whose binding was not visible when
    ``resolve_provider_contexts`` ran on the event loop (a bindings-cache
    refresh between the pre-resolution and the in-transaction evaluation).
    The production providers (``DBProfileProvider`` /
    ``DBStrategyConfigProvider``) are plain sync callables that open and close
    their own sessions, so calling them on the worker thread is safe. An async
    provider cannot be awaited here (the worker thread has no running event
    loop), so it resolves to the empty context — the same fail-open contract
    ``_resolve_provider`` applies to a provider error.
    """
    if provider is None:
        return {}
    if inspect.iscoroutinefunction(provider):
        logger.warning(
            "Async provider cannot be resolved inside the worker-thread dispatch; "
            "using empty context",
            user_id=user_id,
            strategy_id=strategy_id,
        )
        return {}
    try:
        args = _provider_args(provider, user_id, strategy_id, binding_id, broker_account_id)
        result = provider(*args)
        if inspect.isawaitable(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            logger.warning(
                "Awaitable provider result cannot be resolved inside the "
                "worker-thread dispatch; using empty context",
                user_id=user_id,
                strategy_id=strategy_id,
            )
            return {}
        return dict(result) if result else {}
    except Exception:
        # Broad catch is intentional (mirrors ``_resolve_provider``): the
        # provider is caller-supplied and may raise any exception type; fall
        # back to an empty context so downstream guards decide the outcome.
        logger.exception("Provider resolution failed", user_id=user_id, strategy_id=strategy_id)
        return {}


@dataclass(frozen=True)
class DispatchProviderContexts:
    """Profile/config provider lookups pre-resolved on the event loop.

    The ingest hot path runs its whole unit of work on a worker thread, where
    an async provider cannot be awaited. The dispatcher therefore resolves the
    provider lookups for every candidate binding BEFORE the thread starts
    (:meth:`ExecutionDispatcher.resolve_provider_contexts`) and hands the
    results to the synchronous :meth:`ExecutionDispatcher.dispatch_resolved`.

    ``profiles`` is keyed by ``user_id``; ``strategy_configs`` by the exact
    per-decision identity ``(user_id, binding_id, broker_account_id)``.
    """

    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    strategy_configs: dict[tuple[str, int | None, int | None], dict[str, Any]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class FrozenBindingContext:
    """Exact policy and route snapshots reused by single and batch dispatch."""

    profile: dict[str, Any]
    user_strategy_config: dict[str, Any]
    score_context: ScoreContextSnapshot
    execution_policy: ExecutionPolicySnapshot
    broker_route: BrokerRouteSnapshot


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(val) for val in value]
    return value


def _score_snapshot(
    signal: Signal,
    score_context: dict[str, Any] | None,
    *,
    recommended_mode: str | None = None,
) -> ScoreContextSnapshot:
    context = dict(score_context or {})
    return ScoreContextSnapshot(
        asset_score=float(context.get("asset_score", 0.0)),
        sector_score=(
            float(context["sector_score"]) if context.get("sector_score") is not None else None
        ),
        market_score=(
            float(context["market_score"]) if context.get("market_score") is not None else None
        ),
        recommended_mode=recommended_mode or context.get("recommended_mode"),
        score_scope=str(context.get("score_scope") or "asset"),
        score_target=str(context.get("score_target") or signal.symbol),
    )


def _allowed_brokers(profile: dict[str, Any], config: dict[str, Any]) -> list[str]:
    brokers = (
        config.get("allowed_brokers")
        or profile.get("linked_brokers")
        or profile.get("allowed_brokers")
    )
    if not brokers:
        return []
    if isinstance(brokers, str):
        return [brokers]
    return [str(item) for item in brokers if item]


def _policy_snapshot(
    *,
    decision: TriggerDecision,
    strategy_id: str,
    user_strategy_config: dict[str, Any],
    score_context: ScoreContextSnapshot,
) -> ExecutionPolicySnapshot:
    config = dict(user_strategy_config or {})
    execution_modes_allowed = config.get("execution_modes_allowed")
    if not execution_modes_allowed and decision.execution_mode:
        execution_modes_allowed = [decision.execution_mode]
    if isinstance(execution_modes_allowed, str):
        execution_modes_allowed = [execution_modes_allowed]

    return ExecutionPolicySnapshot(
        user_id=decision.user_id,
        strategy_id=strategy_id,
        binding_id=decision.binding_id,
        autopilot=bool(config.get("autopilot", decision.should_execute)),
        entries_enabled=bool(config.get("entries_enabled", decision.should_execute)),
        exits_enabled=bool(config.get("exits_enabled", decision.should_execute)),
        execution_mode=decision.execution_mode or config.get("execution_mode"),
        execution_modes_allowed=[str(mode) for mode in execution_modes_allowed or []],
        mode_selection_policy=config.get("mode_selection_policy"),
        recommended_mode=score_context.recommended_mode,
        asset_score_threshold=(
            float(config["asset_score_threshold"])
            if config.get("asset_score_threshold") is not None
            else None
        ),
        sector_score_threshold=(
            float(config["sector_score_threshold"])
            if config.get("sector_score_threshold") is not None
            else None
        ),
        market_score_threshold=(
            float(config["market_score_threshold"])
            if config.get("market_score_threshold") is not None
            else None
        ),
        allowed_brokers=[str(item) for item in decision.allowed_brokers]
        or _allowed_brokers({}, config),
        sizing=dict(decision.sizing_profile or config.get("sizing") or {}),
        risk_caps={
            **dict(config.get("risk_caps") or {}),
            **dict(decision.risk_caps or {}),
        },
        config=config,
    )


def _resolve_decision_account_id(
    decision: TriggerDecision,
    config: dict[str, Any],
) -> int:
    """Validate the authoritative decision account against frozen configuration."""
    requested_account_id = decision.broker_account_id
    if requested_account_id is None:
        msg = "Active execution decision has no linked broker account"
        raise ValueError(msg)
    try:
        account_id = int(requested_account_id)
    except (TypeError, ValueError) as exc:
        msg = "Execution decision broker_account_id must be a positive integer"
        raise ValueError(msg) from exc
    if account_id <= 0:
        msg = "Execution decision broker_account_id must be a positive integer"
        raise ValueError(msg)

    configured_account_id = config.get("broker_account_id")
    if configured_account_id is None:
        return account_id
    try:
        config_account_id = int(configured_account_id)
    except (TypeError, ValueError) as exc:
        msg = "Configured broker_account_id must be a positive integer"
        raise ValueError(msg) from exc
    if config_account_id <= 0:
        msg = "Configured broker_account_id must be a positive integer"
        raise ValueError(msg)
    if config_account_id != account_id:
        msg = (
            f"Execution decision broker account {account_id} conflicts with "
            f"configured account {config_account_id}"
        )
        raise ValueError(msg)
    return account_id


def _route_snapshot(
    *,
    asset_class: str | None,
    decision: TriggerDecision,
    profile: dict[str, Any],
    user_strategy_config: dict[str, Any],
    runtime_mode: str,
) -> BrokerRouteSnapshot:
    config = dict(user_strategy_config or {})
    account_id = _resolve_decision_account_id(decision, config)

    account_profile = dict((profile.get("accounts") or {}).get(str(account_id)) or {})
    if not account_profile:
        for broker_profile in (profile.get("brokers") or {}).values():
            if int(broker_profile.get("account_id") or 0) == account_id:
                account_profile = dict(broker_profile)
                break
    if not account_profile:
        msg = f"Linked broker account {account_id} is unavailable for this user"
        raise ValueError(msg)
    if account_profile.get("status") != "connected":
        msg = f"Linked broker account {account_id} is not connected"
        raise ValueError(msg)

    broker = (
        config.get("broker")
        or config.get("spot_broker")
        or account_profile.get("broker")
        or profile.get("broker")
        or profile.get("spot_broker")
    )
    account_broker = str(account_profile.get("broker") or broker or "")
    if broker and account_broker and str(broker) != account_broker:
        msg = (
            f"Binding account {account_id} belongs to {account_broker}, "
            f"not selected broker {broker}"
        )
        raise ValueError(msg)
    broker = account_broker or broker
    if not broker:
        msg = f"Linked broker account {account_id} has no broker identity"
        raise ValueError(msg)

    if runtime_mode == "live":
        broker_environment = account_profile.get("environment")
        if broker_environment != "live":
            msg = (
                "Live execution requires a live linked broker account; "
                f"account {account_id} is paper"
            )
            raise ValueError(msg)
    else:
        broker_environment = account_profile.get("environment")
        if broker_environment != "paper":
            msg = (
                f"Non-live execution requires a paper linked broker account; "
                f"account {account_id} is live"
            )
            raise ValueError(msg)
    execution_mode = decision.execution_mode or config.get("execution_mode")
    # Resolve the per-account credential pointer for the broker actually chosen,
    # preferring the broker-specific entry over any profile-level default so the
    # recorded route reflects the exact user credential that will be used.
    broker_credential_ref = account_profile.get("credential_ref")
    return BrokerRouteSnapshot(
        broker=str(broker),
        broker_account_id=account_id,
        broker_environment=broker_environment,
        credential_ref=str(
            config.get("credential_ref")
            or broker_credential_ref
            or profile.get("credential_ref")
            or ""
        )
        or None,
        allowed_brokers=_allowed_brokers(profile, config)
        or [str(item) for item in decision.allowed_brokers],
        route_source="scoring_policy",
        live_enabled=bool(
            runtime_mode == "live" and (profile.get("live_enabled") or config.get("live_enabled"))
        ),
        sandbox=bool(broker_environment != "live"),
        asset_class=asset_class,
        execution_mode=str(execution_mode) if execution_mode else None,
    )


class ExecutionDispatcher:
    """Build and enqueue execution commands from scored decisions."""

    def __init__(
        self,
        exec_engine_url: str,
        profile_provider: Callable[[str], Any] | None = None,
        strategy_config_provider: Callable[[str], Any] | None = None,
        store: ScoreStore | None = None,
        runtime_mode: str = "paper",
    ) -> None:
        self.exec_engine_url = exec_engine_url.rstrip("/")
        self.profile_provider = profile_provider
        self.strategy_config_provider = strategy_config_provider
        self._store = store
        self._runtime_mode = parse_run_mode(runtime_mode).value

    async def resolve_provider_contexts(
        self,
        *,
        strategy_id: str | None,
        targets: Iterable[tuple[str, int | None, int | None]],
        user_profiles: dict[str, dict[str, Any]] | None = None,
        user_strategy_configs: dict[int, dict[str, Any]] | None = None,
    ) -> DispatchProviderContexts:
        """Resolve profile/config lookups on the event loop for later sync dispatch.

        ``targets`` are ``(user_id, binding_id, broker_account_id)`` identities —
        per-decision when called from :meth:`dispatch`, per-candidate-binding
        when the ingest path pre-resolves before its worker-thread unit of
        work. Precedence per identity matches the historical per-decision
        logic: an explicit ``user_profiles`` / ``user_strategy_configs`` entry
        wins, and the provider is the fallback for a missing or empty entry.
        """
        profiles: dict[str, dict[str, Any]] = {}
        strategy_configs: dict[tuple[str, int | None, int | None], dict[str, Any]] = {}
        for user_id, binding_id, broker_account_id in targets:
            if user_id not in profiles:
                profiles[user_id] = (user_profiles or {}).get(
                    user_id, {}
                ) or await _resolve_provider(self.profile_provider, user_id, strategy_id)
            config_key = (user_id, binding_id, broker_account_id)
            if config_key not in strategy_configs:
                explicit_config = (
                    (user_strategy_configs or {}).get(binding_id, {})
                    if binding_id is not None
                    else {}
                )
                strategy_configs[config_key] = explicit_config or await _resolve_provider(
                    self.strategy_config_provider,
                    user_id,
                    strategy_id,
                    binding_id=binding_id,
                    broker_account_id=broker_account_id,
                )
        return DispatchProviderContexts(profiles=profiles, strategy_configs=strategy_configs)

    async def dispatch(
        self,
        signal: Signal,
        decisions: list[TriggerDecision],
        score_context: dict[str, Any] | None = None,
        user_profiles: dict[str, dict[str, Any]] | None = None,
        user_strategy_configs: dict[int, dict[str, Any]] | None = None,
        original_signal: Signal | None = None,
        instrument_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Freeze decisions into versioned execution-command events.

        Composition of the two-phase API: provider contexts are resolved on
        the running loop, then the synchronous :meth:`dispatch_resolved` does
        the store writes. The ingest hot path calls the two phases separately
        so the sync phase can run inside its worker-thread unit of work.
        """
        provider_contexts = await self.resolve_provider_contexts(
            strategy_id=signal.strategy_id,
            targets=[
                (decision.user_id, decision.binding_id, decision.broker_account_id)
                for decision in decisions
            ],
            user_profiles=user_profiles,
            user_strategy_configs=user_strategy_configs,
        )
        return self.dispatch_resolved(
            signal=signal,
            decisions=decisions,
            provider_contexts=provider_contexts,
            score_context=score_context,
            original_signal=original_signal,
            instrument_id=instrument_id,
        )

    def _decision_provider_context(
        self,
        decision: TriggerDecision,
        strategy_id: str,
        provider_contexts: DispatchProviderContexts,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Look up one decision's pre-resolved profile and strategy config.

        A decision whose binding was missed by the event-loop pre-resolution
        (bindings-cache refresh race) falls back to the in-worker sync
        resolver.
        """
        profile = provider_contexts.profiles.get(decision.user_id)
        if profile is None:
            profile = _resolve_provider_in_worker(
                self.profile_provider, decision.user_id, strategy_id
            )
        config_key = (decision.user_id, decision.binding_id, decision.broker_account_id)
        user_strat_cfg = provider_contexts.strategy_configs.get(config_key)
        if user_strat_cfg is None:
            user_strat_cfg = _resolve_provider_in_worker(
                self.strategy_config_provider,
                decision.user_id,
                strategy_id,
                binding_id=decision.binding_id,
                broker_account_id=decision.broker_account_id,
            )
        return profile, user_strat_cfg

    def freeze_binding_context_resolved(
        self,
        *,
        decision: TriggerDecision,
        signal: Signal,
        provider_contexts: DispatchProviderContexts,
        score_context: dict[str, Any] | None = None,
    ) -> FrozenBindingContext:
        """Freeze one pre-resolved binding through the canonical policy/route path."""

        profile, user_strat_cfg = self._decision_provider_context(
            decision,
            signal.strategy_id,
            provider_contexts,
        )
        if decision.execution_mode:
            user_strat_cfg = {**user_strat_cfg, "execution_mode": decision.execution_mode}
        if decision.sizing_profile:
            user_strat_cfg = {**user_strat_cfg, "sizing": decision.sizing_profile}
        if decision.risk_caps:
            user_strat_cfg = {**user_strat_cfg, "risk_caps": decision.risk_caps}
        if decision.allowed_brokers:
            user_strat_cfg = {
                **user_strat_cfg,
                "allowed_brokers": decision.allowed_brokers,
            }
        score_snapshot = _score_snapshot(
            signal,
            score_context,
            recommended_mode=decision.execution_mode,
        )
        route_snapshot = _route_snapshot(
            asset_class=signal.asset_class,
            decision=decision,
            profile=profile,
            user_strategy_config=user_strat_cfg,
            runtime_mode=self._runtime_mode,
        )
        user_strat_cfg = {
            **user_strat_cfg,
            "broker_account_id": route_snapshot.broker_account_id,
        }
        policy_snapshot = _policy_snapshot(
            decision=decision,
            strategy_id=signal.strategy_id,
            user_strategy_config=user_strat_cfg,
            score_context=score_snapshot,
        )
        return FrozenBindingContext(
            profile=_json_safe(profile),
            user_strategy_config=_json_safe(user_strat_cfg),
            score_context=score_snapshot,
            execution_policy=policy_snapshot,
            broker_route=route_snapshot,
        )

    def freeze_portfolio_binding_context_resolved(
        self,
        *,
        decision: TriggerDecision,
        strategy_id: str,
        asset_class: str,
        provider_contexts: DispatchProviderContexts,
    ) -> FrozenBindingContext:
        """Freeze a binding for an intentional all-cash portfolio batch."""

        profile, user_strat_cfg = self._decision_provider_context(
            decision,
            strategy_id,
            provider_contexts,
        )
        if decision.execution_mode:
            user_strat_cfg = {**user_strat_cfg, "execution_mode": decision.execution_mode}
        if decision.sizing_profile:
            user_strat_cfg = {**user_strat_cfg, "sizing": decision.sizing_profile}
        if decision.risk_caps:
            user_strat_cfg = {**user_strat_cfg, "risk_caps": decision.risk_caps}
        if decision.allowed_brokers:
            user_strat_cfg = {
                **user_strat_cfg,
                "allowed_brokers": decision.allowed_brokers,
            }
        score_snapshot = ScoreContextSnapshot(
            asset_score=0.0,
            recommended_mode=decision.execution_mode,
            score_scope="model_rebalance",
            score_target=strategy_id,
        )
        route_snapshot = _route_snapshot(
            asset_class=asset_class,
            decision=decision,
            profile=profile,
            user_strategy_config=user_strat_cfg,
            runtime_mode=self._runtime_mode,
        )
        user_strat_cfg = {
            **user_strat_cfg,
            "broker_account_id": route_snapshot.broker_account_id,
        }
        policy_snapshot = _policy_snapshot(
            decision=decision,
            strategy_id=strategy_id,
            user_strategy_config=user_strat_cfg,
            score_context=score_snapshot,
        )
        return FrozenBindingContext(
            profile=_json_safe(profile),
            user_strategy_config=_json_safe(user_strat_cfg),
            score_context=score_snapshot,
            execution_policy=policy_snapshot,
            broker_route=route_snapshot,
        )

    def enqueue_rebalance(self, command: RebalanceExecutionCommandEvent) -> str:
        """Enqueue one immutable account plan at its official actionable time."""

        if self._store is None:
            message = "Dispatcher store is not configured"
            raise RuntimeError(message)
        return self._store.enqueue_event(
            topic=command.topic,
            event_type=command.event_type,
            payload=command.model_dump(mode="json"),
            schema_version=command.schema_version,
            aggregate_type="account_rebalance_plan",
            aggregate_id=command.account_plan_id,
            event_key=f"rebalance-execution-command:{command.account_plan_id}",
            ordering_key=f"{command.user_id}:{command.broker_route.broker_account_id}",
            headers={"run_id": command.run_id or ""},
            max_attempts=_EXECUTION_COMMAND_MAX_ATTEMPTS,
            available_at=command.execute_not_before,
        )

    def dispatch_resolved(
        self,
        *,
        signal: Signal,
        decisions: list[TriggerDecision],
        provider_contexts: DispatchProviderContexts,
        score_context: dict[str, Any] | None = None,
        original_signal: Signal | None = None,
        instrument_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Freeze decisions into versioned execution-command events (sync).

        Every store write here (``persist_decision_log`` + ``enqueue_event``)
        joins the caller's active unit of work when one is set, so the ingest
        path commits signal + scores + decision log + execution.commands rows
        in ONE transaction on the worker thread that owns that session.
        Provider lookups must already be resolved (``provider_contexts``);
        a decision missing from the pre-resolved contexts falls back to
        ``_resolve_provider_in_worker`` (sync providers only).
        """
        results: list[dict[str, Any]] = []

        asset_score = (score_context or {}).get("asset_score", 0.0)
        sector_score = (score_context or {}).get("sector_score")
        market_score = (score_context or {}).get("market_score")

        base_signal = original_signal or signal

        for decision in decisions:
            trace_ctx = build_trace_context(
                run_id=signal.run_id,
                signal_id=signal.signal_id,
                strategy_id=signal.strategy_id,
                symbol=signal.symbol,
                user_id=decision.user_id,
            )
            binding_id = decision.binding_id or 0
            resolved_instr_id = instrument_id
            if resolved_instr_id is None and signal.instrument_id:
                try:
                    resolved_instr_id = int(signal.instrument_id)
                except (ValueError, TypeError):
                    resolved_instr_id = None
            if resolved_instr_id is None and self._store is not None:
                resolved_instr_id = self._store.resolve_instrument_id(signal.symbol)
            if resolved_instr_id is None:
                # Unresolvable instrument: do NOT fabricate a hashed id — a symbol
                # hash can collide with real auto-increment instrument_ids (and
                # other symbols), corrupting downstream P&L / position / feedback
                # bookkeeping keyed on instr_id. Skip this decision loudly so the
                # instrument is registered upstream instead of silently corrupting.
                msg = f"Unresolvable instrument_id for symbol={signal.symbol}; skipping dispatch"
                logger.error(msg, **trace_ctx)
                results.append(
                    {
                        "user_id": decision.user_id,
                        "status": "error",
                        "reason": msg,
                    }
                )
                continue

            # The resolver is authoritative at this boundary. Carry the same
            # instrument identity used by the decision row into the immutable
            # execution-command snapshot so every downstream order can retain it.
            command_signal = replace(base_signal, instrument_id=str(resolved_instr_id))

            profile, user_strat_cfg = self._decision_provider_context(
                decision, signal.strategy_id, provider_contexts
            )

            if decision.execution_mode:
                user_strat_cfg = {**user_strat_cfg, "execution_mode": decision.execution_mode}
            if decision.sizing_profile:
                user_strat_cfg = {**user_strat_cfg, "sizing": decision.sizing_profile}
            if decision.risk_caps:
                user_strat_cfg = {**user_strat_cfg, "risk_caps": decision.risk_caps}
            if decision.allowed_brokers:
                user_strat_cfg = {**user_strat_cfg, "allowed_brokers": decision.allowed_brokers}

            score_snapshot = _score_snapshot(
                command_signal,
                score_context,
                recommended_mode=decision.execution_mode,
            )
            try:
                route_snapshot = _route_snapshot(
                    asset_class=command_signal.asset_class,
                    decision=decision,
                    profile=profile,
                    user_strategy_config=user_strat_cfg,
                    runtime_mode=self._runtime_mode,
                )
            except ValueError as exc:
                logger.warning(
                    "Broker account routing failed",
                    error=str(exc),
                    binding_id=decision.binding_id,
                    **trace_ctx,
                )
                results.append(
                    {
                        "user_id": decision.user_id,
                        "status": "error",
                        "reason": str(exc),
                    }
                )
                continue
            # The scored binding decision is the authoritative account
            # selection. Freeze that exact identity into every command surface
            # so downstream adapters cannot fall back to a different account.
            user_strat_cfg = {
                **user_strat_cfg,
                "broker_account_id": route_snapshot.broker_account_id,
            }
            policy_snapshot = _policy_snapshot(
                decision=decision,
                strategy_id=command_signal.strategy_id,
                user_strategy_config=user_strat_cfg,
                score_context=score_snapshot,
            )

            exec_decision = trigger_to_execution_decision(
                trigger=decision,
                signal=command_signal,
                binding_id=binding_id,
                instrument_id=resolved_instr_id,
                broker_account_id=route_snapshot.broker_account_id,
                asset_score=asset_score,
                sector_score=sector_score,
                market_score=market_score,
                execution_policy_snapshot=policy_snapshot.model_dump(mode="json"),
                broker_route_snapshot=route_snapshot.model_dump(mode="json"),
            )
            dedup_identity = _require_external_signal_id(command_signal)
            if self._store is not None and hasattr(self._store, "persist_decision_log"):
                self._store.persist_decision_log(
                    exec_decision,
                    signal_action=command_signal.action.value.lower(),
                    dedup_identity=dedup_identity,
                    run_id=command_signal.run_id,
                )

            if not decision.should_execute:
                results.append(
                    {
                        "user_id": decision.user_id,
                        "status": "skipped",
                        "reason": decision.reason,
                        "execution_policy": policy_snapshot.model_dump(mode="json"),
                        "broker_route": route_snapshot.model_dump(mode="json"),
                    }
                )
                continue

            command = ExecutionCommandEvent(
                run_id=command_signal.run_id,
                correlation_id=command_signal.signal_id,
                # The canonical signal is the immediate cause of this command.
                causation_id=command_signal.signal_id,
                producer="scoring_engine",
                user_id=decision.user_id,
                signal=signal_snapshot(command_signal),
                score_context=score_snapshot,
                execution_policy=policy_snapshot,
                broker_route=route_snapshot,
                profile=_json_safe(profile),
                user_strategy_config=_json_safe(user_strat_cfg),
            )

            if self._store is None:
                results.append(
                    {
                        "user_id": decision.user_id,
                        "status": "error",
                        "error": "Dispatcher store is not configured",
                    }
                )
                continue

            event_key = (
                f"execution-command:{dedup_identity}:{decision.user_id}:"
                f"{binding_id}:{command_signal.action.value.lower()}"
            )
            event_id = self._store.enqueue_event(
                topic=command.topic,
                event_type=command.event_type,
                payload=command.model_dump(mode="json"),
                schema_version=command.schema_version,
                aggregate_type="execution_command",
                aggregate_id=f"{dedup_identity}:{decision.user_id}",
                event_key=event_key,
                ordering_key=decision.user_id,
                headers={"run_id": base_signal.run_id or ""},
                max_attempts=_EXECUTION_COMMAND_MAX_ATTEMPTS,
            )
            # Per-event detail at DEBUG: enqueue_event is idempotent on event_key,
            # so a redelivery/bootstrap re-drain reuses the existing row and this
            # line would otherwise masquerade as a brand-new command at INFO (L2).
            # The INFO altitude is the batch summary emitted after the loop.
            logger.debug("Queued execution command", event_id=event_id, **trace_ctx)
            results.append(
                {
                    "user_id": decision.user_id,
                    "status": "queued",
                    "event_id": event_id,
                    "execution_decision": exec_decision.to_dict(),
                    "execution_command": command.model_dump(mode="json"),
                }
            )

        queued = sum(1 for result in results if result.get("status") == "queued")
        if queued:
            logger.info(
                "Dispatched execution commands",
                count=queued,
                signal_id=base_signal.signal_id,
                run_id=base_signal.run_id,
            )

        return results

    async def aclose(self) -> None:
        return None
