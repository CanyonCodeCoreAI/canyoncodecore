"""
Almost all commands will be executing on the canyonos container spawned by deploy
Commands like doctor, version, and new_app will not though
"""

import argparse
import importlib.metadata
import sys

from canyonos import ui
from canyonos.clean import run_clean
from canyonos.config import run_config
from canyonos.deploy import run_deploy
from canyonos.build import run_build
from canyonos.doctor import run_doctor
from canyonos.logs import run_logs
from canyonos.new_app import run_new_app
from canyonos.quit import run_quit
from canyonos.serve import run_serve
from canyonos.status import run_status
from canyonos.stop import run_stop
from canyonos.test import DEFAULT_QUERY, run_test
from utils.help_screen import DESCRIPTIONS, print_custom_help

def _parse_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got: {value!r}")


class _RootParser(argparse.ArgumentParser):
    """Routes the top-level -h/--help through the custom help screen."""

    def print_help(self, file=None):
        print_custom_help()


def main():
    parser = _RootParser(prog="canyonos")
    # Subparsers keep the stock argparse help, so `canyonos <cmd> -h` still
    # describes that command instead of reprinting the top-level screen.
    subparsers = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    def add(name, run):
        # A KeyError here means the command has no entry on the help screen.
        command = subparsers.add_parser(name, help=DESCRIPTIONS[name])
        command.set_defaults(func=run)
        return command

    # Note, not tested much, keeping this in the back burner for now while we flesh out the main path
    add("new-app", lambda args: run_new_app())
    deploy = add("deploy", lambda args: run_deploy(args.config, serve=args.serve, verbose=args.verbose))
    deploy.add_argument(
        "-c",
        "--config",
        help="Path to global controller config (default: resolved by ventis inside the container)",
    )
    deploy.add_argument(
        "--serve",
        type=_parse_bool,
        default=True,
        metavar="true|false",
        help="Automatically launch the local dashboard (canyonos serve) once the workflow is up (default: true)",
    )
    deploy.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream the container's full build and deploy logs instead of a progress summary",
    )
    add("clean", lambda args: run_clean())
    add("stop", lambda args: run_stop())
    add("logs", lambda args: run_logs())
    add("quit", lambda args: run_quit())
    add("config", lambda args: run_config())
    add("build", lambda args: run_build())
    add("doctor", lambda args: sys.exit(0 if run_doctor() else 1))
    add("version", lambda args: ui.say(f"canyonos {importlib.metadata.version('canyonos')}"))
    add("serve", lambda args: sys.exit(run_serve()))
    add("status", lambda args: run_status())
    test = add("test", lambda args: sys.exit(run_test(args.prompt, as_json=args.json)))
    test.add_argument(
        "prompt",
        nargs="?",
        default=DEFAULT_QUERY,
        help=f"Prompt sent to the workflow (default: {DEFAULT_QUERY!r})",
    )
    test.add_argument(
        "--json",
        action="store_true",
        help="Print a single JSON result object and nothing else (for CI)",
    )

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return

    try:
        args.func(args)
    except RuntimeError as e:
        # Docker unreachable, image pull failed, no free port -- all already
        # carry a readable message, so print it rather than a traceback.
        ui.fail(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
