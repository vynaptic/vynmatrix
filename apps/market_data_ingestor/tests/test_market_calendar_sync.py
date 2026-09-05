"""Supervised market-calendar writer tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lib_application.db.models import Base, Broker, Instrument, InstrumentBrokerSymbol
from lib_infrastructure.market_data.market_calendars import (
    OfficialMarketCalendar,
    OfficialSessionWindow,
)
from market_data_ingestor.market_calendar_sync import (
    BackendMarketCalendarClient,
    MarketCalendarSyncWorker,
    build_official_market_calendar_provider,
)

_OBSERVED_AT = datetime(2026, 7, 24, 12, tzinfo=UTC)


class _Provider:
    provider = "ibkr"

    def __init__(self, *, fail_product_id: str | None = None) -> None:
        self.calls: list[str] = []
        self.closed = False
        self._fail_product_id = fail_product_id

    def fetch(
        self,
        *,
        product_id: str,
        broker_instrument_type: str | None,
    ) -> OfficialMarketCalendar:
        del broker_instrument_type
        self.calls.append(product_id)
        if product_id == self._fail_product_id:
            msg = "provider contract failure"
            raise ValueError(msg)
        return OfficialMarketCalendar(
            source_kind="broker",
            provider="ibkr",
            source_reference="https://api.ibkr.com/v1/api/contract/trading-schedule",
            observed_at=_OBSERVED_AT,
            coverage_start=_OBSERVED_AT,
            coverage_end=_OBSERVED_AT + timedelta(days=1),
            sessions=(
                OfficialSessionWindow(
                    opens_at=_OBSERVED_AT,
                    closes_at=_OBSERVED_AT + timedelta(hours=6),
                ),
            ),
        )

    def close(self) -> None:
        self.closed = True


class _APIClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def replace(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def session_factory() -> Any:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        broker = Broker(code="ibkr", name="Interactive Brokers")
        session.add(broker)
        session.flush()
        for instrument_id, canonical, conid in (
            (100, "AAPL", "265598"),
            (101, "MSFT", "272093"),
        ):
            session.add(
                Instrument(
                    instr_id=instrument_id,
                    asset_class="equity",
                    canonical=canonical,
                    exchange="XNAS",
                    settlement_currency="USD",
                    is_tradable=True,
                )
            )
            session.add(
                InstrumentBrokerSymbol(
                    instr_id=instrument_id,
                    broker_id=int(broker.broker_id),
                    broker_symbol=canonical,
                    broker_instrument_id=conid,
                )
            )
        session.commit()
    return factory


def test_worker_loads_exact_venue_identity_from_catalogue_and_publishes_ids(
    session_factory: Any,
) -> None:
    provider = _Provider()
    api_client = _APIClient()
    worker = MarketCalendarSyncWorker(
        session_factory=session_factory,
        provider=provider,
        symbols=["AAPL"],
        poll_interval_sec=300,
        api_client=api_client,  # type: ignore[arg-type]
    )

    summary = worker.run_once()

    assert provider.calls == ["265598"]
    assert summary.target_count == 1
    assert api_client.calls[0]["code"] == "IBKR:I100"
    assert api_client.calls[0]["instrument_ids"] == (100,)
    assert worker.is_fresh()
    worker.stop()
    assert provider.closed
    assert api_client.closed


def test_worker_fetches_every_target_before_publishing_any_calendar(
    session_factory: Any,
) -> None:
    provider = _Provider(fail_product_id="272093")
    api_client = _APIClient()
    worker = MarketCalendarSyncWorker(
        session_factory=session_factory,
        provider=provider,
        symbols=["AAPL", "MSFT"],
        poll_interval_sec=300,
        api_client=api_client,  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="provider contract failure"):
        worker.run_once()

    assert provider.calls == ["265598", "272093"]
    assert api_client.calls == []
    assert not worker.is_fresh()
    worker.stop()


def test_control_plane_client_requires_admin_auth() -> None:
    with pytest.raises(ValueError, match="MARKET_CALENDAR_ADMIN_API_KEY"):
        BackendMarketCalendarClient(
            base_url="http://backend:8081",
            admin_api_key="",
        )


def test_control_plane_client_publishes_exact_normalized_batch() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/market-calendars/IBKR:I100"
        assert request.headers["x-admin-key"] == "admin-key"
        payload = json.loads(request.content)
        assert payload["provider"] == "ibkr"
        assert payload["instrument_ids"] == [100]
        assert payload["sessions"] == [
            {
                "opens_at": "2026-07-24T12:00:00+00:00",
                "closes_at": "2026-07-24T18:00:00+00:00",
            }
        ]
        return httpx.Response(200, json={"calendar_id": 1})

    http_client = httpx.Client(
        base_url="http://backend:8081",
        transport=httpx.MockTransport(handler),
    )
    client = BackendMarketCalendarClient(
        base_url="http://backend:8081",
        admin_api_key="admin-key",
        client=http_client,
    )

    client.replace(
        code="IBKR:I100",
        observation=_Provider().fetch(
            product_id="265598",
            broker_instrument_type=None,
        ),
        instrument_ids=[100],
    )
    client.close()


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("ibkr", "IBKR_GATEWAY_URL"),
        ("saxo_live", "access_token"),
        ("nse", "market name"),
    ],
)
def test_provider_configuration_fails_closed(
    provider: str,
    expected: str,
) -> None:
    with pytest.raises(ValueError, match=expected):
        build_official_market_calendar_provider(provider=provider)
