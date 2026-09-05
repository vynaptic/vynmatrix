"""Main CLI entry point."""

from __future__ import annotations

import click
from rich.console import Console

from dev_cli.commands.audit import audit
from dev_cli.commands.build import build
from dev_cli.commands.clean import clean
from dev_cli.commands.db import db
from dev_cli.commands.format import format
from dev_cli.commands.git import git
from dev_cli.commands.lint import lint
from dev_cli.commands.release import release
from dev_cli.commands.run import run
from dev_cli.commands.test import test
from dev_cli.commands.user import user

console = Console()


class _LazyCLI(click.Group):
    """Load the validation-only strategy command only when it is invoked."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted({*super().list_commands(ctx), "strategy"})

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        if cmd_name == "strategy":
            from dev_cli.commands.strategy import strategy  # noqa: PLC0415

            return strategy
        return super().get_command(ctx, cmd_name)


@click.group(cls=_LazyCLI)
@click.version_option(version="0.1.0")
def cli():
    """
    vynmatrix Developer CLI

    Build, test, and manage your development environment.
    """


# Register command groups
cli.add_command(audit)
cli.add_command(build)
cli.add_command(test)
cli.add_command(clean)
cli.add_command(db)
cli.add_command(format)
cli.add_command(lint)
cli.add_command(git)
cli.add_command(release)
cli.add_command(run)
cli.add_command(user)

if __name__ == "__main__":
    cli()
