"""Unit tests for the binding authority rules the owner UI and the API share.

The most important test here is the first one: it evaluates the three CHECK
constraints from ``user_strategy_bindings`` directly against every mode, which
is what makes "three named modes" a safety property rather than a UI
convenience.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from lib_application.services.binding_control import (
    CUSTOM_MODE,
    MAX_OPEN_POSITIONS,
    MODES,
    NUMERIC_FIELDS,
    PATCHABLE,
    BindingControlError,
    TradingState,
    mode_of,
    trading_state,
    validate_numeric,
)

# The three authority predicates, transcribed from ``control_plane.py``'s
# ``__table_args__`` (added by migration 0077_execution_safety).
CHECKS = {
    "ck_binding_inactive_has_no_authority": (
        lambda f: f["is_active"] or (not f["entries_enabled"] and not f["exits_enabled"])
    ),
    "ck_binding_active_strategy_required": (
        lambda f: not f["is_active"] or bool(f["strategy_id"] and f["strategy_id"].strip())
    ),
    "ck_binding_entries_require_autopilot": (lambda f: f["autopilot"] or not f["entries_enabled"]),
}


@pytest.mark.parametrize("name", sorted(MODES))
def test_every_mode_satisfies_every_authority_check(name: str) -> None:
    """A mode cannot build a row PostgreSQL would reject."""
    fields = {**MODES[name].as_fields(), "strategy_id": "swing_v1"}

    for check, predicate in CHECKS.items():
        assert predicate(fields) is True, (name, check)


def test_the_modes_are_the_only_combinations_offered() -> None:
    assert set(MODES) == {"off", "close_only", "trading"}
    # Off is the only one without authority; the other two are active.
    assert MODES["off"].is_active is False
    assert MODES["close_only"].is_active is True
    assert MODES["trading"].is_active is True
    # Close only is the deliberate asymmetry: exits without autopilot is legal,
    # entries without autopilot is not.
    assert (MODES["close_only"].exits_enabled, MODES["close_only"].entries_enabled) == (True, False)
    assert MODES["trading"].autopilot is True


def test_an_illegal_combination_would_fail_a_check_if_it_were_offered() -> None:
    """Proves the checks above have teeth rather than passing vacuously."""
    entries_without_autopilot = {
        "is_active": True,
        "entries_enabled": True,
        "exits_enabled": True,
        "autopilot": False,
        "strategy_id": "swing_v1",
    }
    authority_while_inactive = {
        "is_active": False,
        "entries_enabled": False,
        "exits_enabled": True,
        "autopilot": False,
        "strategy_id": "swing_v1",
    }
    active_without_strategy = {**MODES["trading"].as_fields(), "strategy_id": None}

    assert CHECKS["ck_binding_entries_require_autopilot"](entries_without_autopilot) is False
    assert CHECKS["ck_binding_inactive_has_no_authority"](authority_while_inactive) is False
    assert CHECKS["ck_binding_active_strategy_required"](active_without_strategy) is False


@pytest.mark.parametrize("name", sorted(MODES))
def test_mode_of_round_trips_every_mode(name: str) -> None:
    assert mode_of(SimpleNamespace(**MODES[name].as_fields())) == name


def test_a_combination_we_do_not_offer_reads_as_custom() -> None:
    """A row the CLI set by hand is named honestly, not forced into a mode."""
    row = SimpleNamespace(
        is_active=True, entries_enabled=False, exits_enabled=False, autopilot=True
    )

    assert mode_of(row) == CUSTOM_MODE


# --------------------------------------------------------------- parameters


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_score_threshold", "0"),
        ("asset_score_threshold", "1"),
        ("max_daily_loss_pct", "0"),
        ("max_position_pct", "0.0001"),
        ("max_total_exposure_pct", "1"),
    ],
)
def test_values_the_constraint_accepts_are_accepted(field: str, value: str) -> None:
    assert validate_numeric(field, value) == Decimal(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # The asymmetry the form must respect: these two reject zero...
        ("max_position_pct", "0"),
        ("max_total_exposure_pct", "0"),
        # ...and everything rejects out-of-range.
        ("asset_score_threshold", "1.01"),
        ("asset_score_threshold", "-0.01"),
        ("max_daily_loss_pct", "2"),
        ("max_position_pct", "not a number"),
    ],
)
def test_values_the_constraint_rejects_are_refused(field: str, value: str) -> None:
    with pytest.raises(BindingControlError) as raised:
        validate_numeric(field, value)

    assert raised.value.status_code == 422


def test_zero_is_accepted_by_exactly_the_two_inclusive_fields() -> None:
    inclusive = {field for field, (_, _, exclusive) in NUMERIC_FIELDS.items() if not exclusive}

    assert inclusive == {"asset_score_threshold", "max_daily_loss_pct"}


@pytest.mark.parametrize("value", [0, -1, 1001, "many"])
def test_open_positions_outside_its_range_is_refused(value: Any) -> None:
    with pytest.raises(BindingControlError):
        validate_numeric("max_open_positions", value)


def test_open_positions_accepts_its_bounds() -> None:
    low, high = MAX_OPEN_POSITIONS
    assert validate_numeric("max_open_positions", low) == low
    assert validate_numeric("max_open_positions", high) == high


def test_the_patchable_set_is_the_mode_plus_the_bounded_numbers() -> None:
    """Nothing that carries identity, tenancy or scope is reachable from a patch."""
    assert {"mode", "max_open_positions", *NUMERIC_FIELDS} == PATCHABLE
    for forbidden in (
        "user_id",
        "binding_id",
        "strategy_id",
        "broker_account_id",
        "instruments_allowed",
        "asset_classes_allowed",
        "created_at",
        "is_active",
        "entries_enabled",
    ):
        assert forbidden not in PATCHABLE


# ------------------------------------------------------------ trading state


@pytest.fixture
def catalogue_session() -> Any:
    """An owner, a peer, and three strategies: released, registered-only, and inactive."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import (
        Base,
        Broker,
        LinkedBrokerAccount,
        Strategy,
        StrategyVersion,
        User,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    session.add_all(
        [
            User(user_id="owner", email="owner@example.invalid", base_ccy="EUR"),
            User(user_id="peer", email="peer@example.invalid", base_ccy="INR"),
            Broker(broker_id=6, code="paper", name="Paper"),
            Strategy(strategy_id="released_v1", strategy_name="Released", is_active=True),
            # is_active alone is not a release: no version is active.
            Strategy(strategy_id="flagged_v1", strategy_name="Flagged", is_active=True),
            Strategy(strategy_id="registered_v1", strategy_name="Registered"),
        ]
    )
    session.flush()
    session.add_all(
        [
            StrategyVersion(
                strategy_id="released_v1", semver="1.0.0", param_schema={}, status="active"
            ),
            StrategyVersion(
                strategy_id="flagged_v1", semver="1.0.0", param_schema={}, status="registered"
            ),
            LinkedBrokerAccount(
                account_id=1,
                display_name="Owner paper",
                user_id="owner",
                broker_id=6,
                environment="paper",
                base_ccy="EUR",
                paper_initial_equity=Decimal("1000"),
                paper_initial_cash=Decimal("1000"),
            ),
            LinkedBrokerAccount(
                account_id=2,
                display_name="Peer paper",
                user_id="peer",
                broker_id=6,
                environment="paper",
                base_ccy="INR",
                paper_initial_equity=Decimal("1000"),
                paper_initial_cash=Decimal("1000"),
            ),
        ]
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _bind(session: Any, *, user: str, account: int, strategy: str | None, mode: str) -> None:
    from lib_application.db.models import UserStrategyBinding

    session.add(
        UserStrategyBinding(
            user_id=user,
            broker_account_id=account,
            strategy_id=strategy,
            **MODES[mode].as_fields(),
        )
    )
    session.commit()


def test_trading_state_of_a_fresh_install_is_released_but_unbound(catalogue_session: Any) -> None:
    assert trading_state(catalogue_session, "owner") == TradingState(
        released=1, bound=0, switched_on=0
    )


def test_trading_state_counts_only_the_owners_bindings_and_only_active_ones_as_on(
    catalogue_session: Any,
) -> None:
    _bind(catalogue_session, user="owner", account=1, strategy="released_v1", mode="close_only")
    _bind(catalogue_session, user="owner", account=1, strategy="registered_v1", mode="off")
    # Neither a strategy-less binding nor another user's binding is the owner's trading.
    _bind(catalogue_session, user="owner", account=1, strategy=None, mode="off")
    _bind(catalogue_session, user="peer", account=2, strategy="released_v1", mode="trading")

    assert trading_state(catalogue_session, "owner") == TradingState(
        released=1, bound=2, switched_on=1
    )
