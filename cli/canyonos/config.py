"""
Logic for `canyonos config`: view or change project/deploy configuration.
"""

import os

import yaml
from rich.console import Console
from rich.table import Table

from canyonos.constants import default_config_path, round_trip_yaml
from canyonos.theme import GREEN, WHITE
from utils.tui import DELETE_ACTION, QUIT_ACTION, select_menu

BACK = "__back__"

OPTIONS = [
    ("view", "View"),
    ("change", "Change"),
]

BORDER = GREEN
HEADER = f"bold {GREEN}"

# Rendered as their own tables (in this order); everything else scalar at the
# top level is collected into a single "General" table.
STRUCTURED_KEYS = ("agents", "otel")


def _fmt(value):
    """Render a YAML value as a compact, single-cell string."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) if value else "-"
    if isinstance(value, dict):
        return ", ".join(f"{k}={_fmt(v)}" for k, v in value.items()) if value else "-"
    return str(value)


def _agents_table(agents):
    table = Table(title="Agents", border_style=BORDER, header_style=HEADER, title_style=HEADER)
    for col in ("Name", "Type", "Replicas", "CPU", "Mem", "Provider", "Port", "Entrypoint"):
        table.add_column(col)

    for agent in agents:
        resources = agent.get("resources") or {}
        # Workflows carry `workflow_file` + `api_port`; plain agents carry
        # `entrypoint` + `redis_port`.
        entry = agent.get("entrypoint") or agent.get("workflow_file") or "-"
        port = agent.get("api_port") or agent.get("redis_port")
        table.add_row(
            _fmt(agent.get("name")),
            agent.get("type", "agent"),
            _fmt(agent.get("replicas")),
            _fmt(resources.get("cpu")),
            _fmt(resources.get("memory")),
            _fmt(agent.get("provider")),
            _fmt(port),
            entry,
        )
    return table


def _otel_table(otel):
    destinations = (otel or {}).get("destinations") or []
    table = Table(
        title="OTel Destinations", border_style=BORDER, header_style=HEADER, title_style=HEADER
    )
    for col in ("Name", "Protocol", "Endpoint", "Insecure", "Headers"):
        table.add_column(col)

    for dest in destinations:
        headers = dest.get("headers") or {}
        table.add_row(
            _fmt(dest.get("name")),
            _fmt(dest.get("protocol")),
            _fmt(dest.get("endpoint")),
            _fmt(dest.get("insecure", False)),
            ", ".join(headers.keys()) if headers else "-",
        )
    return table


def _kv_table(title, data):
    """A two-column Setting/Value table from a flat-ish dict (or single value)."""
    table = Table(title=title, border_style=BORDER, header_style=HEADER, title_style=HEADER)
    table.add_column("Setting", style="bold")
    table.add_column("Value")

    if isinstance(data, dict):
        for key, value in data.items():
            table.add_row(str(key), _fmt(value))
    else:
        table.add_row(title, _fmt(data))
    return table


def _require_config(config_path, console):
    """Resolved config path, or None after reporting that it's missing."""
    config_path = config_path or default_config_path()
    if not os.path.isfile(config_path):
        console.print(f"[red]Config file not found: {config_path}[/red]")
        return None
    return config_path


def run_view_config(config_path=None):
    console = Console()
    config_path = _require_config(config_path, console)
    if config_path is None:
        return

    with open(config_path) as f:
        config = yaml.safe_load(f) or {}

    console.print(_agents_table(config.get("agents") or []))
    console.print()

    if config.get("otel"):
        console.print(_otel_table(config["otel"]))
        console.print()

    # Every other top-level key: dicts get their own table, bare scalars are
    # gathered into a single "General" table.
    general = {}
    for key, value in config.items():
        if key in STRUCTURED_KEYS:
            continue
        if isinstance(value, dict):
            console.print(_kv_table(key, value))
            console.print()
        else:
            general[key] = value

    if general:
        console.print(_kv_table("General", general))


def _is_leaf(value):
    """A value the user edits directly: any scalar, or a list of only scalars.

    Lists of mappings (agents, otel.destinations) are containers to drill into;
    lists of plain scalars (requirements, security_group_ids) are edited whole
    via comma-separated input.
    """
    if isinstance(value, dict):
        return False
    if isinstance(value, list):
        return all(not isinstance(item, (dict, list)) for item in value)
    return True


def _preview(value):
    if isinstance(value, dict):
        return f"{{{len(value)} keys}}"
    if isinstance(value, list) and not _is_leaf(value):
        return f"[{len(value)} items]"
    return _fmt(value)


def _seq_label(index, item):
    if isinstance(item, dict) and item.get("name"):
        return str(item["name"])
    return f"[{index}]"


def _cast(raw, current):
    """Coerce the typed string to the current value's type. Raises ValueError."""
    # bool must precede int: bool is a subclass of int.
    if isinstance(current, bool):
        low = raw.strip().lower()
        if low in ("true", "yes", "y", "1"):
            return True
        if low in ("false", "no", "n", "0"):
            return False
        raise ValueError("expected yes/no")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, list):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return raw


class _Screen:
    """Owns the alt-screen: clears and redraws a persistent breadcrumb header
    (plus a transient status line) before each menu/prompt, so the change
    session replaces the view in place instead of scrolling.
    """

    def __init__(self, console):
        self.console = console
        self.status = ""

    def render(self, breadcrumb):
        self.console.clear()
        path = " \u203a ".join(str(part) for part in breadcrumb) if breadcrumb else "config"
        self.console.print(f"[bold {GREEN}]CanyonOS[/] [{WHITE}]config[/]")
        self.console.print(f"[{WHITE}]{path}[/]")
        if self.status:
            self.console.print(f"[{GREEN}]{self.status}[/]")
        self.console.print()


def _edit_leaf(screen, parent, key, breadcrumb):
    """Prompt for and apply a new value for parent[key]. Returns True if changed."""
    screen.render(breadcrumb)
    console = screen.console
    current = parent[key]
    label = key if not isinstance(key, int) else f"item {key}"
    console.print(f"[bold]{label}[/bold] current: {_fmt(current)}")
    if isinstance(current, list):
        console.print("[dim]enter comma-separated values[/dim]")

    raw = input("New value (blank to cancel): ").strip()
    if raw == "":
        return False

    try:
        parent[key] = _cast(raw, current)
    except ValueError as exc:
        screen.status = f"Invalid value: {exc}"
        return False

    screen.status = f"Set {label} = {_fmt(parent[key])}"
    return True


def _confirm_delete(screen, node, key, breadcrumb):
    """Yes/No confirm menu for deleting node[key]. Returns True to delete."""
    screen.render(breadcrumb)
    label = key if isinstance(node, dict) else _seq_label(key, node[key])
    options = [("yes", f"Yes, delete '{label}'"), ("no", "No, keep it")]
    choice = select_menu(
        options,
        title=f"Delete '{label}' ({_preview(node[key])}) and everything inside?",
    )
    return choice == "yes"


def _navigate(screen, node, breadcrumb):
    """Drill into a mapping/sequence. Returns True if any value was changed or
    deleted, None if the user backed out of this level, or QUIT_ACTION if the
    user quit (which unwinds the whole session from any depth)."""
    while True:
        screen.render(breadcrumb)
        if isinstance(node, dict):
            options = [(k, f"{k}: {_preview(v)}") for k, v in node.items()]
        else:  # list
            options = [(i, f"{_seq_label(i, item)}: {_preview(item)}") for i, item in enumerate(node)]
        options.append((BACK, "\u2190 Back"))

        choice = select_menu(
            options, title="Select a field (d to delete)", deletable=True, quittable=True
        )
        if choice is None:
            return None
        # 'q' anywhere -> unwind the entire session, not just this level.
        if choice is QUIT_ACTION:
            return QUIT_ACTION

        # 'd' over an item -> (DELETE_ACTION, hovered_value).
        if isinstance(choice, tuple) and choice[0] is DELETE_ACTION:
            target = choice[1]
            if target == BACK:
                continue  # the Back entry isn't deletable
            if _confirm_delete(screen, node, target, breadcrumb):
                label = target if isinstance(node, dict) else _seq_label(target, node[target])
                del node[target]
                screen.status = f"Deleted '{label}'"
                return True
            continue  # delete cancelled: stay on this menu

        if choice == BACK:
            return None

        child = node[choice]
        label = choice if isinstance(node, dict) else _seq_label(choice, child)
        if _is_leaf(child):
            if _edit_leaf(screen, node, choice, breadcrumb + [str(label)]):
                return True
            # cancelled/invalid: stay on this menu
        else:
            result = _navigate(screen, child, breadcrumb + [str(label)])
            if result is QUIT_ACTION:
                return QUIT_ACTION
            if result:
                return True
            # backed out of the child: stay on this menu


def run_change_config(config_path=None):
    console = Console()
    config_path = _require_config(config_path, console)
    if config_path is None:
        return

    yaml_rt = round_trip_yaml()
    with open(config_path) as f:
        data = yaml_rt.load(f)

    if not data:
        console.print("[yellow]Config is empty; nothing to change.[/yellow]")
        return

    screen = _Screen(console)
    saves = 0
    # Alternate screen: the whole session replaces the view, and the terminal
    # scrollback is restored untouched on exit.
    console.set_alt_screen(True)
    try:
        while True:
            changed = _navigate(screen, data, ["config"])
            # None = backed out at root, QUIT_ACTION = quit from any depth.
            if changed is None or changed is QUIT_ACTION:
                break
            with open(config_path, "w") as f:
                yaml_rt.dump(data, f)
            saves += 1
            screen.status = f"Saved to {config_path}"
    finally:
        console.set_alt_screen(False)

    if saves:
        console.print(f"[{GREEN}]Saved {saves} change(s) to {config_path}[/]")
    else:
        console.print("No changes made.")


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
