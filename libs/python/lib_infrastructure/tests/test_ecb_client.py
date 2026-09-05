from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from lib_infrastructure.market_data.ecb_client import (
    ECBReferenceRateClient,
    ECBReferenceRateError,
)

# Official ECB rates published for 2024-12-31:
# https://www.ecb.europa.eu/stats/exchange/eurofxref/shared/pdf/2024/12/20241231.pdf
_ECB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope
    xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
    xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <Cube>
    <Cube time="2024-12-31">
      <Cube currency="USD" rate="1.0389"/>
      <Cube currency="INR" rate="88.9335"/>
      <Cube currency="GBP" rate="0.82918"/>
    </Cube>
  </Cube>
</gesmes:Envelope>
"""


def _client(payload: bytes = _ECB_XML) -> ECBReferenceRateClient:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == ECBReferenceRateClient.FEED_PATH
        return httpx.Response(200, content=payload)

    return ECBReferenceRateClient(
        client=httpx.Client(
            transport=httpx.MockTransport(_handler),
            base_url=ECBReferenceRateClient.BASE_URL,
        )
    )


def test_fetches_requested_official_rates_with_conservative_publication_time() -> None:
    client = _client()

    rates = client.fetch_rates(quote_currencies={"USD", "INR"})

    assert [(rate.quote_currency, rate.rate) for rate in rates] == [
        ("USD", Decimal("1.0389")),
        ("INR", Decimal("88.9335")),
    ]
    # 16:00 Europe/Berlin is 15:00 UTC in December.  Same-day historical
    # fills before this timestamp therefore use the prior business-day rate.
    assert {rate.observed_at for rate in rates} == {datetime(2024, 12, 31, 15, 0, tzinfo=UTC)}
    assert {rate.source for rate in rates} == {"ecb_reference"}


def test_rejects_malformed_or_incomplete_responses() -> None:
    with pytest.raises(ECBReferenceRateError, match="valid XML"):
        _client(b"<not-xml").fetch_rates(quote_currencies={"USD"})

    with pytest.raises(ECBReferenceRateError, match="omitted requested currencies: JPY"):
        _client().fetch_rates(quote_currencies={"INR", "JPY"})


@pytest.mark.parametrize("currency", ["", "EU-R", "TOO_LONG_CODE"])
def test_rejects_invalid_currency_codes(currency: str) -> None:
    with pytest.raises(ValueError, match="Invalid ECB quote currency"):
        _client().fetch_rates(quote_currencies={currency})
