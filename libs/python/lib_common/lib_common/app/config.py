"""Configuration management with validation and caching."""

import os
from pathlib import Path
from typing import Any, ClassVar, Optional, cast

import yaml

from lib_common.exceptions import ConfigurationError
from lib_common.logging import get_logger

logger = get_logger(__name__)


class ConfigManager:
    """
    Manages application configuration with validation and caching.

    Supports environment-specific configurations and provides
    safe access to configuration values with defaults.
    """

    _instance: Optional["ConfigManager"] = None
    _config_cache: ClassVar[dict[str, Any]] = {}

    def __init__(self, base_path: Path | None = None) -> None:
        """
        Initialize configuration manager.

        Args:
            base_path: Base path for configuration files.
                      Defaults to current working directory.
        """
        self.base_path = base_path or Path.cwd()
        self.config_dir = self.base_path / "config"
        self._loaded_config: dict[str, Any] = {}

    @classmethod
    def get_instance(cls, base_path: Path | None = None) -> "ConfigManager":
        """
        Get singleton instance of ConfigManager.

        Args:
            base_path: Base path for configuration files

        Returns:
            ConfigManager instance
        """
        if cls._instance is None:
            cls._instance = cls(base_path)
        return cls._instance

    def load_deployment_config(self, environment: str) -> dict[str, Any]:
        """
        Load deployment configuration for specified environment.

        Args:
            environment: Environment name (dev, staging, production)

        Returns:
            Configuration dictionary

        Raises:
            ConfigurationError: If configuration loading fails
        """
        # Check cache first
        cache_key = f"deployment_{environment}"
        if cache_key in self._config_cache:
            logger.debug("Using cached config for environment: %s", environment)
            return cast(dict[str, Any], self._config_cache[cache_key].copy())

        config_file = self.config_dir / "deployment" / f"{environment}.yaml"

        if not config_file.exists():
            logger.warning("Deployment config not found: %s, using defaults", config_file)
            config = self._get_default_config(environment)
        else:
            try:
                config = self._load_yaml_file(config_file)
                logger.info("Loaded deployment config from %s", config_file)
            except Exception as err:
                msg = f"Failed to load config from {config_file}"
                raise ConfigurationError(msg) from err

        # Validate configuration
        self._validate_deployment_config(config, environment)

        # Cache the configuration
        self._config_cache[cache_key] = config.copy()
        self._loaded_config = config

        return config

    def _load_yaml_file(self, file_path: Path) -> dict[str, Any]:
        """
        Load and parse YAML file.

        Args:
            file_path: Path to YAML file

        Returns:
            Parsed YAML content as dictionary

        Raises:
            ConfigurationError: If file cannot be read or parsed
        """
        try:
            with file_path.open(encoding="utf-8") as f:
                content = yaml.safe_load(f)
                if content is None:
                    return {}
                if not isinstance(content, dict):
                    msg = f"Config file must contain a dictionary, got {type(content)}"
                    raise ConfigurationError(msg)
                return content
        except yaml.YAMLError as err:
            msg = f"Invalid YAML in {file_path}"
            raise ConfigurationError(msg) from err
        except OSError as err:
            msg = f"Cannot read config file {file_path}"
            raise ConfigurationError(msg) from err

    def _get_default_config(self, environment: str) -> dict[str, Any]:
        """
        Get default configuration for environment.

        Args:
            environment: Environment name

        Returns:
            Default configuration dictionary
        """
        return {
            "environment": environment,
            "endpoints": {
                "signal_api_url": os.environ.get("SIGNAL_API_URL", "http://localhost:8001"),
            },
            "live_mode": environment == "production",
            "secrets": {"source": "env_vars", "names": ["api_key"]},
        }

    def _validate_deployment_config(self, config: dict[str, Any], _environment: str) -> None:
        """
        Validate deployment configuration.

        Args:
            config: Configuration to validate
            environment: Environment name

        Raises:
            ConfigurationError: If configuration is invalid
        """
        required_fields = ["environment", "endpoints", "secrets"]
        missing_fields = [field for field in required_fields if field not in config]

        if missing_fields:
            msg = f"Missing required config fields: {', '.join(missing_fields)}"
            raise ConfigurationError(msg)

        if config["environment"] != _environment:
            msg = (
                f"Deployment config environment {config['environment']!r} "
                f"does not match requested environment {_environment!r}"
            )
            raise ConfigurationError(msg)

        endpoints = config["endpoints"]
        if not isinstance(endpoints, dict):
            msg = "'endpoints' configuration must be a dictionary"
            raise ConfigurationError(msg)
        signal_api_url = endpoints.get("signal_api_url")
        if not isinstance(signal_api_url, str) or not signal_api_url.strip():
            msg = "'endpoints.signal_api_url' must be a non-empty string"
            raise ConfigurationError(msg)

        # Validate secrets configuration
        if "secrets" in config:
            secrets_config = config["secrets"]
            if not isinstance(secrets_config, dict):
                msg = "'secrets' configuration must be a dictionary"
                raise ConfigurationError(msg)

            source = secrets_config.get("source")
            if source != "env_vars":
                msg = f"Invalid secrets source: {source}. Must be 'env_vars'"
                raise ConfigurationError(msg)

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value safely.

        Args:
            key: Configuration key (supports dot notation, e.g.,
                ``endpoints.signal_api_url``)
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key.split(".")
        value: Any = self._loaded_config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def clear_cache(self) -> None:
        """Clear configuration cache."""
        self._config_cache.clear()
        logger.debug("Configuration cache cleared")
