"""
Most of the commands will be executed by code in the canyonos container.
Anything executing in this CLI pertains to file/folder modification
"""

import argparse
import sys

from canyonos.clean import run_clean
from canyonos.constants import default_config_path
from canyonos.config import run_config
from canyonos.deploy import run_deploy
from canyonos.integrate import run_integrate
from canyonos.logs import run_logs
from canyonos.new_app import run_new_app
from canyonos.quit import run_quit
from canyonos.serve import run_serve
from canyonos.stop import run_stop
from canyonos.sync import run_sync

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

def cmd_connect(args):
    pass

def cmd_quit(args):
    run_quit()

def cmd_new_app(args):
    run_new_app()

# Executed in canyonos: syncs files, then builds + deploys
def cmd_deploy(args):
    run_deploy(args.config, serve=args.serve)

def cmd_clean(args):
    run_clean()

def cmd_stop(args):
    run_stop()

def cmd_logs(args):
    run_logs()

def cmd_sync(args):
    run_sync()

def cmd_config(args):
    run_config()

def cmd_integrate(args):
    run_integrate()

def cmd_doctor(args):
    pass

def cmd_serve(args):
    sys.exit(run_serve(args.config))

# Executed in canyonos
def cmd_test(args):
    pass

# Executed in canyonos
def cmd_mega_build(args):
    pass


def cmd_version(args):
    pass


def print_custom_help():
    """Print a custom, visually appealing help screen."""
    if RICH_AVAILABLE:
        console = Console()
        
        # Header
        title = Text("CanyonOS CLI", style="bold cyan")
        subtitle = Text("\nBuild, deploy, and manage agentic workflows with ease\n", style="dim")
        
        console.print(Panel(title + subtitle, border_style="cyan", padding=(1, 2)))
        
        # Core commands
        console.print("\n[bold yellow]Core Commands[/bold yellow]")
        core_table = Table(show_header=False, border_style="dim", padding=(0, 2))
        core_table.add_column(style="cyan", width=20)
        core_table.add_column(style="white")
        core_table.add_row("integrate", "Sync source files to .car/app/")
        core_table.add_row("deploy", "Build and deploy agents to configured hosts")
        core_table.add_row("config", "Configure project settings")
        console.print(core_table)
        
        # Utils commands
        console.print("\n[bold yellow]Utils[/bold yellow]")
        utils_table = Table(show_header=False, border_style="dim", padding=(0, 2))
        utils_table.add_column(style="cyan", width=20)
        utils_table.add_column(style="white")
        utils_table.add_row("new-app", "Create a new CanyonOS project")
        utils_table.add_row("serve", "Start local CanyonOS dashboard")
        utils_table.add_row("sync", "Sync files with container")
        utils_table.add_row("stop", "Stop running containers")
        utils_table.add_row("clean", "Remove generated files")
        utils_table.add_row("logs", "View container logs")
        utils_table.add_row("doctor", "Check system health")
        utils_table.add_row("connect", "Connect to remote host")
        utils_table.add_row("quit", "Shut down CanyonOS services")
        console.print(utils_table)
        
        # Quick start
        console.print("\n[bold green]Quick Start:[/bold green]")
        console.print("  [dim]1.[/dim] canyonos new-app [cyan]my-app[/cyan]")
        console.print("  [dim]2.[/dim] cd [cyan]my-app[/cyan]")
        console.print("  [dim]3.[/dim] canyonos integrate")
        console.print("  [dim]4.[/dim] canyonos deploy")
        console.print("  [dim]5.[/dim] canyonos serve\n")
        
        console.print("[dim]For command-specific help: [cyan]canyonos <command> --help[/cyan][/dim]\n")
    else:
        # Fallback to simple text if rich is not available
        print("\n" + "="*60)
        print(" " * 20 + "CanyonOS CLI")
        print(" " * 10 + "Build, deploy, and manage agentic workflows")
        print("="*60 + "\n")
        
        print("CORE COMMANDS:")
        print("  integrate    Sync source files to .car/app/")
        print("  deploy       Build and deploy agents to configured hosts")
        print("  config       Configure project settings\n")
        
        print("UTILS:")
        print("  new-app      Create a new CanyonOS project")
        print("  serve        Start local CanyonOS dashboard")
        print("  sync         Sync files with container")
        print("  stop         Stop running containers")
        print("  clean        Remove generated files")
        print("  logs         View container logs")
        print("  doctor       Check system health")
        print("  connect      Connect to remote host")
        print("  quit         Shut down CanyonOS services\n")
        
        print("QUICK START:")
        print("  1. canyonos new-app my-app")
        print("  2. cd my-app")
        print("  3. canyonos integrate")
        print("  4. canyonos deploy")
        print("  5. canyonos serve\n")
        
        print("For command-specific help: canyonos <command> --help\n")


def _parse_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got: {value!r}")


def main():
    parser = argparse.ArgumentParser(prog="canyonos")
    subparsers = parser.add_subparsers(dest="command")
    config_default = default_config_path()

    subparsers.add_parser("new-app").set_defaults(func=cmd_new_app)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument(
        "-c",
        "--config",
        default=config_default,
        help=f"Path to global controller config (default: {config_default})",
    )
    deploy.add_argument(
        "--serve",
        type=_parse_bool,
        default=True,
        metavar="true|false",
        help="Automatically launch the local dashboard (canyonos serve) once the workflow is up (default: true)",
    )
    deploy.set_defaults(func=cmd_deploy)
    subparsers.add_parser("clean").set_defaults(func=cmd_clean)
    subparsers.add_parser("stop").set_defaults(func=cmd_stop)
    subparsers.add_parser("logs").set_defaults(func=cmd_logs)
    subparsers.add_parser("quit").set_defaults(func=cmd_quit)
    subparsers.add_parser("connect").set_defaults(func=cmd_connect)
    subparsers.add_parser("sync").set_defaults(func=cmd_sync)
    subparsers.add_parser("config").set_defaults(func=cmd_config)
    subparsers.add_parser("integrate").set_defaults(func=cmd_integrate)
    subparsers.add_parser("doctor").set_defaults(func=cmd_doctor)
    serve = subparsers.add_parser("serve")
    serve.add_argument(
        "-c",
        "--config",
        default=config_default,
        help=f"Path to global controller config (default: {config_default})",
    )
    serve.set_defaults(func=cmd_serve)
    subparsers.add_parser("test").set_defaults(func=cmd_test)
    subparsers.add_parser("mega-build").set_defaults(func=cmd_mega_build)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        print_custom_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
