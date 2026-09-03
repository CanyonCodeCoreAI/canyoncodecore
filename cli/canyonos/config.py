"""
Logic for `canyonos config`: view or change project/deploy configuration.
"""

from rich.console import Console

from utils.tui import select_menu

OPTIONS = [
    ("view", "View"),
    ("change", "Change"),
]


def run_view_config():
    pass


def run_change_config():
    pass


def run_config():
    console = Console()
    choice = select_menu(OPTIONS, title="What do you want to do?")
    if choice is None:
        console.print("Cancelled.")
        return

    if choice == "view":
        run_view_config()
    elif choice == "change":
        run_change_config()
