"""Environment variable parsing utilities.

This module provides standardized functions for parsing environment variables
with proper type conversion, defaults, and validation.

Usage:
    from lib_common.env_utils import (
        parse_bool_env,
        parse_int_env,
        parse_float_env,
        parse_list_env,
    )

    # Boolean parsing (handles various formats)
    debug_mode = parse_bool_env("DEBUG", default=False)

    # Integer parsing
    port = parse_int_env("PORT", default=8080)
"""

import logging
import math
import os
import re
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

# Common true/false string values
_TRUE_VALUES: set[str] = {"true", "1", "yes", "on", "enabled", "t", "y"}
_FALSE_VALUES: set[str] = {"false", "0", "no", "off", "disabled", "f", "n", ""}


def parse_weight_mapping(
    raw: str,
    *,
    name: str,
    allowed_keys: set[str] | None = None,
) -> dict[str, float]:
    """Parse a strict ``name:nonnegative_weight`` comma-separated mapping.

    Trading weights are decision inputs, so malformed, duplicate, non-finite,
    negative, or unknown entries raise instead of silently changing scoring
    behavior.
    """
    if not raw.strip():
        return {}

    weights: dict[str, float] = {}
    for entry in raw.split(","):
        if entry.count(":") != 1:
            msg = f"{name} entry must contain exactly one ':': {entry!r}"
            raise ValueError(msg)
        key_raw, value_raw = entry.split(":", 1)
        key = key_raw.strip()
        if not key:
            msg = f"{name} contains an empty weight name"
            raise ValueError(msg)
        if key in weights:
            msg = f"{name} contains duplicate weight name {key!r}"
            raise ValueError(msg)
        if allowed_keys is not None and key not in allowed_keys:
            msg = f"{name} contains unsupported weight name {key!r}"
            raise ValueError(msg)
        try:
            value = float(value_raw.strip())
        except ValueError as exc:
            msg = f"{name} weight for {key!r} is not numeric"
            raise ValueError(msg) from exc
        if not math.isfinite(value) or value < 0:
            msg = f"{name} weight for {key!r} must be finite and nonnegative"
            raise ValueError(msg)
        weights[key] = value
    return weights


def parse_bool_env(
    name: str,
    default: bool = False,
    logger: logging.Logger | None = None,
    *,
    strict: bool = False,
) -> bool:
    """Parse a boolean environment variable.

    Handles various string representations:
    - True: "true", "1", "yes", "on", "enabled", "t", "y" (case-insensitive)
    - False: "false", "0", "no", "off", "disabled", "f", "n", "" (case-insensitive)

    Args:
        name: Environment variable name
        default: Default value if not set or unrecognized
        logger: Optional logger for warnings

    Returns:
        Parsed boolean value

    Examples:
        >>> import os
        >>> os.environ["MY_FLAG"] = "true"
        >>> parse_bool_env("MY_FLAG")
        True
        >>> os.environ["MY_FLAG"] = "0"
        >>> parse_bool_env("MY_FLAG")
        False
    """
    return parse_bool_value(
        os.environ.get(name),
        name=name,
        default=default,
        logger=logger,
        strict=strict,
    )


def parse_bool_value(
    value: str | None,
    *,
    name: str,
    default: bool = False,
    logger: logging.Logger | None = None,
    strict: bool = False,
) -> bool:
    """Parse one optional boolean string using the canonical environment syntax.

    ``parse_bool_env`` is the normal process-environment entry point. This
    value-oriented variant exists for explicit environment adapters that accept
    a caller-provided mapping, such as alert-sink construction.
    """
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False

    msg = f"Unrecognized boolean value for {name}={value!r}"
    if strict:
        raise ValueError(msg)
    if logger:
        logger.warning(
            "Unrecognized boolean value for %s='%s'; using default=%s",
            name,
            value,
            default,
        )
    return default


def parse_int_env(
    name: str,
    default: int = 0,
    min_value: int | None = None,
    max_value: int | None = None,
    logger: logging.Logger | None = None,
    *,
    strict: bool = False,
) -> int:
    """Parse an integer environment variable.

    Args:
        name: Environment variable name
        default: Default value if not set or invalid
        min_value: Optional minimum value (returns default if below)
        max_value: Optional maximum value (returns default if above)
        logger: Optional logger for warnings

    Returns:
        Parsed integer value

    Examples:
        >>> import os
        >>> os.environ["PORT"] = "8080"
        >>> parse_int_env("PORT", default=3000)
        8080
        >>> parse_int_env("UNSET_VAR", default=42)
        42
    """
    return parse_int_value(
        os.environ.get(name),
        name=name,
        default=default,
        min_value=min_value,
        max_value=max_value,
        logger=logger,
        strict=strict,
    )


def parse_int_value(
    value: str | None,
    *,
    name: str,
    default: int = 0,
    min_value: int | None = None,
    max_value: int | None = None,
    logger: logging.Logger | None = None,
    strict: bool = False,
) -> int:
    """Parse one optional integer string with canonical range handling."""
    if value is None:
        return default

    try:
        result = int(value.strip())
    except ValueError as exc:
        msg = f"Invalid integer value for {name}={value!r}"
        if strict:
            raise ValueError(msg) from exc
        if logger:
            logger.warning(
                "Invalid integer value for %s='%s'; using default=%d",
                name,
                value,
                default,
            )
        return default

    if min_value is not None and result < min_value:
        msg = f"{name}={result} is below minimum {min_value}"
        if strict:
            raise ValueError(msg)
        if logger:
            logger.warning(
                "%s=%d is below minimum %d; using default=%d",
                name,
                result,
                min_value,
                default,
            )
        return default

    if max_value is not None and result > max_value:
        msg = f"{name}={result} is above maximum {max_value}"
        if strict:
            raise ValueError(msg)
        if logger:
            logger.warning(
                "%s=%d is above maximum %d; using default=%d",
                name,
                result,
                max_value,
                default,
            )
        return default

    return result


def parse_float_env(
    name: str,
    default: float = 0.0,
    min_value: float | None = None,
    max_value: float | None = None,
    logger: logging.Logger | None = None,
    *,
    strict: bool = False,
) -> float:
    """Parse a float environment variable.

    Args:
        name: Environment variable name
        default: Default value if not set or invalid
        min_value: Optional minimum value
        max_value: Optional maximum value
        logger: Optional logger for warnings

    Returns:
        Parsed float value
    """
    return parse_float_value(
        os.environ.get(name),
        name=name,
        default=default,
        min_value=min_value,
        max_value=max_value,
        logger=logger,
        strict=strict,
    )


def parse_float_value(
    value: str | None,
    *,
    name: str,
    default: float = 0.0,
    min_value: float | None = None,
    max_value: float | None = None,
    logger: logging.Logger | None = None,
    strict: bool = False,
) -> float:
    """Parse one optional finite float string with canonical range handling."""
    if value is None:
        return default

    try:
        result = float(value.strip())
    except ValueError as exc:
        msg = f"Invalid float value for {name}={value!r}"
        if strict:
            raise ValueError(msg) from exc
        if logger:
            logger.warning(
                "Invalid float value for %s='%s'; using default=%f",
                name,
                value,
                default,
            )
        return default

    if not math.isfinite(result):
        msg = f"{name}={value!r} must be finite"
        if strict:
            raise ValueError(msg)
        if logger:
            logger.warning(
                "Non-finite float value for %s='%s'; using default=%f",
                name,
                value,
                default,
            )
        return default

    if min_value is not None and result < min_value:
        msg = f"{name}={result} is below minimum {min_value}"
        if strict:
            raise ValueError(msg)
        if logger:
            logger.warning(
                "%s=%f is below minimum %f; using default=%f",
                name,
                result,
                min_value,
                default,
            )
        return default

    if max_value is not None and result > max_value:
        msg = f"{name}={result} is above maximum {max_value}"
        if strict:
            raise ValueError(msg)
        if logger:
            logger.warning(
                "%s=%f is above maximum %f; using default=%f",
                name,
                result,
                max_value,
                default,
            )
        return default

    return result


def parse_list_env(
    name: str,
    default: list[str] | None = None,
    separator: str = ",",
    strip_items: bool = True,
    filter_empty: bool = True,
) -> list[str]:
    """Parse a list environment variable (comma-separated by default).

    Args:
        name: Environment variable name
        default: Default value if not set
        separator: String to split on (default: ",")
        strip_items: Whether to strip whitespace from items
        filter_empty: Whether to filter out empty strings

    Returns:
        List of string values

    Examples:
        >>> import os
        >>> os.environ["HOSTS"] = "host1, host2, host3"
        >>> parse_list_env("HOSTS")
        ['host1', 'host2', 'host3']
    """
    if default is None:
        default = []

    value = os.environ.get(name)
    if value is None:
        return default

    items = value.split(separator)

    if strip_items:
        items = [item.strip() for item in items]

    if filter_empty:
        items = [item for item in items if item]

    return items


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """Parse a single .env line into (key, value).

    Supports inline comments for unquoted values and preserves # inside quotes.
    Returns None for blank/comment/invalid lines.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None

    key, value = stripped.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        value = value[1:-1]
    else:
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()

    return key, value


def load_dotenv_file(path: str) -> dict[str, str]:
    """Load a .env file into a dict (KEY=VALUE)."""
    values: dict[str, str] = {}
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            for line in handle:
                parsed = parse_dotenv_line(line)
                if parsed is None:
                    continue
                key, value = parsed
                values[key] = value
    except FileNotFoundError:
        pass
    return values


def build_database_url(
    *,
    env: str = "dev",
    dotenv_values: dict[str, str] | None = None,
) -> str:
    """Build a PostgreSQL database URL from environment variables.

    Resolution order for each component:
    1. OS environment variable
    2. ``dotenv_values`` dict (if provided)

    DB_USER and DB_PASSWORD are always required -- there are no hardcoded
    identity or secret defaults for any environment. Set them via environment
    variables, a ``.env`` file, or ``DATABASE_URL``.

    Args:
        env: Deployment environment (``"dev"``, ``"staging"``, ``"prod"``).
        dotenv_values: Optional mapping from a ``.env`` file for fallback values.

    Returns:
        A fully-qualified PostgreSQL connection URL.

    Raises:
        ValueError: If ``DB_USER`` or ``DB_PASSWORD`` cannot be resolved.
    """
    dv = dotenv_values or {}

    # Full override
    full_url = os.environ.get("DATABASE_URL", dv.get("DATABASE_URL", ""))
    if full_url:
        return full_url

    db_host = os.environ.get("DB_HOST", dv.get("DB_HOST", "localhost"))
    db_port = os.environ.get("DB_PORT", dv.get("DB_PORT", "5432"))
    db_name = os.environ.get("DB_NAME", dv.get("DB_NAME", "vm_trading"))
    db_user = os.environ.get("DB_USER", dv.get("DB_USER", "")).strip()
    if not db_user:
        msg = (
            f"DB_USER is required for '{env}' environment. "
            "Set an explicit least-privilege login or DATABASE_URL."
        )
        raise ValueError(msg)

    db_password = os.environ.get("DB_PASSWORD", dv.get("DB_PASSWORD", ""))

    if not db_password:
        msg = (
            f"DB_PASSWORD is required for '{env}' environment. "
            "Set via environment variable or .env file."
        )
        raise ValueError(msg)

    return f"postgresql://{db_user}:{db_password}@{db_host}:{db_port}/{db_name}"


__all__ = [
    "build_database_url",
    "load_dotenv_file",
    "parse_bool_env",
    "parse_bool_value",
    "parse_dotenv_line",
    "parse_float_env",
    "parse_float_value",
    "parse_int_env",
    "parse_int_value",
    "parse_list_env",
]
