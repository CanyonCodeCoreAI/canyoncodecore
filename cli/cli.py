"""
Most of the commands will be executed by code in the canyonos container.
Anything executing in this CLI pertains to file/folder modification
"""

import argparse

from canyonos.clean import run_clean
from canyonos.constants import DEFAULT_CONFIG_PATH
from canyonos.config import run_config
from canyonos.deploy import run_deploy
from canyonos.init import run_init
from canyonos.integrate import run_integrate
from canyonos.logs import run_logs
from canyonos.new_app import run_new_app
from canyonos.quit import run_quit
from canyonos.stop import run_stop
from canyonos.sync import run_sync

def cmd_init(args):
    run_init()

def cmd_connect(args):
    pass

def cmd_quit(args):
    run_quit()

def cmd_new_app(args):
    run_new_app()

# Executed in canyonos: syncs files, then builds + deploys
def cmd_deploy(args):
    run_deploy(args.config)

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

# Executed in canyonos
def cmd_serve(args):
    pass

# Executed in canyonos
def cmd_test(args):
    pass

# Executed in canyonos
def cmd_mega_build(args):
    pass


def cmd_version(args):
    pass


def main():
    parser = argparse.ArgumentParser(prog="canyonos")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("new-app").set_defaults(func=cmd_new_app)
    deploy = subparsers.add_parser("deploy")
    deploy.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to global controller config (default: {DEFAULT_CONFIG_PATH})",
    )
    deploy.set_defaults(func=cmd_deploy)
    subparsers.add_parser("clean").set_defaults(func=cmd_clean)
    subparsers.add_parser("stop").set_defaults(func=cmd_stop)
    subparsers.add_parser("logs").set_defaults(func=cmd_logs)
    subparsers.add_parser("init").set_defaults(func=cmd_init)
    subparsers.add_parser("quit").set_defaults(func=cmd_quit)
    subparsers.add_parser("connect").set_defaults(func=cmd_connect)
    subparsers.add_parser("sync").set_defaults(func=cmd_sync)
    subparsers.add_parser("config").set_defaults(func=cmd_config)
    subparsers.add_parser("integrate").set_defaults(func=cmd_integrate)
    subparsers.add_parser("doctor").set_defaults(func=cmd_doctor)
    subparsers.add_parser("serve").set_defaults(func=cmd_serve)
    subparsers.add_parser("test").set_defaults(func=cmd_test)
    subparsers.add_parser("mega-build").set_defaults(func=cmd_mega_build)

    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
