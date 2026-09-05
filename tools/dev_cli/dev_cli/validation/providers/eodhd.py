"""Strict EODHD adapter for immutable historical validation snapshots.

The shared infrastructure client retains its legacy skip-malformed-row default
for existing consumers. This validation-only boundary forces fail-loud daily
rows while reusing the same transport, parsing, retry, and corporate-action
implementation. The fresh strategy runtime neither imports nor selects EODHD.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Final

import httpx

from lib_infrastructure.market_data.eodhd_client import (
    DividendRecord,
    EODHDJsonEvidence,
    EODHDMalformedRowPolicy,
    EODHDMarketDataError,
    SplitRecord,
)
from lib_infrastructure.market_data.eodhd_client import (
    EODHDClient as SharedEODHDClient,
)

__all__ = [
    "DividendRecord",
    "EODHDClient",
    "EODHDJsonEvidence",
    "EODHDMalformedRowPolicy",
    "EODHDMarketDataError",
    "SplitRecord",
]

_STRICT_POLICY: Final = EODHDMalformedRowPolicy.RAISE


class EODHDClient(SharedEODHDClient):
    """Historical-validation client that rejects every unusable daily row."""

    def __init__(
        self,
        api_token: str,
        *,
        base_url: str = SharedEODHDClient.BASE_URL,
        exchange_mic: str = "XNYS",
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            api_token,
            base_url=base_url,
            exchange_mic=exchange_mic,
            client=client,
            malformed_row_policy=_STRICT_POLICY,
            clock=clock,
        )

    def fetch_index_membership_history(
        self,
        *,
        index_symbol: str,
        start: date,
        end: date,
    ) -> tuple[EODHDJsonEvidence, EODHDJsonEvidence]:
        """Fetch interval cross-checks and full point-in-time index snapshots."""

        return self.fetch_index_membership_history_evidence(
            index_symbol=index_symbol,
            start=start,
            end=end,
        )

    def fetch_us_symbol_directory(self, *, delisted: bool) -> EODHDJsonEvidence:
        """Fetch the active or inactive US common-stock identity directory."""

        params = {"type": "common_stock"}
        if delisted:
            params["delisted"] = "1"
        query = "type=common_stock&delisted=1" if delisted else "type=common_stock"
        endpoint = f"/api/exchange-symbol-list/US?{query}&fmt=json"
        payload, content = self._fetch_json(
            path="/api/exchange-symbol-list/US",
            label=("US delisted symbol directory" if delisted else "US active symbol directory"),
            params=params,
        )
        return self._json_evidence(endpoint, payload, content)

    def fetch_us_symbol_change_history(
        self,
        *,
        start: date,
        end: date,
    ) -> EODHDJsonEvidence:
        """Fetch dated US ticker changes used only for provider-history continuity."""

        if end < start:
            message = "EODHD symbol-change end cannot precede start"
            raise ValueError(message)
        endpoint = (
            "/api/symbol-change-history"
            f"?from={start.isoformat()}&to={end.isoformat()}&ex=US&fmt=json"
        )
        payload, content = self._fetch_json(
            path="/api/symbol-change-history",
            label="US symbol-change history",
            params={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "ex": "US",
            },
        )
        return self._json_evidence(endpoint, payload, content)

    def fetch_id_mapping(self, *, provider_symbol: str) -> EODHDJsonEvidence:
        """Fetch exact permanent identifiers for one EODHD US symbol."""

        return self.fetch_id_mapping_evidence(provider_symbol=provider_symbol)

    def fetch_security_fundamentals_general(
        self,
        *,
        provider_symbol: str,
    ) -> EODHDJsonEvidence:
        """Fetch the unmodified Fundamentals ``General`` block for one US security.

        Listing-date and identity interpretation belongs to the membership
        validator. This acquisition boundary retains the exact response bytes,
        decoded JSON value, retrieval timestamp, and credential-free endpoint
        even when the provider returns an empty object or JSON ``null``.
        """

        return self.fetch_security_general_evidence(provider_symbol=provider_symbol)
