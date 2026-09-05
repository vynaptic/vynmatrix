"""Broker capability matrix and documentation.

This module provides a complete capability matrix for all supported brokers,
documenting their support for various features like multi-leg orders,
paper trading, asset classes, and rate limits.
"""

from dataclasses import dataclass, field


@dataclass
class BrokerCapabilityMatrix:
    """
    Complete broker capability documentation.

    Used for:
    - Routing decisions (which broker can handle this order type)
    - Validation (can this broker do multi-leg options?)
    - Paper trading setup (which URL to use)
    - Rate limiting configuration
    """

    broker_code: str
    display_name: str

    # Asset support
    supported_assets: list[str] = field(default_factory=lambda: ["crypto"])

    # Execution modes
    supports_spot: bool = True
    supports_margin: bool = False
    supports_perpetual: bool = False
    supports_futures: bool = False
    supports_options: bool = False
    supports_exact_fill_retrieval: bool = False
    # True only when this broker has a broker-specific, authenticated
    # certification workflow in addition to satisfying the exact-fill
    # contract.  Adapter availability alone never grants live authority.
    live_certification_implemented: bool = False

    # Options specifics
    supports_multi_leg_native: bool = False
    max_legs_per_order: int = 1
    supported_option_strategies: list[str] | None = None

    # Paper trading
    has_native_paper: bool = False
    paper_base_url: str | None = None
    paper_ws_url: str | None = None
    live_base_url: str | None = None
    live_ws_url: str | None = None

    # Symbol mapping
    symbol_format: str = ""
    requires_symbol_mapping: bool = True
    symbol_separator: str = "-"
    requires_broker_instrument_id: bool = False
    requires_broker_instrument_type: bool = False
    broker_instrument_id_format: str | None = None

    # Rate limits
    requests_per_second: float = 10.0
    requests_per_minute: float = 300.0
    burst_limit: int = 20

    # Order constraints
    supported_order_types: list[str] = field(default_factory=list)
    min_order_sizes: dict[str, float] | None = None
    max_leverage: float = 1.0

    # Authentication
    regions: list[str] = field(default_factory=list)
    auth_type: str = "api_key"  # api_key, oauth, jwt, tws
    requires_passphrase: bool = False
    requires_subaccount: bool = False

    def supports_mode(self, mode: str) -> bool:
        """Check if broker supports an execution mode."""
        supports_two_leg = (
            self.supports_options
            and self.supports_multi_leg_native
            and self.max_legs_per_order >= 2  # noqa: PLR2004
        )
        supports_four_leg = (
            self.supports_options
            and self.supports_multi_leg_native
            and self.max_legs_per_order >= 4  # noqa: PLR2004
        )
        mode_map = {
            "spot": self.supports_spot,
            "margin": self.supports_margin,
            "perpetual": self.supports_perpetual,
            "futures": self.supports_futures,
            "bull_call": supports_two_leg,
            "bear_put": supports_two_leg,
            "bull_put": supports_two_leg,
            "bear_call": supports_two_leg,
            "iron_condor": supports_four_leg,
            "straddle": supports_two_leg,
            "strangle": supports_two_leg,
            "options_single": self.supports_options,
        }
        return mode_map.get(mode, False)

    def supports_asset(self, asset_class: str) -> bool:
        """Check if broker supports an asset class."""
        return asset_class in self.supported_assets

    def execution_methods(self) -> list[str]:
        """Return the adapter execution methods implied by this matrix."""
        methods: list[str] = []
        if self.supports_spot:
            methods.append("spot")
        if self.supports_margin:
            methods.append("margin")
        if self.supports_perpetual:
            methods.append("perpetual")
        if self.supports_futures:
            methods.append("futures")
        if self.supports_options:
            methods.append("options_single")
            if self.supports_multi_leg_native:
                methods.append("options_strategy")
        return methods

    def catalogue_payload(self) -> dict[str, list[str] | bool]:
        """Return the canonical database capability document."""
        features: list[str] = []
        for feature, supported in (
            ("spot", self.supports_spot),
            ("margin", self.supports_margin),
            ("perpetual", self.supports_perpetual),
            ("futures", self.supports_futures),
            ("options", self.supports_options),
        ):
            if supported:
                features.append(feature)
        return {
            "asset_classes": list(self.supported_assets),
            "order_types": list(self.supported_order_types),
            "features": features,
            "regions": list(self.regions),
            "exact_fill_retrieval": self.supports_exact_fill_retrieval,
            "live_certification_implemented": self.live_certification_implemented,
        }

    def get_base_url(self, environment: str) -> str | None:
        """Get base URL for environment."""
        if environment == "paper":
            return self.paper_base_url
        return self.live_base_url


# =============================================================================
# Broker Capability Definitions
# =============================================================================

COINBASE_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="coinbase",
    display_name="Coinbase Advanced Trade",
    supported_assets=["crypto"],
    supports_spot=True,
    supports_margin=False,
    supports_perpetual=False,  # Coinbase International only, not Advanced Trade
    supports_options=False,
    supports_exact_fill_retrieval=True,
    live_certification_implemented=True,
    supports_multi_leg_native=False,
    has_native_paper=True,
    paper_base_url="https://api-sandbox.coinbase.com",
    live_base_url="https://api.coinbase.com",
    paper_ws_url=None,
    live_ws_url="wss://advanced-trade-ws.coinbase.com",
    symbol_format="BTC-USD",
    symbol_separator="-",
    supported_order_types=["market", "limit", "stop", "stop_limit"],
    requests_per_second=10,
    requests_per_minute=300,
    min_order_sizes={"BTC": 0.0001, "ETH": 0.001, "SOL": 0.01},
    max_leverage=1.0,
    auth_type="jwt",
    regions=["US", "EU", "UK"],
    requires_passphrase=False,
)

DERIBIT_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="deribit",
    display_name="Deribit",
    supported_assets=["crypto"],
    supports_spot=False,  # Deribit is derivatives only
    supports_margin=False,
    supports_perpetual=True,
    supports_futures=True,
    supports_options=True,
    supports_exact_fill_retrieval=True,
    supports_multi_leg_native=True,  # Deribit supports combo/spread orders
    max_legs_per_order=2,
    supported_option_strategies=["spread", "straddle", "strangle"],
    has_native_paper=True,
    paper_base_url="https://test.deribit.com/api/v2",
    live_base_url="https://www.deribit.com/api/v2",
    paper_ws_url="wss://test.deribit.com/ws/api/v2",
    live_ws_url="wss://www.deribit.com/ws/api/v2",
    symbol_format="BTC-PERPETUAL",  # or BTC-25DEC24-50000-C for options
    symbol_separator="-",
    supported_order_types=["market", "limit", "stop_limit"],
    requests_per_second=20,
    requests_per_minute=600,
    min_order_sizes={"BTC": 0.001, "ETH": 0.01},
    max_leverage=50.0,
    auth_type="api_key",
    regions=["GLOBAL"],
    requires_passphrase=False,
)

IBKR_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="ibkr",
    display_name="Interactive Brokers",
    supported_assets=[
        "equity",
        "etf",
        "futures",
        "options",
        "fx",
        "crypto",
        "commodities",
    ],
    supports_spot=True,
    supports_margin=True,
    supports_perpetual=False,  # IBKR doesn't do crypto perpetuals
    supports_futures=True,
    supports_options=True,
    supports_multi_leg_native=True,  # IBKR combo orders
    max_legs_per_order=4,
    supported_option_strategies=[
        "spread",
        "straddle",
        "strangle",
        "butterfly",
        "condor",
        "iron_condor",
        "collar",
    ],
    has_native_paper=True,
    paper_base_url="https://localhost:5000/v1/api",  # TWS paper trading gateway
    live_base_url="https://localhost:5000/v1/api",  # Same gateway, different account
    symbol_format="AAPL",  # ConId-based internally
    symbol_separator="",
    requires_broker_instrument_id=True,
    broker_instrument_id_format="positive_integer",
    supported_order_types=[
        "market",
        "limit",
        "stop",
        "stop_limit",
        "trailing_stop",
        "bracket",
    ],
    requests_per_second=50,  # TWS is more generous
    requests_per_minute=1000,
    max_leverage=4.0,  # Reg-T margin
    auth_type="oauth",  # TWS connection or OAuth
    regions=["US", "EU", "ASIA"],
    requires_passphrase=False,
    requires_subaccount=True,  # Multi-account support
)

DELTA_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="delta",
    display_name="Delta Exchange",
    supported_assets=["crypto"],
    supports_spot=False,
    supports_margin=False,
    supports_perpetual=True,
    supports_futures=True,
    supports_options=True,
    supports_multi_leg_native=False,
    max_legs_per_order=1,
    supported_option_strategies=None,
    has_native_paper=True,
    # Global defaults. India-linked accounts select their distinct official
    # regional hosts from credential metadata; the adapter never falls back
    # across regions.
    paper_base_url="https://testnet-api.delta.exchange/v2",
    live_base_url="https://api.delta.exchange/v2",
    paper_ws_url="wss://testnet-socket.delta.exchange",
    live_ws_url="wss://socket.delta.exchange",
    symbol_format="BTCUSD",
    symbol_separator="",
    supported_order_types=["market", "limit", "stop", "stop_limit"],
    requests_per_second=20,
    requests_per_minute=600,
    min_order_sizes={"BTC": 0.001, "ETH": 0.01},
    max_leverage=100.0,
    auth_type="api_key",
    regions=["GLOBAL", "IN"],
    requires_passphrase=False,
)

ZERODHA_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="zerodha",
    display_name="Zerodha Kite",
    supported_assets=["equity", "etf", "options", "futures", "fx"],
    supports_spot=True,
    supports_margin=True,  # Intraday margin
    supports_perpetual=False,  # No crypto perpetuals
    supports_futures=True,  # NSE/BSE F&O
    supports_options=True,  # NSE/BSE options
    supports_multi_leg_native=False,  # GTT orders only, no native spread
    max_legs_per_order=1,
    supported_option_strategies=None,
    has_native_paper=False,  # No official paper trading API
    paper_base_url=None,
    live_base_url="https://api.kite.trade",
    live_ws_url="wss://ws.kite.trade",
    symbol_format="RELIANCE",  # NSE/BSE instrument tokens
    symbol_separator="",
    supported_order_types=["market", "limit", "stop", "stop_limit"],
    requests_per_second=3,  # Zerodha is restrictive
    requests_per_minute=180,
    max_leverage=5.0,  # SEBI intraday limits
    auth_type="oauth",  # Kite Connect OAuth
    regions=["IN"],
    requires_passphrase=False,
)

SAXO_CAPABILITIES = BrokerCapabilityMatrix(
    broker_code="saxo",
    display_name="Saxo Bank OpenAPI",
    supported_assets=["equity", "etf", "futures", "options", "fx", "commodities"],
    supports_spot=True,
    supports_margin=True,
    supports_perpetual=False,
    supports_futures=True,
    supports_options=True,
    # Saxo's support is option-root/exchange specific. The current routing
    # contract cannot prove CanParticipateInMultiLegOrder, so it fails closed.
    supports_multi_leg_native=False,
    max_legs_per_order=1,
    supported_option_strategies=None,
    has_native_paper=True,
    paper_base_url="https://gateway.saxobank.com/sim/openapi",
    live_base_url="https://gateway.saxobank.com/openapi",
    paper_ws_url="wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws",
    live_ws_url="wss://live-streaming.saxobank.com/oapi/streaming/ws",
    symbol_format="UIC + AssetType",
    symbol_separator="",
    requires_broker_instrument_id=True,
    requires_broker_instrument_type=True,
    broker_instrument_id_format="positive_integer",
    supported_order_types=["market", "limit", "stop", "stop_limit"],
    requests_per_second=2,
    requests_per_minute=120,
    burst_limit=2,
    max_leverage=1.0,
    auth_type="oauth",
    regions=["global"],
    requires_passphrase=False,
    requires_subaccount=True,
)


# =============================================================================
# Registry — single source of truth for the deployable broker fleet
# =============================================================================


@dataclass(frozen=True)
class BrokerSpec:
    """One row per supported broker — the single place a broker is declared.

    Pairs a broker's canonical code (``BrokerType.value``) with the factory
    registration code, the adapter to import, and its capability matrix. The
    factory registration, the capability lookup (including factory-code
    aliases) and the execution-engine ``BrokerType -> factory_code`` bridge map
    are all *derived* from this table, so adding a broker means adding one
    ``BrokerSpec`` (plus its capability matrix + adapter class), not editing
    three hand-maintained mappings that silently drift.
    """

    code: str  # canonical broker code == BrokerType.value
    factory_code: str  # code the BrokerFactory registers the adapter under
    adapter_module: str  # import path of the adapter module
    adapter_class: str  # adapter class name within that module
    capabilities: BrokerCapabilityMatrix


BROKER_SPECS: tuple[BrokerSpec, ...] = (
    BrokerSpec(
        code="coinbase",
        factory_code="coinbase",
        adapter_module="lib_infrastructure.brokers.adapters.coinbase",
        adapter_class="CoinbaseAdapter",
        capabilities=COINBASE_CAPABILITIES,
    ),
    BrokerSpec(
        code="deribit",
        factory_code="deribit",
        adapter_module="lib_infrastructure.brokers.adapters.deribit",
        adapter_class="DeribitAdapter",
        capabilities=DERIBIT_CAPABILITIES,
    ),
    BrokerSpec(
        code="ibkr",
        factory_code="interactive_brokers",
        adapter_module="lib_infrastructure.brokers.adapters.ibkr",
        adapter_class="IBKRAdapter",
        capabilities=IBKR_CAPABILITIES,
    ),
    BrokerSpec(
        code="delta",
        factory_code="delta_exchange",
        adapter_module="lib_infrastructure.brokers.adapters.delta",
        adapter_class="DeltaExchangeAdapter",
        capabilities=DELTA_CAPABILITIES,
    ),
    BrokerSpec(
        code="zerodha",
        factory_code="zerodha",
        adapter_module="lib_infrastructure.brokers.adapters.zerodha",
        adapter_class="ZerodhaAdapter",
        capabilities=ZERODHA_CAPABILITIES,
    ),
    BrokerSpec(
        code="saxo",
        factory_code="saxo",
        adapter_module="lib_infrastructure.brokers.adapters.saxo",
        adapter_class="SaxoAdapter",
        capabilities=SAXO_CAPABILITIES,
    ),
)

BROKER_SPECS_BY_CODE: dict[str, BrokerSpec] = {spec.code: spec for spec in BROKER_SPECS}


def _build_capability_index() -> dict[str, BrokerCapabilityMatrix]:
    """Index capabilities by both canonical code and factory-code alias."""
    index: dict[str, BrokerCapabilityMatrix] = {}
    for spec in BROKER_SPECS:
        index[spec.code] = spec.capabilities
        index[spec.factory_code] = spec.capabilities  # factory-code alias
    return index


# Lookups keyed by canonical code AND factory-code alias (e.g. both ``ibkr``
# and ``interactive_brokers`` resolve to the IBKR matrix), derived so the
# alias set can never drift from the registration codes.
BROKER_CAPABILITIES: dict[str, BrokerCapabilityMatrix] = _build_capability_index()


def get_broker_capabilities(broker_code: str) -> BrokerCapabilityMatrix | None:
    """Get capabilities for a broker."""
    return BROKER_CAPABILITIES.get(broker_code.lower())
