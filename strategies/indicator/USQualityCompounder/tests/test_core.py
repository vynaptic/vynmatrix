"""Focused contracts for the disabled US quality-compounder core."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from indicator_runner.model_rebalance_projection import build_model_rebalance_event
from USQualityCompounder.panel import (
    USQualityCompounderPanelInput,
    panel_input_from_payload,
    panel_input_to_payload,
)
from lib_common.hashing import canonical_json_hash
from lib_application.db.models import StrategyPanelDecision
from lib_strategy.cross_sectional import CrossSectionalEntity, FactorObservation
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.equity_quality_compounder import (
    MarketCapBucket,
    QualityCompounderPolicy,
    QualityCompounderSecurity,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelAuditDecision,
    PanelExclusion,
    PanelObservationRef,
    PanelReadyInput,
    SessionAuthority,
)
from lib_strategy.signals.emitter import BacktestSignalEmitter
from lib_strategy.signals.loading import load_pure_strategy_core
from lib_strategy.signals.signal import SignalAction

USQualityCompounderCore = load_pure_strategy_core(Path(__file__).resolve().parents[1])


def _config() -> dict[str, object]:
    return {
        "asset_class": "equity",
        "evaluation_horizon": "3m",
        "signal_source": "paper",
        "strategy_version": "0.2.0",
        "target_holdings": "15",
        "trade_direction_mode": "long_only",
        "universe": "SP500",
        "universe_contract": "point_in_time_sp500_membership",
    }


def _session(session_date: date) -> OfficialSessionCutoff:
    opens_at = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        13,
        30,
        tzinfo=UTC,
    )
    closes_at = opens_at.replace(hour=20, minute=0)
    return OfficialSessionCutoff(
        mic="XNYS",
        session_date=session_date,
        opens_at=opens_at,
        closes_at=closes_at,
        authority=SessionAuthority.OFFICIAL_EXCHANGE,
        source_identity=f"test-xnys:{session_date.isoformat()}",
        content_sha256=canonical_json_hash(
            {
                "closes_at": closes_at.isoformat(),
                "opens_at": opens_at.isoformat(),
            }
        ),
    )


def _authority() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="quality-compounder-paper-test-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="eodhd",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("personal_all_in_one",),
                entitlement_owner_user_id="test-owner",
            ),
            ProviderAuthorityRule(
                provider="sec",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public_filings",),
            ),
        ),
    )


def _panel(
    values: tuple[float, ...],
    *,
    quarter: int = 1,
    quarter_end: bool = True,
) -> USQualityCompounderPanelInput:
    if quarter == 1:
        decision_date, execution_date = (
            (date(2026, 6, 30), date(2026, 7, 1))
            if quarter_end
            else (date(2026, 6, 29), date(2026, 6, 30))
        )
    else:
        decision_date, execution_date = date(2026, 9, 30), date(2026, 10, 1)
    decision_session = _session(decision_date)
    execution_session = _session(execution_date)
    cutoff = decision_session.closes_at.replace(minute=5)
    policy = QualityCompounderPolicy()
    entities: list[CrossSectionalEntity] = []
    observations: list[FactorObservation] = []
    provisional: list[QualityCompounderSecurity] = []
    for index, value in enumerate(values):
        entity_id = f"security-{index:02d}"
        symbol = f"S{index:02d}"
        sector = f"sector:{index:02d}"
        industry = f"industry:{index:02d}"
        entities.append(
            CrossSectionalEntity(
                entity_id=entity_id,
                symbol=symbol,
                peer_groups=(industry, sector),
            )
        )
        observations.extend(
            FactorObservation(
                entity_id=entity_id,
                factor_name=spec.name,
                raw_value=value,
                source_observation_ids=(f"source:{quarter}:{entity_id}:{spec.name}",),
            )
            for spec in policy.factor_specs
            if spec.enabled
        )
        provisional.append(
            QualityCompounderSecurity(
                entity_id=entity_id,
                instrument_id=index + 1,
                symbol=symbol,
                factor_snapshot_id=canonical_json_hash(
                    {"factor_snapshot": entity_id, "quarter": quarter}
                ),
                sector=sector,
                industry=industry,
                market_cap_bucket=MarketCapBucket.LARGE,
                sector_score=1.0,
                industry_score=1.0,
                reference_price=100.0 + index,
                expected_round_trip_cost_bps=10.0,
            )
        )
    securities = tuple(provisional)
    members = tuple(
        EffectivePanelMember(
            security_id=security.entity_id,
            issuer_id=f"issuer:{security.entity_id}",
            instrument_id=security.instrument_id,
            canonical_symbol=security.symbol,
        )
        for security in securities
    )
    panel_observations = tuple(
        PanelObservationRef(
            security_id=security.entity_id,
            observation_id=f"quality-panel:{quarter}:{security.entity_id}",
            observed_at=decision_session.closes_at,
            available_at=cutoff,
            content_revision=1,
            content_sha256=canonical_json_hash(
                {"evidence_observation": security.entity_id, "quarter": quarter}
            ),
        )
        for security in securities
    )
    authority = _authority()
    generic = PanelReadyInput(
        cutoff=cutoff,
        session=decision_session,
        execution_session=execution_session,
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=authority,
        provider_authority_sha256=authority.digest,
        membership_sha256=canonical_json_hash(
            [(item.security_id, item.instrument_id, item.canonical_symbol) for item in members]
        ),
        factor_snapshot_sha256=canonical_json_hash(
            {
                "schema": "equity-factor-panel-v1",
                "snapshots": [
                    {
                        "instrument_id": security.instrument_id,
                        "factor_snapshot_id": security.factor_snapshot_id,
                        "completeness_status": "complete",
                        "content_sha256": canonical_json_hash(
                            {
                                "factor_content": security.entity_id,
                                "quarter": quarter,
                            }
                        ),
                    }
                    for security in securities
                ],
            }
        ),
        members=members,
        observations=panel_observations,
    )
    return USQualityCompounderPanelInput(
        panel=generic,
        entries_allowed=True,
        entities=tuple(entities),
        factor_observations=tuple(observations),
        securities=securities,
    )


def _exclude_security(
    panel_input: USQualityCompounderPanelInput,
    *,
    entity_id: str,
    quarter: int,
) -> USQualityCompounderPanelInput:
    excluded = next(item for item in panel_input.securities if item.entity_id == entity_id)
    snapshot_content = {
        item.entity_id: canonical_json_hash({"factor_content": item.entity_id, "quarter": quarter})
        for item in panel_input.securities
    }
    generic = replace(
        panel_input.panel,
        factor_snapshot_sha256=canonical_json_hash(
            {
                "schema": "equity-factor-panel-v1",
                "snapshots": [
                    {
                        "instrument_id": item.instrument_id,
                        "factor_snapshot_id": item.factor_snapshot_id,
                        "completeness_status": (
                            "incomplete" if item.entity_id == entity_id else "complete"
                        ),
                        "content_sha256": snapshot_content[item.entity_id],
                    }
                    for item in panel_input.securities
                ],
            }
        ),
        observations=tuple(
            item for item in panel_input.panel.observations if item.security_id != entity_id
        ),
        exclusions=(
            PanelExclusion(
                security_id=entity_id,
                reason_code="incomplete_factor_snapshot",
                disposition_identity=excluded.factor_snapshot_id,
                content_sha256=snapshot_content[entity_id],
            ),
        ),
    )
    return replace(
        panel_input,
        panel=generic,
        entities=tuple(item for item in panel_input.entities if item.entity_id != entity_id),
        factor_observations=tuple(
            item for item in panel_input.factor_observations if item.entity_id != entity_id
        ),
        securities=tuple(item for item in panel_input.securities if item.entity_id != entity_id),
    )


def test_panel_codec_round_trips_and_rejects_type_coercion() -> None:
    panel = _panel(tuple(float(20 - index) for index in range(20)))
    payload = panel_input_to_payload(panel)

    assert panel_input_from_payload(payload) == panel

    tampered = deepcopy(payload)
    tampered["factor_observations"][0]["raw_value"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="raw_value"):
        panel_input_from_payload(tampered)


def test_quarterly_evaluation_emits_only_the_qualified_target_book() -> None:
    emitter = BacktestSignalEmitter()
    core = USQualityCompounderCore(config=_config(), emitter=emitter)

    audit = core.evaluate_panel(_panel(tuple(float(20 - index) for index in range(20))))
    signals = emitter.get_signals()

    assert [signal.symbol for signal in signals] == ["S00", "S01"]
    assert {signal.action for signal in signals} == {SignalAction.LONG}
    assert all(signal.size_hint == pytest.approx(1.0 / 15.0) for signal in signals)
    assert [signal.metadata["model_rebalance_sequence"] for signal in signals] == [0, 1]
    assert all(signal.metadata["model_rebalance_expected_leg_count"] == 2 for signal in signals)
    assert all(
        signal.metadata["configuration_sha256"] == core.configuration_sha256 for signal in signals
    )
    assert all(signal.metadata["target_weight_drift_fraction"] == 0.0 for signal in signals)
    composite_by_id = {
        row.entity_id: row.composite_score for row in audit.rows if row.rank_complete
    }
    assert all(
        signal.metadata["composite_score"]
        == pytest.approx(composite_by_id[signal.metadata["security_id"]])
        for signal in signals
    )
    assert audit.intentional_cash_slots == 13
    assert [row.decision for row in audit.rows if row.strategy_eligible] == [
        PanelAuditDecision.SELECTED,
        PanelAuditDecision.SELECTED,
    ]


def test_target_book_projects_through_the_canonical_rebalance_event() -> None:
    panel_input = _panel(tuple(float(20 - index) for index in range(20)))
    emitter = BacktestSignalEmitter()
    core = USQualityCompounderCore(config=_config(), emitter=emitter)
    audit = core.evaluate_panel(panel_input)
    strategy_input_sha256 = canonical_json_hash(panel_input_to_payload(panel_input))
    identity = SimpleNamespace(
        strategy_id="us_quality_compounder_v1",
        strategy_version="0.2.0",
    )
    panel_row = cast(
        StrategyPanelDecision,
        SimpleNamespace(
            decision_key="quality-compounder-test-decision",
            strategy_input_sha256=strategy_input_sha256,
        ),
    )

    event = build_model_rebalance_event(
        identity=identity,
        panel_row=panel_row,
        panel=panel_input.panel,
        audit=audit,
        signals=emitter.get_signals(),
        rank_snapshot_id=audit.rank_snapshot.content_digest,
    )

    assert event.expected_leg_count == 2
    assert [leg.phase for leg in event.legs] == ["entry", "entry"]
    assert [leg.signal.symbol for leg in event.legs] == ["S00", "S01"]


def test_rotation_emits_exits_before_entries_and_persists_new_incumbents() -> None:
    emitter = BacktestSignalEmitter()
    core = USQualityCompounderCore(config=_config(), emitter=emitter)
    core.evaluate_panel(_panel(tuple(float(20 - index) for index in range(20))))
    emitter.clear()

    audit = core.evaluate_panel(_panel(tuple(float(index + 1) for index in range(20)), quarter=2))
    signals = emitter.get_signals()

    assert [(signal.action, signal.symbol) for signal in signals] == [
        (SignalAction.CLOSE, "S00"),
        (SignalAction.CLOSE, "S01"),
        (SignalAction.LONG, "S19"),
        (SignalAction.LONG, "S18"),
    ]
    assert [signal.metadata["model_rebalance_phase"] for signal in signals] == [
        "exit",
        "exit",
        "entry",
        "entry",
    ]
    assert [signal.metadata["model_rebalance_sequence"] for signal in signals] == list(range(4))
    assert set(core.serialize_model_state()["symbol_states"]) == {
        "S18",
        "S19",
    }
    assert audit.intentional_cash_slots == 13


def test_restored_incumbents_emit_holds_instead_of_reentries() -> None:
    original = USQualityCompounderCore(config=_config(), emitter=BacktestSignalEmitter())
    original.evaluate_panel(_panel(tuple(float(20 - index) for index in range(20))))
    state = original.serialize_model_state()
    emitter = BacktestSignalEmitter()
    restored = USQualityCompounderCore(config=_config(), emitter=emitter)
    restored.bootstrap_history([])
    restored.restore_model_state(state)

    restored.evaluate_panel(_panel(tuple(float(20 - index) for index in range(20)), quarter=2))

    assert [(signal.action, signal.symbol) for signal in emitter.get_signals()] == [
        (SignalAction.HOLD, "S00"),
        (SignalAction.HOLD, "S01"),
    ]


def test_explicit_factor_exclusion_exits_an_incumbent_with_prior_lineage() -> None:
    emitter = BacktestSignalEmitter()
    core = USQualityCompounderCore(config=_config(), emitter=emitter)
    core.evaluate_panel(_panel(tuple(float(20 - index) for index in range(20))))
    emitter.clear()
    current = _exclude_security(
        _panel(tuple(float(20 - index) for index in range(20)), quarter=2),
        entity_id="security-00",
        quarter=2,
    )

    audit = core.evaluate_panel(current)
    signals = emitter.get_signals()

    assert [(signal.action, signal.symbol) for signal in signals] == [
        (SignalAction.CLOSE, "S00"),
        (SignalAction.HOLD, "S01"),
        (SignalAction.LONG, "S02"),
    ]
    exit_signal = signals[0]
    assert exit_signal.metadata["current_rank_row_present"] is True
    assert exit_signal.metadata["current_rank_factor_snapshot_id"] is None
    assert exit_signal.metadata["membership_change_reason"] == "panel_excluded"
    assert exit_signal.metadata["composite_score"] is None
    excluded_row = next(row for row in audit.rows if row.entity_id == "security-00")
    assert excluded_row.decision is PanelAuditDecision.EXCLUDED
    assert excluded_row.incumbent is True
    assert excluded_row.exclusion_reason == "panel_excluded"


def test_non_quarter_panel_fails_before_emission_or_state_mutation() -> None:
    emitter = BacktestSignalEmitter()
    core = USQualityCompounderCore(config=_config(), emitter=emitter)

    with pytest.raises(ValueError, match="quarter"):
        core.evaluate_panel(
            _panel(
                tuple(float(20 - index) for index in range(20)),
                quarter_end=False,
            )
        )

    assert not emitter.get_signals()
    assert core.serialize_model_state()["symbol_states"] == {}


def test_core_rejects_a_parallel_or_ambiguous_universe_contract() -> None:
    config = _config()
    config["universe_contract"] = "point_in_time_unknown"

    with pytest.raises(ValueError, match="universe_contract"):
        USQualityCompounderCore(config=config)
