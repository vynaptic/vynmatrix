"""Generate the private configuration a fresh installation needs.

Nothing in the repository generated a secret before this module: ``.env.example``
carried twenty-three placeholders holding fifteen distinct secrets, and every
new installation invented them by hand. ``vmdev init`` generates them once,
derives the connection strings that embed them, and writes the two untracked
files the lifecycle reads.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

ENV_EXAMPLE = ".env.example"
ENV_FILE = ".env"
OWNER_FILE = "owner.local.yaml"

DEFAULT_DB_USER = "trader"
DEFAULT_DB_NAME = "vm_trading"
DEFAULT_PORT = 5432
CONTAINER_HOST = "postgres"
_TOKEN_BYTES = 32

#: The maintenance password plus the six runtime role passwords.
PASSWORD_KEYS: tuple[str, ...] = (
    "DB_PASSWORD",
    "VM_BACKEND_DB_PASSWORD",
    "VM_SCORING_DB_PASSWORD",
    "VM_EXECUTION_DB_PASSWORD",
    "VM_FEEDBACK_DB_PASSWORD",
    "VM_MARKET_DATA_DB_PASSWORD",
    "VM_INDICATOR_DB_PASSWORD",
)
#: Service and admin keys. Every one of them fails closed when left empty.
API_KEY_KEYS: tuple[str, ...] = (
    "BACKEND_ADMIN_API_KEY",
    "SCORING_API_KEY",
    "EXECUTION_API_KEY",
    "SCORING_ADMIN_API_KEY",
    "EXECUTION_ADMIN_API_KEY",
    "FEEDBACK_API_KEY",
    "MARKET_DATA_API_KEY",
)
SECRET_RING_KEY = "SECRETS_MASTER_KEYS"
SECRET_KEYS: tuple[str, ...] = (*PASSWORD_KEYS, *API_KEY_KEYS, SECRET_RING_KEY)

#: Each runtime connection string and the login/password pair it carries.
ROLE_URLS: dict[str, tuple[str, str]] = {
    "BACKEND_DATABASE_URL": ("vm_backend_login", "VM_BACKEND_DB_PASSWORD"),
    "SCORING_DATABASE_URL": ("vm_scoring_login", "VM_SCORING_DB_PASSWORD"),
    "EXECUTION_DATABASE_URL": ("vm_execution_login", "VM_EXECUTION_DB_PASSWORD"),
    "FEEDBACK_DATABASE_URL": ("vm_feedback_login", "VM_FEEDBACK_DB_PASSWORD"),
    "MARKET_DATA_DATABASE_URL": ("vm_market_data_login", "VM_MARKET_DATA_DB_PASSWORD"),
    "INDICATOR_DATABASE_URL": ("vm_indicator_login", "VM_INDICATOR_DB_PASSWORD"),
}

PAPER_EQUITY_KEY = "VM_PAPER_ACCOUNT_INITIAL_EQUITY"
PAPER_ACCOUNT_CONFIG_KEY = "local-paper"
PAPER_BROKER_CODE = "paper"


@dataclass(frozen=True)
class OwnerInputs:
    """What a machine cannot know about an installation."""

    email: str
    base_ccy: str
    tz: str
    paper_equity: Decimal
    full_name: str | None = None

    def profile(self) -> dict[str, str]:
        profile = {"email": self.email, "base_ccy": self.base_ccy, "tz": self.tz}
        if self.full_name:
            profile["full_name"] = self.full_name
        return profile


def parse_equity(value: str) -> Decimal:
    """Accept a plain positive decimal; paper capital is never inferred."""
    try:
        equity = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        msg = "Starting paper equity must be a decimal number"
        raise ValueError(msg) from exc
    if not equity.is_finite() or equity <= 0:
        msg = "Starting paper equity must be greater than zero"
        raise ValueError(msg)
    return equity


def generate_secrets() -> dict[str, str]:
    """Fifteen distinct secrets: fourteen URL-safe tokens and one Fernet key."""
    from cryptography.fernet import Fernet  # noqa: PLC0415

    values = {key: secrets.token_urlsafe(_TOKEN_BYTES) for key in (*PASSWORD_KEYS, *API_KEY_KEYS)}
    values[SECRET_RING_KEY] = Fernet.generate_key().decode("ascii")
    if len(set(values.values())) != len(SECRET_KEYS):
        msg = "Generated secrets must be distinct"
        raise RuntimeError(msg)
    return values


def database_url(login: str, password: str, database: str, *, host: str, port: int) -> str:
    """Compose a connection string, percent-encoding the identity it carries."""
    return (
        f"postgresql://{quote(login, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def derive_urls(values: Mapping[str, str], *, host: str, port: int) -> dict[str, str]:
    """Every connection string ``.env`` declares, derived from the generated secrets."""
    user = values.get("DB_USER") or DEFAULT_DB_USER
    name = values.get("DB_NAME") or DEFAULT_DB_NAME
    maintenance = values["DB_PASSWORD"]
    urls = {
        "ADMIN_DATABASE_URL": database_url(user, maintenance, "postgres", host=host, port=port),
        "MIGRATION_DATABASE_URL": database_url(user, maintenance, name, host=host, port=port),
    }
    for key, (login, password_key) in ROLE_URLS.items():
        urls[key] = database_url(login, values[password_key], name, host=host, port=port)
    return urls


def build_values(owner: OwnerInputs, *, host: str, port: int) -> dict[str, str]:
    """The complete set of values ``init`` writes over the template's placeholders."""
    values = generate_secrets()
    values["DB_USER"] = DEFAULT_DB_USER
    values["DB_NAME"] = DEFAULT_DB_NAME
    values["DB_PORT"] = str(port)
    values.update(derive_urls(values, host=host, port=port))
    values[PAPER_EQUITY_KEY] = format(owner.paper_equity, "f")
    return values


def _derive_one(key: str, values: Mapping[str, str], *, host: str, port: int) -> str | None:
    """Rebuild a single connection string, or ``None`` when its inputs are absent."""
    database = values.get("DB_NAME") or DEFAULT_DB_NAME
    if key in ROLE_URLS:
        login, password_key = ROLE_URLS[key]
        password = values.get(password_key)
        return (
            None if not password else database_url(login, password, database, host=host, port=port)
        )
    maintenance = values.get("DB_PASSWORD")
    if not maintenance:
        return None
    user = values.get("DB_USER") or DEFAULT_DB_USER
    target = "postgres" if key == "ADMIN_DATABASE_URL" else database
    return database_url(user, maintenance, target, host=host, port=port)


def fill_missing(
    existing: Mapping[str, str], template: Mapping[str, str], missing: list[str]
) -> dict[str, str]:
    """Values for keys that appeared in the template after ``.env`` was written.

    A new secret is generated; a new connection string is derived from the
    secret already installed so an upgrade never invalidates a provisioned
    role; anything else keeps the template's own default.
    """
    if not missing:
        return {}
    merged = dict(existing)
    generated = generate_secrets()
    values: dict[str, str] = {}
    for key in missing:
        if key in SECRET_KEYS:
            values[key] = generated[key]
            merged[key] = generated[key]
    try:
        port = int(merged.get("DB_PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    derivable = {*ROLE_URLS, "ADMIN_DATABASE_URL", "MIGRATION_DATABASE_URL"}
    for key in missing:
        if key in values:
            continue
        derived = (
            _derive_one(key, merged, host=CONTAINER_HOST, port=port) if key in derivable else None
        )
        values[key] = derived if derived is not None else template.get(key, "")
    return values


def render_owner_file(owner: OwnerInputs) -> str:
    """The owner designation bootstrap reads: a profile and nothing else."""
    lines = [
        "# Private owner designation for this installation. Keep it untracked.",
        "# vmdev deploy reads it once, to create or verify the deployment owner.",
        "profile:",
        f'  email: "{owner.email}"',
        f'  base_ccy: "{owner.base_ccy}"',
        f'  tz: "{owner.tz}"',
    ]
    if owner.full_name:
        lines.append(f'  full_name: "{owner.full_name}"')
    return "\n".join(lines) + "\n"


__all__ = [
    "API_KEY_KEYS",
    "CONTAINER_HOST",
    "DEFAULT_DB_NAME",
    "DEFAULT_DB_USER",
    "DEFAULT_PORT",
    "ENV_EXAMPLE",
    "ENV_FILE",
    "OWNER_FILE",
    "PAPER_ACCOUNT_CONFIG_KEY",
    "PAPER_BROKER_CODE",
    "PAPER_EQUITY_KEY",
    "PASSWORD_KEYS",
    "ROLE_URLS",
    "SECRET_KEYS",
    "SECRET_RING_KEY",
    "OwnerInputs",
    "build_values",
    "database_url",
    "derive_urls",
    "fill_missing",
    "generate_secrets",
    "parse_equity",
    "render_owner_file",
]
