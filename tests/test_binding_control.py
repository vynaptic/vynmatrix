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


def test_the_patchable_set_is_the_mode_the_bounded_numbers_and_the_instrument_scope() -> None:
    """Nothing that carries identity or tenancy is reachable from a patch.

    The instrument scope is, because two strategies whose declared universes
    overlap can share an account only once the owner narrows one of them. Every
    other scope dimension stays where the CLI put it.
    """
    assert {"mode", "max_open_positions", *NUMERIC_FIELDS, "instruments_allowed"} == PATCHABLE
    for forbidden in (
        "user_id",
        "binding_id",
        "strategy_id",
        "broker_account_id",
        "asset_classes_allowed",
        "sectors_allowed",
        "allowed_brokers",
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


# ------------------------------------------------------------- universe scope


@pytest.fixture
def universe_session() -> Any:
    """One owner account, a small catalogue, and strategies declaring universes.

    ``btc_v1`` and ``eth_v1`` are disjoint; ``majors_v1`` shares BTCUSDC with
    ``btc_v1``. ``quality_v1`` and ``value_v1`` declare equity membership
    universes, not lists; ``etf_v1`` declares an ETF one. ``apple_v1`` lists one
    equity, AAPL. Each strategy's catalogue row carries its declared asset class.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from lib_application.db.models import (
        Base,
        Broker,
        Instrument,
        LinkedBrokerAccount,
        Strategy,
        StrategyVersion,
        User,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)()
    declared = {
        "btc_v1": {"universe": "BTC-USDC", "asset_class": "crypto"},
        "eth_v1": {"universe": ["ETHUSDC"], "asset_class": "crypto"},
        "majors_v1": {"universe": "BTCUSDC, SOLUSDC,BTCUSDC", "asset_class": "crypto"},
        "quality_v1": {
            "universe": "SP500",
            "universe_contract": "point_in_time_sp500_membership",
            "asset_class": "equity",
        },
        "value_v1": {
            "universe": "SP500",
            "universe_contract": "point_in_time_sp500_membership",
            "asset_class": "equity",
        },
        "etf_v1": {"universe_contract": "listed_us_etfs", "asset_class": "etf"},
        "apple_v1": {"universe": "AAPL", "asset_class": "equity"},
        "unknown_v1": {"universe": "DOGEUSDC", "asset_class": "crypto"},
    }
    catalogue = {
        "BTCUSDC": ("crypto", "USDC"),
        "ETHUSDC": ("crypto", "USDC"),
        "SOLUSDC": ("crypto", "USDC"),
        "AAPL": ("equity", "USD"),
        "MSFT": ("equity", "USD"),
        "SPY": ("etf", "USD"),
    }
    session.add_all(
        [
            User(user_id="owner", email="owner@example.invalid", base_ccy="EUR"),
            Broker(broker_id=6, code="paper", name="Paper"),
            *(
                Instrument(
                    instr_id=number,
                    asset_class=asset_class,
                    canonical=symbol,
                    settlement_currency=currency,
                )
                for number, (symbol, (asset_class, currency)) in enumerate(
                    catalogue.items(), start=1
                )
            ),
            *(
                Strategy(
                    strategy_id=strategy_id,
                    strategy_name=strategy_id,
                    asset_class=params["asset_class"],
                    is_active=True,
                )
                for strategy_id, params in declared.items()
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            *(
                StrategyVersion(
                    strategy_id=strategy_id,
                    semver="1.0.0",
                    param_schema={},
                    default_params=params,
                    status="active",
                )
                for strategy_id, params in declared.items()
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
        ]
    )
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _create(session: Any, strategy_id: str, mode: str = "trading") -> Any:
    from lib_application.services.binding_control import create_binding

    row = create_binding(
        session, owner_id="owner", strategy_id=strategy_id, broker_account_id=1, mode=mode
    )
    session.commit()
    return row


def test_a_declared_universe_resolves_to_catalogue_symbols(universe_session: Any) -> None:
    from lib_application.services.binding_control import declared_universe

    # A normalized form, a JSON array, and a padded comma string with a repeat.
    assert declared_universe(universe_session, "btc_v1") == ["BTCUSDC"]
    assert declared_universe(universe_session, "eth_v1") == ["ETHUSDC"]
    assert declared_universe(universe_session, "majors_v1") == ["BTCUSDC", "SOLUSDC"]


def test_a_universe_that_is_not_a_fixed_list_is_every_instrument(universe_session: Any) -> None:
    """A membership universe cannot be pinned, so it keeps the unscoped default."""
    from lib_application.db.models import Strategy
    from lib_application.services.binding_control import declared_universe

    universe_session.add(Strategy(strategy_id="bare_v1", strategy_name="bare", is_active=True))
    universe_session.commit()

    assert declared_universe(universe_session, "quality_v1") is None
    assert declared_universe(universe_session, "bare_v1") is None  # no version at all


def test_the_active_release_wins_over_a_newer_unreleased_version(universe_session: Any) -> None:
    from lib_application.db.models import StrategyVersion
    from lib_application.services.binding_control import declared_universe

    universe_session.add(
        StrategyVersion(
            strategy_id="btc_v1",
            semver="2.0.0",
            param_schema={},
            default_params={"universe": "ETHUSDC"},
            status="registered",
        )
    )
    universe_session.commit()

    assert declared_universe(universe_session, "btc_v1") == ["BTCUSDC"]


def test_an_instrument_the_catalogue_lacks_refuses_the_binding(universe_session: Any) -> None:
    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "unknown_v1", mode="off")

    assert raised.value.status_code == 422
    assert "unknown_v1" in raised.value.detail
    assert "DOGEUSDC" in raised.value.detail


def test_a_binding_takes_its_strategys_declared_asset_class(universe_session: Any) -> None:
    crypto = _create(universe_session, "btc_v1", mode="off")
    quality = _create(universe_session, "quality_v1", mode="off")
    etf = _create(universe_session, "etf_v1", mode="off")

    assert list(crypto.asset_classes_allowed) == ["crypto"]
    assert (quality.instruments_allowed, list(quality.asset_classes_allowed)) == (None, ["equity"])
    # An ETF strategy is bounded to ETFs; it is never coerced to equity.
    assert (etf.instruments_allowed, list(etf.asset_classes_allowed)) == (None, ["etf"])


def test_a_strategy_that_declares_no_asset_class_is_refused(universe_session: Any) -> None:
    """With no instrument list, the asset class is the only bound, so none is guessed."""
    from lib_application.db.models import Strategy

    universe_session.add(Strategy(strategy_id="bare_v1", strategy_name="bare", is_active=True))
    universe_session.commit()

    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "bare_v1", mode="off")

    assert raised.value.status_code == 422
    assert "bare_v1 declares no asset class" in raised.value.detail


def test_shared_instruments_bounds_a_scope_without_a_list_by_its_asset_classes(
    universe_session: Any,
) -> None:
    from lib_application.services.binding_control import (
        BindingScope,
        scopes_overlap,
        shared_instruments,
    )

    def crypto(*symbols: str) -> BindingScope:
        return BindingScope.of(list(symbols), ["crypto"])

    equities = BindingScope.of(None, ["equity"])
    session = universe_session

    # Two lists share what both name, after catalogue resolution.
    assert shared_instruments(session, crypto("BTCUSDC"), crypto("ETHUSDC")) == []
    assert shared_instruments(session, crypto("SOLUSDC", "BTCUSDC"), crypto("BTC-USDC")) == [
        "BTCUSDC"
    ]
    # A class scope shares a list's instruments of that class, and only those.
    assert shared_instruments(session, equities, crypto("BTCUSDC", "ETHUSDC")) == []
    assert shared_instruments(session, equities, crypto("BTCUSDC", "AAPL")) == ["AAPL"]
    # Two class scopes share an open-ended set when their classes meet.
    assert shared_instruments(session, equities, BindingScope.of(None, ["etf", "equity"])) is None
    assert shared_instruments(session, equities, BindingScope.of(None, ["crypto"])) == []
    assert shared_instruments(session, equities, BindingScope.of(None, ["etf"])) == []
    # A class list that bounds nothing -- empty, or not canonical -- is every instrument.
    for unbounded in ([], ["Equity"], ["equity", "stocks"]):
        scope = BindingScope.of(None, unbounded)
        assert scope.asset_classes is None
        assert shared_instruments(session, scope, crypto("ETHUSDC")) == ["ETHUSDC"]
        assert shared_instruments(session, scope, equities) is None
    assert scopes_overlap(session, equities, crypto("BTCUSDC")) is False
    assert scopes_overlap(session, equities, BindingScope.of(None, ["equity"])) is True
    assert scopes_overlap(session, BindingScope.of([], []), crypto("ETHUSDC")) is True


def test_two_strategies_with_disjoint_universes_both_trade_one_account(
    universe_session: Any,
) -> None:
    btc = _create(universe_session, "btc_v1")
    eth = _create(universe_session, "eth_v1")

    assert (btc.is_active, list(btc.instruments_allowed)) == (True, ["BTCUSDC"])
    assert (eth.is_active, list(eth.instruments_allowed)) == (True, ["ETHUSDC"])


def test_an_overlapping_pair_is_refused_naming_the_strategy_and_the_instrument(
    universe_session: Any,
) -> None:
    _create(universe_session, "btc_v1")

    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "majors_v1")

    assert raised.value.status_code == 409
    assert "btc_v1 is already switched on for BTCUSDC" in raised.value.detail
    assert "SOLUSDC" not in raised.value.detail  # only what the two actually share


@pytest.mark.parametrize("first", ["btc_v1", "quality_v1"])
def test_a_membership_universe_equity_strategy_and_a_crypto_strategy_both_trade(
    universe_session: Any, first: str
) -> None:
    """The point of the asset-class rule: quality investing beside crypto, one account."""
    second = "quality_v1" if first == "btc_v1" else "btc_v1"

    rows = {name: _create(universe_session, name) for name in (first, second)}

    assert all(mode_of(row) == "trading" for row in rows.values())
    assert rows["quality_v1"].instruments_allowed is None


def test_an_etf_strategy_and_an_equity_strategy_both_trade_one_account(
    universe_session: Any,
) -> None:
    quality = _create(universe_session, "quality_v1")
    etf = _create(universe_session, "etf_v1")

    assert (mode_of(quality), mode_of(etf)) == ("trading", "trading")


def test_a_membership_universe_conflicts_with_a_strategy_listing_its_asset_class(
    universe_session: Any,
) -> None:
    _create(universe_session, "quality_v1")

    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "apple_v1")

    assert raised.value.status_code == 409
    assert "quality_v1 is already switched on for AAPL" in raised.value.detail


def test_two_membership_universes_of_one_asset_class_still_conflict(
    universe_session: Any,
) -> None:
    _create(universe_session, "quality_v1")

    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "value_v1")

    assert raised.value.status_code == 409
    assert "quality_v1 is already switched on for every equity instrument" in raised.value.detail


def test_a_binding_its_asset_classes_do_not_bound_still_conflicts_with_everything(
    universe_session: Any,
) -> None:
    """A stored class list that is empty or not canonical bounds nothing, as in SQL."""
    from lib_application.db.models import Strategy, UserStrategyBinding

    universe_session.add(
        Strategy(strategy_id="legacy_v1", strategy_name="legacy", asset_class="crypto")
    )
    universe_session.flush()
    universe_session.add(
        UserStrategyBinding(
            user_id="owner",
            strategy_id="legacy_v1",
            broker_account_id=1,
            asset_classes_allowed=["stocks"],
            **MODES["trading"].as_fields(),
        )
    )
    universe_session.commit()

    with pytest.raises(BindingControlError) as raised:
        _create(universe_session, "quality_v1")

    assert raised.value.status_code == 409
    assert (
        "legacy_v1 is already switched on for every instrument, "
        "and this binding covers every equity instrument"
    ) in raised.value.detail


def test_switching_a_prepared_binding_on_applies_the_same_overlap_rule(
    universe_session: Any,
) -> None:
    from lib_application.services.binding_control import patch_binding

    _create(universe_session, "btc_v1")
    eth = _create(universe_session, "eth_v1", mode="off")
    majors = _create(universe_session, "majors_v1", mode="off")

    with pytest.raises(BindingControlError) as raised:
        patch_binding(
            universe_session,
            owner_id="owner",
            binding_id=int(majors.binding_id),
            expected={"mode": "off"},
            changes={"mode": "trading"},
        )
    assert raised.value.status_code == 409
    universe_session.rollback()

    switched = patch_binding(
        universe_session,
        owner_id="owner",
        binding_id=int(eth.binding_id),
        expected={"mode": "off"},
        changes={"mode": "trading"},
    )
    assert mode_of(switched) == "trading"
    universe_session.commit()

    # Switching on reads the stored asset classes too: equity beside crypto.
    quality = _create(universe_session, "quality_v1", mode="off")
    switched = patch_binding(
        universe_session,
        owner_id="owner",
        binding_id=int(quality.binding_id),
        expected={"mode": "off"},
        changes={"mode": "trading"},
    )
    assert mode_of(switched) == "trading"


# ------------------------------------------------------------- scope patches


def _fill(
    session: Any, *, number: int, strategy: str, instr: int, side: str, qty: str = "0.5"
) -> None:
    """One exact ledger fill for ``strategy`` on the owner's account."""
    from datetime import datetime

    from lib_application.db.models import Execution, Order, OrderIntent

    session.add(
        OrderIntent(
            intent_id=number,
            user_id="owner",
            account_id=1,
            strategy_id=strategy,
            canonical_signal_id=number,
            side=side,
            execution_mode="spot",
            broker_environment="paper",
            method="SPOT",
            payload={},
            status="routed",
        )
    )
    session.add(
        Order(
            order_id=number,
            intent_id=number,
            broker_id=6,
            account_id=1,
            settlement_currency="USDC",
            client_order_id=f"client-{number}",
            state="filled",
        )
    )
    session.add(
        Execution(
            exec_id=number,
            order_id=number,
            instr_id=instr,
            fill_ts=datetime(2026, 10, 1, 12, number),
            qty=Decimal(qty),
            price=Decimal("100"),
            fee_ccy="USDC",
            fee_amount=Decimal("0"),
            venue="paper",
            trade_id=f"trade-{number}",
        )
    )
    session.commit()


def _scope_patch(
    session: Any, binding: Any, *, expected: dict[str, Any], changes: dict[str, Any]
) -> Any:
    from lib_application.services.binding_control import patch_binding

    row = patch_binding(
        session,
        owner_id="owner",
        binding_id=int(binding.binding_id),
        expected=expected,
        changes=changes,
    )
    session.commit()
    return row


def test_narrowing_one_binding_lets_two_overlapping_universes_trade_one_account(
    universe_session: Any,
) -> None:
    """The patch that narrows the scope may also switch the binding on, in one step."""
    _create(universe_session, "btc_v1")
    majors = _create(universe_session, "majors_v1", mode="off")

    narrowed = _scope_patch(
        universe_session,
        majors,
        # The expected value is a set: order and broker aliases do not matter.
        expected={"mode": "off", "instruments_allowed": ["SOLUSDC", "BTC-USDC"]},
        changes={"mode": "trading", "instruments_allowed": ["sol-usdc"]},
    )

    assert (mode_of(narrowed), list(narrowed.instruments_allowed)) == ("trading", ["SOLUSDC"])


def test_widening_an_active_binding_into_another_strategys_instrument_is_refused(
    universe_session: Any,
) -> None:
    _create(universe_session, "btc_v1")
    majors = _create(universe_session, "majors_v1", mode="off")
    _scope_patch(
        universe_session,
        majors,
        expected={"mode": "off", "instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes={"mode": "trading", "instruments_allowed": ["SOLUSDC"]},
    )

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            majors,
            expected={"instruments_allowed": ["SOLUSDC"]},
            changes={"instruments_allowed": ["SOLUSDC", "BTCUSDC"]},
        )

    assert raised.value.status_code == 409
    assert "btc_v1 is already switched on for BTCUSDC" in raised.value.detail
    assert "narrow one of the two scopes" in raised.value.detail


def test_a_scope_patch_into_an_equity_meets_a_list_free_equity_strategy(
    universe_session: Any,
) -> None:
    """Asset classes bound the other binding when a patch widens this one's list."""
    _create(universe_session, "quality_v1")
    btc = _create(universe_session, "btc_v1")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            btc,
            expected={"instruments_allowed": ["BTCUSDC"]},
            changes={"instruments_allowed": ["BTCUSDC", "AAPL"]},
        )

    assert raised.value.status_code == 409
    assert "quality_v1 is already switched on for AAPL" in raised.value.detail


def test_an_inactive_binding_may_take_an_overlapping_scope_it_cannot_switch_on(
    universe_session: Any,
) -> None:
    """Only authority conflicts; the overlap rule runs again when it is switched on."""
    _create(universe_session, "btc_v1")
    eth = _create(universe_session, "eth_v1", mode="off")

    widened = _scope_patch(
        universe_session,
        eth,
        expected={"instruments_allowed": ["ETHUSDC"]},
        changes={"instruments_allowed": ["ETHUSDC", "BTCUSDC"]},
    )
    assert list(widened.instruments_allowed) == ["ETHUSDC", "BTCUSDC"]

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(universe_session, eth, expected={"mode": "off"}, changes={"mode": "trading"})
    assert raised.value.status_code == 409


@pytest.mark.parametrize(
    "value",
    [[], None, "SOLUSDC", ["SOLUSDC", " "]],
    ids=["empty", "every-instrument", "not-a-list", "blank-entry"],
)
def test_a_scope_change_must_be_a_non_empty_list_of_instruments(
    universe_session: Any, value: Any
) -> None:
    majors = _create(universe_session, "majors_v1", mode="off")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            majors,
            expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
            changes={"instruments_allowed": value},
        )

    assert raised.value.status_code == 422


def test_a_scope_naming_an_instrument_the_catalogue_lacks_is_refused(
    universe_session: Any,
) -> None:
    majors = _create(universe_session, "majors_v1", mode="off")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            majors,
            expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
            changes={"instruments_allowed": ["DOGEUSDC"]},
        )

    assert raised.value.status_code == 422
    assert "DOGEUSDC" in raised.value.detail


def test_a_stale_expected_scope_is_refused_but_a_repeated_patch_is_not(
    universe_session: Any,
) -> None:
    majors = _create(universe_session, "majors_v1", mode="off")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            majors,
            expected={"instruments_allowed": ["BTCUSDC"]},
            changes={"instruments_allowed": ["SOLUSDC"]},
        )
    assert raised.value.status_code == 409
    assert raised.value.detail == "stale expected value for instruments_allowed"
    universe_session.rollback()

    change = {"instruments_allowed": ["SOLUSDC"]}
    _scope_patch(
        universe_session,
        majors,
        expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes=change,
    )
    # A retry of the same patch finds the value it wanted and is not stale.
    again = _scope_patch(
        universe_session,
        majors,
        expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes=change,
    )
    assert list(again.instruments_allowed) == ["SOLUSDC"]


def test_dropping_an_instrument_the_strategy_still_holds_is_refused(
    universe_session: Any,
) -> None:
    """Its close signals would no longer reach the binding, so the position would strand."""
    majors = _create(universe_session, "majors_v1", mode="close_only")
    _fill(universe_session, number=1, strategy="majors_v1", instr=3, side="BUY")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            majors,
            expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
            changes={"instruments_allowed": ["BTCUSDC"]},
        )

    assert raised.value.status_code == 409
    assert "majors_v1 still holds an open position in SOLUSDC" in raised.value.detail
    assert "keep it in the scope until the position is closed" in raised.value.detail
    universe_session.rollback()

    # Narrowing away an instrument it does not hold is still allowed.
    kept = _scope_patch(
        universe_session,
        majors,
        expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes={"instruments_allowed": ["SOLUSDC"]},
    )
    assert list(kept.instruments_allowed) == ["SOLUSDC"]


def test_a_short_position_or_an_unscoped_binding_holds_its_instrument_too(
    universe_session: Any,
) -> None:
    from lib_application.db.models import UserStrategyBinding

    universe_session.add(
        UserStrategyBinding(
            user_id="owner",
            strategy_id="majors_v1",
            broker_account_id=1,
            instruments_allowed=None,
            **MODES["off"].as_fields(),
        )
    )
    universe_session.commit()
    unscoped = universe_session.query(UserStrategyBinding).one()
    _fill(universe_session, number=1, strategy="majors_v1", instr=2, side="SELL")

    with pytest.raises(BindingControlError) as raised:
        _scope_patch(
            universe_session,
            unscoped,
            expected={"instruments_allowed": None},
            changes={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        )

    assert raised.value.status_code == 409
    assert "open position in ETHUSDC" in raised.value.detail


def test_a_closed_position_or_another_strategys_holding_does_not_block_a_narrowing(
    universe_session: Any,
) -> None:
    """The ledger attributes each fill to its strategy; the account total is not the test."""
    from lib_application.services.binding_control import held_instruments

    majors = _create(universe_session, "majors_v1", mode="off")
    _fill(universe_session, number=1, strategy="majors_v1", instr=3, side="BUY")
    _fill(universe_session, number=2, strategy="majors_v1", instr=3, side="SELL")
    _fill(universe_session, number=3, strategy="btc_v1", instr=1, side="BUY")

    assert held_instruments(universe_session, broker_account_id=1, strategy_id="majors_v1") == []
    assert held_instruments(universe_session, broker_account_id=1, strategy_id="btc_v1") == [
        "BTCUSDC"
    ]
    narrowed = _scope_patch(
        universe_session,
        majors,
        expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes={"instruments_allowed": ["ETHUSDC"]},
    )
    assert list(narrowed.instruments_allowed) == ["ETHUSDC"]


def test_a_scope_patch_is_audited_by_field_name_never_by_value(universe_session: Any) -> None:
    from lib_application.db.models import ApiAuditLog

    majors = _create(universe_session, "majors_v1", mode="off")
    _scope_patch(
        universe_session,
        majors,
        expected={"instruments_allowed": ["BTCUSDC", "SOLUSDC"]},
        changes={"instruments_allowed": ["SOLUSDC"]},
    )

    audit = universe_session.query(ApiAuditLog).filter(ApiAuditLog.action == "binding.patch").one()
    assert audit.req == {"fields": ["instruments_allowed"]}
    assert "SOLUSDC" not in str(audit.req)
