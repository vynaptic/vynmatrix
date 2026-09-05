"""Stable semantic identity tests for authoritative market sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from lib_application.db.models import (
    EquitySourceLineage,
    Instrument,
    MarketCalendar,
    MarketSession,
)
from lib_application.services.strategy_panel_sessions import (
    StrategyPanelSessionError,
    market_session_content_sha256,
    market_session_source_identity,
    validate_strategy_panel_sessions,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)
from lib_strategy.panels import (
    EffectivePanelMember,
    OfficialSessionCutoff,
    PanelReadyInput,
    SessionAuthority,
)

_OPEN = datetime(2025, 2, 3, 14, 30, tzinfo=UTC)
_CLOSE = datetime(2025, 2, 3, 21, 0, tzinfo=UTC)


def _calendar() -> MarketCalendar:
    return MarketCalendar(
        calendar_id=1,
        code="XNYS:REGULAR",
        source_kind="exchange",
        provider="nyse",
        source_reference="https://www.nyse.com/markets/hours-calendars",
        observation_id="1" * 64,
        coverage_start=_OPEN - timedelta(days=30),
        coverage_end=_CLOSE + timedelta(days=30),
        observed_at=_OPEN - timedelta(hours=1),
    )


def _window() -> MarketSession:
    return MarketSession(
        session_id=1,
        calendar_id=1,
        opens_at=_OPEN,
        closes_at=_CLOSE,
    )


def _lineage() -> EquitySourceLineage:
    return EquitySourceLineage(
        lineage_id="2" * 64,
        provider="nyse",
        product="regular-session-calendar",
        endpoint="https://www.nyse.com/markets/hours-calendars",
        dataset_version="2025-02-03",
        tool_version="calendar-ingestor-v1",
        source_identity="XNYS:REGULAR",
        source_revision="1",
        retrieved_at=_OPEN - timedelta(hours=1),
        timestamp_semantics={"opens_at": "UTC", "closes_at": "UTC"},
        adjustment_policy="not-applicable",
        entitlement_scope="public",
        missing_data_policy="fail-closed",
        content_sha256="3" * 64,
    )


def test_session_digest_survives_coverage_extension_and_lineage_revision() -> None:
    calendar = _calendar()
    window = _window()
    lineage = _lineage()
    original = market_session_content_sha256(calendar, window, lineage)

    calendar.observation_id = "4" * 64
    calendar.coverage_start = _OPEN - timedelta(days=365)
    calendar.coverage_end = _CLOSE + timedelta(days=365)
    calendar.observed_at = _OPEN + timedelta(days=1)
    lineage.lineage_id = "5" * 64
    lineage.dataset_version = "2026-02-03"
    lineage.tool_version = "calendar-ingestor-v2"
    lineage.source_revision = "2"
    lineage.retrieved_at = _OPEN + timedelta(days=1)
    lineage.content_sha256 = "6" * 64

    assert market_session_content_sha256(calendar, window, lineage) == original


def test_session_digest_changes_for_true_session_correction() -> None:
    calendar = _calendar()
    window = _window()
    lineage = _lineage()
    original = market_session_content_sha256(calendar, window, lineage)

    window.closes_at = _CLOSE - timedelta(hours=3)

    assert market_session_content_sha256(calendar, window, lineage) != original


def _forward_session_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MagicMock, PanelReadyInput]:
    decision_window = _window()
    execution_window = MarketSession(
        session_id=2,
        calendar_id=1,
        opens_at=datetime(2025, 2, 4, 14, 30, tzinfo=UTC),
        closes_at=datetime(2025, 2, 4, 21, 0, tzinfo=UTC),
    )
    knowledge_cutoff = _CLOSE + timedelta(minutes=30)
    calendar = _calendar()
    calendar.code = "XNYS"
    calendar.source_reference = "XNYS:regular-session-contract-fixture"
    calendar.coverage_end = execution_window.closes_at + timedelta(hours=1)
    calendar.observed_at = knowledge_cutoff
    lineage = _lineage()
    lineage.source_identity = calendar.source_reference
    instrument = Instrument(
        instr_id=1,
        asset_class="equity",
        canonical="AAA",
        settlement_currency="USD",
        is_tradable=True,
        market_session_policy="scheduled",
        market_calendar_id=calendar.calendar_id,
    )
    policy = ProviderAuthorityPolicy(
        policy_version="paper-forward-session-test-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=(
            ProviderAuthorityRule(
                provider="nyse",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("public",),
            ),
        ),
    )

    def session_contract(window: MarketSession) -> OfficialSessionCutoff:
        return OfficialSessionCutoff(
            mic="XNYS",
            session_date=window.opens_at.date(),
            opens_at=window.opens_at,
            closes_at=window.closes_at,
            authority=SessionAuthority.OFFICIAL_EXCHANGE,
            source_identity=market_session_source_identity(calendar),
            content_sha256=market_session_content_sha256(calendar, window, lineage),
        )

    panel = PanelReadyInput(
        cutoff=knowledge_cutoff,
        session=session_contract(decision_window),
        execution_session=session_contract(execution_window),
        data_use_scope=DataUseScope.PAPER_FORWARD,
        provider_authority_policy=policy,
        provider_authority_sha256=policy.digest,
        membership_sha256="7" * 64,
        factor_snapshot_sha256="8" * 64,
        members=(EffectivePanelMember("security:AAA", "issuer:AAA", 1, "AAA"),),
        observations=(),
    )
    database = MagicMock(spec=Session)
    database.scalars.side_effect = ([instrument], [decision_window, execution_window])
    database.get.return_value = calendar
    database.scalar.return_value = None
    calendar_observation = SimpleNamespace(source_record_identity=calendar.source_reference)
    monkeypatch.setattr(
        "lib_application.services.strategy_panel_sessions.validate_equity_observation_authority",
        lambda *_args, **_kwargs: (calendar_observation, lineage),
    )
    return database, panel


def test_persisted_sessions_accept_after_close_knowledge_before_next_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, panel = _forward_session_boundary(monkeypatch)

    validate_strategy_panel_sessions(
        database,
        panel=panel,
        now=panel.cutoff,
    )


def test_persisted_sessions_still_reject_an_intervening_official_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, panel = _forward_session_boundary(monkeypatch)
    database.scalar.return_value = 99

    with pytest.raises(StrategyPanelSessionError, match="not the next persisted official session"):
        validate_strategy_panel_sessions(
            database,
            panel=panel,
            now=panel.cutoff,
        )
