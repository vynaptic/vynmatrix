"""Change autopilot default from false to true and backfill existing rows.

ONE-TIME SAFETY NOTE (Mar 2026):
    Before this migration, autopilot was a dead field — stored in the DB but
    never read by any code path.  Every autopilot=false row is a side effect
    of the server_default="false" in migration 0005, NOT a deliberate user
    choice.  The bulk UPDATE below is therefore safe for all existing data.

    After this migration ships and the autopilot gate in evaluate_bindings()
    is live, users CAN intentionally set autopilot=false through the backend
    binding API.
    Do NOT write another blanket UPDATE that flips false→true in the future
    without first auditing which rows are intentionally manual.

Legacy bindings had autopilot=false by default (from migration 0005), but the new
autopilot gate in evaluate_bindings() treats false as "skip execution".  To
preserve backward-compatible behavior (existing bindings auto-execute), we:

1. Backfill all existing rows where autopilot=false to autopilot=true.
2. Change the server_default from 'false' to 'true' so new rows created
   directly via SQL also default to auto-execute.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_autopilot_default_true"
down_revision = "0015_exec_log_canon_sig"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Backfill: flip all existing false rows to true
    op.execute("UPDATE user_strategy_bindings SET autopilot = true WHERE autopilot = false")

    # 2. Change server_default for future rows
    op.alter_column(
        "user_strategy_bindings",
        "autopilot",
        existing_type=sa.Boolean(),
        server_default="true",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "user_strategy_bindings",
        "autopilot",
        existing_type=sa.Boolean(),
        server_default="false",
        existing_nullable=False,
    )
