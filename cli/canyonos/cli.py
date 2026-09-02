"""
Most of the commands will be executed by code in the canyonos container.
Anything executing in this CLI pertains to file/folder modification
"""

import argparse

def cmd_init(args):
    pass
  
def cmd_new_app(args):
    pass

def cmd_build(args):
    pass

# Executed in canyonos
def cmd_deploy(args):
    pass

def cmd_clean(args):
    pass

# Executed in canyonos
def cmd_sync(args):
    pass

def cmd_config(args):
    pass

def cmd_integrate(args):
    pass

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


def main():
    parser = argparse.ArgumentParser(prog="canyonos")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("new-app").set_defaults(func=cmd_new_app)
    subparsers.add_parser("build").set_defaults(func=cmd_build)
    subparsers.add_parser("deploy").set_defaults(func=cmd_deploy)
    subparsers.add_parser("clean").set_defaults(func=cmd_clean)
    subparsers.add_parser("init").set_defaults(func=cmd_init)
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
