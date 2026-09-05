"""Complete only the untouched migrated Saxo reference with explicit false flags.

Revision ID: 0104_saxo_capability_flags
Revises: 0103_remove_commercial_tenancy

Historical 0066 registered four keys; 0070 inserted ETF after equity. Neither
explicit flag grants execution capability. Customized documents remain conflicts
for catalogue reconciliation, rather than being silently repaired here.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0104_saxo_capability_flags"
down_revision = "0103_remove_commercial_tenancy"
branch_labels = None
depends_on = None

_HISTORICAL = {
    "asset_classes": ["equity", "etf", "futures", "options", "fx", "commodities"],
    "order_types": ["market", "limit", "stop", "stop_limit"],
    "features": ["spot", "margin", "futures", "options"],
    "regions": ["global"],
}
_CURRENT = {
    "asset_classes": ["equity", "etf", "futures", "options", "fx", "commodities"],
    "order_types": ["market", "limit", "stop", "stop_limit"],
    "features": ["spot", "margin", "futures", "options"],
    "regions": ["global"],
    "exact_fill_retrieval": False,
    "live_certification_implemented": False,
}


def _replace_exact(expected: dict[str, Any], desired: dict[str, Any]) -> None:
    bind = op.get_bind()
    brokers = sa.table(
        "brokers",
        sa.column("broker_id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("capabilities", sa.JSON()),
        schema="public" if bind.dialect.name == "postgresql" else None,
    )
    row = bind.execute(
        sa.select(brokers.c.broker_id, brokers.c.capabilities)
        .where(brokers.c.code == "saxo")
        .with_for_update()
    ).one_or_none()
    # Canonical JSON comparison preserves boolean/number distinctions (False != 0).
    if row is not None and json.dumps(row.capabilities, sort_keys=True) == json.dumps(
        expected, sort_keys=True
    ):
        bind.execute(
            brokers.update()
            .where(brokers.c.broker_id == row.broker_id)
            .values(capabilities=desired)
        )


def upgrade() -> None:
    _replace_exact(_HISTORICAL, _CURRENT)


def downgrade() -> None:
    _replace_exact(_CURRENT, _HISTORICAL)
