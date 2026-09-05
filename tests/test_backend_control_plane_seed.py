"""Source contracts for the local-paper control-plane seed."""

from __future__ import annotations

from pathlib import Path

_SEED = (
    Path(__file__).resolve().parents[1] / "docker" / "seed" / "03_e2e_test_user.sql"
).read_text(encoding="utf-8")


def test_quality_compounder_has_no_guessed_account_or_binding() -> None:
    assert "'local-paper-sp500:demo_user'" not in _SEED
    assert "'S&P 500 rotation paper'" not in _SEED
    assert "'us_quality_compounder_v1'" not in _SEED


def test_seed_contains_exact_user_risk_mandate() -> None:
    assert "rules::jsonb = '{\"max_drawdown_pct\":0.20}'::jsonb" in _SEED
    assert "WHERE NOT EXISTS (" in _SEED


def test_local_seed_never_grants_ibkr_or_live_authority() -> None:
    assert "'ibkr'" not in _SEED
    assert "'live'" not in _SEED
