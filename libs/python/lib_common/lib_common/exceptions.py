"""Custom exceptions."""


class VMStrategyError(Exception):
    """Base exception for the vynmatrix platform."""


class ConfigurationError(VMStrategyError):
    """Configuration error."""
