"""Custom help screen for the canyonos CLI."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# The single source of truth for command descriptions: cli.py registers every
# subparser through this table, so a command can't be added to one and missed
# in the other.
CORE_COMMANDS = (
    ("build", "Build a compatable workflow with an agent"),
    ("deploy", "Deploy agents"),
    ("config", "Configure project settings"),
)

UTIL_COMMANDS = (
    ("clean", "Remove the generated .car folder from build"),
    ("doctor", "See if all required tools are up"),
    ("logs", "View canyonos logs"),
    ("new-app", "Create a barebones CanyonOS project"),
    ("quit", "Shut down CanyonOS services"),
    ("serve", "Start local CanyonOS dashboard"),
    ("stop", "Stop running containers"),
    ("test", "Run the deployed workflow locally with a test query"),
    ("version", "Print canyonos version"),
)

DESCRIPTIONS = dict(CORE_COMMANDS + UTIL_COMMANDS)


def _command_table(commands):
    table = Table(show_header=False, border_style="dim", padding=(0, 2))
    table.add_column(style="cyan", width=20)
    table.add_column(style="white")
    for name, description in commands:
        table.add_row(name, description)
    return table


def print_custom_help():
    """Print a custom, visually appealing help screen."""
    console = Console()

    title = Text("CanyonOS CLI", style="bold cyan")
    subtitle = Text("\nBuild, deploy, and manage agentic workflows with ease\n", style="dim")
    console.print(Panel(title + subtitle, border_style="cyan", padding=(1, 2)))

    console.print("\n[bold yellow]Core Commands[/bold yellow]")
    console.print(_command_table(CORE_COMMANDS))

    console.print("\n[bold yellow]Utils[/bold yellow]")
    console.print(_command_table(UTIL_COMMANDS))

    console.print("\n[bold green]Quick Start:[/bold green]")
    console.print("  [dim]1.[/dim] cd [cyan]into-your-workflow-root-dir[/cyan]")
    console.print("  [dim]2.[/dim] canyonos build (opens coding agent)")
    console.print("  [dim]3.[/dim] canyonos deploy (UI automatically starts)")

    console.print("[dim]For command-specific help: [cyan]canyonos <command> --help[/cyan][/dim]\n")
