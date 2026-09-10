"""
The CLI's one output surface: every user-facing line goes through here so the
whole tool speaks with the same palette, symbols and spinner.

Messages are emitted as literal text, never as rich markup, so a path or an
error containing square brackets can't be swallowed as a style tag.
"""

from contextlib import contextmanager

from rich.console import Console
from rich.text import Text

from canyonos.theme import GRADIENT, GREEN, WHITE

console = Console()


def set_quiet(quiet):
    """Silence every helper here, so `canyonos test --json` emits only its payload."""
    console.quiet = quiet


def _emit(message, style, symbol=None):
    parts = [(f"{symbol} ", style)] if symbol else []
    parts.append((str(message), WHITE if symbol else style))
    console.print(Text.assemble(*parts))


def say(message):
    _emit(message, WHITE)


def ok(message):
    _emit(message, GREEN, "✓")


def fail(message):
    _emit(message, "bold red", "✗")


def warn(message):
    _emit(message, "yellow", "!")


def hint(message):
    _emit(message, "dim")


def blank():
    console.print()


def gradient(text):
    """Print `text` line by line down the brand ramp (the `init` banner)."""
    for line, color in zip(text.splitlines(), GRADIENT):
        console.print(line, style=color)


def panel(renderable):
    console.print(renderable)


@contextmanager
def status(message):
    with console.status(message) as spinner:
        yield spinner
