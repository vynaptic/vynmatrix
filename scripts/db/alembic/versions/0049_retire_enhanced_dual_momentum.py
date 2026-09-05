"""Retire EnhancedDualMomentum without deleting historical lineage.

Revision ID: 0049_retire_enhanced_dual_v1
Revises: 0048_retire_quantile_v1_2
"""

from __future__ import annotations

from alembic import op

revision = "0049_retire_enhanced_dual_v1"
down_revision = "0048_retire_quantile_v1_2"
branch_labels = None
depends_on = None


_RETIRED_STRATEGY_ID = "enhanced_dual_momentum_v1"
_FEEDBACK_RETIREMENT_NOTE = (
    "[strategy-retirement] Expired because the strategy is retired; historical feedback preserved."
)


def _converge_retired_state() -> None:
    """Fail closed around in-flight work, then disable strategy execution."""

    op.execute(
        f"""
        SET LOCAL lock_timeout = '5s';
        LOCK TABLE
            strategies,
            strategy_versions,
            user_strategy_bindings,
            user_strategy_configs,
            decision_contexts,
            execution_decision_logs,
            outbox_events,
            pending_orders,
            execution_logs,
            linked_broker_accounts,
            positions,
            strategy_parameter_feedback
        IN SHARE ROW EXCLUSIVE MODE;

        DO $$
        DECLARE
            unresolved_count INTEGER;
            nonzero_position_count INTEGER;
        BEGIN
            SELECT
                (SELECT COUNT(*)
                 FROM execution_logs
                 WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                   AND status IN (
                       'pending', 'executing', 'submitted', 'partially_filled',
                       'accepted', 'working'
                   ))
                + (SELECT COUNT(*)
                   FROM pending_orders
                   WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                     AND status IN ('pending', 'submitted', 'partially_filled'))
                + (SELECT COUNT(*)
                   FROM execution_decision_logs decisions
                   LEFT JOIN user_strategy_bindings bindings
                     ON bindings.binding_id = decisions.binding_id
                   LEFT JOIN decision_contexts context
                     ON context.signal_id = decisions.signal_id
                   WHERE decisions.status IN ('pending', 'executing')
                     AND (
                         bindings.strategy_id = '{_RETIRED_STRATEGY_ID}'
                         OR context.strategy_id = '{_RETIRED_STRATEGY_ID}'
                         OR (
                             bindings.strategy_id IS NULL
                             AND context.strategy_id IS NULL
                         )
                     ))
                + (SELECT COUNT(*)
                   FROM outbox_events
                   WHERE (
                       payload::JSONB ->> 'strategy_id' = '{_RETIRED_STRATEGY_ID}'
                       OR payload::JSONB #>> '{{signal,strategy_id}}'
                           = '{_RETIRED_STRATEGY_ID}'
                   )
                     AND status IN (
                         'pending', 'in_progress', 'failed', 'dead_letter'
                     ))
            INTO unresolved_count;

            IF unresolved_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot retire {_RETIRED_STRATEGY_ID}: % non-terminal work items remain',
                    unresolved_count;
            END IF;

            -- Positions do not carry strategy attribution, so any
            -- holding blocks removal of this strategy runtime.
            SELECT COUNT(*) INTO nonzero_position_count
            FROM positions
            JOIN linked_broker_accounts USING (account_id)
            WHERE positions.qty <> 0;

            IF nonzero_position_count > 0 THEN
                RAISE EXCEPTION
                    '% positions require reconciliation before strategy retirement',
                    nonzero_position_count;
            END IF;
        END $$;

        UPDATE user_strategy_bindings binding
        SET is_active = FALSE, autopilot = FALSE, updated_at = NOW()
        WHERE binding.strategy_id = '{_RETIRED_STRATEGY_ID}'
          AND (binding.is_active OR binding.autopilot);

        UPDATE user_strategy_configs config
        SET is_active = FALSE, updated_at = NOW()
        WHERE config.strategy_id = '{_RETIRED_STRATEGY_ID}'
          AND config.is_active;

        UPDATE strategies strategy
        SET is_active = FALSE, updated_at = NOW()
        WHERE strategy.strategy_id = '{_RETIRED_STRATEGY_ID}'
          AND strategy.is_active;

        UPDATE strategy_versions version
        SET status = 'deprecated'
        WHERE version.strategy_id = '{_RETIRED_STRATEGY_ID}'
          AND version.status <> 'deprecated';

        UPDATE strategy_parameter_feedback feedback
        SET status = 'expired',
            reviewed_at = COALESCE(feedback.reviewed_at, NOW()),
            review_notes = CASE
                WHEN POSITION(
                    '{_FEEDBACK_RETIREMENT_NOTE}' IN COALESCE(feedback.review_notes, '')
                ) > 0 THEN feedback.review_notes
                WHEN NULLIF(BTRIM(COALESCE(feedback.review_notes, '')), '') IS NULL
                    THEN '{_FEEDBACK_RETIREMENT_NOTE}'
                ELSE feedback.review_notes || E'\\n' || '{_FEEDBACK_RETIREMENT_NOTE}'
            END
        WHERE feedback.strategy_id = '{_RETIRED_STRATEGY_ID}'
          AND feedback.status IN ('pending', 'approved');

        DO $$
        DECLARE
            actionable_count INTEGER;
        BEGIN
            SELECT
                (SELECT COUNT(*)
                 FROM user_strategy_bindings
                 WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                   AND (is_active OR autopilot))
                + (SELECT COUNT(*)
                   FROM user_strategy_configs
                   WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                     AND is_active)
                + (SELECT COUNT(*)
                   FROM strategies
                   WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                     AND is_active)
                + (SELECT COUNT(*)
                   FROM strategy_versions
                   WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                     AND status = 'active')
                + (SELECT COUNT(*)
                   FROM strategy_parameter_feedback
                   WHERE strategy_id = '{_RETIRED_STRATEGY_ID}'
                     AND status IN ('pending', 'approved'))
            INTO actionable_count;

            IF actionable_count <> 0 THEN
                RAISE EXCEPTION
                    'EnhancedDualMomentum retirement left % actionable records',
                    actionable_count;
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    _converge_retired_state()


def downgrade() -> None:
    # Removed executable code cannot be made safe by a schema downgrade.
    _converge_retired_state()
