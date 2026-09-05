from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from execution_engine._execute import _resolve_order_currency_context
from execution_engine.metrics.fx_rates import (
    CachedFXRateProvider,
    FXConverter,
    FXRateUnavailableError,
    ObservedFXRate,
    SqlFXRateProvider,
)
from lib_application.db.models import Base, Instrument, InstrumentPrice
from lib_strategy.signals.signal import Signal, SignalAction

# ECB euro reference rates published for 2024-12-31:
# https://www.ecb.europa.eu/stats/exchange/eurofxref/shared/pdf/2024/12/20241231.pdf
OBSERVED_AT = datetime(2024, 12, 31, 14, 10, tzinfo=UTC)
USDC_EUR_CANDLE_START = datetime(2024, 12, 31, 0, 0, tzinfo=UTC)
USDC_EUR_AVAILABLE_AT = USDC_EUR_CANDLE_START + timedelta(hours=1)
EUR_USD = Decimal("1.0389")
EUR_INR = Decimal("88.9335")
USDC_EUR = Decimal("0.9661")
BTC_USD = Decimal("93429.20")
ETH_USD = Decimal("3342.18")
USD_INR = (EUR_INR / EUR_USD).quantize(Decimal("0.00000001"))


def _session_local():  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_local = sessionmaker(engine, expire_on_commit=False)
    with session_local() as session:
        eur_usd = Instrument(
            instr_id=1,
            asset_class="fx",
            canonical="EUR/USD",
            settlement_currency="USD",
        )
        usd_inr = Instrument(
            instr_id=2,
            asset_class="fx",
            canonical="USD/INR",
            settlement_currency="INR",
        )
        usdc_eur = Instrument(
            instr_id=3,
            asset_class="fx",
            canonical="USDC/EUR",
            settlement_currency="EUR",
        )
        btc_usd = Instrument(
            instr_id=4,
            asset_class="crypto",
            canonical="BTC-USD",
            settlement_currency="USD",
        )
        eth_usd = Instrument(
            instr_id=5,
            asset_class="crypto",
            canonical="ETH/USD",
            settlement_currency="USD",
        )
        btc_eur = Instrument(
            instr_id=6,
            asset_class="crypto",
            canonical="BTC/EUR",
            settlement_currency="EUR",
        )
        session.add_all([eur_usd, usd_inr, usdc_eur, btc_usd, eth_usd, btc_eur])
        session.flush()
        session.add_all(
            [
                InstrumentPrice(
                    instr_id=eur_usd.instr_id,
                    ts=OBSERVED_AT.replace(tzinfo=None),
                    timeframe="1d",
                    close=EUR_USD,
                    source="ecb_eurofxref_2024_12_31",
                ),
                InstrumentPrice(
                    instr_id=usd_inr.instr_id,
                    ts=OBSERVED_AT.replace(tzinfo=None),
                    timeframe="1d",
                    close=USD_INR,
                    source="ecb_cross_eur_2024_12_31",
                ),
                InstrumentPrice(
                    instr_id=usdc_eur.instr_id,
                    ts=USDC_EUR_CANDLE_START.replace(tzinfo=None),
                    timeframe="1h",
                    close=USDC_EUR,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    instr_id=btc_usd.instr_id,
                    ts=OBSERVED_AT.replace(tzinfo=None),
                    timeframe="1m",
                    close=BTC_USD,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    instr_id=eth_usd.instr_id,
                    ts=OBSERVED_AT.replace(tzinfo=None),
                    timeframe="1m",
                    close=ETH_USD,
                    source="coinbase_live",
                ),
                InstrumentPrice(
                    instr_id=btc_eur.instr_id,
                    ts=OBSERVED_AT.replace(tzinfo=None),
                    timeframe="1m",
                    close=BTC_USD / EUR_USD,
                    source="coinbase_live",
                ),
            ]
        )
        session.commit()
    return session_local


def test_sql_provider_uses_observed_direct_and_inverse_rates() -> None:
    provider = SqlFXRateProvider(_session_local(), max_age=timedelta(days=1))

    eur_usd = provider.get_rate(
        base_currency="EUR",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    usd_eur = provider.get_rate(
        base_currency="USD",
        quote_currency="EUR",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    usd_inr = provider.get_rate(
        base_currency="USD",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )

    assert eur_usd is not None
    assert eur_usd.rate == EUR_USD
    assert eur_usd.source == "ecb_eurofxref_2024_12_31"
    assert usd_eur is not None
    assert usd_eur.rate == Decimal("1") / EUR_USD
    assert usd_inr is not None
    assert usd_inr.rate == USD_INR


def test_sql_provider_triangulates_real_source_legs_through_eur() -> None:
    provider = SqlFXRateProvider(_session_local(), max_age=timedelta(days=1))

    usdc_usd = provider.get_rate(
        base_currency="USDC",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )

    assert usdc_usd is not None
    assert usdc_usd.rate == USDC_EUR * EUR_USD
    assert usdc_usd.observed_at == USDC_EUR_AVAILABLE_AT
    assert usdc_usd.source == ("cross:EUR:coinbase_live|ecb_eurofxref_2024_12_31")


def test_sql_provider_uses_exact_crypto_pairs_for_deribit_equity() -> None:
    provider = SqlFXRateProvider(_session_local(), max_age=timedelta(days=1))

    btc_usd = provider.get_rate(
        base_currency="BTC",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    eth_usd = provider.get_rate(
        base_currency="ETH",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    usd_btc = provider.get_rate(
        base_currency="USD",
        quote_currency="BTC",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )

    assert btc_usd is not None
    assert btc_usd.rate == BTC_USD
    assert btc_usd.source == "coinbase_live"
    assert eth_usd is not None
    assert eth_usd.rate == ETH_USD
    assert usd_btc is not None
    assert usd_btc.rate == Decimal("1") / BTC_USD


def test_sql_provider_never_uses_a_coinbase_close_before_candle_end() -> None:
    provider = SqlFXRateProvider(_session_local(), max_age=timedelta(days=1))

    before_close = provider.get_rate(
        base_currency="BTC",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(seconds=30),
    )
    at_close = provider.get_rate(
        base_currency="BTC",
        quote_currency="USD",
        as_of=OBSERVED_AT + timedelta(minutes=1),
    )

    assert before_close is None
    assert at_close is not None
    assert at_close.rate == BTC_USD
    assert at_close.observed_at == OBSERVED_AT + timedelta(minutes=1)


def test_sql_provider_does_not_cross_crypto_rates() -> None:
    provider = SqlFXRateProvider(_session_local(), max_age=timedelta(days=1))

    btc_inr = provider.get_rate(
        base_currency="BTC",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )

    assert btc_inr is None


def test_converter_fails_closed_without_observed_mixed_currency_rate() -> None:
    class MissingProvider:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_rate(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(kwargs)

    provider = MissingProvider()
    converter = FXConverter(provider)

    assert converter.convert(
        Decimal("10"),
        from_currency="EUR",
        to_currency="EUR",
        as_of=OBSERVED_AT,
    ) == Decimal("10")
    with pytest.raises(FXRateUnavailableError, match="No observed FX rate"):
        converter.convert(
            Decimal("10"),
            from_currency="USD",
            to_currency="USDC",
            as_of=OBSERVED_AT,
        )
    assert provider.calls == [
        {
            "base_currency": "USD",
            "quote_currency": "USDC",
            "as_of": OBSERVED_AT,
        }
    ]


def test_execution_currency_context_uses_signal_time_and_never_assumes_usd_usdc_parity() -> None:
    signal_time = datetime(2026, 7, 15, 12, tzinfo=UTC)

    class Provider:
        def __init__(self, quote: ObservedFXRate | None) -> None:
            self.quote = quote
            self.calls: list[dict[str, object]] = []

        def get_rate(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append(kwargs)
            return self.quote

    signal = Signal(
        strategy_id="test_strategy_alpha_v1",
        strategy_type="indicator",
        symbol="BTC-USDC",
        action=SignalAction.LONG,
        confidence=0.9,
        timestamp=signal_time,
    )
    resolved = SimpleNamespace(
        account_currency="EUR",
        settlement_currency="USDC",
        signal=signal,
    )
    observed = Provider(
        ObservedFXRate(
            base_currency="EUR",
            quote_currency="USDC",
            rate=Decimal("1.2"),
            observed_at=signal_time - timedelta(minutes=1),
            source="coinbase_live",
        )
    )
    context = _resolve_order_currency_context(
        cast("Any", observed),
        cast("Any", resolved),
    )

    assert context is not None
    assert context.account_to_settlement_rate == Decimal("1.2")
    assert context.requested_at == signal_time
    assert observed.calls == [
        {
            "base_currency": "EUR",
            "quote_currency": "USDC",
            "as_of": signal_time,
        }
    ]

    missing = Provider(None)
    resolved.account_currency = "USD"
    with pytest.raises(FXRateUnavailableError, match="No observed FX rate for USD/USDC"):
        _resolve_order_currency_context(
            cast("Any", missing),
            cast("Any", resolved),
        )


def test_cache_reuses_only_fresh_observed_rate() -> None:
    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_rate(self, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            return ObservedFXRate(
                base_currency="USD",
                quote_currency="INR",
                rate=USD_INR,
                observed_at=OBSERVED_AT,
                source="ecb_cross_eur_2024_12_31",
            )

    upstream = CountingProvider()
    cached = CachedFXRateProvider(
        upstream,
        cache_ttl=timedelta(minutes=5),
        max_observation_age=timedelta(days=1),
    )

    first = cached.get_rate(
        base_currency="USD",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    second = cached.get_rate(
        base_currency="USD",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(hours=1, minutes=2),
    )

    assert first is not None
    assert second is first
    assert upstream.calls == 1


def test_cache_does_not_reuse_a_rate_across_historical_time_buckets() -> None:
    class PointInTimeProvider:
        def __init__(self) -> None:
            self.calls = 0

        def get_rate(self, **kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            as_of = kwargs["as_of"]
            return ObservedFXRate(
                base_currency="USD",
                quote_currency="INR",
                rate=USD_INR,
                observed_at=as_of,
                source="ecb_reference",
            )

    upstream = PointInTimeProvider()
    cached = CachedFXRateProvider(
        upstream,
        cache_ttl=timedelta(minutes=5),
        max_observation_age=timedelta(days=1),
    )

    cached.get_rate(
        base_currency="USD",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(minutes=1),
    )
    cached.get_rate(
        base_currency="USD",
        quote_currency="INR",
        as_of=OBSERVED_AT + timedelta(days=1, minutes=1),
    )

    assert upstream.calls == 2
