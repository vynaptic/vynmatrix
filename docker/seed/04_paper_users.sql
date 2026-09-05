-- Retired-strategy convergence and second paper tenant.
--
-- No imported catalogue strategy is staged for execution. Native EMA/RSI
-- bindings are registered by 05_e2e_scalpers.sql but remain inactive until the
-- preceding Swing canary is retired and their own promotion evidence passes.
-- Idempotent on repeated seed runs and self-contained after 02_seed_data.sql.

BEGIN;

-- Serialize retirement against every table scanned or converged below. Earlier
-- writers finish before the scan; later writers wait for this transaction.
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

-- One authoritative retirement set for every gate and convergence update in
-- this transaction. Repeating this list made it possible for the safety check,
-- bindings, configs, versions, and catalogue rows to drift independently.
CREATE TEMP TABLE vm_retired_strategy_ids (
    strategy_id TEXT PRIMARY KEY
) ON COMMIT DROP;

INSERT INTO vm_retired_strategy_ids (strategy_id)
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

-- Refuse to retire executable code while any event/order/decision for it can
-- still progress. Historical rows remain untouched; only non-terminal work is a
-- blocker. Positions cannot be attributed to a strategy in the current schema.
-- Every holding therefore blocks retirement until it is manually reconciled.
DO $$
DECLARE
    unresolved_count INTEGER;
    nonzero_live_position_count INTEGER;
    nonzero_nonlive_position_count INTEGER;
BEGIN
    SELECT
        (SELECT COUNT(*)
         FROM execution_logs logs
         JOIN vm_retired_strategy_ids retired ON retired.strategy_id = logs.strategy_id
         WHERE logs.status IN (
             'pending', 'executing', 'submitted', 'partially_filled',
             'accepted', 'working'
         ))
        + (SELECT COUNT(*)
           FROM pending_orders orders
           JOIN vm_retired_strategy_ids retired
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
           LEFT JOIN vm_retired_strategy_ids retired_binding
             ON retired_binding.strategy_id = bindings.strategy_id
           LEFT JOIN vm_retired_strategy_ids retired_signal
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
           JOIN vm_retired_strategy_ids retired
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
            'Cannot retire imported strategy code: % non-terminal work items remain',
            unresolved_count;
    END IF;

    SELECT COUNT(*) INTO nonzero_live_position_count
    FROM positions
    JOIN linked_broker_accounts USING (account_id)
    WHERE positions.qty <> 0
      AND linked_broker_accounts.environment = 'live';
    IF nonzero_live_position_count > 0 THEN
        RAISE EXCEPTION
            '% non-zero live-account positions require manual reconciliation before strategy retirement',
            nonzero_live_position_count;
    END IF;

    -- Non-live positions are attributed per strategy by replaying the
    -- immutable executions -> orders -> order_intents fill lineage (the same
    -- rebuildable projection the infra go-live gate uses). Only a position
    -- attributed to a RETIRED strategy, or one that does not reconcile with
    -- the lineage (unattributable drift), blocks retirement — an ACTIVE
    -- strategy may hold non-live positions across deploys.
    CREATE TEMP TABLE vm_seed_strategy_net ON COMMIT DROP AS
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
    FROM vm_seed_strategy_net net
    JOIN vm_retired_strategy_ids retired USING (strategy_id)
    JOIN linked_broker_accounts accounts USING (account_id)
    WHERE net.net_qty <> 0
      AND accounts.environment <> 'live';
    IF nonzero_nonlive_position_count > 0 THEN
        RAISE EXCEPTION
            '% open non-live positions are attributed to retired strategies and require reconciliation',
            nonzero_nonlive_position_count;
    END IF;

    SELECT COUNT(*) INTO nonzero_nonlive_position_count
    FROM positions
    JOIN linked_broker_accounts USING (account_id)
    LEFT JOIN (
        SELECT account_id, instr_id, SUM(net_qty) AS attributed_qty
        FROM vm_seed_strategy_net
        GROUP BY account_id, instr_id
    ) attribution USING (account_id, instr_id)
    WHERE positions.qty <> 0
      AND linked_broker_accounts.environment <> 'live'
      AND COALESCE(attribution.attributed_qty, 0) <> positions.qty;
    IF nonzero_nonlive_position_count > 0 THEN
        RAISE EXCEPTION
            '% non-zero non-live positions do not reconcile with execution fill-lineage attribution',
            nonzero_nonlive_position_count;
    END IF;
END $$;

-- Retire removed implementations without deleting catalogue lineage, signals,
-- executions, or research evidence. This also converges an existing local volume
-- that was seeded with the former Demo Peer/Donchian binding.
UPDATE user_strategy_bindings binding
SET is_active = FALSE,
    autopilot = FALSE,
    entries_enabled = FALSE,
    exits_enabled = FALSE,
    updated_at = NOW()
FROM vm_retired_strategy_ids retired
WHERE binding.strategy_id = retired.strategy_id
  AND (
      binding.is_active
      OR binding.autopilot
      OR binding.entries_enabled
      OR binding.exits_enabled
  );

UPDATE user_strategy_configs config
SET is_active = FALSE, updated_at = NOW()
FROM vm_retired_strategy_ids retired
WHERE config.strategy_id = retired.strategy_id
  AND config.is_active;

UPDATE strategies strategy
SET is_active = FALSE, updated_at = NOW()
FROM vm_retired_strategy_ids retired
WHERE strategy.strategy_id = retired.strategy_id
  AND strategy.is_active;

UPDATE strategy_versions version
SET status = 'deprecated'
FROM vm_retired_strategy_ids retired
WHERE version.strategy_id = retired.strategy_id
  AND version.status <> 'deprecated';

-- Retire version/catalogue rows before converging feedback. This lock order
-- closes the race with suggestion creation: a concurrent insert either commits
-- first and is expired below, or observes the inactive catalogue and aborts.
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
FROM vm_retired_strategy_ids retired
WHERE feedback.strategy_id = retired.strategy_id
  AND feedback.status IN ('pending', 'approved');

-- ── User and tenancy controls ───────────────────────────────────────────────
INSERT INTO users (user_id, org_id, email, full_name, tz, base_ccy, status, created_at)
VALUES (
    'demo_peer', 1, 'demo_peer@example.invalid', 'Demo Peer',
    'America/New_York', 'USD', 'active', NOW()
)
ON CONFLICT (user_id) DO UPDATE SET
    status = EXCLUDED.status,
    tz = EXCLUDED.tz,
    base_ccy = EXCLUDED.base_ccy;

INSERT INTO user_roles (user_id, role)
VALUES ('demo_peer', 'trader')
ON CONFLICT DO NOTHING;

INSERT INTO user_plan_subscriptions (user_id, plan_id, status, started_at)
SELECT 'demo_peer', 2, 'active', NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM user_plan_subscriptions
    WHERE user_id = 'demo_peer' AND status = 'active'
);

INSERT INTO linked_broker_accounts (
    user_id, broker_id, environment, display_name, external_ref, base_ccy,
    paper_initial_equity, paper_initial_cash, status, created_at
)
SELECT
    'demo_peer', broker_id, 'paper', 'Local paper', 'local-paper:demo_peer',
    'USD', 100000.00, 100000.00, 'connected', NOW()
FROM brokers
WHERE code = 'paper'
  AND NOT EXISTS (
      SELECT 1
      FROM linked_broker_accounts
      WHERE user_id = 'demo_peer' AND external_ref = 'local-paper:demo_peer'
  );

UPDATE linked_broker_accounts
SET paper_initial_equity = 100000.00,
    paper_initial_cash = 100000.00
WHERE user_id = 'demo_peer'
  AND external_ref = 'local-paper:demo_peer';

INSERT INTO sizing_profiles (user_id, name, method, params, is_default, created_at)
SELECT
    'demo_peer', 'Fixed-Fraction 1%', 'fixed_pct',
    '{"fixed_pct": 0.01}', TRUE, NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM sizing_profiles
    WHERE user_id = 'demo_peer' AND name = 'Fixed-Fraction 1%'
);

UPDATE sizing_profiles
SET method = 'fixed_pct', params = '{"fixed_pct": 0.01}', is_default = TRUE
WHERE user_id = 'demo_peer' AND name = 'Fixed-Fraction 1%';

DO $$
DECLARE
    retired_strategy_count INTEGER;
    active_retired_count INTEGER;
    active_retired_catalogue_count INTEGER;
    active_retired_version_count INTEGER;
    active_retired_config_count INTEGER;
    actionable_retired_feedback_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO retired_strategy_count
    FROM vm_retired_strategy_ids;
    IF retired_strategy_count <> 18 THEN
        RAISE EXCEPTION 'Expected 18 retired strategy IDs, found %',
            retired_strategy_count;
    END IF;

    SELECT COUNT(*) INTO actionable_retired_feedback_count
    FROM strategy_parameter_feedback feedback
    JOIN vm_retired_strategy_ids retired
      ON retired.strategy_id = feedback.strategy_id
    WHERE feedback.status IN ('pending', 'approved');
    IF actionable_retired_feedback_count <> 0 THEN
        RAISE EXCEPTION
            'Retirement convergence left % actionable parameter feedback row(s)',
            actionable_retired_feedback_count;
    END IF;

    SELECT COUNT(*) INTO active_retired_count
    FROM user_strategy_bindings
    WHERE (is_active OR autopilot OR entries_enabled OR exits_enabled)
      AND strategy_id IN (SELECT strategy_id FROM vm_retired_strategy_ids);
    IF active_retired_count <> 0 THEN
        RAISE EXCEPTION 'Found % active/autopilot bindings for retired strategies',
            active_retired_count;
    END IF;

    SELECT COUNT(*) INTO active_retired_catalogue_count
    FROM strategies
    WHERE is_active
      AND strategy_id IN (SELECT strategy_id FROM vm_retired_strategy_ids);
    SELECT COUNT(*) INTO active_retired_version_count
    FROM strategy_versions
    WHERE status = 'active'
      AND strategy_id IN (SELECT strategy_id FROM vm_retired_strategy_ids);
    SELECT COUNT(*) INTO active_retired_config_count
    FROM user_strategy_configs
    WHERE is_active
      AND strategy_id IN (SELECT strategy_id FROM vm_retired_strategy_ids);
    IF active_retired_catalogue_count <> 0
       OR active_retired_version_count <> 0
       OR active_retired_config_count <> 0 THEN
        RAISE EXCEPTION
            'Retired strategy state remains active: catalogue=% versions=% configs=%',
            active_retired_catalogue_count,
            active_retired_version_count,
            active_retired_config_count;
    END IF;

    RAISE NOTICE
        'Second paper tenant seed verified: 18 retired strategies inactive';
END $$;

COMMIT;
