-- E2E Test: Create user 'demo_user' with isolated local-paper strategy ownership

BEGIN;

-- User
INSERT INTO users (user_id, org_id, email, full_name, tz, base_ccy, status, created_at)
VALUES ('demo_user', 1, 'demo_user@example.invalid', 'Demo User', 'Europe/Amsterdam', 'EUR', 'active', NOW())
ON CONFLICT DO NOTHING;

-- Role
INSERT INTO user_roles (user_id, role) VALUES ('demo_user', 'trader') ON CONFLICT DO NOTHING;

-- Pro plan
INSERT INTO user_plan_subscriptions (user_id, plan_id, status, started_at)
VALUES ('demo_user', 3, 'active', NOW())
ON CONFLICT DO NOTHING;

-- Explicit local-paper account. Execution authorization is account-scoped; the
-- engine must never auto-create or guess an account after a signal is approved.
INSERT INTO linked_broker_accounts (
    user_id, broker_id, environment, display_name, external_ref, base_ccy,
    paper_initial_equity, paper_initial_cash, status, created_at
)
SELECT
    'demo_user', broker_id, 'paper', 'Local paper', 'local-paper:demo_user',
    'EUR', 100000.00, 100000.00, 'connected', NOW()
FROM brokers
WHERE code = 'paper'
  AND NOT EXISTS (
      SELECT 1
      FROM linked_broker_accounts
      WHERE user_id = 'demo_user' AND external_ref = 'local-paper:demo_user'
  );

UPDATE linked_broker_accounts
SET paper_initial_equity = 100000.00,
    paper_initial_cash = 100000.00
WHERE user_id = 'demo_user'
  AND external_ref = 'local-paper:demo_user';

-- Dedicated first-canary binding: only SwingHighLowPMO may own the top-3
-- crypto pairs (BTC/ETH/SOL vs USDC) on this account. EMA/RSI remain staged
-- inactive until this binding is retired.
INSERT INTO user_strategy_bindings (
    user_id, strategy_id, broker_account_id,
    asset_score_threshold, sector_score_threshold, market_score_threshold,
    execution_modes_allowed, preferred_mode, mode_selection_policy,
    asset_classes_allowed, instruments_allowed, sectors_allowed,
    max_position_pct, max_total_exposure_pct, max_daily_loss_pct, max_open_positions,
    allowed_brokers, is_active, autopilot, entries_enabled, exits_enabled,
    created_at, updated_at
) VALUES (
    'demo_user', 'swing_high_low_pmo_v1',
    (SELECT account_id FROM linked_broker_accounts
     WHERE user_id = 'demo_user' AND external_ref = 'local-paper:demo_user'),
    0.0, NULL, NULL,
    '["spot"]', 'spot', 'fixed',
    '["crypto"]', '["BTC/USDC", "ETH/USDC", "SOL/USDC"]', NULL,
    0.20, 0.50, 0.05, 3,
    '["paper"]', TRUE, TRUE, TRUE, TRUE, NOW(), NOW()
) ON CONFLICT (user_id, strategy_id, broker_account_id) DO UPDATE SET
    asset_score_threshold = EXCLUDED.asset_score_threshold,
    sector_score_threshold = EXCLUDED.sector_score_threshold,
    market_score_threshold = EXCLUDED.market_score_threshold,
    execution_modes_allowed = EXCLUDED.execution_modes_allowed,
    preferred_mode = EXCLUDED.preferred_mode,
    mode_selection_policy = EXCLUDED.mode_selection_policy,
    asset_classes_allowed = EXCLUDED.asset_classes_allowed,
    instruments_allowed = EXCLUDED.instruments_allowed,
    sectors_allowed = EXCLUDED.sectors_allowed,
    max_position_pct = EXCLUDED.max_position_pct,
    max_total_exposure_pct = EXCLUDED.max_total_exposure_pct,
    max_daily_loss_pct = EXCLUDED.max_daily_loss_pct,
    max_open_positions = EXCLUDED.max_open_positions,
    allowed_brokers = EXCLUDED.allowed_brokers,
    broker_account_id = EXCLUDED.broker_account_id,
    is_active = EXCLUDED.is_active,
    autopilot = EXCLUDED.autopilot,
    entries_enabled = EXCLUDED.entries_enabled,
    exits_enabled = EXCLUDED.exits_enabled,
    updated_at = NOW();

-- The 20% ceiling is the platform's validated RiskLimits default. This exact
-- user-owned row is idempotent and enables the durable account-drawdown gate;
-- it does not enable live execution or automatic liquidation.
INSERT INTO risk_mandates (user_id, rules, effective_at)
SELECT 'demo_user', '{"max_drawdown_pct":0.20}', NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM risk_mandates
    WHERE user_id = 'demo_user'
      AND rules::jsonb = '{"max_drawdown_pct":0.20}'::jsonb
);


-- Sizing profile
-- sizing_profiles has an autoincrement PK and no unique key, so guard with
-- NOT EXISTS to keep the seed idempotent across repeated `compose up` runs.
INSERT INTO sizing_profiles (user_id, name, method, params, is_default, created_at)
SELECT 'demo_user', 'Risk-Based 2%', 'risk_pct', '{"risk_pct": 0.02, "stop_loss_pct": 0.05}', true, NOW()
WHERE NOT EXISTS (
    SELECT 1 FROM sizing_profiles WHERE user_id = 'demo_user' AND name = 'Risk-Based 2%'
);

-- Verify
DO $$
BEGIN
    RAISE NOTICE 'User demo_user created: %', (SELECT COUNT(*) FROM users WHERE user_id = 'demo_user');
    RAISE NOTICE 'Bindings created: %', (SELECT COUNT(*) FROM user_strategy_bindings WHERE user_id = 'demo_user');
    RAISE NOTICE 'Paper accounts created: %', (SELECT COUNT(*) FROM linked_broker_accounts WHERE user_id = 'demo_user' AND environment = 'paper');
END $$;

COMMIT;
