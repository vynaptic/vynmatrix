"""Tests for isolated EODHD quality-compounder acquisition."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from lib_infrastructure.market_data.eodhd_client import EODHDJsonEvidence
from market_data_ingestor.quality_compounder_eodhd import (
    AcquiredQualityCompounderMembership,
    QualityCompounderEODHDAcquisitionError,
    acquire_quality_compounder_benchmark_identity,
    acquire_quality_compounder_identities,
    acquire_quality_compounder_membership,
)
from market_data_ingestor.quality_compounder_universe import (
    QualityCompounderUniverseComponent,
)

_CLOSE = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
_RETRIEVED = datetime(2026, 6, 30, 21, 0, tzinfo=UTC)
_DEADLINE = datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


def _evidence(
    endpoint: str, payload: object, *, retrieved_at: datetime = _RETRIEVED
) -> EODHDJsonEvidence:
    content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return EODHDJsonEvidence(
        endpoint=endpoint,
        retrieved_at=retrieved_at,
        payload=payload,
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _membership_payloads() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    current: dict[str, object] = {}
    historical: dict[str, object] = {}
    ticker: dict[str, object] = {}
    for index in range(500):
        symbol = f"S{index:03d}"
        current[str(index)] = {
            "Code": symbol,
            "Name": f"Issuer {index}",
            "Exchange": "NASDAQ",
            "Sector": "Technology",
            "Industry": "Software",
            "Weight": "0.002",
        }
        historical[str(index)] = {
            "Code": symbol,
            "Name": f"Issuer {index}",
            "Exchange": "NASDAQ",
            "Date": "2026-06-30",
        }
        ticker[str(index)] = {
            "Code": symbol,
            "Name": f"Issuer {index}",
            "IsActiveNow": 1,
            "IsDelisted": 0,
        }
    return current, {"HistoricalComponents": {"2026-06-30": historical}}, ticker


class _Client:
    def __init__(self, *, retrieved_at: datetime = _RETRIEVED) -> None:
        self.retrieved_at = retrieved_at
        self.identity_symbols: list[str] = []
        self.membership_range: tuple[date, date] | None = None

    def fetch_current_index_components_evidence(self, *, index_symbol: str) -> EODHDJsonEvidence:
        current, _historical, _ticker = _membership_payloads()
        return _evidence("/api/current", current, retrieved_at=self.retrieved_at)

    def fetch_index_membership_history_evidence(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]:
        self.membership_range = (start, end)
        _current, historical, ticker = _membership_payloads()
        return (
            _evidence("/api/ticker-history", ticker, retrieved_at=self.retrieved_at),
            _evidence("/api/historical", historical, retrieved_at=self.retrieved_at),
        )

    def fetch_id_mapping_evidence(self, *, provider_symbol: str) -> EODHDJsonEvidence:
        self.identity_symbols.append(provider_symbol)
        return _evidence(
            f"/api/id-mapping/{provider_symbol}",
            {
                "meta": {"total": 1},
                "links": {"next": None},
                "data": [
                    {
                        "symbol": f"{provider_symbol}.US",
                        "figi": "BBG000TEST01",
                        "isin": "US0000000001",
                        "cusip": "000000001",
                        "cik": "1234",
                    }
                ],
            },
            retrieved_at=self.retrieved_at,
        )

    def fetch_security_general_evidence(self, *, provider_symbol: str) -> EODHDJsonEvidence:
        benchmark = provider_symbol == "SPY"
        return _evidence(
            f"/api/general/{provider_symbol}",
            {
                "Code": provider_symbol,
                "Name": "SPDR S&P 500 ETF Trust" if benchmark else "Alpha Corporation",
                "Type": "ETF" if benchmark else "Common Stock",
                "Exchange": "NYSE" if benchmark else "NASDAQ",
                "CurrencyCode": "USD",
                "CountryISO": "US",
                "CountryName": "United States",
                "IsDelisted": False,
                "CIK": "0000001234",
                "ISIN": "US0000000001",
                "IPODate": "2000-01-03",
            },
            retrieved_at=self.retrieved_at,
        )


def test_membership_acquisition_reconciles_all_three_views() -> None:
    client = _Client()
    acquired = acquire_quality_compounder_membership(
        client=client,
        decision_session=date(2026, 6, 30),
        decision_close=_CLOSE,
        complete_before=_DEADLINE,
    )

    assert len(acquired.components) == 500
    assert acquired.components[0].symbol == "S000"
    assert client.membership_range == (date(2024, 6, 28), date(2026, 6, 30))


def test_identity_acquisition_exactly_covers_membership() -> None:
    component = QualityCompounderUniverseComponent(
        symbol="AAA",
        name="Alpha Corporation",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        weight=Decimal("1"),
    )
    placeholder = _evidence("/api/placeholder", {})
    membership = AcquiredQualityCompounderMembership(
        decision_session=date(2026, 6, 30),
        decision_close=_CLOSE,
        current_evidence=placeholder,
        historical_evidence=placeholder,
        ticker_history_evidence=placeholder,
        components=(component,),
    )
    client = _Client()

    acquired = acquire_quality_compounder_identities(
        client=client,
        membership=membership,
        complete_before=_DEADLINE,
    )

    assert tuple(acquired.identities) == ("AAA",)
    assert acquired.identities["AAA"].issuer_id == "cik:0000001234"
    assert client.identity_symbols == ["AAA"]


def test_benchmark_acquisition_resolves_exact_spy_etf() -> None:
    benchmark = acquire_quality_compounder_benchmark_identity(
        client=_Client(),
        decision_close=_CLOSE,
        complete_before=_DEADLINE,
    )

    assert benchmark.symbol == "SPY"
    assert benchmark.exchange == "NYSE"


def test_acquisition_fails_when_source_completes_at_next_open() -> None:
    with pytest.raises(QualityCompounderEODHDAcquisitionError, match="quarter-end window"):
        acquire_quality_compounder_membership(
            client=_Client(retrieved_at=_DEADLINE),
            decision_session=date(2026, 6, 30),
            decision_close=_CLOSE,
            complete_before=_DEADLINE,
        )
