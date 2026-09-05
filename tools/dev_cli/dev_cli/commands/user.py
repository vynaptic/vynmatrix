"""User management commands for onboarding."""

import re
import sys
from datetime import UTC
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()

# Project root detection
# user.py -> commands -> dev_cli -> dev_cli -> tools -> PROJECT_ROOT
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent.resolve()
_BASE_CURRENCY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{2,9}$")

# Add libs to path for imports
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_application"))
sys.path.insert(0, str(PROJECT_ROOT / "libs" / "python" / "lib_common"))

from lib_common.asset_classes import (  # noqa: E402
    CANONICAL_ASSET_CLASS_VALUES,
    normalize_asset_class,
)


def _get_db_session():  # noqa: ANN202 - Returns Session but import is conditional
    """Get database session."""
    try:
        from sqlalchemy.orm import Session  # noqa: PLC0415

        from lib_application.db.session import create_engine_for_env  # noqa: PLC0415

        engine = create_engine_for_env()
        return Session(engine)
    except ImportError as e:
        console.print(f"[red]Error: Could not import database modules: {e}[/red]")
        console.print("[dim]Make sure lib_application is built: vmdev build libs[/dim]")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error connecting to database: {e}[/red]")
        console.print("[dim]Make sure PostgreSQL is running: vmdev db start[/dim]")
        sys.exit(1)


def _load_config(config_path: str) -> dict[str, Any]:
    """Load user configuration from YAML file."""
    import yaml  # noqa: PLC0415

    path = Path(config_path)
    if not path.exists():
        console.print(f"[red]Error: Config file not found: {config_path}[/red]")
        sys.exit(1)

    with path.open() as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def _require_base_currency(value: Any, *, field_name: str) -> str:
    """Return an explicit canonical account currency or fail closed."""

    if not isinstance(value, str) or not _BASE_CURRENCY_PATTERN.fullmatch(value):
        msg = f"{field_name} must be an uppercase 3-10 character currency code"
        raise ValueError(msg)
    return value


def _require_paper_capital(broker_data: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Validate explicit local-paper capital without inventing a balance."""
    if broker_data.get("environment") != "paper":
        if (
            broker_data.get("paper_initial_equity") is not None
            or broker_data.get("paper_initial_cash") is not None
        ):
            msg = "Paper capital fields are invalid for a live broker account"
            raise ValueError(msg)
        return None, None
    try:
        equity = Decimal(str(broker_data["paper_initial_equity"]))
        cash = Decimal(str(broker_data["paper_initial_cash"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        msg = "Paper broker accounts require explicit initial equity and cash"
        raise ValueError(msg) from exc
    if not equity.is_finite() or equity <= 0:
        msg = "paper_initial_equity must be a positive finite amount"
        raise ValueError(msg)
    if not cash.is_finite() or cash < 0 or cash > equity:
        msg = "paper_initial_cash must be finite and between zero and initial equity"
        raise ValueError(msg)
    return equity, cash


def _create_user_interactive() -> dict[str, Any]:
    """Interactively collect user information."""
    console.print("\n[bold cyan]== New User Setup ==[/bold cyan]\n")

    # Basic info
    email = Prompt.ask("[cyan]Email address[/cyan]")
    full_name = Prompt.ask("[cyan]Full name[/cyan]")
    timezone = Prompt.ask("[cyan]Timezone[/cyan]", default="UTC")
    base_currency = _require_base_currency(
        Prompt.ask("[cyan]Base currency (for example EUR or USD)[/cyan]"),
        field_name="base_currency",
    )

    # Plan selection
    console.print("\n[bold]Available Plans:[/bold]")
    console.print("  1. free     - 2 strategies, paper trading only")
    console.print("  2. starter  - 5 strategies, Coinbase live")
    console.print("  3. pro      - 20 strategies, multiple brokers")
    console.print("  4. enterprise - Unlimited")
    plan = Prompt.ask(
        "[cyan]Select plan[/cyan]",
        choices=["free", "starter", "pro", "enterprise"],
        default="free",
    )

    # Trading preferences
    console.print("\n[bold]Trading Preferences:[/bold]")
    asset_class = Prompt.ask(
        "[cyan]Primary asset class[/cyan]",
        choices=list(CANONICAL_ASSET_CLASS_VALUES),
        default="crypto",
    )

    horizon = Prompt.ask(
        "[cyan]Trading horizon[/cyan]",
        choices=["scalp", "intraday", "swing", "position"],
        default="swing",
    )

    default_method = Prompt.ask(
        "[cyan]Default execution method[/cyan]",
        choices=["SPOT", "PERP", "OPTIONS_STRATEGY"],
        default="SPOT",
    )

    # Broker setup
    console.print("\n[bold]Broker Configuration:[/bold]")
    setup_broker = Confirm.ask("[cyan]Set up broker account now?[/cyan]", default=False)

    broker_config = None
    if setup_broker:
        broker = Prompt.ask(
            "[cyan]Select broker[/cyan]",
            choices=["coinbase", "ibkr", "deribit", "saxo", "zerodha", "delta"],
            default="coinbase",
        )
        default_name = f"My {broker.title()}"
        account_name = Prompt.ask("[cyan]Account display name[/cyan]", default=default_name)
        environment = Prompt.ask(
            "[cyan]Environment[/cyan]",
            choices=["paper", "live"],
            default="paper",
        )
        account_base_currency = _require_base_currency(
            Prompt.ask("[cyan]Broker account base currency[/cyan]"),
            field_name="broker.base_currency",
        )
        broker_config = {
            "broker": broker,
            "display_name": account_name,
            "environment": environment,
            "base_currency": account_base_currency,
        }
        if environment == "paper":
            broker_config["paper_initial_equity"] = Prompt.ask(
                "[cyan]Paper account initial equity[/cyan]"
            )
            broker_config["paper_initial_cash"] = Prompt.ask(
                "[cyan]Paper account initial cash[/cyan]"
            )
            _require_paper_capital(broker_config)

    return {
        "email": email,
        "full_name": full_name,
        "timezone": timezone,
        "base_currency": base_currency,
        "plan": plan,
        "trading_policy": {
            "asset_class": asset_class,
            "horizon": horizon,
            "default_method": default_method,
        },
        "broker": broker_config,
    }


def _add_user_to_db(user_data: dict[str, Any], session: Any) -> str:
    """Add user to database and return ``user_id``.

    Returns the canonical UUID-form ``user_id`` (string). Older versions of
    this function declared the return type as ``int`` and called
    ``int(user.user_id)`` — that was always wrong because the schema
    stores ``user_id`` as ``String(50)``; the bug surfaced once the
    column gained a ``generate_uuid`` default.
    """
    from datetime import datetime  # noqa: PLC0415

    from lib_application.db.models import (  # noqa: PLC0415
        LinkedBrokerAccount,
        Organization,
        Plan,
        User,
        UserPlanSubscription,
        UserRole,
        UserTradingPolicy,
    )

    now = datetime.now(tz=UTC)
    base_currency = _require_base_currency(
        user_data.get("base_currency"),
        field_name="base_currency",
    )
    broker_data = user_data.get("broker")
    broker_base_currency = (
        _require_base_currency(
            broker_data.get("base_currency"),
            field_name="broker.base_currency",
        )
        if broker_data
        else None
    )
    paper_initial_equity, paper_initial_cash = (
        _require_paper_capital(broker_data) if broker_data else (None, None)
    )

    # Get or create organization (default org for now)
    org = session.query(Organization).first()
    if not org:
        org = Organization(name="vynmatrix Trading", created_at=now)
        session.add(org)
        session.flush()

    # Check if user already exists
    existing = session.query(User).filter_by(email=user_data["email"]).first()
    if existing:
        console.print(f"[yellow]User already exists: {user_data['email']}[/yellow]")
        return str(existing.user_id)

    # Create user
    user = User(
        org_id=org.org_id,
        email=user_data["email"],
        full_name=user_data["full_name"],
        tz=user_data.get("timezone", "UTC"),
        base_ccy=base_currency,
        status="active",
        created_at=now,
    )
    session.add(user)
    session.flush()

    # Add trader role
    role = UserRole(user_id=user.user_id, role="trader")
    session.add(role)

    # Add plan subscription
    plan_code = user_data.get("plan", "free")
    plan = session.query(Plan).filter_by(code=plan_code).first()
    if plan:
        subscription = UserPlanSubscription(
            user_id=user.user_id,
            plan_id=plan.plan_id,
            status="active",
            started_at=now,
        )
        session.add(subscription)

    # Add trading policy
    policy_data = user_data.get("trading_policy", {})
    if policy_data:
        asset_class = normalize_asset_class(
            policy_data.get("asset_class", "crypto"),
            field_name="trading_policy.asset_class",
        )
        policy = UserTradingPolicy(
            user_id=user.user_id,
            asset_class=asset_class,
            horizon=policy_data.get("horizon", "swing"),
            methods_allowed=[policy_data.get("default_method", "SPOT")],
            default_method=policy_data.get("default_method", "SPOT"),
            sizing_rules={"mode": "fixed_notional", "value": 1000},
        )
        session.add(policy)

    # Add broker account if configured
    if broker_data:
        from lib_application.db.models import Broker  # noqa: PLC0415

        assert broker_base_currency is not None
        broker = session.query(Broker).filter_by(code=broker_data["broker"]).first()
        if broker:
            # ``LinkedBrokerAccount`` carries the environment as a plain
            # string column (``paper`` / ``live``) rather than a FK to
            # ``broker_environments``; older revisions of this code
            # referenced a non-existent ``env_id`` field.
            # Schema CHECK constraint allows ``connected``/``revoked``/``error``;
            # an "I created this in the CLI but haven't verified credentials"
            # state isn't modelled — fresh accounts land as ``connected`` and
            # are revoked later if a credential check fails.
            account = LinkedBrokerAccount(
                user_id=user.user_id,
                broker_id=broker.broker_id,
                environment=broker_data["environment"],
                display_name=broker_data["display_name"],
                base_ccy=broker_base_currency,
                paper_initial_equity=paper_initial_equity,
                paper_initial_cash=paper_initial_cash,
                status="connected",
                created_at=now,
            )
            session.add(account)

    session.commit()
    return str(user.user_id)


@click.group()
def user() -> None:
    """User management commands.

    Add, list, and manage user profiles for the trading platform.
    """


@user.command()
@click.option("--config", "-c", "config_file", help="YAML config file for batch user creation")
def add(config_file: str | None) -> None:
    """Add a new user (interactive or from config file).

    Examples:

        vmdev user add                    # Interactive mode
        vmdev user add --config users.yaml  # From config file
    """
    session = _get_db_session()

    if config_file:
        # Batch mode from config file
        config = _load_config(config_file)
        users_data = config.get("users", [config])  # Support single user or list

        console.print(f"[cyan]Adding {len(users_data)} user(s) from config...[/cyan]\n")

        for user_data in users_data:
            try:
                user_id = _add_user_to_db(user_data, session)
                console.print(f"[green]✓ Added user: {user_data['email']} (ID: {user_id})[/green]")
            except Exception as e:
                email = user_data.get("email", "unknown")
                console.print(f"[red]✗ Failed to add {email}: {e}[/red]")
                session.rollback()
    else:
        # Interactive mode
        user_data = _create_user_interactive()

        # Confirm
        console.print("\n[bold]Summary:[/bold]")
        console.print(f"  Email: {user_data['email']}")
        console.print(f"  Name: {user_data['full_name']}")
        console.print(f"  Plan: {user_data['plan']}")
        console.print(f"  Asset Class: {user_data['trading_policy']['asset_class']}")
        console.print(f"  Default Method: {user_data['trading_policy']['default_method']}")
        if user_data.get("broker"):
            broker_info = user_data["broker"]
            console.print(f"  Broker: {broker_info['broker']} ({broker_info['environment']})")

        if not Confirm.ask("\n[cyan]Create this user?[/cyan]", default=True):
            console.print("[dim]Cancelled[/dim]")
            return

        try:
            user_id = _add_user_to_db(user_data, session)
            console.print(f"\n[bold green]✓ User created! (ID: {user_id})[/bold green]")
        except Exception as e:
            console.print(f"[red]✗ Failed to create user: {e}[/red]")
            session.rollback()
            sys.exit(1)


@user.command("list")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed information")
def list_users(verbose: bool) -> None:
    """List all users in the system."""
    session = _get_db_session()

    try:
        from lib_application.db.models import User  # noqa: PLC0415

        users = session.query(User).all()

        if not users:
            console.print("[yellow]No users found.[/yellow]")
            console.print("[dim]Add users with: vmdev user add[/dim]")
            return

        table = Table(title="Users", show_header=True, header_style="bold cyan")
        table.add_column("ID", justify="right")
        table.add_column("Email")
        table.add_column("Name")
        table.add_column("Status")

        if verbose:
            table.add_column("Timezone")
            table.add_column("Currency")
            table.add_column("Created")

        for u in users:
            row = [str(u.user_id), u.email, u.full_name or "-", u.status or "active"]
            if verbose:
                created = str(u.created_at)[:10] if u.created_at else "-"
                row.extend([u.tz or "UTC", u.base_ccy, created])
            table.add_row(*row)

        console.print(table)
        console.print(f"\n[dim]Total: {len(users)} user(s)[/dim]")

    except Exception as e:
        console.print(f"[red]Error listing users: {e}[/red]")
        sys.exit(1)


@user.command()
@click.argument("user_id", type=str)
def show(user_id: str) -> None:
    """Show detailed information for a specific user."""
    session = _get_db_session()

    try:
        from lib_application.db.models import (  # noqa: PLC0415
            LinkedBrokerAccount,
            Plan,
            User,
            UserPlanSubscription,
            UserTradingPolicy,
        )

        user = session.query(User).filter_by(user_id=user_id).first()
        if not user:
            console.print(f"[red]User not found: {user_id}[/red]")
            sys.exit(1)

        console.print(f"\n[bold cyan]User: {user.full_name or user.email}[/bold cyan]\n")

        # Basic info
        console.print("[bold]Basic Information:[/bold]")
        console.print(f"  ID: {user.user_id}")
        console.print(f"  Email: {user.email}")
        console.print(f"  Status: {user.status}")
        console.print(f"  Timezone: {user.tz}")
        console.print(f"  Currency: {user.base_ccy}")

        # Plan
        subscription = (
            session.query(UserPlanSubscription).filter_by(user_id=user_id, status="active").first()
        )
        if subscription:
            plan = session.query(Plan).filter_by(plan_id=subscription.plan_id).first()
            console.print(f"\n[bold]Plan:[/bold] {plan.code if plan else 'Unknown'}")

        # Trading policies
        policies = session.query(UserTradingPolicy).filter_by(user_id=user_id).all()
        if policies:
            console.print("\n[bold]Trading Policies:[/bold]")
            for p in policies:
                console.print(f"  {p.asset_class}/{p.horizon}: {p.default_method}")

        # Linked accounts
        accounts = session.query(LinkedBrokerAccount).filter_by(user_id=user_id).all()
        if accounts:
            console.print("\n[bold]Linked Broker Accounts:[/bold]")
            for a in accounts:
                console.print(f"  {a.display_name}: {a.status}")

    except Exception as e:
        console.print(f"[red]Error showing user: {e}[/red]")
        sys.exit(1)
