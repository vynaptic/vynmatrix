"""Resolve one configuration for two vantage points: the host and the containers.

``.env`` addresses PostgreSQL as ``postgres:5432`` because that is what the
declared containers resolve. Host-side maintenance reaches the same server on
the published loopback listener. Every command that connects from the host
resolves its URLs through :func:`resolve_host_env` instead of asking an operator
to export a rewritten connection string.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

CONTAINER_DATABASE_HOST = "postgres"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_DB_PORT = 5432
_MAX_PORT = 65535

#: Every connection string ``.env`` carries, in the order ``.env.example`` lists them.
DATABASE_URL_KEYS: tuple[str, ...] = (
    "ADMIN_DATABASE_URL",
    "MIGRATION_DATABASE_URL",
    "BACKEND_DATABASE_URL",
    "SCORING_DATABASE_URL",
    "EXECUTION_DATABASE_URL",
    "FEEDBACK_DATABASE_URL",
    "MARKET_DATA_DATABASE_URL",
    "INDICATOR_DATABASE_URL",
)


def inside_container() -> bool:
    """True when this process is already on the Compose network."""
    return Path("/.dockerenv").exists()


def load_env_file(path: Path) -> dict[str, str]:
    """Read one ``.env``-style file; a missing file is an empty mapping."""
    from lib_common.env_utils import load_dotenv_file  # noqa: PLC0415

    return load_dotenv_file(str(path))


def declared_keys(path: Path) -> list[str]:
    """Keys declared by a ``.env``-style file, in file order and without duplicates."""
    from lib_common.env_utils import parse_dotenv_line  # noqa: PLC0415

    keys: list[str] = []
    seen: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return keys
    for line in text.splitlines():
        parsed = parse_dotenv_line(line)
        if parsed is None or parsed[0] in seen:
            continue
        seen.add(parsed[0])
        keys.append(parsed[0])
    return keys


def key_diff(example: Sequence[str], configured: Iterable[str]) -> dict[str, list[str]]:
    """Compare a configuration against the template it was generated from.

    ``missing`` keys appeared in ``.env.example`` after the file was written and
    are scenario B's silent failure. ``unexpected`` keys were retired from the
    template or added by hand; they are reported, never removed.
    """
    present = set(configured)
    return {
        "missing": [key for key in example if key not in present],
        "unexpected": sorted(present - set(example)),
    }


def db_port(env: Mapping[str, str]) -> int:
    """The published loopback port, refusing a value that is not a port."""
    raw = (env.get("DB_PORT") or "").strip() or str(DEFAULT_DB_PORT)
    try:
        port = int(raw)
    except ValueError as exc:
        msg = "DB_PORT must be a port number"
        raise ValueError(msg) from exc
    if not 1 <= port <= _MAX_PORT:
        msg = "DB_PORT must be a port number"
        raise ValueError(msg)
    return port


def resolve_host_url(url: str, env: Mapping[str, str]) -> str:
    """Rewrite a container-addressed database URL to the published listener.

    Any other host is left exactly as written: an operator who points a URL at
    a different server means it.
    """
    try:
        parsed = make_url(url)
    except SQLAlchemyError as exc:
        msg = "Database URL is invalid"
        raise ValueError(msg) from exc
    if parsed.host != CONTAINER_DATABASE_HOST:
        return url
    return parsed.set(host=LOOPBACK_HOST, port=db_port(env)).render_as_string(hide_password=False)


def resolve_host_env(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` whose database URLs are reachable from this machine.

    A value this cannot parse is passed through untouched: validating it, and
    reporting it without echoing the credential it carries, belongs to the
    command that is about to use it.
    """
    resolved = dict(env)
    if inside_container():
        return resolved
    for key in DATABASE_URL_KEYS:
        value = (resolved.get(key) or "").strip()
        if not value:
            continue
        try:
            resolved[key] = resolve_host_url(value, resolved)
        except ValueError:
            continue
    return resolved


def shell_overrides(env_file: Mapping[str, str]) -> list[str]:
    """Keys the process environment overrides with a different value.

    A leftover ``export MIGRATION_DATABASE_URL=...`` silently wins over ``.env``
    for every command in this repository. ``doctor`` reports it rather than
    letting it decide which database is migrated.
    """
    return sorted(
        key
        for key, value in env_file.items()
        if key in os.environ and os.environ[key] != value and key in DATABASE_URL_KEYS
    )


def render_values(text: str, values: Mapping[str, str]) -> str:
    """Rewrite assignments in a ``.env``-style document, preserving its comments.

    Keys absent from the document are appended in sorted order under a heading,
    which is how ``init --update`` adds configuration that appeared upstream.
    """
    from lib_common.env_utils import parse_dotenv_line  # noqa: PLC0415

    remaining = dict(values)
    lines = text.splitlines()
    rendered: list[str] = []
    for line in lines:
        parsed = parse_dotenv_line(line)
        if parsed is None or parsed[0] not in remaining:
            rendered.append(line)
            continue
        key = parsed[0]
        rendered.append(f"{key}={remaining.pop(key)}")
    if remaining:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Added by vmdev init --update; review before deploying.")
        rendered.extend(f"{key}={remaining[key]}" for key in sorted(remaining))
    return "\n".join(rendered) + "\n"


__all__ = [
    "CONTAINER_DATABASE_HOST",
    "DATABASE_URL_KEYS",
    "DEFAULT_DB_PORT",
    "LOOPBACK_HOST",
    "db_port",
    "declared_keys",
    "inside_container",
    "key_diff",
    "load_env_file",
    "render_values",
    "resolve_host_env",
    "resolve_host_url",
    "shell_overrides",
]
