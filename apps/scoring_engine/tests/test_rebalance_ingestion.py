from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from lib_common.hashing import canonical_json_hash
from lib_common.internal_events import (
    CanonicalSignalSnapshot,
    ModelRebalanceLegSnapshot,
    ModelRebalanceSubmissionEvent,
    compute_model_rebalance_content_sha256,
    compute_model_rebalance_id,
)
from lib_common.paper_promotion import (
    PaperPromotionScope,
    paper_promotion_instrument_set_sha256,
)
from scoring_engine.api import create_app
from scoring_engine.dispatcher import DispatchProviderContexts, ExecutionDispatcher
from scoring_engine.domain import MarketContext, MarketRegime
from scoring_engine.engine import ScoreEngine
from scoring_engine.models import AccountRebalancePlanDraft, ScoringUserBinding
from scoring_engine.services.rebalance_service import RebalanceScoringService
from scoring_engine.storage_memory import InMemoryScoreStore

_STRATEGY_ID = "us_quality_compounder_v1"
_STRATEGY_VERSION = "1.0.0"
_SESSION = date(2026, 12, 31)
_CUTOFF = datetime(2026, 12, 31, 21, 0, tzinfo=UTC)
_EXECUTE_AT = datetime(2027, 1, 4, 14, 30, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _event(
    *,
    data_use_scope: str = "paper_forward",
    include_second_entry: bool = False,
    incumbent_hold: bool = False,
    all_cash: bool = False,
) -> ModelRebalanceSubmissionEvent:
    configuration = _digest("configuration")
    input_snapshot = _digest("strategy-input")
    rank_snapshot = _digest("rank-snapshot")
    authority = _digest("provider-authority")
    execution_session = _digest("next-official-session")
    legs: list[ModelRebalanceLegSnapshot] = []
    decisions = (
        []
        if all_cash
        else [
            ("IBM", "close", "exit", 1, _digest("ibm-factor"), 25.0, 0.0),
            (
                "MSFT",
                "hold" if incumbent_hold else "long",
                "hold" if incumbent_hold else "entry",
                2,
                _digest("msft-factor"),
                1.0,
                0.05,
            ),
        ]
    )
    if include_second_entry:
        decisions.append(("AAPL", "long", "entry", 3, _digest("aapl-factor"), 2.0, 0.05))
    for sequence, (symbol, action, phase, instrument_id, factor_id, rank, allocation) in enumerate(
        decisions
    ):
        signal_id = f"rebalance-signal-{sequence}"
        signal = CanonicalSignalSnapshot(
            signal_id=signal_id,
            strategy_id=_STRATEGY_ID,
            strategy_type="indicator",
            symbol=symbol,
            action=action,
            confidence=0.9,
            timestamp=_CUTOFF,
            horizon="swing",
            expected_return=0.01,
            predicted_risk=0.02,
            horizon_days=20,
            asset_class="equity",
            sector="Information Technology",
            index="SP500",
            instrument_id=str(instrument_id),
            strategy_version=_STRATEGY_VERSION,
            run_id="paper-rebalance-run",
            source="paper",
            external_signal_id=f"rebalance-external-{sequence}",
            metadata={
                "configuration_sha256": configuration,
                "strategy_input_sha256": input_snapshot,
                "rank_snapshot_sha256": rank_snapshot,
                "provider_authority_sha256": authority,
                "data_use_scope": data_use_scope,
                "execution_session_sha256": execution_session,
                "execute_not_before": _EXECUTE_AT.isoformat(),
                "factor_snapshot_id": factor_id,
                "current_rank_row_present": True,
                "current_rank_factor_snapshot_id": factor_id,
            },
        )
        legs.append(
            ModelRebalanceLegSnapshot(
                leg_id=f"model-leg-{sequence}",
                sequence=sequence,
                phase=phase,
                signal=signal,
                rank=rank,
                allocation_hint=allocation,
                factor_snapshot_id=factor_id,
                rank_snapshot_id=rank_snapshot,
                current_rank_row_present=True,
                current_rank_factor_snapshot_id=factor_id,
            )
        )
    content = compute_model_rebalance_content_sha256(
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        effective_session=_SESSION,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=execution_session,
        data_use_scope=data_use_scope,
        provider_authority_sha256=authority,
        configuration_sha256=configuration,
        input_snapshot_sha256=input_snapshot,
        rank_snapshot_sha256=rank_snapshot,
        intentional_cash_slots=30 if all_cash else 0,
        legs=legs,
    )
    return ModelRebalanceSubmissionEvent(
        event_id="model-rebalance-event",
        occurred_at=_CUTOFF,
        run_id="paper-rebalance-run",
        producer="indicator_runner",
        rebalance_id=compute_model_rebalance_id(
            strategy_id=_STRATEGY_ID,
            strategy_version=_STRATEGY_VERSION,
            effective_session=_SESSION,
            execute_not_before=_EXECUTE_AT,
            execution_session_sha256=execution_session,
            data_use_scope=data_use_scope,
            provider_authority_sha256=authority,
            configuration_sha256=configuration,
            input_snapshot_sha256=input_snapshot,
            content_sha256=content,
        ),
        strategy_id=_STRATEGY_ID,
        strategy_version=_STRATEGY_VERSION,
        effective_session=_SESSION,
        decision_cutoff=_CUTOFF,
        execute_not_before=_EXECUTE_AT,
        execution_session_sha256=execution_session,
        data_use_scope=data_use_scope,
        provider_authority_sha256=authority,
        configuration_sha256=configuration,
        input_snapshot_sha256=input_snapshot,
        rank_snapshot_sha256=rank_snapshot,
        content_sha256=content,
        expected_leg_count=len(legs),
        intentional_cash_slots=30 if all_cash else 0,
        legs=legs,
    )


def _binding(*, user_id: str, binding_id: int, account_id: int) -> ScoringUserBinding:
    return ScoringUserBinding(
        user_id=user_id,
        binding_id=binding_id,
        strategy_id=_STRATEGY_ID,
        broker_account_id=account_id,
        asset_score_threshold=0.0,
        execution_mode="spot",
        risk_caps={"max_open_positions": 30},
        allowed_brokers=["paper"],
        autopilot=True,
        entries_enabled=True,
        exits_enabled=True,
    )


def _contexts(*bindings: ScoringUserBinding) -> DispatchProviderContexts:
    profiles: dict[str, dict[str, object]] = {}
    configs: dict[tuple[str, int | None, int | None], dict[str, object]] = {}
    for binding in bindings:
        account_id = int(binding.broker_account_id or 0)
        profiles[binding.user_id] = {
            "accounts": {
                str(account_id): {
                    "account_id": account_id,
                    "broker": "paper",
                    "environment": "paper",
                    "status": "connected",
                }
            }
        }
        configs[(binding.user_id, binding.binding_id, binding.broker_account_id)] = {
            "user_id": binding.user_id,
            "binding_id": binding.binding_id,
            "strategy_id": binding.strategy_id,
            "broker_account_id": binding.broker_account_id,
            "broker": "paper",
            "execution_mode": "spot",
            "autopilot": binding.autopilot,
            "entries_enabled": binding.entries_enabled,
            "exits_enabled": binding.exits_enabled,
        }
    return DispatchProviderContexts(profiles=profiles, strategy_configs=configs)


def _promotion_scope(event: ModelRebalanceSubmissionEvent) -> PaperPromotionScope:
    instruments = {1: "IBM", 2: "MSFT", 3: "AAPL"}
    return PaperPromotionScope(
        user_id="tenant-a",
        broker_account_id=11,
        strategy_binding_id=7,
        strategy_id=event.strategy_id,
        strategy_version=event.strategy_version,
        strategy_universe="SP500",
        model_scope="synchronized_portfolio",
        canonical_instrument=None,
        asset_class="equity",
        broker_code="paper",
        data_use_scope="paper_forward",
        model_configuration_sha256=event.configuration_sha256,
        instrument_set_sha256=paper_promotion_instrument_set_sha256(instruments),
        instruments=tuple(sorted(instruments.items())),
        scoring_semantics="rank_model",
        order_evidence_profile="synchronized_targets",
    )


def _service(
    store: InMemoryScoreStore,
    *,
    promotion_scope: PaperPromotionScope | None = None,
) -> RebalanceScoringService:
    engine = ScoreEngine(
        store,
        paper_promotion_scope=promotion_scope,
        paper_promotion_required=promotion_scope is not None,
    )
    for symbol in ("IBM", "MSFT", "AAPL"):
        engine.pipeline.set_market_context(
            symbol,
            MarketContext(
                regime=MarketRegime.BULL_QUIET,
                as_of=_CUTOFF,
                realized_vol_20d=0.01,
            ),
        )
    dispatcher = ExecutionDispatcher(
        "http://execution-engine",
        store=store,
        runtime_mode="paper",
    )
    return RebalanceScoringService(engine=engine, dispatcher=dispatcher)


def test_promoted_portfolio_accepts_only_exact_model_and_binding_scope() -> None:
    store = InMemoryScoreStore()
    exact = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    exact.asset_filter = ["IBM", "MSFT", "AAPL"]
    unrelated = _binding(user_id="tenant-b", binding_id=8, account_id=12)
    unrelated.asset_filter = ["IBM", "MSFT", "AAPL"]
    store.seed_binding(exact)
    store.seed_binding(unrelated)
    event = _event()

    service = _service(store, promotion_scope=_promotion_scope(event))
    result = service.ingest(
        event,
        provider_contexts=_contexts(exact, unrelated),
    )

    assert len(result.plan_ids) == 1
    commands = [
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    ]
    assert [record.payload["user_id"] for record in commands] == ["tenant-a"]
    outbox_count = len(store._outbox)
    replay = service.ingest(event, provider_contexts=_contexts(exact, unrelated))
    assert replay.replayed is True
    assert replay.plan_ids == result.plan_ids
    assert len(store._outbox) == outbox_count


def test_promoted_portfolio_rejects_model_configuration_drift() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = ["IBM", "MSFT", "AAPL"]
    store.seed_binding(binding)
    event = _event()
    wrong_scope = replace(
        _promotion_scope(event),
        model_configuration_sha256=_digest("different-model-configuration"),
    )

    with pytest.raises(ValueError, match="differs from the exact paper promotion scope"):
        _service(store, promotion_scope=wrong_scope).ingest(
            event,
            provider_contexts=_contexts(binding),
        )

    assert store._model_rebalances == {}
    assert store._outbox == {}


def test_promoted_portfolio_rejects_nonexact_binding_instrument_scope() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = ["MSFT"]
    store.seed_binding(binding)
    event = _event()

    with pytest.raises(ValueError, match="one active binding with the exact"):
        _service(store, promotion_scope=_promotion_scope(event)).ingest(
            event,
            provider_contexts=_contexts(binding),
        )

    assert store._model_rebalances == {}
    assert store._outbox == {}


@pytest.mark.parametrize(
    ("asset_filter", "allowed_brokers"),
    [
        (["*", "IBM", "MSFT", "AAPL"], ["paper"]),
        (["IBM", "MSFT", "AAPL", "aapl"], ["paper"]),
        (["IBM", "MSFT", "AAPL"], ["paper", "local-paper"]),
        (["IBM", "MSFT", "AAPL"], ["*", "paper"]),
    ],
)
def test_promoted_portfolio_rejects_wildcard_or_duplicate_binding_scope(
    asset_filter: list[str],
    allowed_brokers: list[str],
) -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = asset_filter
    binding.allowed_brokers = allowed_brokers
    store.seed_binding(binding)
    event = _event()

    with pytest.raises(ValueError, match="one active binding with the exact"):
        _service(store, promotion_scope=_promotion_scope(event)).ingest(
            event,
            provider_contexts=_contexts(binding),
        )

    assert store._model_rebalances == {}
    assert store._outbox == {}


@pytest.mark.parametrize(
    ("scope_change", "replacement"),
    [
        ("user_id", "other-user"),
        ("strategy_binding_id", 8),
        ("broker_account_id", 12),
        ("broker_code", "ibkr"),
    ],
)
def test_promoted_replay_revalidates_changed_route_authority(
    scope_change: str,
    replacement: object,
) -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = ["IBM", "MSFT", "AAPL"]
    store.seed_binding(binding)
    event = _event()
    original_service = _service(store, promotion_scope=_promotion_scope(event))
    original = original_service.ingest(event, provider_contexts=_contexts(binding))
    outbox_count = len(store._outbox)
    scope = replace(_promotion_scope(event), **{scope_change: replacement})
    changed_service = _service(store, promotion_scope=scope)

    assert changed_service.preflight_exact_replay(event) is None
    with pytest.raises(ValueError, match="one active binding with the exact"):
        changed_service.ingest(
            event,
            provider_contexts=_contexts(binding),
        )

    assert original.plan_ids
    assert len(store._outbox) == outbox_count


def test_promoted_portfolio_rejects_changed_authorized_allowlist() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = ["IBM", "MSFT", "AAPL"]
    store.seed_binding(binding)
    event = _event()
    original_service = _service(store, promotion_scope=_promotion_scope(event))
    original = original_service.ingest(event, provider_contexts=_contexts(binding))
    outbox_count = len(store._outbox)
    instruments = {1: "IBM", 2: "MSFT", 3: "AAPL", 4: "GOOG"}
    scope = replace(
        _promotion_scope(event),
        instruments=tuple(sorted(instruments.items())),
        instrument_set_sha256=paper_promotion_instrument_set_sha256(instruments),
    )

    changed_service = _service(store, promotion_scope=scope)
    assert changed_service.preflight_exact_replay(event) is None
    with pytest.raises(ValueError, match="one active binding with the exact"):
        changed_service.ingest(
            event,
            provider_contexts=_contexts(binding),
        )

    assert original.plan_ids
    assert len(store._outbox) == outbox_count


def test_atomic_batch_fans_out_isolated_ordered_account_plans_and_replays() -> None:
    store = InMemoryScoreStore()
    first = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    second = _binding(user_id="tenant-b", binding_id=8, account_id=12)
    second.asset_filter = ["IBM"]
    store.seed_binding(first)
    store.seed_binding(second)
    service = _service(store)
    event = _event()

    result = service.ingest(event, provider_contexts=_contexts(first, second))

    assert result.replayed is False
    assert len(result.plan_ids) == 2
    assert len(set(result.plan_ids)) == 2
    commands = [
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    ]
    assert len(commands) == 2
    assert {record.payload["user_id"] for record in commands} == {"tenant-a", "tenant-b"}
    tenant_a = next(record for record in commands if record.payload["user_id"] == "tenant-a")
    assert [leg["phase"] for leg in tenant_a.payload["legs"]] == ["exit", "entry"]
    assert tenant_a.payload["legs"][1]["depends_on_sequences"] == [0]
    assert [leg["external_signal_id"] for leg in tenant_a.payload["legs"]] == [
        "rebalance-external-0",
        "rebalance-external-1",
    ]
    assert all(
        not leg["external_signal_id"].startswith("rebalance-signal-")
        for leg in tenant_a.payload["legs"]
    )
    assert tenant_a.available_at == _EXECUTE_AT
    tenant_b = next(record for record in commands if record.payload["user_id"] == "tenant-b")
    assert [leg["phase"] for leg in tenant_b.payload["legs"]] == ["exit"]
    assert not [record for record in store._outbox.values() if record.topic == "signals.scored"]
    assert not [record for record in store._outbox.values() if record.topic == "signals.ingested"]
    persisted_signals = {item.symbol: item for item in store.list_all_signals()}
    assert persisted_signals["MSFT"].expected_return == 0.01
    assert persisted_signals["MSFT"].predicted_risk == 0.02
    assert persisted_signals["MSFT"].metadata["scoring_input_source"] == "explicit"

    outbox_count = len(store._outbox)
    preflight = service.preflight_exact_replay(event)
    assert preflight is not None
    assert preflight.replayed is True
    assert preflight.plan_ids == result.plan_ids
    replay = service.ingest(event, provider_contexts=_contexts(first, second))
    assert replay.replayed is True
    assert replay.plan_ids == result.plan_ids
    assert len(store._outbox) == outbox_count


def test_personal_provider_evidence_fans_out_only_to_its_entitlement_owner() -> None:
    store = InMemoryScoreStore()
    owner = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    other = _binding(user_id="tenant-b", binding_id=8, account_id=12)
    store.seed_binding(owner)
    store.seed_binding(other)
    event = _event()
    store.seed_rebalance_entitlement_owner(
        provider_authority_sha256=event.provider_authority_sha256,
        user_id=owner.user_id,
    )

    result = _service(store).ingest(
        event,
        provider_contexts=_contexts(owner, other),
    )

    assert len(result.plan_ids) == 1
    commands = [
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    ]
    assert [record.payload["user_id"] for record in commands] == [owner.user_id]
    assert not [record for record in store._outbox.values() if record.topic == "signals.scored"]
    assert not [record for record in store._outbox.values() if record.topic == "signals.ingested"]


class _FailingPlanStore(InMemoryScoreStore):
    def persist_account_rebalance_plan(self, draft: AccountRebalancePlanDraft) -> str:
        super().persist_account_rebalance_plan(draft)
        message = "forced plan persistence failure"
        raise ValueError(message)


def test_invalid_account_plan_rolls_back_signals_scores_model_and_outbox() -> None:
    store = _FailingPlanStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    store.seed_binding(binding)

    with pytest.raises(ValueError, match="forced plan persistence failure"):
        _service(store).ingest(_event(), provider_contexts=_contexts(binding))

    assert store.list_all_signals() == []
    assert store._scores == {}
    assert store._model_rebalances == {}
    assert store._account_rebalance_plans == {}
    assert store._outbox == {}


@pytest.mark.parametrize("data_use_scope", ["historical_validation", "live_forward"])
def test_nonpaper_scope_cannot_create_plan_or_outbox(data_use_scope: str) -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    store.seed_binding(binding)

    with pytest.raises(ValueError, match="paper_forward"):
        _service(store).ingest(
            _event(data_use_scope=data_use_scope),
            provider_contexts=_contexts(binding),
        )

    assert store._account_rebalance_plans == {}
    assert store._outbox == {}
    assert store.list_all_signals() == []


def test_account_command_remains_unclaimable_before_official_execution_time() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    store.seed_binding(binding)
    _service(store).ingest(_event(), provider_contexts=_contexts(binding))

    assert (
        store.claim_outbox_batch(
            topics=["execution.rebalance.commands"],
            consumer="execution-test",
        )
        == []
    )
    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert command.available_at > datetime.now(tz=UTC) + timedelta(days=1)


def test_manual_binding_suppresses_entry_but_preserves_risk_reducing_exit() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.autopilot = False
    store.seed_binding(binding)

    result = _service(store).ingest(_event(), provider_contexts=_contexts(binding))

    assert len(result.plan_ids) == 1
    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert [leg["phase"] for leg in command.payload["legs"]] == ["exit"]
    assert command.payload["execution_policy"]["autopilot"] is False


@pytest.mark.parametrize("threshold_field", ["sector_score_threshold", "market_score_threshold"])
def test_unsealed_hierarchy_threshold_rejects_target_but_preserves_exit(
    threshold_field: str,
) -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    setattr(binding, threshold_field, 0.1)
    store.seed_binding(binding)

    result = _service(store).ingest(_event(), provider_contexts=_contexts(binding))

    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert [leg["symbol"] for leg in command.payload["legs"]] == ["IBM"]
    draft = store._account_rebalance_plans[result.plan_ids[0]]
    target = next(leg for leg in draft.legs if leg.symbol == "MSFT")
    assert target.disposition == "rejected"
    assert target.reason_code == "point-in-time_hierarchy_score_context_unavailable"


@pytest.mark.parametrize("authority_field", ["autopilot", "entries_enabled"])
def test_disabled_entry_authority_preserves_hold_and_allows_only_negative_target_delta(
    authority_field: str,
) -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    setattr(binding, authority_field, False)
    store.seed_binding(binding)

    result = _service(store).ingest(
        _event(incumbent_hold=True),
        provider_contexts=_contexts(binding),
    )

    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert [leg["phase"] for leg in command.payload["legs"]] == ["exit", "entry"]
    assert command.payload["intentional_cash_slots"] == 0
    draft = store._account_rebalance_plans[result.plan_ids[0]]
    incumbent = next(leg for leg in draft.legs if leg.symbol == "MSFT")
    assert incumbent.disposition == "selected"
    assert (
        incumbent.reason_code
        == f"{'manual_approval_required' if authority_field == 'autopilot' else 'entry_authority_disabled'}_risk_reduction_only"
    )
    assert command.payload["execution_policy"][authority_field] is False


def test_disabled_entry_and_exit_authority_audits_nonexecutable_hold_preservation() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.autopilot = False
    binding.exits_enabled = False
    store.seed_binding(binding)

    result = _service(store).ingest(
        _event(incumbent_hold=True),
        provider_contexts=_contexts(binding),
    )

    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert command.payload["legs"] == []
    draft = store._account_rebalance_plans[result.plan_ids[0]]
    incumbent = next(leg for leg in draft.legs if leg.symbol == "MSFT")
    assert incumbent.disposition == "rejected"
    assert incumbent.reason_code == "manual_approval_required_hold_preserved"


def test_max_open_positions_keeps_highest_ranked_entry_and_audits_rejection() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.risk_caps = {"max_open_positions": 1}
    store.seed_binding(binding)
    service = _service(store)
    event = _event(include_second_entry=True)

    result = service.ingest(event, provider_contexts=_contexts(binding))

    draft = store._account_rebalance_plans[result.plan_ids[0]]
    assert [leg.symbol for leg in draft.legs if leg.action == "entry"] == ["MSFT"]
    rejected = next(leg for leg in draft.legs if leg.symbol == "AAPL")
    assert rejected.disposition == "rejected"
    assert rejected.reason_code == "max_open_positions"
    model_leg = next(leg for leg in event.legs if leg.leg_id == rejected.model_leg_id)
    assert rejected.model_signal_snapshot_sha256 == canonical_json_hash(
        model_leg.signal.model_dump(mode="json")
    )


def test_provider_context_account_mismatch_rolls_back_entire_batch() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    store.seed_binding(binding)
    contexts = _contexts(binding)
    contexts.strategy_configs[(binding.user_id, binding.binding_id, binding.broker_account_id)][
        "broker_account_id"
    ] = 99

    with pytest.raises(ValueError, match="conflicts with configured account"):
        _service(store).ingest(_event(), provider_contexts=contexts)

    assert store.list_all_signals() == []
    assert store._account_rebalance_plans == {}
    assert store._outbox == {}


def test_ibkr_paper_broker_route_is_frozen_for_plan_execution() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.allowed_brokers = ["ibkr"]
    store.seed_binding(binding)
    contexts = _contexts(binding)
    account = contexts.profiles[binding.user_id]["accounts"]["11"]
    assert isinstance(account, dict)
    account["broker"] = "ibkr"
    config = contexts.strategy_configs[
        (binding.user_id, binding.binding_id, binding.broker_account_id)
    ]
    config["broker"] = "ibkr"
    config["allowed_brokers"] = ["ibkr"]

    result = _service(store).ingest(_event(), provider_contexts=contexts)

    command = store._account_rebalance_plans[result.plan_ids[0]].command
    assert command.data_use_scope == "paper_forward"
    assert command.broker_route.broker == "ibkr"
    assert command.broker_route.broker_environment == "paper"
    assert command.broker_route.live_enabled is False
    assert command.broker_route.sandbox is True
    assert command.broker_route.broker_account_id == binding.broker_account_id
    assert command.execution_policy.user_id == binding.user_id
    assert command.execution_policy.binding_id == binding.binding_id


def test_unsupported_sandbox_broker_route_is_rejected_before_plan_persistence() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.allowed_brokers = ["coinbase"]
    store.seed_binding(binding)
    contexts = _contexts(binding)
    account = contexts.profiles[binding.user_id]["accounts"]["11"]
    assert isinstance(account, dict)
    account["broker"] = "coinbase"
    config = contexts.strategy_configs[
        (binding.user_id, binding.binding_id, binding.broker_account_id)
    ]
    config["broker"] = "coinbase"
    config["allowed_brokers"] = ["coinbase"]

    with pytest.raises(ValueError, match="sandboxed paper or IBKR paper route"):
        _service(store).ingest(_event(), provider_contexts=contexts)

    assert store._account_rebalance_plans == {}
    assert store._outbox == {}
    assert store.list_all_signals() == []


def test_promoted_zero_leg_all_cash_batch_uses_manifest_asset_scope() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    binding.asset_filter = ["IBM", "MSFT", "AAPL"]
    store.seed_binding(binding)
    event = _event(all_cash=True)

    result = _service(store, promotion_scope=_promotion_scope(event)).ingest(
        event,
        provider_contexts=_contexts(binding),
    )

    assert result.canonical_signal_count == 0
    assert len(result.plan_ids) == 1
    draft = store._account_rebalance_plans[result.plan_ids[0]]
    assert draft.legs == ()
    assert draft.command.expected_leg_count == 0
    assert draft.command.intentional_cash_slots == 30
    command = next(
        record
        for record in store._outbox.values()
        if record.topic == "execution.rebalance.commands"
    )
    assert command.payload["legs"] == []
    assert command.payload["intentional_cash_slots"] == 30


def test_http_endpoint_accepts_direct_typed_model_event() -> None:
    store = InMemoryScoreStore()
    binding = _binding(user_id="tenant-a", binding_id=7, account_id=11)
    store.seed_binding(binding)
    engine = ScoreEngine(store)
    for symbol in ("IBM", "MSFT"):
        engine.pipeline.set_market_context(
            symbol,
            MarketContext(
                regime=MarketRegime.BULL_QUIET,
                as_of=_CUTOFF,
                realized_vol_20d=0.01,
            ),
        )
    dispatcher = ExecutionDispatcher(
        "http://execution-engine",
        store=store,
        runtime_mode="paper",
    )
    contexts = _contexts(binding)
    app = create_app(
        engine,
        dispatcher=dispatcher,
        user_profiles=contexts.profiles,
        user_strategy_configs={
            int(binding.binding_id or 0): contexts.strategy_configs[
                (binding.user_id, binding.binding_id, binding.broker_account_id)
            ]
        },
    )

    response = TestClient(app).post(
        "/api/v1/model-rebalances",
        json=_event().model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["canonical_signal_count"] == 2

    provider_calls: list[tuple[object, ...]] = []

    def _unavailable_provider(*args: object) -> dict[str, object]:
        provider_calls.append(args)
        raise RuntimeError("provider unavailable")

    replay_app = create_app(
        engine,
        dispatcher=ExecutionDispatcher(
            "http://execution-engine",
            profile_provider=_unavailable_provider,
            strategy_config_provider=_unavailable_provider,
            store=store,
            runtime_mode="paper",
        ),
    )
    replay_response = TestClient(replay_app).post(
        "/api/v1/model-rebalances",
        json=_event().model_dump(mode="json"),
    )

    assert replay_response.status_code == 200
    assert replay_response.json()["replayed"] is True
    assert provider_calls == []
