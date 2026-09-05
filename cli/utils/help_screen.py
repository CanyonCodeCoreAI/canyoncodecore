"""Custom help screen for the canyonos CLI."""

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from canyonos import ui
from canyonos.theme import GREEN, WHITE

# The single source of truth for command descriptions: cli.py registers every
# subparser through this table, so a command can't be added to one and missed
# in the other.
CORE_COMMANDS = (
    ("build", "Port an existing project into a CanyonOS workflow"),
    ("deploy", "Build and launch the workflow, then open the dashboard"),
    ("config", "Configure project settings"),
)

# The three teardown commands differ only in what they leave behind, so each
# description says so explicitly rather than all three reading as "stop stuff".
UTIL_COMMANDS = (
    ("clean", "Delete the generated .car folder from this project"),
    ("doctor", "Check Docker, git and a coding agent are all available"),
    ("logs", "Follow the running deploy's logs"),
    ("new-app", "Create a barebones CanyonOS project"),
    ("quit", "Stop the deploy and remove the container and its files"),
    ("serve", "Start local CanyonOS dashboard"),
    ("status", "Check whether a deploy is running and where it answers"),
    ("stop", "Stop the running deploy, keeping the container and files"),
    ("test", "Deploy locally and run one prompt end to end"),
    ("version", "Print canyonos version"),
)

DESCRIPTIONS = dict(CORE_COMMANDS + UTIL_COMMANDS)


# Both columns are sized from the widest entry across BOTH tables, so Core and
# Utils line up with each other instead of each shrinking to fit its own rows.
_ALL_COMMANDS = CORE_COMMANDS + UTIL_COMMANDS
_NAME_WIDTH = max(len(name) for name, _ in _ALL_COMMANDS)
_DESCRIPTION_WIDTH = max(len(description) for _, description in _ALL_COMMANDS)


def _command_table(commands):
    table = Table(show_header=False, border_style="dim", padding=(0, 2))
    table.add_column(style=f"bold {GREEN}", width=_NAME_WIDTH)
    table.add_column(style=WHITE, width=_DESCRIPTION_WIDTH)
    for name, description in commands:
        table.add_row(name, description)
    return table


def print_custom_help():
    """Print a custom, visually appealing help screen."""
    title = Text("CanyonOS CLI", style=f"bold {GREEN}")
    subtitle = Text("\nBuild, deploy, and manage agentic workflows with ease", style="dim")
    ui.console.print(Panel(title + subtitle, border_style=GREEN, padding=(1, 2)))

    ui.console.print(f"\n[bold {GREEN}]Core Commands[/]")
    ui.console.print(_command_table(CORE_COMMANDS))

    ui.console.print(f"\n[bold {GREEN}]Utils[/]")
    ui.console.print(_command_table(UTIL_COMMANDS))

    ui.console.print(f"\n[bold {GREEN}]Quick Start:[/]")
    ui.console.print(f"  [dim]1.[/dim] cd [{GREEN}]into-your-workflow-root-dir[/]")
    ui.console.print("  [dim]2.[/dim] canyonos build (opens coding agent)")
    ui.console.print("  [dim]3.[/dim] canyonos deploy (UI automatically starts)")

    ui.console.print(f"[dim]For command-specific help: [{GREEN}]canyonos <command> --help[/][/dim]\n")
