"""ECB euro reference-rate client.

The ECB publishes business-day reference rates as quote-currency units per
euro.  This adapter parses the official 90-day XML feed and preserves the
publication date as a conservative 16:00 Europe/Berlin point-in-time so a
historical replay cannot use a same-day rate before it was available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

_ECB_TIMEZONE = ZoneInfo("Europe/Berlin")
_REFERENCE_PUBLICATION_TIME = time(hour=16)
_MAX_CURRENCY_LENGTH = 10


class ECBReferenceRateError(RuntimeError):
    """The ECB reference-rate response was unavailable or malformed."""


@dataclass(frozen=True)
class ECBReferenceRate:
    """One official EUR-based reference-rate observation."""

    quote_currency: str
    rate: Decimal
    observed_at: datetime
    source: str = "ecb_reference"


class ECBReferenceRateClient:
    """Fetch and parse the ECB's official 90-day euro reference-rate feed."""

    BASE_URL = "https://www.ecb.europa.eu"
    FEED_PATH = "/stats/eurofxref/eurofxref-hist-90d.xml"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(base_url=self.BASE_URL, timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def fetch_rates(self, *, quote_currencies: set[str]) -> list[ECBReferenceRate]:
        """Return requested EUR quote rates from the rolling official feed."""
        requested = {_normalize_currency(value) for value in quote_currencies}
        requested.discard("EUR")
        if not requested:
            return []

        response = self._client.get(
            self.FEED_PATH,
            headers={
                "Accept": "application/xml, text/xml",
                "Cache-Control": "no-cache",
            },
        )
        response.raise_for_status()
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            msg = "ECB reference-rate response was not valid XML"
            raise ECBReferenceRateError(msg) from exc

        observations: list[ECBReferenceRate] = []
        observed_currencies: set[str] = set()
        for day_cube in root.iter():
            raw_date = day_cube.attrib.get("time")
            if raw_date is None:
                continue
            try:
                publication_date = date.fromisoformat(raw_date)
            except ValueError as exc:
                msg = f"ECB reference-rate response contained an invalid date: {raw_date!r}"
                raise ECBReferenceRateError(msg) from exc
            observed_at = datetime.combine(
                publication_date,
                _REFERENCE_PUBLICATION_TIME,
                tzinfo=_ECB_TIMEZONE,
            ).astimezone(UTC)
            for rate_cube in day_cube:
                raw_currency = rate_cube.attrib.get("currency")
                if raw_currency is None:
                    continue
                try:
                    currency = _normalize_currency(raw_currency)
                except ValueError:
                    continue
                if currency not in requested:
                    continue
                try:
                    rate = Decimal(str(rate_cube.attrib["rate"]))
                except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
                    msg = (
                        "ECB reference-rate response contained an invalid "
                        f"{currency} rate for {raw_date}"
                    )
                    raise ECBReferenceRateError(msg) from exc
                if not rate.is_finite() or rate <= 0:
                    msg = (
                        "ECB reference-rate response contained a non-positive "
                        f"{currency} rate for {raw_date}"
                    )
                    raise ECBReferenceRateError(msg)
                observations.append(
                    ECBReferenceRate(
                        quote_currency=currency,
                        rate=rate,
                        observed_at=observed_at,
                    )
                )
                observed_currencies.add(currency)

        missing = requested - observed_currencies
        if missing:
            msg = "ECB reference-rate feed omitted requested currencies: " + ", ".join(
                sorted(missing)
            )
            raise ECBReferenceRateError(msg)
        observations.sort(key=lambda observation: observation.observed_at)
        return observations


def _normalize_currency(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if not normalized or len(normalized) > _MAX_CURRENCY_LENGTH or not normalized.isalpha():
        msg = f"Invalid ECB quote currency: {value!r}"
        raise ValueError(msg)
    return normalized


__all__ = [
    "ECBReferenceRate",
    "ECBReferenceRateClient",
    "ECBReferenceRateError",
]
