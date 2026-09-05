-- Final fail-closed convergence for the production seed profile.
-- Applied only by scripts/db/migrate_and_seed.sh after the canonical seed files.

BEGIN;

-- Serialize retirement against every table scanned or converged below. The
-- bounded timeout fails deployment instead of accepting an ambiguous snapshot.
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

CREATE TEMP TABLE vm_production_seed_context (
    image_tag TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO vm_production_seed_context (image_tag) VALUES (:'image_tag');

-- Keep the complete retired fleet in one transaction-local relation. This
-- guard may be invoked independently of 04_paper_users.sql, so it must converge
-- legacy production databases without relying on the catalogue seed having run.
CREATE TEMP TABLE vm_production_retired_strategy_ids (
    strategy_id TEXT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO vm_production_retired_strategy_ids (strategy_id)
VALUES
    ('adaptive_kalman_filter_v1'),
    ('bbkc_squeeze_v1'),
    ('chaos_v1'),
    ('confirmed_mss_v1'),
    ('donchian_atr_channel_v1'),
    ('dual_regime_weekly_v1'),
    ('enhanced_dual_momentum_v1'),
    ('liquidity_leaders_basket_v1'),
    ('ma_ribbon_pullback_v1'),
    ('ma_slope_v1'),
    ('ou_mean_reversion_v1'),
    ('psar_volume_adaptive_af_v1'),
    ('quantile_channel_v1'),
    ('stat_validated_reg_channel_breakout_v1'),
    ('vol_mom_wma_profit_target_v1'),
    ('volatility_reversal_v1'),
    ('vortex_rsi_profit_target_v1'),
    ('vortex_trend_capture_v1');

-- Defence in depth for callers that apply the production guard independently
-- of 04_paper_users.sql. Positions currently have no strategy attribution, so
-- every holding must be reconciled before catalogue or binding state is changed.
DO $$
DECLARE
    unresolved_count INTEGER;
    nonzero_live_position_count INTEGER;
    nonzero_nonlive_position_count INTEGER;
BEGIN
    SELECT
        (SELECT COUNT(*)
         FROM execution_logs logs
         JOIN vm_production_retired_strategy_ids retired
           ON retired.strategy_id = logs.strategy_id
         WHERE logs.status IN (
             'pending', 'executing', 'submitted', 'partially_filled',
             'accepted', 'working'
         ))
        + (SELECT COUNT(*)
           FROM pending_orders orders
           JOIN vm_production_retired_strategy_ids retired
             ON retired.strategy_id = orders.strategy_id
           WHERE orders.status IN (
               'pending', 'submission_unknown', 'submitted', 'working',
               'partially_filled'
           ))
        + (SELECT COUNT(*)
           FROM execution_decision_logs decisions
           LEFT JOIN user_strategy_bindings bindings
             ON bindings.binding_id = decisions.binding_id
           LEFT JOIN decision_contexts context
             ON context.signal_id = decisions.signal_id
           LEFT JOIN vm_production_retired_strategy_ids retired_binding
             ON retired_binding.strategy_id = bindings.strategy_id
           LEFT JOIN vm_production_retired_strategy_ids retired_signal
             ON retired_signal.strategy_id = context.strategy_id
           WHERE decisions.status IN ('pending', 'executing')
             AND (
                 retired_binding.strategy_id IS NOT NULL
                 OR retired_signal.strategy_id IS NOT NULL
                 OR (
                     bindings.strategy_id IS NULL
                     AND context.strategy_id IS NULL
                 )
             ))
        + (SELECT COUNT(*)
           FROM outbox_events events
           JOIN vm_production_retired_strategy_ids retired
             ON retired.strategy_id = (events.payload::JSONB ->> 'strategy_id')
             OR retired.strategy_id = (
                 events.payload::JSONB #>> '{signal,strategy_id}'
             )
           WHERE events.status IN (
               'pending', 'in_progress', 'failed', 'dead_letter'
           ))
    INTO unresolved_count;

    IF unresolved_count > 0 THEN
        RAISE EXCEPTION
            'Cannot converge retired strategy code: % non-terminal work items remain',
            unresolved_count;
    END IF;

    SELECT COUNT(*) INTO nonzero_live_position_count
    FROM positions
    JOIN linked_broker_accounts USING (account_id)
    WHERE positions.qty <> 0
      AND linked_broker_accounts.environment = 'live';
    IF nonzero_live_position_count > 0 THEN
        RAISE EXCEPTION
            '% non-zero live-account positions require manual reconciliation before production seed convergence',
            nonzero_live_position_count;
    END IF;

    -- Non-live positions are attributed per strategy by replaying the
    -- immutable executions -> orders -> order_intents fill lineage (same
    -- rebuildable projection as the infra go-live gate). Retired-attributed
    -- or lineage-unreconciled positions block; an ACTIVE strategy may hold
    -- non-live positions across production seed convergence.
    CREATE TEMP TABLE vm_production_strategy_net ON COMMIT DROP AS
    SELECT oi.account_id,
           e.instr_id,
           oi.strategy_id,
           SUM(CASE WHEN oi.side = 'BUY' THEN e.qty ELSE -e.qty END) AS net_qty
    FROM executions e
    JOIN orders o ON o.order_id = e.order_id
    JOIN order_intents oi
      ON oi.intent_id = o.intent_id AND oi.account_id = o.account_id
    GROUP BY oi.account_id, e.instr_id, oi.strategy_id;

    SELECT COUNT(*) INTO nonzero_nonlive_position_count
    FROM vm_production_strategy_net net
    JOIN vm_production_retired_strategy_ids retired USING (strategy_id)
    JOIN linked_broker_accounts accounts USING (account_id)
    WHERE net.net_qty <> 0
      AND accounts.environment <> 'live';
    IF nonzero_nonlive_position_count > 0 THEN
        RAISE EXCEPTION
            '% open non-live positions are attributed to retired strategies and require manual reconciliation',
            nonzero_nonlive_position_count;
    END IF;

    SELECT COUNT(*) INTO nonzero_nonlive_position_count
    FROM positions
    JOIN linked_broker_accounts USING (account_id)
    LEFT JOIN (
        SELECT account_id, instr_id, SUM(net_qty) AS attributed_qty
        FROM vm_production_strategy_net
        GROUP BY account_id, instr_id
    ) attribution USING (account_id, instr_id)
    WHERE positions.qty <> 0
      AND linked_broker_accounts.environment <> 'live'
      AND COALESCE(attribution.attributed_qty, 0) <> positions.qty;
    IF nonzero_nonlive_position_count > 0 THEN
        RAISE EXCEPTION
            '% non-zero non-live positions do not reconcile with execution fill-lineage attribution; manual reconciliation required',
            nonzero_nonlive_position_count;
    END IF;
END $$;

-- Preserve historical signals and executions while removing every route back
-- into scheduling or execution for retired implementations.
UPDATE user_strategy_bindings binding
SET is_active = FALSE,
    autopilot = FALSE,
    entries_enabled = FALSE,
    exits_enabled = FALSE,
    updated_at = NOW()
FROM vm_production_retired_strategy_ids retired
WHERE binding.strategy_id = retired.strategy_id
  AND (
      binding.is_active
      OR binding.autopilot
      OR binding.entries_enabled
      OR binding.exits_enabled
  );

UPDATE user_strategy_configs config
SET is_active = FALSE, updated_at = NOW()
FROM vm_production_retired_strategy_ids retired
WHERE config.strategy_id = retired.strategy_id
  AND config.is_active;

UPDATE strategies strategy
SET is_active = FALSE, updated_at = NOW()
FROM vm_production_retired_strategy_ids retired
WHERE strategy.strategy_id = retired.strategy_id
  AND strategy.is_active;

UPDATE strategy_versions version
SET status = 'deprecated'
FROM vm_production_retired_strategy_ids retired
WHERE version.strategy_id = retired.strategy_id
  AND version.status <> 'deprecated';

-- Retire version/catalogue rows before feedback so a concurrent suggestion
-- creator cannot commit an actionable row after this convergence has scanned.
UPDATE strategy_parameter_feedback feedback
SET status = 'expired',
    reviewed_at = COALESCE(feedback.reviewed_at, NOW()),
    review_notes = CASE
        WHEN POSITION(
            '[strategy-retirement] Expired because the strategy is retired; historical feedback preserved.'
            IN COALESCE(feedback.review_notes, '')
        ) > 0 THEN feedback.review_notes
        WHEN NULLIF(BTRIM(COALESCE(feedback.review_notes, '')), '') IS NULL
            THEN '[strategy-retirement] Expired because the strategy is retired; historical feedback preserved.'
        ELSE feedback.review_notes || E'\n'
            || '[strategy-retirement] Expired because the strategy is retired; historical feedback preserved.'
    END
FROM vm_production_retired_strategy_ids retired
WHERE feedback.strategy_id = retired.strategy_id
  AND feedback.status IN ('pending', 'approved');

-- Production bootstrap grants no strategy execution authority. The exact
-- Swing route can be activated only by a later reviewed deployment containing
-- its validated promotion manifest; EMA/RSI follow in separate promotions.
UPDATE user_strategy_bindings
SET is_active = FALSE,
    autopilot = FALSE,
    entries_enabled = FALSE,
    exits_enabled = FALSE,
    updated_at = NOW()
WHERE is_active OR autopilot;

UPDATE strategies
SET is_active = FALSE, updated_at = NOW()
WHERE strategy_id IN (
    'swing_high_low_pmo_v1'
)
  AND is_active;

UPDATE strategy_versions
SET status = 'deprecated'
WHERE strategy_id IN (
    'swing_high_low_pmo_v1'
)
  AND status <> 'deprecated';

DO $$
DECLARE
    demo_count INTEGER;
    active_native_count INTEGER;
    image_mismatch_count INTEGER;
    retired_strategy_count INTEGER;
    active_retired_count INTEGER;
    actionable_retired_feedback_count INTEGER;
    configured_image_tag TEXT;
BEGIN
    SELECT COUNT(*) INTO retired_strategy_count
    FROM vm_production_retired_strategy_ids;
    IF retired_strategy_count <> 18 THEN
        RAISE EXCEPTION 'Expected 18 retired strategy IDs, found %',
            retired_strategy_count;
    END IF;

    SELECT COUNT(*) INTO actionable_retired_feedback_count
    FROM strategy_parameter_feedback feedback
    JOIN vm_production_retired_strategy_ids retired
      ON retired.strategy_id = feedback.strategy_id
    WHERE feedback.status IN ('pending', 'approved');
    IF actionable_retired_feedback_count <> 0 THEN
        RAISE EXCEPTION
            'Production convergence left % actionable retired-strategy feedback row(s)',
            actionable_retired_feedback_count;
    END IF;

    SELECT image_tag INTO configured_image_tag FROM vm_production_seed_context;
    SELECT COUNT(*) INTO demo_count
    FROM users
    WHERE user_id IN ('1', '2', '3', '4');
    IF demo_count <> 0 THEN
        RAISE EXCEPTION
            'Production bootstrap refuses % legacy numeric demo tenant(s)', demo_count;
    END IF;

    SELECT
        (SELECT COUNT(*)
         FROM user_strategy_bindings binding
         JOIN vm_production_retired_strategy_ids retired
           ON retired.strategy_id = binding.strategy_id
         WHERE binding.is_active
            OR binding.autopilot
            OR binding.entries_enabled
            OR binding.exits_enabled)
        + (SELECT COUNT(*)
           FROM user_strategy_configs config
           JOIN vm_production_retired_strategy_ids retired
             ON retired.strategy_id = config.strategy_id
           WHERE config.is_active)
        + (SELECT COUNT(*)
           FROM strategy_versions version
           JOIN vm_production_retired_strategy_ids retired
             ON retired.strategy_id = version.strategy_id
           WHERE version.status = 'active')
        + (SELECT COUNT(*)
           FROM strategies strategy
           JOIN vm_production_retired_strategy_ids retired
             ON retired.strategy_id = strategy.strategy_id
           WHERE strategy.is_active)
    INTO active_retired_count;
    IF active_retired_count <> 0 THEN
        RAISE EXCEPTION
            'Production bootstrap left % active retired-strategy records',
            active_retired_count;
    END IF;

    SELECT
        (SELECT COUNT(*)
         FROM user_strategy_bindings
         WHERE is_active OR autopilot OR entries_enabled OR exits_enabled)
        + (SELECT COUNT(*)
           FROM strategies
           WHERE strategy_id IN (
               'swing_high_low_pmo_v1'
           )
             AND is_active)
        + (SELECT COUNT(*)
           FROM strategy_versions
           WHERE strategy_id IN (
               'swing_high_low_pmo_v1'
           )
             AND status = 'active')
    INTO active_native_count;
    IF active_native_count <> 0 THEN
        RAISE EXCEPTION
            'Production bootstrap left % native execution-authority record(s) active',
            active_native_count;
    END IF;

    SELECT COUNT(*) INTO image_mismatch_count
    FROM strategy_versions
    WHERE status = 'active'
      AND strategy_id IN (
          'swing_high_low_pmo_v1'
      )
      AND docker_image <> 'vynmatrix/indicator-runner:' || configured_image_tag;
    IF image_mismatch_count <> 0 THEN
        RAISE EXCEPTION
            '% active strategy version(s) do not match deployment image tag %; bump strategy semver instead of rewriting lineage',
            image_mismatch_count, configured_image_tag;
    END IF;
END $$;

COMMIT;
