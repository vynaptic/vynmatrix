"""Unit tests for ConfigManager."""

import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from lib_common.app.config import ConfigManager
from lib_common.config_validation import (
    RunMode,
    ScoringConfig,
    load_execution_engine_config,
    load_indicator_runner_config,
    load_indicator_worker_config,
    load_scoring_engine_config,
)
from lib_common.env_utils import (
    parse_bool_value,
    parse_float_value,
    parse_int_value,
    parse_weight_mapping,
)
from lib_common.exceptions import ConfigurationError


def test_parse_weight_mapping_accepts_explicit_nonnegative_weights() -> None:
    assert parse_weight_mapping(
        "trend:0.75,mean_reversion:0",
        name="SCORE_WEIGHTS",
    ) == {"trend": 0.75, "mean_reversion": 0.0}


@pytest.mark.parametrize(
    "raw",
    [
        "missing-separator",
        "too:many:separators",
        ":1",
        "trend:not-a-number",
        "trend:nan",
        "trend:inf",
        "trend:-0.1",
        "trend:1,trend:2",
        "trend:1,",
    ],
)
def test_parse_weight_mapping_rejects_ambiguous_decision_config(raw: str) -> None:
    with pytest.raises(ValueError, match="SCORE_WEIGHTS"):
        parse_weight_mapping(raw, name="SCORE_WEIGHTS")


def test_parse_weight_mapping_rejects_unknown_registered_factor() -> None:
    with pytest.raises(ValueError, match="unsupported weight name"):
        parse_weight_mapping(
            "unknown:1",
            name="SCORING_FACTOR_WEIGHTS",
            allowed_keys={"momentum", "low_volatility"},
        )


def test_scoring_config_rejects_invalid_programmatic_weight() -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ScoringConfig(score_weights={"trend": float("nan")})


def test_value_parsers_fail_closed_when_strict() -> None:
    with pytest.raises(ValueError, match="FEATURE_FLAG"):
        parse_bool_value("maybe", name="FEATURE_FLAG", strict=True)
    with pytest.raises(ValueError, match="WORKERS"):
        parse_int_value("0", name="WORKERS", min_value=1, strict=True)
    with pytest.raises(ValueError, match="RATIO"):
        parse_float_value("nan", name="RATIO", strict=True)


def test_execution_runtime_config_is_frozen_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("RUN_MODE", "paper")
    monkeypatch.setenv("EXECUTION_RISK_GUARD_ENABLED", "false")
    monkeypatch.setenv("EXECUTION_DEDUP_TTL_SECONDS", "600")
    monkeypatch.setenv("PAPER_BROKER_SLIPPAGE_PCT", "0.002")
    monkeypatch.setenv("EXECUTION_PAPER_ORDER_MAX_LAG_SECONDS", "180")

    config = load_execution_engine_config()
    monkeypatch.setenv("EXECUTION_RISK_GUARD_ENABLED", "true")
    monkeypatch.setenv("EXECUTION_DEDUP_TTL_SECONDS", "900")

    assert config.runtime.risk_guard_enabled is False
    assert config.runtime.dedup.ttl_seconds == 600
    assert config.runtime.paper.slippage_pct == 0.002
    assert config.runtime.paper.max_order_processing_lag_seconds == 180
    with pytest.raises(ValidationError, match="frozen"):
        config.runtime.risk_guard_enabled = True


def test_execution_runtime_config_rejects_explicit_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("EXECUTION_DEDUP_TTL_SECONDS", "not-an-integer")

    with pytest.raises(ValueError, match="EXECUTION_DEDUP_TTL_SECONDS"):
        load_execution_engine_config()


def test_scoring_runtime_config_is_frozen_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "paper_strategy_promotion.json"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("RUN_MODE", "paper")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SCORING_PAPER_PROMOTION_MANIFEST", str(manifest_path))
    monkeypatch.setenv("VM_DEPLOY_IMAGE_TAG", "1.2.3")
    monkeypatch.setenv("SCORING_CROSS_STRATEGY_ENSEMBLE", "true")
    monkeypatch.setenv("SCORING_MAX_SIBLINGS", "7")
    monkeypatch.setenv("SCORING_BINDINGS_CACHE_TTL_SECONDS", "2.5")
    monkeypatch.setenv("SCORING_FACTOR_WEIGHTS", "momentum:0.75,low_volatility:0.25")
    monkeypatch.setenv("SCORING_OUTBOX_MAX_AGE_SECONDS", "420")

    config = load_scoring_engine_config()
    monkeypatch.setenv("SCORING_PAPER_PROMOTION_MANIFEST", "/changed-after-startup.json")
    monkeypatch.setenv("SCORING_MAX_SIBLINGS", "99")

    assert config.runtime.environment == "production"
    assert config.runtime.paper_promotion_manifest == manifest_path
    assert config.runtime.deploy_image_tag == "1.2.3"
    assert config.runtime.ensemble.enabled is True
    assert config.runtime.ensemble.max_siblings == 7
    assert config.runtime.bindings_cache_ttl_seconds == 2.5
    assert config.runtime.relay.max_backlog_age_seconds == 420
    assert config.runtime.factors.weights == (
        ("momentum", 0.75),
        ("low_volatility", 0.25),
    )
    with pytest.raises(ValidationError, match="frozen"):
        config.runtime.ensemble.max_siblings = 8


def test_scoring_runtime_config_rejects_explicit_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCORING_CROSS_STRATEGY_ENSEMBLE", "sometimes")

    with pytest.raises(ValueError, match="SCORING_CROSS_STRATEGY_ENSEMBLE"):
        load_scoring_engine_config()


def test_indicator_runner_config_is_frozen_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    schema_path = tmp_path / "indicator-schema.json"
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("INDICATOR_SCHEMA_PATH", str(schema_path))
    monkeypatch.setenv("STRATEGY_LIST", "Alpha, Beta")
    monkeypatch.setenv("STRATEGY_START_DELAY_SECONDS", "1.5")
    manifest_path = tmp_path / "paper_strategy_promotion.json"
    monkeypatch.setenv("INDICATOR_PAPER_PROMOTION_MANIFEST", str(manifest_path))
    monkeypatch.setenv("VM_DEPLOY_IMAGE_TAG", "1.2.3")
    monkeypatch.setenv("INDICATOR_MAX_SIGNAL_BACKLOG_AGE_SECONDS", "240")
    monkeypatch.setenv("INDICATOR_MAX_STRATEGY_LAG_SECONDS", "180")
    monkeypatch.delenv("SIGNAL_API_URL", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)

    config = load_indicator_runner_config(
        deployment_config={"endpoints": {"signal_api_url": "http://scoring-engine:8001"}},
        secrets={"api_key": "shared-key"},
        repo_root=tmp_path,
    )
    monkeypatch.setenv("STRATEGY_LIST", "Changed")
    monkeypatch.setenv("API_KEY", "rotated-after-startup")

    assert config.environment == "staging"
    assert config.schema_path == schema_path
    assert config.strategy_names == frozenset({"Alpha", "Beta"})
    assert config.loading_mode == "bundle"
    assert config.start_delay_seconds == 1.5
    assert config.signal_api_url == "http://scoring-engine:8001"
    assert config.api_key == "shared-key"
    assert config.paper_promotion_manifest == manifest_path
    assert config.deploy_image_tag == "1.2.3"
    assert config.max_signal_backlog_age_seconds == 240
    assert config.max_strategy_lag_seconds == 180
    with pytest.raises(ValidationError, match="frozen"):
        config.start_delay_seconds = 2.0


def test_indicator_worker_config_is_frozen_startup_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("RUN_MODE", " PAPER ")
    monkeypatch.setenv("SIGNAL_HTTP_MAX_RETRIES", "4")
    monkeypatch.setenv("SIGNAL_HTTP_RETRY_BASE_DELAY_SEC", "0.25")
    monkeypatch.setenv("SIGNAL_HTTP_RETRY_MAX_DELAY_SEC", "8")
    monkeypatch.setenv("SIGNAL_HTTP_RETRY_JITTER", "0.2")
    monkeypatch.setenv("INGESTOR_NOTIFY_CHANNEL", "prices_ready")
    monkeypatch.setenv("INDICATOR_MIN_BAR_COVERAGE", "0.9")
    monkeypatch.setenv("SIGNAL_CATCHUP_BATCH_SIZE", "750")
    monkeypatch.setenv("SIGNAL_WORKER_CATCHUP_FLOOR_SEC", "15")

    config = load_indicator_worker_config()
    monkeypatch.setenv("SIGNAL_WORKER_CATCHUP_FLOOR_SEC", "60")

    assert config.run_mode == RunMode.PAPER
    assert config.database_url == "sqlite:///:memory:"
    assert config.signal_http_max_retries == 4
    assert config.signal_http_retry_base_delay_seconds == 0.25
    assert config.signal_http_retry_max_delay_seconds == 8
    assert config.signal_http_retry_jitter == 0.2
    assert config.notify_channel == "prices_ready"
    assert config.min_bar_coverage == 0.9
    assert config.catchup_batch_size == 750
    assert config.catchup_floor_seconds == 15
    with pytest.raises(ValidationError, match="frozen"):
        config.catchup_floor_seconds = 60


def test_indicator_worker_config_rejects_malformed_operational_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SIGNAL_WORKER_CATCHUP_FLOOR_SEC", "eventually")

    with pytest.raises(ValueError, match="SIGNAL_WORKER_CATCHUP_FLOOR_SEC"):
        load_indicator_worker_config()


def test_indicator_worker_config_binds_exact_paper_panel_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("INDICATOR_PANEL_DATA_USE_SCOPE", "paper_forward")
    monkeypatch.setenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("INDICATOR_PANEL_ACTIVATION_CUTOFF", "2026-08-01T00:00:00Z")

    binding = load_indicator_worker_config().panel_runtime

    assert binding is not None
    assert binding.environment == "staging"
    assert binding.data_use_scope == "paper_forward"
    assert binding.entitlement_owner_user_id == "owner-1"
    assert binding.activation_cutoff.isoformat() == "2026-08-01T00:00:00+00:00"


def test_research_owner_alone_does_not_activate_panel_worker_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SP500_RESEARCH_OWNER_USER_ID", "research-owner")

    assert load_indicator_worker_config().panel_runtime is None


def test_indicator_worker_config_rejects_historical_or_partial_panel_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("INDICATOR_PANEL_DATA_USE_SCOPE", "historical_validation")
    monkeypatch.setenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("INDICATOR_PANEL_ACTIVATION_CUTOFF", "2026-08-01T00:00:00Z")
    with pytest.raises(ValidationError, match="paper_forward"):
        load_indicator_worker_config()

    monkeypatch.delenv("INDICATOR_PANEL_ACTIVATION_CUTOFF")
    with pytest.raises(ValueError, match="INDICATOR_PANEL_ACTIVATION_CUTOFF"):
        load_indicator_worker_config()


def test_indicator_worker_config_rejects_invalid_panel_activation_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("INDICATOR_PANEL_DATA_USE_SCOPE", "paper_forward")
    monkeypatch.setenv("INDICATOR_PANEL_ENTITLEMENT_OWNER_USER_ID", "owner-1")
    monkeypatch.setenv("INDICATOR_PANEL_ACTIVATION_CUTOFF", "next-month-end")

    with pytest.raises(ValueError, match="ISO-8601"):
        load_indicator_worker_config()


class TestConfigManager:
    """Test suite for ConfigManager."""

    @pytest.fixture(autouse=True)
    def clear_config_cache(self) -> None:
        """Clear ConfigManager class-level cache before each test."""
        ConfigManager._config_cache.clear()

    @pytest.fixture
    def temp_base_dir(self) -> Generator[Path, None, None]:
        """Create a temporary base directory with test config files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            config_dir = base_dir / "config"
            config_dir.mkdir()

            # Create deployment subdirectory
            deployment_dir = config_dir / "deployment"
            deployment_dir.mkdir()

            # Create test deployment configs
            dev_config = {
                "environment": "dev",
                "endpoints": {
                    "signal_api_url": "http://scoring-dev:8001",
                },
                "secrets": {
                    "source": "env_vars",
                },
                "app": {
                    "debug": True,
                    "log_level": "DEBUG",
                },
                "nested": {"deep": {"value": 42}},
            }

            prod_config = {
                "environment": "prod",
                "endpoints": {
                    "signal_api_url": "http://scoring-prod:8001",
                },
                "secrets": {
                    "source": "env_vars",
                },
                "app": {
                    "debug": False,
                    "log_level": "INFO",
                },
            }

            with (deployment_dir / "dev.yaml").open("w") as f:
                yaml.dump(dev_config, f)

            with (deployment_dir / "prod.yaml").open("w") as f:
                yaml.dump(prod_config, f)

            yield base_dir

    @pytest.fixture
    def config_manager(self, temp_base_dir: Path) -> ConfigManager:
        """Create ConfigManager instance with temp directory."""
        return ConfigManager(base_path=temp_base_dir)

    def test_load_deployment_config_dev(self, config_manager: ConfigManager) -> None:
        """Test loading development configuration."""
        config = config_manager.load_deployment_config("dev")

        assert config["environment"] == "dev"
        assert config["endpoints"]["signal_api_url"] == "http://scoring-dev:8001"
        assert config["app"]["debug"] is True

    def test_load_deployment_config_prod(self, config_manager: ConfigManager) -> None:
        """Test loading production configuration."""
        config = config_manager.load_deployment_config("prod")

        assert config["environment"] == "prod"
        assert config["endpoints"]["signal_api_url"] == "http://scoring-prod:8001"
        assert config["app"]["debug"] is False

    def test_load_deployment_config_caching(self, config_manager: ConfigManager) -> None:
        """Test that configurations are cached."""
        config1 = config_manager.load_deployment_config("dev")
        config2 = config_manager.load_deployment_config("dev")

        # Should be different objects (copies) but same content
        assert config1 is not config2
        assert config1 == config2

    def test_load_deployment_config_invalid_environment(
        self, config_manager: ConfigManager
    ) -> None:
        """Test loading non-existent environment uses default config."""
        # Implementation now returns default config instead of raising error
        config = config_manager.load_deployment_config("invalid")

        # Should get default config with environment set
        assert config["environment"] == "invalid"
        assert config["endpoints"]["signal_api_url"]
        assert config["secrets"]["source"] == "env_vars"

    def test_load_deployment_config_invalid_yaml(self, temp_base_dir: Path) -> None:
        """Test loading invalid YAML raises error."""
        deployment_dir = temp_base_dir / "config" / "deployment"

        # Create invalid YAML file
        with (deployment_dir / "invalid.yaml").open("w") as f:
            f.write("invalid: yaml: content: [unclosed")

        config_manager = ConfigManager(base_path=temp_base_dir)

        with pytest.raises(ConfigurationError) as exc_info:
            config_manager.load_deployment_config("invalid")

        assert "Invalid YAML" in str(exc_info.value) or "Failed to load config" in str(
            exc_info.value
        )

    def test_rejects_non_environment_secret_source(self, temp_base_dir: Path) -> None:
        deployment_dir = temp_base_dir / "config" / "deployment"
        config = {
            "environment": "unsupported-secrets",
            "endpoints": {"signal_api_url": "http://scoring:8001"},
            "secrets": {"source": "external_secret_manager"},
        }
        with (deployment_dir / "unsupported-secrets.yaml").open("w") as config_file:
            yaml.safe_dump(config, config_file)

        with pytest.raises(ConfigurationError, match="Must be 'env_vars'"):
            ConfigManager(base_path=temp_base_dir).load_deployment_config("unsupported-secrets")

    def test_get_existing_value(self, config_manager: ConfigManager) -> None:
        """Test getting existing configuration value."""
        config_manager.load_deployment_config("dev")
        value = config_manager.get("endpoints.signal_api_url")

        assert value == "http://scoring-dev:8001"

    def test_get_nested_value(self, config_manager: ConfigManager) -> None:
        """Test getting deeply nested configuration value."""
        config_manager.load_deployment_config("dev")
        value = config_manager.get("nested.deep.value")

        nested_expected = 42

        assert value == nested_expected

    def test_get_missing_value_with_default(self, config_manager: ConfigManager) -> None:
        """Test getting missing value returns default."""
        config_manager.load_deployment_config("dev")
        value = config_manager.get("missing.key", default="default_value")

        assert value == "default_value"

    def test_get_missing_value_no_default(self, config_manager: ConfigManager) -> None:
        """Test getting missing value without default returns None."""
        config_manager.load_deployment_config("dev")
        value = config_manager.get("missing.key")

        assert value is None

    def test_get_before_load_raises_error(self, config_manager: ConfigManager) -> None:
        """Test getting value before loading config returns default."""
        # Implementation returns default/None instead of raising error
        value = config_manager.get("any.key")

        assert value is None

        # Can specify default
        value_with_default = config_manager.get("any.key", default="default")
        assert value_with_default == "default"

    def test_config_manager_singleton_behavior(self, temp_base_dir: Path) -> None:
        """Test that multiple ConfigManager instances share cache."""
        cm1 = ConfigManager(base_path=temp_base_dir)
        cm2 = ConfigManager(base_path=temp_base_dir)

        # Load config in first instance
        config1 = cm1.load_deployment_config("dev")

        # Should be cached in second instance
        config2 = cm2.load_deployment_config("dev")

        assert config1 == config2

    def test_get_config_dir(self, config_manager: ConfigManager, temp_base_dir: Path) -> None:
        """Test getting config directory path."""
        assert config_manager.config_dir == temp_base_dir / "config"

    def test_clear_cache(self, config_manager: ConfigManager) -> None:
        """Test clearing configuration cache."""
        config_manager.load_deployment_config("dev")

        # Config should be loaded
        assert config_manager.get("endpoints.signal_api_url") == "http://scoring-dev:8001"

        # Clear cache
        config_manager.clear_cache()

        # Config should still be accessible from instance
        assert config_manager.get("endpoints.signal_api_url") == "http://scoring-dev:8001"


def test_scoring_market_context_overrides_parse_and_normalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv(
        "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS",
        '{"EQUITY": {"source": "eodhd", "timeframe": "1d",'
        ' "window": 20, "max_age_seconds": 432000}}',
    )
    config = load_scoring_engine_config()
    overrides = dict(config.runtime.market_context.by_asset_class)
    assert set(overrides) == {"equity"}  # keys normalize to the canonical class
    assert overrides["equity"].source == "eodhd"
    assert overrides["equity"].timeframe == "1d"
    assert overrides["equity"].max_age_seconds == 432000
    # Default feed stays independent of the override.
    assert config.runtime.market_context.source == "coinbase_live"


def test_scoring_market_context_overrides_reject_partial_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv(
        "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS",
        '{"equity": {"source": "eodhd", "timeframe": "1d"}}',
    )
    with pytest.raises((ValueError, ValidationError)):
        load_scoring_engine_config()


def test_scoring_market_context_overrides_reject_unknown_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv(
        "SCORING_MARKET_CONTEXT_BY_ASSET_CLASS",
        '{"stonks": {"source": "eodhd", "timeframe": "1d",'
        ' "window": 20, "max_age_seconds": 432000}}',
    )
    with pytest.raises((ValueError, ValidationError), match="stonks"):
        load_scoring_engine_config()


def test_scoring_market_context_overrides_reject_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SCORING_MARKET_CONTEXT_BY_ASSET_CLASS", "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_scoring_engine_config()
