"""Command-line entry point. Subcommands are added milestone by milestone."""

import typer

from revertbench import __version__

app = typer.Typer(help="RevertBench: mine, build, validate, run and report regression cases.")


@app.callback()
def main() -> None:
    """RevertBench command line."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)
