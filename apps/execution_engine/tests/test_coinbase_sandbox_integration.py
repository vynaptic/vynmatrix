"""
Coinbase sandbox integration smoke tests.

These tests validate that the CoinbaseAdapter can authenticate and perform
basic operations against the Coinbase Advanced Trade sandbox environment.

Pull-request and local runs skip when sandbox credentials are not present.
Release-tag builds set ``COINBASE_SANDBOX_REQUIRED=true`` so an absent or
unusable credential fails the release gate instead of producing a false pass.
The suite must never run against production credentials.
"""

import asyncio
import os
from collections.abc import Awaitable
from typing import Any, TypeVar

import pytest

from lib_infrastructure.brokers.adapters.coinbase import CoinbaseAdapter

pytestmark = pytest.mark.integration

T = TypeVar("T")

# A single persistent event loop shared by every _run() call in this module.
# The adapter and BrokerBridge fixtures are module-scoped and open an HTTP
# session bound to the loop that connected them; per-call asyncio.run() would
# close that loop after the first test, so every later authenticated call would
# raise "Event loop is closed". One shared loop keeps the session alive across
# the whole suite.
_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro: Awaitable[T]) -> T:
    """Run the adapter coroutine on the module's shared event loop."""
    global _LOOP  # noqa: PLW0603 - module-singleton loop for the integration suite
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module", autouse=True)
def _close_shared_loop():
    """Close the shared loop after the module's tests finish."""
    yield
    global _LOOP  # noqa: PLW0603 - module-singleton loop for the integration suite
    if _LOOP is not None and not _LOOP.is_closed():
        _LOOP.close()
    _LOOP = None


def _get_sandbox_credentials() -> tuple[str, str]:
    """Read sandbox credentials, failing closed for release certification."""
    api_key = os.environ.get("COINBASE_SANDBOX_API_KEY")
    api_secret = os.environ.get("COINBASE_SANDBOX_API_SECRET")

    if not api_key or not api_secret:
        if os.environ.get("COINBASE_SANDBOX_REQUIRED", "").strip().lower() == "true":
            pytest.fail("Coinbase sandbox credentials are required for a release build")
        pytest.skip("Coinbase sandbox credentials not configured")

    return api_key, api_secret


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sandbox_adapter():
    """Return an authenticated CoinbaseAdapter pointed at the sandbox environment."""
    api_key, api_secret = _get_sandbox_credentials()
    adapter = CoinbaseAdapter(environment="paper")
    adapter.configure_settlement_currency("USD")
    if not _run(adapter.connect(api_key=api_key, api_secret=api_secret)):
        if os.environ.get("COINBASE_SANDBOX_REQUIRED", "").strip().lower() == "true":
            pytest.fail("Coinbase sandbox adapter could not authenticate")
        pytest.skip("Coinbase sandbox adapter could not connect")
    yield adapter
    _run(adapter.disconnect())


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


class TestCoinbaseSandboxAuthentication:
    """Verify that the adapter can authenticate against the sandbox."""

    def test_adapter_initializes(self, sandbox_adapter):
        """Adapter should be created without raising."""
        assert sandbox_adapter is not None

    def test_adapter_is_sandbox(self, sandbox_adapter):
        """Adapter should expose a flag or property indicating sandbox mode."""
        assert sandbox_adapter.environment == "paper"

    def test_authentication_succeeds(self, sandbox_adapter):
        """Calling an authenticated endpoint should not raise an auth error."""
        # Fetching accounts is a lightweight authenticated call available in the sandbox.
        result: Any = _run(sandbox_adapter.get_account_balance())
        assert result is not None


class TestCoinbaseSandboxAccountBalance:
    """Verify that account / balance information can be retrieved."""

    def test_get_accounts_returns_list(self, sandbox_adapter):
        accounts = _run(sandbox_adapter.get_account_balance())
        assert isinstance(accounts, list)

    def test_accounts_have_expected_fields(self, sandbox_adapter):
        accounts = _run(sandbox_adapter.get_account_balance())
        if not accounts:
            pytest.skip("No accounts returned from sandbox - nothing to validate.")
        first = accounts[0]
        # Each account entry should expose at minimum an id / currency / balance.
        for field in ("currency", "available", "total"):
            assert field in first or hasattr(first, field), (
                f"Account entry missing expected field: '{field}'"
            )


class TestCoinbaseSandboxOrderSubmission:
    """Verify that a small BTC-USD order can be placed and cancelled."""

    # Deliberately tiny so the sandbox accepts it without risk.
    _ORDER_SIZE = "0.0001"
    _PRODUCT_ID = "BTC-USD"

    def test_place_market_buy_order(self, sandbox_adapter):
        """Submit a small market-buy order and confirm a non-empty order id is returned."""
        order = _run(
            sandbox_adapter.place_order(
                {
                    "symbol": self._PRODUCT_ID,
                    "side": "buy",
                    "order_type": "market",
                    "quantity": self._ORDER_SIZE,
                    "execution_method": "spot",
                }
            )
        )
        assert order is not None
        order_id = order.get("broker_order_id") if isinstance(order, dict) else None
        assert order_id, "Expected a non-empty order id from place_order()"

    def test_cancel_order(self, sandbox_adapter):
        """Place an order and then cancel it; cancellation should not raise."""
        order = _run(
            sandbox_adapter.place_order(
                {
                    "symbol": self._PRODUCT_ID,
                    "side": "buy",
                    "order_type": "market",
                    "quantity": self._ORDER_SIZE,
                    "execution_method": "spot",
                }
            )
        )
        order_id = order.get("broker_order_id") if isinstance(order, dict) else None
        assert order_id, "Cannot test cancellation - no order id returned."

        result = _run(sandbox_adapter.cancel_order(order_id))
        assert isinstance(result, bool)


class TestCoinbaseSandboxInvalidCredentials:
    """Verify that invalid credentials are rejected with an appropriate error."""

    def test_invalid_credentials_raise(self):
        """An adapter built with bad credentials should reject connection."""
        bad_adapter = CoinbaseAdapter(environment="paper")

        connected = _run(
            bad_adapter.connect(
                api_key="invalid-key-000",
                api_secret="invalid-secret-000",
            )
        )

        assert connected is False


# ---------------------------------------------------------------------------
# Additional integration coverage for BrokerBridge delegation
# ---------------------------------------------------------------------------


class TestBrokerBridgeCoinbaseIntegration:
    """Verify that BrokerBridge correctly delegates to CoinbaseAdapter in sandbox mode."""

    @pytest.fixture(scope="module")
    def sandbox_bridge(self):
        """Create a BrokerBridge configured for Coinbase sandbox."""
        api_key, api_secret = _get_sandbox_credentials()

        from execution_engine.broker_bridge import BrokerBridge
        from execution_engine.config import BrokerType

        return BrokerBridge(
            broker_type=BrokerType.COINBASE,
            environment="paper",
            credential_ref="coinbase-sandbox-test",
            credentials={"api_key": api_key, "api_secret": api_secret},
            account_currency="USD",
            settlement_currency="USD",
        )

    def test_bridge_initialization(self, sandbox_bridge):
        """Bridge should initialize without errors."""
        assert sandbox_bridge is not None

    def test_bridge_balance_fetch(self, sandbox_bridge):
        """BrokerBridge should successfully delegate balance retrieval."""
        assert _run(sandbox_bridge.connect()) is True
        account = _run(sandbox_bridge.get_account_info())
        assert account is not None

    def test_bridge_order_submission_and_cancel(self, sandbox_bridge):
        """Submit and cancel an order through the BrokerBridge."""
        from execution_engine.models import OrderIntent

        assert _run(sandbox_bridge.connect()) is True
        order = _run(
            sandbox_bridge.submit_order(
                OrderIntent(
                    broker_code="coinbase",
                    symbol="BTC-USD",
                    side="BUY",
                    quantity=0.0001,
                    order_type="market",
                )
            )
        )

        assert order is not None

        order_id = getattr(order, "order_id", None)
        assert order_id, "Expected a valid order id returned from BrokerBridge"

        cancel = _run(sandbox_bridge.cancel_order(order_id))

        assert isinstance(cancel, bool)


# ---------------------------------------------------------------------------
# Additional order status validation
# ---------------------------------------------------------------------------


class TestCoinbaseSandboxOrderStatus:
    """Verify that order status retrieval works after order submission."""

    _ORDER_SIZE = "0.0001"
    _PRODUCT_ID = "BTC-USD"

    def test_order_status_round_trip(self, sandbox_adapter):
        """Submit an order and verify its status can be fetched."""
        order = _run(
            sandbox_adapter.place_order(
                {
                    "symbol": self._PRODUCT_ID,
                    "side": "buy",
                    "order_type": "market",
                    "quantity": self._ORDER_SIZE,
                    "execution_method": "spot",
                }
            )
        )

        order_id = order.get("broker_order_id") if isinstance(order, dict) else None
        assert order_id, "Expected order id from submission"

        status = _run(sandbox_adapter.get_order_status(order_id))

        assert status is not None
