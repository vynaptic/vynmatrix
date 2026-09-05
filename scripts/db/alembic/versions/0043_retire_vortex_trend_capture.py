"""Retire VortexTrendCapture without deleting historical lineage.

Revision ID: 0043_retire_vortex_trend_capture
Revises: 0042_backtest_trial_provenance
"""

from __future__ import annotations

from alembic import op

revision = "0043_retire_vortex_trend_capture"
down_revision = "0042_backtest_trial_provenance"
branch_labels = None
depends_on = None


_RETIRED_STRATEGY_IDS = (
    "adaptive_kalman_filter_v1",
    "bbkc_squeeze_v1",
    "chaos_v1",
    "confirmed_mss_v1",
    "donchian_atr_channel_v1",
    "dual_regime_weekly_v1",
    "ma_ribbon_pullback_v1",
    "ou_mean_reversion_v1",
    "psar_volume_adaptive_af_v1",
    "vol_mom_wma_profit_target_v1",
    "vortex_trend_capture_v1",
)
_RETIRED_VALUES_SQL = ",\n            ".join(
    f"('{strategy_id}')" for strategy_id in _RETIRED_STRATEGY_IDS
)
_FEEDBACK_RETIREMENT_NOTE = (
    "[strategy-retirement] Expired because the strategy is retired; historical feedback preserved."
)


def _converge_retired_state() -> None:
    """Fail closed around in-flight work, then disable all execution surfaces."""

    op.execute(
        f"""
        SET LOCAL lock_timeout = '5s';
        LOCK TABLE
            strategies,
            strategy_versions,
            user_strategy_bindings,
            user_strategy_configs,
            execution_decision_logs,
            outbox_events,
            pending_orders,
            execution_logs,
            linked_broker_accounts,
            positions,
            strategy_parameter_feedback
        IN SHARE ROW EXCLUSIVE MODE;

        CREATE TEMP TABLE vm_migration_retired_strategy_ids (
            strategy_id TEXT PRIMARY KEY
        ) ON COMMIT DROP;

        INSERT INTO vm_migration_retired_strategy_ids (strategy_id)
        VALUES
            {_RETIRED_VALUES_SQL};

        DO $$
        DECLARE
            unresolved_count INTEGER;
            nonzero_position_count INTEGER;
        BEGIN
            SELECT
                (SELECT COUNT(*)
                 FROM execution_logs
                 JOIN vm_migration_retired_strategy_ids retired
                   ON retired.strategy_id = execution_logs.strategy_id
                 WHERE status IN (
                       'pending', 'executing', 'submitted', 'partially_filled',
                       'accepted', 'working'
                   ))
                + (SELECT COUNT(*)
                   FROM pending_orders
                   JOIN vm_migration_retired_strategy_ids retired
                     ON retired.strategy_id = pending_orders.strategy_id
                   WHERE status IN ('pending', 'submitted', 'partially_filled'))
                + (SELECT COUNT(*)
                   FROM execution_decision_logs decisions
                   JOIN user_strategy_bindings bindings
                     ON bindings.binding_id = decisions.binding_id
                   JOIN vm_migration_retired_strategy_ids retired
                     ON retired.strategy_id = bindings.strategy_id
                   WHERE decisions.status IN ('pending', 'executing'))
                + (SELECT COUNT(*)
                   FROM outbox_events
                   JOIN vm_migration_retired_strategy_ids retired
                     ON retired.strategy_id = (
                         outbox_events.payload::JSONB ->> 'strategy_id'
                     )
                     OR retired.strategy_id = (
                         outbox_events.payload::JSONB #>> '{{signal,strategy_id}}'
                     )
                   WHERE status IN (
                         'pending', 'in_progress', 'failed', 'dead_letter'
                     ))
            INTO unresolved_count;

            IF unresolved_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot converge retired strategies: % non-terminal work items remain',
                    unresolved_count;
            END IF;

            -- Position rows do not carry strategy attribution. Any holding
            -- therefore blocks this irreversible runtime retirement.
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
        FROM vm_migration_retired_strategy_ids retired
        WHERE binding.strategy_id = retired.strategy_id
          AND (binding.is_active OR binding.autopilot);

        UPDATE user_strategy_configs config
        SET is_active = FALSE, updated_at = NOW()
        FROM vm_migration_retired_strategy_ids retired
        WHERE config.strategy_id = retired.strategy_id
          AND config.is_active;

        UPDATE strategies strategy
        SET is_active = FALSE, updated_at = NOW()
        FROM vm_migration_retired_strategy_ids retired
        WHERE strategy.strategy_id = retired.strategy_id
          AND strategy.is_active;

        UPDATE strategy_versions version
        SET status = 'deprecated'
        FROM vm_migration_retired_strategy_ids retired
        WHERE version.strategy_id = retired.strategy_id
          AND version.status <> 'deprecated';

        -- Version/catalogue rows are locked and retired before feedback is
        -- converged. A concurrent suggestion creator therefore either sees the
        -- retirement or commits first and is included by this update.
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
        FROM vm_migration_retired_strategy_ids retired
        WHERE feedback.strategy_id = retired.strategy_id
          AND feedback.status IN ('pending', 'approved');

        DO $$
        DECLARE
            actionable_feedback_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO actionable_feedback_count
            FROM strategy_parameter_feedback feedback
            JOIN vm_migration_retired_strategy_ids retired
              ON retired.strategy_id = feedback.strategy_id
            WHERE feedback.status IN ('pending', 'approved');

            IF actionable_feedback_count <> 0 THEN
                RAISE EXCEPTION
                    'Strategy retirement left % actionable feedback records',
                    actionable_feedback_count;
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    _converge_retired_state()


def downgrade() -> None:
    # The executable strategy is intentionally removed from released artifacts.
    # Re-enabling catalogue state during a schema downgrade would create dangling
    # bindings to code that cannot run, so downgrade preserves the safe retired
    # state and never fabricates the pre-migration activation flags.
    _converge_retired_state()
