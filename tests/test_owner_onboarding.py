"""Owner initialization and profile mutation preserve authority and history."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from lib_application.db.models import (
    ApiAuditLog,
    Base,
    Broker,
    LinkedBrokerAccount,
    OutboxEvent,
    User,
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


def _profile(**kwargs: object) -> dict[str, object]:
    return {"email": "owner@example.invalid", "base_ccy": "EUR", "tz": "Europe/Amsterdam"} | kwargs


def test_init_is_explicit_and_retry_preserves_omitted_profile(session: Session) -> None:
    from lib_application.services.owner_onboarding import initialize_owner

    first = initialize_owner(session, profile=_profile(full_name="Original"))
    session.commit()
    repeated = initialize_owner(session, profile=_profile())
    assert repeated == first
    assert repeated["full_name"] == "Original"
    assert session.scalar(select(User.user_id)) == first["user_id"]


def test_existing_users_require_explicit_adoption_and_keep_ids(session: Session) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    session.add_all(
        [
            User(
                user_id="historic-a",
                email="owner@example.invalid",
                base_ccy="EUR",
                tz="Europe/Amsterdam",
            ),
            User(
                user_id="historic-b",
                email="past@example.invalid",
                base_ccy="USD",
                tz="UTC",
                status="closed",
            ),
        ]
    )
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="existing-user-id"):
        initialize_owner(session, profile=_profile())
    owner = initialize_owner(session, profile={}, existing_user_id="historic-a")
    session.commit()
    assert owner["user_id"] == "historic-a"
    assert session.get(User, "historic-b").status == "closed"
    with pytest.raises(OwnerOnboardingError, match="different owner"):
        initialize_owner(session, profile={}, existing_user_id="historic-b")


@pytest.mark.parametrize(
    "profile",
    [
        {},
        _profile(tz="Invented/Zone"),
        _profile(base_ccy="eur"),
        _profile(email="bad"),
        _profile(status="active"),
    ],
)
def test_init_validates_all_input_before_creating_owner(
    session: Session, profile: dict[str, object]
) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    with pytest.raises(OwnerOnboardingError):
        initialize_owner(session, profile=profile)
    assert session.scalar(select(User.user_id)) is None


def test_init_retry_cannot_overwrite_owner_edits(session: Session) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    owner = initialize_owner(session, profile=_profile(full_name="Original"))
    session.commit()
    session.get(User, owner["user_id"]).full_name = "Edited"
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="different"):
        initialize_owner(session, profile=_profile(full_name="Original"))
    assert session.get(User, owner["user_id"]).full_name == "Edited"


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "other"},
        {"signal": {}},
        {"user_id": "candidate", "execution_policy": {"user_id": "other"}},
    ],
)
def test_adoption_rejects_foreign_or_ambiguous_pending_outbox(session: Session, payload) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    session.add(User(user_id="candidate", **_profile()))
    session.add(
        OutboxEvent(topic="execution.commands", event_type="execution.command", payload=payload)
    )
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="outbox"):
        initialize_owner(session, profile={}, existing_user_id="candidate")
    assert session.get(User, "candidate").is_deployment_owner is False


def test_profile_patch_is_owner_relative_and_expected_value_guarded(session: Session) -> None:
    from lib_application.services.owner_onboarding import (
        OwnerOnboardingError,
        apply_owner_patch,
        initialize_owner,
    )

    owner = initialize_owner(session, profile=_profile(full_name="Original"))
    session.commit()
    result = apply_owner_patch(
        session, expected={"full_name": "Original"}, changes={"full_name": "Edited"}
    )
    session.commit()
    assert result["full_name"] == "Edited"
    assert result["email"] == owner["email"]
    with pytest.raises(OwnerOnboardingError, match="expected"):
        apply_owner_patch(
            session, expected={"full_name": "Original"}, changes={"full_name": "Overwrite"}
        )
    assert session.get(User, owner["user_id"]).full_name == "Edited"
    assert session.scalars(
        select(ApiAuditLog).where(ApiAuditLog.action == "owner.patch")
    ).one().req == {"fields": ["full_name"]}


@pytest.mark.parametrize("field", ["user_id", "is_deployment_owner", "status", "org_id"])
def test_profile_patch_rejects_authority_fields(session: Session, field: str) -> None:
    from lib_application.services.owner_onboarding import (
        OwnerOnboardingError,
        apply_owner_patch,
        initialize_owner,
    )

    initialize_owner(session, profile=_profile())
    session.commit()
    with pytest.raises(OwnerOnboardingError):
        apply_owner_patch(session, expected={field: None}, changes={field: "changed"})


def test_owner_currency_cannot_change_after_account_authority(session: Session) -> None:
    from lib_application.services.owner_onboarding import (
        OwnerOnboardingError,
        apply_owner_patch,
        initialize_owner,
    )

    owner = initialize_owner(session, profile=_profile())
    session.add(Broker(broker_id=1, code="paper", name="Paper"))
    session.add(
        LinkedBrokerAccount(
            user_id=owner["user_id"],
            broker_id=1,
            environment="paper",
            display_name="Paper",
            base_ccy="EUR",
            paper_initial_equity=100,
            paper_initial_cash=100,
        )
    )
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="currency"):
        apply_owner_patch(session, expected={"base_ccy": "EUR"}, changes={"base_ccy": "USD"})
    assert session.get(User, owner["user_id"]).base_ccy == "EUR"


def _historical_fills(session: Session, *, sell_quantity: str, method: str = "SPOT") -> None:
    from datetime import UTC, datetime
    from decimal import Decimal

    from lib_application.db.models import (
        CanonicalSignal,
        Execution,
        Instrument,
        Order,
        OrderIntent,
        Position,
        Strategy,
    )

    session.add_all(
        [
            User(user_id="candidate", **_profile()),
            User(
                user_id="foreign",
                email="foreign@example.invalid",
                base_ccy="USD",
                tz="UTC",
                status="closed",
            ),
            Broker(broker_id=1, code="paper", name="Paper"),
            Instrument(
                instr_id=1, canonical="BTC/USD", asset_class="crypto", settlement_currency="USD"
            ),
            Strategy(strategy_id="historical", strategy_name="Historical", asset_class="crypto"),
        ]
    )
    session.flush()
    session.add(
        LinkedBrokerAccount(
            account_id=1,
            user_id="foreign",
            broker_id=1,
            environment="paper",
            display_name="History",
            base_ccy="USD",
            paper_initial_equity=100,
            paper_initial_cash=100,
        )
    )
    session.add(
        CanonicalSignal(
            signal_id=1,
            strategy_id="historical",
            instr_id=1,
            action="long",
            external_signal_id="historical-signal",
        )
    )
    session.flush()
    for number, side, quantity in [(1, "BUY", "1"), (2, "SELL", sell_quantity)]:
        session.add(
            OrderIntent(
                intent_id=number,
                user_id="foreign",
                account_id=1,
                strategy_id="historical",
                canonical_signal_id=1,
                side=side,
                execution_mode="spot",
                broker_environment="paper",
                method=method,
                payload={"symbol": "BTC/USD"},
                status="routed",
            )
        )
        session.flush()
        session.add(
            Order(
                order_id=number,
                intent_id=number,
                broker_id=1,
                account_id=1,
                settlement_currency="USD",
                state="filled",
            )
        )
        session.flush()
        session.add(
            Execution(
                order_id=number,
                instr_id=1,
                fill_ts=datetime.now(UTC),
                qty=Decimal(quantity),
                price=10,
                fee_ccy="USD",
                fee_amount=0,
                venue="paper",
                trade_id=f"fill-{number}",
            )
        )
    session.add(
        Position(
            account_id=1,
            instr_id=1,
            qty=Decimal("0") if sell_quantity != "1" else Decimal("99"),
            gross_notional=990,
            notional_currency="USD",
        )
    )
    session.commit()


def test_adoption_rejects_open_canonical_exposure_despite_empty_projection(
    session: Session,
) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    _historical_fills(session, sell_quantity="0.75")
    with pytest.raises(OwnerOnboardingError, match=r"canonical.*exposure"):
        initialize_owner(session, profile={}, existing_user_id="candidate")
    assert session.get(User, "candidate").is_deployment_owner is False


def test_adoption_preserves_closed_spot_ledger_and_ignores_projection(session: Session) -> None:
    from lib_application.db.models import Execution, Position
    from lib_application.services.owner_onboarding import initialize_owner

    _historical_fills(session, sell_quantity="1")
    owner = initialize_owner(session, profile={}, existing_user_id="candidate")
    assert owner["user_id"] == "candidate"
    assert len(session.scalars(select(Execution)).all()) == 2
    assert session.scalars(select(Position)).one().qty == 99


def test_adoption_refuses_non_spot_history_without_contract_reconciliation(
    session: Session,
) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    _historical_fills(session, sell_quantity="1", method="FUTURES")
    with pytest.raises(OwnerOnboardingError, match=r"non-SPOT.*reconciliation"):
        initialize_owner(session, profile={}, existing_user_id="candidate")


def test_adoption_rejects_foreign_nonterminal_order_even_with_flat_fills(session: Session) -> None:
    from lib_application.db.models import Order
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    _historical_fills(session, sell_quantity="1")
    session.get(Order, 2).state = "working"
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="orders"):
        initialize_owner(session, profile={}, existing_user_id="candidate")


def test_adoption_rejects_foreign_pending_decision(session: Session) -> None:
    from lib_application.db.models import ExecutionDecisionLog
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    session.add_all(
        [
            User(user_id="candidate", **_profile()),
            User(user_id="foreign", email="other@example.invalid", base_ccy="EUR", tz="UTC"),
        ]
    )
    session.add(
        ExecutionDecisionLog(user_id="foreign", idempotency_key="foreign-pending", status="pending")
    )
    session.commit()
    with pytest.raises(OwnerOnboardingError, match="decisions"):
        initialize_owner(session, profile={}, existing_user_id="candidate")


def test_pending_outbox_cannot_hide_foreign_account_under_selected_user(session: Session) -> None:
    from lib_application.services.owner_onboarding import OwnerOnboardingError, initialize_owner

    session.add_all(
        [
            User(user_id="candidate", **_profile()),
            User(user_id="foreign", email="foreign@example.invalid", base_ccy="EUR", tz="UTC"),
            Broker(broker_id=1, code="paper", name="Paper"),
        ]
    )
    session.flush()
    session.add(
        LinkedBrokerAccount(
            account_id=1,
            user_id="foreign",
            broker_id=1,
            environment="paper",
            base_ccy="EUR",
            display_name="Foreign",
            paper_initial_equity=100,
            paper_initial_cash=100,
        )
    )
    session.add(
        OutboxEvent(
            topic="execution.commands",
            event_type="execution.command",
            payload={"user_id": "candidate", "execution_policy": {"broker_account_id": 1}},
        )
    )
    session.commit()
    with pytest.raises(OwnerOnboardingError, match=r"outbox.*account"):
        initialize_owner(session, profile={}, existing_user_id="candidate")
    assert session.get(User, "candidate").is_deployment_owner is False
