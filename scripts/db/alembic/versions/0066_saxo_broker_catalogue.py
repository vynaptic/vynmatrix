"""Register Saxo OpenAPI in the canonical broker catalogue.

The execution adapter cannot be selected by a tenant until the canonical
``brokers`` and ``broker_environments`` rows exist. This revision owns those
rows for both SIM and LIVE without creating any user account, credential, or
instrument mapping.

Revision ID: 0066_saxo_broker_catalogue
Revises: 0065_retire_strategy_metadata
Create Date: 2026-07-25
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0066_saxo_broker_catalogue"
down_revision = "0065_retire_strategy_metadata"
branch_labels = None
depends_on = None

_CAPABILITIES: dict[str, Any] = {
    "asset_classes": ["equity", "futures", "options", "fx", "commodities"],
    "order_types": ["market", "limit", "stop", "stop_limit"],
    "features": ["spot", "margin", "futures", "options"],
    "regions": ["global"],
}
_ENVIRONMENTS: tuple[dict[str, Any], ...] = (
    {
        "environment": "paper",
        "region": "global",
        "base_urls": {
            "rest": "https://gateway.saxobank.com/sim/openapi",
            "ws": "wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws",
        },
        "rate_limits": {
            "requests_per_minute": 120,
            "orders_per_second": 1,
        },
    },
    {
        "environment": "live",
        "region": "global",
        "base_urls": {
            "rest": "https://gateway.saxobank.com/openapi",
            "ws": "wss://live-streaming.saxobank.com/oapi/streaming/ws",
        },
        "rate_limits": {
            "requests_per_minute": 120,
            "orders_per_second": 1,
        },
    },
)

_BROKERS = sa.table(
    "brokers",
    sa.column("broker_id", sa.Integer()),
    sa.column("code", sa.String()),
    sa.column("name", sa.String()),
    sa.column("capabilities", sa.JSON()),
)
_BROKER_ENVIRONMENTS = sa.table(
    "broker_environments",
    sa.column("broker_env_id", sa.Integer()),
    sa.column("broker_id", sa.Integer()),
    sa.column("environment", sa.String()),
    sa.column("region", sa.String()),
    sa.column("base_urls", sa.JSON()),
    sa.column("rate_limits", sa.JSON()),
)
_LINKED_BROKER_ACCOUNTS = sa.table(
    "linked_broker_accounts",
    sa.column("account_id", sa.BigInteger()),
    sa.column("broker_id", sa.Integer()),
)


def _saxo_broker_id(bind: sa.Connection) -> int | None:
    value = bind.execute(
        sa.select(_BROKERS.c.broker_id).where(_BROKERS.c.code == "saxo")
    ).scalar_one_or_none()
    return None if value is None else int(value)


def upgrade() -> None:
    bind = op.get_bind()
    broker_id = _saxo_broker_id(bind)
    if broker_id is None:
        bind.execute(
            sa.insert(_BROKERS).values(
                code="saxo",
                name="Saxo Bank",
                capabilities=_CAPABILITIES,
            )
        )
        broker_id = _saxo_broker_id(bind)
        if broker_id is None:
            msg = "Saxo broker catalogue insert did not return a broker id"
            raise RuntimeError(msg)
    else:
        bind.execute(
            sa.update(_BROKERS)
            .where(_BROKERS.c.broker_id == broker_id)
            .values(name="Saxo Bank", capabilities=_CAPABILITIES)
        )

    for desired in _ENVIRONMENTS:
        rows = list(
            bind.execute(
                sa.select(_BROKER_ENVIRONMENTS.c.broker_env_id).where(
                    _BROKER_ENVIRONMENTS.c.broker_id == broker_id,
                    _BROKER_ENVIRONMENTS.c.environment == desired["environment"],
                    _BROKER_ENVIRONMENTS.c.region == desired["region"],
                )
            ).scalars()
        )
        if len(rows) > 1:
            msg = (
                "Saxo broker catalogue contains duplicate "
                f"{desired['environment']}/global environment rows"
            )
            raise RuntimeError(msg)
        values = {
            "broker_id": broker_id,
            "environment": desired["environment"],
            "region": desired["region"],
            "base_urls": desired["base_urls"],
            "rate_limits": desired["rate_limits"],
        }
        if rows:
            bind.execute(
                sa.update(_BROKER_ENVIRONMENTS)
                .where(_BROKER_ENVIRONMENTS.c.broker_env_id == int(rows[0]))
                .values(**values)
            )
        else:
            bind.execute(sa.insert(_BROKER_ENVIRONMENTS).values(**values))


def downgrade() -> None:
    bind = op.get_bind()
    broker_id = _saxo_broker_id(bind)
    if broker_id is None:
        return
    linked_accounts = int(
        bind.execute(
            sa.select(sa.func.count())
            .select_from(_LINKED_BROKER_ACCOUNTS)
            .where(_LINKED_BROKER_ACCOUNTS.c.broker_id == broker_id)
        ).scalar_one()
    )
    if linked_accounts:
        msg = (
            "Cannot remove the Saxo broker catalogue while "
            f"{linked_accounts} linked account(s) reference it"
        )
        raise RuntimeError(msg)
    bind.execute(
        sa.delete(_BROKER_ENVIRONMENTS).where(_BROKER_ENVIRONMENTS.c.broker_id == broker_id)
    )
    bind.execute(sa.delete(_BROKERS).where(_BROKERS.c.broker_id == broker_id))
