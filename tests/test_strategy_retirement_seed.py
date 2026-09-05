from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_RETIREMENT_MIGRATION_PATHS = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0043_retire_vortex_trend_capture.py",
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0044_retire_ma_slope.py",
    _ROOT
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0045_retire_stat_validated_reg_channel_breakout.py",
    _ROOT
    / "scripts"
    / "db"
    / "alembic"
    / "versions"
    / "0046_quarantine_vortex_rsi_profit_target.py",
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0048_retire_quantile_channel.py",
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0049_retire_enhanced_dual_momentum.py",
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0050_retire_volatility_reversal.py",
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0098_retire_liquidity_leaders.py",
)
_RETIREMENT_IDS_0043 = (
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
_CURRENT_RETIREMENT_IDS = (
    *_RETIREMENT_IDS_0043,
    "ma_slope_v1",
    "stat_validated_reg_channel_breakout_v1",
    "vortex_rsi_profit_target_v1",
    "quantile_channel_v1",
    "enhanced_dual_momentum_v1",
    "volatility_reversal_v1",
    "liquidity_leaders_basket_v1",
)
_QUANTILE_QUARANTINE_MIGRATION = (
    _ROOT / "scripts" / "db" / "alembic" / "versions" / "0047_quarantine_quantile_channel.py"
)
_VRPT_RETIREMENT_MIGRATION = _RETIREMENT_MIGRATION_PATHS[3]
_QUANTILE_RETIREMENT_MIGRATION = _RETIREMENT_MIGRATION_PATHS[4]
_EDM_RETIREMENT_MIGRATION = _RETIREMENT_MIGRATION_PATHS[5]
_LEGACY_RETIREMENT_LOCK_ORDER = """LOCK TABLE
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
        IN SHARE ROW EXCLUSIVE MODE;"""
_ATTRIBUTED_RETIREMENT_LOCK_ORDER = _LEGACY_RETIREMENT_LOCK_ORDER.replace(
    "            execution_decision_logs,",
    "            decision_contexts,\n            execution_decision_logs,",
)


def test_canonical_seed_contains_no_fabricated_demo_tenants() -> None:
    seed = (_ROOT / "docker" / "seed" / "02_seed_data.sql").read_text(encoding="utf-8")
    bootstrap = (_ROOT / "scripts" / "db" / "migrate_and_seed.sh").read_text(encoding="utf-8")

    assert "include_demo_tenants" not in seed
    assert "include_demo_tenants" not in bootstrap
    assert "Demo Trader" not in seed
    assert "admin@example.invalid" not in seed


def test_quality_compounder_catalogue_seed_grants_no_default_execution_authority() -> None:
    seed = (_ROOT / "docker" / "seed" / "02_seed_data.sql").read_text(encoding="utf-8")
    e2e_user_seed = (_ROOT / "docker" / "seed" / "03_e2e_test_user.sql").read_text(encoding="utf-8")

    assert "'us_quality_compounder_v1'" in seed
    assert "'USQualityCompounder'" in seed
    assert "1400::BIGINT" in seed
    assert "1401::BIGINT" in seed
    assert "'0.1.0'" in seed
    assert "'0.2.0'" in seed
    assert "'deprecated'" in seed
    assert "'active'" in seed
    assert "USQualityCompounder strategy/version lineage uses an unexpected ID" in seed
    assert "No user/account binding or execution" in seed
    assert "'us_quality_compounder_v1'" not in e2e_user_seed
    assert "'local-paper-sp500:demo_user'" not in e2e_user_seed


def test_global_risk_mandate_can_represent_the_frozen_thirty_name_portfolio() -> None:
    seed = (_ROOT / "docker" / "seed" / "02_seed_data.sql").read_text(encoding="utf-8")

    assert '"max_open_positions": 30' in seed
    assert '"max_open_positions": 20' not in seed


def test_retirement_gate_accounts_for_current_outbox_payload_and_terminal_failures() -> None:
    sql = (_ROOT / "docker" / "seed" / "04_paper_users.sql").read_text(encoding="utf-8")
    gate = sql[sql.index("SELECT\n        (SELECT COUNT(*)") : sql.index("INTO unresolved_count;")]

    assert gate.count("JOIN vm_retired_strategy_ids retired") == 5
    assert "retired_binding.strategy_id = bindings.strategy_id" in gate
    assert "retired_signal.strategy_id = context.strategy_id" in gate
    assert "events.payload::JSONB ->> 'strategy_id'" in gate
    assert "events.payload::JSONB #>> '{signal,strategy_id}'" in gate
    assert "'pending', 'in_progress', 'failed', 'dead_letter'" in gate
    assert "LEFT JOIN user_strategy_bindings bindings" in gate
    assert "LEFT JOIN decision_contexts context" in gate
    assert "bindings.strategy_id IS NULL" in gate
    assert "AND context.strategy_id IS NULL" in gate


def test_current_retirement_guards_block_all_recoverable_pending_order_states() -> None:
    paths = (
        _ROOT / "docker" / "seed" / "04_paper_users.sql",
        _ROOT / "scripts" / "db" / "production_seed_guard.sql",
    )

    for path in paths:
        normalized = " ".join(path.read_text(encoding="utf-8").split())
        pending_gate = normalized[normalized.index("orders.status IN (") :]
        pending_gate = pending_gate[
            : pending_gate.index("+ (SELECT COUNT(*) FROM execution_decision_logs")
        ]
        assert (
            "'pending', 'submission_unknown', 'submitted', 'working', 'partially_filled'"
        ) in pending_gate


def test_retirement_paths_lock_scanned_state_with_a_bounded_timeout() -> None:
    paths = (
        _ROOT / "docker" / "seed" / "04_paper_users.sql",
        _ROOT / "scripts" / "db" / "production_seed_guard.sql",
        *_RETIREMENT_MIGRATION_PATHS,
        _QUANTILE_QUARANTINE_MIGRATION,
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        normalized = " ".join(source.split())
        expected_lock = (
            _LEGACY_RETIREMENT_LOCK_ORDER
            if path.name == "0043_retire_vortex_trend_capture.py"
            else _ATTRIBUTED_RETIREMENT_LOCK_ORDER
        )
        expected_order = " ".join(expected_lock.split())
        assert "SET LOCAL lock_timeout = '5s';" in source
        assert expected_order in normalized
        assert source.index("SET LOCAL lock_timeout") < source.index("INTO unresolved_count")


def test_retirement_seed_has_one_authoritative_strategy_set() -> None:
    sql = (_ROOT / "docker" / "seed" / "04_paper_users.sql").read_text(encoding="utf-8")

    assert "CREATE TEMP TABLE vm_retired_strategy_ids" in sql
    for strategy_id in _CURRENT_RETIREMENT_IDS:
        assert sql.count(f"('{strategy_id}')") == 1


def test_platform_production_seed_paths_scope_positions_by_attribution() -> None:
    """Live positions block unconditionally; non-live positions are scoped by
    the execution fill-lineage attribution (owner decision 2026-07-30):
    retired-attributed or lineage-unreconciled rows block, while an ACTIVE
    strategy may hold non-live positions across seed convergence."""
    catalogue_sql = (_ROOT / "docker" / "seed" / "04_paper_users.sql").read_text(encoding="utf-8")
    production_guard_sql = (_ROOT / "scripts" / "db" / "production_seed_guard.sql").read_text(
        encoding="utf-8"
    )

    for sql in (catalogue_sql, production_guard_sql):
        assert "linked_broker_accounts.environment = 'live'" in sql
        assert "linked_broker_accounts.environment <> 'live'" in sql
        assert "non-zero live-account positions require manual reconciliation" in sql
        assert "SUM(CASE WHEN oi.side = 'BUY' THEN e.qty ELSE -e.qty END)" in sql
        assert "attributed to retired strategies" in sql
        assert "do not reconcile with execution fill-lineage attribution" in sql
        assert "RAISE EXCEPTION" in sql
        assert "RAISE WARNING" not in sql


def test_strategy_retirement_migrations_block_all_positions() -> None:
    paths = (*_RETIREMENT_MIGRATION_PATHS, _QUANTILE_QUARANTINE_MIGRATION)

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "nonzero_position_count INTEGER" in source
        assert "WHERE positions.qty <> 0;" in source
        assert "linked_broker_accounts.environment" not in source
        assert "% positions require reconciliation before strategy" in source


def test_seed_paths_create_no_candidate_execution_authority() -> None:
    paths = (
        _ROOT / "docker" / "seed" / "04_paper_users.sql",
        _ROOT / "scripts" / "db" / "production_seed_guard.sql",
    )
    forbidden_fragments = (
        "disabled_candidate",
        "research_bindings",
        "INSERT INTO strategies",
        "INSERT INTO strategy_versions",
        "INSERT INTO user_strategy_bindings",
        "INSERT INTO user_strategy_configs",
        "1107",
    )

    for path in paths:
        sql = path.read_text(encoding="utf-8")
        for fragment in forbidden_fragments:
            assert fragment not in sql


def test_quantile_quarantine_is_fail_closed_and_irreversible() -> None:
    source = _QUANTILE_QUARANTINE_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0047_quarantine_quantile_v1_2"' in source
    assert 'down_revision = "0046_quarantine_vrpt_v1_2"' in source
    assert source.count('"quantile_channel_v1"') == 1
    assert "LEFT JOIN user_strategy_bindings bindings" in source
    assert "LEFT JOIN decision_contexts context" in source
    assert "bindings.strategy_id IS NULL" in source
    assert "AND context.strategy_id IS NULL" in source
    assert "Cannot quarantine" in source

    downgrade = source[source.index("def downgrade()") :]
    assert "_converge_quarantined_state()" in downgrade
    assert "is_active = TRUE" not in downgrade
    assert "autopilot = TRUE" not in downgrade


def test_quantile_retirement_is_exact_and_irreversible() -> None:
    seed = (_ROOT / "docker" / "seed" / "04_paper_users.sql").read_text(encoding="utf-8")
    guard = (_ROOT / "scripts" / "db" / "production_seed_guard.sql").read_text(encoding="utf-8")
    migration = _QUANTILE_RETIREMENT_MIGRATION.read_text(encoding="utf-8")

    for source in (seed, guard):
        assert source.count("('quantile_channel_v1')") == 1

    assert '_RETIRED_STRATEGY_ID = "quantile_channel_v1"' in migration
    assert 'revision = "0048_retire_quantile_v1_2"' in migration
    assert 'down_revision = "0047_quarantine_quantile_v1_2"' in migration
    assert migration.count('"quantile_channel_v1"') == 1
    assert "Cannot retire" in migration
    assert "LEFT JOIN decision_contexts context" in migration
    assert "positions.qty <> 0" in migration
    assert "linked_broker_accounts.environment" not in migration
    assert "UPDATE strategy_parameter_feedback feedback" in migration

    downgrade = migration[migration.index("def downgrade()") :]
    assert "_converge_retired_state()" in downgrade
    assert "is_active = TRUE" not in downgrade
    assert "autopilot = TRUE" not in downgrade


def test_vrpt_retirement_is_exact_and_irreversible() -> None:
    seed = (_ROOT / "docker" / "seed" / "04_paper_users.sql").read_text(encoding="utf-8")
    guard = (_ROOT / "scripts" / "db" / "production_seed_guard.sql").read_text(encoding="utf-8")
    migration = _VRPT_RETIREMENT_MIGRATION.read_text(encoding="utf-8")

    for source in (seed, guard):
        assert source.count("('vortex_rsi_profit_target_v1')") == 1

    assert '_RETIRED_STRATEGY_ID = "vortex_rsi_profit_target_v1"' in migration
    assert "binding.strategy_id = '{_RETIRED_STRATEGY_ID}'" in migration
    assert "SET is_active = FALSE, autopilot = FALSE, updated_at = NOW()" in migration
    assert 'revision = "0046_quarantine_vrpt_v1_2"' in migration
    assert 'down_revision = "0045_retire_stat_reg_channel"' in migration
    assert migration.count('"vortex_rsi_profit_target_v1"') == 1
    assert "Cannot retire" in migration
    assert "UPDATE strategy_parameter_feedback feedback" in migration
    assert "positions.qty <> 0" in migration
    assert "linked_broker_accounts.environment" not in migration

    downgrade = migration[migration.index("def downgrade()") :]
    assert "_converge_retired_state()" in downgrade
    assert "is_active = TRUE" not in downgrade
    assert "autopilot = TRUE" not in downgrade


def test_production_guard_converges_retired_strategy_state_fail_closed() -> None:
    sql = (_ROOT / "scripts" / "db" / "production_seed_guard.sql").read_text(encoding="utf-8")

    assert "CREATE TEMP TABLE vm_production_retired_strategy_ids" in sql
    for strategy_id in _CURRENT_RETIREMENT_IDS:
        assert sql.count(f"('{strategy_id}')") == 1
    assert "Expected 18 retired strategy IDs" in sql
    assert "Cannot converge retired strategy code" in sql
    assert "Production bootstrap left % active retired-strategy records" in sql
    for table in (
        "user_strategy_bindings",
        "user_strategy_configs",
        "strategy_versions",
        "strategies",
    ):
        assert table in sql


def test_production_guard_grants_no_native_execution_authority() -> None:
    sql = (_ROOT / "scripts" / "db" / "production_seed_guard.sql").read_text(encoding="utf-8")

    assert "WHERE is_active OR autopilot;" in sql
    assert "entries_enabled = FALSE" in sql
    assert "exits_enabled = FALSE" in sql
    assert "Production bootstrap left % native execution-authority record(s) active" in sql
    assert "SET status = 'deprecated'" in sql
    assert "SET is_active = TRUE" not in sql
    assert "autopilot = TRUE" not in sql


def test_retirement_paths_expire_only_actionable_parameter_feedback() -> None:
    paths = (
        _ROOT / "docker" / "seed" / "04_paper_users.sql",
        _ROOT / "scripts" / "db" / "production_seed_guard.sql",
        *_RETIREMENT_MIGRATION_PATHS,
    )

    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "UPDATE strategy_parameter_feedback feedback" in source
        assert "feedback.status IN ('pending', 'approved')" in source
        assert "[strategy-retirement]" in source
        assert "actionable" in source.lower()
        assert (
            source.index("UPDATE strategies strategy")
            < source.index("UPDATE strategy_versions version")
            < source.index("UPDATE strategy_parameter_feedback feedback")
        )


def test_retirement_migration_uses_the_authoritative_strategy_set() -> None:
    source = (
        _ROOT / "scripts" / "db" / "alembic" / "versions" / "0043_retire_vortex_trend_capture.py"
    ).read_text(encoding="utf-8")
    assert "vm_migration_retired_strategy_ids" in source
    for strategy_id in _RETIREMENT_IDS_0043:
        assert source.count(f'"{strategy_id}"') == 1


def test_single_strategy_retirement_migrations_are_exact_and_irreversible() -> None:
    expected = (
        (
            _RETIREMENT_MIGRATION_PATHS[1],
            "0044_retire_ma_slope",
            "0043_retire_vortex_trend_capture",
            "ma_slope_v1",
        ),
        (
            _RETIREMENT_MIGRATION_PATHS[2],
            "0045_retire_stat_reg_channel",
            "0044_retire_ma_slope",
            "stat_validated_reg_channel_breakout_v1",
        ),
        (
            _EDM_RETIREMENT_MIGRATION,
            "0049_retire_enhanced_dual_v1",
            "0048_retire_quantile_v1_2",
            "enhanced_dual_momentum_v1",
        ),
        (
            _RETIREMENT_MIGRATION_PATHS[6],
            "0050_retire_vol_reversal_v1",
            "0049_retire_enhanced_dual_v1",
            "volatility_reversal_v1",
        ),
        (
            _RETIREMENT_MIGRATION_PATHS[7],
            "0098_retire_liquidity_leaders",
            "0097_binding_entry_cash_buffer",
            "liquidity_leaders_basket_v1",
        ),
    )

    for path, revision, down_revision, strategy_id in expected:
        source = path.read_text(encoding="utf-8")
        assert f'revision = "{revision}"' in source
        assert f'down_revision = "{down_revision}"' in source
        assert source.count(f'"{strategy_id}"') == 1
        assert all(
            retired_id == strategy_id or retired_id not in source
            for retired_id in _CURRENT_RETIREMENT_IDS
        )

        downgrade = source[source.index("def downgrade()") :]
        assert "_converge_retired_state()" in downgrade
        assert "is_active = TRUE" not in downgrade


def test_single_strategy_retirement_migrations_fail_closed_before_deactivation() -> None:
    for path in _RETIREMENT_MIGRATION_PATHS[1:]:
        source = path.read_text(encoding="utf-8")
        gate = source[source.index("SELECT\n                (SELECT COUNT(*)") :]

        for work_table in (
            "execution_logs",
            "pending_orders",
            "execution_decision_logs",
            "outbox_events",
        ):
            assert f"FROM {work_table}" in gate
        assert "payload::JSONB ->> 'strategy_id'" in gate
        assert "payload::JSONB #>> '{{signal,strategy_id}}'" in gate
        assert "'pending', 'in_progress', 'failed', 'dead_letter'" in gate
        assert "LEFT JOIN user_strategy_bindings bindings" in gate
        assert "LEFT JOIN decision_contexts context" in gate
        assert "bindings.strategy_id IS NULL" in gate
        assert "AND context.strategy_id IS NULL" in gate
        assert "positions.qty <> 0" in gate
        assert "linked_broker_accounts.environment" not in gate
        assert source.index("INTO unresolved_count") < source.index(
            "UPDATE user_strategy_bindings binding"
        )

        for surface in (
            "user_strategy_bindings binding",
            "user_strategy_configs config",
            "strategies strategy",
            "strategy_versions version",
            "strategy_parameter_feedback feedback",
        ):
            assert f"UPDATE {surface}" in source
