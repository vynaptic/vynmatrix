"""Cutoff and canonical-source-series tests for equity observation authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from lib_application.db.models import (
    Base,
    EquityObservation,
    EquityObservationValue,
    EquitySourceLineage,
    Instrument,
    User,
)
from lib_application.services.equity_lineage import (
    OWNER_SCOPED_DELAYED_BBO_CONTRACT,
    OWNER_SCOPED_DELAYED_BBO_EXECUTION_AUTHORITY,
    EquityObservationAuthorityError,
    canonical_equity_quote_decimal,
    equity_observation_semantic_sha256,
    load_owner_scoped_delayed_bbo,
    owner_scoped_delayed_bbo_sha256,
    validate_equity_observation_authority,
)
from lib_strategy.data_authority import (
    DataUseScope,
    ProviderAuthorityDecision,
    ProviderAuthorityPolicy,
    ProviderAuthorityRule,
)

_CUTOFF = datetime(2026, 7, 31, 20, tzinfo=UTC)


def _policy() -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="authority-v1",
        data_use_scope=DataUseScope.PAPER_FORWARD,
        rules=tuple(
            ProviderAuthorityRule(
                provider=provider,
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("licensed",),
            )
            for provider in ("provider-a", "provider-b")
        ),
    )


def _personal_policy(owner_user_id: str) -> ProviderAuthorityPolicy:
    return ProviderAuthorityPolicy(
        policy_version="personal-authority-v1",
        data_use_scope=DataUseScope.HISTORICAL_VALIDATION,
        rules=(
            ProviderAuthorityRule(
                provider="provider-a",
                decision=ProviderAuthorityDecision.ALLOW,
                entitlement_scopes=("licensed",),
            ),
        ),
        entitlement_owner_user_id=owner_user_id,
    )


def _lineage(
    digest_character: str,
    *,
    provider: str = "provider-a",
    entitlement_scope: str = "licensed",
    retrieved_at: datetime = _CUTOFF,
    source_identity: str = "XNYS/regular-hours",
    entitlement_owner_user_id: str | None = None,
) -> EquitySourceLineage:
    return EquitySourceLineage(
        lineage_id=digest_character * 64,
        provider=provider,
        product="official-calendar",
        endpoint="https://provider.example/calendar",
        dataset_version="2026-v1",
        tool_version="calendar-ingestor-v1",
        source_identity=source_identity,
        source_revision=f"revision-{digest_character}",
        retrieved_at=retrieved_at,
        timestamp_semantics={"available_at": "provider publication timestamp"},
        adjustment_policy="not-applicable",
        entitlement_scope=entitlement_scope,
        entitlement_owner_user_id=entitlement_owner_user_id,
        missing_data_policy="fail-closed",
        content_sha256=digest_character * 64,
    )


def _observation(
    digest_character: str,
    *,
    lineage_id: str,
    revision: int,
    source_record_identity: str = "XNYS/2026-07-31",
    supersedes_observation_id: str | None = None,
    available_at: datetime | None = None,
) -> EquityObservation:
    return EquityObservation(
        observation_id=digest_character * 64,
        lineage_id=lineage_id,
        instr_id=None,
        observation_kind="calendar",
        source_record_identity=source_record_identity,
        event_at=_CUTOFF - timedelta(hours=2),
        available_at=available_at or _CUTOFF - timedelta(hours=1),
        revision=revision,
        supersedes_observation_id=supersedes_observation_id,
        disposition="observed",
        content_sha256=digest_character * 64,
    )


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def test_cross_provider_and_unauthorized_collisions_do_not_supersede() -> None:
    engine = _engine()
    selected_lineage = _lineage("1")
    selected = _observation("2", lineage_id=selected_lineage.lineage_id, revision=1)
    cross_provider_lineage = _lineage("3", provider="provider-b")
    cross_provider = _observation(
        "4",
        lineage_id=cross_provider_lineage.lineage_id,
        revision=2,
    )
    unauthorized_lineage = _lineage("5", entitlement_scope="other-license")
    unauthorized = _observation("6", lineage_id=unauthorized_lineage.lineage_id, revision=3)
    with Session(engine) as session:
        session.add_all(
            (
                selected_lineage,
                cross_provider_lineage,
                unauthorized_lineage,
                selected,
                cross_provider,
                unauthorized,
            )
        )
        session.commit()

        actual, lineage = validate_equity_observation_authority(
            session,
            observation_id=selected.observation_id,
            expected_kind="calendar",
            cutoff=_CUTOFF,
            provider_authority_policy=_policy(),
            expected_instrument_id=None,
        )

    assert actual.observation_id == selected.observation_id
    assert lineage.lineage_id == selected_lineage.lineage_id


def test_authorized_later_revision_in_same_source_series_supersedes() -> None:
    engine = _engine()
    selected_lineage = _lineage("1")
    selected = _observation("2", lineage_id=selected_lineage.lineage_id, revision=1)
    later_lineage = _lineage("3")
    later = _observation("4", lineage_id=later_lineage.lineage_id, revision=2)
    with Session(engine) as session:
        session.add_all((selected_lineage, later_lineage, selected, later))
        session.commit()

        with pytest.raises(
            EquityObservationAuthorityError,
            match="superseded before the cutoff",
        ):
            validate_equity_observation_authority(
                session,
                observation_id=selected.observation_id,
                expected_kind="calendar",
                cutoff=_CUTOFF,
                provider_authority_policy=_policy(),
                expected_instrument_id=None,
            )


def test_explicit_changed_identity_correction_is_rejected() -> None:
    engine = _engine()
    selected_lineage = _lineage("1")
    selected = _observation("2", lineage_id=selected_lineage.lineage_id, revision=1)
    correction_lineage = _lineage("3", source_identity="XNYS/corrected-schedule")
    correction = _observation(
        "4",
        lineage_id=correction_lineage.lineage_id,
        revision=2,
        source_record_identity="XNYS/2026-07-31/correction-1",
        supersedes_observation_id=selected.observation_id,
        available_at=_CUTOFF - timedelta(minutes=30),
    )
    with Session(engine) as session:
        session.add_all((selected_lineage, correction_lineage, selected, correction))
        session.commit()

        with pytest.raises(
            EquityObservationAuthorityError,
            match="correction lineage is inconsistent",
        ):
            validate_equity_observation_authority(
                session,
                observation_id=selected.observation_id,
                expected_kind="calendar",
                cutoff=_CUTOFF,
                provider_authority_policy=_policy(),
                expected_instrument_id=None,
            )


def test_explicit_correction_fork_fails_closed() -> None:
    engine = _engine()
    selected_lineage = _lineage("1")
    selected = _observation("2", lineage_id=selected_lineage.lineage_id, revision=1)
    first_lineage = _lineage("3")
    second_lineage = _lineage("4")
    first = _observation(
        "5",
        lineage_id=first_lineage.lineage_id,
        revision=2,
        supersedes_observation_id=selected.observation_id,
        available_at=_CUTOFF - timedelta(minutes=30),
    )
    second = _observation(
        "6",
        lineage_id=second_lineage.lineage_id,
        revision=3,
        supersedes_observation_id=selected.observation_id,
        available_at=_CUTOFF - timedelta(minutes=15),
    )
    with Session(engine) as session:
        session.add_all((selected_lineage, first_lineage, second_lineage, selected, first, second))
        session.commit()

        with pytest.raises(EquityObservationAuthorityError, match="correction chain forks"):
            validate_equity_observation_authority(
                session,
                observation_id=selected.observation_id,
                expected_kind="calendar",
                cutoff=_CUTOFF,
                provider_authority_policy=_policy(),
                expected_instrument_id=None,
            )


def test_personal_lineage_cannot_cross_authority_owner() -> None:
    engine = _engine()
    lineage = _lineage("1", entitlement_owner_user_id="research-owner")
    observation = _observation("2", lineage_id=lineage.lineage_id, revision=1)
    with Session(engine) as session:
        session.add_all((lineage, observation))
        session.commit()

        with pytest.raises(EquityObservationAuthorityError, match="outside provider authority"):
            validate_equity_observation_authority(
                session,
                observation_id=observation.observation_id,
                expected_kind="calendar",
                cutoff=_CUTOFF,
                provider_authority_policy=_personal_policy("another-user"),
                expected_instrument_id=None,
            )


def test_semantic_digest_ignores_retrieval_and_surrogate_identity() -> None:
    first_lineage = _lineage("1")
    second_lineage = _lineage("2", retrieved_at=_CUTOFF + timedelta(minutes=5))
    second_lineage.source_revision = first_lineage.source_revision
    second_lineage.content_sha256 = first_lineage.content_sha256
    first = _observation("3", lineage_id=first_lineage.lineage_id, revision=1)
    second = _observation("4", lineage_id=second_lineage.lineage_id, revision=1)
    second.content_sha256 = first.content_sha256

    assert equity_observation_semantic_sha256(
        first, first_lineage
    ) == equity_observation_semantic_sha256(second, second_lineage)


def test_owner_scoped_delayed_bbo_reader_is_provider_neutral_and_owner_exact() -> None:
    engine = _engine()
    owner = "research-owner"
    source_content_sha256 = "9" * 64
    last_trade_at = _CUTOFF - timedelta(minutes=2)
    bid_at = last_trade_at - timedelta(seconds=2)
    ask_at = last_trade_at - timedelta(seconds=1)
    snapshot_at = _CUTOFF - timedelta(minutes=1)
    available_at = _CUTOFF - timedelta(seconds=30)
    last_trade_price = Decimal("200.25")
    bid_price = Decimal("200.20")
    ask_price = Decimal("200.30")
    content_sha256 = owner_scoped_delayed_bbo_sha256(
        source_symbol="AAPL.PA",
        exchange="XPAR",
        currency="EUR",
        last_trade_price=last_trade_price,
        last_trade_at=last_trade_at,
        last_trade_size=10,
        bid_price=bid_price,
        bid_size=100,
        bid_at=bid_at,
        ask_price=ask_price,
        ask_size=120,
        ask_at=ask_at,
        snapshot_at=snapshot_at,
        source_content_sha256=source_content_sha256,
        raw_response_sha256="a" * 64,
    )
    lineage = EquitySourceLineage(
        lineage_id="7" * 64,
        provider="provider-a",
        product="delayed-bbo",
        endpoint="https://provider.example/quotes",
        dataset_version="2026-v1",
        tool_version="provider-a-ingestor-v1",
        source_identity="provider-a:delayed:AAPL.PA",
        source_revision=f"snapshot-1-{source_content_sha256}",
        retrieved_at=available_at,
        timestamp_semantics={
            "execution_authority": OWNER_SCOPED_DELAYED_BBO_EXECUTION_AUTHORITY,
        },
        adjustment_policy="unadjusted-quote-snapshot",
        entitlement_scope="personal-paper-only",
        entitlement_owner_user_id=owner,
        missing_data_policy="fail-closed",
        content_sha256=source_content_sha256,
    )
    observation = EquityObservation(
        observation_id="8" * 64,
        lineage_id=lineage.lineage_id,
        instr_id=101,
        observation_kind="price",
        source_record_identity="provider-a:delayed:AAPL.PA:snapshot:1",
        event_at=last_trade_at,
        available_at=available_at,
        revision=1,
        disposition="observed",
        content_sha256=content_sha256,
    )

    def value_row(
        index: int,
        field_name: str,
        value_type: str,
        value: object,
    ) -> EquityObservationValue:
        kwargs: dict[str, object] = {
            "value_id": f"{index:064x}",
            "observation_id": observation.observation_id,
            "field_name": field_name,
            "ordinal": 0,
            "value_type": value_type,
        }
        if value_type == "text":
            kwargs["text_value"] = value
        elif value_type == "decimal":
            kwargs["decimal_value"] = value
        elif value_type == "integer":
            kwargs["integer_value"] = value
        elif value_type == "timestamp":
            kwargs["timestamp_value"] = value
        else:  # pragma: no cover - test rows are declared above
            raise AssertionError(value_type)
        return EquityObservationValue(**kwargs)

    raw_values = (
        ("quote_contract", "text", OWNER_SCOPED_DELAYED_BBO_CONTRACT),
        ("source_content_sha256", "text", source_content_sha256),
        ("symbol", "text", "AAPL.PA"),
        ("exchange", "text", "XPAR"),
        ("currency", "text", "EUR"),
        ("last_trade_price", "decimal", last_trade_price),
        (
            "last_trade_price_canonical",
            "text",
            canonical_equity_quote_decimal(last_trade_price),
        ),
        ("last_trade_at", "timestamp", last_trade_at),
        ("last_trade_size", "integer", 10),
        ("bid_price", "decimal", bid_price),
        ("bid_price_canonical", "text", canonical_equity_quote_decimal(bid_price)),
        ("bid_size", "integer", 100),
        ("bid_at", "timestamp", bid_at),
        ("ask_price", "decimal", ask_price),
        ("ask_price_canonical", "text", canonical_equity_quote_decimal(ask_price)),
        ("ask_size", "integer", 120),
        ("ask_at", "timestamp", ask_at),
        ("snapshot_at", "timestamp", snapshot_at),
        ("raw_response_sha256", "text", "a" * 64),
    )
    values = [
        value_row(index, field_name, value_type, value)
        for index, (field_name, value_type, value) in enumerate(raw_values, start=1)
    ]
    with Session(engine) as session:
        session.add_all(
            (
                User(user_id=owner, email="owner@example.test", base_ccy="EUR"),
                Instrument(
                    instr_id=101,
                    asset_class="equity",
                    canonical="AAPL.PA",
                    exchange="XPAR",
                    settlement_currency="EUR",
                ),
                lineage,
                observation,
                *values,
            )
        )
        session.commit()

        evidence = load_owner_scoped_delayed_bbo(
            session,
            instrument_id=101,
            entitlement_owner_user_id=owner,
            observed_at=_CUTOFF,
            max_staleness=timedelta(minutes=5),
        )
        other_owner = load_owner_scoped_delayed_bbo(
            session,
            instrument_id=101,
            entitlement_owner_user_id="another-owner",
            observed_at=_CUTOFF,
            max_staleness=timedelta(minutes=5),
        )

    assert evidence is not None
    assert evidence.provider == "provider-a"
    assert evidence.source_symbol == "AAPL.PA"
    assert evidence.bid_price == bid_price
    assert evidence.ask_price == ask_price
    assert evidence.source_content_sha256 == source_content_sha256
    assert other_owner is None
