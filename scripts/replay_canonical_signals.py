"""Replay canonical signals through the execution engine in paper mode."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "execution_engine"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_common"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_strategy"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_application"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_infrastructure"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_data"))

from execution_engine.canonical_execution_store import CanonicalExecutionStore  # noqa: E402
from execution_engine.engine import ExecutionEngine  # noqa: E402
from execution_engine.execution_log_store import ExecutionLogStore  # noqa: E402
from execution_engine.execution_metrics_store import ExecutionMetricsStore  # noqa: E402
from execution_engine.execution_position_store import ExecutionPositionStore  # noqa: E402
from execution_engine.market_data import SqlPriceQuoteProvider  # noqa: E402
from execution_engine.order_builder import OrderBuilder  # noqa: E402
from execution_engine.replay import replay_canonical_signals  # noqa: E402
from execution_engine.risk_breach_store import RiskBreachStore  # noqa: E402
from lib_application.db.models import (  # noqa: E402
    Broker,
    BrokerCredential,
    LinkedBrokerAccount,
    UserStrategyBinding,
)
from lib_application.db.session import create_engine_for_env  # noqa: E402
from lib_application.outbox import OutboxStore  # noqa: E402
from lib_application.services.instrument_resolution import resolve_instrument  # noqa: E402
from lib_common.internal_events import BrokerRouteSnapshot  # noqa: E402
from lib_common.logging import setup_logging  # noqa: E402


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay canonical signals through paper execution")
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--broker-account-id",
        required=True,
        type=int,
        help="Existing paper linked_broker_accounts.account_id owned by --user-id.",
    )
    parser.add_argument("--strategy-id", default="swing_high_low_pmo_v1")
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols; every symbol must be in the active binding scope.",
    )
    parser.add_argument("--start-date", help="Replay start date YYYY-MM-DD")
    parser.add_argument("--end-date", help="Exclusive replay end date YYYY-MM-DD")
    parser.add_argument(
        "--timeframe",
        default="15m",
        help="Replay fill timeframe (currently only 15m, sourced from 1m prices)",
    )
    parser.add_argument(
        "--source",
        default="coinbase_live",
        help="Exact persisted prices.source used for replay fills.",
    )
    parser.add_argument("--max-signals", type=int)
    parser.add_argument(
        "--require-minute-data", action=argparse.BooleanOptionalAction, default=True
    )
    parser.add_argument(
        "--enable-shorting",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Safety assertion only; replay never grants shorting outside persisted policy.",
    )
    parser.add_argument(
        "--require-stop-loss",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Safety assertion for the long-only canary; policy comes from the published command.",
    )
    return parser.parse_args(argv)


def _validate_cli_safety(args: argparse.Namespace) -> None:
    if args.broker_account_id <= 0:
        msg = "--broker-account-id must be a positive integer"
        raise ValueError(msg)
    if args.enable_shorting:
        msg = "Canonical replay cannot grant shorting from an operator CLI flag"
        raise ValueError(msg)
    if not args.require_stop_loss:
        msg = "Canonical replay cannot disable the canary stop-loss requirement"
        raise ValueError(msg)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    _validate_cli_safety(args)
    engine_obj = create_engine_for_env(env="dev")
    session_factory = sessionmaker(engine_obj, expire_on_commit=False)
    canonical_execution_store = CanonicalExecutionStore(session_factory=session_factory)
    with session_factory() as session:
        row = (
            session.query(LinkedBrokerAccount, Broker)
            .join(Broker, LinkedBrokerAccount.broker_id == Broker.broker_id)
            .filter(LinkedBrokerAccount.account_id == args.broker_account_id)
            .one_or_none()
        )
        if row is None or str(row[0].user_id) != str(args.user_id):
            msg = (
                f"Linked broker account {args.broker_account_id} is not owned by "
                f"user {args.user_id}"
            )
            raise ValueError(msg)
        linked_account, broker = row
        if (
            linked_account.environment != "paper"
            or linked_account.status != "connected"
            or broker.code != "paper"
        ):
            msg = "--broker-account-id must select a connected local paper account"
            raise ValueError(msg)
        if linked_account.paper_initial_equity is None or linked_account.paper_initial_cash is None:
            msg = "Paper account must configure initial equity and cash before replay"
            raise ValueError(msg)
        starting_equity = float(linked_account.paper_initial_equity)
        starting_cash = float(linked_account.paper_initial_cash)
        account_currency = str(linked_account.base_ccy).strip().upper()
        bindings = (
            session.query(UserStrategyBinding)
            .filter(
                UserStrategyBinding.user_id == args.user_id,
                UserStrategyBinding.broker_account_id == args.broker_account_id,
                UserStrategyBinding.strategy_id == args.strategy_id,
                UserStrategyBinding.is_active.is_(True),
            )
            .all()
        )
        if len(bindings) != 1:
            msg = (
                "Canonical replay requires exactly one active binding for "
                "the user/account/strategy route"
            )
            raise ValueError(msg)
        binding = bindings[0]
        if not (
            bool(binding.autopilot)
            and bool(binding.entries_enabled)
            and bool(binding.exits_enabled)
        ):
            msg = "Canonical replay binding must explicitly authorize both entries and exits"
            raise ValueError(msg)
        requested_symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        if not requested_symbols:
            msg = "--symbols must contain at least one explicit instrument"
            raise ValueError(msg)
        allowed_instruments = {
            str(item).strip() for item in (binding.instruments_allowed or []) if str(item).strip()
        }
        if not allowed_instruments:
            msg = "Canonical replay requires a bounded binding instrument scope"
            raise ValueError(msg)
        for symbol in requested_symbols:
            instrument = resolve_instrument(session, symbol)
            if instrument is None or str(instrument.canonical) not in allowed_instruments:
                msg = f"Replay symbol is outside the active binding scope: {symbol!r}"
                raise ValueError(msg)
        if (
            session.query(BrokerCredential)
            .filter(BrokerCredential.account_id == args.broker_account_id)
            .first()
            is not None
        ):
            msg = "Canonical replay local-paper account must not carry broker credentials"
            raise ValueError(msg)
        binding_id = int(binding.binding_id)

    engine = ExecutionEngine(
        order_builder=OrderBuilder(),
        market_data_provider=SqlPriceQuoteProvider(session_factory=session_factory),
        execution_log_store=ExecutionLogStore(session_factory=session_factory),
        execution_metrics_store=ExecutionMetricsStore(session_factory=session_factory),
        execution_position_store=ExecutionPositionStore(session_factory=session_factory),
        risk_breach_store=RiskBreachStore(session_factory=session_factory),
        outbox_store=OutboxStore(session_factory),
        canonical_execution_store=canonical_execution_store,
        default_mode="paper",
        allow_live=False,
        session_factory=session_factory,
    )

    symbols = requested_symbols
    start_date = (
        datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        if args.start_date
        else None
    )
    end_date = (
        datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=UTC) if args.end_date else None
    )

    credential_ref = "paper-paper"
    account_snapshot = {
        "account_id": args.broker_account_id,
        "broker": "paper",
        "environment": "paper",
        "status": "connected",
        "base_ccy": account_currency,
        "paper_initial_equity": starting_equity,
        "paper_initial_cash": starting_cash,
        "credential_ref": credential_ref,
        "binding_id": binding_id,
    }
    route_snapshot = BrokerRouteSnapshot(
        broker="paper",
        broker_account_id=args.broker_account_id,
        broker_environment="paper",
        credential_ref=credential_ref,
        allowed_brokers=["paper"],
        sandbox=True,
        route_source="canonical_replay",
        live_enabled=False,
        execution_mode="spot",
    ).model_dump(mode="json")
    profile = {
        "broker": "paper",
        "broker_account_id": args.broker_account_id,
        "binding_id": binding_id,
        "broker_environment": "paper",
        "credential_ref": credential_ref,
        "sandbox": True,
        "equity": starting_equity,
        "available_cash": starting_cash,
        "margin_used": 0.0,
        "unrealized_pnl": 0.0,
        "realized_pnl": 0.0,
        "currency": account_currency,
        "live_enabled": False,
        "accounts": {str(args.broker_account_id): account_snapshot},
        "_broker_route_snapshot": route_snapshot,
    }
    user_strategy_config = {
        "broker": "paper",
        "broker_account_id": args.broker_account_id,
        "binding_id": binding_id,
        "credential_ref": credential_ref,
        "allowed_brokers": ["paper"],
        "execution_mode": "spot",
        "mode": "paper",
    }

    summary = await replay_canonical_signals(
        engine=engine,
        session_factory=session_factory,
        user_id=args.user_id,
        strategy_id=args.strategy_id,
        profile=profile,
        user_strategy_config=user_strategy_config,
        timeframe=args.timeframe,
        source=args.source,
        require_minute_data=args.require_minute_data,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        max_signals=args.max_signals,
        starting_equity=starting_equity,
    )

    aggregate = await engine.get_aggregate_pnl(
        user_id=args.user_id,
        strategy_id=args.strategy_id,
        account_id=args.broker_account_id,
    )
    await engine.close()
    return {
        **summary.__dict__,
        "aggregate_pnl": aggregate,
    }


def main() -> None:
    setup_logging("INFO")
    args = parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
