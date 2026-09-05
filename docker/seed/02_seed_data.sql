-- Seed Data for the vynmatrix platform Database
-- This script runs after schema creation

\if :{?image_tag}
\else
\set image_tag latest
\endif

BEGIN;

-- ============================================================================
-- A) Organization
-- ============================================================================

INSERT INTO orgs (org_id, name, created_at)
VALUES
    (1, 'vynmatrix Trading', NOW())
ON CONFLICT DO NOTHING;

-- Reset sequence
SELECT setval('orgs_org_id_seq', (SELECT MAX(org_id) FROM orgs));

-- ============================================================================
-- B) Commercial Plans
-- ============================================================================

INSERT INTO plans (plan_id, code, features)
VALUES
    (1, 'free', '{"max_strategies": 2, "max_signals_per_day": 10, "paper_trading": true, "live_trading": false}'),
    (2, 'starter', '{"max_strategies": 5, "max_signals_per_day": 100, "paper_trading": true, "live_trading": true, "brokers": ["coinbase"]}'),
    (3, 'pro', '{"max_strategies": 20, "max_signals_per_day": 1000, "paper_trading": true, "live_trading": true, "brokers": ["coinbase", "deribit", "delta"]}'),
    (4, 'enterprise', '{"max_strategies": -1, "max_signals_per_day": -1, "paper_trading": true, "live_trading": true, "brokers": ["all"], "custom_strategies": true}')
ON CONFLICT DO NOTHING;

SELECT setval('plans_plan_id_seq', (SELECT MAX(plan_id) FROM plans));

-- ============================================================================
-- C) Brokers
-- ============================================================================

INSERT INTO brokers (code, name, capabilities)
VALUES
    ('coinbase', 'Coinbase', '{"asset_classes": ["crypto"], "order_types": ["market", "limit", "stop", "stop_limit"], "features": ["spot"], "regions": ["US", "EU", "UK"], "exact_fill_retrieval": true, "live_certification_implemented": true}'),
    ('deribit', 'Deribit', '{"asset_classes": ["crypto"], "order_types": ["market", "limit", "stop_limit"], "features": ["perpetual", "futures", "options"], "regions": ["GLOBAL"], "exact_fill_retrieval": true, "live_certification_implemented": false}'),
    ('delta', 'Delta Exchange', '{"asset_classes": ["crypto"], "order_types": ["market", "limit", "stop", "stop_limit"], "features": ["perpetual", "futures", "options"], "regions": ["GLOBAL", "IN"], "exact_fill_retrieval": false, "live_certification_implemented": false}'),
    ('ibkr', 'Interactive Brokers', '{"asset_classes": ["equity", "etf", "futures", "options", "fx", "crypto", "commodities"], "order_types": ["market", "limit", "stop", "stop_limit", "trailing_stop", "bracket"], "features": ["spot", "margin", "futures", "options"], "regions": ["US", "EU", "ASIA"], "exact_fill_retrieval": false, "live_certification_implemented": false}'),
    ('zerodha', 'Zerodha', '{"asset_classes": ["equity", "etf", "options", "futures", "fx"], "order_types": ["market", "limit", "stop", "stop_limit"], "features": ["spot", "margin", "futures", "options"], "regions": ["IN"], "exact_fill_retrieval": false, "live_certification_implemented": false}'),
    -- Local paper-trading simulator (N5). Paper FILLS are simulated in-process by
    -- the PaperBroker (no real exchange credentials), but every user must have an
    -- explicitly configured linked account with owned capital and base currency.
    ('paper', 'Paper (local simulator)', '{"asset_classes": ["crypto", "equity", "etf", "futures", "options", "fx", "commodities"], "order_types": ["market", "limit", "stop"], "features": ["spot", "margin", "futures", "options"], "regions": ["global"], "exact_fill_retrieval": true, "live_certification_implemented": false}'),
    ('saxo', 'Saxo Bank', '{"asset_classes": ["equity", "etf", "futures", "options", "fx", "commodities"], "order_types": ["market", "limit", "stop", "stop_limit"], "features": ["spot", "margin", "futures", "options"], "regions": ["global"], "exact_fill_retrieval": false, "live_certification_implemented": false}')
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    capabilities = EXCLUDED.capabilities;

SELECT setval('brokers_broker_id_seq', (SELECT MAX(broker_id) FROM brokers));

-- ============================================================================
-- D) Broker Environments
-- ============================================================================

WITH desired(
    broker_code,
    environment,
    region,
    base_urls,
    rate_limits
) AS (
    VALUES
        ('coinbase', 'paper', 'US', '{"rest": "https://api-sandbox.coinbase.com", "ws": null}'::JSONB, '{"requests_per_second": 10}'::JSONB),
        ('coinbase', 'live', 'US', '{"rest": "https://api.coinbase.com", "ws": "wss://advanced-trade-ws.coinbase.com"}'::JSONB, '{"requests_per_second": 10}'::JSONB),
        ('deribit', 'paper', 'global', '{"rest": "https://test.deribit.com/api/v2", "ws": "wss://test.deribit.com/ws/api/v2"}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('deribit', 'live', 'global', '{"rest": "https://www.deribit.com/api/v2", "ws": "wss://www.deribit.com/ws/api/v2"}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('delta', 'paper', 'global', '{"rest": "https://testnet-api.delta.exchange", "ws": "wss://testnet-socket.delta.exchange"}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('delta', 'live', 'global', '{"rest": "https://api.delta.exchange", "ws": "wss://socket.delta.exchange"}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('delta', 'paper', 'india', '{"rest": "https://cdn-ind.testnet.deltaex.org", "ws": null}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('delta', 'live', 'india', '{"rest": "https://api.india.delta.exchange", "ws": null}'::JSONB, '{"requests_per_second": 20}'::JSONB),
        ('ibkr', 'paper', 'US', '{"rest": "https://api.ibkr.com/v1/portal", "gateway": "https://localhost:5000"}'::JSONB, '{"requests_per_second": 50}'::JSONB),
        ('ibkr', 'live', 'US', '{"rest": "https://api.ibkr.com/v1/portal", "gateway": "https://localhost:5000"}'::JSONB, '{"requests_per_second": 50}'::JSONB),
        ('zerodha', 'paper', 'IN', '{"rest": "https://api.kite.trade"}'::JSONB, '{"requests_per_second": 3}'::JSONB),
        ('zerodha', 'live', 'IN', '{"rest": "https://api.kite.trade"}'::JSONB, '{"requests_per_second": 3}'::JSONB),
        ('saxo', 'paper', 'global', '{"rest": "https://gateway.saxobank.com/sim/openapi", "ws": "wss://sim-streaming.saxobank.com/sim/oapi/streaming/ws"}'::JSONB, '{"requests_per_minute": 120, "orders_per_second": 1}'::JSONB),
        ('saxo', 'live', 'global', '{"rest": "https://gateway.saxobank.com/openapi", "ws": "wss://live-streaming.saxobank.com/oapi/streaming/ws"}'::JSONB, '{"requests_per_minute": 120, "orders_per_second": 1}'::JSONB),
        ('paper', 'paper', 'global', '{"transport": "in_process"}'::JSONB, '{}'::JSONB)
)
INSERT INTO broker_environments (
    broker_id,
    environment,
    region,
    base_urls,
    rate_limits
)
SELECT
    broker.broker_id,
    desired.environment,
    desired.region,
    desired.base_urls,
    desired.rate_limits
FROM desired
JOIN brokers AS broker ON broker.code = desired.broker_code
ON CONFLICT (broker_id, environment, region) DO UPDATE SET
    base_urls = EXCLUDED.base_urls,
    rate_limits = EXCLUDED.rate_limits;

SELECT setval('broker_environments_broker_env_id_seq', (SELECT MAX(broker_env_id) FROM broker_environments));

-- ============================================================================
-- E) Common Instruments
-- ============================================================================

INSERT INTO instruments (
    instr_id,
    asset_class,
    canonical,
    exchange,
    settlement_currency,
    tick_size,
    lot_size,
    market_session_policy,
    is_tradable
)
VALUES
    -- Crypto
    (1, 'crypto', 'BTC/USD', 'global', 'USD', 0.01, 0.0001, 'continuous', TRUE),
    (2, 'crypto', 'ETH/USD', 'global', 'USD', 0.01, 0.001, 'continuous', TRUE),
    (3, 'crypto', 'SOL/USD', 'global', 'USD', 0.01, 0.01, 'continuous', TRUE),
    (4, 'crypto', 'BTC/USDT', 'global', 'USDT', 0.01, 0.0001, 'continuous', TRUE),
    (5, 'crypto', 'ETH/USDT', 'global', 'USDT', 0.01, 0.001, 'continuous', TRUE),
    -- USDC-quoted pairs used by EUR-funded Coinbase accounts. The USD pairs
    -- above remain valid global market-data and broker instruments.
    (11, 'crypto', 'BTC/USDC', 'global', 'USDC', 0.01, 0.0001, 'continuous', TRUE),
    (12, 'crypto', 'ETH/USDC', 'global', 'USDC', 0.01, 0.001, 'continuous', TRUE),
    (13, 'crypto', 'SOL/USDC', 'global', 'USDC', 0.01, 0.01, 'continuous', TRUE),
    -- ETFs and equities retain distinct canonical classes even when a venue
    -- represents both with the same external security type.
    (6, 'etf', 'SPY', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    (7, 'etf', 'QQQ', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    (8, 'equity', 'AAPL', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    (9, 'equity', 'NVDA', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE)
ON CONFLICT DO NOTHING;

-- Cash indices are market/scoring references, never direct order targets.
-- Futures and options must be provisioned as their own concrete instruments.
INSERT INTO instruments (
    asset_class,
    canonical,
    exchange,
    settlement_currency,
    tick_size,
    lot_size,
    market_session_policy,
    is_tradable
)
VALUES
    ('index', 'NIFTY50', 'NSE', 'INR', 0.05, 1, 'scheduled', FALSE),
    ('index', 'BANKNIFTY', 'NSE', 'INR', 0.05, 1, 'scheduled', FALSE)
ON CONFLICT (asset_class, canonical) DO UPDATE SET
    exchange = EXCLUDED.exchange,
    settlement_currency = EXCLUDED.settlement_currency,
    tick_size = EXCLUDED.tick_size,
    lot_size = EXCLUDED.lot_size,
    market_session_policy = EXCLUDED.market_session_policy,
    is_tradable = EXCLUDED.is_tradable;


-- US equity catalogue: the remaining large-cap US listings beyond the
-- AAPL/NVDA rows above. EODHD/vendor US tickers ARE the canonical equity
-- symbols, so no aliases are required. These are catalogue reference rows and
-- are not owned by any one strategy.
INSERT INTO instruments (
    asset_class,
    canonical,
    exchange,
    settlement_currency,
    tick_size,
    lot_size,
    market_session_policy,
    is_tradable
)
VALUES
    ('equity', 'TSLA', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'MSFT', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'AMZN', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'GOOGL', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'GOOG', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'META', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'MU', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'AMD', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'AVGO', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'PLTR', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'NFLX', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'COST', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'INTC', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'HOOD', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'COIN', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'APP', 'NASDAQ', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'ORCL', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'LLY', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'WMT', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'XOM', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'JPM', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'CRM', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE),
    ('equity', 'UNH', 'NYSE', 'USD', 0.01, 1, 'scheduled', TRUE)
ON CONFLICT (asset_class, canonical) DO UPDATE SET
    exchange = EXCLUDED.exchange,
    settlement_currency = EXCLUDED.settlement_currency,
    tick_size = EXCLUDED.tick_size,
    lot_size = EXCLUDED.lot_size,
    market_session_policy = EXCLUDED.market_session_policy,
    is_tradable = EXCLUDED.is_tradable;

-- Revision 0054 owns the observed FX catalogue (EUR/USD, EUR/INR, EUR/GBP,
-- USDC/EUR) because the FX ingestor must also work on migration-only
-- production databases. Do not duplicate those rows here.

SELECT setval('instruments_instr_id_seq', (SELECT MAX(instr_id) FROM instruments));

-- ============================================================================
-- H1) Instrument Aliases (map vendor symbols to canonical symbols)
-- ============================================================================

INSERT INTO instrument_aliases (instr_id, alias, source)
SELECT instrument.instr_id, desired.alias, 'seed'
FROM (
    VALUES
        ('BTC/USD', 'BTCUSD'),
        ('ETH/USD', 'ETHUSD'),
        ('SOL/USD', 'SOLUSD'),
        ('BTC/USDC', 'BTCUSDC'),
        ('ETH/USDC', 'ETHUSDC'),
        ('SOL/USDC', 'SOLUSDC')
) AS desired(canonical, alias)
JOIN instruments AS instrument
  ON instrument.asset_class = 'crypto'
 AND instrument.canonical = desired.canonical
ON CONFLICT DO NOTHING;

SELECT setval('instrument_aliases_alias_id_seq', (SELECT MAX(alias_id) FROM instrument_aliases));

-- ============================================================================
-- F) Instrument-Broker Symbol Mapping
-- ============================================================================

WITH desired(
    canonical,
    broker_code,
    broker_symbol,
    broker_instrument_id,
    broker_instrument_type
) AS (
    VALUES
        ('BTC/USD', 'coinbase', 'BTC-USD', NULL, NULL),
        ('ETH/USD', 'coinbase', 'ETH-USD', NULL, NULL),
        ('SOL/USD', 'coinbase', 'SOL-USD', NULL, NULL),
        ('BTC/USDC', 'coinbase', 'BTC-USDC', NULL, NULL),
        ('ETH/USDC', 'coinbase', 'ETH-USDC', NULL, NULL),
        ('SOL/USDC', 'coinbase', 'SOL-USDC', NULL, NULL),
        ('BTC/USD', 'deribit', 'BTC-PERPETUAL', NULL, NULL),
        ('ETH/USD', 'deribit', 'ETH-PERPETUAL', NULL, NULL),
        ('BTC/USD', 'delta', 'BTCUSD', NULL, NULL),
        ('ETH/USD', 'delta', 'ETHUSD', NULL, NULL),
        ('SPY', 'ibkr', 'SPY', NULL, NULL),
        ('QQQ', 'ibkr', 'QQQ', NULL, NULL),
        -- IBKR's official Web API order examples identify AAPL as conid 265598.
        ('AAPL', 'ibkr', 'AAPL', '265598', NULL),
        ('NVDA', 'ibkr', 'NVDA', NULL, NULL)
)
INSERT INTO instrument_broker_symbols (
    instr_id,
    broker_id,
    broker_symbol,
    broker_instrument_id,
    broker_instrument_type
)
SELECT
    instrument.instr_id,
    broker.broker_id,
    desired.broker_symbol,
    desired.broker_instrument_id,
    desired.broker_instrument_type
FROM desired
JOIN instruments AS instrument ON instrument.canonical = desired.canonical
JOIN brokers AS broker ON broker.code = desired.broker_code
ON CONFLICT (instr_id, broker_id) DO UPDATE SET
    broker_symbol = EXCLUDED.broker_symbol,
    broker_instrument_id = EXCLUDED.broker_instrument_id,
    broker_instrument_type = EXCLUDED.broker_instrument_type
WHERE EXCLUDED.broker_instrument_id IS NOT NULL;

INSERT INTO instrument_broker_symbols (
    instr_id,
    broker_id,
    broker_symbol,
    broker_instrument_id,
    broker_instrument_type
)
SELECT
    instrument.instr_id,
    broker.broker_id,
    desired.broker_symbol,
    desired.broker_instrument_id,
    desired.broker_instrument_type
FROM (
    VALUES
        ('USDC/EUR', 'coinbase', 'USDC-EUR', NULL, NULL),
        ('EUR/USD', 'ibkr', 'EUR.USD', NULL, NULL),
        -- Saxo publishes EURUSD as UIC 21 / FxSpot in its OpenAPI reference.
        ('EUR/USD', 'saxo', 'EURUSD', '21', 'FxSpot')
) AS desired(
    canonical,
    broker_code,
    broker_symbol,
    broker_instrument_id,
    broker_instrument_type
)
JOIN instruments AS instrument
  ON instrument.asset_class = 'fx'
 AND instrument.canonical = desired.canonical
JOIN brokers AS broker ON broker.code = desired.broker_code
ON CONFLICT DO NOTHING;
-- ============================================================================
-- G) Strategy Catalog (seed indicator strategy codes)
-- ============================================================================

INSERT INTO strategies (strategy_id, strategy_name, asset_class, description, is_active)
VALUES
    (
        'swing_high_low_pmo_v1',
        'SwingHighLowPMO',
        'crypto',
        'Swing High/Low PMO indicator strategy running in pure-core signal_worker mode.',
        TRUE
    ),
    (
        'us_quality_compounder_v1',
        'USQualityCompounder',
        'equity',
        'Disabled quarterly point-in-time S&P 500 quality-compounder strategy.',
        FALSE
    )
ON CONFLICT (strategy_id) DO UPDATE SET
    strategy_name = EXCLUDED.strategy_name,
    asset_class = EXCLUDED.asset_class,
    description = EXCLUDED.description,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- status + released_at are NOT NULL with Python-side ORM defaults only (no DB
-- default under create_all), so the seed must supply them explicitly.
DO $$
DECLARE
    collision_count INTEGER;
    duplicate_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO collision_count
    FROM strategy_versions existing
    JOIN (
        VALUES
            (1004::BIGINT, 'swing_high_low_pmo_v1', '1.0.0'),
            (1108::BIGINT, 'swing_high_low_pmo_v1', '1.0.1'),
            (1120::BIGINT, 'swing_high_low_pmo_v1', '1.1.0')
    ) AS expected(strat_ver_id, strategy_id, semver)
      ON existing.strat_ver_id = expected.strat_ver_id
    WHERE existing.strategy_id <> expected.strategy_id
       OR existing.semver <> expected.semver;
    IF collision_count > 0 THEN
        RAISE EXCEPTION 'SwingHighLowPMO strategy-version ID 1004, 1108, or 1120 collides with existing lineage';
    END IF;

    SELECT COUNT(*) INTO duplicate_count
    FROM (
        SELECT strategy_id, semver
        FROM strategy_versions
        WHERE strategy_id = 'swing_high_low_pmo_v1'
          AND semver IN ('1.0.0', '1.0.1', '1.1.0')
        GROUP BY strategy_id, semver
        HAVING COUNT(*) > 1
    ) duplicates;
    IF duplicate_count > 0 THEN
        RAISE EXCEPTION 'Duplicate SwingHighLowPMO strategy/version rows require manual lineage repair';
    END IF;
END $$;

WITH desired_versions(strat_ver_id, strategy_id, semver, version_status) AS (
    VALUES
        (1004::BIGINT, 'swing_high_low_pmo_v1', '1.0.0', 'deprecated'),
        (1108::BIGINT, 'swing_high_low_pmo_v1', '1.0.1', 'deprecated'),
        (1120::BIGINT, 'swing_high_low_pmo_v1', '1.1.0', 'active')
)
UPDATE strategy_versions existing
SET status = desired_versions.version_status
FROM desired_versions
WHERE existing.strategy_id = desired_versions.strategy_id
  AND existing.semver = desired_versions.semver;

WITH desired_versions(
    strat_ver_id, strategy_id, semver, version_status, default_params
) AS (
    VALUES
        (
            1004::BIGINT,
            'swing_high_low_pmo_v1',
            '1.0.0',
            'deprecated',
            '{"runner_kind":"signal_worker"}'::JSONB
        ),
        (
            1108::BIGINT,
            'swing_high_low_pmo_v1',
            '1.0.1',
            'deprecated',
            '{
                "universe":"BTCUSDC",
                "asset_class":"crypto",
                "strategy_version":"1.0.1",
                "trade_direction_mode":"long_only",
                "pmo_first_length":35,
                "pmo_second_length":20,
                "pmo_signal_length":10,
                "ema_period":50,
                "swing_length":5,
                "risk_reward_ratio":1.5,
                "max_swing_age_bars":20,
                "max_stop_distance_pct":0.10,
                "trading_start_hour":7,
                "trading_end_hour":23,
                "trading_timezone":"UTC"
            }'::JSONB
        ),
        (
            1120::BIGINT,
            'swing_high_low_pmo_v1',
            '1.1.0',
            'active',
            '{
                "universe":"BTCUSDC,ETHUSDC,SOLUSDC",
                "asset_class":"crypto",
                "strategy_version":"1.1.0",
                "trade_direction_mode":"long_only",
                "pmo_first_length":35,
                "pmo_second_length":20,
                "pmo_signal_length":10,
                "ema_period":50,
                "swing_length":5,
                "risk_reward_ratio":1.5,
                "max_swing_age_bars":20,
                "max_stop_distance_pct":0.10,
                "trading_start_hour":7,
                "trading_end_hour":23,
                "trading_timezone":"UTC"
            }'::JSONB
        )
)
INSERT INTO strategy_versions (
    strat_ver_id, strategy_id, semver, docker_image,
    param_schema, default_params, status, released_at
)
SELECT
    desired_versions.strat_ver_id,
    desired_versions.strategy_id,
    desired_versions.semver,
    'vynmatrix/indicator-runner:' || :'image_tag',
    '{}'::JSONB,
    desired_versions.default_params,
    desired_versions.version_status,
    NOW()
FROM desired_versions
ON CONFLICT (strategy_id, semver) DO UPDATE SET
    -- Version parameter snapshots are immutable after registration.
    status = EXCLUDED.status;

SELECT setval('strategy_versions_strat_ver_id_seq', (SELECT MAX(strat_ver_id) FROM strategy_versions));

-- Disabled strategy lineage only. No user/account binding or execution
-- authority is seeded; paper activation remains an explicit promotion step.
DO $$
DECLARE
    collision_count INTEGER;
    lineage_mismatch_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO collision_count
    FROM strategy_versions existing
    JOIN (
        VALUES
            (1400::BIGINT, 'us_quality_compounder_v1', '0.1.0'),
            (1401::BIGINT, 'us_quality_compounder_v1', '0.2.0')
    ) AS expected(strat_ver_id, strategy_id, semver)
      ON existing.strat_ver_id = expected.strat_ver_id
    WHERE existing.strategy_id <> expected.strategy_id
       OR existing.semver <> expected.semver;
    IF collision_count > 0 THEN
        RAISE EXCEPTION
            'USQualityCompounder strategy-version ID 1400 or 1401 collides with existing lineage';
    END IF;

    SELECT COUNT(*) INTO lineage_mismatch_count
    FROM strategy_versions existing
    JOIN (
        VALUES
            (1400::BIGINT, 'us_quality_compounder_v1', '0.1.0'),
            (1401::BIGINT, 'us_quality_compounder_v1', '0.2.0')
    ) AS expected(strat_ver_id, strategy_id, semver)
      ON existing.strategy_id = expected.strategy_id
     AND existing.semver = expected.semver
    WHERE existing.strat_ver_id <> expected.strat_ver_id;
    IF lineage_mismatch_count > 0 THEN
        RAISE EXCEPTION
            'USQualityCompounder strategy/version lineage uses an unexpected ID';
    END IF;
END $$;

INSERT INTO strategy_versions (
    strat_ver_id, strategy_id, semver, docker_image,
    param_schema, default_params, status, released_at
)
VALUES
    (
        1400::BIGINT,
        'us_quality_compounder_v1',
        '0.1.0',
        'vynmatrix/indicator-runner:' || :'image_tag',
        '{}'::JSONB,
        '{
            "runner_kind":"signal_worker",
            "universe":"SP500",
            "universe_contract":"point_in_time_sp500_membership",
            "asset_class":"equity",
            "trade_direction_mode":"long_only",
            "target_holdings":"15",
            "max_panel_age_days":"100"
        }'::JSONB,
        'deprecated',
        NOW()
    ),
    (
        1401::BIGINT,
        'us_quality_compounder_v1',
        '0.2.0',
        'vynmatrix/indicator-runner:' || :'image_tag',
        '{}'::JSONB,
        '{
            "runner_kind":"signal_worker",
            "universe":"SP500",
            "universe_contract":"point_in_time_sp500_membership",
            "asset_class":"equity",
            "trade_direction_mode":"long_only",
            "target_holdings":"15",
            "max_panel_age_days":"100"
        }'::JSONB,
        'active',
        NOW()
    )
ON CONFLICT (strategy_id, semver) DO UPDATE SET
    status = EXCLUDED.status;

SELECT setval('strategy_versions_strat_ver_id_seq', (SELECT MAX(strat_ver_id) FROM strategy_versions));

-- ============================================================================
-- H) Sectors
-- ============================================================================

INSERT INTO sectors (sector_id, code, name, asset_class, parent_sector_id, description, created_at)
VALUES
    -- Crypto sectors
    (1, 'crypto-layer1', 'Layer 1 Blockchains', 'crypto', NULL, 'Base layer blockchain protocols', NOW()),
    (2, 'crypto-defi', 'DeFi', 'crypto', NULL, 'Decentralized finance protocols', NOW()),
    (3, 'crypto-btc-eco', 'Bitcoin Ecosystem', 'crypto', 1, 'Bitcoin and related assets', NOW()),
    (4, 'crypto-eth-eco', 'Ethereum Ecosystem', 'crypto', 1, 'Ethereum and L2 solutions', NOW()),
    -- Equity sectors
    (5, 'equity-tech', 'Technology', 'equity', NULL, 'Technology sector', NOW()),
    (6, 'equity-semis', 'Semiconductors', 'equity', 5, 'Semiconductor companies', NOW()),
    (7, 'equity-software', 'Software', 'equity', 5, 'Software companies', NOW())
ON CONFLICT DO NOTHING;

-- Explicit legacy IDs do not advance PostgreSQL's sequence. Synchronize it
-- before inserting the dynamic ETF/index hierarchy or a fresh database will
-- reuse sector_id=1 and abort the entire transactional seed.
SELECT setval('sectors_sector_id_seq', (SELECT MAX(sector_id) FROM sectors));

-- ETF and cash-index hierarchies use dynamic IDs so this seed can converge an
-- existing production catalogue without assuming unused primary keys. These
-- codes match config/instruments.yaml, the hierarchy source used by vmdev DB
-- bootstrap paths.
INSERT INTO sectors (
    code,
    name,
    asset_class,
    parent_sector_id,
    description,
    created_at
)
VALUES
    ('etf', 'Exchange-Traded Funds', 'etf', NULL, 'ETF reference hierarchy', NOW()),
    ('india_index', 'India Cash Indices', 'index', NULL, 'Reference-only India cash indices', NOW())
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    asset_class = EXCLUDED.asset_class,
    parent_sector_id = EXCLUDED.parent_sector_id,
    description = EXCLUDED.description;

WITH desired(code, name, asset_class, parent_code, description) AS (
    VALUES
        ('broad_market_etf', 'Broad-Market ETFs', 'etf', 'etf', 'Broad-market ETF products'),
        ('technology_etf', 'Technology ETFs', 'etf', 'etf', 'Technology-focused ETF products'),
        ('broad_market_index', 'Broad-Market Cash Indices', 'index', 'india_index', 'Reference-only broad-market cash indices'),
        ('banking_index', 'Banking Cash Indices', 'index', 'india_index', 'Reference-only banking cash indices')
)
INSERT INTO sectors (
    code,
    name,
    asset_class,
    parent_sector_id,
    description,
    created_at
)
SELECT
    desired.code,
    desired.name,
    desired.asset_class,
    parent.sector_id,
    desired.description,
    NOW()
FROM desired
JOIN sectors AS parent ON parent.code = desired.parent_code
ON CONFLICT (code) DO UPDATE SET
    name = EXCLUDED.name,
    asset_class = EXCLUDED.asset_class,
    parent_sector_id = EXCLUDED.parent_sector_id,
    description = EXCLUDED.description;

SELECT setval('sectors_sector_id_seq', (SELECT MAX(sector_id) FROM sectors));

-- ============================================================================
-- I) Instrument-Sector Mapping
-- ============================================================================

INSERT INTO instrument_sectors (instr_id, sector_id, weight)
VALUES
    (1, 3, 1.0),   -- BTC -> Bitcoin Ecosystem
    (2, 4, 1.0),   -- ETH -> Ethereum Ecosystem
    (3, 1, 1.0),   -- SOL -> Layer 1
    (8, 5, 0.5),   -- AAPL -> Tech
    (9, 6, 1.0)    -- NVDA -> Semiconductors
ON CONFLICT DO NOTHING;

-- The source-controlled ETF/index hierarchy is authoritative for these four
-- references. Remove superseded classifications before applying both the
-- sector and industry memberships.
DELETE FROM instrument_sectors
WHERE instr_id IN (
    SELECT instr_id
    FROM instruments
    WHERE canonical IN ('SPY', 'QQQ', 'NIFTY50', 'BANKNIFTY')
);

WITH desired(canonical, sector_code) AS (
    VALUES
        ('SPY', 'etf'),
        ('SPY', 'broad_market_etf'),
        ('QQQ', 'etf'),
        ('QQQ', 'technology_etf'),
        ('NIFTY50', 'india_index'),
        ('NIFTY50', 'broad_market_index'),
        ('BANKNIFTY', 'india_index'),
        ('BANKNIFTY', 'banking_index')
)
INSERT INTO instrument_sectors (instr_id, sector_id, weight)
SELECT instrument.instr_id, sector.sector_id, 1.0
FROM desired
JOIN instruments AS instrument ON instrument.canonical = desired.canonical
JOIN sectors AS sector ON sector.code = desired.sector_code
ON CONFLICT (instr_id, sector_id) DO UPDATE SET
    weight = EXCLUDED.weight;

-- ============================================================================
-- M) Default Scoring Rules
-- ============================================================================

INSERT INTO scoring_rules (rule_id, code, name, description, target_type, aggregation_method, min_quorum, decay_enabled, decay_half_life_hours, strategy_weights, is_active, created_at)
VALUES
    (1, 'default_asset_scoring', 'Default Asset Scoring', 'Default weighted average scoring for assets', 'asset', 'weighted_average', 1, true, 24.0, '[]', true, NOW()),
    (2, 'crypto_sector_scoring', 'Crypto Sector Scoring', 'Sector-level scoring for crypto', 'sector', 'weighted_average', 2, true, 12.0, '[]', true, NOW()),
    (3, 'market_consensus', 'Market Consensus', 'Market-wide sentiment scoring', 'market', 'majority_vote', 3, true, 48.0, '[]', true, NOW())
ON CONFLICT DO NOTHING;

SELECT setval('scoring_rules_rule_id_seq', (SELECT MAX(rule_id) FROM scoring_rules));

-- ============================================================================
-- N) Risk Mandates
-- ============================================================================

INSERT INTO risk_mandates (mandate_id, user_id, rules, effective_at)
VALUES
    -- Global risk rules
    (1, NULL, '{"max_position_pct": 0.20, "max_daily_loss_pct": 0.10, "max_open_positions": 30, "restricted_assets": []}', NOW())
ON CONFLICT DO NOTHING;

SELECT setval('risk_mandates_mandate_id_seq', (SELECT MAX(mandate_id) FROM risk_mandates));

-- ============================================================================
-- Verification
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '=== Seed Data Summary ===';
    RAISE NOTICE 'Organizations: %', (SELECT COUNT(*) FROM orgs);
    RAISE NOTICE 'Plans: %', (SELECT COUNT(*) FROM plans);
    RAISE NOTICE 'Users: %', (SELECT COUNT(*) FROM users);
    RAISE NOTICE 'Brokers: %', (SELECT COUNT(*) FROM brokers);
    RAISE NOTICE 'Broker Environments: %', (SELECT COUNT(*) FROM broker_environments);
    RAISE NOTICE 'Instruments: %', (SELECT COUNT(*) FROM instruments);
    RAISE NOTICE 'Sectors: %', (SELECT COUNT(*) FROM sectors);
    RAISE NOTICE 'Scoring Rules: %', (SELECT COUNT(*) FROM scoring_rules);
    RAISE NOTICE '========================';
END $$;

COMMIT;
