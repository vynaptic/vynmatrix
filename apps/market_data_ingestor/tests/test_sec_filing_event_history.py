"""Tests for the acceptance-time historical diagnostic of the SEC filing gate rules."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from lib_application.db.models import (
    Base,
    Broker,
    Instrument,
    InstrumentBrokerSymbol,
    MarketCalendar,
    User,
)
from lib_application.services.equity_observation_writer import (
    EquityObservationSubmission,
    EquityObservationValueInput,
    persist_equity_observation,
)
from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from lib_strategy.sec_filing_rules import SecFilingGateReason
from market_data_ingestor import sec_filing_event_history as history
from market_data_ingestor.equity_evidence import EODHD_PAPER_ENTITLEMENT
from market_data_ingestor.quality_compounder_market import (
    QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
)
from market_data_ingestor.quality_compounder_universe import (
    QualityCompounderIdentityEvidence,
    QualityCompounderSecurityIdentity,
    QualityCompounderUniverseComponent,
    persist_quality_compounder_universe,
)
from market_data_ingestor.sec_edgar import (
    SecFilingRecord,
    SecSourceDocument,
    parse_submissions_payload,
)
from market_data_ingestor.sec_filing_event_history import (
    SecFilingDiagnosticReport,
    forward_returns,
    load_recorded_submissions,
    quarter_end_cutoffs,
    read_recorded_closes,
    replay_sec_filing_rules,
)

_T1 = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
_T2 = datetime(2024, 6, 30, 23, 59, 59, tzinfo=UTC)
_RETRIEVED = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_NON_RELIANCE = SecFilingGateReason.NON_RELIANCE.value
_BANKRUPTCY = SecFilingGateReason.BANKRUPTCY_OR_RECEIVERSHIP.value
_LATE = SecFilingGateReason.LATE_PERIODIC_REPORT.value
_IMPAIRMENT = SecFilingGateReason.MATERIAL_IMPAIRMENT.value
_INVALID = SecFilingGateReason.EVIDENCE_INVALID.value
_UNAVAILABLE = SecFilingGateReason.COVERAGE_UNAVAILABLE.value


def _columns(rows: list[tuple[str, str, str, str, str]]) -> dict[str, list[str]]:
    """EDGAR column layout from (accession, filingDate, acceptanceDateTime, form, items)."""
    return {
        "accessionNumber": [row[0] for row in rows],
        "filingDate": [row[1] for row in rows],
        "acceptanceDateTime": [row[2] for row in rows],
        "form": [row[3] for row in rows],
        "items": [row[4] for row in rows],
    }


def _root(
    cik: str,
    rows: list[tuple[str, str, str, str, str]],
    files: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "cik": cik,
        "name": "EXAMPLE ISSUER INC",
        "sic": "3571",
        "filings": {"recent": _columns(rows), "files": files or []},
    }


def _records(cik: str, rows: list[tuple[str, str, str, str, str]]) -> tuple[SecFilingRecord, ...]:
    source = SecSourceDocument(
        endpoint=f"https://data.sec.gov/submissions/CIK{cik}.json",
        retrieved_at=_RETRIEVED,
        content_sha256="0" * 64,
    )
    return parse_submissions_payload(_root(cik, rows), cik=cik, source=source).filings


# CIK 1: 8-K item 4.02 in Q1 2024 -> non-reliance at T1 and T2 (365-day window).
_ISSUER_1 = [
    ("0000000001-24-000001", "2024-02-15", "2024-02-15T10:00:00.000Z", "8-K", "4.02,9.01"),
]
# CIK 2: NT 10-Q in May 2024 -> late periodic report at T2 only; 8-K/A 2.06 at T1.
_ISSUER_2 = [
    ("0000000002-24-000001", "2024-01-10", "2024-01-10T10:00:00.000Z", "8-K/A", "2.06"),
    ("0000000002-24-000002", "2024-05-15", "2024-05-15T10:00:00.000Z", "NT 10-Q", ""),
]
# CIK 3: 8-K with empty items inside the evidence scope -> evidence invalid.
_ISSUER_3 = [
    ("0000000003-24-000001", "2024-03-01", "2024-03-01T10:00:00.000Z", "8-K", ""),
]
# CIK 4: only routine filings, plus a 1.03 accepted after T2 (not yet available).
_ISSUER_4 = [
    ("0000000004-24-000001", "2024-02-01", "2024-02-01T10:00:00.000Z", "8-K", "2.02"),
    ("0000000004-24-000002", "2024-07-15", "2024-07-15T10:00:00.000Z", "8-K", "1.03"),
]
_FILINGS = {
    "0000000001": _records("0000000001", _ISSUER_1),
    "0000000002": _records("0000000002", _ISSUER_2),
    "0000000003": _records("0000000003", _ISSUER_3),
    "0000000004": _records("0000000004", _ISSUER_4),
}
# Instruments 10/11 are two share classes of CIK 1; 50 has no recorded filings.
_MEMBERS = {
    10: "0000000001",
    11: "0000000001",
    20: "0000000002",
    30: "0000000003",
    40: "0000000004",
    50: "0000000005",
}
_SECTORS = {10: "Tech", 11: "Tech", 20: "Tech", 30: "Energy", 40: "Energy", 50: "Energy"}


def _replay(
    *,
    returns_63: dict[tuple[datetime, int], float] | None = None,
    point_in_time_membership: bool = True,
) -> SecFilingDiagnosticReport:
    return replay_sec_filing_rules(
        filings_by_cik=_FILINGS,
        members_by_cutoff={_T1: _MEMBERS, _T2: _MEMBERS},
        sector_by_instrument=_SECTORS,
        returns_63=returns_63 or {},
        cutoffs=(_T1, _T2),
        point_in_time_membership=point_in_time_membership,
        return_basis="total_return_close",
    )


def test_removal_counts_per_reason_and_sector() -> None:
    report = _replay()

    first, second = report.quarters
    assert first.cutoff == _T1
    assert first.members == 6
    assert dict(first.removed_by_reason) == {_NON_RELIANCE: 2, _IMPAIRMENT: 1, _INVALID: 1}
    assert dict(first.removed_by_sector) == {"Tech": 3, "Energy": 1}
    assert first.invalid_items == 1
    assert second.cutoff == _T2
    # The 2.06 window (120 days) has closed and the NT notice is now available.
    assert dict(second.removed_by_reason) == {_NON_RELIANCE: 2, _LATE: 1, _INVALID: 1}
    assert dict(second.removed_by_sector) == {"Tech": 3, "Energy": 1}
    assert report.mean_removal_rate == pytest.approx(4 / 6)
    assert dict(report.per_rule_flags) == {
        _BANKRUPTCY: 0,
        _NON_RELIANCE: 4,
        _LATE: 1,
        _IMPAIRMENT: 1,
    }
    assert report.label == "acceptance-time diagnostic"


def test_acceptance_after_cutoff_is_not_available() -> None:
    report = _replay()

    assert report.per_rule_flags[_BANKRUPTCY] == 0
    for quarter in report.quarters:
        assert _BANKRUPTCY not in quarter.removed_by_reason


def test_report_never_sets_coverage_unavailable() -> None:
    report = _replay()

    for quarter in report.quarters:
        assert _UNAVAILABLE not in quarter.removed_by_reason
    assert _UNAVAILABLE not in report.per_rule_flags


def test_rules_below_thirty_flags_are_safety_rules() -> None:
    report = _replay()

    assert all(report.per_rule_safety_rule.values())
    assert set(report.per_rule_safety_rule) == set(report.per_rule_flags)


def test_survivorship_flag_follows_point_in_time_membership() -> None:
    assert _replay(point_in_time_membership=True).survivorship_biased is False
    assert _replay(point_in_time_membership=False).survivorship_biased is True


def test_excess_return_against_sector_median_and_left_tail() -> None:
    returns = {
        # T1 Tech: 10/11 non-reliance, 20 impairment; Energy: 30 invalid, 40/50 clean.
        (_T1, 10): -0.30,
        (_T1, 11): -0.10,
        (_T1, 20): 0.05,
        (_T1, 30): 0.00,
        (_T1, 40): 0.10,
        (_T1, 50): -0.25,
        # T2: only non-reliance names and one clean name have returns.
        (_T2, 10): 0.20,
        (_T2, 40): 0.02,
    }

    report = _replay(returns_63=returns)

    # T1 Tech median of (-0.30, -0.10, 0.05) is -0.10; T2 Tech median of (0.20,) is 0.20.
    # Non-reliance excess: (-0.20, 0.00, 0.00) -> mean -0.0666...
    assert report.per_rule_excess_return_63[_NON_RELIANCE] == pytest.approx(-0.2 / 3)
    assert report.per_rule_left_tail_share[_NON_RELIANCE] == pytest.approx(1 / 3)
    assert report.per_rule_excess_return_63[_IMPAIRMENT] == pytest.approx(0.15)
    assert report.per_rule_left_tail_share[_IMPAIRMENT] == 0.0
    assert report.per_rule_excess_return_63[_LATE] is None
    assert report.per_rule_left_tail_share[_LATE] is None
    # Unflagged: T1 40, 50 (Energy median of 0.00, 0.10, -0.25 is 0.00); T2 40 (median 0.02).
    assert report.unflagged_excess_return_63 == pytest.approx((0.10 - 0.25 + 0.0) / 3)
    assert report.unflagged_left_tail_share == pytest.approx(1 / 3)


def test_missing_returns_report_none() -> None:
    report = _replay()

    assert all(value is None for value in report.per_rule_excess_return_63.values())
    assert all(value is None for value in report.per_rule_left_tail_share.values())
    assert report.unflagged_excess_return_63 is None
    assert report.unflagged_left_tail_share is None


def test_replay_fails_closed_on_missing_inputs() -> None:
    with pytest.raises(ValueError, match="membership"):
        replay_sec_filing_rules(
            filings_by_cik=_FILINGS,
            members_by_cutoff={_T1: _MEMBERS},
            sector_by_instrument=_SECTORS,
            returns_63={},
            cutoffs=(_T1, _T2),
            point_in_time_membership=True,
            return_basis="total_return_close",
        )
    with pytest.raises(ValueError, match="sector"):
        replay_sec_filing_rules(
            filings_by_cik=_FILINGS,
            members_by_cutoff={_T1: _MEMBERS},
            sector_by_instrument={10: "Tech"},
            returns_63={},
            cutoffs=(_T1,),
            point_in_time_membership=True,
            return_basis="total_return_close",
        )
    with pytest.raises(ValueError, match="cutoff"):
        replay_sec_filing_rules(
            filings_by_cik=_FILINGS,
            members_by_cutoff={},
            sector_by_instrument=_SECTORS,
            returns_63={},
            cutoffs=(),
            point_in_time_membership=True,
            return_basis="total_return_close",
        )


def test_quarter_end_cutoffs_are_end_of_day_utc() -> None:
    cutoffs = quarter_end_cutoffs(start=date(2023, 11, 1), end=date(2024, 9, 30))

    assert cutoffs == tuple(
        datetime.combine(day, time.max, tzinfo=UTC)
        for day in (date(2023, 12, 31), date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30))
    )


def _write(directory: Path, name: str, payload: dict[str, Any]) -> None:
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_load_recorded_submissions_merges_declared_pages(tmp_path: Path) -> None:
    page = "CIK0000000002-submissions-001.json"
    _write(
        tmp_path,
        "CIK0000000002.json",
        _root("0000000002", _ISSUER_2[1:], files=[{"name": page, "filingTo": "2024-01-31"}]),
    )
    _write(tmp_path, page, _columns(_ISSUER_2[:1]))

    loaded = load_recorded_submissions(tmp_path)

    assert set(loaded) == {"0000000002"}
    assert [record.accession for record in loaded["0000000002"]] == [
        "0000000002-24-000001",
        "0000000002-24-000002",
    ]


def test_load_recorded_submissions_requires_every_declared_page(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "CIK0000000002.json",
        _root(
            "0000000002",
            _ISSUER_2,
            files=[{"name": "CIK0000000002-submissions-001.json", "filingTo": "2023-12-31"}],
        ),
    )

    with pytest.raises(FileNotFoundError, match="submissions-001"):
        load_recorded_submissions(tmp_path)


def test_main_rejects_missing_submissions_dir() -> None:
    with pytest.raises(SystemExit):
        history.main([])


def _evidence(endpoint: str, payload: object, *, minute: int = 0) -> EODHDJsonEvidence:
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return EODHDJsonEvidence(
        endpoint=endpoint,
        retrieved_at=datetime(2026, 6, 30, 21, minute, tzinfo=UTC),
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _seed_checkpoint(factory: sessionmaker[Session]) -> None:
    """Persist one prospective S&P 500 checkpoint on 2026-06-30 through the real writer."""

    with factory() as session, session.begin():
        session.add(
            User(user_id="owner-1", email="owner@example.com", base_ccy="EUR", status="active")
        )
        broker = Broker(code="ibkr", name="Interactive Brokers", capabilities={})
        calendar = MarketCalendar(
            code="XNYS",
            source_kind="exchange",
            provider="NYSE",
            source_reference="test official calendar",
        )
        session.add_all((broker, calendar))
        session.flush()
        session.add(
            Instrument(
                instr_id=1,
                asset_class="equity",
                canonical="AAA",
                exchange="NASDAQ",
                settlement_currency="USD",
                lot_size=Decimal("1"),
                is_tradable=True,
                market_session_policy="scheduled",
                market_calendar_id=int(calendar.calendar_id),
            )
        )
        session.flush()
        session.add(
            InstrumentBrokerSymbol(
                instr_id=1,
                broker_id=int(broker.broker_id),
                broker_symbol="AAA",
                broker_instrument_id="123456",
                broker_instrument_type="STK",
            )
        )
        session.flush()
        identity = QualityCompounderSecurityIdentity(
            symbol="AAA",
            security_id="figi:BBG000TEST01",
            issuer_id="cik:0000000001",
            name="Alpha Corporation",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            listing_date=date(2000, 1, 3),
            source=QualityCompounderIdentityEvidence(
                mapping=_evidence("/api/id-mapping?symbol=AAA.US", {"AAA": 1}, minute=1),
                general=_evidence("/api/v1.1/fundamentals/AAA.US", {"AAA": 2}, minute=2),
            ),
        )
        persist_quality_compounder_universe(
            session,
            components=(
                QualityCompounderUniverseComponent(
                    symbol="AAA",
                    name="Alpha Corporation",
                    exchange="NASDAQ",
                    sector="Technology",
                    industry="Software",
                    weight=Decimal("1"),
                ),
            ),
            identities={"AAA": identity},
            current_evidence=_evidence("/api/current", {"AAA": True}),
            historical_evidence=_evidence("/api/historical", {"AAA": True}, minute=3),
            ticker_history_evidence=_evidence("/api/ticker-history", {"AAA": True}, minute=4),
            decision_session=date(2026, 6, 30),
            decision_close=datetime(2026, 6, 30, 20, 0, tzinfo=UTC),
            cutoff=datetime(2026, 6, 30, 22, 0, tzinfo=UTC),
            entitlement_owner_user_id="owner-1",
        )


def test_main_prints_report_from_recorded_submissions_and_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    _seed_checkpoint(factory)
    monkeypatch.setattr(history, "_session_factory", lambda: factory)
    _write(tmp_path, "CIK0000000001.json", _root("0000000001", _ISSUER_1))

    exit_code = history.main(
        [
            "--submissions-dir",
            str(tmp_path),
            "--start",
            "2024-01-01",
            "--end",
            "2026-06-30",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["label"] == "acceptance-time diagnostic"
    # Only the 2026-06-30 cutoff has a recorded checkpoint; earlier ones use it as-is.
    assert report["survivorship_biased"] is True
    quarters = {item["cutoff"]: item for item in report["quarters"]}
    assert len(quarters) == 10
    assert quarters["2024-03-31T23:59:59.999999+00:00"]["removed_by_reason"] == {_NON_RELIANCE: 1}
    assert quarters["2024-03-31T23:59:59.999999+00:00"]["removed_by_sector"] == {"Technology": 1}
    assert quarters["2026-06-30T23:59:59.999999+00:00"]["removed_by_reason"] == {}
    assert report["per_rule_flags"][_NON_RELIANCE] == 4
    assert report["per_rule_excess_return_63"][_NON_RELIANCE] is None
    assert report["per_rule_safety_rule"][_NON_RELIANCE] is True
    assert report["return_basis"] == "total_return_close"


def test_main_reports_point_in_time_when_every_cutoff_has_a_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    _seed_checkpoint(factory)
    monkeypatch.setattr(history, "_session_factory", lambda: factory)

    exit_code = history.main(
        ["--submissions-dir", str(tmp_path), "--start", "2026-06-01", "--end", "2026-06-30"]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["survivorship_biased"] is False
    assert report["quarters"][0]["members"] == 1


_CUT = datetime(2024, 3, 31, 23, 59, 59, tzinfo=UTC)
_RUN_AT = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _series(count: int, *, start_close: float = 100.0) -> list[tuple[datetime, float]]:
    """``count`` daily closes, the first two at or before ``_CUT`` and the rest after it."""
    first = datetime(2024, 3, 30, 20, 0, tzinfo=UTC)
    return [(first + timedelta(days=index), start_close + index) for index in range(count)]


def test_forward_return_uses_last_close_before_and_63rd_session_after() -> None:
    series = _series(2 + 63)

    returns = forward_returns({7: series}, cutoffs=(_CUT,))

    # Base is the second close (101); target is the 63rd close after the cutoff (164).
    assert returns == {(_CUT, 7): pytest.approx(164.0 / 101.0 - 1.0)}


def test_forward_return_omits_insufficient_history() -> None:
    assert forward_returns({7: _series(2 + 62)}, cutoffs=(_CUT,)) == {}
    after_only = [(at, close) for at, close in _series(70) if at > _CUT]
    assert forward_returns({7: after_only}, cutoffs=(_CUT,)) == {}


def _price_submission(
    instrument_id: int,
    at: datetime,
    *,
    close: Decimal | None,
    revision: int = 1,
    supersedes: str | None = None,
    available_at: datetime | None = None,
) -> EquityObservationSubmission:
    values = (
        ()
        if close is None
        else (
            EquityObservationValueInput(
                field_name="total_return_close", value_type="decimal", value=close, unit="USD"
            ),
        )
    )
    identity = f"AAA.US:{at.date().isoformat()}:derived-market"
    digest = hashlib.sha256(f"{identity}:{close}:{revision}".encode()).hexdigest()
    return EquityObservationSubmission(
        provider="eodhd",
        product="derived-daily-market-with-corporate-actions",
        endpoint="/api/eod/AAA.US",
        dataset_version="prospective-derived-market-v1",
        tool_version="test",
        source_identity="eodhd:derived-market:AAA.US",
        source_revision=digest,
        retrieved_at=at,
        timestamp_semantics={"event_at": "official exchange regular-session close"},
        adjustment_policy=QUALITY_COMPOUNDER_MARKET_ADJUSTMENT_POLICY,
        entitlement_scope=EODHD_PAPER_ENTITLEMENT,
        entitlement_owner_user_id="owner-1",
        missing_data_policy="test",
        artifact_content_sha256=digest,
        instrument_id=instrument_id,
        observation_kind="price",
        source_record_identity=identity,
        event_at=at,
        available_at=available_at or at,
        disposition="observed",
        normalized_content_sha256=digest,
        values=values,
        revision=revision,
        supersedes_observation_id=supersedes,
    )


def test_recorded_closes_feed_the_forward_return() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    _seed_checkpoint(factory)
    series = _series(2 + 63)
    with factory() as session, session.begin():
        first = None
        for index, (at, close) in enumerate(series):
            observation = persist_equity_observation(
                session, _price_submission(1, at, close=Decimal(str(close)))
            )
            if index == 1:
                first = observation
        assert first is not None
        # A later revision of the base close supersedes the original.
        persist_equity_observation(
            session,
            _price_submission(
                1,
                series[1][0],
                close=Decimal("82"),
                revision=2,
                supersedes=str(first.observation_id),
            ),
        )

    with factory() as session:
        closes = read_recorded_closes(session, instrument_ids=(1,), run_at=_RUN_AT)

    assert len(closes[1]) == 65
    assert closes[1][1] == (series[1][0], 82.0)
    returns = forward_returns(closes, cutoffs=(_CUT,))
    assert returns == {(_CUT, 1): pytest.approx(164.0 / 82.0 - 1.0)}


def test_recorded_close_without_value_fails_closed() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    _seed_checkpoint(factory)
    with factory() as session, session.begin():
        persist_equity_observation(session, _price_submission(1, _CUT, close=None))

    with factory() as session, pytest.raises(ValueError, match="total_return_close"):
        read_recorded_closes(session, instrument_ids=(1,), run_at=_RUN_AT)


def test_recorded_close_available_after_run_time_is_ignored() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    _seed_checkpoint(factory)
    early, late = _series(2)
    with factory() as session, session.begin():
        persist_equity_observation(session, _price_submission(1, early[0], close=Decimal("100")))
        persist_equity_observation(
            session,
            _price_submission(
                1, late[0], close=Decimal("101"), available_at=_RUN_AT + timedelta(seconds=1)
            ),
        )

    with factory() as session:
        closes = read_recorded_closes(session, instrument_ids=(1,), run_at=_RUN_AT)
        with pytest.raises(ValueError, match="timezone-aware"):
            read_recorded_closes(session, instrument_ids=(1,), run_at=_RUN_AT.replace(tzinfo=None))

    assert closes == {1: [(early[0], 100.0)]}
